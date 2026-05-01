"""
BMA Health Database -- One-Stop Backend API

Serves AGGREGATE / SUMMARY health data, LLM chat, LaTeX reports, and exports.
  - NO individual records
  - NO PII (idcard_hash, patient_id, staff_code never exposed)
  - k-anonymity >= 5 enforced on filtered queries
  - API key required (X-API-Key header)
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from database import execute_scalar, close_pool
from security import APIKeyMiddleware, RateLimitMiddleware, add_cors
from errors import BMAException, bma_exception_handler, unhandled_exception_handler
from cache import cache_stats
from admin import router as admin_router, upload_excel_router, start_upload_janitor
from auth import router as auth_router
from config import validate_production_config

# Routers
from routers.summary import router as summary_router
from routers.zones import router as zones_router
from routers.districts import router as districts_router
from routers.epidemiology import router as epidemiology_router
from routers.trends import router as trends_router
from routers.search import router as search_router
from routers.kpi import router as kpi_router
from routers.executive import router as executive_router
from routers.promotion import router as promotion_router
from routers.disease_control import router as disease_control_router
from routers.facility import router as facility_router
from routers.strategy import router as strategy_router
from routers.research import router as research_router
from routers.public import router as public_router
from routers.monitoring import router as monitoring_router
from routers.gis import router as gis_router

# --- New routers (one-stop backend) ---
_new_routers = []
try:
    from routers.chat import router as chat_router
    _new_routers.append(chat_router)
except ImportError:
    pass
try:
    from routers.reports import router as reports_router
    _new_routers.append(reports_router)
except ImportError:
    pass
try:
    from routers.export import router as export_router
    _new_routers.append(export_router)
except ImportError:
    pass
try:
    from routers.statistics_v1 import router as stats_v1_router
    _new_routers.append(stats_v1_router)
except ImportError:
    pass
try:
    from routers.dashboard_v1 import router as dashboard_v1_router
    _new_routers.append(dashboard_v1_router)
except ImportError:
    pass
try:
    from routers.factors import router as factors_router
    _new_routers.append(factors_router)
except ImportError:
    pass
try:
    from routers.screening_tests import router as screening_tests_router
    _new_routers.append(screening_tests_router)
except ImportError:
    pass
try:
    from routers.admin_api import router as admin_api_router
    _new_routers.append(admin_api_router)
except ImportError:
    pass
try:
    from routers.charts import router as charts_router
    _new_routers.append(charts_router)
except ImportError:
    pass
try:
    from routers.reports_v2 import router as reports_v2_router
    _new_routers.append(reports_v2_router)
except ImportError:
    pass
try:
    from routers.chat_v2 import router as chat_v2_router
    _new_routers.append(chat_v2_router)
except ImportError:
    pass

# --------------------------------------------------------------------------- #
# Audit logging
# --------------------------------------------------------------------------- #

_audit_logger = logging.getLogger("bma.audit")
_audit_logger.setLevel(logging.INFO)
if not _audit_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter('%(asctime)s AUDIT %(message)s'))
    _audit_logger.addHandler(_handler)


class AuditMiddleware(BaseHTTPMiddleware):
    """Log API access for audit trail. Never logs PII."""

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        duration = time.time() - start

        path = request.url.path
        if path in ("/health", "/docs", "/redoc", "/openapi.json"):
            return response
        if path.startswith("/admin") or path.startswith("/static"):
            return response

        client_ip = request.client.host if request.client else "unknown"
        # Note: request.url.path excludes the query string by design — we never
        # log raw query params because callers might pass small-cell identifiers
        # (district + age + sex tuples) that are PII-adjacent. If you need to
        # debug a specific request, instrument the route handler explicitly
        # with whitelisted, sanitised parameter names.
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
    # Start nightly report scheduler if available
    try:
        from services.scheduler import start_scheduler
        start_scheduler()
    except ImportError:
        pass
    # Periodic cleanup of stale 'pending_confirm' upload-excel tmpdirs.
    try:
        start_upload_janitor()
    except Exception:
        pass
    yield
    close_pool()


# OpenAPI tag metadata — gives each domain group a description and
# rendering order in /docs and /redoc.
_OPENAPI_TAGS = [
    {"name": "System", "description": "Health checks, dependency probes, and server-info endpoints."},
    {"name": "Public", "description": "PDPA-safe endpoints for public consumption (no auth, k-anonymity >= 5)."},
    {"name": "summary", "description": "Pre-aggregated summary tables — fast dashboard endpoints."},
    {"name": "zones", "description": "8 BMA Health Zones — list, detail, and zone-level aggregates."},
    {"name": "districts", "description": "50 Bangkok districts — list, detail, and district-level aggregates."},
    {"name": "epidemiology", "description": "Disease prevalence, age-group analysis, comorbidity matrix."},
    {"name": "trends", "description": "Time-series data: monthly/quarterly/yearly trends."},
    {"name": "kpi", "description": "MOPH NCD KPI tracking — coverage, detection, control rates."},
    {"name": "executive", "description": "Governor/exec dashboards: headline KPIs, alerts, year-over-year."},
    {"name": "factors", "description": "Disease risk by demographic/behaviour factors (sex, age, occupation, smoking, etc.)."},
    {"name": "Reports", "description": "PDF report generation (LaTeX/Tectonic). Cached, hash-validated."},
    {"name": "GIS", "description": "Geographic data: facility locations, district boundaries, PM2.5 overlay."},
    {"name": "stats", "description": "Statistical tests: chi-square, Welch's t-test, ranking, comparison."},
    {"name": "chat", "description": "LLM chat (SSE streaming). Powered by LMStudio + Gemma."},
    {"name": "Admin", "description": "Admin web UI: CSV upload, ETL, dashboard, history."},
    {"name": "Admin API", "description": "JSON admin endpoints: data status, audit log, Excel upload."},
    {"name": "Monitoring", "description": "Data quality, ETL status, query performance."},
]

app = FastAPI(
    title="BMA Health One-Stop Backend API",
    version="4.0.0",
    description=(
        "ระบบฐานข้อมูลสุขภาพ กรุงเทพมหานคร — One-Stop Backend\n\n"
        "Aggregate health screening data for Bangkok Metropolitan Administration.\n"
        "No PII. k-anonymity >= 5 enforced.\n\n"
        "**100+ endpoints** across 24 domain groups:\n"
        "V2 data API, LLM chat (SSE streaming), LaTeX/PDF reports,\n"
        "Excel export, statistics, dashboards, factor analysis,\n"
        "GIS, PM2.5 overlay, and diet-disease analysis.\n"
        "Redis caching with 4-tier TTL. Structured error handling."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    openapi_tags=_OPENAPI_TAGS,
)

# Middleware order: CORS -> Rate Limit -> API Key -> Audit
add_cors(app)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(APIKeyMiddleware)
app.add_middleware(AuditMiddleware)

# Global error handlers
app.add_exception_handler(BMAException, bma_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

# --------------------------------------------------------------------------- #
# Mount routers
# --------------------------------------------------------------------------- #

app.include_router(admin_router)
# IMPORTANT: include the new /api/admin/upload-excel router BEFORE
# admin_api_router (added later via _new_routers loop) so the new
# auth-protected, pipeline-driven endpoints win over the legacy
# /upload-excel handler in routers/admin_api.py for duplicate paths.
app.include_router(upload_excel_router)
app.include_router(auth_router)
app.include_router(summary_router)
app.include_router(zones_router)
app.include_router(districts_router)
app.include_router(epidemiology_router)
app.include_router(trends_router)
app.include_router(search_router)
app.include_router(kpi_router)
app.include_router(executive_router)
app.include_router(promotion_router)
app.include_router(disease_control_router)
app.include_router(facility_router)
app.include_router(strategy_router)
app.include_router(research_router)
app.include_router(public_router)
app.include_router(monitoring_router)
app.include_router(gis_router)

# --- New routers (one-stop backend) ---
for _r in _new_routers:
    app.include_router(_r)

# Static files
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# --------------------------------------------------------------------------- #
# Health check (no API key required — in middleware PUBLIC_PATHS)
# --------------------------------------------------------------------------- #


@app.get("/health", tags=["System"])
def health_check(deep: bool = False):
    """Liveness probe.

    Default response is fast: only checks the DB (the one truly required
    dependency). Pass `?deep=1` for a full readiness probe that also pings
    Redis, LMStudio and Tectonic — useful for load balancers / on-call
    diagnosis but slower (~100-300ms vs ~5ms).

    Status semantics:
      - "ok":       DB up and (in deep mode) every required dep up.
      - "degraded": DB up but at least one optional dep is down.
      - "down":    DB unreachable (return HTTP 503).
    """
    from fastapi.responses import JSONResponse

    db_ok = False
    try:
        result = execute_scalar("SELECT 1")
        db_ok = result == 1
    except Exception:
        pass

    body: dict = {
        "status": "ok" if db_ok else "down",
        "database": "connected" if db_ok else "disconnected",
        "timestamp": datetime.utcnow().isoformat(),
    }

    if deep:
        deps: dict = {}

        # Cache (Redis) — already exposed via cache_stats() but also include in deps
        try:
            cs = cache_stats()
            deps["redis"] = "connected" if cs.get("available") else "disconnected"
            body["cache"] = cs
        except Exception:
            deps["redis"] = "error"

        # DB pool saturation
        try:
            from database import get_pool_status
            body["db_pool"] = get_pool_status()
        except Exception:
            body["db_pool"] = {"error": "unavailable"}

        # LMStudio (LLM) — quick HTTP check, short timeout
        try:
            import httpx
            from config import LMSTUDIO_URL
            r = httpx.get(f"{LMSTUDIO_URL}/v1/models", timeout=2.0)
            deps["lmstudio"] = "connected" if r.status_code == 200 else f"http {r.status_code}"
        except Exception as e:
            deps["lmstudio"] = f"unreachable ({type(e).__name__})"

        # Tectonic (PDF compiler) — file existence + executability
        try:
            from config import TECTONIC_PATH
            deps["tectonic"] = "available" if os.path.exists(TECTONIC_PATH) and os.access(TECTONIC_PATH, os.X_OK) else "missing"
        except Exception:
            deps["tectonic"] = "error"

        body["dependencies"] = deps
        # Downgrade to "degraded" if any dep is unhealthy (DB still up).
        if db_ok and any(v not in ("connected", "available") for v in deps.values()):
            body["status"] = "degraded"
    else:
        # Shallow mode: include cache stats since it's cheap (one Redis ping)
        body["cache"] = cache_stats()

    if not db_ok:
        return JSONResponse(status_code=503, content=body)
    return body
