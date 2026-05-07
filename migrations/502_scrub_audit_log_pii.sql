-- =============================================================================
-- 502_scrub_audit_log_pii.sql — purge base64 IDCARDs from bma_med.audit_log
-- =============================================================================
-- WHY: bma_med.audit_log was populated by the patient_audit trigger
-- (schema_init.sql:178), which writes to_jsonb(OLD) and to_jsonb(NEW) of
-- every patient row to audit_log.detail on every INSERT/UPDATE/DELETE.
-- Until the migration 501 rehash, those JSONB blobs contained the base64
-- plaintext IDCARD in the .pid_encoded key — a secondary PII leak that
-- mirrors the one in bma_med.patient itself.
--
-- This migration redacts the pid_encoded field inside detail->'before' and
-- detail->'after' for every audit_log row that touched the patient table.
-- The audit trail of WHEN and BY WHOM stays intact; only the leaked column
-- value is replaced with a redaction marker so a future row-level audit
-- can still see "this row was changed at T by U" without the PII.
--
-- HOW TO RUN:
--   psql "$DATABASE_URL_WRITER" -f migrations/502_scrub_audit_log_pii.sql
--
-- DOWNTIME: depends on audit_log size. Per spec, audit_log only contains
-- patient master changes (one row per patient INSERT or UPDATE), so even
-- with full history it's bounded at low millions and finishes in seconds.
--
-- ROLLBACK: irreversible by design — the leaked plaintext is gone.
-- =============================================================================

\set ON_ERROR_STOP on

BEGIN;

-- Scrub the .pid_encoded key inside both before/after JSONB blobs.
-- Use jsonb_set when the path exists; #- to drop it otherwise.
UPDATE bma_med.audit_log
SET detail = jsonb_set(
    jsonb_set(detail, '{before,pid_encoded}', '"<redacted-by-502>"'::jsonb, false),
    '{after,pid_encoded}', '"<redacted-by-502>"'::jsonb, false
)
WHERE table_name = 'patient'
  AND (detail->'before' ? 'pid_encoded' OR detail->'after' ? 'pid_encoded')
  -- Skip rows already scrubbed (idempotent)
  AND NOT (
    (detail->'before'->>'pid_encoded' = '<redacted-by-502>')
    AND (detail->'after'->>'pid_encoded' = '<redacted-by-502>' OR detail->'after' IS NULL)
  );

-- Also scrub pid_hash if present (reversibility same risk class).
UPDATE bma_med.audit_log
SET detail = jsonb_set(
    jsonb_set(detail, '{before,pid_hash}', '"<redacted-by-502>"'::jsonb, false),
    '{after,pid_hash}', '"<redacted-by-502>"'::jsonb, false
)
WHERE table_name = 'patient'
  AND (detail->'before' ? 'pid_hash' OR detail->'after' ? 'pid_hash')
  AND NOT (
    (detail->'before'->>'pid_hash' = '<redacted-by-502>')
    AND (detail->'after'->>'pid_hash' = '<redacted-by-502>' OR detail->'after' IS NULL)
  );

-- Sanity check
DO $$
DECLARE
    leaked int;
BEGIN
    SELECT COUNT(*) INTO leaked FROM bma_med.audit_log
    WHERE table_name = 'patient'
      AND (
        (detail->'before'->>'pid_encoded') IS NOT NULL
          AND (detail->'before'->>'pid_encoded') != '<redacted-by-502>'
          AND (detail->'before'->>'pid_encoded') !~ '^[0-9a-f]{64}$'
        OR
        (detail->'after'->>'pid_encoded') IS NOT NULL
          AND (detail->'after'->>'pid_encoded') != '<redacted-by-502>'
          AND (detail->'after'->>'pid_encoded') !~ '^[0-9a-f]{64}$'
      );
    IF leaked > 0 THEN
        RAISE EXCEPTION 'audit_log: % rows still hold non-hashed pid_encoded in detail', leaked;
    END IF;
    RAISE NOTICE '✓ audit_log scrub complete';
END $$;

COMMIT;
