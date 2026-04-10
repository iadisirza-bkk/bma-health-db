"""AgentRunner — conversation loop with tool dispatch.

Tools in this project are SYNC. When called from async context,
they are wrapped with asyncio.to_thread().
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from agents.core.agent import Agent
from agents.adapters.base import LLMResponse
from agents.tools.registry import ToolRegistry
from agents.tools.base import ToolResult

logger = logging.getLogger(__name__)


class AgentRunner:
    """Runs an agent through the tool-calling conversation loop."""

    def __init__(self, agent: Agent, registry: ToolRegistry):
        self.agent = agent
        self.registry = registry

    async def run_single_turn(self, messages: list[dict]) -> tuple[LLMResponse, list[ToolResult]]:
        """One turn: get response, execute any tool calls, return both."""
        response = await self.agent.run(messages)
        results = []

        if response.tool_calls:
            for tc in response.tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"]
                except (json.JSONDecodeError, TypeError):
                    fn_args = {}

                logger.info("Tool call: %s(%s)", fn_name, fn_args)
                # Tools are sync — wrap in thread to avoid blocking event loop
                result = await asyncio.to_thread(self.registry.execute_sync, fn_name, fn_args)
                results.append(result)

                # Append tool result to messages for next turn
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", f"call_{len(results)}"),
                    "content": result.text,
                })

        return response, results

    async def run_conversation(self, messages: list[dict], max_turns: int = 3) -> tuple[str, list[dict], list[dict], list[ToolResult]]:
        """Multi-turn conversation loop.

        Returns: (final_text, visualizations, artifacts, all_tool_results)
        """
        all_viz = []
        all_artifacts = []
        all_results = []

        for turn in range(max_turns):
            response, results = await self.run_single_turn(messages)
            all_results.extend(results)

            # Collect visualizations and artifacts from tool results
            for r in results:
                all_viz.extend(r.visualizations)
                if r.metadata and r.metadata.get("type") == "clarification":
                    # Clarification stops the loop
                    return "", all_viz, all_artifacts, all_results

            if not response.tool_calls:
                # No more tools — return final text
                return response.content, all_viz, all_artifacts, all_results

            # If we had tool calls, add the assistant message for the next turn
            messages.append({
                "role": "assistant",
                "content": response.content,
                "tool_calls": response.tool_calls,
            })

        # Max turns exceeded
        return response.content if response else "", all_viz, all_artifacts, all_results
