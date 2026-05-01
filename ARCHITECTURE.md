# BMA Health — Architecture

## Table of contents

- [Overview](#overview)
- [Project layout](#project-layout)
- [Three sister surfaces](#three-sister-surfaces)
- [Privacy-by-construction](#privacy-by-construction)
- [Wire diagram](#wire-diagram)
- [Test discipline](#test-discipline)
- [Configuration loading](#configuration-loading)
- [Operator notes](#operator-notes)
- [ADR cross-links](#adr-cross-links)
- [Out-of-scope (S5+ tickets)](#out-of-scope-s5-tickets)

## Overview

BMA Health is a config-driven backend serving aggregate health data, chat-with-tools, and PDF/HTML reports for the Bangkok Metropolitan Administration. Pre-S2 the codebase had hardcoded charts, hardcoded chat tools, and hardcoded report layouts — adding any of the three meant editing service code and rebuilding the frontend. ADR-01, ADR-02, and ADR-03 made each surface declarative: a chart, a chat tool, or a report is now a YAML file plus (optionally) a Python class loaded by `class_path`. Three sister registries (`ChartRegistry`, `ToolRegistry`, `ReportRegistry`) discover those YAMLs at boot and validate them against Pydantic v2 specs.

## Project layout

Top-level layout, annotated with the ADR that governs each directory:

```
api/
├── services/
│   ├── charts/          # ADR-01 (ChartSpec, ChartRegistry, ChartService)
│   ├── chat/            # ADR-02 (ChatService)
│   └── reports/         # ADR-03 (ReportService, BlockRegistry, RendererRegistry)
├── repositories/        # data access (ADR-01 §5 rule — only place SQL lives)
├── agents/              # ADR-02 (LLM adapters, tools, strategies, providers)
│   ├── adapters/        # ADR-02 — LLMAdapter implementations
│   ├── core/            # ADR-02 — OpenMultiAgent orchestrator
│   ├── strategies/      # ADR-02 — tool-call strategies (Anthropic / OpenAI / Gemma)
│   └── tools/           # ADR-02 — BaseTool subclasses + ToolRegistry
├── routers/             # FastAPI route handlers
└── main.py              # app factory + router include map
config/
├── charts/              # ADR-01 — one YAML per ChartSpec
├── tools/               # ADR-02 — one YAML per chat ContentTool
├── reports/             # ADR-03 — one YAML per ReportDescriptor
├── reports/blocks/      # ADR-03 — one YAML per ContentBlock class
└── llm/providers.yaml   # ADR-02 — LLM provider declarations
```

Tests mirror the source tree:

```
tests/
├── services/
│   ├── charts/          # unit tests for ChartService + ChartRegistry
│   ├── reports/         # unit tests for ReportService + blocks
│   └── test_chat_service.py
├── repositories/        # unit tests for MVRepository (in-memory fakes)
└── test_v2_full_coverage.py   # live integration suite (DB + uvicorn required)
```

## Three sister surfaces

The three v2 surfaces share a common shape: a Pydantic spec, a YAML directory, a registry singleton, a service class, and a single FastAPI router prefix.

### Charts (ADR-01)

- **What's config-driven:** each chart spec — title, query_id, dimensions, encoding, k-anon threshold — lives in a YAML file. No code changes for a new chart unless the underlying `query_id` is novel.
- **Pydantic spec:** `ChartSpec` (`api/services/charts/spec.py:83`).
- **Registry:** `ChartRegistry` (`api/services/charts/registry.py:25`); `discover()` scans `config/charts/*.yaml`; lazy singleton via `chart_registry()` at line 119.
- **`class_path` lazy-load:** charts have no plugin classes (the spec drives a generic renderer); the `query_id` field selects an `MVRepository` method instead.
- **Route prefix:** `/api/v2/charts` (`api/routers/charts.py:76`).

### Chat (ADR-02)

- **What's config-driven:** each chat tool (name, description, JSON-schema parameters, `class_path`) lives in a YAML file. LLM providers also live in YAML (`config/llm/providers.yaml`).
- **Pydantic spec:** `ToolSpec` (`api/agents/tools/spec.py`).
- **Registry:** `ToolRegistry` (`api/agents/tools/registry.py:30`); `discover()` scans `config/tools/*.yaml`; lazy singleton via `tool_registry()` at line 190. LLM providers go through `ProviderRegistry` (`api/agents/providers.py:133`) with singleton `provider_registry()` at line 322.
- **`class_path` lazy-load:** every YAML names a `module.path:ClassName`; `import_tool_class()` at `api/agents/tools/spec.py:44` does `importlib.import_module + getattr` on first use, then caches.
- **Route prefix:** `/api/v2/chat` (`api/routers/chat_v2.py:41`).

### Reports (ADR-03)

- **What's config-driven:** each `ReportDescriptor` (cover, blocks, formats) is a YAML; each `ContentBlock` class is also registered via a YAML in `config/reports/blocks/`.
- **Pydantic spec:** `ReportDescriptor` (`api/services/reports/spec.py:92`).
- **Registries:** `ReportRegistry` (`api/services/reports/registry.py:29`); `BlockRegistry` (`api/services/reports/blocks/base.py:195`); `RendererRegistry` (`api/services/reports/renderer.py:66`). Singletons: `report_registry()`, `block_registry()`, `renderer_registry()`.
- **`class_path` lazy-load:** the BlockRegistry imports each block class from its YAML's `class_path`; renderers self-register on import.
- **Route prefix:** `/api/v2/reports` (`api/routers/reports_v2.py:97`).

## Privacy-by-construction

Both ADR-01 §8 and ADR-02 §6 push privacy into the type system rather than relying on per-endpoint review.

**3-layer k-anonymity for charts:**

1. **SQL `HAVING`** clauses inside `MVRepository` query methods drop low-count groups before they leave the database.
2. **`ChartService._apply_k_anon`** (`api/services/charts/service.py:225`) drops or masks any group whose count is below the spec's `k_threshold` after rows reach Python.
3. **`assert_no_individual_fields(rows)`** (`api/services/charts/service.py:71`, loaded from the sibling `bma-med` repo) — final defense-in-depth assertion that the result set contains no PII columns.

**Patient-tier protection:**

- The `bma_med` schema (raw patient rows) is gated to the `bma_med_clinician` and `bma_med_loader` Postgres roles.
- The `api_reader` role used by the FastAPI app only sees `public.mv_*` materialized views — never `bma_med.*`. This invariant is reasserted in the docstring at the top of `api/repositories/mv_repository.py`.

**Chat threads tiered as PII:**

- `chat_message.content` is **never** logged to the audit log; only `length(content)` is recorded so we can monitor abuse without exposing message bodies.
- Chat thread row-level access is enforced in `ChatRepository` (`api/repositories/chat_repository.py:45`).

**Reports cannot bypass the k-anon layer:**

- Chart blocks inside a report do not query the DB directly. They go through `ChartService.render_chart(spec_id, params)`, which means the same 3-layer k-anon path applies whether the chart is reached via `/api/v2/charts/<id>` or embedded in a `/api/v2/reports/<id>.pdf` render.

## Wire diagram

```
                       FastAPI app (api/main.py)
                            │
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                 ▼
   /api/v2/charts/*  /api/v2/chat/*    /api/v2/reports/*
          │                 │                 │
   ChartService   →    ChatService   →   ReportService
          │                 │                 │
   ChartRegistry      ToolRegistry      ReportRegistry
   MVRepository       ChatRepository    BlockRegistry
                                         RendererRegistry
                                         (LaTeX, HTML)
                            │
                       OpenMultiAgent
                            │
                      ProviderRegistry
                       (LMStudioAdapter,
                        AnthropicAdapter)
```

Arrows are read as "depends on / delegates to". `ChartService` → `MVRepository` is the only legal SQL path from the v2 surfaces. The chat and report services may both call `ChartService` (chat for inline visualizations; reports for embedded `chart` blocks) but never call `MVRepository` directly.

## Test discipline

- **Per-surface unit tests:** `tests/services/charts/`, `tests/services/reports/`, and `tests/services/test_chat_service.py` each cover one surface. Repository unit tests live in `tests/repositories/` (currently `test_mv_repository.py`).
- **ABCs are tested via stub subclasses defined inside the test file.** This keeps the public surface of the ABC small and avoids leaking test helpers into the production import graph.
- **DB-touching code is tested with in-memory fakes** rather than a sqlite-on-disk shim — see the S3.5 decision documented in `tests/services/test_chat_service.py`. The fakes implement only the methods the service under test actually calls.
- **Live integration tests live in `tests/test_v2_full_coverage.py`** and require a running PostgreSQL plus uvicorn. They are not part of the unit-test loop and only run in CI's nightly job and manual ops checks.

## Configuration loading

Boot sequence (read top-to-bottom in `api/main.py`):

1. **`validate_production_config()`** runs on import (`api/main.py:145`). It bails at startup if required env-vars (DB URL, API key secret, etc.) are missing in production mode.
2. **Each registry's lazy singleton** (`chart_registry()`, `tool_registry()`, `report_registry()`, `block_registry()`, `renderer_registry()`, `provider_registry()`) loads YAML on first call. All accept a `reload: bool = False` kwarg used by tests.
3. **Renderer registrations happen as a side effect of importing renderer modules.** Each module under `api/services/reports/renderers/` calls `renderer_registry().register(...)` at import time, so a new format becomes available simply by importing its module.
4. **Adapter registrations** follow the same pattern: each adapter module under `api/agents/adapters/` calls `_register_adapter("<name>", <Class>)` at module bottom (see `anthropic.py:307`).
5. **`bootstrap()`** (where present) is called from FastAPI dependency factories so that test runs that bypass `main.py` still warm the registries.

## Operator notes

- **Database migrations:** apply `migrations/300_chat_threads.sql` to create the `bma_med.chat_thread` and `bma_med.chat_message` tables and grant the `bma_med_loader`/`bma_med_clinician` roles.
- **One-time superuser command:** `GRANT bma_med_loader TO etl_user;` so that the ETL role inherits write access to the chat tables. (See the comment block at the bottom of `migrations/300_chat_threads.sql` for the exact incantation.)
- **Frontend types:** `make types` regenerates the frontend's `api.gen.ts` after backend Pydantic changes. Run this any time you add a chart spec, a tool spec, a report descriptor, or a new block class.

## ADR cross-links

- [ADR-01 — Chart Registry & Config-Driven Dashboard](docs/adr/ADR-01-chart-registry.md)
- [ADR-02 — Chat Tool Registry, Provider Abstraction, & Conversation State](docs/adr/ADR-02-chat-tool-registry.md)
- [ADR-03 — Report Descriptors, Content Blocks, & Pluggable Renderers](docs/adr/ADR-03-report-descriptors.md)

For step-by-step instructions on extending each surface, see [docs/plugin-sdk.md](docs/plugin-sdk.md).

## Out-of-scope (S5+ tickets)

Deferred from S2–S4 and tracked as separate epics:

- **8 new block types:** `cover_page` extension, `disease_district_grid`, `crosstab`, `statistical_test_results`, `trend_table`, `callout`, `formula`, `two_column_layout`, `ai_insight`.
- **PPTX renderer** (`fmt = "pptx"`). The renderer ABC and registry already accept it; no implementation yet.
- **DB-backed live-edit of descriptors.** Today every spec lives in a YAML on disk, so editing requires a redeploy. S5 will move descriptors into a `bma_med.report_descriptor` table behind the same `ReportRegistry` interface.
