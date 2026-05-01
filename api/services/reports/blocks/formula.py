r"""``formula`` block — a single math equation, optionally captioned.

Per ADR-03 §3 this ports the legacy whitepaper template's
``\begin{equation}...\end{equation}`` blocks (e.g. the chi-square,
odds-ratio, Mann-Kendall formulas in the appendix) into a
descriptor-driven primitive. Authors supply the math expression as raw
LaTeX (e.g. ``OR = \frac{a/b}{c/d}``) and the block emits the
appropriate equation environment.

HTML rendering is intentionally minimal — we render the raw LaTeX
expression inside a ``<code>`` block. Real math rendering (MathJax /
KaTeX) is a follow-up: this block deliberately does NOT pull in a
browser-side math library because the existing report HTML output is
self-contained (no CDN loads, no external assets) and bundling KaTeX
would push us past 1 MB per report. When proper math rendering lands
the change is local to ``render_html`` here.
"""
from __future__ import annotations

from typing import Any, ClassVar, Dict, Optional

from pydantic import BaseModel, ConfigDict

from services.latex_utils import latex_escape
from services.reports.blocks.base import ContentBlock
from services.reports.spec import RenderContext


class _FormulaParams(BaseModel):
    """Parameters for the ``formula`` block.

    ``latex`` is passed through verbatim into the equation environment —
    it MUST be valid LaTeX math mode. The block does NOT escape this
    string (otherwise ``\\frac`` would become ``\\textbackslash{}frac``).
    Trust contract: descriptors are repo-controlled, not user input.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    latex: str
    caption_th: Optional[str] = None
    numbered: bool = True


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class FormulaBlock(ContentBlock):
    """A single LaTeX math equation, with optional caption + numbering."""

    block_id: ClassVar[str] = "formula"
    Parameters: ClassVar[type[BaseModel]] = _FormulaParams

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _FormulaParams)
        return {
            "latex": params.latex,
            "caption": params.caption_th,
            "numbered": params.numbered,
        }

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        expr = str(data["latex"])
        numbered = bool(data.get("numbered", True))
        env = "equation" if numbered else "equation*"
        # Equation environment is rendered as-is — the LaTeX inside is
        # trusted (descriptor-controlled). Caption placement matches the
        # legacy whitepaper template, which puts captions outside the
        # equation environment so they pick up the caption styling.
        out = (
            "\\begin{" + env + "}\n"
            + expr + "\n"
            + "\\end{" + env + "}\n"
        )
        caption = data.get("caption")
        if caption:
            out += (
                r"\begin{center}\small\textit{"
                + latex_escape(str(caption))
                + r"}\end{center}" + "\n"
            )
        return out

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        # Self-contained fallback: render the raw LaTeX as monospaced
        # code. Proper math rendering is a follow-up — see module
        # docstring. ``<code>`` is used over ``<pre>`` because formulas
        # fit on one line in nearly every real report and the centered
        # ``<div>`` wrapper handles the equation-style horizontal break.
        expr_html = _html_escape(str(data["latex"]))
        style = (
            "text-align: center; "
            "padding: 0.5em 0; "
            "font-family: Consolas, Menlo, monospace; "
            "margin: 0.5em 0;"
        )
        body = (
            f'<div class="formula" style="{style}">'
            f"<code>{expr_html}</code>"
            f"</div>"
        )
        caption = data.get("caption")
        if caption:
            cap_html = _html_escape(str(caption))
            body += f'<div class="formula-caption" style="text-align: center;"><small>{cap_html}</small></div>'
        return body
