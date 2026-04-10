"""
Chat API router — LLM-powered health data assistant.

Supports both synchronous and SSE streaming responses.
Prefix: /api/health
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/health", tags=["chat"])


def _get_orchestrator():
    """Lazy-load orchestrator to avoid import errors when LMStudio is unavailable."""
    try:
        from agents import create_orchestrator
        return create_orchestrator()
    except Exception as e:
        logger.warning("Failed to create orchestrator: %s", e)
        return None


# --------------------------------------------------------------------------- #
# Sync chat
# --------------------------------------------------------------------------- #


@router.get("/chat")
@router.post("/chat")
async def chat_endpoint(message: str = Query("", description="User message")):
    """Synchronous chat — returns full response as JSON."""
    orchestrator = _get_orchestrator()
    if orchestrator is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "LLM service unavailable. Check LMSTUDIO_URL config."},
        )
    try:
        result = await orchestrator.process(message)
        return result
    except Exception as e:
        logger.error("Chat error: %s", e)
        return JSONResponse(
            status_code=500,
            content={"detail": f"Chat processing failed: {e}"},
        )


# --------------------------------------------------------------------------- #
# SSE streaming chat
# --------------------------------------------------------------------------- #


@router.get("/chat/stream")
@router.post("/chat/stream")
async def chat_stream_endpoint(
    message: str = Query("", description="User message"),
    history: str = Query("[]", description="Conversation history as JSON array"),
):
    """SSE streaming chat — returns Server-Sent Events with incremental content."""
    orchestrator = _get_orchestrator()
    if orchestrator is None:
        async def error_stream():
            yield f"data: {json.dumps({'type': 'error', 'message': 'LLM service unavailable'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(
            error_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # Parse conversation history
    try:
        conv_history = json.loads(history)
        if not isinstance(conv_history, list):
            conv_history = []
    except Exception:
        conv_history = []

    # Trim history to prevent context overflow
    if conv_history:
        conv_history = conv_history[-2:]
        for msg in conv_history:
            if isinstance(msg, dict) and "content" in msg:
                content = msg["content"]
                if "clarification" in content.lower() or len(content) > 300:
                    msg["content"] = content[:200] + "..."

    return StreamingResponse(
        orchestrator.process_stream(message, conv_history=conv_history),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
