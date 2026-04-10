"""Districts router — list, detail, disease detail."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from database import execute_query
from security import enforce_k_anonymity, K_ANONYMITY_THRESHOLD

router = APIRouter(prefix="/api/v2/summary", tags=["Districts"])

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
# List districts
# =========================================================================== #

@router.get("/districts")
def list_districts(zone_code: Optional[str] = Query(None)):
    """List districts, optionally filtered by zone_code."""
    sql = """
        SELECT
          s.district_code, s.district_name, s.zone_code, s.total_screened,
          s.risk_dm_count, s.pct_risk_dm,
          s.risk_hpt_count, s.pct_risk_hpt,
          s.risk_cvd_count, s.pct_risk_cvd,
          s.found_obesity_count,
          s.found_dyslipidemia_count,
          s.found_stroke_count
        FROM summary_district_disease s
    """
    params: tuple = ()
    if zone_code:
        sql += " WHERE s.zone_code = %s"
        params = (zone_code,)
    sql += " ORDER BY s.district_code"
    rows = execute_query(sql, params or None)
    rows = [r for r in rows if (r.get("total_screened") or 0) >= K_ANONYMITY_THRESHOLD]
    return rows


# =========================================================================== #
# District detail
# =========================================================================== #

@router.get("/districts/{dcode}")
def district_detail(dcode: str):
    """Full district detail: diseases, lab, mental health, demographics."""
    disease = execute_query(
        """SELECT district_code, zone_code, district_name, total_screened,
                  risk_dm_count, pct_risk_dm, risk_hpt_count, pct_risk_hpt,
                  risk_cvd_count, pct_risk_cvd, risk_bmi_count,
                  found_dm_count, pct_found_dm, found_hpt_count, pct_found_hpt,
                  found_cvd_count, pct_found_cvd,
                  found_obesity_count, found_dyslipidemia_count, found_stroke_count
           FROM summary_district_disease WHERE district_code = %s""", (dcode,)
    )
    if not disease:
        raise HTTPException(status_code=404, detail="District not found")
    if (disease[0].get("total_screened") or 0) < K_ANONYMITY_THRESHOLD:
        raise HTTPException(status_code=403, detail="Data suppressed for privacy (k-anonymity)")

    lab = execute_query(
        """SELECT district_code, total_lab_patients,
                  avg_hemoglobin, avg_hematocrit, avg_fbs,
                  avg_cholesterol, avg_triglyceride, avg_hdl, avg_ldl,
                  avg_creatinine, avg_egfr, avg_uric_acid, avg_sgot, avg_sgpt,
                  pct_anemia, pct_ckd
           FROM summary_district_lab WHERE district_code = %s""", (dcode,)
    )
    mental = execute_query(
        """SELECT district_code, total_screened,
                  pct_depression_risk, pct_phq9_moderate, pct_high_stress
           FROM summary_district_mental WHERE district_code = %s""", (dcode,)
    )
    demographics = execute_query(
        """SELECT district_code, total_respondents,
                  edu_none, edu_primary, edu_secondary, edu_high_school,
                  edu_vocational, edu_bachelor, edu_postgrad,
                  occ_government, occ_private, occ_self_employed,
                  occ_agriculture, occ_unemployed, occ_student, occ_retired,
                  priv_ucs, priv_sso, priv_csmbs, priv_other,
                  house_owned, house_rented, house_condo, house_other
           FROM summary_district_demographics WHERE district_code::text = %s""", (dcode,)
    )

    return {
        "disease": disease[0] if disease else None,
        "lab_summary": lab[0] if lab else None,
        "mental_health": mental[0] if mental else None,
        "demographics": demographics[0] if demographics else None,
    }


# =========================================================================== #
# District disease detail
# =========================================================================== #

@router.get("/districts/{dcode}/disease/{disease_key}")
def district_disease_detail(dcode: str, disease_key: str):
    """Disease detail for a district with risk factor breakdown."""
    _validate_disease_key(disease_key)

    # Base disease row
    disease = execute_query(
        """SELECT district_code, zone_code, district_name, total_screened,
                  risk_dm_count, pct_risk_dm, risk_hpt_count, pct_risk_hpt,
                  risk_cvd_count, pct_risk_cvd, risk_bmi_count,
                  found_dm_count, pct_found_dm, found_hpt_count, pct_found_hpt,
                  found_cvd_count, pct_found_cvd,
                  found_obesity_count, found_dyslipidemia_count, found_stroke_count
           FROM summary_district_disease WHERE district_code = %s""", (dcode,)
    )
    if not disease:
        raise HTTPException(status_code=404, detail="District not found")

    # Risk factor breakdown from summary_district_risk_factors
    # Build the disease-specific filter
    dk = DISEASE_KEYS[disease_key]
    risk_col = dk.get("risk")
    found_col = dk.get("found")

    # For ckd and anemia we pull from lab view instead
    if disease_key in ("ckd", "anemia"):
        lab = execute_query(
            """SELECT district_code, total_lab_patients,
                      avg_hemoglobin, avg_hematocrit, avg_fbs,
                      avg_cholesterol, avg_triglyceride, avg_hdl, avg_ldl,
                      avg_creatinine, avg_egfr, avg_uric_acid, avg_sgot, avg_sgpt,
                      pct_anemia, pct_ckd
               FROM summary_district_lab WHERE district_code = %s""", (dcode,)
        )
        return {
            "district_code": dcode,
            "disease_key": disease_key,
            "source": "lab",
            "lab_summary": lab[0] if lab else None,
        }

    # For diseases tracked in vitalsigns, query risk factor breakdown
    # We query summary_district_risk_factors grouped by sex and age_group
    rf_rows = execute_query("""
        SELECT
          sex, age_group, smoking, exercise,
          SUM(patient_count) AS patient_count,
          ROUND(AVG(avg_sbp)::numeric, 1) AS avg_sbp,
          ROUND(AVG(avg_dbp)::numeric, 1) AS avg_dbp,
          ROUND(AVG(avg_bmi)::numeric, 1) AS avg_bmi
        FROM summary_district_risk_factors
        WHERE district_code = %s
        GROUP BY sex, age_group, smoking, exercise
        ORDER BY sex, age_group
    """, (dcode,))

    # Enforce k-anonymity
    rf_rows = enforce_k_anonymity(rf_rows, count_field="patient_count")

    return {
        "district_code": dcode,
        "disease_key": disease_key,
        "disease_summary": disease[0],
        "risk_factor_breakdown": rf_rows,
    }
