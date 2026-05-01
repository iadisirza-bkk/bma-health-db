"""Observability — structured logging + Prometheus metrics for the OOP layers.

Re-exports the small set of names the rest of the app uses so callers
write::

    from observability import configure_logging, prometheus_router, metrics

instead of reaching into the submodules directly.
"""
from __future__ import annotations

from . import metrics
from .logging import bind_request_context, configure_logging, get_logger
from .router import prometheus_router

__all__ = [
    "bind_request_context",
    "configure_logging",
    "get_logger",
    "metrics",
    "prometheus_router",
]
