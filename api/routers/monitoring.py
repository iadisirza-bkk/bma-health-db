"""
Monitoring router — data quality, cleansing, ETL status, audit.
Refactored for bma_med.* schema.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from database import execute_query, execute_scalar
from cache import cache_stats, cache_flush_all

router = APIRouter(prefix="/api/v2/monitoring", tags=["Monitoring"])

# --------------------------------------------------------------------------- #
# Reusable UNION across the two main vitalsigns sources (app1 + portal)
# Used for any query that needs aggregate visit data across all sources.
# --------------------------------------------------------------------------- #
_VISITS_UNION_SQL = """
SELECT patient_id, vstdate, hbpn, lbpn, alcohal, smoke, record_cancelled,
       dm, hpt, cdvcl, stroke, fat, chltr,
       riskdm, riskhpt, riskcdvcl, riskbmi
FROM bma_med.app1_vitalsignslf
UNION ALL
SELECT patient_id, vstdate, hbpn, lbpn, alcohal, smoke, record_cancelled,
       dm, hpt, cdvcl, stroke, fat, chltr,
       riskdm, riskhpt, riskcdvcl, riskbmi
FROM bma_med.portal_vitalsignslf
"""

# Map old raw_* table names → list of (schema.table) underlying them in bma_med.*
# Used by the data-quality + cleansing reports to walk per-source completeness.
_BMA_MED_TABLE_MAP = {
    "raw_patients": ["bma_med.patient"],
    "raw_vitalsigns": ["bma_med.app1_vitalsignslf", "bma_med.portal_vitalsignslf"],
    "raw_homevisit": ["bma_med.app1_homevisit", "bma_med.portal_homevisit"],
    "raw_homehealth": ["bma_med.app1_homehealth", "bma_med.portal_homehealth"],
}


@router.get("/data-quality")
def data_quality():
    """Data completeness report -- row counts per legacy table.

    Note: the api_user (bma_api_reader) role intentionally has NO direct SELECT on
    bma_med.* (raw screening tables). Row counts come from pg_stat_user_tables
    (catalog access only). Per-column null rates require row-level access and
    are therefore not reported here — promote a `mv_data_quality` summary view
    if the field-level breakdown is needed.
    TODO: bma_med equivalent unclear — once a public.mv_data_quality MV is
    created from inside bma_med (e.g. via etl_user) and granted to api_user,
    re-enable per-column null counts here.
    """
    # Row counts per underlying bma_med.* table via catalog (no row-level read needed)
    catalog_rows = execute_query("""
        SELECT n.nspname AS schema_name,
               c.relname AS table_name,
               COALESCE(s.n_live_tup, 0)::bigint AS row_count
        FROM pg_class c
        JOIN pg_namespace n ON c.relnamespace = n.oid
        LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
        WHERE n.nspname = 'bma_med' AND c.relkind = 'r'
    """)
    counts = {f"{r['schema_name']}.{r['table_name']}": int(r['row_count'] or 0)
              for r in catalog_rows}

    result = {}
    for legacy_name, real_tables in _BMA_MED_TABLE_MAP.items():
        total = sum(counts.get(fq, 0) for fq in real_tables)
        result[legacy_name] = {
            "total_rows": int(total),
            "fields": {},
            "underlying_tables": real_tables,
            "note": "field-level null counts unavailable to read-only role; "
                    "promote a public.mv_data_quality MV if needed",
        }

    return {
        "tables": result,
        "blocked_fields": [],
        "data_available_partial": True,
        "message": "Row counts only — per-column null rates require row-level access "
                   "to bma_med.* which the api_user does not have.",
    }


@router.get("/cleansing-report")
def cleansing_report():
    """Data cleansing summary -- what was cleaned during import."""
    tables_info = {}

    # Defense in depth — _BMA_MED_TABLE_MAP is hardcoded so schema/tbl can
    # only be one of a known set, but assert before f-string interpolation.
    # If this list ever takes user input, the assert fails closed.
    _ALLOWED_SCHEMAS = {"bma_med", "public"}
    for legacy_name, real_tables in _BMA_MED_TABLE_MAP.items():
        total = 0
        cancelled = 0
        for fq in real_tables:
            schema, tbl = fq.split(".")
            assert schema in _ALLOWED_SCHEMAS, schema
            assert tbl.replace("_", "").isalnum(), tbl
            total += execute_scalar(f'SELECT COUNT(*) FROM "{schema}"."{tbl}"') or 0
            if legacy_name != "raw_patients":
                cancelled += execute_scalar(
                    f'SELECT COUNT(*) FROM "{schema}"."{tbl}" WHERE record_cancelled = 1'
                ) or 0

        tables_info[legacy_name] = {
            "total_rows": int(total),
            "active_rows": int(total - cancelled),
            "cancelled_excluded": int(cancelled),
        }

    null_birth = execute_scalar(
        "SELECT COUNT(*) FROM bma_med.patient WHERE birthdate IS NULL"
    ) or 0
    null_sex = execute_scalar(
        "SELECT COUNT(*) FROM bma_med.patient WHERE sex_code IS NULL"
    ) or 0
    tables_info["raw_patients"]["null_birth_year"] = int(null_birth)
    tables_info["raw_patients"]["null_sex"] = int(null_sex)

    # district_code lives in homevisit (crdistrict/district), not in vitalsignslf in new schema.
    # TODO: bma_med equivalent unclear — counting visits with no homevisit district_code lookup.
    null_district = 0
    for fq in ["bma_med.app1_homevisit", "bma_med.portal_homevisit"]:
        schema, tbl = fq.split(".")
        assert schema in _ALLOWED_SCHEMAS, schema
        assert tbl.replace("_", "").isalnum(), tbl
        null_district += execute_scalar(
            f'SELECT COUNT(*) FROM "{schema}"."{tbl}" WHERE district IS NULL AND crdistrict IS NULL'
        ) or 0

    null_bp = 0
    for fq in ["bma_med.app1_vitalsignslf", "bma_med.portal_vitalsignslf"]:
        schema, tbl = fq.split(".")
        assert schema in _ALLOWED_SCHEMAS, schema
        assert tbl.replace("_", "").isalnum(), tbl
        null_bp += execute_scalar(
            f'SELECT COUNT(*) FROM "{schema}"."{tbl}" WHERE (hbpn IS NULL OR hbpn = 0) AND record_cancelled = 0'
        ) or 0
    tables_info["raw_vitalsigns"]["null_district_code"] = int(null_district)
    tables_info["raw_vitalsigns"]["null_bp"] = int(null_bp)

    last_import = execute_query("""
        SELECT filename, file_type, status, started_at, rows_imported
        FROM import_history
        ORDER BY started_at DESC LIMIT 5
    """)

    blocked = []
    # TODO: bma_med equivalent unclear — egfr/cervical/colorectal labs and
    # food_preference_* / *_treatment / referral_type are not in the migration map.
    # Returning empty list rather than throwing.
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
