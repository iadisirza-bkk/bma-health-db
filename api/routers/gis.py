"""
GIS router — facility locations, disease heatmaps, PM2.5 overlay, boundaries.
New endpoints for map dashboard (Docs 01-08).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from database import execute_query, execute_scalar
from cache import cache_get, cache_set, TTL_T1_EXTERNAL, TTL_T2_AGGREGATE, TTL_T4_STATIC
from data.pm25_stations import (
    STANDARD_TH, STANDARD_WHO, pm25_to_aqi, extract_district_name,
)

logger = logging.getLogger("bma.gis")

router = APIRouter(prefix="/api/v2/gis", tags=["GIS"])


# =========================================================================== #
# Facility endpoints
# =========================================================================== #


@router.get("/facilities")
def list_facilities(
    zone_code: Optional[str] = Query(None, description="Filter by zone"),
    district_code: Optional[str] = Query(None, description="Filter by district"),
    facility_type: Optional[str] = Query(None, description="Filter by ct_name (facility type)"),
    limit: int = Query(500, ge=1, le=15000),
    offset: int = Query(0, ge=0),
):
    """All facilities with lat/lng from clinic_latlong data (14,063 records).
    ดึงรายชื่อสถานบริการทั้งหมดพร้อมพิกัด สำหรับแสดงบนแผนที่"""

    conditions = ["latitude IS NOT NULL"]
    params: list = []

    if zone_code:
        conditions.append("zone_code = %s")
        params.append(zone_code)
    if district_code:
        conditions.append("district_code = %s")
        params.append(district_code)
    if facility_type:
        conditions.append("ct_name ILIKE %s")
        params.append(f"%{facility_type}%")

    where = " AND ".join(conditions)

    total = execute_scalar(f"SELECT COUNT(*) FROM ref_facilities WHERE {where}", tuple(params)) or 0

    params.extend([limit, offset])
    rows = execute_query(f"""
        SELECT code, name_th, name_en, facility_type, district_code, zone_code,
               latitude, longitude, address, telephone, ct_id, ct_name
        FROM ref_facilities
        WHERE {where}
        ORDER BY code
        LIMIT %s OFFSET %s
    """, tuple(params))

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "data": rows,
    }


@router.get("/facilities/{code}")
def get_facility(code: str):
    """Single facility detail with screening stats if available.
    ดึงรายละเอียดสถานบริการเดียว พร้อมสถิติการคัดกรอง (ถ้ามี)"""

    rows = execute_query("""
        SELECT f.code, f.name_th, f.name_en, f.facility_type,
               f.district_code, f.zone_code,
               f.latitude, f.longitude, f.address, f.telephone,
               f.ct_id, f.ct_name,
               s.total_screened, s.risk_dm, s.risk_hpt,
               s.found_dm, s.found_hpt, s.found_obesity,
               s.lab_completed, s.first_screening, s.last_screening
        FROM ref_facilities f
        LEFT JOIN summary_facility s ON s.facility_code = f.code
        WHERE f.code = %s
    """, (code,))

    if not rows:
        raise HTTPException(status_code=404, detail=f"Facility '{code}' not found")
    return rows[0]


@router.get("/facilities/zone/{zone_code}")
def facilities_by_zone(zone_code: str):
    """Facilities in a specific health zone with screening summary.
    ดึงสถานบริการทั้งหมดในโซนสุขภาพ"""

    rows = execute_query("""
        SELECT f.code, f.name_th, f.latitude, f.longitude, f.ct_name,
               f.district_code,
               s.total_screened, s.risk_dm, s.risk_hpt
        FROM ref_facilities f
        LEFT JOIN summary_facility s ON s.facility_code = f.code
        WHERE f.zone_code = %s AND f.latitude IS NOT NULL
        ORDER BY f.code
    """, (zone_code,))

    return {"zone_code": zone_code, "total": len(rows), "facilities": rows}


@router.get("/facilities/district/{district_code}")
def facilities_by_district(district_code: str):
    """Facilities in a specific district.
    ดึงสถานบริการทั้งหมดในเขต"""

    rows = execute_query("""
        SELECT f.code, f.name_th, f.latitude, f.longitude, f.ct_name,
               f.address, f.telephone,
               s.total_screened, s.risk_dm, s.risk_hpt
        FROM ref_facilities f
        LEFT JOIN summary_facility s ON s.facility_code = f.code
        WHERE f.district_code = %s AND f.latitude IS NOT NULL
        ORDER BY f.code
    """, (district_code,))

    return {"district_code": district_code, "total": len(rows), "facilities": rows}


@router.get("/facility-types")
def facility_types():
    """List all facility types with counts.
    ดึงประเภทสถานบริการทั้งหมดพร้อมจำนวน"""

    rows = execute_query("""
        SELECT ct_name, ct_id, COUNT(*) as count
        FROM ref_facilities
        WHERE ct_name IS NOT NULL AND latitude IS NOT NULL
        GROUP BY ct_name, ct_id
        ORDER BY count DESC
    """)
    return {"types": rows}


# =========================================================================== #
# Disease heatmap
# =========================================================================== #


@router.get("/heatmap/disease/{disease_key}")
def disease_heatmap(disease_key: str):
    """District centroids + disease prevalence for heatmap rendering.
    ดึงพิกัดกลางเขต + อัตราโรค สำหรับแสดง heatmap บนแผนที่"""

    valid_keys = {
        "diabetes": ("pct_risk_dm", "risk_dm_count"),
        "hypertension": ("pct_risk_hpt", "risk_hpt_count"),
        "cardiovascular": ("pct_risk_cvd", "risk_cvd_count"),
        "obesity": ("risk_bmi_count", "risk_bmi_count"),
        "dyslipidemia": ("found_dyslipidemia_count", "found_dyslipidemia_count"),
        "stroke": ("found_stroke_count", "found_stroke_count"),
    }

    if disease_key not in valid_keys:
        raise HTTPException(status_code=400, detail=f"Invalid disease_key. Valid: {sorted(valid_keys)}")

    pct_col, count_col = valid_keys[disease_key]

    rows = execute_query(f"""
        SELECT
            d.district_code,
            d.district_name,
            AVG(f.latitude) AS centroid_lat,
            AVG(f.longitude) AS centroid_lng,
            d.total_screened,
            d.{pct_col} AS disease_value,
            d.{count_col} AS disease_count
        FROM summary_district_disease d
        LEFT JOIN ref_facilities f ON f.district_code = d.district_code
            AND f.latitude IS NOT NULL
        WHERE d.total_screened >= 5
        GROUP BY d.district_code, d.district_name, d.total_screened,
                 d.{pct_col}, d.{count_col}
        ORDER BY d.{pct_col} DESC NULLS LAST
    """)

    return {
        "disease_key": disease_key,
        "total_districts": len(rows),
        "data": rows,
    }


# =========================================================================== #
# PM2.5 endpoints (proxy to ArcGIS)
# =========================================================================== #


@router.get("/pm25/current")
async def pm25_current():
    """Current PM2.5 readings from Bangkok stations.
    ดึงค่า PM2.5 ปัจจุบันจากสถานีตรวจวัดใน กทม."""
    try:
        from external.arcgis_client import ArcGISClient
        client = ArcGISClient()
        data = await client.get_pm25()
        return data
    except Exception as e:
        return {
            "data_available": False,
            "message": f"PM2.5 data temporarily unavailable: {str(e)}",
            "fallback": "Use https://bmagis.bangkok.go.th/arcgis/rest/services/Hosted/air_quality_data_processed/FeatureServer/0/query?where=1%3D1&outFields=*&f=json directly",
        }


@router.get("/boundaries/districts")
async def district_boundaries():
    """Bangkok district boundary polygons (GeoJSON).
    ดึงขอบเขตเขต กทม. ในรูปแบบ GeoJSON สำหรับแสดงบนแผนที่"""
    try:
        from external.arcgis_client import ArcGISClient
        client = ArcGISClient()
        data = await client.get_district_boundaries()
        return data
    except Exception as e:
        return {
            "data_available": False,
            "message": f"Boundary data temporarily unavailable: {str(e)}",
        }


# =========================================================================== #
# Disease x Environment overlay (Doc 01 — Governor's core requirement)
# =========================================================================== #


@router.get("/overlay/disease-environment")
async def disease_environment_overlay(
    disease_key: str = Query("diabetes", description="Disease to overlay"),
):
    """Disease prevalence + PM2.5 + pollution sources combined per district.
    ซ้อนทับข้อมูลโรค + PM2.5 + แหล่งมลพิษ ต่อเขต (ตอบคำถามผู้ว่า กทม.)"""

    valid_keys = {
        "diabetes": "pct_risk_dm",
        "hypertension": "pct_risk_hpt",
        "cardiovascular": "pct_risk_cvd",
    }

    if disease_key not in valid_keys:
        raise HTTPException(status_code=400, detail=f"Valid disease_keys: {sorted(valid_keys)}")

    pct_col = valid_keys[disease_key]

    # Get disease data per district
    disease_rows = execute_query(f"""
        SELECT d.district_code, d.district_name, d.total_screened,
               d.{pct_col} AS disease_pct,
               AVG(f.latitude) AS centroid_lat,
               AVG(f.longitude) AS centroid_lng
        FROM summary_district_disease d
        LEFT JOIN ref_facilities f ON f.district_code = d.district_code
            AND f.latitude IS NOT NULL
        WHERE d.total_screened >= 5
        GROUP BY d.district_code, d.district_name, d.total_screened, d.{pct_col}
        ORDER BY d.{pct_col} DESC NULLS LAST
    """)

    # Try to get PM2.5 data
    pm25_data = None
    try:
        from external.arcgis_client import ArcGISClient
        client = ArcGISClient()
        pm25_data = await client.get_pm25()
    except Exception:
        pass

    return {
        "disease_key": disease_key,
        "disease_data": disease_rows,
        "pm25_available": pm25_data is not None,
        "pm25_data": pm25_data.get("data", []) if pm25_data else [],
        "note": "Overlay disease prevalence per district with PM2.5 station readings on the map",
    }


# =========================================================================== #
# PM2.5 aggregated endpoints (zones / districts / monthly)
# =========================================================================== #
# ArcGIS returns one reading per Bangkok district (~50 records) with field
# `district` containing "เขตXXX".  We match to ref_districts.name_th,
# aggregate by zone, and compute AQI from raw PM2.5.


async def _get_pm25_cached() -> dict:
    """Fetch PM2.5 from ArcGIS with T1 (5 min) cache."""
    hit = cache_get("pm25:current_readings")
    if hit is not None:
        return hit
    try:
        from external.arcgis_client import ArcGISClient
        client = ArcGISClient()
        data = await client.get_pm25()
        if data.get("data_available"):
            cache_set("pm25:current_readings", data, TTL_T1_EXTERNAL)
        return data
    except Exception as e:
        logger.warning("ArcGIS PM2.5 fetch failed: %s", e)
        return {"data_available": False, "data": []}


def _build_district_readings(pm25_data: dict) -> dict[str, dict]:
    """Map ArcGIS readings to district name_th.

    Returns {name_th: {pm25, aqi}} keyed by Thai district name (no เขต prefix).
    """
    readings: dict[str, dict] = {}
    for s in pm25_data.get("data", []):
        name = extract_district_name(s.get("station_name"))
        if not name:
            continue
        pm25_val = s.get("pm25_value")
        aqi_val = pm25_to_aqi(pm25_val) if pm25_val is not None else None
        readings[name] = {"pm25": pm25_val, "aqi": aqi_val}
    return readings


def _get_zone_meta() -> list[dict]:
    """Zone metadata from ref_health_zones (cached T4)."""
    hit = cache_get("pm25:zone_meta")
    if hit is not None:
        return hit
    rows = execute_query("""
        SELECT z.zone_code, z.name_th, z.name_en,
               COUNT(d.dcode) AS district_count
        FROM ref_health_zones z
        LEFT JOIN ref_districts d ON d.zone_code = z.zone_code
        GROUP BY z.zone_code, z.name_th, z.name_en
        ORDER BY z.zone_code
    """)
    cache_set("pm25:zone_meta", rows, TTL_T4_STATIC)
    return rows


def _get_district_list() -> list[dict]:
    """All 50 districts (cached T4)."""
    hit = cache_get("pm25:district_list")
    if hit is not None:
        return hit
    rows = execute_query("""
        SELECT dcode, zone_code, name_th, name_en, population
        FROM ref_districts
        ORDER BY dcode
    """)
    cache_set("pm25:district_list", rows, TTL_T4_STATIC)
    return rows


def _get_historical_stats(dcodes: list[str]) -> dict | None:
    """Query pm25_daily for historical aggregates. Returns None if no data."""
    try:
        count = execute_scalar(
            "SELECT COUNT(*) FROM pm25_daily WHERE dcode = ANY(%s)",
            (dcodes,),
        )
        if not count:
            return None
        row = execute_query("""
            SELECT
                COUNT(DISTINCT reading_date) FILTER (
                    WHERE avg_pm25 > %s
                ) AS days_exceeded,
                AVG(avg_pm25) FILTER (
                    WHERE reading_date >= '2025-01-01' AND reading_date < '2026-01-01'
                ) AS avg_2025,
                AVG(avg_pm25) FILTER (
                    WHERE reading_date >= '2026-01-01' AND reading_date < '2026-04-01'
                ) AS avg_2026_q1
            FROM pm25_daily
            WHERE dcode = ANY(%s)
        """, (STANDARD_TH, dcodes))
        if row:
            return row[0]
        return None
    except Exception:
        return None


def _compute_trend(avg_2025: float | None, avg_2026_q1: float | None) -> str | None:
    """Simple trend: compare 2025 annual avg to 2026 Q1."""
    if avg_2025 is None or avg_2026_q1 is None:
        return None
    if avg_2026_q1 > avg_2025 * 1.05:
        return "increasing"
    if avg_2026_q1 < avg_2025 * 0.95:
        return "decreasing"
    return "stable"


@router.get("/pm25/zones")
async def pm25_zones():
    """PM2.5 averages aggregated per health zone (8 zones).
    ค่าเฉลี่ย PM2.5 ต่อโซนสุขภาพ (8 โซน)"""

    hit = cache_get("pm25:zones")
    if hit is not None:
        return hit

    pm25_data = await _get_pm25_cached()
    readings = _build_district_readings(pm25_data)
    zones_meta = _get_zone_meta()
    districts = _get_district_list()

    # Group districts by zone, collect PM2.5 values
    zone_data: dict[str, list[dict]] = {}
    for d in districts:
        zone_data.setdefault(d["zone_code"], []).append(d)

    result = []
    for z in zones_meta:
        zc = z["zone_code"]
        zone_districts = zone_data.get(zc, [])
        zone_dcodes = [d["dcode"] for d in zone_districts]

        pm25_vals = []
        aqi_vals = []
        for d in zone_districts:
            r = readings.get(d["name_th"])
            if r and r["pm25"] is not None:
                pm25_vals.append(r["pm25"])
            if r and r["aqi"] is not None:
                aqi_vals.append(r["aqi"])

        avg_pm25 = round(sum(pm25_vals) / len(pm25_vals), 1) if pm25_vals else None
        avg_aqi = round(sum(aqi_vals) / len(aqi_vals)) if aqi_vals else None
        max_pm25 = round(max(pm25_vals), 1) if pm25_vals else None

        hist = _get_historical_stats(zone_dcodes)

        row = {
            "zone_code": zc,
            "zone_name_th": z["name_th"],
            "zone_name_en": z["name_en"],
            "district_count": z["district_count"],
            "avg_pm25": avg_pm25,
            "avg_aqi": avg_aqi,
            "max_pm25": max_pm25,
            "station_count": len(pm25_vals),
            "days_exceeded": hist["days_exceeded"] if hist else None,
            "trend": _compute_trend(
                hist.get("avg_2025") if hist else None,
                hist.get("avg_2026_q1") if hist else None,
            ),
            "standard_th_exceeded": avg_pm25 > STANDARD_TH if avg_pm25 is not None else None,
            "standard_who_exceeded": avg_pm25 > STANDARD_WHO if avg_pm25 is not None else None,
        }
        result.append(row)

    resp = {
        "data_available": pm25_data.get("data_available", False),
        "total_zones": len(result),
        "standards": {"th": STANDARD_TH, "who": STANDARD_WHO},
        "data": result,
    }
    cache_set("pm25:zones", resp, TTL_T2_AGGREGATE)
    return resp


@router.get("/pm25/districts")
async def pm25_districts():
    """PM2.5 values per district (50 districts) from ArcGIS per-district readings.
    ค่า PM2.5 ต่อเขต (50 เขต)"""

    hit = cache_get("pm25:districts")
    if hit is not None:
        return hit

    pm25_data = await _get_pm25_cached()
    readings = _build_district_readings(pm25_data)
    districts = _get_district_list()

    result = []
    for d in districts:
        r = readings.get(d["name_th"], {})
        hist = _get_historical_stats([d["dcode"]])

        row = {
            "dcode": d["dcode"],
            "district_name": d["name_th"],
            "district_name_en": d["name_en"],
            "zone_code": d["zone_code"],
            "avg_pm25": r.get("pm25"),
            "avg_aqi": r.get("aqi"),
            "nearest_station": d["name_th"],
            "station_name_th": d["name_th"],
            "days_exceeded": hist["days_exceeded"] if hist else None,
            "avg_2025": round(hist["avg_2025"], 1) if hist and hist.get("avg_2025") else None,
            "avg_2026_q1": round(hist["avg_2026_q1"], 1) if hist and hist.get("avg_2026_q1") else None,
            "trend": _compute_trend(
                hist.get("avg_2025") if hist else None,
                hist.get("avg_2026_q1") if hist else None,
            ),
        }
        result.append(row)

    resp = {
        "data_available": pm25_data.get("data_available", False),
        "total_districts": len(result),
        "standards": {"th": STANDARD_TH, "who": STANDARD_WHO},
        "data": result,
    }
    cache_set("pm25:districts", resp, TTL_T2_AGGREGATE)
    return resp


@router.get("/pm25/monthly")
async def pm25_monthly(
    zone_code: Optional[str] = Query(None, description="Filter by zone (1-8)"),
):
    """Monthly PM2.5 trend data for charts.
    ข้อมูล PM2.5 รายเดือน สำหรับแสดงกราฟ"""

    # Validate zone_code if provided
    if zone_code is not None:
        valid = execute_scalar(
            "SELECT COUNT(*) FROM ref_health_zones WHERE zone_code = %s",
            (zone_code,),
        )
        if not valid:
            raise HTTPException(status_code=400, detail=f"Invalid zone_code: {zone_code}")

    cache_key = f"pm25:monthly:{zone_code or 'all'}"
    hit = cache_get(cache_key)
    if hit is not None:
        return hit

    # Determine which dcodes to query
    all_districts = _get_district_list()
    if zone_code:
        dcodes = [d["dcode"] for d in all_districts if d["zone_code"] == zone_code]
    else:
        dcodes = [d["dcode"] for d in all_districts]

    # Query monthly aggregates from pm25_daily
    period = []
    historical_available = False
    try:
        count = execute_scalar(
            "SELECT COUNT(*) FROM pm25_daily WHERE dcode = ANY(%s)",
            (dcodes,),
        )
        if count and count > 0:
            historical_available = True
            period = execute_query("""
                SELECT
                    EXTRACT(YEAR FROM reading_date)::INTEGER AS year,
                    LPAD(EXTRACT(MONTH FROM reading_date)::TEXT, 2, '0') AS month,
                    ROUND(AVG(avg_pm25)::NUMERIC, 1) AS avg_pm25,
                    ROUND(AVG(avg_aqi)::NUMERIC) AS avg_aqi
                FROM pm25_daily
                WHERE dcode = ANY(%s)
                GROUP BY year, month
                ORDER BY year, month
            """, (dcodes,))
            for row in period:
                row["year"] = str(row["year"])
    except Exception:
        pass

    # Current snapshot from ArcGIS
    pm25_data = await _get_pm25_cached()
    readings = _build_district_readings(pm25_data)

    target_names = {d["name_th"] for d in all_districts if d["dcode"] in set(dcodes)}
    current_vals = [readings[n]["pm25"] for n in target_names
                    if n in readings and readings[n]["pm25"] is not None]
    current_aqi = [readings[n]["aqi"] for n in target_names
                   if n in readings and readings[n]["aqi"] is not None]

    resp = {
        "zone_code": zone_code,
        "period": period,
        "current_snapshot": {
            "avg_pm25": round(sum(current_vals) / len(current_vals), 1) if current_vals else None,
            "avg_aqi": round(sum(current_aqi) / len(current_aqi)) if current_aqi else None,
            "station_count": len(current_vals),
        },
        "standards": {"th": STANDARD_TH, "who": STANDARD_WHO},
        "historical_data_available": historical_available,
    }
    cache_set(cache_key, resp, TTL_T2_AGGREGATE)
    return resp
