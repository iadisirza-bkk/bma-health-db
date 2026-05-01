# API Audit — Sprint 1, Item #6 (Endpoint cleanup)

**Generated:** 2026-05-01
**Scope:** all 24 routers in `/api/routers/` (excluding `__init__.py`)
**Method:** static analysis, decorator enumeration, MV/table cross-reference against
`db/migrations/100…114_*.sql`. **No code changed.**

---

## Reference: schema state (post-migration 114)

| Object | Status | Notes |
|---|---|---|
| `summary_district_disease` | **VIEW** (105) | live; over `mv_visit_resolved + ref_districts` |
| `summary_district_lab`     | **VIEW** (105) | live; computes from `private.lab_event/lab_measurement` |
| `summary_district_mental`  | **VIEW** (105) | live; over `mv_visit_resolved + private.visit_measurement` |
| `summary_district_demographics` | **VIEW** (105) | live; many cols are placeholder `0::bigint` |
| `summary_facility`         | **VIEW** (105) | live; columns `risk_dm`/`risk_hpt`/`found_obesity`/`lab_completed`(=0)/`first_screening`/`last_screening` |
| `summary_district_risk_factors` | **EMPTY STUB VIEW** (105) | `WHERE FALSE` — returns 0 rows |
| `summary_disease_age_sex`  | **MV** (112) | live |
| `summary_lab_disease_cross` | **MV** (112) | live |
| `summary_comorbidity`      | **MV** (112) | live |
| `summary_disease_control`  | **MV** (112) | live |
| `mv_summary_districts`/`mv_summary_zones`/`mv_summary_global` | **MV** (106) | live |
| `mv_summary_lab` / `mv_summary_mental` | **MV** (110) | live |
| `mv_visit_resolved`        | **MV** (104) | live |
| `mv_ncd_diagnostic_report` / `mv_ncd_diagnostic_zone` | **MV** (113/114) | live |
| **`summary_bmi_waist`**    | **DROPPED** (105, never recreated) | querying it ⇒ 500 |
| **`summary_screening_tests`** | **DROPPED** (105, never recreated) | not actually queried by any router (admin filename clash only) |
| **`summary_chronic_history` / `summary_family_history`** | **DROPPED** (105) | not queried |
| `raw_vitalsigns` / `raw_homehealth` / `raw_homevisit` / `raw_lab_results` / `raw_lab_extended` / `raw_visits` / `raw_patients` | tables exist (001) but **never written by v3 ETL** | queries succeed but return 0 rows |

---

## Summary counts

| Classification | Count |
|---|---:|
| Total endpoints (path × method, counting stacked GET/POST as 2) | **150** |
| `OK_AGGREGATE` | 115 |
| `LIKELY_EMPTY` | 21 |
| `LIKELY_500` | 4 |
| `INDIVIDUAL_LEAK` | 0 |
| `UNCERTAIN` | 10 |

(Top-10 priority list and "Per-router detail" tables share rows — 10 + 140 here, but the priority list is repeats not new endpoints. The 150 total above counts each route once.)

> **Note on `INDIVIDUAL_LEAK`.** No router returns row-level patient PII
> (`pid`, `fname`, `lname`, `birthdate`, `phone`, `email`, free-text remarks).
> All handlers either aggregate via `COUNT(DISTINCT patient_id)` or use the
> JSON-cache aggregate (`load_district_data()`). The closest is
> `gis.py /facilities*` exposing **facility** address/telephone — that is
> facility metadata, not patient PII, so it is classified `OK_AGGREGATE`
> with a note. `research.py /individual-data` *gates* on IRB approval and
> only returns a metadata stub even with the gate open.

---

## Top priority for Day 6

(zero `INDIVIDUAL_LEAK` rows ⇒ list begins with `LIKELY_500`s)

| # | Path | Method | Why |
|---|---|---|---|
| 1 | `/api/v2/promotion/bmi-distribution` | GET | reads `summary_bmi_waist` (DROPPED 105). Either rebuild MV from `mv_visit_resolved.bmi_*` or drop endpoint. |
| 2 | `/api/v2/promotion/waist-risk-analysis` | GET | same — `summary_bmi_waist` DROPPED. |
| 3 | `/api/v2/public/district-health-card` | GET | `JOIN summary_bmi_waist` — will 500 even though `summary_district_lab` part works. Rewrite to use `mv_summary_lab` and drop the BMI piece, OR rebuild `summary_bmi_waist`. |
| 4 | `/api/v2/research/correlation-matrix` | GET | `LEFT JOIN summary_bmi_waist b ON … AND b.sex='all'` — view gone, query 500s. Drop the `avg_bmi` column or join `mv_visit_resolved` directly. |
| 5 | `/api/v2/districts/{dcode}/disease/{disease_key}` | GET | risk-factor breakdown reads `summary_district_risk_factors` (now empty stub). Currently returns empty `risk_factor_breakdown: []` — not 500, but visibly broken. Replace with `summary_disease_age_sex` per-district slice. |
| 6 | `/api/v2/promotion/risk-factor-profile` | GET | `summary_district_risk_factors` empty-stub: returns 0 rows after k-anon. Drop or re-route to `mv_visit_resolved` + `private.visit_measurement` for `smoking`/`exercise`. |
| 7 | `/api/v2/promotion/exercise-frequency` | GET | same empty-stub problem (`summary_district_risk_factors`). |
| 8 | `/api/v2/promotion/behavior-disease-correlation` | GET | smoking/exercise branch hits the empty stub; alcohol branch hits empty `raw_homehealth`. Rebuild via EAV (`vd.variable_key='smoking_status'` etc.) on `private.visit_measurement`. |
| 9 | `/api/v2/disease-control/treatment-compliance` | GET | reads `raw_homehealth.{dm_treatment,…}` columns, table empty under v3. Defer until a `treatment_status` variable_key is ingested into `private.visit_measurement`. |
| 10 | `/api/v2/disease-control/referral-outcome` | GET | reads `raw_vitalsigns.referral_type`; table empty under v3. Defer or migrate to `private.visit_event`/`visit_measurement`. |

Beyond the top-10, every other `LIKELY_EMPTY` (especially `trends/*`, `epidemiology/{incidence-rate,outbreak-detection}`, `executive/yoy-comparison`, `kpi/progress-tracker`) is a candidate to **re-route** to `mv_visit_resolved` (which has `visit_date` + risk/found columns) so the dashboards stop showing empty time-series.

---

## Per-router detail

### `admin_api.py`  (prefix: `/api/admin`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/upload-screening` | POST | OK_AGGREGATE | writes JSON cache file; admin-gated |
| `/data-status` | GET | OK_AGGREGATE | reads JSON cache only |
| `/invalidate-cache` | POST | OK_AGGREGATE | cache mgmt only |
| `/audit-log` | GET | OK_AGGREGATE | reads `audit.log` file (no DB) |
| `/audit-log/verify` | GET | OK_AGGREGATE | hashes JSONL audit chain |
| `/upload-excel` | POST | OK_AGGREGATE | writes JSON cache file |
| `/excel-template` | GET | OK_AGGREGATE | static template file |

### `chat.py` (prefix: `/api/health`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/chat` | GET | UNCERTAIN | LLM passthrough; depends on what tools the orchestrator queries |
| `/chat` | POST | UNCERTAIN | same |
| `/chat/stream` | GET | UNCERTAIN | same (SSE) |
| `/chat/stream` | POST | UNCERTAIN | same (SSE) |

### `dashboard_v1.py` (prefix: `/api/v1/dashboard`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/governor` | GET | OK_AGGREGATE | uses JSON cache only |
| `/district/{dcode}` | GET | OK_AGGREGATE | uses JSON cache only |
| `/medical` | GET | OK_AGGREGATE | uses JSON cache only |

### `disease_control.py` (prefix: `/api/v2/disease-control`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/screening-coverage` | GET | OK_AGGREGATE | `summary_district_disease` (VIEW, live) |
| `/ncd-cascade` | GET | OK_AGGREGATE | `summary_district_disease` (VIEW, live) |
| `/repeat-screening` | GET | LIKELY_EMPTY | `raw_vitalsigns` empty under v3 |
| `/progression` | GET | LIKELY_EMPTY | `raw_vitalsigns` empty |
| `/referral-outcome` | GET | LIKELY_EMPTY | `raw_vitalsigns.referral_type` empty (Top-10 #10) |
| `/treatment-compliance` | GET | LIKELY_EMPTY | `raw_homehealth.*_treatment` empty (Top-10 #9) |

### `districts.py` (prefix: `/api/v2`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/districts` | GET | OK_AGGREGATE | `mv_summary_districts` |
| `/districts/{dcode}` | GET | OK_AGGREGATE | joins multiple summary VIEWs (all live) |
| `/districts/{dcode}/disease/{disease_key}` | GET | LIKELY_EMPTY | risk-factor part reads empty-stub `summary_district_risk_factors` (Top-10 #5) |

### `epidemiology.py` (prefix: `/api/v2/epidemiology`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/age-group-prevalence` | GET | OK_AGGREGATE | `summary_disease_age_sex` MV (live since 112) |
| `/disease-lab-crosstab` | GET | OK_AGGREGATE | `summary_lab_disease_cross` MV (live since 112) |
| `/multi-disease-matrix` | GET | OK_AGGREGATE | `summary_comorbidity` MV (live since 112) |
| `/age-pyramid` | GET | OK_AGGREGATE | `summary_disease_age_sex` MV |
| `/incidence-rate` | GET | LIKELY_EMPTY | `raw_vitalsigns` empty — rewrite to `mv_visit_resolved` for time-series |
| `/outbreak-detection` | GET | LIKELY_EMPTY | same |

### `executive.py` (prefix: `/api/v2/executive`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/headline-kpi` | GET | OK_AGGREGATE | `unified` CTE + `summary_district_disease` |
| `/yoy-comparison` | GET | LIKELY_EMPTY | `raw_vitalsigns` empty — rewrite via `mv_visit_resolved.visit_date` |
| `/campaign-impact` | GET | OK_AGGREGATE | hard-coded "data_available: false" stub |
| `/media-brief` | GET | OK_AGGREGATE | `summary_district_disease` (VIEW) |
| `/alert` | GET | OK_AGGREGATE | `summary_district_disease` (VIEW) |

### `export.py` (prefix: `/api/v2/export`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/district/{dcode}/pdf` | GET | OK_AGGREGATE | JSON cache only |
| `/district/{dcode}/pdf/json` | GET | OK_AGGREGATE | JSON cache only |
| `/district/{dcode}/excel` | GET | OK_AGGREGATE | JSON cache only |
| `/zone/{zone_code}/excel` | GET | OK_AGGREGATE | JSON cache only |
| `/city/excel` | GET | OK_AGGREGATE | JSON cache only |
| `/rankings/{disease}/excel` | GET | OK_AGGREGATE | JSON cache only |

### `facility.py` (prefix: `/api/v2/facility`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/performance` | GET | OK_AGGREGATE | `summary_facility` (VIEW, live) |
| `/workload` | GET | OK_AGGREGATE | `summary_facility` (VIEW) |
| `/screening-yield-rank` | GET | OK_AGGREGATE | `summary_facility` (VIEW) |
| `/staff-performance` | GET | OK_AGGREGATE | hard-coded "PDPA: not exposed" stub |
| `/capacity-planning` | GET | OK_AGGREGATE | `summary_district_disease` |
| `/comparison` | GET | OK_AGGREGATE | `summary_facility` (VIEW) |

### `factors.py` (prefix: `/api/v2/factors`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/sex` | GET | OK_AGGREGATE | JSON cache + simulated modifiers |
| `/age-group` | GET | OK_AGGREGATE | JSON cache + simulated modifiers |
| `/occupation` | GET | OK_AGGREGATE | JSON cache + simulated modifiers |
| `/zone` | GET | OK_AGGREGATE | JSON cache only |
| `/behavior/smoking` | GET | OK_AGGREGATE | JSON cache + simulated modifiers |
| `/behavior/alcohol` | GET | OK_AGGREGATE | JSON cache + simulated modifiers |
| `/behavior/exercise` | GET | OK_AGGREGATE | JSON cache + simulated modifiers |
| `/cross-tabulation` | GET | OK_AGGREGATE | JSON cache + simulated modifiers |

### `gis.py` (prefix: `/api/v2/gis`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/facilities` | GET | OK_AGGREGATE | `ref_facilities` (telephone/address are facility metadata, not patient PII) |
| `/facilities/{code}` | GET | OK_AGGREGATE | same + `summary_facility` |
| `/facilities/zone/{zone_code}` | GET | OK_AGGREGATE | same |
| `/facilities/district/{district_code}` | GET | OK_AGGREGATE | same |
| `/facility-types` | GET | OK_AGGREGATE | `ref_facilities` GROUP BY ct_name |
| `/heatmap/disease/{disease_key}` | GET | OK_AGGREGATE | `summary_district_disease` |
| `/pm25/current` | GET | OK_AGGREGATE | external ArcGIS proxy |
| `/pm25/health` | GET | OK_AGGREGATE | external ArcGIS proxy diagnostics |
| `/boundaries/districts` | GET | OK_AGGREGATE | external ArcGIS proxy |
| `/overlay/disease-environment` | GET | OK_AGGREGATE | `summary_district_disease` + ArcGIS |
| `/pm25/zones` | GET | OK_AGGREGATE | `pm25_daily` table |
| `/pm25/districts` | GET | OK_AGGREGATE | `pm25_daily` |
| `/pm25/monthly` | GET | OK_AGGREGATE | `pm25_daily` |

### `kpi.py` (prefix: `/api/v2/kpi`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/moph-targets` | GET | OK_AGGREGATE | `summary_district_disease` |
| `/screening-yield` | GET | OK_AGGREGATE | `summary_district_disease` |
| `/control-rates` | GET | OK_AGGREGATE | `summary_disease_control` MV (live since 112) |
| `/zone-comparison` | GET | OK_AGGREGATE | `summary_district_disease` |
| `/progress-tracker` | GET | LIKELY_EMPTY | `raw_vitalsigns` empty; rewrite to `mv_visit_resolved` |
| `/benchmark` | GET | OK_AGGREGATE | `summary_district_disease` + national constants |
| `/gap-analysis` | GET | OK_AGGREGATE | `summary_district_disease` |

### `monitoring.py` (prefix: `/api/v2/monitoring`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/data-quality` | GET | LIKELY_EMPTY | `raw_*` tables empty under v3 (numbers correct: 0 rows) |
| `/cleansing-report` | GET | LIKELY_EMPTY | same; "blocked_fields" will list everything |
| `/table-stats` | GET | OK_AGGREGATE | `pg_stat_user_tables` + `summary_district_disease.refreshed_at` (works via VIEW NOW()) |
| `/etl-status` | GET | OK_AGGREGATE | `import_history` table |
| `/api-performance` | GET | OK_AGGREGATE | hard-coded constants |
| `/audit-log` | GET | OK_AGGREGATE | `import_history` |
| `/cache-stats` | GET | OK_AGGREGATE | Redis stats |

### `promotion.py` (prefix: `/api/v2/promotion`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/bmi-distribution` | GET | **LIKELY_500** | `summary_bmi_waist` DROPPED (105, never recreated) — Top-10 #1 |
| `/behavior-disease-correlation` | GET | LIKELY_EMPTY | `summary_district_risk_factors` empty stub + `raw_homehealth` empty (Top-10 #8) |
| `/risk-factor-profile` | GET | LIKELY_EMPTY | `summary_district_risk_factors` empty stub (Top-10 #6) |
| `/exercise-frequency` | GET | LIKELY_EMPTY | same (Top-10 #7) |
| `/waist-risk-analysis` | GET | **LIKELY_500** | `summary_bmi_waist` DROPPED — Top-10 #2 |
| `/diet-disease-correlation` | GET | LIKELY_EMPTY | `raw_homehealth` empty under v3 |

### `public.py` (prefix: `/api/v2/public`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/district-summary` | GET | OK_AGGREGATE | `summary_district_disease` |
| `/screening-locations` | GET | OK_AGGREGATE | `ref_facilities` |
| `/health-tips` | GET | OK_AGGREGATE | hard-coded dict |
| `/service-satisfaction` | GET | OK_AGGREGATE | hard-coded "data_available: false" stub |
| `/complaint-status` | GET | OK_AGGREGATE | hard-coded stub |
| `/open-data` | GET | OK_AGGREGATE | `summary_district_disease` |
| `/district-health-card` | GET | **LIKELY_500** | joins `summary_bmi_waist` (DROPPED) — Top-10 #3 |

### `reports.py` (prefix: `/api/reports`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/comprehensive/{lang}` | GET | OK_AGGREGATE | PDF file passthrough |
| `/executive/{lang}` | GET | OK_AGGREGATE | PDF file passthrough |
| `/disease/{disease}` | GET | OK_AGGREGATE | PDF file passthrough |
| `/adaptive/{filename}` | GET | OK_AGGREGATE | PDF file passthrough |
| `/zone/{zone_code}/{lang}` | GET | OK_AGGREGATE | PDF generator |
| `/msd/{lang}` | GET | OK_AGGREGATE | PDF generator |
| `/repeat-screening/{lang}` | GET | OK_AGGREGATE | PDF generator |
| `/public/{lang}` | GET | OK_AGGREGATE | PDF file passthrough |
| `/catalog` | GET | OK_AGGREGATE | filesystem stat only |
| `/dashboard` | GET | OK_AGGREGATE | filesystem stat + scheduler |
| `/scheduler-status` | GET | OK_AGGREGATE | scheduler info |
| `/generation-progress` | GET | OK_AGGREGATE | in-memory progress |
| `/status` | GET | OK_AGGREGATE | filesystem stat |
| `/generate` | POST | OK_AGGREGATE | background task |
| `/generate/{lang}` | POST | OK_AGGREGATE | background task |
| `/generate/{lang}/{report_type}` | POST | OK_AGGREGATE | background task |
| `/invalidate` | POST | OK_AGGREGATE | cache mgmt |

### `research.py` (prefix: `/api/v2/research`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/data-dictionary` | GET | OK_AGGREGATE | `information_schema` |
| `/individual-data` | GET | OK_AGGREGATE | gated on IRB; otherwise reads `raw_patients`/`raw_vitalsigns` (empty) → metadata stub. **Not a leak**, but `LIKELY_EMPTY` for the data-shape numbers. |
| `/statistical-test` | GET | OK_AGGREGATE | `summary_disease_age_sex` MV |
| `/correlation-matrix` | GET | **LIKELY_500** | `LEFT JOIN summary_bmi_waist` DROPPED — Top-10 #4 |
| `/sample-size-calculator` | GET | OK_AGGREGATE | math only + `ref_districts.population` |
| `/export` | GET | OK_AGGREGATE | `summary_district_disease` aggregate |
| `/ncd-diagnostic-report` | GET | OK_AGGREGATE | `mv_ncd_diagnostic_report` (live since 113) |
| `/ncd-diagnostic-by-zone` | GET | OK_AGGREGATE | `mv_ncd_diagnostic_zone` (live since 114) |

### `screening_tests.py` (prefix: `/api/v2/screening-tests`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/summary` | GET | OK_AGGREGATE | JSON cache + deterministic simulation |
| `/district/{dcode}` | GET | OK_AGGREGATE | same |
| `/ekg/summary` | GET | OK_AGGREGATE | same |
| `/chest-xray/summary` | GET | OK_AGGREGATE | same |
| `/blood/summary` | GET | OK_AGGREGATE | same |
| `/retinal/summary` | GET | OK_AGGREGATE | same |

### `search.py` (prefix: `/api/v2/search`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/districts` | GET | OK_AGGREGATE | `summary_district_disease` (VIEW) |

### `statistics_v1.py` (prefix: `/api/stats`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/district/{dcode}` | GET | UNCERTAIN | delegates to `services.statistics_service` — not inspected here |
| `/compare` | GET | UNCERTAIN | same |
| `/zone/{zone_code}` | GET | UNCERTAIN | same |
| `/city` | GET | UNCERTAIN | same |
| `/ranking/{disease}` | GET | UNCERTAIN | same |
| `/trends/{dcode}/{disease}` | GET | UNCERTAIN | same — note router docstring says "placeholder for future time-series" |

### `strategy.py` (prefix: `/api/v2/strategy`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/cost-per-screening` | GET | OK_AGGREGATE | `summary_district_disease` |
| `/budget-allocation-model` | GET | OK_AGGREGATE | `summary_district_disease` |
| `/roi-analysis` | GET | OK_AGGREGATE | `summary_district_disease` aggregates |
| `/resource-optimization` | GET | OK_AGGREGATE | `summary_district_disease` + `ref_facilities` |
| `/projected-savings` | GET | OK_AGGREGATE | `summary_district_disease` |

### `summary.py` (prefix: `/api/v2/summary`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/overview` | GET | OK_AGGREGATE | `mv_summary_global` + `mv_summary_zones` (fast path); legacy unified-CTE fallback also fine since v3 ETL writes to `private.*` and `mv_visit_resolved` is current |
| `/filtered` | GET | OK_AGGREGATE | `summary_disease_age_sex` MV |
| `/lab` | GET | OK_AGGREGATE | `mv_summary_lab` (110) |
| `/mental-health` | GET | OK_AGGREGATE | `mv_summary_mental` (110) |
| `/demographics` | GET | LIKELY_EMPTY | `summary_district_demographics` VIEW returns counts but most cols are placeholder `0::bigint` (105) |
| `/non-bangkok-overview` | GET | LIKELY_EMPTY | reads `raw_vitalsigns`/`raw_homevisit`/`raw_homehealth`/`raw_lab_results` — empty under v3 (headline via unified CTE works, drill-downs return 0) |
| `/non-bangkok-province/{province_code}` | GET | LIKELY_EMPTY | same |
| `/fiscal-years` | GET | LIKELY_EMPTY | `raw_vitalsigns` empty |

### `trends.py` (prefix: `/api/v2/trends`)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/screening` | GET | LIKELY_EMPTY | `raw_vitalsigns` empty — easy rewrite via `mv_visit_resolved.visit_date` |
| `/disease/{disease_key}` | GET | LIKELY_EMPTY | same |

### `zones.py` (prefix: empty — full paths in decorator)

| Path | Method | Class | Reason |
|---|---|---|---|
| `/api/v2/summary/zones` | GET | OK_AGGREGATE | `mv_summary_zones` fast path |
| `/api/v2/summary/zones/{zone_code}` | GET | OK_AGGREGATE | `summary_district_disease` (VIEW) |
| `/api/v2/zone/{zone_code}/dashboard` | GET | OK_AGGREGATE | `summary_district_disease` + `summary_facility` |

---

## Recommended Day-6 actions

### Drop (no migration path)
None — every endpoint that broke either has an obvious MV path or a hardcoded "data_available: false" stub already.

### Fix (LIKELY_500 — must rewrite query before next deploy)
1. `promotion.bmi-distribution`, `promotion.waist-risk-analysis`, `public.district-health-card`, `research.correlation-matrix` — all four reference the dropped `summary_bmi_waist`. Either:
   - **(a)** add migration `115_recreate_summary_bmi_waist.sql` building the MV from `private.visit_measurement` (variable_keys `bmi`, `waist_cm`, `weight_kg`, `height_cm`); OR
   - **(b)** rewrite each handler to query `mv_visit_resolved` + `private.visit_measurement` directly.

### Re-route to live MVs (LIKELY_EMPTY — works but returns nothing)
2. `trends/{screening,disease}`, `epidemiology/{incidence-rate,outbreak-detection}`, `executive/yoy-comparison`, `kpi/progress-tracker`, `disease-control/{repeat-screening,progression}` — all currently `FROM raw_vitalsigns`. Single helper `_visit_timeseries(disease_key, granularity, district)` over `mv_visit_resolved` would unblock the lot.
3. `districts/{dcode}/disease/{disease_key}` risk-factor breakdown, `promotion.risk-factor-profile`, `promotion.exercise-frequency` — point at `summary_disease_age_sex` (sex × age_group is already there) and synthesise smoking/exercise from `private.visit_measurement` (variable_keys `smoking_status`, `exercise_frequency`).

### Defer (data not yet ingested)
4. `disease-control/{referral-outcome,treatment-compliance}` — needs `referral_type` and `*_treatment` to be added to `private.variable_definition` and ingested. Leave handlers as-is but ensure they keep returning `data_available: false` rather than 0-row aggregates.

### Inspect during Day-6 review
5. `chat/*` — depends on the orchestrator's tool catalogue; verify it doesn't pull patient-level rows.
6. `statistics_v1/*` — six endpoints delegating to `services.statistics_service` not opened here.
