"""HTMLRenderer — single-file self-contained HTML report (ADR-03 §4).

The renderer trusts each ``RenderedSection.markup`` to already be valid
HTML produced by the block's ``render_html`` method (S4.4 ships the
chart/table blocks that emit inline SVG, etc.). All this module does is:

    1.  Wrap the per-section HTML fragments in a single Jinja2 root
        template (``templates/html/descriptor_html_root.html.j2``).
    2.  Inline every external asset (CSS, font, logo) so the resulting
        ``.html`` opens without network access — that's the ADR-03 §4
        contract for the HTML format. Charts are SVG already; the block
        is responsible for embedding them as ``<svg>``.
    3.  Write the result to ``out_path`` and return that path.

The renderer self-registers with the module-level ``renderer_registry()``
at import time, so ``bootstrap()`` (in ``services.reports``) only needs
``import services.reports.renderers.html``.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import jinja2

from services.reports import (
    RenderContext,
    RenderedSection,
    ReportDescriptor,
    ReportRenderer,
    renderer_registry,
)
from services.reports.renderers._filters import SHARED_FILTERS

logger = logging.getLogger("api.services.reports.renderers.html")

# Languages that read right-to-left. Anything else gets ``dir="ltr"``.
RTL_LANGS = {"ar", "he", "fa", "ur"}

# The template directory shipped with this renderer. The renderer also
# scans a few well-known locations for the Sarabun font file so the
# output can embed it as a ``data:`` URI.
TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "templates" / "html"

# Hunting order for a Sarabun font file. Picked to match developer
# laptops (~/Library/Fonts), Linux servers (/usr/share/fonts), the
# project's own ``fonts/`` directory, and a Docker bind mount. The first
# hit wins; if nothing matches we fall back to a system Thai font.
SARABUN_CANDIDATES = (
    "fonts/Sarabun-Regular.ttf",
    "fonts/Sarabun.ttf",
    "/usr/share/fonts/truetype/sarabun/Sarabun-Regular.ttf",
    str(Path.home() / "Library" / "Fonts" / "Sarabun-Regular.ttf"),
    str(Path.home() / ".fonts" / "Sarabun-Regular.ttf"),
)


class HTMLRenderer(ReportRenderer):
    """Strategy implementation for ``fmt = "html"``.

    Stateless aside from the Jinja2 environment, which is constructed
    once at ``__init__`` and reused for every ``render()`` call.
    """

    fmt = "html"

    def __init__(self, template_dir: Optional[Path] = None) -> None:
        # HTML is a friendly target — no LaTeX-style delimiter override
        # is needed. Default ``{{ }}`` / ``{% %}`` Jinja2 syntax keeps
        # the templates legible. ``autoescape=True`` so any non-block
        # context value is XSS-safe; block markup that needs to pass
        # through raw uses ``| safe`` (see the root template).
        self.template_dir = Path(template_dir) if template_dir else TEMPLATE_DIR
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(self.template_dir)),
            autoescape=jinja2.select_autoescape(["html", "j2", "html.j2"]),
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        # Shared formatters live in ``_filters.py`` so the LaTeX side
        # uses the exact same number / pct rounding (otherwise the PDF
        # and the HTML version of the same report would silently drift
        # at the third decimal). ``_latex_filters`` imports from there
        # too.
        for name, fn in SHARED_FILTERS.items():
            self.env.filters[name] = fn

    # ------------------------------------------------------------------
    # Public ABC method
    # ------------------------------------------------------------------

    def render(
        self,
        desc: ReportDescriptor,
        sections: List[RenderedSection],
        ctx: RenderContext,
        out_path: Path,
    ) -> Path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        template = self.env.get_template("descriptor_html_root.html.j2")
        html = template.render(**self._build_context(desc, sections, ctx))
        out_path.write_text(html, encoding="utf-8")
        logger.info(
            "HTMLRenderer.render: wrote %d bytes to %s",
            len(html),
            out_path,
        )
        return out_path

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_context(
        self,
        desc: ReportDescriptor,
        sections: List[RenderedSection],
        ctx: RenderContext,
    ) -> Dict[str, Any]:
        lang = ctx.lang
        section_titles = self._section_titles(desc, sections, lang)
        data_hash = ctx.extra.get("data_hash", "")
        app_version = ctx.extra.get("app_version", "BMA Health Database")
        generated_at = ctx.requested_at or datetime.now(timezone.utc)
        return {
            "desc": desc,
            "sections": sections,
            "ctx": ctx,
            "style": desc.style,
            "lang": lang,
            "lang_dir": "rtl" if lang in RTL_LANGS else "ltr",
            "generated_at": generated_at.isoformat(timespec="seconds"),
            "data_hash": data_hash,
            "app_version": app_version,
            "logo_data_uri": _logo_data_uri(desc.style.logo_path),
            "font_face_css": _font_face_css(desc.style.font_family),
            "section_titles": section_titles,
        }

    def _section_titles(
        self,
        desc: ReportDescriptor,
        sections: List[RenderedSection],
        lang: str,
    ) -> Dict[str, str]:
        """Resolve the human-readable label for each section.

        Priority:
            1. ``SectionSpec.title_th`` (Thai default — descriptors are
               authored in Thai first).
            2. ``s.section_id`` as a fallback so the TOC always renders.

        The descriptor and the rendered sections may diverge in length if
        a block opted out via ``visible_in``; we walk both and only emit
        TOC entries for sections actually rendered.
        """
        spec_by_id = {sec.id: sec for sec in desc.sections}
        out: Dict[str, str] = {}
        for s in sections:
            spec = spec_by_id.get(s.section_id)
            if spec is not None and spec.title_th:
                out[s.section_id] = spec.title_th
            else:
                out[s.section_id] = s.section_id
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _logo_data_uri(logo_path: Optional[str]) -> Optional[str]:
    """Resolve ``style.logo_path`` to a ``data:`` URI, or ``None``.

    A non-existent or unreadable path is treated as "no logo" — the
    template hides the ``<img>`` element when this returns ``None`` so
    the report still renders cleanly. We never raise; a missing logo is
    not a blocking error.
    """
    if not logo_path:
        return None
    p = Path(logo_path)
    if not p.is_file():
        logger.debug("HTMLRenderer: logo not found at %s", p)
        return None
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    try:
        b = p.read_bytes()
    except OSError as exc:
        logger.warning("HTMLRenderer: failed to read logo %s: %s", p, exc)
        return None
    encoded = base64.b64encode(b).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _font_face_css(font_family: str) -> str:
    """Return ``@font-face`` CSS embedding Sarabun if available, else ''.

    The fallback strategy is layered:
        1. Embed Sarabun as a base64 ``data:`` URI when we find it on
           disk — the resulting HTML opens correctly with no network
           access (per ADR-03 §4 self-contained requirement).
        2. If no Sarabun file is found, return an empty string and let
           the system fall back via the ``font-family`` declaration in
           the root template (which lists ``"Noto Sans Thai"`` and
           generic ``sans-serif`` after the requested family).
    """
    if font_family.lower() != "sarabun":
        # The descriptor asked for something else — let the system pick
        # it up; we don't try to embed arbitrary fonts.
        return ""
    for candidate in SARABUN_CANDIDATES:
        p = Path(candidate)
        if p.is_file():
            try:
                encoded = base64.b64encode(p.read_bytes()).decode("ascii")
            except OSError:
                continue
            mime = "font/ttf" if p.suffix.lower() == ".ttf" else "font/otf"
            return (
                "@font-face{"
                f"font-family:'{font_family}';"
                "font-style:normal;font-weight:400;"
                f"src:url(data:{mime};base64,{encoded}) "
                f"format('{('truetype' if p.suffix.lower() == '.ttf' else 'opentype')}');"
                "}"
            )
    logger.debug(
        "HTMLRenderer: Sarabun not found in any of %s — falling back to "
        "system Thai font",
        SARABUN_CANDIDATES,
    )
    return ""


# ---------------------------------------------------------------------------
# Self-registration. Importing this module is enough to make HTMLRenderer
# available via ``renderer_registry().get("html")``.
# ---------------------------------------------------------------------------
renderer_registry().register(HTMLRenderer())
