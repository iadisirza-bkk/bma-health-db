"""Summary router — overview, filtered, lab, mental-health, demographics."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Path, Query

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

    # Core aggregation: screenings + physical vitals for non-Bangkok residents
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
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.smoking IS NOT NULL) AS smoking_answered,
          -- physical vitals (used by lab-factor display when a disease is active)
          AVG(v.sbp)        AS avg_sbp,
          AVG(v.dbp)        AS avg_dbp,
          AVG(v.weight_kg)  AS avg_weight_kg,
          AVG(v.waist_cm)   AS avg_waist_cm,
          AVG(CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) AS avg_bmi,
          MAX(v.visit_date) AS last_visit
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

    # Lab aggregates (avg lab values) — same shape as /summary/lab per-district
    lab_rows = execute_query("""
        SELECT
          COUNT(DISTINCT l.patient_id) AS total_lab_patients,
          AVG(l.hemoglobin)   AS avg_hemoglobin,
          AVG(l.hematocrit)   AS avg_hematocrit,
          AVG(l.fbs)          AS avg_fbs,
          AVG(l.cholesterol)  AS avg_cholesterol,
          AVG(l.triglyceride) AS avg_triglyceride,
          AVG(l.hdl)          AS avg_hdl,
          AVG(l.ldl)          AS avg_ldl,
          AVG(l.creatinine)   AS avg_creatinine,
          AVG(l.egfr)         AS avg_egfr,
          AVG(l.uric_acid)    AS avg_uric_acid,
          AVG(l.sgot)         AS avg_sgot,
          AVG(l.sgpt)         AS avg_sgpt,
          ROUND(100.0 * COUNT(*) FILTER (WHERE l.hemoglobin < 12)
                      / NULLIF(COUNT(*) FILTER (WHERE l.hemoglobin IS NOT NULL), 0), 2) AS pct_anemia,
          ROUND(100.0 * COUNT(*) FILTER (WHERE l.egfr < 60)
                      / NULLIF(COUNT(*) FILTER (WHERE l.egfr IS NOT NULL), 0), 2) AS pct_ckd
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        JOIN raw_lab_results l ON l.patient_id = v.patient_id
          AND l.cancel_status IS DISTINCT FROM 1
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province IS NOT NULL
          AND hv.home_province <> 10
    """) or []
    lab_row = lab_rows[0] if lab_rows else {}

    # Exercise / lifestyle (no-exercise rate from raw_homehealth.exercise == 0)
    hh_rows = execute_query("""
        SELECT
          COUNT(DISTINCT v.patient_id) FILTER (WHERE h.exercise = 0) AS no_exercise_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE h.exercise IS NOT NULL) AS exercise_answered
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        LEFT JOIN raw_homehealth h ON h.patient_id = v.patient_id
          AND h.cancel_status IS DISTINCT FROM 1
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province IS NOT NULL
          AND hv.home_province <> 10
    """) or []
    hh_row = hh_rows[0] if hh_rows else {}

    # Mental health percentages (same computation as summary_district_mental)
    mental_rows = execute_query("""
        SELECT
          ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (
            WHERE v.depression_2q_1 >= 1 OR v.depression_2q_2 >= 1
          ) / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_depression_risk,
          ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (
            WHERE (COALESCE(v.phq9_q1,0) + COALESCE(v.phq9_q2,0) + COALESCE(v.phq9_q3,0)
                 + COALESCE(v.phq9_q4,0) + COALESCE(v.phq9_q5,0) + COALESCE(v.phq9_q6,0)
                 + COALESCE(v.phq9_q7,0) + COALESCE(v.phq9_q8,0) + COALESCE(v.phq9_q9,0)) >= 10
          ) / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_phq9_moderate,
          ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (
            WHERE (COALESCE(v.st5_q1,0) + COALESCE(v.st5_q2,0) + COALESCE(v.st5_q3,0)
                 + COALESCE(v.st5_q4,0) + COALESCE(v.st5_q5,0)) >= 7
          ) / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_high_stress
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province IS NOT NULL
          AND hv.home_province <> 10
    """) or []
    mental_row = mental_rows[0] if mental_rows else {}

    # Rates
    smoking_count = int(row.get("smoking_count") or 0)
    smoking_answered = int(row.get("smoking_answered") or 0)
    smoking_rate = round(100.0 * smoking_count / smoking_answered, 2) if smoking_answered else 0

    no_ex = int(hh_row.get("no_exercise_count") or 0)
    ex_answered = int(hh_row.get("exercise_answered") or 0)
    no_exercise_rate = round(100.0 * no_ex / ex_answered, 2) if ex_answered else 0

    last_visit = row.get("last_visit")

    # Helper: safely convert numeric or None to float
    def _f(v):
        return float(v) if v is not None else None

    result = {
        "total_screened": total,
        "smoking_rate": smoking_rate,
        "no_exercise_rate": no_exercise_rate,
        "last_updated": str(last_visit) if last_visit else None,
        "by_disease": by_disease,
        "by_home_province": by_home_province,
        # Disease counts (raw) — used to build ZoneHealthData.diseases shape
        "disease_counts": {
            "diabetes":       int(row.get("risk_dm_count") or 0),
            "hypertension":   int(row.get("risk_hpt_count") or 0),
            "cardiovascular": int(row.get("risk_cvd_count") or 0),
            "obesity":        int(row.get("risk_bmi_count") or 0),
            "dyslipidemia":   int(row.get("found_dyslipidemia_count") or 0),
            "stroke":         int(row.get("found_stroke_count") or 0),
        },
        # Physical vitals (averages)
        "physical": {
            "avg_sbp":       _f(row.get("avg_sbp")),
            "avg_dbp":       _f(row.get("avg_dbp")),
            "avg_weight_kg": _f(row.get("avg_weight_kg")),
            "avg_waist_cm":  _f(row.get("avg_waist_cm")),
            "avg_bmi":       _f(row.get("avg_bmi")),
        },
        # Lab averages (same shape as /summary/lab row)
        "lab": {
            "total_lab_patients": int(lab_row.get("total_lab_patients") or 0),
            "avg_hemoglobin":   _f(lab_row.get("avg_hemoglobin")),
            "avg_hematocrit":   _f(lab_row.get("avg_hematocrit")),
            "avg_fbs":          _f(lab_row.get("avg_fbs")),
            "avg_cholesterol":  _f(lab_row.get("avg_cholesterol")),
            "avg_triglyceride": _f(lab_row.get("avg_triglyceride")),
            "avg_hdl":          _f(lab_row.get("avg_hdl")),
            "avg_ldl":          _f(lab_row.get("avg_ldl")),
            "avg_creatinine":   _f(lab_row.get("avg_creatinine")),
            "avg_egfr":         _f(lab_row.get("avg_egfr")),
            "avg_uric_acid":    _f(lab_row.get("avg_uric_acid")),
            "avg_sgot":         _f(lab_row.get("avg_sgot")),
            "avg_sgpt":         _f(lab_row.get("avg_sgpt")),
            "pct_anemia":       _f(lab_row.get("pct_anemia")),
            "pct_ckd":          _f(lab_row.get("pct_ckd")),
        },
        # Mental health (%s already computed)
        "mental": {
            "pct_depression_risk": _f(mental_row.get("pct_depression_risk")),
            "pct_phq9_moderate":   _f(mental_row.get("pct_phq9_moderate")),
            "pct_high_stress":     _f(mental_row.get("pct_high_stress")),
        },
    }
    cache_set("summary:non_bangkok_overview", result, TTL_T2_AGGREGATE)
    return result


# =========================================================================== #
# Non-Bangkok per-province detail
# =========================================================================== #

@router.get("/non-bangkok-province/{province_code}")
def non_bangkok_province(
    province_code: int = Path(..., ge=11, le=96, description="Thai province code (TIS 1099)"),
):
    """Full health stats for patients whose home_province equals the given code.

    Same response shape as /non-bangkok-overview but filtered to one province.
    Returns 404 if the province has no qualifying records.
    k-anonymity: suppressed flag set when total < threshold.
    """
    if province_code == 10:
        raise HTTPException(status_code=400, detail="use /summary/overview for Bangkok")

    cache_key = f"summary:non_bangkok_province:{province_code}"
    hit = cache_get(cache_key)
    if hit is not None:
        return hit

    rows = execute_query("""
        SELECT
          COUNT(DISTINCT v.patient_id)                                     AS total_screened,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm)            AS risk_dm_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt)           AS risk_hpt_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_cvd)           AS risk_cvd_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_bmi)           AS risk_bmi_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_stroke)       AS found_stroke_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dyslipidemia) AS found_dyslipidemia_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.smoking = 1)        AS smoking_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.smoking IS NOT NULL) AS smoking_answered,
          AVG(v.sbp)        AS avg_sbp,
          AVG(v.dbp)        AS avg_dbp,
          AVG(v.weight_kg)  AS avg_weight_kg,
          AVG(v.waist_cm)   AS avg_waist_cm,
          AVG(CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) AS avg_bmi,
          MAX(v.visit_date) AS last_visit
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province = %s
    """, (province_code,))
    row = rows[0] if rows else {}
    total = int(row.get("total_screened") or 0)

    if total < K_ANONYMITY_THRESHOLD:
        result = {
            "province_code": province_code,
            "total_screened": 0,
            "suppressed": True,
            "reason": f"k-anonymity: n < {K_ANONYMITY_THRESHOLD}",
            "by_disease": [],
        }
        cache_set(cache_key, result, TTL_T2_AGGREGATE)
        return result

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

    # Lab aggregates
    lab_rows = execute_query("""
        SELECT
          COUNT(DISTINCT l.patient_id) AS total_lab_patients,
          AVG(l.fbs)          AS avg_fbs,
          AVG(l.cholesterol)  AS avg_cholesterol,
          AVG(l.triglyceride) AS avg_triglyceride,
          AVG(l.hdl)          AS avg_hdl,
          AVG(l.ldl)          AS avg_ldl,
          AVG(l.creatinine)   AS avg_creatinine,
          AVG(l.egfr)         AS avg_egfr,
          AVG(l.hemoglobin)   AS avg_hemoglobin
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        JOIN raw_lab_results l ON l.patient_id = v.patient_id
          AND l.cancel_status IS DISTINCT FROM 1
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province = %s
    """, (province_code,)) or []
    lab_row = lab_rows[0] if lab_rows else {}

    # No-exercise rate
    hh_rows = execute_query("""
        SELECT
          COUNT(DISTINCT v.patient_id) FILTER (WHERE h.exercise = 0) AS no_exercise_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE h.exercise IS NOT NULL) AS exercise_answered
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        LEFT JOIN raw_homehealth h ON h.patient_id = v.patient_id
          AND h.cancel_status IS DISTINCT FROM 1
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province = %s
    """, (province_code,)) or []
    hh_row = hh_rows[0] if hh_rows else {}

    def _f(v):
        return float(v) if v is not None else None

    smoking_count = int(row.get("smoking_count") or 0)
    smoking_answered = int(row.get("smoking_answered") or 0)
    smoking_rate = round(100.0 * smoking_count / smoking_answered, 2) if smoking_answered else 0

    no_ex = int(hh_row.get("no_exercise_count") or 0)
    ex_answered = int(hh_row.get("exercise_answered") or 0)
    no_exercise_rate = round(100.0 * no_ex / ex_answered, 2) if ex_answered else 0

    result = {
        "province_code": province_code,
        "total_screened": total,
        "smoking_rate": smoking_rate,
        "no_exercise_rate": no_exercise_rate,
        "last_updated": str(row.get("last_visit")) if row.get("last_visit") else None,
        "by_disease": by_disease,
        "physical": {
            "avg_sbp":       _f(row.get("avg_sbp")),
            "avg_dbp":       _f(row.get("avg_dbp")),
            "avg_weight_kg": _f(row.get("avg_weight_kg")),
            "avg_waist_cm":  _f(row.get("avg_waist_cm")),
            "avg_bmi":       _f(row.get("avg_bmi")),
        },
        "lab": {
            "total_lab_patients": int(lab_row.get("total_lab_patients") or 0),
            "avg_fbs":          _f(lab_row.get("avg_fbs")),
            "avg_cholesterol":  _f(lab_row.get("avg_cholesterol")),
            "avg_triglyceride": _f(lab_row.get("avg_triglyceride")),
            "avg_hdl":          _f(lab_row.get("avg_hdl")),
            "avg_ldl":          _f(lab_row.get("avg_ldl")),
            "avg_creatinine":   _f(lab_row.get("avg_creatinine")),
            "avg_egfr":         _f(lab_row.get("avg_egfr")),
            "avg_hemoglobin":   _f(lab_row.get("avg_hemoglobin")),
        },
    }
    cache_set(cache_key, result, TTL_T2_AGGREGATE)
    return result
