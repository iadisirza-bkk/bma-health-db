"""
BMA Health DB -- Admin Panel Backend

Provides CSV upload, ETL import, dashboard, and import history routes.
All routes are mounted under /admin and require session authentication.
"""
from __future__ import annotations

from typing import Any, Optional, List, Dict, Literal

import asyncio
import hashlib
import hmac
import importlib.util
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime
from io import StringIO
from pathlib import Path

import pandas as pd
import psycopg2
from fastapi import (
    APIRouter, Request, UploadFile, File, Form, HTTPException,
    Header, BackgroundTasks,
)
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from database import (
    execute_query as _execute_query_reader,
    execute_scalar as _execute_scalar_reader,
    get_conn,
    get_writer_conn,
)

# Admin endpoints write to public.import_history and bma_med.* — always use
# the writer pool (etl_user). Auth is enforced via _require_auth + CSRF before
# any DB call.
#
# Override get_conn → get_writer_conn for the admin module.
get_conn = get_writer_conn


def execute_query(sql: str, params=None):
    """Admin variant: runs through writer pool (etl_user). The reader pool
    (api_user) has zero access to writer-only tables."""
    import psycopg2.extras as _extras
    with get_writer_conn() as conn:
        with conn.cursor(cursor_factory=_extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            try:
                return cur.fetchall()
            except psycopg2.ProgrammingError:
                return []


def execute_scalar(sql: str, params=None):
    """Admin variant of execute_scalar — uses writer pool."""
    with get_writer_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None
from config import DATABASE_URL, DATABASE_URL_WRITER

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

logger = logging.getLogger("admin")

# --------------------------------------------------------------------------- #
# ETL imports (loaded from file path to avoid config module name collision)
# --------------------------------------------------------------------------- #

ETL_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "etl")

_etl_mod = None
_etl_mtime: Optional[float] = None


def _load_etl():
    """Lazy-load the legacy MV-refresh helper (etl/refresh_legacy_summaries.py).

    All v1 ETL logic has been removed; only `refresh_all_summaries(cur)` is
    still called from /admin/refresh and /admin/erasure. Uses mtime to reload
    on edits without restarting the API server, mirroring _load_etl_v3.
    """
    global _etl_mod, _etl_mtime
    etl_path = os.path.join(ETL_DIR, "refresh_legacy_summaries.py")
    try:
        current_mtime = os.path.getmtime(etl_path)
    except OSError:
        current_mtime = None

    if _etl_mod is not None and current_mtime == _etl_mtime:
        return _etl_mod

    spec = importlib.util.spec_from_file_location("etl_refresh_legacy", etl_path)
    _etl_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_etl_mod)
    _etl_mtime = current_mtime
    if _etl_mtime is not None:
        logger.info("Loaded etl/refresh_legacy_summaries.py (mtime=%s)", _etl_mtime)
    return _etl_mod


# v3 ETL loader — same lazy-mtime pattern, separate module.
_etl_v3_mod = None
_etl_v3_mtime = None


def _load_etl_v3():
    """Lazy-load etl/import_csv_v3.py by absolute path.

    Avoids `from etl import …` which fails because uvicorn runs from api/
    and etl/ is a sibling without __init__.py — adding sys.path manipulation
    would be brittle.
    """
    global _etl_v3_mod, _etl_v3_mtime
    etl_path = os.path.join(ETL_DIR, "import_csv_v3.py")
    try:
        current_mtime = os.path.getmtime(etl_path)
    except OSError:
        current_mtime = None

    if _etl_v3_mod is not None and current_mtime == _etl_v3_mtime:
        return _etl_v3_mod

    spec = importlib.util.spec_from_file_location("etl_import_v3", etl_path)
    _etl_v3_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_etl_v3_mod)
    _etl_v3_mtime = current_mtime
    if _etl_v3_mtime is not None:
        logger.info("Loaded etl/import_csv_v3.py (mtime=%s)", _etl_v3_mtime)
    return _etl_v3_mod

CURRENT_YEAR = int(os.getenv("CURRENT_YEAR", str(datetime.now().year)))


# --------------------------------------------------------------------------- #
# v3 schema migration banner — for pages whose queries pre-date migration 105
# --------------------------------------------------------------------------- #
# Migration 105 dropped the legacy `raw_*` MVs and the four analytical pages
# (data-quality, cleansing-report, cross-stats, agreement) still query the
# old `raw_patients / raw_visits / raw_vitalsigns / raw_lab_results / …`
# tables. On a v3-only deployment the queries either return empty results
# (legacy compat tables exist but are empty) or fail outright.
#
# Until the pages are rewritten against `bma_med.*` + `public.mv_*`,
# render a banner so the operator isn't fooled by silent zeros. We treat
# the page as "pending v3 rewrite" when EVERY raw_* canonical table has
# zero rows (i.e. no legacy data is present).
# --------------------------------------------------------------------------- #

_RAW_LEGACY_TABLES = (
    "raw_patients", "raw_visits", "raw_vitalsigns",
    "raw_homevisit", "raw_homehealth",
    "raw_lab_results", "raw_lab_extended",
)


def _legacy_raw_has_data() -> bool:
    """True iff at least ONE raw_* canonical table has rows.

    On a v3-only DB these tables exist as empty compat shells (see
    db/migrations/105_*) but never see new inserts. If no raw_* table has
    data, the four analytical pages cannot produce meaningful output and
    the page should render the "v3 migration pending" banner.
    """
    for tbl in _RAW_LEGACY_TABLES:
        try:
            n = execute_scalar(f'SELECT 1 FROM "{tbl}" LIMIT 1')
            if n is not None:
                return True
        except Exception:
            # Table doesn't exist (deeper-v3 deploy where migration 105 was
            # extended to drop raw_*). Treat as no data.
            continue
    return False


# --------------------------------------------------------------------------- #
# Import concurrency lock
# --------------------------------------------------------------------------- #
# PostgreSQL session-level advisory lock key — prevents two imports running
# simultaneously (which would corrupt patient_map / cause TRUNCATE conflicts).
# The lock is held by the import connection and released when it closes.
IMPORT_LOCK_KEY = 0xBA10AD17  # arbitrary stable bigint


def _try_acquire_import_lock(cur) -> bool:
    """Try to acquire the import lock. Returns True if acquired, False if busy."""
    cur.execute("SELECT pg_try_advisory_lock(%s)", (IMPORT_LOCK_KEY,))
    return bool(cur.fetchone()[0])


def _release_import_lock(cur) -> None:
    """Release the import lock. Safe to call even if not held."""
    try:
        cur.execute("SELECT pg_advisory_unlock(%s)", (IMPORT_LOCK_KEY,))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Per-source delete (replaces TRUNCATE-everything for multi-source imports)
# --------------------------------------------------------------------------- #
# When the new multi-source schema (migration 011) is applied, each raw row
# is tagged with data_source ∈ {'portal','app1','app2'}. A bundle import for
# a single source must NOT wipe data from the other two sources. We delete
# only the targeted sources here; rollback still restores everything because
# the deletes happen inside the import transaction.
#
# Old single-source schema (pre-011) has no data_source column → fall back to
# TRUNCATE CASCADE (existing behaviour). The check is one cheap query per
# import; the result is cached for the lifetime of the process.

_HAS_DATA_SOURCE_COL: Optional[bool] = None


def _has_data_source_column(cur) -> bool:
    """Cached check: is migration 011 applied? (data_source column exists)"""
    global _HAS_DATA_SOURCE_COL
    if _HAS_DATA_SOURCE_COL is not None:
        return _HAS_DATA_SOURCE_COL
    cur.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'raw_patients'
              AND column_name = 'data_source'
        )
    """)
    _HAS_DATA_SOURCE_COL = bool(cur.fetchone()[0])
    return _HAS_DATA_SOURCE_COL


# --------------------------------------------------------------------------- #
# Authentication helpers
# --------------------------------------------------------------------------- #

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")

# --------------------------------------------------------------------------- #
# Session store — Redis-backed when available, in-memory fallback otherwise.
# --------------------------------------------------------------------------- #
# Multi-instance deployments need Redis; otherwise a session created on
# instance A is rejected by instance B. In-memory fallback exists so dev
# (and Redis outages) still works — the cost is logout-on-restart and no
# cross-instance sync.
#
# Storage shape (Redis): key = "bma:session:<token>", value = "" (empty),
# TTL = SESSION_MAX_AGE. Existence + TTL are all we need; the token itself
# is the credential.

_active_sessions: Dict[str, float] = {}  # token -> created_timestamp (fallback)
_SESSION_MAX_AGE = 86400  # 24 hours
_SESSION_KEY_PREFIX = "bma:session:"


def _session_redis():
    """Return Redis client if available, else None. Mirrors cache.py logic."""
    try:
        from cache import _get_redis
        return _get_redis()
    except Exception:
        return None


def _create_session() -> str:
    """Create a new random session token, stored in Redis if possible."""
    token = secrets.token_hex(32)
    r = _session_redis()
    if r is not None:
        try:
            # Empty value — token's existence is the credential
            r.setex(f"{_SESSION_KEY_PREFIX}{token}", _SESSION_MAX_AGE, "1")
            return token
        except Exception as e:
            logger.warning("Redis session create failed (%s) — falling back to memory", e)

    _active_sessions[token] = time.time()
    # GC expired entries when running in-memory mode
    cutoff = time.time() - _SESSION_MAX_AGE
    expired = [k for k, v in _active_sessions.items() if v < cutoff]
    for k in expired:
        _active_sessions.pop(k, None)
    return token


def _check_auth(request: Request) -> bool:
    """Return True if the request carries a valid session cookie."""
    token = request.cookies.get("admin_session")
    if not token:
        return False

    r = _session_redis()
    if r is not None:
        try:
            return bool(r.exists(f"{_SESSION_KEY_PREFIX}{token}"))
        except Exception:
            # Redis blip — fall through to in-memory check so live admins
            # don't get bounced just because Redis hiccupped.
            pass

    # In-memory path
    if token not in _active_sessions:
        return False
    created = _active_sessions[token]
    if time.time() - created > _SESSION_MAX_AGE:
        _active_sessions.pop(token, None)
        return False
    return True


def _revoke_session(token: str):
    """Revoke a session token from both Redis and in-memory store."""
    r = _session_redis()
    if r is not None:
        try:
            r.delete(f"{_SESSION_KEY_PREFIX}{token}")
        except Exception:
            pass
    _active_sessions.pop(token, None)


def _generate_csrf_token(request: Request) -> str:
    """Get or create a CSRF token for this session."""
    token = request.cookies.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
    return token

def _validate_csrf(request: Request, form_token: str) -> bool:
    """Validate CSRF token from form matches cookie."""
    cookie_token = request.cookies.get("csrf_token")
    if not cookie_token or not form_token:
        return False
    return hmac.compare_digest(cookie_token, form_token)


def _require_auth(request: Request):
    """Raise a redirect to login if not authenticated. Also check CSRF on POST."""
    if not _check_auth(request):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    # CSRF check for POST requests
    if request.method == "POST":
        origin = request.headers.get("origin") or ""
        referer = request.headers.get("referer") or ""
        host = request.headers.get("host") or ""
        if origin and host not in origin and host not in referer:
            raise HTTPException(status_code=403, detail="CSRF check failed")

# --------------------------------------------------------------------------- #
# File type detection and mapping
# --------------------------------------------------------------------------- #

# TODO: legacy, remove in next sprint — the single-file `/admin/upload` page
# (which is the only consumer of FILE_TYPE_MAP) targets the dropped legacy
# schema via `_run_import`. Keep the dict so the UI still renders, but use
# `bma_med.*` table names for display only — there is no active write path
# behind these labels anymore. The new flow lives at `/api/admin/upload-excel`.
FILE_TYPE_MAP = {
    "pt": {"table": "bma_med.{src}_patient", "csv": "pt.csv", "importer": "patients"},
    "pthistory": {"table": "bma_med.{src}_visit", "csv": "pthistory.csv", "importer": "visits"},
    "vitalsignslf": {
        "table": "bma_med.{src}_visit + measurements",
        "csv": "vitalsignslf.csv",
        "importer": "vital",
    },
    "homevisit": {
        "table": "bma_med.{src}_address + measurements",
        "csv": "homevisit.csv",
        "importer": "homevisit",
    },
    "homehealth": {
        "table": "bma_med.{src}_measurement",
        "csv": "homehealth.csv",
        "importer": "homehealth",
    },
    "labhealth": {
        "table": "bma_med.{src}_lab + measurements",
        "csv": "labhealth.csv",
        "importer": "lab",
    },
    "labhealthext": {
        "table": "bma_med.{src}_lab + measurements",
        "csv": "labhealthext.csv",
        "importer": "lab_ext",
    },
    "app2": {
        "table": "bma_med.app2_* (auto-split)",
        "csv": "app2.csv",
        "importer": "app2",
    },
}


# Columns that must NEVER appear in upload preview
_PREVIEW_PII_COLUMNS = {
    "IDCARD", "PID", "FNAME", "LNAME", "EFNAME", "ELNAME",
    "PHONE", "IDLINE", "EMAIL",
    "FIRSTSTF", "LASTSTF", "CANCELSTF",
    "HADDR", "HMOO", "HSOI", "HSTREET",
}


def _detect_file_type(columns: List[str]) -> Optional[str]:
    """Auto-detect CSV file type from column headers.

    Order matters: more specific signatures first.
    """
    cols = {c.upper() for c in columns}

    # App2 combined CSV — has _NAME/_SORT suffixed pre-computed columns
    app2_signals = sum(1 for c in cols if c.endswith("_NAME") or c.endswith("_SORT"))
    if app2_signals >= 5 and "PID" in cols:
        return "app2"

    # Patient master (Portal IDCARD or App1/Portal pt)
    if "IDCARD" in cols and "BIRTHDATE" in cols:
        return "pt"
    if {"PID", "MALE"} <= cols and "BRTHDATE" in cols:
        return "pt"  # App1 pt.csv

    # Portal pthistory (religion/lgbtq are unique markers)
    if "RLGN" in cols or "LGBTQ" in cols:
        return "pthistory"

    # Vital signs — strong markers
    if "HBPN" in cols or "RISKDM" in cols or "SCN9Q1" in cols:
        return "vitalsignslf"

    # Lab extended (Portal-only)
    if "PTGRIGHT" in cols or "PTGLEFT" in cols or "PAINHEAD" in cols:
        return "labhealthext"

    # Lab basic
    if "CBCRS" in cols or "HMGB" in cols or "HEMOGLOBIN" in cols or "FBS" in cols:
        return "labhealth"

    # Home visit (address + occupation)
    if "SELFOUR" in cols or "DISTYPE1" in cols or {"HDISTRICT", "DISTRICT"} & cols:
        return "homevisit"

    # Home health (lifestyle)
    if "EXCERCISE" in cols or "CGTDS" in cols or "FOOD" in cols:
        return "homehealth"

    return None


def _coverage_report(df_columns: list, source_code: str, file_type: str) -> dict:
    """Return mapping coverage: how many CSV columns match variable_definition.

    Used by /admin/upload preview to inform user before commit.

    TODO: legacy, remove in next sprint — historically queried the dropped
    legacy variable-definition table. The single-file `/admin/upload` path
    is superseded by `/api/admin/upload-excel`; this whole helper goes when
    that legacy page is retired. For now we return empty coverage so the
    preview still renders without raising.
    """
    if not source_code or source_code not in ("portal", "app1", "app2"):
        return {"matched": 0, "unmatched": 0, "address": 0, "total": len(df_columns)}

    upper_cols = {c.upper() for c in df_columns}
    # Variable-definition lookup is gone with the schema; treat every column as
    # "unmatched (significant)" minus the visit-meta whitelist below.
    known: Dict[str, str] = {}

    matched = upper_cols & set(known.keys())
    address_cols = {c for c in matched if known.get(c) == 'address'}
    unmatched = upper_cols - set(known.keys())

    # Skip visit-meta columns from "unmatched" warning
    visit_meta = {"PID", "IDCARD", "VSTDATE", "VSTTIME", "HPTCODE", "CANCELST",
                  "VST_ID", "HD"}
    unmatched_significant = unmatched - visit_meta

    return {
        "matched": len(matched),
        "unmatched": len(unmatched_significant),
        "address": len(address_cols),
        "total": len(df_columns),
        "unmatched_columns": sorted(unmatched_significant)[:20],  # show first 20
    }

# --------------------------------------------------------------------------- #
# In-memory upload cache (keyed by upload_id)
# --------------------------------------------------------------------------- #

_upload_cache: dict[str, dict] = {}
_CACHE_MAX_AGE_SECONDS = 3600  # auto-expire entries older than 1 hour

def _cleanup_cache():
    """Remove expired entries from the upload cache."""
    now = time.time()
    expired = [
        uid for uid, data in _upload_cache.items()
        if now - data.get("created_at", 0) > _CACHE_MAX_AGE_SECONDS
    ]
    for uid in expired:
        _upload_cache.pop(uid, None)

# --------------------------------------------------------------------------- #
# Login brute-force protection
# --------------------------------------------------------------------------- #

_login_attempts: Dict[str, List[float]] = {}
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300  # 5 minutes

def _check_login_rate(ip: str) -> bool:
    """Return False if too many login attempts from this IP."""
    import time as _time
    now = _time.time()
    cutoff = now - _LOGIN_WINDOW_SECONDS
    attempts = _login_attempts.get(ip, [])
    attempts = [t for t in attempts if t > cutoff]
    _login_attempts[ip] = attempts
    return len(attempts) < _MAX_LOGIN_ATTEMPTS

def _record_login_attempt(ip: str):
    import time as _time
    _login_attempts.setdefault(ip, []).append(_time.time())

# --------------------------------------------------------------------------- #
# Background import helpers
# --------------------------------------------------------------------------- #

def _update_history(
    history_id: int,
    status: str,
    rows_imported: int,
    rows_skipped: int,
    error_message: Optional[str],
    duration: float,
    view_refresh_status: Optional[str] = None,
    view_refresh_error: Optional[str] = None,
):
    """Update an import_history record. Uses a fresh connection (thread-safe).

    view_refresh_status: 'success' | 'failed' | 'skipped' | None
    view_refresh_error: error message when status is 'failed'
    """
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL_WRITER)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE import_history
                SET status = %s,
                    rows_imported = %s,
                    rows_skipped = %s,
                    error_message = %s,
                    duration_seconds = %s,
                    view_refresh_status = COALESCE(%s, view_refresh_status),
                    view_refresh_error  = COALESCE(%s, view_refresh_error),
                    completed_at = NOW()
                WHERE id = %s
                """,
                (
                    status, rows_imported, rows_skipped, error_message,
                    round(duration, 2),
                    view_refresh_status, view_refresh_error,
                    history_id,
                ),
            )
    except Exception:
        logger.exception("Failed to update import_history id=%s", history_id)
    finally:
        if conn:
            conn.close()


def _update_progress(history_id: int, step_label: str, pct: int,
                     rows_processed: Optional[int] = None,
                     rows_total: Optional[int] = None) -> None:
    """Update progress_step / progress_pct (+ live row counters) on import_history.

    Called from inside the import worker thread so the admin UI can poll
    /admin/api/import-progress/{id} and render a live progress bar with
    "4,250 / 200,000 rows" style feedback.

    Failures are swallowed — progress reporting must never break the import.
    """
    pct = max(0, min(100, int(pct)))
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL_WRITER)
        conn.autocommit = True
        with conn.cursor() as cur:
            # Only update non-None counters to avoid clobbering between files
            cols = ["progress_step = %s", "progress_pct = %s"]
            vals: List = [step_label[:120], pct]
            if rows_processed is not None:
                cols.append("rows_processed = %s")
                vals.append(int(rows_processed))
            if rows_total is not None:
                cols.append("rows_total = %s")
                vals.append(int(rows_total))
            vals.append(history_id)
            cur.execute(
                f"UPDATE import_history SET {', '.join(cols)} WHERE id = %s",
                tuple(vals),
            )
    except Exception:
        logger.debug("progress update failed for history_id=%s", history_id, exc_info=True)
    finally:
        if conn:
            conn.close()


def _make_progress_cb(history_id: int, step_label: str, global_pct_start: int,
                      global_pct_end: int, throttle_ms: int = 500):
    """Build a progress_cb(done, total) callback for ETL execute_values.

    Maps ETL's per-file progress onto the global [global_pct_start, global_pct_end]
    bar range and writes `progress_step`/`progress_pct`/`rows_processed`/`rows_total`
    to import_history.

    Throttles writes to ~1 per `throttle_ms` so 200K rows / 2000-per-batch = 100
    callbacks don't all hit Postgres back-to-back.
    """
    last_write = [0.0]

    def _cb(done: int, total: int):
        now = time.monotonic() * 1000
        # Always write the final chunk; throttle intermediate chunks
        is_final = (done >= total) if total else True
        if not is_final and (now - last_write[0]) < throttle_ms:
            return
        last_write[0] = now

        frac = (done / total) if total > 0 else 0.0
        frac = max(0.0, min(1.0, frac))
        pct = int(global_pct_start + (global_pct_end - global_pct_start) * frac)
        label = f"{step_label} — {done:,} / {total:,}"
        _update_progress(history_id, label, pct,
                         rows_processed=done, rows_total=total)

    return _cb


def _sanitize_error(exc: Exception) -> str:
    """Sanitize error message to avoid leaking internal details.

    Strips:
      - postgresql://… connection URLs
      - Source file paths (`/foo/bar.py`)
      - libpq-style key/value secrets in psycopg2 errors
        (`host=…`, `password=…`, `user=…`, `dbname=…`, etc.)
    """
    import re
    msg = str(exc)
    # Remove URI-style connection strings.
    msg = re.sub(r'postgresql://[^\s"\']+', 'postgresql://***', msg)
    # Remove libpq key=value secrets that psycopg2 sometimes embeds in errors.
    msg = re.sub(
        r"\b(host|hostaddr|password|user|dbname|port|sslmode|sslcert|sslkey)\s*=\s*\S+",
        r"\1=***",
        msg,
        flags=re.IGNORECASE,
    )
    # Remove file paths
    msg = re.sub(r'/[^\s"\']*\.py', '<file>', msg)
    # Truncate
    if len(msg) > 500:
        msg = msg[:500] + "..."
    return f"{type(exc).__name__}: {msg}"


def _run_import(upload_id: str, history_id: int):
    """Stub — the legacy single-file ETL import is retired.

    The body used to dispatch to `etl.import_csv_v3` and write into the now-
    dropped legacy schema. The replacement is `_run_pipeline_upload` behind
    `/api/admin/upload-excel`. If this background-thread target is ever
    reached, fail the history row loudly instead of pretending success.
    """
    _upload_cache.pop(upload_id, None)
    _update_history(
        history_id, "error", 0, 0,
        "Endpoint replaced by /api/admin/upload-excel", 0.0,
    )

# --------------------------------------------------------------------------- #
# Router and templates
# --------------------------------------------------------------------------- #

router = APIRouter(prefix="/admin", tags=["Admin"])

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)

# --------------------------------------------------------------------------- #
# Flash message helper (cookie-based, single request lifetime)
# --------------------------------------------------------------------------- #

def _set_flash(response, msg_type: str, text: str):
    """Set a flash message cookie to be consumed on next page load."""
    import json
    response.set_cookie(
        "flash_message",
        json.dumps({"type": msg_type, "text": text}),
        max_age=30,
        httponly=True,
        samesite="lax",
    )


def _get_flash(request: Request) -> Optional[List[Dict]]:
    """Read and consume flash message from cookie."""
    import json
    raw = request.cookies.get("flash_message")
    if not raw:
        return None
    try:
        return [json.loads(raw)]
    except (json.JSONDecodeError, TypeError):
        return None

# =========================================================================== #
# LOGIN / LOGOUT
# =========================================================================== #

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Render the login form."""
    if _check_auth(request):
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    csrf_token = _generate_csrf_token(request)
    response = templates.TemplateResponse(
        "admin/login.html", {"request": request, "error": None, "csrf_token": csrf_token}
    )
    response.set_cookie("csrf_token", csrf_token, httponly=True, samesite="strict", max_age=86400)
    return response


@router.post("/login")
async def login_submit(request: Request, password: str = Form(...), csrf_token: str = Form("")):
    """Validate password and set session cookie."""
    # Validate CSRF
    if not _validate_csrf(request, csrf_token):
        new_token = secrets.token_hex(32)
        response = templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": "Invalid request. Please try again.", "csrf_token": new_token},
            status_code=403,
        )
        response.set_cookie("csrf_token", new_token, httponly=True, samesite="strict", max_age=86400)
        return response

    client_ip = request.client.host if request.client else "unknown"
    if not _check_login_rate(client_ip):
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": "Too many login attempts. Please wait 5 minutes."},
            status_code=429,
        )
    _record_login_attempt(client_ip)

    if not hmac.compare_digest(password, ADMIN_PASSWORD):
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": "Invalid password"},
            status_code=401,
        )

    token = _create_session()
    response = RedirectResponse(url="/admin/dashboard", status_code=303)
    response.set_cookie(
        "admin_session",
        token,
        httponly=True,
        samesite="strict",
        secure=os.getenv("SECURE_COOKIES", "false").lower() == "true",
        max_age=_SESSION_MAX_AGE,
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    """Clear session cookie and redirect to login."""
    token = request.cookies.get("admin_session")
    if token:
        _revoke_session(token)
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie("admin_session")
    return response

# =========================================================================== #
# DASHBOARD
# =========================================================================== #

# Valid values for ?source= query param
_SOURCE_VALUES = ("all", "portal", "app1", "app2")

# Project screening target — denominator for Coverage %
# (สนพ. กทม. screening goal: 1M people)
COVERAGE_TARGET = int(os.getenv("COVERAGE_TARGET", "1000000"))


def _normalize_source(raw: Optional[str]) -> str:
    """Clamp ?source= query to one of all|portal|app1|app2. Default 'all'."""
    if not raw:
        return "all"
    raw = raw.strip().lower()
    return raw if raw in _SOURCE_VALUES else "all"


def _source_where_clause(source: str, alias: str = "") -> tuple[str, tuple]:
    """Build a WHERE fragment + params for a source filter.

    Returns ("" , ()) when source='all' (no filter needed).
    Returns ("WHERE data_source = %s", (source,)) for a specific source.
    `alias` can be "v" to produce "v.data_source = %s" for joined queries.
    """
    if source == "all":
        return "", ()
    col = f"{alias}.data_source" if alias else "data_source"
    return f"WHERE {col} = %s", (source,)


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, source: str = "all"):
    """Main admin dashboard with table counts and view info.

    Query params:
      source: all | portal | app1 | app2 (default: all) — filters raw table
              counts and materialized view row counts per data_source.

    Uses a 5-minute Redis cache for the heavy COUNT queries. The
    `/api/admin/upload-excel` flow calls cache_flush_all() at the end so this
    never serves stale data after a real upload.

    TODO: legacy, remove in next sprint — the COUNT queries below still
    target the dropped legacy schema. Each one is wrapped in try/except so
    the page renders zeros instead of 500s, but the dashboard now needs a
    rewrite against `bma_med.*` (or the public mv_* aggregates) to surface
    real numbers.
    """
    _require_auth(request)
    source = _normalize_source(source)

    # ── Cache: dashboard contents per source. ──
    # 5-min TTL is safe because cache_flush_all is called after every import.
    from cache import cache_get, cache_set
    _dashboard_cache_key = f"admin:dashboard:{source}"
    _cached_ctx = cache_get(_dashboard_cache_key)
    if _cached_ctx is not None:
        csrf_token = _generate_csrf_token(request)
        response = templates.TemplateResponse(
            "admin/dashboard.html",
            {**_cached_ctx,
             "request": request,
             "messages": _get_flash(request),
             "csrf_token": csrf_token},
        )
        response.set_cookie("csrf_token", csrf_token, httponly=True,
                            samesite="strict", max_age=86400)
        return response

    db_available = True
    raw_tables = []
    table_counts = {
        "patients": 0, "vitalsigns": 0, "visits": 0, "lab": 0,
        "homevisit": 0, "homehealth": 0, "lab_extended": 0,
    }
    people_counts = dict(table_counts)   # same shape, zeroed
    view_info = []
    source_breakdown: List[Dict] = []   # per-source row counts of raw_patients
    coverage_stats: Optional[Dict] = None

    try:
        where_clause, params = _source_where_clause(source)
        # TODO: legacy dashboard counts — these queries historically read from
        # the now-dropped legacy schema. Stub to zero so the page renders
        # without 500s; rewrite against `bma_med.*` or the public mv_*
        # aggregates to surface real numbers.

        # Each spec is (display_label, key) — counts default to zero.
        _legacy_dashboard_specs = [
            ("bma_med.*_patient",      "patients"),
            ("bma_med.*_visit",        "vitalsigns"),
            ("bma_med.*_visit",        "visits"),
            ("bma_med.*_lab",          "lab"),
            ("bma_med.*_address",      "homevisit"),
            ("bma_med.*_measurement",  "homehealth"),
            ("bma_med.*_lab_meas",     "lab_extended"),
        ]

        raw_tables = []
        for tbl, key in _legacy_dashboard_specs:
            table_counts[key] = 0
            raw_tables.append({
                "name": tbl,
                "count": 0,
                "n_records": 0,
                "n_people": 0,
            })
        people_counts = {key: 0 for _, key in _legacy_dashboard_specs}

        # ─── Per-source breakdown ────────────────────────────────────────
        # TODO: legacy — was a GROUP BY on the dropped patient_alias table.
        # Stubbed until the rewrite against bma_med.* lands.
        source_breakdown = []

        # ─── Coverage stats vs project target ────────────────────────────
        # TODO: legacy — was DISTINCT counts on the dropped patient_alias /
        # patient tables. Stubbed for now.
        try:
            n_per_source: Dict[str, int] = {}
            n_unique_all = 0
            coverage_stats = {
                "target":            COVERAGE_TARGET,
                "n_unique_all":      n_unique_all,
                "pct_unique_all":    round(100.0 * n_unique_all / COVERAGE_TARGET, 2)
                                       if COVERAGE_TARGET else 0.0,
                "n_per_source":      n_per_source,   # {portal: N, app1: N, app2: N}
                "pct_per_source":    {
                    s: round(100.0 * n_per_source.get(s, 0) / COVERAGE_TARGET, 2)
                       if COVERAGE_TARGET else 0.0
                    for s in ("portal", "app1", "app2")
                },
                "overlap_count":     sum(n_per_source.values()) - n_unique_all,
            }
        except Exception:
            coverage_stats = None

        # Materialized view info (with source filter if applicable).
        # NOT all views have data_source (e.g. summary_disease_control added in
        # migration 016) — probe column existence once so we can choose
        # per-view whether to apply the filter.
        mat_views = execute_query("""
            SELECT matviewname AS name
            FROM pg_matviews
            WHERE schemaname = 'public'
            ORDER BY matviewname
        """) or []

        views_with_source = set()
        if where_clause:
            src_rows = execute_query("""
                SELECT c.relname AS name
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = c.oid
                WHERE c.relkind = 'm'
                  AND n.nspname = 'public'
                  AND a.attnum > 0
                  AND a.attname = 'data_source'
            """) or []
            views_with_source = {r["name"] for r in src_rows}

        for mv in mat_views:
            view_name = mv["name"]
            try:
                if where_clause and view_name in views_with_source:
                    view_sql = f'SELECT COUNT(*) FROM "{view_name}" WHERE data_source = %s'
                    row_count = execute_scalar(view_sql, params) or 0
                else:
                    # No filter: either source='all' or view doesn't support it.
                    # For the latter, count shown here is "ALL sources combined"
                    # regardless of tab — that's the best we can do until the
                    # view is rebuilt with data_source.
                    row_count = execute_scalar(f'SELECT COUNT(*) FROM "{view_name}"') or 0
            except Exception as view_exc:
                logger.warning("View count failed for %s: %s", view_name, view_exc)
                row_count = 0
            view_info.append({
                "name": view_name,
                "row_count": row_count,
                "refreshed_at": "-",
                "has_source_col": (view_name in views_with_source),
            })
    except Exception as exc:
        db_available = False
        logger.exception("Dashboard query failed (source=%s): %s", source, exc)

    messages = _get_flash(request)
    if not db_available:
        messages = messages or []
        messages.append({"type": "error", "text": "Database is not connected. Start PostgreSQL to see data."})
        people_counts = {}

    csrf_token = _generate_csrf_token(request)
    # Cacheable subset of the template context. Excludes request/messages/csrf
    # which are per-request and must be regenerated on every hit.
    ctx_cacheable = {
        "table_counts": table_counts,
        "people_counts": people_counts if db_available else {},
        "raw_tables": raw_tables,
        "view_info": view_info,
        "source": source,
        "source_values": _SOURCE_VALUES,
        "source_breakdown": source_breakdown,
        "coverage_stats": coverage_stats,
    }
    if db_available:
        # 5-minute TTL — invalidated on next bundle import via cache_flush_all
        cache_set(_dashboard_cache_key, ctx_cacheable, 300)

    response = templates.TemplateResponse(
        "admin/dashboard.html",
        {**ctx_cacheable,
         "request": request,
         "messages": messages,
         "csrf_token": csrf_token},
    )
    # Clear flash cookie after reading
    response.delete_cookie("flash_message")
    response.set_cookie("csrf_token", csrf_token, httponly=True, samesite="strict", max_age=86400)
    return response

# =========================================================================== #
# UPLOAD CSV
# =========================================================================== #

@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    """Render the CSV upload form."""
    _require_auth(request)
    csrf_token = _generate_csrf_token(request)
    response = templates.TemplateResponse(
        "admin/upload.html",
        {
            "request": request,
            "file_types": FILE_TYPE_MAP,
            "preview": None,
            "messages": _get_flash(request),
            "csrf_token": csrf_token,
        },
    )
    response.set_cookie("csrf_token", csrf_token, httponly=True, samesite="strict", max_age=86400)
    return response


@router.post("/upload", response_class=HTMLResponse)
async def upload_csv(
    request: Request,
    file: UploadFile = File(...),
    file_type: str = Form("auto"),
    source_code: str = Form(""),
    csrf_token: str = Form(""),
):
    """Handle CSV file upload: parse, detect type, show preview.

    Legacy single-file path; the live import flow is `/api/admin/upload-excel`.
    `source_code` is still required for the preview UI.
    """
    _require_auth(request)

    # Validate CSRF token
    if not _validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF validation failed")

    # Source is required for v3 schema (must be portal/app1/app2 — or future)
    if source_code not in ("portal", "app1", "app2"):
        return templates.TemplateResponse(
            "admin/upload.html",
            {
                "request": request,
                "file_types": FILE_TYPE_MAP,
                "preview": None,
                "messages": [{"type": "error",
                              "text": "กรุณาเลือกแหล่งข้อมูล (Portal / App1 / App2)"}],
                "csrf_token": csrf_token,
            },
        )

    # Validate file extension
    if not file.filename or not file.filename.lower().endswith(".csv"):
        return templates.TemplateResponse(
            "admin/upload.html",
            {
                "request": request,
                "file_types": FILE_TYPE_MAP,
                "preview": None,
                "messages": [{"type": "error", "text": "Only .csv files are accepted."}],
                "csrf_token": csrf_token,
            },
        )

    # Read file content
    try:
        # Enforce max file size (500 MB)
        MAX_UPLOAD_SIZE = 500 * 1024 * 1024  # 500 MB
        raw_bytes = await file.read()
        if len(raw_bytes) > MAX_UPLOAD_SIZE:
            return templates.TemplateResponse(
                "admin/upload.html",
                {
                    "request": request,
                    "file_types": FILE_TYPE_MAP,
                    "preview": None,
                    "messages": [{"type": "error", "text": f"File too large. Maximum size is 500 MB."}],
                    "csrf_token": csrf_token,
                },
            )
        # Try UTF-8 first, fall back to TIS-620 (Thai encoding)
        for encoding in ("utf-8", "tis-620", "cp874", "latin-1"):
            try:
                content = raw_bytes.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            content = raw_bytes.decode("utf-8", errors="replace")

        df = pd.read_csv(StringIO(content), dtype=str, low_memory=False)
    except Exception as exc:
        return templates.TemplateResponse(
            "admin/upload.html",
            {
                "request": request,
                "file_types": FILE_TYPE_MAP,
                "preview": None,
                "messages": [{"type": "error", "text": f"Failed to parse CSV: {exc}"}],
                "csrf_token": csrf_token,
            },
        )

    # Auto-detect file type if needed
    detected_type = file_type
    if file_type == "auto":
        detected_type = _detect_file_type(df.columns.tolist())
        if detected_type is None:
            return templates.TemplateResponse(
                "admin/upload.html",
                {
                    "request": request,
                    "file_types": FILE_TYPE_MAP,
                    "preview": None,
                    "messages": [
                        {
                            "type": "error",
                            "text": (
                                "Could not auto-detect file type from columns. "
                                "Please select the file type manually."
                            ),
                        }
                    ],
                    "csrf_token": csrf_token,
                },
            )

    if detected_type not in FILE_TYPE_MAP:
        return templates.TemplateResponse(
            "admin/upload.html",
            {
                "request": request,
                "file_types": FILE_TYPE_MAP,
                "preview": None,
                "messages": [{"type": "error", "text": f"Unknown file type: {detected_type}"}],
                "csrf_token": csrf_token,
            },
        )

    # Clean up old cache entries
    _cleanup_cache()

    # Limit cache to 10 entries
    if len(_upload_cache) >= 10:
        _cleanup_cache()
        if len(_upload_cache) >= 10:
            oldest = min(_upload_cache, key=lambda k: _upload_cache[k].get("created_at", 0))
            _upload_cache.pop(oldest, None)

    # Store in cache
    upload_id = uuid.uuid4().hex
    _upload_cache[upload_id] = {
        "filename": file.filename,
        "file_type": detected_type,
        "source_code": source_code,           # v3: source for variable mapping
        "df": df,
        "created_at": time.time(),
    }

    file_info = FILE_TYPE_MAP[detected_type]

    # Strip PII columns from preview
    safe_columns = [c for c in df.columns if c.upper() not in _PREVIEW_PII_COLUMNS]
    safe_df = df[safe_columns]

    # v3: variable mapping coverage report
    coverage = _coverage_report(list(df.columns), source_code, detected_type)

    preview_data = {
        "upload_id": upload_id,
        "filename": file.filename,
        "file_type": detected_type,
        "source_code": source_code,
        "table_name": file_info["table"],
        "total_rows": len(df),
        "columns": safe_columns,
        "total_columns": len(df.columns),
        "sample_rows": safe_df.head(10).fillna("").to_dict(orient="records"),
        "coverage": coverage,
    }

    return templates.TemplateResponse(
        "admin/upload.html",
        {
            "request": request,
            "file_types": FILE_TYPE_MAP,
            "preview": preview_data,
            "messages": None,
            "csrf_token": csrf_token,
        },
    )

# =========================================================================== #
# IMPORT (run ETL)
# =========================================================================== #

@router.post("/import")
async def run_import(request: Request, upload_id: str = Form(...), csrf_token: str = Form("")):
    """Start background ETL import for a previously uploaded CSV."""
    _require_auth(request)

    # Validate CSRF token
    if not _validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF validation failed")

    data = _upload_cache.get(upload_id)
    if not data:
        response = RedirectResponse(url="/admin/upload", status_code=303)
        _set_flash(response, "error", "Upload expired. Please upload the file again.")
        return response

    file_type = data["file_type"]
    file_info = FILE_TYPE_MAP.get(file_type, {})
    table_name = file_info.get("table", file_type)

    # Create import_history record
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO import_history (filename, table_name, file_type, status, started_at)
                    VALUES (%s, %s, %s, 'running', NOW())
                    RETURNING id
                    """,
                    (data["filename"], table_name, file_type),
                )
                history_id = cur.fetchone()[0]
            conn.commit()
    except Exception as exc:
        logger.exception("Failed to create import_history record")
        response = RedirectResponse(url="/admin/upload", status_code=303)
        _set_flash(response, "error", f"Failed to start import: {_sanitize_error(exc)}")
        return response

    # Launch background import thread
    thread = threading.Thread(
        target=_run_import,
        args=(upload_id, history_id),
        daemon=True,
        name=f"import-{upload_id[:8]}",
    )
    thread.start()

    response = RedirectResponse(url="/admin/history", status_code=303)
    _set_flash(response, "success", f"Import started for {data['filename']} (job #{history_id}).")
    return response

# =========================================================================== #
# REFRESH MATERIALIZED VIEWS
# =========================================================================== #

@router.post("/refresh")
async def refresh_views(request: Request):
    """Manually refresh all materialized views."""
    _require_auth(request)

    # Validate CSRF token — read from form body
    form = await request.form()
    csrf_token_val = form.get("csrf_token", "")
    if not _validate_csrf(request, csrf_token_val):
        raise HTTPException(status_code=403, detail="CSRF validation failed")

    try:
        # v3: refresh public.mv_* via the SQL function. Returns one row per
        # view: (view_name, status). Status='ok' on success, otherwise the
        # error short-text. (The legacy etl.refresh_all_summaries() helper
        # only knew how to refresh the dropped summary_district_disease MV.)
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT view_name, status FROM public.refresh_all_mvs()")
            results = cur.fetchall()
            conn.commit()

        ok = [r[0] for r in results if r[1] == "ok"]
        failed = [(r[0], r[1]) for r in results if r[1] != "ok"]

        # Flush Redis cache after view refresh — the data downstream of the
        # MVs is what the API/frontend serve, so caches must drop too.
        try:
            from cache import cache_flush_all
            cache_flush_all()
        except Exception:
            pass

        response = RedirectResponse(url="/admin/dashboard", status_code=303)
        if failed:
            details = ", ".join(f"{n}: {s}" for n, s in failed)
            _set_flash(
                response, "warning",
                f"Refreshed {len(ok)} MVs OK, {len(failed)} failed — {details}",
            )
        else:
            names = ", ".join(ok) if ok else "(no MVs found)"
            _set_flash(
                response, "success",
                f"Materialized views refreshed: {len(ok)} OK ({names}). Cache flushed.",
            )
    except Exception as exc:
        logger.exception("Failed to refresh materialized views")
        response = RedirectResponse(url="/admin/dashboard", status_code=303)
        _set_flash(response, "error", f"Failed to refresh views: {_sanitize_error(exc)}")

    return response

# =========================================================================== #
# IMPORT HISTORY
# =========================================================================== #

@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    """Show recent import history."""
    _require_auth(request)

    rows = []
    stale_views_count = 0
    try:
        rows = execute_query("""
            SELECT id, filename, table_name, file_type,
                   rows_imported, rows_skipped, status,
                   error_message, started_at, completed_at,
                   duration_seconds, uploaded_by,
                   view_refresh_status, view_refresh_error,
                   progress_step, progress_pct,
                   rows_processed, rows_total
            FROM import_history
            ORDER BY started_at DESC
            LIMIT 50
        """)
        # Count recent imports where views are stale (latest import per
        # outcome — the dashboard banner mainly cares about the most recent).
        stale_views_count = sum(
            1 for r in rows if (r.get("view_refresh_status") == "failed")
        )
    except Exception:
        # Table may not exist yet if migration has not been run
        rows = []

    # How many completed records exist (for clear-button enablement)
    try:
        total_count = execute_scalar(
            "SELECT COUNT(*) FROM import_history WHERE status IN ('success','error')"
        ) or 0
        error_count = execute_scalar(
            "SELECT COUNT(*) FROM import_history WHERE status = 'error'"
        ) or 0
    except Exception:
        total_count = 0
        error_count = 0

    csrf_token = _generate_csrf_token(request)
    response = templates.TemplateResponse(
        "admin/history.html",
        {
            "request": request,
            "history": rows,
            "stale_views_count": stale_views_count,
            "messages": _get_flash(request),
            "csrf_token": csrf_token,
            "total_finished_count": int(total_count),
            "error_count": int(error_count),
        },
    )
    response.delete_cookie("flash_message")
    response.set_cookie("csrf_token", csrf_token, httponly=True,
                        samesite="strict", max_age=86400)
    return response


@router.post("/history/clear")
async def history_clear(request: Request,
                        mode: str = Form("completed"),
                        csrf_token: str = Form("")):
    """Delete import_history rows.

    `mode` values:
      * "completed" (default) — delete all status IN ('success','error')
      * "errors"              — delete only status = 'error'
      * "all"                 — delete EVERYTHING including running (dangerous;
                                use only for full reset — does not cancel the
                                worker thread, just hides it from UI)

    Running jobs are NEVER auto-deleted in "completed" or "errors" mode so we
    don't orphan the UI while a real import is in flight.
    """
    _require_auth(request)
    if not _validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF validation failed")

    mode = (mode or "").strip().lower()
    if mode == "all":
        where = "TRUE"
    elif mode == "errors":
        where = "status = 'error'"
    else:  # completed (default)
        where = "status IN ('success', 'error')"

    response = RedirectResponse(url="/admin/history", status_code=303)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM import_history WHERE {where}")
                n = cur.rowcount
            conn.commit()
        logger.info("Cleared %d import_history rows (mode=%s)", n, mode)
        label = {"all": "ทั้งหมด", "errors": "เฉพาะ error", "completed": "ที่จบแล้ว"}[mode]
        _set_flash(response, "success", f"ล้างประวัติ {label} สำเร็จ — ลบ {n:,} แถว")
    except Exception as exc:
        logger.exception("history_clear failed: %s", exc)
        _set_flash(response, "error", f"ล้างประวัติล้มเหลว: {_sanitize_error(exc)}")
    return response


@router.get("/api/import-progress")
async def api_import_progress(request: Request):
    """Return live progress for any imports still in 'running' status.

    The history page polls this endpoint to render a live progress bar.
    Response:
      {"running": [{
          "id", "filename", "file_type",
          "progress_step", "progress_pct",
          "rows_processed", "rows_total",
          "elapsed_sec", "rows_per_sec", "eta_sec"
      }]}
    """
    _require_auth(request)
    try:
        rows = execute_query("""
            SELECT id, filename, file_type, progress_step, progress_pct,
                   rows_processed, rows_total,
                   EXTRACT(EPOCH FROM (NOW() - started_at))::int AS elapsed_sec
            FROM import_history
            WHERE status = 'running'
            ORDER BY started_at DESC
            LIMIT 10
        """)
    except Exception:
        rows = []

    # Enrich with throughput + ETA (best-effort — ignore on bad input)
    for r in rows:
        rp = r.get("rows_processed") or 0
        rt = r.get("rows_total") or 0
        elapsed = r.get("elapsed_sec") or 0
        rps = (rp / elapsed) if (rp and elapsed > 0) else 0.0
        eta = int((rt - rp) / rps) if (rps > 0 and rt > rp) else None
        r["rows_per_sec"] = round(rps, 1) if rps else 0
        r["eta_sec"] = eta
    return JSONResponse({"running": rows})


# =========================================================================== #
# LOGS
# =========================================================================== #

@router.get("/data-quality", response_class=HTMLResponse)
async def data_quality_page(request: Request):
    """Data quality dashboard -- field completeness and cleansing report."""
    _require_auth(request)

    # Fetch data quality info
    data_quality = {}
    cleansing = {}
    # Migration 105 dropped the legacy raw_* MVs; the queries below still
    # target the legacy compat tables. If no raw_* table has data (v3-only
    # deployment), short-circuit and let the template render a banner.
    v3_pending = not _legacy_raw_has_data()
    try:
        # Query data quality directly (same logic as the API endpoint)
        tables = ["raw_patients", "raw_visits", "raw_vitalsigns", "raw_homevisit",
                  "raw_homehealth", "raw_lab_results", "raw_lab_extended"]
        if v3_pending:
            tables = []  # skip queries entirely on v3-only DB
        for table in tables:
            total = execute_scalar(f'SELECT COUNT(*) FROM "{table}"') or 0
            if total == 0:
                data_quality[table] = {"total_rows": 0, "fields": {}, "avg_fill_pct": 0}
                continue
            cols = execute_query("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                AND column_name NOT IN ('id','created_at','updated_at')
                ORDER BY ordinal_position
            """, (table,))
            fields = {}
            filled_total = 0
            for col in cols:
                cn = col["column_name"]
                null_count = execute_scalar(f'SELECT COUNT(*) FROM "{table}" WHERE "{cn}" IS NULL') or 0
                fill_pct = round(100.0 * (total - null_count) / total, 1)
                filled_total += fill_pct
                fields[cn] = {"null_count": int(null_count), "null_pct": round(100.0 * null_count / total, 1), "fill_pct": fill_pct}
            avg_fill = round(filled_total / len(fields), 1) if fields else 0
            data_quality[table] = {"total_rows": int(total), "fields": fields, "avg_fill_pct": avg_fill}

        # Blocked fields
        blocked = []
        for table, info in data_quality.items():
            for field, stats in info.get("fields", {}).items():
                if stats["null_pct"] >= 100 and info["total_rows"] > 0:
                    blocked.append({"table": table, "field": field})

        # Recent imports
        recent_imports = execute_query("""
            SELECT filename, file_type, status, started_at, rows_imported, rows_skipped, duration_seconds
            FROM import_history ORDER BY started_at DESC LIMIT 5
        """)
        cleansing = {"blocked_fields": blocked, "recent_imports": recent_imports}
    except Exception:
        pass

    return templates.TemplateResponse(
        "admin/data_quality.html",
        {
            "request": request,
            "data_quality": data_quality,
            "cleansing": cleansing,
            "v3_pending": v3_pending,
            "messages": _get_flash(request),
        },
    )

# =========================================================================== #
# CROSS-STATISTICS (portal × app1 × app2 comparison)
# =========================================================================== #

_CROSS_SOURCES = ("portal", "app1", "app2")


def _chi_square_2xN(row_positive: List[int], row_negative: List[int]) -> Dict:
    """Chi-square test on a 2×N contingency table (has vs. doesn't have × N sources).

    Returns dict with chi2, p_value, dof, significant (p<0.05). Gracefully
    handles degenerate cases (all zeros, only 1 source with data).
    """
    try:
        import numpy as _np
        from scipy import stats as _stats
        table = _np.array([row_positive, row_negative])
        # Drop columns where the total is 0 (source with no data)
        col_sums = table.sum(axis=0)
        keep = col_sums > 0
        if keep.sum() < 2:
            return {"applicable": False, "reason": "ต้องมีอย่างน้อย 2 source ที่มีข้อมูล"}
        table = table[:, keep]
        if table.sum() == 0:
            return {"applicable": False, "reason": "ตารางว่าง"}
        chi2, p, dof, expected = _stats.chi2_contingency(table)
        return {
            "applicable": True,
            "chi2": round(float(chi2), 3),
            "p_value": round(float(p), 6),
            "dof": int(dof),
            "significant": bool(p < 0.05),
            "n_sources_with_data": int(keep.sum()),
        }
    except Exception as exc:
        logger.warning("chi_square_2xN failed: %s", exc)
        return {"applicable": False, "reason": f"คำนวณไม่ได้: {exc}"}


def _coverage_matrix() -> List[Dict]:
    """Return 50 × 3 matrix: per-district patient counts per source + zone info."""
    rows = execute_query("""
        SELECT
            d.dcode AS district_code,
            d.name_th AS district_name,
            d.zone_code,
            COALESCE(SUM(s.total_screened) FILTER (WHERE s.data_source = 'portal'), 0)::int AS n_portal,
            COALESCE(SUM(s.total_screened) FILTER (WHERE s.data_source = 'app1'),   0)::int AS n_app1,
            COALESCE(SUM(s.total_screened) FILTER (WHERE s.data_source = 'app2'),   0)::int AS n_app2,
            COALESCE(SUM(s.total_screened), 0)::int AS n_total
        FROM ref_districts d
        LEFT JOIN summary_district_disease s ON s.district_code = d.dcode
        GROUP BY d.dcode, d.name_th, d.zone_code
        ORDER BY d.dcode
    """) or []
    return rows


def _distribution_comparison() -> Dict:
    """Per-source disease prevalence counts + chi-square tests per metric.

    Queries summary_district_disease rolled up (summed across districts).
    Computes per-metric chi-square on 2×3 tables (positive/negative × 3 sources).
    """
    src_rows = execute_query("""
        SELECT data_source,
               COALESCE(SUM(total_screened),         0)::int AS total,
               COALESCE(SUM(risk_dm_count),          0)::int AS risk_dm,
               COALESCE(SUM(risk_hpt_count),         0)::int AS risk_hpt,
               COALESCE(SUM(risk_cvd_count),         0)::int AS risk_cvd,
               COALESCE(SUM(risk_bmi_count),         0)::int AS risk_bmi,
               COALESCE(SUM(found_dm_count),         0)::int AS found_dm,
               COALESCE(SUM(found_hpt_count),        0)::int AS found_hpt,
               COALESCE(SUM(found_cvd_count),        0)::int AS found_cvd,
               COALESCE(SUM(found_obesity_count),    0)::int AS found_obesity,
               COALESCE(SUM(found_dyslipidemia_count), 0)::int AS found_dyslipidemia
        FROM summary_district_disease
        WHERE data_source IN ('portal', 'app1', 'app2')
        GROUP BY data_source
        ORDER BY data_source
    """) or []

    # Reshape to dict for easier template access
    by_source = {r["data_source"]: r for r in src_rows}
    # Ensure all 3 sources appear (with 0s if absent)
    for src in _CROSS_SOURCES:
        by_source.setdefault(src, {
            "data_source": src, "total": 0,
            "risk_dm": 0, "risk_hpt": 0, "risk_cvd": 0, "risk_bmi": 0,
            "found_dm": 0, "found_hpt": 0, "found_cvd": 0,
            "found_obesity": 0, "found_dyslipidemia": 0,
        })

    # Compute per-metric chi-square
    metrics = [
        ("risk_dm",            "เสี่ยงเบาหวาน"),
        ("risk_hpt",           "เสี่ยงความดัน"),
        ("risk_cvd",           "เสี่ยงหัวใจ"),
        ("risk_bmi",           "เสี่ยง BMI"),
        ("found_dm",           "พบเบาหวาน"),
        ("found_hpt",          "พบความดัน"),
        ("found_cvd",          "พบหัวใจ"),
        ("found_obesity",      "พบอ้วน"),
        ("found_dyslipidemia", "พบไขมันผิดปกติ"),
    ]
    comparisons: List[Dict] = []
    for key, label in metrics:
        positives = [by_source[s][key]                for s in _CROSS_SOURCES]
        negatives = [max(by_source[s]["total"] - by_source[s][key], 0) for s in _CROSS_SOURCES]
        pcts = [
            (100.0 * by_source[s][key] / by_source[s]["total"]) if by_source[s]["total"] > 0 else 0.0
            for s in _CROSS_SOURCES
        ]
        comparisons.append({
            "metric": key,
            "label": label,
            "per_source": {
                src: {
                    "count": positives[i],
                    "total": by_source[src]["total"],
                    "pct": round(pcts[i], 2),
                }
                for i, src in enumerate(_CROSS_SOURCES)
            },
            "chi_square": _chi_square_2xN(positives, negatives),
        })

    return {
        "by_source": by_source,
        "comparisons": comparisons,
        "sources": list(_CROSS_SOURCES),
    }


# =========================================================================== #
# INCLUSION / EXCLUSION CRITERIA REGISTRY
# =========================================================================== #
# For every numerical/categorical field where ETL applies a clinical-range
# validation, register the rule + rationale. The /admin/cleansing-report
# page renders this as a table together with live missing % per source.
#
# Fields not in this registry are still tracked by the data-quality endpoint
# (information_schema introspection) but their "rule" cell shows blank.
#
# Format per entry:
#   table       : raw_<X> table name
#   column      : DB column name
#   raw_field   : original CSV column (Portal/App1)
#   rule        : human-readable inclusion criterion (out-of-range → NULL)
#   unit        : measurement unit
#   rationale   : clinical/medical reason for the bound
#   category    : "vital" / "lab" / "demo" / "ratio" / "code" / "computed"

INCLUSION_CRITERIA: List[Dict[str, str]] = [
    # ── raw_patients ───────────────────────────────────────────────────────
    {"table": "raw_patients",     "column": "birth_year",       "raw_field": "BIRTHDATE",
     "rule": "1900 ≤ year ≤ 2030",                  "unit": "year",   "rationale": "อายุสูงสุดในประวัติศาสตร์ = 122 ปี — ก่อน 1900 = ผิด",
     "category": "demo"},
    {"table": "raw_patients",     "column": "age",              "raw_field": "AGE",
     "rule": "0 ≤ age ≤ 150",                       "unit": "year",   "rationale": "เป็นไปไม่ได้นอกช่วงนี้",
     "category": "demo"},
    {"table": "raw_patients",     "column": "sex",              "raw_field": "MALE",
     "rule": "{10, 20}",                            "unit": "code",   "rationale": "10=ชาย, 20=หญิง (App2 'ชาย/หญิง' → 10/20)",
     "category": "code"},

    # ── raw_vitalsigns ─────────────────────────────────────────────────────
    {"table": "raw_vitalsigns",   "column": "sbp",              "raw_field": "HBPN",
     "rule": "40 ≤ x ≤ 300",                        "unit": "mmHg",   "rationale": "SBP < 40 = cardiac arrest; พบจริงมีค่า 0, 1 (ไม่ได้วัดแต่กรอก)",
     "category": "vital"},
    {"table": "raw_vitalsigns",   "column": "dbp",              "raw_field": "LBPN",
     "rule": "20 ≤ x ≤ 200",                        "unit": "mmHg",   "rationale": "DBP < 20 = severe shock",
     "category": "vital"},
    {"table": "raw_vitalsigns",   "column": "pulse_rate",       "raw_field": "PR",
     "rule": "0 ≤ x ≤ 999",                         "unit": "bpm",    "rationale": "INT4 bound; resting + tachy + bradycardia ครอบคลุม",
     "category": "vital"},
    {"table": "raw_vitalsigns",   "column": "height_cm",        "raw_field": "HEIGHT",
     "rule": "50 ≤ x ≤ 250",                        "unit": "cm",     "rationale": "ทารกแรกเกิด ~50; คนสูงสุดในโลก ~272",
     "category": "vital"},
    {"table": "raw_vitalsigns",   "column": "weight_kg",        "raw_field": "WEIGHT",
     "rule": "10 ≤ x ≤ 300",                        "unit": "kg",     "rationale": "ทารก ~3 → 1ขวบ ~10; คนหนักสุดในไทย ~300",
     "category": "vital"},
    {"table": "raw_vitalsigns",   "column": "waist_cm",         "raw_field": "WSTL",
     "rule": "30 ≤ x ≤ 200",                        "unit": "cm",     "rationale": "พบจริงมีค่า 0, 29 (ไม่ได้วัด)",
     "category": "vital"},
    {"table": "raw_vitalsigns",   "column": "fasting_glucose",  "raw_field": "PREFPG",
     "rule": "0 ≤ x ≤ 999",                         "unit": "mg/dL",  "rationale": "เครื่องวัดน้ำตาลปลายนิ้ว upper bound",
     "category": "vital"},
    {"table": "raw_vitalsigns",   "column": "post_glucose",     "raw_field": "POSTFPG",
     "rule": "0 ≤ x ≤ 999",                         "unit": "mg/dL",  "rationale": "หลังอาหาร 2 hr",
     "category": "vital"},
    {"table": "raw_vitalsigns",   "column": "bmi",              "raw_field": "(computed: weight/height²)",
     "rule": "10 ≤ x ≤ 80",                         "unit": "kg/m²",  "rationale": "BMI สูงสุดที่บันทึก ~70 (Manuel Uribe)",
     "category": "computed"},
    {"table": "raw_vitalsigns",   "column": "bmi_src",          "raw_field": "BMI (App2 only)",
     "rule": "5 ≤ x ≤ 80",                          "unit": "kg/m²",  "rationale": "App2 pre-computed BMI",
     "category": "computed"},
    {"table": "raw_vitalsigns",   "column": "map_bp",           "raw_field": "(computed: DBP+(SBP-DBP)/3)",
     "rule": "computed only when SBP+DBP both present", "unit": "mmHg", "rationale": "Mean Arterial Pressure",
     "category": "computed"},
    {"table": "raw_vitalsigns",   "column": "phq9_total",       "raw_field": "Σ(SCN9Q1..SCN9Q9)",
     "rule": "0 ≤ x ≤ 27 (NULL if any item missing)", "unit": "score", "rationale": "PHQ-9 9 ข้อ ข้อละ 0-3",
     "category": "computed"},
    {"table": "raw_vitalsigns",   "column": "st5_total",        "raw_field": "Σ(ST501..ST505)",
     "rule": "0 ≤ x ≤ 15 (NULL if any item missing)", "unit": "score", "rationale": "ST-5 5 ข้อ ข้อละ 0-3",
     "category": "computed"},

    # ── raw_lab_results ────────────────────────────────────────────────────
    {"table": "raw_lab_results",  "column": "wbc",              "raw_field": "WBC",
     "rule": "0 ≤ x ≤ 999,999",                     "unit": "/μL",    "rationale": "Clinical leukocytosis upper bound",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "rbc",              "raw_field": "RBC",
     "rule": "0 ≤ x ≤ 999,999",                     "unit": "·10⁶/μL","rationale": "RBC count",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "hemoglobin",       "raw_field": "HMGB",
     "rule": "0 ≤ x ≤ 30",                          "unit": "g/dL",   "rationale": "Hb สูงสุดจริง ~20 (polycythemia vera)",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "hematocrit",       "raw_field": "HMTC",
     "rule": "0 ≤ x ≤ 80",                          "unit": "%",      "rationale": "ปกติ 36-54%; max 80 = severe polycythemia",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "mcv",              "raw_field": "MCV",
     "rule": "0 ≤ x ≤ 200",                         "unit": "fL",     "rationale": "ปกติ 80-100; >200 = ผิดปกติ",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "platelet",         "raw_field": "PITCNT",
     "rule": "0 ≤ x ≤ 9,999,999",                   "unit": "/μL",    "rationale": "INT4 bound",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "fbs",              "raw_field": "FBS",
     "rule": "0 ≤ x ≤ 999",                         "unit": "mg/dL",  "rationale": "เครื่องวัดน้ำตาลปลายนิ้ว upper",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "blood_sugar",      "raw_field": "BLDSUGAR",
     "rule": "0 ≤ x ≤ 999",                         "unit": "mg/dL",  "rationale": "Random/postprandial",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "dtx",              "raw_field": "DTX",
     "rule": "0 ≤ x ≤ 999",                         "unit": "mg/dL",  "rationale": "Capillary glucose meter",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "cholesterol",      "raw_field": "CHOLEST",
     "rule": "0 ≤ x ≤ 999",                         "unit": "mg/dL",  "rationale": "Total cholesterol",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "triglyceride",     "raw_field": "TRIGLY",
     "rule": "0 ≤ x ≤ 999",                         "unit": "mg/dL",  "rationale": "Fasting triglyceride",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "hdl",              "raw_field": "HDL",
     "rule": "0 ≤ x ≤ 500",                         "unit": "mg/dL",  "rationale": "HDL — high upper bound",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "ldl",              "raw_field": "LDL",
     "rule": "0 ≤ x ≤ 500",                         "unit": "mg/dL",  "rationale": "LDL — high upper bound",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "sgot",             "raw_field": "SGOT (AST)",
     "rule": "0 ≤ x ≤ 999",                         "unit": "U/L",    "rationale": "ปกติ 10-40; >999 = severe hepatitis",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "sgpt",             "raw_field": "SGPT (ALT)",
     "rule": "0 ≤ x ≤ 999",                         "unit": "U/L",    "rationale": "ปกติ 7-56",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "alk_phosphatase",  "raw_field": "ALKPPT",
     "rule": "0 ≤ x ≤ 999",                         "unit": "U/L",    "rationale": "ปกติ 44-147",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "uric_acid",        "raw_field": "URICACID",
     "rule": "0 ≤ x ≤ 50",                          "unit": "mg/dL",  "rationale": "M 3.4-7, F 2.4-6; >50 = unrealistic",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "creatinine",       "raw_field": "CRTININE",
     "rule": "0 ≤ x ≤ 50",                          "unit": "mg/dL",  "rationale": "ปกติ 0.7-1.3; ESRD ~10; >50 = ผิดพลาด",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "egfr",             "raw_field": "EGFRRS / EGFR_LAB",
     "rule": "0 ≤ x ≤ 200",                         "unit": "mL/min", "rationale": "Physiologic max ~120; >200 = formula error",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "bun",              "raw_field": "BUNRS",
     "rule": "0 ≤ x ≤ 200",                         "unit": "mg/dL",  "rationale": "ปกติ 7-20",
     "category": "lab"},
    {"table": "raw_lab_results",  "column": "egfr_stage",       "raw_field": "(computed)",
     "rule": "G1/G2/G3a/G3b/G4/G5",                 "unit": "stage",  "rationale": "KDIGO 2012 staging from eGFR",
     "category": "computed"},
    {"table": "raw_lab_results",  "column": "anemia_class",     "raw_field": "(computed)",
     "rule": "microcytic/normocytic/macrocytic",    "unit": "class",  "rationale": "Hb < 12/13 (sex-spec) + MCV cut-offs",
     "category": "computed"},

    # ── raw_homevisit ──────────────────────────────────────────────────────
    {"table": "raw_homevisit",    "column": "home_district",    "raw_field": "DISTRICT",
     "rule": "1001 ≤ x ≤ 1050 (BMA only)",          "unit": "code",   "rationale": "BMA district code; App2 9999 → NULL",
     "category": "code"},
    {"table": "raw_homevisit",    "column": "work_district",    "raw_field": "WRKDISTRICT",
     "rule": "1001 ≤ x ≤ 1050 (App2 9999 → NULL)",  "unit": "code",   "rationale": "Same range as home_district",
     "category": "code"},

    # ── ID-level rule (filter, not NULL) ───────────────────────────────────
    {"table": "raw_patients",     "column": "idcard_hash",      "raw_field": "IDCARD / PID",
     "rule": "Base64 decode + HMAC-SHA-256",        "unit": "hex64",  "rationale": "ถ้า decode ไม่ได้ → row ถูก SKIP (ไม่ insert)",
     "category": "code"},
]


# Tables to include in data-quality, in display order
_QUALITY_TABLES = (
    "raw_patients",
    "raw_visits",
    "raw_vitalsigns",
    "raw_homevisit",
    "raw_homehealth",
    "raw_lab_results",
    "raw_lab_extended",
)

# Columns to exclude from completeness measurement (bookkeeping, not data)
_QUALITY_SKIP_COLS = frozenset({
    "id", "patient_id", "data_source", "import_batch_id",
    "created_at", "updated_at",
    # PII — stripped at the database.py layer anyway, but be explicit
    "idcard_hash", "staff_code",
})


def _table_columns(table: str) -> List[str]:
    """Return user-data columns for `table` (excluding bookkeeping + PII),
    in ordinal_position order."""
    rows = execute_query("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
    """, (table,)) or []
    return [r["column_name"] for r in rows if r["column_name"] not in _QUALITY_SKIP_COLS]


def _data_quality_per_source() -> Dict:
    """Field completeness % per source, for ALL 7 raw tables.

    Dynamic introspection — queries information_schema for the column list,
    then runs one aggregated query per table:

        SELECT data_source, COUNT(*), COUNT(col1), COUNT(col2), ...
        FROM <table> GROUP BY data_source

    → 1 full-table scan per table (not per column), so the cost scales with
    n_tables, not n_cols. For 7 tables × 1.8M rows total ≈ 3-6 seconds.
    """
    from psycopg2 import sql as psql   # safe identifier quoting

    out: Dict[str, List[Dict]] = {}
    for tbl in _QUALITY_TABLES:
        cols = _table_columns(tbl)
        if not cols:
            out[tbl] = []
            continue

        # Build: SELECT data_source, COUNT(*), COUNT("col1"), COUNT("col2"), ...
        count_exprs = psql.SQL(", ").join(
            psql.SQL("COUNT({0}) AS {1}").format(
                psql.Identifier(c),
                psql.Identifier(f"n_{c}"),
            )
            for c in cols
        )
        query = psql.SQL(
            "SELECT data_source, COUNT(*) AS n_records, {counts} "
            "FROM {tbl} GROUP BY data_source ORDER BY data_source"
        ).format(counts=count_exprs, tbl=psql.Identifier(tbl))

        # Execute raw SQL (psycopg2 sql.Composed object)
        from database import get_conn
        import psycopg2.extras
        present: Dict[str, Dict] = {}
        try:
            with get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(query)
                    raw = cur.fetchall()
            for r in raw:
                total = int(r.get("n_records") or 0)
                fields = {}
                for c in cols:
                    v = int(r.get(f"n_{c}") or 0)
                    pct = round(100.0 * v / total, 1) if total else 0.0
                    fields[c] = {"count": v, "pct": pct}
                present[r["data_source"]] = {
                    "data_source": r["data_source"],
                    "n_records": total,
                    "fields": fields,
                }
        except Exception as exc:
            logger.warning("data-quality query failed for %s: %s", tbl, exc)

        # Always emit 3 source slots — fill blanks where a source has no rows
        # in this table (so the template can show an explicit — cell).
        blank_fields = {c: {"count": 0, "pct": 0.0} for c in cols}
        table_rows = []
        for src in ("portal", "app1", "app2"):
            if src in present:
                table_rows.append(present[src])
            else:
                table_rows.append({
                    "data_source": src,
                    "n_records": 0,
                    "fields": dict(blank_fields),  # shallow copy is fine — never mutated
                })
        out[tbl] = table_rows
    return out


# =========================================================================== #
# AGREEMENT (Phase 3 — same-person discrepancy analysis)
# =========================================================================== #

@router.get("/agreement", response_class=HTMLResponse)
async def agreement_page(request: Request, pair: str = "portal-app1"):
    """Bland-Altman + Cohen's kappa between two sources.

    Query param:
      pair: portal-app1 | portal-app2 | app1-app2 (default: portal-app1)
    """
    _require_auth(request)
    try:
        from services.agreement_service import (
            build_agreement_report, normalize_pair, ALL_PAIRS,
        )
    except Exception as exc:
        logger.exception("agreement_service import failed")
        return templates.TemplateResponse(
            "admin/agreement.html",
            {
                "request": request,
                "pair": ("portal", "app1"),
                "all_pairs": [("portal", "app1"), ("portal", "app2"), ("app1", "app2")],
                "report": None,
                "load_error": _sanitize_error(exc),
                "v3_pending": False,
                "messages": _get_flash(request),
            },
        )

    source_a, source_b = normalize_pair(pair)

    # Fast path — skip heavy compute if either source has 0 rows.
    # Note: agreement_service still reads from `raw_patients` etc.; until it
    # is rewritten against `bma_med.*` the page can only render against
    # legacy data. Treat absence of raw_* rows as v3_pending so the user gets
    # a clear banner instead of a misleading "ไม่สามารถวิเคราะห์ได้" screen.
    v3_pending = not _legacy_raw_has_data()
    sources_present: set = set()
    report = None
    if not v3_pending:
        try:
            # bma_med.* doesn't have a data_source column on patient — derive
            # presence from which per-source pt table holds each patient_id.
            present_q = execute_query("""
                SELECT 'app1' AS data_source, COUNT(*) AS n FROM bma_med.app1_pt
                UNION ALL
                SELECT 'portal' AS data_source, COUNT(*) AS n FROM bma_med.portal_pt
                UNION ALL
                SELECT 'app2' AS data_source, COUNT(*) AS n FROM bma_med.app2_app2
            """) or []
            sources_present = {r["data_source"] for r in present_q if r["n"] > 0}
        except Exception:
            sources_present = set()

        if source_a in sources_present and source_b in sources_present:
            try:
                report = build_agreement_report(source_a, source_b, with_plots=True)
            except Exception as exc:
                logger.exception("build_agreement_report failed")
                report = {"error": _sanitize_error(exc),
                          "source_a": source_a, "source_b": source_b,
                          "n_common_patients": 0,
                          "continuous": [], "categorical": []}

    return templates.TemplateResponse(
        "admin/agreement.html",
        {
            "request": request,
            "pair": (source_a, source_b),
            "all_pairs": ALL_PAIRS,
            "sources_present": sources_present,
            "report": report,
            "load_error": None,
            "v3_pending": v3_pending,
            "messages": _get_flash(request),
        },
    )


# =========================================================================== #
# CLEANSING REPORT — inclusion/exclusion criteria + missing % per source
# =========================================================================== #

@router.get("/cleansing-report", response_class=HTMLResponse)
async def cleansing_report_page(request: Request):
    """Per-field inclusion/exclusion criteria + missing counts per source.

    Each row of INCLUSION_CRITERIA gets:
      - rule  : ETL inclusion criterion (out-of-range → NULL)
      - n     : total rows in that source
      - non_null  : rows with a value
      - missing   : rows with NULL (already includes both originally-missing
                    AND originally-out-of-range, since ETL collapses them)
      - pct_missing : missing / n × 100
    """
    _require_auth(request)
    from psycopg2 import sql as psql

    # Group entries by table for fewer queries (one COUNT per table×source)
    by_table: Dict[str, List[Dict]] = {}
    for entry in INCLUSION_CRITERIA:
        by_table.setdefault(entry["table"], []).append(entry)

    sources = ("portal", "app1", "app2")
    rows_per_source: Dict[str, Dict[str, int]] = {}
    # INCLUSION_CRITERIA still references legacy raw_* tables. On a v3-only
    # deployment there's no data to count → skip the queries and let the
    # template render the v3-pending banner.
    v3_pending = not _legacy_raw_has_data()
    # Source totals — for the denominator
    if not v3_pending:
        try:
            for tbl in by_table.keys():
                rows_per_source[tbl] = {}
                for src in sources:
                    n = execute_scalar(
                        f'SELECT COUNT(*) FROM "{tbl}" WHERE data_source = %s',
                        (src,),
                    ) or 0
                    rows_per_source[tbl][src] = int(n)
        except Exception as exc:
            logger.warning("cleansing-report totals failed: %s", exc)

    # Per-field non-null counts in 1 query per (table, source) using
    # COUNT(col1), COUNT(col2), ...
    # On v3-only DB this loop is skipped via `not by_table` guard below;
    # every (table, column) falls through to an `applicable: False` placeholder.
    field_stats: Dict[str, Dict] = {}   # key = (table, column) → {portal: {non_null,pct}, app1: ..., app2: ...}
    try:
        from database import get_conn
        import psycopg2.extras
        for tbl, entries in (by_table.items() if not v3_pending else []):
            cols = [e["column"] for e in entries]
            # filter out columns that don't exist in DB (e.g. typos)
            db_cols = set(_table_columns(tbl))
            cols_present = [c for c in cols if c in db_cols]
            if not cols_present:
                continue

            count_exprs = psql.SQL(", ").join(
                psql.SQL("COUNT({0}) AS {1}").format(
                    psql.Identifier(c), psql.Identifier(f"n_{c}")
                ) for c in cols_present
            )
            query = psql.SQL(
                "SELECT data_source, COUNT(*) AS n_records, {counts} "
                "FROM {tbl} GROUP BY data_source"
            ).format(counts=count_exprs, tbl=psql.Identifier(tbl))

            with get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(query)
                    raw = cur.fetchall()
            by_src = {r["data_source"]: r for r in raw}
            for col in cols_present:
                key = (tbl, col)
                field_stats[key] = {}
                for src in sources:
                    r = by_src.get(src)
                    if r is None:
                        field_stats[key][src] = {
                            "n": 0, "non_null": 0, "missing": 0,
                            "pct_missing": None, "applicable": False,
                        }
                        continue
                    n = int(r.get("n_records") or 0)
                    nn = int(r.get(f"n_{col}") or 0)
                    missing = n - nn
                    pct = round(100.0 * missing / n, 1) if n else None
                    field_stats[key][src] = {
                        "n": n, "non_null": nn, "missing": missing,
                        "pct_missing": pct, "applicable": (n > 0),
                    }
    except Exception as exc:
        logger.exception("cleansing-report stats failed: %s", exc)

    # Build display structure: list of {entry_meta, per_source: {portal: stats, ...}}
    enriched = []
    for entry in INCLUSION_CRITERIA:
        key = (entry["table"], entry["column"])
        stats = field_stats.get(key)
        if stats is None:   # column not present in DB
            stats = {s: {"applicable": False} for s in sources}
        enriched.append({**entry, "per_source": stats})

    # Group by category for the UI
    by_category: Dict[str, List[Dict]] = {}
    for e in enriched:
        by_category.setdefault(e["category"], []).append(e)

    # Category display order + labels
    category_order = [
        ("vital",    "🩺 Vital signs",     "สัญญาณชีพ + สรีรวิทยาพื้นฐาน"),
        ("lab",      "🧪 Lab values",       "ผลแลปเชิงตัวเลข"),
        ("computed", "📐 Computed",         "ค่าที่ ETL คำนวณจาก raw"),
        ("demo",     "👤 Demographics",     "อายุ / วันเกิด / เพศ"),
        ("code",     "🔢 Codes",            "รหัสเขต/ID — รูปแบบ + range"),
        ("ratio",    "📊 Ratios",           "ค่าอัตราส่วน"),
    ]

    csrf_token = _generate_csrf_token(request)
    response = templates.TemplateResponse(
        "admin/cleansing_report.html",
        {
            "request": request,
            "category_order": category_order,
            "by_category": by_category,
            "sources": list(sources),
            "rows_per_source": rows_per_source,
            "csrf_token": csrf_token,
            "v3_pending": v3_pending,
            "messages": _get_flash(request),
        },
    )
    response.set_cookie("csrf_token", csrf_token, httponly=True,
                        samesite="strict", max_age=86400)
    return response


@router.get("/cross-stats", response_class=HTMLResponse)
async def cross_stats_page(request: Request, tab: str = "coverage"):
    """Cross-source statistics dashboard — coverage, distribution, data quality.

    Query param:
      tab: coverage | distribution | quality (default: coverage)
    """
    _require_auth(request)
    if tab not in ("coverage", "distribution", "quality"):
        tab = "coverage"

    # Coverage + distribution still work — they query summary_district_disease
    # which is a v3 compat view (migration 105 preserved it). Only the quality
    # tab depends on legacy raw_* tables. The "sources_present" probe also
    # hits raw_patients; if that's empty, derive presence from the v3 view
    # instead so the coverage tab still labels its sources correctly.
    legacy_has_data = _legacy_raw_has_data()
    v3_pending = not legacy_has_data  # quality tab cannot render without raw_*

    sources_present: set = set()
    if legacy_has_data:
        try:
            # bma_med.* doesn't have a data_source column on patient — derive
            # presence from which per-source pt table holds each patient_id.
            presence = execute_query("""
                SELECT 'app1' AS data_source, COUNT(*) AS n FROM bma_med.app1_pt
                UNION ALL
                SELECT 'portal' AS data_source, COUNT(*) AS n FROM bma_med.portal_pt
                UNION ALL
                SELECT 'app2' AS data_source, COUNT(*) AS n FROM bma_med.app2_app2
            """) or []
            sources_present = {r["data_source"] for r in presence if r["n"] > 0}
        except Exception:
            sources_present = set()
    else:
        # v3-only: probe summary_district_disease (compat view) for presence
        try:
            presence = execute_query(
                "SELECT data_source, SUM(total_screened)::int AS n "
                "FROM summary_district_disease GROUP BY data_source"
            ) or []
            sources_present = {r["data_source"] for r in presence if (r["n"] or 0) > 0}
        except Exception:
            sources_present = set()

    coverage: List[Dict] = []
    distribution: Dict = {}
    quality: Dict = {}

    try:
        coverage     = _coverage_matrix()
        distribution = _distribution_comparison()
        # _data_quality_per_source reads raw_* — skip on v3-only DBs
        if legacy_has_data:
            quality  = _data_quality_per_source()
    except Exception as exc:
        logger.exception("cross-stats failed: %s", exc)

    # Precompute heatmap max for color scaling (for coverage tab)
    max_n = 0
    for r in coverage:
        for src in _CROSS_SOURCES:
            max_n = max(max_n, int(r.get(f"n_{src}") or 0))

    return templates.TemplateResponse(
        "admin/cross_stats.html",
        {
            "request": request,
            "tab": tab,
            "sources": list(_CROSS_SOURCES),
            "sources_present": sources_present,
            "coverage": coverage,
            "coverage_max": max_n or 1,
            "distribution": distribution,
            "quality": quality,
            # quality tab is the only one that depends on legacy raw_* tables
            "v3_pending_quality_tab": v3_pending,
            "messages": _get_flash(request),
        },
    )

# =========================================================================== #
# LOGS
# =========================================================================== #

@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    """Show recent import log entries."""
    _require_auth(request)

    try:
        rows = execute_query("""
            SELECT id, filename, file_type, status, error_message,
                   started_at, completed_at, duration_seconds,
                   rows_imported, rows_skipped
            FROM import_history
            ORDER BY started_at DESC
            LIMIT 100
        """)
    except Exception:
        rows = []

    # Format as log lines
    log_lines = []
    for r in rows:
        ts = r.get("started_at", "")
        status = r.get("status", "unknown")
        fname = r.get("filename", "?")
        ftype = r.get("file_type", "?")
        dur = r.get("duration_seconds") or 0
        imported = r.get("rows_imported") or 0
        err = r.get("error_message") or ""

        line = f"[{ts}] {status.upper():7s} | {fname} ({ftype}) | {imported} rows | {dur:.1f}s"
        if status == "error" and err:
            line += f" | ERROR: {err[:200]}"
        log_lines.append({"text": line, "status": status})

    return templates.TemplateResponse(
        "admin/logs.html",
        {
            "request": request,
            "log_lines": log_lines,
            "messages": _get_flash(request),
        },
    )

# =========================================================================== #
# PDPA ERASURE
# =========================================================================== #

# Tables that hold per-patient data — checked post-erasure to confirm
# the SQL function actually removed everything.
_PATIENT_DATA_TABLES = (
    "raw_visits", "raw_vitalsigns", "raw_homevisit",
    "raw_homehealth", "raw_lab_results", "raw_lab_extended",
)


@router.post("/erasure", response_class=HTMLResponse)
async def process_erasure(request: Request, idcard_hash: str = Form(...), csrf_token: str = Form("")):
    """Process a PDPA erasure request for a patient by idcard_hash.

    Hardened flow:
      1. Log a 'pending' audit row BEFORE deletion so the request is
         recorded even if the deletion crashes mid-way.
      2. Run execute_patient_erasure() (DB function).
      3. Verify by counting rows that still reference the patient — if any
         remain, mark the audit row as 'incomplete' and surface the error.
      4. On success, refresh materialized views and update the audit row
         with the final row count.
    """
    _require_auth(request)
    if not _validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF validation failed")

    audit_id: Optional[int] = None
    try:
        with get_conn() as conn:
            cur = conn.cursor()

            # 1. Pre-log audit row in 'pending' state
            cur.execute(
                """INSERT INTO erasure_requests
                   (idcard_hash, status, processed_date, rows_deleted, processed_by)
                   VALUES (%s, 'pending', NOW(), 0, 'admin')
                   RETURNING id""",
                (idcard_hash,),
            )
            audit_id = cur.fetchone()[0]
            conn.commit()

            # 2. Execute erasure
            cur.execute("SELECT execute_patient_erasure(%s)", (idcard_hash,))
            rows_deleted = cur.fetchone()[0]

            # 3. Verify — count any rows that still reference this patient.
            # bma_med.patient.pid_encoded is the renamed equivalent of idcard_hash.
            cur.execute(
                "SELECT patient_id FROM bma_med.patient WHERE pid_encoded = %s",
                (idcard_hash,),
            )
            patient_ids = [r[0] for r in cur.fetchall()]
            residual = 0
            if patient_ids:
                # Patient row(s) still present — count children too
                for tbl in _PATIENT_DATA_TABLES:
                    cur.execute(
                        f"SELECT COUNT(*) FROM {tbl} WHERE patient_id = ANY(%s)",
                        (patient_ids,),
                    )
                    residual += cur.fetchone()[0]
                residual += len(patient_ids)

            if residual > 0:
                cur.execute(
                    """UPDATE erasure_requests
                       SET status = 'incomplete', rows_deleted = %s,
                           reason = %s
                       WHERE id = %s""",
                    (rows_deleted, f"verify failed: {residual} residual rows remain", audit_id),
                )
                conn.commit()
                logger.error(
                    "PDPA erasure incomplete for hash=%s — %d residual rows",
                    idcard_hash[:12] + "…", residual,
                )
                response = RedirectResponse(url="/admin/dashboard", status_code=303)
                _set_flash(
                    response, "error",
                    f"Erasure INCOMPLETE: {rows_deleted} deleted, {residual} residual. "
                    "Check erasure_requests table.",
                )
                return response

            # 4. All clean — refresh views + finalise audit
            cur.execute(
                """UPDATE erasure_requests
                   SET status = 'completed', rows_deleted = %s
                   WHERE id = %s""",
                (rows_deleted, audit_id),
            )
            etl = _load_etl()
            etl.refresh_all_summaries(cur)
            conn.commit()

        response = RedirectResponse(url="/admin/dashboard", status_code=303)
        _set_flash(response, "success",
                   f"Erasure complete + verified: {rows_deleted} records deleted.")
        return response
    except Exception as exc:
        # Mark the audit row failed if we got far enough to insert it
        if audit_id is not None:
            try:
                with get_conn() as _conn:
                    _conn.cursor().execute(
                        """UPDATE erasure_requests
                           SET status = 'failed', reason = %s
                           WHERE id = %s""",
                        (_sanitize_error(exc), audit_id),
                    )
                    _conn.commit()
            except Exception:
                logger.exception("Could not mark erasure_request id=%s as failed", audit_id)
        response = RedirectResponse(url="/admin/dashboard", status_code=303)
        _set_flash(response, "error", f"Erasure failed: {_sanitize_error(exc)}")
        return response

# =========================================================================== #
# API: Table counts (JSON, for AJAX dashboard refresh)
# =========================================================================== #

@router.get("/api/table-counts")
async def api_table_counts(request: Request, source: str = "all"):
    """Return table and view row counts as JSON, optionally filtered by source.

    Query param:
      source: all | portal | app1 | app2 — filters both raw tables and
              materialized views (all views have data_source after migration 012).
    """
    _require_auth(request)
    source = _normalize_source(source)
    where_clause, params = _source_where_clause(source)

    raw_table_names = (
        "raw_patients", "raw_visits", "raw_vitalsigns",
        "raw_homevisit", "raw_homehealth",
        "raw_lab_results", "raw_lab_extended",
    )
    raw_tables = []
    for tbl in raw_table_names:
        sql = f'SELECT COUNT(*) AS count FROM "{tbl}" {where_clause}'
        cnt = execute_scalar(sql, params) or 0
        raw_tables.append({"name": tbl, "count": cnt})

    mat_views = execute_query("""
        SELECT matviewname AS name
        FROM pg_matviews
        WHERE schemaname = 'public'
        ORDER BY matviewname
    """) or []

    # Probe which views actually have data_source column (not all do)
    views_with_source = set()
    if where_clause:
        src_rows = execute_query("""
            SELECT c.relname AS name
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid
            WHERE c.relkind = 'm' AND n.nspname = 'public'
              AND a.attnum > 0 AND a.attname = 'data_source'
        """) or []
        views_with_source = {r["name"] for r in src_rows}

    view_counts = []
    for mv in mat_views:
        view_name = mv["name"]
        try:
            if where_clause and view_name in views_with_source:
                view_sql = f'SELECT COUNT(*) FROM "{view_name}" WHERE data_source = %s'
                row_count = execute_scalar(view_sql, params) or 0
            else:
                row_count = execute_scalar(f'SELECT COUNT(*) FROM "{view_name}"') or 0
        except Exception as view_exc:
            logger.warning("api_table_counts view %s failed: %s", view_name, view_exc)
            row_count = 0
        view_counts.append({
            "name": view_name,
            "count": row_count,
            "has_source_col": view_name in views_with_source,
        })

    return JSONResponse({
        "source": source,
        "raw_tables": raw_tables,
        "materialized_views": view_counts,
    })

# =========================================================================== #
# API: Import status (JSON, for polling)
# =========================================================================== #

@router.get("/api/import-status/{history_id}")
async def api_import_status(request: Request, history_id: int):
    """Return the current status of an import job."""
    _require_auth(request)

    rows = execute_query(
        "SELECT * FROM import_history WHERE id = %s", (history_id,)
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Import job not found")
    return JSONResponse({k: str(v) if v is not None else None for k, v in rows[0].items()})


# =========================================================================== #
# BUNDLE UPLOAD (up to 13 CSV files across 3 sources)
# =========================================================================== #

# Import order: Portal first (7 files), App1 next (5 files), App2 last (1 file).
# pt.csv MUST come before child tables because they reference patient_id.
SOURCE_IMPORT_ORDER = ("portal", "app1", "app2")
FILE_TYPE_ORDER = (
    "pt", "pthistory", "vitalsignslf",
    "homevisit", "homehealth",
    "labhealth", "labhealthext",
    "app2",
)

# Max per-file upload (streamed to tempfile; no full-in-memory buffering)
MAX_FILE_BYTES = 1024 * 1024 * 1024  # 1 GB per file


def _detect_source_from_path(relpath: str) -> Optional[str]:
    """Extract source ('portal'/'app1'/'app2') from the upload relpath.

    Users upload a folder; relpath looks like 'BMI_100/portal/pt.csv' or
    'portal/pt.csv'. We match case-insensitively on path segments.
    """
    if not relpath:
        return None
    parts = [p.lower() for p in relpath.replace("\\", "/").split("/") if p]
    for part in parts:
        if part in ("portal", "app1", "app2"):
            return part
    return None


def _detect_file_type_from_name(name: str) -> Optional[str]:
    """Guess file_type from the filename (e.g., 'pt.csv', 'labhealthext.csv')."""
    if not name:
        return None
    n = name.lower()
    if n.endswith("app2.csv") or "app2" in n:
        return "app2"
    if "labhealthext" in n:
        return "labhealthext"
    if "labhealth" in n:
        return "labhealth"
    if "vitalsign" in n:
        return "vitalsignslf"
    if "homehealth" in n:
        return "homehealth"
    if "homevisit" in n:
        return "homevisit"
    if "pthistory" in n:
        return "pthistory"
    if n == "pt.csv" or n.endswith("/pt.csv"):
        return "pt"
    return None


# --------------------------------------------------------------------------- #
# Facility bootstrap removed (2026-05-01) — `_ensure_facilities_seeded` was
# the v3 ETL's one-time seed of the legacy facility table from
# clinic_latlong.xls. That schema is gone; facility data now lives under
# `bma_med.*` and is seeded by the new pipeline directly. Helper had no live
# callers.
# --------------------------------------------------------------------------- #


# ─── bma-med pipeline integration helpers ──────────────────────────────────
# Path to the bma-med repo (sibling checkout). The four pipeline scripts
# (ingest.py / clean.py / validate.py / export.py) live at the top level.
BMA_MED_ROOT = "/Users/dev/bma-med"


def _set_history_error(history_id: int, error: str,
                       detail: Optional[str] = None) -> None:
    """Mark an import_history row as failed with an error message + detail.

    `detail` is appended to `error_message` for human-readable diagnosis
    (e.g. validation report markdown). Truncates the combined message to
    avoid blowing past column limits — full reports remain on disk.
    """
    msg = error if not detail else f"{error}\n\n{detail}"
    if len(msg) > 8000:
        msg = msg[:8000] + "\n…(truncated)"
    _update_history(history_id, "error", 0, 0, msg, 0.0)


def _refresh_hot_mvs(cur) -> Dict[str, str]:
    """Refresh the hot materialized views used by the public dashboard.

    Each view is refreshed in its own try/except so a missing or broken MV
    doesn't sink the whole upload. Returns {view_name: 'ok' | error_msg}.
    """
    views = (
        "public.mv_visit_resolved",
        "public.summary_district_disease",
        "public.summary_facility",
        "public.summary_disease_age_sex",
    )
    results: Dict[str, str] = {}
    for v in views:
        try:
            cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {v}")
            results[v] = "ok"
            logger.info("Refreshed MV %s", v)
        except Exception as exc:
            err = _sanitize_error(exc)
            results[v] = err
            logger.warning("MV refresh failed for %s (non-fatal): %s", v, err)
            # Roll back the failed REFRESH so the connection is usable again.
            try:
                cur.connection.rollback()
            except Exception:
                pass
    return results


# --------------------------------------------------------------------------- #
# Legacy bundle-upload endpoints removed (2026-05-01).
#
# `/admin/upload-bundle` (GET + POST) and the background `_run_bundle_import`
# worker staged xlsx → CSV → ingest → clean → validate → export → MV refresh
# directly into the legacy schema. That schema has been dropped; the JSON
# upload API (`/api/admin/upload-excel`, defined below) is the only supported
# path now.
# Stub the old route so legacy frontends/bookmarks get a clear 410 instead of
# a confusing 404.
# --------------------------------------------------------------------------- #

@router.api_route("/upload-bundle", methods=["GET", "POST"], include_in_schema=False)
async def upload_bundle_gone(request: Request):
    raise HTTPException(
        status_code=410,
        detail="Endpoint replaced by /api/admin/upload-excel",
    )


# =========================================================================== #
# JSON UPLOAD API — /api/admin/upload-excel  (Bearer-token auth)
# =========================================================================== #
#
# Production-secrecy upload path. Accepts:
#   • .xlsx — demuxed via /Users/dev/bma-med/xlsx_to_bmi100.py
#   • .zip  — must contain a BMI_100/{app1,app2,portal}/*.csv layout
#
# Security guards (see SECURITY CHECKLIST in commit 115_upload_excel_columns):
#   1. Bearer-token via _require_admin (separate from session-cookie auth)
#   2. Streaming write w/ MAX_UPLOAD_MB cap (default 1024 MB / 1 GB, env-override via BMA_UPLOAD_MAX_MB)
#   3. Extension allow-list  (.xlsx | .zip)
#   4. Magic-byte sniff      (PK\x03\x04 — both .xlsx and .zip are zip-based)
#   5. Filename sanitization (basename + alnum/._- only, 120-char cap)
#   6. SHA-256 of body recorded for tamper detection
#   7. Zip path-traversal guard (reject ".." / absolute / out-of-root members)
#   8. Tempfile cleanup in `finally` regardless of outcome
#   9. Audit log via bma_med.security.audit on every state transition
#  10. Sanitized errors only — never raw tracebacks in API responses
#  11. Pending-confirm tmpdirs auto-cleaned after 2h (janitor task)
# =========================================================================== #

MAX_UPLOAD_MB = int(os.environ.get("BMA_UPLOAD_MAX_MB", "1024"))
ALLOWED_EXT   = {".xlsx", ".zip"}
ALLOWED_MAGIC = {b"PK\x03\x04"}  # both .zip and .xlsx are zip-based
ADMIN_BEARER_ENV = "BMA_ADMIN_TOKEN"

# Subprocess timeouts (seconds). At 1 GB / ~10 M rows the export step is the
# bottleneck — keep generous defaults; override per-deployment if needed.
PIPELINE_TIMEOUT_INGEST   = int(os.environ.get("BMA_TIMEOUT_INGEST",   "3600"))
PIPELINE_TIMEOUT_CLEAN    = int(os.environ.get("BMA_TIMEOUT_CLEAN",    "3600"))
PIPELINE_TIMEOUT_VALIDATE = int(os.environ.get("BMA_TIMEOUT_VALIDATE", "1800"))
PIPELINE_TIMEOUT_EXPORT   = int(os.environ.get("BMA_TIMEOUT_EXPORT",   "7200"))
PIPELINE_TIMEOUT_DEMUX    = int(os.environ.get("BMA_TIMEOUT_DEMUX",    "900"))


def _safe_filename(name: str) -> str:
    """Strip directories, keep only alnum + ._- and cap at 120 chars."""
    base = os.path.basename(name or "")
    cleaned = "".join(c for c in base if c.isalnum() or c in "._-")[:120]
    return cleaned or "upload"


def _require_admin(authorization: Optional[str]) -> None:
    """Enforce Bearer-token auth for the JSON admin API.

    Reads the expected token from BMA_ADMIN_TOKEN env. If unset or empty, the
    JSON API is hard-disabled (HTTP 503) — fail closed so a misconfigured prod
    box doesn't accept uploads anonymously.
    """
    expected = os.environ.get(ADMIN_BEARER_ENV, "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Admin upload API is disabled (BMA_ADMIN_TOKEN not configured).",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.split(None, 1)[1].strip()
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid Bearer token")


def _create_history_row(*, filename: str, kind: str,
                        size_bytes: int, sha256: str,
                        uploaded_path: str,
                        load_mode: str = "replace") -> int:
    """Insert a new import_history row in 'queued' state and return its id."""
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL_WRITER)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO import_history
                  (filename, table_name, file_type, status, started_at,
                   sha256, size_bytes, kind, uploaded_path, load_mode)
                VALUES (%s, %s, %s, 'queued', NOW(), %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (filename[:255], "ALL", kind, sha256, size_bytes,
                 kind, uploaded_path, load_mode),
            )
            return cur.fetchone()[0]
    finally:
        if conn:
            conn.close()


def _fetch_history(history_id: int) -> Dict:
    """Return the full import_history row as a dict (or 404)."""
    rows = execute_query(
        "SELECT * FROM import_history WHERE id = %s", (history_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="history not found")
    return dict(rows[0])


def _set_history_fields(history_id: int, **fields) -> None:
    """Generic UPDATE on import_history for arbitrary whitelisted columns.

    Whitelist enforced — never lets caller-supplied keys slip into SQL.
    """
    allowed = {
        "status", "validate_status", "validate_report",
        "tmpdir_path", "uploaded_path", "kind",
        "view_refresh_status", "view_refresh_error",
        "rows_imported", "rows_skipped", "error_message",
        "duration_seconds", "completed_at",
        "load_mode", "detail",
    }
    safe = {k: v for k, v in fields.items() if k in allowed}
    if not safe:
        return
    cols = ", ".join(f"{k} = %s" for k in safe)
    vals = list(safe.values()) + [history_id]
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL_WRITER)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE import_history SET {cols} WHERE id = %s",
                tuple(vals),
            )
    except Exception:
        logger.exception("Failed to update history fields id=%s", history_id)
    finally:
        if conn:
            conn.close()


def _audit(history_id: int, transition: str, **detail) -> None:
    """Best-effort audit-log write for upload-excel state transitions.

    Never raises — audit is informational; a logging failure must not abort
    the import. Imports the helper from /Users/dev/bma-med/security/audit.py.
    """
    try:
        import sys as _sys
        if BMA_MED_ROOT not in _sys.path:
            _sys.path.insert(0, BMA_MED_ROOT)
        from security.audit import audit_event  # type: ignore
        ev = audit_event(
            operator=os.environ.get("BMA_OPERATOR", "admin"),
            operation="UPLOAD_EXCEL",
            resource=f"admin.upload-excel:{transition}",
            params={"history_id": history_id},
            detail=detail or None,
        )
        logger.info("audit-log[%s]: %s", transition, ev)
    except Exception as exc:
        logger.warning("audit-log failed (non-fatal) [%s]: %s", transition, exc)


# --------------------------------------------------------------------------- #
# Pipeline runner — the body that actually processes the staged upload.
# --------------------------------------------------------------------------- #

def _safe_extract_zip(zip_path: str, dest_dir: str) -> List[str]:
    """Extract a zip into dest_dir, rejecting any path-traversal members.

    Returns the list of extracted relative paths. Raises ValueError on a
    traversal attempt — the caller should mark the import 'error' and abort.
    """
    extracted: List[str] = []
    dest_abs = os.path.realpath(dest_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            name = member.filename
            # Reject obvious traversal patterns BEFORE resolving.
            if (
                not name
                or name.startswith("/")
                or name.startswith("\\")
                or ".." in name.replace("\\", "/").split("/")
                or os.path.isabs(name)
            ):
                raise ValueError(f"unsafe zip member rejected: {name!r}")
            target = os.path.realpath(os.path.join(dest_abs, name))
            # Final containment check — defends against symlinks-in-zip and
            # encoded path tricks that string-checks can miss.
            if not (target == dest_abs or target.startswith(dest_abs + os.sep)):
                raise ValueError(f"zip member escapes root: {name!r}")
            zf.extract(member, dest_abs)
            extracted.append(name)
    return extracted


def _run_pipeline_upload(uploaded_path: str, kind: str, history_id: int,
                         load_mode: str = "replace") -> None:
    """Background pipeline for the JSON upload-excel endpoint.

    Stages the upload into a BMI_100/-shaped tmpdir, then runs ingest →
    clean → validate. Outcome:
        validate rc=0  → continue to export → MV refresh → success
        validate rc=1  → status='validation_failed', stop
        validate rc=2  → status='pending_confirm', tmpdir kept; operator
                         must call /api/admin/upload-excel/confirm
    On success/cancel/error the tmpdir + uploaded_path are cleaned up.

    `load_mode` ∈ {'replace', 'append'} is forwarded to
    `_resume_pipeline_export` so the rc=0 inline path (and the deferred
    /confirm path, via `import_history.load_mode`) honour the operator's
    chosen replace/append semantics.
    """
    import subprocess
    import tempfile
    from pathlib import Path as _Path

    start = time.time()
    _set_history_fields(history_id, status="running")
    _audit(history_id, "start", kind=kind, uploaded_path=uploaded_path)

    tmpdir = tempfile.mkdtemp(prefix="bma_upload_xlsx_")
    _set_history_fields(history_id, tmpdir_path=tmpdir)

    paused_for_confirm = False
    try:
        _update_progress(history_id, "stage files", 5)

        if kind == "zip":
            try:
                _safe_extract_zip(uploaded_path, tmpdir)
            except (zipfile.BadZipFile, ValueError) as exc:
                _set_history_error(history_id, "zip extraction failed",
                                   detail=str(exc)[:2000])
                _audit(history_id, "error",
                       reason="bad_zip", message=str(exc)[:200])
                return
        elif kind == "xlsx":
            r = subprocess.run(
                ["python3",
                 os.path.join(BMA_MED_ROOT, "xlsx_to_bmi100.py"),
                 uploaded_path, tmpdir],
                capture_output=True, text=True,
                timeout=PIPELINE_TIMEOUT_DEMUX,
            )
            if r.returncode != 0:
                _set_history_error(
                    history_id, "xlsx demux failed",
                    detail=(r.stderr or r.stdout or "")[-4000:],
                )
                _audit(history_id, "error",
                       reason="xlsx_demux_failed",
                       rc=r.returncode)
                return
        else:
            _set_history_error(history_id, f"unknown kind {kind!r}")
            return

        # If the demuxer/zip used a BMI_100/ wrapper directory, descend into
        # it so subsequent scripts see {portal,app1,app2}/ at the root.
        bmi_root = _Path(tmpdir) / "BMI_100"
        raw_root = str(bmi_root) if bmi_root.is_dir() else tmpdir
        env = {**os.environ, "RAW_ROOT": raw_root}

        # ─── 1/4 ingest ────────────────────────────────────────────────
        _update_progress(history_id, "ingest", 15)
        r = subprocess.run(
            ["python3", os.path.join(BMA_MED_ROOT, "ingest.py")],
            env=env, cwd=BMA_MED_ROOT,
            capture_output=True, text=True,
            timeout=PIPELINE_TIMEOUT_INGEST,
        )
        if r.returncode != 0:
            _set_history_error(history_id, "ingest failed",
                               detail=(r.stderr or r.stdout or "")[-4000:])
            _audit(history_id, "error", reason="ingest_failed",
                   rc=r.returncode)
            return

        # ─── 2/4 clean ─────────────────────────────────────────────────
        _update_progress(history_id, "clean", 35)
        r = subprocess.run(
            ["python3", os.path.join(BMA_MED_ROOT, "clean.py")],
            env=env, cwd=BMA_MED_ROOT,
            capture_output=True, text=True,
            timeout=PIPELINE_TIMEOUT_CLEAN,
        )
        if r.returncode != 0:
            _set_history_error(history_id, "clean failed",
                               detail=(r.stderr or r.stdout or "")[-4000:])
            _audit(history_id, "error", reason="clean_failed",
                   rc=r.returncode)
            return

        # ─── 3/4 validate ──────────────────────────────────────────────
        _update_progress(history_id, "validate", 55)
        r = subprocess.run(
            ["python3", os.path.join(BMA_MED_ROOT, "validate.py")],
            env=env, cwd=BMA_MED_ROOT,
            capture_output=True, text=True,
            timeout=PIPELINE_TIMEOUT_VALIDATE,
        )
        report_path = _Path(BMA_MED_ROOT) / "output" / "validate" / "report.md"
        report_md = ""
        if report_path.exists():
            try:
                report_md = report_path.read_text(encoding="utf-8")
            except Exception:
                report_md = ""

        if r.returncode == 1:
            _set_history_fields(
                history_id,
                status="validation_failed",
                validate_status="fail",
                validate_report=(report_md or
                                 (r.stderr or r.stdout or "")[-8000:]),
                error_message="validation failed",
                completed_at=datetime.now(),
            )
            _audit(history_id, "validate_fail")
            return
        if r.returncode == 2:
            # Warnings-only — pause for operator confirmation. Tmpdir stays
            # alive; the janitor wipes it after 2h if no confirm/cancel.
            #
            # In replace mode, prepend a destructiveness notice so the
            # operator sees what's about to happen *before* clicking
            # "ดำเนินการต่อ". We compute a row-count preview cheaply and
            # fall back to a generic warning if the probe fails — the
            # message must always render even when the DB probe errors.
            warn_report = report_md or ""
            if load_mode == "replace":
                preview_total = _replace_mode_preview_row_count()
                if preview_total is None:
                    notice = (
                        "## ⚠️ โหมด REPLACE\n\n"
                        "ระบบจะลบข้อมูลทั้งหมดใน `bma_med.*` ก่อนโหลดใหม่ "
                        "(จำนวนแถวปัจจุบัน: ตรวจสอบไม่ได้). "
                        "ข้อมูลปัจจุบันจะถูกลบและไม่สามารถกู้คืนได้.\n\n"
                        "---\n\n"
                    )
                else:
                    notice = (
                        f"## ⚠️ โหมด REPLACE\n\n"
                        f"ระบบจะลบข้อมูลทั้งหมด **{preview_total:,} rows** "
                        f"ก่อนโหลดใหม่. "
                        f"ข้อมูลปัจจุบันใน bma_med.* ทั้งหมดจะถูกลบ "
                        f"ไม่สามารถกู้คืนได้.\n\n"
                        "---\n\n"
                    )
                warn_report = notice + warn_report
            _set_history_fields(
                history_id,
                status="pending_confirm",
                validate_status="warning",
                validate_report=warn_report,
            )
            paused_for_confirm = True
            _audit(history_id, "pending_confirm", load_mode=load_mode)
            return
        if r.returncode != 0:
            _set_history_error(history_id,
                               f"validate failed (rc={r.returncode})",
                               detail=(r.stderr or r.stdout or "")[-4000:])
            _audit(history_id, "error", reason="validate_failed",
                   rc=r.returncode)
            return

        # rc == 0 — clean run; export inline.
        _set_history_fields(history_id, validate_status="pass",
                            validate_report=report_md)
        _resume_pipeline_export(
            tmpdir, kind, history_id,
            env_raw_root=raw_root, load_mode=load_mode,
        )

    except Exception as exc:
        _set_history_error(history_id, "pipeline failed",
                           detail=_sanitize_error(exc))
        _audit(history_id, "error", reason="pipeline_exception",
               message=_sanitize_error(exc))
        logger.exception("upload-excel pipeline failed (history=%s)", history_id)
    finally:
        # Always clean the original uploaded file.
        try:
            os.unlink(uploaded_path)
        except OSError:
            pass
        # Cleanup tmpdir UNLESS we're holding it for operator confirmation.
        if not paused_for_confirm:
            shutil.rmtree(tmpdir, ignore_errors=True)
            _set_history_fields(history_id, tmpdir_path=None)
        # Best-effort duration update if the row didn't already finalize.
        try:
            row = _fetch_history(history_id)
            if row.get("status") in ("running", "queued"):
                _set_history_fields(
                    history_id, duration_seconds=round(time.time() - start, 2),
                )
        except Exception:
            pass


# Tables touched by export.py that we sanity-check after the export step.
# If NONE of these grew (and no rows existed before), the export silently
# no-op'd — most likely the runtime user is missing USAGE on bma_med (which
# returns empty `information_schema.columns` and lets export.py exit 0 with
# zero rows inserted instead of raising InsufficientPrivilege).
_POST_EXPORT_GROWTH_TABLES = (
    "bma_med.patient",
    "bma_med.app1_patient",
    "bma_med.app2_patient",
    "bma_med.portal_patient",
)


def _table_row_counts(conn: object, tables: tuple) -> Dict[str, Optional[int]]:
    """Return {table: count or None on error} using its own cursor.

    A `None` value means the count failed (e.g. permission denied / missing
    table) — treated by the sanity check as "not grown".
    """
    out: Dict[str, Optional[int]] = {}
    for t in tables:
        try:
            cur = conn.cursor()  # type: ignore[attr-defined]
            try:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                row = cur.fetchone()
                out[t] = int(row[0]) if row else 0
            finally:
                cur.close()
        except Exception:
            out[t] = None
            # The failed COUNT may have aborted a transaction — roll back so
            # the next COUNT runs cleanly.
            try:
                conn.rollback()  # type: ignore[attr-defined]
            except Exception:
                pass
    return out


def _post_export_sanity_check(history_id: int, conn: object) -> Optional[str]:
    """Compare expected vs actual row growth in bma_med.* tables.

    Mark history_id as status='error' with a clear message if the
    growth is zero across all targeted tables.

    Returns None on success (at least one table has rows), or an error
    message (also written to import_history) when every targeted table is
    empty / inaccessible. Caller must `return` after a non-None reply.
    """
    counts = _table_row_counts(conn, _POST_EXPORT_GROWTH_TABLES)
    total = sum((v or 0) for v in counts.values())
    if total > 0:
        return None
    summary = ", ".join(
        f"{t}={'?' if v is None else v}" for t, v in counts.items()
    )
    msg = (
        "post-flight sanity check failed: export reported success but no "
        "bma_med target table contains any rows. This usually means the "
        "runtime DB user lacks USAGE/INSERT on the bma_med schema "
        f"(observed counts: {summary})."
    )
    _set_history_error(history_id, "export sanity check failed", detail=msg)
    _audit(history_id, "error",
           reason="post_export_sanity_check_failed",
           counts={k: v for k, v in counts.items()})
    return msg


# Schema-level metadata tables that must NEVER be truncated by load_mode=replace.
# These hold codebooks, source registry, and audit history that survive a
# data refresh — wiping them would force a re-bootstrap from schema_init.sql.
_BMA_MED_PROTECTED_TABLES = frozenset({
    "audit_log",
    "codebook",
    "source",
    "table_origin",
    "variable",
})


def _replace_mode_preview_row_count() -> Optional[int]:
    """Best-effort total row count across bma_med data tables.

    Used to render a destructiveness warning ("ระบบจะลบข้อมูลทั้งหมด N rows
    ก่อนโหลดใหม่") in the pending_confirm validate report. Returns None on
    any DB error so the surrounding caller can fall back to a generic
    warning instead of failing the validation gate.
    """
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL_WRITER)
        conn.autocommit = True
        tables = _discover_bma_med_data_tables(conn)
        total = 0
        for t in tables:
            with conn.cursor() as cur:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {t}")
                    row = cur.fetchone()
                    total += int(row[0]) if row else 0
                except Exception:
                    # Table-level COUNT failure is non-fatal — skip and move
                    # on. Roll back so the next COUNT runs cleanly.
                    try:
                        conn.rollback()
                    except Exception:
                        pass
        return total
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _discover_bma_med_data_tables(conn: Any) -> List[str]:
    """Discover bma_med data tables eligible for TRUNCATE under load_mode=replace.

    Excludes the protected metadata set (audit_log, codebook, source,
    table_origin, variable). Returns fully-qualified `bma_med.<name>` strings
    in stable alphabetical order so logs/assertions are deterministic.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT tablename
              FROM pg_tables
             WHERE schemaname = 'bma_med'
               AND tablename NOT IN %s
             ORDER BY tablename
            """,
            (tuple(_BMA_MED_PROTECTED_TABLES),),
        )
        rows = cur.fetchall() or []
    finally:
        cur.close()
    return [f"bma_med.{r[0]}" for r in rows]


def _truncate_bma_med_tables(
    conn: Any,
    history_id: int,
    *,
    log_prefix: str = "load_mode=replace",
) -> Dict[str, int]:
    """Capture row counts then TRUNCATE every bma_med data table CASCADE.

    Runs inside the caller's open transaction (autocommit must be False).
    The caller is responsible for COMMIT / ROLLBACK around the entire
    truncate-then-export sequence. Returns the pre-truncate counts so the
    caller can store them in `import_history.detail.pre_truncate_counts`.

    Permission failure guard: if the runtime role lacks TRUNCATE on bma_med,
    psycopg2 raises InsufficientPrivilege. We re-raise so the caller can
    flip status='error' with a setup-style message naming the missing
    GRANT — never silently swallow.
    """
    tables = _discover_bma_med_data_tables(conn)
    counts: Dict[str, int] = {}
    for t in tables:
        try:
            cur = conn.cursor()
            try:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                row = cur.fetchone()
                counts[t] = int(row[0]) if row else 0
            finally:
                cur.close()
        except Exception as exc:
            # COUNT failure aborts the transaction — re-raise so the caller
            # can roll back. We avoid the `_table_row_counts` rollback path
            # because that would also undo any prior work in this same txn.
            raise RuntimeError(
                f"pre-truncate COUNT(*) on {t} failed: {_sanitize_error(exc)}"
            ) from exc

    total_rows = sum(counts.values())
    logger.info(
        "%s: would truncate %d tables, totaling %d rows (history=%s)",
        log_prefix, len(tables), total_rows, history_id,
    )

    # TRUNCATE in a single statement so CASCADE handles FK ordering for us.
    # Empty list shouldn't be possible (bma_med always has data tables once
    # schema_init has run), but be defensive.
    if tables:
        cur = conn.cursor()
        try:
            cur.execute(f"TRUNCATE TABLE {', '.join(tables)} CASCADE")
        finally:
            cur.close()
        logger.info(
            "%s: TRUNCATE issued on %d tables (history=%s)",
            log_prefix, len(tables), history_id,
        )

    return counts


def _resume_pipeline_export(tmpdir: str, kind: str, history_id: int,
                            *, env_raw_root: Optional[str] = None,
                            load_mode: str = "replace") -> None:
    """Run export → MV refresh → flush caches → mark success.

    Used both by the inline rc=0 path and by the /confirm endpoint when the
    operator approves a 'pending_confirm' upload.

    Load-mode handling
    ------------------
    * `load_mode='replace'` (default): open a transaction, TRUNCATE every
      bma_med data table CASCADE, run export.py inside the same transaction.
      On export failure → ROLLBACK (data preserved). On success → COMMIT.
    * `load_mode='append'`: skip the truncate, run export.py against the
      existing data set (UPSERT / merge semantics from export.py).

    Silent-failure guard
    --------------------
    Historically the export step was treated as successful whenever
    `export.py` exited rc=0. That allowed a class of permission bugs to ship
    "success" with `rows_imported=0`: when the runtime DB user (e.g.
    `etl_user`) lacks USAGE on the `bma_med` schema, `information_schema.
    columns` legitimately returns empty rows, so export.py skips every
    target table, exits 0, and the dashboard then shows zero rows with no
    indication that anything is wrong. The post-flight sanity check below
    closes that gap by counting rows in the canonical target tables and
    flipping the history row to `status='error'` when none of them grew.
    Any unhandled `psycopg2.Error` (e.g. `InsufficientPrivilege`) raised
    while running the check is also caught and recorded with a sanitised
    message — never silently swallowed.
    """
    import json as _json
    import subprocess
    from pathlib import Path as _Path

    # Normalise load_mode — defensive against legacy callers passing None or
    # an unexpected string. Anything other than 'append' falls back to the
    # default 'replace' so the user's stated expectation is honoured.
    if load_mode not in ("replace", "append"):
        logger.warning(
            "unknown load_mode=%r — defaulting to 'replace' (history=%s)",
            load_mode, history_id,
        )
        load_mode = "replace"

    start = time.time()
    raw_root = env_raw_root
    if raw_root is None:
        bmi = _Path(tmpdir) / "BMI_100"
        raw_root = str(bmi) if bmi.is_dir() else tmpdir
    env = {**os.environ, "RAW_ROOT": raw_root}

    conn = None
    view_status = "skipped"
    view_err: Optional[str] = None
    rows_inserted = 0

    # Transaction handle for load_mode=replace. Held open across the export
    # subprocess so a fatal error during export rolls the TRUNCATE back.
    # Typed as Any because psycopg2 has no stubs in this codebase
    # (mypy.ini sets ignore_missing_imports for psycopg2).
    truncate_conn: Any = None
    pre_truncate_counts: Dict[str, int] = {}

    try:
        # ─── load_mode=replace: TRUNCATE bma_med.* CASCADE then COMMIT ──
        # Runs BEFORE export so the export subprocess sees an empty target
        # set. CRITICAL: we MUST commit the TRUNCATE before launching the
        # export subprocess. TRUNCATE takes ACCESS EXCLUSIVE locks on every
        # target table — holding the transaction open while the export
        # subprocess tries to INSERT into those same tables produces a
        # cross-process deadlock (subprocess waits for locks the parent
        # transaction never releases). The S6 ADR caveat called this out;
        # the original "rollback on export failure" semantics is therefore
        # downgraded to best-effort: if export fails, the prior data IS
        # gone and recovery requires re-uploading.
        if load_mode == "replace":
            _update_progress(history_id, "truncate", 65)
            try:
                truncate_conn = psycopg2.connect(DATABASE_URL_WRITER)
                truncate_conn.autocommit = False
                pre_truncate_counts = _truncate_bma_med_tables(
                    truncate_conn, history_id,
                    log_prefix="load_mode=replace",
                )
                # COMMIT immediately so the ACCESS EXCLUSIVE locks release
                # before the subprocess starts. Without this, export.py
                # blocks on every INSERT and the pipeline hangs forever.
                truncate_conn.commit()
                truncate_conn.close()
                truncate_conn = None
                # Persist the pre-truncate counts + mode for audit visibility.
                detail_payload = {
                    "load_mode": "replace",
                    "pre_truncate_counts": pre_truncate_counts,
                }
                _set_history_fields(
                    history_id,
                    load_mode="replace",
                    detail=_json.dumps(detail_payload),
                )
                _audit(history_id, "truncate",
                       table_count=len(pre_truncate_counts),
                       total_rows=sum(pre_truncate_counts.values()))
            except psycopg2.errors.InsufficientPrivilege as exc:
                # Surface a setup-style message naming the missing GRANT so
                # the operator can fix it. Distinct from generic export
                # errors so monitoring can alert separately.
                if truncate_conn is not None:
                    try:
                        truncate_conn.rollback()
                    except Exception:
                        pass
                msg = (
                    "TRUNCATE failed: runtime DB user lacks privilege on "
                    "bma_med.*. Grant TRUNCATE (e.g. GRANT bma_med_loader "
                    f"TO etl_user). Underlying error: {_sanitize_error(exc)}"
                )
                _set_history_error(history_id, "TRUNCATE permission denied",
                                   detail=msg)
                _audit(history_id, "error",
                       reason="truncate_permission_denied",
                       message=_sanitize_error(exc))
                return
            except Exception as exc:
                if truncate_conn is not None:
                    try:
                        truncate_conn.rollback()
                        truncate_conn.close()
                    except Exception:
                        pass
                    truncate_conn = None
                _set_history_error(history_id, "TRUNCATE failed",
                                   detail=_sanitize_error(exc))
                _audit(history_id, "error", reason="truncate_failed",
                       message=_sanitize_error(exc))
                return
        else:
            # append mode — record it so the audit trail is unambiguous.
            _set_history_fields(
                history_id,
                load_mode="append",
                detail=_json.dumps({"load_mode": "append"}),
            )
            _audit(history_id, "append_mode")

        # ─── 4/4 export ────────────────────────────────────────────────
        _update_progress(history_id, "export", 75)
        r = subprocess.run(
            ["python3", os.path.join(BMA_MED_ROOT, "export.py")],
            env=env, cwd=BMA_MED_ROOT,
            capture_output=True, text=True,
            timeout=PIPELINE_TIMEOUT_EXPORT,
        )
        if r.returncode != 0:
            # TRUNCATE was already committed (replace mode) — prior data
            # is gone. Operator must re-upload to recover. We still flag
            # this as a hard error so the UI surfaces it loudly.
            _set_history_error(history_id, "export failed",
                               detail=(r.stderr or r.stdout or "")[-4000:])
            _audit(history_id, "error", reason="export_failed",
                   rc=r.returncode)
            return

        # In replace mode the TRUNCATE was committed before subprocess.
        # In append mode truncate_conn is None. Either way, no commit
        # work to do here.

        # Best-effort row count from export stdout — never load-bearing.
        try:
            for line in (r.stdout or "").splitlines():
                if "rows" in line.lower() and "exported" in line.lower():
                    digits = "".join(c for c in line if c.isdigit())
                    if digits:
                        rows_inserted = int(digits[-12:])
                        break
        except Exception:
            rows_inserted = 0

        # ─── Post-flight sanity check ──────────────────────────────────
        # export.py exits 0 even when the runtime user lacks USAGE on
        # bma_med (information_schema returns empty → every target table
        # is silently "skipped"). Verify at least one bma_med table grew
        # before flipping the history row to success.
        sanity_conn = None
        try:
            sanity_conn = psycopg2.connect(DATABASE_URL_WRITER)
            sanity_conn.autocommit = True
            if _post_export_sanity_check(history_id, sanity_conn) is not None:
                # _post_export_sanity_check already wrote status='error'
                # and emitted an audit event; abort before the success path.
                return
        except psycopg2.Error as exc:
            _set_history_error(
                history_id, "post-flight sanity check failed",
                detail=_sanitize_error(exc),
            )
            _audit(history_id, "error",
                   reason="post_export_sanity_check_db_error",
                   message=_sanitize_error(exc))
            return
        finally:
            if sanity_conn is not None:
                try:
                    sanity_conn.close()
                except Exception:
                    pass

        # ─── Refresh hot MVs ───────────────────────────────────────────
        _update_progress(history_id, "refresh hot MVs", 92)
        try:
            conn = psycopg2.connect(DATABASE_URL_WRITER)
            conn.autocommit = False
            cur = conn.cursor()
            mv_results = _refresh_hot_mvs(cur)
            conn.commit()
            failed = [k for k, v in mv_results.items() if v != "ok"]
            view_status = "partial" if failed else "success"
            if failed:
                view_err = "failed: " + ", ".join(failed)
        except Exception as exc:
            view_status = "failed"
            view_err = _sanitize_error(exc)
        finally:
            if conn:
                conn.close()

        # ─── Flush caches ──────────────────────────────────────────────
        _update_progress(history_id, "flush caches", 98)
        try:
            from cache import cache_flush_all
            from services.data_adapter import invalidate_cache as invalidate_data_cache
            cache_flush_all()
            invalidate_data_cache()
        except Exception:
            logger.warning("Cache flush after upload-excel failed (non-fatal)")

        duration = time.time() - start
        _update_progress(history_id, "done", 100)
        _update_history(
            history_id, "success", rows_inserted, 0, None, duration,
            view_refresh_status=view_status, view_refresh_error=view_err,
        )
        _audit(history_id, "success",
               rows_inserted=rows_inserted, view_status=view_status)

        # ─── S9 — Bulk pre-build report cache ──────────────────────────
        # Fire-and-forget the popular-set rebuild for every descriptor.
        # The build worker is idempotent (cache hits skip) so re-firing
        # after a no-op data refresh is cheap. Failures are swallowed —
        # users can fall back to live-compile next request.
        try:
            from services.reports.build_worker import get_build_worker
            from services.reports.registry import report_registry as _registry
            _worker = get_build_worker()
            for _rid in _registry().list_ids():
                _worker.enqueue_popular_set(_rid)
            try:
                _loop = asyncio.get_event_loop()
                if _loop.is_running():
                    asyncio.create_task(_worker.run_pending())
                else:
                    threading.Thread(
                        target=lambda: asyncio.run(_worker.run_pending()),
                        daemon=True,
                        name="bma-bulk-prebuild",
                    ).start()
            except RuntimeError:
                # No running loop in this thread — drain in a daemon thread.
                threading.Thread(
                    target=lambda: asyncio.run(_worker.run_pending()),
                    daemon=True,
                    name="bma-bulk-prebuild",
                ).start()
        except Exception:
            logger.warning(
                "S9 bulk pre-build hook failed (non-fatal)",
                exc_info=True,
            )
    except Exception as exc:
        # Roll back the truncate transaction if it's still open so the prior
        # data set is preserved on any unhandled error after truncate.
        if truncate_conn is not None:
            try:
                truncate_conn.rollback()
            except Exception:
                pass
        _set_history_error(history_id, "export pipeline failed",
                           detail=_sanitize_error(exc))
        _audit(history_id, "error", reason="export_exception",
               message=_sanitize_error(exc))
        logger.exception("upload-excel export failed (history=%s)", history_id)
    finally:
        # Always close the truncate transaction connection — by this point
        # the txn has been committed or rolled back along the success/fail
        # paths above; we just need to release the connection.
        if truncate_conn is not None:
            try:
                truncate_conn.close()
            except Exception:
                pass
        # Done with the staged dir whether success or fail.
        shutil.rmtree(tmpdir, ignore_errors=True)
        _set_history_fields(history_id, tmpdir_path=None)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
#
# New router with a clean /api/admin prefix so these routes don't get
# double-prefixed by the parent `router` (whose prefix is /admin). Included
# in main.py BEFORE admin_api_router so it wins for duplicate paths.

upload_excel_router = APIRouter(prefix="/api/admin", tags=["Admin API"])


@upload_excel_router.post("/upload-excel")
async def upload_screening(
    background: BackgroundTasks,
    request: Request,
    file: UploadFile = File(...),
    load_mode: Literal["replace", "append"] = Form("replace"),
    authorization: Optional[str] = Header(None),
):
    """Accept an .xlsx OR .zip upload, stream to disk, kick off the pipeline.

    `load_mode` controls how the new data set replaces or merges with what's
    already in `bma_med.*`:
      * 'replace' (default): TRUNCATE every bma_med data table before
        export. Matches the operator's "ล้างกระดาน เริ่มใหม่" expectation.
      * 'append': run the existing UPSERT/merge path so incremental loads
        (e.g. monthly snapshots) build on top of prior data.

    Backward-compat: returns the legacy keys `districtsUpdated` and `errors`
    so older frontends keep working.
    """
    from auth import require_admin_session_or_bearer
    require_admin_session_or_bearer(request, authorization)
    fname = _safe_filename(file.filename or "upload")
    ext = Path(fname).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Only {sorted(ALLOWED_EXT)} supported, got {ext!r}",
        )

    # Stream to a tempfile with size guard + sha256 in one pass.
    tmp = tempfile.NamedTemporaryFile(
        prefix="bma-upload-", suffix=ext, delete=False,
    )
    sha = hashlib.sha256()
    total = 0
    LIMIT = MAX_UPLOAD_MB * 1024 * 1024
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > LIMIT:
                tmp.close()
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
                raise HTTPException(
                    status_code=413,
                    detail=f"File exceeds {MAX_UPLOAD_MB} MB",
                )
            sha.update(chunk)
            tmp.write(chunk)
    finally:
        try:
            tmp.close()
        except Exception:
            pass

    # Magic-byte sniff — both .xlsx and .zip start with PK\x03\x04.
    try:
        with open(tmp.name, "rb") as fh:
            head = fh.read(4)
    except OSError as exc:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise HTTPException(status_code=500,
                            detail=f"upload spool error: {_sanitize_error(exc)}")
    if head not in ALLOWED_MAGIC:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise HTTPException(
            status_code=400,
            detail="File header doesn't match an allowed type (xlsx/zip)",
        )

    if total == 0:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail="Empty upload")

    sha_hex = sha.hexdigest()
    kind = ext.lstrip(".")  # 'xlsx' | 'zip'
    try:
        history_id = _create_history_row(
            filename=fname, kind=kind, size_bytes=total,
            sha256=sha_hex, uploaded_path=tmp.name,
            load_mode=load_mode,
        )
    except Exception as exc:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        logger.exception("Failed to create history row for upload")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to queue upload: {_sanitize_error(exc)}",
        )

    _audit(history_id, "queue", filename=fname, kind=kind,
           size_bytes=total, sha256=sha_hex, load_mode=load_mode)
    background.add_task(
        _run_pipeline_upload, tmp.name, kind, history_id, load_mode,
    )

    return {
        "history_id": history_id,
        "status": "queued",
        "size_bytes": total,
        "sha256": sha_hex,
        # Backward-compat keys for legacy frontends.
        "districtsUpdated": [],
        "errors": [],
    }


class ConfirmRequest(BaseModel):
    history_id: int


@upload_excel_router.post("/upload-excel/confirm")
def confirm_upload(
    req: ConfirmRequest,
    background: BackgroundTasks,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Confirm a 'pending_confirm' upload and resume export.

    Operator must have reviewed the validate report (warnings) before calling.
    """
    from auth import require_admin_session_or_bearer
    require_admin_session_or_bearer(request, authorization)
    row = _fetch_history(req.history_id)
    if row.get("status") != "pending_confirm":
        raise HTTPException(
            status_code=409,
            detail=f"history {req.history_id} not in pending_confirm "
                   f"(current: {row.get('status')!r})",
        )
    tmpdir = row.get("tmpdir_path")
    kind = row.get("kind") or "zip"
    if not tmpdir or not os.path.isdir(tmpdir):
        # Tmpdir was cleaned (timeout or restart) — operator must re-upload.
        _set_history_error(
            req.history_id,
            "staged tmpdir is gone (timed out or server restarted) — re-upload required",
        )
        _audit(req.history_id, "confirm_too_late")
        raise HTTPException(
            status_code=410,
            detail="staged tmpdir missing — re-upload required",
        )

    # Resume with the load_mode the operator chose at upload time so the
    # confirm path matches the original intent. Default to 'replace' for
    # legacy rows that pre-date the load_mode column.
    resume_load_mode = row.get("load_mode") or "replace"
    if resume_load_mode not in ("replace", "append"):
        resume_load_mode = "replace"

    _set_history_fields(req.history_id, status="running")
    _audit(req.history_id, "confirm", load_mode=resume_load_mode)
    background.add_task(
        _resume_pipeline_export, tmpdir, kind, req.history_id,
        load_mode=resume_load_mode,
    )
    return {
        "history_id": req.history_id,
        "status": "confirmed",
    }


@upload_excel_router.post("/upload-excel/cancel")
def cancel_upload(
    req: ConfirmRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Cancel a 'pending_confirm' upload, wipe the staged tmpdir."""
    from auth import require_admin_session_or_bearer
    require_admin_session_or_bearer(request, authorization)
    row = _fetch_history(req.history_id)
    if row.get("status") != "pending_confirm":
        raise HTTPException(
            status_code=409,
            detail=f"history {req.history_id} not pending_confirm",
        )
    tmpdir = row.get("tmpdir_path")
    if tmpdir and os.path.isdir(tmpdir):
        shutil.rmtree(tmpdir, ignore_errors=True)
    _set_history_fields(
        req.history_id, status="cancelled",
        tmpdir_path=None, error_message="cancelled by operator",
        completed_at=datetime.now(),
    )
    _audit(req.history_id, "cancel")
    return {"history_id": req.history_id, "status": "cancelled"}


# --------------------------------------------------------------------------- #
# Step-label mapping — converts the raw progress_step strings written by the
# pipeline into Thai labels + a stable `step` slug the frontend renders. The
# pipeline writes labels like "ingest", "ingest — 4,250 / 200,000", "clean",
# "validate", "export", "refresh hot MVs", "flush caches", "stage files",
# "done". `_classify_step` normalises these into one of nine slugs.
# --------------------------------------------------------------------------- #

_STEP_SLUG_TO_TH = {
    "upload":     "อัปโหลดไฟล์",
    "ingest":     "นำเข้า CSV",
    "clean":      "ทำความสะอาดข้อมูล",
    "profile":    "วิเคราะห์ profile",
    "validate":   "ตรวจสอบความถูกต้อง",
    "export":     "บันทึกลงฐานข้อมูล",
    "mv_refresh": "อัปเดต Materialized Views",
    "done":       "เสร็จสมบูรณ์",
    "failed":     "ล้มเหลว",
}


def _classify_step(progress_step: Optional[str], status: str) -> str:
    """Normalise a free-form progress_step + status into a stable slug.

    Returns one of: upload | ingest | clean | profile | validate | export
                  | mv_refresh | done | failed
    """
    if status in ("error", "validation_failed", "cancelled"):
        return "failed"
    if status == "success":
        return "done"
    raw = (progress_step or "").lower()
    # Mid-stage labels carry a delimiter ("ingest — 1,000 / 200,000").
    head = raw.split("—", 1)[0].strip()
    if head.startswith("done"):
        return "done"
    if head.startswith("ingest"):
        return "ingest"
    if head.startswith("clean"):
        return "clean"
    if head.startswith("profile"):
        return "profile"
    if head.startswith("validate"):
        return "validate"
    if head.startswith("export"):
        return "export"
    if "refresh" in head or "mv" in head:
        return "mv_refresh"
    if "flush" in head:
        return "mv_refresh"
    if "stage" in head or "queue" in head or status == "queued":
        return "upload"
    # Fallback: still uploading / no progress written yet.
    return "upload"


@upload_excel_router.get("/upload-excel/status/{history_id}")
def upload_status(
    history_id: int,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Polling endpoint for the JSON upload-excel flow.

    Returns per-stage progress so the admin UI can render a real progress
    bar and stepper for the long-running ETL (ingest → clean → validate →
    export → MV refresh). Frontend polls this every ~1.5 s.
    """
    from auth import require_admin_session_or_bearer
    require_admin_session_or_bearer(request, authorization)
    row = _fetch_history(history_id)

    status = row.get("status") or "queued"
    progress_step_raw = row.get("progress_step")
    step_slug = _classify_step(progress_step_raw, status)
    pct_raw = row.get("progress_pct") or 0
    try:
        pct = max(0, min(100, int(pct_raw)))
    except (TypeError, ValueError):
        pct = 0
    if step_slug == "done":
        pct = 100

    started_at = row.get("started_at")
    completed_at = row.get("completed_at")

    return {
        "history_id":        history_id,
        "status":            status,
        "step":              step_slug,
        "step_label_th":     _STEP_SLUG_TO_TH.get(step_slug, step_slug),
        "progress_step_raw": progress_step_raw,
        "pct":               pct,
        "rows_processed":    int(row.get("rows_processed") or 0),
        "rows_total":        int(row.get("rows_total") or 0),
        "rows_inserted":     int(row.get("rows_imported") or 0),
        "started_at":        started_at.isoformat() if started_at else None,
        "finished_at":       completed_at.isoformat() if completed_at else None,
        "error":             row.get("error_message"),
        "validate_status":   row.get("validate_status"),
        "validate_report":   row.get("validate_report"),
        "view_status":       row.get("view_refresh_status"),
        "filename":          row.get("filename"),
        "load_mode":         row.get("load_mode"),
        "timestamp":         (completed_at or started_at).isoformat()
                             if (completed_at or started_at) else None,
        # Backward-compat keys for legacy frontends.
        "districtsUpdated":  [],
        "errors":            [row["error_message"]] if row.get("error_message") else [],
    }


# --------------------------------------------------------------------------- #
# Janitor — wipe pending_confirm tmpdirs older than 2h.
# --------------------------------------------------------------------------- #

PENDING_CONFIRM_TTL_SECONDS = 2 * 3600   # 2 hours
JANITOR_INTERVAL_SECONDS    = 30 * 60    # wake every 30 min

_janitor_started = False
_janitor_lock = threading.Lock()


def _janitor_pass() -> int:
    """One sweep: cancel pending_confirm rows older than the TTL.

    Returns the number of rows cancelled. Never raises.
    """
    cancelled = 0
    try:
        rows = execute_query(
            """
            SELECT id, tmpdir_path, started_at
            FROM import_history
            WHERE status = 'pending_confirm'
              AND started_at < NOW() - (%s || ' seconds')::INTERVAL
            """,
            (str(PENDING_CONFIRM_TTL_SECONDS),),
        ) or []
    except Exception:
        logger.exception("janitor query failed")
        return 0

    for row in rows:
        hid = row["id"]
        tmpdir = row.get("tmpdir_path")
        try:
            if tmpdir and os.path.isdir(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)
            _set_history_fields(
                hid, status="cancelled", tmpdir_path=None,
                error_message="auto-cancelled: pending_confirm TTL exceeded",
                completed_at=datetime.now(),
            )
            _audit(hid, "auto_cancel_ttl")
            cancelled += 1
        except Exception:
            logger.exception("janitor cleanup failed for history_id=%s", hid)
    return cancelled


def _janitor_loop() -> None:
    """Background daemon — sweep every JANITOR_INTERVAL_SECONDS forever."""
    while True:
        try:
            n = _janitor_pass()
            if n:
                logger.info("upload-excel janitor: cancelled %d stale pending_confirm rows", n)
        except Exception:
            logger.exception("janitor loop iteration failed")
        time.sleep(JANITOR_INTERVAL_SECONDS)


def start_upload_janitor() -> None:
    """Start the periodic cleanup thread (idempotent).

    Mounted from api/main.py:lifespan() at app startup.
    """
    global _janitor_started
    with _janitor_lock:
        if _janitor_started:
            return
        t = threading.Thread(
            target=_janitor_loop,
            daemon=True,
            name="upload-excel-janitor",
        )
        t.start()
        _janitor_started = True
        logger.info("upload-excel janitor started (interval=%ds, ttl=%ds)",
                    JANITOR_INTERVAL_SECONDS, PENDING_CONFIRM_TTL_SECONDS)
