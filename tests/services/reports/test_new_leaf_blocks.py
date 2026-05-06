"""Tests for the four new whitepaper-parity ContentBlocks (ADR-03 §3).

Surface under test:
    * ``CalloutBlock``       — ``callout``    (info/warn/example/note)
    * ``FormulaBlock``       — ``formula``    (math equation)
    * ``TrendTableBlock``    — ``trend_table`` (Mann-Kendall arrows)
    * ``CoverPageBlock``     — ``cover_page``  with ``mode=closing``
      extension (and a regression test that ``mode=title`` is byte-
      compatible with the original behaviour).

Each block gets:
    * Pydantic Parameters happy-path + ``extra="forbid"`` rejection.
    * ``collect()`` returns a non-empty dict.
    * ``render_latex()`` contains the expected LaTeX macro.
    * ``render_html()`` starts with ``<`` and carries the expected class.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest
from pydantic import ValidationError

# Make ``api/`` importable for ``services.reports.*``.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.reports.blocks import (  # noqa: E402
    CalloutBlock,
    CoverPageBlock,
    FormulaBlock,
    TrendTableBlock,
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
    """Minimal stand-in for ``ReportDataCollector``.

    Same shape as the one in ``test_blocks.py`` — a flat dict accessible
    via ``.data()``. Tests pre-populate ``trends`` for trend_table.
    """

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def data(self) -> Dict[str, Any]:
        return self._payload


def _trend_payload() -> Dict[str, Any]:
    """Five canned trend rows covering every direction value."""
    return {
        "trends": [
            {
                "metric": "DM at-risk %",
                "value": "12.5%",
                "direction": "up",
                "change_pct": 3.4,
                "p_value": 0.012,
            },
            {
                "metric": "HT at-risk %",
                "value": "22.1%",
                "direction": "down",
                "change_pct": -1.7,
                "p_value": 0.041,
            },
            {
                "metric": "BMI mean",
                "value": "24.3",
                "direction": "stable",
                "change_pct": 0.2,
                "p_value": 0.412,
            },
            {
                "metric": "Smoking %",
                "value": "8.9%",
                "direction": "down",
                "change_pct": -0.8,
                "p_value": 0.110,
            },
            {
                "metric": "PM2.5 days >50",
                "value": "21",
                "direction": "up",
                "change_pct": 12.3,
                "p_value": 0.003,
            },
        ],
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
    payload: Dict[str, Any] | None = None, lang: str = "th"
) -> RenderContext:
    return RenderContext(
        data_collector=_FakeDataCollector(payload or {}),
        lang=lang,
        fmt="html",
        descriptor=_descriptor(),
        requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        extra={},
    )


# ===========================================================================
# CalloutBlock
# ===========================================================================


def test_callout_params_happy_path() -> None:
    p = CalloutBlock.Parameters(
        kind="info",
        title_th="ข้อสังเกต",
        text_th="คัดกรองครอบคลุมแล้ว 50 เขต",
    )
    assert p.kind == "info"
    assert p.title_th == "ข้อสังเกต"


def test_callout_params_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        CalloutBlock.Parameters(
            kind="info",
            text_th="x",
            unknown_field="boom",  # type: ignore[call-arg]
        )


def test_callout_params_invalid_kind() -> None:
    with pytest.raises(ValidationError):
        CalloutBlock.Parameters(kind="danger", text_th="x")  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_callout_collect_returns_non_empty_dict() -> None:
    block = CalloutBlock()
    params = block.Parameters(kind="warn", title_th="คำเตือน", text_th="ระวัง")
    data = await block.collect(_ctx(), params)
    assert isinstance(data, dict)
    assert data["kind"] == "warn"
    assert data["title"] == "คำเตือน"
    assert "ระวัง" in data["text"]


@pytest.mark.anyio
async def test_callout_render_latex_uses_fcolorbox() -> None:
    block = CalloutBlock()
    for kind in ("info", "warn", "example"):
        params = block.Parameters(
            kind=kind,  # type: ignore[arg-type]
            title_th="หัวข้อ",
            text_th="เนื้อหา",
        )
        data = await block.collect(_ctx(), params)
        out = block.render_latex(data, params, _ctx())
        assert r"\fcolorbox" in out, f"{kind} missing fcolorbox"
        assert r"\parbox" in out


@pytest.mark.anyio
async def test_callout_note_kind_uses_italic_quote() -> None:
    block = CalloutBlock()
    params = block.Parameters(kind="note", text_th="หมายเหตุเล็กน้อย")
    data = await block.collect(_ctx(), params)
    latex = block.render_latex(data, params, _ctx())
    # ``note`` is the only kind that doesn't use ``\fcolorbox``.
    assert r"\fcolorbox" not in latex
    assert r"\textit" in latex
    assert r"\begin{quote}" in latex


@pytest.mark.anyio
async def test_callout_render_html_has_kind_class_and_aside() -> None:
    block = CalloutBlock()
    params = block.Parameters(kind="example", text_th="ตัวอย่าง")
    data = await block.collect(_ctx(), params)
    html = block.render_html(data, params, _ctx())
    assert html.startswith("<")
    assert "callout-example" in html
    assert "<aside" in html


# ===========================================================================
# FormulaBlock
# ===========================================================================


def test_formula_params_happy_path() -> None:
    p = FormulaBlock.Parameters(
        latex=r"OR = \frac{a/b}{c/d}",
        caption_th="Odds ratio",
    )
    assert p.numbered is True
    assert p.caption_th == "Odds ratio"


def test_formula_params_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        FormulaBlock.Parameters(
            latex=r"x=1", surprise=True  # type: ignore[call-arg]
        )


@pytest.mark.anyio
async def test_formula_collect_returns_non_empty_dict() -> None:
    block = FormulaBlock()
    params = block.Parameters(latex=r"\chi^2 = \sum (O-E)^2 / E")
    data = await block.collect(_ctx(), params)
    assert data["latex"].startswith(r"\chi^2")
    assert data["numbered"] is True


@pytest.mark.anyio
async def test_formula_render_latex_numbered_uses_equation_env() -> None:
    block = FormulaBlock()
    params = block.Parameters(latex=r"a^2 + b^2 = c^2", numbered=True)
    data = await block.collect(_ctx(), params)
    out = block.render_latex(data, params, _ctx())
    assert r"\begin{equation}" in out
    assert r"\end{equation}" in out
    # The asterisk version must NOT slip in for ``numbered=True``.
    assert r"\begin{equation*}" not in out


@pytest.mark.anyio
async def test_formula_render_latex_unnumbered_uses_equation_star() -> None:
    block = FormulaBlock()
    params = block.Parameters(latex=r"E = mc^2", numbered=False)
    data = await block.collect(_ctx(), params)
    out = block.render_latex(data, params, _ctx())
    assert r"\begin{equation*}" in out


@pytest.mark.anyio
async def test_formula_caption_appears_in_latex_when_set() -> None:
    block = FormulaBlock()
    params = block.Parameters(
        latex=r"OR = \frac{ad}{bc}",
        caption_th="คำอธิบาย",
    )
    data = await block.collect(_ctx(), params)
    out = block.render_latex(data, params, _ctx())
    assert "คำอธิบาย" in out


@pytest.mark.anyio
async def test_formula_render_html_wraps_in_div_with_code() -> None:
    block = FormulaBlock()
    params = block.Parameters(latex=r"OR = \frac{a/b}{c/d}")
    data = await block.collect(_ctx(), params)
    html = block.render_html(data, params, _ctx())
    assert html.startswith("<")
    assert 'class="formula"' in html
    assert "<code>" in html
    # LaTeX backslashes survive (escaped or not, '\frac' substring stays).
    assert "frac" in html


# ===========================================================================
# TrendTableBlock
# ===========================================================================


def test_trend_table_params_defaults() -> None:
    p = TrendTableBlock.Parameters()
    assert p.source_path == "trends"
    assert p.metric_filter is None
    assert p.max_rows == 30


def test_trend_table_params_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        TrendTableBlock.Parameters(  # type: ignore[call-arg]
            unknown_thing=True
        )


@pytest.mark.anyio
async def test_trend_table_collect_returns_rows() -> None:
    block = TrendTableBlock()
    params = block.Parameters()
    data = await block.collect(_ctx(_trend_payload()), params)
    assert isinstance(data, dict)
    assert data["n_rows"] == 5
    assert data["truncated"] is False
    # Direction values pass through verbatim.
    assert data["rows"][0]["direction"] == "up"


@pytest.mark.anyio
async def test_trend_table_collect_missing_path_returns_empty() -> None:
    block = TrendTableBlock()
    params = block.Parameters(source_path="not.in.payload")
    data = await block.collect(_ctx(_trend_payload()), params)
    assert data["n_rows"] == 0
    assert data["rows"] == []


@pytest.mark.anyio
async def test_trend_table_metric_filter_keeps_only_matching_rows() -> None:
    block = TrendTableBlock()
    params = block.Parameters(metric_filter="DM at-risk %")
    data = await block.collect(_ctx(_trend_payload()), params)
    assert data["n_rows"] == 1
    assert data["rows"][0]["metric"] == "DM at-risk %"


@pytest.mark.anyio
async def test_trend_table_max_rows_truncates() -> None:
    block = TrendTableBlock()
    params = block.Parameters(max_rows=2)
    data = await block.collect(_ctx(_trend_payload()), params)
    assert data["n_rows"] == 2
    assert data["truncated"] is True


@pytest.mark.anyio
async def test_trend_table_render_latex_emits_direction_macros() -> None:
    block = TrendTableBlock()
    params = block.Parameters()
    ctx = _ctx(_trend_payload())
    data = await block.collect(ctx, params)
    latex = block.render_latex(data, params, ctx)
    assert r"\begin{tabular}" in latex
    # All three direction icons must show up across the 5-row sample.
    assert r"\uparrow" in latex
    assert r"\downarrow" in latex
    assert r"\rightarrow" in latex
    # Colour semantics: up=red, down=okgreen, stable=gray.
    assert r"\textcolor{errred}" in latex
    assert r"\textcolor{okgreen}" in latex
    assert r"\textcolor{gray}" in latex


@pytest.mark.anyio
async def test_trend_table_render_html_has_class_and_arrows() -> None:
    block = TrendTableBlock()
    params = block.Parameters()
    ctx = _ctx(_trend_payload())
    data = await block.collect(ctx, params)
    html = block.render_html(data, params, ctx)
    assert html.startswith("<table")
    assert 'class="trend-table"' in html
    assert "trend-up" in html
    assert "trend-down" in html
    assert "trend-stable" in html
    # Arrow glyphs (HTML entities) — at least one of each should land.
    assert "&#8593;" in html  # up arrow
    assert "&#8595;" in html  # down arrow


@pytest.mark.anyio
async def test_trend_table_caption_renders_above_tabular() -> None:
    """Phase 0 caption convention: when ``caption_th`` is supplied, the
    LaTeX output wraps the tabular in a ``table`` float with ``\\caption``
    appearing BEFORE ``\\begin{tabular}`` (= caption above), and the HTML
    output puts ``<caption>`` as the first child of ``<table class=
    "trend-table">``. Without a caption, the legacy bare-tabular /
    un-captioned-table output is preserved.
    """
    block = TrendTableBlock()
    params = block.Parameters(
        metric_filter="DM at-risk %",
        caption_th="แนวโน้มอัตราเสี่ยงโรคไม่ติดต่อเรื้อรัง (Mann-Kendall)",
        caption_en="NCD risk trend (Mann-Kendall)",
    )
    ctx = _ctx(_trend_payload())
    data = await block.collect(ctx, params)

    # Caption resolved per ctx.lang (default "th") + label auto-derived
    # from source_path + metric_filter so different filters get distinct
    # ``\label``s on the same source.
    assert data["caption"] == "แนวโน้มอัตราเสี่ยงโรคไม่ติดต่อเรื้อรัง (Mann-Kendall)"
    assert data["label"] == "tab:trend:trends__DM_at_risk__"

    # LaTeX: \caption MUST appear BEFORE \begin{tabular} (= above).
    latex = block.render_latex(data, params, ctx)
    assert r"\begin{table}" in latex
    cap_idx = latex.find(r"\caption{")
    tab_idx = latex.find(r"\begin{tabular}")
    assert cap_idx != -1 and tab_idx != -1
    assert cap_idx < tab_idx, "caption must render ABOVE the tabular"

    # HTML: <caption> MUST be the first child of <table class="trend-table">.
    html = block.render_html(data, params, ctx)
    assert '<table class="trend-table"' in html
    assert "<caption>" in html
    table_open = html.find("<table")
    table_open_close = html.find(">", table_open)
    cap_open = html.find("<caption>")
    assert table_open < cap_open <= table_open_close + 1

    # Back-compat: omitting caption_* keeps the legacy output shape.
    params_no_cap = block.Parameters()
    data_no_cap = await block.collect(ctx, params_no_cap)
    latex_no_cap = block.render_latex(data_no_cap, params_no_cap, ctx)
    assert r"\begin{table}" not in latex_no_cap
    assert r"\caption" not in latex_no_cap
    html_no_cap = block.render_html(data_no_cap, params_no_cap, ctx)
    assert "<caption>" not in html_no_cap
    assert html_no_cap.startswith('<table class="trend-table">')


# ===========================================================================
# CoverPageBlock — mode="title" (preserve) and mode="closing" (new)
# ===========================================================================


def test_cover_page_params_default_mode_is_title() -> None:
    p = CoverPageBlock.Parameters()
    assert p.mode == "title"


def test_cover_page_params_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        CoverPageBlock.Parameters(  # type: ignore[call-arg]
            unknown_field="x"
        )


def test_cover_page_params_invalid_mode_rejected() -> None:
    with pytest.raises(ValidationError):
        CoverPageBlock.Parameters(mode="middle")  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_cover_page_title_mode_preserves_existing_behaviour() -> None:
    """``mode="title"`` (the default) emits a manual ``\\begin{titlepage}``
    block (S10 redesign) — replaces the old ``\\title``/``\\maketitle``
    pair which produced a duplicate cover when the root template also
    rendered its own titlepage."""
    block = CoverPageBlock()
    params = block.Parameters(
        subtitle_th="สรุปประจำปี",
        generation_date="2026-05-01",
        logo_path="/tmp/logo.png",
    )
    ctx = _ctx()
    data = await block.collect(ctx, params)
    latex = block.render_latex(data, params, ctx)
    # New surface: full ``\begin{titlepage}...\end{titlepage}`` env with
    # logo + title + subtitle + date inside.
    assert r"\begin{titlepage}" in latex
    assert r"\end{titlepage}" in latex
    assert r"\includegraphics" in latex
    assert "/tmp/logo.png" in latex
    # Title text is rendered directly (no \title{}/\maketitle indirection)
    assert "รายงานทดสอบ" in latex
    # Closing-mode marker must NOT appear.
    assert "ขอบคุณ" not in latex
    html = block.render_html(data, params, ctx)
    assert html.startswith("<header")
    assert 'class="cover"' in html
    assert "/tmp/logo.png" in html


@pytest.mark.anyio
async def test_cover_page_closing_mode_emits_thank_you_finale() -> None:
    block = CoverPageBlock()
    params = block.Parameters(
        mode="closing",
        subtitle_th="ขอบคุณทุกหน่วยงาน",
    )
    ctx = _ctx()
    data = await block.collect(ctx, params)
    assert data["mode"] == "closing"
    latex = block.render_latex(data, params, ctx)
    assert "ขอบคุณ" in latex
    assert r"\section*" in latex
    assert "ขอบคุณทุกหน่วยงาน" in latex
    # Closing-mode must NOT emit ``\title`` / ``\maketitle`` (no double
    # title page).
    assert r"\maketitle" not in latex
    html = block.render_html(data, params, ctx)
    assert html.startswith("<section")
    assert "cover-closing" in html
    assert "ขอบคุณ" in html
    assert "ขอบคุณทุกหน่วยงาน" in html


@pytest.mark.anyio
async def test_cover_page_closing_without_subtitle_omits_paragraph() -> None:
    block = CoverPageBlock()
    params = block.Parameters(mode="closing")
    ctx = _ctx()
    data = await block.collect(ctx, params)
    html = block.render_html(data, params, ctx)
    # No ``<p>`` because subtitle is absent — keeps the finale clean.
    assert "<p>" not in html
    assert "ขอบคุณ" in html


# ===========================================================================
# Registry sanity — all 3 new blocks land in the singleton, plus existing 7.
# ===========================================================================


def test_block_registry_lists_existing_plus_three_new() -> None:
    """The three new blocks (callout, formula, trend_table) must be
    discoverable from the singleton; cover_page is in-place so its id
    must still appear exactly once. Other pre-existing blocks beyond
    the three new ones (e.g. ``crosstab``, ``disease_district_grid``)
    are accepted as-is — this test pins the new ones rather than the
    full count, so it's resilient to future block additions.
    """
    reg = block_registry(reload=True)
    ids = set(reg.list_ids())
    # New blocks must all land.
    assert {"callout", "formula", "trend_table"}.issubset(ids)
    # cover_page is in-place (not a new id).
    assert "cover_page" in ids
    # Pre-existing 7 from ADR-03 §3 still present.
    for required in (
        "appendix_methodology",
        "chart",
        "heading",
        "kpi_grid",
        "paragraph",
        "table",
    ):
        assert required in ids, f"missing pre-existing block: {required!r}"


def test_block_registry_resolves_new_class_paths() -> None:
    reg = block_registry(reload=True)
    assert reg.get("callout") is CalloutBlock
    assert reg.get("formula") is FormulaBlock
    assert reg.get("trend_table") is TrendTableBlock


# ===========================================================================
# Sanity: every new block declares an ``extra="forbid"`` Parameters model
# ===========================================================================


@pytest.mark.parametrize(
    "block_cls",
    [CalloutBlock, FormulaBlock, TrendTableBlock, CoverPageBlock],
)
def test_block_parameters_forbids_extra_fields(block_cls: Any) -> None:
    cfg = getattr(block_cls.Parameters, "model_config", None)
    assert cfg is not None
    # ``model_config`` is a dict in Pydantic v2; ``extra`` may be either a
    # plain string ``"forbid"`` or the equivalent enum.
    extra = cfg.get("extra") if isinstance(cfg, dict) else getattr(cfg, "extra", None)
    assert str(extra) == "forbid", (
        f"{block_cls.__name__} Parameters must set extra='forbid'"
    )
