# Plugin SDK

How to extend BMA Health: add a chart, a chat tool, a report, a renderer, or an LLM provider — without modifying any service code.

## Table of contents

- [How to add a new chart (ADR-01)](#how-to-add-a-new-chart-adr-01)
- [How to add a new chat tool (ADR-02)](#how-to-add-a-new-chat-tool-adr-02)
- [How to add a new report (ADR-03)](#how-to-add-a-new-report-adr-03)
- [How to add a new LLM provider (ADR-02)](#how-to-add-a-new-llm-provider-adr-02)
- [How to add a new report format (ADR-03)](#how-to-add-a-new-report-format-adr-03)
- [Conventions across all surfaces](#conventions-across-all-surfaces)
- [Testing your plugin](#testing-your-plugin)
- [Where to ask](#where-to-ask)

Audience: a new dev (or third-party plugin author) who wants to extend BMA Health. Read [ARCHITECTURE.md](../ARCHITECTURE.md) first for the high-level layout. Each section below is a numbered recipe with a minimal end-to-end example.

## How to add a new chart (ADR-01)

**Step 1.** Write `config/charts/<spec_id>.yaml` (the filename stem must equal `spec_id`).

**Step 2.** If `query_id` is novel (i.e. no existing `MVRepository` method matches), add a method `MVRepository.<query_id>(self, ...)` that returns rows already aggregated and k-anon-filtered at the SQL level.

**Step 3.** Add a row dataclass / Pydantic model in `api/repositories/rows.py` so the frontend types are generated correctly by `make types`.

**Step 4.** Restart the API. `chart_registry()` will pick up the new YAML on first request.

### Minimal example: `district_population` bar chart

```yaml
# config/charts/district_population.yaml
spec_id: district_population
title: Population by district
description: Total population per district from mv_summary_districts.
chart_type: bar
query_id: district_population
encoding:
  x: district_name
  y: total_population
k_threshold: 10
```

Method on `MVRepository`:

```python
# api/repositories/mv_repository.py
def district_population(self) -> list[DistrictPopulationRow]:
    sql = """
      SELECT district_name, SUM(total_population) AS total_population
      FROM public.mv_summary_districts
      GROUP BY district_name
      HAVING SUM(total_population) >= 10
      ORDER BY total_population DESC
    """
    return [DistrictPopulationRow(**row) for row in self._fetch_all(sql)]
```

Now `GET /api/v2/charts/district_population` returns the rendered chart.

## How to add a new chat tool (ADR-02)

**Step 1.** Write `config/tools/<tool_name>.yaml`. The filename stem must equal the `name` field.

**Step 2.** Write a Tool class subclassing `BaseTool` (`api/agents/tools/base.py:18`). Declare a class-level `Parameters: type[BaseModel]` so the registry can publish a JSON schema to the LLM.

**Step 3.** Set `class_path` in the YAML to `module.path:ClassName` — exactly what `import_tool_class` (`api/agents/tools/spec.py:44`) will resolve.

**Step 4.** Restart the API.

### Minimal example: `query_today_count` tool

```yaml
# config/tools/query_today_count.yaml
name: query_today_count
description: Return today's count of new screening records.
class_path: agents.tools.today_count:QueryTodayCountTool
```

```python
# api/agents/tools/today_count.py
from pydantic import BaseModel
from agents.tools.base import BaseTool, ToolResult

class QueryTodayCountParams(BaseModel):
    district: str | None = None

class QueryTodayCountTool(BaseTool):
    name = "query_today_count"
    Parameters = QueryTodayCountParams

    async def run(self, args: dict) -> ToolResult:
        parsed = self.Parameters(**args)
        # ... call MVRepository ...
        return ToolResult(content=f"Today: 1234 new records")
```

The LLM now sees `query_today_count` in its tool list automatically.

## How to add a new report (ADR-03)

**Step 1.** Write `config/reports/<report_id>.yaml`. The filename stem must equal `report_id`.

**Step 2.** Each `block:` reference must already exist. Use `block_registry().list_ids()` (`api/services/reports/blocks/base.py:321`) to introspect what's available.

**Step 3.** If you need a new block type, write a `ContentBlock` subclass (see `api/services/reports/blocks/heading.py:39` for the smallest example) and register it via a YAML in `config/reports/blocks/`.

**Step 4.** Restart the API.

### Minimal example: `district_overview` report

```yaml
# config/reports/district_overview.yaml
report_id: district_overview
title: District overview
formats: [pdf, html]
blocks:
  - block: cover_page
    title: District overview
    subtitle: 2026 cohort
  - block: heading
    text: Population
    level: 1
  - block: chart
    spec_id: district_population
```

Now `GET /api/v2/reports/district_overview.pdf` renders via the LaTeX renderer; `.html` renders via the HTML renderer.

## How to add a new LLM provider (ADR-02)

**Step 1.** Write a class subclassing `LLMAdapter` (`api/agents/adapters/base.py:26`) in `api/agents/adapters/<provider>.py`.

**Step 2.** Register via `_register_adapter("<name>", <Class>)` at the module bottom — see the pattern in `api/agents/adapters/anthropic.py:307`.

**Step 3.** Add a `<name>` entry in `config/llm/providers.yaml`. Use `api_key_env: <ENV_NAME>` so the secret comes from the environment, not the YAML.

**Step 4.** Restart the API.

### About strategies

Tool-call wire formats vary by provider (Anthropic, OpenAI, Gemma each handle the structured-output handshake differently). If your new provider needs custom serialization, register a strategy via `@StrategyRegistry.register(pattern=...)` (see `api/agents/strategies/registry.py:39`) keyed off the model name. Existing strategies live in `api/agents/strategies/` (`anthropic.py`, `openai_native.py`, `gemma.py`).

## How to add a new report format (ADR-03)

**Step 1.** Subclass `ReportRenderer` (`api/services/reports/renderer.py:39`) with the class attribute `fmt = "<your_fmt>"`.

**Step 2.** Register with `renderer_registry().register(<YourRenderer>())` at module import. The convention is one module per format under `api/services/reports/renderers/` — each module self-registers when imported (see the docstring in `renderers/__init__.py`).

**Step 3.** Add a `render_<your_fmt>` method to each `ContentBlock` subclass you want to support. Existing blocks declare both `render_html` and `render_latex` (see `api/services/reports/blocks/chart.py:120` and `:246`).

**Step 4.** Any descriptor that already declares `formats: [..., <your_fmt>]` automatically gains the new format — no edits to existing descriptors needed.

## Conventions across all surfaces

- All Pydantic models use `model_config = ConfigDict(extra="forbid")`. A typo in YAML fails loud at boot, never silently as a no-op.
- All YAMLs are scanned at boot from a fixed config directory. No silent skips: a parse error or schema mismatch raises and the app refuses to start.
- `class_path` strings always use the format `module.path:ClassName`. The loader splits on `:` and does `importlib.import_module + getattr`.
- Filename stem MUST match the spec_id / tool name / block_id / report_id. Mismatches are rejected at registry load.
- Singletons (`chart_registry()`, `tool_registry()`, `report_registry()`, `block_registry()`, `renderer_registry()`, `provider_registry()`) are lazy. Each accepts a `reload: bool = False` kwarg so tests can rebuild the registry against a tmpdir without restarting the process.

## Testing your plugin

The pattern across all surfaces: write your YAML into a tmpdir, point the registry's `discover()` at that tmpdir, and assert your spec is loaded.

```python
# tests/services/charts/test_my_chart.py
from pathlib import Path
from services.charts.registry import ChartRegistry

def test_my_chart_loads(tmp_path: Path) -> None:
    spec_yaml = tmp_path / "district_population.yaml"
    spec_yaml.write_text(
        "spec_id: district_population\n"
        "title: Population by district\n"
        "chart_type: bar\n"
        "query_id: district_population\n"
        "encoding:\n  x: district_name\n  y: total_population\n"
        "k_threshold: 10\n"
    )
    registry = ChartRegistry.discover(tmp_path)
    assert "district_population" in registry.list_ids()
    spec = registry.get("district_population")
    assert spec.title == "Population by district"
```

Mirror this pattern for `ToolRegistry.discover`, `ReportRegistry.discover`, `BlockRegistry.discover`. For services, define stub repositories inside the test file (per the in-memory-fake convention documented in `tests/services/test_chat_service.py`) — do not require a live DB.

## Where to ask

The cleanest reference implementations live in the test suite:

- Charts: `tests/services/charts/`
- Reports: `tests/services/reports/`
- Chat: `tests/services/test_chat_service.py`
- Repositories: `tests/repositories/test_mv_repository.py`

For end-to-end integration patterns (DB + uvicorn) see `tests/test_v2_full_coverage.py`.

For the architectural rationale behind each plugin point, read the ADRs:

- [ADR-01 — Chart Registry](adr/ADR-01-chart-registry.md)
- [ADR-02 — Chat Tool Registry](adr/ADR-02-chat-tool-registry.md)
- [ADR-03 — Report Descriptors](adr/ADR-03-report-descriptors.md)
