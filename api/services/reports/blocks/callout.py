r"""``callout`` block — a tinted info / warn / example / note box.

Per ADR-03 §3 this block ports the legacy whitepaper template's
``\block{}`` / ``\alertblock{}`` macro semantics into a
descriptor-driven primitive: pick a ``kind`` and the block emits a
colour-tinted box (LaTeX ``\fcolorbox`` for the visual variants, italic
indented paragraph for the muted ``note`` kind).

The HTML rendering is intentionally self-contained — each ``<aside>``
carries inline ``style=""`` so the resulting report file works without a
separate stylesheet (every report we ship today is single-file).

Colour palette (matches the existing whitepaper preamble brand colours
``warnblue`` / ``warnamber`` / ``okgreen`` semantically). The LaTeX
``\fcolorbox`` form uses the standard xcolor ``blue!50``/``orange!50``/
``green!50`` tints because those are guaranteed available with bare
``\usepackage{xcolor}`` (no ``dvipsnames`` required) — the preamble
loads xcolor at line 17 of ``bma_article_preamble.tex``.
"""
from __future__ import annotations

from typing import Any, ClassVar, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict

from services.latex_utils import latex_escape
from services.reports.blocks.base import ContentBlock
from services.reports.spec import RenderContext


CalloutKind = Literal["info", "warn", "example", "note"]


class _CalloutParams(BaseModel):
    """Parameters for the ``callout`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    kind: CalloutKind
    title_th: Optional[str] = None
    text_th: str
    text_en: Optional[str] = None


# (LaTeX frame colour, LaTeX fill colour) per kind. ``note`` is rendered
# without a frame (italic indented paragraph) so it has no entry here.
_LATEX_TINTS: Dict[str, tuple[str, str]] = {
    "info": ("blue!50", "blue!10"),
    "warn": ("orange!50", "orange!10"),
    "example": ("green!50", "green!10"),
}

# (border colour, background colour) for the inline-styled HTML
# ``<aside>`` — kept self-contained so reports stay single-file.
_HTML_TINTS: Dict[str, tuple[str, str]] = {
    "info": ("#1565C0", "#E3F2FD"),
    "warn": ("#F57F17", "#FFF8E1"),
    "example": ("#388E3C", "#E8F5E9"),
    "note": ("#9E9E9E", "#FAFAFA"),
}


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class CalloutBlock(ContentBlock):
    """A coloured info / warn / example callout box, or muted note."""

    block_id: ClassVar[str] = "callout"
    Parameters: ClassVar[type[BaseModel]] = _CalloutParams

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _CalloutParams)
        # Resolve th/en text by ctx.lang — same fallback rule as
        # heading / paragraph blocks (Thai is canonical).
        if ctx.lang == "en" and params.text_en:
            text = params.text_en
        else:
            text = params.text_th
        return {
            "kind": params.kind,
            "title": params.title_th,  # title only authored in Thai today
            "text": text,
        }

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        kind = str(data["kind"])
        title_raw = data.get("title")
        text = latex_escape(str(data["text"]))
        title_block = ""
        if title_raw:
            title_block = (
                r"\textbf{" + latex_escape(str(title_raw)) + r"}\\" + "\n"
            )
        if kind == "note":
            # Italic indented paragraph — no frame. ``\par`` flushes the
            # paragraph cleanly so subsequent content doesn't wrap into
            # the indented block.
            inner = title_block + r"\textit{" + text + "}"
            return (
                r"\begin{quote}" + "\n"
                + inner + "\n"
                + r"\end{quote}\par" + "\n"
            )
        try:
            frame, fill = _LATEX_TINTS[kind]
        except KeyError:  # pragma: no cover — Literal guards this at parse
            frame, fill = ("gray!50", "gray!10")
        # ``\parbox`` lets the callout wrap multi-line text inside the
        # ``\fcolorbox``. ``\linewidth`` keeps it tied to the surrounding
        # column / page, not the whole page width.
        body = title_block + text
        return (
            r"\noindent\fcolorbox{" + frame + "}{" + fill + "}{%" + "\n"
            + r"\parbox{\dimexpr\linewidth-2\fboxsep-2\fboxrule\relax}{%" + "\n"
            + body + "%" + "\n"
            + r"}}\par\medskip" + "\n"
        )

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        kind = str(data["kind"])
        border, bg = _HTML_TINTS.get(kind, ("#9E9E9E", "#FAFAFA"))
        # Inline CSS so the report stays self-contained — same idea as
        # the chart block embedding raw SVG inline.
        style = (
            f"border-left: 4px solid {border}; "
            f"background: {bg}; "
            f"padding: 0.75em 1em; "
            f"margin: 0.5em 0;"
        )
        title_html = ""
        if data.get("title"):
            title_html = f"<strong>{_html_escape(str(data['title']))}</strong>"
        text_html = _html_escape(str(data["text"]))
        return (
            f'<aside class="callout callout-{kind}" style="{style}">'
            f"{title_html}<p>{text_html}</p>"
            f"</aside>"
        )
