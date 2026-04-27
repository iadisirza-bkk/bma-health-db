"""Unified screening CTE — v3 thin wrapper over public.mv_visit_resolved.

Schema v3 (2026-04-27): all dashboard endpoints now read from
`public.mv_visit_resolved` (a materialized view that pre-computes
home_district resolution + bucket + risk flag pivot from the EAV core).

The MV is refreshed nightly by pg_cron (and after each ETL upload),
so reads are O(1) regardless of the underlying private.* table sizes.

Why a CTE wrapper at all:
- Backwards-compatible API: existing /overview /zones /districts queries
  still call build_unified_cte() and SELECT FROM unified.
- The CTE just SELECTs from the MV — no per-source UNION ALL.
- Source filter (?sources=portal,app1) becomes a simple WHERE.

Schema of the resulting `unified` view:
  visit_id, patient_id, source (= source_code), day (= visit_date),
  dc (= home_district_code), bucket,
  risk_dm, risk_hpt, risk_cvd, risk_bmi, risk_stroke,
  found_dm, found_hpt, found_cvd, found_obesity,
  found_dyslipidemia, found_stroke
"""
from __future__ import annotations

VALID_SOURCES = ('portal', 'app1', 'app2')


def build_unified_cte(
    sources: list[str] | None = None,
    include_visits: bool = True,
) -> str:
    """Generate WITH unified [, unified_visits] AS (...) SQL.

    `sources`: optional list to filter — None means all.
    `include_visits`: if True, emit unified_visits with >30-day dedup.
    """
    where_clauses = []
    if sources:
        valid = [s for s in sources if s in VALID_SOURCES]
        if valid:
            quoted = "', '".join(valid)
            where_clauses.append(f"source_code IN ('{quoted}')")
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    cte_parts = [f"""
unified AS (
  SELECT
    visit_id,
    patient_id,
    source_code AS source,
    visit_date  AS day,
    home_district_code AS dc,
    bucket,
    risk_dm, risk_hpt, risk_cvd, risk_bmi, risk_stroke,
    found_dm, found_hpt, found_cvd, found_obesity,
    found_dyslipidemia, found_stroke
  FROM public.mv_visit_resolved
  {where_sql}
)
""".strip()]

    if include_visits:
        # 30-day retry dedup: group same patient's visits within 30-day windows,
        # keep the latest visit (rn=1 ORDER BY day DESC).
        cte_parts.append("""
unified_visits AS (
  SELECT visit_id, source, patient_id, day, dc, bucket
  FROM (
    SELECT visit_id, source, patient_id, day, dc, bucket, group_id,
           ROW_NUMBER() OVER (
             PARTITION BY source, patient_id, group_id ORDER BY day DESC
           ) AS rn
    FROM (
      SELECT visit_id, source, patient_id, day, dc, bucket,
             SUM(CASE WHEN prev_day IS NULL OR (day - prev_day) >= 30
                      THEN 1 ELSE 0 END)
               OVER (PARTITION BY source, patient_id ORDER BY day) AS group_id
      FROM (
        SELECT visit_id, source, patient_id, day, dc, bucket,
               LAG(day) OVER (PARTITION BY source, patient_id ORDER BY day) AS prev_day
        FROM unified
      ) lagged
    ) grouped
  ) ranked
  WHERE rn = 1
)
""".strip())

    return "WITH " + ", ".join(cte_parts)


def parse_sources(sources_param: str | None) -> list[str] | None:
    """Parse a comma-separated `sources` query string into validated list."""
    if not sources_param or sources_param.strip().lower() == 'all':
        return None
    parts = [s.strip().lower() for s in sources_param.split(',') if s.strip()]
    valid = [s for s in parts if s in VALID_SOURCES]
    return valid if valid else None


# Backward-compat
UNIFIED_CTE = build_unified_cte()
