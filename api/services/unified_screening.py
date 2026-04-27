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

UNIFIED_CTE = """
WITH unified AS (
  -- Portal: vitalsigns + vital.district_code
  SELECT v.patient_id,
         v.district_code AS dc,
         v.visit_date::date AS day,
         v.risk_dm, v.risk_hpt, v.risk_cvd, v.risk_bmi,
         v.found_dyslipidemia, v.found_stroke
  FROM raw_vitalsigns v
  WHERE v.data_source = 'portal'
    AND v.cancel_status IS DISTINCT FROM 1
    AND v.district_code BETWEEN '1001' AND '1050'

  UNION ALL

  -- App1: vitalsigns + homevisit.home_district
  SELECT v.patient_id,
         hv.home_district::text AS dc,
         v.visit_date::date AS day,
         v.risk_dm, v.risk_hpt, v.risk_cvd, v.risk_bmi,
         v.found_dyslipidemia, v.found_stroke
  FROM raw_vitalsigns v
  JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
  WHERE v.data_source = 'app1'
    AND v.cancel_status IS DISTINCT FROM 1
    AND hv.home_district BETWEEN 1001 AND 1050

  UNION ALL

  -- App2: homehealth (HD) + home_district (skip null)
  SELECT hh.patient_id,
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
)
"""
