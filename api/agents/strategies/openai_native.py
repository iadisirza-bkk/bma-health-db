"""OpenAI-native tool call strategy (fallback for models with OpenAI tools support).

Uses standard OpenAI `tools` parameter and `tool_calls` response field.
"""
from __future__ import annotations

import re

from agents.strategies.base import ToolCallStrategy


class OpenAINativeStrategy(ToolCallStrategy):

    def inject_tools(self, messages: list[dict], tools: list[dict]) -> tuple[list[dict], dict]:
        """Pass tools via OpenAI tools parameter."""
        return messages, {"tools": tools, "tool_choice": "auto"}

    def parse_tool_calls(self, response: dict) -> list[dict]:
        """Extract tool_calls from standard OpenAI response."""
        return response.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])

    def strip_artifacts(self, content: str) -> str:
        """Remove think tags and hallucinated tool calls."""
        content = re.sub(r'<think>.*?</think>\s*', '', content, flags=re.DOTALL)
        content = re.sub(r'<tool_call>.*?</tool_call>\s*', '', content, flags=re.DOTALL)
        content = re.sub(r'!\[.*?\]\(query_\w+.*?\)', '', content)
        content = re.sub(r'\[.*?\]\(query_\w+.*?\)', '', content)
        return content.strip()
