"""ReportRenderer ABC + RendererRegistry — the format strategy layer.

Per ADR-03 §4 a renderer takes a ``ReportDescriptor`` plus the list of
already-rendered ``RenderedSection``s and assembles the final artefact
(``.tex`` / ``.html`` / ``.pptx``).

Unlike ``BlockRegistry`` and ``ReportRegistry``, the renderer registry is
NOT auto-discovered from the filesystem. Concrete impls (``LaTeXRenderer``,
``HTMLRenderer``, ``PPTXRenderer``) live in ``services/reports/renderers/``
and self-register on import; ``services/reports/__init__.py:bootstrap()``
is the single entry point that imports those modules at boot once they
exist (S4.2 / S4.3).

Public surface used by the orchestrator:

    - ``ReportRenderer.render(desc, sections, ctx, out_path) -> Path``
    - ``RendererRegistry.register(renderer)``
    - ``RendererRegistry.get(fmt) -> ReportRenderer``
    - ``RendererRegistry.list_formats() -> list[str]``
    - ``renderer_registry()`` module singleton (``reset=True`` for tests)
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — only used for type hints
    from services.reports.spec import (
        RenderContext,
        RenderedSection,
        ReportDescriptor,
    )

logger = logging.getLogger("api.services.reports.renderer")


class ReportRenderer(ABC):
    """Strategy interface for one output format (latex/html/pptx).

    Subclasses MUST set ``fmt`` to the format string that matches the
    ``ReportDescriptor.formats`` Literal (``"latex"`` / ``"html"`` /
    ``"pptx"``) and implement ``render``. Concrete subclasses self-register
    with ``renderer_registry()`` at module-import time.
    """

    fmt: ClassVar[str]

    @abstractmethod
    def render(
        self,
        desc: "ReportDescriptor",
        sections: List["RenderedSection"],
        ctx: "RenderContext",
        out_path: Path,
    ) -> Path:
        """Assemble ``sections`` into the final artefact at ``out_path``.

        Returns the path of the produced artefact (which may differ from
        ``out_path`` if the concrete renderer rewrites the suffix —
        e.g. ``.tex`` → ``.pdf`` after Tectonic).
        """


class RendererRegistry:
    """In-memory ``fmt`` → ``ReportRenderer`` map.

    Renderers self-register via ``renderer_registry().register(self)`` at
    module-import time. The orchestrator looks them up by ``fmt``.

    Re-import during tests / dev is normal so ``register`` SILENTLY
    REPLACES an existing entry for the same ``fmt`` rather than raising.
    """

    def __init__(
        self,
        renderers: Optional[Dict[str, "ReportRenderer"]] = None,
    ) -> None:
        self._renderers: Dict[str, "ReportRenderer"] = (
            dict(renderers) if renderers else {}
        )

    def register(self, renderer: "ReportRenderer") -> None:
        """Register a renderer keyed by its ``fmt`` ClassVar.

        Fails loud on missing / blank ``fmt``. Replaces silently if a
        renderer for the same ``fmt`` is already registered (re-import
        during tests is normal).

        S7 alias: a renderer registered under ``latex`` is ALSO registered
        under ``pdf`` (and vice versa). The descriptor format ``latex``
        was renamed to ``pdf`` in S7; the alias keeps the old key working
        for one sprint. See ``services.reports.format_alias`` for the
        rationale.
        """
        fmt = getattr(renderer, "fmt", None)
        if not fmt or not isinstance(fmt, str):
            raise ValueError(
                f"renderer {type(renderer).__name__} has no class-level "
                f"``fmt``"
            )
        self._renderers[fmt] = renderer
        # Lazy import to avoid a top-level cycle (format_alias imports
        # nothing from this module, but keeping the imports flat lets
        # tests stub the alias map without touching renderer.py).
        from services.reports.format_alias import aliases_for

        for alias in aliases_for(fmt):
            if alias != fmt:
                self._renderers[alias] = renderer
        logger.debug("Registered renderer: %s (aliases=%s)", fmt, sorted(aliases_for(fmt)))

    def get(self, fmt: str) -> "ReportRenderer":
        """Return the renderer or raise ``KeyError`` if unknown."""
        try:
            return self._renderers[fmt]
        except KeyError as exc:
            raise KeyError(
                f"no renderer registered for fmt={fmt!r}"
            ) from exc

    def list_formats(self) -> List[str]:
        """Return all registered formats in stable sorted order."""
        return sorted(self._renderers)

    def __contains__(self, fmt: object) -> bool:
        return fmt in self._renderers

    def __len__(self) -> int:
        return len(self._renderers)


# ---------------------------------------------------------------------------
# Lazy module-level singleton. Concrete renderers register themselves on
# import (driven by ``services/reports/__init__.py:bootstrap()``).
# ---------------------------------------------------------------------------


_REGISTRY: Optional[RendererRegistry] = None


def renderer_registry(*, reset: bool = False) -> RendererRegistry:
    """Lazy singleton getter.

    Parameters
    ----------
    reset:
        If True, drop the cached registry and start fresh. Useful in
        tests that monkey-patch a custom set of renderers.
    """
    global _REGISTRY
    if _REGISTRY is None or reset:
        _REGISTRY = RendererRegistry()
    return _REGISTRY
