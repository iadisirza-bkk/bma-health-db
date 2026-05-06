"""TextPolishService — optional Gemma-powered prose-polish for prose-only blocks.

Sprint S9 ("Bulk Pre-build with Content-Hash Cache") complement: lets the 3
prose-only blocks (``paragraph``, ``callout``, ``ai_insight``) opt into a
clarity / Thai-grammar polish pass through LMStudio Gemma. Stat-bearing
audience blocks (``audience_summary_*``) are excluded by allow-list — their
wording must stay deterministic.

Drift-free by construction
--------------------------
The cache key is content-hashed (text + context_hint + lang + model_version).
Identical input → identical output forever. Same data → same prose. Drift
is **impossible** because the cache key embeds the input.

Safety filter
-------------
Every Gemma response is run through ``_safety_filter`` before being returned.
The filter rejects (and falls back to the original text) any output that:

* contains medical advice phrases (Thai or English),
* mentions specific drugs (insulin, metformin, atenolol, …),
* asserts causation (เพราะ … จึง … / X causes Y),
* changes any number that appears in input,
* differs in length by more than 2× from input (sanity).

Rejections are logged at WARNING; the original text is returned unchanged.

Usage
-----
::

    svc = TextPolishService()
    polished = await svc.polish(PolishRequest(
        text="ในเขต ... ความเสี่ยงสูง",
        context_hint="zone summary",
        lang="th",
    ))

The service is stateless aside from the SQLite cache — safe to share across
all renders within one process.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger("api.services.reports.polish")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolishRequest:
    """One polish request — all four fields participate in the cache key.

    ``text`` is the raw template-rendered prose; ``context_hint`` is a
    short author-supplied tag (e.g. ``"summary intro for ผู้บริหาร"``)
    that helps Gemma pick a register; ``lang`` selects the system prompt.
    """

    text: str
    context_hint: str
    lang: str  # 'th' / 'en'


# ---------------------------------------------------------------------------
# Safety filter — fail-closed: original text is returned if anything is
# off about the polished output. Patterns are intentionally broad; false
# positives just leak through as "no polish", which is the safe default.
# ---------------------------------------------------------------------------

# Phrases that suggest medical advice. Matched as substrings (lowercase
# for English, raw for Thai). Add aggressively — false positives just
# mean "no polish today, render the template instead".
_MEDICAL_ADVICE_PATTERNS_TH = (
    "ควรกินยา",
    "ควรทานยา",
    "ควรรับประทานยา",
    "แนะนำให้กินยา",
    "แนะนำให้ทานยา",
    "แนะนำให้รับประทาน",
    "ควรไปพบแพทย์",
    "ต้องไปพบแพทย์",
    "คุณต้อง",  # imperative-personal — too directive
    "ท่านต้อง",
    "ท่านควรไป",
    "ควรตรวจ",
    "ต้องตรวจ",
    "ต้องฉีด",
    "ต้องผ่าตัด",
)
_MEDICAL_ADVICE_PATTERNS_EN = (
    "should take medication",
    "you must take",
    "you should take",
    "must visit",
    "should visit a doctor",
    "you need to",
    "should consult",
)

# Drug name fragments (lowercased for English; Thai transliterations
# included). Hits any output mentioning a specific drug → reject.
_DRUG_PATTERNS = (
    "insulin",
    "อินซูลิน",
    "metformin",
    "เมทฟอร์มิน",
    "atenolol",
    "อะทีโนลอล",
    "amlodipine",
    "แอมโลดิปีน",
    "losartan",
    "โลซาร์แทน",
    "simvastatin",
    "ซิมวาสแตติน",
    "atorvastatin",
    "อะทอร์วาสแตติน",
    "warfarin",
    "วาร์ฟาริน",
    "aspirin",
    "แอสไพริน",
)

# Causation patterns. The data we render is correlation only — any
# X-causes-Y assertion is dangerous editorialising.
_CAUSATION_PATTERNS_TH_RE = re.compile(
    r"เพราะ.{1,80}?จึง|เนื่องจาก.{1,80}?ทำให้|ทำให้เกิด"
)
_CAUSATION_PATTERNS_EN_RE = re.compile(
    r"\bcauses?\b|\bbecause of\b.{1,80}?\bso\b|\bleads? to\b",
    re.IGNORECASE,
)


# Number tokens — any digit-string with optional comma/period/percent.
# Used to verify that the polished text didn't drop or change a number
# from the input.
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*%?")


def _extract_numbers(text: str) -> list[str]:
    """Pull every digit-bearing token out of ``text``."""
    return list(_NUMBER_RE.findall(text))


def _safety_violations(output: str, request: "PolishRequest") -> list[str]:
    """Return a list of human-readable violations, or empty list if clean."""
    violations: list[str] = []
    lower_out = output.lower()
    if request.lang == "en":
        for phrase in _MEDICAL_ADVICE_PATTERNS_EN:
            if phrase in lower_out:
                violations.append(f"medical advice (en): {phrase!r}")
    # Thai patterns checked regardless of declared lang (mixed-lang text
    # is common — a Thai sentence in an "en" block still triggers).
    for phrase in _MEDICAL_ADVICE_PATTERNS_TH:
        if phrase in output:
            violations.append(f"medical advice (th): {phrase!r}")
    for drug in _DRUG_PATTERNS:
        if drug in lower_out or drug in output:
            violations.append(f"drug mention: {drug!r}")
    if _CAUSATION_PATTERNS_TH_RE.search(output):
        violations.append("causation claim (th)")
    if _CAUSATION_PATTERNS_EN_RE.search(output):
        violations.append("causation claim (en)")

    in_nums = _extract_numbers(request.text)
    out_nums = _extract_numbers(output)
    # The polished output must contain every number from the input,
    # in the same multiset. Any drop / change / addition is a violation.
    if sorted(in_nums) != sorted(out_nums):
        violations.append(
            f"number set changed: input={in_nums!r} output={out_nums!r}"
        )

    # Length sanity. Fail-closed if Gemma went way long or way short.
    in_len = len(request.text)
    out_len = len(output)
    if in_len > 0 and (out_len > in_len * 2 or out_len * 2 < in_len):
        violations.append(
            f"length out of band: input={in_len} output={out_len}"
        )
    if out_len == 0:
        violations.append("empty output")
    return violations


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TextPolishService:
    """LLM-powered polish for prose-only blocks.

    Hash-keyed cache — identical (text, hint, lang, model_version) returns
    identical output forever. Drift impossible by construction.

    The HTTP call timeout is hard-bounded (default 30s); on timeout / any
    HTTP error / safety violation, the *original* text is returned and the
    failure is logged. The renderer must always succeed even if Gemma is
    down or rate-limited.
    """

    def __init__(
        self,
        lmstudio_url: str = "http://localhost:5555/v1",
        model: str = "google/gemma-4-26b-a4b",
        timeout_s: int = 30,
        cache_path: Path = Path("var/cache/text_polish.sqlite"),
    ) -> None:
        # Strip a trailing /v1 if the caller passed bare base URL too —
        # we always re-append the OpenAI route below.
        self._url = lmstudio_url.rstrip("/")
        if not self._url.endswith("/v1"):
            self._url = f"{self._url}/v1"
        self._model = model
        self._timeout_s = timeout_s
        self._cache_path = Path(cache_path)
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_cache()

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _init_cache(self) -> None:
        """Create the SQLite cache table if it doesn't already exist."""
        with sqlite3.connect(self._cache_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS polish_cache (
                    key TEXT PRIMARY KEY,
                    polished TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def _hash_key(self, req: PolishRequest) -> str:
        """Stable 32-hex-char content hash. Includes model name so a
        model upgrade auto-invalidates without a manual purge."""
        h = hashlib.sha256()
        h.update(req.text.encode("utf-8"))
        h.update(b"\x00")
        h.update(req.context_hint.encode("utf-8"))
        h.update(b"\x00")
        h.update(req.lang.encode("utf-8"))
        h.update(b"\x00")
        h.update(self._model.encode("utf-8"))
        return h.hexdigest()[:32]

    def _cache_get(self, key: str) -> Optional[str]:
        with sqlite3.connect(self._cache_path) as conn:
            row = conn.execute(
                "SELECT polished FROM polish_cache WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def _cache_put(self, key: str, polished: str) -> None:
        with sqlite3.connect(self._cache_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO polish_cache (key, polished, "
                "created_at) VALUES (?, ?, ?)",
                (key, polished, time.time()),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def polish(self, req: PolishRequest) -> str:
        """Return polished text (or the original on any failure).

        Cache hit ≈ ~1ms; miss = ~1-3s LLM call. The function NEVER raises
        — every error path returns the original ``req.text`` so the
        renderer keeps working even when LMStudio is unreachable.
        """
        if not req.text.strip():
            return req.text  # nothing to polish
        key = self._hash_key(req)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            polished = await self._call_gemma(req)
        except Exception as exc:  # noqa: BLE001 — fail-closed by design
            logger.warning(
                "polish: Gemma call failed (%s) — falling back to original",
                type(exc).__name__,
            )
            return req.text
        polished = self._safety_filter(polished, req)
        self._cache_put(key, polished)
        return polished

    # ------------------------------------------------------------------
    # Safety filter — public-ish (tested directly)
    # ------------------------------------------------------------------

    def _safety_filter(self, output: str, req: PolishRequest) -> str:
        """Reject + fallback to original on any violation."""
        violations = _safety_violations(output, req)
        if violations:
            logger.warning(
                "polish: safety filter rejected output (%d violation(s)): %s",
                len(violations),
                "; ".join(violations[:5]),
            )
            return req.text
        return output.strip()

    # ------------------------------------------------------------------
    # Gemma call
    # ------------------------------------------------------------------

    def _system_prompt(self, lang: str) -> str:
        """Compose the system prompt. Keep it short — the longer the
        prompt the more drift in the polished output."""
        if lang == "en":
            return (
                "You polish health-screening report prose for clarity. "
                "Rules: keep ALL numbers and citations EXACTLY as given. "
                "Do not add medical advice. Do not name specific drugs. "
                "Do not assert causation (only correlation is supported "
                "by the data). Keep the tone clinical-but-friendly. "
                "Output ONLY the polished prose — no preamble, no notes."
            )
        # default: Thai
        return (
            "คุณคือบรรณาธิการที่ขัดเกลาสำนวนภาษาไทยสำหรับรายงานคัดกรองสุขภาพ "
            "ข้อบังคับสำคัญ: คงตัวเลขและการอ้างอิงทุกตัวให้เหมือนเดิม "
            "ห้ามเพิ่มคำแนะนำทางการแพทย์ ห้ามระบุชื่อยา "
            "ห้ามอ้างเหตุ-ผล (ข้อมูลเป็น correlation เท่านั้น) "
            "คงโทนวิชาการแต่อ่านง่าย "
            "ตอบเฉพาะข้อความที่ขัดเกลาแล้ว ไม่ต้องมีคำนำหรือหมายเหตุ"
        )

    async def _call_gemma(self, req: PolishRequest) -> str:
        """Issue one OpenAI-compatible chat-completion. Returns the raw
        ``content`` — caller applies safety filter."""
        user_msg = (
            f"[context: {req.context_hint}]\n\n"
            f"{req.text}"
        )
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": self._system_prompt(req.lang)},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.1,
            "seed": 42,
            "max_tokens": 800,
        }
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(
                f"{self._url}/chat/completions", json=payload
            )
            resp.raise_for_status()
            data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Gemma response had unexpected shape: {exc}"
            ) from exc
        return str(content or "").strip()


# ---------------------------------------------------------------------------
# Allow-list — only these block_ids may be polished. Audience-summary
# blocks are stat-bearing prose where wording must stay deterministic.
# ---------------------------------------------------------------------------

POLISHABLE_BLOCKS: frozenset[str] = frozenset(
    {"paragraph", "callout", "ai_insight"}
)


def is_polish_enabled(ctx: Any, block_id: str) -> bool:
    """True iff polish should run for this (ctx, block) pair.

    Reads ``ctx.feature_flags["polish_prose"]`` (falls back to
    ``ctx.extra["feature_flags"]["polish_prose"]`` for back-compat) and
    requires both flags to be truthy AND the block to be in the
    allow-list AND a polish_service to be available on the context.
    """
    if block_id not in POLISHABLE_BLOCKS:
        return False
    flags = getattr(ctx, "feature_flags", None) or (
        ctx.extra.get("feature_flags", {}) if getattr(ctx, "extra", None) else {}
    )
    if not flags or not flags.get("polish_prose", False):
        return False
    svc = getattr(ctx, "polish_service", None) or (
        ctx.extra.get("polish_service") if getattr(ctx, "extra", None) else None
    )
    return svc is not None


async def maybe_polish(
    ctx: Any,
    block_id: str,
    text: str,
    *,
    context_hint: str,
    lang: Optional[str] = None,
) -> str:
    """Convenience: polish ``text`` if enabled, else return as-is.

    Used by the 3 polishable blocks inside their async ``collect()``
    method so the polished text becomes part of ``data["text"]`` and
    the existing sync ``render_*`` impls keep working unchanged.
    """
    if not is_polish_enabled(ctx, block_id):
        return text
    svc = getattr(ctx, "polish_service", None) or ctx.extra.get(
        "polish_service"
    )
    return await svc.polish(
        PolishRequest(
            text=text,
            context_hint=context_hint,
            lang=lang or getattr(ctx, "lang", "th"),
        )
    )
