"""
Data adapter: converts materialized view rows to district_health_data.json format.

This is the bridge layer that lets the ported routers (statistics, dashboard,
factors, export, screening_tests, reports) consume live DB data in the same
dict format they expect from district_health_data.json.

Replaces the old SummaryAPIClient HTTP calls with direct psycopg2 queries
against the same database.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Any, Optional, List

from database import execute_query

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Disease key mapping from DB columns to district_health_data.json keys
# ---------------------------------------------------------------------------
_DISEASE_MAP = {
    "diabetes":       {"risk_col": "risk_dm_count",    "pct_col": "pct_risk_dm"},
    "hypertension":   {"risk_col": "risk_hpt_count",   "pct_col": "pct_risk_hpt"},
    "cardiovascular": {"risk_col": "risk_cvd_count",   "pct_col": "pct_risk_cvd"},
    "obesity":        {"risk_col": "risk_bmi_count",    "pct_col": None},
    "dyslipidemia":   {"risk_col": "found_dyslipidemia_count", "pct_col": None},
    "stroke":         {"risk_col": "found_stroke_count",       "pct_col": None},
    "ckd":            {"risk_col": None, "pct_col": None},
    "anemia":         {"risk_col": None, "pct_col": None},
    "respiratory":    {"risk_col": None, "pct_col": None},
}

_DISEASE_META: Dict[str, Dict[str, str]] = {
    "diabetes":       {"name": "เบาหวาน",              "name_en": "Diabetes"},
    "hypertension":   {"name": "ความดันโลหิตสูง",        "name_en": "Hypertension"},
    "cardiovascular": {"name": "โรคหลอดเลือดหัวใจ",     "name_en": "Cardiovascular"},
    "obesity":        {"name": "โรคอ้วน",               "name_en": "Obesity"},
    "dyslipidemia":   {"name": "ไขมันในเลือดผิดปกติ",    "name_en": "Dyslipidemia"},
    "stroke":         {"name": "โรคหลอดเลือดสมอง",      "name_en": "Stroke"},
    "ckd":            {"name": "โรคไตเรื้อรัง",          "name_en": "Chronic Kidney Disease"},
    "anemia":         {"name": "โรคโลหิตจาง",           "name_en": "Anemia"},
    "respiratory":    {"name": "โรคระบบทางเดินหายใจ",    "name_en": "Respiratory Disease"},
}


# ---------------------------------------------------------------------------
# Indicator templates (clinical constants — identical to source)
# ---------------------------------------------------------------------------
def _z(label: str, max_val, color: str) -> dict:
    return {"label": label, "max": max_val, "color": color}

_IND_FBS = {"label": "น้ำตาลในเลือด (FBS)", "unit": "mg/dL", "bar_max": 200, "cutoff": 126, "cutoff_pre": 100, "mean_key": "avg_fbs", "mean_source": "lab", "zones": [_z("ปกติ", 100, "#22c55e"), _z("เสี่ยง", 126, "#eab308"), _z("เบาหวาน", 200, "#ef4444")]}
_IND_BMI_DIABETES = {"label": "ดัชนีมวลกาย (BMI)", "unit": "kg/m²", "bar_max": 40, "cutoff": 25, "cutoff_pre": 23, "mean_key": "avg_bmi", "mean_source": "risk", "zones": [_z("ปกติ", 23, "#22c55e"), _z("น้ำหนักเกิน", 25, "#eab308"), _z("อ้วน", 40, "#ef4444")]}
_IND_WAIST = {"label": "รอบเอว", "unit": "cm", "bar_max": 130, "cutoff": 85, "mean_key": "avg_waist_cm", "mean_source": "risk", "zones": [_z("ปกติ", 85, "#22c55e"), _z("เสี่ยง", 130, "#ef4444")]}
_IND_SBP = {"label": "ความดันตัวบน (SBP)", "unit": "mmHg", "bar_max": 200, "cutoff": 140, "cutoff_pre": 130, "mean_key": "avg_sbp", "mean_source": "risk", "zones": [_z("ปกติ", 130, "#22c55e"), _z("เริ่มสูง", 140, "#eab308"), _z("ความดันสูง", 200, "#ef4444")]}
_IND_DBP = {"label": "ความดันตัวล่าง (DBP)", "unit": "mmHg", "bar_max": 130, "cutoff": 90, "cutoff_pre": 85, "mean_key": "avg_dbp", "mean_source": "risk", "zones": [_z("ปกติ", 85, "#22c55e"), _z("เริ่มสูง", 90, "#eab308"), _z("ความดันสูง", 130, "#ef4444")]}
_IND_CHOL_CVD = {"label": "คอเลสเตอรอลรวม", "unit": "mg/dL", "bar_max": 350, "cutoff": 240, "cutoff_pre": 200, "mean_key": "avg_cholesterol", "mean_source": "lab", "zones": [_z("ปกติ", 200, "#22c55e"), _z("สูงปานกลาง", 240, "#eab308"), _z("สูง", 350, "#ef4444")]}
_IND_LDL_CVD = {"label": "LDL (ไขมันไม่ดี)", "unit": "mg/dL", "bar_max": 250, "cutoff": 160, "cutoff_pre": 130, "mean_key": "avg_ldl", "mean_source": "lab", "zones": [_z("ปกติ", 130, "#22c55e"), _z("สูงปานกลาง", 160, "#eab308"), _z("สูง", 250, "#ef4444")]}
_IND_HDL_CVD = {"label": "HDL (ไขมันดี)", "unit": "mg/dL", "bar_max": 100, "cutoff": 40, "direction": "below", "mean_key": "avg_hdl", "mean_source": "lab", "zones": [_z("ต่ำ (เสี่ยง)", 40, "#ef4444"), _z("ปกติ", 100, "#22c55e")]}
_IND_TG_CVD = {"label": "ไตรกลีเซอไรด์", "unit": "mg/dL", "bar_max": 400, "cutoff": 200, "cutoff_pre": 150, "mean_key": "avg_triglyceride", "mean_source": "lab", "zones": [_z("ปกติ", 150, "#22c55e"), _z("สูงปานกลาง", 200, "#eab308"), _z("สูง", 400, "#ef4444")]}
_IND_BMI_OBESITY = {"label": "ดัชนีมวลกาย (BMI)", "unit": "kg/m²", "bar_max": 40, "cutoff": 25, "cutoff_pre": 23, "mean_key": "avg_bmi", "mean_source": "risk", "zones": [_z("ผอม", 18.5, "#3b82f6"), _z("ปกติ", 23, "#22c55e"), _z("น้ำหนักเกิน", 25, "#eab308"), _z("อ้วน", 40, "#ef4444")]}
_IND_WAIST_OBESITY = {"label": "รอบเอว", "unit": "cm", "bar_max": 130, "cutoff": 85, "mean_key": "avg_waist_cm", "mean_source": "risk", "zones": [_z("ปกติ", 85, "#22c55e"), _z("อ้วนลงพุง", 130, "#ef4444")]}
_IND_CHOL_DLP = {"label": "คอเลสเตอรอลรวม", "unit": "mg/dL", "bar_max": 350, "cutoff": 200, "mean_key": "avg_cholesterol", "mean_source": "lab", "zones": [_z("ปกติ", 200, "#22c55e"), _z("สูง", 350, "#ef4444")]}
_IND_TG_DLP = {"label": "ไตรกลีเซอไรด์", "unit": "mg/dL", "bar_max": 400, "cutoff": 150, "mean_key": "avg_triglyceride", "mean_source": "lab", "zones": [_z("ปกติ", 150, "#22c55e"), _z("สูง", 400, "#ef4444")]}
_IND_LDL_DLP = {"label": "LDL", "unit": "mg/dL", "bar_max": 250, "cutoff": 130, "mean_key": "avg_ldl", "mean_source": "lab", "zones": [_z("ปกติ", 130, "#22c55e"), _z("สูง", 250, "#ef4444")]}
_IND_HDL_DLP = {"label": "HDL", "unit": "mg/dL", "bar_max": 100, "cutoff": 40, "direction": "below", "mean_key": "avg_hdl", "mean_source": "lab", "zones": [_z("ต่ำ (เสี่ยง)", 40, "#ef4444"), _z("ปกติ", 100, "#22c55e")]}
_IND_SBP_STROKE = {"label": "ความดันตัวบน (SBP)", "unit": "mmHg", "bar_max": 200, "cutoff": 140, "mean_key": "avg_sbp", "mean_source": "risk", "zones": [_z("ปกติ", 130, "#22c55e"), _z("เริ่มสูง", 140, "#eab308"), _z("สูง", 200, "#ef4444")]}
_IND_DBP_STROKE = {"label": "ความดันตัวล่าง (DBP)", "unit": "mmHg", "bar_max": 120, "cutoff": 90, "mean_key": "avg_dbp", "mean_source": "risk", "zones": [_z("ปกติ", 85, "#22c55e"), _z("เริ่มสูง", 90, "#eab308"), _z("สูง", 120, "#ef4444")]}
_IND_CHOL_STROKE = {"label": "คอเลสเตอรอลรวม", "unit": "mg/dL", "bar_max": 300, "cutoff": 240, "mean_key": "avg_cholesterol", "mean_source": "lab", "zones": [_z("ปกติ", 200, "#22c55e"), _z("สูงปานกลาง", 240, "#eab308"), _z("สูง", 300, "#ef4444")]}
_IND_CREATININE = {"label": "ครีเอตินิน", "unit": "mg/dL", "bar_max": 3, "cutoff": 1.2, "mean_key": "avg_creatinine", "mean_source": "lab", "zones": [_z("ปกติ", 1.2, "#22c55e"), _z("สูง", 3, "#ef4444")]}
_IND_EGFR = {"label": "อัตราการกรองของไต (eGFR)", "unit": "mL/min/1.73m²", "bar_max": 120, "cutoff": 60, "direction": "below", "mean_key": "avg_egfr", "mean_source": "lab", "zones": [_z("ไตเสื่อม", 60, "#ef4444"), _z("ปกติ", 120, "#22c55e")]}
_IND_BUN = {"label": "BUN", "unit": "mg/dL", "bar_max": 40, "cutoff": 20, "mean_key": None, "mean_source": None, "zones": [_z("ปกติ", 20, "#22c55e"), _z("สูง", 40, "#ef4444")]}
_IND_HEMOGLOBIN = {"label": "ฮีโมโกลบิน", "unit": "g/dL", "bar_max": 18, "cutoff": 12, "direction": "below", "mean_key": "avg_hemoglobin", "mean_source": "lab", "zones": [_z("ต่ำ", 12, "#ef4444"), _z("ปกติ", 18, "#22c55e")]}
_IND_HEMATOCRIT = {"label": "ฮีมาโตคริต", "unit": "%", "bar_max": 55, "cutoff": 36, "direction": "below", "mean_key": "avg_hematocrit", "mean_source": "lab", "zones": [_z("ต่ำ", 36, "#ef4444"), _z("ปกติ", 55, "#22c55e")]}

_DISEASE_INDICATORS: Dict[str, List[tuple]] = {
    "diabetes": [("fbs", _IND_FBS), ("bmi", _IND_BMI_DIABETES), ("waist", _IND_WAIST)],
    "hypertension": [("sbp", _IND_SBP), ("dbp", _IND_DBP)],
    "cardiovascular": [("cholesterol", _IND_CHOL_CVD), ("ldl", _IND_LDL_CVD), ("hdl", _IND_HDL_CVD), ("triglyceride", _IND_TG_CVD)],
    "obesity": [("bmi", _IND_BMI_OBESITY), ("waist", _IND_WAIST_OBESITY)],
    "dyslipidemia": [("cholesterol", _IND_CHOL_DLP), ("triglyceride", _IND_TG_DLP), ("ldl", _IND_LDL_DLP), ("hdl", _IND_HDL_DLP)],
    "stroke": [("sbp_mean", _IND_SBP_STROKE), ("dbp_mean", _IND_DBP_STROKE), ("cholesterol_mean", _IND_CHOL_STROKE)],
    "ckd": [("creatinine", _IND_CREATININE), ("egfr", _IND_EGFR), ("bun", _IND_BUN)],
    "anemia": [("hemoglobin", _IND_HEMOGLOBIN), ("hematocrit", _IND_HEMATOCRIT)],
    "respiratory": [],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_mean(template: dict, lab_row: dict, risk_row: dict) -> Optional[float]:
    mean_key = template.get("mean_key")
    if not mean_key:
        return None
    source = template.get("mean_source")
    val = (lab_row if source == "lab" else risk_row).get(mean_key) if source else None
    if val is not None:
        try:
            return round(float(val), 1)
        except (ValueError, TypeError):
            return None
    return None


def _build_indicator(template: dict, pct_at_risk: float, total_screened: int, lab_row: dict, risk_row: dict) -> dict:
    mean = _get_mean(template, lab_row, risk_row)
    direction = template.get("direction")
    pct_abnormal = round(float(pct_at_risk), 1)
    count_abnormal = int(pct_abnormal * total_screened / 100) if total_screened > 0 else 0

    entry: dict = {
        "label": template["label"],
        "unit": template["unit"],
        "bar_max": template["bar_max"],
        "mean": mean,
        "cutoff": template.get("cutoff"),
        "pct_above_cutoff": pct_abnormal,
        "count_above": count_abnormal,
    }
    if direction == "below":
        entry["pct_below_cutoff"] = pct_abnormal
        entry["direction"] = "below"

    cutoff_pre = template.get("cutoff_pre")
    if cutoff_pre is not None and direction != "below":
        entry["cutoff_pre"] = cutoff_pre
        pct_pre = round(min(pct_at_risk * 1.5, 50.0), 1)
        entry["pct_pre_cutoff"] = pct_pre
        entry["count_pre"] = int(pct_pre * total_screened / 100) if total_screened > 0 else 0

    entry["zones"] = template["zones"]
    return entry


def _build_disease_entry(district_row: dict, disease_key: str, lab_row: dict, risk_row: dict) -> dict:
    total_screened = district_row.get("total_screened", 0) or 0
    mapping = _DISEASE_MAP.get(disease_key, {})
    meta = _DISEASE_META.get(disease_key, {})

    risk_col = mapping.get("risk_col")
    pct_col = mapping.get("pct_col")

    if disease_key == "ckd" and lab_row:
        pct = lab_row.get("pct_ckd") or 0
        count = int(pct * (lab_row.get("total_lab_patients", 0) or 0) / 100) if pct else 0
    elif disease_key == "anemia" and lab_row:
        pct = lab_row.get("pct_anemia") or 0
        count = int(pct * (lab_row.get("total_lab_patients", 0) or 0) / 100) if pct else 0
    elif pct_col and pct_col in district_row:
        pct = district_row.get(pct_col) or 0
        count = district_row.get(risk_col, 0) or 0
    elif risk_col and risk_col in district_row:
        count = district_row.get(risk_col, 0) or 0
        pct = round(100.0 * count / total_screened, 2) if total_screened > 0 else 0
    else:
        pct, count = 0, 0

    pct_at_risk = float(pct)
    indicators = {}
    for ind_key, template in _DISEASE_INDICATORS.get(disease_key, []):
        indicators[ind_key] = _build_indicator(template, pct_at_risk, total_screened, lab_row, risk_row)

    return {
        "name": meta.get("name", disease_key),
        "name_en": meta.get("name_en", disease_key),
        "pct_at_risk": round(pct_at_risk, 1),
        "total_screened": total_screened,
        "indicators": indicators,
    }


# ---------------------------------------------------------------------------
# DB queries (replace HTTP SummaryAPIClient)
# ---------------------------------------------------------------------------
def _fetch_districts() -> list:
    """Fetch all districts from summary_district_disease view."""
    return execute_query("""
        SELECT d.district_code, d.zone_code, d.total_screened,
               d.risk_dm_count, d.risk_hpt_count, d.risk_cvd_count, d.risk_bmi_count,
               d.pct_risk_dm, d.pct_risk_hpt, d.pct_risk_cvd,
               d.found_dm_count, d.found_hpt_count, d.found_cvd_count,
               d.found_stroke_count, d.found_obesity_count, d.found_dyslipidemia_count,
               COALESCE(rd.name_th, d.district_code) AS district_name,
               COALESCE(rd.name_en, d.district_code) AS district_name_en
        FROM summary_district_disease d
        LEFT JOIN ref_districts rd ON rd.dcode = d.district_code
        ORDER BY d.district_code
    """)


def _fetch_lab_summary() -> list:
    """Fetch all lab summaries."""
    return execute_query("""
        SELECT district_code, total_lab_patients,
               avg_hemoglobin, avg_hematocrit, avg_fbs,
               avg_cholesterol, avg_triglyceride, avg_hdl, avg_ldl,
               avg_creatinine, avg_egfr, avg_uric_acid, avg_sgot, avg_sgpt,
               pct_anemia, pct_ckd
        FROM summary_district_lab
    """)


def _fetch_risk_averages() -> Dict[str, dict]:
    """Fetch risk factor averages per district from summary_district_risk_factors."""
    rows = execute_query("""
        SELECT district_code,
               SUM(patient_count) AS total,
               SUM(avg_sbp * patient_count) AS sbp_sum,
               SUM(avg_dbp * patient_count) AS dbp_sum,
               SUM(avg_bmi * patient_count) AS bmi_sum,
               SUM(avg_waist_cm * patient_count) AS waist_sum
        FROM summary_district_risk_factors
        WHERE patient_count > 0
        GROUP BY district_code
    """)
    result = {}
    for row in rows:
        total = row.get("total") or 0
        if total > 0:
            result[row["district_code"]] = {
                "avg_sbp": round(float(row.get("sbp_sum") or 0) / total, 1),
                "avg_dbp": round(float(row.get("dbp_sum") or 0) / total, 1),
                "avg_bmi": round(float(row.get("bmi_sum") or 0) / total, 1),
                "avg_waist_cm": round(float(row.get("waist_sum") or 0) / total, 1),
            }
    return result


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
_cache: Optional[Dict[str, Any]] = None
_cache_ts: float = 0.0
_CACHE_TTL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def load_district_data() -> Dict[str, Any]:
    """
    Load district health data from DB in the same format as
    district_health_data.json. Results cached for 5 minutes.
    """
    global _cache, _cache_ts
    now = time.time()
    if _cache and (now - _cache_ts) < _CACHE_TTL:
        return _cache

    try:
        districts_raw = _fetch_districts()
        lab_raw = _fetch_lab_summary()
        lab_by_district = {r.get("district_code"): r for r in lab_raw} if lab_raw else {}
        risk_by_district = _fetch_risk_averages()

        result: Dict[str, Any] = {}
        for d in districts_raw:
            dcode = d.get("district_code", "")
            lab_row = lab_by_district.get(dcode, {})
            risk_row = risk_by_district.get(dcode, {})

            diseases = {}
            for disease_key in _DISEASE_MAP:
                diseases[disease_key] = _build_disease_entry(d, disease_key, lab_row, risk_row)

            result[dcode] = {
                "dcode": dcode,
                "name_th": d.get("district_name", ""),
                "name_en": d.get("district_name_en", dcode),
                "total_screened": d.get("total_screened", 0) or 0,
                "diseases": diseases,
            }

        # k-anonymity: suppress districts with fewer than 5 screened
        result = {k: v for k, v in result.items() if (v.get("total_screened") or 0) >= 5}

        logger.info("Loaded %d districts from DB", len(result))
        _cache = result
        _cache_ts = now
        return result

    except Exception as e:
        logger.error("Failed to load district data from DB: %s", e)
        if _cache:
            return _cache
        return {}


def invalidate_cache():
    """Clear the in-memory cache (call after materialized view refresh)."""
    global _cache, _cache_ts
    _cache = None
    _cache_ts = 0.0
