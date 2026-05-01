"""Tests for the two ADR-03 §3 bridge blocks introduced in S4.6:

* ``statistical_test_results`` — variable-shape table that branches on
  ``test_type`` (odds_ratio / logistic_regression / correlation /
  chi_square / t_test). Per-type column headers, per-row significance
  bolding, missing-fields fallback to ``"—"``.

* ``ai_insight`` — short Thai prose synthesised from the report data
  via ``ChatService`` (or, if no chat_service is injected and the
  legacy orchestrator import fails, a static fallback string). Cache
  hits avoid re-querying the LLM. LaTeX-injection sanitisation must
  neutralise an LLM response containing macro-injection attempts.

The tests do NOT call a real ChatService — they inject an ``AsyncMock``
into ``ctx.extra['chat_service']`` and assert on its
``await_count`` / ``call_args``. The orchestrator-fallback path is
exercised by patching ``agents.create_orchestrator`` to raise inside the
block's lazy import.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

# Make ``api/`` importable for ``services.reports.*``.
_API_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "api"
)
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.reports.blocks import (  # noqa: E402
    AiInsightBlock,
    AiInsightParams,
    StatisticalTestResultsBlock,
    StatTestParams,
)
from services.reports.spec import (  # noqa: E402
    RenderContext,
    ReportDescriptor,
    SectionSpec,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


class _FakeDataCollector:
    """Mirror of the fixture used by ``test_blocks.py`` — a flat dict
    behind a ``.data()`` accessor."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def data(self) -> Dict[str, Any]:
        return self._payload


def _descriptor() -> ReportDescriptor:
    return ReportDescriptor(
        report_id="t",
        title_th="t",
        formats=["latex", "html"],
        languages=["th"],
        sections=[SectionSpec(id="s", block="heading")],
    )


def _ctx(
    payload: Dict[str, Any] | None = None,
    extra: Dict[str, Any] | None = None,
    lang: str = "th",
) -> RenderContext:
    return RenderContext(
        data_collector=_FakeDataCollector(payload or {}),
        lang=lang,
        fmt="latex",
        descriptor=_descriptor(),
        requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        extra=extra or {},
    )


# ===========================================================================
# statistical_test_results
# ===========================================================================


def _stat_test_payload() -> Dict[str, Any]:
    """Mixed test types: 3 odds_ratio + 2 logistic_regression + 1 correlation.

    p-values straddle the 0.05 threshold so the bolding test has both
    sides to assert on.
    """
    return {
        "statistical_tests": [
            # 3 odds_ratio rows
            {
                "test_type": "odds_ratio",
                "name": "or_age",
                "factor": "อายุ ≥ 60",
                "or_value": 2.34,
                "ci_lower": 1.45,
                "ci_upper": 3.78,
                "p_value": 0.001,  # significant -> bold
            },
            {
                "test_type": "odds_ratio",
                "name": "or_smoke",
                "factor": "สูบบุหรี่",
                "or_value": 1.18,
                "ci_lower": 0.92,
                "ci_upper": 1.51,
                "p_value": 0.18,  # not significant
            },
            {
                "test_type": "odds_ratio",
                "name": "or_pm25",
                "factor": "PM2.5 สูง",
                # Deliberately missing or_value -> renders "—"
                "ci_lower": 1.05,
                "ci_upper": 2.10,
                "p_value": 0.04,  # significant
            },
            # 2 logistic_regression rows
            {
                "test_type": "logistic_regression",
                "name": "lr_bmi",
                "predictor": "BMI",
                "beta": 0.18,
                "se": 0.05,
                "or_adjusted": 1.20,
                "p_value": 0.002,  # significant
            },
            {
                "test_type": "logistic_regression",
                "name": "lr_age",
                "predictor": "อายุ",
                "beta": 0.04,
                "se": 0.07,
                # or_adjusted intentionally absent
                "p_value": 0.55,  # not significant
            },
            # 1 correlation row
            {
                "test_type": "correlation",
                "name": "corr_age_bmi",
                "pair": "อายุ ↔ BMI",
                "r": 0.42,
                "p_value": 0.0008,
                "n": 1234,
            },
            # A row of an excluded type (chi_square) — must be filtered
            # out by default include_types.
            {
                "test_type": "chi_square",
                "name": "chi_x",
                "pair": "x↔y",
                "chi2": 5.5,
                "df": 2,
                "p_value": 0.06,
            },
        ]
    }


@pytest.mark.anyio
async def test_stat_block_buckets_rows_by_type() -> None:
    block = StatisticalTestResultsBlock()
    params = StatTestParams()
    ctx = _ctx(payload=_stat_test_payload())
    data = await block.collect(ctx, params)
    by_type = data["by_type"]
    # default include_types = odds_ratio + logistic_regression + correlation;
    # chi_square row should NOT show up.
    assert set(by_type.keys()) == {
        "odds_ratio",
        "logistic_regression",
        "correlation",
    }
    assert len(by_type["odds_ratio"]) == 3
    assert len(by_type["logistic_regression"]) == 2
    assert len(by_type["correlation"]) == 1


@pytest.mark.anyio
async def test_stat_block_sorts_within_bucket_by_p_value() -> None:
    block = StatisticalTestResultsBlock()
    params = StatTestParams()
    ctx = _ctx(payload=_stat_test_payload())
    data = await block.collect(ctx, params)
    or_rows = data["by_type"]["odds_ratio"]
    p_values = [r["p_value"] for r in or_rows]
    assert p_values == sorted(p_values)


@pytest.mark.anyio
async def test_stat_block_latex_emits_one_subsection_per_type() -> None:
    block = StatisticalTestResultsBlock()
    params = StatTestParams()
    ctx = _ctx(payload=_stat_test_payload())
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    # Three subsections, each with the test-type's Thai title.
    assert out.count(r"\subsection*{") == 3
    assert "Odds Ratio" in out
    assert "Logistic" in out or "ถดถอย" in out  # title may translate
    assert "สหสัมพันธ์" in out  # correlation


@pytest.mark.anyio
async def test_stat_block_latex_columns_per_type() -> None:
    """odds_ratio row has 5 cols, correlation has 4, logistic has 5.

    Verified by counting ``&`` per row in the rendered output: an N-col
    row produces (N - 1) ampersands. We pick ONE row per type and count
    the ampersands in the line containing its known label text.
    """
    block = StatisticalTestResultsBlock()
    params = StatTestParams()
    ctx = _ctx(payload=_stat_test_payload())
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    or_line = next(line for line in out.splitlines() if "อายุ" in line and "60" in line)
    # odds_ratio has 5 columns -> 4 ampersands.
    assert or_line.count("&") == 4

    corr_line = next(
        line for line in out.splitlines() if "BMI" in line and "0.42" in line
    )
    # correlation has 4 columns -> 3 ampersands.
    assert corr_line.count("&") == 3

    lr_line = next(
        line for line in out.splitlines() if "BMI" in line and "0.18" in line
    )
    # logistic_regression has 5 columns -> 4 ampersands.
    assert lr_line.count("&") == 4


@pytest.mark.anyio
async def test_stat_block_latex_bolds_significant_rows() -> None:
    block = StatisticalTestResultsBlock()
    params = StatTestParams()
    ctx = _ctx(payload=_stat_test_payload())
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    # The "อายุ ≥ 60" odds_ratio row has p=0.001 (< 0.05) so its cells
    # must be wrapped in \textbf{...}.
    sig_line = next(line for line in out.splitlines() if "อายุ" in line and "60" in line)
    assert r"\textbf{" in sig_line
    # And the not-significant "สูบบุหรี่" row (p=0.18) must NOT be bold.
    nonsig_line = next(line for line in out.splitlines() if "สูบบุหรี่" in line)
    assert r"\textbf{" not in nonsig_line


@pytest.mark.anyio
async def test_stat_block_missing_field_renders_em_dash() -> None:
    """The third odds_ratio row has no ``or_value`` — must render "—"."""
    block = StatisticalTestResultsBlock()
    params = StatTestParams()
    ctx = _ctx(payload=_stat_test_payload())
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    # PM2.5 row has missing or_value -> the OR cell is "—"
    pm_line = next(line for line in out.splitlines() if "PM2.5" in line)
    assert "—" in pm_line


@pytest.mark.anyio
async def test_stat_block_html_per_type_section() -> None:
    block = StatisticalTestResultsBlock()
    params = StatTestParams()
    ctx = _ctx(payload=_stat_test_payload())
    data = await block.collect(ctx, params)
    html_out = block.render_html(data, params, ctx)
    # h3 for each type
    assert html_out.count("<h3>") == 3
    # class="stat-test stat-<type>" for each table
    assert 'class="stat-test stat-odds_ratio"' in html_out
    assert 'class="stat-test stat-logistic_regression"' in html_out
    assert 'class="stat-test stat-correlation"' in html_out
    # significant rows get class="significant"
    assert 'class="significant"' in html_out


@pytest.mark.anyio
async def test_stat_block_tolerates_missing_source_path() -> None:
    """Empty payload -> empty by_type dict, render returns sentinel text."""
    block = StatisticalTestResultsBlock()
    params = StatTestParams()
    ctx = _ctx(payload={})  # no statistical_tests key
    data = await block.collect(ctx, params)
    assert data["by_type"] == {}
    out = block.render_latex(data, params, ctx)
    assert "ไม่มีผลการทดสอบ" in out
    html_out = block.render_html(data, params, ctx)
    assert "ไม่มีผลการทดสอบ" in html_out


@pytest.mark.anyio
async def test_stat_block_tests_filter_keeps_only_named_rows() -> None:
    block = StatisticalTestResultsBlock()
    params = StatTestParams(tests=["or_age", "lr_bmi"])
    ctx = _ctx(payload=_stat_test_payload())
    data = await block.collect(ctx, params)
    flat = [r["name"] for rows in data["by_type"].values() for r in rows]
    assert sorted(flat) == ["lr_bmi", "or_age"]


@pytest.mark.anyio
async def test_stat_block_include_types_extends_to_chi_square() -> None:
    block = StatisticalTestResultsBlock()
    params = StatTestParams(include_types=["chi_square"])
    ctx = _ctx(payload=_stat_test_payload())
    data = await block.collect(ctx, params)
    assert set(data["by_type"]) == {"chi_square"}
    out = block.render_latex(data, params, ctx)
    assert "ไค-สแควร์" in out


# ===========================================================================
# ai_insight
# ===========================================================================


def _ai_payload() -> Dict[str, Any]:
    return {
        "zone_code": "Z1",
        "summary": {"total_screened": 12345, "msd_count": 12},
    }


def _make_chat_mock(reply: str = "เขต Z1 มีผู้คัดกรอง 12,345 ราย") -> AsyncMock:
    """Return an ``AsyncMock`` whose ``.chat(...)`` resolves to a dict."""
    mock = AsyncMock()
    mock.chat.return_value = {"content": reply, "visualizations": []}
    return mock


@pytest.mark.anyio
async def test_ai_insight_calls_chat_with_substituted_prompt() -> None:
    block = AiInsightBlock()
    params = AiInsightParams(
        prompt_template_th=(
            "สรุปข้อมูลสุขภาพของเขต {zone_code} ใน 3 ประเด็น"
        ),
        data_keys=["zone_code"],
    )
    chat_mock = _make_chat_mock("สรุป 3 ประเด็น...")
    ctx = _ctx(payload=_ai_payload(), extra={"chat_service": chat_mock})
    data = await block.collect(ctx, params)

    # The substituted prompt was passed to chat.
    assert chat_mock.chat.await_count == 1
    call_kwargs = chat_mock.chat.await_args.kwargs
    # chat_service is invoked with thread_id=None and the formatted prompt.
    assert call_kwargs.get("thread_id") is None
    assert "Z1" in call_kwargs["user_message"]
    assert data["source"] == "llm"
    assert data["text"] == "สรุป 3 ประเด็น..."
    assert "{zone_code}" not in data["prompt_used"]


@pytest.mark.anyio
async def test_ai_insight_cache_hit_avoids_second_call() -> None:
    block = AiInsightBlock()
    params = AiInsightParams(
        prompt_template_th="สรุปเขต {zone_code}",
        data_keys=["zone_code"],
    )
    chat_mock = _make_chat_mock("ตอบครั้งเดียว")
    extra = {"chat_service": chat_mock}
    ctx = _ctx(payload=_ai_payload(), extra=extra)

    a = await block.collect(ctx, params)
    b = await block.collect(ctx, params)

    assert chat_mock.chat.await_count == 1
    assert a["text"] == b["text"] == "ตอบครั้งเดียว"
    # Cache bucket exists on extra after the first call.
    assert "__ai_insight_cache" in extra


@pytest.mark.anyio
async def test_ai_insight_falls_back_when_no_chat_and_import_fails() -> None:
    """No ctx.extra['chat_service'] AND legacy import raises -> fallback."""
    block = AiInsightBlock()
    params = AiInsightParams(
        prompt_template_th="สรุปเขต {zone_code}",
        data_keys=["zone_code"],
        fallback_text_th="ไม่พร้อมใช้งาน",
    )
    ctx = _ctx(payload=_ai_payload(), extra={})  # no chat_service

    # Patch the lazy `from agents import create_orchestrator` so it raises.
    with patch.dict(sys.modules, {"agents": None}):
        # ``sys.modules['agents'] = None`` triggers ImportError on
        # subsequent ``import agents`` / ``from agents import …`` calls.
        data = await block.collect(ctx, params)

    assert data["source"] == "fallback"
    assert data["text"] == "ไม่พร้อมใช้งาน"


@pytest.mark.anyio
async def test_ai_insight_latex_escapes_macro_injection() -> None:
    """LLM returns ``\\input{evil}`` — must NOT be a live macro."""
    block = AiInsightBlock()
    params = AiInsightParams(
        prompt_template_th="prompt",
        data_keys=[],
    )
    chat_mock = _make_chat_mock(
        "ปกติ \\input{evil} และ %comment ตามด้วย $x_1$"
    )
    ctx = _ctx(extra={"chat_service": chat_mock})
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    # The literal `\input{evil}` must be escaped — NOT present as a
    # bare macro. After ``latex_escape`` we expect
    # ``\textbackslash{}input\{evil\}``.
    assert r"\input{evil}" not in out
    assert r"\textbackslash{}input\{evil\}" in out
    # `%` should be escaped to `\%` so the rest of the line doesn't
    # become a LaTeX comment.
    assert r"\%comment" in out
    # `$` should be escaped so math mode doesn't open.
    assert r"\$x\_1\$" in out


@pytest.mark.anyio
async def test_ai_insight_html_escapes_html_special_chars() -> None:
    block = AiInsightBlock()
    params = AiInsightParams(prompt_template_th="prompt", data_keys=[])
    chat_mock = _make_chat_mock(
        "ตัวอย่าง <script>alert('x')</script> & ลิงก์"
    )
    ctx = _ctx(extra={"chat_service": chat_mock})
    data = await block.collect(ctx, params)
    out = block.render_html(data, params, ctx)
    # The <script> opening tag must be escaped, and the literal text
    # must be present in escaped form.
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp; ลิงก์" in out
    assert '<blockquote class="ai-insight">' in out
    assert "ที่มา: AI สรุป" in out


@pytest.mark.anyio
async def test_ai_insight_latex_includes_attribution_block() -> None:
    block = AiInsightBlock()
    params = AiInsightParams(prompt_template_th="x", data_keys=[])
    chat_mock = _make_chat_mock("ผลสรุป")
    ctx = _ctx(extra={"chat_service": chat_mock})
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    assert r"\begin{quote}" in out
    assert r"\end{quote}" in out
    assert "ที่มา" in out and "LMStudio" in out


@pytest.mark.anyio
async def test_ai_insight_chat_failure_falls_back_silently() -> None:
    """If chat.chat() raises, block returns fallback text — never bubbles."""
    block = AiInsightBlock()
    params = AiInsightParams(
        prompt_template_th="prompt",
        data_keys=[],
        fallback_text_th="ขออภัย ไม่พร้อมใช้งาน",
    )
    chat_mock = AsyncMock()
    chat_mock.chat.side_effect = RuntimeError("LLM is down")
    ctx = _ctx(extra={"chat_service": chat_mock})
    data = await block.collect(ctx, params)
    assert data["source"] == "fallback"
    assert data["text"] == "ขออภัย ไม่พร้อมใช้งาน"


@pytest.mark.anyio
async def test_ai_insight_data_keys_substitution_only_top_level() -> None:
    """Only top-level keys of data() are honoured — dotted-path attempts
    are NOT resolved (keeps the prompt template surface minimal)."""
    block = AiInsightBlock()
    params = AiInsightParams(
        prompt_template_th=(
            "ผู้คัดกรอง {summary} เขต {zone_code} แต่ {missing_key} หาย"
        ),
        data_keys=["summary", "zone_code", "missing_key"],
    )
    chat_mock = _make_chat_mock("ack")
    ctx = _ctx(payload=_ai_payload(), extra={"chat_service": chat_mock})
    await block.collect(ctx, params)
    sent_prompt = chat_mock.chat.await_args.kwargs["user_message"]
    # ``summary`` (a dict) was forwarded as-is — Python ``str.format``
    # stringifies it. ``zone_code`` is the literal "Z1". ``missing_key``
    # surfaces as the literal "{missing_key}" so a typo is visible.
    assert "Z1" in sent_prompt
    assert "{missing_key}" in sent_prompt or "missing_key" in sent_prompt


@pytest.mark.anyio
async def test_ai_insight_empty_response_falls_back() -> None:
    """An LLM that returns empty content -> fallback path engages."""
    block = AiInsightBlock()
    params = AiInsightParams(
        prompt_template_th="prompt",
        data_keys=[],
        fallback_text_th="empty fallback",
    )
    chat_mock = AsyncMock()
    chat_mock.chat.return_value = {"content": "", "visualizations": []}
    ctx = _ctx(extra={"chat_service": chat_mock})
    data = await block.collect(ctx, params)
    assert data["source"] == "fallback"
    assert data["text"] == "empty fallback"
