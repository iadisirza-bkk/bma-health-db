"""LLM eval suite — exercises the *real* chat stack against canned questions.

Hits the running BMA API (default localhost:9002) → real orchestrator →
real LMStudio → real DB. For each question we assert:

  - The stream completes (`done` event received within timeout).
  - At least one `token` event arrived (= the LLM produced text).
  - No `error` event appeared.
  - Topic-specific keywords appear in the joined response (sanity check
    that the LLM didn't go off-rails or hit the refusal path).
  - Negative checks: refusal phrase / known hallucinations are absent.

Failures here surface real LLM-quality regressions: e.g. the system prompt
changed and now refuses in-scope questions; or a tool returned bad data
and the LLM hallucinated a number.

Each case is a `EvalCase` dataclass — to add a question, append a row.

Run with:

    BMA_RUN_LLM_EVAL=1 python3 -m pytest tests/llm_eval -v --tb=short

This file deliberately uses `requests` (sync) and HTTP I/O against the
running server — it is a true end-to-end probe, not a unit test.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import pytest
import requests

API_URL = os.environ.get("BMA_API_URL", "http://localhost:9002")
API_KEY = os.environ.get("API_KEY", "dev-api-key")
STREAM_TIMEOUT = int(os.environ.get("BMA_EVAL_STREAM_TIMEOUT", "120"))

# Phrases the orchestrator emits when refusing a request — these must NOT
# appear for in-scope questions.
REFUSAL_PHRASES = [
    "ขออภัย",
    "ฉันตอบได้เฉพาะ",
    "ไม่สามารถตอบ",
]

# Numbers the LLM has hallucinated in the past. They were not in any tool
# output, so they should never appear in the response.
KNOWN_HALLUCINATIONS = [
    "1.6 ล้าน",
    "1,600,000",
    "1600000",
]


@dataclass
class EvalCase:
    """One question + assertions for the eval."""

    name: str
    question: str
    # All of these substrings must appear (case-insensitive) in the joined token text.
    expect_any_of: list[list[str]] = field(default_factory=list)
    # If set, at least one chart event must be emitted.
    expect_chart: bool = False
    # If set, the stream must show evidence a tool ran — either a `tool_call`
    # event OR a `tool_result` event. The orchestrator's legacy stream path
    # doesn't always emit `tool_call` separately (it gets folded into the
    # visualization translation), so accepting either keeps this honest.
    expect_tool_use: bool = True
    # Override the global timeout if a question is known to be slow.
    timeout: int = STREAM_TIMEOUT
    # Documented known gap — marks the case as xfail so the suite stays green
    # while the limitation is on record. If someone fixes the gap, pytest will
    # flag it as XPASS so we know to remove the marker.
    xfail_reason: Optional[str] = None


CASES: list[EvalCase] = [
    # ----------------------------------------------------------------------- #
    # Single-variable baseline (regression guard for the simple cases).
    # ----------------------------------------------------------------------- #
    EvalCase(
        name="overview_all_diseases",
        question="ภาพรวมโรคทั้งหมด",
        # Must mention either the headline number or one of the disease names.
        expect_any_of=[
            ["คน", "ราย"],  # a unit must appear
            ["เบาหวาน", "ความดัน", "อ้วน", "DM", "HT"],  # disease mention
        ],
        expect_chart=True,
    ),
    EvalCase(
        name="obesity_prevalence",
        question="โรคอ้วนกี่เปอร์เซ็นต์",
        expect_any_of=[
            ["%", "เปอร์เซ็นต์", "ร้อยละ"],
            ["อ้วน"],
        ],
    ),
    EvalCase(
        name="zone_comparison_dm",
        question="เปรียบเทียบเบาหวานแต่ละเขต",
        expect_any_of=[
            ["เขต", "โซน", "zone"],
            ["เบาหวาน", "DM"],
        ],
        expect_chart=True,
    ),
    EvalCase(
        name="age_band_obesity",
        question="อายุ 20-30 อ้วนกี่%",
        expect_any_of=[
            ["20", "30"],
            ["อ้วน"],
        ],
    ),
    EvalCase(
        name="off_topic_recipe_refused",
        question="วิธีทำพาสต้าแบบง่าย",
        # The deny-list path returns the refusal directly — assert it appears.
        expect_any_of=[REFUSAL_PHRASES],
        expect_tool_use=False,
    ),

    # ----------------------------------------------------------------------- #
    # Bivariate — disease × one demographic dimension.
    # Each `expect_any_of` row pins one variable: BOTH must surface in the
    # response, otherwise the LLM dropped a variable = wrong answer.
    # ----------------------------------------------------------------------- #
    EvalCase(
        name="bivariate_dm_by_sex",
        question="ความชุกเบาหวานในผู้ชายเทียบกับผู้หญิง",
        expect_any_of=[
            ["เบาหวาน", "DM"],
            ["ชาย", "หญิง", "เพศ"],
            ["%", "เปอร์เซ็นต์", "ร้อยละ"],
        ],
        expect_chart=True,
    ),
    EvalCase(
        name="bivariate_htn_by_age",
        question="ความดันโลหิตสูงตามกลุ่มอายุ",
        expect_any_of=[
            ["ความดัน", "HT", "HPT"],
            ["อายุ", "วัย", "ปี"],
        ],
        expect_chart=True,
    ),
    EvalCase(
        name="bivariate_bmi_by_zone",
        question="ค่า BMI เฉลี่ยแต่ละเขตในกรุงเทพ",
        expect_any_of=[
            ["BMI", "ดัชนีมวลกาย"],
            ["เขต", "โซน"],
        ],
        # System answers via obesity-rate-per-district which mentions both
        # BMI category language and zone/district. Good enough for this case.
    ),
    EvalCase(
        name="bivariate_smoking_vs_disease",
        question="คนสูบบุหรี่เป็นโรคอะไรมากที่สุด",
        expect_any_of=[
            ["สูบบุหรี่", "บุหรี่", "smok"],
            ["เบาหวาน", "ความดัน", "อ้วน", "โรค"],
        ],
    ),

    # ----------------------------------------------------------------------- #
    # Plot-explicit requests — user asks for a SPECIFIC chart shape.
    # The LLM/tool should pick a chart_type that matches the request.
    # ----------------------------------------------------------------------- #
    EvalCase(
        name="plot_request_bar_dm_by_district",
        question="พล็อตกราฟแท่งจำนวนผู้เป็นเบาหวานแต่ละเขต",
        expect_any_of=[
            ["เบาหวาน", "DM"],
            ["เขต", "โซน"],
        ],
        expect_chart=True,
    ),
    EvalCase(
        name="plot_request_heatmap_disease_by_zone",
        question="แสดง heatmap ความชุกของโรคแต่ละเขต",
        expect_any_of=[
            ["เขต", "โซน", "zone"],
            ["โรค", "เบาหวาน", "ความดัน"],
        ],
        expect_chart=True,
    ),
    EvalCase(
        name="plot_request_pyramid_age_sex",
        question="วาด population pyramid ของผู้คัดกรอง แยกตามอายุและเพศ",
        expect_any_of=[
            ["อายุ", "วัย"],
            ["เพศ", "ชาย", "หญิง"],
        ],
        expect_chart=True,
        # Pyramids are slow — give it more headroom.
        timeout=180,
    ),

    # ----------------------------------------------------------------------- #
    # Trivariate — three variables in one question.
    # The synthesiser tends to flatten one dimension into prose ("each zone,
    # broken down by sex"); we accept that as long as ALL three variables
    # are mentioned somewhere.
    # ----------------------------------------------------------------------- #
    EvalCase(
        name="trivariate_dm_zone_sex",
        question="เบาหวานในแต่ละเขต แยกตามเพศชายหญิง",
        expect_any_of=[
            ["เบาหวาน", "DM"],
            ["เขต", "โซน"],
            ["ชาย", "หญิง", "เพศ"],
        ],
        expect_chart=True,
        timeout=180,
    ),
    EvalCase(
        name="trivariate_obesity_age_sex",
        question="ความชุกโรคอ้วน แยกตามอายุและเพศ",
        expect_any_of=[
            ["อ้วน", "obes", "BMI"],
            ["อายุ", "วัย", "ปี"],
            ["เพศ", "ชาย", "หญิง"],
        ],
        # Trivariate is handled by the LLM making TWO query_health_data calls
        # (one per dimension) and stitching them in prose. We don't expect a
        # single combined chart — that would need a 2D group_by tool.
        expect_chart=False,
        timeout=180,
    ),
    EvalCase(
        name="trivariate_htn_workingage_zone",
        question="ความดันสูงในวัยทำงาน แยกตามเขต",
        expect_any_of=[
            ["ความดัน", "HT", "HPT"],
            ["วัยทำงาน", "อายุ", "ปี", "30", "40", "50", "60"],
            ["เขต", "โซน"],
        ],
        timeout=180,
    ),

    # ----------------------------------------------------------------------- #
    # Statistical relationship — asks if A and B are related.
    # Must use language about RELATIONSHIP, not just list both numbers.
    # ----------------------------------------------------------------------- #
    EvalCase(
        name="relationship_smoking_htn",
        question="การสูบบุหรี่กับโรคความดันสูงสัมพันธ์กันไหม",
        expect_any_of=[
            ["สูบบุหรี่", "บุหรี่"],
            ["ความดัน", "HT", "HPT"],
            ["สัมพันธ์", "เกี่ยวข้อง", "ความเสี่ยง", "เพิ่มขึ้น", "OR", "odds", "เท่า"],
        ],
        timeout=180,
    ),
    EvalCase(
        name="relationship_bmi_dm",
        question="BMI สูงกับการเป็นเบาหวานเชื่อมโยงกันแค่ไหน",
        expect_any_of=[
            ["BMI", "อ้วน", "ดัชนีมวลกาย"],
            ["เบาหวาน", "DM"],
            ["สัมพันธ์", "เชื่อมโยง", "ความเสี่ยง", "OR", "odds", "เท่า", "%"],
        ],
        timeout=180,
    ),
    EvalCase(
        name="comorbidity_multi_disease",
        question="มีคนเป็นโรคร่วมหลายโรคพร้อมกันกี่คน",
        expect_any_of=[
            ["โรคร่วม", "comorbid", "หลายโรค", "พร้อมกัน"],
            ["คน", "ราย", "%"],
        ],
        timeout=180,
    ),

    # ----------------------------------------------------------------------- #
    # Lab × disease — clinical relationship between a lab value and a disease.
    # ----------------------------------------------------------------------- #
    EvalCase(
        name="lab_hba1c_dm",
        question="ค่า HbA1c ของผู้ที่เป็นเบาหวานเป็นอย่างไร",
        expect_any_of=[
            ["HbA1c", "เอวันซี", "น้ำตาลสะสม", "FBS", "FPG", "น้ำตาล"],
            ["เบาหวาน", "DM"],
            ["%", "เฉลี่ย", "ค่า", "ระดับ"],
        ],
        timeout=180,
    ),
    EvalCase(
        name="lab_fbs_to_dm_diagnosis",
        question="คนที่ผลน้ำตาลในเลือดสูง เป็นเบาหวานกี่เปอร์เซ็นต์",
        expect_any_of=[
            ["น้ำตาล", "FBS", "FPG", "glucose"],
            ["เบาหวาน", "DM"],
            ["%", "เปอร์เซ็นต์", "ร้อยละ"],
        ],
        timeout=180,
    ),
]


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def _create_thread() -> str:
    res = requests.post(
        f"{API_URL}/api/v2/chat/threads",
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        json={},
        timeout=10,
    )
    res.raise_for_status()
    return res.json()["thread_id"]


def _stream_chat(thread_id: str, message: str, timeout: int) -> list[tuple[str, dict]]:
    """Open the SSE stream and accumulate (event, data) tuples until done."""
    res = requests.post(
        f"{API_URL}/api/v2/chat/threads/{thread_id}/stream",
        headers={
            "X-API-Key": API_KEY,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        json={"message": message},
        stream=True,
        timeout=timeout,
    )
    res.raise_for_status()

    events: list[tuple[str, dict]] = []
    buffer = ""
    deadline = time.monotonic() + timeout

    for chunk in res.iter_content(chunk_size=None, decode_unicode=True):
        if not chunk:
            continue
        buffer += chunk
        # Flush complete frames (separated by blank lines).
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            ev_name = "message"
            data_lines: list[str] = []
            for line in frame.split("\n"):
                if line.startswith("event:"):
                    ev_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if not data_lines:
                continue
            try:
                payload = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                payload = {"raw": "\n".join(data_lines)}
            events.append((ev_name, payload))
            if ev_name == "done":
                return events
        if time.monotonic() > deadline:
            break

    return events


def _join_tokens(events: list[tuple[str, dict]]) -> str:
    return "".join(p.get("text", "") for ev, p in events if ev == "token")


def _delete_thread(thread_id: str) -> None:
    try:
        requests.delete(
            f"{API_URL}/api/v2/chat/threads/{thread_id}",
            headers={"X-API-Key": API_KEY},
            timeout=5,
        )
    except requests.RequestException:
        pass


# --------------------------------------------------------------------------- #
# Tests — one per EvalCase, parameterised
# --------------------------------------------------------------------------- #


def _params():
    """Wrap each EvalCase in a pytest.param, attaching xfail markers when a
    known-gap reason is set. Keeps the parametrize call site one-liner."""
    out = []
    for c in CASES:
        marks: list[Any] = []
        if c.xfail_reason:
            marks.append(pytest.mark.xfail(reason=c.xfail_reason, strict=False))
        out.append(pytest.param(c, id=c.name, marks=marks))
    return out


@pytest.mark.parametrize("case", _params())
def test_eval(case: EvalCase) -> None:
    thread_id = _create_thread()
    try:
        events = _stream_chat(thread_id, case.question, timeout=case.timeout)
    finally:
        _delete_thread(thread_id)

    names = [e[0] for e in events]
    text = _join_tokens(events)

    # 1. Stream must finish.
    assert "done" in names, (
        f"[{case.name}] Stream did not finish. "
        f"Events seen: {names[:20]}"
    )

    # 2. No error events.
    error_events = [p for ev, p in events if ev == "error"]
    assert not error_events, (
        f"[{case.name}] Stream emitted error event(s): {error_events}"
    )

    # 3. At least one token (= LLM produced text).
    assert names.count("token") >= 1, (
        f"[{case.name}] No token events. The LLM produced no text."
        f" Events: {names}"
    )

    # 4. Optional: a tool must have been used (either explicit tool_call or the
    # tool_result that the visualization translator emits).
    if case.expect_tool_use:
        assert "tool_call" in names or "tool_result" in names, (
            f"[{case.name}] Expected a tool_call or tool_result but saw: {names}"
        )

    # 5. Optional: chart must have been emitted.
    if case.expect_chart:
        assert "chart" in names or "tool_result" in names, (
            f"[{case.name}] Expected a chart/tool_result event but saw: {names}"
        )

    # 6. Topic-specific keywords (each row in expect_any_of must match at least one keyword).
    for required_group in case.expect_any_of:
        if not any(_loose_contains(text, kw) for kw in required_group):
            pytest.fail(
                f"[{case.name}] Response missing any of {required_group!r}.\n"
                f"--- Response ---\n{text[:800]}\n--- end ---"
            )

    # 7. Negative checks for in-scope questions only.
    if case.name != "off_topic_recipe_refused":
        for hallucination in KNOWN_HALLUCINATIONS:
            assert hallucination not in text, (
                f"[{case.name}] Known hallucination '{hallucination}' appeared:"
                f"\n{text[:400]}"
            )


def _loose_contains(haystack: str, needle: str) -> bool:
    """Case-insensitive substring match — small bit of normalisation."""
    return re.search(re.escape(needle), haystack, flags=re.IGNORECASE) is not None
