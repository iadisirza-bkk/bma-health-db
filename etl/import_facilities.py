"""Stub — module archived 2026-05-01.

Original facility importer wrote to `private.facility` (now-dropped schema).
Replaced by the bma-med pipeline (`/Users/dev/bma-med/`).

The original file is preserved at:
    etl/_archived_2026-05-01/import_facilities.py

To restore:
    mv etl/_archived_2026-05-01/import_facilities.py etl/

Loading this module raises ImportError so callers (api/admin.py
`_ensure_facilities_seeded`) fail loudly instead of silently no-op'ing.
"""
raise ImportError(
    "etl.import_facilities archived 2026-05-01; "
    "see etl/_archived_2026-05-01/. Replaced by /Users/dev/bma-med/ pipeline."
)
