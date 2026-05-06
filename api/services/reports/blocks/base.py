"""ContentBlock ABC + BlockRegistry — the per-section building primitive.

Per ADR-03 §3:
    * One ``ContentBlock`` subclass = one reusable section type.
    * ``collect`` produces a format-agnostic ``data`` dict (one DB pass).
    * ``render_<fmt>`` consumes the same ``data`` and emits format-specific
      markup. Default impls raise ``NotImplementedError`` so a renderer
      can detect unsupported formats cleanly (``ContentBlock.supports``).

Discovery mirrors ChartRegistry / ToolRegistry: one ``*.yaml`` per block
in ``config/reports/blocks/``, each declaring the importable
``class_path``. ``BlockRegistry.discover()`` lazy-imports each class,
asserts subclass-of-``ContentBlock``, and registers it. Adding a new
block in 3rd-party code is a single ``BlockRegistry.register(MyBlock)``
call — no project edits needed.
"""
from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

import yaml
from pydantic import BaseModel, ConfigDict

from services.reports.spec import RenderContext

logger = logging.getLogger("api.services.reports.blocks")


# ---------------------------------------------------------------------------
# Audience targeting (S8 — "Audience-Segmented Report Sections")
# ---------------------------------------------------------------------------
#
# Distinct from ``ReportAudience`` in :mod:`services.reports.spec` which
# tags a *whole report* (public / clinician / admin / msd) — the four
# values below tag *individual blocks* by the **reader profile** they're
# calibrated for. The orchestrator filters sections at render time when
# the API is called with ``?audience=<value>``; ``None`` means "render
# in any audience" (back-compat — every block that shipped before S8
# defaults to ``None`` so existing descriptors keep working unchanged).


class AudienceTarget(str, Enum):
    """Which reader profile a block is calibrated for. Used by the
    orchestrator to filter sections when ?audience= is set."""

    PEOPLE = "people"            # ประชาชนทั่วไป
    EXECUTIVE = "executive"      # ผู้บริหาร
    CLINICIAN = "clinician"      # แพทย์/บุคลากรการแพทย์
    RESEARCHER = "researcher"    # นักวิจัย / population-based study


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class ContentBlock(ABC):
    """One reusable report section type — see ADR-03 §3.

    Subclasses MUST:
        * declare a unique ``block_id`` ClassVar (registry key);
        * declare a ``Parameters`` Pydantic model class — even if it has
          no fields — so the orchestrator can validate ``SectionSpec.params``
          uniformly;
        * implement ``collect``;
        * override at least one of ``render_latex`` / ``render_html`` /
          ``render_pptx``.

    The default ``render_*`` methods raise ``NotImplementedError`` with a
    self-describing message; ``supports(fmt)`` lets the orchestrator
    discover which formats a block actually implements without having to
    catch exceptions.
    """

    block_id: ClassVar[str]
    Parameters: ClassVar[type[BaseModel]] = BaseModel

    # S8: which reader profile this block targets. ``None`` = "any
    # audience" (back-compat default for blocks that shipped before
    # audience filtering existed). The orchestrator consults this only
    # when ``?audience=<value>`` is passed; otherwise it's a no-op.
    audience_target: ClassVar[Optional[AudienceTarget]] = None

    @abstractmethod
    async def collect(
        self, ctx: RenderContext, params: BaseModel
    ) -> Dict[str, Any]:
        """Compute the format-agnostic data payload for this section.

        Runs once per (report_id, lang) — both renderers reuse the dict.
        Block code should query ``ctx.data_collector`` here, never the
        DB directly (ADR-03 §7).

        ``collect`` is declared ``async`` even on blocks whose data path is
        synchronous: chart / table blocks call into the async ``ChartService``
        / ``MVRepository`` and the orchestrator awaits one common coroutine
        contract instead of branching on the block kind. Sync blocks simply
        ``return`` from a body with no ``await``.
        """

    # ------------------------------------------------------------------
    # Default render impls — fail loud with a clear message. Subclasses
    # that support a format MUST override the corresponding method.
    # ------------------------------------------------------------------

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        raise NotImplementedError(
            f"Block {self.block_id!r} does not support format 'latex'"
        )

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        raise NotImplementedError(
            f"Block {self.block_id!r} does not support format 'html'"
        )

    def render_pptx(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> Dict[str, Any]:
        raise NotImplementedError(
            f"Block {self.block_id!r} does not support format 'pptx'"
        )

    # ------------------------------------------------------------------
    # Capability introspection — used by the orchestrator + the descriptor
    # registry to surface "block X has no HTML output, hide section in
    # the HTML build" without exception-driven control flow.
    # ------------------------------------------------------------------

    @classmethod
    def supports(cls, fmt: str) -> bool:
        """True iff this subclass overrides ``render_<fmt>``.

        Implementation note: we compare the resolved attribute on the
        subclass to the one on ``ContentBlock``. If they differ, the
        subclass overrode the method, which means it has a real impl
        for that format.
        """
        method_name = f"render_{fmt}"
        own = getattr(cls, method_name, None)
        if own is None:
            return False
        base = getattr(ContentBlock, method_name, None)
        return own is not base


# ---------------------------------------------------------------------------
# Per-block YAML wire schema
# ---------------------------------------------------------------------------


class BlockYaml(BaseModel):
    """Wire format for ``config/reports/blocks/<block_id>.yaml``.

    ``class_path`` is the dotted ``module.path:ClassName`` reference the
    registry lazy-imports. Mirrors ``ToolSpec.class_path`` from ADR-02 §4.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    block_id: str
    class_path: str
    description_th: str
    description_en: Optional[str] = None
    enabled: bool = True


def _import_block_class(class_path: str) -> type[ContentBlock]:
    """Resolve ``"module.path:ClassName"`` to a ``ContentBlock`` subclass.

    Fails loud on:
        * malformed ``class_path`` (no ``:`` separator)
        * import error
        * missing attribute on the module
        * attribute is not a subclass of ``ContentBlock``
    """
    if ":" not in class_path:
        raise ValueError(
            f"block class_path must be 'module.path:ClassName', "
            f"got {class_path!r}"
        )
    module_name, _, attr = class_path.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ImportError(
            f"failed to import block module {module_name!r}: {exc}"
        ) from exc
    try:
        cls = getattr(module, attr)
    except AttributeError as exc:
        raise AttributeError(
            f"module {module_name!r} has no attribute {attr!r}"
        ) from exc
    if not isinstance(cls, type) or not issubclass(cls, ContentBlock):
        raise TypeError(
            f"{class_path} resolves to {cls!r} which is not a "
            f"ContentBlock subclass"
        )
    return cls


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class BlockRegistry:
    """In-memory map of block_id → ``ContentBlock`` subclass.

    Two ways to populate:
        * Code: ``BlockRegistry.register(MyBlock)`` (decorator-friendly).
        * Filesystem: ``BlockRegistry.discover(config_dir)`` scans
          ``*.yaml`` files (one per block) and lazy-imports each class.
    """

    def __init__(
        self,
        blocks: Optional[Dict[str, type[ContentBlock]]] = None,
    ) -> None:
        self._blocks: Dict[str, type[ContentBlock]] = (
            dict(blocks) if blocks else {}
        )

    # ------------------------------------------------------------------
    # Registration — decorator-friendly so 3rd-party code can ``@register``
    # ------------------------------------------------------------------

    def register(
        self, block_cls: type[ContentBlock]
    ) -> type[ContentBlock]:
        """Register a ``ContentBlock`` subclass keyed by its ``block_id``."""
        if not isinstance(block_cls, type) or not issubclass(
            block_cls, ContentBlock
        ):
            raise TypeError(
                f"register expected a ContentBlock subclass, got {block_cls!r}"
            )
        block_id = getattr(block_cls, "block_id", None)
        if not block_id or not isinstance(block_id, str):
            raise ValueError(
                f"block class {block_cls.__name__} must declare a non-empty "
                f"``block_id`` ClassVar"
            )
        if block_id in self._blocks:
            raise ValueError(f"duplicate block_id: {block_id!r}")
        self._blocks[block_id] = block_cls
        logger.debug("Registered content block: %s", block_id)
        return block_cls

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @classmethod
    def discover(cls, config_dir: Path) -> "BlockRegistry":
        """Scan ``config_dir`` for ``*.yaml`` and load every enabled block.

        Fails loud on:
            * missing directory
            * YAML parse error
            * Pydantic validation error
            * filename-stem ≠ block_id mismatch
            * duplicate block_id
            * bad ``class_path`` (unknown module / attribute / not
              ContentBlock subclass)
        """
        config_dir = Path(config_dir)
        if not config_dir.is_dir():
            raise FileNotFoundError(
                f"block config_dir does not exist or is not a directory: "
                f"{config_dir}"
            )

        registry = cls()
        for path in sorted(config_dir.glob("*.yaml")):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh)
            except yaml.YAMLError as exc:
                raise ValueError(
                    f"failed to parse YAML for block spec {path}: {exc}"
                ) from exc

            if not isinstance(raw, dict):
                raise ValueError(
                    f"block spec {path} must be a YAML mapping, got "
                    f"{type(raw).__name__}"
                )

            spec = BlockYaml(**raw)  # raises ValidationError on bad input
            stem = path.stem
            if spec.block_id != stem:
                raise ValueError(
                    f"block spec {path}: filename stem {stem!r} does not "
                    f"match block_id {spec.block_id!r}"
                )
            if not spec.enabled:
                logger.info(
                    "Skipping disabled block: %s (%s)",
                    spec.block_id,
                    path.name,
                )
                continue
            block_cls = _import_block_class(spec.class_path)
            # block_id sanity — class declaration must agree with YAML.
            class_block_id = getattr(block_cls, "block_id", None)
            if class_block_id != spec.block_id:
                raise ValueError(
                    f"block spec {path}: class_path {spec.class_path!r} "
                    f"declares block_id={class_block_id!r} but YAML says "
                    f"{spec.block_id!r}"
                )
            registry.register(block_cls)

        logger.info(
            "BlockRegistry.discover: loaded %d block(s) from %s",
            len(registry._blocks),
            config_dir,
        )
        return registry

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get(self, block_id: str) -> type[ContentBlock]:
        """Return the block class or raise ``KeyError`` if unknown."""
        try:
            return self._blocks[block_id]
        except KeyError as exc:
            raise KeyError(f"unknown block_id: {block_id!r}") from exc

    def list_ids(self) -> List[str]:
        """Return all known block ids in stable sorted order."""
        return sorted(self._blocks)

    def __contains__(self, block_id: object) -> bool:
        return block_id in self._blocks

    def __len__(self) -> int:
        return len(self._blocks)


# ---------------------------------------------------------------------------
# Lazy module-level singleton — same idiom as ``chart_registry()``.
# ---------------------------------------------------------------------------


_REGISTRY: Optional[BlockRegistry] = None


def _default_config_dir() -> Path:
    """Default config dir: ``<repo>/config/reports/blocks/``.

    Four levels up from this module
    (``api/services/reports/blocks/base.py``) lands on the repo root.
    """
    return (
        Path(__file__).resolve().parents[4]
        / "config"
        / "reports"
        / "blocks"
    )


def block_registry(
    config_dir: Optional[Path] = None,
    *,
    reload: bool = False,
) -> BlockRegistry:
    """Lazy singleton getter — mirrors ``chart_registry()``.

    Parameters
    ----------
    config_dir:
        Override the default ``<repo>/config/reports/blocks/`` directory.
        Mostly useful for tests.
    reload:
        If True, rebuild the registry from disk even if a cached copy
        exists. Hot-reload is NOT a production feature — this flag is
        for tests / dev only.
    """
    global _REGISTRY
    if _REGISTRY is None or reload:
        _REGISTRY = BlockRegistry.discover(
            config_dir or _default_config_dir()
        )
    return _REGISTRY
