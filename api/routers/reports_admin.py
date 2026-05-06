"""Admin endpoints for the report PDF cache — Sprint S9 Task 2.3.

Routes
------
POST   /api/v2/admin/reports/rebuild        — enqueue popular set + drain
GET    /api/v2/admin/reports/cache/stats    — aggregate cache stats
GET    /api/v2/admin/reports/cache/manifest — manifest rows for a report
DELETE /api/v2/admin/reports/cache/{hash}   — drop one entry

Auth follows the existing admin pattern: cookie session OR Bearer token via
``auth.require_admin_session_or_bearer``. The route is registered under
``/api/v2/admin/...`` (a private namespace — the public ``/api/v2/reports``
surface in ``reports_v2.py`` doesn't expose any of these).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("api.routers.reports_admin")

router = APIRouter(prefix="/api/v2/admin/reports", tags=["Admin API"])


# ---------------------------------------------------------------------------
# Auth dependency — wraps require_admin_session_or_bearer so each route just
# declares ``Depends(_admin_required)`` instead of repeating the signature.
# ---------------------------------------------------------------------------


def _admin_required(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> str:
    from auth import require_admin_session_or_bearer
    return require_admin_session_or_bearer(request, authorization)


# ---------------------------------------------------------------------------
# Lazy worker / cache lookups — keep importable when reports services aren't
# wired (e.g. fresh checkout, mid-merge).
# ---------------------------------------------------------------------------


def _get_worker():
    from services.reports.build_worker import get_build_worker
    return get_build_worker()


def _get_cache():
    from services.reports.pdf_cache import get_pdf_cache
    return get_pdf_cache()


def _get_registry():
    from services.reports.registry import report_registry
    return report_registry()


# ---------------------------------------------------------------------------
# Track in-flight rebuild jobs so the operator can poll status.
# ---------------------------------------------------------------------------

_JOBS: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Pydantic request bodies
# ---------------------------------------------------------------------------


class RebuildRequest(BaseModel):
    report_ids: Optional[List[str]] = Field(
        default=None,
        description="Subset of report ids to rebuild; null/missing = all.",
    )
    force: bool = Field(
        default=False,
        description="If true, ignore cache hits and rebuild every variant.",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/rebuild")
async def rebuild(
    body: RebuildRequest,
    _principal: str = Depends(_admin_required),
):
    """Enqueue the popular set for the given (or all) report ids and run.

    Response is returned IMMEDIATELY after enqueue + dispatch — the actual
    drain happens in a background task. ``job_id`` lets the operator poll
    later.
    """
    try:
        worker = _get_worker()
        registry = _get_registry()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"report services not available: {exc}",
        ) from exc

    requested = body.report_ids or registry.list_ids()
    enqueued = 0
    for rid in requested:
        try:
            enqueued += worker.enqueue_popular_set(rid)
        except Exception:
            logger.exception("rebuild: enqueue_popular_set failed for %s", rid)

    job_id = uuid.uuid4().hex[:12]
    started_at = time.time()
    _JOBS[job_id] = {
        "job_id": job_id,
        "started_at": started_at,
        "status": "running",
        "report_ids": requested,
        "force": body.force,
        "enqueued": enqueued,
    }

    # Toggle force on the worker for this drain. ``force`` is a worker-wide
    # flag (single-threaded drain) so we restore it once the run finishes.
    prev_force = getattr(worker, "_force", False)
    worker._force = bool(body.force)

    async def _runner():
        try:
            stats = await worker.run_pending()
            _JOBS[job_id].update(stats)
            _JOBS[job_id]["status"] = "completed"
            _JOBS[job_id]["finished_at"] = time.time()
        except Exception as exc:
            logger.exception("rebuild job %s failed", job_id)
            _JOBS[job_id]["status"] = "failed"
            _JOBS[job_id]["error"] = str(exc)
            _JOBS[job_id]["finished_at"] = time.time()
        finally:
            worker._force = prev_force

    # Try to schedule on the running event loop; fall back to a daemon thread.
    try:
        asyncio.create_task(_runner())
    except RuntimeError:
        def _t():
            asyncio.run(_runner())
        threading.Thread(target=_t, daemon=True, name=f"reports-rebuild-{job_id}").start()

    return {
        "enqueued": enqueued,
        "started_at": started_at,
        "job_id": job_id,
        "report_ids": requested,
        "force": body.force,
    }


@router.get("/rebuild/{job_id}")
async def rebuild_status(
    job_id: str,
    _principal: str = Depends(_admin_required),
):
    """Status for a previously-issued rebuild job."""
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/cache/stats")
async def cache_stats(
    _principal: str = Depends(_admin_required),
):
    """Aggregate cache stats — total bytes, file count, per-report rollups."""
    try:
        return _get_cache().stats()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"pdf_cache not available: {exc}",
        ) from exc


@router.get("/cache/manifest")
async def cache_manifest(
    report_id: str,
    _principal: str = Depends(_admin_required),
):
    """Manifest rows for a single report id (JSON list)."""
    try:
        cache = _get_cache()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"pdf_cache not available: {exc}",
        ) from exc
    rows = cache.list_for_report(report_id)
    # Convert dataclass rows to dicts for JSON serialization.
    return [
        {
            "hash": r.hash,
            "report_id": r.report_id,
            "fmt": r.fmt,
            "lang": r.lang,
            "audience": r.audience,
            "params": r.params,
            "path": r.path,
            "bytes": r.bytes,
            "created_at": r.created_at,
            "data_version": r.data_version,
            "descriptor_mtime": r.descriptor_mtime,
        }
        for r in rows
    ]


@router.delete("/cache/{hash}", status_code=204)
async def cache_delete(
    hash: str,
    _principal: str = Depends(_admin_required),
):
    """Remove one cache entry. 204 on success, 404 if unknown hash."""
    try:
        cache = _get_cache()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"pdf_cache not available: {exc}",
        ) from exc
    if not cache.delete(hash):
        raise HTTPException(status_code=404, detail="hash not found in cache")
    return JSONResponse(status_code=204, content=None)
