-- =============================================================================
-- Migration 259 — Cleanup legacy CVD-as-NCD artifacts
-- =============================================================================
-- Per the official BMA disease-criteria guideline (2026-05-05):
-- /Users/dev/bma-med/medical-knowledge/disease-criteria-guideline.jpg
-- /Users/dev/bma-med/medical-knowledge/DISEASE-CRITERIA-BASELINE.md
--
-- CVD criterion = "ผล EKG ผิดปกติ" (abnormal EKG), single-axis screening.
-- Was historically modelled as a 4-axis NCD with c4 = Cholesterol ≥ 240.
-- The new pipeline lives in 258_mv_cvd.sql + 358_mv_cvd_screening_factors.sql.
--
-- This migration is IDEMPOTENT — safe to run on any environment, including
-- those where the legacy MVs were already dropped manually. It leaves the
-- WHO-risk `risk_cvd` BOOLEAN column on mv_visit_resolved alone (it still
-- feeds mv_summary_districts/zones for now; that's tracked separately as a
-- cross-cutting cleanup).
-- =============================================================================

BEGIN;

DROP MATERIALIZED VIEW IF EXISTS public.mv_cvd_classification CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_cvd_factors        CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_cvd_factors_district CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_cvd_factors_region   CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_cvd_region            CASCADE;

COMMIT;

-- =============================================================================
-- END migration 259
-- =============================================================================
