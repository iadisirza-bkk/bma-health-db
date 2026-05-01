"""Tests for the ``two_column_layout`` container ContentBlock.

ADR-03 S6 addendum (2026-06-15)
-------------------------------
This is the project's first container block. Its contract — and the
shape of the tests below — derives from the addendum at the bottom of
``docs/adr/ADR-03-report-descriptors.md``:

* The block re-enters the orchestrator via
  ``ctx.extra['report_service']._render_sections(child_specs, ctx)``.
* Depth=1 is the only level supported. A ``two_column_layout`` whose
  children include another ``two_column_layout`` is rejected at TWO
  levels — the block itself (friendlier error) and the orchestrator
  recursion-depth check (backstop).
* ``visible_in`` filtering applies to nested children.
* The block requires ``ctx.extra['report_service']`` to be set; without
  it ``collect()`` raises ``RuntimeError``.

Test surface
~~~~~~~~~~~~
1. Direct collect() with a fake report service stub.
2. render_latex / render_html structural invariants.
3. Nesting rejection (block-level ValueError).
4. Missing report_service injection (RuntimeError).
5. Depth-cap rejection through the real ReportService orchestrator.
6. Auto-injection of report_service into ctx.extra by ReportService.render.
7. End-to-end: descriptor with one layout section + two paragraph
   children renders correctly.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from pydantic import BaseModel

# Make ``api/`` importable for ``services.reports.*``.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.reports.blocks import (  # noqa: E402
    BlockRegistry,
    ContentBlock,
    ParagraphBlock,
    TwoColumnLayoutBlock,
    TwoColumnLayoutParams,
)
from services.reports.data_collector import ReportDataCollector  # noqa: E402
from services.reports.registry import ReportRegistry  # noqa: E402
from services.reports.renderer import (  # noqa: E402
    RendererRegistry,
    ReportRenderer,
)
from services.reports.service import ReportService  # noqa: E402
from services.reports.spec import (  # noqa: E402
    RenderContext,
    RenderedSection,
    ReportDescriptor,
    SectionSpec,
)


# ---------------------------------------------------------------------------
# Fakes — a stub orchestrator so collect() can be unit-tested without
# spinning up the full ReportService + descriptor stack.
# ---------------------------------------------------------------------------


class _EmptyParams(BaseModel):
    """Concrete subclass — Pydantic v2 won't instantiate BaseModel itself."""


class _FakeReportService:
    """Minimal ``ReportService`` stub for direct ``collect()`` testing.

    The real orchestrator's ``_render_sections`` walks each child
    section through its block; here we simply turn each ``SectionSpec``
    into a hardcoded ``RenderedSection`` so the layout block's collect
    can be exercised in isolation.
    """

    # The block-level nesting check uses MAX_RECURSION_DEPTH from the
    # block, not from the service, so we don't need to set it here.
    # But the orchestrator-level check DOES read ``MAX_RECURSION_DEPTH``
    # off the service — leave it absent on the fake, the block-level
    # check fires first.

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def _render_sections(
        self,
        sections: List[SectionSpec],
        ctx: RenderContext,
    ) -> List[RenderedSection]:
        # Record the call so tests can assert that both columns reach
        # the orchestrator.
        self.calls.append(
            {"sections": list(sections), "depth": ctx.recursion_depth}
        )
        # Build a hardcoded RenderedSection per child — the layout
        # block only consumes ``markup``, so the data dict can be empty.
        return [
            RenderedSection(
                section_id=s.id,
                block_id=s.block,
                markup=f"<<{s.id}>>",
                data={},
                params=_EmptyParams(),
            )
            for s in sections
        ]


class _FakeDataCollector:
    """Stand-in for ``ReportDataCollector`` — paragraph block needs one."""

    def __init__(self, payload: Optional[Dict[str, Any]] = None) -> None:
        self._payload = payload or {}

    def data(self) -> Dict[str, Any]:
        return self._payload


def _make_descriptor(sections: List[SectionSpec]) -> ReportDescriptor:
    return ReportDescriptor(
        report_id="layout_test",
        title_th="ทดสอบเลย์เอาต์",
        formats=["html", "latex"],
        languages=["th"],
        sections=sections,
    )


def _make_ctx(
    *,
    fmt: str = "html",
    extra: Optional[Dict[str, Any]] = None,
    sections: Optional[List[SectionSpec]] = None,
) -> RenderContext:
    return RenderContext(
        data_collector=_FakeDataCollector(),
        lang="th",
        fmt=fmt,
        descriptor=_make_descriptor(
            sections if sections is not None else []
        ),
        requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        extra=extra or {},
    )


# ---------------------------------------------------------------------------
# 1. Direct collect() — both columns reach the (fake) orchestrator.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_collect_recurses_into_orchestrator_for_both_columns() -> None:
    """``collect`` calls ``_render_sections`` once per column."""
    block = TwoColumnLayoutBlock()
    params = TwoColumnLayoutParams(
        left=[SectionSpec(id="L1", block="paragraph", params={"text_th": "L"})],
        right=[
            SectionSpec(id="R1", block="paragraph", params={"text_th": "R1"}),
            SectionSpec(id="R2", block="paragraph", params={"text_th": "R2"}),
        ],
    )

    fake_svc = _FakeReportService()
    ctx = _make_ctx(extra={"report_service": fake_svc})

    data = await block.collect(ctx, params)

    # Both columns surface as lists of RenderedSection.
    assert isinstance(data["left"], list)
    assert isinstance(data["right"], list)
    assert [s.section_id for s in data["left"]] == ["L1"]
    assert [s.section_id for s in data["right"]] == ["R1", "R2"]

    # Both columns went through the orchestrator stub.
    assert len(fake_svc.calls) == 2
    # Depth was bumped to 1 (the orchestrator-level check) for the
    # nested call, and restored after.
    assert fake_svc.calls[0]["depth"] == 1
    assert fake_svc.calls[1]["depth"] == 1
    assert ctx.recursion_depth == 0  # restored after collect()


# ---------------------------------------------------------------------------
# 2. Render markup — structural invariants.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_render_latex_emits_two_minipages_and_hspace() -> None:
    """LaTeX output: two ``\\begin{minipage}`` + a ``\\hspace`` between."""
    block = TwoColumnLayoutBlock()
    params = TwoColumnLayoutParams(
        left=[SectionSpec(id="L", block="paragraph", params={"text_th": "L"})],
        right=[SectionSpec(id="R", block="paragraph", params={"text_th": "R"})],
        ratio=(0.6, 0.4),
        gap_em=2.0,
    )
    ctx = _make_ctx(
        fmt="latex", extra={"report_service": _FakeReportService()}
    )
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)

    # Two minipages on the page.
    assert out.count(r"\begin{minipage}") == 2
    assert out.count(r"\end{minipage}") == 2
    # The ratios appear in the minipage widths.
    assert r"0.6\textwidth" in out
    assert r"0.4\textwidth" in out
    # The hspace separator is emitted with the configured gap.
    assert r"\hspace{2.0em}" in out
    # Children's markup is concatenated in column order.
    assert out.find("<<L>>") < out.find(r"\end{minipage}")
    assert out.find("<<R>>") > out.find(r"\hspace{2.0em}")


@pytest.mark.anyio
async def test_render_html_emits_grid_with_correct_percentages() -> None:
    """HTML output: a ``<div class="two-column">`` with inline grid styles."""
    block = TwoColumnLayoutBlock()
    params = TwoColumnLayoutParams(
        left=[SectionSpec(id="L", block="paragraph", params={"text_th": "L"})],
        right=[SectionSpec(id="R", block="paragraph", params={"text_th": "R"})],
        ratio=(0.7, 0.3),
        gap_em=1.5,
    )
    ctx = _make_ctx(
        fmt="html", extra={"report_service": _FakeReportService()}
    )
    data = await block.collect(ctx, params)
    out = block.render_html(data, params, ctx)

    # Inline grid CSS — the report stays self-contained without an
    # external stylesheet.
    assert 'class="two-column"' in out
    assert "display: grid" in out
    assert "grid-template-columns: 70% 30%" in out
    assert "gap: 1.5em" in out
    # Both column wrappers + their child markups are present.
    assert '<div class="col-left"><<L>></div>' in out
    assert '<div class="col-right"><<R>></div>' in out


# ---------------------------------------------------------------------------
# 3. Nesting rejection — block-level (friendlier error).
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_collect_rejects_nested_two_column_layout_in_left() -> None:
    """A ``two_column_layout`` inside ``params.left`` raises ValueError."""
    block = TwoColumnLayoutBlock()
    params = TwoColumnLayoutParams(
        left=[
            SectionSpec(
                id="nested",
                block="two_column_layout",
                params={
                    "left": [
                        {
                            "id": "x",
                            "block": "paragraph",
                            "params": {"text_th": "x"},
                        }
                    ],
                    "right": [
                        {
                            "id": "y",
                            "block": "paragraph",
                            "params": {"text_th": "y"},
                        }
                    ],
                },
            )
        ],
        right=[SectionSpec(id="R", block="paragraph", params={"text_th": "R"})],
    )
    ctx = _make_ctx(extra={"report_service": _FakeReportService()})

    with pytest.raises(
        ValueError,
        match=r"two_column_layout cannot nest inside itself .S6 depth=1 cap.",
    ):
        await block.collect(ctx, params)


@pytest.mark.anyio
async def test_collect_rejects_nested_two_column_layout_in_right() -> None:
    """Same check, but with the offender on the right side."""
    block = TwoColumnLayoutBlock()
    params = TwoColumnLayoutParams(
        left=[SectionSpec(id="L", block="paragraph", params={"text_th": "L"})],
        right=[
            SectionSpec(
                id="nested",
                block="two_column_layout",
                params={
                    "left": [
                        {
                            "id": "x",
                            "block": "paragraph",
                            "params": {"text_th": "x"},
                        }
                    ],
                    "right": [
                        {
                            "id": "y",
                            "block": "paragraph",
                            "params": {"text_th": "y"},
                        }
                    ],
                },
            )
        ],
    )
    ctx = _make_ctx(extra={"report_service": _FakeReportService()})

    with pytest.raises(
        ValueError, match="two_column_layout cannot nest"
    ):
        await block.collect(ctx, params)


# ---------------------------------------------------------------------------
# 4. Missing report_service — clear RuntimeError.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_collect_without_report_service_raises_runtime_error() -> None:
    """Missing ``ctx.extra['report_service']`` is a clear RuntimeError."""
    block = TwoColumnLayoutBlock()
    params = TwoColumnLayoutParams(
        left=[SectionSpec(id="L", block="paragraph", params={"text_th": "L"})],
        right=[SectionSpec(id="R", block="paragraph", params={"text_th": "R"})],
    )
    ctx = _make_ctx(extra={})  # no report_service

    with pytest.raises(
        RuntimeError, match=r"ctx\.extra\['report_service'\]"
    ):
        await block.collect(ctx, params)


# ---------------------------------------------------------------------------
# 5. Parameter validation — ratio must sum to 1.0, both > 0.
# ---------------------------------------------------------------------------


def test_params_reject_ratio_not_summing_to_one() -> None:
    with pytest.raises(ValueError, match="must sum to 1.0"):
        TwoColumnLayoutParams(
            left=[SectionSpec(id="L", block="paragraph", params={})],
            right=[SectionSpec(id="R", block="paragraph", params={})],
            ratio=(0.4, 0.4),
        )


def test_params_reject_zero_or_negative_ratio() -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        TwoColumnLayoutParams(
            left=[SectionSpec(id="L", block="paragraph", params={})],
            right=[SectionSpec(id="R", block="paragraph", params={})],
            ratio=(0.0, 1.0),
        )


# ---------------------------------------------------------------------------
# 6. End-to-end through ReportService — the auto-injection contract.
# ---------------------------------------------------------------------------


class _CapturingRenderer(ReportRenderer):
    """Renderer that captures (sections, ctx) for assertions."""

    fmt = "html"

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def render(
        self,
        desc: ReportDescriptor,
        sections: List[RenderedSection],
        ctx: RenderContext,
        out_path: Path,
    ) -> Path:
        self.calls.append(
            {
                "section_ids": [s.section_id for s in sections],
                "section_markups": [s.markup for s in sections],
                "fmt": ctx.fmt,
                "report_service_in_extra": ctx.extra.get("report_service"),
            }
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Concatenate everything so the e2e demo test can grep the file.
        body = "".join(
            s.markup if isinstance(s.markup, str) else "" for s in sections
        )
        out_path.write_text(body, encoding="utf-8")
        return out_path


def _build_real_service(
    descriptor: ReportDescriptor,
    out_dir: Path,
) -> tuple[ReportService, _CapturingRenderer]:
    """Wire a real ``ReportService`` with the real ``two_column_layout``
    + ``paragraph`` blocks plus a capturing renderer, no DB."""
    blocks = BlockRegistry()
    blocks.register(TwoColumnLayoutBlock)
    blocks.register(ParagraphBlock)

    descriptors = ReportRegistry({descriptor.report_id: descriptor})

    renderer = _CapturingRenderer()
    rreg = RendererRegistry()
    rreg.register(renderer)

    data = ReportDataCollector(
        cache_ttl_seconds=300,
        collector_fn=lambda: {"x": "y"},
        hash_fn=lambda: "deadbeef",
    )
    svc = ReportService(
        descriptors=descriptors,
        blocks=blocks,
        renderers=rreg,
        data=data,
        out_dir=out_dir,
    )
    return svc, renderer


@pytest.mark.anyio
async def test_render_auto_injects_report_service_into_ctx(
    tmp_path: Path,
) -> None:
    """``ReportService.render`` sets ``ctx.extra['report_service']`` itself."""
    desc = ReportDescriptor(
        report_id="auto_inject",
        title_th="ทดสอบ",
        formats=["html"],
        languages=["th"],
        sections=[
            SectionSpec(
                id="layout",
                block="two_column_layout",
                params={
                    "left": [
                        {
                            "id": "L",
                            "block": "paragraph",
                            "params": {"text_th": "เนื้อหาซ้าย"},
                        }
                    ],
                    "right": [
                        {
                            "id": "R",
                            "block": "paragraph",
                            "params": {"text_th": "เนื้อหาขวา"},
                        }
                    ],
                },
            )
        ],
    )
    svc, renderer = _build_real_service(desc, tmp_path)

    out = await svc.render("auto_inject", "html", "th")

    # The renderer saw a context whose extra carries the orchestrator
    # back-reference. That's the auto-injection contract from the
    # service.render() flow.
    assert len(renderer.calls) == 1
    assert renderer.calls[0]["report_service_in_extra"] is svc
    # Single top-level section (the layout block's own markup).
    assert renderer.calls[0]["section_ids"] == ["layout"]
    # Read the file the renderer wrote and confirm both children's
    # paragraph markup made it through.
    text = out.read_text(encoding="utf-8")
    assert "เนื้อหาซ้าย" in text
    assert "เนื้อหาขวา" in text
    # Inline grid styling proves we ran the layout's render_html.
    assert 'class="two-column"' in text


@pytest.mark.anyio
async def test_e2e_layout_renders_paragraphs_in_expected_order(
    tmp_path: Path,
) -> None:
    """End-to-end: descriptor with one layout section + two paragraphs
    renders to a single HTML body with both paragraphs in left/right
    column order."""
    desc = ReportDescriptor(
        report_id="e2e_layout",
        title_th="ทดสอบเลย์เอาต์ครบวงจร",
        formats=["html"],
        languages=["th"],
        sections=[
            SectionSpec(
                id="layout",
                block="two_column_layout",
                params={
                    "left": [
                        {
                            "id": "L",
                            "block": "paragraph",
                            "params": {"text_th": "ALPHA"},
                        }
                    ],
                    "right": [
                        {
                            "id": "R",
                            "block": "paragraph",
                            "params": {"text_th": "BETA"},
                        }
                    ],
                    "ratio": [0.5, 0.5],
                    "gap_em": 1.0,
                },
            )
        ],
    )
    svc, _renderer = _build_real_service(desc, tmp_path)

    out = await svc.render("e2e_layout", "html", "th")
    body = out.read_text(encoding="utf-8")
    # Left column appears before the right column in source order.
    assert body.find("ALPHA") < body.find("BETA")
    # Both paragraphs are wrapped in <p> per the paragraph block.
    assert "<p>ALPHA</p>" in body
    assert "<p>BETA</p>" in body
    # Layout structure is intact.
    assert "col-left" in body and "col-right" in body


@pytest.mark.anyio
async def test_e2e_double_nested_layout_is_rejected_by_block(
    tmp_path: Path,
) -> None:
    """A descriptor with a ``two_column_layout`` inside another layout
    fails at render time with the block-level nesting error."""
    desc = ReportDescriptor(
        report_id="bad_nested",
        title_th="ลายในลาย",
        formats=["html"],
        languages=["th"],
        sections=[
            SectionSpec(
                id="outer",
                block="two_column_layout",
                params={
                    "left": [
                        # The inner layout is the violation.
                        {
                            "id": "inner",
                            "block": "two_column_layout",
                            "params": {
                                "left": [
                                    {
                                        "id": "i_l",
                                        "block": "paragraph",
                                        "params": {"text_th": "L"},
                                    }
                                ],
                                "right": [
                                    {
                                        "id": "i_r",
                                        "block": "paragraph",
                                        "params": {"text_th": "R"},
                                    }
                                ],
                            },
                        }
                    ],
                    "right": [
                        {
                            "id": "R",
                            "block": "paragraph",
                            "params": {"text_th": "BENIGN"},
                        }
                    ],
                },
            )
        ],
    )
    svc, _renderer = _build_real_service(desc, tmp_path)

    # The orchestrator wraps block exceptions in RuntimeError("collect() failed");
    # the original ValueError is the __cause__.
    with pytest.raises(RuntimeError) as excinfo:
        await svc.render("bad_nested", "html", "th")
    cause = excinfo.value.__cause__
    assert isinstance(cause, ValueError)
    assert "two_column_layout cannot nest inside itself" in str(cause)


@pytest.mark.anyio
async def test_visible_in_filter_applies_to_nested_children(
    tmp_path: Path,
) -> None:
    """A nested child marked ``visible_in=['latex']`` is skipped in HTML."""
    desc = ReportDescriptor(
        report_id="visible_in_nested",
        title_th="กรองตามฟอร์แมต",
        formats=["html"],
        languages=["th"],
        sections=[
            SectionSpec(
                id="layout",
                block="two_column_layout",
                params={
                    "left": [
                        {
                            "id": "html_only",
                            "block": "paragraph",
                            "params": {"text_th": "VISIBLE"},
                        },
                        {
                            "id": "latex_only",
                            "block": "paragraph",
                            "params": {"text_th": "HIDDEN"},
                            "visible_in": ["latex"],
                        },
                    ],
                    "right": [
                        {
                            "id": "always",
                            "block": "paragraph",
                            "params": {"text_th": "ALSO"},
                        }
                    ],
                },
            )
        ],
    )
    svc, _renderer = _build_real_service(desc, tmp_path)
    out = await svc.render("visible_in_nested", "html", "th")
    body = out.read_text(encoding="utf-8")
    assert "VISIBLE" in body
    assert "ALSO" in body
    # The latex-only nested child was filtered out by the orchestrator.
    assert "HIDDEN" not in body


# ---------------------------------------------------------------------------
# 7. Backward-compat: _build_sections alias still works.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_sections_alias_still_works(tmp_path: Path) -> None:
    """The legacy ``_build_sections(desc, ctx)`` name keeps working."""
    desc = ReportDescriptor(
        report_id="alias_test",
        title_th="alias",
        formats=["html"],
        languages=["th"],
        sections=[
            SectionSpec(
                id="p",
                block="paragraph",
                params={"text_th": "hello"},
            )
        ],
    )
    svc, _renderer = _build_real_service(desc, tmp_path)
    ctx = RenderContext(
        data_collector=svc._data,  # type: ignore[attr-defined]
        lang="th",
        fmt="html",
        descriptor=desc,
        requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        extra={"report_service": svc},
    )
    rendered = await svc._build_sections(desc, ctx)
    assert len(rendered) == 1
    assert rendered[0].section_id == "p"
    assert "hello" in rendered[0].markup
