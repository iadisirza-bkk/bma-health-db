# ADR-02 — Chat Tool Registry, Provider Abstraction, & Conversation State

**Status:** Accepted (S3 sprint kickoff, 2026-05-25)
**Decision authority:** Architecture lead + sprint plan ULTRAPLAN S3.
**Builds on:** ADR-01 (chart layer); the existing partly-OOP chat code in `api/agents/`.

---

## Context

S2 made charts config-driven (ADR-01). The chat / agent layer is the next surface
that violates the post-S1 hard rule "EVERYTHING AFTER THIS SHOULD NEVER BE HARDCODED."

What's already OOP today:
- `LLMAdapter(ABC)` (`api/agents/adapters/base.py`) — chat / stream / health_check interface.
- `LMStudioAdapter` — concrete impl.
- `BaseTool(ABC)` (`api/agents/tools/base.py`) — `execute(args) -> ToolResult`.
- `ToolRegistry` — register / get / list / OpenAI-schema export.
- `GemmaToolCallStrategy` / `OpenAINativeStrategy` — model-aware tool-call parsing.
- `OpenMultiAgent` orchestrator with `analyst_adapter` + `synthesizer_adapter`.

What still violates "no hardcode":
1. **Single provider**: only LMStudio is supported. Claude / OpenAI / Ollama need
   first-class adapters with the same `LLMAdapter` interface.
2. **Tool wiring is `create_default()` Python**: adding a new tool requires editing
   `agents/__init__.py` and `tools/registry.py`. Should be YAML-driven.
3. **No conversation persistence**: `chat_stream_endpoint` takes `history` as a
   query string from the client. There is no server-side `conversation_id`.
4. **Strategy selection is a hardcoded ternary**: `GemmaToolCallStrategy() if is_gemma
   else OpenAINativeStrategy()`. Should be a strategy registry that maps model →
   strategy by config.
5. **Tool parameters are raw JSON Schema dicts** (`parameters_schema: dict`).
   Should be Pydantic v2 models so we get validation + OpenAPI schema for free.

## Decision

### 1. ChatAdapter contract (no breaking changes)

Keep the existing `LLMAdapter` ABC. It is already correctly shaped. We add concrete
implementations alongside `LMStudioAdapter`:

- `LMStudioAdapter`     — already exists, talks to local OpenAI-compatible server.
- `AnthropicAdapter`    — Claude API; uses the official `anthropic` Python SDK.
- `OpenAIAdapter`       — OpenAI / Azure OpenAI / OpenRouter (any OAI-compatible).
- `OllamaAdapter`       — local Ollama (different request shape from LMStudio).

Each implements `health_check`, `chat`, `stream`. Each lives in
`api/agents/adapters/<provider>.py`.

### 2. Provider registry (`api/agents/providers.py`)

```python
class ProviderRegistry:
    def register(name: str, adapter_cls: type[LLMAdapter]) -> None
    def build(name: str, model: str, **overrides) -> LLMAdapter
    def list() -> list[str]
```

Construction is **config-driven** from `config/llm/providers.yaml`:

```yaml
providers:
  - name: lmstudio
    adapter: lmstudio
    base_url: ${LMSTUDIO_URL}
    timeout: 120
  - name: claude
    adapter: anthropic
    api_key_env: ANTHROPIC_API_KEY
    timeout: 60
  - name: openai
    adapter: openai
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY

defaults:
  analyst:     { provider: lmstudio, model: gemma-3-27b }
  synthesizer: { provider: lmstudio, model: gemma-3-27b }
```

Switching to Claude becomes a one-line YAML edit. The registry validates the
config at boot — fail loud on unknown adapter type or missing env var.

### 3. Strategy registry

`StrategyRegistry` maps model name patterns → tool-call strategy:

```python
@StrategyRegistry.register(pattern=r"(?i)gemma")
class GemmaToolCallStrategy: ...

@StrategyRegistry.register(pattern=r"(?i)claude")
class AnthropicToolUseStrategy: ...

@StrategyRegistry.register(pattern=r".*")  # default
class OpenAINativeStrategy: ...
```

`StrategyRegistry.for_model(model: str) -> ToolCallStrategy` picks by regex match,
default last. The `analyst` adapter consults this; `synthesizer` does not need a
tool-call strategy (it never calls tools).

### 4. Tool registry: YAML-driven (`config/tools/*.yaml`)

One file per tool. Filename stem = canonical tool name.

```yaml
# config/tools/query_health_data.yaml
name: query_health_data
description_th: "ดึงข้อมูลสุขภาพจาก MV"
description_en: "Fetch health screening aggregates from materialized views"
class_path: agents.tools.health_data:QueryHealthDataTool
enabled: true
audience: ["public", "clinician"]   # who can invoke this tool
parameters:
  type: object
  properties:
    district_code: { type: string, description: "Bangkok district code" }
    metric:        { type: string, enum: ["dm_count", "hpt_count", "cvd_count"] }
  required: ["metric"]
```

Loader semantics mirror ChartRegistry from ADR-01:
- Filesystem scan of `config/tools/*.yaml` at boot.
- Fail loud on parse / class-import / Pydantic-validation error.
- Filename stem must match `name` field.
- Duplicate names → fail.
- A tool's `class_path` is `module:Class`; the loader imports lazily.

`ToolRegistry.create_default()` is rewritten to call `ToolRegistry.discover(config_dir)`.
The legacy hardcoded list is deleted in S3 finale.

### 5. Pydantic v2 tool parameters

`BaseTool.parameters_schema: dict` is replaced by `BaseTool.Parameters: type[BaseModel]`:

```python
class QueryHealthDataParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    district_code: Optional[str] = None
    metric: Literal["dm_count", "hpt_count", "cvd_count"]

class QueryHealthDataTool(BaseTool):
    name = "query_health_data"
    Parameters = QueryHealthDataParams
    def execute(self, args: QueryHealthDataParams) -> ToolResult: ...
```

`to_openai_schema()` introspects `Parameters.model_json_schema()`. The YAML
`parameters` block (§4) becomes optional override — if present, it overrides the
auto-derived schema (escape hatch for tools whose Pydantic model can't express
something).

### 6. Conversation persistence

New table `bma_med.chat_thread`:

```sql
CREATE TABLE bma_med.chat_thread (
  thread_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  user_id      TEXT,            -- from auth cookie (S1)
  title        TEXT,
  metadata     JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE bma_med.chat_message (
  message_id   BIGSERIAL PRIMARY KEY,
  thread_id    UUID NOT NULL REFERENCES bma_med.chat_thread(thread_id) ON DELETE CASCADE,
  role         TEXT NOT NULL CHECK (role IN ('system','user','assistant','tool')),
  content      TEXT NOT NULL,
  tool_calls   JSONB,          -- on assistant
  tool_name    TEXT,           -- on role=tool
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Routes:
- `POST /api/health/chat/thread` → create thread, return `{thread_id}`.
- `GET  /api/health/chat/thread/{thread_id}` → list messages.
- `POST /api/health/chat/stream?thread_id=…&message=…` → stream + persist.
- `DELETE /api/health/chat/thread/{thread_id}` → soft-delete (sets `metadata.deleted_at`).

Server reads history from DB; no more `history` query param. Frontend just sends
`thread_id`. New conversation? Hit `POST /thread` first, save the id in
TanStack Query cache.

### 7. ChatService

Mirrors `ChartService` from ADR-01:

```python
class ChatService:
    def __init__(self, providers: ProviderRegistry, tools: ToolRegistry,
                 conv_repo: ConversationRepository, ...): ...
    async def stream(self, thread_id: UUID, user_message: str) -> AsyncGen[str]: ...
    async def chat(self, thread_id: UUID, user_message: str) -> dict: ...
    async def list_threads(self, user_id: str | None) -> list[ThreadSummary]: ...
```

Wraps the existing `OpenMultiAgent` orchestrator but adds per-thread persistence.

### 8. Wire format (SSE event types)

Event types are **stable** across providers (consumer doesn't need to know which
adapter answered):

```
event: thread_id   data: {"thread_id": "..."}
event: token       data: {"text": "..."}
event: tool_call   data: {"name": "...", "args": {...}}
event: tool_result data: {"name": "...", "summary": "...", "viz": [...]}
event: chart       data: {"spec_id": "...", "filters": {...}}    # ADR-01 chart
event: error       data: {"code": "...", "message": "..."}
event: done        data: {}
```

Frontend `useChat` hook handles events generically. Adding a new SSE event type =
update the schema doc + add a handler; no provider-side change.

---

## Consequences

### Positive
- New LLM provider = new `<Provider>Adapter` class + 1 YAML edit. Zero changes to the orchestrator, tool layer, or frontend.
- New tool = new YAML in `config/tools/` + new Tool class. Zero changes to `agents/__init__.py` or registry boot code.
- Conversations survive page refresh; users can resume threads, share by URL.
- Pydantic-typed tool params catch bad LLM tool-call args at the boundary, not deep inside `execute()`.

### Negative / accepted trade-offs
- Migration cost: 14 existing tools to convert from `parameters_schema: dict` to `Parameters: type[BaseModel]`. ~1 day.
- DB schema change: needs a migration that ships before the new chat router can write to it.
- Tighter coupling to Pydantic v2 (already a project dep).
- Strategy regex matching could be ambiguous — solve by ordering and a single default rule, like Django URL resolver.

---

## Test contract

- `tests/agents/test_provider_registry.py` — load `providers.yaml`, build each
  adapter, mock-call `health_check()`.
- `tests/agents/test_tool_registry_yaml.py` — discover `config/tools/`, expect
  N entries, each with `Parameters` model resolvable.
- `tests/services/test_chat_service.py` — round-trip a thread: create, post,
  stream, persist, list.
- `tests/agents/test_anthropic_adapter.py` — golden-record a tool-call response
  from the SDK fixture, ensure `chat()` produces the same `LLMResponse`.

---

## Out-of-scope for ADR-02

- Streaming tool execution (current model: tool runs sync after the analyst returns
  tool_calls). Postpone.
- Multi-tenancy / per-org isolation. S5+.
- Token-budget management across long threads. S5+.
- Embeddings / vector retrieval (RAG). Separate ADR if/when adopted.
