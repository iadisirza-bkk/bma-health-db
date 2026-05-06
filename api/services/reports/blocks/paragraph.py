"""``paragraph`` block — a single paragraph of bilingual text with
data substitution.

Per ADR-03 §3, this block is the bridge between a static template and a
data-driven section: copy authors put ``{var}`` placeholders in their
text, the block resolves them via dotted paths into
``ctx.data_collector.data()`` at render time. Anything more elaborate
(loops, conditionals) belongs in its own block class.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar, Dict, Optional

from pydantic import BaseModel, ConfigDict

from services.latex_utils import latex_escape
from services.reports.blocks.base import ContentBlock
from services.reports.polish import maybe_polish
from services.reports.spec import RenderContext

logger = logging.getLogger("api.services.reports.blocks.paragraph")


class _ParagraphParams(BaseModel):
    """Parameters for the ``paragraph`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    text_th: str
    text_en: Optional[str] = None


def _resolve_dotted(data: Any, path: str) -> Any:
    """Walk a dotted ``path`` (e.g. ``summary.total_screened``) into ``data``.

    Returns ``"{<path>}"`` when any segment is missing — this surfaces
    misnamed substitutions in the rendered output instead of failing
    silently. Same idea as Jinja's ``StrictUndefined`` but local to the
    paragraph block so a typo doesn't crash the entire report.
    """
    cur: Any = data
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif hasattr(cur, seg):
            cur = getattr(cur, seg)
        else:
            return "{" + path + "}"  # surface miss to the reader
    return cur


def _substitute(text: str, data: Any) -> str:
    """Substitute every ``{var}`` token in ``text`` with the resolved value.

    The grammar is intentionally tiny: ``{path.with.dots}``, no
    formatting spec (use the ``kpi_grid`` block when you need ``%``,
    thousands separators, …). Tokens that can't be resolved are left as
    literal ``{path}`` so authors notice them in the output.
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            close = text.find("}", i + 1)
            if close == -1:
                out.append(text[i:])
                break
            path = text[i + 1 : close]
            value = _resolve_dotted(data, path)
            out.append(str(value))
            i = close + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


class ParagraphBlock(ContentBlock):
    """One paragraph of static (or substituted) bilingual copy."""

    block_id: ClassVar[str] = "paragraph"
    Parameters: ClassVar[type[BaseModel]] = _ParagraphParams

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _ParagraphParams)
        if ctx.lang == "en" and params.text_en:
            raw = params.text_en
        else:
            raw = params.text_th
        # Resolve `data` lazily — duck-type both a method and a plain attr.
        getter = getattr(ctx.data_collector, "data", None)
        bag: Any = getter() if callable(getter) else (getter or {})
        text = _substitute(raw, bag)
        # S9: optional Gemma polish — runs only when ``polish_prose``
        # feature flag is on AND a polish_service is wired up. The polish
        # is hash-cached so identical (text, hint, lang) returns the same
        # output forever; same data → same prose.
        text = await maybe_polish(
            ctx,
            self.block_id,
            text,
            context_hint="paragraph block prose",
            lang=ctx.lang,
        )
        return {"text": text}

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        # Wrap in a paragraph break for clarity — Tectonic / xelatex will
        # collapse the trailing newline automatically.
        return latex_escape(data["text"]) + "\n\n"

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        text = (
            data["text"]
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return f"<p>{text}</p>"
