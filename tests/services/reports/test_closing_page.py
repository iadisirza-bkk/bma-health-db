"""S10 (Template-First Reports) — closing_page block.

The closing_page block is the appendix-style finale of a report:
acknowledgments paragraph, references bullet list, contact stanza.
It's audience-agnostic by design (closes every audience filter).

These tests cover:
    * happy-path LaTeX + HTML rendering of all three sections,
    * each section is independently optional (descriptor authors can
      drop any combination),
    * registry discovery picks up the new block,
    * exact-string LaTeX bullet emission so the whitepaper's
      ``\\begin{itemize}`` survives template churn.
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

from services.reports.blocks import (  # noqa: E402
    ClosingPageBlock,
    block_registry,
)
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


def _ctx(lang: str = "th", fmt: str = "latex") -> RenderContext:
    desc = ReportDescriptor(
        report_id="t",
        title_th="ทดสอบ",
        title_en="Test",
        formats=["html", "latex"],
        languages=["th", "en"],
        sections=[SectionSpec(id="s", block="closing_page")],
    )
    return RenderContext(
        data_collector=_FakeDataCollector({}),
        lang=lang,
        fmt=fmt,
        descriptor=desc,
        requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        extra={},
    )


def _full_params() -> Any:
    """A fully populated closing-page param set — what the whitepaper
    descriptor will plumb through at render time."""
    return ClosingPageBlock.Parameters(
        acknowledgments_th=(
            "ขอขอบคุณบุคลากรทางการแพทย์และสาธารณสุข สำนักการแพทย์ กทม."
        ),
        contact_th="สำนักการแพทย์ กรุงเทพมหานคร",
        contact_email="med-info@bangkok.go.th",
        references=[
            "WHO Global NCD Action Plan 2013-2030",
            "MSD Best-Practice Guidelines (2024)",
        ],
    )


# ---------------------------------------------------------------------------
# LaTeX rendering
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_closing_page_renders_acknowledgments() -> None:
    block = ClosingPageBlock()
    params = _full_params()
    ctx = _ctx()
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    # Newpage forces the closing onto its own sheet.
    assert r"\newpage" in out
    # Un-numbered section so the auto-numbered body isn't disturbed.
    assert r"\section*{" in out
    # The acknowledgments paragraph lands verbatim (latex_escape is a
    # no-op on plain Thai prose).
    assert "ขอขอบคุณบุคลากรทางการแพทย์และสาธารณสุข" in out


@pytest.mark.anyio
async def test_closing_page_renders_references_as_bullets() -> None:
    block = ClosingPageBlock()
    params = _full_params()
    ctx = _ctx()
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    # Bullet list scaffolding present...
    assert r"\begin{itemize}" in out
    assert r"\end{itemize}" in out
    # ...with each reference on its own ``\item``.
    assert r"\item WHO Global NCD Action Plan 2013-2030" in out
    assert r"\item MSD Best-Practice Guidelines (2024)" in out


@pytest.mark.anyio
async def test_closing_page_renders_contact() -> None:
    block = ClosingPageBlock()
    params = _full_params()
    ctx = _ctx()
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    assert "สำนักการแพทย์ กรุงเทพมหานคร" in out
    # Email goes through ``\texttt`` for monospaced rendering.
    assert r"\texttt{" in out
    assert "med-info@bangkok.go.th" in out


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_closing_page_renders_html_section() -> None:
    block = ClosingPageBlock()
    params = _full_params()
    ctx = _ctx(fmt="html")
    data = await block.collect(ctx, params)
    out = block.render_html(data, params, ctx)
    assert out.startswith('<section class="closing-page">')
    # Bullet list rendered as ``<ul><li>...</li></ul>``.
    assert "<ul>" in out
    assert "<li>WHO Global NCD Action Plan 2013-2030</li>" in out
    # Email rendered as a mailto link.
    assert 'href="mailto:med-info@bangkok.go.th"' in out


# ---------------------------------------------------------------------------
# Optionality — each part is independently skippable
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_closing_page_without_references_omits_itemize() -> None:
    block = ClosingPageBlock()
    params = ClosingPageBlock.Parameters(
        acknowledgments_th="ขอขอบคุณ", contact_th="org",
    )
    ctx = _ctx()
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    assert r"\begin{itemize}" not in out
    assert "ขอขอบคุณ" in out


@pytest.mark.anyio
async def test_closing_page_without_acknowledgments_still_renders() -> None:
    """Refs-only descriptor should still produce a valid closing page."""
    block = ClosingPageBlock()
    params = ClosingPageBlock.Parameters(
        references=["WHO Global NCD Action Plan 2013-2030"],
    )
    ctx = _ctx()
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    assert r"\section*{" in out
    assert r"\begin{itemize}" in out
    # No empty paragraph emitted.
    assert "ขอขอบคุณ" not in out


@pytest.mark.anyio
async def test_closing_page_without_contact_omits_email_block() -> None:
    block = ClosingPageBlock()
    params = ClosingPageBlock.Parameters(
        acknowledgments_th="ขอขอบคุณ",
    )
    ctx = _ctx(fmt="html")
    data = await block.collect(ctx, params)
    out = block.render_html(data, params, ctx)
    assert "closing-contact" not in out
    assert "mailto:" not in out


# ---------------------------------------------------------------------------
# i18n — English fallback to Thai when ``_en`` variant is missing
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_closing_page_english_falls_back_to_thai_when_en_missing() -> None:
    block = ClosingPageBlock()
    params = ClosingPageBlock.Parameters(
        acknowledgments_th="ขอขอบคุณ", contact_th="org",
    )
    ctx = _ctx(lang="en")
    data = await block.collect(ctx, params)
    assert data["acknowledgments"] == "ขอขอบคุณ"
    assert data["contact"] == "org"


@pytest.mark.anyio
async def test_closing_page_english_uses_en_when_supplied() -> None:
    block = ClosingPageBlock()
    params = ClosingPageBlock.Parameters(
        acknowledgments_th="ขอขอบคุณ",
        acknowledgments_en="Thank you",
        contact_th="กทม.",
        contact_en="BMA",
        heading_th="ภาคผนวก",
        heading_en="Appendix",
    )
    ctx = _ctx(lang="en")
    data = await block.collect(ctx, params)
    assert data["acknowledgments"] == "Thank you"
    assert data["contact"] == "BMA"
    assert data["heading"] == "Appendix"


# ---------------------------------------------------------------------------
# Registry sanity
# ---------------------------------------------------------------------------


def test_closing_page_block_is_discoverable() -> None:
    reg = block_registry(reload=True)
    assert "closing_page" in reg.list_ids()
    cls = reg.get("closing_page")
    assert cls is ClosingPageBlock


def test_closing_page_audience_target_is_none() -> None:
    """Closing page must close every audience-filtered render — so its
    ``audience_target`` is ``None`` (the orchestrator's "any audience"
    sentinel)."""
    assert ClosingPageBlock.audience_target is None
