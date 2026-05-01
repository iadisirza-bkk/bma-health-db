"""ReportDataCollector — single-source-of-data handle for blocks.

ADR-03 §7 — every block reads aggregate data through this class via
``ctx.data_collector``. No block touches the DB directly. The legacy
``services/report_data_collector.py`` continues to be the actual SQL
caller; this class is a thin caching adapter so multiple blocks in one
render share one DB pass.

Caching semantics
-----------------
* ``data()`` collects on first call, then memoises for ``cache_ttl_seconds``.
* ``invalidate()`` drops the cache, forcing the next call to refetch.
* ``data_hash()`` defers to the legacy ``compute_data_hash`` helper
  (cheap — keys + total_screened only) and is NOT cached, since the
  orchestrator uses it to decide if a previous render's hash sidecar is
  still fresh and the answer must reflect the live DB.

Pluggability
------------
``collector_fn`` lets tests inject a fake collector (no DB). When None,
the legacy ``collect_report_data()`` is used. The signature is
``Callable[[], ReportData]`` so the fake matches the production shape.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from services.report_data_collector import (
    ReportData,
    collect_report_data as _legacy_collect,
    compute_data_hash as _legacy_hash,
)

logger = logging.getLogger("api.services.reports.data_collector")

DEFAULT_TTL_SECONDS = 300


class ReportDataCollector:
    """Caching wrapper around ``services.report_data_collector``.

    One instance is shared across every block in a single render (passed
    via ``RenderContext.data_collector``); the orchestrator may also reuse
    one process-wide instance for the cache to span requests.

    Parameters
    ----------
    cache_ttl_seconds:
        How long a collected ``ReportData`` is considered fresh. Default
        300s (5 min) — short enough that newly ingested rows surface in
        roughly one cache cycle, long enough that a multi-block render
        sees a single DB pass.
    collector_fn:
        Optional injected callable returning a ``ReportData`` (or any
        ``dict``-coercible object). Tests use this to bypass the DB.
    hash_fn:
        Optional injected callable returning the data-hash string. Tests
        use this so cache-hit assertions don't depend on the live DB.
    """

    def __init__(
        self,
        *,
        cache_ttl_seconds: int = DEFAULT_TTL_SECONDS,
        collector_fn: Optional[Callable[[], Any]] = None,
        hash_fn: Optional[Callable[[], str]] = None,
    ) -> None:
        self._ttl = int(cache_ttl_seconds)
        self._collector_fn = collector_fn or _legacy_collect
        self._hash_fn = hash_fn or _legacy_hash
        self._cache: Optional[Any] = None
        self._cache_at: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def data(self) -> Any:
        """Return the cached ``ReportData`` (or fresh-collect if stale).

        Returns the same shape the legacy ``collect_report_data()`` does
        — typed as ``Any`` so block authors aren't forced to import the
        legacy ``ReportData`` dataclass when they only need a few fields.
        Production code can rely on ``isinstance(result, ReportData)``;
        tests may inject a plain ``dict`` or a fake.
        """
        now = time.monotonic()
        if self._cache is not None and (now - self._cache_at) < self._ttl:
            return self._cache
        logger.debug(
            "ReportDataCollector cache miss (age=%.1fs ttl=%ds)",
            now - self._cache_at if self._cache is not None else float("inf"),
            self._ttl,
        )
        self._cache = self._collector_fn()
        self._cache_at = now
        return self._cache

    def data_hash(self) -> str:
        """Return the current data-hash string. NOT cached — see docstring."""
        try:
            return self._hash_fn()
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("data_hash failed: %s", exc)
            return "no-data"

    def invalidate(self) -> None:
        """Drop the cached payload. Next ``data()`` call refetches."""
        self._cache = None
        self._cache_at = 0.0
        logger.debug("ReportDataCollector cache invalidated")

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def is_cached(self) -> bool:
        """True iff a non-stale payload is currently held."""
        if self._cache is None:
            return False
        return (time.monotonic() - self._cache_at) < self._ttl

    @property
    def ttl_seconds(self) -> int:
        return self._ttl


__all__ = ["ReportDataCollector", "ReportData", "DEFAULT_TTL_SECONDS"]
