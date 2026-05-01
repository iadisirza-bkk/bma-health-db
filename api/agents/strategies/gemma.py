"""Gemma tool-call strategy — Gemma 3 + Gemma 4 compatibility.

Observed Gemma 4 behaviour against LMStudio (probed 2026-05-01,
``google/gemma-4-31b``):

  * When the request uses the standard OpenAI ``tools=[...]`` parameter,
    Gemma 4 emits a clean OpenAI-native response — ``content`` is empty
    and the tool call appears in ``message.tool_calls`` as::

        {
          "type": "function",
          "id": "<numeric-id>",
          "function": {
            "name": "query_health_data",
            "arguments": "{\\"metric\\":\\"total_screened\\"}"
          }
        }

  * When tools are instead embedded in the system prompt (the legacy
    Gemma 3 approach this module used to require), Gemma 4 falls back to
    its custom token format inside ``content``::

        <|tool_call>call:query_health_data{metric: "total_screened"}<tool_call|>

This strategy now defaults to the **OpenAI-native injection** path (the
clean one) so Gemma 4 produces structured tool calls. The legacy
``<|tool>...<tool|>`` system-prompt injection that Gemma 3 needed
caused Gemma 4 to drift into prose far more often, which manifested as
"the LLM emits a refusal instead of calling a tool".

The parser still understands both response shapes:
  * If the response carries ``message.tool_calls`` (OpenAI-native),
    return it directly.
  * Otherwise, scan ``message.content`` for the legacy
    ``<|tool_call>call:fn{...}<tool_call|>`` pattern as a fallback so
    Gemma 3 deployments and stray formats keep working.
"""
from __future__ import annotations

import json
import re
from typing import Any, cast

from agents.strategies.base import ToolCallStrategy


class GemmaToolCallStrategy(ToolCallStrategy):

    def inject_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Use OpenAI-native ``tools=`` parameter.

        Gemma 4 emits clean ``tool_calls`` when given tools through the
        standard OpenAI parameter. This avoids the "Gemma 3 system-prompt
        injection" path that triggered the model to copy prompt examples
        verbatim (including the refusal text) instead of calling a tool.

        Messages pass through untouched; the tools live in the request
        payload.
        """
        return messages, {"tools": tools, "tool_choice": "auto"}

    def parse_tool_calls(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse tool calls from either OpenAI-native or legacy Gemma format.

        Preference order:
          1. OpenAI-native ``message.tool_calls`` (Gemma 4 with
             native injection — the new default path).
          2. Legacy ``<|tool_call>call:fn{...}<tool_call|>`` in
             ``message.content`` (Gemma 3 / Gemma 4 with system-prompt
             injection / regression fallback).
        """
        message = response.get("choices", [{}])[0].get("message", {})

        # 1. OpenAI-native — Gemma 4 with `tools=` param.
        existing = message.get("tool_calls")
        if existing:
            return cast("list[dict[str, Any]]", existing)

        content = message.get("content", "") or ""
        calls: list[dict[str, Any]] = []
        # 2. Legacy Gemma format — scan content.
        for match in re.finditer(r'<\|?tool_call>call:(\w+)\{', content):
            fn_name = match.group(1)
            # Find matching closing brace (balanced brackets).
            start = match.end() - 1  # position of opening {
            depth = 0
            end = start
            for i in range(start, len(content)):
                if content[i] == '{':
                    depth += 1
                elif content[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            raw_args = content[start:end]  # includes outer { }

            # Strategy 1: parse as JSON directly (works for simple/complex args).
            args: dict[str, Any] = {}
            # Clean Gemma quotes: <|"|>text<|"|> -> "text"
            cleaned = re.sub(r'<\|"\|>(.*?)<\|"\|>', r'"\1"', raw_args)
            # Fix unquoted keys: key: -> "key":
            cleaned = re.sub(r'(\w+):', r'"\1":', cleaned)
            # Fix double-quoted keys: ""key"": -> "key":
            cleaned = re.sub(r'""(\w+)"":', r'"\1":', cleaned)
            try:
                args = json.loads(cleaned)
            except json.JSONDecodeError:
                # Strategy 2: simple key-value extraction.
                for kv in re.finditer(r'(\w+):<\|"\|>(.*?)<\|"\|>', raw_args):
                    args[kv.group(1)] = kv.group(2)
                # Strategy 3: try original JSON.
                if not args:
                    try:
                        args = json.loads(raw_args)
                    except Exception:
                        pass

            calls.append({
                "id": f"gemma_{len(calls)}",
                "type": "function",
                "function": {
                    "name": fn_name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            })
        return calls

    def strip_artifacts(self, content: str) -> str:
        """Remove Gemma-specific artifacts from content."""
        # Think tags (Gemma thinking mode).
        content = re.sub(r'<\|channel>thought.*?<channel\|>\s*', '', content, flags=re.DOTALL)
        # Tool call tags (legacy Gemma native format).
        content = re.sub(r'<\|?tool_call>.*?<\|?tool_call\|?>\s*', '', content, flags=re.DOTALL)
        content = re.sub(r'<\|?tool_call\|?>.*', '', content, flags=re.DOTALL)
        # Hallucinated markdown tool links.
        content = re.sub(r'!\[.*?\]\(query_\w+.*?\)', '', content)
        content = re.sub(r'\[.*?\]\(query_\w+.*?\)', '', content)
        content = re.sub(r'!\[.*?\]\(generate_\w+.*?\)', '', content)
        content = re.sub(r'\[.*?\]\(generate_\w+.*?\)', '', content)
        return content.strip()
