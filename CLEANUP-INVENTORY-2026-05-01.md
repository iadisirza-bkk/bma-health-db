# BMA Health DB — Cleanup Inventory (2026-05-01)

Audit of every relation in `public` and `private` against migration **200**
(`db/migrations/200_bma_med_mv_translation.sql`) — the only 200-series migration
present today. The migration translates four hot MVs onto the new `bma_med.*`
typed tables and replaces the eight cold MVs with `WHERE FALSE` stubs. **No SQL
in migration 200 references `private.*`** — every `FROM` / `JOIN` resolves to
`bma_med.*` or `public.mv_visit_resolved`.

Sizes / row counts captured live from the running container
(`bma-health-db`, db `bma_health`) at 2026-05-01.

## What migration 200 actually consumes

```
FROM bma_med.app1_vitalsignslf       (line 177)
FROM bma_med.portal_vitalsignslf     (line 208)
FROM bma_med.app1_homevisit          (line 224)
FROM bma_med.portal_homevisit        (line 234)
FROM bma_med.app1_homehealth         (line 250)
FROM bma_med.portal_homehealth       (line 262)
FROM bma_med.app1_labhealth          (line 492)
FROM bma_med.portal_labhealth        (line 495)
LEFT JOIN bma_med.patient            (line 337)
FROM public.mv_visit_resolved        (lines 392, 468, 552 — self-reference)
```

That's it. All other tables in `public` / `private` are either:
1. Reference data already mirrored into `public` (KEEP),
2. Reference data only in `private` that the API joins against (MIGRATE), or
3. Old EAV pipeline output / abandoned `raw_*` tables (DROP).

Note migration 200 silently leaves 16 legacy MVs/views in `public` that still
`SELECT FROM private.*`. Those MVs become stale-but-functional after migration
200 (they're not in 200's DROP list). They will fail to refresh once `private`
is dropped, so this inventory also flags them as **legacy MVs to drop** in
section E.

---

## A. KEEP in `public` — already-good reference data

These tables hold reference data already in `public`, are referenced by the
API (`api/routers/*.py`, `api/agents/tools/*.py`), and are independent of the
old EAV pipeline. Migration 200 does not touch them. **Action: nothing.**

| Table                          | Size     | Rows   | Notes                                                             |
|--------------------------------|----------|--------|-------------------------------------------------------------------|
| `public.ref_districts`         | 40 kB    | 50     | dcode/zone_code/name_th/population — joined by 14 router queries  |
| `public.ref_facilities`        | 12 MB    | 14063  | facility metadata — joined by `routers/public.py`, `strategy.py`  |
| `public.ref_health_zones`      | 32 kB    |  8     | 8 BMA health zones — joined by `routers/zones.py`, `summary.py`   |
| `public.ref_facility_code_map` | 32 kB    | 11     | hptcode mapping — referenced by `etl/import_facilities.py`        |
| `public.pm25_daily`            | 24 kB    |  0     | FK → ref_districts; migration 010 created — keep for daily PM25   |
| `public.data_retention_policy` | 48 kB    |  8     | PDPA retention rules — migration 005                              |
| `public.erasure_requests`      | 32 kB    |  0     | PDPA Right-to-Erasure log — migration 005                         |
| `public.import_history`        | 144 kB   |  9     | Used by `api/admin.py` to render upload UI                        |
| `public.mv_refresh_log`        | 48 kB    | 79     | Operational, written by REFRESH wrapper                           |

---

## B. MIGRATE from `private` to `bma_med` — reference data the API still needs

The API and ETL still read `private.geo_district`, `private.geo_health_zone`,
`private.geo_province`, `private.facility`. These are **not duplicates** of the
`public.ref_*` tables — they have additional columns (e.g.
`geo_district.is_bangkok`, `geo_district.province_code`) and a richer schema.
However, all of them are seeded from migrations
(`103_seed_geography.sql`, `111_seed_77_provinces.sql`, `014_official_hrsi_dcodes.sql`)
or from `etl/import_facilities.py` reading `data/clinic_latlong.xls`, so they
are reproducible.

Two options:
- **B1** (recommended): keep them as is, do **not** drop the `private` schema yet
  — only drop the heavy `private.{visit,lab}_*` partitions and `private.patient*`.
- **B2** (cleaner): copy them into `bma_med` first, rewrite refs in
  `etl/geocode_facilities.py` and `etl/import_facilities.py`, then drop
  `private` whole.

If you choose B2, the copy SQL is:

```sql
-- Reference geography
CREATE TABLE bma_med.geo_province     AS TABLE private.geo_province;
CREATE TABLE bma_med.geo_health_zone  AS TABLE private.geo_health_zone;
CREATE TABLE bma_med.geo_district     AS TABLE private.geo_district;
CREATE TABLE bma_med.geo_subdistrict  AS TABLE private.geo_subdistrict;
CREATE TABLE bma_med.facility         AS TABLE private.facility;

-- Restore PKs / FKs (CREATE TABLE AS strips them)
ALTER TABLE bma_med.geo_province    ADD PRIMARY KEY (province_code);
ALTER TABLE bma_med.geo_health_zone ADD PRIMARY KEY (zone_code);
ALTER TABLE bma_med.geo_district    ADD PRIMARY KEY (dcode);
ALTER TABLE bma_med.geo_subdistrict ADD PRIMARY KEY (sdcode);
ALTER TABLE bma_med.facility        ADD PRIMARY KEY (code);

GRANT SELECT ON ALL TABLES IN SCHEMA bma_med
    TO bma_med_reader, bma_med_clinician, bma_med_loader;
```

| Table                       | Size     | Rows | Why preserve                              |
|-----------------------------|----------|------|-------------------------------------------|
| `private.geo_province`      | 24 kB    | 77   | 77 Thai provinces (mig 111)               |
| `private.geo_health_zone`   | 32 kB    |  8   | 8 BMA health zones (mig 103)              |
| `private.geo_district`      | 56 kB    | 50   | BMA 50 districts + zone (mig 014/103)     |
| `private.geo_subdistrict`   | 8.1 kB   |  ?   | (small, derived from data)                |
| `private.facility`          | 11 MB    |  ?   | Loaded from clinic_latlong.xls            |
| `private.data_source`       | 32 kB    |  3   | Source codes app1/portal/app2 (3 rows)    |
| `private.variable_definition` | 504 kB |  ?   | EAV column→variable map; no longer needed |

The last two (`data_source`, `variable_definition`) belong to the old EAV
pipeline and are NOT needed by the new bma_med MVs — they go to bucket C. The
top five are the only true reference-data candidates for migration.

Note: the bma_med ingestion pipeline (`/Users/dev/bma-med/ingest.py`) already
maintains `bma_med.source` (3 rows) which is a typed-schema replacement of
`private.data_source`. So `data_source` is **not** in B.

---

## C. DROP via `DROP SCHEMA private CASCADE` — old EAV pipeline output

These tables hold the old EAV pivot output that migration 200 replaces. Once
migration 200 is applied **and** any remaining legacy MV/view references are
removed (see section E), the entire `private` schema can be cascade-dropped.

| Table                              | Size     | Rows | Notes                                  |
|------------------------------------|----------|------|----------------------------------------|
| `private.visit_measurement_p0..p15` | 16 × ~625 MB ≈ 10 GB | ~78 M total | EAV pivot, replaced by bma_med.app1/portal_vitalsignslf etc. |
| `private.visit_event`              | 501 MB   |  ?   | Visit headers, replaced by bma_med.app1_vitalsignslf |
| `private.patient`                  | 259 MB   |  ?   | Replaced by bma_med.patient            |
| `private.patient_address`          | 221 MB   |  ?   | SCD-2 address; folded into bma_med.app1_homevisit district |
| `private.lab_event`                | 212 MB   |  ?   | Replaced by bma_med.app1/portal_labhealth |
| `private.patient_alias`            | 146 MB   |  ?   | (source, source_pid) → patient_id; folded into bma_med ingest |
| `private.lab_measurement_p0..p15`  | 16 × ~141 MB ≈ 2.3 GB | many | EAV labs; replaced by bma_med.app1/portal_labhealth |
| `private.lab_measurement` parent   | 0 bytes  |  0   | Partition root                         |
| `private.visit_measurement` parent | 0 bytes  |  0   | Partition root                         |
| `private.variable_definition`      | 504 kB   |  ?   | EAV column→variable map; obsolete      |
| `private.variable_code_value`      | 120 kB   |  ?   | EAV value-code map; obsolete           |
| `private.import_batch`             | 48 kB    |  ?   | ETL batch log (replaced by bma_med.ingestion_batch) |
| `private.data_source`              | 32 kB    |  3   | Source codes (replaced by bma_med.source) |
| `private.audit_log`                | 24 kB    |  ?   | Replaced by bma_med.audit_log          |
| `private.erasure_request`          | 16 kB    |  0   | PDPA log (`public.erasure_requests` is the keeper) |
| `private.data_quality_issue`       | 16 kB    |  ?   | Replaced by bma_med.quality_flag       |
| 9 sequences (`*_id_seq`)           | -        | -    | Drop via CASCADE                       |

**Total dropped from `private`: ≈ 12.5 GB** (mostly the 16 visit_measurement_p*
partitions at 625 MB each + 16 lab_measurement_p* at 141 MB).

---

## D. DROP in `public` — abandoned `raw_*` tables

These are the v1/v2 `raw_*` tables (migration 001) — migration 200 does not
touch them and they are **all empty** in production today. Comments throughout
`api/admin.py` (lines 154, 1083) and migration files confirm v3 already
abandoned these. The new bma_med pipeline ingests CSVs directly into
`bma_med.*` (typed), bypassing raw staging entirely.

| Table                       | Size  | Rows | Action       |
|-----------------------------|-------|------|--------------|
| `public.raw_patients`       | 48 kB |  0   | DROP TABLE   |
| `public.raw_visits`         | 64 kB |  0   | DROP TABLE   |
| `public.raw_vitalsigns`     | 104 kB|  0   | DROP TABLE   |
| `public.raw_homevisit`      | 64 kB |  0   | DROP TABLE   |
| `public.raw_homehealth`     | 64 kB |  0   | DROP TABLE   |
| `public.raw_lab_results`    | 72 kB |  0   | DROP TABLE   |
| `public.raw_lab_extended`   | 64 kB |  0   | DROP TABLE   |

```sql
DROP TABLE IF EXISTS public.raw_patients     CASCADE;
DROP TABLE IF EXISTS public.raw_visits       CASCADE;
DROP TABLE IF EXISTS public.raw_vitalsigns   CASCADE;
DROP TABLE IF EXISTS public.raw_homevisit    CASCADE;
DROP TABLE IF EXISTS public.raw_homehealth   CASCADE;
DROP TABLE IF EXISTS public.raw_lab_results  CASCADE;
DROP TABLE IF EXISTS public.raw_lab_extended CASCADE;
```

---

## E. DROP in `public` — legacy MVs / views that still reference `private.*`

Migration 200 only drops the four MVs it replaces (`mv_visit_resolved`,
`summary_district_disease`, `summary_facility`, `summary_disease_age_sex`)
plus the eight cold stubs. The remaining 12 MVs and 4 views below still
`SELECT FROM private.*` and will break the moment `private` is dropped.
None of them are referenced by migration 200; if the API still hits them the
endpoints will need to be either repointed at the new MVs or dropped along
with the public-facing routes.

Materialized views referencing `private.*` (will fail to refresh post-drop):

| MV                              | Size     | First private ref           |
|---------------------------------|----------|-----------------------------|
| `public.mv_lab_distribution`    | 13 MB    | private.lab_event           |
| `public.summary_disease_control`| 168 kB   | private.lab_event           |
| `public.summary_bmi_waist`      | 88 kB    | private.patient             |
| `public.summary_lab_disease_cross` | 1.1 MB | private.lab_event         |
| `public.mv_demographics`        | 1.1 MB   | private.patient             |
| `public.mv_lifestyle`           | 1.0 MB   | private.variable_definition |
| `public.mv_mental_health`       | 816 kB   | private.visit_measurement   |
| `public.mv_kpi_tier1`           | 392 kB   | (none — but depends on mv_visit_resolved) |
| `public.mv_data_dictionary`     | 192 kB   | private.variable_definition |
| `public.mv_disease_district`    | 184 kB   | private.variable_definition |
| `public.mv_summary_lab`         | 176 kB   | private.lab_event           |
| `public.mv_summary_mental`      | 176 kB   | private.visit_measurement   |
| `public.mv_summary_districts`   | 72 kB    | (uses ref_districts only)   |
| `public.mv_ncd_diagnostic_zone` | 40 kB    | private.geo_district        |
| `public.mv_ncd_diagnostic_report`| 32 kB   | private.lab_event           |
| `public.mv_summary_global`      | 24 kB    | (none direct)               |
| `public.summary_comorbidity`    | 296 kB   | (depends on mv_visit_resolved cascade) |
| `public.summary_disease_age_sex`| 2.1 MB   | private.patient (will be replaced by mig 200) |

Views referencing `private.*` (will fail on next query post-drop):

| View                                  | First private ref           |
|---------------------------------------|-----------------------------|
| `public.summary_district_demographics`| private.visit_measurement   |
| `public.summary_district_lab`         | private.lab_event           |
| `public.summary_district_mental`      | private.visit_measurement   |
| `public.summary_facility`             | private.visit_event         |

The four views above are recreated as `WHERE FALSE` stubs by migration 200 —
that's safe. The MVs are NOT touched and need a separate `DROP MATERIALIZED VIEW`
sweep before `DROP SCHEMA private CASCADE`. CASCADE will automatically drop
the dependent MVs, but the explicit pre-drop is cleaner and avoids accidentally
losing a real ref-data table that we forgot to migrate.

---

## RISKS — code that still reads `private.*`

The following files in `api/`, `etl/`, `templates/` read `private.*` and **will
break** once the schema is dropped. They must be either rewritten against
`bma_med.*` or removed before applying the destructive cleanup. None block
migration 200 itself (which only redefines public MVs); they block the
destructive `DROP SCHEMA private` step.

### `api/admin.py` (heaviest)
- L271–303: DELETE statements against `private.visit_measurement / lab_measurement /
  visit_event / lab_event / patient_alias / patient` (admin "delete-source" path)
- L426–454: `IMPORT_TABLES` dict mapping CSV name → `private.*` table
- L529: `SELECT … FROM private.variable_definition` (variable mapping)
- L766–824: ETL v3 dispatcher — `INSERT INTO private.import_batch`,
  `SELECT … FROM private.patient JOIN private.patient_alias …`,
  `UPDATE private.import_batch …`
- L1087–1130: `IMPORT_TABLE_SPECS` listing per-source row counts from
  `private.patient / visit_event / lab_event / patient_address /
  visit_measurement / lab_measurement`
- L1175–1190: dashboard stats from `private.patient_alias` + `private.patient`
- L2820–2862: Facility bootstrap path (`SELECT COUNT(*) FROM private.facility`)
- L3331–3337: Variable-definition completeness check
  (`SELECT COUNT(*) FROM private.variable_definition`)

### `api/database.py`, `api/config.py`
- L25–28, L105: doc-comments referring to writer-pool privileges on `private.*`
  (cosmetic — change after the role privileges are revoked).

### `api/agents/tools/insights.py`
- L12–14: doc-comments saying "We cannot reference private.* — use ref_*";
  these are **already correct** post-cleanup.
- L266: doc-comment — cosmetic.

### `api/templates/admin/upload.html`, `upload_bundle.html`, `cleansing_report.html`
- Many user-facing `<code>private.*</code>` blocks naming the legacy schema. The
  upload UX needs to be retargeted at `bma_med.*` (or dropped if the new
  ingest pipeline is owned by `/Users/dev/bma-med/ingest.py` and admin uploads
  go away).

### `etl/geocode_facilities.py`
- L51, L65, L108, L125: reads `private.geo_district` and updates
  `private.facility`. Either repoint at `bma_med.geo_district / facility`
  (after section B copy) or retire (the new ingest doesn't require it).

### `etl/import_facilities.py`
- L131, L164: writes `private.facility` (legacy v3 path). Same options.

### `etl/import_csv_v3.py`
- ALL of it (L207, L218, L227, L239, L260, L295, L308, L320, …): the entire
  v3 EAV ingest. Retire whole file once `bma_med` ingest is the sole CSV path
  (per `/Users/dev/bma-med/ingest.py`).

### `etl/refresh_legacy_summaries.py`
- L9: doc-comment (`raw_vitalsigns`). Cosmetic; the code is presumably dead
  once raw_* are dropped.

### Migrations 101 / 112 / 114 (history)
- Already applied; they reference `private.*` but a freshly-cloned dev DB
  applies them in order. After cleanup, those migrations cannot be re-applied
  on a clean DB without `private.*`. **You should mark them historic
  (`-- HISTORIC: applied prior to 200) and add a no-op guard `IF EXISTS`** or
  squash them into a baseline migration that creates the post-cleanup state.
  Otherwise CI's `migrate-from-zero` will fail.

---

## DROP READY — destructive command sequence

```sql
-- =============================================================================
-- WARNING: RUN ONLY AFTER MIGRATIONS 200-211 ARE APPLIED *AND* VERIFIED.
--
-- Pre-flight checks (all must pass):
--   1. Migration 200 applied:
--        SELECT EXISTS(SELECT 1 FROM pg_class
--                       WHERE relname='mv_visit_resolved'
--                         AND relnamespace=(SELECT oid FROM pg_namespace
--                                           WHERE nspname='public'));
--      AND its definition references bma_med.* (not private.*):
--        SELECT pg_get_viewdef(c.oid) ~ 'bma_med\.'
--          FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
--         WHERE n.nspname='public' AND c.relname='mv_visit_resolved';
--   2. mv_visit_resolved has been refreshed and has rows:
--        SELECT COUNT(*) FROM public.mv_visit_resolved;
--   3. summary_district_disease / summary_facility / summary_disease_age_sex
--      all populated:
--        SELECT relname, n_live_tup FROM pg_stat_user_tables
--         WHERE schemaname='public'
--           AND relname IN ('summary_district_disease','summary_facility',
--                           'summary_disease_age_sex');
--   4. API still answers without 5xx for the 53 endpoints in FRONTEND-API-MAP.md.
--   5. api/admin.py ETL paths have been rewritten or disabled (the 14 `private.*`
--      references in section RISKS are gone or guarded).
--   6. etl/import_csv_v3.py / etl/geocode_facilities.py / etl/import_facilities.py
--      have been retired or repointed.
--
-- IF ANY OF THE ABOVE FAILS — DO NOT RUN. Fix first, then re-attempt.
-- This is a one-way operation. Take a backup with pg_dump before running.
-- =============================================================================

-- (Optional) Section B — copy reference geography into bma_med BEFORE drop.
--   Skip if you choose to leave private schema in place for B1.
CREATE TABLE bma_med.geo_province     AS TABLE private.geo_province;
CREATE TABLE bma_med.geo_health_zone  AS TABLE private.geo_health_zone;
CREATE TABLE bma_med.geo_district     AS TABLE private.geo_district;
CREATE TABLE bma_med.geo_subdistrict  AS TABLE private.geo_subdistrict;
CREATE TABLE bma_med.facility         AS TABLE private.facility;
ALTER TABLE bma_med.geo_province    ADD PRIMARY KEY (province_code);
ALTER TABLE bma_med.geo_health_zone ADD PRIMARY KEY (zone_code);
ALTER TABLE bma_med.geo_district    ADD PRIMARY KEY (dcode);
ALTER TABLE bma_med.geo_subdistrict ADD PRIMARY KEY (sdcode);
ALTER TABLE bma_med.facility        ADD PRIMARY KEY (code);
GRANT SELECT ON ALL TABLES IN SCHEMA bma_med
    TO bma_med_reader, bma_med_clinician, bma_med_loader;

-- Section E — drop legacy MVs/views that reference private.* and aren't
--   replaced by migration 200 (CASCADE handles dependents like mv_kpi_tier1
--   and summary_comorbidity that fan out from mv_visit_resolved).
DROP MATERIALIZED VIEW IF EXISTS public.mv_lab_distribution      CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.summary_disease_control  CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_demographics          CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_lifestyle             CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_mental_health         CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_kpi_tier1             CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_data_dictionary       CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_disease_district      CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_summary_lab           CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_summary_mental        CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_summary_districts     CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_ncd_diagnostic_zone   CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_ncd_diagnostic_report CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_summary_global        CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.summary_comorbidity      CASCADE;

-- Section D — drop abandoned raw_* staging tables in public.
DROP TABLE IF EXISTS public.raw_patients     CASCADE;
DROP TABLE IF EXISTS public.raw_visits       CASCADE;
DROP TABLE IF EXISTS public.raw_vitalsigns   CASCADE;
DROP TABLE IF EXISTS public.raw_homevisit    CASCADE;
DROP TABLE IF EXISTS public.raw_homehealth   CASCADE;
DROP TABLE IF EXISTS public.raw_lab_results  CASCADE;
DROP TABLE IF EXISTS public.raw_lab_extended CASCADE;

-- Section C — drop the entire private schema (12.5 GB of EAV data).
--   CASCADE removes all tables, sequences, FKs, and any remaining views/MVs
--   that point at it.
DROP SCHEMA IF EXISTS private CASCADE;

-- Reclaim disk
VACUUM FULL;
```

---

## Summary by bucket

| Bucket | What         | Count | Total size | Action            |
|--------|--------------|-------|------------|-------------------|
| A. KEEP             | public reference data | 9   | ~12.3 MB | None              |
| B. MIGRATE          | private geography → bma_med | 5  | ~11 MB | Optional `CREATE TABLE … AS TABLE` |
| C. DROP (private)   | EAV pipeline + sequences | 51 tables, 9 sequences | ~12.5 GB | `DROP SCHEMA private CASCADE` |
| D. DROP (public.raw_*) | Abandoned v1/v2 staging | 7 | 480 kB | `DROP TABLE` |
| E. DROP (legacy MVs) | Public MVs still reading private.* | 15 MVs + 4 views | ~17 MB | `DROP MATERIALIZED VIEW … CASCADE` |
