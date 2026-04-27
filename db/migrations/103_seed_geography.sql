-- =============================================================================
-- Seed: Bangkok geography + health zones (50 districts, 8 zones)
-- =============================================================================

-- Provinces (Thailand 77 — top 20 + BKK; full list bootstrapped from CSV later)
INSERT INTO private.geo_province (province_code, name_th, name_en, region) VALUES
  ('10', 'กรุงเทพมหานคร', 'Bangkok',         'Central'),
  ('11', 'สมุทรปราการ',  'Samut Prakan',     'Central'),
  ('12', 'นนทบุรี',       'Nonthaburi',       'Central'),
  ('13', 'ปทุมธานี',     'Pathum Thani',     'Central'),
  ('14', 'พระนครศรีอยุธยา','Phra Nakhon Si Ayutthaya','Central'),
  ('20', 'ชลบุรี',        'Chonburi',         'East'),
  ('30', 'นครราชสีมา',   'Nakhon Ratchasima','Northeast'),
  ('40', 'ขอนแก่น',       'Khon Kaen',        'Northeast'),
  ('50', 'เชียงใหม่',     'Chiang Mai',       'North'),
  ('60', 'นครสวรรค์',    'Nakhon Sawan',     'Central'),
  ('70', 'ราชบุรี',       'Ratchaburi',       'Central'),
  ('80', 'นครศรีธรรมราช','Nakhon Si Thammarat','South'),
  ('90', 'สงขลา',         'Songkhla',         'South')
ON CONFLICT (province_code) DO NOTHING;

-- 8 Bangkok health zones
INSERT INTO private.geo_health_zone (zone_code, name_th, name_en, facilitator) VALUES
  ('01','โซน 1 (ธนบุรีใต้)',     'Zone 1 (Thonburi South)',    'โรงพยาบาลราชพิพัฒน์'),
  ('02','โซน 2 (ธนบุรีเหนือ)',   'Zone 2 (Thonburi North)',    'โรงพยาบาลตากสิน'),
  ('03','โซน 3 (กรุงเทพใต้)',    'Zone 3 (Bangkok South)',     'โรงพยาบาลเจริญกรุงประชารักษ์'),
  ('04','โซน 4 (กรุงเทพกลาง)',   'Zone 4 (Bangkok Central)',   'โรงพยาบาลวชิรพยาบาล'),
  ('05','โซน 5 (กรุงเทพใน)',     'Zone 5 (Bangkok Inner)',     'โรงพยาบาลกลาง'),
  ('06','โซน 6 (กรุงเทพเหนือ)',  'Zone 6 (Bangkok North)',     'โรงพยาบาลกลาง'),
  ('07','โซน 7 (กรุงเทพตะวันออก)','Zone 7 (Bangkok East)',     'โรงพยาบาลสิรินธร'),
  ('08','โซน 8 (กรุงเทพตะวันออกเฉียงเหนือ)','Zone 8 (Bangkok NE)','โรงพยาบาลเวชการุณย์รัศมิ์')
ON CONFLICT (zone_code) DO NOTHING;

-- 50 BKK districts (per fact/Bangkok_Health_Zoning.md)
INSERT INTO private.geo_district (dcode, province_code, zone_code, name_th, name_en) VALUES
  ('1001','10','04','พระนคร',        'Phra Nakhon'),
  ('1002','10','04','ดุสิต',          'Dusit'),
  ('1003','10','08','หนองจอก',       'Nong Chok'),
  ('1004','10','03','บางรัก',         'Bang Rak'),
  ('1005','10','06','บางเขน',         'Bang Khen'),
  ('1006','10','07','บางกะปิ',        'Bang Kapi'),
  ('1007','10','03','ปทุมวัน',        'Pathum Wan'),
  ('1008','10','05','ป้อมปราบศัตรูพ่าย','Pom Prap Sattru Phai'),
  ('1009','10','03','พระโขนง',         'Phra Khanong'),
  ('1010','10','08','มีนบุรี',         'Min Buri'),
  ('1011','10','07','ลาดกระบัง',       'Lat Krabang'),
  ('1012','10','03','ยานนาวา',         'Yan Nawa'),
  ('1013','10','05','สัมพันธวงศ์',     'Samphanthawong'),
  ('1014','10','05','พญาไท',           'Phaya Thai'),
  ('1015','10','02','ธนบุรี',          'Thon Buri'),
  ('1016','10','02','บางกอกใหญ่',      'Bangkok Yai'),
  ('1017','10','05','ห้วยขวาง',        'Huai Khwang'),
  ('1018','10','02','คลองสาน',         'Khlong San'),
  ('1019','10','01','ตลิ่งชัน',        'Taling Chan'),
  ('1020','10','02','บางกอกน้อย',      'Bangkok Noi'),
  ('1021','10','02','บางขุนเทียน',     'Bang Khun Thian'),
  ('1022','10','01','ภาษีเจริญ',       'Phasi Charoen'),
  ('1023','10','01','หนองแขม',         'Nong Khaem'),
  ('1024','10','03','ราษฎร์บูรณะ',     'Rat Burana'),
  ('1025','10','04','บางพลัด',         'Bang Phlat'),
  ('1026','10','04','ดินแดง',          'Din Daeng'),
  ('1027','10','08','บึงกุ่ม',         'Bueng Kum'),
  ('1028','10','03','สาทร',            'Sathon'),
  ('1029','10','04','บางซื่อ',         'Bang Sue'),
  ('1030','10','05','จตุจักร',         'Chatuchak'),
  ('1031','10','03','บางคอแหลม',       'Bang Kho Laem'),
  ('1032','10','07','ประเวศ',          'Prawet'),
  ('1033','10','03','คลองเตย',         'Khlong Toei'),
  ('1034','10','05','สวนหลวง',         'Suan Luang'),
  ('1035','10','02','จอมทอง',          'Chom Thong'),
  ('1036','10','06','ดอนเมือง',        'Don Mueang'),
  ('1037','10','06','ราชเทวี',         'Ratchathewi'),
  ('1038','10','01','ลาดพร้าว',        'Lat Phrao'),
  ('1039','10','03','วัฒนา',           'Watthana'),
  ('1040','10','01','บางแค',           'Bang Khae'),
  ('1041','10','06','หลักสี่',         'Lak Si'),
  ('1042','10','06','สายไหม',          'Sai Mai'),
  ('1043','10','08','คันนายาว',         'Khan Na Yao'),
  ('1044','10','03','สะพานสูง',        'Saphan Sung'),
  ('1045','10','06','วังทองหลาง',      'Wang Thonglang'),
  ('1046','10','08','คลองสามวา',       'Khlong Sam Wa'),
  ('1047','10','03','บางนา',           'Bang Na'),
  ('1048','10','02','ทวีวัฒนา',        'Thawi Watthana'),
  ('1049','10','03','ทุ่งครุ',         'Thung Khru'),
  ('1050','10','01','บางบอน',          'Bang Bon')
ON CONFLICT (dcode) DO NOTHING;
