"""Disease Control router — screening coverage, NCD cascade, repeat screening,
disease progression, referral outcome, treatment compliance."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from database import execute_query, execute_scalar
from security import enforce_k_anonymity, suppress_scalar_if_small, K_ANONYMITY_THRESHOLD

router = APIRouter(prefix="/api/v2/disease-control", tags=["Disease Control"])

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

@router.get("/screening-coverage")
def screening_coverage(zone_code: Optional[str] = Query(None)):
    """Screening coverage rate per district (screened / population)."""
    conditions = []
    params = []
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT d.dcode, d.name_th, d.zone_code, d.population,
               COALESCE(SUM(s.total_screened), 0) AS screened,
               ROUND(100.0 * COALESCE(SUM(s.total_screened), 0)
                     / NULLIF(d.population, 0), 2) AS coverage_pct
        FROM ref_districts d
        LEFT JOIN summary_district_disease s ON d.dcode = s.district_code
        {where}
        GROUP BY d.dcode, d.name_th, d.zone_code, d.population
        ORDER BY coverage_pct DESC
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("screened") or 0) >= K_ANONYMITY_THRESHOLD]
    return {"data": rows}


@router.get("/ncd-cascade")
def ncd_cascade(disease: str = Query("diabetes")):
    """NCD cascade: screening -> risk -> diagnosis -> treatment."""
    _validate_disease_key(disease)
    dk = DISEASE_KEYS[disease]

    total_screened = execute_scalar(
        "SELECT COALESCE(SUM(total_screened), 0) FROM summary_district_disease"
    ) or 0

    risk_col = dk.get("risk")
    found_col = dk.get("found")

    at_risk = None
    diagnosed = None

    if risk_col:
        at_risk = execute_scalar(
            f"SELECT COALESCE(SUM({risk_col}_count), 0) FROM summary_district_disease"
        ) or 0
    if found_col:
        diagnosed = execute_scalar(
            f"SELECT COALESCE(SUM({found_col}_count), 0) FROM summary_district_disease"
        ) or 0

    cascade = [
        {
            "step": "screened",
            "label_th": "คัดกรอง",
            "count": suppress_scalar_if_small(total_screened),
            "pct_of_screened": 100.0,
        },
    ]
    if at_risk is not None:
        cascade.append({
            "step": "at_risk",
            "label_th": "มีความเสี่ยง",
            "count": suppress_scalar_if_small(at_risk),
            "pct_of_screened": round(100.0 * at_risk / total_screened, 2) if total_screened else 0,
        })
    if diagnosed is not None:
        cascade.append({
            "step": "diagnosed",
            "label_th": "พบโรค",
            "count": suppress_scalar_if_small(diagnosed),
            "pct_of_screened": round(100.0 * diagnosed / total_screened, 2) if total_screened else 0,
        })

    # Treatment data not available in current schema
    cascade.append({
        "step": "treatment",
        "label_th": "ได้รับการรักษา",
        "count": None,
        "pct_of_screened": None,
        "note": "ข้อมูลการรักษายังไม่มีในระบบ",
    })

    return {"disease": disease, "cascade": cascade}


@router.get("/repeat-screening")
def repeat_screening(district: Optional[str] = Query(None)):
    """Visit frequency distribution: how many patients screened 1x, 2x, 3x+."""
    conditions = ["cancel_status IS DISTINCT FROM 1"]
    params: list = []
    if district:
        conditions.append("district_code = %s")
        params.append(district)
    where = "WHERE " + " AND ".join(conditions)

    rows = execute_query(f"""
        SELECT visit_count, COUNT(*) AS patient_count
        FROM (
            SELECT patient_id, COUNT(*) AS visit_count
            FROM raw_vitalsigns
            {where}
            GROUP BY patient_id
        ) sub
        GROUP BY visit_count
        ORDER BY visit_count
    """, tuple(params) or None)

    rows = enforce_k_anonymity(rows, count_field="patient_count")

    if not rows:
        return {"data_available": False, "message": "ไม่มีข้อมูลการตรวจซ้ำ"}

    return {"district": district, "data": rows}


@router.get("/progression")
def disease_progression(
    disease: str = Query("diabetes"),
    district: Optional[str] = Query(None),
):
    """Disease progression: for patients with 2+ visits, did risk increase/decrease?"""
    _validate_disease_key(disease)
    dk = DISEASE_KEYS[disease]
    bool_col = dk.get("risk") or dk.get("found")
    if not bool_col:
        return {"data_available": False, "message": f"ไม่มีคอลัมน์ risk/found สำหรับ '{disease}' — ใช้ /api/v2/summary/lab แทน"}

    conditions = ["cancel_status IS DISTINCT FROM 1"]
    params: list = []
    if district:
        conditions.append("district_code = %s")
        params.append(district)
    where = "WHERE " + " AND ".join(conditions)

    multi = execute_scalar(f"""
        SELECT COUNT(*) FROM (
            SELECT patient_id FROM raw_vitalsigns
            {where}
            GROUP BY patient_id HAVING COUNT(*) >= 2
        ) sub
    """, tuple(params) or None) or 0

    if multi < K_ANONYMITY_THRESHOLD:
        return {
            "data_available": False,
            "multi_visit_patients": int(multi),
            "message": "ข้อมูลการตรวจซ้ำไม่เพียงพอ ต้องมีผู้ป่วยที่ตรวจ >=2 ครั้ง อย่างน้อย 5 คน",
        }

    # Compute first vs last visit disease flag change
    rows = execute_query(f"""
        WITH ranked AS (
            SELECT patient_id, {bool_col},
                   ROW_NUMBER() OVER (PARTITION BY patient_id ORDER BY visit_date ASC, id ASC) AS rn_first,
                   ROW_NUMBER() OVER (PARTITION BY patient_id ORDER BY visit_date DESC, id DESC) AS rn_last
            FROM raw_vitalsigns
            {where}
        ),
        first_last AS (
            SELECT
                f.patient_id,
                f.{bool_col} AS first_flag,
                l.{bool_col} AS last_flag
            FROM (SELECT patient_id, {bool_col} FROM ranked WHERE rn_first = 1) f
            JOIN (SELECT patient_id, {bool_col} FROM ranked WHERE rn_last = 1) l
                ON f.patient_id = l.patient_id
            WHERE f.patient_id IN (
                SELECT patient_id FROM raw_vitalsigns {where}
                GROUP BY patient_id HAVING COUNT(*) >= 2
            )
        )
        SELECT
            COUNT(*) FILTER (WHERE first_flag AND NOT last_flag) AS improved,
            COUNT(*) FILTER (WHERE NOT first_flag AND last_flag) AS worsened,
            COUNT(*) FILTER (WHERE first_flag = last_flag) AS stable,
            COUNT(*) AS total
        FROM first_last
    """, tuple(params) or None)

    if not rows:
        return {"data_available": False, "message": "ไม่สามารถคำนวณ progression ได้"}

    r = rows[0]
    result = {
        "disease": disease,
        "district": district,
        "multi_visit_patients": int(multi),
    }
    for key in ("improved", "worsened", "stable", "total"):
        val = r.get(key) or 0
        result[key] = int(val) if int(val) >= K_ANONYMITY_THRESHOLD else None

    return result


@router.get("/referral-outcome")
def referral_outcome(zone_code: Optional[str] = Query(None)):
    """Referral analysis: how many patients were referred, by type."""
    total_ref = execute_scalar(
        "SELECT COUNT(*) FROM raw_vitalsigns WHERE referral_type IS NOT NULL AND cancel_status IS DISTINCT FROM 1"
    ) or 0

    if total_ref == 0:
        return {
            "data_available": False,
            "message": "ไม่มีข้อมูลการส่งต่อ (referral_type ว่างทั้งหมด) — ต้องรอข้อมูลจาก HDC",
        }

    conditions = ["v.referral_type IS NOT NULL", "v.cancel_status IS DISTINCT FROM 1"]
    params: list = []
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = "WHERE " + " AND ".join(conditions)

    rows = execute_query(f"""
        SELECT v.district_code, v.referral_type, COUNT(*) AS count
        FROM raw_vitalsigns v
        JOIN ref_districts d ON v.district_code = d.dcode
        {where}
        GROUP BY v.district_code, v.referral_type
        ORDER BY v.district_code, v.referral_type
    """, tuple(params) or None)

    rows = enforce_k_anonymity(rows, count_field="count")

    return {"zone_code": zone_code, "total_referrals": int(total_ref), "data": rows}


@router.get("/treatment-compliance")
def treatment_compliance(disease: str = Query("diabetes")):
    """Treatment compliance: active/inconsistent/self-med/abandoned per disease."""
    treatment_cols = {
        "diabetes": "dm_treatment",
        "hypertension": "hpt_treatment",
        "dyslipidemia": "dyslipidemia_treatment",
        "heart": "heart_treatment",
        "kidney": "kidney_treatment",
        "stroke": "stroke_treatment",
    }
    col = treatment_cols.get(disease)
    if not col:
        return {
            "data_available": False,
            "message": f"ไม่มีข้อมูล treatment สำหรับ {disease}. รองรับ: {sorted(treatment_cols.keys())}",
        }

    total = execute_scalar(
        f'SELECT COUNT(*) FROM raw_homehealth WHERE "{col}" IS NOT NULL'
    ) or 0

    if total == 0:
        return {
            "data_available": False,
            "message": f"ไม่มีข้อมูลสถานะการรักษา {disease} (ว่างทั้งหมด) — ต้องรอข้อมูลจาก HDC",
        }

    rows = execute_query(f"""
        SELECT "{col}" AS treatment_status, COUNT(*) AS count
        FROM raw_homehealth WHERE "{col}" IS NOT NULL
        GROUP BY "{col}" ORDER BY "{col}"
    """)

    labels = {1: "รับการรักษาอยู่", 2: "รักษาไม่สม่ำเสมอ", 3: "ซื้อยาทานเอง", 4: "ไม่รักษา"}
    result = []
    for r in rows:
        status = r.get("treatment_status")
        if (r.get("count") or 0) >= K_ANONYMITY_THRESHOLD:
            result.append({
                "status_code": status,
                "status_label": labels.get(status, str(status)),
                "count": r["count"],
            })

    return {"disease": disease, "total_with_data": int(total), "data": result}
