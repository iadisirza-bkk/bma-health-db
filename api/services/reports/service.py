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
from services.reports.format_alias import (
    canonicalize as _canonicalize_fmt,
    format_matches as _format_matches,
    warn_if_legacy as _warn_legacy_fmt,
)
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

# Observability — metrics module exposes no-op stubs when prometheus_client
# is missing, so this import is safe in any environment.
try:
    from observability.metrics import (
        report_cache_hit,
        report_render_duration,
        report_render_total,
        track_duration,
    )
except Exception:  # pragma: no cover - defensive
    report_cache_hit = report_render_duration = report_render_total = None  # type: ignore[assignment]

    from contextlib import contextmanager
    from typing import Iterator as _Iterator

    @contextmanager
    def track_duration(
        histogram: Any, labels: Any = None
    ) -> "_Iterator[None]":
        yield

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
    # ``latex`` and ``pdf`` both produce the same .pdf artefact — see
    # ``services.reports.format_alias`` for the rename rationale.
    "latex": ".pdf",
    "pdf": ".pdf",
    "html": ".html",
    "pptx": ".pptx",
}


# ---------------------------------------------------------------------------
# Param substitution helpers — recursively walk a JSON-shaped tree and run
# ``str.format(**params)`` on every string leaf. Needed for the per-zone
# descriptor flow (S4.4) where ``zone_code`` is supplied at render time.
# ---------------------------------------------------------------------------


class _SoftFormatter(__import__("string").Formatter):
    """``str.format``-style formatter that leaves unknown placeholders intact.

    Descriptors mix two substitution layers: descriptor-level params
    (``{zone_code}`` resolved here) and block-level data references
    (``{zone.total_screened}`` resolved later by ``ParagraphBlock``).
    A plain ``str.format`` raises on the second flavour because the root
    name (``zone``) isn't in ``params``. This subclass intercepts the
    miss and re-emits ``{path.with.dots}`` so the block sees the original
    placeholder.
    """

    def get_field(self, field_name, args, kwargs):  # type: ignore[override]
        try:
            return super().get_field(field_name, args, kwargs)
        except (KeyError, IndexError, AttributeError):
            return "{" + field_name + "}", field_name


_SOFT_FMT = _SoftFormatter()


def _substitute(node: Any, params: Mapping[str, Any]) -> Any:
    """Recursively format string leaves in ``node`` against ``params``.

    Unknown placeholders pass through unchanged (see ``_SoftFormatter``)
    so block-level substitution (``ParagraphBlock``, etc.) still gets a
    chance to resolve ``{path.like.this}`` against the data collector.
    """
    if not params:
        return node
    if isinstance(node, str):
        # Only call format() if the string actually contains a placeholder
        # — avoids surprises from stray '{' in copy-pasted Thai text.
        if "{" not in node:
            return node
        return _SOFT_FMT.vformat(node, (), dict(params))
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
        audience: Optional[set[str]] = None,
        feature_flags: Optional[Mapping[str, Any]] = None,
        polish_service: Optional[Any] = None,
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

        ``audience`` (S8) is the set of requested
        :class:`AudienceTarget` values (string form). When provided, the
        orchestrator drops every section whose underlying block declares
        an ``audience_target`` that is not in the set. Audience-agnostic
        blocks (``audience_target = None``) ALWAYS render. The default
        ``None`` means "render every section" (status quo).

        ``feature_flags`` (S9) is forwarded to ``RenderContext`` so blocks
        can opt into experimental behaviour (e.g. ``polish_prose``). The
        flag dict is propagated as-is — unknown keys are silently
        ignored by blocks that don't care.

        ``polish_service`` (S9) is the optional ``TextPolishService``
        handle wired up by the router when ``?polish=1``. Defaults to
        ``None`` (no polish).
        """
        # Wrap the full render in a duration histogram + status counter.
        # Cache short-circuits also count as "ok" — they're a successful
        # outcome from the caller's POV.
        with track_duration(report_render_duration, {"report_id": report_id, "fmt": fmt}):
            try:
                produced = await self._render_inner(
                    report_id, fmt, lang,
                    params=params, out_path=out_path,
                    audience=audience,
                    feature_flags=feature_flags,
                    polish_service=polish_service,
                )
            except Exception:
                if report_render_total is not None:
                    report_render_total.labels(
                        report_id=report_id, fmt=fmt, lang=lang, status="error"
                    ).inc()
                raise
            if report_render_total is not None:
                report_render_total.labels(
                    report_id=report_id, fmt=fmt, lang=lang, status="ok"
                ).inc()
            return produced

    async def _render_inner(
        self,
        report_id: str,
        fmt: str,
        lang: str = "th",
        *,
        params: Optional[Mapping[str, Any]] = None,
        out_path: Optional[Path] = None,
        audience: Optional[set[str]] = None,
        feature_flags: Optional[Mapping[str, Any]] = None,
        polish_service: Optional[Any] = None,
    ) -> Path:
        """Original render pipeline. Split out so the public ``render``
        wrapper can attach metrics without indenting the entire body.
        """
        desc = self._descriptors.get(report_id)
        desc = _resolve_descriptor(desc, params)
        self._validate_request(desc, fmt, lang)
        # S8: filter by audience_target BEFORE we hit the renderer. Done
        # on a fresh copy of ``desc.sections`` so the registry's stored
        # descriptor (returned by ``describe()``) is never mutated. The
        # descriptor's other fields stay intact — only the section list
        # is shortened.
        if audience is not None:
            desc = self._filter_sections_by_audience(desc, audience)

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
        assert out_path is not None  # narrowed by branch above

        # ------------------------------------------------------------
        # Cache check — short-circuit if we have a fresh artefact whose
        # data-hash matches the live DB. Both files must be present and
        # the hash must agree; otherwise we re-render. Parameterised,
        # audience-filtered, or explicit-out_path renders disable cache
        # (different audience set = different output file but the same
        # cache key — bypass instead of risking a stale hit).
        # ------------------------------------------------------------
        if (
            desc.cache.enabled
            and not params
            and not explicit_out_path
            and audience is None
            and self._is_cache_fresh(out_path)
        ):
            logger.info(
                "Report cache hit: %s/%s/%s -> %s",
                report_id, fmt, lang, out_path,
            )
            if report_cache_hit is not None:
                try:
                    report_cache_hit.labels(
                        report_id=report_id, fmt=fmt, lang=lang
                    ).inc()
                except Exception:
                    logger.debug("report_cache_hit inc failed", exc_info=True)
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
            feature_flags=dict(feature_flags or {}),
            polish_service=polish_service,
        )
        # ADR-03 S6 addendum: container blocks (e.g. ``two_column_layout``)
        # need a back-reference to the orchestrator so they can resolve
        # their own nested children through the same render pipeline.
        # Stash it in ``ctx.extra`` so the public ``RenderContext`` shape
        # stays stable for non-container blocks that don't care.
        ctx.extra["report_service"] = self

        sections = await self._render_sections(desc.sections, ctx)

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
        if (
            desc.cache.enabled
            and not params
            and not explicit_out_path
            and audience is None
        ):
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
        """Catalog every registered descriptor, FE-friendly shape.

        Formats are canonicalised before they leave the API surface — any
        descriptor still declaring legacy ``latex`` is reported as
        ``pdf`` to the catalog (S7 rename). Backward-compat alias remains
        accepted on render requests; this is just the outward-facing
        label the FE shows.
        """
        catalog: List[Dict[str, Any]] = []
        for report_id in self._descriptors.list_ids():
            desc = self._descriptors.get(report_id)
            # Canonicalise + de-dup while preserving order. ``latex`` and
            # ``pdf`` collapse to a single ``pdf`` entry.
            seen: List[str] = []
            for f in desc.formats:
                canon = _canonicalize_fmt(f)
                if canon not in seen:
                    seen.append(canon)
            catalog.append(
                {
                    "report_id": desc.report_id,
                    "title_th": desc.title_th,
                    "title_en": desc.title_en,
                    "description_th": desc.description_th,
                    "description_en": desc.description_en,
                    "formats": seen,
                    "languages": list(desc.languages),
                    "audience": list(desc.audience),
                    "parameters": [p.model_dump() for p in desc.parameters],
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
        # S7: fmt comparison is alias-aware so `?fmt=latex` succeeds
        # against `formats: [pdf, html]` (and vice versa). Logs a
        # deprecation warning when the legacy `latex` name is used so we
        # can drop the alias next sprint.
        if not _format_matches(fmt, desc.formats):
            raise ValueError(
                f"report {desc.report_id!r} does not declare format "
                f"{fmt!r}; declared: {list(desc.formats)}"
            )
        _warn_legacy_fmt(fmt, source="render-request")
        if lang not in desc.languages:
            raise ValueError(
                f"report {desc.report_id!r} does not declare language "
                f"{lang!r}; declared: {list(desc.languages)}"
            )

    def _filter_sections_by_audience(
        self,
        desc: ReportDescriptor,
        audience: set[str],
    ) -> ReportDescriptor:
        """S8: drop sections whose block has a non-matching ``audience_target``.

        Audience-agnostic blocks (``audience_target = None``) ALWAYS
        render — they're the structural scaffolding (cover, headings)
        that has no audience opinion of its own. Blocks tagged with a
        specific :class:`AudienceTarget` render iff their target's
        string value is in ``audience``.

        Returns a copy of the descriptor with a filtered ``sections``
        list. The original (registry-stored) descriptor is untouched.
        """
        if not audience:
            return desc
        kept = []
        for section in desc.sections:
            try:
                block_cls = self._blocks.get(section.block)
            except KeyError:
                # Unknown block — let the rendering pass produce the
                # same KeyError it would have without audience filter.
                kept.append(section)
                continue
            target = getattr(block_cls, "audience_target", None)
            if target is None:
                kept.append(section)
                continue
            target_value = (
                target.value if hasattr(target, "value") else str(target)
            )
            if target_value in audience:
                kept.append(section)
            else:
                logger.debug(
                    "S8 audience filter: dropping section %s "
                    "(block=%s target=%s requested=%s)",
                    section.id, section.block, target_value, audience,
                )
        # Build a copy with the same field values + filtered sections.
        # Pydantic v2 ``model_copy(update=...)`` is the right primitive.
        return desc.model_copy(update={"sections": kept})

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

    # ------------------------------------------------------------------
    # Section rendering — public per ADR-03 S6 addendum so container
    # blocks (e.g. ``two_column_layout``) can re-enter the orchestrator
    # to resolve their own children. Top-level callers pass
    # ``desc.sections``; container blocks pass their nested
    # ``SectionSpec`` lists via ``ctx.extra["report_service"]``.
    # ------------------------------------------------------------------

    # Hard cap on container-block nesting depth. ADR-03 S6 addendum:
    # depth=1 is the only level supported — a layout block's children
    # cannot themselves be layout blocks. Caps live here (not on the
    # block) so the orchestrator owns the contract.
    MAX_RECURSION_DEPTH: int = 1

    async def _render_sections(
        self,
        sections: List[SectionSpec],
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

        Recursion depth (S6 addendum):
            Container blocks call this method again to resolve their
            children. Each re-entry bumps ``ctx.recursion_depth``;
            depth > ``MAX_RECURSION_DEPTH`` is rejected with a clear
            ``ValueError`` — the layout primitive is not reentrant.
            The depth counter is restored after the nested call so
            sibling container blocks at the same level each see the
            same starting depth.
        """
        # Note: the depth bump only fires on RE-ENTRY (when a container
        # block has already pushed past the top level). The initial
        # caller — ReportService.render() — passes ctx with depth==0 and
        # we run that level normally, exactly as the legacy code did.
        if ctx.recursion_depth > self.MAX_RECURSION_DEPTH:
            raise ValueError(
                f"_render_sections: recursion depth "
                f"{ctx.recursion_depth} exceeds cap "
                f"{self.MAX_RECURSION_DEPTH}; container blocks like "
                f"'two_column_layout' may not nest inside each other "
                f"(ADR-03 S6 addendum)"
            )

        rendered: List[RenderedSection] = []
        for section in sections:
            if not self._section_visible(section, ctx.fmt):
                logger.debug(
                    "ReportService: skipping section %s (visible_in=%s, fmt=%s)",
                    section.id, section.visible_in, ctx.fmt,
                )
                continue
            rendered.append(await self._render_section(section, ctx))
        return rendered

    async def _build_sections(
        self,
        desc: ReportDescriptor,
        ctx: RenderContext,
    ) -> List[RenderedSection]:
        """Backwards-compat alias for the pre-S6 ``_build_sections`` name.

        S4.5 callers (and any external tests) that drove section assembly
        through the private ``_build_sections`` keep working unchanged.
        New code should call :meth:`_render_sections` directly with an
        explicit ``list[SectionSpec]``.
        """
        logger.debug(
            "ReportService._build_sections is a S6 backward-compat "
            "alias; prefer _render_sections(desc.sections, ctx) directly."
        )
        return await self._render_sections(desc.sections, ctx)

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
        # S7: ``pdf`` and ``latex`` are aliases — every existing block
        # ships ``render_latex`` because that pre-dated the rename, so
        # we fall through to alias resolution before declaring the block
        # incapable. Block authors can add ``render_pdf`` whenever they
        # want; the alias keeps the old method name working until they do.
        render_method_name = f"render_{ctx.fmt}"
        render_method = getattr(block, render_method_name, None)
        if render_method is None:
            from services.reports.format_alias import aliases_for
            for alias_fmt in aliases_for(ctx.fmt):
                if alias_fmt == ctx.fmt:
                    continue
                fallback = getattr(block, f"render_{alias_fmt}", None)
                if fallback is not None:
                    render_method = fallback
                    break
        if render_method is None:
            raise ValueError(
                f"block {section.block!r} has no method {render_method_name!r}"
            )

        # collect() is async on the ABC; tolerate sync overrides too so
        # legacy / test blocks that haven't been migrated still work.
        try:
            collect_result: Any = block.collect(ctx, params_model)
            if hasattr(collect_result, "__await__"):
                data = await collect_result
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
