"""Regression tests for the upload-excel post-flight sanity check.

Background
----------
A long-standing silent-failure bug in the upload pipeline let an export run
exit `rc=0` with **zero rows actually inserted** when the runtime DB user
lacked USAGE on the `bma_med` schema. `information_schema.columns` returned
empty rows, so `export.py` "skipped" every target table without raising. The
pipeline wrapper then flipped `import_history.status` to `'success'`, set
`progress_step='done'`, and the dashboard reported zeros with no error.

These tests pin the fix:
  * if the export step raises `psycopg2.errors.InsufficientPrivilege`, the
    `import_history` row ends `status='error'` with a sanitised message;
  * if export.py exits 0 but no `bma_med.*` target table grew, the
    post-flight sanity check trips and the row still ends `status='error'`,
    `progress_step` is NOT 'done', and `error_message` explains the issue.

We use the same in-memory fake style as `tests/services/test_chat_service.py`:
no real Postgres, no real subprocess. The pipeline writes through three
helpers (`_set_history_fields`, `_update_history`, `_update_progress` and
`_set_history_error`); we monkey-patch all four to record state on a fake
`import_history` row.
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
# Fakes
# --------------------------------------------------------------------------- #


class _FakeHistoryStore:
    """In-memory stand-in for the import_history row this pipeline mutates."""

    def __init__(self) -> None:
        # Mirror the production schema fields the pipeline touches.
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
        }
        self.audit_events: List[Dict[str, Any]] = []

    # The four helpers in admin.py that touch import_history. We replace them
    # all with closures over this store.

    def set_fields(self, history_id: int, **fields: Any) -> None:
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
    """Minimal psycopg2-style cursor over a fixed row-count table.

    Counts are looked up case-insensitively from the table name embedded in
    the SQL string. A `failure_mode` selects what kind of error to raise
    instead of returning a count, so we can simulate InsufficientPrivilege.
    """

    def __init__(
        self,
        row_counts: Dict[str, int],
        failure_mode: Optional[Exception] = None,
    ) -> None:
        self._row_counts = {k.lower(): v for k, v in row_counts.items()}
        self._failure_mode = failure_mode
        self._last: Optional[int] = None

    def execute(self, sql: str, params: Any = None) -> None:
        if self._failure_mode is not None:
            raise self._failure_mode
        # Extract the table name after FROM (very small, controlled SQL).
        upper = sql.upper()
        idx = upper.find("FROM ")
        if idx < 0:
            self._last = 0
            return
        rest = sql[idx + 5:].strip().split()[0].lower().strip(";")
        self._last = self._row_counts.get(rest, 0)

    def fetchone(self) -> Optional[tuple]:
        if self._last is None:
            return None
        return (self._last,)

    def close(self) -> None:
        pass

    # Context-manager support not required for the helper but keeps parity
    # with real psycopg2 cursors.
    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class _FakeConn:
    def __init__(
        self,
        row_counts: Dict[str, int],
        failure_mode: Optional[Exception] = None,
    ) -> None:
        self._row_counts = row_counts
        self._failure_mode = failure_mode
        self.autocommit = True
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._row_counts, self._failure_mode)

    def rollback(self) -> None:
        self.rolled_back = True

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
    """Wire monkey-patches for one `_resume_pipeline_export` invocation.

    Returns the `admin` module so callers can invoke the function under test.
    """
    import admin  # type: ignore

    # 1. Replace import_history mutators with closures over the fake store.
    monkeypatch.setattr(admin, "_set_history_fields", store.set_fields)
    monkeypatch.setattr(admin, "_update_history", store.update_history)
    monkeypatch.setattr(admin, "_update_progress", store.update_progress)
    monkeypatch.setattr(admin, "_set_history_error", store.set_history_error)
    monkeypatch.setattr(admin, "_audit", store.audit)

    # 2. Stub MV refresh (we don't care about it here).
    monkeypatch.setattr(
        admin, "_refresh_hot_mvs", lambda cur: {},
    )

    # 3. Replace psycopg2.connect with the fake conn factory.
    monkeypatch.setattr(admin.psycopg2, "connect", conn_factory)

    # 4. Stub subprocess.run so we never actually fork python3 export.py.
    fake_subprocess = types.SimpleNamespace(
        run=lambda *a, **kw: subprocess_result,
    )
    monkeypatch.setitem(sys.modules, "subprocess", fake_subprocess)

    # 5. Ignore tmpdir cleanup (fake tmpdir doesn't exist).
    monkeypatch.setattr(admin.shutil, "rmtree",
                        lambda *a, **kw: None)

    # 6. Cache flushes are imported lazily — stub the modules so the imports
    # inside the function don't reach real Redis / data-adapter code.
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


def test_export_insufficient_privilege_marks_history_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the post-flight COUNT(*) probes raise InsufficientPrivilege, the
    history row must end status='error' with a sanitised psycopg2 message —
    NOT status='success' / progress_step='done'."""
    import psycopg2  # type: ignore
    from psycopg2 import errors as pg_errors  # type: ignore

    store = _FakeHistoryStore()

    # The export subprocess "succeeds" but actually inserted nothing — the
    # post-flight check is what catches the problem.
    fake_result = types.SimpleNamespace(
        returncode=0,
        stdout="loaded bma_med.patient: 0 rows\n",
        stderr="",
    )

    # Build an InsufficientPrivilege carrying a libpq-style key=value
    # secret to verify _sanitize_error scrubs it.
    raise_exc = pg_errors.InsufficientPrivilege(
        "permission denied for schema bma_med "
        "host=secret-host.internal password=hunter2"
    )

    def conn_factory(*_a: Any, **_kw: Any) -> _FakeConn:
        return _FakeConn(row_counts={}, failure_mode=raise_exc)

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

    # Status must be 'error' — never 'success'.
    assert store.row["status"] == "error", store.row
    assert store.row["status"] != "success"
    # progress_step must NOT have advanced to 'done'.
    assert store.row["progress_step"] != "done", store.row["progress_step"]

    # Error message must mention the underlying privilege problem.
    err = store.row["error_message"] or ""
    assert "permission denied" in err.lower() or \
        "InsufficientPrivilege" in err or \
        "sanity check" in err.lower(), err

    # The libpq secrets must NOT survive sanitisation.
    assert "hunter2" not in err
    assert "secret-host.internal" not in err
    assert "password=" not in err.lower() or "password=***" in err.lower()


def test_export_silent_zero_rows_marks_history_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The original silent-failure bug: export.py exits 0 but every bma_med
    target table is empty. Pipeline must NOT mark this row 'success'."""
    store = _FakeHistoryStore()

    fake_result = types.SimpleNamespace(
        returncode=0,
        stdout="loaded bma_med.patient: 0 rows\n",
        stderr="",
    )

    # Every target table reports 0 rows — no exception, just empty.
    def conn_factory(*_a: Any, **_kw: Any) -> _FakeConn:
        return _FakeConn(row_counts={
            "bma_med.patient": 0,
            "bma_med.app1_patient": 0,
            "bma_med.app2_patient": 0,
            "bma_med.portal_patient": 0,
        })

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

    assert store.row["status"] == "error", store.row
    assert store.row["status"] != "success"
    assert store.row["progress_step"] != "done"
    err = store.row["error_message"] or ""
    assert "sanity check" in err.lower() or "no bma_med" in err.lower(), err


def test_export_legitimate_success_still_flips_to_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Don't break the happy path — a real export with rows inserted must
    still flip the history row to status='success' and progress_step='done'.
    """
    store = _FakeHistoryStore()

    fake_result = types.SimpleNamespace(
        returncode=0,
        stdout="loaded bma_med.patient: 1234 rows exported\n",
        stderr="",
    )

    def conn_factory(*_a: Any, **_kw: Any) -> _FakeConn:
        return _FakeConn(row_counts={
            "bma_med.patient": 1234,
            "bma_med.app1_patient": 1234,
            "bma_med.app2_patient": 0,
            "bma_med.portal_patient": 0,
        })

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

    assert store.row["status"] == "success", store.row
    assert store.row["progress_step"] == "done"
    assert store.row["error_message"] is None


def test_sanitize_error_strips_libpq_secrets() -> None:
    """Direct unit test for the augmented sanitiser: libpq key=value secrets
    must be redacted alongside postgresql:// URIs."""
    import admin  # type: ignore

    exc = RuntimeError(
        "connect failed: host=db.internal password=letmein "
        "user=etl_user dbname=bma_med "
        "via postgresql://etl:letmein@db.internal/bma_med"
    )
    out = admin._sanitize_error(exc)
    assert "letmein" not in out
    assert "db.internal" not in out
    assert "etl_user" not in out
    assert "host=***" in out
    assert "password=***" in out
    assert "postgresql://***" in out
