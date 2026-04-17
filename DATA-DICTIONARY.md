# DATA-DICTIONARY.md — คู่มือ Data Cleansing & Integration

> **โครงการคัดกรองสุขภาพกรุงเทพมหานคร** | สำนักการแพทย์ กรุงเทพมหานคร
> Last Updated: 2026-04-17

---

## สารบัญ

- [1. แหล่งข้อมูล (Data Sources)](#1-แหล่งข้อมูล)
- [2. พจนานุกรมตัวแปร (Variable Dictionary)](#2-พจนานุกรมตัวแปร)
- [3. การเปรียบเทียบตัวแปรระหว่าง Source](#3-การเปรียบเทียบตัวแปรระหว่าง-source)
- [4. วิธีการ Data Cleansing](#4-วิธีการ-data-cleansing)
- [5. วิธีการ Merge Data](#5-วิธีการ-merge-data)
- [6. ตัวแปรคำนวณ (Derived Variables)](#6-ตัวแปรคำนวณ)
- [7. Quality Metrics](#7-quality-metrics)

---

## 1. แหล่งข้อมูล

| Source | ลักษณะ | ไฟล์ | จำนวน columns | หมายเหตุ |
|--------|--------|------|--------------|---------|
| **Portal** | ระบบหลัก สนพ. (สำนักการแพทย์) | pt, pthistory, vitalsignslf, homevisit, homehealth, labhealth, labhealthext (7 ไฟล์) | 12-92 cols/file | ข้อมูลดิบจากศูนย์บริการสาธารณสุข ใช้ PID/IDCARD เข้ารหัส Base64 |
| **App1** | แอปมือถือ สำนักอนามัย | pt, vitalsignslf, homevisit, homehealth, labhealth (5 ไฟล์) | 17-66 cols/file | เป็น subset ของ Portal มีบาง column ต่าง (BRTHDATE vs BIRTHDATE) **ไม่มี labhealthext, pthistory** |
| **App2** | แดชบอร์ดสรุป (pre-aggregated) | app2.csv (1 ไฟล์) | 103 cols | **แตกต่างจาก Portal/App1 มาก** — มี label ภาษาไทยแทน code, มี BMI/AGE_GROUP คำนวณแล้ว, มี lab result interpretation |

### ความแตกต่างหลัก

| มิติ | Portal | App1 | App2 |
|------|--------|------|------|
| ID ผู้ป่วย | `IDCARD` (Base64) | `PID` (Base64) | `PID` (Base64) |
| วันเกิด | `BIRTHDATE` | `BRTHDATE` | ไม่มี (มี `AGE_GROUP`) |
| เพศ | `MALE` (code: 10/20) | `MALE` (code: 10/20) | `MALE` (text: "ชาย"/"หญิง") |
| สถานพยาบาล | `HPTCODE` (code) | `HPTCODE` (code) + `SUBHPT`/`LOCATION` (text) | `HPTCODE` (code) + `HD` (text) |
| เขต | `DISTRICTBKK` (มักว่าง) | ไม่มี | `DISTRICT` (code: 1001-1050) |
| ผล screening | Code (0/1/2) | Code (0/1/2) | Text ("ปกติ"/"ผิดปกติ") |
| BMI | ไม่มี (คำนวณเอง) | ไม่มี (คำนวณเอง) | มี `BMI` + `BMI_GROUP` |
| ผลแลป | ค่าตัวเลขดิบ | ค่าตัวเลขดิบ | ค่าตัวเลข + interpretation text |

---

## 2. พจนานุกรมตัวแปร

### 2.1 pt.csv — ข้อมูลผู้ป่วย

| Column | Portal | App1 | ชนิด | คำอธิบาย | ค่าที่เป็นไปได้ |
|--------|--------|------|------|---------|---------------|
| `IDCARD` | ✅ | ❌ | Base64 string | เลขบัตรประชาชนเข้ารหัส | Base64 → HMAC-SHA256 hash |
| `PID` | ❌ | ✅ | Base64 string | รหัสผู้ป่วย (= IDCARD แต่คนละชื่อ) | Base64 → HMAC-SHA256 hash |
| `NOTYPE` | ✅ | ❌ | int | ประเภทบัตร | 10=บัตร ปชช., 20=passport |
| `PNAME` | ✅ | ✅ | int/float | คำนำหน้า | 11=นาย, 12=นาง, 13=นางสาว, 14=เด็กชาย, 15=เด็กหญิง, 16+=อื่นๆ |
| `FNAME` | ✅ | ✅ | string | ชื่อ (PII) | **ไม่นำเข้า DB** |
| `LNAME` | ✅ | ✅ | string | นามสกุล (PII) | **ไม่นำเข้า DB** |
| `MALE` | ✅ | ✅ | int | เพศ | 10=ชาย, 20=หญิง |
| `BIRTHDATE` | ✅ | ❌ | datetime | วันเกิด | DD/MM/YYYY HH:MM:SS (ปี พ.ศ. ถ้า >2400) |
| `BRTHDATE` | ❌ | ✅ | datetime | วันเกิด (ชื่อต่าง) | DD/MM/YYYY HH:MM:SS |
| `AGE` | ❌ | ✅ | int | อายุ (คำนวณแล้ว) | 0-150 |
| `PHONE` | ❌ | ✅ | string | โทรศัพท์ (PII) | **ไม่นำเข้า DB** |
| `HPTCODE` | ❌ | ✅ | string | รหัสสถานพยาบาล | 3-letter code (cnt, chr, srt...) |
| `LOCATION` | ❌ | ✅ | string | สถานที่ตรวจ (text) | ชื่อเต็มศูนย์ฯ |

### 2.2 vitalsignslf.csv — สัญญาณชีพ + การคัดกรอง

| Column | Portal | App1 | ชนิด | คำอธิบาย | ค่าปกติ | Cleansing rule |
|--------|--------|------|------|---------|--------|---------------|
| `HBPN` | ✅ | ✅ | int | ความดันตัวบน (SBP) | 70-250 mmHg | NULL ถ้า <40 หรือ >300 |
| `LBPN` | ✅ | ✅ | int | ความดันตัวล่าง (DBP) | 40-150 mmHg | NULL ถ้า <20 หรือ >200 |
| `PREFPG` | ✅ | ✅ | float | น้ำตาลก่อนอาหาร (Fasting glucose) | 60-400 mg/dL | NULL ถ้า <0 หรือ >999 |
| `POSTFPG` | ✅ | ✅ | float | น้ำตาลหลังอาหาร | 60-500 mg/dL | NULL ถ้า <0 หรือ >999 |
| `HEIGHT` | ✅ | ✅ | float | ส่วนสูง | 50-250 cm | NULL ถ้า <50 หรือ >250 |
| `WEIGHT` | ✅ | ✅ | float | น้ำหนัก | 10-300 kg | NULL ถ้า <10 หรือ >300 |
| `WSTL` | ✅ | ✅ | float | รอบเอว | 30-200 cm | NULL ถ้า <30 หรือ >200 |
| `PR` | ✅ | ❌ | int | ชีพจร | 40-200 bpm | — |
| `SMOKE` | ✅ | ✅ | int | สถานะสูบบุหรี่ | 0=ไม่สูบ, 1=สูบปัจจุบัน, 2=เคยสูบเลิกแล้ว | — |
| `ALCOHAL` | ✅ | ✅ | int | สถานะดื่มแอลกอฮอล์ | 0=ไม่ดื่ม, 1=ดื่มเป็นประจำ, 2=ดื่มเป็นครั้งคราว, 3=เลิกดื่ม | — |
| `CHEST` | ✅ | ✅ | int | ผล X-ray ปอด | 0=ไม่ได้ตรวจ, 1=ปกติ, 2=ผิดปกติ | — |
| `EKG` | ✅ | ✅ | int | ผล EKG | 0=ไม่ได้ตรวจ, 1=ปกติ, 2=ผิดปกติ | — |
| `VSACT` | ✅ | ✅ | int | การมองเห็น | 0=ไม่ได้ตรวจ, 1=ชัดเจน, 2=ไม่ชัดเจน | — |
| `DRSCN` | ✅ | ✅ | int | ตรวจจอประสาทตา (DR screening) | 0=ไม่ได้ตรวจ, 1=ปกติ, 2=ผิดปกติ | — |
| `SCR2Q1-2` | ✅ | ✅ | int | Depression 2Q (คัดกรองซึมเศร้า) | 0=ไม่มี, 1=มี | — |
| `SCN9Q1-9` | ✅ | ✅ | int | PHQ-9 (9 ข้อ) | 0-3 ต่อข้อ (รวม 0-27) | — |
| `ST501-5` | ✅ | ✅ | int | ST-5 ความเครียด (5 ข้อ) | 0-3 ต่อข้อ (รวม 0-15) | — |
| `SCRRS` | ✅ | ✅ | int | ผลการคัดกรองรวม | 1=ปกติ, 2=ผิดปกติ | — |
| `RISKDM` | ✅ | ✅ | bool | เสี่ยงเบาหวาน | 0/1 | — |
| `RISKHPT` | ✅ | ✅ | bool | เสี่ยงความดัน | 0/1 | — |
| `RISKCDVCL` | ✅ | ✅ | bool | เสี่ยงหลอดเลือดหัวใจ | 0/1 | — |
| `RISKBMI` | ✅ | ✅ | bool | เสี่ยง BMI ผิดปกติ | 0/1 | — |
| `DM` | ✅ | ✅ | bool | พบเบาหวาน | 0/1 | — |
| `HPT` | ✅ | ✅ | bool | พบความดันสูง | 0/1 | — |
| `CDVCL` | ✅ | ✅ | bool | พบโรคหลอดเลือดหัวใจ | 0/1 | — |
| `STROKE` | ✅ | ✅ | bool | พบหลอดเลือดสมอง | 0/1 | — |
| `FAT` | ✅ | ✅ | bool | พบอ้วน | 0/1 | — |
| `CHLTR` | ✅ | ✅ | bool | พบไขมันผิดปกติ | 0/1 | — |
| `DISTRICTBKK` | ✅ | ❌ | string | รหัสเขต กทม. | 1001-1050 | **มักว่าง** → backfill จาก facility mapping |
| `DMFM` | ✅ | ❌ | int | ประวัติเบาหวานครอบครัว | 0=ไม่มี, 1=มี | — |
| `STMNG1-4` | ✅ | ❌ | int[] | วิธีจัดการความเครียด | array of codes | — |

### 2.3 homehealth.csv — พฤติกรรมสุขภาพ + โรคเรื้อรัง

| Column | Portal | App1 | คำอธิบาย | ค่า |
|--------|--------|------|---------|-----|
| `CGTDS` | ✅ | ✅ | มีโรคเรื้อรัง | 0=ไม่มี, 1=มี |
| `DM` | ✅ | ✅ | ประวัติเบาหวาน | 0=ไม่เป็น, 1-3=ระดับ |
| `HPT` | ✅ | ✅ | ประวัติความดัน | 0=ไม่เป็น, 1-3=ระดับ |
| `STROKE` | ✅ | ✅ | ประวัติหลอดเลือดสมอง | 0/1 |
| `CHLTR` | ✅ | ✅ | ประวัติไขมันในเลือด | 0/1 |
| `HRT` | ✅ | ✅ | ประวัติโรคหัวใจ | 0/1 |
| `KIDNEY` | ✅ | ✅ | ประวัติโรคไต | 0/1 |
| `DMRS`-`STROKERS` | ✅ | ❌ | สถานะการรักษา | 1=ไม่รักษา, 2=รักษาไม่สม่ำเสมอ, 3=รักษาสม่ำเสมอ |
| `PARENT` | ✅ | ✅ | ประวัติโรคในครอบครัว | 0=ไม่มี, 1=มี |
| `PDM`-`PEPM` | ✅ | ✅ | พ่อแม่เป็นโรคอะไร | bool (0/1) |
| `EXCERCISE` | ✅ | ✅ | ความถี่ออกกำลังกาย | 1=ไม่ออก, 2=<3 ครั้ง/สัปดาห์, 3=≥3 ครั้ง/สัปดาห์ |
| `FDSW` | ✅ | ✅ | ชอบหวาน | 0/1 (bool) |
| `FDSLT` | ✅ | ✅ | ชอบเค็ม | 0/1 (bool) |
| `FDFAT` | ✅ | ✅ | ชอบมัน | 0/1 (bool) |
| `FOOD` | ✅ | ❌ | ความถี่ทอด/ผัด | 0=ไม่เคย, 1=สัปดาห์ละ, 2=วันเว้นวัน, 3=ทุกวัน |
| `WATER` | ✅ | ❌ | ความถี่น้ำหวาน | 0-3 (เหมือน FOOD) |
| `NOODLE` | ✅ | ❌ | ความถี่บะหมี่กึ่งสำเร็จรูป | 0-3 |
| `COVID` | ✅ | ❌ | ประวัติ COVID | 0=ไม่แน่ใจ, 1=เคยติด, 2=ไม่เคย |
| `VCCCOVID` | ✅ | ❌ | วัคซีน COVID | 0=ไม่เคยฉีด, 1=ฉีดแล้ว |
| `VCCINFLUZA` | ✅ | ❌ | วัคซีนไข้หวัดใหญ่ | 0=ไม่เคยฉีด, 1=ฉีดแล้ว |
| `CHKHIV` | ✅ | ❌ | ต้องการตรวจ HIV | 0/1 |

### 2.4 homevisit.csv — สังคมเศรษฐกิจ

| Column | Portal | App1 | คำอธิบาย | ค่า |
|--------|--------|------|---------|-----|
| `SELFOUR` | ✅ | ✅ | ดูแลตนเอง | 1=ได้, 2=ได้บางส่วน, 3=ไม่ได้ |
| `DISTYPE1-8` | ✅ | ✅ | ประเภทความพิการ | 0/1 per type |
| `EDU` | ✅ | ✅ | การศึกษา | 1=ไม่ได้เรียน, 2=ประถม, 3=มัธยม, 4=ปวช/ปวส, 5=ปริญญาตรี, 6=สูงกว่า ป.ตรี |
| `OCCPTN` | ✅ | ✅ | อาชีพ | 1=ข้าราชการ, 2=รัฐวิสาหกิจ, ... 17-19=อาชีพใหม่ |
| `PROVINCE` | ✅ | ✅ | จังหวัด | 10=กทม., อื่นๆ=ต่างจังหวัด |
| `DISTRICT` | ✅ | ✅ | เขต | 1001-1050 (4 หลัก) **มักว่างใน Portal** |
| `SUBDISTRICT` | ✅ | ✅ | แขวง | 6 หลัก |
| `HOMETYPE` | ✅ | ✅ | ประเภทที่อยู่ | 1=บ้านเดี่ยว, 2=ทาวน์เฮาส์, 3=คอนโด, 4=ห้องเช่า, 5=ชุมชนแออัด |
| `PRVLG` | ✅ | ✅ | สิทธิ์สุขภาพ | 1=บัตรทอง, 2=ประกันสังคม, 3=ข้าราชการ, ... |
| `WRKDISTRICT` | ✅ | ❌ | เขตที่ทำงาน | 1001-1050 |
| `WRKTYPE` | ✅ | ❌ | ลักษณะงาน | 1=ในร่ม, 2=กลางแจ้ง, 3=ผสม |
| `WRKJOURNEY` | ✅ | ❌ | วิธีเดินทาง | 1=รถส่วนตัว, 2=ขนส่งสาธารณะ, 3=มอเตอร์ไซค์, ... |
| `REQUEST1-7` | ✅ | ❌ | สิ่งที่ต้องการ | int[] array |
| `PET`/`DOG`/`CAT` | ✅ | ❌ | สัตว์เลี้ยง | 0/1, จำนวน |

### 2.5 labhealth.csv — ผลตรวจแลป

| Column | Portal | App1 | คำอธิบาย | หน่วย | ค่าปกติ | Cleansing rule |
|--------|--------|------|---------|------|--------|---------------|
| `HMGB` | ✅ | ✅ | Hemoglobin | g/dL | ชาย 13-17, หญิง 12-16 | NULL ถ้า <0 หรือ >30 |
| `HMTC` | ✅ | ✅ | Hematocrit | % | 36-54 | NULL ถ้า <0 หรือ >80 |
| `MCV` | ✅ | ✅ | Mean Corpuscular Volume | fL | 80-100 | NULL ถ้า <0 หรือ >200 |
| `FBS` | ✅ | ✅ | Fasting Blood Sugar | mg/dL | 70-100 | NULL ถ้า <0 หรือ >999 |
| `CHOLEST` | ✅ | ✅ | Total Cholesterol | mg/dL | <200 desirable | NULL ถ้า <0 หรือ >999 |
| `TRIGLY` | ✅ | ✅ | Triglyceride | mg/dL | <150 | NULL ถ้า <0 หรือ >999 |
| `HDL` | ✅ | ✅ | HDL Cholesterol | mg/dL | >40 ชาย, >50 หญิง | NULL ถ้า <0 หรือ >500 |
| `LDL` | ✅ | ✅ | LDL Cholesterol | mg/dL | <100 optimal | NULL ถ้า <0 หรือ >500 |
| `SGOT` | ✅ | ✅ | AST (Liver) | U/L | 10-40 | NULL ถ้า <0 หรือ >999 |
| `SGPT` | ✅ | ✅ | ALT (Liver) | U/L | 7-56 | NULL ถ้า <0 หรือ >999 |
| `URICACID` | ✅ | ✅ | Uric Acid | mg/dL | ชาย 3.4-7.0, หญิง 2.4-6.0 | NULL ถ้า <0 หรือ >50 |
| `CRTININE` | ✅ | ✅ | Creatinine | mg/dL | 0.7-1.3 | NULL ถ้า <0 หรือ >50 |
| `EGFRRS` | ✅ | ✅ | eGFR | mL/min | >90 ปกติ | NULL ถ้า <0 หรือ >200 |
| `BUNRS` | ✅ | ❌ | BUN | mg/dL | 7-20 | NULL ถ้า <0 หรือ >200 |

### 2.6 labhealthext.csv — ผลตรวจเพิ่มเติม (Portal เท่านั้น)

| Column | คำอธิบาย | ค่า |
|--------|---------|-----|
| `SCRRES01-04` | ผลตรวจระบบทางเดินหายใจ (ไอ, หอบ, แน่นหน้าอก, หายใจลำบาก) | 0/1/2 |
| `FGRUB01` | ผลตรวจการได้ยิน | 0=ปกติ, 1=ผิดปกติ |
| `PTGRIGHT`/`PTGLEFT` | ต้อเนื้อ ตาขวา/ซ้าย | 0=ไม่มี, 1=มี |
| `HEAD`-`ANKLE` | อาการปวด 10 ตำแหน่ง (MSD) | bool |
| `SYMP01-04` | อาการร้าว/ชา (neurological) | bool |

### 2.7 app2.csv — ข้อมูลสรุป (pre-processed)

| Column เฉพาะ App2 | คำอธิบาย | หมายเหตุ |
|-------------------|---------|---------|
| `AGE_GROUP` | กลุ่มอายุ (text) | "15-34 ปี", "35-44 ปี", ... "60 ปีขึ้นไป" |
| `BMI` | ค่า BMI คำนวณแล้ว | float |
| `BMI_GROUP` | กลุ่ม BMI (text) | "ผอม", "ปกติสมส่วน", "อ้วนระดับ 1-3" |
| `BP_GROUP` | กลุ่มความดัน (text) | "ปกติ", "สูงเล็กน้อย", "สูง" |
| `*_NAME` columns | Label ภาษาไทยแทน code | เช่น SMOKE_NAME="ไม่สูบ", DM_NAME="ปกติ" |
| `*_SORT` columns | ลำดับสำหรับ sorting | int |
| `*RES` columns | Interpretation ผลแลป | "ปกติ", "ผิดปกติ", "ไม่ได้ตรวจ" |
| `LAB_HEMOGLOBIN` | ค่า Hemoglobin จากแลป | float (อาจต่างจาก HMGB ใน labhealth) |
| `LAB_CHOLESTERAL` | ค่า Cholesterol จากแลป | float |
| `LAB_EGFR` | ค่า eGFR จากแลป | float |
| `HD` | ชื่อสถานพยาบาลเต็ม | text |

---

## 3. การเปรียบเทียบตัวแปรระหว่าง Source

### 3.1 Mapping ชื่อ Column ที่ต่างกัน

| ความหมาย | Portal | App1 | App2 | DB Column |
|---------|--------|------|------|-----------|
| รหัสผู้ป่วย | `IDCARD` | `PID` | `PID` | `idcard_hash` |
| วันเกิด | `BIRTHDATE` | `BRTHDATE` | — | `birth_year` |
| อายุ | — (คำนวณ) | `AGE` | `AGE_GROUP` | `age`, `age_group` |
| เพศ | `MALE` (10/20) | `MALE` (10/20) | `MALE` ("ชาย"/"หญิง") | `sex` |
| ออกกำลังกาย | `EXCERCISE` | `EXCERCISE` | `EXCERCISE` (text) | `exercise` |
| เขต | `DISTRICTBKK` | — | `DISTRICT` | `district_code` |
| eGFR | `EGFRRS` | `EGFR` + `EGFR_LAB` | `LAB_EGFR` | `egfr` |

### 3.2 ไฟล์ที่มีเฉพาะบาง Source

| ไฟล์ | Portal | App1 | App2 |
|------|--------|------|------|
| pthistory.csv | ✅ | ❌ | ❌ |
| labhealthext.csv | ✅ | ❌ | ❌ |
| app2.csv | ❌ | ❌ | ✅ |

---

## 4. วิธีการ Data Cleansing

### 4.1 กฎการ Filter Out (ลบทิ้ง)

| กฎ | เหตุผล | Implementation |
|----|--------|---------------|
| `IDCARD`/`PID` = NULL หรือ hash ไม่ได้ | ไม่สามารถระบุตัวบุคคลได้ | `hash_id()` returns None → skip row |
| `CANCELST` = 1 | record ถูกยกเลิก | WHERE cancel_status IS DISTINCT FROM 1 |
| Duplicate `IDCARD` | ข้อมูลซ้ำ | ON CONFLICT (idcard_hash) DO UPDATE |

### 4.2 กฎการ Fill In / Replace (แทนค่า)

| Field | กฎ | เหตุผล |
|-------|-----|--------|
| `BIRTHDATE` ปี > 2400 | ลบ 543 (แปลง พ.ศ. → ค.ศ.) | ข้อมูลบางส่วนใช้ปี พ.ศ. |
| `BIRTHDATE` ปี < 1900 หรือ > 2030 | → NULL | ค่าผิดปกติ |
| อายุ < 0 หรือ > 150 | → NULL | เป็นไปไม่ได้ทางชีวภาพ |
| `HEIGHT` < 50 หรือ > 250 cm | → NULL | นอกช่วงมนุษย์ปกติ |
| `WEIGHT` < 10 หรือ > 300 kg | → NULL | นอกช่วงมนุษย์ปกติ |
| `WSTL` < 30 หรือ > 200 cm | → NULL | นอกช่วงมนุษย์ปกติ |
| BMI > 80 | → NULL | ค่า BMI ผิดปกติ (สูงสุดจริงๆ ≈70) |
| `FBS` / glucose < 0 หรือ > 999 | → NULL | เครื่องวัดไม่ได้ให้ค่าเกินนี้ |
| `HMGB` < 0 หรือ > 30 g/dL | → NULL | ค่าเลือดผิดปกติ |
| `CRTININE` < 0 หรือ > 50 | → NULL | ค่า creatinine ผิดปกติ |
| `CHOLEST` / `TRIGLY` < 0 หรือ > 999 | → NULL | ค่าแลปเกินช่วง |
| `URICACID` < 0 หรือ > 50 | → NULL | ค่า uric acid ผิดปกติ |
| Integer overflow (> 2,147,483,647) | → NULL | ข้อมูลเสีย |
| Float = inf / NaN | → NULL | ข้อมูลเสีย |
| `DISTRICTBKK` ว่าง | Backfill จาก `ref_facility_districts` | ใช้ facility code → district mapping |

### 4.3 กฎเฉพาะ App2

| Field | กฎ | เหตุผล |
|-------|-----|--------|
| `MALE` = "ชาย" | → 10 | แปลง text → code |
| `MALE` = "หญิง" | → 20 | แปลง text → code |
| `*_NAME` columns | ไม่นำเข้า (ใช้ code แทน) | ป้องกัน data inconsistency |
| `*_SORT` columns | ไม่นำเข้า | metadata สำหรับ UI เท่านั้น |
| `BMI` จาก App2 | ตรวจสอบ vs คำนวณจาก HEIGHT/WEIGHT | ใช้ค่าที่ตรงกันทั้งสอง |

### 4.4 Error Handling ระดับ Row

เมื่อ INSERT ล้มเหลว (type overflow, truncation):
1. **SAVEPOINT** ก่อน batch insert
2. ถ้า batch ล้มเหลว → **ROLLBACK TO SAVEPOINT**
3. Retry ทีละ row → **skip row ที่พัง** แทนที่จะ fail ทั้ง import
4. Log จำนวน row ที่ skip

---

## 5. วิธีการ Merge Data

### 5.1 ลำดับการนำเข้า (Import Order)

```
1. pt.csv (ผู้ป่วย)        → raw_patients     ← ต้องมาก่อน เพราะตารางอื่นอ้างอิง patient_id
2. pthistory.csv (ประวัติ)   → raw_visits
3. vitalsignslf.csv (สัญญาณชีพ) → raw_vitalsigns
4. homevisit.csv (สังคม)     → raw_homevisit
5. homehealth.csv (พฤติกรรม) → raw_homehealth
6. labhealth.csv (ผลแลป)    → raw_lab_results
7. labhealthext.csv (MSD)   → raw_lab_extended
```

### 5.2 Patient Matching (การจับคู่ผู้ป่วย)

| Source | ID Field | วิธี Match |
|--------|----------|----------|
| Portal | `IDCARD` | Base64 decode → HMAC-SHA256 hash → `idcard_hash` |
| App1 | `PID` | Base64 decode → HMAC-SHA256 hash → `idcard_hash` |
| App2 | `PID` | Base64 decode → HMAC-SHA256 hash → `idcard_hash` |

**ทั้ง 3 source ใช้ hashing algorithm เดียวกัน** → ผู้ป่วยคนเดียวกันจะได้ hash เดียวกัน → ON CONFLICT DO UPDATE

### 5.3 Visit Matching (การจับคู่ครั้งที่มาตรวจ)

- Join ผ่าน `patient_id` (FK → raw_patients.id)
- ถ้าผู้ป่วยมาจาก App1 แต่ยังไม่มีใน raw_patients → **auto-register** (สร้าง patient record ใหม่)
- ข้อมูลจากหลาย source สำหรับผู้ป่วยเดียวกัน → เก็บทุก visit (ON CONFLICT DO NOTHING)

### 5.4 District Backfill Pipeline

```
1. DISTRICTBKK จาก vitalsignslf.csv               → ใช้ตรง (ถ้ามี)
2. facility_code → ref_facility_districts mapping   → backfill ถ้า DISTRICTBKK ว่าง
3. home_district จาก homevisit (≥1001, ≤1050)       → backfill ถ้ายังว่าง
4. current_district จาก homevisit                   → fallback สุดท้าย
```

### 5.5 Bundle Upload Behavior

เมื่ออัปโหลดทุกไฟล์พร้อมกัน:
1. **TRUNCATE raw_patients CASCADE** → ลบข้อมูลเดิมทั้งหมด
2. Import ตามลำดับ (pt → pthistory → ... → labhealthext)
3. **Commit** ข้อมูลก่อน (ป้องกัน rollback)
4. Backfill district_code
5. Refresh 13 materialized views
6. Flush Redis cache + in-memory cache

---

## 6. ตัวแปรคำนวณ (Derived Variables)

### 6.1 ตัวแปรพื้นฐาน

| ตัวแปร | สูตร | เกณฑ์ทางการแพทย์ |
|--------|------|-----------------|
| **อายุจริง** | `CURRENT_YEAR - birth_year` (แปลง พ.ศ. ก่อน) | ใช้ในการแบ่งกลุ่มอายุ |
| **กลุ่มอายุ** | <15=ไม่รวม, 15-21=วัยเรียน, 22-35=วัยเริ่มทำงาน, 36-45=วัยทำงาน, 46-55=วัยกลางคน, 56-64=วัยก่อนสูงอายุ, 65+=สูงวัย | เกณฑ์ สนพ. กทม. |
| **BMI** | `weight_kg / (height_cm / 100)²` | เกณฑ์ WHO: <18.5=ผอม, 18.5-22.9=ปกติ, 23-24.9=เกิน, 25-29.9=อ้วน, ≥30=อ้วนมาก |
| **ความดันเฉลี่ย (MAP)** | `DBP + (SBP - DBP) / 3` | ≥60 ปกติ, <60 ช็อค |
| **Pulse Pressure** | `SBP - DBP` | >40 ปกติ, >60 เสี่ยงหลอดเลือด |

### 6.2 ตัวแปร Cross-Analysis (มีประโยชน์ต่อสำนักการแพทย์)

| ตัวแปร | สูตร/เงื่อนไข | ประโยชน์ |
|--------|-------------|---------|
| **Metabolic Syndrome** | ≥3 จาก: รอบเอวเกิน (M≥90, F≥80) + TG≥150 + HDL ต่ำ (M<40, F<50) + BP≥130/85 + FBS≥100 | คัดกรองกลุ่มเสี่ยงสูง NCD |
| **eGFR Stage** | >90=G1 ปกติ, 60-89=G2 ลดเล็กน้อย, 45-59=G3a, 30-44=G3b, 15-29=G4, <15=G5 | จำแนกระดับโรคไตเรื้อรัง |
| **DM + HPT Comorbidity** | risk_dm=1 AND risk_hpt=1 | ผู้ป่วยเบาหวาน+ความดัน ต้องการดูแลเข้มข้น |
| **Depression Score (PHQ-9)** | ΣQ1-Q9 (0-27) → 0-4=ปกติ, 5-9=เล็กน้อย, 10-14=ปานกลาง, 15-19=ค่อนข้างรุนแรง, 20-27=รุนแรง | คัดกรองสุขภาพจิตเชิงรุก |
| **Stress Score (ST-5)** | ΣQ1-Q5 (0-15) → 0-4=น้อย, 5-7=ปานกลาง, 8-9=สูง, 10-12=รุนแรง, 13-15=รุนแรงมาก | ประเมินความเครียดชุมชน |
| **CVD Risk Score** | อายุ >45 + ชาย + สูบบุหรี่ + DM + HPT + LDL>160 | คัดกรองเร่งด่วนหลอดเลือดหัวใจ |
| **Anemia Classification** | Hb<13 (M) / <12 (F) + MCV → <80=Microcytic, 80-100=Normocytic, >100=Macrocytic | จำแนกชนิดโลหิตจาง |
| **ดัชนีสุขภาพเขต** | Weighted: 30%×coverage + 25%×NCD_cascade + 20%×lab_completion + 15%×repeat_rate + 10%×satisfaction | เปรียบเทียบประสิทธิภาพระหว่างเขต |
| **Screening Yield** | at_risk / total_screened × 100% per disease | วัดผลผลิตการคัดกรอง |
| **Coverage Rate** | screened / target_population × 100% per district | ติดตาม KPI ครอบคลุม |

### 6.3 ตัวแปรเชิงภูมิศาสตร์ (GIS)

| ตัวแปร | แหล่งข้อมูล | ประโยชน์ |
|--------|-----------|---------|
| **PM2.5 เฉลี่ยเขต** | ArcGIS realtime + สถานีใกล้เคียง | วิเคราะห์ผลกระทบมลพิษต่อโรคระบบทางเดินหายใจ |
| **ระยะทางบ้าน→ศูนย์ฯ** | home_district vs facility district | วิเคราะห์ accessibility — ประชาชนต้องข้ามเขตมาตรวจไหม |
| **ความหนาแน่นคัดกรอง** | screened / พื้นที่เขต (km²) | จัดสรร mobile unit ไปเขตที่ยังเข้าไม่ถึง |

---

## 7. Quality Metrics

### 7.1 Completeness (ความครบถ้วน)

| Field | คาดหวัง | ปัญหาที่พบ |
|-------|---------|-----------|
| IDCARD/PID | 100% | ข้อมูลที่ hash ไม่ได้จะถูก skip |
| BIRTHDATE | >95% | บางส่วนใช้ พ.ศ., บางส่วนว่าง |
| MALE (เพศ) | >99% | แทบไม่มีค่าว่าง |
| HEIGHT/WEIGHT | ~70% | ไม่ได้วัดทุกคน |
| DISTRICTBKK | <5% (Portal), ~60% (App2) | **ปัญหาหลัก** — ต้อง backfill |
| FBS | ~50% | ต้องอดอาหาร — ไม่ได้ตรวจทุกคน |
| Cholesterol/TG/HDL/LDL | ~40% | ต้องเจาะเลือด — ไม่ได้ตรวจทุกคน |
| PHQ-9 | ~80% | คัดกรองซึมเศร้าทุกคน แต่บางคนไม่ตอบ |

### 7.2 ปริมาณข้อมูลต่อ Source

| Source | ผู้ป่วย | Records รวม | % ของทั้งหมด |
|--------|---------|------------|-------------|
| Portal | ~446K | ~3.28M (7 files) | ~80% |
| App1 | TBD | TBD (5 files) | ~15% |
| App2 | TBD | TBD (1 file) | ~5% |

### 7.3 สิ่งที่ต้องระวัง

1. **ข้อมูลซ้ำ**: ผู้ป่วยคนเดียวกันอาจอยู่ทั้ง Portal และ App1 → ใช้ hash dedup
2. **Visit ซ้ำ**: คนเดียวกันมาตรวจหลายครั้ง → ON CONFLICT DO NOTHING (เก็บทุกครั้ง)
3. **ค่า outlier**: BMI=500, อายุ=300, BP=9999 → NULL ด้วย safe_int/safe_float
4. **ปี พ.ศ./ค.ศ.**: BIRTHDATE อาจมาเป็น พ.ศ. (>2400) → ลบ 543
5. **DISTRICT ว่าง**: vitalsignslf ส่วนใหญ่ไม่มี DISTRICTBKK → backfill จาก facility mapping
6. **App2 format ต่าง**: ใช้ text label แทน code → ต้อง map กลับเป็น code ก่อน merge
7. **Lab ไม่ครบ**: FBS, Cholesterol ตรวจไม่ทุกคน → missing ≠ ปกติ
