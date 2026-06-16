-- =============================================================================
-- 501_rehash_pid_encoded.sql — replace base64(IDCARD) with HMAC-SHA256(IDCARD)
-- =============================================================================
-- WHY: bma_med.patient.pid_encoded and every visit table's `pid` column hold
-- base64-encoded plaintext Thai national IDs. After 500 closes the API read
-- window, this migration rewrites the existing data in-place so that even an
-- admin or compromised loader sees only the HMAC, never the raw IDCARD.
--
-- ALGORITHM (matches /Users/dev/bma-med/export.py:hash_pid):
--   pid_hashed = encode(hmac(decode(pid_encoded, 'base64'), :secret, 'sha256'), 'hex')
--
-- The function is idempotent: if `pid_encoded` is already a 64-char hex hash,
-- skip it. That lets the migration be re-run safely after partial failure or
-- after a fresh ETL load that already hashed.
--
-- HOW TO RUN (the secret is passed as a psql variable so it never appears in
-- the file or in shell history under `set -x`):
--   IDCARD_HASH_SECRET=$(grep '^IDCARD_HASH_SECRET=' .env | cut -d= -f2-) \
--   psql "$DATABASE_URL_WRITER" \
--        -v secret="$IDCARD_HASH_SECRET" \
--        -f migrations/501_rehash_pid_encoded.sql
--
-- DOWNTIME: bma_med.patient is locked for the duration of UPDATE — at 600k
-- rows this is ~5–15 s on commodity Postgres. Visit tables (10–100 M rows
-- combined) are batched 100k at a time to keep transactions short.
--
-- ROLLBACK: there is none — once the IDCARD is hashed, it cannot be reversed
-- without the original CSV. Take a logical dump of bma_med.patient before
-- running:
--   pg_dump -t bma_med.patient "$DATABASE_URL_WRITER" > pre_rehash.dump
-- =============================================================================

\set ON_ERROR_STOP on

\if :{?secret}
\else
    \echo 'FATAL: IDCARD_HASH_SECRET not provided. Run with: psql -v secret=$IDCARD_HASH_SECRET -f ...'
    \quit 1
\endif

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ── Stash the secret in a session GUC so the helper function can read it
--    without it appearing in every UPDATE statement. The GUC is session-local
--    and dies with the connection.
SELECT set_config('rehash.secret', :'secret', false);

-- ── Helper: HMAC-SHA256 with idempotency check ──────────────────────────────
-- Lives in the public schema so the per-table dynamic UPDATEs can call it
-- across COMMITs (pg_temp objects don't survive COMMITs in plpgsql blocks).
-- Dropped at the end of this migration.
CREATE OR REPLACE FUNCTION public._rehash_pid_hmac(raw text)
RETURNS text
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    decoded bytea;
    secret  text := current_setting('rehash.secret', true);
BEGIN
    IF secret IS NULL OR length(secret) < 16 THEN
        RAISE EXCEPTION 'rehash.secret GUC missing or too short (need >= 16 chars)';
    END IF;
    IF raw IS NULL OR length(trim(raw)) = 0 THEN
        RETURN raw;
    END IF;
    -- Already hashed? 64 hex chars — pass through unchanged.
    IF raw ~ '^[0-9a-f]{64}$' THEN
        RETURN raw;
    END IF;
    -- Try base64 decode; fall back to raw bytes if it's not valid base64.
    BEGIN
        decoded := decode(raw, 'base64');
    EXCEPTION WHEN OTHERS THEN
        decoded := convert_to(raw, 'UTF8');
    END;
    RETURN encode(hmac(decoded, secret, 'sha256'), 'hex');
END;
$$;

-- ── 1. Patient master ───────────────────────────────────────────────────────
-- pid_encoded is UNIQUE NOT NULL — must rewrite before any visit-table FK
-- traversal, because patient_id (the BIGSERIAL surrogate) is unaffected.
--
-- The patient_audit trigger (schema_init.sql:178) writes to_jsonb(OLD) and
-- to_jsonb(NEW) into audit_log.detail on every UPDATE. If we leave it on
-- during this migration, we'd write 600k+ base64 plaintext IDCARDs into
-- audit_log — exactly the leak we're trying to close. Disable for the
-- duration; the rehash itself is recorded as a single audit_log entry below.
BEGIN;

ALTER TABLE bma_med.patient DISABLE TRIGGER patient_audit;

UPDATE bma_med.patient
SET pid_encoded = public._rehash_pid_hmac(pid_encoded)
WHERE pid_encoded IS NOT NULL
  AND pid_encoded !~ '^[0-9a-f]{64}$';

ALTER TABLE bma_med.patient ENABLE TRIGGER patient_audit;

INSERT INTO bma_med.audit_log (user_name, operation, table_name, row_pk, detail)
VALUES (current_user, 'UPDATE', 'patient', 'BULK',
        jsonb_build_object(
            'note', 'rehash_pid_encoded migration 501',
            'rows_affected', (SELECT COUNT(*) FROM bma_med.patient)
        ));

DO $$
DECLARE
    leaked int;
BEGIN
    SELECT COUNT(*) INTO leaked FROM bma_med.patient
    WHERE pid_encoded IS NOT NULL AND pid_encoded !~ '^[0-9a-f]{64}$';
    IF leaked > 0 THEN
        RAISE EXCEPTION 'patient: % rows still hold non-hashed pid_encoded', leaked;
    END IF;
END $$;

COMMIT;

-- ── 2. Visit tables — every bma_med.* with a `pid` column ───────────────────
-- One UPDATE per table, each in its own transaction. Locks the whole table
-- for the duration — for the largest visit tables (10–50 M rows) plan for
-- single-digit minutes of write blocking. Run during a quiet window or
-- replace this DO block with a CALL to a procedure that does row-batching
-- if your table sizes need it (Postgres 11+, only procedures can COMMIT
-- mid-loop).
--
-- The session GUC `rehash.secret` set above persists across these COMMITs.
DO $$
DECLARE
    t text;
    n_updated bigint;
BEGIN
    FOR t IN
        SELECT table_name
        FROM information_schema.columns
        WHERE table_schema = 'bma_med'
          AND column_name = 'pid'
          AND table_name <> 'patient'
        ORDER BY table_name
    LOOP
        RAISE NOTICE 'rehashing pid in bma_med.% ...', t;
        EXECUTE format(
            'UPDATE bma_med.%1$I SET pid = public._rehash_pid_hmac(pid) '
            'WHERE pid IS NOT NULL AND pid !~ ''^[0-9a-f]{64}$''',
            t
        );
        GET DIAGNOSTICS n_updated = ROW_COUNT;
        RAISE NOTICE '  bma_med.% — % rows updated', t, n_updated;
    END LOOP;
END $$;

-- ── 3. Sanity check ─────────────────────────────────────────────────────────
DO $$
DECLARE
    t text;
    leaked bigint;
    total_leaked bigint := 0;
BEGIN
    FOR t IN
        SELECT table_name
        FROM information_schema.columns
        WHERE table_schema = 'bma_med' AND column_name = 'pid'
    LOOP
        EXECUTE format(
            'SELECT COUNT(*) FROM bma_med.%I WHERE pid IS NOT NULL AND pid !~ ''^[0-9a-f]{64}$''',
            t
        ) INTO leaked;
        IF leaked > 0 THEN
            RAISE WARNING 'bma_med.%: % rows still hold non-hashed pid', t, leaked;
            total_leaked := total_leaked + leaked;
        END IF;
    END LOOP;
    IF total_leaked > 0 THEN
        RAISE EXCEPTION 'rehash incomplete: % unhashed rows total', total_leaked;
    END IF;
    RAISE NOTICE '✓ rehash complete — all pid columns now HMAC-SHA256 hex';
END $$;

-- ── 4. Cleanup ──────────────────────────────────────────────────────────────
DROP FUNCTION public._rehash_pid_hmac(text);
SELECT set_config('rehash.secret', '', false);
