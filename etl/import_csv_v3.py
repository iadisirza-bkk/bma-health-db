"""Stub — module archived 2026-05-01.

Original v3 ETL has been replaced by the bma-med pipeline
(`/Users/dev/bma-med/clean.py` + `/Users/dev/bma-med/export.py`).

The original file is preserved at:
    etl/_archived_2026-05-01/import_csv_v3.py

To restore:
    mv etl/_archived_2026-05-01/import_csv_v3.py etl/

Loading this module raises ImportError so callers (api/admin.py lazy
loaders) fail loudly instead of silently no-op'ing.
"""
raise ImportError(
    "etl.import_csv_v3 archived 2026-05-01; "
    "see etl/_archived_2026-05-01/. Replaced by /Users/dev/bma-med/ pipeline."
)
