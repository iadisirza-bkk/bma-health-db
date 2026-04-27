"""Unified screening CTE — single source of truth for the public dashboard.

Per fact/aggregation-base.md "Tier 1 Hero KPI" (revised 2026-04-27),
all three sources use **registered home district** (เขตที่อยู่ตามทะเบียนบ้าน)
as the primary aggregation field — NOT screening location.

Per-source resolution chain:

| Source | Primary (homevisit) | Fallback                                     |
|--------|---------------------|----------------------------------------------|
| Portal | home → current → work (NULLIF 9999) | raw_patients.district_code   |
| App1   | home_district                       | raw_patients.district_code   |
| App2   | home → work (NULLIF 9999)           | raw_patients.district_code   |

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
          -- Portal: hv.home → current → work → raw_patients.district_code
          SELECT 'portal'::text AS source,
                 v.patient_id,
                 COALESCE(
                   NULLIF(hv.home_district,    9999),
                   NULLIF(hv.current_district, 9999),
                   NULLIF(hv.work_district,    9999),
                   CASE WHEN p.district_code BETWEEN '1001' AND '1050'
                        THEN p.district_code::int END
                 ) AS dc_int,
                 v.visit_date::date AS day,
                 v.risk_dm, v.risk_hpt, v.risk_cvd, v.risk_bmi,
                 v.found_dyslipidemia, v.found_stroke
          FROM raw_vitalsigns v
          LEFT JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
          LEFT JOIN raw_patients  p  ON p.id = v.patient_id
          WHERE v.data_source = 'portal'
            AND v.cancel_status IS DISTINCT FROM 1
        """)

    if sources is None or 'app1' in sources:
        legs.append("""
          -- App1: hv.home_district → raw_patients.district_code
          SELECT 'app1'::text AS source,
                 v.patient_id,
                 COALESCE(
                   NULLIF(hv.home_district, 9999),
                   CASE WHEN p.district_code BETWEEN '1001' AND '1050'
                        THEN p.district_code::int END
                 ) AS dc_int,
                 v.visit_date::date AS day,
                 v.risk_dm, v.risk_hpt, v.risk_cvd, v.risk_bmi,
                 v.found_dyslipidemia, v.found_stroke
          FROM raw_vitalsigns v
          LEFT JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
          LEFT JOIN raw_patients  p  ON p.id = v.patient_id
          WHERE v.data_source = 'app1'
            AND v.cancel_status IS DISTINCT FROM 1
        """)

    if sources is None or 'app2' in sources:
        legs.append("""
          -- App2: hv.home → work → raw_patients.district_code
          --   (visit row from raw_homehealth; vitals optional)
          SELECT 'app2'::text AS source,
                 hh.patient_id,
                 COALESCE(
                   NULLIF(hv.home_district, 9999),
                   NULLIF(hv.work_district, 9999),
                   CASE WHEN p.district_code BETWEEN '1001' AND '1050'
                        THEN p.district_code::int END
                 ) AS dc_int,
                 hh.visit_date::date AS day,
                 v.risk_dm, v.risk_hpt, v.risk_cvd, v.risk_bmi,
                 v.found_dyslipidemia, v.found_stroke
          FROM raw_homehealth hh
          LEFT JOIN raw_homevisit hv ON hv.patient_id = hh.patient_id
          LEFT JOIN raw_patients  p  ON p.id = hh.patient_id
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
        # `unified_visits` — canonical visit set with the >30-day dedup rule.
        #
        # Spec (2026-04-27): "ตัวแปรครั้งในโครงการ คือ PID + VSTDATE
        # กันเคสที่กรอกข้อมูลผิดแล้วกรอกใหม่ — PID เดิมซ้ำได้แต่ต้องเกิน 1 เดือน".
        # When the same patient has visits within 30 days, only the first is
        # kept; subsequent ones are treated as data-correction duplicates.
        #
        # Two-pass construction (PostgreSQL evaluates DISTINCT *after* window
        # functions, so we use nested subqueries to force the right order):
        #
        #   1. DISTINCT ON (source, patient_id, day) — collapse JOIN
        #      multiplication caused by patients with multiple homevisit rows.
        #      ORDER BY ..., dc NULLS LAST deterministically picks a BKK dc
        #      over NULL when a single visit has both available.
        #   2. LAG(day) over (source, patient_id) — find the previous visit.
        #   3. WHERE prev_day IS NULL OR (day - prev_day) > 30 — drop
        #      duplicates entered within 30 days of the prior record.
        cte_parts.append("""
unified_visits AS MATERIALIZED (
  SELECT source, patient_id, day, dc, bucket
  FROM (
    SELECT source, patient_id, day, dc, bucket,
           LAG(day) OVER (PARTITION BY source, patient_id ORDER BY day) AS prev_day
    FROM (
      -- ORDER BY dc NULLS LAST: BKK codes ('1001'..'1050') sort before
      -- non-BKK ('1101'+) by text comparison, so when a single visit has
      -- both BKK and non-BKK matches the BKK one wins. NULL (unknown)
      -- ranks last.
      SELECT DISTINCT ON (source, patient_id, day)
             source, patient_id, day, dc, bucket
      FROM unified
      ORDER BY source, patient_id, day, dc NULLS LAST
    ) collapsed
  ) lagged
  WHERE prev_day IS NULL OR (day - prev_day) > 30
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
