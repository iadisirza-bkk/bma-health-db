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
from typing import Any, Dict, List, Optional, Set

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
from services.reports.spec import (  # noqa: E402
    ParameterOption,
    ParameterSpec,
    ReportDescriptor,
    SectionSpec,
)


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


def _zone_descriptor() -> ReportDescriptor:
    """Zone-like descriptor exercising S7 typed `parameters` + `pdf` format.

    Mirrors the shape of ``config/reports/zone.yaml``: enum dropdown for
    ``zone_code`` and the canonical ``pdf`` format name (rather than the
    legacy ``latex``).
    """
    return ReportDescriptor(
        report_id="zone",
        title_th="รายงานเขตสุขภาพ {zone_code}",
        title_en="Zone Report {zone_code}",
        description_th="รายงานสุขภาพระดับเขตสุขภาพของกรุงเทพมหานคร",
        description_en="Per-zone health report for Bangkok",
        formats=["pdf", "html"],
        languages=["th", "en"],
        audience=["public", "clinician"],
        parameters=[
            ParameterSpec(
                key="zone_code",
                type="enum",
                label_th="เขตสุขภาพ",
                label_en="Health Zone",
                required=True,
                options=[
                    ParameterOption(value="01", label_th="เขตสุขภาพ 1"),
                    ParameterOption(value="02", label_th="เขตสุขภาพ 2"),
                    ParameterOption(value="03", label_th="เขตสุขภาพ 3"),
                ],
            ),
        ],
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
            "zone": _zone_descriptor(),
        }
        self.render_calls: List[
            tuple[str, str, str, Dict[str, Any]]
        ] = []
        # S8 — captures the ``audience=`` kwarg so tests can assert the
        # router parsed + forwarded the comma-list correctly.
        self.audience_calls: List[Optional[set[str]]] = []

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
        audience: Optional[set[str]] = None,
        # S9 — accept (and ignore) the orchestration kwargs the v2 router
        # forwards in real production. The fake doesn't drive the polish
        # / cache pipelines; we just need to stay signature-compatible
        # so the router's call doesn't raise TypeError → 422.
        feature_flags: Optional[Dict[str, Any]] = None,
        polish_service: Optional[Any] = None,
    ) -> Path:
        self.render_calls.append((report_id, fmt, lang, dict(params or {})))
        # S8 — record the per-call audience set (None == "no filter").
        self.audience_calls.append(set(audience) if audience else audience)
        # Write a tiny artefact whose contents reflect fmt so tests can
        # eyeball the response body when something goes wrong. ``latex``
        # and ``pdf`` both produce the same .pdf artefact (S7 alias).
        suffix = "pdf" if fmt in ("latex", "pdf") else fmt
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


@pytest.fixture(autouse=True)
def _isolate_pdf_cache(tmp_path: Path):
    """Re-root the S9 ``PdfCache`` singleton at a tmp dir per test.

    Without this, the first test puts a cache entry that subsequent
    tests would HIT (since the content hash is deterministic for the
    fake render output), masking the call into ``service.render``.
    """
    from services.reports.pdf_cache import (
        get_pdf_cache,
        reset_pdf_cache_singleton,
    )
    reset_pdf_cache_singleton()
    get_pdf_cache(tmp_path / "pdf_cache_isolated")
    try:
        yield
    finally:
        reset_pdf_cache_singleton()


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


# ---------------------------------------------------------------------------
# S7 — typed parameters round-trip through /spec
# ---------------------------------------------------------------------------


def test_get_spec_returns_typed_parameters_and_description(
    client: TestClient,
) -> None:
    """S7 (a): the typed `parameters` schema + `description_th` round-trip
    through `/api/v2/reports/{id}/spec`.

    The frontend reads this to render a typed dropdown instead of a
    free-form `key=value` text input. A regression here would silently
    break the FE — the response shape is the contract.
    """
    resp = client.get("/api/v2/reports/zone/spec")
    assert resp.status_code == 200
    body = resp.json()

    # Descriptor-level fields exposed by Task A/D.
    assert body["report_id"] == "zone"
    assert body["description_th"] == "รายงานสุขภาพระดับเขตสุขภาพของกรุงเทพมหานคร"
    assert body["description_en"] == "Per-zone health report for Bangkok"
    # Canonical S7 format name.
    assert "pdf" in body["formats"]

    # Typed parameters surface — the FE renders a dropdown from this.
    assert isinstance(body["parameters"], list)
    assert len(body["parameters"]) == 1
    p = body["parameters"][0]
    assert p["key"] == "zone_code"
    assert p["type"] == "enum"
    assert p["label_th"] == "เขตสุขภาพ"
    assert p["label_en"] == "Health Zone"
    assert p["required"] is True
    # Each option survives the JSON round-trip with its bilingual labels.
    assert p["options"][0] == {
        "value": "01",
        "label_th": "เขตสุขภาพ 1",
        "label_en": None,
    }
    assert {opt["value"] for opt in p["options"]} == {"01", "02", "03"}


# ---------------------------------------------------------------------------
# S7 — `pdf` and `latex` aliases interoperate
# ---------------------------------------------------------------------------


def test_render_pdf_alias_against_latex_only_descriptor(
    client: TestClient,
    fake_service: _FakeReportService,
) -> None:
    """S7 (b): `?fmt=pdf` succeeds against the legacy `[latex, html]`
    descriptor (whitepaper) — the alias resolves at the router validation
    step. Content-type is `application/pdf` either way.
    """
    resp = client.get("/api/v2/reports/whitepaper/pdf/th")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == b"%PDF-fake"
    assert fake_service.render_calls == [("whitepaper", "pdf", "th", {})]


def test_render_latex_alias_against_pdf_only_descriptor(
    client: TestClient,
    fake_service: _FakeReportService,
) -> None:
    """S7 (b, reverse): `?fmt=latex` still works against the new
    `[pdf, html]` descriptor (zone) — backward-compat alias keeps older
    callers (and copy-pasted CLIs) functional through the S7 cutover.
    """
    resp = client.get("/api/v2/reports/zone/latex/th?zone_code=01")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert fake_service.render_calls == [
        ("zone", "latex", "th", {"zone_code": "01"})
    ]


# ---------------------------------------------------------------------------
# S8 — audience query-param routing
# ---------------------------------------------------------------------------
#
# These tests cover the OPTIONAL ``?audience=<comma-list>`` parameter
# added in Sprint S8 ("Audience-Segmented Report Sections"). The router
# is responsible for:
#     * Validating each comma-separated value against ``AudienceTarget``.
#     * Stripping ``audience`` from the params forwarded to
#       ``service.render(..., params=...)`` so it doesn't accidentally
#       become a ``{placeholder}`` substitution.
#     * Forwarding the parsed set to ``service.render(..., audience=...)``.
#     * Returning HTTP 400 (bad request) on unknown values — NOT 422,
#       because audience filter is orchestration metadata and not part
#       of the descriptor's typed parameters.


def test_render_no_audience_param_keeps_existing_behavior(
    client: TestClient,
    fake_service: _FakeReportService,
) -> None:
    """No ``?audience=`` → audience kwarg arrives as ``None`` (status quo)."""
    resp = client.get("/api/v2/reports/whitepaper/html/th")
    assert resp.status_code == 200
    assert fake_service.audience_calls == [None]


def test_render_audience_single_value_parsed_into_set(
    client: TestClient,
    fake_service: _FakeReportService,
) -> None:
    """`?audience=executive` → set with one value, NOT a string."""
    resp = client.get(
        "/api/v2/reports/whitepaper/html/th?audience=executive"
    )
    assert resp.status_code == 200
    assert fake_service.audience_calls == [{"executive"}]
    # And the audience kwarg was NOT forwarded as a placeholder param.
    assert fake_service.render_calls == [
        ("whitepaper", "html", "th", {})
    ]


def test_render_audience_comma_list_parsed_correctly(
    client: TestClient,
    fake_service: _FakeReportService,
) -> None:
    """`?audience=executive,clinician` → set of two values, both kept."""
    resp = client.get(
        "/api/v2/reports/whitepaper/html/th?audience=executive,clinician"
    )
    assert resp.status_code == 200
    assert fake_service.audience_calls == [{"executive", "clinician"}]
    # And the audience kwarg was NOT forwarded as a placeholder param.
    assert fake_service.render_calls == [
        ("whitepaper", "html", "th", {})
    ]


def test_render_audience_invalid_value_returns_400(
    client: TestClient,
    fake_service: _FakeReportService,
) -> None:
    """`?audience=invalid` → 400 INVALID_AUDIENCE, render NOT called."""
    resp = client.get(
        "/api/v2/reports/whitepaper/html/th?audience=invalid"
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["error_code"] == "INVALID_AUDIENCE"
    assert "invalid" in body["detail"]["audience"]
    # Validation happens BEFORE service.render — the fake never sees it.
    assert fake_service.render_calls == []


def test_render_audience_mixed_valid_and_invalid_returns_400(
    client: TestClient,
) -> None:
    """`?audience=executive,bogus` → 400 (one bad value taints the set)."""
    resp = client.get(
        "/api/v2/reports/whitepaper/html/th?audience=executive,bogus"
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["error_code"] == "INVALID_AUDIENCE"
    # The unknown-list contains the bad value and not the good one.
    assert "bogus" in body["detail"]["message"]


def test_render_audience_empty_string_returns_400(
    client: TestClient,
) -> None:
    """`?audience=` (empty) → 400; we require at least one value."""
    resp = client.get("/api/v2/reports/whitepaper/html/th?audience=")
    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["error_code"] == "INVALID_AUDIENCE"


def test_render_audience_combined_with_other_params(
    client: TestClient,
    fake_service: _FakeReportService,
) -> None:
    """``?zone_code=3&audience=clinician`` — audience pop'd from params,
    other query params (zone_code) still forwarded as placeholders."""
    resp = client.get(
        "/api/v2/reports/whitepaper/html/th?zone_code=3&audience=clinician"
    )
    assert resp.status_code == 200
    assert fake_service.audience_calls == [{"clinician"}]
    # zone_code IS still forwarded; audience IS NOT in the params dict.
    assert fake_service.render_calls == [
        ("whitepaper", "html", "th", {"zone_code": "3"})
    ]


# ---------------------------------------------------------------------------
# S8 — section-level audience filter behavior in the orchestrator
# ---------------------------------------------------------------------------
#
# The router test above checks the wire surface (parse + forward). This
# block exercises the orchestrator's :meth:`_filter_sections_by_audience`
# helper directly — covering the "all 4 audience blocks present, only
# the requested one survives" scenario from the brief without needing a
# DB or a real renderer.


def test_filter_sections_by_audience_drops_non_matching_blocks(
    tmp_path: Path,
) -> None:
    """When called with a single-element audience set, sections whose
    block has a non-matching ``audience_target`` are dropped; agnostic
    sections (audience_target=None) ALWAYS survive."""
    from services.reports.service import ReportService  # noqa: WPS433
    from services.reports.spec import ReportDescriptor, SectionSpec

    # Tiny stand-ins for the four audience blocks + one agnostic block.
    class _ExecBlock:
        block_id = "e"
        from services.reports.blocks.base import AudienceTarget as _AT  # noqa: WPS433
        audience_target = _AT.EXECUTIVE

    class _ClinBlock:
        block_id = "c"
        from services.reports.blocks.base import AudienceTarget as _AT  # noqa: WPS433
        audience_target = _AT.CLINICIAN

    class _AgnosticBlock:
        block_id = "ag"
        audience_target = None

    class _FakeBlocks:
        def get(self, block_id: str):
            return {
                "exec_block": _ExecBlock,
                "clin_block": _ClinBlock,
                "agnostic_block": _AgnosticBlock,
            }[block_id]

    # Minimal fakes for the service constructor.
    class _FakeRenderers:
        def get(self, fmt):  # pragma: no cover — never called in this test
            raise NotImplementedError

    class _FakeRegistry:
        def get(self, rid):  # pragma: no cover
            raise NotImplementedError

    class _FakeData:
        def data_hash(self):  # pragma: no cover
            return "x"

    svc = ReportService(
        descriptors=_FakeRegistry(),
        blocks=_FakeBlocks(),
        renderers=_FakeRenderers(),
        data=_FakeData(),
        out_dir=tmp_path,
    )

    desc = ReportDescriptor(
        report_id="r",
        title_th="t",
        formats=["html"],
        languages=["th"],
        sections=[
            SectionSpec(id="cover", block="agnostic_block", params={}),
            SectionSpec(id="exec_section", block="exec_block", params={}),
            SectionSpec(id="clin_section", block="clin_block", params={}),
        ],
    )

    filtered = svc._filter_sections_by_audience(desc, {"executive"})
    kept_ids = [s.id for s in filtered.sections]
    # Agnostic + exec stay; clinician drops.
    assert kept_ids == ["cover", "exec_section"]


def test_filter_sections_by_audience_keeps_multiple_when_multi_set(
    tmp_path: Path,
) -> None:
    """`audience={'executive','clinician'}` — both audience blocks
    kept, plus the agnostic one."""
    from services.reports.service import ReportService  # noqa: WPS433
    from services.reports.spec import ReportDescriptor, SectionSpec
    from services.reports.blocks.base import AudienceTarget as _AT

    class _ExecBlock:
        block_id = "e"
        audience_target = _AT.EXECUTIVE

    class _ClinBlock:
        block_id = "c"
        audience_target = _AT.CLINICIAN

    class _ResBlock:
        block_id = "r"
        audience_target = _AT.RESEARCHER

    class _AgnosticBlock:
        block_id = "ag"
        audience_target = None

    class _FakeBlocks:
        def get(self, block_id: str):
            return {
                "exec_block": _ExecBlock,
                "clin_block": _ClinBlock,
                "res_block": _ResBlock,
                "agnostic_block": _AgnosticBlock,
            }[block_id]

    class _FakeRenderers:
        def get(self, fmt):
            raise NotImplementedError

    class _FakeRegistry:
        def get(self, rid):
            raise NotImplementedError

    class _FakeData:
        def data_hash(self):
            return "x"

    svc = ReportService(
        descriptors=_FakeRegistry(),
        blocks=_FakeBlocks(),
        renderers=_FakeRenderers(),
        data=_FakeData(),
        out_dir=tmp_path,
    )

    desc = ReportDescriptor(
        report_id="r",
        title_th="t",
        formats=["html"],
        languages=["th"],
        sections=[
            SectionSpec(id="cover", block="agnostic_block", params={}),
            SectionSpec(id="exec_section", block="exec_block", params={}),
            SectionSpec(id="clin_section", block="clin_block", params={}),
            SectionSpec(id="res_section", block="res_block", params={}),
        ],
    )

    filtered = svc._filter_sections_by_audience(
        desc, {"executive", "clinician"}
    )
    kept_ids = [s.id for s in filtered.sections]
    assert kept_ids == ["cover", "exec_section", "clin_section"]
    # Researcher is dropped.
    assert "res_section" not in kept_ids


# ---------------------------------------------------------------------------
# S9 — content-hash cache HIT / MISS round-trip through the v2 router
# ---------------------------------------------------------------------------
#
# The cache wraps the (still-mocked) ``service.render`` call. The fake
# always writes the same bytes to its tmp file, so the second identical
# request through the router should HIT the on-disk cache and skip the
# fake's ``render`` entirely.


def test_render_cache_first_request_is_miss(
    client: TestClient,
    fake_service: _FakeReportService,
) -> None:
    """First request → ``X-Cache: MISS`` and ``service.render`` runs."""
    resp = client.get("/api/v2/reports/whitepaper/pdf/th")
    assert resp.status_code == 200
    assert resp.headers.get("x-cache") == "MISS"
    assert resp.headers.get("x-cache-hash") not in (None, "")
    # Data-version header is always set, even when the cache misses.
    assert resp.headers.get("x-data-version") not in (None, "")
    # And the fake renderer was called exactly once.
    assert len(fake_service.render_calls) == 1


def test_render_cache_second_request_is_hit_and_skips_render(
    client: TestClient,
    fake_service: _FakeReportService,
) -> None:
    """Second identical request → ``X-Cache: HIT``, fake render NOT called."""
    # Prime the cache with the first request.
    resp1 = client.get("/api/v2/reports/whitepaper/pdf/th")
    assert resp1.status_code == 200
    assert resp1.headers.get("x-cache") == "MISS"
    miss_hash = resp1.headers.get("x-cache-hash")

    # Second identical request — cache hit.
    resp2 = client.get("/api/v2/reports/whitepaper/pdf/th")
    assert resp2.status_code == 200
    assert resp2.headers.get("x-cache") == "HIT"
    # The cache hash should match the miss hash exactly.
    assert resp2.headers.get("x-cache-hash") == miss_hash
    # The renderer was called only ONCE total (the miss); the hit
    # served from disk and never touched the fake.
    assert len(fake_service.render_calls) == 1
    # PDF bytes round-trip through the cache faithfully.
    assert resp2.content == resp1.content
    # X-Cache-Age is non-negative.
    assert int(resp2.headers.get("x-cache-age", "-1")) >= 0


def test_render_cache_different_audience_is_separate_entry(
    client: TestClient,
    fake_service: _FakeReportService,
) -> None:
    """Different ``?audience=`` value → different hash → MISS again."""
    r1 = client.get("/api/v2/reports/whitepaper/pdf/th")
    assert r1.headers.get("x-cache") == "MISS"

    # Same URL but new audience filter → cache key shifts.
    r2 = client.get(
        "/api/v2/reports/whitepaper/pdf/th?audience=executive"
    )
    assert r2.headers.get("x-cache") == "MISS"
    assert r2.headers.get("x-cache-hash") != r1.headers.get("x-cache-hash")
    # Renderer ran twice (different cache entries).
    assert len(fake_service.render_calls) == 2


def test_render_cache_different_params_is_separate_entry(
    client: TestClient,
    fake_service: _FakeReportService,
) -> None:
    """Changing a query param → MISS the second time too."""
    r1 = client.get("/api/v2/reports/whitepaper/pdf/th?zone_code=03")
    assert r1.headers.get("x-cache") == "MISS"

    r2 = client.get("/api/v2/reports/whitepaper/pdf/th?zone_code=04")
    assert r2.headers.get("x-cache") == "MISS"
    assert r2.headers.get("x-cache-hash") != r1.headers.get("x-cache-hash")
    assert len(fake_service.render_calls) == 2


def test_render_cache_invalidates_when_data_version_changes(
    client: TestClient,
    fake_service: _FakeReportService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulated MV refresh ⇒ data_version flips ⇒ next request MISS."""
    # First request — cache MISS, computed against current data_version.
    r1 = client.get("/api/v2/reports/whitepaper/pdf/th")
    assert r1.headers.get("x-cache") == "MISS"
    h1 = r1.headers.get("x-cache-hash")

    # Re-issue WITHOUT changing anything → HIT.
    r_hit = client.get("/api/v2/reports/whitepaper/pdf/th")
    assert r_hit.headers.get("x-cache") == "HIT"

    # Now SHIFT the data_version computation so the next request hashes
    # differently. We monkey-patch the helper inside the router module.
    monkeypatch.setattr(
        r2,
        "_compute_data_version",
        lambda: ("zzzz9999zzzz9999", "2099-01-01"),
    )

    r2_resp = client.get("/api/v2/reports/whitepaper/pdf/th")
    assert r2_resp.headers.get("x-cache") == "MISS"
    assert r2_resp.headers.get("x-cache-hash") != h1
    # And the new data_version surfaces in the response header.
    assert r2_resp.headers.get("x-data-version") == "zzzz9999zzzz9999"
    assert r2_resp.headers.get("x-data-as-of") == "2099-01-01"


def test_render_cache_data_version_header_present_on_hit(
    client: TestClient,
    fake_service: _FakeReportService,
) -> None:
    """``X-Data-Version`` is set on BOTH miss and hit (per S9 spec)."""
    r1 = client.get("/api/v2/reports/whitepaper/pdf/th")
    assert r1.headers.get("x-data-version") not in (None, "")
    r2 = client.get("/api/v2/reports/whitepaper/pdf/th")
    # Same data → same data_version on both responses.
    assert r2.headers.get("x-data-version") == r1.headers.get("x-data-version")


