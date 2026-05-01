"""Abstract base tool and result dataclass."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from pydantic import BaseModel


@dataclass
class ToolResult:
    text: str
    visualizations: list[dict] = field(default_factory=list)
    metadata: dict | None = None  # clarification data, artifact URLs, etc.


class BaseTool(ABC):
    """Abstract tool that can be registered and executed by name.

    Tools in this project are SYNC (they query the local DB directly).

    Subclasses should declare `Parameters: ClassVar[type[BaseModel]]` (Pydantic
    v2 model with `extra="forbid"`). Legacy tools that still expose
    `parameters_schema: dict` continue to work — `to_openai_schema()` falls
    back to the dict when `Parameters` is None.
    """

    name: str
    description: str
    parameters_schema: dict
    Parameters: ClassVar[type[BaseModel] | None] = None

    @abstractmethod
    def execute(self, args: dict) -> ToolResult:
        """Execute the tool with given arguments. SYNC."""
        ...

    def to_openai_schema(self) -> dict:
        """Return OpenAI function-calling tool definition.

        Resolution order (mirrors registry precedence):
          1. Instance-level ``parameters_schema`` (set by registry from a
             YAML override) — escape hatch for hand-written schemas.
          2. ``Parameters.model_json_schema()`` when ``Parameters`` is set.
          3. Class-level ``parameters_schema`` (legacy dict, kept for tools
             that haven't been migrated yet).
        """
        if "parameters_schema" in self.__dict__:
            params = self.__dict__["parameters_schema"]
        elif self.Parameters is not None:
            params = self.Parameters.model_json_schema()
        else:
            params = self.parameters_schema
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params,
            },
        }

    def validate_args(self, args: dict) -> BaseModel | dict:
        """Validate raw tool-call args against `Parameters` if defined.

        Returns the parsed Pydantic model when `Parameters` is set, else the
        raw dict. Raises `pydantic.ValidationError` on bad input.
        """
        if self.Parameters is not None:
            return self.Parameters(**args)
        return args
