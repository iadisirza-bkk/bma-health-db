"""Generic chart-renderer route — ADR-01 §4 implementation.

A SINGLE FastAPI route that dispatches every dashboard chart by `spec_id`.
The router is pure glue: it delegates loading + SQL + k-anon to the
service / repository layer (built in parallel under
`api/services/charts/` and `api/repositories/`).

Per ADR-01, the wire envelope is identical for every chart kind — the
client picks an `OptionBuilder` strategy by `response.kind` and never
has to know an endpoint URL again.

Routes
------
GET /api/v2/charts                — catalog (spec_id + title for each)
GET /api/v2/charts/{spec_id}      — render the chart with query-string filters
GET /api/v2/charts/{spec_id}/spec — raw ChartSpec (frontend introspection)

Auth: relies on the existing X-API-Key middleware (covers /api/v2/*).
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Dict, Protocol, runtime_checkable

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from errors import BMAException, NotFoundError, InvalidParameterError

# ---------------------------------------------------------------------------
# Dependency imports — the concrete classes are being built in parallel by
# other agents (S2 sprint). We import the real implementations when present
# and fall back to Protocol stubs so this router can be merged independently.
# Once the sibling modules land, the `try` block succeeds and the stubs are
# silently superseded — no further edits needed here.
# ---------------------------------------------------------------------------

logger = logging.getLogger("api.routers.charts")

try:
    # Spec models always exist (services/charts/spec.py is committed).
    from services.charts.spec import ChartResponse, ChartSpec  # noqa: F401
except Exception:  # pragma: no cover — defensive only
    ChartResponse = Any  # type: ignore[assignment,misc]
    ChartSpec = Any  # type: ignore[assignment,misc]

try:
    from services.charts.registry import ChartRegistry  # type: ignore[assignment]
except Exception:  # pragma: no cover
    @runtime_checkable
    class ChartRegistry(Protocol):  # type: ignore[no-redef]
        def get(self, spec_id: str) -> Any: ...
        def list_ids(self) -> list[str]: ...
        def __contains__(self, spec_id: object) -> bool: ...

try:
    # Service is being authored in parallel; absence is expected at write time.
    from services.charts.service import ChartService  # type: ignore[assignment]
except Exception:
    @runtime_checkable
    class ChartService(Protocol):  # type: ignore[no-redef]
        def render(self, spec_id: str, filters: Dict[str, Any]) -> Any: ...

try:
    from repositories.mv_repository import MVRepository  # type: ignore[assignment]
except Exception:
    # `Exception` (not just ImportError) — sibling modules can have
    # transient class-body errors during parallel development; the router
    # itself must remain importable so unrelated routes keep serving.
    @runtime_checkable
    class MVRepository(Protocol):  # type: ignore[no-redef]
        ...


router = APIRouter(prefix="/api/v2/charts", tags=["charts"])


# ---------------------------------------------------------------------------
# Default config dir — `<repo>/config/charts/` (sibling of `api/`).
# Computed once; the registry singleton scans it on first use.
# ---------------------------------------------------------------------------
def _default_config_dir():
    from pathlib import Path

    return Path(__file__).resolve().parents[2] / "config" / "charts"


# ---------------------------------------------------------------------------
# DI factory — singleton service per process.
#
# `lru_cache` makes this cheap on every request after the first call: one
# registry scan, one repository instance, one service instance reused for
# the lifetime of the process. (S2 explicitly excludes hot-reload — server
# restart is required to pick up new YAML.)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _get_chart_registry():
    """Construct (once) the ChartRegistry by scanning `<repo>/config/charts/`.

    Kept separate from the full service factory so that the `/charts`
    catalog endpoint can answer even if the service / repository imports
    are mid-merge — the catalog only needs the registry.
    """
    from services.charts.registry import ChartRegistry as _Registry  # type: ignore

    config_dir = _default_config_dir()
    if not config_dir.is_dir():
        # No YAMLs yet — return an empty registry so the catalog endpoint
        # answers "[]" instead of 500ing on a fresh checkout.
        logger.info("no chart config_dir at %s; serving empty catalog", config_dir)
        return _Registry()
    registry = _Registry.discover(config_dir)
    logger.info(
        "chart registry initialised: %d spec(s) from %s",
        len(registry),
        config_dir,
    )
    return registry


@lru_cache(maxsize=1)
def get_chart_service() -> ChartService:
    """Build (and cache) the ChartService for FastAPI's DI system."""
    # Lazy imports — keep router importable even when sibling modules are
    # mid-merge. The Protocol fallbacks above let py_compile succeed; the
    # real classes are needed only when the route is actually called.
    from services.charts.service import ChartService as _Service  # type: ignore
    from repositories.mv_repository import MVRepository as _Repo  # type: ignore

    registry = _get_chart_registry()
    # MVRepository inherits from Repository (ADR-01 §5) and uses the
    # existing reader connection pool via database.get_conn() internally;
    # no constructor args are needed.
    repository = _Repo()
    # Constructor kw is `repo` (see services/charts/service.py:77).
    return _Service(registry=registry, repo=repository)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", summary="List all chart spec_ids and titles (catalog)")
def list_charts(registry=Depends(_get_chart_registry)) -> list[dict]:
    """Return every chart known to the registry as a flat catalog.

    Empty list is a valid response (e.g. fresh checkout with no YAML yet).
    The frontend uses this to build the dashboard menu without any
    hard-coded chart names.

    Only depends on the registry — the catalog endpoint must work even
    when the broader service / repository wiring is mid-merge.
    """
    catalog: list[dict] = []
    for spec_id in registry.list_ids():
        spec = registry.get(spec_id)
        catalog.append(
            {
                "spec_id": spec.spec_id,
                "kind": spec.kind,
                "title_th": spec.title_th,
                "title_en": spec.title_en,
            }
        )
    return catalog


@router.get("/{spec_id}", summary="Render a chart by spec_id with query-string filters")
async def render_chart(
    spec_id: str,
    request: Request,
    service: ChartService = Depends(get_chart_service),
):
    """Render the chart identified by ``spec_id``.

    Filters are read from the raw query string — the set of valid
    filter names is defined dynamically by `spec.accepts`, so we cannot
    declare them as typed FastAPI parameters. The service validates each
    filter against `spec.accepts` and raises on unknown / wrong-typed
    values (we map that to HTTP 422).

    Errors:
        404 — `spec_id` is not in the registry.
        422 — filter params don't match `spec.accepts`.
        500 — unexpected; sanitised by the global handler in errors.py.
    """
    # Pre-check via the registry so we can raise a 404 before booting the
    # full service (which might fail for reasons unrelated to this chart).
    registry = _get_chart_registry()
    if spec_id not in registry:
        raise NotFoundError("ChartSpec", spec_id)

    # Convert MultiDict → plain dict[str, str]. We intentionally drop
    # repeated keys (last-wins) since none of the v2 filter kinds in
    # FilterKind are list-typed today. If that ever changes, the service
    # layer is the right place to introduce list-aware parsing.
    filters: Dict[str, Any] = dict(request.query_params)

    try:
        return await service.render(spec_id, filters)
    except KeyError as exc:
        # Service / registry says spec_id is unknown.
        raise NotFoundError("ChartSpec", spec_id) from exc
    except (ValidationError, ValueError) as exc:
        # Filter validation failed in the service layer.
        raise InvalidParameterError("filters", str(exc)) from exc
    except BMAException:
        # Already a structured error — let the global handler format it.
        raise
    except Exception:  # noqa: BLE001
        # Bubble to unhandled_exception_handler in errors.py — that
        # path logs the full traceback and returns a sanitised 500.
        logger.exception("chart render failed: spec_id=%s", spec_id)
        raise


@router.get(
    "/{spec_id}/spec",
    summary="Return the raw ChartSpec (filters, axes, kind) for client introspection",
)
def get_spec(
    spec_id: str,
    registry=Depends(_get_chart_registry),
):
    """Return the YAML-loaded ChartSpec verbatim.

    The frontend uses this to know which filters a chart accepts before
    issuing a render call — no hard-coded knowledge of filter names per
    chart on the client side.
    """
    if spec_id not in registry:
        raise NotFoundError("ChartSpec", spec_id)

    spec = registry.get(spec_id)
    # Pydantic v2 → JSON-friendly dict (datetime, enums, etc. handled).
    if hasattr(spec, "model_dump"):
        return spec.model_dump(mode="json")
    return spec
