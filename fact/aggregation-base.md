# Aggregation Base — Tier 1 KPI Spec (revised 2026-04-27)

## Decision

All public-facing aggregates group by **เขตที่อยู่ตามทะเบียนบ้าน**
(registered home district) regardless of source. Each source uses a
different column name in its own CSV, but they all map to one DB column:
`raw_homevisit.home_district`.

| Source | Spec column (per CSV) | Visits source | DB resolution chain |
|--------|----------------------|---------------|--------------------|
| **Portal** | `HDISTRICT` (schema, 2%) / `DISTRICT` (used in system, ~88%) | `vital.PID + VSTDATE` | `home_district → current_district → work_district → raw_patients.district_code` |
| **App1**   | `DISTRICT` (~86%) | `vital.PID + VSTDATE` | `home_district → raw_patients.district_code` |
| **App2**   | `DISTRICT` (column exists, mostly NULL) | `HD` (raw_homehealth) | `home_district → work_district → raw_patients.district_code` |

`NULLIF(_, 9999)` is applied at each step (9999 = sentinel for "no district / out of BKK / unknown").

**All records** (BKK, non-BKK, and unknown) flow into `unified`, each
tagged with a `bucket` column so the headline "เลขรีพอร์ต" matches the
project total. Bucket meaning:

| bucket    | Resolved dc                  | Use                                       |
|-----------|------------------------------|-------------------------------------------|
| `bkk`     | 1001..1050                   | Drives `/zones`, `/districts`, choropleth |
| `non_bkk` | other province (e.g. 1101…)  | Counted in `/overview` total; detail in `/non-bangkok-overview` |
| `unknown` | NULL after full fallback     | Counted in total; no zone to attribute    |

`/overview` returns the ALL total + a `breakdown` field with per-bucket
counts. `/zones` and `/districts` JOIN to `ref_districts` so non-BKK and
unknown rows are auto-excluded — those endpoints stay BKK-only.

Cancelled records (`cancel_status = 1`) are excluded everywhere.

## Why the fallback chain for Portal

The Portal source CSV has multiple district fields, but the schema-canonical
`HDISTRICT` is barely populated (2%). The operational system instead uses
a field labelled `DISTRICT`, which lands in our `current_district` and
`work_district` columns depending on how the import maps them. We use a
priority fallback to recover the operational data:

```
COALESCE(home_district,        -- HDISTRICT, ~2%  — schema-correct but sparse
         current_district,      -- ปัจจุบันอยู่ที่, ~20% — close to home
         work_district,         -- ที่ทำงาน, ~88% — workplace fallback
         raw_patients.district_code)  -- registered patient master, 94% BKK
```

Trade-off: when the chain falls through to `work_district`, we're
counting the patient in their workplace's district rather than their
registered home. For most Bangkok residents work and home are in the same
zone, so the impact is small — but documented as a known approximation.

## Why raw_patients.district_code as final fallback

Added 2026-04-27. Coverage analysis revealed ~93K visits where the
homevisit row had no usable district info (NULL or 9999 in every column):

| Source | Orphan visits (no hv district) | Recoverable via raw_patients |
|--------|-------------------------------:|----------------------------:|
| Portal |                         79,970 |                79,970 (100%) |
| App1   |                            207 |                    107 (52%) |
| App2   |                         13,174 |                12,535 (95%) |
| **Total** |                    **93,351** |          **92,612 (99.2%)** |

`raw_patients.district_code` is the patient master's registered district
(populated for 94% of patients, **all values within 1001–1050**), so it's
a reliable proxy for "where the person lives". The remaining 739 visits
that can't be resolved anywhere stay in `unified` with `dc = NULL` and
appear in the `unknown_district` bucket on /overview.

Trade-off: the patient master may be stale relative to homevisit (the
person may have moved). But homevisit is what was in the form for that
specific visit; raw_patients is the registration record. For 99% of
orphans this is the only available signal.

## Coverage results (snapshot 2026-04-27, all-bucket)

After raw_patients fallback + bucket tagging, /overview reconciles to the
project total. Numbers below are after >30-day visit dedup (PID + VSTDATE):

| Source | Total persons | Total visits | BKK persons | BKK visits | Non-BKK persons | Non-BKK visits | Unknown |
|--------|--------------:|-------------:|------------:|-----------:|----------------:|---------------:|--------:|
| Portal |       400,850 |      471,433 |     379,212 |    448,366 |          24,535 |         23,067 |       0 |
| App1   |       354,586 |      375,471 |     331,797 |    351,999 |          24,612 |         23,372 |     100 |
| App2   |        34,543 |       34,624 |      33,904 |     33,985 |               0 |              0 |     639 |
| **All combined** | **789,979** | **881,528** | **744,913** | **834,350** | **49,147** | **46,439** | **739** |

Note: per-bucket persons sum may slightly exceed total (e.g. 794,799 ≠
789,979) because ~5K patients have visits across multiple buckets (e.g.
both BKK and non-BKK addresses on different visits) — they're counted
distinctly per bucket but only once in the cross-bucket total. Visits are
clean (each visit has exactly one bucket; 834,350 + 46,439 + 739 = 881,528).

This is a major improvement from the prior strict-`home_district`-only
base (341K patients) — a ~2.3× recovery.

## Pending data-quality work

1. **Portal**: investigate ETL — does `DISTRICT` from CSV land in
   `current_district` or `work_district`? Standardise to one column and
   document.
2. **App2**: ~35K records have `DISTRICT` column entirely NULL. Backfill
   from upstream (patient registry, ID card, or ask App2 dev to populate).
3. **All sources**: add a data-quality dashboard tile showing coverage %
   so quality regressions are visible.

## Why per-source instead of one base

The earlier (2026-04-22) attempt used `raw_homevisit.home_district` for ALL
sources. Coverage by source:
- Portal: 2.4% (11K of 480K records had home_district) → 97% of Portal lost
- App1: 100% ✅
- App2: 0% (no home_district at all) → all of App2 lost

Total dropped from 781K → 341K. The Tier 1 KPI spec from the team clarified
that each source records its district differently, and the dashboard must
respect those differences. The unified CTE restores 732K patients.

## Why home_district

| Question | Aggregation base | Example |
|----------|-----------------|---------|
| "เขตนี้สุขภาพประชาชนเป็นอย่างไร" | **home_district** ✅ | Health planning per residential population |
| "ศูนย์บริการสาธารณสุขในเขตนี้ทำงานเท่าไร" | screening district | Workload of each ศบส. (separate dashboard) |

The default user-facing question is **population health by residence**:
"คนที่บ้านอยู่ในเขตนี้ มีสถานะสุขภาพอย่างไร"

When a Bangkok resident travels to a different district to get screened
(e.g., works in central BKK, visits a hospital in a different zone), they
must still count toward the **เขตที่บ้านอยู่จริง** for population planning.
Counting by screening district would inflate hub-districts (where many
hospitals/ศบส. are concentrated) and undercount residential districts.

## Pattern that confirmed this

| District | Screening count (vitalsigns.district_code) | Resident count (homevisit.home_district) |
|----------|------------------------------------------|----------------------------------------|
| 1024 บางกะปิ | 42,793 (top — hub for screening) | far less |
| 1040 บางแค | 17,381 | **23,687** (top — actual residents) |

**บางกะปิ** is a screening hub (people commute there from elsewhere). **บางแค**
is an actual residential center where many people live but get screened
elsewhere. For the public dashboard, "Zone 1 has the most patients" should
mean "Zone 1 has the most residents who got screened", not "Zone 1 has the
most screening visits performed there".

## SQL pattern

```sql
SELECT
  d.dcode,
  COUNT(DISTINCT hv.patient_id) AS total_screened,
  COUNT(hv.id)                  AS total_visits,
  COUNT(DISTINCT hv.patient_id) FILTER (WHERE v.risk_dm) AS dm_count
FROM ref_districts d
LEFT JOIN raw_homevisit hv
  ON hv.home_district::text = d.dcode
  AND hv.cancel_status IS DISTINCT FROM 1
LEFT JOIN raw_vitalsigns v
  ON v.patient_id = hv.patient_id
  AND v.cancel_status IS DISTINCT FROM 1
GROUP BY d.dcode
```

Key elements:
1. **Base table = `raw_homevisit`** — provides `home_district`
2. **`home_district::text = d.dcode`** — type cast (homevisit uses INT, ref_districts uses VARCHAR)
3. **LEFT JOIN raw_vitalsigns** — adds disease flags / vitals; LEFT not INNER
   so residents who registered but haven't been screened still count toward
   `total_screened` (but with no risk flags)

## Coverage trade-off

| Metric | All-time, BKK |
|--------|---------------|
| Patients with vitalsigns (old base) | 781,728 |
| Patients with homevisit + home_district in BKK (new base) | **341,567** |
| Patients with vitalsigns BUT no homevisit | ~440K (residents unknown) |

The new base **excludes patients without recorded home_district** (about
440K). This is a known data-quality issue — those patients exist in
`raw_vitalsigns` but no `raw_homevisit` record. Reasons could be:
- Walk-in screening with no home registration
- Data lost in migration from older systems
- Foreign workers / out-of-province (home_district outside BKK)

For now, the dashboard reports the smaller but cleaner population (~340K
residents). If the project later requires the full ~780K population, add
a fallback that uses `vitalsigns.district_code` when `homevisit.home_district`
is missing — but this would mix the two aggregation bases.

## Visit count: PID + VSTDATE with >30-day dedup

**Spec (2026-04-27):** "ตัวแปรครั้งในโครงการ คือ PID + VSTDATE
กันเคสที่กรอกข้อมูลผิดแล้วกรอกใหม่ — PID เดิมซ้ำได้แต่ต้องเกิน 1 เดือน".

A "visit" = one (PID, visit_date) pair. The same PID may legitimately appear
multiple times **only if** consecutive visits are more than 30 days apart.
Consecutive same-PID rows within 30 days are treated as data-correction
duplicates and the later one is dropped.

### SQL implementation (in `services/unified_screening.py`)

The CTE generator emits a second CTE `unified_visits` alongside `unified`:

```sql
unified_visits AS (
  SELECT source, patient_id, day, dc
  FROM (
    SELECT source, patient_id, day, dc,
           LAG(day) OVER (PARTITION BY source, patient_id ORDER BY day) AS prev_day
    FROM (
      -- Step 1: Collapse JOIN multiplication. A patient with multiple
      -- raw_homevisit rows produces multiple unified rows for the same
      -- (source, patient_id, day) — DISTINCT ON keeps one (smallest dc).
      SELECT DISTINCT ON (source, patient_id, day)
             source, patient_id, day, dc
      FROM unified
      ORDER BY source, patient_id, day, dc
    ) collapsed
  ) lagged
  -- Step 2: Drop within-30-day duplicates
  WHERE prev_day IS NULL OR (day - prev_day) > 30
)
```

### Numbers (snapshot 2026-04-27)

| Source | Distinct (PID, day) | After >30d dedup | Dropped (within-30d) |
|--------|--------------------:|----------------:|---------------------:|
| Portal |             399,595 |          395,450 |               4,145 |
| App1   |             362,031 |          351,893 |              10,138 |
| App2   |              21,513 |           21,493 |                  20 |
| **Total** |          **783,139** |       **768,836** |          **14,303** |

App1 has the highest correction rate (~2.8%), Portal moderate (~1%), App2
near-zero. Total reduction is ~1.8% of raw visits.

### Usage by routers

| Router | What it uses | Why |
|--------|--------------|-----|
| `/overview` total_visits | `COUNT(*) FROM unified_visits` | global count (BKK + unknown) |
| `/overview` unknown_district | `COUNT(*) FROM unified_visits WHERE dc IS NULL` | bucket for unresolvable records |
| `/zones` total_visits | subquery: visits whose dc lives in zone | per-zone count (BKK only) |
| `/districts` total_visits | subquery: visits whose dc = district code | per-district count (BKK only) |
| `/overview` total_screened, /zones, /districts | `COUNT DISTINCT patient_id FROM unified` | persons unaffected by dedup |
| Risk-flag counts (diabetes, etc) | `unified` with FILTER | preserve all observations including corrections |

Risk flags use the un-deduped `unified` because a corrected entry might
upgrade a "no risk" record to "risk_dm = TRUE" — we want to keep that
observation, just not count it as a separate visit.

### Reconciliation: project total

`/overview` total now equals the project total directly — no addition
across endpoints needed:

```
/overview.total_visits   = 881,528  ← project total visits (after >30d dedup)
/overview.total_screened = 789,979  ← project total persons (cross-bucket distinct)

/overview.breakdown:
  bkk         = 834,350 visits / 744,913 persons
  non_bangkok =  46,439 visits /  49,147 persons (detail at /non-bangkok-overview)
  unknown     =     739 visits /     739 persons
```

The frontend dashboard headline shows `total_screened` / `total_visits`
plus a footnote rendering the breakdown ("กทม X · ตจว Y · ไม่ระบุ Z").
`/zones` and `/districts` continue to drive the BKK choropleth — they
return `bkk`-bucket counts only, which sums to `breakdown.bkk`.

## Code files derived from this

- `bma-health-db/api/services/unified_screening.py` — emits `unified` + `unified_visits` CTEs
- `bma-health-db/api/routers/summary.py` — `/overview`, `/filtered` use home_district + unified_visits
- `bma-health-db/api/routers/zones.py` — `/zones` uses home_district + unified_visits
- `bma-health-db/api/routers/districts.py` — `/districts` uses home_district + unified_visits
- (frontend doesn't need changes — it's transparent through the API)
