"""Safety-filter test corpus for the S9 TextPolishService.

The service is fail-closed: any output that mentions medical advice,
specific drugs, asserts causation, drops/changes a number, or differs
in length by more than 2× from the input is rejected, and the original
text is returned instead.

These tests pin the corpus we expect the safety filter to catch. The
corpus is intentionally over-inclusive — false positives just mean the
report renders the original template (which is the safe default).

The tests do NOT call Gemma; they invoke ``_safety_filter`` directly
with a hand-crafted ``output`` to verify the rejection / acceptance
logic in isolation.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make ``api/`` importable for ``services.reports.*``.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.reports.polish import (  # noqa: E402
    PolishRequest,
    TextPolishService,
    _safety_violations,
)


# ---------------------------------------------------------------------------
# Fixture: a service backed by a throwaway SQLite cache. The cache is not
# touched in these tests — we call ``_safety_filter`` directly — but the
# service constructor opens the file regardless, so we redirect it to a
# tmp dir to keep the repo clean.
# ---------------------------------------------------------------------------


@pytest.fixture()
def svc(tmp_path: Path) -> TextPolishService:
    return TextPolishService(
        cache_path=tmp_path / "cache.sqlite",
    )


# ---------------------------------------------------------------------------
# Reject corpus — each entry pairs an "output" Gemma might return with
# the input it was supposed to polish, and the human-readable category
# of violation we expect the filter to flag.
# ---------------------------------------------------------------------------


REJECT_CASES: list[tuple[str, str, str, str]] = [
    # (input_text, polished_output, lang, reason)
    (
        "ผู้สูงอายุควรตรวจน้ำตาลเป็นประจำ",
        "แนะนำให้กินยาลดความดันทุกวัน",
        "th",
        "medical advice (gives drug instruction)",
    ),
    (
        "การคัดกรองช่วยให้ทราบความเสี่ยงเร็วขึ้น",
        "ควรไปพบแพทย์ทันที",
        "th",
        "medical advice (urges doctor visit)",
    ),
    (
        "อัตราเสี่ยงเบาหวาน 23.4% ในกลุ่ม 60+",
        "คนเสี่ยงเบาหวานเพราะกินหวานเยอะ จึงต้องระวัง",
        "th",
        "causation claim (data is correlation only)",
    ),
    (
        "อัตราเสี่ยงเบาหวาน 23.4% ในกลุ่ม 60+",
        "อัตราเสี่ยงเบาหวาน 30% ในกลุ่ม 60+",
        "th",
        "number changed",
    ),
    (
        "ผู้ป่วยเบาหวานบางรายต้องการการดูแลพิเศษ",
        "ใช้ insulin ชนิดที่เหมาะสม",
        "th",
        "drug mention",
    ),
    (
        "การคัดกรองความดันที่บ้านมีประโยชน์",
        "คุณต้องตรวจน้ำตาลทุกเดือน",
        "th",
        "medical advice (directive personal command)",
    ),
    (
        "Diabetes risk varies across districts.",
        "You should take medication daily for diabetes.",
        "en",
        "medical advice (en)",
    ),
    (
        "Hypertension prevalence is rising.",
        "Hypertension causes strokes in elderly patients.",
        "en",
        "causation claim (en)",
    ),
    (
        "Some patients need careful follow-up.",
        "Treat with metformin twice daily.",
        "en",
        "drug mention (en)",
    ),
    (
        "ผลคัดกรองเฉลี่ย 12 ราย",
        "ผลคัดกรองเฉลี่ย 12 ราย โปรดทานยา atorvastatin หากจำเป็น",
        "th",
        "drug mention (en drug name in th text)",
    ),
    (
        "ผู้คัดกรอง 100 ราย จำนวนมากในเขตนี้และมีรายละเอียดต่อไป",
        "ผู้คัดกรอง 100",  # 14 chars vs ~70 chars → length out of band
        "th",
        "length out of band (way shorter)",
    ),
    (
        "เขต 03 มีอัตราคัดกรอง 45%",
        "เขต 03 มีอัตราคัดกรอง",  # 45% dropped
        "th",
        "number changed (dropped)",
    ),
    (
        "ผู้สูงอายุ 1 ใน 4 อาจเสี่ยงเบาหวาน",
        "ท่านควรไปพบแพทย์โดยด่วน",
        "th",
        "medical advice (formal directive)",
    ),
]


@pytest.mark.parametrize(
    "text, polished, lang, reason",
    REJECT_CASES,
    ids=[c[3] for c in REJECT_CASES],
)
def test_safety_filter_rejects(
    svc: TextPolishService,
    text: str,
    polished: str,
    lang: str,
    reason: str,
) -> None:
    """Every reject case must produce ≥1 violation."""
    req = PolishRequest(
        text=text, context_hint="test", lang=lang
    )
    # Direct violations check first — clarifies what tripped.
    violations = _safety_violations(polished, req)
    assert violations, (
        f"expected violations for {reason!r}; got none. "
        f"input={text!r} output={polished!r}"
    )
    # Then the public surface: filter must return the ORIGINAL text.
    out = svc._safety_filter(polished, req)
    assert out == text, (
        f"expected fallback to original for {reason!r}; got {out!r}"
    )


# ---------------------------------------------------------------------------
# Accept corpus — outputs the filter MUST allow through unchanged.
# Each output preserves every number from the input and avoids the
# rejected-pattern surface.
# ---------------------------------------------------------------------------


ACCEPT_CASES: list[tuple[str, str, str, str]] = [
    # (input_text, polished_output, lang, why_it_should_pass)
    (
        "ผู้สูงอายุประมาณ 1 ใน 4 อาจมีความเสี่ยงเบาหวาน "
        "การตรวจน้ำตาลเป็นประจำจะช่วยให้รู้แต่เนิ่น",
        "ผู้สูงอายุประมาณ 1 ใน 4 อาจมีความเสี่ยงเบาหวาน "
        "การตรวจน้ำตาลอย่างสม่ำเสมอจะช่วยให้รู้แต่เนิ่น",
        "th",
        "general health info, no advice",
    ),
    (
        "ตรวจวัดความดันที่บ้านได้ที่สถานีอนามัยใกล้บ้าน",
        "ตรวจวัดความดันที่บ้านได้ที่สถานีอนามัยใกล้บ้านของท่าน",
        "th",
        "informational, no directive",
    ),
    (
        "อัตราคัดกรองในเขต 03 อยู่ที่ 45%",
        "อัตราคัดกรองในเขต 03 อยู่ที่ 45%",
        "th",
        "verbatim numeric statement",
    ),
    (
        "Screening reached 12345 individuals.",
        "Screening reached 12345 individuals across the city.",
        "en",
        "factual statement (en)",
    ),
    (
        "ความเสี่ยงเบาหวานพบมากในกลุ่มอายุ 60 ปีขึ้นไป",
        "ความเสี่ยงเบาหวานพบมากในกลุ่มอายุ 60 ปีขึ้นไป",
        "th",
        "verbatim risk-group statement",
    ),
    (
        "การคัดกรองครอบคลุม 50 เขต",
        "การคัดกรองครอบคลุมทั้งสิ้น 50 เขต",
        "th",
        "epidemiological description",
    ),
    (
        "Hypertension prevalence varies across the 50 districts.",
        "Hypertension prevalence varies across the 50 districts of Bangkok.",
        "en",
        "factual variation statement (en)",
    ),
]


@pytest.mark.parametrize(
    "text, polished, lang, why",
    ACCEPT_CASES,
    ids=[c[3] for c in ACCEPT_CASES],
)
def test_safety_filter_accepts(
    svc: TextPolishService,
    text: str,
    polished: str,
    lang: str,
    why: str,
) -> None:
    """Every accept case must produce zero violations + return polished."""
    req = PolishRequest(
        text=text, context_hint="test", lang=lang
    )
    violations = _safety_violations(polished, req)
    assert violations == [], (
        f"expected no violations for {why!r}; got {violations!r}"
    )
    out = svc._safety_filter(polished, req)
    assert out == polished.strip(), (
        f"expected polished output for {why!r}; got {out!r}"
    )


# ---------------------------------------------------------------------------
# Cache + hash-key tests — the "drift impossible" guarantee from S9.
# ---------------------------------------------------------------------------


def test_hash_key_stable_across_calls(svc: TextPolishService) -> None:
    """Identical request → identical 32-hex-char key. Different fields
    → different keys."""
    a = PolishRequest(text="hi", context_hint="x", lang="th")
    b = PolishRequest(text="hi", context_hint="x", lang="th")
    c = PolishRequest(text="hi", context_hint="x", lang="en")
    assert svc._hash_key(a) == svc._hash_key(b)
    assert svc._hash_key(a) != svc._hash_key(c)
    assert len(svc._hash_key(a)) == 32


def test_cache_put_get_roundtrip(svc: TextPolishService) -> None:
    """SQLite cache write + read returns same value."""
    svc._cache_put("k", "polished")
    assert svc._cache_get("k") == "polished"
    assert svc._cache_get("missing") is None


@pytest.mark.anyio
async def test_polish_uses_cache_on_second_call(
    svc: TextPolishService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second identical call must hit the cache (no second LLM call)."""
    call_count = {"n": 0}

    async def _fake_call(req: PolishRequest) -> str:  # type: ignore[unused-argument]
        call_count["n"] += 1
        return req.text + "!"  # benign mutation, passes safety filter

    monkeypatch.setattr(svc, "_call_gemma", _fake_call)

    req = PolishRequest(
        text="ทดสอบแคช", context_hint="cache test", lang="th"
    )
    out1 = await svc.polish(req)
    out2 = await svc.polish(req)
    assert out1 == out2
    assert call_count["n"] == 1, (
        f"expected exactly 1 LLM call (rest cached), got {call_count['n']}"
    )


@pytest.mark.anyio
async def test_polish_returns_original_on_gemma_error(
    svc: TextPolishService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Gemma raises, ``polish`` must fail-closed → return original."""

    async def _fail(req: PolishRequest) -> str:  # type: ignore[unused-argument]
        raise RuntimeError("LMStudio offline")

    monkeypatch.setattr(svc, "_call_gemma", _fail)

    req = PolishRequest(
        text="original", context_hint="t", lang="th"
    )
    out = await svc.polish(req)
    assert out == "original"


@pytest.mark.anyio
async def test_polish_returns_original_on_safety_violation(
    svc: TextPolishService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Gemma returns medical advice, polish returns ORIGINAL."""

    async def _bad(req: PolishRequest) -> str:  # type: ignore[unused-argument]
        return "ควรกินยาลดความดันทุกวัน"

    monkeypatch.setattr(svc, "_call_gemma", _bad)

    req = PolishRequest(
        text="ความดันโลหิตสูงพบบ่อยในผู้สูงอายุ",
        context_hint="health summary",
        lang="th",
    )
    out = await svc.polish(req)
    # Reject → fallback to original
    assert out == "ความดันโลหิตสูงพบบ่อยในผู้สูงอายุ"


# ---------------------------------------------------------------------------
# Integration smoke (Task 3.4) — only runs when LMSTUDIO_URL is set.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.skipif(
    not os.environ.get("LMSTUDIO_URL"),
    reason="needs LMStudio (set LMSTUDIO_URL=http://localhost:5555/v1)",
)
async def test_polish_real_gemma_roundtrip(tmp_path: Path) -> None:
    """End-to-end with a live Gemma. Verifies non-empty output, key term
    preservation, and that the second identical call is a cache hit."""
    svc = TextPolishService(
        lmstudio_url=os.environ["LMSTUDIO_URL"],
        cache_path=tmp_path / "smoke.sqlite",
    )
    text = "ในเขต 03 มีคนเสี่ยงเบาหวานสูงกว่าค่าเฉลี่ย"
    req = PolishRequest(
        text=text, context_hint="zone summary", lang="th"
    )
    out = await svc.polish(req)
    assert out, "expected non-empty polished text"
    # Numeric "03" + key term "เบาหวาน" must survive
    assert "03" in out
    assert "เบาหวาน" in out

    # Second call → cache hit, identical bytes
    out2 = await svc.polish(req)
    assert out == out2


@pytest.mark.anyio
async def test_polish_second_call_cache_hit_with_mock(
    svc: TextPolishService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same as above but mocked — proves the cache-hit code path works
    when LMStudio is unreachable."""
    n = {"calls": 0}

    async def _mock(req: PolishRequest) -> str:
        n["calls"] += 1
        return req.text  # echo, passes safety filter

    monkeypatch.setattr(svc, "_call_gemma", _mock)

    req = PolishRequest(text="t1", context_hint="h", lang="th")
    a = await svc.polish(req)
    b = await svc.polish(req)
    assert a == b
    assert n["calls"] == 1


# ---------------------------------------------------------------------------
# Feature-flag gating — polish must be off by default + only run on
# whitelisted blocks even when explicitly enabled.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_maybe_polish_off_when_flag_unset(svc: TextPolishService) -> None:
    """Default `feature_flags={}` → polish never runs."""
    from dataclasses import dataclass, field

    from services.reports.polish import maybe_polish

    @dataclass
    class _Ctx:
        polish_service: TextPolishService = None  # type: ignore[assignment]
        feature_flags: dict = field(default_factory=dict)
        extra: dict = field(default_factory=dict)
        lang: str = "th"

    ctx = _Ctx(polish_service=svc, feature_flags={})  # flag off
    out = await maybe_polish(
        ctx, "paragraph", "raw text", context_hint="x", lang="th"
    )
    assert out == "raw text"  # untouched


@pytest.mark.anyio
async def test_maybe_polish_off_for_audience_block_even_when_enabled(
    svc: TextPolishService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audience blocks (audience_summary_*) MUST never be polished."""
    from dataclasses import dataclass, field

    from services.reports.polish import maybe_polish

    @dataclass
    class _Ctx:
        polish_service: TextPolishService = None  # type: ignore[assignment]
        feature_flags: dict = field(default_factory=dict)
        extra: dict = field(default_factory=dict)
        lang: str = "th"

    # Tracking polish calls — even with the flag explicitly ON, the
    # audience block must not invoke the service.
    called = {"n": 0}

    async def _track(req: PolishRequest) -> str:
        called["n"] += 1
        return req.text

    monkeypatch.setattr(svc, "polish", _track)

    ctx = _Ctx(
        polish_service=svc,
        feature_flags={"polish_prose": True},
    )
    out = await maybe_polish(
        ctx,
        "audience_summary_executive",
        "stat-bearing prose",
        context_hint="exec",
        lang="th",
    )
    assert out == "stat-bearing prose"
    assert called["n"] == 0, "audience blocks must never be polished"


@pytest.mark.anyio
async def test_maybe_polish_runs_for_paragraph_when_flag_on(
    svc: TextPolishService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow-listed block + flag on + service injected → polish runs."""
    from dataclasses import dataclass, field

    from services.reports.polish import maybe_polish

    @dataclass
    class _Ctx:
        polish_service: TextPolishService = None  # type: ignore[assignment]
        feature_flags: dict = field(default_factory=dict)
        extra: dict = field(default_factory=dict)
        lang: str = "th"

    async def _polish(req: PolishRequest) -> str:
        return req.text + " [polished]"

    monkeypatch.setattr(svc, "polish", _polish)

    ctx = _Ctx(
        polish_service=svc,
        feature_flags={"polish_prose": True},
    )
    out = await maybe_polish(
        ctx,
        "paragraph",
        "raw",
        context_hint="x",
        lang="th",
    )
    assert out == "raw [polished]"
