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
    "1": {
        "name_th": "โซน 1",
        "name_en": "Zone 1",
        "facilitator": "โรงพยาบาลราชพิพัฒน์",
        "sub_facilitators": ["โรงพยาบาลหลวงพ่อทวีศักดิ์ ชุตินฺธโร อุทิศ"],
        "districts": ["ทวีวัฒนา", "ตลิ่งชัน", "บางแค", "ภาษีเจริญ", "หนองแขม", "บางบอน"],
        "dcodes": ["1048", "1019", "1040", "1022", "1023", "1050"],
        "area_manager_count": 15,
    },
    "2": {
        "name_th": "โซน 2",
        "name_en": "Zone 2",
        "facilitator": "โรงพยาบาลตากสิน",
        "sub_facilitators": ["โรงพยาบาลศิริราช", "โรงพยาบาลผู้สูงอายุบางขุนเทียน"],
        "districts": ["บางกอกน้อย", "บางกอกใหญ่", "คลองสาน", "ธนบุรี", "จอมทอง", "บางขุนเทียน"],
        "dcodes": ["1020", "1016", "1018", "1015", "1035", "1021"],
        "area_manager_count": 17,
    },
    "3": {
        "name_th": "โซน 3",
        "name_en": "Zone 3",
        "facilitator": "โรงพยาบาลเจริญกรุงประชารักษ์",
        "sub_facilitators": ["โรงพยาบาลเลิดสิน", "โรงพยาบาลจุฬาลงกรณ์"],
        "districts": ["ปทุมวัน", "บางรัก", "สาทร", "บางคอแหลม", "ยานนาวา", "ราษฎร์บูรณะ", "ทุ่งครุ", "คลองเตย", "วัฒนา", "พระโขนง"],
        "dcodes": ["1007", "1004", "1028", "1031", "1012", "1024", "1049", "1033", "1039", "1009"],
        "area_manager_count": 32,
    },
    "4": {
        "name_th": "โซน 4",
        "name_en": "Zone 4",
        "facilitator": "โรงพยาบาลวชิรพยาบาล",
        "sub_facilitators": [],
        "districts": ["บางซื่อ", "ดุสิต", "บางพลัด", "พระนคร"],
        "dcodes": ["1029", "1002", "1025", "1001"],
        "area_manager_count": 12,
    },
    "5": {
        "name_th": "โซน 5",
        "name_en": "Zone 5",
        "facilitator": "โรงพยาบาลกลาง",
        "sub_facilitators": ["โรงพยาบาลรามาธิบดี", "โรงพยาบาลราชวิถี"],
        "districts": ["พญาไท", "ราชเทวี", "ดินแดง", "ห้วยขวาง", "วังทองหลาง", "สัมพันธวงศ์", "ป้อมปราบศัตรูพ่าย"],
        "dcodes": ["1014", "1037", "1026", "1017", "1045", "1013", "1008"],
        "area_manager_count": 21,
    },
    "6": {
        "name_th": "โซน 6",
        "name_en": "Zone 6",
        "facilitator": "โรงพยาบาลกลาง",
        "sub_facilitators": ["โรงพยาบาลภูมิพลอดุลยเดช", "โรงพยาบาลมงกุฎวัฒนะ"],
        "districts": ["ดอนเมือง", "สายไหม", "หลักสี่", "บางเขน", "จตุจักร", "ลาดพร้าว"],
        "dcodes": ["1036", "1042", "1041", "1005", "1030", "1038"],
        "area_manager_count": 17,
    },
    "7": {
        "name_th": "โซน 7",
        "name_en": "Zone 7",
        "facilitator": "โรงพยาบาลสิรินธร",
        "sub_facilitators": ["โรงพยาบาลบางนา กรุงเทพมหานคร"],
        "districts": ["บางกะปิ", "สะพานสูง", "สวนหลวง", "ประเวศ", "บางนา", "ลาดกระบัง"],
        "dcodes": ["1006", "1044", "1034", "1032", "1047", "1011"],
        "area_manager_count": 18,
    },
    "8": {
        "name_th": "โซน 8",
        "name_en": "Zone 8",
        "facilitator": "โรงพยาบาลเวชการุณย์รัศมิ์",
        "sub_facilitators": ["โรงพยาบาลนพรัตนราชธานี"],
        "districts": ["คลองสามวา", "หนองจอก", "คันนายาว", "บึงกุ่ม", "มีนบุรี"],
        "dcodes": ["1046", "1003", "1043", "1027", "1010"],
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
