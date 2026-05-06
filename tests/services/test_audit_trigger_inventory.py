"""S11: regression test for audit-trigger scope.

After migration 500, audit triggers should ONLY exist on bma_med.patient.
If this test fails, someone re-introduced bulk-table auditing — likely a
generate_table_ddl.py regression. Drop the new triggers and fix the
generator before re-running data imports.

Why this matters
----------------
Today's audit_log is 80k rows / 346 MB. The 13 bulk source tables
(app1_*, portal_*, app2_*) used to have audit triggers writing
to_jsonb(OLD) + to_jsonb(NEW) on every INSERT/UPDATE/DELETE. The next
data import of 5.3M rows would have produced ~10-15M audit_log entries,
inflating the table to ~20-30 GB with no clinical benefit (the source
tables hold immutable raw screening readings).

Migration db/migrations/500_drop_audit_triggers_on_bulk_tables.sql
removed those triggers. This test pins the post-migration state.
"""
from __future__ import annotations

import os

import psycopg2
import pytest


@pytest.fixture(scope="module")
def db_conn():
    """Real DB connection — autocommit, read-only.

    Skips (rather than fails) when no Postgres is reachable so the test
    is CI-friendly even without a live DB.
    """
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:bma_health_dev@localhost:5433/bma_health",
    )
    try:
        conn = psycopg2.connect(url)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable for audit-trigger test: {exc}")
    conn.autocommit = True
    yield conn
    conn.close()


def test_audit_triggers_only_on_patient(db_conn):
    """Audit triggers must live on bma_med.patient and nowhere else.

    Reintroducing audit triggers on the 13 bulk source tables would
    inflate audit_log by ~20-30 GB per data import — see migration 500
    for context.
    """
    cur = db_conn.cursor()
    cur.execute(
        """
        SELECT tgrelid::regclass::text AS tbl
        FROM pg_trigger
        WHERE NOT tgisinternal
          AND tgname LIKE '%audit%'
          AND tgrelid::regclass::text LIKE 'bma_med.%'
        ORDER BY tbl
        """
    )
    audited = [row[0] for row in cur.fetchall()]
    assert audited == ["bma_med.patient"], (
        f"Audit triggers found on tables other than bma_med.patient: {audited}. "
        f"Bulk-table auditing inflates audit_log by ~20-30 GB per import. "
        f"Run db/migrations/500_drop_audit_triggers_on_bulk_tables.sql to fix."
    )
