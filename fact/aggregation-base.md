# Aggregation Base — Tier 1 KPI Spec (per-source dispatch)

## Decision (2026-04-27 — supersedes 2026-04-22 single-base)

All public-facing aggregates use a **per-source CASE** because each source
populates a different "district" field. The unified CTE in
`api/services/unified_screening.py` joins three sub-streams that each carry
the most-meaningful district available for that source.

| Source | District field | Visits source | DB column |
|--------|---------------|---------------|-----------|
| **Portal** | `vital.DISTRICTBKK` | `vital.PID + VSTDATE` | `raw_vitalsigns.district_code` |
| **App1** | `hv.DISTRICT` (home) | `vital.PID + VSTDATE` | `raw_homevisit.home_district` |
| **App2** | `hv.DISTRICT` (home, **skip null**) | `HD` count | `raw_homevisit.home_district` + `raw_homehealth` |

App2 has 0 records with home_district populated, so it **contributes 0** to
the public dashboard until upstream provides the home_district field.

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
