"""QueryAPITool — gives the LLM agent direct access to ALL backend API endpoints.

This is the key bridge: instead of calling HTTP (old backend_api tool), we call
the router functions directly in-process via the FastAPI TestClient-like pattern,
or more simply, by importing and calling the underlying functions.

We use a lightweight approach: call database.execute_query() with curated SQL
for the most important endpoint groups, matching what each router does.
"""
from __future__ import annotations

import logging
from typing import Any

from agents.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Endpoint catalog — maps logical endpoint names to query functions
# ---------------------------------------------------------------------------

ENDPOINT_CATALOG = {
    # Executive
    "headline_kpi": "Governor headline KPIs (total screened, top disease, coverage %)",
    "yoy_comparison": "Year-over-year disease trend comparison",
    "executive_alert": "Active health alerts and warnings",
    # KPI
    "moph_targets": "Compare against Ministry of Public Health NCD targets",
    "screening_yield": "Risk detection rate (yield %) per district",
    "zone_comparison": "Cross-zone KPI comparison",
    # Disease Control
    "ncd_cascade": "NCD care cascade: screened → at risk → diagnosed → treatment",
    "screening_coverage": "Screening coverage rate per district vs target",
    "repeat_screening": "Repeat screening / follow-up visit rates",
    # Lab
    "lab_summary": "Average lab values by district (FBS, cholesterol, hemoglobin, etc.)",
    "lab_city_average": "City-wide average lab values",
    "disease_lab_crosstab": "Cross-tab: lab values (FBS, SBP, cholesterol) by disease status (DM+/DM-, HPT+/HPT-)",
    # Promotion
    "bmi_distribution": "BMI category distribution (underweight/normal/overweight/obese)",
    "exercise_frequency": "Exercise frequency distribution",
    # Strategy
    "cost_per_screening": "Cost analysis (THB per person screened)",
    "budget_allocation": "Budget allocation model by district",
    # Facility
    "facility_performance": "Screening counts per facility",
    "facility_yield_rank": "Facilities ranked by screening yield",
    # Screening Tests
    "screening_tests": "EKG, chest X-ray, vision, DR screening rates",
    # Chronic History
    "chronic_history": "Known chronic conditions, treatment adherence, vaccination",
    "family_history": "Family disease history (DM, HPT, stroke, etc.)",
    # Public
    "screening_locations": "Health center locations with coordinates",
    "district_summary": "Simplified district health summary (Thai)",
    # Comorbidity
    "comorbidity_matrix": "Disease co-occurrence counts (DM+HPT, metabolic syndrome, etc.)",
    # Overview
    "overview": "Top-level screening overview with zone and disease breakdown",
}


def _query(sql: str, params: tuple = None) -> list[dict]:
    """Execute SQL and return results."""
    from database import execute_query
    return execute_query(sql, params)


def _scalar(sql: str, params: tuple = None):
    """Execute SQL and return single value."""
    from database import execute_scalar
    return execute_scalar(sql, params)


# ---------------------------------------------------------------------------
# Query implementations — one function per endpoint group
# ---------------------------------------------------------------------------

def _overview() -> dict:
    rows = _query("""
        SELECT d.zone_code, COUNT(DISTINCT d.district_code) AS districts,
               SUM(d.total_screened) AS total_screened,
               SUM(d.risk_dm_count) AS risk_dm, SUM(d.risk_hpt_count) AS risk_hpt,
               SUM(d.risk_cvd_count) AS risk_cvd, SUM(d.risk_bmi_count) AS risk_bmi,
               SUM(d.found_dm_count) AS found_dm, SUM(d.found_hpt_count) AS found_hpt,
               SUM(d.found_obesity_count) AS found_obesity,
               SUM(d.found_dyslipidemia_count) AS found_dyslipidemia,
               SUM(d.found_stroke_count) AS found_stroke
        FROM summary_district_disease d
        GROUP BY d.zone_code ORDER BY d.zone_code
    """)
    total = sum(r.get("total_screened", 0) or 0 for r in rows)
    return {"total_screened": total, "target": 1_600_000, "zones": rows}


_DISEASE_TH = {
    "diabetes": "เบาหวาน", "hypertension": "ความดันโลหิตสูง",
    "cardiovascular": "หลอดเลือดหัวใจ", "obesity": "อ้วน",
    "dyslipidemia": "ไขมันในเลือดสูง", "stroke": "หลอดเลือดสมอง",
    "ckd": "ไตเรื้อรัง", "anemia": "โลหิตจาง",
}


def _headline_kpi() -> dict:
    total = int(_scalar("SELECT SUM(total_screened) FROM summary_district_disease") or 0)
    diseases = _query("""
        SELECT 'diabetes' AS disease, SUM(risk_dm_count) AS at_risk FROM summary_district_disease
        UNION ALL SELECT 'hypertension', SUM(risk_hpt_count) FROM summary_district_disease
        UNION ALL SELECT 'cardiovascular', SUM(risk_cvd_count) FROM summary_district_disease
        UNION ALL SELECT 'obesity', SUM(found_obesity_count) FROM summary_district_disease
        UNION ALL SELECT 'dyslipidemia', SUM(found_dyslipidemia_count) FROM summary_district_disease
        UNION ALL SELECT 'stroke', SUM(found_stroke_count) FROM summary_district_disease
        ORDER BY at_risk DESC
    """)
    # Clean: int values + Thai names
    for d in diseases:
        d["at_risk"] = int(d.get("at_risk") or 0)
        d["name_th"] = _DISEASE_TH.get(d["disease"], d["disease"])
    top = diseases[0] if diseases else {}
    return {
        "total_screened": total,
        "coverage_pct": round(100.0 * total / 1_600_000, 2),
        "top_disease": top.get("name_th", top.get("disease")),
        "top_disease_count": top.get("at_risk"),
        "diseases": diseases,
    }


def _yoy_comparison() -> dict:
    rows = _query("""
        SELECT DATE_TRUNC('quarter', v.visit_date)::date AS quarter,
               COUNT(DISTINCT v.patient_id) AS screened,
               COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm) AS risk_dm,
               COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt) AS risk_hpt,
               COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_obesity) AS found_obesity
        FROM raw_vitalsigns v
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND v.visit_date IS NOT NULL
          AND v.visit_date >= '2024-01-01'
        GROUP BY DATE_TRUNC('quarter', v.visit_date)
        ORDER BY quarter
    """)
    # Format as readable text so synthesizer can summarize
    lines = ["เปรียบเทียบรายไตรมาส (2024-ปัจจุบัน):"]
    for r in rows:
        q = str(r.get("quarter", ""))[:10]
        lines.append(
            f"- {q}: คัดกรอง {int(r.get('screened', 0)):,} คน, "
            f"เสี่ยงเบาหวาน {int(r.get('risk_dm', 0)):,}, "
            f"เสี่ยงความดัน {int(r.get('risk_hpt', 0)):,}, "
            f"พบอ้วน {int(r.get('found_obesity', 0)):,}"
        )
    if len(rows) >= 2:
        latest = rows[-1]
        prev = rows[-2]
        delta = int(latest.get("screened", 0)) - int(prev.get("screened", 0))
        pct = round(100.0 * delta / max(int(prev.get("screened", 0)), 1), 1)
        lines.append(f"\nเทียบไตรมาสล่าสุด: {'เพิ่มขึ้น' if delta > 0 else 'ลดลง'} {abs(delta):,} คน ({pct:+.1f}%)")
    return {"summary": "\n".join(lines)}


def _moph_targets() -> dict:
    total = _scalar("SELECT SUM(total_screened) FROM summary_district_disease") or 0
    risk_dm = _scalar("SELECT SUM(risk_dm_count) FROM summary_district_disease") or 0
    risk_hpt = _scalar("SELECT SUM(risk_hpt_count) FROM summary_district_disease") or 0
    found_obesity = _scalar("SELECT SUM(found_obesity_count) FROM summary_district_disease") or 0
    t = max(total, 1)
    return {"kpis": [
        {"code": "NCD-01", "name": "Coverage", "target_pct": 60, "actual_pct": round(100.0 * total / 1_600_000, 2), "status": "PASS" if total / 1_600_000 >= 0.6 else "FAIL"},
        {"code": "NCD-02", "name": "DM detection", "target_pct": 5, "actual_pct": round(100.0 * risk_dm / t, 2), "status": "PASS" if risk_dm / t >= 0.05 else "FAIL"},
        {"code": "NCD-03", "name": "HPT detection", "target_pct": 10, "actual_pct": round(100.0 * risk_hpt / t, 2), "status": "PASS" if risk_hpt / t >= 0.10 else "FAIL"},
        {"code": "NCD-04", "name": "Obesity control", "target_pct": 30, "actual_pct": round(100.0 * found_obesity / t, 2), "status": "PASS" if found_obesity / t < 0.30 else "FAIL"},
    ]}


def _ncd_cascade() -> dict:
    total = _scalar("SELECT SUM(total_screened) FROM summary_district_disease") or 0
    at_risk = _scalar("SELECT SUM(risk_dm_count + risk_hpt_count + risk_cvd_count) FROM summary_district_disease") or 0
    diagnosed = _scalar("SELECT SUM(found_dm_count + found_hpt_count + found_cvd_count) FROM summary_district_disease") or 0
    treatment = _scalar("SELECT SUM(dm_on_treatment + hpt_on_treatment) FROM summary_chronic_history") or 0
    return {"cascade": [
        {"stage": "คัดกรอง (Screened)", "count": total},
        {"stage": "พบเสี่ยง (At Risk)", "count": at_risk},
        {"stage": "วินิจฉัย (Diagnosed)", "count": diagnosed},
        {"stage": "รักษา (Treatment)", "count": treatment, "note": "from homehealth self-report"},
    ]}


def _screening_coverage() -> dict:
    rows = _query("""
        SELECT d.district_code, d.district_name, d.total_screened,
               rd.population,
               ROUND(100.0 * d.total_screened / NULLIF(rd.population, 0), 2) AS coverage_pct
        FROM summary_district_disease d
        JOIN ref_districts rd ON rd.dcode = d.district_code
        WHERE d.total_screened >= 5
        ORDER BY coverage_pct DESC
    """)
    return {"districts": rows}


def _lab_summary() -> dict:
    rows = _query("""
        SELECT district_code, total_lab_patients,
               ROUND(avg_fbs::numeric, 1) AS avg_fbs,
               ROUND(avg_cholesterol::numeric, 1) AS avg_cholesterol,
               ROUND(avg_triglyceride::numeric, 1) AS avg_triglyceride,
               ROUND(avg_hdl::numeric, 1) AS avg_hdl,
               ROUND(avg_ldl::numeric, 1) AS avg_ldl,
               ROUND(avg_hemoglobin::numeric, 1) AS avg_hemoglobin,
               ROUND(avg_creatinine::numeric, 2) AS avg_creatinine,
               ROUND(avg_egfr::numeric, 1) AS avg_egfr,
               pct_anemia, pct_ckd
        FROM summary_district_lab
        ORDER BY district_code
    """)
    return {"districts": rows}


def _lab_city_average() -> dict:
    row = _query("""
        SELECT SUM(total_lab_patients) AS total,
               ROUND(AVG(avg_fbs)::numeric, 1) AS avg_fbs,
               ROUND(AVG(avg_cholesterol)::numeric, 1) AS avg_cholesterol,
               ROUND(AVG(avg_triglyceride)::numeric, 1) AS avg_triglyceride,
               ROUND(AVG(avg_hdl)::numeric, 1) AS avg_hdl,
               ROUND(AVG(avg_ldl)::numeric, 1) AS avg_ldl,
               ROUND(AVG(avg_hemoglobin)::numeric, 1) AS avg_hemoglobin,
               ROUND(AVG(avg_creatinine)::numeric, 2) AS avg_creatinine,
               ROUND(AVG(avg_egfr)::numeric, 1) AS avg_egfr,
               ROUND(AVG(pct_anemia)::numeric, 1) AS avg_pct_anemia,
               ROUND(AVG(pct_ckd)::numeric, 1) AS avg_pct_ckd
        FROM summary_district_lab
    """)
    return row[0] if row else {}


def _bmi_distribution() -> dict:
    row = _query("""
        SELECT SUM(total_measured) AS total,
               SUM(bmi_underweight) AS underweight,
               SUM(bmi_normal) AS normal,
               SUM(bmi_overweight) AS overweight,
               SUM(bmi_obese) AS obese,
               SUM(bmi_severely_obese) AS severely_obese,
               ROUND(AVG(avg_bmi)::numeric, 1) AS avg_bmi,
               ROUND(AVG(avg_waist)::numeric, 1) AS avg_waist
        FROM summary_bmi_waist
        WHERE district_code ~ '^[0-9]' AND sex = -1
    """)
    if not row:
        return {}
    r = row[0]
    total = int(r.get("total") or 1)
    def _p(k):
        v = int(r.get(k) or 0)
        return f"{v:,} คน ({round(100*v/total,1)}%)"
    return {
        "summary": (
            f"การกระจายตัว BMI ของผู้คัดกรอง ({total:,} คน):\n"
            f"- น้ำหนักต่ำกว่าเกณฑ์ (Underweight): {_p('underweight')}\n"
            f"- ปกติ (Normal): {_p('normal')}\n"
            f"- น้ำหนักเกิน (Overweight): {_p('overweight')}\n"
            f"- อ้วน (Obese): {_p('obese')}\n"
            f"- อ้วนมาก (Severely Obese): {_p('severely_obese')}\n"
            f"- BMI เฉลี่ย: {r.get('avg_bmi')} kg/m²\n"
            f"- รอบเอวเฉลี่ย: {r.get('avg_waist')} cm"
        ),
    }


def _cost_per_screening() -> dict:
    total = _scalar("SELECT SUM(total_screened) FROM summary_district_disease") or 0
    cost_per_person = 350  # NHSO reference 2567
    return {
        "cost_reference": {"screening_per_person_thb": cost_per_person, "source": "NHSO 2567"},
        "total_screened": total,
        "total_cost_thb": total * cost_per_person,
        "remaining_to_target": 1_600_000 - total,
        "remaining_cost_thb": (1_600_000 - total) * cost_per_person,
    }


def _budget_allocation() -> dict:
    rows = _query("""
        SELECT d.district_code, d.district_name, d.total_screened,
               rd.population,
               ROUND(350.0 * rd.population * 0.6, 0) AS allocated_budget_thb
        FROM summary_district_disease d
        JOIN ref_districts rd ON rd.dcode = d.district_code
        ORDER BY allocated_budget_thb DESC
    """)
    return {"model": "population_weighted_60pct_target", "cost_per_person_thb": 350, "districts": rows}


def _screening_tests() -> dict:
    rows = _query("SELECT * FROM summary_screening_tests ORDER BY district_code")
    city = _query("""
        SELECT SUM(total_screened) AS total,
               SUM(ekg_done) AS ekg_done, SUM(ekg_normal) AS ekg_normal, SUM(ekg_abnormal) AS ekg_abnormal,
               SUM(xray_done) AS xray_done, SUM(xray_normal) AS xray_normal, SUM(xray_abnormal) AS xray_abnormal,
               SUM(vision_done) AS vision_done, SUM(vision_normal) AS vision_normal,
               SUM(dr_done) AS dr_done, SUM(dr_normal) AS dr_normal
        FROM summary_screening_tests
    """)
    return {"city_total": city[0] if city else {}, "by_district": rows}


def _chronic_history() -> dict:
    rows = _query("SELECT * FROM summary_chronic_history ORDER BY district_code")
    city = _query("""
        SELECT SUM(total_respondents) AS total,
               SUM(history_dm) AS history_dm, SUM(history_hpt) AS history_hpt,
               SUM(history_stroke) AS history_stroke, SUM(history_heart) AS history_heart,
               SUM(dm_on_treatment) AS dm_on_treatment, SUM(hpt_on_treatment) AS hpt_on_treatment,
               SUM(vaccinated_covid) AS vaccinated_covid, SUM(vaccinated_influenza) AS vaccinated_influenza,
               SUM(exercise_regular) AS exercise_regular, SUM(exercise_sometimes) AS exercise_sometimes,
               SUM(exercise_never) AS exercise_never
        FROM summary_chronic_history
    """)
    return {"city_total": city[0] if city else {}, "by_district": rows}


def _family_history() -> dict:
    rows = _query("SELECT * FROM summary_family_history ORDER BY district_code")
    city = _query("""
        SELECT SUM(total_respondents) AS total,
               SUM(family_dm_count) AS family_dm,
               SUM(parent_dm) AS parent_dm, SUM(parent_hpt) AS parent_hpt,
               SUM(parent_stroke) AS parent_stroke, SUM(parent_heart) AS parent_heart,
               SUM(parent_kidney) AS parent_kidney
        FROM summary_family_history
    """)
    return {"city_total": city[0] if city else {}, "by_district": rows}


def _comorbidity_matrix() -> dict:
    rows = _query("""
        SELECT district_code, total_screened,
               dm_only, hpt_only, obesity_only,
               dm_and_hpt, dm_and_obesity, dm_and_dyslipidemia,
               hpt_and_obesity, hpt_and_dyslipidemia,
               cvd_and_stroke, dm_and_cvd,
               metabolic_syndrome, dm_hpt_obesity,
               multi_disease_count, no_disease
        FROM summary_comorbidity
        ORDER BY district_code
    """)
    city = _query("""
        SELECT SUM(total_screened) AS total,
               SUM(dm_and_hpt) AS dm_and_hpt,
               SUM(dm_and_obesity) AS dm_and_obesity,
               SUM(hpt_and_obesity) AS hpt_and_obesity,
               SUM(metabolic_syndrome) AS metabolic_syndrome,
               SUM(multi_disease_count) AS multi_disease,
               SUM(no_disease) AS no_disease
        FROM summary_comorbidity
    """)
    return {"city_total": city[0] if city else {}, "by_district": rows}


def _repeat_screening() -> dict:
    rows = _query("""
        SELECT visit_count, COUNT(*) AS patient_count
        FROM (
            SELECT patient_id, COUNT(*) AS visit_count
            FROM raw_vitalsigns
            WHERE cancel_status IS DISTINCT FROM 1
            GROUP BY patient_id
        ) sub
        GROUP BY visit_count ORDER BY visit_count
    """)
    return {"visit_distribution": rows}


def _screening_locations() -> dict:
    rows = _query("""
        SELECT code, name_th, name_en, facility_type, zone_code, district_code,
               latitude, longitude
        FROM ref_facilities
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
        LIMIT 500
    """)
    return {"facilities": rows, "total": len(rows)}


def _zone_comparison() -> dict:
    rows = _query("""
        SELECT d.zone_code,
               SUM(d.total_screened) AS total_screened,
               ROUND(100.0 * SUM(d.risk_dm_count) / NULLIF(SUM(d.total_screened), 0), 2) AS pct_dm,
               ROUND(100.0 * SUM(d.risk_hpt_count) / NULLIF(SUM(d.total_screened), 0), 2) AS pct_hpt,
               ROUND(100.0 * SUM(d.risk_cvd_count) / NULLIF(SUM(d.total_screened), 0), 2) AS pct_cvd,
               ROUND(100.0 * SUM(d.found_obesity_count) / NULLIF(SUM(d.total_screened), 0), 2) AS pct_obesity,
               ROUND(100.0 * SUM(d.found_dyslipidemia_count) / NULLIF(SUM(d.total_screened), 0), 2) AS pct_dyslipidemia
        FROM summary_district_disease d
        GROUP BY d.zone_code ORDER BY d.zone_code
    """)
    return {"zones": rows}


def _executive_alert() -> dict:
    # Identify districts with unusually high disease rates
    rows = _query("""
        SELECT district_code, district_name, zone_code, total_screened,
               pct_risk_dm, pct_risk_hpt, pct_risk_cvd
        FROM summary_district_disease
        WHERE total_screened >= 5
        AND (pct_risk_dm > 15 OR pct_risk_hpt > 20 OR pct_risk_cvd > 10)
        ORDER BY pct_risk_dm DESC
    """)
    return {"alerts": rows, "total_alerts": len(rows)}


def _disease_lab_crosstab() -> dict:
    """FBS/SBP/cholesterol averages stratified by disease status."""
    rows = _query("""
        SELECT district_code,
               ROUND(avg_fbs_dm_positive::numeric, 1) AS avg_fbs_dm_positive,
               ROUND(avg_fbs_dm_negative::numeric, 1) AS avg_fbs_dm_negative,
               n_fbs_dm_positive, n_fbs_dm_negative,
               ROUND(avg_sbp_hpt_positive::numeric, 1) AS avg_sbp_hpt_positive,
               ROUND(avg_sbp_hpt_negative::numeric, 1) AS avg_sbp_hpt_negative,
               ROUND(avg_chol_dyslip_positive::numeric, 1) AS avg_chol_dyslip_positive,
               ROUND(avg_chol_dyslip_negative::numeric, 1) AS avg_chol_dyslip_negative
        FROM summary_lab_disease_cross
        WHERE district_code = 'city_total' OR district_code = '__city__'
        LIMIT 1
    """)
    if not rows:
        # Fallback: compute city-wide
        rows = _query("""
            SELECT 'city' AS district_code,
                   ROUND(AVG(avg_fbs_dm_positive)::numeric, 1) AS avg_fbs_dm_positive,
                   ROUND(AVG(avg_fbs_dm_negative)::numeric, 1) AS avg_fbs_dm_negative,
                   SUM(n_fbs_dm_positive) AS n_fbs_dm_positive,
                   SUM(n_fbs_dm_negative) AS n_fbs_dm_negative,
                   ROUND(AVG(avg_sbp_hpt_positive)::numeric, 1) AS avg_sbp_hpt_positive,
                   ROUND(AVG(avg_sbp_hpt_negative)::numeric, 1) AS avg_sbp_hpt_negative,
                   ROUND(AVG(avg_chol_dyslip_positive)::numeric, 1) AS avg_chol_dyslip_positive,
                   ROUND(AVG(avg_chol_dyslip_negative)::numeric, 1) AS avg_chol_dyslip_negative
            FROM summary_lab_disease_cross
            WHERE district_code ~ '^[0-9]'
        """)
    return {
        "cross_tab": rows[0] if rows else {},
        "note": "ค่าเฉลี่ย FBS/SBP/cholesterol แยกตามสถานะโรค (positive=พบโรค, negative=ไม่พบ)",
    }


# Dispatch table
_DISPATCH = {
    "overview": _overview,
    "headline_kpi": _headline_kpi,
    "yoy_comparison": _yoy_comparison,
    "moph_targets": _moph_targets,
    "ncd_cascade": _ncd_cascade,
    "screening_coverage": _screening_coverage,
    "repeat_screening": _repeat_screening,
    "lab_summary": _lab_summary,
    "lab_city_average": _lab_city_average,
    "disease_lab_crosstab": _disease_lab_crosstab,
    "bmi_distribution": _bmi_distribution,
    "cost_per_screening": _cost_per_screening,
    "budget_allocation": _budget_allocation,
    "screening_tests": _screening_tests,
    "chronic_history": _chronic_history,
    "family_history": _family_history,
    "comorbidity_matrix": _comorbidity_matrix,
    "screening_locations": _screening_locations,
    "zone_comparison": _zone_comparison,
    "executive_alert": _executive_alert,
    "screening_yield": _screening_coverage,  # alias
    "facility_performance": lambda: {"facilities": _query("SELECT * FROM summary_facility ORDER BY total_screened DESC LIMIT 20")},
    "facility_yield_rank": lambda: {"facilities": _query("SELECT * FROM summary_facility ORDER BY total_screened DESC LIMIT 20")},
    "exercise_frequency": _chronic_history,  # exercise data is in chronic_history
    "district_summary": lambda: {"districts": _query("SELECT district_code, district_name, zone_code, total_screened FROM summary_district_disease ORDER BY district_code")},
}


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------

class QueryAPITool(BaseTool):
    name = "query_api"
    description = (
        "Query specialized health data endpoints. Use this for: KPIs, NCD cascade, "
        "lab results, BMI/waist distribution, cost/budget analysis, screening test rates, "
        "chronic disease history, family history, comorbidity counts, facility performance, "
        "year-over-year comparison, MOPH targets, and screening locations. "
        "IMPORTANT: Use this instead of query_health_data when the user asks about "
        "lab values, cost, budget, KPI targets, NCD cascade, screening tests (EKG/X-ray), "
        "treatment adherence, vaccination, or comorbidity COUNTS."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "endpoint": {
                "type": "string",
                "enum": list(ENDPOINT_CATALOG.keys()),
                "description": "Which data endpoint to query. Options:\n" + "\n".join(
                    f"- {k}: {v}" for k, v in ENDPOINT_CATALOG.items()
                ),
            },
            "zone_code": {
                "type": "string",
                "description": "Optional: filter by zone (1-8)",
            },
            "district_code": {
                "type": "string",
                "description": "Optional: filter by district (4-digit code)",
            },
        },
        "required": ["endpoint"],
    }

    def execute(self, args: dict) -> ToolResult:
        endpoint = args.get("endpoint", "overview")
        zone_code = args.get("zone_code")
        district_code = args.get("district_code")

        fn = _DISPATCH.get(endpoint)
        if not fn:
            return ToolResult(
                text=f"ไม่รู้จัก endpoint '{endpoint}' — ใช้ได้: {', '.join(ENDPOINT_CATALOG.keys())}",
            )

        try:
            result = fn()
        except Exception as e:
            logger.exception("query_api(%s) failed: %s", endpoint, e)
            return ToolResult(text=f"เกิดข้อผิดพลาดในการดึงข้อมูล {endpoint}: {e}")

        # Format result as readable text
        import json
        text = json.dumps(result, ensure_ascii=False, default=str, indent=2)

        # Truncate if too long (LLM context limit)
        if len(text) > 4000:
            text = text[:4000] + "\n... (truncated)"

        return ToolResult(
            text=f"=== {ENDPOINT_CATALOG.get(endpoint, endpoint)} ===\n{text}",
            metadata={"endpoint": endpoint, "raw": result},
        )
