"""Anthropic (Claude) LLM adapter.

Conforms to the LLMAdapter ABC (api/agents/adapters/base.py). Translates the
OpenAI-style message/tool format the orchestrator already speaks into Anthropic's
Messages API and back, so swapping providers is a YAML edit (per ADR-02 §1).

Tool calls returned to the orchestrator are normalized into the OpenAI shape:
    {"id": ..., "type": "function",
     "function": {"name": ..., "arguments": "<json string>"}}
This matches what `agent_runner.run_single_turn` already consumes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncGenerator

from agents.adapters.base import AdapterConfig, LLMAdapter, LLMResponse

logger = logging.getLogger(__name__)


# --- Optional SDK import ------------------------------------------------------
# The official anthropic SDK is an optional dependency. We let the module import
# even when it's missing so the adapter class can be referenced (e.g. for
# registration / typing) — the actual constructor will raise a clear ImportError
# the first time someone tries to instantiate it without the SDK installed.
try:  # pragma: no cover - exercised by import-only tests
    import anthropic as _anthropic  # type: ignore[import-not-found]
    _SDK_AVAILABLE = True
    _SDK_IMPORT_ERROR: Exception | None = None
except ImportError as _exc:  # pragma: no cover
    _anthropic = None  # type: ignore[assignment]
    _SDK_AVAILABLE = False
    _SDK_IMPORT_ERROR = _exc


# Default model used for the cheap health probe and as a fallback when
# AdapterConfig.model is empty. Callers should set config.model explicitly.
_DEFAULT_MODEL = "claude-3-5-haiku-latest"


# stop_reason → finish_reason mapping (Anthropic → orchestrator-internal).
_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
}


def _translate_messages(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """Split OpenAI-style messages into (system_prompt, anthropic_messages).

    Anthropic's Messages API takes `system` as a top-level parameter, not a
    role inside `messages`. Multiple system messages are concatenated.

    Tool messages from the orchestrator (`role=tool`, `tool_call_id`, `content`)
    are translated into a `user` message with a `tool_result` content block,
    which is the shape Anthropic expects for tool responses.
    """
    sys_chunks: list[str] = []
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            sys_chunks.append(m.get("content") or "")
            continue
        if role == "tool":
            # Tool result — Anthropic packages this as a user message with a
            # tool_result content block. The id must match the prior tool_use.
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or "",
                    "content": m.get("content") or "",
                }],
            })
            continue
        if role == "assistant":
            # If the assistant message previously emitted tool_calls, replay
            # them as tool_use blocks so the conversation history makes sense
            # to Claude on the next turn.
            tool_calls = m.get("tool_calls") or []
            if tool_calls:
                blocks: list[dict] = []
                text = m.get("content") or ""
                if text:
                    blocks.append({"type": "text", "text": text})
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    args_raw = fn.get("arguments") or "{}"
                    try:
                        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id") or "",
                        "name": fn.get("name") or "",
                        "input": args,
                    })
                out.append({"role": "assistant", "content": blocks})
                continue
            out.append({"role": "assistant", "content": m.get("content") or ""})
            continue
        # user (or anything else falls through to user)
        out.append({"role": "user", "content": m.get("content") or ""})

    system = "\n\n".join(c for c in sys_chunks if c) or None
    return system, out


def _translate_tools(tools: list[dict] | None) -> list[dict] | None:
    """Convert OpenAI-style tool defs to Anthropic's tool schema.

    Input shape (orchestrator/OpenAI):
        {"type": "function",
         "function": {"name": ..., "description": ..., "parameters": {...}}}
    Output shape (Anthropic):
        {"name": ..., "description": ..., "input_schema": {...}}
    """
    if not tools:
        return None
    out: list[dict] = []
    for t in tools:
        fn = t.get("function") or t  # accept already-flat shape too
        out.append({
            "name": fn.get("name") or "",
            "description": fn.get("description") or "",
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


class AnthropicAdapter(LLMAdapter):
    """Adapter for the Anthropic Claude Messages API.

    Constructed by ProviderRegistry from `config/llm/providers.yaml`. The
    api_key is supplied by the registry (resolved from `api_key_env`); we
    accept None so tests can construct the adapter without a real key — in
    that mode `health_check()` returns False and any chat/stream call returns
    an error LLMResponse instead of raising.
    """

    def __init__(self, config: AdapterConfig, *, api_key: str | None = None):
        super().__init__(config)
        if not _SDK_AVAILABLE:  # pragma: no cover - simple guard
            raise ImportError(
                "The 'anthropic' package is required for AnthropicAdapter. "
                "Install it with: pip install anthropic"
            )
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        # Build the async client lazily-ish: the SDK accepts api_key=None and
        # only fails on the first request, which is what we want for tests.
        self._client = _anthropic.AsyncAnthropic(
            api_key=self._api_key,
            timeout=float(config.timeout),
        )

    # -- health -----------------------------------------------------------------

    async def health_check(self) -> bool:
        """Return True iff a cheap call to the API succeeds within 5s.

        Without an api_key we never hit the network — return False directly so
        the orchestrator's circuit breaker treats Anthropic as unavailable.
        """
        if not self._api_key:
            return False
        try:
            # Prefer models.list (cheap, doesn't burn tokens). Fall back to
            # count_tokens if the SDK on this machine lacks .models.list.
            models = getattr(self._client, "models", None)
            if models is not None and hasattr(models, "list"):
                await asyncio.wait_for(models.list(limit=1), timeout=5.0)
                return True
            # Fallback path for older SDKs.
            await asyncio.wait_for(
                self._client.messages.count_tokens(
                    model=self.config.model or _DEFAULT_MODEL,
                    messages=[{"role": "user", "content": "ping"}],
                ),
                timeout=5.0,
            )
            return True
        except Exception as exc:
            logger.warning("Anthropic health_check failed: %s", type(exc).__name__)
            return False

    # -- chat -------------------------------------------------------------------

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        system, anth_msgs = _translate_messages(messages)
        anth_tools = _translate_tools(tools)

        kwargs: dict[str, Any] = {
            "model": self.config.model or _DEFAULT_MODEL,
            "messages": anth_msgs,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if system is not None:
            kwargs["system"] = system
        if anth_tools:
            kwargs["tools"] = anth_tools

        try:
            resp = await self._client.messages.create(**kwargs)
        except Exception as exc:
            # Wrap in a generic error LLMResponse — never raise raw provider
            # errors. The circuit breaker upstream will engage on repeated
            # failure-shaped responses.
            logger.warning("Anthropic chat failed: %s", type(exc).__name__)
            return LLMResponse(
                content=f"Anthropic call failed: {type(exc).__name__}",
                tool_calls=[],
                raw={"error": str(exc), "error_type": type(exc).__name__},
                finish_reason="error",
            )

        # Translate response.content blocks into our normalized shape.
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        # `content` is a list of blocks: TextBlock / ToolUseBlock / etc.
        for block in getattr(resp, "content", []) or []:
            btype = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
            if btype == "text":
                txt = getattr(block, "text", None)
                if txt is None and isinstance(block, dict):
                    txt = block.get("text", "")
                text_parts.append(txt or "")
            elif btype == "tool_use":
                bid = getattr(block, "id", None) or (block.get("id") if isinstance(block, dict) else None) or ""
                bname = getattr(block, "name", None) or (block.get("name") if isinstance(block, dict) else None) or ""
                binput = getattr(block, "input", None)
                if binput is None and isinstance(block, dict):
                    binput = block.get("input", {})
                tool_calls.append({
                    "id": bid,
                    "type": "function",
                    "function": {
                        "name": bname,
                        "arguments": json.dumps(binput or {}, ensure_ascii=False),
                    },
                })

        stop_reason = getattr(resp, "stop_reason", None) or "end_turn"
        finish_reason = _STOP_REASON_MAP.get(stop_reason, "stop")

        # Build a JSON-serialisable raw payload (model_dump if Pydantic-like).
        raw: dict
        if hasattr(resp, "model_dump"):
            try:
                raw = resp.model_dump()
            except Exception:
                raw = {"stop_reason": stop_reason}
        elif isinstance(resp, dict):
            raw = resp
        else:
            raw = {"stop_reason": stop_reason}

        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            raw=raw,
            finish_reason=finish_reason,
        )

    # -- stream -----------------------------------------------------------------

    async def stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        """Yield text deltas for the synthesizer path (no tools)."""
        system, anth_msgs = _translate_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.config.model or _DEFAULT_MODEL,
            "messages": anth_msgs,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if system is not None:
            kwargs["system"] = system

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                # SDK exposes .text_stream — an async iterator of plain text deltas.
                async for delta in stream.text_stream:
                    if delta:
                        yield delta
        except Exception as exc:
            logger.warning("Anthropic stream failed: %s", type(exc).__name__)
            # Surface the failure as a single yielded chunk so callers
            # (which just stream tokens to the client) see the error inline
            # instead of an empty stream.
            yield f"[Anthropic stream failed: {type(exc).__name__}]"


# --- Self-registration with the provider registry (ADR-02 §2) ----------------
# S3.1 owns ProviderRegistry; we coordinate via a module-level
# `_register_adapter(name, cls)` helper so the registry doesn't need to import
# every adapter eagerly. If the helper isn't there yet (S3.1 not landed) we
# silently skip — the adapter is still importable on its own.
try:  # pragma: no cover - registration glue
    from agents.providers import _register_adapter  # type: ignore[import-not-found]
    _register_adapter("anthropic", AnthropicAdapter)
except ImportError:
    pass
