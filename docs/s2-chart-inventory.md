# S2 Dashboard Refactor — Chart Component Inventory

**Report Generated:** 2026-05-01  
**Scope:** All chart-rendering React components in `/Users/dev/bma-health/frontend/src/components/sidebar/`, `/components/map/stats/`, plus dashboard `ChartCard`.

---

## Executive Summary

- **Total chart components:** 19 (14 in sidebar, 4 in map/stats, 1 dashboard wrapper)
- **Total frontend LOC:** 3,140 (2,738 sidebar + 372 map/stats + 30 ChartCard)
- **Total API endpoints:** 14 unique `/api/v2/*` endpoints queried
- **Total backend LOC:** ~1,200 (across 4 routers: promotion, epidemiology, disease_control, facility)
- **ECharts kinds in use:** `pie`, `heatmap`, `bar` (horizontal & vertical), `boxplot`, `scatter`, `line`
- **Common filters:** `district`, `zone_code` (sometimes `sex`, `age_band`)

---

## Frontend Chart Components

### Sidebar Components (14)

#### 1. RiskFactorProfile
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/sidebar/RiskFactorProfile.tsx` |
| Lines | 50 |
| Endpoint | `/api/v2/promotion/risk-factor-profile` |
| JSON Shape | `{ data?: [ { factor\|name\|label, count\|value\|pct } ] }` |
| ECharts Kind | `pie` (donut variant with `radius: ['40%', '70%']`) |
| Filters | `district`, `zone_code` (via query params in backend, not frontend props) |
| Hardcoded Values | Radius `['40%', '70%']`, label fontSize 9, tooltip formatter `{b}: {c} ({d}%)` |
| Edge Cases | Loading, null data, `data_available === false`, empty array |

#### 2. AgeSexPyramid
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/sidebar/AgeSexPyramid.tsx` |
| Lines | 50 |
| Endpoint | `/api/v2/epidemiology/age-pyramid` |
| JSON Shape | `{ groups\|age_groups?: [ { age_group\|label\|group, male\|male_count, female\|female_count } ] }` |
| ECharts Kind | `bar` (horizontal, stacked population pyramid) |
| Filters | None on frontend (backend supports `district`, `zone_code`) |
| Hardcoded Values | Blue `#3b82f6` (male), Pink `#ec4899` (female), grid `{left:60,right:20,top:10,bottom:20}`, fontSize 9 |
| Edge Cases | Loading, null data, empty array |

#### 3. ComorbidityMatrix
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/sidebar/ComorbidityMatrix.tsx` |
| Lines | 121 |
| Endpoint | `/api/v2/epidemiology/multi-disease-matrix` |
| JSON Shape | `{ data?: [ { district_code, total_screened, dm_and_hpt, dm_and_obesity, ... } ] }` |
| ECharts Kind | None (rendered as HTML `<table>`) |
| Filters | None (aggregates all districts in response) |
| Hardcoded Values | Colors: `bg-red-200` (pct >= 10), `bg-orange-100` (pct >= 5), `bg-yellow-50` (pct >= 1), `bg-gray-50` (< 1) |
| Edge Cases | Loading, null/empty data |

#### 4. DiseaseLabCrosstab
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/sidebar/DiseaseLabCrosstab.tsx` |
| Lines | 223 |
| Endpoint | `/api/v2/epidemiology/disease-lab-crosstab` |
| JSON Shape | `{ data?: [ { district_code, avg_fbs_dm_positive, avg_fbs_dm_negative, n_fbs_dm_positive, n_fbs_dm_negative, ... } ] }` |
| ECharts Kind | None (rendered as horizontal bars with CSS, no ECharts) |
| Filters | None (aggregates all districts) |
| Hardcoded Values | Red `bg-red-400` (positive), Green `bg-emerald-400` (negative), thresholds (e.g., FBS ≥126 mg/dL) |
| Edge Cases | Loading, null/empty data, skips hemoglobin duplicate rows |

#### 5. BehaviorDisease
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/sidebar/BehaviorDisease.tsx` |
| Lines | 49 |
| Endpoint | `/api/v2/promotion/behavior-disease-correlation` |
| JSON Shape | `{ matrix: number[][], behaviors: string[], diseases: string[] }` |
| ECharts Kind | `heatmap` |
| Filters | None (backend supports `behavior`, `disease`, `district`) |
| Hardcoded Values | ColorMap `['#f7f7f7', '#fee08b', '#d73027']`, grid left 80, bottom 50 |
| Edge Cases | Loading, data validation (checks array structure), conditional label display |

#### 6. ExerciseFrequency
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/sidebar/ExerciseFrequency.tsx` |
| Lines | 49 |
| Endpoint | `/api/v2/promotion/exercise-frequency` |
| JSON Shape | `{ categories\|frequencies?: [ { count\|value, label\|frequency\|category } ], data_available?: boolean }` |
| ECharts Kind | None (horizontal bars with CSS) |
| Filters | None on frontend |
| Hardcoded Values | Colors: `['bg-emerald-500', 'bg-blue-500', 'bg-amber-500', 'bg-red-500', 'bg-gray-400']` (cycling) |
| Edge Cases | Loading, `data_available === false`, empty array |

#### 7. ScreeningCoverage
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/sidebar/ScreeningCoverage.tsx` |
| Lines | 99 |
| Endpoint | `/api/v2/disease-control/screening-coverage` |
| JSON Shape | `{ screened, target, coverage_pct } \| [ { dcode, name_th, population, screened, coverage_pct } ]` (aggregates if array) |
| ECharts Kind | None (single progress bar) |
| Filters | None on frontend |
| Hardcoded Values | Green `#22c55e` (≥80%), Amber `#eab308` (≥50%), Red `#ef4444` (<50%) |
| Edge Cases | Loading, error, null data, aggregation from array |

#### 8. RepeatScreening
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/sidebar/RepeatScreening.tsx` |
| Lines | 89 |
| Endpoint | `/api/v2/disease-control/repeat-screening` |
| JSON Shape | `{ once, twice, three_plus, unique_patients, total_visits, data_available? }` |
| ECharts Kind | None (stacked horizontal bar) |
| Filters | None on frontend |
| Hardcoded Values | Colors: `'#22c55e'` (1x), `'#3b82f6'` (2x), `'#8b5cf6'` (3+) |
| Edge Cases | Loading, error, `data_available === false`, null counts |

#### 9. FacilityCard
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/sidebar/FacilityCard.tsx` |
| Lines | 74 |
| Endpoint | `/api/v2/facility/screening-yield-rank` |
| JSON Shape | `{ data?: [ { facility_code, facility_name, screened, yield_pct } ] }` |
| ECharts Kind | None (HTML table) |
| Filters | None (backend supports `zone_code`, `disease`) |
| Hardcoded Values | Yield colors: Red >30%, Amber >15%, Green ≤15% |
| Edge Cases | Loading, error, empty data, takes top 8 facilities |

#### 10. RankingView
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/sidebar/RankingView.tsx` |
| Lines | 116 |
| Endpoint | None (uses local `healthData`, `zoneHealthData` props) |
| JSON Shape | N/A (client-side only) |
| ECharts Kind | None (horizontal progress bars) |
| Filters | Reads `currentLevel`, `currentZoneCode`, `activeHeatmap` from mapStore |
| Hardcoded Values | Risk color mapping via `getRiskHex()` function |
| Edge Cases | Null data, no heatmap selected, empty items |

#### 11. CompareView
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/sidebar/CompareView.tsx` |
| Lines | 133 |
| Endpoint | None (uses local `healthData`, `zoneHealthData` props) |
| JSON Shape | N/A (client-side only) |
| ECharts Kind | None (HTML tables) |
| Filters | Reads `currentLevel`, `currentZoneCode`, `activeHeatmap` from mapStore |
| Hardcoded Values | Top 5 Red `bg-red-50`, Bottom 5 Green `bg-green-50` |
| Edge Cases | Null data, no heatmap selected, empty items |

#### 12. DiseaseDetail
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/sidebar/DiseaseDetail.tsx` |
| Lines | 105 |
| Endpoint | None (uses local `healthData` prop + IndicatorCard child) |
| JSON Shape | N/A (main is client-side; delegates to IndicatorCard for charts) |
| ECharts Kind | None (text display + IndicatorCard subcomponent) |
| Filters | Reads `currentDistrictCode`, `selectedDisease`, `activeHeatmap` from mapStore |
| Hardcoded Values | Risk color threshold: >30% red, >15% amber, ≤15% green |
| Edge Cases | Null district, missing disease data |

#### 13. IndicatorCard
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/sidebar/IndicatorCard.tsx` |
| Lines | 91 |
| Endpoint | None (receives `indicator` prop) |
| JSON Shape | `{ label, mean, cutoff, unit, direction, zones?: [ { max, color } ], pct_above_cutoff\|pct_below_cutoff, pct_pre_cutoff? }` |
| ECharts Kind | None (custom bar + cutoff lines) |
| Filters | None (prop-driven) |
| Hardcoded Values | Bar height 5px, cutoff line 2px, zones colored with opacity 0.3, triangle marker `#BMA_GREEN` |
| Edge Cases | No zones, null pre_cutoff, empty percent values |

#### 14. PM25Summary
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/sidebar/PM25Summary.tsx` |
| Lines | 518 |
| Endpoint | Multiple custom hooks (`usePM25Zones`, `usePM25Districts`, `usePM25Monthly`, `usePM25Health`) |
| JSON Shape | Complex nested structures with `current_snapshot`, `standards`, `avg_pm25`, `avg_aqi`, `station_count`, `trend` |
| ECharts Kind | None (HTML cards + progress bars) |
| Filters | Reads `currentZoneCode`, `currentDistrictCode`, `currentLevel` from mapStore; conditionally fetches based on drill-down |
| Hardcoded Values | AQI levels: 0–50 (green), 51–100 (amber), 101–150 (orange), 151–200 (red); PM2.5 standards TH 37.5, WHO 15 |
| Edge Cases | Null PM2.5 values, unavailable/stale data, upstream error states, retry handling |

#### 15. TrendChart (Shared Component)
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/sidebar/TrendChart.tsx` |
| Lines | 128 |
| Endpoint | None (receives `data` array as prop) |
| JSON Shape | `[ { period: string, value: number, [key: string]: unknown } ]` |
| ECharts Kind | `line` with gradient fill and reference lines |
| Filters | Prop-driven: `dataKey`, `color`, `unit`, `referenceLines`, `additionalLines`, `lang` |
| Hardcoded Values | Grid `{left:35, right:10, top:10, bottom:25}`, gradient `color + '20'` to `color + '05'`, symbolSize 4 |
| Edge Cases | Empty data, null values in series, reference lines optional |

#### 16. ZoneSummary
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/sidebar/ZoneSummary.tsx` |
| Lines | 211 |
| Endpoint | None (uses local `zoneHealthData` prop; composes multiple sub-components) |
| JSON Shape | N/A (routes to child components) |
| ECharts Kind | None (text + child components: NcdCascade, RepeatScreening, etc.) |
| Filters | Reads `currentZoneCode` from mapStore; passes data to children |
| Hardcoded Values | Within-zone variation warning if max−min > 15% |
| Edge Cases | Null zone, null health data |

#### Extra: NcdCascade & WaistRisk
| Field | Value |
|-------|-------|
| Files | `NcdCascade.tsx` (73), `WaistRisk.tsx` (63) |
| Endpoints | `/api/v2/disease-control/ncd-cascade`, `/api/v2/promotion/waist-risk-analysis` |
| JSON Shapes | `{ cascade: [ { step, label_th, count, pct_of_screened } ] }`, `{ male: { pct_over, count, total }, female: {...} }` |
| ECharts Kinds | None (horizontal bars with CSS) |
| Filters | `disease` param (NcdCascade), `zone_code` (WaistRisk) |
| Hardcoded Values | Cascade bar gradient HSL, Male blue `#3b82f6`, Female pink `#ec4899` |
| Edge Cases | `data_available === false`, null counts, threshold-based risk logic |

---

### Map/Stats Components (4)

#### 1. BoxPlotChart
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/map/stats/BoxPlotChart.tsx` |
| Lines | 99 |
| Endpoint | None (receives `data: BoxPlotItem[]` prop) |
| JSON Shape | `[ { zoneName, min, q1, median, q3, max, outliers: number[] } ]` |
| ECharts Kind | `boxplot` + `scatter` (for outliers) |
| Filters | None (prop-driven) |
| Hardcoded Values | Grid `{left:90, right:20, top:10, bottom:30}`, box fill `#e6f5ef`, border `BMA_GREEN`, outlier `#ef4444` |
| Edge Cases | Empty data, no outliers |

#### 2. DescriptiveStats
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/map/stats/DescriptiveStats.tsx` |
| Lines | 99 |
| Endpoint | None (receives `stats: DescriptiveResult` prop) |
| JSON Shape | `{ mean, median, stdDev, iqr, range: [min, max], cv, n }` |
| ECharts Kind | None (grid of stat cards with info tooltips) |
| Filters | None (prop-driven) |
| Hardcoded Values | Grid 3 columns, font sizes 10px (label) / 14px (value), info box bg `#333` |
| Edge Cases | n === 0 (returns null) |

#### 3. FactorComparison
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/map/stats/FactorComparison.tsx` |
| Lines | 81 |
| Endpoint | None (receives `data: ZoneFactorItem[]` prop) |
| JSON Shape | `[ { zoneName, value } ]` |
| ECharts Kind | `bar` (horizontal) |
| Filters | None (prop-driven: `factorLabel`, `unit`) |
| Hardcoded Values | Grid `{left:70, right:40, top:5, bottom:5}`, bar color `BMA_GREEN`, barWidth `'55%'` |
| Edge Cases | Empty data, no unit |

#### 4. ZoneComparisonBar
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/map/stats/ZoneComparisonBar.tsx` |
| Lines | 93 |
| Endpoint | None (receives `data: ZoneRankItem[]`, `avgPct: number` props) |
| JSON Shape | `[ { zoneName, pct } ]` |
| ECharts Kind | `bar` (horizontal) with `markLine` (city avg reference) |
| Filters | None (prop-driven: `lang` for label) |
| Hardcoded Values | Grid `{left:70, right:45, top:5, bottom:5}`, colors from `CHART_PALETTE`, barWidth `'60%'`, markLine dashed gray |
| Edge Cases | Empty data |

---

### Dashboard Components (1)

#### ChartCard (Wrapper)
| Field | Value |
|-------|-------|
| File | `/Users/dev/bma-health/frontend/src/components/dashboard/ChartCard.tsx` |
| Lines | 30 |
| Endpoint | None (wrapper for children) |
| JSON Shape | N/A |
| ECharts Kind | None (container) |
| Filters | Props: `title`, `subtitle`, `loading`, `error`, `className` |
| Hardcoded Values | Border radius `rounded-xl`, ring `ring-gray-200`, dark `dark:ring-slate-700`, spinner color `border-bma-green` |
| Edge Cases | `loading === true` (shows spinner), `error === true` (shows error text) |

---

## Backend API Endpoints

### Router: `/api/v2/promotion` (promotion.py)

#### 1. `/risk-factor-profile`
| Field | Value |
|-------|-------|
| HTTP | GET |
| Query Params | `district?` (str), `zone_code?` (str) |
| SQL | `summary_district_risk_factors` (smoking, exercise counts) |
| Transformations | Aggregates by `district_code`, computes `pct_smoking`, `pct_no_exercise` |
| Response | `{ district, zone_code, data: [ { district_code, total, smoking_count, pct_smoking, pct_no_exercise } ] }` |

#### 2. `/behavior-disease-correlation`
| Field | Value |
|-------|-------|
| HTTP | GET |
|Query Params | `behavior` (smoking\|alcohol\|exercise), `disease` (diabetes\|hypertension\|...), `district?` |
| SQL | `summary_district_risk_factors` or `app1_vitalsignslf`+`portal_vitalsignslf` UNION (behavior) or `app1_homehealth` UNION (alcohol) |
| Transformations | Groups by behavior_value, adds `behavior_label` map (codebook), enforces k-anonymity ≥5 |
| Response | `{ behavior, disease, district, data: [ { behavior_value, behavior_label, total } ] }` |

#### 3. `/exercise-frequency`
| Field | Value |
|-------|-------|
| HTTP | GET |
| Query Params | `district?` (str) |
| SQL | `app1_homehealth` UNION `portal_homehealth` (exercise codes 1/2/3) |
| Transformations | Groups by exercise code, counts patients, k-anonymity filter ≥5 |
| Response | `{ data: [ { district_code?, exercise_3plus, exercise_less3, exercise_never, total } ] }` |

#### 4. `/waist-risk-analysis`
| Field | Value |
|-------|-------|
| HTTP | GET |
| Query Params | `zone_code?` (str) |
| SQL | `summary_bmi_waist` (male_waist_risk, female_waist_risk counts) |
| Transformations | Aggregates risk counts by district, computes `pct_at_risk`, k-anonymity ≥5 |
| Response | `{ thresholds: { male: ">90 cm", female: ">80 cm" }, zone_code, data: [ {...} ] }` |

### Router: `/api/v2/epidemiology` (epidemiology.py)

#### 5. `/age-pyramid`
| Field | Value |
|-------|-------|
| HTTP | GET |
| Query Params | `district?`, `zone_code?` |
| SQL | `summary_disease_age_sex` (pivoted: age_group × sex → counts) |
| Transformations | Pivots `{age_group, male_count, female_count}`, filters by `sex IS NOT 'all'`, k-anonymity ≥5 |
| Response | `{ data: [ { age_group, male_count, female_count } ] }` |

#### 6. `/disease-lab-crosstab`
| Field | Value |
|-------|-------|
| HTTP | GET |
| Query Params | `district?`, `zone_code?` |
| SQL | `summary_lab_disease_cross` (avg lab values stratified by disease status) |
| Transformations | Direct query with k-anonymity ≥5 filter on `total_patients` |
| Response | `{ data: [ { district_code, total_patients, avg_fbs_dm_positive, avg_fbs_dm_negative, ... } ] }` |

#### 7. `/multi-disease-matrix`
| Field | Value |
|-------|-------|
| HTTP | GET |
| Query Params | `district?`, `zone_code?` |
| SQL | `summary_comorbidity` (disease pair counts: dm_and_hpt, dm_and_obesity, etc.) |
| Transformations | Direct query, k-anonymity ≥5 on `total_screened` |
| Response | `{ data: [ { district_code, total_screened, dm_only, hpt_only, dm_and_hpt, ... } ] }` |

### Router: `/api/v2/disease-control` (disease_control.py)

#### 8. `/screening-coverage`
| Field | Value |
|-------|-------|
| HTTP | GET |
| Query Params | `zone_code?` |
| SQL | `ref_districts` LEFT JOIN `summary_district_disease` |
| Transformations | Sums `total_screened`, computes `coverage_pct = 100 × screened / population`, k-anonymity ≥5 |
| Response | `{ data: [ { dcode, name_th, zone_code, population, screened, coverage_pct } ] }` |

#### 9. `/ncd-cascade`
| Field | Value |
|-------|-------|
| HTTP | GET |
| Query Params | `disease` (diabetes\|hypertension\|...) |
| SQL | `summary_district_disease` (aggregates total_screened, risk_*_count, found_*_count) |
| Transformations | Builds cascade array: screened → at_risk → diagnosed → (treatment: null), computes pct_of_screened |
| Response | `{ disease, cascade: [ { step, label_th, count\|null, pct_of_screened } ] }` |

#### 10. `/repeat-screening`
| Field | Value |
|-------|-------|
| HTTP | GET |
| Query Params | `district?` |
| SQL | `public.mv_visit_resolved` (each row = one visit; groups by patient_id → visit_count) |
| Transformations | Counts patients by visit frequency, k-anonymity ≥5 |
| Response | `{ district?, data: [ { visit_count, patient_count } ], data_available: bool }` |

### Router: `/api/v2/facility` (facility.py)

#### 11. `/screening-yield-rank`
| Field | Value |
|-------|-------|
| HTTP | GET |
| Query Params | `zone_code?`, `disease` (default="diabetes") |
| SQL | `summary_facility` joined with `ref_districts` |
| Transformations | Computes `yield_pct = 100 × risk_* / total_screened`, adds rank index |
| Response | `{ disease, facilities: [ { facility_code, district_code, total_screened, risk_count, yield_pct, rank } ] }` |

---

## Summary Table: Front-to-Back Mapping

| Frontend Component | Endpoint | Backend Router | Query MVs/Tables | Filters (Frontend → Backend) |
|---|---|---|---|---|
| RiskFactorProfile | `/api/v2/promotion/risk-factor-profile` | promotion.py | `summary_district_risk_factors` | none (hardcoded GET) |
| AgeSexPyramid | `/api/v2/epidemiology/age-pyramid` | epidemiology.py | `summary_disease_age_sex` | none |
| ComorbidityMatrix | `/api/v2/epidemiology/multi-disease-matrix` | epidemiology.py | `summary_comorbidity` | none |
| DiseaseLabCrosstab | `/api/v2/epidemiology/disease-lab-crosstab` | epidemiology.py | `summary_lab_disease_cross` | none |
| BehaviorDisease | `/api/v2/promotion/behavior-disease-correlation` | promotion.py | `summary_district_risk_factors` or `app1_vitalsignslf` UNION | none (hardcoded GET) |
| ExerciseFrequency | `/api/v2/promotion/exercise-frequency` | promotion.py | `app1_homehealth` UNION `portal_homehealth` | none |
| ScreeningCoverage | `/api/v2/disease-control/screening-coverage` | disease_control.py | `ref_districts` + `summary_district_disease` | none |
| RepeatScreening | `/api/v2/disease-control/repeat-screening` | disease_control.py | `public.mv_visit_resolved` | none |
| FacilityCard | `/api/v2/facility/screening-yield-rank` | facility.py | `summary_facility` + `ref_districts` | none |
| WaistRisk | `/api/v2/promotion/waist-risk-analysis` | promotion.py | `summary_bmi_waist` | none |
| NcdCascade | `/api/v2/disease-control/ncd-cascade` | disease_control.py | `summary_district_disease` | none |
| RankingView | (local data) | N/A | N/A | `currentZoneCode`, `activeHeatmap` (mapStore) |
| CompareView | (local data) | N/A | N/A | `currentZoneCode`, `activeHeatmap` (mapStore) |
| DiseaseDetail | (local data) | N/A | N/A | `currentDistrictCode`, `selectedDisease` (mapStore) |
| IndicatorCard | (local data) | N/A | N/A | N/A (prop-driven) |
| PM25Summary | (custom hooks) | `/api/v2/pm25/*` (separate) | BMA ArcGIS + local cache | `currentZoneCode`, `currentDistrictCode`, `currentLevel` (mapStore) |
| TrendChart | (reusable) | N/A | N/A | N/A (prop-driven) |
| ZoneSummary | (composes children) | Multiple (see children) | N/A | (routes to children) |
| BoxPlotChart | (prop-driven) | N/A | N/A | N/A (prop: `data`, `diseaseLabel`) |
| DescriptiveStats | (prop-driven) | N/A | N/A | N/A (prop: `stats`, `lang`) |
| FactorComparison | (prop-driven) | N/A | N/A | N/A (prop: `data`, `factorLabel`, `unit`) |
| ZoneComparisonBar | (prop-driven) | N/A | N/A | N/A (prop: `data`, `avgPct`, `lang`) |
| ChartCard | (wrapper) | N/A | N/A | (props: `title`, `loading`, `error`) |

---

## K-Anonymity & Data Masking

All backend endpoints enforce k-anonymity threshold **≥ 5** (defined as `K_ANONYMITY_THRESHOLD` in `security.py`):

- Rows with `total_screened` < 5, `total_patients` < 5, or similar count fields < 5 are **filtered out** before response.
- Small scalar values are **suppressed** (replaced with `None`) via `suppress_scalar_if_small()`.
- Frontend components handle `None` / null gracefully (show "ไม่มีข้อมูล" or omit fields).

---

## Hardcoded Design Constants

**Colors (Tailwind + Custom Hex):**
- BMA Green: `#16A34A` (CSS var `--bma-green` → `#16A34A` in `data/constants.ts`)
- Risk Scale: Red >30%, Amber 15–30%, Green <15%
- PM2.5 AQI: Green (0–50), Amber (51–100), Orange (101–150), Red (151–200)
- Gender: Blue Male `#3b82f6`, Pink Female `#ec4899`

**Typography:**
- Axis labels: 9px (most charts)
- Info icons: 10px
- Grid labels: 9–11px

**Layout (ECharts Grid):**
- Standard chart: `{left: 35, right: 10, top: 10, bottom: 25}`
- Age Pyramid: `{left: 60, right: 20, top: 10, bottom: 20}`
- Box Plot: `{left: 90, right: 20, top: 10, bottom: 30}`
- Horizontal bars: `{left: 70, right: 40/45, top: 5, bottom: 5}`

---

## Total LOC Replacement Summary

- **Frontend (browser):** 3,140 LOC
  - Sidebar components: 2,738 LOC
  - Map/stats components: 372 LOC
  - ChartCard wrapper: 30 LOC
- **Backend (Python/SQL):** ~1,200 LOC
  - promotion.py: ~350 LOC
  - epidemiology.py: ~200 LOC
  - disease_control.py: ~400 LOC
  - facility.py: ~150 LOC
- **Grand Total:** ~4,340 LOC

---

## Notes for YAML Config Generation

1. **Endpoint Standardization:** All endpoints follow `/api/v2/{module}/{action}` pattern. Response wrapper is `{ data?: [...] }` or direct array.
2. **Filter Propagation:** Most components don't currently support runtime filters—they query global data. Future refactor should enable district/zone filters in:
   - RiskFactorProfile (backend has `district`, `zone_code`)
   - AgeSexPyramid (backend supports filters)
   - BehaviorDisease (backend supports `behavior`, `disease`, `district`)
   - ExerciseFrequency (backend supports `district`)
3. **Cache Strategy:** No client-side caching visible. Backend uses SQL queries with `execute_query()` (no Redis). Consider adding `@router.get(..., response_model=..., include_in_schema=...)` cache headers if needed.
4. **Error Handling:** Frontend components catch null/empty data and show loading/error states. Backend returns `{ data_available: false, message: "..." }` for data gaps.
5. **Type Safety:** TypeScript interfaces in sidebar components are often **duck-typed** (access `.data`, `.matrix`, `.factors`, etc. with optional chaining). Backend response shapes should be formalized in `pydantic` models for contract clarity.

---

**Document End**
