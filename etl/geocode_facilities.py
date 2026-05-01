"""Stub — module archived 2026-05-01.

Original geocoder updated `private.facility` (now-dropped schema) using
`private.geo_district`. Replaced by the bma-med pipeline
(`/Users/dev/bma-med/`).

The original file is preserved at:
    etl/_archived_2026-05-01/geocode_facilities.py

To restore:
    mv etl/_archived_2026-05-01/geocode_facilities.py etl/

Loading this module raises ImportError so callers (api/admin.py
`_ensure_facilities_seeded`) fail loudly instead of silently no-op'ing.
"""
raise ImportError(
    "etl.geocode_facilities archived 2026-05-01; "
    "see etl/_archived_2026-05-01/. Replaced by /Users/dev/bma-med/ pipeline."
)
