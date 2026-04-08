"""
Database connection pool and query helpers using psycopg2.
Returns query results as lists of dicts. Never exposes PII columns.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Optional, List, Dict
from urllib.parse import urlparse

import psycopg2
import psycopg2.pool
import psycopg2.extras

from config import DATABASE_URL

# --------------------------------------------------------------------------- #
# Connection pool (singleton)
# --------------------------------------------------------------------------- #

_parsed = urlparse(DATABASE_URL)
_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=20,
            host=_parsed.hostname,
            port=_parsed.port or 5432,
            dbname=_parsed.path.lstrip("/"),
            user=_parsed.username,
            password=_parsed.password,
        )
    return _pool


@contextmanager
def get_conn():
    """Yield a connection from the pool; return it when done."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


# --------------------------------------------------------------------------- #
# Query helpers
# --------------------------------------------------------------------------- #

# Columns that must NEVER appear in API results
_PII_COLUMNS = frozenset({"idcard_hash", "patient_id", "staff_code"})


def execute_query(sql: str, params: Optional[tuple] = None) -> List[Dict]:
    """Execute a read-only query and return rows as dicts.

    Automatically strips any PII columns from the result.
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    # Strip PII columns
    return [{k: v for k, v in row.items() if k not in _PII_COLUMNS} for row in rows]


def execute_scalar(sql: str, params: Optional[tuple] = None):
    """Execute a query and return the first column of the first row."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    return row[0] if row else None


def close_pool():
    """Shutdown the connection pool (called on app shutdown)."""
    global _pool
    if _pool and not _pool.closed:
        _pool.closeall()
        _pool = None
