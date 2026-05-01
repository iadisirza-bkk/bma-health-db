"""Unit tests for the generic /api/v2/reports router (ADR-03 §6).

Strategy:
    * Build a minimal FastAPI app that mounts ONLY ``routers/reports_v2``
      so tests don't drag in the full /api/main.py + DB pool + middleware.
    * Override the ``get_report_service`` dependency with a hand-rolled
      mock that:
        - returns a fixed catalog list
        - returns a fixed descriptor (Pydantic model) for ``describe``
        - writes a tmp file and returns its path for ``render``
    * Verify the public contract: status codes, content-type for each
      format, descriptor JSON shape, and the 404 / 422 error paths.

The mocks NEVER touch the real LaTeX (Tectonic) or HTML (Jinja2) pipelines
— ``service.render`` is intercepted long before any renderer would run.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Make api/ importable for `routers.reports_v2` (mirrors tests/conftest.py).
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

# `routers.reports_v2` is the System Under Test. The Protocol-based import
# shims in that module mean importing it never blows up even if the sibling
# service modules aren't built yet.
from routers import reports_v2 as r2  # noqa: E402
from errors import BMAException, bma_exception_handler  # noqa: E402
from services.reports.spec import ReportDescriptor, SectionSpec  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _whitepaper_descriptor() -> ReportDescriptor:
    """Build a minimal valid descriptor for the test report id 'whitepaper'."""
    return ReportDescriptor(
        report_id="whitepaper",
        title_th="รายงานสุขภาพฉบับเต็ม",
        title_en="Comprehensive Whitepaper",
        formats=["latex", "html"],
        languages=["th", "en"],
        audience=["public"],
        sections=[
            SectionSpec(id="cover", block="cover_page", params={}),
        ],
    )


class _FakeReportService:
    """Mock ReportService.

    Supports:
        - ``list()`` → fixed catalog
        - ``describe(report_id)`` → known descriptor or KeyError
        - ``render(...)`` → write a tmp file and return its path
    """

    def __init__(self, tmp_dir: Path) -> None:
        self.tmp_dir = tmp_dir
        self._descriptors: Dict[str, ReportDescriptor] = {
            "whitepaper": _whitepaper_descriptor(),
        }
        self.render_calls: List[
            tuple[str, str, str, Dict[str, Any]]
        ] = []

    def list(self) -> List[dict]:
        return [
            {
                "report_id": "whitepaper",
                "title_th": "รายงานสุขภาพฉบับเต็ม",
                "formats": ["latex", "html"],
                "languages": ["th", "en"],
                "audience": ["public"],
            }
        ]

    def describe(self, report_id: str) -> ReportDescriptor:
        try:
            return self._descriptors[report_id]
        except KeyError as exc:
            raise KeyError(f"unknown report_id: {report_id!r}") from exc

    async def render(
        self,
        report_id: str,
        fmt: str,
        lang: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Path:
        self.render_calls.append((report_id, fmt, lang, dict(params or {})))
        # Write a tiny artefact whose contents reflect fmt so tests can
        # eyeball the response body when something goes wrong.
        suffix = "pdf" if fmt == "latex" else fmt
        out = self.tmp_dir / f"{report_id}_{lang}.{suffix}"
        if fmt == "html":
            out.write_text(
                "<!doctype html><title>fake</title><body>ok</body>",
                encoding="utf-8",
            )
        else:
            # Stand-in PDF / pptx — bytes don't matter, only that
            # FileResponse can stat & stream them.
            out.write_bytes(b"%PDF-fake")
        return out


# ---------------------------------------------------------------------------
# App / client fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_service(tmp_path: Path) -> _FakeReportService:
    return _FakeReportService(tmp_dir=tmp_path)


@pytest.fixture
def app(fake_service: _FakeReportService) -> FastAPI:
    """Build a slim FastAPI app that mounts only the v2 reports router.

    We deliberately bypass the full /api/main.py so tests don't need a
    DB pool, Redis, or the X-API-Key middleware. The router under test
    is the same module that main.py mounts — only the surrounding wiring
    differs.
    """
    a = FastAPI()
    # Mirror the real app's BMAException handling so the assertions on
    # 404 / 422 status codes hit the same code path as production.
    a.add_exception_handler(BMAException, bma_exception_handler)

    # Override the DI factory before mounting so every request through
    # this app sees the fake service.
    a.dependency_overrides[r2.get_report_service] = lambda: fake_service
    a.include_router(r2.router)
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_list_reports_returns_catalog(
    client: TestClient,
    fake_service: _FakeReportService,
) -> None:
    """`GET /api/v2/reports` returns 200 + a list."""
    resp = client.get("/api/v2/reports")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    item = body[0]
    assert item["report_id"] == "whitepaper"
    assert item["formats"] == ["latex", "html"]
    assert item["languages"] == ["th", "en"]


def test_get_spec_returns_descriptor_json(
    client: TestClient,
) -> None:
    """`GET /api/v2/reports/whitepaper/spec` returns 200 + descriptor JSON."""
    resp = client.get("/api/v2/reports/whitepaper/spec")
    assert resp.status_code == 200
    body = resp.json()
    assert body["report_id"] == "whitepaper"
    assert body["title_th"] == "รายงานสุขภาพฉบับเต็ม"
    assert body["formats"] == ["latex", "html"]
    assert body["languages"] == ["th", "en"]
    # Sections came through (Pydantic v2 model_dump preserves nested models).
    assert isinstance(body["sections"], list)
    assert body["sections"][0]["id"] == "cover"
    assert body["sections"][0]["block"] == "cover_page"


def test_get_spec_unknown_report_returns_404(
    client: TestClient,
) -> None:
    """Unknown report_id on the /spec endpoint → 404."""
    resp = client.get("/api/v2/reports/nope/spec")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_code"] == "NOT_FOUND"


def test_render_html_returns_html_content_type(
    client: TestClient,
    fake_service: _FakeReportService,
) -> None:
    """`GET /api/v2/reports/whitepaper/html/th` returns 200 + text/html."""
    resp = client.get("/api/v2/reports/whitepaper/html/th")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    # Body is whatever the fake renderer wrote.
    assert b"<title>fake</title>" in resp.content
    # And the service was called with the right (report_id, fmt, lang).
    assert fake_service.render_calls == [("whitepaper", "html", "th", {})]


def test_render_latex_returns_pdf_content_type(
    client: TestClient,
    fake_service: _FakeReportService,
) -> None:
    """LaTeX format maps to `application/pdf` — Tectonic compiles to PDF."""
    resp = client.get("/api/v2/reports/whitepaper/latex/en")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == b"%PDF-fake"
    assert fake_service.render_calls == [("whitepaper", "latex", "en", {})]


def test_render_forwards_query_params_as_substitution_params(
    client: TestClient,
    fake_service: _FakeReportService,
) -> None:
    """Arbitrary `?key=value` query params reach `service.render(..., params=...)`."""
    resp = client.get(
        "/api/v2/reports/whitepaper/html/th?zone_code=3&another=x"
    )
    assert resp.status_code == 200
    assert fake_service.render_calls == [
        ("whitepaper", "html", "th", {"zone_code": "3", "another": "x"})
    ]


def test_render_unsupported_format_returns_422(
    client: TestClient,
) -> None:
    """`pptx` is not in the descriptor's `formats` list → 422."""
    resp = client.get("/api/v2/reports/whitepaper/pptx/th")
    assert resp.status_code == 422
    body = resp.json()
    # FastAPI's HTTPException wraps the dict under "detail" by default.
    assert body["detail"]["error_code"] == "UNSUPPORTED_FORMAT"
    assert body["detail"]["fmt"] == "pptx"
    assert body["detail"]["supported_formats"] == ["latex", "html"]


def test_render_unsupported_lang_returns_422(
    client: TestClient,
) -> None:
    """Unknown language → 422."""
    resp = client.get("/api/v2/reports/whitepaper/html/xx")
    assert resp.status_code == 422
    body = resp.json()
    assert body["detail"]["error_code"] == "UNSUPPORTED_LANGUAGE"
    assert body["detail"]["lang"] == "xx"


def test_render_unknown_report_returns_404(
    client: TestClient,
) -> None:
    """`GET /api/v2/reports/nope/html/th` returns 404 (unknown report_id)."""
    resp = client.get("/api/v2/reports/nope/html/th")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_code"] == "NOT_FOUND"
