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


# =========================================================================== #
# Epidemiology
# =========================================================================== #

@app.get("/api/v2/epidemiology/age-group-prevalence", tags=["Epidemiology"])
def age_group_prevalence(
    district: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
    sex: Optional[int] = Query(None),
):
    """Disease prevalence by Thai lifecycle age group x sex x district."""
    conditions = []
    params = []
    if district:
        conditions.append("s.district_code = %s")
        params.append(district)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    if sex is not None:
        conditions.append("s.sex = %s")
        params.append(sex)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT s.district_code, s.sex, s.age_group, s.total_screened,
               s.risk_dm, s.risk_hpt, s.risk_cvd, s.risk_bmi,
               s.found_dm, s.found_hpt, s.found_cvd, s.found_stroke,
               s.found_obesity, s.found_dyslipidemia
        FROM summary_disease_age_sex s
        JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        ORDER BY s.age_group, s.sex
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_screened") or 0) >= K_ANONYMITY_THRESHOLD]
    return {"age_groups": ["วัยเรียน","วัยเริ่มทำงาน","วัยทำงาน","วัยกลางคน","วัยก่อนสูงอายุ","สูงวัย"], "data": rows}


@app.get("/api/v2/epidemiology/disease-lab-crosstab", tags=["Epidemiology"])
def disease_lab_crosstab(
    district: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Lab values stratified by disease status (positive vs negative)."""
    conditions = []
    params = []
    if district:
        conditions.append("s.district_code = %s")
        params.append(district)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT s.*
        FROM summary_lab_disease_cross s
        JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        ORDER BY s.district_code
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_patients") or 0) >= K_ANONYMITY_THRESHOLD]
    return {"data": rows}


# =========================================================================== #
# Monitoring
# =========================================================================== #

@app.get("/api/v2/monitoring/data-quality", tags=["Monitoring"])
def data_quality():
    """Data completeness report -- null rates per table per field."""
    tables = ["raw_patients", "raw_visits", "raw_vitalsigns", "raw_homevisit",
              "raw_homehealth", "raw_lab_results", "raw_lab_extended"]
    result = {}
    for table in tables:
        total = execute_scalar(f'SELECT COUNT(*) FROM "{table}"') or 0
        if total == 0:
            result[table] = {"total_rows": 0, "fields": {}}
            continue

        # Get column null counts
        cols = execute_query("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            AND column_name NOT IN ('id','created_at','updated_at')
            ORDER BY ordinal_position
        """, (table,))

        fields = {}
        for col in cols:
            cn = col["column_name"]
            null_count = execute_scalar(
                f'SELECT COUNT(*) FROM "{table}" WHERE "{cn}" IS NULL'
            ) or 0
            fields[cn] = {
                "null_count": int(null_count),
                "null_pct": round(100.0 * null_count / total, 1) if total > 0 else 0,
                "filled_pct": round(100.0 * (total - null_count) / total, 1) if total > 0 else 0,
            }

        result[table] = {"total_rows": int(total), "fields": fields}

    # Identify blocked fields (100% null)
    blocked = []
    for table, info in result.items():
        for field, stats in info.get("fields", {}).items():
            if stats["null_pct"] >= 100 and info["total_rows"] > 0:
                blocked.append({"table": table, "field": field, "note": f"ไม่มีข้อมูล {field} เลย"})

    return {"tables": result, "blocked_fields": blocked}


@app.get("/api/v2/monitoring/cleansing-report", tags=["Monitoring"])
def cleansing_report():
    """Data cleansing summary -- what was cleaned during import."""
    tables_info = {}

    for table in ["raw_patients", "raw_vitalsigns", "raw_lab_results", "raw_homevisit", "raw_homehealth", "raw_lab_extended"]:
        total = execute_scalar(f'SELECT COUNT(*) FROM "{table}"') or 0
        cancelled = 0
        if table != "raw_patients":
            cancelled = execute_scalar(f'SELECT COUNT(*) FROM "{table}" WHERE cancel_status = 1') or 0

        tables_info[table] = {
            "total_rows": int(total),
            "active_rows": int(total - cancelled),
            "cancelled_excluded": int(cancelled),
        }

    # Patient-specific quality
    null_birth = execute_scalar("SELECT COUNT(*) FROM raw_patients WHERE birth_year IS NULL") or 0
    null_sex = execute_scalar("SELECT COUNT(*) FROM raw_patients WHERE sex IS NULL") or 0
    tables_info["raw_patients"]["null_birth_year"] = int(null_birth)
    tables_info["raw_patients"]["null_sex"] = int(null_sex)

    # Vitalsigns-specific quality
    null_district = execute_scalar("SELECT COUNT(*) FROM raw_vitalsigns WHERE district_code IS NULL AND cancel_status = 0") or 0
    null_bp = execute_scalar("SELECT COUNT(*) FROM raw_vitalsigns WHERE (sbp IS NULL OR sbp = 0) AND cancel_status = 0") or 0
    tables_info["raw_vitalsigns"]["null_district_code"] = int(null_district)
    tables_info["raw_vitalsigns"]["null_bp"] = int(null_bp)

    # Last import info
    last_import = execute_query("""
        SELECT filename, file_type, status, started_at, rows_imported
        FROM import_history
        ORDER BY started_at DESC LIMIT 5
    """)

    # Blocked fields (100% null in active records)
    blocked = []
    checks = [
        ("raw_lab_results", "egfr", "eGFR (ค่าการทำงานของไต)"),
        ("raw_lab_results", "cervical_cancer_result", "มะเร็งปากมดลูก"),
        ("raw_lab_results", "colorectal_result", "มะเร็งลำไส้"),
        ("raw_homehealth", "food_preference_sweet", "ความชอบอาหารหวาน"),
        ("raw_homehealth", "dm_treatment", "สถานะการรักษาเบาหวาน"),
        ("raw_vitalsigns", "referral_type", "ประเภทการส่งต่อ"),
    ]
    for table, field, label in checks:
        total_t = execute_scalar(f'SELECT COUNT(*) FROM "{table}"') or 0
        if total_t > 0:
            filled = execute_scalar(f'SELECT COUNT(*) FROM "{table}" WHERE "{field}" IS NOT NULL') or 0
            if filled == 0:
                blocked.append({"table": table, "field": field, "label": label, "null_pct": 100.0})

    return {
        "tables": tables_info,
        "recent_imports": last_import,
        "blocked_fields": blocked,
    }


# =========================================================================== #
# Health Promotion
# =========================================================================== #

@app.get("/api/v2/promotion/bmi-distribution", tags=["Health Promotion"])
def bmi_distribution(
    district: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """BMI category distribution + waist circumference risk per district."""
    conditions = []
    params = []
    if district:
        conditions.append("s.district_code = %s")
        params.append(district)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT s.district_code, s.sex, s.total_measured,
               s.bmi_underweight, s.bmi_normal, s.bmi_overweight, s.bmi_obese, s.bmi_severely_obese,
               s.avg_bmi, s.total_waist_measured, s.avg_waist,
               s.male_waist_risk, s.female_waist_risk,
               s.avg_height, s.avg_weight
        FROM summary_bmi_waist s
        JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        ORDER BY s.district_code, s.sex
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_measured") or 0) >= K_ANONYMITY_THRESHOLD]

    return {
        "bmi_categories": {
            "underweight": {"label": "ผอม", "range": "< 18.5"},
            "normal": {"label": "ปกติ", "range": "18.5-22.9"},
            "overweight": {"label": "ท้วม", "range": "23-24.9"},
            "obese": {"label": "อ้วน", "range": "25-29.9"},
            "severely_obese": {"label": "อ้วนมาก", "range": "≥ 30"},
        },
        "data": rows,
    }


# =========================================================================== #
# Executive
# =========================================================================== #

@app.get("/api/v2/executive/headline-kpi", tags=["Executive"])
def headline_kpi():
    """3 headline KPIs for the Governor's press conference."""
    total = execute_scalar("SELECT COALESCE(SUM(total_screened), 0) FROM summary_district_disease") or 0
    target = TARGET_SCREENED
    coverage = round(100.0 * total / target, 2) if target > 0 else 0

    # Top disease by prevalence
    disease_rows = execute_query("""
        SELECT
            SUM(risk_dm_count) as diabetes, SUM(risk_hpt_count) as hypertension,
            SUM(risk_cvd_count) as cardiovascular, SUM(risk_bmi_count) as obesity,
            SUM(found_dyslipidemia_count) as dyslipidemia, SUM(found_stroke_count) as stroke,
            SUM(total_screened) as total
        FROM summary_district_disease
    """)

    disease_names_th = {
        "diabetes": "เบาหวาน", "hypertension": "ความดันโลหิตสูง",
        "cardiovascular": "หลอดเลือดหัวใจ", "obesity": "โรคอ้วน",
        "dyslipidemia": "ไขมันในเลือดผิดปกติ", "stroke": "หลอดเลือดสมอง",
    }

    d = disease_rows[0] if disease_rows else {}
    ts = d.get("total") or 1
    top_disease = None
    top_pct = 0
    for key in disease_names_th:
        cnt = d.get(key) or 0
        pct = round(100.0 * cnt / ts, 1) if ts else 0
        if pct > top_pct:
            top_pct = pct
            top_disease = {"key": key, "name_th": disease_names_th[key], "pct": pct, "count": cnt}

    # Most concerning district (highest total disease burden)
    worst = execute_query("""
        SELECT district_code, district_name, total_screened,
               risk_dm_count + risk_hpt_count + risk_cvd_count + risk_bmi_count AS total_risk
        FROM summary_district_disease
        WHERE total_screened >= 5
        ORDER BY (risk_dm_count + risk_hpt_count + risk_cvd_count + risk_bmi_count)::float / NULLIF(total_screened, 0) DESC
        LIMIT 1
    """)

    worst_district = None
    if worst:
        w = worst[0]
        worst_district = {
            "district_code": w.get("district_code"),
            "name_th": w.get("district_name"),
            "total_risk_pct": round(100.0 * (w.get("total_risk") or 0) / (w.get("total_screened") or 1), 1),
        }

    # Population from ref_districts
    pop = execute_scalar("SELECT SUM(population) FROM ref_districts") or 0

    return {
        "total_screened": total,
        "target": target,
        "coverage_pct": coverage,
        "population": pop,
        "top_disease": top_disease,
        "most_concerning_district": worst_district,
        "summary_text": f"คัดกรองแล้ว {total:,} คน จากเป้า {target:,} ({coverage}%) โรคที่พบมากที่สุดคือ{top_disease['name_th'] if top_disease else '-'} ({top_pct}%)",
    }


# =========================================================================== #
# P1 Endpoints
# =========================================================================== #

@app.get("/api/v2/facility/performance", tags=["Facility"])
def facility_performance(
    district: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Screening performance per health facility (HPTCODE)."""
    conditions = []
    params = []
    if district:
        conditions.append("s.district_code = %s")
        params.append(district)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT s.facility_code, s.district_code, s.total_screened,
               s.risk_dm, s.risk_hpt, s.found_dm, s.found_hpt,
               s.found_obesity, s.found_dyslipidemia,
               s.lab_completed, s.first_screening, s.last_screening
        FROM summary_facility s
        LEFT JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        ORDER BY s.total_screened DESC
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_screened") or 0) >= K_ANONYMITY_THRESHOLD]

    # Convert dates to strings
    for r in rows:
        for k in ("first_screening", "last_screening"):
            if r.get(k) and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()

    return {"facilities": rows}


@app.get("/api/v2/kpi/moph-targets", tags=["KPI"])
def moph_targets():
    """Compare actual performance against Ministry of Public Health targets."""
    total = execute_scalar("SELECT COALESCE(SUM(total_screened), 0) FROM summary_district_disease") or 0
    pop = execute_scalar("SELECT COALESCE(SUM(population), 0) FROM ref_districts") or 0

    disease_totals = execute_query("""
        SELECT
            SUM(total_screened) as total,
            SUM(risk_dm_count) as dm, SUM(risk_hpt_count) as hpt,
            SUM(found_obesity_count) as obesity
        FROM summary_district_disease
    """)
    d = disease_totals[0] if disease_totals else {}
    ts = d.get("total") or 1

    # MoPH KPI targets (standard Thai public health KPIs)
    kpis = [
        {
            "kpi_code": "NCD-01",
            "name_th": "ความครอบคลุมการคัดกรองสุขภาพ",
            "name_en": "Screening Coverage",
            "target_pct": 60.0,
            "actual_pct": round(100.0 * total / pop, 1) if pop > 0 else 0,
            "unit": "% ของประชากร",
        },
        {
            "kpi_code": "NCD-02",
            "name_th": "อัตราการพบผู้ป่วยเบาหวานรายใหม่",
            "name_en": "New DM Detection Rate",
            "target_pct": 5.0,
            "actual_pct": round(100.0 * (d.get("dm") or 0) / ts, 1),
            "unit": "% ของผู้คัดกรอง",
        },
        {
            "kpi_code": "NCD-03",
            "name_th": "อัตราการพบผู้ป่วยความดันรายใหม่",
            "name_en": "New HPT Detection Rate",
            "target_pct": 10.0,
            "actual_pct": round(100.0 * (d.get("hpt") or 0) / ts, 1),
            "unit": "% ของผู้คัดกรอง",
        },
        {
            "kpi_code": "NCD-04",
            "name_th": "อัตราภาวะอ้วน",
            "name_en": "Obesity Rate",
            "target_pct": 30.0,
            "actual_pct": round(100.0 * (d.get("obesity") or 0) / ts, 1),
            "unit": "% ของผู้คัดกรอง",
            "direction": "lower_is_better",
        },
    ]

    for kpi in kpis:
        direction = kpi.pop("direction", "higher_is_better")
        if direction == "lower_is_better":
            kpi["status"] = "ผ่าน" if kpi["actual_pct"] <= kpi["target_pct"] else "ไม่ผ่าน"
        else:
            kpi["status"] = "ผ่าน" if kpi["actual_pct"] >= kpi["target_pct"] else "ไม่ผ่าน"

    return {"total_screened": total, "population": pop, "kpis": kpis}


@app.get("/api/v2/zone/{zone_code}/dashboard", tags=["Zones"])
def zone_dashboard(zone_code: str):
    """Zone dashboard with facility breakdown."""
    # Zone info
    zone = execute_query(
        "SELECT zone_code, name_th, name_en, facilitator FROM ref_health_zones WHERE zone_code = %s",
        (zone_code,),
    )
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")

    # Districts in zone
    districts = execute_query("""
        SELECT s.district_code, s.district_name, s.total_screened,
               s.risk_dm_count, s.risk_hpt_count, s.found_obesity_count
        FROM summary_district_disease s
        WHERE s.zone_code = %s AND s.total_screened >= 5
        ORDER BY s.total_screened DESC
    """, (zone_code,))

    # Facilities in zone
    facilities = execute_query("""
        SELECT s.facility_code, s.district_code, s.total_screened,
               s.risk_dm, s.risk_hpt, s.found_obesity, s.lab_completed,
               s.first_screening, s.last_screening
        FROM summary_facility s
        JOIN ref_districts d ON s.district_code = d.dcode
        WHERE d.zone_code = %s AND s.total_screened >= 5
        ORDER BY s.total_screened DESC
    """, (zone_code,))

    for f in facilities:
        for k in ("first_screening", "last_screening"):
            if f.get(k) and hasattr(f[k], "isoformat"):
                f[k] = f[k].isoformat()

    # Zone totals
    zone_total = sum(d.get("total_screened", 0) or 0 for d in districts)

    return {
        **zone[0],
        "total_screened": zone_total,
        "district_count": len(districts),
        "facility_count": len(facilities),
        "districts": districts,
        "facilities": facilities,
    }


@app.get("/api/v2/executive/yoy-comparison", tags=["Executive"])
def yoy_comparison(
    granularity: str = Query("quarterly"),
):
    """Year-over-year or quarter-over-quarter comparison."""
    if granularity not in ("monthly", "quarterly"):
        raise HTTPException(status_code=400, detail="granularity must be monthly or quarterly")

    trunc = "quarter" if granularity == "quarterly" else "month"

    rows = execute_query(f"""
        SELECT
            DATE_TRUNC('{trunc}', v.visit_date) AS period,
            COUNT(DISTINCT v.patient_id) AS screened,
            COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm) AS risk_dm,
            COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt) AS risk_hpt,
            COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_obesity) AS found_obesity
        FROM raw_vitalsigns v
        WHERE v.cancel_status IS DISTINCT FROM 1 AND v.visit_date IS NOT NULL
        GROUP BY DATE_TRUNC('{trunc}', v.visit_date)
        HAVING COUNT(DISTINCT v.patient_id) >= %s
        ORDER BY period
    """, (K_ANONYMITY_THRESHOLD,))

    # Convert periods and compute deltas
    result = []
    prev = None
    for r in rows:
        if r.get("period") and hasattr(r["period"], "isoformat"):
            r["period"] = r["period"].isoformat()[:10]
        if prev:
            r["delta_screened"] = (r.get("screened") or 0) - (prev.get("screened") or 0)
            r["delta_pct"] = round(100.0 * r["delta_screened"] / (prev.get("screened") or 1), 1)
        else:
            r["delta_screened"] = 0
            r["delta_pct"] = 0
        prev = r
        result.append(r)

    return {"granularity": granularity, "periods": result}


@app.get("/api/v2/epidemiology/multi-disease-matrix", tags=["Epidemiology"])
def multi_disease_matrix(
    district: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Disease co-occurrence matrix -- comorbidity analysis."""
    conditions = []
    params = []
    if district:
        conditions.append("s.district_code = %s")
        params.append(district)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT s.district_code, s.total_screened,
               s.dm_only, s.hpt_only, s.obesity_only,
               s.dm_and_hpt, s.dm_and_obesity, s.dm_and_dyslipidemia,
               s.hpt_and_obesity, s.hpt_and_dyslipidemia,
               s.cvd_and_stroke, s.dm_and_cvd,
               s.metabolic_syndrome, s.dm_hpt_obesity,
               s.multi_disease_count, s.no_disease
        FROM summary_comorbidity s
        LEFT JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        ORDER BY s.district_code
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_screened") or 0) >= K_ANONYMITY_THRESHOLD]
    return {"data": rows}


# --------------------------------------------------------------------------- #
# Run with: uvicorn main:app --host 0.0.0.0 --port 8000
# --------------------------------------------------------------------------- #
