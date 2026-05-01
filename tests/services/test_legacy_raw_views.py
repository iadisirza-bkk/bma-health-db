"""Regression tests for the legacy raw_* compatibility views (S6).

Background
----------
The S1 cutover migrated everything to bma_med.* and dropped the original
raw_* table layer. Several routers (summary.py, monitoring.py, executive.py,
trends.py, kpi.py, epidemiology.py) still reference the legacy raw_* names
in 600+ LOC of validated SQL we don't want to rewrite.

`migrations/400_compat_raw_views.sql` re-creates each legacy raw_* as a
SELECT-only VIEW over the new bma_med.* sources. This test suite pins
each view's existence, projects the columns the legacy code expects, and
asserts that representative legacy queries don't crash.

These tests need a real PostgreSQL connection — they cannot be faked
because the bug they protect against (UndefinedTable) is a property of
the live schema, not application code.
"""
from __future__ import annotations

import os
import sys
from typing import Iterable

import psycopg2
import psycopg2.extras
import pytest

# Make `api/` importable so we get the same DATABASE_URL the app uses.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from config import DATABASE_URL  # noqa: E402  (sys.path mutation above)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def db_conn():
    """Real DB connection — autocommit, read-only usage."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable for legacy-views tests: {exc}")
    conn.autocommit = True
    yield conn
    conn.close()


# Each entry: (view_name, expected_columns_subset)
# We only assert that the listed columns are projected — the view is allowed
# to project additional columns (that's how the compat layer evolves).
LEGACY_VIEWS: dict[str, set[str]] = {
    "raw_vitalsigns": {
        "patient_id", "data_source", "cancel_status", "visit_date",
        "district_code", "sbp", "dbp", "weight_kg", "height_cm",
        "waist_cm", "smoking",
        "risk_dm", "risk_hpt", "risk_cvd", "risk_bmi",
        "found_dyslipidemia", "found_obesity", "found_stroke",
        "depression_2q_1", "depression_2q_2",
        "phq9_q1", "phq9_q2", "phq9_q3", "phq9_q4", "phq9_q5",
        "phq9_q6", "phq9_q7", "phq9_q8", "phq9_q9",
        "st5_q1", "st5_q2", "st5_q3", "st5_q4", "st5_q5",
    },
    "raw_homevisit": {
        "patient_id", "data_source", "home_province", "district_code",
    },
    "raw_homehealth": {
        "patient_id", "data_source", "cancel_status", "exercise",
    },
    "raw_lab_results": {
        "patient_id", "data_source", "cancel_status",
        "hemoglobin", "hematocrit", "fbs", "cholesterol", "triglyceride",
        "hdl", "ldl", "creatinine", "egfr", "uric_acid", "sgot", "sgpt",
    },
    "raw_patients": {
        "patient_id", "sex_code", "birthdate", "first_seen", "last_seen",
    },
}


def _columns_of(conn, view_name: str) -> set[str]:
    """Return the column-name set of `public.<view_name>`."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (view_name,),
        )
        return {r[0] for r in cur.fetchall()}


# --------------------------------------------------------------------------- #
# 1. Existence + column projection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("view_name", sorted(LEGACY_VIEWS.keys()))
def test_view_exists(db_conn, view_name: str) -> None:
    """Each legacy raw_* view exists in `public` and is a VIEW (not a table)."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_type
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (view_name,),
        )
        row = cur.fetchone()
    assert row is not None, f"public.{view_name} does not exist"
    assert row[0] == "VIEW", f"public.{view_name} is {row[0]}, expected VIEW"


@pytest.mark.parametrize("view_name,expected_cols", sorted(LEGACY_VIEWS.items()))
def test_view_projects_required_columns(
    db_conn, view_name: str, expected_cols: set[str]
) -> None:
    """Each view projects the columns the legacy router code expects."""
    actual_cols = _columns_of(db_conn, view_name)
    missing = expected_cols - actual_cols
    assert not missing, (
        f"public.{view_name} is missing {sorted(missing)} "
        f"(present columns: {sorted(actual_cols)})"
    )


# --------------------------------------------------------------------------- #
# 2. Trivial SELECT — view is queryable
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("view_name", sorted(LEGACY_VIEWS.keys()))
def test_view_is_queryable(db_conn, view_name: str) -> None:
    """`SELECT count(*)` on each legacy view succeeds."""
    with db_conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM public.{view_name}")
        row = cur.fetchone()
    assert row is not None
    assert row[0] >= 0  # zero is a valid value (empty DB)


# --------------------------------------------------------------------------- #
# 3. data_source values cover the expected sources
# --------------------------------------------------------------------------- #


def test_raw_vitalsigns_data_source_values(db_conn) -> None:
    """raw_vitalsigns synthesises data_source = {'app1','portal'}."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT DISTINCT data_source FROM public.raw_vitalsigns")
        seen = {r[0] for r in cur.fetchall()}
    # If the underlying tables are empty, `seen` is empty — that's OK; we
    # only require that any value present is one of the expected literals.
    expected = {"app1", "portal"}
    assert seen.issubset(expected), f"unexpected data_source values: {seen - expected}"


def test_raw_homehealth_data_source_values(db_conn) -> None:
    """raw_homehealth synthesises data_source ∈ {'app1','portal','app2'}."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT DISTINCT data_source FROM public.raw_homehealth")
        seen = {r[0] for r in cur.fetchall()}
    expected = {"app1", "portal", "app2"}
    assert seen.issubset(expected), f"unexpected data_source values: {seen - expected}"


# --------------------------------------------------------------------------- #
# 4. The exact legacy queries that summary.py runs (audit + safe_provinces)
# --------------------------------------------------------------------------- #


def test_legacy_audit_query_runs(db_conn) -> None:
    """The /summary/overview audit query — the one that tripped the original
    UndefinedTable error — now executes without exception.
    """
    with db_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE v.data_source IN ('portal','app1')
                                  AND v.cancel_status = 1)
                AS vital_cancelled,
              COUNT(*) FILTER (WHERE v.data_source IN ('portal','app1')
                                  AND v.cancel_status IS DISTINCT FROM 1)
                AS vital_after_cancel
            FROM raw_vitalsigns v
            """
        )
        row = cur.fetchone()
    assert row is not None
    assert row["vital_cancelled"] is not None
    assert row["vital_after_cancel"] is not None


def test_legacy_safe_provinces_join(db_conn) -> None:
    """The /summary/non-bangkok-overview safe_provinces JOIN — exercises both
    raw_vitalsigns and raw_homevisit views together.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT hv.home_province AS pc
            FROM raw_vitalsigns v
            JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
            WHERE v.cancel_status IS DISTINCT FROM 1
              AND hv.home_province IS NOT NULL
              AND hv.home_province <> 10
            GROUP BY hv.home_province
            HAVING COUNT(DISTINCT v.patient_id) >= 5
            """
        )
        rows = cur.fetchall()
    # Result content depends on test data — we only require the query to run.
    assert isinstance(rows, list)


def test_legacy_lifestyle_join_runs(db_conn) -> None:
    """The /summary/non-bangkok-overview lifestyle query — exercises
    raw_homehealth alongside the other two views.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(DISTINCT v.patient_id) FILTER (WHERE h.exercise = 0)
                AS no_exercise_count,
              COUNT(DISTINCT v.patient_id) FILTER (WHERE h.exercise IS NOT NULL)
                AS exercise_answered
            FROM raw_vitalsigns v
            JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
            LEFT JOIN raw_homehealth h ON h.patient_id = v.patient_id
              AND h.cancel_status IS DISTINCT FROM 1
            WHERE v.cancel_status IS DISTINCT FROM 1
            """
        )
        row = cur.fetchone()
    assert row is not None


def test_legacy_lab_join_runs(db_conn) -> None:
    """The /summary/non-bangkok-overview lab query — exercises raw_lab_results."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(DISTINCT l.patient_id) AS total_lab_patients,
              AVG(l.hemoglobin)   AS avg_hemoglobin,
              AVG(l.fbs)          AS avg_fbs,
              AVG(l.cholesterol)  AS avg_cholesterol,
              AVG(l.creatinine)   AS avg_creatinine,
              AVG(l.egfr)         AS avg_egfr
            FROM raw_vitalsigns v
            JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
            JOIN raw_lab_results l ON l.patient_id = v.patient_id
              AND l.cancel_status IS DISTINCT FROM 1
            WHERE v.cancel_status IS DISTINCT FROM 1
            """
        )
        row = cur.fetchone()
    assert row is not None


# --------------------------------------------------------------------------- #
# 5. Migration is idempotent (CREATE OR REPLACE)
# --------------------------------------------------------------------------- #


def test_migration_is_idempotent(db_conn) -> None:
    """Re-applying the migration must not error."""
    sql_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "migrations",
        "400_compat_raw_views.sql",
    )
    with open(sql_path) as f:
        sql = f.read()
    # Run it again — CREATE OR REPLACE must not fail.
    with db_conn.cursor() as cur:
        cur.execute(sql)
    # Sanity-check: views still queryable after the re-apply.
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.raw_vitalsigns")
        assert cur.fetchone()[0] >= 0
