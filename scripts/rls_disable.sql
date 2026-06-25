-- VAF Memory — ROLLBACK of the RLS cutover (scripts/rls_enforce.sql).
--
-- The PRIMARY rollback is simply setting memory_db_url back to the vaf (owner/superuser) DSN and
-- restarting VAF: the app then bypasses RLS and all rows are visible again. This script is the optional
-- belt-and-suspenders step to also remove enforcement at the DB level. Run as owner (vaf):
--   docker exec -i vaf-memory-db psql -U vaf -d vaf_memory < scripts/rls_disable.sql
-- No row is mutated.

ALTER TABLE memories NO FORCE ROW LEVEL SECURITY;
ALTER TABLE memories DISABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_isolation_memories ON memories;
