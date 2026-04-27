# PREPROCESSING.md — BMA Health Data Preprocessing Pipeline

> โครงการคัดกรองสุขภาพกรุงเทพมหานคร | สำนักการแพทย์ กรุงเทพมหานคร
> Last Updated: 2026-04-22

เอกสารนี้อธิบาย **preprocessing pipeline ทั้งหมด** ตั้งแต่ไฟล์ CSV ต้นทาง 13 ไฟล์
จาก 3 ระบบ ไปจนถึงตาราง PostgreSQL ที่ใช้เสิร์ฟ API และรายงาน

---

## สารบัญ

1. [ภาพรวมและหลักการออกแบบ](#1-ภาพรวมและหลักการออกแบบ)
2. [แหล่งข้อมูล 3 ระบบ](#2-แหล่งข้อมูล-3-ระบบ)
3. [Pipeline 6 ขั้น](#3-pipeline-6-ขั้น)
4. [Data Cleansing Rules](#4-data-cleansing-rules)
5. [Stack Mode — ไม่ Merge ไม่ Dedupe](#5-stack-mode--ไม่-merge-ไม่-dedupe)
6. [App2 Normalizer — Thai Labels → Code](#6-app2-normalizer--thai-labels--code)
7. [Derived Fields 3 ชั้น](#7-derived-fields-3-ชั้น)
8. [การอัปโหลดผ่านหน้าเว็บ](#8-การอัปโหลดผ่านหน้าเว็บ)
9. [การใช้งาน Query / Report](#9-การใช้งาน-query--report)
10. [Troubleshooting & Data Quality](#10-troubleshooting--data-quality)

---

## 1. ภาพรวมและหลักการออกแบบ

### สิ่งที่ pipeline นี้ทำ

| Input | Output |
|-------|--------|
| CSV 13 ไฟล์ จาก 3 ระบบ (~1M records รวมกัน) | PostgreSQL 7 raw tables + 13 materialized views |
| ชื่อ + เลขบัตร ปชช. + ค่า raw | idcard_hash (HMAC-SHA256) + normalised values + derived fields |
| Out-of-range / missing / Thai labels ปนกัน | Typed columns + NULL for invalid + code + label |

### หลักการออกแบบ 5 ข้อ

1. **NO imputation** — ค่าผิด/นอก range / missing → `NULL` เสมอ **ไม่เดา ไม่เติม**
   ข้อมูลแพทย์ imputation ทำให้ผิด epidemiology
2. **Stack mode** — คนเดียวอยู่ใน 2 ระบบ = 2 แถว (ไม่ merge) เพื่อรักษา "จำนวนการตรวจ"
   และรายงานความซ้ำซ้อนข้ามระบบ
3. **3-tier fields** — raw / source-derived (`*_src`) / ETL-derived แยกกันชัดเจน
   ทำให้ audit ได้และเปรียบเทียบระหว่าง source ได้
4. **Provenance** — ทุก record มี `data_source` (portal/app1/app2) และ `import_batch_id`
   ตรวจสอบย้อนกลับได้เสมอ
5. **Fail-soft** — ฟังก์ชัน `execute_values` มี SAVEPOINT แถว-ต่อ-แถว ถ้า row ไหนพัง
   จะ skip แล้ว log ไม่ล้ม transaction ทั้งก้อน

---

## 2. แหล่งข้อมูล 3 ระบบ

### โครงสร้างโฟลเดอร์ที่คาดหวัง

```
<data-dir>/
├── portal/           # ระบบหลัก สนพ. (portal_top)
│   ├── pt.csv                (12 cols — ข้อมูลผู้ป่วย)
│   ├── pthistory.csv         (20 cols — ประวัติตรวจ: ศาสนา, LGBTQ, ติดต่อ)
│   ├── vitalsignslf.csv      (92 cols — สัญญาณชีพ + คัดกรองโรค)
│   ├── homevisit.csv         (88 cols — สังคมเศรษฐกิจ)
│   ├── homehealth.csv        (63 cols — พฤติกรรม + วัคซีน)
│   ├── labhealth.csv         (75 cols — CBC, FBS, ไขมัน, ตับ, ไต)
│   └── labhealthext.csv      (33 cols — ระบบหายใจ + MSD + ต้อเนื้อ)
├── app1/             # แอปมือถือ สำนักอนามัย (subset ของ portal)
│   ├── pt.csv                (17 cols — เพิ่ม AGE, HPTCODE, LOCATION)
│   ├── vitalsignslf.csv      (56 cols — subset)
│   ├── homevisit.csv         (24 cols — subset)
│   ├── homehealth.csv        (26 cols — subset)
│   └── labhealth.csv         (66 cols — มี EGFR_LAB เพิ่ม)
└── app2/             # แดชบอร์ดสรุป (pre-aggregated, Thai labels)
    └── app2.csv              (103 cols — 1 แถวครอบคลุมทุกตาราง)
```

### ความแตกต่างสำคัญ

| | Portal | App1 | App2 |
|---|--------|------|------|
| **จำนวนไฟล์** | 7 | 5 | 1 |
| **Column รวม** | 383 | 189 | 103 |
| **ID field** | `IDCARD` | `PID` | `PID` |
| **Birth date** | `BIRTHDATE` | `BRTHDATE` | (ไม่มี — มีแค่ AGE_GROUP) |
| **Sex encoding** | `MALE` = 10/20 | `MALE` = 10/20 | `MALE` = "ชาย"/"หญิง" |
| **BMI** | ไม่คำนวณให้ — มี HEIGHT + WEIGHT | เหมือน Portal | pre-computed ในช่อง `BMI` |
| **Lab results** | ตัวเลข raw (HMGB, CHOLEST, FBS) | เหมือน Portal | ตัวเลขแค่ 3 ช่อง + text interpretation 11 ช่อง |
| **ค่าคัดกรอง** | 0/1 integer | 0/1 integer | "ปกติ"/"เสี่ยง"/"ผิดปกติ" text |

---

## 3. Pipeline 6 ขั้น

```
┌───────────┐   ┌───────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌──────┐
│ 1. Upload │→ │ 2. Parse  │→ │ 3. Hash │→ │ 4. Clean│→ │ 5. Stack│→ │6.View│
│  Stream   │   │  pandas   │   │  +HMAC  │   │  out-of │   │ Insert  │   │Refrsh│
│  tempfile │   │  dtype=str│   │  SHA256 │   │  range  │   │  +data_src│ │      │
└───────────┘   └───────────┘   └─────────┘   └─────────┘   └─────────┘   └──────┘
```

### 3.1 Upload (streaming-to-tempfile)

- หน้าเว็บ `/admin/upload-bundle` รับได้สูงสุด **1 GB ต่อไฟล์** (รวม ~5 GB ต่อ bundle)
- Browser ส่ง multipart/form-data พร้อม `relpaths` array (path ใน folder picker)
- Server **ไม่โหลดเข้า RAM** — ใช้ `shutil.copyfileobj` stream 64 KB chunks ลง tempfile
  → peak memory สำหรับ upload phase < 10 MB แม้ไฟล์ 500 MB

### 3.2 Parse

- อ่านด้วย `pandas.read_csv(path, dtype=str, low_memory=False)` — **ทุกคอลัมน์เป็น str**
- `dtype=str` ป้องกัน pandas inference ที่ทำให้เลข 0 กลายเป็น `0.0` หรือ NaN → สูญเสียข้อมูล
- Encoding fallback: `utf-8` → `tis-620` → `cp874` → `latin-1` → `errors='replace'`
- 1 ไฟล์ ~ 1 DataFrame ขนาด ~100-500 MB (ประมาณการ) — ประมวลผลทีละไฟล์
  ไม่ถือทั้ง 13 พร้อมกัน

### 3.3 Hash (HMAC-SHA256)

```python
# etl/import_csv.py:hash_id()
IDCARD_base64 → base64.b64decode() → HMAC-SHA256(SECRET, bytes) → hex digest (64 chars)
```

- Secret ต้องอยู่ใน env var `IDCARD_HASH_SECRET` (ยาว ≥ 16 chars)
- Portal `IDCARD` / App1 `PID` / App2 `PID` **hash ด้วย secret ตัวเดียวกัน**
  → คนเดียวกันข้าม source ได้ hash เดียวกัน → match ได้ในรายงาน cross-system

### 3.4 Clean (NULL on out-of-range)

ดูหัวข้อ [4. Data Cleansing Rules](#4-data-cleansing-rules)

### 3.5 Stack Insert (no ON CONFLICT, no dedupe)

ดูหัวข้อ [5. Stack Mode](#5-stack-mode--ไม่-merge-ไม่-dedupe)

### 3.6 View Refresh

- `REFRESH MATERIALIZED VIEW CONCURRENTLY <view>` ทั้ง 13 views
- Non-fatal — ถ้า refresh ล้ม ข้อมูล raw ยัง commit แล้ว (ยังใช้ได้ผ่าน raw queries)

---

## 4. Data Cleansing Rules

### 4.1 Rule 1: ลบทิ้ง (Filter Out) — เฉพาะ row ที่ไม่มีประโยชน์

| เงื่อนไข | เหตุผล |
|---------|--------|
| `IDCARD`/`PID` = NULL, empty, หรือ decode ไม่ได้ | ระบุตัวบุคคลไม่ได้ |

**เราไม่ลบ row ที่ `CANCELST=1`** อีกต่อไป (plan เดิมใน DATA-DICTIONARY) —
เก็บไว้แต่ใช้ `cancel_status` filter ตอน query เพราะยังต้องรายงาน "รายการที่ถูกยกเลิก"

### 4.2 Rule 2: แทนค่าด้วย NULL (Out-of-range → NULL)

**หลักการ**: ค่าที่ "เป็นไปไม่ได้ทาง clinical" → NULL — ไม่ใช่ 0, ไม่ใช่ค่าเฉลี่ย

| Field | เงื่อนไข → NULL | เหตุผลทางการแพทย์ |
|-------|----------------|-----------------|
| `BIRTHDATE` ปี < 1900 หรือ > 2030 | อายุเกิน 126 ปี ไม่มีจริง | ใช้ CURRENT_YEAR reference |
| `age` < 0 หรือ > 150 | เป็นไปไม่ได้ | — |
| `HEIGHT` < 50 หรือ > 250 cm | ทารกแรกเกิด ~50cm, คนสูงสุด 272 cm | ดู ETL `safe_float(v, 50, 250)` |
| `WEIGHT` < 10 หรือ > 300 kg | ทารก ~3 kg, คนหนักสุดในไทย ~300 kg | — |
| `WSTL` < 30 cm | ทารก ~30 cm, พบจริงมี 0 (ไม่ได้วัด) | — |
| `HBPN` (SBP) < 40 หรือ > 300 | SBP < 40 = cardiac arrest | พบจริงมี 0, 1 (ไม่ได้วัด) |
| `LBPN` (DBP) < 20 หรือ > 200 | DBP < 20 = shock รุนแรง | — |
| `PREFPG`/`POSTFPG`/`FBS` < 0 หรือ > 999 | เครื่องวัดสูงสุด | — |
| `HMGB` < 0 หรือ > 30 g/dL | Hb สูงสุดจริง ~20 (polycythemia vera) | — |
| `HMTC` < 0 หรือ > 80 % | Hematocrit ปกติ 36-54 | — |
| `MCV` < 0 หรือ > 200 fL | MCV ปกติ 80-100 | — |
| `CHOLEST`/`TRIGLY` < 0 หรือ > 999 mg/dL | — | — |
| `HDL`/`LDL` < 0 หรือ > 500 mg/dL | — | — |
| `URICACID` < 0 หรือ > 50 | — | — |
| `CRTININE` < 0 หรือ > 50 | — | — |
| `EGFR`/`EGFRRS`/`EGFR_LAB` < 0 หรือ > 200 | — | — |
| `BUNRS` < 0 หรือ > 200 | — | — |
| `BMI` (computed) > 80 | คนหนักสุดในโลก BMI ~70 | — |
| integer > 2,147,483,647 | เกิน PostgreSQL INT4 | — |
| float = inf หรือ NaN | data เสีย | — |

**ค่าที่พบบ่อยจริงในข้อมูล 100-record sample**:
- `WSTL` = 0.0, 29.0 (< 30 cm = ไม่ได้วัดแต่กรอก 0 — ต้อง NULL)
- `HBPN` = 0.0, 1.0 (ไม่ได้วัด)
- `LBPN` = 0.0, 1.0 (ไม่ได้วัด)

### 4.3 Rule 3: แปลงค่า (Transform)

| เงื่อนไข | การแปลง |
|---------|---------|
| `BIRTHDATE` ปี > 2400 | ลบ 543 (พ.ศ. → ค.ศ.) |
| App2 `MALE` = "ชาย" | → 10 |
| App2 `MALE` = "หญิง" | → 20 |
| App2 `DM_NAME` = "ปกติ" / "เสี่ยง" / "เป็น" | → 0 / 1 / 1 |
| App2 `RISKDM_NAME` = "ปกติ" / "เสี่ยง" | → 0 / 1 |
| App2 `H_DM_NAME` = "เป็น" / "ไม่เป็น" / "ไม่แน่ใจ" | → True / False / NULL |
| App2 `DISTRICT` = 9999 | → NULL (placeholder "ไม่ระบุ") |
| App2 `WRKDISTRICT` = 9999 | → NULL |
| `DISTRICTBKK` ว่าง (Portal) | Backfill 4 ขั้น (ดู §4.4) |

### 4.4 District Backfill (4 Priority Levels)

```
Priority 1: DISTRICTBKK จาก vitalsignslf.csv (ถ้ามี)
   ↓ ยังว่าง?
Priority 2: DISTRICT จาก app2.csv (ถ้ามี, ≠ 9999)
   ↓ ยังว่าง?
Priority 3: facility_code → ref_facility_districts mapping
   ↓ ยังว่าง?
Priority 4: home_district จาก homevisit (ต้องอยู่ 1001-1050)
```

`etl.backfill_district_codes(cur)` ทำ Priority 3+4 ตอน view refresh

### 4.5 Rule 4: Error Handling ระดับ Row

```
execute_values(cur, sql, rows, page_size=2000):
  1. SAVEPOINT ev_batch
  2. execute_values(rows) ทั้ง batch
  3. ถ้า fail → ROLLBACK TO SAVEPOINT ev_batch
  4. Retry ทีละ row แต่ละ row SAVEPOINT เอง
  5. Row ที่ fail → skip + log "Skipped N bad rows"
  6. Row ที่ผ่าน → commit ปกติ
```

→ **ไม่มี row ไหนทำให้ import ทั้งหมดล้มเหลว**

---

## 5. Stack Mode — ไม่ Merge ไม่ Dedupe

### 5.1 ทำไมต้อง Stack

**User requirement**: `total_records = portal + app1 + app2` — ข้อมูลต้องเหลือตามจริงมากที่สุด

**Use cases ที่ต้องรู้ว่าใครอยู่กี่ระบบ**:
1. รายงาน cross-system coverage: ประชาชนคนเดียวตรวจซ้ำกี่แห่ง
2. Data quality: ระบบไหนกรอกครบกว่า (per-field null rate per source)
3. Audit trail: ข้อมูลเฉพาะเจาะจงมาจาก system ไหน

### 5.2 Natural Key ใหม่

```sql
-- ก่อน (migration 001-004):
raw_patients (idcard_hash UNIQUE, ...)

-- หลัง (migration 011):
raw_patients (idcard_hash, data_source, ...)
  UNIQUE(idcard_hash, data_source)   -- คนเดียว 2 ระบบ = 2 แถว
```

ตาราง child (`raw_visits`, `raw_vitalsigns`, ฯลฯ) มี FK `patient_id` → `raw_patients(id)`
และเพิ่ม `data_source` ของตัวเอง (ควรตรงกับ parent เสมอ)

### 5.3 Composite UNIQUE ถูกถอด

`migration 004` เคยตั้ง `UNIQUE(patient_id, visit_date, facility_code)`
บน raw_visits/vitalsigns/ฯลฯ → migration 011 ถอดเหลือ `INDEX` ธรรมดา

เหตุผล: **visit เดียวกันจาก 2 ระบบ = ให้เป็น 2 row ได้** (นับเป็นการตรวจซ้ำ)

### 5.4 ผลกระทบต่อ Query

```sql
-- จำนวนการตรวจ (visits) — Portal อาจนับคนเดียวกันซ้ำ
SELECT COUNT(*) FROM raw_vitalsigns;

-- จำนวนประชาชนคัดกรอง (unique people)
SELECT COUNT(DISTINCT idcard_hash) FROM raw_patients;

-- ตรวจซ้ำข้ามระบบ
SELECT * FROM v_cross_system_duplicates;
-- schema: idcard_hash, n_systems, systems, n_patient_rows

-- แยกตาม source
SELECT * FROM v_source_row_counts;
-- schema: table_name, data_source, n
```

### 5.5 ทำไมไม่ใช้ `COALESCE` merge

Plan อื่นคือ "best-available" merge (Portal > App1 > App2) แต่มีปัญหา:
- เสียข้อมูลต่าง — ถ้า Portal `BMI=26` กับ App2 `BMI=27.5` merge แล้วได้ 1 ค่า ไม่รู้คลาดเคลื่อน
- ไม่นับ visit ซ้ำ — ไม่ตรงกับ requirement รายงาน

**Stack + COALESCE ระดับ view** (ไม่ใช่ระดับ row) ให้ความยืดหยุ่นสูงสุด:
- Raw layer: เก็บทุก row ทุกค่าจริง
- View layer: compute "best value per person" เมื่อจำเป็น (`LAST_VALUE(bmi) OVER ...`)

---

## 6. App2 Normalizer — Thai Labels → Code

App2 เป็นไฟล์เดียวที่แตกต่างจาก Portal/App1 มาก:
- **Pre-aggregated**: 1 row ครอบคลุมทุกตาราง (patient + vitalsigns + homevisit + homehealth + lab)
- **Thai labels**: ค่าเป็นข้อความไทย ("ปกติ"/"เสี่ยง"/"อ้วนระดับ 1") แทนเลข code
- **Pre-computed derived**: `BMI`, `AGE_GROUP`, `LAB_EGFR` คำนวณมาแล้ว

### 6.1 Split Strategy

1 row ใน app2.csv → 5 rows ข้าม 5 ตาราง:

```
app2.csv (1 แถว)
   │
   ├─→ raw_patients   (sex, age, age_group_src)
   ├─→ raw_vitalsigns (code fields + bmi_src + *_src labels)
   ├─→ raw_homevisit  (district + *_src for edu/occupation/etc.)
   ├─→ raw_homehealth (family history bools + *_src for smoke/alcohol)
   └─→ raw_lab_results(binary results + 3 numeric labs + 11 text interpretations)
```

### 6.2 Thai → Code Mapping Table

| ค่าดิบ App2 | ค่าหลัง normalize | ที่ใช้ |
|-------------|--------------------|-------|
| "ชาย" / "หญิง" | 10 / 20 | `MALE` → `sex` |
| "ปกติ" / "เสี่ยง" / "เป็น" / "ผิดปกติ" | 0 / 1 / 1 / 1 | `DM_NAME`/`HPT_NAME`/etc. → `found_*` |
| "ไม่เป็น" / "เป็น" / "ไม่แน่ใจ" | False / True / NULL | `H_DM_NAME`/etc. → `parent_*` |
| "15-34 ปี" / "35-44 ปี" / "45-59 ปี" / "60 ปีขึ้นไป" | proxy age 25 / 40 / 52 / 70 | `AGE_GROUP` → `age` |
| 9999 | NULL | `DISTRICT`, `WRKDISTRICT` |
| Thai label อื่น (เช่น "อ้วนระดับ 1", "ห้องเช่า") | เก็บไว้ใน `*_src` column | `BMI_GROUP`, `HOMETYPE_NAME` |

### 6.3 What App2 CAN'T Provide

- **ไม่มี HEIGHT / WEIGHT** — `bmi` (our calc) = NULL, ใช้ได้แค่ `bmi_src` (App2's)
- **ไม่มี SBP / DBP** — `map_bp`, `pulse_pressure`, `bp_group` = NULL
- **ไม่มี SCN9Q / ST50 individual items** — `phq9_total`, `st5_total` = NULL
- **ไม่มี birth_year** — ใช้ `AGE_GROUP` midpoint เป็น proxy age

→ App2 rows จะมี **NULL มากกว่า** Portal/App1 rows สำหรับ ETL-derived fields
การตีความต้องระวัง (เวลา average BMI อย่าทำ `AVG(bmi)` จะตกหล่น App2 — ใช้
`AVG(COALESCE(bmi, bmi_src))` แทน)

---

## 7. Derived Fields 3 ชั้น

ทุก raw table มี **3 ชั้น** ของ column:

### 7.1 Layer 1: Raw

คัดจาก CSV ตรง ๆ (หลัง out-of-range → NULL)

```
height_cm, weight_kg, sbp, dbp, hemoglobin, cholesterol, fbs, ...
```

### 7.2 Layer 2: Source-provided (`*_src`)

ค่าที่ source system คำนวณไว้ให้ — เก็บไว้เพื่อ audit

```
bmi_src          (App2)  ← App2's pre-computed BMI
bmi_group_src    (App2)  ← "อ้วนระดับ 1"
age_group_src    (App2)  ← "15-34 ปี"
cbc_interp_src   (App2)  ← "ปกติ" / "ผิดปกติ" / "ไม่ได้ตรวจ"
hemoglobin_src   (App2)  ← LAB_HEMOGLOBIN numeric
edu_src          (App2)  ← "ปริญญาตรี" / "มัธยมศึกษา" (ไม่ map เป็น code)
```

### 7.3 Layer 3: ETL-derived (plain name)

เราคำนวณเองจาก raw — **harmonized ทุก source** (ถ้า raw มีครบ)

| Column | สูตร | เกณฑ์ |
|--------|------|-------|
| `bmi` | `weight_kg / (height_cm/100)²` | 10 ≤ bmi ≤ 80 else NULL |
| `bmi_group` | int 1-5 | 1=ผอม, 2=ปกติ, 3=เกิน, 4=อ้วน I, 5=อ้วน II (WHO Asia-Pacific) |
| `bp_group` | int 1-4 | 1=ปกติ, 2=สูงกว่าปกติ, 3=HTN-I, 4=HTN-II |
| `map_bp` | `DBP + (SBP-DBP)/3` | Mean Arterial Pressure |
| `pulse_pressure` | `SBP - DBP` | — |
| `phq9_total` | Σ(Q1..Q9) | NULL if any item NULL (conservative) |
| `phq9_severity` | 0-4 | ปกติ/เล็ก/กลาง/ค่อนข้าง/รุนแรง |
| `st5_total` | Σ(Q1..Q5) | — |
| `st5_severity` | 0-4 | น้อย/ปานกลาง/สูง/รุนแรง/รุนแรงมาก |
| `age_group` | label | วัยเรียน / วัยเริ่มทำงาน / ... / สูงวัย |
| `egfr_stage` | text | G1 / G2 / G3a / G3b / G4 / G5 (KDIGO 2012) |
| `anemia_class` | text | microcytic / normocytic / macrocytic / NULL ถ้าไม่ anemic |

### 7.4 ตัวอย่าง: เปรียบเทียบ `bmi` vs `bmi_src`

```sql
-- ความคลาดเคลื่อนระหว่าง App2 pre-computed BMI กับของเราจาก HEIGHT+WEIGHT
-- (ใช้ได้เฉพาะที่ Portal/App1 มี height+weight AND App2 มี bmi_src)
SELECT
    ROUND(AVG(ABS(bmi - bmi_src)), 2) AS avg_diff,
    COUNT(*) FILTER (WHERE ABS(bmi - bmi_src) > 1) AS n_large_diff
FROM raw_vitalsigns
WHERE bmi IS NOT NULL AND bmi_src IS NOT NULL;
```

---

## 8. การอัปโหลดผ่านหน้าเว็บ

### 8.1 URL + Access

- `/admin/upload-bundle` — หน้าอัปโหลด (ต้อง login ด้วย `ADMIN_PASSWORD`)
- หน้านี้มีได้ 2 โหมด:
  1. **เลือกโฟลเดอร์ (แนะนำ)** — `<input webkitdirectory>` เลือกโฟลเดอร์ที่มี
     `portal/`, `app1/`, `app2/` ข้างใน — ระบบ detect source จาก path อัตโนมัติ
  2. **เลือกไฟล์ทีละชุด** — drag-drop หลายไฟล์ + ระบุ default source ถ้าจำเป็น

### 8.2 Memory-safe Upload

| ขั้น | เทคนิค | Peak memory |
|------|---------|------------|
| Browser → Server | multipart streaming | < 100 KB buffer |
| Server receive | `UploadFile.file` (SpooledTemporaryFile, 1 MB spool) | 1 MB × N files |
| Copy to tempfile | `shutil.copyfileobj` in 64 KB chunks | 64 KB per copy |
| Read into pandas | `pd.read_csv(path, dtype=str)` one file at a time | 1 DataFrame's RAM |

→ ระบบประมวลผลได้ไฟล์ **> 1 GB** บนเครื่องที่มี RAM 4 GB

### 8.3 Size Limits

- **ต่อไฟล์**: 1 GB (ปรับใน `admin.py:MAX_FILE_BYTES`)
- **ต่อ bundle**: ไม่มี hard limit — จำกัดด้วย disk tempfile
- **FastAPI/Starlette**: ไม่มี default body size limit
- **Reverse proxy**: ถ้าใช้ Cloudflare tunnel ระวัง Cloudflare free plan 100 MB limit
  → อัปโหลดใหญ่ ๆ ให้ใช้ direct localhost (port 9002)

### 8.4 Flow

1. User drag/drop โฟลเดอร์ → JS เก็บ `file.webkitRelativePath` ใน hidden input `relpaths`
2. JS แสดง badge per-file: source (portal/app1/app2/?) + type (pt/vitalsignslf/...)
3. Submit → server:
   - Validate CSRF + session
   - Stream each file to tempfile
   - Detect source (relpath) + type (filename)
   - Build manifest `[{source, file_type, tmp_path, filename, size_bytes}, ...]`
   - INSERT import_history row (status='running')
   - Launch background thread → run `_run_bundle_import(manifest, history_id)`
   - Return redirect to `/admin/history` พร้อม flash message
4. Background thread:
   - Acquire PostgreSQL advisory lock (prevent concurrent imports)
   - `TRUNCATE raw_patients CASCADE`
   - Loop by source (portal → app1 → app2), by file_type (pt → children):
     - `pd.read_csv(tmp_path)` one file at a time
     - Call `etl.import_*` with `data_source=source`
     - `del df` หลัง import (free memory)
   - `conn.commit()`
   - Refresh materialized views (non-fatal)
   - Flush Redis + data_adapter cache
   - `os.unlink` ทุก tempfile
   - Update `import_history` (status='success')

### 8.5 Progress Monitoring

- ดูสถานะ: `/admin/history` — auto-refresh ทุก 5 วินาที
- API endpoint (JSON): `GET /admin/api/import-status/<history_id>`
- Log: `docker logs bma-health-api` (ดูทาง `make logs`)

---

## 9. การใช้งาน Query / Report

### 9.1 พื้นฐาน — นับ records

```sql
-- จำนวนการตรวจทั้งหมด (รวม duplicates ข้ามระบบ)
SELECT COUNT(*) FROM raw_vitalsigns;

-- จำนวนประชาชนเฉพาะ
SELECT COUNT(DISTINCT idcard_hash) FROM raw_patients;

-- แยกตาม source
SELECT data_source, COUNT(*) FROM raw_vitalsigns GROUP BY data_source;
```

### 9.2 Cross-system Duplicate Report

```sql
-- คนที่ตรวจมากกว่า 1 ระบบ
SELECT * FROM v_cross_system_duplicates LIMIT 10;
-- columns: idcard_hash, n_systems, systems, n_patient_rows

-- สรุปจำนวน rows แต่ละ source แต่ละ table
SELECT * FROM v_source_row_counts;
```

### 9.3 Best-available Value Query

```sql
-- ค่า BMI ที่ดีที่สุด: priority Portal calc → App1 calc → App2 src
SELECT
    idcard_hash,
    COALESCE(
        MAX(bmi) FILTER (WHERE data_source = 'portal'),
        MAX(bmi) FILTER (WHERE data_source = 'app1'),
        MAX(bmi_src) FILTER (WHERE data_source = 'app2')
    ) AS best_bmi
FROM raw_patients p
JOIN raw_vitalsigns v USING (idcard_hash)
GROUP BY idcard_hash;
```

### 9.4 Data Quality per Source

```sql
-- % null per field per source (สำหรับเทียบ data quality ระหว่างระบบ)
SELECT
    data_source,
    COUNT(*) AS total,
    ROUND(100.0 * COUNT(*) FILTER (WHERE bmi IS NULL) / COUNT(*), 1) AS pct_null_bmi,
    ROUND(100.0 * COUNT(*) FILTER (WHERE sbp IS NULL) / COUNT(*), 1) AS pct_null_sbp,
    ROUND(100.0 * COUNT(*) FILTER (WHERE fasting_glucose IS NULL) / COUNT(*), 1) AS pct_null_fbs
FROM raw_vitalsigns
GROUP BY data_source;
```

---

## 10. Troubleshooting & Data Quality

### 10.1 Common Issues

| Issue | ดูที่ | แก้ |
|-------|------|-----|
| `IDCARD_HASH_SECRET is not set` | env var | `export IDCARD_HASH_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')` |
| `Another import is currently running` | PostgreSQL advisory lock | รอ import เก่าเสร็จ หรือ restart connection ที่ถือ lock |
| Import "success" แต่ row count ต่ำ | Check `rows_skipped` + log | Skipped rows มักเพราะ PID decode ไม่ได้ |
| View refresh failed | `import_history.view_refresh_error` | Re-run `POST /admin/refresh` หรือ `make refresh-views` |
| Upload timeout (~120s) | Uvicorn default timeout | เพิ่ม `--timeout-keep-alive 300` หรือ proxy timeout |
| Memory error during import | DataFrame too large | ลดขนาดไฟล์ หรือเพิ่ม Docker memory limit |

### 10.2 Data Quality Dashboard

ดู `/admin/data-quality` — แสดง:
- % null per column per table
- Blocked fields (100% null)
- Recent import history

### 10.3 Per-source Quality Comparison

```sql
-- เปรียบเทียบ completeness ระหว่าง source
WITH src_stats AS (
  SELECT
    data_source,
    COUNT(*) AS n,
    100.0 * COUNT(height_cm) / COUNT(*) AS pct_height,
    100.0 * COUNT(sbp)       / COUNT(*) AS pct_sbp,
    100.0 * COUNT(bmi)       / COUNT(*) AS pct_bmi_calc,
    100.0 * COUNT(bmi_src)   / COUNT(*) AS pct_bmi_src
  FROM raw_vitalsigns
  GROUP BY data_source
)
SELECT * FROM src_stats;
```

→ App2 จะมี `pct_height=0`, `pct_sbp=0`, `pct_bmi_calc=0` (raw ไม่มี) แต่ `pct_bmi_src` สูง

---

## ภาคผนวก: ไฟล์ที่เกี่ยวข้องใน Repo

| Path | หน้าที่ |
|------|--------|
| `db/migrations/011_multi_source_stack.sql` | Schema changes: data_source tag, *_src cols, derived cols |
| `etl/import_csv.py` | Main ETL orchestrator + per-table importers |
| `etl/derived.py` | Shared derived-field helpers (BMI, MAP, PHQ-9, eGFR stage) |
| `etl/app2_normalizer.py` | App2 Thai→code mapping + row splitter |
| `api/admin.py` | Bundle upload handler + background worker |
| `api/templates/admin/upload_bundle.html` | Upload UI (folder picker + drag-drop) |
| `DATA-DICTIONARY.md` | Column-level reference (ยังใช้เป็น dictionary ได้) |
| `MEDICAL-DICTIONARY.md` | Clinical range reference |

---

## Change Log

| Date | Change |
|------|--------|
| 2026-04-22 | Initial multi-source preprocessing pipeline (migration 011) |
| 2026-04-17 | (Legacy) DATA-DICTIONARY.md documenting Portal-only design |
