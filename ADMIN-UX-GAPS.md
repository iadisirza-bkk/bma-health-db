# BMA Health DB Admin Panel — UX Gaps & Bugs Audit

**Date:** 2026-04-27
**Scope:** `/Users/dev/bma-health-db/api/admin.py` (3035 lines) +
all 10 templates in `api/templates/admin/` + `api/templates/base.html`.
**Method:** Read-only review against the v3 schema (private/public split,
EAV measurements) and the `minimal_data/BMA_DATA_100_records/BMI_100/`
sample (7 portal + 5 app1 + 1 app2 = 13 CSV files).

---

## TL;DR (top issues)

1. **Bundle warning text is wrong.** It says "TRUNCATE raw_patients CASCADE"
   but the code does a per-source DELETE on `private.*` tables. Misleading
   in two ways: wrong table family (raw_* doesn't exist in v3 anymore in the
   way the text implies for this schema) and wrong DDL verb. (`upload_bundle.html:46-49`)
2. **`private.facility` is never seeded.** `etl/import_facilities.py` exists
   but is **not called from `_run_bundle_import` or `_run_import`**. v3 ETL
   (`etl/import_csv_v3.py:_validate_facility`) silently NULLs out
   `facility_code` if the FK target row is missing — visits land with
   `facility_code = NULL`, breaking `summary_facility`-style analytics.
   No user-visible warning. (admin.py — entire bundle flow lacks any
   facility check.)
3. **`private.variable_definition` is never bootstrapped.** Same story:
   `etl/bootstrap_variable_definitions.py` exists, must be run manually
   before any v3 import. With it empty, `_coverage_report` shows
   `0 matched / N unmatched` for every column and the EAV insert produces
   zero measurements — but there is no preflight that warns the user.
4. **History page mostly displays blank cells.** Template uses
   `item.timestamp`, `item.table`, `item.duration`; SQL returns
   `started_at`, `table_name`, `duration_seconds`. Three of the eight
   columns are silently empty. (`history.html:90, 92, 115` vs.
   `admin.py:1485-1495`).
5. **Most non-upload pages reference a dead schema.** Data-Quality,
   Cleansing-Report, Cross-Stats, and Agreement read from `raw_patients`,
   `raw_visits`, `raw_vitalsigns`, `raw_lab_results`, etc. — which are no
   longer the canonical store after migration 100/101/105. They will
   either return empty results or 500 against a fresh v3-only DB.
6. **No "Try with sample data" affordance.** A first-time admin has to
   know to manually drag the 13-file folder. There is no one-click
   "Load minimal_data sample" button, no preflight checklist, no per-source
   status dashboard explaining what was actually imported.

---

## 1. Page-by-page audit (10 templates)

### 1.1 `templates/admin/login.html` — Login

- **Route (GET):** `admin.py:857-867` — `login_page`
- **Route (POST):** `admin.py:870-910` — `login_submit`
- **What UI claims:** "เข้าสู่ระบบ Admin" — basic password gate.
- **What backend does:**
  - CSRF check (`_validate_csrf`).
  - 5 attempts / 5 min IP rate limit (`_check_login_rate`).
  - `hmac.compare_digest` against `ADMIN_PASSWORD` env var.
  - On success: 32-byte token in Redis (TTL=24h) or in-memory dict.
- **Mismatches:** None substantive.
- **Missing features the UI implies:**
  - The page suggests there's some kind of identity ("Admin"), but only
    a single shared password exists (`ADMIN_PASSWORD`). No per-user
    accounts, no audit attribution beyond IP. `import_history.uploaded_by`
    column exists in the SELECT (admin.py:1488) but is never populated
    on INSERT (admin.py:1407 + admin.py:2997-3003) — always NULL.
  - No "logged in as …" indicator anywhere in `base.html`.

---

### 1.2 `templates/admin/dashboard.html` — Dashboard

- **Route:** `admin.py:956-1179` — `dashboard`
- **What UI claims:**
  - "🎯 เป้าหมายโครงการ" coverage banner with cross-source unique count.
  - Per-source tabs (รวม/portal/app1/app2) with `N patients`.
  - 7 summary cards (patients, vitalsigns, visits, lab, homevisit,
    homehealth, lab_extended) showing `n_records` and `n_people`.
  - Materialized Views table.
  - "Refresh Views" button (top-right).
- **What backend does:**
  - Source filter via `pa.source_code`.
  - Reads from `private.patient_alias`, `private.visit_event`,
    `private.lab_event`, `private.patient_address`,
    `private.visit_measurement`, `private.lab_measurement`.
  - `coverage_stats` queries `private.patient_alias` and
    `private.patient WHERE NOT is_erased`.
  - MV row counts via `pg_matviews` + per-view COUNT(*).
- **Mismatches:**
  - **Card label "Lab Extended" (`dashboard.html:124`)** maps to v3 key
    `lab_extended` which the spec at `admin.py:1020-1025` defines as
    `private.lab_measurement` (rows of measurements). That is **not the
    same concept** as the legacy `raw_lab_extended` table (Portal-only,
    pulmonary/MSD lab). On v3, every lab row produces 1 N measurements,
    so this card double-counts vs. the user's mental model.
  - **Card label "Visits"** uses the same `private.visit_event` query as
    "Vitalsigns" (admin.py:998-1005), so they show **identical numbers**.
    UI-wise this is misleading — the user expects them to count different
    things (visits = N PIDs+VSTDATE pairs, vitalsigns = subset that has
    HBPN/LBPN). After v3 they're collapsed into one event table; the
    template should either drop one card or label them more honestly.
  - **`refreshed_at: "-"`** is hardcoded in admin.py:1146 — the "Active"
    badge in the views table does not reflect actual freshness; a stale
    MV looks identical to a fresh one.
  - **"Refresh Views" button** posts to `/admin/refresh` (admin.py:1438)
    which calls `etl.refresh_all_summaries(cur)` from the **legacy**
    `etl/import_csv.py` — that function operates on the old
    `summary_district_disease` MVs. Current v3 schema uses
    `public.refresh_all_mvs()` (DB function) per `admin.py:768` and
    `admin.py:2784`. The dashboard's manual refresh therefore probably
    does NOT refresh the views the dashboard lists.
- **Missing features:**
  - Per-source last-import timestamp (admin.py only counts current totals).
  - No indicator if `private.facility` or `private.variable_definition`
    is empty (both bootstraps required for sane v3 import).
  - "Refresh Views" doesn't show what was refreshed or what failed —
    silently flashes "Materialized views refreshed successfully".

---

### 1.3 `templates/admin/upload.html` — Single CSV upload

- **Route (GET):** `admin.py:1185-1201` — `upload_page`
- **Route (POST upload preview):** `admin.py:1204-1376` — `upload_csv`
- **Route (POST commit):** `admin.py:1382-1432` — `run_import` →
  `_run_import` thread (admin.py:656-814)
- **What UI claims:**
  - "Schema v3 — EAV-based, two-schema (private/public)."
  - "Variable Mapping Coverage" + unmatched-column drill-down.
  - "ขั้นตอน ETL ที่จะรัน" (numbered list) — `pt` upserts `private.patient`
    + alias; `app2` auto-splits; others resolve `patient_id` then EAV.
- **What backend does:**
  - Source selector required (must be portal/app1/app2; admin.py:1224).
  - File-type auto-detect via column heuristics (`_detect_file_type`,
    admin.py:399-441).
  - 500 MB max per file (admin.py:1253).
  - Encoding fallback chain: utf-8 → tis-620 → cp874 → latin-1.
  - PII columns stripped from preview (admin.py:391-396; cols include
    IDCARD, FNAME, PHONE, etc.).
  - Coverage report compares CSV columns to
    `private.variable_definition WHERE source_code = … AND deprecated_at IS NULL`.
  - Stores `df` (full DataFrame in memory) in `_upload_cache` keyed by
    `upload_id`, max 10 entries, 1-hour TTL.
- **Mismatches:**
  - **"Frontend จะเห็นข้อมูลใหม่ภายใน 10 นาที (React Query staleTime)"
    (upload.html:248)** is unrelated to the bundle/import flow — caching
    behavior depends on the consumer. Misleading promise. After commit
    the worker calls `cache_flush_all()` (admin.py:786-791) — the
    *server* caches are flushed. Browser-side React Query staleTime is
    not under server control.
  - **"แต่ Frontend เข้าไม่ได้" (upload.html:177)** is the rationale for
    public.mv — but the UI talks about "MV refresh ทุกวัน 03:00" which
    refers to a cron job; the actual refresh happens at end of every
    import via `public.refresh_all_mvs()`. There is no daily 03:00 cron
    that I can see configured.
  - **In-memory `df` in `_upload_cache`** is a memory bomb for the single
    upload path — a 500 MB CSV stored as DataFrame can be 2-3 GB resident.
    The bundle path uses tempfiles on disk; the single path does not.
  - **`run_import` returns to `/admin/history` with a flash message
    (admin.py:1430-1432)** but the user has zero way to see preview
    coverage on the history page; the preview-stage `coverage` data is
    discarded after commit.
- **Missing features:**
  - No way to commit a multi-file batch from this page — must use bundle.
  - The "Cancel" button (`upload.html:290-293`) wipes the upload_id
    out of the URL but the cached df remains for ≤ 1h, taking RAM.
  - No "Save preview as template" or "Re-use last source/file_type"
    affordance.

---

### 1.4 `templates/admin/upload_bundle.html` — Bundle upload (CRITICAL — see §2)

- **Route (GET):** `admin.py:2837-2852` — `upload_bundle_page`
- **Route (POST):** `admin.py:2855-3035` — `upload_bundle_submit` →
  `_run_bundle_import` thread (admin.py:2635-2834)
- **What UI claims (verbatim):**
  > "อัปโหลดได้ถึง 13 ไฟล์ CSV จาก 3 แหล่ง (Portal + App1 + App2) ในครั้งเดียว
  > — ข้อมูลจะ stack รวมกัน ไม่ merge"
  >
  > "**ข้อมูลเดิมจะถูกลบทั้งหมด** — ระบบ TRUNCATE raw_patients CASCADE ก่อน
  > import แล้ว stack ข้อมูลใหม่ทั้ง 3 source"
- **What backend does:**
  - Streams each file to a tempfile (admin.py:2895-2916), max 1 GB
    per file.
  - Source detection: relpath segment match (`portal`/`app1`/`app2`)
    OR filename fallback OR user-supplied `default_source`.
  - File-type detection by filename pattern (admin.py:2611-2632).
  - Refuses duplicate (source, file_type) pairs (admin.py:2949-2959).
  - Acquires advisory lock 0xBA10AD17 (admin.py:2654-2661).
  - **DELETES per source** via `_delete_for_sources` (admin.py:193-232) —
    only the sources present in this bundle. Other sources' data is
    preserved. Cascades through `private.visit_event`, `private.lab_event`,
    `private.patient_alias`, then orphan `private.patient`.
  - Imports in fixed order: portal → app1 → app2 (admin.py:2584).
    Within each source, `pt.csv` first then child files.
  - Calls `etlv3.import_patients`, `import_visits_and_measurements`,
    `import_lab`, `import_app2`.
  - Single transaction across all files (admin.py:2776 `conn.commit()`).
  - On commit: `public.refresh_all_mvs()`, then cache flush.
  - Tempfiles unlinked in `finally`.
- **Mismatches (BUGS):**
  1. **"TRUNCATE raw_patients CASCADE"** — wrong on three counts:
     - It's not a TRUNCATE; it's a `DELETE FROM … WHERE source_code IN (…)`.
     - It does **not** target `raw_patients`; it targets `private.visit_event`,
       `private.lab_event`, `private.patient_alias`, `private.patient`.
     - It does **not** wipe everything; only sources in the bundle.
  2. **"ข้อมูลเดิมจะถูกลบทั้งหมด"** is therefore an over-statement.
     If the user uploads only `portal/`, app1/app2 data is preserved.
  3. **App2 path (`admin.py:2706-2722`)** scopes `source = "app2"` but the
     `_delete_for_sources` call already happened with `sources_in_bundle`
     including 'app2'. Fine in isolation, but `app2` data lives in same
     tables as portal/app1 with `source_code='app2'` — the warning text
     is technically accurate only "for the sources you're uploading."
- **Missing features:**
  - **No "Try with minimal_data sample" button** — the user has to
    know the folder layout and drag-drop it. Critical onboarding gap.
  - **No preflight checklist** — does the user have:
    - all 7 portal files? all 5 app1? the 1 app2?
    - `private.variable_definition` bootstrapped?
    - `private.facility` populated?
    - `private.geo_*` (district / zone / province) populated?
  - **No partial-failure surfacing.** If 3 of 12 files succeed and one
    crashes, the entire transaction rolls back (correct), but the user
    sees only "error" with the last exception message. No per-file
    breakdown of "imported / skipped / failed". The `steps_done` list
    (admin.py:2645) is populated but not shown to the user.
  - **No "kill running import" button.** If a 200K-row import is going
    to fail anyway, the user can't stop it.
  - **Drag-and-drop a single zip file** is not supported — must drag a
    folder which only works in Chromium-derived browsers. Safari users
    have to use the multi-file picker.

---

### 1.5 `templates/admin/history.html` — Import history

- **Route (GET):** `admin.py:1476-1533` — `history_page`
- **Route (POST clear):** `admin.py:1536-1577` — `history_clear`
- **API (live progress poll):** `admin.py:1580-1616` — `/admin/api/import-progress`
- **What UI claims:**
  - List of last 50 imports with status badges.
  - Live progress bar polling every 2 s (history.html:213-287).
  - Stale-MV banner when `view_refresh_status='failed'`.
  - "Clear history" buttons (errors only / all completed).
- **What backend does:**
  - SELECT 16 columns from `import_history` ORDER BY `started_at DESC`.
  - Live JSON endpoint enriches with `rows_per_sec`, `eta_sec`.
- **Mismatches (BUGS):**
  - **Template uses field names that the SQL never returns:**
    - `item.timestamp` (history.html:90)  — SQL returns `started_at`.
    - `item.table` (history.html:92) — SQL returns `table_name`.
    - `item.duration` (history.html:115) — SQL returns `duration_seconds`.
    All three render blank. (admin.py:1485 SELECT vs. template references.)
  - **`uploaded_by` column SELECTed but never INSERTed** (admin.py:1488,
    1407, 2997). Always NULL. No user attribution.
  - **`item.id or loop.index`** (history.html:89) — IDs are real DB
    primary keys; the fallback should never trigger and is a code smell.
- **Missing features:**
  - No filter by status (success / error / running) or by source.
  - No pagination — hardcoded LIMIT 50, no "load more".
  - "View details" of a successful import doesn't list **per-file row
    counts** (the bundle import knows them — `steps_done` — but discards
    them).
  - No way to re-run a failed import without re-uploading.

---

### 1.6 `templates/admin/data_quality.html` — Data quality

- **Route:** `admin.py:1623-1681` — `data_quality_page`
- **What UI claims:**
  - "Blocked Fields (100% null)" banner.
  - Per-table fill % with progress bar.
  - Per-field null analysis with "BLOCKED" badge.
  - Recent Import History table at the bottom.
- **What backend does (admin.py:1633-1671):**
  - Hardcoded list `tables = ["raw_patients", "raw_visits", …]`.
  - For each table: `SELECT COUNT(*)`, then per-column null counts.
- **Mismatches (CRITICAL):**
  - **`raw_patients`, `raw_visits`, `raw_vitalsigns`, `raw_homevisit`,
    `raw_homehealth`, `raw_lab_results`, `raw_lab_extended` are all
    legacy v2 tables.** v3 schema (migration 100 + 101) replaces them
    with `private.patient`, `private.visit_event`, `private.visit_measurement`
    (EAV), `private.lab_event`, `private.lab_measurement`.
  - On a fresh v3-only DB the SELECTs raise "relation does not exist"
    and the entire `try` block silently fails (admin.py:1670 `except: pass`).
    Page renders empty "No data" cards instead of any error.
- **Missing features:**
  - No data-quality view of the **EAV** layer (`visit_measurement` per
    `variable_definition.csv_column_name`), which is what v3 ETL actually
    produces.
  - No data-quality breakdown per `data_source`.

---

### 1.7 `templates/admin/cleansing_report.html` — Cleansing report

- **Route:** `admin.py:2147-2272` — `cleansing_report_page`
- **What UI claims:**
  - 6-category table of inclusion criteria + missing % per source.
  - Methodology footer explains "missing = NULL = ไม่ได้บันทึก ∪ หลุดเกณฑ์".
- **What backend does:**
  - Iterates `INCLUSION_CRITERIA` (admin.py:1836-1969) — 30 entries, all
    targeting **legacy `raw_*` tables**.
  - For each (table, source), runs `COUNT(col)` on data.
- **Mismatches (CRITICAL):**
  - Same as §1.6 — every `raw_*` reference is dead in v3. The SQL fails
    silently at admin.py:2230 (`except Exception … pass`).
  - The "Inclusion rule" column says e.g. `"40 ≤ x ≤ 300 mmHg"` for
    `raw_vitalsigns.sbp` — but **v3 ETL applies these in
    `etl/import_csv_v3.py`** (and writes to `private.visit_measurement.value_num`,
    not a typed column). The column-level rule presentation is misleading
    on v3.
- **Missing features:**
  - No way to drill into a specific source × variable to see *which*
    out-of-range values were collapsed to NULL.

---

### 1.8 `templates/admin/cross_stats.html` — Cross-source stats

- **Route:** `admin.py:2275-2322` — `cross_stats_page`
- **What UI claims:**
  - 3 tabs: Coverage (50 districts × 3 sources heatmap), Distribution
    (prevalence + chi-square), Data Quality.
- **What backend does:**
  - `_coverage_matrix` (admin.py:1722-1738) — queries `summary_district_disease`
    (now a compat VIEW per migration 105 — works) and `ref_districts`
    (legacy table).
  - `_distribution_comparison` (admin.py:1741-1814) — queries
    `summary_district_disease` (works as compat view).
  - `_data_quality_per_source` (admin.py:2004-2076) — queries `raw_*` tables
    (DEAD on v3-only).
  - "sources_present" probe queries `raw_patients` (admin.py:2287) —
    DEAD on v3-only; will return empty set, the warning banner will say
    "ยังไม่มีข้อมูลจาก: portal, app1, app2" even after a successful import.
- **Mismatches:**
  - **`raw_patients` query at admin.py:2287** is the canonical example —
    on v3-only DB this returns empty, every tab incorrectly shows
    "ยังไม่มีข้อมูล".
  - **`ref_districts` (admin.py:1733)** — was renamed in migration 014
    or 103 to `private.geo_district` (per the v3 schema). Need to verify
    if `ref_districts` survives as a compat view; if not, this also fails.
- **Missing features:**
  - No tab for "EAV variable usage" — what % of patients have each
    `variable_id` populated.

---

### 1.9 `templates/admin/agreement.html` — Agreement (Bland-Altman + κ)

- **Route:** `admin.py:2083-2140` — `agreement_page`
- **What UI claims:** Same-person agreement analysis between source pairs
  (portal-app1, portal-app2, app1-app2).
- **What backend does:**
  - Imports `services.agreement_service.build_agreement_report`.
  - Probes presence via `SELECT data_source, COUNT(*) FROM raw_patients`
    (admin.py:2113) — DEAD on v3-only.
- **Mismatches:**
  - Same `raw_patients` issue as cross-stats: the "fast-path" presence
    check fails, so `report` is never built, and the page always shows
    "ไม่สามารถวิเคราะห์ได้ในคู่นี้" even when v3 data exists.
- **Missing features:**
  - No way to pick which variables to compare (currently the service
    has its own hard-coded list).

---

### 1.10 `templates/admin/logs.html` — Logs

- **Route:** `admin.py:2328-2368` — `logs_page`
- **What UI claims:** "Logs — บันทึกการทำงานของระบบ", "Refresh" button.
- **What backend does:**
  - SELECTs last 100 import_history rows, formats as text lines.
  - **It is just a text-formatted view of the same data shown on
    /admin/history.** No actual application log file is read.
- **Mismatches:**
  - The label "Logs" implies system logs (uvicorn output, errors,
    stack traces). What you get is import history pretty-printed as
    monospace text. Severely misleading.
  - "Refresh" button is just `<a href="/admin/logs">` — same as a
    page reload. Visually it looks like an action.
- **Missing features:**
  - No actual `logger.error/warning/exception` output (those are written
    to uvicorn's stderr but nowhere queryable).
  - No log streaming, no severity filter, no search.

---

## 2. Upload-flow deep dive (`/admin/upload-bundle`)

The bundle upload is the **production path** for ingesting the
3-source dataset. It is also the page most likely to mislead a new
admin. Going question by question:

### 2.1 Is the warning text accurate?
**No.** `upload_bundle.html:46-49` says:
> "ข้อมูลเดิมจะถูกลบทั้งหมด — ระบบ TRUNCATE raw_patients CASCADE ก่อน import"

Reality (`admin.py:193-232`, called from `admin.py:2673`):
- It's a per-source `DELETE FROM private.visit_event/lab_event/patient_alias/patient`.
- `raw_patients` is not touched (and, in current v3 schema, doesn't
  hold canonical data).
- Only sources **present in this bundle** are deleted. If you upload
  just `portal/`, app1+app2 data is preserved — the warning over-states
  destruction.

**Recommended copy:**
> "การอัปโหลดนี้จะลบข้อมูลเก่าของแหล่งที่อัปโหลดเท่านั้น
> (portal / app1 / app2 อย่างใดอย่างหนึ่งหรือหลายอย่าง). แหล่งอื่นๆ จะไม่ถูกแตะต้อง.
> ระบบจะ DELETE ตาราง `private.visit_event`, `private.lab_event`,
> `private.patient_alias`, แล้วลบ `private.patient` ที่ไม่มี alias
> เหลืออยู่ — ทั้งหมดอยู่ใน 1 transaction (rollback ได้ถ้าล้มเหลว)."

### 2.2 Can you test with `minimal_data` without manually selecting 13 files?
**No.** The page has two modes (folder picker, multi-file picker), both
of which require the operator to navigate to
`/Users/dev/bma-health-db/minimal_data/BMA_DATA_100_records/BMI_100/`
manually. There is no:
- "Run sample import" button.
- "Use bundled fixture" link.
- Auto-detection of the fixture path on first run.

### 2.3 Do error messages guide the user to fix?
**Mostly no.** Examples:
- Missing pt.csv but uploading vitalsignslf.csv → at runtime
  `etlv3.import_visits_and_measurements` raises
  `ValueError("No patients found for source=… Upload pt.csv first.")`
  (admin.py:722-726). Surfaced as a single-line flash message via
  `_sanitize_error`. There's no preflight check at upload-time that
  warns BEFORE the 200K-row import starts.
- Missing `private.variable_definition` rows → ETL silently produces
  zero `visit_measurement` rows (since the EAV pivot has no columns to
  match). User sees "Bundle import succeeded — 100 rows" but the EAV
  table is empty. **No warning.**
- Missing `private.facility` → all visits get `facility_code = NULL`.
  Again, no warning.
- A bad encoding produces "Failed to parse CSV: <stack>" via raw
  exception (admin.py:1283) — user has to know what tis-620 vs cp874
  means.

### 2.4 Where does the user verify the import worked?
**Inadequately.** After commit:
- Flash message lists files+sources+sizes (admin.py:3030-3034).
- /admin/history shows ONE bundle row with `rows_imported = total_imported`
  (sum across 13 files).
- /admin/dashboard summary cards re-query private.* (works).

But there is **no per-source, per-file table that says e.g.**:

| source | file | rows in CSV | rows imported | rows skipped | duration |
|--------|------|-------------|---------------|--------------|----------|
| portal | pt.csv | 100 | 100 | 0 | 0.4 s |
| portal | pthistory.csv | 100 | 100 | 0 | 0.6 s |
| ... | ... | ... | ... | ... | ... |
| app2 | app2.csv | 100 | 100 patients + 100 visits | 0 | 1.2 s |

The `steps_done` list (admin.py:2645, 2734, 2768) is built but only
emitted to log + flash message. The detailed per-file breakdown is
hidden.

### 2.5 Is facility import included or skipped?
**Skipped.** `etl/import_facilities.py` is **never invoked from
`_run_bundle_import` or `_run_import`**. Confirmed by grep: zero
references in `api/admin.py` or `db/migrations/`. Result:
- `private.facility` is empty unless the operator runs the script
  manually (`python etl/import_facilities.py`).
- v3 ETL's `_validate_facility` (etl/import_csv_v3.py:217-228) treats
  every HPTCODE as invalid and silently sets `facility_code = NULL`.
- Downstream: `summary_facility` MV / facility-level KPIs / map
  facility pins all have nothing to join on.
- **No UI surface tells you this.** Dashboard doesn't show "facility
  table empty" warning.

### 2.6 Are there pre-flight checks?
**No.** The form accepts whatever you drop. No checks for:
- All 13 expected files present.
- Required `pt.csv` per source.
- `private.variable_definition` non-empty.
- `private.facility` non-empty.
- `private.geo_district` populated.
- `private.data_source` rows for portal/app1/app2 (FK requirement —
  failing this raises an FK violation deep in ETL).

### 2.7 What happens on partial failure?
Single transaction → rollback works correctly: if file 8/13 crashes,
files 1-7 are rolled back at the DB level. The user sees:
- import_history row → status = `error`, `error_message` =
  `_sanitize_error(exc)[:500]`.
- All tempfiles unlinked.
- **No way to know which file failed** unless the exception text mentions
  it (sometimes — depends on which ETL fn raised). The UI doesn't show
  the partial `steps_done` log that the worker accumulated.

### 2.8 CSRF / session expiry behavior
- CSRF mismatch → `HTTPException(403, "CSRF validation failed")` →
  bare FastAPI 403 page. Not friendly.
- Session expiry (>24h) → next request gets a redirect to /admin/login.
  But for a long-running bundle upload, an uploadthat exceeds 24h would
  still complete (worker thread doesn't re-check auth) — but the
  redirect happens after the user reloads.
- Login page itself prints CSRF error inline (login.html:53-60), which
  is good. Other pages just 403.
- **Drop-zone form does NOT preserve uploaded files** if CSRF fails —
  the user has to re-drop everything.

---

## 3. Bug list with severity

### 🔴 Critical (data integrity / wrong behavior visible to user)

| # | Where | Bug | Recommended fix |
|---|-------|-----|------------------|
| C1 | `admin.py:1485-1495` SELECT vs `history.html:90,92,115` | Template references `item.timestamp / item.table / item.duration` — the SQL only exposes `item.started_at / item.table_name / item.duration_seconds`. Three columns silently render empty. | Either alias in SQL (`SELECT … started_at AS timestamp, table_name AS table, duration_seconds AS duration`) or update the template to use the canonical column names. |
| C2 | `_run_bundle_import` (admin.py:2635-2834) | `etl/import_facilities.py` is never invoked. `private.facility` stays empty → every visit's `facility_code` is set to NULL by `etl/import_csv_v3.py:_validate_facility`. Facility-level analytics return zero data without any error. | Add a `_ensure_facilities()` step at start of `_run_bundle_import`: if `SELECT COUNT(*) FROM private.facility = 0`, invoke `etl/import_facilities.py` against the bundled XLS. Surface "facility table seeded (N rows)" in the success flash. |
| C3 | Same as C2, but for `private.variable_definition` | Without bootstrap, `etlv3` cannot map any CSV column → variable_id, so `visit_measurement` and `lab_measurement` get zero rows even on a "successful" bundle import. Coverage report on /admin/upload shows 0 matched. | Add `_ensure_variable_definitions()` at start of bundle import: if table empty, run `etl/bootstrap_variable_definitions.py` against the bundled XLSX. Block bundle commit until this is non-empty. |
| C4 | `data_quality.html`, `cleansing_report.html`, `cross_stats.html`, `agreement.html` | All four pages query `raw_patients / raw_visits / raw_vitalsigns / raw_lab_results` etc. — which migration 105 dropped. On a v3-only DB the SELECTs error and the pages render "no data" without any banner explaining the schema mismatch. | Rewrite each query against `private.*` + `public.mv_visit_resolved`. Until then, hide each page from base.html when the underlying tables are missing, OR display a banner "v3 schema migration in progress — page not yet rebuilt". |
| C5 | `upload_bundle.html:46-49` warning text | Says "TRUNCATE raw_patients CASCADE" when the actual code does per-source DELETE on `private.*` tables. Two factual errors (verb + table). | Replace with the recommended copy in §2.1 above. |
| C6 | `admin.py:1438-1470` `/admin/refresh` | Calls `etl.refresh_all_summaries(cur)` (legacy v2 path) instead of `public.refresh_all_mvs()` used everywhere else (admin.py:768, 2784). The dashboard's manual "Refresh Views" button likely refreshes nothing relevant. | Replace with `cur.execute("SELECT view_name, status FROM public.refresh_all_mvs()")` and report which views succeeded / failed. |

### 🟠 Major (misleading UX / could lead to bad decisions)

| # | Where | Bug | Recommended fix |
|---|-------|-----|------------------|
| M1 | `dashboard.html:117-148` summary cards | "Visits" and "Vitalsigns" cards show identical numbers (both query `private.visit_event WHERE cancel_status=0`). User expects them to be different. | Re-define one of them: e.g. "Visits" = distinct `(patient_id, visit_date)`, "Vitalsigns" = visits with at least one vital-related `visit_measurement.variable_id`. Or drop the redundant card. |
| M2 | `dashboard.html:112` per-source tab counts | "patients" count uses `source_breakdown[*].n` which is `COUNT(*) FROM patient_alias` — i.e. **alias rows**, not unique patients. A patient with 2 aliases counts twice in the tab label. | Use `COUNT(DISTINCT patient_id)` per source. |
| M3 | `upload.html:248` | "Frontend จะเห็นข้อมูลใหม่ภายใน 10 นาที (React Query staleTime)" is incorrect — server caches are flushed at end of import (admin.py:786-791). | Remove or rephrase: "Frontend cache flush ทันทีหลัง import; React Query บนเบราว์เซอร์อาจค้างได้ตาม staleTime." |
| M4 | `logs.html` | Page is labeled "Logs" but only shows pretty-printed import_history rows. No app-level logs. | Either rename the page to "Import log" OR actually wire up a log capture (e.g. a rotating file handler whose tail is rendered here). |
| M5 | `dashboard.html:177-179` MV "Active" badge | Hardcoded `refreshed_at: "-"` (admin.py:1146); every view shows "Active" regardless of last refresh time or row count of 0. | Add `pg_stat_user_tables.last_vacuum`/`pg_matviews` last-refresh timestamp; show stale badge if > 1h old or row count = 0. |
| M6 | `history_clear` (admin.py:1536-1577) | Confirm dialog says "งานที่กำลังรัน (running) จะถูกเก็บไว้" but `mode='all'` deletes EVERYTHING including running rows (admin.py:1557-1558). The worker thread is **not cancelled** — it just proceeds with a deleted history row, leaving an orphan import in the DB. | (a) Refuse `mode='all'` while any row has `status='running'`. (b) Update copy to admit running rows are also wiped on "all". |
| M7 | `upload_csv` (admin.py:1204-1376) caches full `df` in memory | `_upload_cache[upload_id]['df']` stores the entire DataFrame; with 500 MB CSV files this can be 2-3 GB resident. Bundle path correctly uses tempfiles. | Stream single-file uploads to a tempfile too; cache the path, not the DF. Re-read on commit. |
| M8 | `_run_bundle_import` step `_update_progress("delete prior data", 1)` (admin.py:2672) | Progress jumps to 1% and stays there during a potentially-multi-second DELETE (e.g. 500 K rows). User sees nothing happen. | Use `_make_progress_cb` style throttled updates during the DELETE (estimate by source row counts before deleting). |

### 🟡 Minor (cosmetic / clarity)

| # | Where | Bug | Recommended fix |
|---|-------|-----|------------------|
| m1 | `base.html` nav | 9 menu items in the desktop nav, no grouping. On a 1280-wide window they wrap awkwardly. | Group: "Operate" (Dashboard, Upload, Bundle, History, Logs), "Analyze" (Data Quality, Cleansing, Cross-Stats, Agreement). |
| m2 | `login.html` | No "Forgot password" link; with single shared password this is fine, but no copy explains that. | Either remove the appearance of a per-user account (no "Admin" label) OR add a help link. |
| m3 | `history.html:89` `{{ item.id or loop.index }}` | DB IDs are NEVER null (PK), so `or loop.index` is dead code. | Remove the fallback. |
| m4 | `upload_bundle.html:78-79` | Two `<input type="file">` with the same `name="files"`. The JS at line 305-309 sets one `.name=''` on submit — fragile. | Use a single input and toggle its `webkitdirectory` attribute. |
| m5 | `cleansing_report.html:14` | "ETL แปลง out-of-range → NULL" — accurate for legacy, but in v3 the rule lives in `etl/import_csv_v3.py` checking each `value_num` insert. Same effect, but the user can't trace the rule to the new file. | Add a link to `/Users/dev/bma-health-db/etl/import_csv_v3.py` (or render the rules from a registry stored in `private.variable_definition.validation_rules`). |
| m6 | `agreement.html:62` | Empty-state CTA: "ไปอัปโหลดได้ที่ Bundle Upload" — fine. But for a brand-new admin who has never logged in before, there's no "Upload sample data" shortcut. | Add a "Try with minimal_data" button on the empty state. |
| m7 | `dashboard.html:14-21` "Refresh Views" button | No tooltip explaining what gets refreshed (and given M5/C6 above, the answer is "the wrong thing"). | Add tooltip + show last refresh time per view. |
| m8 | All flash messages (`base.html:234-258`) | 30-second `max_age` cookie (admin.py:837) means a slow page load on a flaky network can lose the flash. | Bump to 120 s or store in session. |
| m9 | Mobile nav (`base.html:146-230`) | Hamburger menu has no close button or backdrop dismiss; tapping outside doesn't close it. | Add `onclick` on a backdrop div. |
| m10 | `cross_stats.html:243` | Jinja: `quality[tbl_key][0].fields | length if quality[tbl_key] else 0` — assumes quality is non-empty list of dicts; will crash if `quality[tbl_key]` is undefined. | Use `(quality[tbl_key] or [{}])[0].get('fields', {})|length`. |

---

## 4. Quick wins (each <30 minutes)

In rough priority order:

1. **Fix history.html column names** (3 line edits in `history.html` OR
   3 SQL aliases in admin.py:1485). Renders the page properly.
2. **Replace bundle warning copy** in `upload_bundle.html:46-49` with
   the truthful version from §2.1.
3. **Add a "Try with minimal_data" button** to `upload_bundle.html` —
   POSTs `?fixture=minimal_data/BMA_DATA_100_records/BMI_100` to
   a new server-side endpoint that reads files directly from disk,
   no upload required. (~25 minutes including endpoint.)
4. **Replace "Refresh Views" backend** to call `public.refresh_all_mvs()`
   (admin.py:1438-1470). One-liner change.
5. **Render `steps_done` in history.html** as a `<details>` block per
   bundle row — currently lost in logs only. Pass through to the row
   as JSON in `progress_step` once import completes.
6. **Add empty-table banner on /admin/data-quality** when `SELECT COUNT(*)
   FROM raw_patients` errors out — currently the whole page just looks
   broken. (~5 lines to add a try/except and render a "schema migrated to
   v3 — this page is being rebuilt" alert.)
7. **Hide / rename "Logs" until it shows real logs.** Either remove the
   nav entry or rename to "Import Log".
8. **Distinct-patient tab counts** on dashboard (M2): change SQL at
   admin.py:1068 to `SELECT pa.source_code, COUNT(DISTINCT pa.patient_id) AS n`.
9. **"Per-source last import" column on dashboard** — add SELECT MAX(uploaded_at)
   FROM private.import_batch GROUP BY source_code, render under each tab.
10. **Friendlier 403 on CSRF mismatch** — wrap `_validate_csrf` failures
    in a redirect-to-login with flash "Session expired, please re-login."
    instead of bare 403. (~10 lines around admin.py:1220-1222 and other
    CSRF check sites.)

---

## 5. Recommended additions (longer-haul)

### 5.1 "Bootstrap from sample" wizard

A single page at `/admin/bootstrap` that runs in sequence:
1. **Variable definitions** — invoke `bootstrap_variable_definitions.py`
   if `private.variable_definition` is empty.
2. **Facilities** — invoke `import_facilities.py` if `private.facility`
   is empty.
3. **Geography** — verify `private.geo_district` has 50 rows; otherwise
   surface a "ask a developer to run migration 103/104".
4. **Sample data** — invoke bundle import on `minimal_data/.../BMI_100`.
5. **Refresh MVs** — `public.refresh_all_mvs()`.

Each step has its own progress bar and can be re-run idempotently. The
wizard is the **first thing a new admin sees** if all four prerequisites
fail — replacing the current dashboard's silent zeros.

### 5.2 Per-source health-check dashboard

Add a section to `/admin/dashboard` (or a dedicated `/admin/health`
page) that shows:

| Source | data_source | patients | last_import | variable_def rows | facility coverage | MV freshness |
|--------|-------------|----------|-------------|--------------------|-------------------|--------------|
| portal | ✓ | 32,418 | 2026-04-21 14:32 | 412/412 | 88% | fresh (3 min) |
| app1   | ✓ | 8,902  | 2026-04-21 09:11 | 311/350 | 0% (table empty) | stale (4 h) |
| app2   | ✗ | 0      | —              | —      | —    | — |

Each cell is a clickable link to drill in. This is the **single most
valuable page** for an admin trying to figure out "what state is the
DB in?" — currently they have to query psql.

### 5.3 Pre-flight checklist on `/admin/upload-bundle`

Before the user can click "นำเข้าทั้งหมด", show a checklist:

```
□ private.variable_definition ✓ (412 rows)
□ private.facility ✗ (0 rows) — Run /admin/bootstrap first
□ private.geo_district ✓ (50 rows)
□ private.data_source ✓ (portal, app1, app2 registered)

Files in this bundle:
  portal/ — 7 files ✓ (all expected files present)
  app1/   — 5 files ✓ (all expected files present)
  app2/   — 1 file  ✓
```

If any prerequisite fails, the submit button is disabled with a tooltip.

### 5.4 Validation preview for bundle (extend `/admin/upload`'s coverage report)

Currently `/admin/upload` (single-file) shows variable mapping coverage.
The bundle path skips this entirely. Add a **dry-run mode**: parse all
13 files, compute per-file coverage report (matched / unmatched /
estimated rows imported), show in a table, then offer "Confirm import"
button. The current single-shot "drop-and-go" is too fragile for users
who want to verify before committing.

### 5.5 Per-file results in import history

Replace the bundle's single "rows_imported = sum" with a JSON column
in `import_history` that stores an array of `{source, file, rows_in_csv,
rows_imported, rows_skipped, duration_ms}`. Render as an expandable
sub-table on `/admin/history`.

### 5.6 Background task cancellation

Add `/admin/api/import-cancel/{history_id}` that flips a flag the worker
checks at each chunk boundary. UI: "Cancel" button on the running row
in `/admin/history`. Useful when a bad upload is going to fail anyway.

### 5.7 Audit attribution

Every admin action (login, upload, import, erase, history clear) should
write a row to `audit_log` with `(timestamp, ip, action, payload_summary)`.
`uploaded_by` in `import_history` should be filled (currently always
NULL — admin.py:1407, 2997 don't set it).

### 5.8 Real-time log viewer

Replace the current "Logs" page with a real log tail (e.g. rotating
file handler `logs/admin.log`, last 500 lines, severity filter, search).
Eliminate the duplication between `/admin/logs` and `/admin/history`.

### 5.9 Encoding picker on `/admin/upload`

Currently the encoding fallback chain is hardcoded (admin.py:1267).
Expose a select: "auto / utf-8 / tis-620 / cp874" with a preview of
the first 5 rows so the user can pick the right one when the auto
guess produces garbage characters.

### 5.10 Session timeout warning

For a 24-hour session with a bundle import that may take 10 minutes,
the user benefits from a JS countdown timer in `base.html` showing
"Session expires in: 23 h 47 m" and offering "Extend session" button
that pings `/admin/api/session-renew`.

---

## Appendix A: Key files & line references

| File | Lines | Purpose |
|------|-------|---------|
| `/Users/dev/bma-health-db/api/admin.py` | 656-814 | `_run_import` (single-file worker) |
| `/Users/dev/bma-health-db/api/admin.py` | 2635-2834 | `_run_bundle_import` (bundle worker) |
| `/Users/dev/bma-health-db/api/admin.py` | 193-232 | `_delete_for_sources` (the actual "delete logic") |
| `/Users/dev/bma-health-db/api/admin.py` | 1438-1470 | `/admin/refresh` (calls **legacy** ETL) |
| `/Users/dev/bma-health-db/api/admin.py` | 1485-1495 | History SELECT (column names) |
| `/Users/dev/bma-health-db/api/admin.py` | 1633-1671 | data-quality (queries dead `raw_*` tables) |
| `/Users/dev/bma-health-db/api/admin.py` | 1836-1969 | `INCLUSION_CRITERIA` (all `raw_*`) |
| `/Users/dev/bma-health-db/api/admin.py` | 2113, 2287 | `raw_patients` presence check (dead on v3) |
| `/Users/dev/bma-health-db/etl/import_facilities.py` | full file | Facility importer — **never invoked** |
| `/Users/dev/bma-health-db/etl/bootstrap_variable_definitions.py` | full file | Variable bootstrap — **never invoked** |
| `/Users/dev/bma-health-db/etl/import_csv_v3.py` | 217-228 | `_validate_facility` silently NULLs unknown HPTCODE |
| `/Users/dev/bma-health-db/api/templates/admin/upload_bundle.html` | 46-49 | Wrong "TRUNCATE" warning copy |
| `/Users/dev/bma-health-db/api/templates/admin/history.html` | 90, 92, 115 | Wrong field names → blank cells |
| `/Users/dev/bma-health-db/api/templates/admin/data_quality.html` | full file | Renders against dead schema |
| `/Users/dev/bma-health-db/api/templates/admin/cleansing_report.html` | full file | Same |
| `/Users/dev/bma-health-db/api/templates/admin/cross_stats.html` | full file | Same (partial — coverage tab works via compat view) |
| `/Users/dev/bma-health-db/api/templates/admin/agreement.html` | full file | Same |
| `/Users/dev/bma-health-db/api/templates/admin/logs.html` | full file | Mislabeled — shows import_history, not logs |
| `/Users/dev/bma-health-db/db/migrations/100_schema_v3_private.sql` | 74-91, 241 | `private.facility` schema + FK on `visit_event` |
| `/Users/dev/bma-health-db/db/migrations/105_drop_legacy_mvs.sql` | full file | Dropped raw_* MVs, replaced with compat views |
| `/Users/dev/bma-health-db/minimal_data/BMA_DATA_100_records/BMI_100/` | 13 files | Sample bundle (portal=7, app1=5, app2=1) |

## Appendix B: Sample bundle file inventory

```
portal/   (7 files, ~131 KB total)
  pt.csv             9.8 KB
  pthistory.csv     14.0 KB
  vitalsignslf.csv  27.2 KB
  homevisit.csv     21.4 KB
  homehealth.csv    17.9 KB
  labhealth.csv     19.3 KB
  labhealthext.csv  22.2 KB

app1/     (5 files, ~91 KB total)
  pt.csv            27.9 KB
  vitalsignslf.csv  20.1 KB
  homevisit.csv     11.7 KB
  homehealth.csv    13.8 KB
  labhealth.csv     17.9 KB

app2/     (1 file, ~177 KB)
  app2.csv         180.9 KB
```

All 13 files = 100 patients each × N visits. Total < 500 KB — trivial
to ingest. The bundle currently completes in <2 s, but only **1 row**
ever lands in `private.visit_measurement` (the EAV table) when
`variable_definition` and `facility` are unbootstrapped, which is the
default state.
