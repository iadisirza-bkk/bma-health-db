-- Migration 008: Add computed columns (age, BMI) to raw tables
-- These are derived from existing data but stored for query performance.

-- =========================================================================
-- 1. Add age column to raw_patients (computed from birth_year)
-- =========================================================================

ALTER TABLE raw_patients ADD COLUMN IF NOT EXISTS age SMALLINT;

-- Backfill age from birth_year using 2026 as reference year
-- (matches CURRENT_YEAR default in etl/config.py)
UPDATE raw_patients
SET age = 2026 - birth_year
WHERE birth_year IS NOT NULL
  AND birth_year > 1900
  AND age IS NULL;

CREATE INDEX IF NOT EXISTS idx_raw_patients_age ON raw_patients (age);

-- =========================================================================
-- 2. Add BMI column to raw_vitalsigns (computed from height + weight)
-- =========================================================================

ALTER TABLE raw_vitalsigns ADD COLUMN IF NOT EXISTS bmi DECIMAL(5,2);

-- Backfill BMI: weight_kg / (height_cm / 100)^2
-- Only compute when both values are valid and positive
UPDATE raw_vitalsigns
SET bmi = ROUND(weight_kg / POWER(height_cm / 100.0, 2), 2)
WHERE height_cm > 0
  AND weight_kg > 0
  AND height_cm < 250   -- sanity: max 2.5m
  AND weight_kg < 300   -- sanity: max 300kg
  AND bmi IS NULL;

CREATE INDEX IF NOT EXISTS idx_raw_vitalsigns_bmi ON raw_vitalsigns (bmi);

-- =========================================================================
-- 3. Add referral_type mapping (extract from referral-related flags)
-- =========================================================================
-- referral_type column already exists in schema but was never populated.
-- Future ETL updates will populate it from CSREFER, RFPRVLG, RFOVER columns.
