"""Prometheus metrics for the S2/S3/S4 OOP layers.

Defines all counters / histograms used by the chart, chat, and report
services. Importable in environments without `prometheus_client` — the
module degrades to no-op stubs and logs a single startup warning instead
of crashing.

Usage
-----
    from observability.metrics import (
        track_duration,
        chart_render_total,
        chart_render_duration,
    )

    with track_duration(chart_render_duration, {"spec_id": spec_id}):
        try:
            ... do work ...
            chart_render_total.labels(spec_id=spec_id, status="ok").inc()
        except Exception:
            chart_render_total.labels(spec_id=spec_id, status="error").inc()
            raise

Cardinality
-----------
Label values that originate from user input MUST be bounded server-side
(spec_id, report_id, fmt, lang are all enum-like strings drawn from a
small registry; tool_name comes from the orchestrator's whitelist; role
is one of {system, user, assistant, tool}). DO NOT add labels for
free-form strings (district, user_id, query text) — Prometheus stores
one timeseries per unique label combination and unbounded label values
will OOM the scraper.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Optional

logger = logging.getLogger("api.observability.metrics")


# ---------------------------------------------------------------------------
# Try to import prometheus_client; if missing, define no-op stubs that
# preserve the public API (Counter/Histogram/labels/inc/observe) so callers
# don't need conditional imports everywhere.
# ---------------------------------------------------------------------------

try:
    from prometheus_client import (  # type: ignore[import-not-found]
        CONTENT_TYPE_LATEST,
        REGISTRY,
        Counter as _Counter,
        Histogram as _Histogram,
        generate_latest as _generate_latest,
    )

    PROMETHEUS_AVAILABLE = True
except Exception:  # pragma: no cover - tested only when lib is missing
    PROMETHEUS_AVAILABLE = False
    logger.warning(
        "prometheus_client not installed; metrics will be no-ops. "
        "Install with: pip install prometheus_client"
    )

    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class _NoopChild:
        """Stub returned by Counter.labels()/Histogram.labels()."""

        def inc(self, _amount: float = 1.0) -> None:
            return None

        def observe(self, _value: float) -> None:
            return None

        def time(self):  # pragma: no cover
            @contextmanager
            def _ctx():
                yield self
            return _ctx()

    class _NoopMetric:
        """Stub used when prometheus_client is unavailable."""

        def __init__(
            self,
            name: str,
            documentation: str,
            labelnames: tuple[str, ...] = (),
            **_kwargs: Any,
        ) -> None:
            self._name = name
            self._documentation = documentation
            self._labelnames = tuple(labelnames)

        def labels(self, *_args: Any, **_kwargs: Any) -> _NoopChild:
            return _NoopChild()

        def inc(self, _amount: float = 1.0) -> None:
            return None

        def observe(self, _value: float) -> None:
            return None

    _Counter = _NoopMetric  # type: ignore[assignment,misc]
    _Histogram = _NoopMetric  # type: ignore[assignment,misc]

    class _NoopRegistry:
        def collect(self):
            return []

    REGISTRY = _NoopRegistry()  # type: ignore[assignment]

    def _generate_latest(_registry: Any = None) -> bytes:  # type: ignore[misc]
        return b"# prometheus_client not installed\n"


# ---------------------------------------------------------------------------
# Metric definitions
#
# Naming follows the prometheus convention (`bma_<subsystem>_<unit>_<type>`).
# Histograms use the default bucket set unless overridden — chart renders
# and report renders span O(10ms)–O(60s), so the default buckets (5ms-10s)
# are fine for charts but slightly cramped for the long tail of reports.
# We let prometheus pick defaults rather than guess; tune in v2 once we
# have real production data.
# ---------------------------------------------------------------------------

chart_render_total = _Counter(
    "bma_chart_render_total",
    "Chart renders, labelled by spec_id and outcome status.",
    ["spec_id", "status"],
)
chart_render_duration = _Histogram(
    "bma_chart_render_duration_seconds",
    "Chart render latency, labelled by spec_id.",
    ["spec_id"],
)
chart_kanon_dropped = _Counter(
    "bma_chart_kanon_dropped_total",
    "Rows dropped (or masked) by k-anonymity, labelled by spec_id.",
    ["spec_id"],
)

chat_message_total = _Counter(
    "bma_chat_message_total",
    "Chat messages persisted, labelled by role.",
    ["role"],
)
chat_tool_call_total = _Counter(
    "bma_chat_tool_call_total",
    "Chat tool invocations, labelled by tool_name and status.",
    ["tool_name", "status"],
)
chat_stream_duration = _Histogram(
    "bma_chat_stream_duration_seconds",
    "Chat stream lifecycle latency from first byte to done event.",
)

report_render_total = _Counter(
    "bma_report_render_total",
    "Report renders, labelled by report_id, fmt, lang, and outcome.",
    ["report_id", "fmt", "lang", "status"],
)
report_render_duration = _Histogram(
    "bma_report_render_duration_seconds",
    "Report render latency.",
    ["report_id", "fmt"],
)
report_cache_hit = _Counter(
    "bma_report_cache_hit_total",
    "Report cache short-circuit hits.",
    ["report_id", "fmt", "lang"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@contextmanager
def track_duration(
    histogram: Any,
    labels: Optional[Mapping[str, str]] = None,
) -> Iterator[None]:
    """Time a block and record the duration on ``histogram``.

    Falls back to a manual ``time.monotonic()`` measurement instead of
    using ``Histogram.time()`` directly so we can attach labels without
    forcing every call site to know whether the histogram is labelled.
    Always records — even when an exception escapes the ``with`` block —
    so latency for failures is observable too.
    """
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - start
        try:
            if labels:
                histogram.labels(**labels).observe(elapsed)
            else:
                histogram.observe(elapsed)
        except Exception:  # pragma: no cover - defensive
            # Never let metric recording break the caller.
            logger.debug("track_duration: observe() failed", exc_info=True)


def count_status(
    counter: Any,
    labels: Mapping[str, str],
    status: str,
    status_label: str = "status",
) -> None:
    """Increment ``counter`` with ``labels`` augmented by a status value.

    Convenience wrapper: callers don't have to spread/copy the label dict
    themselves at every increment site.
    """
    try:
        merged = {**labels, status_label: status}
        counter.labels(**merged).inc()
    except Exception:  # pragma: no cover - defensive
        logger.debug("count_status: inc() failed", exc_info=True)


def generate_latest() -> bytes:
    """Return the prometheus exposition payload (or a stub line if absent)."""
    return _generate_latest(REGISTRY)


__all__ = [
    "CONTENT_TYPE_LATEST",
    "PROMETHEUS_AVAILABLE",
    "REGISTRY",
    "chart_kanon_dropped",
    "chart_render_duration",
    "chart_render_total",
    "chat_message_total",
    "chat_stream_duration",
    "chat_tool_call_total",
    "count_status",
    "generate_latest",
    "report_cache_hit",
    "report_render_duration",
    "report_render_total",
    "track_duration",
]
