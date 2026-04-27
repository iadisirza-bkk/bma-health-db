-- =============================================================================
-- Migration 108 — Rename variable_key 'body_fat_pct' → 'found_obesity'
-- =============================================================================
--
-- Background
-- ----------
-- The all_var.xlsx specification labels the FAT column from vitalsignslf.csv
-- as "body fat percent", and our bootstrap_variable_definitions.py historically
-- mapped it to canonical key `body_fat_pct`. In reality the column is a
-- Boolean obesity flag (0 = ไม่ใช่, 1 = ใช่) — and the rest of the analytics
-- stack already expects a key named `found_obesity`:
--
--   • public.mv_visit_resolved derives its found_obesity column via
--       bool_or(value_boolean) FILTER (WHERE variable_key = 'found_obesity')
--   • public.summary_district_disease, public.mv_disease_district,
--     public.mv_summary_districts/zones/global all read found_obesity from
--     mv_visit_resolved.
--   • The disease registry in the frontend (DISEASE_REGISTRY) expects
--     `found_obesity`.
--
-- Net effect of the misnomer: 564,475 measurement rows (165 + 5,181 TRUE
-- across portal + app1) fed nothing useful — every consumer saw 0 visits
-- with obesity. Renaming the canonical key in private.variable_definition
-- joins the data back up to the rest of the system.
--
-- The companion code change is etl/bootstrap_variable_definitions.py — the
-- CANONICAL_RENAMES['FAT'] entry now resolves to 'found_obesity', and the
-- duplicate EXPLICIT_TYPES['body_fat_pct'] entry was removed (the existing
-- 'found_obesity' boolean entry covers it). After this migration, future
-- bootstrap runs will see the same key the data is already stored under.
--
-- See also:
--   • ETL-TYPE-FIX-DESIGN.md §7 ("Out of scope (follow-ups)") — calls this
--     out as a pure rename.
--   • ETL-TYPE-FIX-DESIGN.md §1.A — confirms `found_obesity` as a boolean.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- Step 1: rename the canonical key
-- -----------------------------------------------------------------------------
-- variable_definition is keyed by (source_code, csv_column_name) — the
-- variable_key is just a denormalised label. Two rows are affected:
--   id=503 (app1, FAT, vitalsignslf.csv)
--   id=106 (portal, FAT, vitalsignslf.csv)
-- private.visit_measurement references variable_definition by id, so the
-- existing 564k+ measurement rows automatically pick up the new key
-- without a data rewrite. No CASCADE needed.
-- -----------------------------------------------------------------------------

UPDATE private.variable_definition
   SET variable_key = 'found_obesity'
 WHERE variable_key = 'body_fat_pct';

-- Sanity check: the rename should have updated exactly the FAT mappings.
DO $$
DECLARE
  v_count INT;
BEGIN
  SELECT COUNT(*) INTO v_count
    FROM private.variable_definition
   WHERE variable_key = 'body_fat_pct';
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'Migration 108: body_fat_pct rows still exist after rename (count=%)', v_count;
  END IF;

  SELECT COUNT(*) INTO v_count
    FROM private.variable_definition
   WHERE variable_key = 'found_obesity';
  IF v_count = 0 THEN
    RAISE EXCEPTION 'Migration 108: no found_obesity rows after rename — refusing to commit';
  END IF;
END $$;

COMMIT;
