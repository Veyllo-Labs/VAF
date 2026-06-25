-- VAF Memory — Stage 5 (CUTOVER): make Row-Level Security a real, fail-closed backstop on `memories`.
--
-- Run as the owner/superuser (vaf), TOGETHER with switching the app data DSN to the non-super role:
--   docker exec -i vaf-memory-db psql -U vaf -d vaf_memory < scripts/rls_enforce.sql
-- and set in ~/.vaf/config.json:
--   "memory_db_owner_url": "postgresql://vaf:vaf_dev_secret@localhost:5432/vaf_memory"
--   "memory_db_url":       "postgresql://vaf_app:vaf_app_dev_secret@localhost:5432/vaf_memory"
-- then restart VAF.
--
-- After this, the app connects as vaf_app (NOSUPERUSER, NOBYPASSRLS), so RLS is enforced for every data
-- session. The owner role 'vaf' (superuser) keeps bypassing RLS, so DDL/migrations/global stats still work
-- via the owner engine (database.py). Prerequisite: scripts/rls_app_role.sql has been applied (role exists).
--
-- ROLLBACK (if memory goes empty or anything breaks): set memory_db_url back to the vaf DSN and restart —
-- the app reconnects as the superuser owner, which bypasses RLS, so all rows are visible again immediately.
-- Optionally also run scripts/rls_disable.sql to drop FORCE/policy. No row is ever mutated by this script.

-- Fail-CLOSED policy: a row is visible/writable ONLY when its user_scope_id equals the per-transaction GUC.
-- NULLIF(...,'') maps an unset/empty GUC to NULL, so an unscoped session matches nothing (deny), and a row
-- with a NULL user_scope_id is NOT blanket-visible. This replaces the old fail-OPEN policy.
DROP POLICY IF EXISTS user_isolation_memories ON memories;
CREATE POLICY user_isolation_memories ON memories
    USING      (user_scope_id = NULLIF(current_setting('app.current_user_scope_id', true), '')::uuid)
    WITH CHECK (user_scope_id = NULLIF(current_setting('app.current_user_scope_id', true), '')::uuid);

ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories FORCE  ROW LEVEL SECURITY;

-- Sanity (run as vaf_app afterwards, NOT here): a scoped session sees only its own rows; an unscoped session
-- sees zero. As the owner (this script's session) you still see all rows because superuser bypasses RLS.
