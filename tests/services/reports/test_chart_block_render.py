"""S10 — ChartBlock.render_latex regression tests.

These tests pin the new pgfplots TikZ + matplotlib-PNG fallback contract
introduced in Sprint S10 ("Template-First Reports"). They cover every
chart kind the descriptor catalog uses today, with a mocked
``ChartService`` so the test suite stays DB-free.

Coverage:
    * bar         → ``\\begin{tikzpicture}`` + ``\\addplot coordinates``
    * line        → ``\\addplot[mark=*, ...]`` line plot
    * stacked_bar → ``ybar stacked`` axis with multiple ``\\addplot`` calls
    * pyramid     → ``xbar`` mirror with two ``\\addplot`` blocks
    * donut       → fallback share-of-total xbar OR pgf-pie
    * heatmap     → matplotlib ``\\includegraphics`` (skipped if absent)
    * choropleth  → matplotlib ranked-bar fallback (skipped if absent)
    * empty rows  → ``ไม่มีข้อมูล`` figure (NOT 'rendering not available')
    * caption     → escaped + wrapped in ``\\caption{...}``
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

from services.reports.blocks import ChartBlock  # noqa: E402
from services.reports.spec import (  # noqa: E402
    RenderContext,
    ReportDescriptor,
    SectionSpec,
)

try:  # matplotlib is required at runtime; skip those tests if absent.
    import matplotlib  # noqa: F401
    _HAS_MPL = True
except ImportError:  # pragma: no cover — runtime dep
    _HAS_MPL = False


# ---------------------------------------------------------------------------
# Test infra: stub ``ReportDataCollector`` + ``RenderContext`` factory
# ---------------------------------------------------------------------------


class _FakeDataCollector:
    def data(self) -> Dict[str, Any]:
        return {}


def _ctx(extra: Optional[Dict[str, Any]] = None) -> RenderContext:
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
        lang="th",
        fmt="latex",
        descriptor=desc,
        requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        extra=extra or {},
    )


def _resp(kind: str, rows: List[Dict[str, Any]], spec_id: str = "x"):
    canned = {
        "kind": kind,
        "spec_id": spec_id,
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


async def _render(kind: str, rows: List[Dict[str, Any]], **kwargs: Any) -> str:
    block = ChartBlock()
    params = block.Parameters(spec_id=kwargs.get("spec_id", "x"), **{
        k: v for k, v in kwargs.items() if k != "spec_id"
    })
    fake = _resp(kind, rows, spec_id=kwargs.get("spec_id", "x"))
    ctx = _ctx(extra={"chart_service": fake})
    data = await block.collect(ctx, params)
    return block.render_latex(data, params, ctx)


# ---------------------------------------------------------------------------
# pgfplots-driven chart kinds
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bar_chart_emits_pgfplots_axis_and_addplot_coordinates() -> None:
    rows = [
        {"x": "บางรัก", "y": 12.5, "n": 47, "series": None, "masked": False},
        {"x": "ดุสิต", "y": 9.8, "n": 30, "series": None, "masked": False},
    ]
    latex = await _render("bar", rows, spec_id="screening_coverage")
    assert r"\begin{tikzpicture}" in latex
    assert r"\begin{axis}" in latex
    assert "ybar" in latex
    assert r"\addplot" in latex
    assert "coordinates" in latex
    # Numeric value from the fixture must appear, escaped or not.
    assert "12.5" in latex
    # No legacy placeholder text.
    assert "rendering not available" not in latex


@pytest.mark.anyio
async def test_line_chart_uses_mark_star_and_addplot() -> None:
    rows = [
        {"x": "Q1", "y": 100.0, "n": 100, "series": None, "masked": False},
        {"x": "Q2", "y": 120.0, "n": 120, "series": None, "masked": False},
        {"x": "Q3", "y": 90.0, "n": 90, "series": None, "masked": False},
    ]
    latex = await _render("line", rows, spec_id="trend_demo")
    assert r"\begin{tikzpicture}" in latex
    assert "mark=*" in latex
    assert r"\addplot" in latex


@pytest.mark.anyio
async def test_stacked_bar_emits_one_addplot_per_series() -> None:
    rows = [
        {"x": "smoke", "y": 10.0, "n": 100, "series": "DM", "masked": False},
        {"x": "smoke", "y": 5.0, "n": 50, "series": "HT", "masked": False},
        {"x": "alcohol", "y": 8.0, "n": 80, "series": "DM", "masked": False},
        {"x": "alcohol", "y": 3.0, "n": 30, "series": "HT", "masked": False},
    ]
    latex = await _render("stacked_bar", rows, spec_id="behavior_disease")
    assert r"\begin{tikzpicture}" in latex
    assert "ybar stacked" in latex
    # Two series → at least two \addplot directives.
    assert latex.count(r"\addplot") >= 2


@pytest.mark.anyio
async def test_pyramid_emits_two_addplots_on_xbar_axis() -> None:
    rows = [
        {"x": "20-29", "y": 100.0, "n": 100, "series": "10", "masked": False},
        {"x": "20-29", "y": 110.0, "n": 110, "series": "20", "masked": False},
        {"x": "30-39", "y": 80.0, "n": 80, "series": "10", "masked": False},
        {"x": "30-39", "y": 90.0, "n": 90, "series": "20", "masked": False},
    ]
    latex = await _render("pyramid", rows, spec_id="age_pyramid")
    assert r"\begin{tikzpicture}" in latex
    assert "xbar" in latex
    # One \addplot per sex → 2 calls.
    assert latex.count(r"\addplot") == 2
    # Male series should appear as a NEGATIVE coordinate so the bar
    # extends left of the y axis. A leading minus sign in coords is the
    # canonical signal.
    assert "(-100" in latex or "(-110" in latex


@pytest.mark.anyio
async def test_donut_falls_back_to_share_of_total_bar() -> None:
    rows = [
        {"x": "A", "y": 30.0, "n": 30, "series": None, "masked": False},
        {"x": "B", "y": 70.0, "n": 70, "series": None, "masked": False},
    ]
    latex = await _render("donut", rows, spec_id="donut_demo")
    # Either pgf-pie (`\pie`) OR the horizontal-bar share-of-total fallback.
    assert (r"\pie" in latex) or ("xbar" in latex and r"\addplot" in latex)


# ---------------------------------------------------------------------------
# Empty data path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_empty_rows_emit_no_data_message_not_placeholder() -> None:
    latex = await _render("bar", [], spec_id="empty_demo")
    assert "ไม่มีข้อมูล" in latex
    assert "rendering not available" not in latex
    assert r"\begin{figure}" in latex


# ---------------------------------------------------------------------------
# matplotlib PNG fallback path
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_MPL, reason="matplotlib is not installed")
@pytest.mark.anyio
async def test_choropleth_uses_matplotlib_png_fallback() -> None:
    rows = [
        {"x": "01", "y": 80.0, "n": 800, "series": None, "masked": False},
        {"x": "02", "y": 60.0, "n": 600, "series": None, "masked": False},
        {"x": "03", "y": 90.0, "n": 900, "series": None, "masked": False},
    ]
    latex = await _render("choropleth", rows, spec_id="cover_chart")
    assert r"\includegraphics" in latex
    # Ensure the PNG path embedded in the includegraphics call ends in .png
    assert ".png}" in latex


@pytest.mark.skipif(not _HAS_MPL, reason="matplotlib is not installed")
@pytest.mark.anyio
async def test_heatmap_uses_matplotlib_png_fallback() -> None:
    rows = [
        {"x": "DM", "y": 1.0, "n": 10, "series": "fbs", "masked": False},
        {"x": "HT", "y": 2.0, "n": 20, "series": "fbs", "masked": False},
        {"x": "DM", "y": 3.0, "n": 30, "series": "ldl", "masked": False},
    ]
    latex = await _render("heatmap", rows, spec_id="disease_lab_crosstab")
    assert r"\includegraphics" in latex


# ---------------------------------------------------------------------------
# Caption + escaping
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_caption_th_appears_inside_caption_block_escaped() -> None:
    rows = [
        {"x": "บางรัก", "y": 12.0, "n": 12, "series": None, "masked": False},
    ]
    latex = await _render(
        "bar", rows, spec_id="x_pct", caption_th="ภาพรวม กทม. & เขต_1"
    )
    # Caption command present.
    assert r"\caption{" in latex
    # latex_escape turns & → \&, _ → \_ — both must appear unescaped-as-LaTeX.
    assert r"\&" in latex
    assert r"\_" in latex


@pytest.mark.anyio
async def test_render_wraps_chart_in_figure_environment_with_label() -> None:
    rows = [{"x": "A", "y": 1.0, "n": 1, "series": None, "masked": False}]
    latex = await _render("bar", rows, spec_id="abc_demo", caption_th="x")
    assert r"\begin{figure}[H]" in latex
    assert r"\centering" in latex
    assert r"\end{figure}" in latex
    assert r"\label{chart:abc_demo}" in latex


# ---------------------------------------------------------------------------
# Spec-id label coverage (S10 polish iter 3)
# ---------------------------------------------------------------------------

def test_every_registered_chart_spec_has_thai_label():
    """Every chart spec in config/charts/ MUST have a SPEC_ID_LABELS_TH entry
    so reports never show raw spec_ids in figure captions."""
    from pathlib import Path
    from services.reports.blocks.chart import SPEC_ID_LABELS_TH

    config_dir = Path(__file__).resolve().parents[3] / "config" / "charts"
    if not config_dir.exists():
        import pytest
        pytest.skip(f"chart configs not at {config_dir}")
    spec_files = sorted(config_dir.glob("*.yaml"))
    spec_ids = {f.stem for f in spec_files}
    missing = spec_ids - set(SPEC_ID_LABELS_TH.keys())
    assert not missing, (
        f"chart specs missing from SPEC_ID_LABELS_TH: {missing}. "
        f"Add entries to api/services/reports/blocks/chart.py."
    )


def test_every_registered_chart_spec_has_english_label():
    """Mirror Thai-coverage check for English labels."""
    from pathlib import Path
    from services.reports.blocks.chart import SPEC_ID_LABELS_EN

    config_dir = Path(__file__).resolve().parents[3] / "config" / "charts"
    if not config_dir.exists():
        import pytest
        pytest.skip(f"chart configs not at {config_dir}")
    spec_ids = {f.stem for f in config_dir.glob("*.yaml")}
    missing = spec_ids - set(SPEC_ID_LABELS_EN.keys())
    assert not missing, (
        f"chart specs missing from SPEC_ID_LABELS_EN: {missing}"
    )


def test_every_descriptor_spec_id_has_label():
    """Mirror of test_every_registered_chart_spec_has_thai_label, but
    descriptor-side: every spec_id REFERENCED in any config/reports/*.yaml
    must have a SPEC_ID_LABELS_TH entry. Catches descriptor authors who
    introduce a new spec_id without a friendly caption fallback (which
    would surface as "Figure N: <raw_spec_id>" in the rendered PDF).
    """
    from pathlib import Path
    import yaml
    from services.reports.blocks.chart import SPEC_ID_LABELS_TH

    config_dir = Path(__file__).resolve().parents[3] / "config" / "reports"
    if not config_dir.exists():
        import pytest
        pytest.skip(f"report configs not at {config_dir}")
    spec_ids: set[str] = set()
    for yaml_file in config_dir.glob("*.yaml"):
        try:
            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        for section in (data.get("sections", []) or []):
            params = section.get("params") or {}
            sid = params.get("spec_id")
            if isinstance(sid, str):
                spec_ids.add(sid)
    missing = spec_ids - set(SPEC_ID_LABELS_TH.keys())
    assert not missing, (
        f"descriptors reference these spec_ids without a SPEC_ID_LABELS_TH "
        f"entry — would render as 'Figure N: <raw_id>' in PDFs: {missing}"
    )
