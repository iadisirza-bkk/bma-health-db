"""Agent — wraps an LLM adapter with config, tools, and prompt."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncGenerator

from agents.adapters.base import LLMAdapter, LLMResponse
from agents.tools.registry import ToolRegistry


@dataclass
class AgentConfig:
    name: str                        # e.g., "analyst", "synthesizer"
    role: str                        # human-readable description
    system_prompt: str
    icon: str = "brain"             # SSE icon
    max_turns: int = 3


class Agent:
    """Individual agent that can run prompts, call tools, and stream responses."""

    def __init__(self, config: AgentConfig, adapter: LLMAdapter, tools: ToolRegistry | None = None):
        self.config = config
        self.adapter = adapter
        self.tools = tools

    async def run(self, messages: list[dict]) -> LLMResponse:
        """Single turn: send messages with tools, get response."""
        tool_schemas = self.tools.to_openai_schemas() if self.tools else None
        return await self.adapter.chat(messages, tools=tool_schemas)

    async def prompt(self, user_message: str, history: list[dict] | None = None) -> LLMResponse:
        """Convenience: build messages from system_prompt + optional history + user message."""
        messages = [{"role": "system", "content": self.config.system_prompt}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})
        return await self.run(messages)

    async def stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        """Stream tokens from adapter (no tools — for final synthesis)."""
        async for token in self.adapter.stream(messages):
            yield token
