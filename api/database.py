"""
Database connection pool and query helpers using psycopg2.
Returns query results as lists of dicts. Never exposes PII columns.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from typing import Optional, List, Dict
from urllib.parse import urlparse

import os

import psycopg2
import psycopg2.pool
import psycopg2.extras

from config import DATABASE_URL, DATABASE_URL_READER, DATABASE_URL_WRITER

# --------------------------------------------------------------------------- #
# Connection pools — separate for reader (api_user) and writer (etl_user)
# --------------------------------------------------------------------------- #
# Two least-privilege pools (v3, 2026-04-27):
#   - reader pool: api_user (bma_api_reader) — SELECT on public.* only
#   - writer pool: etl_user (bma_etl_writer) — INSERT/UPDATE on private.*
#
# Default `get_conn()` uses the reader pool. Use `get_writer_conn()` from
# admin/ETL paths that need to write to private.*.
#
# In dev (DATABASE_URL_READER == DATABASE_URL_WRITER == DATABASE_URL), both
# pools resolve to the same DSN — backward-compatible.

_pool_reader: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_pool_writer: Optional[psycopg2.pool.ThreadedConnectionPool] = None

DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "5"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "50"))


def _make_pool(dsn: str) -> psycopg2.pool.ThreadedConnectionPool:
    p = urlparse(dsn)
    return psycopg2.pool.ThreadedConnectionPool(
        minconn=DB_POOL_MIN,
        maxconn=DB_POOL_MAX,
        host=p.hostname,
        port=p.port or 5432,
        dbname=p.path.lstrip("/"),
        user=p.username,
        password=p.password,
    )


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Reader pool (api_user). Returns least-privilege connection.
    Most API endpoints query public.* MVs only — this is what they should use.
    """
    global _pool_reader
    if _pool_reader is None or _pool_reader.closed:
        _pool_reader = _make_pool(DATABASE_URL_READER)
    return _pool_reader


def _get_writer_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Writer pool (etl_user). Use ONLY from /admin/* and ETL paths."""
    global _pool_writer
    if _pool_writer is None or _pool_writer.closed:
        _pool_writer = _make_pool(DATABASE_URL_WRITER)
    return _pool_writer


def get_pool_status() -> dict:
    """Return pool stats for /health debugging. Best-effort, never raises."""
    def _stats(p):
        if p is None:
            return {"in_use": 0, "available": 0}
        used = len(getattr(p, "_used", {}))
        free = len(getattr(p, "_pool", []))
        return {"in_use": used, "available": free,
                "saturation_pct": round(100 * used / max(DB_POOL_MAX, 1), 1)}
    try:
        return {
            "min": DB_POOL_MIN, "max": DB_POOL_MAX,
            "reader": _stats(_pool_reader),
            "writer": _stats(_pool_writer),
        }
    except Exception:
        return {"min": DB_POOL_MIN, "max": DB_POOL_MAX, "error": "unavailable"}


@contextmanager
def get_conn():
    """Yield a READER connection (api_user) — SELECT public.* only.
    Default for /api/v2/* endpoints that read MVs.
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


@contextmanager
def get_writer_conn():
    """Yield a WRITER connection (etl_user) — INSERT/UPDATE private.*.
    Use ONLY from admin upload / ETL background threads.
    """
    pool = _get_writer_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


# --------------------------------------------------------------------------- #
# Query helpers
# --------------------------------------------------------------------------- #

# Columns that must NEVER appear in API results.
#
# CRITICAL: pid_encoded historically held base64-encoded plaintext Thai
# national IDs (reversible). Even after the ETL fix that HMAC-SHA256s on
# write, this allowlist is the last line of defense against a future query
# accidentally selecting the column. Keep both raw and hashed identifier
# columns here — never expose either to the API surface.
_PII_COLUMNS = frozenset({
    # Patient identifiers (raw + hashed forms)
    "idcard", "idcard_hash", "pid", "pid_encoded", "pid_hash",
    "patient_id", "staff_code", "hn",
    # Direct contact / locator PII
    "phone", "tel", "telephone", "email", "idline", "lineid",
    "fname", "lname", "efname", "elname", "fullname",
    "haddr", "address", "addr", "discaretel",
})


def execute_query(sql: str, params: Optional[tuple] = None) -> List[Dict]:
    """Execute a read-only query and return rows as dicts.

    Automatically strips any PII columns from the result.
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    # Strip PII columns + convert Decimal to float for JSON serialization
    def _clean(v):
        return float(v) if isinstance(v, Decimal) else v
    return [{k: _clean(v) for k, v in row.items() if k not in _PII_COLUMNS} for row in rows]


def execute_scalar(sql: str, params: Optional[tuple] = None):
    """Execute a query and return the first column of the first row."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    val = row[0] if row else None
    return float(val) if isinstance(val, Decimal) else val


def close_pool():
    """Shutdown the connection pool (called on app shutdown)."""
    global _pool
    if _pool and not _pool.closed:
        _pool.closeall()
        _pool = None
