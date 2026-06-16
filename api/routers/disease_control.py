"""Disease Control router — screening coverage, NCD cascade, repeat screening,
disease progression, referral outcome, treatment compliance.
Refactored for bma_med.* schema."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from database import execute_query, execute_scalar
from security import enforce_k_anonymity, suppress_scalar_if_small, K_ANONYMITY_THRESHOLD

router = APIRouter(prefix="/api/v2/disease-control", tags=["Disease Control"])

# --------------------------------------------------------------------------- #
# Reusable UNION across the two main vitalsigns sources (app1 + portal).
# Subselect aliased as `v` so handlers can plug it in as `FROM ({_VISITS_UNION_SQL}) v`.
# --------------------------------------------------------------------------- #
_VISITS_UNION_SQL = """
SELECT row_id AS id, patient_id, vstdate AS visit_date,
       hbpn AS sbp, lbpn AS dbp,
       record_cancelled AS cancel_status,
       dm AS found_dm, hpt AS found_hpt, cdvcl AS found_cvd,
       stroke AS found_stroke, fat AS found_obesity, chltr AS found_dyslipidemia,
       riskdm AS risk_dm, riskhpt AS risk_hpt,
       riskcdvcl AS risk_cvd, riskbmi AS risk_bmi
FROM bma_med.app1_vitalsignslf
UNION ALL
SELECT row_id AS id, patient_id, vstdate AS visit_date,
       hbpn AS sbp, lbpn AS dbp,
       record_cancelled AS cancel_status,
       dm AS found_dm, hpt AS found_hpt, cdvcl AS found_cvd,
       stroke AS found_stroke, fat AS found_obesity, chltr AS found_dyslipidemia,
       riskdm AS risk_dm, riskhpt AS risk_hpt,
       riskcdvcl AS risk_cvd, riskbmi AS risk_bmi
FROM bma_med.portal_vitalsignslf
"""

# Homevisit join lookup for district_code (lives in homevisit in new schema,
# not in vitalsignslf as it did in raw_*)
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

    # Defense in depth — DISEASE_KEYS is hardcoded so risk_col/found_col can
    # only be one of a known set, but assert before f-string interpolation so
    # any future change to DISEASE_KEYS that adds an attacker-controlled
    # value still fails closed instead of yielding SQL injection.
    _ALLOWED_RISK_COLS = {dk_v["risk"] for dk_v in DISEASE_KEYS.values() if dk_v.get("risk")}
    _ALLOWED_FOUND_COLS = {dk_v["found"] for dk_v in DISEASE_KEYS.values() if dk_v.get("found")}

    at_risk = None
    diagnosed = None

    if risk_col:
        assert risk_col in _ALLOWED_RISK_COLS, risk_col
        at_risk = execute_scalar(
            f"SELECT COALESCE(SUM({risk_col}_count), 0) FROM summary_district_disease"
        ) or 0
    if found_col:
        assert found_col in _ALLOWED_FOUND_COLS, found_col
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
    """Visit frequency distribution: how many patients screened 1x, 2x, 3x+.

    Reads from public.mv_visit_resolved (api_user role has no direct SELECT on
    bma_med.* raw tables — see migration 200 grants). Each row in the MV is one
    visit; home_district_code is pre-resolved from homevisit, so the optional
    district filter is a single WHERE clause instead of a homevisit JOIN.
    """
    params: list = []
    district_filter = ""
    if district:
        district_filter = " AND home_district_code = %s"
        params.append(district)

    rows = execute_query(f"""
        SELECT visit_count, COUNT(*) AS patient_count
        FROM (
            SELECT patient_id, COUNT(*) AS visit_count
            FROM public.mv_visit_resolved
            WHERE cancel_status IS DISTINCT FROM 1
              AND is_dedup_kept = TRUE{district_filter}
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

    params: list = []
    district_join = ""
    district_filter = ""
    if district:
        district_join = f"JOIN ({_HOMEVISIT_DISTRICT_SQL}) hv ON v.patient_id = hv.patient_id"
        district_filter = " AND hv.district_code = %s"
        params.append(district)

    multi = execute_scalar(f"""
        SELECT COUNT(*) FROM (
            SELECT v.patient_id FROM ({_VISITS_UNION_SQL}) v
            {district_join}
            WHERE v.cancel_status IS DISTINCT FROM 1{district_filter}
            GROUP BY v.patient_id HAVING COUNT(*) >= 2
        ) sub
    """, tuple(params) or None) or 0

    if multi < K_ANONYMITY_THRESHOLD:
        return {
            "data_available": False,
            "multi_visit_patients": int(multi),
            "message": "ข้อมูลการตรวจซ้ำไม่เพียงพอ ต้องมีผู้ป่วยที่ตรวจ >=2 ครั้ง อย่างน้อย 5 คน",
        }

    # Compute first vs last visit disease flag change
    # Note: smallint 0/1 in new schema (vs bool in old). Use `= 1` semantics.
    progression_params = list(params) + list(params) + list(params)
    rows = execute_query(f"""
        WITH visits AS (
            SELECT v.patient_id, v.{bool_col}, v.visit_date, v.id
            FROM ({_VISITS_UNION_SQL}) v
            {district_join}
            WHERE v.cancel_status IS DISTINCT FROM 1{district_filter}
        ),
        ranked AS (
            SELECT patient_id, {bool_col},
                   ROW_NUMBER() OVER (PARTITION BY patient_id ORDER BY visit_date ASC, id ASC) AS rn_first,
                   ROW_NUMBER() OVER (PARTITION BY patient_id ORDER BY visit_date DESC, id DESC) AS rn_last
            FROM visits
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
                SELECT v.patient_id FROM ({_VISITS_UNION_SQL}) v
                {district_join}
                WHERE v.cancel_status IS DISTINCT FROM 1{district_filter}
                GROUP BY v.patient_id HAVING COUNT(*) >= 2
            )
        )
        SELECT
            COUNT(*) FILTER (WHERE first_flag = 1 AND (last_flag = 0 OR last_flag IS NULL)) AS improved,
            COUNT(*) FILTER (WHERE (first_flag = 0 OR first_flag IS NULL) AND last_flag = 1) AS worsened,
            COUNT(*) FILTER (WHERE first_flag IS NOT DISTINCT FROM last_flag) AS stable,
            COUNT(*) AS total
        FROM first_last
    """, tuple(progression_params) or None)

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
    # TODO: bma_med equivalent unclear — referral_type column not in vitalsignslf in new schema.
    # The factsheet rfprvlg/rfover/rffw/rfspc/rfoth flags exist but aren't unified into one
    # "referral_type" enum. Returning empty until mapping is finalized.
    return {
        "data_available": False,
        "message": "ไม่มีข้อมูลการส่งต่อ (referral_type ว่างทั้งหมด) — ต้องรอข้อมูลจาก HDC",
    }


@router.get("/treatment-compliance")
def treatment_compliance(disease: str = Query("diabetes")):
    """Treatment compliance: active/inconsistent/self-med/abandoned per disease."""
    # New schema treatment columns live in homehealth: dmrs, hptrs, chltrrs, hrtrs, kidneyrs, strokers
    treatment_cols = {
        "diabetes": "dmrs",
        "hypertension": "hptrs",
        "dyslipidemia": "chltrrs",
        "heart": "hrtrs",
        "kidney": "kidneyrs",
        "stroke": "strokers",
    }
    col = treatment_cols.get(disease)
    if not col:
        return {
            "data_available": False,
            "message": f"ไม่มีข้อมูล treatment สำหรับ {disease}. รองรับ: {sorted(treatment_cols.keys())}",
        }

    total = execute_scalar(f"""
        SELECT COUNT(*) FROM (
            SELECT {col} FROM bma_med.app1_homehealth WHERE {col} IS NOT NULL
            UNION ALL
            SELECT {col} FROM bma_med.portal_homehealth WHERE {col} IS NOT NULL
        ) t
    """) or 0

    if total == 0:
        return {
            "data_available": False,
            "message": f"ไม่มีข้อมูลสถานะการรักษา {disease} (ว่างทั้งหมด) — ต้องรอข้อมูลจาก HDC",
        }

    rows = execute_query(f"""
        SELECT {col} AS treatment_status, COUNT(*) AS count
        FROM (
            SELECT {col} FROM bma_med.app1_homehealth WHERE {col} IS NOT NULL
            UNION ALL
            SELECT {col} FROM bma_med.portal_homehealth WHERE {col} IS NOT NULL
        ) t
        GROUP BY {col} ORDER BY {col}
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
