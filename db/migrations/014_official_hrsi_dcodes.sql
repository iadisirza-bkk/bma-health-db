-- Migration 014: Switch all dcodes to official HRSI/BMA codes
-- (the same codes used by bangkok-districts.geojson on the frontend map).
--
-- BEFORE: backend used a non-official ad-hoc numbering for dcodes 1031..1050
--         that did NOT match the geojson the frontend renders. Result:
--         "วังทองหลาง's stats" (internal dcode 1041) were rendered on the
--         "หลักสี่" polygon (geojson 1041), and so on for 20 districts.
--
-- AFTER:  every dcode in every table matches the geojson / FACT MD. The
--         mapping is anchored on district NAME (the unambiguous identifier).
--         Names and zone assignments are unchanged — only the numeric dcode
--         attached to each district moves to its official value.
--
-- Identity: 30 of 50 dcodes are already correct (1001..1030). 20 dcodes
-- (1031..1050) are remapped according to _dcode_remap below.
--
-- Safety:
--   * Single transaction → rollback if anything fails
--   * FK constraints to ref_districts are dropped + recreated within tx
--   * Every UPDATE is gated on `internal_dc <> official_dc` to no-op the 30
--     identity rows (avoids needless WAL)
--   * Sanity checks raise EXCEPTION on row-count drift
--   * Materialized views are refreshed at the end (separate step in app code)

BEGIN;

-- ============================================================================
-- 1. The single hardcoded mapping (the only allowed hardcode per policy).
--    Derived ONCE from district name match: DB-seed-name → geojson dcode.
-- ============================================================================
CREATE TEMP TABLE _dcode_remap (
    internal_dc TEXT PRIMARY KEY,
    official_dc TEXT NOT NULL UNIQUE
) ON COMMIT DROP;

INSERT INTO _dcode_remap (internal_dc, official_dc) VALUES
    -- Identity (1001..1030): same in both systems
    ('1001','1001'),('1002','1002'),('1003','1003'),('1004','1004'),('1005','1005'),
    ('1006','1006'),('1007','1007'),('1008','1008'),('1009','1009'),('1010','1010'),
    ('1011','1011'),('1012','1012'),('1013','1013'),('1014','1014'),('1015','1015'),
    ('1016','1016'),('1017','1017'),('1018','1018'),('1019','1019'),('1020','1020'),
    ('1021','1021'),('1022','1022'),('1023','1023'),('1024','1024'),('1025','1025'),
    ('1026','1026'),('1027','1027'),('1028','1028'),('1029','1029'),('1030','1030'),
    -- Renumbered (1031..1050): internal → official (HRSI)
    ('1031','1035'),  -- จอมทอง
    ('1032','1036'),  -- ดอนเมือง
    ('1033','1037'),  -- ราชเทวี
    ('1034','1038'),  -- ลาดพร้าว
    ('1035','1039'),  -- วัฒนา
    ('1036','1040'),  -- บางแค
    ('1037','1041'),  -- หลักสี่
    ('1038','1042'),  -- สายไหม
    ('1039','1043'),  -- คันนายาว
    ('1040','1044'),  -- สะพานสูง
    ('1041','1045'),  -- วังทองหลาง
    ('1042','1046'),  -- คลองสามวา
    ('1043','1047'),  -- บางนา
    ('1044','1048'),  -- ทวีวัฒนา
    ('1045','1049'),  -- ทุ่งครุ
    ('1046','1050'),  -- บางบอน
    ('1047','1033'),  -- คลองเตย
    ('1048','1032'),  -- ประเวศ
    ('1049','1034'),  -- สวนหลวง
    ('1050','1031');  -- บางคอแหลม


-- ============================================================================
-- 2. Snapshot row counts so we can sanity-check at the end
-- ============================================================================
CREATE TEMP TABLE _row_counts_before ON COMMIT DROP AS
SELECT 'raw_vitalsigns'::text AS tbl,
       COUNT(*) AS total,
       COUNT(district_code) AS with_dcode FROM raw_vitalsigns
UNION ALL SELECT 'raw_homevisit',
       COUNT(*),
       COUNT(home_district) + COUNT(work_district) + COUNT(current_district)
       FROM raw_homevisit
UNION ALL SELECT 'ref_facility_districts', COUNT(*), COUNT(district_code)
       FROM ref_facility_districts
UNION ALL SELECT 'ref_districts', COUNT(*), COUNT(dcode) FROM ref_districts;


-- ============================================================================
-- 3. Drop FK constraints that point at ref_districts (we'll recreate them)
-- ============================================================================
ALTER TABLE ref_facility_districts
    DROP CONSTRAINT IF EXISTS ref_facility_districts_district_code_fkey;
ALTER TABLE ref_facilities
    DROP CONSTRAINT IF EXISTS ref_facilities_district_code_fkey;


-- ============================================================================
-- 4. Translate dcodes in raw_* and ref_* tables (no-op on identity rows)
-- ============================================================================

-- raw_vitalsigns.district_code (TEXT)
UPDATE raw_vitalsigns v
SET district_code = m.official_dc
FROM _dcode_remap m
WHERE v.district_code = m.internal_dc
  AND v.district_code <> m.official_dc;

-- raw_homevisit.home_district (INT)
UPDATE raw_homevisit h
SET home_district = m.official_dc::int
FROM _dcode_remap m
WHERE h.home_district = m.internal_dc::int
  AND h.home_district <> m.official_dc::int;

-- raw_homevisit.work_district (INT)
UPDATE raw_homevisit h
SET work_district = m.official_dc::int
FROM _dcode_remap m
WHERE h.work_district = m.internal_dc::int
  AND h.work_district <> m.official_dc::int;

-- raw_homevisit.current_district (INT)
UPDATE raw_homevisit h
SET current_district = m.official_dc::int
FROM _dcode_remap m
WHERE h.current_district = m.internal_dc::int
  AND h.current_district <> m.official_dc::int;

-- ref_facility_districts.district_code (TEXT)
UPDATE ref_facility_districts f
SET district_code = m.official_dc
FROM _dcode_remap m
WHERE f.district_code = m.internal_dc
  AND f.district_code <> m.official_dc;

-- ref_facilities.district_code (currently empty in dev, but cover the case)
UPDATE ref_facilities f
SET district_code = m.official_dc
FROM _dcode_remap m
WHERE f.district_code = m.internal_dc
  AND f.district_code <> m.official_dc;


-- ============================================================================
-- 5. Rebuild ref_districts with official codes (preserve names/zones/pop)
-- ============================================================================
CREATE TEMP TABLE _new_ref_districts ON COMMIT DROP AS
SELECT m.official_dc AS dcode, r.zone_code, r.name_th, r.name_en, r.population
FROM ref_districts r
JOIN _dcode_remap m ON r.dcode = m.internal_dc;

DO $$
DECLARE
    n INT;
BEGIN
    SELECT COUNT(*) INTO n FROM _new_ref_districts;
    IF n <> 50 THEN
        RAISE EXCEPTION 'Expected 50 districts after remap, got %', n;
    END IF;
END $$;

TRUNCATE ref_districts;
INSERT INTO ref_districts (dcode, zone_code, name_th, name_en, population)
SELECT dcode, zone_code, name_th, name_en, population FROM _new_ref_districts;


-- ============================================================================
-- 6. Recreate FK constraints
-- ============================================================================
ALTER TABLE ref_facility_districts
    ADD CONSTRAINT ref_facility_districts_district_code_fkey
        FOREIGN KEY (district_code) REFERENCES ref_districts(dcode);
ALTER TABLE ref_facilities
    ADD CONSTRAINT ref_facilities_district_code_fkey
        FOREIGN KEY (district_code) REFERENCES ref_districts(dcode);


-- ============================================================================
-- 7. Sanity checks: row counts are invariant
-- ============================================================================
DO $$
DECLARE
    rec RECORD;
    new_total BIGINT;
    new_with_dcode BIGINT;
BEGIN
    FOR rec IN SELECT * FROM _row_counts_before LOOP
        IF rec.tbl = 'raw_vitalsigns' THEN
            SELECT COUNT(*), COUNT(district_code)
            INTO new_total, new_with_dcode FROM raw_vitalsigns;
        ELSIF rec.tbl = 'raw_homevisit' THEN
            SELECT COUNT(*),
                   COUNT(home_district) + COUNT(work_district) + COUNT(current_district)
            INTO new_total, new_with_dcode FROM raw_homevisit;
        ELSIF rec.tbl = 'ref_facility_districts' THEN
            SELECT COUNT(*), COUNT(district_code)
            INTO new_total, new_with_dcode FROM ref_facility_districts;
        ELSIF rec.tbl = 'ref_districts' THEN
            SELECT COUNT(*), COUNT(dcode)
            INTO new_total, new_with_dcode FROM ref_districts;
        END IF;

        IF new_total <> rec.total THEN
            RAISE EXCEPTION 'Row count drift in %: before=%, after=%',
                rec.tbl, rec.total, new_total;
        END IF;
        IF new_with_dcode <> rec.with_dcode THEN
            RAISE EXCEPTION 'Dcode-populated count drift in %: before=%, after=%',
                rec.tbl, rec.with_dcode, new_with_dcode;
        END IF;
    END LOOP;
END $$;

COMMIT;

-- After commit, run: REFRESH MATERIALIZED VIEW CONCURRENTLY <each view>
-- (handled by application via refresh_all_summaries())
