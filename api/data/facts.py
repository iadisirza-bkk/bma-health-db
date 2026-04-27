"""Single source of truth for Bangkok Health Zoning.

Based on: fact/Bangkok_Health_Zoning.md + fact/FACT_เอกสารแนบ_text.md
These are FACT files — always correct. All other zone mappings in the
codebase MUST import from here.
"""
from __future__ import annotations


# ──────────────────────────────────────────────────────────────────────
# 8 Health Zones — from FACT file (Bangkok_Health_Zoning.md)
# ──────────────────────────────────────────────────────────────────────

HEALTH_ZONES: dict[str, dict] = {
    # dcodes are the OFFICIAL HRSI / BMA codes — same numbering as
    # frontend/public/vectors/bangkok-districts.geojson. These MUST match
    # ref_districts.dcode in the database. Do NOT renumber by hand; if you
    # need a code for a new district, look it up in the geojson.
    # See migration 014 for the historical remap.
    "1": {
        "name_th": "โซน 1",
        "name_en": "Zone 1",
        "facilitator": "โรงพยาบาลราชพิพัฒน์",
        "sub_facilitators": ["โรงพยาบาลหลวงพ่อทวีศักดิ์ ชุตินฺธโร อุทิศ"],
        "districts": ["ตลิ่งชัน", "ภาษีเจริญ", "หนองแขม", "บางแค", "ทวีวัฒนา", "บางบอน"],
        "dcodes": ["1019", "1022", "1023", "1040", "1048", "1050"],
        "area_manager_count": 15,
    },
    "2": {
        "name_th": "โซน 2",
        "name_en": "Zone 2",
        "facilitator": "โรงพยาบาลตากสิน",
        "sub_facilitators": ["โรงพยาบาลศิริราช", "โรงพยาบาลผู้สูงอายุบางขุนเทียน"],
        "districts": ["ธนบุรี", "บางกอกใหญ่", "คลองสาน", "บางกอกน้อย", "บางขุนเทียน", "จอมทอง"],
        "dcodes": ["1015", "1016", "1018", "1020", "1021", "1035"],
        "area_manager_count": 17,
    },
    "3": {
        "name_th": "โซน 3",
        "name_en": "Zone 3",
        "facilitator": "โรงพยาบาลเจริญกรุงประชารักษ์",
        "sub_facilitators": ["โรงพยาบาลเลิดสิน", "โรงพยาบาลจุฬาลงกรณ์"],
        "districts": ["บางรัก", "ปทุมวัน", "พระโขนง", "ยานนาวา", "ราษฎร์บูรณะ", "สาทร", "วัฒนา", "ทุ่งครุ", "คลองเตย", "บางคอแหลม"],
        "dcodes": ["1004", "1007", "1009", "1012", "1024", "1028", "1031", "1033", "1039", "1049"],
        "area_manager_count": 32,
    },
    "4": {
        "name_th": "โซน 4",
        "name_en": "Zone 4",
        "facilitator": "โรงพยาบาลวชิรพยาบาล",
        "sub_facilitators": [],
        "districts": ["พระนคร", "ดุสิต", "บางพลัด", "บางซื่อ"],
        "dcodes": ["1001", "1002", "1025", "1029"],
        "area_manager_count": 12,
    },
    "5": {
        "name_th": "โซน 5",
        "name_en": "Zone 5",
        "facilitator": "โรงพยาบาลกลาง",
        "sub_facilitators": ["โรงพยาบาลรามาธิบดี", "โรงพยาบาลราชวิถี"],
        "districts": ["ป้อมปราบศัตรูพ่าย", "สัมพันธวงศ์", "พญาไท", "ห้วยขวาง", "ดินแดง", "ราชเทวี", "วังทองหลาง"],
        "dcodes": ["1008", "1013", "1014", "1017", "1026", "1037", "1045"],
        "area_manager_count": 21,
    },
    "6": {
        "name_th": "โซน 6",
        "name_en": "Zone 6",
        "facilitator": "โรงพยาบาลกลาง",
        "sub_facilitators": ["โรงพยาบาลภูมิพลอดุลยเดช", "โรงพยาบาลมงกุฎวัฒนะ"],
        "districts": ["บางเขน", "จตุจักร", "ดอนเมือง", "ลาดพร้าว", "หลักสี่", "สายไหม"],
        "dcodes": ["1005", "1030", "1036", "1038", "1041", "1042"],
        "area_manager_count": 17,
    },
    "7": {
        "name_th": "โซน 7",
        "name_en": "Zone 7",
        "facilitator": "โรงพยาบาลสิรินธร",
        "sub_facilitators": ["โรงพยาบาลบางนา กรุงเทพมหานคร"],
        "districts": ["บางกะปิ", "ลาดกระบัง", "สะพานสูง", "บางนา", "ประเวศ", "สวนหลวง"],
        "dcodes": ["1006", "1011", "1032", "1034", "1044", "1047"],
        "area_manager_count": 18,
    },
    "8": {
        "name_th": "โซน 8",
        "name_en": "Zone 8",
        "facilitator": "โรงพยาบาลเวชการุณย์รัศมิ์",
        "sub_facilitators": ["โรงพยาบาลนพรัตนราชธานี"],
        "districts": ["หนองจอก", "มีนบุรี", "บึงกุ่ม", "คันนายาว", "คลองสามวา"],
        "dcodes": ["1003", "1010", "1027", "1043", "1046"],
        "area_manager_count": 14,
    },
}


# ──────────────────────────────────────────────────────────────────────
# Derived lookups (computed once at import time)
# ──────────────────────────────────────────────────────────────────────

DCODE_TO_ZONE: dict[str, str] = {}
for _zc, _zd in HEALTH_ZONES.items():
    for _dc in _zd["dcodes"]:
        DCODE_TO_ZONE[_dc] = _zc

ZONE_NAMES_TH: dict[str, str] = {zc: zd["name_th"] for zc, zd in HEALTH_ZONES.items()}
ZONE_NAMES_EN: dict[str, str] = {zc: zd["name_en"] for zc, zd in HEALTH_ZONES.items()}
ZONE_FACILITATORS: dict[str, str] = {zc: zd["facilitator"] for zc, zd in HEALTH_ZONES.items()}

# Total: 50 districts across 8 zones
assert len(DCODE_TO_ZONE) == 50, f"Expected 50 districts, got {len(DCODE_TO_ZONE)}"
assert len(HEALTH_ZONES) == 8, f"Expected 8 zones, got {len(HEALTH_ZONES)}"
