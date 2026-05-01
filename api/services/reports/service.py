"""ReportService — orchestrator for descriptor-driven report rendering.

Per ADR-03 §5, this service is the single entry point that:
    1. Looks the descriptor up by ``report_id``.
    2. Optionally substitutes parameterised placeholders (S4.4 needs this
       for the per-zone descriptor flow — every string field of the
       descriptor goes through ``str.format(**params)``).
    3. Validates the requested ``fmt`` and ``lang`` against descriptor.
    4. Resolves the renderer for that format from ``RendererRegistry``.
    5. Honours the descriptor's ``cache`` policy (data-hash sidecar).
    6. Runs each section's ``ContentBlock`` end-to-end:
       * looks the class up in ``BlockRegistry``,
       * parses ``params`` against ``block.Parameters``,
       * calls ``block.collect(ctx, params)`` once,
       * calls ``block.render_<fmt>(data, params, ctx)``,
       * skips sections whose ``visible_in`` excludes ``fmt``.
    7. Hands the assembled list to the renderer.
    8. Writes a data-hash sidecar so subsequent renders can short-circuit.

Async surface
-------------
The render pipeline is naturally synchronous (Tectonic is a blocking
subprocess; data collection is a sync DB call). The public methods are
``async def`` and use ``asyncio.to_thread`` for the blocking work so a
FastAPI handler can await without holding the event loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from pydantic import BaseModel

import config
from services.reports.blocks import BlockRegistry, ContentBlock, block_registry
from services.reports.data_collector import ReportDataCollector
from services.reports.registry import ReportRegistry, report_registry
from services.reports.renderer import (
    RendererRegistry,
    ReportRenderer,
    renderer_registry,
)
from services.reports.spec import (
    RenderContext,
    RenderedSection,
    ReportDescriptor,
    SectionSpec,
)

logger = logging.getLogger("api.services.reports.service")


# ---------------------------------------------------------------------------
# Cache layout — kept in sync with ``LaTeXRenderer.cache_path``.
#
#   <reports_dir>/<lang>/<report_id>.<ext>          rendered artefact
#   <reports_dir>/<lang>/<report_id>.hash           data hash for invalidation
#
# ``ext`` is ``.pdf`` for LaTeX, ``.html`` for HTML, ``.pptx`` for PPTX.
# The orchestrator writes the .hash sidecar AFTER the renderer succeeds,
# so a partial render never produces a stale-hash hit on the next call.
# ---------------------------------------------------------------------------

_FMT_EXT = {
    "latex": ".pdf",
    "html": ".html",
    "pptx": ".pptx",
}


# ---------------------------------------------------------------------------
# Param substitution helpers — recursively walk a JSON-shaped tree and run
# ``str.format(**params)`` on every string leaf. Needed for the per-zone
# descriptor flow (S4.4) where ``zone_code`` is supplied at render time.
# ---------------------------------------------------------------------------


def _substitute(node: Any, params: Mapping[str, Any]) -> Any:
    """Recursively format string leaves in ``node`` against ``params``."""
    if not params:
        return node
    if isinstance(node, str):
        # Only call format() if the string actually contains a placeholder
        # — avoids surprises from stray '{' in copy-pasted Thai text.
        if "{" not in node:
            return node
        return node.format(**params)
    if isinstance(node, dict):
        return {k: _substitute(v, params) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, params) for v in node]
    return node


def _resolve_descriptor(
    descriptor: ReportDescriptor,
    params: Optional[Mapping[str, Any]],
) -> ReportDescriptor:
    """Deep-copy the descriptor with all string fields substituted.

    Pydantic models go through ``model_dump`` -> mutate -> re-validate so
    ``model_config(extra="forbid")`` still protects us from typos.
    """
    if not params:
        return descriptor
    raw = descriptor.model_dump()
    resolved = _substitute(raw, params)
    return ReportDescriptor.model_validate(resolved)


# ---------------------------------------------------------------------------
# ReportService
# ---------------------------------------------------------------------------


class ReportService:
    """Descriptor-driven orchestrator. See module docstring for the flow."""

    def __init__(
        self,
        descriptors: Optional[ReportRegistry] = None,
        blocks: Optional[BlockRegistry] = None,
        renderers: Optional[RendererRegistry] = None,
        data: Optional[Any] = None,
        *,
        out_dir: Optional[Path] = None,
        # ----- backward-compat kwargs (S4.1 transitional surface) -------
        registry: Optional[ReportRegistry] = None,
        data_collector: Optional[Any] = None,
    ) -> None:
        # ``registry`` / ``data_collector`` are S4.1's keyword names.
        # ``descriptors`` / ``data`` are this prompt's spec. Accept both
        # so tests written against either surface keep working until the
        # S4.5 cleanup ticket consolidates them.
        descriptors = descriptors if descriptors is not None else registry
        data = data if data is not None else data_collector
        if descriptors is None:
            raise TypeError(
                "ReportService.__init__ requires `descriptors` (or alias `registry`)"
            )
        if blocks is None:
            raise TypeError("ReportService.__init__ requires `blocks`")
        if renderers is None:
            raise TypeError("ReportService.__init__ requires `renderers`")
        if data is None:
            raise TypeError(
                "ReportService.__init__ requires `data` (or alias `data_collector`)"
            )
        self._descriptors = descriptors
        self._blocks = blocks
        self._renderers = renderers
        self._data = data
        self._out_dir = Path(out_dir) if out_dir else Path(config.REPORTS_DIR)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def render(
        self,
        report_id: str,
        fmt: str,
        lang: str = "th",
        *,
        params: Optional[Mapping[str, Any]] = None,
        out_path: Optional[Path] = None,
    ) -> Path:
        """Full render pipeline. Returns path to the produced artefact.

        ``params`` is the optional parameterised-descriptor input — each
        ``{placeholder}`` in any string field of the descriptor is
        substituted via ``str.format(**params)`` before block dispatch.
        Used by the per-zone flow in S4.4. Defaults to ``None`` (no sub).

        ``out_path`` overrides the default ``<out_dir>/<lang>/<report_id>.<ext>``
        cache path. Mostly useful for tests / one-off renders that don't
        want to thrash the shared cache directory. When supplied, the
        cache short-circuit is bypassed and no .hash sidecar is written.
        """
        desc = self._descriptors.get(report_id)
        desc = _resolve_descriptor(desc, params)
        self._validate_request(desc, fmt, lang)

        try:
            renderer = self._renderers.get(fmt)
        except KeyError as exc:
            # Bubble as KeyError; the FastAPI router maps to 404.
            raise KeyError(
                f"no renderer registered for format {fmt!r} "
                f"(needed by report {report_id!r})"
            ) from exc

        # When the caller supplies ``out_path`` they're opting out of the
        # shared cache layout (tests / one-off renders) so we skip the
        # hash sidecar dance entirely on that branch.
        explicit_out_path = out_path is not None
        if not explicit_out_path:
            out_path = self._cache_path(report_id, fmt, lang)

        # ------------------------------------------------------------
        # Cache check — short-circuit if we have a fresh artefact whose
        # data-hash matches the live DB. Both files must be present and
        # the hash must agree; otherwise we re-render. Parameterised or
        # explicit-out_path renders disable cache (a {zone_code} change
        # should produce a different cache key, which we'll add in S4.4).
        # ------------------------------------------------------------
        if (
            desc.cache.enabled
            and not params
            and not explicit_out_path
            and self._is_cache_fresh(out_path)
        ):
            logger.info(
                "Report cache hit: %s/%s/%s -> %s",
                report_id, fmt, lang, out_path,
            )
            return out_path

        # ------------------------------------------------------------
        # Build the render context. The data_collector is shared across
        # every block in this render so collect() runs once per blocks
        # that need data — actual de-dup is the collector's job.
        # ------------------------------------------------------------
        ctx = RenderContext(
            data_collector=self._data,
            lang=lang,
            fmt=fmt,
            descriptor=desc,
            requested_at=datetime.now(timezone.utc),
        )

        sections = await self._build_sections(desc, ctx)

        # ------------------------------------------------------------
        # Hand off to the renderer. Tectonic / file I/O is sync, so we
        # offload to a worker thread to keep the event loop free.
        # ------------------------------------------------------------
        out_path.parent.mkdir(parents=True, exist_ok=True)
        produced = await asyncio.to_thread(
            renderer.render, desc, sections, ctx, out_path
        )
        produced = Path(produced)

        # ------------------------------------------------------------
        # Cache hash sidecar — only written after a successful render so
        # a partial Tectonic output (legacy compile_clean=False path) does
        # NOT leave a fresh-looking hash on disk. Parameterised renders
        # skip the sidecar (see cache check above).
        # ------------------------------------------------------------
        if desc.cache.enabled and not params and not explicit_out_path:
            try:
                self._write_hash_sidecar(produced, self._data.data_hash())
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "ReportService: failed to write hash sidecar: %s", exc
                )

        logger.info(
            "Report rendered: %s/%s/%s -> %s",
            report_id, fmt, lang, produced,
        )
        return produced

    async def list(self) -> List[Dict[str, Any]]:
        """Catalog every registered descriptor, FE-friendly shape."""
        catalog: List[Dict[str, Any]] = []
        for report_id in self._descriptors.list_ids():
            desc = self._descriptors.get(report_id)
            catalog.append(
                {
                    "report_id": desc.report_id,
                    "title_th": desc.title_th,
                    "title_en": desc.title_en,
                    "formats": list(desc.formats),
                    "languages": list(desc.languages),
                    "audience": list(desc.audience),
                }
            )
        return catalog

    async def describe(self, report_id: str) -> ReportDescriptor:
        """Return the raw descriptor — useful for the ``/spec`` debug route."""
        return self._descriptors.get(report_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_request(
        desc: ReportDescriptor, fmt: str, lang: str
    ) -> None:
        if fmt not in desc.formats:
            raise ValueError(
                f"report {desc.report_id!r} does not declare format "
                f"{fmt!r}; declared: {list(desc.formats)}"
            )
        if lang not in desc.languages:
            raise ValueError(
                f"report {desc.report_id!r} does not declare language "
                f"{lang!r}; declared: {list(desc.languages)}"
            )

    def _cache_path(self, report_id: str, fmt: str, lang: str) -> Path:
        """Cache layout: ``<out_dir>/<lang>/<report_id>.<ext>``.

        Lang is part of the path because translations differ — same
        descriptor + same fmt + different lang = different artefact.
        """
        ext = _FMT_EXT.get(fmt, f".{fmt}")
        return self._out_dir / lang / f"{report_id}{ext}"

    def _is_cache_fresh(self, out_path: Path) -> bool:
        """True iff the artefact + matching hash sidecar both exist."""
        if not out_path.exists():
            return False
        sidecar = out_path.with_suffix(".hash")
        if not sidecar.exists():
            return False
        try:
            cached_hash = sidecar.read_text(encoding="utf-8").strip()
            current_hash = self._data.data_hash()
        except Exception as exc:
            logger.warning("ReportService: hash sidecar read failed: %s", exc)
            return False
        return bool(cached_hash) and cached_hash == current_hash

    def _write_hash_sidecar(self, out_path: Path, data_hash: str) -> None:
        """Atomic write of the data-hash sidecar next to the artefact."""
        if not data_hash:
            logger.debug(
                "ReportService: empty data_hash; skipping sidecar write"
            )
            return
        sidecar = out_path.with_suffix(".hash")
        tmp = sidecar.with_suffix(".hash.tmp")
        tmp.write_text(data_hash, encoding="utf-8")
        tmp.replace(sidecar)

    async def _build_sections(
        self,
        desc: ReportDescriptor,
        ctx: RenderContext,
    ) -> List[RenderedSection]:
        """Resolve + run each section sequentially.

        Sections are sequential because:
            * Block ``collect()`` shares one DB pass via the collector
              already — running them in parallel buys nothing.
            * Some blocks may depend on prior side effects in
              ``ctx.extra`` (ADR-03 leaves this open). Sequential keeps
              the contract simple.

        ``collect()`` is async (chart/table blocks await ChartService /
        MVRepository); ``render_<fmt>()`` is sync but CPU-bound, so it
        runs in a worker thread to keep the event loop free.
        """
        rendered: List[RenderedSection] = []
        for section in desc.sections:
            if not self._section_visible(section, ctx.fmt):
                logger.debug(
                    "ReportService: skipping section %s (visible_in=%s, fmt=%s)",
                    section.id, section.visible_in, ctx.fmt,
                )
                continue
            rendered.append(await self._render_section(section, ctx))
        return rendered

    @staticmethod
    def _section_visible(section: SectionSpec, fmt: str) -> bool:
        """Honour ``section.visible_in`` per ADR-03 §2."""
        if section.visible_in is None:
            return True
        return fmt in section.visible_in

    async def _render_section(
        self,
        section: SectionSpec,
        ctx: RenderContext,
    ) -> RenderedSection:
        """Resolve the block class, parse params, await collect, render."""
        block_cls = self._blocks.get(section.block)
        block: ContentBlock = block_cls()

        # Parameters — every ContentBlock subclass MUST declare a
        # Parameters Pydantic model (per ADR-03 §3 + the ABC default).
        params_cls = getattr(block_cls, "Parameters", BaseModel)
        try:
            params_model = params_cls(**section.params)
        except Exception as exc:
            raise ValueError(
                f"section {section.id!r} (block={section.block!r}): "
                f"invalid params: {exc}"
            ) from exc

        # Format-specific render method, e.g. render_latex / render_html.
        render_method_name = f"render_{ctx.fmt}"
        render_method = getattr(block, render_method_name, None)
        if render_method is None:
            raise ValueError(
                f"block {section.block!r} has no method {render_method_name!r}"
            )

        # collect() is async on the ABC; tolerate sync overrides too so
        # legacy / test blocks that haven't been migrated still work.
        try:
            collect_result = block.collect(ctx, params_model)
            if hasattr(collect_result, "__await__"):
                data = await collect_result  # type: ignore[misc]
            else:
                data = collect_result  # sync override
        except Exception as exc:
            raise RuntimeError(
                f"section {section.id!r}: block {section.block!r} "
                f"collect() failed: {exc}"
            ) from exc

        try:
            # render_<fmt> is sync + CPU-bound — push to a thread so we
            # don't block the event loop on Jinja2 template work.
            markup = await asyncio.to_thread(
                render_method, data, params_model, ctx
            )
        except NotImplementedError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"section {section.id!r}: block {section.block!r} "
                f"render_{ctx.fmt}() failed: {exc}"
            ) from exc

        return RenderedSection(
            section_id=section.id,
            block_id=section.block,
            markup=markup,
            data=data,
            params=params_model,
        )


# ---------------------------------------------------------------------------
# DI factory — lazy singleton. Mirrors the chart/tool service factories.
# ---------------------------------------------------------------------------
_SERVICE: Optional[ReportService] = None


def get_report_service() -> ReportService:
    """Return the process-wide :class:`ReportService` singleton.

    Construction is lazy so importing this module doesn't trigger
    descriptor / block discovery (which reads YAML files from disk and
    would be a startup-order hazard during tests).

    Tests bypass this factory entirely — they construct ``ReportService``
    directly with hand-built fake registries.
    """
    global _SERVICE
    if _SERVICE is None:
        # Importing the renderers package triggers each concrete renderer
        # to self-register. This is the bootstrap point.
        import services.reports.renderers.latex  # noqa: F401

        _SERVICE = ReportService(
            descriptors=report_registry(),
            blocks=block_registry(),
            renderers=renderer_registry(),
            data=ReportDataCollector(),
        )
    return _SERVICE


__all__ = ["ReportService", "get_report_service"]
