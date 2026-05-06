-- =============================================================================
-- Migration 214 — Add UNIQUE index to mv_dm_classification
-- =============================================================================
-- Background:
--   Mig 213 created two non-unique indexes on mv_dm_classification:
--     idx_mv_dm_classification_dist     (district_code)
--     idx_mv_dm_classification_pattern  (pattern)
--   `REFRESH MATERIALIZED VIEW CONCURRENTLY` requires a UNIQUE index, so the
--   auto-discovered refresh in `public.refresh_all_mvs()` errors out for this
--   MV with: "cannot refresh materialized view <...> concurrently".
--
--   `(district_code, pattern)` is naturally unique because the source query
--   GROUPs BY exactly those two columns. Promoting to a unique index lets
--   CONCURRENTLY work without changing data.
-- =============================================================================

BEGIN;

-- Drop the redundant non-unique index on district_code; the unique compound
-- index covers (district_code, …) lookups too.
DROP INDEX IF EXISTS public.idx_mv_dm_classification_dist;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_dm_classification
  ON public.mv_dm_classification (district_code, pattern);

-- Keep the pattern-only index for cross-district pattern queries.
-- (idx_mv_dm_classification_pattern remains as-is from mig 213.)

COMMIT;
