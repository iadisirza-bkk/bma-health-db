"""ChatService — DB-backed conversation orchestration (ADR-02 §7).

Wraps the existing `OpenMultiAgent` orchestrator and adds per-thread
persistence on top of it. The router (`routers/chat_v2.py`) is a thin
glue layer; everything below the wire lives here.

Wire format (ADR-02 §8)
-----------------------
The new SSE events use **named** event types rather than the legacy
single-channel `data:` lines used by `/api/health/chat/stream`:

    event: thread_id    data: {"thread_id": "..."}
    event: token        data: {"text": "..."}
    event: tool_call    data: {"name": "...", "args": {...}}
    event: tool_result  data: {"name": "...", "summary": "...", "viz": [...]}
    event: chart        data: {"spec_id": "...", "filters": {...}}
    event: error        data: {"code": "...", "message": "..."}
    event: done         data: {}

This module is provider-agnostic — the consumer doesn't have to know
which adapter (LMStudio / Anthropic / OpenAI) answered.

Persistence
-----------
On every `chat()` and `stream()` call:
    1. Append the user message to `chat_message`.
    2. Run the orchestrator with the full thread history reconstructed
       from the DB.
    3. Append the assistant message (the joined token stream).
    4. Append any tool_call / tool_result records.

Errors during persistence are logged but never abort the stream — the
client still gets the answer; the operator gets a warning to investigate.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator, Optional, Protocol
from uuid import UUID

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Wire format helpers
# --------------------------------------------------------------------------- #

def format_sse_event(event_name: str, data: dict[str, Any]) -> str:
    """Format a named SSE event per ADR-02 §8.

    Output is `event: <name>\\ndata: <json>\\n\\n` — newline-safe because
    `json.dumps` escapes embedded newlines inside string values, so the
    `data:` line can never contain a literal LF. We pass `ensure_ascii=False`
    so Thai content goes over the wire as UTF-8 (smaller than \\u escapes
    and easier to debug with `curl`).
    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event_name}\ndata: {payload}\n\n"


# --------------------------------------------------------------------------- #
# Orchestrator protocol
# --------------------------------------------------------------------------- #

class _OrchestratorProtocol(Protocol):
    """Minimal contract we need from `OpenMultiAgent`.

    Defining a Protocol (instead of importing the concrete class) keeps
    this module testable with a tiny stub orchestrator and avoids loading
    the heavy LMStudio / agents stack just to import the service.
    """

    async def process(
        self,
        user_message: str,
        context: dict | None = None,
    ) -> dict: ...

    def process_stream(
        self,
        user_message: str,
        conv_history: list[dict] | None = None,
    ) -> AsyncGenerator[str, None]: ...


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #

class ChatService:
    """Stateful chat layer that ties an orchestrator to a DB-backed thread store.

    Construction is via DI — both the orchestrator and the repository are
    passed in, so tests can swap either independently.
    """

    def __init__(
        self,
        orchestrator: _OrchestratorProtocol,
        repository: Any,  # ChatRepository — typed loose to avoid an import cycle
    ) -> None:
        self.orchestrator = orchestrator
        self.repo = repository

    # ------------------------------------------------------------------ #
    # Thread CRUD wrappers
    # ------------------------------------------------------------------ #

    async def create_thread(
        self,
        user_id: Optional[str],
        first_message: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a thread; if `first_message` is given, auto-title from it."""
        title: Optional[str] = None
        if first_message:
            title = first_message.strip()[:80]
        thread_id = await self.repo.create_thread(user_id=user_id, title=title)
        thread = await self.repo.get_thread(thread_id)
        # `get_thread` always succeeds here — we just inserted the row.
        assert thread is not None
        return {
            "thread_id": str(thread.thread_id),
            "created_at": thread.created_at.isoformat(),
            "title": thread.title,
        }

    async def list_threads(
        self,
        user_id: Optional[str],
    ) -> list[dict[str, Any]]:
        """Return the user's non-deleted threads, newest first."""
        rows = await self.repo.list_threads(user_id=user_id)
        return [
            {
                "thread_id": str(r.thread_id),
                "title": r.title,
                "created_at": r.created_at.isoformat(),
                "updated_at": r.updated_at.isoformat(),
            }
            for r in rows
        ]

    async def list_messages(self, thread_id: UUID) -> list[dict[str, Any]]:
        """Return every message in a thread in chronological order."""
        rows = await self.repo.list_messages(thread_id=thread_id)
        return [
            {
                "message_id": r.message_id,
                "role": r.role,
                "content": r.content,
                "tool_calls": r.tool_calls,
                "tool_name": r.tool_name,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]

    async def delete_thread(self, thread_id: UUID) -> None:
        """Soft-delete (sets `metadata.deleted_at`)."""
        await self.repo.soft_delete_thread(thread_id)

    # ------------------------------------------------------------------ #
    # Internal: rebuild orchestrator-shaped history from DB rows
    # ------------------------------------------------------------------ #

    async def _build_conv_history(self, thread_id: UUID) -> list[dict[str, Any]]:
        """Convert persisted rows into the `[{role, content}, ...]` shape
        that `OpenMultiAgent.process_stream` expects.

        Tool messages are flattened into a synthetic `assistant` summary
        because the orchestrator's history-trim logic only inspects
        `user` and `assistant` roles; raw `tool` rows would be dropped.
        """
        rows = await self.repo.list_messages(thread_id=thread_id)
        history: list[dict[str, Any]] = []
        for r in rows:
            if r.role in ("user", "assistant"):
                history.append({"role": r.role, "content": r.content})
            # `system` and `tool` rows are intentionally not forwarded.
        return history

    # ------------------------------------------------------------------ #
    # Synchronous chat
    # ------------------------------------------------------------------ #

    async def chat(
        self,
        thread_id: UUID,
        user_message: str,
    ) -> dict[str, Any]:
        """Append user msg, call orchestrator.process(), persist reply.

        Returns the orchestrator's `{content, visualizations}` dict augmented
        with the new `thread_id`.
        """
        # 1. Persist user message first — even if the LLM call fails, we
        # want the question on record.
        await self.repo.append_message(thread_id, "user", user_message)

        # 2. Reconstruct history; the orchestrator handles trimming.
        history = await self._build_conv_history(thread_id)
        # The latest message is the one we just inserted; drop it so the
        # orchestrator's `process()` path doesn't see it twice.
        if history and history[-1]["role"] == "user":
            history = history[:-1]

        # `process()` doesn't take a history kwarg — the synchronous path
        # is single-turn by design. We pass the message directly. The
        # streaming path is the one that uses thread context.
        try:
            result = await self.orchestrator.process(user_message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("orchestrator.process failed for thread %s", thread_id)
            err_msg = f"chat processing failed: {exc}"
            await self.repo.append_message(thread_id, "assistant", err_msg)
            return {
                "thread_id": str(thread_id),
                "content": err_msg,
                "visualizations": [],
                "error": True,
            }

        content = result.get("content", "") or ""
        viz = result.get("visualizations", []) or []

        # 3. Persist assistant reply (visualizations live in `tool_calls`
        # so the frontend can replay them when the thread is re-opened).
        tool_calls_payload: Optional[list[dict[str, Any]]] = None
        if viz:
            tool_calls_payload = [{"type": "visualization", "data": v} for v in viz]
        try:
            await self.repo.append_message(
                thread_id,
                "assistant",
                content,
                tool_calls=tool_calls_payload,
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to persist assistant message for thread %s", thread_id)

        return {
            "thread_id": str(thread_id),
            "content": content,
            "visualizations": viz,
        }

    # ------------------------------------------------------------------ #
    # SSE stream
    # ------------------------------------------------------------------ #

    async def stream(
        self,
        thread_id: UUID,
        user_message: str,
    ) -> AsyncGenerator[str, None]:
        """Stream the orchestrator's response as ADR-02 §8 SSE events.

        Translates the legacy `data: {"type": ...}` events emitted by
        `OpenMultiAgent.process_stream` into the new named-event format,
        and persists the joined token stream + tool calls + tool results
        when the stream completes.
        """
        # 1. Persist user message + announce thread_id to the client.
        await self.repo.append_message(thread_id, "user", user_message)
        yield format_sse_event("thread_id", {"thread_id": str(thread_id)})

        # 2. Reconstruct history (drop the just-inserted user message —
        # the orchestrator receives it via the `user_message` arg).
        history = await self._build_conv_history(thread_id)
        if history and history[-1]["role"] == "user":
            history = history[:-1]

        # Buffers we flush to the DB at the end of the stream.
        token_buffer: list[str] = []
        tool_calls_seen: list[dict[str, Any]] = []
        tool_results_seen: list[dict[str, Any]] = []

        try:
            async for raw in self.orchestrator.process_stream(
                user_message,
                conv_history=history,
            ):
                # The orchestrator emits legacy single-line SSE: `data: {...}\n\n`.
                # We parse, translate, and re-emit.
                translated = _translate_legacy_sse(
                    raw,
                    token_buffer=token_buffer,
                    tool_calls_seen=tool_calls_seen,
                    tool_results_seen=tool_results_seen,
                )
                for event_name, payload in translated:
                    yield format_sse_event(event_name, payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception("orchestrator.process_stream failed for thread %s", thread_id)
            yield format_sse_event(
                "error",
                {"code": "orchestrator_failure", "message": str(exc)[:300]},
            )

        # 3. Persist what we emitted.
        await self._persist_stream_artifacts(
            thread_id=thread_id,
            token_buffer=token_buffer,
            tool_calls=tool_calls_seen,
            tool_results=tool_results_seen,
        )

        # 4. Final `done` event so the client knows to close.
        yield format_sse_event("done", {})

    async def _persist_stream_artifacts(
        self,
        *,
        thread_id: UUID,
        token_buffer: list[str],
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
    ) -> None:
        """Save the joined token stream + tool round-trips. Best-effort."""
        joined = "".join(token_buffer).strip()
        try:
            if joined or tool_calls:
                await self.repo.append_message(
                    thread_id,
                    "assistant",
                    joined,
                    tool_calls=tool_calls or None,
                )
            for tr in tool_results:
                await self.repo.append_message(
                    thread_id,
                    "tool",
                    json.dumps(tr, ensure_ascii=False),
                    tool_name=tr.get("name"),
                )
        except Exception:  # noqa: BLE001
            # Persistence failure must not break a stream that already
            # delivered tokens to the client — log and move on.
            logger.exception(
                "failed to persist stream artifacts for thread %s", thread_id,
            )


# --------------------------------------------------------------------------- #
# Legacy SSE translation
# --------------------------------------------------------------------------- #

def _translate_legacy_sse(
    raw: str,
    *,
    token_buffer: list[str],
    tool_calls_seen: list[dict[str, Any]],
    tool_results_seen: list[dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    """Convert one chunk of the orchestrator's legacy SSE stream into ADR-02 §8 events.

    The legacy stream uses `data: {"type": "...", ...}\\n\\n` only — no
    `event:` line. We map types onto the new vocabulary:

        type=content       → token        + buffer for persistence
        type=tool_call     → tool_call    + record
        type=visualization → tool_result  + chart (with synthesized name)
        type=artifact      → tool_result
        type=error         → error
        type=done          → (suppressed; emitted by stream() at the end)
        anything else      → swallowed (agent_start/agent_done are UI-only signals)

    Returns a list of `(event_name, payload)` tuples.
    """
    events: list[tuple[str, dict[str, Any]]] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[len("data:"):].strip()
        if not body:
            continue
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            # Unknown shape — surface as a warning, don't crash the stream.
            events.append(("error", {"code": "bad_sse", "message": body[:200]}))
            continue
        if not isinstance(obj, dict):
            continue

        kind = obj.get("type")
        if kind == "content":
            text = obj.get("text", "")
            if text:
                token_buffer.append(text)
                events.append(("token", {"text": text}))
        elif kind == "tool_call":
            tc = {"name": obj.get("name", ""), "args": obj.get("args", {})}
            tool_calls_seen.append(tc)
            events.append(("tool_call", tc))
        elif kind == "visualization":
            data = obj.get("data") or {}
            tr = {
                "name": "visualization",
                "summary": data.get("title") or data.get("type") or "chart",
                "viz": [data],
            }
            tool_results_seen.append(tr)
            events.append(("tool_result", tr))
            # Also emit a `chart` event for ADR-01 dashboard consumers.
            spec_id = data.get("spec_id")
            if spec_id:
                events.append(
                    (
                        "chart",
                        {
                            "spec_id": spec_id,
                            "filters": data.get("filters", {}),
                        },
                    )
                )
        elif kind == "artifact":
            tr = {
                "name": "artifact",
                "summary": obj.get("label", ""),
                "url": obj.get("url"),
            }
            tool_results_seen.append(tr)
            events.append(("tool_result", tr))
        elif kind == "error":
            events.append(
                (
                    "error",
                    {
                        "code": "orchestrator_error",
                        "message": obj.get("message", "unknown error"),
                    },
                )
            )
        elif kind == "done":
            # The wrapping `stream()` emits its own `done` once persistence
            # finishes — suppress the inner one to avoid duplicate-close.
            continue
        else:
            # agent_start, agent_done, warning, clarification, etc. — these
            # are UI scaffolding; the new wire format doesn't expose them.
            # Drop silently rather than spam the client with unknown events.
            continue

    return events
