"""Chat v2 API router — DB-backed conversation persistence (ADR-02).

Lives alongside the legacy `/api/health/chat*` routes. Adds:
    POST   /api/v2/chat/threads                       — create
    GET    /api/v2/chat/threads                       — list (per-user)
    GET    /api/v2/chat/threads/{thread_id}           — list messages
    POST   /api/v2/chat/threads/{thread_id}/messages  — sync chat
    POST   /api/v2/chat/threads/{thread_id}/stream    — SSE stream
    DELETE /api/v2/chat/threads/{thread_id}           — soft-delete

Auth model
----------
We try to read the session cookie set by `/api/auth/login` (see `auth.py`).
If it's present and valid, the cookie's `sub` becomes the thread's `user_id`
and the user's threads are listable via `GET /threads`.

If no cookie is present, the request still succeeds — the thread is
created anonymously (`user_id = NULL`) but won't be listable later. This
is a deliberate design: we want the chat to work for unauthenticated
visitors without forcing a login, while still letting logged-in users
see their history.

DI
--
A singleton ChatService is built once per process via `lru_cache`,
mirroring the chart router pattern.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/chat", tags=["chat_v2"])


# --------------------------------------------------------------------------- #
# DI: build the ChatService singleton lazily.
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def get_chat_service():
    """Construct (once) the ChatService for FastAPI's DI system.

    Keeps the heavy orchestrator import lazy so this router stays
    importable even when LMStudio is unreachable at boot — the actual
    orchestrator construction happens on first request.
    """
    from agents import create_orchestrator
    from repositories.chat_repository import ChatRepository
    from services.chat import ChatService

    orchestrator = create_orchestrator()
    repo = ChatRepository()
    return ChatService(orchestrator=orchestrator, repository=repo)


# --------------------------------------------------------------------------- #
# Auth helper — softer than auth.require_session: returns None on missing
# cookie instead of raising, so anonymous chat keeps working.
# --------------------------------------------------------------------------- #
def _user_from_cookie(request: Request) -> Optional[str]:
    """Return the authenticated user_id (cookie `sub`) or None.

    Reuses the JWT verification helper from `auth.py` so cookie semantics
    stay in one place.
    """
    try:
        from auth import SESSION_COOKIE_NAME, _verify_jwt
    except Exception:  # noqa: BLE001
        # Auth module not available (e.g. in a stripped-down test env);
        # treat the request as anonymous.
        return None
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    claims = _verify_jwt(token)
    if claims and claims.get("sub"):
        return str(claims["sub"])
    return None


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #
class CreateThreadRequest(BaseModel):
    first_message: Optional[str] = None
    title: Optional[str] = None


class SendMessageRequest(BaseModel):
    message: str


# --------------------------------------------------------------------------- #
# Routes — threads
# --------------------------------------------------------------------------- #
@router.post("/threads", summary="Create a new chat thread")
async def create_thread(
    body: CreateThreadRequest,
    request: Request,
    service=Depends(get_chat_service),
):
    user_id = _user_from_cookie(request)
    return await service.create_thread(
        user_id=user_id,
        first_message=body.first_message or body.title,
    )


@router.get("/threads", summary="List the caller's chat threads")
async def list_threads(
    request: Request,
    service=Depends(get_chat_service),
):
    user_id = _user_from_cookie(request)
    return await service.list_threads(user_id=user_id)


@router.get(
    "/threads/{thread_id}",
    summary="List all messages in a thread",
)
async def list_messages(
    thread_id: UUID,
    service=Depends(get_chat_service),
):
    return await service.list_messages(thread_id=thread_id)


@router.delete(
    "/threads/{thread_id}",
    summary="Soft-delete a thread (sets metadata.deleted_at)",
)
async def delete_thread(
    thread_id: UUID,
    service=Depends(get_chat_service),
):
    await service.delete_thread(thread_id=thread_id)
    return {"ok": True, "thread_id": str(thread_id)}


# --------------------------------------------------------------------------- #
# Routes — messages
# --------------------------------------------------------------------------- #
@router.post(
    "/threads/{thread_id}/messages",
    summary="Synchronous chat (returns the full assistant reply)",
)
async def send_message(
    thread_id: UUID,
    body: SendMessageRequest,
    service=Depends(get_chat_service),
):
    msg = (body.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="message must be non-empty")
    return await service.chat(thread_id=thread_id, user_message=msg)


@router.post(
    "/threads/{thread_id}/stream",
    summary="SSE-stream the assistant reply (named ADR-02 §8 events)",
)
async def stream_message(
    thread_id: UUID,
    body: SendMessageRequest,
    service=Depends(get_chat_service),
):
    msg = (body.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="message must be non-empty")

    return StreamingResponse(
        service.stream(thread_id=thread_id, user_message=msg),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # See chat.py for the rationale — Next.js dev proxy will gzip
            # the stream and break SSE if we don't pin identity here.
            "X-Accel-Buffering": "no",
            "Content-Encoding": "identity",
        },
    )
