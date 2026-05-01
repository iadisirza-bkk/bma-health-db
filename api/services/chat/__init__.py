"""ChatService — DB-backed conversation orchestration (ADR-02 §7).

Re-exports the public surface so callers can simply:
    from services.chat import ChatService, format_sse_event
"""
from __future__ import annotations

from .service import ChatService, format_sse_event

__all__ = ["ChatService", "format_sse_event"]
