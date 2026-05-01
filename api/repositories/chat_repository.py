"""ChatRepository — DB-backed conversation persistence (ADR-02 §6).

All chat reads/writes funnel through this class. Mirrors the `MVRepository`
style but uses the WRITER pool (`database.get_writer_conn()`) because the
chat tables are mutated as well as read.

Privacy model
-------------
The chat_message table is at the PII tier (a user might paste an HN/PID
into a question), so:
    * `bma_med_loader` (writer) is the only role that can INSERT/UPDATE.
    * Plain `bma_med_reader` has NO grants — these methods all use the
      writer pool and never expose rows to the public reader path.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Iterable, Optional
from uuid import UUID

import psycopg2.extras

from database import get_writer_conn

from .base import Repository
from .chat_rows import MessageRow, ThreadRow

logger = logging.getLogger(__name__)


def _coerce(value: Any) -> Any:
    """Mirror `repositories.base._clean` for the writer pool."""
    if isinstance(value, Decimal):
        return float(value)
    return value


def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {k: _coerce(v) for k, v in row.items()}


class ChatRepository(Repository):
    """SQL-backed CRUD for `bma_med.chat_thread` and `bma_med.chat_message`.

    All methods are `async def` for symmetry with the rest of the
    repository layer; psycopg2 itself is synchronous, so the bodies run
    inline. This is the same pattern used by `MVRepository`.
    """

    # ------------------------------------------------------------------ #
    # Internal write helpers — uses the writer pool (etl_user / loader).
    # ------------------------------------------------------------------ #

    @contextmanager
    def _cursor(self):
        """Yield a (conn, RealDictCursor) pair from the writer pool.

        Commits on clean exit; rolls back on exception.
        """
        with get_writer_conn() as conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    yield conn, cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    async def _exec_returning_one(
        self,
        sql: str,
        params: Optional[Iterable[Any]] = None,
    ) -> Optional[dict[str, Any]]:
        with self._cursor() as (_conn, cur):
            cur.execute(sql, tuple(params) if params is not None else None)
            row = cur.fetchone()
        return _row_to_dict(row) if row else None

    async def _exec_returning_all(
        self,
        sql: str,
        params: Optional[Iterable[Any]] = None,
    ) -> list[dict[str, Any]]:
        with self._cursor() as (_conn, cur):
            cur.execute(sql, tuple(params) if params is not None else None)
            rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def _exec_no_return(
        self,
        sql: str,
        params: Optional[Iterable[Any]] = None,
    ) -> None:
        with self._cursor() as (_conn, cur):
            cur.execute(sql, tuple(params) if params is not None else None)

    # ------------------------------------------------------------------ #
    # Threads
    # ------------------------------------------------------------------ #

    async def create_thread(
        self,
        user_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> UUID:
        """Insert a new thread and return its UUID."""
        sql = """
            INSERT INTO bma_med.chat_thread (user_id, title)
            VALUES (%s, %s)
            RETURNING thread_id
        """
        row = await self._exec_returning_one(sql, (user_id, title))
        if not row:
            # Should not happen — RETURNING on a successful INSERT always yields.
            raise RuntimeError("create_thread: no thread_id returned")
        return row["thread_id"]

    async def get_thread(self, thread_id: UUID) -> Optional[ThreadRow]:
        """Return the thread row, or None if missing."""
        sql = """
            SELECT thread_id, created_at, updated_at, user_id, title, metadata
              FROM bma_med.chat_thread
             WHERE thread_id = %s
        """
        row = await self._exec_returning_one(sql, (str(thread_id),))
        if row is None:
            return None
        return ThreadRow(**row)

    async def list_threads(
        self,
        user_id: Optional[str],
        limit: int = 50,
    ) -> list[ThreadRow]:
        """List non-deleted threads, ordered by recency.

        If `user_id` is provided, scope to that user. If not, return all
        threads with NULL `user_id` (anonymous) — anonymous threads are
        never visible to a logged-in user, by design.

        Soft-deleted threads (those with `metadata->>'deleted_at'` set) are
        excluded.
        """
        if user_id is None:
            where = "WHERE user_id IS NULL"
            params: tuple[Any, ...] = ()
        else:
            where = "WHERE user_id = %s"
            params = (user_id,)
        sql = f"""
            SELECT thread_id, created_at, updated_at, user_id, title, metadata
              FROM bma_med.chat_thread
              {where}
               AND (metadata ->> 'deleted_at') IS NULL
             ORDER BY updated_at DESC
             LIMIT %s
        """
        rows = await self._exec_returning_all(sql, params + (limit,))
        return [ThreadRow(**r) for r in rows]

    async def update_thread_title(self, thread_id: UUID, title: str) -> None:
        """Set the human-readable title on a thread."""
        sql = "UPDATE bma_med.chat_thread SET title = %s WHERE thread_id = %s"
        await self._exec_no_return(sql, (title, str(thread_id)))

    async def soft_delete_thread(self, thread_id: UUID) -> None:
        """Mark a thread as deleted via `metadata.deleted_at`.

        We never DELETE rows: messages may be subject to retention rules
        and we always want the audit trail. A scheduled job can hard-delete
        rows that have been soft-deleted for >N days, separately.
        """
        sql = """
            UPDATE bma_med.chat_thread
               SET metadata = metadata || jsonb_build_object('deleted_at', now()::text)
             WHERE thread_id = %s
        """
        await self._exec_no_return(sql, (str(thread_id),))

    # ------------------------------------------------------------------ #
    # Messages
    # ------------------------------------------------------------------ #

    async def append_message(
        self,
        thread_id: UUID,
        role: str,
        content: str,
        *,
        tool_calls: Optional[list[dict[str, Any]]] = None,
        tool_name: Optional[str] = None,
    ) -> int:
        """Append one message to a thread, return the new `message_id`.

        Also bumps the parent thread's `updated_at` so list ordering reflects
        recency. (The `chat_thread_touch_updated_at` trigger handles UPDATEs
        but we want a touch even if no other column changed.)
        """
        if role not in ("system", "user", "assistant", "tool"):
            # Defence-in-depth: the SQL CHECK enforces the same constraint
            # but failing fast in Python gives a clearer error to the caller.
            raise ValueError(f"invalid role {role!r}")
        tool_calls_json = json.dumps(tool_calls) if tool_calls is not None else None
        sql = """
            INSERT INTO bma_med.chat_message
                (thread_id, role, content, tool_calls, tool_name)
            VALUES (%s, %s, %s, %s::jsonb, %s)
            RETURNING message_id
        """
        with self._cursor() as (_conn, cur):
            cur.execute(
                sql,
                (str(thread_id), role, content, tool_calls_json, tool_name),
            )
            row = cur.fetchone()
            cur.execute(
                "UPDATE bma_med.chat_thread SET updated_at = now() WHERE thread_id = %s",
                (str(thread_id),),
            )
        if not row:
            raise RuntimeError("append_message: no message_id returned")
        return int(row["message_id"])

    async def list_messages(
        self,
        thread_id: UUID,
        limit: int = 200,
    ) -> list[MessageRow]:
        """Return messages for a thread in chronological order."""
        sql = """
            SELECT message_id, thread_id, role, content,
                   tool_calls, tool_name, created_at
              FROM bma_med.chat_message
             WHERE thread_id = %s
             ORDER BY created_at, message_id
             LIMIT %s
        """
        rows = await self._exec_returning_all(sql, (str(thread_id), limit))
        return [MessageRow(**r) for r in rows]
