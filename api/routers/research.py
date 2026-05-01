"""Research router -- extracted from main.py.
Refactored for bma_med.* schema."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from database import execute_query, execute_scalar
from security import K_ANONYMITY_THRESHOLD

router = APIRouter(prefix="/api/v2/research", tags=["Research"])

# --------------------------------------------------------------------------- #
# Reusable UNION across the two main vitalsigns sources (app1 + portal).
# Subselect aliased as `v` so handlers can plug it in as `FROM ({_VISITS_UNION_SQL}) v`.
# --------------------------------------------------------------------------- #
_VISITS_UNION_SQL = """
SELECT row_id AS id, patient_id, vstdate AS visit_date,
       hbpn AS sbp, lbpn AS dbp,
       record_cancelled AS cancel_status
FROM bma_med.app1_vitalsignslf
UNION ALL
SELECT row_id AS id, patient_id, vstdate AS visit_date,
       hbpn AS sbp, lbpn AS dbp,
       record_cancelled AS cancel_status
FROM bma_med.portal_vitalsignslf
"""


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
    # TODO: bma_med equivalent unclear — facility_code (hptcode) and district_code
    # don't live on vitalsignslf in new schema. Set to NULL counts.
    stats = execute_query(f"""
        WITH v AS ({_VISITS_UNION_SQL})
        SELECT
            COUNT(DISTINCT p.patient_id) as total_patients,
            COUNT(DISTINCT v.id) as total_visits,
            NULL::int as facilities,
            NULL::int as districts,
            MIN(v.visit_date) as date_range_start,
            MAX(v.visit_date) as date_range_end
        FROM bma_med.patient p
        LEFT JOIN v ON p.patient_id = v.patient_id AND v.cancel_status IS DISTINCT FROM 1
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
    # Return district-level averages for correlation computation.
    # Notes:
    # - summary_district_disease is now grained by (data_source, district_code) — aggregate
    #   per district with SUM/AVG.
    # - summary_district_lab and summary_bmi_waist are stub views (always 0 rows) in the
    #   bma_med.* migration; LEFT JOINs return NULL → COALESCE(...,0) keeps shape stable.
    # - summary_bmi_waist.sex is INT (NULL in the stub); the legacy `b.sex='all'` filter
    #   is incompatible with the new type. Drop the filter — the stub returns no rows
    #   anyway, and once promoted, a real per-district aggregate will replace it.
    # TODO: bma_med equivalent unclear — once summary_bmi_waist / summary_district_lab
    # are promoted from stubs, restore proper sex='all' rollup if applicable.
    rows = execute_query("""
        SELECT d.dcode AS district_code,
               COALESCE(s.screened, 0)              AS screened,
               COALESCE(s.dm_pct, 0)                AS dm_pct,
               COALESCE(s.hpt_pct, 0)               AS hpt_pct,
               COALESCE(s.cvd_pct, 0)               AS cvd_pct,
               COALESCE(l.avg_fbs, 0)               AS avg_fbs,
               COALESCE(l.avg_hemoglobin, 0)        AS avg_hemoglobin,
               COALESCE(l.avg_cholesterol, 0)       AS avg_cholesterol,
               COALESCE(b.avg_bmi, 0)               AS avg_bmi
        FROM ref_districts d
        LEFT JOIN (
            SELECT district_code,
                   SUM(total_screened)                                                  AS screened,
                   AVG(NULLIF(pct_risk_dm, 0))                                          AS dm_pct,
                   AVG(NULLIF(pct_risk_hpt, 0))                                         AS hpt_pct,
                   AVG(NULLIF(pct_risk_cvd, 0))                                         AS cvd_pct
            FROM summary_district_disease
            GROUP BY district_code
        ) s ON d.dcode = s.district_code
        LEFT JOIN summary_district_lab l ON d.dcode = l.district_code
        LEFT JOIN summary_bmi_waist b ON d.dcode = b.district_code
        WHERE COALESCE(s.screened, 0) >= 5
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


# ------------------------------------------------------------------ #
# GET /api/v2/research/ncd-diagnostic-report
# ------------------------------------------------------------------ #

@router.get("/ncd-diagnostic-report")
def ncd_diagnostic_report():
    """NCD diagnostic-axis report (clinical dashboard).

    For each of 11 NCDs, return 4 metrics:
      - at_risk         : screening risk flag (RISKDM/RISKHPT/...)
      - sick_clinical   : confirmed/self-reported (FOUND_*)
      - new_clinical    : found_* AND lab not abnormal
                          → clinically detected (lab didn't catch)
      - new_from_lab    : lab criterion met AND NOT found_*
                          → lab caught what self-report missed

    Lab thresholds per ราชวิทยาลัย/MOPH:
      DM           FBS ≥ 126 mg/dL
      HPT          SBP ≥ 140 OR DBP ≥ 90 mmHg
      Dyslipidemia Cholesterol ≥ 200 mg/dL
      Obesity      BMI ≥ 23 kg/m²
      CKD          eGFR < 60 mL/min/1.73m²
      Liver        SGOT ≥ 120 OR SGPT ≥ 120 U/L
      Anemia       Hemoglobin < 13 (M) / < 12 (F) g/dL

    Reads from `mv_ncd_diagnostic_report` (refreshed by refresh_all_mvs after
    each ETL import; ~5ms response).
    """
    rows = execute_query("""
        SELECT disease_key, disease_name_th, lab_threshold,
               at_risk, sick_clinical, new_clinical, new_from_lab,
               has_lab_threshold
        FROM public.mv_ncd_diagnostic_report
        ORDER BY
          CASE disease_key
            WHEN 'diabetes' THEN 1 WHEN 'hypertension' THEN 2
            WHEN 'dyslipidemia' THEN 3 WHEN 'obesity' THEN 4
            WHEN 'kidney' THEN 5 WHEN 'liver' THEN 6
            WHEN 'anemia' THEN 7 WHEN 'cardiovascular' THEN 8
            WHEN 'stroke' THEN 9 WHEN 'cervical_cancer' THEN 10
            WHEN 'colorectal_cancer' THEN 11 END
    """)

    total_screened = execute_scalar("""
        SELECT COUNT(DISTINCT patient_id)
        FROM public.mv_visit_resolved
        WHERE bucket = 'bkk' AND is_dedup_kept
    """) or 0

    return {
        "total_screened": int(total_screened),
        "methodology": {
            "at_risk": "RISKDM/RISKHPT/etc. screening criteria flagged TRUE",
            "sick_clinical": "FOUND_* flag — clinically confirmed or self-reported during screening",
            "new_clinical": "FOUND_* TRUE AND lab not over threshold (clinical only)",
            "new_from_lab": "Lab over threshold AND NOT FOUND_* (lab caught what self-report missed)",
            "k_anonymity": K_ANONYMITY_THRESHOLD,
        },
        "data": rows,
    }


# ------------------------------------------------------------------ #
# GET /api/v2/research/ncd-diagnostic-by-zone
# ------------------------------------------------------------------ #

@router.get("/ncd-diagnostic-by-zone")
def ncd_diagnostic_by_zone(zone_code: Optional[str] = None):
    """Per-zone version of /ncd-diagnostic-report (≥migration 114).

    Returns 11 disease rows per zone × 4 metrics, suitable for the map
    hover tooltip. Optional `?zone_code=03` filters to one zone.
    """
    if zone_code:
        rows = execute_query("""
            SELECT zone_code, disease_key, disease_name_th, lab_threshold,
                   at_risk, sick_clinical, new_clinical, by_snp_criteria
            FROM public.mv_ncd_diagnostic_zone
            WHERE zone_code = %s
            ORDER BY
              CASE disease_key
                WHEN 'diabetes' THEN 1 WHEN 'hypertension' THEN 2
                WHEN 'dyslipidemia' THEN 3 WHEN 'obesity' THEN 4
                WHEN 'kidney' THEN 5 WHEN 'liver' THEN 6
                WHEN 'anemia' THEN 7 WHEN 'cardiovascular' THEN 8
                WHEN 'stroke' THEN 9 WHEN 'cervical_cancer' THEN 10
                WHEN 'colorectal_cancer' THEN 11 END
        """, (zone_code,))
    else:
        rows = execute_query("""
            SELECT zone_code, disease_key, disease_name_th, lab_threshold,
                   at_risk, sick_clinical, new_clinical, by_snp_criteria
            FROM public.mv_ncd_diagnostic_zone
            ORDER BY zone_code,
              CASE disease_key
                WHEN 'diabetes' THEN 1 WHEN 'hypertension' THEN 2
                WHEN 'dyslipidemia' THEN 3 WHEN 'obesity' THEN 4
                WHEN 'kidney' THEN 5 WHEN 'liver' THEN 6
                WHEN 'anemia' THEN 7 WHEN 'cardiovascular' THEN 8
                WHEN 'stroke' THEN 9 WHEN 'cervical_cancer' THEN 10
                WHEN 'colorectal_cancer' THEN 11 END
        """)

    return {"k_anonymity": K_ANONYMITY_THRESHOLD, "data": rows}
