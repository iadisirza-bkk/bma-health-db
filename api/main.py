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


app = FastAPI(
    title="BMA Health Summary API",
    version="2.0.0",
    description="ระบบฐานข้อมูลสุขภาพ กรุงเทพมหานคร — Summary API\n\nAggregate health screening data for Bangkok Metropolitan Administration.\nNo PII. k-anonymity ≥ 5 enforced.",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
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


# =========================================================================== #
# Age-sex pyramid
# =========================================================================== #

@app.get("/api/v2/epidemiology/age-pyramid", tags=["Epidemiology"])
def age_pyramid(
    district: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Age-sex pyramid of screened population."""
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
        SELECT s.age_group, s.sex,
               SUM(s.total_screened) AS count
        FROM summary_disease_age_sex s
        JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        GROUP BY s.age_group, s.sex
        ORDER BY s.age_group, s.sex
    """, tuple(params) or None)

    # Pivot into {age_group, male_count, female_count}
    age_map: dict = {}
    for r in rows:
        ag = r["age_group"]
        if ag not in age_map:
            age_map[ag] = {"age_group": ag, "male_count": 0, "female_count": 0}
        sex = r.get("sex")
        cnt = r.get("count") or 0
        if sex == 1:
            age_map[ag]["male_count"] = int(cnt)
        elif sex == 2:
            age_map[ag]["female_count"] = int(cnt)

    result = list(age_map.values())
    # Enforce k-anonymity: suppress rows where both counts are below threshold
    result = [
        r for r in result
        if (r["male_count"] + r["female_count"]) >= K_ANONYMITY_THRESHOLD
    ]

    return {"data": result}


# =========================================================================== #
# Disease Control
# =========================================================================== #

@app.get("/api/v2/disease-control/screening-coverage", tags=["Disease Control"])
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


@app.get("/api/v2/disease-control/ncd-cascade", tags=["Disease Control"])
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


# =========================================================================== #
# Monitoring — table stats
# =========================================================================== #

@app.get("/api/v2/monitoring/table-stats", tags=["Monitoring"])
def table_stats():
    """Row counts and metadata per table."""
    rows = execute_query("""
        SELECT
            t.tablename AS table_name,
            COALESCE(s.n_live_tup, 0) AS row_count,
            (SELECT COUNT(*) FROM information_schema.columns c
             WHERE c.table_schema = 'public' AND c.table_name = t.tablename) AS column_count
        FROM pg_catalog.pg_tables t
        LEFT JOIN pg_stat_user_tables s ON s.relname = t.tablename
        WHERE t.schemaname = 'public'
        ORDER BY t.tablename
    """)

    # Add last-updated from summary tables where available
    last_updated = execute_scalar(
        "SELECT MAX(refreshed_at) FROM summary_district_disease"
    )

    return {
        "tables": rows,
        "last_updated": str(last_updated) if last_updated else None,
    }


# =========================================================================== #
# KPI — screening yield
# =========================================================================== #

@app.get("/api/v2/kpi/screening-yield", tags=["KPI"])
def screening_yield(
    disease: str = Query("diabetes"),
    zone_code: Optional[str] = Query(None),
):
    """Screening yield (% risk found / total screened) per district."""
    _validate_disease_key(disease)
    dk = DISEASE_KEYS[disease]

    risk_col = dk.get("risk")
    found_col = dk.get("found")
    count_col = None
    if risk_col:
        count_col = f"{risk_col}_count"
    elif found_col:
        count_col = f"{found_col}_count"

    if not count_col:
        raise HTTPException(
            status_code=400,
            detail=f"No risk/found column available for '{disease}'.",
        )

    conditions = ["s.total_screened > 0"]
    params: list = []
    if zone_code:
        conditions.append("s.zone_code = %s")
        params.append(zone_code)
    where = "WHERE " + " AND ".join(conditions)

    rows = execute_query(f"""
        SELECT s.district_code, s.district_name, s.zone_code,
               s.total_screened,
               s.{count_col} AS risk_found,
               ROUND(100.0 * s.{count_col} / NULLIF(s.total_screened, 0), 2) AS yield_pct
        FROM summary_district_disease s
        {where}
        ORDER BY yield_pct DESC
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_screened") or 0) >= K_ANONYMITY_THRESHOLD]
    return {"disease": disease, "data": rows}


# =========================================================================== #
# Public — district summary
# =========================================================================== #

@app.get("/api/v2/public/district-summary", tags=["Public"])
def public_district_summary(
    district: str = Query(..., description="District code"),
    lang: str = Query("th"),
):
    """Simplified health summary for public. PDPA-safe, Thai language."""
    disease = execute_query(
        """SELECT district_code, district_name, total_screened,
                  risk_dm_count, pct_risk_dm,
                  risk_hpt_count, pct_risk_hpt,
                  risk_cvd_count, pct_risk_cvd,
                  found_obesity_count
           FROM summary_district_disease WHERE district_code = %s""",
        (district,),
    )
    if not disease:
        raise HTTPException(status_code=404, detail="District not found")

    d = disease[0]
    total = d.get("total_screened") or 0
    if total < K_ANONYMITY_THRESHOLD:
        raise HTTPException(status_code=403, detail="Data suppressed for privacy (k-anonymity)")

    name = d.get("district_name") or district

    # Suppress individual disease counts below threshold
    dm_count = d.get("risk_dm_count") or 0
    hpt_count = d.get("risk_hpt_count") or 0
    cvd_count = d.get("risk_cvd_count") or 0
    obesity_count = d.get("found_obesity_count") or 0

    dm_text = f"เบาหวาน {dm_count:,} คน ({d.get('pct_risk_dm') or 0}%)" if dm_count >= K_ANONYMITY_THRESHOLD else "เบาหวาน: ข้อมูลไม่เพียงพอ"
    hpt_text = f"ความดันสูง {hpt_count:,} คน ({d.get('pct_risk_hpt') or 0}%)" if hpt_count >= K_ANONYMITY_THRESHOLD else "ความดันสูง: ข้อมูลไม่เพียงพอ"
    cvd_text = f"หัวใจและหลอดเลือด {cvd_count:,} คน ({d.get('pct_risk_cvd') or 0}%)" if cvd_count >= K_ANONYMITY_THRESHOLD else "หัวใจและหลอดเลือด: ข้อมูลไม่เพียงพอ"
    obesity_text = f"โรคอ้วน {obesity_count:,} คน" if obesity_count >= K_ANONYMITY_THRESHOLD else "โรคอ้วน: ข้อมูลไม่เพียงพอ"

    summary = (
        f"สรุปผลการคัดกรองสุขภาพ เขต{name}\n"
        f"จำนวนผู้เข้ารับการคัดกรอง: {total:,} คน\n\n"
        f"ผลการคัดกรองโรคเรื้อรัง:\n"
        f"- {dm_text}\n"
        f"- {hpt_text}\n"
        f"- {cvd_text}\n"
        f"- {obesity_text}\n\n"
        f"หมายเหตุ: ข้อมูลนี้เป็นข้อมูลรวม ไม่มีข้อมูลส่วนบุคคล"
    )

    return {
        "district_code": district,
        "district_name": name,
        "total_screened": total,
        "summary_text": summary,
        "lang": lang,
    }


# =========================================================================== #
# Research — data dictionary
# =========================================================================== #

@app.get("/api/v2/research/data-dictionary", tags=["Research"])
def data_dictionary():
    """Auto-generated data dictionary for all public tables."""
    rows = execute_query("""
        SELECT
            c.table_name,
            c.column_name,
            c.data_type,
            c.is_nullable,
            c.column_default,
            c.ordinal_position,
            pgd.description
        FROM information_schema.columns c
        LEFT JOIN pg_catalog.pg_statio_all_tables st
            ON c.table_schema = st.schemaname AND c.table_name = st.relname
        LEFT JOIN pg_catalog.pg_description pgd
            ON pgd.objoid = st.relid
            AND pgd.objsubid = c.ordinal_position
        WHERE c.table_schema = 'public'
        ORDER BY c.table_name, c.ordinal_position
    """)

    # Group by table
    tables: dict = {}
    for r in rows:
        tn = r["table_name"]
        if tn not in tables:
            tables[tn] = {"table": tn, "columns": []}
        tables[tn]["columns"].append({
            "column": r["column_name"],
            "type": r["data_type"],
            "nullable": r["is_nullable"],
            "default": r.get("column_default"),
            "description": r.get("description"),
        })

    return {"tables": list(tables.values())}


# =========================================================================== #
# Evaluator 1: Epidemiology — additional endpoints
# =========================================================================== #

@app.get("/api/v2/epidemiology/incidence-rate", tags=["Epidemiology"])
def incidence_rate(
    disease: str = Query("diabetes"),
    period: Optional[str] = Query(None, description="e.g. 2024Q3, 2025-01"),
    district: Optional[str] = Query(None),
):
    """Incidence rate: new cases / population at risk per period."""
    _validate_disease_key(disease)
    dk = DISEASE_KEYS[disease]
    bool_col = dk.get("risk") or dk.get("found")
    if not bool_col:
        return {"data_available": False, "message": f"ไม่มีคอลัมน์ risk/found สำหรับ '{disease}' — ใช้ /api/v2/summary/lab แทน"}

    conditions = ["v.cancel_status IS DISTINCT FROM 1", "v.visit_date IS NOT NULL"]
    params: list = []

    if district:
        conditions.append("v.district_code = %s")
        params.append(district)

    where = "WHERE " + " AND ".join(conditions)

    rows = execute_query(f"""
        SELECT
          DATE_TRUNC('month', v.visit_date) AS period,
          COUNT(DISTINCT v.patient_id) AS total_screened,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.{bool_col}) AS new_cases,
          ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.{bool_col})
                / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS incidence_pct
        FROM raw_vitalsigns v
        {where}
        GROUP BY DATE_TRUNC('month', v.visit_date)
        ORDER BY period
    """, tuple(params) or None)

    # Filter by period if specified
    if period and rows:
        filtered = []
        for r in rows:
            p = r.get("period")
            p_str = p.strftime("%Y-%m-%d") if hasattr(p, "strftime") else str(p)
            if period in p_str:
                filtered.append(r)
        rows = filtered

    # Enforce k-anonymity
    rows = [r for r in rows if (r.get("total_screened") or 0) >= K_ANONYMITY_THRESHOLD]

    for r in rows:
        if r.get("period") and hasattr(r["period"], "strftime"):
            r["period"] = r["period"].strftime("%Y-%m-%d")

    if not rows:
        return {"data_available": False, "disease": disease, "message": "ไม่มีข้อมูล incidence สำหรับเงื่อนไขที่ระบุ"}

    return {"disease": disease, "district": district, "period_filter": period, "data": rows}


@app.get("/api/v2/epidemiology/outbreak-detection", tags=["Epidemiology"])
def outbreak_detection(
    zone_code: Optional[str] = Query(None),
    disease: str = Query("diabetes"),
):
    """Early warning: detect if current prevalence exceeds baseline by 2 SD."""
    _validate_disease_key(disease)
    dk = DISEASE_KEYS[disease]
    bool_col = dk.get("risk") or dk.get("found")
    if not bool_col:
        return {"data_available": False, "message": f"ไม่มีคอลัมน์ risk/found สำหรับ '{disease}' — ใช้ /api/v2/summary/lab แทน"}

    conditions = ["v.cancel_status IS DISTINCT FROM 1", "v.visit_date IS NOT NULL"]
    params: list = []

    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)

    where = "WHERE " + " AND ".join(conditions)

    rows = execute_query(f"""
        SELECT
          DATE_TRUNC('month', v.visit_date) AS period,
          COUNT(DISTINCT v.patient_id) AS total_screened,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.{bool_col}) AS at_risk_count,
          ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.{bool_col})
                / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct
        FROM raw_vitalsigns v
        JOIN ref_districts d ON v.district_code = d.dcode
        {where}
        GROUP BY DATE_TRUNC('month', v.visit_date)
        HAVING COUNT(DISTINCT v.patient_id) >= %s
        ORDER BY period
    """, tuple(params) + (K_ANONYMITY_THRESHOLD,))

    if len(rows) < 3:
        return {
            "disease": disease,
            "zone_code": zone_code,
            "alert": False,
            "reason": "insufficient_data",
            "message": f"ข้อมูลไม่เพียงพอ (มี {len(rows)} ช่วงเวลา ต้องมีอย่างน้อย 3 ช่วง)",
        }

    # Compute baseline from all periods except the latest
    pcts = [float(r.get("pct") or 0) for r in rows]
    baseline_pcts = pcts[:-1]
    current_pct = pcts[-1]

    mean_val = sum(baseline_pcts) / len(baseline_pcts)
    variance = sum((x - mean_val) ** 2 for x in baseline_pcts) / len(baseline_pcts)
    sd_val = variance ** 0.5
    threshold = round(mean_val + 2 * sd_val, 2)

    latest_period = rows[-1].get("period")
    if latest_period and hasattr(latest_period, "strftime"):
        latest_period = latest_period.strftime("%Y-%m-%d")

    return {
        "disease": disease,
        "zone_code": zone_code,
        "latest_period": latest_period,
        "current_pct": current_pct,
        "baseline_mean": round(mean_val, 2),
        "baseline_sd": round(sd_val, 2),
        "threshold": threshold,
        "alert": current_pct > threshold,
        "periods_used": len(baseline_pcts),
    }


# =========================================================================== #
# Evaluator 2: Health Promotion — additional endpoints
# =========================================================================== #

@app.get("/api/v2/promotion/behavior-disease-correlation", tags=["Health Promotion"])
def behavior_disease_correlation(
    behavior: str = Query("smoking", description="smoking|alcohol|exercise"),
    disease: str = Query("diabetes"),
    district: Optional[str] = Query(None),
):
    """Correlation between lifestyle behavior and disease prevalence."""
    _validate_disease_key(disease)

    valid_behaviors = {"smoking", "alcohol", "exercise"}
    if behavior not in valid_behaviors:
        raise HTTPException(status_code=400, detail=f"Invalid behavior '{behavior}'. Valid: {sorted(valid_behaviors)}")

    dk = DISEASE_KEYS[disease]
    risk_col = dk.get("risk")
    found_col = dk.get("found")

    if behavior in ("smoking", "exercise"):
        # Data from summary_district_risk_factors
        behavior_col = behavior
        conditions = []
        params: list = []
        if district:
            conditions.append("district_code = %s")
            params.append(district)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        total_check = execute_scalar(
            f'SELECT COUNT(*) FROM summary_district_risk_factors WHERE "{behavior_col}" IS NOT NULL'
        ) or 0
        if total_check == 0:
            return {"data_available": False, "message": f"ไม่มีข้อมูล {behavior} ใน summary_district_risk_factors"}

        rows = execute_query(f"""
            SELECT
              "{behavior_col}" AS behavior_value,
              SUM(patient_count)::int AS total
            FROM summary_district_risk_factors
            {where}
            GROUP BY "{behavior_col}"
            ORDER BY "{behavior_col}"
        """, tuple(params) or None)

        rows = enforce_k_anonymity(rows, count_field="total")

        behavior_labels = {
            "smoking": {0: "ไม่สูบ", 1: "สูบ"},
            "exercise": {1: "≥3 วัน/สัปดาห์", 2: "<3 วัน/สัปดาห์", 3: "ไม่ออกกำลังกาย"},
        }
        labels = behavior_labels.get(behavior, {})
        for r in rows:
            val = r.get("behavior_value")
            r["behavior_label"] = labels.get(val, str(val))

        return {"behavior": behavior, "disease": disease, "district": district, "data": rows}

    else:
        # alcohol — check if data exists in raw_homehealth
        total_check = execute_scalar(
            "SELECT COUNT(*) FROM raw_homehealth WHERE alcohol IS NOT NULL"
        ) or 0
        if total_check == 0:
            return {"data_available": False, "message": "ไม่มีข้อมูล alcohol ใน raw_homehealth — ต้องรอข้อมูลจาก HDC"}

        conditions = ["h.alcohol IS NOT NULL"]
        params = []
        if district:
            conditions.append("v.district_code = %s")
            params.append(district)
        where = "WHERE " + " AND ".join(conditions)

        rows = execute_query(f"""
            SELECT
              h.alcohol AS behavior_value,
              COUNT(DISTINCT h.patient_id) AS total
            FROM raw_homehealth h
            JOIN raw_vitalsigns v ON h.patient_id = v.patient_id
                AND v.cancel_status IS DISTINCT FROM 1
            {where}
            GROUP BY h.alcohol
            ORDER BY h.alcohol
        """, tuple(params) or None)

        rows = enforce_k_anonymity(rows, count_field="total")
        alcohol_labels = {1: "ไม่ดื่ม", 2: "ดื่ม", 3: "เลิกแล้ว"}
        for r in rows:
            val = r.get("behavior_value")
            r["behavior_label"] = alcohol_labels.get(val, str(val))

        return {"behavior": behavior, "disease": disease, "district": district, "data": rows}


@app.get("/api/v2/promotion/risk-factor-profile", tags=["Health Promotion"])
def risk_factor_profile(
    district: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Risk factor summary per district: smoking, alcohol, exercise rates."""
    conditions = []
    params: list = []
    if district:
        conditions.append("s.district_code = %s")
        params.append(district)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT
          s.district_code,
          SUM(s.patient_count)::int AS total,
          SUM(CASE WHEN s.smoking = 1 THEN s.patient_count ELSE 0 END)::int AS smoking_count,
          SUM(CASE WHEN s.exercise = 3 THEN s.patient_count ELSE 0 END)::int AS no_exercise_count
        FROM summary_district_risk_factors s
        JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        GROUP BY s.district_code
        ORDER BY s.district_code
    """, tuple(params) or None)

    rows = enforce_k_anonymity(rows, count_field="total")

    for r in rows:
        total = r.get("total") or 1
        r["pct_smoking"] = round(100.0 * (r.get("smoking_count") or 0) / total, 2)
        r["pct_no_exercise"] = round(100.0 * (r.get("no_exercise_count") or 0) / total, 2)

    if not rows:
        return {"data_available": False, "message": "ไม่มีข้อมูล risk factor ใน summary_district_risk_factors"}

    return {"district": district, "zone_code": zone_code, "data": rows}


@app.get("/api/v2/promotion/exercise-frequency", tags=["Health Promotion"])
def exercise_frequency(district: Optional[str] = Query(None)):
    """Exercise frequency distribution: >=3/wk, <3/wk, never."""
    total_check = execute_scalar(
        "SELECT COUNT(*) FROM raw_homehealth WHERE exercise IS NOT NULL"
    ) or 0
    if total_check == 0:
        return {
            "data_available": False,
            "message": "ไม่มีข้อมูลการออกกำลังกาย (exercise) ใน raw_homehealth — ต้องรอข้อมูลจาก HDC",
        }

    conditions = ["h.exercise IS NOT NULL"]
    params: list = []
    if district:
        conditions.append("v.district_code = %s")
        params.append(district)
    where = "WHERE " + " AND ".join(conditions)

    rows = execute_query(f"""
        SELECT
          v.district_code,
          SUM(CASE WHEN h.exercise = 1 THEN 1 ELSE 0 END) AS exercise_3plus,
          SUM(CASE WHEN h.exercise = 2 THEN 1 ELSE 0 END) AS exercise_less3,
          SUM(CASE WHEN h.exercise = 3 THEN 1 ELSE 0 END) AS exercise_never,
          COUNT(*) AS total
        FROM raw_homehealth h
        JOIN raw_vitalsigns v ON h.patient_id = v.patient_id
            AND v.cancel_status IS DISTINCT FROM 1
        {where}
        GROUP BY v.district_code
        ORDER BY v.district_code
    """, tuple(params) or None)

    rows = enforce_k_anonymity(rows, count_field="total")

    return {
        "exercise_codes": {"1": ">=3 วัน/สัปดาห์", "2": "<3 วัน/สัปดาห์", "3": "ไม่ออกกำลังกาย"},
        "district": district,
        "data": rows,
    }


@app.get("/api/v2/promotion/waist-risk-analysis", tags=["Health Promotion"])
def waist_risk_analysis(zone_code: Optional[str] = Query(None)):
    """Waist circumference risk: % exceeding threshold (M>90cm, F>80cm) per district."""
    conditions = []
    params: list = []
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT
          s.district_code,
          SUM(s.total_waist_measured)::int AS total_measured,
          SUM(s.male_waist_risk)::int AS male_risk_count,
          SUM(s.female_waist_risk)::int AS female_risk_count,
          ROUND(100.0 * (SUM(s.male_waist_risk) + SUM(s.female_waist_risk))
                / NULLIF(SUM(s.total_waist_measured), 0), 2) AS pct_at_risk
        FROM summary_bmi_waist s
        JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        GROUP BY s.district_code
        ORDER BY s.district_code
    """, tuple(params) or None)

    rows = enforce_k_anonymity(rows, count_field="total_measured")

    if not rows:
        return {"data_available": False, "message": "ไม่มีข้อมูลรอบเอว — ต้องรอข้อมูลจาก HDC"}

    return {
        "thresholds": {"male": ">90 cm", "female": ">80 cm"},
        "zone_code": zone_code,
        "data": rows,
    }


# =========================================================================== #
# Evaluator 3: Disease Control — additional endpoints
# =========================================================================== #

@app.get("/api/v2/disease-control/repeat-screening", tags=["Disease Control"])
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


@app.get("/api/v2/disease-control/progression", tags=["Disease Control"])
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


@app.get("/api/v2/disease-control/referral-outcome", tags=["Disease Control"])
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


@app.get("/api/v2/disease-control/treatment-compliance", tags=["Disease Control"])
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


# =========================================================================== #
# Evaluator 4: IT — Monitoring endpoints
# =========================================================================== #

@app.get("/api/v2/monitoring/etl-status", tags=["Monitoring"])
def etl_status():
    """ETL pipeline status: last import per file type, success/failure."""
    rows = execute_query("""
        SELECT file_type,
               MAX(started_at) as last_import,
               MAX(CASE WHEN status = 'success' THEN started_at END) as last_success,
               COUNT(*) as total_imports,
               COUNT(CASE WHEN status = 'success' THEN 1 END) as success_count,
               COUNT(CASE WHEN status = 'error' THEN 1 END) as error_count
        FROM import_history
        GROUP BY file_type
        ORDER BY file_type
    """)
    # Convert datetimes to strings
    for r in rows:
        for k in ("last_import", "last_success"):
            if r.get(k) and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()
    return {"file_types": rows, "overall_status": "healthy" if rows else "no_imports"}


@app.get("/api/v2/monitoring/api-performance", tags=["Monitoring"])
def api_performance():
    """API performance metrics (from audit log if available)."""
    # We don't have stored metrics, return basic info
    return {
        "note": "API performance metrics are logged to stdout via AuditMiddleware",
        "endpoints_count": 45,
        "rate_limit": {"public": 60, "per_minute": True},
        "cache_ttl": {"health_data": 300, "static_data": 3600},
        "database_pool": {"min_connections": 2, "max_connections": 20},
    }


@app.get("/api/v2/monitoring/audit-log", tags=["Monitoring"])
def audit_log(limit: int = Query(50, ge=1, le=500)):
    """PDPA audit log: recent data access events from import history."""
    # We track imports + admin access via import_history
    # Full audit is in AuditMiddleware (stdout)
    rows = execute_query("""
        SELECT id, filename, table_name, file_type, status,
               started_at, completed_at, rows_imported, uploaded_by
        FROM import_history
        ORDER BY started_at DESC
        LIMIT %s
    """, (limit,))
    for r in rows:
        for k in ("started_at", "completed_at"):
            if r.get(k) and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()
    return {"events": rows, "source": "import_history",
            "note": "Full API audit logs available in server stdout (AuditMiddleware)"}


# =========================================================================== #
# Evaluator 5: นโยบาย — KPI endpoints
# =========================================================================== #

@app.get("/api/v2/kpi/control-rates", tags=["KPI"])
def control_rates(disease: str = Query("diabetes")):
    """Disease control rates (e.g., HbA1c < 7% for DM, BP < 140/90 for HPT)."""
    # Control rates require lab follow-up data
    # Check if we have lab data
    lab_count = execute_scalar("SELECT COUNT(*) FROM raw_lab_results WHERE cancel_status IS DISTINCT FROM 1 AND fbs IS NOT NULL") or 0

    if disease == "diabetes" and lab_count > 0:
        # FBS < 126 as a proxy for "controlled"
        controlled = execute_scalar("""
            SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE l.fbs < 126) / NULLIF(COUNT(*), 0), 1)
            FROM raw_lab_results l
            JOIN raw_vitalsigns v ON l.patient_id = v.patient_id
            WHERE v.found_dm AND l.fbs IS NOT NULL AND l.cancel_status IS DISTINCT FROM 1
        """)
        return {"disease": disease, "control_metric": "FBS < 126 mg/dL",
                "control_rate_pct": controlled or 0, "lab_patients": lab_count,
                "note": "Proxy metric. HbA1c not available in current dataset."}

    if disease == "hypertension":
        controlled = execute_scalar("""
            SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE v.sbp < 140 AND v.dbp < 90) / NULLIF(COUNT(*), 0), 1)
            FROM raw_vitalsigns v
            WHERE v.found_hpt AND v.sbp IS NOT NULL AND v.cancel_status IS DISTINCT FROM 1
        """)
        return {"disease": disease, "control_metric": "BP < 140/90 mmHg",
                "control_rate_pct": controlled or 0}

    return {"data_available": False, "message": f"ไม่มี control rate metric สำหรับ {disease} — ต้องรอข้อมูล lab เพิ่มเติม"}


@app.get("/api/v2/kpi/zone-comparison", tags=["KPI"])
def zone_comparison(kpi: str = Query("screening_coverage")):
    """Compare 8 zones on a single KPI."""
    if kpi == "screening_coverage":
        rows = execute_query("""
            SELECT z.zone_code, z.name_th,
                   COALESCE(SUM(s.total_screened), 0) as screened,
                   SUM(d.population) as population,
                   ROUND(100.0 * COALESCE(SUM(s.total_screened), 0) / NULLIF(SUM(d.population), 0), 2) as value
            FROM ref_health_zones z
            JOIN ref_districts d ON d.zone_code = z.zone_code
            LEFT JOIN summary_district_disease s ON s.zone_code = z.zone_code AND s.district_code = d.dcode
            GROUP BY z.zone_code, z.name_th
            ORDER BY value DESC
        """)
    elif kpi in ("dm_risk", "hpt_risk", "obesity_risk"):
        col_map = {"dm_risk": "risk_dm_count", "hpt_risk": "risk_hpt_count", "obesity_risk": "found_obesity_count"}
        col = col_map[kpi]
        rows = execute_query(f"""
            SELECT z.zone_code, z.name_th,
                   COALESCE(SUM(s.total_screened), 0) as screened,
                   COALESCE(SUM(s.{col}), 0) as risk_count,
                   ROUND(100.0 * COALESCE(SUM(s.{col}), 0) / NULLIF(SUM(s.total_screened), 0), 2) as value
            FROM ref_health_zones z
            LEFT JOIN summary_district_disease s ON s.zone_code = z.zone_code
            GROUP BY z.zone_code, z.name_th
            ORDER BY value DESC
        """)
    else:
        return {"error": f"Unknown KPI: {kpi}. Valid: screening_coverage, dm_risk, hpt_risk, obesity_risk"}

    return {"kpi": kpi, "zones": rows}


@app.get("/api/v2/kpi/progress-tracker", tags=["KPI"])
def progress_tracker(
    year: Optional[int] = Query(None),
    quarter: Optional[str] = Query(None, description="Q1, Q2, Q3, Q4"),
):
    """Quarterly progress vs targets."""
    # Query trends grouped by quarter
    rows = execute_query("""
        SELECT DATE_TRUNC('quarter', v.visit_date) as period,
               COUNT(DISTINCT v.patient_id) as screened,
               COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm) as dm_risk,
               COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt) as hpt_risk
        FROM raw_vitalsigns v
        WHERE v.cancel_status IS DISTINCT FROM 1 AND v.visit_date IS NOT NULL
        GROUP BY DATE_TRUNC('quarter', v.visit_date)
        HAVING COUNT(DISTINCT v.patient_id) >= %s
        ORDER BY period
    """, (K_ANONYMITY_THRESHOLD,))

    target_per_quarter = TARGET_SCREENED // 4
    for r in rows:
        if r.get("period") and hasattr(r["period"], "isoformat"):
            r["period"] = r["period"].isoformat()[:10]
        r["target"] = target_per_quarter
        r["progress_pct"] = round(100.0 * (r.get("screened") or 0) / target_per_quarter, 1)

    return {"target_annual": TARGET_SCREENED, "target_quarterly": target_per_quarter, "quarters": rows}


@app.get("/api/v2/kpi/benchmark", tags=["KPI"])
def benchmark():
    """Benchmark BMA against national averages (reference data)."""
    total = execute_scalar("SELECT COALESCE(SUM(total_screened), 0) FROM summary_district_disease") or 0
    pop = execute_scalar("SELECT COALESCE(SUM(population), 0) FROM ref_districts") or 0

    d = execute_query("""
        SELECT SUM(risk_dm_count) as dm, SUM(risk_hpt_count) as hpt,
               SUM(found_obesity_count) as obesity, SUM(total_screened) as total
        FROM summary_district_disease
    """)
    dd = d[0] if d else {}
    ts = dd.get("total") or 1

    # National reference values (from MoPH Health Survey 2024)
    benchmarks = [
        {"indicator": "DM prevalence", "indicator_th": "ความชุกเบาหวาน",
         "bma_pct": round(100.0 * (dd.get("dm") or 0) / ts, 1),
         "national_pct": 9.5, "source": "สำรวจสุขภาพ ครั้งที่ 6 (2562)"},
        {"indicator": "HPT prevalence", "indicator_th": "ความชุกความดันสูง",
         "bma_pct": round(100.0 * (dd.get("hpt") or 0) / ts, 1),
         "national_pct": 24.7, "source": "สำรวจสุขภาพ ครั้งที่ 6"},
        {"indicator": "Obesity prevalence", "indicator_th": "ความชุกโรคอ้วน",
         "bma_pct": round(100.0 * (dd.get("obesity") or 0) / ts, 1),
         "national_pct": 37.5, "source": "สำรวจสุขภาพ ครั้งที่ 6"},
        {"indicator": "Screening coverage", "indicator_th": "ความครอบคลุมการคัดกรอง",
         "bma_pct": round(100.0 * total / pop, 1) if pop else 0,
         "national_pct": 45.0, "source": "เป้าหมาย สปสช. 2567"},
    ]
    for b in benchmarks:
        b["vs_national"] = "สูงกว่า" if b["bma_pct"] > b["national_pct"] else "ต่ำกว่า"
        b["delta"] = round(b["bma_pct"] - b["national_pct"], 1)

    return {"benchmarks": benchmarks}


# =========================================================================== #
# Evaluator 6: ที่ปรึกษาผู้ว่า — Executive endpoints
# =========================================================================== #

@app.get("/api/v2/executive/campaign-impact", tags=["Executive"])
def campaign_impact(campaign: Optional[str] = Query(None)):
    """Campaign impact analysis."""
    return {"data_available": False,
            "message": "ยังไม่มีข้อมูล campaign ในระบบ — ต้องเพิ่ม campaign_events table เพื่อบันทึกกิจกรรมรณรงค์ แล้ว link กับ screening volume",
            "suggestion": "สร้าง reference table: campaign_events (id, name, start_date, end_date, zone_code, type)"}


@app.get("/api/v2/executive/media-brief", tags=["Executive"])
def media_brief(lang: str = Query("th"), max_bullets: int = Query(5)):
    """Auto-generated media brief for press conferences."""
    # Reuse headline KPI data
    total = execute_scalar("SELECT COALESCE(SUM(total_screened), 0) FROM summary_district_disease") or 0
    pop = execute_scalar("SELECT COALESCE(SUM(population), 0) FROM ref_districts") or 0

    d = execute_query("SELECT SUM(risk_dm_count) as dm, SUM(risk_hpt_count) as hpt, SUM(found_obesity_count) as obesity, SUM(total_screened) as total FROM summary_district_disease")
    dd = d[0] if d else {}
    ts = dd.get("total") or 1

    bullets = [
        f"กรุงเทพมหานครคัดกรองสุขภาพแล้ว {total:,.0f} คน จากประชากร {pop:,.0f} คน (ครอบคลุม {round(100*total/pop,1) if pop else 0}%)",
        f"โรคที่พบมากที่สุด: ความดันโลหิตสูง ({round(100*(dd.get('hpt') or 0)/ts,1)}%) เบาหวาน ({round(100*(dd.get('dm') or 0)/ts,1)}%)",
        f"ภาวะอ้วนพบ {round(100*(dd.get('obesity') or 0)/ts,1)}% ของผู้คัดกรอง",
        f"ดำเนินการคัดกรองผ่านศูนย์บริการสาธารณสุข กทม. ทั้ง 69 แห่ง ครอบคลุม 50 เขต 8 โซนสุขภาพ",
        f"ข้อมูลทั้งหมดเป็นข้อมูลรวม (aggregate) ไม่มีข้อมูลส่วนบุคคล ตาม พ.ร.บ.คุ้มครองข้อมูลส่วนบุคคล พ.ศ. 2562",
    ]

    return {"lang": lang, "bullets": bullets[:max_bullets], "generated_at": datetime.utcnow().isoformat()}


@app.get("/api/v2/executive/alert", tags=["Executive"])
def executive_alerts(severity: str = Query("all", description="all|critical|warning")):
    """Alert system: flag districts/diseases exceeding thresholds."""
    alerts = []

    # Check districts with very high disease rates
    high_risk = execute_query("""
        SELECT district_code, district_name, total_screened,
               pct_risk_dm, pct_risk_hpt,
               ROUND(100.0 * found_obesity_count / NULLIF(total_screened, 0), 1) as pct_obesity
        FROM summary_district_disease
        WHERE total_screened >= 5
    """)

    for d in high_risk:
        if (d.get("pct_risk_dm") or 0) > 25:
            alerts.append({"severity": "critical", "type": "high_prevalence", "district": d.get("district_name"),
                          "disease": "เบาหวาน", "value": d["pct_risk_dm"], "threshold": 25})
        if (d.get("pct_risk_hpt") or 0) > 30:
            alerts.append({"severity": "critical", "type": "high_prevalence", "district": d.get("district_name"),
                          "disease": "ความดันสูง", "value": d["pct_risk_hpt"], "threshold": 30})
        if (d.get("pct_obesity") or 0) > 40:
            alerts.append({"severity": "warning", "type": "high_prevalence", "district": d.get("district_name"),
                          "disease": "อ้วน", "value": d["pct_obesity"], "threshold": 40})

    if severity != "all":
        alerts = [a for a in alerts if a["severity"] == severity]

    return {"alerts": alerts, "total": len(alerts)}


# =========================================================================== #
# Evaluator 7: ยุทธศาสตร์ — Strategy endpoints
# =========================================================================== #

# Standard Thai healthcare cost references (สปสช. 2567)
_SCREENING_COST_PER_PERSON = 350  # THB per person
_DM_TREATMENT_COST_YEAR = 15_000  # THB/year
_HPT_TREATMENT_COST_YEAR = 8_000  # THB/year
_STROKE_TREATMENT_COST = 200_000  # THB/episode
_EARLY_DETECTION_SAVING_PCT = 0.40  # 30-50%, use midpoint


@app.get("/api/v2/strategy/cost-per-screening", tags=["Strategy"])
def cost_per_screening():
    """Cost per screening by district using standard cost reference (350 THB/person)."""
    rows = execute_query("""
        SELECT district_code, district_name, zone_code, total_screened,
               risk_dm_count, risk_hpt_count, found_obesity_count
        FROM summary_district_disease
        WHERE total_screened > 0
        ORDER BY total_screened DESC
    """)

    for r in rows:
        screened = r.get("total_screened") or 0
        r["screening_cost_thb"] = screened * _SCREENING_COST_PER_PERSON
        r["cost_per_person"] = _SCREENING_COST_PER_PERSON
        # Cost per risk case found (efficiency metric)
        total_risk = (r.get("risk_dm_count") or 0) + (r.get("risk_hpt_count") or 0)
        r["cost_per_risk_found"] = round(r["screening_cost_thb"] / total_risk, 0) if total_risk > 0 else None

    total_screened = sum(r.get("total_screened") or 0 for r in rows)
    total_cost = total_screened * _SCREENING_COST_PER_PERSON

    return {
        "cost_reference": {"screening_per_person_thb": _SCREENING_COST_PER_PERSON, "source": "สปสช. 2567"},
        "total_screened": total_screened,
        "total_cost_thb": total_cost,
        "districts": rows,
    }


@app.get("/api/v2/strategy/budget-allocation-model", tags=["Strategy"])
def budget_allocation_model(total_budget: float = Query(560_000_000, description="Total budget in THB")):
    """Allocate budget proportional to population x risk level per district."""
    rows = execute_query("""
        SELECT s.district_code, s.district_name, s.zone_code,
               s.total_screened, s.risk_dm_count, s.risk_hpt_count,
               d.population
        FROM summary_district_disease s
        JOIN ref_districts d ON d.dcode = s.district_code
        WHERE s.total_screened > 0
    """)

    # Score = population * (1 + risk_rate). Higher risk districts get more budget.
    for r in rows:
        screened = r.get("total_screened") or 1
        risk_rate = ((r.get("risk_dm_count") or 0) + (r.get("risk_hpt_count") or 0)) / screened
        r["risk_rate"] = round(risk_rate, 4)
        r["score"] = (r.get("population") or 0) * (1 + risk_rate)

    total_score = sum(r["score"] for r in rows) or 1
    for r in rows:
        r["allocation_pct"] = round(100.0 * r["score"] / total_score, 2)
        r["allocated_budget_thb"] = round(total_budget * r["score"] / total_score, 0)
        r["per_capita_thb"] = round(r["allocated_budget_thb"] / (r.get("population") or 1), 0)

    rows.sort(key=lambda x: x["allocated_budget_thb"], reverse=True)

    return {
        "total_budget_thb": total_budget,
        "model": "population_x_risk_weighted",
        "districts": rows,
    }


@app.get("/api/v2/strategy/roi-analysis", tags=["Strategy"])
def roi_analysis():
    """ROI = (prevented_treatment_cost - screening_cost) / screening_cost."""
    d = execute_query("""
        SELECT SUM(total_screened) as total, SUM(risk_dm_count) as dm,
               SUM(risk_hpt_count) as hpt, SUM(found_obesity_count) as obesity
        FROM summary_district_disease
    """)
    dd = d[0] if d else {}
    total = dd.get("total") or 0
    dm = dd.get("dm") or 0
    hpt = dd.get("hpt") or 0

    screening_cost = total * _SCREENING_COST_PER_PERSON

    # Early detection prevents progression: estimated savings
    dm_savings = dm * _DM_TREATMENT_COST_YEAR * _EARLY_DETECTION_SAVING_PCT
    hpt_savings = hpt * _HPT_TREATMENT_COST_YEAR * _EARLY_DETECTION_SAVING_PCT
    # Prevented strokes (estimate 5% of HPT would have stroke without intervention)
    stroke_prevented = int(hpt * 0.05)
    stroke_savings = stroke_prevented * _STROKE_TREATMENT_COST * _EARLY_DETECTION_SAVING_PCT

    total_savings = dm_savings + hpt_savings + stroke_savings
    net_benefit = total_savings - screening_cost
    roi = round(net_benefit / screening_cost, 2) if screening_cost > 0 else 0

    return {
        "screening_cost_thb": screening_cost,
        "prevented_costs": {
            "dm_early_treatment_savings": round(dm_savings, 0),
            "hpt_early_treatment_savings": round(hpt_savings, 0),
            "stroke_prevention_savings": round(stroke_savings, 0),
            "strokes_potentially_prevented": stroke_prevented,
        },
        "total_savings_thb": round(total_savings, 0),
        "net_benefit_thb": round(net_benefit, 0),
        "roi_ratio": roi,
        "roi_interpretation": f"ทุก 1 บาทที่ลงทุนคัดกรอง ได้ผลตอบแทน {roi} บาท",
        "assumptions": {
            "screening_cost_per_person": _SCREENING_COST_PER_PERSON,
            "dm_treatment_cost_year": _DM_TREATMENT_COST_YEAR,
            "hpt_treatment_cost_year": _HPT_TREATMENT_COST_YEAR,
            "stroke_treatment_cost": _STROKE_TREATMENT_COST,
            "early_detection_saving_pct": _EARLY_DETECTION_SAVING_PCT,
            "stroke_risk_in_hpt_pct": 5,
        },
    }


@app.get("/api/v2/strategy/resource-optimization", tags=["Strategy"])
def resource_optimization():
    """Rank districts by risk/resource ratio to identify under-served areas."""
    rows = execute_query("""
        SELECT s.district_code, s.district_name, s.zone_code,
               s.total_screened, s.risk_dm_count, s.risk_hpt_count,
               d.population,
               COUNT(c.id) as clinic_count
        FROM summary_district_disease s
        JOIN ref_districts d ON d.dcode = s.district_code
        LEFT JOIN ref_clinics c ON c.district_code = s.district_code
        WHERE s.total_screened > 0
        GROUP BY s.district_code, s.district_name, s.zone_code,
                 s.total_screened, s.risk_dm_count, s.risk_hpt_count, d.population
    """)

    for r in rows:
        pop = r.get("population") or 1
        screened = r.get("total_screened") or 0
        clinics = r.get("clinic_count") or 1
        total_risk = (r.get("risk_dm_count") or 0) + (r.get("risk_hpt_count") or 0)

        r["coverage_pct"] = round(100.0 * screened / pop, 2)
        r["risk_rate"] = round(100.0 * total_risk / screened, 2) if screened > 0 else 0
        r["population_per_clinic"] = round(pop / clinics, 0)
        r["screened_per_clinic"] = round(screened / clinics, 0)
        # Priority score: high risk + low coverage = needs more resources
        r["priority_score"] = round(r["risk_rate"] * (100 - r["coverage_pct"]) / 100, 2)

    rows.sort(key=lambda x: x["priority_score"], reverse=True)

    return {
        "model": "risk_adjusted_coverage_gap",
        "description": "Districts ranked by (risk_rate * coverage_gap). Higher = needs more resources.",
        "districts": rows,
    }


@app.get("/api/v2/strategy/projected-savings", tags=["Strategy"])
def projected_savings(
    target_coverage_pct: float = Query(80.0, description="Target screening coverage %"),
    years: int = Query(5, ge=1, le=10, description="Projection horizon in years"),
):
    """Estimate savings from early detection at target coverage levels."""
    pop = execute_scalar("SELECT COALESCE(SUM(population), 0) FROM ref_districts") or 0
    d = execute_query("""
        SELECT SUM(total_screened) as total, SUM(risk_dm_count) as dm,
               SUM(risk_hpt_count) as hpt
        FROM summary_district_disease
    """)
    dd = d[0] if d else {}
    current_screened = dd.get("total") or 0
    current_dm = dd.get("dm") or 0
    current_hpt = dd.get("hpt") or 0

    # Current risk rates
    dm_rate = current_dm / current_screened if current_screened > 0 else 0.10
    hpt_rate = current_hpt / current_screened if current_screened > 0 else 0.20

    target_screened = int(pop * target_coverage_pct / 100)
    additional_screened = max(0, target_screened - current_screened)

    # Project new cases found at current risk rates
    new_dm_found = int(additional_screened * dm_rate)
    new_hpt_found = int(additional_screened * hpt_rate)

    additional_screening_cost = additional_screened * _SCREENING_COST_PER_PERSON

    # Annual savings from early detection of new cases
    annual_dm_savings = new_dm_found * _DM_TREATMENT_COST_YEAR * _EARLY_DETECTION_SAVING_PCT
    annual_hpt_savings = new_hpt_found * _HPT_TREATMENT_COST_YEAR * _EARLY_DETECTION_SAVING_PCT
    strokes_prevented_annual = int(new_hpt_found * 0.05)
    annual_stroke_savings = strokes_prevented_annual * _STROKE_TREATMENT_COST * _EARLY_DETECTION_SAVING_PCT
    annual_total_savings = annual_dm_savings + annual_hpt_savings + annual_stroke_savings

    projections = []
    cumulative_savings = 0
    for y in range(1, years + 1):
        cumulative_savings += annual_total_savings
        net = cumulative_savings - additional_screening_cost
        projections.append({
            "year": y,
            "cumulative_savings_thb": round(cumulative_savings, 0),
            "net_benefit_thb": round(net, 0),
            "breakeven": net >= 0,
        })

    return {
        "current_coverage_pct": round(100.0 * current_screened / pop, 1) if pop else 0,
        "target_coverage_pct": target_coverage_pct,
        "additional_screenings_needed": additional_screened,
        "additional_screening_cost_thb": additional_screening_cost,
        "projected_new_cases": {"dm": new_dm_found, "hpt": new_hpt_found},
        "annual_savings_thb": round(annual_total_savings, 0),
        "projections": projections,
        "assumptions": {
            "screening_cost_per_person": _SCREENING_COST_PER_PERSON,
            "dm_treatment_cost_year": _DM_TREATMENT_COST_YEAR,
            "hpt_treatment_cost_year": _HPT_TREATMENT_COST_YEAR,
            "stroke_treatment_cost": _STROKE_TREATMENT_COST,
            "early_detection_saving_pct": _EARLY_DETECTION_SAVING_PCT,
            "stroke_risk_in_hpt_pct": 5,
        },
    }


# --------------------------------------------------------------------------- #
# Evaluator 8: วิจัย (Research)
# --------------------------------------------------------------------------- #


@app.get("/api/v2/research/individual-data", tags=["Research"])
def research_individual_data(
    format: str = Query("json", description="json|summary"),
    irb_approval: Optional[str] = Query(None),
):
    """Anonymized individual-level data for approved research."""
    if not irb_approval:
        return {"data_available": False,
                "message": "ต้องระบุ IRB approval number (irb_approval parameter) เพื่อเข้าถึงข้อมูลระดับบุคคล",
                "requirements": ["IRB approval from BMA Ethics Committee", "Data Use Agreement signed", "PDPA consent documented"]}

    # Return aggregated individual-level stats (NOT actual records)
    # This is a safe proxy: summarize the shape of individual data
    stats = execute_query("""
        SELECT
            COUNT(DISTINCT p.id) as total_patients,
            COUNT(DISTINCT v.id) as total_visits,
            COUNT(DISTINCT v.facility_code) as facilities,
            COUNT(DISTINCT v.district_code) as districts,
            MIN(v.visit_date) as date_range_start,
            MAX(v.visit_date) as date_range_end
        FROM raw_patients p
        LEFT JOIN raw_vitalsigns v ON p.id = v.patient_id AND v.cancel_status IS DISTINCT FROM 1
    """)

    s = stats[0] if stats else {}
    for k in ("date_range_start", "date_range_end"):
        if s.get(k) and hasattr(s[k], "isoformat"):
            s[k] = s[k].isoformat()

    return {
        "irb_approval": irb_approval,
        "data_shape": s,
        "anonymization": {
            "method": "HMAC-SHA256 on IDCARD with secret key",
            "pii_removed": ["ชื่อ-นามสกุล", "ที่อยู่", "เบอร์โทร", "LINE ID", "Email"],
            "age_generalized": "กลุ่มวัยไทย (6 กลุ่ม)",
            "k_anonymity": 5,
        },
        "access_procedure": "ส่ง IRB approval + Data Use Agreement ไปที่ สำนักการแพทย์ กทม. เพื่อขอ API key สำหรับ research tier",
        "note": "Endpoint นี้ส่งเฉพาะ metadata ไม่ส่งข้อมูลรายบุคคล — ต้องขอ research API key แยก",
    }


@app.get("/api/v2/research/statistical-test", tags=["Research"])
def statistical_test(
    test: str = Query("chi_square", description="chi_square|t_test|proportion"),
    var1: str = Query("disease"),
    var2: str = Query("age_group"),
):
    """Run basic statistical tests on aggregate data."""
    # Chi-square-like comparison using aggregate counts
    if test == "proportion":
        # Compare disease proportion between age groups
        rows = execute_query("""
            SELECT age_group, SUM(total_screened) as total,
                   SUM(risk_dm) as dm, SUM(risk_hpt) as hpt, SUM(found_obesity) as obesity
            FROM summary_disease_age_sex
            WHERE age_group != '__none__'
            GROUP BY age_group ORDER BY age_group
        """)
        rows = [r for r in rows if (r.get("total") or 0) >= K_ANONYMITY_THRESHOLD]
        if len(rows) >= 2:
            for r in rows:
                t = r.get("total") or 1
                r["pct_dm"] = round(100.0 * (r.get("dm") or 0) / t, 1)
                r["pct_hpt"] = round(100.0 * (r.get("hpt") or 0) / t, 1)
                r["pct_obesity"] = round(100.0 * (r.get("obesity") or 0) / t, 1)
            return {"test": "proportion_comparison", "groups": rows,
                    "note": "For formal hypothesis testing (chi-square, Fisher exact), use the research export with R/Python"}
        return {"data_available": False, "message": "กลุ่มข้อมูลไม่เพียงพอสำหรับการทดสอบทางสถิติ"}

    return {"test": test, "message": f"Statistical test '{test}' available via research export. Use R/Python/SPSS for formal analysis.",
            "available_tests_in_api": ["proportion"]}


@app.get("/api/v2/research/correlation-matrix", tags=["Research"])
def correlation_matrix():
    """Correlation matrix of key health variables (aggregate level)."""
    # Return district-level averages for correlation computation
    rows = execute_query("""
        SELECT d.district_code,
               COALESCE(s.total_screened, 0) as screened,
               COALESCE(s.pct_risk_dm, 0) as dm_pct,
               COALESCE(s.pct_risk_hpt, 0) as hpt_pct,
               COALESCE(s.pct_risk_cvd, 0) as cvd_pct,
               COALESCE(l.avg_fbs, 0) as avg_fbs,
               COALESCE(l.avg_hemoglobin, 0) as avg_hemoglobin,
               COALESCE(l.avg_cholesterol, 0) as avg_cholesterol,
               COALESCE(b.avg_bmi, 0) as avg_bmi
        FROM ref_districts d
        LEFT JOIN summary_district_disease s ON d.dcode = s.district_code
        LEFT JOIN summary_district_lab l ON d.dcode = l.district_code
        LEFT JOIN summary_bmi_waist b ON d.dcode = b.district_code AND b.sex = -1
        WHERE COALESCE(s.total_screened, 0) >= 5
        ORDER BY d.dcode
    """)
    return {"variables": ["screened", "dm_pct", "hpt_pct", "cvd_pct", "avg_fbs", "avg_hemoglobin", "avg_cholesterol", "avg_bmi"],
            "data": rows,
            "note": "Data is at district level (n=50). Compute Pearson/Spearman correlation in R/Python from this matrix."}


@app.get("/api/v2/research/sample-size-calculator", tags=["Research"])
def sample_size_calculator(
    prevalence: float = Query(0.15, description="Expected prevalence (0-1)"),
    precision: float = Query(0.05, description="Desired margin of error"),
    confidence: float = Query(0.95, description="Confidence level"),
    population: Optional[int] = Query(None, description="Population size (for finite correction)"),
):
    """Sample size calculator for health surveys."""
    import math
    # Z-scores for common confidence levels
    z_map = {0.90: 1.645, 0.95: 1.96, 0.99: 2.576}
    z = z_map.get(confidence, 1.96)

    p = prevalence
    e = precision
    # Infinite population formula
    n_inf = math.ceil((z**2 * p * (1-p)) / (e**2))

    # Finite population correction
    n_final = n_inf
    if population and population > 0:
        n_final = math.ceil(n_inf / (1 + (n_inf - 1) / population))

    pop_bkk = execute_scalar("SELECT SUM(population) FROM ref_districts") or 6063003
    n_bkk = math.ceil(n_inf / (1 + (n_inf - 1) / pop_bkk))

    return {
        "parameters": {"prevalence": p, "precision": e, "confidence": confidence, "z_score": z},
        "sample_size_infinite": n_inf,
        "sample_size_finite": n_final if population else None,
        "sample_size_bangkok": n_bkk,
        "bangkok_population": pop_bkk,
        "formula": "n = Z² × p × (1-p) / e² with finite population correction",
    }


@app.get("/api/v2/research/export", tags=["Research"])
def research_export(
    format: str = Query("json", description="json|csv_summary"),
    irb_approval: Optional[str] = Query(None),
):
    """Export aggregate data for research purposes."""
    if format == "csv_summary":
        return {"data_available": False,
                "message": "CSV export ต้องระบุ irb_approval และใช้ research API key",
                "available_without_irb": ["json (aggregate district-level data)"]}

    # Return aggregate data that's safe to share
    districts = execute_query("""
        SELECT s.district_code, s.district_name, s.zone_code, s.total_screened,
               s.risk_dm_count, s.risk_hpt_count, s.risk_cvd_count, s.risk_bmi_count,
               s.found_dm_count, s.found_hpt_count, s.found_obesity_count,
               s.found_dyslipidemia_count, s.found_stroke_count
        FROM summary_district_disease s
        WHERE s.total_screened >= 5
        ORDER BY s.district_code
    """)

    return {"format": "json", "type": "aggregate_district_level",
            "k_anonymity": 5, "records": len(districts), "data": districts}


# --------------------------------------------------------------------------- #
# Evaluator 9: ผอ.เขตสุขภาพ (Facility)
# --------------------------------------------------------------------------- #


@app.get("/api/v2/facility/workload", tags=["Facility"])
def facility_workload(zone_code: Optional[str] = Query(None)):
    """Workload per facility: screenings per facility."""
    conditions = ["s.total_screened >= 5"]
    params = []
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = "WHERE " + " AND ".join(conditions)

    rows = execute_query(f"""
        SELECT s.facility_code, s.district_code, d.name_th as district_name,
               s.total_screened, s.lab_completed,
               ROUND(100.0 * s.lab_completed / NULLIF(s.total_screened, 0), 1) as lab_completion_pct
        FROM summary_facility s
        JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        ORDER BY s.total_screened DESC
    """, tuple(params) or None)

    return {"facilities": rows, "note": "ข้อมูลจำนวนเจ้าหน้าที่ (staffing) ยังไม่มีในระบบ — ต้องเพิ่ม ref_staff table"}


@app.get("/api/v2/facility/screening-yield-rank", tags=["Facility"])
def facility_screening_yield_rank(zone_code: Optional[str] = Query(None), disease: str = Query("diabetes")):
    """Rank facilities by screening yield (risk found / screened)."""
    risk_col = {"diabetes": "risk_dm", "hypertension": "risk_hpt"}.get(disease, "risk_dm")
    conditions = ["s.total_screened >= 5"]
    params = []
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = "WHERE " + " AND ".join(conditions)

    rows = execute_query(f"""
        SELECT s.facility_code, s.district_code, d.name_th,
               s.total_screened, s.{risk_col} as risk_count,
               ROUND(100.0 * s.{risk_col} / NULLIF(s.total_screened, 0), 1) as yield_pct
        FROM summary_facility s
        JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        ORDER BY yield_pct DESC
    """, tuple(params) or None)

    # Add rank
    for i, r in enumerate(rows):
        r["rank"] = i + 1

    return {"disease": disease, "facilities": rows}


@app.get("/api/v2/facility/staff-performance", tags=["Facility"])
def staff_performance():
    """Staff-level screening performance."""
    return {"data_available": False,
            "message": "ข้อมูลผลงานรายเจ้าหน้าที่ไม่สามารถเปิดเผยผ่าน API ได้ — ต้องเข้าผ่าน Admin Panel",
            "reason": "PDPA: staff_code (FIRSTSTF) เป็นข้อมูลส่วนบุคคลของเจ้าหน้าที่"}


@app.get("/api/v2/facility/capacity-planning", tags=["Facility"])
def capacity_planning(zone_code: Optional[str] = Query(None), target_coverage: int = Query(80)):
    """Capacity planning: how many more screenings needed to reach target."""
    conditions = []
    params = []
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT d.dcode, d.name_th, d.population,
               COALESCE(s.total_screened, 0) as screened,
               ROUND(100.0 * COALESCE(s.total_screened, 0) / NULLIF(d.population, 0), 1) as current_coverage_pct,
               GREATEST(0, ROUND(d.population * %s / 100.0 - COALESCE(s.total_screened, 0))) as additional_needed
        FROM ref_districts d
        LEFT JOIN summary_district_disease s ON d.dcode = s.district_code
        {where}
        ORDER BY additional_needed DESC
    """, tuple([target_coverage] + params))

    total_needed = sum(r.get("additional_needed", 0) or 0 for r in rows)
    return {"target_coverage_pct": target_coverage, "districts": rows, "total_additional_needed": total_needed}


@app.get("/api/v2/facility/comparison", tags=["Facility"])
def facility_comparison(facility1: str = Query(...), facility2: str = Query(...)):
    """Compare 2 facilities side by side."""
    f1 = execute_query("SELECT * FROM summary_facility WHERE facility_code = %s", (facility1,))
    f2 = execute_query("SELECT * FROM summary_facility WHERE facility_code = %s", (facility2,))

    if not f1 or not f2:
        raise HTTPException(status_code=404, detail="One or both facilities not found")

    return {"facility_1": f1[0], "facility_2": f2[0]}


# --------------------------------------------------------------------------- #
# Evaluator 10: สภา กทม. (Public)
# --------------------------------------------------------------------------- #


@app.get("/api/v2/public/screening-locations", tags=["Public"])
def screening_locations(district: Optional[str] = Query(None)):
    """Health centers in district (from ref_facilities)."""
    conditions = []
    params = []
    if district:
        conditions.append("f.district_code = %s")
        params.append(district)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT f.code, f.name_th, f.name_en, f.facility_type,
               f.district_code, f.zone_code, f.latitude, f.longitude
        FROM ref_facilities f
        {where}
        ORDER BY f.code
    """, tuple(params) or None)

    if not rows:
        return {"locations": [], "note": "ยังไม่มีข้อมูลสถานที่คัดกรองใน ref_facilities — ต้องเพิ่ม seed data"}
    return {"locations": rows}


@app.get("/api/v2/public/health-tips", tags=["Public"])
def health_tips(risk: str = Query("diabetes")):
    """Health tips/recommendations based on risk factor."""
    tips = {
        "diabetes": {
            "disease_th": "เบาหวาน",
            "tips": [
                "ลดอาหารหวาน แป้ง น้ำตาล ข้าวขาว",
                "ออกกำลังกายอย่างน้อย 150 นาที/สัปดาห์",
                "ตรวจระดับน้ำตาลเป็นประจำทุก 6 เดือน",
                "รักษาน้ำหนักตัวให้อยู่ในเกณฑ์ BMI < 25",
                "งดสูบบุหรี่และลดแอลกอฮอล์",
            ],
            "warning_signs": ["ปัสสาวะบ่อย กระหายน้ำมาก", "น้ำหนักลดโดยไม่ทราบสาเหตุ", "แผลหายช้า ชาปลายมือปลายเท้า"],
            "where_to_go": "ศูนย์บริการสาธารณสุข กทม. ใกล้บ้านท่าน (69 แห่งทั่ว กทม.)",
        },
        "hypertension": {
            "disease_th": "ความดันโลหิตสูง",
            "tips": ["ลดเค็ม ลดโซเดียม", "ออกกำลังกายสม่ำเสมอ", "ลดน้ำหนักถ้า BMI > 25", "จัดการความเครียด", "วัดความดันเป็นประจำ"],
            "warning_signs": ["ปวดศีรษะรุนแรง", "ตาพร่ามัว", "เจ็บหน้าอก หายใจลำบาก"],
            "where_to_go": "ศูนย์บริการสาธารณสุข กทม.",
        },
        "obesity": {
            "disease_th": "โรคอ้วน",
            "tips": ["กินผักผลไม้เพิ่มขึ้น", "ลดอาหารทอด ของมัน", "เดิน 10,000 ก้าว/วัน", "ลดน้ำหวาน ชานม", "ลดบะหมี่กึ่งสำเร็จรูป"],
            "warning_signs": ["รอบเอว ≥ 90cm (ชาย) หรือ ≥ 80cm (หญิง)", "BMI ≥ 25", "หายใจลำบากเมื่อออกแรง"],
            "where_to_go": "ศูนย์บริการสาธารณสุข กทม.",
        },
    }

    if risk in tips:
        return tips[risk]
    return {"disease_th": risk, "tips": ["ปรึกษาแพทย์ที่ศูนย์บริการสาธารณสุข กทม. ใกล้บ้าน"], "where_to_go": "ศูนย์บริการสาธารณสุข กทม."}


@app.get("/api/v2/public/service-satisfaction", tags=["Public"])
def service_satisfaction(district: Optional[str] = Query(None)):
    """Service satisfaction survey results."""
    return {"data_available": False,
            "message": "ยังไม่มีข้อมูลความพึงพอใจในระบบ — ต้องเชื่อมกับระบบสำรวจความพึงพอใจ กทม.",
            "suggestion": "เพิ่ม satisfaction_surveys table หรือเชื่อมกับระบบ Traffy Fondue"}


@app.get("/api/v2/public/complaint-status", tags=["Public"])
def complaint_status(ticket: Optional[str] = Query(None)):
    """Complaint/service request status."""
    return {"data_available": False,
            "message": "ยังไม่มีระบบร้องเรียนในฐานข้อมูลสุขภาพ — ใช้ระบบ Traffy Fondue หรือ สายด่วน กทม. 1555",
            "links": {"traffy_fondue": "https://fondue.traffy.in.th", "hotline": "1555"}}


@app.get("/api/v2/public/open-data", tags=["Public"])
def open_data(format: str = Query("json")):
    """Open data portal: aggregate health data for transparency."""
    # Return district-level aggregate data that's safe for public
    districts = execute_query("""
        SELECT s.district_code, s.district_name, s.zone_code, s.total_screened,
               s.pct_risk_dm, s.pct_risk_hpt, s.pct_risk_cvd,
               ROUND(100.0 * s.found_obesity_count / NULLIF(s.total_screened, 0), 1) as pct_obesity,
               ROUND(100.0 * s.found_dyslipidemia_count / NULLIF(s.total_screened, 0), 1) as pct_dyslipidemia
        FROM summary_district_disease s
        WHERE s.total_screened >= 5
        ORDER BY s.district_code
    """)

    return {
        "license": "Creative Commons Attribution 4.0 (CC BY 4.0)",
        "source": "สำนักการแพทย์ กรุงเทพมหานคร",
        "description": "ข้อมูลรวมผลการคัดกรองสุขภาพ ระดับเขต (aggregate, ไม่มีข้อมูลส่วนบุคคล)",
        "k_anonymity": 5,
        "last_updated": datetime.utcnow().isoformat(),
        "format": format,
        "records": len(districts),
        "data": districts,
    }


# --------------------------------------------------------------------------- #
# Run with: uvicorn main:app --host 0.0.0.0 --port 8000
# --------------------------------------------------------------------------- #
