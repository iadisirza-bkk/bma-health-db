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
from admin import router as admin_router
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
    version="3.0.0",
    description=(
        "ระบบฐานข้อมูลสุขภาพ กรุงเทพมหานคร — Summary API v3\n\n"
        "Aggregate health screening data for Bangkok Metropolitan Administration.\n"
        "No PII. k-anonymity >= 5 enforced.\n\n"
        "**85+ endpoints** across 16 domain groups including GIS, PM2.5 overlay, and diet-disease analysis.\n"
        "**13 MCP tools** for LLM agent access via shared service layer.\n"
        "Redis caching with 4-tier TTL. Structured error handling."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
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

# Static files
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# --------------------------------------------------------------------------- #
# Health check (no API key required — in middleware PUBLIC_PATHS)
# --------------------------------------------------------------------------- #


@app.get("/health", tags=["System"])
def health_check():
    db_ok = False
    try:
        result = execute_scalar("SELECT 1")
        db_ok = result == 1
    except Exception:
        pass

    cache = cache_stats()
    status = "ok" if db_ok else "degraded"
    return {
        "status": status,
        "database": "connected" if db_ok else "disconnected",
        "cache": cache,
        "timestamp": datetime.utcnow().isoformat(),
    }
