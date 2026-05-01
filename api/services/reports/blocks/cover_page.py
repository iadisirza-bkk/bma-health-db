"""``cover_page`` block — the title-page section of a report.

Per ADR-03 §3 this is the canonical "first page" block: title (from the
descriptor), optional subtitle (from params), generation date (from
params or ``ctx.requested_at``), optional logo. Used by every report
flavor — both whitepaper-style PDFs and HTML dashboards.
"""
from __future__ import annotations

from typing import Any, ClassVar, Dict, Optional

from pydantic import BaseModel, ConfigDict

from services.latex_utils import latex_escape
from services.reports.blocks.base import ContentBlock
from services.reports.spec import RenderContext


class _CoverPageParams(BaseModel):
    """Parameters for the ``cover_page`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    subtitle_th: Optional[str] = None
    subtitle_en: Optional[str] = None
    generation_date: Optional[str] = None
    logo_path: Optional[str] = None


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class CoverPageBlock(ContentBlock):
    """Title page block — title, subtitle, date, optional logo."""

    block_id: ClassVar[str] = "cover_page"
    Parameters: ClassVar[type[BaseModel]] = _CoverPageParams

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _CoverPageParams)
        # Pick title from descriptor; fall back to Thai if EN missing.
        if ctx.lang == "en" and ctx.descriptor.title_en:
            title = ctx.descriptor.title_en
        else:
            title = ctx.descriptor.title_th
        # Same fallback rule on subtitle.
        if ctx.lang == "en" and params.subtitle_en:
            subtitle = params.subtitle_en
        else:
            subtitle = params.subtitle_th
        # Generation date: explicit param wins; otherwise derive from
        # ctx.requested_at so cached renders stay deterministic.
        date_str = params.generation_date or ctx.requested_at.strftime(
            "%Y-%m-%d"
        )
        return {
            "title_th": ctx.descriptor.title_th,
            "title_en": ctx.descriptor.title_en,
            "title": title,
            "subtitle": subtitle,
            "generation_date_str": date_str,
            "logo_path": params.logo_path,
            "lang": ctx.lang,
        }

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        # ``\title{...}`` + ``\maketitle`` is the conventional LaTeX cover.
        # We assemble each piece separately so a missing subtitle / logo
        # simply omits its line instead of producing ``\subtitle{}``.
        parts: list[str] = []
        if data.get("logo_path"):
            # ``\includegraphics`` references a file path — escape only
            # for safety; LaTeX accepts most filename chars verbatim.
            parts.append(
                r"\begin{center}"
                r"\includegraphics[width=0.4\textwidth]{"
                + data["logo_path"]
                + "}"
                + r"\end{center}"
            )
        parts.append(r"\title{" + latex_escape(str(data["title"])) + "}")
        if data.get("subtitle"):
            # Subtitle goes into the ``\author`` slot — LaTeX article
            # class doesn't have a first-class subtitle command and
            # adding a custom one is overkill for one block.
            parts.append(
                r"\author{" + latex_escape(str(data["subtitle"])) + "}"
            )
        parts.append(
            r"\date{" + latex_escape(str(data["generation_date_str"])) + "}"
        )
        parts.append(r"\maketitle")
        return "\n".join(parts) + "\n"

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        # Build the cover header. Each optional piece (subtitle, logo)
        # is gated separately so the HTML stays clean when omitted.
        pieces: list[str] = []
        if data.get("logo_path"):
            # ``logo_path`` is trusted (descriptor-controlled) — quote
            # it as an attribute value with html-escape.
            logo = _html_escape(str(data["logo_path"]))
            pieces.append(
                f'<img class="cover-logo" src="{logo}" alt="logo" />'
            )
        title = _html_escape(str(data["title"]))
        pieces.append(f'<h1 class="cover-title">{title}</h1>')
        if data.get("subtitle"):
            sub = _html_escape(str(data["subtitle"]))
            pieces.append(f'<p class="cover-subtitle">{sub}</p>')
        date = _html_escape(str(data["generation_date_str"]))
        pieces.append(f'<p class="cover-date">{date}</p>')
        body = "".join(pieces)
        return f'<header class="cover">{body}</header>'
