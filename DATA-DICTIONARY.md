# ⚠️ ARCHIVED — see `bma-med/MED-FACTSHEET.md` + `bma-med/CODES_REFERENCE.md`

> **As of Sprint S1 (2026-05) this document is preserved for historical
> reference only.** The canonical data dictionary has moved to:
>
> - **`/Users/dev/bma-med/MED-FACTSHEET.md`** — variable definitions, types, codebooks (the TOR)
> - **`/Users/dev/bma-med/CODES_REFERENCE.md`** — auto-generated value→Thai-label reference
> - **`/Users/dev/bma-med/CLEANING_NOTES.md`** — methodology, deviations, derived variables
>
> The cleaner (`bma-med/clean.py`) parses `MED-FACTSHEET.md` at module
> load — the factsheet *is* the runtime contract. Edits here have no
> effect on the pipeline. Cross-walk in `bma-health-db/CUTOVER-PLAN.md`.

---

# DATA-DICTIONARY.md (archived) — คู่มือ Data Cleansing & Integration ฉบับเต็ม

> **โครงการคัดกรองสุขภาพกรุงเทพมหานคร** | สำนักการแพทย์ กรุงเทพมหานคร
> Last Updated: 2026-04-17

---

## สารบัญ

- [1. ภาพรวมแหล่งข้อมูล](#1-ภาพรวมแหล่งข้อมูล)
- [2. สรุปจำนวนตัวแปรแต่ละไฟล์](#2-สรุปจำนวนตัวแปรแต่ละไฟล์)
- [3. Intersection & Difference ระหว่าง Source](#3-intersection--difference-ระหว่าง-source)
- [4. พจนานุกรมตัวแปรฉบับเต็ม](#4-พจนานุกรมตัวแปรฉบับเต็ม)
- [5. Data Cleansing Rules](#5-data-cleansing-rules)
- [6. Data Merge Pipeline](#6-data-merge-pipeline)
- [7. Derived Variables (ตัวแปรคำนวณ)](#7-derived-variables)
- [8. Database Schema Design](#8-database-schema-design)

---

## 1. ภาพรวมแหล่งข้อมูล

### 3 แหล่งข้อมูล

| Source | ระบบ | ไฟล์ | ลักษณะข้อมูล | PID/ID Format |
|--------|------|------|-------------|--------------|
| **Portal** | ระบบหลัก สนพ. (portal_top) | 7 ไฟล์ | ข้อมูลดิบ ค่าเป็น code ตัวเลข | `IDCARD` (Base64-encoded บัตร ปชช.) |
| **App1** | แอปมือถือ สำนักอนามัย | 5 ไฟล์ | ข้อมูลดิบ format คล้าย Portal แต่ subset | `PID` (Base64-encoded) |
| **App2** | แดชบอร์ดสรุป (pre-aggregated) | 1 ไฟล์ | ข้อมูล pre-processed มี label ภาษาไทย + ค่าคำนวณ (BMI, กลุ่มอายุ) | `PID` (Base64-encoded) |

### ข้อมูลตัวอย่าง 100 records — ไม่มี PID ซ้ำกันระหว่าง source

```
Portal: 100 unique IDs
App1:   100 unique IDs
App2:   100 unique IDs
Portal ∩ App1: 0  (ตัวอย่างคนละชุด — ข้อมูลจริงจะซ้อนทับ)
Portal ∩ App2: 0
App1 ∩ App2:   0
Union: 300 unique IDs
```

**หมายเหตุ**: ในข้อมูลจริง จะมี PID ซ้อนทับกันระหว่าง source (ประชาชนคนเดียวกันตรวจได้หลายแอป) → ใช้ HMAC-SHA256 hash เพื่อ match

---

## 2. สรุปจำนวนตัวแปรแต่ละไฟล์

### Portal (7 ไฟล์, 383 columns รวม)

| ไฟล์ | จำนวน columns | คำอธิบาย |
|------|-------------|---------|
| pt.csv | **12** | ข้อมูลผู้ป่วย (ID, เพศ, วันเกิด, ชื่อ) |
| pthistory.csv | **20** | ประวัติการมาตรวจ (ศาสนา, LGBTQ, ข้อมูลติดต่อ) |
| vitalsignslf.csv | **92** | สัญญาณชีพ + คัดกรองโรค + สุขภาพจิต + ส่งต่อ |
| homevisit.csv | **88** | สังคมเศรษฐกิจ (การศึกษา, อาชีพ, ที่อยู่, สัตว์เลี้ยง, ความพิการ) |
| homehealth.csv | **63** | พฤติกรรม (โรคเรื้อรัง, การรักษา, อาหาร, วัคซีน, ครอบครัว) |
| labhealth.csv | **75** | ผลแลป (CBC, FBS, cholesterol, ตับ, ไต, มะเร็ง) |
| labhealthext.csv | **33** | ผลตรวจเพิ่มเติม (ระบบหายใจ, MSD ปวดกล้ามเนื้อ, ต้อเนื้อ) |

### App1 (5 ไฟล์, 189 columns รวม)

| ไฟล์ | จำนวน columns | เทียบ Portal |
|------|-------------|-------------|
| pt.csv | **17** | Portal มี 12 — App1 เพิ่ม AGE, HPTCODE, LOCATION, SUBHPT |
| vitalsignslf.csv | **56** | Portal มี 92 — App1 เป็น subset (ไม่มีส่งต่อ, ชีพจร, เขต) |
| homevisit.csv | **24** | Portal มี 88 — App1 เป็น subset (ไม่มีสัตว์เลี้ยง, ที่ทำงาน) |
| homehealth.csv | **26** | Portal มี 63 — App1 เป็น subset (ไม่มีอาหาร, วัคซีน, HIV) |
| labhealth.csv | **66** | Portal มี 75 — App1 เกือบเท่า (มี EGFR_LAB เพิ่ม) |

**App1 ไม่มี**: pthistory.csv, labhealthext.csv

### App2 (1 ไฟล์, 103 columns)

| ลักษณะ | รายละเอียด |
|--------|-----------|
| จำนวน columns | **103** |
| ตรงกับ Portal | 42 columns |
| ตรงกับ App1 | 34 columns |
| **เฉพาะ App2 (61 columns)** | Label ภาษาไทย (*_NAME), ลำดับ sort (*_SORT), BMI คำนวณแล้ว, กลุ่มอายุ, interpretation ผลแลป |

---

## 3. Intersection & Difference ระหว่าง Source

### 3.1 pt.csv — ข้อมูลผู้ป่วย

```
Portal (12 cols) ←→ App1 (17 cols)
├── ร่วมกัน (6):  FNAME, LNAME, FIRSTDATE, LASTDATE, MALE, PNAME
├── Portal only (6):  IDCARD, NOTYPE, RANK, BIRTHDATE, EFNAME, ELNAME
└── App1 only (11):   PID, AGE, BRTHDATE, PHONE, HPTCODE, LOCATION,
                      SUBHPT, HPT, PNAMEOTH, FIRSTSTF, VSTDATE
```

**ปัญหาสำคัญ**:
- Portal ใช้ `IDCARD`, App1 ใช้ `PID` — ทั้งคู่เป็น Base64 encode ของเลขบัตร ปชช.
- Portal ใช้ `BIRTHDATE`, App1 ใช้ `BRTHDATE` — ชื่อต่างกัน format เดียวกัน
- App1 มี `AGE` คำนวณมาแล้ว, Portal ไม่มี (ต้องคำนวณเอง)

### 3.2 vitalsignslf.csv — สัญญาณชีพ

```
Portal (92 cols) ←→ App1 (56 cols)
├── ร่วมกัน (56):  ทุก column ของ App1 มีใน Portal
├── Portal only (36):  DISTRICTBKK, LOCATION, PR, DMFM, HRT,
│                      STMNG1-4, SMOKETYPE1-4, LEFTRW, LEFTVL,
│                      RIGHTRW, RIGHTVL, CS*, RF*, WDSICK, HOURLIST,
│                      VSTTIME, FLAG, EXTOTH
└── App1 only (0):     ไม่มี — App1 เป็น pure subset ของ Portal
```

**ตัวแปรสำคัญที่ Portal มีแต่ App1 ไม่มี**:
- `DISTRICTBKK` — รหัสเขต (ใช้ backfill)
- `PR` — ชีพจร
- `DMFM` — ประวัติเบาหวานครอบครัว
- `STMNG1-4` — วิธีจัดการความเครียด
- `SMOKETYPE1-4` — ประเภทบุหรี่ที่สูบ
- `CS*` — ผลการส่งต่อ (refer)
- `RF*` — เหตุผลการส่งต่อ

### 3.3 homevisit.csv — สังคมเศรษฐกิจ

```
Portal (88 cols) ←→ App1 (24 cols)
├── ร่วมกัน (24):  ทุก column ของ App1 มีใน Portal
├── Portal only (64):  PET, DOG, CAT, DOGAMT, CATAMT, AMLOTH,
│                      REQUEST1-7, WORKSHOP, WRKDISTRICT, WRKTYPE,
│                      WRKJOURNEY, HEALTHUSE, HADDR, HMOO, HSOI,
│                      HSTREET, HPROVINCE, HZIPCODE, CR*, DISCARE*,
│                      PRVLGCHK, OCCPTN17-19, ORGBMA, ...
└── App1 only (0):     ไม่มี
```

**Portal มีแต่ App1 ไม่มี (สำคัญ)**:
- สัตว์เลี้ยง: PET, DOG, CAT + จำนวน
- ที่ทำงาน: WRKDISTRICT, WRKTYPE, WRKJOURNEY
- ความต้องการบริการ: REQUEST1-7, WORKSHOP
- ที่อยู่ละเอียด: HADDR, HMOO, HSOI, HSTREET (PII — ไม่นำเข้า DB)

### 3.4 homehealth.csv — พฤติกรรม

```
Portal (63 cols) ←→ App1 (26 cols)
├── ร่วมกัน (26):  ทุก column ของ App1 มีใน Portal
├── Portal only (37):  DMRS, HPTRS, CHLTRRS, HRTRS, KIDNEYRS, STROKERS,
│                      FOOD, WATER, NOODLE, ALGYFOOD, ALGYMED,
│                      COVID, VCCCOVID, VCCINFLUZA, CHKHIV,
│                      ASTH, ASTHRS, EMPHY, EMPHYRS, EPLPY, EPLYRS,
│                      TREATSLF, CGTDSMN, CGTDSMNRS, ...
└── App1 only (0):     ไม่มี
```

**Portal มีแต่ App1 ไม่มี (สำคัญ)**:
- สถานะการรักษา: DMRS-STROKERS (1=ไม่รักษา, 2=ไม่สม่ำเสมอ, 3=สม่ำเสมอ)
- อาหาร: FOOD (ทอด), WATER (น้ำหวาน), NOODLE (บะหมี่)
- วัคซีน: VCCCOVID, VCCINFLUZA
- โรคระบบหายใจ: ASTH (หอบหืด), EMPHY (ถุงลมโป่งพอง), EPLPY (ลมชัก)

### 3.5 labhealth.csv — ผลแลป

```
Portal (75 cols) ←→ App1 (66 cols)
├── ร่วมกัน (65):  เกือบทั้งหมด
├── Portal only (10):  BUNRS, CANCELST, CANCELSTF, CHKEGFR,
│                      FIRSTSTF, FLAG, LASTDATE, LASTSTF, PRVLG, VSTTIME
└── App1 only (1):     EGFR_LAB (ค่า eGFR จากห้องแลป — Portal ใช้ EGFRRS แทน)
```

### 3.6 ไฟล์ที่มีเฉพาะบาง Source

| ไฟล์ | Portal | App1 | App2 | ข้อมูลที่ได้ |
|------|--------|------|------|-----------|
| **pthistory.csv** | ✅ (20 cols) | ❌ | ❌ | ศาสนา, LGBTQ, ข้อมูลติดต่อ |
| **labhealthext.csv** | ✅ (33 cols) | ❌ | ❌ | ระบบหายใจ, MSD ปวดกล้ามเนื้อ 10 ตำแหน่ง, ต้อเนื้อ |
| **app2.csv** | ❌ | ❌ | ✅ (103 cols) | ข้อมูล pre-processed + 61 columns เฉพาะ (label, BMI, กลุ่มอายุ) |

### 3.7 App2 — 61 Columns เฉพาะ

| ประเภท | Columns | คำอธิบาย |
|--------|---------|---------|
| **Label ภาษาไทย (22)** | `SMOKE_NAME`, `DM_NAME`, `HPT_NAME`, `ALCOHAL_NAME`, `EDU_NAME`, `OCCPTN_NAME`, `HOMETYPE_NAME`, `PRVLG_NAME`, `SELFOUR_NAME`, `ST5_NAME`, `VSACT_NAME`, `DRSCN_NAME`, `SCR2Q_NAME`, `HOMELAND_NAME`, `WRKJOURNEY_NAME`, `RISKDM_NAME`, `RISKHPT_NAME`, `RISKCDVCL_NAME`, `RISKBMI_NAME`, `CDVCL_NAME`, `STROKE_NAME`, `FAT_NAME`, `CHLTR_NAME`, `OTH_NAME` | ค่า code แปลงเป็น text ("ไม่สูบ", "ปกติ", "ผิดปกติ") |
| **Sort order (11)** | `AGE_SORT`, `BMI_SORT`, `ALCOHAL_SORT`, `SMOKE_SORT`, `BP_SORT`, `SELFOUR_SORT`, `ST5_SORT`, `VSACT_SORT`, `DRSCN_SORT`, `SCR2Q_SORT`, `HOMELAND_SORT` | ลำดับสำหรับเรียง UI |
| **ค่าคำนวณ (4)** | `BMI`, `BMI_GROUP`, `AGE_GROUP`, `BP_GROUP` | ค่าที่ compute จากข้อมูลดิบ |
| **ผลแลป interpretation (11)** | `CHESTRES`, `EKGRES`, `BLDSGRES`, `LIVERRES`, `URICRES`, `CVCRES`, `CLCRES`, `EGFRES`, `CBCRES`, `UARES`, `CHLTRRES` | "ปกติ" / "ผิดปกติ" / "ไม่ได้ตรวจ" |
| **ค่าแลปตัวเลข (3)** | `LAB_HEMOGLOBIN`, `LAB_CHOLESTERAL`, `LAB_EGFR` | ค่าจากห้องแลป (float) |
| **ประวัติโรคครอบครัว (6)** | `H_DM_NAME`, `H_HPT_NAME`, `H_STROKE_NAME`, `H_CHLTR_NAME`, `H_HRT_NAME`, `H_KIDNEY_NAME` | "เป็น" / "ไม่เป็น" |
| **อื่นๆ (4)** | `HD` (ชื่อสถานพยาบาลเต็ม), `DISTRICT` (รหัสเขต 1001-1050), `WRKDISTRICT`, `DISTYPE` | ข้อมูลที่ Portal เก็บเป็น code |

---

## 4. พจนานุกรมตัวแปรฉบับเต็ม

### 4.1 ตัวแปรระบุตัวตน (Identity)

| Column | Source | Type | คำอธิบาย | การจัดการ |
|--------|--------|------|---------|---------|
| `IDCARD` | Portal | Base64 string | เลขบัตร ปชช. เข้ารหัส | Base64 decode → HMAC-SHA256 → `idcard_hash` |
| `PID` | App1, App2 | Base64 string | = IDCARD (ชื่อต่าง) | เหมือน IDCARD |
| `FNAME` | Portal, App1 | string | ชื่อ (**PII**) | **ไม่นำเข้า DB** |
| `LNAME` | Portal, App1 | string | นามสกุล (**PII**) | **ไม่นำเข้า DB** |
| `EFNAME` | Portal | string | ชื่ออังกฤษ (**PII**) | **ไม่นำเข้า DB** |
| `ELNAME` | Portal | string | นามสกุลอังกฤษ (**PII**) | **ไม่นำเข้า DB** |
| `PHONE` | App1, Portal(pthistory) | string | โทรศัพท์ (**PII**) | **ไม่นำเข้า DB** |
| `EMAIL` | Portal(pthistory) | string | อีเมล (**PII**) | **ไม่นำเข้า DB** |
| `IDLINE` | Portal(pthistory) | string | LINE ID (**PII**) | **ไม่นำเข้า DB** |

### 4.2 ตัวแปรประชากร (Demographics)

| Column | Source | Type | ค่าที่เป็นไปได้ | DB Column | Cleansing |
|--------|--------|------|---------------|-----------|-----------|
| `MALE` | Portal/App1 | int | 10=ชาย, 20=หญิง | `sex` | — |
| `MALE` | **App2** | **string** | **"ชาย", "หญิง"** | `sex` | **Map: ชาย→10, หญิง→20** |
| `BIRTHDATE` | Portal | datetime | DD/MM/YYYY HH:MM:SS | `birth_year` | แปลง พ.ศ. (>2400 → -543) |
| `BRTHDATE` | App1 | datetime | DD/MM/YYYY HH:MM:SS | `birth_year` | **ชื่อต่างจาก Portal** |
| `AGE` | App1 | int | 0-150 | `age` | ตรวจสอบ <0 หรือ >150 → NULL |
| `AGE_GROUP` | App2 | string | "15-34 ปี", "35-44 ปี", "45-59 ปี", "60 ปีขึ้นไป" | `age_group` | Map text → กลุ่ม |
| `PNAME` | Portal/App1 | int/float | 11=นาย, 12=นาง, 13=น.ส., 14=ด.ช., 15=ด.ญ. | `pname` | — |
| `NOTYPE` | Portal | int | 10=บัตร ปชช., 20=passport | `notype` | — |
| `RLGN` | Portal(pthistory) | int | 1=พุทธ, 2=อิสลาม, 3=คริสต์, 4=อื่นๆ | `religion` | — |
| `LGBTQ` | Portal(pthistory) | int | 1=ไม่ใช่, 2=ใช่, 3=ไม่ระบุ | `lgbtq` | — |

### 4.3 ตัวแปรสัญญาณชีพ (Vitals)

| Column | Source | Type | หน่วย | ค่าปกติ | Range ที่ยอมรับ | ถ้านอก range |
|--------|--------|------|------|--------|--------------|------------|
| `HBPN` | Portal/App1 | int | mmHg | 90-140 | **40-300** | → NULL |
| `LBPN` | Portal/App1 | int | mmHg | 60-90 | **20-200** | → NULL |
| `PREFPG` | Portal/App1 | float | mg/dL | 70-100 | **0-999** | → NULL |
| `POSTFPG` | Portal/App1 | float | mg/dL | 70-140 | **0-999** | → NULL |
| `HEIGHT` | Portal/App1 | float | cm | 150-180 | **50-250** | → NULL |
| `WEIGHT` | Portal/App1 | float | kg | 40-80 | **10-300** | → NULL |
| `WSTL` | Portal/App1 | float | cm | 60-90 | **30-200** | → NULL |
| `PR` | Portal only | int | bpm | 60-100 | **30-220** | → NULL |

**ตัวอย่างข้อมูลผิดปกติที่พบจริง** (จาก sample 100 records):
- `WSTL` = 0.0, 29.0 → **ต่ำกว่า 30 cm — ไม่ใช่รอบเอวจริง → NULL**
- `HBPN` = 0.0, 1.0 → **ไม่ใช่ความดันจริง → NULL**
- `LBPN` = 0.0, 1.0 → **เหมือนกัน → NULL**

### 4.4 ตัวแปรผลแลป (Lab Results)

| Column | Source | Type | หน่วย | ค่าปกติ | Range ยอมรับ | ถ้านอก range |
|--------|--------|------|------|--------|------------|------------|
| `HMGB` | Portal/App1 | float | g/dL | ชาย 13-17, หญิง 12-16 | **0-30** | → NULL |
| `HMTC` | Portal/App1 | float | % | 36-54 | **0-80** | → NULL |
| `MCV` | Portal/App1 | float | fL | 80-100 | **0-200** | → NULL |
| `FBS` | Portal/App1 | float | mg/dL | 70-100 | **0-999** | → NULL |
| `CHOLEST` | Portal/App1 | float | mg/dL | <200 | **0-999** | → NULL |
| `TRIGLY` | Portal/App1 | float | mg/dL | <150 | **0-999** | → NULL |
| `HDL` | Portal/App1 | float | mg/dL | M>40, F>50 | **0-500** | → NULL |
| `LDL` | Portal/App1 | float | mg/dL | <100 | **0-500** | → NULL |
| `SGOT` | Portal/App1 | float | U/L | 10-40 | **0-999** | → NULL |
| `SGPT` | Portal/App1 | float | U/L | 7-56 | **0-999** | → NULL |
| `ALKPPT` | Portal/App1 | float | U/L | 44-147 | **0-999** | → NULL |
| `URICACID` | Portal/App1 | float | mg/dL | M 3.4-7, F 2.4-6 | **0-50** | → NULL |
| `CRTININE` | Portal/App1 | float | mg/dL | 0.7-1.3 | **0-50** | → NULL |
| `EGFRRS` | Portal | float | mL/min | >90 ปกติ | **0-200** | → NULL |
| `EGFR_LAB` | App1 only | float | mL/min | >90 ปกติ | **0-200** | → NULL |
| `BUNRS` | Portal only | float | mg/dL | 7-20 | **0-200** | → NULL |

### 4.5 ตัวแปร Screening Results (Code)

| Column | ค่า | คำอธิบาย |
|--------|-----|---------|
| `RISKDM` | 0/1 | เสี่ยงเบาหวาน |
| `RISKHPT` | 0/1 | เสี่ยงความดัน |
| `RISKCDVCL` | 0/1 | เสี่ยงหลอดเลือดหัวใจ |
| `RISKBMI` | 0/1 | เสี่ยง BMI ผิดปกติ |
| `DM` | 0/1 | พบเบาหวาน |
| `HPT` | 0/1 | พบความดันสูง |
| `CDVCL` | 0/1 | พบโรคหลอดเลือดหัวใจ |
| `STROKE` | 0/1 | พบหลอดเลือดสมอง |
| `FAT` | 0/1 | พบอ้วน |
| `CHLTR` | 0/1 | พบไขมันผิดปกติ |
| `OTH` | 0/1 | พบโรคอื่น |

**App2 ใช้ text แทน code**: `DM_NAME`="ปกติ"/"เสี่ยง"/"เป็น", `RISKDM_NAME`="ปกติ"/"เสี่ยง"

### 4.6 ตัวแปรสุขภาพจิต

| Column | Type | คำอธิบาย | Range | การให้คะแนน |
|--------|------|---------|-------|-----------|
| `SCR2Q1` | int | Depression 2Q ข้อ 1 | 0-1 | 0=ไม่มี, 1=มีอาการ |
| `SCR2Q2` | int | Depression 2Q ข้อ 2 | 0-1 | 0=ไม่มี, 1=มีอาการ |
| `SCN9Q1`-`SCN9Q9` | int | PHQ-9 (9 ข้อ) | 0-3/ข้อ | 0=ไม่เลย, 1=หลายวัน, 2=มากกว่าครึ่ง, 3=เกือบทุกวัน |
| `ST501`-`ST505` | int | ST-5 ความเครียด (5 ข้อ) | 0-3/ข้อ | 0=ไม่เลย ... 3=เป็นประจำ |
| `SCRRS` | int | ผลคัดกรองรวม | 1-2 | 1=ปกติ, 2=ผิดปกติ |

---

## 5. Data Cleansing Rules

### 5.1 Rule 1: ลบทิ้ง (Filter Out)

| เงื่อนไข | เหตุผล | จำนวนที่คาดว่าจะลบ |
|---------|--------|-----------------|
| ID (IDCARD/PID) = NULL หรือว่าง หรือ decode ไม่ได้ | ไม่สามารถระบุตัวบุคคลได้ | <1% |
| `CANCELST` = 1 | record ถูกยกเลิกโดยเจ้าหน้าที่ | ~2-5% |
| Duplicate hash ใน batch เดียวกัน | ข้อมูลซ้ำ | ~3-5% |

### 5.2 Rule 2: แทนค่า NULL (Replace with NULL)

**หลักการ**: ค่าที่เป็นไปไม่ได้ทาง clinical → NULL (ไม่ใช่ 0 ไม่ใช่ค่าเฉลี่ย)

| Field | เงื่อนไข NULL | เหตุผลทางการแพทย์ |
|-------|-------------|-----------------|
| `BIRTHDATE` ปี < 1900 | ไม่มีใครอายุเกิน 126 ปี (สถิติโลก = 122 ปี) | |
| `BIRTHDATE` ปี > 2030 | ยังไม่เกิด | |
| อายุ < 0 หรือ > 150 | เป็นไปไม่ได้ | |
| `HEIGHT` < 50 cm | ทารกแรกเกิด ~50 cm, ผู้ใหญ่ต่ำสุด ~60 cm | |
| `HEIGHT` > 250 cm | คนสูงที่สุดในโลก = 272 cm | |
| `WEIGHT` < 10 kg | ทารก ~3 kg, เด็ก 1 ขวบ ~10 kg | |
| `WEIGHT` > 300 kg | คนหนักที่สุดในไทย ~300 kg | |
| `WSTL` < 30 cm | ทารกรอบเอว ~30 cm | |
| `WSTL` = 0 | **พบจริงในข้อมูล** — ไม่ได้วัดแต่กรอก 0 | |
| `HBPN` (SBP) < 40 | SBP ต่ำกว่า 40 = cardiac arrest | |
| `HBPN` = 0 หรือ 1 | **พบจริงในข้อมูล** — ไม่ได้วัดแต่กรอก 0/1 | |
| `LBPN` (DBP) < 20 | DBP ต่ำกว่า 20 = shock รุนแรง | |
| `FBS` > 999 | เครื่องวัดไม่ให้ค่าเกินนี้ | |
| `HMGB` > 30 g/dL | Hemoglobin สูงสุดจริง ~20 g/dL (polycythemia vera) | |
| `BMI` > 80 | BMI สูงสุดจริง ~70 (คนหนักที่สุดในโลก) | |
| Integer > 2,147,483,647 | เกิน PostgreSQL INT4 range | |
| Float = inf / NaN | ข้อมูลเสีย | |

### 5.3 Rule 3: แปลงค่า (Transform)

| เงื่อนไข | การแปลง | เหตุผล |
|---------|--------|--------|
| `BIRTHDATE` ปี > 2400 | ลบ 543 | ข้อมูลใช้ปี พ.ศ. |
| `MALE` = "ชาย" (App2) | → 10 | แปลง text → code |
| `MALE` = "หญิง" (App2) | → 20 | แปลง text → code |
| `DISTRICTBKK` ว่าง | Backfill จาก `ref_facility_districts` | ดึงเขตจาก facility code |
| `DISTRICT` = 9999 (App2) | → NULL | 9999 = ไม่ระบุ (พบ 16% ในตัวอย่าง) |

### 5.4 Rule 4: Error Handling ระดับ Row

```
เมื่อ INSERT batch 500 rows ล้มเหลว:
  1. SAVEPOINT ก่อน batch
  2. ถ้า batch fail → ROLLBACK TO SAVEPOINT
  3. Retry ทีละ row
  4. Row ที่ fail → skip + log
  5. Row ที่ผ่าน → insert ปกติ
→ ไม่มี row ไหนทำให้ import ทั้งหมดล้มเหลว
```

---

## 6. Data Merge Pipeline

### 6.1 ลำดับการนำเข้า

```
Portal/App1 ไฟล์ที่ตรงกัน → merge เข้า table เดียวกัน

1. pt.csv (Portal IDCARD + App1 PID)  → raw_patients
2. pthistory.csv (Portal only)         → raw_visits
3. vitalsignslf.csv (Portal + App1)    → raw_vitalsigns
4. homevisit.csv (Portal + App1)       → raw_homevisit
5. homehealth.csv (Portal + App1)      → raw_homehealth
6. labhealth.csv (Portal + App1)       → raw_lab_results
7. labhealthext.csv (Portal only)      → raw_lab_extended
```

### 6.2 Patient Matching

```
Portal IDCARD ──→ Base64 decode ──→ HMAC-SHA256 ──→ idcard_hash
App1 PID ────────→ Base64 decode ──→ HMAC-SHA256 ──→ idcard_hash  (same hash)
App2 PID ────────→ Base64 decode ──→ HMAC-SHA256 ──→ idcard_hash  (same hash)

ถ้า hash ตรงกัน = คนเดียวกัน → ON CONFLICT (idcard_hash) DO UPDATE
```

### 6.3 Column Mapping เมื่อ Merge

| Portal Column | App1 Column | App2 Column | DB Column | วิธีจัดการ |
|--------------|-------------|-------------|-----------|----------|
| `IDCARD` | `PID` | `PID` | `idcard_hash` | Hash ด้วย algorithm เดียวกัน |
| `BIRTHDATE` | `BRTHDATE` | — | `birth_year` | ชื่อต่างแต่ format เดียวกัน |
| — | `AGE` | `AGE_GROUP` | `age`, `age_group` | Portal คำนวณเอง, App1/App2 มีมาแล้ว |
| `EGFRRS` | `EGFR` + `EGFR_LAB` | `LAB_EGFR` | `egfr` | ใช้ค่าจาก source ที่มี |
| `DISTRICTBKK` | — | `DISTRICT` | `district_code` | Portal มักว่าง → backfill |

### 6.4 District Backfill Pipeline (4 ขั้น)

```
Priority 1: DISTRICTBKK จาก vitalsignslf.csv (ถ้ามี)
    ↓ ยังว่าง?
Priority 2: DISTRICT จาก app2.csv (ถ้ามี, ≠9999)
    ↓ ยังว่าง?
Priority 3: facility_code → ref_facility_districts mapping
    ↓ ยังว่าง?
Priority 4: home_district จาก homevisit (≥1001, ≤1050)
```

### 6.5 Data Source Tracking

**ทุก record ควรมีตัวแปรบอกว่ามาจาก source ไหน**

| Column ที่เพิ่ม | Type | ค่า | คำอธิบาย |
|---------------|------|-----|---------|
| `data_source` | TEXT | "portal", "app1", "app2" | แหล่งข้อมูลต้นทาง |
| `import_batch_id` | INT | FK → import_history.id | รอบที่ import |
| `created_at` | TIMESTAMP | auto | เวลาที่ import |

**ประโยชน์**:
- ตรวจสอบย้อนกลับได้ว่าข้อมูลมาจากไหน
- เปรียบเทียบคุณภาพข้อมูลระหว่าง source
- ถ้ามีข้อมูลซ้ำ เลือกใช้จาก source ที่ครบกว่า

---

## 7. Derived Variables (ตัวแปรคำนวณ)

### 7.1 ตัวแปรพื้นฐาน (คำนวณตอน Import)

| ตัวแปร | สูตร | เกณฑ์ทางการแพทย์ | ประโยชน์ |
|--------|------|-----------------|---------|
| **อายุจริง** | `CURRENT_YEAR - birth_year` | ใช้ปี ค.ศ. หลังแปลง พ.ศ. | แบ่งกลุ่มอายุ, คำนวณ eGFR |
| **กลุ่มอายุ** | <15=ไม่รวม, 15-21=วัยเรียน, 22-35=วัยเริ่มทำงาน, 36-45=วัยทำงาน, 46-55=วัยกลางคน, 56-64=วัยก่อนสูงอายุ, 65+=สูงวัย | เกณฑ์ สนพ. กทม. | จำแนกกลุ่มเป้าหมาย |
| **BMI** | `WEIGHT / (HEIGHT/100)²` | WHO: <18.5=ผอม, 18.5-22.9=ปกติ, 23-24.9=เกิน, 25-29.9=อ้วน, ≥30=อ้วนมาก | คัดกรองอ้วน, ประเมิน NCD risk |

### 7.2 ตัวแปร Cross-Analysis (คำนวณตอน View Refresh)

| ตัวแปร | สูตร/เงื่อนไข | ประโยชน์ต่อ สนพ. |
|--------|-------------|----------------|
| **Metabolic Syndrome** | ≥3 จาก: รอบเอวเกิน (ชาย≥90, หญิง≥80 cm) + TG≥150 + HDL ต่ำ (ชาย<40, หญิง<50) + BP≥130/85 + FBS≥100 | **คัดกรองกลุ่มเสี่ยงสูง NCD — ส่งต่อรักษาเร่งด่วน** |
| **eGFR Stage** | >90=G1, 60-89=G2, 45-59=G3a, 30-44=G3b, 15-29=G4, <15=G5 | **จำแนกระดับ CKD — จัดสรรทรัพยากรไตเทียม** |
| **PHQ-9 Score** | Σ(Q1-Q9): 0-4=ปกติ, 5-9=เล็กน้อย, 10-14=ปานกลาง, 15-19=ค่อนข้างรุนแรง, 20-27=รุนแรง | **คัดกรองซึมเศร้าเชิงรุก — ส่งต่อจิตแพทย์** |
| **ST-5 Stress Score** | Σ(Q1-Q5): 0-4=น้อย, 5-7=ปานกลาง, 8-9=สูง, 10-12=รุนแรง, ≥13=รุนแรงมาก | **วางแผนสุขภาพจิตชุมชน** |
| **CVD Risk Score** | อายุ>45 + ชาย + สูบบุหรี่ + DM + HPT + LDL>160 | **คัดกรองเร่งด่วนหลอดเลือดหัวใจ** |
| **Anemia Classification** | Hb<13 (ชาย) / <12 (หญิง) + MCV: <80=Microcytic, 80-100=Normocytic, >100=Macrocytic | **จำแนกชนิดโลหิตจาง → เลือกการรักษา** |
| **DM+HPT Comorbidity** | risk_dm=1 AND risk_hpt=1 | **ผู้ป่วยที่ต้องดูแลเข้มข้น — จัดคลินิกเฉพาะ** |
| **Mean Arterial Pressure (MAP)** | DBP + (SBP-DBP)/3 | **ประเมิน organ perfusion** |
| **Pulse Pressure** | SBP - DBP | **>60 เสี่ยงหลอดเลือดแข็ง — ส่ง cardiologist** |
| **Screening Yield** | at_risk / total_screened × 100% per disease per district | **วัดผลผลิตคัดกรอง — จัดสรรงบ** |
| **Coverage Rate** | screened / target_population × 100% per district | **ติดตาม KPI เป้าหมาย 1.6 ล้านคน** |
| **ดัชนีสุขภาพเขต** | 30%×coverage + 25%×NCD_cascade + 20%×lab_completion + 15%×repeat_rate + 10%×satisfaction | **เปรียบเทียบประสิทธิภาพระหว่างเขต — จัดอันดับ** |

### 7.3 ตัวแปรเชิงภูมิศาสตร์

| ตัวแปร | แหล่งข้อมูล | ประโยชน์ต่อ กทม. |
|--------|-----------|----------------|
| **PM2.5 เฉลี่ยเขต** | ArcGIS realtime + สถานีใกล้เคียง | **วิเคราะห์ผลกระทบมลพิษต่อโรคทางเดินหายใจ** |
| **ระยะบ้าน→ศูนย์ฯ** | home_district vs facility district | **ประชาชนข้ามเขตมาตรวจ → จัด mobile unit** |
| **ความหนาแน่นคัดกรอง** | screened / พื้นที่เขต (km²) | **จัดสรร mobile unit ไปเขตที่ยังเข้าไม่ถึง** |
| **Zone Heat Index** | ค่าเฉลี่ยโรคทุกเขตในโซน vs ค่าเฉลี่ย กทม. | **โซนไหนมีปัญหามากกว่าค่าเฉลี่ย → จัดสรรงบเพิ่ม** |

---

## 8. Database Schema Design

### 8.1 ตาราง Raw (7 ตาราง)

```sql
-- ทุกตารางมี:
--   id BIGSERIAL PRIMARY KEY
--   patient_id BIGINT REFERENCES raw_patients(id)
--   data_source TEXT DEFAULT 'portal'  ← บอกว่ามาจาก source ไหน
--   created_at TIMESTAMPTZ DEFAULT NOW()

raw_patients        -- 446K records (Portal + App1 + App2 deduplicated)
raw_visits          -- 398K records (Portal pthistory only)
raw_vitalsigns      -- 480K records (Portal + App1 vitalsignslf)
raw_homevisit       -- 371K records (Portal + App1 homevisit)
raw_homehealth      -- 431K records (Portal + App1 homehealth)
raw_lab_results     -- ~400K records (Portal + App1 labhealth)
raw_lab_extended    -- ~400K records (Portal labhealthext only)
```

### 8.2 ตาราง Reference (4 ตาราง)

```sql
ref_districts             -- 50 เขต กทม. (dcode, name_th, zone_code)
ref_facilities            -- 14K+ สถานพยาบาล (code, lat, lng)
ref_health_zones          -- 8 โซนสุขภาพ
ref_facility_districts    -- facility code → district mapping (backfill)
```

### 8.3 Materialized Views (13 views)

```
summary_district_disease       -- โรครายเขต (50 rows)
summary_district_risk_factors  -- ปัจจัยเสี่ยงรายเขต×เพศ×อายุ
summary_district_lab           -- ผลแลปรายเขต
summary_district_mental        -- สุขภาพจิตรายเขต
summary_district_demographics  -- ประชากรรายเขต
summary_bmi_waist              -- BMI/รอบเอวรายเขต×เพศ
summary_disease_age_sex        -- โรคแยกอายุ×เพศ
summary_comorbidity            -- โรคร่วม (DM+HPT, metabolic)
summary_lab_disease_cross      -- ค่าแลปแยกตามสถานะโรค
summary_facility               -- ผลงานรายสถานพยาบาล
summary_screening_tests        -- EKG/X-ray/ตา/จอประสาทตา
summary_chronic_history        -- โรคเรื้อรัง/วัคซีน
summary_family_history         -- ประวัติครอบครัว
```

### 8.4 Column `data_source` — ติดตามแหล่งข้อมูล

**เพิ่มใน raw tables ทุกตาราง:**

```sql
ALTER TABLE raw_patients ADD COLUMN IF NOT EXISTS data_source TEXT DEFAULT 'portal';
ALTER TABLE raw_vitalsigns ADD COLUMN IF NOT EXISTS data_source TEXT DEFAULT 'portal';
-- ... ทุกตาราง

-- ค่าที่เป็นไปได้:
-- 'portal'  = ระบบหลัก สนพ.
-- 'app1'    = แอปมือถือ สำนักอนามัย
-- 'app2'    = แดชบอร์ดสรุป
-- 'merged'  = ข้อมูลที่ merge จากหลาย source
```

**ประโยชน์**:
1. ตรวจสอบย้อนกลับว่า record มาจากไหน
2. เปรียบเทียบ data quality ระหว่าง source (เช่น App1 มี DISTRICTBKK ว่างกี่ %)
3. ถ้าข้อมูลซ้ำ เลือกใช้จาก source ที่มี column ครบกว่า
4. รายงานผู้บริหาร: "ข้อมูลจาก Portal 80%, App1 15%, App2 5%"
