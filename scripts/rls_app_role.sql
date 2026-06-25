-- VAF Memory — Stage 3 of RLS hardening: non-superuser application role for per-user data access.
--
-- Idempotent. Run as the owner/superuser (vaf):
--   docker exec -i vaf-memory-db psql -U vaf -d vaf_memory < scripts/rls_app_role.sql
--
-- This only CREATES the role and GRANTs DML. It is an UNUSED login until the final cutover (Stage 5),
-- where the app data DSN (memory_db_url) switches to this role together with the fail-closed policy +
-- FORCE ROW LEVEL SECURITY (scripts/rls_enforce.sql). Until then it changes nothing observable.
--
-- The owner role 'vaf' is kept for DDL, schema migrations and global stats (database.py owner engine).

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vaf_app') THEN
    CREATE ROLE vaf_app LOGIN PASSWORD 'vaf_app_dev_secret'
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END
$$;

GRANT CONNECT ON DATABASE vaf_memory TO vaf_app;
GRANT USAGE ON SCHEMA public TO vaf_app;
-- The vaf_memory database also holds the auth/system tables (local_users, user_sessions) accessed via
-- the same get_db connection, so vaf_app needs DML on ALL tables, not just the 3 memory tables. Only the
-- 'memories' table is RLS-protected; the auth/system tables have no RLS (login needs a cross-user lookup;
-- they are gated at the application layer / require_admin instead).
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO vaf_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO vaf_app;

-- Cover any future tables/sequences the owner creates so the app role keeps working after migrations.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO vaf_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO vaf_app;
