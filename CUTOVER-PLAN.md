# CUTOVER-PLAN — Sprint S1

> **Status:** Day 1 deliverable. Locked decisions for the 2026-05-04 → 2026-05-15 sprint.
> **Pre-existing companions:** `ACTION-PLAN.md`, `CLEANUP-PROPOSAL.md`, `ETL-TYPE-FIX-DESIGN.md`, `DATABASE.md`, `API-AUDIT.md` — all cross-referenced from this single source-of-truth file.
> **ULTRAPLAN context:** This sprint is S1 of 4. S2 = parameterised descriptive (Module A); S3 = inference (Module B); S4 = cost-effectiveness (Module C, MSD audience). The architecture below leaves explicit empty boxes for those modules — do not fill them in S1.

---

## Locked decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Old EAV vs new wide tables | **Wide** (one Postgres table per cleaned CSV, e.g. `bma_med.app1_homehealth`) | Matches `bma-med/clean.py` 1:1; query speed; ALTER-friendly. The old EAV is the source of the type-inference bug that started this sprint. |
| 2 | Admin upload format | **Keep `.xlsx`**; add server-side demuxer | Frontend `ExcelUpload.tsx` (266 lines) already production-ish. Don't touch UI; add `bma-med/xlsx_to_bmi100.py` to demux on the server. |
| 3 | Re-load semantics | **UPSERT on row-hash** (last-write-wins, content-equivalent rows skipped) | Idempotent re-runs; handles the 18K dup-`(pid, vstdate)` rows in real data. |
| 4 | MV strategy | **Translate first, materialize hot**: rewrite all 12 MVs as views over new tables; flag the 4 most-queried for `MATERIALIZED VIEW` upgrade | Correctness now, perf later. Acceptance gate is *dashboard renders*, not *all 12 are materialized*. |
| 5 | Backup-then-wipe production | **Yes** — `pg_basebackup` + checksum BEFORE Day-2 drop. Test restore on staging first. | Not negotiable. |
| 6 | Old data dictionaries | **Archive** `bma-health-db/DATA-DICTIONARY.md` and `MEDICAL-DICTIONARY.md` with redirect notes pointing to `bma-med/MED-FACTSHEET.md` + `bma-med/CODES_REFERENCE.md`. | Single source of truth = the factsheet. |
| 7 | Test database | **`bma_med_test`** (fresh DB on port 5433); production DB `bma_health` is untouched until S1 demo passes. | Smoke test isolated. |

---

## Architecture target (must exist by EOD Day 1)

```
                 ┌─────────────────────────────────────────────┐
                 │  bma-health (Next.js webadmin) — UNCHANGED  │
                 │   /admin: ExcelUpload + ReportDownload      │
                 │   /admin?persona=citizen|doctor|bma|msd     │  ← S1 #11 stub
                 │   Charts call /api/v2/* (no MV names hard)  │
                 └─────────────────────────────────────────────┘
                                    │ POST /api/admin/upload-excel (xlsx multipart)
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  bma-health-db/api  (FastAPI, port 9002)                            │
│  ┌──────────────────────────┐    ┌──────────────────────────────┐   │
│  │ /api/admin/*  (existing) │    │ /api/v2/*  (existing,        │   │
│  │  upload-excel ── chain   │    │   cleaned per #6)            │   │
│  │  → demuxer               │    │  reads MVs only, k-anon ≥ 5  │   │
│  │  → ingest.py             │    └──────────────────────────────┘   │
│  │  → clean.py              │            │                          │
│  │  → validate.py           │            │ proxy 501s →             │
│  │  → export.py             │            ▼                          │
│  │  → REFRESH MV            │    ┌──────────────────────────────┐   │
│  └──────────────────────────┘    │ /analytics/*  (S1 SKELETON,  │   │
│                                  │   filled in S2/S3/S4)        │   │
│                                  │  - /analytics/contingency    │   │
│                                  │  - /analytics/regression     │   │
│                                  │  - /economics/cost-per-pos   │   │
│                                  │  All return HTTP 501 + plan  │   │
│                                  └──────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
        │                                      │
        │ public-API role                      │ analytics_layer role
        │ SELECT on MVs only                   │ SELECT on bma_med.* (row-level)
        ▼                                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│  PostgreSQL (port 5433, container bma-health-db)                    │
│                                                                     │
│   schema bma_med (NEW, S1)                                          │
│     ├ patient                          (PII, column-level grants)   │
│     ├ <source>_<table> × 13            (typed wide; row_hash key)   │
│     ├ quality_flag                     (long-format issue log)      │
│     ├ ingestion_batch                  (per-run audit)              │
│     ├ audit_log                        (append-only)                │
│     ├ source / table_origin / variable / codebook  (reference)      │
│     └ economics_*                      (RESERVED, S4)               │
│                                                                     │
│   schema public                                                     │
│     ├ mv_disease_district  ◄ ported in S1                           │
│     ├ mv_lab_distribution  ◄ ported in S1                           │
│     ├ mv_visit_resolved    ◄ ported in S1                           │
│     ├ mv_*  (translated views, materialized when hot)               │
│     └ v_patient_deid       (sanitised; for bma_med_reader)          │
│                                                                     │
│   roles                                                             │
│     ├ bma_med_admin        DDL                                      │
│     ├ bma_med_loader       INSERT/UPDATE on data tables             │
│     ├ bma_med_clinician    SELECT incl. PII                         │
│     ├ bma_med_reader       SELECT on MVs + v_patient_deid (no PII)  │
│     └ analytics_layer      SELECT on bma_med.* (S1 reserved; only   │
│                            granted in S2 when service is built)     │
└─────────────────────────────────────────────────────────────────────┘
```

S1 builds the LABELS — not the analytics service contents. S2-S4 fill it.

---

## Kill list (artifacts that go away in S1)

Sourced from `CLEANUP-PROPOSAL.md` + `API-AUDIT.md` + this sprint's MV translation plan.

| Category | Items | Sprint item |
|---|---|---|
| **DB** | `private.visit_measurement` (16 hash partitions, 78M rows EAV) | #2 (after backup) |
| **DB** | `private.lab_measurement` (22M rows EAV) | #2 |
| **DB** | `private.visit_event`, `private.lab_event`, `private.import_batch` (private schema; old structure) | #2 |
| **DB** | 9 dead `private.*` tables per CLEANUP-PROPOSAL Phase 4 (visit_pain, patient_attribute, …) | #13 (P2) |
| **DB** | Original 12 MVs in `public.*` (will be replaced by translated versions) | #5 |
| **DB** | 4 dead views per CLEANUP-PROPOSAL | #13 (P2) |
| **API** | 62 empty endpoints (return 200 with empty arrays — not real) | #6 |
| **API** | 11 endpoints throwing HTTP 500 (query dropped MVs from migration 105) | #6 |
| **API** | v1 routers `dashboard_v1.py`, `statistics_v1.py` (mounted but unused by frontend) | #13 (P2) |
| **ETL** | `etl/import_csv.py` (legacy v1; only `refresh_all_summaries()` still referenced from admin.py) | #13 (P2) |
| **Docs** | `DATA-DICTIONARY.md`, `MEDICAL-DICTIONARY.md` (replaced by `bma-med/MED-FACTSHEET.md` + `CODES_REFERENCE.md`) | #1 (this doc; archive note added EOD Day 1) |

---

## MV translation strategy

### Hot 4 (materialized in S1)
*Selected by API-reference frequency — top hits in the routers.*

| MV | Used by | New shape (one-liner) |
|---|---|---|
| `mv_disease_district` | promotion, epidemiology, disease-control routers | `SELECT district, dx, sex_code, count(*) AS n FROM bma_med.portal_vitalsignslf JOIN patient HAVING n ≥ 5 GROUP BY ALL` |
| `mv_lab_distribution` | epidemiology, screening_tests | `SELECT lab_name, value_bucket, count(*) AS n FROM bma_med.portal_labhealth GROUP BY ALL HAVING n ≥ 5` |
| `mv_visit_resolved` | summary, executive | `SELECT vstdate::date, count(*) AS visits, count(DISTINCT patient_id) AS patients FROM bma_med.portal_vitalsignslf GROUP BY 1` |
| `mv_screening_coverage` | disease-control | `SELECT district, dx, count(*) FILTER (WHERE msd_dm=1) AS positives, count(*) AS screened HAVING screened ≥ 5 GROUP BY ALL` |

### Cold 8 (views in S1, materialize-on-demand later)
- `mv_age_pyramid`, `mv_repeat_screening`, `mv_behavior_disease`, `mv_risk_factor_profile`, `mv_disease_lab_crosstab`, `mv_facility_summary`, `mv_zone_kpi`, `mv_district_summary`.

Each gets a plain `CREATE VIEW` that returns the same column shape as the original MV but reads from new tables. Performance is acceptable on 100-row test data; will be revisited when production volume hits.

**K-anonymity at the SQL level:** every translated MV/view has a `HAVING count(*) ≥ 5` (or per-cell mask) embedded so callers cannot accidentally pull sub-threshold cells.

---

## Pipeline-as-background-job wiring

Replaces lines 2914–3061 in `bma-health-db/api/admin.py`. Pseudocode:

```python
def _run_bundle_import(manifest, history_id):
    start = time.time()
    try:
        # 1. Demux xlsx → BMI_100/-shaped folder
        tmp = tempfile.mkdtemp(prefix="bma-med-")
        from bma_med.xlsx_to_bmi100 import demux
        demux(manifest[0]["tmp_path"], out_dir=tmp)

        # 2. Run the four-step chain
        for step, script in [("ingest", "ingest.py"),
                             ("clean",  "clean.py"),
                             ("validate", "validate.py"),
                             ("export", "export.py")]:
            _update_progress(history_id, step, 10 + 20 * STEP_INDEX)
            r = subprocess.run(["python3", f"/Users/dev/bma-med/{script}"],
                               env={**os.environ, "RAW_ROOT": tmp},
                               capture_output=True, text=True, timeout=3600)
            if step == "validate" and r.returncode == 1:
                # FAIL — block export, surface report.md
                _set_history_error(history_id, "validation failed",
                                    detail=Path(VALIDATE_REPORT).read_text())
                return
            elif r.returncode != 0:
                raise RuntimeError(f"{step} exited {r.returncode}: {r.stderr}")

        # 3. Refresh hot MVs
        _update_progress(history_id, "refresh public MVs", 95)
        _refresh_hot_mvs()  # only the 4 hot ones; cold are views (no refresh)

        # 4. Audit log
        _audit("UPLOAD", resource="admin.upload-excel",
               operator=current_user(), output_count=_count_rows_loaded())

    except Exception as e:
        _set_history_error(history_id, str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
```

Key rules:
- `validate.py exit 1` blocks export. Surface markdown report verbatim to admin UI.
- `validate.py exit 2` (warnings only) shows report, requires operator click-through (sprint item #12).
- All steps inside one DB transaction is **not** the goal — `export.py` already manages its own batch; we just want the chain to be atomic at the *history-row* level (started/completed/failed status).
- Preserve `import_history` and `import_batch` semantics so existing admin UI history view continues to work.

---

## Endpoint cleanup checklist (sprint item #6)

For each of the 24 router files (170 endpoints total per `API-AUDIT.md`):

- [ ] Delete handlers that return empty arrays (62 endpoints — found by `API-AUDIT.md`).
- [ ] Fix or delete handlers that throw HTTP 500 (11 endpoints — these query dropped MVs from migration 105).
- [ ] Audit list-response shape on remaining ~47 handlers — any keys ⊃ {`pid`, `patient_id`, `birthdate`, `phone`, `email`, free-text remarks} → ticket and remove.
- [ ] Update `API-AUDIT.md` with new pass/empty/500 counts.

Use `bma-med/security/k_anon.py:assert_no_individual_fields` as a final-step gate inside the FastAPI response middleware. Belt-and-braces over column-level grants.

---

## Day-1 EOD checklist (this doc + 5 small things)

- [x] **CUTOVER-PLAN.md committed** at `bma-health-db/CUTOVER-PLAN.md`
- [ ] Canonical `.env.example` covering DATABASE_URL, ANALYTICS_DB_URL, REDIS_URL, MV-refresh creds, copied to all three repos (`bma-med/`, `bma-health/`, `bma-health-db/`)
- [ ] `pg_basebackup` of `bma_health` to encrypted tarball + checksum + restore-test on staging
- [ ] `bma_med_test` database created, `schema_init.sql` + `tables.sql` applied
- [ ] Old data dictionaries (`DATA-DICTIONARY.md`, `MEDICAL-DICTIONARY.md`) get a 5-line redirect header pointing to `bma-med/MED-FACTSHEET.md`
- [ ] Day-2 runbook drafted (drop production schema, apply new schema, smoke against minimal_data)

---

## Reference cross-walk

| Question | Answer in… |
|---|---|
| What's the new schema look like? | `bma-med/schema_init.sql`, `bma-med/generate_table_ddl.py` |
| What's the typing/cleaning logic? | `bma-med/clean.py`, `bma-med/CLEANING_NOTES.md` |
| What codes mean what? | `bma-med/CODES_REFERENCE.md` (auto-generated from factsheet) |
| How to deploy to a fresh Postgres? | `bma-med/DEPLOY.md` |
| How is the old DB shaped? | `bma-health-db/DATABASE.md` |
| What endpoints exist + which are broken? | `bma-health-db/API-AUDIT.md` |
| What was already proposed for cleanup? | `bma-health-db/CLEANUP-PROPOSAL.md`, `bma-health-db/ACTION-PLAN.md` |
| Why was the EAV broken? | `bma-health-db/ETL-TYPE-FIX-DESIGN.md` |
| What S2/S3/S4 look like? | The ULTRAPLAN (in chat); to be promoted to `ULTRAPLAN.md` at end of S1 |
