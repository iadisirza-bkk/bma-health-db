-- Apply manually before first chat_v2 use: psql -U bma_med_admin -d bma_health -f migrations/300_chat_threads.sql
-- ============================================================================
-- 300_chat_threads.sql — DB-backed conversation persistence (ADR-02 §6).
-- ============================================================================
-- New tables for the chat layer:
--   * bma_med.chat_thread   — one row per conversation
--   * bma_med.chat_message  — one row per message (role: system/user/assistant/tool)
--
-- Privacy tier
-- ------------
-- Chat messages may contain PII inadvertently (a user might paste an HN/PID
-- into a question). We therefore treat the chat_message table at the same
-- tier as `patient`: writers (loader) + clinicians can read, plain readers
-- have NO grants. Sanitised re-exposure (if ever needed) must go through a
-- view, not directly.
--
-- Idempotent: every CREATE is "IF NOT EXISTS"; every GRANT/REVOKE is safe
-- to re-run; the audit trigger DROP-and-CREATEs.
-- ============================================================================

\echo '== creating chat_thread + chat_message =='

SET search_path = bma_med, public;

-- ============================================================================
-- chat_thread — one row per conversation
-- ============================================================================
CREATE TABLE IF NOT EXISTS bma_med.chat_thread (
    thread_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id     TEXT,                                    -- from auth cookie (S1)
    title       TEXT,
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb       -- soft-delete sentinel: metadata->>'deleted_at'
);

-- Listing threads for a user is the dominant read pattern; index by
-- (user_id, updated_at desc) so recent threads come back without a sort.
CREATE INDEX IF NOT EXISTS chat_thread_user_updated_idx
    ON bma_med.chat_thread (user_id, updated_at DESC);

-- ============================================================================
-- chat_message — one row per message in a thread
-- ============================================================================
CREATE TABLE IF NOT EXISTS bma_med.chat_message (
    message_id  BIGSERIAL PRIMARY KEY,
    thread_id   UUID NOT NULL REFERENCES bma_med.chat_thread(thread_id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('system','user','assistant','tool')),
    content     TEXT NOT NULL,
    tool_calls  JSONB,                                   -- on assistant
    tool_name   TEXT,                                    -- on role=tool
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Replay-a-thread query reads in (thread_id, created_at) order.
CREATE INDEX IF NOT EXISTS chat_message_thread_created_idx
    ON bma_med.chat_message (thread_id, created_at);

-- ============================================================================
-- updated_at maintenance for chat_thread
-- ============================================================================
-- Keep `updated_at` fresh on every UPDATE (most callers won't bother).
CREATE OR REPLACE FUNCTION bma_med.chat_thread_touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SET search_path = bma_med, pg_catalog;

DROP TRIGGER IF EXISTS chat_thread_touch_updated_at ON bma_med.chat_thread;
CREATE TRIGGER chat_thread_touch_updated_at
BEFORE UPDATE ON bma_med.chat_thread
FOR EACH ROW EXECUTE FUNCTION bma_med.chat_thread_touch_updated_at();

-- ============================================================================
-- Audit trigger on chat_message (append-only audit_log)
-- ============================================================================
-- The schema-init `bma_med.audit_row_change()` references `NEW.patient_id`,
-- which doesn't exist on chat_message. We therefore declare a chat-specific
-- audit function that uses message_id as the row_pk. Same target table
-- (`bma_med.audit_log`), same SECURITY DEFINER posture.
CREATE OR REPLACE FUNCTION bma_med.audit_chat_message_change() RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO bma_med.audit_log (operation, table_name, row_pk, detail)
    VALUES (TG_OP, TG_TABLE_NAME,
            COALESCE(NEW.message_id::text, OLD.message_id::text, ''),
            jsonb_build_object(
                'thread_id', COALESCE(NEW.thread_id::text, OLD.thread_id::text),
                'role',      COALESCE(NEW.role,           OLD.role),
                -- Don't echo full content into audit_log — it could be PII.
                -- Only the row PK + role + thread are recorded.
                'content_len', COALESCE(length(NEW.content), length(OLD.content), 0)
            ));
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = bma_med, pg_catalog;

DROP TRIGGER IF EXISTS chat_message_audit ON bma_med.chat_message;
CREATE TRIGGER chat_message_audit
AFTER INSERT OR UPDATE OR DELETE ON bma_med.chat_message
FOR EACH ROW EXECUTE FUNCTION bma_med.audit_chat_message_change();

-- ============================================================================
-- Grants
-- ============================================================================
\echo '== grants =='

-- Writer (the API process / orchestrator persists messages here).
GRANT SELECT, INSERT, UPDATE ON bma_med.chat_thread  TO bma_med_loader;
GRANT SELECT, INSERT, UPDATE ON bma_med.chat_message TO bma_med_loader;

-- Clinicians may read for support / debugging — never write.
GRANT SELECT ON bma_med.chat_thread  TO bma_med_clinician;
GRANT SELECT ON bma_med.chat_message TO bma_med_clinician;

-- Reader is non-PII tier — explicitly REVOKE so a fresh GRANT ALL above
-- (e.g. via a future blanket "GRANT SELECT ON ALL TABLES") doesn't leak.
REVOKE ALL ON bma_med.chat_thread  FROM bma_med_reader;
REVOKE ALL ON bma_med.chat_message FROM bma_med_reader;

-- BIGSERIAL on message_id needs USAGE on the underlying sequence for INSERT.
GRANT USAGE, SELECT ON SEQUENCE bma_med.chat_message_message_id_seq TO bma_med_loader;

-- ============================================================================
-- Runtime role wiring (CRITICAL — without this the API can't write)
-- ----------------------------------------------------------------------------
-- The FastAPI process connects as `etl_user` (see config.DATABASE_URL). The
-- privacy tiering in this migration is declared against the role *concept*
-- `bma_med_loader`. Make `etl_user` inherit those privileges:
--
--   GRANT bma_med_loader TO etl_user;
--
-- This is intentionally NOT executed by this migration — adding a role to a
-- group is an admin-tier privilege change that should land via a reviewed
-- secure-db-setup script, not silently from a feature migration. Run it once
-- as a superuser before the first /api/v2/chat/* request:
--
--   psql -U postgres -d bma_health -c "GRANT bma_med_loader TO etl_user;"
--
-- Verify with:
--   SELECT pg_has_role('etl_user', 'bma_med_loader', 'MEMBER');
-- ============================================================================

\echo '== 300_chat_threads.sql complete =='
