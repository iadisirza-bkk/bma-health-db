# DATABASE.md — โครงสร้างฐานข้อมูล BMA Health

เอกสารนี้สรุป **โครงสร้างจริง** ของฐานข้อมูล `bma_health` (Postgres 16, Docker `bma-health-db`) เพื่อให้นักพัฒนาและผู้ใช้งาน admin panel เข้าใจตรงกัน ก่อนที่จะแก้ไขใด ๆ ต่อ

> **Single source of truth:**
> - ฟิลด์/ตัวแปร → `/Users/dev/bma-med/all_var.xlsx` (675 ตัวแปร, 3 sources)
> - Schema → `db/migrations/100-112_*.sql`
> - ETL → `etl/import_csv_v3.py`
> - Admin upload → `api/admin.py` + `api/templates/admin/*.html`
>
> **Snapshot 2026-04-28** (after migration 112): 709,662 patients · 910,800 visits · 77.9M measurements · 21.9M lab measurements · 14,063 facilities · 77 provinces · 12 perf MVs

---

## 1. ภาพรวมการไหลของข้อมูล

```
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│  Portal (7 ไฟล์) │  │   App1 (5 ไฟล์)  │  │   App2 (1 ไฟล์)  │
│   pt, pthist,    │  │   pt, vital,     │  │     app2.csv     │
│   vital, hv, hh, │  │   hv, hh,        │  │   (รวมทุกอย่าง) │
│   lab, labext    │  │   lab            │  │                  │
└────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘
         │                     │                     │
         └────────────┬────────┴─────────────────────┘
                      ▼
         ┌────────────────────────────┐
         │  /admin/upload-bundle      │  ← ผู้ใช้ลากโฟลเดอร์
         │  (FastAPI, CSRF, session)  │
         └────────────┬───────────────┘
                      ▼
         ┌────────────────────────────┐
         │   ETL v3 — import_csv_v3   │  ← thread bg, single tx
         │   - per-source DELETE      │
         │   - dedupe + validate      │
         │   - bulk INSERT            │
         └────────────┬───────────────┘
                      ▼
   ┌──────────────────────────────────────┐
   │  private.* schema (ETL-write only)   │  ← bma_etl_writer
   │   patient → visit_event → vm/lm      │
   │   variable_definition (EAV catalog)  │
   └────────────┬─────────────────────────┘
                ▼ refresh_all_mvs()
   ┌──────────────────────────────────────┐
   │  public.mv_* (k-anonymized aggregates)│  ← bma_api_reader
   │   mv_visit_resolved, mv_kpi_tier1,   │
   │   mv_disease_district, mv_lab_*, ... │
   └────────────┬─────────────────────────┘
                ▼
         ┌──────────────────┐
         │  FastAPI v2 API  │  ← /api/v2/* (72 endpoints)
         │  port 9002       │
         └──────────────────┘
                ▼
         ┌──────────────────┐
         │  Next.js frontend│
         │  (bma-health)    │
         └──────────────────┘
```

---

## 2. โมเดลข้อมูล (3 Sources)

### 2.1 Source = ระบบต้นทาง

| code | ชื่อ | ไฟล์ที่อัปโหลด | ตัวแปร (`all_var.xlsx`) |
|------|-----|---------------|-------------------------|
| `portal` | BMA Portal (เว็บ) | 7 ไฟล์ | 383 |
| `app1` | Mobile App 1 | 5 ไฟล์ | 189 |
| `app2` | Mobile App 2 (รวมแล้ว + derived) | 1 ไฟล์ | 103 |

ทั้ง 3 source เก็บเรื่องเดียวกัน (การคัดกรอง NCD ในกรุงเทพ) แต่ schema ต่างกัน → ระบบใช้ **EAV** เพื่อไม่ต้อง migrate column ทุกครั้งที่ source เพิ่มฟิลด์

### 2.2 ไฟล์ของแต่ละ source

| File | Portal | App1 | App2 | คำอธิบาย |
|------|:--:|:--:|:--:|---|
| `pt.csv` | ✓ | ✓ | — | ทะเบียนผู้ป่วย (master) |
| `pthistory.csv` | ✓ | — | — | ประวัติเพิ่มเติม (LGBTQ, ศาสนา) |
| `vitalsignslf.csv` | ✓ | ✓ | — | ความดัน, น้ำตาล, BMI, สุขภาพจิต |
| `homevisit.csv` | ✓ | ✓ | — | ที่อยู่ + อาชีพ |
| `homehealth.csv` | ✓ | ✓ | — | พฤติกรรม (สูบ, ดื่ม, ออกกำลัง) |
| `labhealth.csv` | ✓ | ✓ | — | ผลแลบ (CBC, glucose, lipid) |
| `labhealthext.csv` | ✓ | — | — | แลบขยาย (มะเร็ง, การมองเห็น) |
| `app2.csv` | — | — | ✓ | รวมทุกอย่างใน CSV เดียว + Dashboard derived columns |

→ Bundle upload รับสูงสุด **13 ไฟล์ในครั้งเดียว** (`POST /admin/upload-bundle`)

---

## 3. Physical Schema

### 3.1 `private.*` — ETL writes here

ตารางที่สำคัญ (ตามลำดับ dependency):

| Table | Role | Key |
|-------|------|-----|
| `data_source` | catalog ของ portal/app1/app2 | `source_code` |
| `geo_province` / `geo_district` / `geo_subdistrict` | reference geography | `province_code` / `dcode` |
| `facility` | ศบส., รพ., สถานพยาบาล | `code` |
| **`patient`** | **คน 1 row = 1 person** (cross-source) | `id`, `idcard_hash` (SHA-256) |
| `patient_alias` | (patient_id, source_code, source_pid) | unique per source |
| `patient_address` | SCD Type 2 → ไม่ duplicate visits | (patient_id, type, effective_to) |
| **`variable_definition`** | EAV catalog 579 active rows | (`source_code`, `csv_column_name`) unique |
| `variable_code_value` | enum labels (1=ชาย, 2=หญิง) | (variable_id, code) |
| **`visit_event`** | 1 visit = 1 row | (`patient_id`, `source_code`, `visit_date`) |
| **`visit_measurement`** (16 hash partitions) | EAV value rows | (`visit_id`, `variable_id`) |
| `lab_event` + `lab_measurement` (16 partitions) | แลบแยก stream | (`patient_id`, `source_code`, `visit_date`) |
| `import_batch` | provenance ทุก ETL run | per-file row |
| `audit_log`, `data_quality_issue`, `erasure_request` | ตามรอย, GDPR | — |

#### โครงสร้าง `visit_measurement` (EAV คอลัมน์ 5 ช่อง)

```
visit_id      bigint   ← FK to visit_event.id
variable_id   bigint   ← FK to variable_definition.id
value_number  numeric  ← เก็บถ้า data_type='number'
value_text    varchar  ← เก็บถ้า data_type='text' หรือ 'code'
value_boolean boolean  ← เก็บถ้า data_type='boolean'
value_date    date     ← เก็บถ้า data_type='date'
value_array   jsonb    ← เผื่อ multi-select
source_value  varchar  ← raw CSV value สำหรับ audit
```

**กฎ:** ETL ดู `variable_definition.data_type` แล้วเลือกเขียนเฉพาะคอลัมน์ที่ตรง type — column อื่นเหลือ `NULL`

### 3.2 `public.*` — API reads here

| Object | Type | ใช้ทำอะไร |
|--------|------|-----------|
| **`mv_visit_resolved`** | matview | base ของทุก analytics — มี risk/found booleans pivoted แล้ว |
| `mv_kpi_tier1` | matview | นับ persons/visits ต่อ district × source × bucket |
| `mv_disease_district` | matview | จำนวน at-risk ต่อ disease × district × source |
| `mv_demographics` | matview | กระจายตามเพศ × ช่วงอายุ |
| `mv_lifestyle` | matview | smoke/alcohol/exercise distribution |
| `mv_mental_health` | matview | PHQ-9 / ST-5 / 2Q score |
| `mv_lab_distribution` | matview | percentile/mean ผลแลบ |
| `mv_data_dictionary` | matview | reflection ของ variable_definition (สำหรับ admin) |
| `summary_district_*` (6 views) | view | summary per district สำหรับ /admin |
| `v_*` (6 views) | view | reference data ที่ frontend ใช้ |
| `ref_districts`, `ref_health_zones` | table | seed จาก `db/seeds/001_reference_data.sql` |
| `import_history` | table | ทุก upload run (1 row ต่อ bundle/file) |
| `mv_refresh_log` | table | track เวลา refresh แต่ละ MV |
| `pm25_daily` | table | PM2.5 จาก external (ไม่เกี่ยวกับ upload) |

> **Bottom line:** `mv_visit_resolved` คือ **single source of truth** สำหรับ API queries — ไม่ต้องไปแตะ `private.*` โดยตรง

---

## 4. การ Map ไฟล์ CSV → Schema

### 4.1 ตามไฟล์

| ไฟล์ CSV | สิ่งที่ ETL ทำ |
|---------|----------------|
| `pt.csv` | INSERT/UPDATE `patient`, `patient_alias` |
| `pthistory.csv` | UPSERT `patient_attribute` (เฉพาะ Portal) |
| `vitalsignslf.csv` | INSERT `visit_event` + `visit_measurement` (แต่ละ column = 1 row EAV) |
| `homevisit.csv` | UPSERT `patient_address` (SCD2) + `visit_measurement` |
| `homehealth.csv` | INSERT `visit_measurement` (lifestyle vars) |
| `labhealth.csv` | INSERT `lab_event` + `lab_measurement` |
| `labhealthext.csv` | เหมือน labhealth แต่ Portal-only fields |
| `app2.csv` | INSERT ทุกอย่างเลย (รวม patient+visit+address+measurements) |

### 4.2 ตามคอลัมน์

ETL ดู `variable_definition` ที่ `(source_code, csv_column_name)`:
1. ถ้าเจอ → เก็บค่าตาม `data_type`
2. ถ้าไม่เจอ → `data_quality_issue` (level=info, kind='unmapped_column')
3. ถ้าเจอแต่ค่าผิด → `data_quality_issue` (level=warn, ไม่ crash)

### 4.3 Bootstrap variable_definition

Source ของ `variable_definition` มาจาก **`โครงสร้าง_BMA_PORTAL.xlsx`** ผ่าน script:
```bash
python etl/bootstrap_variable_definitions.py
```
อ่าน xlsx → infer `data_type` ด้วย regex (`infer_data_type()`) → INSERT/UPSERT 579 rows

---

## 5. Pipeline ครบวงจร (เน้น Upload Path)

```
1. ผู้ใช้ login    → POST /admin/login (CSRF + password + session cookie)
2. หน้า bundle     → GET  /admin/upload-bundle (แสดง drag-drop UI)
3. อัปโหลด        → POST /admin/upload-bundle (multipart, ≤13 ไฟล์, ≤500 MB ต่อไฟล์)
   ├─ ตรวจ source จาก path  (portal/app1/app2)
   ├─ ตรวจ file_type จากชื่อ (pt, vital, lab, ...)
   ├─ stream → temp file (ไม่ buffer ทั้งก้อนใน RAM)
   └─ INSERT import_history row, ปล่อย thread
4. _run_bundle_import (background)
   ├─ ACQUIRE pg_advisory_lock (ป้องกัน concurrent imports)
   ├─ DELETE per-source ของที่ระบุใน bundle (sources อื่นยังอยู่)
   ├─ FOR EACH source IN [portal, app1, app2]:
   │   ├─ pt.csv ก่อน (สร้าง patient + alias)
   │   ├─ vital, hv, hh, pthist, lab, labext (ตามลำดับ)
   │   └─ each file → 1 import_batch row (provenance)
   ├─ COMMIT
   ├─ refresh_all_mvs()  → public.mv_* ทุกตัว
   └─ flush cache (Redis + in-memory)
5. ผู้ใช้เห็นหน้า /admin/history → status=success
```

### กฎการทับข้อมูล (per-source delete, **ไม่ใช่ TRUNCATE**)
- bundle = portal+app1 → ลบ portal+app1 ของเดิม, **app2 ยังคงอยู่**
- bundle = app2 → ลบ app2, portal/app1 ยังคงอยู่
- bundle ครบ 3 source → ลบทุก source

> ⚠️ UI text ปัจจุบัน (`upload_bundle.html` บรรทัด 47) เขียนว่า *"TRUNCATE raw_patients CASCADE"* — **ผิด** กับ behavior จริง ควรแก้

---

## 6. variable_definition คือหัวใจ — รายละเอียด

```sql
CREATE TABLE private.variable_definition (
  id              bigint PRIMARY KEY,
  variable_key    varchar(80)  -- canonical key (snake_case): risk_dm, bmi, ...
  csv_column_name varchar(80)  -- raw CSV column: RISKDM, BMI, ...
  source_code     varchar(20)  -- portal | app1 | app2
  csv_file        varchar(40)  -- pt | vitalsignslf | ...
  domain          varchar(40)  -- patient | visit | lab | ...
  sub_domain      varchar(80)  -- ตามที่อยู่ใน all_var.xlsx
  data_type       varchar(20)  -- number | boolean | date | code | text
  unit            varchar(30)  -- mg/dL, cm, kg, ...
  description_th  text
  possible_values text         -- '0=ไม่มี, 1=มี'
  tier            smallint     -- 1=hero, 2=std, 3=drilldown, 4=audit
  valid_min       numeric
  valid_max       numeric
  is_pii          boolean
  ...
  UNIQUE (source_code, csv_column_name)
);
```

**ตัวอย่าง**: `RISKDM` มี 3 row (1 ต่อ source):

| id | variable_key | csv_column_name | source | data_type | unit |
|----|--------------|-----------------|--------|-----------|------|
| 98 | risk_dm | RISKDM | portal | code* | — |
| 469 | risk_dm | RISKDM | app1 | code* | — |
| 498 | risk_dm | RISKDM | app2 | code* | — |

> *🔴 ตามแบบจริง `data_type` ของ disease flag ควรเป็น `boolean` แต่ infer_data_type() ใส่เป็น `code` → MV ที่กรอง `value_boolean=true` เลยว่าง (ปัญหาที่กำลังแก้)*

---

## 7. รายการตารางทั้งหมด (~60 objects) แยกตามหน้าที่

> Snapshot 2026-04-28 — DB ~12 GB · 579 active variables · 709,662 patients · 910,800 visits · 77.9M measurements · 21.9M lab measurements · 14,063 facilities · 77 provinces

### 7.1 🔵 `private.*` — ETL writes here (ปิดต่อ API)

#### A. Reference geography & catalog (8 ตาราง — เล็กและคงที่)
| Table | Rows | Size | เก็บอะไร |
|---|---:|---|---|
| `data_source` | 3 | 32 kB | catalog ของ portal / app1 / app2 |
| `geo_province` | **77** | 32 kB | ✅ 77 จังหวัดครบ (≥ migration 111) |
| `geo_district` | 50 | 56 kB | 50 เขต BKK (มี zone_code) |
| `geo_subdistrict` | 0 | 8 kB | ⚠ ว่าง — ยังไม่ได้ seed |
| `geo_health_zone` | 8 | 32 kB | 8 เขตสุขภาพ |
| `facility` | **14,063** | — | ✅ seeded by `etl/import_facilities.py` (≥ Agent B fix); `district_code`/`zone_code` ยัง NULL — ต้อง geocoding pass |
| `variable_definition` | **579** | 336 kB | **EAV catalog** — 1 row ต่อ (source × csv_column) — typed (197 boolean / 124 number / 11 date / 69 code / 178 text) ตาม ≥ migration 108 |
| `variable_code_value` | 615 | 120 kB | enum labels (เช่น "1=ชาย, 2=หญิง") |

#### B. Patient identity (3 ตาราง — ส่วน long-tail ถูก drop ที่ migration 109)
| Table | Rows | เก็บอะไร |
|---|---:|---|
| `patient` | **709,662** | คน 1 row = 1 person (cross-source dedup ด้วย `idcard_hash` SHA-256); `sex_code` populated 34,433/709,662 (App2 EAV only ตาม Phase 1; Portal/App1 ETL fix in place แต่ต้อง re-import) |
| `patient_alias` | 837,031 | (patient_id, source, source_pid) — link คนข้าม source |
| `patient_address` | 1,044,986 | SCD Type 2 — ที่อยู่ home/work เปลี่ยนได้ |

> 🗑 **Dropped at migration 109:** `patient_attribute`, `patient_chronic_history`, `patient_family_history`, `patient_allergy` (dead schema, 0 callers)

#### C. Visits & Measurements (EAV หลักของระบบ)
| Table | Rows | Size | เก็บอะไร |
|---|---:|---|---|
| `visit_event` | **910,800** | 501 MB | 1 visit = 1 row, key = (patient_id, source, visit_date) |
| `visit_measurement` (16 hash partitions) | **77,923,676** | ~10 GB | EAV — 1 measurement = 1 row, key = (visit_id, variable_id) |
| `lab_event` | 1,283,729 | 212 MB | แลบ event แยก stream (Portal labhealthext) |
| `lab_measurement` (16 hash partitions) | 21,993,110 | — | EAV ของผลแลบ |

> 🗑 **Dropped at migration 109:** `visit_pain`, `visit_neurological`, `visit_respiratory`, `visit_recommendation`, `visit_referral` (5 ตาราง dead schema — ETL v3 ใช้ EAV `visit_measurement` ทั้งหมด)

#### E. Provenance & Audit (4 ตาราง)
| Table | Rows | เก็บอะไร |
|---|---:|---|
| `import_batch` | **13** | 1 row ต่อไฟล์ที่ import → traceback row → batch |
| `data_quality_issue` | 0 | ETL ใส่เมื่อพบ unmapped column / out-of-range |
| `audit_log` | 0 | sensitive ops |
| `erasure_request` | 0 | PDPA right-to-be-forgotten |

---

### 7.2 🟢 `public.*` — API reads here

#### F. Materialized Views (18 ตัว — single source of truth สำหรับ API)

**Base + dictionary:**
| MV | Rows | Size | หมายเหตุ |
|---|---:|---|---|
| `mv_visit_resolved` | 910,800 | 165 MB | ✅ base — visit-level + 11 risk/found booleans pivoted; refresh first per migration 101 fix |
| `mv_data_dictionary` | 378 | 192 kB | ✅ reflection ของ variable_definition |

**Aggregated for `/summary/*` endpoints (≥ migration 106, 110):**
| MV | Rows | Size | Endpoint | Speedup |
|---|---:|---|---|---:|
| `mv_summary_global` | 1 | 24 kB | `/summary/overview` | 38s → 4ms (12,700×) |
| `mv_summary_districts` | 50 | 72 kB | `/summary/districts` | 75s → 7ms (15,000×) |
| `mv_summary_zones` | 8 | 32 kB | `/summary/zones` | 33s → 4ms (8,250×) |
| `mv_summary_lab` | 50 | 176 kB | `/summary/lab` | 7.5s → 5ms (1,500×) |
| `mv_summary_mental` | 50 | 176 kB | `/summary/mental-health` | 4.7s → 4ms (1,175×) |

**Promoted from stub (≥ migration 112):**
| MV | Rows | Size | Endpoints |
|---|---:|---|---|
| `summary_disease_age_sex` | 11,647 | 2.1 MB | age-group-prevalence, age-pyramid, statistical-test |
| `summary_lab_disease_cross` | 8,242 | 1.1 MB | disease-lab-crosstab |
| `summary_comorbidity` | 1,058 | 296 kB | multi-disease-matrix |
| `summary_disease_control` | 1,058 | 168 kB | kpi/control-rates |
| `summary_bmi_waist` | 232 | 88 kB | bmi-distribution, waist-risk-analysis, correlation-matrix, district-health-card |

**Other analytics:**
| MV | Rows | Size | หมายเหตุ |
|---|---:|---|---|
| `mv_lab_distribution` | per district × test | 13 MB | lab percentile/distribution |
| `mv_lifestyle` | per district | 1 MB | smoke / alcohol / exercise |
| `mv_kpi_tier1` | per district × source × bucket | 392 kB | persons / visits |
| `mv_mental_health` | per district | 816 kB | PHQ-9 / ST-5 raw rolled-up |
| `mv_demographics` | per district | 1 MB | sex × age_group |
| `mv_disease_district` | per disease × district × source | 184 kB | disease at-risk per district |

#### G. SQL Views (12 ตัว — readable wrappers)
| Group | Views | หมายเหตุ |
|---|---|---|
| `summary_district_*` (6) | demographics · disease · lab · mental · risk_factors · facility | อ่านจาก `private.*` ตรง |
| `v_*` (2) | source_row_counts · cross_system_duplicates | 4 ตัวที่ดี dropped at migration 109 (no callers) |

#### H. Reference & utility tables (10 ตาราง)
| Table | Rows | เก็บอะไร |
|---|---:|---|
| `ref_districts` | 50 | 50 เขต BKK (frontend อ่านจากนี่) |
| `ref_health_zones` | 8 | 8 เขตสุขภาพ |
| `ref_facility_code_map` | 11 | mapping legacy facility codes |
| `ref_facilities` | 0 | ⚠ ว่าง |
| `import_history` | 2 | log ทุก bundle upload |
| `mv_refresh_log` | 0 | track timing ของ refresh |
| `data_retention_policy` | 8 | PDPA retention rules |
| `pm25_daily` | 0 | external PM2.5 (ไม่เกี่ยวกับ upload) |
| `erasure_requests` | 0 | mirror ของ private |
| `raw_*` (7 ตาราง: patients · visits · vitalsigns · lab_results · lab_extended · homevisit · homehealth) | 0 | 🟡 schema เก่า (v1) — v3 ETL ไม่ใช้ ควรพิจารณา drop |

---

## 8. รายละเอียด `variable_definition` 579 ตัว

### 8.1 แตกตาม source × type
| source | code | text | date | **รวม** |
|---|---:|---:|---:|---:|
| Portal | 166 | 147 | 1 | **314** |
| App1 | 71 | 90 | 1 | **162** |
| App2 | 11 | 92 | 0 | **103** |
| **รวม** | **248** | **329** | **2** | **579** |

> 🔴 ไม่มี `data_type='boolean'` หรือ `'number'` แม้แต่ตัวเดียวจาก 579 ตัว — bootstrap regex จัด BMI, RISKDM, SBP เป็น `'text'` หรือ `'code'` ทั้งหมด

### 8.2 แตกตาม domain
| domain | vars |
|---|---:|
| vital | 125 |
| lab | 120 |
| derived (App2 dashboard) | 103 |
| address | 95 |
| lifestyle | 67 |
| identity | 47 |
| symptom | 22 |

### 8.3 Top sub-domains (15 อันดับแรก)
| sub_domain | vars |
|---|---:|
| ตัวแปรคำนวณ/แสดงผล (Derived) | 45 |
| คัดกรองสุขภาพจิต/ความเครียด | 38 |
| ความพิการ/พึ่งพิง | 33 |
| ที่อยู่/พิกัด | 32 |
| ประวัติครอบครัว | 27 |
| แลบ - CBC | 27 |
| โรคประจำตัว | 22 |
| พฤติกรรมสุขภาพ | 21 |
| ระบบ/Audit | 20 |
| แลบ - น้ำตาลในเลือด | 17 |
| การทำงาน/อาชีพ | 17 |
| ข้อมูลส่วนบุคคล | 16 |
| การมองเห็น | 14 |
| แลบ - ตับ / แลบ - ไขมัน | 13 / 13 |

---

## 9. การเก็บค่าใน `visit_measurement` 77.9 ล้านแถว

หลัง Phase 1 fix (migration 107-108) — ETL routes ค่าเข้า column ที่ถูกตาม `variable_definition.data_type`:

| declared | vars (with data) | `value_number` | `value_boolean` | `value_date` | `value_text` (incl. parse-fail fallback) |
|---|---:|---:|---:|---:|---:|
| `boolean` | 141 | 0 | **34,300,820** | 0 | 6,774,444 |
| `number` | 56 | **9,769,785** | 0 | 0 | 57,585 |
| `date` | 4 | 0 | 0 | **1,378,431** | 0 |
| `code` | 62 | 0 | 0 | 0 | 17,701,935 |
| `text` | 104 | 0 | 0 | 0 | 7,940,676 |
| **รวม** | **367** | 9.77M | 34.3M | 1.38M | 32.5M |

**Storage rule** (ETL v3 ใน `import_csv_v3.py`):
- ดู `data_type` → เลือกเขียนเฉพาะ column ที่ตรง type
- ถ้า parse ไม่ได้ → fallback เก็บเป็น `value_text` + log audit (≈ 6.8M boolean parse-fails, 57K number parse-fails)
- `source_value` คอลัมน์เก็บค่า raw ของ CSV เผื่อ trace กลับ

→ **77.9M total measurements** กระจายตาม type ถูกต้อง:
- 44.0% boolean (34.3M `value_boolean`)
- 12.5% number (9.77M `value_number`) 
- 1.8% date (1.38M `value_date`)
- 41.7% text/code/fallback (32.5M `value_text`)

**Storage rule** (ETL v3 ใน `import_csv_v3.py`):
- ดู `data_type` → เลือกเขียนเฉพาะ column ที่ตรง type
- ถ้า parse ไม่ได้ → fallback เก็บเป็น `value_text` + log audit
- `source_value` คอลัมน์เก็บค่า raw ของ CSV เผื่อ trace กลับ

**Result MVs (สาเหตุที่ disease analytics ทำงาน):**
- `mv_visit_resolved.found_obesity` มี TRUE 5,344 visits (was 0)
- `mv_disease_district` มีข้อมูลครบ 6 disease keys
- `mv_summary_lab` รวบ avg ของ 13 lab values
- `summary_bmi_waist` มี 232 rows (50 districts × 4-5 sex categories)

> ⚠ **Boolean fallback 6.8M rows in value_text** — เกิดจาก source data ที่ ETL parse ไม่ได้เป็น TRUE/FALSE (เช่น empty string, "9", non-Thai value). ไม่ได้กระทบ analytics ปัจจุบัน แต่ถ้าจะเพิ่ม `value_boolean=true` filter ที่เคย miss ตรงนี้ ลองตรวจ `_parse_bool` ใน ETL ว่าครอบ encoding ครบไหม

---

## 10. ตารางว่าง / partial — สถานะ

| ตาราง | สถานะ | หมายเหตุ |
|---|---|---|
| `private.facility.district_code` / `zone_code` | ✅ 14,025/14,063 (99.7%) | geocoded via `etl/geocode_facilities.py` (point-in-polygon) — auto-runs ใน `_ensure_facilities_seeded` ครั้งหน้า; 38 ที่เหลืออยู่นอก BKK 50 (BMR provinces — expected) |
| `private.geo_subdistrict` | 🟡 ว่าง | seed file ไม่ครอบคลุม subdistrict — ถ้าต้องใช้ subdistrict-level analytics ต้อง seed เพิ่ม |
| `private.patient.sex_code` (Portal/App1) | 🟡 NULL 675K | ETL fix in place (≥ 107). Recovery deferred — จะ auto-fix เมื่อ BMA ส่งข้อมูล batch update รอบถัดไป → re-upload portal+app1 ผ่าน /admin/upload-bundle จะ populate sex_code ครบ (App2 34K patients populated แล้ว) |
| `data_quality_issue`, `audit_log`, `erasure_request` | ⚪ ปกติ | ว่างจนกว่าจะมีเหตุการณ์ |

---

## 11. Known Issues — Resolution Log

### ✅ P1 (was Critical) — Type inference fixed at migrations 107-108
- ก่อน: ทุก measurement เป็น `value_text` ไม่มี `value_number`/`value_boolean`/`value_date`
- หลัง migration 107-108: 197 boolean / 124 number / 11 date — disease MV + lab MV มีข้อมูลจริง

### ✅ P2 (was Major) — Facility seeded at Agent B + import_facilities fix
- ก่อน: `private.facility` มี 0 rows
- หลัง: 14,063 rows ทั้งใน `private.facility` และ `ref_facilities`; bundle import auto-seeds เมื่อ table empty
- คงเหลือ: `district_code`/`zone_code` ยัง NULL ต้อง geo-coding pass

### ✅ P3 (was Minor) — Admin UI text fixed at Agent B
- TRUNCATE warning rewritten → per-source DELETE description ถูกต้อง

### 🟡 P4 — District mapping fail (ongoing)
- ~1% ของ visits → `bucket='unknown'` (จับเขตไม่ได้)
- ส่วนใหญ่เป็น app2 records ที่ DISTRICT มีค่าว่าง หรือ '9999'
- คงเหลือ — ต้องตรวจ data quality ที่ source ETL

### ✅ Performance — All key endpoints < 100ms
| Endpoint | Before | After | Migration |
|---|---:|---:|---|
| `/summary/overview` | 38s | 4ms | 106 |
| `/summary/districts` | 75s | 7ms | 106 |
| `/summary/zones` | 33s | 4ms | 106 |
| `/summary/lab` | 7.5s | 5ms | 110 |
| `/summary/mental-health` | 4.7s | 4ms | 110 |
| `/admin/dashboard` | 20s | 2.6s cold / 3ms warm | (admin.py edit) |
| Bundle DELETE portal | 150s+ stuck | ~45s | 110 (FK index) |

### ✅ MV refresh order bug (was hidden) — fixed at migration 101 (CREATE OR REPLACE)
- `refresh_all_mvs()` ใช้ `ORDER BY 1` หลัง UNION → sort ทั้ง result → `mv_visit_resolved` ตกท้ายสุด
- หลังแก้: `mv_visit_resolved` refresh ก่อน, ที่เหลือ refresh จาก fresh data

### ✅ FK index bug — fixed at migration 110
- `patient_address.reported_by_visit_id` มี FK ON DELETE SET NULL แต่ไม่มี index
- หลังแก้: bundle DELETE 18+ นาที → 15s

---

## 12. คำสั่งที่ใช้บ่อย

```bash
# Service
make start              # API + tunnel up
make dev                # API hot-reload
make infra              # Postgres up

# DB
make migrate            # apply 001-112 + seeds
make refresh-views      # refresh ทุก mv
make db-stats           # row counts
docker exec bma-health-db psql -U postgres -d bma_health   # shell เข้า DB

# ETL จากบรรทัดคำสั่ง
make etl                # import จาก minimal_data/portal_top
python etl/bootstrap_variable_definitions.py   # bootstrap variable_definition

# Admin upload (browser)
http://localhost:9002/admin/login → upload-bundle
```

---

## 13. ลิงก์อ้างอิง

| เอกสาร | เนื้อหา |
|--------|---------|
| `/Users/dev/bma-med/all_var.xlsx` | **ตัวแปรทั้ง 675 ตัว** ของ Portal+App1+App2 (canonical) |
| `/Users/dev/bma-med/Indicator_Mapping_Portal_App1_App2.md` | Mapping ตัวชี้วัดข้าม source |
| `bma-health-db/ACTION-PLAN.md` | แผนปฏิบัติการ 6 phases |
| `bma-health-db/API-AUDIT.md` | 170 endpoints — สถานะรายตัว |
| `bma-health-db/ETL-TYPE-FIX-DESIGN.md` | Phase 1 fix design (1,730 บรรทัด) |
| `bma-health-db/CLEANUP-PROPOSAL.md` | Schema cleanup proposal |
| `bma-health-db/ADMIN-UX-GAPS.md` | Admin UX audit (24 issues) |
| `bma-health/FRONTEND-API-MAP.md` | hook → endpoint → page map |
| `bma-health-db/CLAUDE.md` | คู่มือ developer |
| `bma-health-db/SYSTEM-SPEC.md` | รายละเอียดระบบ |
| `bma-health-db/DATA-DICTIONARY.md` | คำอธิบาย field-level |
| `bma-health-db/PREPROCESSING.md` | กฎ ETL ก่อน insert |
| `bma-health-db/SECURITY.md` | PDPA, role-based access, audit |
| `bma-health/CLAUDE.md` | คู่มือ frontend |
| `bma-health/fact/api-endpoints.md` | API endpoints ของ v2 |

---

## 14. Migration history (essential)

| # | ไฟล์ | สรุป |
|---|---|---|
| 100 | `100_schema_v3_private.sql` | Schema v3: private.* EAV + 16 partitions |
| 101 | `101_schema_v3_public_mvs.sql` | 8 base MVs + `refresh_all_mvs()` (ลำดับ refresh fixed in this session) |
| 102 | `102_schema_v3_roles.sql` | 3 roles: bma_etl_writer / bma_dba_admin / bma_api_reader |
| 103 | `103_seed_geography.sql` | Initial 13 provinces + 50 districts + 8 zones |
| 104 | `104_mv_visit_resolved_with_risk.sql` | Pivot risk/found booleans into MV |
| 105 | `105_drop_legacy_mvs.sql` | Drop 5 legacy summary_* MVs (replaced later by stubs at Phase 3) |
| **106** | `106_perf_summary_mvs.sql` | 3 perf MVs: summary_districts/zones/global (75s/38s/33s → ms) |
| **107** | `107_fix_sex_code_backfill.sql` | UPDATE patient.sex_code from EAV; recreate summary_bmi_waist with sex from patient table |
| **108** | `108_rename_body_fat_pct_to_found_obesity.sql` | Variable rename → mv_visit_resolved.found_obesity populated |
| **109** | `109_phase1_cleanup_drop_dead_schema.sql` | DROP 9 dead tables + 4 alias views |
| **110** | `110_perf_summary_lab_mental_and_delete_index.sql` | mv_summary_lab/mental + idx on patient_address.reported_by_visit_id |
| **111** | `111_seed_77_provinces.sql` | Seed all 77 Thai provinces |
| **112** | `112_promote_4_stub_views.sql` | Promote 4 placeholder views → real MVs (epi/comorbidity/control) |
