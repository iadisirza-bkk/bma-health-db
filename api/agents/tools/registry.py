"""Tool registry — register, list, and execute tools by name.

Tools are SYNC in this project. The registry provides both sync and
async execution (async wraps sync via asyncio.to_thread).
"""
from __future__ import annotations

import asyncio
import logging

from agents.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool
        logger.debug("Registered tool: %s", tool.name)

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def to_openai_schemas(self) -> list[dict]:
        return [t.to_openai_schema() for t in self._tools.values()]

    def to_filtered_schemas(self, tool_names: list[str]) -> list[dict]:
        """Return schemas for only the specified tools."""
        return [t.to_openai_schema() for t in self._tools.values() if t.name in tool_names]

    def execute_sync(self, name: str, args: dict) -> ToolResult:
        """Execute a tool synchronously."""
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(text=f"Unknown tool: {name}")
        logger.info("Executing tool: %s(%s)", name, args)
        try:
            return tool.execute(args)
        except Exception as e:
            logger.exception("Tool '%s' failed: %s", name, e)
            return ToolResult(text=f"เครื่องมือ {name} เกิดข้อผิดพลาด กรุณาลองใหม่")

    async def execute(self, name: str, args: dict) -> ToolResult:
        """Execute a tool from async context (wraps sync in thread)."""
        return await asyncio.to_thread(self.execute_sync, name, args)

    @classmethod
    def create_default(cls) -> "ToolRegistry":
        """Create registry with all built-in tools.

        NOTE: backend_api tool is NOT registered — we query DB directly now.
        """
        registry = cls()
        from agents.tools.health_data import QueryHealthDataTool
        from agents.tools.statistical import QueryStatisticalTestTool
        from agents.tools.report import GenerateReportTool
        from agents.tools.clarification import AskClarificationTool
        from agents.tools.zone_info import QueryZoneInfoTool
        from agents.tools.adaptive_report import GenerateAdaptiveReportTool

        for tool_cls in [QueryHealthDataTool, QueryStatisticalTestTool,
                         GenerateReportTool, GenerateAdaptiveReportTool,
                         AskClarificationTool, QueryZoneInfoTool]:
            registry.register(tool_cls())
        return registry
