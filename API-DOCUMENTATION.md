# BMA Health DB Summary API -- Frontend Integration Guide

**Version:** 2.0.0
**Base URL:** Configurable via environment. Default: `http://localhost:8002`
**Content-Type:** All responses are `application/json`
**Data Policy:** All endpoints return aggregate/summary data only. No individual patient records. No PII is ever exposed.

---

## Table of Contents

1. [Authentication](#authentication)
2. [Rate Limiting](#rate-limiting)
3. [k-Anonymity](#k-anonymity)
4. [Error Handling](#error-handling)
5. [Reference Values](#reference-values)
6. [Endpoints -- System](#system)
7. [Endpoints -- Summary](#summary)
8. [Endpoints -- Zones](#zones)
9. [Endpoints -- Districts](#districts)
10. [Endpoints -- Filtered](#filtered)
11. [Endpoints -- Lab](#lab)
12. [Endpoints -- Mental Health](#mental-health)
13. [Endpoints -- Demographics](#demographics)
14. [Endpoints -- Trends](#trends)
15. [Endpoints -- Search](#search)
16. [Endpoints -- Admin](#admin)

---

## Authentication

All API endpoints (except `/health`) require the `X-API-Key` header.

```
X-API-Key: <your-api-key>
```

Requests without a valid key receive a `401` response:

```json
{
  "detail": "Invalid or missing API key"
}
```

Admin endpoints (`/admin/*`) use a separate session-based authentication (cookie) and do not require the `X-API-Key` header.

---

## Rate Limiting

The API enforces per-IP rate limiting using a sliding window (default: 60 requests per minute). When exceeded, the API returns `429`:

```json
{
  "detail": "Rate limit exceeded. Try again later."
}
```

---

## k-Anonymity

All filtered and trend queries enforce k-anonymity with a threshold of **5**. Any group or time period with fewer than 5 individuals is suppressed (excluded from results entirely). This prevents re-identification of individuals through small-group analysis.

---

## Error Handling

All errors return JSON with a `detail` field:

| Status | Meaning |
|--------|---------|
| `400` | Invalid parameters (bad disease key, invalid granularity, etc.) |
| `401` | Missing or invalid API key |
| `404` | Resource not found (zone, district) |
| `429` | Rate limit exceeded |
| `500` | Internal server error |

Example error response:

```json
{
  "detail": "Invalid disease_key 'flu'. Valid keys: ['anemia', 'cardiovascular', 'ckd', 'diabetes', 'dyslipidemia', 'hypertension', 'obesity', 'stroke']"
}
```

---

## Reference Values

### Disease Keys

| Key | Description |
|-----|-------------|
| `diabetes` | Diabetes mellitus risk/found |
| `hypertension` | Hypertension risk/found |
| `cardiovascular` | Cardiovascular disease risk/found |
| `obesity` | Obesity (BMI-based) |
| `dyslipidemia` | Dyslipidemia (found) |
| `stroke` | Stroke (found) |
| `ckd` | Chronic kidney disease (lab-based) |
| `anemia` | Anemia (lab-based) |

### Zone Codes

`01` through `08` (Bangkok health zones, zero-padded strings).

### District Codes

Standard Bangkok district codes, typically `1001` through `1050` (string values).

### Sex Values

| Value | Meaning |
|-------|---------|
| `1` | Male |
| `2` | Female |

### Smoking / Exercise Values

Integer codes (typically `0` = no, `1` = yes).

### Age Group Values

String ranges such as `"15-29"`, `"30-44"`, `"45-59"`, `"60+"`.

---

## System

### GET /health

Health check endpoint. No authentication required.

**Request:**

```
GET /health
```

No headers required.

**Response (200):**

```json
{
  "status": "ok",
  "timestamp": "2026-04-08T03:45:12.123456"
}
```

---

## Summary

### GET /api/v2/summary/overview

City-wide screening overview with zone and disease breakdowns.

**Request:**

```
GET /api/v2/summary/overview
X-API-Key: <key>
```

No query parameters.

**Response (200):**

```json
{
  "total_screened": 1245832,
  "target": 1600000,
  "zones_count": 8,
  "districts_count": 50,
  "last_updated": "2026-04-07 18:30:00",
  "by_zone": [
    {
      "zone_code": "01",
      "name_th": "กลุ่มเขตกรุงเทพกลาง",
      "total_screened": 156420
    },
    {
      "zone_code": "02",
      "name_th": "กลุ่มเขตกรุงเทพใต้",
      "total_screened": 148350
    },
    {
      "zone_code": "03",
      "name_th": "กลุ่มเขตกรุงเทพเหนือ",
      "total_screened": 162510
    }
  ],
  "by_disease": [
    {
      "disease_key": "diabetes",
      "total_at_risk": 124583,
      "pct": 10.0
    },
    {
      "disease_key": "hypertension",
      "total_at_risk": 286941,
      "pct": 23.03
    },
    {
      "disease_key": "cardiovascular",
      "total_at_risk": 62291,
      "pct": 5.0
    },
    {
      "disease_key": "obesity",
      "total_at_risk": 373749,
      "pct": 29.99
    },
    {
      "disease_key": "dyslipidemia",
      "total_at_risk": 187456,
      "pct": 15.04
    },
    {
      "disease_key": "stroke",
      "total_at_risk": 12458,
      "pct": 1.0
    }
  ]
}
```

---

## Zones

### GET /api/v2/summary/zones

List all 8 health zones with screening totals and disease breakdown.

**Request:**

```
GET /api/v2/summary/zones
X-API-Key: <key>
```

No query parameters.

**Response (200):**

```json
[
  {
    "zone_code": "01",
    "name_th": "กลุ่มเขตกรุงเทพกลาง",
    "name_en": "Central Bangkok",
    "district_count": 7,
    "total_screened": 156420,
    "diseases": {
      "diabetes": { "count": 15642, "pct": 10.0 },
      "hypertension": { "count": 36028, "pct": 23.03 },
      "cardiovascular": { "count": 7821, "pct": 5.0 },
      "obesity": { "count": 46926, "pct": 30.0 },
      "dyslipidemia": { "count": 23463, "pct": 15.0 },
      "stroke": { "count": 1564, "pct": 1.0 }
    }
  },
  {
    "zone_code": "02",
    "name_th": "กลุ่มเขตกรุงเทพใต้",
    "name_en": "South Bangkok",
    "district_count": 6,
    "total_screened": 148350,
    "diseases": {
      "diabetes": { "count": 14835, "pct": 10.0 },
      "hypertension": { "count": 34120, "pct": 23.0 },
      "cardiovascular": { "count": 7418, "pct": 5.0 },
      "obesity": { "count": 44505, "pct": 30.0 },
      "dyslipidemia": { "count": 22253, "pct": 15.0 },
      "stroke": { "count": 1484, "pct": 1.0 }
    }
  }
]
```

### GET /api/v2/summary/zones/{zone_code}

Detail for a single zone including its districts and per-district disease data.

**Request:**

```
GET /api/v2/summary/zones/01
X-API-Key: <key>
```

| Path Parameter | Type | Required | Description |
|----------------|------|----------|-------------|
| `zone_code` | string | Yes | Zone code (`01`-`08`) |

**Response (200):**

```json
{
  "zone_code": "01",
  "name_th": "กลุ่มเขตกรุงเทพกลาง",
  "name_en": "Central Bangkok",
  "districts": [
    {
      "district_code": "1001",
      "district_name": "พระนคร",
      "total_screened": 22345,
      "risk_dm_count": 2234,
      "pct_risk_dm": 10.0,
      "risk_hpt_count": 5140,
      "pct_risk_hpt": 23.0,
      "risk_cvd_count": 1117,
      "pct_risk_cvd": 5.0,
      "risk_bmi_count": 6703,
      "found_obesity_count": 4469,
      "found_dyslipidemia_count": 3352,
      "found_stroke_count": 223
    },
    {
      "district_code": "1002",
      "district_name": "ดุสิต",
      "total_screened": 19876,
      "risk_dm_count": 1988,
      "pct_risk_dm": 10.0,
      "risk_hpt_count": 4571,
      "pct_risk_hpt": 23.0,
      "risk_cvd_count": 994,
      "pct_risk_cvd": 5.0,
      "risk_bmi_count": 5963,
      "found_obesity_count": 3975,
      "found_dyslipidemia_count": 2981,
      "found_stroke_count": 199
    }
  ]
}
```

**Error Responses:**

| Status | Condition |
|--------|-----------|
| `404` | `{"detail": "Zone not found"}` |

---

## Districts

### GET /api/v2/summary/districts

List all districts with disease data. Optionally filter by zone.

**Request:**

```
GET /api/v2/summary/districts?zone_code=01
X-API-Key: <key>
```

| Query Parameter | Type | Required | Description |
|-----------------|------|----------|-------------|
| `zone_code` | string | No | Filter to districts in this zone |

**Response (200):**

```json
[
  {
    "district_code": "1001",
    "district_name": "พระนคร",
    "zone_code": "01",
    "total_screened": 22345,
    "risk_dm_count": 2234,
    "pct_risk_dm": 10.0,
    "risk_hpt_count": 5140,
    "pct_risk_hpt": 23.0,
    "risk_cvd_count": 1117,
    "pct_risk_cvd": 5.0,
    "found_obesity_count": 4469,
    "found_dyslipidemia_count": 3352,
    "found_stroke_count": 223
  }
]
```

### GET /api/v2/summary/districts/{dcode}

Full detail for a single district: disease summary, lab results, mental health screening, and demographics.

**Request:**

```
GET /api/v2/summary/districts/1001
X-API-Key: <key>
```

| Path Parameter | Type | Required | Description |
|----------------|------|----------|-------------|
| `dcode` | string | Yes | District code (e.g. `1001`) |

**Response (200):**

```json
{
  "disease": {
    "district_code": "1001",
    "zone_code": "01",
    "district_name": "พระนคร",
    "total_screened": 22345,
    "risk_dm_count": 2234,
    "pct_risk_dm": 10.0,
    "risk_hpt_count": 5140,
    "pct_risk_hpt": 23.0,
    "risk_cvd_count": 1117,
    "pct_risk_cvd": 5.0,
    "risk_bmi_count": 6703,
    "found_dm_count": 1341,
    "pct_found_dm": 6.0,
    "found_hpt_count": 3575,
    "pct_found_hpt": 16.0,
    "found_cvd_count": 670,
    "pct_found_cvd": 3.0,
    "found_obesity_count": 4469,
    "found_dyslipidemia_count": 3352,
    "found_stroke_count": 223
  },
  "lab_summary": {
    "district_code": "1001",
    "total_lab_patients": 18500,
    "avg_hemoglobin": 13.24,
    "avg_hematocrit": 39.87,
    "avg_fbs": 102.45,
    "avg_cholesterol": 198.32,
    "avg_triglyceride": 145.67,
    "avg_hdl": 52.18,
    "avg_ldl": 121.45,
    "avg_creatinine": 1.02,
    "avg_egfr": 82.34,
    "avg_uric_acid": 5.89,
    "avg_sgot": 24.56,
    "avg_sgpt": 27.89,
    "pct_anemia": 8.45,
    "pct_ckd": 5.23
  },
  "mental_health": {
    "district_code": "1001",
    "total_screened": 20150,
    "pct_depression_risk": 12.34,
    "pct_phq9_moderate": 5.67,
    "pct_high_stress": 18.90
  },
  "demographics": {
    "district_code": "1001",
    "total_respondents": 21800,
    "edu_none": 432,
    "edu_primary": 5450,
    "edu_secondary": 4360,
    "edu_high_school": 3924,
    "edu_vocational": 2180,
    "edu_bachelor": 4360,
    "edu_postgrad": 1094,
    "occ_government": 3270,
    "occ_private": 6540,
    "occ_self_employed": 3924,
    "occ_agriculture": 218,
    "occ_unemployed": 2180,
    "occ_student": 1526,
    "occ_retired": 4142,
    "priv_ucs": 10900,
    "priv_sso": 5450,
    "priv_csmbs": 3270,
    "priv_other": 2180,
    "house_owned": 10900,
    "house_rented": 6540,
    "house_condo": 3270,
    "house_other": 1090
  }
}
```

Any section may be `null` if no data exists for that district.

**Error Responses:**

| Status | Condition |
|--------|-----------|
| `404` | `{"detail": "District not found"}` |

### GET /api/v2/summary/districts/{dcode}/disease/{disease_key}

Disease-specific detail for a district with risk factor breakdown by sex, age group, smoking, and exercise.

**Request:**

```
GET /api/v2/summary/districts/1001/disease/diabetes
X-API-Key: <key>
```

| Path Parameter | Type | Required | Description |
|----------------|------|----------|-------------|
| `dcode` | string | Yes | District code |
| `disease_key` | string | Yes | One of the valid disease keys |

**Response (200) -- standard diseases (diabetes, hypertension, cardiovascular, obesity, dyslipidemia, stroke):**

```json
{
  "district_code": "1001",
  "disease_key": "diabetes",
  "disease_summary": {
    "district_code": "1001",
    "zone_code": "01",
    "district_name": "พระนคร",
    "total_screened": 22345,
    "risk_dm_count": 2234,
    "pct_risk_dm": 10.0,
    "risk_hpt_count": 5140,
    "pct_risk_hpt": 23.0,
    "risk_cvd_count": 1117,
    "pct_risk_cvd": 5.0,
    "risk_bmi_count": 6703,
    "found_dm_count": 1341,
    "pct_found_dm": 6.0,
    "found_hpt_count": 3575,
    "pct_found_hpt": 16.0,
    "found_cvd_count": 670,
    "pct_found_cvd": 3.0,
    "found_obesity_count": 4469,
    "found_dyslipidemia_count": 3352,
    "found_stroke_count": 223
  },
  "risk_factor_breakdown": [
    {
      "sex": 1,
      "age_group": "30-44",
      "smoking": 1,
      "exercise": 0,
      "patient_count": 128,
      "avg_sbp": 132.5,
      "avg_dbp": 84.2,
      "avg_bmi": 27.3
    },
    {
      "sex": 2,
      "age_group": "45-59",
      "smoking": 0,
      "exercise": 1,
      "patient_count": 256,
      "avg_sbp": 128.1,
      "avg_dbp": 82.0,
      "avg_bmi": 25.8
    }
  ]
}
```

Note: `risk_factor_breakdown` rows with `patient_count < 5` are suppressed (k-anonymity).

**Response (200) -- lab-based diseases (ckd, anemia):**

```json
{
  "district_code": "1001",
  "disease_key": "ckd",
  "source": "lab",
  "lab_summary": {
    "district_code": "1001",
    "total_lab_patients": 18500,
    "avg_hemoglobin": 13.24,
    "avg_hematocrit": 39.87,
    "avg_fbs": 102.45,
    "avg_cholesterol": 198.32,
    "avg_triglyceride": 145.67,
    "avg_hdl": 52.18,
    "avg_ldl": 121.45,
    "avg_creatinine": 1.02,
    "avg_egfr": 82.34,
    "avg_uric_acid": 5.89,
    "avg_sgot": 24.56,
    "avg_sgpt": 27.89,
    "pct_anemia": 8.45,
    "pct_ckd": 5.23
  }
}
```

**Error Responses:**

| Status | Condition |
|--------|-----------|
| `400` | `{"detail": "Invalid disease_key 'flu'. Valid keys: [...]"}` |
| `404` | `{"detail": "District not found"}` |

---

## Filtered

### GET /api/v2/summary/filtered

Query risk factor summaries with arbitrary filters. k-anonymity is enforced: groups with fewer than 5 patients are excluded.

**Request:**

```
GET /api/v2/summary/filtered?district=1001&sex=1&age_group=45-59&smoking=0&exercise=1
X-API-Key: <key>
```

| Query Parameter | Type | Required | Description |
|-----------------|------|----------|-------------|
| `district` | string | No | District code |
| `sex` | integer | No | `1` = male, `2` = female |
| `age_group` | string | No | e.g. `"15-29"`, `"30-44"`, `"45-59"`, `"60+"` |
| `smoking` | integer | No | `0` = no, `1` = yes |
| `exercise` | integer | No | `0` = no, `1` = yes |

All parameters are optional. Omitting all returns the full (ungrouped) summary.

**Response (200):**

```json
{
  "filters_applied": {
    "district": "1001",
    "sex": 1,
    "age_group": "45-59",
    "smoking": 0,
    "exercise": 1
  },
  "k_anonymity_threshold": 5,
  "data": [
    {
      "district_code": "1001",
      "sex": 1,
      "age_group": "45-59",
      "smoking": 0,
      "exercise": 1,
      "patient_count": 87,
      "avg_sbp": 130.2,
      "avg_dbp": 83.5,
      "avg_weight_kg": 72.4,
      "avg_waist_cm": 88.6,
      "avg_bmi": 25.9
    }
  ]
}
```

If a combination yields fewer than 5 patients, that row is suppressed entirely. The `data` array may be empty.

---

## Lab

### GET /api/v2/summary/lab

Lab result averages and prevalence rates by district.

**Request:**

```
GET /api/v2/summary/lab?dcode=1001
X-API-Key: <key>
```

| Query Parameter | Type | Required | Description |
|-----------------|------|----------|-------------|
| `dcode` | string | No | Filter to a single district |
| `zone_code` | string | No | Filter to all districts in a zone |

Both parameters are optional. If both are provided, both filters apply. Omitting both returns all districts.

**Response (200):**

```json
[
  {
    "district_code": "1001",
    "total_lab_patients": 18500,
    "avg_hemoglobin": 13.24,
    "avg_fbs": 102.45,
    "avg_cholesterol": 198.32,
    "avg_triglyceride": 145.67,
    "avg_hdl": 52.18,
    "avg_ldl": 121.45,
    "avg_creatinine": 1.02,
    "avg_egfr": 82.34,
    "pct_anemia": 8.45,
    "pct_ckd": 5.23
  }
]
```

---

## Mental Health

### GET /api/v2/summary/mental-health

Mental health screening summary by district.

**Request:**

```
GET /api/v2/summary/mental-health?zone_code=01
X-API-Key: <key>
```

| Query Parameter | Type | Required | Description |
|-----------------|------|----------|-------------|
| `dcode` | string | No | Filter to a single district |
| `zone_code` | string | No | Filter to all districts in a zone |

**Response (200):**

```json
[
  {
    "district_code": "1001",
    "total_screened": 20150,
    "pct_depression_risk": 12.34,
    "pct_phq9_moderate": 5.67,
    "pct_high_stress": 18.90
  },
  {
    "district_code": "1002",
    "total_screened": 18430,
    "pct_depression_risk": 11.56,
    "pct_phq9_moderate": 4.89,
    "pct_high_stress": 17.23
  }
]
```

---

## Demographics

### GET /api/v2/summary/demographics

Demographic breakdown (education, occupation, health privilege, housing) by district.

**Request:**

```
GET /api/v2/summary/demographics?dcode=1001
X-API-Key: <key>
```

| Query Parameter | Type | Required | Description |
|-----------------|------|----------|-------------|
| `dcode` | string | No | Filter to a single district |
| `zone_code` | string | No | Filter to all districts in a zone |

**Response (200):**

```json
[
  {
    "district_code": "1001",
    "total_respondents": 21800,
    "edu_none": 432,
    "edu_primary": 5450,
    "edu_secondary": 4360,
    "edu_high_school": 3924,
    "edu_vocational": 2180,
    "edu_bachelor": 4360,
    "edu_postgrad": 1094,
    "occ_government": 3270,
    "occ_private": 6540,
    "occ_self_employed": 3924,
    "occ_agriculture": 218,
    "occ_unemployed": 2180,
    "occ_student": 1526,
    "occ_retired": 4142,
    "priv_ucs": 10900,
    "priv_sso": 5450,
    "priv_csmbs": 3270,
    "priv_other": 2180,
    "house_owned": 10900,
    "house_rented": 6540,
    "house_condo": 3270,
    "house_other": 1090
  }
]
```

**Field reference:**

| Prefix | Category | Fields |
|--------|----------|--------|
| `edu_` | Education level | `none`, `primary`, `secondary`, `high_school`, `vocational`, `bachelor`, `postgrad` |
| `occ_` | Occupation | `government`, `private`, `self_employed`, `agriculture`, `unemployed`, `student`, `retired` |
| `priv_` | Health privilege | `ucs` (Universal Coverage), `sso` (Social Security), `csmbs` (Civil Servant), `other` |
| `house_` | Housing type | `owned`, `rented`, `condo`, `other` |

---

## Trends

### GET /api/v2/trends/screening

Time series of screening counts.

**Request:**

```
GET /api/v2/trends/screening?granularity=monthly&zone_code=01
X-API-Key: <key>
```

| Query Parameter | Type | Required | Default | Description |
|-----------------|------|----------|---------|-------------|
| `granularity` | string | No | `monthly` | `monthly` or `quarterly` |
| `zone_code` | string | No | -- | Filter to a specific zone |

**Response (200):**

```json
{
  "granularity": "monthly",
  "zone_code": "01",
  "data": [
    {
      "period": "2025-10-01",
      "screened_count": 18432
    },
    {
      "period": "2025-11-01",
      "screened_count": 21567
    },
    {
      "period": "2025-12-01",
      "screened_count": 19823
    },
    {
      "period": "2026-01-01",
      "screened_count": 24105
    },
    {
      "period": "2026-02-01",
      "screened_count": 22891
    },
    {
      "period": "2026-03-01",
      "screened_count": 25340
    }
  ]
}
```

The `period` field is a date string representing the start of the time bucket (first day of the month or quarter). Periods with fewer than 5 screened individuals are suppressed.

**Error Responses:**

| Status | Condition |
|--------|-----------|
| `400` | `{"detail": "granularity must be one of ['monthly', 'quarterly']"}` |

### GET /api/v2/trends/disease/{disease_key}

Time series of disease prevalence rates.

**Request:**

```
GET /api/v2/trends/disease/hypertension?district=1001&granularity=monthly
X-API-Key: <key>
```

| Path Parameter | Type | Required | Description |
|----------------|------|----------|-------------|
| `disease_key` | string | Yes | Valid disease key (not `ckd` or `anemia`) |

| Query Parameter | Type | Required | Default | Description |
|-----------------|------|----------|---------|-------------|
| `district` | string | No | -- | Filter to a single district |
| `granularity` | string | No | `monthly` | `monthly` or `quarterly` |

**Response (200):**

```json
{
  "disease_key": "hypertension",
  "granularity": "monthly",
  "district": "1001",
  "data": [
    {
      "period": "2025-10-01",
      "total_screened": 18432,
      "at_risk_count": 4240,
      "pct": 23.01
    },
    {
      "period": "2025-11-01",
      "total_screened": 21567,
      "at_risk_count": 4961,
      "pct": 23.0
    },
    {
      "period": "2025-12-01",
      "total_screened": 19823,
      "at_risk_count": 4559,
      "pct": 23.0
    },
    {
      "period": "2026-01-01",
      "total_screened": 24105,
      "at_risk_count": 5544,
      "pct": 23.0
    }
  ]
}
```

Periods with `total_screened < 5` are suppressed.

**Error Responses:**

| Status | Condition |
|--------|-----------|
| `400` | Invalid `disease_key`, invalid `granularity`, or unsupported disease (`ckd`, `anemia`) |
| `400` | `{"detail": "Trend data not available for 'ckd'. Use /api/v2/summary/lab instead."}` |

---

## Search

### GET /api/v2/search/districts

Search and rank districts by disease prevalence. Useful for finding hotspots.

**Request:**

```
GET /api/v2/search/districts?disease=diabetes&min_pct=10&max_pct=30&sort_by=pct_desc&limit=10
X-API-Key: <key>
```

| Query Parameter | Type | Required | Default | Description |
|-----------------|------|----------|---------|-------------|
| `disease` | string | **Yes** | -- | Disease key to rank by |
| `min_pct` | float | No | -- | Minimum prevalence percentage (0-100) |
| `max_pct` | float | No | -- | Maximum prevalence percentage (0-100) |
| `sort_by` | string | No | `pct_desc` | Sort order: `pct_desc`, `pct_asc`, `count_desc`, `count_asc` |
| `limit` | integer | No | `50` | Max results (1-200) |

**Response (200) -- standard diseases:**

```json
{
  "disease": "diabetes",
  "results": [
    {
      "district_code": "1038",
      "district_name": "หนองจอก",
      "zone_code": "07",
      "total_screened": 28456,
      "disease_count": 5691,
      "disease_pct": 20.0
    },
    {
      "district_code": "1027",
      "district_name": "คลองสามวา",
      "zone_code": "06",
      "total_screened": 31200,
      "disease_count": 5928,
      "disease_pct": 19.0
    },
    {
      "district_code": "1015",
      "district_name": "บางกะปิ",
      "zone_code": "03",
      "total_screened": 24890,
      "disease_count": 4482,
      "disease_pct": 18.0
    }
  ]
}
```

**Response (200) -- lab-based diseases (ckd, anemia):**

```json
{
  "disease": "ckd",
  "results": [
    {
      "district_code": "1038",
      "district_name": "หนองจอก",
      "zone_code": "07",
      "total_lab_patients": 22100,
      "disease_pct": 8.45
    }
  ]
}
```

Note: Lab-based diseases do not include `disease_count` since prevalence is derived from lab analysis.

**Error Responses:**

| Status | Condition |
|--------|-----------|
| `400` | Invalid `disease_key` |

---

## Admin

Admin endpoints use session-based authentication (login form, not API key). These are intended for internal use by data administrators, not for frontend integration.

### GET /admin/

Redirects to the admin dashboard. Requires an active session cookie.

### GET /admin/login

Renders the login form (HTML page).

### POST /admin/login

Authenticates with a password and sets a session cookie.

**Request:**

```
POST /admin/login
Content-Type: application/x-www-form-urlencoded

password=<admin-password>
```

**Response:** Redirects to `/admin/dashboard` on success, or renders login page with error on failure.

### POST /admin/upload

Upload a CSV file for preview before import. Requires an active session cookie and CSRF token.

**Request:**

```
POST /admin/upload
Content-Type: multipart/form-data

file: <csv-file>
file_type: auto | pt | pthistory | vitalsignslf | homevisit | homehealth | labhealth | labhealthext
csrf_token: <csrf-token>
```

Supported file types are auto-detected from CSV column headers. Max file size: 50 MB.

**Response:** HTML page with upload preview (column list, sample rows, row count). PII columns are stripped from the preview.

### POST /admin/import

Trigger ETL import for a previously uploaded CSV. Runs in a background thread.

**Request:**

```
POST /admin/import
Content-Type: application/x-www-form-urlencoded

upload_id=<upload-id-from-preview>
csrf_token=<csrf-token>
```

**Response:** Redirects to `/admin/history` with a flash message indicating the import has started.

### POST /admin/refresh

Manually refresh all materialized views (summary tables).

**Request:**

```
POST /admin/refresh
Content-Type: application/x-www-form-urlencoded

csrf_token=<csrf-token>
```

**Response:** Redirects to `/admin/dashboard` with a success or error flash message.

### GET /admin/history

HTML page showing the 50 most recent import jobs with status, row counts, and timing.

### GET /admin/logs

HTML page showing formatted import log lines.

### GET /admin/api/table-counts

JSON endpoint for AJAX dashboard refresh. Requires session cookie.

**Response (200):**

```json
{
  "raw_tables": [
    { "name": "raw_patients", "count": 425000 },
    { "name": "raw_vitalsigns", "count": 1245832 },
    { "name": "raw_lab_results", "count": 890000 },
    { "name": "raw_visits", "count": 1100000 }
  ],
  "materialized_views": [
    { "name": "summary_district_disease", "count": 50 },
    { "name": "summary_district_lab", "count": 50 },
    { "name": "summary_district_mental", "count": 50 },
    { "name": "summary_district_demographics", "count": 50 },
    { "name": "summary_district_risk_factors", "count": 2400 }
  ]
}
```

### GET /admin/api/import-status/{history_id}

Poll the status of a running import job. Requires session cookie.

**Response (200):**

```json
{
  "id": "42",
  "filename": "vitalsignslf.csv",
  "table_name": "raw_vitalsigns",
  "file_type": "vitalsignslf",
  "status": "success",
  "rows_imported": "125000",
  "rows_skipped": "0",
  "error_message": null,
  "started_at": "2026-04-08 10:30:00",
  "completed_at": "2026-04-08 10:32:15",
  "duration_seconds": "135.42",
  "uploaded_by": null
}
```

Note: All values are returned as strings (or null).

| Status | Condition |
|--------|-----------|
| `404` | `{"detail": "Import job not found"}` |

---

## Frontend Integration Quick Start

### 1. Set up your API client

```javascript
const API_BASE = process.env.REACT_APP_API_URL || "http://localhost:8002";
const API_KEY = process.env.REACT_APP_API_KEY;

async function apiFetch(path, params = {}) {
  const url = new URL(path, API_BASE);
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null) url.searchParams.set(k, v);
  });

  const res = await fetch(url, {
    headers: { "X-API-Key": API_KEY },
  });

  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}
```

### 2. Example calls

```javascript
// City overview
const overview = await apiFetch("/api/v2/summary/overview");

// All zones
const zones = await apiFetch("/api/v2/summary/zones");

// Districts in zone 03
const districts = await apiFetch("/api/v2/summary/districts", { zone_code: "03" });

// Full district detail
const detail = await apiFetch("/api/v2/summary/districts/1015");

// Screening trends (quarterly, zone 01)
const trends = await apiFetch("/api/v2/trends/screening", {
  granularity: "quarterly",
  zone_code: "01",
});

// Disease hotspot search
const hotspots = await apiFetch("/api/v2/search/districts", {
  disease: "hypertension",
  min_pct: 25,
  sort_by: "pct_desc",
  limit: 10,
});

// Filtered risk factors
const filtered = await apiFetch("/api/v2/summary/filtered", {
  district: "1001",
  sex: 2,
  age_group: "45-59",
});
```

### 3. Handle suppressed data

Due to k-anonymity enforcement, some queries may return empty `data` arrays or fewer rows than expected. Your UI should display an appropriate message such as "Data suppressed for privacy (fewer than 5 individuals in this group)."
