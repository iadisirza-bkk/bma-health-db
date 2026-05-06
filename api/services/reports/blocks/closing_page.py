"""``closing_page`` block — the final page of a report.

S10 ("Template-First Reports"): the existing ``cover_page`` block grew
a ``mode="closing"`` toggle for a one-line "ขอบคุณ" finale, but the
template-first whitepaper deliverable wants something richer at the
end: an acknowledgments paragraph, a bullet list of references /
sources, and a contact stanza.

That richer page is reusable across descriptors (whitepaper, zone, …)
so it lives as its own block rather than as another mode of cover_page.
``cover_page(mode=closing)`` and ``closing_page`` are not mutually
exclusive — descriptors can use either; the difference is whether the
finale is purely ceremonial (cover_page) or carries appendix-style
content (closing_page).

The block is audience-agnostic on purpose: it closes every render.
"""
from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

from services.latex_utils import latex_escape
from services.reports.blocks.base import AudienceTarget, ContentBlock
from services.reports.spec import RenderContext


class _ClosingPageParams(BaseModel):
    """Parameters for the ``closing_page`` block.

    All fields are optional so a descriptor can opt in to the parts it
    cares about — a closing page with only an acknowledgment paragraph
    is a valid configuration; so is one with only a contact line. The
    block silently drops empty / absent sub-blocks rather than emitting
    blank LaTeX scaffolding.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Acknowledgments — a free-form Thai (and optional English) paragraph.
    acknowledgments_th: Optional[str] = None
    acknowledgments_en: Optional[str] = None

    # Contact line (organization name).
    contact_th: Optional[str] = None
    contact_en: Optional[str] = None

    # Email — language-agnostic; rendered as ``{email}`` verbatim.
    contact_email: Optional[str] = None

    # References / sources / works-cited list. Each entry renders as a
    # bullet item. Plain strings only — bibliographic structure (author /
    # year / title) is the descriptor author's job.
    references: List[str] = []

    # Section heading text — defaults to the Thai "ภาคผนวก" heading
    # which works as a closing-page label. Override with text_en for
    # English locales.
    heading_th: str = "ภาคผนวก"
    heading_en: Optional[str] = None


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _pick_lang(
    ctx: RenderContext, th: Optional[str], en: Optional[str]
) -> Optional[str]:
    """Pick th/en variant by ``ctx.lang`` with Thai fallback. Returns
    ``None`` only when both inputs are ``None`` (or the chosen variant
    is empty)."""
    if ctx.lang == "en" and en:
        return en
    return th


class ClosingPageBlock(ContentBlock):
    """Closing page — acknowledgments + references + contact.

    Audience-agnostic (closes every audience filter). Renders an
    un-numbered ``\\section*`` so it doesn't disturb the auto-numbered
    body sections (and therefore doesn't appear in the TOC twice).
    """

    block_id: ClassVar[str] = "closing_page"
    # ``None`` = render for every audience filter (closes ALL audiences).
    # Inherited default from the ABC, restated here for clarity.
    audience_target: ClassVar[Optional[AudienceTarget]] = None
    Parameters: ClassVar[type[BaseModel]] = _ClosingPageParams

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _ClosingPageParams)
        return {
            "heading": _pick_lang(ctx, params.heading_th, params.heading_en)
            or "ภาคผนวก",
            "acknowledgments": _pick_lang(
                ctx, params.acknowledgments_th, params.acknowledgments_en
            ),
            "contact": _pick_lang(ctx, params.contact_th, params.contact_en),
            "contact_email": params.contact_email,
            "references": list(params.references),
        }

    # ------------------------------------------------------------------
    # LaTeX
    # ------------------------------------------------------------------

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        # ``\newpage`` pushes the closing onto its own sheet (legacy
        # whitepaper convention). ``\section*`` keeps it un-numbered so
        # the auto-numbering of the body stays untouched.
        parts: List[str] = [r"\newpage"]
        parts.append(
            r"\section*{" + latex_escape(str(data["heading"])) + "}"
        )

        # References — bullet list before the prose so readers know
        # what's cited above before the thank-you stanza.
        refs = data.get("references") or []
        if refs:
            parts.append(r"\begin{itemize}")
            for ref in refs:
                parts.append(r"\item " + latex_escape(str(ref)))
            parts.append(r"\end{itemize}")

        # Acknowledgments paragraph (multi-line YAML strings keep their
        # newlines — LaTeX collapses single newlines to spaces, double
        # newlines to paragraph breaks, which matches author intent).
        ack = data.get("acknowledgments")
        if ack:
            parts.append("")  # blank line forces a paragraph break
            parts.append(latex_escape(str(ack)))

        # Contact stanza — organization name on one line, email on the
        # next. Email goes through ``\texttt`` so monospaced font marks
        # it as machine-readable.
        contact = data.get("contact")
        email = data.get("contact_email")
        if contact or email:
            parts.append(r"\vspace{1em}")
            if contact:
                parts.append(latex_escape(str(contact)) + r" \\")
            if email:
                # ``\texttt`` + escape — ``@`` and ``.`` are LaTeX-safe but
                # we still pipe through latex_escape for symmetric defence.
                parts.append(
                    r"\texttt{" + latex_escape(str(email)) + "}"
                )
        return "\n".join(parts) + "\n"

    # ------------------------------------------------------------------
    # HTML
    # ------------------------------------------------------------------

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        pieces: List[str] = []
        heading = _html_escape(str(data["heading"]))
        pieces.append(f"<h2>{heading}</h2>")

        refs = data.get("references") or []
        if refs:
            items = "".join(
                f"<li>{_html_escape(str(r))}</li>" for r in refs
            )
            pieces.append(f"<ul>{items}</ul>")

        ack = data.get("acknowledgments")
        if ack:
            pieces.append(f"<p>{_html_escape(str(ack))}</p>")

        contact = data.get("contact")
        email = data.get("contact_email")
        if contact or email:
            cparts: List[str] = []
            if contact:
                cparts.append(_html_escape(str(contact)))
            if email:
                e = _html_escape(str(email))
                # Trusted descriptor field — safe to wrap as a mailto link.
                cparts.append(f'<a href="mailto:{e}">{e}</a>')
            pieces.append(
                '<p class="closing-contact">'
                + "<br/>".join(cparts)
                + "</p>"
            )

        body = "".join(pieces)
        return f'<section class="closing-page">{body}</section>'
