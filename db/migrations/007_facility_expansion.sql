-- ============================================================
-- Migration 007: Expand ref_facilities for GIS integration
-- Adds columns for address/telephone and creates a code mapping
-- table for the short facility codes used in screening CSVs.
-- ============================================================

-- Add new columns to ref_facilities
ALTER TABLE ref_facilities
  ADD COLUMN IF NOT EXISTS address TEXT,
  ADD COLUMN IF NOT EXISTS telephone VARCHAR(50),
  ADD COLUMN IF NOT EXISTS ct_id INTEGER,
  ADD COLUMN IF NOT EXISTS ct_name VARCHAR(100);

-- Mapping table: short screening codes → full facility codes
-- The screening CSVs use short codes like 'bkt', 'chr', 'cnt'
-- while the facility registry uses numeric hcodes.
CREATE TABLE IF NOT EXISTS ref_facility_code_map (
  short_code VARCHAR(10) PRIMARY KEY,
  hcode VARCHAR(10),
  name_th VARCHAR(200),
  notes TEXT
);

-- Known mappings from the screening data (11 active facilities)
-- These are ศูนย์บริการสาธารณสุข (public health centers) used for screening
INSERT INTO ref_facility_code_map (short_code, hcode, name_th, notes) VALUES
  ('cnt', '13655', 'ศบส.10 สุขุมวิท / เขตคลองเตย', 'Central area'),
  ('chr', '13648', 'ศบส.3 บางซื่อ / เขตจตุจักร', 'Chatuchak area'),
  ('bkt', '13653', 'ศบส.8 บุญรอด รุ่งเรือง / เขตบางกะปิ', 'Bangkapi area'),
  ('srt', '13646', 'ศบส.1 สะพานมอญ / เขตสาทร', 'Sathorn area'),
  ('wkr', '13665', 'ศบส.20 ป้อมปราบ / เขตวังทองหลาง', 'Wangthonglang area'),
  ('sir', '13651', 'ศบส.6 สโมสรวัฒนธรรมหญิง / เขตสีลม', 'Silom area'),
  ('rtp', '13654', 'ศบส.9 ประชาธิปไตย / เขตราชเทวี', 'Ratchathewi area'),
  ('dkk', '13661', 'ศบส.16 ลุมพินี / เขตดอนเมือง', 'Don Mueang area'),
  ('wch', '13665', 'ศบส.20 / เขตวัชรพล', 'Watchara area'),
  ('cjk', '13650', 'ศบส.5 จุฬาลงกรณ์ / เขตจตุจักร', 'Chatuchak sub-area'),
  ('tks', '13660', 'ศบส.15 ลาดพร้าว / เขตทุ่งครุ-สะพานสูง', 'Thung Kru area')
ON CONFLICT (short_code) DO NOTHING;

-- Index for facility queries
CREATE INDEX IF NOT EXISTS idx_ref_facilities_district ON ref_facilities(district_code);
CREATE INDEX IF NOT EXISTS idx_ref_facilities_zone ON ref_facilities(zone_code);
CREATE INDEX IF NOT EXISTS idx_ref_facilities_latlong ON ref_facilities(latitude, longitude)
  WHERE latitude IS NOT NULL AND longitude IS NOT NULL;
