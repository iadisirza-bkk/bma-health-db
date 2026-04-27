# ACTION-PLAN.md — แผนปฏิบัติการหลังตรวจฐานข้อมูล + API + Frontend

> สังเคราะห์จาก 5 รายงานตรวจวันที่ 2026-04-27
> เอกสารนี้เป็น **orchestration doc** — รายละเอียดทั้งหมดอยู่ใน 5 รายงานต้นทาง

## Reports referenced

| Report | สรุปสั้น |
|---|---|
| [DATABASE.md](DATABASE.md) | โครงสร้าง DB 64 tables + 579 variables |
| [API-AUDIT.md](API-AUDIT.md) | 170 endpoints — 47 working, 62 broken, 11 HTTP 500 |
| [ETL-TYPE-FIX-DESIGN.md](ETL-TYPE-FIX-DESIGN.md) | Patch + migration + hybrid backfill (1,730 บรรทัด) |
| [FRONTEND-API-MAP.md](../bma-health/FRONTEND-API-MAP.md) | 17 hooks → pages → 9 panels ที่แสดง 0 ปลอม |
| [CLEANUP-PROPOSAL.md](CLEANUP-PROPOSAL.md) | 9 dead tables + 4 alias views + v1 ETL |
| [ADMIN-UX-GAPS.md](ADMIN-UX-GAPS.md) | 6 critical + 8 major + 10 minor admin bugs |

---

## ภาพรวม — สิ่งที่ค้นพบ

จุดเริ่ม: ผู้ใช้ขอตรวจฐานข้อมูล → พบว่า **DB กับ API เหมือนคนละเรื่องกัน**

หลังตรวจครบ ภาพที่แท้จริง:

```
                ┌──────────────────────────────┐
                │  รากของปัญหา: data_type      │
                │  579 vars → text|code เท่านั้น │
                │  (ไม่มี boolean/number/date)  │
                └───────────┬──────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
  ┌──────────┐       ┌──────────┐       ┌──────────┐
  │ EAV ผิด  │       │ MVs ว่าง │       │ API zero │
  │  ใส่ผิด  │  →    │ disease/  │  →    │ 62/170   │
  │  column  │       │   lab     │       │ broken   │
  └──────────┘       └──────────┘       └──────────┘
                                              │
                            ┌─────────────────┴────┐
                            ▼                      ▼
                       ┌─────────┐          ┌────────────┐
                       │ 11 throw│          │ Frontend   │
                       │ HTTP 500│          │ 9 panels   │
                       │ (views  │          │ แสดง 0%   │
                       │ ไม่มี)  │          │ ปลอม      │
                       └─────────┘          └────────────┘
```

**ขอบเขตที่กระทบ:**
- 🔴 **62 endpoints** คืน 0 / array ว่าง (จาก 170)
- 🔴 **11 endpoints** throw HTTP 500 เพราะ query view ที่ไม่มีใน DB
- 🔴 **9 panels** ใน frontend แสดง 0% ปลอม + 4 panels render raw JSON
- 🟠 **6 critical + 8 major** admin UX bugs
- 🟢 **47 endpoints** ปกติ + total/visit count ทั้งหมดถูก

**ที่น่าสนใจ — สิ่งที่นึกว่าเสีย แต่จริง ๆ ปกติ:**
- Per-source DELETE (ไม่ใช่ TRUNCATE — UI text บอกผิด) ✅ ปกติ
- Bundle upload pipeline ทั้งหมด ✅ ทำงานครบ
- Patient dedup ข้าม source ✅ ทำงานครบ
- MV refresh / cache flush หลัง upload ✅ ทำงานครบ
- Frontend pages /admin, /reports, /methodology, /privacy ฯลฯ ✅ ไม่กระทบ

→ **upload pipeline ของ minimal_data "เวิค" จริง** แค่ปลายทาง (analytics layer) พัง

---

## Action Plan — 6 phases

### 🔥 Phase 0 — Hot-patch frontend (1-2 ชม.) [ทำได้ก่อน DB fix]

**เป้า:** ผู้ใช้ปัจจุบันไม่ควรเห็น 0% ปลอมระหว่างเรารอ DB fix

**งาน:**
1. เพิ่ม `NEXT_PUBLIC_DEGRADED_MODE` env var
2. ซ่อน 9 panels ตาม FRONTEND-API-MAP §5.1:
   - `OverviewBoard` 6-NCD section
   - `Header` KPI ticker disease slides
   - Sidebar disease cards (DistrictSummary, DiseaseDetail, ZoneSummary, ZoneDiseaseDetail, RankingView, CompareView)
   - Filter chips (กดแล้วเงียบ)
   - NCD heatmap pills
   - `PM25Summary`
   - `NcdCascade` stages 2-4
   - 4 panels ที่ render raw JSON (RepeatScreening, RiskFactorProfile, ExerciseFrequency, WaistRisk)

**กำกับด้วย:** หรือเปลี่ยน text เป็น "ข้อมูลกำลังปรับปรุง — รออีกสักครู่"

**Owner:** frontend dev

---

### 🚀 Phase 1 — DB Fix (วันเดียว, สำคัญที่สุด) [แก้รากเหง้า]

**เป้า:** ทำให้ measurement values อยู่ใน column ที่ถูก → MVs ใช้งานได้ → endpoints คืนค่าจริง

**ขั้นตอน (ตาม ETL-TYPE-FIX-DESIGN.md):**

1. **Backup ก่อน** — `pg_dump bma_health > backup_pre_fix.sql`

2. **Apply patch** ไปที่ `etl/bootstrap_variable_definitions.py`:
   - เพิ่ม `EXPLICIT_TYPES` lookup (95 boolean + 71 number + 5 date)
   - เพิ่ม `BOOLEAN_POLARITY` table (positive / inverted / two_q)
   - แก้ `infer_data_type()` ให้ consult lookup ก่อน regex
   - แก้ `_parse_bool()` ให้รับ polarity hint

3. **Migration UPDATE** ไปที่ `private.variable_definition.data_type`:
   ```sql
   UPDATE private.variable_definition SET data_type='boolean' WHERE variable_key IN (95 keys);
   UPDATE private.variable_definition SET data_type='number'  WHERE variable_key IN (71 keys);
   UPDATE private.variable_definition SET data_type='date'    WHERE variable_key IN (5 keys);
   ```

4. **Hybrid backfill 3.28M rows** (เก็บ `value_text` ไว้ audit + populate ใหม่):
   - boolean → `value_boolean = (value_text='1') XOR polarity_inverted`
   - number → `value_number = NULLIF(value_text, '')::numeric` (with try/catch)
   - date → `value_date = parse_date(value_text)`
   - idempotent: `WHERE value_boolean IS NULL` (etc.) ทุกครั้ง

5. **REFRESH MATERIALIZED VIEW** ทุก 8 ตัวใน `public.mv_*`

6. **Invalidate cache** — `POST /api/admin/invalidate-cache`
   ⚠️ จาก API-AUDIT — Cache TTL T1=5min, T4=24h ค่า 0 จะค้างถ้าไม่ flush

7. **Validate** — รัน 10 queries ตาม ETL-TYPE-FIX-DESIGN §5:
   - `mv_disease_district` ต้องมีแถว
   - `mv_lab_distribution` ต้องมีแถว
   - BMI range 10-50, SBP range 70-220, HBA1C range 3-15
   - prevalence sanity: DM ~10%, HPT ~25-30%

**Risk:** R1-R10 ใน design §6 — Phase 1 rollback คือ `UPDATE ... SET value_boolean = NULL` (data-only, schema unchanged)

**Owner:** DB admin / backend dev

---

### 🩹 Phase 2 — Admin Critical Bugs (ครึ่งวัน) [ทำคู่ Phase 1 ได้]

**เป้า:** แก้ 6 critical bugs ใน admin panel เพื่อให้ผู้ใช้ทำงานต่อได้

**ตาม ADMIN-UX-GAPS.md:**

| # | บั๊ก | แก้ที่ |
|---|---|---|
| C1 | History page 3 columns ว่าง (`item.timestamp` vs `started_at`) | `templates/admin/history.html:90,92,115` |
| C3 | `variable_definition` ไม่ auto-bootstrap | เพิ่ม step ใน `_run_bundle_import` |
| C4 | 4 หน้า query `raw_*` ที่ drop แล้ว | data_quality, cleansing_report, cross_stats, agreement → ทำเป็น stub หรือ migrate |
| C6 | `/admin/refresh` เรียก legacy `etl.refresh_all_summaries` | เปลี่ยนเป็น `public.refresh_all_mvs()` |
| C-* | facility ไม่ import | เรียก `etl/import_facilities.py` ก่อน Phase 0 ของ `_run_bundle_import` |
| C-* | Warning text "TRUNCATE raw_patients" ผิด | `upload_bundle.html:46-49` แก้เป็น "ลบเฉพาะ source ที่อัปโหลด — sources อื่นยังอยู่" |

**Owner:** backend dev

---

### 🧹 Phase 3 — Drop Broken Views (1-2 ชม.) [หลัง Phase 1 ผ่าน]

**เป้า:** ลบ/stub 11 endpoints ที่ throw HTTP 500 เพราะ query view ที่ migration 105 drop ไปแล้ว

**Views ที่หายไปจาก DB แต่ router ยังอ้าง:**
- `summary_disease_age_sex`
- `summary_lab_disease_cross`
- `summary_comorbidity`
- `summary_bmi_waist`
- `summary_disease_control`

**ทางเลือก 2 ทาง:**
- A) ลบ endpoint ออกจาก router (ถ้า frontend ไม่เรียก) — fast, แต่ break API contract
- B) Stub return `{"data_available": false}` — บอกผู้ใช้ชัดเจนว่ายังไม่พร้อม

ตรวจกับ FRONTEND-API-MAP §3 ว่าหน้าไหนเรียก → ถ้าไม่เรียก → ลบ; ถ้าเรียก → stub

**Owner:** backend dev

---

### 🗑️ Phase 4 — Phase 1 Cleanup (1-2 ชม.) [ทำได้ทุกเมื่อ]

**ตาม CLEANUP-PROPOSAL.md Phase 1:**

DROP ที่ปลอดภัยทันที (ไม่มี caller):
- 5 `private.visit_*` (pain, neurological, respiratory, recommendation, referral)
- 4 `private.patient_*` (attribute, chronic_history, family_history, allergy)
- 4 `public.v_*` alias views (districts, health_zones, facilities, data_sources)
- `/Users/dev/bma-health/packages/` directory (ว่างเปล่า)
- `etl/derived.py`, `etl/app2_normalizer.py` — extract `refresh_all_summaries()` 10 บรรทัดออกจาก `import_csv.py` แล้วลบที่เหลือ
- `tests/test_etl_parsers.py`

**ห้าม drop ในเฟสนี้:**
- `public.raw_*` (7 ตาราง) — มี 275 callers ใน 20+ ไฟล์ ต้อง refactor ก่อน → Phase 5

**Owner:** DB admin

---

### 🏗️ Phase 5 — Refactor `raw_*` consumers (สัปดาห์) [optional, low priority]

**ตาม CLEANUP-PROPOSAL.md Phase 3:**

Migrate 275 references ของ `public.raw_*` ใน:
- `routers/monitoring.py`
- `routers/research.py`
- `routers/promotion.py`
- `api/admin.py`
- frontend Research page

ให้อ่านจาก `private.*` หรือ `public.mv_*` แทน → แล้วค่อย DROP `raw_*` (Phase 6)

**Optional** — ถ้าผู้ใช้ตัดสินใจว่าให้ความสำคัญต่ำ ก็ค้าง raw_* ไว้ก็ได้ (ไม่กระทบ functionality เพราะตารางว่างอยู่แล้ว)

**Owner:** backend dev

---

### 🎨 Phase 6 — Frontend Hardening (สัปดาห์) [optional]

**ตาม FRONTEND-API-MAP.md:**

1. Centralize 11 sidebar widgets ที่ bypass hook layer (Appendix A) → ใช้ `useV2Query` ทุกที่
2. Fix `ScreeningCoverage.tsx` array-vs-object bug (pre-existing — API คืน 50 districts แต่ component คาด single object)
3. Server-driven feature flags `/api/v2/health/feature-flags` (แทน `NEXT_PUBLIC_DEGRADED_MODE` build-time)
4. WS5 quick wins (10 ตัว < 30 นาที/ตัว — UX polish)

**Owner:** frontend dev

---

## ลำดับ Critical Path

```
Phase 0 (frontend hot-patch)  ──┐
                                ├──► Phase 1 (DB fix)  ──► Phase 3 (drop broken views)
Phase 2 (admin bugs)        ────┘                       └──► Phase 4 (cleanup)
                                                              │
                                                              ▼
                                                         Phase 5 (raw_* refactor)
                                                              │
                                                              ▼
                                                         Phase 6 (frontend polish)
```

**Minimum viable fix (1 วัน):** Phase 0 + Phase 1 + Phase 3 → endpoints ส่วนใหญ่กลับมาทำงาน, frontend ไม่แสดงค่าปลอม

**Production-ready (3-5 วัน):** + Phase 2 + Phase 4

**Long-term healthy (2 สัปดาห์):** + Phase 5 + Phase 6

---

## Verification Checklist หลัง Phase 1 + 3

```bash
# 1. Disease MV ต้องมีแถว
docker exec bma-health-db psql -U postgres -d bma_health -c \
  "SELECT disease_key, SUM(persons_at_risk) FROM mv_disease_district GROUP BY disease_key ORDER BY 1;"
# ควรเห็น risk_dm, risk_hpt, ... พร้อมตัวเลข > 0

# 2. Overview API ต้องคืน prevalence != 0
curl -s -H "X-API-Key: dev-api-key" http://localhost:9002/api/v2/summary/overview | jq '.by_disease'
# ควรเห็น "pct" != 0 ทุก disease

# 3. Lab MV ต้องมีแถว
docker exec bma-health-db psql -U postgres -d bma_health -c \
  "SELECT COUNT(*) FROM mv_lab_distribution;"
# ควร > 0

# 4. ไม่มี endpoint ไหนยัง throw 500
for ep in /api/v2/epidemiology/multi-disease-matrix /api/v2/epidemiology/age-pyramid \
          /api/v2/promotion/behavior-disease-correlation; do
  echo -n "$ep: "
  curl -s -o /dev/null -w "%{http_code}\n" -H "X-API-Key: dev-api-key" \
    "http://localhost:9002$ep"
done
# ควรเห็น 200 ทุกตัว (ถ้าเลือกทาง stub) หรือ 404 (ถ้าเลือกลบ)

# 5. Frontend
open http://localhost:3000
# ตรวจ OverviewBoard 6-NCD bars ไม่เป็น 0% หมด
# ตรวจ KPI ticker ไม่หมุนสไลด์ "0.0%"
# ตรวจ heatmap มีสี
```

---

## Open Questions ก่อนเริ่ม Phase 1

1. **App2 dataset 34k visits** — ปัจจุบันอยู่ใน DB แต่ไม่มีใน disk → Hybrid backfill (เก็บ value_text ไว้) จะใช้ได้ไหม? ถ้า user มี backup CSV ของ app2 ก็ Re-import ได้สะอาดกว่า
2. **Polarity inversion** — ETL-TYPE-FIX-DESIGN §1.1 ระบุว่า DM/HPT/CHLTR/STROKE/HRT/KIDNEY มี encoding ต่างกัน 7 แบบข้าม source → user ยืนยันได้ไหมว่ารายการนั้นถูก?
3. **`risk_stroke`/`risk_dyslipidemia`** — ไม่มีข้อมูลต้นทางเลย — สรุปว่าจะคำนวณ derived จาก lab values หรือจะลบออก?
4. **`body_fat_pct` rename** — เป็น `found_obesity` ตามที่ design เสนอ จะกระทบ frontend ตรงไหน?
5. **Drop dashboard_v1/statistics_v1 routers** — frontend ไม่เรียกแต่ test ยังคลุม → ลบ test ด้วยเลยไหม?

---

## ไฟล์ทั้งหมดที่อ้างใน plan นี้

```
bma-health-db/
├── DATABASE.md                  ← โครงสร้าง DB
├── API-AUDIT.md                 ← 170 endpoints
├── ETL-TYPE-FIX-DESIGN.md       ← Phase 1 spec (1730 lines)
├── CLEANUP-PROPOSAL.md          ← Phase 4-5 spec
├── ADMIN-UX-GAPS.md             ← Phase 2 spec
└── ACTION-PLAN.md               ← THIS FILE

bma-health/
└── FRONTEND-API-MAP.md          ← Phase 0 + Phase 6 spec
```
