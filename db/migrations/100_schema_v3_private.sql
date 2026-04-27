-- =============================================================================
-- Schema v3 — Private (locked-down ETL-write tables)
-- =============================================================================
-- Design goals:
--   1. EAV pattern → flexible to add variables/sources without schema migration
--   2. Patient identity deduplicated across sources (1 person = 1 patient row)
--   3. Address as SCD Type 2 (slowly-changing) → no JOIN multiplication
--   4. Visit as central encounter; measurements link off visit
--   5. PARTITION BY HASH on big EAV tables for write throughput
--
-- Access:
--   - bma_etl_writer  : INSERT/UPDATE/SELECT
--   - bma_dba_admin   : ALL
--   - bma_api_reader  : NO ACCESS (reads public.mv_* only)
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS private;
CREATE SCHEMA IF NOT EXISTS public;     -- already exists; safe-IF-NOT-EXISTS

-- -----------------------------------------------------------------------------
-- ref geography (small, static)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS private.data_source (
  source_code     VARCHAR(20) PRIMARY KEY,
  name_th         VARCHAR(100) NOT NULL,
  name_en         VARCHAR(100),
  description     TEXT,
  added_at        TIMESTAMPTZ DEFAULT NOW(),
  active          BOOLEAN DEFAULT TRUE
);

INSERT INTO private.data_source (source_code, name_th, name_en) VALUES
  ('portal', 'BMA Portal', 'BMA Portal System'),
  ('app1',   'แอพ App1',   'Mobile App 1'),
  ('app2',   'แอพ App2',   'Mobile App 2')
ON CONFLICT (source_code) DO NOTHING;

CREATE TABLE IF NOT EXISTS private.geo_province (
  province_code   VARCHAR(2) PRIMARY KEY,
  name_th         VARCHAR(100) NOT NULL,
  name_en         VARCHAR(100),
  region          VARCHAR(20)
);

CREATE TABLE IF NOT EXISTS private.geo_district (
  dcode           VARCHAR(4) PRIMARY KEY,
  province_code   VARCHAR(2) REFERENCES private.geo_province(province_code),
  zone_code       VARCHAR(2),       -- BKK only; NULL for outside
  name_th         VARCHAR(100) NOT NULL,
  name_en         VARCHAR(100),
  population      INTEGER,
  is_bangkok      BOOLEAN GENERATED ALWAYS AS (province_code = '10') STORED
);
CREATE INDEX IF NOT EXISTS idx_geo_district_zone ON private.geo_district(zone_code);
CREATE INDEX IF NOT EXISTS idx_geo_district_bkk ON private.geo_district(is_bangkok);

CREATE TABLE IF NOT EXISTS private.geo_subdistrict (
  scode           VARCHAR(6) PRIMARY KEY,
  dcode           VARCHAR(4) REFERENCES private.geo_district(dcode),
  name_th         VARCHAR(100) NOT NULL,
  name_en         VARCHAR(100)
);

CREATE TABLE IF NOT EXISTS private.geo_health_zone (
  zone_code       VARCHAR(2) PRIMARY KEY,
  name_th         VARCHAR(100) NOT NULL,
  name_en         VARCHAR(100),
  facilitator     VARCHAR(150),
  mentor          TEXT,
  area_manager_count INTEGER
);

CREATE TABLE IF NOT EXISTS private.facility (
  code            VARCHAR(10) PRIMARY KEY,
  name_th         VARCHAR(150) NOT NULL,
  name_en         VARCHAR(150),
  facility_type   VARCHAR(40),
  district_code   VARCHAR(4) REFERENCES private.geo_district(dcode),
  zone_code       VARCHAR(2) REFERENCES private.geo_health_zone(zone_code),
  latitude        NUMERIC(10,7),
  longitude       NUMERIC(10,7),
  address         TEXT,
  telephone       VARCHAR(50),
  ct_id           INTEGER,
  ct_name         VARCHAR(100),
  active          BOOLEAN DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_facility_district ON private.facility(district_code);
CREATE INDEX IF NOT EXISTS idx_facility_zone ON private.facility(zone_code);
CREATE INDEX IF NOT EXISTS idx_facility_type ON private.facility(facility_type) WHERE active;

-- -----------------------------------------------------------------------------
-- variable_definition — THE catalog of every variable from every source
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS private.variable_definition (
  id              BIGSERIAL PRIMARY KEY,
  variable_key    VARCHAR(80) NOT NULL,        -- canonical: 'sbp', 'home_district', 'phq9_q1'
  csv_column_name VARCHAR(80) NOT NULL,        -- as in CSV: 'HBPN', 'HDISTRICT', 'SCN9Q1'
  source_code     VARCHAR(20) NOT NULL REFERENCES private.data_source(source_code),
  csv_file        VARCHAR(40),                  -- 'pt' | 'vital' | 'hv' | 'hh' | 'lab' | 'labext' | 'pthistory' | 'app2'
  domain          VARCHAR(40) NOT NULL,         -- 'identity'|'address'|'vital'|'lab'|'mental'|'lifestyle'|'symptom'|'audit'
  sub_domain      VARCHAR(80),                  -- finer category (matches Excel pivot)
  data_type       VARCHAR(20) NOT NULL CHECK (data_type IN ('number','text','boolean','code','date','array')),
  unit            VARCHAR(30),
  description_th  TEXT,
  description_en  TEXT,
  possible_values TEXT,                          -- raw doc string
  tier            SMALLINT DEFAULT 4 CHECK (tier BETWEEN 1 AND 4),
  valid_min       NUMERIC,
  valid_max       NUMERIC,
  is_pii          BOOLEAN DEFAULT FALSE,         -- if TRUE → never expose in MVs
  is_required     BOOLEAN DEFAULT FALSE,
  added_at        TIMESTAMPTZ DEFAULT NOW(),
  deprecated_at   TIMESTAMPTZ,
  notes           TEXT,
  UNIQUE (source_code, csv_column_name)
);
CREATE INDEX IF NOT EXISTS idx_var_def_key       ON private.variable_definition(variable_key);
CREATE INDEX IF NOT EXISTS idx_var_def_dom_tier  ON private.variable_definition(domain, tier);
CREATE INDEX IF NOT EXISTS idx_var_def_active    ON private.variable_definition(source_code) WHERE deprecated_at IS NULL;

-- For variables of data_type='code': enumerate the allowed code values
CREATE TABLE IF NOT EXISTS private.variable_code_value (
  variable_id     BIGINT NOT NULL REFERENCES private.variable_definition(id) ON DELETE CASCADE,
  code            VARCHAR(50) NOT NULL,
  label_th        VARCHAR(300) NOT NULL,
  label_en        VARCHAR(300),
  sort_order      SMALLINT,
  PRIMARY KEY (variable_id, code)
);

-- -----------------------------------------------------------------------------
-- Patient — canonical identity (deduplicated across sources)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS private.patient (
  id              BIGSERIAL PRIMARY KEY,
  idcard_hash     VARCHAR(64) UNIQUE NOT NULL,   -- SHA-256, never raw IDCARD
  sex_code        VARCHAR(2),                     -- '1'=male '2'=female (per BMA convention)
  birth_year      SMALLINT,
  birth_month     SMALLINT,
  primary_source  VARCHAR(20) REFERENCES private.data_source(source_code),
  first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  is_erased       BOOLEAN DEFAULT FALSE,
  erased_at       TIMESTAMPTZ,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_patient_active ON private.patient(sex_code, birth_year)
  WHERE NOT is_erased;

-- Per-source PID mapping (STACK mode supported via 1:N alias)
CREATE TABLE IF NOT EXISTS private.patient_alias (
  patient_id      BIGINT NOT NULL REFERENCES private.patient(id) ON DELETE CASCADE,
  source_code     VARCHAR(20) NOT NULL REFERENCES private.data_source(source_code),
  source_pid      VARCHAR(80) NOT NULL,           -- HN/PID in source CSV
  first_imported_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (patient_id, source_code),
  UNIQUE (source_code, source_pid)
);

-- -----------------------------------------------------------------------------
-- Patient address — SCD Type 2 (versioned)
-- -----------------------------------------------------------------------------

DO $$ BEGIN
  CREATE TYPE private.address_type AS ENUM ('home', 'current', 'work');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS private.patient_address (
  id              BIGSERIAL PRIMARY KEY,
  patient_id      BIGINT NOT NULL REFERENCES private.patient(id) ON DELETE CASCADE,
  address_type    private.address_type NOT NULL,
  province_code   VARCHAR(2),
  district_code   VARCHAR(4),
  subdistrict_code VARCHAR(6),
  effective_from  DATE NOT NULL,
  effective_to    DATE,                            -- NULL = current
  reported_by_visit_id BIGINT,                    -- FK to visit_event (added later)
  source_code     VARCHAR(20) REFERENCES private.data_source(source_code),
  created_at      TIMESTAMPTZ DEFAULT NOW()
);
-- Constraint: only ONE active row per (patient, address_type)
CREATE UNIQUE INDEX IF NOT EXISTS uq_patient_address_active
  ON private.patient_address (patient_id, address_type) WHERE effective_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_patient_address_district
  ON private.patient_address (district_code) WHERE effective_to IS NULL;

-- Slowly-changing patient attributes (last value wins; full audit via audit_log)
CREATE TABLE IF NOT EXISTS private.patient_attribute (
  patient_id      BIGINT NOT NULL REFERENCES private.patient(id) ON DELETE CASCADE,
  variable_id     BIGINT NOT NULL REFERENCES private.variable_definition(id),
  value_text      VARCHAR(500),
  value_number    NUMERIC,
  value_array     JSONB,
  last_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_source     VARCHAR(20) REFERENCES private.data_source(source_code),
  PRIMARY KEY (patient_id, variable_id)
);

-- Patient health history (1:N — chronic conditions, family history, allergies)
CREATE TABLE IF NOT EXISTS private.patient_chronic_history (
  patient_id      BIGINT REFERENCES private.patient(id) ON DELETE CASCADE,
  condition_code  VARCHAR(40) NOT NULL,           -- 'dm','hpt','cvd','stroke','dyslipidemia','kidney'
  diagnosed_year  SMALLINT,
  on_treatment    BOOLEAN,
  last_reported_at TIMESTAMPTZ DEFAULT NOW(),
  last_source     VARCHAR(20),
  PRIMARY KEY (patient_id, condition_code)
);

CREATE TABLE IF NOT EXISTS private.patient_family_history (
  patient_id      BIGINT REFERENCES private.patient(id) ON DELETE CASCADE,
  relation        VARCHAR(20) NOT NULL,           -- 'parent','sibling','grandparent'
  condition_code  VARCHAR(40) NOT NULL,
  PRIMARY KEY (patient_id, relation, condition_code)
);

CREATE TABLE IF NOT EXISTS private.patient_allergy (
  id              BIGSERIAL PRIMARY KEY,
  patient_id      BIGINT REFERENCES private.patient(id) ON DELETE CASCADE,
  allergy_type    VARCHAR(20) CHECK (allergy_type IN ('food','medicine','other')),
  description     VARCHAR(300),
  source_code     VARCHAR(20),
  reported_at     DATE
);

-- -----------------------------------------------------------------------------
-- Visit event — central encounter
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS private.visit_event (
  id              BIGSERIAL PRIMARY KEY,
  patient_id      BIGINT NOT NULL REFERENCES private.patient(id),
  source_code     VARCHAR(20) NOT NULL REFERENCES private.data_source(source_code),
  visit_date      DATE NOT NULL,
  visit_time      TIME,
  facility_code   VARCHAR(10) REFERENCES private.facility(code),
  cancel_status   SMALLINT DEFAULT 0,
  source_visit_id VARCHAR(80),                    -- original VST_ID/HN
  import_batch_id BIGINT,
  retry_group_id  INTEGER,                         -- 30-day dedup grouping
  is_dedup_kept   BOOLEAN DEFAULT TRUE,            -- FALSE = collapsed into another visit
  created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_visit_event
  ON private.visit_event (patient_id, source_code, visit_date)
  WHERE cancel_status = 0;
CREATE INDEX IF NOT EXISTS idx_visit_event_date
  ON private.visit_event (visit_date) WHERE cancel_status = 0;
CREATE INDEX IF NOT EXISTS idx_visit_event_source
  ON private.visit_event (source_code, visit_date) WHERE cancel_status = 0;
CREATE INDEX IF NOT EXISTS idx_visit_event_facility
  ON private.visit_event (facility_code, visit_date);
CREATE INDEX IF NOT EXISTS idx_visit_event_dedup
  ON private.visit_event (patient_id, source_code, retry_group_id);

-- Now we can wire patient_address.reported_by_visit_id FK
ALTER TABLE private.patient_address
  DROP CONSTRAINT IF EXISTS patient_address_visit_fk;
ALTER TABLE private.patient_address
  ADD CONSTRAINT patient_address_visit_fk
    FOREIGN KEY (reported_by_visit_id) REFERENCES private.visit_event(id) ON DELETE SET NULL;

-- -----------------------------------------------------------------------------
-- visit_measurement — THE EAV core
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS private.visit_measurement (
  visit_id        BIGINT NOT NULL,
  variable_id     BIGINT NOT NULL,
  -- Value stored in the slot matching variable_definition.data_type
  value_number    NUMERIC,
  value_text      VARCHAR(500),
  value_boolean   BOOLEAN,
  value_date      DATE,
  value_array     JSONB,
  is_computed     BOOLEAN DEFAULT FALSE,           -- TRUE = ETL-derived (not from source)
  source_value    VARCHAR(500),                    -- raw value from CSV (audit)
  PRIMARY KEY (visit_id, variable_id)
) PARTITION BY HASH (visit_id);

-- 16 hash partitions for write throughput
DO $$
DECLARE i INT;
BEGIN
  FOR i IN 0..15 LOOP
    EXECUTE format(
      'CREATE TABLE IF NOT EXISTS private.visit_measurement_p%s
         PARTITION OF private.visit_measurement
         FOR VALUES WITH (modulus 16, remainder %s)', i, i
    );
  END LOOP;
END $$;

-- Indexes on parent (will propagate to partitions in PG13+)
CREATE INDEX IF NOT EXISTS idx_vm_var_num
  ON private.visit_measurement (variable_id, value_number)
  WHERE value_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_vm_var_bool
  ON private.visit_measurement (variable_id)
  WHERE value_boolean = TRUE;
CREATE INDEX IF NOT EXISTS idx_vm_var_text
  ON private.visit_measurement (variable_id, value_text)
  WHERE value_text IS NOT NULL;

-- -----------------------------------------------------------------------------
-- Lab event + measurement (parallel structure)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS private.lab_event (
  id              BIGSERIAL PRIMARY KEY,
  patient_id      BIGINT NOT NULL REFERENCES private.patient(id),
  source_code     VARCHAR(20) NOT NULL REFERENCES private.data_source(source_code),
  visit_id        BIGINT REFERENCES private.visit_event(id),  -- nullable
  lab_date        DATE NOT NULL,
  facility_code   VARCHAR(10) REFERENCES private.facility(code),
  cancel_status   SMALLINT DEFAULT 0,
  source_lab_id   VARCHAR(80),
  privilege_code  VARCHAR(20),
  import_batch_id BIGINT,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_lab_event_patient ON private.lab_event(patient_id, lab_date);
CREATE INDEX IF NOT EXISTS idx_lab_event_visit ON private.lab_event(visit_id);

CREATE TABLE IF NOT EXISTS private.lab_measurement (
  lab_id          BIGINT NOT NULL,
  variable_id     BIGINT NOT NULL,
  value_number    NUMERIC,
  value_text      VARCHAR(500),
  value_boolean   BOOLEAN,
  out_of_range    BOOLEAN,
  is_computed     BOOLEAN DEFAULT FALSE,
  source_value    VARCHAR(500),
  PRIMARY KEY (lab_id, variable_id)
) PARTITION BY HASH (lab_id);

DO $$
DECLARE i INT;
BEGIN
  FOR i IN 0..15 LOOP
    EXECUTE format(
      'CREATE TABLE IF NOT EXISTS private.lab_measurement_p%s
         PARTITION OF private.lab_measurement
         FOR VALUES WITH (modulus 16, remainder %s)', i, i
    );
  END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS idx_lm_var_num
  ON private.lab_measurement (variable_id, value_number)
  WHERE value_number IS NOT NULL;

-- -----------------------------------------------------------------------------
-- Symptom / long-tail (Portal-extended exam)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS private.visit_pain (
  id              BIGSERIAL PRIMARY KEY,
  visit_id        BIGINT NOT NULL REFERENCES private.visit_event(id) ON DELETE CASCADE,
  body_part       VARCHAR(40) NOT NULL,           -- 'head','neck','shoulder','back_upper',...
  has_pain        BOOLEAN DEFAULT TRUE,
  severity        SMALLINT,                        -- 1..5 if recorded
  UNIQUE (visit_id, body_part)
);

CREATE TABLE IF NOT EXISTS private.visit_neurological (
  id              BIGSERIAL PRIMARY KEY,
  visit_id        BIGINT NOT NULL REFERENCES private.visit_event(id) ON DELETE CASCADE,
  warning_sign    VARCHAR(60) NOT NULL,
  present         BOOLEAN,
  UNIQUE (visit_id, warning_sign)
);

CREATE TABLE IF NOT EXISTS private.visit_respiratory (
  id              BIGSERIAL PRIMARY KEY,
  visit_id        BIGINT NOT NULL REFERENCES private.visit_event(id) ON DELETE CASCADE,
  symptom         VARCHAR(60) NOT NULL,
  present         BOOLEAN,
  severity        SMALLINT,
  UNIQUE (visit_id, symptom)
);

CREATE TABLE IF NOT EXISTS private.visit_recommendation (
  id              BIGSERIAL PRIMARY KEY,
  visit_id        BIGINT NOT NULL REFERENCES private.visit_event(id) ON DELETE CASCADE,
  category        VARCHAR(40),                    -- 'lifestyle','followup','referral','medication'
  recommendation_text TEXT,
  source_code     VARCHAR(20)
);

CREATE TABLE IF NOT EXISTS private.visit_referral (
  id              BIGSERIAL PRIMARY KEY,
  visit_id        BIGINT NOT NULL REFERENCES private.visit_event(id) ON DELETE CASCADE,
  referred_to_facility VARCHAR(10) REFERENCES private.facility(code),
  referral_reason TEXT,
  referral_status VARCHAR(20)                     -- 'pending','completed','declined'
);

-- -----------------------------------------------------------------------------
-- Audit + import tracking
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS private.import_batch (
  id              BIGSERIAL PRIMARY KEY,
  source_code     VARCHAR(20) REFERENCES private.data_source(source_code),
  filename        VARCHAR(300),
  csv_file_type   VARCHAR(40),                    -- 'pt'|'vital'|'hv'|'hh'|'lab'|'labext'|'pthistory'|'app2'
  uploaded_at     TIMESTAMPTZ DEFAULT NOW(),
  uploaded_by     VARCHAR(80),
  rows_parsed     INTEGER DEFAULT 0,
  rows_inserted   INTEGER DEFAULT 0,
  rows_skipped    INTEGER DEFAULT 0,
  status          VARCHAR(20) DEFAULT 'running', -- 'running'|'completed'|'error'
  error_message   TEXT,
  duration_ms     INTEGER,
  progress_pct    SMALLINT DEFAULT 0,
  progress_note   VARCHAR(200)
);
CREATE INDEX IF NOT EXISTS idx_import_batch_status ON private.import_batch(status, uploaded_at DESC);

CREATE TABLE IF NOT EXISTS private.audit_log (
  id              BIGSERIAL PRIMARY KEY,
  occurred_at     TIMESTAMPTZ DEFAULT NOW(),
  actor           VARCHAR(80),
  action          VARCHAR(60) NOT NULL,           -- 'import','erasure','manual_edit','login',...
  target_type     VARCHAR(40),
  target_id       BIGINT,
  details         JSONB
);
CREATE INDEX IF NOT EXISTS idx_audit_log_time ON private.audit_log(occurred_at DESC);

CREATE TABLE IF NOT EXISTS private.erasure_request (
  id              BIGSERIAL PRIMARY KEY,
  patient_id      BIGINT REFERENCES private.patient(id) ON DELETE SET NULL,
  idcard_hash     VARCHAR(64),                    -- preserved even after patient deleted
  requested_at    TIMESTAMPTZ DEFAULT NOW(),
  reason          TEXT,
  processed_at    TIMESTAMPTZ,
  processed_by    VARCHAR(80)
);

CREATE TABLE IF NOT EXISTS private.data_quality_issue (
  id              BIGSERIAL PRIMARY KEY,
  detected_at     TIMESTAMPTZ DEFAULT NOW(),
  source_code     VARCHAR(20),
  table_name      VARCHAR(60),
  issue_type      VARCHAR(60) NOT NULL,
  affected_rows   INTEGER,
  details         JSONB,
  resolved        BOOLEAN DEFAULT FALSE
);

-- -----------------------------------------------------------------------------
-- Maintenance helpers
-- -----------------------------------------------------------------------------

COMMENT ON SCHEMA private IS
  'Private schema — ETL-write only. NOT exposed to API. Frontend reads public.mv_* only.';

-- Done
