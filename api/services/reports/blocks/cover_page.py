"""``cover_page`` block — the title-page section of a report.

Per ADR-03 §3 this is the canonical "first page" block: title (from the
descriptor), optional subtitle (from params), generation date (from
params or ``ctx.requested_at``), optional logo. Used by every report
flavor — both whitepaper-style PDFs and HTML dashboards.

Two modes:
    * ``mode="title"`` (default) — the front cover. Behaviour preserved
      byte-for-byte from the original implementation.
    * ``mode="closing"`` — a back-cover finale ("ขอบคุณ") used by the
      whitepaper template's last page.
"""
from __future__ import annotations

from typing import Any, ClassVar, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict

from services.latex_utils import latex_escape
from services.reports.blocks.base import ContentBlock
from services.reports.spec import RenderContext


CoverMode = Literal["title", "closing"]


class _CoverPageParams(BaseModel):
    """Parameters for the ``cover_page`` block.

    ``data_as_of`` (S9) is the data-freshness stamp — a short
    human-readable date string ("2026-05-01" or "1 พฤษภาคม 2569") that
    the cover renders as ``ข้อมูล ณ {data_as_of}`` directly under the
    title. Most callers leave this ``None`` and let the orchestrator
    compute it dynamically from ``MAX(bma_med.ingestion_batch.finished_at)``
    via ``ctx.feature_flags["data_as_of"]`` (a tiny piece of plumbing in
    ``routers/reports_v2.py``); descriptor authors can also override
    explicitly via the YAML.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    subtitle_th: Optional[str] = None
    subtitle_en: Optional[str] = None
    generation_date: Optional[str] = None
    logo_path: Optional[str] = None
    mode: CoverMode = "title"
    data_as_of: Optional[str] = None


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class CoverPageBlock(ContentBlock):
    """Title page block — title, subtitle, date, optional logo.

    With ``mode="closing"`` the same block emits a back-cover finale.
    """

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
        subtitle: Optional[str]
        if ctx.lang == "en" and params.subtitle_en:
            subtitle = params.subtitle_en
        else:
            subtitle = params.subtitle_th
        # Generation date: explicit param wins; otherwise derive from
        # ctx.requested_at so cached renders stay deterministic.
        date_str = params.generation_date or ctx.requested_at.strftime(
            "%Y-%m-%d"
        )
        # S9 freshness stamp: descriptor-supplied value wins; otherwise
        # fall back to the orchestrator's value plumbed through
        # ``ctx.feature_flags`` from the v2 router. ``None`` means "skip
        # the stamp" — older descriptors without S9 wiring keep
        # rendering byte-for-byte the same.
        data_as_of = params.data_as_of
        if not data_as_of and isinstance(ctx.feature_flags, dict):
            data_as_of = ctx.feature_flags.get("data_as_of")
        return {
            "title_th": ctx.descriptor.title_th,
            "title_en": ctx.descriptor.title_en,
            "title": title,
            "subtitle": subtitle,
            "generation_date_str": date_str,
            "logo_path": params.logo_path,
            "lang": ctx.lang,
            "mode": params.mode,
            "data_as_of": data_as_of or None,
        }

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        if data.get("mode") == "closing":
            return self._render_latex_closing(data)
        return self._render_latex_title(data)

    def _render_latex_title(self, data: Dict[str, Any]) -> str:
        # S10 — full cover layout now lives in the block (was duplicated
        # between the block and ``descriptor_latex_root.tex.j2`` titlepage,
        # producing two covers + a debug ``lang=th`` stamp). The root
        # template strips its own titlepage; we render a complete
        # ``\begin{titlepage}...\end{titlepage}`` with:
        #   * BMA + MSD logos at top (or caller-supplied ``logo_path``)
        #   * Huge title + Large English subtitle (when present)
        #   * Caller subtitle (e.g. "เขตสุขภาพ 01")
        #   * "ข้อมูล ณ <data_as_of>" freshness stamp
        #   * generation_date
        # Mirrors ``report_whitepaper.tex.j2`` lines 55-76 layout.
        parts: list[str] = [r"\begin{titlepage}", r"\centering", r"\vspace*{1cm}"]

        # --- Logos: caller-supplied path wins; otherwise default to the
        # BMA + MSD pair that the renderer's ``_stage_assets`` copies into
        # ``assets/`` for every build.
        logo_path = data.get("logo_path")
        if logo_path:
            parts.append(
                r"\includegraphics[height=2.5cm]{" + str(logo_path) + r"}"
            )
        else:
            parts.append(
                r"\includegraphics[height=2.5cm]{assets/bma_logo.png}"
                r"\hspace{2cm}"
                r"\includegraphics[height=2.5cm]{assets/msd_logo.png}"
            )
        parts.append(r"\vspace{1.5cm}")
        parts.append("")

        # --- Title block (Huge, BMA-green) + optional title_en ---
        title_th = latex_escape(str(data["title_th"] or data.get("title", "")))
        parts.append(
            r"{\Huge\bmafont\bfseries\color{bmagreen} "
            + title_th
            + r" \par}"
        )
        title_en = data.get("title_en")
        if title_en:
            parts.append(r"\vspace{0.5cm}")
            parts.append(
                r"{\Large\bmafont\color{bmadark} "
                + latex_escape(str(title_en))
                + r" \par}"
            )

        # --- Caller subtitle (e.g. "เขตสุขภาพ 01") ---
        subtitle = data.get("subtitle")
        if subtitle:
            parts.append(r"\vspace{0.5cm}")
            parts.append(
                r"{\large " + latex_escape(str(subtitle)) + r" \par}"
            )

        parts.append(r"\vfill")

        # --- Freshness stamp (S9) ---
        if data.get("data_as_of"):
            stamp = "ข้อมูล ณ " + str(data["data_as_of"])
            parts.append(
                r"{\small " + latex_escape(stamp) + r" \par}"
            )
            parts.append(r"\vspace{0.3cm}")

        # --- Generation date ---
        parts.append(
            r"{\large "
            + latex_escape(str(data["generation_date_str"]))
            + r" \par}"
        )
        parts.append(r"\end{titlepage}")
        return "\n".join(parts) + "\n"

    def _render_latex_closing(self, data: Dict[str, Any]) -> str:
        # Article-mode finale — a centered "ขอบคุณ" section with optional
        # subtitle. We use ``\section*`` (un-numbered) so it doesn't
        # appear in the table of contents and ``\newpage`` to push the
        # finale onto its own sheet (legacy whitepaper convention).
        parts: list[str] = [r"\newpage", r"\begin{center}"]
        parts.append(r"\section*{\Huge ขอบคุณ}")
        if data.get("subtitle"):
            parts.append(
                r"{\large "
                + latex_escape(str(data["subtitle"]))
                + r"\par}"
            )
        parts.append(r"\end{center}")
        return "\n".join(parts) + "\n"

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        if data.get("mode") == "closing":
            return self._render_html_closing(data)
        return self._render_html_title(data)

    def _render_html_title(self, data: Dict[str, Any]) -> str:
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
        # S9 freshness stamp — adjacent to the date line.
        if data.get("data_as_of"):
            stamp = _html_escape("ข้อมูล ณ " + str(data["data_as_of"]))
            pieces.append(f'<p class="cover-data-as-of">{stamp}</p>')
        body = "".join(pieces)
        return f'<header class="cover">{body}</header>'

    def _render_html_closing(self, data: Dict[str, Any]) -> str:
        pieces: list[str] = ['<h1>ขอบคุณ</h1>']
        if data.get("subtitle"):
            sub = _html_escape(str(data["subtitle"]))
            pieces.append(f"<p>{sub}</p>")
        body = "".join(pieces)
        return f'<section class="cover cover-closing">{body}</section>'
