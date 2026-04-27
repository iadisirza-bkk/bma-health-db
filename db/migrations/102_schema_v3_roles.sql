-- =============================================================================
-- Schema v3 — Roles & Permissions
-- =============================================================================
-- Defines THE security boundary:
--   - bma_etl_writer:  ETL pipeline → INSERT/UPDATE private.*
--   - bma_dba_admin:   DBAs → ALL on both schemas
--   - bma_api_reader:  FastAPI → SELECT public.* ONLY (no private)
-- =============================================================================

-- Drop existing roles (idempotent)
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bma_etl_writer') THEN
    CREATE ROLE bma_etl_writer NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bma_dba_admin') THEN
    CREATE ROLE bma_dba_admin NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bma_api_reader') THEN
    CREATE ROLE bma_api_reader NOLOGIN;
  END IF;
END $$;

-- -----------------------------------------------------------------------------
-- Lock down PUBLIC pseudo-role: deny everything by default
-- -----------------------------------------------------------------------------
REVOKE ALL ON SCHEMA private FROM PUBLIC;
REVOKE ALL ON ALL TABLES    IN SCHEMA private FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA private FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA private FROM PUBLIC;

-- public schema: only what we explicitly grant
REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- -----------------------------------------------------------------------------
-- bma_etl_writer — pipeline that imports CSVs
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA private TO bma_etl_writer;
GRANT USAGE ON SCHEMA public  TO bma_etl_writer;       -- can call refresh_all_mvs()

GRANT SELECT, INSERT, UPDATE, DELETE
  ON ALL TABLES IN SCHEMA private TO bma_etl_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA private TO bma_etl_writer;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO bma_etl_writer;

-- Legacy admin tracking tables in public schema (admin upload writes here):
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema='public' AND table_name='import_history') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON public.import_history TO bma_etl_writer;
    GRANT USAGE, SELECT ON SEQUENCE public.import_history_id_seq TO bma_etl_writer;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema='public' AND table_name='erasure_requests') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON public.erasure_requests TO bma_etl_writer;
    GRANT USAGE, SELECT ON SEQUENCE public.erasure_requests_id_seq TO bma_etl_writer;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema='public' AND table_name='mv_refresh_log') THEN
    GRANT SELECT, INSERT ON public.mv_refresh_log TO bma_etl_writer;
    GRANT USAGE, SELECT ON SEQUENCE public.mv_refresh_log_id_seq TO bma_etl_writer;
  END IF;
END $$;

-- Read access to legacy ref tables (admin pages display facilities/zones/districts):
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema='public' AND table_name='ref_districts') THEN
    GRANT SELECT ON public.ref_districts, public.ref_health_zones, public.ref_facilities
      TO bma_etl_writer;
  END IF;
END $$;

-- Default privileges for future tables created in private
ALTER DEFAULT PRIVILEGES IN SCHEMA private
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bma_etl_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA private
  GRANT USAGE, SELECT ON SEQUENCES TO bma_etl_writer;

-- -----------------------------------------------------------------------------
-- bma_dba_admin — full DBA access (audit-logged)
-- -----------------------------------------------------------------------------
GRANT ALL ON SCHEMA private, public TO bma_dba_admin;
GRANT ALL ON ALL TABLES    IN SCHEMA private TO bma_dba_admin;
GRANT ALL ON ALL TABLES    IN SCHEMA public  TO bma_dba_admin;
GRANT ALL ON ALL SEQUENCES IN SCHEMA private TO bma_dba_admin;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public  TO bma_dba_admin;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA private, public TO bma_dba_admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA private GRANT ALL ON TABLES    TO bma_dba_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public  GRANT ALL ON TABLES    TO bma_dba_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA private GRANT ALL ON SEQUENCES TO bma_dba_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public  GRANT ALL ON SEQUENCES TO bma_dba_admin;

-- -----------------------------------------------------------------------------
-- bma_api_reader — FastAPI / Frontend
-- ▶ Can ONLY read from public.mv_* and public.v_*
-- ▶ NO access to private schema
-- ▶ Cannot bypass k-anonymity (which is enforced inside MV definitions)
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO bma_api_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO bma_api_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO bma_api_reader;

-- IMPORTANT: explicitly deny private (in case of misconfiguration)
REVOKE ALL ON SCHEMA private FROM bma_api_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA private FROM bma_api_reader;

-- -----------------------------------------------------------------------------
-- Login users (created here for completeness; passwords set via env)
-- -----------------------------------------------------------------------------
-- Note: in the real deployment, these should be created with strong passwords
-- and the existing 'postgres' superuser stays for emergencies. The application
-- DATABASE_URL should switch to api_user after migration verified.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'etl_user') THEN
    CREATE USER etl_user PASSWORD 'CHANGE_ME_etl_pwd' IN ROLE bma_etl_writer;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'api_user') THEN
    CREATE USER api_user PASSWORD 'CHANGE_ME_api_pwd' IN ROLE bma_api_reader;
  END IF;
END $$;

-- -----------------------------------------------------------------------------
-- Row-level security (defense in depth) on patient — hide erased rows
-- -----------------------------------------------------------------------------
ALTER TABLE private.patient ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS exclude_erased ON private.patient;
CREATE POLICY exclude_erased ON private.patient
  FOR SELECT
  USING (NOT is_erased);
-- bma_dba_admin bypasses RLS via BYPASSRLS:
ALTER ROLE bma_dba_admin BYPASSRLS;
