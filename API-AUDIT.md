# BMA Health API Audit — Endpoint Status Map

**Date:** 2026-04-27
**Scope:** All 170 endpoints across 25 routers in `bma-health-db/api/`
**Method:** Static analysis of `routers/*.py`, `services/*.py`, view definitions in PostgreSQL,
plus live HTTP smoke tests against `http://localhost:9002` with `X-API-Key: dev-api-key`.

---

## 1 · Executive Summary

### Status counts (170 endpoints)

| Status                | Count | Notes                                                                                        |
|-----------------------|-------|----------------------------------------------------------------------------------------------|
| WORKING               |    47 | Returns valid data — generally those that read scalar counts, ref tables, or static configs. |
| BROKEN (zeros/empty)  |    62 | Returns 200 but disease/lab/risk numbers are all 0 because of the value_text/MV bug.         |
| BROKEN (500 error)    |    11 | Hits a non-existent view/table or a column that does not exist.                              |
| PARTIAL               |    24 | Some fields work (counts, %s of population), other fields are zero/empty.                    |
| STUB / NO-DATA        |    14 | Endpoint returns `{"data_available": false}` by design — schema gap, not the MV bug.         |
| NON-DATA              |    12 | Auth, file uploads, PDF downloads, health checks — not affected by the data bug.             |

### Top 10 broken endpoints by user impact

| # | Endpoint                                              | Why it matters                                                       |
|---|-------------------------------------------------------|----------------------------------------------------------------------|
| 1 | `GET /api/v2/summary/overview`                        | Frontpage. `by_disease` all zeros — the headline tile shows 0 cases. |
| 2 | `GET /api/v2/executive/headline-kpi`                  | Governor's banner. `top_disease=null`, `most_concerning_district=null`. |
| 3 | `GET /api/v2/summary/lab`                             | Returns `[]` — no lab averages anywhere.                             |
| 4 | `GET /api/v2/summary/districts`                       | District list shows total_screened only; risk counts all 0.          |
| 5 | `GET /api/v2/summary/zones`                           | Zone diseases all 0 (same root cause).                               |
| 6 | `GET /api/v2/epidemiology/age-group-prevalence`       | Returns 500 — view `summary_disease_age_sex` does not exist.         |
| 7 | `GET /api/v2/epidemiology/multi-disease-matrix`       | Returns 500 — view `summary_comorbidity` does not exist.             |
| 8 | `GET /api/v2/epidemiology/disease-lab-crosstab`       | Returns 500 — view `summary_lab_disease_cross` does not exist.       |
| 9 | `GET /api/v2/promotion/bmi-distribution`              | Returns 500 — view `summary_bmi_waist` does not exist.               |
|10 | `GET /api/v2/kpi/control-rates?disease=diabetes`      | Returns 500 — table `summary_disease_control` does not exist.        |

### Knock-on effect

* **Every dashboard tile that quotes a disease %** returns 0. ~95 % of the user-facing surface is materially affected.
* **Frontend reports** (Excel/PDF/zone reports) compile successfully because they pull from `data_adapter.load_district_data()`, but the resulting documents contain zero-valued indicators.
* **Cache hit-rate is high** (76 %) → many of the wrong (zero) numbers are also cached. After the data is fixed, run `POST /api/admin/invalidate-cache` (or wait ~15 min for T2 TTL).

---

## 2 · Affected DB Objects

### 2.1 Materialized views

| MV                            | Rows  | Status     | Notes                                                                       |
|-------------------------------|-------|------------|-----------------------------------------------------------------------------|
| `public.mv_visit_resolved`    | 35,111 | **BROKEN** | risk/found booleans all FALSE because EAV `value_boolean` is NULL.        |
| `public.mv_disease_district`  | 0     | **EMPTY**  | Filters on `value_boolean = true`.                                          |
| `public.mv_lab_distribution`  | 0     | **EMPTY**  | AVGs `value_number`, all NULL.                                              |
| `public.mv_demographics`      | 51    | **PARTIAL** | Has rows but counts are all 0 (filters on `value_text` enums).            |
| `public.mv_kpi_tier1`         | 52    | **PARTIAL** | Has rows but disease counts are 0.                                          |
| `public.mv_lifestyle`         | 156   | **PARTIAL** | Counts all 0.                                                               |
| `public.mv_mental_health`     | 52    | **PARTIAL** | Has rows but `pct_*` percentages all 0.                                     |
| `public.mv_data_dictionary`   | 378   | WORKING    | Schema metadata — not affected.                                             |

### 2.2 Public views (built on top of the above)

| View                                  | Rows | Status      | Notes                                                                                         |
|---------------------------------------|------|-------------|-----------------------------------------------------------------------------------------------|
| `public.summary_district_disease`     | 70   | **PARTIAL** | total_screened correct (29,979) but `risk_*_count` and `found_*_count` all 0.                 |
| `public.summary_district_demographics`| 62   | **PARTIAL** | total_respondents has rows; education/occupation/etc breakdowns all 0.                        |
| `public.summary_district_lab`         | 0    | **EMPTY**   | Joins `private.lab_measurement` with `WHERE value_number IS NOT NULL` → zero rows.            |
| `public.summary_district_mental`      | 14   | **PARTIAL** | total_screened present; `pct_*` all 0 because the boolean flags are NULL.                     |
| `public.summary_district_risk_factors`| 0    | **EMPTY**   | Source view excludes rows when keys cannot be resolved from EAV.                              |
| `public.summary_facility`             | 0    | **EMPTY**   | Built directly from `private.visit_event` joined to `mv_visit_resolved` → no risk hits.       |

### 2.3 Views referenced by endpoints but **DO NOT EXIST** in DB

These cause 500 INTERNAL_ERROR responses:

| Missing view/table                 | Endpoints affected                                                                          |
|------------------------------------|---------------------------------------------------------------------------------------------|
| `summary_disease_age_sex`          | `/epidemiology/age-group-prevalence`, `/epidemiology/age-pyramid`, `/research/statistical-test` |
| `summary_lab_disease_cross`        | `/epidemiology/disease-lab-crosstab`                                                        |
| `summary_comorbidity`              | `/epidemiology/multi-disease-matrix`                                                        |
| `summary_bmi_waist`                | `/promotion/bmi-distribution`, `/promotion/waist-risk-analysis`, `/research/correlation-matrix`, `/public/district-health-card` (joins it) |
| `summary_disease_control`          | `/kpi/control-rates`                                                                        |
| `raw_vitalsigns` (table exists, 0 rows) | `/epidemiology/incidence-rate`, `/epidemiology/outbreak-detection`, `/disease-control/repeat-screening`, `/disease-control/progression`, `/disease-control/referral-outcome`, `/executive/yoy-comparison`, `/kpi/progress-tracker`, `/trends/screening`, `/trends/disease/*`, `/research/individual-data`, `/summary/non-bangkok-overview` (province detail), `/summary/non-bangkok-province/*`, `/summary/fiscal-years`, `/summary/filtered` (raw path) |
| `raw_lab_results` (table exists, 0 rows) | `/summary/non-bangkok-overview` (lab detail), `/summary/non-bangkok-province/*` |
| `raw_homehealth` (table exists, 0 rows) | `/disease-control/treatment-compliance`, `/promotion/exercise-frequency`, `/promotion/diet-disease-correlation`, `/promotion/behavior-disease-correlation` (alcohol path) |
| `raw_homevisit` (table exists, 0 rows) | `/summary/non-bangkok-overview` (province aggregation), `/summary/non-bangkok-province/*` |
| `ref_facilities` (table exists, 0 rows) | `/gis/facilities*`, `/gis/heatmap/*` (centroids = NULL), `/public/screening-locations`, `/strategy/resource-optimization` (clinic_count=0) |

---

## 3 · Per-router endpoint tables

> **Legend**
> - **W** = WORKING (returns expected non-zero data)
> - **B** = BROKEN (returns zero/empty/500 due to data or schema issue)
> - **P** = PARTIAL (subset of fields work)
> - **S** = STUB (returns `{"data_available": false}` by design)
> - **U** = UNKNOWN (cannot determine without integration test)
> - **N** = NON-DATA (auth, file IO, etc.)

### 3.1 `summary` — `routers/summary.py`

Pre-aggregated overview, lab/mental, demographics, fiscal-years, non-Bangkok.

| Method | Path                                              | DB sources                                                                          | Status | Notes                                                                                            |
|--------|---------------------------------------------------|-------------------------------------------------------------------------------------|--------|--------------------------------------------------------------------------------------------------|
| GET    | `/api/v2/summary/overview`                        | `mv_visit_resolved` (via `unified` CTE), `raw_vitalsigns`, `raw_homehealth`, refs    | **P**  | `total_screened`/`total_visits`/zone counts WORK (34,970). `by_disease` all 0. `audit.dropped_*` shows 0. |
| GET    | `/api/v2/summary/filtered`                        | `summary_district_risk_factors` OR `raw_vitalsigns`+`raw_patients`                  | **B**  | Both code paths empty (view has 0 rows; raw tables empty).                                       |
| GET    | `/api/v2/summary/lab`                             | `summary_district_lab` JOIN `ref_districts`                                         | **B**  | `[]` — view is empty.                                                                            |
| GET    | `/api/v2/summary/mental-health`                   | `summary_district_mental` JOIN `ref_districts`                                      | **P**  | Returns 2 districts; all `pct_*` = 0.                                                            |
| GET    | `/api/v2/summary/demographics`                    | `summary_district_demographics` JOIN `ref_districts`                                | **P**  | Rows exist; `total_respondents` populated, all education/occupation breakdowns = 0.              |
| GET    | `/api/v2/summary/non-bangkok-overview`            | `mv_visit_resolved` + `raw_vitalsigns` + `raw_homevisit` + `raw_lab_results`        | **P**  | Headline 14 patients works (mv_visit_resolved). Province detail/lab/mental zeros (raw tables empty). |
| GET    | `/api/v2/summary/non-bangkok-province/{code}`     | `raw_vitalsigns` + `raw_homevisit` + `raw_lab_results` + `raw_homehealth`           | **B**  | All raw tables are empty → always returns 404 / suppressed.                                      |
| GET    | `/api/v2/summary/fiscal-years`                    | `raw_vitalsigns`                                                                    | **B**  | Empty result set (raw_vitalsigns is empty).                                                      |
| GET    | `/api/v2/summary/zones` *(actually in zones router)* | n/a                                                                              | —      | Listed in zones below.                                                                            |
| GET    | `/api/v2/summary/districts` *(in districts router)*  | n/a                                                                              | —      | Listed in districts below.                                                                        |

### 3.2 `zones` — `routers/zones.py`

Zone list / detail / dashboard.

| Method | Path                                | DB sources                                                                  | Status | Notes                                                                                       |
|--------|-------------------------------------|-----------------------------------------------------------------------------|--------|---------------------------------------------------------------------------------------------|
| GET    | `/api/v2/summary/zones`             | `mv_visit_resolved` (unified CTE) JOIN `ref_health_zones`+`ref_districts`   | **P**  | `total_screened`/`total_visits` populated; `diseases.{key}.count` all 0.                    |
| GET    | `/api/v2/summary/zones/{zone_code}` | `summary_district_disease`                                                  | **P**  | Districts + zone meta returned; risk_*_count/found_*_count = 0.                             |
| GET    | `/api/v2/zone/{zone_code}/dashboard`| `summary_district_disease` + `summary_facility`                             | **P**  | Districts list works. `facilities=[]` (summary_facility is empty).                          |

### 3.3 `districts` — `routers/districts.py`

Per-district list, detail, and disease drill-down.

| Method | Path                                                  | DB sources                                                                          | Status | Notes                                                                                       |
|--------|-------------------------------------------------------|-------------------------------------------------------------------------------------|--------|---------------------------------------------------------------------------------------------|
| GET    | `/api/v2/summary/districts`                           | `mv_visit_resolved` (unified CTE) JOIN `ref_districts`                              | **P**  | total_screened/total_visits OK; all disease counts 0.                                       |
| GET    | `/api/v2/summary/districts/{dcode}`                   | `summary_district_disease` + `_lab` + `_mental` + `_demographics`                   | **P**  | Disease block has total_screened; lab=null; mental %s = 0; demographics counts 0.           |
| GET    | `/api/v2/summary/districts/{dcode}/disease/{key}`     | `summary_district_disease` + `summary_district_risk_factors`                        | **P**  | Disease summary numbers 0; `risk_factor_breakdown=[]` (sdrf empty).                          |

### 3.4 `epidemiology` — `routers/epidemiology.py`

| Method | Path                                              | DB sources                                                                  | Status | Notes                                                                                       |
|--------|---------------------------------------------------|-----------------------------------------------------------------------------|--------|---------------------------------------------------------------------------------------------|
| GET    | `/api/v2/epidemiology/age-group-prevalence`       | `summary_disease_age_sex` (does not exist)                                  | **B**  | 500 INTERNAL_ERROR — view missing.                                                          |
| GET    | `/api/v2/epidemiology/disease-lab-crosstab`       | `summary_lab_disease_cross` (does not exist)                                | **B**  | 500.                                                                                         |
| GET    | `/api/v2/epidemiology/multi-disease-matrix`       | `summary_comorbidity` (does not exist)                                      | **B**  | 500.                                                                                         |
| GET    | `/api/v2/epidemiology/age-pyramid`                | `summary_disease_age_sex`                                                   | **B**  | 500.                                                                                         |
| GET    | `/api/v2/epidemiology/incidence-rate`             | `raw_vitalsigns` (empty table)                                              | **B**  | Returns `{"data_available": false}`.                                                        |
| GET    | `/api/v2/epidemiology/outbreak-detection`         | `raw_vitalsigns` JOIN `ref_districts`                                       | **B**  | `{"alert": false, "reason": "insufficient_data"}`.                                          |

### 3.5 `trends` — `routers/trends.py`

| Method | Path                                | DB sources           | Status | Notes                                  |
|--------|-------------------------------------|----------------------|--------|----------------------------------------|
| GET    | `/api/v2/trends/screening`          | `raw_vitalsigns`     | **B**  | `data: []` (raw_vitalsigns is empty).  |
| GET    | `/api/v2/trends/disease/{key}`      | `raw_vitalsigns`     | **B**  | `data: []`.                             |

### 3.6 `kpi` — `routers/kpi.py`

| Method | Path                                | DB sources                                                                | Status | Notes                                                                                                  |
|--------|-------------------------------------|---------------------------------------------------------------------------|--------|--------------------------------------------------------------------------------------------------------|
| GET    | `/api/v2/kpi/moph-targets`          | `summary_district_disease` + `ref_districts.population`                   | **P**  | Coverage % works (29,979/6.06M = 0.5 %). Disease KPIs all 0 % so all "ไม่ผ่าน".                     |
| GET    | `/api/v2/kpi/screening-yield`       | `summary_district_disease`                                                | **P**  | Districts list returns; all `yield_pct` = 0.                                                           |
| GET    | `/api/v2/kpi/control-rates`         | `summary_disease_control` (does not exist)                                | **B**  | 500.                                                                                                   |
| GET    | `/api/v2/kpi/zone-comparison`       | `ref_health_zones` + `summary_district_disease`                           | **P**  | `screening_coverage` works. `dm_risk`/`hpt_risk`/`obesity_risk` all return 0 %.                        |
| GET    | `/api/v2/kpi/progress-tracker`      | `raw_vitalsigns`                                                          | **B**  | `quarters: []` (empty raw table).                                                                      |
| GET    | `/api/v2/kpi/benchmark`             | `summary_district_disease`                                                | **P**  | Coverage % works; all disease BMA prevalence = 0 %, so all "ต่ำกว่า national".                       |
| GET    | `/api/v2/kpi/gap-analysis`          | `summary_district_disease` JOIN `ref_districts`                           | **P**  | Works for `screening_coverage_pct` (uses populations). For disease pct's, every district reads 0.      |

### 3.7 `executive` — `routers/executive.py`

| Method | Path                                | DB sources                                                                | Status | Notes                                                                                       |
|--------|-------------------------------------|---------------------------------------------------------------------------|--------|---------------------------------------------------------------------------------------------|
| GET    | `/api/v2/executive/headline-kpi`    | `mv_visit_resolved` (unified) + `summary_district_disease` + ref_districts | **P**  | total_screened (34,970) + coverage % work; `top_disease=null`, `most_concerning_district=null`. |
| GET    | `/api/v2/executive/yoy-comparison`  | `raw_vitalsigns`                                                          | **B**  | `periods: []`.                                                                              |
| GET    | `/api/v2/executive/campaign-impact` | (none — static stub)                                                      | **S**  | Returns `{data_available:false}` — schema-gap stub.                                          |
| GET    | `/api/v2/executive/media-brief`     | `summary_district_disease` + `ref_districts`                              | **P**  | Total + bullets render but disease %s all 0 in copy.                                         |
| GET    | `/api/v2/executive/alert`           | `summary_district_disease`                                                | **P**  | `alerts: []` (no district crosses thresholds because all pct's are 0).                       |

### 3.8 `disease_control` — `routers/disease_control.py`

| Method | Path                                          | DB sources                                                  | Status | Notes                                                                          |
|--------|-----------------------------------------------|-------------------------------------------------------------|--------|--------------------------------------------------------------------------------|
| GET    | `/api/v2/disease-control/screening-coverage`  | `ref_districts` + `summary_district_disease`                | **W**  | Works — uses population + total_screened only. Returns 50 districts.           |
| GET    | `/api/v2/disease-control/ncd-cascade`         | `summary_district_disease`                                  | **P**  | "Screened" step works (29,979). at_risk + diagnosed all 0.                     |
| GET    | `/api/v2/disease-control/repeat-screening`    | `raw_vitalsigns`                                            | **B**  | `{data_available:false}`.                                                      |
| GET    | `/api/v2/disease-control/progression`         | `raw_vitalsigns`                                            | **B**  | `multi_visit_patients: 0`.                                                     |
| GET    | `/api/v2/disease-control/referral-outcome`    | `raw_vitalsigns`                                            | **B**  | `{data_available:false, message:"... referral_type ว่างทั้งหมด"}`.           |
| GET    | `/api/v2/disease-control/treatment-compliance`| `raw_homehealth`                                            | **B**  | `{data_available:false}`.                                                      |

### 3.9 `factors` — `routers/factors.py` (modeled — uses `data_adapter`)

| Method | Path                                  | DB sources                                          | Status | Notes                                                                                       |
|--------|---------------------------------------|-----------------------------------------------------|--------|---------------------------------------------------------------------------------------------|
| GET    | `/api/factors/sex`                    | `data_adapter.load_district_data` → `summary_district_disease` | **P**  | Modeled outputs render. Base disease rates all 0 → every category's pct_at_risk ~0. |
| GET    | `/api/factors/age-group`              | same                                                | **P**  | Same problem — chi-square computed on zeros.                                                |
| GET    | `/api/factors/occupation`             | same                                                | **P**  | Same.                                                                                        |
| GET    | `/api/factors/zone`                   | same                                                | **P**  | Same.                                                                                        |
| GET    | `/api/factors/behavior/smoking`       | same                                                | **P**  | Same.                                                                                        |
| GET    | `/api/factors/behavior/alcohol`       | same                                                | **P**  | Same.                                                                                        |
| GET    | `/api/factors/behavior/exercise`      | same                                                | **P**  | Same.                                                                                        |
| GET    | `/api/factors/cross-tabulation`       | same                                                | **P**  | Cells have counts, all pct_at_risk are derived from base_rate = 0.                           |

### 3.10 `promotion` — `routers/promotion.py`

| Method | Path                                            | DB sources                                                | Status | Notes                                                                                       |
|--------|-------------------------------------------------|-----------------------------------------------------------|--------|---------------------------------------------------------------------------------------------|
| GET    | `/api/v2/promotion/bmi-distribution`            | `summary_bmi_waist` (does not exist)                      | **B**  | 500.                                                                                         |
| GET    | `/api/v2/promotion/behavior-disease-correlation`| `summary_district_risk_factors` OR `raw_homehealth`       | **B**  | Both empty — `{data_available:false}`.                                                       |
| GET    | `/api/v2/promotion/risk-factor-profile`         | `summary_district_risk_factors`                           | **B**  | View empty → `{data_available:false}`.                                                       |
| GET    | `/api/v2/promotion/exercise-frequency`          | `raw_homehealth`                                          | **B**  | `{data_available:false}`.                                                                    |
| GET    | `/api/v2/promotion/waist-risk-analysis`         | `summary_bmi_waist` (does not exist)                      | **B**  | 500.                                                                                         |
| GET    | `/api/v2/promotion/diet-disease-correlation`    | `raw_homehealth` JOIN `raw_vitalsigns`                    | **B**  | Both raw tables empty.                                                                       |

### 3.11 `facility` — `routers/facility.py`

| Method | Path                                          | DB sources                                                | Status | Notes                                                                                                    |
|--------|-----------------------------------------------|-----------------------------------------------------------|--------|----------------------------------------------------------------------------------------------------------|
| GET    | `/api/v2/facility/performance`                | `summary_facility` JOIN `ref_districts`                   | **B**  | 500 — view exists but column `lab_completed` referenced; the immediate failure is the view returning 0 rows but column-list mismatch. (`facility_code` cast issue.) |
| GET    | `/api/v2/facility/workload`                   | `summary_facility` JOIN `ref_districts`                   | **B**  | Returns `{facilities:[]}` (view empty).                                                                  |
| GET    | `/api/v2/facility/screening-yield-rank`       | `summary_facility`                                        | **B**  | `{facilities:[]}`.                                                                                       |
| GET    | `/api/v2/facility/staff-performance`          | (none)                                                    | **N**  | Static stub — by design "PDPA: not exposed".                                                             |
| GET    | `/api/v2/facility/capacity-planning`          | `ref_districts` + `summary_district_disease`              | **W**  | Works — uses population + total_screened only.                                                           |
| GET    | `/api/v2/facility/comparison`                 | `summary_facility`                                        | **B**  | Always returns 404 (view is empty).                                                                      |

### 3.12 `strategy` — `routers/strategy.py`

| Method | Path                                          | DB sources                                                | Status | Notes                                                                                                       |
|--------|-----------------------------------------------|-----------------------------------------------------------|--------|-------------------------------------------------------------------------------------------------------------|
| GET    | `/api/v2/strategy/cost-per-screening`         | `summary_district_disease`                                | **P**  | Total cost works (29,979 × 350 THB). `cost_per_risk_found = null` for every row (risk counts = 0).          |
| GET    | `/api/v2/strategy/budget-allocation-model`    | `summary_district_disease` JOIN `ref_districts`           | **P**  | Allocation works because score uses population; `risk_rate` collapses to 0 → nearly equal per-capita allocation. |
| GET    | `/api/v2/strategy/roi-analysis`               | `summary_district_disease`                                | **P**  | Screening cost OK; all savings = 0 because dm/hpt counts = 0.                                                |
| GET    | `/api/v2/strategy/resource-optimization`      | `summary_district_disease` + `ref_districts` + `ref_facilities` | **P**  | Coverage works; `risk_rate=0`, `clinic_count=0` (ref_facilities empty), `priority_score=0` for everyone.    |
| GET    | `/api/v2/strategy/projected-savings`          | `ref_districts.population` + `summary_district_disease`   | **P**  | Coverage projection works; new-cases-found and savings = 0.                                                  |

### 3.13 `research` — `routers/research.py`

| Method | Path                                          | DB sources                                                | Status | Notes                                                                                                       |
|--------|-----------------------------------------------|-----------------------------------------------------------|--------|-------------------------------------------------------------------------------------------------------------|
| GET    | `/api/v2/research/data-dictionary`            | `information_schema.columns`                              | **W**  | Schema introspection — works.                                                                                |
| GET    | `/api/v2/research/individual-data`            | `raw_patients` LEFT JOIN `raw_vitalsigns`                 | **B**  | Both raw tables empty. data_shape all zeros.                                                                |
| GET    | `/api/v2/research/statistical-test`           | `summary_disease_age_sex` (test=proportion path)          | **B**  | "proportion" path errors silently because view missing; default chi_square path returns OK message.         |
| GET    | `/api/v2/research/correlation-matrix`         | `summary_district_disease`+`summary_district_lab`+`summary_bmi_waist` | **B**  | 500 — `summary_bmi_waist` does not exist.                                                            |
| GET    | `/api/v2/research/sample-size-calculator`     | `ref_districts.population`                                | **W**  | Pure math + population scalar — works.                                                                       |
| GET    | `/api/v2/research/export`                     | `summary_district_disease`                                | **P**  | 54 rows returned with district names + total_screened; all risk/found counts = 0.                           |

### 3.14 `public` — `routers/public.py`

| Method | Path                                          | DB sources                                                                          | Status | Notes                                                                                                  |
|--------|-----------------------------------------------|-------------------------------------------------------------------------------------|--------|--------------------------------------------------------------------------------------------------------|
| GET    | `/api/v2/public/district-summary`             | `summary_district_disease`                                                          | **P**  | Heading + total OK; all per-disease lines say "ข้อมูลไม่เพียงพอ" because count < 5.                  |
| GET    | `/api/v2/public/screening-locations`          | `ref_facilities`                                                                    | **B**  | `[]` (ref_facilities empty).                                                                            |
| GET    | `/api/v2/public/health-tips`                  | (static dict)                                                                       | **W**  | Returns hard-coded tips.                                                                                |
| GET    | `/api/v2/public/service-satisfaction`         | (none)                                                                              | **S**  | Stub by design.                                                                                          |
| GET    | `/api/v2/public/complaint-status`             | (none)                                                                              | **S**  | Stub.                                                                                                    |
| GET    | `/api/v2/public/open-data`                    | `summary_district_disease`                                                          | **P**  | 54 records; all pct's ≈ 0.                                                                              |
| GET    | `/api/v2/public/district-health-card`         | `summary_district_disease` + `summary_district_mental` + `summary_district_lab` + `summary_bmi_waist` | **B**  | 500 — `summary_bmi_waist` JOIN throws.                                                                |

### 3.15 `monitoring` — `routers/monitoring.py`

| Method | Path                                          | DB sources                                                | Status | Notes                                                                                                                       |
|--------|-----------------------------------------------|-----------------------------------------------------------|--------|-----------------------------------------------------------------------------------------------------------------------------|
| GET    | `/api/v2/monitoring/data-quality`             | All `raw_*` tables                                        | **W**  | Reports 0 rows for all (which is actually accurate — they ARE 0).                                                            |
| GET    | `/api/v2/monitoring/cleansing-report`         | All `raw_*` + `import_history`                            | **W**  | Same — reports correctly that raw tables are empty.                                                                          |
| GET    | `/api/v2/monitoring/table-stats`              | `pg_stat_user_tables` + `summary_district_disease.refreshed_at` | **W** | Works.                                                                                                                       |
| GET    | `/api/v2/monitoring/etl-status`               | `import_history`                                          | **W**  | Works — shows last bundle import.                                                                                            |
| GET    | `/api/v2/monitoring/api-performance`          | (none)                                                    | **W**  | Static config.                                                                                                               |
| GET    | `/api/v2/monitoring/audit-log`                | `import_history`                                          | **W**  | Works.                                                                                                                       |
| GET    | `/api/v2/monitoring/cache-stats`              | Redis                                                     | **W**  | Works.                                                                                                                       |

### 3.16 `gis` — `routers/gis.py`

| Method | Path                                              | DB sources                                                  | Status | Notes                                                                                                       |
|--------|---------------------------------------------------|-------------------------------------------------------------|--------|-------------------------------------------------------------------------------------------------------------|
| GET    | `/api/v2/gis/facilities`                          | `ref_facilities`                                            | **B**  | `total: 0` (ref_facilities empty).                                                                          |
| GET    | `/api/v2/gis/facilities/{code}`                   | `ref_facilities` LEFT JOIN `summary_facility`               | **B**  | 404 — empty table.                                                                                          |
| GET    | `/api/v2/gis/facilities/zone/{zone_code}`         | `ref_facilities`                                            | **B**  | `facilities: []`.                                                                                            |
| GET    | `/api/v2/gis/facilities/district/{district_code}` | `ref_facilities`                                            | **B**  | Same.                                                                                                       |
| GET    | `/api/v2/gis/facility-types`                      | `ref_facilities`                                            | **B**  | `types: []`.                                                                                                |
| GET    | `/api/v2/gis/heatmap/disease/{disease_key}`       | `summary_district_disease` LEFT JOIN `ref_facilities`       | **P**  | District names + total_screened OK; centroid_lat/lng = null (no facilities); disease_value = 0.            |
| GET    | `/api/v2/gis/overlay/disease-environment`         | `summary_district_disease` + `ref_facilities` + ArcGIS PM2.5| **P**  | Disease pct = 0; PM2.5 layer works (external).                                                              |
| GET    | `/api/v2/gis/pm25/current`                        | ArcGIS proxy                                                | **U**  | External — depends on ArcGIS up. Smoke test showed `data_available:false` (likely network/cors in dev).     |
| GET    | `/api/v2/gis/boundaries/districts`                | ArcGIS proxy                                                | **U**  | External.                                                                                                   |
| GET    | `/api/v2/gis/pm25/zones`                          | ArcGIS + `ref_health_zones` + `pm25_daily`                  | **P**  | Returns 8 zones; pm25 values null (data_available=false).                                                   |
| GET    | `/api/v2/gis/pm25/districts`                      | ArcGIS + `ref_districts` + `pm25_daily`                     | **P**  | 50 districts returned; readings null.                                                                       |
| GET    | `/api/v2/gis/pm25/monthly`                        | `pm25_daily` + ArcGIS                                       | **B**  | `period: []` and current_snapshot null — pm25_daily presumably empty too.                                   |

### 3.17 `chat` — `routers/chat.py`

| Method | Path                          | DB sources    | Status | Notes                                            |
|--------|-------------------------------|---------------|--------|--------------------------------------------------|
| GET/POST | `/api/health/chat`          | LMStudio      | **U**  | Depends on LMStudio reachability — not data-bug related. Will surface zeros via tool calls. |
| GET/POST | `/api/health/chat/stream`   | LMStudio + tools (which call other endpoints) | **U** | Same. Zero-data shows up downstream.            |

### 3.18 `reports` — `routers/reports.py`

PDF generation — pulls aggregate data via `data_adapter` and `report_data_collector`.

| Method | Path                                              | Source                                | Status | Notes                                                                                       |
|--------|---------------------------------------------------|---------------------------------------|--------|---------------------------------------------------------------------------------------------|
| GET    | `/api/reports/comprehensive/{lang}`               | LaTeX/Tectonic + `data_adapter`        | **P**  | Compiles; numeric content reads zero-disease values.                                         |
| GET    | `/api/reports/executive/{lang}`                   | same                                   | **P**  | Same.                                                                                        |
| GET    | `/api/reports/disease/{disease}`                  | LaTeX cache                            | **P**  | Existing cached PDF returned; content stale at zero data.                                    |
| GET    | `/api/reports/zone/{zone_code}/{lang}`            | LaTeX + `data_adapter`                 | **P**  | Same.                                                                                        |
| GET    | `/api/reports/msd/{lang}`                         | LaTeX + `data_adapter`                 | **P**  | Same.                                                                                        |
| GET    | `/api/reports/repeat-screening/{lang}`            | LaTeX + `raw_vitalsigns`               | **B**  | Likely fails or shows zeros (depends on `raw_vitalsigns`).                                   |
| GET    | `/api/reports/public/{lang}`                      | Cached PDF                             | **N**  | Static file fetch.                                                                            |
| GET    | `/api/reports/adaptive/{filename}`                | Cached PDF                             | **N**  | Static fetch.                                                                                 |
| GET    | `/api/reports/catalog`                            | Filesystem stat                        | **W**  | Works.                                                                                        |
| GET    | `/api/reports/dashboard`                          | Filesystem + scheduler                 | **W**  | Works.                                                                                        |
| GET    | `/api/reports/scheduler-status`                   | Scheduler                              | **W**  | Works.                                                                                        |
| GET    | `/api/reports/generation-progress`                | In-memory                              | **W**  | Works.                                                                                        |
| GET    | `/api/reports/status`                             | Filesystem                             | **W**  | Works.                                                                                        |
| POST   | `/api/reports/generate`                           | Background task                        | **N**  | Triggers generation.                                                                          |
| POST   | `/api/reports/generate/{lang}`                    | Background task                        | **N**  | Same.                                                                                          |
| POST   | `/api/reports/generate/{lang}/{report_type}`      | Background task                        | **N**  | Same.                                                                                          |
| POST   | `/api/reports/invalidate`                         | Cache wipe                             | **N**  | Side-effect.                                                                                  |

### 3.19 `export` — `routers/export.py`

| Method | Path                                          | Source                              | Status | Notes                                                                       |
|--------|-----------------------------------------------|-------------------------------------|--------|-----------------------------------------------------------------------------|
| GET    | `/api/export/city/excel`                      | `data_adapter` → `summary_district_disease` | **P** | Spreadsheet contains all districts; numeric columns zero.            |
| GET    | `/api/export/district/{dcode}/excel`          | same                                | **P**  | Same.                                                                       |
| GET    | `/api/export/district/{dcode}/pdf`            | same                                | **P**  | Text file with zero numbers.                                                |
| GET    | `/api/export/district/{dcode}/pdf/json`       | same                                | **P**  | Same.                                                                       |
| GET    | `/api/export/zone/{zone_code}/excel`          | same                                | **P**  | Same.                                                                       |
| GET    | `/api/export/rankings/{disease}/excel`        | same                                | **P**  | Sorted by 0 → arbitrary order, every row pct=0.                             |

### 3.20 `dashboard_v1` — `routers/dashboard_v1.py`

| Method | Path                              | Source                              | Status | Notes                                                                          |
|--------|-----------------------------------|-------------------------------------|--------|--------------------------------------------------------------------------------|
| GET    | `/api/dashboard/governor`         | `data_adapter`                      | **P**  | total_screened (26,297 — 46 districts after k-anon) works; all disease pct = 0. |
| GET    | `/api/dashboard/district/{dcode}` | `data_adapter`                      | **P**  | District meta + indicators present; all means/pct = 0.                          |
| GET    | `/api/dashboard/medical`          | `data_adapter`                      | **P**  | indicator_stats render with mean=0, std=0.                                      |

### 3.21 `statistics_v1` — `routers/statistics_v1.py`

| Method | Path                              | Source                              | Status | Notes                                                                          |
|--------|-----------------------------------|-------------------------------------|--------|--------------------------------------------------------------------------------|
| GET    | `/api/stats/city`                 | `data_adapter`                      | **P**  | total_screened OK; per-disease weighted_pct_at_risk all 0.                     |
| GET    | `/api/stats/district/{dcode}`     | `data_adapter`                      | **P**  | Same.                                                                          |
| GET    | `/api/stats/zone/{zone_code}`     | `data_adapter` + ZONE_MAPPING       | **P**  | Same.                                                                          |
| GET    | `/api/stats/ranking/{disease}`    | `data_adapter`                      | **P**  | Returns 50 districts ranked by pct=0 (alphabetical-ish tie-break).              |
| GET    | `/api/stats/compare`              | `data_adapter`                      | **P**  | t-tests on zero arrays return non-meaningful p-values.                         |
| GET    | `/api/stats/trends/{dcode}/{key}` | (placeholder)                        | **S**  | Returns `data_points: []` by design.                                            |

### 3.22 `screening_tests` — `routers/screening_tests.py`

| Method | Path                                          | Source                              | Status | Notes                                                                          |
|--------|-----------------------------------------------|-------------------------------------|--------|--------------------------------------------------------------------------------|
| GET    | `/api/screening-tests/summary`                | `data_adapter` (modeled)            | **P**  | "Works" — but numbers are deterministically modeled from total_screened, not real. |
| GET    | `/api/screening-tests/district/{dcode}`       | same                                | **P**  | Same.                                                                          |
| GET    | `/api/screening-tests/ekg/summary`            | same                                | **P**  | Same.                                                                          |
| GET    | `/api/screening-tests/chest-xray/summary`     | same                                | **P**  | Same.                                                                          |
| GET    | `/api/screening-tests/blood/summary`          | same                                | **P**  | Same.                                                                          |
| GET    | `/api/screening-tests/retinal/summary`        | same                                | **P**  | Same.                                                                          |

> NOTE: This router is *intentionally synthetic* (the `_seed_for(dcode)` + `_vary` helpers fabricate plausible numbers for TOR compliance). The data bug doesn't directly break it but means the seed (total_screened) does not reflect reality fully.

### 3.23 `search` — `routers/search.py`

| Method | Path                                  | DB sources                                | Status | Notes                                                                          |
|--------|---------------------------------------|-------------------------------------------|--------|--------------------------------------------------------------------------------|
| GET    | `/api/v2/search/districts`            | `summary_district_disease` (or `_lab` for ckd/anemia) | **P**  | Districts list returns; all `disease_pct=0`. ckd/anemia path returns `[]` (lab view empty). |

### 3.24 `admin_api` — `routers/admin_api.py`

| Method | Path                                  | Source                              | Status | Notes                                                                          |
|--------|---------------------------------------|-------------------------------------|--------|--------------------------------------------------------------------------------|
| GET    | `/api/admin/data-status`              | `data_adapter`                      | **P**  | Reports `total_districts=46, completeness=92%` — derived from `total_screened`. |
| POST   | `/api/admin/upload-screening`         | filesystem                          | **N**  | JSON upload; auth required.                                                    |
| POST   | `/api/admin/upload-excel`             | filesystem                          | **N**  | Excel upload; auth required.                                                   |
| GET    | `/api/admin/excel-template`           | static                              | **N**  | Generated template.                                                            |
| POST   | `/api/admin/invalidate-cache`         | Redis + memory                      | **N**  | Side-effect.                                                                   |
| GET    | `/api/admin/audit-log`                | log file                            | **N**  | Probably empty in dev.                                                         |
| GET    | `/api/admin/audit-log/verify`         | log file                            | **N**  | Hash chain verify.                                                             |

### 3.25 `admin` (HTML) — `api/admin.py` (mounted at `/admin/*`)

| Method | Path                              | Source                              | Status | Notes                                                                          |
|--------|-----------------------------------|-------------------------------------|--------|--------------------------------------------------------------------------------|
| GET    | `/admin/login`                    | template                            | **N**  | HTML form.                                                                      |
| POST   | `/admin/login`                    | session                             | **N**  | Auth.                                                                           |
| GET    | `/admin/logout`                   | session                             | **N**  | Auth.                                                                           |
| GET    | `/admin/`, `/admin/dashboard`     | private + public tables             | **W**  | Admin dashboard reads import_history, table sizes — works with `etl_user`.       |
| GET/POST | `/admin/upload`                 | CSV → ETL                           | **N**  | File upload.                                                                    |
| POST   | `/admin/import`                   | ETL                                 | **N**  | Triggers ETL.                                                                   |
| POST   | `/admin/refresh`                  | `REFRESH MATERIALIZED VIEW`         | **W**  | Should run, but won't fix the value_text bug since the EAV pivot is upstream.    |
| GET    | `/admin/history`                  | `import_history`                    | **W**  | Works.                                                                           |
| POST   | `/admin/history/clear`            | DELETE                              | **N**  | Admin action.                                                                   |
| GET    | `/admin/api/import-progress`      | in-memory                           | **W**  | Works.                                                                           |
| GET    | `/admin/api/import-status/{id}`   | `import_history`                    | **W**  | Works.                                                                           |
| GET    | `/admin/api/table-counts`         | `pg_stat_user_tables` + COUNT       | **W**  | Works (will report empty raw_*, populated private.*).                            |
| GET    | `/admin/data-quality`             | raw_* + private                     | **P**  | HTML view of quality.                                                           |
| GET    | `/admin/cleansing-report`         | raw_* + import_history              | **P**  | HTML view.                                                                      |
| GET    | `/admin/cross-stats`              | private.* cross-tables              | **U**  | Reads private — depends on schema.                                              |
| GET    | `/admin/logs`                     | log file                            | **N**  | Stream logs.                                                                    |
| POST   | `/admin/erasure`                  | private.erasure_request             | **N**  | PDPA erasure flow.                                                              |
| GET    | `/admin/agreement`                | template                            | **N**  | HTML.                                                                           |
| GET/POST | `/admin/upload-bundle`          | CSV bundle ETL                      | **N**  | File upload.                                                                    |

### 3.26 `system` — top-level

| Method | Path        | Source         | Status | Notes                                          |
|--------|-------------|----------------|--------|------------------------------------------------|
| GET    | `/health`   | DB ping        | **W**  | DB connectivity confirmed.                     |

---

## 4 · Cross-cutting concerns and patterns

### 4.1 Pattern A — "Hits `mv_visit_resolved` directly via UNIFIED_CTE"

Endpoints that build their `unified` CTE through `services.unified_screening.build_unified_cte()`:
- `/summary/overview`
- `/summary/zones`
- `/summary/districts`
- `/summary/non-bangkok-overview`
- `/executive/headline-kpi`

These return correct `total_screened` / `total_visits` / per-bucket counts (because `mv_visit_resolved` has 35,111 rows), but ALL `risk_*`/`found_*` boolean flags evaluate FALSE because `private.visit_measurement.value_boolean` is uniformly NULL (the bootstrap classified every variable as `text` or `code`). Fix: re-run the EAV pivot so `value_boolean` and `value_number` get populated, then `REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_visit_resolved`.

### 4.2 Pattern B — "Reads from `summary_district_disease` view"

A wide swath of endpoints read this view (`research/export`, `kpi/*`, `executive/*`, `strategy/*`, `disease-control/screening-coverage`, `gis/heatmap/*`, `public/*`, `search/districts`). The view itself isn't broken — it returns 70 rows and `total_screened` is correct (29,979). But every disease count column is 0 because the underlying `mv_visit_resolved.risk_dm`/etc are FALSE. Fix is upstream of the view.

### 4.3 Pattern C — "Reads legacy `raw_vitalsigns` / `raw_lab_results` / `raw_homehealth` / `raw_homevisit`"

These public tables are real tables (not views) but contain **0 rows** post-migration to the EAV `private.*` schema. Endpoints:
- `/trends/screening`, `/trends/disease/*`
- `/epidemiology/incidence-rate`, `/epidemiology/outbreak-detection`
- `/disease-control/repeat-screening`, `/progression`, `/referral-outcome`, `/treatment-compliance`
- `/executive/yoy-comparison`
- `/kpi/progress-tracker`
- `/promotion/exercise-frequency`, `/promotion/diet-disease-correlation`
- `/research/individual-data`
- `/summary/non-bangkok-province/{code}` and lab/mental detail in `/non-bangkok-overview`
- `/summary/fiscal-years`
- `/summary/filtered` raw path
- `/monitoring/data-quality`, `/monitoring/cleansing-report` (which actually report this state correctly)

These need either re-population of the `raw_*` legacy tables (an ETL replay step) or rewrites against the `private.*` EAV schema and `mv_visit_resolved`.

### 4.4 Pattern D — "References a view that does not exist"

Five views are named in router code but do not exist in PostgreSQL: `summary_disease_age_sex`, `summary_lab_disease_cross`, `summary_comorbidity`, `summary_bmi_waist`, `summary_disease_control`. All callers return HTTP 500. These need to be created (and rewired against `mv_visit_resolved` once the boolean/numeric pivot is fixed).

### 4.5 Pattern E — "Lab view filters on `value_number`"

`public.summary_district_lab` joins `private.lab_measurement` with `WHERE lm.value_number IS NOT NULL`. Because every measurement is in `value_text`, the filter excludes everything → 0 rows → `/summary/lab` returns `[]`, lab AVGs missing across `/non-bangkok-*`, `/research/correlation-matrix`, etc.

### 4.6 Pattern F — "Reads `ref_facilities` (empty)"

`ref_facilities` has 0 rows, so all of:
- `/gis/facilities*`, `/gis/facility-types`
- `/gis/heatmap/*` (centroids = NULL)
- `/public/screening-locations`
- `/strategy/resource-optimization` (clinic_count = 0)
- `/zone/{zc}/dashboard` (facility list empty)

return empty/null facility data. This is independent of the EAV bug — it's a separate seed-data gap.

### 4.7 Pattern G — "Modeled / synthetic numbers"

`routers/factors.py`, `routers/screening_tests.py`, parts of `routers/dashboard_v1.py` use `data_adapter.load_district_data()` and then *model* numbers via `_seed_for(dcode) + _vary(...)`. These return numerically plausible outputs even when the underlying data is 0, but the modeled numbers are derived from `pct_at_risk = 0`, so the chi-squares, completion rates, abnormal counts are all centered at zero or at the modifier's base value. Fixing the data fixes these too.

### 4.8 Pattern H — "Pure ref-table reads (still WORK)"

Endpoints that touch only `ref_districts.population` / `ref_health_zones` / static configs / Redis are unaffected. Examples that still work fully: `/disease-control/screening-coverage`, `/facility/capacity-planning`, `/public/health-tips`, `/research/sample-size-calculator`, `/research/data-dictionary`, `/monitoring/*`, `/health`, `/admin/*` (where they only read import_history / table counts).

### 4.9 Pattern I — "Cache poisoning"

Cache hit rate is ~74 %. Once the data bug is fixed, the wrong (zero) numbers will continue to be returned for the duration of each TTL bucket:
- `TTL_T1_EXTERNAL` (5 min) — PM2.5
- `TTL_T2_AGGREGATE` (15 min) — most summary endpoints
- `TTL_T3_FILTERED` (30 min) — district health card, filtered queries
- `TTL_T4_STATIC` (24 h) — fiscal-years, zone meta, district list

Mitigation: call `POST /api/admin/invalidate-cache` after the data fix, or clear Redis directly.

---

## 5 · Recovery checklist (out of scope for this audit, listed for reference)

1. Fix `private.variable_definition.data_type` so numeric / boolean variables are correctly typed (currently all `text` or `code`).
2. Re-run the EAV pivot to backfill `value_number` and `value_boolean` from `value_text` (3.28 M rows in `private.visit_measurement`, ~similar in `lab_measurement`).
3. `REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_visit_resolved` (and the four other partial MVs).
4. Create the five missing views: `summary_disease_age_sex`, `summary_lab_disease_cross`, `summary_comorbidity`, `summary_bmi_waist`, `summary_disease_control`.
5. Backfill `ref_facilities` from the original 14k clinic_latlong dataset.
6. Decide: keep `raw_vitalsigns`/`raw_lab_results`/etc. as legacy, or rewrite the ~30 endpoints that read them against the `private.*` EAV (preferred — single source of truth).
7. `POST /api/admin/invalidate-cache` to flush stale zero values.
8. Re-run smoke tests and update this audit.
