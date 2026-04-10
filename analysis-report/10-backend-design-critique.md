# Backend Design Critique
## BMA Health Screening Platform

> **Context:** FastAPI + PostgreSQL 16 + Redis 7 backend สำหรับข้อมูลคัดกรองสุขภาพ 50 เขต กทม
> **Stage:** Post-prototype, pre-production — ระบบทำงานได้แต่กำลังจะเพิ่ม GIS + LLM agents
> **Reviewed by:** Architecture critique framework

---

## Overall Impression

**สิ่งที่ทำได้ดีมาก:** Privacy-first design ที่จริงจัง — k-anonymity, PII hashing, PDPA compliance, audit logging ทำได้ดีกว่า health-tech startups หลายแห่ง ระบบ materialized views ของ PostgreSQL เป็นการตัดสินใจที่ถูกต้องสำหรับ analytics workload

**โอกาสที่ใหญ่ที่สุด:** `main.py` 3,083 บรรทัด 72 endpoints + 338 SQL fragments + 115 database calls + **0 Pydantic models** + **0 tests** = ระเบิดเวลาที่จะพังตอนเพิ่ม feature ใหม่

---

## Usability (Developer Experience)

| Finding | Severity | Recommendation |
|---------|----------|----------------|
| `main.py` 3,083 บรรทัด — ต้อง scroll หาไม่เจอ ไม่มี IDE navigation ที่ดี | **Critical** | แยก 14 router files ตาม domain (summary, districts, epidemiology...) |
| **0 Pydantic models** — `response_model` ไม่เคยถูกใช้เลย ทำให้ OpenAPI docs ไม่มี response schema | **Critical** | สร้าง `schemas/` folder — ทุก endpoint ต้องมี response_model |
| **0 test files** — ไม่มี tests directory เลย ไม่มี conftest.py | **Critical** | เริ่มจาก integration tests สำหรับ endpoints สำคัญ (overview, districts, comorbidity) |
| SQL string building แบบ manual (`f"WHERE {' AND '.join(...)}"`) กระจายอยู่ทั่ว main.py | **Moderate** | ย้าย SQL ไปอยู่ใน repository layer หรือ `.sql` files |
| `DISEASE_KEYS` dict ถูก define ซ้ำใน main.py (บรรทัด 109) **และ** server.py (บรรทัด 74) | **Moderate** | ย้ายไป shared module — `constants/diseases.py` |
| MCP server สร้าง connection pool ของตัวเอง แยกจาก API | **Moderate** | Share database layer เดียวกัน หรือใช้ service layer ร่วม |
| Error handling มี try/except แค่ **3 จุด** ใน 3,083 บรรทัด — endpoints ส่วนใหญ่ปล่อย crash | **Moderate** | Global exception handler + AppException hierarchy |
| ไม่มี Dependency Injection — ทุก endpoint เรียก `execute_query()` ตรง | **Minor** | FastAPI `Depends()` → repository → service |

---

## Visual Hierarchy (Code Architecture)

### "สายตาจับอะไรก่อน?" — Flow ของ developer ที่เข้ามาใหม่

```
Developer เปิดโปรเจค:
  api/
    main.py    ← 3,083 lines "...อะไรอยู่ตรงไหน?"
    admin.py   ← 1,009 lines
    database.py← 91 lines    ← "โอเค เข้าใจ"
    security.py← 141 lines   ← "โอเค เข้าใจ"
    config.py  ← 47 lines    ← "โอเค เข้าใจ"

ปัญหา: 85% ของ logic อยู่ใน 2 ไฟล์ (main.py + admin.py)
        ไม่มี "reading order" — ต้อง ctrl+F หา endpoint ที่ต้องการ
```

**Eye flow ที่ควรจะเป็น:**
```
routers/          ← "endpoints อยู่ที่นี่ แยกตาม domain"
  summary.py      ← 5 endpoints, ~80 lines
  districts.py    ← 3 endpoints, ~60 lines
  epidemiology.py ← 6 endpoints, ~100 lines
  ...

services/         ← "business logic อยู่ที่นี่"
  disease_service.py
  lab_service.py
  ...

schemas/          ← "data contracts อยู่ที่นี่"
  disease.py
  lab.py
  ...
```

### Reading Order Assessment

| Layer | Current State | Score |
|---|---|---|
| Entry point (`main.py`) | ถูกต้อง — app creation ชัดเจน | Good |
| Middleware chain | Audit → APIKey → RateLimit → CORS — ลำดับถูก | Good |
| Route organization | **ไม่มี** — 72 endpoints ปนกันตาม comment headers | Bad |
| Data flow | Handler → raw SQL → dict — **ไม่มี layer กลาง** | Bad |
| Configuration | `config.py` ชัดเจน + production validation | Good |

---

## Consistency

| Element | Issue | Recommendation |
|---------|-------|----------------|
| **Return types** — บาง endpoint return `{"data": [...]}` บาง endpoint return `[...]` ตรงๆ | ไม่มี standard response envelope | ใช้ `{"data": T, "meta": {"total": N, "generated_at": str}}` ทุก endpoint |
| **Error format** — บาง endpoint raise `HTTPException` บางจุดปล่อย 500 | ไม่ consistent | Global error handler → `{"error": code, "message": str, "request_id": str}` |
| **Query param naming** — `district` (singular) vs `zone_code` vs `sex` vs `age_group` | ชื่อ parameter ไม่เป็นรูปแบบเดียว | สร้าง `QueryFilters` Pydantic model ใช้ร่วม |
| **SQL style** — บาง query ใช้ `%s` placeholder บางจุดใช้ f-string | เสี่ยง SQL injection | ใช้ parameterized queries ทุกที่ (ส่วนใหญ่ดีอยู่แล้ว แต่ f-string ต้องกำจัด) |
| **k-anonymity** — enforce ใน API (security.py) **แยกจาก** MCP (server.py) ที่ reimplement เอง | Logic ซ้ำ 2 ที่ | Share privacy module เดียวกัน |
| **Date handling** — ไม่มี standard ว่า timezone ใช้ UTC หรือ Bangkok | `datetime.utcnow()` ปนกับ no-tz dates | ใช้ `datetime.now(timezone.utc)` + store UTC everywhere |

---

## Accessibility (ในที่นี้ = Operability & Observability)

| Area | Current | Target | Gap |
|---|---|---|---|
| **Health check** | `/health` ตรวจ DB only | ตรวจ DB + Redis + external APIs | ไม่มี Redis health check |
| **Structured logging** | Text format (`method=GET path=...`) | JSON format สำหรับ log aggregator | ไม่พร้อมสำหรับ ELK/Grafana |
| **Request tracing** | ไม่มี request ID | X-Request-ID header สำหรับ trace across services | ไม่สามารถ debug cross-service |
| **Metrics** | ไม่มี | Prometheus `/metrics` endpoint | ไม่มี performance monitoring |
| **Redis usage** | มี container ใน docker-compose แต่ **code ไม่เคยเรียก Redis เลย** | Cache layer สำหรับ heavy queries | Redis วิ่งเปล่า 100% |
| **Error alerting** | ไม่มี | Error rate > threshold → alert | Silent failures |
| **DB connection monitoring** | Pool 2–20 connections, no monitoring | Pool stats endpoint | ไม่รู้ว่า pool exhausted |

---

## What Works Well

- **Privacy architecture** เยี่ยม — k-anonymity >= 5, PII column blocklist, HMAC-SHA256 hashing, PDPA erasure support ครบ ดีกว่า health-tech หลายแห่ง
- **Materialized views** — 5 summary views (`summary_district_disease`, `summary_district_risk_factors`, etc.) ลด query complexity + performance ดี
- **Migration system** — 6 ordered SQL migrations, idempotent, มี PDPA compliance migration แยก
- **Config validation** — `validate_production_config()` บังคับเปลี่ยน default secrets ก่อน deploy production
- **Audit logging** — ทั้ง API access log (middleware) และ MCP SHA-256 chained audit log
- **Docker Compose** — healthcheck ครบ, port binding 127.0.0.1 only (ไม่ expose ข้างนอก), depends_on condition

---

## Priority Recommendations

### 1. **Break the God File** (Impact: Critical, Effort: 1 week)

`main.py:3083` → 14 router files, ~80–120 lines each

**ทำไม:** ไม่มีทาง code review, merge, หรือ onboard developer ใหม่ได้เมื่อ 85% ของ logic อยู่ในไฟล์เดียว ทุก git conflict จะอยู่ใน main.py

**วิธี:**
```python
# main.py — after refactor: ~50 lines
app = FastAPI(...)
app.include_router(summary_router)
app.include_router(district_router)
app.include_router(epidemiology_router)
# ... 11 more routers
```

### 2. **Add Pydantic Response Models** (Impact: Critical, Effort: 3 days)

0 models → ~20 models ใน `schemas/`

**ทำไม:** ตอนนี้ OpenAPI docs ไม่มี response schema — frontend, MCP, และทีมอื่นไม่รู้ว่า API return อะไร ต้องดู source code ถึงจะรู้ format

**ผลที่ได้:**
- Auto-generated TypeScript types สำหรับ frontend
- Runtime response validation (ป้องกัน PII leak)
- MCP tools ใช้ schema เดียวกัน

### 3. **Introduce Service Layer** (Impact: High, Effort: 1 week)

**ทำไม:** ตอนนี้ MCP server (1,016 lines) **duplicate SQL logic ทั้งหมด** จาก main.py — แก้ bug ต้องแก้ 2 ที่ เพิ่ม feature ต้องเพิ่ม 2 ที่

```
Before:  main.py ──SQL──→ DB     mcp/server.py ──SQL──→ DB  (duplicate)
After:   main.py → service → repo → DB ← mcp/server.py     (shared)
```

### 4. **Actually Use Redis** (Impact: High, Effort: 2 days)

Redis container วิ่งอยู่แล้ว แต่ **ไม่มี code ใน api/ ที่ import redis เลย**

```
Dashboard overview:    ~2s → <200ms  (cache 5 min)
District detail:       ~1s → <100ms  (cache 1 hour)
Materialized refresh:  Flush all cache
```

### 5. **Write Tests** (Impact: High, Effort: 1 week ongoing)

0 tests → เริ่มจาก 20 integration tests สำหรับ critical paths

**Priority test targets:**
- `overview` endpoint — ถูกเรียกมากที่สุด
- `comorbidity-matrix` — complex SQL
- `k-anonymity enforcement` — security-critical
- `PII column blocking` — privacy-critical
- `admin upload` — data ingestion

---

## Quantified Risk Assessment

```
Risk Matrix (Likelihood x Impact):
───────────────────────────────────────────────────────
                       Low Impact    High Impact
High Likelihood    │              │ God file merge   │
                   │              │ conflicts         │
                   │              │ Silent data leak  │
                   │              │ (no response      │
                   │              │  model)           │
───────────────────┼──────────────┼───────────────────│
Low Likelihood     │ Redis waste  │ SQL injection     │
                   │              │ (f-string SQL)    │
                   │              │ MCP drift (logic  │
                   │              │ diverge from API) │
───────────────────────────────────────────────────────
```

---

## Score Summary

| Dimension | Score | Notes |
|---|---|---|
| **Privacy & Security** | 9/10 | World-class for a gov health platform — k-anonymity, PDPA, hashing |
| **Functionality** | 8/10 | 72 endpoints cover most use cases; GIS integration missing |
| **Code Organization** | 3/10 | God file, no layers, no tests |
| **Developer Experience** | 3/10 | New dev needs days to understand main.py |
| **Operability** | 4/10 | Redis idle, no metrics, no request tracing |
| **Consistency** | 5/10 | Return formats vary, error handling inconsistent |
| **Scalability** | 5/10 | Materialized views help, but no caching, in-memory rate limit |
| **Overall** | **5.3/10** | Solid prototype, needs architectural refactor for production |

---

> **Bottom line:** ระบบนี้มีพื้นฐานที่ดีมาก (privacy, materialized views, Docker) แต่ **โตมาจาก prototype ที่ไม่ได้ refactor** ก่อนที่จะเพิ่ม GIS integration + LLM agents + multi-team development ต้อง break the God file, add response schemas, และ introduce service layer — มิเช่นนั้นทุก feature ใหม่จะยากขึ้นเรื่อยๆ แบบ exponential
