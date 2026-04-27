"""Materialised-view refresh helper extracted from the legacy v1 ETL.

This is the only piece of `etl/import_csv.py` that survived the v3 cutover —
`api/admin.py` calls it from `/admin/refresh` (manual MV refresh button) and
`/admin/erasure` (refresh views after a PDPA delete). All other v1 ETL logic
has been removed.

The original v1 helper also called `backfill_district_codes()`, which mutated
`raw_vitalsigns` rows. Those rows are empty under v3, so the backfill is a
no-op and is intentionally omitted here.
"""
from __future__ import annotations


def refresh_all_summaries(cur) -> None:
    """REFRESH MATERIALIZED VIEW CONCURRENTLY for every MV in `public`.

    Discovers MVs dynamically via `pg_matviews`, so the helper is
    schema-agnostic and survives migrations that add/remove views.
    """
    cur.execute(
        """
        SELECT matviewname
        FROM pg_matviews
        WHERE schemaname = 'public'
        ORDER BY matviewname
        """
    )
    views = [r[0] for r in cur.fetchall()]
    for v in views:
        cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY public.{v}")
