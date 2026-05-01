"""Structured logging configuration via ``structlog``.

``configure_logging()`` is the single entry point — call it once at
process start (top of ``api/main.py``). After that, both:

    * the existing ``logging.getLogger(...).info(...)`` calls
    * any new ``structlog.get_logger().info(...)`` calls

emit through the same processor chain. In production (``LOG_FORMAT=json``)
that chain produces one JSON object per line, suitable for ingestion into
Loki / Datadog / CloudWatch. In dev (default) it produces colourised
console output.

Importable in environments without ``structlog`` — the module degrades to
stdlib ``logging.basicConfig`` and emits a single warning.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

logger = logging.getLogger("api.observability.logging")


try:
    import structlog  # type: ignore[import-not-found]

    STRUCTLOG_AVAILABLE = True
except Exception:  # pragma: no cover - tested only when lib is missing
    structlog = None  # type: ignore[assignment]
    STRUCTLOG_AVAILABLE = False


# ---------------------------------------------------------------------------
# Library-noise suppression — these libs are *very* chatty at INFO/DEBUG and
# their volume drowns out our own logs in production. Bumping them to WARNING
# is a no-brainer; if you need to debug an HTTP issue, set the env var
# ``BMA_DEBUG_LIBS=1`` to bypass this list.
# ---------------------------------------------------------------------------

_NOISY_LIBRARIES = (
    "httpx",
    "urllib3",
    "psycopg2.pool",
    "asyncio",
    "matplotlib",
    "PIL",
)


def _suppress_noisy_libs() -> None:
    if os.environ.get("BMA_DEBUG_LIBS"):
        return
    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def configure_logging(level: str = "INFO") -> None:
    """Configure stdlib + structlog with a shared processor chain.

    Idempotent — safe to call multiple times (we tear down handlers first).
    The level can be overridden by the ``LOG_LEVEL`` env var.
    """
    level_name = os.environ.get("LOG_LEVEL", level).upper()
    log_level = getattr(logging, level_name, logging.INFO)

    # Tear down any handlers attached by previous configure_logging() calls
    # or by the default basicConfig() — we want a single, well-known chain.
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(log_level)

    if not STRUCTLOG_AVAILABLE:
        logger.warning(
            "structlog not installed; falling back to stdlib logging. "
            "Install with: pip install structlog"
        )
        # Reasonable stdlib default so existing logger.info() calls still
        # produce useful output.
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s"
            )
        )
        root.addHandler(handler)
        _suppress_noisy_libs()
        return

    use_json = os.environ.get("LOG_FORMAT", "").lower() == "json"

    # Shared processors run before format-specific renderers. Order matters:
    # contextvars first (so request_id / user_id propagate), then level / ts
    # decoration, then exception formatting, finally the renderer.
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        timestamper,
    ]

    if use_json:
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    # Configure structlog's own logger factory — `structlog.get_logger()`
    # calls go through this chain.
    structlog.configure(
        processors=shared_processors
        + [
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Bridge stdlib `logging.getLogger(...)` calls into the same chain so
    # the existing `_audit_logger`, ChartService logger, etc. produce
    # structured output too.
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root.addHandler(handler)

    _suppress_noisy_libs()


def bind_request_context(
    request_id: str,
    user_id: Optional[str] = None,
) -> None:
    """Attach a per-request context to every log line emitted in this scope.

    Uses ``structlog.contextvars`` which is asyncio-task-aware: a copy of
    the dict is captured per coroutine, so concurrent requests don't see
    each other's request_id.

    No-op when structlog isn't installed.
    """
    if not STRUCTLOG_AVAILABLE:
        return
    structlog.contextvars.clear_contextvars()
    ctx: dict[str, Any] = {"request_id": request_id}
    if user_id is not None:
        ctx["user_id"] = user_id
    structlog.contextvars.bind_contextvars(**ctx)


def get_logger(name: Optional[str] = None) -> Any:
    """Return a structlog logger if available, else a stdlib logger.

    Lets call sites prefer the structured API without worrying about whether
    the lib is installed.
    """
    if STRUCTLOG_AVAILABLE:
        return structlog.get_logger(name)
    return logging.getLogger(name)


__all__ = [
    "STRUCTLOG_AVAILABLE",
    "bind_request_context",
    "configure_logging",
    "get_logger",
]
