"""Unit tests for ``api/services/reports/pdf_cache.py`` (S9 Task 1.2).

Coverage targets:
    * round-trip ``put`` → ``get``
    * idempotent re-put overwrites cleanly
    * file-on-disk + manifest stay in sync (manual rm → next get returns None)
    * ``last_served_at`` flips after ``touch``
    * ``evict_older_than`` drops the right rows
    * ``stats`` shape matches both spec keys + legacy aliases
    * ``list_for_report`` filters by report_id

Tests use ``tmp_path`` so each one gets a fresh cache directory.
"""
from __future__ import annotations

import json
import os
import sys
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Ensure ``api/`` is importable.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.reports.pdf_cache import (  # noqa: E402
    ManifestRow,
    PdfCache,
    get_pdf_cache,
    reset_pdf_cache_singleton,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cache(tmp_path: Path) -> PdfCache:
    """Fresh cache rooted at a tmp dir."""
    return PdfCache(tmp_path / "cache")


@pytest.fixture
def fake_pdf(tmp_path: Path) -> Path:
    """A stand-in PDF source the cache will copy in."""
    p = tmp_path / "src.pdf"
    p.write_bytes(b"%PDF-1.4 fake content")
    return p


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_put_then_get_returns_cached_path(
    cache: PdfCache, fake_pdf: Path
) -> None:
    """After ``put``, ``get`` returns a path that exists on disk."""
    cache.put(
        "deadbeef00000001",
        fake_pdf,
        report_id="zone",
        fmt="pdf",
        lang="th",
        audience=None,
        params={"zone_code": "03"},
        data_version="abc123",
    )
    cached_path = cache.get("deadbeef00000001")
    assert cached_path is not None
    assert cached_path.exists()
    # Extension and naming derived from fmt + hash.
    assert cached_path.name == "deadbeef00000001.pdf"
    assert cached_path.read_bytes() == b"%PDF-1.4 fake content"


def test_get_unknown_hash_returns_none(cache: PdfCache) -> None:
    """Asking for a hash that was never put returns None (no error)."""
    assert cache.get("nope0000nope0000") is None


def test_put_does_not_move_source(
    cache: PdfCache, fake_pdf: Path
) -> None:
    """``put`` must COPY (not move) the source — the renderer's tmp dir
    might still want the original after the cache is populated."""
    cache.put(
        "h0000000h0000000", fake_pdf,
        report_id="r", fmt="pdf", lang="th",
        data_version="v",
    )
    # Source still exists after put.
    assert fake_pdf.exists()
    assert fake_pdf.read_bytes() == b"%PDF-1.4 fake content"


def test_put_is_idempotent_on_same_hash(
    cache: PdfCache, fake_pdf: Path, tmp_path: Path
) -> None:
    """Re-putting the same hash overwrites cleanly without raising."""
    cache.put(
        "abcd1234abcd1234", fake_pdf,
        report_id="r", fmt="pdf", lang="th",
        data_version="v1",
    )

    # Different content, same hash — second put wins.
    new_src = tmp_path / "src2.pdf"
    new_src.write_bytes(b"%PDF-1.4 newer")
    cache.put(
        "abcd1234abcd1234", new_src,
        report_id="r", fmt="pdf", lang="th",
        data_version="v2",
    )

    cached = cache.get("abcd1234abcd1234")
    assert cached is not None
    assert cached.read_bytes() == b"%PDF-1.4 newer"

    # Manifest only has the most recent row (no duplicates).
    rows = cache.list_for_report("r")
    assert len(rows) == 1
    assert rows[0].data_version == "v2"


# ---------------------------------------------------------------------------
# File / manifest synchronisation
# ---------------------------------------------------------------------------


def test_get_drops_row_if_file_disappeared(
    cache: PdfCache, fake_pdf: Path
) -> None:
    """If somebody manually deleted the file, ``get`` returns None AND
    cleans up the orphan manifest row."""
    cache.put(
        "0000111122223333", fake_pdf,
        report_id="r", fmt="pdf", lang="th",
        data_version="v",
    )
    # Yank the file out from under us.
    target = cache.root / "0000111122223333.pdf"
    target.unlink()

    assert cache.get("0000111122223333") is None
    # And the manifest row is gone too.
    assert cache.list_for_report("r") == []


def test_put_missing_source_raises(cache: PdfCache, tmp_path: Path) -> None:
    missing = tmp_path / "ghost.pdf"
    with pytest.raises(FileNotFoundError):
        cache.put(
            "ghost00000000000", missing,
            report_id="r", fmt="pdf", lang="th",
            data_version="v",
        )


# ---------------------------------------------------------------------------
# touch / last_served_at
# ---------------------------------------------------------------------------


def test_touch_updates_last_served_at(
    cache: PdfCache, fake_pdf: Path
) -> None:
    cache.put(
        "touch0000touch00", fake_pdf,
        report_id="r", fmt="pdf", lang="th",
        data_version="v",
    )
    rows_before = cache.list_for_report("r")
    assert rows_before[0].last_served_at is None

    cache.touch("touch0000touch00")
    rows_after = cache.list_for_report("r")
    assert rows_after[0].last_served_at is not None
    # ISO-8601-ish — at minimum starts with year.
    assert rows_after[0].last_served_at.startswith(("20", "19"))


# ---------------------------------------------------------------------------
# Eviction / GC
# ---------------------------------------------------------------------------


def test_evict_older_than_drops_old_rows_only(
    cache: PdfCache, fake_pdf: Path
) -> None:
    """Two puts; we backdate one in the manifest and evict.

    Backdating directly via SQLite is the cheapest way to simulate
    age — much faster than spinning the clock.
    """
    cache.put(
        "old0000old0000aa", fake_pdf,
        report_id="r", fmt="pdf", lang="th",
        data_version="v",
    )
    cache.put(
        "new0000new0000aa", fake_pdf,
        report_id="r", fmt="pdf", lang="th",
        data_version="v",
    )

    # Backdate "old" by 30 days.
    backdated = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(
        timespec="seconds"
    )
    with sqlite3.connect(cache.manifest_path) as conn:
        conn.execute(
            "UPDATE manifest SET created_at = ? WHERE hash = ?",
            (backdated, "old0000old0000aa"),
        )
        conn.commit()

    dropped = cache.evict_older_than(days=14)
    assert dropped == 1

    # Old gone, new survives.
    assert cache.get("old0000old0000aa") is None
    assert cache.get("new0000new0000aa") is not None
    # File on disk for the old one is gone too.
    assert not (cache.root / "old0000old0000aa.pdf").exists()


def test_evict_zero_days_is_a_noop(cache: PdfCache, fake_pdf: Path) -> None:
    cache.put(
        "n00000000000000a", fake_pdf,
        report_id="r", fmt="pdf", lang="th",
        data_version="v",
    )
    assert cache.evict_older_than(0) == 0
    assert cache.get("n00000000000000a") is not None


def test_delete_removes_row_and_file(
    cache: PdfCache, fake_pdf: Path
) -> None:
    cache.put(
        "del000000000000a", fake_pdf,
        report_id="r", fmt="pdf", lang="th",
        data_version="v",
    )
    assert cache.delete("del000000000000a") is True
    assert cache.get("del000000000000a") is None
    assert cache.delete("del000000000000a") is False  # idempotent


# ---------------------------------------------------------------------------
# list_for_report / stats
# ---------------------------------------------------------------------------


def test_list_for_report_filters_by_report_id(
    cache: PdfCache, fake_pdf: Path
) -> None:
    cache.put(
        "a000000000000001", fake_pdf,
        report_id="zone", fmt="pdf", lang="th",
        data_version="v",
    )
    cache.put(
        "a000000000000002", fake_pdf,
        report_id="zone", fmt="pdf", lang="en",
        data_version="v",
    )
    cache.put(
        "a000000000000003", fake_pdf,
        report_id="whitepaper", fmt="pdf", lang="th",
        data_version="v",
    )

    zone_rows = cache.list_for_report("zone")
    assert {r.hash for r in zone_rows} == {
        "a000000000000001",
        "a000000000000002",
    }
    wp_rows = cache.list_for_report("whitepaper")
    assert {r.hash for r in wp_rows} == {"a000000000000003"}
    assert cache.list_for_report("nope") == []


def test_stats_aggregates_across_reports(
    cache: PdfCache, fake_pdf: Path
) -> None:
    cache.put(
        "s000000000000001", fake_pdf,
        report_id="zone", fmt="pdf", lang="th",
        data_version="v",
    )
    cache.put(
        "s000000000000002", fake_pdf,
        report_id="zone", fmt="pdf", lang="en",
        data_version="v",
    )
    cache.put(
        "s000000000000003", fake_pdf,
        report_id="whitepaper", fmt="pdf", lang="th",
        data_version="v",
    )

    s = cache.stats()
    assert s["total_files"] == 3
    assert s["total_bytes"] > 0
    assert s["oldest"] is not None
    assert s["newest"] is not None
    # Spec keys + legacy aliases both present.
    assert s["oldest_entry"] == s["oldest"]
    assert s["newest_entry"] == s["newest"]
    assert s["by_report"]["zone"]["files"] == 2
    assert s["by_report"]["whitepaper"]["files"] == 1


def test_audience_round_trip_through_manifest(
    cache: PdfCache, fake_pdf: Path
) -> None:
    """Audience iterables are stored sorted + de-duped, deserialised
    back into a list."""
    cache.put(
        "aud0000000000001", fake_pdf,
        report_id="r", fmt="pdf", lang="th",
        audience=["clinician", "executive", "executive"],
        data_version="v",
    )
    rows = cache.list_for_report("r")
    assert len(rows) == 1
    assert rows[0].audience == ["clinician", "executive"]


def test_params_json_round_trip(
    cache: PdfCache, fake_pdf: Path
) -> None:
    cache.put(
        "par0000000000001", fake_pdf,
        report_id="r", fmt="pdf", lang="th",
        params={"zone_code": "03", "year": 2026},
        data_version="v",
    )
    rows = cache.list_for_report("r")
    assert rows[0].params == {"zone_code": "03", "year": 2026}


# ---------------------------------------------------------------------------
# Singleton helper
# ---------------------------------------------------------------------------


def test_get_pdf_cache_singleton_is_idempotent(tmp_path: Path) -> None:
    reset_pdf_cache_singleton()
    a = get_pdf_cache(tmp_path / "x")
    b = get_pdf_cache()  # no root → reuse cached singleton
    assert a is b
    reset_pdf_cache_singleton()


def test_get_pdf_cache_with_new_root_replaces_singleton(
    tmp_path: Path,
) -> None:
    reset_pdf_cache_singleton()
    a = get_pdf_cache(tmp_path / "x")
    b = get_pdf_cache(tmp_path / "y")
    assert a is not b
    assert b.root == (tmp_path / "y")
    reset_pdf_cache_singleton()
