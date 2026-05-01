"""``two_column_layout`` block — side-by-side composition primitive.

ADR-03 S6 addendum (2026-06-15)
-------------------------------
This block is the FIRST container ContentBlock in the descriptor surface.
It holds two LISTS OF ``SectionSpec``s (one per column) inside its
``params`` and renders them side-by-side. Children are themselves
resolved through the same ``ReportService`` orchestrator (per-format
``collect()`` + ``render_<fmt>()``) so any block that works at the top
level also works inside a column.

Why params instead of a new top-level descriptor field?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``SectionSpec.params`` is already ``dict[str, Any]``; nested children
ride inside ``params.left`` / ``params.right`` with zero schema churn.
The alternative — adding ``children`` to ``SectionSpec`` — would
sprinkle layout concerns into every block's parameter validation and
forces the orchestrator to special-case "container" sections. Composing
through ``params`` keeps the ABC clean.

Recursion contract
~~~~~~~~~~~~~~~~~~
* The orchestrator injects ``ctx.extra["report_service"] = self`` before
  it dispatches the first section. ``two_column_layout.collect()``
  retrieves that handle and re-enters via
  ``report_service._render_sections(child_specs, ctx)``.
* Depth is capped at ``ReportService.MAX_RECURSION_DEPTH`` (currently 1).
  A ``two_column_layout`` whose ``params.left`` or ``params.right``
  contains another ``two_column_layout`` raises ``ValueError`` at
  ``collect()`` time. We check at the block level (loop over the
  children's ``block`` keys) AND at the orchestrator level (the
  ``ctx.recursion_depth`` counter); the block-level check produces a
  more user-friendly error message that names the block, the
  orchestrator-level check is the belt-and-braces backstop.

Format support
~~~~~~~~~~~~~~
* LaTeX → two ``\begin{minipage}[t]{...}...\end{minipage}`` chunks
  separated by ``\hspace{<gap>em}``.
* HTML → a ``<div class="two-column">`` with inline grid styles so the
  output is self-contained (no external CSS).
* PPTX → not implemented (the only S6 use case is the whitepaper).
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar, Dict, List, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.reports.blocks.base import ContentBlock
from services.reports.spec import RenderContext, RenderedSection, SectionSpec

logger = logging.getLogger("api.services.reports.blocks.two_column_layout")

# The block_id is also used inside the nesting check below; keep it as a
# module-level constant to avoid drift if the class name ever changes.
_BLOCK_ID = "two_column_layout"


class TwoColumnLayoutParams(BaseModel):
    """Parameters for the ``two_column_layout`` block.

    ``left`` / ``right`` are themselves lists of ``SectionSpec`` — i.e.
    the same wire format the top-level ``ReportDescriptor.sections``
    uses. The orchestrator runs each child through its own block via
    ``ReportService._render_sections``.

    ``ratio`` defines the column widths as fractions of the available
    width and must sum to 1.0 (within float tolerance).

    ``gap_em`` is the inter-column gap. Translated to ``\\hspace`` for
    LaTeX and the CSS ``gap`` property for HTML.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    left: List[SectionSpec]
    right: List[SectionSpec]
    ratio: Tuple[float, float] = Field(default=(0.5, 0.5))
    gap_em: float = Field(default=1.0, ge=0.0)

    @field_validator("ratio")
    @classmethod
    def _ratio_sums_to_one(cls, v: Tuple[float, float]) -> Tuple[float, float]:
        # Both fractions must be > 0 (a 0-width column is silly) and the
        # pair must sum to 1.0 within a small float tolerance — we don't
        # want a 0.4/0.4 typo to silently render with an inferred 0.2 gap.
        a, b = v
        if a <= 0.0 or b <= 0.0:
            raise ValueError(
                f"two_column_layout.ratio fractions must be > 0, "
                f"got {v!r}"
            )
        if abs((a + b) - 1.0) > 1e-6:
            raise ValueError(
                f"two_column_layout.ratio fractions must sum to 1.0, "
                f"got {v!r} (sum={a + b!r})"
            )
        return v


# Pydantic v2 needs an explicit rebuild because ``SectionSpec`` is
# imported under ``from __future__ import annotations`` and would
# otherwise stay a forward reference until first use.
TwoColumnLayoutParams.model_rebuild()


def _markup_str(section: RenderedSection) -> str:
    """Coerce a child's ``markup`` to ``str``.

    LaTeX/HTML render methods always return ``str``; PPTX returns
    ``dict``. The layout block only supports the str-yielding formats
    (latex, html), so this helper raises a clear error if a non-str
    payload sneaks through (e.g. a hand-built test using PPTX).
    """
    if isinstance(section.markup, str):
        return section.markup
    raise TypeError(
        f"two_column_layout: child section {section.section_id!r} "
        f"produced non-string markup ({type(section.markup).__name__}); "
        f"two_column_layout only supports formats whose blocks emit "
        f"strings (latex, html)."
    )


class TwoColumnLayoutBlock(ContentBlock):
    """Container block: render two lists of sections side-by-side.

    See module docstring for the recursion / depth-cap contract.
    """

    block_id: ClassVar[str] = _BLOCK_ID
    Parameters: ClassVar[type[BaseModel]] = TwoColumnLayoutParams

    # ------------------------------------------------------------------
    # collect() — re-enter the orchestrator to materialise both columns.
    # ------------------------------------------------------------------

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, TwoColumnLayoutParams)

        # ----- 1. Block-level nesting rejection ------------------------
        # ADR-03 S6 addendum: depth=1 is the only level supported. The
        # orchestrator also checks ``ctx.recursion_depth`` as a backstop,
        # but spotting the violation here yields a much friendlier
        # diagnostic ("two_column_layout cannot nest inside itself")
        # than the generic depth-overflow message.
        for child in (*params.left, *params.right):
            if child.block == _BLOCK_ID:
                raise ValueError(
                    "two_column_layout cannot nest inside itself "
                    "(S6 depth=1 cap)"
                )

        # ----- 2. Pull the orchestrator handle off the context. -------
        # ``ReportService.render`` injects this; the block is unusable
        # in a one-off "render block in isolation" test path that
        # bypasses the service. Fail loud with a self-explanatory
        # message rather than crashing with AttributeError later.
        report_service = ctx.extra.get("report_service")
        if report_service is None:
            raise RuntimeError(
                "two_column_layout block requires "
                "`ctx.extra['report_service']` to be set; ReportService "
                "normally injects this automatically."
            )

        # ----- 3. Recurse into the orchestrator for each column. ------
        # ``_render_sections`` honours ``visible_in`` for nested children
        # too (the same gate as top-level sections), so a child marked
        # ``visible_in=["html"]`` will be skipped in the LaTeX render —
        # exactly as it would be at the top level.
        #
        # Bump ``ctx.recursion_depth`` for the duration of the nested
        # call so the orchestrator's depth check fires if a deeper
        # container slips past the block-level check above. Restore it
        # afterwards so the outer iteration sees the original depth.
        original_depth = ctx.recursion_depth
        ctx.recursion_depth = original_depth + 1
        try:
            left_rendered = await report_service._render_sections(
                params.left, ctx
            )
            right_rendered = await report_service._render_sections(
                params.right, ctx
            )
        finally:
            ctx.recursion_depth = original_depth

        return {
            "left": left_rendered,
            "right": right_rendered,
            "ratio": params.ratio,
            "gap_em": params.gap_em,
        }

    # ------------------------------------------------------------------
    # render_latex — two minipages joined by \hspace.
    # ------------------------------------------------------------------

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        assert isinstance(params, TwoColumnLayoutParams)

        left_sections: List[RenderedSection] = data["left"]
        right_sections: List[RenderedSection] = data["right"]
        left_markup = "".join(_markup_str(s) for s in left_sections)
        right_markup = "".join(_markup_str(s) for s in right_sections)

        ratio_left, ratio_right = params.ratio
        gap_em = params.gap_em

        # ``[t]`` aligns the top of each minipage so columns of
        # different heights line up at the top edge — the natural
        # reading order for side-by-side text. ``\hspace`` keeps the
        # two minipages from butting up against each other.
        return (
            f"\\begin{{minipage}}[t]{{{ratio_left}\\textwidth}}"
            f"{left_markup}"
            f"\\end{{minipage}}"
            f"\\hspace{{{gap_em}em}}"
            f"\\begin{{minipage}}[t]{{{ratio_right}\\textwidth}}"
            f"{right_markup}"
            f"\\end{{minipage}}"
        )

    # ------------------------------------------------------------------
    # render_html — CSS grid, inline styles for self-containment.
    # ------------------------------------------------------------------

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        assert isinstance(params, TwoColumnLayoutParams)

        left_sections: List[RenderedSection] = data["left"]
        right_sections: List[RenderedSection] = data["right"]
        left_markup = "".join(_markup_str(s) for s in left_sections)
        right_markup = "".join(_markup_str(s) for s in right_sections)

        # Inline styles keep the report a single self-contained file —
        # no external stylesheet to ship alongside the .html artefact
        # (matches the ADR-03 §4 HTMLRenderer goal).
        ratio_left_pct = params.ratio[0] * 100
        ratio_right_pct = params.ratio[1] * 100
        # ``%g`` strips trailing zeros so 50.0 -> 50, but keeps 33.3
        # intact. Saves us a few CSS bytes and reads cleaner in the
        # generated HTML.
        style = (
            f"display: grid; "
            f"grid-template-columns: {ratio_left_pct:g}% {ratio_right_pct:g}%; "
            f"gap: {params.gap_em:g}em;"
        )
        return (
            f'<div class="two-column" style="{style}">'
            f'<div class="col-left">{left_markup}</div>'
            f'<div class="col-right">{right_markup}</div>'
            f"</div>"
        )


__all__ = ["TwoColumnLayoutBlock", "TwoColumnLayoutParams"]
