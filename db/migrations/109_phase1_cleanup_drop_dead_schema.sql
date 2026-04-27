-- =============================================================================
-- Migration 109 — Phase 1 cleanup: drop confirmed-dead schema
-- =============================================================================
--
-- Drops 9 empty private.* long-tail tables and 4 confirmed-dead public.v_*
-- alias views. All targets verified via:
--   1. pg_stat_user_tables.n_live_tup = 0   (zero rows)
--   2. pg_depend / pg_rewrite scan         (zero view dependents)
--   3. grep across api/ + etl/ + tests/    (zero code references)
--
-- Source: CLEANUP-PROPOSAL.md §2 items #1-#13.
--
-- Note: v_cross_system_duplicates and v_source_row_counts (#14-#15) are
-- referenced from LaTeX `lstlisting` examples in templates and are deferred
-- to Phase 2 per the proposal.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- Step 1: drop empty private.visit_* long-tail tables
-- (defined in 100_schema_v3_private.sql, never populated; v3 ETL writes to
--  private.visit_measurement EAV instead)
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS private.visit_pain;
DROP TABLE IF EXISTS private.visit_neurological;
DROP TABLE IF EXISTS private.visit_respiratory;
DROP TABLE IF EXISTS private.visit_recommendation;
DROP TABLE IF EXISTS private.visit_referral;

-- -----------------------------------------------------------------------------
-- Step 2: drop empty private.patient_* long-tail tables
-- (defined for a future feature that never shipped)
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS private.patient_attribute;
DROP TABLE IF EXISTS private.patient_chronic_history;
DROP TABLE IF EXISTS private.patient_family_history;
DROP TABLE IF EXISTS private.patient_allergy;

-- -----------------------------------------------------------------------------
-- Step 3: drop dead public.v_* alias views
-- (redundant aliases for private.geo_*/facility/data_source — unused by code)
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS public.v_districts;
DROP VIEW IF EXISTS public.v_health_zones;
DROP VIEW IF EXISTS public.v_facilities;
DROP VIEW IF EXISTS public.v_data_sources;

COMMIT;
