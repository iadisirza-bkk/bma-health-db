"""``ai_insight`` block — short LLM-generated Thai prose about the report.

Per ADR-03 §3 this block bridges the descriptor layer to the chat /
agent layer (ADR-02). The block formats a Thai prompt template against
a slice of the collected report data, calls
``ChatService.chat(thread_id=None, user_message=prompt)``, and emits the
returned text as a quoted block in the rendered output.

Caller contract
---------------
The orchestrator MUST inject a ``ChatService``-shaped instance via
``ctx.extra["chat_service"]`` if AI insights are wanted; else this
block falls back to ``params.fallback_text_th``. There is no
``services.chat.service.get_chat_service`` factory today
(``routers/chat_v2.py`` builds one through FastAPI ``Depends``), so the
block does NOT lazy-import the production wiring — the bootstrap glue
is the orchestrator's job, not the block's.

Sanitisation
------------
LLM output is treated as untrusted: every character of the response is
passed through ``latex_escape`` (LaTeX) or ``html.escape`` (HTML)
before emission. A response containing ``\\input{evil}`` will render as
the literal text, never as an unsafe LaTeX macro.

Caching
-------
Within a single report render the same prompt is unlikely to be issued
twice — but defensive caching is cheap. ``ctx.extra['__ai_insight_cache']``
(a plain dict) keys hashed prompt → cached ``{text, source}`` so a
descriptor that reuses one prompt across multiple sections fires the
LLM exactly once. If the orchestrator does not initialise the bucket,
this block creates it lazily.

Logging
-------
LLM calls are logged at ``INFO`` with the first 100 chars of the prompt
and the response length only. The full prompt + full response are NEVER
logged (PII risk — the synthesised prompt contains aggregated health
metrics that could be re-identifying when joined with other logs).
"""
from __future__ import annotations

import hashlib
import html
import logging
from typing import Any, ClassVar, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.latex_utils import latex_escape
from services.reports.blocks.base import ContentBlock
from services.reports.spec import RenderContext

logger = logging.getLogger("api.services.reports.blocks.ai_insight")


# Cache bucket name on ``ctx.extra``. Underscored so it doesn't collide
# with descriptor-author-controlled keys.
_CACHE_KEY = "__ai_insight_cache"


class AiInsightParams(BaseModel):
    """Parameters for the ``ai_insight`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    prompt_template_th: str
    """A ``str.format``-style template, e.g.
    ``"สรุปข้อมูลสุขภาพของเขต {zone_code} ใน 3 ประเด็น..."``.
    Substituted ONLY with values from ``data_collector.data()``."""

    data_keys: List[str] = Field(default_factory=list)
    """Top-level keys of ``data_collector.data()`` to forward into the
    prompt's ``str.format`` substitution. Restricted to first-level keys
    (no dotted paths, no env-var lookup, no attr traversal) — keeps the
    LLM-prompt template surface minimal and prevents accidental leakage
    of unrelated globals."""

    max_tokens: int = 500
    """Hint to the orchestrator for response length. Honoured if the
    chat service exposes the parameter; ignored otherwise."""

    temperature: float = 0.2
    """Hint to the orchestrator for sampling temperature. Honoured if
    the chat service exposes the parameter; ignored otherwise."""

    fallback_text_th: str = "ข้อมูลเชิงลึกไม่พร้อมใช้งานในขณะนี้"
    """Static Thai paragraph used when no chat_service is injected and
    the lazy-import path also fails (or the LLM call raises)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_chat_service(ctx: RenderContext) -> Any:
    """Return a chat-service-shaped instance or ``None``.

    Order of preference:
        1. ``ctx.extra["chat_service"]`` — the orchestrator's injected
           handle (test seam + production glue).
        2. A lazy-imported orchestrator (``agents.create_orchestrator``)
           wrapped in a tiny adapter so its ``.process()`` method walks
           like ``ChatService.chat``. Per the task spec there is NO
           ``get_chat_service`` factory today — the legacy orchestrator
           is the only fallback we can reach without FastAPI internals.

    Returns ``None`` if neither path yields a usable service. The block
    treats ``None`` as the "use ``fallback_text_th``" signal.
    """
    pre = ctx.extra.get("chat_service") if ctx.extra else None
    if pre is not None:
        return pre
    try:
        # Same import idiom as ``routers/chat_v2.py`` — the only known
        # entry point to the orchestrator that doesn't rely on FastAPI
        # ``Depends``. We wrap it so callers see the ``ChatService.chat``
        # contract (``thread_id`` arg + dict return).
        from agents import create_orchestrator

        orch = create_orchestrator()

        class _OneShotAdapter:
            """Orchestrator wrapped to look like ``ChatService.chat``."""

            async def chat(
                self,
                thread_id: Any,  # ignored — orchestrator is stateless here
                user_message: str,
            ) -> Dict[str, Any]:
                return await orch.process(user_message)

        return _OneShotAdapter()
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "ai_insight: no chat_service available "
            "(extra absent + lazy import failed: %s)",
            type(exc).__name__,
        )
        return None


def _build_data_subset(
    data_collector: Any, data_keys: List[str]
) -> Dict[str, Any]:
    """Pull ``data_keys`` from ``data_collector.data()`` for substitution.

    ONLY top-level keys of the data bag are honoured. Keys absent from
    ``data()`` resolve to the literal ``"{key}"`` so that a typo in the
    descriptor surfaces in the rendered prose instead of crashing the
    report (same surface-the-miss philosophy as ``paragraph._substitute``).
    """
    getter = getattr(data_collector, "data", None)
    bag: Any = getter() if callable(getter) else (getter or {})
    if not isinstance(bag, dict):
        return {k: f"{{{k}}}" for k in data_keys}
    out: Dict[str, Any] = {}
    for k in data_keys:
        if k in bag:
            out[k] = bag[k]
        else:
            out[k] = f"{{{k}}}"  # surface-the-miss
    return out


def _format_prompt(
    template: str, subset: Dict[str, Any]
) -> str:
    """Substitute ``{key}`` placeholders in ``template`` with ``subset``.

    Falls back to a defensive return of the unmodified template when
    formatting raises (unbalanced braces, KeyError on a key not in
    ``subset``). The caller logs the failure and the LLM still gets a
    sensible prompt rather than crashing the report.
    """
    try:
        return template.format(**subset)
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning(
            "ai_insight: prompt template substitution failed (%s) — "
            "using template verbatim",
            type(exc).__name__,
        )
        return template


def _cache_key(prompt: str) -> str:
    """Stable cache key from the prompt — sha256, first 16 hex chars."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


async def _call_chat_service(
    chat_service: Any,
    prompt: str,
    *,
    max_tokens: int,
    temperature: float,
) -> str:
    """Invoke ``chat_service.chat`` and extract the response text.

    The chat service may not expose ``max_tokens`` / ``temperature``
    kwargs (the production ``ChatService.chat`` signature is
    ``(thread_id, user_message)``); we attempt a kwarg-rich call first
    and fall back to the minimal signature on TypeError. This keeps the
    block forward-compatible with future signature growth without
    duck-typing.
    """
    try:
        result = await chat_service.chat(
            thread_id=None,
            user_message=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except TypeError:
        # Fall back to the canonical ChatService.chat signature.
        result = await chat_service.chat(
            thread_id=None,
            user_message=prompt,
        )
    if isinstance(result, dict):
        return str(result.get("content", "") or "")
    if isinstance(result, str):
        return result
    return str(result)


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------


class AiInsightBlock(ContentBlock):
    """One short AI-generated Thai paragraph synthesised from report data.

    Caller must inject ``chat_service`` via ``ctx.extra['chat_service']``
    if AI insights are wanted; else falls back to a static string
    (``params.fallback_text_th``).
    """

    block_id: ClassVar[str] = "ai_insight"
    Parameters: ClassVar[type[BaseModel]] = AiInsightParams

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, AiInsightParams)

        # 1. Build the prompt by substituting top-level data_keys into
        # the Thai template.
        subset = _build_data_subset(
            ctx.data_collector, params.data_keys
        )
        prompt = _format_prompt(params.prompt_template_th, subset)

        # 2. Cache lookup. The orchestrator may have already populated
        # the bucket; create it lazily if not.
        cache: Dict[str, Dict[str, Any]] = ctx.extra.setdefault(
            _CACHE_KEY, {}
        )
        ckey = _cache_key(prompt)
        cached = cache.get(ckey)
        if cached is not None:
            logger.debug(
                "ai_insight: cache hit (key=%s, source=%s)",
                ckey,
                cached.get("source"),
            )
            # Return a fresh dict so renderers can't mutate the cache.
            return {
                "text": cached["text"],
                "source": cached["source"],
                "prompt_used": prompt,
            }

        # 3. Resolve the chat service. None = no LLM available.
        chat_service = _resolve_chat_service(ctx)
        if chat_service is None:
            text = params.fallback_text_th
            source = "fallback"
        else:
            try:
                logger.info(
                    "ai_insight: calling chat service (prompt[:100]=%r)",
                    prompt[:100],
                )
                text = await _call_chat_service(
                    chat_service,
                    prompt,
                    max_tokens=params.max_tokens,
                    temperature=params.temperature,
                )
                logger.info(
                    "ai_insight: chat service returned (response_len=%d)",
                    len(text),
                )
                if not text:
                    # Empty response = treat as a soft failure → fallback.
                    logger.warning(
                        "ai_insight: chat service returned empty response — "
                        "using fallback_text_th"
                    )
                    text = params.fallback_text_th
                    source = "fallback"
                else:
                    source = "llm"
            except Exception as exc:  # noqa: BLE001
                # NEVER bubble up — the report should still render.
                logger.warning(
                    "ai_insight: chat service raised %s — using fallback",
                    type(exc).__name__,
                )
                text = params.fallback_text_th
                source = "fallback"

        # 4. Cache + return.
        cache[ckey] = {"text": text, "source": source}
        return {
            "text": text,
            "source": source,
            "prompt_used": prompt,
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
        # CRITICAL: latex_escape every char of the LLM output. A
        # response containing ``\input{evil}`` would otherwise expand
        # at compile time. We trust ``latex_escape`` to neutralise the
        # 10 LaTeX-special characters per ``services.latex_utils``.
        safe_text = latex_escape(str(data.get("text", "")))
        return (
            r"\begin{quote}"
            + "\n"
            + safe_text
            + "\n"
            + r"\end{quote}"
            + "\n"
            + r"\par\textit{\small ที่มา: AI สรุป (LMStudio)}"
            + "\n"
        )

    # ------------------------------------------------------------------
    # HTML
    # ------------------------------------------------------------------

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        # ``html.escape`` (with quote=True default) handles the five
        # dangerous chars (``& < > " '``) — the LLM cannot inject a
        # ``<script>`` tag through this surface.
        safe_text = html.escape(str(data.get("text", "")))
        return (
            f'<blockquote class="ai-insight">{safe_text}</blockquote>'
            f"<cite>ที่มา: AI สรุป</cite>"
        )
