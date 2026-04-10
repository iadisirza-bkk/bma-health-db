"""
GIS router — facility locations, disease heatmaps, PM2.5 overlay, boundaries.
New endpoints for map dashboard (Docs 01-08).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from database import execute_query, execute_scalar

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
