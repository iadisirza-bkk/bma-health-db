# BMA Health — Database Backend & Summary API

## Requirement Specification v1.1

> **วันที่**: 2026-04-08 (updated)
> **สถาปัตยกรรม**: `On-Premise Database` → `Summary MCP/API (Security Gateway)` → `BMA Health Web`
> **หลักการ**: ข้อมูลทั้งหมดอยู่ **on-premise เท่านั้น** ห้ามขึ้น cloud — Web frontend เข้าถึงได้เฉพาะ **summary/aggregate** ผ่าน MCP + API ที่ผ่านการพิสูจน์ตัวตน ไม่มีข้อมูลรายบุคคลหลุดออกไปได้

---

## 0. Deployment Constraint — ON-PREMISE ONLY

> **ข้อมูลสุขภาพเป็นข้อมูลอ่อนไหว (Sensitive Personal Data) ตาม พ.ร.บ.คุ้มครองข้อมูลส่วนบุคคล พ.ศ. 2562 (PDPA)**
>
> - Database, API, Redis, ETL, Agent — ทุก component ต้อง deploy บน **on-premise server ของ กทม.** หรือ **private data center** เท่านั้น
> - **ห้ามใช้ cloud service** ใด ๆ สำหรับเก็บหรือประมวลผลข้อมูลรายบุคคล (AWS, GCP, Azure, Cloudflare, Supabase, Neon ฯลฯ)
> - BMA Health Web (frontend) อาจ host บน CDN/cloud ได้เพราะเป็น static + เรียก Summary API เท่านั้น
> - LLM inference อาจใช้ external API ได้ **ก็ต่อเมื่อ** ส่งเฉพาะ aggregate data ไม่มี PII (ดู Section 1.3)

---

## 1. Architecture Overview

```
                      ON-PREMISE (กทม. Data Center / Private Server)
┌──────────────────────────────────────────────────────────────────────────────┐
│                                                                              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌──────────────┐  │
│  │ PostgreSQL   │───▶│ Aggregation │───▶│ Summary DB  │───▶│ Summary      │  │
│  │ (Raw PII)    │    │ Engine      │    │ (Views only)│    │ MCP Server   │  │
│  │ ENCRYPTED    │    │ (Agents)    │    │ No PII      │    │ (Port 3100)  │  │
│  └─────────────┘    └─────────────┘    └─────────────┘    └──────┬───────┘  │
│         │                                                         │          │
│  ┌─────────────┐    ┌──────────────────────────────────┐         │          │
│  │ ETL Pipeline │    │ Security Gateway                 │         │          │
│  │ (CSV Import) │    │ ┌────────────────┐ ┌───────────┐│         │          │
│  └─────────────┘    │ │ Auth (mTLS/JWT)│ │Rate Limit ││─────────┤          │
│                      │ └────────────────┘ └───────────┘│         │          │
│  ┌─────────────┐    │ ┌────────────────┐ ┌───────────┐│         │          │
│  │ Redis Cache  │    │ │ k-Anonymity   │ │Audit Log  ││         │          │
│  │ (Summary)    │    │ │ Enforcer      │ │(tamper-   ││         │          │
│  └─────────────┘    │ └────────────────┘ │ proof)    ││         │          │
│                      └──────────────────────┴───────────┘         │          │
│                                                                    │          │
└────────────────────────────────────────────────────────────────────┼──────────┘
                                                                     │
         ┌─── firewall / reverse proxy (nginx) ── only port 443 ────┤
         │                                                           │
         ▼                                                           ▼
┌─────────────────┐                                    ┌──────────────────────┐
│ BMA Health Web  │   GET /api/v2/summary/*            │ LLM Agent (Claude)   │
│ (Static, CDN OK)│──────────────────────────────────▶ │ connects via MCP     │
│ No PII ever     │   Only aggregate JSON              │ sees ONLY summary    │
└─────────────────┘                                    │ NEVER raw/individual │
                                                        └──────────────────────┘
```

### 1.1 Security Boundaries — 4 Zones

| Zone | Network | Access | Contains |
|------|---------|--------|----------|
| **Zone 0 — Raw Data** | Air-gapped VLAN, on-premise only | ETL service account only | PostgreSQL (encrypted at rest), raw PII, IDCARD hashes |
| **Zone 1 — Summary Engine** | Internal VLAN, on-premise | Aggregation Agents, Admin | Materialized views (no PII), Redis cache, Audit logs |
| **Zone 2 — API Gateway** | DMZ, on-premise | Authenticated clients (mTLS/JWT) | Summary MCP Server, REST API, Rate limiter, k-Anonymity enforcer |
| **Zone 3 — Public** | Internet (CDN allowed) | Anyone | BMA Health Web (static), receives only aggregate JSON |

### 1.2 Core Principle — No Individual Data Leakage

- **Web frontend (Zone 3)** ไม่สามารถเข้าถึงข้อมูลรายบุคคลได้ไม่ว่าจะทางใดทั้งสิ้น
- **ไม่มี endpoint** ที่ return record เดียว — ทุก response ต้องเป็น aggregate (count, percentage, average, range)
- **k-anonymity ≥ 5**: ถ้า group มีคนน้อยกว่า 5 คน → suppress หรือ merge กับ group ใกล้เคียง
- PID/IDCARD ต้อง **ไม่ปรากฏ** ใน API response, log, error message, หรือ LLM prompt
- **ไม่มี SQL passthrough** — ไม่มี endpoint ที่รับ SQL query จากภายนอก
- **ไม่มี export** ข้อมูลรายบุคคลผ่าน API — export ได้เฉพาะ aggregate report (PDF)

### 1.3 LLM / AI Agent Security

LLM (Claude API) ทำงานเป็น external service — ต้องมั่นใจว่า:

```
                    ┌───────────────────────────────┐
                    │   WHAT LLM SEES (allowed)     │
                    │                               │
                    │ • "เขตบางแค เบาหวาน 32.4%"    │
                    │ • "โซน 1 คัดกรอง 96,083 คน"   │
                    │ • "avg FBS = 105.2 mg/dL"     │
                    │ • chart data: [{zone, pct}]   │
                    └───────────────────────────────┘

                    ┌───────────────────────────────┐
                    │   WHAT LLM NEVER SEES (blocked)│
                    │                               │
                    │ ✗ IDCARD / PID                 │
                    │ ✗ ชื่อ-นามสกุล                   │
                    │ ✗ ที่อยู่ / เบอร์โทร / Line ID   │
                    │ ✗ ผลตรวจรายบุคคล                │
                    │ ✗ Raw SQL query results        │
                    └───────────────────────────────┘
```

- LLM Agent เข้าถึง database ผ่าน **Summary MCP Server เท่านั้น**
- MCP Server query ได้เฉพาะ **materialized views** (summary_district_*, summary_zone_*)
- MCP Server **ปฏิเสธ** ทุก query ที่พยายามเข้าถึง raw_* tables
- ทุก MCP tool call ถูก log ใน tamper-proof audit log

---

## 1.4 Summary MCP Server Specification

MCP Server ที่เป็น **ประตูเดียว** ระหว่าง LLM Agent กับข้อมูล:

### MCP Tools (ที่ LLM Agent เรียกได้)

```typescript
// Tool 1: ดึง overview ทั้ง กทม.
mcp__bma_health__get_overview()
→ { total_screened, target, zones: 8, districts: 50, last_updated }

// Tool 2: ดึงข้อมูลรายโซน
mcp__bma_health__get_zone_summary(zone_code: string)
→ { zone_code, name_th, total_screened, districts: [...], diseases: {...} }

// Tool 3: ดึงข้อมูลรายเขต
mcp__bma_health__get_district_summary(dcode: string)
→ { dcode, name_th, zone_code, total_screened, diseases, lab_summary, mental_health }

// Tool 4: เปรียบเทียบโรคข้ามเขต/โซน
mcp__bma_health__compare_disease(disease_key: string, level: "zone"|"district", codes?: string[])
→ [{ code, name_th, pct_at_risk, total_screened, rank }]

// Tool 5: ดึงข้อมูลตาม risk factor filter
mcp__bma_health__get_filtered_summary(filters: { dcode?, sex?, age_group?, smoking?, exercise? })
→ { filtered aggregate — k-anonymity enforced }
→ ERROR if result group < 5 people

// Tool 6: ดึง trend data
mcp__bma_health__get_trend(disease_key: string, dcode?: string, granularity: "monthly"|"quarterly")
→ [{ period, pct_at_risk, total_screened }]

// Tool 7: ดึงข้อมูล lab summary
mcp__bma_health__get_lab_summary(dcode?: string, zone_code?: string)
→ { avg_hemoglobin, avg_fbs, avg_cholesterol, pct_anemia, pct_ckd, ... }

// Tool 8: ดึง mental health summary
mcp__bma_health__get_mental_health_summary(dcode?: string, zone_code?: string)
→ { pct_depression_risk, pct_phq9_moderate, pct_high_stress }

// Tool 9: ดึงข้อมูล demographic summary
mcp__bma_health__get_demographics(dcode?: string, zone_code?: string)
→ { education_breakdown, occupation_breakdown, privilege_breakdown, housing_breakdown }

// Tool 10: ค้นหาเขต/โซนตามเกณฑ์
mcp__bma_health__search_districts(query: { disease?, min_pct?, max_pct?, sort_by?, limit? })
→ [{ dcode, name_th, zone_code, matching_value }]
```

### MCP Server Security Rules

```python
class BMAHealthMCPServer:
    """
    MCP Server สำหรับ LLM Agent — เข้าถึงได้เฉพาะ summary data
    """

    ALLOWED_TABLES = [
        'summary_district_disease',
        'summary_district_risk_factors',
        'summary_district_lab',
        'summary_district_mental',
        'summary_district_demographics',
        'ref_health_zones',
        'ref_districts',
        'ref_facilities',
    ]

    BLOCKED_TABLES = [
        'raw_patients', 'raw_visits', 'raw_vitalsigns',
        'raw_homevisit', 'raw_homehealth', 'raw_lab_results',
        'raw_lab_extended',
    ]

    BLOCKED_COLUMNS = [
        'idcard_hash', 'patient_id', 'staff_code',
        'firststf', 'laststf', 'cancelstf',
    ]

    def validate_query(self, sql: str) -> bool:
        """ตรวจสอบทุก query ก่อน execute"""
        sql_lower = sql.lower()

        # Block any reference to raw tables
        for table in self.BLOCKED_TABLES:
            if table in sql_lower:
                raise SecurityError(f"Access denied: table '{table}' is restricted")

        # Block any reference to PII columns
        for col in self.BLOCKED_COLUMNS:
            if col in sql_lower:
                raise SecurityError(f"Access denied: column '{col}' is restricted")

        # Block DDL / DML
        for keyword in ['insert', 'update', 'delete', 'drop', 'alter', 'create', 'truncate']:
            if keyword in sql_lower.split():
                raise SecurityError(f"Access denied: '{keyword}' is not allowed")

        # Must be SELECT only
        if not sql_lower.strip().startswith('select'):
            raise SecurityError("Only SELECT queries are allowed")

        return True

    def enforce_k_anonymity(self, results: list, k: int = 5) -> list:
        """Suppress groups smaller than k"""
        return [
            {**r, 'total': '<5', 'suppressed': True}
            if r.get('total', float('inf')) < k
            else r
            for r in results
        ]
```

### MCP Transport & Authentication

```yaml
# MCP Server config
server:
  transport: stdio          # local stdio (same machine) — preferred
  # OR
  transport: sse            # SSE over HTTPS (if agent runs on different machine)
  host: 127.0.0.1           # localhost only — not exposed to network
  port: 3100
  tls: true                 # mandatory if using SSE
  mutual_tls: true          # client cert required

auth:
  type: api_key + client_cert
  allowed_clients:
    - name: "chat_agent"
      permissions: ["read_summary"]
    - name: "report_agent"
      permissions: ["read_summary", "read_lab_summary"]
    - name: "admin_cli"
      permissions: ["read_summary", "refresh_views"]

audit:
  log_every_call: true
  log_destination: /var/log/bma-health/mcp-audit.jsonl
  include: [timestamp, client, tool, params, result_row_count]
  exclude: [result_data]    # don't log actual data in audit
  tamper_protection: sha256_chain  # each log entry hashes previous
```

---

## 2. Database Schema

### 2.1 Source Tables (Raw — Secure Zone Only)

ข้อมูลตาม `minimal_data` format ทั้งหมด 7 tables:

#### 2.1.1 `raw_patients` (จาก pt)

| Column | Type | Description |
|--------|------|-------------|
| id | BIGSERIAL PK | Internal surrogate key |
| idcard_hash | VARCHAR(64) | SHA-256 of IDCARD (ไม่เก็บ plaintext) |
| notype | SMALLINT | ประเภทบัตร (10=ประชาชน, 20=ต่างด้าว, 30=passport) |
| pname | SMALLINT | คำนำหน้าชื่อ |
| sex | SMALLINT | เพศ (10=ชาย, 20=หญิง) |
| birth_year | SMALLINT | ปีเกิด (ไม่เก็บวันเต็ม — ลด PII) |
| age_group | VARCHAR(10) | คำนวณจาก birthdate → '15-24', '25-34', '35-44', '45-54', '55-64', '65+' |
| created_at | TIMESTAMPTZ | firstdate |
| updated_at | TIMESTAMPTZ | lastdate |

> **Note**: ชื่อ-นามสกุล (FNAME, LNAME, EFNAME, ELNAME) ไม่ import เข้า database — ใช้ idcard_hash เป็น key แทน

#### 2.1.2 `raw_visits` (จาก pthistory)

| Column | Type | Description |
|--------|------|-------------|
| id | BIGSERIAL PK | |
| patient_id | BIGINT FK | → raw_patients.id |
| visit_date | DATE | vstdate |
| facility_code | VARCHAR(10) | hptcode (รหัสสถานพยาบาล) |
| religion | SMALLINT | rlgn |
| lgbtq | SMALLINT | lgbtq (1=ไม่ใช่, 2=ใช่, 3=ไม่ระบุ) |
| cancel_status | SMALLINT | cancelst |
| staff_code | VARCHAR(20) | firststf |
| created_at | TIMESTAMPTZ | |

#### 2.1.3 `raw_vitalsigns` (จาก vitalsignslf)

| Column | Type | Description |
|--------|------|-------------|
| id | BIGSERIAL PK | |
| patient_id | BIGINT FK | → raw_patients.id |
| visit_date | TIMESTAMPTZ | vstdate |
| facility_code | VARCHAR(10) | hptcode |
| sbp | SMALLINT | hbpn — ความดันตัวบน |
| dbp | SMALLINT | lbpn — ความดันตัวล่าง |
| fasting_glucose | DECIMAL(6,1) | prefpg — น้ำตาลขณะอดอาหาร |
| post_glucose | DECIMAL(6,1) | postfpg — น้ำตาลหลังอาหาร |
| height_cm | DECIMAL(5,1) | height |
| weight_kg | DECIMAL(5,1) | weight |
| waist_cm | DECIMAL(5,1) | wstl — รอบเอว |
| pulse_rate | SMALLINT | pr |
| smoking | SMALLINT | smoke (0=ไม่, 1=สูบ, 2=เลิก) |
| alcohol | SMALLINT | alcohal (0=ไม่, 1=ดื่ม, 2=เลิก) |
| chest_xray | SMALLINT | chest (0=ไม่ตรวจ, 1=ปกติ, 2=ผิดปกติ) |
| ekg | SMALLINT | ekg |
| vision | SMALLINT | vsact |
| dr_screening | SMALLINT | drscn — DR screening ตา |
| depression_2q_1 | SMALLINT | scr2q1 |
| depression_2q_2 | SMALLINT | scr2q2 |
| phq9_q1..q9 | SMALLINT | scn9q1..scn9q9 |
| st5_q1..q5 | SMALLINT | st501..st505 |
| screening_result | SMALLINT | scrrs (1=ปกติ, 2=เสี่ยง) |
| risk_dm | BOOLEAN | riskdm |
| risk_hpt | BOOLEAN | riskhpt |
| risk_cvd | BOOLEAN | riskcdvcl |
| risk_bmi | BOOLEAN | riskbmi |
| found_dm | BOOLEAN | dm |
| found_hpt | BOOLEAN | hpt |
| found_cvd | BOOLEAN | cdvcl |
| found_stroke | BOOLEAN | stroke |
| found_obesity | BOOLEAN | fat |
| found_dyslipidemia | BOOLEAN | chltr |
| found_other | BOOLEAN | oth |
| family_dm | SMALLINT | dmfm (1=มี, 2=ไม่มี, 3=ไม่ทราบ) |
| district_code | VARCHAR(4) | districtbkk |
| location_code | VARCHAR(10) | location (สถานที่ตรวจ) |
| referral_type | VARCHAR(20) | computed from rf* fields |
| stress_management | SMALLINT[] | computed from stmng1-4 |
| cancel_status | SMALLINT | cancelst |

#### 2.1.4 `raw_homevisit` (จาก homevisit)

| Column | Type | Description |
|--------|------|-------------|
| id | BIGSERIAL PK | |
| patient_id | BIGINT FK | → raw_patients.id |
| visit_date | TIMESTAMPTZ | vstdate |
| facility_code | VARCHAR(10) | hptcode |
| self_care | SMALLINT | selfour (1=ได้, 2=ได้บางส่วน, 3=ไม่ได้) |
| disability_types | SMALLINT[] | distype1-8 as array |
| education | SMALLINT | edu |
| occupation | SMALLINT | occptn |
| home_province | INTEGER | province (ทะเบียนบ้าน) |
| home_district | INTEGER | district |
| home_subdistrict | INTEGER | subdistrict |
| home_type | SMALLINT | hometype |
| health_privilege | SMALLINT | prvlg — สิทธิการรักษา |
| current_province | INTEGER | crprovince |
| current_district | INTEGER | crdistrict |
| work_district | INTEGER | wrkdistrict |
| work_type | SMALLINT | wrktype |
| work_journey | SMALLINT | wrkjourney — การเดินทาง |
| health_facility_used | SMALLINT | healthuse |
| service_requests | SMALLINT[] | request1-7 as array |
| workshop_willing | SMALLINT | workshop |
| cancel_status | SMALLINT | cancelst |

#### 2.1.5 `raw_homehealth` (จาก homehealth)

| Column | Type | Description |
|--------|------|-------------|
| id | BIGSERIAL PK | |
| patient_id | BIGINT FK | → raw_patients.id |
| visit_date | TIMESTAMPTZ | vstdate |
| facility_code | VARCHAR(10) | hptcode |
| has_chronic | SMALLINT | cgtds |
| history_dm | SMALLINT | dm (1=เป็น, 2=ไม่เป็น, 3=ไม่เคยตรวจ) |
| history_hpt | SMALLINT | hpt |
| history_stroke | SMALLINT | stroke |
| history_dyslipidemia | SMALLINT | chltr |
| history_heart | SMALLINT | hrt |
| history_kidney | SMALLINT | kidney |
| dm_treatment | SMALLINT | dmrs (1=รักษาอยู่, 2=ไม่สม่ำเสมอ, 3=ซื้อยาเอง, 4=ไม่รักษา) |
| hpt_treatment | SMALLINT | hptrs |
| dyslipidemia_treatment | SMALLINT | chltrrs |
| heart_treatment | SMALLINT | hrtrs |
| kidney_treatment | SMALLINT | kidneyrs |
| stroke_treatment | SMALLINT | strokers |
| parent_history | SMALLINT | parent (1=มี, 2=ไม่มี, 3=ไม่ทราบ) |
| parent_dm | BOOLEAN | pdm |
| parent_kidney | BOOLEAN | pkidney |
| parent_stroke | BOOLEAN | pstroke |
| parent_hpt | BOOLEAN | phpt |
| parent_heart_attack | BOOLEAN | phrtm |
| parent_gout | BOOLEAN | pgout |
| parent_emphysema | BOOLEAN | pepm |
| exercise | SMALLINT | excercise (1=≥3/wk, 2=<3/wk, 3=ไม่เลย) |
| food_preference_sweet | BOOLEAN | fdsw |
| food_preference_salty | BOOLEAN | fdslt |
| food_preference_fatty | BOOLEAN | fdfat |
| food_fried_freq | SMALLINT | food (1-4) |
| drink_sugar_freq | SMALLINT | water (1-4) |
| instant_noodle_freq | SMALLINT | noodle (1-4) |
| allergy_food | SMALLINT | algyfood |
| allergy_medicine | SMALLINT | algymed |
| covid_history | SMALLINT | covid |
| vaccine_covid | SMALLINT | vcccovid |
| vaccine_influenza | SMALLINT | vccinfluza |
| want_hiv_test | SMALLINT | chkhiv |
| cancel_status | SMALLINT | cancelst |

#### 2.1.6 `raw_lab_results` (จาก labhealth)

| Column | Type | Description |
|--------|------|-------------|
| id | BIGSERIAL PK | |
| patient_id | BIGINT FK | → raw_patients.id |
| visit_date | DATE | vstdate |
| facility_code | VARCHAR(10) | hptcode |
| privilege | SMALLINT | prvlg — สิทธิการรักษา |
| cbc_result | SMALLINT | cbcrs (1=ปกติ, 2=ผิดปกติ) |
| wbc | INTEGER | wbc |
| rbc | INTEGER | rbc |
| hemoglobin | DECIMAL(5,1) | hmgb |
| hematocrit | DECIMAL(5,1) | hmtc |
| mcv | DECIMAL(5,1) | mcv |
| platelet | INTEGER | pitcnt |
| blood_sugar_type | SMALLINT | bldsgtype (1=งดอาหาร, 2=หลังอาหาร) |
| blood_sugar_result | SMALLINT | bldsgrs |
| dtx | DECIMAL(6,1) | dtx |
| blood_sugar | DECIMAL(6,1) | bldsugar |
| fbs | DECIMAL(6,1) | fbs — fasting blood sugar |
| urine_result | SMALLINT | uars |
| urine_wbc | VARCHAR(10) | uawbc |
| urine_rbc | VARCHAR(10) | uarbc |
| urine_protein | VARCHAR(20) | protein |
| cholesterol_type | SMALLINT | chltrtype |
| cholesterol_result | SMALLINT | chltrrs |
| cholesterol | DECIMAL(6,1) | cholest |
| triglyceride | DECIMAL(6,1) | trigly |
| hdl | DECIMAL(6,1) | hdl |
| ldl | DECIMAL(6,1) | ldl |
| liver_result | SMALLINT | liverrs |
| sgot | DECIMAL(6,1) | sgot |
| sgpt | DECIMAL(6,1) | sgpt |
| alk_phosphatase | DECIMAL(6,1) | alkppt |
| uric_acid_result | SMALLINT | uricrs |
| uric_acid | DECIMAL(5,2) | uricacid |
| cervical_cancer_result | SMALLINT | cvcrs |
| hpv_result | VARCHAR(20) | hpv |
| colorectal_result | SMALLINT | clcrs |
| fit_test | VARCHAR(20) | fittest |
| creatinine | DECIMAL(5,2) | crtinine |
| egfr | DECIMAL(6,2) | egfrrs |
| bun | DECIMAL(5,1) | bunrs |
| cancel_status | SMALLINT | cancelst |

#### 2.1.7 `raw_lab_extended` (จาก labhealthext)

| Column | Type | Description |
|--------|------|-------------|
| id | BIGSERIAL PK | |
| patient_id | BIGINT FK | → raw_patients.id |
| visit_date | DATE | vstdate |
| facility_code | VARCHAR(10) | hptcode |
| respiratory_cough | SMALLINT | scrres01 — ไอเรื้อรัง |
| respiratory_dyspnea | SMALLINT | scrres02 — หอบเหนื่อย |
| respiratory_chest_tight | SMALLINT | scrres03 — แน่นหน้าอก |
| respiratory_breathing | SMALLINT | scrres04 — หายใจลำบาก |
| hearing_test | SMALLINT | fgrub01 — finger rub test |
| pterygium_right | SMALLINT | ptgright — ต้อเนื้อ ตาขวา |
| pterygium_left | SMALLINT | ptgleft — ต้อเนื้อ ตาซ้าย |
| pain_head | BOOLEAN | head |
| pain_neck | BOOLEAN | neck |
| pain_shoulder | BOOLEAN | shldr |
| pain_upper_back | BOOLEAN | upbh |
| pain_elbow | BOOLEAN | elbow |
| pain_lower_back | BOOLEAN | lwbh |
| pain_wrist | BOOLEAN | wrist |
| pain_hip | BOOLEAN | hip |
| pain_knee | BOOLEAN | knee |
| pain_ankle | BOOLEAN | ankle |
| symptom_neck_radiating | BOOLEAN | symp01 |
| symptom_hand_numbness | BOOLEAN | symp02 |
| symptom_back_radiating | BOOLEAN | symp03 |
| symptom_heel_pain | BOOLEAN | symp04 |
| cancel_status | SMALLINT | cancelst |

### 2.2 Reference Tables

```sql
-- เขตสุขภาพ
CREATE TABLE ref_health_zones (
  zone_code    VARCHAR(2) PRIMARY KEY,
  name_th      VARCHAR(50) NOT NULL,
  name_en      VARCHAR(50) NOT NULL,
  facilitator  VARCHAR(100),  -- รพ.ประจำโซน
  mentor       TEXT,           -- Mentor System
  area_manager_count INTEGER
);

-- เขต (50 เขต)
CREATE TABLE ref_districts (
  dcode        VARCHAR(4) PRIMARY KEY,
  zone_code    VARCHAR(2) REFERENCES ref_health_zones(zone_code),
  name_th      VARCHAR(50) NOT NULL,
  name_en      VARCHAR(50),
  population   INTEGER
);

-- สถานพยาบาล / ศูนย์บริการสาธารณสุข
CREATE TABLE ref_facilities (
  code         VARCHAR(10) PRIMARY KEY,
  name_th      VARCHAR(100) NOT NULL,
  name_en      VARCHAR(100),
  facility_type VARCHAR(20),  -- 'hospital', 'health_center', 'clinic'
  zone_code    VARCHAR(2) REFERENCES ref_health_zones(zone_code),
  district_code VARCHAR(4) REFERENCES ref_districts(dcode),
  latitude     DECIMAL(10,7),
  longitude    DECIMAL(10,7)
);
```

### 2.3 Materialized Summary Tables (Pre-aggregated)

```sql
-- สรุปรายเขต-รายโรค (หลักที่ frontend ใช้)
CREATE MATERIALIZED VIEW summary_district_disease AS
SELECT
  d.dcode,
  d.zone_code,
  d.name_th AS district_name_th,
  d.name_en AS district_name_en,
  COUNT(DISTINCT v.patient_id) AS total_screened,

  -- Demographics breakdown
  COUNT(DISTINCT v.patient_id) FILTER (WHERE p.sex = 10) AS male_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE p.sex = 20) AS female_count,

  -- Disease risk percentages
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.risk_dm) / NULLIF(COUNT(*), 0), 1) AS pct_risk_dm,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.risk_hpt) / NULLIF(COUNT(*), 0), 1) AS pct_risk_hpt,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.risk_cvd) / NULLIF(COUNT(*), 0), 1) AS pct_risk_cvd,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.risk_bmi) / NULLIF(COUNT(*), 0), 1) AS pct_risk_bmi,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.found_dm) / NULLIF(COUNT(*), 0), 1) AS pct_found_dm,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.found_hpt) / NULLIF(COUNT(*), 0), 1) AS pct_found_hpt,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.found_cvd) / NULLIF(COUNT(*), 0), 1) AS pct_found_cvd,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.found_stroke) / NULLIF(COUNT(*), 0), 1) AS pct_found_stroke,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.found_obesity) / NULLIF(COUNT(*), 0), 1) AS pct_found_obesity,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.found_dyslipidemia) / NULLIF(COUNT(*), 0), 1) AS pct_found_dyslipidemia,

  -- Lab-based (anemia = hemoglobin < threshold)
  -- CKD = eGFR-based
  -- Respiratory = from labhealthext

  NOW() AS refreshed_at
FROM ref_districts d
LEFT JOIN raw_vitalsigns v ON v.district_code = d.dcode AND v.cancel_status = 0
LEFT JOIN raw_patients p ON p.id = v.patient_id
GROUP BY d.dcode, d.zone_code, d.name_th, d.name_en;

-- สรุปรายเขต-รายปัจจัยเสี่ยง (สำหรับ Risk Filter)
CREATE MATERIALIZED VIEW summary_district_risk_factors AS
SELECT
  v.district_code AS dcode,
  p.sex,
  p.age_group,
  v.smoking,
  v.alcohol,
  h.exercise,
  COUNT(DISTINCT v.patient_id) AS total,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.risk_dm) / NULLIF(COUNT(*), 0), 1) AS pct_risk_dm,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.risk_hpt) / NULLIF(COUNT(*), 0), 1) AS pct_risk_hpt,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.risk_cvd) / NULLIF(COUNT(*), 0), 1) AS pct_risk_cvd,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.risk_bmi) / NULLIF(COUNT(*), 0), 1) AS pct_risk_bmi,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.found_dm) / NULLIF(COUNT(*), 0), 1) AS pct_found_dm,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.found_hpt) / NULLIF(COUNT(*), 0), 1) AS pct_found_hpt,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.found_stroke) / NULLIF(COUNT(*), 0), 1) AS pct_found_stroke,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.found_obesity) / NULLIF(COUNT(*), 0), 1) AS pct_found_obesity,
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.found_dyslipidemia) / NULLIF(COUNT(*), 0), 1) AS pct_found_dyslipidemia
FROM raw_vitalsigns v
JOIN raw_patients p ON p.id = v.patient_id
LEFT JOIN raw_homehealth h ON h.patient_id = v.patient_id
WHERE v.cancel_status = 0
GROUP BY v.district_code, p.sex, p.age_group, v.smoking, v.alcohol, h.exercise;

-- สรุปรายเขต-ผล Lab
CREATE MATERIALIZED VIEW summary_district_lab AS
SELECT
  v.district_code AS dcode,
  COUNT(DISTINCT l.patient_id) AS lab_total,
  ROUND(AVG(l.hemoglobin), 1) AS avg_hemoglobin,
  ROUND(AVG(l.fbs), 1) AS avg_fbs,
  ROUND(AVG(l.cholesterol), 1) AS avg_cholesterol,
  ROUND(AVG(l.triglyceride), 1) AS avg_triglyceride,
  ROUND(AVG(l.hdl), 1) AS avg_hdl,
  ROUND(AVG(l.ldl), 1) AS avg_ldl,
  ROUND(AVG(l.creatinine), 2) AS avg_creatinine,
  ROUND(AVG(l.egfr), 1) AS avg_egfr,
  ROUND(100.0 * COUNT(*) FILTER (WHERE l.hemoglobin < 12) / NULLIF(COUNT(*), 0), 1) AS pct_anemia,
  ROUND(100.0 * COUNT(*) FILTER (WHERE l.egfr < 60) / NULLIF(COUNT(*), 0), 1) AS pct_ckd,
  ROUND(100.0 * COUNT(*) FILTER (WHERE l.cbc_result = 2) / NULLIF(COUNT(*), 0), 1) AS pct_cbc_abnormal,
  ROUND(100.0 * COUNT(*) FILTER (WHERE l.liver_result = 2) / NULLIF(COUNT(*), 0), 1) AS pct_liver_abnormal
FROM raw_vitalsigns v
JOIN raw_lab_results l ON l.patient_id = v.patient_id AND l.cancel_status = 0
WHERE v.cancel_status = 0
GROUP BY v.district_code;

-- สรุปรายเขต-สุขภาพจิต
CREATE MATERIALIZED VIEW summary_district_mental AS
SELECT
  v.district_code AS dcode,
  COUNT(DISTINCT v.patient_id) AS total,
  -- 2Q depression screening (positive = both q1 and q2 = 2)
  ROUND(100.0 * COUNT(*) FILTER (WHERE v.depression_2q_1 = 2 OR v.depression_2q_2 = 2) / NULLIF(COUNT(*), 0), 1) AS pct_depression_risk,
  -- PHQ-9 >= 7 = moderate depression
  ROUND(100.0 * COUNT(*) FILTER (WHERE
    COALESCE(v.phq9_q1,0)+COALESCE(v.phq9_q2,0)+COALESCE(v.phq9_q3,0)+
    COALESCE(v.phq9_q4,0)+COALESCE(v.phq9_q5,0)+COALESCE(v.phq9_q6,0)+
    COALESCE(v.phq9_q7,0)+COALESCE(v.phq9_q8,0)+COALESCE(v.phq9_q9,0) >= 7
  ) / NULLIF(COUNT(*), 0), 1) AS pct_phq9_moderate,
  -- ST5 >= 5 = high stress
  ROUND(100.0 * COUNT(*) FILTER (WHERE
    COALESCE(v.st5_q1,0)+COALESCE(v.st5_q2,0)+COALESCE(v.st5_q3,0)+
    COALESCE(v.st5_q4,0)+COALESCE(v.st5_q5,0) >= 5
  ) / NULLIF(COUNT(*), 0), 1) AS pct_high_stress
FROM raw_vitalsigns v
WHERE v.cancel_status = 0
GROUP BY v.district_code;

-- สรุปรายเขต-ประชากรศาสตร์
CREATE MATERIALIZED VIEW summary_district_demographics AS
SELECT
  hv.current_district AS dcode,
  COUNT(DISTINCT hv.patient_id) AS total,
  -- Education breakdown
  COUNT(*) FILTER (WHERE hv.education = 1) AS edu_none,
  COUNT(*) FILTER (WHERE hv.education = 2) AS edu_primary,
  COUNT(*) FILTER (WHERE hv.education = 3) AS edu_secondary,
  COUNT(*) FILTER (WHERE hv.education IN (5,6,7)) AS edu_higher,
  -- Occupation
  COUNT(*) FILTER (WHERE hv.occupation = 1) AS occ_unemployed,
  COUNT(*) FILTER (WHERE hv.occupation = 2) AS occ_laborer,
  COUNT(*) FILTER (WHERE hv.occupation = 4) AS occ_government,
  COUNT(*) FILTER (WHERE hv.occupation = 7) AS occ_private,
  -- Health privilege
  COUNT(*) FILTER (WHERE hv.health_privilege = 2) AS prv_gold_card,
  COUNT(*) FILTER (WHERE hv.health_privilege = 5 OR hv.health_privilege = 6) AS prv_social_security,
  COUNT(*) FILTER (WHERE hv.health_privilege = 4) AS prv_government,
  -- Housing type
  COUNT(*) FILTER (WHERE hv.home_type = 2) AS housing_slum,
  COUNT(*) FILTER (WHERE hv.home_type = 3) AS housing_house,
  COUNT(*) FILTER (WHERE hv.home_type = 6) AS housing_condo
FROM raw_homevisit hv
WHERE hv.cancel_status = 0
GROUP BY hv.current_district;
```

---

## 3. Summary API Endpoints

### 3.1 Core Summary (สำหรับ BMA Health Web)

```
GET /api/v2/summary/overview
  → { total_screened, target, by_zone: [...], by_disease: [...], last_updated }

GET /api/v2/summary/zones
  → [{ zone_code, name_th, name_en, total_screened, district_count, diseases: {...} }]

GET /api/v2/summary/zones/{zone_code}
  → { ...zone_detail, districts: [{ dcode, name_th, total_screened, diseases }] }

GET /api/v2/summary/districts
  ?zone_code=3
  → [{ dcode, name_th, total_screened, diseases: { diabetes: { pct_at_risk, total }, ... } }]

GET /api/v2/summary/districts/{dcode}
  → { dcode, name_th, zone_code, total_screened, diseases, lab_summary, mental_health, demographics }

GET /api/v2/summary/districts/{dcode}/disease/{disease_key}
  → { disease detail with risk factor breakdown, age/sex distribution }
```

### 3.2 Filtered Summary (Risk Factor Filters)

```
GET /api/v2/summary/filtered
  ?district=1040&sex=10&age_group=45-54&smoking=1&exercise=3
  → { filtered aggregate — same structure as district summary }
  → MUST enforce k-anonymity ≥ 5
```

### 3.3 Trends (Time-series)

```
GET /api/v2/trends/screening
  ?granularity=monthly&zone_code=3
  → [{ period: "2025-09", screened: 12345, ... }]

GET /api/v2/trends/disease/{disease_key}
  ?district=1040&granularity=monthly
  → [{ period, pct_at_risk, total_screened }]
```

### 3.4 Reports (PDF/LaTeX generation)

```
POST /api/v2/reports/generate
  { type: "zone_summary", zone_code: "3", lang: "th", format: "pdf" }
  → { job_id, status: "processing" }

GET /api/v2/reports/{job_id}
  → { status, download_url (signed, expires in 1h) }
```

### 3.5 AI Assistant (Chat)

```
POST /api/v2/chat
  { message: "เบาหวานเขตไหนสูงสุด", session_id: "..." }
  → SSE stream with text + chart artifacts
  → Agent can ONLY query summary materialized views, never raw tables
```

---

## 4. ETL / Import Pipeline

### 4.1 Data Flow

```
Source CSV (base64 encoded PID)
  ↓ Decrypt PID → SHA-256 hash
  ↓ Validate schema + data types
  ↓ Dedup by (idcard_hash, visit_date, facility_code)
  ↓ Insert/Upsert into raw_* tables
  ↓ Refresh materialized views
  ↓ Invalidate Redis cache
  ↓ Notify Summary API
```

### 4.2 Import Format

รองรับ 2 format ตาม minimal_data:
- **CSV** (portal_top/): Column headers uppercase, PID base64-encoded
- **JSON** (json/): Schema definition files — ใช้สำหรับ validation

### 4.3 Scheduling

- **Daily incremental**: Import เฉพาะ records ที่ LASTDATE > last_import_timestamp
- **Weekly full refresh**: Rebuild materialized views
- **On-demand**: Admin trigger ผ่าน Admin API

---

## 5. Security Layer

### 5.1 Authentication & Authorization

| Role | Access | Description |
|------|--------|-------------|
| `public` | Summary API (read-only) | BMA Health Web, ประชาชนทั่วไป |
| `analyst` | Summary API + filtered queries | นักวิเคราะห์ สนพ. |
| `admin` | Summary + Admin API | ผู้ดูแลระบบ |
| `etl_service` | Raw DB (write) + Admin API | ETL pipeline service account |
| `agent` | Summary materialized views (read-only) | AI Chat agent |

### 5.2 API Security

- **Rate limiting**: 100 req/min (public), 1000 req/min (analyst)
- **API Key**: Required for all endpoints
- **JWT**: For authenticated admin/analyst sessions
- **CORS**: Allow only BMA Health Web origin
- **Request logging**: Log endpoint, params, response time — **never log PII**

### 5.3 Data Protection

- PID/IDCARD: SHA-256 hashed at import, original discarded
- Names: Not imported into database at all
- Phone/Email/LineID: Not imported
- Addresses: Aggregated to district level only
- All PII fields from pthistory (phone, idline, email): **excluded from import**

### 5.4 k-Anonymity Enforcement

```python
def enforce_k_anonymity(result: dict, k: int = 5) -> dict:
    """Suppress groups with fewer than k individuals."""
    if result.get('total', 0) < k:
        return {'total': '<5', 'suppressed': True}
    return result
```

---

## 6. Agent Swarm Architecture

สำหรับ aggregation tasks ที่ซับซ้อน + report generation + AI chat

### 6.1 Agent Types

```
┌─────────────────────────────────────────────────────────┐
│                    Orchestrator Agent                     │
│  (รับ task → แบ่งงาน → รวมผล → return summary)          │
└────┬──────────┬──────────┬──────────┬──────────┬────────┘
     │          │          │          │          │
┌────▼───┐ ┌───▼────┐ ┌───▼────┐ ┌───▼────┐ ┌───▼────┐
│ETL     │ │Agg     │ │Report  │ │Chat    │ │Monitor │
│Agent   │ │Agent   │ │Agent   │ │Agent   │ │Agent   │
│        │ │        │ │        │ │        │ │        │
│CSV→DB  │ │SQL→    │ │Data→   │ │NL→SQL  │ │Health  │
│import  │ │Summary │ │PDF/LaTeX│ │→Answer │ │checks  │
└────────┘ └────────┘ └────────┘ └────────┘ └────────┘
```

### 6.2 ETL Agent

**Responsibility**: Import CSV/JSON → PostgreSQL
- Validate schema against `minimal_data/json/*.json` definitions
- Decode base64 PID → SHA-256 hash
- Detect and handle duplicates
- Track import history + row counts
- Report errors to Monitor Agent

### 6.3 Aggregation Agent

**Responsibility**: Refresh materialized views + compute derived metrics
- Triggered after ETL completes
- Computes disease prevalence per district/zone
- Applies clinical thresholds:
  - Anemia: Hemoglobin < 12 g/dL (female), < 13 g/dL (male)
  - CKD: eGFR < 60 mL/min
  - Obesity: BMI ≥ 25 (Asia-Pacific)
  - Hypertension: SBP ≥ 140 or DBP ≥ 90
  - Diabetes risk: FBS ≥ 100 mg/dL
  - Dyslipidemia: LDL ≥ 160 or Triglyceride ≥ 200
- Refreshes Redis cache with latest summaries
- Enforces k-anonymity on all outputs

### 6.4 Report Agent

**Responsibility**: Generate PDF/LaTeX reports from summary data
- Zone summary reports (8 zones)
- District comparison reports
- Disease-specific deep-dive reports
- MSD executive reports (monthly/quarterly)
- Multi-language support (th, en)
- Uses Jinja2 LaTeX templates

### 6.5 Chat Agent

**Responsibility**: Answer natural language questions about health data
- Query ONLY materialized views (never raw tables)
- Generate chart artifacts (ECharts config)
- Enforce k-anonymity in all responses
- Support Thai + English
- Context-aware: knows current zone/district selection

### 6.6 Monitor Agent

**Responsibility**: System health + data quality
- Track import success/failure rates
- Alert on data anomalies (sudden spikes/drops)
- Monitor API response times
- Check materialized view freshness
- Report to admin dashboard

---

## 7. Technology Stack

| Component | Technology | Deployment | Rationale |
|-----------|-----------|------------|-----------|
| **Database** | PostgreSQL 16+ | **On-premise** (encrypted at rest, VLAN isolated) | Materialized views, proven at scale |
| **Cache** | Redis 7+ | **On-premise** (same VLAN as DB) | Summary cache, rate limiting |
| **Summary MCP Server** | Python (FastMCP / custom) | **On-premise** (stdio or localhost SSE) | Secure bridge for LLM agents |
| **API Framework** | FastAPI (Python) | **On-premise** (behind nginx reverse proxy) | Existing backend, async, OpenAPI |
| **ETL** | Python + pandas | **On-premise** (cron job / systemd service) | Matches existing codebase |
| **Agent Framework** | Claude Agent SDK | **On-premise** (MCP client) | Multi-agent orchestration |
| **LLM Inference** | Claude API (external) | **Cloud** (OK — receives only aggregate data) | Chat/Report generation |
| **Report Gen** | Jinja2 + LaTeX + pdflatex | **On-premise** | Existing report pipeline |
| **Web Frontend** | Next.js (static export) | **CDN allowed** (no data stored) | Receives only summary JSON |
| **Reverse Proxy** | nginx + mTLS | **On-premise** (DMZ) | TLS termination, rate limit, auth |
| **Audit Log** | Append-only JSONL + SHA-256 chain | **On-premise** (separate disk) | Tamper-proof access logging |
| **Backup** | pg_dump + encrypted offsite | **On-premise** (encrypted tape/NAS) | Disaster recovery |

### 7.1 Network Topology

```
┌─── VLAN 10: Raw Data (air-gapped) ───────────────────┐
│  PostgreSQL (port 5432, listen 10.0.10.x only)       │
│  ETL Service (no outbound internet)                   │
└───────────────────────────────────────────────────────┘
         │ pg_hba.conf: only from 10.0.20.x
         ▼
┌─── VLAN 20: Summary Engine ──────────────────────────┐
│  Aggregation Agent (reads raw, writes summary views)  │
│  Redis (port 6379, bind 10.0.20.x)                   │
│  Summary MCP Server (port 3100, bind 127.0.0.1)      │
│  FastAPI Backend (port 8001, bind 10.0.20.x)         │
└───────────────────────────────────────────────────────┘
         │ nginx: only /api/v2/summary/* allowed
         ▼
┌─── DMZ: API Gateway ────────────────────────────────┐
│  nginx reverse proxy (port 443, public IP)           │
│  mTLS for admin, API key for public                  │
│  Rate limiter: 100 req/min public, 1000 admin        │
└──────────────────────────────────────────────────────┘
         │ HTTPS only
         ▼
      Internet → BMA Health Web (CDN)
```

---

## 8. Migration Plan

### Phase 1: Database + ETL (สัปดาห์ 1-2)

- [ ] Setup PostgreSQL on-premise server (encrypted at rest, VLAN isolated)
- [ ] Create schema (raw tables + reference tables)
- [ ] Build ETL pipeline (CSV import → raw tables)
- [ ] Import existing `minimal_data/portal_top/*.csv`
- [ ] Create materialized views (summary_district_*)
- [ ] Validate aggregates match current `district_health_data.json`

### Phase 2: Summary MCP Server + API (สัปดาห์ 3-4)

- [ ] Build Summary MCP Server (10 tools, read-only summary views)
- [ ] Implement MCP security rules (table/column blocklist, k-anonymity)
- [ ] Implement `/api/v2/summary/*` REST endpoints (calls same summary DB)
- [ ] Add Redis caching layer
- [ ] Implement k-anonymity enforcement
- [ ] Authentication (API key + JWT)
- [ ] Rate limiting
- [ ] Migrate frontend from static JSON → Summary API

### Phase 3: Agent Swarm + MCP Integration (สัปดาห์ 5-6)

- [ ] ETL Agent: automated CSV import (on-premise cron)
- [ ] Aggregation Agent: materialized view refresh
- [ ] Connect Chat Agent → Summary MCP Server (replace static data)
- [ ] Report Agent → Summary MCP Server (live data for PDF generation)
- [ ] Monitor Agent: data quality checks + alerting
- [ ] Validate: Chat Agent cannot access raw_* tables (penetration test)

### Phase 4: Security Hardening + PDPA Compliance (สัปดาห์ 7-8)

- [ ] RBAC implementation (4 roles: public, analyst, admin, etl_service)
- [ ] Tamper-proof audit logging (SHA-256 chain, separate disk)
- [ ] mTLS for admin/agent connections
- [ ] nginx reverse proxy hardening (WAF rules, CSP headers)
- [ ] Penetration testing (focus: MCP → raw table access, SQL injection, k-anonymity bypass)
- [ ] PDPA compliance review with legal team
- [ ] k-anonymity validation suite (automated tests)
- [ ] Data retention policy enforcement (auto-purge after N years)
- [ ] Incident response plan document
- [ ] Backup & disaster recovery drill

---

## 9. Data Dictionary Quick Reference

### Disease Keys (used in API + frontend)

| Key | ภาษาไทย | Source Field |
|-----|---------|-------------|
| `diabetes` | เบาหวาน | vitalsigns.risk_dm / found_dm |
| `hypertension` | ความดันโลหิตสูง | vitalsigns.risk_hpt / found_hpt |
| `cardiovascular` | โรคหลอดเลือดหัวใจ | vitalsigns.risk_cvd / found_cvd |
| `obesity` | โรคอ้วน | vitalsigns.risk_bmi / found_obesity |
| `dyslipidemia` | ไขมันในเลือดผิดปกติ | vitalsigns.found_dyslipidemia |
| `stroke` | โรคหลอดเลือดสมอง | vitalsigns.found_stroke |
| `ckd` | โรคไตเรื้อรัง | lab.egfr < 60 |
| `anemia` | โลหิตจาง | lab.hemoglobin < threshold |
| `respiratory` | โรคระบบทางเดินหายใจ | labhealthext.scrres01-04 |

### Risk Factor Filter Keys

| Key | Values | Source |
|-----|--------|--------|
| `sex` | `10` (ชาย), `20` (หญิง) | patients.sex |
| `age_group` | `15-24`, `25-34`, `35-44`, `45-54`, `55-64`, `65+` | computed from birth_year |
| `smoking` | `0` (ไม่), `1` (สูบ), `2` (เลิก) | vitalsigns.smoking |
| `alcohol` | `0` (ไม่), `1` (ดื่ม), `2` (เลิก) | vitalsigns.alcohol |
| `exercise` | `1` (≥3/wk), `2` (<3/wk), `3` (ไม่เลย) | homehealth.exercise |

### Health Zone Mapping

| Zone | เขต | Zone Facilitator |
|------|-----|-----------------|
| 1 | ทวีวัฒนา, ตลิ่งชัน, บางแค, ภาษีเจริญ, หนองแขม, บางบอน | รพ.ราชพิพัฒน์ |
| 2 | บางกอกน้อย, บางกอกใหญ่, คลองสาน, ธนบุรี, จอมทอง, บางขุนเทียน | รพ.ตากสิน |
| 3 | ปทุมวัน, บางรัก, สาทร, บางคอแหลม, ยานนาวา, ราษฎร์บูรณะ, ทุ่งครุ, คลองเตย, วัฒนา, พระโขนง | รพ.เจริญกรุงประชารักษ์ |
| 4 | บางซื่อ, ดุสิต, บางพลัด, พระนคร | รพ.วชิรพยาบาล |
| 5 | พญาไท, ราชเทวี, ดินแดง, ห้วยขวาง, วังทองหลาง, สัมพันธวงศ์, ป้อมปราบฯ | รพ.กลาง |
| 6 | ดอนเมือง, สายไหม, หลักสี่, บางเขน, จตุจักร, ลาดพร้าว | รพ.กลาง |
| 7 | บางกะปิ, สะพานสูง, สวนหลวง, ประเวศ, บางนา, ลาดกระบัง | รพ.สิรินธร |
| 8 | คลองสามวา, หนองจอก, คันนายาว, บึงกุ่ม, มีนบุรี | รพ.เวชการุณย์รัศมิ์ |

---

## 10. External Data Sources (เอกสารแนบ ๒)

| Layer | Service Type | URL Pattern |
|-------|-------------|-------------|
| พื้นที่เขต | ArcGIS MapServer | cpudgiapp.bangkok.go.th |
| โรงพยาบาล | ArcGIS MapServer | bmagis.bangkok.go.th |
| ศูนย์บริการสาธารณสุข | ArcGIS MapServer | cpudgiportal.bangkok.go.th |
| PM2.5 | ArcGIS FeatureServer | bmagis.bangkok.go.th |
| แหล่งมลพิษ (20-28) | ArcGIS MapServer | bmamap.bangkok.go.th |

> **Note**: หลาย endpoint ต้องการสิทธิ์ admin — ต้องประสานกับ CPUD/GIS team

---

## Appendix A: Current vs Future Data Flow

### Current (Static — ไม่มี security layer)
```
Excel/CSV → manual process → district_health_data.json → Frontend (static import)
                              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                              ข้อมูล aggregate อยู่ใน static file
                              ไม่มี PII แต่ไม่มี access control
```

### Future (Live — On-Premise + MCP Security)
```
Hospital Portal CSV
    ↓ (internal transfer, encrypted)
ETL Agent (on-premise)
    ↓ validate + hash PID + dedup
PostgreSQL (on-premise, VLAN 10, encrypted at rest)
    ↓ (VLAN 10 → VLAN 20 only)
Aggregation Agent (on-premise)
    ↓ compute summary, enforce k-anonymity
Materialized Views (summary_district_*)  ← NO PII
    ↓
┌─────────────────────────────────────────────────┐
│  Two access paths — both read summary only:     │
│                                                  │
│  Path A: REST API                               │
│    Summary Views → Redis Cache → FastAPI         │
│    → nginx (mTLS/API key) → Frontend (CDN)       │
│                                                  │
│  Path B: MCP (for LLM Agents)                   │
│    Summary Views → Summary MCP Server            │
│    → LLM Agent (Claude API, external)            │
│    → receives only aggregate text/charts          │
│    → Chat UI on Frontend                          │
└─────────────────────────────────────────────────┘
```

### What CANNOT happen (by design)

```
✗ Frontend → raw_patients               (no endpoint exists)
✗ LLM Agent → raw_vitalsigns            (MCP blocks raw_* tables)
✗ Anyone → SELECT * FROM raw_patients   (pg_hba.conf + MCP validation)
✗ Cloud service → any PII               (data never leaves on-premise)
✗ API response → individual record      (k-anonymity enforced)
✗ Log file → PID/IDCARD                 (scrubbed before logging)
```

---

*Document generated from FACT sources: Bangkok_Health_Zoning.md, FACT_เอกสารแนบ_text.md, minimal_data/*
*Updated v1.1: On-premise only + MCP security architecture*
