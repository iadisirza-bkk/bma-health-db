"""Epidemiology router — age-group prevalence, disease-lab crosstab,
multi-disease matrix, age pyramid, incidence rate, outbreak detection."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from database import execute_query, execute_scalar
from security import enforce_k_anonymity, suppress_scalar_if_small, K_ANONYMITY_THRESHOLD

router = APIRouter(prefix="/api/v2/epidemiology", tags=["Epidemiology"])

# --------------------------------------------------------------------------- #
# Valid disease keys (shared with main — kept here for validation)
# --------------------------------------------------------------------------- #

DISEASE_KEYS = {
    "diabetes":       {"risk": "risk_dm",  "found": "found_dm",            "pct": "pct_risk_dm"},
    "hypertension":   {"risk": "risk_hpt", "found": "found_hpt",           "pct": "pct_risk_hpt"},
    "cardiovascular": {"risk": "risk_cvd", "found": "found_cvd",           "pct": "pct_risk_cvd"},
    "obesity":        {"risk": "risk_bmi", "found": "found_obesity",        "pct": None},
    "dyslipidemia":   {"risk": None,       "found": "found_dyslipidemia",  "pct": None},
    "stroke":         {"risk": None,       "found": "found_stroke",         "pct": None},
    "ckd":            {"risk": None,       "found": None,                   "pct": None},
    "anemia":         {"risk": None,       "found": None,                   "pct": None},
}


def _validate_disease_key(disease_key: str) -> None:
    if disease_key not in DISEASE_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid disease_key '{disease_key}'. Valid keys: {sorted(DISEASE_KEYS)}",
        )


# =========================================================================== #
# Endpoints
# =========================================================================== #

@router.get("/age-group-prevalence")
def age_group_prevalence(
    district: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
    sex: Optional[int] = Query(None),
):
    """Disease prevalence by Thai lifecycle age group x sex x district."""
    conditions = []
    params = []
    if district:
        conditions.append("s.district_code = %s")
        params.append(district)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    if sex is not None:
        conditions.append("s.sex = %s")
        params.append(sex)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT s.district_code, s.sex, s.age_group, s.total_screened,
               s.risk_dm, s.risk_hpt, s.risk_cvd, s.risk_bmi,
               s.found_dm, s.found_hpt, s.found_cvd, s.found_stroke,
               s.found_obesity, s.found_dyslipidemia
        FROM summary_disease_age_sex s
        JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        ORDER BY s.age_group, s.sex
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_screened") or 0) >= K_ANONYMITY_THRESHOLD]
    return {"age_groups": ["วัยเรียน","วัยเริ่มทำงาน","วัยทำงาน","วัยกลางคน","วัยก่อนสูงอายุ","สูงวัย"], "data": rows}


@router.get("/disease-lab-crosstab")
def disease_lab_crosstab(
    district: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Lab values stratified by disease status (positive vs negative)."""
    conditions = []
    params = []
    if district:
        conditions.append("s.district_code = %s")
        params.append(district)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT s.*
        FROM summary_lab_disease_cross s
        JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        ORDER BY s.district_code
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_patients") or 0) >= K_ANONYMITY_THRESHOLD]
    return {"data": rows}


@router.get("/multi-disease-matrix")
def multi_disease_matrix(
    district: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Disease co-occurrence matrix -- comorbidity analysis."""
    conditions = []
    params = []
    if district:
        conditions.append("s.district_code = %s")
        params.append(district)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT s.district_code, s.total_screened,
               s.dm_only, s.hpt_only, s.obesity_only,
               s.dm_and_hpt, s.dm_and_obesity, s.dm_and_dyslipidemia,
               s.hpt_and_obesity, s.hpt_and_dyslipidemia,
               s.cvd_and_stroke, s.dm_and_cvd,
               s.metabolic_syndrome, s.dm_hpt_obesity,
               s.multi_disease_count, s.no_disease
        FROM summary_comorbidity s
        LEFT JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        ORDER BY s.district_code
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_screened") or 0) >= K_ANONYMITY_THRESHOLD]
    return {"data": rows}


@router.get("/age-pyramid")
def age_pyramid(
    district: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Age-sex pyramid of screened population."""
    conditions = []
    params = []
    if district:
        conditions.append("s.district_code = %s")
        params.append(district)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT s.age_group, s.sex,
               SUM(s.total_screened) AS count
        FROM summary_disease_age_sex s
        JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        GROUP BY s.age_group, s.sex
        ORDER BY s.age_group, s.sex
    """, tuple(params) or None)

    # Pivot into {age_group, male_count, female_count}
    age_map: dict = {}
    for r in rows:
        ag = r["age_group"]
        if ag not in age_map:
            age_map[ag] = {"age_group": ag, "male_count": 0, "female_count": 0}
        sex = r.get("sex")
        cnt = r.get("count") or 0
        if sex == 1:
            age_map[ag]["male_count"] = int(cnt)
        elif sex == 2:
            age_map[ag]["female_count"] = int(cnt)

    result = list(age_map.values())
    # Enforce k-anonymity: suppress rows where both counts are below threshold
    result = [
        r for r in result
        if (r["male_count"] + r["female_count"]) >= K_ANONYMITY_THRESHOLD
    ]

    return {"data": result}


@router.get("/incidence-rate")
def incidence_rate(
    disease: str = Query("diabetes"),
    period: Optional[str] = Query(None, description="e.g. 2024Q3, 2025-01"),
    district: Optional[str] = Query(None),
):
    """Incidence rate: new cases / population at risk per period."""
    _validate_disease_key(disease)
    dk = DISEASE_KEYS[disease]
    bool_col = dk.get("risk") or dk.get("found")
    if not bool_col:
        return {"data_available": False, "message": f"ไม่มีคอลัมน์ risk/found สำหรับ '{disease}' — ใช้ /api/v2/summary/lab แทน"}

    conditions = ["v.cancel_status IS DISTINCT FROM 1", "v.visit_date IS NOT NULL"]
    params: list = []

    if district:
        conditions.append("v.district_code = %s")
        params.append(district)

    where = "WHERE " + " AND ".join(conditions)

    rows = execute_query(f"""
        SELECT
          DATE_TRUNC('month', v.visit_date) AS period,
          COUNT(DISTINCT v.patient_id) AS total_screened,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.{bool_col}) AS new_cases,
          ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.{bool_col})
                / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS incidence_pct
        FROM raw_vitalsigns v
        {where}
        GROUP BY DATE_TRUNC('month', v.visit_date)
        ORDER BY period
    """, tuple(params) or None)

    # Filter by period if specified
    if period and rows:
        filtered = []
        for r in rows:
            p = r.get("period")
            p_str = p.strftime("%Y-%m-%d") if hasattr(p, "strftime") else str(p)
            if period in p_str:
                filtered.append(r)
        rows = filtered

    # Enforce k-anonymity
    rows = [r for r in rows if (r.get("total_screened") or 0) >= K_ANONYMITY_THRESHOLD]

    for r in rows:
        if r.get("period") and hasattr(r["period"], "strftime"):
            r["period"] = r["period"].strftime("%Y-%m-%d")

    if not rows:
        return {"data_available": False, "disease": disease, "message": "ไม่มีข้อมูล incidence สำหรับเงื่อนไขที่ระบุ"}

    return {"disease": disease, "district": district, "period_filter": period, "data": rows}


@router.get("/outbreak-detection")
def outbreak_detection(
    zone_code: Optional[str] = Query(None),
    disease: str = Query("diabetes"),
):
    """Early warning: detect if current prevalence exceeds baseline by 2 SD."""
    _validate_disease_key(disease)
    dk = DISEASE_KEYS[disease]
    bool_col = dk.get("risk") or dk.get("found")
    if not bool_col:
        return {"data_available": False, "message": f"ไม่มีคอลัมน์ risk/found สำหรับ '{disease}' — ใช้ /api/v2/summary/lab แทน"}

    conditions = ["v.cancel_status IS DISTINCT FROM 1", "v.visit_date IS NOT NULL"]
    params: list = []

    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)

    where = "WHERE " + " AND ".join(conditions)

    rows = execute_query(f"""
        SELECT
          DATE_TRUNC('month', v.visit_date) AS period,
          COUNT(DISTINCT v.patient_id) AS total_screened,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.{bool_col}) AS at_risk_count,
          ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.{bool_col})
                / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct
        FROM raw_vitalsigns v
        JOIN ref_districts d ON v.district_code = d.dcode
        {where}
        GROUP BY DATE_TRUNC('month', v.visit_date)
        HAVING COUNT(DISTINCT v.patient_id) >= %s
        ORDER BY period
    """, tuple(params) + (K_ANONYMITY_THRESHOLD,))

    if len(rows) < 3:
        return {
            "disease": disease,
            "zone_code": zone_code,
            "alert": False,
            "reason": "insufficient_data",
            "message": f"ข้อมูลไม่เพียงพอ (มี {len(rows)} ช่วงเวลา ต้องมีอย่างน้อย 3 ช่วง)",
        }

    # Compute baseline from all periods except the latest
    pcts = [float(r.get("pct") or 0) for r in rows]
    baseline_pcts = pcts[:-1]
    current_pct = pcts[-1]

    mean_val = sum(baseline_pcts) / len(baseline_pcts)
    variance = sum((x - mean_val) ** 2 for x in baseline_pcts) / len(baseline_pcts)
    sd_val = variance ** 0.5
    threshold = round(mean_val + 2 * sd_val, 2)

    latest_period = rows[-1].get("period")
    if latest_period and hasattr(latest_period, "strftime"):
        latest_period = latest_period.strftime("%Y-%m-%d")

    return {
        "disease": disease,
        "zone_code": zone_code,
        "latest_period": latest_period,
        "current_pct": current_pct,
        "baseline_mean": round(mean_val, 2),
        "baseline_sd": round(sd_val, 2),
        "threshold": threshold,
        "alert": current_pct > threshold,
        "periods_used": len(baseline_pcts),
    }
