"""
Security utilities:
  - k-anonymity enforcement (suppress groups with count < K)
  - API key validation middleware
  - Rate limiting (in-memory, with optional Redis upgrade)
  - CORS configuration
"""
from __future__ import annotations

import hmac
import time
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Union

from fastapi import Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from config import API_KEY, CORS_ORIGINS, RATE_LIMIT_PUBLIC

# --------------------------------------------------------------------------- #
# k-anonymity
# --------------------------------------------------------------------------- #

K_ANONYMITY_THRESHOLD = 5


def enforce_k_anonymity(rows: List[Dict], count_field: str = "patient_count") -> List[Dict]:
    """Remove rows where the count field is below the k-anonymity threshold.

    Suppressed rows are completely excluded from results to prevent
    differential attacks that infer counts by subtraction.
    """
    result = []
    for row in rows:
        count_val = row.get(count_field, 0) or 0
        if count_val >= K_ANONYMITY_THRESHOLD:
            result.append(dict(row))
    return result


def suppress_scalar_if_small(value: Optional[int]) -> Optional[int]:
    """Return None if the aggregate count is below the k-anonymity threshold."""
    if value is None:
        return None
    return value if value >= K_ANONYMITY_THRESHOLD else None


# --------------------------------------------------------------------------- #
# API key middleware
# --------------------------------------------------------------------------- #

# Paths that do NOT require an API key.
# /metrics is the Prometheus exposition endpoint — scrapers don't carry the
# X-API-Key header. The metric surface contains no PII (counts/latencies
# only) so public exposure is by design.
_PUBLIC_PATHS = frozenset({"/health", "/docs", "/redoc", "/openapi.json", "/metrics"})
# Path prefixes exempt from API key
#
# /api/admin/upload-excel* is exempt because:
#   1. Next.js Edge middleware (which injects X-API-Key in dev) has a 4MB
#      body cap and 30s timeout — large uploads (>4MB) get truncated by
#      the middleware buffer, leading to "Empty upload" 400s. Bypassing
#      middleware on this path requires the FastAPI side to also accept
#      requests without X-API-Key.
#   2. The route is independently auth-protected by `require_admin_
#      session_or_bearer` (JWT cookie OR BMA_ADMIN_TOKEN bearer), so
#      removing X-API-Key here doesn't widen the security surface.
_PUBLIC_PREFIXES = ("/api/auth/", "/api/admin/upload-excel")


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Validate X-API-Key header on every request except health/docs."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith("/admin") or path.startswith("/static"):
            return await call_next(request)
        if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        provided = request.headers.get("X-API-Key")
        if not provided or not hmac.compare_digest(provided, API_KEY):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
            )
        return await call_next(request)


# --------------------------------------------------------------------------- #
# In-memory rate limiter (sliding window per IP)
# --------------------------------------------------------------------------- #

class _SlidingWindowCounter:
    """Simple in-memory sliding-window rate limiter."""

    def __init__(self, max_requests: int, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: Dict[str, List[float]] = defaultdict(list)
        self._call_count = 0

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        cutoff = now - self.window
        # Prune old entries for this key
        self._hits[key] = [t for t in self._hits[key] if t > cutoff]
        if len(self._hits[key]) >= self.max_requests:
            return False
        self._hits[key].append(now)

        # Periodic global cleanup every 100 calls
        self._call_count += 1
        if self._call_count >= 100:
            self._call_count = 0
            stale_keys = [k for k, v in self._hits.items() if not v or v[-1] < cutoff]
            for k in stale_keys:
                del self._hits[k]

        return True


_limiter = _SlidingWindowCounter(max_requests=RATE_LIMIT_PUBLIC, window_seconds=60)

# Stricter limit specifically for admin login — prevents brute-force.
# 10 attempts per IP per 5 minutes (separate counter from the global one).
_admin_login_limiter = _SlidingWindowCounter(max_requests=10, window_seconds=300)

# Stricter generic limit for the rest of /admin (still much more permissive
# than /admin/login since admins making legit requests need headroom).
_admin_limiter = _SlidingWindowCounter(max_requests=RATE_LIMIT_PUBLIC * 2, window_seconds=60)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP rate limiting.

    - /static and pure docs paths are exempt (cacheable static assets).
    - /admin/login uses a strict counter (10/5min) to deter brute-force.
    - Other /admin paths use a generous counter (so a logged-in admin
      doing legitimate dashboard work isn't throttled, but a flood is).
    - Everything else uses the public counter.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"

        # Admin login gets the strictest limit (brute-force protection)
        if path == "/admin/login" and request.method == "POST":
            if not _admin_login_limiter.is_allowed(client_ip):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many login attempts. Try again in 5 minutes."},
                )
        # Other /admin paths get a generous-but-not-unlimited limit
        elif path.startswith("/admin"):
            if not _admin_limiter.is_allowed(client_ip):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Admin rate limit exceeded. Try again later."},
                )
        else:
            if not _limiter.is_allowed(client_ip):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Try again later."},
                )
        return await call_next(request)


# --------------------------------------------------------------------------- #
# CORS helper
# --------------------------------------------------------------------------- #

def add_cors(app):
    """Attach CORS middleware to the FastAPI app.

    Refuses to start if CORS_ORIGINS contains "*" while ENVIRONMENT=production
    (FastAPI/Starlette also disables credentials with wildcard, but the
    misconfiguration is dangerous enough to fail loud).
    """
    import os
    is_prod = os.getenv("ENVIRONMENT", "").strip().lower() == "production"
    if is_prod and ("*" in CORS_ORIGINS or any(o.strip() == "*" for o in CORS_ORIGINS)):
        raise RuntimeError(
            "FATAL — CORS_ORIGINS contains '*' in production. "
            "Specify exact frontend origins (e.g. 'https://bma-health.pages.dev')."
        )
    if not CORS_ORIGINS:
        raise RuntimeError(
            "CORS_ORIGINS is empty. Set CORS_ORIGINS in your .env file."
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key", "Content-Type"],
    )
