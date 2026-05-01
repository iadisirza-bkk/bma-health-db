#!/usr/bin/env python3
"""Smoke test for the descriptor + block + renderer pipeline (S4.4).

Renders a single descriptor in a chosen format/lang to a tempfile via stub
blocks + a fake data collector, prints the artefact path and size, and
exits non-zero if any block can't render.

Usage:
    python3 scripts/smoke_reports.py whitepaper html th
    python3 scripts/smoke_reports.py zone html th 01

Exits with status 1 and prints which block failed if any block.collect or
block.render_<fmt> raises. The fake collector returns deterministic shapes
so this script does NOT need a live DB.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, List

# Make ``api/`` importable so ``services.reports.*`` resolves the same way
# the test suite resolves it (mirrors ``tests/conftest.py``).
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent
_API_DIR = _REPO_ROOT / "api"
sys.path.insert(0, str(_API_DIR))

from pydantic import BaseModel, ConfigDict  # noqa: E402

from services.reports.blocks import BlockRegistry, ContentBlock  # noqa: E402
from services.reports.registry import ReportRegistry  # noqa: E402
from services.reports.renderer import RendererRegistry, ReportRenderer  # noqa: E402
from services.reports.service import ReportService  # noqa: E402
from services.reports.spec import (  # noqa: E402
    RenderContext,
    RenderedSection,
    ReportDescriptor,
)


# ---------------------------------------------------------------------------
# Stub blocks — one per block_id used by whitepaper.yaml + zone.yaml.
# These exist so the smoke test can run before S4.4 lands the real impls.
# ---------------------------------------------------------------------------


class _StubBlock(ContentBlock):
    block_id: str = "_stub"

    class Parameters(BaseModel):
        # ``extra="allow"`` — the descriptor params shape is whatever the
        # real block decides; for smoke we accept anything.
        model_config = ConfigDict(extra="allow")

    def collect(self, ctx: RenderContext, params: BaseModel) -> Dict[str, Any]:
        return {"block_id": self.block_id, "params": params.model_dump()}

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        return f"<section data-block=\"{self.block_id}\">[stub]</section>"

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        return f"% block: {self.block_id}\n"


def _make_block(block_id: str) -> type[ContentBlock]:
    cls = type(
        f"_Stub_{block_id}",
        (_StubBlock,),
        {"block_id": block_id},
    )
    return cls


def _build_block_registry() -> BlockRegistry:
    reg = BlockRegistry()
    for bid in (
        "cover_page",
        "heading",
        "kpi_grid",
        "chart",
        "table",
        "appendix_methodology",
    ):
        reg.register(_make_block(bid))
    return reg


# ---------------------------------------------------------------------------
# Fake renderer + data collector
# ---------------------------------------------------------------------------


class _FakeHtmlRenderer(ReportRenderer):
    fmt = "html"

    def render(
        self,
        desc: ReportDescriptor,
        sections: List[RenderedSection],
        ctx: RenderContext,
        out_path: Path,
    ) -> Path:
        body = "\n".join(str(s.markup) for s in sections)
        out_path.write_text(
            f"<!doctype html>\n<title>{desc.title_th}</title>\n"
            f"<body>\n{body}\n</body>\n",
            encoding="utf-8",
        )
        return out_path


class _FakeLatexRenderer(ReportRenderer):
    fmt = "latex"

    def render(
        self,
        desc: ReportDescriptor,
        sections: List[RenderedSection],
        ctx: RenderContext,
        out_path: Path,
    ) -> Path:
        body = "\n".join(str(s.markup) for s in sections)
        out_path.write_text(
            f"% {desc.title_th}\n\\begin{{document}}\n{body}"
            f"\n\\end{{document}}\n",
            encoding="utf-8",
        )
        return out_path


class _FakeDataCollector:
    """Stand-in for the real ReportDataCollector (S4.2)."""

    def get_summary(self, **_: Any) -> Dict[str, int]:
        return {"total_screened": 0, "districts_covered": 50, "found_ncd": 0}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Smoke-render a report descriptor through the new "
        "descriptor + block + renderer pipeline."
    )
    p.add_argument("report_id", help="e.g. whitepaper, zone")
    p.add_argument("fmt", choices=["html", "latex"], help="output format")
    p.add_argument("lang", help="language ISO code, e.g. th or en")
    p.add_argument(
        "zone_code",
        nargs="?",
        default=None,
        help="optional zone_code for parameterized descriptors",
    )
    return p.parse_args(argv)


def main(argv: List[str]) -> int:
    args = _parse_args(argv)

    config_dir = _REPO_ROOT / "config" / "reports"
    blocks = _build_block_registry()
    registry = ReportRegistry.discover(config_dir, blocks=blocks)

    renderers = RendererRegistry()
    renderers.register(_FakeHtmlRenderer())
    renderers.register(_FakeLatexRenderer())

    service = ReportService(
        registry=registry,
        blocks=blocks,
        renderers=renderers,
        data_collector=_FakeDataCollector(),
    )

    suffix = ".html" if args.fmt == "html" else ".tex"
    tmp = tempfile.NamedTemporaryFile(
        prefix=f"{args.report_id}_smoke_", suffix=suffix, delete=False
    )
    out_path = Path(tmp.name)
    tmp.close()

    params: Dict[str, Any] = {}
    if args.zone_code is not None:
        params["zone_code"] = args.zone_code

    try:
        result = service.render(
            args.report_id,
            args.fmt,
            args.lang,
            out_path=out_path,
            params=params or None,
        )
    except Exception as exc:
        # The orchestrator wraps block failures with the offending section
        # id; surface that verbatim so an operator can grep for it.
        print(
            f"[smoke_reports] FAILED to render {args.report_id} "
            f"({args.fmt}/{args.lang}): {exc}",
            file=sys.stderr,
        )
        traceback.print_exc()
        return 1

    size = result.stat().st_size if result.is_file() else 0
    print(f"OK  report_id={args.report_id} fmt={args.fmt} lang={args.lang}")
    print(f"    path={result}")
    print(f"    size={size} bytes")
    if size == 0:
        print("[smoke_reports] WARNING: artefact is zero bytes", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
