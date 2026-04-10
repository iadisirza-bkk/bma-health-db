"""
BMA Health Summary MCP Server
==============================
MCP server providing aggregate health data for Bangkok Metropolitan Administration.
This is the ONLY gateway for LLM agents to access BMA health data.

Uses the shared HealthDataService for all business logic — same service layer
as the REST API. No duplicated SQL.

เซิร์ฟเวอร์ MCP สำหรับข้อมูลสรุปสุขภาพ กทม.
เข้าถึงได้เฉพาะข้อมูล aggregate จาก materialized views เท่านั้น
ไม่มีข้อมูลรายบุคคล ไม่มี SQL passthrough
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool
from contextlib import contextmanager
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Add parent directory to path so we can import the shared service
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
from services.health_data_service import HealthDataService

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://bma_readonly:readonly@localhost:5432/bma_health",
)

AUDIT_LOG_PATH = os.getenv(
    "MCP_AUDIT_LOG_PATH",
    "/var/log/bma-health/mcp-audit.jsonl",
)

K_ANONYMITY_THRESHOLD = int(os.getenv("K_ANONYMITY_THRESHOLD", "5"))

# ---------------------------------------------------------------------------
# Security: blocked raw tables and PII columns
# ---------------------------------------------------------------------------

BLOCKED_TABLES = frozenset([
    "raw_patients", "raw_visits", "raw_vitalsigns",
    "raw_homevisit", "raw_homehealth", "raw_lab_results", "raw_lab_extended",
])

BLOCKED_COLUMNS = frozenset([
    "idcard_hash", "patient_id", "staff_code", "firststf", "laststf", "cancelstf",
])

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("bma-health-mcp")
logger.setLevel(logging.INFO)
_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_stderr_handler)

# ---------------------------------------------------------------------------
# Audit log with SHA-256 chain
# ---------------------------------------------------------------------------

_last_audit_hash: str = "0" * 64


def _write_audit_entry(tool: str, params: dict, result_row_count: int, client: str = "unknown") -> None:
    """Append a tamper-evident audit entry (SHA-256 chained)."""
    global _last_audit_hash

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "params": params,
        "result_row_count": result_row_count,
        "client": client,
        "prev_hash": _last_audit_hash,
    }
    entry_bytes = json.dumps(entry, sort_keys=True, ensure_ascii=False).encode()
    entry["hash"] = hashlib.sha256(entry_bytes).hexdigest()
    _last_audit_hash = entry["hash"]

    line = json.dumps(entry, ensure_ascii=False)

    try:
        log_dir = os.path.dirname(AUDIT_LOG_PATH)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        logger.warning("Audit log file not writable, logging to stderr: %s", line)


# ---------------------------------------------------------------------------
# Database helpers (MCP's own pool with security validation)
# ---------------------------------------------------------------------------

_pool = None


def _get_pool():
    global _pool
    if _pool is None or _pool.closed:
        _pool = ThreadedConnectionPool(minconn=2, maxconn=10, dsn=DATABASE_URL)
    return _pool


@contextmanager
def _get_connection():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


class SecurityError(Exception):
    pass


def _validate_query_safety(sql: str):
    """Block queries that reference raw tables or PII columns."""
    sql_lower = sql.lower()
    for table in BLOCKED_TABLES:
        if table in sql_lower:
            raise SecurityError(f"Access denied: table '{table}' is restricted")
    for col in BLOCKED_COLUMNS:
        if col in sql_lower:
            raise SecurityError(f"Access denied: column '{col}' is restricted")
    stripped = sql_lower.strip()
    if not stripped.startswith("select"):
        raise SecurityError("Only SELECT queries are allowed")


def _query(sql: str, params=None) -> List[Dict]:
    """Execute a read-only query with security validation."""
    _validate_query_safety(sql)
    with _get_connection() as conn:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _scalar(sql: str, params=None) -> Any:
    """Execute a query and return a single scalar value."""
    rows = _query(sql, params)
    if rows:
        return list(rows[0].values())[0]
    return None


def _query_trend(sql: str, params=None):
    """Special query path for trend aggregates that need raw tables but block PII."""
    sql_lower = sql.lower()
    for col in BLOCKED_COLUMNS:
        if col in sql_lower:
            raise SecurityError(f"Access denied: column '{col}' is restricted")
    if not sql_lower.strip().startswith("select"):
        raise SecurityError("Only SELECT queries are allowed")
    if "group by" not in sql_lower:
        raise SecurityError("Trend queries must use GROUP BY aggregation")
    with _get_connection() as conn:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Shared Service Instance
# ---------------------------------------------------------------------------

_service = HealthDataService(
    query=_query,
    scalar=_scalar,
    query_trend=_query_trend,
)

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "bma-health-summary",
    instructions=(
        "BMA Health Summary Data Server. "
        "ข้อมูลสรุปสุขภาพ กทม. เข้าถึงได้เฉพาะข้อมูล aggregate เท่านั้น ไม่มีข้อมูลรายบุคคล"
    ),
)


@mcp.tool()
def get_overview() -> dict:
    """
    Get an overview of the BMA health screening programme.
    ดึงภาพรวมโครงการตรวจคัดกรองสุขภาพ กทม. ทั้งหมด
    Returns total screened, target, number of zones and districts, and last updated time.
    """
    result = _service.get_overview()
    _write_audit_entry("get_overview", {}, 1)
    return result


@mcp.tool()
def get_zone_summary(zone_code: str) -> dict:
    """
    Get a summary for a specific BMA health zone.
    ดึงข้อมูลสรุปสุขภาพตามโซนสุขภาพ กทม. (zone_code เช่น '1', '2', ...)
    Returns zone totals, list of districts, and disease prevalence percentages.
    """
    result = _service.get_zone_summary(zone_code)
    _write_audit_entry("get_zone_summary", {"zone_code": zone_code}, len(result.get("districts", [])))
    return result


@mcp.tool()
def get_district_summary(dcode: str) -> dict:
    """
    Get a comprehensive summary for a single district.
    ดึงข้อมูลสรุปสุขภาพรายเขต (district) รวมโรค, ผลแลป, สุขภาพจิต
    dcode = district code เช่น '1001', '1002', ...
    """
    result = _service.get_district_summary(dcode)
    _write_audit_entry("get_district_summary", {"dcode": dcode}, 0 if "error" in result else 1)
    return result


@mcp.tool()
def compare_disease(
    disease_key: str,
    level: str = "zone",
    codes: Optional[List[str]] = None,
) -> Union[List[Dict], Dict]:
    """
    Compare disease prevalence across zones or districts.
    เปรียบเทียบอัตราความชุกของโรคระหว่างโซนหรือเขต
    disease_key: diabetes, hypertension, cardiovascular, obesity, dyslipidemia, stroke
    level: 'zone' or 'district'
    codes: optional list of zone_codes or dcodes to filter
    """
    try:
        result = _service.compare_disease(disease_key, level, codes)
    except ValueError as e:
        result = {"error": str(e)}
    row_count = len(result) if isinstance(result, list) else 0
    _write_audit_entry("compare_disease", {"disease_key": disease_key, "level": level, "codes": codes}, row_count)
    return result


@mcp.tool()
def get_filtered_summary(filters: Dict) -> Union[List[Dict], Dict]:
    """
    Get risk factor summaries with optional demographic filters, enforcing k-anonymity.
    ดึงข้อมูลปัจจัยเสี่ยงสุขภาพ กรองตามเขต/เพศ/กลุ่มอายุ/การสูบบุหรี่/การออกกำลังกาย
    filters: { dcode, sex, age_group, smoking, exercise }
    Groups with fewer than 5 people are suppressed for privacy.
    """
    result = _service.get_filtered_summary(filters)
    row_count = result.get("total_rows", 0) if isinstance(result, dict) else 0
    _write_audit_entry("get_filtered_summary", filters or {}, row_count)
    return result


@mcp.tool()
def get_trend(
    disease_key: str,
    dcode: Optional[str] = None,
    granularity: str = "monthly",
) -> Union[List[Dict], Dict]:
    """
    Get disease trend over time (aggregated, never individual records).
    ดึงแนวโน้มโรคตามช่วงเวลา (รายเดือน/รายไตรมาส) แบบ aggregate เท่านั้น
    disease_key: diabetes, hypertension, cardiovascular, obesity
    dcode: optional district code filter
    granularity: 'monthly' or 'quarterly'
    """
    try:
        result = _service.get_trend(disease_key, dcode, granularity)
    except ValueError as e:
        result = {"error": str(e)}
    row_count = len(result) if isinstance(result, list) else 0
    _write_audit_entry("get_trend", {"disease_key": disease_key, "dcode": dcode, "granularity": granularity}, row_count)
    return result


@mcp.tool()
def get_lab_summary(
    dcode: Optional[str] = None,
    zone_code: Optional[str] = None,
) -> Dict:
    """
    Get lab result summary (averages and clinical thresholds) by district or zone.
    ดึงข้อมูลสรุปผลแลป เช่น ค่าเฉลี่ย hemoglobin, FBS, cholesterol, สัดส่วนโลหิตจาง, CKD
    """
    result = _service.get_lab_summary(dcode, zone_code)
    _write_audit_entry("get_lab_summary", {"dcode": dcode, "zone_code": zone_code}, 0 if "error" in result else 1)
    return result


@mcp.tool()
def get_mental_health_summary(
    dcode: Optional[str] = None,
    zone_code: Optional[str] = None,
) -> Dict:
    """
    Get mental health screening summary (depression, PHQ-9, stress).
    ดึงข้อมูลสรุปสุขภาพจิต: ความเสี่ยงซึมเศร้า, PHQ-9 ระดับปานกลางขึ้นไป, ความเครียดสูง
    """
    result = _service.get_mental_health_summary(dcode, zone_code)
    _write_audit_entry("get_mental_health_summary", {"dcode": dcode, "zone_code": zone_code}, 0 if "error" in result else 1)
    return result


@mcp.tool()
def get_demographics(
    dcode: Optional[str] = None,
    zone_code: Optional[str] = None,
) -> Dict:
    """
    Get demographic breakdown: education, occupation, health privilege, housing type.
    ดึงข้อมูลประชากรศาสตร์: การศึกษา, อาชีพ, สิทธิ์สุขภาพ, ประเภทที่อยู่อาศัย
    """
    result = _service.get_demographics(dcode, zone_code)
    _write_audit_entry("get_demographics", {"dcode": dcode, "zone_code": zone_code}, 0 if "error" in result else 1)
    return result


@mcp.tool()
def search_districts(query: Dict) -> Union[List[Dict], Dict]:
    """
    Search districts by disease prevalence criteria.
    ค้นหาเขตตามเกณฑ์ความชุกของโรค เช่น เขตที่มีเบาหวานสูงสุด
    query: { disease (str), min_pct (float), max_pct (float), sort_by (str), limit (int) }
    """
    try:
        result = _service.search_districts(query)
    except ValueError as e:
        result = {"error": str(e)}
    row_count = len(result) if isinstance(result, list) else 0
    _write_audit_entry("search_districts", query or {}, row_count)
    return result


# ===== Tool 11: get_facility_locations (NEW — GIS) =====

@mcp.tool()
def get_facility_locations(
    zone_code: Optional[str] = None,
    district_code: Optional[str] = None,
    limit: int = 100,
) -> Dict:
    """
    Get health facility locations with lat/lng for map display.
    ดึงตำแหน่งสถานบริการ พร้อมพิกัด สำหรับแสดงบนแผนที่
    zone_code: optional filter by health zone
    district_code: optional filter by district
    limit: max facilities to return (default 100)
    """
    conditions = ["latitude IS NOT NULL"]
    params: list = []

    if zone_code:
        conditions.append("zone_code = %s")
        params.append(str(zone_code).strip())
    if district_code:
        conditions.append("district_code = %s")
        params.append(str(district_code).strip())

    where = " AND ".join(conditions)
    params.append(min(limit, 500))

    rows = _query(f"""
        SELECT code, name_th, facility_type, district_code, zone_code,
               latitude, longitude, address, telephone
        FROM ref_facilities
        WHERE {where}
        ORDER BY code
        LIMIT %s
    """, params)

    _write_audit_entry("get_facility_locations", {"zone_code": zone_code, "district_code": district_code}, len(rows))
    return {"total": len(rows), "facilities": _service._round_floats(rows)}


# ===== Tool 12: get_disease_heatmap (NEW — GIS) =====

@mcp.tool()
def get_disease_heatmap(disease_key: str) -> Union[Dict, List]:
    """
    Get disease prevalence per district with centroid coordinates for heatmap.
    ดึงความชุกโรคต่อเขต พร้อมพิกัดกลางเขต สำหรับแสดง heatmap บนแผนที่
    disease_key: diabetes, hypertension, cardiovascular
    """
    valid_cols = {
        "diabetes": "pct_risk_dm",
        "hypertension": "pct_risk_hpt",
        "cardiovascular": "pct_risk_cvd",
    }
    disease_key = disease_key.strip().lower()
    if disease_key not in valid_cols:
        return {"error": f"Valid disease_keys: {sorted(valid_cols)}"}

    pct_col = valid_cols[disease_key]

    rows = _query(f"""
        SELECT d.district_code, d.district_name, d.total_screened,
               d.{pct_col} AS disease_pct,
               AVG(f.latitude) AS centroid_lat,
               AVG(f.longitude) AS centroid_lng
        FROM summary_district_disease d
        LEFT JOIN ref_facilities f ON f.district_code = d.district_code AND f.latitude IS NOT NULL
        WHERE d.total_screened >= %s
        GROUP BY d.district_code, d.district_name, d.total_screened, d.{pct_col}
        ORDER BY d.{pct_col} DESC NULLS LAST
    """, (K_ANONYMITY_THRESHOLD,))

    _write_audit_entry("get_disease_heatmap", {"disease_key": disease_key}, len(rows))
    return _service._round_floats({"disease_key": disease_key, "data": rows})


# ===== Tool 13: get_diet_disease_summary (NEW — Doc 01 Governor) =====

@mcp.tool()
def get_diet_disease_summary(
    diet: str = "sweet",
    disease: str = "diabetes",
) -> Dict:
    """
    Get diet-disease correlation: does eating sweet/salty/fatty increase disease risk?
    ความสัมพันธ์ระหว่างพฤติกรรมอาหารกับโรค เช่น กินเค็มแล้วเป็นความดันจริงไหม?
    diet: sweet, salty, fatty, fried, sugary_drinks, instant_noodle
    disease: diabetes, hypertension, cardiovascular, obesity, dyslipidemia, stroke
    NOTE: Diet data may be unavailable (100% NULL) depending on HDC data import status.
    """
    diet_cols = {
        "sweet": "food_preference_sweet", "salty": "food_preference_salty",
        "fatty": "food_preference_fatty", "fried": "food_fried_freq",
        "sugary_drinks": "drink_sugar_freq", "instant_noodle": "instant_noodle_freq",
    }
    disease_cols = {
        "diabetes": "found_dm", "hypertension": "found_hpt",
        "cardiovascular": "found_cvd", "obesity": "found_obesity",
        "dyslipidemia": "found_dyslipidemia", "stroke": "found_stroke",
    }

    diet = diet.strip().lower()
    disease = disease.strip().lower()
    if diet not in diet_cols:
        return {"error": f"Valid diets: {sorted(diet_cols)}"}
    if disease not in disease_cols:
        return {"error": f"Valid diseases: {sorted(disease_cols)}"}

    diet_col = diet_cols[diet]
    disease_col = disease_cols[disease]

    # Check data availability (this uses _query_trend to access raw tables)
    try:
        filled = _query_trend(f"""
            SELECT COUNT(*) AS n FROM raw_homehealth
            WHERE {diet_col} IS NOT NULL
            GROUP BY 1=1
        """)
        count = filled[0]["n"] if filled else 0
    except Exception:
        count = 0

    if count == 0:
        result = {
            "data_available": False,
            "diet": diet, "disease": disease,
            "message": f"ไม่มีข้อมูล {diet} (NULL ทั้งหมด) — ต้องรอข้อมูลจาก HDC",
        }
        _write_audit_entry("get_diet_disease_summary", {"diet": diet, "disease": disease}, 0)
        return result

    rows = _query_trend(f"""
        SELECT h.{diet_col} AS diet_value,
               COUNT(DISTINCT v.patient_id) AS total_patients,
               COUNT(DISTINCT v.patient_id) FILTER (WHERE v.{disease_col}) AS disease_count,
               ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.{disease_col})
                   / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS disease_pct
        FROM raw_homehealth h
        JOIN raw_vitalsigns v ON h.patient_id = v.patient_id
        WHERE h.cancel_status IS DISTINCT FROM 1 AND v.cancel_status IS DISTINCT FROM 1
          AND h.{diet_col} IS NOT NULL
        GROUP BY h.{diet_col}
        HAVING COUNT(DISTINCT v.patient_id) >= {K_ANONYMITY_THRESHOLD}
        ORDER BY h.{diet_col}
    """)

    result = {"data_available": True, "diet": diet, "disease": disease, "data": _service._round_floats(rows)}
    _write_audit_entry("get_diet_disease_summary", {"diet": diet, "disease": disease}, len(rows))
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
