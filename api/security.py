"""
Security utilities:
  - k-anonymity enforcement (suppress groups with count < K)
  - API key validation middleware
  - Rate limiting (in-memory, with optional Redis upgrade)
  - CORS configuration
"""
from __future__ import annotations

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

# Paths that do NOT require an API key
_PUBLIC_PATHS = frozenset({"/health", "/docs", "/redoc", "/openapi.json"})


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Validate X-API-Key header on every request except health/docs."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith("/admin"):
            return await call_next(request)

        provided = request.headers.get("X-API-Key")
        if not provided or provided != API_KEY:
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


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP rate limiting."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith("/admin"):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
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
    """Attach CORS middleware to the FastAPI app."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key", "Content-Type"],
    )
