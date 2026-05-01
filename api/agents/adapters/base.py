"""Abstract LLM adapter — interface for all LLM backends."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agents.strategies.base import ToolCallStrategy


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
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = "stop"


class LLMAdapter(ABC):
    """Abstract base for LLM backends (LMStudio, MLX, OpenAI, etc.)."""

    def __init__(self, config: AdapterConfig, strategy: Optional["ToolCallStrategy"] = None) -> None:
        self.config = config
        self.strategy = strategy

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the LLM service is available."""
        ...

    @abstractmethod
    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> LLMResponse:
        """Send messages + optional tools, get response with possible tool calls."""
        ...

    @abstractmethod
    def stream(self, messages: list[dict[str, Any]]) -> AsyncGenerator[str, None]:
        """Stream tokens from LLM (no tools, for final synthesis).

        Implementations are async generator functions (use ``yield`` inside).
        Declared with ``def`` here (not ``async def``) so the signature
        matches concrete subclasses, which mypy otherwise treats as
        ``Coroutine[..., AsyncGenerator[...]]``.
        """
        ...
