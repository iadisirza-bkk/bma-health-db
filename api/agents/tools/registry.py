"""Tool registry — register, list, and execute tools by name.

Per ADR-02 §4 the registry is **YAML-driven**: each tool lives at
``config/tools/<name>.yaml`` and is discovered at boot. Adding a new tool
no longer requires editing ``create_default()`` — just drop a YAML file
plus the Tool class.

``ToolRegistry.create_default()`` is kept as a thin, deprecated wrapper
so existing callers (`agents.__init__.create_orchestrator`,
`routers/chat.py`) keep working until S3 finale removes it.

Tools are SYNC in this project. The registry provides both sync and
async execution (async wraps sync via ``asyncio.to_thread``).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

import yaml

from agents.tools.base import BaseTool, ToolResult
from agents.tools.spec import ToolSpec, import_tool_class

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    # ------------------------------------------------------------------
    # Public API — same shape as before so callers don't break.
    # ------------------------------------------------------------------

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name!r}")
        self._tools[tool.name] = tool
        logger.debug("Registered tool: %s", tool.name)

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def to_openai_schemas(self) -> list[dict[str, Any]]:
        return [t.to_openai_schema() for t in self._tools.values()]

    def to_filtered_schemas(self, tool_names: list[str]) -> list[dict[str, Any]]:
        """Return schemas for only the specified tools."""
        return [t.to_openai_schema() for t in self._tools.values() if t.name in tool_names]

    def execute_sync(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Execute a tool synchronously."""
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(text=f"Unknown tool: {name}")
        logger.info("Executing tool: %s(%s)", name, args)
        try:
            return tool.execute(args)
        except Exception as e:
            logger.exception("Tool '%s' failed: %s", name, e)
            return ToolResult(text=f"เครื่องมือ {name} เกิดข้อผิดพลาด กรุณาลองใหม่")

    async def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Execute a tool from async context (wraps sync in thread)."""
        return await asyncio.to_thread(self.execute_sync, name, args)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @classmethod
    def discover(cls, config_dir: Path) -> "ToolRegistry":
        """Scan ``config_dir`` for ``*.yaml`` and load every enabled tool.

        Fails loud on:
            - missing directory
            - YAML parse error
            - Pydantic validation error
            - filename-stem ≠ name mismatch
            - duplicate tool name
            - bad ``class_path`` (unknown module / attribute / not BaseTool)
        """
        config_dir = Path(config_dir)
        if not config_dir.is_dir():
            raise FileNotFoundError(
                f"tool config_dir does not exist or is not a directory: {config_dir}"
            )

        registry = cls()
        yaml_paths = sorted(config_dir.glob("*.yaml"))
        for path in yaml_paths:
            try:
                with path.open("r", encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh)
            except yaml.YAMLError as exc:
                raise ValueError(
                    f"failed to parse YAML for tool spec {path}: {exc}"
                ) from exc

            if not isinstance(raw, dict):
                raise ValueError(
                    f"tool spec {path} must be a YAML mapping, got "
                    f"{type(raw).__name__}"
                )

            spec = ToolSpec(**raw)  # raises ValidationError on bad input

            stem = path.stem
            if spec.name != stem:
                raise ValueError(
                    f"tool spec {path}: filename stem {stem!r} does not "
                    f"match name {spec.name!r}"
                )

            if not spec.enabled:
                logger.info("Skipping disabled tool: %s (%s)", spec.name, path.name)
                continue

            tool_cls = import_tool_class(spec.class_path)
            tool = tool_cls()  # all current tools have no-arg constructors

            # Stamp YAML metadata onto the instance so to_openai_schema()
            # and audience-aware filters can read it (S3.4 will switch
            # to_openai_schema to consult these directly).
            tool.description_th = spec.description_th  # type: ignore[attr-defined]
            tool.description_en = spec.description_en  # type: ignore[attr-defined]
            tool.audience = list(spec.audience)        # type: ignore[attr-defined]

            # YAML override > Pydantic Parameters model > legacy schema dict.
            if spec.parameters is not None:
                tool.parameters_schema = spec.parameters
            else:
                params_model = getattr(tool_cls, "Parameters", None)
                if params_model is not None and hasattr(params_model, "model_json_schema"):
                    tool.parameters_schema = params_model.model_json_schema()
                # else: keep whatever the class declared on parameters_schema.

            # Description on the LLM-facing schema is the Thai text — keep
            # the legacy ``description`` attribute in sync so existing
            # to_openai_schema() output is unchanged.
            tool.description = spec.description_th

            registry.register(tool)

        logger.info(
            "ToolRegistry.discover: loaded %d tool(s) from %s",
            len(registry._tools),
            config_dir,
        )
        return registry

    # ------------------------------------------------------------------
    # Backward-compat wrapper — slated for removal in S3 finale.
    # ------------------------------------------------------------------

    @classmethod
    def create_default(cls) -> "ToolRegistry":
        """Discover tools from the default ``config/tools/`` directory.

        DEPRECATED — call ``ToolRegistry.discover`` (or the
        ``tool_registry()`` singleton) directly. Kept so legacy callers
        in ``agents.__init__`` and the chat router keep working.
        """
        logger.debug(
            "ToolRegistry.create_default() is deprecated; "
            "use ToolRegistry.discover() or tool_registry() instead."
        )
        return cls.discover(_default_config_dir())


# ---------------------------------------------------------------------------
# Lazy module-level singleton — mirrors ``chart_registry()`` from ADR-01.
# ---------------------------------------------------------------------------

_REGISTRY: Optional[ToolRegistry] = None


def _default_config_dir() -> Path:
    """Default config dir is ``<repo>/config/tools/`` — three levels up
    from this module (``api/agents/tools/registry.py``)."""
    return Path(__file__).resolve().parents[3] / "config" / "tools"


def tool_registry(
    config_dir: Optional[Path] = None,
    *,
    reload: bool = False,
) -> ToolRegistry:
    """Lazy singleton getter.

    Parameters
    ----------
    config_dir:
        Override the default ``<repo>/config/tools/`` directory. Mostly
        useful for tests.
    reload:
        If True, rebuild the registry from disk even if a cached copy
        exists. Hot-reload is NOT a production feature — this flag is
        for tests / dev only.
    """
    global _REGISTRY
    if _REGISTRY is None or reload:
        _REGISTRY = ToolRegistry.discover(config_dir or _default_config_dir())
    return _REGISTRY
