"""Smoke tests for the 7 initial ContentBlock subclasses (ADR-03 §3).

Per the ADR-03 contract every block must:
    1. instantiate cleanly,
    2. ``await block.collect(...)`` and return a non-empty dict,
    3. ``block.render_latex(...)`` returns a non-empty string,
    4. ``block.render_html(...)`` returns a string starting with ``<``.

This file parameterises the basic happy-path checks, then adds extra
asserts per block where the data path needs mocking (chart, table) or
where a format-specific invariant is worth pinning (heading levels,
KPI grid value formatting, paragraph substitution).

Mocks: the chart block calls ``ChartService.render`` (async) and the
table block calls ``MVRepository.run_query`` (async). Both are injected
through ``RenderContext.extra`` to avoid the real DB / repo wiring.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

# Make ``api/`` importable for ``services.reports.*``.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.reports.blocks import (  # noqa: E402
    AppendixMethodologyBlock,
    ChartBlock,
    ColSpec,
    CoverPageBlock,
    HeadingBlock,
    KPIGridBlock,
    KPISpec,
    ParagraphBlock,
    TableBlock,
    block_registry,
)
from services.reports.spec import (  # noqa: E402
    RenderContext,
    ReportDescriptor,
    SectionSpec,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeDataCollector:
    """Tiny stand-in for ``ReportDataCollector``.

    The real one is built by S4.2; this one returns the same shape
    (a flat dict accessible via ``.data()``) so blocks that read from
    the collector don't care which one they got.
    """

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def data(self) -> Dict[str, Any]:
        return self._payload


def _payload() -> Dict[str, Any]:
    return {
        "summary": {
            "total_screened": 12345,
            "districts_covered": 50,
            "msd_count": 12,
        },
        "city": {
            "diabetes": {"pct_at_risk": 12.5},
            "hypertension": {"pct_at_risk": 22.45},
        },
    }


def _descriptor() -> ReportDescriptor:
    return ReportDescriptor(
        report_id="t",
        title_th="รายงานทดสอบ",
        title_en="Test Report",
        formats=["html", "latex"],
        languages=["th", "en"],
        sections=[
            SectionSpec(id="s1", block="cover_page"),
        ],
    )


def _ctx(
    extra: Dict[str, Any] | None = None, lang: str = "th"
) -> RenderContext:
    return RenderContext(
        data_collector=_FakeDataCollector(_payload()),
        lang=lang,
        fmt="html",
        descriptor=_descriptor(),
        requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        extra=extra or {},
    )


# ---------------------------------------------------------------------------
# Per-block construction matrix
# ---------------------------------------------------------------------------


def _cover_case() -> tuple:
    block = CoverPageBlock()
    params = block.Parameters(
        subtitle_th="สรุปประจำปี",
        generation_date="2026-05-01",
        logo_path="/tmp/logo.png",
    )
    return block, params, {}


def _heading_case() -> tuple:
    block = HeadingBlock()
    params = block.Parameters(
        text_th="บทนำ", text_en="Introduction", level=2
    )
    return block, params, {}


def _paragraph_case() -> tuple:
    block = ParagraphBlock()
    params = block.Parameters(
        text_th=(
            "คัดกรองทั้งหมด {summary.total_screened} ราย "
            "ครอบคลุม {summary.districts_covered} เขต"
        ),
    )
    return block, params, {}


def _kpi_grid_case() -> tuple:
    block = KPIGridBlock()
    params = block.Parameters(
        metrics=[
            KPISpec(
                label_th="ผู้คัดกรอง",
                source_path="summary.total_screened",
                format="int",
            ),
            KPISpec(
                label_th="DM at-risk",
                source_path="city.diabetes.pct_at_risk",
                format="pct",
            ),
        ]
    )
    return block, params, {}


def _chart_case() -> tuple:
    """Chart block needs a mocked ChartService injected via ctx.extra."""
    block = ChartBlock()
    params = block.Parameters(
        spec_id="risk_factor_profile",
        filters={"district": "บางรัก"},
        caption_th="ภาพรวมเขต",
    )
    # Build a small response that walks like a ``ChartResponse``.
    canned = {
        "kind": "bar",
        "spec_id": "risk_factor_profile",
        "data": [
            {"x": "บางรัก", "y": 12.5, "n": 47, "series": None, "masked": False},
            {"x": "ดุสิต", "y": 9.8, "n": 30, "series": None, "masked": False},
        ],
        "meta": {
            "n_total": 2,
            "k_anon_threshold": 5,
            "k_anon_dropped": 0,
            "filters_applied": {"district": "บางรัก"},
            "generated_at": "2026-05-01T00:00:00Z",
        },
    }

    class _Resp:
        """Minimal ``ChartResponse`` lookalike — quacks via model_dump."""

        def model_dump(self) -> Dict[str, Any]:
            return canned

    fake_service = AsyncMock()
    fake_service.render.return_value = _Resp()
    return block, params, {"chart_service": fake_service}


def _table_case() -> tuple:
    """Table block needs a mocked MVRepository injected via ctx.extra."""
    block = TableBlock()
    params = block.Parameters(
        query_id="district_disease_counts",
        columns=[
            ColSpec(key="district_code", header_th="เขต", format="str"),
            ColSpec(
                key="total_screened", header_th="ผู้คัดกรอง", format="int"
            ),
            ColSpec(key="pct_risk_dm", header_th="% เสี่ยง DM", format="pct"),
        ],
        max_rows=10,
    )
    fake_rows: List[Dict[str, Any]] = [
        {"district_code": "1001", "total_screened": 500, "pct_risk_dm": 8.7},
        {"district_code": "1002", "total_screened": 723, "pct_risk_dm": 11.2},
    ]
    fake_repo = AsyncMock()
    fake_repo.run_query.return_value = fake_rows
    return block, params, {"mv_repository": fake_repo}


def _appendix_case() -> tuple:
    block = AppendixMethodologyBlock()
    params = block.Parameters()
    return block, params, {}


_CASES = [
    pytest.param(_cover_case, id="cover_page"),
    pytest.param(_heading_case, id="heading"),
    pytest.param(_paragraph_case, id="paragraph"),
    pytest.param(_kpi_grid_case, id="kpi_grid"),
    pytest.param(_chart_case, id="chart"),
    pytest.param(_table_case, id="table"),
    pytest.param(_appendix_case, id="appendix_methodology"),
]


# ---------------------------------------------------------------------------
# Parametrised happy-path: collect → non-empty dict
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("factory", _CASES)
async def test_block_collect_returns_non_empty_dict(factory: Any) -> None:
    block, params, extra = factory()
    ctx = _ctx(extra=extra)
    data = await block.collect(ctx, params)
    assert isinstance(data, dict)
    assert len(data) > 0, f"{block.block_id} returned empty dict from collect"


# ---------------------------------------------------------------------------
# Parametrised happy-path: render_latex + render_html non-empty
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("factory", _CASES)
async def test_block_render_latex_non_empty(factory: Any) -> None:
    block, params, extra = factory()
    ctx = _ctx(extra=extra)
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    assert isinstance(out, str)
    assert out.strip(), f"{block.block_id} rendered empty LaTeX"


@pytest.mark.anyio
@pytest.mark.parametrize("factory", _CASES)
async def test_block_render_html_starts_with_angle_bracket(
    factory: Any,
) -> None:
    block, params, extra = factory()
    ctx = _ctx(extra=extra)
    data = await block.collect(ctx, params)
    out = block.render_html(data, params, ctx)
    assert isinstance(out, str)
    stripped = out.lstrip()
    assert stripped.startswith("<"), (
        f"{block.block_id} HTML output does not start with '<': {out[:80]!r}"
    )


# ---------------------------------------------------------------------------
# Block-specific invariants — each catches a distinct regression risk
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_heading_levels_map_to_correct_tags() -> None:
    block = HeadingBlock()
    for level, html_tag, latex_cmd in (
        (1, "h1", r"\section"),
        (2, "h2", r"\subsection"),
        (3, "h3", r"\subsubsection"),
    ):
        params = block.Parameters(text_th="หัวข้อ", level=level)
        ctx = _ctx()
        data = await block.collect(ctx, params)
        assert f"<{html_tag}>" in block.render_html(data, params, ctx)
        assert latex_cmd in block.render_latex(data, params, ctx)


@pytest.mark.anyio
async def test_paragraph_substitutes_dotted_paths() -> None:
    block, params, extra = _paragraph_case()
    ctx = _ctx(extra=extra)
    data = await block.collect(ctx, params)
    # '12345' should appear (the int formatting helper isn't applied to
    # paragraph substitution — that's the kpi_grid block's job).
    assert "12345" in data["text"]
    assert "50" in data["text"]


@pytest.mark.anyio
async def test_kpi_grid_formats_values_per_kind() -> None:
    block, params, extra = _kpi_grid_case()
    ctx = _ctx(extra=extra)
    data = await block.collect(ctx, params)
    tiles = data["tiles"]
    assert tiles[0]["value"] == "12,345"  # int with thousands separator
    assert tiles[1]["value"] == "12.5%"   # pct with 1 decimal


@pytest.mark.anyio
async def test_cover_page_includes_logo_when_set() -> None:
    block, params, _ = _cover_case()
    ctx = _ctx()
    data = await block.collect(ctx, params)
    html = block.render_html(data, params, ctx)
    assert "/tmp/logo.png" in html
    latex = block.render_latex(data, params, ctx)
    assert r"\includegraphics" in latex


@pytest.mark.anyio
async def test_cover_page_omits_logo_when_unset() -> None:
    block = CoverPageBlock()
    params = block.Parameters()
    ctx = _ctx()
    data = await block.collect(ctx, params)
    html = block.render_html(data, params, ctx)
    assert "<img" not in html
    latex = block.render_latex(data, params, ctx)
    assert r"\includegraphics" not in latex


@pytest.mark.anyio
async def test_chart_block_calls_chartservice_with_filters() -> None:
    block, params, extra = _chart_case()
    ctx = _ctx(extra=extra)
    await block.collect(ctx, params)
    extra["chart_service"].render.assert_awaited_once_with(
        "risk_factor_profile", {"district": "บางรัก"}
    )


@pytest.mark.anyio
async def test_chart_block_html_inline_svg() -> None:
    block, params, extra = _chart_case()
    ctx = _ctx(extra=extra)
    data = await block.collect(ctx, params)
    html = block.render_html(data, params, ctx)
    # Either pyecharts produced an <svg>, or our fallback did. Either
    # way an <svg ...> tag must be present so the chart is self-contained.
    assert "<svg" in html


@pytest.mark.anyio
async def test_chart_block_latex_uses_tikz_for_bar() -> None:
    block, params, extra = _chart_case()
    ctx = _ctx(extra=extra)
    data = await block.collect(ctx, params)
    latex = block.render_latex(data, params, ctx)
    assert r"\begin{tikzpicture}" in latex


@pytest.mark.anyio
async def test_chart_block_latex_placeholder_for_unknown_kind() -> None:
    block = ChartBlock()
    params = block.Parameters(spec_id="heatmap_demo")

    class _Resp:
        def model_dump(self) -> Dict[str, Any]:
            return {
                "kind": "heatmap",
                "spec_id": "heatmap_demo",
                "data": [],
                "meta": {
                    "n_total": 0,
                    "k_anon_threshold": 5,
                    "k_anon_dropped": 0,
                    "filters_applied": {},
                    "generated_at": "2026-05-01T00:00:00Z",
                },
            }

    fake = AsyncMock()
    fake.render.return_value = _Resp()
    ctx = _ctx(extra={"chart_service": fake})
    data = await block.collect(ctx, params)
    latex = block.render_latex(data, params, ctx)
    assert "rendering not available in LaTeX" in latex


@pytest.mark.anyio
async def test_table_block_runs_query_and_truncates() -> None:
    block, params, extra = _table_case()
    ctx = _ctx(extra=extra)
    data = await block.collect(ctx, params)
    assert data["n_rows"] == 2
    assert data["truncated"] is False
    extra["mv_repository"].run_query.assert_awaited_once_with(
        "district_disease_counts", {}
    )
    # Cell formatting respects per-column ``format``.
    assert data["rows"][0][1] == "500"  # int with thousands separator
    assert data["rows"][0][2] == "8.7%"  # pct with 1 decimal


@pytest.mark.anyio
async def test_table_block_truncation_marks_flag() -> None:
    block = TableBlock()
    params = block.Parameters(
        query_id="q",
        columns=[ColSpec(key="x", header_th="x")],
        max_rows=3,
    )
    rows = [{"x": str(i)} for i in range(10)]
    fake = AsyncMock()
    fake.run_query.return_value = rows
    ctx = _ctx(extra={"mv_repository": fake})
    data = await block.collect(ctx, params)
    assert data["truncated"] is True
    assert data["n_rows"] == 3


@pytest.mark.anyio
async def test_appendix_methodology_mentions_required_topics() -> None:
    block, params, _ = _appendix_case()
    ctx = _ctx()
    data = await block.collect(ctx, params)
    text = " ".join(data["bullets"])
    # All four pillars from the task spec must show up in the copy.
    assert "k-anonymity" in text
    assert "non-imputation" in text
    assert "MSD" in text
    assert "HHC" in text


# ---------------------------------------------------------------------------
# Registry sanity — the package import side-effect chain must end with
# all 7 blocks discoverable from the singleton (this is the smoke test
# the task explicitly calls out).
# ---------------------------------------------------------------------------


def test_block_registry_lists_all_seven_blocks() -> None:
    # ``reload=True`` defends against test ordering: other tests in
    # the suite may have replaced the singleton with a tmp-dir registry
    # for their own scenarios. Re-discovering from the default path
    # gives us a deterministic check regardless of run order.
    reg = block_registry(reload=True)
    ids = reg.list_ids()
    assert ids == sorted(
        [
            "appendix_methodology",
            "chart",
            "cover_page",
            "heading",
            "kpi_grid",
            "paragraph",
            "table",
        ]
    )
    assert len(reg) == 7
