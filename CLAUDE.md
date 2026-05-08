# CLAUDE.md

> **DEPRECATED — DO NOT EXTEND THIS PROJECT.**
>
> This repository has been retired. The active replacement is **`bma-dms-backend`** at
> `/Users/dev/bma-dms-backend/` (Fastify + MongoDB, port 3001).
>
> If a user asks you to add a feature, fix a bug, or extend functionality here, **stop and
> point them to `bma-dms-backend` instead**. Only proceed with work in this repo if the user
> explicitly acknowledges the deprecation (e.g. "I know it's deprecated, just do X for git
> history / archival / migration purposes").
>
> The Postgres container, volumes, source CSVs, and `.env` have already been removed —
> running `make start`, `make etl`, or the test suite will fail until the database is
> rebuilt from scratch. See [README.md](README.md) for the deprecation notice.

---

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

BMA Health Database — One-stop backend for Bangkok Metropolitan Administration health screening (155 endpoints, FastAPI + PostgreSQL). Serves aggregate health data for 50 districts / 8 zones, LLM-powered chat, LaTeX/PDF reports, and an admin panel. **No individual records or PII are ever exposed.**

## Commands

```bash
# Service management
make start              # Start API (port 9002) + Cloudflare tunnel (background)
make stop               # Stop API + tunnel
make status             # Check all services
make dev                # Start API with hot-reload (foreground, for development)

# Database
make infra              # Start PostgreSQL (Docker) on port 5433
make migrate            # Run all SQL migrations (001-010)
make seed               # Load reference data
make refresh-views      # Refresh all 13 materialized views
make db-stats           # Show row counts

# ETL
make etl                # Import CSV files from minimal_data/portal_top
make etl-backfill       # Backfill district_code + refresh views only

# Testing
cd api && python3 -m pytest -v                    # Full suite (~225 tests)
cd api && python3 -m pytest -k "admin" -x -q      # Run specific tests
cd api && python3 -m pytest tests/test_gis.py -v   # Single file

# Reports
make generate-reports   # Trigger PDF generation (22 reports)
make report-catalog     # List reports with cache status
```

## Architecture

```
api/
├── main.py                 # FastAPI app, middleware, router mounting
├── admin.py                # Admin panel (HTML): upload, bundle, dashboard, history
├── database.py             # psycopg2 pool, execute_query() — auto-strips PII columns
├── security.py             # X-API-Key middleware, CORS, rate limiting, k-anonymity threshold
├── cache.py                # Redis 4-tier TTL cache (5min/15min/1hr/24hr), fail-open
├── config.py               # All env vars with defaults
├── routers/                # 24 FastAPI routers (one per domain group)
├── services/
│   ├── data_adapter.py     # load_district_data() — main data bridge (DB → JSON format)
│   ├── report_generator.py # LaTeX/PDF whitepaper + slides generation
│   ├── report_data_collector.py  # Aggregates all data for reports
│   ├── zone_report_generator.py  # Per-zone slide decks
│   └── statistics_service.py     # Chi-square, ANOVA, odds ratio
├── agents/                 # LLM chat pipeline
│   ├── core/orchestrator.py      # Main pipeline: analyst → tools → synthesizer
│   ├── core/router.py            # Keyword-based tool selection
│   ├── tools/                    # 7 tools (health_data, statistical, report, etc.)
│   ├── adapters/lmstudio.py      # LMStudio/OpenAI-compatible HTTP client
│   └── prompts/health_assistant_skill.md  # System prompt
├── data/
│   ├── facts.py            # HEALTH_ZONES — single source of truth for zone→district mapping
│   └── pm25_stations.py    # ArcGIS PM2.5 station name extraction
├── templates/
│   ├── admin/              # Jinja2 HTML templates for admin panel
│   └── latex/              # LaTeX/Jinja2 templates for PDF reports
etl/
├── import_csv.py           # CSV→PostgreSQL ETL (7 tables), backfill, view refresh
db/
├── migrations/             # 001-010 SQL migrations
└── seeds/                  # Reference data (districts, facilities, zones)
```

## Key Data Flow

1. **CSV Upload** → `admin.py` `/admin/upload-bundle` → `etl/import_csv.py` → raw tables → materialized views → API
2. **API Request** → `security.py` (API key + rate limit) → router → `database.py` (parameterized query, PII stripped) → JSON
3. **LLM Chat** → `orchestrator.py` → guardrail check → keyword router → tool execution → synthesizer → SSE stream
4. **Report Gen** → `report_data_collector.py` (DB queries) → `report_generator.py` (Jinja2 → LaTeX → PDF)

## Critical Rules

- **PII**: `database.py` auto-strips `idcard_hash`, `patient_id`, `staff_code` from ALL query results
- **k-anonymity**: Groups < 5 people are suppressed. Threshold in `security.py:K_ANONYMITY_THRESHOLD`
- **Zone mapping**: `api/data/facts.py:HEALTH_ZONES` dcodes MUST match `ref_districts` in DB. If they diverge, reports/API/frontend show different numbers
- **District codes are OFFICIAL HRSI / BMA**: same numbering as `bma-health/frontend/public/vectors/bangkok-districts.geojson` (the truth source). District NAME is the unambiguous identifier — if you need a code for a new district, look it up in the geojson, never invent one. Migration 014 historically remapped 20 dcodes (1031..1050) from a non-official ad-hoc system to the official one. Source data ingested before that migration was translated in-place.
- **SQL injection**: Use parameterized queries (`%s` placeholders) everywhere. For table/column identifiers, use `psycopg2.sql.Identifier()`
- **ETL caching**: `admin.py:_load_etl()` caches the ETL module by mtime. Edits to `etl/import_csv.py` are picked up on the next import without restart
- **LLM guardrails**: Two layers — input keyword filter (`_is_on_topic`) rejects off-topic before calling LLM; system prompt enforces scope. If LLM skips tools, orchestrator forces the top-priority tool from router
- **Tool output format**: LLM tools must return readable Thai text, not raw JSON. The synthesizer cannot summarize large JSON blobs

## Database

- **PostgreSQL 16** on port 5433 (Docker container `bma-health-db`)
- **7 raw tables**: raw_patients, raw_visits, raw_vitalsigns, raw_homevisit, raw_homehealth, raw_lab_results, raw_lab_extended
- **13 materialized views**: summary_district_disease, summary_bmi_waist, etc. — refreshed after import via `REFRESH MATERIALIZED VIEW CONCURRENTLY` (requires unique indexes)
- **Reference tables**: ref_districts (50 BKK districts), ref_facilities, ref_health_zones, ref_facility_districts (facility code → district mapping for backfill)
- **Bundle upload** truncates all tables then imports in order: pt → pthistory → vitalsignslf → homevisit → homehealth → labhealth → labhealthext. Data is committed before view refresh to prevent rollback

## Environment

Required in `.env`:
```
DATABASE_URL=postgresql://postgres:bma_health_dev@localhost:5433/bma_health
API_KEY=dev-api-key
ADMIN_PASSWORD=admin
```

Optional:
```
REDIS_URL=redis://localhost:6379/0          # Cache (fail-open if unavailable)
LMSTUDIO_URL=http://localhost:5555          # LLM for chat (503 fallback if down)
LLM_MODEL=google/gemma-4-26b-a4b
```

## Testing Notes

- Tests use the **real Docker PostgreSQL** — not mocked
- `conftest.py` sets `RATE_LIMIT_PUBLIC=5000` to avoid throttling
- Tests are read-only (no writes to DB)
- API key for tests: `dev-api-key`
