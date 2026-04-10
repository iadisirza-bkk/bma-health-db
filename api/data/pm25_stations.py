"""
PM2.5 reference data for Bangkok.

ArcGIS returns one reading per district (50 records), keyed by Thai district
name in the `district` field (e.g. "เขตดินแดง").  This module provides:
  - Thai PM2.5 / WHO standards
  - AQI calculation from PM2.5 concentration
  - District name extraction from ArcGIS format
"""
from __future__ import annotations

import math

# Thai NAAQS daily PM2.5 standard (ug/m3)
STANDARD_TH = 37.5
# WHO 2021 annual guideline (ug/m3)
STANDARD_WHO = 15.0


# -- AQI calculation from PM2.5 (US EPA breakpoints) -----------------------
# Used because ArcGIS does not return AQI — only raw pm2_5 concentration.

_AQI_BREAKPOINTS = [
    # (pm25_lo, pm25_hi, aqi_lo, aqi_hi)
    (0.0, 12.0, 0, 50),
    (12.1, 35.4, 51, 100),
    (35.5, 55.4, 101, 150),
    (55.5, 150.4, 151, 200),
    (150.5, 250.4, 201, 300),
    (250.5, 350.4, 301, 400),
    (350.5, 500.4, 401, 500),
]


def pm25_to_aqi(pm25: float | None) -> int | None:
    """Convert PM2.5 concentration (ug/m3) to AQI using US EPA breakpoints."""
    if pm25 is None or math.isnan(pm25) or pm25 < 0:
        return None
    for pm_lo, pm_hi, aqi_lo, aqi_hi in _AQI_BREAKPOINTS:
        if pm25 <= pm_hi:
            aqi = ((aqi_hi - aqi_lo) / (pm_hi - pm_lo)) * (pm25 - pm_lo) + aqi_lo
            return round(aqi)
    return 500  # Above highest breakpoint


# -- District name matching -------------------------------------------------
# ArcGIS returns "เขตXXX" format.  We strip the "เขต" prefix to match
# against ref_districts.name_th which stores just "XXX".

def extract_district_name(arcgis_district: str | None) -> str | None:
    """Extract district name from ArcGIS station field to match ref_districts.name_th.

    Handles formats: "เขตXXX", "สถานีYYY เขตXXX", "XXX"
    """
    if not arcgis_district:
        return None
    name = arcgis_district.strip()
    # If "เขต" appears anywhere, extract the part after the LAST "เขต"
    # e.g. "สวนทวีวนารมย์ เขตทวีวัฒนา" → "ทวีวัฒนา"
    idx = name.rfind("เขต")
    if idx >= 0:
        name = name[idx + len("เขต"):]
    return name.strip() or None
