"""Unit tests for ``services.reports.renderers.html.HTMLRenderer``.

These tests stand alone — they fabricate a ``RenderContext`` and a list of
``RenderedSection`` instances with hand-written HTML, then drive the
renderer end-to-end and inspect the on-disk output.

We intentionally do NOT exercise:
    * Real ``ContentBlock`` instances — those live in S4.4.
    * ``ReportDataCollector`` queries — the renderer never touches the
      DB; ``ctx.data_collector`` is a sentinel here.
    * Tectonic / PDF compilation — that's the LaTeX renderer's job.

The renderer's contract per ADR-03 §4:
    * Output starts with ``<!DOCTYPE html>``.
    * Output is self-contained (inline ``<style>``, no external
      ``<link rel="stylesheet">``, no remote ``<script src>``).
    * Each section's ``markup`` is embedded verbatim.
    * The ``<nav class="toc">`` lists every section by its declared
      ``title_th`` (or by id as a fallback).
    * The ``data_hash`` reaches the footer.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.reports import (
    RenderContext,
    RenderedSection,
    ReportDescriptor,
    SectionSpec,
)
from services.reports.renderers.html import HTMLRenderer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_descriptor() -> ReportDescriptor:
    """A minimal three-section descriptor covering: declared title,
    declared-but-no-title-th (must fall back to id), and an English-only
    label scenario."""
    return ReportDescriptor(
        report_id="hello_world",
        title_th="รายงานทดสอบ",
        title_en="Test Report",
        formats=["html"],
        languages=["th", "en"],
        sections=[
            SectionSpec(id="cover", block="cover_page", title_th="หน้าปก"),
            SectionSpec(id="kpis", block="kpi_grid", title_th="ตัวชี้วัดหลัก"),
            SectionSpec(id="appendix", block="appendix_methodology"),
        ],
    )


def _make_sections() -> list[RenderedSection]:
    """Hand-written HTML markup, one per declared section. The
    renderer must embed each verbatim."""
    # Bare BaseModel so the dataclass field is satisfied without
    # requiring real block Parameters models.
    from pydantic import BaseModel

    class _Empty(BaseModel):
        pass

    p = _Empty()
    return [
        RenderedSection(
            section_id="cover",
            block_id="cover_page",
            markup="<h1>HELLO_COVER_MARKER</h1><p>เวอร์ชันทดสอบ</p>",
            data={},
            params=p,
        ),
        RenderedSection(
            section_id="kpis",
            block_id="kpi_grid",
            markup="<h2>KPIS_MARKER</h2><table><tr><th>Metric</th><td>42</td></tr></table>",
            data={"metrics": {"foo": 42}},
            params=p,
        ),
        RenderedSection(
            section_id="appendix",
            block_id="appendix_methodology",
            markup='<h2>APPENDIX_MARKER</h2><figure><svg width="10" height="10"><rect fill="red" width="10" height="10"/></svg><figcaption>chart</figcaption></figure>',
            data={},
            params=p,
        ),
    ]


def _make_context(desc: ReportDescriptor, *, lang: str = "th") -> RenderContext:
    return RenderContext(
        # data_collector is opaque to the renderer; SimpleNamespace stub
        # avoids dragging the real collector / DB into a unit test.
        data_collector=SimpleNamespace(),
        lang=lang,
        fmt="html",
        descriptor=desc,
        requested_at=datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc),
        extra={"data_hash": "deadbeefcafe1234", "app_version": "BMA Health Database v2.1"},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_render_writes_doctype_and_returns_path(tmp_path: Path) -> None:
    desc = _make_descriptor()
    sections = _make_sections()
    ctx = _make_context(desc)

    renderer = HTMLRenderer()
    out = renderer.render(desc, sections, ctx, tmp_path / "out.html")

    assert out == tmp_path / "out.html"
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.startswith("<!DOCTYPE html>"), "must start with HTML5 doctype"


def test_render_embeds_every_section_markup(tmp_path: Path) -> None:
    desc = _make_descriptor()
    sections = _make_sections()
    ctx = _make_context(desc)

    out = HTMLRenderer().render(desc, sections, ctx, tmp_path / "out.html")
    text = out.read_text(encoding="utf-8")

    for s in sections:
        assert s.markup in text, (
            f"section {s.section_id!r} markup missing — renderer must "
            "splice block-rendered HTML in verbatim"
        )


def test_render_includes_toc_link_per_section(tmp_path: Path) -> None:
    desc = _make_descriptor()
    sections = _make_sections()
    ctx = _make_context(desc)

    out = HTMLRenderer().render(desc, sections, ctx, tmp_path / "out.html")
    text = out.read_text(encoding="utf-8")

    # Sanity: there is exactly one nav.toc.
    assert text.count('<nav class="toc"') == 1
    # Each section gets a TOC link to its anchor id. The link text
    # should be the declared title_th, falling back to the id when no
    # label was set on the SectionSpec.
    for s in sections:
        anchor = f'href="#{s.section_id}"'
        assert anchor in text, f"TOC missing anchor for {s.section_id}"

    expected_labels = {"cover": "หน้าปก", "kpis": "ตัวชี้วัดหลัก", "appendix": "appendix"}
    for sec_id, label in expected_labels.items():
        assert label in text, f"TOC label {label!r} (for {sec_id}) not found"


def test_render_section_anchors_match_section_ids(tmp_path: Path) -> None:
    desc = _make_descriptor()
    sections = _make_sections()
    ctx = _make_context(desc)

    out = HTMLRenderer().render(desc, sections, ctx, tmp_path / "out.html")
    text = out.read_text(encoding="utf-8")

    for s in sections:
        assert f'<section id="{s.section_id}">' in text


def test_render_includes_data_hash_in_footer(tmp_path: Path) -> None:
    desc = _make_descriptor()
    sections = _make_sections()
    ctx = _make_context(desc)
    ctx.extra["data_hash"] = "abc123fingerprint"

    out = HTMLRenderer().render(desc, sections, ctx, tmp_path / "out.html")
    text = out.read_text(encoding="utf-8")

    # The hash must appear inside the footer block, not loose.
    footer_match = re.search(
        r'<footer[^>]*class="[^"]*report-footer[^"]*"[^>]*>(.*?)</footer>',
        text,
        flags=re.DOTALL,
    )
    assert footer_match is not None, "report footer not found"
    assert "abc123fingerprint" in footer_match.group(1)


def test_render_is_self_contained_no_external_assets(tmp_path: Path) -> None:
    desc = _make_descriptor()
    sections = _make_sections()
    ctx = _make_context(desc)

    out = HTMLRenderer().render(desc, sections, ctx, tmp_path / "out.html")
    text = out.read_text(encoding="utf-8")

    # Inline CSS is mandatory.
    assert "<style>" in text
    # No external stylesheet links of any flavour.
    assert not re.search(r'<link\b[^>]*rel=["\']?stylesheet', text, flags=re.IGNORECASE)
    # No remote scripts either.
    assert not re.search(
        r'<script\b[^>]*src=["\']https?://', text, flags=re.IGNORECASE
    )
    # No <link href="https://..."> for icons / fonts that would phone
    # home — local data: URIs are the only acceptable source.
    assert not re.search(r'<link\b[^>]*href=["\']https?://', text, flags=re.IGNORECASE)


def test_render_sets_lang_dir_for_rtl_languages(tmp_path: Path) -> None:
    desc = _make_descriptor()
    desc.languages = ["ar"]
    sections = _make_sections()
    ctx = _make_context(desc, lang="ar")

    out = HTMLRenderer().render(desc, sections, ctx, tmp_path / "out.html")
    text = out.read_text(encoding="utf-8")
    assert 'lang="ar"' in text
    assert 'dir="rtl"' in text


def test_render_uses_english_title_when_lang_en(tmp_path: Path) -> None:
    desc = _make_descriptor()
    sections = _make_sections()
    ctx = _make_context(desc, lang="en")

    out = HTMLRenderer().render(desc, sections, ctx, tmp_path / "out.html")
    text = out.read_text(encoding="utf-8")
    assert "<title>Test Report</title>" in text


def test_render_creates_parent_directory(tmp_path: Path) -> None:
    desc = _make_descriptor()
    sections = _make_sections()
    ctx = _make_context(desc)

    nested = tmp_path / "html" / "th" / "hello_world.html"
    out = HTMLRenderer().render(desc, sections, ctx, nested)
    assert out == nested
    assert nested.exists()


def test_renderer_self_registers_under_html_fmt() -> None:
    # Importing the module must register the renderer.
    from services.reports import renderer_registry
    from services.reports.renderers import html as html_module  # noqa: F401

    reg = renderer_registry()
    assert "html" in reg
    assert reg.get("html").fmt == "html"


def test_render_includes_app_version_in_footer(tmp_path: Path) -> None:
    desc = _make_descriptor()
    sections = _make_sections()
    ctx = _make_context(desc)

    out = HTMLRenderer().render(desc, sections, ctx, tmp_path / "out.html")
    text = out.read_text(encoding="utf-8")
    assert "BMA Health Database v2.1" in text
