# Analytics Privilege Layer

This FastAPI app is the only service that — in S2-S4 — will hold the DB role
grant authorizing row-level reads against `bma_med.*` fact and dim tables.
Every response is aggregate-shaped; per-patient fields are forbidden.

## Current status: S1 = stubs only

All endpoints return HTTP 501 with a typed JSON envelope (`status`,
`planned_in_sprint`, `spec_url`). The contract is frozen so the frontend can
integrate now while the privilege wiring is built.

## Modules

- **Module A — `routers/contingency.py` (S2):** `POST /analytics/contingency`
  performs 2-way cross-tabulation and a chi-squared test, with optional
  `group_by` / `adjust_for`.
- **Module B — `routers/regression.py` (S3):** `POST /analytics/regression`
  runs logistic / linear regression; `POST /analytics/odds_ratio` covers the
  meeting-style "OR across three variables" use case.
- **Module C — `routers/economics.py` (S4):** `POST /economics/cost-per-positive`,
  `POST /economics/drop-the-test`, `POST /economics/icer` — health-economics
  outputs for the MSD audience.

## Privacy invariants

1. No `pid` / `patient_id` / `birthdate` / contact fields ever appear in any
   response — enforced via `bma_med.security.k_anon.assert_no_individual_fields`
   on any `rows` payload before serialization.
2. Every call (including S1 stubs) emits a structured `audit_event`.
3. K-anonymity threshold (default 5) will be applied to every aggregate row in
   S2+ via `k_anon_filter`.
