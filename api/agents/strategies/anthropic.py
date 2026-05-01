"""Anthropic / Claude tool-use strategy.

Per ADR-02 §3, the StrategyRegistry maps model name → ToolCallStrategy. Claude's
tool_use blocks already arrive structured (no JSON-string-in-content like Gemma
needs), and AnthropicAdapter normalises them into the OpenAI-style
`{id, type, function: {name, arguments}}` shape before they reach the strategy.
So this strategy is essentially a passthrough — closer to OpenAINativeStrategy
than to GemmaToolCallStrategy.

Why have a strategy at all then? Two reasons:
1. The orchestrator's strategy dispatch expects a ToolCallStrategy for every
   non-LMStudio-Gemma path; this gives the registry a class to bind to the
   `(?i)claude` regex.
2. Future tweaks (e.g. extracting `<thinking>` blocks if extended thinking is
   enabled) live here without touching the adapter.
"""
from __future__ import annotations

import re

from agents.strategies.base import ToolCallStrategy


class AnthropicToolUseStrategy(ToolCallStrategy):
    """Passthrough strategy for Anthropic's native tool-use protocol.

    AnthropicAdapter does the heavy lifting of translating between OpenAI-style
    tool definitions and Anthropic's `tools` block, and of converting `tool_use`
    response blocks into OpenAI-shaped tool_calls. The methods below cover the
    contract for completeness and for the rare case the strategy is invoked on
    a raw provider response.
    """

    def inject_tools(self, messages: list[dict], tools: list[dict]) -> tuple[list[dict], dict]:
        """Tools are passed through untouched.

        AnthropicAdapter consumes the OpenAI-style `tools` list directly via the
        adapter's `chat(messages, tools=...)` parameter and translates it before
        calling the SDK. The orchestrator path that uses a strategy
        (LMStudio's `inject_tools(...) -> (msgs, extra_params)`) doesn't apply
        here — the adapter never round-trips through `extra_params`. Returning
        an empty extras dict is the safe no-op.
        """
        return messages, {}

    def parse_tool_calls(self, response: dict) -> list[dict]:
        """Extract tool calls from an already-normalized response.

        The adapter has already mapped Anthropic `tool_use` blocks to the
        OpenAI shape in `LLMResponse.tool_calls`. If a caller hands us a raw
        Anthropic response dict, look for `content` blocks of type `tool_use`
        and convert them on the fly.
        """
        # Already-normalized OpenAI shape (e.g. via LLMResponse.raw):
        choices = response.get("choices")
        if choices:
            return choices[0].get("message", {}).get("tool_calls", []) or []

        # Raw Anthropic response shape:
        out: list[dict] = []
        for block in response.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                import json as _json
                out.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": _json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                })
        return out

    def strip_artifacts(self, content: str) -> str:
        """Strip thinking blocks if Claude extended thinking ever leaks through.

        With standard Claude requests this is a no-op. Extended-thinking
        responses can include `<thinking>...</thinking>` segments that we don't
        want to show end users.
        """
        content = re.sub(r"<thinking>.*?</thinking>\s*", "", content, flags=re.DOTALL)
        return content.strip()
