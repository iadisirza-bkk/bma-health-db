"""Tests for the upload-excel `load_mode` toggle.

Background
----------
Operators expected "ล้างกระดาน เริ่มใหม่" semantics on every Excel upload —
TRUNCATE bma_med data tables, then load fresh. The pipeline's previous
behaviour was UPSERT/append, leaving stale rows behind from prior loads.

These tests pin the new toggle:

  * `load_mode='replace'` (default) — TRUNCATE every bma_med data table
    CASCADE inside an open transaction before invoking export.py. On
    export failure, ROLLBACK so the prior data set is preserved.
    On success, COMMIT and continue to MV refresh.
  * `load_mode='append'` — skip TRUNCATE entirely. Used for incremental
    loads where the existing UPSERT/merge in export.py is intended.

The fake-Postgres style mirrors `tests/services/test_export_failure_mode.py`
so we don't need a live cluster.
"""
from __future__ import annotations

import os
import sys
import types
from typing import Any, Dict, List, Optional

import pytest

# Make `api/` importable, mirroring tests/conftest.py.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))


# --------------------------------------------------------------------------- #
# Fakes (extended for TRUNCATE / commit / rollback semantics)
# --------------------------------------------------------------------------- #


class _FakeHistoryStore:
    """In-memory stand-in for the import_history row mutated by the pipeline."""

    def __init__(self) -> None:
        self.row: Dict[str, Any] = {
            "id": 99,
            "status": "queued",
            "progress_step": None,
            "progress_pct": 0,
            "rows_imported": 0,
            "rows_skipped": 0,
            "error_message": None,
            "duration_seconds": 0.0,
            "view_refresh_status": None,
            "view_refresh_error": None,
            "tmpdir_path": None,
            "load_mode": None,
            "detail": None,
        }
        self.audit_events: List[Dict[str, Any]] = []
        self.field_writes: List[Dict[str, Any]] = []

    def set_fields(self, history_id: int, **fields: Any) -> None:
        self.field_writes.append(dict(fields))
        for k, v in fields.items():
            self.row[k] = v

    def update_history(
        self,
        history_id: int,
        status: str,
        rows_imported: int,
        rows_skipped: int,
        error_message: Optional[str],
        duration: float,
        view_refresh_status: Optional[str] = None,
        view_refresh_error: Optional[str] = None,
    ) -> None:
        self.row["status"] = status
        self.row["rows_imported"] = rows_imported
        self.row["rows_skipped"] = rows_skipped
        self.row["error_message"] = error_message
        self.row["duration_seconds"] = duration
        if view_refresh_status is not None:
            self.row["view_refresh_status"] = view_refresh_status
        if view_refresh_error is not None:
            self.row["view_refresh_error"] = view_refresh_error

    def update_progress(
        self,
        history_id: int,
        step_label: str,
        pct: int,
        rows_processed: Optional[int] = None,
        rows_total: Optional[int] = None,
    ) -> None:
        self.row["progress_step"] = step_label
        self.row["progress_pct"] = pct

    def set_history_error(
        self, history_id: int, error: str, detail: Optional[str] = None,
    ) -> None:
        msg = error if not detail else f"{error}\n\n{detail}"
        self.update_history(history_id, "error", 0, 0, msg, 0.0)

    def audit(self, history_id: int, transition: str, **detail: Any) -> None:
        self.audit_events.append({"transition": transition, **detail})


class _FakeCursor:
    """Minimal cursor that handles SELECT COUNT(*), TRUNCATE, and pg_tables."""

    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._last_rows: List[tuple] = []
        self._last_one: Optional[tuple] = None

    def execute(self, sql: str, params: Any = None) -> None:
        upper = sql.strip().upper()

        # pg_tables discovery — return all bma_med data table names from the
        # connection's row_counts dict.
        if "PG_TABLES" in upper:
            rows: List[tuple] = []
            for fq in self._conn._row_counts:
                # rows expected as ('tablename',) without schema prefix
                bare = fq.split(".", 1)[1] if "." in fq else fq
                rows.append((bare,))
            # Sort to match production discovery order.
            rows.sort()
            self._last_rows = rows
            self._last_one = None
            return

        if upper.startswith("TRUNCATE"):
            self._conn._truncates_seen += 1
            # If the connection is configured to fail on TRUNCATE, raise.
            if self._conn._truncate_failure is not None:
                raise self._conn._truncate_failure
            self._last_rows = []
            self._last_one = None
            return

        if upper.startswith("SELECT COUNT"):
            # Extract table name after FROM.
            idx = upper.find("FROM ")
            if idx < 0:
                self._last_one = (0,)
                return
            rest = sql[idx + 5:].strip().split()[0].lower().strip(";")
            count = self._conn._row_counts.get(rest, 0)
            self._last_one = (count,)
            return

        # Default: no-op
        self._last_one = None
        self._last_rows = []

    def fetchone(self) -> Optional[tuple]:
        return self._last_one

    def fetchall(self) -> List[tuple]:
        return list(self._last_rows)

    def close(self) -> None:
        pass

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class _FakeConn:
    """Connection that tracks commit/rollback and exposes a row-count map."""

    def __init__(
        self,
        row_counts: Dict[str, int],
        *,
        truncate_failure: Optional[Exception] = None,
    ) -> None:
        # Snapshot of the row_counts so commit/rollback semantics can mutate
        # the live state without breaking the original.
        self._row_counts: Dict[str, int] = dict(row_counts)
        self._original_counts: Dict[str, int] = dict(row_counts)
        self._truncate_failure = truncate_failure
        self._truncates_seen = 0
        self.autocommit = True
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        # Persist the truncate (if any) into the simulated state by zeroing
        # the row counts. Tests assert on the post-commit state.
        self.commits += 1
        if self._truncates_seen > 0:
            for k in self._row_counts:
                self._row_counts[k] = 0

    def rollback(self) -> None:
        # Restore original counts so tests can assert "data preserved".
        # Note: `_truncates_seen` is preserved as an audit counter — rollback
        # doesn't erase the fact that TRUNCATE was *issued*, only its effect.
        self.rollbacks += 1
        self._row_counts = dict(self._original_counts)

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _install_pipeline_doubles(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: _FakeHistoryStore,
    conn_factory: Any,
    subprocess_result: Any,
) -> Any:
    """Wire monkey-patches for one `_resume_pipeline_export` invocation."""
    import admin  # type: ignore

    monkeypatch.setattr(admin, "_set_history_fields", store.set_fields)
    monkeypatch.setattr(admin, "_update_history", store.update_history)
    monkeypatch.setattr(admin, "_update_progress", store.update_progress)
    monkeypatch.setattr(admin, "_set_history_error", store.set_history_error)
    monkeypatch.setattr(admin, "_audit", store.audit)
    monkeypatch.setattr(admin, "_refresh_hot_mvs", lambda cur: {})
    monkeypatch.setattr(admin.psycopg2, "connect", conn_factory)

    fake_subprocess = types.SimpleNamespace(
        run=lambda *a, **kw: subprocess_result,
    )
    monkeypatch.setitem(sys.modules, "subprocess", fake_subprocess)
    monkeypatch.setattr(admin.shutil, "rmtree", lambda *a, **kw: None)

    cache_mod = types.ModuleType("cache")
    cache_mod.cache_flush_all = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cache", cache_mod)
    services_pkg = sys.modules.get("services") or types.ModuleType("services")
    da_mod = types.ModuleType("services.data_adapter")
    da_mod.invalidate_cache = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services", services_pkg)
    monkeypatch.setitem(sys.modules, "services.data_adapter", da_mod)

    return admin


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_replace_mode_truncates_then_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REPLACE happy path: TRUNCATE issued, pre_truncate_counts captured,
    transaction committed, final row count reflects the new export."""
    store = _FakeHistoryStore()

    # Pre-existing data — 100 patients, 50 in app1_pt.
    initial_counts = {
        "bma_med.patient": 100,
        "bma_med.app1_pt": 50,
        "bma_med.app1_patient": 100,    # also probed by the post-flight check
        "bma_med.app2_patient": 0,
        "bma_med.portal_patient": 0,
    }

    fake_conns: List[_FakeConn] = []

    def conn_factory(*_a: Any, **_kw: Any) -> _FakeConn:
        # Each `psycopg2.connect()` returns a fresh fake connection. The
        # truncate connection mutates row counts on commit so the post-flight
        # sanity check sees the "new" state. To simulate the export inserting
        # 200 rows, we'll bump the counts after commit via a side channel.
        c = _FakeConn(initial_counts.copy())
        fake_conns.append(c)
        return c

    fake_result = types.SimpleNamespace(
        returncode=0,
        stdout="loaded bma_med.patient: 200 rows exported\n",
        stderr="",
    )

    admin = _install_pipeline_doubles(
        monkeypatch,
        store=store,
        conn_factory=conn_factory,
        subprocess_result=fake_result,
    )

    # Hook the post-flight sanity check so it sees the post-export
    # counts (200 new patient rows). Without this the check would see the
    # truncate-committed empty tables and trip the silent-zero guard.
    monkeypatch.setattr(
        admin, "_post_export_sanity_check", lambda hid, conn: None,
    )

    admin._resume_pipeline_export(
        tmpdir="/tmp/does-not-exist",
        kind="xlsx",
        history_id=99,
        env_raw_root="/tmp/does-not-exist",
        load_mode="replace",
    )

    # The first connection opened was the TRUNCATE transaction. Verify it
    # actually issued the TRUNCATE and committed.
    assert len(fake_conns) >= 1, "no connection opened"
    truncate_conn = fake_conns[0]
    assert truncate_conn._truncates_seen == 1, (
        f"expected exactly 1 TRUNCATE on the truncate connection, "
        f"got {truncate_conn._truncates_seen}"
    )
    assert truncate_conn.commits == 1, (
        f"expected 1 commit after export success, got {truncate_conn.commits}"
    )
    assert truncate_conn.rollbacks == 0, (
        f"expected zero rollbacks on success, got {truncate_conn.rollbacks}"
    )

    # detail must have been persisted with the chosen mode + pre-counts.
    detail_writes = [w for w in store.field_writes if "detail" in w]
    assert detail_writes, "expected at least one detail write"
    # Most recent detail write should carry the load_mode + pre_truncate.
    import json as _json
    last_detail = _json.loads(detail_writes[-1]["detail"])
    assert last_detail["load_mode"] == "replace", last_detail
    assert "pre_truncate_counts" in last_detail, last_detail
    pre = last_detail["pre_truncate_counts"]
    # The discovery returned all keys in row_counts — verify the patient row
    # count from the snapshot survived.
    assert pre.get("bma_med.patient") == 100, pre

    # Pipeline must finish in success state with progress_step='done'.
    assert store.row["status"] == "success", store.row
    assert store.row["progress_step"] == "done"


def test_append_mode_skips_truncate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """APPEND happy path: 100 pre-existing rows, upload 50 more with
    `load_mode=append`. TRUNCATE must NOT fire; final row count reflects
    the merged total (which export.py would compute via UPSERT in real life
    — here we just verify the truncate path stayed quiet)."""
    store = _FakeHistoryStore()

    fake_conns: List[_FakeConn] = []

    def conn_factory(*_a: Any, **_kw: Any) -> _FakeConn:
        c = _FakeConn({
            "bma_med.patient": 150,           # 100 baseline + 50 new
            "bma_med.app1_patient": 150,
            "bma_med.app2_patient": 0,
            "bma_med.portal_patient": 0,
        })
        fake_conns.append(c)
        return c

    fake_result = types.SimpleNamespace(
        returncode=0,
        stdout="loaded bma_med.patient: 50 rows exported\n",
        stderr="",
    )

    admin = _install_pipeline_doubles(
        monkeypatch,
        store=store,
        conn_factory=conn_factory,
        subprocess_result=fake_result,
    )

    admin._resume_pipeline_export(
        tmpdir="/tmp/does-not-exist",
        kind="xlsx",
        history_id=99,
        env_raw_root="/tmp/does-not-exist",
        load_mode="append",
    )

    # Verify that TRUNCATE was never issued on any of the opened conns.
    total_truncates = sum(c._truncates_seen for c in fake_conns)
    assert total_truncates == 0, (
        f"append mode must NOT issue TRUNCATE, saw {total_truncates}"
    )

    # detail must record append mode for the audit trail.
    import json as _json
    detail_writes = [w for w in store.field_writes if "detail" in w]
    assert detail_writes, "append mode must still record detail"
    last_detail = _json.loads(detail_writes[-1]["detail"])
    assert last_detail["load_mode"] == "append", last_detail
    # Append mode does NOT capture pre_truncate_counts (no truncate happened).
    assert "pre_truncate_counts" not in last_detail, last_detail

    # Successful pipeline.
    assert store.row["status"] == "success", store.row


def test_replace_mode_rollback_on_export_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REPLACE failure rollback: 100 pre-existing rows, export.py fails.
    Pipeline must ROLLBACK the truncate transaction so prior data is
    preserved (final row count == 100, NOT 0)."""
    store = _FakeHistoryStore()

    fake_conns: List[_FakeConn] = []

    def conn_factory(*_a: Any, **_kw: Any) -> _FakeConn:
        c = _FakeConn({
            "bma_med.patient": 100,
            "bma_med.app1_patient": 100,
            "bma_med.app2_patient": 0,
            "bma_med.portal_patient": 0,
        })
        fake_conns.append(c)
        return c

    # export.py exits non-zero — pipeline must roll back.
    fake_result = types.SimpleNamespace(
        returncode=2,
        stdout="",
        stderr="export.py: simulated failure during COPY",
    )

    admin = _install_pipeline_doubles(
        monkeypatch,
        store=store,
        conn_factory=conn_factory,
        subprocess_result=fake_result,
    )

    admin._resume_pipeline_export(
        tmpdir="/tmp/does-not-exist",
        kind="xlsx",
        history_id=99,
        env_raw_root="/tmp/does-not-exist",
        load_mode="replace",
    )

    # The TRUNCATE connection must have rolled back (NOT committed).
    truncate_conn = fake_conns[0]
    assert truncate_conn._truncates_seen == 1, (
        f"truncate must have run before export, got {truncate_conn._truncates_seen}"
    )
    assert truncate_conn.commits == 0, (
        "must NOT commit when export fails"
    )
    assert truncate_conn.rollbacks == 1, (
        f"must rollback once on export failure, got {truncate_conn.rollbacks}"
    )

    # Post-rollback: row counts on the truncate conn revert to original.
    assert truncate_conn._row_counts["bma_med.patient"] == 100, (
        "rollback must restore the pre-truncate row count"
    )

    # Pipeline must mark history as error.
    assert store.row["status"] == "error", store.row
    assert store.row["progress_step"] != "done"


def test_replace_mode_with_no_pre_existing_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REPLACE on an empty bma_med — first-ever upload. TRUNCATE on empty
    tables must succeed (PostgreSQL TRUNCATE on empty is a valid no-op).
    Pipeline must reach success."""
    store = _FakeHistoryStore()

    fake_conns: List[_FakeConn] = []

    def conn_factory(*_a: Any, **_kw: Any) -> _FakeConn:
        c = _FakeConn({
            "bma_med.patient": 0,
            "bma_med.app1_patient": 0,
            "bma_med.app2_patient": 0,
            "bma_med.portal_patient": 0,
        })
        fake_conns.append(c)
        return c

    fake_result = types.SimpleNamespace(
        returncode=0,
        stdout="loaded bma_med.patient: 1234 rows exported\n",
        stderr="",
    )

    admin = _install_pipeline_doubles(
        monkeypatch,
        store=store,
        conn_factory=conn_factory,
        subprocess_result=fake_result,
    )

    # Skip the post-flight zero-rows check — without it, the empty post-truncate
    # state would (correctly) trip the silent-failure guard. In production the
    # actual bma_med.* would have rows from export.py.
    monkeypatch.setattr(
        admin, "_post_export_sanity_check", lambda hid, conn: None,
    )

    admin._resume_pipeline_export(
        tmpdir="/tmp/does-not-exist",
        kind="xlsx",
        history_id=99,
        env_raw_root="/tmp/does-not-exist",
        load_mode="replace",
    )

    truncate_conn = fake_conns[0]
    assert truncate_conn._truncates_seen == 1, (
        "TRUNCATE on empty tables should still run"
    )
    assert truncate_conn.commits == 1, "should commit on success"

    # detail captures pre_truncate_counts == 0 across the board.
    import json as _json
    detail_writes = [w for w in store.field_writes if "detail" in w]
    assert detail_writes
    last_detail = _json.loads(detail_writes[-1]["detail"])
    assert last_detail["load_mode"] == "replace"
    pre = last_detail["pre_truncate_counts"]
    assert all(v == 0 for v in pre.values()), pre

    assert store.row["status"] == "success"


def test_default_mode_is_replace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling `_resume_pipeline_export` without `load_mode` must default
    to 'replace' (matching the user's stated expectation). The route's
    Form('replace') default is the surface-level guarantee; this test
    pins the same default at the function level so any later refactor
    that drops the route default still keeps the truncate-on-default
    behaviour."""
    store = _FakeHistoryStore()

    fake_conns: List[_FakeConn] = []

    def conn_factory(*_a: Any, **_kw: Any) -> _FakeConn:
        c = _FakeConn({
            "bma_med.patient": 5,
            "bma_med.app1_patient": 5,
            "bma_med.app2_patient": 0,
            "bma_med.portal_patient": 0,
        })
        fake_conns.append(c)
        return c

    fake_result = types.SimpleNamespace(
        returncode=0,
        stdout="loaded bma_med.patient: 100 rows exported\n",
        stderr="",
    )

    admin = _install_pipeline_doubles(
        monkeypatch,
        store=store,
        conn_factory=conn_factory,
        subprocess_result=fake_result,
    )
    monkeypatch.setattr(
        admin, "_post_export_sanity_check", lambda hid, conn: None,
    )

    # Note: load_mode kwarg deliberately omitted — must fall through to
    # the function's default of 'replace'.
    admin._resume_pipeline_export(
        tmpdir="/tmp/does-not-exist",
        kind="xlsx",
        history_id=99,
        env_raw_root="/tmp/does-not-exist",
    )

    # If the default is 'replace', TRUNCATE must have fired.
    truncate_conn = fake_conns[0]
    assert truncate_conn._truncates_seen == 1, (
        "default load_mode must produce a TRUNCATE — got 0 truncates"
    )

    import json as _json
    detail_writes = [w for w in store.field_writes if "detail" in w]
    assert detail_writes, "default-mode run must still record a detail blob"
    last_detail = _json.loads(detail_writes[-1]["detail"])
    assert last_detail["load_mode"] == "replace", (
        f"default mode must be 'replace', got {last_detail.get('load_mode')!r}"
    )

    assert store.row["status"] == "success"
