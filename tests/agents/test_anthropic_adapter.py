"""Unit tests for AnthropicAdapter.

No real network calls — the `anthropic.AsyncAnthropic` client is mocked.
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# tests/conftest.py inserts api/ on sys.path, but tests subdir doesn't always
# inherit that — make sure the api dir is importable here too.
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "api"))

from agents.adapters.anthropic import AnthropicAdapter  # noqa: E402
from agents.adapters.base import AdapterConfig  # noqa: E402


def _config(**overrides) -> AdapterConfig:
    base = dict(
        base_url="https://api.anthropic.com",
        model="claude-3-5-sonnet-latest",
        temperature=0.1,
        max_tokens=200,
        timeout=30,
    )
    base.update(overrides)
    return AdapterConfig(**base)


def _text_response(text: str, stop_reason: str = "end_turn"):
    """Build a mock Anthropic Messages API response with a single text block."""
    block = SimpleNamespace(type="text", text=text)
    resp = SimpleNamespace(
        content=[block],
        stop_reason=stop_reason,
        model_dump=lambda: {"content": [{"type": "text", "text": text}], "stop_reason": stop_reason},
    )
    return resp


def _tool_use_response(tool_id: str, name: str, args: dict):
    """Build a mock response containing a single tool_use block."""
    block = SimpleNamespace(type="tool_use", id=tool_id, name=name, input=args)
    resp = SimpleNamespace(
        content=[block],
        stop_reason="tool_use",
        model_dump=lambda: {
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": args}],
            "stop_reason": "tool_use",
        },
    )
    return resp


# --- chat: text-only path -----------------------------------------------------

@pytest.mark.anyio
async def test_chat_returns_text_when_no_tools():
    adapter = AnthropicAdapter(_config(), api_key="test-key")
    fake_create = AsyncMock(return_value=_text_response("Hello from Claude"))
    adapter._client.messages = SimpleNamespace(create=fake_create, stream=MagicMock())

    out = await adapter.chat([
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hi"},
    ])

    assert out.content == "Hello from Claude"
    assert out.tool_calls == []
    assert out.finish_reason == "stop"

    # Verify system was extracted to top-level kwarg.
    call_kwargs = fake_create.await_args.kwargs
    assert call_kwargs["system"] == "You are helpful."
    assert call_kwargs["messages"] == [{"role": "user", "content": "Hi"}]
    assert call_kwargs["model"] == "claude-3-5-sonnet-latest"


# --- chat: tool_use path ------------------------------------------------------

@pytest.mark.anyio
async def test_chat_translates_tool_use_blocks_to_openai_shape():
    adapter = AnthropicAdapter(_config(), api_key="test-key")
    fake_create = AsyncMock(return_value=_tool_use_response(
        "toolu_abc123", "query_health_data", {"metric": "dm_count", "district_code": "1001"},
    ))
    adapter._client.messages = SimpleNamespace(create=fake_create, stream=MagicMock())

    tools = [{
        "type": "function",
        "function": {
            "name": "query_health_data",
            "description": "Fetch health aggregates",
            "parameters": {
                "type": "object",
                "properties": {"metric": {"type": "string"}, "district_code": {"type": "string"}},
                "required": ["metric"],
            },
        },
    }]
    out = await adapter.chat([{"role": "user", "content": "DM in Bang Rak?"}], tools=tools)

    assert out.finish_reason == "tool_calls"
    assert len(out.tool_calls) == 1
    tc = out.tool_calls[0]
    assert tc["id"] == "toolu_abc123"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "query_health_data"
    # arguments must be a JSON string (matches OpenAI shape the orchestrator parses).
    parsed = json.loads(tc["function"]["arguments"])
    assert parsed == {"metric": "dm_count", "district_code": "1001"}

    # Verify tools were translated into Anthropic's input_schema shape.
    call_kwargs = fake_create.await_args.kwargs
    assert call_kwargs["tools"][0]["name"] == "query_health_data"
    assert call_kwargs["tools"][0]["input_schema"]["properties"]["metric"]["type"] == "string"


# --- chat: provider error wrapped ---------------------------------------------

@pytest.mark.anyio
async def test_chat_wraps_provider_errors_instead_of_raising():
    adapter = AnthropicAdapter(_config(), api_key="test-key")
    fake_create = AsyncMock(side_effect=RuntimeError("upstream 500"))
    adapter._client.messages = SimpleNamespace(create=fake_create, stream=MagicMock())

    out = await adapter.chat([{"role": "user", "content": "hi"}])

    assert "Anthropic call failed" in out.content
    assert out.finish_reason == "error"
    assert out.tool_calls == []
    assert out.raw["error_type"] == "RuntimeError"


# --- stream: yields text deltas ------------------------------------------------

class _FakeStreamCtx:
    """Minimal async-context-manager that mimics anthropic's stream object."""

    def __init__(self, deltas: list[str]):
        self._deltas = deltas

    async def __aenter__(self):
        async def _agen():
            for d in self._deltas:
                yield d
        self.text_stream = _agen()
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.anyio
async def test_stream_yields_text_deltas():
    adapter = AnthropicAdapter(_config(), api_key="test-key")

    def _stream_factory(**_kwargs):
        return _FakeStreamCtx(["Hello", " ", "world"])

    adapter._client.messages = SimpleNamespace(
        create=AsyncMock(),
        stream=_stream_factory,
    )

    chunks = []
    async for delta in adapter.stream([{"role": "user", "content": "say hi"}]):
        chunks.append(delta)

    assert chunks == ["Hello", " ", "world"]


@pytest.mark.anyio
async def test_stream_yields_error_chunk_on_failure():
    adapter = AnthropicAdapter(_config(), api_key="test-key")

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("network down")

        async def __aexit__(self, *exc):
            return False

    adapter._client.messages = SimpleNamespace(
        create=AsyncMock(),
        stream=lambda **_kw: _Boom(),
    )

    chunks = []
    async for delta in adapter.stream([{"role": "user", "content": "hi"}]):
        chunks.append(delta)

    assert len(chunks) == 1
    assert "stream failed" in chunks[0].lower()


# --- health_check -------------------------------------------------------------

@pytest.mark.anyio
async def test_health_check_returns_true_when_models_list_succeeds():
    adapter = AnthropicAdapter(_config(), api_key="test-key")
    adapter._client.models = SimpleNamespace(list=AsyncMock(return_value=SimpleNamespace(data=[])))

    assert await adapter.health_check() is True


@pytest.mark.anyio
async def test_health_check_returns_false_on_exception():
    adapter = AnthropicAdapter(_config(), api_key="test-key")
    adapter._client.models = SimpleNamespace(list=AsyncMock(side_effect=RuntimeError("401")))

    assert await adapter.health_check() is False


@pytest.mark.anyio
async def test_health_check_returns_false_without_api_key():
    # Construct without api_key — adapter should refuse to hit the network.
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        adapter = AnthropicAdapter(_config(), api_key=None)

    assert await adapter.health_check() is False


# --- message translation ------------------------------------------------------

@pytest.mark.anyio
async def test_chat_translates_tool_role_messages_to_tool_result_blocks():
    """A `role=tool` message must become a user message with a tool_result block."""
    adapter = AnthropicAdapter(_config(), api_key="test-key")
    fake_create = AsyncMock(return_value=_text_response("ok"))
    adapter._client.messages = SimpleNamespace(create=fake_create, stream=MagicMock())

    msgs = [
        {"role": "user", "content": "fetch foo"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "toolu_1",
                "type": "function",
                "function": {"name": "foo", "arguments": '{"x": 1}'},
            }],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "result-data"},
    ]
    await adapter.chat(msgs)

    sent = fake_create.await_args.kwargs["messages"]
    # User → assistant(tool_use) → user(tool_result)
    assert len(sent) == 3
    assert sent[0]["role"] == "user"
    assert sent[1]["role"] == "assistant"
    assert any(b.get("type") == "tool_use" and b.get("id") == "toolu_1" for b in sent[1]["content"])
    assert sent[2]["role"] == "user"
    assert sent[2]["content"][0]["type"] == "tool_result"
    assert sent[2]["content"][0]["tool_use_id"] == "toolu_1"
    assert sent[2]["content"][0]["content"] == "result-data"
