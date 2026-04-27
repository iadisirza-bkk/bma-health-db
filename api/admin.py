"""
BMA Health DB -- Admin Panel Backend

Provides CSV upload, ETL import, dashboard, and import history routes.
All routes are mounted under /admin and require session authentication.
"""
from __future__ import annotations

from typing import Optional, List, Dict

import hmac
import importlib.util
import logging
import os
import secrets
import threading
import time
import uuid
from datetime import datetime
from io import StringIO

import pandas as pd
import psycopg2
from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from database import execute_query, execute_scalar, get_conn, get_writer_conn

# Admin endpoints write to private.* AND public.import_history — always use the
# writer pool (etl_user). Auth is enforced via _require_auth + CSRF before any
# DB call. We alias get_conn → get_writer_conn for the admin module so existing
# `with get_conn() as conn` blocks pick up the writer pool automatically.
get_conn = get_writer_conn
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
    """Lazy-load ETL module; reload automatically when import_csv.py changes.

    Uses file mtime to detect edits, so changes to etl/import_csv.py take
    effect on the next import without restarting the API server.
    """
    global _etl_mod, _etl_mtime
    etl_path = os.path.join(ETL_DIR, "import_csv.py")
    try:
        current_mtime = os.path.getmtime(etl_path)
    except OSError:
        current_mtime = None

    if _etl_mod is not None and current_mtime == _etl_mtime:
        return _etl_mod

    spec = importlib.util.spec_from_file_location("etl_import", etl_path)
    _etl_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_etl_mod)
    _etl_mtime = current_mtime
    if _etl_mtime is not None:
        logger.info("Loaded etl/import_csv.py (mtime=%s)", _etl_mtime)
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


def _delete_for_sources(cur, sources: List[str]) -> str:
    """Delete v3 private.* data for the given sources (preserves other sources).

    Cascades:
      - private.patient_alias → patient_id
      - private.patient (CASCADE deletes patient_address / chronic / family / allergy / attribute)
      - private.visit_event (CASCADE deletes visit_measurement / pain / etc.)
      - private.lab_event   (CASCADE deletes lab_measurement)

    Returns a short label for logging.
    """
    if not sources:
        return "noop"

    placeholders = ",".join(["%s"] * len(sources))
    params = tuple(sources)

    # Delete visit/lab events for these sources (cascades to measurements)
    cur.execute(f"DELETE FROM private.visit_event WHERE source_code IN ({placeholders})", params)
    cur.execute(f"DELETE FROM private.lab_event   WHERE source_code IN ({placeholders})", params)

    # Find patients EXCLUSIVELY in these sources (not in any other source)
    cur.execute(f"""
        WITH only_in_sources AS (
          SELECT pa.patient_id
          FROM private.patient_alias pa
          GROUP BY pa.patient_id
          HAVING bool_and(pa.source_code IN ({placeholders}))
        )
        DELETE FROM private.patient_alias
        WHERE source_code IN ({placeholders})
    """, params + params)

    # Delete patients now alias-orphaned (no remaining alias)
    cur.execute("""
        DELETE FROM private.patient
        WHERE id NOT IN (SELECT patient_id FROM private.patient_alias)
    """)

    return "delete " + ",".join(sources)

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

FILE_TYPE_MAP = {
    "pt": {"table": "private.patient", "csv": "pt.csv", "importer": "patients"},
    "pthistory": {"table": "private.visit_event", "csv": "pthistory.csv", "importer": "visits"},
    "vitalsignslf": {
        "table": "private.visit_event + visit_measurement",
        "csv": "vitalsignslf.csv",
        "importer": "vital",
    },
    "homevisit": {
        "table": "private.patient_address + visit_measurement",
        "csv": "homevisit.csv",
        "importer": "homevisit",
    },
    "homehealth": {
        "table": "private.visit_measurement",
        "csv": "homehealth.csv",
        "importer": "homehealth",
    },
    "labhealth": {
        "table": "private.lab_event + lab_measurement",
        "csv": "labhealth.csv",
        "importer": "lab",
    },
    "labhealthext": {
        "table": "private.lab_event + lab_measurement",
        "csv": "labhealthext.csv",
        "importer": "lab_ext",
    },
    "app2": {
        "table": "private.* (auto-split)",
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
    """
    if not source_code or source_code not in ("portal", "app1", "app2"):
        return {"matched": 0, "unmatched": 0, "address": 0, "total": len(df_columns)}

    upper_cols = {c.upper() for c in df_columns}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT csv_column_name, domain
                    FROM private.variable_definition
                    WHERE source_code = %s AND deprecated_at IS NULL
                """, (source_code,))
                known = {row[0].upper(): row[1] for row in cur.fetchall()}
    except Exception:
        return {"matched": 0, "unmatched": len(df_columns), "address": 0,
                "total": len(df_columns)}

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
    """Sanitize error message to avoid leaking internal details."""
    import re
    msg = str(exc)
    # Remove connection strings
    msg = re.sub(r'postgresql://[^\s"\']+', 'postgresql://***', msg)
    # Remove file paths
    msg = re.sub(r'/[^\s"\']*\.py', '<file>', msg)
    # Truncate
    if len(msg) > 500:
        msg = msg[:500] + "..."
    return f"{type(exc).__name__}: {msg}"


def _run_import(upload_id: str, history_id: int):
    """Execute the ETL import in a background thread.

    v3 (2026-04-27): writes to private.* schema via etl.import_csv_v3
    (EAV pattern). Public.mv_* refreshed at end so dashboard sees new data.
    """
    data = _upload_cache.pop(upload_id, None)
    if not data:
        _update_history(history_id, "error", 0, 0, "Upload data expired or missing", 0.0)
        return

    start = time.time()
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL_WRITER)
        conn.autocommit = False
        cur = conn.cursor()

        # Refuse to start if another import is already running on this DB.
        if not _try_acquire_import_lock(cur):
            _update_history(
                history_id, "error", 0, 0,
                "Another import is currently running. Try again when it finishes.",
                time.time() - start,
            )
            logger.warning("Import refused: another import holds the lock")
            return

        file_type = data["file_type"]
        source_code = data.get("source_code") or "portal"
        df = data["df"]

        # ─── ETL v3 dispatch — writes to private.* (EAV) ────────────────
        # Use _load_etl_v3() (file-path import) — same pattern as _load_etl()
        # because uvicorn runs from api/ and etl/ has no __init__.py
        etlv3 = _load_etl_v3()

        # Create import_batch row for audit
        cur.execute("""
            INSERT INTO private.import_batch
              (source_code, filename, csv_file_type, uploaded_at, status, progress_pct)
            VALUES (%s, %s, %s, NOW(), 'running', 0)
            RETURNING id
        """, (source_code, data["filename"], file_type))
        batch_id = cur.fetchone()[0]
        conn.commit()

        _update_progress(history_id, f"v3 import {source_code}/{file_type}", 20)

        if file_type == "pt":
            # Patient master + alias
            pid_map = etlv3.import_patients(cur, df, source_code, batch_id)
            rows_imported = len(pid_map)
        elif file_type == "app2":
            # Combined CSV — auto-splits patient + visit
            n_pat, n_vis = etlv3.import_app2(cur, df, batch_id)
            rows_imported = n_vis
        elif file_type in ("vitalsignslf", "homevisit", "homehealth"):
            # Need patient_map first — fetch from existing
            cur.execute("""
                SELECT p.idcard_hash, p.id
                FROM private.patient p
                JOIN private.patient_alias pa ON pa.patient_id = p.id
                WHERE pa.source_code = %s
            """, (source_code,))
            pid_map = {h: pid for h, pid in cur.fetchall()}
            if not pid_map:
                raise ValueError(
                    f"No patients found for source={source_code}. "
                    "Upload pt.csv first.",
                )
            rows_imported = etlv3.import_visits_and_measurements(
                cur, df, source_code, file_type, pid_map, batch_id,
            )
        elif file_type in ("labhealth", "labhealthext"):
            cur.execute("""
                SELECT p.idcard_hash, p.id FROM private.patient p
                JOIN private.patient_alias pa ON pa.patient_id = p.id
                WHERE pa.source_code = %s
            """, (source_code,))
            pid_map = {h: pid for h, pid in cur.fetchall()}
            rows_imported = etlv3.import_lab(cur, df, source_code, pid_map, batch_id)
        elif file_type == "pthistory":
            cur.execute("""
                SELECT p.idcard_hash, p.id FROM private.patient p
                JOIN private.patient_alias pa ON pa.patient_id = p.id
                WHERE pa.source_code = %s
            """, (source_code,))
            pid_map = {h: pid for h, pid in cur.fetchall()}
            rows_imported = etlv3.import_visits_and_measurements(
                cur, df, source_code, file_type, pid_map, batch_id,
            )
        else:
            raise ValueError(f"Unknown file type: {file_type}")

        # Update import_batch
        cur.execute("""
            UPDATE private.import_batch
            SET status = 'completed', rows_inserted = %s, rows_parsed = %s,
                duration_ms = %s, progress_pct = 90
            WHERE id = %s
        """, (rows_imported, len(df), int((time.time() - start) * 1000), batch_id))

        # Commit raw data FIRST — safe even if MV refresh fails
        _update_progress(history_id, "commit", 85)
        conn.commit()

        # Refresh public.mv_* (k-anonymized aggregates) — non-fatal
        _update_progress(history_id, "refresh public MVs", 92)
        view_status: str
        view_err: Optional[str] = None
        try:
            cur.execute("SELECT view_name, status FROM public.refresh_all_mvs()")
            results = cur.fetchall()
            failed = [r[0] for r in results if r[1] != 'ok']
            if failed:
                view_status = "partial"
                view_err = f"failed: {', '.join(failed)}"
            else:
                view_status = "success"
            conn.commit()
        except Exception as exc:
            conn.rollback()
            view_status = "failed"
            view_err = _sanitize_error(exc)
            logger.error("MV refresh failed after import: %s", view_err)

        # Flush Redis cache + in-memory data_adapter cache
        _update_progress(history_id, "flush caches", 99)
        try:
            from cache import cache_flush_all
            from services.data_adapter import invalidate_cache as invalidate_data_cache
            cache_flush_all()
            invalidate_data_cache()
        except Exception:
            logger.warning("Cache flush after import failed (non-fatal)")

        duration = time.time() - start
        _update_progress(history_id, "done", 100)
        _update_history(
            history_id, "success", rows_imported, 0, None, duration,
            view_refresh_status=view_status,
            view_refresh_error=view_err,
        )
        logger.info(
            "Import complete: file_type=%s rows=%d duration=%.2fs view_refresh=%s",
            file_type, rows_imported, duration, view_status,
        )

    except Exception as exc:
        if conn:
            conn.rollback()
        duration = time.time() - start
        error_msg = _sanitize_error(exc)
        _update_history(history_id, "error", 0, 0, error_msg, duration)
        logger.exception("Import failed for upload_id=%s", upload_id)
    finally:
        if conn:
            conn.close()

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
    """
    _require_auth(request)
    source = _normalize_source(source)

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

        # For each raw table compute 2 counts:
        #   n_records = "ครั้ง" (visits) — defined per spec:
        #     - raw_patients: COUNT(*) (each row = 1 registration / 1 person-source)
        #     - child tables: COUNT(DISTINCT (patient_id, visit_date::date))
        #         → matches the project spec "vital.PID + VSTDATE (count)"
        #         → drops within-day duplicates (e.g. App1 has ~2% same-day dups)
        #   n_people  = "คน" — unique persons
        #     - raw_patients: COUNT(DISTINCT idcard_hash)
        #     - child tables: COUNT(DISTINCT patient_id)
        #
        # `records_expr` is interpolated as raw SQL — the values are static
        # (no user input), so it's safe.
        _table_specs = [
            ("raw_patients",     "patients",     "COUNT(*)",                                                    "COUNT(DISTINCT idcard_hash)"),
            ("raw_vitalsigns",   "vitalsigns",   "COUNT(DISTINCT (patient_id, visit_date::date))",              "COUNT(DISTINCT patient_id)"),
            ("raw_visits",       "visits",       "COUNT(DISTINCT (patient_id, visit_date::date))",              "COUNT(DISTINCT patient_id)"),
            ("raw_lab_results",  "lab",          "COUNT(DISTINCT (patient_id, visit_date::date))",              "COUNT(DISTINCT patient_id)"),
            ("raw_homevisit",    "homevisit",    "COUNT(DISTINCT (patient_id, visit_date::date))",              "COUNT(DISTINCT patient_id)"),
            ("raw_homehealth",   "homehealth",   "COUNT(DISTINCT (patient_id, visit_date::date))",              "COUNT(DISTINCT patient_id)"),
            ("raw_lab_extended", "lab_extended", "COUNT(DISTINCT (patient_id, visit_date::date))",              "COUNT(DISTINCT patient_id)"),
        ]
        raw_tables = []
        for tbl, key, records_expr, people_expr in _table_specs:
            sql = (
                f'SELECT {records_expr} AS n_records, '
                f'{people_expr} AS n_people '
                f'FROM "{tbl}" {where_clause}'
            )
            rows = execute_query(sql, params) or [{"n_records": 0, "n_people": 0}]
            row = rows[0]
            n_rec = int(row.get("n_records") or 0)
            n_ppl = int(row.get("n_people") or 0)
            table_counts[key] = n_rec
            raw_tables.append({
                "name": tbl,
                "count": n_rec,
                "n_records": n_rec,
                "n_people": n_ppl,
            })
        people_counts = {key: rt["n_people"] for rt, (_, key, _, _) in
                         zip(raw_tables, _table_specs)}

        # Per-source breakdown on top of the page (always computed, source-independent)
        try:
            source_breakdown = execute_query(
                "SELECT data_source, COUNT(*) AS n "
                "FROM raw_patients GROUP BY data_source ORDER BY data_source"
            ) or []
        except Exception:
            source_breakdown = []

        # Coverage stats vs project target (e.g. 1,000,000 people)
        # n_per_source[X]      = unique people in source X
        # n_unique_all_sources = unique idcard_hash across ALL 3 sources (people who
        #                        have been screened anywhere, deduped cross-source)
        try:
            cov_rows = execute_query(
                "SELECT data_source, COUNT(DISTINCT idcard_hash) AS n "
                "FROM raw_patients GROUP BY data_source"
            ) or []
            n_per_source = {r["data_source"]: int(r["n"]) for r in cov_rows}
            n_unique_all = int(execute_scalar(
                "SELECT COUNT(DISTINCT idcard_hash) FROM raw_patients"
            ) or 0)
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
    response = templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "table_counts": table_counts,
            "people_counts": people_counts if db_available else {},
            "raw_tables": raw_tables,
            "view_info": view_info,
            "messages": messages,
            "csrf_token": csrf_token,
            "source": source,
            "source_values": _SOURCE_VALUES,
            "source_breakdown": source_breakdown,
            "coverage_stats": coverage_stats,
        },
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

    NEW (v3 schema): `source_code` is required so ETL knows which
    `private.variable_definition` rows to use for column mapping.
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
        etl = _load_etl()
        with get_conn() as conn:
            cur = conn.cursor()
            etl.refresh_all_summaries(cur)
            conn.commit()

        # Flush Redis cache after view refresh
        try:
            from cache import cache_flush_all
            cache_flush_all()
        except Exception:
            pass

        response = RedirectResponse(url="/admin/dashboard", status_code=303)
        _set_flash(response, "success", "Materialized views refreshed successfully. Cache flushed.")
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
    try:
        # Query data quality directly (same logic as the API endpoint)
        tables = ["raw_patients", "raw_visits", "raw_vitalsigns", "raw_homevisit",
                  "raw_homehealth", "raw_lab_results", "raw_lab_extended"]
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
                "messages": _get_flash(request),
            },
        )

    source_a, source_b = normalize_pair(pair)

    # Fast path — skip heavy compute if either source has 0 rows
    present_q = execute_query(
        "SELECT data_source, COUNT(*) AS n FROM raw_patients GROUP BY data_source"
    ) or []
    present = {r["data_source"]: r["n"] for r in present_q if r["n"] > 0}
    sources_present = set(present.keys())

    report = None
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
    # Source totals — for the denominator
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
    field_stats: Dict[str, Dict] = {}   # key = (table, column) → {portal: {non_null,pct}, app1: ..., app2: ...}
    try:
        from database import get_conn
        import psycopg2.extras
        for tbl, entries in by_table.items():
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

    # Which sources actually have data (to show "no data yet" notices)
    presence = execute_query(
        "SELECT data_source, COUNT(*) AS n FROM raw_patients GROUP BY data_source"
    ) or []
    sources_present = {r["data_source"] for r in presence if r["n"] > 0}

    coverage: List[Dict] = []
    distribution: Dict = {}
    quality: Dict = {}

    try:
        coverage     = _coverage_matrix()
        distribution = _distribution_comparison()
        quality      = _data_quality_per_source()
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

            # 3. Verify — count any rows that still reference this patient
            cur.execute(
                "SELECT id FROM raw_patients WHERE idcard_hash = %s",
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


def _run_bundle_import(manifest: List[Dict], history_id: int):
    """Import all CSV files in the manifest in correct order.

    manifest entries: {source, file_type, tmp_path, filename, size_bytes}
    Files are read from disk (never all-in-memory) and processed one-at-a-time.
    Each tempfile is unlinked after its file is imported.
    """
    start = time.time()
    conn = None
    total_imported = 0
    steps_done = []
    tmp_paths = [m["tmp_path"] for m in manifest]

    try:
        conn = psycopg2.connect(DATABASE_URL_WRITER)
        conn.autocommit = False
        cur = conn.cursor()
        etlv3 = _load_etl_v3()   # v3 ETL — writes to private.*

        if not _try_acquire_import_lock(cur):
            _update_history(
                history_id, "error", 0, 0,
                "Another import is currently running. Try again when it finishes.",
                time.time() - start,
            )
            logger.warning("Bundle import refused: another import holds the lock")
            return

        # Group manifest by source for easy lookup. We discover which sources
        # are present FIRST so we can scope the delete to just those.
        by_source: Dict[str, Dict[str, Dict]] = {"portal": {}, "app1": {}, "app2": {}}
        for m in manifest:
            by_source[m["source"]][m["file_type"]] = m
        sources_in_bundle = [s for s, files in by_source.items() if files]

        # One transaction across all files — rollback on any failure.
        # Per-source delete preserves data from sources NOT in this bundle.
        _update_progress(history_id, "delete prior data", 1)
        delete_label = _delete_for_sources(cur, sources_in_bundle)
        logger.info("Bundle import: %s", delete_label)

        # Estimate total work
        total_files = sum(len(v) for v in by_source.values())
        files_done = 0

        import pandas as _pd

        def _pct_bounds(file_idx: int) -> tuple[int, int]:
            span = 85 / max(total_files, 1)
            s = 5 + int(span * file_idx)
            e = 5 + int(span * (file_idx + 1))
            return s, e

        def _new_batch(source_code: str, label: str) -> int:
            """Create a per-source import_batch row inside the bundle.
            Returns the batch_id which is then passed to ETL functions."""
            cur.execute("""
                INSERT INTO private.import_batch
                  (source_code, filename, csv_file_type, status)
                VALUES (%s, %s, %s, 'running')
                RETURNING id
            """, (source_code, f"bundle-{history_id}/{label}", label))
            return cur.fetchone()[0]

        for source in SOURCE_IMPORT_ORDER:
            files = by_source.get(source, {})
            if not files:
                continue
            logger.info("Processing source: %s (%d files)", source, len(files))

            # ─── App2: combined CSV — split internally ──────────────────
            if source == "app2":
                m = files.get("app2")
                if m:
                    batch_id = _new_batch("app2", "app2")
                    s, _e = _pct_bounds(files_done)
                    _update_progress(history_id, f"{source}/app2", s)
                    df = _pd.read_csv(m["tmp_path"], dtype=str, low_memory=False,
                                      keep_default_na=True)
                    n_pat, n_vis = etlv3.import_app2(cur, df, batch_id)
                    total_imported += n_vis
                    steps_done.append(f"{m['filename']}(app2:{n_pat}p/{n_vis}v)")
                    cur.execute("""UPDATE private.import_batch
                        SET status='completed', rows_inserted=%s WHERE id=%s""",
                        (n_vis, batch_id))
                    del df
                    files_done += 1
                continue

            # ─── Portal / App1: pt.csv first to populate patient table ──
            m_pt = files.get("pt")
            if m_pt:
                batch_id = _new_batch(source, "pt")
                s, _e = _pct_bounds(files_done)
                _update_progress(history_id, f"{source}/pt", s)
                df = _pd.read_csv(m_pt["tmp_path"], dtype=str, low_memory=False,
                                  keep_default_na=True)
                pid_map_pt = etlv3.import_patients(cur, df, source, batch_id)
                total_imported += len(pid_map_pt)
                steps_done.append(f"{m_pt['filename']}({source}/pt:{len(pid_map_pt)})")
                cur.execute("""UPDATE private.import_batch
                    SET status='completed', rows_inserted=%s WHERE id=%s""",
                    (len(pid_map_pt), batch_id))
                del df
                files_done += 1

            # Build pid_map from patient_alias (resolve idcard_hash → patient_id)
            cur.execute("""
                SELECT p.idcard_hash, p.id FROM private.patient p
                JOIN private.patient_alias pa ON pa.patient_id = p.id
                WHERE pa.source_code = %s
            """, (source,))
            pid_map = {h: pid for h, pid in cur.fetchall()}

            # ─── Child files: vital, hv, hh, lab, labext, pthistory ─────
            for ft in ("vitalsignslf", "homevisit", "homehealth",
                       "pthistory", "labhealth", "labhealthext"):
                m = files.get(ft)
                if not m:
                    continue
                ft_batch_id = _new_batch(source, ft)
                s, _e = _pct_bounds(files_done)
                _update_progress(history_id, f"{source}/{ft}", s)
                df = _pd.read_csv(m["tmp_path"], dtype=str, low_memory=False,
                                  keep_default_na=True)

                if ft in ("labhealth", "labhealthext"):
                    n = etlv3.import_lab(cur, df, source, pid_map, ft_batch_id)
                else:
                    n = etlv3.import_visits_and_measurements(
                        cur, df, source, ft, pid_map, ft_batch_id,
                    )
                total_imported += n
                steps_done.append(f"{m['filename']}({source}/{ft}:{n})")
                cur.execute("""UPDATE private.import_batch
                    SET status='completed', rows_inserted=%s WHERE id=%s""",
                    (n, ft_batch_id))
                del df
                files_done += 1

        _update_progress(history_id, "commit", 92)
        conn.commit()
        logger.info("Bundle import: data committed (%d rows total)", total_imported)

        # Refresh public.mv_* (k-anonymized aggregates) — non-fatal
        _update_progress(history_id, "refresh public MVs", 95)
        view_status = "skipped"
        view_err = None
        try:
            cur.execute("SELECT view_name, status FROM public.refresh_all_mvs()")
            results = cur.fetchall()
            failed = [r[0] for r in results if r[1] != 'ok']
            view_status = "partial" if failed else "success"
            if failed:
                view_err = f"failed: {', '.join(failed)}"
            conn.commit()
        except Exception as exc:
            conn.rollback()
            view_status = "failed"
            view_err = _sanitize_error(exc)
            logger.error("MV refresh failed after bundle import: %s", view_err)

        # Flush caches
        _update_progress(history_id, "flush caches", 99)
        try:
            from cache import cache_flush_all
            from services.data_adapter import invalidate_cache as invalidate_data_cache
            cache_flush_all()
            invalidate_data_cache()
        except Exception:
            logger.warning("Cache flush after bundle import failed (non-fatal)")

        duration = time.time() - start
        _update_progress(history_id, "done", 100)
        _update_history(
            history_id, "success", total_imported, 0, None, duration,
            view_refresh_status=view_status,
            view_refresh_error=view_err,
        )
        logger.info(
            "Bundle import complete: %d files, %d rows, %.2fs — %s",
            len(steps_done), total_imported, duration, ", ".join(steps_done),
        )

    except Exception as exc:
        if conn:
            conn.rollback()
        duration = time.time() - start
        error_msg = _sanitize_error(exc)
        _update_history(history_id, "error", total_imported, 0, error_msg, duration)
        logger.exception("Bundle import failed")
    finally:
        if conn:
            conn.close()
        # Clean up tempfiles regardless of outcome
        for p in tmp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass


@router.get("/upload-bundle", response_class=HTMLResponse)
async def upload_bundle_page(request: Request):
    """Render the bundle upload form (multiple CSV files at once)."""
    _require_auth(request)
    csrf_token = _generate_csrf_token(request)
    response = templates.TemplateResponse(
        "admin/upload_bundle.html",
        {
            "request": request,
            "file_types": FILE_TYPE_MAP,
            "messages": _get_flash(request),
            "csrf_token": csrf_token,
        },
    )
    response.set_cookie("csrf_token", csrf_token, httponly=True, samesite="strict", max_age=86400)
    return response


@router.post("/upload-bundle", response_class=HTMLResponse)
async def upload_bundle_submit(request: Request):
    """Accept up to 13 CSV files across 3 source folders (portal/, app1/, app2/).

    Files are streamed to tempfiles on disk (no full-in-memory buffering).
    Source is detected from either the `relpaths` form field (folder upload) or
    a fallback of filename + user-supplied source selector.
    """
    _require_auth(request)

    form = await request.form()
    csrf_token_val = form.get("csrf_token", "")
    if not _validate_csrf(request, csrf_token_val):
        raise HTTPException(status_code=403, detail="CSRF validation failed")

    files = form.getlist("files")
    relpaths = form.getlist("relpaths")   # parallel to files; may be empty
    default_source = form.get("default_source", "").strip().lower() or None

    if not files:
        response = RedirectResponse(url="/admin/upload-bundle", status_code=303)
        _set_flash(response, "error", "No files uploaded.")
        return response

    import shutil, tempfile

    manifest: List[Dict] = []
    errors: List[str] = []

    for idx, upload_file in enumerate(files):
        if not hasattr(upload_file, "filename") or not upload_file.filename:
            continue
        fname = upload_file.filename
        if not fname.lower().endswith(".csv"):
            errors.append(f"{fname}: ไม่ใช่ไฟล์ .csv")
            continue

        relpath = relpaths[idx] if idx < len(relpaths) else fname

        # Stream upload to a tempfile on disk (memory-safe even for 500MB+ files)
        tmp = tempfile.NamedTemporaryFile(
            suffix=".csv", prefix="bma_upload_", delete=False
        )
        bytes_written = 0
        try:
            # copyfileobj streams in 64 KB chunks — SpooledTemporaryFile is
            # already disk-backed if the file was >1 MB, so this is a
            # disk-to-disk copy. Memory stays flat.
            upload_file.file.seek(0)
            while True:
                chunk = upload_file.file.read(65536)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > MAX_FILE_BYTES:
                    tmp.close()
                    os.unlink(tmp.name)
                    errors.append(f"{fname}: ไฟล์ใหญ่เกิน {MAX_FILE_BYTES // (1024*1024)} MB")
                    break
                tmp.write(chunk)
            else:
                pass
        finally:
            tmp.close()

        if bytes_written > MAX_FILE_BYTES:
            continue
        if bytes_written == 0:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            errors.append(f"{fname}: ไฟล์ว่าง")
            continue

        # Determine (source, file_type) from relpath → filename → default
        source = _detect_source_from_path(relpath) or default_source
        file_type = _detect_file_type_from_name(fname)

        if file_type == "app2":
            source = "app2"     # app2.csv is uniquely scoped
        if not source:
            os.unlink(tmp.name)
            errors.append(
                f"{fname}: ตรวจจับ source (portal/app1/app2) ไม่ได้ — "
                f"โปรดอัปโหลดเป็นโฟลเดอร์หรือเลือก default source"
            )
            continue
        if not file_type:
            os.unlink(tmp.name)
            errors.append(f"{fname}: ตรวจจับประเภทไฟล์ไม่ได้")
            continue

        # Check duplicates (same source+file_type twice)
        dup = next(
            (m for m in manifest
             if m["source"] == source and m["file_type"] == file_type),
            None,
        )
        if dup:
            os.unlink(tmp.name)
            errors.append(
                f"{fname}: ซ้ำกับ {dup['filename']} — ทั้งคู่เป็น {source}/{file_type}"
            )
            continue

        manifest.append({
            "source": source,
            "file_type": file_type,
            "tmp_path": tmp.name,
            "filename": fname,
            "size_bytes": bytes_written,
        })

    if errors:
        # Clean up any tempfiles that were already created
        for m in manifest:
            try:
                os.unlink(m["tmp_path"])
            except OSError:
                pass
        response = RedirectResponse(url="/admin/upload-bundle", status_code=303)
        _set_flash(response, "error", " | ".join(errors))
        return response

    if not manifest:
        response = RedirectResponse(url="/admin/upload-bundle", status_code=303)
        _set_flash(response, "error", "ไม่พบไฟล์ CSV ที่ตรวจจับประเภทได้")
        return response

    # Build summary for flash message
    file_list = ", ".join(
        f"{m['filename']}({m['source']}/{m['file_type']})"
        for m in sorted(manifest, key=lambda x: (x["source"], x["file_type"]))
    )
    total_bytes = sum(m["size_bytes"] for m in manifest)

    try:
        with get_conn() as conn_hist:
            with conn_hist.cursor() as cur_hist:
                cur_hist.execute(
                    """
                    INSERT INTO import_history
                        (filename, table_name, file_type, status, started_at)
                    VALUES (%s, %s, %s, 'running', NOW())
                    RETURNING id
                    """,
                    (f"[Bundle] {len(manifest)} files", "ALL", "bundle"),
                )
                history_id = cur_hist.fetchone()[0]
            conn_hist.commit()
    except Exception as exc:
        logger.exception("Failed to create bundle import_history")
        # Clean up tempfiles since we can't proceed
        for m in manifest:
            try:
                os.unlink(m["tmp_path"])
            except OSError:
                pass
        response = RedirectResponse(url="/admin/upload-bundle", status_code=303)
        _set_flash(response, "error", f"Failed to start import: {_sanitize_error(exc)}")
        return response

    # Launch background thread — ownership of tempfiles transfers to the worker,
    # which unlinks them in its finally block.
    thread = threading.Thread(
        target=_run_bundle_import,
        args=(manifest, history_id),
        daemon=True,
        name=f"bundle-import-{history_id}",
    )
    thread.start()

    size_mb = total_bytes / (1024 * 1024)
    response = RedirectResponse(url="/admin/history", status_code=303)
    _set_flash(
        response, "success",
        f"Bundle import started (job #{history_id}): {len(manifest)} files, "
        f"{size_mb:.1f} MB — {file_list}",
    )
    return response
