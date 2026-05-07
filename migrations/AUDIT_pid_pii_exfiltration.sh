#!/usr/bin/env bash
# =============================================================================
# AUDIT_pid_pii_exfiltration.sh — Stage 4 of pid_encoded remediation
# =============================================================================
# Looks for evidence that the base64 plaintext IDCARD column was read or
# exfiltrated before migrations 500/501/502 closed the leak. Runs three
# independent checks:
#
#   A. Postgres pg_stat_statements — any query that touched pid_encoded /
#      bma_med.patient / raw_patients in the recorded statement window.
#   B. bma_med.audit_log — any TRUNCATE/SELECT-with-COPY operations on the
#      patient table (TRUNCATEs are logged; COPY/dump activity is not, but
#      may show as bursts in import_history).
#   C. FastAPI stdout — needs server-side journalctl/docker logs/Cloudflare
#      Worker logs; this script prints the grep recipe but does not execute.
#
# REQUIRES:
#   - psql with WRITER credentials (so CREATE EXTENSION works if needed)
#   - pg_stat_statements extension enabled (most prod Postgres ship with it)
#   - jq for log filtering (optional)
#
# OUTPUT: prints findings to stdout and writes JSONL summary to
#   /tmp/pid_pii_audit_$(date +%F).jsonl  for archival.
#
# ZERO false positives by design — only flag queries that explicitly name
# pid_encoded or bma_med.patient. Generic SELECT * FROM raw_patients is OK
# because that view doesn't expose pid_encoded.
# =============================================================================

set -euo pipefail

DB_URL="${DATABASE_URL_WRITER:-${DATABASE_URL:-}}"
if [[ -z "${DB_URL}" ]]; then
    echo "FATAL: DATABASE_URL_WRITER (or DATABASE_URL) not set" >&2
    exit 1
fi

OUT_FILE="/tmp/pid_pii_audit_$(date +%F).jsonl"
echo "Writing findings to ${OUT_FILE}"
: > "${OUT_FILE}"

echo
echo "=== A. pg_stat_statements — queries touching pid_encoded / patient ==="
echo

psql "${DB_URL}" -At <<'SQL' | tee -a "${OUT_FILE}"
SELECT json_build_object(
    'check', 'pg_stat_statements',
    'pattern', regexp_replace(query, '\\s+', ' ', 'g'),
    'calls', calls,
    'rows_returned', rows,
    'total_exec_time_ms', round(total_exec_time::numeric, 2),
    'last_seen', NULL  -- pg_stat_statements doesn't track last-seen timestamps
)::text
FROM pg_stat_statements
WHERE query ILIKE '%pid_encoded%'
   OR query ILIKE '%bma_med.patient%'
   OR query ILIKE '%FROM patient%'
   OR query ILIKE '%idcard_hash%'
ORDER BY calls DESC
LIMIT 100;
SQL

echo
echo "=== B. bma_med.audit_log — patient-table writes (last 30 days) ==="
echo

psql "${DB_URL}" -At <<'SQL' | tee -a "${OUT_FILE}"
SELECT json_build_object(
    'check', 'audit_log_patient_writes',
    'occurred_at', occurred_at,
    'user_name', user_name,
    'operation', operation,
    'row_pk', row_pk,
    'detail_keys', (SELECT json_agg(k) FROM jsonb_object_keys(detail) k)
)::text
FROM bma_med.audit_log
WHERE table_name = 'patient'
  AND occurred_at > now() - INTERVAL '30 days'
ORDER BY occurred_at DESC
LIMIT 200;
SQL

echo
echo "=== C. import_history — bulk export bursts (>10k rows) ==="
echo

psql "${DB_URL}" -At <<'SQL' | tee -a "${OUT_FILE}"
SELECT json_build_object(
    'check', 'import_history_burst',
    'started_at', started_at,
    'filename', filename,
    'rows_imported', rows_imported,
    'uploaded_by', uploaded_by,
    'status', status
)::text
FROM import_history
WHERE rows_imported > 10000
   OR file_type IN ('csv', 'sql', 'dump')  -- export-shaped payloads
ORDER BY started_at DESC
LIMIT 50;
SQL

echo
echo "=== D. Roles currently holding SELECT on identifier columns ==="
echo

psql "${DB_URL}" -At <<'SQL' | tee -a "${OUT_FILE}"
SELECT json_build_object(
    'check', 'current_grants',
    'grantee', grantee,
    'table', table_schema || '.' || table_name,
    'column', column_name,
    'privilege', privilege_type
)::text
FROM information_schema.column_privileges
WHERE table_schema = 'bma_med'
  AND column_name IN ('pid', 'pid_encoded', 'pid_hash', 'idcard')
  AND privilege_type = 'SELECT'
  AND grantee NOT IN ('postgres', 'bma_med_admin');
SQL

echo
cat <<'EOF'
=== E. Server-side log review (do this manually) ===

Run on the API host (Cloudflare Worker, journalctl, docker, k8s — pick yours):

  # Cloudflare Workers (wrangler tail or dashboard):
  wrangler tail --format=pretty | grep -E 'pid_encoded|bma_med.patient'

  # systemd / journalctl:
  journalctl -u bma-health-api --since '30 days ago' \
    | grep -E 'AUDIT.*path=/api/v2/(monitoring|admin)' \
    | awk '$9 == "200"' \
    | sort | uniq -c | sort -rn | head -50

  # Docker:
  docker logs bma-health-api 2>&1 | grep -E 'pid_encoded|raw_patients'

Look for:
  - Repeated non-200 hits (probing)
  - Unusual ip= values from outside known operator ranges
  - Any AUDIT line referencing /admin/* from a non-admin IP

EOF

echo
echo "=== F. PDPC notification check ==="
cat <<'EOF'
If sections A, B, C, or E surface concrete evidence of plaintext IDCARD
extraction (NOT just metadata reads), Thai PDPA §37 requires notification
to the PDPC and to affected data subjects within 72 hours.

  - PDPC notification portal: https://www.pdpc.or.th/
  - Affected-subject notification: required if "high risk" — most IDCARD
    leaks meet this threshold.
  - Internal: file an incident in the breach register; preserve the
    evidence (this script's JSONL output) for the regulator's inspection.

EOF

echo "Audit complete. Findings: ${OUT_FILE}"
echo "Line count by check:"
sort "${OUT_FILE}" | jq -r '.check' 2>/dev/null | sort | uniq -c | sort -rn || cat "${OUT_FILE}" | wc -l
