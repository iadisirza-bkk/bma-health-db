# BMA Health DB -- API Documentation

> Summary API v2 for Bangkok Metropolitan Administration health screening data.
> Serves **aggregate data only** -- no individual records, no PII.

Base URL: `https://your-domain.com` (or `http://localhost:8000` in development)

---

## Table of Contents

- [Quick Start](#quick-start)
- [Authentication](#authentication)
- [Rate Limiting](#rate-limiting)
- [k-Anonymity Policy](#k-anonymity-policy)
- [Error Handling](#error-handling)
- [Endpoints](#endpoints)
  - [System](#system)
  - [Summary](#summary)
  - [Zones](#zones)
  - [Districts](#districts)
  - [Trends](#trends)
  - [Search](#search)
  - [Admin (session auth)](#admin-session-auth)
- [Reference Tables](#reference-tables)
- [Frontend Integration](#frontend-integration)

---

## Quick Start

```bash
# Health check (no auth required)
curl http://localhost:8000/health

# Fetch screening overview
curl -H "X-API-Key: your-api-key" http://localhost:8000/api/v2/summary/overview

# List all zones with disease breakdown
curl -H "X-API-Key: your-api-key" http://localhost:8000/api/v2/summary/zones

# Get a specific district
curl -H "X-API-Key: your-api-key" http://localhost:8000/api/v2/summary/districts/1001
```

---

## Authentication

All API endpoints (except `/health`) require an API key passed via the `X-API-Key` header.

```
X-API-Key: your-api-key
```

The API key is configured via the `API_KEY` environment variable. The default development key is `changeme-dev-key` -- this must be changed in production.

**Paths that do NOT require an API key:**
- `GET /health`
- `GET /docs` (development only)
- `GET /redoc` (development only)
- `GET /openapi.json` (development only)
- All `/admin/*` paths (use session auth instead)
- All `/static/*` paths

**Admin routes** use cookie-based session authentication (see [Admin](#admin-session-auth)).

### Error Response

```json
{
  "detail": "Invalid or missing API key"
}
```
Status: `401 Unauthorized`

---

## Rate Limiting

In-memory sliding-window rate limiter, per IP address.

| Tier   | Limit                          | Window |
|--------|--------------------------------|--------|
| Public | 60 requests (configurable via `RATE_LIMIT_PUBLIC`) | 60 seconds |

Rate limiting applies to all API endpoints. Health checks, admin routes, and static files are exempt.

### Error Response

```json
{
  "detail": "Rate limit exceeded. Try again later."
}
```
Status: `429 Too Many Requests`

---

## k-Anonymity Policy

All endpoints enforce **k-anonymity with k=5**. This prevents identification of individuals in small groups.

**How it works:**

1. **District-level endpoints** -- Districts with `total_screened < 5` are completely excluded from list results. Single-district lookups return `403 Forbidden`.
2. **Filtered queries** -- Rows where `patient_count < 5` are removed from results.
3. **Trend endpoints** -- Time periods with `screened_count < 5` or `total_screened < 5` are suppressed.
4. **Lab/Mental health/Demographics** -- Districts with `total_lab_patients < 5`, `total_screened < 5`, or `total_respondents < 5` are excluded.

Suppressed rows are **completely excluded** (not masked or replaced with zeros) to prevent differential attacks.

### Suppression Example

```json
{
  "detail": "Data suppressed for privacy (k-anonymity)"
}
```
Status: `403 Forbidden` (when requesting a single district with insufficient data)

---

## Error Handling

All errors follow the standard FastAPI error format:

```json
{
  "detail": "Error description here"
}
```

| Status | Meaning |
|--------|---------|
| 400 | Bad request -- invalid parameter value |
| 401 | Missing or invalid API key |
| 403 | Data suppressed for privacy (k-anonymity) or CSRF check failed |
| 404 | Resource not found |
| 429 | Rate limit exceeded |
| 500 | Internal server error |

---

## Endpoints

### System

#### GET /health

Health check with database connectivity status. **No API key required.**

```bash
curl http://localhost:8000/health
```

**Response** `200 OK`:

```json
{
  "status": "ok",
  "database": "connected",
  "timestamp": "2025-03-15T08:30:00.000000"
}
```

When the database is unreachable:

```json
{
  "status": "degraded",
  "database": "disconnected",
  "timestamp": "2025-03-15T08:30:00.000000"
}
```

| Field | Type | Description |
|-------|------|-------------|
| status | string | `"ok"` or `"degraded"` |
| database | string | `"connected"` or `"disconnected"` |
| timestamp | string | ISO 8601 UTC timestamp |

---

### Summary

#### GET /api/v2/summary/overview

Top-level screening overview with zone and disease breakdowns. ภาพรวมการคัดกรองสุขภาพ

```bash
curl -H "X-API-Key: your-api-key" http://localhost:8000/api/v2/summary/overview
```

**Response** `200 OK`:

```json
{
  "total_screened": 1245832,
  "target": 1600000,
  "zones_count": 8,
  "districts_count": 50,
  "last_updated": "2025-03-15 10:00:00+07",
  "by_zone": [
    {
      "zone_code": "01",
      "name_th": "เขตสุขภาพที่ 1",
      "total_screened": 156420
    },
    {
      "zone_code": "02",
      "name_th": "เขตสุขภาพที่ 2",
      "total_screened": 148930
    }
  ],
  "by_disease": [
    {
      "disease_key": "diabetes",
      "total_at_risk": 98452,
      "pct": 7.9
    },
    {
      "disease_key": "hypertension",
      "total_at_risk": 186734,
      "pct": 14.99
    },
    {
      "disease_key": "cardiovascular",
      "total_at_risk": 45231,
      "pct": 3.63
    },
    {
      "disease_key": "obesity",
      "total_at_risk": 312456,
      "pct": 25.08
    },
    {
      "disease_key": "dyslipidemia",
      "total_at_risk": 67890,
      "pct": 5.45
    },
    {
      "disease_key": "stroke",
      "total_at_risk": 12345,
      "pct": 0.99
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| total_screened | integer | Total unique screenings across all districts |
| target | integer | Target screening goal (fixed at 1,600,000) |
| zones_count | integer | Number of health zones |
| districts_count | integer | Number of districts |
| last_updated | string or null | Latest `refreshed_at` from summary data |
| by_zone | array | Zone-level screening totals |
| by_disease | array | Disease-level risk counts and percentages |

**Diseases reported:** diabetes, hypertension, cardiovascular, obesity, dyslipidemia, stroke.

---

#### GET /api/v2/summary/filtered

Query risk factor summary with demographic/behavioral filters. k-anonymity enforced.
สรุปปัจจัยเสี่ยงตามตัวกรอง (เพศ, กลุ่มอายุ, สูบบุหรี่, ออกกำลังกาย)

```bash
curl -H "X-API-Key: your-api-key" \
  "http://localhost:8000/api/v2/summary/filtered?district=1001&sex=1&age_group=40-49"
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| district | string | null | Filter by district code (e.g. `1001`) |
| sex | integer | null | Filter by sex (`1` = male, `2` = female) |
| age_group | string | null | Filter by age group (e.g. `15-19`, `20-29`, `30-39`, `40-49`, `50-59`, `60-69`, `70+`) |
| smoking | integer | null | Filter by smoking status |
| exercise | integer | null | Filter by exercise status |

**Response** `200 OK`:

```json
{
  "filters_applied": {
    "district": "1001",
    "sex": 1,
    "age_group": "40-49",
    "smoking": null,
    "exercise": null
  },
  "k_anonymity_threshold": 5,
  "data": [
    {
      "district_code": "1001",
      "sex": 1,
      "age_group": "40-49",
      "smoking": 0,
      "exercise": 1,
      "patient_count": 142,
      "avg_sbp": 128.3,
      "avg_dbp": 82.1,
      "avg_weight_kg": 68.5,
      "avg_waist_cm": 84.2,
      "avg_bmi": 24.8
    }
  ]
}
```

**k-anonymity:** Rows with `patient_count < 5` are excluded from `data`.

---

#### GET /api/v2/summary/lab

Lab results summary by district. สรุปผลตรวจทางห้องปฏิบัติการ

```bash
# All districts
curl -H "X-API-Key: your-api-key" http://localhost:8000/api/v2/summary/lab

# Filter by district
curl -H "X-API-Key: your-api-key" "http://localhost:8000/api/v2/summary/lab?dcode=1004"

# Filter by zone
curl -H "X-API-Key: your-api-key" "http://localhost:8000/api/v2/summary/lab?zone_code=03"
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| dcode | string | null | Filter by district code |
| zone_code | string | null | Filter by zone code |

**Response** `200 OK`:

```json
[
  {
    "district_code": "1004",
    "total_lab_patients": 3420,
    "avg_hemoglobin": 13.25,
    "avg_fbs": 102.34,
    "avg_cholesterol": 198.56,
    "avg_triglyceride": 145.23,
    "avg_hdl": 52.18,
    "avg_ldl": 124.67,
    "avg_creatinine": 0.92,
    "avg_egfr": 85.43,
    "pct_anemia": 8.5,
    "pct_ckd": 3.2
  }
]
```

**k-anonymity:** Districts with `total_lab_patients < 5` are excluded.

---

#### GET /api/v2/summary/mental-health

Mental health screening summary. สรุปการคัดกรองสุขภาพจิต

```bash
curl -H "X-API-Key: your-api-key" http://localhost:8000/api/v2/summary/mental-health

# Filter by district or zone
curl -H "X-API-Key: your-api-key" "http://localhost:8000/api/v2/summary/mental-health?zone_code=05"
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| dcode | string | null | Filter by district code |
| zone_code | string | null | Filter by zone code |

**Response** `200 OK`:

```json
[
  {
    "district_code": "1014",
    "total_screened": 4521,
    "pct_depression_risk": 12.3,
    "pct_phq9_moderate": 5.8,
    "pct_high_stress": 18.7
  }
]
```

| Field | Type | Description |
|-------|------|-------------|
| pct_depression_risk | float | % screened at risk for depression |
| pct_phq9_moderate | float | % with PHQ-9 score indicating moderate depression |
| pct_high_stress | float | % reporting high stress levels |

**k-anonymity:** Districts with `total_screened < 5` are excluded.

---

#### GET /api/v2/summary/demographics

Demographic breakdown by district. สรุปข้อมูลประชากร (การศึกษา, อาชีพ, สิทธิ์, ที่อยู่)

```bash
curl -H "X-API-Key: your-api-key" http://localhost:8000/api/v2/summary/demographics

# Filter by district or zone
curl -H "X-API-Key: your-api-key" "http://localhost:8000/api/v2/summary/demographics?dcode=1002"
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| dcode | string | null | Filter by district code |
| zone_code | string | null | Filter by zone code |

**Response** `200 OK`:

```json
[
  {
    "district_code": "1002",
    "total_respondents": 8432,
    "edu_none": 102,
    "edu_primary": 2341,
    "edu_secondary": 1876,
    "edu_high_school": 1543,
    "edu_vocational": 890,
    "edu_bachelor": 1234,
    "edu_postgrad": 446,
    "occ_government": 1245,
    "occ_private": 2678,
    "occ_self_employed": 1456,
    "occ_agriculture": 123,
    "occ_unemployed": 987,
    "occ_student": 654,
    "occ_retired": 1289,
    "priv_ucs": 4567,
    "priv_sso": 2345,
    "priv_csmbs": 1234,
    "priv_other": 286,
    "house_owned": 5432,
    "house_rented": 2134,
    "house_condo": 567,
    "house_other": 299
  }
]
```

**Field categories:**

| Prefix | Category | Description |
|--------|----------|-------------|
| edu_ | Education level | ระดับการศึกษา (none / primary / secondary / high_school / vocational / bachelor / postgrad) |
| occ_ | Occupation | อาชีพ (government / private / self_employed / agriculture / unemployed / student / retired) |
| priv_ | Insurance privilege | สิทธิ์การรักษา (ucs = บัตรทอง, sso = ประกันสังคม, csmbs = สวัสดิการข้าราชการ, other) |
| house_ | Housing type | ประเภทที่อยู่อาศัย (owned / rented / condo / other) |

**k-anonymity:** Districts with `total_respondents < 5` are excluded.

---

### Zones

#### GET /api/v2/summary/zones

All 8 health zones with screening totals and disease breakdown. รายชื่อเขตสุขภาพทั้ง 8 เขต

```bash
curl -H "X-API-Key: your-api-key" http://localhost:8000/api/v2/summary/zones
```

**Response** `200 OK`:

```json
[
  {
    "zone_code": "01",
    "name_th": "เขตสุขภาพที่ 1",
    "name_en": "Health Zone 1",
    "district_count": 6,
    "total_screened": 156420,
    "diseases": {
      "diabetes": { "count": 12543, "pct": 8.02 },
      "hypertension": { "count": 23456, "pct": 14.99 },
      "cardiovascular": { "count": 5678, "pct": 3.63 },
      "obesity": { "count": 39210, "pct": 25.07 },
      "dyslipidemia": { "count": 8765, "pct": 5.6 },
      "stroke": { "count": 1567, "pct": 1.0 }
    }
  },
  {
    "zone_code": "02",
    "name_th": "เขตสุขภาพที่ 2",
    "name_en": "Health Zone 2",
    "district_count": 6,
    "total_screened": 148930,
    "diseases": {
      "diabetes": { "count": 11234, "pct": 7.54 },
      "hypertension": { "count": 21890, "pct": 14.7 },
      "cardiovascular": { "count": 4567, "pct": 3.07 },
      "obesity": { "count": 36789, "pct": 24.7 },
      "dyslipidemia": { "count": 7654, "pct": 5.14 },
      "stroke": { "count": 1234, "pct": 0.83 }
    }
  }
]
```

---

#### GET /api/v2/summary/zones/{zone_code}

Single zone with its districts and disease data. รายละเอียดเขตสุขภาพแต่ละเขต

```bash
curl -H "X-API-Key: your-api-key" http://localhost:8000/api/v2/summary/zones/04
```

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| zone_code | string | Zone code: `01` through `08` |

**Response** `200 OK`:

```json
{
  "zone_code": "04",
  "name_th": "เขตสุขภาพที่ 4",
  "name_en": "Health Zone 4",
  "districts": [
    {
      "district_code": "1001",
      "district_name": "พระนคร",
      "total_screened": 8432,
      "risk_dm_count": 678,
      "pct_risk_dm": 8.04,
      "risk_hpt_count": 1245,
      "pct_risk_hpt": 14.77,
      "risk_cvd_count": 312,
      "pct_risk_cvd": 3.7,
      "risk_bmi_count": 2134,
      "found_obesity_count": 1567,
      "found_dyslipidemia_count": 456,
      "found_stroke_count": 89
    },
    {
      "district_code": "1002",
      "district_name": "ดุสิต",
      "total_screened": 12456,
      "risk_dm_count": 987,
      "pct_risk_dm": 7.92,
      "risk_hpt_count": 1876,
      "pct_risk_hpt": 15.06,
      "risk_cvd_count": 456,
      "pct_risk_cvd": 3.66,
      "risk_bmi_count": 3123,
      "found_obesity_count": 2345,
      "found_dyslipidemia_count": 678,
      "found_stroke_count": 134
    }
  ]
}
```

**k-anonymity:** Districts with `total_screened < 5` are excluded from the `districts` array.

**Error** `404 Not Found`:

```json
{
  "detail": "Zone not found"
}
```

---

### Districts

#### GET /api/v2/summary/districts

List all districts, optionally filtered by zone. รายชื่อเขตทั้งหมด

```bash
# All districts
curl -H "X-API-Key: your-api-key" http://localhost:8000/api/v2/summary/districts

# Filter by zone
curl -H "X-API-Key: your-api-key" "http://localhost:8000/api/v2/summary/districts?zone_code=03"
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| zone_code | string | null | Filter by zone code (`01`-`08`) |

**Response** `200 OK`:

```json
[
  {
    "district_code": "1004",
    "district_name": "บางรัก",
    "zone_code": "03",
    "total_screened": 6789,
    "risk_dm_count": 543,
    "pct_risk_dm": 7.99,
    "risk_hpt_count": 1023,
    "pct_risk_hpt": 15.07,
    "risk_cvd_count": 245,
    "pct_risk_cvd": 3.61,
    "found_obesity_count": 1678,
    "found_dyslipidemia_count": 345,
    "found_stroke_count": 67
  },
  {
    "district_code": "1007",
    "district_name": "ปทุมวัน",
    "zone_code": "03",
    "total_screened": 5432,
    "risk_dm_count": 432,
    "pct_risk_dm": 7.95,
    "risk_hpt_count": 812,
    "pct_risk_hpt": 14.95,
    "risk_cvd_count": 198,
    "pct_risk_cvd": 3.65,
    "found_obesity_count": 1345,
    "found_dyslipidemia_count": 278,
    "found_stroke_count": 54
  }
]
```

**k-anonymity:** Districts with `total_screened < 5` are excluded.

---

#### GET /api/v2/summary/districts/{dcode}

Full district detail: diseases, lab results, mental health, and demographics.
ข้อมูลสุขภาพครบทุกมิติของเขตใดเขตหนึ่ง

```bash
curl -H "X-API-Key: your-api-key" http://localhost:8000/api/v2/summary/districts/1002
```

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| dcode | string | 4-digit district code (e.g. `1001`, `1002`) |

**Response** `200 OK`:

```json
{
  "disease": {
    "district_code": "1002",
    "zone_code": "04",
    "district_name": "ดุสิต",
    "total_screened": 12456,
    "risk_dm_count": 987,
    "pct_risk_dm": 7.92,
    "risk_hpt_count": 1876,
    "pct_risk_hpt": 15.06,
    "risk_cvd_count": 456,
    "pct_risk_cvd": 3.66,
    "risk_bmi_count": 3123,
    "found_dm_count": 534,
    "pct_found_dm": 4.29,
    "found_hpt_count": 1234,
    "pct_found_hpt": 9.91,
    "found_cvd_count": 234,
    "pct_found_cvd": 1.88,
    "found_obesity_count": 2345,
    "found_dyslipidemia_count": 678,
    "found_stroke_count": 134
  },
  "lab_summary": {
    "district_code": "1002",
    "total_lab_patients": 5678,
    "avg_hemoglobin": 13.42,
    "avg_hematocrit": 40.1,
    "avg_fbs": 104.56,
    "avg_cholesterol": 201.34,
    "avg_triglyceride": 148.67,
    "avg_hdl": 51.23,
    "avg_ldl": 126.45,
    "avg_creatinine": 0.94,
    "avg_egfr": 83.67,
    "avg_uric_acid": 5.8,
    "avg_sgot": 24.3,
    "avg_sgpt": 28.7,
    "pct_anemia": 9.2,
    "pct_ckd": 3.5
  },
  "mental_health": {
    "district_code": "1002",
    "total_screened": 4567,
    "pct_depression_risk": 11.8,
    "pct_phq9_moderate": 5.4,
    "pct_high_stress": 17.9
  },
  "demographics": {
    "district_code": "1002",
    "total_respondents": 8432,
    "edu_none": 102,
    "edu_primary": 2341,
    "edu_secondary": 1876,
    "edu_high_school": 1543,
    "edu_vocational": 890,
    "edu_bachelor": 1234,
    "edu_postgrad": 446,
    "occ_government": 1245,
    "occ_private": 2678,
    "occ_self_employed": 1456,
    "occ_agriculture": 123,
    "occ_unemployed": 987,
    "occ_student": 654,
    "occ_retired": 1289,
    "priv_ucs": 4567,
    "priv_sso": 2345,
    "priv_csmbs": 1234,
    "priv_other": 286,
    "house_owned": 5432,
    "house_rented": 2134,
    "house_condo": 567,
    "house_other": 299
  }
}
```

Any of the four sections may be `null` if no data exists for that district.

**k-anonymity:** Returns `403` if `total_screened < 5`.

**Error** `403 Forbidden`:

```json
{
  "detail": "Data suppressed for privacy (k-anonymity)"
}
```

**Error** `404 Not Found`:

```json
{
  "detail": "District not found"
}
```

---

#### GET /api/v2/summary/districts/{dcode}/disease/{disease_key}

Disease detail for a district with risk factor breakdown.
ข้อมูลโรคเฉพาะของเขตพร้อมปัจจัยเสี่ยง

```bash
curl -H "X-API-Key: your-api-key" \
  http://localhost:8000/api/v2/summary/districts/1001/disease/diabetes
```

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| dcode | string | 4-digit district code |
| disease_key | string | One of: `diabetes`, `hypertension`, `cardiovascular`, `obesity`, `dyslipidemia`, `stroke`, `ckd`, `anemia` |

**Response for vitalsign-based diseases** (diabetes, hypertension, cardiovascular, obesity, dyslipidemia, stroke) `200 OK`:

```json
{
  "district_code": "1001",
  "disease_key": "diabetes",
  "disease_summary": {
    "district_code": "1001",
    "zone_code": "04",
    "district_name": "พระนคร",
    "total_screened": 8432,
    "risk_dm_count": 678,
    "pct_risk_dm": 8.04,
    "risk_hpt_count": 1245,
    "pct_risk_hpt": 14.77,
    "risk_cvd_count": 312,
    "pct_risk_cvd": 3.7,
    "risk_bmi_count": 2134,
    "found_dm_count": 345,
    "pct_found_dm": 4.09,
    "found_hpt_count": 890,
    "pct_found_hpt": 10.56,
    "found_cvd_count": 156,
    "pct_found_cvd": 1.85,
    "found_obesity_count": 1567,
    "found_dyslipidemia_count": 456,
    "found_stroke_count": 89
  },
  "risk_factor_breakdown": [
    {
      "sex": 1,
      "age_group": "40-49",
      "smoking": 0,
      "exercise": 1,
      "patient_count": 87,
      "avg_sbp": 126.4,
      "avg_dbp": 80.2,
      "avg_bmi": 24.3
    },
    {
      "sex": 2,
      "age_group": "50-59",
      "smoking": 0,
      "exercise": 0,
      "patient_count": 134,
      "avg_sbp": 132.8,
      "avg_dbp": 84.6,
      "avg_bmi": 26.1
    }
  ]
}
```

**Response for lab-based diseases** (ckd, anemia) `200 OK`:

```json
{
  "district_code": "1001",
  "disease_key": "ckd",
  "source": "lab",
  "lab_summary": {
    "district_code": "1001",
    "total_lab_patients": 3210,
    "avg_hemoglobin": 13.15,
    "avg_hematocrit": 39.8,
    "avg_fbs": 101.23,
    "avg_cholesterol": 196.45,
    "avg_triglyceride": 142.67,
    "avg_hdl": 53.12,
    "avg_ldl": 122.34,
    "avg_creatinine": 0.91,
    "avg_egfr": 86.54,
    "avg_uric_acid": 5.6,
    "avg_sgot": 23.4,
    "avg_sgpt": 27.8,
    "pct_anemia": 8.1,
    "pct_ckd": 3.0
  }
}
```

**k-anonymity:** `risk_factor_breakdown` rows with `patient_count < 5` are excluded.

**Error** `400 Bad Request`:

```json
{
  "detail": "Invalid disease_key 'invalid'. Valid keys: ['anemia', 'cardiovascular', 'ckd', 'diabetes', 'dyslipidemia', 'hypertension', 'obesity', 'stroke']"
}
```

---

### Trends

#### GET /api/v2/trends/screening

Time series of screening counts. แนวโน้มจำนวนการคัดกรอง

```bash
# Monthly (default)
curl -H "X-API-Key: your-api-key" http://localhost:8000/api/v2/trends/screening

# Quarterly, filtered by zone
curl -H "X-API-Key: your-api-key" \
  "http://localhost:8000/api/v2/trends/screening?granularity=quarterly&zone_code=01"
```

**Query Parameters:**

| Parameter | Type | Default | Constraints | Description |
|-----------|------|---------|-------------|-------------|
| granularity | string | `"monthly"` | `monthly` or `quarterly` | Time aggregation level |
| zone_code | string | null | `01`-`08` | Filter by health zone |

**Response** `200 OK`:

```json
{
  "granularity": "monthly",
  "zone_code": null,
  "data": [
    { "period": "2024-10-01", "screened_count": 45678 },
    { "period": "2024-11-01", "screened_count": 52341 },
    { "period": "2024-12-01", "screened_count": 48923 },
    { "period": "2025-01-01", "screened_count": 56789 },
    { "period": "2025-02-01", "screened_count": 61234 }
  ]
}
```

**k-anonymity:** Periods with `screened_count < 5` are excluded.

**Error** `400 Bad Request`:

```json
{
  "detail": "granularity must be one of ['monthly', 'quarterly']"
}
```

---

#### GET /api/v2/trends/disease/{disease_key}

Time series of disease prevalence. แนวโน้มอัตราความเสี่ยงโรค

```bash
curl -H "X-API-Key: your-api-key" \
  "http://localhost:8000/api/v2/trends/disease/hypertension?granularity=monthly&district=1002"
```

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| disease_key | string | One of: `diabetes`, `hypertension`, `cardiovascular`, `obesity`, `dyslipidemia`, `stroke` |

**Query Parameters:**

| Parameter | Type | Default | Constraints | Description |
|-----------|------|---------|-------------|-------------|
| granularity | string | `"monthly"` | `monthly` or `quarterly` | Time aggregation level |
| district | string | null | 4-digit district code | Filter by district |

**Response** `200 OK`:

```json
{
  "disease_key": "hypertension",
  "granularity": "monthly",
  "district": "1002",
  "data": [
    {
      "period": "2024-10-01",
      "total_screened": 8765,
      "at_risk_count": 1312,
      "pct": 14.97
    },
    {
      "period": "2024-11-01",
      "total_screened": 9876,
      "at_risk_count": 1523,
      "pct": 15.42
    }
  ]
}
```

**k-anonymity:** Periods with `total_screened < 5` are excluded.

**Unsupported disease keys** (`ckd`, `anemia`) return:

```json
{
  "detail": "Trend data not available for 'ckd'. Use /api/v2/summary/lab instead."
}
```
Status: `400 Bad Request`

---

### Search

#### GET /api/v2/search/districts

Search and rank districts by disease prevalence. ค้นหาและจัดอันดับเขตตามอัตราความชุกของโรค

```bash
# Top 10 districts by diabetes prevalence
curl -H "X-API-Key: your-api-key" \
  "http://localhost:8000/api/v2/search/districts?disease=diabetes&sort_by=pct_desc&limit=10"

# Districts with hypertension > 20%
curl -H "X-API-Key: your-api-key" \
  "http://localhost:8000/api/v2/search/districts?disease=hypertension&min_pct=20"
```

**Query Parameters:**

| Parameter | Type | Default | Constraints | Description |
|-----------|------|---------|-------------|-------------|
| disease | string | **required** | Valid disease key | Disease to rank by |
| min_pct | float | null | 0-100 | Minimum prevalence % |
| max_pct | float | null | 0-100 | Maximum prevalence % |
| sort_by | string | `"pct_desc"` | `pct_desc`, `pct_asc`, `count_desc`, `count_asc` | Sort order |
| limit | integer | 50 | 1-200 | Max results |

**Response** `200 OK` (for vitalsign-based diseases):

```json
{
  "disease": "diabetes",
  "results": [
    {
      "district_code": "1003",
      "district_name": "หนองจอก",
      "zone_code": "08",
      "total_screened": 24567,
      "disease_count": 2456,
      "disease_pct": 10.0
    },
    {
      "district_code": "1042",
      "district_name": "คลองสามวา",
      "zone_code": "08",
      "total_screened": 28901,
      "disease_count": 2745,
      "disease_pct": 9.5
    }
  ]
}
```

**Response** `200 OK` (for lab-based diseases: ckd, anemia):

```json
{
  "disease": "ckd",
  "results": [
    {
      "district_code": "1031",
      "district_name": "จอมทอง",
      "zone_code": "02",
      "total_lab_patients": 4567,
      "disease_pct": 5.2
    }
  ]
}
```

**k-anonymity:** Districts with `total_screened < 5` (or `total_lab_patients < 5` for lab diseases) are excluded.

---

### Admin (session auth)

Admin routes are mounted at `/admin` and use cookie-based session authentication. They are exempt from API key checks and rate limiting.

**Login flow:**
1. `GET /admin/login` -- renders HTML login form
2. `POST /admin/login` -- validates password, sets `admin_session` cookie (24h expiry)
3. All subsequent `/admin/*` requests require the session cookie

**Security features:**
- CSRF protection on all POST requests (cookie + form token)
- Login brute-force protection: max 5 attempts per IP per 5-minute window
- Session tokens are 32-byte random hex, stored server-side
- Cookie attributes: `HttpOnly`, `SameSite=Strict`, `Secure` (when `SECURE_COOKIES=true`)

---

#### GET /admin/login

Render the login form. If already authenticated, redirects to `/admin/dashboard`.

#### POST /admin/login

Authenticate with password. Set via `ADMIN_PASSWORD` environment variable (default: `admin`).

| Form Field | Type | Description |
|------------|------|-------------|
| password | string | Admin password |
| csrf_token | string | CSRF token from cookie |

**Success:** Redirects to `/admin/dashboard` with `admin_session` cookie set.

**Errors:**
- `401` -- Invalid password
- `429` -- Too many login attempts (5 per 5 minutes per IP)
- `403` -- CSRF validation failed

---

#### GET /admin/logout

Clear session cookie and redirect to login.

---

#### GET /admin/dashboard

Main admin dashboard with table row counts and materialized view status. Renders HTML.

---

#### POST /admin/upload

Upload a CSV file for import preview. การอัปโหลดไฟล์ CSV

| Form Field | Type | Description |
|------------|------|-------------|
| file | file | CSV file (max 50 MB). Supports UTF-8, TIS-620, CP874 encodings |
| file_type | string | `auto` (default), `pt`, `pthistory`, `vitalsignslf`, `homevisit`, `homehealth`, `labhealth`, `labhealthext` |
| csrf_token | string | CSRF token |

**Auto-detection** identifies file type from column headers:

| File Type | Target Table | Detection Columns |
|-----------|-------------|-------------------|
| pt | raw_patients | `IDCARD` |
| pthistory | raw_visits | `RLGN`, `LGBTQ` |
| vitalsignslf | raw_vitalsigns | `HBPN`, `RISKDM` |
| homevisit | raw_homevisit | `SELFOUR`, `DISTYPE1` |
| homehealth | raw_homehealth | `EXCERCISE`, `CGTDS` |
| labhealth | raw_lab_results | `CBCRS`, `HMGB` |
| labhealthext | raw_lab_extended | `SCRRES01`, `PTGRIGHT` |

PII columns (IDCARD, FNAME, LNAME, PHONE, etc.) are **never** shown in the upload preview.

**Success:** Renders preview page with upload_id, column list, sample rows (max 10), and "Confirm Import" button.

---

#### POST /admin/import

Start a background ETL import for a previously uploaded CSV. เริ่มนำเข้าข้อมูล

| Form Field | Type | Description |
|------------|------|-------------|
| upload_id | string | ID from the upload preview step |
| csrf_token | string | CSRF token |

**Behavior:**
1. Creates an `import_history` record with `status = 'running'`
2. Launches a background thread to run ETL
3. Refreshes all materialized views after successful import
4. Redirects to `/admin/history` with flash message

Upload cache entries expire after 1 hour.

---

#### POST /admin/refresh

Manually refresh all materialized views. รีเฟรชข้อมูลสรุป

| Form Field | Type | Description |
|------------|------|-------------|
| csrf_token | string | CSRF token |

Redirects to `/admin/dashboard` with success/error flash message.

---

#### POST /admin/erasure

Process a PDPA data erasure request. ลบข้อมูลผู้ป่วยตาม พ.ร.บ.คุ้มครองข้อมูลส่วนบุคคล (PDPA มาตรา 33)

| Form Field | Type | Description |
|------------|------|-------------|
| idcard_hash | string | SHA-256 hash of the patient's ID card number |
| csrf_token | string | CSRF token |

**Behavior:**
1. Calls `execute_patient_erasure(idcard_hash)` database function
2. Deletes all patient data from: `raw_lab_extended`, `raw_lab_results`, `raw_homehealth`, `raw_homevisit`, `raw_vitalsigns`, `raw_visits`, `raw_patients`
3. Logs the erasure in `erasure_requests` table
4. Refreshes all materialized views
5. Redirects to `/admin/dashboard` with count of deleted records

**PDPA Compliance:**
- Implements Thailand PDPA Section 33 (right to erasure / สิทธิในการลบข้อมูล)
- All data for the patient is permanently deleted across all tables
- Erasure is logged for audit purposes
- Data retention policy: 7 years for health records per Thai MOH guidelines, 3 years for import history

---

#### GET /admin/history

Show recent import history (last 50 entries). Renders HTML table with:
- Filename, table name, file type
- Rows imported/skipped, status (success/error/running)
- Error message (if any), start time, completion time, duration

---

#### GET /admin/logs

Show recent import logs (last 100 entries). Renders formatted log lines.

---

#### GET /admin/api/table-counts

JSON endpoint for AJAX dashboard refresh. Returns raw table and materialized view row counts.

```json
{
  "raw_tables": [
    { "name": "raw_patients", "count": 156789 },
    { "name": "raw_vitalsigns", "count": 1245832 }
  ],
  "materialized_views": [
    { "name": "summary_district_disease", "count": 50 },
    { "name": "summary_district_lab", "count": 48 }
  ]
}
```

---

#### GET /admin/api/import-status/{history_id}

Poll the status of a running import job. Returns all fields from `import_history` as JSON.

---

### Frontend Integration: /districts-live

The BMA Health frontend consumes data via the `/api/health/districts-live` endpoint on the **bma-health backend** (not directly from the Summary API). This endpoint returns the full `HealthDataMap` -- the same format as `district_health_data.json` -- with live data sourced from the Summary API via the `data_adapter`.

```
Frontend  -->  bma-health backend (/api/health/districts-live)  -->  Summary API (/api/v2/summary/*)
```

The frontend uses `useHealthData()` (React Query) which fetches from `/api/health/districts-live` when `DATA_SOURCE=api`, or falls back to the static JSON file.

---

## Reference Tables

### Disease Keys

8 disease keys are recognized by the API. Each maps to specific database columns.

| Key | Thai Name | Risk Column | Found Column | Pct Column | Trend Support |
|-----|-----------|-------------|-------------|------------|---------------|
| `diabetes` | เบาหวาน | risk_dm | found_dm | pct_risk_dm | Yes |
| `hypertension` | ความดันโลหิตสูง | risk_hpt | found_hpt | pct_risk_hpt | Yes |
| `cardiovascular` | โรคหัวใจและหลอดเลือด | risk_cvd | found_cvd | pct_risk_cvd | Yes |
| `obesity` | โรคอ้วน | risk_bmi | found_obesity | -- | Yes |
| `dyslipidemia` | ไขมันในเลือดสูง | -- | found_dyslipidemia | -- | Yes |
| `stroke` | โรคหลอดเลือดสมอง | -- | found_stroke | -- | Yes |
| `ckd` | โรคไตเรื้อรัง | -- | -- | -- | No (use /summary/lab) |
| `anemia` | โรคโลหิตจาง | -- | -- | -- | No (use /summary/lab) |

---

### Zone Mapping

Bangkok is divided into 8 health zones (เขตสุขภาพ), each managed by a facilitator hospital.

| Zone Code | Thai Name | English Name | Facilitator Hospital | Districts |
|-----------|-----------|-------------|---------------------|-----------|
| 01 | เขตสุขภาพที่ 1 | Health Zone 1 | รพ.ราชพิพัฒน์ | ทวีวัฒนา, ตลิ่งชัน, บางแค, ภาษีเจริญ, หนองแขม, บางบอน |
| 02 | เขตสุขภาพที่ 2 | Health Zone 2 | รพ.ตากสิน | บางกอกน้อย, บางกอกใหญ่, คลองสาน, ธนบุรี, จอมทอง, บางขุนเทียน |
| 03 | เขตสุขภาพที่ 3 | Health Zone 3 | รพ.เจริญกรุงประชารักษ์ | ปทุมวัน, บางรัก, สาทร, บางคอแหลม, ยานนาวา, ราษฎร์บูรณะ, ทุ่งครุ, คลองเตย, วัฒนา, พระโขนง |
| 04 | เขตสุขภาพที่ 4 | Health Zone 4 | รพ.วชิรพยาบาล | บางซื่อ, ดุสิต, บางพลัด, พระนคร |
| 05 | เขตสุขภาพที่ 5 | Health Zone 5 | รพ.กลาง | พญาไท, ราชเทวี, ดินแดง, ห้วยขวาง, วังทองหลาง, สัมพันธวงศ์, ป้อมปราบศัตรูพ่าย |
| 06 | เขตสุขภาพที่ 6 | Health Zone 6 | รพ.กลาง | ดอนเมือง, สายไหม, หลักสี่, บางเขน, จตุจักร, ลาดพร้าว |
| 07 | เขตสุขภาพที่ 7 | Health Zone 7 | รพ.สิรินธร | บางกะปิ, สะพานสูง, สวนหลวง, ประเวศ, บางนา, ลาดกระบัง |
| 08 | เขตสุขภาพที่ 8 | Health Zone 8 | รพ.เวชการุณย์รัศมิ์ | คลองสามวา, หนองจอก, คันนายาว, บึงกุ่ม, มีนบุรี |

---

### District Codes

All 50 Bangkok districts with their codes and zone assignments.

| District Code | Zone | Thai Name | English Name |
|---------------|------|-----------|-------------|
| 1001 | 04 | พระนคร | Phra Nakhon |
| 1002 | 04 | ดุสิต | Dusit |
| 1003 | 08 | หนองจอก | Nong Chok |
| 1004 | 03 | บางรัก | Bang Rak |
| 1005 | 06 | บางเขน | Bang Khen |
| 1006 | 07 | บางกะปิ | Bang Kapi |
| 1007 | 03 | ปทุมวัน | Pathum Wan |
| 1008 | 05 | ป้อมปราบศัตรูพ่าย | Pom Prap Sattru Phai |
| 1009 | 03 | พระโขนง | Phra Khanong |
| 1010 | 08 | มีนบุรี | Min Buri |
| 1011 | 07 | ลาดกระบัง | Lat Krabang |
| 1012 | 03 | ยานนาวา | Yan Nawa |
| 1013 | 05 | สัมพันธวงศ์ | Samphanthawong |
| 1014 | 05 | พญาไท | Phaya Thai |
| 1015 | 02 | ธนบุรี | Thon Buri |
| 1016 | 02 | บางกอกใหญ่ | Bangkok Yai |
| 1017 | 05 | ห้วยขวาง | Huai Khwang |
| 1018 | 02 | คลองสาน | Khlong San |
| 1019 | 01 | ตลิ่งชัน | Taling Chan |
| 1020 | 02 | บางกอกน้อย | Bangkok Noi |
| 1021 | 02 | บางขุนเทียน | Bang Khun Thian |
| 1022 | 01 | ภาษีเจริญ | Phasi Charoen |
| 1023 | 01 | หนองแขม | Nong Khaem |
| 1024 | 03 | ราษฎร์บูรณะ | Rat Burana |
| 1025 | 04 | บางพลัด | Bang Phlat |
| 1026 | 05 | ดินแดง | Din Daeng |
| 1027 | 08 | บึงกุ่ม | Bueng Kum |
| 1028 | 03 | สาทร | Sathon |
| 1029 | 04 | บางซื่อ | Bang Sue |
| 1030 | 06 | จตุจักร | Chatuchak |
| 1031 | 02 | จอมทอง | Chom Thong |
| 1032 | 06 | ดอนเมือง | Don Mueang |
| 1033 | 05 | ราชเทวี | Ratchathewi |
| 1034 | 06 | ลาดพร้าว | Lat Phrao |
| 1035 | 03 | วัฒนา | Watthana |
| 1036 | 01 | บางแค | Bang Khae |
| 1037 | 06 | หลักสี่ | Lak Si |
| 1038 | 06 | สายไหม | Sai Mai |
| 1039 | 08 | คันนายาว | Khan Na Yao |
| 1040 | 07 | สะพานสูง | Saphan Sung |
| 1041 | 05 | วังทองหลาง | Wang Thonglang |
| 1042 | 08 | คลองสามวา | Khlong Sam Wa |
| 1043 | 07 | บางนา | Bang Na |
| 1044 | 01 | ทวีวัฒนา | Thawi Watthana |
| 1045 | 03 | ทุ่งครุ | Thung Khru |
| 1046 | 01 | บางบอน | Bang Bon |
| 1047 | 03 | คลองเตย | Khlong Toei |
| 1048 | 07 | ประเวศ | Prawet |
| 1049 | 07 | สวนหลวง | Suan Luang |
| 1050 | 03 | บางคอแหลม | Bang Kho Laem |

---

### Risk Factor Values

Values used in `summary_district_risk_factors` and the filtered query endpoint.

| Field | Values | Description |
|-------|--------|-------------|
| sex | `1` = male, `2` = female | เพศ |
| age_group | `15-19`, `20-29`, `30-39`, `40-49`, `50-59`, `60-69`, `70+` | กลุ่มอายุ |
| smoking | `0` = no, `1` = yes | สูบบุหรี่ |
| exercise | `0` = no, `1` = yes | ออกกำลังกาย |

---

## Frontend Integration

### TypeScript Fetch Example

```typescript
const API_BASE = "http://localhost:8000";
const API_KEY = "your-api-key";

interface OverviewResponse {
  total_screened: number;
  target: number;
  zones_count: number;
  districts_count: number;
  last_updated: string | null;
  by_zone: Array<{
    zone_code: string;
    name_th: string;
    total_screened: number;
  }>;
  by_disease: Array<{
    disease_key: string;
    total_at_risk: number;
    pct: number;
  }>;
}

async function fetchOverview(): Promise<OverviewResponse> {
  const res = await fetch(`${API_BASE}/api/v2/summary/overview`, {
    headers: { "X-API-Key": API_KEY },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  return res.json();
}

interface DistrictSummary {
  district_code: string;
  district_name: string;
  zone_code: string;
  total_screened: number;
  risk_dm_count: number;
  pct_risk_dm: number;
  risk_hpt_count: number;
  pct_risk_hpt: number;
  risk_cvd_count: number;
  pct_risk_cvd: number;
  found_obesity_count: number;
  found_dyslipidemia_count: number;
  found_stroke_count: number;
}

async function fetchDistricts(zoneCode?: string): Promise<DistrictSummary[]> {
  const params = zoneCode ? `?zone_code=${zoneCode}` : "";
  const res = await fetch(`${API_BASE}/api/v2/summary/districts${params}`, {
    headers: { "X-API-Key": API_KEY },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
```

### React Query Hook Example

```typescript
import { useQuery } from "@tanstack/react-query";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const API_KEY = process.env.NEXT_PUBLIC_API_KEY ?? "";

async function apiFetch<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "X-API-Key": API_KEY },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// Overview
export function useOverview() {
  return useQuery({
    queryKey: ["overview"],
    queryFn: () => apiFetch<OverviewResponse>("/api/v2/summary/overview"),
    staleTime: 5 * 60 * 1000, // 5 minutes
  });
}

// Districts (optionally filtered by zone)
export function useDistricts(zoneCode?: string) {
  return useQuery({
    queryKey: ["districts", zoneCode],
    queryFn: () => {
      const params = zoneCode ? `?zone_code=${zoneCode}` : "";
      return apiFetch<DistrictSummary[]>(`/api/v2/summary/districts${params}`);
    },
    staleTime: 5 * 60 * 1000,
  });
}

// Zone detail
export function useZoneDetail(zoneCode: string) {
  return useQuery({
    queryKey: ["zone", zoneCode],
    queryFn: () => apiFetch(`/api/v2/summary/zones/${zoneCode}`),
    enabled: !!zoneCode,
  });
}

// Disease trends
export function useDiseaseTrend(diseaseKey: string, district?: string) {
  return useQuery({
    queryKey: ["trend", diseaseKey, district],
    queryFn: () => {
      const params = new URLSearchParams({ granularity: "monthly" });
      if (district) params.set("district", district);
      return apiFetch(`/api/v2/trends/disease/${diseaseKey}?${params}`);
    },
    enabled: !!diseaseKey,
  });
}

// Search districts by disease
export function useDistrictSearch(disease: string, sortBy = "pct_desc", limit = 50) {
  return useQuery({
    queryKey: ["search", disease, sortBy, limit],
    queryFn: () => {
      const params = new URLSearchParams({
        disease,
        sort_by: sortBy,
        limit: String(limit),
      });
      return apiFetch(`/api/v2/search/districts?${params}`);
    },
    enabled: !!disease,
  });
}
```

### Environment Variables

Configure the API connection in your frontend `.env`:

```bash
# Summary API (bma-health-db)
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_API_KEY=your-api-key

# Or use the bma-health backend proxy
NEXT_PUBLIC_API_URL=http://localhost:8080
DATA_SOURCE=api
```

---

## Configuration Reference

All configuration is via environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://postgres:bma_health_dev@localhost:5433/bma_health` | PostgreSQL connection string |
| `API_KEY` | `changeme-dev-key` | API key for X-API-Key header (must change in production) |
| `ADMIN_PASSWORD` | `admin` | Admin panel password (must change in production) |
| `SECRET_KEY` | `change-me-in-production-use-random-secret` | Secret key (must change in production) |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:5173` | Comma-separated allowed origins |
| `RATE_LIMIT_PUBLIC` | `60` | Max requests per minute per IP |
| `RATE_LIMIT_ANALYST` | `300` | Max requests per minute (analyst tier, reserved) |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis URL (reserved for future use) |
| `ENVIRONMENT` | `development` | Set to `production` to enforce secure defaults and disable docs |
| `SECURE_COOKIES` | `false` | Set to `true` for HTTPS cookie flag on admin sessions |
| `CURRENT_YEAR` | Current year | Override year for ETL imports |

In production (`ENVIRONMENT=production`):
- Swagger UI (`/docs`), ReDoc (`/redoc`), and OpenAPI schema (`/openapi.json`) are disabled
- Default API_KEY, ADMIN_PASSWORD, and SECRET_KEY cause a fatal startup error
