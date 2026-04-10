"""AskClarificationTool — interactive Q&A with user before analysis.

SYNC.
"""
from __future__ import annotations

from agents.tools.base import BaseTool, ToolResult


class AskClarificationTool(BaseTool):
    name = "ask_clarification"
    description = "Ask user clarifying questions BEFORE analysis. Use when request is ambiguous or complex."
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
        questions = args.get("questions", [])
        return ToolResult(
            text="กำลังรอคำตอบจากผู้ใช้",
            metadata={"type": "clarification", "questions": questions},
        )
