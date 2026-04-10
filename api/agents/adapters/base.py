"""Abstract LLM adapter — interface for all LLM backends."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncGenerator


@dataclass
class AdapterConfig:
    base_url: str
    model: str
    temperature: float = 0.1
    max_tokens: int = 2000
    timeout: int = 120


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    finish_reason: str = "stop"


class LLMAdapter(ABC):
    """Abstract base for LLM backends (LMStudio, MLX, OpenAI, etc.)."""

    def __init__(self, config: AdapterConfig):
        self.config = config

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the LLM service is available."""
        ...

    @abstractmethod
    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        """Send messages + optional tools, get response with possible tool calls."""
        ...

    @abstractmethod
    async def stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        """Stream tokens from LLM (no tools, for final synthesis)."""
        ...
