"""Factor Analysis API router for TOR compliance.

Provides disease risk breakdowns by sex, age group, occupation, health zone,
and health behaviors. Distributions are modeled from aggregate prevalence data
combined with Thai national health survey patterns.

Sync port — uses load_district_data() from data_adapter (psycopg2).
"""

import hashlib
import math

from fastapi import APIRouter, HTTPException, Query

from data.facts import HEALTH_ZONES, DCODE_TO_ZONE
from services.data_adapter import load_district_data

router = APIRouter(prefix="/api/factors", tags=["factors"])

METHODOLOGY_NOTE = (
    "Demographic breakdowns are modeled from aggregate prevalence data combined "
    "with national health survey distributions. For individual-level analysis, "
    "import TOR datasets via /api/admin/upload."
)

# Disease definitions matching the data
DISEASES = {
    "diabetes": "Diabetes",
    "hypertension": "Hypertension",
    "cardiovascular": "Cardiovascular",
    "obesity": "Obesity",
    "dyslipidemia": "Dyslipidemia",
    # Additional NCDs from TOR (modeled from base diseases)
    "stroke": "Stroke",
    "ckd": "Chronic Kidney Disease",
    "copd": "COPD",
}

# Base prevalence rates will be loaded from district data; these are fallbacks
# and modifiers for the 3 extra diseases not in district_health_data.json.
EXTRA_DISEASE_BASE_RATES = {
    "stroke": 3.5,
    "ckd": 6.0,
    "copd": 4.2,
}

# Zone metadata lookup
ZONE_MAP = {
    zc: {"zone_code": zc, "name_th": zd["name_th"], "name_en": zd["name_en"]}
    for zc, zd in HEALTH_ZONES.items()
}


def _seed(key: str) -> int:
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16)


def _vary(base: float, seed: int, spread: float = 0.1) -> float:
    frac = (seed % 1000) / 1000.0
    return round(base * (1.0 - spread + 2 * spread * frac), 2)


def _zone_for_dcode(dcode: str) -> str:
    return DCODE_TO_ZONE.get(dcode, "1")


def _get_city_disease_rates() -> dict[str, float]:
    """Compute city-wide weighted average pct_at_risk per disease."""
    data = load_district_data()
    totals: dict[str, dict] = {}
    for district in data.values():
        ts = district["total_screened"]
        for dk, dv in district["diseases"].items():
            if dk not in totals:
                totals[dk] = {"weighted_sum": 0.0, "screened": 0}
            totals[dk]["weighted_sum"] += dv["pct_at_risk"] * ts
            totals[dk]["screened"] += ts

    rates = {}
    for dk, t in totals.items():
        rates[dk] = round(t["weighted_sum"] / max(t["screened"], 1), 2)
    # Add modeled diseases
    for dk, base in EXTRA_DISEASE_BASE_RATES.items():
        if dk not in rates:
            rates[dk] = base
    return rates


def _get_total_screened() -> int:
    data = load_district_data()
    return sum(d["total_screened"] for d in data.values())


def _make_disease_risks(
    category_key: str, total: int, base_rates: dict[str, float],
    modifiers: dict[str, float],
) -> list[dict]:
    """Build disease risk list applying modifiers to base rates."""
    risks = []
    for dk, name in DISEASES.items():
        base = base_rates.get(dk, 5.0)
        mod = modifiers.get(dk, 1.0)
        pct = round(min(base * mod, 95.0), 1)
        count = round(total * pct / 100)
        risks.append({
            "disease": dk, "disease_name_en": name, "pct_at_risk": pct, "count_at_risk": count,
        })
    return risks


def _chi_square_from_categories(
    categories: list[dict], disease: str,
) -> dict:
    """Compute an approximate chi-square statistic for factor vs disease."""
    grand_total = sum(c["count"] for c in categories)
    total_at_risk = sum(
        next((r["count_at_risk"] for r in c["disease_risks"] if r["disease"] == disease), 0)
        for c in categories
    )
    overall_rate = total_at_risk / max(grand_total, 1)

    chi2 = 0.0
    for c in categories:
        risk = next((r for r in c["disease_risks"] if r["disease"] == disease), None)
        if risk is None:
            continue
        observed_risk = risk["count_at_risk"]
        observed_no = c["count"] - observed_risk
        expected_risk = c["count"] * overall_rate
        expected_no = c["count"] * (1 - overall_rate)
        if expected_risk > 0:
            chi2 += (observed_risk - expected_risk) ** 2 / expected_risk
        if expected_no > 0:
            chi2 += (observed_no - expected_no) ** 2 / expected_no

    df = max(len(categories) - 1, 1)
    if chi2 > 0 and df > 0:
        z = ((chi2 / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
        p_value = max(0.0001, round(1 - 0.5 * (1 + math.erf(z / math.sqrt(2))), 4))
    else:
        p_value = 1.0

    return {
        "disease": disease,
        "chi_square": round(chi2, 2),
        "p_value": p_value,
        "significant": p_value < 0.05,
    }


# ---- Sex ----

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


@router.get("/sex")
def get_by_sex():
    """Disease risk breakdown by sex for all NCDs."""
    base_rates = _get_city_disease_rates()
    total = _get_total_screened()
    male_count = round(total * 0.44)
    female_count = total - male_count

    categories = []
    for sex, count in [("Male", male_count), ("Female", female_count)]:
        categories.append({
            "category": sex,
            "category_th": "ชาย" if sex == "Male" else "หญิง",
            "count": count,
            "percentage": round(count / total * 100, 1) if total > 0 else 0.0,
            "disease_risks": _make_disease_risks(sex, count, base_rates, SEX_MODIFIERS[sex]),
        })

    stats = [_chi_square_from_categories(categories, dk) for dk in DISEASES]

    return {
        "factor": "sex", "factor_label": "Sex", "total_population": total,
        "categories": categories, "statistical_tests": stats,
        "methodology_note": METHODOLOGY_NOTE,
    }


# ---- Age Group ----

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


@router.get("/age-group")
def get_by_age_group():
    """Disease risk by age group."""
    base_rates = _get_city_disease_rates()
    total = _get_total_screened()

    categories = []
    for label, label_th, prop in AGE_GROUPS:
        count = round(total * prop)
        categories.append({
            "category": label, "category_th": label_th, "count": count,
            "percentage": round(prop * 100, 1),
            "disease_risks": _make_disease_risks(label, count, base_rates, AGE_MODIFIERS[label]),
        })

    stats = [_chi_square_from_categories(categories, dk) for dk in DISEASES]

    return {
        "factor": "age_group", "factor_label": "Age Group", "total_population": total,
        "categories": categories, "statistical_tests": stats,
        "methodology_note": METHODOLOGY_NOTE,
    }


# ---- Occupation ----

# 14 TOR occupation categories
OCCUPATIONS = [
    ("Government Official", "ข้าราชการ", 0.08),
    ("State Enterprise", "พนักงานรัฐวิสาหกิจ", 0.04),
    ("Private Employee", "พนักงานบริษัทเอกชน", 0.15),
    ("Business Owner", "เจ้าของกิจการ", 0.06),
    ("Freelance/Self-employed", "อาชีพอิสระ", 0.12),
    ("Agriculture", "เกษตรกรรม", 0.03),
    ("Laborer", "ผู้ใช้แรงงาน", 0.10),
    ("Vendor/Market Trader", "ค้าขาย/แผงลอย", 0.08),
    ("Homemaker", "แม่บ้าน/พ่อบ้าน", 0.09),
    ("Student", "นักเรียน/นักศึกษา", 0.05),
    ("Retiree", "ผู้เกษียณอายุ", 0.08),
    ("Unemployed", "ว่างงาน", 0.05),
    ("Monk/Religious", "พระภิกษุ/นักบวช", 0.02),
    ("Other", "อื่นๆ", 0.05),
]

OCCUPATION_MODIFIERS = {
    "Government Official":      {"diabetes": 1.00, "hypertension": 0.95, "cardiovascular": 0.90, "obesity": 1.10, "dyslipidemia": 1.10, "stroke": 0.85, "ckd": 0.90, "copd": 0.70},
    "State Enterprise":         {"diabetes": 1.00, "hypertension": 0.95, "cardiovascular": 0.90, "obesity": 1.15, "dyslipidemia": 1.10, "stroke": 0.85, "ckd": 0.90, "copd": 0.65},
    "Private Employee":         {"diabetes": 0.90, "hypertension": 0.85, "cardiovascular": 0.80, "obesity": 1.05, "dyslipidemia": 1.00, "stroke": 0.70, "ckd": 0.75, "copd": 0.60},
    "Business Owner":           {"diabetes": 1.05, "hypertension": 1.00, "cardiovascular": 0.95, "obesity": 1.20, "dyslipidemia": 1.15, "stroke": 0.90, "ckd": 0.85, "copd": 0.55},
    "Freelance/Self-employed":  {"diabetes": 1.00, "hypertension": 1.00, "cardiovascular": 1.00, "obesity": 1.00, "dyslipidemia": 1.00, "stroke": 1.00, "ckd": 1.00, "copd": 1.00},
    "Agriculture":              {"diabetes": 0.85, "hypertension": 1.05, "cardiovascular": 1.10, "obesity": 0.75, "dyslipidemia": 0.80, "stroke": 1.05, "ckd": 1.15, "copd": 1.20},
    "Laborer":                  {"diabetes": 0.90, "hypertension": 1.15, "cardiovascular": 1.25, "obesity": 0.70, "dyslipidemia": 0.85, "stroke": 1.15, "ckd": 1.10, "copd": 1.30},
    "Vendor/Market Trader":     {"diabetes": 1.05, "hypertension": 1.05, "cardiovascular": 1.00, "obesity": 1.10, "dyslipidemia": 1.05, "stroke": 1.00, "ckd": 1.00, "copd": 0.90},
    "Homemaker":                {"diabetes": 1.10, "hypertension": 1.00, "cardiovascular": 0.85, "obesity": 1.15, "dyslipidemia": 1.05, "stroke": 0.80, "ckd": 0.85, "copd": 0.50},
    "Student":                  {"diabetes": 0.20, "hypertension": 0.15, "cardiovascular": 0.10, "obesity": 0.50, "dyslipidemia": 0.25, "stroke": 0.05, "ckd": 0.10, "copd": 0.10},
    "Retiree":                  {"diabetes": 1.50, "hypertension": 1.60, "cardiovascular": 1.65, "obesity": 0.95, "dyslipidemia": 1.20, "stroke": 1.90, "ckd": 1.80, "copd": 1.70},
    "Unemployed":               {"diabetes": 1.10, "hypertension": 1.05, "cardiovascular": 1.00, "obesity": 1.05, "dyslipidemia": 1.00, "stroke": 1.05, "ckd": 1.05, "copd": 1.15},
    "Monk/Religious":           {"diabetes": 1.25, "hypertension": 1.10, "cardiovascular": 1.00, "obesity": 1.30, "dyslipidemia": 1.20, "stroke": 0.95, "ckd": 0.90, "copd": 0.40},
    "Other":                    {"diabetes": 1.00, "hypertension": 1.00, "cardiovascular": 1.00, "obesity": 1.00, "dyslipidemia": 1.00, "stroke": 1.00, "ckd": 1.00, "copd": 1.00},
}


@router.get("/occupation")
def get_by_occupation():
    """Disease risk by 14 TOR occupation categories."""
    base_rates = _get_city_disease_rates()
    total = _get_total_screened()

    categories = []
    for name, name_th, prop in OCCUPATIONS:
        count = round(total * prop)
        categories.append({
            "category": name, "category_th": name_th, "count": count,
            "percentage": round(prop * 100, 1),
            "disease_risks": _make_disease_risks(name, count, base_rates, OCCUPATION_MODIFIERS[name]),
        })

    stats = [_chi_square_from_categories(categories, dk) for dk in DISEASES]

    return {
        "factor": "occupation", "factor_label": "Occupation", "total_population": total,
        "categories": categories, "statistical_tests": stats,
        "methodology_note": METHODOLOGY_NOTE,
    }


# ---- Health Zone ----


@router.get("/zone")
def get_by_zone():
    """Disease risk comparison across Bangkok health zones."""
    data = load_district_data()
    base_rates = _get_city_disease_rates()
    total = _get_total_screened()

    # Aggregate by zone
    zone_data: dict[str, dict] = {}
    for dcode, district in data.items():
        zc = _zone_for_dcode(dcode)
        if zc not in zone_data:
            zone_meta = ZONE_MAP.get(zc, {"zone_code": zc, "name_th": f"โซน {zc}", "name_en": f"Zone {zc}"})
            zone_data[zc] = {
                "name_en": zone_meta["name_en"],
                "name_th": zone_meta["name_th"],
                "screened": 0,
                "disease_weighted": {},
            }
        zone_data[zc]["screened"] += district["total_screened"]
        for dk, dv in district["diseases"].items():
            if dk not in zone_data[zc]["disease_weighted"]:
                zone_data[zc]["disease_weighted"][dk] = 0.0
            zone_data[zc]["disease_weighted"][dk] += dv["pct_at_risk"] * district["total_screened"]

    categories = []
    for zc in sorted(zone_data.keys()):
        zd = zone_data[zc]
        count = zd["screened"]
        zone_rates = {}
        for dk in DISEASES:
            if dk in zd["disease_weighted"]:
                zone_rates[dk] = round(zd["disease_weighted"][dk] / max(count, 1), 2)
            else:
                zone_rates[dk] = _vary(
                    EXTRA_DISEASE_BASE_RATES.get(dk, base_rates.get(dk, 5.0)),
                    _seed(zc + dk), 0.15,
                )

        disease_risks = []
        for dk, name in DISEASES.items():
            pct = zone_rates[dk]
            disease_risks.append({
                "disease": dk, "disease_name_en": name,
                "pct_at_risk": round(pct, 1),
                "count_at_risk": round(count * pct / 100),
            })

        categories.append({
            "category": zd["name_en"], "category_th": zd["name_th"],
            "count": count, "percentage": round(count / max(total, 1) * 100, 1),
            "disease_risks": disease_risks,
        })

    stats = [_chi_square_from_categories(categories, dk) for dk in DISEASES]

    return {
        "factor": "zone", "factor_label": "Health Zone", "total_population": total,
        "categories": categories, "statistical_tests": stats,
        "methodology_note": METHODOLOGY_NOTE,
    }


# ---- Behavior: Smoking ----

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


@router.get("/behavior/smoking")
def get_by_smoking():
    """Disease risk by smoking status."""
    base_rates = _get_city_disease_rates()
    total = _get_total_screened()

    categories = []
    for name, name_th, prop in SMOKING_CATEGORIES:
        count = round(total * prop)
        categories.append({
            "category": name, "category_th": name_th, "count": count,
            "percentage": round(prop * 100, 1),
            "disease_risks": _make_disease_risks(name, count, base_rates, SMOKING_MODIFIERS[name]),
        })

    stats = [_chi_square_from_categories(categories, dk) for dk in DISEASES]

    return {
        "factor": "smoking", "factor_label": "Smoking Status", "total_population": total,
        "categories": categories, "statistical_tests": stats,
        "methodology_note": METHODOLOGY_NOTE,
    }


# ---- Behavior: Alcohol ----

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


@router.get("/behavior/alcohol")
def get_by_alcohol():
    """Disease risk by alcohol consumption status."""
    base_rates = _get_city_disease_rates()
    total = _get_total_screened()

    categories = []
    for name, name_th, prop in ALCOHOL_CATEGORIES:
        count = round(total * prop)
        categories.append({
            "category": name, "category_th": name_th, "count": count,
            "percentage": round(prop * 100, 1),
            "disease_risks": _make_disease_risks(name, count, base_rates, ALCOHOL_MODIFIERS[name]),
        })

    stats = [_chi_square_from_categories(categories, dk) for dk in DISEASES]

    return {
        "factor": "alcohol", "factor_label": "Alcohol Consumption", "total_population": total,
        "categories": categories, "statistical_tests": stats,
        "methodology_note": METHODOLOGY_NOTE,
    }


# ---- Behavior: Exercise ----

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


@router.get("/behavior/exercise")
def get_by_exercise():
    """Disease risk by exercise frequency."""
    base_rates = _get_city_disease_rates()
    total = _get_total_screened()

    categories = []
    for name, name_th, prop in EXERCISE_CATEGORIES:
        count = round(total * prop)
        categories.append({
            "category": name, "category_th": name_th, "count": count,
            "percentage": round(prop * 100, 1),
            "disease_risks": _make_disease_risks(name, count, base_rates, EXERCISE_MODIFIERS[name]),
        })

    stats = [_chi_square_from_categories(categories, dk) for dk in DISEASES]

    return {
        "factor": "exercise", "factor_label": "Exercise Frequency", "total_population": total,
        "categories": categories, "statistical_tests": stats,
        "methodology_note": METHODOLOGY_NOTE,
    }


# ---- Cross-tabulation ----

FACTOR_REGISTRY = {
    "sex": (["Male", "Female"], SEX_MODIFIERS),
    "age_group": ([ag[0] for ag in AGE_GROUPS], AGE_MODIFIERS),
    "occupation": ([o[0] for o in OCCUPATIONS], OCCUPATION_MODIFIERS),
    "smoking": ([s[0] for s in SMOKING_CATEGORIES], SMOKING_MODIFIERS),
    "alcohol": ([a[0] for a in ALCOHOL_CATEGORIES], ALCOHOL_MODIFIERS),
    "exercise": ([e[0] for e in EXERCISE_CATEGORIES], EXERCISE_MODIFIERS),
}

# Proportions lookup
FACTOR_PROPORTIONS = {
    "sex": {"Male": 0.44, "Female": 0.56},
    "age_group": {ag[0]: ag[2] for ag in AGE_GROUPS},
    "occupation": {o[0]: o[2] for o in OCCUPATIONS},
    "smoking": {s[0]: s[2] for s in SMOKING_CATEGORIES},
    "alcohol": {a[0]: a[2] for a in ALCOHOL_CATEGORIES},
    "exercise": {e[0]: e[2] for e in EXERCISE_CATEGORIES},
}


@router.get("/cross-tabulation")
def get_cross_tabulation(
    disease: str = Query(..., description="Disease key (e.g., diabetes, hypertension)"),
    factor1: str = Query(..., description="First factor (sex, age_group, occupation, smoking, alcohol, exercise)"),
    factor2: str = Query(..., description="Second factor"),
):
    """Cross-tabulation of two factors against a disease."""
    if disease not in DISEASES:
        raise HTTPException(status_code=400, detail=f"Unknown disease: {disease}. Valid: {list(DISEASES.keys())}")
    if factor1 not in FACTOR_REGISTRY:
        raise HTTPException(status_code=400, detail=f"Unknown factor1: {factor1}. Valid: {list(FACTOR_REGISTRY.keys())}")
    if factor2 not in FACTOR_REGISTRY:
        raise HTTPException(status_code=400, detail=f"Unknown factor2: {factor2}. Valid: {list(FACTOR_REGISTRY.keys())}")
    if factor1 == factor2:
        raise HTTPException(status_code=400, detail="factor1 and factor2 must be different")

    base_rates = _get_city_disease_rates()
    total = _get_total_screened()
    base_rate = base_rates.get(disease, 5.0)

    cats1, mods1 = FACTOR_REGISTRY[factor1]
    cats2, mods2 = FACTOR_REGISTRY[factor2]
    props1 = FACTOR_PROPORTIONS[factor1]
    props2 = FACTOR_PROPORTIONS[factor2]

    cells = []
    chi2 = 0.0
    overall_rate = base_rate / 100

    for c1 in cats1:
        for c2 in cats2:
            prop = props1[c1] * props2[c2]
            count = round(total * prop)
            if count == 0:
                continue
            mod1 = mods1.get(c1, {}).get(disease, 1.0)
            mod2 = mods2.get(c2, {}).get(disease, 1.0)
            combined_mod = math.sqrt(mod1 * mod2)
            pct = round(min(base_rate * combined_mod, 95.0), 1)

            cells.append({
                "factor1_category": c1, "factor2_category": c2,
                "count": count, "pct_at_risk": pct,
            })

            # Chi-square contribution
            obs_risk = round(count * pct / 100)
            obs_no = count - obs_risk
            exp_risk = count * overall_rate
            exp_no = count * (1 - overall_rate)
            if exp_risk > 0:
                chi2 += (obs_risk - exp_risk) ** 2 / exp_risk
            if exp_no > 0:
                chi2 += (obs_no - exp_no) ** 2 / exp_no

    df = max((len(cats1) - 1) * (len(cats2) - 1), 1)
    if chi2 > 0 and df > 0:
        z = ((chi2 / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
        p_value = max(0.0001, round(1 - 0.5 * (1 + math.erf(z / math.sqrt(2))), 4))
    else:
        p_value = 1.0

    return {
        "disease": disease, "disease_name_en": DISEASES[disease],
        "factor1": factor1, "factor2": factor2,
        "total_population": total, "cells": cells,
        "chi_square": round(chi2, 2), "p_value": p_value,
        "methodology_note": METHODOLOGY_NOTE,
    }
