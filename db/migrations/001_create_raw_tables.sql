-- Migration 001: Create reference tables and raw data tables
-- BMA Health Database

BEGIN;

-- ============================================================
-- Reference Tables
-- ============================================================

CREATE TABLE ref_health_zones (
  zone_code VARCHAR(2) PRIMARY KEY,
  name_th VARCHAR(50) NOT NULL,
  name_en VARCHAR(50) NOT NULL,
  facilitator VARCHAR(100),
  mentor TEXT,
  area_manager_count INTEGER
);

CREATE TABLE ref_districts (
  dcode VARCHAR(4) PRIMARY KEY,
  zone_code VARCHAR(2) REFERENCES ref_health_zones(zone_code),
  name_th VARCHAR(50) NOT NULL,
  name_en VARCHAR(50),
  population INTEGER
);

CREATE TABLE ref_facilities (
  code VARCHAR(10) PRIMARY KEY,
  name_th VARCHAR(100) NOT NULL,
  name_en VARCHAR(100),
  facility_type VARCHAR(20),
  zone_code VARCHAR(2) REFERENCES ref_health_zones(zone_code),
  district_code VARCHAR(4) REFERENCES ref_districts(dcode),
  latitude DECIMAL(10,7),
  longitude DECIMAL(10,7)
);

-- ============================================================
-- Raw Tables
-- ============================================================

-- 1. raw_patients (from pt.csv)
CREATE TABLE raw_patients (
  id BIGSERIAL PRIMARY KEY,
  idcard_hash VARCHAR(64) UNIQUE,
  notype SMALLINT,
  pname SMALLINT,
  sex SMALLINT,
  birth_year SMALLINT,
  age_group VARCHAR(10),
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- 2. raw_visits (from pthistory.csv)
CREATE TABLE raw_visits (
  id BIGSERIAL PRIMARY KEY,
  patient_id BIGINT REFERENCES raw_patients(id),
  visit_date DATE,
  facility_code VARCHAR(10),
  religion SMALLINT,
  lgbtq SMALLINT,
  cancel_status SMALLINT,
  staff_code VARCHAR(20),
  created_at TIMESTAMPTZ DEFAULT now()
);

-- 3. raw_vitalsigns (from vitalsignslf.csv)
CREATE TABLE raw_vitalsigns (
  id BIGSERIAL PRIMARY KEY,
  patient_id BIGINT REFERENCES raw_patients(id),
  visit_date TIMESTAMPTZ,
  facility_code VARCHAR(10),
  sbp SMALLINT,
  dbp SMALLINT,
  fasting_glucose DECIMAL(6,1),
  post_glucose DECIMAL(6,1),
  height_cm DECIMAL(5,1),
  weight_kg DECIMAL(5,1),
  waist_cm DECIMAL(5,1),
  pulse_rate SMALLINT,
  smoking SMALLINT,
  alcohol SMALLINT,
  chest_xray SMALLINT,
  ekg SMALLINT,
  vision SMALLINT,
  dr_screening SMALLINT,
  depression_2q_1 SMALLINT,
  depression_2q_2 SMALLINT,
  phq9_q1 SMALLINT,
  phq9_q2 SMALLINT,
  phq9_q3 SMALLINT,
  phq9_q4 SMALLINT,
  phq9_q5 SMALLINT,
  phq9_q6 SMALLINT,
  phq9_q7 SMALLINT,
  phq9_q8 SMALLINT,
  phq9_q9 SMALLINT,
  st5_q1 SMALLINT,
  st5_q2 SMALLINT,
  st5_q3 SMALLINT,
  st5_q4 SMALLINT,
  st5_q5 SMALLINT,
  screening_result SMALLINT,
  risk_dm BOOLEAN,
  risk_hpt BOOLEAN,
  risk_cvd BOOLEAN,
  risk_bmi BOOLEAN,
  found_dm BOOLEAN,
  found_hpt BOOLEAN,
  found_cvd BOOLEAN,
  found_stroke BOOLEAN,
  found_obesity BOOLEAN,
  found_dyslipidemia BOOLEAN,
  found_other BOOLEAN,
  family_dm SMALLINT,
  district_code VARCHAR(4),
  location_code VARCHAR(10),
  referral_type VARCHAR(20),
  stress_management SMALLINT[],
  cancel_status SMALLINT
);

-- 4. raw_homevisit (from homevisit.csv)
CREATE TABLE raw_homevisit (
  id BIGSERIAL PRIMARY KEY,
  patient_id BIGINT REFERENCES raw_patients(id),
  visit_date TIMESTAMPTZ,
  facility_code VARCHAR(10),
  self_care SMALLINT,
  disability_types SMALLINT[],
  education SMALLINT,
  occupation SMALLINT,
  home_province INTEGER,
  home_district INTEGER,
  home_subdistrict INTEGER,
  home_type SMALLINT,
  health_privilege SMALLINT,
  current_province INTEGER,
  current_district INTEGER,
  work_district INTEGER,
  work_type SMALLINT,
  work_journey SMALLINT,
  health_facility_used SMALLINT,
  service_requests SMALLINT[],
  workshop_willing SMALLINT,
  cancel_status SMALLINT
);

-- 5. raw_homehealth (from homehealth.csv)
CREATE TABLE raw_homehealth (
  id BIGSERIAL PRIMARY KEY,
  patient_id BIGINT REFERENCES raw_patients(id),
  visit_date TIMESTAMPTZ,
  facility_code VARCHAR(10),
  has_chronic SMALLINT,
  history_dm SMALLINT,
  history_hpt SMALLINT,
  history_stroke SMALLINT,
  history_dyslipidemia SMALLINT,
  history_heart SMALLINT,
  history_kidney SMALLINT,
  dm_treatment SMALLINT,
  hpt_treatment SMALLINT,
  dyslipidemia_treatment SMALLINT,
  heart_treatment SMALLINT,
  kidney_treatment SMALLINT,
  stroke_treatment SMALLINT,
  parent_history SMALLINT,
  parent_dm BOOLEAN,
  parent_kidney BOOLEAN,
  parent_stroke BOOLEAN,
  parent_hpt BOOLEAN,
  parent_heart_attack BOOLEAN,
  parent_gout BOOLEAN,
  parent_emphysema BOOLEAN,
  exercise SMALLINT,
  food_preference_sweet BOOLEAN,
  food_preference_salty BOOLEAN,
  food_preference_fatty BOOLEAN,
  food_fried_freq SMALLINT,
  drink_sugar_freq SMALLINT,
  instant_noodle_freq SMALLINT,
  allergy_food SMALLINT,
  allergy_medicine SMALLINT,
  covid_history SMALLINT,
  vaccine_covid SMALLINT,
  vaccine_influenza SMALLINT,
  want_hiv_test SMALLINT,
  cancel_status SMALLINT
);

-- 6. raw_lab_results (from labhealth.csv)
CREATE TABLE raw_lab_results (
  id BIGSERIAL PRIMARY KEY,
  patient_id BIGINT REFERENCES raw_patients(id),
  visit_date DATE,
  facility_code VARCHAR(10),
  privilege SMALLINT,
  cbc_result SMALLINT,
  wbc INTEGER,
  rbc INTEGER,
  hemoglobin DECIMAL(5,1),
  hematocrit DECIMAL(5,1),
  mcv DECIMAL(5,1),
  platelet INTEGER,
  blood_sugar_type SMALLINT,
  blood_sugar_result SMALLINT,
  dtx DECIMAL(6,1),
  blood_sugar DECIMAL(6,1),
  fbs DECIMAL(6,1),
  urine_result SMALLINT,
  urine_wbc VARCHAR(10),
  urine_rbc VARCHAR(10),
  urine_protein VARCHAR(20),
  cholesterol_type SMALLINT,
  cholesterol_result SMALLINT,
  cholesterol DECIMAL(6,1),
  triglyceride DECIMAL(6,1),
  hdl DECIMAL(6,1),
  ldl DECIMAL(6,1),
  liver_result SMALLINT,
  sgot DECIMAL(6,1),
  sgpt DECIMAL(6,1),
  alk_phosphatase DECIMAL(6,1),
  uric_acid_result SMALLINT,
  uric_acid DECIMAL(5,2),
  cervical_cancer_result SMALLINT,
  hpv VARCHAR(20),
  colorectal_result SMALLINT,
  fit_test VARCHAR(20),
  creatinine DECIMAL(5,2),
  egfr DECIMAL(6,2),
  bun DECIMAL(5,1),
  cancel_status SMALLINT
);

-- 7. raw_lab_extended (from labhealthext.csv)
CREATE TABLE raw_lab_extended (
  id BIGSERIAL PRIMARY KEY,
  patient_id BIGINT REFERENCES raw_patients(id),
  visit_date DATE,
  facility_code VARCHAR(10),
  respiratory_cough SMALLINT,
  dyspnea SMALLINT,
  chest_tight SMALLINT,
  breathing SMALLINT,
  hearing_test SMALLINT,
  pterygium_right SMALLINT,
  pterygium_left SMALLINT,
  pain_head BOOLEAN,
  pain_neck BOOLEAN,
  pain_shoulder BOOLEAN,
  pain_upper_back BOOLEAN,
  pain_elbow BOOLEAN,
  pain_lower_back BOOLEAN,
  pain_wrist BOOLEAN,
  pain_hip BOOLEAN,
  pain_knee BOOLEAN,
  pain_ankle BOOLEAN,
  symptom_neck_radiating BOOLEAN,
  symptom_hand_numbness BOOLEAN,
  symptom_back_radiating BOOLEAN,
  symptom_heel_pain BOOLEAN,
  cancel_status SMALLINT
);

-- ============================================================
-- Indexes
-- ============================================================

-- Foreign key indexes (patient_id)
CREATE INDEX idx_raw_visits_patient_id ON raw_visits(patient_id);
CREATE INDEX idx_raw_vitalsigns_patient_id ON raw_vitalsigns(patient_id);
CREATE INDEX idx_raw_homevisit_patient_id ON raw_homevisit(patient_id);
CREATE INDEX idx_raw_homehealth_patient_id ON raw_homehealth(patient_id);
CREATE INDEX idx_raw_lab_results_patient_id ON raw_lab_results(patient_id);
CREATE INDEX idx_raw_lab_extended_patient_id ON raw_lab_extended(patient_id);

-- Visit date indexes
CREATE INDEX idx_raw_visits_visit_date ON raw_visits(visit_date);
CREATE INDEX idx_raw_vitalsigns_visit_date ON raw_vitalsigns(visit_date);
CREATE INDEX idx_raw_homevisit_visit_date ON raw_homevisit(visit_date);
CREATE INDEX idx_raw_homehealth_visit_date ON raw_homehealth(visit_date);
CREATE INDEX idx_raw_lab_results_visit_date ON raw_lab_results(visit_date);
CREATE INDEX idx_raw_lab_extended_visit_date ON raw_lab_extended(visit_date);

-- District code indexes
CREATE INDEX idx_raw_vitalsigns_district_code ON raw_vitalsigns(district_code);
CREATE INDEX idx_ref_districts_zone_code ON ref_districts(zone_code);

-- Cancel status indexes
CREATE INDEX idx_raw_visits_cancel_status ON raw_visits(cancel_status);
CREATE INDEX idx_raw_vitalsigns_cancel_status ON raw_vitalsigns(cancel_status);
CREATE INDEX idx_raw_homevisit_cancel_status ON raw_homevisit(cancel_status);
CREATE INDEX idx_raw_homehealth_cancel_status ON raw_homehealth(cancel_status);
CREATE INDEX idx_raw_lab_results_cancel_status ON raw_lab_results(cancel_status);
CREATE INDEX idx_raw_lab_extended_cancel_status ON raw_lab_extended(cancel_status);

-- Facility code indexes
CREATE INDEX idx_raw_visits_facility_code ON raw_visits(facility_code);
CREATE INDEX idx_raw_vitalsigns_facility_code ON raw_vitalsigns(facility_code);
CREATE INDEX idx_raw_homevisit_facility_code ON raw_homevisit(facility_code);
CREATE INDEX idx_raw_homehealth_facility_code ON raw_homehealth(facility_code);
CREATE INDEX idx_raw_lab_results_facility_code ON raw_lab_results(facility_code);
CREATE INDEX idx_raw_lab_extended_facility_code ON raw_lab_extended(facility_code);

COMMIT;
