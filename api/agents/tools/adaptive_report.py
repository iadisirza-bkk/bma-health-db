"""GenerateAdaptiveReportTool — LLM writes custom content, Tectonic renders to PDF.

SYNC execute(). The LLM call inside (_ask_llm_to_write) remains async and is
called via asyncio.run() when needed from the sync execute method, OR the
orchestrator calls it from async context and wraps in to_thread.

Adapted: LaTeX/Tectonic compilation may not be available. Falls back gracefully.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from typing import Literal, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

from agents.tools.base import BaseTool, ToolResult
from agents.tools.helpers import (
    load_data, normalize_disease, DISEASE_NAMES, get_base_rates,
    DCODE_TO_ZONE, HEALTH_ZONES, DISEASE_ALIASES,
)
import config

logger = logging.getLogger(__name__)

CACHE_DIR = Path(config.REPORTS_DIR) / "adaptive" if hasattr(config, "REPORTS_DIR") else Path("/tmp/bma_reports/adaptive")


def _latex_escape(text: str) -> str:
    """Minimal LaTeX escaping for Thai text."""
    for ch in ["&", "%", "$", "#", "_", "{", "}"]:
        text = text.replace(ch, "\\" + ch)
    text = text.replace("~", "\\textasciitilde{}")
    text = text.replace("^", "\\textasciicircum{}")
    return text


def _ask_llm_to_write_sync(prompt: str) -> str:
    """Use LLM to write report content (sync via httpx)."""
    try:
        with httpx.Client(timeout=90) as client:
            resp = client.post(
                f"{config.LMSTUDIO_URL}/v1/chat/completions",
                json={
                    "model": config.LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": "เขียนเนื้อหารายงานเป็นภาษาไทย กระชับ ชัดเจน ห้ามใช้ Markdown symbols ให้เขียนเป็น plain text ใช้ตัวเลขจริงเท่านั้น"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 2000,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            content = re.sub(r'<think>.*?</think>\s*', '', content, flags=re.DOTALL)
            cleaned = _latex_escape(content.strip())
            if not cleaned or len(cleaned) < 50:
                return _latex_escape(_generate_fallback_content(prompt))
            return cleaned
    except Exception as e:
        logger.warning("LLM write failed: %s — using fallback", e)
        return _latex_escape(_generate_fallback_content(prompt))


def _generate_fallback_content(prompt: str) -> str:
    """Generate report content from data without LLM."""
    data = load_data()
    base_rates = get_base_rates(data)
    total = sum(d["total_screened"] for d in data.values())

    lines = [
        f"จากการคัดกรองสุขภาพประชาชน {total:,} คน ใน 50 เขต 8 โซนสุขภาพ กรุงเทพมหานคร พบข้อมูลดังนี้:",
        "",
    ]
    sorted_diseases = sorted(base_rates.items(), key=lambda x: x[1], reverse=True)
    for dk, pct in sorted_diseases:
        dn = DISEASE_NAMES.get(dk, dk)
        at_risk = round(total * pct / 100)
        lines.append(f"{dn}: อัตราเสี่ยง {pct} เปอร์เซ็นต์ ({at_risk:,} คน)")
    lines.append("")
    if sorted_diseases:
        lines.append("โรคที่พบมากที่สุดคือ " + DISEASE_NAMES.get(sorted_diseases[0][0], "") +
                     f" ({sorted_diseases[0][1]} เปอร์เซ็นต์) ซึ่งสะท้อนถึงความจำเป็นในการเฝ้าระวังและส่งเสริมสุขภาพเชิงรุก")
    lines.append("")
    lines.append("ข้อเสนอแนะ: ควรจัดกิจกรรมคัดกรองเชิงรุกในเขตที่มีความเสี่ยงสูง เพิ่มศูนย์ตรวจสุขภาพชุมชน และส่งเสริมการออกกำลังกายในพื้นที่สาธารณะ")
    return "\n".join(lines)


class GenerateAdaptiveReportParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(..., description="Report title in Thai")
    topic: str = Field(..., description="What to analyze — disease, zone, factor, comparison, etc.")
    disease: Optional[str] = None
    zone: Optional[str] = None
    district: Optional[str] = None
    format: Optional[Literal["slides", "document"]] = Field(
        default=None, description="slides=Beamer 4-6 pages, document=Article 1-2 pages"
    )


class GenerateAdaptiveReportTool(BaseTool):
    name = "generate_adaptive_report"
    description = "Generate CUSTOM PDF with AI-written content tailored to user request"
    Parameters = GenerateAdaptiveReportParams
    parameters_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Report title in Thai"},
            "topic": {"type": "string", "description": "What to analyze — disease, zone, factor, comparison, etc."},
            "disease": {"type": "string"},
            "zone": {"type": "string"},
            "district": {"type": "string"},
            "format": {"type": "string", "enum": ["slides", "document"], "description": "slides=Beamer 4-6 pages, document=Article 1-2 pages"},
        },
        "required": ["title", "topic"],
    }

    def execute(self, args: dict) -> ToolResult:
        args = self.Parameters(**args).model_dump(exclude_none=True)
        title = args.get("title", "รายงานวิเคราะห์สุขภาพ")
        topic = args.get("topic", "")
        disease = normalize_disease(args.get("disease"))
        zone = args.get("zone")
        district = args.get("district")
        fmt = args.get("format", "slides")

        data = load_data()
        base_rates = get_base_rates(data)

        # ===== Strategy: Use existing generators when possible =====

        # Disease-specific
        if disease and not zone and not district:
            try:
                from services.disease_slide_generator import generate_disease_slide
                generate_disease_slide(disease)
                dn = DISEASE_NAMES.get(disease, disease)
                url = f"/api/reports/disease/{disease}"
                return ToolResult(
                    text=f"สร้างสไลด์ {dn} (6 หน้า, AI-generated) สำเร็จ\nDownload URL: {url}",
                    metadata={"url": url},
                )
            except (ImportError, Exception) as e:
                logger.warning("Disease slide failed, falling back to adaptive: %s", e)

        # Zone-specific
        if zone and zone in HEALTH_ZONES:
            try:
                from services.zone_report_generator import generate_zone_report
                generate_zone_report(zone)
                zn = HEALTH_ZONES[zone]["name_th"]
                url = f"/api/reports/zone/{zone}/th"
                return ToolResult(
                    text=f"สร้างรายงาน {zn} สำเร็จ\nDownload URL: {url}",
                    metadata={"url": url},
                )
            except (ImportError, Exception) as e:
                logger.warning("Zone report failed, falling back to adaptive: %s", e)

        # ===== Fallback: Build custom report with REAL DATA + AI insight =====
        context_parts = [f"หัวข้อ: {topic}", f"ข้อมูลจากการคัดกรองสุขภาพ 50 เขต 8 โซน กทม."]
        sorted_diseases = sorted(base_rates.items(), key=lambda x: x[1], reverse=True)
        context_parts.append("อัตราเสี่ยงรายโรค:")
        for dk, pct in sorted_diseases[:5]:
            dn = DISEASE_NAMES.get(dk, dk)
            context_parts.append(f"  - {dn}: {pct}%")
        if zone and zone in HEALTH_ZONES:
            z = HEALTH_ZONES[zone]
            context_parts.append(f"โซน: {z['name_th']} ({z['facilitator']}, {len(z['districts'])} เขต)")
        if district:
            for d in data.values():
                if district in d["name_th"]:
                    context_parts.append(f"เขต: {d['name_th']} (คัดกรอง {d['total_screened']:,} คน)")
                    for dk, dv in list(d["diseases"].items())[:5]:
                        context_parts.append(f"  - {dv['name']}: {dv['pct_at_risk']}%")
                    break
        context = "\n".join(context_parts)

        # Generate text content
        if fmt == "slides":
            content = self._write_slides_text(title, context, topic)
        else:
            content = self._write_document_text(title, context, topic)

        # Try LaTeX compilation
        pdf_path = self._try_compile(title, content, fmt)

        if pdf_path:
            filename = pdf_path.name
            url = f"/api/reports/adaptive/{filename}"
            return ToolResult(
                text=f"สร้างรายงาน '{title}' สำเร็จ ({fmt})\nDownload URL: {url}",
                metadata={"url": url, "filename": filename},
            )

        # If compilation not available, return the text content directly
        return ToolResult(
            text=f"## {title}\n\n{_generate_fallback_content(topic)}",
            metadata={"format": "text_only"},
        )

    def _write_slides_text(self, title: str, context: str, topic: str) -> str:
        prompt = (
            f"เขียนเนื้อหาสไลด์นำเสนอ 5 หน้า เรื่อง: {topic}\n\n"
            f"ข้อมูลจริง:\n{context}\n\n"
            f"กฎ: ใช้ตัวเลขจากข้อมูลจริงเท่านั้น ห้ามสร้างตัวเลขเอง\n"
            f"รูปแบบ: SLIDE 1: ภาพรวม\n[bullet points]\nSLIDE 2: ...\n"
        )
        return _ask_llm_to_write_sync(prompt)

    def _write_document_text(self, title: str, context: str, topic: str) -> str:
        prompt = (
            f"เขียนรายงานสรุปเรื่อง: {topic}\n\n"
            f"ข้อมูลจริง:\n{context}\n\n"
            f"กฎ: ใช้ตัวเลขจากข้อมูลจริงเท่านั้น เขียน 5 ย่อหน้า\n"
        )
        return _ask_llm_to_write_sync(prompt)

    def _try_compile(self, title: str, content: str, fmt: str) -> Path | None:
        """Try to compile LaTeX to PDF. Returns None if not available."""
        try:
            from services.latex_utils import TEMPLATE_DIR, register_thai_font
        except ImportError:
            logger.info("LaTeX utils not available — returning text-only report")
            return None

        try:
            import jinja2
            env = jinja2.Environment(
                loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
                variable_start_string="<<", variable_end_string=">>",
                block_start_string="<%", block_end_string="%>",
            )

            if fmt == "slides":
                template = env.get_template("report_adaptive_slides.tex.j2")
                sections = []
                for block in re.split(r'SLIDE \d+:', content):
                    block = block.strip()
                    if not block:
                        continue
                    lines = block.split('\n', 1)
                    sec_title = lines[0].strip()
                    sec_body = lines[1].strip() if len(lines) > 1 else ""
                    items = [l.strip().lstrip('- ').lstrip('* ') for l in sec_body.split('\n') if l.strip()]
                    sections.append({"title": sec_title, "bullet_points": items, "note": ""})
                tex = template.render(
                    title=_latex_escape(title),
                    generated_date=date.today().strftime("%d/%m/%Y"),
                    slides=sections[:5], charts=[],
                )
            else:
                template = env.get_template("report_adaptive_doc.tex.j2")
                paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
                section_titles = ["สถานการณ์ปัจจุบัน", "ข้อค้นพบสำคัญ", "กลุ่มเสี่ยง", "ข้อเสนอแนะ", "แนวทางดำเนินการ"]
                sections = []
                for i, para in enumerate(paragraphs):
                    sec_title = section_titles[i] if i < len(section_titles) else f"ส่วนที่ {i+1}"
                    sections.append({"title": sec_title, "body": para})
                if not sections:
                    sections = [{"title": "สรุป", "body": content}]
                tex = template.render(
                    title=_latex_escape(title),
                    generated_date=date.today().strftime("%d/%m/%Y"),
                    sections=sections, charts=[],
                )

            # Compile
            with tempfile.TemporaryDirectory() as tmpdir:
                shutil.copytree(str(TEMPLATE_DIR / "assets"), str(Path(tmpdir) / "assets"))
                for preamble in ["bma_beamer_preamble.tex", "bma_article_preamble.tex"]:
                    src = TEMPLATE_DIR / preamble
                    if src.exists():
                        shutil.copy2(str(src), tmpdir)

                tex_path = Path(tmpdir) / "report.tex"
                tex_path.write_text(tex, encoding="utf-8")

                tectonic = shutil.which("tectonic") or getattr(config, "TECTONIC_PATH", "/opt/homebrew/bin/tectonic")
                result = subprocess.run(
                    [tectonic, "-X", "compile", str(tex_path)],
                    capture_output=True, cwd=tmpdir,
                    timeout=int(getattr(config, "TECTONIC_TIMEOUT", 120)),
                )
                if result.returncode != 0:
                    logger.error("Tectonic failed: %s", result.stderr[-500:] if result.stderr else "")
                    return None

                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                name = f"adaptive_{hash(title) % 10000}"
                out = CACHE_DIR / f"{name}.pdf"
                shutil.copy2(str(tex_path.with_suffix(".pdf")), str(out))
                return out

        except Exception as e:
            logger.error("Compile failed: %s", e)
            return None
