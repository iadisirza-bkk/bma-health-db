"""Repository base class (ADR-01).

Every concrete repository inherits from `Repository` and uses `fetch_all` /
`fetch_one` to talk to PostgreSQL. We piggy-back on the existing connection
pool from `api/database.py` — no second pool is introduced.

The methods are declared `async def` so they fit naturally into the
FastAPI / Service layer; psycopg2 itself is sync, so the body just runs
inline. That's the same pattern used elsewhere in this codebase.
"""
from __future__ import annotations

from abc import ABC
from decimal import Decimal
from typing import Any, Iterable, Optional

import psycopg2.extras

from database import get_conn

# Columns that must NEVER appear in API results (keeps us aligned with the
# scrub list in `database.execute_query`).
_PII_COLUMNS = frozenset({"idcard_hash", "patient_id", "staff_code"})


class QueryNotFound(KeyError):
    """Raised by `MVRepository.run_query` when a `query_id` isn't registered."""


def _clean(value: Any) -> Any:
    """Best-effort row-value coercion for JSON / Pydantic.

    psycopg2 returns NUMERIC as `Decimal`; downstream code (Pydantic v2,
    json.dumps) is happier with `float`. Other types pass through untouched.
    """
    if isinstance(value, Decimal):
        return float(value)
    return value


class Repository(ABC):
    """Base class for SQL-backed data repositories.

    Subclasses run queries through `fetch_all` / `fetch_one`. Both helpers:
      * acquire a reader connection from `database.get_conn()`
      * use `RealDictCursor` so rows come back as dicts
      * strip PII columns defensively
      * coerce `Decimal` → `float`

    Parameters MUST be passed through `params=` (psycopg2 substitutes them
    safely). Never f-string user input into the SQL.
    """

    async def fetch_all(
        self,
        query: str,
        params: Optional[Iterable[Any]] = None,
    ) -> list[dict[str, Any]]:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, tuple(params) if params is not None else None)
                rows = cur.fetchall()
        return [
            {k: _clean(v) for k, v in row.items() if k not in _PII_COLUMNS}
            for row in rows
        ]

    async def fetch_one(
        self,
        query: str,
        params: Optional[Iterable[Any]] = None,
    ) -> Optional[dict[str, Any]]:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, tuple(params) if params is not None else None)
                row = cur.fetchone()
        if row is None:
            return None
        return {k: _clean(v) for k, v in row.items() if k not in _PII_COLUMNS}
