"""S11 — ``forest_plot`` block regression tests.

Pins the coefficient + 95% CI visualisation block introduced in Sprint
S11 ("PhD-grade Whitepaper"). The block has no DB access — its
``collect`` is a sort + truncate over caller-supplied rows — so every
test runs DB-free.

Coverage (≥6 tests per Agent B brief):
    1. Happy path — three rows + LaTeX includegraphics.
    2. Empty rows — ``ไม่มีข้อมูล`` figure, no crash.
    3. All-significant case — q-value annotations rendered.
    4. Truncation — > 25 rows yields a warning + 25 kept.
    5. HTML output — base64 image + ``<figure>`` and parses with lxml.
    6. LaTeX output — contains ``\\includegraphics{generated/...}``-style
       absolute path + ``\\caption{}``.
    7. Sort orderings — estimate / p_value / label all behave.
    8. Audience target — ``RESEARCHER`` constant pinned.
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

from services.reports.blocks.base import AudienceTarget  # noqa: E402
from services.reports.blocks.forest_plot import (  # noqa: E402
    ForestPlotBlock,
    _ForestPlotParams,
    _significance_stars,
    _sort_rows,
    render_forest_to_png,
)
from services.reports.spec import (  # noqa: E402
    RenderContext,
    ReportDescriptor,
    SectionSpec,
)

try:
    import matplotlib  # noqa: F401
    _HAS_MPL = True
except ImportError:  # pragma: no cover — runtime dep
    _HAS_MPL = False

try:
    import lxml.html  # noqa: F401
    _HAS_LXML = True
except ImportError:  # pragma: no cover — runtime dep
    _HAS_LXML = False


# ---------------------------------------------------------------------------
# Test infra
# ---------------------------------------------------------------------------


class _FakeDataCollector:
    def data(self) -> Dict[str, Any]:
        return {}


def _ctx(lang: str = "th", fmt: str = "latex") -> RenderContext:
    desc = ReportDescriptor(
        report_id="t",
        title_th="t",
        title_en="t",
        formats=["html", "latex"],
        languages=["th", "en"],
        sections=[SectionSpec(id="s1", block="forest_plot")],
    )
    return RenderContext(
        data_collector=_FakeDataCollector(),
        lang=lang,
        fmt=fmt,
        descriptor=desc,
        requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )


def _three_rows() -> List[Dict[str, Any]]:
    return [
        {
            "label": "Smoking",
            "estimate": 1.74,
            "ci_lo": 1.20,
            "ci_hi": 2.53,
            "p_value": 0.004,
            "q_value": 0.012,
            "n": 4321,
        },
        {
            "label": "Exercise",
            "estimate": 0.65,
            "ci_lo": 0.45,
            "ci_hi": 0.94,
            "p_value": 0.022,
            "q_value": 0.045,
            "n": 4321,
        },
        {
            "label": "Age (per decade)",
            "estimate": 1.30,
            "ci_lo": 1.18,
            "ci_hi": 1.43,
            "p_value": 1e-6,
            "q_value": 1e-5,
            "n": 4321,
        },
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_audience_target_is_researcher():
    """The forest plot is calibrated for the RESEARCHER persona."""
    assert ForestPlotBlock.audience_target is AudienceTarget.RESEARCHER
    assert ForestPlotBlock.block_id == "forest_plot"


def test_significance_stars_thresholds():
    """``q < 0.05 / 0.01 / 0.001`` → ``*`` / ``**`` / ``***``."""
    assert _significance_stars(None) == ""
    assert _significance_stars(0.10) == ""
    assert _significance_stars(0.049) == "*"
    assert _significance_stars(0.009) == "**"
    assert _significance_stars(0.0009) == "***"
    # raw 0 still gets ***; this is consistent with q < 0.001.
    assert _significance_stars(0.0) == "***"


@pytest.mark.skipif(not _HAS_MPL, reason="matplotlib not available")
@pytest.mark.anyio
async def test_happy_path_latex_includegraphics():
    """Happy-path: 3 rows → LaTeX figure with absolute PNG path + caption."""
    block = ForestPlotBlock()
    params = _ForestPlotParams(
        rows=_three_rows(),
        metric_name="Odds Ratio",
        null_value=1.0,
        log_scale=True,
        caption_th="OR สำหรับปัจจัยเสี่ยง",
    )
    ctx = _ctx()
    data = await block.collect(ctx, params)
    assert len(data["rows"]) == 3
    latex = block.render_latex(data, params, ctx)
    # Must contain \includegraphics with an absolute path and \caption{}.
    assert r"\includegraphics" in latex
    assert r"\caption{" in latex
    assert r"\label{fig:forest_" in latex
    # Sanity-check the path exists as an actual PNG file.
    import re
    m = re.search(r"\\includegraphics\[[^\]]*\]\{([^}]+)\}", latex)
    assert m is not None
    png_path = Path(m.group(1))
    assert png_path.is_absolute()
    assert png_path.exists()
    # PNG magic bytes — confirms matplotlib actually wrote a PNG.
    assert png_path.read_bytes()[:4] == b"\x89PNG"


@pytest.mark.skipif(not _HAS_MPL, reason="matplotlib not available")
@pytest.mark.anyio
async def test_empty_rows_renders_no_data_figure():
    """Empty rows → 'ไม่มีข้อมูล' caption inside a figure, no crash."""
    block = ForestPlotBlock()
    params = _ForestPlotParams(rows=[], metric_name="Odds Ratio")
    ctx = _ctx()
    data = await block.collect(ctx, params)
    assert data["rows"] == []
    latex = block.render_latex(data, params, ctx)
    # Empty path still emits a figure (with the no-data PNG).
    assert r"\begin{figure}" in latex
    assert r"\includegraphics" in latex
    # And HTML.
    html_out = block.render_html(data, params, ctx)
    assert "<figure" in html_out
    assert "data:image/png;base64," in html_out


@pytest.mark.skipif(not _HAS_MPL, reason="matplotlib not available")
@pytest.mark.anyio
async def test_all_significant_q_values_annotate():
    """When every row carries a ``q_value < 0.05`` the figure renders
    without raising — annotation chars themselves are inside the PNG so
    we just confirm the data dict captures the q_values verbatim."""
    block = ForestPlotBlock()
    rows = _three_rows()
    # Force them to be significant with stars at all three levels.
    rows[0]["q_value"] = 0.04   # *
    rows[1]["q_value"] = 0.005  # **
    rows[2]["q_value"] = 0.0005  # ***
    params = _ForestPlotParams(rows=rows, metric_name="Odds Ratio")
    ctx = _ctx()
    data = await block.collect(ctx, params)
    assert all(r["q_value"] is not None for r in data["rows"])
    # Render should not crash.
    latex = block.render_latex(data, params, ctx)
    assert r"\caption{" in latex


@pytest.mark.skipif(not _HAS_MPL, reason="matplotlib not available")
@pytest.mark.anyio
async def test_truncation_warns_above_max_rows():
    """> 25 rows → kept = 25, n_dropped > 0, footer note in LaTeX."""
    block = ForestPlotBlock()
    rows = []
    for i in range(40):
        rows.append(
            {
                "label": f"Var{i:02d}",
                "estimate": 1.0 + 0.01 * i,
                "ci_lo": 0.9 + 0.01 * i,
                "ci_hi": 1.1 + 0.01 * i,
            }
        )
    params = _ForestPlotParams(
        rows=rows, metric_name="OR", sort_by="input"
    )
    ctx = _ctx()
    data = await block.collect(ctx, params)
    assert len(data["rows"]) == 25
    assert data["n_dropped"] == 15
    latex = block.render_latex(data, params, ctx)
    # Truncation note is present (Thai prose).
    assert "ตัดออก" in latex


@pytest.mark.skipif(not (_HAS_MPL and _HAS_LXML), reason="needs mpl + lxml")
@pytest.mark.anyio
async def test_html_output_parses_with_lxml():
    """HTML output is structurally valid and contains base64 PNG."""
    import lxml.html

    block = ForestPlotBlock()
    params = _ForestPlotParams(
        rows=_three_rows(),
        metric_name="Odds Ratio",
        log_scale=True,
        caption_en="Forest plot",
    )
    ctx = _ctx(lang="en", fmt="html")
    data = await block.collect(ctx, params)
    html_out = block.render_html(data, params, ctx)
    tree = lxml.html.fromstring(html_out)
    assert tree.tag == "figure"
    assert "forest-plot" in (tree.get("class") or "")
    img = tree.find("img")
    assert img is not None
    assert img.get("src", "").startswith("data:image/png;base64,")
    figcap = tree.find("figcaption")
    assert figcap is not None
    assert "Forest" in (figcap.text or "")


def test_sort_by_estimate_orders_descending():
    """Default ``estimate`` sort puts biggest effect at the top."""
    rows = [
        {"label": "A", "estimate": 1.10, "ci_lo": 0.9, "ci_hi": 1.30},
        {"label": "B", "estimate": 2.50, "ci_lo": 1.5, "ci_hi": 4.10},
        {"label": "C", "estimate": 0.50, "ci_lo": 0.3, "ci_hi": 0.80},
    ]
    sorted_rows = _sort_rows(rows, "estimate")
    labels = [r["label"] for r in sorted_rows]
    assert labels == ["B", "A", "C"]


def test_sort_by_label_orders_alphabetically():
    """``label`` sort produces a deterministic alphabetical order."""
    rows = [
        {"label": "Banana", "estimate": 1.1, "ci_lo": 0.9, "ci_hi": 1.3},
        {"label": "Apple", "estimate": 2.5, "ci_lo": 1.5, "ci_hi": 4.1},
        {"label": "Carrot", "estimate": 0.5, "ci_lo": 0.3, "ci_hi": 0.8},
    ]
    sorted_rows = _sort_rows(rows, "label")
    labels = [r["label"] for r in sorted_rows]
    assert labels == ["Apple", "Banana", "Carrot"]


def test_sort_by_p_value_ascending_with_nan_last():
    """``p_value`` sort: most-significant first, NaN p-values to the end."""
    rows = [
        {"label": "A", "estimate": 1.1, "ci_lo": 0.9, "ci_hi": 1.3, "p_value": 0.05},
        {"label": "B", "estimate": 2.5, "ci_lo": 1.5, "ci_hi": 4.1, "p_value": None},
        {"label": "C", "estimate": 0.5, "ci_lo": 0.3, "ci_hi": 0.8, "p_value": 0.001},
    ]
    sorted_rows = _sort_rows(rows, "p_value")
    labels = [r["label"] for r in sorted_rows]
    assert labels == ["C", "A", "B"]


@pytest.mark.skipif(not _HAS_MPL, reason="matplotlib not available")
def test_render_to_png_returns_existing_file():
    """The pure renderer can be used directly without going through the
    block (LogisticRegressionBlock relies on this)."""
    out = render_forest_to_png(
        _three_rows(),
        "Odds Ratio",
        null_value=1.0,
        log_scale=True,
    )
    assert out.exists()
    assert out.suffix == ".png"
    # PNG magic bytes.
    assert out.read_bytes()[:4] == b"\x89PNG"


@pytest.mark.skipif(not _HAS_MPL, reason="matplotlib not available")
@pytest.mark.anyio
async def test_missing_estimate_renders_dash_marker():
    """A row with NaN estimate still gets a label slot but no whisker."""
    block = ForestPlotBlock()
    rows = [
        {"label": "Good", "estimate": 1.5, "ci_lo": 1.1, "ci_hi": 2.0},
        {"label": "Missing", "estimate": float("nan"),
         "ci_lo": float("nan"), "ci_hi": float("nan")},
    ]
    params = _ForestPlotParams(rows=rows, metric_name="OR", sort_by="input")
    ctx = _ctx()
    data = await block.collect(ctx, params)
    assert len(data["rows"]) == 2
    # Renders without throwing.
    latex = block.render_latex(data, params, ctx)
    assert r"\includegraphics" in latex


@pytest.mark.skipif(not _HAS_MPL, reason="matplotlib not available")
@pytest.mark.anyio
async def test_caption_falls_back_to_metric_when_unset():
    """No caption_th / caption_en → the figure caption is auto-generated."""
    block = ForestPlotBlock()
    params = _ForestPlotParams(
        rows=_three_rows(),
        metric_name="Risk Ratio",
    )
    # Thai
    ctx_th = _ctx(lang="th")
    data = await block.collect(ctx_th, params)
    latex = block.render_latex(data, params, ctx_th)
    assert "Risk Ratio" in latex
    # English
    ctx_en = _ctx(lang="en")
    html_out = block.render_html(data, params, ctx_en)
    assert "Risk Ratio" in html_out
