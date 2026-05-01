"""Abstract tool call strategy — model-specific tool format handling."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ToolCallStrategy(ABC):
    """Handles model-specific tool injection, parsing, and artifact stripping."""

    @abstractmethod
    def inject_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Inject tools into the request.
        Returns (modified_messages, extra_request_params).
        Gemma: tools go in system prompt, extra_params={}
        OpenAI: messages unchanged, extra_params={"tools": [...], "tool_choice": "auto"}
        """
        ...

    @abstractmethod
    def parse_tool_calls(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract tool calls from response. Returns OpenAI-normalized format."""
        ...

    @abstractmethod
    def strip_artifacts(self, content: str) -> str:
        """Remove model-specific artifacts (think tags, tool call tags, etc.)."""
        ...
