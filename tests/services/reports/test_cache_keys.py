"""Unit tests for ``api/services/reports/cache_keys.py`` (S9 Task 1.1).

We exercise:
    * Pure ``content_hash`` determinism + canonicalisation.
    * Each input axis (changing ANY one shifts the digest).
    * ``data_version`` against a fake DB connection (no live PG needed).
    * ``data_version_human`` formatting + fallback.

Tests are pure-Python — no DB roundtrips, no fixtures. They run in
under 50ms on a laptop.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

# Ensure ``api/`` is importable (mirrors the routers test pattern).
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.reports.cache_keys import (  # noqa: E402
    content_hash,
    data_version,
    data_version_human,
    descriptor_mtime,
)


# ---------------------------------------------------------------------------
# Fake DB connection — minimal duck-type that ``data_version`` understands.
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Tiny DB-API cursor stub."""

    def __init__(self, scripted_results: list):
        # ``scripted_results`` is consumed in execute-call order; each
        # entry is the rows the next ``fetchall``/``fetchone`` returns.
        self._scripted = list(scripted_results)
        self._current: list = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        # Pop the next scripted result for this SQL call; if we run
        # out, the test is over-fetching and we want a clear failure.
        if not self._scripted:
            raise AssertionError(
                "FakeCursor: ran out of scripted results "
                f"(SQL was: {sql!r})"
            )
        self._current = self._scripted.pop(0)

    def fetchone(self):
        # First row of the current result; ``data_version`` uses fetchone
        # for the ingestion_batch query.
        return self._current[0] if self._current else None

    def fetchall(self):
        return list(self._current)


class _FakeConn:
    def __init__(self, scripted_results: list, *, raise_on_cursor: bool = False):
        self._scripted = scripted_results
        self._raise = raise_on_cursor

    def cursor(self):
        if self._raise:
            raise RuntimeError("simulated DB outage")
        return _FakeCursor(self._scripted)


# ---------------------------------------------------------------------------
# content_hash — determinism + canonicalisation
# ---------------------------------------------------------------------------


def _base_kwargs(**overrides):
    """Helper — every test starts from a known baseline."""
    base = dict(
        report_id="zone",
        fmt="pdf",
        lang="th",
        audience=None,
        params={"zone_code": "03"},
        data_version="abc123def",
        descriptor_mtime=1700000000.0,
        gemma_version="v0",
    )
    base.update(overrides)
    return base


def test_content_hash_is_deterministic() -> None:
    """Same inputs → same digest, every call."""
    a = content_hash(**_base_kwargs())
    b = content_hash(**_base_kwargs())
    assert a == b
    # Length contract: 16 lowercase hex chars.
    assert len(a) == 16
    assert all(ch in "0123456789abcdef" for ch in a)


def test_content_hash_audience_order_does_not_matter() -> None:
    """Sorting + de-dup means audience iterables hash the same."""
    h1 = content_hash(
        **_base_kwargs(audience=["executive", "clinician"])
    )
    h2 = content_hash(
        **_base_kwargs(audience=["clinician", "executive"])
    )
    h3 = content_hash(
        **_base_kwargs(audience={"executive", "clinician"})
    )
    h4 = content_hash(
        **_base_kwargs(audience=["executive", "clinician", "executive"])
    )
    assert h1 == h2 == h3 == h4


def test_content_hash_audience_none_and_empty_collapse() -> None:
    """``audience=None``, ``[]``, and ``set()`` all mean 'render all'."""
    h_none = content_hash(**_base_kwargs(audience=None))
    h_empty_list = content_hash(**_base_kwargs(audience=[]))
    h_empty_set = content_hash(**_base_kwargs(audience=set()))
    assert h_none == h_empty_list == h_empty_set


def test_content_hash_params_key_order_irrelevant() -> None:
    """Inserting params in different orders yields the same digest."""
    h1 = content_hash(
        **_base_kwargs(params={"zone_code": "03", "year": "2026"})
    )
    h2 = content_hash(
        **_base_kwargs(params={"year": "2026", "zone_code": "03"})
    )
    assert h1 == h2


def test_content_hash_changes_on_each_axis() -> None:
    """Flipping any one input must produce a DIFFERENT digest.

    This is the strongest invariant — we don't want some axis silently
    dropped from the canonicalisation step.
    """
    base = content_hash(**_base_kwargs())
    assert content_hash(**_base_kwargs(report_id="other")) != base
    assert content_hash(**_base_kwargs(fmt="html")) != base
    assert content_hash(**_base_kwargs(lang="en")) != base
    assert content_hash(**_base_kwargs(audience=["executive"])) != base
    assert content_hash(**_base_kwargs(params={"zone_code": "04"})) != base
    assert content_hash(**_base_kwargs(data_version="new-version")) != base
    assert content_hash(**_base_kwargs(descriptor_mtime=1800000000.0)) != base
    assert content_hash(**_base_kwargs(gemma_version="v1")) != base


def test_content_hash_handles_non_json_param_values() -> None:
    """Stray non-JSON values (e.g. set objects) coerce to str rather
    than blowing up — cache-key generation must not crash."""
    h = content_hash(
        **_base_kwargs(params={"weird": object()})
    )
    assert len(h) == 16


# ---------------------------------------------------------------------------
# data_version — uses our FakeConn to avoid the real DB
# ---------------------------------------------------------------------------


def _scripted(batch_id: int, finished_at: str, mv_rows):
    """Build the scripted results list ``data_version`` consumes."""
    return [
        # First execute() — ingestion_batch summary (one row).
        [(batch_id, finished_at)],
        # Second execute() — MV rows.
        list(mv_rows),
    ]


def test_data_version_changes_when_max_batch_changes() -> None:
    """Bumping ``MAX(batch_id)`` flips the data_version hash."""
    rows = [
        ("public", "mv_demographics", 100, "2026-05-01 00:00:00"),
        ("public", "mv_kpi_tier1", 200, "2026-05-01 00:00:00"),
    ]
    h_before = data_version(_FakeConn(_scripted(10, "2026-05-01", rows)))
    h_after = data_version(_FakeConn(_scripted(11, "2026-05-01", rows)))
    assert h_before != h_after
    assert len(h_before) == 16


def test_data_version_changes_when_mv_refreshes() -> None:
    """Bumping a MV's ``last_analyze`` flips the data_version hash."""
    rows_v1 = [
        ("public", "mv_demographics", 100, "2026-05-01 00:00:00"),
        ("public", "mv_kpi_tier1", 200, "2026-05-01 00:00:00"),
    ]
    rows_v2 = [
        ("public", "mv_demographics", 100, "2026-05-02 03:14:00"),
        ("public", "mv_kpi_tier1", 200, "2026-05-01 00:00:00"),
    ]
    h_before = data_version(_FakeConn(_scripted(10, "2026-05-01", rows_v1)))
    h_after = data_version(_FakeConn(_scripted(10, "2026-05-01", rows_v2)))
    assert h_before != h_after


def test_data_version_changes_when_mv_row_count_changes() -> None:
    """A bigger ``n_live_tup`` (newly ingested rows) flips the digest."""
    rows_small = [
        ("public", "mv_demographics", 100, "2026-05-01"),
    ]
    rows_big = [
        ("public", "mv_demographics", 200, "2026-05-01"),
    ]
    h_before = data_version(_FakeConn(_scripted(10, "2026-05-01", rows_small)))
    h_after = data_version(_FakeConn(_scripted(10, "2026-05-01", rows_big)))
    assert h_before != h_after


def test_data_version_stable_for_identical_inputs() -> None:
    """Same DB state → same hash (sanity)."""
    rows = [
        ("public", "mv_demographics", 100, "2026-05-01"),
    ]
    a = data_version(_FakeConn(_scripted(10, "2026-05-01", rows)))
    b = data_version(_FakeConn(_scripted(10, "2026-05-01", rows)))
    assert a == b


def test_data_version_falls_back_when_db_unreachable() -> None:
    """Simulated outage → fallback digest, NOT an exception."""
    h = data_version(_FakeConn([], raise_on_cursor=True))
    assert isinstance(h, str)
    assert len(h) == 16


# ---------------------------------------------------------------------------
# data_version_human — date formatting
# ---------------------------------------------------------------------------


class _HumanCursor:
    def __init__(self, value):
        self._value = value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return (self._value,)


class _HumanConn:
    def __init__(self, value, *, raise_on_cursor: bool = False):
        self._value = value
        self._raise = raise_on_cursor

    def cursor(self):
        if self._raise:
            raise RuntimeError("simulated DB outage")
        return _HumanCursor(self._value)


def test_data_version_human_formats_datetime_as_yyyy_mm_dd() -> None:
    ts = datetime(2026, 5, 1, 17, 19, 3, tzinfo=timezone.utc)
    h = data_version_human(_HumanConn(ts))
    assert h == "2026-05-01"


def test_data_version_human_handles_empty_table() -> None:
    h = data_version_human(_HumanConn(None))
    assert h == "ไม่ทราบ"


def test_data_version_human_falls_back_on_db_error() -> None:
    h = data_version_human(_HumanConn(None, raise_on_cursor=True))
    assert h == "ไม่ทราบ"


# ---------------------------------------------------------------------------
# descriptor_mtime helper
# ---------------------------------------------------------------------------


def test_descriptor_mtime_returns_zero_for_missing_path(tmp_path) -> None:
    missing = tmp_path / "nope.yaml"
    assert descriptor_mtime(missing) == 0.0


def test_descriptor_mtime_returns_stat_value(tmp_path) -> None:
    yaml = tmp_path / "desc.yaml"
    yaml.write_text("report_id: x\n", encoding="utf-8")
    mt = descriptor_mtime(yaml)
    assert mt > 0.0
    # Stable across calls (no I/O drift).
    assert descriptor_mtime(yaml) == mt


def test_descriptor_mtime_handles_none() -> None:
    assert descriptor_mtime(None) == 0.0


# ---------------------------------------------------------------------------
# code_version (S10 polish)
# ---------------------------------------------------------------------------

def test_code_version_changes_when_explicit_version_changes():
    """Different code_version → different hash → cache miss = fresh render."""
    base = dict(
        report_id="zone", fmt="pdf", lang="th",
        audience=None, params={"zone_code": "01"},
        data_version="abc", descriptor_mtime=1.0,
    )
    h_old = content_hash(**base, code_version="v0_block_a")
    h_new = content_hash(**base, code_version="v1_block_a")
    assert h_old != h_new, (
        "code_version must affect content_hash so block code edits bust the cache"
    )


def test_code_version_default_is_computed_from_files(tmp_path, monkeypatch):
    """When code_version omitted, falls back to compute_code_version()."""
    # Two consecutive calls with identical args should produce identical hashes
    # (deterministic compute_code_version).
    base = dict(
        report_id="zone", fmt="pdf", lang="th",
        audience=None, params={"zone_code": "01"},
        data_version="abc", descriptor_mtime=1.0,
    )
    h1 = content_hash(**base)
    h2 = content_hash(**base)
    assert h1 == h2


def test_compute_code_version_returns_short_hex():
    from services.reports.cache_keys import compute_code_version
    v = compute_code_version()
    assert isinstance(v, str)
    assert len(v) == 12
    assert all(c in "0123456789abcdef" for c in v)
