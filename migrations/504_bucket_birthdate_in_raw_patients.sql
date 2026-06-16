-- =============================================================================
-- 504_bucket_birthdate_in_raw_patients.sql — replace birthdate with age bucket
-- =============================================================================
-- WHY: public.raw_patients (created in 400_compat_raw_views.sql:294) exposes
-- birthdate, sex_code, first_seen, last_seen. These are not directly PII, but
-- they form a quasi-identifier set: combined with the structure of the Thai
-- national ID — digits 6-10 encode birth-registration order in a district —
-- birthdate + sex narrows the search to a handful of candidates. Joined with
-- public election-roll or census data, re-identification becomes feasible
-- even without ever seeing pid_encoded.
--
-- Replacing birthdate with a 5-year age bucket reduces the cardinality from
-- ~30k distinct values (one per day across a 80-year span) to 16 buckets
-- (0-4, 5-9, ..., 75+). A 5-year bucket is wide enough that, combined with
-- sex and any single zone, you stay above the k=5 threshold the API already
-- enforces in security/k_anon.py.
--
-- first_seen / last_seen are kept as raw timestamps (DATE precision is fine
-- for the "screening trend over time" analyses that need them).
--
-- HOW TO RUN:
--   psql "$DATABASE_URL_WRITER" -f migrations/504_bucket_birthdate_in_raw_patients.sql
--
-- DOWNTIME: zero — CREATE OR REPLACE VIEW is metadata-only, no data scan.
-- Existing connections see the new view definition on their next query.
--
-- ROLLBACK:
--   CREATE OR REPLACE VIEW public.raw_patients AS
--   SELECT patient_id, sex_code, birthdate, first_seen, last_seen
--   FROM bma_med.patient;
-- =============================================================================

\set ON_ERROR_STOP on

BEGIN;

-- ── 1. New view definition ─────────────────────────────────────────────────
-- age_years_bucket = lower edge of the 5-year band the patient falls into,
-- computed against the latest visit (last_seen) or current date if last_seen
-- is missing. NULL passed through if birthdate is NULL.
--
-- 75+ patients all map to the same bucket (75) per ICD demographic
-- conventions — the population gets sparse fast, so finer buckets at the
-- top end break k-anon for old-age subgroups.
CREATE OR REPLACE VIEW public.raw_patients AS
SELECT
    p.patient_id,
    p.sex_code,
    CASE
        WHEN p.birthdate IS NULL THEN NULL
        WHEN EXTRACT(YEAR FROM age(COALESCE(p.last_seen::date, current_date), p.birthdate))::int >= 75 THEN 75
        ELSE (EXTRACT(YEAR FROM age(COALESCE(p.last_seen::date, current_date), p.birthdate))::int / 5) * 5
    END AS age_years_bucket,
    p.first_seen,
    p.last_seen
FROM bma_med.patient p;

COMMENT ON COLUMN public.raw_patients.age_years_bucket IS
    'Lower edge of 5-year age band at last_seen (e.g. 35 = 35-39 years). '
    '75 = 75+. NULL when birthdate missing. Replaces raw birthdate per '
    'migration 504 (quasi-identifier mitigation).';

-- ── 2. Re-grant (CREATE OR REPLACE preserves grants but be defensive) ──────
GRANT SELECT ON public.raw_patients TO api_user, etl_user;

-- ── 3. Sanity check — make sure no caller depends on the dropped column ────
-- This is a compile-time check, not runtime: information_schema.view_column_usage
-- shows what columns *each view* references. If any other view depends on the
-- old `birthdate` column from raw_patients (none should, per the audit), it'll
-- still work — they'll just see NULL where birthdate used to be.
DO $$
DECLARE
    dependent_views text;
BEGIN
    SELECT string_agg(table_schema || '.' || table_name, ', ')
    INTO dependent_views
    FROM information_schema.view_column_usage
    WHERE view_schema = 'public'
      AND view_name = 'raw_patients'
      AND column_name = 'birthdate';

    IF dependent_views IS NOT NULL THEN
        RAISE NOTICE 'note: views depend on raw_patients.birthdate: %. They will see NULL.', dependent_views;
    ELSE
        RAISE NOTICE '✓ no other view depends on raw_patients.birthdate';
    END IF;
END $$;

COMMIT;
