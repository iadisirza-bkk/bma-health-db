"""Abstract base tool and result dataclass."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolResult:
    text: str
    visualizations: list[dict] = field(default_factory=list)
    metadata: dict | None = None  # clarification data, artifact URLs, etc.


class BaseTool(ABC):
    """Abstract tool that can be registered and executed by name.

    Tools in this project are SYNC (they query the local DB directly).
    """

    name: str
    description: str
    parameters_schema: dict

    @abstractmethod
    def execute(self, args: dict) -> ToolResult:
        """Execute the tool with given arguments. SYNC."""
        ...

    def to_openai_schema(self) -> dict:
        """Return OpenAI function-calling tool definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }
