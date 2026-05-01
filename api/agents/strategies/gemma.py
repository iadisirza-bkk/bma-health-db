"""Gemma 4 native tool call strategy.

Gemma uses special tokens: <|tool>...<tool|> for definitions,
<|tool_call>call:func{args}<tool_call|> for calls.
"""
from __future__ import annotations

import json
import re
from typing import Any, cast

from agents.strategies.base import ToolCallStrategy


class GemmaToolCallStrategy(ToolCallStrategy):

    def inject_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Embed tools in system prompt using Gemma native <|tool> format."""
        tool_defs = "\n".join(
            f'<|tool>\n{json.dumps(t["function"], ensure_ascii=False)}\n<tool|>'
            for t in tools
        )
        patched = []
        for m in messages:
            if m["role"] == "system":
                patched.append({"role": "system", "content": m["content"] + "\n\n" + tool_defs})
            else:
                patched.append(m)
        # No extra params — Gemma doesn't use OpenAI tools parameter
        return patched, {}

    def parse_tool_calls(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse Gemma native <|tool_call>call:func{args}<tool_call|> from content."""
        content = response.get("choices", [{}])[0].get("message", {}).get("content", "") or ""

        # Check if LMStudio already parsed tool_calls (OpenAI format)
        existing = response.get("choices", [{}])[0].get("message", {}).get("tool_calls")
        if existing:
            return cast("list[dict[str, Any]]", existing)

        calls: list[dict[str, Any]] = []
        # Find tool calls — use greedy match for nested braces
        for match in re.finditer(r'<\|?tool_call>call:(\w+)\{', content):
            fn_name = match.group(1)
            # Find matching closing brace (balanced brackets)
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

            # Strategy 1: Parse as JSON directly (works for simple and complex args)
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
                # Strategy 2: Simple key-value extraction
                for kv in re.finditer(r'(\w+):<\|"\|>(.*?)<\|"\|>', raw_args):
                    args[kv.group(1)] = kv.group(2)
                # Strategy 3: Try original JSON
                if not args:
                    try:
                        args = json.loads(raw_args)
                    except Exception:
                        pass

            calls.append({
                "id": f"gemma_{len(calls)}",
                "type": "function",
                "function": {"name": fn_name, "arguments": json.dumps(args, ensure_ascii=False)},
            })
        return calls

    def strip_artifacts(self, content: str) -> str:
        """Remove Gemma-specific artifacts from content."""
        # Think tags (Gemma thinking mode)
        content = re.sub(r'<\|channel>thought.*?<channel\|>\s*', '', content, flags=re.DOTALL)
        # Tool call tags (Gemma native format)
        content = re.sub(r'<\|?tool_call>.*?<\|?tool_call\|?>\s*', '', content, flags=re.DOTALL)
        content = re.sub(r'<\|?tool_call\|?>.*', '', content, flags=re.DOTALL)
        # Hallucinated markdown tool links
        content = re.sub(r'!\[.*?\]\(query_\w+.*?\)', '', content)
        content = re.sub(r'\[.*?\]\(query_\w+.*?\)', '', content)
        content = re.sub(r'!\[.*?\]\(generate_\w+.*?\)', '', content)
        content = re.sub(r'\[.*?\]\(generate_\w+.*?\)', '', content)
        return content.strip()
