"""S10 (Template-First Reports) — heading block emits real LaTeX
section commands.

Companion to ``test_blocks.py::test_heading_levels_map_to_correct_tags``
which already pins the level→tag mapping. This file pins:
    * ``\\section{...}`` exact-string emission for level 1 (so the
      template-first descriptor TOC actually receives entries),
    * extension to level 4 (``\\paragraph`` / ``<h4>``) added in S10,
    * defensive fallback when a malformed descriptor sneaks past the
      Pydantic guard at construction time (we still want LaTeX to
      compile rather than blow up mid-build).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict

import pytest

# Make ``api/`` importable for ``services.reports.*``.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.reports.blocks import HeadingBlock  # noqa: E402
from services.reports.spec import (  # noqa: E402
    RenderContext,
    ReportDescriptor,
    SectionSpec,
)


class _FakeDataCollector:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def data(self) -> Dict[str, Any]:
        return self._payload


def _ctx(lang: str = "th") -> RenderContext:
    desc = ReportDescriptor(
        report_id="t",
        title_th="ทดสอบ",
        title_en="Test",
        formats=["html", "latex"],
        languages=["th", "en"],
        sections=[SectionSpec(id="s", block="heading")],
    )
    return RenderContext(
        data_collector=_FakeDataCollector({}),
        lang=lang,
        fmt="latex",
        descriptor=desc,
        requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        extra={},
    )


# ---------------------------------------------------------------------------
# LaTeX exact-string emission
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_heading_level_1_emits_section_latex() -> None:
    block = HeadingBlock()
    params = block.Parameters(text_th="ประชากรในเขต", level=1)
    ctx = _ctx()
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    assert out.strip() == r"\section{ประชากรในเขต}"


@pytest.mark.anyio
async def test_heading_level_2_emits_subsection() -> None:
    block = HeadingBlock()
    params = block.Parameters(text_th="ปัจจัยเสี่ยง", level=2)
    ctx = _ctx()
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    assert out.strip() == r"\subsection{ปัจจัยเสี่ยง}"


@pytest.mark.anyio
async def test_heading_level_3_emits_subsubsection() -> None:
    block = HeadingBlock()
    params = block.Parameters(text_th="โปรไฟล์ NCD", level=3)
    ctx = _ctx()
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    assert out.strip() == r"\subsubsection{โปรไฟล์ NCD}"


@pytest.mark.anyio
async def test_heading_level_4_emits_paragraph() -> None:
    """S10: extend supported levels to 4 (``\\paragraph``)."""
    block = HeadingBlock()
    params = block.Parameters(text_th="หมายเหตุย่อย", level=4)
    ctx = _ctx()
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    assert out.strip() == r"\paragraph{หมายเหตุย่อย}"


@pytest.mark.anyio
async def test_heading_latex_escapes_special_chars() -> None:
    block = HeadingBlock()
    params = block.Parameters(text_th="50% ของผู้ป่วย", level=1)
    ctx = _ctx()
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    # ``%`` would start a LaTeX comment if not escaped.
    assert "50\\%" in out
    assert out.startswith(r"\section{")


# ---------------------------------------------------------------------------
# HTML mirroring
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_heading_html_h1() -> None:
    block = HeadingBlock()
    params = block.Parameters(text_th="ประชากร", level=1)
    ctx = _ctx()
    data = await block.collect(ctx, params)
    out = block.render_html(data, params, ctx)
    assert out == "<h1>ประชากร</h1>"


@pytest.mark.anyio
async def test_heading_html_h4() -> None:
    block = HeadingBlock()
    params = block.Parameters(text_th="หมายเหตุย่อย", level=4)
    ctx = _ctx()
    data = await block.collect(ctx, params)
    out = block.render_html(data, params, ctx)
    assert out == "<h4>หมายเหตุย่อย</h4>"


# ---------------------------------------------------------------------------
# Defensive fallback — Pydantic Literal[1,2,3,4] guards at construction
# time, but a programmatic data dict (e.g. from a misbuilt orchestrator
# call) can still reach render_*. Output should never crash; it should
# read like a heading.
# ---------------------------------------------------------------------------


def test_heading_invalid_level_falls_back_in_latex() -> None:
    """level 0 / 7 → fall back to bold paragraph, never crash."""
    block = HeadingBlock()
    params = block.Parameters(text_th="ดัมมี่", level=1)
    ctx = _ctx()
    out = block.render_latex(
        {"text": "ดัมมี่", "level": 0}, params, ctx
    )
    # Bold paragraph fallback — must NOT emit any section command.
    assert r"\section" not in out
    assert r"\textbf" in out
    assert "ดัมมี่" in out


def test_heading_invalid_level_falls_back_in_html() -> None:
    block = HeadingBlock()
    params = block.Parameters(text_th="ดัมมี่", level=1)
    ctx = _ctx()
    out = block.render_html(
        {"text": "ดัมมี่", "level": 7}, params, ctx
    )
    # Falls back to a bold paragraph; no <h1>..<h4> tag.
    assert "<h1>" not in out
    assert "<h4>" not in out
    assert "<strong>" in out
    assert "ดัมมี่" in out
