# ADR-01 — Chart Registry & Config-Driven Dashboard

**Status:** Accepted (S2 sprint kickoff, 2026-05-18)
**Decision authority:** Architecture lead + sprint plan ULTRAPLAN S2.
**Supersedes:** Per-chart hardcoded React components + per-endpoint Python handlers.

---

## Context

Today every dashboard chart is a bespoke React component that:
1. Hardcodes its API endpoint URL.
2. Hardcodes its data shape, colors, units, k-anonymity threshold.
3. Has a matching backend handler with its own SQL.

Adding a new chart = code change in three places (frontend component + frontend route registration + backend handler). This violates the project's hard rule (post-S1): **everything must be config-driven, never hardcoded.**

## Decision

### 1. Single source of truth: `ChartSpec` (Pydantic v2)

```python
class ChartSpec(BaseModel):
    spec_id: str                           # filename stem; unique across project
    kind: Literal["bar", "line", "pyramid",
                  "scatter", "boxplot", "heatmap",
                  "donut", "stacked_bar"]
    title_th: str
    title_en: str | None = None
    description_th: str | None = None
    query_id: str                          # name of a Repository method
    query_params: dict[str, Any] = {}      # static defaults
    accepts: list[FilterParam] = []        # which filters the chart accepts
    axes: AxesSpec
    color_palette: list[str] | None = None # default: BMA-green sequential
    k_anon_threshold: int = 5              # cells < this are masked
    units: dict[str, str] = {}             # per-axis or per-series
    extra: dict[str, Any] = {}             # escape hatch for kind-specific opts
```

### 2. Config-as-data: YAML files in repo

**Location:** `bma-health-db/config/charts/<spec_id>.yaml`. One file per chart.
**Discovery:** server reads all `*.yaml` at startup into the `ChartRegistry` (filesystem scan, fail-loud on parse error).
**Validation:** `python -m chart_tools validate` (CLI) loads each file through `ChartSpec(**yaml)` and fails non-zero on any error. Wired into pre-commit and CI.
**Hot-reload:** **NOT** in S2. Server-restart required after editing YAML. (S5+ may add it.)

### 3. Wire format

Every chart endpoint returns:

```json
{
  "kind": "bar",
  "spec_id": "risk_factor_profile",
  "data": [ { "x": "...", "y": 12, "n": 47 }, … ],
  "meta": {
    "n_total": 181,
    "k_anon_threshold": 5,
    "k_anon_dropped": 3,
    "filters_applied": { "district": "1024" },
    "generated_at": "2026-05-18T08:30:00Z"
  }
}
```

**Same shape for every chart.** The `data` field's row schema is constrained per `kind` (documented in JSON Schema; see `bma-health-db/docs/schemas/chart_data.json`).

### 4. Generic backend route

```
GET /api/v2/charts/{spec_id}?filter1=…&filter2=…
```

Single route, dispatches via `ChartService.render(spec_id, filters)`. **Per-chart endpoints are deprecated** but kept as thin proxies for one cycle, then deleted in S5.

### 5. Service / Repository layering (the "seam")

```
Router  →  Service  →  Repository  →  PostgreSQL
                     ↑
                Strategy + DI
```

- **`ChartService`** (`bma_health_db/services/chart_service.py`) — orchestration. Loads spec from `ChartRegistry`, calls repository, applies k-anon, builds wire JSON.
- **`MVRepository`** (`bma_health_db/repositories/mv_repository.py`) — **the only place** that runs SQL. Methods named after `query_id`s (e.g. `district_disease_counts`); returns Pydantic row models.
- **No router runs SQL directly.** Day-1 lint rule: `git grep "execute_query\|execute(" api/routers/` should drop to zero by EOW.

### 6. Generic frontend renderer

```tsx
<ChartRenderer specId="risk_factor_profile" filters={{district: "1024"}} />
```

Internals:
1. `useChart(specId, filters)` hook fetches `/api/v2/charts/{specId}?…`.
2. Picks an `OptionBuilder` for `response.kind` from a strategy registry (`barOptionBuilder`, `lineOptionBuilder`, `pyramidOptionBuilder`, …).
3. Returns `<ReactECharts option={…} />`.
4. Built-in loading / error / empty / k-anon-masked states.

### 7. TypeScript types

Auto-generated from backend OpenAPI via `openapi-typescript`. Pydantic is the single source of truth. Drift caught in CI by a `make types-check` step.

### 8. Privacy-by-construction

- k-anon `HAVING count(*) >= threshold` is in the SQL of every MV (S1 already did this).
- ALSO applied at `ChartService.apply_k_anon()` as defense in depth.
- ALSO `assert_no_individual_fields(rows)` (from `bma_med.security`) called before serialise.
- Three layers; all have to fail for PII to leak.

---

## Consequences

### Positive

- New chart = 1 YAML + 1 repository method (only when query_id is novel) + zero frontend code.
- `git grep "fetch.*api/v2/" frontend/src/components/charts/` enforced empty in CI.
- Frontend can add a new chart type by registering a new `OptionBuilder` strategy — no other component change.
- Provides the foundation S3 (tools) and S4 (reports) build on.

### Negative / accepted trade-offs

- Initial migration cost: 6 existing charts to port (1.5 days).
- Schema rigidity: ChartSpec must evolve with new chart kinds. Mitigated by `extra: dict[str, Any]` escape hatch.
- YAML files in repo means ops can't tweak charts without a deploy. Mitigated by S5+ DB-backed override layer.

---

## Open question deferred to S5

- Should `ChartSpec` rows live in `bma_med.chart_spec` table (live-editable from admin UI)? **Defer.** YAML-in-repo is sufficient through S2-S4.

---

## Test contract

Every chart spec ships with a fixture under `tests/fixtures/charts/<spec_id>.json` describing:
- An input filter set
- An expected wire-JSON shape (counts can be approximate; structure is exact)

`pytest tests/services/test_chart_service.py` parameterises over every fixture and asserts shape + privacy invariants.

---

## Out-of-scope for ADR-01

- Dashboard layout / page composition (next ADR — `ADR-04-dashboard-layout` in S2 P1)
- Cross-chart drill-down (S5+)
- Real-time / streaming charts (post-S5)
