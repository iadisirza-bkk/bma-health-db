-- =============================================================================
-- Seed: All 77 Thai provinces (full ราชบัณฑิตฯ / ISO 3166-2:TH list)
--
-- Background:
--   private.geo_province previously held only 13 rows (BKK + a handful of
--   neighbours seeded in 103_seed_geography.sql). When the frontend renders the
--   "ตจว" (other-province) breakdown, missing province codes display as "?".
--
-- This migration:
--   * Inserts all 77 provinces using the official 2-digit MOI province codes.
--   * Updates existing rows to use a consistent 6-region classification
--     (Central / East / West / North / Northeast / South) so the
--     headline-by-region grouping is sane.
--   * Idempotent via ON CONFLICT DO UPDATE.
--
-- Region classification follows the standard 6-region MOI scheme:
--   Central:   22 (Bangkok + lower-Chao Phraya basin)
--   East:       7 (eastern seaboard)
--   West:       5 (Tenasserim foothills)
--   North:      9 (northern highlands)
--   Northeast: 20 (Khorat plateau / Isan)
--   South:     14 (peninsular)
--   ---------
--   Total:     77
-- =============================================================================

INSERT INTO private.geo_province (province_code, name_th, name_en, region) VALUES
  -- Central plain (22) ----------------------------------------------------
  ('10', 'กรุงเทพมหานคร',     'Bangkok',                  'Central'),
  ('11', 'สมุทรปราการ',       'Samut Prakan',             'Central'),
  ('12', 'นนทบุรี',            'Nonthaburi',               'Central'),
  ('13', 'ปทุมธานี',           'Pathum Thani',             'Central'),
  ('14', 'พระนครศรีอยุธยา',    'Phra Nakhon Si Ayutthaya', 'Central'),
  ('15', 'อ่างทอง',            'Ang Thong',                'Central'),
  ('16', 'ลพบุรี',             'Lopburi',                  'Central'),
  ('17', 'สิงห์บุรี',           'Sing Buri',                'Central'),
  ('18', 'ชัยนาท',             'Chai Nat',                 'Central'),
  ('19', 'สระบุรี',            'Saraburi',                 'Central'),
  -- East (7) --------------------------------------------------------------
  ('20', 'ชลบุรี',             'Chonburi',                 'East'),
  ('21', 'ระยอง',             'Rayong',                   'East'),
  ('22', 'จันทบุรี',           'Chanthaburi',              'East'),
  ('23', 'ตราด',               'Trat',                     'East'),
  ('24', 'ฉะเชิงเทรา',          'Chachoengsao',             'East'),
  ('25', 'ปราจีนบุรี',          'Prachinburi',              'East'),
  ('26', 'นครนายก',            'Nakhon Nayok',             'Central'),
  ('27', 'สระแก้ว',            'Sa Kaeo',                  'East'),
  -- Northeast / Isan (20) -------------------------------------------------
  ('30', 'นครราชสีมา',         'Nakhon Ratchasima',        'Northeast'),
  ('31', 'บุรีรัมย์',           'Buri Ram',                 'Northeast'),
  ('32', 'สุรินทร์',            'Surin',                    'Northeast'),
  ('33', 'ศรีสะเกษ',           'Sisaket',                  'Northeast'),
  ('34', 'อุบลราชธานี',         'Ubon Ratchathani',         'Northeast'),
  ('35', 'ยโสธร',              'Yasothon',                 'Northeast'),
  ('36', 'ชัยภูมิ',             'Chaiyaphum',               'Northeast'),
  ('37', 'อำนาจเจริญ',          'Amnat Charoen',            'Northeast'),
  ('38', 'บึงกาฬ',              'Bueng Kan',                'Northeast'),
  ('39', 'หนองบัวลำภู',          'Nong Bua Lam Phu',         'Northeast'),
  ('40', 'ขอนแก่น',             'Khon Kaen',                'Northeast'),
  ('41', 'อุดรธานี',            'Udon Thani',               'Northeast'),
  ('42', 'เลย',                 'Loei',                     'Northeast'),
  ('43', 'หนองคาย',             'Nong Khai',                'Northeast'),
  ('44', 'มหาสารคาม',           'Maha Sarakham',            'Northeast'),
  ('45', 'ร้อยเอ็ด',            'Roi Et',                   'Northeast'),
  ('46', 'กาฬสินธุ์',           'Kalasin',                  'Northeast'),
  ('47', 'สกลนคร',              'Sakon Nakhon',             'Northeast'),
  ('48', 'นครพนม',              'Nakhon Phanom',            'Northeast'),
  ('49', 'มุกดาหาร',             'Mukdahan',                 'Northeast'),
  -- North (9) -------------------------------------------------------------
  ('50', 'เชียงใหม่',           'Chiang Mai',               'North'),
  ('51', 'ลำพูน',               'Lamphun',                  'North'),
  ('52', 'ลำปาง',               'Lampang',                  'North'),
  ('53', 'อุตรดิตถ์',           'Uttaradit',                'North'),
  ('54', 'แพร่',                 'Phrae',                    'North'),
  ('55', 'น่าน',                 'Nan',                      'North'),
  ('56', 'พะเยา',                'Phayao',                   'North'),
  ('57', 'เชียงราย',             'Chiang Rai',               'North'),
  ('58', 'แม่ฮ่องสอน',           'Mae Hong Son',             'North'),
  -- Lower north / upper Central (60-67) -----------------------------------
  ('60', 'นครสวรรค์',            'Nakhon Sawan',             'Central'),
  ('61', 'อุทัยธานี',             'Uthai Thani',              'Central'),
  ('62', 'กำแพงเพชร',            'Kamphaeng Phet',           'Central'),
  ('63', 'ตาก',                   'Tak',                      'West'),
  ('64', 'สุโขทัย',               'Sukhothai',                'Central'),
  ('65', 'พิษณุโลก',              'Phitsanulok',              'Central'),
  ('66', 'พิจิตร',                'Phichit',                  'Central'),
  ('67', 'เพชรบูรณ์',             'Phetchabun',               'Central'),
  -- West (5) and lower-Central (70-77) ------------------------------------
  ('70', 'ราชบุรี',               'Ratchaburi',               'West'),
  ('71', 'กาญจนบุรี',             'Kanchanaburi',             'West'),
  ('72', 'สุพรรณบุรี',             'Suphan Buri',              'Central'),
  ('73', 'นครปฐม',                 'Nakhon Pathom',            'Central'),
  ('74', 'สมุทรสาคร',              'Samut Sakhon',             'Central'),
  ('75', 'สมุทรสงคราม',            'Samut Songkhram',          'Central'),
  ('76', 'เพชรบุรี',               'Phetchaburi',              'West'),
  ('77', 'ประจวบคีรีขันธ์',         'Prachuap Khiri Khan',      'West'),
  -- South (14) ------------------------------------------------------------
  ('80', 'นครศรีธรรมราช',          'Nakhon Si Thammarat',      'South'),
  ('81', 'กระบี่',                 'Krabi',                    'South'),
  ('82', 'พังงา',                  'Phangnga',                 'South'),
  ('83', 'ภูเก็ต',                 'Phuket',                   'South'),
  ('84', 'สุราษฎร์ธานี',            'Surat Thani',              'South'),
  ('85', 'ระนอง',                  'Ranong',                   'South'),
  ('86', 'ชุมพร',                  'Chumphon',                 'South'),
  ('90', 'สงขลา',                  'Songkhla',                 'South'),
  ('91', 'สตูล',                    'Satun',                    'South'),
  ('92', 'ตรัง',                    'Trang',                    'South'),
  ('93', 'พัทลุง',                  'Phatthalung',              'South'),
  ('94', 'ปัตตานี',                 'Pattani',                  'South'),
  ('95', 'ยะลา',                    'Yala',                     'South'),
  ('96', 'นราธิวาส',                'Narathiwat',               'South')
ON CONFLICT (province_code) DO UPDATE
SET name_th = EXCLUDED.name_th,
    name_en = EXCLUDED.name_en,
    region  = EXCLUDED.region;

-- Sanity check: this migration should leave exactly 77 rows.
DO $$
DECLARE
  n integer;
BEGIN
  SELECT COUNT(*) INTO n FROM private.geo_province;
  IF n <> 77 THEN
    RAISE EXCEPTION 'geo_province row count = %, expected 77', n;
  END IF;
END $$;
