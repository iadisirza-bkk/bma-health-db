# bma-health-db Cleanup Proposal — Read-Only Audit

**Date:** 2026-04-27
**Audit scope:** Suspected dead schema (`public.raw_*`, unused `private.visit_*` tables, legacy MVs from migrations 002/006/009/012) and dead code (`dashboard_v1` / `statistics_v1` routers, `import_csv.py` v1, `_load_etl()` callers, frontend `packages/shared/`).
**Status:** Read-only investigation. **Nothing has been dropped, deleted, or modified.** This document is a proposal only.

---

## Executive Summary

| Finding | Count | Status |
|---|---|---|
| Confirmed dead views (no caller) | 4 | safe to drop |
| Possibly dead views (only in docs/templates) | 2 | drop with care |
| Confirmed dead `private.*` tables (no caller) | 9 | safe to drop |
| `public.raw_*` tables | 7 | **KEEP — heavily used** by `monitoring`, `research`, `promotion`, `admin` routers (and frontend Research page reads `monitoring.data-quality`) |
| Dead routers in `api/routers/` | 0 | dashboard_v1 + statistics_v1 are wired in but **no caller** found in frontend / chat / agents → **drop candidates** |
| Files where `_load_etl()` (v1) is still called | 1 (`admin.py`, 2 sites) | **needs replacement** before v1 ETL file can be deleted |
| `frontend/packages/shared/` | empty | safe to delete `tsconfig.json` reference + drop empty dir |
| Bonus finding (out-of-scope) | many endpoints query MVs that **were dropped** in migration 105 | listed below as "Phase 4: pre-existing breakage" |

---

## 1. Dead Artifact Inventory (with evidence)

### 1.1 Database — `public.raw_*` tables (7 tables, all 0 rows)

```
docker exec bma-health-db psql -U postgres -d bma_health -c \
  "SELECT relname, n_live_tup FROM pg_stat_user_tables WHERE schemaname='public' AND relname LIKE 'raw_%';"
```
Result:
```
 raw_homehealth   | 0
 raw_homevisit    | 0
 raw_lab_extended | 0
 raw_lab_results  | 0
 raw_patients     | 0
 raw_visits       | 0
 raw_vitalsigns   | 0
```

But the API code references them:
```
grep -rln "raw_patients\|raw_visits\|raw_vitalsigns\|raw_lab_results\|raw_lab_extended\|raw_homevisit\|raw_homehealth" /Users/dev/bma-health-db/api/ /Users/dev/bma-health-db/etl/
```
Files: `admin.py`, `agents/fallback.py`, `agents/tools/query_api.py`, `routers/disease_control.py`, `routers/epidemiology.py`, `routers/executive.py`, `routers/kpi.py`, `routers/monitoring.py`, `routers/promotion.py`, `routers/research.py`, `routers/summary.py`, `routers/trends.py`, `services/agreement_service.py`, `services/health_data_service.py`, `services/repeat_screening_report.py`, `services/report_data_collector.py`, `etl/app2_normalizer.py`, `etl/import_csv.py`, plus templates.

Total references: **275** across 20+ files (see `grep -c` count run during audit).

The frontend Research page (compiled bundle) calls `/api/v2/monitoring/data-quality` and `/api/v2/monitoring/etl-status`, which query these raw tables.

> **Status: ❌ Used by current code.** Even though tables are empty, dropping them would break:
> - `/api/v2/monitoring/data-quality` (read by frontend)
> - `/api/v2/monitoring/cleansing-report`
> - `/api/v2/monitoring/etl-status` (read by frontend)
> - 100+ SQL queries in routers/services/agents
>
> **Recommendation: Keep.** Either: (a) repopulate them via a new ingestion path, or (b) refactor every consumer to read from `private.visit_event` / `private.lab_event` / `private.patient` first, then drop. **DO NOT DROP** until the consumers are migrated. This is Phase 3 work.

---

### 1.2 Database — `private.visit_*` long-tail tables (5 tables, all 0 rows)

```
docker exec bma-health-db psql -U postgres -d bma_health -c \
  "SELECT relname, n_live_tup FROM pg_stat_user_tables WHERE schemaname='private' AND relname IN ('visit_pain','visit_neurological','visit_respiratory','visit_recommendation','visit_referral');"
```
Result: all 0 rows.

Code reference search:
```
grep -rn "visit_pain\|visit_neurological\|visit_respiratory\|visit_recommendation\|visit_referral" \
  /Users/dev/bma-health-db/api/ /Users/dev/bma-health-db/etl/ /Users/dev/bma-health-db/db/migrations/
```
Result — **only the table definitions themselves**:
```
db/migrations/100_schema_v3_private.sql:362:CREATE TABLE IF NOT EXISTS private.visit_pain (
db/migrations/100_schema_v3_private.sql:371:CREATE TABLE IF NOT EXISTS private.visit_neurological (
db/migrations/100_schema_v3_private.sql:379:CREATE TABLE IF NOT EXISTS private.visit_respiratory (
db/migrations/100_schema_v3_private.sql:388:CREATE TABLE IF NOT EXISTS private.visit_recommendation (
db/migrations/100_schema_v3_private.sql:396:CREATE TABLE IF NOT EXISTS private.visit_referral (
```

View dependency check:
```sql
SELECT n.nspname, c.relname AS view_name, d.refobjid::regclass AS depends_on
FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid
JOIN pg_rewrite r ON r.ev_class = c.oid
JOIN pg_depend d ON d.objid = r.oid AND d.classid = 'pg_rewrite'::regclass
WHERE c.relkind IN ('v','m')
  AND d.refobjid::regclass::text IN (
    'private.visit_pain','private.visit_neurological','private.visit_respiratory',
    'private.visit_recommendation','private.visit_referral');
```
Result: 0 rows. **No view depends on any of them.**

> **Status: ✅ Confirmed dead.** No view, no router, no ETL writes them. v3 ETL stores everything in `private.visit_measurement` (EAV) instead. Safe to drop unconditionally.

---

### 1.3 Database — empty `private.patient_*` long-tail tables

```
private.patient_attribute        | 0 rows
private.patient_chronic_history  | 0 rows
private.patient_family_history   | 0 rows
private.patient_allergy          | 0 rows
```

Reference search:
```
grep -rn "patient_chronic_history\|patient_family_history\|patient_attribute\|patient_allergy" \
  /Users/dev/bma-health-db/api/ /Users/dev/bma-health-db/etl/
```
Result: zero matches in code. Only in `DATABASE.md` docs and the migration that defines them.

View-dependency check (same SQL as 1.2 above): **0 rows.**

> **Status: ✅ Confirmed dead.** Same situation as visit_*: defined for a future feature that never shipped. Safe to drop.

---

### 1.4 Database — orphaned `public.v_*` views from migration 011

These views exist in `public` and reference the (still-kept) `raw_*` tables:

```
docker exec bma-health-db psql -U postgres -d bma_health -c \
  "SELECT viewname FROM pg_views WHERE schemaname='public' AND viewname LIKE 'v_%';"
```
Result:
```
v_cross_system_duplicates
v_data_sources
v_districts
v_facilities
v_health_zones
v_source_row_counts
```

Code-side reference grep:
```
grep -rln "\bv_districts\b\|\bv_health_zones\b\|\bv_facilities\b\|\bv_data_sources\b\|\bv_cross_system_duplicates\b\|\bv_source_row_counts\b" \
  /Users/dev/bma-health-db/
```
Result:
```
/Users/dev/bma-health-db/PREPROCESSING.md          # docs
/Users/dev/bma-health-db/db/migrations/011_multi_source_stack.sql  # definition
/Users/dev/bma-health-db/db/migrations/101_schema_v3_public_mvs.sql  # definition
/Users/dev/bma-health-db/api/templates/latex/preprocessing.tex     # docs (lstlisting examples)
```
**Zero hits in any `.py` code.** All API code accesses `private.geo_district`, `private.geo_health_zone`, `private.facility`, `private.data_source` directly through `ref_*` tables — never the `v_*` views.

> **Status:**
> - `v_districts`, `v_health_zones`, `v_facilities`, `v_data_sources` (defined in migration 101): **✅ Confirmed dead.** Convenience aliases for `private.geo_*` that nothing uses.
> - `v_cross_system_duplicates`, `v_source_row_counts` (defined in migration 011): **⚠ Possibly used.** They appear inside `lstlisting` SQL examples in `templates/latex/preprocessing.tex` (manually-rendered LaTeX docs, not runtime). Drop only if doc team confirms the LaTeX is stale.

---

### 1.5 Database — legacy MVs from migrations 002, 006, 009, 012, 016

Migration 105 (`105_drop_legacy_mvs.sql`) explicitly dropped:
```sql
DROP MATERIALIZED VIEW IF EXISTS
  summary_bmi_waist, summary_chronic_history, summary_comorbidity,
  summary_disease_age_sex, summary_district_demographics, summary_district_disease,
  summary_district_lab, summary_district_mental, summary_district_risk_factors,
  summary_facility, summary_family_history, summary_lab_disease_cross,
  summary_screening_tests CASCADE;
```
…then re-created six of them as compat **VIEWs** (relkind='v') over `private.*` + `public.mv_visit_resolved`.

Verification — what's left in the DB:
```sql
SELECT relname, relkind FROM pg_class c
JOIN pg_namespace n ON c.relnamespace=n.oid
WHERE n.nspname='public' AND relname LIKE 'summary_%';
```
Result:
```
summary_district_demographics   | v
summary_district_disease        | v
summary_district_lab            | v
summary_district_mental         | v
summary_district_risk_factors   | v
summary_facility                | v
```
Also gone (as expected): `summary_disease_control` (created in migration 016) — dropped by 105.

**However** — many code paths still query the dropped MVs:

```
grep -rn "summary_disease_age_sex\|summary_bmi_waist\|summary_screening_tests\|summary_chronic_history\|summary_family_history\|summary_comorbidity\|summary_lab_disease_cross\|summary_disease_control" \
  /Users/dev/bma-health-db/api/
```
Result (excerpt):
```
api/routers/research.py:122:            FROM summary_disease_age_sex
api/routers/research.py:162:        LEFT JOIN summary_bmi_waist b ...
api/routers/promotion.py:64:        FROM summary_bmi_waist s
api/routers/promotion.py:279:        FROM summary_bmi_waist s
api/routers/public.py:281:        JOIN summary_bmi_waist b ...
api/routers/kpi.py:169:        FROM summary_disease_control
api/routers/epidemiology.py:68:        FROM summary_disease_age_sex s
api/routers/epidemiology.py:96:        FROM summary_lab_disease_cross s
api/routers/epidemiology.py:130:        FROM summary_comorbidity s
api/routers/epidemiology.py:159:        FROM summary_disease_age_sex s
api/agents/tools/query_api.py:186:    treatment = ... FROM summary_chronic_history
api/agents/tools/query_api.py:254:    ... FROM summary_bmi_waist
api/agents/tools/query_api.py:303:    rows = ... summary_screening_tests
api/agents/tools/query_api.py:316:    rows = ... summary_chronic_history
api/agents/tools/query_api.py:331:    rows = ... summary_family_history
api/agents/tools/query_api.py:352:    ... FROM summary_comorbidity
api/agents/tools/query_api.py:437:    ... FROM summary_lab_disease_cross
api/services/report_data_collector.py:926, 928, 938, 980, 985
api/services/report_generator.py:507
```

> **Status: ❌ Pre-existing breakage (out of audit scope, but worth flagging).** The MVs themselves are already dropped (good — migration 105 succeeded). The **code paths still calling them** will return 500 / "relation does not exist" at runtime. This is a separate cleanup tracked as **Phase 4** below — fixing the code, not the schema.

---

### 1.6 Code — `api/routers/dashboard_v1.py` and `api/routers/statistics_v1.py`

Wired up in `api/main.py`:
```
api/main.py:64-71:
    from routers.statistics_v1 import router as stats_v1_router
    _new_routers.append(stats_v1_router)
    from routers.dashboard_v1 import router as dashboard_v1_router
    _new_routers.append(dashboard_v1_router)
api/main.py:223-224:
    for _r in _new_routers:
        app.include_router(_r)
```
So they ARE mounted. Endpoints exposed:
- `dashboard_v1.py`: `/api/dashboard/governor`, `/api/dashboard/director/{dcode}`, `/api/dashboard/medical`
- `statistics_v1.py`: `/api/stats/district/{dcode}`, `/api/stats/compare`, `/api/stats/zone/{zone_code}`, `/api/stats/city`, `/api/stats/ranking/{disease}`, `/api/stats/trends/{dcode}/{disease}`

Frontend caller search:
```
grep -rn "/api/dashboard\|/api/stats" /Users/dev/bma-health/frontend/src/
```
Result: **zero hits.** Frontend uses `/api/v2/*` exclusively (verified via `grep -rn "/api/v2/" /Users/dev/bma-health/frontend/src/hooks/`).

Chat / agent caller search:
```
grep -rn "/api/dashboard\|/api/stats" /Users/dev/bma-health/backend/ /Users/dev/bma-health-db/api/agents/
```
Result: **zero hits.** Agents call `data_adapter.load_district_data()` directly, not via HTTP.

Test caller search:
```
grep -rn "/api/dashboard\|/api/stats" /Users/dev/bma-health-db/tests/
```
Result:
```
tests/test_new_routers.py:233-331  (full coverage)
tests/test_error_cases.py:116, 186 (a few error cases)
```

Documentation references (thesis, ARCHITECTURE.md): exist but are stale text only.

> **Status: ⚠ Possibly used by tests, but no production caller.** Both routers are wired but the **only callers are the test suite itself**. They appear in old thesis docs but no live frontend / agent / chat code. Underlying services (`data_adapter.load_district_data`, `statistics_service`) are also used by `factors`, `export`, `reports`, `admin_api`, `screening_tests`, and `agents/*` — so the SERVICES stay even after the v1 routers go.

---

### 1.7 Code — `etl/import_csv.py` v1 (1285 lines) and `_load_etl()`

Direct callers in production code:
```
grep -rn "_load_etl\b" /Users/dev/bma-health-db/api/ | grep -v "_v3"
```
Result:
```
api/admin.py:81:def _load_etl():
api/admin.py:1450:        etl = _load_etl()
api/admin.py:2464:            etl = _load_etl()
```
Both call sites invoke `etl.refresh_all_summaries(cur)`:
- `admin.py:1438` — `POST /admin/refresh` (manual MV refresh button)
- `admin.py:2382` — `POST /admin/erasure` (PDPA erasure: refresh views after delete)

Tests reference v1:
```
grep -rn "import_csv" /Users/dev/bma-health-db/tests/
tests/test_etl_parsers.py:34: etl_path = os.path.join(here, "..", "etl", "import_csv.py")
```

Single-file upload `/admin/upload` → `/admin/import` → `_run_import` (`api/admin.py:656`): uses **`_load_etl_v3()` (v3) only**:
```
api/admin.py:691:        etlv3 = _load_etl_v3()
```
Verified by reading `_run_import` body (lines 656-756): every dispatch (`pt`, `app2`, `vitalsignslf`, `homevisit`, `homehealth`, `labhealth`, `labhealthext`, `pthistory`) calls `etlv3.import_*` — no path uses v1 ETL for ingestion.

Bundle upload `/admin/upload-bundle` (`api/admin.py:2855`): uses `_load_etl_v3()` (already verified by user before this audit).

Makefile uses v1:
```
Makefile:277:	cd $(API_DIR) && $(PYTHON) ../etl/import_csv.py \
Makefile:287:	spec = importlib.util.spec_from_file_location('etl','../etl/import_csv.py'); \
```

What v1's `refresh_all_summaries` does (from `etl/import_csv.py:1147`):
```python
def refresh_all_summaries(cur):
    backfill_district_codes(cur)              # fixes raw_* district codes
    cur.execute("SELECT matviewname FROM pg_matviews WHERE schemaname='public'")
    for v in [r[0] for r in cur.fetchall()]:
        cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {v}")
```
It dynamically discovers MVs — schema-agnostic. `backfill_district_codes` mutates `raw_vitalsigns` rows (which are empty in v3) — no-op now.

> **Status:** `import_csv.py` v1 is **functionally one helper** (`refresh_all_summaries`) called from 2 places in `admin.py`. The ingestion logic (1100+ lines) is dead. **Recommendation:** extract `refresh_all_summaries` into a tiny helper (10 lines) inside `admin.py` or a new `services/mv_refresh.py`, then delete `etl/import_csv.py` + `etl/derived.py` + `etl/app2_normalizer.py` (the v1-only modules) entirely. **Test file `tests/test_etl_parsers.py` must be deleted with them.**

---

### 1.8 Frontend — `packages/shared/`

Directory inspection:
```
ls -la /Users/dev/bma-health/packages/
total 0
drwxr-xr-x@ 2 dev staff   64 Apr 10 13:43 .
drwxr-xr-x  40 dev staff 1280 Apr 17 09:27 ..
```
The directory is **empty** — no `shared/` subdir, nothing to import.

Reference search:
```
grep -rln "packages/shared\|@bma-health/shared\|@bma-health" /Users/dev/bma-health/frontend/ /Users/dev/bma-health/frontend_bundle_CI_BKK/
```
Result: zero matches in source code. Only stale references in:
- `/Users/dev/bma-health/CLAUDE.md` (warning text)
- `/Users/dev/bma-health/tsconfig.json` (path alias `@bma-health/*: packages/*/src` and `references: [{path: ./packages/shared}]`)
- `/Users/dev/bma-health/thesis/old/*.md` (historical docs)

> **Status: ✅ Confirmed dead.** Already empty on disk. Leftover config in root `tsconfig.json` references a non-existent path. Safe to delete the empty `packages/` dir + clean up `tsconfig.json`. The CLAUDE.md "do not import" warning becomes unnecessary.

---

## 2. Removal Proposal

| # | Artifact | Type | Size | Reason confirmed dead | Proposed action |
|---|---|---|---|---|---|
| 1 | `private.visit_pain` | table | 16 kB | 0 rows; no view/code reference; v3 ETL writes to `visit_measurement` instead | DROP |
| 2 | `private.visit_neurological` | table | 16 kB | same as #1 | DROP |
| 3 | `private.visit_respiratory` | table | 16 kB | same as #1 | DROP |
| 4 | `private.visit_recommendation` | table | 16 kB | same as #1 | DROP |
| 5 | `private.visit_referral` | table | 16 kB | same as #1 | DROP |
| 6 | `private.patient_attribute` | table | small | 0 rows; no view/code reference | DROP |
| 7 | `private.patient_chronic_history` | table | small | 0 rows; no view/code reference | DROP |
| 8 | `private.patient_family_history` | table | small | 0 rows; no view/code reference | DROP |
| 9 | `private.patient_allergy` | table | small | 0 rows; no view/code reference | DROP |
| 10 | `public.v_districts` | view | n/a | no code reference; redundant alias for `private.geo_district` | DROP |
| 11 | `public.v_health_zones` | view | n/a | no code reference; redundant alias for `private.geo_health_zone` | DROP |
| 12 | `public.v_facilities` | view | n/a | no code reference; redundant alias for `private.facility` | DROP |
| 13 | `public.v_data_sources` | view | n/a | no code reference; redundant alias for `private.data_source` | DROP |
| 14 | `public.v_cross_system_duplicates` | view | n/a | no code reference; only in LaTeX docs `lstlisting` | DROP (after doc team confirmation) |
| 15 | `public.v_source_row_counts` | view | n/a | no code reference; only in LaTeX docs `lstlisting` | DROP (after doc team confirmation) |
| 16 | `api/routers/dashboard_v1.py` | file | 357 lines | not called by frontend / chat / agents (only by tests) | DELETE (after deciding to abandon `/api/dashboard/*` URL space) |
| 17 | `api/routers/statistics_v1.py` | file | 66 lines | same as #16 | DELETE |
| 18 | `etl/import_csv.py` (v1) | file | 1285 lines | only `refresh_all_summaries()` is still used; rest is dead ingestion code | REWRITE — extract `refresh_all_summaries` to a 10-line helper, delete the rest |
| 19 | `etl/derived.py` | file | (depends only on v1) | only imported by v1 `import_csv.py` | DELETE with #18 |
| 20 | `etl/app2_normalizer.py` | file | (depends only on v1) | only imported by v1 `import_csv.py` (v3 has its own `import_app2`) | DELETE with #18 — **VERIFY** v3 doesn't import it |
| 21 | `tests/test_etl_parsers.py` | file | n/a | tests v1 ETL parsers only | DELETE with #18 |
| 22 | `tests/test_new_routers.py:233-331` | partial-file | ~100 lines | tests v1 endpoints | DELETE block (or whole file if rest is also v1) |
| 23 | `tests/test_error_cases.py:116, 186` | partial-file | small | tests v1 error paths | DELETE block |
| 24 | `/Users/dev/bma-health/packages/` | dir | empty | no source imports; empty on disk | RMDIR |
| 25 | `/Users/dev/bma-health/tsconfig.json` paths/references | config | small | references non-existent `packages/shared/src` | EDIT — remove `paths`/`references` blocks |
| 26 | `/Users/dev/bma-health/CLAUDE.md` "Do NOT import packages/shared" rule | doc | 1 line | warning becomes irrelevant after #24 lands | EDIT — drop the rule |
| 27 | `Makefile` `etl` and `etl-backfill` targets | partial-file | ~17 lines | reference `etl/import_csv.py` | REWRITE/DELETE with #18 |

**Not proposed for removal** (these surfaced as suspects but were ruled live):
- `public.raw_*` (7 tables) — 275 code references; need to be either repopulated or migrated off, **then** dropped. Phase 3.
- `dashboard_v1` services (`data_adapter`, `statistics_service`) — also used by `factors`, `export`, `reports`, `admin_api`, `screening_tests`, agents.

---

## 3. Risk Assessment

| Removal | Risk | What could break |
|---|---|---|
| #1-#9 (empty private tables) | **LOW** | Nothing — no views/code reference them. CASCADE is unnecessary. |
| #10-#13 (`v_districts` etc.) | **LOW** | LaTeX docs (manual render) reference some. Code is unaffected. |
| #14-#15 (`v_cross_system_duplicates`, `v_source_row_counts`) | **LOW-MEDIUM** | They depend on the still-alive `raw_*` tables. Dropping the views does not affect the tables. Risk is only that someone runs the LaTeX render and gets a SQL error in output. |
| #16-#17 (v1 routers) | **MEDIUM** | Test suites `test_new_routers.py` and `test_error_cases.py` will fail. Any external integration relying on `/api/dashboard/*` or `/api/stats/*` will 404 — **the audit found none**, but the URL space has been advertised in docs/thesis. Consider returning a 410 Gone with a hint pointing at v2 endpoints rather than a hard 404. |
| #18-#21 (v1 ETL files) | **MEDIUM** | The `_load_etl()` callers in `admin.py` (`/admin/refresh`, `/admin/erasure`) WILL break unless `refresh_all_summaries` is reimplemented inline FIRST. Order matters. |
| #22-#23 (v1 tests) | **LOW** | Only test code is touched. |
| #24-#26 (frontend `packages/`) | **LOW** | TS build picks up `paths`/`references` to a missing dir — silent at runtime, may show warnings during `tsc --build`. After cleanup, no warnings. |
| #27 (Makefile etl targets) | **LOW** | Local-only `make etl` / `make etl-backfill` commands stop working. These call into v1; they're already obsolete because v3 ingestion is via `/admin/upload*` endpoints. |

**Cascading dependencies — none of the proposed drops require CASCADE.** Verified by:
```sql
SELECT n.nspname, c.relname, d.refobjid::regclass
FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid
JOIN pg_rewrite r ON r.ev_class = c.oid
JOIN pg_depend d ON d.objid = r.oid AND d.classid = 'pg_rewrite'::regclass
WHERE c.relkind IN ('v','m')
  AND d.refobjid::regclass::text IN ( <list of all 9 dead tables> );
-- result: 0 rows
```

---

## 4. Migration SQL Draft

### Phase 1 SQL — drop dead views (lowest risk)

```sql
BEGIN;

-- Step 1a: drop alias views (definitely dead — no Python caller)
DROP VIEW IF EXISTS public.v_districts;
DROP VIEW IF EXISTS public.v_health_zones;
DROP VIEW IF EXISTS public.v_facilities;
DROP VIEW IF EXISTS public.v_data_sources;

-- Step 1b: drop multi-source debugging views (only LaTeX docs reference them)
DROP VIEW IF EXISTS public.v_cross_system_duplicates;
DROP VIEW IF EXISTS public.v_source_row_counts;

COMMIT;
```

### Phase 2 SQL — drop dead empty `private.*` tables

```sql
BEGIN;

-- Long-tail visit tables — never populated, no view/code dependency
DROP TABLE IF EXISTS private.visit_pain;
DROP TABLE IF EXISTS private.visit_neurological;
DROP TABLE IF EXISTS private.visit_respiratory;
DROP TABLE IF EXISTS private.visit_recommendation;
DROP TABLE IF EXISTS private.visit_referral;

-- Long-tail patient tables — never populated, no view/code dependency
DROP TABLE IF EXISTS private.patient_attribute;
DROP TABLE IF EXISTS private.patient_chronic_history;
DROP TABLE IF EXISTS private.patient_family_history;
DROP TABLE IF EXISTS private.patient_allergy;

COMMIT;
```

> **No CASCADE used** — verified zero dependents. If a CASCADE is unexpectedly needed, abort and re-investigate; that means a dependency was missed.

### Phase 3 SQL — `public.raw_*` tables (BLOCKED — needs code refactor first)

NOT INCLUDED. Do not attempt until every `raw_*` consumer has been migrated to read from `private.*`. See Phase 3 in §6.

---

## 5. Code Deletion List

### Files to delete (Phase 1, after step #18 helper is in place)
```bash
rm /Users/dev/bma-health-db/etl/import_csv.py            # 1285 lines, v1 ETL
rm /Users/dev/bma-health-db/etl/derived.py               # only used by v1
rm /Users/dev/bma-health-db/etl/app2_normalizer.py       # only used by v1
rm /Users/dev/bma-health-db/tests/test_etl_parsers.py    # tests v1 only
rmdir /Users/dev/bma-health/packages                      # empty, unused
```

**Pre-flight: verify v3 doesn't import `app2_normalizer`:**
```bash
grep -rn "app2_normalizer" /Users/dev/bma-health-db/etl/import_csv_v3.py /Users/dev/bma-health-db/api/
```
Audit result: only `etl/app2_normalizer.py` itself + `etl/import_csv.py` reference it. **`import_csv_v3.py` does NOT import it** — v3 has its own app2 splitter inlined.

### Files to delete (Phase 2)
```bash
rm /Users/dev/bma-health-db/api/routers/dashboard_v1.py
rm /Users/dev/bma-health-db/api/routers/statistics_v1.py
```

### Edits to existing files (Phase 1)

**`api/admin.py`** — replace 2 `_load_etl()` call sites with a small inline helper:

```python
# Replace this block at top of admin.py (around line 81-110):
def _load_etl():
    """Lazy-load ETL module..."""
    # ... 30 lines

# With:
def _refresh_all_mvs(cur) -> None:
    """Refresh every MV in public schema. Replaces etl.refresh_all_summaries."""
    cur.execute("SELECT matviewname FROM pg_matviews WHERE schemaname='public' ORDER BY matviewname")
    for (v,) in cur.fetchall():
        cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY public.{v}")

# Then at line 1450:
- etl = _load_etl()
- etl.refresh_all_summaries(cur)
+ _refresh_all_mvs(cur)

# And at line 2464:
- etl = _load_etl()
- etl.refresh_all_summaries(cur)
+ _refresh_all_mvs(cur)
```

(`backfill_district_codes` from v1 mutates `raw_*` tables which are empty — safe to drop from the new helper.)

**`api/main.py`** — remove v1 router wiring (Phase 2):
```python
# Delete lines 64-71:
- try:
-     from routers.statistics_v1 import router as stats_v1_router
-     _new_routers.append(stats_v1_router)
- except ImportError:
-     pass
- try:
-     from routers.dashboard_v1 import router as dashboard_v1_router
-     _new_routers.append(dashboard_v1_router)
- except ImportError:
-     pass
```

**`Makefile`** — remove the `etl` and `etl-backfill` targets (lines 275-293):
```diff
-.PHONY: etl
-etl: ## Run full ETL import from minimal_data/portal_top CSVs
-	cd $(API_DIR) && $(PYTHON) ../etl/import_csv.py \
-		--data-dir ../minimal_data/portal_top \
-		--db-url "$(DB_URL)"
-
-.PHONY: etl-backfill
-etl-backfill: ## Backfill district_code + refresh views (no re-import)
-	@echo "Backfilling district codes and refreshing views..."
-	@cd $(API_DIR) && $(PYTHON) -c "..."
```

(Use `/admin/refresh` UI button instead — same effect.)

**`/Users/dev/bma-health/tsconfig.json`** — remove dead `packages/` references:
```diff
   "compilerOptions": {
     ...
     "moduleResolution": "bundler",
     "allowImportingTsExtensions": true,
-    "noEmit": true,
-    "paths": {
-      "@bma-health/*": ["packages/*/src"]
-    }
+    "noEmit": true
   },
-  "include": ["packages", "frontend"],
-  "references": [
-    { "path": "./packages/shared" },
-    { "path": "./frontend" }
-  ]
+  "include": ["frontend"],
+  "references": [
+    { "path": "./frontend" }
+  ]
 }
```

**`/Users/dev/bma-health/pnpm-workspace.yaml`** — already only lists `frontend`, no edit needed.

**`/Users/dev/bma-health/CLAUDE.md`** — remove the "do NOT import from packages/shared/" line.

**`/Users/dev/bma-health-db/CLAUDE.md`** — update ETL pipeline description:
```diff
-1. CSV Upload → admin.py /admin/upload-bundle → etl/import_csv.py → raw tables → ...
+1. CSV Upload → admin.py /admin/upload* → etl/import_csv_v3.py → private.* (EAV) → public.mv_* → API
-- ETL caching: admin.py:_load_etl() caches the ETL module by mtime...
+- ETL caching: admin.py:_load_etl_v3() caches the v3 ETL module by mtime...
```

### Tests to delete (Phase 2)
- `tests/test_new_routers.py` lines 233-331 — `TestStatistics*` and `TestDashboard*` classes
- `tests/test_error_cases.py` lines 116, 186 — v1 error-case tests

Confirm no other test references v1 endpoints:
```bash
grep -rn "/api/dashboard\|/api/stats" /Users/dev/bma-health-db/tests/
```

### Confirmation greps (run after each phase)

After Phase 1:
```bash
grep -rn "from etl import\|import_csv\.py\b" /Users/dev/bma-health-db/api/
# Expected: 0 hits in api/ (still hits in tests/ until Phase 2 lands)

grep -rn "_load_etl\b" /Users/dev/bma-health-db/api/
# Expected: only _load_etl_v3 hits
```

After Phase 2:
```bash
grep -rn "/api/dashboard\|/api/stats\|dashboard_v1\|statistics_v1" /Users/dev/bma-health-db/
# Expected: 0 hits in code; only stale doc references in thesis/ remain (out of scope)
```

After SQL Phase 1:
```bash
docker exec bma-health-db psql -U postgres -d bma_health -c \
  "SELECT viewname FROM pg_views WHERE schemaname='public' AND viewname LIKE 'v_%';"
# Expected: 0 rows
```

After SQL Phase 2:
```bash
docker exec bma-health-db psql -U postgres -d bma_health -c \
  "SELECT relname FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid
   WHERE n.nspname='private' AND relname LIKE 'visit_pain%'
                                OR relname LIKE 'visit_neurological%'
                                OR relname LIKE 'visit_respiratory%'
                                OR relname LIKE 'visit_recommendation%'
                                OR relname LIKE 'visit_referral%'
                                OR relname LIKE 'patient_attribute%'
                                OR relname LIKE 'patient_allergy%'
                                OR relname LIKE 'patient_chronic_history%'
                                OR relname LIKE 'patient_family_history%';"
# Expected: 0 rows
```

---

## 6. Suggested Order

### Phase 1 — lowest risk, do this first

1. SQL: drop dead `public.v_*` views (#10-#13). Zero code touches them.
2. SQL: drop empty `private.visit_*` and `private.patient_*` long-tail tables (#1-#9).
3. Code: replace `_load_etl()` calls in `admin.py` with the inline `_refresh_all_mvs()` helper.
4. Code: delete `etl/import_csv.py`, `etl/derived.py`, `etl/app2_normalizer.py`.
5. Code: delete `tests/test_etl_parsers.py`.
6. Code: edit `Makefile` to drop `etl` and `etl-backfill` targets.
7. Code: edit `bma-health-db/CLAUDE.md` — replace v1 references with v3 ones.
8. Code: delete empty `/Users/dev/bma-health/packages/` dir + clean root `tsconfig.json` + delete the dead-code rule from `bma-health/CLAUDE.md`.
9. Re-run test suite. Re-run `/admin/refresh` to verify MV refresh still works.

### Phase 2 — medium risk

10. SQL: drop `v_cross_system_duplicates`, `v_source_row_counts` after confirming with whoever owns the LaTeX preprocessing doc (#14-#15).
11. Code: delete `api/routers/dashboard_v1.py` and `statistics_v1.py` after deciding the `/api/dashboard/*` and `/api/stats/*` URL space is truly abandoned. **Recommended:** add a 410 Gone shim first that points users at the v2 equivalents (`/api/v2/executive/headline-kpi`, `/api/v2/summary/overview`, etc.) and keep it for one release before deletion.
12. Code: delete the v1 router wiring in `main.py`.
13. Code: delete the corresponding test classes in `tests/test_new_routers.py` and `tests/test_error_cases.py`.

### Phase 3 — highest risk, most work

14. **Migrate `raw_*` consumers** off the empty `raw_*` tables onto `private.*` and `public.mv_*`. Per file:
    - `api/routers/monitoring.py` — `data-quality`, `cleansing-report`, `etl-status` queries
    - `api/routers/research.py` — `individual-data` query
    - `api/routers/promotion.py` — `alcohol`, `exercise`, `diet` queries
    - `api/admin.py` — `data-quality` admin page, table-counts endpoint
    - All references in `services/*.py` and `agents/tools/query_api.py`
    Each one needs: a v3 query against `private.visit_event`/`private.visit_measurement`/`private.lab_event`/etc., plus k-anonymity guard.
15. Once **all** consumers are off raw tables: SQL drop the 7 `raw_*` tables.

### Phase 4 — pre-existing breakage (BONUS, separate work item)

The audit revealed that several routers query MVs that `migration 105` already dropped. These endpoints are silently broken on the current main branch. Recommended cleanup pass (independent of this proposal):

| File | Dropped MV referenced |
|---|---|
| `api/routers/research.py:122` | `summary_disease_age_sex` |
| `api/routers/research.py:162` | `summary_bmi_waist` |
| `api/routers/promotion.py:64,279` | `summary_bmi_waist` |
| `api/routers/public.py:281` | `summary_bmi_waist` |
| `api/routers/kpi.py:169` | `summary_disease_control` |
| `api/routers/epidemiology.py:68,96,130,159` | `summary_disease_age_sex`, `summary_lab_disease_cross`, `summary_comorbidity` |
| `api/agents/tools/query_api.py:186,254,303,316,331,352,437` | `summary_chronic_history`, `summary_bmi_waist`, `summary_screening_tests`, `summary_family_history`, `summary_comorbidity`, `summary_lab_disease_cross` |
| `api/services/report_data_collector.py:926,938,985` | `summary_family_history`, `summary_comorbidity` |
| `api/services/report_generator.py:507` | `summary_screening_tests` |

Each call needs to be either rewritten against `private.*`/`public.mv_*` or guarded with try/except returning a "feature not yet ported to v3" stub. Until then: those endpoints raise SQL errors at runtime.

---

## Appendix A — Key Verification Commands

```bash
# Confirm 9 empty private tables have no view dependencies
docker exec bma-health-db psql -U postgres -d bma_health -c "
SELECT n.nspname, c.relname, d.refobjid::regclass
FROM pg_class c
JOIN pg_namespace n ON c.relnamespace = n.oid
JOIN pg_rewrite r ON r.ev_class = c.oid
JOIN pg_depend d ON d.objid = r.oid AND d.classid = 'pg_rewrite'::regclass
WHERE c.relkind IN ('v','m')
  AND d.refobjid::regclass::text IN (
    'private.visit_pain','private.visit_neurological','private.visit_respiratory',
    'private.visit_recommendation','private.visit_referral',
    'private.patient_attribute','private.patient_chronic_history',
    'private.patient_family_history','private.patient_allergy');
"
# Returns 0 rows ⇒ no CASCADE needed

# Confirm v_* aliases have no caller
grep -rn "\bv_districts\b\|\bv_health_zones\b\|\bv_facilities\b\|\bv_data_sources\b" \
  /Users/dev/bma-health-db/api/ /Users/dev/bma-health-db/etl/
# Returns 0 hits

# Confirm v1 routers have no production caller
grep -rn "/api/dashboard\|/api/stats" /Users/dev/bma-health/frontend/src/ \
                                       /Users/dev/bma-health/backend/ \
                                       /Users/dev/bma-health-db/api/
# Returns hits ONLY inside dashboard_v1.py / statistics_v1.py themselves and tests

# Confirm _load_etl is the only v1 ETL caller
grep -rn "_load_etl\b" /Users/dev/bma-health-db/api/ | grep -v "_v3"
# Returns 3 hits: 1 def + 2 callers in admin.py

# Confirm bma-health/packages is empty
ls -la /Users/dev/bma-health/packages/
# total 0, only . and ..
```

---

## Appendix B — Files Cited

Database migrations:
- `/Users/dev/bma-health-db/db/migrations/001_create_raw_tables.sql`
- `/Users/dev/bma-health-db/db/migrations/002_create_materialized_views.sql`
- `/Users/dev/bma-health-db/db/migrations/006_expanded_views.sql`
- `/Users/dev/bma-health-db/db/migrations/009_new_materialized_views.sql`
- `/Users/dev/bma-health-db/db/migrations/011_multi_source_stack.sql`
- `/Users/dev/bma-health-db/db/migrations/012_matviews_per_source.sql`
- `/Users/dev/bma-health-db/db/migrations/016_summary_disease_control.sql`
- `/Users/dev/bma-health-db/db/migrations/100_schema_v3_private.sql` (lines 193-228, 362-401 — dead table defs)
- `/Users/dev/bma-health-db/db/migrations/101_schema_v3_public_mvs.sql` (lines 290-307 — dead v_* views)
- `/Users/dev/bma-health-db/db/migrations/105_drop_legacy_mvs.sql`

API code:
- `/Users/dev/bma-health-db/api/main.py` (router wiring, lines 64-71, 223-224)
- `/Users/dev/bma-health-db/api/admin.py` (lines 81, 111, 656, 691, 1450, 2464)
- `/Users/dev/bma-health-db/api/routers/dashboard_v1.py`
- `/Users/dev/bma-health-db/api/routers/statistics_v1.py`
- `/Users/dev/bma-health-db/api/routers/monitoring.py` (raw_* dependencies)
- `/Users/dev/bma-health-db/api/routers/research.py`
- `/Users/dev/bma-health-db/api/routers/promotion.py`
- `/Users/dev/bma-health-db/api/services/data_adapter.py`

ETL code:
- `/Users/dev/bma-health-db/etl/import_csv.py`
- `/Users/dev/bma-health-db/etl/import_csv_v3.py`
- `/Users/dev/bma-health-db/etl/derived.py`
- `/Users/dev/bma-health-db/etl/app2_normalizer.py`

Tests:
- `/Users/dev/bma-health-db/tests/test_etl_parsers.py`
- `/Users/dev/bma-health-db/tests/test_new_routers.py`
- `/Users/dev/bma-health-db/tests/test_error_cases.py`

Project root:
- `/Users/dev/bma-health-db/Makefile` (lines 275-293)
- `/Users/dev/bma-health-db/CLAUDE.md`
- `/Users/dev/bma-health/CLAUDE.md`
- `/Users/dev/bma-health/tsconfig.json`
- `/Users/dev/bma-health/pnpm-workspace.yaml`
