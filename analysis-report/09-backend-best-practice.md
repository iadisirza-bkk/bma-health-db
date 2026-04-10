# Backend Architecture Best Practice
## BMA Health Screening Platform — Redesign Proposal

> **สถานะปัจจุบัน:** FastAPI + PostgreSQL 16 + Redis 7, `main.py` 3,083 บรรทัด, 72 endpoints, 10 MCP tools, 15 admin routes — ทำงานได้แต่ **ไม่ scale**
>
> **ปัญหาหลัก:** God file, no separation of concerns, raw SQL in handlers, no schema validation, no external API layer, no caching strategy
>
> **เป้าหมาย:** ระบบระดับ production-grade ที่ WHO / กระทรวงสาธารณสุข ยอมรับ รองรับ 50 เขต, 14,000+ สถานบริการ, LLM agents, 28 GIS endpoints

---

## 1. Current State Assessment — สิ่งที่มีอยู่

```
สิ่งที่ดีอยู่แล้ว (Keep)              สิ่งที่ต้องแก้ (Fix)
─────────────────────────              ─────────────────────
 FastAPI (async, modern)               main.py 3,083 lines (God file)
 PostgreSQL materialized views         Raw SQL ฝังใน route handlers
 k-anonymity enforcement               ไม่มี Pydantic response models
 PDPA compliance (erasure)             ไม่มี service layer
 HMAC-SHA256 ID hashing                ไม่มี external API integration
 Connection pooling                     ไม่มี caching strategy (Redis ว่าง)
 Audit logging                         ไม่มี error handling pattern
 MCP server for LLM                    MCP duplicates SQL logic
 Docker Compose                        ไม่มี test suite
 API versioning (v2)                   No OpenAPI schema validation
```

---

## 2. Target Architecture — Clean Architecture + Domain-Driven Design

```
┌─────────────────────────────────────────────────────────────────────┐
│                         CLIENTS                                      │
│  Web Dashboard    Mobile App    LLM Agents (MCP)    Admin Panel      │
└────────┬───────────┬──────────────┬──────────────────┬───────────────┘
         │           │              │                  │
         ▼           ▼              ▼                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      API GATEWAY LAYER                               │
│  CORS · Rate Limit · API Key Auth · Request ID · Audit Log          │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                    FastAPI Application                        │   │
│  │  /api/v2/... (public)  /admin/... (session)  /mcp (tools)   │   │
│  └──────────────────────────────────────────────────────────────┘   │
└────────┬───────────────────────────────────────────┬────────────────┘
         │                                           │
         ▼                                           ▼
┌────────────────────────┐              ┌──────────────────────────┐
│   ROUTER LAYER         │              │   SCHEMA LAYER           │
│   (Thin controllers)   │              │   (Pydantic v2 models)   │
│                        │              │                          │
│   routers/             │              │   schemas/               │
│     summary.py         │              │     common.py            │
│     districts.py       │              │     disease.py           │
│     epidemiology.py    │              │     lab.py               │
│     facility.py        │              │     demographics.py      │
│     strategy.py        │              │     gis.py               │
│     public.py          │              │     mcp.py               │
│     research.py        │              │     ...                  │
│     monitoring.py      │              │                          │
└────────┬───────────────┘              └──────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       SERVICE LAYER                                  │
│   (Business logic, orchestration, data transformation)               │
│                                                                      │
│   services/                                                          │
│     disease_service.py      ← NCD prevalence, comorbidity matrix     │
│     lab_service.py          ← Lab x Disease cross-analysis           │
│     screening_service.py    ← Coverage, trends, longitudinal         │
│     demographics_service.py ← Age pyramid, education, occupation     │
│     mental_health_service.py← PHQ-9, ST-5 analysis                   │
│     gis_service.py          ← ArcGIS integration + clinic_latlong    │
│     facility_service.py     ← Performance, capacity planning         │
│     strategy_service.py     ← Budget, ROI, resource optimization     │
│     analytics_service.py    ← Correlation, statistical tests         │
│     data_quality_service.py ← Cleaning rules, validation, outliers   │
│     export_service.py       ← CSV/Excel export with PII stripping    │
│     cache_service.py        ← Redis caching strategy                 │
└────────┬────────────────────────────────┬───────────────────────────┘
         │                                │
         ▼                                ▼
┌─────────────────────────┐  ┌──────────────────────────────────────┐
│   REPOSITORY LAYER      │  │   EXTERNAL API LAYER                 │
│   (Data access only)    │  │   (Third-party integrations)         │
│                         │  │                                      │
│   repositories/         │  │   external/                          │
│     disease_repo.py     │  │     arcgis_client.py  ← 28 endpoints│
│     lab_repo.py         │  │     pm25_client.py    ← Air quality  │
│     patient_repo.py     │  │     pollution_client.py← Dust sources│
│     screening_repo.py   │  │     facility_client.py← GIS clinics │
│     facility_repo.py    │  │                                      │
│     base_repo.py        │  │   Retry · Circuit Breaker · Cache    │
│                         │  │   Timeout · Error Mapping             │
│   SQL in .sql files     │  │                                      │
│   or query builder      │  │                                      │
└────────┬────────────────┘  └──────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     DATA LAYER                                       │
│                                                                      │
│   PostgreSQL 16              Redis 7                                 │
│   ┌──────────────────┐      ┌────────────────────┐                  │
│   │  raw_patients     │      │  cache:district:*   │                  │
│   │  raw_vitalsigns   │      │  cache:zone:*       │                  │
│   │  raw_lab_results  │      │  cache:facility:*   │                  │
│   │  raw_homehealth   │      │  cache:gis:*        │                  │
│   │  raw_homevisit    │      │  rate_limit:*       │                  │
│   │  raw_lab_extended │      │  session:admin:*    │                  │
│   │  ───────────────  │      └────────────────────┘                  │
│   │  MV: summary_*    │                                              │
│   │  (materialized)   │                                              │
│   └──────────────────┘                                               │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Folder Structure — เปรียบเทียบ Before / After

### Before (ปัจจุบัน)
```
api/
  main.py              ← 3,083 lines, 72 endpoints ปนกัน
  admin.py             ← 900+ lines
  database.py          ← 91 lines (ดีแล้ว แต่ใช้ไม่เต็มที่)
  security.py          ← 141 lines
  config.py            ← 47 lines
```

### After (เป้าหมาย)
```
api/
├── main.py                     ← ~50 lines: สร้าง app + mount routers
├── config.py                   ← Environment config (keep)
├── dependencies.py             ← FastAPI Depends: DB session, auth, rate limit
│
├── middleware/
│   ├── audit.py                ← Access logging
│   ├── rate_limit.py           ← Sliding window (Redis-backed)
│   ├── error_handler.py        ← Global exception → JSON response
│   └── request_id.py           ← Inject X-Request-ID for tracing
│
├── routers/                    ← Thin controllers (~50–80 lines each)
│   ├── summary.py              ← 5 endpoints: overview, filtered, lab, mental, demo
│   ├── districts.py            ← 3 endpoints: list, detail, disease
│   ├── zones.py                ← 2 endpoints: list, detail
│   ├── trends.py               ← 2 endpoints: screening, disease
│   ├── epidemiology.py         ← 6 endpoints: age pyramid, crosstab, etc.
│   ├── facility.py             ← 5 endpoints: performance, workload, etc.
│   ├── monitoring.py           ← 3 endpoints: data quality, cleansing
│   ├── health_promotion.py     ← 4 endpoints: BMI, behavior-disease
│   ├── strategy.py             ← 6 endpoints: cost, budget, ROI
│   ├── research.py             ← 7 endpoints: dictionary, stats, export
│   ├── public.py               ← 5 endpoints: district summary, locations
│   ├── kpi.py                  ← 2 endpoints: MOH targets, yield
│   ├── disease_control.py      ← 3 endpoints: NCD cascade, progression
│   ├── gis.py                  ← NEW: GIS proxy + overlay endpoints
│   └── admin.py                ← 15 admin routes (move from admin.py)
│
├── schemas/                    ← Pydantic v2 models
│   ├── common.py               ← Pagination, ErrorResponse, HealthResponse
│   ├── filters.py              ← QueryFilters (district, zone, sex, age_group)
│   ├── disease.py              ← DiseasePrevalence, ComorbidityMatrix
│   ├── lab.py                  ← LabCrosstab, LabTrend
│   ├── demographics.py         ← AgePyramid, PopulationSummary
│   ├── facility.py             ← FacilityPerformance, CapacityPlan
│   ├── gis.py                  ← GeoPoint, FacilityLocation, PMReading
│   ├── screening.py            ← ScreeningCoverage, LongitudinalTrend
│   └── strategy.py             ← BudgetAllocation, ROIProjection
│
├── services/                   ← Business logic (~100–200 lines each)
│   ├── disease_service.py
│   ├── lab_service.py
│   ├── screening_service.py
│   ├── demographics_service.py
│   ├── mental_health_service.py
│   ├── gis_service.py          ← NEW: ArcGIS + clinic_latlong
│   ├── facility_service.py
│   ├── strategy_service.py
│   ├── analytics_service.py
│   ├── data_quality_service.py
│   ├── export_service.py
│   └── cache_service.py        ← NEW: Redis caching
│
├── repositories/               ← SQL queries only
│   ├── base.py                 ← execute_query, execute_scalar (from database.py)
│   ├── disease_repo.py
│   ├── lab_repo.py
│   ├── patient_repo.py
│   ├── screening_repo.py
│   └── facility_repo.py
│
├── external/                   ← Third-party API clients
│   ├── base_client.py          ← httpx AsyncClient + retry + circuit breaker
│   ├── arcgis_client.py        ← 28 GIS endpoints
│   ├── pm25_client.py          ← PM2.5 data
│   └── pollution_client.py     ← Dust sources
│
├── privacy/                    ← PII protection (from security.py)
│   ├── k_anonymity.py          ← enforce_k_anonymity()
│   ├── pii_filter.py           ← Column blocking
│   └── pdpa.py                 ← Erasure requests
│
├── sql/                        ← Externalized SQL (ย้ายจาก main.py)
│   ├── disease/
│   │   ├── prevalence_by_district.sql
│   │   ├── comorbidity_matrix.sql
│   │   └── disease_lab_cross.sql
│   ├── screening/
│   │   ├── coverage_by_zone.sql
│   │   ├── longitudinal_trend.sql
│   │   └── ncd_cascade.sql
│   ├── facility/
│   │   ├── performance.sql
│   │   └── capacity_planning.sql
│   └── ...
│
└── tests/                      ← Test suite
    ├── conftest.py             ← Fixtures: test DB, test client
    ├── test_disease_service.py
    ├── test_lab_service.py
    ├── test_gis_service.py
    ├── test_privacy.py
    └── test_api_endpoints.py
```

---

## 4. Layer-by-Layer Design Principles

### 4.1 Router Layer — Thin Controllers

**กฎ: Router ห้ามมี business logic, ห้ามมี SQL, ห้ามมี data transformation**

```python
# routers/epidemiology.py — ตัวอย่าง

from fastapi import APIRouter, Depends, Query
from schemas.disease import ComorbidityMatrixResponse
from schemas.filters import DiseaseFilters
from services.disease_service import DiseaseService
from dependencies import get_disease_service

router = APIRouter(prefix="/api/v2/epidemiology", tags=["Epidemiology"])


@router.get(
    "/comorbidity-matrix",
    response_model=ComorbidityMatrixResponse,
    summary="NCD comorbidity co-occurrence matrix",
)
async def comorbidity_matrix(
    filters: DiseaseFilters = Depends(),
    svc: DiseaseService = Depends(get_disease_service),
):
    return await svc.get_comorbidity_matrix(filters)
```

**ก่อน (main.py บรรทัด ~1400):**
```python
# ❌ 80+ lines of raw SQL + data transform ฝังใน route handler
@app.get("/api/v2/epidemiology/multi-disease-matrix")
async def multi_disease_matrix(district: str = None, zone_code: str = None, ...):
    where_clauses = []
    params = []
    if district:
        where_clauses.append("dcode = %s")
        params.append(district)
    # ... 60 lines of SQL building ...
    sql = f"""
        SELECT ... FROM summary_district_disease
        WHERE {' AND '.join(where_clauses) if where_clauses else '1=1'}
    """
    rows = execute_query(sql, params)
    # ... 20 lines of data transformation ...
    return {"data": result}
```

### 4.2 Schema Layer — Pydantic v2 Contracts

**กฎ: ทุก request/response ต้องมี type-safe schema**

```python
# schemas/disease.py

from pydantic import BaseModel, Field
from typing import Optional


class DiseasePrevalence(BaseModel):
    district_code: str
    district_name: str
    total_screened: int = Field(ge=0)
    dm_count: int = Field(ge=0)
    dm_rate: float = Field(ge=0, le=100, description="Prevalence %")
    hpt_count: int = Field(ge=0)
    hpt_rate: float = Field(ge=0, le=100)
    # ... other diseases


class ComorbidityPair(BaseModel):
    disease_a: str
    disease_b: str
    co_occurrence_count: int = Field(ge=5, description="k-anonymity enforced")
    co_occurrence_rate: float


class ComorbidityMatrixResponse(BaseModel):
    matrix: list[ComorbidityPair]
    total_patients: int
    generated_at: str
```

**ประโยชน์:**
- OpenAPI docs สร้างอัตโนมัติจาก schema
- Request validation อัตโนมัติ (ไม่ต้อง manual validate)
- Response serialization ปลอดภัย (ไม่มี PII หลุด)
- MCP tools ใช้ schema เดียวกัน (DRY)

### 4.3 Service Layer — Business Logic

**กฎ: Service เป็นที่เดียวที่มี business logic ทั้ง router, MCP, admin เรียก service เดียวกัน**

```python
# services/disease_service.py

from repositories.disease_repo import DiseaseRepo
from services.cache_service import CacheService
from privacy.k_anonymity import enforce_k_anonymity
from schemas.filters import DiseaseFilters


class DiseaseService:
    def __init__(self, repo: DiseaseRepo, cache: CacheService):
        self.repo = repo
        self.cache = cache

    async def get_comorbidity_matrix(
        self, filters: DiseaseFilters
    ) -> dict:
        cache_key = f"comorbidity:{filters.cache_key()}"
        cached = await self.cache.get(cache_key)
        if cached:
            return cached

        raw = await self.repo.query_comorbidity(filters)
        result = enforce_k_anonymity(raw, threshold=5)
        matrix = self._build_matrix(result)

        await self.cache.set(cache_key, matrix, ttl=3600)
        return matrix

    def _build_matrix(self, rows: list[dict]) -> dict:
        """Pure business logic — no SQL, no HTTP, no side effects."""
        # ... transform rows into comorbidity pairs ...
```

**แก้ปัญหา MCP duplication:** MCP server เรียก service เดียวกัน

```python
# mcp_server/server.py — After

from services.disease_service import DiseaseService

@mcp.tool()
async def compare_disease(disease_a: str, disease_b: str, ...):
    svc = get_disease_service()  # same service, same logic
    return await svc.get_comorbidity_matrix(filters)
```

### 4.4 Repository Layer — SQL Isolation

**กฎ: SQL อยู่ใน repository เท่านั้น ใช้ .sql file หรือ query builder**

```python
# repositories/disease_repo.py

from repositories.base import BaseRepo
from schemas.filters import DiseaseFilters


class DiseaseRepo(BaseRepo):
    async def query_comorbidity(self, f: DiseaseFilters) -> list[dict]:
        sql = self.load_sql("disease/comorbidity_matrix.sql")
        params = f.to_sql_params()
        return await self.execute(sql, params)

    async def query_prevalence_by_district(self, f: DiseaseFilters):
        sql = self.load_sql("disease/prevalence_by_district.sql")
        return await self.execute(sql, f.to_sql_params())
```

```sql
-- sql/disease/comorbidity_matrix.sql

WITH patient_diseases AS (
    SELECT
        pid,
        bool_or(dm_flag)     AS has_dm,
        bool_or(hpt_flag)    AS has_hpt,
        bool_or(stroke_flag) AS has_stroke,
        bool_or(hrt_flag)    AS has_hrt,
        bool_or(kidney_flag) AS has_kidney,
        bool_or(chltr_flag)  AS has_chltr
    FROM summary_district_disease
    WHERE 1=1
        AND (:district IS NULL OR dcode = :district)
        AND (:zone_code IS NULL OR zone_code = :zone_code)
    GROUP BY pid
)
SELECT
    d1.disease AS disease_a,
    d2.disease AS disease_b,
    COUNT(*)   AS co_occurrence_count
FROM patient_diseases pd
CROSS JOIN LATERAL unnest(ARRAY[
    CASE WHEN has_dm     THEN 'DM' END,
    CASE WHEN has_hpt    THEN 'HPT' END,
    CASE WHEN has_stroke THEN 'STROKE' END,
    CASE WHEN has_hrt    THEN 'HRT' END,
    CASE WHEN has_kidney THEN 'KIDNEY' END,
    CASE WHEN has_chltr  THEN 'CHLTR' END
]) AS d1(disease)
CROSS JOIN LATERAL unnest(ARRAY[...]) AS d2(disease)
WHERE d1.disease < d2.disease  -- upper triangle only
GROUP BY d1.disease, d2.disease
HAVING COUNT(*) >= 5  -- k-anonymity
ORDER BY co_occurrence_count DESC;
```

### 4.5 External API Layer — GIS Integration

**กฎ: External API client ต้องมี retry, circuit breaker, timeout, cache**

```python
# external/arcgis_client.py

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


class ArcGISClient:
    """Client for 28 Bangkok GIS endpoints (เอกสารแนบ ๒)."""

    SERVERS = {
        "cpudgiapp": "https://cpudgiapp.bangkok.go.th/arcgis/rest/services",
        "bmagis":    "https://bmagis.bangkok.go.th/arcgis/rest/services",
        "bmamap":    "https://bmamap.bangkok.go.th/bmamap/rest/services",
    }

    ENDPOINTS = {
        "districts":        ("cpudgiapp", "Basemap_Service/CPUD_Basemap1000/MapServer/12"),
        "hospitals":        ("bmagis",    "จุดสนับสนุนสถานที่/MapServer/16"),
        "pm25":             ("bmagis",    "Hosted/air_quality_data_processed/FeatureServer/0"),
        "cement_plants":    ("bmamap",    "HEALTHMAP/dust_pollution/MapServer/0"),
        "construction":     ("bmamap",    "HEALTHMAP/dust_pollution/MapServer/1"),
        "factories":        ("bmamap",    "HEALTHMAP/dust_pollution/MapServer/2"),
        "communities":      ("cpudgiapp", "Community/Service_Community/FeatureServer/14"),
        # ... 21 more endpoints
    }

    def __init__(self, cache: CacheService):
        self.client = httpx.AsyncClient(timeout=30.0)
        self.cache = cache

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def query_layer(
        self,
        layer_key: str,
        where: str = "1=1",
        out_fields: str = "*",
        out_sr: int = 4326,
        limit: int = 1000,
    ) -> dict:
        cache_key = f"gis:{layer_key}:{where}:{limit}"
        cached = await self.cache.get(cache_key)
        if cached:
            return cached

        server_key, path = self.ENDPOINTS[layer_key]
        base = self.SERVERS[server_key]
        url = f"{base}/{path}/query"

        params = {
            "where": where,
            "outFields": out_fields,
            "returnGeometry": "true",
            "outSR": out_sr,
            "f": "json",
            "resultRecordCount": limit,
        }

        resp = await self.client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        # Cache GIS data for 1 hour (PM2.5) or 24 hours (static layers)
        ttl = 3600 if layer_key == "pm25" else 86400
        await self.cache.set(cache_key, data, ttl=ttl)
        return data
```

### 4.6 Caching Strategy — Redis

**ปัจจุบัน Redis ว่างเปล่า ทั้งที่มีอยู่แล้วใน docker-compose**

```
Cache Tiers:
─────────────────────────────────────────────────────
Tier 1: Hot data (TTL 5 min)
  - /api/v2/summary/overview        ← ถูกเรียกบ่อยสุด
  - /api/v2/summary/zones           ← dashboard default view

Tier 2: Warm data (TTL 1 hour)
  - /api/v2/districts/*             ← เปลี่ยนแค่ตอน refresh views
  - /api/v2/epidemiology/*          ← heavy queries
  - /api/v2/facility/*              ← aggregated

Tier 3: Slow-changing (TTL 24 hours)
  - GIS static layers (districts, roads, communities)
  - clinic_latlong data
  - Reference tables

Tier 4: Real-time (TTL 1 hour)
  - PM2.5 data                      ← เปลี่ยนบ่อย
  - Active construction sites

Cache Invalidation:
  - POST /admin/refresh → ลบ cache ทั้งหมด (FLUSHDB)
  - POST /admin/import → ลบ cache ทั้งหมด
  - Materialized view refresh → ลบ Tier 1-2
```

```python
# services/cache_service.py

import json, redis.asyncio as redis

class CacheService:
    def __init__(self, redis_url: str):
        self.redis = redis.from_url(redis_url)

    async def get(self, key: str):
        val = await self.redis.get(key)
        return json.loads(val) if val else None

    async def set(self, key: str, value, ttl: int = 300):
        await self.redis.setex(key, ttl, json.dumps(value, default=str))

    async def invalidate_pattern(self, pattern: str):
        keys = []
        async for key in self.redis.scan_iter(pattern):
            keys.append(key)
        if keys:
            await self.redis.delete(*keys)

    async def flush_all(self):
        await self.redis.flushdb()
```

---

## 5. Error Handling Pattern

**ปัจจุบัน:** ไม่มี — error จะ crash หรือ return 500 ไม่มี context

```python
# middleware/error_handler.py

from fastapi import Request
from fastapi.responses import JSONResponse
import traceback, logging

logger = logging.getLogger("bma-health")


class AppException(Exception):
    """Base exception for all business errors."""
    def __init__(self, message: str, code: str, status: int = 400):
        self.message = message
        self.code = code
        self.status = status


class NotFoundError(AppException):
    def __init__(self, entity: str, identifier: str):
        super().__init__(
            message=f"{entity} '{identifier}' not found",
            code="NOT_FOUND",
            status=404,
        )


class PrivacyViolationError(AppException):
    def __init__(self):
        super().__init__(
            message="Result suppressed: group size below k-anonymity threshold",
            code="K_ANONYMITY_SUPPRESSED",
            status=200,  # ไม่ใช่ error จริง แค่ suppress
        )


class ExternalAPIError(AppException):
    def __init__(self, service: str, detail: str):
        super().__init__(
            message=f"External service '{service}' error: {detail}",
            code="EXTERNAL_API_ERROR",
            status=502,
        )


async def global_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, AppException):
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": exc.code,
                "message": exc.message,
                "request_id": request.state.request_id,
            },
        )

    # Unexpected errors — log full traceback, return safe message
    logger.error(f"Unhandled: {traceback.format_exc()}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "INTERNAL_ERROR",
            "message": "An unexpected error occurred",
            "request_id": request.state.request_id,
        },
    )
```

---

## 6. Dependency Injection Pattern

```python
# dependencies.py

from functools import lru_cache
from fastapi import Depends
from api.config import Settings
from repositories.disease_repo import DiseaseRepo
from repositories.lab_repo import LabRepo
from services.disease_service import DiseaseService
from services.lab_service import LabService
from services.cache_service import CacheService
from external.arcgis_client import ArcGISClient


@lru_cache()
def get_settings() -> Settings:
    return Settings()


async def get_cache(settings: Settings = Depends(get_settings)) -> CacheService:
    return CacheService(settings.redis_url)


async def get_disease_service(
    cache: CacheService = Depends(get_cache),
) -> DiseaseService:
    repo = DiseaseRepo()
    return DiseaseService(repo, cache)


async def get_gis_client(
    cache: CacheService = Depends(get_cache),
) -> ArcGISClient:
    return ArcGISClient(cache)
```

---

## 7. MCP Server Refactor — Share Service Layer

**ปัจจุบัน:** MCP server มี SQL queries ซ้ำกับ main.py
**เป้าหมาย:** MCP server เรียก service layer เดียวกัน

```python
# mcp_server/server.py — After refactor

from mcp.server.fastmcp import FastMCP
from services.disease_service import DiseaseService
from services.lab_service import LabService
from services.screening_service import ScreeningService
from schemas.filters import DiseaseFilters

mcp = FastMCP("BMA Health MCP", stateless=True)


@mcp.tool()
async def get_zone_summary(zone_code: str) -> dict:
    """Zone-level epidemiology summary."""
    svc = DiseaseService(...)  # injected
    filters = DiseaseFilters(zone_code=zone_code)
    return await svc.get_zone_summary(filters)


@mcp.tool()
async def get_disease_lab_cross(
    disease_key: str, district: str = None
) -> dict:
    """Lab values stratified by disease flag — Disease x Lab Result."""
    svc = LabService(...)
    return await svc.get_disease_lab_cross(disease_key, district)
```

**ประโยชน์:**
- SQL อยู่ที่เดียว (repository)
- Business logic อยู่ที่เดียว (service)
- k-anonymity enforce ที่เดียว
- Cache ใช้ร่วมกัน

---

## 8. Migration Plan — 5 Phases

```
Phase 1: Foundation (สัปดาห์ที่ 1)
──────────────────────────────────
  ☐ สร้าง folder structure ใหม่
  ☐ สร้าง schemas/ (Pydantic models) จาก OpenAPI ที่มีอยู่
  ☐ สร้าง repositories/ — ย้าย SQL ออกจาก main.py
  ☐ สร้าง base_repo.py (extract from database.py)
  ☐ ย้าย error handling → middleware/
  ☐ Tests: schema validation tests

Phase 2: Service Layer (สัปดาห์ที่ 2)
─────────────────────────────────────
  ☐ สร้าง services/ ทั้ง 11 ตัว
  ☐ ย้าย business logic จาก main.py → services
  ☐ Implement cache_service.py (Redis)
  ☐ แยก k_anonymity → privacy/
  ☐ Tests: service unit tests

Phase 3: Router Refactor (สัปดาห์ที่ 3)
───────────────────────────────────────
  ☐ แยก main.py → 14 router files
  ☐ Wire up Depends() injection
  ☐ ทุก endpoint ใช้ response_model
  ☐ ย้าย admin.py → routers/admin.py
  ☐ Tests: API integration tests

Phase 4: External APIs (สัปดาห์ที่ 4)
─────────────────────────────────────
  ☐ สร้าง external/arcgis_client.py (28 endpoints)
  ☐ สร้าง gis_service.py — merge clinic_latlong + ArcGIS
  ☐ เพิ่ม GIS router: /api/v2/gis/*
  ☐ Implement PM2.5 overlay endpoint
  ☐ Tests: mock external APIs

Phase 5: MCP + Polish (สัปดาห์ที่ 5)
────────────────────────────────────
  ☐ Refactor MCP server → ใช้ service layer
  ☐ เพิ่ม MCP tools ใหม่ (GIS, comorbidity, longitudinal)
  ☐ Performance testing (locust)
  ☐ Security audit
  ☐ Documentation
```

---

## 9. Key Design Decisions — ทำไมถึงเลือกแบบนี้

| Decision | Rationale |
|---|---|
| **Clean Architecture (layers)** | แยก concern ชัดเจน — เปลี่ยน DB ไม่กระทบ router, เปลี่ยน UI ไม่กระทบ SQL |
| **Repository pattern** | SQL อยู่ที่เดียว — เวลา DBA optimize query แก้ที่เดียว ไม่ต้องหาใน 3,000 บรรทัด |
| **Pydantic v2** | Type safety + auto-OpenAPI docs + ป้องกัน PII leak ที่ serialization layer |
| **Service layer shared by API + MCP** | DRY — business logic อยู่ที่เดียว ไม่ duplicate ระหว่าง REST กับ MCP |
| **Redis caching** | PostgreSQL materialized views ดีแต่ refresh ช้า — Redis ให้ sub-ms response สำหรับ dashboard |
| **External API client with retry** | ArcGIS อาจ timeout/ล่ม — retry + circuit breaker ป้องกัน cascade failure |
| **SQL in .sql files** | อ่านง่าย, DBA review ได้, IDE syntax highlight, version control diff ชัดเจน |
| **Dependency injection** | Testable — mock repo ได้ง่าย, swap implementation ได้ |
| **No ORM** | Raw SQL + materialized views มี performance ดีกว่า ORM สำหรับ analytics workload |

---

## 10. Performance Targets

| Metric | Current (est.) | Target | How |
|---|---|---|---|
| Dashboard load | ~2s | <200ms | Redis cache Tier 1 |
| District detail | ~1s | <300ms | Materialized view + cache |
| GIS overlay | N/A (ไม่มี) | <500ms | ArcGIS cache 24h + Redis |
| PM2.5 + disease | N/A | <1s | Pre-computed hourly + cache |
| Comorbidity matrix | ~3s | <500ms | Pre-computed view + cache |
| MCP tool response | ~2s | <500ms | Shared cache with API |
| Concurrent users | ~10 | 200+ | Connection pool 20 + Redis |

---

## 11. Security Checklist

```
 Keep ──────────────────────────────
 ✓ HMAC-SHA256 ID hashing
 ✓ k-anonymity >= 5
 ✓ PDPA erasure compliance
 ✓ API key authentication
 ✓ PII column blocking
 ✓ Audit logging

 Add ───────────────────────────────
 ☐ Rate limiting backed by Redis (not in-memory)
 ☐ Request ID tracing (X-Request-ID)
 ☐ Input validation via Pydantic (SQL injection prevention at schema level)
 ☐ CORS strict origin list
 ☐ Admin password → bcrypt hash (not plaintext in .env)
 ☐ API key rotation mechanism
 ☐ GIS proxy — ไม่ expose ArcGIS URLs ตรงไปที่ client
 ☐ Response size limit (prevent memory exhaustion)
 ☐ Structured logging (JSON format) for SIEM
```

---

> **สรุป:** ระบบปัจจุบันทำงานได้ดีสำหรับ prototype แต่ `main.py` 3,083 บรรทัดคือ **technical debt** ที่จะเจ็บปวดมากขึ้นเรื่อยๆ เมื่อเพิ่ม GIS integration, LLM agents, และ user roles — การ refactor เป็น Clean Architecture จะทำให้ทีม 2–3 คนทำงานพร้อมกันได้โดยไม่ conflict, test ได้จริง, และ scale ได้ถึงระดับ production ที่ WHO ยอมรับ
