"""Unified screening CTE — single source of truth for the public dashboard.

Per fact/aggregation-base.md "Tier 1 Hero KPI" spec, each data source uses
its own primary district field:

| Source  | District field            | Visits source         |
|---------|---------------------------|-----------------------|
| Portal  | vital.DISTRICTBKK         | vital.PID + VSTDATE   |
| App1    | hv.DISTRICT (home)         | vital.PID + VSTDATE   |
| App2    | hv.DISTRICT (home, skip null) | HD = raw_homehealth count |

This CTE UNIONs the three streams so endpoints can simply
`SELECT ... FROM unified` and the per-source dispatch is invisible.

Columns:
- patient_id           : FK to raw_patients.id
- dc                   : effective district code ('1001'..'1050')
- day                  : visit date (date, not timestamp — used for
                          PID+VSTDATE distinct visit count)
- risk_dm/hpt/cvd/bmi  : NCD risk flags (boolean, NULL when source
                          has no vitalsigns)
- found_dyslipidemia   : boolean
- found_stroke         : boolean
"""
from __future__ import annotations

VALID_SOURCES = ('portal', 'app1', 'app2')


def build_unified_cte(sources: list[str] | None = None) -> str:
    """Build the UNIFIED CTE, optionally filtered to a subset of data sources.

    Each UNION leg carries a literal `source` column so callers can also
    filter post-CTE if needed. Passing `sources=None` (default) returns all
    three legs.

    Caller responsibility: validate `sources` against VALID_SOURCES — strings
    are interpolated directly (not parameterised) because they're literals.
    """
    legs = []

    if sources is None or 'portal' in sources:
        legs.append("""
          -- Portal: vitalsigns + vital.district_code
          SELECT 'portal'::text AS source,
                 v.patient_id,
                 v.district_code AS dc,
                 v.visit_date::date AS day,
                 v.risk_dm, v.risk_hpt, v.risk_cvd, v.risk_bmi,
                 v.found_dyslipidemia, v.found_stroke
          FROM raw_vitalsigns v
          WHERE v.data_source = 'portal'
            AND v.cancel_status IS DISTINCT FROM 1
            AND v.district_code BETWEEN '1001' AND '1050'
        """)

    if sources is None or 'app1' in sources:
        legs.append("""
          -- App1: vitalsigns + homevisit.home_district
          SELECT 'app1'::text AS source,
                 v.patient_id,
                 hv.home_district::text AS dc,
                 v.visit_date::date AS day,
                 v.risk_dm, v.risk_hpt, v.risk_cvd, v.risk_bmi,
                 v.found_dyslipidemia, v.found_stroke
          FROM raw_vitalsigns v
          JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
          WHERE v.data_source = 'app1'
            AND v.cancel_status IS DISTINCT FROM 1
            AND hv.home_district BETWEEN 1001 AND 1050
        """)

    if sources is None or 'app2' in sources:
        legs.append("""
          -- App2: homehealth (HD) + home_district (skip null)
          SELECT 'app2'::text AS source,
                 hh.patient_id,
                 hv.home_district::text AS dc,
                 hh.visit_date::date AS day,
                 v.risk_dm, v.risk_hpt, v.risk_cvd, v.risk_bmi,
                 v.found_dyslipidemia, v.found_stroke
          FROM raw_homehealth hh
          JOIN raw_homevisit hv ON hv.patient_id = hh.patient_id
          LEFT JOIN raw_vitalsigns v ON v.patient_id = hh.patient_id
            AND v.data_source = 'app2'
            AND v.cancel_status IS DISTINCT FROM 1
          WHERE hh.data_source = 'app2'
            AND hh.cancel_status IS DISTINCT FROM 1
            AND hv.home_district BETWEEN 1001 AND 1050
        """)

    if not legs:
        # Empty selection — return an empty result set with the right shape
        legs.append("""
          SELECT NULL::text AS source, NULL::int AS patient_id, NULL::text AS dc,
                 NULL::date AS day, NULL::bool AS risk_dm, NULL::bool AS risk_hpt,
                 NULL::bool AS risk_cvd, NULL::bool AS risk_bmi,
                 NULL::bool AS found_dyslipidemia, NULL::bool AS found_stroke
          WHERE FALSE
        """)

    return "WITH unified AS (\n" + "\nUNION ALL\n".join(legs) + "\n)"


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
