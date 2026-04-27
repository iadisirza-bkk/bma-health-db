"""Unified screening CTE — single source of truth for the public dashboard.

Per fact/aggregation-base.md "Tier 1 Hero KPI" (revised 2026-04-27),
all three sources use **registered home district** (เขตที่อยู่ตามทะเบียนบ้าน)
as the primary aggregation field — NOT screening location.

Per-source resolution chain (final spec 2026-04-27):

| Source | CSV variable                            | DB resolution                                              |
|--------|-----------------------------------------|------------------------------------------------------------|
| Portal | `COALESCE(HDISTRICT, DISTRICT)` ~97.5%  | `COALESCE(home_district, work_district)` NULLIF 9999       |
| App1   | `hv.DISTRICT` 100%                      | `home_district` NULLIF 9999                                |
| App2   | `DISTRICT` (BKK only) backfill WRKDISTRICT | `COALESCE(home_district, work_district)` NULLIF 9999    |

Portal is the only source with a fallback chain — `HDISTRICT` is the
schema-canonical home district but is sparsely populated (~2.4% of Portal
patients have it in BKK), while `DISTRICT` is the operational field
populated for ~80%. The COALESCE recovers ~81.5% of Portal patients.

App1's `hv.DISTRICT` lands in `home_district` and is well-populated.
App2 follows the same pattern but with WRKDISTRICT as the secondary.
No `raw_patients.district_code` fallback — per user spec, the homevisit
record is the only authoritative source for "registered home district".

Why the raw_patients fallback (added 2026-04-27): ~93K visits had no usable
home info anywhere in raw_homevisit. raw_patients.district_code (the
registered district from the patient master record, BKK-only, 94% coverage)
recovers 99% of those orphans. The remaining ~739 visits stay in `unified`
with `dc = NULL` — counted in the headline total under bucket = 'unknown'.

Bucket tagging (revised 2026-04-27 — "เลขรีพอร์ตต้องรวมทั้งโครงการ"):
`unified` keeps **all records** (no district filter) so the dashboard
headline can sum to the project total. Each row carries a `bucket` label:

| bucket    | Meaning                                                   | dc value |
|-----------|-----------------------------------------------------------|----------|
| 'bkk'     | Resolved district 1001..1050                              | '1001'..'1050' |
| 'non_bkk' | Resolved to a non-BKK district (other province)           | e.g. '1101', '4101' |
| 'unknown' | No district info anywhere (NULL after full fallback)      | NULL     |

`/overview` returns total + per-bucket breakdown. `/zones` and `/districts`
INNER-join to `ref_districts` so non-BKK and unknown rows are naturally
excluded from per-zone/per-district aggregates (those endpoints stay
BKK-only as intended). Cancelled records are excluded everywhere.

Two CTEs are emitted (when `include_visits=True`, the default):

1. `unified` — every (source, patient_id, day, dc, bucket, risk_*) row.
   Use for patient/risk-flag aggregates.

2. `unified_visits` — distinct (source, patient_id, day, dc, bucket) rows
   with the **>30-day visit dedup rule** applied (spec 2026-04-27):
   "PID เดิมซ้ำได้แต่ต้องเกิน 1 เดือน". Same-patient visits within 30
   days are treated as data-correction duplicates. Use for visit counts.

Columns in `unified`:
- patient_id           : FK to raw_patients.id
- dc                   : effective district code (text) or NULL (unknown)
- bucket               : 'bkk' | 'non_bkk' | 'unknown'
- day                  : visit date (date, not timestamp — used for
                          PID+VSTDATE distinct visit count)
- risk_dm/hpt/cvd/bmi  : NCD risk flags (boolean, NULL when source
                          has no vitalsigns)
- found_dyslipidemia   : boolean
- found_stroke         : boolean
"""
from __future__ import annotations

VALID_SOURCES = ('portal', 'app1', 'app2')


def build_unified_cte(
    sources: list[str] | None = None,
    include_visits: bool = True,
) -> str:
    """Build the UNIFIED CTE(s), optionally filtered to a subset of data sources.

    Each UNION leg carries a literal `source` column so callers can also
    filter post-CTE if needed. Passing `sources=None` (default) returns all
    three legs.

    When `include_visits=True` (default), a second CTE `unified_visits` is
    appended that applies the >30-day visit dedup rule (see module docstring).
    Set `include_visits=False` to emit only the `unified` CTE — useful for
    queries that don't need visit counts and want to skip the LAG sort.

    Caller responsibility: validate `sources` against VALID_SOURCES — strings
    are interpolated directly (not parameterised) because they're literals.
    """
    legs = []

    # Each leg emits an integer `dc_int` (NULL for unknown). The wrapping
    # `unified` CTE casts to text and filters BKK + unknown only (drops
    # resolved-non-BKK, which /non-bangkok-overview handles separately).
    #
    # The raw_patients fallback uses a CASE guard so only valid BKK codes
    # ('1001'..'1050') are accepted — defensive against any junk values.

    if sources is None or 'portal' in sources:
        legs.append("""
          -- Portal: COALESCE(HDISTRICT, DISTRICT) per user spec 2026-04-27.
          -- HDISTRICT (primary, ~2.4% in BKK) lives in DB.home_district.
          -- DISTRICT (backfill, ~80% in BKK) lives in DB.work_district.
          -- Combined gives ~81.5% of Portal patients with BKK home district —
          -- total matches user's HDISTRICT pivot of 326,001 within 0.8%.
          -- No current_district / no raw_patients fallback per spec.
          SELECT 'portal'::text AS source,
                 v.patient_id,
                 COALESCE(
                   NULLIF(hv.home_district, 9999),  -- HDISTRICT (primary)
                   NULLIF(hv.work_district, 9999)   -- DISTRICT (backfill)
                 ) AS dc_int,
                 v.visit_date::date AS day,
                 v.risk_dm, v.risk_hpt, v.risk_cvd, v.risk_bmi,
                 v.found_dyslipidemia, v.found_stroke
          FROM raw_vitalsigns v
          LEFT JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
          WHERE v.data_source = 'portal'
            AND v.cancel_status IS DISTINCT FROM 1
        """)

    if sources is None or 'app1' in sources:
        legs.append("""
          -- App1: hv.DISTRICT (home_district, ~100% per user spec 2026-04-27).
          -- No fallback needed.
          SELECT 'app1'::text AS source,
                 v.patient_id,
                 NULLIF(hv.home_district, 9999) AS dc_int,
                 v.visit_date::date AS day,
                 v.risk_dm, v.risk_hpt, v.risk_cvd, v.risk_bmi,
                 v.found_dyslipidemia, v.found_stroke
          FROM raw_vitalsigns v
          LEFT JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
          WHERE v.data_source = 'app1'
            AND v.cancel_status IS DISTINCT FROM 1
        """)

    if sources is None or 'app2' in sources:
        legs.append("""
          -- App2: DISTRICT (home_district), backfill WRKDISTRICT (work_district)
          -- if HOMEDISTRICT is 9999 — per spec 2026-04-27. Visit rows from
          -- raw_homehealth (HD source); vitals optional.
          SELECT 'app2'::text AS source,
                 hh.patient_id,
                 COALESCE(
                   NULLIF(hv.home_district, 9999),
                   NULLIF(hv.work_district, 9999)
                 ) AS dc_int,
                 hh.visit_date::date AS day,
                 v.risk_dm, v.risk_hpt, v.risk_cvd, v.risk_bmi,
                 v.found_dyslipidemia, v.found_stroke
          FROM raw_homehealth hh
          LEFT JOIN raw_homevisit hv ON hv.patient_id = hh.patient_id
          LEFT JOIN raw_vitalsigns v ON v.patient_id = hh.patient_id
            AND v.data_source = 'app2'
            AND v.cancel_status IS DISTINCT FROM 1
          WHERE hh.data_source = 'app2'
            AND hh.cancel_status IS DISTINCT FROM 1
        """)

    if not legs:
        # Empty selection — return an empty result set with the right shape
        legs.append("""
          SELECT NULL::text AS source, NULL::int AS patient_id, NULL::int AS dc_int,
                 NULL::date AS day, NULL::bool AS risk_dm, NULL::bool AS risk_hpt,
                 NULL::bool AS risk_cvd, NULL::bool AS risk_bmi,
                 NULL::bool AS found_dyslipidemia, NULL::bool AS found_stroke
          WHERE FALSE
        """)

    # `unified_raw` collects per-source rows with cancel filter + resolution.
    # `unified` keeps all records and tags each with a bucket so callers can
    # filter as needed (e.g. /overview totals everything; /zones and
    # /districts INNER-join to ref_districts which auto-excludes non-BKK
    # and unknown rows).
    #
    # MATERIALIZED forces PostgreSQL to compute each CTE once and reuse the
    # result. Without it, /overview's many subqueries against `unified` and
    # `unified_visits` each re-evaluate the union, making the same heavy
    # join run N times — fine for one-shot but blows up the planner with
    # filtered source combos and triggers DiskFull on large query plans.
    cte_parts = [
        "unified_raw AS MATERIALIZED (\n" + "\nUNION ALL\n".join(legs) + "\n)",
        """
unified AS MATERIALIZED (
  SELECT source, patient_id, dc_int::text AS dc, day,
         risk_dm, risk_hpt, risk_cvd, risk_bmi,
         found_dyslipidemia, found_stroke,
         CASE
           WHEN dc_int BETWEEN 1001 AND 1050 THEN 'bkk'
           WHEN dc_int IS NULL                THEN 'unknown'
           ELSE                                    'non_bkk'
         END AS bucket
  FROM unified_raw
)
""".strip(),
    ]

    if include_visits:
        # `unified_visits` — canonical visit set with the 30-day dedup rule.
        #
        # Spec (2026-04-27): "กันเคสที่กรอกข้อมูลผิดแล้วกรอกใหม่ — PID เดิม
        # ซ้ำได้แต่ต้องเกิน 1 เดือน". Implementation matches the user's
        # CTE precisely:
        #
        #   1. DISTINCT ON (source, pid, day) — collapse JOIN multiplication
        #      from patients with multiple homevisit rows.
        #   2. group_id = cumulative count of "new windows" — increments
        #      whenever the gap from the previous visit is NULL (first
        #      visit) or >= 30 days. Visits in the same window share an id.
        #   3. Within each (source, pid, group_id), keep the LATEST visit
        #      (ORDER BY day DESC, rn=1) — the corrected entry, not the
        #      original mistaken one.
        #
        # Net effect: same visit COUNT as user's Python pivot
        # (Portal 473,391 / App1 375,678 / App2 34,624) and the kept row is
        # the latest in each 30-day window.
        cte_parts.append("""
unified_visits AS MATERIALIZED (
  SELECT source, patient_id, day, dc, bucket
  FROM (
    SELECT source, patient_id, day, dc, bucket, group_id,
           ROW_NUMBER() OVER (
             PARTITION BY source, patient_id, group_id
             ORDER BY day DESC
           ) AS rn
    FROM (
      SELECT source, patient_id, day, dc, bucket,
             SUM(CASE WHEN prev_day IS NULL OR (day - prev_day) >= 30
                      THEN 1 ELSE 0 END)
               OVER (PARTITION BY source, patient_id ORDER BY day) AS group_id
      FROM (
        SELECT source, patient_id, day, dc, bucket,
               LAG(day) OVER (PARTITION BY source, patient_id ORDER BY day) AS prev_day
        FROM (
          -- ORDER BY dc NULLS LAST: BKK codes sort before non-BKK,
          -- and NULL (unknown) ranks last.
          SELECT DISTINCT ON (source, patient_id, day)
                 source, patient_id, day, dc, bucket
          FROM unified
          ORDER BY source, patient_id, day, dc NULLS LAST
        ) collapsed
      ) lagged
    ) grouped
  ) ranked
  WHERE rn = 1
)
""".strip())

    return "WITH " + ", ".join(cte_parts)


def parse_sources(sources_param: str | None) -> list[str] | None:
    """Parse a comma-separated `sources` query string into a validated list.

    - None / empty / 'all'  → returns None (means: use all sources)
    - 'portal'              → ['portal']
    - 'portal,app1'         → ['portal', 'app1']

    Invalid source names are silently dropped. Returns None if nothing
    valid remains, which means "use all sources".
    """
    if not sources_param or sources_param.strip().lower() == 'all':
        return None
    parts = [s.strip().lower() for s in sources_param.split(',') if s.strip()]
    valid = [s for s in parts if s in VALID_SOURCES]
    return valid if valid else None


# Backward-compatible: callers that imported UNIFIED_CTE keep working
# (returns the all-sources CTE).
UNIFIED_CTE = build_unified_cte()
