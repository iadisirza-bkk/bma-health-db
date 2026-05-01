# ADR-03 — Report Descriptors, Content Blocks, & Pluggable Renderers

**Status:** Accepted (S4 sprint kickoff, 2026-06-01)
**Decision authority:** Architecture lead + sprint plan ULTRAPLAN S4.
**Supersedes (gradually):** Monolithic `ReportGenerator` + per-report Python service files
(`msd_report_generator.py`, `zone_report_generator.py`, `repeat_screening_report.py`).

---

## Context

Today's report generation layer (`api/services/report_*.py`, ~3,455 LOC across 5 files,
plus 12+ Jinja2 LaTeX templates):

- One `ReportGenerator` class hardcodes whitepaper + slides flows for 10 languages.
- Per-flavor classes (`MsdReportGenerator`, `ZoneReportGenerator`, `RepeatScreeningReport`)
  duplicate the same data-collection + Jinja2 + Tectonic + caching pipeline.
- Templates are LaTeX-only. There is no HTML or PPTX path.
- Adding a new report = new class + new `.tex.j2` + new router endpoint + new schema row.

This violates the post-S1 hard rule "EVERYTHING AFTER THIS SHOULD NEVER BE HARDCODED."
ADR-01 (charts) and ADR-02 (chat) made their respective surfaces config-driven; this ADR
does the same for reports.

## Decision

### 1. Two top-level layers

```
ReportDescriptor (YAML)  ──>  ContentBlock instances (Python)  ──>  Renderer (LaTeX/HTML/PPTX)
```

Descriptor = "what to put in the report" (declarative, in repo).
Content block = "how to compute one section" (Python class, reusable across reports).
Renderer = "how to format the assembled blocks" (Strategy; one per output format).

### 2. ReportDescriptor (Pydantic v2)

```python
class ReportDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str                       # filename stem; unique across project
    title_th: str
    title_en: Optional[str] = None
    formats: list[Literal["latex", "html", "pptx"]]   # which renderers must support this
    languages: list[str] = ["th"]                     # ISO codes
    audience: list[Literal["public", "clinician", "admin", "msd"]] = ["public"]
    sections: list[SectionSpec]                       # ordered
    style: StyleSpec = Field(default_factory=StyleSpec)
    cache: CacheSpec = Field(default_factory=CacheSpec)
    metadata: dict[str, Any] = Field(default_factory=dict)

class SectionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    block: str                              # ContentBlock class identifier (e.g. "kpi_grid")
    params: dict[str, Any] = {}             # passed to block.render(context, params=...)
    title_th: Optional[str] = None
    visible_in: Optional[list[str]] = None  # restrict to specific formats; None = all
```

### 3. ContentBlock ABC

```python
class ContentBlock(ABC):
    block_id: ClassVar[str]              # registry key, e.g. "kpi_grid"
    Parameters: ClassVar[type[BaseModel]] = BaseModel
    @abstractmethod
    def collect(self, ctx: RenderContext, params: BaseModel) -> dict[str, Any]: ...
    def render_latex(self, data: dict, params: BaseModel, ctx: RenderContext) -> str: ...
    def render_html(self, data: dict, params: BaseModel, ctx: RenderContext) -> str: ...
    def render_pptx(self, data: dict, params: BaseModel, ctx: RenderContext) -> dict: ...
```

- `collect()` is data-only, format-agnostic (queries DB / charts / KPIs once).
- `render_*` methods consume the same `data` dict and emit format-specific markup.
- A renderer may not exist for some formats — block raises `NotImplementedError` for
  unsupported formats; the orchestrator catches and warns (descriptor `visible_in` is
  the cleaner way to opt out).
- Block registry mirrors ChartRegistry / ToolRegistry: `BlockRegistry.discover()` scans
  `config/reports/blocks/*.yaml` (one YAML per block declares its `class_path`,
  description, ownership). Adding a new block = new class + new YAML.

Initial blocks (ports of existing template snippets):
- `heading` / `paragraph` — bare scaffolding
- `kpi_grid` — tile of KPI numbers
- `chart` — embeds an ADR-01 ChartSpec by `spec_id`
- `table` — long-format data table
- `district_compare` — port of the legacy MSD descriptive-stats section
- `cover_page` — title + date + scope summary
- `appendix_methodology` — static methodology block

Pluggability: any block can be added by 3rd-party code via
`BlockRegistry.register(MyBlock)` — no project edit needed.

### 4. Renderer ABC

```python
class ReportRenderer(ABC):
    fmt: ClassVar[str]               # 'latex' | 'html' | 'pptx'
    @abstractmethod
    def render(self, desc: ReportDescriptor, sections: list[RenderedSection],
               ctx: RenderContext, out_path: Path) -> Path: ...
```

- `LaTeXRenderer` wraps the existing Jinja2 + Tectonic pipeline. The current
  `report_generator.py` becomes a thin shim that delegates here.
- `HTMLRenderer` — Jinja2 with HTML templates living at `templates/html/*.j2`. Output:
  one self-contained `.html` (charts inlined as SVG via existing chart_generator).
- `PPTXRenderer` — `python-pptx`-based. Each section becomes one slide; blocks emit
  pptx-specific dicts (`{"layout": "title_content", "title": ..., "body": [...]}`).
  Pull this in S5 if scope tightens; in S4 we ship LaTeX + HTML and stub PPTX with a
  `NotImplementedError` (with a clear message) so the registration is in place.

### 5. ReportService (orchestrator)

```python
class ReportService:
    def __init__(self, registry: ReportRegistry, blocks: BlockRegistry,
                 renderers: dict[str, ReportRenderer], data: ReportDataCollector): ...
    async def render(self, report_id: str, fmt: str, lang: str) -> Path: ...
    async def list(self) -> list[dict]: ...
```

- Loads descriptor by id, picks the renderer by `fmt`, resolves each section's
  `block` to a `ContentBlock` instance, calls `block.collect(ctx, params)` once,
  then `block.render_<fmt>(data, params, ctx)`.
- Caches by `(report_id, fmt, lang, data_hash)` — same hash semantics the existing
  `report_generator.py` uses today.

### 6. Wire route & response

`GET /api/v2/reports` — catalog (id, title, formats, languages).
`GET /api/v2/reports/{report_id}/{fmt}/{lang}` — generate-or-cached download.
`GET /api/v2/reports/{report_id}/spec` — raw descriptor for debugging.

Legacy `/api/reports/comprehensive/{lang}` and `/api/reports/executive/{lang}` stay
during S4; they're rerouted internally to call `ReportService.render(...)`.
Decommission in S5 once frontend cuts over.

### 7. Data collection

Existing `report_data_collector.py:collect_report_data()` is the single source of
aggregate data. Refactored into a `ReportDataCollector` class held by `ReportService`
and passed to each block via `RenderContext`. Blocks query through it; they don't
hit the DB directly.

This is the same "Repository = only place SQL lives" rule from ADR-01 §5, applied to
reports. If a block needs data the collector doesn't provide, the collector grows a
new method — block code stays SQL-free.

### 8. Frontend impact

- `frontend/src/app/admin/reports/` already has a UI driven by
  `ReportDashboardResponse`. Switch its data source to `/api/v2/reports` (which
  returns the same shape via a thin adapter). No UI rewrite for S4.
- `frontend/src/types/api.gen.ts` regenerated as part of `make types`.

---

## Consequences

### Positive
- New report = 1 YAML + (optionally) 1 new ContentBlock class + zero changes to
  renderer / orchestrator / router.
- New format (PPTX, DOCX) = 1 Renderer class + register; existing reports automatically
  gain that format if their blocks have `render_<fmt>` impls.
- LaTeX templates collapse from 12 partial templates + glue code into a much smaller
  set of block-level Jinja2 partials.
- One data-collection pass per report (block.collect runs once, all renderers reuse).
- Same caching / hashing wins from ADR-01 §1 carry over.

### Negative / accepted trade-offs
- Migration cost: 4 existing report flavors to port to descriptors. ~2 days; tracked as
  S4.4 sub-tickets per report.
- Block surface is narrower than freeform LaTeX templates — gnarly bespoke layouts
  (e.g. MSD `part5_factor_analysis.tex.j2` with 200+ lines of pgfplots) need either
  a dedicated block class or stay as raw-LaTeX escape-hatch blocks.
- Two render paths (LaTeX vs HTML) means double the per-block work; pick which formats
  matter per report via descriptor `formats` so we don't pay for unused output.

---

## Test contract

- `tests/services/reports/test_descriptor_loader.py` — every YAML in
  `config/reports/*.yaml` parses and resolves all `block` references.
- `tests/services/reports/test_block_registry.py` — every registered block has a
  `Parameters` Pydantic model and at least one `render_*` method.
- `tests/services/reports/test_render_e2e.py` — render the smallest descriptor
  (cover_page only) in both LaTeX and HTML; assert non-empty output and basic
  structural invariants (e.g. `<title>` present in HTML, `\documentclass` in LaTeX).

---

## Out-of-scope for ADR-03

- Real-time co-edit / preview of descriptors (ops-tier feature, S5+).
- Granular per-block ACL (audience filter at block, not just descriptor) — defer.
- DOCX renderer — defer to S5+ if a stakeholder asks.
- Bilingual/side-by-side rendering — defer.
- Database-backed descriptors (live-edit from admin UI) — same deferral as ADR-01 §"Open
  question deferred to S5".
