"""Fallback handler — rule-based responses when LLM is unavailable.

Adapted: uses load_district_data() from data_adapter instead of JSON file.
No SQLAlchemy dependency — session parameter removed.
"""
from __future__ import annotations

import re

from services.data_adapter import load_district_data

DISEASE_MAP = {
    "เบาหวาน": "diabetes",
    "diabetes": "diabetes",
    "ความดัน": "hypertension",
    "ความดันโลหิต": "hypertension",
    "hypertension": "hypertension",
    "ไขมัน": "dyslipidemia",
    "ไขมันในเลือด": "dyslipidemia",
    "dyslipidemia": "dyslipidemia",
    "อ้วน": "obesity",
    "โรคอ้วน": "obesity",
    "obesity": "obesity",
    "ไต": "kidney",
    "โรคไต": "kidney",
    "kidney": "kidney",
}

INTENT_PATTERNS = [
    ("advice", [r"ต้องทำ", r"ทำยังไง", r"ทำอย่างไร", r"แนะนำ", r"ป้องกัน", r"รักษา", r"ดูแล", r"ลด.*ความเสี่ยง", r"หลีกเลี่ยง", r"advice", r"prevent", r"how to", r"วิธี"]),
    ("lab_values", [r"ผลเลือด", r"ค่าเลือด", r"lab", r"ผลตรวจเลือด", r"ค่าปกติ", r"normal.*value", r"ผล.*ตรวจ"]),
    ("overview", [r"ภาพรวม", r"สรุป", r"ทั้งหมด", r"overview", r"summary", r"รวม"]),
    ("prevalence", [r"ความชุก", r"prevalence", r"อัตรา", r"เท่าไ[หร]", r"กี่เปอร์เซ็นต์", r"เปอร์เซ็น", r"%"]),
    ("compare_sex", [r"เพศ", r"ชาย.*หญิง", r"หญิง.*ชาย", r"sex", r"gender"]),
    ("trend", [r"แนวโน้ม", r"trend", r"เปลี่ยนแปลง", r"เพิ่ม|ลด"]),
    ("by_area", [r"เขต", r"พื้นที่", r"district", r"area", r"อำเภอ"]),
    ("risk", [r"เสี่ยง", r"risk", r"ปัจจัย", r"factor"]),
    ("stat_test", [r"สถิติ", r"ทดสอบ", r"stat", r"test", r"p-value", r"significant"]),
]


def _load_data():
    data = load_district_data()
    if not data:
        return {}
    return data


def detect_intent(message: str) -> str:
    message_lower = message.lower()
    for intent, patterns in INTENT_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, message_lower):
                return intent
    return "overview"


def detect_disease(message: str) -> str | None:
    message_lower = message.lower()
    for thai_name, key in DISEASE_MAP.items():
        if thai_name in message_lower:
            return key
    return None


def detect_district(message: str, data: dict) -> str | None:
    for code, district in data.items():
        if district["name_th"] in message or district.get("name_en", "").lower() in message.lower():
            return code
    return None


async def handle_fallback(session, message: str, context: dict | None = None) -> dict:
    """Handle fallback (session param kept for API compat, ignored)."""
    data = _load_data()
    intent = detect_intent(message)
    disease = detect_disease(message)
    district = detect_district(message, data)

    if intent == "advice":
        return build_advice(data, disease)
    elif intent == "lab_values":
        return build_lab_values(data)
    elif intent == "overview":
        return build_overview(data, disease)
    elif intent == "prevalence":
        return build_prevalence(data, disease, district)
    elif intent == "compare_sex":
        return build_compare_sex(data, disease)
    elif intent == "by_area":
        return build_by_area(data, disease)
    elif intent == "risk":
        return build_risk(data, disease)
    elif intent == "trend":
        return build_trend(data, disease)
    elif intent == "stat_test":
        return build_stat_test(data, disease)
    else:
        return build_overview(data, disease)


def build_overview(data: dict, disease: str | None) -> dict:
    total_screened = sum(d["total_screened"] for d in data.values())
    num_districts = len(data)

    lines = [
        f"## ภาพรวมการคัดกรองสุขภาพ กรุงเทพมหานคร",
        f"",
        f"- **จำนวนผู้รับการคัดกรอง**: {total_screened:,} คน",
        f"- **จำนวนเขต**: {num_districts} เขต",
        f"",
        f"### สัดส่วนกลุ่มเสี่ยงรายโรค",
        f"",
    ]

    disease_stats = {}
    for d in data.values():
        for dk, dv in d["diseases"].items():
            if dk not in disease_stats:
                disease_stats[dk] = {"name": dv["name"], "total_risk": 0, "total_screened": 0}
            disease_stats[dk]["total_risk"] += round(dv["pct_at_risk"] * d["total_screened"] / 100)
            disease_stats[dk]["total_screened"] += d["total_screened"]

    chart_data = []
    for dk, ds in disease_stats.items():
        pct = round(ds["total_risk"] / ds["total_screened"] * 100, 1) if ds["total_screened"] > 0 else 0
        lines.append(f"- **{ds['name']}**: {pct}% ({ds['total_risk']:,} คน)")
        chart_data.append({"name": ds["name"], "value": pct})

    visualizations = [{
        "type": "bar",
        "title": "สัดส่วนกลุ่มเสี่ยงรายโรค (%)",
        "data": chart_data,
        "xKey": "name",
        "yKey": "value",
        "color": "#00744B",
    }]

    return {"content": "\n".join(lines), "visualizations": visualizations}


def build_prevalence(data: dict, disease: str | None, district: str | None) -> dict:
    if not disease:
        return build_overview(data, None)

    lines = []
    chart_data = []

    if district and district in data:
        d = data[district]
        dd = d["diseases"].get(disease)
        if dd:
            lines.append(f"## ข้อมูล{dd['name']} เขต{d['name_th']}")
            lines.append(f"")
            lines.append(f"- **สัดส่วนกลุ่มเสี่ยง**: {dd['pct_at_risk']}%")
            lines.append(f"- **จำนวนผู้คัดกรอง**: {d['total_screened']:,} คน")
            for ik, iv in dd.get("indicators", {}).items():
                lines.append(f"- **{iv['label']}**: ค่าเฉลี่ย {iv.get('mean', 'N/A')} {iv['unit']}")
    else:
        disease_name = ""
        for d in data.values():
            dd = d["diseases"].get(disease)
            if dd:
                disease_name = dd["name"]
                chart_data.append({"name": d["name_th"], "value": dd["pct_at_risk"]})

        chart_data.sort(key=lambda x: x["value"], reverse=True)
        top5 = chart_data[:5]

        lines.append(f"## ความชุกของ{disease_name} รายเขต")
        lines.append(f"")
        lines.append(f"### เขตที่มีสัดส่วนกลุ่มเสี่ยงสูงสุด 5 อันดับ")
        lines.append(f"")
        for i, item in enumerate(top5, 1):
            lines.append(f"{i}. **{item['name']}**: {item['value']}%")

    visualizations = []
    if chart_data:
        visualizations.append({
            "type": "bar",
            "title": f"ความชุก{disease_name}รายเขต (%)",
            "data": chart_data[:10],
            "xKey": "name",
            "yKey": "value",
            "color": "#ef4444",
        })

    return {"content": "\n".join(lines), "visualizations": visualizations}


def build_compare_sex(data: dict, disease: str | None) -> dict:
    try:
        from agents.tools.query_api import _query
        rows = _query("""
            SELECT p.sex, COUNT(DISTINCT v.patient_id) AS total,
                   COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm) AS risk_dm,
                   COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt) AS risk_hpt,
                   COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_obesity) AS found_obesity
            FROM raw_vitalsigns v
            JOIN raw_patients p ON v.patient_id = p.id
            WHERE v.cancel_status IS DISTINCT FROM 1
            GROUP BY p.sex
        """)
        sex_labels = {10: "ชาย", 20: "หญิง"}
        lines = ["## เปรียบเทียบรายเพศ", ""]
        chart_data = []
        for r in rows:
            label = sex_labels.get(r.get("sex"), f"เพศ {r.get('sex')}")
            t = r.get("total") or 1
            dm_pct = round(100.0 * (r.get("risk_dm") or 0) / t, 1)
            hpt_pct = round(100.0 * (r.get("risk_hpt") or 0) / t, 1)
            lines.append(f"- **{label}** ({t:,} คน): เสี่ยงเบาหวาน {dm_pct}%, ความดัน {hpt_pct}%")
            chart_data.append({"name": label, "value": dm_pct})
        viz = [{"type": "bar", "title": "เสี่ยงเบาหวาน แยกเพศ (%)", "data": chart_data, "xKey": "name", "yKey": "value", "color": "#3b82f6"}] if chart_data else []
        return {"content": "\n".join(lines), "visualizations": viz}
    except Exception:
        lines = ["## เปรียบเทียบรายเพศ", "", "ไม่สามารถดึงข้อมูลแยกเพศได้ในขณะนี้"]
        return {"content": "\n".join(lines), "visualizations": []}


def build_by_area(data: dict, disease: str | None) -> dict:
    if not disease:
        disease = "diabetes"

    disease_name = ""
    chart_data = []
    for d in data.values():
        dd = d["diseases"].get(disease)
        if dd:
            disease_name = dd["name"]
            chart_data.append({"name": d["name_th"], "value": dd["pct_at_risk"], "screened": d["total_screened"]})

    chart_data.sort(key=lambda x: x["value"], reverse=True)

    lines = [
        f"## {disease_name} รายเขตพื้นที่",
        f"",
        f"### เขตที่มีสัดส่วนกลุ่มเสี่ยงสูงสุด",
        f"",
    ]
    for i, item in enumerate(chart_data[:10], 1):
        lines.append(f"{i}. **{item['name']}**: {item['value']}% (คัดกรอง {item['screened']:,} คน)")

    visualizations = [{
        "type": "bar",
        "title": f"{disease_name} รายเขตพื้นที่ (%)",
        "data": chart_data[:15],
        "xKey": "name",
        "yKey": "value",
        "color": "#f59e0b",
    }]

    return {"content": "\n".join(lines), "visualizations": visualizations}


def build_risk(data: dict, disease: str | None) -> dict:
    if not disease:
        disease = "diabetes"

    disease_name = ""
    indicators_info = []
    for d in list(data.values())[:1]:
        dd = d["diseases"].get(disease)
        if dd:
            disease_name = dd["name"]
            for ik, iv in dd.get("indicators", {}).items():
                indicators_info.append(iv)

    lines = [
        f"## ปัจจัยเสี่ยง{disease_name}",
        f"",
    ]
    for iv in indicators_info:
        lines.append(f"- **{iv['label']}**: จุดตัด {iv.get('cutoff', 'N/A')} {iv['unit']}, เกินเกณฑ์ {iv.get('pct_above_cutoff', 0)}%")

    return {"content": "\n".join(lines), "visualizations": []}


def build_trend(data: dict, disease: str | None) -> dict:
    try:
        from agents.tools.query_api import _yoy_comparison
        result = _yoy_comparison()
        quarters = result.get("quarters", [])
        if quarters:
            lines = ["## แนวโน้มการคัดกรอง (รายไตรมาส)", ""]
            chart_data = []
            for q in quarters:
                period = str(q.get("quarter", ""))[:7]
                screened = q.get("screened", 0)
                lines.append(f"- **{period}**: คัดกรอง {screened:,} คน")
                chart_data.append({"name": period, "value": screened})
            viz = [{"type": "line", "title": "จำนวนคัดกรองรายไตรมาส", "data": chart_data, "xKey": "name", "yKey": "value", "color": "#00744B"}] if chart_data else []
            return {"content": "\n".join(lines), "visualizations": viz}
    except Exception:
        pass
    lines = ["## แนวโน้มข้อมูล", "", "ข้อมูลแนวโน้มรายปียังไม่เพียงพอสำหรับการวิเคราะห์"]
    return {"content": "\n".join(lines), "visualizations": []}


def build_stat_test(data: dict, disease: str | None) -> dict:
    if not disease:
        disease = "diabetes"

    disease_name = ""
    values = []
    for d in data.values():
        dd = d["diseases"].get(disease)
        if dd:
            disease_name = dd["name"]
            values.append(dd["pct_at_risk"])

    if values:
        mean_val = sum(values) / len(values)
        variance = sum((x - mean_val) ** 2 for x in values) / len(values)
        std_val = variance ** 0.5
        min_val = min(values)
        max_val = max(values)

        lines = [
            f"## สถิติเชิงพรรณนา -- {disease_name}",
            f"",
            f"- **ค่าเฉลี่ย (Mean)**: {mean_val:.1f}%",
            f"- **ส่วนเบี่ยงเบนมาตรฐาน (SD)**: {std_val:.1f}%",
            f"- **ค่าต่ำสุด**: {min_val:.1f}%",
            f"- **ค่าสูงสุด**: {max_val:.1f}%",
            f"- **จำนวนเขต**: {len(values)} เขต",
        ]
    else:
        lines = ["## ไม่พบข้อมูลสำหรับการวิเคราะห์"]

    return {"content": "\n".join(lines), "visualizations": []}


ADVICE_DB: dict[str, dict] = {
    "obesity": {
        "name": "โรคอ้วน",
        "prevention": [
            "ควบคุมอาหาร ลดอาหารที่มีไขมันสูงและน้ำตาลสูง",
            "ออกกำลังกายอย่างน้อย 150 นาทีต่อสัปดาห์ (เช่น เดินเร็ว ว่ายน้ำ ปั่นจักรยาน)",
            "กินผักผลไม้เพิ่มขึ้น อย่างน้อย 5 ส่วนต่อวัน",
            "ลดเครื่องดื่มที่มีน้ำตาล เช่น ชานมไข่มุก น้ำอัดลม",
            "นอนหลับให้เพียงพอ 7-9 ชั่วโมงต่อคืน",
            "ตรวจสุขภาพประจำปี วัดรอบเอวและดัชนีมวลกาย (BMI)",
        ],
        "warning": "ค่า BMI >= 25 ถือว่าน้ำหนักเกิน, >= 30 ถือว่าเป็นโรคอ้วน (เกณฑ์ WHO สำหรับเอเชีย)",
    },
    "diabetes": {
        "name": "เบาหวาน",
        "prevention": [
            "ควบคุมน้ำหนักให้อยู่ในเกณฑ์ปกติ (BMI 18.5-22.9)",
            "ลดอาหารแป้งขัดขาว ข้าวขาว ขนมหวาน เลือกธัญพืชไม่ขัดสี",
            "ออกกำลังกายสม่ำเสมอ อย่างน้อย 30 นาทีต่อวัน",
            "ตรวจระดับน้ำตาลในเลือดเป็นประจำ (FBS ควร < 100 mg/dL)",
            "หลีกเลี่ยงเครื่องดื่มที่มีน้ำตาลสูง",
            "หากมีประวัติครอบครัว ควรตรวจคัดกรองทุก 1-3 ปี",
        ],
        "warning": "ระดับน้ำตาลในเลือด (FBS) >= 126 mg/dL ถือว่าเป็นเบาหวาน",
    },
    "hypertension": {
        "name": "ความดันโลหิตสูง",
        "prevention": [
            "ลดอาหารเค็ม โซเดียมไม่เกิน 2,000 มก./วัน",
            "ออกกำลังกายอย่างสม่ำเสมอ เช่น เดินเร็ว 30 นาที/วัน",
            "ควบคุมน้ำหนัก ลดความเครียด",
            "หลีกเลี่ยงแอลกอฮอล์และบุหรี่",
            "วัดความดันเป็นประจำ เป้าหมาย < 140/90 mmHg",
            "กินผักผลไม้ที่มีโพแทสเซียมสูง เช่น กล้วย ส้ม ผักใบเขียว",
        ],
        "warning": "ความดัน >= 140/90 mmHg ถือว่าเป็นความดันโลหิตสูง",
    },
    "dyslipidemia": {
        "name": "ไขมันในเลือดผิดปกติ",
        "prevention": [
            "ลดอาหารไขมันอิ่มตัว เช่น ของทอด เนื้อสัตว์ติดมัน",
            "เพิ่มอาหารที่มีไขมันดี เช่น ปลา ถั่ว น้ำมันมะกอก",
            "กินใยอาหารเพิ่มขึ้น เช่น ข้าวกล้อง ผัก ผลไม้",
            "ออกกำลังกายสม่ำเสมอเพื่อเพิ่ม HDL (ไขมันดี)",
            "ตรวจระดับไขมันในเลือดเป็นประจำ",
            "หลีกเลี่ยงอาหาร trans fat เช่น เบเกอรี่ มาร์การีน",
        ],
        "warning": "LDL >= 160 mg/dL หรือ Total Cholesterol >= 240 mg/dL ถือว่าผิดปกติ",
    },
    "kidney": {
        "name": "โรคไตเรื้อรัง",
        "prevention": [
            "ดื่มน้ำเปล่าให้เพียงพอ 6-8 แก้วต่อวัน",
            "ควบคุมความดันและน้ำตาลในเลือด",
            "ลดอาหารเค็ม โปรตีนสูง และอาหารแปรรูป",
            "หลีกเลี่ยงยาแก้ปวด NSAIDs เป็นประจำ",
            "ตรวจค่าไต (eGFR, creatinine) เป็นประจำ",
            "ไม่ซื้อยากินเอง โดยเฉพาะยาสมุนไพรไม่ทราบที่มา",
        ],
        "warning": "eGFR < 60 mL/min ถือว่าไตเริ่มเสื่อม ควรพบแพทย์",
    },
}


def build_advice(data: dict, disease: str | None) -> dict:
    if not disease:
        lines = [
            "## คำแนะนำด้านสุขภาพทั่วไป",
            "",
            "การดูแลสุขภาพเบื้องต้นที่สำคัญ:",
            "",
            "1. **ออกกำลังกาย** อย่างน้อย 150 นาทีต่อสัปดาห์",
            "2. **ควบคุมอาหาร** ลดหวาน มัน เค็ม",
            "3. **ตรวจสุขภาพประจำปี** เพื่อคัดกรองโรคเรื้อรัง",
            "4. **นอนหลับเพียงพอ** 7-9 ชั่วโมง",
            "5. **จัดการความเครียด** ด้วยการพักผ่อนและทำกิจกรรมที่ชอบ",
            "",
            "ลองถามเจาะจงโรค เช่น \"โรคอ้วนต้องทำยังไง\" หรือ \"ป้องกันเบาหวาน\"",
        ]
        return {"content": "\n".join(lines), "visualizations": []}

    info = ADVICE_DB.get(disease)
    if not info:
        return {"content": "ขออภัย ยังไม่มีคำแนะนำสำหรับโรคนี้", "visualizations": []}

    total_risk = 0
    total_screened = 0
    for d in data.values():
        dd = d["diseases"].get(disease)
        if dd:
            total_risk += round(dd["pct_at_risk"] * d["total_screened"] / 100)
            total_screened += d["total_screened"]
    pct = round(total_risk / total_screened * 100, 1) if total_screened > 0 else 0

    lines = [
        f"## คำแนะนำ -- {info['name']}",
        "",
        f"**สถานการณ์ใน กทม.**: พบกลุ่มเสี่ยง **{pct}%** ({total_risk:,} คน จาก {total_screened:,} คน)",
        "",
        "### วิธีป้องกันและดูแลตัวเอง",
        "",
    ]
    for i, tip in enumerate(info["prevention"], 1):
        lines.append(f"{i}. {tip}")

    lines.append("")
    lines.append(f"**เกณฑ์สำคัญ**: {info['warning']}")
    lines.append("")
    lines.append("---")
    lines.append("*ข้อมูลนี้เป็นคำแนะนำเบื้องต้น ควรปรึกษาแพทย์เพื่อรับคำแนะนำเฉพาะบุคคล*")

    return {"content": "\n".join(lines), "visualizations": []}


def build_lab_values(data: dict) -> dict:
    """Lab values with reference ranges + actual BMA averages."""
    try:
        from agents.tools.query_api import _lab_city_average
        city = _lab_city_average()
    except Exception:
        city = {}

    lines = [
        "## ค่าผลตรวจเลือดสำคัญ",
        "",
        "| รายการ | ค่าเฉลี่ย กทม. | ค่าปกติ | หน่วย |",
        "|--------|---------------|--------|------|",
    ]

    ref = [
        ("น้ำตาลในเลือด (FBS)", city.get("avg_fbs"), "< 100", "mg/dL"),
        ("คอเลสเตอรอล", city.get("avg_cholesterol"), "< 200", "mg/dL"),
        ("ไตรกลีเซอไรด์", city.get("avg_triglyceride"), "< 150", "mg/dL"),
        ("HDL (ไขมันดี)", city.get("avg_hdl"), "> 40", "mg/dL"),
        ("LDL (ไขมันไม่ดี)", city.get("avg_ldl"), "< 130", "mg/dL"),
        ("ฮีโมโกลบิน", city.get("avg_hemoglobin"), "> 12", "g/dL"),
        ("ครีเอตินิน", city.get("avg_creatinine"), "< 1.2", "mg/dL"),
        ("eGFR (การกรองไต)", city.get("avg_egfr"), "> 60", "mL/min"),
    ]
    for label, val, normal, unit in ref:
        val_str = f"{val}" if val else "ไม่มีข้อมูล"
        lines.append(f"| {label} | {val_str} | {normal} | {unit} |")

    lines.extend([
        "",
        "**คำแนะนำ**: ค่าผิดปกติไม่ได้แปลว่าเป็นโรค ควรปรึกษาแพทย์เพื่อแปลผลเฉพาะบุคคล",
        "",
        "*ข้อมูลเฉลี่ยจากการคัดกรองสุขภาพ กทม.*",
    ])

    return {"content": "\n".join(lines), "visualizations": []}
