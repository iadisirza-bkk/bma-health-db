"""Keyword router — pre-route user messages to relevant tools without LLM.

Reduces payload from 6 tools (~3.5KB) to 1-2 tools (~1KB).
NOTE: query_backend_api removed — we query DB directly now.

Matching uses substring containment because Thai has no word delimiters,
so word-boundary regex (\\b) doesn't help. We compensate by:
  1. Adding common Thai verbs that frequently introduce queries (ดู/อยาก/ขอ/ช่วย)
     so they don't fall through to the default tool with no signal.
  2. Removing keywords that overlap heavily with district/disease names
     (those false-positives matter more than the false-negatives).
"""
from __future__ import annotations

TOOL_KEYWORDS: dict[str, list[str]] = {
    "query_health_data": [
        "เสี่ยง", "อัตรา", "เปรียบเทียบ", "เขต", "โซน", "อายุ", "เพศ",
        "กราฟ", "chart", "ภาพรวม", "district", "zone", "disease",
        "โรค", "สุขภาพ", "obesity", "diabetes", "hypertension",
        "สถานการณ์", "กี่", "เท่าไหร่", "แยกตาม", "ranking",
        "กลุ่มอายุ", "วัยทำงาน", "วัยเรียน", "ผู้สูงอายุ", "age group",
        "พฤติกรรม",
        # Common Thai query openers — previously fell through to default
        "ดู", "ดูข้อมูล", "ดูสถิติ", "ดูเปรียบเทียบ", "อยากรู้", "อยากดู",
        "ขอข้อมูล", "ช่วยดู", "แสดง", "show", "แสดงผล",
    ],
    "query_api": [
        # KPI / targets
        "kpi", "เป้าหมาย", "target", "สธ", "moph", "coverage", "ครอบคลุม",
        # NCD cascade / care continuum
        "cascade", "คัดกรอง.*วินิจฉัย", "คัดกรอง.*รักษา", "ncd", "continuum",
        # Lab results
        "lab", "ผลเลือด", "ผลตรวจ", "fbs", "cholesterol", "hemoglobin", "ค่าเลือด",
        "น้ำตาล", "ไขมัน", "creatinine", "egfr", "โลหิตจาง", "ไต",
        "hba1c", "เอวันซี", "น้ำตาลสะสม", "fpg", "glucose", "ldl", "hdl",
        "triglyceride", "ไตรกลีเซอไรด์",
        # Cost / budget
        "งบ", "budget", "ต้นทุน", "cost", "ค่าใช้จ่าย", "จัดสรร", "งบประมาณ",
        # Screening tests
        "ekg", "x-ray", "xray", "เอกซเรย์", "chest", "คลื่นหัวใจ", "vision", "ตา", "จอประสาท",
        # Chronic / treatment / vaccination
        "รักษา", "treatment", "ยา", "adherence", "วัคซีน", "vaccine",
        "โรคเรื้อรัง", "chronic", "ประวัติ",
        # Family history
        "ครอบครัว", "พ่อแม่", "พันธุกรรม", "family",
        # Comorbidity / cross-tab
        "ร่วมกับ", "ร่วมกัน", "comorbid", "ร่วม.*กี่คน", "metabolic",
        "cross-tab", "cross tab", "crosstab", "แยกตาม", "เทียบกับ",
        "เป็นทั้ง", "ทั้ง.*และ", "พร้อมกัน", "ในเวลาเดียว",
        "หลายโรค", "โรคหลาย", "หลายโรค.*คน",
        # Facilities / locations
        "สถานพยาบาล", "facility", "จุดคัดกรอง", "ที่ไหน.*ตรวจ", "ตรวจ.*ที่ไหน",
        # YoY / comparison
        "ปีที่แล้ว", "เทียบ.*ปี", "yoy", "year",
        # BMI / waist distribution
        "bmi", "รอบเอว", "waist", "น้ำหนัก", "อ้วน.*กี่",
        # Exercise
        "ออกกำลังกาย", "exercise",
        # Overview / general
        "overview", "คัดกรอง.*กี่คน", "ข้อมูล",
        # Screening yield / repeat
        "yield", "ซ้ำ", "repeat",
    ],
    "query_statistical_test": [
        "ทดสอบ", "สถิติ", "chi", "odds", "anova", "logistic", "correlation",
        "mann", "comorbidity", "p-value", "ความสัมพันธ์", "significance",
        "นัยสำคัญ", "forest", "trend", "แนวโน้ม",
        "สัมพันธ์", "เกี่ยวข้อง", "ปัจจัย", "สาเหตุ",
        "สูบบุหรี่", "ดื่มเหล้า", "ไม่ออกกำลังกาย",
        # Multi-disease / comorbidity vocab — eval found these missed.
        # The comorbidity test inside query_statistical_test answers them.
        "เชื่อมโยง", "โรคร่วม", "หลายโรค", "พร้อมกัน", "ร่วมกัน",
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
        # Catch "ดูโซน X" / "โซนไหนดูแล" style questions
        "ดูโซน", "โซนไหน", "ผู้รับผิดชอบ", "หน่วยที่ดูแล",
    ],
    # --- New insight tools (7) ---
    "query_time_trend": [
        "แนวโน้ม", "trend", "เปลี่ยนไป", "ราย.*เดือน", "ราย.*ไตรมาส",
        "monthly", "quarter", "ไตรมาส", "เดือน",
        "ปี 20", "ปี 25", "เพิ่มขึ้น.*ตามเวลา", "ลดลง.*ตามเวลา",
        "ช่วง.*ปี", "ระหว่างปี", "เทียบ.*เดือน",
    ],
    "query_province_breakdown": [
        "ตจว", "ต่างจังหวัด", "นอกกทม", "นอก กทม", "non-bkk", "non bkk",
        "จังหวัดไหน", "จังหวัดต้นทาง", "บ้านเดิม", "ต้นทาง",
        "ภูมิภาค", "ภาค.*ไหน", "ภาคอีสาน", "ภาคเหนือ", "ภาคใต้",
        "province", "region",
    ],
    "query_facility": [
        "สถานพยาบาล", "คลินิก", "ร้านยา", "facility", "clinic", "pharmacy",
        "รพ..*ในเขต", "รพ..*ในโซน", "โรงพยาบาล.*ในเขต", "โรงพยาบาล.*ในโซน",
        "มี.*กี่ที่", "มี.*กี่แห่ง", "ทำเนียบ", "หน่วยบริการ",
        "ในเขต.*มี", "ในโซน.*มี",
    ],
    "query_risk_profile": [
        "โปรไฟล์", "profile", "ลักษณะ", "characteristic",
        "เพศไหน.*อายุ", "อายุ.*เพศ", "พฤติกรรม.*เพศ",
        "ผู้ป่วย.*ส่วนใหญ่", "คนที่.*ส่วนใหญ่",
        "demographic", "demographics",
    ],
    "query_district_compare": [
        "สูงสุด.*ต่ำสุด", "ต่ำสุด.*สูงสุด", "top.*bottom",
        "เปรียบเทียบ.*สูงสุด", "เปรียบเทียบ.*เขต.*โรค",
        "อันดับ.*เขต", "ranking.*district", "percentile",
        "เขต.*อ้วนสูง", "เขต.*เบาหวานสูง", "ที่สุด",
    ],
    "query_mental_health": [
        "phq", "phq-9", "phq9", "ซึมเศร้า", "depression",
        "สุขภาพจิต", "เครียด", "stress", "mental", "จิต",
    ],
    "query_ncd_cascade": [
        "cascade", "เส้นทาง", "ตรวจ.*พบ.*วินิจฉัย", "พบ.*ส่งต่อ", "พบ.*รักษา",
        "ncd cascade", "continuum", "ผ่านขั้นตอน", "stage",
    ],
    # ask_clarification is NEVER pre-selected — LLM decides when to use it
}

# Tools always available (LLM can call even if not keyword-matched)
ALWAYS_AVAILABLE = ["ask_clarification"]


import re as _re


def _kw_matches(keyword: str, message_lower: str) -> bool:
    """Match a keyword against a message.

    A keyword containing `.*` is treated as a regex pattern (re.search);
    everything else uses a fast substring check. This lets the keyword
    table express "X near Y" patterns like "คัดกรอง.*วินิจฉัย" — those used
    to silently never match because the table was scanned with `in`.
    """
    if ".*" in keyword:
        try:
            return _re.search(keyword.lower(), message_lower) is not None
        except _re.error:
            # Bad pattern in the table — fall back to substring so it at
            # least partially works rather than silently never matching.
            return keyword.lower() in message_lower
    return keyword.lower() in message_lower


def keyword_route(message: str) -> list[str]:
    """Route user message to relevant tool names via keyword matching.

    Returns 1-4 tool names. Always includes ask_clarification.
    Default (no match): query_health_data + ask_clarification.
    """
    msg_lower = message.lower()
    matched = set()

    for tool_name, keywords in TOOL_KEYWORDS.items():
        for kw in keywords:
            if _kw_matches(kw, msg_lower):
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

    # Specific insight tools rank HIGHER than the generic query_health_data
    # so the LLM gets the specialized schema first when keywords match.
    INSIGHTS = [
        "query_time_trend", "query_province_breakdown", "query_facility",
        "query_risk_profile", "query_district_compare",
        "query_mental_health", "query_ncd_cascade",
    ]

    if is_report_request:
        PRIORITY = ["generate_adaptive_report", "generate_report", "ask_clarification",
                    *INSIGHTS,
                    "query_api", "query_health_data", "query_statistical_test", "query_zone_info"]
    else:
        PRIORITY = [*INSIGHTS,
                    "query_api", "query_health_data", "query_statistical_test",
                    "generate_adaptive_report", "generate_report", "query_zone_info",
                    "ask_clarification"]
    sorted_tools = sorted(matched, key=lambda t: PRIORITY.index(t) if t in PRIORITY else 99)

    # Cap at 4 tools (was 3 — too aggressive, dropped relevant tools)
    return sorted_tools[:4]
