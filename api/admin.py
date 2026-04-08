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

from database import execute_query, execute_scalar, get_conn
from config import DATABASE_URL

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

logger = logging.getLogger("admin")

# --------------------------------------------------------------------------- #
# ETL imports (loaded from file path to avoid config module name collision)
# --------------------------------------------------------------------------- #

ETL_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "etl")

_etl_mod = None


def _load_etl():
    """Lazy-load ETL module to avoid import-time failures when DB is unavailable."""
    global _etl_mod
    if _etl_mod is not None:
        return _etl_mod
    spec = importlib.util.spec_from_file_location(
        "etl_import", os.path.join(ETL_DIR, "import_csv.py")
    )
    _etl_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_etl_mod)
    return _etl_mod

CURRENT_YEAR = int(os.getenv("CURRENT_YEAR", str(datetime.now().year)))

# --------------------------------------------------------------------------- #
# Authentication helpers
# --------------------------------------------------------------------------- #

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")

# Server-side session store
_active_sessions: Dict[str, float] = {}  # token -> created_timestamp
_SESSION_MAX_AGE = 86400  # 24 hours

def _create_session() -> str:
    """Create a new random session token."""
    token = secrets.token_hex(32)
    _active_sessions[token] = time.time()
    # Clean expired sessions
    cutoff = time.time() - _SESSION_MAX_AGE
    expired = [k for k, v in _active_sessions.items() if v < cutoff]
    for k in expired:
        _active_sessions.pop(k, None)
    return token

def _check_auth(request: Request) -> bool:
    """Return True if the request carries a valid session cookie."""
    token = request.cookies.get("admin_session")
    if not token or token not in _active_sessions:
        return False
    created = _active_sessions[token]
    if time.time() - created > _SESSION_MAX_AGE:
        _active_sessions.pop(token, None)
        return False
    return True

def _revoke_session(token: str):
    """Revoke a session token."""
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
    "pt": {"table": "raw_patients", "csv": "pt.csv", "importer": "patients"},
    "pthistory": {"table": "raw_visits", "csv": "pthistory.csv", "importer": "visits"},
    "vitalsignslf": {
        "table": "raw_vitalsigns",
        "csv": "vitalsignslf.csv",
        "importer": "vitalsigns",
    },
    "homevisit": {
        "table": "raw_homevisit",
        "csv": "homevisit.csv",
        "importer": "homevisit",
    },
    "homehealth": {
        "table": "raw_homehealth",
        "csv": "homehealth.csv",
        "importer": "homehealth",
    },
    "labhealth": {
        "table": "raw_lab_results",
        "csv": "labhealth.csv",
        "importer": "lab_results",
    },
    "labhealthext": {
        "table": "raw_lab_extended",
        "csv": "labhealthext.csv",
        "importer": "lab_extended",
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
    """Auto-detect CSV file type from column headers."""
    cols = {c.upper() for c in columns}
    if "IDCARD" in cols:
        return "pt"
    if "RLGN" in cols or "LGBTQ" in cols:
        return "pthistory"
    if "HBPN" in cols or "RISKDM" in cols:
        return "vitalsignslf"
    if "SELFOUR" in cols or "DISTYPE1" in cols:
        return "homevisit"
    if "EXCERCISE" in cols or "CGTDS" in cols:
        return "homehealth"
    if "CBCRS" in cols or "HMGB" in cols:
        return "labhealth"
    if "SCRRES01" in cols or "PTGRIGHT" in cols:
        return "labhealthext"
    return None

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
):
    """Update an import_history record. Uses a fresh connection (thread-safe)."""
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
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
                    completed_at = NOW()
                WHERE id = %s
                """,
                (status, rows_imported, rows_skipped, error_message, round(duration, 2), history_id),
            )
    except Exception:
        logger.exception("Failed to update import_history id=%s", history_id)
    finally:
        if conn:
            conn.close()


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
    """Execute the ETL import in a background thread."""
    data = _upload_cache.pop(upload_id, None)
    if not data:
        _update_history(history_id, "error", 0, 0, "Upload data expired or missing", 0.0)
        return

    start = time.time()
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        cur = conn.cursor()

        file_type = data["file_type"]
        df = data["df"]

        etl = _load_etl()

        if file_type == "pt":
            patient_map = etl.import_patients(cur, df, CURRENT_YEAR)
            rows_imported = len(patient_map)
        else:
            # Build patient_map from existing raw_patients
            cur.execute("SELECT idcard_hash, id FROM raw_patients")
            patient_map = {row[0]: row[1] for row in cur.fetchall()}

            importers = {
                "pthistory": etl.import_visits,
                "vitalsignslf": etl.import_vitalsigns,
                "homevisit": etl.import_homevisit,
                "homehealth": etl.import_homehealth,
                "labhealth": etl.import_lab_results,
                "labhealthext": etl.import_lab_extended,
            }
            importer_fn = importers.get(file_type)
            if importer_fn is None:
                raise ValueError(f"Unknown file type: {file_type}")

            importer_fn(cur, df, patient_map)
            rows_imported = len(df)

        # Refresh materialized views after import
        etl.refresh_all_summaries(cur)
        conn.commit()

        duration = time.time() - start
        _update_history(history_id, "success", rows_imported, 0, None, duration)
        logger.info(
            "Import complete: file_type=%s rows=%d duration=%.2fs",
            file_type, rows_imported, duration,
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
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": "Invalid request. Please try again.", "csrf_token": ""},
            status_code=403,
        )

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

@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main admin dashboard with table counts and view info."""
    _require_auth(request)

    db_available = True
    raw_tables = []
    table_counts = {"patients": 0, "vitalsigns": 0, "lab": 0, "visits": 0}
    view_info = []

    try:
        # Raw table row counts
        raw_tables = execute_query("""
            SELECT relname AS name, n_live_tup AS count
            FROM pg_stat_user_tables
            WHERE schemaname = 'public' AND relname LIKE 'raw_%%'
            ORDER BY relname
        """)

        # Summary card counts
        for t in raw_tables:
            name = t["name"]
            if "patient" in name:
                table_counts["patients"] = t["count"]
            elif "vitalsign" in name:
                table_counts["vitalsigns"] = t["count"]
            elif "lab_result" in name:
                table_counts["lab"] = t["count"]
            elif "visit" in name:
                table_counts["visits"] = t["count"]

        # Materialized view info
        mat_views = execute_query("""
            SELECT matviewname AS name
            FROM pg_matviews
            WHERE schemaname = 'public'
            ORDER BY matviewname
        """)

        for mv in mat_views:
            view_name = mv["name"]
            row_count = execute_scalar(f'SELECT COUNT(*) FROM "{view_name}"') or 0
            view_info.append({
                "name": view_name,
                "row_count": row_count,
                "refreshed_at": "-",
            })
    except Exception:
        db_available = False
        logger.warning("Database not available — showing empty dashboard")

    messages = _get_flash(request)
    if not db_available:
        messages = messages or []
        messages.append({"type": "error", "text": "Database is not connected. Start PostgreSQL to see data."})

    csrf_token = _generate_csrf_token(request)
    response = templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "table_counts": table_counts,
            "raw_tables": raw_tables,
            "view_info": view_info,
            "messages": messages,
            "csrf_token": csrf_token,
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
    csrf_token: str = Form(""),
):
    """Handle CSV file upload: parse, detect type, show preview."""
    _require_auth(request)

    # Validate CSRF token
    if not _validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF validation failed")

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
        # Enforce max file size (50 MB)
        MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
        raw_bytes = await file.read()
        if len(raw_bytes) > MAX_UPLOAD_SIZE:
            return templates.TemplateResponse(
                "admin/upload.html",
                {
                    "request": request,
                    "file_types": FILE_TYPE_MAP,
                    "preview": None,
                    "messages": [{"type": "error", "text": f"File too large. Maximum size is 50 MB."}],
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
        "df": df,
        "created_at": time.time(),
    }

    file_info = FILE_TYPE_MAP[detected_type]

    # Strip PII columns from preview
    safe_columns = [c for c in df.columns if c.upper() not in _PREVIEW_PII_COLUMNS]
    safe_df = df[safe_columns]

    preview_data = {
        "upload_id": upload_id,
        "filename": file.filename,
        "file_type": detected_type,
        "table_name": file_info["table"],
        "total_rows": len(df),
        "columns": safe_columns,
        "sample_rows": safe_df.head(10).fillna("").to_dict(orient="records"),
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

        response = RedirectResponse(url="/admin/dashboard", status_code=303)
        _set_flash(response, "success", "Materialized views refreshed successfully.")
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

    try:
        rows = execute_query("""
            SELECT id, filename, table_name, file_type,
                   rows_imported, rows_skipped, status,
                   error_message, started_at, completed_at,
                   duration_seconds, uploaded_by
            FROM import_history
            ORDER BY started_at DESC
            LIMIT 50
        """)
    except Exception:
        # Table may not exist yet if migration has not been run
        rows = []

    return templates.TemplateResponse(
        "admin/history.html",
        {
            "request": request,
            "history": rows,
            "messages": _get_flash(request),
        },
    )

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

@router.post("/erasure", response_class=HTMLResponse)
async def process_erasure(request: Request, idcard_hash: str = Form(...), csrf_token: str = Form("")):
    """Process a PDPA erasure request for a patient by idcard_hash."""
    _require_auth(request)
    if not _validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF validation failed")

    try:
        with get_conn() as conn:
            cur = conn.cursor()
            # Execute erasure
            cur.execute("SELECT execute_patient_erasure(%s)", (idcard_hash,))
            rows_deleted = cur.fetchone()[0]
            # Log the erasure request
            cur.execute(
                """INSERT INTO erasure_requests
                   (idcard_hash, status, processed_date, rows_deleted, processed_by)
                   VALUES (%s, 'completed', NOW(), %s, 'admin')""",
                (idcard_hash, rows_deleted),
            )
            # Refresh views after deletion
            etl = _load_etl()
            etl.refresh_all_summaries(cur)
            conn.commit()

        response = RedirectResponse(url="/admin/dashboard", status_code=303)
        _set_flash(response, "success", f"Erasure complete: {rows_deleted} records deleted.")
        return response
    except Exception as exc:
        response = RedirectResponse(url="/admin/dashboard", status_code=303)
        _set_flash(response, "error", f"Erasure failed: {_sanitize_error(exc)}")
        return response

# =========================================================================== #
# API: Table counts (JSON, for AJAX dashboard refresh)
# =========================================================================== #

@router.get("/api/table-counts")
async def api_table_counts(request: Request):
    """Return table and view row counts as JSON."""
    _require_auth(request)

    raw_tables = execute_query("""
        SELECT relname AS name, n_live_tup AS count
        FROM pg_stat_user_tables
        WHERE schemaname = 'public' AND relname LIKE 'raw_%%'
        ORDER BY relname
    """)

    mat_views = execute_query("""
        SELECT matviewname AS name
        FROM pg_matviews
        WHERE schemaname = 'public'
        ORDER BY matviewname
    """)

    view_counts = []
    for mv in mat_views:
        view_name = mv["name"]
        row_count = execute_scalar(f'SELECT COUNT(*) FROM "{view_name}"') or 0
        view_counts.append({"name": view_name, "count": row_count})

    return JSONResponse({
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
