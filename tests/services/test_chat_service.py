"""Unit tests for ChatService (ADR-02 §7).

Test backend choice
-------------------
We use an **in-memory fake repository** rather than a real PostgreSQL DB
or a SQLite mock. Reasons:

  * The real schema (300_chat_threads.sql) leans hard on PG-specific
    features: `gen_random_uuid()`, `JSONB`, the audit trigger, the
    `metadata ||` jsonb operator. Running it against SQLite needs a
    dialect-translation layer that would dwarf the actual test logic.
  * The repository is thin enough that a 30-line in-memory stand-in
    captures all the behaviour the service actually depends on.
  * `tests/services/charts/test_service.py` follows the exact same
    pattern (a `_FakeRepo` class) — keeps the test corpus consistent.

A separate integration test against a scratch DB would be valuable for
the repository itself, but is out of scope for the service unit test.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

import pytest

# Make `api/` importable for `services.chat`. Mirrors tests/conftest.py.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from repositories.chat_rows import MessageRow, ThreadRow  # noqa: E402
from services.chat import ChatService, format_sse_event  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakeRepo:
    """In-memory stand-in for ChatRepository — no SQL.

    Implements only the methods the service actually calls.
    """

    def __init__(self) -> None:
        self.threads: dict[UUID, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self._next_msg_id = 1

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    async def create_thread(
        self, user_id: Optional[str] = None, title: Optional[str] = None,
    ) -> UUID:
        tid = uuid4()
        now = self._now()
        self.threads[tid] = {
            "thread_id": tid,
            "created_at": now,
            "updated_at": now,
            "user_id": user_id,
            "title": title,
            "metadata": {},
        }
        return tid

    async def get_thread(self, thread_id: UUID) -> Optional[ThreadRow]:
        row = self.threads.get(thread_id)
        return ThreadRow(**row) if row else None

    async def list_threads(
        self, user_id: Optional[str], limit: int = 50,
    ) -> list[ThreadRow]:
        rows = [
            t for t in self.threads.values()
            if t["user_id"] == user_id
            and t["metadata"].get("deleted_at") is None
        ]
        rows.sort(key=lambda r: r["updated_at"], reverse=True)
        return [ThreadRow(**r) for r in rows[:limit]]

    async def append_message(
        self,
        thread_id: UUID,
        role: str,
        content: str,
        *,
        tool_calls: Optional[list[dict[str, Any]]] = None,
        tool_name: Optional[str] = None,
    ) -> int:
        mid = self._next_msg_id
        self._next_msg_id += 1
        self.messages.append({
            "message_id": mid,
            "thread_id": thread_id,
            "role": role,
            "content": content,
            "tool_calls": tool_calls,
            "tool_name": tool_name,
            "created_at": self._now(),
        })
        if thread_id in self.threads:
            self.threads[thread_id]["updated_at"] = self._now()
        return mid

    async def list_messages(
        self, thread_id: UUID, limit: int = 200,
    ) -> list[MessageRow]:
        rows = [m for m in self.messages if m["thread_id"] == thread_id]
        rows.sort(key=lambda m: (m["created_at"], m["message_id"]))
        return [MessageRow(**m) for m in rows[:limit]]

    async def soft_delete_thread(self, thread_id: UUID) -> None:
        if thread_id in self.threads:
            self.threads[thread_id]["metadata"] = {
                **self.threads[thread_id]["metadata"],
                "deleted_at": self._now().isoformat(),
            }

    async def update_thread_title(self, thread_id: UUID, title: str) -> None:
        if thread_id in self.threads:
            self.threads[thread_id]["title"] = title


class _StubOrchestrator:
    """Echoes the user message back as both a token stream and a sync reply.

    Matches the Protocol expected by ChatService (`process` and
    `process_stream`).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict] | None]] = []

    async def process(self, user_message: str, context: dict | None = None) -> dict:
        self.calls.append(("process", None))
        return {"content": f"echo: {user_message}", "visualizations": []}

    async def process_stream(
        self,
        user_message: str,
        conv_history: list[dict] | None = None,
    ):
        self.calls.append(("process_stream", conv_history))
        # Yield legacy-format SSE chunks (matching `agents.sse.format_sse`).
        # The service must translate these into ADR-02 §8 events.
        yield f"data: {json.dumps({'type': 'agent_start', 'agent': 'analyst'})}\n\n"
        for token in (f"echo: {user_message}").split():
            yield f"data: {json.dumps({'type': 'content', 'text': token + ' '})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def repo() -> _FakeRepo:
    return _FakeRepo()


@pytest.fixture
def orchestrator() -> _StubOrchestrator:
    return _StubOrchestrator()


@pytest.fixture
def service(repo: _FakeRepo, orchestrator: _StubOrchestrator) -> ChatService:
    return ChatService(orchestrator=orchestrator, repository=repo)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_format_sse_event_shape() -> None:
    """Output must be `event: name\\ndata: <json>\\n\\n` per ADR-02 §8."""
    out = format_sse_event("token", {"text": "สวัสดี"})
    assert out.startswith("event: token\n")
    assert "data: " in out
    assert out.endswith("\n\n")
    # JSON payload must round-trip — including the Thai content.
    data_line = [l for l in out.splitlines() if l.startswith("data: ")][0]
    payload = json.loads(data_line[len("data: "):])
    assert payload == {"text": "สวัสดี"}


@pytest.mark.anyio
async def test_create_thread_then_list_finds_it(
    service: ChatService, repo: _FakeRepo,
) -> None:
    """create_thread must produce a row that list_threads returns."""
    out = await service.create_thread(user_id="alice", first_message="hi there")
    assert "thread_id" in out
    assert out["title"] == "hi there"

    listed = await service.list_threads(user_id="alice")
    assert len(listed) == 1
    assert listed[0]["thread_id"] == out["thread_id"]
    # Other users see nothing.
    assert await service.list_threads(user_id="bob") == []


@pytest.mark.anyio
async def test_chat_round_trips_messages(
    service: ChatService, repo: _FakeRepo,
) -> None:
    """chat() persists user msg + assistant reply; list_messages returns both."""
    out = await service.create_thread(user_id="alice")
    tid = UUID(out["thread_id"])

    reply = await service.chat(thread_id=tid, user_message="ping")
    assert reply["content"] == "echo: ping"

    msgs = await service.list_messages(thread_id=tid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "ping"
    assert msgs[1]["content"] == "echo: ping"


@pytest.mark.anyio
async def test_stream_yields_expected_event_order(
    service: ChatService, repo: _FakeRepo,
) -> None:
    """Stream emits thread_id → token* → done; persists assistant reply."""
    out = await service.create_thread(user_id="alice")
    tid = UUID(out["thread_id"])

    chunks: list[str] = []
    async for chunk in service.stream(thread_id=tid, user_message="hello world"):
        chunks.append(chunk)

    # Extract event names.
    events: list[str] = []
    for c in chunks:
        for line in c.splitlines():
            if line.startswith("event: "):
                events.append(line[len("event: "):])

    assert events[0] == "thread_id"
    assert events[-1] == "done"
    # At least one token event in between.
    assert "token" in events

    # The orchestrator stub yields three space-delimited tokens of "echo: hello world".
    msgs = await service.list_messages(thread_id=tid)
    roles = [m["role"] for m in msgs]
    # user → assistant; agent_start / done are NOT persisted, only the
    # joined token text.
    assert roles == ["user", "assistant"]
    # Assistant content must be the concatenated tokens (with the trailing
    # space on each, then stripped at persistence).
    assert msgs[1]["content"].startswith("echo:")


@pytest.mark.anyio
async def test_stream_passes_history_to_orchestrator(
    service: ChatService, repo: _FakeRepo, orchestrator: _StubOrchestrator,
) -> None:
    """Second turn must reconstruct history from the DB and forward it."""
    out = await service.create_thread(user_id="alice")
    tid = UUID(out["thread_id"])

    # First turn (sync) — populates history.
    await service.chat(thread_id=tid, user_message="first")

    # Second turn (stream) — orchestrator should see "first" + the assistant
    # reply in conv_history.
    async for _ in service.stream(thread_id=tid, user_message="second"):
        pass

    stream_calls = [c for c in orchestrator.calls if c[0] == "process_stream"]
    assert len(stream_calls) == 1
    history = stream_calls[0][1]
    assert history is not None
    # The latest user message ("second") is dropped before forwarding —
    # the orchestrator gets it via the `user_message` arg.
    assert history[-1]["role"] == "assistant"
    assert "echo: first" in history[-1]["content"]


@pytest.mark.anyio
async def test_soft_delete_hides_from_list_threads(
    service: ChatService, repo: _FakeRepo,
) -> None:
    """delete_thread() removes the thread from list_threads but not the DB."""
    out = await service.create_thread(user_id="alice", first_message="bye")
    tid = UUID(out["thread_id"])

    await service.delete_thread(thread_id=tid)
    listed = await service.list_threads(user_id="alice")
    assert listed == []
    # Row still exists in the underlying repo (soft-delete, not hard-delete).
    assert tid in repo.threads
    assert repo.threads[tid]["metadata"]["deleted_at"] is not None


@pytest.mark.anyio
async def test_chat_handles_orchestrator_error(
    service: ChatService, repo: _FakeRepo, orchestrator: _StubOrchestrator,
) -> None:
    """If the orchestrator raises, chat() returns an error reply but persists it."""
    out = await service.create_thread(user_id="alice")
    tid = UUID(out["thread_id"])

    async def _boom(_msg: str, context=None):
        raise RuntimeError("LLM down")

    orchestrator.process = _boom  # type: ignore[assignment]

    reply = await service.chat(thread_id=tid, user_message="ping")
    assert reply["error"] is True
    assert "LLM down" in reply["content"]
    msgs = await service.list_messages(thread_id=tid)
    # User msg + error assistant reply both persisted.
    assert [m["role"] for m in msgs] == ["user", "assistant"]
