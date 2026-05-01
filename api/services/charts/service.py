"""ChartService — orchestrator that turns a (spec_id, filters) pair into
a wire-format ChartResponse, with k-anonymity enforcement applied as
defense in depth.

Layering rules (ADR-01 §5):
    * NEVER touches the database directly. Calls ``self.repo.run_query``.
    * Looks the spec up via the injected ``ChartRegistry``.
    * Applies k-anon at three layers (SQL HAVING in the MV, this service,
      and a final field-name assertion). All three have to fail for PII
      to leak — see ADR-01 §8.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from .registry import ChartRegistry
from .spec import (
    ChartDataRow,
    ChartMeta,
    ChartResponse,
    ChartSpec,
)

logger = logging.getLogger("api.services.charts.service")

# ---------------------------------------------------------------------------
# Load `assert_no_individual_fields` from the sibling /Users/dev/bma-med repo
# without polluting `sys.path`. Why not `import bma_med.security.k_anon`?
#   * `api/security.py` (this repo) lives on `sys.path` and would shadow
#     `import security` from the sibling repo.
#   * sibling repo isn't pip-installed and has no setup.py.
# So we resolve the file path explicitly and load it as a private module
# under a unique name. This survives any sys.path ordering and is the same
# trick CPython's `importlib.util` page recommends for cross-repo helpers.
# ---------------------------------------------------------------------------
def _load_assert_no_individual_fields() -> Callable[..., None]:
    candidates = [
        os.environ.get("BMA_MED_PATH"),
        "/Users/dev/bma-med",
        str(Path(__file__).resolve().parents[4] / "bma-med"),
    ]
    for root in candidates:
        if not root:
            continue
        k_anon_path = Path(root) / "security" / "k_anon.py"
        if not k_anon_path.is_file():
            continue
        spec = importlib.util.spec_from_file_location(
            "_bma_med_k_anon_private", str(k_anon_path)
        )
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            continue
        module = importlib.util.module_from_spec(spec)
        # Cache under a private name so reloads of this service module
        # don't re-execute the file (avoids surprising side-effects like
        # the if-__main__ self-test block).
        sys.modules["_bma_med_k_anon_private"] = module
        spec.loader.exec_module(module)
        return module.assert_no_individual_fields  # type: ignore[no-any-return]
    raise ImportError(
        "could not locate /Users/dev/bma-med/security/k_anon.py — set "
        "BMA_MED_PATH env or add the bma-med repo as a sibling directory."
    )


assert_no_individual_fields = _load_assert_no_individual_fields()


class _Repository(Protocol):
    """Structural type for the slice of MVRepository we depend on.

    The real ``MVRepository`` lives at
    ``bma_health_db/repositories/mv_repository.py`` (per ADR-01 §5);
    this Protocol lets us DI a mock without importing it directly.
    """

    async def run_query(
        self,
        query_id: str,
        params: Dict[str, Any],
    ) -> List[Dict[str, Any]]: ...  # pragma: no cover


class ChartService:
    """Stateless orchestrator. Construct once per request (or per app)."""

    def __init__(
        self,
        registry: ChartRegistry,
        repo: _Repository,
    ) -> None:
        self._registry = registry
        self._repo = repo

    async def render(
        self,
        spec_id: str,
        filters: Dict[str, Any],
    ) -> ChartResponse:
        """Look up the spec, run the query, apply k-anon, project the rows.

        Steps mirror ADR-01 §3 + §8:
            1. registry.get(spec_id)
            2. validate filter keys ⊆ accepted keys
            3. await repo.run_query(spec.query_id, query_params + filters)
            4. _apply_k_anon → drop / mask
            5. project to ChartDataRow per spec.axes
            6. build ChartMeta
            7. assert_no_individual_fields(rows) — DEFENSE IN DEPTH
            8. return ChartResponse
        """
        spec: ChartSpec = self._registry.get(spec_id)

        # 2. accept-set validation. Reject unknown filter keys outright.
        accepted = {p.name for p in spec.accepts}
        unknown = set(filters) - accepted
        if unknown:
            raise ValueError(
                f"chart {spec_id!r} does not accept filters "
                f"{sorted(unknown)}; allowed: {sorted(accepted)}"
            )
        missing = {
            p.name for p in spec.accepts if p.required and p.name not in filters
        }
        if missing:
            raise ValueError(
                f"chart {spec_id!r} requires filter(s) {sorted(missing)}"
            )

        # 3. repo call. Static query_params come first; user filters
        # override (e.g. spec wants `year=2024` but caller supplied 2025).
        merged = {**spec.query_params, **filters}
        raw_rows = await self._repo.run_query(spec.query_id, merged)

        # Normalize at the boundary: repos return Pydantic row models for
        # column-name validation, but the rest of the pipeline operates
        # on plain dicts (k-anon, projection, defense-in-depth check). Do
        # the conversion here so neither layer has to know about the
        # other's representation.
        rows: List[Dict[str, Any]] = [
            r.model_dump() if hasattr(r, "model_dump") else dict(r)
            for r in raw_rows
        ]

        # 4. k-anonymity defense in depth. We use `drop` strategy by
        # default; specs may opt into `mask` via `extra={"k_anon_strategy":
        # "mask"}` — see ADR-01 §1's `extra` escape hatch.
        strategy_raw = spec.extra.get("k_anon_strategy", "drop")
        if strategy_raw not in ("drop", "mask"):
            raise ValueError(
                f"spec {spec_id!r}: extra.k_anon_strategy must be 'drop' or "
                f"'mask', got {strategy_raw!r}"
            )
        n_total = len(rows)
        kept_rows, dropped = _apply_k_anon(
            rows,
            threshold=spec.k_anon_threshold,
            strategy=strategy_raw,
            count_field=spec.count_field,
        )

        # 7. defense in depth — fail loud if any row leaks an
        # individual-level field name. MUST run BEFORE projection so it
        # catches anything, even values we wouldn't have plotted.
        assert_no_individual_fields(kept_rows)

        # 5. project to ChartDataRow shape per spec.axes.
        data: List[ChartDataRow] = [
            self._project_row(r, spec) for r in kept_rows
        ]

        # 6. assemble the meta block.
        meta = ChartMeta(
            n_total=n_total,
            k_anon_threshold=spec.k_anon_threshold,
            k_anon_dropped=dropped,
            filters_applied=dict(filters),
        )

        return ChartResponse(
            kind=spec.kind,
            spec_id=spec.spec_id,
            data=data,
            meta=meta,
        )

    @staticmethod
    def _project_row(row: Dict[str, Any], spec: ChartSpec) -> ChartDataRow:
        """Pull (x, y, n, series, masked) out of a repo row per axes spec."""
        x_key, y_key = spec.axes.x, spec.axes.y
        if x_key not in row:
            raise ValueError(
                f"spec {spec.spec_id!r}: row missing axes.x field {x_key!r}"
            )
        n = row.get(spec.count_field)
        if n is None:
            raise ValueError(
                f"spec {spec.spec_id!r}: row missing required count_field "
                f"{spec.count_field!r}; row keys: {list(row)}"
            )
        y_raw = row.get(y_key)
        # mask sentinel: k_anon_filter(strategy='mask') replaces n with
        # f"<{threshold}" and zeroes other numerics.
        masked = isinstance(n, str) and n.startswith("<")
        n_int = 0 if masked else int(n)
        y_val: Optional[float] = None if y_raw is None else float(y_raw)
        series_val: Optional[str] = None
        if spec.axes.series is not None:
            s = row.get(spec.axes.series)
            series_val = None if s is None else str(s)
        return ChartDataRow(
            x=row[x_key],
            y=y_val,
            n=n_int,
            series=series_val,
            masked=masked,
        )


def _apply_k_anon(
    rows: List[Dict[str, Any]],
    threshold: int,
    strategy: str = "drop",
    count_field: str = "n",
) -> Tuple[List[Dict[str, Any]], int]:
    """Apply k-anonymity to ``rows`` and report how many were dropped.

    Returns a ``(filtered_rows, dropped_count)`` tuple. This is a thin,
    side-effect-free wrapper around :func:`bma_med.security.k_anon.k_anon_filter`
    that additionally tracks the drop count (the underlying helper does
    not).

    The return type is left as ``list[dict]`` (not ``list[ChartDataRow]``)
    on purpose: ``assert_no_individual_fields`` runs against the raw
    repo-shaped rows, before projection.
    """
    if threshold < 1:
        raise ValueError(f"threshold must be >= 1, got {threshold}")
    if strategy not in ("drop", "mask"):
        raise ValueError(
            f"strategy must be 'drop' or 'mask', got {strategy!r}"
        )

    out: List[Dict[str, Any]] = []
    dropped = 0
    mask_token = f"<{threshold}"
    for i, row in enumerate(rows):
        if count_field not in row:
            raise ValueError(
                f"row {i} missing count_field {count_field!r}: "
                f"keys={list(row)}"
            )
        n = row[count_field]
        if n is None or n < threshold:
            if strategy == "drop":
                dropped += 1
                continue
            # mask: keep the row but blank numeric aggregates other than
            # the count itself, replace count with the threshold sentinel.
            masked_row = dict(row)
            masked_row[count_field] = mask_token
            for k, v in row.items():
                if k == count_field:
                    continue
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    masked_row[k] = None
            out.append(masked_row)
            dropped += 1  # mask-vs-drop is a strategy choice; either way
            #              the cell was below threshold, so we count it.
        else:
            out.append(dict(row))
    return out, dropped
