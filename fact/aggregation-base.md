# Aggregation Base — Tier 1 KPI Spec (revised 2026-04-27)

## Decision

All public-facing aggregates group by **เขตที่อยู่ตามทะเบียนบ้าน**
(registered home district) regardless of source. Each source uses a
different column name in its own CSV, but they all map to one DB column:
`raw_homevisit.home_district`.

| Source | Spec column (per CSV) | Visits source | DB resolution |
|--------|----------------------|---------------|---------------|
| **Portal** | `HDISTRICT` (schema, 2%) / `DISTRICT` (used in system, ~88%) | `vital.PID + VSTDATE` | `COALESCE(home_district, current_district, work_district)` |
| **App1**   | `DISTRICT` (~86%) | `vital.PID + VSTDATE` | `home_district` |
| **App2**   | `DISTRICT` (column exists, all NULL) | `HD` count | `home_district` (currently 0) |

A record is included only when the resolved district is BETWEEN 1001 AND 1050.

## Why the fallback chain for Portal

The Portal source CSV has multiple district fields, but the schema-canonical
`HDISTRICT` is barely populated (2%). The operational system instead uses
a field labelled `DISTRICT`, which lands in our `current_district` and
`work_district` columns depending on how the import maps them. We use a
priority fallback to recover the operational data:

```
COALESCE(home_district,        -- HDISTRICT, ~2%  — schema-correct but sparse
         current_district,      -- ปัจจุบันอยู่ที่, ~20% — close to home
         work_district)         -- ที่ทำงาน, ~88% — fallback when nothing else
```

Trade-off: when the chain falls through to `work_district`, we're
counting the patient in their workplace's district rather than their
registered home. For most Bangkok residents work and home are in the same
zone, so the impact is small — but documented as a known approximation.

## Coverage results (snapshot 2026-04-27)

| Source | Resolution | Persons (BKK) | % of source |
|--------|-----------|---------------|-------------|
| App1 | `home_district`                              | ~331K | ~86% |
| Portal | `home → current → work` COALESCE           | ~340K | ~81% |
| App2 | `home_district` (data NULL)                   | 0 | 0% |
| **All combined** | | **~660K** | **~84%** |

This is a ×2 improvement over the strict `home_district`-only base (341K)
and matches what BMA officials report.

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

## Code files derived from this

- `bma-health-db/api/routers/summary.py` — `/overview`, `/filtered` use home_district
- `bma-health-db/api/routers/zones.py` — `/zones` uses home_district
- `bma-health-db/api/routers/districts.py` — `/districts` uses home_district
- (frontend doesn't need changes — it's transparent through the API)
