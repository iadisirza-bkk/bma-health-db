-- =============================================================================
-- 503_constrain_pid_format.sql — DB-level guard against future pid regressions
-- =============================================================================
-- WHY: 500/501/502 closed the existing leak, but the application code is the
-- only thing stopping a future ETL bug, manual UPDATE, or COPY from putting
-- raw base64 IDCARDs back into bma_med.patient.pid_encoded. A CHECK
-- constraint at the DB layer makes the leak physically impossible — any
-- INSERT/UPDATE that supplies a value not matching ^[0-9a-f]{64}$ is rejected
-- by the database, regardless of how trusted the calling code is.
--
-- Same constraint added to every visit table's pid column. This is the
-- "design out" defense that pairs with the "REVOKE" defense in 500.
--
-- PRECONDITION: 501 has run successfully, so all existing rows already match
-- the regex. The migration verifies this before adding the constraint.
--
-- HOW TO RUN:
--   psql "$DATABASE_URL_WRITER" -f migrations/503_constrain_pid_format.sql
--
-- DOWNTIME: ALTER TABLE ... ADD CONSTRAINT requires SHARE ROW EXCLUSIVE for
-- the validation scan. On a 600k-row patient table this is sub-second; on
-- a 50M-row visit table it can take ~30s. Use NOT VALID + VALIDATE if you
-- need to defer the scan; this migration uses the simpler "validate now"
-- form because correctness > speed for a security guard.
--
-- ROLLBACK: ALTER TABLE bma_med.patient DROP CONSTRAINT pid_encoded_hex_chk;
-- =============================================================================

\set ON_ERROR_STOP on

BEGIN;

-- ── 1. Pre-flight — confirm 501 has fully landed everywhere ────────────────
DO $$
DECLARE
    leaked int := 0;
    total  int := 0;
    t      text;
    n      int;
BEGIN
    -- patient
    SELECT COUNT(*) INTO leaked FROM bma_med.patient
    WHERE pid_encoded IS NOT NULL AND pid_encoded !~ '^[0-9a-f]{64}$';
    IF leaked > 0 THEN
        RAISE EXCEPTION
            'cannot add constraint: bma_med.patient still has % unhashed rows. '
            'Run migration 501 first.', leaked;
    END IF;

    -- visit tables
    FOR t IN
        SELECT table_name FROM information_schema.columns
        WHERE table_schema = 'bma_med'
          AND column_name = 'pid'
          AND table_name <> 'patient'
    LOOP
        EXECUTE format(
            'SELECT COUNT(*) FROM bma_med.%I WHERE pid IS NOT NULL AND pid !~ ''^[0-9a-f]{64}$''',
            t
        ) INTO n;
        IF n > 0 THEN
            RAISE EXCEPTION
                'cannot add constraint: bma_med.% still has % unhashed rows. '
                'Run migration 501 first.', t, n;
        END IF;
        total := total + 1;
    END LOOP;

    RAISE NOTICE 'pre-flight OK: % visit tables verified clean, ready to constrain', total;
END $$;

-- ── 2. Patient master ──────────────────────────────────────────────────────
ALTER TABLE bma_med.patient
    DROP CONSTRAINT IF EXISTS pid_encoded_hex_chk;

ALTER TABLE bma_med.patient
    ADD CONSTRAINT pid_encoded_hex_chk
    CHECK (pid_encoded ~ '^[0-9a-f]{64}$');

-- pid_hash is BYTEA so the regex doesn't apply, but enforce length 32 (raw
-- SHA-256 = 32 bytes) as a similar guard.
ALTER TABLE bma_med.patient
    DROP CONSTRAINT IF EXISTS pid_hash_length_chk;

ALTER TABLE bma_med.patient
    ADD CONSTRAINT pid_hash_length_chk
    CHECK (pid_hash IS NULL OR length(pid_hash) = 32);

-- ── 3. Visit tables ────────────────────────────────────────────────────────
DO $$
DECLARE
    t text;
BEGIN
    FOR t IN
        SELECT table_name FROM information_schema.columns
        WHERE table_schema = 'bma_med'
          AND column_name = 'pid'
          AND table_name <> 'patient'
        ORDER BY table_name
    LOOP
        EXECUTE format(
            'ALTER TABLE bma_med.%1$I DROP CONSTRAINT IF EXISTS %2$I',
            t, t || '_pid_hex_chk'
        );
        EXECUTE format(
            $f$ALTER TABLE bma_med.%1$I ADD CONSTRAINT %2$I CHECK (pid IS NULL OR pid ~ '^[0-9a-f]{64}$')$f$,
            t, t || '_pid_hex_chk'
        );
        RAISE NOTICE '  ✓ bma_med.% — pid CHECK constraint added', t;
    END LOOP;
END $$;

-- ── 4. Sanity check ────────────────────────────────────────────────────────
DO $$
DECLARE
    n_constraints int;
BEGIN
    SELECT COUNT(*) INTO n_constraints
    FROM information_schema.check_constraints
    WHERE constraint_name LIKE '%_pid_hex_chk' OR constraint_name = 'pid_encoded_hex_chk';
    RAISE NOTICE '✓ % CHECK constraint(s) in place — base64 IDCARDs cannot be inserted', n_constraints;
END $$;

COMMIT;
