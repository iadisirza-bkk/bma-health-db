"""Keyword router — pre-route user messages to relevant tools without LLM.

Reduces payload from 6 tools (~3.5KB) to 1-2 tools (~1KB).
NOTE: query_backend_api removed — we query DB directly now.
"""
from __future__ import annotations

TOOL_KEYWORDS: dict[str, list[str]] = {
    "query_health_data": [
        "เสี่ยง", "อัตรา", "เปรียบเทียบ", "เขต", "โซน", "อายุ", "เพศ",
        "กราฟ", "chart", "overview", "ภาพรวม", "district", "zone", "disease",
        "โรค", "สุขภาพ", "คัดกรอง", "obesity", "diabetes", "hypertension",
        "สถานการณ์", "ข้อมูล", "กี่", "เท่าไหร่", "แยกตาม", "ranking",
        # Also absorb keywords that used to route to backend_api:
        "กลุ่มอายุ", "วัยทำงาน", "วัยเรียน", "ผู้สูงอายุ", "age group",
        "bmi", "รอบเอว", "waist", "น้ำหนัก",
        "พฤติกรรม", "สูบบุหรี่", "ออกกำลังกาย",
    ],
    "query_statistical_test": [
        "ทดสอบ", "สถิติ", "chi", "odds", "anova", "logistic", "correlation",
        "mann", "comorbidity", "p-value", "ความสัมพันธ์", "significance",
        "นัยสำคัญ", "forest", "trend", "แนวโน้ม",
    ],
    "generate_adaptive_report": [
        "รายงาน", "report", "pdf", "slide", "สไลด์", "เอกสาร", "document",
        "whitepaper", "executive", "สรุป.*ผู้บริหาร",
    ],
    "generate_report": [
        "comprehensive", "disease_focus",
    ],
    "query_zone_info": [
        "facilitator", "รพ.", "โรงพยาบาล", "ดูแล.*โซน", "โซน.*ไหน",
    ],
    # ask_clarification is NEVER pre-selected — LLM decides when to use it
}

# Tools always available (LLM can call even if not keyword-matched)
ALWAYS_AVAILABLE = ["ask_clarification"]


def keyword_route(message: str) -> list[str]:
    """Route user message to relevant tool names via keyword matching.

    Returns 1-3 tool names. Always includes ask_clarification.
    Default (no match): query_health_data + ask_clarification.
    """
    msg_lower = message.lower()
    matched = set()

    for tool_name, keywords in TOOL_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in msg_lower:
                matched.add(tool_name)
                break

    # Default: health data query
    if not matched:
        matched.add("query_health_data")

    # Always include clarification (added AFTER selection, guaranteed slot)
    matched.update(ALWAYS_AVAILABLE)

    # If user explicitly asks for report/slide/PDF, prioritize report tools FIRST
    report_keywords = ["สไลด์", "รายงาน", "report", "pdf", "slide", "เอกสาร"]
    is_report_request = any(rk in msg_lower for rk in report_keywords)

    if is_report_request:
        PRIORITY = ["generate_adaptive_report", "generate_report", "ask_clarification",
                    "query_health_data", "query_statistical_test", "query_zone_info"]
    else:
        PRIORITY = ["query_health_data", "query_statistical_test", "generate_adaptive_report",
                    "generate_report", "query_zone_info", "ask_clarification"]
    sorted_tools = sorted(matched, key=lambda t: PRIORITY.index(t) if t in PRIORITY else 99)

    # Cap at 4 tools (was 3 — too aggressive, dropped relevant tools)
    return sorted_tools[:4]
