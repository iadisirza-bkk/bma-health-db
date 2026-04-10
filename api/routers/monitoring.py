"""
Monitoring router — data quality, cleansing, ETL status, audit.
Extracted from main.py lines 894-935, 938-995, 1492-1515, 2221-2273.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from database import execute_query, execute_scalar
from cache import cache_stats, cache_flush_all

router = APIRouter(prefix="/api/v2/monitoring", tags=["Monitoring"])


@router.get("/data-quality")
def data_quality():
    """Data completeness report -- null rates per table per field."""
    tables = ["raw_patients", "raw_visits", "raw_vitalsigns", "raw_homevisit",
              "raw_homehealth", "raw_lab_results", "raw_lab_extended"]
    result = {}
    for table in tables:
        total = execute_scalar(f'SELECT COUNT(*) FROM "{table}"') or 0
        if total == 0:
            result[table] = {"total_rows": 0, "fields": {}}
            continue

        cols = execute_query("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            AND column_name NOT IN ('id','created_at','updated_at')
            ORDER BY ordinal_position
        """, (table,))

        fields = {}
        for col in cols:
            cn = col["column_name"]
            null_count = execute_scalar(
                f'SELECT COUNT(*) FROM "{table}" WHERE "{cn}" IS NULL'
            ) or 0
            fields[cn] = {
                "null_count": int(null_count),
                "null_pct": round(100.0 * null_count / total, 1) if total > 0 else 0,
                "filled_pct": round(100.0 * (total - null_count) / total, 1) if total > 0 else 0,
            }

        result[table] = {"total_rows": int(total), "fields": fields}

    blocked = []
    for table, info in result.items():
        for field, stats in info.get("fields", {}).items():
            if stats["null_pct"] >= 100 and info["total_rows"] > 0:
                blocked.append({"table": table, "field": field, "note": f"ไม่มีข้อมูล {field} เลย"})

    return {"tables": result, "blocked_fields": blocked}


@router.get("/cleansing-report")
def cleansing_report():
    """Data cleansing summary -- what was cleaned during import."""
    tables_info = {}

    for table in ["raw_patients", "raw_vitalsigns", "raw_lab_results", "raw_homevisit", "raw_homehealth", "raw_lab_extended"]:
        total = execute_scalar(f'SELECT COUNT(*) FROM "{table}"') or 0
        cancelled = 0
        if table != "raw_patients":
            cancelled = execute_scalar(f'SELECT COUNT(*) FROM "{table}" WHERE cancel_status = 1') or 0

        tables_info[table] = {
            "total_rows": int(total),
            "active_rows": int(total - cancelled),
            "cancelled_excluded": int(cancelled),
        }

    null_birth = execute_scalar("SELECT COUNT(*) FROM raw_patients WHERE birth_year IS NULL") or 0
    null_sex = execute_scalar("SELECT COUNT(*) FROM raw_patients WHERE sex IS NULL") or 0
    tables_info["raw_patients"]["null_birth_year"] = int(null_birth)
    tables_info["raw_patients"]["null_sex"] = int(null_sex)

    null_district = execute_scalar("SELECT COUNT(*) FROM raw_vitalsigns WHERE district_code IS NULL AND cancel_status = 0") or 0
    null_bp = execute_scalar("SELECT COUNT(*) FROM raw_vitalsigns WHERE (sbp IS NULL OR sbp = 0) AND cancel_status = 0") or 0
    tables_info["raw_vitalsigns"]["null_district_code"] = int(null_district)
    tables_info["raw_vitalsigns"]["null_bp"] = int(null_bp)

    last_import = execute_query("""
        SELECT filename, file_type, status, started_at, rows_imported
        FROM import_history
        ORDER BY started_at DESC LIMIT 5
    """)

    blocked = []
    checks = [
        ("raw_lab_results", "egfr", "eGFR (ค่าการทำงานของไต)"),
        ("raw_lab_results", "cervical_cancer_result", "มะเร็งปากมดลูก"),
        ("raw_lab_results", "colorectal_result", "มะเร็งลำไส้"),
        ("raw_homehealth", "food_preference_sweet", "ความชอบอาหารหวาน"),
        ("raw_homehealth", "dm_treatment", "สถานะการรักษาเบาหวาน"),
        ("raw_vitalsigns", "referral_type", "ประเภทการส่งต่อ"),
    ]
    for table, field, label in checks:
        total_t = execute_scalar(f'SELECT COUNT(*) FROM "{table}"') or 0
        if total_t > 0:
            filled = execute_scalar(f'SELECT COUNT(*) FROM "{table}" WHERE "{field}" IS NOT NULL') or 0
            if filled == 0:
                blocked.append({"table": table, "field": field, "label": label, "null_pct": 100.0})

    return {
        "tables": tables_info,
        "recent_imports": last_import,
        "blocked_fields": blocked,
    }


@router.get("/table-stats")
def table_stats():
    """Row counts and metadata per table."""
    rows = execute_query("""
        SELECT
            t.tablename AS table_name,
            COALESCE(s.n_live_tup, 0) AS row_count,
            (SELECT COUNT(*) FROM information_schema.columns c
             WHERE c.table_schema = 'public' AND c.table_name = t.tablename) AS column_count
        FROM pg_catalog.pg_tables t
        LEFT JOIN pg_stat_user_tables s ON s.relname = t.tablename
        WHERE t.schemaname = 'public'
        ORDER BY t.tablename
    """)

    last_updated = execute_scalar(
        "SELECT MAX(refreshed_at) FROM summary_district_disease"
    )

    return {
        "tables": rows,
        "last_updated": str(last_updated) if last_updated else None,
    }


@router.get("/etl-status")
def etl_status():
    """ETL pipeline status: last import per file type, success/failure."""
    rows = execute_query("""
        SELECT file_type,
               MAX(started_at) as last_import,
               MAX(CASE WHEN status = 'success' THEN started_at END) as last_success,
               COUNT(*) as total_imports,
               COUNT(CASE WHEN status = 'success' THEN 1 END) as success_count,
               COUNT(CASE WHEN status = 'error' THEN 1 END) as error_count
        FROM import_history
        GROUP BY file_type
        ORDER BY file_type
    """)
    for r in rows:
        for k in ("last_import", "last_success"):
            if r.get(k) and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()
    return {"file_types": rows, "overall_status": "healthy" if rows else "no_imports"}


@router.get("/api-performance")
def api_performance():
    """API performance metrics."""
    return {
        "note": "API performance metrics are logged to stdout via AuditMiddleware",
        "endpoints_count": 72,
        "rate_limit": {"public": 60, "per_minute": True},
        "cache_ttl": {"health_data": 300, "static_data": 3600},
        "database_pool": {"min_connections": 2, "max_connections": 20},
    }


@router.get("/audit-log")
def audit_log(limit: int = Query(50, ge=1, le=500)):
    """PDPA audit log: recent data access events from import history."""
    rows = execute_query("""
        SELECT id, filename, table_name, file_type, status,
               started_at, completed_at, rows_imported, uploaded_by
        FROM import_history
        ORDER BY started_at DESC
        LIMIT %s
    """, (limit,))
    for r in rows:
        for k in ("started_at", "completed_at"):
            if r.get(k) and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()
    return {"events": rows, "source": "import_history",
            "note": "Full API audit logs available in server stdout (AuditMiddleware)"}


@router.get("/cache-stats")
def get_cache_stats():
    """Redis cache statistics — hit rate, key count, availability."""
    return cache_stats()
