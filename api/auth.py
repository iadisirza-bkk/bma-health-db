"""
Session-cookie authentication for the FastAPI backend (S1).

Goals
-----
* Browsers visiting /admin (and the React/SPA admin shell) get a JSON login
  endpoint that, on success, writes an `HttpOnly Secure SameSite=Strict`
  cookie carrying a signed JWT.
* Existing Bearer-token auth on `/api/admin/*` keeps working for CLI/curl/CI;
  callers may use EITHER cookie OR Bearer, never both required.
* Single-account: the only credential is the `ADMIN_PASSWORD` env var.
  Multi-user with a DB-backed user table is S2.

This module is intentionally small (~120 lines) and self-contained — the
Bearer-token logic lives in `admin._require_admin` and we delegate to it
from the dual-auth dependency below to avoid duplication.
"""
from __future__ import annotations

import os
import time
import hmac
import secrets
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from jose import jwt, JWTError
from pydantic import BaseModel

from config import ADMIN_PASSWORD, SECRET_KEY

logger = logging.getLogger("auth")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SESSION_COOKIE_NAME = "bma_session"
SESSION_TTL_HOURS = int(os.environ.get("BMA_SESSION_TTL_HOURS", "8"))
_JWT_ALG = "HS256"
_FAILED_LOGIN_DELAY_SEC = 0.25  # slow-path brute-force


# --------------------------------------------------------------------------- #
# JWT helpers
# --------------------------------------------------------------------------- #

def _make_jwt(sub: str) -> str:
    """Sign a session JWT for ``sub`` valid for SESSION_TTL_HOURS."""
    now = int(time.time())
    payload = {
        "sub": sub,
        "exp": now + SESSION_TTL_HOURS * 3600,
        "iat": now,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=_JWT_ALG)


def _verify_jwt(token: str) -> Optional[dict]:
    """Return claims dict on a valid signature + non-expired token, else None."""
    if not token:
        return None
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[_JWT_ALG])
    except JWTError:
        return None


# --------------------------------------------------------------------------- #
# Cookie helpers
# --------------------------------------------------------------------------- #

def _cookie_secure(request: Request) -> bool:
    """Whether to send the cookie with the Secure flag.

    True when:
      * the inbound request came over HTTPS, OR
      * operator set BMA_FORCE_SECURE_COOKIE=1 (e.g. behind a TLS-terminating
        proxy where request.url.scheme is "http" but the public origin is
        "https").

    On localhost dev (HTTP, no override) we leave it off so browsers actually
    accept the cookie.
    """
    if os.environ.get("BMA_FORCE_SECURE_COOKIE", "").strip() == "1":
        return True
    return request.url.scheme == "https"


def _set_session_cookie(response: Response, token: str, *, request: Request) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_HOURS * 3600,
        path="/",
        httponly=True,
        samesite="strict",
        secure=_cookie_secure(request),
    )


def _clear_session_cookie(response: Response) -> None:
    # Mirror the path so browsers actually clear the right cookie.
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #

_BMA_MED_ROOT = "/Users/dev/bma-med"


def _audit_login(operator: str, operation: str, request: Request) -> None:
    """Best-effort audit-log write for login attempts (success and failure).

    Never raises — auditing is informational and must not block authentication.
    Mirrors the import pattern used in admin._audit so we don't take a hard
    dependency on the bma_med package being importable at module load time.
    """
    try:
        import sys as _sys
        if _BMA_MED_ROOT not in _sys.path:
            _sys.path.insert(0, _BMA_MED_ROOT)
        from security.audit import audit_event  # type: ignore

        ip = request.client.host if request.client else "unknown"
        ev = audit_event(
            operator=operator or "anonymous",
            operation=operation,
            resource="api.auth.login",
            detail={"ip": ip},
        )
        logger.info("audit-log[%s]: %s", operation, ev)
    except Exception as exc:  # noqa: BLE001 — audit is best-effort
        logger.warning("audit-log failed (non-fatal) [%s]: %s", operation, exc)


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #

def require_session(request: Request) -> str:
    """FastAPI dependency: require a valid session cookie.

    Returns the authenticated `sub` (currently always "admin").
    Raises 401 on absence or invalid/expired token.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    claims = _verify_jwt(token)
    if not claims or not claims.get("sub"):
        raise HTTPException(status_code=401, detail="not authenticated")
    return str(claims["sub"])


def require_admin_session_or_bearer(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> str:
    """Accept EITHER a valid session cookie OR a valid Bearer token.

    Use this on endpoints that must work both from the browser admin UI
    (cookie) and from CLI/curl/CI (`Authorization: Bearer …`).

    Order of attempts:
      1. Session cookie (cheap — no env lookup needed).
      2. Bearer token via the existing `admin._require_admin` (preserves the
         "BMA_ADMIN_TOKEN unset → 503" fail-closed behaviour for the JSON API).

    Returns the authenticated principal name on success; raises 401/503 on
    failure.
    """
    # --- 1. Cookie path ---
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    claims = _verify_jwt(token)
    if claims and claims.get("sub"):
        return str(claims["sub"])

    # --- 2. Browser request with no/invalid cookie AND no Bearer header → 401 ---
    # We short-circuit BEFORE delegating to _require_admin so a logged-out
    # browser (no cookie, no Authorization) gets a clean 401 the frontend
    # can act on (redirect to /login). Otherwise the Bearer fallback's
    # "BMA_ADMIN_TOKEN not configured" 503 would mislead the operator.
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail={"error": "authentication required",
                    "hint": "POST /api/auth/login or supply Authorization: Bearer …"},
        )

    # --- 3. Bearer path (delegate to the existing implementation) ---
    # Imported lazily to avoid a circular import at module load time.
    from admin import _require_admin
    _require_admin(authorization)  # raises HTTPException on failure
    return "admin"


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(req: LoginRequest, request: Request, response: Response):
    """Validate credentials, set session cookie on success.

    On failure we sleep 250 ms before responding to slow brute-force probes
    (the per-IP /admin/login rate-limiter still applies on top of this for
    the HTML form path; the JSON API path is rate-limited by the global
    public limiter — see security.RateLimitMiddleware).
    """
    submitted = (req.password or "").encode("utf-8")
    expected = (ADMIN_PASSWORD or "").encode("utf-8")
    ok = bool(expected) and secrets.compare_digest(submitted, expected)

    if not ok:
        _audit_login(req.username or "anonymous", "LOGIN_FAIL", request)
        time.sleep(_FAILED_LOGIN_DELAY_SEC)
        raise HTTPException(status_code=401, detail={"error": "invalid credentials"})

    # Success — issue a session JWT.
    sub = "admin"
    token = _make_jwt(sub)
    _set_session_cookie(response, token, request=request)
    _audit_login(req.username or sub, "LOGIN_OK", request)
    return {
        "ok": True,
        "user": sub,
        "expires_in": SESSION_TTL_HOURS * 3600,
    }


@router.post("/logout")
def logout(response: Response):
    """Delete the session cookie (best-effort — cookie may already be absent)."""
    _clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    """Return the authenticated principal, or 401 if no/expired session."""
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    claims = _verify_jwt(token)
    if not claims or not claims.get("sub"):
        raise HTTPException(status_code=401, detail={"authenticated": False})
    return {"authenticated": True, "user": str(claims["sub"])}
