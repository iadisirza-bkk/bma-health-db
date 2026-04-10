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

# Load system prompt from skill guide
_SKILL_PATH = Path(__file__).parent.parent / "prompts" / "health_assistant_skill.md"
try:
    SYSTEM_PROMPT = _SKILL_PATH.read_text(encoding="utf-8")
except FileNotFoundError:
    SYSTEM_PROMPT = "You are a health data analyst for Bangkok. Respond in Thai. Use tools to query data."

# ---------------------------------------------------------------------------
# Topic guardrail — reject off-topic before calling LLM
# ---------------------------------------------------------------------------

_ON_TOPIC_KEYWORDS = {
    # Thai health terms
    "สุขภาพ", "คัดกรอง", "เบาหวาน", "ความดัน", "อ้วน", "ไขมัน", "หัวใจ", "หลอดเลือด",
    "ไต", "โลหิตจาง", "ทางเดินหายใจ", "โรค", "เสี่ยง", "ป่วย", "ตรวจ", "แลป", "ผลเลือด",
    "BMI", "น้ำหนัก", "ส่วนสูง", "เอว", "ความดันโลหิต", "น้ำตาล", "คอเลสเตอรอล",
    "เขต", "โซน", "กทม", "กรุงเทพ", "เขตสุขภาพ", "ศูนย์บริการสาธารณสุข",
    "สถิติ", "เปรียบเทียบ", "แนวโน้ม", "กราฟ", "กลุ่มอายุ", "เพศ", "อาชีพ",
    "รายงาน", "PDF", "สไลด์", "ภาพรวม", "วิเคราะห์", "สรุป", "ข้อมูล",
    "PM2.5", "ฝุ่น", "มลพิษ", "อากาศ", "NCD",
    "สูบบุหรี่", "เหล้า", "แอลกอฮอล์", "ออกกำลังกาย", "พฤติกรรม",
    "ซึมเศร้า", "สุขภาพจิต", "PHQ", "ความเครียด",
    "รพ.", "โรงพยาบาล", "1555", "คำแนะนำ",
    # English health terms
    "health", "screening", "diabetes", "hypertension", "obesity", "disease",
    "risk", "district", "zone", "bangkok", "report", "chart", "compare",
    "prevalence", "lab", "cholesterol", "bmi", "blood",
}

_REFUSAL_RESPONSE = (
    "ขออภัยค่ะ ฉันตอบได้เฉพาะเรื่องข้อมูล**โครงการคัดกรองสุขภาพกรุงเทพมหานคร**เท่านั้น "
    "เช่น สถิติโรค สุขภาพรายเขต/โซน กลุ่มเสี่ยง ผลแลป แนวโน้ม หรือขอรายงาน\n\n"
    "หากมีคำถามเกี่ยวกับสุขภาพส่วนบุคคล โปรดโทรสายด่วนสุขภาพ **1555**"
)


def _is_on_topic(message: str) -> bool:
    """Quick keyword check — returns True if message likely relates to BMA health screening."""
    lower = message.lower()
    return any(kw.lower() in lower for kw in _ON_TOPIC_KEYWORDS)


class OpenMultiAgent:
    """Top-level orchestrator — factory for teams, entry point for requests.

    Architecture:
        OpenMultiAgent
          +-- Team (per request)
                |-- Analyst Agent (tool selection)
                |-- Data Agent (tool execution)
                +-- Synthesizer Agent (final response)
    """

    def __init__(self, adapter: LLMAdapter, registry: ToolRegistry,
                 circuit_breaker: CircuitBreaker | None = None):
        self.adapter = adapter
        self.registry = registry
        self.cb = circuit_breaker or CircuitBreaker()

    def create_team(self) -> Team:
        """Create a team with analyst + synthesizer agents."""
        analyst = Agent(
            config=AgentConfig(name="analyst", role="วิเคราะห์คำถาม", system_prompt=SYSTEM_PROMPT, icon="brain"),
            adapter=self.adapter,
            tools=self.registry,
        )
        synthesizer = Agent(
            config=AgentConfig(name="synthesizer", role="สรุปคำตอบ", system_prompt="ตอบภาษาไทย Markdown กระชับ <=200 คำ", icon="sparkle"),
            adapter=self.adapter,
        )
        return Team(agents={"analyst": analyst, "synthesizer": synthesizer})

    async def process(self, user_message: str, context: dict | None = None) -> dict:
        """Non-streaming: returns {content, visualizations}."""
        # Guardrail: reject off-topic
        if not _is_on_topic(user_message):
            return {"content": _REFUSAL_RESPONSE, "visualizations": []}

        if not self.cb.can_execute() or not await self.adapter.health_check():
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
                        {"role": "system", "content": "ตอบภาษาไทย Markdown กระชับ <=200 คำ ใช้ข้อมูลที่ให้มาเท่านั้น"},
                        {"role": "user", "content": f"ข้อมูล:\n{tool_context}\n\nสรุปข้อมูลนี้ตอบคำถาม: {user_message}"},
                    ]
                    synth_resp = await self.adapter.chat(synth_msgs)
                    text = synth_resp.content

            # --- Gap #4 fix: add sample size warning for small datasets ---
            if text.strip():
                from agents.tools.helpers import get_total_screened, load_data
                try:
                    total = get_total_screened(load_data())
                    if total < 1000:
                        text += f"\n\n> **หมายเหตุ**: ข้อมูลจากกลุ่มตัวอย่าง {total:,} คน (เป้าหมาย 1.6 ล้านคน) สัดส่วนอาจเปลี่ยนแปลงเมื่อมีข้อมูลเพิ่ม"
                except Exception:
                    pass

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

        # Guardrail: reject off-topic
        if not _is_on_topic(user_message):
            yield format_sse({"type": "content", "text": _REFUSAL_RESPONSE})
            yield format_sse({"type": "done"})
            return

        # Circuit breaker / health check
        if not self.cb.can_execute() or not await self.adapter.health_check():
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

            # Build messages with trimmed history (3 turns, 200 chars max)
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            if conv_history:
                for m in conv_history[-4:]:
                    if m.get("role") in ("user", "assistant") and m.get("content"):
                        text = m["content"][:400]
                        if len(m["content"]) > 400:
                            for end in ["\n", "ครับ", "ค่ะ", ". "]:
                                idx = text.rfind(end)
                                if idx > 100:
                                    text = text[:idx + len(end)]
                                    break
                        messages.append({"role": m["role"], "content": text})
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
                    self.adapter.chat(messages, tools=filtered_schemas),
                    timeout=60,
                )
            except asyncio.TimeoutError:
                yield format_sse({"type": "agent_done", "agent": "analyst"})
                yield format_sse({"type": "content", "text": "ระบบใช้เวลานานกว่าปกติ กรุณาลองถามใหม่ด้วยคำถามที่เจาะจงขึ้นครับ"})
                yield format_sse({"type": "done"})
                return
            yield format_sse({"type": "agent_done", "agent": "analyst"})

            # No tools -> direct answer or re-stream
            if not response.tool_calls:
                yield format_sse({"type": "agent_start", "agent": "synthesizer", "label": "กำลังเขียนคำตอบ...", "icon": "sparkle"})
                content = response.content.strip()
                if content:
                    yield format_sse({"type": "content", "text": content})
                else:
                    synth_msgs = [
                        {"role": "system", "content": "ตอบภาษาไทย Markdown กระชับ <=200 คำ"},
                        {"role": "user", "content": user_message},
                    ]
                    async for token in synthesizer.stream(synth_msgs):
                        yield format_sse({"type": "content", "text": token})
                yield format_sse({"type": "agent_done", "agent": "synthesizer"})
                yield format_sse({"type": "done"})
                self.cb.record_success()
                return

            # ===== Phase 2: Execute tools =====
            all_viz = []
            all_artifacts = []

            for tc in response.tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"]
                except (json.JSONDecodeError, TypeError):
                    fn_args = {}

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
                    yield format_sse({"type": "agent_done", "agent": "chart"})

                if "Download URL:" in result.text:
                    url = result.text.split("Download URL:")[-1].strip()
                    all_artifacts.append({"url": url, "label": label})

                yield format_sse({"type": "agent_done", "agent": "data"})

            # ===== Phase 3: Synthesizer =====
            yield format_sse({"type": "agent_start", "agent": "synthesizer", "label": "กำลังสรุปคำตอบ...", "icon": "sparkle"})

            tool_results = [m["content"] for m in messages if m.get("role") == "tool" and m.get("content")]
            tool_context = "\n".join(tool_results) if tool_results else "ไม่มีข้อมูล"

            # --- Gap #4 fix: inject sample size context ---
            sample_note = ""
            try:
                from agents.tools.helpers import get_total_screened, load_data
                total = get_total_screened(load_data())
                if total < 1000:
                    sample_note = f"\nหมายเหตุ: ข้อมูลจากกลุ่มตัวอย่าง {total:,} คน (เป้า 1.6 ล้าน) ให้ระบุในคำตอบด้วย"
            except Exception:
                pass

            synth_messages = [
                {"role": "system", "content": "ตอบภาษาไทย Markdown กระชับ <=200 คำ ถ้ามีหมายเหตุเรื่องขนาดตัวอย่างให้ระบุด้วย"},
                {"role": "user", "content": f"ข้อมูล:\n{tool_context}{sample_note}\n\nสรุปข้อมูลนี้ตอบคำถาม: {user_message}"},
            ]

            try:
                async for token in synthesizer.stream(synth_messages):
                    yield format_sse({"type": "content", "text": token})
            except Exception:
                logger.warning("Streaming failed, falling back")
                resp = await self.adapter.chat(synth_messages)
                yield format_sse({"type": "content", "text": resp.content})

            yield format_sse({"type": "agent_done", "agent": "synthesizer"})

            for v in all_viz:
                yield format_sse({"type": "visualization", "data": v})
            for a in all_artifacts:
                yield format_sse({"type": "artifact", "url": a["url"], "label": a["label"]})

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
            "model": self.adapter.config.model,
            "tools": len(self.registry.list_tools()),
        }
