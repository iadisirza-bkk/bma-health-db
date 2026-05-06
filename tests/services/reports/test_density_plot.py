"""Sprint S11 — DensityPlotBlock tests.

Coverage:
    * Happy path: BMI distribution by sex with WHO Asian cut-offs
    * Empty data → graceful empty figure
    * Single value (zero variance) → graceful spike rendering
    * Stratify_by None → single curve
    * Stratify_by populated → one curve per stratum
    * Reference ranges shown when supplied
    * Filters non-numeric / NaN values out
    * HTML output parses cleanly via lxml
    * LaTeX output contains ``\\includegraphics{...}``
    * audience_target = None (any audience)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# Make ``api/`` importable for ``services.reports.*``.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

try:
    import matplotlib  # noqa: F401
    _HAS_MPL = True
except ImportError:  # pragma: no cover
    _HAS_MPL = False

from services.reports.blocks.density_plot import DensityPlotBlock  # noqa: E402
from services.reports.spec import (  # noqa: E402
    RenderContext,
    ReportDescriptor,
    SectionSpec,
)


pytestmark = pytest.mark.skipif(not _HAS_MPL, reason="matplotlib required")


# ---------------------------------------------------------------------------
# Test infra
# ---------------------------------------------------------------------------


class _FakeDataCollector:
    def data(self) -> Dict[str, Any]:
        return {}


def _ctx(extra: Optional[Dict[str, Any]] = None, lang: str = "th") -> RenderContext:
    desc = ReportDescriptor(
        report_id="t",
        title_th="t",
        title_en="t",
        formats=["html", "latex"],
        languages=["th", "en"],
        sections=[SectionSpec(id="s1", block="cover_page")],
    )
    return RenderContext(
        data_collector=_FakeDataCollector(),
        lang=lang,
        fmt="latex",
        descriptor=desc,
        requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        extra=extra or {},
    )


def _bmi_rows() -> List[Dict[str, Any]]:
    """Synthetic BMI by sex — male centered ~24, female centered ~22."""
    import numpy as np

    rng = np.random.default_rng(42)
    rows: List[Dict[str, Any]] = []
    for v in rng.normal(loc=24.0, scale=3.5, size=120):
        rows.append({"bmi": float(v), "sex": "M"})
    for v in rng.normal(loc=22.5, scale=3.0, size=120):
        rows.append({"bmi": float(v), "sex": "F"})
    return rows


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_density_happy_path_with_who_bmi_cutoffs() -> None:
    block = DensityPlotBlock()
    rows = _bmi_rows()
    params = block.Parameters(
        column="bmi",
        stratify_by="sex",
        reference_ranges=[
            (23.0, 25.0, "overweight"),
            (25.0, 30.0, "obese I"),
            (30.0, 50.0, "obese II"),
        ],
        caption_th="BMI by sex",
    )
    ctx = _ctx(extra={"density_rows": rows})
    data = await block.collect(ctx, params)
    assert data["n"] == 240
    assert set(data["strata"].keys()) == {"M", "F"}
    assert len(data["reference_ranges"]) == 3
    latex = block.render_latex(data, params, ctx)
    assert r"\includegraphics" in latex
    assert ".png}" in latex


@pytest.mark.anyio
async def test_density_no_stratify_yields_single_curve() -> None:
    block = DensityPlotBlock()
    rows = [{"bmi": 22.0 + i * 0.1} for i in range(100)]
    params = block.Parameters(column="bmi")
    ctx = _ctx(extra={"density_rows": rows})
    data = await block.collect(ctx, params)
    assert list(data["strata"].keys()) == ["all"]
    assert data["n"] == 100


# ---------------------------------------------------------------------------
# Empty / degenerate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_density_empty_data_renders_gracefully() -> None:
    block = DensityPlotBlock()
    params = block.Parameters(column="bmi")
    ctx = _ctx(extra={"density_rows": []})
    data = await block.collect(ctx, params)
    assert data["n"] == 0
    latex = block.render_latex(data, params, ctx)
    assert r"\includegraphics" in latex


@pytest.mark.anyio
async def test_density_single_value_zero_variance_safe() -> None:
    """All values identical — zero-variance fallback should produce a spike, not crash."""
    block = DensityPlotBlock()
    rows = [{"bmi": 22.5} for _ in range(20)]
    params = block.Parameters(column="bmi")
    ctx = _ctx(extra={"density_rows": rows})
    data = await block.collect(ctx, params)
    assert data["n"] == 20
    latex = block.render_latex(data, params, ctx)
    # Should NOT crash — produces a figure either way.
    assert r"\includegraphics" in latex


@pytest.mark.anyio
async def test_density_drops_non_numeric_and_nan() -> None:
    block = DensityPlotBlock()
    rows: List[Dict[str, Any]] = [
        {"bmi": 22.0},
        {"bmi": float("nan")},
        {"bmi": "not a number"},
        {"bmi": None},
        {"bmi": 25.0},
        {"bmi": 24.5},
    ]
    params = block.Parameters(column="bmi")
    ctx = _ctx(extra={"density_rows": rows})
    data = await block.collect(ctx, params)
    assert data["n"] == 3  # only 22.0, 25.0, 24.5


@pytest.mark.anyio
async def test_density_single_value_renders_empty_state_for_n_lt_2() -> None:
    """n=1 → too few values to KDE; render the empty path."""
    block = DensityPlotBlock()
    rows = [{"bmi": 22.0}]
    params = block.Parameters(column="bmi")
    ctx = _ctx(extra={"density_rows": rows})
    data = await block.collect(ctx, params)
    assert data["n"] == 1
    latex = block.render_latex(data, params, ctx)
    assert r"\includegraphics" in latex


# ---------------------------------------------------------------------------
# Reference ranges
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_density_reference_ranges_propagate_to_payload() -> None:
    block = DensityPlotBlock()
    rows = _bmi_rows()
    params = block.Parameters(
        column="bmi",
        reference_ranges=[(23.0, 25.0, "OW"), (25.0, 30.0, "OBI"), (30.0, 60.0, "OBII")],
    )
    ctx = _ctx(extra={"density_rows": rows})
    data = await block.collect(ctx, params)
    assert len(data["reference_ranges"]) == 3
    assert data["reference_ranges"][0][2] == "OW"


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_density_html_parses_cleanly_via_lxml() -> None:
    pytest.importorskip("lxml")
    from lxml import etree

    block = DensityPlotBlock()
    rows = _bmi_rows()
    params = block.Parameters(column="bmi", stratify_by="sex", caption_th="BMI by sex")
    ctx = _ctx(extra={"density_rows": rows})
    data = await block.collect(ctx, params)
    html = block.render_html(data, params, ctx)
    root = etree.fromstring(f"<root>{html}</root>")
    assert root.find(".//figure") is not None
    img = root.find(".//img")
    assert img is not None


@pytest.mark.anyio
async def test_density_latex_contains_includegraphics_to_generated() -> None:
    block = DensityPlotBlock()
    rows = _bmi_rows()
    params = block.Parameters(column="bmi", stratify_by="sex")
    ctx = _ctx(extra={"density_rows": rows})
    data = await block.collect(ctx, params)
    latex = block.render_latex(data, params, ctx)
    assert r"\includegraphics" in latex
    import re
    m = re.search(r"\\includegraphics\[[^\]]*\]\{([^}]+\.png)\}", latex)
    assert m is not None
    assert Path(m.group(1)).exists()


# ---------------------------------------------------------------------------
# Audience-neutral + provider injection
# ---------------------------------------------------------------------------


def test_density_block_targets_any_audience() -> None:
    """audience_target=None means the block renders in any audience."""
    assert DensityPlotBlock.audience_target is None


@pytest.mark.anyio
async def test_density_provider_callable_path() -> None:
    rows = _bmi_rows()

    async def fake_provider(column: str, stratify_by: Optional[str], filters: Dict[str, Any]):
        assert column == "bmi"
        assert stratify_by == "sex"
        return rows

    block = DensityPlotBlock()
    params = block.Parameters(column="bmi", stratify_by="sex")
    ctx = _ctx(extra={"density_provider": fake_provider})
    data = await block.collect(ctx, params)
    assert data["n"] == len(rows)
