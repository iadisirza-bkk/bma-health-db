# BMA Health DB — Security Model

This document describes the data-protection guarantees of the BMA Health
analytics platform, the threat model behind them, and the operational
practices required to keep them intact.

> Audience: anyone who reviews, deploys, or extends this codebase.
> If you are extending an API endpoint or migration, **read §3 (Defense
> Layers) before adding any new SQL that touches `bma_med.*`**.

---

## 1. Data Sensitivity Tiers

| Tier | Examples | Storage | API Visibility |
|---|---|---|---|
| **T0 — Identifiers** | citizen ID (IDCARD), `hn`, `pid`, `pid_encoded`, `pid_hash`, phone, email, line ID | `bma_med.patient` (encrypted/hashed only), never in `public.*` views | **Never returned.** Stripped by `_PII_COLUMNS` filter at the API boundary. |
| **T1 — Direct PII** | name (`fname`, `lname`, `efname`, `elname`), home address (`haddr`), `idline`, `discaretel` | source CSVs only — discarded at clean stage, never written to `bma_med.*` | **Never returned.** Listed in `_PII_COLUMNS` as defense-in-depth. |
| **T2 — Quasi-identifiers** | `birthdate`, exact `first_seen` / `last_seen`, district code | `bma_med.patient` (raw); `public.raw_patients` (bucketed) | Aggregated only, with k-anonymity threshold ≥ 5. Never returned at row level. |
| **T3 — Health observations** | lab values, BMI, BP, disease flags | `bma_med.{source}_{table}` rows joined by `patient_id` | Returned in aggregate by district / zone / subdistrict. |
| **T4 — Reference / structural** | district names, zone codes, hospital codes, screening targets | `ref_*` tables | Returned freely; no privacy implications. |

The fundamental rule: **T0 and T1 must never leave the database in
plaintext.** T2 must never appear at the row level outside k-anonymous
aggregates. T3 is publishable in aggregate. T4 is public.

---

## 2. Threat Model

### Adversaries we defend against

| Adversary | Capabilities | Defenses |
|---|---|---|
| **External (no creds)** | HTTP requests to `api-health.splash-wonderland.com`. | Cloudflare WAF + per-IP rate limit + `X-API-Key` middleware. CORS lockdown (no wildcard in production). |
| **External (leaked API key)** | Issues authenticated API requests as the public consumer. | `_PII_COLUMNS` filter strips T0/T1. K-anonymity strips small cells. Only public read endpoints are reachable; `/admin/*` requires session cookie + CSRF. |
| **Internal — analyst (read-only DB)** | `bma_med_reader` role: SELECT on `v_patient_deid`, no access to `bma_med.patient`. | Column-level GRANTs ensure `pid_encoded`, `pid_hash`, `birthdate` are not visible. |
| **Internal — clinician (full read)** | `bma_med_clinician`: SELECT on `bma_med.patient` including `pid_encoded`. | `pid_encoded` is HMAC-SHA256, irreversible without the secret. CHECK constraint enforces hex format. |
| **Internal — operator (admin)** | Holds `IDCARD_HASH_SECRET`, can run migrations. | Audit log records every patient-table write (with PII redacted post-501). All admin actions go through CSRF-protected endpoints with session auth. |
| **Internal — DB admin (postgres role)** | Full DB access. | Cannot reverse HMAC without `IDCARD_HASH_SECRET` (stored only in API + ETL env). Backup access policy treats backups as T0 — encrypted at rest + access logged. |

### Adversaries we do NOT fully defend against

- **Compromise of the API host** that simultaneously holds `IDCARD_HASH_SECRET` AND `DATABASE_URL_WRITER`. Mitigation: keep these on different hosts where possible; rotate secrets on suspected compromise.
- **Inference attacks** combining T2 quasi-identifiers across many requests. Mitigation: per-IP rate limiting + k-anon threshold; not perfect against a determined attacker with sustained API access.
- **Side-channel attacks** on Cloudflare or upstream Postgres infrastructure. Out of scope for application-layer defense.

---

## 3. Defense Layers (in order of evaluation)

When you add a new endpoint or migration, all five layers must remain intact:

### Layer 1 — Network (Cloudflare WAF)

- All traffic terminates at Cloudflare. Direct origin access is gated by Cloudflare Access service tokens (`CF-Access-Client-Id` / `CF-Access-Client-Secret` headers, set by the frontend Worker per `migrations/400_compat_raw_views.sql` doc).
- Rate limiting at the edge: `RATE_LIMIT_PUBLIC` requests/minute per IP.

### Layer 2 — Authentication (`api/security.py`)

- Public endpoints: `X-API-Key` validated by `APIKeyMiddleware` (constant-time `hmac.compare_digest`).
- Admin endpoints: `bma_session` JWT cookie + `_validate_csrf()` (256-bit `secrets.token_hex(32)` token).
- Bypass list (`_PUBLIC_PATHS`, `_PUBLIC_PREFIXES`): only `/health`, `/docs`, `/openapi.json`, `/metrics`, `/api/auth/*`, `/api/admin/upload-excel` (internally re-protected by `require_admin_session_or_bearer`).

### Layer 3 — Authorization (Postgres role)

- API process connects as `api_user`. Migration 500 explicitly REVOKEs SELECT on `pid`, `pid_encoded`, `pid_hash`, `idcard` columns from `api_user`.
- Migration 503 enforces `pid_encoded ~ '^[0-9a-f]{64}$'` via CHECK constraint — even a privileged role cannot insert raw IDCARDs.
- `etl_user` is used only by the writer pool, called from background ETL threads.

### Layer 4 — PII filter (`api/database.py:_PII_COLUMNS`)

- Last line of defense before serialization. `execute_query()` strips any column in the 23-name allowlist before returning rows. Applies regardless of the SQL written; protects against accidental `SELECT *` regressions.
- See `_PII_COLUMNS` in `api/database.py` for the canonical list.

### Layer 5 — k-anonymity (`bma-med/security/k_anon.py`)

- Aggregate endpoints with row counts call `k_anon_filter(rows, threshold=5)` before returning. Cells with `n < 5` are dropped (or masked, depending on caller).
- Quasi-identifier fields (`birthdate`) are bucketed at the view layer (`migrations/504_bucket_birthdate_in_raw_patients.sql`) so even joined queries can't reconstruct fine-grained demographics.

---

## 4. Secret Inventory

| Secret | Env Var | Length | Where Used | Rotation Policy |
|---|---|---|---|---|
| API key | `API_KEY` | ≥ 16 chars | API X-API-Key validation | Rotate quarterly. Update Cloudflare Worker secret + `.env`. |
| Admin password | `ADMIN_PASSWORD` | ≥ 16 chars | Admin login | Rotate per personnel change. |
| Session signing | `SECRET_KEY` | ≥ 32 chars | JWT cookie signing | Rotate yearly. Existing sessions invalidate (re-login required). |
| JWT signing | `JWT_SECRET` | ≥ 32 chars | API token signing | Same as SECRET_KEY. |
| IDCARD HMAC | `IDCARD_HASH_SECRET` | ≥ 16 chars | ETL pipeline + future API hash-on-lookup | **Never rotate without re-hashing the entire `pid_encoded` column.** Rotation invalidates cross-source patient joins until rehash completes. |
| DB connection | `DATABASE_URL_*` | n/a | Postgres connection | Rotate per personnel change. Use SCRAM-SHA-256 auth, never md5. |
| Cloudflare Access | `CF_ACCESS_CLIENT_ID` / `CLIENT_SECRET` | service token | Origin auth | Rotate yearly via Cloudflare dashboard. |

`config.py:validate_production_config()` refuses to boot the API in
production unless `API_KEY`, `ADMIN_PASSWORD`, `SECRET_KEY`, `JWT_SECRET`,
and `IDCARD_HASH_SECRET` are all set, ≥ 16 chars, and not on the
known-insecure-default list.

---

## 5. Incident Response

### Suspected PII leak

1. **Stop the leak**: revoke the suspect API key / DB credential immediately. Update `.env` + Cloudflare Worker secret + restart API.
2. **Assess scope**: run `migrations/AUDIT_pid_pii_exfiltration.sh` against the prod DB. Capture JSONL output for the regulator.
3. **Notify**: Thai PDPA §37 — notify PDPC within 72 hours if "high risk" to data subjects. IDCARD leaks meet this threshold by default.
4. **Notify subjects**: required if high risk. Keep records of all notifications for the inspection register.
5. **Post-mortem**: document root cause, defenses that failed, and which Layer (1-5 above) needs strengthening.

### Suspected RCE / DB compromise

1. **Rotate every secret** in §4 simultaneously.
2. **Verify migrations 500-504 are in place** — these are the single point of failure if the application code is compromised.
3. **Re-run rehash migration 501** with a fresh `IDCARD_HASH_SECRET` so any base64 backups become unusable.
4. **Restore backups only after** confirming the backup pre-dates compromise indicators. Treat all post-incident data as suspect until proven otherwise.

---

## 6. Change Review Checklist

Before merging a PR that touches `bma_med.*`, `api/routers/`, or migrations:

- [ ] No new `SELECT *` against `bma_med.patient` or any visit table with a `pid` column
- [ ] Any new identifier-shaped column added is in `_PII_COLUMNS`
- [ ] Any new aggregate endpoint applies `k_anon_filter()` before return
- [ ] Any f-string SQL has an explicit `assert` against the interpolated value
- [ ] Any new `.env` variable consumed by API is added to `validate_production_config()`
- [ ] Any new migration that adds a `pid` / `idcard` column also adds the same CHECK constraint as 503
- [ ] If the change touches authentication or authorization, document the threat-model implication in the PR description
