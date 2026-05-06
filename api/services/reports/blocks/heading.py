"""``heading`` block — a single section heading at level 1/2/3/4.

Per ADR-03 §3 this is the simplest possible block: no data lookup, no
substitution, just a string in / formatted heading out. The block exists
because every report needs structural section breaks and we want them in
the same descriptor surface as everything else (no special-case escape
hatch).

S10 (Template-First Reports): the LaTeX renderer emits real
``\\section`` / ``\\subsection`` / ``\\subsubsection`` / ``\\paragraph``
commands so headings auto-number and register in
``\\tableofcontents``. Level falls outside the supported set → render a
bold paragraph instead of crashing (the descriptor surface still
constrains via Pydantic, but we keep the renderer defensive).
"""
from __future__ import annotations

from typing import Any, ClassVar, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict

from services.latex_utils import latex_escape
from services.reports.blocks.base import ContentBlock
from services.reports.spec import RenderContext


class _HeadingParams(BaseModel):
    """Parameters for the ``heading`` block.

    ``level`` is constrained to {1, 2, 3, 4}. Deeper nesting is a smell
    that usually means a section should have been split out into a
    sub-report, and ``\\subparagraph`` is rarely useful in print.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    text_th: str
    text_en: Optional[str] = None
    level: Literal[1, 2, 3, 4] = 1


# Map heading level → (LaTeX command, HTML tag). S10: extend to level 4
# (``\paragraph`` / ``<h4>``) so the whitepaper TOC has the depth it
# needs without touching the descriptor surface.
_LATEX_CMD = {
    1: r"\section",
    2: r"\subsection",
    3: r"\subsubsection",
    4: r"\paragraph",
}
_HTML_TAG = {1: "h1", 2: "h2", 3: "h3", 4: "h4"}


class HeadingBlock(ContentBlock):
    """One bare-scaffolding heading."""

    block_id: ClassVar[str] = "heading"
    Parameters: ClassVar[type[BaseModel]] = _HeadingParams

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        # mypy: params will always be a _HeadingParams at runtime — the
        # orchestrator parses the descriptor before calling collect.
        assert isinstance(params, _HeadingParams)
        # Resolve th/en text by ctx.lang. Fall back to Thai if the
        # English copy is missing — this matches the existing whitepaper
        # behavior where Thai is the canonical text.
        if ctx.lang == "en" and params.text_en:
            text = params.text_en
        else:
            text = params.text_th
        return {"text": text, "level": params.level}

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        # Defensive: descriptor-supplied level outside {1..4} should
        # never crash the compile — fall back to a bold paragraph that
        # reads roughly like a heading but doesn't claim a TOC slot.
        level = int(data["level"])
        cmd = _LATEX_CMD.get(level)
        text = latex_escape(data["text"])
        if cmd is None:
            return r"\textbf{" + text + r"}\\" + "\n"
        # Escape user copy so a stray ``%`` or ``$`` doesn't blow up the
        # LaTeX compile. No trailing newline — the orchestrator joins
        # blocks with its own separator and ``\section{}`` already
        # forces vertical space.
        return f"{cmd}{{{text}}}"

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        level = int(data["level"])
        tag = _HTML_TAG.get(level, "p")
        # Minimal HTML escape — Thai text never contains the dangerous
        # five chars but user-supplied english copy might.
        text = (
            data["text"]
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        if tag == "p":
            # Out-of-range level → bold paragraph fallback in HTML too.
            return f"<p><strong>{text}</strong></p>"
        return f"<{tag}>{text}</{tag}>"
