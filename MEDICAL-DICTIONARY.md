# ⚠️ ARCHIVED — see `bma-med/MED-FACTSHEET.md` + `bma-med/CODES_REFERENCE.md`

> **As of Sprint S1 (2026-05) this document is preserved for historical
> reference only.** Canonical sources are `bma-med/MED-FACTSHEET.md` (TOR)
> and `bma-med/CODES_REFERENCE.md` (auto-generated value→Thai-label
> reference). The cleaner reads the factsheet at module load — the
> factsheet is the runtime contract. Cross-walk in
> `bma-health-db/CUTOVER-PLAN.md`.

---

# MEDICAL-DICTIONARY.md (archived) — พจนานุกรมตัวแปรทางการแพทย์ฉบับเต็ม

> **โครงการคัดกรองสุขภาพกรุงเทพมหานคร** | สำนักการแพทย์ กรุงเทพมหานคร
> Last Updated: 2026-04-17

---

## สรุปภาพรวม

| | Portal | App1 | App2 | Merged Total |
|---|--------|------|------|-------------|
| **จำนวนไฟล์** | 7 | 5 | 1 | — |
| **จำนวน columns (unique)** | **314** | **162** | **103** | **380** |

### Venn Diagram — การทับซ้อนของตัวแปร

```
                    Portal (314)
                   ╱          ╲
           ┌──149──┐    ┌──8──┐
           │Portal │    │P+A2 │
           │ only  │    │     │
           └───────┘    └─────┘
              ╱    ╲       │
        ┌──123──┐ ┌──34──┐
        │ P+A1  │ │ ALL  │
        │ only  │ │THREE │
        └───────┘ └──────┘
              ╲       │
           ┌──5──┐  ┌──0──┐
           │ A1  │  │A1+A2│
           │only │  │     │
           └─────┘  └─────┘
                ╲      │
              ┌──61──┐
              │ A2   │
              │ only │
              └──────┘

Portal ∩ App1 ∩ App2:  34 columns (ทั้ง 3 มี)
Portal ∩ App1 only:   123 columns
Portal ∩ App2 only:     8 columns
App1 ∩ App2 only:       0 columns
Portal only:          149 columns
App1 only:              5 columns
App2 only:             61 columns
────────────────────────────────
TOTAL UNIQUE:         380 columns
```

---

## ตัวแปรที่ทั้ง 3 Source มี (34 columns)

ตัวแปรเหล่านี้ merge ได้โดยตรง ไม่ต้องแปลง

| # | Column | ไฟล์ (Portal) | ความหมาย | Type | ค่าตัวอย่าง | หมายเหตุ |
|---|--------|-------------|---------|------|-----------|---------|
| 1 | `BLDSGRS` | labhealth | ผลตรวจน้ำตาลในเลือด | int | 1 | 0=ไม่ตรวจ, 1=ปกติ, 2=ผิดปกติ |
| 2 | `CANCELDATE` | หลายไฟล์ | วันที่ยกเลิก | datetime | — | — |
| 3 | `CBCRS` | labhealth | ผล CBC | int | 1 | 0=ไม่ตรวจ, 1=ปกติ, 2=ผิดปกติ |
| 4 | `CDVCL` | vitalsignslf | พบโรคหลอดเลือดหัวใจ | bool | 0/1 | **App2 ใช้ text ใน CDVCL_NAME** |
| 5 | `CGTDS` | homehealth | มีโรคเรื้อรัง | int | 2 | 0=ไม่มี, 1=มี, 2=ไม่ทราบ |
| 6 | `CHEST` | vitalsignslf | ผล X-ray ปอด | int | 0 | 0=ไม่ตรวจ, 1=ปกติ, 2=ผิดปกติ |
| 7 | `CHLTR` | vitalsignslf/homehealth | พบ/ประวัติไขมันผิดปกติ | bool | 0/1 | — |
| 8 | `CHLTRRS` | labhealth/homehealth | ผลตรวจ/สถานะรักษาไขมัน | int | 1 | ใช้ต่างกัน: lab=ผลตรวจ, health=สถานะรักษา |
| 9 | `CLCRS` | labhealth | ผลตรวจมะเร็งลำไส้ใหญ่ | int | 0 | 0=ไม่ตรวจ, 1=ปกติ, 2=ผิดปกติ |
| 10 | `CVCRS` | labhealth | ผลตรวจมะเร็งปากมดลูก | int | 0 | 0=ไม่ตรวจ, 1=ปกติ, 2=ผิดปกติ |
| 11 | `DISTYPE1`-`8` | homevisit | ประเภทความพิการ 1-8 | int | 0/1 | — |
| 12 | `DM` | vitalsignslf/homehealth | พบ/ประวัติเบาหวาน | bool/int | 0/1 | — |
| 13 | `EGFR` | labhealth | ค่า eGFR | float | — | Portal=EGFRRS, App1=EGFR, App2=LAB_EGFR |
| 14 | `EKG` | vitalsignslf | ผล EKG | int | 0 | 0=ไม่ตรวจ, 1=ปกติ, 2=ผิดปกติ |
| 15 | `EXCERCISE` | homehealth | ความถี่ออกกำลังกาย | int | 3 | 1=ไม่ออก, 2=<3ครั้ง/สัปดาห์, 3=≥3ครั้ง |
| 16 | `FAT` | vitalsignslf | พบอ้วน | bool | 0/1 | — |
| 17 | `FIRSTDATE` | หลายไฟล์ | วันที่สร้าง record | datetime | — | — |
| 18 | `HEALTHUSE` | homevisit | สถานพยาบาลที่ใช้ | int | — | Portal only has full detail |
| 19 | `HPT` | vitalsignslf/homehealth | พบ/ประวัติความดันสูง | bool/int | 0/1 | — |
| 20 | `HPTCODE` | หลายไฟล์ | รหัสสถานพยาบาล | string | cnt, chr | 3 ตัวอักษร |
| 21 | `HRT` | homehealth | ประวัติโรคหัวใจ | bool | 0/1 | — |
| 22 | `KIDNEY` | homehealth | ประวัติโรคไต | bool | 0/1 | — |
| 23 | `LIVERRS` | labhealth | ผลตรวจตับ | int | 0 | 0=ไม่ตรวจ, 1=ปกติ, 2=ผิดปกติ |
| 24 | `MALE` | pt | เพศ | int/**string** | 10/20 หรือ "ชาย"/"หญิง" | **App2 ใช้ text!** |
| 25 | `OTH` | vitalsignslf | พบโรคอื่น | bool | 0/1 | — |
| 26 | `PARENT` | homehealth | มีประวัติโรคครอบครัว | int | 0/1 | — |
| 27 | `PDM`-`PGOUT` | homehealth | พ่อแม่เป็นโรคเฉพาะ | bool | 0/1 | PDM=เบาหวาน, PHPT=ความดัน ฯลฯ |
| 28 | `PID` | หลายไฟล์ | รหัสผู้ป่วย | Base64 | MzEw... | = IDCARD (Portal ใช้ IDCARD แทน) |
| 29 | `RISKBMI` | vitalsignslf | เสี่ยง BMI ผิดปกติ | bool | 0/1 | — |
| 30 | `RISKCDVCL` | vitalsignslf | เสี่ยงหลอดเลือดหัวใจ | bool | 0/1 | — |
| 31 | `RISKDM` | vitalsignslf | เสี่ยงเบาหวาน | bool | 0/1 | — |
| 32 | `RISKHPT` | vitalsignslf | เสี่ยงความดัน | bool | 0/1 | — |
| 33 | `STROKE` | vitalsignslf/homehealth | พบ/ประวัติหลอดเลือดสมอง | bool | 0/1 | — |
| 34 | `UARS` | labhealth | ผลตรวจปัสสาวะ | int | 0 | 0=ไม่ตรวจ, 1=ปกติ, 2=ผิดปกติ |
| 35 | `URICRS` | labhealth | ผลตรวจกรดยูริก | int | 0 | 0=ไม่ตรวจ, 1=ปกติ, 2=ผิดปกติ |
| 36 | `VCCCOVID` | homehealth | วัคซีน COVID | int | 0/1 | — |
| 37 | `VSTDATE` | หลายไฟล์ | วันที่มาตรวจ | datetime | 22/04/2025 | — |
| 38 | `WRKDISTRICT` | homevisit | เขตที่ทำงาน | int | 1024 | App2 มี, Portal มี, App1 ไม่มี |

---

## ตัวแปรที่ Portal + App1 มีร่วมกัน (123 columns เพิ่มเติม)

App2 ไม่มี — ต้อง merge จาก Portal/App1 เข้า DB

### สัญญาณชีพ (Vitals) — 10 columns

| Column | ความหมาย | Type | หน่วย | ค่าปกติ | Clinical Range | ถ้านอก Range |
|--------|---------|------|------|--------|---------------|------------|
| `HBPN` | ความดันตัวบน (SBP) | int | mmHg | 90-140 | 40-300 | → NULL |
| `LBPN` | ความดันตัวล่าง (DBP) | int | mmHg | 60-90 | 20-200 | → NULL |
| `PREFPG` | น้ำตาลก่อนอาหาร (Fasting glucose) | float | mg/dL | 70-100 | 0-999 | → NULL |
| `POSTFPG` | น้ำตาลหลังอาหาร | float | mg/dL | 70-140 | 0-999 | → NULL |
| `HEIGHT` | ส่วนสูง | float | cm | 150-180 | 50-250 | → NULL |
| `WEIGHT` | น้ำหนัก | float | kg | 40-80 | 10-300 | → NULL |
| `WSTL` | รอบเอว | float | cm | 60-90 | 30-200 | → NULL (**พบ 0.0 ในข้อมูลจริง**) |
| `SMOKE` | สูบบุหรี่ | int | — | — | 0=ไม่สูบ, 1=สูบ, 2=เคยสูบเลิกแล้ว | — |
| `ALCOHAL` | ดื่มแอลกอฮอล์ | int | — | — | 0=ไม่ดื่ม, 1=ประจำ, 2=บางครั้ง, 3=เลิก | — |
| `SCRRS` | ผลคัดกรองรวม | int | — | — | 1=ปกติ, 2=ผิดปกติ | — |

### สุขภาพจิต — 16 columns

| Column | ความหมาย | Range | การให้คะแนน |
|--------|---------|-------|-----------|
| `SCR2Q1` | Depression 2Q ข้อ 1: รู้สึกเบื่อหน่าย | 0-1 | 0=ไม่มี, 1=มี |
| `SCR2Q2` | Depression 2Q ข้อ 2: รู้สึกหมดหวัง | 0-1 | 0=ไม่มี, 1=มี |
| `SCN9Q1` | PHQ-9 ข้อ 1: เบื่อ ไม่สนใจ | 0-3 | 0=ไม่เลย, 1=หลายวัน, 2=>ครึ่ง, 3=เกือบทุกวัน |
| `SCN9Q2` | PHQ-9 ข้อ 2: หดหู่ ท้อแท้ | 0-3 | เหมือนข้อ 1 |
| `SCN9Q3` | PHQ-9 ข้อ 3: นอนไม่หลับ/หลับมาก | 0-3 | |
| `SCN9Q4` | PHQ-9 ข้อ 4: เหนื่อยง่าย ไม่มีแรง | 0-3 | |
| `SCN9Q5` | PHQ-9 ข้อ 5: เบื่ออาหาร/กินมาก | 0-3 | |
| `SCN9Q6` | PHQ-9 ข้อ 6: รู้สึกไม่ดีกับตัวเอง | 0-3 | |
| `SCN9Q7` | PHQ-9 ข้อ 7: สมาธิไม่ดี | 0-3 | |
| `SCN9Q8` | PHQ-9 ข้อ 8: เชื่องช้า/กระวนกระวาย | 0-3 | |
| `SCN9Q9` | PHQ-9 ข้อ 9: คิดทำร้ายตัวเอง | 0-3 | **⚠️ ข้อสำคัญ — ถ้า ≥1 ต้องส่งต่อ** |
| `ST501` | ST-5 ข้อ 1: นอนไม่หลับ | 0-3 | 0=ไม่เลย ... 3=เป็นประจำ |
| `ST502` | ST-5 ข้อ 2: หงุดหงิดง่าย | 0-3 | |
| `ST503` | ST-5 ข้อ 3: ทำอะไรไม่ถูก | 0-3 | |
| `ST504` | ST-5 ข้อ 4: ไม่อยากพบคน | 0-3 | |
| `ST505` | ST-5 ข้อ 5: เบื่อหน่ายท้อแท้ | 0-3 | |

### ผลตรวจตา — 2 columns (Portal+App1)

| Column | ความหมาย | ค่า |
|--------|---------|-----|
| `VSACT` | การมองเห็น | 0=ไม่ตรวจ, 1=ชัดเจน, 2=ไม่ชัดเจน |
| `DRSCN` | ตรวจจอประสาทตา (DR screening) | 0=ไม่ตรวจ, 1=ปกติ, 2=ผิดปกติ |

### ผลแลปตัวเลข — 16 columns

| Column | ความหมาย | หน่วย | ค่าปกติ | Range | ถ้านอก |
|--------|---------|------|--------|-------|------|
| `WBC` | เม็ดเลือดขาว | cells/µL | 4,500-11,000 | 0-999,999 | → NULL |
| `RBC` | เม็ดเลือดแดง | M/µL | 4.5-5.5 | 0-999,999 | → NULL |
| `HMGB` | Hemoglobin | g/dL | M:13-17, F:12-16 | 0-30 | → NULL |
| `HMTC` | Hematocrit | % | 36-54 | 0-80 | → NULL |
| `MCV` | Mean Corpuscular Volume | fL | 80-100 | 0-200 | → NULL |
| `PITCNT` | Platelet count | K/µL | 150-400 | 0-9,999,999 | → NULL |
| `DTX` | Dextrostix | mg/dL | 70-110 | 0-999 | → NULL |
| `BLDSUGAR` | น้ำตาลในเลือด | mg/dL | 70-100 | 0-999 | → NULL |
| `FBS` | Fasting Blood Sugar | mg/dL | 70-100 | 0-999 | → NULL |
| `CHOLEST` | Total Cholesterol | mg/dL | <200 | 0-999 | → NULL |
| `TRIGLY` | Triglyceride | mg/dL | <150 | 0-999 | → NULL |
| `HDL` | HDL Cholesterol | mg/dL | M:>40, F:>50 | 0-500 | → NULL |
| `LDL` | LDL Cholesterol | mg/dL | <100 | 0-500 | → NULL |
| `SGOT` | AST (ตับ) | U/L | 10-40 | 0-999 | → NULL |
| `SGPT` | ALT (ตับ) | U/L | 7-56 | 0-999 | → NULL |
| `URICACID` | กรดยูริก | mg/dL | M:3.4-7, F:2.4-6 | 0-50 | → NULL |
| `CRTININE` | Creatinine | mg/dL | 0.7-1.3 | 0-50 | → NULL |
| `ALKPPT` | Alk Phosphatase | U/L | 44-147 | 0-999 | → NULL |

### สังคมเศรษฐกิจ — 6 columns

| Column | ความหมาย | ค่า |
|--------|---------|-----|
| `SELFOUR` | ดูแลตนเอง | 1=ได้, 2=บางส่วน, 3=ไม่ได้ |
| `EDU` | การศึกษา | 1=ไม่เรียน, 2=ประถม, 3=มัธยม, 4=ปวช/ปวส, 5=ป.ตรี, 6=สูงกว่า |
| `OCCPTN` | อาชีพ | 1=ข้าราชการ, 2=รัฐวิสาหกิจ, ... 17-19=ใหม่ |
| `PROVINCE` | จังหวัด | 10=กทม. |
| `DISTRICT` | เขต | 1001-1050 (**มักว่างใน Portal**) |
| `HOMETYPE` | ที่อยู่ | 1=บ้านเดี่ยว, 2=ทาวน์เฮาส์, 3=คอนโด, 4=ห้องเช่า, 5=ชุมชนแออัด |

---

## ตัวแปรเฉพาะ Portal (149 columns)

App1 และ App2 ไม่มี — ข้อมูลหายถ้ามาจาก source อื่น

### การส่งต่อผู้ป่วย (Referral) — 14 columns

| Column | ความหมาย |
|--------|---------|
| `CSSLF` | ดูแลตนเอง (self-care result) |
| `CSREFER` | ส่งต่อ (referral result) |
| `CSOTH` | อื่นๆ |
| `RFNON` | ไม่ส่งต่อ |
| `RFPRVLG` | ส่งต่อตามสิทธิ์ |
| `RFOVER` | ส่งต่อเกินสิทธิ์ |
| `RFFW` | ส่งต่อตาม follow-up |
| `RFSPC` | ส่งต่อเฉพาะทาง |
| `RFOTH` | ส่งต่ออื่นๆ |

### พฤติกรรมอาหาร — 3 columns

| Column | ความหมาย | ค่า |
|--------|---------|-----|
| `FOOD` | ความถี่ทอด/ผัด | 0=ไม่เคย, 1=สัปดาห์ละ, 2=วันเว้นวัน, 3=ทุกวัน |
| `WATER` | ความถี่น้ำหวาน | 0-3 เหมือน FOOD |
| `NOODLE` | ความถี่บะหมี่กึ่งสำเร็จรูป | 0-3 เหมือน FOOD |

### วัคซีน + โรคเพิ่มเติม — 8 columns

| Column | ความหมาย |
|--------|---------|
| `VCCINFLUZA` | วัคซีนไข้หวัดใหญ่ |
| `CHKHIV` | ต้องการตรวจ HIV |
| `ASTH` / `ASTHRS` | หอบหืด / สถานะรักษา |
| `EMPHY` / `EMPHYRS` | ถุงลมโป่งพอง / สถานะรักษา |
| `EPLPY` / `EPLYRS` | ลมชัก / สถานะรักษา |

### สถานะการรักษา — 6 columns

| Column | ความหมาย | ค่า |
|--------|---------|-----|
| `DMRS` | การรักษาเบาหวาน | 1=ไม่รักษา, 2=ไม่สม่ำเสมอ, 3=สม่ำเสมอ |
| `HPTRS` | การรักษาความดัน | เหมือน DMRS |
| `CHLTRRS` | การรักษาไขมัน | เหมือน DMRS |
| `HRTRS` | การรักษาโรคหัวใจ | เหมือน DMRS |
| `KIDNEYRS` | การรักษาโรคไต | เหมือน DMRS |
| `STROKERS` | การรักษาหลอดเลือดสมอง | เหมือน DMRS |

### สัตว์เลี้ยง — 7 columns

| Column | ความหมาย |
|--------|---------|
| `PET` | มีสัตว์เลี้ยง (0/1) |
| `DOG` / `DOGAMT` | มีสุนัข / จำนวน |
| `CAT` / `CATAMT` | มีแมว / จำนวน |
| `AMLOTH` / `AMLNAME` / `AMLAMT` | สัตว์อื่น / ชื่อ / จำนวน |

### ที่อยู่ละเอียด (PII — ไม่นำเข้า DB)

| Column | ความหมาย | **ไม่นำเข้า** |
|--------|---------|-------------|
| `HADDR` | บ้านเลขที่ | PII |
| `HMOO` | หมู่ | PII |
| `HSOI` | ซอย | PII |
| `HSTREET` | ถนน | PII |
| `HZIPCODE` | รหัสไปรษณีย์ | PII |

### MSD — ระบบกล้ามเนื้อ (labhealthext.csv) — 22 columns

| Column | ความหมาย | Type |
|--------|---------|------|
| `SCRRES01` | ไอ (Cough) | 0/1/2 |
| `SCRRES02` | หอบเหนื่อย (Dyspnea) | 0/1/2 |
| `SCRRES03` | แน่นหน้าอก (Chest tightness) | 0/1/2 |
| `SCRRES04` | หายใจลำบาก (Breathing difficulty) | 0/1/2 |
| `FGRUB01` | การได้ยิน (Hearing) | 0=ปกติ, 1=ผิดปกติ |
| `PTGRIGHT` | ต้อเนื้อตาขวา (Pterygium R) | 0/1 |
| `PTGLEFT` | ต้อเนื้อตาซ้าย (Pterygium L) | 0/1 |
| `HEAD` | ปวดศีรษะ | bool |
| `NECK` | ปวดคอ | bool |
| `SHLDR` | ปวดไหล่ | bool |
| `UPBH` | ปวดหลังส่วนบน | bool |
| `ELBOW` | ปวดข้อศอก | bool |
| `LWBH` | ปวดหลังส่วนล่าง | bool |
| `WRIST` | ปวดข้อมือ | bool |
| `HIP` | ปวดสะโพก | bool |
| `KNEE` | ปวดเข่า | bool |
| `ANKLE` | ปวดข้อเท้า | bool |
| `SYMP01` | ปวดร้าลงขา (Neck radiating) | bool |
| `SYMP02` | มือชา (Hand numbness) | bool |
| `SYMP03` | ปวดร้าวหลัง (Back radiating) | bool |
| `SYMP04` | ปวดส้นเท้า (Heel pain) | bool |

---

## ตัวแปรเฉพาะ App1 (5 columns)

| Column | ไฟล์ | ความหมาย | หมายเหตุ |
|--------|------|---------|---------|
| `AGE` | pt.csv | อายุ (คำนวณแล้ว) | Portal ต้องคำนวณเอง |
| `BRTHDATE` | pt.csv | วันเกิด | = `BIRTHDATE` ใน Portal (ชื่อต่าง) |
| `PNAMEOTH` | pt.csv | คำนำหน้าอื่นๆ | text |
| `EGFR_LAB` | labhealth.csv | ค่า eGFR จากห้องแลป | Portal ใช้ `EGFRRS` |
| `SUBHPT` | pt.csv | สถานพยาบาลย่อย | text (ชื่อเต็ม) |

---

## ตัวแปรเฉพาะ App2 (61 columns)

### Label ภาษาไทย (22 columns) — ไม่นำเข้า DB (ใช้ code แทน)

| Column | แปลจาก code | ตัวอย่าง |
|--------|-----------|---------|
| `SMOKE_NAME` | `SMOKE` | "ไม่สูบ", "สูบ", "เคยสูบเลิกแล้ว" |
| `ALCOHAL_NAME` | `ALCOHAL` | "ไม่ดื่ม", "ดื่มเป็นประจำ" |
| `DM_NAME` | `DM` (risk) | "ปกติ", "เสี่ยง", "เป็น" |
| `HPT_NAME` | `HPT` (risk) | "ปกติ", "เสี่ยง" |
| `EDU_NAME` | `EDU` | "ประถมศึกษา", "มัธยมศึกษา" |
| `OCCPTN_NAME` | `OCCPTN` | "รับจ้างทั่วไป", "ข้าราชการ" |
| `HOMETYPE_NAME` | `HOMETYPE` | "ห้องเช่า", "บ้านเดี่ยว" |
| `PRVLG_NAME` | `PRVLG` | "บัตรทอง", "ประกันสังคม" |
| `WRKJOURNEY_NAME` | `WRKJOURNEY` | "รถจักรยานยนต์", "รถโดยสาร" |
| ... | ... | ... |

### Sort Order (11 columns) — ไม่นำเข้า DB

`AGE_SORT`, `BMI_SORT`, `ALCOHAL_SORT`, `SMOKE_SORT`, `BP_SORT`, `SELFOUR_SORT`, `ST5_SORT`, `VSACT_SORT`, `DRSCN_SORT`, `SCR2Q_SORT`, `HOMELAND_SORT`

### ค่าคำนวณ (4 columns) — ใช้ตรวจสอบ/เสริม

| Column | ความหมาย | ตัวอย่าง | วิธีใช้ |
|--------|---------|---------|--------|
| `BMI` | Body Mass Index | 27.5 | ตรวจสอบกับ BMI ที่คำนวณจาก HEIGHT/WEIGHT |
| `BMI_GROUP` | กลุ่ม BMI | "อ้วนระดับ 1" | ใช้เป็น reference |
| `AGE_GROUP` | กลุ่มอายุ | "15-34 ปี" | ตรวจสอบกับ age_group ที่คำนวณ |
| `BP_GROUP` | กลุ่มความดัน | "ปกติ" | ใช้เป็น reference |

### Interpretation ผลแลป (11 columns) — ใช้ cross-check

| Column | แปลจาก | ค่า |
|--------|--------|-----|
| `CHESTRES` | `CHEST` | "ปกติ" / "ผิดปกติ" / "ไม่ได้ตรวจ" |
| `EKGRES` | `EKG` | เหมือน |
| `BLDSGRES` | `BLDSGRS` | เหมือน |
| `CBCRES` | `CBCRS` | เหมือน |
| `LIVERRES` | `LIVERRS` | เหมือน |
| `URICRES` | `URICRS` | เหมือน |
| `CVCRES` | `CVCRS` | เหมือน |
| `CLCRES` | `CLCRS` | เหมือน |
| `EGFRES` | `EGFR` | เหมือน |
| `UARES` | `UARS` | เหมือน |
| `CHLTRRES` | `CHLTRRS` | เหมือน |

### ค่าแลปตัวเลข (3 columns)

| Column | ความหมาย | เทียบกับ Portal |
|--------|---------|---------------|
| `LAB_HEMOGLOBIN` | Hemoglobin | = `HMGB` |
| `LAB_CHOLESTERAL` | Cholesterol | = `CHOLEST` |
| `LAB_EGFR` | eGFR | = `EGFRRS` |

### ประวัติโรคครอบครัว — text (6 columns)

| Column | ความหมาย | แปลจาก |
|--------|---------|--------|
| `H_DM_NAME` | พ่อแม่เป็นเบาหวาน | `PDM` |
| `H_HPT_NAME` | พ่อแม่เป็นความดัน | `PHPT` |
| `H_STROKE_NAME` | พ่อแม่เป็นหลอดเลือดสมอง | `PSTROKE` |
| `H_CHLTR_NAME` | พ่อแม่เป็นไขมัน | — |
| `H_HRT_NAME` | พ่อแม่เป็นหัวใจ | `PHRTM` |
| `H_KIDNEY_NAME` | พ่อแม่เป็นไต | `PKIDNEY` |

### อื่นๆ

| Column | ความหมาย |
|--------|---------|
| `HD` | ชื่อสถานพยาบาลเต็ม (text) |
| `DISTRICT` | รหัสเขต 1001-1050 (**9999 = ไม่ระบุ**) |
| `DISTYPE` | รวมความพิการ (text) |

---

## Merge Summary — Total Fields เข้า Database

| หมวด | จำนวน fields | แหล่ง |
|------|------------|------|
| **Identity** (hash) | 1 | IDCARD/PID → idcard_hash |
| **Demographics** | 6 | เพศ, วันเกิด, อายุ, กลุ่มอายุ, คำนำหน้า, ประเภทบัตร |
| **Visit metadata** | 5 | วันตรวจ, สถานพยาบาล, เขต, สถานะยกเลิก, data_source |
| **Vitals** | 9 | BP, glucose, height, weight, waist, pulse, BMI |
| **Screening results** | 11 | RISKDM, RISKHPT, DM, HPT, STROKE, FAT, CHLTR, ... |
| **Behavior** | 8 | สูบบุหรี่, แอลกอฮอล์, ออกกำลังกาย, อาหาร 3 ชนิด, วัคซีน 2 |
| **Mental health** | 16 | 2Q, PHQ-9 (9 ข้อ), ST-5 (5 ข้อ) |
| **Lab values** | 18 | CBC, FBS, lipid, liver, kidney, uric acid |
| **Lab results (code)** | 10 | ผลตรวจ ปกติ/ผิดปกติ |
| **Screening tests** | 4 | EKG, X-ray, สายตา, จอประสาทตา |
| **Chronic history** | 13 | โรคเรื้อรัง 6 + สถานะรักษา 6 + มี/ไม่มี |
| **Family history** | 8 | พ่อแม่เป็นโรค 7 + มี/ไม่มี |
| **Social** | 8 | การศึกษา, อาชีพ, ที่อยู่, สิทธิ์, ดูแลตนเอง, ความพิการ |
| **MSD** | 22 | ระบบหายใจ 4, ปวดกล้ามเนื้อ 10, อาการร้าว 4, หู, ตา |
| **Referral** | 9 | ผลส่งต่อ + เหตุผล (Portal only) |
| **LGBTQ/Religion** | 3 | ศาสนา, LGBTQ, รายละเอียด |
| **Pets** | 7 | สัตว์เลี้ยง (Portal only) |
| **Computed** | 5 | BMI, age, age_group, PHQ-9 total, ST-5 total |
| **Meta** | 3 | data_source, import_batch_id, created_at |
| | | |
| **TOTAL** | **~166 fields** | **เข้า DB จาก 380 raw columns** |

**214 columns ที่ไม่นำเข้า**: PII (ชื่อ/ที่อยู่/โทร), *_NAME labels, *_SORT, *_OTH descriptions, duplicate audit fields (FIRSTSTF/LASTSTF), FLAG
