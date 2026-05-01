"""OpenMultiAgent — top-level orchestrator with SSE streaming pipeline.

KEPT ASYNC — the orchestrator is async because:
1. LLM adapter (httpx) is genuinely async for streaming
2. SSE streaming requires async generators
3. Tools are sync but wrapped via asyncio.to_thread in agent_runner
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import AsyncGenerator

from agents.adapters.base import LLMAdapter, AdapterConfig
from agents.core.agent import Agent, AgentConfig
from agents.core.agent_runner import AgentRunner
from agents.core.circuit_breaker import CircuitBreaker
from agents.core.team import Team
from agents.tools.registry import ToolRegistry
from agents.tools.base import ToolResult
from agents.core.router import keyword_route
from agents.sse import format_sse

logger = logging.getLogger(__name__)


# Thai sentence terminators (most common in user/LLM messages)
# `\u0e2f` = ฯ (paiyannoi)  | `\u0e46` = ๆ (maiyamok)
_THAI_SENTENCE_ENDS = (
    "\n\n", "\n",
    "ครับ ", "ค่ะ ", "นะคะ ", "นะครับ ",
    "ครับ.", "ค่ะ.", "ครับ\n", "ค่ะ\n",
    ". ", "? ", "! ", ".\n", "?\n", "!\n",
    "\u0e2f ", "\u0e2f\n",
)


def _truncate_thai(text: str, max_chars: int = 400) -> str:
    """Truncate `text` to roughly max_chars without cutting mid-sentence.

    Strategy:
      1. Anything <= max_chars: return as-is.
      2. Find the latest sentence terminator before max_chars and cut there.
      3. If no terminator is found in the last 60% of the window, fall
         through to a hard slice + ellipsis (better than cutting mid-word).
    """
    if not text or len(text) <= max_chars:
        return text
    window = text[:max_chars]
    soft_floor = int(max_chars * 0.4)  # don't cut earlier than 40% of budget
    best_idx = -1
    for term in _THAI_SENTENCE_ENDS:
        idx = window.rfind(term)
        if idx > soft_floor and idx > best_idx:
            best_idx = idx + len(term)
    if best_idx > 0:
        return text[:best_idx].rstrip()
    # No sentence terminator — fall back to word boundary if possible.
    # Thai doesn't use spaces between words, so just hard-slice with ellipsis.
    return text[:max_chars].rstrip() + "\u2026"

# Load system prompt from skill guide
_SKILL_PATH = Path(__file__).parent.parent / "prompts" / "health_assistant_skill.md"
try:
    SYSTEM_PROMPT = _SKILL_PATH.read_text(encoding="utf-8")
except FileNotFoundError:
    SYSTEM_PROMPT = "You are a health data analyst for Bangkok. Respond in Thai. Use tools to query data."

# ---------------------------------------------------------------------------
# Topic guardrail — reject only OBVIOUSLY-off-topic messages before LLM call
#
# Previously this was an inclusive keyword allowlist (`_ON_TOPIC_KEYWORDS`)
# that returned False for anything that didn't mention an explicit health
# term. That misclassified legitimate BMA-health queries (e.g. "ภาพรวม"
# alone) as off-topic and the orchestrator returned a hard-coded refusal
# without ever calling the LLM. Worse, the system prompt also literally
# instructed Gemma to copy the same refusal text, so on the rare occasion
# the keyword filter let a query through, Gemma still refused — emitting
# the prompt's example phrase verbatim.
#
# New approach: keep a SMALL deny-list of clearly-unrelated patterns
# (recipes, crypto prices, weather, code-writing) and let the LLM-with-
# tools decide for everything else. The LLM is instructed (via the
# rewritten skill prompt) to answer in-scope questions by calling tools,
# and refuse only obviously-off-topic ones.
# ---------------------------------------------------------------------------

# Substrings that, when found case-insensitively in the user message,
# trigger a hard refusal without consulting the LLM. Keep this list
# narrow on purpose: a false-negative here just means the LLM gets to
# reject (which it can do per the skill prompt); a false-positive means
# we wrongly refuse a legitimate health question, which is the bug we
# are fixing.
_HARD_OFF_TOPIC = (
    # Cooking / recipes
    "วิธีทำพาสต้า", "วิธีทำอาหาร", "สูตรอาหาร", "สูตรขนม", "วิธีทำขนม",
    "recipe", "how to cook",
    # Finance / crypto
    "ราคาบิทคอยน์", "ราคา bitcoin", "ราคาหุ้น", "ราคาทอง",
    "stock price", "crypto price", "bitcoin price",
    "mortgage", "ลอตเตอรี่", "หวย",
    # Weather
    "พยากรณ์อากาศ", "weather forecast", "ฝนตกไหม",
    # Code / generic dev help
    "เขียนโค้ด python", "เขียนโปรแกรม", "write code", "write a python",
    # Entertainment
    "แต่งเพลง", "แต่งกลอน", "เล่าเรื่อง", "joke", "เรื่องตลก",
)

_REFUSAL_RESPONSE = (
    "ขออภัยค่ะ ฉันตอบได้เฉพาะข้อมูลโครงการคัดกรองสุขภาพ กทม. "
    "หากต้องการคำแนะนำสุขภาพส่วนบุคคล โทรสายด่วน **1555**"
)


SYNTH_PROMPT = (
    "ตอบภาษาไทย Markdown กระชับ <=200 คำ\n"
    "กฎเด็ดขาด:\n"
    "1. คัดลอกตัวเลขจากข้อมูล API ที่ให้มาตรงๆ ห้ามปัดเศษ ห้ามประมาณ ห้ามคำนวณเอง\n"
    "2. ถ้าข้อมูลบอก 333,841 ต้องเขียน 333,841 ไม่ใช่ 333,900\n"
    "3. ถ้าข้อมูลไม่มีตัวเลขนั้น ห้ามใส่ ให้ข้ามไป\n"
    "4. ห้ามคำนวณจำนวนคนจาก % เด็ดขาด\n"
    "5. ห้ามเพิ่มข้อมูลที่ไม่ได้มาจาก API ถ้าข้อมูลให้มาไม่พอ ให้บอกว่ามีข้อมูลแค่นี้\n"
    "6. ห้ามแต่ง 'เป้าหมาย' หรือเลขใดที่ tool ไม่ได้ส่งมา — ถ้าไม่มีให้ใส่ '—'"
)


def _is_obviously_off_topic(message: str) -> bool:
    """Permissive guardrail: True only when the query is clearly NOT about BMA health screening.

    Returns True only when the message contains a phrase from the small
    `_HARD_OFF_TOPIC` deny-list (recipes, finance, weather, generic code,
    entertainment). Anything ambiguous is assumed in-scope; the LLM (with
    the tool-first skill prompt) decides whether to call a tool or refuse.
    """
    lower = message.lower()
    return any(phrase in lower for phrase in _HARD_OFF_TOPIC)


class OpenMultiAgent:
    """Top-level orchestrator — factory for teams, entry point for requests.

    Architecture:
        OpenMultiAgent
          +-- Team (per request)
                |-- Analyst Agent (tool selection)
                |-- Data Agent (tool execution)
                +-- Synthesizer Agent (final response)
    """

    def __init__(
        self,
        analyst_adapter: LLMAdapter | None = None,
        synthesizer_adapter: LLMAdapter | None = None,
        registry: ToolRegistry | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        # Legacy single-adapter form, kept so existing callers don't break.
        adapter: LLMAdapter | None = None,
    ):
        if analyst_adapter is None and adapter is None:
            raise ValueError("OpenMultiAgent requires analyst_adapter (or legacy `adapter`)")
        self.analyst_adapter = analyst_adapter or adapter
        self.synthesizer_adapter = synthesizer_adapter or self.analyst_adapter
        # Backwards-compat alias: callers that read self.adapter still get the
        # synthesizer adapter (the one used for the visible final response).
        self.adapter = self.synthesizer_adapter
        self.registry = registry  # type: ignore[assignment]
        self.cb = circuit_breaker or CircuitBreaker()

    async def _adapters_healthy(self) -> bool:
        """Both adapters must be reachable. When they share an instance, this
        is a single ping; when split, it pings both so we fail fast if either
        model isn't loaded in LMStudio."""
        if self.analyst_adapter is self.synthesizer_adapter:
            return await self.analyst_adapter.health_check()
        return (
            await self.analyst_adapter.health_check()
            and await self.synthesizer_adapter.health_check()
        )

    def create_team(self) -> Team:
        """Create a team with analyst + synthesizer agents (each with its own adapter)."""
        analyst = Agent(
            config=AgentConfig(name="analyst", role="วิเคราะห์คำถาม", system_prompt=SYSTEM_PROMPT, icon="brain"),
            adapter=self.analyst_adapter,
            tools=self.registry,
        )
        synthesizer = Agent(
            config=AgentConfig(name="synthesizer", role="สรุปคำตอบ", system_prompt=SYNTH_PROMPT, icon="sparkle"),
            adapter=self.synthesizer_adapter,
        )
        return Team(agents={"analyst": analyst, "synthesizer": synthesizer})

    async def process(self, user_message: str, context: dict | None = None) -> dict:
        """Non-streaming: returns {content, visualizations}."""
        # Hard guardrail: refuse only OBVIOUSLY-off-topic messages.
        # In-scope and ambiguous messages reach the LLM, which (per the
        # skill prompt) decides whether to call a tool or refuse.
        if _is_obviously_off_topic(user_message):
            return {"content": _REFUSAL_RESPONSE, "visualizations": []}

        if not self.cb.can_execute() or not await self._adapters_healthy():
            self.cb.record_failure()
            from agents.fallback import handle_fallback
            return await handle_fallback(None, user_message, context)

        try:
            team = self.create_team()
            analyst = team.get_agent("analyst")
            synthesizer = team.get_agent("synthesizer")
            runner = AgentRunner(analyst, self.registry)

            messages = [{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_message}]
            text, viz, artifacts, results = await runner.run_conversation(messages)

            # --- Gap #2 fix: if tools ran but text is empty, synthesize from tool results ---
            if not text.strip() and results:
                tool_context = "\n".join(r.text[:1500] for r in results if r.text)
                if tool_context:
                    synth_msgs = [
                        {"role": "system", "content": SYNTH_PROMPT},
                        {"role": "user", "content": f"ข้อมูล:\n{tool_context}\n\nสรุปข้อมูลนี้ตอบคำถาม: {user_message}"},
                    ]
                    synth_resp = await self.adapter.chat(synth_msgs)
                    text = synth_resp.content

            # NOTE: a previous "Gap #4 fix" appended a hard-coded
            # "เป้าหมาย 1.6 ล้านคน" suffix here whenever the local sample
            # size was below 1000. That string was the source of the
            # dashboard widget showing "เป้าหมาย 1.6 ล้าน" — the LLM
            # never said it, the orchestrator pasted it on every reply.
            # The target number is not part of the screening dataset and
            # tools never return it; if the operator wants to expose a
            # programmatic target, add an explicit tool that returns the
            # real value (e.g. ``query_api endpoint=headline_kpi``) and
            # let the synthesiser cite that.

            self.cb.record_success()
            return {"content": text, "visualizations": viz}
        except Exception as e:
            logger.exception("Process failed: %s", e)
            self.cb.record_failure()
            from agents.fallback import handle_fallback
            return await handle_fallback(None, user_message, context)

    async def process_stream(self, user_message: str,
                             conv_history: list[dict] | None = None) -> AsyncGenerator[str, None]:
        """Streaming: yields SSE events with agent status animation."""

        # Hard guardrail: refuse only OBVIOUSLY-off-topic messages.
        # In-scope and ambiguous messages reach the LLM, which (per the
        # skill prompt) decides whether to call a tool or refuse.
        if _is_obviously_off_topic(user_message):
            yield format_sse({"type": "content", "text": _REFUSAL_RESPONSE})
            yield format_sse({"type": "done"})
            return

        # Circuit breaker / health check
        if not self.cb.can_execute() or not await self._adapters_healthy():
            self.cb.record_failure()
            from agents.fallback import handle_fallback
            result = await handle_fallback(None, user_message)
            yield format_sse({"type": "content", "text": result["content"]})
            yield format_sse({"type": "done"})
            return

        try:
            team = self.create_team()
            analyst = team.get_agent("analyst")
            synthesizer = team.get_agent("synthesizer")

            # Build messages with trimmed history (last 4 turns, ~400 chars each)
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            if conv_history:
                for m in conv_history[-4:]:
                    if m.get("role") in ("user", "assistant") and m.get("content"):
                        messages.append({
                            "role": m["role"],
                            "content": _truncate_thai(m["content"], max_chars=400),
                        })
            messages.append({"role": "user", "content": user_message})

            # ===== Check: Report from history (no tools needed) =====
            history_report_keywords = ["ทั้งหมดทำเป็นรายงาน", "ทำเป็นรายงาน", "เขียนเป็น", "สรุปเป็น pdf", "สรุปเป็นรายงาน", "make report from", "compile report"]
            is_history_report = any(kw in user_message.lower() for kw in history_report_keywords) and conv_history and len(conv_history) > 1

            if is_history_report:
                yield format_sse({"type": "agent_start", "agent": "analyst", "label": "กำลังรวบรวมข้อมูล...", "icon": "brain"})
                yield format_sse({"type": "agent_done", "agent": "analyst"})

                history_content = "\n\n".join(
                    m["content"][:500] for m in (conv_history or []) if m.get("role") == "assistant" and m.get("content")
                )
                if history_content:
                    yield format_sse({"type": "agent_start", "agent": "data", "label": "กำลังสร้างรายงาน PDF...", "icon": "document"})
                    from agents.tools.adaptive_report import GenerateAdaptiveReportTool
                    report_tool = GenerateAdaptiveReportTool()
                    result = await asyncio.to_thread(report_tool.execute, {
                        "title": "สรุปการวิเคราะห์สุขภาพ",
                        "topic": history_content[:800],
                        "format": "slides",
                    })
                    yield format_sse({"type": "agent_done", "agent": "data"})

                    yield format_sse({"type": "agent_start", "agent": "synthesizer", "label": "กำลังสรุป...", "icon": "sparkle"})
                    yield format_sse({"type": "content", "text": result.text})
                    yield format_sse({"type": "agent_done", "agent": "synthesizer"})

                    if result.metadata and result.metadata.get("url"):
                        yield format_sse({"type": "artifact", "url": result.metadata["url"], "label": "รายงานสรุป"})
                else:
                    yield format_sse({"type": "content", "text": "ไม่มีข้อมูลจากการสนทนาก่อนหน้าสำหรับสร้างรายงาน"})

                yield format_sse({"type": "done"})
                self.cb.record_success()
                return

            # ===== Phase 1: Analyst (with progressive tool disclosure) =====
            yield format_sse({"type": "agent_start", "agent": "analyst", "label": "กำลังวิเคราะห์คำถาม...", "icon": "brain"})

            selected_tools = keyword_route(user_message)
            filtered_schemas = self.registry.to_filtered_schemas(selected_tools)
            logger.info("Routed to %d tools: %s", len(selected_tools), selected_tools)

            try:
                response = await asyncio.wait_for(
                    self.analyst_adapter.chat(messages, tools=filtered_schemas),
                    timeout=60,
                )
            except asyncio.TimeoutError:
                yield format_sse({"type": "agent_done", "agent": "analyst"})
                yield format_sse({"type": "content", "text": "ระบบใช้เวลานานกว่าปกติ กรุณาลองถามใหม่ด้วยคำถามที่เจาะจงขึ้นครับ"})
                yield format_sse({"type": "done"})
                return
            yield format_sse({"type": "agent_done", "agent": "analyst"})

            # No tools -> FORCE the top-priority tool from router
            if not response.tool_calls:
                forced = selected_tools[0] if selected_tools else "query_health_data"
                logger.warning("LLM skipped tools — forcing %s", forced)
                # Build sensible default args per tool
                _default_args = {
                    "query_health_data": {"group_by": "disease", "chart_type": "donut"},
                    "query_statistical_test": {"test_type": "cross_tabulation"},
                    "query_api": {"endpoint": "headline_kpi"},
                }
                response.tool_calls = [{
                    "id": "forced_tool",
                    "function": {
                        "name": forced,
                        "arguments": json.dumps(_default_args.get(forced, {})),
                    },
                }]

            # ===== Phase 2: Execute tools =====
            all_viz = []
            all_artifacts = []

            for tc in response.tool_calls:
                fn_name = tc["function"]["name"]
                raw_args = tc["function"]["arguments"]
                args_malformed = False
                try:
                    fn_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(fn_args, dict):
                        # LLM returned valid JSON but not an object (e.g., a list or string)
                        raise TypeError(f"tool args must be an object, got {type(fn_args).__name__}")
                except (json.JSONDecodeError, TypeError) as e:
                    fn_args = {}
                    args_malformed = True
                    # Surface the failure in logs + downstream tool result so a
                    # debugging operator can see the LLM's bad output verbatim.
                    raw_preview = (raw_args if isinstance(raw_args, str) else str(raw_args))[:200]
                    logger.warning(
                        "Tool %s called with malformed args (%s): %r",
                        fn_name, type(e).__name__, raw_preview,
                    )
                    yield format_sse({
                        "type": "warning",
                        "message": f"LLM ส่ง argument ที่ parse ไม่ได้ให้ tool `{fn_name}` — ใช้ค่าเริ่มต้นแทน",
                    })

                # Clarification stops pipeline
                if fn_name == "ask_clarification":
                    result = await asyncio.to_thread(self.registry.execute_sync, fn_name, fn_args)
                    if result.metadata:
                        yield format_sse({"type": "clarification", "questions": result.metadata["questions"]})
                    yield format_sse({"type": "done"})
                    return

                from agents.tools.helpers import DISEASE_NAMES
                label = DISEASE_NAMES.get(fn_args.get("disease", ""), "ข้อมูล")
                yield format_sse({"type": "agent_start", "agent": "data", "label": f"กำลังดึงข้อมูล ({label})...", "icon": "database"})

                try:
                    tool_timeout = 300 if 'report' in fn_name else 90
                    result = await asyncio.wait_for(
                        asyncio.to_thread(self.registry.execute_sync, fn_name, fn_args),
                        timeout=tool_timeout,
                    )
                except asyncio.TimeoutError:
                    result = ToolResult(text=f"Tool {fn_name} timed out after 90s")
                    logger.warning("Tool %s timed out", fn_name)

                tool_text = result.text[:1500] + ("..." if len(result.text) > 1500 else "")
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": tool_text})

                if result.visualizations:
                    all_viz.extend(result.visualizations)
                    yield format_sse({"type": "agent_start", "agent": "chart", "label": "กำลังสร้างกราฟ...", "icon": "chart"})
                    # Emit each visualization as soon as it's ready so the
                    # frontend can render the chart while the synthesizer is
                    # still streaming text. (Previously these were batched at
                    # the end of the stream — users saw them pop in only after
                    # the entire response finished.)
                    for v in result.visualizations:
                        yield format_sse({"type": "visualization", "data": v})
                    yield format_sse({"type": "agent_done", "agent": "chart"})

                if "Download URL:" in result.text:
                    url = result.text.split("Download URL:")[-1].strip()
                    all_artifacts.append({"url": url, "label": label})
                    yield format_sse({"type": "artifact", "url": url, "label": label})

                yield format_sse({"type": "agent_done", "agent": "data"})

            # ===== Phase 3: Synthesizer =====
            yield format_sse({"type": "agent_start", "agent": "synthesizer", "label": "กำลังสรุปคำตอบ...", "icon": "sparkle"})

            tool_results = [m["content"] for m in messages if m.get("role") == "tool" and m.get("content")]
            # Check if tools returned actual data (not just "ไม่พบข้อมูล")
            useful_results = [r for r in tool_results if "ไม่พบข้อมูล" not in r and "ไม่รู้จัก" not in r and len(r.strip()) > 20]
            if not useful_results:
                yield format_sse({"type": "content", "text": "ขออภัยค่ะ ไม่พบข้อมูลที่ตรงกับคำถามนี้ในระบบ ลองถามแบบเจาะจงขึ้น เช่น:\n- \"เบาหวานเขตไหนเยอะสุด\"\n- \"ภาพรวมโรคทั้งหมด\"\n- \"เปรียบเทียบโซน 1 กับ 3\""})
                yield format_sse({"type": "agent_done", "agent": "synthesizer"})
                yield format_sse({"type": "done"})
                self.cb.record_success()
                return
            tool_context = "\n".join(useful_results)

            # NOTE: a previous "Gap #4 fix" injected a "เป้า 1.6 ล้าน"
            # hint into the synthesiser prompt whenever the local sample
            # size was below 1000. That nudge was the upstream cause of
            # Gemma echoing "เป้าหมาย 1.6 ล้าน" verbatim — the model was
            # never told a real target; we were just suggesting one. The
            # target number is not part of the screening dataset, so it
            # has been removed. To surface a real target, register a
            # tool (e.g. ``query_api endpoint=headline_kpi``) that
            # returns the value and let the synthesiser cite the tool's
            # output.

            synth_messages = [
                {"role": "system", "content": SYNTH_PROMPT},
                {"role": "user", "content": f"ข้อมูลจาก API (ห้ามใช้ตัวเลขอื่นนอกจากนี้):\n{tool_context}\n\nสรุปข้อมูลนี้ตอบคำถาม: {user_message}"},
            ]

            try:
                async for token in synthesizer.stream(synth_messages):
                    yield format_sse({"type": "content", "text": token})
            except Exception:
                logger.warning("Streaming failed, falling back")
                resp = await self.adapter.chat(synth_messages)
                yield format_sse({"type": "content", "text": resp.content})

            yield format_sse({"type": "agent_done", "agent": "synthesizer"})

            # Visualizations + artifacts were already streamed inline as soon
            # as each tool produced them (see Phase 2 above). No need to
            # re-emit at the end — that just causes duplicates.

            yield format_sse({"type": "done"})
            self.cb.record_success()

        except (asyncio.CancelledError, GeneratorExit):
            logger.info("Client disconnected mid-stream")
            return

        except Exception as e:
            logger.exception("Streaming failed: %s", e)
            self.cb.record_failure()
            try:
                from agents.fallback import handle_fallback
                result = await handle_fallback(None, user_message)
                yield format_sse({"type": "content", "text": result["content"]})
            except Exception:
                yield format_sse({"type": "content", "text": (
                    "ขออภัยครับ ขณะนี้ระบบ AI กำลังประมวลผลหนัก ลองถามใหม่อีกครั้ง\n\n"
                    "**สิ่งที่ผมช่วยได้:**\n"
                    "- ภาพรวมสุขภาพ 50 เขต 8 โซน\n"
                    "- เปรียบเทียบโรค NCD 9 โรค\n"
                    "- วิเคราะห์ปัจจัยเสี่ยง (อายุ เพศ พฤติกรรม)\n"
                    "- สร้างรายงาน PDF/สไลด์\n"
                    "- ทดสอบทางสถิติ (Chi-square, ANOVA, OR)\n\n"
                    "ลองถามใหม่ให้เจาะจงขึ้นครับ"
                )})
            yield format_sse({"type": "done"})

    def get_status(self) -> dict:
        return {
            "circuit_breaker": self.cb.get_status(),
            "model": self.synthesizer_adapter.config.model,
            "model_analyst": self.analyst_adapter.config.model,
            "tools": len(self.registry.list_tools()),
        }
