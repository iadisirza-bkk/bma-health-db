-- =============================================================================
-- 500_drop_audit_triggers_on_bulk_tables.sql
-- =============================================================================
-- Issue: 14 bma_med source tables have audit triggers writing
--   to_jsonb(OLD) + to_jsonb(NEW) on every INSERT/UPDATE/DELETE.
-- Today's audit_log = 80k rows / 346 MB. Next data import projection
-- = 10-15M rows / ~20-30 GB. Source tables hold immutable raw
-- screening readings; no clinical use for per-row audit history.
--
-- Action: drop audit triggers from the 13 bulk tables. Keep
-- patient_audit (PII tier, regulatory).
--
-- Idempotent: DROP TRIGGER IF EXISTS — safe to re-run.
-- =============================================================================

DO $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT tgname, tgrelid::regclass AS tbl
    FROM pg_trigger
    WHERE NOT tgisinternal
      AND tgname LIKE '%audit%'
      AND tgrelid::regclass::text LIKE 'bma_med.%'
      AND tgrelid::regclass::text NOT IN ('bma_med.patient')
  LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON %s', r.tgname, r.tbl);
    RAISE NOTICE 'Dropped audit trigger % on %', r.tgname, r.tbl;
  END LOOP;
END$$;

-- Optional retention policy: delete audit_log older than 180 days.
-- Run as a scheduled job; commented out here so apply is non-destructive.
-- DELETE FROM bma_med.audit_log WHERE occurred_at < now() - interval '180 days';

-- Verify after running:
--   SELECT tgrelid::regclass, tgname FROM pg_trigger
--   WHERE NOT tgisinternal AND tgname LIKE '%audit%'
--     AND tgrelid::regclass::text LIKE 'bma_med.%';
-- Expected: only `bma_med.patient` rows.
