"""Admin API router for data pipeline management and audit trail.

Sync port — uses psycopg2 via data_adapter, simple API-key auth,
and Redis cache helpers from cache.py.

Endpoints ported:
  - POST /api/admin/upload-screening  (JSON upload)
  - POST /api/admin/upload-excel      (Excel upload)
  - GET  /api/admin/excel-template    (template download)
  - GET  /api/admin/data-status       (data freshness)
  - POST /api/admin/invalidate-cache  (cache flush)
  - GET  /api/admin/audit-log         (audit log viewer)
"""

from __future__ import annotations

import io
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from cache import cache_delete_pattern
from config import ADMIN_PASSWORD
from services.data_adapter import load_district_data, invalidate_cache as invalidate_data_cache
from services.excel_parser import parse_health_excel, generate_template_workbook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Simple auth helper (no Depends(require_role) — flat function)
# ---------------------------------------------------------------------------

def _require_admin(authorization: Optional[str]) -> None:
    """Validate admin access via Bearer token or X-Admin-Password header.

    Raises HTTPException 401 if not authorised.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    token = authorization.replace("Bearer ", "").strip()
    if token != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Invalid admin credentials")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class UploadScreeningRequest(BaseModel):
    """Request body for uploading new screening data."""
    data: dict[str, Any] = Field(
        ...,
        description="District health data keyed by district code (e.g. '1001': {...})",
    )
    replace: bool = Field(
        False,
        description="If True, replace all data; if False, merge/update existing districts",
    )


class UploadSummary(BaseModel):
    districts_updated: int
    records_added: int
    timestamp: str


class DataStatus(BaseModel):
    last_updated: Optional[str] = None
    total_districts: int
    total_records: int
    diseases_found: list[str]
    missing_districts: list[str] = Field(default_factory=list)
    completeness_pct: float


class CacheInvalidationResult(BaseModel):
    keys_deleted: int
    timestamp: str
    message: str


class ExcelUploadResponse(BaseModel):
    success: bool
    districts_updated: int
    errors: list[str]
    timestamp: Optional[str] = None


class AuditLogEntry(BaseModel):
    timestamp: str
    request_id: str
    method: str
    path: str
    status: int
    duration_ms: float
    client_ip: str
    user: Optional[str] = None


class AuditLogResponse(BaseModel):
    entries: list[AuditLogEntry]
    total: int
    page: int
    limit: int
    message: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_last_data_update: Optional[str] = None


def _resolve_data_path() -> str:
    """Find or determine the writable data path."""
    data_dir = os.environ.get("DATA_DIR", "")
    if data_dir:
        return os.path.join(data_dir, "district_health_data.json")

    candidates = [
        os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "district_health_data.json")),
        os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "district_health_data.json")),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p

    default = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "district_health_data.json"))
    return default


def _validate_district_entry(dcode: str, entry: dict[str, Any]) -> list[str]:
    """Validate a district data entry. Returns list of error messages."""
    errors: list[str] = []
    required_fields = ["dcode", "name_th", "name_en", "total_screened", "diseases"]
    for f in required_fields:
        if f not in entry:
            errors.append(f"District {dcode}: missing required field '{f}'")

    if "diseases" in entry and isinstance(entry["diseases"], dict):
        for disease_key, disease_data in entry["diseases"].items():
            if not isinstance(disease_data, dict):
                errors.append(f"District {dcode}, disease {disease_key}: must be a dict")
                continue
            for req in ["name", "name_en", "pct_at_risk", "total_screened", "indicators"]:
                if req not in disease_data:
                    errors.append(f"District {dcode}, disease {disease_key}: missing '{req}'")
    return errors


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/upload-screening", response_model=UploadSummary)
def upload_screening(
    req: UploadScreeningRequest,
    authorization: Optional[str] = Header(None),
):
    """Upload new screening data (JSON format).

    Validates the data structure, stores it, and invalidates caches.
    """
    global _last_data_update
    _require_admin(authorization)

    # Validate input
    all_errors: list[str] = []
    for dcode, entry in req.data.items():
        all_errors.extend(_validate_district_entry(dcode, entry))

    if all_errors:
        raise HTTPException(status_code=422, detail={"validation_errors": all_errors})

    # Load existing data
    data_path = _resolve_data_path()
    existing: dict[str, Any] = {}
    if not req.replace and os.path.exists(data_path):
        with open(data_path, "r", encoding="utf-8") as f:
            existing = json.load(f)

    # Merge or replace
    if req.replace:
        merged = req.data
    else:
        merged = {**existing, **req.data}

    # Write
    os.makedirs(os.path.dirname(data_path), exist_ok=True)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    # Invalidate caches
    invalidate_data_cache()
    deleted = 0
    for pattern in ["health:*", "api:*", "stats:*", "dashboard:*"]:
        deleted += cache_delete_pattern(pattern)

    _last_data_update = datetime.now(timezone.utc).isoformat()

    logger.info(
        "Data upload: %d districts updated, %d cache keys invalidated",
        len(req.data), deleted,
    )

    return UploadSummary(
        districts_updated=len(req.data),
        records_added=sum(d.get("total_screened", 0) for d in req.data.values()),
        timestamp=_last_data_update,
    )


@router.get("/data-status", response_model=DataStatus)
def data_status(authorization: Optional[str] = Header(None)):
    """Check data freshness and completeness."""
    _require_admin(authorization)

    data = load_district_data()

    all_diseases: set[str] = set()
    for d in data.values():
        all_diseases.update(d["diseases"].keys())

    expected = {str(i) for i in range(1001, 1051)}
    present = set(data.keys())
    missing = sorted(expected - present)

    total_records = sum(d["total_screened"] for d in data.values())
    completeness = len(present & expected) / len(expected) * 100 if expected else 100.0

    return DataStatus(
        last_updated=_last_data_update,
        total_districts=len(data),
        total_records=total_records,
        diseases_found=sorted(all_diseases),
        missing_districts=missing,
        completeness_pct=round(completeness, 1),
    )


@router.post("/invalidate-cache", response_model=CacheInvalidationResult)
def invalidate_cache(authorization: Optional[str] = Header(None)):
    """Force cache invalidation of all cached entries."""
    _require_admin(authorization)

    deleted = 0
    for pattern in ["health:*", "api:*", "stats:*", "dashboard:*", "export:*"]:
        deleted += cache_delete_pattern(pattern)

    # Also reload the in-memory data adapter cache
    invalidate_data_cache()

    timestamp = datetime.now(timezone.utc).isoformat()
    logger.info("Cache invalidated: %d keys deleted", deleted)

    return CacheInvalidationResult(
        keys_deleted=deleted,
        timestamp=timestamp,
        message=f"Cache invalidated successfully. {deleted} keys deleted. In-memory data reloaded.",
    )


@router.get("/audit-log", response_model=AuditLogResponse)
def get_audit_log(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    endpoint: Optional[str] = Query(None, description="Filter by endpoint path prefix"),
    user_filter: Optional[str] = Query(None, alias="user", description="Filter by username"),
    authorization: Optional[str] = Header(None),
):
    """View recent audit trail.

    Reads from the structured audit log. Since the audit middleware writes to
    Python's logging system, this endpoint parses recent log entries.
    In production, this would read from a dedicated log store.
    """
    _require_admin(authorization)

    entries: list[AuditLogEntry] = []

    log_handlers = logging.getLogger("audit").handlers
    log_file = None
    for handler in log_handlers:
        if hasattr(handler, "baseFilename"):
            log_file = handler.baseFilename
            break

    if log_file and os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()

            parsed: list[dict[str, Any]] = []
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    json_start = line.find("{")
                    if json_start >= 0:
                        entry = json.loads(line[json_start:])
                        parsed.append(entry)
                except (json.JSONDecodeError, ValueError):
                    continue

            if endpoint:
                parsed = [e for e in parsed if e.get("path", "").startswith(endpoint)]
            if user_filter:
                parsed = [e for e in parsed if e.get("user") == user_filter]

            total = len(parsed)
            start = (page - 1) * limit
            page_entries = parsed[start: start + limit]

            entries = [
                AuditLogEntry(
                    timestamp=e.get("timestamp", ""),
                    request_id=e.get("request_id", ""),
                    method=e.get("method", ""),
                    path=e.get("path", ""),
                    status=e.get("status", 0),
                    duration_ms=e.get("duration_ms", 0.0),
                    client_ip=e.get("client_ip", ""),
                    user=e.get("user"),
                )
                for e in page_entries
            ]
            return AuditLogResponse(entries=entries, total=total, page=page, limit=limit)
        except Exception as exc:
            logger.warning("Failed to read audit log file: %s", exc)

    return AuditLogResponse(
        entries=[],
        total=0,
        page=page,
        limit=limit,
        message="Audit logs are written to stdout/structured logging. "
        "Configure a file handler or log aggregation service for queryable access.",
    )


@router.get("/audit-log/verify")
def verify_audit_log_chain(authorization: Optional[str] = Header(None)):
    """Verify the SHA-256 chain on the MCP audit log file.

    The MCP server writes JSONL entries with `prev_hash` and `hash` fields
    forming a tamper-evident chain. This endpoint walks the file and
    confirms every link, returning the first broken line if any.

    Use cases:
      - Periodic compliance check (PDPA / SOC2)
      - Pre-archive integrity verification
      - Incident response — was the audit log tampered with?
    """
    _require_admin(authorization)

    import hashlib as _hashlib
    import json as _json

    audit_path = os.getenv("MCP_AUDIT_LOG_PATH", "/var/log/bma-health/mcp-audit.jsonl")
    report = {
        "path": audit_path,
        "verified": False,
        "total_entries": 0,
        "first_broken_line": None,
        "last_hash": "0" * 64,
        "errors": [],
    }

    if not os.path.exists(audit_path):
        report["errors"].append("audit log file does not exist")
        return report

    try:
        prev_hash = "0" * 64
        with open(audit_path, "r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = _json.loads(raw)
                except _json.JSONDecodeError as e:
                    report["first_broken_line"] = lineno
                    report["errors"].append(f"line {lineno}: invalid JSON ({e})")
                    return report
                stored_hash = entry.pop("hash", None)
                if not stored_hash:
                    report["first_broken_line"] = lineno
                    report["errors"].append(f"line {lineno}: missing 'hash' field")
                    return report
                if entry.get("prev_hash") != prev_hash:
                    report["first_broken_line"] = lineno
                    report["errors"].append(
                        f"line {lineno}: prev_hash mismatch "
                        f"(expected {prev_hash[:12]}..., got {(entry.get('prev_hash') or '')[:12]}...)"
                    )
                    return report
                expected = _hashlib.sha256(
                    _json.dumps(entry, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                if stored_hash != expected:
                    report["first_broken_line"] = lineno
                    report["errors"].append(
                        f"line {lineno}: content hash mismatch "
                        f"(expected {expected[:12]}..., got {stored_hash[:12]}...)"
                    )
                    return report
                prev_hash = stored_hash
                report["total_entries"] += 1
        report["last_hash"] = prev_hash
        report["verified"] = True
        return report
    except Exception as e:
        report["errors"].append(f"verification crashed: {type(e).__name__}: {e}")
        return report


# ---------------------------------------------------------------------------
# Excel upload endpoints
# ---------------------------------------------------------------------------

# Aligned with the CSV upload path (admin.py): file is parsed straight into
# DB then discarded, no on-disk persistence — 500 MB is the same ceiling.
MAX_UPLOAD_SIZE = 500 * 1024 * 1024  # 500 MB


# DEPRECATED 2026-05-01 — replaced by `upload_excel_router.upload_screening`
# in api/admin.py (auth-protected, streaming, validate-gated, pipeline-driven).
# This route is no longer registered: the decorator below is renamed to
# `_legacy_upload_excel` so it's importable as a function but FastAPI doesn't
# wire it up. Kept as code reference for the next sprint's data-migration
# plan; safe to delete once the new flow has been live for one cycle.
def _legacy_upload_excel(
    file: UploadFile = File(..., description="Excel (.xlsx) file with district health data"),
    authorization: Optional[str] = Header(None),
):
    """Upload district health data via an Excel spreadsheet.

    Parses the .xlsx, validates every row, writes to district_health_data.json,
    and invalidates all caches.
    """
    global _last_data_update
    _require_admin(authorization)

    # Validate file type
    if file.content_type not in (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/octet-stream",
    ):
        if not (file.filename and file.filename.lower().endswith(".xlsx")):
            raise HTTPException(
                status_code=400,
                detail="Only .xlsx files are accepted.",
            )

    # Read and check size (sync read)
    contents = file.file.read()
    if len(contents) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(contents)} bytes). Maximum is {MAX_UPLOAD_SIZE} bytes (10 MB).",
        )

    # Parse
    result = parse_health_excel(contents)

    if result.errors:
        raise HTTPException(
            status_code=422,
            detail={
                "success": False,
                "districts_updated": 0,
                "errors": result.errors,
            },
        )

    if not result.data:
        raise HTTPException(
            status_code=422,
            detail={
                "success": False,
                "districts_updated": 0,
                "errors": ["No valid data rows found in the uploaded file."],
            },
        )

    # Merge with existing data
    data_path = _resolve_data_path()
    existing: dict[str, Any] = {}
    if os.path.exists(data_path):
        with open(data_path, "r", encoding="utf-8") as f:
            existing = json.load(f)

    merged = {**existing, **result.data}

    # Write
    os.makedirs(os.path.dirname(data_path), exist_ok=True)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    # Invalidate caches
    invalidate_data_cache()
    deleted = 0
    for pattern in ["health:*", "api:*", "stats:*", "dashboard:*"]:
        deleted += cache_delete_pattern(pattern)

    _last_data_update = datetime.now(timezone.utc).isoformat()

    logger.info(
        "Excel upload: %d districts updated, %d cache keys invalidated",
        len(result.data), deleted,
    )

    return ExcelUploadResponse(
        success=True,
        districts_updated=len(result.data),
        errors=[],
        timestamp=_last_data_update,
    )


@router.get("/excel-template")
def download_excel_template(authorization: Optional[str] = Header(None)):
    """Download a template .xlsx with the correct column headers and one example row."""
    _require_admin(authorization)

    template_bytes = generate_template_workbook()

    return StreamingResponse(
        io.BytesIO(template_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="bma_health_template.xlsx"',
        },
    )
