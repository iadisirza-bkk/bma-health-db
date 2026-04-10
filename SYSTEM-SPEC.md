# BMA Health -- One-Stop Backend System Spec & Frontend Integration Guide

> **Version:** 4.3.0 | **Last Updated:** 2026-04-10
> **Live API Docs:** `http://localhost:9002/docs` (Swagger) | `http://localhost:9002/redoc` (ReDoc)
> **OpenAPI JSON:** `http://localhost:9002/openapi.json`

---

## Table of Contents

- [1. System Overview](#1-system-overview)
- [2. Architecture](#2-architecture)
- [3. Tech Stack](#3-tech-stack)
- [4. Base URL & Environments](#4-base-url--environments)
- [5. Authentication](#5-authentication)
- [6. Rate Limiting](#6-rate-limiting)
- [7. Privacy & k-Anonymity](#7-privacy--k-anonymity)
- [8. Error Handling](#8-error-handling)
- [9. Common Query Parameters](#9-common-query-parameters)
- [10. Valid Enums & Constants](#10-valid-enums--constants)
- [11. API Endpoint Reference](#11-api-endpoint-reference)
  - [System](#system)
  - [V2 Data API (84 endpoints)](#v2-data-api-84-endpoints)
  - [LLM Chat (2 endpoints)](#llm-chat-2-endpoints)
  - [PDF Reports (15 endpoints)](#pdf-reports-15-endpoints)
  - [Data Export (6 endpoints)](#data-export-6-endpoints)
  - [Statistics (6 endpoints)](#statistics-6-endpoints)
  - [Dashboards (3 endpoints)](#dashboards-3-endpoints)
  - [Factor Analysis (8 endpoints)](#factor-analysis-8-endpoints)
  - [Screening Tests (6 endpoints)](#screening-tests-6-endpoints)
  - [Admin API (6 endpoints)](#admin-api-6-endpoints)
- [12. Frontend Integration Guide](#12-frontend-integration-guide)
  - [next.config.ts Setup](#nextconfigts-setup)
  - [API Client](#api-client)
  - [Page-by-Page Data Fetching](#page-by-page-data-fetching)
  - [Chat Integration (SSE)](#chat-integration-sse)
  - [Report Downloads](#report-downloads)
  - [TypeScript Types](#typescript-types)
- [13. Response Schema Examples](#13-response-schema-examples)
- [14. Data Preprocessing & Computed Variables](#14-data-preprocessing--computed-variables)
- [15. Database Schema & Materialized Views](#15-database-schema--materialized-views)
- [16. CORS Configuration](#16-cors-configuration)
- [17. Caching Behavior](#17-caching-behavior)
- [18. Running Services](#18-running-services)

---

## 1. System Overview

The BMA Health One-Stop Backend serves **all** backend functionality for the Bangkok Metropolitan Administration health screening system from a **single server**. It consolidates the V2 data API, LLM-powered chat, LaTeX/PDF report generation, Excel export, statistics, and admin endpoints.

**153 endpoints** across **24 domain groups**.

### Key Constraints
- **No individual records** -- all data is aggregate/summary level
- **No PII exposed** -- `idcard_hash`, `patient_id`, `staff_code` automatically stripped
- **k-anonymity >= 5** -- groups with fewer than 5 individuals are suppressed
- **PDPA compliant** -- meets Thailand's Personal Data Protection Act requirements

### Domain Groups

| # | Domain | Prefix | Endpoints | Description |
|---|--------|--------|-----------|-------------|
| 1 | Summary | `/api/v2/summary/` | 5 | Screening overview, filtered risk factors, lab, mental health, demographics |
| 2 | Zones | `/api/v2/summary/zones/` | 3 | Health zone dashboards (8 zones) |
| 3 | Districts | `/api/v2/summary/districts/` | 3 | District-level detail (50 districts) |
| 4 | Epidemiology | `/api/v2/epidemiology/` | 6 | Age-group prevalence, comorbidity, outbreak detection |
| 5 | Trends | `/api/v2/trends/` | 2 | Time-series screening & disease trends |
| 6 | Search | `/api/v2/search/` | 1 | Search & rank districts by disease |
| 7 | KPI | `/api/v2/kpi/` | 7 | MOPH targets, screening yield, benchmarks |
| 8 | Executive | `/api/v2/executive/` | 5 | Governor-level KPIs, YoY, media briefs |
| 9 | Promotion | `/api/v2/promotion/` | 6 | BMI, behavior-disease correlation |
| 10 | Disease Control | `/api/v2/disease-control/` | 6 | NCD cascade, coverage, referrals |
| 11 | Facility | `/api/v2/facility/` | 6 | Performance, workload, capacity |
| 12 | Strategy | `/api/v2/strategy/` | 5 | Cost, budget allocation, ROI |
| 13 | Research | `/api/v2/research/` | 6 | Data dictionary, correlation, export |
| 14 | Public | `/api/v2/public/` | 7 | Thai summaries, locations, health tips |
| 15 | GIS | `/api/v2/gis/` | 12 | Facility coords, heatmaps, PM2.5 zones/districts/monthly |
| 16 | Monitoring | `/api/v2/monitoring/` | 7 | Data quality, ETL status, cache |
| 17 | **Chat** | `/api/health/` | **2** | **LLM chat (sync + SSE streaming)** |
| 18 | **Reports** | `/api/reports/` | **16** | **PDF generation, download, catalog, dashboard** |
| 19 | **Export** | `/api/export/` | **6** | **PDF/Excel export per district/zone** |
| 20 | **Statistics** | `/api/stats/` | **6** | **Descriptive stats, comparison, ranking** |
| 21 | **Dashboards** | `/api/dashboard/` | **3** | **Governor, director, medical views** |
| 22 | **Factors** | `/api/factors/` | **8** | **Sex, age, occupation, behavior analysis** |
| 23 | **Screening Tests** | `/api/screening-tests/` | **6** | **EKG, X-ray, blood, retinal** |
| 24 | **Admin API** | `/api/admin/` | **6** | **Excel upload, audit, cache** |

---

## 2. Architecture

```
                    Frontend (Next.js :3000)
                           |
                    ALL requests proxy to
                           |
                           v
              +--------------------------+
              |  One-Stop Backend :9002  |
              |  FastAPI + Uvicorn       |
              +--------------------------+
              |  /api/v2/*    Data API   |
              |  /api/health/* LLM Chat  |
              |  /api/reports/* Reports  |
              |  /api/export/* Export    |
              |  /api/stats/*  Stats    |
              |  /api/dashboard/* Dash  |
              |  /api/factors/* Factors |
              |  /api/screening-tests/* |
              |  /api/admin/*  Admin    |
              +------+-------+----------+
                     |       |
            +--------+       +--------+
            v                         v
      PostgreSQL :5433          LMStudio :5555
      (health data)             (Gemma 4 LLM)
            |
      Redis :6379
      (cache layer)
```

Previously this required **2 backends** (port 9002 + 8001). Now everything runs on **port 9002**.

---

## 3. Tech Stack

| Layer | Technology |
|-------|-----------|
| API Framework | FastAPI 0.110+ (Python 3.12) |
| Server | Uvicorn |
| Database | PostgreSQL 16 |
| Cache | Redis 7 |
| LLM | Gemma 4 via LMStudio (localhost:5555) |
| PDF Generation | LaTeX (Tectonic / XeLaTeX) + Jinja2 |
| Charts | Matplotlib |
| Excel | openpyxl |
| Containerization | Docker Compose |
| Auth | API Key (HMAC-SHA256) |
| Docs | Swagger UI + ReDoc (auto-generated) |

---

## 4. Base URL & Environments

| Environment | Base URL | Port | Start Command |
|-------------|----------|------|---------------|
| Local Dev | `http://localhost:9002` | 9002 | `make dev` |
| Docker (all services) | `http://localhost:8001` | 8001 | `make up` |
| Production | `https://<your-domain>` | 443 | `make prod` |

### Service Ports

| Service | Port | Description |
|---------|------|-------------|
| **API** (local dev) | `9002` | FastAPI with hot-reload |
| **API** (Docker) | `8001` | FastAPI via Uvicorn |
| **PostgreSQL** | `5433` | Mapped from container `5432` |
| **Redis** | `6379` | Cache layer |
| **LMStudio** | `5555` | LLM inference (external, optional) |

---

## 5. Authentication

All API endpoints (except public paths) require an API key:

```
X-API-Key: <your-api-key>
```

**Public paths (no key required):**
- `GET /health`
- `GET /docs`, `/redoc`, `/openapi.json` (dev only)
- `/admin/*` (uses session/cookie auth)
- `/static/*`
- `/api/auth/*` (future JWT endpoints)

**Admin API (`/api/admin/*`):** Requires `Authorization: Bearer <admin_password>` header.

**Error (401):**
```json
{ "detail": "Invalid or missing API key" }
```

---

## 6. Rate Limiting

| Tier | Limit | Window |
|------|-------|--------|
| Public | 60 requests | 60 seconds |

Per-IP sliding window. Health checks, admin, and static routes exempt.

---

## 7. Privacy & k-Anonymity

All endpoints enforce **k-anonymity with k=5**:
- Groups with `count < 5` are **completely excluded**
- Single-resource lookups return `403` if insufficient data

---

## 8. Error Handling

```json
{ "detail": "Error description" }
```

| Status | Meaning |
|--------|---------|
| `400` | Bad request -- invalid parameter |
| `401` | Missing or invalid API key |
| `403` | Data suppressed (k-anonymity) |
| `404` | Resource not found |
| `429` | Rate limit exceeded |
| `500` | Internal server error |
| `503` | LLM service or report generator unavailable |

---

## 9. Common Query Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `zone_code` | `string` | `"01"` through `"08"` |
| `dcode` / `district` | `string` | 4-digit district code (e.g. `"1001"`) |
| `sex` | `int` | `1` = Male, `2` = Female |
| `age_group` | `string` | `"15-19"`, `"20-29"`, ..., `"70+"` |
| `smoking` | `int` | `0` = No, `1` = Yes |
| `exercise` | `int` | `1` = >=3 days/wk, `2` = <3, `3` = None |
| `granularity` | `string` | `"monthly"` or `"quarterly"` |
| `disease_key` | `string` | Disease identifier (see below) |

---

## 10. Valid Enums & Constants

### Disease Keys

```
diabetes, hypertension, cardiovascular, obesity,
dyslipidemia, stroke, ckd, anemia
```

### Age Groups

```
"15-19", "20-29", "30-39", "40-49", "50-59", "60-69", "70+"
```

### Report Languages (10)

```
th, en, zh, ja, ko, ru, my, hi, vi, fr
```

### Report Types

```
whitepaper    -- Comprehensive document (article class)
slides        -- Executive presentation (Beamer class)
```

---

## 11. API Endpoint Reference

### System

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | None | Health check with DB & cache status |

---

### V2 Data API (84 endpoints)

All under `/api/v2/`. Full reference at **`/docs`** or **`/redoc`**.

#### Summary
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v2/summary/overview` | Top-level screening overview |
| `GET` | `/api/v2/summary/filtered` | Risk factors by demographic filters |
| `GET` | `/api/v2/summary/lab` | Lab results by district |
| `GET` | `/api/v2/summary/mental-health` | Mental health screening rates |
| `GET` | `/api/v2/summary/demographics` | Education, occupation, housing |

#### Zones
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v2/summary/zones` | All 8 zones with disease counts |
| `GET` | `/api/v2/summary/zones/{zone_code}` | Single zone with districts |
| `GET` | `/api/v2/zone/{zone_code}/dashboard` | Zone dashboard |

#### Districts
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v2/summary/districts` | All districts (optionally by zone) |
| `GET` | `/api/v2/summary/districts/{dcode}` | Full district detail |
| `GET` | `/api/v2/summary/districts/{dcode}/disease/{disease_key}` | Disease breakdown |

#### Epidemiology, Trends, Search, KPI, Executive, Promotion, Disease Control, Facility, Strategy, Research, Public, GIS, Monitoring

#### GIS — PM2.5 Aggregated Endpoints (NEW)

| Method | Path | Params | Description |
|--------|------|--------|-------------|
| `GET` | `/api/v2/gis/pm25/zones` | — | PM2.5 averages per health zone (8 zones). Includes AQI, max, standard exceeded flags |
| `GET` | `/api/v2/gis/pm25/districts` | — | PM2.5 per district (50 districts). Each district has its own ArcGIS reading |
| `GET` | `/api/v2/gis/pm25/monthly` | `?zone_code=01` (optional) | Monthly PM2.5 trend data for charts. Returns current snapshot + historical periods |

**Data source:** BMA ArcGIS REST API (`bmagis.bangkok.go.th`) — returns one reading per district (~50 records).
**Caching:** T1 (5 min) for ArcGIS data, T2 (15 min) for assembled responses.
**AQI:** Computed from PM2.5 using US EPA breakpoints (ArcGIS does not return AQI).
**Standards:** Thai NAAQS = 37.5 µg/m³, WHO 2021 = 15.0 µg/m³.

> Historical fields (`days_exceeded`, `avg_2025`, `avg_2026_q1`, `trend`) return `null` until `pm25_daily` table is populated. No API contract change needed — values appear automatically once data accumulates.

See **Swagger UI** at `/docs` for the complete V2 reference with request/response schemas.

---

### LLM Chat (2 endpoints)

| Method | Path | Params | Description |
|--------|------|--------|-------------|
| `GET/POST` | `/api/health/chat` | `message` | Synchronous chat -- returns full JSON response |
| `GET/POST` | `/api/health/chat/stream` | `message`, `history` | **SSE streaming** -- returns Server-Sent Events |

**Requires:** LMStudio running at `LMSTUDIO_URL` (default `localhost:5555`).
Returns `503` if LLM is unavailable. Falls back to rule-based responses.

#### Agent Tools (7 registered)

The LLM agent has 7 tools that give it access to all backend data:

| Tool | What It Can Answer |
|------|-------------------|
| `query_health_data` | Disease prevalence by district/zone/age/sex/behavior, rankings, comparisons |
| `query_api` | **25 specialized endpoints**: KPI targets, NCD cascade, lab values, BMI distribution, cost/budget, screening tests (EKG/X-ray), chronic history, family history, comorbidity counts, facility performance, YoY comparison, screening locations, zone comparison |
| `query_statistical_test` | Chi-square, odds ratio, ANOVA, correlation, comorbidity matrix |
| `generate_report` | PDF report generation (comprehensive/executive/disease) |
| `generate_adaptive_report` | AI-written custom PDF with real data |
| `query_zone_info` | Zone details, facilitator hospitals, district mapping |
| `ask_clarification` | Ask follow-up questions to narrow query |

#### What Personas Can Ask

| Persona | Example Questions the Agent Can Answer |
|---------|---------------------------------------|
| **ผู้ว่า กทม.** | ภาพรวมสุขภาพ, โซนไหนมีปัญหามากสุด, งบคัดกรอง, เทียบปีที่แล้ว |
| **รองผู้ว่า** | เปรียบเทียบโซน, Top 10 เบาหวาน, Lab เฉลี่ย, DM+HPT กี่คน |
| **ผอ.สำนักการแพทย์** | NCD Cascade, KPI เทียบ สธ., ต้นทุน/คน, คัดกรองซ้ำ |
| **ผอ.รพ.เขต** | สุขภาพโซน 1, EKG/X-ray, ออกกำลังกาย, งบจัดสรร |
| **ประชาชนการศึกษาสูง** | ชาย 40-49 เสี่ยงเบาหวาน%, BMI เฉลี่ย, ประวัติครอบครัว |
| **ประชาชนจบ ป.6** | ตรวจฟรีที่ไหน, เบาหวานอันตรายไหม, ผลเลือดปกติเท่าไหร่ |

#### Data Quality Safeguards

- **Sample size warning**: When data < 1,000 records, responses include: *"ข้อมูลจ���กกลุ่มต��วอย่าง N คน ส���ดส่วนอาจเปลี่ยนแปลงเมื่อม���ข้อมูลเพิ่ม"*
- **Modeled data disclaimer**: When using demographic modifiers: *"ค่าประมาณจากแบบจำลอง อิงข้อมูลจริง กทม. ปรับด้วยค่���สัดส่วนจากสำรวจระดับชาติ"*
- **Fallback responses**: When LLM is unavailable, rule-based fallback handles: overview, prevalence, risk factors, lab values, health advice, trend data, sex comparison

#### SSE Event Types

| Event Type | Description |
|------------|-------------|
| `agent_start` | Agent began processing (with label, icon) |
| `content` | Streamed text token |
| `visualization` | Chart data (ECharts config) |
| `artifact` | File download URL (PDF report) |
| `clarification` | Follow-up question for user |
| `agent_done` | Agent finished |
| `done` | Stream complete |
| `error` | Error occurred |

---

### PDF Reports (16 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/reports/dashboard` | **Unified dashboard: progress %, scheduler, catalog, summary** |
| `GET` | `/api/reports/catalog` | List all reports with cache status |
| `GET` | `/api/reports/status` | Cache status for all variants |
| `GET` | `/api/reports/generation-progress` | Real-time generation progress |
| `GET` | `/api/reports/scheduler-status` | Nightly scheduler status |
| `GET` | `/api/reports/comprehensive/{lang}` | Download whitepaper PDF |
| `GET` | `/api/reports/executive/{lang}` | Download executive slides PDF |
| `GET` | `/api/reports/disease/{disease}` | Download disease-specific slides |
| `GET` | `/api/reports/zone/{zone_code}/{lang}` | Download zone report |
| `GET` | `/api/reports/adaptive/{filename}` | Download AI-generated report |
| `GET` | `/api/reports/msd/{lang}` | Download MSD comprehensive (100+ pages) |
| `GET` | `/api/reports/public/{lang}` | Download public infographic |
| `POST` | `/api/reports/generate` | Trigger all report generation (background) |
| `POST` | `/api/reports/generate/{lang}` | Generate both types for a language |
| `POST` | `/api/reports/generate/{lang}/{report_type}` | Generate specific variant |
| `POST` | `/api/reports/invalidate` | Clear cache, force regeneration |

**Languages:** `th`, `en`, `zh`, `ja`, `ko`, `ru`, `my`, `hi`, `vi`, `fr`

---

### Data Export (6 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/export/district/{dcode}/pdf` | Text PDF for a district |
| `GET` | `/api/export/district/{dcode}/pdf/json` | Structured JSON report |
| `GET` | `/api/export/district/{dcode}/excel` | Excel/CSV for a district |
| `GET` | `/api/export/zone/{zone_code}/excel` | Excel for all districts in zone |
| `GET` | `/api/export/city/excel` | Excel for all 50 districts |
| `GET` | `/api/export/rankings/{disease}/excel` | District rankings by disease |

---

### Statistics (6 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/stats/district/{dcode}` | Descriptive stats for a district |
| `GET` | `/api/stats/compare` | Side-by-side comparison (with p-values) |
| `GET` | `/api/stats/zone/{zone_code}` | Zone aggregate stats |
| `GET` | `/api/stats/city` | Bangkok-wide statistics |
| `GET` | `/api/stats/ranking/{disease}` | Rank districts by disease prevalence |
| `GET` | `/api/stats/trends/{dcode}/{disease}` | Trend analysis |

---

### Dashboards (3 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/dashboard/governor` | City-wide KPIs, zone comparison, top-5 risk |
| `GET` | `/api/dashboard/director/{zone_code}` | Zone director view |
| `GET` | `/api/dashboard/medical/{dcode}` | Medical officer district view |

---

### Factor Analysis (8 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/factors/sex` | Disease risk by sex |
| `GET` | `/api/factors/age-group` | Disease risk by age band |
| `GET` | `/api/factors/occupation` | Disease risk by occupation |
| `GET` | `/api/factors/zone` | Disease risk by health zone |
| `GET` | `/api/factors/behavior/smoking` | Risk by smoking status |
| `GET` | `/api/factors/behavior/alcohol` | Risk by alcohol consumption |
| `GET` | `/api/factors/behavior/exercise` | Risk by exercise frequency |
| `GET` | `/api/factors/cross-tabulation` | 2-way cross-tab with chi-square |

---

### Screening Tests (6 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/screening-tests/summary` | Overall completion rates |
| `GET` | `/api/screening-tests/district/{dcode}` | District-level results |
| `GET` | `/api/screening-tests/ekg/summary` | EKG breakdown |
| `GET` | `/api/screening-tests/chest-xray/summary` | Chest X-ray results |
| `GET` | `/api/screening-tests/blood/summary` | Blood panel results |
| `GET` | `/api/screening-tests/retinal/summary` | Diabetic retinopathy grading |

---

### Admin API (6 endpoints)

**Auth:** `Authorization: Bearer <ADMIN_PASSWORD>`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/admin/upload-screening` | Upload district health data (JSON) |
| `POST` | `/api/admin/upload-excel` | Upload Excel file (.xlsx) |
| `GET` | `/api/admin/excel-template` | Download Excel template |
| `GET` | `/api/admin/data-status` | Check data completeness |
| `POST` | `/api/admin/invalidate-cache` | Clear all Redis caches |
| `GET` | `/api/admin/audit-log` | PDPA audit trail |

---

## 12. Frontend Integration Guide

### next.config.ts Setup

After consolidation, **all** rewrites point to **one** backend:

```typescript
// next.config.ts
export default {
  async rewrites() {
    const apiUrl = process.env.NEXT_PUBLIC_V2_API_URL || 'http://localhost:9002';
    return [
      { source: '/api/v2/:path*',      destination: `${apiUrl}/api/v2/:path*` },
      { source: '/api/health/:path*',   destination: `${apiUrl}/api/health/:path*` },
      { source: '/api/reports/:path*',  destination: `${apiUrl}/api/reports/:path*` },
      { source: '/api/export/:path*',   destination: `${apiUrl}/api/export/:path*` },
      { source: '/api/stats/:path*',    destination: `${apiUrl}/api/stats/:path*` },
      { source: '/api/dashboard/:path*',destination: `${apiUrl}/api/dashboard/:path*` },
      { source: '/api/factors/:path*',  destination: `${apiUrl}/api/factors/:path*` },
      { source: '/api/screening-tests/:path*', destination: `${apiUrl}/api/screening-tests/:path*` },
      { source: '/api/admin/:path*',    destination: `${apiUrl}/api/admin/:path*` },
    ];
  },
};
```

**Environment variables:**
```env
NEXT_PUBLIC_V2_API_URL=http://localhost:9002
NEXT_PUBLIC_V2_API_KEY=dev-api-key
```

> **No more `NEXT_PUBLIC_API_URL` pointing to port 8001.** Remove it.

---

### API Client

```typescript
// lib/api-client.ts
const API_BASE = process.env.NEXT_PUBLIC_V2_API_URL || 'http://localhost:9002';
const API_KEY = process.env.NEXT_PUBLIC_V2_API_KEY || 'dev-api-key';

export async function apiFetch<T>(path: string, params?: Record<string, string>): Promise<T> {
  const url = new URL(`${API_BASE}${path}`);
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v != null) url.searchParams.set(k, v);
    });
  }
  const res = await fetch(url.toString(), {
    headers: { 'X-API-Key': API_KEY, 'Content-Type': 'application/json' },
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}
```

---

### Page-by-Page Data Fetching

```
Dashboard Page:
  GET /api/v2/summary/overview           -> Hero stats, zone cards
  GET /api/v2/executive/headline-kpi     -> 3 headline numbers
  GET /api/dashboard/governor            -> Full governor dashboard

Zone Detail Page:
  GET /api/v2/summary/zones/{zone_code}  -> Zone info + district list
  GET /api/v2/zone/{zone_code}/dashboard -> Facilities + metrics
  GET /api/dashboard/director/{zone_code}-> Director dashboard

District Detail Page:
  GET /api/v2/summary/districts/{dcode}  -> All 4 dimensions
  GET /api/v2/trends/disease/{disease}?district={dcode}  -> Charts
  GET /api/v2/gis/facilities/district/{dcode}             -> Map pins
  GET /api/stats/district/{dcode}        -> Detailed statistics
  GET /api/dashboard/medical/{dcode}     -> Medical officer view

Map Page:
  GET /api/v2/gis/facilities             -> All facility markers
  GET /api/v2/gis/heatmap/disease/{key}  -> Heatmap layer
  GET /api/v2/gis/pm25/current           -> PM2.5 raw station overlay
  GET /api/v2/gis/pm25/zones             -> PM2.5 avg per zone (8 zones)
  GET /api/v2/gis/pm25/districts         -> PM2.5 per district (50 districts)
  GET /api/v2/gis/pm25/monthly?zone_code=01 -> Monthly trend for charts

Epidemiology Page:
  GET /api/v2/epidemiology/age-pyramid   -> Population pyramid
  GET /api/v2/epidemiology/multi-disease-matrix -> Comorbidity
  GET /api/factors/sex                   -> Risk by sex
  GET /api/factors/age-group             -> Risk by age

Reports Page:
  GET /api/reports/dashboard             -> Unified: progress %, catalog, scheduler (poll this)
  GET /api/reports/catalog               -> List all reports
  GET /api/reports/generation-progress   -> Poll during generation (legacy)
  POST /api/reports/generate             -> Trigger generation

Admin Page:
  GET /api/admin/excel-template          -> Download template
  POST /api/admin/upload-excel           -> Upload data
  GET /api/admin/data-status             -> Check completeness
```

---

### Chat Integration (SSE)

```typescript
// hooks/useChat.ts
function streamChat(message: string, history: ChatMessage[]) {
  const params = new URLSearchParams({
    message,
    history: JSON.stringify(history.slice(-2)),
  });

  const eventSource = new EventSource(`/api/health/chat/stream?${params}`);
  // Note: X-API-Key header cannot be set with EventSource.
  // Use fetch() with ReadableStream instead:

  fetch(`/api/health/chat/stream?${params}`, {
    headers: { 'X-API-Key': API_KEY },
  }).then(async (res) => {
    const reader = res.body!.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const text = decoder.decode(value);
      // Parse SSE lines: "data: {...}\n\n"
      for (const line of text.split('\n')) {
        if (line.startsWith('data: ')) {
          const event = JSON.parse(line.slice(6));
          switch (event.type) {
            case 'content':       // Append text token
            case 'visualization': // Render ECharts config
            case 'artifact':      // Show download link
            case 'agent_start':   // Show agent indicator
            case 'agent_done':    // Hide agent indicator
            case 'clarification': // Show follow-up options
            case 'done':          // Stream complete
            case 'error':         // Show error
          }
        }
      }
    }
  });
}
```

---

### Report Dashboard (Unified Endpoint)

Frontend should use `GET /api/reports/dashboard` as a **single polling endpoint** for the reports page.

```typescript
// hooks/useReportDashboard.ts
function useReportDashboard(pollInterval = 5000) {
  const [dashboard, setDashboard] = useState<ReportDashboardResponse | null>(null);

  useEffect(() => {
    const fetchDashboard = async () => {
      const res = await apiFetch<ReportDashboardResponse>('/api/reports/dashboard');
      setDashboard(res);
    };
    fetchDashboard();
    const interval = setInterval(fetchDashboard, pollInterval);
    return () => clearInterval(interval);
  }, [pollInterval]);

  return dashboard;
}

// Usage in Reports page
function ReportsPage() {
  const dashboard = useReportDashboard();
  if (!dashboard) return <Loading />;

  const { generation, scheduler, categories, summary } = dashboard;

  // 1. Show progress bar if generation is running (cron runs at 00:30)
  if (generation.running) {
    return <ProgressBar percent={generation.percent} current={generation.current} />;
  }

  // 2. Show cached reports immediately (no generation needed)
  return (
    <>
      <SummaryBadge ready={summary.cached_reports} total={summary.total_reports}
                    percent={summary.percent_ready} />
      {categories.map(cat => (
        <CategorySection key={cat.id} label={cat.label} icon={cat.icon}>
          {cat.reports.map(r => (
            <ReportCard key={r.url} label={r.label} cached={r.cached}
                        size={r.size} updatedAt={r.updated_at}
                        onDownload={() => window.open(r.url)} />
          ))}
        </CategorySection>
      ))}
      <SchedulerInfo nextRun={scheduler.next_run} lastRun={scheduler.last_run} />
    </>
  );
}
```

### Report Downloads (Direct)

```typescript
// Download PDF report directly via URL from dashboard
async function downloadReport(url: string) {
  const res = await fetch(url, {
    headers: { 'X-API-Key': API_KEY },
  });
  if (!res.ok) throw new Error('Report not available');
  const blob = await res.blob();
  const blobUrl = URL.createObjectURL(blob);
  window.open(blobUrl);
}
```

---

### TypeScript Types

```typescript
type DiseaseKey = 'diabetes' | 'hypertension' | 'cardiovascular' | 'obesity'
  | 'dyslipidemia' | 'stroke' | 'ckd' | 'anemia';

type ReportLang = 'th' | 'en' | 'zh' | 'ja' | 'ko' | 'ru' | 'my' | 'hi' | 'vi' | 'fr';

interface OverviewResponse {
  total_screened: number;
  target: number;
  zones_count: number;
  districts_count: number;
  last_updated: string | null;
  by_zone: { zone_code: string; name_th: string; total_screened: number }[];
  by_disease: { disease_key: DiseaseKey; total_at_risk: number; pct: number }[];
}

interface DistrictDetail {
  disease: DistrictDisease | null;
  lab_summary: LabSummary | null;
  mental_health: MentalHealth | null;
  demographics: Demographics | null;
}

interface SSEEvent {
  type: 'content' | 'visualization' | 'artifact' | 'agent_start'
    | 'agent_done' | 'clarification' | 'done' | 'error';
  text?: string;
  agent?: string;
  label?: string;
  url?: string;
  message?: string;
  data?: Record<string, unknown>;
}

interface ReportDashboardResponse {
  generation: GenerationProgress;
  scheduler: SchedulerInfo;
  categories: ReportCategory[];
  summary: DashboardSummary;
}

interface GenerationProgress {
  running: boolean;
  percent: number;          // 0-100
  completed: number;
  total: number;
  current: string | null;
  started_at: string | null;
  finished_at: string | null;
  errors: string[];
}

interface SchedulerInfo {
  enabled: boolean;
  cron: string;
  last_run: string | null;
  next_run: string | null;
  running: boolean;
}

interface ReportCategory {
  id: string;
  label: string;
  icon: string;
  reports: ReportDashboardItem[];
}

interface ReportDashboardItem {
  label: string;
  url: string;
  cached: boolean;
  size: number;
  updated_at: string | null;  // ISO 8601 UTC, null if not cached
}

interface DashboardSummary {
  total_reports: number;
  cached_reports: number;
  percent_ready: number;    // 0-100
}

/** @deprecated Use ReportDashboardResponse instead */
interface ReportCatalog {
  categories: {
    id: string;
    label: string;
    icon: string;
    reports: { label: string; url: string; cached: boolean; size: number }[];
  }[];
}

interface GovernorDashboard {
  total_screened: number;
  total_districts: number;
  diseases: Record<DiseaseKey, { total_at_risk: number; pct: number }>;
  zone_comparison: Record<string, unknown>[];
  top_risk_districts: Record<string, unknown>[];
  timestamp: string;
}

interface PM25ZoneItem {
  zone_code: string;
  zone_name_th: string;
  zone_name_en: string;
  district_count: number;
  avg_pm25: number | null;       // null if ArcGIS unavailable
  avg_aqi: number | null;
  max_pm25: number | null;
  station_count: number;
  days_exceeded: number | null;  // null until pm25_daily populated
  trend: 'increasing' | 'decreasing' | 'stable' | null;
  standard_th_exceeded: boolean | null;
  standard_who_exceeded: boolean | null;
}

interface PM25ZonesResponse {
  data_available: boolean;
  total_zones: number;
  standards: { th: number; who: number };
  data: PM25ZoneItem[];
}

interface PM25DistrictItem {
  dcode: string;
  district_name: string;
  district_name_en: string;
  zone_code: string;
  avg_pm25: number | null;
  avg_aqi: number | null;
  nearest_station: string;
  station_name_th: string;
  days_exceeded: number | null;
  avg_2025: number | null;
  avg_2026_q1: number | null;
  trend: 'increasing' | 'decreasing' | 'stable' | null;
}

interface PM25DistrictsResponse {
  data_available: boolean;
  total_districts: number;
  standards: { th: number; who: number };
  data: PM25DistrictItem[];
}

interface PM25MonthlyResponse {
  zone_code: string | null;
  period: { year: string; month: string; avg_pm25: number; avg_aqi: number }[];
  current_snapshot: { avg_pm25: number | null; avg_aqi: number | null; station_count: number };
  standards: { th: number; who: number };
  historical_data_available: boolean;
}

interface FactorAnalysis {
  factor: string;
  categories: {
    label: string;
    count: number;
    diseases: Record<DiseaseKey, { pct: number; relative_risk: number }>;
  }[];
  chi_square: { statistic: number; p_value: number; significant: boolean };
}
```

---

## 13. Response Schema Examples

### Chat Stream (SSE)

```
data: {"type":"agent_start","agent":"analyst","label":"Analyzing query...","icon":"brain"}

data: {"type":"content","text":"จาก"}

data: {"type":"content","text":"ข้อมูล"}

data: {"type":"visualization","chart_type":"bar","title":"Diabetes by Zone","data":[...]}

data: {"type":"artifact","url":"/api/reports/adaptive/custom_report.pdf","label":"Download Report"}

data: {"type":"agent_done","agent":"analyst"}

data: {"type":"done"}
```

### Report Catalog

```json
{
  "categories": [
    {
      "id": "executive",
      "label": "Executive Slides",
      "icon": "presentation",
      "reports": [
        { "label": "Executive Slides (EN)", "url": "/api/reports/executive/en", "cached": true, "size": 245000 },
        { "label": "Executive Slides (TH)", "url": "/api/reports/executive/th", "cached": false, "size": 0 }
      ]
    }
  ]
}
```

### Governor Dashboard

```json
{
  "total_screened": 177,
  "total_districts": 50,
  "diseases": {
    "diabetes": { "total_at_risk": 14, "pct": 7.91 },
    "hypertension": { "total_at_risk": 27, "pct": 15.25 }
  },
  "zone_comparison": [...],
  "top_risk_districts": [...],
  "timestamp": "2026-04-10T16:30:00Z"
}
```

### PM2.5 Zones (`GET /api/v2/gis/pm25/zones`)

```json
{
  "data_available": true,
  "total_zones": 8,
  "standards": { "th": 37.5, "who": 15.0 },
  "data": [
    {
      "zone_code": "01",
      "zone_name_th": "โซน 1",
      "zone_name_en": "Zone 1",
      "district_count": 6,
      "avg_pm25": 52.3,
      "avg_aqi": 142,
      "max_pm25": 68.5,
      "station_count": 6,
      "days_exceeded": null,
      "trend": null,
      "standard_th_exceeded": true,
      "standard_who_exceeded": true
    }
  ]
}
```

### PM2.5 Districts (`GET /api/v2/gis/pm25/districts`)

```json
{
  "data_available": true,
  "total_districts": 50,
  "standards": { "th": 37.5, "who": 15.0 },
  "data": [
    {
      "dcode": "1026",
      "district_name": "ดินแดง",
      "district_name_en": "Din Daeng",
      "zone_code": "05",
      "avg_pm25": 62.3,
      "avg_aqi": 155,
      "nearest_station": "ดินแดง",
      "station_name_th": "ดินแดง",
      "days_exceeded": null,
      "avg_2025": null,
      "avg_2026_q1": null,
      "trend": null
    }
  ]
}
```

### PM2.5 Monthly (`GET /api/v2/gis/pm25/monthly?zone_code=01`)

```json
{
  "zone_code": "01",
  "period": [],
  "current_snapshot": { "avg_pm25": 52.3, "avg_aqi": 142, "station_count": 6 },
  "standards": { "th": 37.5, "who": 15.0 },
  "historical_data_available": false
}
```

---

## 14. Data Preprocessing & Computed Variables

Raw CSV data from the BMA health screening portal contains measurements but not all derived variables the frontend needs. The ETL pipeline and materialized views compute these automatically.

### Computed at ETL Import Time

| Variable | Formula | Stored In | Source CSV |
|----------|---------|-----------|------------|
| **Age** | `current_year - birth_year` | `raw_patients.age` | `pt.csv` BIRTHDATE |
| **Age Group** | Thai lifecycle cohorts (6 groups) | `raw_patients.age_group` | `pt.csv` BIRTHDATE |
| **BMI** | `weight_kg / (height_cm / 100)^2` | `raw_vitalsigns.bmi` | `vitalsignslf.csv` HEIGHT + WEIGHT |

### Computed in Materialized Views

| Variable | View | SQL Logic |
|----------|------|-----------|
| **% Risk DM/HPT/CVD** | `summary_district_disease` | `COUNT(FILTER WHERE risk_dm) / total_screened * 100` |
| **% Found DM/HPT/CVD/Stroke** | `summary_district_disease` | `COUNT(FILTER WHERE found_dm) / total_screened * 100` |
| **Avg SBP/DBP/BMI/Waist** | `summary_district_risk_factors` | `AVG()` grouped by district, sex, age_group, smoking, alcohol, exercise |
| **BMI Categories** | `summary_bmi_waist` | `<18.5` underweight, `18.5-23` normal, `23-25` overweight, `25-30` obese, `>=30` severely obese |
| **Waist Risk** | `summary_bmi_waist` | Male >=90cm, Female >=80cm (Asia-Pacific thresholds) |
| **% Depression Risk** | `summary_district_mental` | PHQ-2 Q1>=1 OR Q2>=1 |
| **% Moderate Depression** | `summary_district_mental` | PHQ-9 total >= 10 |
| **% High Stress** | `summary_district_mental` | ST-5 total >= 7 |
| **% Anemia** | `summary_district_lab` | Hemoglobin < 12 g/dL |
| **% CKD** | `summary_district_lab` | eGFR < 60 mL/min |
| **EKG/Xray/Vision/DR Rates** | `summary_screening_tests` | Count done, normal, abnormal per test |
| **Chronic Disease History** | `summary_chronic_history` | From homehealth: known conditions, treatment adherence, vaccination |
| **Family History** | `summary_family_history` | Family DM, parent DM/HPT/stroke/heart/kidney |
| **Comorbidity Pairs** | `summary_comorbidity` | DM+HPT, DM+obesity, metabolic syndrome, multi-disease count |

### Risk Flags (Pre-computed in Source CSV)

These come directly from the screening portal as binary flags -- **not** recomputed by our system:

| Flag | CSV Column | DB Column | Meaning |
|------|-----------|-----------|---------|
| `RISKDM` | vitalsignslf.csv | `risk_dm` BOOLEAN | At risk for diabetes |
| `RISKHPT` | vitalsignslf.csv | `risk_hpt` BOOLEAN | At risk for hypertension |
| `RISKCDVCL` | vitalsignslf.csv | `risk_cvd` BOOLEAN | At risk for cardiovascular disease |
| `RISKBMI` | vitalsignslf.csv | `risk_bmi` BOOLEAN | At risk for obesity |
| `DM` | vitalsignslf.csv | `found_dm` BOOLEAN | Diagnosed with diabetes |
| `HPT` | vitalsignslf.csv | `found_hpt` BOOLEAN | Diagnosed with hypertension |
| `CDVCL` | vitalsignslf.csv | `found_cvd` BOOLEAN | Diagnosed with CVD |
| `STROKE` | vitalsignslf.csv | `found_stroke` BOOLEAN | Diagnosed with stroke |
| `FAT` | vitalsignslf.csv | `found_obesity` BOOLEAN | Diagnosed with obesity |
| `CHLTR` | vitalsignslf.csv | `found_dyslipidemia` BOOLEAN | Diagnosed with dyslipidemia |

---

## 15. Database Schema & Materialized Views

### Raw Tables (7)

| Table | Source CSV | Key Columns | Records |
|-------|-----------|-------------|---------|
| `raw_patients` | pt.csv | idcard_hash, sex, birth_year, **age**, age_group | ~1M |
| `raw_vitalsigns` | vitalsignslf.csv | SBP, DBP, height, weight, **BMI**, waist, risk flags, disease flags, mental health scores | ~1M |
| `raw_visits` | pthistory.csv | visit_date, facility_code | ~1M |
| `raw_homevisit` | homevisit.csv | education, occupation, health_privilege, home_type | ~800K |
| `raw_homehealth` | homehealth.csv | chronic disease history, treatment, exercise, vaccination, family history | ~800K |
| `raw_lab_results` | labhealth.csv | FBS, cholesterol, TG, HDL, LDL, hemoglobin, creatinine, eGFR | ~500K |
| `raw_lab_extended` | labhealthext.csv | respiratory symptoms, musculoskeletal pain | ~500K |

### Materialized Views (13)

| View | Groups By | Key Metrics |
|------|-----------|-------------|
| `summary_district_disease` | district | Total screened, risk counts, disease counts, percentages |
| `summary_district_risk_factors` | district, sex, age_group, smoking, alcohol, exercise | Avg SBP/DBP/BMI/waist, patient count |
| `summary_district_lab` | district | Avg lab values, % anemia, % CKD |
| `summary_district_mental` | district | % depression risk, % PHQ-9 moderate, % high stress |
| `summary_district_demographics` | district | Education, occupation, insurance, housing counts |
| `summary_bmi_waist` | district, sex | BMI categories, waist risk, avg height/weight |
| `summary_disease_age_sex` | district, sex, age_group | Disease counts by demographic |
| `summary_comorbidity` | district | Disease pair counts, metabolic syndrome |
| `summary_lab_disease_cross` | district | Lab values stratified by disease status |
| `summary_facility` | facility, district | Screening counts, lab completion |
| **`summary_screening_tests`** | district | **EKG, X-ray, vision, DR screening rates** |
| **`summary_chronic_history`** | district | **Known conditions, treatment adherence, vaccination** |
| **`summary_family_history`** | district | **Family DM, parent disease history** |

### Migrations

| File | Description |
|------|-------------|
| `001_create_raw_tables.sql` | 7 raw tables + indexes |
| `002_create_materialized_views.sql` | Core 5 views (disease, risk, lab, mental, demographics) |
| `003_create_import_history.sql` | ETL audit table |
| `004_add_unique_constraints.sql` | Unique indexes for REFRESH CONCURRENTLY |
| `005_pdpa_compliance.sql` | Privacy controls |
| `006_expanded_views.sql` | 5 more views (disease_age_sex, bmi_waist, facility, comorbidity, lab_cross) |
| `007_facility_expansion.sql` | Facility reference data |
| **`008_add_computed_columns.sql`** | **Add age + BMI columns, backfill from existing data** |
| **`009_new_materialized_views.sql`** | **3 new views: screening_tests, chronic_history, family_history** |
| **`010_pm25_daily.sql`** | **PM2.5 daily readings table (dcode + date, for historical trends)** |

---

## 16. CORS Configuration

**Default allowed origins:** `http://localhost:3000`, `http://localhost:5173`
**Allowed methods:** `GET`, `POST`
**Allowed headers:** `X-API-Key`, `Content-Type`, `Authorization`
**Credentials:** Enabled

> Production: Set `CORS_ORIGINS` env var to your frontend domain(s).

---

## 17. Caching Behavior

| Tier | TTL | Endpoints |
|------|-----|-----------|
| T1 | 5 min | External data (PM2.5 raw from ArcGIS) |
| T2 | 15 min | Aggregate summaries (`/summary/overview`, `/executive/headline-kpi`, `/gis/pm25/zones`, `/gis/pm25/districts`, `/gis/pm25/monthly`) |
| T3 | 1 hour | Filtered queries (`/districts/{dcode}`, `/trends/*`) |
| T4 | 24 hours | Static/reference (`/search/districts`, `/research/data-dictionary`) |

Reports are cached as PDF files on disk until explicitly invalidated.

---

## 18. Running Services

### Prerequisites

- Python 3.12+
- PostgreSQL 16 (or Docker)
- Redis 7 (or Docker)
- **Optional:** LMStudio with Gemma 4 model (for chat)
- **Optional:** Tectonic (for LaTeX PDF generation)

### Quick Start

```bash
cp .env.example .env        # Configure environment
make install                # Install Python dependencies
make infra                  # Start PostgreSQL + Redis (Docker)
make migrate                # Run all database migrations
make seed                   # Load reference data
make dev                    # Start API server (port 9002)
```

### Available Make Targets

| Command | Description |
|---------|-------------|
| `make dev` | Start API locally with hot-reload (port 9002) |
| `make up` | Start all via Docker Compose |
| `make down` | Stop Docker services |
| `make infra` | Start only PostgreSQL + Redis |
| `make install` | Install Python dependencies |
| `make migrate` | Run all database migrations (001-010) |
| `make seed` | Load reference/seed data |
| `make test` | Run full test suite (224 tests) |
| `make health` | Check API health |
| `make status` | Show all service status + endpoint count |
| `make endpoints` | List all endpoint groups with counts |
| `make docs` | Open Swagger UI in browser |
| `make redoc` | Open ReDoc in browser |
| `make chat-test` | Test LLM chat with a sample question |
| `make agent-tools` | List registered agent tools (7) |
| `make generate-reports` | Trigger PDF report generation |
| `make report-catalog` | List available reports with cache status |
| `make report-status` | Check generation progress |
| `make db-stats` | Show row counts for all tables and views |
| `make refresh-views` | Refresh all 13 materialized views |
| `make clean` | Stop and remove volumes |
| `make clean-reports` | Remove generated PDF cache |

### Environment Variables

```env
# Database
DATABASE_URL=postgresql://postgres:bma_health_dev@localhost:5433/bma_health
REDIS_URL=redis://localhost:6379/0

# API Auth
API_KEY=dev-api-key
ADMIN_PASSWORD=admin
SECRET_KEY=change-me-in-production

# LLM (optional -- chat will return 503 if unavailable)
LMSTUDIO_URL=http://localhost:5555
LLM_MODEL=google/gemma-4-26b-a4b

# Reports (optional -- PDF endpoints will return 404 if not generated)
TECTONIC_PATH=/opt/homebrew/bin/tectonic
REPORTS_DIR=./data/reports

# CORS
CORS_ORIGINS=http://localhost:3000,http://localhost:5173
```

---

## Quick Reference: Interactive API Explorer

```
Swagger UI:   http://localhost:9002/docs
ReDoc:         http://localhost:9002/redoc
OpenAPI JSON:  http://localhost:9002/openapi.json
```

Auto-generated from FastAPI source. Always reflects the current state of all 150 endpoints.

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 4.2.0 | 2026-04-10 | Agent: 7 tools (added `query_api` with 25 endpoints), Gap #1-5 fixes, data quality safeguards |
| 4.1.0 | 2026-04-10 | ETL: age + BMI preprocessing, 3 new materialized views (screening_tests, chronic_history, family_history) |
| 4.0.0 | 2026-04-10 | One-stop backend: consolidated LLM chat, LaTeX reports, export, stats from 2 servers to 1 |
| 3.0.0 | 2026-04-10 | Initial V2 data API: 85 endpoints, 16 domain groups |
