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
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, runtime_checkable

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
# S9 — content-hash cache helpers
#
# These helpers wrap the cache_keys + pdf_cache primitives so the route
# stays readable. They're intentionally defensive: every failure mode
# (DB outage, no descriptor YAML on disk, cache directory unwritable)
# falls through to the renderer as a "MISS" rather than aborting the
# request. The cache is an optimisation, never a correctness gate.
# ---------------------------------------------------------------------------


def _descriptor_yaml_mtime(report_id: str) -> float:
    """Return the descriptor YAML's ``st_mtime`` or ``0.0`` if missing.

    We cache by content hash, but we ALSO want a fresh hash whenever the
    YAML is edited (even if the underlying data didn't move) so authors
    see their changes immediately on the next request.
    """
    yaml_path = _default_reports_dir() / f"{report_id}.yaml"
    try:
        from services.reports.cache_keys import descriptor_mtime
        return descriptor_mtime(yaml_path)
    except Exception:  # pragma: no cover — defensive
        return 0.0


def _compute_data_version() -> tuple[str, str]:
    """Return ``(data_version, data_version_human)`` for the current DB.

    Both pieces share a single DB connection so we avoid double round-
    trip cost. Falls back gracefully when the connection cannot be
    obtained (e.g. tests without a DB pool).
    """
    try:
        from services.reports.cache_keys import (
            data_version as _dv,
            data_version_human as _dvh,
        )
        from database import get_conn
        with get_conn() as conn:
            return _dv(conn), _dvh(conn)
    except Exception as exc:  # noqa: BLE001
        logger.debug("S9 data_version compute failed: %s", exc)
        # Fallback hash bucketed to one minute keeps the cache key
        # stable for near-simultaneous requests but still drifts.
        import hashlib
        bucket = int(time.time() // 60)
        return (
            hashlib.sha256(f"fallback:{bucket}".encode()).hexdigest()[:16],
            "ไม่ทราบ",
        )


def _cache_age_seconds(path: Path) -> int:
    """Seconds since ``path`` was last modified (rounded down)."""
    try:
        return max(0, int(time.time() - path.stat().st_mtime))
    except OSError:
        return 0


def _ascii_safe(value: str) -> str:
    """Coerce ``value`` to a latin-1-safe string for HTTP headers.

    The freshness stamp can be Thai ("ไม่ทราบ" when the DB is silent) and
    Starlette refuses anything outside latin-1 in headers — so we strip
    + percent-encode characters that don't fit.
    """
    if not value:
        return ""
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        # Replace each non-latin-1 character with a ``\uXXXX`` escape so
        # ops dashboards still see something deterministic to grep.
        return value.encode("ascii", errors="backslashreplace").decode("ascii")


# ---------------------------------------------------------------------------
# Format → HTTP media-type. New formats land here when their renderer ships.
#
# S7 NOTE: ``latex`` was renamed to ``pdf`` (the LaTeXRenderer compiles to
# PDF on disk). Both keys map to ``application/pdf`` for the duration of
# the alias window — see ``services.reports.format_alias``.
# ---------------------------------------------------------------------------
_MEDIA_TYPES: Dict[str, str] = {
    "latex": "application/pdf",  # LaTeXRenderer compiles to PDF on disk
    "pdf": "application/pdf",
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

    S8 audience filter
    ------------------
    The OPTIONAL ``?audience=`` query parameter (comma-separated) drops
    sections whose underlying block declares an ``audience_target``
    that is not in the requested set. Audience-agnostic blocks
    (``audience_target = None`` — e.g. ``cover_page``, ``heading``)
    ALWAYS render. Unknown audience values produce a 400.

    Examples
    --------
    * ``?audience=executive`` → render only executive (+ agnostic) sections.
    * ``?audience=executive,clinician`` → render executive + clinician.
    * (omitted) → status quo, render every section.

    Errors:
        400 — bad ``audience=`` value.
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
    #
    # S7: alias-aware match — `?fmt=latex` succeeds against
    # `formats: [pdf, html]` (and vice versa) for one sprint.
    if formats is not None:
        try:
            from services.reports.format_alias import format_matches as _fm
        except Exception:  # pragma: no cover — defensive only
            _fm = lambda r, d: r in d  # type: ignore[assignment,misc]
        if not _fm(fmt, formats):
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

    # ------------------------------------------------------------------
    # S8 audience filter — extract & validate ``?audience=...`` BEFORE
    # forwarding ``params`` so the comma-list never reaches the
    # descriptor placeholder substitution layer (it's not a
    # ``{placeholder}`` key — it's orchestration metadata).
    # ------------------------------------------------------------------
    audience_raw = params.pop("audience", None)
    audience_filter: Optional[set[str]] = None
    if audience_raw is not None:
        try:
            from services.reports.blocks.base import AudienceTarget
        except Exception:  # pragma: no cover — defensive
            AudienceTarget = None  # type: ignore[assignment,misc]
        valid = (
            {a.value for a in AudienceTarget}
            if AudienceTarget is not None
            else {"people", "executive", "clinician", "researcher"}
        )
        requested = {p.strip() for p in str(audience_raw).split(",") if p.strip()}
        unknown = requested - valid
        if unknown or not requested:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": "INVALID_AUDIENCE",
                    "message": (
                        f"unknown audience value(s): {sorted(unknown)!r}; "
                        f"valid: {sorted(valid)!r}"
                    )
                    if unknown
                    else "audience= must list at least one value",
                    "report_id": report_id,
                    "audience": audience_raw,
                    "valid_audiences": sorted(valid),
                },
            )
        audience_filter = requested

    # ------------------------------------------------------------------
    # S9 prose polish — extract & validate ``?polish=1`` BEFORE forwarding
    # ``params`` (it is orchestration metadata, not a descriptor key).
    # Defaults to OFF; admin/dev opt-in only at this stage. Audience-summary
    # blocks are excluded by allow-list inside the polish service so the
    # router does not need to re-check that here.
    # ------------------------------------------------------------------
    polish_raw = params.pop("polish", None)
    polish_enabled = str(polish_raw).strip().lower() in (
        "1", "true", "yes", "on"
    )
    feature_flags: Dict[str, Any] = {}
    polish_svc: Any = None
    if polish_enabled:
        try:
            from services.reports.polish import TextPolishService

            polish_svc = TextPolishService()
            feature_flags["polish_prose"] = True
        except Exception:  # noqa: BLE001 — polish is best-effort, never blocks
            polish_svc = None

    # ------------------------------------------------------------------
    # S9 freshness stamp + content-hash cache.
    #
    # Compute data_version (+ its human-readable date) ONCE per request.
    # The human date is plumbed through to the cover via the existing
    # ``ctx.feature_flags`` dict (cover_page reads ``feature_flags
    # ["data_as_of"]`` when ``params.data_as_of`` is unset). We re-use the
    # same data_version for the cache key so a HIT and a MISS print the
    # exact same stamp.
    # ------------------------------------------------------------------
    data_ver, data_ver_human = _compute_data_version()
    feature_flags["data_as_of"] = data_ver_human

    cache_hash: Optional[str] = None
    pdf_cache: Any = None
    cache_audience: Optional[list[str]] = None
    cache_params: Dict[str, Any] = {}
    try:
        from services.reports.cache_keys import content_hash as _content_hash
        from services.reports.format_alias import canonicalize as _canon_fmt
        from services.reports.pdf_cache import get_pdf_cache as _get_cache

        # Polish + audience filter both shift the output, so they're part
        # of the cache key. Extra params with non-string values are
        # canonicalised inside ``content_hash``.
        cache_audience = (
            sorted(audience_filter) if audience_filter else None
        )
        cache_params = dict(params)
        if polish_enabled:
            cache_params["__polish__"] = "1"

        cache_hash = _content_hash(
            report_id=report_id,
            fmt=_canon_fmt(fmt),
            lang=lang,
            audience=cache_audience,
            params=cache_params,
            data_version=data_ver,
            descriptor_mtime=_descriptor_yaml_mtime(report_id),
        )
        pdf_cache = _get_cache()
        cached_path = pdf_cache.get(cache_hash)
    except Exception as exc:  # noqa: BLE001 — cache is best-effort
        logger.debug("S9 cache lookup failed (treating as miss): %s", exc)
        cached_path = None

    if cached_path is not None and cache_hash is not None:
        # Update last_served_at and stream the file. Any failure in the
        # touch path is swallowed — the response must still go out.
        try:
            pdf_cache.touch(cache_hash)
        except Exception:  # noqa: BLE001
            pass
        media_type = _MEDIA_TYPES.get(fmt, "application/octet-stream")
        logger.info(
            "S9 cache HIT report_id=%s fmt=%s lang=%s hash=%s",
            report_id, fmt, lang, cache_hash,
        )
        return FileResponse(
            str(cached_path),
            media_type=media_type,
            filename=Path(str(cached_path)).name,
            headers={
                "X-Cache": "HIT",
                "X-Cache-Age": str(_cache_age_seconds(cached_path)),
                "X-Cache-Hash": cache_hash,
                "X-Data-Version": _ascii_safe(data_ver),
                "X-Data-As-Of": _ascii_safe(data_ver_human),
            },
        )

    # ------------------------------------------------------------------
    # Cache MISS — fall through to the existing render path. After
    # ReportService.render() succeeds, we ``put`` into the cache so the
    # next identical request hits the fast path.
    # ------------------------------------------------------------------
    try:
        path = await service.render(
            report_id, fmt, lang,
            params=params,
            audience=audience_filter,
            feature_flags=feature_flags,
            polish_service=polish_svc,
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

    # S9 — populate the cache from the freshly rendered artefact. The
    # write is best-effort (the response must succeed even if the cache
    # is unwritable). We copy via ``shutil.copy2`` inside ``put`` so the
    # renderer's tmp-dir cleanup can't yank the file out from under us.
    served_path = Path(str(path))
    if cache_hash is not None and pdf_cache is not None:
        try:
            from services.reports.format_alias import canonicalize as _canon_fmt
            pdf_cache.put(
                cache_hash,
                served_path,
                report_id=report_id,
                fmt=_canon_fmt(fmt),
                lang=lang,
                audience=cache_audience,
                params=cache_params,
                data_version=data_ver,
                descriptor_mtime=_descriptor_yaml_mtime(report_id),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("S9 cache put failed (response unaffected): %s", exc)

    media_type = _MEDIA_TYPES.get(fmt, "application/octet-stream")
    # `path` is whatever the service returned; coerce to str so FileResponse
    # can stat it whether the orchestrator handed us a Path or a str.
    return FileResponse(
        str(served_path),
        media_type=media_type,
        filename=served_path.name,
        headers={
            "X-Cache": "MISS",
            "X-Cache-Hash": cache_hash or "",
            "X-Data-Version": _ascii_safe(data_ver),
            "X-Data-As-Of": _ascii_safe(data_ver_human),
        },
    )
