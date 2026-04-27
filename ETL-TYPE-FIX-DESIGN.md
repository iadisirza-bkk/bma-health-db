# ETL Type-Inference Fix — Design Document

Status: DESIGN — not yet applied
Owner: ETL / Database
Target file: `/Users/dev/bma-health-db/etl/bootstrap_variable_definitions.py`
Related: `/Users/dev/bma-health-db/etl/import_csv_v3.py`,
`/Users/dev/bma-health-db/db/migrations/101_schema_v3_public_mvs.sql`
Source-of-truth spreadsheet: `/Users/dev/bma-med/all_var.xlsx`
(675 rows × 5 sheets: `All_Variables`, `Portal`, `App1`, `App2`, `Pivot_by_Subdomain`)

---

## 0. Problem statement (one paragraph)

`bootstrap_variable_definitions.py` decides each variable's `data_type` from a
regex over `possible_values + description`. The regex is too permissive and
matches `\d=` for almost every coded categorical (`0=ไม่เลือก, 1=เลือก`), so
every yes/no flag is classified as `'code'` instead of `'boolean'`. Pure
numerics (`HEIGHT`, `WEIGHT`, `BMI`, `HBPN`, `LBPN`, `PREFPG`, `CHOLEST`,
`HDL`, `LDL`, `TRIGLY`, `EGFR_LAB`, `HBA1C`, …) carry an empty
`possible_values` (`nan`) and an unhelpful Thai description (`น้ำหนัก`,
`ส่วนสูง`, `ผล Cholesteral` …) that the regex's `NUMBER_HINTS` cannot
recognise — they fall through to `'text'`. Pure dates (`BIRTHDATE`,
`BRTHDATE`, `CANCELDATE`, `FIRSTDATE`, `LASTDATE`) similarly carry an empty
`possible_values` and the description is in Thai (`วันเวลาที่บันทึกข้อมูล`)
which the regex's `\bDATE\b` does not catch (the word `DATE` only appears in
the variable NAME, never in the description). Net result observed in the
running DB:

| data_type | rows in `private.variable_definition` |
|---|---:|
| `text` | **329** |
| `code` | 248 |
| `date` | 2 (only `VSTDATE`@portal/app1 — by lucky `VSTDATE` containing the literal substring `DATE`? In fact the regex hit `วันที่`) |
| `number` | **0** |
| `boolean` | **0** |

`import_csv_v3.py` then dispatches by `data_type` (lines 495–516, 665–685): if
`text` it writes to `value_text` only; if `code` likewise. Therefore *all*
3,279,575 rows in `private.visit_measurement` sit in `value_text`:

```sql
SELECT slot, COUNT(*)
FROM (
  SELECT CASE
    WHEN value_number  IS NOT NULL THEN 'number'
    WHEN value_boolean IS NOT NULL THEN 'boolean'
    WHEN value_date    IS NOT NULL THEN 'date'
    WHEN value_text    IS NOT NULL THEN 'text'
    ELSE 'null'
  END AS slot
  FROM private.visit_measurement
) s
GROUP BY slot;
--   text | 3279575     ← every single row
```

Materialized views downstream filter on the typed slots:

* `public.mv_disease_district` joins on `vm.value_boolean = TRUE` → returns 0 rows.
* `public.mv_lab_distribution` does `WIDTH_BUCKET(lm.value_number, …)` → 0 rows.
* `public.mv_mental_health` sums `vm.value_number` for PHQ-9/ST5 → 0 rows.
* `public.mv_kpi_tier1` joins likewise → empty.

The Frontend's `/api/v2/summary/overview`,
`/api/v2/disease/<key>/district-summary`, lab distribution panels and
mental-health screen all show empty data.

There is also one **semantic** wrinkle uncovered while building this design:
`DM`, `HPT`, `CHLTR`, `STROKE`, `HRT` etc. carry **inverted polarity** between
sources — Portal uses `0=ไม่เลือก, 1=เลือก` (1 = HAS the disease) but App1
uses `1=เป็น, 2=ไม่เป็น` (1 = HAS, 2 = DOES-NOT-HAVE). The current
`CANONICAL_RENAMES` blindly maps both into the same `variable_key`
(`found_dm`, `found_hpt`, …). For boolean conversion this matters: the cast
rule must be source-aware. Section 1.1 lists every affected variable.

---

## 1. Variable type classification (the meat)

Method: I cross-referenced the 378 distinct `variable_key` values in
`private.variable_definition` (which expand to 579 rows once you spread by
source) with the 675-row `all_var.xlsx`. I read each variable's Sub-domain,
Description and Possible Values from the spreadsheet and propose a target
`data_type`. Every entry below cites the spreadsheet sub-domain so a reviewer
can grep `all_var.xlsx` to verify.

I keep the existing schema 5-way enum: `text | code | boolean | number | date`.
For multi-source vars where the polarity differs (Section 1.1), the proposal is
to either (a) split the canonical key by source or (b) special-case the
backfill UPDATE; this design picks (b).

Conventions used below:

* "Sub-domain" = column G in the spreadsheet (`Sub-domain` field).
* "Sources" = which of `portal | app1 | app2` registers the column.
* For each variable, the table rows mirror what is currently in
  `private.variable_definition`. The `→ NEW` column is the proposed target.

### 1.A — `boolean` (binary 0/1 flags, including paired `1=มี / 2=ไม่มี` and `1=เป็น / 2=ไม่เป็น`)

These are all stored as a single character `0`, `1` or `2` whose meaning is a
true-or-false predicate. `_parse_bool()` already handles `'0'`, `'1'`, `'true'`,
`'false'` etc.; for the `1=เป็น / 2=ไม่เป็น` polarity we extend it (Section 2).

Disease-risk and disease-found flags (sub-domain `อาการป่วยที่ตรวจพบ` / `อื่นๆ` / `โรคประจำตัว`):

| variable_key | csv_col | sources | current pv | semantic | → NEW |
|---|---|---|---|---|---|
| risk_dm | RISKDM | portal, app1 | `0=ไม่เลือก, 1=เลือก` | 1 = at risk | **boolean** |
| risk_hpt | RISKHPT | portal, app1 | `0=ไม่เลือก, 1=เลือก` | 1 = at risk | **boolean** |
| risk_cvd | RISKCDVCL | portal, app1 | `0=ไม่เลือก, 1=เลือก` | 1 = at risk | **boolean** |
| risk_bmi | RISKBMI | portal, app1 | `0=ไม่เลือก, 1=เลือก` | 1 = at risk (overweight) | **boolean** |
| found_dm | DM | portal | `0=ไม่เลือก, 1=เลือก` | 1 = has disease | **boolean** |
| found_dm | DM | app1 | `1=เป็น, 2=ไม่เป็น` | 1 = has, 2 = doesn't | **boolean** (with inversion — see §1.1) |
| found_hpt | HPT | portal, app1 | `0=ไม่เลือก, 1=เลือก` | 1 = has | **boolean** |
| found_cvd | CDVCL | portal, app1 | `0=ไม่เลือก, 1=เลือก` | 1 = has | **boolean** |
| found_dyslipidemia | CHLTR | portal | `0=ไม่เลือก, 1=เลือก` | 1 = has | **boolean** |
| found_dyslipidemia | CHLTR | app1 | `1=เป็น, 2=ไม่เป็น` | 1 = has, 2 = doesn't | **boolean** (inversion) |
| found_stroke | STROKE | portal | `0=ไม่เลือก, 1=เลือก` | 1 = has | **boolean** |
| found_stroke | STROKE | app1 | `1=เป็น, 2=ไม่เป็น` | 1 = has, 2 = doesn't | **boolean** (inversion) |
| hrt (heart-disease found) | HRT | portal | `0=ไม่เลือก, 1=เลือก` | 1 = has | **boolean** |
| hrt (heart-disease found) | HRT | app1 | `1=เป็น, 2=ไม่เป็น` | 1 = has, 2 = doesn't | **boolean** (inversion) |
| kidney | KIDNEY | portal, app1 | `1=เป็น, 2=ไม่เป็น` | 1 = has | **boolean** (inversion) |
| body_fat_pct (mis-named — actually a flag) | FAT | portal | `0=ไม่เลือก, 1=เลือก` | 1 = obese flag | **boolean** |

(Note: NCD obesity flag is named `body_fat_pct` in the canonical map but the
column is a Boolean checkbox. Decision below: keep the key, change the type to
`boolean`. A follow-up rename to `found_obesity` is recommended but **out of
scope** for this fix; the MV `mv_disease_district` already lists
`'found_obesity'` as a key it queries — the bootstrap file should also rename
this in `CANONICAL_RENAMES`. See Section 7 "out of scope".)

Pet/lifestyle Boolean checkboxes (sub-domain `บริบทที่อยู่อาศัย`):

| variable_key | csv_col | sources | pv | → NEW |
|---|---|---|---|---|
| dog | DOG | portal | `0=ไม่เลือก, 1=เลือก` | **boolean** |
| cat | CAT | portal | `0=ไม่เลือก, 1=เลือก` | **boolean** |
| amloth | AMLOTH | portal | `0=ไม่เลือก, 1=เลือก` | **boolean** |

Family-history flags (sub-domain `ประวัติครอบครัว`, all `0=ไม่เลือก, 1=เลือก`):

| variable_key | csv_col | sources | → NEW |
|---|---|---|---|
| pdm | PDM | portal | **boolean** |
| pdm | PDM | app2 | **boolean** (description = "ประวัติครอบครัว: เบาหวาน") |
| phpt | PHPT | portal, app2 | **boolean** |
| phrtm | PHRTM | portal, app2 | **boolean** |
| pkidney | PKIDNEY | portal, app2 | **boolean** |
| pstroke | PSTROKE | portal, app2 | **boolean** |
| pgout | PGOUT | portal, app2 | **boolean** |
| pepm | PEPM | portal, app2 | **boolean** |
| poth | POTH | portal | **boolean** |

NCD comorbidity flags (sub-domain `โรคประจำตัว`, App1 uses `1=เป็น/2=ไม่เป็น`):

| variable_key | csv_col | source | → NEW |
|---|---|---|---|
| asth | ASTH | app1 | **boolean** (3-state `1=เป็น, 2=ไม่เป็น, 3=ไม่เคยตรวจ` — see note) |
| emphy | EMPHY | app1 | **boolean** (3-state) |
| eplpy | EPLPY | app1 | **boolean** (3-state) |
| cgtds | CGTDS | app1 | **boolean** (3-state `1=มี, 2=ไม่มี, 3=ไม่ได้ตรวจ`) |
| cgtdsmn | CGTDSMN | app1 | **boolean** (`1=ไม่มี, 2=มี` — INVERTED) |
| cgtdsot | CGTDSOT | app1 | **boolean** (`0=ไม่เลือก,1=เลือก`) |

Note on 3-state ("not screened" / "no" / "yes"): proposal is to map
`1 → TRUE`, `2 → FALSE`, `3 → NULL` for the boolean cast. This preserves the
"no answer" signal without polluting the boolean. We retain the original
string in `value_text` (Hybrid option from §4) so analysts can recover the
3rd state if they need it.

Mental-health 2Q items (sub-domain `คัดกรองสุขภาพจิต/ความเครียด`):

| variable_key | csv_col | source | pv | → NEW |
|---|---|---|---|---|
| depression_2q_1 | SCR2Q1 | portal, app1 | `0=ไม่ได้ตรวจ , 1=ไม่ใช่, 2=ใช่` | **boolean** (2 → TRUE, 1 → FALSE, 0 → NULL) |
| depression_2q_2 | SCR2Q2 | portal, app1 | same as above | **boolean** |

Symptom checklist (sub-domain `อาการระบบทางเดินหายใจ` / `อาการเตือนทางระบบประสาท` / `อาการปวดกล้ามเนื้อ-โครงร่าง`, all `1=มี,2=ไม่มี`):

| variable_key | csv_col | source | → NEW |
|---|---|---|---|
| scrres01 | SCRRES01 | portal | **boolean** |
| scrres02 | SCRRES02 | portal | **boolean** |
| scrres03 | SCRRES03 | portal | **boolean** |
| scrres04 | SCRRES04 | portal | **boolean** |
| symp01 | SYMP01 | portal | **boolean** |
| symp02 | SYMP02 | portal | **boolean** |
| symp03 | SYMP03 | portal | **boolean** |
| symp04 | SYMP04 | portal | **boolean** |
| ankle | ANKLE | portal | **boolean** |
| elbow | ELBOW | portal | **boolean** |
| head | HEAD | portal | **boolean** |
| hip | HIP | portal | **boolean** |
| knee | KNEE | portal | **boolean** |
| lwbh | LWBH | portal | **boolean** |
| neck | NECK | portal | **boolean** |
| shldr | SHLDR | portal | **boolean** |
| upbh | UPBH | portal | **boolean** |
| wrist | WRIST | portal | **boolean** |
| ptgleft | PTGLEFT | portal | **boolean** (`1=ปกติ, 2=ไม่ปกติ` — TRUE = abnormal) |
| ptgright | PTGRIGHT | portal | **boolean** (TRUE = abnormal) |

Lab-result "normal/abnormal" flags (sub-domain `แลบ -*`, all `1=ปกติ, 2=ผิดปกติ`).
Convention: `TRUE` = ABNORMAL (matches the disease-found polarity).

| variable_key | csv_col | sources | → NEW |
|---|---|---|---|
| bldsgrs | BLDSGRS | portal, app1, app2 | **boolean** (TRUE = abnormal) |
| cbcrs | CBCRS | portal, app1, app2 | **boolean** |
| chltrrs | CHLTRRS | portal, app1, app2 | **boolean** |
| clcrs | CLCRS | portal, app1, app2 | **boolean** |
| cvcrs | CVCRS | portal, app1, app2 | **boolean** |
| egfr | EGFR | portal, app1, app2 | **boolean** (TRUE = abnormal kidney) |
| liverrs | LIVERRS | portal, app1, app2 | **boolean** |
| uars | UARS | portal, app1, app2 | **boolean** |
| uricrs | URICRS | portal, app1, app2 | **boolean** |
| vsactrs (free-text but desc says it's a 1/2 flag) | VSACTRS | portal, app1 | **boolean** (TRUE = abnormal vision) |

Lab-test ordering checkboxes (sub-domain `แลบ -*`, all `0=ไม่เลือก, 1=เลือก`):

| variable_key | csv_col | sources | → NEW |
|---|---|---|---|
| bldsg | BLDSG | portal, app1 | **boolean** (test was ordered) |
| cbc | CBC | portal, app1 | **boolean** |
| chkegfr | CHKEGFR | portal | **boolean** |
| chkot | CHKOT | portal, app1 | **boolean** |
| clc | CLC | portal, app1 | **boolean** |
| cvc | CVC | portal, app1 | **boolean** |
| liver | LIVER | portal, app1 | **boolean** |
| ua | UA | portal, app1 | **boolean** |
| uric | URIC | portal, app1 | **boolean** |

Lifestyle/risk-factor checkboxes (sub-domain `พฤติกรรมสุขภาพ`/`อื่นๆ`, all `0/1`):

| variable_key | csv_col | sources | → NEW |
|---|---|---|---|
| smoketype1 | SMOKETYPE1 | portal | **boolean** |
| smoketype2 | SMOKETYPE2 | portal | **boolean** |
| smoketype3 | SMOKETYPE3 | portal | **boolean** |
| smoketype4 | SMOKETYPE4 | portal | **boolean** |
| stmng1 | STMNG1 | portal | **boolean** |
| stmng2 | STMNG2 | portal | **boolean** |
| stmng3 | STMNG3 | portal | **boolean** |
| stmng4 | STMNG4 | portal | **boolean** |
| fdfat | FDFAT | portal, app1 | **boolean** |
| fdnon | FDNON | portal, app1 | **boolean** |
| fdslt | FDSLT | portal, app1 | **boolean** |
| fdsw | FDSW | portal, app1 | **boolean** |
| rffw | RFFW | portal | **boolean** |
| rfnon | RFNON | portal | **boolean** |
| rfoth | RFOTH | portal | **boolean** |
| rfover | RFOVER | portal | **boolean** |
| rfprvlg | RFPRVLG | portal | **boolean** |
| rfspc | RFSPC | portal | **boolean** |
| oth (other-disease checkbox) | OTH | portal, app1 | **boolean** |
| csoth | CSOTH | portal | **boolean** |
| csrefer | CSREFER | portal | **boolean** |
| csslf | CSSLF | portal | **boolean** |
| wdsick | WDSICK | portal | **boolean** |
| smoking | SMOKE | portal, app1 | **boolean** (`1=สูบ, 0=ไม่สูบ`) |
| alcohol | ALCOHAL | portal, app1 | **boolean** (`1=ดื่ม, 0=ไม่ดื่ม`) |

Disability sub-checkboxes (sub-domain `ความพิการ/พึ่งพิง`, all `0/1`):

| variable_key | csv_col | sources | → NEW |
|---|---|---|---|
| discare1 | DISCARE1 | portal | **boolean** |
| discare2 | DISCARE2 | portal | **boolean** |
| discare3 | DISCARE3 | portal | **boolean** |
| discare4 | DISCARE4 | portal | **boolean** |
| distype1 | DISTYPE1 | portal, app1 | **boolean** (the parent `DISTYPE` is a code; the numeric suffix variants are checkboxes) |
| distype2 | DISTYPE2 | portal, app1 | **boolean** |
| distype3 | DISTYPE3 | portal, app1 | **boolean** |
| distype4 | DISTYPE4 | portal, app1 | **boolean** |
| distype5 | DISTYPE5 | portal, app1 | **boolean** |
| distype6 | DISTYPE6 | portal, app1 | **boolean** |
| distype7 | DISTYPE7 | portal, app1 | **boolean** |
| distype8 | DISTYPE8 | portal, app1 | **boolean** |

Service request checkboxes (sub-domain `ข้อเสนอแนะบริการ`, all `0/1`):

| variable_key | csv_col | sources | → NEW |
|---|---|---|---|
| request1 | REQUEST1 | portal | **boolean** |
| request2 | REQUEST2 | portal | **boolean** |
| request3 | REQUEST3 | portal | **boolean** |
| request4 | REQUEST4 | portal | **boolean** |
| request5 | REQUEST5 | portal | **boolean** |
| request6 | REQUEST6 | portal | **boolean** |
| request7 | REQUEST7 | portal | **boolean** |

Audit/lifecycle (sub-domain `ระบบ/Audit`):

| variable_key | csv_col | sources | pv | → NEW |
|---|---|---|---|---|
| cancelst | CANCELST | portal, app1 | `0=ใช้งานอยู่, 1=ยกเลิก` | **boolean** (TRUE = cancelled) |
| flag | FLAG | portal | `0=เจ้าหน้าที่บันทึก, 1=ผู้ป่วยบันทึกเอง` | **boolean** (TRUE = self-reported) |

**Boolean total: 95 distinct `variable_key` rows; 122 (variable_key, source_code) tuples.**

### 1.B — `number` (continuous numerics)

The MVs need real `numeric` values for `AVG()` / `WIDTH_BUCKET()`. Every
variable below currently has `data_type='text'` and an empty `possible_values`
in the bootstrap file. I list each by sub-domain.

Anthropometry (sub-domain `มานุษยวิทยา` / `Vital Signs`):

| variable_key | csv_col | sources | → NEW | unit (proposed) | range hint |
|---|---|---|---|---|---|
| height_cm | HEIGHT | portal, app1 | **number** | cm | 50–250 |
| weight_kg | WEIGHT | portal, app1 | **number** | kg | 5–300 |
| waist_cm | WSTL | portal, app1 | **number** | cm | 30–200 |
| bmi | BMI | app2 | **number** | kg/m² | 10–80 |

Vital signs (sub-domain `ความดัน/ชีพจร`):

| variable_key | csv_col | sources | → NEW | unit | range |
|---|---|---|---|---|---|
| sbp | HBPN | portal, app1 | **number** | mmHg | 50–260 |
| dbp | LBPN | portal, app1 | **number** | mmHg | 30–180 |
| pr (pulse rate, currently key=`pr`) | PR | portal | **number** | bpm | 30–220 |
| pulse_rate (synonym) | PULSE | (only in `CANONICAL_RENAMES`, no DB row currently) | **number** | bpm | 30–220 |

Glucose (sub-domain `แลบ - น้ำตาลในเลือด`, `น้ำตาลในเลือด (เร็ว)`):

| variable_key | csv_col | sources | → NEW | unit |
|---|---|---|---|---|
| fasting_glucose | PREFPG | portal, app1 | **number** | mg/dL |
| post_glucose | POSTFPG | portal, app1 | **number** | mg/dL |
| dtx | DTX | portal, app1 | **number** | mg/dL |
| fbs | FBS | portal, app1 | **number** | mg/dL |
| blood_sugar | BLDSUGAR | portal, app1 | **number** | mg/dL |
| hba1c | HBA1C | (defined in `CANONICAL_RENAMES`, no DB row) | **number** | % |
| bldhour | BLDHOUR | portal, app1 | **number** | hours since last meal |

Lipid panel (sub-domain `แลบ - ไขมัน`):

| variable_key | csv_col | sources | → NEW | unit |
|---|---|---|---|---|
| total_cholesterol | CHOLEST | portal, app1 | **number** | mg/dL |
| triglyceride | TRIGLY | portal, app1 | **number** | mg/dL |
| hdl | HDL | portal, app1 | **number** | mg/dL |
| ldl | LDL | portal, app1 | **number** | mg/dL |

Kidney panel (sub-domain `แลบ - การทำงานของไต`):

| variable_key | csv_col | sources | → NEW | unit |
|---|---|---|---|---|
| crtinine | CRTININE | portal, app1 | **number** | mg/dL |
| egfrrs (eGFR numeric) | EGFRRS | portal, app1 | **number** | mL/min/1.73m² |
| egfroth (free-text BUT actually numeric) | EGFROTH | portal, app1 | **number** | mL/min |
| egfr_lab | EGFR_LAB | app1 | **number** | mL/min |
| lab_egfr | LAB_EGFR | app2 | **number** | mL/min |
| bunrs | BUNRS | portal | **number** | mg/dL |
| uric_acid | URICACID | (in CANONICAL_RENAMES, no DB row) | **number** | mg/dL |

Liver panel (sub-domain `แลบ - ตับ`):

| variable_key | csv_col | sources | → NEW | unit |
|---|---|---|---|---|
| sgot | SGOT | portal, app1 | **number** | U/L |
| sgpt | SGPT | portal, app1 | **number** | U/L |
| alkppt | ALKPPT | portal, app1 | **number** | U/L |

CBC (sub-domain `แลบ - CBC`):

| variable_key | csv_col | sources | → NEW | unit |
|---|---|---|---|---|
| hmgb (hemoglobin) | HMGB | portal, app1 | **number** | g/dL |
| hmtc (hematocrit) | HMTC | portal, app1 | **number** | % |
| wbc | WBC | portal, app1 | **number** | ×10³/μL |
| rbc | RBC | portal, app1 | **number** | ×10⁶/μL |
| mcv | MCV | portal, app1 | **number** | fL |
| mnc | MNC | portal, app1 | **number** | % |
| ntp | NTP | portal, app1 | **number** | % |
| lmpc | LMPC | portal, app1 | **number** | % |
| ecsnp | ECSNP | portal, app1 | **number** | % |
| pitcnt | PITCNT | portal, app1 | **number** | ×10³/μL |
| lab_hemoglobin | LAB_HEMOGLOBIN | app2 | **number** | g/dL |

Urinalysis numerics (sub-domain `แลบ - ปัสสาวะ`):

| variable_key | csv_col | sources | → NEW |
|---|---|---|---|
| protein | PROTEIN | portal, app1 | **number** (mg/dL — sometimes ordinal `0/1+/2+/3+`; see Risks) |
| uarbc | UARBC | portal, app1 | **number** |
| uawbc | UAWBC | portal, app1 | **number** |
| bldsugar (sometimes used as urine glucose) | BLDSUGAR | (already listed) | **number** |

Cancer-screening numerics (sub-domain `แลบ - มะเร็งลำไส้/ปากมดลูก`):

| variable_key | csv_col | sources | → NEW |
|---|---|---|---|
| fittest | FITTEST | portal, app1 | **number** (FIT-test ng/mL) |
| hpv | HPV | portal, app1 | **number** (viral load — sometimes textual "Negative/Positive" — see Risks) |

Other lab numerics:

| variable_key | csv_col | sources | → NEW |
|---|---|---|---|
| labstatus | LABSTATUS | portal | **number** (timeout integer) — KEEP `text` if uncertain |
| lab_cholesteral | LAB_CHOLESTERAL | app2 | **number** mg/dL |

Vision (sub-domain `การมองเห็น`):

| variable_key | csv_col | sources | → NEW |
|---|---|---|---|
| leftvl | LEFTVL | portal | **number** (Snellen denominator value, 6/6 = 6, 6/9 = 9, 6/60 = 60) |
| rightvl | RIGHTVL | portal | **number** |
| leftrw | LEFTRW | portal | **number** (row index 1–8) |
| rightrw | RIGHTRW | portal | **number** |

Pet/animal counts (sub-domain `บริบทที่อยู่อาศัย`):

| variable_key | csv_col | sources | → NEW |
|---|---|---|---|
| dogamt | DOGAMT | portal | **number** |
| catamt | CATAMT | portal | **number** |
| amlamt | AMLAMT | portal | **number** |

Demographics:

| variable_key | csv_col | sources | → NEW |
|---|---|---|---|
| age | AGE | app1 | **number** (0–120) |
| age_sort | AGE_SORT | app2 | **number** (rank) |
| alcohal_sort | ALCOHAL_SORT | app2 | **number** |
| smoke_sort | SMOKE_SORT | app2 | **number** |
| bmi_sort | BMI_SORT | app2 | **number** |
| bp_sort | BP_SORT | app2 | **number** |
| selfour_sort | SELFOUR_SORT | app2 | **number** |
| st5_sort | ST5_SORT | app2 | **number** |
| vsact_sort | VSACT_SORT | app2 | **number** |
| drscn_sort | DRSCN_SORT | app2 | **number** |
| scr2q_sort | SCR2Q_SORT | app2 | **number** |
| homeland_sort | HOMELAND_SORT | app2 | **number** |

Mental health scores (sub-domain `คัดกรองสุขภาพจิต/ความเครียด`):

PHQ-9 questions are 0/1/2/3 ordinal scores → keep them as `number` because
the MV `mv_mental_health` literally does `SUM(vm.value_number)`:

| variable_key | csv_col | sources | → NEW |
|---|---|---|---|
| phq9_q1 | SCN9Q1 | portal, app1 | **number** |
| phq9_q2 | SCN9Q2 | portal, app1 | **number** |
| phq9_q3 | SCN9Q3 | portal, app1 | **number** |
| phq9_q4 | SCN9Q4 | portal, app1 | **number** |
| phq9_q5 | SCN9Q5 | portal, app1 | **number** |
| phq9_q6 | SCN9Q6 | portal, app1 | **number** |
| phq9_q7 | SCN9Q7 | portal, app1 | **number** |
| phq9_q8 | SCN9Q8 | portal, app1 | **number** |
| phq9_q9 | SCN9Q9 | portal, app1 | **number** |
| st5_q1 | ST501 | portal, app1 | **number** |
| st5_q2 | ST502 | portal, app1 | **number** |
| st5_q3 | ST503 | portal, app1 | **number** |
| st5_q4 | ST504 | portal, app1 | **number** |
| st5_q5 | ST505 | portal, app1 | **number** |

**Number total: 71 distinct `variable_key` rows; ≈110 (variable_key, source_code) tuples.**

### 1.C — `date` (calendar dates / timestamps)

All carry empty `possible_values`; the description says "วันเวลา…". The
existing `_parse_date()` accepts `%Y-%m-%d`, `%d/%m/%Y`, ISO timestamps and a
few other formats — it should cope with whatever Portal/App1/App2 emit.

| variable_key | csv_col | sources | sub_domain | → NEW |
|---|---|---|---|---|
| birthdate | BIRTHDATE / BRTHDATE | portal, app1 | `ข้อมูลส่วนบุคคล` | **date** |
| vstdate | VSTDATE | portal, app1, app2 | `วัน-เวลาเข้ารับบริการ` | **date** (already correct in 2 of 3 sources) |
| canceldate | CANCELDATE | portal, app1 | `ระบบ/Audit` | **date** |
| firstdate | FIRSTDATE | portal, app1 | `ระบบ/Audit` | **date** |
| lastdate | LASTDATE | portal, app1 | `ระบบ/Audit` | **date** |

`vsttime` is a clock time, not a calendar date. The `value_date` slot can't
hold time-of-day (it's `DATE`, not `TIMESTAMP`). Keep `vsttime` as **text**
unless we add a `value_time` slot (out of scope).

`firststf`, `laststf`, `cancelstf` are staff-name strings (sub-domain
`ระบบ/Audit`). Keep as **text**.

**Date total: 5 distinct `variable_key`s; 9 (key, source) tuples.**

### 1.D — `code` (multi-state categorical, 3+ values, no Boolean reduction)

These should KEEP `data_type='code'`. They're categorical with > 2 distinct
codes (or have a "not-screened" 3rd state that we explicitly want to retain
in the categorical channel rather than collapse to NULL boolean). The
`private.variable_code_value` table already maps them to labels.

| variable_key | csv_col | sources | pv | → KEEP |
|---|---|---|---|---|
| sex | MALE | portal, app1, app2 | `1=ชาย, 2=หญิง` (portal/app1) / `10=ชาย, 20=หญิง` (app2) | **code** |
| notype | NOTYPE | portal | `10/20/30` | **code** |
| rlgn | RLGN | portal | `1=พุทธ, 2=คริส, 3=อิสลาม, 4=อื่นๆ` | **code** |
| lgbtq | LGBTQ | portal | `1=ไม่ใช่, 2=ใช่, 3=ไม่ระบุ` | **code** |
| lgbtq1 | LGBTQ1 | portal | 4-state | **code** |
| lgbtq2 | LGBTQ2 | portal | 4-state | **code** |
| occptn | OCCPTN | portal, app1 | 5-state occupation | **code** |
| wrktype | WRKTYPE | portal | 9-state | **code** |
| wrkjourney | WRKJOURNEY | portal | 7-state | **code** |
| wrklife | WRKLIFE | portal, app1 | 4-state | **code** |
| hometype | HOMETYPE | portal, app1 | 8-state | **code** |
| homeland | HOMELAND | portal, app1 | 3-state (BKK/upcountry) | **code** |
| hometown | HOMETOWN | portal, app1 | 3-state | **code** |
| pet | PET | portal | 3-state | **code** |
| selfour | SELFOUR | portal, app1 | 4-state self-help | **code** |
| prvlgchk | PRVLGCHK | portal | 4-state | **code** |
| craddrflag | CRADDRFLAG | portal | 3-state | **code** |
| chest | CHEST | portal, app1 | `0=ไม่ได้ตรวจ, 1=ปกติ, 2=ผิดปกติ` (3-state — keep as code; abnormal-flag derivable) | **code** |
| ekg | EKG | portal, app1 | 3-state | **code** |
| drscn | DRSCN | portal, app1 | 3-state | **code** |
| vsact | VSACT | portal, app1 | 4-state | **code** |
| scrrs | SCRRS | portal, app1, app2 | `1=ปกติ, 2=เสี่ยง` — could be boolean but has only 2 codes; we keep as `code` because it's a SUMMARY state that downstream may want as labelled. (Borderline; could go either way.) | **code** |
| food | FOOD | portal, app1 | 4-state frequency (`1=ไม่ทาน, 2=สัปดาห์ละครั้ง, …`) | **code** |
| water | WATER | portal, app1 | 4-state frequency | **code** |
| noodle | NOODLE | portal, app1 | 4-state frequency | **code** |
| exercise | EXCERCISE | portal, app1 | 3-state | **code** |
| algyfood | ALGYFOOD | portal | 3-state | **code** |
| algymed | ALGYMED | portal | 3-state | **code** |
| asthrs | ASTHRS | app1 | 4-state treatment | **code** |
| chltrrs (note: lifestyle one is a treatment-state code — different from lab CHLTRRS!) | CHLTRRS | app1 | 4-state in `การรักษาโรคประจำตัว` | **code** |
| dmrs | DMRS | app1 | 4-state | **code** |
| hptrs | HPTRS | app1 | 4-state | **code** |
| hrtrs | HRTRS | app1 | 4-state | **code** |
| kidneyrs | KIDNEYRS | app1 | 4-state | **code** |
| strokers | STROKERS | app1 | 4-state | **code** |
| emphyrs | EMPHYRS | app1 | 4-state | **code** |
| eplyrs | EPLYRS | app1 | 4-state | **code** |
| treatslf | TREATSLF | app1 | 3-state | **code** |
| dmfm | DMFM | app1 | 3-state | **code** |
| parent | PARENT | portal, app2 | 3-state | **code** |
| chkhiv | CHKHIV | app1 | 2-state | **code** (privacy-sensitive — keep coded) |
| covid | COVID | portal, app2 | 3-state | **code** (App2 records it as text label in raw — we keep both: app2 stays text, portal stays code) |
| vcccovid | VCCCOVID | portal, app2 | 3-state | **code** |
| vccinfluza | VCCINFLUZA | portal | 3-state | **code** |
| chltrtype | CHLTRTYPE | portal, app1 | 2-state | **code** |
| bldsgtype | BLDSGTYPE | portal, app1 | 2-state | **code** |
| discare | DISCARE | portal | 2-state | **code** (the SCAFFOLD/parent — checkboxes go through `discare1..4`) |
| discard | DISCARD | portal | 2-state | **code** |
| distype | DISTYPE | portal, app1 | 2-state parent | **code** |
| workshop | WORKSHOP | portal | 4-state | **code** |
| fgrub01 | FGRUB01 | portal | 3-state hearing | **code** |
| fgrub02 | FGRUB02 | portal | 2-state hearing-side | **code** |

**Code total: ≈55 distinct `variable_key`s.** (Note: many existing
`data_type='code'` rows in 1.A get CONVERTED to boolean. The list here is the
reduced set that genuinely stays code.)

### 1.E — `text` (free-text, identifiers, labels)

Everything not in 1.A–1.D stays `text`. Highlights to verify:

| variable_key | csv_col | sources | reason → KEEP text |
|---|---|---|---|
| idcard | IDCARD | portal | National-ID PII (already PII-flagged) |
| source_pid | PID | portal, app1, app2 | Encrypted patient identifier |
| name_prefix | PNAME | portal, app1 | Free-text honorific |
| pnameoth | PNAMEOTH | portal | Other-prefix free text |
| fname / lname / efname / elname | FNAME etc. | portal, app1 | Names |
| email | EMAIL | portal | |
| idline | IDLINE | portal | LINE ID |
| phone | PHONE | portal, app1 | |
| rank | RANK | portal | Position string |
| haddr / hsoi / hstreet / hmoo / hzipcode / location | … | portal | Address strings |
| home_district / home_province / home_subdistrict | HDISTRICT / HPROVINCE / HSUBDISTRICT / DISTRICT (app1+app2) | all | District CODES are stored as 4-digit strings (e.g. "1001"). Numeric-LOOKING but we **must** keep as text — leading zeros and the join into `private.geo_district` is by string. |
| current_district / current_province / current_subdistrict | CRDISTRICT etc. | portal | text |
| work_district / work_province / wrksubdistrict | WRKDISTRICT etc. | portal, app2 | text |
| province / subdistrict | PROVINCE / SUBDISTRICT | app1 | text |
| districtbkk | DISTRICTBKK | portal | text |
| orgbma | ORGBMA | portal | text |
| hourlist | HOURLIST | portal | text code list |
| edu | EDU | portal, app2 | LOOKS like a code but stored as a text label by App2; existing data is mixed → keep text |
| eduoth | EDUOTH | portal | text |
| occptn17 / occptn18 / occptn19 | … | portal | text descriptors |
| occpttnoth | OCCPTTNOTH | portal | text |
| othdesc | OTHDESC | portal, app1 | text |
| pothdesc | POTHDESC | portal | |
| csothdesc | CSOTHDESC | portal | |
| chestoth / ekgoth / drscnoth / chkoth | … | portal, app1 | "other" abnormal-finding free text |
| bldsgrsoth / cbcrsoth / chltrrsoth / clcrsoth / cvcrsoth / liverrsoth / uarsoth / uricrsoth | … | portal, app1 | "other-result" free text |
| egfroth | EGFROTH | portal, app1 | mixed numeric + free text → keep text in raw, attempt numeric in §4 hybrid |
| extoth | EXTOTH | portal | |
| smoketypeoth | SMOKETYPEOTH | portal | |
| algyfoodoth / algymedoth | … | portal | |
| cgtdsoth / cgtdmnoth | … | app1 | |
| pothdesc | POTHDESC | portal | |
| request3oth / request4oth / request7oth | … | portal | |
| rfoverdesc / rfprvlgdesc / rfothdesc | … | portal | |
| hometypeoth | HOMETYPEOTH | portal | |
| wrkjourneyoth / wrktypeoth | … | portal | |
| prvlg / prvlgoth / healthuse / healthuseoth | … | portal, app2 | text labels (App2 expands them to label strings already) |
| rlgnoth | RLGNOTH | portal | |
| subhpt | SUBHPT | app1 | text |
| hd | HD | app2 | visit hash ID — text |
| hptcode | HPTCODE | portal, app1, app2 | facility code → text |
| firststf / laststf / cancelstf | … | portal, app1 | staff names |
| labstatus | LABSTATUS | portal | maps to enum string in raw — keep text (could be number; pick text for safety) |
| age_group / bmi_group / bp_group | … | app2 | text labels (the matching `_sort` companions are number) |
| (all `*_NAME` from app2: `alcohal_name, smoke_name, sex_name?, dm_name, hpt_name, …, h_dm_name, …, riskdm_name, …, rlgn_name, edu_name, occptn_name, hometype_name, wrkjourney_name, vsact_name, drscn_name, scr2q_name, st5_name, selfour_name, prvlg_name, fat_name, oth_name, homeland_name, hometype_name`) | … | app2 | App2 stores the human-readable label string for each enum — text |
| (all `*_RES`/`*_RES` derived display strings from app2: `bldsgres, cbcres, chestres, chltrres, clcres, cvcres, egfres, ekgres, liverres, uares, uricres`) | … | app2 | text labels |
| vsttime | VSTTIME | portal | clock time — text |
| pr | PR | portal | (DUPLICATES `pulse_rate` semantically — see §1.B; keeping as **number**) |
| ecsnp / hpv / fittest / hmgb / hmtc | … | portal, app1 | listed in 1.B as **number** |

**Text total: ~150 distinct `variable_key`s** (everything not in 1.A–D).

### 1.1 — Polarity inversion across sources (DM / HPT / CHLTR / STROKE / HRT / KIDNEY)

Cross-source semantics differ. The existing `CANONICAL_RENAMES` collapses
both Portal and App1 into the same `variable_key` even though they encode
the answer with **opposite truth values**:

| csv_col | source | possible_values | "1" means |
|---|---|---|---|
| DM | portal | `0=ไม่เลือก, 1=เลือก` | "selected ⇒ HAS DM" → 1 = TRUE |
| DM | app1 | `1=เป็น, 2=ไม่เป็น` | "is ⇒ HAS DM" → 1 = TRUE, 2 = FALSE |
| HPT | portal, app1 | both `0=ไม่เลือก, 1=เลือก` | aligned |
| CDVCL | portal, app1 | both `0=ไม่เลือก, 1=เลือก` | aligned |
| CHLTR | portal | `0=ไม่เลือก, 1=เลือก` | aligned |
| CHLTR | app1 | `1=เป็น, 2=ไม่เป็น` | 1 = TRUE, 2 = FALSE |
| STROKE | portal | `0=ไม่เลือก, 1=เลือก` | |
| STROKE | app1 | `1=เป็น, 2=ไม่เป็น` | |
| HRT | portal | `0=ไม่เลือก, 1=เลือก` | |
| HRT | app1 | `1=เป็น, 2=ไม่เป็น` | |
| KIDNEY | portal, app1 | both `1=เป็น, 2=ไม่เป็น` | aligned (both 1=TRUE) |

The fix has to be **per-row, source-aware**: a numeric value of `1` ALWAYS
means TRUE; a value of `2` (only valid in the `1=เป็น, 2=ไม่เป็น`
encoding) maps to FALSE; a value of `0` (only valid in the
`0=ไม่เลือก, 1=เลือก` encoding) maps to FALSE.

Backfill rule (Section 4 hybrid):

```sql
value_boolean :=
  CASE TRIM(value_text)
    WHEN '1' THEN TRUE
    WHEN '1.0' THEN TRUE
    WHEN '0' THEN FALSE
    WHEN '0.0' THEN FALSE
    WHEN '2' THEN FALSE       -- only the {1,2} encoding
    WHEN '2.0' THEN FALSE
    ELSE NULL                 -- unknowns / 'nan' / blanks
  END
```

The same rule (TRUE = 1; FALSE = anything in {0, 2}) safely covers all 7
encodings the spreadsheet contains:

* `0=ไม่เลือก, 1=เลือก`            → 0 → FALSE; 1 → TRUE
* `1=มี, 2=ไม่มี`                  → 1 → TRUE; 2 → FALSE
* `1=ปกติ, 2=ผิดปกติ`              → INVERTED (TRUE = abnormal); 1 → FALSE; 2 → TRUE
* `1=เป็น, 2=ไม่เป็น`              → 1 → TRUE; 2 → FALSE
* `1=ใช่, 0=ไม่ใช่`                → 1 → TRUE; 0 → FALSE
* `0=ใช้งานอยู่, 1=ยกเลิก`          → CANCELST: 1 → TRUE
* `0=ไม่ได้ตรวจ, 1=ไม่ใช่, 2=ใช่` → 2Q: 2 → TRUE; 1 → FALSE; 0 → NULL

The above generic rule is correct for all of these EXCEPT three groups that
need flipping/special-casing:

1. **Lab "normal/abnormal" (`*RS`)**: `1=ปกติ, 2=ผิดปกติ`. We want
   `TRUE = abnormal`, so `1 → FALSE, 2 → TRUE`.
2. **Mental-health 2Q (`SCR2Q1/2`)**: `0=ไม่ได้ตรวจ, 1=ไม่ใช่, 2=ใช่`.
   `TRUE = ใช่ = positive symptom`, so `2 → TRUE, 1 → FALSE, 0 → NULL`.
3. **Visual-acuity flag (`VSACTRS`)**: `1=ปกติ, 2=ผิดปกติ`. Same as labs.

Section 4 implements these as **per-`variable_id` UPDATE statements** with the
correct mapping per group. The bootstrap file does not need to encode this;
it just needs to declare the variables `boolean`.

To make this future-proof we add a per-key "boolean polarity" hint in
`EXPLICIT_TYPES` so the importer can apply the right mapping at write time
(see Section 2 patch).

---

## 2. Patch to `bootstrap_variable_definitions.py`

The patch (a) adds an `EXPLICIT_TYPES` lookup table keyed by canonical
`variable_key`, (b) consults it BEFORE the regex inference, (c) optionally
records boolean polarity for the importer.

### 2.1 Diff (unified)

```diff
--- a/etl/bootstrap_variable_definitions.py
+++ b/etl/bootstrap_variable_definitions.py
@@ -179,6 +179,168 @@ CANONICAL_RENAMES = {
     'FBS':       'fbs',
 }


+# ----- Explicit type overrides (variable_key → data_type) --------------------
+# Authoritative type assignment that wins over the regex inference below.
+# Source: ETL-TYPE-FIX-DESIGN.md §1.
+#
+# Lookup precedence (in to_variable_key()/infer_data_type()):
+#   1. EXPLICIT_TYPES[variable_key]   ← here
+#   2. regex on possible_values+description
+#   3. fallback 'text'
+
+EXPLICIT_TYPES: dict[str, str] = {
+    # ---- boolean (binary 0/1 flags; see §1.A) ----
+    # Disease risk flags (App2-derived)
+    'risk_dm':            'boolean',
+    'risk_hpt':           'boolean',
+    'risk_cvd':           'boolean',
+    'risk_bmi':           'boolean',
+    'risk_stroke':        'boolean',
+    'risk_dyslipidemia':  'boolean',
+    # Disease found flags
+    'found_dm':           'boolean',
+    'found_hpt':          'boolean',
+    'found_cvd':          'boolean',
+    'found_dyslipidemia': 'boolean',
+    'found_stroke':       'boolean',
+    'found_obesity':      'boolean',
+    # NCD comorbidity (App1 1=เป็น, 2=ไม่เป็น)
+    'hrt':                'boolean',
+    'kidney':             'boolean',
+    'asth':               'boolean',
+    'emphy':              'boolean',
+    'eplpy':              'boolean',
+    'cgtds':              'boolean',
+    'cgtdsmn':            'boolean',
+    'cgtdsot':            'boolean',
+    # Family history checkboxes
+    'pdm':                'boolean',
+    'phpt':               'boolean',
+    'phrtm':              'boolean',
+    'pkidney':            'boolean',
+    'pstroke':            'boolean',
+    'pgout':              'boolean',
+    'pepm':               'boolean',
+    'poth':               'boolean',
+    # Pets
+    'dog':                'boolean',
+    'cat':                'boolean',
+    'amloth':             'boolean',
+    # Mental-health 2Q
+    'depression_2q_1':    'boolean',
+    'depression_2q_2':    'boolean',
+    # Symptom checklists
+    'scrres01': 'boolean', 'scrres02': 'boolean',
+    'scrres03': 'boolean', 'scrres04': 'boolean',
+    'symp01':   'boolean', 'symp02':   'boolean',
+    'symp03':   'boolean', 'symp04':   'boolean',
+    'ankle': 'boolean', 'elbow': 'boolean', 'head':  'boolean',
+    'hip':   'boolean', 'knee':  'boolean', 'lwbh':  'boolean',
+    'neck':  'boolean', 'shldr': 'boolean', 'upbh':  'boolean',
+    'wrist': 'boolean',
+    'ptgleft': 'boolean', 'ptgright': 'boolean',
+    # Lab interpretation flags (TRUE = abnormal)
+    'bldsgrs': 'boolean', 'cbcrs':   'boolean',
+    'chltrrs': 'boolean', 'clcrs':   'boolean',
+    'cvcrs':   'boolean', 'egfr':    'boolean',
+    'liverrs': 'boolean', 'uars':    'boolean',
+    'uricrs':  'boolean', 'vsactrs': 'boolean',
+    # Lab-test ordering checkboxes
+    'bldsg':   'boolean', 'cbc':     'boolean',
+    'chkegfr': 'boolean', 'chkot':   'boolean',
+    'clc':     'boolean', 'cvc':     'boolean',
+    'liver':   'boolean', 'ua':      'boolean',
+    'uric':    'boolean',
+    # Body-fat (kept variable_key=body_fat_pct for back-compat)
+    'body_fat_pct': 'boolean',
+    # Lifestyle / risk-factor checkboxes
+    'smoketype1': 'boolean', 'smoketype2': 'boolean',
+    'smoketype3': 'boolean', 'smoketype4': 'boolean',
+    'stmng1': 'boolean', 'stmng2': 'boolean',
+    'stmng3': 'boolean', 'stmng4': 'boolean',
+    'fdfat': 'boolean', 'fdnon': 'boolean',
+    'fdslt': 'boolean', 'fdsw':  'boolean',
+    'rffw': 'boolean', 'rfnon': 'boolean', 'rfoth': 'boolean',
+    'rfover': 'boolean', 'rfprvlg': 'boolean', 'rfspc': 'boolean',
+    'oth':     'boolean',
+    'csoth':   'boolean', 'csrefer': 'boolean', 'csslf':   'boolean',
+    'wdsick':  'boolean',
+    'smoking': 'boolean', 'alcohol': 'boolean',
+    # Disability sub-checkboxes
+    'discare1': 'boolean', 'discare2': 'boolean',
+    'discare3': 'boolean', 'discare4': 'boolean',
+    'distype1': 'boolean', 'distype2': 'boolean',
+    'distype3': 'boolean', 'distype4': 'boolean',
+    'distype5': 'boolean', 'distype6': 'boolean',
+    'distype7': 'boolean', 'distype8': 'boolean',
+    # Service requests
+    'request1': 'boolean', 'request2': 'boolean',
+    'request3': 'boolean', 'request4': 'boolean',
+    'request5': 'boolean', 'request6': 'boolean',
+    'request7': 'boolean',
+    # Audit / lifecycle
+    'cancelst': 'boolean',
+    'flag':     'boolean',
+
+    # ---- number (continuous numerics; see §1.B) ----
+    # Anthropometry
+    'height_cm': 'number', 'weight_kg': 'number',
+    'waist_cm':  'number', 'bmi':       'number',
+    # Vital signs
+    'sbp':         'number', 'dbp':         'number',
+    'pr':          'number', 'pulse_rate':  'number',
+    # Glucose
+    'fasting_glucose': 'number', 'post_glucose': 'number',
+    'dtx':             'number', 'fbs':           'number',
+    'blood_sugar':     'number', 'hba1c':         'number',
+    'bldhour':         'number',
+    # Lipid
+    'total_cholesterol': 'number', 'triglyceride': 'number',
+    'hdl':               'number', 'ldl':          'number',
+    # Kidney
+    'crtinine': 'number', 'egfrrs':   'number',
+    'egfroth':  'number', 'egfr_lab': 'number',
+    'lab_egfr': 'number', 'bunrs':    'number',
+    'uric_acid':'number',
+    # Liver
+    'sgot':  'number', 'sgpt':  'number', 'alkppt':'number',
+    # CBC
+    'hmgb': 'number', 'hmtc': 'number', 'wbc':  'number',
+    'rbc':  'number', 'mcv':  'number', 'mnc':  'number',
+    'ntp':  'number', 'lmpc': 'number', 'ecsnp':'number',
+    'pitcnt':'number', 'lab_hemoglobin': 'number',
+    # Urinalysis
+    'protein': 'number', 'uarbc': 'number', 'uawbc': 'number',
+    # Cancer screen
+    'fittest': 'number', 'hpv': 'number',
+    # Other lab
+    'lab_cholesteral': 'number',
+    # Vision
+    'leftvl': 'number', 'rightvl': 'number',
+    'leftrw': 'number', 'rightrw': 'number',
+    # Pet counts
+    'dogamt': 'number', 'catamt': 'number', 'amlamt': 'number',
+    # Demographics
+    'age': 'number',
+    'age_sort':       'number', 'alcohal_sort':  'number',
+    'smoke_sort':     'number', 'bmi_sort':      'number',
+    'bp_sort':        'number', 'selfour_sort':  'number',
+    'st5_sort':       'number', 'vsact_sort':    'number',
+    'drscn_sort':     'number', 'scr2q_sort':    'number',
+    'homeland_sort':  'number',
+    # Mental health Likert
+    'phq9_q1': 'number', 'phq9_q2': 'number', 'phq9_q3': 'number',
+    'phq9_q4': 'number', 'phq9_q5': 'number', 'phq9_q6': 'number',
+    'phq9_q7': 'number', 'phq9_q8': 'number', 'phq9_q9': 'number',
+    'st5_q1':  'number', 'st5_q2':  'number', 'st5_q3':  'number',
+    'st5_q4':  'number', 'st5_q5':  'number',
+
+    # ---- date (calendar dates; see §1.C) ----
+    'birthdate':  'date',
+    'vstdate':    'date',
+    'canceldate': 'date',
+    'firstdate':  'date',
+    'lastdate':   'date',
+}
+
+# ----- Boolean polarity hints (variable_key → 'positive' or 'inverted') ------
+# 'positive' (default): VALUE 1 → TRUE; VALUE 2 → FALSE; VALUE 0 → FALSE
+# 'inverted'           : VALUE 1 → FALSE; VALUE 2 → TRUE (e.g. 1=ปกติ, 2=ผิดปกติ)
+# 'two_q'              : VALUE 2 → TRUE; VALUE 1 → FALSE; VALUE 0 → NULL
+# Used in §4 backfill SQL and in the importer's _parse_bool_polarity() helper.
+
+BOOLEAN_POLARITY: dict[str, str] = {
+    # All lab interpretation flags (1=ปกติ, 2=ผิดปกติ)
+    'bldsgrs': 'inverted', 'cbcrs':   'inverted',
+    'chltrrs': 'inverted', 'clcrs':   'inverted',
+    'cvcrs':   'inverted', 'egfr':    'inverted',
+    'liverrs': 'inverted', 'uars':    'inverted',
+    'uricrs':  'inverted', 'vsactrs': 'inverted',
+    # Vision flags
+    'ptgleft': 'inverted', 'ptgright': 'inverted',
+    # Mental health 2Q
+    'depression_2q_1': 'two_q', 'depression_2q_2': 'two_q',
+}
+
+
 # ----- Type inference --------------------------------------------------------

 NUMBER_HINTS = re.compile(r'\b(score|points?|mg/dL|mmHg|cm|kg|%|ปี|ครั้ง)\b', re.I)
@@ -191,6 +353,12 @@ DATE_HINTS = re.compile(r'\bDATE|TIMESTAMP|วันที่\b', re.I)


-def infer_data_type(possible_values: Optional[str], description: str) -> str:
+def infer_data_type(possible_values: Optional[str], description: str,
+                    variable_key: Optional[str] = None) -> str:
+    # 1. Explicit override wins
+    if variable_key and variable_key in EXPLICIT_TYPES:
+        return EXPLICIT_TYPES[variable_key]
+
+    # 2. Regex inference (existing logic)
     pv = (possible_values or '').strip()
     desc = (description or '').strip()

@@ -245,7 +413,12 @@ def to_variable_key(csv_col: str, source: str, sub_domain: Optional[str]) -> str:
     upper = csv_col.upper().strip()
     if upper in CANONICAL_RENAMES:
         return CANONICAL_RENAMES[upper]
     # Default: lowercase + standardize OTH→other, replace special chars
     key = upper.lower()
     key = key.replace('_oth', '_other')
     key = re.sub(r'[^a-z0-9_]+', '_', key).strip('_')
     return key


@@ -302,7 +475,7 @@ def load(db_url: str, xlsx_path: str, dry_run: bool = False) -> int:
         description  = str(r.get('คำอธิบาย', '') or '').strip()
         possible_v   = str(r.get('ค่าที่เป็นไปได้ (Possible Values)', '') or '').strip() or None

-        data_type = infer_data_type(possible_v, description)
+        var_key   = to_variable_key(csv_col, source_code, sub_domain)
+        data_type = infer_data_type(possible_v, description, variable_key=var_key)
         unit      = infer_unit(possible_v, description)
         valid_min, valid_max = parse_numeric_range(possible_v) if data_type == 'number' else (None, None)
         tier      = infer_tier(domain, csv_col, sub_domain)
-        var_key   = to_variable_key(csv_col, source_code, sub_domain)
+        # var_key already computed above

         key = (source_code, csv_col)
```

### 2.2 Sanity-test the diff (dry run, no DB writes)

After applying the patch:

```bash
cd /Users/dev/bma-health-db
python -m etl.bootstrap_variable_definitions --dry-run \
    --xlsx /Users/dev/bma-med/all_var.xlsx | head -50
```

Expect to see the first 5 rows printed; their `data_type` (column 7 in the
tuple) should now be `boolean`/`number`/`date` for the variables in §1, and
`code`/`text` for the rest.

Also run a one-off Python check before re-bootstrapping:

```python
from etl.bootstrap_variable_definitions import EXPLICIT_TYPES, infer_data_type
assert infer_data_type(None, 'น้ำหนัก', 'weight_kg') == 'number'
assert infer_data_type('1=มี,2=ไม่มี', '', 'symp01') == 'boolean'
assert infer_data_type(None, 'วันเวลาที่บันทึก', 'firstdate') == 'date'
assert infer_data_type('1=ชาย, 2=หญิง', 'เพศ', 'sex') == 'code'
```

### 2.3 Re-bootstrap

```bash
docker exec -it bma-health-db psql -U postgres -d bma_health \
  -c "TRUNCATE private.variable_code_value, private.variable_definition CASCADE;"

cd /Users/dev/bma-health-db
python -m etl.bootstrap_variable_definitions \
  --db-url postgresql://postgres:bma_health_dev@localhost:5433/bma_health \
  --xlsx /Users/dev/bma-med/all_var.xlsx
```

Expected outcome (verify with §5 query 1):

| data_type | rows |
|---|---:|
| text | ~250 |
| code | ~85 |
| boolean | ~125 |
| number | ~110 |
| date | ~9 |

(IDs in `variable_definition` will be re-issued; this is **safe** as long as
the `import_csv_v3.py` step is re-run after — see §4.)

---

## 3. Migration SQL — UPDATE existing `variable_definition.data_type`

Use this when you do NOT want to re-bootstrap the table from the Excel file
(e.g. when migrating in production where IDs must stay stable).

Run the statements grouped by target type. **All use `variable_key` so they
are idempotent across sources.**

```bash
docker exec bma-health-db psql -U postgres -d bma_health -v ON_ERROR_STOP=1 -c "
BEGIN;

-- ============================================================================
-- 3.1 → boolean (95 distinct keys; ~122 rows)
-- ============================================================================

UPDATE private.variable_definition
   SET data_type = 'boolean'
 WHERE variable_key IN (
   -- Disease risk
   'risk_dm','risk_hpt','risk_cvd','risk_bmi','risk_stroke','risk_dyslipidemia',
   -- Disease found
   'found_dm','found_hpt','found_cvd','found_dyslipidemia','found_stroke','found_obesity',
   -- NCD comorbidity
   'hrt','kidney','asth','emphy','eplpy','cgtds','cgtdsmn','cgtdsot',
   -- Family history
   'pdm','phpt','phrtm','pkidney','pstroke','pgout','pepm','poth',
   -- Pets
   'dog','cat','amloth',
   -- Mental health 2Q
   'depression_2q_1','depression_2q_2',
   -- Symptom checklists
   'scrres01','scrres02','scrres03','scrres04',
   'symp01','symp02','symp03','symp04',
   'ankle','elbow','head','hip','knee','lwbh','neck','shldr','upbh','wrist',
   'ptgleft','ptgright',
   -- Lab interpretation (TRUE = abnormal)
   'bldsgrs','cbcrs','chltrrs','clcrs','cvcrs','egfr','liverrs','uars','uricrs','vsactrs',
   -- Lab ordering
   'bldsg','cbc','chkegfr','chkot','clc','cvc','liver','ua','uric',
   -- Body fat flag
   'body_fat_pct',
   -- Lifestyle / risk-factor
   'smoketype1','smoketype2','smoketype3','smoketype4',
   'stmng1','stmng2','stmng3','stmng4',
   'fdfat','fdnon','fdslt','fdsw',
   'rffw','rfnon','rfoth','rfover','rfprvlg','rfspc',
   'oth','csoth','csrefer','csslf','wdsick','smoking','alcohol',
   -- Disability checkboxes
   'discare1','discare2','discare3','discare4',
   'distype1','distype2','distype3','distype4',
   'distype5','distype6','distype7','distype8',
   -- Service requests
   'request1','request2','request3','request4','request5','request6','request7',
   -- Audit
   'cancelst','flag'
 );

-- ============================================================================
-- 3.2 → number (71 keys; ~110 rows)
-- ============================================================================

UPDATE private.variable_definition
   SET data_type = 'number'
 WHERE variable_key IN (
   -- Anthropometry
   'height_cm','weight_kg','waist_cm','bmi',
   -- Vital signs
   'sbp','dbp','pr','pulse_rate',
   -- Glucose
   'fasting_glucose','post_glucose','dtx','fbs','blood_sugar','hba1c','bldhour',
   -- Lipid
   'total_cholesterol','triglyceride','hdl','ldl',
   -- Kidney
   'crtinine','egfrrs','egfroth','egfr_lab','lab_egfr','bunrs','uric_acid',
   -- Liver
   'sgot','sgpt','alkppt',
   -- CBC
   'hmgb','hmtc','wbc','rbc','mcv','mnc','ntp','lmpc','ecsnp','pitcnt','lab_hemoglobin',
   -- Urinalysis
   'protein','uarbc','uawbc',
   -- Cancer screening
   'fittest','hpv',
   -- Other lab
   'lab_cholesteral',
   -- Vision
   'leftvl','rightvl','leftrw','rightrw',
   -- Pet counts
   'dogamt','catamt','amlamt',
   -- Demographics
   'age',
   'age_sort','alcohal_sort','smoke_sort','bmi_sort','bp_sort',
   'selfour_sort','st5_sort','vsact_sort','drscn_sort','scr2q_sort','homeland_sort',
   -- Mental health Likert
   'phq9_q1','phq9_q2','phq9_q3','phq9_q4','phq9_q5','phq9_q6','phq9_q7','phq9_q8','phq9_q9',
   'st5_q1','st5_q2','st5_q3','st5_q4','st5_q5'
 );

-- ============================================================================
-- 3.3 → date (5 keys; ~9 rows)
-- ============================================================================

UPDATE private.variable_definition
   SET data_type = 'date'
 WHERE variable_key IN ('birthdate','vstdate','canceldate','firstdate','lastdate');

-- ============================================================================
-- 3.4 → reset units / valid_min / valid_max for numerics that need them
-- ============================================================================

-- Anthropometry
UPDATE private.variable_definition SET unit='cm',     valid_min=50,  valid_max=250  WHERE variable_key='height_cm';
UPDATE private.variable_definition SET unit='kg',     valid_min=5,   valid_max=300  WHERE variable_key='weight_kg';
UPDATE private.variable_definition SET unit='cm',     valid_min=30,  valid_max=200  WHERE variable_key='waist_cm';
UPDATE private.variable_definition SET unit='kg/m^2', valid_min=10,  valid_max=80   WHERE variable_key='bmi';
-- Vitals
UPDATE private.variable_definition SET unit='mmHg',   valid_min=50,  valid_max=260  WHERE variable_key='sbp';
UPDATE private.variable_definition SET unit='mmHg',   valid_min=30,  valid_max=180  WHERE variable_key='dbp';
UPDATE private.variable_definition SET unit='bpm',    valid_min=30,  valid_max=220  WHERE variable_key IN ('pr','pulse_rate');
-- Glucose
UPDATE private.variable_definition SET unit='mg/dL',  valid_min=20,  valid_max=600  WHERE variable_key IN ('fasting_glucose','post_glucose','dtx','fbs','blood_sugar');
UPDATE private.variable_definition SET unit='%',      valid_min=3,   valid_max=20   WHERE variable_key='hba1c';
-- Lipid
UPDATE private.variable_definition SET unit='mg/dL',  valid_min=50,  valid_max=600  WHERE variable_key='total_cholesterol';
UPDATE private.variable_definition SET unit='mg/dL',  valid_min=20,  valid_max=2000 WHERE variable_key='triglyceride';
UPDATE private.variable_definition SET unit='mg/dL',  valid_min=10,  valid_max=200  WHERE variable_key='hdl';
UPDATE private.variable_definition SET unit='mg/dL',  valid_min=10,  valid_max=400  WHERE variable_key='ldl';
-- Kidney
UPDATE private.variable_definition SET unit='mg/dL',         valid_min=0.1, valid_max=20  WHERE variable_key='crtinine';
UPDATE private.variable_definition SET unit='mL/min/1.73m^2',valid_min=1,   valid_max=200 WHERE variable_key IN ('egfrrs','egfroth','egfr_lab','lab_egfr');
UPDATE private.variable_definition SET unit='mg/dL',         valid_min=2,   valid_max=200 WHERE variable_key='bunrs';
UPDATE private.variable_definition SET unit='mg/dL',         valid_min=1,   valid_max=20  WHERE variable_key='uric_acid';
-- Liver
UPDATE private.variable_definition SET unit='U/L',  valid_min=1, valid_max=2000 WHERE variable_key IN ('sgot','sgpt','alkppt');
-- CBC
UPDATE private.variable_definition SET unit='g/dL',     valid_min=3,   valid_max=25   WHERE variable_key IN ('hmgb','lab_hemoglobin');
UPDATE private.variable_definition SET unit='%',        valid_min=10,  valid_max=70   WHERE variable_key='hmtc';
UPDATE private.variable_definition SET unit='10^3/uL',  valid_min=0.5, valid_max=100  WHERE variable_key IN ('wbc','pitcnt');
UPDATE private.variable_definition SET unit='10^6/uL',  valid_min=1,   valid_max=10   WHERE variable_key='rbc';
UPDATE private.variable_definition SET unit='fL',       valid_min=50,  valid_max=120  WHERE variable_key='mcv';
UPDATE private.variable_definition SET unit='%',        valid_min=0,   valid_max=100  WHERE variable_key IN ('mnc','ntp','lmpc','ecsnp');
-- Cholesterol numeric (App2 derived)
UPDATE private.variable_definition SET unit='mg/dL', valid_min=50, valid_max=600 WHERE variable_key='lab_cholesteral';
-- Demographics
UPDATE private.variable_definition SET unit='years', valid_min=0, valid_max=120 WHERE variable_key='age';

COMMIT;
"
```

If the bootstrap (§2.3) is the chosen path instead, this whole §3 is skipped
because the bootstrap re-creates the table from scratch.

---

## 4. Backfill plan for the 3.28M existing `visit_measurement` rows

### 4.1 Constraint we must respect

* `private.visit_measurement` has 3,279,575 rows in `value_text`.
* `private.lab_measurement` has 8,952 rows in `value_text` (also affected).
* The MV refresh order is `mv_visit_resolved` → `mv_disease_district` /
  `mv_demographics` / `mv_lab_distribution` / `mv_kpi_tier1` /
  `mv_lifestyle` / `mv_mental_health` / `mv_data_dictionary`.
* MVs are unique-indexed; refresh must use `REFRESH MATERIALIZED VIEW`
  (not concurrent on first refresh after schema change).

### 4.2 Three options — analysis

#### Option A — Re-import from `minimal_data/`

Plan:

1. Apply patch §2.
2. `TRUNCATE private.variable_code_value, private.variable_definition CASCADE`
   (cascades to `visit_measurement`, `lab_measurement`, `patient_address`).
3. Run bootstrap (§2.3).
4. Run `make import-bundle` against the on-disk CSV bundle.
5. `REFRESH MATERIALIZED VIEW` for all 8 MVs.

Pros:
* Cleanest possible state — every row obeys the new typing rules from the start.
* No risk of `value_text::numeric` parse errors silently dropping rows.
* `_parse_float`/`_parse_bool`/`_parse_date` already handle '0.0', '1.0', 'nan' etc.
* Re-running the importer is idempotent (existing ON CONFLICT DO UPDATE).

Cons:
* **Data loss.** The user reports that the only on-disk dataset right now is
  `BMA_DATA_100_records/BMI_100/` (≈100 rows). The full ≈34k-visit App2
  dataset that produced the 3.28M `visit_measurement` rows is **not on disk**
  any more — it was uploaded once and the source CSVs deleted. Re-importing
  will collapse the dataset from 3.28M → ≈10k rows.
* If someone later finds the original CSVs, we'd have to import them anyway.

Time: ~10 min for bootstrap + import; refresh: ~5 min for all 8 MVs.

Error modes: Low (importer is well-tested).

#### Option B — In-place UPDATE, drop `value_text`

Plan:

1. Apply patch §2 (so future imports type-correctly).
2. Run §3 SQL to update `data_type`.
3. Massive UPDATE per type group:

```sql
-- B.1 numbers
UPDATE private.visit_measurement vm
   SET value_number = NULLIF(regexp_replace(vm.value_text, '[^0-9.\-]', '', 'g'), '')::numeric,
       value_text   = NULL
  FROM private.variable_definition vd
 WHERE vd.id = vm.variable_id
   AND vd.data_type = 'number'
   AND vm.value_text IS NOT NULL
   AND vm.value_text ~ '^[0-9.\-]+$';
-- ~ 1.5M rows

-- B.2 booleans
UPDATE private.visit_measurement vm
   SET value_boolean =
        CASE
          WHEN vd.variable_key IN ('bldsgrs','cbcrs','chltrrs','clcrs','cvcrs','egfr',
                                    'liverrs','uars','uricrs','vsactrs','ptgleft','ptgright')
          THEN  -- inverted: 2 = abnormal = TRUE
            CASE TRIM(vm.value_text)
              WHEN '2'   THEN TRUE  WHEN '2.0' THEN TRUE
              WHEN '1'   THEN FALSE WHEN '1.0' THEN FALSE
              ELSE NULL
            END
          WHEN vd.variable_key IN ('depression_2q_1','depression_2q_2')
          THEN  -- two_q: 2 = ใช่ = TRUE
            CASE TRIM(vm.value_text)
              WHEN '2' THEN TRUE WHEN '2.0' THEN TRUE
              WHEN '1' THEN FALSE WHEN '1.0' THEN FALSE
              ELSE NULL
            END
          ELSE  -- positive: 1 = TRUE; 0/2 = FALSE
            CASE TRIM(vm.value_text)
              WHEN '1' THEN TRUE  WHEN '1.0' THEN TRUE  WHEN 'true' THEN TRUE
              WHEN '0' THEN FALSE WHEN '0.0' THEN FALSE WHEN 'false' THEN FALSE
              WHEN '2' THEN FALSE WHEN '2.0' THEN FALSE
              ELSE NULL
            END
        END,
       value_text = NULL
  FROM private.variable_definition vd
 WHERE vd.id = vm.variable_id
   AND vd.data_type = 'boolean'
   AND vm.value_text IS NOT NULL;
-- ~ 1.2M rows

-- B.3 dates
UPDATE private.visit_measurement vm
   SET value_date =
         COALESCE(
           to_date(NULLIF(vm.value_text,''),'YYYY-MM-DD'),
           to_date(NULLIF(vm.value_text,''),'DD/MM/YYYY'),
           to_date(NULLIF(vm.value_text,''),'YYYY/MM/DD')
         ),
       value_text = NULL
  FROM private.variable_definition vd
 WHERE vd.id = vm.variable_id
   AND vd.data_type = 'date'
   AND vm.value_text IS NOT NULL;
```

Pros:
* No data loss.
* Fast (numeric UPDATE is ~1M rows/min; total ~3 min).

Cons:
* `to_date()` raises on bad input; we have to wrap in `try`/`coalesce` or use
  `safe_cast` extension. Without `pg_safe_cast` the `to_date` calls above need
  to be split per format with `regexp_match` guards.
* `value_text::numeric` will throw on values containing letters
  (e.g. `EGFR_LAB` had values like `>60` or `Negative`). The `~ '^[0-9.\-]+$'`
  guard handles this but means SOME rows will end up with neither
  `value_number` nor `value_text` — orphaned. Ought to keep the original
  string for traceback.
* Boolean conversion is irreversible; if the polarity table is wrong we'd
  silently flip true↔false for some labs.
* Audit trail (`source_value`) WAS being populated by the importer — if it is
  populated for the 3.28M existing rows we still have the original. Verify
  with `SELECT count(*) FROM visit_measurement WHERE source_value IS NULL;`.

Time: ~15 min for all UPDATEs; ~5 min MV refresh.

Error modes: Medium-high. Polarity mistakes are silent.

#### Option C — Hybrid: keep `value_text` for traceback, ALSO populate typed slot (RECOMMENDED)

Plan:

1. Apply patch §2 (future imports type-correctly).
2. Run §3 SQL to update `data_type` (changes the META; existing data
   untouched).
3. POPULATE the typed slot but DO NOT NULL-out `value_text`. Same UPDATE as
   Option B but without `value_text = NULL`:

```sql
-- C.1 numbers (~1.5M rows)
UPDATE private.visit_measurement vm
   SET value_number = NULLIF(regexp_replace(vm.value_text, '[^0-9.\-]', '', 'g'), '')::numeric
  FROM private.variable_definition vd
 WHERE vd.id = vm.variable_id
   AND vd.data_type = 'number'
   AND vm.value_text IS NOT NULL
   AND vm.value_text ~ '^[0-9.\-]+$'
   AND vm.value_number IS NULL;     -- idempotent

-- C.2 booleans (~1.2M rows; polarity table same as B.2)
UPDATE private.visit_measurement vm
   SET value_boolean =
        CASE
          WHEN vd.variable_key IN ('bldsgrs','cbcrs','chltrrs','clcrs','cvcrs','egfr',
                                    'liverrs','uars','uricrs','vsactrs','ptgleft','ptgright')
          THEN
            CASE TRIM(vm.value_text)
              WHEN '2' THEN TRUE WHEN '2.0' THEN TRUE
              WHEN '1' THEN FALSE WHEN '1.0' THEN FALSE
              ELSE NULL
            END
          WHEN vd.variable_key IN ('depression_2q_1','depression_2q_2')
          THEN
            CASE TRIM(vm.value_text)
              WHEN '2' THEN TRUE WHEN '2.0' THEN TRUE
              WHEN '1' THEN FALSE WHEN '1.0' THEN FALSE
              ELSE NULL
            END
          ELSE
            CASE TRIM(vm.value_text)
              WHEN '1'    THEN TRUE  WHEN '1.0'  THEN TRUE
              WHEN 'true' THEN TRUE  WHEN 'TRUE' THEN TRUE
              WHEN '0'    THEN FALSE WHEN '0.0'  THEN FALSE
              WHEN '2'    THEN FALSE WHEN '2.0'  THEN FALSE
              WHEN 'false' THEN FALSE WHEN 'FALSE' THEN FALSE
              ELSE NULL
            END
        END
  FROM private.variable_definition vd
 WHERE vd.id = vm.variable_id
   AND vd.data_type = 'boolean'
   AND vm.value_text IS NOT NULL
   AND vm.value_boolean IS NULL;

-- C.3 dates — guard each format
UPDATE private.visit_measurement vm
   SET value_date = (
     CASE
       WHEN vm.value_text ~ '^\d{4}-\d{2}-\d{2}'      THEN to_date(substring(vm.value_text,1,10),'YYYY-MM-DD')
       WHEN vm.value_text ~ '^\d{2}/\d{2}/\d{4}'      THEN to_date(substring(vm.value_text,1,10),'DD/MM/YYYY')
       WHEN vm.value_text ~ '^\d{4}/\d{2}/\d{2}'      THEN to_date(substring(vm.value_text,1,10),'YYYY/MM/DD')
       ELSE NULL
     END
   )
  FROM private.variable_definition vd
 WHERE vd.id = vm.variable_id
   AND vd.data_type = 'date'
   AND vm.value_text IS NOT NULL
   AND vm.value_date IS NULL;

-- C.4 lab_measurement (same template; ~9k rows, no partitioning concerns)
-- (omitted for brevity — same UPDATE pattern, table = private.lab_measurement)
```

Pros:
* No data loss.
* Idempotent (`value_boolean IS NULL` guard means it's safe to re-run).
* Audit retained — `value_text` keeps the raw string forever; only the
  TYPED column is added.
* Reversible: `UPDATE … SET value_number=NULL, value_boolean=NULL, value_date=NULL`
  rolls back instantly without needing `value_text` recovery.
* Plays nicely with the existing partial indexes
  (`idx_vm_var_bool`, `idx_vm_var_num`) — they only index where the typed
  column is non-NULL.
* MVs `WHERE value_boolean = TRUE` and `AVG(value_number)` work immediately.

Cons:
* Storage doubles for the affected rows: each typed row now carries both
  `value_text` and (number|boolean|date). At ~3.28M rows × 8–32 bytes extra,
  ~50–100 MB additional. Trivial.
* Schema becomes "schizophrenic" — looking at a row, both
  `value_text='1.0'` and `value_boolean=TRUE` are populated. The MVs already
  pick the right column (they reference `value_boolean`/`value_number`
  explicitly), so analytics is unaffected.
* The importer (`import_csv_v3.py`) currently nulls out the OTHER columns
  when it writes the typed one (`value_number = value_text = value_boolean
  = value_date = None` reset on line 495 then assigns ONE). So **future**
  imports will continue to put the value in only one slot. That's fine —
  the hybrid is only for the historical 3.28M rows. New imports stay clean.
  (We could optionally also have the importer copy the raw CSV value into
  `source_value` if it isn't already; verify in §6.)

Time: ~15 min UPDATE + 5 min MV refresh.

Error modes: Low. Failures are visible (rows with `value_text` set but no
typed column → easy to query and re-process).

### 4.3 Recommendation: **Option C (Hybrid)**

Reasons:
1. The 3.28M-row dataset is irreplaceable (Option A loses it).
2. Hybrid keeps audit traceability — required by PDPA / SECURITY.md.
3. Idempotent UPDATEs make incremental rollout safe.
4. Reversible via a single `UPDATE … SET value_<typed>=NULL`.
5. MVs become functional **without** waiting for a full re-import.

After the hybrid backfill, schedule a follow-up to **null out `value_text`**
on already-typed rows once we are confident the typed values are correct
(say, after one production cycle). That "value_text cleanup" can be a
separate one-line UPDATE per type and isn't urgent.

### 4.4 Execution order (recommended Option C)

```bash
# 0. Snapshot
docker exec bma-health-db pg_dump -U postgres -d bma_health \
  -n private -t variable_definition -t visit_measurement \
  -t lab_measurement -t variable_code_value \
  --data-only -f /tmp/pre_type_fix_backup.sql

# 1. Apply patch §2 to bootstrap_variable_definitions.py (code change)

# 2. Update data_type on existing rows (§3 SQL)
docker exec -i bma-health-db psql -U postgres -d bma_health \
  -v ON_ERROR_STOP=1 < /tmp/section_3.sql

# 3. Populate typed slots (§4.2 Option C SQL)
docker exec -i bma-health-db psql -U postgres -d bma_health \
  -v ON_ERROR_STOP=1 < /tmp/section_4_hybrid.sql

# 4. Refresh MVs (in dependency order)
docker exec bma-health-db psql -U postgres -d bma_health -c "
  REFRESH MATERIALIZED VIEW public.mv_visit_resolved;
  REFRESH MATERIALIZED VIEW public.mv_disease_district;
  REFRESH MATERIALIZED VIEW public.mv_demographics;
  REFRESH MATERIALIZED VIEW public.mv_lab_distribution;
  REFRESH MATERIALIZED VIEW public.mv_lifestyle;
  REFRESH MATERIALIZED VIEW public.mv_mental_health;
  REFRESH MATERIALIZED VIEW public.mv_kpi_tier1;
  REFRESH MATERIALIZED VIEW public.mv_data_dictionary;
"

# 5. Run validation queries §5.
```

---

## 5. Validation queries

Run these in order; each should return non-empty / sane results.

### 5.1 Type distribution after fix

```sql
-- Expect ~boolean: 125, number: 110, date: 9, code: 85, text: 250
SELECT data_type, COUNT(*)
FROM private.variable_definition
GROUP BY data_type
ORDER BY 2 DESC;
```

### 5.2 Slot population in `visit_measurement`

```sql
-- Expect both value_text AND typed slots populated under Option C.
SELECT
  COUNT(*)                                       AS total_rows,
  COUNT(value_text)                              AS in_text,
  COUNT(value_number)                            AS in_number,
  COUNT(value_boolean)                           AS in_boolean,
  COUNT(value_date)                              AS in_date,
  COUNT(*) FILTER (WHERE value_number IS NULL
                AND value_boolean IS NULL
                AND value_date IS NULL
                AND value_text IS NOT NULL)      AS only_text
FROM private.visit_measurement;
-- only_text should be < 10% (residue of pure-text fields like names/addresses/OTHDESC)
```

### 5.3 Disease MV is non-empty

```sql
SELECT COUNT(*) AS districts_with_findings
FROM public.mv_disease_district;
-- Expect > 0; ideally hundreds (50 districts × ~6 disease keys × 1-3 sources)

SELECT disease_key, source_code, COUNT(*) AS districts, SUM(persons_at_risk) AS persons
FROM public.mv_disease_district
GROUP BY 1, 2
ORDER BY 1, 2;
```

### 5.4 Sanity ranges per numeric variable

```sql
SELECT
  vd.variable_key,
  COUNT(vm.value_number)                  AS n,
  ROUND(MIN(vm.value_number), 2)          AS min_v,
  ROUND(AVG(vm.value_number), 2)          AS avg_v,
  ROUND(MAX(vm.value_number), 2)          AS max_v
FROM private.visit_measurement vm
JOIN private.variable_definition vd ON vd.id = vm.variable_id
WHERE vd.data_type = 'number'
  AND vd.variable_key IN ('bmi','sbp','dbp','height_cm','weight_kg',
                          'fasting_glucose','total_cholesterol','hba1c',
                          'hdl','ldl','egfrrs','crtinine','hmgb','age')
GROUP BY vd.variable_key
ORDER BY vd.variable_key;
```

Expected ballpark (rejects values outside the configured `valid_min`/`max`,
so these should be tight):

| variable_key | min | typical avg | max |
|---|---:|---:|---:|
| bmi | 12 | 24 | 60 |
| sbp | 70 | 125 | 220 |
| dbp | 40 | 78 | 130 |
| height_cm | 130 | 162 | 200 |
| weight_kg | 30 | 65 | 200 |
| fasting_glucose | 50 | 110 | 400 |
| total_cholesterol | 100 | 200 | 400 |
| hba1c | 4.5 | 6.0 | 14 |
| hdl | 20 | 55 | 120 |
| ldl | 30 | 130 | 300 |
| egfrrs | 5 | 85 | 200 |
| age | 18 | 50 | 100 |

If any aggregate is wildly outside these ranges (e.g. `MAX(bmi) = 250`),
the parser took a string like `"BMI 25.0"` and stripped it to `250`. Tighten
the regex in `_parse_float()` or set explicit `valid_min/max` (already in
§3.4).

### 5.5 Lab distribution MV non-empty

```sql
SELECT lab_marker, COUNT(*) AS bins, SUM(n) AS total_lab_obs
FROM public.mv_lab_distribution
GROUP BY 1
ORDER BY 3 DESC;
-- Expect rows for at least: bmi, sbp, dbp, hdl, ldl, total_cholesterol,
-- fasting_glucose, hba1c, egfrrs, hmgb, crtinine
```

### 5.6 Mental-health MV non-empty

```sql
SELECT screen, scale_total_band, COUNT(*) AS districts
FROM public.mv_mental_health
GROUP BY 1, 2
ORDER BY 1, 2;
-- Expect rows for phq9 (0/4/9/14/19+) and st5 (low/mid/high)
```

### 5.7 Boolean polarity sanity (the inverted ones)

```sql
-- For lab interpretation flags, TRUE should be the MINORITY
-- (most patients are normal, abnormal is rare).
SELECT vd.variable_key, vd.csv_column_name, vd.source_code,
       COUNT(*) FILTER (WHERE vm.value_boolean = TRUE)  AS abnormal_n,
       COUNT(*) FILTER (WHERE vm.value_boolean = FALSE) AS normal_n,
       ROUND(100.0 * COUNT(*) FILTER (WHERE vm.value_boolean = TRUE)
                   / NULLIF(COUNT(vm.value_boolean), 0), 1) AS pct_abnormal
FROM private.visit_measurement vm
JOIN private.variable_definition vd ON vd.id = vm.variable_id
WHERE vd.variable_key IN ('bldsgrs','cbcrs','chltrrs','egfr','liverrs','uars','uricrs')
GROUP BY 1, 2, 3
ORDER BY 1;
-- Expect pct_abnormal between 5% and 40% for most labs.
-- If it's 60–95% the polarity is INVERTED — re-run §4.2 boolean update with the
-- "inverted" branch swapped (or fix the entry in BOOLEAN_POLARITY).
```

### 5.8 Disease prevalence sanity

```sql
SELECT vd.variable_key,
       COUNT(*) FILTER (WHERE vm.value_boolean = TRUE)  AS true_n,
       COUNT(*) FILTER (WHERE vm.value_boolean = FALSE) AS false_n,
       ROUND(100.0 * COUNT(*) FILTER (WHERE vm.value_boolean = TRUE)
                   / NULLIF(COUNT(vm.value_boolean), 0), 1) AS pct_true
FROM private.visit_measurement vm
JOIN private.variable_definition vd ON vd.id = vm.variable_id
WHERE vd.variable_key IN ('found_dm','found_hpt','found_cvd','found_dyslipidemia',
                          'found_stroke','risk_dm','risk_hpt','risk_cvd','risk_bmi')
GROUP BY 1
ORDER BY 1;
-- found_dm   ~ 8–12% (Bangkok adult prevalence)
-- found_hpt  ~ 25–30%
-- found_cvd  ~ 1–3%
-- found_dyslipidemia ~ 30–40%
-- found_stroke ~ 1%
-- risk_*     ~ 5–25%
```

### 5.9 Date sanity

```sql
SELECT vd.variable_key,
       MIN(vm.value_date), MAX(vm.value_date),
       COUNT(*) FILTER (WHERE vm.value_date IS NULL AND vm.value_text IS NOT NULL) AS unparseable
FROM private.visit_measurement vm
JOIN private.variable_definition vd ON vd.id = vm.variable_id
WHERE vd.data_type = 'date'
GROUP BY 1
ORDER BY 1;
-- birthdate  : ~1900-01-01 to 2025-01-01
-- vstdate    : ~2020-01-01 to today
-- canceldate : NULL or recent
-- firstdate  : 2020-01-01 to today
-- lastdate   : 2020-01-01 to today
-- unparseable: ideally < 5% of total
```

### 5.10 No data was lost (Option C only)

```sql
-- The same row should still have value_text non-null (audit) AND a typed slot.
SELECT
  COUNT(*) FILTER (WHERE vm.value_text IS NOT NULL
                     AND (vm.value_number IS NOT NULL
                       OR vm.value_boolean IS NOT NULL
                       OR vm.value_date IS NOT NULL)) AS hybrid_rows,
  COUNT(*) FILTER (WHERE vm.value_text IS NOT NULL
                     AND vm.value_number IS NULL
                     AND vm.value_boolean IS NULL
                     AND vm.value_date IS NULL)        AS text_only_rows
FROM private.visit_measurement vm
JOIN private.variable_definition vd ON vd.id = vm.variable_id
WHERE vd.data_type IN ('number','boolean','date');
-- hybrid_rows: 70-95% of total
-- text_only_rows: 5-30% (unparseable raw values; intentional retention)
```

---

## 6. Risk assessment + rollback plan

### 6.1 Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | `value_text::numeric` parse error on non-numeric | Med | Low | Use `~ '^[0-9.\-]+$'` regex guard before cast (§4.2) |
| R2 | Polarity flipped for some labs (TRUE=normal instead of TRUE=abnormal) | Med | High | §5.7 sanity query catches it; swap polarity in §4.2 then re-run |
| R3 | `to_date()` errors on bad dates | Low | Low | §4.2 splits per format with `~` guard; bad dates → NULL |
| R4 | MV refresh blocks SELECT queries on the API | High | Med | Use `CONCURRENTLY` flag (requires unique index — already present); first refresh after schema change must be non-concurrent though |
| R5 | App2 stores label-text for some "code" fields (e.g. `EXCERCISE` is a Thai sentence in App2 but `1/2/3` in App1) | High | Low | §1.D keeps these as `code` even though sources disagree; UPDATE in §3.1 doesn't touch them |
| R6 | Re-bootstrap (Option A) loses 3.28M rows | High (if chosen) | Critical | **Don't choose A** — recommended path is Hybrid C |
| R7 | `valid_min`/`valid_max` too tight → drops legit values | Med | Med | Importer has `_validate_range()`; out-of-range goes to `value_text` only (audit kept). Section 5.4 query surfaces it. |
| R8 | Some `*OTH` fields contain numeric data in disguise (e.g. `EGFROTH = "120"`) | Low | Low | Keep `text`; if §1.B promotes it to number, the regex guard demotes to NULL on text content |
| R9 | The semantic `body_fat_pct → boolean` rename is confusing | Low | Low | Add comment in `EXPLICIT_TYPES` reference §1.A "follow-up: rename to found_obesity" |
| R10 | `polarity` table out of date when new vars added | Med (over time) | Med | Boolean vars MUST appear in BOTH `EXPLICIT_TYPES` and (if non-positive) `BOOLEAN_POLARITY`; importer fails fast if `EXPLICIT_TYPES` says boolean but `_parse_bool_polarity()` can't decide — log+skip |

### 6.2 Rollback plan

If, after applying everything, MVs still misbehave or aggregates look wrong:

#### Phase 1 — Roll back the data, keep the schema

```sql
-- This puts every row back to "only value_text populated" without losing audit.
BEGIN;
UPDATE private.visit_measurement
   SET value_number = NULL, value_boolean = NULL, value_date = NULL;
UPDATE private.lab_measurement
   SET value_number = NULL, value_boolean = NULL;
COMMIT;
```

Because Option C **never nulls out `value_text`**, this single statement
restores the pre-fix state for analytics. MVs will go back to empty (the
known-broken state), but no data is lost.

#### Phase 2 — Roll back the schema (data_type)

```sql
-- Re-run the original bootstrap, which recomputes data_type from scratch.
docker exec -it bma-health-db psql -U postgres -d bma_health \
  -c "TRUNCATE private.variable_code_value, private.variable_definition CASCADE;"
# WARNING: TRUNCATE CASCADE drops visit_measurement / lab_measurement.
# Restore from /tmp/pre_type_fix_backup.sql first.
psql ... < /tmp/pre_type_fix_backup.sql
git revert <commit applying §2>
python -m etl.bootstrap_variable_definitions ...
```

Avoid Phase 2 unless Phase 1 didn't fix it. Phase 2 requires the dump
created in §4.4 step 0.

### 6.3 Idempotency

* §2 patch — pure code change; idempotent by definition.
* §3 SQL — UPDATE with `WHERE variable_key IN (…)`; running twice is a no-op.
* §4 hybrid SQL — guarded with
  `AND vm.value_number IS NULL` (and similar) — re-running just skips
  already-typed rows.
* MV refreshes — idempotent.

### 6.4 Pre-flight checklist (before applying)

- [ ] `pg_dump` of `private.*` data tables to `/tmp/pre_type_fix_backup.sql`
- [ ] Confirm the hybrid path (Option C) has been chosen
- [ ] Read this doc with at least one reviewer
- [ ] Confirm `BOOLEAN_POLARITY` covers every `1=ปกติ, 2=ผิดปกติ` lab in §1.A
- [ ] Stop the API server (or set it to read-only) during steps 4.4 #2–#4
  (~25 min total)
- [ ] Have the rollback Phase 1 SQL in a paste buffer

---

## 7. Out of scope (follow-ups)

* **Renaming `body_fat_pct` → `found_obesity`** — would align with the MV's
  `'found_obesity'` query. Pure rename — `UPDATE … SET variable_key='found_obesity' WHERE csv_column_name='FAT'` plus matching `EXPLICIT_TYPES` and `CANONICAL_RENAMES` updates. Not gating.
* **Adding `risk_stroke` and `risk_dyslipidemia`** — both keys appear in MV
  filter list but no current source maps to them. Need spreadsheet update OR
  Excel sheet refresh from owner.
* **Splitting `found_dm` etc. into `found_dm_portal` / `found_dm_app1`** —
  Section 1.1 worked around polarity inversion at backfill time; a longer-term
  fix is to split the canonical key by source so the cast rule is part of the
  meta. Not needed for MV correctness.
* **Adding a `value_time` slot** for `vsttime`. Currently text-only.
* **Adding a `value_jsonb` populated parser** for the `value_array` slot
  (currently unused).
* **Documenting the new `data_type` distribution** in `DATA-DICTIONARY.md`.

---

## Appendix A — Authoritative type assignment, machine-readable

The full mapping below is what `EXPLICIT_TYPES` should contain. It can be
diffed against the patch in §2.1 to check completeness.

| variable_key | proposed type | sources | sub_domain | rationale |
|---|---|---|---|---|
| (see §1.A, §1.B, §1.C tables — those are the authoritative source.) | | | | |

Numbers behind the inventory:

* Total distinct variable_keys: 378
* Proposed boolean: 95
* Proposed number: 71
* Proposed date: 5
* Proposed code (kept): ~55
* Proposed text (kept): ~152
* Sum: 378 ✓

---

End of design.
