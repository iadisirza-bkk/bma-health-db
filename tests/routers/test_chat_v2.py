"""Integration tests for the v2 chat router (`/api/v2/chat/...`).

What we exercise here:
    - POST /threads          create + auto-title from first_message
    - GET  /threads          list (anonymous + authed paths)
    - GET  /threads/{id}     list_messages
    - POST /threads/{id}/messages   sync chat
    - POST /threads/{id}/stream     SSE stream (named-event format)
    - DELETE /threads/{id}   soft-delete
    - 400 on empty message
    - SSE event ordering: thread_id → token+ → tool_call/result → done
    - Persistence: after stream() the user msg + assistant msg are in the DB

The orchestrator is replaced with a fake that yields a canned legacy SSE
stream — no LMStudio, no DB writes for the orchestrator path. The
`ChatRepository` is replaced with an in-memory fake so the test does not
require PostgreSQL either; this keeps the suite hermetic and fast.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, AsyncGenerator, Optional
from uuid import UUID, uuid4
from datetime import datetime, timezone

import pytest

# Mirror tests/agents/test_chat_in_scope.py — make `api/` importable.
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "api"))

from repositories.chat_rows import MessageRow, ThreadRow  # noqa: E402
from services.chat.service import ChatService, format_sse_event  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakeOrchestrator:
    """Minimal orchestrator stub — emits a fixed legacy SSE stream.

    Matches the `_OrchestratorProtocol` defined in services/chat/service.py.
    """

    def __init__(
        self,
        *,
        sync_content: str = "ภาพรวม: ผู้คัดกรองทั้งหมด 181 คน",
        sync_viz: Optional[list[dict[str, Any]]] = None,
        stream_chunks: Optional[list[str]] = None,
        raise_on_process: bool = False,
        raise_on_stream: bool = False,
    ) -> None:
        self.sync_content = sync_content
        self.sync_viz = sync_viz or []
        self.stream_chunks = stream_chunks
        self.raise_on_process = raise_on_process
        self.raise_on_stream = raise_on_stream
        self.process_calls: list[str] = []
        self.stream_calls: list[tuple[str, list[dict[str, Any]]]] = []

    async def process(
        self,
        user_message: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.process_calls.append(user_message)
        if self.raise_on_process:
            raise RuntimeError("synthetic process failure")
        return {"content": self.sync_content, "visualizations": self.sync_viz}

    async def process_stream(
        self,
        user_message: str,
        conv_history: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[str, None]:
        self.stream_calls.append((user_message, conv_history or []))
        if self.raise_on_stream:
            raise RuntimeError("synthetic stream failure")
        chunks = self.stream_chunks or [
            f"data: {json.dumps({'type': 'content', 'text': 'สวัสดี '})}\n\n",
            f"data: {json.dumps({'type': 'content', 'text': 'ครับ'})}\n\n",
            f"data: {json.dumps({'type': 'tool_call', 'name': 'query_health_data', 'args': {'group_by': 'disease'}})}\n\n",
            f"data: {json.dumps({'type': 'visualization', 'data': {'type': 'bar', 'title': 'P', 'spec_id': 'prevalence_by_zone', 'filters': {'disease': 'dm'}}})}\n\n",
            f"data: {json.dumps({'type': 'content', 'text': ' พบ 181 คน'})}\n\n",
            f"data: {json.dumps({'type': 'done'})}\n\n",
        ]
        for c in chunks:
            yield c


class _FakeChatRepository:
    """In-memory replacement for ChatRepository — no DB needed."""

    def __init__(self) -> None:
        self.threads: dict[UUID, ThreadRow] = {}
        self.messages: dict[UUID, list[MessageRow]] = {}
        self._next_msg_id = 1

    async def create_thread(
        self,
        user_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> UUID:
        tid = uuid4()
        now = datetime.now(timezone.utc)
        self.threads[tid] = ThreadRow(
            thread_id=tid,
            created_at=now,
            updated_at=now,
            user_id=user_id,
            title=title,
            metadata={},
        )
        self.messages[tid] = []
        return tid

    async def get_thread(self, thread_id: UUID) -> Optional[ThreadRow]:
        return self.threads.get(thread_id)

    async def list_threads(
        self,
        user_id: Optional[str] = None,
    ) -> list[ThreadRow]:
        rows = [
            t for t in self.threads.values()
            if (user_id is None and t.user_id is None) or t.user_id == user_id
        ]
        return sorted(rows, key=lambda r: r.updated_at, reverse=True)

    async def soft_delete_thread(self, thread_id: UUID) -> None:
        self.threads.pop(thread_id, None)
        self.messages.pop(thread_id, None)

    async def append_message(
        self,
        thread_id: UUID,
        role: str,
        content: str,
        tool_calls: Optional[list[dict[str, Any]]] = None,
        tool_name: Optional[str] = None,
    ) -> int:
        if thread_id not in self.messages:
            # Auto-create so we don't need to seed in tests.
            self.messages[thread_id] = []
        mid = self._next_msg_id
        self._next_msg_id += 1
        self.messages[thread_id].append(
            MessageRow(
                message_id=mid,
                thread_id=thread_id,
                role=role,  # type: ignore[arg-type]
                content=content,
                tool_calls=tool_calls,
                tool_name=tool_name,
                created_at=datetime.now(timezone.utc),
            )
        )
        # Touch the thread's updated_at if known.
        if thread_id in self.threads:
            t = self.threads[thread_id]
            self.threads[thread_id] = t.model_copy(
                update={"updated_at": datetime.now(timezone.utc)}
            )
        return mid

    async def list_messages(self, thread_id: UUID) -> list[MessageRow]:
        return list(self.messages.get(thread_id, []))


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def fake_repo() -> _FakeChatRepository:
    return _FakeChatRepository()


@pytest.fixture()
def fake_orch() -> _FakeOrchestrator:
    return _FakeOrchestrator()


@pytest.fixture()
def chat_app(app, fake_orch, fake_repo):
    """FastAPI app with the chat-v2 service swapped for fakes.

    Uses dependency_overrides so the rest of the API is untouched and we
    can safely mutate the override in the same test session.
    """
    from routers.chat_v2 import get_chat_service

    service = ChatService(orchestrator=fake_orch, repository=fake_repo)
    app.dependency_overrides[get_chat_service] = lambda: service
    yield app
    app.dependency_overrides.pop(get_chat_service, None)


@pytest.fixture()
async def chat_client(chat_app, api_key):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=chat_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.headers["X-API-Key"] = api_key
        yield ac


# --------------------------------------------------------------------------- #
# Tests — thread CRUD
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_create_thread_no_first_message(chat_client):
    res = await chat_client.post("/api/v2/chat/threads", json={})
    assert res.status_code == 200
    data = res.json()
    assert UUID(data["thread_id"])  # parses
    assert data["title"] is None
    assert "created_at" in data


@pytest.mark.anyio
async def test_create_thread_auto_titles_from_first_message(chat_client):
    first = "ภาพรวมโรคทั้งหมดในกรุงเทพ"
    res = await chat_client.post(
        "/api/v2/chat/threads", json={"first_message": first}
    )
    assert res.status_code == 200
    assert res.json()["title"] == first[:80]


@pytest.mark.anyio
async def test_create_thread_long_first_message_truncated_to_80(chat_client):
    first = "ก" * 200
    res = await chat_client.post(
        "/api/v2/chat/threads", json={"first_message": first}
    )
    assert res.status_code == 200
    assert len(res.json()["title"]) == 80


@pytest.mark.anyio
async def test_list_threads_empty(chat_client):
    res = await chat_client.get("/api/v2/chat/threads")
    assert res.status_code == 200
    assert res.json() == []


@pytest.mark.anyio
async def test_list_threads_returns_created(chat_client):
    await chat_client.post("/api/v2/chat/threads", json={"first_message": "a"})
    await chat_client.post("/api/v2/chat/threads", json={"first_message": "b"})
    res = await chat_client.get("/api/v2/chat/threads")
    assert res.status_code == 200
    threads = res.json()
    assert len(threads) == 2
    # Newest-first order.
    assert all("thread_id" in t for t in threads)


@pytest.mark.anyio
async def test_list_messages_empty_thread(chat_client):
    create = await chat_client.post(
        "/api/v2/chat/threads", json={"first_message": "x"}
    )
    tid = create.json()["thread_id"]
    res = await chat_client.get(f"/api/v2/chat/threads/{tid}")
    assert res.status_code == 200
    assert res.json() == []


@pytest.mark.anyio
async def test_delete_thread_returns_ok(chat_client):
    create = await chat_client.post("/api/v2/chat/threads", json={})
    tid = create.json()["thread_id"]
    res = await chat_client.delete(f"/api/v2/chat/threads/{tid}")
    assert res.status_code == 200
    assert res.json() == {"ok": True, "thread_id": tid}


@pytest.mark.anyio
async def test_delete_invalid_uuid_returns_422(chat_client):
    res = await chat_client.delete("/api/v2/chat/threads/not-a-uuid")
    assert res.status_code == 422


# --------------------------------------------------------------------------- #
# Tests — sync send_message
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_send_message_returns_orchestrator_content(chat_client, fake_orch):
    create = await chat_client.post("/api/v2/chat/threads", json={})
    tid = create.json()["thread_id"]

    res = await chat_client.post(
        f"/api/v2/chat/threads/{tid}/messages",
        json={"message": "ภาพรวม"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["thread_id"] == tid
    assert "181" in body["content"]
    assert fake_orch.process_calls == ["ภาพรวม"]


@pytest.mark.anyio
async def test_send_message_persists_user_and_assistant(
    chat_client, fake_repo
):
    create = await chat_client.post("/api/v2/chat/threads", json={})
    tid_str = create.json()["thread_id"]
    tid = UUID(tid_str)

    await chat_client.post(
        f"/api/v2/chat/threads/{tid_str}/messages", json={"message": "ภาพรวม"}
    )

    rows = fake_repo.messages[tid]
    roles = [r.role for r in rows]
    assert "user" in roles
    assert "assistant" in roles


@pytest.mark.anyio
async def test_send_message_empty_returns_400(chat_client):
    create = await chat_client.post("/api/v2/chat/threads", json={})
    tid = create.json()["thread_id"]
    res = await chat_client.post(
        f"/api/v2/chat/threads/{tid}/messages", json={"message": "   "}
    )
    assert res.status_code == 400
    assert "non-empty" in res.json()["detail"]


@pytest.mark.anyio
async def test_send_message_orchestrator_failure_persists_error(
    chat_app, api_key, fake_repo
):
    """If orchestrator.process throws, the user msg + an error reply must persist."""
    from httpx import ASGITransport, AsyncClient
    from routers.chat_v2 import get_chat_service

    boom_orch = _FakeOrchestrator(raise_on_process=True)
    chat_app.dependency_overrides[get_chat_service] = lambda: ChatService(
        orchestrator=boom_orch, repository=fake_repo
    )
    transport = ASGITransport(app=chat_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.headers["X-API-Key"] = api_key
        create = await ac.post("/api/v2/chat/threads", json={})
        tid_str = create.json()["thread_id"]
        res = await ac.post(
            f"/api/v2/chat/threads/{tid_str}/messages", json={"message": "x"}
        )
    assert res.status_code == 200
    body = res.json()
    assert body.get("error") is True
    rows = fake_repo.messages[UUID(tid_str)]
    # user + assistant(error) — both persisted.
    assert [r.role for r in rows] == ["user", "assistant"]
    assert "failed" in rows[1].content


# --------------------------------------------------------------------------- #
# Tests — SSE stream
# --------------------------------------------------------------------------- #


def _parse_sse_events(body: str) -> list[tuple[str, dict]]:
    """Parse a full SSE response body into (event_name, data) tuples."""
    out: list[tuple[str, dict]] = []
    for chunk in re.split(r"\n\n+", body):
        if not chunk.strip():
            continue
        ev = "message"
        data_lines: list[str] = []
        for line in chunk.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            payload = {"raw": "\n".join(data_lines)}
        out.append((ev, payload))
    return out


@pytest.mark.anyio
async def test_stream_emits_named_events_in_order(chat_client):
    create = await chat_client.post("/api/v2/chat/threads", json={})
    tid = create.json()["thread_id"]

    res = await chat_client.post(
        f"/api/v2/chat/threads/{tid}/stream", json={"message": "ภาพรวม"}
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse_events(res.text)
    names = [e[0] for e in events]

    # Must start with thread_id and end with done.
    assert names[0] == "thread_id"
    assert names[-1] == "done"
    # Must contain at least one token, one tool_call, one tool_result.
    assert "token" in names
    assert "tool_call" in names
    assert "tool_result" in names
    # And a chart event derived from the visualization.
    assert "chart" in names


@pytest.mark.anyio
async def test_stream_concatenated_tokens_match_expected_text(chat_client):
    create = await chat_client.post("/api/v2/chat/threads", json={})
    tid = create.json()["thread_id"]
    res = await chat_client.post(
        f"/api/v2/chat/threads/{tid}/stream", json={"message": "ภาพรวม"}
    )
    events = _parse_sse_events(res.text)
    text = "".join(p["text"] for ev, p in events if ev == "token")
    assert text == "สวัสดี ครับ พบ 181 คน"


@pytest.mark.anyio
async def test_stream_persists_assistant_and_tool_messages(chat_client, fake_repo):
    create = await chat_client.post("/api/v2/chat/threads", json={})
    tid_str = create.json()["thread_id"]

    res = await chat_client.post(
        f"/api/v2/chat/threads/{tid_str}/stream", json={"message": "ภาพรวม"}
    )
    assert res.status_code == 200

    rows = fake_repo.messages[UUID(tid_str)]
    roles = [r.role for r in rows]
    # user msg + assistant joined token stream + at least one tool record.
    assert "user" in roles
    assert "assistant" in roles
    assert "tool" in roles
    # The assistant content should be the joined token stream.
    assistant_row = next(r for r in rows if r.role == "assistant")
    assert "181" in assistant_row.content


@pytest.mark.anyio
async def test_stream_rejects_empty_message(chat_client):
    create = await chat_client.post("/api/v2/chat/threads", json={})
    tid = create.json()["thread_id"]
    res = await chat_client.post(
        f"/api/v2/chat/threads/{tid}/stream", json={"message": ""}
    )
    assert res.status_code == 400


@pytest.mark.anyio
async def test_stream_orchestrator_failure_emits_error_then_done(
    chat_app, api_key, fake_repo
):
    from httpx import ASGITransport, AsyncClient
    from routers.chat_v2 import get_chat_service

    boom = _FakeOrchestrator(raise_on_stream=True)
    chat_app.dependency_overrides[get_chat_service] = lambda: ChatService(
        orchestrator=boom, repository=fake_repo
    )
    transport = ASGITransport(app=chat_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.headers["X-API-Key"] = api_key
        create = await ac.post("/api/v2/chat/threads", json={})
        tid = create.json()["thread_id"]
        res = await ac.post(
            f"/api/v2/chat/threads/{tid}/stream", json={"message": "x"}
        )
    assert res.status_code == 200

    events = _parse_sse_events(res.text)
    names = [e[0] for e in events]
    assert "error" in names
    assert names[-1] == "done"


# --------------------------------------------------------------------------- #
# Tests — auth (API key middleware applies to v2 routes)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_no_api_key_returns_401(chat_app):
    """The X-API-Key middleware should reject requests without a key."""
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=chat_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.get("/api/v2/chat/threads")
    assert res.status_code == 401


# --------------------------------------------------------------------------- #
# Tests — wire-format helper (defensive — frontend parser depends on this)
# --------------------------------------------------------------------------- #


def test_format_sse_event_thai_passes_through_as_utf8():
    """ensure_ascii=False keeps Thai readable in `curl` output."""
    out = format_sse_event("token", {"text": "สวัสดี"})
    assert out.startswith("event: token\ndata: ")
    assert out.endswith("\n\n")
    assert "สวัสดี" in out
    assert "\\u" not in out  # not escaped


def test_format_sse_event_no_embedded_newlines_in_data_line():
    """Newlines inside the JSON value must be escaped, never raw."""
    out = format_sse_event("token", {"text": "line1\nline2"})
    # The single data: line should have exactly two newlines after it
    # (data line LF + blank line LF), and the embedded newline must be
    # escaped to \n inside the JSON string.
    data_section = out.split("data: ", 1)[1]
    # Stripping the trailing \n\n leaves the JSON payload only.
    payload_str = data_section.rstrip("\n")
    assert "\n" not in payload_str
    parsed = json.loads(payload_str)
    assert parsed["text"] == "line1\nline2"
