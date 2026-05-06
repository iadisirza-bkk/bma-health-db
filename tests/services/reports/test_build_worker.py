"""Sprint S9 Task 2.5 — BuildWorker unit tests.

Exercises the bulk pre-build orchestrator with hand-built fakes so the
tests don't need tectonic, the live DB, or any descriptor on disk.

Coverage:
    * ``enqueue_popular_set("zone")`` returns 5 audience × 8 zones × 2 langs
      = 80 jobs (the brief says 5×8 = 40 if a single language; the actual
      descriptor declares both ``th`` and ``en`` so the cartesian doubles).
    * ``run_pending`` skips jobs whose content_hash already exists in the
      cache (mocked cache hit).
    * ``run_pending`` invokes the report service for cache-miss jobs.
    * ``force=True`` bypasses the cache hit short-circuit.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, List, Mapping, Optional

import pytest

# Make ``api/`` importable.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.reports.build_worker import BuildJob, BuildWorker  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeDescriptor:
    def __init__(self, languages, parameters=None):
        self.languages = languages
        self.parameters = parameters or []
        self.formats = ["pdf", "html"]


class _FakeRegistry:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, report_id):
        if report_id not in self._mapping:
            raise KeyError(report_id)
        return self._mapping[report_id]

    def list_ids(self):
        return list(self._mapping)


class _FakeCache:
    """Minimal ``PdfCache`` substitute. ``hits`` is the set of hashes
    that ``get`` should consider already-cached."""

    def __init__(self, hits=None):
        self.hits = set(hits or ())
        self.put_calls: list[tuple] = []

    def get(self, hash):
        return Path("/tmp/fake-cache") / f"{hash}.pdf" if hash in self.hits else None

    def put(self, hash, source_path, **meta):
        self.put_calls.append((hash, source_path, meta))
        self.hits.add(hash)


class _FakeService:
    """Stand-in for ReportService — counts calls + returns a tmp-file path."""

    def __init__(self, tmp_dir: Path, fail_for=None):
        self.tmp_dir = tmp_dir
        self.fail_for = set(fail_for or ())
        self.render_calls: list[tuple] = []

    async def render(self, report_id, fmt, lang, *, params=None, audience=None):
        self.render_calls.append((report_id, fmt, lang, params, audience))
        if (report_id, lang) in self.fail_for:
            raise RuntimeError("synthetic failure")
        out = self.tmp_dir / f"{report_id}-{lang}.pdf"
        out.write_bytes(b"%PDF-1.4 fake\n")
        return out


def _zone_descriptor():
    return _FakeDescriptor(languages=["th", "en"])


def _whitepaper_descriptor():
    return _FakeDescriptor(languages=["th", "en", "zh"])


# ---------------------------------------------------------------------------
# Tests — enqueue_popular_set
# ---------------------------------------------------------------------------


def _make_worker(tmp_path, registry, cache=None, force=False, fail_for=None):
    return BuildWorker(
        report_service=_FakeService(tmp_path, fail_for=fail_for),
        pdf_cache=cache or _FakeCache(),
        lock_path=tmp_path / "build.lock",
        registry=registry,
        config_dir=tmp_path / "configs-not-real",
        force=force,
    )


def test_enqueue_popular_set_zone(tmp_path):
    """Zone has 5 audiences × 8 zones × 2 langs = 80 jobs."""
    registry = _FakeRegistry({"zone": _zone_descriptor()})
    worker = _make_worker(tmp_path, registry)
    n = worker.enqueue_popular_set("zone")
    assert n == 80
    assert len(worker.queue) == 80
    # Sanity check: every job has a zone_code param and pdf format.
    for job in worker.queue:
        assert job.fmt == "pdf"
        assert job.lang in ("th", "en")
        assert job.params is not None
        assert job.params[0][0] == "zone_code"


def test_enqueue_popular_set_whitepaper_multiplies_by_lang(tmp_path):
    """Whitepaper has no parameters; popular set = 5 audiences × N langs."""
    registry = _FakeRegistry({"whitepaper": _whitepaper_descriptor()})
    worker = _make_worker(tmp_path, registry)
    n = worker.enqueue_popular_set("whitepaper")
    # 5 audience variants × 3 langs.
    assert n == 15
    assert len(worker.queue) == 15
    # No params for whitepaper.
    for job in worker.queue:
        assert job.params is None


def test_enqueue_unknown_report_returns_zero(tmp_path):
    registry = _FakeRegistry({})
    worker = _make_worker(tmp_path, registry)
    assert worker.enqueue_popular_set("nope") == 0
    assert len(worker.queue) == 0


# ---------------------------------------------------------------------------
# Tests — run_pending
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_pending_skips_cached_jobs(tmp_path, monkeypatch):
    """When cache.get() returns a path, the job should be skipped (not rendered)."""
    # whitepaper has no params + 1 lang → exactly 5 popular variants.
    registry = _FakeRegistry({"whitepaper": _FakeDescriptor(languages=["th"])})
    cache = _FakeCache()
    worker = _make_worker(tmp_path, registry, cache=cache)

    # Patch hash compute to a deterministic value so we know what to mark
    # as cached. content_hash() is keyed by job + data_version + mtime;
    # we replace _content_hash inside the build_worker module.
    from services.reports import build_worker as bw

    counter = {"n": 0}

    def _fake_hash(**kwargs):
        counter["n"] += 1
        # Mark the FIRST job's hash as already cached.
        return "cached" if counter["n"] == 1 else f"miss-{counter['n']}"

    monkeypatch.setattr(bw, "_content_hash", _fake_hash)
    monkeypatch.setattr(bw, "_data_version", lambda conn=None: "test-data-ver")
    monkeypatch.setattr(bw, "_descriptor_mtime", lambda p: 0.0)

    cache.hits.add("cached")
    worker.enqueue_popular_set("whitepaper")
    # 5 audiences × 1 lang = 5 jobs; one is "cached", others miss.
    assert len(worker.queue) == 5

    stats = await worker.run_pending()
    assert stats["skipped_cached"] == 1
    assert stats["built"] == 4
    assert stats["failed"] == 0
    assert worker._service.render_calls  # render was called for the misses


@pytest.mark.anyio
async def test_run_pending_force_bypasses_cache(tmp_path, monkeypatch):
    registry = _FakeRegistry({"whitepaper": _FakeDescriptor(languages=["th"])})
    cache = _FakeCache(hits={"always-cached"})
    worker = _make_worker(tmp_path, registry, cache=cache, force=True)

    from services.reports import build_worker as bw

    monkeypatch.setattr(bw, "_content_hash", lambda **kw: "always-cached")
    monkeypatch.setattr(bw, "_data_version", lambda conn=None: "v1")
    monkeypatch.setattr(bw, "_descriptor_mtime", lambda p: 0.0)

    worker.enqueue_popular_set("whitepaper")
    stats = await worker.run_pending()
    # force=True ignores the cache hit so EVERY job builds.
    assert stats["skipped_cached"] == 0
    assert stats["built"] == len(worker._service.render_calls)


@pytest.mark.anyio
async def test_run_pending_lock_prevents_concurrent_drain(tmp_path, monkeypatch):
    """Second worker against the same lock_path returns immediately."""
    registry = _FakeRegistry({"whitepaper": _FakeDescriptor(languages=["th"])})
    lock = tmp_path / "build.lock"

    from services.reports import build_worker as bw

    monkeypatch.setattr(bw, "_content_hash", lambda **kw: "h")
    monkeypatch.setattr(bw, "_data_version", lambda conn=None: "v")
    monkeypatch.setattr(bw, "_descriptor_mtime", lambda p: 0.0)

    # Manually grab the flock to simulate another worker draining.
    import fcntl
    fh = open(lock, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = BuildWorker(
            report_service=_FakeService(tmp_path),
            pdf_cache=_FakeCache(),
            lock_path=lock,
            registry=registry,
        )
        worker.enqueue_popular_set("whitepaper")
        stats = await worker.run_pending()
        assert stats["locked"] is True
        assert stats["built"] == 0
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


@pytest.mark.anyio
async def test_run_pending_failures_isolated(tmp_path, monkeypatch):
    """One failing render doesn't kill the rest."""
    registry = _FakeRegistry({"whitepaper": _FakeDescriptor(languages=["th", "en"])})
    cache = _FakeCache()
    worker = _make_worker(
        tmp_path, registry, cache=cache,
        fail_for=[("whitepaper", "th")],
    )

    from services.reports import build_worker as bw
    counter = {"n": 0}

    def _h(**kwargs):
        counter["n"] += 1
        return f"miss-{counter['n']}"

    monkeypatch.setattr(bw, "_content_hash", _h)
    monkeypatch.setattr(bw, "_data_version", lambda conn=None: "v")
    monkeypatch.setattr(bw, "_descriptor_mtime", lambda p: 0.0)

    worker.enqueue_popular_set("whitepaper")
    stats = await worker.run_pending()
    # 5 audiences × 2 langs = 10 jobs; 5 fail (th), 5 succeed (en).
    assert stats["failed"] == 5
    assert stats["built"] == 5
    assert stats["skipped_cached"] == 0
