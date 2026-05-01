"""AskClarificationTool — interactive Q&A with user before analysis.

SYNC.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict

from agents.tools.base import BaseTool, ToolResult


class _ClarificationOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Optional[str] = None
    value: Optional[str] = None


class _ClarificationQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Optional[str] = None
    question: Optional[str] = None
    options: Optional[list[_ClarificationOption]] = None


class AskClarificationParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[_ClarificationQuestion]


class AskClarificationTool(BaseTool):
    name = "ask_clarification"
    description = "Ask user clarifying questions BEFORE analysis. Use when request is ambiguous or complex."
    Parameters = AskClarificationParams
    parameters_schema = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "question": {"type": "string"},
                        "options": {"type": "array", "items": {"type": "object", "properties": {"label": {"type": "string"}, "value": {"type": "string"}}}},
                    },
                },
            },
        },
        "required": ["questions"],
    }

    def execute(self, args: dict) -> ToolResult:
        args = self.Parameters(**args).model_dump(exclude_none=True)
        questions = args.get("questions", [])
        return ToolResult(
            text="กำลังรอคำตอบจากผู้ใช้",
            metadata={"type": "clarification", "questions": questions},
        )
