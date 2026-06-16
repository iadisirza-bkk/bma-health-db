"""Health Promotion router — BMI distribution, behavior-disease correlation,
risk factor profile, exercise frequency, waist risk analysis.
Refactored for bma_med.* schema."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from database import execute_query, execute_scalar
from security import enforce_k_anonymity, suppress_scalar_if_small, K_ANONYMITY_THRESHOLD

router = APIRouter(prefix="/api/v2/promotion", tags=["Health Promotion"])

# --------------------------------------------------------------------------- #
# Reusable UNION across the two main vitalsigns sources (app1 + portal).
# Subselect aliased as `v` so handlers can plug it in as `FROM ({_VISITS_UNION_SQL}) v`.
# --------------------------------------------------------------------------- #
_VISITS_UNION_SQL = """
SELECT patient_id, vstdate AS visit_date, hbpn AS sbp, lbpn AS dbp,
       alcohal AS alcohol, smoke,
       record_cancelled AS cancel_status,
       dm AS found_dm, hpt AS found_hpt, cdvcl AS found_cvd,
       stroke AS found_stroke, fat AS found_obesity, chltr AS found_dyslipidemia,
       riskdm AS risk_dm, riskhpt AS risk_hpt,
       riskcdvcl AS risk_cvd, riskbmi AS risk_bmi
FROM bma_med.app1_vitalsignslf
UNION ALL
SELECT patient_id, vstdate AS visit_date, hbpn AS sbp, lbpn AS dbp,
       alcohal AS alcohol, smoke,
       record_cancelled AS cancel_status,
       dm AS found_dm, hpt AS found_hpt, cdvcl AS found_cvd,
       stroke AS found_stroke, fat AS found_obesity, chltr AS found_dyslipidemia,
       riskdm AS risk_dm, riskhpt AS risk_hpt,
       riskcdvcl AS risk_cvd, riskbmi AS risk_bmi
FROM bma_med.portal_vitalsignslf
"""

# Reusable UNION across homehealth sources (excercise, diet flags etc.)
_HOMEHEALTH_UNION_SQL = """
SELECT patient_id, vstdate, excercise AS exercise,
       food, water, noodle,
       record_cancelled AS cancel_status
FROM bma_med.app1_homehealth
UNION ALL
SELECT patient_id, vstdate, excercise AS exercise,
       food, water, noodle,
       record_cancelled AS cancel_status
FROM bma_med.portal_homehealth
"""

# Homevisit-derived district lookup (district_code lives in homevisit, not vitalsignslf)
_HOMEVISIT_DISTRICT_SQL = """
SELECT patient_id, COALESCE(crdistrict, district)::text AS district_code
FROM bma_med.app1_homevisit
UNION ALL
SELECT patient_id, COALESCE(crdistrict, district)::text AS district_code
FROM bma_med.portal_homevisit
"""

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

@router.get("/bmi-distribution")
def bmi_distribution(
    district: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """BMI category distribution + waist circumference risk per district."""
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
        SELECT s.district_code, s.sex, s.total_measured,
               s.bmi_underweight, s.bmi_normal, s.bmi_overweight, s.bmi_obese, s.bmi_severely_obese,
               s.avg_bmi, s.total_waist_measured, s.avg_waist,
               s.male_waist_risk, s.female_waist_risk,
               s.avg_height, s.avg_weight
        FROM summary_bmi_waist s
        JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        ORDER BY s.district_code, s.sex
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_measured") or 0) >= K_ANONYMITY_THRESHOLD]

    return {
        "bmi_categories": {
            "underweight": {"label": "ผอม", "range": "< 18.5"},
            "normal": {"label": "ปกติ", "range": "18.5-22.9"},
            "overweight": {"label": "ท้วม", "range": "23-24.9"},
            "obese": {"label": "อ้วน", "range": "25-29.9"},
            "severely_obese": {"label": "อ้วนมาก", "range": "≥ 30"},
        },
        "data": rows,
    }


@router.get("/behavior-disease-correlation")
def behavior_disease_correlation(
    behavior: str = Query("smoking", description="smoking|alcohol|exercise"),
    disease: str = Query("diabetes"),
    district: Optional[str] = Query(None),
):
    """Correlation between lifestyle behavior and disease prevalence."""
    _validate_disease_key(disease)

    valid_behaviors = {"smoking", "alcohol", "exercise"}
    if behavior not in valid_behaviors:
        raise HTTPException(status_code=400, detail=f"Invalid behavior '{behavior}'. Valid: {sorted(valid_behaviors)}")

    dk = DISEASE_KEYS[disease]
    risk_col = dk.get("risk")
    found_col = dk.get("found")

    if behavior in ("smoking", "exercise"):
        # Data from summary_district_risk_factors. behavior is membership-
        # checked against valid_behaviors above, but assert again before
        # f-string interpolation so a future refactor that loosens the
        # whitelist can't silently introduce SQL injection.
        assert behavior in {"smoking", "alcohol", "exercise"}, behavior
        behavior_col = behavior
        conditions = []
        params: list = []
        if district:
            conditions.append("district_code = %s")
            params.append(district)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        total_check = execute_scalar(
            f'SELECT COUNT(*) FROM summary_district_risk_factors WHERE "{behavior_col}" IS NOT NULL'
        ) or 0
        if total_check == 0:
            return {"data_available": False, "message": f"ไม่มีข้อมูล {behavior} ใน summary_district_risk_factors"}

        rows = execute_query(f"""
            SELECT
              "{behavior_col}" AS behavior_value,
              SUM(patient_count)::int AS total
            FROM summary_district_risk_factors
            {where}
            GROUP BY "{behavior_col}"
            ORDER BY "{behavior_col}"
        """, tuple(params) or None)

        rows = enforce_k_anonymity(rows, count_field="total")

        behavior_labels = {
            "smoking": {0: "ไม่สูบ", 1: "สูบ"},
            "exercise": {1: "≥3 วัน/สัปดาห์", 2: "<3 วัน/สัปดาห์", 3: "ไม่ออกกำลังกาย"},
        }
        labels = behavior_labels.get(behavior, {})
        for r in rows:
            val = r.get("behavior_value")
            r["behavior_label"] = labels.get(val, str(val))

        return {"behavior": behavior, "disease": disease, "district": district, "data": rows}

    else:
        # alcohol — moved to vitalsignslf in new schema (alcohal column)
        total_check = execute_scalar(f"""
            SELECT COUNT(*) FROM (
                SELECT alcohal FROM bma_med.app1_vitalsignslf WHERE alcohal IS NOT NULL
                UNION ALL
                SELECT alcohal FROM bma_med.portal_vitalsignslf WHERE alcohal IS NOT NULL
            ) t
        """) or 0
        if total_check == 0:
            return {"data_available": False, "message": "ไม่มีข้อมูล alcohol ใน vitalsignslf — ต้องรอข้อมูลจาก HDC"}

        district_join = ""
        district_filter = ""
        params = []
        if district:
            district_join = f"JOIN ({_HOMEVISIT_DISTRICT_SQL}) hv ON v.patient_id = hv.patient_id"
            district_filter = " AND hv.district_code = %s"
            params.append(district)

        rows = execute_query(f"""
            SELECT
              v.alcohol AS behavior_value,
              COUNT(DISTINCT v.patient_id) AS total
            FROM ({_VISITS_UNION_SQL}) v
            {district_join}
            WHERE v.alcohol IS NOT NULL
              AND v.cancel_status IS DISTINCT FROM 1{district_filter}
            GROUP BY v.alcohol
            ORDER BY v.alcohol
        """, tuple(params) or None)

        rows = enforce_k_anonymity(rows, count_field="total")
        # Codebook (factsheet): 0=ไม่ดื่ม, 1=ดื่ม, 2=เคยดื่มแต่เลิกแล้ว
        alcohol_labels = {0: "ไม่ดื่ม", 1: "ดื่ม", 2: "เลิกแล้ว"}
        for r in rows:
            val = r.get("behavior_value")
            r["behavior_label"] = alcohol_labels.get(val, str(val))

        return {"behavior": behavior, "disease": disease, "district": district, "data": rows}


@router.get("/risk-factor-profile")
def risk_factor_profile(
    district: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Risk factor summary per district: smoking, alcohol, exercise rates."""
    conditions = []
    params: list = []
    if district:
        conditions.append("s.district_code = %s")
        params.append(district)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT
          s.district_code,
          SUM(s.patient_count)::int AS total,
          SUM(CASE WHEN s.smoking = 1 THEN s.patient_count ELSE 0 END)::int AS smoking_count,
          SUM(CASE WHEN s.exercise = 3 THEN s.patient_count ELSE 0 END)::int AS no_exercise_count
        FROM summary_district_risk_factors s
        JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        GROUP BY s.district_code
        ORDER BY s.district_code
    """, tuple(params) or None)

    rows = enforce_k_anonymity(rows, count_field="total")

    for r in rows:
        total = r.get("total") or 1
        r["pct_smoking"] = round(100.0 * (r.get("smoking_count") or 0) / total, 2)
        r["pct_no_exercise"] = round(100.0 * (r.get("no_exercise_count") or 0) / total, 2)

    if not rows:
        return {"data_available": False, "message": "ไม่มีข้อมูล risk factor ใน summary_district_risk_factors"}

    return {"district": district, "zone_code": zone_code, "data": rows}


@router.get("/exercise-frequency")
def exercise_frequency(district: Optional[str] = Query(None)):
    """Exercise frequency distribution: >=3/wk, <3/wk, never."""
    total_check = execute_scalar(f"""
        SELECT COUNT(*) FROM ({_HOMEHEALTH_UNION_SQL}) h WHERE h.exercise IS NOT NULL
    """) or 0
    if total_check == 0:
        return {
            "data_available": False,
            "message": "ไม่มีข้อมูลการออกกำลังกาย (excercise) ใน homehealth — ต้องรอข้อมูลจาก HDC",
        }

    district_join = ""
    district_filter = ""
    params: list = []
    if district:
        district_join = f"JOIN ({_HOMEVISIT_DISTRICT_SQL}) hv ON h.patient_id = hv.patient_id"
        district_filter = " AND hv.district_code = %s"
        params.append(district)

    rows = execute_query(f"""
        SELECT
          {"hv.district_code" if district else "NULL::text"} AS district_code,
          SUM(CASE WHEN h.exercise = 1 THEN 1 ELSE 0 END) AS exercise_3plus,
          SUM(CASE WHEN h.exercise = 2 THEN 1 ELSE 0 END) AS exercise_less3,
          SUM(CASE WHEN h.exercise = 3 THEN 1 ELSE 0 END) AS exercise_never,
          COUNT(*) AS total
        FROM ({_HOMEHEALTH_UNION_SQL}) h
        {district_join}
        WHERE h.exercise IS NOT NULL
          AND h.cancel_status IS DISTINCT FROM 1{district_filter}
        GROUP BY {"hv.district_code" if district else "1"}
        ORDER BY 1
    """, tuple(params) or None)

    rows = enforce_k_anonymity(rows, count_field="total")

    return {
        "exercise_codes": {"1": ">=3 วัน/สัปดาห์", "2": "<3 วัน/สัปดาห์", "3": "ไม่ออกกำลังกาย"},
        "district": district,
        "data": rows,
    }


@router.get("/waist-risk-analysis")
def waist_risk_analysis(zone_code: Optional[str] = Query(None)):
    """Waist circumference risk: % exceeding threshold (M>90cm, F>80cm) per district."""
    conditions = []
    params: list = []
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT
          s.district_code,
          SUM(s.total_waist_measured)::int AS total_measured,
          SUM(s.male_waist_risk)::int AS male_risk_count,
          SUM(s.female_waist_risk)::int AS female_risk_count,
          ROUND(100.0 * (SUM(s.male_waist_risk) + SUM(s.female_waist_risk))
                / NULLIF(SUM(s.total_waist_measured), 0), 2) AS pct_at_risk
        FROM summary_bmi_waist s
        JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        GROUP BY s.district_code
        ORDER BY s.district_code
    """, tuple(params) or None)

    rows = enforce_k_anonymity(rows, count_field="total_measured")

    if not rows:
        return {"data_available": False, "message": "ไม่มีข้อมูลรอบเอว — ต้องรอข้อมูลจาก HDC"}

    return {
        "thresholds": {"male": ">90 cm", "female": ">80 cm"},
        "zone_code": zone_code,
        "data": rows,
    }


# =========================================================================== #
# Diet-disease correlation (NEW — Doc 01 Governor requirement)
# =========================================================================== #

# Map old diet column names → new bma_med columns (homehealth.food/water/noodle)
# Old food_preference_sweet/salty/fatty/fried don't have direct equivalents.
DIET_COLUMNS = {
    "sweet": "water",        # sugary drinks proxy (water column = น้ำอัดลม/กาแฟเย็น/ชานม)
    "salty": "noodle",       # instant noodle/seasoning proxy
    "fatty": "food",         # fried/curry/coconut proxy
    "fried": "food",
    "sugary_drinks": "water",
    "instant_noodle": "noodle",
}

DIET_DISEASE_MAP = {
    "diabetes": "found_dm",
    "hypertension": "found_hpt",
    "cardiovascular": "found_cvd",
    "obesity": "found_obesity",
    "dyslipidemia": "found_dyslipidemia",
    "stroke": "found_stroke",
}


@router.get("/diet-disease-correlation")
def diet_disease_correlation(
    diet: str = Query("sweet", description="Diet factor: sweet, salty, fatty, fried, sugary_drinks, instant_noodle"),
    disease: str = Query("diabetes", description="Disease: diabetes, hypertension, cardiovascular, obesity, dyslipidemia, stroke"),
    district: Optional[str] = Query(None),
):
    """Correlation between dietary behavior and disease prevalence.
    ความสัมพันธ์ระหว่างพฤติกรรมอาหารกับความชุกโรค
    ตอบคำถามผู้ว่า: 'กินเค็มแล้วเป็นความดันจริงไหม?'"""

    if diet not in DIET_COLUMNS:
        return {"error": f"Invalid diet. Valid: {sorted(DIET_COLUMNS.keys())}"}
    if disease not in DIET_DISEASE_MAP:
        return {"error": f"Invalid disease. Valid: {sorted(DIET_DISEASE_MAP.keys())}"}

    # TODO: bma_med equivalent unclear — old food_preference_sweet/salty/fatty/fried
    # were standalone columns; new schema has only 3 dietary axes (food/water/noodle).
    # We map best-effort but the semantics may not be 1:1.
    diet_col = DIET_COLUMNS[diet]
    disease_col = DIET_DISEASE_MAP[disease]

    # First check if diet data exists
    filled = execute_scalar(f"""
        SELECT COUNT(*) FROM (
            SELECT {diet_col} FROM bma_med.app1_homehealth WHERE {diet_col} IS NOT NULL
            UNION ALL
            SELECT {diet_col} FROM bma_med.portal_homehealth WHERE {diet_col} IS NOT NULL
        ) t
    """) or 0

    if filled == 0:
        return {
            "data_available": False,
            "diet": diet,
            "disease": disease,
            "message": f"ไม่มีข้อมูล {diet} (คอลัมน์ {diet_col} เป็น NULL ทั้งหมด) — ต้องรอข้อมูลจาก HDC",
            "suggestion": "ข้อมูลพฤติกรรมอาหารยังไม่ได้นำเข้า ใช้ exercise + smoking + alcohol ที่มีอยู่แทนได้",
        }

    # Join homehealth (diet) with vitalsigns-union (disease) via patient_id
    district_join = ""
    district_filter = ""
    params: list = []
    if district:
        district_join = f"JOIN ({_HOMEVISIT_DISTRICT_SQL}) hv ON v.patient_id = hv.patient_id"
        district_filter = " AND hv.district_code = %s"
        params.append(district)

    rows = execute_query(f"""
        SELECT
            h.{diet_col} AS diet_value,
            COUNT(DISTINCT v.patient_id) AS total_patients,
            COUNT(DISTINCT v.patient_id) FILTER (WHERE v.{disease_col} = 1) AS disease_count,
            ROUND(
                100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.{disease_col} = 1)
                / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2
            ) AS disease_pct
        FROM ({_HOMEHEALTH_UNION_SQL}) h
        JOIN ({_VISITS_UNION_SQL}) v ON h.patient_id = v.patient_id
        {district_join}
        WHERE h.cancel_status IS DISTINCT FROM 1
          AND v.cancel_status IS DISTINCT FROM 1
          AND h.{diet_col} IS NOT NULL{district_filter}
        GROUP BY h.{diet_col}
        HAVING COUNT(DISTINCT v.patient_id) >= %s
        ORDER BY h.{diet_col}
    """, tuple(params) + (K_ANONYMITY_THRESHOLD,))

    return {
        "data_available": True,
        "diet": diet,
        "diet_column": diet_col,
        "disease": disease,
        "disease_column": disease_col,
        "district": district,
        "k_anonymity_threshold": K_ANONYMITY_THRESHOLD,
        "data": rows,
    }
