# Archived ETL scripts — 2026-05-01

These scripts wrote to the now-dropped `private.*` schema. They have been
archived after being replaced by the new bma-med pipeline.

## Why

The `private.*` schema (v3 ETL, EAV pattern) has been dropped. The new
pipeline lives at `/Users/dev/bma-med/` and handles ingestion, cleaning,
validation, and export through:

- `/Users/dev/bma-med/ingest.py`
- `/Users/dev/bma-med/clean.py`
- `/Users/dev/bma-med/validate.py`
- `/Users/dev/bma-med/export.py`

## When

Archived 2026-05-01.

## Files

| File | Original purpose |
|------|------------------|
| `import_csv_v3.py`             | Wrote `private.patient`, `private.visit_event`, `private.visit_measurement`, `private.lab_event`, `private.lab_measurement`, etc. |
| `import_facilities.py`         | Wrote `private.facility` (and `ref_facilities`). |
| `geocode_facilities.py`        | Updated `private.facility` lat/lng → `district_code` / `zone_code` via `private.geo_district`. |
| `bootstrap_variable_definitions.py` | Bootstrapped `private.variable_definition` and `private.variable_code_value` from `all_var.xlsx`. |

## How to restore

Each file's original path now holds a stub that raises `ImportError`. To
restore the working implementation:

```bash
mv etl/_archived_2026-05-01/<filename>.py etl/
```

(That overwrites the stub. Restore the stub from git history if you ever
need to roll forward again.)

## Cleanup reminder

**Delete this folder once the new bma-med pipeline has been live ≥ 30 days
(target: on or after 2026-05-31).** Verify production has been stable on
the new pipeline before pruning.
