"""Summary router — overview, filtered, lab, mental-health, demographics."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Path, Query

from database import execute_query, execute_scalar
from security import enforce_k_anonymity, K_ANONYMITY_THRESHOLD
from cache import cache_get, cache_set, TTL_T2_AGGREGATE, TTL_T3_FILTERED, TTL_T4_STATIC

router = APIRouter(prefix="/api/v2/summary", tags=["Summary"])

TARGET_SCREENED = 1_000_000


# =========================================================================== #
# Overview
# =========================================================================== #

@router.get("/overview")
def overview():
    """Top-level screening overview with zone and disease breakdowns.

    NOTE: total_screened uses COUNT DISTINCT on raw_vitalsigns — NOT SUM on
    summary_district_disease. The materialized view has a hidden data_source
    dimension (app1/app2/portal) so SUMming double-counts patients who appear
    in multiple sources. See the dashboard bug Apr 2026: the admin page
    showed 837k (raw record count) while the public map showed 807k (inflated
    SUM). Correct public number is ~782k unique patients with BKK dcode.
    """

    # Cache check (TTL 15 min)
    hit = cache_get("summary:overview")
    if hit is not None:
        return hit

    # Total unique patients screened in Bangkok (dcode 1001-1050).
    # Raw query — slower but correctly de-duplicates across data_source.
    total = execute_scalar("""
        SELECT COUNT(DISTINCT patient_id)
        FROM raw_vitalsigns
        WHERE cancel_status IS DISTINCT FROM 1
          AND district_code BETWEEN '1001' AND '1050'
    """) or 0

    zone_count = execute_scalar("SELECT COUNT(*) FROM ref_health_zones") or 0
    district_count = execute_scalar("SELECT COUNT(*) FROM ref_districts") or 0

    last_updated = execute_scalar(
        "SELECT MAX(refreshed_at) FROM summary_district_disease"
    )

    # By zone — COUNT DISTINCT per zone (not SUM from the view)
    by_zone = execute_query("""
        SELECT z.zone_code, z.name_th,
               COUNT(DISTINCT v.patient_id) AS total_screened
        FROM ref_health_zones z
        LEFT JOIN ref_districts d ON d.zone_code = z.zone_code
        LEFT JOIN raw_vitalsigns v ON v.district_code = d.dcode
          AND v.cancel_status IS DISTINCT FROM 1
        GROUP BY z.zone_code, z.name_th
        ORDER BY z.zone_code
    """)

    # By disease (overall) — COUNT DISTINCT with FILTER
    disease_rows = execute_query("""
        SELECT
          COUNT(DISTINCT patient_id)                                  AS total_screened,
          COUNT(DISTINCT patient_id) FILTER (WHERE risk_dm)           AS diabetes,
          COUNT(DISTINCT patient_id) FILTER (WHERE risk_hpt)          AS hypertension,
          COUNT(DISTINCT patient_id) FILTER (WHERE risk_cvd)          AS cardiovascular,
          COUNT(DISTINCT patient_id) FILTER (WHERE risk_bmi)          AS obesity,
          COUNT(DISTINCT patient_id) FILTER (WHERE found_dyslipidemia) AS dyslipidemia,
          COUNT(DISTINCT patient_id) FILTER (WHERE found_stroke)      AS stroke
        FROM raw_vitalsigns
        WHERE cancel_status IS DISTINCT FROM 1
          AND district_code BETWEEN '1001' AND '1050'
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
    age_group: Optional[str] = Query(None, description="Legacy Thai age-group string (e.g. 'สูงวัย')"),
    age_min: Optional[int] = Query(None, ge=0, le=120, description="Minimum age (inclusive)"),
    age_max: Optional[int] = Query(None, ge=0, le=120, description="Maximum age (inclusive)"),
    fiscal_year: Optional[int] = Query(None, ge=2550, le=2700,
        description="Thai fiscal year (BE). FY 2569 = Oct 2025–Sep 2026."),
    date_from: Optional[str] = Query(None, description="Custom range start (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="Custom range end (YYYY-MM-DD)"),
    smoking: Optional[int] = Query(None),
    exercise: Optional[int] = Query(None),
):
    """Query risk factor summary with filters. k-anonymity enforced.

    Filter paths:
    - `age_group=<legacy-string>` alone → fast materialized-view path.
    - `age_min/age_max`, `fiscal_year`, or `date_from/date_to` → raw-tables
      path that computes age from raw_patients.birth_year and/or filters by
      raw_vitalsigns.visit_date. Slower but flexible — per
      fact/age-groups.md and fact/fiscal-years.md.
    """
    # Resolve fiscal_year → date range. Thai FY X = Oct 1 (X-544) → Sep 30 (X-543).
    fy_date_from: Optional[str] = None
    fy_date_to: Optional[str] = None
    if fiscal_year is not None:
        fy_start_year = fiscal_year - 544  # e.g. FY 2569 → 2025
        fy_end_year = fiscal_year - 543    # e.g. FY 2569 → 2026
        fy_date_from = f"{fy_start_year}-10-01"
        fy_date_to = f"{fy_end_year}-09-30"

    needs_raw = (
        age_min is not None or age_max is not None
        or fiscal_year is not None
        or date_from is not None or date_to is not None
    )

    # ─── Raw-tables path: supports age range + time range filters ───────────
    if needs_raw:
        lo = age_min if age_min is not None else 0
        hi = age_max if age_max is not None else 120
        if lo > hi:
            lo, hi = hi, lo
        conditions = [
            "v.cancel_status IS DISTINCT FROM 1",
        ]
        params: list = []
        # Age filter (optional — only when age_min/age_max provided)
        if age_min is not None or age_max is not None:
            conditions.append("p.birth_year IS NOT NULL")
            conditions.append(
                "(EXTRACT(YEAR FROM CURRENT_DATE)::int - p.birth_year) BETWEEN %s AND %s"
            )
            params.extend([lo, hi])
        # Time filter — explicit date_from/date_to take precedence over fiscal_year
        effective_from = date_from or fy_date_from
        effective_to = date_to or fy_date_to
        if effective_from:
            conditions.append("v.visit_date >= %s")
            params.append(effective_from)
        if effective_to:
            conditions.append("v.visit_date <= %s")
            params.append(effective_to)
        if district:
            conditions.append("v.district_code = %s")
            params.append(district)
        if sex is not None:
            conditions.append("p.sex = %s")
            params.append(sex)
        if smoking is not None:
            conditions.append("v.smoking = %s")
            params.append(smoking)
        # Note: exercise lives in raw_homehealth; skipped in this path for perf.
        where = "WHERE " + " AND ".join(conditions)

        rows = execute_query(f"""
            SELECT
              v.district_code,
              p.sex,
              NULL::text AS age_group,
              v.smoking,
              NULL::int  AS exercise,
              COUNT(DISTINCT v.patient_id)::int AS patient_count,
              -- Disease counts for map choropleth + zone aggregation
              COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm)              AS risk_dm_count,
              COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt)             AS risk_hpt_count,
              COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_cvd)             AS risk_cvd_count,
              COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_bmi)             AS risk_bmi_count,
              COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_obesity)        AS found_obesity_count,
              COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dyslipidemia)   AS found_dyslipidemia_count,
              COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_stroke)         AS found_stroke_count,
              -- Vitals averages
              ROUND(AVG(v.sbp)::numeric, 1)          AS avg_sbp,
              ROUND(AVG(v.dbp)::numeric, 1)          AS avg_dbp,
              ROUND(AVG(v.weight_kg)::numeric, 1)    AS avg_weight_kg,
              ROUND(AVG(v.waist_cm)::numeric, 1)     AS avg_waist_cm,
              ROUND(AVG(CASE WHEN v.height_cm > 0
                             THEN v.weight_kg / POWER(v.height_cm / 100.0, 2)
                        END)::numeric, 1)            AS avg_bmi
            FROM raw_vitalsigns v
            JOIN raw_patients p ON p.id = v.patient_id
            {where}
            GROUP BY v.district_code, p.sex, v.smoking
            ORDER BY v.district_code, p.sex
        """, tuple(params))

        rows = enforce_k_anonymity(rows, count_field="patient_count")
        return {"filters_applied": {
            "district": district, "sex": sex,
            "age_min": lo if (age_min is not None or age_max is not None) else None,
            "age_max": hi if (age_min is not None or age_max is not None) else None,
            "fiscal_year": fiscal_year,
            "date_from": effective_from,
            "date_to": effective_to,
            "smoking": smoking, "exercise": exercise,
        }, "k_anonymity_threshold": K_ANONYMITY_THRESHOLD, "data": rows}

    # ─── Path 1: legacy categorical filter → uses materialized view ─────────
    conditions = []
    params = []

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

    # Step 1: Find k-anonymity-safe provinces (n >= threshold). All overall
    # aggregates below restrict to these provinces so every number on every
    # UI level (overall, region, province) stays mathematically consistent —
    # i.e. sum of provinces = sum of regions = overall.
    safe_provinces_rows = execute_query("""
        SELECT hv.home_province AS pc
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province IS NOT NULL
          AND hv.home_province <> 10
        GROUP BY hv.home_province
        HAVING COUNT(DISTINCT v.patient_id) >= %s
    """, (K_ANONYMITY_THRESHOLD,)) or []
    safe_provinces = [r["pc"] for r in safe_provinces_rows]

    # Short-circuit: if no province meets k-anon, whole payload is suppressed
    if not safe_provinces:
        result = {
            "total_screened": 0,
            "suppressed": True,
            "reason": f"k-anonymity: no province with n >= {K_ANONYMITY_THRESHOLD}",
            "by_disease": [],
            "by_home_province": [],
            "disease_counts": {},
            "physical": {},
            "lab": {},
            "mental": {},
            "last_updated": None,
        }
        cache_set("summary:non_bangkok_overview", result, TTL_T2_AGGREGATE)
        return result

    # Core aggregation: screenings + physical vitals for non-Bangkok residents
    # (restricted to k-anonymity-safe provinces)
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
          AVG(v.sbp)        AS avg_sbp,
          AVG(v.dbp)        AS avg_dbp,
          AVG(v.weight_kg)  AS avg_weight_kg,
          AVG(v.waist_cm)   AS avg_waist_cm,
          AVG(CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) AS avg_bmi,
          MAX(v.visit_date) AS last_visit
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province = ANY(%s)
    """, (safe_provinces,))

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

    # Top home-provinces with per-disease breakdown. Uses the same k-anon
    # safe_provinces set as the overall aggregates so sum(province.count)
    # == overall.total_screened.
    by_home_province_rows = execute_query("""
        SELECT
          hv.home_province AS province_code,
          COUNT(DISTINCT v.patient_id) AS count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm)            AS dm_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt)           AS hpt_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_cvd)           AS cvd_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_bmi)           AS bmi_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dyslipidemia) AS dys_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_stroke)       AS stroke_count
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province = ANY(%s)
        GROUP BY hv.home_province
        ORDER BY count DESC
    """, (safe_provinces,)) or []

    def _disease_breakdown(r: dict) -> dict:
        n = int(r.get("count") or 0)
        pairs = [
            ("diabetes",       "dm_count"),
            ("hypertension",   "hpt_count"),
            ("cardiovascular", "cvd_count"),
            ("obesity",        "bmi_count"),
            ("dyslipidemia",   "dys_count"),
            ("stroke",         "stroke_count"),
        ]
        out = {}
        for key, col in pairs:
            c = int(r.get(col) or 0)
            out[key] = {"count": c, "pct": round(100.0 * c / n, 2) if n else 0}
        return out

    by_home_province = [
        {
            "province_code": r["province_code"],
            "count": int(r["count"]),
            "diseases": _disease_breakdown(r),
        }
        for r in by_home_province_rows
    ]

    # Lab aggregates — same k-anon safe set
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
          AND hv.home_province = ANY(%s)
    """, (safe_provinces,)) or []
    lab_row = lab_rows[0] if lab_rows else {}

    # Exercise / lifestyle — same k-anon safe set
    hh_rows = execute_query("""
        SELECT
          COUNT(DISTINCT v.patient_id) FILTER (WHERE h.exercise = 0) AS no_exercise_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE h.exercise IS NOT NULL) AS exercise_answered
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        LEFT JOIN raw_homehealth h ON h.patient_id = v.patient_id
          AND h.cancel_status IS DISTINCT FROM 1
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province = ANY(%s)
    """, (safe_provinces,)) or []
    hh_row = hh_rows[0] if hh_rows else {}

    # Mental health percentages — same k-anon safe set
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
          AND hv.home_province = ANY(%s)
    """, (safe_provinces,)) or []
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


# =========================================================================== #
# Fiscal year catalog
# =========================================================================== #

@router.get("/fiscal-years")
def list_fiscal_years(min_records: int = Query(100, ge=0, description="Suppress FYs with fewer than N records")):
    """List Thai fiscal years present in raw_vitalsigns with record counts.

    Thai FY X starts Oct 1 (Buddhist year X - 544 → Gregorian X - 544) and
    ends Sep 30 (Buddhist year X - 543). Used by the frontend timeline filter
    to enumerate selectable periods.

    k-anonymity: suppresses FYs with < min_records entries (default 100).
    """
    hit = cache_get(f"summary:fiscal_years:{min_records}")
    if hit is not None:
        return hit

    rows = execute_query("""
        WITH fy AS (
          SELECT
            (EXTRACT(YEAR FROM visit_date)::int + 543 +
              CASE WHEN EXTRACT(MONTH FROM visit_date) >= 10 THEN 1 ELSE 0 END
            ) AS fiscal_year,
            patient_id,
            visit_date
          FROM raw_vitalsigns
          WHERE cancel_status IS DISTINCT FROM 1
            AND visit_date IS NOT NULL
        )
        SELECT
          fiscal_year,
          COUNT(*)::int                         AS records,
          COUNT(DISTINCT patient_id)::int       AS unique_patients,
          MIN(visit_date)::date                 AS first_visit,
          MAX(visit_date)::date                 AS last_visit
        FROM fy
        GROUP BY fiscal_year
        HAVING COUNT(*) >= %s
        ORDER BY fiscal_year DESC
    """, (min_records,)) or []

    result = [
        {
            "fiscal_year": int(r["fiscal_year"]),
            "records": int(r["records"]),
            "unique_patients": int(r["unique_patients"]),
            "first_visit": str(r["first_visit"]) if r["first_visit"] else None,
            "last_visit": str(r["last_visit"]) if r["last_visit"] else None,
            # ISO date range of the FY (for frontend labels)
            "fy_start": f"{int(r['fiscal_year']) - 544}-10-01",
            "fy_end":   f"{int(r['fiscal_year']) - 543}-09-30",
        }
        for r in rows
    ]
    cache_set(f"summary:fiscal_years:{min_records}", result, TTL_T4_STATIC)
    return result
