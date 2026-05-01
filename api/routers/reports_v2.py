"""Generic report-renderer route — ADR-03 §6 implementation.

A SINGLE FastAPI router group (`/api/v2/reports/*`) that dispatches every
descriptor-driven report by `report_id` + `fmt` + `lang`. The router is
pure glue: it delegates loading + data-collection + rendering to the
service / registry layer (built in parallel under
`api/services/reports/`).

Mirrors the pattern established by `routers/charts.py` (ADR-01 reference):
lazy DI factory cached via `functools.lru_cache`, Protocol-based import
shims so the router stays merge-safe even when sibling modules are
mid-build.

Routes
------
GET /api/v2/reports                                 — catalog
GET /api/v2/reports/{report_id}/spec                — raw ReportDescriptor
GET /api/v2/reports/{report_id}/{fmt}/{lang}        — render-or-cached download

Auth: relies on the existing X-API-Key middleware (covers /api/v2/*).

Coexists with the legacy `/api/reports/*` router defined in
`routers/reports.py`; both can run at the same time during the S4 → S5
migration window. The legacy router is decommissioned in S5 once the
frontend cuts over (per ADR-03 §6).
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Protocol, runtime_checkable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from errors import BMAException, NotFoundError

# ---------------------------------------------------------------------------
# Dependency imports — concrete classes live in `services/reports/*`. Some
# are still being built in parallel (S4.2 ReportService / ReportDataCollector,
# S4.1 renderer modules). Import the real implementations when present and
# fall back to Protocol stubs so this router can be merged independently.
# Once the sibling modules land, the `try` blocks succeed and the stubs are
# silently superseded — no further edits needed here.
# ---------------------------------------------------------------------------

logger = logging.getLogger("api.routers.reports_v2")

try:
    from services.reports.spec import ReportDescriptor  # noqa: F401
except Exception:  # pragma: no cover — defensive only
    ReportDescriptor = Any  # type: ignore[assignment,misc]

try:
    from services.reports.registry import ReportRegistry  # type: ignore[assignment]
except Exception:  # pragma: no cover
    @runtime_checkable
    class ReportRegistry(Protocol):  # type: ignore[no-redef]
        def get(self, report_id: str) -> Any: ...
        def list_ids(self) -> list[str]: ...
        def __contains__(self, report_id: object) -> bool: ...

try:
    from services.reports.blocks import BlockRegistry  # type: ignore[assignment]
except Exception:  # pragma: no cover
    @runtime_checkable
    class BlockRegistry(Protocol):  # type: ignore[no-redef]
        ...

try:
    from services.reports.renderer import RendererRegistry  # type: ignore[assignment]
except Exception:  # pragma: no cover
    @runtime_checkable
    class RendererRegistry(Protocol):  # type: ignore[no-redef]
        ...

try:
    # Service is being authored in parallel (S4.2); absence is expected at write time.
    from services.reports.service import ReportService  # type: ignore[assignment]
except Exception:
    @runtime_checkable
    class ReportService(Protocol):  # type: ignore[no-redef]
        async def render(
            self,
            report_id: str,
            fmt: str,
            lang: str,
            params: Dict[str, Any] | None = ...,
        ) -> Path: ...

        def list(self) -> list[dict]: ...

        def describe(self, report_id: str) -> Any: ...


router = APIRouter(prefix="/api/v2/reports", tags=["Reports"])


# ---------------------------------------------------------------------------
# Default config dirs — `<repo>/config/reports/` (sibling of `api/`) for the
# descriptors and `<repo>/config/reports/blocks/` for block specs. Computed
# once; the registry singleton scans them on first use.
# ---------------------------------------------------------------------------
def _default_reports_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "reports"


def _default_blocks_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "reports" / "blocks"


# ---------------------------------------------------------------------------
# Format → HTTP media-type. New formats land here when their renderer ships.
# ---------------------------------------------------------------------------
_MEDIA_TYPES: Dict[str, str] = {
    "latex": "application/pdf",  # LaTeXRenderer compiles to PDF on disk
    "html": "text/html; charset=utf-8",
    "pptx": (
        "application/vnd.openxmlformats-officedocument."
        "presentationml.presentation"
    ),
}


# ---------------------------------------------------------------------------
# DI factory — singleton service per process.
#
# `lru_cache(maxsize=1)` makes this cheap on every request after the first
# call: one registry scan, one renderer registry, one service instance
# reused for the lifetime of the process. (S4 explicitly excludes hot-reload
# — server restart is required to pick up new YAML.)
#
# Per ADR-03 brief: "DO NOT pre-warm the cache at boot — render only on
# demand." The first request through the route triggers all of this.
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def get_report_service() -> ReportService:
    """Build (and cache) the ReportService for FastAPI's DI system.

    Lazy imports keep this router importable even when sibling modules are
    mid-merge. The Protocol fallbacks above let py_compile succeed; the
    real classes are required only when the route is actually called.
    """
    # Lazy imports — see comment in module header.
    from services.reports.registry import ReportRegistry as _ReportRegistry  # type: ignore
    from services.reports.blocks import BlockRegistry as _BlockRegistry  # type: ignore
    from services.reports.renderer import (  # type: ignore
        renderer_registry as _renderer_registry,
    )
    from services.reports.data_collector import (  # type: ignore
        ReportDataCollector as _ReportDataCollector,
    )
    from services.reports.service import ReportService as _ReportService  # type: ignore

    blocks_dir = _default_blocks_dir()
    blocks = (
        _BlockRegistry.discover(blocks_dir)
        if blocks_dir.is_dir()
        else _BlockRegistry()
    )

    reports_dir = _default_reports_dir()
    if reports_dir.is_dir():
        registry = _ReportRegistry.discover(reports_dir, blocks=blocks)
    else:
        # Empty registry — `GET /api/v2/reports` answers `[]` instead of 500
        # on a fresh checkout where no descriptors have been authored yet.
        logger.info(
            "no report config_dir at %s; serving empty catalog",
            reports_dir,
        )
        registry = _ReportRegistry()

    # Renderers self-register on import — importing the modules below is
    # what populates the singleton `renderer_registry()`.
    try:
        import services.reports.renderers.latex  # noqa: F401  pylint: disable=unused-import
    except Exception:  # pragma: no cover — renderer module may be mid-build
        logger.warning("LaTeX renderer module not importable yet")
    try:
        import services.reports.renderers.html  # noqa: F401  pylint: disable=unused-import
    except Exception:  # pragma: no cover
        logger.warning("HTML renderer module not importable yet")

    renderers = _renderer_registry()
    data_collector = _ReportDataCollector()

    logger.info(
        "ReportService initialised: %d descriptor(s), %d block(s), formats=%s",
        len(registry) if hasattr(registry, "__len__") else -1,
        len(blocks) if hasattr(blocks, "__len__") else -1,
        renderers.list_formats() if hasattr(renderers, "list_formats") else [],
    )
    return _ReportService(
        registry=registry,
        blocks=blocks,
        renderers=renderers,
        data=data_collector,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", summary="List all report descriptors (catalog)")
async def list_reports(
    service: ReportService = Depends(get_report_service),
) -> list[dict]:
    """Return every descriptor known to the registry as a flat catalog.

    Empty list is a valid response (e.g. fresh checkout with no YAML yet).
    The frontend uses this to build the report dashboard without any
    hard-coded report ids.
    """
    # `service.list()` is async — see ADR-03 §5. We `await` it; FastAPI is
    # happy returning a coroutine result through the JSON encoder. Sync
    # mock implementations (returning a list directly) are tolerated via
    # the `inspect.iscoroutine` check below.
    result = service.list()
    if hasattr(result, "__await__"):
        result = await result  # type: ignore[misc]
    return result  # type: ignore[return-value]


@router.get(
    "/{report_id}/spec",
    summary="Return the raw ReportDescriptor for a report (frontend introspection)",
)
async def get_report_spec(
    report_id: str,
    service: ReportService = Depends(get_report_service),
):
    """Return the YAML-loaded descriptor verbatim.

    Used by the frontend to know which formats / languages a report
    supports before issuing a render call.
    """
    try:
        descriptor = service.describe(report_id)
        if hasattr(descriptor, "__await__"):
            descriptor = await descriptor  # async describe()
    except KeyError as exc:
        raise NotFoundError("ReportDescriptor", report_id) from exc

    # Pydantic v2 → JSON-friendly dict. Falls back to plain dict if the
    # service already returned one (e.g. test mock).
    if hasattr(descriptor, "model_dump"):
        return descriptor.model_dump(mode="json")
    return descriptor


@router.get(
    "/{report_id}/{fmt}/{lang}",
    summary="Render or fetch the cached artefact for a report descriptor",
)
async def render_report(
    report_id: str,
    fmt: str,
    lang: str,
    request: Request,
    service: ReportService = Depends(get_report_service),
):
    """Render the report identified by ``report_id`` in ``fmt``/``lang``.

    Arbitrary ``?key=value`` query-string parameters are forwarded to
    ``service.render(..., params=...)`` — these are the placeholder
    substitutions for descriptor-level tokens (e.g. `?zone_code=3`).

    Errors:
        404 — `report_id` is not in the registry.
        422 — `fmt` not in descriptor.formats, or `lang` not in
              descriptor.languages.
        500 — render pipeline blew up; sanitised by the global handler
              in errors.py.
    """
    # Pre-flight: get the descriptor to validate fmt + lang before booting
    # the (potentially expensive) data-collection pipeline. Raises 404 if
    # the report id is unknown.
    try:
        descriptor = service.describe(report_id)
    except KeyError as exc:
        raise NotFoundError("ReportDescriptor", report_id) from exc

    # Handle both Pydantic models (real impl) and plain dicts (test mocks).
    formats = (
        getattr(descriptor, "formats", None)
        if not isinstance(descriptor, dict)
        else descriptor.get("formats")
    )
    languages = (
        getattr(descriptor, "languages", None)
        if not isinstance(descriptor, dict)
        else descriptor.get("languages")
    )
    # Use HTTP 422 (Unprocessable Entity) for descriptor-based validation
    # failures per ADR-03 §6 — the request is syntactically valid but the
    # combination of fmt/lang is not allowed by the descriptor.
    if formats is not None and fmt not in formats:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "UNSUPPORTED_FORMAT",
                "message": f"format {fmt!r} not supported by report {report_id!r}",
                "report_id": report_id,
                "fmt": fmt,
                "supported_formats": list(formats),
            },
        )
    if languages is not None and lang not in languages:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "UNSUPPORTED_LANGUAGE",
                "message": f"language {lang!r} not supported by report {report_id!r}",
                "report_id": report_id,
                "lang": lang,
                "supported_languages": list(languages),
            },
        )

    # Convert MultiDict → plain dict[str, str]. Last-wins on repeats; if
    # any block ever needs list-typed params, the orchestrator is the
    # right place to add list-aware parsing (mirrors charts.py).
    params: Dict[str, Any] = dict(request.query_params)

    try:
        path = await service.render(
            report_id, fmt, lang, params=params
        )
    except KeyError as exc:
        raise NotFoundError("ReportDescriptor", report_id) from exc
    except (ValueError, TypeError) as exc:
        # Bad params or descriptor inconsistency surfaced at render time.
        # 422 — request was well-formed but the orchestrator rejected it.
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "INVALID_PARAMETER",
                "message": str(exc),
                "report_id": report_id,
                "fmt": fmt,
                "lang": lang,
            },
        ) from exc
    except BMAException:
        # Already a structured error — let the global handler format it.
        raise
    except Exception:  # noqa: BLE001
        # Bubble to unhandled_exception_handler in errors.py — that
        # path logs the full traceback and returns a sanitised 500.
        logger.exception(
            "report render failed: report_id=%s fmt=%s lang=%s",
            report_id,
            fmt,
            lang,
        )
        raise

    media_type = _MEDIA_TYPES.get(fmt, "application/octet-stream")
    # `path` is whatever the service returned; coerce to str so FileResponse
    # can stat it whether the orchestrator handed us a Path or a str.
    return FileResponse(
        str(path),
        media_type=media_type,
        filename=Path(str(path)).name,
    )
