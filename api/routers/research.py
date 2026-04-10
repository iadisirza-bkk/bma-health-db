"""Research router -- extracted from main.py."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from database import execute_query, execute_scalar
from security import K_ANONYMITY_THRESHOLD

router = APIRouter(prefix="/api/v2/research", tags=["Research"])


# ------------------------------------------------------------------ #
# GET /api/v2/research/data-dictionary
# ------------------------------------------------------------------ #

@router.get("/data-dictionary")
def data_dictionary():
    """Auto-generated data dictionary for all public tables."""
    rows = execute_query("""
        SELECT
            c.table_name,
            c.column_name,
            c.data_type,
            c.is_nullable,
            c.column_default,
            c.ordinal_position,
            pgd.description
        FROM information_schema.columns c
        LEFT JOIN pg_catalog.pg_statio_all_tables st
            ON c.table_schema = st.schemaname AND c.table_name = st.relname
        LEFT JOIN pg_catalog.pg_description pgd
            ON pgd.objoid = st.relid
            AND pgd.objsubid = c.ordinal_position
        WHERE c.table_schema = 'public'
        ORDER BY c.table_name, c.ordinal_position
    """)

    # Group by table
    tables: dict = {}
    for r in rows:
        tn = r["table_name"]
        if tn not in tables:
            tables[tn] = {"table": tn, "columns": []}
        tables[tn]["columns"].append({
            "column": r["column_name"],
            "type": r["data_type"],
            "nullable": r["is_nullable"],
            "default": r.get("column_default"),
            "description": r.get("description"),
        })

    return {"tables": list(tables.values())}


# ------------------------------------------------------------------ #
# GET /api/v2/research/individual-data
# ------------------------------------------------------------------ #

@router.get("/individual-data")
def research_individual_data(
    format: str = Query("json", description="json|summary"),
    irb_approval: Optional[str] = Query(None),
):
    """Anonymized individual-level data for approved research."""
    if not irb_approval:
        return {"data_available": False,
                "message": "ต้องระบุ IRB approval number (irb_approval parameter) เพื่อเข้าถึงข้อมูลระดับบุคคล",
                "requirements": ["IRB approval from BMA Ethics Committee", "Data Use Agreement signed", "PDPA consent documented"]}

    # Return aggregated individual-level stats (NOT actual records)
    # This is a safe proxy: summarize the shape of individual data
    stats = execute_query("""
        SELECT
            COUNT(DISTINCT p.id) as total_patients,
            COUNT(DISTINCT v.id) as total_visits,
            COUNT(DISTINCT v.facility_code) as facilities,
            COUNT(DISTINCT v.district_code) as districts,
            MIN(v.visit_date) as date_range_start,
            MAX(v.visit_date) as date_range_end
        FROM raw_patients p
        LEFT JOIN raw_vitalsigns v ON p.id = v.patient_id AND v.cancel_status IS DISTINCT FROM 1
    """)

    s = stats[0] if stats else {}
    for k in ("date_range_start", "date_range_end"):
        if s.get(k) and hasattr(s[k], "isoformat"):
            s[k] = s[k].isoformat()

    return {
        "irb_approval": irb_approval,
        "data_shape": s,
        "anonymization": {
            "method": "HMAC-SHA256 on IDCARD with secret key",
            "pii_removed": ["ชื่อ-นามสกุล", "ที่อยู่", "เบอร์โทร", "LINE ID", "Email"],
            "age_generalized": "กลุ่มวัยไทย (6 กลุ่ม)",
            "k_anonymity": 5,
        },
        "access_procedure": "ส่ง IRB approval + Data Use Agreement ไปที่ สำนักการแพทย์ กทม. เพื่อขอ API key สำหรับ research tier",
        "note": "Endpoint นี้ส่งเฉพาะ metadata ไม่ส่งข้อมูลรายบุคคล — ต้องขอ research API key แยก",
    }


# ------------------------------------------------------------------ #
# GET /api/v2/research/statistical-test
# ------------------------------------------------------------------ #

@router.get("/statistical-test")
def statistical_test(
    test: str = Query("chi_square", description="chi_square|t_test|proportion"),
    var1: str = Query("disease"),
    var2: str = Query("age_group"),
):
    """Run basic statistical tests on aggregate data."""
    # Chi-square-like comparison using aggregate counts
    if test == "proportion":
        # Compare disease proportion between age groups
        rows = execute_query("""
            SELECT age_group, SUM(total_screened) as total,
                   SUM(risk_dm) as dm, SUM(risk_hpt) as hpt, SUM(found_obesity) as obesity
            FROM summary_disease_age_sex
            WHERE age_group != '__none__'
            GROUP BY age_group ORDER BY age_group
        """)
        rows = [r for r in rows if (r.get("total") or 0) >= K_ANONYMITY_THRESHOLD]
        if len(rows) >= 2:
            for r in rows:
                t = r.get("total") or 1
                r["pct_dm"] = round(100.0 * (r.get("dm") or 0) / t, 1)
                r["pct_hpt"] = round(100.0 * (r.get("hpt") or 0) / t, 1)
                r["pct_obesity"] = round(100.0 * (r.get("obesity") or 0) / t, 1)
            return {"test": "proportion_comparison", "groups": rows,
                    "note": "For formal hypothesis testing (chi-square, Fisher exact), use the research export with R/Python"}
        return {"data_available": False, "message": "กลุ่มข้อมูลไม่เพียงพอสำหรับการทดสอบทางสถิติ"}

    return {"test": test, "message": f"Statistical test '{test}' available via research export. Use R/Python/SPSS for formal analysis.",
            "available_tests_in_api": ["proportion"]}


# ------------------------------------------------------------------ #
# GET /api/v2/research/correlation-matrix
# ------------------------------------------------------------------ #

@router.get("/correlation-matrix")
def correlation_matrix():
    """Correlation matrix of key health variables (aggregate level)."""
    # Return district-level averages for correlation computation
    rows = execute_query("""
        SELECT d.district_code,
               COALESCE(s.total_screened, 0) as screened,
               COALESCE(s.pct_risk_dm, 0) as dm_pct,
               COALESCE(s.pct_risk_hpt, 0) as hpt_pct,
               COALESCE(s.pct_risk_cvd, 0) as cvd_pct,
               COALESCE(l.avg_fbs, 0) as avg_fbs,
               COALESCE(l.avg_hemoglobin, 0) as avg_hemoglobin,
               COALESCE(l.avg_cholesterol, 0) as avg_cholesterol,
               COALESCE(b.avg_bmi, 0) as avg_bmi
        FROM ref_districts d
        LEFT JOIN summary_district_disease s ON d.dcode = s.district_code
        LEFT JOIN summary_district_lab l ON d.dcode = l.district_code
        LEFT JOIN summary_bmi_waist b ON d.dcode = b.district_code AND b.sex = -1
        WHERE COALESCE(s.total_screened, 0) >= 5
        ORDER BY d.dcode
    """)
    return {"variables": ["screened", "dm_pct", "hpt_pct", "cvd_pct", "avg_fbs", "avg_hemoglobin", "avg_cholesterol", "avg_bmi"],
            "data": rows,
            "note": "Data is at district level (n=50). Compute Pearson/Spearman correlation in R/Python from this matrix."}


# ------------------------------------------------------------------ #
# GET /api/v2/research/sample-size-calculator
# ------------------------------------------------------------------ #

@router.get("/sample-size-calculator")
def sample_size_calculator(
    prevalence: float = Query(0.15, description="Expected prevalence (0-1)"),
    precision: float = Query(0.05, description="Desired margin of error"),
    confidence: float = Query(0.95, description="Confidence level"),
    population: Optional[int] = Query(None, description="Population size (for finite correction)"),
):
    """Sample size calculator for health surveys."""
    import math
    # Z-scores for common confidence levels
    z_map = {0.90: 1.645, 0.95: 1.96, 0.99: 2.576}
    z = z_map.get(confidence, 1.96)

    p = prevalence
    e = precision
    # Infinite population formula
    n_inf = math.ceil((z**2 * p * (1-p)) / (e**2))

    # Finite population correction
    n_final = n_inf
    if population and population > 0:
        n_final = math.ceil(n_inf / (1 + (n_inf - 1) / population))

    pop_bkk = execute_scalar("SELECT SUM(population) FROM ref_districts") or 6063003
    n_bkk = math.ceil(n_inf / (1 + (n_inf - 1) / pop_bkk))

    return {
        "parameters": {"prevalence": p, "precision": e, "confidence": confidence, "z_score": z},
        "sample_size_infinite": n_inf,
        "sample_size_finite": n_final if population else None,
        "sample_size_bangkok": n_bkk,
        "bangkok_population": pop_bkk,
        "formula": "n = Z² × p × (1-p) / e² with finite population correction",
    }


# ------------------------------------------------------------------ #
# GET /api/v2/research/export
# ------------------------------------------------------------------ #

@router.get("/export")
def research_export(
    format: str = Query("json", description="json|csv_summary"),
    irb_approval: Optional[str] = Query(None),
):
    """Export aggregate data for research purposes."""
    if format == "csv_summary":
        return {"data_available": False,
                "message": "CSV export ต้องระบุ irb_approval และใช้ research API key",
                "available_without_irb": ["json (aggregate district-level data)"]}

    # Return aggregate data that's safe to share
    districts = execute_query("""
        SELECT s.district_code, s.district_name, s.zone_code, s.total_screened,
               s.risk_dm_count, s.risk_hpt_count, s.risk_cvd_count, s.risk_bmi_count,
               s.found_dm_count, s.found_hpt_count, s.found_obesity_count,
               s.found_dyslipidemia_count, s.found_stroke_count
        FROM summary_district_disease s
        WHERE s.total_screened >= 5
        ORDER BY s.district_code
    """)

    return {"format": "json", "type": "aggregate_district_level",
            "k_anonymity": 5, "records": len(districts), "data": districts}
