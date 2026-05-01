"""LaTeXRenderer — Jinja2 + Tectonic strategy for LaTeX/PDF output.

Wraps the same Jinja2 + Tectonic pipeline the legacy
``services/report_generator.py`` already uses. Both paths must produce
byte-identical PDFs during the S4 transition; that's why the filter
functions live in ``_latex_filters.py`` and the LaTeX preamble
(``bma_article_preamble.tex``) is shared verbatim.

Per ADR-03 §4, this renderer:
    * Receives a list of pre-rendered sections (one ``markup`` string per
      section, already escaped by the block's ``render_latex``).
    * Composes them into a single ``.tex`` via the
      ``descriptor_latex_root.tex.j2`` top-level template.
    * Compiles via Tectonic in a temp build dir.
    * Writes the resulting PDF to ``<config.REPORTS_DIR>/<lang>/<report_id>.pdf``
      and a ``.hash`` sidecar matching the orchestrator's cache layout.

Out-of-scope for S4.2:
    * Beamer / slides path — descriptor with ``style.layout = "slides"``
      ships in a follow-up ticket. The current root template hard-codes
      the ``article`` documentclass via the shared preamble.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, List, Optional

import jinja2

import config
from services.reports.renderer import ReportRenderer, renderer_registry
from services.reports.renderers._latex_filters import LATEX_FILTERS
from services.reports.spec import (
    RenderContext,
    RenderedSection,
    ReportDescriptor,
)

logger = logging.getLogger("api.services.reports.renderers.latex")

# Path constants — resolved relative to the API package so a deployed
# copy with a different cwd still finds the templates.
_TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "templates" / "latex"
_ROOT_TEMPLATE = "descriptor_latex_root.tex.j2"

# Tectonic config mirrors the legacy generator so behaviour is identical.
_TECTONIC_PATH = config.TECTONIC_PATH
_TECTONIC_TIMEOUT = int(config.TECTONIC_TIMEOUT)
_REPORTS_DIR = Path(config.REPORTS_DIR)


class LaTeXRenderer(ReportRenderer):
    """Concrete renderer for ``fmt = "latex"``.

    Construction is cheap (just builds a Jinja2 ``Environment``) so the
    module-level singleton at the bottom of this file is the standard
    way to use it; passing an instance to ``RendererRegistry.register``
    happens once at import time.
    """

    fmt: ClassVar[str] = "latex"

    def __init__(
        self,
        *,
        template_dir: Optional[Path] = None,
        tectonic_path: Optional[str] = None,
        tectonic_timeout: Optional[int] = None,
        reports_dir: Optional[Path] = None,
    ) -> None:
        self._template_dir = Path(template_dir) if template_dir else _TEMPLATE_DIR
        self._tectonic_path = tectonic_path or _TECTONIC_PATH
        self._tectonic_timeout = (
            int(tectonic_timeout) if tectonic_timeout is not None else _TECTONIC_TIMEOUT
        )
        self._reports_dir = Path(reports_dir) if reports_dir else _REPORTS_DIR

        # LaTeX-friendly Jinja2 delimiters chosen to avoid LaTeX's `{`,
        # `}`, `$`, `%` syntax — same as legacy ReportGenerator.__init__.
        self._env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(self._template_dir)),
            block_start_string="<%",
            block_end_string="%>",
            variable_start_string="<<",
            variable_end_string=">>",
            comment_start_string="<#",
            comment_end_string="#>",
            autoescape=False,
            undefined=jinja2.StrictUndefined,
        )
        # Install the shared filter set — single source of truth lives in
        # ``_latex_filters.LATEX_FILTERS`` so the legacy generator and this
        # renderer can't drift.
        for name, fn in LATEX_FILTERS.items():
            self._env.filters[name] = fn

    # ------------------------------------------------------------------
    # Cache-path helpers — same shape as the legacy
    # ``ReportGenerator.get_cache_path``, parameterised by report_id.
    # ------------------------------------------------------------------

    def cache_path(self, report_id: str, lang: str) -> Path:
        """Cache layout: ``<reports_dir>/<lang>/<report_id>.pdf``."""
        return self._reports_dir / lang / f"{report_id}.pdf"

    def hash_sidecar_path(self, report_id: str, lang: str) -> Path:
        return self.cache_path(report_id, lang).with_suffix(".hash")

    # ------------------------------------------------------------------
    # ReportRenderer impl
    # ------------------------------------------------------------------

    def render(
        self,
        desc: ReportDescriptor,
        sections: List[RenderedSection],
        ctx: RenderContext,
        out_path: Path,
    ) -> Path:
        """Compose the assembled sections + run Tectonic → PDF.

        ``out_path`` is the desired final PDF location. The renderer
        builds in a temp dir and atomically renames the result into
        place; the returned path is always equal to ``out_path``.
        """
        if not self._template_dir.is_dir():
            raise FileNotFoundError(
                f"LaTeX template dir does not exist: {self._template_dir}"
            )

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="bma_descriptor_") as tmpdir:
            build_dir = Path(tmpdir)

            # 1. Stage shared LaTeX assets (preamble + assets/ + i18n/).
            #    The legacy generator does this at runtime per render, and
            #    we mirror it so the shared preamble keeps working.
            self._stage_assets(build_dir)

            # 2. Render the root .tex via Jinja2.
            tex_source = self._render_root_tex(desc, sections, ctx)
            tex_path = build_dir / f"{desc.report_id}.tex"
            tex_path.write_text(tex_source, encoding="utf-8")

            # 3. Compile with Tectonic. SYNC — orchestrator wraps in
            #    ``asyncio.to_thread`` per the constraint in the prompt.
            pdf_path, clean = self._compile_tex(tex_path, build_dir)

            # 4. Atomic publish: write to a temp sibling, then rename.
            tmp_out = out_path.with_suffix(out_path.suffix + ".tmp")
            shutil.copy(pdf_path, tmp_out)
            tmp_out.replace(out_path)

            # 5. Hash sidecar — written ONLY on a clean compile, same as
            #    the legacy generator. The orchestrator separately writes
            #    the *data* hash sidecar; this method writes nothing more.
            if not clean:
                logger.warning(
                    "LaTeXRenderer: Tectonic non-zero for %s/%s — caller "
                    "should refrain from writing data_hash sidecar",
                    ctx.lang, desc.report_id,
                )

            return out_path

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _stage_assets(self, build_dir: Path) -> None:
        """Copy preamble + assets/ + i18n/ into the temp build dir."""
        assets_src = self._template_dir / "assets"
        if assets_src.exists():
            shutil.copytree(assets_src, build_dir / "assets")
        for f in self._template_dir.glob("bma_*.tex"):
            shutil.copy(f, build_dir)
        i18n_dir = self._template_dir / "i18n"
        if i18n_dir.exists():
            build_i18n = build_dir / "i18n"
            build_i18n.mkdir(exist_ok=True)
            for f in i18n_dir.glob("*.tex"):
                shutil.copy(f, build_i18n)

    def _render_root_tex(
        self,
        desc: ReportDescriptor,
        sections: List[RenderedSection],
        ctx: RenderContext,
    ) -> str:
        """Run the descriptor_latex_root.tex.j2 template against the spec."""
        try:
            template = self._env.get_template(_ROOT_TEMPLATE)
        except jinja2.TemplateNotFound as exc:
            raise FileNotFoundError(
                f"LaTeX root template missing: "
                f"{self._template_dir / _ROOT_TEMPLATE}"
            ) from exc

        # report_class is reserved for the future slides path. ``article``
        # matches the legacy preamble and is the only supported value in
        # S4.2.
        return template.render(
            desc=desc,
            sections=sections,
            style=desc.style,
            lang=ctx.lang,
            fmt=ctx.fmt,
            generated_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            report_class="article",
        )

    def _compile_tex(self, tex_path: Path, build_dir: Path) -> tuple[Path, bool]:
        """Run Tectonic on ``tex_path``. Returns ``(pdf_path, clean)``.

        ``clean`` is True iff the compile finished cleanly (exit 0).
        Same semantics as the legacy ``ReportGenerator._compile_tex``:
        non-zero exit + a partial PDF still returns the PDF, but the
        caller is expected to skip the cache-hash write so the next
        request retries.
        """
        if not Path(self._tectonic_path).exists():
            raise FileNotFoundError(
                f"Tectonic not found at {self._tectonic_path}. "
                "Set TECTONIC_PATH environment variable."
            )

        logger.info("LaTeXRenderer: compiling %s with Tectonic", tex_path.name)
        try:
            result = subprocess.run(
                [self._tectonic_path, str(tex_path)],
                capture_output=True,
                text=True,
                cwd=str(build_dir),
                timeout=self._tectonic_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Tectonic compilation timed out after {self._tectonic_timeout}s"
            ) from exc

        pdf_path = tex_path.with_suffix(".pdf")
        if result.returncode != 0:
            stderr = result.stderr[:1000] if result.stderr else "(no stderr)"
            stdout = result.stdout[:500] if result.stdout else ""
            if pdf_path.exists():
                logger.warning(
                    "Tectonic exited %d but PDF exists; returning partial "
                    "PDF (caller must NOT write hash sidecar). stderr: %s",
                    result.returncode, stderr[:300],
                )
                return pdf_path, False
            logger.error(
                "Tectonic failed (exit %d):\nstderr: %s\nstdout: %s",
                result.returncode, stderr, stdout,
            )
            raise RuntimeError(f"LaTeX compilation failed: {stderr[:500]}")

        if not pdf_path.exists():
            raise RuntimeError(
                f"Tectonic exited 0 but PDF not found at {pdf_path}"
            )
        return pdf_path, True


# ---------------------------------------------------------------------------
# Module-level registration. The act of importing this module wires the
# renderer into the global registry; the orchestrator's ``bootstrap()``
# ensures every concrete renderer module gets imported once at startup.
# ---------------------------------------------------------------------------
renderer_registry().register(LaTeXRenderer())


__all__ = ["LaTeXRenderer"]
