"""
BMA Health Database -- Summary API v2

Serves AGGREGATE / SUMMARY health data only.
  - NO individual records
  - NO PII (idcard_hash, patient_id, staff_code never exposed)
  - k-anonymity >= 5 enforced on filtered queries
  - API key required (X-API-Key header)
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from database import execute_query, execute_scalar, close_pool
from security import (
    APIKeyMiddleware,
    RateLimitMiddleware,
    add_cors,
    enforce_k_anonymity,
    suppress_scalar_if_small,
    K_ANONYMITY_THRESHOLD,
)
from admin import router as admin_router
from config import validate_production_config

import os

_audit_logger = logging.getLogger("bma.audit")
_audit_logger.setLevel(logging.INFO)
# Add handler if none exists
if not _audit_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        '%(asctime)s AUDIT %(message)s'
    ))
    _audit_logger.addHandler(_handler)


class AuditMiddleware(BaseHTTPMiddleware):
    """Log API access for audit trail. Never logs PII."""

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        duration = time.time() - start

        # Skip health checks and static assets from audit
        path = request.url.path
        if path in ("/health", "/docs", "/redoc", "/openapi.json"):
            return response

        client_ip = request.client.host if request.client else "unknown"
        _audit_logger.info(
            "method=%s path=%s status=%d duration=%.3fs ip=%s",
            request.method, path, response.status_code, duration, client_ip,
        )
        return response

validate_production_config()

# --------------------------------------------------------------------------- #
# App lifecycle
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    close_pool()


_is_production = os.getenv("ENVIRONMENT", "development") == "production"

app = FastAPI(
    title="BMA Health Summary API",
    version="2.0.0",
    description="Aggregate health screening data for Bangkok Metropolitan Administration. No PII.",
    lifespan=lifespan,
    docs_url=None if _is_production else "/docs",
    redoc_url=None if _is_production else "/redoc",
    openapi_url=None if _is_production else "/openapi.json",
)

# Middleware order: CORS -> Rate Limit -> API Key
add_cors(app)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(APIKeyMiddleware)
app.add_middleware(AuditMiddleware)

app.include_router(admin_router)

# Serve static files (fonts, logo)
from fastapi.staticfiles import StaticFiles
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

TARGET_SCREENED = 1_600_000

# --------------------------------------------------------------------------- #
# Valid disease keys
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
# Health check (no API key required)
# =========================================================================== #

@app.get("/health", tags=["System"])
def health_check():
    db_ok = False
    try:
        result = execute_scalar("SELECT 1")
        db_ok = result == 1
    except Exception:
        pass

    status = "ok" if db_ok else "degraded"
    return {
        "status": status,
        "database": "connected" if db_ok else "disconnected",
        "timestamp": datetime.utcnow().isoformat(),
    }


# =========================================================================== #
# Overview
# =========================================================================== #

@app.get("/api/v2/summary/overview", tags=["Summary"])
def overview():
    """Top-level screening overview with zone and disease breakdowns."""

    total = execute_scalar(
        "SELECT COALESCE(SUM(total_screened), 0) FROM summary_district_disease"
    ) or 0

    zone_count = execute_scalar("SELECT COUNT(*) FROM ref_health_zones") or 0
    district_count = execute_scalar("SELECT COUNT(*) FROM ref_districts") or 0

    last_updated = execute_scalar(
        "SELECT MAX(refreshed_at) FROM summary_district_disease"
    )

    # By zone
    by_zone = execute_query("""
        SELECT z.zone_code, z.name_th,
               COALESCE(SUM(s.total_screened), 0) AS total_screened
        FROM ref_health_zones z
        LEFT JOIN summary_district_disease s ON s.zone_code = z.zone_code
        GROUP BY z.zone_code, z.name_th
        ORDER BY z.zone_code
    """)

    # By disease (overall)
    disease_rows = execute_query("""
        SELECT
          SUM(total_screened)              AS total_screened,
          SUM(risk_dm_count)              AS diabetes,
          SUM(risk_hpt_count)             AS hypertension,
          SUM(risk_cvd_count)             AS cardiovascular,
          SUM(risk_bmi_count)             AS obesity,
          SUM(found_dyslipidemia_count)   AS dyslipidemia,
          SUM(found_stroke_count)         AS stroke
        FROM summary_district_disease
    """)
    d = disease_rows[0] if disease_rows else {}
    ts = d.get("total_screened") or 1
    by_disease = []
    for key in ("diabetes", "hypertension", "cardiovascular", "obesity", "dyslipidemia", "stroke"):
        cnt = d.get(key) or 0
        by_disease.append({
            "disease_key": key,
            "total_at_risk": cnt,
            "pct": round(100.0 * cnt / ts, 2) if ts else 0,
        })

    return {
        "total_screened": total,
        "target": TARGET_SCREENED,
        "zones_count": zone_count,
        "districts_count": district_count,
        "last_updated": str(last_updated) if last_updated else None,
        "by_zone": by_zone,
        "by_disease": by_disease,
    }


# =========================================================================== #
# Zones
# =========================================================================== #

@app.get("/api/v2/summary/zones", tags=["Zones"])
def list_zones():
    """All zones with screening totals and disease breakdown."""
    rows = execute_query("""
        SELECT
          z.zone_code, z.name_th, z.name_en,
          COUNT(DISTINCT s.district_code) AS district_count,
          COALESCE(SUM(s.total_screened), 0) AS total_screened,
          COALESCE(SUM(s.risk_dm_count), 0) AS diabetes,
          COALESCE(SUM(s.risk_hpt_count), 0) AS hypertension,
          COALESCE(SUM(s.risk_cvd_count), 0) AS cardiovascular,
          COALESCE(SUM(s.risk_bmi_count), 0) AS obesity,
          COALESCE(SUM(s.found_dyslipidemia_count), 0) AS dyslipidemia,
          COALESCE(SUM(s.found_stroke_count), 0) AS stroke
        FROM ref_health_zones z
        LEFT JOIN summary_district_disease s ON s.zone_code = z.zone_code
        GROUP BY z.zone_code, z.name_th, z.name_en
        ORDER BY z.zone_code
    """)
    result = []
    for r in rows:
        ts = r["total_screened"] or 1
        diseases = {}
        for dk in ("diabetes", "hypertension", "cardiovascular", "obesity", "dyslipidemia", "stroke"):
            cnt = r.pop(dk, 0) or 0
            diseases[dk] = {"count": cnt, "pct": round(100.0 * cnt / ts, 2)}
        r["diseases"] = diseases
        result.append(r)
    return result


@app.get("/api/v2/summary/zones/{zone_code}", tags=["Zones"])
def zone_detail(zone_code: str):
    """Single zone with its districts and disease data."""
    zone = execute_query(
        "SELECT zone_code, name_th, name_en FROM ref_health_zones WHERE zone_code = %s",
        (zone_code,),
    )
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")

    districts = execute_query("""
        SELECT
          s.district_code, s.district_name, s.total_screened,
          s.risk_dm_count, s.pct_risk_dm,
          s.risk_hpt_count, s.pct_risk_hpt,
          s.risk_cvd_count, s.pct_risk_cvd,
          s.risk_bmi_count,
          s.found_obesity_count,
          s.found_dyslipidemia_count,
          s.found_stroke_count
        FROM summary_district_disease s
        WHERE s.zone_code = %s
        ORDER BY s.district_code
    """, (zone_code,))

    districts = [d for d in districts if (d.get("total_screened") or 0) >= K_ANONYMITY_THRESHOLD]
    return {**zone[0], "districts": districts}


# =========================================================================== #
# Districts
# =========================================================================== #

@app.get("/api/v2/summary/districts", tags=["Districts"])
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


@app.get("/api/v2/summary/districts/{dcode}", tags=["Districts"])
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
           FROM summary_district_demographics WHERE district_code = %s""", (dcode,)
    )

    return {
        "disease": disease[0] if disease else None,
        "lab_summary": lab[0] if lab else None,
        "mental_health": mental[0] if mental else None,
        "demographics": demographics[0] if demographics else None,
    }


@app.get("/api/v2/summary/districts/{dcode}/disease/{disease_key}", tags=["Districts"])
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


# =========================================================================== #
# Filtered query (k-anonymity enforced)
# =========================================================================== #

@app.get("/api/v2/summary/filtered", tags=["Summary"])
def filtered_summary(
    district: Optional[str] = Query(None),
    sex: Optional[int] = Query(None),
    age_group: Optional[str] = Query(None),
    smoking: Optional[int] = Query(None),
    exercise: Optional[int] = Query(None),
):
    """Query risk factor summary with filters. k-anonymity enforced."""
    conditions = []
    params: list = []

    if district:
        conditions.append("district_code = %s")
        params.append(district)
    if sex is not None:
        conditions.append("sex = %s")
        params.append(sex)
    if age_group:
        conditions.append("age_group = %s")
        params.append(age_group)
    if smoking is not None:
        conditions.append("smoking = %s")
        params.append(smoking)
    if exercise is not None:
        conditions.append("exercise = %s")
        params.append(exercise)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT
          district_code, sex, age_group, smoking, exercise,
          SUM(patient_count)::int AS patient_count,
          ROUND(AVG(avg_sbp)::numeric, 1) AS avg_sbp,
          ROUND(AVG(avg_dbp)::numeric, 1) AS avg_dbp,
          ROUND(AVG(avg_weight_kg)::numeric, 1) AS avg_weight_kg,
          ROUND(AVG(avg_waist_cm)::numeric, 1) AS avg_waist_cm,
          ROUND(AVG(avg_bmi)::numeric, 1) AS avg_bmi
        FROM summary_district_risk_factors
        {where}
        GROUP BY district_code, sex, age_group, smoking, exercise
        ORDER BY district_code, sex, age_group
    """, tuple(params) or None)

    rows = enforce_k_anonymity(rows, count_field="patient_count")
    return {"filters_applied": {
        "district": district, "sex": sex, "age_group": age_group,
        "smoking": smoking, "exercise": exercise,
    }, "k_anonymity_threshold": K_ANONYMITY_THRESHOLD, "data": rows}


# =========================================================================== #
# Trends
# =========================================================================== #

_VALID_GRANULARITY = {"monthly", "quarterly"}


def _date_trunc_expr(granularity: str) -> str:
    if granularity == "quarterly":
        return "DATE_TRUNC('quarter', visit_date)"
    return "DATE_TRUNC('month', visit_date)"


@app.get("/api/v2/trends/screening", tags=["Trends"])
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


@app.get("/api/v2/trends/disease/{disease_key}", tags=["Trends"])
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


# =========================================================================== #
# Lab summary
# =========================================================================== #

@app.get("/api/v2/summary/lab", tags=["Summary"])
def lab_summary(
    dcode: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Lab summary, optionally filtered by district or zone."""
    conditions: list[str] = []
    params: list = []

    if dcode:
        conditions.append("l.district_code = %s")
        params.append(dcode)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT
          l.district_code,
          l.total_lab_patients,
          ROUND(l.avg_hemoglobin::numeric, 2) AS avg_hemoglobin,
          ROUND(l.avg_fbs::numeric, 2) AS avg_fbs,
          ROUND(l.avg_cholesterol::numeric, 2) AS avg_cholesterol,
          ROUND(l.avg_triglyceride::numeric, 2) AS avg_triglyceride,
          ROUND(l.avg_hdl::numeric, 2) AS avg_hdl,
          ROUND(l.avg_ldl::numeric, 2) AS avg_ldl,
          ROUND(l.avg_creatinine::numeric, 2) AS avg_creatinine,
          ROUND(l.avg_egfr::numeric, 2) AS avg_egfr,
          l.pct_anemia,
          l.pct_ckd
        FROM summary_district_lab l
        JOIN ref_districts d ON l.district_code = d.dcode
        {where}
        ORDER BY l.district_code
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_lab_patients") or 0) >= K_ANONYMITY_THRESHOLD]
    return rows


# =========================================================================== #
# Mental health summary
# =========================================================================== #

@app.get("/api/v2/summary/mental-health", tags=["Summary"])
def mental_health_summary(
    dcode: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Mental health screening summary."""
    conditions: list[str] = []
    params: list = []

    if dcode:
        conditions.append("m.district_code = %s")
        params.append(dcode)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT
          m.district_code,
          m.total_screened,
          m.pct_depression_risk,
          m.pct_phq9_moderate,
          m.pct_high_stress
        FROM summary_district_mental m
        JOIN ref_districts d ON m.district_code = d.dcode
        {where}
        ORDER BY m.district_code
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_screened") or 0) >= K_ANONYMITY_THRESHOLD]
    return rows


# =========================================================================== #
# Demographics summary
# =========================================================================== #

@app.get("/api/v2/summary/demographics", tags=["Summary"])
def demographics_summary(
    dcode: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Demographic breakdown by district."""
    conditions: list[str] = []
    params: list = []

    if dcode:
        conditions.append("dm.district_code = %s")
        params.append(dcode)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT
          dm.district_code,
          dm.total_respondents,
          dm.edu_none, dm.edu_primary, dm.edu_secondary,
          dm.edu_high_school, dm.edu_vocational, dm.edu_bachelor, dm.edu_postgrad,
          dm.occ_government, dm.occ_private, dm.occ_self_employed,
          dm.occ_agriculture, dm.occ_unemployed, dm.occ_student, dm.occ_retired,
          dm.priv_ucs, dm.priv_sso, dm.priv_csmbs, dm.priv_other,
          dm.house_owned, dm.house_rented, dm.house_condo, dm.house_other
        FROM summary_district_demographics dm
        JOIN ref_districts d ON dm.district_code = d.dcode
        {where}
        ORDER BY dm.district_code
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_respondents") or 0) >= K_ANONYMITY_THRESHOLD]
    return rows


# =========================================================================== #
# Search / rank districts
# =========================================================================== #

@app.get("/api/v2/search/districts", tags=["Search"])
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


# --------------------------------------------------------------------------- #
# Run with: uvicorn main:app --host 0.0.0.0 --port 8000
# --------------------------------------------------------------------------- #
