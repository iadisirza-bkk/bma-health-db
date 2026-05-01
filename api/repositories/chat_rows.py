"""Pydantic v2 row models for the chat persistence layer (ADR-02 §6).

Mirrors the convention used in `repositories/rows.py`:
    * One model per table row shape returned by the repository.
    * `extra="forbid"` so a typo in a column name fails loudly at parse time.
    * Optional fields default to `None` — a column being NULL in PostgreSQL
      should not require special handling at the call site.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _RowBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ThreadRow(_RowBase):
    """One row of `bma_med.chat_thread`."""

    thread_id: UUID
    created_at: datetime
    updated_at: datetime
    user_id: Optional[str] = None
    title: Optional[str] = None
    metadata: dict[str, Any] = {}


class MessageRow(_RowBase):
    """One row of `bma_med.chat_message`.

    `role` is constrained to the same four values enforced by the
    SQL CHECK constraint so consumers can rely on exhaustive matching.
    """

    message_id: int
    thread_id: UUID
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: Optional[list[dict[str, Any]]] = None
    tool_name: Optional[str] = None
    created_at: datetime
