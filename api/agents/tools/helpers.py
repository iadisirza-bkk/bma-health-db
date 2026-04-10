"""Shared helpers for tool implementations — data loading, disease/factor lookups.

Adapted for bma-health-db: uses services.data_adapter.load_district_data()
and data.facts for zone mappings. Factor/disease constants are inlined
since the target project does not have the factors router.
"""
from __future__ import annotations

from data.facts import HEALTH_ZONES, DCODE_TO_ZONE
from services.data_adapter import load_district_data


# ---------------------------------------------------------------
# Disease constants (inlined from source app/data/diseases.py)
# ---------------------------------------------------------------

DISEASE_NAMES_TH: dict[str, str] = {
    "diabetes": "เบาหวาน",
    "hypertension": "ความดันโลหิตสูง",
    "obesity": "โรคอ้วน",
    "dyslipidemia": "ไขมันในเลือดผิดปกติ",
    "cardiovascular": "โรคหลอดเลือดหัวใจ",
    "stroke": "โรคหลอดเลือดสมอง",
    "ckd": "โรคไตเรื้อรัง",
    "anemia": "โรคโลหิตจาง",
    "respiratory": "โรคระบบทางเดินหายใจ",
}

DISEASE_NAMES_EN: dict[str, str] = {
    "diabetes": "Diabetes",
    "hypertension": "Hypertension",
    "obesity": "Obesity",
    "dyslipidemia": "Dyslipidemia",
    "cardiovascular": "Cardiovascular",
    "stroke": "Stroke",
    "ckd": "Chronic Kidney Disease",
    "anemia": "Anemia",
    "respiratory": "Respiratory Disease",
}

# Backward compat alias
DISEASE_NAMES = DISEASE_NAMES_TH

DISEASE_ALIASES: dict[str, str] = {
    "kidney": "ckd",
    "copd": "respiratory",
}

ALL_DISEASES: list[str] = list(DISEASE_NAMES_TH.keys())


def normalize_disease(d: str | None) -> str | None:
    """Normalize disease key — resolve aliases."""
    if not d:
        return None
    d = d.lower().strip()
    return DISEASE_ALIASES.get(d, d) if d in DISEASE_NAMES_TH or d in DISEASE_ALIASES else d


# ---------------------------------------------------------------
# Factor constants (inlined from source app/routers/factors.py)
# ---------------------------------------------------------------

AGE_GROUPS = [
    ("0-19", "0-19 ปี", 0.02),
    ("20-29", "20-29 ปี", 0.08),
    ("30-39", "30-39 ปี", 0.14),
    ("40-49", "40-49 ปี", 0.22),
    ("50-59", "50-59 ปี", 0.25),
    ("60-69", "60-69 ปี", 0.18),
    ("70+", "70+ ปี", 0.11),
]

AGE_MODIFIERS = {
    "0-19": {"diabetes": 0.15, "hypertension": 0.10, "cardiovascular": 0.05, "obesity": 0.40,
             "dyslipidemia": 0.15, "stroke": 0.02, "ckd": 0.05, "copd": 0.05},
    "20-29": {"diabetes": 0.30, "hypertension": 0.25, "cardiovascular": 0.15, "obesity": 0.60,
              "dyslipidemia": 0.35, "stroke": 0.08, "ckd": 0.10, "copd": 0.10},
    "30-39": {"diabetes": 0.55, "hypertension": 0.50, "cardiovascular": 0.40, "obesity": 0.80,
              "dyslipidemia": 0.60, "stroke": 0.20, "ckd": 0.25, "copd": 0.25},
    "40-49": {"diabetes": 0.85, "hypertension": 0.80, "cardiovascular": 0.75, "obesity": 1.00,
              "dyslipidemia": 0.90, "stroke": 0.55, "ckd": 0.60, "copd": 0.55},
    "50-59": {"diabetes": 1.15, "hypertension": 1.20, "cardiovascular": 1.15, "obesity": 1.10,
              "dyslipidemia": 1.15, "stroke": 1.10, "ckd": 1.15, "copd": 1.10},
    "60-69": {"diabetes": 1.45, "hypertension": 1.55, "cardiovascular": 1.60, "obesity": 1.05,
              "dyslipidemia": 1.30, "stroke": 1.80, "ckd": 1.70, "copd": 1.65},
    "70+": {"diabetes": 1.60, "hypertension": 1.75, "cardiovascular": 1.90, "obesity": 0.90,
            "dyslipidemia": 1.25, "stroke": 2.50, "ckd": 2.20, "copd": 2.10},
}

SEX_MODIFIERS = {
    "Male": {
        "diabetes": 1.05, "hypertension": 1.15, "cardiovascular": 1.25,
        "obesity": 0.90, "dyslipidemia": 1.05, "stroke": 1.30, "ckd": 1.10, "copd": 1.40,
    },
    "Female": {
        "diabetes": 0.95, "hypertension": 0.85, "cardiovascular": 0.75,
        "obesity": 1.10, "dyslipidemia": 0.95, "stroke": 0.70, "ckd": 0.90, "copd": 0.60,
    },
}

SMOKING_CATEGORIES = [
    ("Never Smoker", "ไม่เคยสูบ", 0.65),
    ("Current Smoker", "สูบบุหรี่ปัจจุบัน", 0.18),
    ("Former Smoker", "เคยสูบแต่เลิกแล้ว", 0.17),
]

SMOKING_MODIFIERS = {
    "Never Smoker":  {"diabetes": 0.90, "hypertension": 0.85, "cardiovascular": 0.75, "obesity": 1.00, "dyslipidemia": 0.90, "stroke": 0.70, "ckd": 0.90, "copd": 0.30},
    "Current Smoker": {"diabetes": 1.20, "hypertension": 1.30, "cardiovascular": 1.60, "obesity": 0.85, "dyslipidemia": 1.15, "stroke": 1.70, "ckd": 1.20, "copd": 3.00},
    "Former Smoker":  {"diabetes": 1.05, "hypertension": 1.10, "cardiovascular": 1.20, "obesity": 1.10, "dyslipidemia": 1.05, "stroke": 1.25, "ckd": 1.05, "copd": 1.60},
}

ALCOHOL_CATEGORIES = [
    ("Non-drinker", "ไม่ดื่ม", 0.55),
    ("Occasional", "ดื่มเป็นครั้งคราว", 0.25),
    ("Regular", "ดื่มเป็นประจำ", 0.13),
    ("Heavy", "ดื่มหนัก", 0.07),
]

ALCOHOL_MODIFIERS = {
    "Non-drinker": {"diabetes": 0.90, "hypertension": 0.85, "cardiovascular": 0.85, "obesity": 0.95, "dyslipidemia": 0.90, "stroke": 0.80, "ckd": 0.85, "copd": 0.90},
    "Occasional":  {"diabetes": 1.00, "hypertension": 1.00, "cardiovascular": 1.00, "obesity": 1.00, "dyslipidemia": 1.00, "stroke": 1.00, "ckd": 1.00, "copd": 1.00},
    "Regular":     {"diabetes": 1.15, "hypertension": 1.25, "cardiovascular": 1.20, "obesity": 1.10, "dyslipidemia": 1.15, "stroke": 1.30, "ckd": 1.25, "copd": 1.15},
    "Heavy":       {"diabetes": 1.35, "hypertension": 1.50, "cardiovascular": 1.45, "obesity": 1.20, "dyslipidemia": 1.30, "stroke": 1.60, "ckd": 1.55, "copd": 1.35},
}

EXERCISE_CATEGORIES = [
    ("Sedentary", "ไม่ออกกำลังกาย", 0.30),
    ("Light (1-2 days/week)", "เบา (1-2 วัน/สัปดาห์)", 0.28),
    ("Moderate (3-4 days/week)", "ปานกลาง (3-4 วัน/สัปดาห์)", 0.25),
    ("Active (5+ days/week)", "สม่ำเสมอ (5+ วัน/สัปดาห์)", 0.17),
]

EXERCISE_MODIFIERS = {
    "Sedentary":                   {"diabetes": 1.30, "hypertension": 1.25, "cardiovascular": 1.35, "obesity": 1.50, "dyslipidemia": 1.25, "stroke": 1.30, "ckd": 1.15, "copd": 1.10},
    "Light (1-2 days/week)":       {"diabetes": 1.05, "hypertension": 1.05, "cardiovascular": 1.05, "obesity": 1.10, "dyslipidemia": 1.05, "stroke": 1.05, "ckd": 1.00, "copd": 1.00},
    "Moderate (3-4 days/week)":    {"diabetes": 0.85, "hypertension": 0.85, "cardiovascular": 0.80, "obesity": 0.75, "dyslipidemia": 0.85, "stroke": 0.80, "ckd": 0.90, "copd": 0.90},
    "Active (5+ days/week)":       {"diabetes": 0.70, "hypertension": 0.70, "cardiovascular": 0.65, "obesity": 0.55, "dyslipidemia": 0.70, "stroke": 0.65, "ckd": 0.85, "copd": 0.85},
}


FACTOR_CATEGORIES = {
    "age_group": [(k, th, p) for k, th, p in AGE_GROUPS],
    "sex": [("Male", "ชาย", 0.44), ("Female", "หญิง", 0.56)],
    "smoking": SMOKING_CATEGORIES,
    "alcohol": ALCOHOL_CATEGORIES,
    "exercise": EXERCISE_CATEGORIES,
}

FACTOR_MODIFIERS = {
    "age_group": AGE_MODIFIERS,
    "sex": SEX_MODIFIERS,
    "smoking": SMOKING_MODIFIERS,
    "alcohol": ALCOHOL_MODIFIERS,
    "exercise": EXERCISE_MODIFIERS,
}

FILTER_ALIASES = {
    "sex": {"male": "Male", "female": "Female", "ชาย": "Male", "หญิง": "Female"},
    "smoking": {"never": "Never Smoker", "current": "Current Smoker", "former": "Former Smoker",
                "ไม่สูบ": "Never Smoker", "สูบ": "Current Smoker", "เลิกสูบ": "Former Smoker"},
    "alcohol": {"non-drinker": "Non-drinker", "occasional": "Occasional", "regular": "Regular", "heavy": "Heavy",
                "ไม่ดื่ม": "Non-drinker", "ดื่มหนัก": "Heavy"},
    "exercise": {"sedentary": "Sedentary", "light": "Light (1-2 days/week)",
                 "moderate": "Moderate (3-4 days/week)", "active": "Active (5+ days/week)",
                 "ไม่ออกกำลังกาย": "Sedentary"},
}


def resolve_filter(factor: str, value: str) -> str | None:
    v = value.strip().lower()
    if factor in FILTER_ALIASES:
        for alias, internal in FILTER_ALIASES[factor].items():
            if alias.lower() == v:
                return internal
    if factor in FACTOR_MODIFIERS:
        for key in FACTOR_MODIFIERS[factor]:
            if key.lower() == v or v in key.lower():
                return key
    if factor == "age_group":
        for key, _, _ in AGE_GROUPS:
            if v.replace(" ", "") == key.replace(" ", "") or v in key:
                return key
    return value


# ---------------------------------------------------------------
# Data loading + aggregation helpers
# ---------------------------------------------------------------
_data_cache: dict | None = None


def load_data() -> dict:
    """Load district health data from DB via data_adapter (cached)."""
    global _data_cache
    if _data_cache is not None:
        return _data_cache
    data = load_district_data()
    if data:
        _data_cache = data
        return _data_cache
    return {}


def get_base_rates(data: dict) -> dict[str, float]:
    """City-wide weighted average rates per disease."""
    totals: dict[str, float] = {}
    screened: dict[str, int] = {}
    for d in data.values():
        for dk, dv in d["diseases"].items():
            dk_norm = DISEASE_ALIASES.get(dk, dk)
            totals[dk_norm] = totals.get(dk_norm, 0) + dv["pct_at_risk"] * d["total_screened"]
            screened[dk_norm] = screened.get(dk_norm, 0) + d["total_screened"]
    return {dk: round(totals[dk] / screened[dk], 1) for dk in totals if screened[dk] > 0}


def get_total_screened(data: dict) -> int:
    return sum(d["total_screened"] for d in data.values())


def apply_modifier(base: float, modifier: float) -> float:
    return min(round(base * modifier, 1), 95.0)
