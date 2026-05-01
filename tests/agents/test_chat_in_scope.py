"""Integration tests for the chat orchestrator's scope guardrail and
tool-first behaviour.

These tests pin down two regressions that previously broke the dashboard
chat:

  1. **In-scope queries used to be refused.** "ภาพรวมโรคทั้งหมด" is a
     legitimate question about the BMA screening database, but the old
     keyword guardrail (`_is_on_topic`) sometimes rejected it, and the
     system prompt instructed Gemma to copy the refusal phrase verbatim.
     We now check that an in-scope query routes a tool, the tool's data
     reaches the synthesiser, and the final reply contains the real
     numbers (not the refusal phrase, not a hallucinated 1.6 ล้าน
     target).

  2. **Obviously-off-topic queries are still refused.** "วิธีทำพาสต้า"
     should hit the `_is_obviously_off_topic` deny-list and return the
     short refusal without ever calling the LLM.

The orchestrator is exercised end-to-end with fake adapters and a fake
tool registry — no network, no LMStudio, no DB.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, AsyncGenerator

import pytest

# Mirror the rest of tests/agents/ — make `api/` importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "api"))

from agents.adapters.base import AdapterConfig, LLMAdapter, LLMResponse  # noqa: E402
from agents.core.circuit_breaker import CircuitBreaker  # noqa: E402
from agents.core.orchestrator import (  # noqa: E402
    OpenMultiAgent,
    _REFUSAL_RESPONSE,
    _is_obviously_off_topic,
)
from agents.tools.base import BaseTool, ToolResult  # noqa: E402
from agents.tools.registry import ToolRegistry  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakeAdapter(LLMAdapter):
    """In-process LLM adapter — returns canned responses, never hits HTTP.

    The first call returns a tool_call (analyst phase). Subsequent calls
    return prose (synthesiser phase) so the orchestrator can stitch a
    final reply.
    """

    def __init__(
        self,
        config: AdapterConfig,
        tool_call: dict[str, Any] | None = None,
        synth_text: str = "",
    ) -> None:
        super().__init__(config, strategy=None)
        self._tool_call = tool_call
        self._synth_text = synth_text
        self.calls: list[list[dict[str, Any]]] = []

    async def health_check(self) -> bool:
        return True

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.calls.append(messages)
        # First call — analyst with tools = emit the canned tool_call.
        if tools and self._tool_call is not None:
            return LLMResponse(
                content="",
                tool_calls=[self._tool_call],
                raw={},
                finish_reason="tool_calls",
            )
        # Subsequent calls — synthesiser, no tools = emit canned prose.
        return LLMResponse(
            content=self._synth_text,
            tool_calls=[],
            raw={},
            finish_reason="stop",
        )

    async def stream(
        self, messages: list[dict[str, Any]],
    ) -> AsyncGenerator[str, None]:
        # The streaming path yields the synth text in one chunk.
        if False:  # pragma: no cover — keeps mypy happy
            yield ""
        yield self._synth_text


class _FakeTool(BaseTool):
    """Tool stub that returns a fixed payload regardless of args."""

    def __init__(self, name: str, payload: str) -> None:
        self.name = name
        self.description = "fake tool"
        self.parameters_schema = {"type": "object", "properties": {}}
        self._payload = payload

    def execute(self, args: dict[str, Any]) -> ToolResult:
        return ToolResult(text=self._payload)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_registry(tool_name: str, payload: str) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_FakeTool(tool_name, payload))
    # The orchestrator's process_stream forces a default tool when the
    # LLM omits tool_calls; one of the defaults is `query_health_data`.
    # Register it under that name too so the forced-tool branch finds
    # something concrete if the analyst ever skips tools in a future
    # change.
    if tool_name != "query_health_data":
        reg.register(_FakeTool("query_health_data", payload))
    return reg


def _make_orchestrator(
    tool_call: dict[str, Any] | None,
    synth_text: str,
    registry: ToolRegistry,
) -> OpenMultiAgent:
    cfg = AdapterConfig(
        base_url="http://test", model="gemma-4-31b",
        temperature=0.1, max_tokens=500, timeout=30,
    )
    adapter = _FakeAdapter(cfg, tool_call=tool_call, synth_text=synth_text)
    return OpenMultiAgent(
        analyst_adapter=adapter,
        synthesizer_adapter=adapter,
        registry=registry,
        circuit_breaker=CircuitBreaker(),
    )


# --------------------------------------------------------------------------- #
# Tests — keyword guardrail
# --------------------------------------------------------------------------- #


def test_obvious_off_topic_recipe_blocked() -> None:
    """Cooking questions are obviously off-topic."""
    assert _is_obviously_off_topic("วิธีทำพาสต้าแบบง่าย") is True


def test_obvious_off_topic_crypto_blocked() -> None:
    assert _is_obviously_off_topic("ราคาบิทคอยน์วันนี้") is True


def test_obvious_off_topic_weather_blocked() -> None:
    assert _is_obviously_off_topic("พยากรณ์อากาศพรุ่งนี้") is True


def test_in_scope_overview_passes_guardrail() -> None:
    """The previously-failing question must NOT be flagged as off-topic."""
    assert _is_obviously_off_topic("ภาพรวมโรคทั้งหมด") is False


def test_in_scope_count_passes_guardrail() -> None:
    assert _is_obviously_off_topic("มีคนคัดกรองกี่คน") is False


def test_ambiguous_obesity_compare_passes_guardrail() -> None:
    """'อ้วนกว่ารัฐอื่นไหม' should reach the LLM, not be pre-rejected."""
    assert _is_obviously_off_topic("อ้วนกว่ารัฐอื่นไหม") is False


# --------------------------------------------------------------------------- #
# Tests — in-scope question reaches a tool, real numbers come back
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_in_scope_question_calls_tool_and_returns_real_numbers() -> None:
    """End-to-end: 'ภาพรวมโรคทั้งหมด' → tool call → synth uses tool data.

    Asserts the bug-fix invariants:
      - The final reply contains the tool's actual number (181).
      - It does NOT contain the hard-coded refusal phrase.
      - It does NOT mention the previously-hallucinated "1.6 ล้าน" target.
    """
    tool_payload = (
        "total_screened=181 patients\n"
        "by_disease: diabetes=42, hypertension=58, obesity=63"
    )
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "query_health_data",
            "arguments": json.dumps(
                {"group_by": "disease", "chart_type": "donut"},
            ),
        },
    }
    synth_text = (
        "## ภาพรวมโรค NCD\n"
        "ผู้คัดกรองทั้งหมด **181** คน "
        "พบความดันสูง **58** คน เบาหวาน **42** คน อ้วน **63** คน"
    )

    registry = _make_registry("query_health_data", tool_payload)
    orch = _make_orchestrator(tool_call, synth_text, registry)

    result = await orch.process("ภาพรวมโรคทั้งหมด")
    content = result["content"]

    assert "181" in content, "Final reply must include the tool's actual count"
    # The new short refusal phrase used by the orchestrator.
    assert "ขออภัยค่ะ ฉันตอบได้เฉพาะ" not in content, (
        "Refusal phrase must not appear for in-scope queries"
    )
    # The hallucinated target ("1.6 ล้าน") must not appear unless the
    # tool actually returned it (it didn't).
    assert "1.6 ล้าน" not in content
    assert "1,600,000" not in content


@pytest.mark.anyio
async def test_obvious_off_topic_short_circuits_without_llm() -> None:
    """A clearly off-topic question hits the deny-list and never sees the LLM."""
    # Tool/registry irrelevant — the guardrail returns before either is consulted.
    registry = _make_registry("query_health_data", "irrelevant")
    orch = _make_orchestrator(None, "irrelevant", registry)

    result = await orch.process("วิธีทำพาสต้าแบบง่าย")
    assert result["content"] == _REFUSAL_RESPONSE
    assert result["visualizations"] == []
    # The fake adapter must NOT have been called.
    adapter = orch.analyst_adapter  # type: ignore[assignment]
    assert getattr(adapter, "calls", []) == []


@pytest.mark.anyio
async def test_in_scope_screening_count_returns_tool_number() -> None:
    """A bare 'มีคนคัดกรองกี่คน' query also reaches the tool."""
    tool_payload = "headline_kpi: total_screened=181"
    tool_call = {
        "id": "call_2",
        "type": "function",
        "function": {
            "name": "query_health_data",
            "arguments": json.dumps({"group_by": "disease"}),
        },
    }
    synth_text = "มีผู้คัดกรองทั้งหมด **181** คน"

    registry = _make_registry("query_health_data", tool_payload)
    orch = _make_orchestrator(tool_call, synth_text, registry)

    result = await orch.process("มีคนคัดกรองกี่คน")
    assert "181" in result["content"]
    assert "ขออภัยค่ะ ฉันตอบได้เฉพาะ" not in result["content"]
