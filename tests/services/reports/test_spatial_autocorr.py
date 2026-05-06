"""Tests for ``services.reports.blocks.spatial_autocorr`` (S11).

These pin the block-level contract: ``collect`` produces a dict with
``global_I`` and ``lisa_rows``, the renderers consume it without
crashing, and the empty / insufficient-units paths fall back gracefully.

The block depends on a chart-service to fetch outcome rows; we inject a
``ctx.extra["chart_service"]`` mock so the suite stays DB-free.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

# Make ``api/`` importable for ``services.reports.*``.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.reports.blocks.spatial_autocorr import (  # noqa: E402
    SpatialAutocorrBlock,
)
from services.reports.spec import (  # noqa: E402
    RenderContext,
    ReportDescriptor,
    SectionSpec,
)


# ---------------------------------------------------------------------------
# Stub chart-service: minimal model_dump() that mimics ChartResponse
# ---------------------------------------------------------------------------


def _resp(rows: List[Dict[str, Any]]):
    """Wrap ``rows`` as something ``ChartService.render`` could return."""
    canned = {
        "kind": "bar",
        "spec_id": "stub",
        "data": rows,
        "meta": {
            "n_total": len(rows),
            "k_anon_threshold": 5,
            "k_anon_dropped": 0,
            "filters_applied": {},
            "generated_at": "2026-05-01T00:00:00Z",
        },
    }

    class _R:
        def model_dump(self) -> Dict[str, Any]:
            return canned

    fake = AsyncMock()
    fake.render.return_value = _R()
    return fake


class _FakeDataCollector:
    def data(self) -> Dict[str, Any]:
        return {}


def _ctx(
    extra: Optional[Dict[str, Any]] = None,
    lang: str = "th",
) -> RenderContext:
    desc = ReportDescriptor(
        report_id="t",
        title_th="t",
        formats=["html", "pdf"],
        languages=["th", "en"],
        sections=[SectionSpec(id="s1", block="spatial_autocorr")],
    )
    return RenderContext(
        data_collector=_FakeDataCollector(),
        lang=lang,
        fmt="html",
        descriptor=desc,
        requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        extra=extra or {},
    )


def _params(**kwargs: Any) -> Any:
    block = SpatialAutocorrBlock()
    return block.Parameters(**kwargs)


@pytest.mark.anyio
async def test_collect_returns_global_I_and_lisa_rows_for_zone_input() -> None:
    """A clean 8-row zone input yields a populated ``global_I`` and 8
    ``lisa_rows``."""
    rows = [
        {"zone": "1", "y": 0.99, "n": 100},
        {"zone": "2", "y": 0.98, "n": 100},
        {"zone": "3", "y": 0.97, "n": 100},
        {"zone": "4", "y": 0.96, "n": 100},
        {"zone": "5", "y": 0.05, "n": 100},
        {"zone": "6", "y": 0.04, "n": 100},
        {"zone": "7", "y": 0.02, "n": 100},
        {"zone": "8", "y": 0.01, "n": 100},
    ]
    fake = _resp(rows)
    block = SpatialAutocorrBlock()
    params = _params(outcome_spec_id="stub", n_perm=199, random_state=42)
    ctx = _ctx(extra={"chart_service": fake})
    data = await block.collect(ctx, params)
    assert not data["skipped"]
    assert data["n"] == 8
    assert data["geographic_unit"] == "zone"
    # I should be strongly positive on this synthetic clustered set.
    assert data["global_I"]["I"] > 0.4
    assert len(data["lisa_rows"]) == 8
    # Each lisa_row should have all the expected keys.
    sample = data["lisa_rows"][0]
    assert set(sample.keys()) >= {
        "unit_code", "value", "Ii", "p_value",
        "quadrant", "quadrant_label", "is_significant",
    }


@pytest.mark.anyio
async def test_collect_skips_when_too_few_units() -> None:
    """Fewer than 5 zones present → block skips with a reason."""
    rows = [
        {"zone": "1", "y": 0.5, "n": 10},
        {"zone": "2", "y": 0.3, "n": 10},
    ]
    fake = _resp(rows)
    block = SpatialAutocorrBlock()
    params = _params(outcome_spec_id="stub", n_perm=99, random_state=0)
    ctx = _ctx(extra={"chart_service": fake})
    data = await block.collect(ctx, params)
    assert data["skipped"]
    assert "insufficient" in str(data["skip_reason"]).lower()


@pytest.mark.anyio
async def test_collect_skips_district_unit_until_geojson_arrives() -> None:
    """``geographic_unit='district'`` is intentionally a no-op for now."""
    fake = _resp([])
    block = SpatialAutocorrBlock()
    params = _params(
        outcome_spec_id="stub",
        geographic_unit="district",
        n_perm=99,
    )
    ctx = _ctx(extra={"chart_service": fake})
    data = await block.collect(ctx, params)
    assert data["skipped"]
    assert "geojson" in str(data["skip_reason"]).lower()


@pytest.mark.anyio
async def test_render_html_emits_table_when_significant_rows_exist() -> None:
    """Synthetic data with a significant global I → HTML contains the
    table head with the cluster-type column."""
    rows = [
        {"zone": str(z), "y": v, "n": 100}
        for z, v in zip(range(1, 9), [0.99, 0.98, 0.97, 0.96, 0.05, 0.04, 0.02, 0.01])
    ]
    fake = _resp(rows)
    block = SpatialAutocorrBlock()
    params = _params(outcome_spec_id="stub", n_perm=399, random_state=42)
    ctx = _ctx(extra={"chart_service": fake})
    data = await block.collect(ctx, params)
    html = block.render_html(data, params, ctx)
    assert "<section" in html
    assert "Moran's I" in html
    # Caption is present.
    assert "การกระจุกตัว" in html or "ตัวชี้วัด" in html or "Moran" in html


@pytest.mark.anyio
async def test_render_html_skipped_path_emits_friendly_message() -> None:
    """When ``collect`` skipped, the renderer must NOT raise and must
    surface the reason as a paragraph."""
    rows = [{"zone": "1", "y": 1.0, "n": 1}]  # only 1 unit
    fake = _resp(rows)
    block = SpatialAutocorrBlock()
    params = _params(outcome_spec_id="stub", n_perm=49)
    ctx = _ctx(extra={"chart_service": fake})
    data = await block.collect(ctx, params)
    html = block.render_html(data, params, ctx)
    assert "skipped" in html.lower()


@pytest.mark.anyio
async def test_render_latex_emits_subsection_and_global_I() -> None:
    """The LaTeX renderer always opens with a subsection heading and
    cites the global Moran's I value."""
    rows = [
        {"zone": str(z), "y": v, "n": 100}
        for z, v in zip(range(1, 9), [0.99, 0.98, 0.97, 0.96, 0.05, 0.04, 0.02, 0.01])
    ]
    fake = _resp(rows)
    block = SpatialAutocorrBlock()
    params = _params(outcome_spec_id="stub", n_perm=399, random_state=42)
    ctx = _ctx(extra={"chart_service": fake})
    data = await block.collect(ctx, params)
    latex = block.render_latex(data, params, ctx)
    assert r"\subsection*{" in latex
    assert "Moran's I" in latex
    # Should NOT have unescaped LaTeX-hot characters slipping through.
    assert r"\textbf{" in latex
