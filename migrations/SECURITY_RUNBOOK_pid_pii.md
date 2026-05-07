# Runbook — Closing the `pid_encoded` PII leak (2026-05-07)

## Summary

`bma_med.patient.pid_encoded` and every visit table's `pid` column held
**base64-encoded plaintext Thai national IDs**. Despite the column name,
no hashing was applied — base64 is reversible in one line of Python.
Anyone with `SELECT` on these columns could decode and recover the raw
13-digit IDCARD (severe PDPA breach).

This runbook applies in **3 stages**: code → schema grants → data rewrite.
**Each stage is reversible only by restore-from-backup until the next stage
runs.** Do them in order.

## Pre-flight

1. **Take a logical backup of `bma_med.patient` and the visit tables.**
   The data rewrite is irreversible.

   ```bash
   pg_dump -t 'bma_med.*' "$DATABASE_URL_WRITER" \
     | gzip > backup_pre_pid_rehash_$(date +%F).sql.gz
   ```

2. **Confirm `IDCARD_HASH_SECRET` is set in `.env`** — minimum 16 chars.
   If missing, generate one:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Add to `.env` and **never rotate** without simultaneously re-hashing
   the column (rotation breaks the cross-source patient join).

3. **Pause the ETL/upload UI.** Concurrent uploads with the partially-
   rewritten table can leave a mix of base64 and HMAC values; the migration
   tolerates the mix but new uploads should use the patched ETL only.

## Stage 1 — Deploy code changes

Patches already applied locally (uncommitted):

| File | What changed |
|---|---|
| `bma-health-db/api/database.py` | `_PII_COLUMNS` now includes `pid_encoded`, `pid`, `pid_hash`, `idcard`, `phone`, `email`, `idline`, `fname/lname/efname/elname/fullname`, `haddr/address/addr`, `discaretel`, `hn`. Filters at `execute_query()` so any future SQL that selects them is silently stripped before serialization. |
| `bma-med/export.py` | Added `hash_pid()` helper using `hmac.new(IDCARD_HASH_SECRET, base64.b64decode(raw), 'sha256').hexdigest()`. `upsert_patient_master` and `load_cleaned_table` now hash `pid` before insert. Idempotent — passes through values that already look like 64-char hex. Aborts loudly if `IDCARD_HASH_SECRET` is unset / <16 chars. |

Commit, push, deploy:

```bash
cd /Users/dev/bma-health-db && git add api/database.py migrations/500_*.sql migrations/501_*.sql migrations/SECURITY_RUNBOOK_pid_pii.md
cd /Users/dev/bma-med   && git add export.py
# review diffs, then commit + deploy each repo per its normal release flow
```

## Stage 2 — Apply migration 500 (REVOKE)

Closes the API's read window in < 1 s. Safe under live traffic.

```bash
psql "$DATABASE_URL_WRITER" -f migrations/500_revoke_pid_pii.sql
```

The migration verifies no `SELECT (pid|pid_encoded|pid_hash)` privilege
remains for `api_user` and aborts otherwise.

After this stage, even if the existing data is still base64, the API
process **cannot read the column** — first line of defense in place.

## Stage 3 — Apply migration 501 (re-hash data)

Single-pass UPDATE per table. Locks each table for the duration; plan a
maintenance window if any visit table exceeds ~10 M rows.

```bash
IDCARD_HASH_SECRET=$(grep '^IDCARD_HASH_SECRET=' .env | cut -d= -f2-) \
psql "$DATABASE_URL_WRITER" \
     -v secret="$IDCARD_HASH_SECRET" \
     -f migrations/501_rehash_pid_encoded.sql
```

Watch for `RAISE NOTICE` lines — one per table — and the final
`✓ rehash complete`. If the migration aborts mid-way it can be re-run;
the helper is idempotent (already-hashed rows are skipped).

**Verify** by hand-decoding a sample row:

```bash
psql "$DATABASE_URL_WRITER" -c \
  "SELECT pid_encoded FROM bma_med.patient LIMIT 1"
# expect 64 hex chars — NOT a base64 string ending in '=' or '=='
```

## Stage 4 — Audit access logs

Check for anyone who *might* have already extracted the column. If your
deployment has Cloudflare Worker / API access logs:

- search for queries containing `pid_encoded`, `bma_med.patient`, or
  bulk SELECT against `raw_patients`
- search for unusual data export volumes from `api_user`
- if you find evidence of extraction, treat as a notifiable PDPA
  breach — file with PDPC within 72 hours

## After

- Add `IDCARD_HASH_SECRET` to the production validation in `api/config.py`
  (currently only checks `API_KEY/ADMIN_PASSWORD/SECRET_KEY/JWT_SECRET`).
- k-anonymity-protect `birthdate` in `public.raw_patients`: replace with
  `age_years` bucketed to 5-year ranges.
- Move `IDCARD_HASH_SECRET` to a managed secret store (Cloudflare Workers
  secrets, Vault, etc.) — never commit to git, never log.
- Schedule a quarterly column-grant audit:

  ```sql
  SELECT grantee, table_name, column_name FROM information_schema.column_privileges
  WHERE table_schema='bma_med' AND privilege_type='SELECT'
    AND column_name IN ('pid','pid_encoded','pid_hash','idcard','phone','email');
  ```
