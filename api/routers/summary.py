"""Summary router — overview, filtered, lab, mental-health, demographics."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from database import execute_query, execute_scalar
from security import enforce_k_anonymity, K_ANONYMITY_THRESHOLD
from cache import cache_get, cache_set, TTL_T2_AGGREGATE, TTL_T3_FILTERED, TTL_T4_STATIC

router = APIRouter(prefix="/api/v2/summary", tags=["Summary"])

TARGET_SCREENED = 1_600_000


# =========================================================================== #
# Overview
# =========================================================================== #

@router.get("/overview")
def overview():
    """Top-level screening overview with zone and disease breakdowns."""

    # Cache check (TTL 15 min)
    hit = cache_get("summary:overview")
    if hit is not None:
        return hit

    total = execute_scalar(
        "SELECT COALESCE(SUM(total_screened), 0) FROM summary_district_disease"
    ) or 0

    zone_count = execute_scalar("SELECT COUNT(*) FROM ref_health_zones") or 0
    district_count = execute_scalar("SELECT COUNT(*) FROM ref_districts") or 0

    last_updated = execute_scalar(
        "SELECT MAX(refreshed_at) FROM summary_district_disease"
    )

    # By zone
    by_zone = execute_query("""
        SELECT z.zone_code, z.name_th,
               COALESCE(SUM(s.total_screened), 0) AS total_screened
        FROM ref_health_zones z
        LEFT JOIN summary_district_disease s ON s.zone_code = z.zone_code
        GROUP BY z.zone_code, z.name_th
        ORDER BY z.zone_code
    """)

    # By disease (overall)
    disease_rows = execute_query("""
        SELECT
          SUM(total_screened)              AS total_screened,
          SUM(risk_dm_count)              AS diabetes,
          SUM(risk_hpt_count)             AS hypertension,
          SUM(risk_cvd_count)             AS cardiovascular,
          SUM(risk_bmi_count)             AS obesity,
          SUM(found_dyslipidemia_count)   AS dyslipidemia,
          SUM(found_stroke_count)         AS stroke
        FROM summary_district_disease
    """)
    d = disease_rows[0] if disease_rows else {}
    ts = d.get("total_screened") or 1
    by_disease = []
    for key in ("diabetes", "hypertension", "cardiovascular", "obesity", "dyslipidemia", "stroke"):
        cnt = d.get(key) or 0
        by_disease.append({
            "disease_key": key,
            "total_at_risk": cnt,
            "pct": round(100.0 * cnt / ts, 2) if ts else 0,
        })

    result = {
        "total_screened": total,
        "target": TARGET_SCREENED,
        "zones_count": zone_count,
        "districts_count": district_count,
        "last_updated": str(last_updated) if last_updated else None,
        "by_zone": by_zone,
        "by_disease": by_disease,
    }
    cache_set("summary:overview", result, TTL_T2_AGGREGATE)
    return result


# =========================================================================== #
# Filtered query (k-anonymity enforced)
# =========================================================================== #

@router.get("/filtered")
def filtered_summary(
    district: Optional[str] = Query(None),
    sex: Optional[int] = Query(None),
    age_group: Optional[str] = Query(None),
    smoking: Optional[int] = Query(None),
    exercise: Optional[int] = Query(None),
):
    """Query risk factor summary with filters. k-anonymity enforced."""
    conditions = []
    params: list = []

    if district:
        conditions.append("district_code = %s")
        params.append(district)
    if sex is not None:
        conditions.append("sex = %s")
        params.append(sex)
    if age_group:
        conditions.append("age_group = %s")
        params.append(age_group)
    if smoking is not None:
        conditions.append("smoking = %s")
        params.append(smoking)
    if exercise is not None:
        conditions.append("exercise = %s")
        params.append(exercise)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT
          district_code, sex, age_group, smoking, exercise,
          SUM(patient_count)::int AS patient_count,
          ROUND(AVG(avg_sbp)::numeric, 1) AS avg_sbp,
          ROUND(AVG(avg_dbp)::numeric, 1) AS avg_dbp,
          ROUND(AVG(avg_weight_kg)::numeric, 1) AS avg_weight_kg,
          ROUND(AVG(avg_waist_cm)::numeric, 1) AS avg_waist_cm,
          ROUND(AVG(avg_bmi)::numeric, 1) AS avg_bmi
        FROM summary_district_risk_factors
        {where}
        GROUP BY district_code, sex, age_group, smoking, exercise
        ORDER BY district_code, sex, age_group
    """, tuple(params) or None)

    rows = enforce_k_anonymity(rows, count_field="patient_count")
    return {"filters_applied": {
        "district": district, "sex": sex, "age_group": age_group,
        "smoking": smoking, "exercise": exercise,
    }, "k_anonymity_threshold": K_ANONYMITY_THRESHOLD, "data": rows}


# =========================================================================== #
# Lab summary
# =========================================================================== #

@router.get("/lab")
def lab_summary(
    dcode: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Lab summary, optionally filtered by district or zone."""
    conditions: list[str] = []
    params: list = []

    if dcode:
        conditions.append("l.district_code = %s")
        params.append(dcode)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT
          l.district_code,
          l.total_lab_patients,
          ROUND(l.avg_hemoglobin::numeric, 2) AS avg_hemoglobin,
          ROUND(l.avg_fbs::numeric, 2) AS avg_fbs,
          ROUND(l.avg_cholesterol::numeric, 2) AS avg_cholesterol,
          ROUND(l.avg_triglyceride::numeric, 2) AS avg_triglyceride,
          ROUND(l.avg_hdl::numeric, 2) AS avg_hdl,
          ROUND(l.avg_ldl::numeric, 2) AS avg_ldl,
          ROUND(l.avg_creatinine::numeric, 2) AS avg_creatinine,
          ROUND(l.avg_egfr::numeric, 2) AS avg_egfr,
          l.pct_anemia,
          l.pct_ckd
        FROM summary_district_lab l
        JOIN ref_districts d ON l.district_code = d.dcode
        {where}
        ORDER BY l.district_code
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_lab_patients") or 0) >= K_ANONYMITY_THRESHOLD]
    return rows


# =========================================================================== #
# Mental health summary
# =========================================================================== #

@router.get("/mental-health")
def mental_health_summary(
    dcode: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Mental health screening summary."""
    conditions: list[str] = []
    params: list = []

    if dcode:
        conditions.append("m.district_code = %s")
        params.append(dcode)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT
          m.district_code,
          m.total_screened,
          m.pct_depression_risk,
          m.pct_phq9_moderate,
          m.pct_high_stress
        FROM summary_district_mental m
        JOIN ref_districts d ON m.district_code = d.dcode
        {where}
        ORDER BY m.district_code
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_screened") or 0) >= K_ANONYMITY_THRESHOLD]
    return rows


# =========================================================================== #
# Demographics summary
# =========================================================================== #

@router.get("/demographics")
def demographics_summary(
    dcode: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Demographic breakdown by district."""
    conditions: list[str] = []
    params: list = []

    if dcode:
        conditions.append("dm.district_code = %s")
        params.append(dcode)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT
          dm.district_code,
          dm.total_respondents,
          dm.edu_none, dm.edu_primary, dm.edu_secondary,
          dm.edu_high_school, dm.edu_vocational, dm.edu_bachelor, dm.edu_postgrad,
          dm.occ_government, dm.occ_private, dm.occ_self_employed,
          dm.occ_agriculture, dm.occ_unemployed, dm.occ_student, dm.occ_retired,
          dm.priv_ucs, dm.priv_sso, dm.priv_csmbs, dm.priv_other,
          dm.house_owned, dm.house_rented, dm.house_condo, dm.house_other
        FROM summary_district_demographics dm
        JOIN ref_districts d ON dm.district_code::text = d.dcode
        {where}
        ORDER BY dm.district_code
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_respondents") or 0) >= K_ANONYMITY_THRESHOLD]
    return rows


# =========================================================================== #
# Non-Bangkok overview
# Aggregates screening records where the patient's district_code is outside
# Bangkok (not in the 1001–1050 range). These are patients who self-reported
# a home district outside BKK yet came for BMA screening.
# =========================================================================== #

@router.get("/non-bangkok-overview")
def non_bangkok_overview():
    """Aggregated health stats for patients whose home province is outside Bangkok.

    The patient was screened at a BMA facility (district_code in 1001–1050) but
    self-reported a home province other than Bangkok (home_province <> 10 in
    raw_homevisit). This surfaces "outsiders" who use Bangkok health services.
    """

    # Cache check (TTL 15 min)
    hit = cache_get("summary:non_bangkok_overview")
    if hit is not None:
        return hit

    # Core aggregation: screenings for patients with non-Bangkok home province
    rows = execute_query("""
        SELECT
          COUNT(DISTINCT v.patient_id)                                     AS total_screened,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm)            AS risk_dm_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt)           AS risk_hpt_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_cvd)           AS risk_cvd_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_bmi)           AS risk_bmi_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm)           AS found_dm_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_hpt)          AS found_hpt_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_cvd)          AS found_cvd_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_obesity)      AS found_obesity_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_stroke)       AS found_stroke_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dyslipidemia) AS found_dyslipidemia_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.smoking = 1)        AS smoking_count,
          MAX(v.visit_date)                                                 AS last_visit
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province IS NOT NULL
          AND hv.home_province <> 10
    """)

    row = rows[0] if rows else {}
    total = int(row.get("total_screened") or 0)

    # k-anonymity guard: don't expose anything if too few patients
    if total < K_ANONYMITY_THRESHOLD:
        result = {
            "total_screened": 0,
            "suppressed": True,
            "reason": f"k-anonymity: n < {K_ANONYMITY_THRESHOLD}",
            "by_disease": [],
            "by_home_province": [],
            "last_updated": None,
        }
        cache_set("summary:non_bangkok_overview", result, TTL_T2_AGGREGATE)
        return result

    # Per-disease breakdown (mirrors /overview shape)
    disease_map = [
        ("diabetes",       "risk_dm_count"),
        ("hypertension",   "risk_hpt_count"),
        ("cardiovascular", "risk_cvd_count"),
        ("obesity",        "risk_bmi_count"),
        ("dyslipidemia",   "found_dyslipidemia_count"),
        ("stroke",         "found_stroke_count"),
    ]
    by_disease = []
    for key, col in disease_map:
        cnt = int(row.get(col) or 0)
        by_disease.append({
            "disease_key": key,
            "total_at_risk": cnt,
            "pct": round(100.0 * cnt / total, 2) if total else 0,
        })

    # Top home-provinces (self-reported in raw_homevisit). k-anonymity per bucket.
    by_home_province = execute_query("""
        SELECT
          hv.home_province AS province_code,
          COUNT(DISTINCT v.patient_id) AS count
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province IS NOT NULL
          AND hv.home_province <> 10
        GROUP BY hv.home_province
        HAVING COUNT(DISTINCT v.patient_id) >= %s
        ORDER BY count DESC
        LIMIT 15
    """, (K_ANONYMITY_THRESHOLD,)) or []

    # Smoking rate as a headline risk modifier
    smoking_count = int(row.get("smoking_count") or 0)
    smoking_rate = round(100.0 * smoking_count / total, 2) if total else 0

    last_visit = row.get("last_visit")

    result = {
        "total_screened": total,
        "smoking_rate": smoking_rate,
        "last_updated": str(last_visit) if last_visit else None,
        "by_disease": by_disease,
        "by_home_province": by_home_province,
    }
    cache_set("summary:non_bangkok_overview", result, TTL_T2_AGGREGATE)
    return result
