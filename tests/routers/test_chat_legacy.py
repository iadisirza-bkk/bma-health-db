"""Integration tests for the legacy `/api/health/chat*` router.

The legacy router emits single-channel SSE (`data: {"type": "..."}\\n\\n`)
and is used by the `ChatPanel` component on the dashboard. We test:

    - GET /api/health/chat              sync, returns content
    - POST /api/health/chat             sync, accepts the same query
    - GET /api/health/chat/stream       SSE — content + done events
    - 503 when orchestrator construction fails
    - history JSON is parsed without crashing (malformed → empty)

The orchestrator is monkey-patched at module load time so we don't need
LMStudio. The actual chat.py uses `_get_orchestrator()` which lazily calls
`agents.create_orchestrator()` — we patch the latter.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, AsyncGenerator

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "api"))


class _FakeLegacyOrchestrator:
    """Mirrors the orchestrator surface used by `chat.py`."""

    async def process(self, message: str) -> dict[str, Any]:
        return {
            "content": f"reply to: {message}",
            "visualizations": [],
        }

    async def process_stream(
        self,
        message: str,
        conv_history: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[str, None]:
        # Legacy single-channel SSE format.
        yield f"data: {json.dumps({'type': 'content', 'text': 'hello '})}\n\n"
        yield f"data: {json.dumps({'type': 'content', 'text': 'world'})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"


@pytest.fixture()
def patched_legacy(monkeypatch):
    """Patch `agents.create_orchestrator` to return our fake."""
    fake = _FakeLegacyOrchestrator()
    import agents
    monkeypatch.setattr(agents, "create_orchestrator", lambda: fake)
    return fake


@pytest.fixture()
def patched_legacy_unavailable(monkeypatch):
    """Force `_get_orchestrator` to return None — simulates LMStudio offline."""
    import agents
    def _boom():
        raise RuntimeError("LMStudio unreachable")
    monkeypatch.setattr(agents, "create_orchestrator", _boom)


# --------------------------------------------------------------------------- #
# Sync chat
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_sync_chat_get_returns_content(client, patched_legacy):
    res = await client.get("/api/health/chat", params={"message": "hi"})
    assert res.status_code == 200
    assert res.json()["content"] == "reply to: hi"


@pytest.mark.anyio
async def test_sync_chat_post_returns_content(client, patched_legacy):
    res = await client.post("/api/health/chat", params={"message": "hi"})
    assert res.status_code == 200
    assert res.json()["content"] == "reply to: hi"


@pytest.mark.anyio
async def test_sync_chat_503_when_orchestrator_unavailable(
    client, patched_legacy_unavailable
):
    res = await client.get("/api/health/chat", params={"message": "hi"})
    assert res.status_code == 503
    assert "unavailable" in res.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# SSE stream
# --------------------------------------------------------------------------- #


def _parse_legacy_sse(body: str) -> list[dict]:
    """The legacy stream is `data: {"type":...}\\n\\n` only — no event names."""
    out = []
    for line in body.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            out.append(json.loads(line[5:].strip()))
        except json.JSONDecodeError:
            pass
    return out


@pytest.mark.anyio
async def test_stream_emits_content_and_done(client, patched_legacy):
    res = await client.get(
        "/api/health/chat/stream", params={"message": "hi"}
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    # Critical: response must NOT be gzipped. The `Content-Encoding: identity`
    # header is what tells Next.js dev rewrites not to compress the stream.
    assert res.headers.get("content-encoding") == "identity"

    events = _parse_legacy_sse(res.text)
    types = [e["type"] for e in events]
    assert types == ["content", "content", "done"]

    text = "".join(e["text"] for e in events if e["type"] == "content")
    assert text == "hello world"


@pytest.mark.anyio
async def test_stream_503_path_returns_error_event(
    client, patched_legacy_unavailable
):
    """When orchestrator is unavailable the stream emits an error+done.
    The HTTP status is still 200 (SSE convention)."""
    res = await client.get(
        "/api/health/chat/stream", params={"message": "hi"}
    )
    assert res.status_code == 200
    events = _parse_legacy_sse(res.text)
    types = [e["type"] for e in events]
    assert "error" in types
    assert types[-1] == "done"


@pytest.mark.anyio
async def test_stream_handles_malformed_history_gracefully(
    client, patched_legacy
):
    """Frontend bug shouldn't crash the backend — bad history is dropped."""
    res = await client.get(
        "/api/health/chat/stream",
        params={"message": "hi", "history": "this-is-not-json"},
    )
    assert res.status_code == 200
    events = _parse_legacy_sse(res.text)
    assert any(e["type"] == "done" for e in events)


@pytest.mark.anyio
async def test_stream_handles_history_that_is_object_not_array(
    client, patched_legacy
):
    """History must be a list — an object should be silently coerced to []."""
    res = await client.get(
        "/api/health/chat/stream",
        params={"message": "hi", "history": '{"role":"user"}'},
    )
    assert res.status_code == 200


@pytest.mark.anyio
async def test_stream_history_trimmed_to_last_two_turns(
    client, patched_legacy, monkeypatch
):
    """The router slices history to the last 2 turns before passing on.

    We capture what process_stream actually receives by wrapping the fake.
    """
    captured: dict[str, Any] = {}

    fake = _FakeLegacyOrchestrator()
    orig = fake.process_stream

    async def wrapped(message, conv_history=None):
        captured["history"] = conv_history
        async for c in orig(message, conv_history):
            yield c

    fake.process_stream = wrapped  # type: ignore[assignment]
    import agents
    monkeypatch.setattr(agents, "create_orchestrator", lambda: fake)

    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
    ]
    res = await client.get(
        "/api/health/chat/stream",
        params={"message": "hi", "history": json.dumps(history)},
    )
    assert res.status_code == 200
    assert captured["history"] is not None
    # Router slices to last 2 turns.
    assert len(captured["history"]) == 2
    assert captured["history"][-1]["content"] == "q3"
