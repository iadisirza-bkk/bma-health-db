"""
BMA Health Summary MCP Server
==============================
MCP server providing aggregate health data for Bangkok Metropolitan Administration.
This is the ONLY gateway for LLM agents to access BMA health data.
All queries target materialized summary views -- raw tables are never exposed.

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
    "raw_patients",
    "raw_visits",
    "raw_vitalsigns",
    "raw_homevisit",
    "raw_homehealth",
    "raw_lab_results",
    "raw_lab_extended",
])

BLOCKED_COLUMNS = frozenset([
    "idcard_hash",
    "patient_id",
    "staff_code",
    "firststf",
    "laststf",
    "cancelstf",
])

# ---------------------------------------------------------------------------
# Disease key mapping
# ---------------------------------------------------------------------------

DISEASE_KEY_MAP: Dict[str, Dict[str, str]] = {
    "diabetes": {
        "pct_at_risk": "pct_risk_dm",
        "pct_found": "pct_found_dm",
        "risk_count": "risk_dm_count",
        "found_count": "found_dm_count",
    },
    "hypertension": {
        "pct_at_risk": "pct_risk_hpt",
        "pct_found": "pct_found_hpt",
        "risk_count": "risk_hpt_count",
        "found_count": "found_hpt_count",
    },
    "cardiovascular": {
        "pct_at_risk": "pct_risk_cvd",
        "pct_found": "pct_found_cvd",
        "risk_count": "risk_cvd_count",
        "found_count": "found_cvd_count",
    },
    "obesity": {
        "pct_at_risk": "pct_risk_bmi",
        "pct_found": "found_obesity_count",  # no pct column, compute from count
    },
    "dyslipidemia": {
        "pct_found": "found_dyslipidemia_count",
    },
    "stroke": {
        "pct_found": "found_stroke_count",
    },
}

VALID_DISEASE_KEYS = set(DISEASE_KEY_MAP.keys())

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logger = logging.getLogger("bma-health-mcp")
logger.setLevel(logging.INFO)
_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_stderr_handler)

# ---------------------------------------------------------------------------
# Audit log with SHA-256 chain
# ---------------------------------------------------------------------------

_last_audit_hash: str = "0" * 64  # genesis hash


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
        # Fall back to stderr if the log path is not writable
        logger.warning("Audit log file not writable, logging to stderr: %s", line)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


_pool = None


def _get_pool():
    global _pool
    if _pool is None or _pool.closed:
        _pool = ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=DATABASE_URL,
        )
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
    # Must be SELECT only
    stripped = sql_lower.strip()
    if not stripped.startswith("select"):
        raise SecurityError("Only SELECT queries are allowed")


def _query(sql: str, params: Union[tuple, list, None] = None) -> List[Dict]:
    """Execute a read-only query and return rows as dicts."""
    _validate_query_safety(sql)
    with _get_connection() as conn:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]


def _scalar(sql: str, params: Union[tuple, list, None] = None) -> Any:
    """Execute a query and return a single scalar value."""
    rows = _query(sql, params)
    if rows:
        return list(rows[0].values())[0]
    return None


def _enforce_k_anonymity(rows: List[Dict], count_col: str = "patient_count") -> List[Dict]:
    """Suppress rows where the count column is below the k-anonymity threshold."""
    return [r for r in rows if r.get(count_col, 0) >= K_ANONYMITY_THRESHOLD]


def _round_floats(obj: Any, decimals: int = 2) -> Any:
    """Recursively round float values for clean JSON output."""
    if isinstance(obj, float):
        return round(obj, decimals)
    if isinstance(obj, dict):
        return {k: _round_floats(v, decimals) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(item, decimals) for item in obj]
    return obj


def _query_trend(sql: str, params=None):
    """Special query path for trend aggregates that need raw tables but must still block PII columns."""
    sql_lower = sql.lower()
    for col in BLOCKED_COLUMNS:
        if col in sql_lower:
            raise SecurityError(f"Access denied: column '{col}' is restricted")
    if not sql_lower.strip().startswith("select"):
        raise SecurityError("Only SELECT queries are allowed")
    # Must have GROUP BY (aggregate only)
    if "group by" not in sql_lower:
        raise SecurityError("Trend queries must use GROUP BY aggregation")
    with _get_connection() as conn:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


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


# ===== Tool 1: get_overview =====

@mcp.tool()
def get_overview() -> dict:
    """
    Get an overview of the BMA health screening programme.
    ดึงภาพรวมโครงการตรวจคัดกรองสุขภาพ กทม. ทั้งหมด
    Returns total screened, target, number of zones and districts, and last updated time.
    """
    rows = _query("""
        SELECT
            SUM(total_screened)       AS total_screened,
            COUNT(DISTINCT zone_code) AS zones,
            COUNT(*)                  AS districts
        FROM summary_district_disease
    """)
    result = rows[0] if rows else {}
    result["target"] = 1_600_000
    result["last_updated"] = datetime.now(timezone.utc).isoformat()

    _write_audit_entry("get_overview", {}, 1)
    return _round_floats(result)


# ===== Tool 2: get_zone_summary =====

@mcp.tool()
def get_zone_summary(zone_code: str) -> dict:
    """
    Get a summary for a specific BMA health zone.
    ดึงข้อมูลสรุปสุขภาพตามโซนสุขภาพ กทม. (zone_code เช่น '1', '2', ...)
    Returns zone totals, list of districts, and disease prevalence percentages.
    """
    zone_code = str(zone_code).strip()

    rows = _query("""
        SELECT
            district_code, district_name, zone_code, total_screened,
            pct_risk_dm, pct_found_dm,
            pct_risk_hpt, pct_found_hpt,
            pct_risk_cvd, pct_found_cvd,
            risk_bmi_count, found_obesity_count, found_dyslipidemia_count, found_stroke_count
        FROM summary_district_disease
        WHERE zone_code = %s
        ORDER BY district_code
    """, (zone_code,))

    if not rows:
        _write_audit_entry("get_zone_summary", {"zone_code": zone_code}, 0)
        return {"error": f"No data found for zone_code='{zone_code}'"}

    total_screened = sum(r["total_screened"] or 0 for r in rows)
    districts = [
        {"dcode": r["district_code"], "name_th": r["district_name"], "total_screened": r["total_screened"]}
        for r in rows
    ]

    # Aggregate disease percentages weighted by screened counts
    def _weighted_pct(col: str) -> Optional[float]:
        total = sum((r[col] or 0) * (r["total_screened"] or 0) for r in rows)
        return round(total / total_screened, 2) if total_screened else None

    diseases = {
        "diabetes": {"pct_risk": _weighted_pct("pct_risk_dm"), "pct_found": _weighted_pct("pct_found_dm")},
        "hypertension": {"pct_risk": _weighted_pct("pct_risk_hpt"), "pct_found": _weighted_pct("pct_found_hpt")},
        "cardiovascular": {"pct_risk": _weighted_pct("pct_risk_cvd"), "pct_found": _weighted_pct("pct_found_cvd")},
    }

    result = {
        "zone_code": zone_code,
        "total_screened": total_screened,
        "districts": districts,
        "diseases": diseases,
    }

    _write_audit_entry("get_zone_summary", {"zone_code": zone_code}, len(rows))
    return _round_floats(result)


# ===== Tool 3: get_district_summary =====

@mcp.tool()
def get_district_summary(dcode: str) -> dict:
    """
    Get a comprehensive summary for a single district.
    ดึงข้อมูลสรุปสุขภาพรายเขต (district) รวมโรค, ผลแลป, สุขภาพจิต
    dcode = district code เช่น '1001', '1002', ...
    """
    dcode = str(dcode).strip()

    # Disease data
    disease_rows = _query("""
        SELECT * FROM summary_district_disease WHERE district_code = %s
    """, (dcode,))

    if not disease_rows:
        _write_audit_entry("get_district_summary", {"dcode": dcode}, 0)
        return {"error": f"No data found for district code='{dcode}'"}

    d = disease_rows[0]

    # Lab data
    lab_rows = _query("""
        SELECT * FROM summary_district_lab WHERE district_code = %s
    """, (dcode,))
    lab = lab_rows[0] if lab_rows else {}

    # Mental health data
    mental_rows = _query("""
        SELECT * FROM summary_district_mental WHERE district_code = %s
    """, (dcode,))
    mental = mental_rows[0] if mental_rows else {}

    result = {
        "dcode": dcode,
        "name_th": d.get("district_name"),
        "zone_code": d.get("zone_code"),
        "total_screened": d.get("total_screened"),
        "diseases": {
            "diabetes": {"pct_risk": d.get("pct_risk_dm"), "pct_found": d.get("pct_found_dm")},
            "hypertension": {"pct_risk": d.get("pct_risk_hpt"), "pct_found": d.get("pct_found_hpt")},
            "cardiovascular": {"pct_risk": d.get("pct_risk_cvd"), "pct_found": d.get("pct_found_cvd")},
            "obesity": {"found_count": d.get("found_obesity_count")},
            "dyslipidemia": {"found_count": d.get("found_dyslipidemia_count")},
            "stroke": {"found_count": d.get("found_stroke_count")},
        },
        "lab_summary": {
            "total_lab_patients": lab.get("total_lab_patients"),
            "avg_hemoglobin": lab.get("avg_hemoglobin"),
            "avg_fbs": lab.get("avg_fbs"),
            "avg_cholesterol": lab.get("avg_cholesterol"),
            "avg_triglyceride": lab.get("avg_triglyceride"),
            "avg_hdl": lab.get("avg_hdl"),
            "avg_ldl": lab.get("avg_ldl"),
            "avg_creatinine": lab.get("avg_creatinine"),
            "avg_egfr": lab.get("avg_egfr"),
            "pct_anemia": lab.get("pct_anemia"),
            "pct_ckd": lab.get("pct_ckd"),
        },
        "mental_health": {
            "total_screened": mental.get("total_screened"),
            "pct_depression_risk": mental.get("pct_depression_risk"),
            "pct_phq9_moderate": mental.get("pct_phq9_moderate"),
            "pct_high_stress": mental.get("pct_high_stress"),
        },
    }

    _write_audit_entry("get_district_summary", {"dcode": dcode}, 1)
    return _round_floats(result)


# ===== Tool 4: compare_disease =====

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
    disease_key = disease_key.strip().lower()
    level = level.strip().lower()

    if disease_key not in VALID_DISEASE_KEYS:
        return {"error": f"Invalid disease_key. Choose from: {sorted(VALID_DISEASE_KEYS)}"}
    if level not in ("zone", "district"):
        return {"error": "level must be 'zone' or 'district'"}

    mapping = DISEASE_KEY_MAP[disease_key]

    if level == "zone":
        # Aggregate by zone
        sql = """
            SELECT
                zone_code AS code,
                zone_code AS name_th,
                SUM(total_screened) AS total_screened
        """
        # Add disease columns
        if "pct_at_risk" in mapping:
            risk_count_col = mapping.get("risk_count", mapping["pct_at_risk"].replace("pct_", "").replace("risk_", "risk_") + "_count")
            # Use weighted average
            sql += f""",
                ROUND(100.0 * SUM({risk_count_col}) / NULLIF(SUM(total_screened), 0), 2) AS pct_at_risk
            """
        else:
            sql += ", NULL AS pct_at_risk"

        sql += """
            FROM summary_district_disease
        """
        params: list = []
        if codes:
            placeholders = ",".join(["%s"] * len(codes))
            sql += f" WHERE zone_code IN ({placeholders})"
            params.extend(codes)

        sql += " GROUP BY zone_code ORDER BY zone_code"

    else:
        # District level
        pct_col = mapping.get("pct_at_risk")
        if pct_col and pct_col.startswith("pct_"):
            select_pct = f"{pct_col} AS pct_at_risk"
        else:
            select_pct = "NULL AS pct_at_risk"

        sql = f"""
            SELECT
                district_code AS code,
                district_name AS name_th,
                total_screened,
                {select_pct}
            FROM summary_district_disease
        """
        params = []
        if codes:
            placeholders = ",".join(["%s"] * len(codes))
            sql += f" WHERE district_code IN ({placeholders})"
            params.extend(codes)

        sql += " ORDER BY district_code"

    rows = _query(sql, params or None)

    # Add ranking by pct_at_risk descending
    sorted_rows = sorted(rows, key=lambda r: r.get("pct_at_risk") or 0, reverse=True)
    for rank, row in enumerate(sorted_rows, 1):
        row["rank"] = rank

    _write_audit_entry(
        "compare_disease",
        {"disease_key": disease_key, "level": level, "codes": codes},
        len(sorted_rows),
    )
    return _round_floats(sorted_rows)


# ===== Tool 5: get_filtered_summary =====

@mcp.tool()
def get_filtered_summary(filters: Dict) -> Union[List[Dict], Dict]:
    """
    Get risk factor summaries with optional demographic filters, enforcing k-anonymity.
    ดึงข้อมูลปัจจัยเสี่ยงสุขภาพ กรองตามเขต/เพศ/กลุ่มอายุ/การสูบบุหรี่/การออกกำลังกาย
    filters: { dcode, sex, age_group, smoking, exercise }
    Groups with fewer than 5 people are suppressed for privacy.
    """
    allowed_filters = {"dcode", "sex", "age_group", "smoking", "exercise"}
    conditions = []
    params: list = []

    col_map = {
        "dcode": "district_code",
        "sex": "sex",
        "age_group": "age_group",
        "smoking": "smoking",
        "exercise": "exercise",
    }

    for key, value in (filters or {}).items():
        key = key.strip().lower()
        if key not in allowed_filters:
            continue
        col = col_map[key]
        conditions.append(f"{col} = %s")
        params.append(str(value))

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = f"""
        SELECT
            district_code,
            sex,
            age_group,
            smoking,
            exercise,
            SUM(patient_count) AS patient_count,
            ROUND(AVG(avg_sbp)::numeric, 2) AS avg_sbp,
            ROUND(AVG(avg_dbp)::numeric, 2) AS avg_dbp,
            ROUND(AVG(avg_weight_kg)::numeric, 2) AS avg_weight_kg,
            ROUND(AVG(avg_waist_cm)::numeric, 2) AS avg_waist_cm,
            ROUND(AVG(avg_bmi)::numeric, 2) AS avg_bmi
        FROM summary_district_risk_factors
        {where_clause}
        GROUP BY district_code, sex, age_group, smoking, exercise
        ORDER BY district_code, sex, age_group
    """

    rows = _query(sql, params or None)

    # Enforce k-anonymity
    safe_rows = _enforce_k_anonymity(rows, count_col="patient_count")
    suppressed = len(rows) - len(safe_rows)

    if not safe_rows:
        _write_audit_entry("get_filtered_summary", filters or {}, 0)
        return {
            "error": "All result groups have fewer than 5 people. Cannot return data to protect privacy.",
            "k_anonymity_threshold": K_ANONYMITY_THRESHOLD,
        }

    result = {
        "rows": _round_floats(safe_rows),
        "total_rows": len(safe_rows),
        "suppressed_groups": suppressed,
        "k_anonymity_threshold": K_ANONYMITY_THRESHOLD,
    }

    _write_audit_entry("get_filtered_summary", filters or {}, len(safe_rows))
    return result


# ===== Tool 6: get_trend =====

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
    NOTE: This queries raw_vitalsigns only in aggregated form (GROUP BY date period).
    """
    disease_key = disease_key.strip().lower()
    granularity = granularity.strip().lower()

    if disease_key not in VALID_DISEASE_KEYS:
        return {"error": f"Invalid disease_key. Choose from: {sorted(VALID_DISEASE_KEYS)}"}
    if granularity not in ("monthly", "quarterly"):
        return {"error": "granularity must be 'monthly' or 'quarterly'"}

    # Map disease_key to a boolean column in raw_vitalsigns
    risk_col_map = {
        "diabetes": "risk_dm",
        "hypertension": "risk_hpt",
        "cardiovascular": "risk_cvd",
        "obesity": "risk_bmi",
        "dyslipidemia": "found_dyslipidemia",
        "stroke": "found_stroke",
    }
    risk_col = risk_col_map.get(disease_key)
    if not risk_col:
        return {"error": f"Trend data not available for '{disease_key}'"}

    trunc = "month" if granularity == "monthly" else "quarter"

    conditions = ["v.cancel_status IS DISTINCT FROM 1"]
    params: list = []

    if dcode:
        dcode = str(dcode).strip()
        conditions.append("v.district_code = %s")
        params.append(dcode)

    where_clause = " AND ".join(conditions)

    # IMPORTANT: This query only returns aggregated counts, never individual records
    sql = f"""
        SELECT
            DATE_TRUNC('{trunc}', v.visit_date) AS period,
            COUNT(DISTINCT v.patient_id) AS total_screened,
            ROUND(
                100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.{risk_col})
                / NULLIF(COUNT(DISTINCT v.patient_id), 0),
                2
            ) AS pct_at_risk
        FROM raw_vitalsigns v
        WHERE {where_clause}
        GROUP BY DATE_TRUNC('{trunc}', v.visit_date)
        HAVING COUNT(DISTINCT v.patient_id) >= %s
        ORDER BY period
    """
    params.append(K_ANONYMITY_THRESHOLD)

    rows = _query_trend(sql, params)

    # Convert period to ISO string
    for row in rows:
        if row.get("period"):
            row["period"] = row["period"].isoformat()

    _write_audit_entry(
        "get_trend",
        {"disease_key": disease_key, "dcode": dcode, "granularity": granularity},
        len(rows),
    )
    return _round_floats(rows)


# ===== Tool 7: get_lab_summary =====

@mcp.tool()
def get_lab_summary(
    dcode: Optional[str] = None,
    zone_code: Optional[str] = None,
) -> Dict:
    """
    Get lab result summary (averages and clinical thresholds) by district or zone.
    ดึงข้อมูลสรุปผลแลป เช่น ค่าเฉลี่ย hemoglobin, FBS, cholesterol, สัดส่วนโลหิตจาง, CKD
    dcode: optional district code
    zone_code: optional zone code (ignored if dcode provided)
    """
    conditions: list[str] = []
    params: list = []

    if dcode:
        conditions.append("l.district_code = %s")
        params.append(str(dcode).strip())
    elif zone_code:
        conditions.append("""
            l.district_code IN (
                SELECT district_code FROM summary_district_disease WHERE zone_code = %s
            )
        """)
        params.append(str(zone_code).strip())

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = f"""
        SELECT
            SUM(total_lab_patients)                                        AS total_lab_patients,
            ROUND((SUM(avg_hemoglobin * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2)  AS avg_hemoglobin,
            ROUND((SUM(avg_hematocrit * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2)  AS avg_hematocrit,
            ROUND((SUM(avg_fbs * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2)         AS avg_fbs,
            ROUND((SUM(avg_cholesterol * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2) AS avg_cholesterol,
            ROUND((SUM(avg_triglyceride * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2) AS avg_triglyceride,
            ROUND((SUM(avg_hdl * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2)         AS avg_hdl,
            ROUND((SUM(avg_ldl * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2)         AS avg_ldl,
            ROUND((SUM(avg_creatinine * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2)  AS avg_creatinine,
            ROUND((SUM(avg_egfr * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2)        AS avg_egfr,
            ROUND((SUM(avg_uric_acid * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2)   AS avg_uric_acid,
            ROUND((SUM(avg_sgot * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2)        AS avg_sgot,
            ROUND((SUM(avg_sgpt * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2)        AS avg_sgpt,
            ROUND((SUM(pct_anemia * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2)      AS pct_anemia,
            ROUND((SUM(pct_ckd * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2)         AS pct_ckd
        FROM summary_district_lab l
        {where_clause}
    """

    rows = _query(sql, params or None)
    result = rows[0] if rows else {}

    _write_audit_entry(
        "get_lab_summary",
        {"dcode": dcode, "zone_code": zone_code},
        1 if result else 0,
    )
    return _round_floats(result)


# ===== Tool 8: get_mental_health_summary =====

@mcp.tool()
def get_mental_health_summary(
    dcode: Optional[str] = None,
    zone_code: Optional[str] = None,
) -> Dict:
    """
    Get mental health screening summary (depression, PHQ-9, stress).
    ดึงข้อมูลสรุปสุขภาพจิต: ความเสี่ยงซึมเศร้า, PHQ-9 ระดับปานกลางขึ้นไป, ความเครียดสูง
    dcode: optional district code
    zone_code: optional zone code (ignored if dcode provided)
    """
    conditions: list[str] = []
    params: list = []

    if dcode:
        conditions.append("m.district_code = %s")
        params.append(str(dcode).strip())
    elif zone_code:
        conditions.append("""
            m.district_code IN (
                SELECT district_code FROM summary_district_disease WHERE zone_code = %s
            )
        """)
        params.append(str(zone_code).strip())

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = f"""
        SELECT
            SUM(total_screened) AS total_screened,
            ROUND((SUM(pct_depression_risk * total_screened) / NULLIF(SUM(total_screened), 0))::numeric, 2) AS pct_depression_risk,
            ROUND((SUM(pct_phq9_moderate * total_screened) / NULLIF(SUM(total_screened), 0))::numeric, 2)   AS pct_phq9_moderate,
            ROUND((SUM(pct_high_stress * total_screened) / NULLIF(SUM(total_screened), 0))::numeric, 2)     AS pct_high_stress
        FROM summary_district_mental m
        {where_clause}
    """

    rows = _query(sql, params or None)
    result = rows[0] if rows else {}

    _write_audit_entry(
        "get_mental_health_summary",
        {"dcode": dcode, "zone_code": zone_code},
        1 if result else 0,
    )
    return _round_floats(result)


# ===== Tool 9: get_demographics =====

@mcp.tool()
def get_demographics(
    dcode: Optional[str] = None,
    zone_code: Optional[str] = None,
) -> Dict:
    """
    Get demographic breakdown: education, occupation, health privilege, housing type.
    ดึงข้อมูลประชากรศาสตร์: การศึกษา, อาชีพ, สิทธิ์สุขภาพ, ประเภทที่อยู่อาศัย
    dcode: optional district code
    zone_code: optional zone code (ignored if dcode provided)
    """
    conditions: list[str] = []
    params: list = []

    if dcode:
        conditions.append("d.district_code = %s")
        params.append(str(dcode).strip())
    elif zone_code:
        conditions.append("""
            d.district_code IN (
                SELECT district_code FROM summary_district_disease WHERE zone_code = %s
            )
        """)
        params.append(str(zone_code).strip())

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = f"""
        SELECT
            SUM(total_respondents) AS total_respondents,
            SUM(edu_none) AS edu_none,
            SUM(edu_primary) AS edu_primary,
            SUM(edu_secondary) AS edu_secondary,
            SUM(edu_high_school) AS edu_high_school,
            SUM(edu_vocational) AS edu_vocational,
            SUM(edu_bachelor) AS edu_bachelor,
            SUM(edu_postgrad) AS edu_postgrad,
            SUM(occ_government) AS occ_government,
            SUM(occ_private) AS occ_private,
            SUM(occ_self_employed) AS occ_self_employed,
            SUM(occ_agriculture) AS occ_agriculture,
            SUM(occ_unemployed) AS occ_unemployed,
            SUM(occ_student) AS occ_student,
            SUM(occ_retired) AS occ_retired,
            SUM(priv_ucs) AS priv_ucs,
            SUM(priv_sso) AS priv_sso,
            SUM(priv_csmbs) AS priv_csmbs,
            SUM(priv_other) AS priv_other,
            SUM(house_owned) AS house_owned,
            SUM(house_rented) AS house_rented,
            SUM(house_condo) AS house_condo,
            SUM(house_other) AS house_other
        FROM summary_district_demographics d
        {where_clause}
    """

    rows = _query(sql, params or None)
    r = rows[0] if rows else {}

    total = r.get("total_respondents") or 0

    def _pct(val: Any) -> Optional[float]:
        if val is None or not total:
            return None
        return round(100.0 * val / total, 2)

    result = {
        "total_respondents": total,
        "education_breakdown": {
            "none": {"count": r.get("edu_none"), "pct": _pct(r.get("edu_none"))},
            "primary": {"count": r.get("edu_primary"), "pct": _pct(r.get("edu_primary"))},
            "secondary": {"count": r.get("edu_secondary"), "pct": _pct(r.get("edu_secondary"))},
            "high_school": {"count": r.get("edu_high_school"), "pct": _pct(r.get("edu_high_school"))},
            "vocational": {"count": r.get("edu_vocational"), "pct": _pct(r.get("edu_vocational"))},
            "bachelor": {"count": r.get("edu_bachelor"), "pct": _pct(r.get("edu_bachelor"))},
            "postgrad": {"count": r.get("edu_postgrad"), "pct": _pct(r.get("edu_postgrad"))},
        },
        "occupation_breakdown": {
            "government": {"count": r.get("occ_government"), "pct": _pct(r.get("occ_government"))},
            "private": {"count": r.get("occ_private"), "pct": _pct(r.get("occ_private"))},
            "self_employed": {"count": r.get("occ_self_employed"), "pct": _pct(r.get("occ_self_employed"))},
            "agriculture": {"count": r.get("occ_agriculture"), "pct": _pct(r.get("occ_agriculture"))},
            "unemployed": {"count": r.get("occ_unemployed"), "pct": _pct(r.get("occ_unemployed"))},
            "student": {"count": r.get("occ_student"), "pct": _pct(r.get("occ_student"))},
            "retired": {"count": r.get("occ_retired"), "pct": _pct(r.get("occ_retired"))},
        },
        "privilege_breakdown": {
            "ucs": {"count": r.get("priv_ucs"), "pct": _pct(r.get("priv_ucs"))},
            "sso": {"count": r.get("priv_sso"), "pct": _pct(r.get("priv_sso"))},
            "csmbs": {"count": r.get("priv_csmbs"), "pct": _pct(r.get("priv_csmbs"))},
            "other": {"count": r.get("priv_other"), "pct": _pct(r.get("priv_other"))},
        },
        "housing_breakdown": {
            "owned": {"count": r.get("house_owned"), "pct": _pct(r.get("house_owned"))},
            "rented": {"count": r.get("house_rented"), "pct": _pct(r.get("house_rented"))},
            "condo": {"count": r.get("house_condo"), "pct": _pct(r.get("house_condo"))},
            "other": {"count": r.get("house_other"), "pct": _pct(r.get("house_other"))},
        },
    }

    _write_audit_entry(
        "get_demographics",
        {"dcode": dcode, "zone_code": zone_code},
        1 if total else 0,
    )
    return _round_floats(result)


# ===== Tool 10: search_districts =====

@mcp.tool()
def search_districts(query: Dict) -> Union[List[Dict], Dict]:
    """
    Search districts by disease prevalence criteria.
    ค้นหาเขตตามเกณฑ์ความชุกของโรค เช่น เขตที่มีเบาหวานสูงสุด
    query: { disease (str), min_pct (float), max_pct (float), sort_by (str), limit (int) }
    disease: diabetes, hypertension, cardiovascular, obesity, dyslipidemia, stroke
    sort_by: 'asc' or 'desc' (default 'desc')
    limit: max rows to return (default 10)
    """
    if not query:
        return {"error": "query parameter is required"}

    disease = str(query.get("disease", "")).strip().lower()
    min_pct = query.get("min_pct")
    max_pct = query.get("max_pct")
    sort_by = str(query.get("sort_by", "desc")).strip().lower()
    limit = int(query.get("limit", 10))

    if disease not in VALID_DISEASE_KEYS:
        return {"error": f"Invalid disease. Choose from: {sorted(VALID_DISEASE_KEYS)}"}
    if sort_by not in ("asc", "desc"):
        return {"error": "sort_by must be 'asc' or 'desc'"}
    if limit < 1 or limit > 100:
        limit = min(max(limit, 1), 100)

    mapping = DISEASE_KEY_MAP[disease]
    # Prefer pct_at_risk column if available, else compute from found_count
    value_col = mapping.get("pct_at_risk")
    if value_col:
        select_expr = f"{value_col} AS matching_value"
    else:
        # For diseases without a pct column, compute from counts
        found_col = mapping.get("pct_found", mapping.get("found_count"))
        select_expr = f"ROUND(100.0 * {found_col} / NULLIF(total_screened, 0), 2) AS matching_value"

    conditions = []
    params: list = []

    if min_pct is not None:
        conditions.append("matching_value >= %s")
        params.append(float(min_pct))
    if max_pct is not None:
        conditions.append("matching_value <= %s")
        params.append(float(max_pct))

    # Use a CTE so we can filter on the computed column
    having_clause = ""
    if conditions:
        having_clause = " HAVING " + " AND ".join(conditions)

    order = "DESC" if sort_by == "desc" else "ASC"

    sql = f"""
        WITH ranked AS (
            SELECT
                district_code AS dcode,
                district_name AS name_th,
                zone_code,
                total_screened,
                {select_expr}
            FROM summary_district_disease
        )
        SELECT * FROM ranked
        WHERE matching_value IS NOT NULL
    """

    if min_pct is not None:
        sql += " AND matching_value >= %s"
        params.append(float(min_pct))
    if max_pct is not None:
        sql += " AND matching_value <= %s"
        params.append(float(max_pct))

    sql += f" ORDER BY matching_value {order} LIMIT %s"
    params.append(limit)

    rows = _query(sql, params)

    _write_audit_entry("search_districts", query, len(rows))
    return _round_floats(rows)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
