"""Zones router — list, detail, dashboard."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from database import execute_query
from security import K_ANONYMITY_THRESHOLD

router = APIRouter(tags=["Zones"])


# =========================================================================== #
# Zone list
# =========================================================================== #

@router.get("/api/v2/summary/zones")
def list_zones():
    """All zones with screening totals and disease breakdown.

    Aggregation base = HOME district from raw_homevisit (where the patient
    lives), NOT screening district. See fact/aggregation-base.md for why.
    Vitals/disease flags come from raw_vitalsigns via JOIN by patient_id.
    """
    rows = execute_query("""
        SELECT
          z.zone_code, z.name_th, z.name_en,
          COUNT(DISTINCT d.dcode) AS district_count,
          COUNT(DISTINCT hv.patient_id) AS total_screened,
          COUNT(hv.id) AS total_visits,
          COUNT(DISTINCT hv.patient_id) FILTER (WHERE v.risk_dm)            AS diabetes,
          COUNT(DISTINCT hv.patient_id) FILTER (WHERE v.risk_hpt)           AS hypertension,
          COUNT(DISTINCT hv.patient_id) FILTER (WHERE v.risk_cvd)           AS cardiovascular,
          COUNT(DISTINCT hv.patient_id) FILTER (WHERE v.risk_bmi)           AS obesity,
          COUNT(DISTINCT hv.patient_id) FILTER (WHERE v.found_dyslipidemia) AS dyslipidemia,
          COUNT(DISTINCT hv.patient_id) FILTER (WHERE v.found_stroke)       AS stroke
        FROM ref_health_zones z
        LEFT JOIN ref_districts d ON d.zone_code = z.zone_code
        LEFT JOIN raw_homevisit hv ON hv.home_district::text = d.dcode
          AND hv.cancel_status IS DISTINCT FROM 1
        LEFT JOIN raw_vitalsigns v ON v.patient_id = hv.patient_id
          AND v.cancel_status IS DISTINCT FROM 1
        GROUP BY z.zone_code, z.name_th, z.name_en
        ORDER BY z.zone_code
    """)
    result = []
    for r in rows:
        ts = r["total_screened"] or 1
        diseases = {}
        for dk in ("diabetes", "hypertension", "cardiovascular", "obesity", "dyslipidemia", "stroke"):
            cnt = r.pop(dk, 0) or 0
            diseases[dk] = {"count": cnt, "pct": round(100.0 * cnt / ts, 2)}
        r["diseases"] = diseases
        result.append(r)
    return result


# =========================================================================== #
# Zone detail
# =========================================================================== #

@router.get("/api/v2/summary/zones/{zone_code}")
def zone_detail(zone_code: str):
    """Single zone with its districts and disease data."""
    zone = execute_query(
        "SELECT zone_code, name_th, name_en FROM ref_health_zones WHERE zone_code = %s",
        (zone_code,),
    )
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")

    districts = execute_query("""
        SELECT
          s.district_code, s.district_name, s.total_screened,
          s.risk_dm_count, s.pct_risk_dm,
          s.risk_hpt_count, s.pct_risk_hpt,
          s.risk_cvd_count, s.pct_risk_cvd,
          s.risk_bmi_count,
          s.found_obesity_count,
          s.found_dyslipidemia_count,
          s.found_stroke_count
        FROM summary_district_disease s
        WHERE s.zone_code = %s
        ORDER BY s.district_code
    """, (zone_code,))

    districts = [d for d in districts if (d.get("total_screened") or 0) >= K_ANONYMITY_THRESHOLD]
    return {**zone[0], "districts": districts}


# =========================================================================== #
# Zone dashboard
# =========================================================================== #

@router.get("/api/v2/zone/{zone_code}/dashboard")
def zone_dashboard(zone_code: str):
    """Zone dashboard with facility breakdown."""
    # Zone info
    zone = execute_query(
        "SELECT zone_code, name_th, name_en, facilitator FROM ref_health_zones WHERE zone_code = %s",
        (zone_code,),
    )
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")

    # Districts in zone
    districts = execute_query("""
        SELECT s.district_code, s.district_name, s.total_screened,
               s.risk_dm_count, s.risk_hpt_count, s.found_obesity_count
        FROM summary_district_disease s
        WHERE s.zone_code = %s AND s.total_screened >= 5
        ORDER BY s.total_screened DESC
    """, (zone_code,))

    # Facilities in zone
    facilities = execute_query("""
        SELECT s.facility_code, s.district_code, s.total_screened,
               s.risk_dm, s.risk_hpt, s.found_obesity, s.lab_completed,
               s.first_screening, s.last_screening
        FROM summary_facility s
        JOIN ref_districts d ON s.district_code = d.dcode
        WHERE d.zone_code = %s AND s.total_screened >= 5
        ORDER BY s.total_screened DESC
    """, (zone_code,))

    for f in facilities:
        for k in ("first_screening", "last_screening"):
            if f.get(k) and hasattr(f[k], "isoformat"):
                f[k] = f[k].isoformat()

    # Zone totals
    zone_total = sum(d.get("total_screened", 0) or 0 for d in districts)

    return {
        **zone[0],
        "total_screened": zone_total,
        "district_count": len(districts),
        "facility_count": len(facilities),
        "districts": districts,
        "facilities": facilities,
    }
