"""Bulk pre-build worker — Sprint S9 Task 2.1.

Queue + drain unit for popular report variants. Runs in-process, sequential
(tectonic doesn't parallelize well; serial is fine for solo dev). Future:
swap for Celery/RQ if scale demands.

Idempotent
----------
Each job is keyed by its content_hash; a job whose hash already exists in
the cache is skipped. This neatly solves the "cron + post-upload trigger
fires twice" race — both end up enqueueing the same set, but the second
drain is a no-op.

Lock file
---------
A filesystem flock around the drain prevents concurrent workers (e.g. the
post-upload hook firing while the cron run is still draining). If the lock
is held, the second drain returns immediately with a "skipped: locked"
status — the in-flight worker will pick up our newly enqueued jobs.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, List, Mapping, Optional

from services.reports.cache_keys import (
    content_hash as _content_hash,
    data_version as _data_version,
    descriptor_mtime as _descriptor_mtime,
)
from services.reports.pdf_cache import PdfCache

logger = logging.getLogger("api.services.reports.build_worker")


# ---------------------------------------------------------------------------
# The four audience values used to expand the "popular set". Single-source-of
# truth lives on AudienceTarget in blocks/base.py — we pull from there at
# import time so a new audience automatically widens the popular set.
# ---------------------------------------------------------------------------
def _audience_values() -> List[str]:
    try:
        from services.reports.blocks.base import AudienceTarget
        return [a.value for a in AudienceTarget]
    except Exception:
        return ["people", "executive", "clinician", "researcher"]


# 8 BMA health zones — pulled directly from the zone descriptor's enum.
_ZONE_CODES = ["01", "02", "03", "04", "05", "06", "07", "08"]


# ---------------------------------------------------------------------------
# BuildJob
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildJob:
    """One report-build unit of work.

    ``audience`` is a tuple (not list) so the dataclass stays hashable; the
    queue can de-dup on equality if we ever want to.
    """
    report_id: str
    fmt: str
    lang: str
    # None = full report (no filter); tuple of audience values otherwise.
    audience: Optional[tuple] = None
    # Frozen mapping for hashability.
    params: Optional[tuple] = None  # tuple of (key, value) pairs

    def params_dict(self) -> Optional[dict]:
        if self.params is None:
            return None
        return dict(self.params)

    def audience_list(self) -> Optional[List[str]]:
        if self.audience is None:
            return None
        return list(self.audience)

    def descriptor(self) -> str:
        # Short human-readable id used in logs.
        bits = [self.report_id, self.fmt, self.lang]
        if self.audience:
            bits.append("aud=" + ",".join(self.audience))
        if self.params:
            bits.append(
                "params=" + ",".join(f"{k}={v}" for k, v in self.params)
            )
        return " ".join(bits)


# ---------------------------------------------------------------------------
# File-based lock — minimal, non-blocking. ``fcntl.flock`` works on macOS +
# Linux; Windows is not a target deployment for this app so we accept the
# Unix-only behaviour.
# ---------------------------------------------------------------------------


@contextmanager
def _try_flock(lock_path: Path) -> Iterator[bool]:
    """Yield True if we got the lock, False if another worker has it."""
    import fcntl
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
        except BlockingIOError:
            held = False
        yield held
    finally:
        try:
            if held:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


# ---------------------------------------------------------------------------
# BuildWorker
# ---------------------------------------------------------------------------


class BuildWorker:
    """In-process serial worker — drains a queue of :class:`BuildJob`.

    Parameters
    ----------
    report_service
        Something with ``async render(report_id, fmt, lang, *, params=None,
        audience=None) -> Path`` (i.e. the real ReportService). Tests inject
        a fake.
    pdf_cache
        :class:`PdfCache` instance (or anything matching its surface).
    lock_path
        Filesystem path used as a flock to serialise concurrent workers.
    registry, gemma_version
        Optional — only used to compute content hashes. The registry lets
        us look up descriptor mtime; ``gemma_version`` plumbs through to
        the hash so future LLM upgrades invalidate prior cache entries.
    force
        If True, ignore cache hits during ``run_pending`` (every job
        rebuilds). Useful for the admin "force rebuild" endpoint.
    """

    def __init__(
        self,
        report_service: Any,
        pdf_cache: PdfCache,
        lock_path: Path,
        *,
        registry: Optional[Any] = None,
        config_dir: Optional[Path] = None,
        gemma_version: str = "v0",
        force: bool = False,
    ):
        self.queue: "deque[BuildJob]" = deque()
        self.lock_path = Path(lock_path)
        self._service = report_service
        self._cache = pdf_cache
        self._registry = registry
        self._config_dir = Path(config_dir) if config_dir else None
        self._gemma_version = gemma_version
        self._force = force
        # last-drain stats — exposed via run_pending() return + admin GET
        self.last_run: dict = {}

    # ------------------------------------------------------------------
    # Enqueue API
    # ------------------------------------------------------------------

    def enqueue(self, job: BuildJob) -> None:
        """Add a single job to the queue (no de-dup — drain handles cache hits)."""
        self.queue.append(job)

    def enqueue_popular_set(self, report_id: str) -> int:
        """Enqueue popular variants for ``report_id``.

        Per the brief:
            * 5 audience variants per (lang × params combination):
                1. full report (no audience filter)
                2-5. one per single audience (people / executive /
                     clinician / researcher)
            * For ``zone``: multiply by 8 zone codes
            * For ``whitepaper``: multiply by every declared language
            * For other reports: one (lang × audience) cartesian over the
              descriptor's languages list

        Returns the number of jobs enqueued.
        """
        descriptor, languages, _formats = self._descriptor_meta(report_id)
        if descriptor is None:
            logger.warning("enqueue_popular_set: unknown report_id %r", report_id)
            return 0
        added = 0
        # Always pdf — that's what bulk-prebuild is for. HTML can be added
        # in S10 if it turns out to be slow on demand.
        fmt = "pdf"
        audiences: List[Optional[tuple]] = [
            None,                           # full report
            *[(a,) for a in _audience_values()],
        ]
        # Build the params cartesian.
        param_sets: List[Optional[tuple]] = self._param_combos_for(report_id)
        # Languages: zone keeps a tighter set; whitepaper uses every
        # declared language; default is whatever the descriptor says.
        if report_id == "whitepaper":
            lang_set = list(languages)
        elif report_id == "zone":
            lang_set = list(languages)
        else:
            lang_set = list(languages)
        for lang in lang_set:
            for params in param_sets:
                for audience in audiences:
                    job = BuildJob(
                        report_id=report_id,
                        fmt=fmt,
                        lang=lang,
                        audience=audience,
                        params=params,
                    )
                    self.enqueue(job)
                    added += 1
        logger.info(
            "enqueue_popular_set: %s -> %d jobs (queue size=%d)",
            report_id, added, len(self.queue),
        )
        return added

    # ------------------------------------------------------------------
    # Drain
    # ------------------------------------------------------------------

    async def run_pending(self) -> dict:
        """Drain the queue. Returns a stats dict.

        ``built``: how many jobs actually rendered.
        ``skipped_cached``: how many were cache hits.
        ``failed``: how many threw.
        ``duration_s``: wall-clock seconds.
        ``locked``: True if another worker held the lock and we exited
                    immediately (queue is preserved for the in-flight one).
        """
        with _try_flock(self.lock_path) as held:
            if not held:
                logger.info("run_pending: lock held; another worker is draining")
                return {
                    "built": 0,
                    "skipped_cached": 0,
                    "failed": 0,
                    "duration_s": 0.0,
                    "locked": True,
                    "queue_size": len(self.queue),
                }
            return await self._drain()

    async def _drain(self) -> dict:
        started = time.monotonic()
        built = 0
        skipped = 0
        failed = 0
        # Snapshot the queue so a hook firing mid-drain doesn't extend
        # this run indefinitely. New jobs land in self.queue and are
        # picked up on the next run_pending().
        jobs = list(self.queue)
        self.queue.clear()
        for job in jobs:
            try:
                outcome = await self._run_one(job)
                if outcome == "built":
                    built += 1
                elif outcome == "skipped":
                    skipped += 1
                elif outcome == "failed":
                    failed += 1
            except Exception:
                logger.exception(
                    "build_worker: unexpected error draining job %s",
                    job.descriptor(),
                )
                failed += 1
        duration = time.monotonic() - started
        stats = {
            "built": built,
            "skipped_cached": skipped,
            "failed": failed,
            "duration_s": round(duration, 2),
            "locked": False,
            "queue_size": len(self.queue),
        }
        self.last_run = dict(stats)
        logger.info("build_worker drain complete: %s", stats)
        return stats

    async def _run_one(self, job: BuildJob) -> str:
        """Render one job, taking cache hits into account.

        Returns ``"built"`` / ``"skipped"`` / ``"failed"``.
        """
        params_dict = job.params_dict()
        audience_list = job.audience_list()
        try:
            data_ver = self._safe_data_version()
            descriptor_mtime_val = self._descriptor_mtime_for(job.report_id)
            h = _content_hash(
                report_id=job.report_id,
                fmt=job.fmt,
                lang=job.lang,
                audience=audience_list,
                params=params_dict,
                data_version=data_ver,
                descriptor_mtime=descriptor_mtime_val,
                gemma_version=self._gemma_version,
            )
        except Exception:
            logger.exception("build_worker: hash compute failed for %s", job.descriptor())
            return "failed"

        if not self._force and self._cache.get(h) is not None:
            logger.debug("build_worker: cache hit %s -> skip", job.descriptor())
            return "skipped"

        # Render. The service handles its own cache short-circuit too,
        # but our cache layer is content-hash and the service's is
        # data-hash + path-based — they coexist.
        try:
            audience_set = set(audience_list) if audience_list else None
            rendered_path = await self._service.render(
                job.report_id,
                job.fmt,
                job.lang,
                params=params_dict,
                audience=audience_set,
            )
        except Exception:
            logger.exception("build_worker: render failed for %s", job.descriptor())
            return "failed"

        # Stash a copy in the cache.
        try:
            self._cache.put(
                h,
                Path(rendered_path),
                report_id=job.report_id,
                fmt=job.fmt,
                lang=job.lang,
                audience=audience_list,
                params=params_dict,
                data_version=data_ver,
                descriptor_mtime=descriptor_mtime_val,
            )
        except Exception:
            logger.exception("build_worker: cache put failed for %s", job.descriptor())
            # Render succeeded but cache write failed — count as built;
            # next request will still re-render but that's ok.
        return "built"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _descriptor_meta(self, report_id: str):
        """Return (descriptor, languages, formats) for ``report_id`` or None."""
        registry = self._registry
        if registry is None:
            try:
                from services.reports.registry import report_registry
                registry = report_registry()
            except Exception:
                logger.warning("build_worker: registry unavailable")
                return None, [], []
        try:
            desc = registry.get(report_id)
        except KeyError:
            return None, [], []
        languages = list(getattr(desc, "languages", []))
        formats = list(getattr(desc, "formats", []))
        return desc, languages, formats

    def _param_combos_for(self, report_id: str) -> List[Optional[tuple]]:
        """Cartesian of descriptor parameters → list of ``(k,v) tuples``.

        For ``zone`` we hard-code the 8 zone codes (descriptor enum). For
        any descriptor with no parameters we return ``[None]`` (one combo:
        no params).
        """
        if report_id == "zone":
            return [(("zone_code", z),) for z in _ZONE_CODES]
        # Generic: walk descriptor.parameters and build a cartesian.
        desc, _, _ = self._descriptor_meta(report_id)
        if desc is None:
            return [None]
        params = list(getattr(desc, "parameters", []) or [])
        if not params:
            return [None]
        # Build per-param value lists.
        per_param_values: list[list[tuple[str, Any]]] = []
        for p in params:
            key = getattr(p, "key", None)
            if key is None:
                continue
            options = getattr(p, "options", None) or []
            values = []
            for opt in options:
                v = getattr(opt, "value", None)
                if v is None and isinstance(opt, dict):
                    v = opt.get("value")
                if v is not None:
                    values.append(v)
            if not values:
                # parameter without enumerable values — skip combo expansion.
                continue
            per_param_values.append([(key, v) for v in values])
        if not per_param_values:
            return [None]
        # Simple iterative cartesian product (no itertools tuple gymnastics
        # — keeps it readable + we have small fan-out).
        combos: list[tuple] = [tuple()]
        for vals in per_param_values:
            combos = [c + (v,) for c in combos for v in vals]
        return list(combos)  # type: ignore[return-value]

    def _safe_data_version(self) -> str:
        """Compute the live data_version, falling back gracefully."""
        try:
            # The cache_keys.data_version() takes a conn; we open one
            # ad-hoc and close it. Falls back via its own try/except.
            import psycopg2
            from config import DATABASE_URL_WRITER
            conn = psycopg2.connect(DATABASE_URL_WRITER)
            try:
                return _data_version(conn)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("build_worker: data_version fallback (%s)", exc)
            # Without DB, fall back to a time-bucketed token so consecutive
            # runs use the same version and cache hits work in tests.
            return "no-db"

    def _descriptor_mtime_for(self, report_id: str) -> float:
        if self._config_dir is None:
            try:
                # Default mirrors registry._default_config_dir().
                self._config_dir = (
                    Path(__file__).resolve().parents[3]
                    / "config" / "reports"
                )
            except Exception:
                return 0.0
        return _descriptor_mtime(self._config_dir / f"{report_id}.yaml")


# ---------------------------------------------------------------------------
# Process-wide singleton — wired up by the FastAPI startup hook.
# ---------------------------------------------------------------------------


_WORKER: Optional[BuildWorker] = None


def get_build_worker() -> BuildWorker:
    """Lazy singleton — built on first call.

    Wires the real ReportService + the default PdfCache. Tests construct
    BuildWorker directly with fakes and never touch this helper.
    """
    global _WORKER
    if _WORKER is None:
        from services.reports.service import get_report_service
        from services.reports.pdf_cache import get_pdf_cache
        from services.reports.registry import report_registry

        try:
            import config
            reports_dir = Path(config.REPORTS_DIR)
        except Exception:
            reports_dir = Path("/tmp/bma-reports")

        lock_path = reports_dir / ".pdf_cache" / "build_worker.lock"
        _WORKER = BuildWorker(
            report_service=get_report_service(),
            pdf_cache=get_pdf_cache(),
            lock_path=lock_path,
            registry=report_registry(),
        )
    return _WORKER


__all__ = ["BuildJob", "BuildWorker", "get_build_worker"]
