"""Trends router — screening and disease time series."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from database import execute_query
from security import K_ANONYMITY_THRESHOLD

router = APIRouter(prefix="/api/v2/trends", tags=["Trends"])

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


_VALID_GRANULARITY = {"monthly", "quarterly"}


def _date_trunc_expr(granularity: str) -> str:
    if granularity == "quarterly":
        return "DATE_TRUNC('quarter', visit_date)"
    return "DATE_TRUNC('month', visit_date)"


# =========================================================================== #
# Endpoints
# =========================================================================== #

@router.get("/screening")
def trends_screening(
    granularity: str = Query("monthly"),
    zone_code: Optional[str] = Query(None),
):
    """Time series of screening counts from raw_vitalsigns."""
    if granularity not in _VALID_GRANULARITY:
        raise HTTPException(status_code=400, detail=f"granularity must be one of {sorted(_VALID_GRANULARITY)}")

    trunc = _date_trunc_expr(granularity)
    conditions = ["v.cancel_status IS DISTINCT FROM 1", "v.visit_date IS NOT NULL"]
    params: list = []

    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)

    where = "WHERE " + " AND ".join(conditions)

    rows = execute_query(f"""
        SELECT
          {trunc} AS period,
          COUNT(DISTINCT v.patient_id) AS screened_count
        FROM raw_vitalsigns v
        JOIN ref_districts d ON v.district_code = d.dcode
        {where}
        GROUP BY period
        ORDER BY period
    """, tuple(params) or None)

    # Enforce k-anonymity: suppress periods with fewer than K people
    rows = [r for r in rows if (r.get("screened_count") or 0) >= K_ANONYMITY_THRESHOLD]

    # Convert period to string
    for r in rows:
        if r.get("period"):
            r["period"] = r["period"].strftime("%Y-%m-%d") if hasattr(r["period"], "strftime") else str(r["period"])

    return {"granularity": granularity, "zone_code": zone_code, "data": rows}


@router.get("/disease/{disease_key}")
def trends_disease(
    disease_key: str,
    district: Optional[str] = Query(None),
    granularity: str = Query("monthly"),
):
    """Time series of disease prevalence from raw_vitalsigns."""
    _validate_disease_key(disease_key)
    if granularity not in _VALID_GRANULARITY:
        raise HTTPException(status_code=400, detail=f"granularity must be one of {sorted(_VALID_GRANULARITY)}")

    dk = DISEASE_KEYS[disease_key]
    # For ckd/anemia we cannot get trends from vitalsigns easily
    if disease_key in ("ckd", "anemia"):
        raise HTTPException(
            status_code=400,
            detail=f"Trend data not available for '{disease_key}'. Use /api/v2/summary/lab instead.",
        )

    # Build the boolean column to track
    risk_col = dk.get("risk")
    found_col = dk.get("found")
    bool_col = risk_col or found_col
    if not bool_col:
        raise HTTPException(status_code=400, detail=f"No trackable column for '{disease_key}'")

    trunc = _date_trunc_expr(granularity)
    conditions = ["v.cancel_status IS DISTINCT FROM 1", "v.visit_date IS NOT NULL"]
    params: list = []

    if district:
        conditions.append("v.district_code = %s")
        params.append(district)

    where = "WHERE " + " AND ".join(conditions)

    rows = execute_query(f"""
        SELECT
          {trunc} AS period,
          COUNT(DISTINCT v.patient_id) AS total_screened,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.{bool_col}) AS at_risk_count,
          ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.{bool_col})
                / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct
        FROM raw_vitalsigns v
        {where}
        GROUP BY period
        ORDER BY period
    """, tuple(params) or None)

    # Enforce k-anonymity: suppress periods with fewer than K people
    rows = [r for r in rows if (r.get("total_screened") or 0) >= K_ANONYMITY_THRESHOLD]

    for r in rows:
        if r.get("period"):
            r["period"] = r["period"].strftime("%Y-%m-%d") if hasattr(r["period"], "strftime") else str(r["period"])

    return {"disease_key": disease_key, "granularity": granularity, "district": district, "data": rows}
