"""Tests for ``services.reports.blocks.choropleth_block`` (S11).

Pin the contract that ``ChoroplethBlock`` produces a PNG file at zone
and district resolution and that the LaTeX / HTML renderers wrap it in
the expected envelope. matplotlib is a hard dep of the project so we
don't bother gating these tests behind an availability check.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

# Make ``api/`` importable for ``services.reports.*``.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.reports.blocks.choropleth_block import ChoroplethBlock  # noqa: E402
from services.reports.spec import (  # noqa: E402
    RenderContext,
    ReportDescriptor,
    SectionSpec,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _resp(rows: List[Dict[str, Any]]):
    """Mimic a ``ChartResponse`` enough for the block's ``_response_to_dict``."""
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
        sections=[SectionSpec(id="s1", block="choropleth")],
    )
    return RenderContext(
        data_collector=_FakeDataCollector(),
        lang=lang,
        fmt="html",
        descriptor=desc,
        requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        extra=extra or {},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_zone_choropleth_writes_png_and_8_units() -> None:
    """8-zone input → ``png_path`` exists on disk + ``n=8`` in the payload."""
    rows = [
        {"zone": str(z), "y": float(z) * 0.1, "n": 100}
        for z in range(1, 9)
    ]
    fake = _resp(rows)
    block = ChoroplethBlock()
    params = block.Parameters(
        outcome_spec_id="zone_demo",
        geographic_unit="zone",
        value_unit="% prevalence",
    )
    ctx = _ctx(extra={"chart_service": fake})
    data = await block.collect(ctx, params)
    assert not data["skipped"]
    assert data["n"] == 8
    assert data["geographic_unit"] == "zone"
    p = Path(data["png_path"])
    assert p.exists(), f"PNG not written at {p}"
    assert p.suffix == ".png"
    # File should be non-trivially-sized (at least a few KB for a real
    # matplotlib render).
    assert p.stat().st_size > 1000


@pytest.mark.anyio
async def test_district_choropleth_writes_png_for_50_units() -> None:
    """50-district input → block lays out a 5×10 grid PNG."""
    rows = [
        {"district_code": f"10{n:02d}", "y": float(n) / 50, "n": 100}
        for n in range(1, 51)
    ]
    fake = _resp(rows)
    block = ChoroplethBlock()
    params = block.Parameters(
        outcome_spec_id="district_demo",
        geographic_unit="district",
    )
    ctx = _ctx(extra={"chart_service": fake})
    data = await block.collect(ctx, params)
    assert not data["skipped"]
    assert data["n"] == 50
    p = Path(data["png_path"])
    assert p.exists()


@pytest.mark.anyio
async def test_empty_data_skips_with_friendly_reason() -> None:
    """Zero rows from chart-service → block sets ``skipped=True``."""
    fake = _resp([])
    block = ChoroplethBlock()
    params = block.Parameters(
        outcome_spec_id="empty",
        geographic_unit="zone",
    )
    ctx = _ctx(extra={"chart_service": fake})
    data = await block.collect(ctx, params)
    assert data["skipped"]
    assert "no spatial-unit data" in str(data["skip_reason"])
    assert data["png_path"] is None


@pytest.mark.anyio
async def test_render_latex_includegraphics_with_absolute_path() -> None:
    """LaTeX path must reference the absolute PNG path verbatim."""
    rows = [
        {"zone": str(z), "y": float(z), "n": 100}
        for z in range(1, 9)
    ]
    fake = _resp(rows)
    block = ChoroplethBlock()
    params = block.Parameters(
        outcome_spec_id="caption_demo",
        geographic_unit="zone",
        caption_th="ภาพการกระจาย",
    )
    ctx = _ctx(extra={"chart_service": fake})
    data = await block.collect(ctx, params)
    latex = block.render_latex(data, params, ctx)
    assert r"\begin{figure}" in latex
    assert r"\includegraphics" in latex
    assert data["png_path"] in latex
    assert r"\caption{" in latex


@pytest.mark.anyio
async def test_render_html_emits_img_tag() -> None:
    """HTML path emits ``<img src=...>`` (file:// for absolute paths)."""
    rows = [
        {"zone": str(z), "y": float(z), "n": 100}
        for z in range(1, 9)
    ]
    fake = _resp(rows)
    block = ChoroplethBlock()
    params = block.Parameters(
        outcome_spec_id="html_demo",
        geographic_unit="zone",
    )
    ctx = _ctx(extra={"chart_service": fake})
    data = await block.collect(ctx, params)
    html = block.render_html(data, params, ctx)
    assert "<figure" in html
    assert "<img" in html
    assert "html_demo" in html


@pytest.mark.anyio
async def test_render_latex_skipped_emits_placeholder_figure() -> None:
    """When ``collect`` skipped, LaTeX still emits a figure environment
    so the surrounding template's ordering doesn't break."""
    fake = _resp([])
    block = ChoroplethBlock()
    params = block.Parameters(
        outcome_spec_id="empty_demo",
        geographic_unit="zone",
    )
    ctx = _ctx(extra={"chart_service": fake})
    data = await block.collect(ctx, params)
    latex = block.render_latex(data, params, ctx)
    assert r"\begin{figure}" in latex
    assert r"\end{figure}" in latex
