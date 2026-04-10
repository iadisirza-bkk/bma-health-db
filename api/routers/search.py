"""Search router — district search and ranking by disease prevalence."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from database import execute_query
from security import K_ANONYMITY_THRESHOLD

router = APIRouter(prefix="/api/v2/search", tags=["Search"])

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


def _search_districts_lab(disease: str, min_pct, max_pct, sort_by, limit):
    """Search districts for lab-based diseases (ckd, anemia)."""
    pct_col = "pct_ckd" if disease == "ckd" else "pct_anemia"
    conditions = [f"l.{pct_col} IS NOT NULL"]
    params: list = []

    if min_pct is not None:
        conditions.append(f"l.{pct_col} >= %s")
        params.append(min_pct)
    if max_pct is not None:
        conditions.append(f"l.{pct_col} <= %s")
        params.append(max_pct)

    where = "WHERE " + " AND ".join(conditions)
    order = f"l.{pct_col} DESC" if "desc" in sort_by else f"l.{pct_col} ASC"

    rows = execute_query(f"""
        SELECT
          l.district_code,
          d.name_th AS district_name,
          d.zone_code,
          l.total_lab_patients,
          l.{pct_col} AS disease_pct
        FROM summary_district_lab l
        JOIN ref_districts d ON l.district_code = d.dcode
        {where}
        ORDER BY {order}
        LIMIT %s
    """, tuple(params + [limit]))

    rows = [r for r in rows if (r.get("total_screened") or r.get("total_lab_patients") or 0) >= K_ANONYMITY_THRESHOLD]
    return {"disease": disease, "results": rows}


# =========================================================================== #
# Endpoints
# =========================================================================== #

@router.get("/districts")
def search_districts(
    disease: str = Query(..., description="Disease key to rank by"),
    min_pct: Optional[float] = Query(None, ge=0, le=100),
    max_pct: Optional[float] = Query(None, ge=0, le=100),
    sort_by: str = Query("pct_desc", description="pct_desc | pct_asc | count_desc | count_asc"),
    limit: int = Query(50, ge=1, le=200),
):
    """Search and rank districts by disease prevalence."""
    _validate_disease_key(disease)

    dk = DISEASE_KEYS[disease]

    # Determine which columns to use
    count_col = None
    pct_col = dk.get("pct")

    if dk.get("risk"):
        count_col = dk["risk"].replace("risk_", "risk_") + "_count"
    elif dk.get("found"):
        count_col = dk["found"].replace("found_", "found_") + "_count"

    # For diseases without a precomputed pct, compute inline
    if not pct_col and count_col:
        pct_expr = f"ROUND(100.0 * s.{count_col} / NULLIF(s.total_screened, 0), 2)"
    elif pct_col:
        pct_expr = f"s.{pct_col}"
    else:
        # ckd / anemia -- use lab view
        return _search_districts_lab(disease, min_pct, max_pct, sort_by, limit)

    conditions: list[str] = ["s.total_screened > 0"]
    params: list = []

    if min_pct is not None:
        conditions.append(f"{pct_expr} >= %s")
        params.append(min_pct)
    if max_pct is not None:
        conditions.append(f"{pct_expr} <= %s")
        params.append(max_pct)

    where = "WHERE " + " AND ".join(conditions)

    order_map = {
        "pct_desc": f"{pct_expr} DESC",
        "pct_asc": f"{pct_expr} ASC",
        "count_desc": f"s.{count_col} DESC" if count_col else f"{pct_expr} DESC",
        "count_asc": f"s.{count_col} ASC" if count_col else f"{pct_expr} ASC",
    }
    order = order_map.get(sort_by, f"{pct_expr} DESC")

    rows = execute_query(f"""
        SELECT
          s.district_code, s.district_name, s.zone_code,
          s.total_screened,
          {"s." + count_col if count_col else "0"} AS disease_count,
          {pct_expr} AS disease_pct
        FROM summary_district_disease s
        {where}
        ORDER BY {order}
        LIMIT %s
    """, tuple(params + [limit]))

    rows = [r for r in rows if (r.get("total_screened") or r.get("total_lab_patients") or 0) >= K_ANONYMITY_THRESHOLD]
    return {"disease": disease, "results": rows}
