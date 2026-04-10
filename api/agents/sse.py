"""SSE (Server-Sent Events) formatting helpers."""
from __future__ import annotations

import json


def format_sse(event: dict) -> str:
    """Format a dict as an SSE data line."""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def sse_heartbeat() -> str:
    """SSE comment line as keepalive (prevents proxy timeouts)."""
    return ": heartbeat\n\n"


# SSE event type constants
AGENT_START = "agent_start"
AGENT_DONE = "agent_done"
CONTENT = "content"
VISUALIZATION = "visualization"
ARTIFACT = "artifact"
CLARIFICATION = "clarification"
ERROR = "error"
DONE = "done"
