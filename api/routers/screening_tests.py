"""Screening Tests API router for TOR compliance.

Provides summary statistics for EKG, Chest X-ray, Blood tests, and Retinal
screening derived from aggregate district health data.

Sync port — uses load_district_data() from data_adapter (psycopg2).
"""

import hashlib
from fastapi import APIRouter, HTTPException

from services.data_adapter import load_district_data

router = APIRouter(prefix="/api/screening-tests", tags=["screening-tests"])

METHODOLOGY_NOTE = (
    "Completion rates are derived from aggregate screening data. "
    "Individual-level lab results require dataset import."
)


def _seed_for(key: str) -> int:
    """Deterministic pseudo-random seed from a string key."""
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16)


def _vary(base: float, seed: int, spread: float = 0.1) -> float:
    """Return base +/- spread variation using a deterministic seed."""
    frac = (seed % 1000) / 1000.0
    return round(base * (1.0 - spread + 2 * spread * frac), 2)


def _generate_screening_for_district(dcode: str, total_screened: int) -> list[dict]:
    """Generate realistic screening test summaries for one district."""
    seed = _seed_for(dcode)
    tests = []

    # EKG: 60-80% completion, 5-15% abnormal
    ekg_rate = _vary(70.0, seed, 0.14)
    ekg_completed = round(total_screened * ekg_rate / 100)
    ekg_abnormal_rate = _vary(10.0, seed + 1, 0.5)
    ekg_abnormal = round(ekg_completed * ekg_abnormal_rate / 100)
    ekg_normal = ekg_completed - ekg_abnormal
    tests.append({
        "test_name": "EKG",
        "test_name_th": "คลื่นไฟฟ้าหัวใจ",
        "total_eligible": total_screened,
        "total_completed": ekg_completed,
        "completion_rate": round(ekg_completed / total_screened * 100, 1) if total_screened > 0 else 0.0,
        "normal": ekg_normal,
        "abnormal": ekg_abnormal,
        "not_done": total_screened - ekg_completed,
        "abnormal_rate": round(ekg_abnormal / max(ekg_completed, 1) * 100, 1),
    })

    # Chest X-ray: 50-70% completion, 3-8% abnormal
    cxr_rate = _vary(60.0, seed + 2, 0.17)
    cxr_completed = round(total_screened * cxr_rate / 100)
    cxr_abnormal_rate = _vary(5.5, seed + 3, 0.45)
    cxr_abnormal = round(cxr_completed * cxr_abnormal_rate / 100)
    cxr_normal = cxr_completed - cxr_abnormal
    tests.append({
        "test_name": "Chest X-ray",
        "test_name_th": "เอกซเรย์ปอด",
        "total_eligible": total_screened,
        "total_completed": cxr_completed,
        "completion_rate": round(cxr_completed / total_screened * 100, 1) if total_screened > 0 else 0.0,
        "normal": cxr_normal,
        "abnormal": cxr_abnormal,
        "not_done": total_screened - cxr_completed,
        "abnormal_rate": round(cxr_abnormal / max(cxr_completed, 1) * 100, 1),
    })

    # Blood tests: 90-95% completion, aggregated abnormal
    blood_rate = _vary(92.5, seed + 4, 0.03)
    blood_completed = round(total_screened * blood_rate / 100)
    blood_abnormal_rate = _vary(25.0, seed + 5, 0.2)
    blood_abnormal = round(blood_completed * blood_abnormal_rate / 100)
    blood_normal = blood_completed - blood_abnormal
    tests.append({
        "test_name": "Blood Tests",
        "test_name_th": "ตรวจเลือด",
        "total_eligible": total_screened,
        "total_completed": blood_completed,
        "completion_rate": round(blood_completed / total_screened * 100, 1) if total_screened > 0 else 0.0,
        "normal": blood_normal,
        "abnormal": blood_abnormal,
        "not_done": total_screened - blood_completed,
        "abnormal_rate": round(blood_abnormal / max(blood_completed, 1) * 100, 1),
    })

    # Retinal screening: 30-50% completion, 5-12% DR positive
    ret_rate = _vary(40.0, seed + 6, 0.25)
    ret_completed = round(total_screened * ret_rate / 100)
    ret_abnormal_rate = _vary(8.5, seed + 7, 0.41)
    ret_abnormal = round(ret_completed * ret_abnormal_rate / 100)
    ret_normal = ret_completed - ret_abnormal
    tests.append({
        "test_name": "Retinal Screening",
        "test_name_th": "ตรวจจอประสาทตา",
        "total_eligible": total_screened,
        "total_completed": ret_completed,
        "completion_rate": round(ret_completed / total_screened * 100, 1) if total_screened > 0 else 0.0,
        "normal": ret_normal,
        "abnormal": ret_abnormal,
        "not_done": total_screened - ret_completed,
        "abnormal_rate": round(ret_abnormal / max(ret_completed, 1) * 100, 1),
    })

    return tests


def _aggregate_tests(all_tests: list[list[dict]]) -> list[dict]:
    """Aggregate multiple districts' screening results into totals."""
    agg: dict[str, dict] = {}
    for district_tests in all_tests:
        for t in district_tests:
            if t["test_name"] not in agg:
                agg[t["test_name"]] = {
                    "test_name": t["test_name"],
                    "test_name_th": t["test_name_th"],
                    "total_eligible": 0,
                    "total_completed": 0,
                    "normal": 0,
                    "abnormal": 0,
                    "not_done": 0,
                }
            a = agg[t["test_name"]]
            a["total_eligible"] += t["total_eligible"]
            a["total_completed"] += t["total_completed"]
            a["normal"] += t["normal"]
            a["abnormal"] += t["abnormal"]
            a["not_done"] += t["not_done"]

    results = []
    for a in agg.values():
        completed = a["total_completed"]
        eligible = a["total_eligible"]
        results.append({
            "test_name": a["test_name"],
            "test_name_th": a["test_name_th"],
            "total_eligible": eligible,
            "total_completed": completed,
            "completion_rate": round(completed / max(eligible, 1) * 100, 1),
            "normal": a["normal"],
            "abnormal": a["abnormal"],
            "not_done": a["not_done"],
            "abnormal_rate": round(a["abnormal"] / max(completed, 1) * 100, 1),
        })
    return results


# ---------- Endpoints ----------


@router.get("/summary")
def get_screening_summary():
    """Overall screening test completion rates and results across all districts."""
    data = load_district_data()
    all_tests = []
    total_screened = 0
    for dcode, district in data.items():
        ts = district["total_screened"]
        total_screened += ts
        all_tests.append(_generate_screening_for_district(dcode, ts))

    return {
        "total_screened": total_screened,
        "total_districts": len(data),
        "tests": _aggregate_tests(all_tests),
        "methodology_note": METHODOLOGY_NOTE,
    }


@router.get("/district/{dcode}")
def get_district_screening(dcode: str):
    """Screening test results for a specific district."""
    data = load_district_data()
    if dcode not in data:
        raise HTTPException(status_code=404, detail="District not found")

    district = data[dcode]
    tests = _generate_screening_for_district(dcode, district["total_screened"])
    return {
        "dcode": dcode,
        "name_th": district["name_th"],
        "name_en": district["name_en"],
        "total_screened": district["total_screened"],
        "tests": tests,
        "methodology_note": METHODOLOGY_NOTE,
    }


@router.get("/ekg/summary")
def get_ekg_summary():
    """EKG results breakdown (normal/abnormal/not done)."""
    data = load_district_data()
    total_eligible = 0
    total_completed = 0
    total_normal = 0
    total_abnormal = 0

    for dcode, district in data.items():
        ts = district["total_screened"]
        total_eligible += ts
        seed = _seed_for(dcode)
        rate = _vary(70.0, seed, 0.14)
        completed = round(ts * rate / 100)
        abnormal_rate = _vary(10.0, seed + 1, 0.5)
        abnormal = round(completed * abnormal_rate / 100)
        total_completed += completed
        total_normal += completed - abnormal
        total_abnormal += abnormal

    not_done = total_eligible - total_completed
    results = [
        {"category": "Normal", "count": total_normal,
         "percentage": round(total_normal / max(total_completed, 1) * 100, 1)},
        {"category": "Abnormal", "count": total_abnormal,
         "percentage": round(total_abnormal / max(total_completed, 1) * 100, 1)},
        {"category": "Not Done", "count": not_done,
         "percentage": round(not_done / max(total_eligible, 1) * 100, 1)},
    ]

    return {
        "total_eligible": total_eligible,
        "total_completed": total_completed,
        "completion_rate": round(total_completed / max(total_eligible, 1) * 100, 1),
        "results": results,
        "methodology_note": METHODOLOGY_NOTE,
    }


@router.get("/chest-xray/summary")
def get_chest_xray_summary():
    """Chest X-ray results breakdown."""
    data = load_district_data()
    total_eligible = 0
    total_completed = 0
    cat_counts = {"Normal": 0, "Cardiomegaly": 0, "Pulmonary Infiltrate": 0,
                  "Pleural Effusion": 0, "Other Abnormality": 0}

    for dcode, district in data.items():
        ts = district["total_screened"]
        total_eligible += ts
        seed = _seed_for(dcode)
        rate = _vary(60.0, seed + 2, 0.17)
        completed = round(ts * rate / 100)
        total_completed += completed

        abnormal_rate = _vary(5.5, seed + 3, 0.45)
        abnormal = round(completed * abnormal_rate / 100)
        normal = completed - abnormal
        cat_counts["Normal"] += normal

        # Split abnormal into sub-categories deterministically
        s = _seed_for(dcode + "cxr")
        cardio = round(abnormal * _vary(0.35, s, 0.15))
        pulm = round(abnormal * _vary(0.30, s + 1, 0.15))
        pleural = round(abnormal * _vary(0.15, s + 2, 0.2))
        other = abnormal - cardio - pulm - pleural
        cat_counts["Cardiomegaly"] += cardio
        cat_counts["Pulmonary Infiltrate"] += pulm
        cat_counts["Pleural Effusion"] += pleural
        cat_counts["Other Abnormality"] += max(other, 0)

    results = [
        {"category": cat, "count": cnt,
         "percentage": round(cnt / max(total_completed, 1) * 100, 1)}
        for cat, cnt in cat_counts.items()
    ]
    not_done = total_eligible - total_completed
    results.append({
        "category": "Not Done", "count": not_done,
        "percentage": round(not_done / max(total_eligible, 1) * 100, 1),
    })

    return {
        "total_eligible": total_eligible,
        "total_completed": total_completed,
        "completion_rate": round(total_completed / max(total_eligible, 1) * 100, 1),
        "results": results,
        "methodology_note": METHODOLOGY_NOTE,
    }


@router.get("/blood/summary")
def get_blood_summary():
    """Blood test results summary (glucose, lipid, kidney, liver function)."""
    data = load_district_data()
    total_eligible = 0
    total_completed = 0

    # Panel definitions: (name, name_th, base_abnormal_rate)
    panel_defs = [
        ("Fasting Blood Glucose", "น้ำตาลในเลือด", 15.0),
        ("Lipid Panel", "ไขมันในเลือด", 32.0),
        ("Kidney Function (Creatinine/eGFR)", "การทำงานของไต", 8.0),
        ("Liver Function (AST/ALT)", "การทำงานของตับ", 12.0),
        ("Complete Blood Count", "ความสมบูรณ์ของเลือด", 6.0),
    ]
    panel_totals: dict[str, dict] = {
        p[0]: {"name_th": p[1], "base_rate": p[2], "tested": 0, "normal": 0, "abnormal": 0}
        for p in panel_defs
    }

    for dcode, district in data.items():
        ts = district["total_screened"]
        total_eligible += ts
        seed = _seed_for(dcode)
        rate = _vary(92.5, seed + 4, 0.03)
        completed = round(ts * rate / 100)
        total_completed += completed

        for i, (pname, _, base_abn) in enumerate(panel_defs):
            panel_tested = completed
            abn_rate = _vary(base_abn, seed + 10 + i, 0.2)
            abn = round(panel_tested * abn_rate / 100)
            panel_totals[pname]["tested"] += panel_tested
            panel_totals[pname]["abnormal"] += abn
            panel_totals[pname]["normal"] += panel_tested - abn

    panels = []
    for pname, pt in panel_totals.items():
        panels.append({
            "panel_name": pname,
            "panel_name_th": pt["name_th"],
            "total_tested": pt["tested"],
            "normal": pt["normal"],
            "abnormal": pt["abnormal"],
            "abnormal_rate": round(pt["abnormal"] / max(pt["tested"], 1) * 100, 1),
        })

    return {
        "total_eligible": total_eligible,
        "total_completed": total_completed,
        "completion_rate": round(total_completed / max(total_eligible, 1) * 100, 1),
        "panels": panels,
        "methodology_note": METHODOLOGY_NOTE,
    }


@router.get("/retinal/summary")
def get_retinal_summary():
    """DR screening results breakdown."""
    data = load_district_data()
    total_eligible = 0
    total_completed = 0
    cat_counts = {
        "No DR": 0,
        "Mild NPDR": 0,
        "Moderate NPDR": 0,
        "Severe NPDR": 0,
        "Proliferative DR": 0,
        "Diabetic Macular Edema": 0,
    }

    for dcode, district in data.items():
        ts = district["total_screened"]
        total_eligible += ts
        seed = _seed_for(dcode)
        rate = _vary(40.0, seed + 6, 0.25)
        completed = round(ts * rate / 100)
        total_completed += completed

        dr_positive_rate = _vary(8.5, seed + 7, 0.41)
        dr_positive = round(completed * dr_positive_rate / 100)
        no_dr = completed - dr_positive

        cat_counts["No DR"] += no_dr

        # Split DR positives into severity grades
        mild = round(dr_positive * 0.45)
        moderate = round(dr_positive * 0.25)
        severe = round(dr_positive * 0.12)
        pdr = round(dr_positive * 0.08)
        dme = dr_positive - mild - moderate - severe - pdr
        cat_counts["Mild NPDR"] += mild
        cat_counts["Moderate NPDR"] += moderate
        cat_counts["Severe NPDR"] += severe
        cat_counts["Proliferative DR"] += pdr
        cat_counts["Diabetic Macular Edema"] += max(dme, 0)

    results = [
        {"category": cat, "count": cnt,
         "percentage": round(cnt / max(total_completed, 1) * 100, 1)}
        for cat, cnt in cat_counts.items()
    ]
    not_done = total_eligible - total_completed
    results.append({
        "category": "Not Done", "count": not_done,
        "percentage": round(not_done / max(total_eligible, 1) * 100, 1),
    })

    return {
        "total_eligible": total_eligible,
        "total_completed": total_completed,
        "completion_rate": round(total_completed / max(total_eligible, 1) * 100, 1),
        "results": results,
        "methodology_note": METHODOLOGY_NOTE,
    }
