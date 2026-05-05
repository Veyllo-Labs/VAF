-- VAF Memory System - Database Initialization
-- This script runs automatically when the PostgreSQL container starts for the first time

-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Create schema for memory system
CREATE SCHEMA IF NOT EXISTS memory;

-- Grant permissions
GRANT ALL ON SCHEMA memory TO vaf;
GRANT ALL ON ALL TABLES IN SCHEMA memory TO vaf;
GRANT ALL ON ALL SEQUENCES IN SCHEMA memory TO vaf;

-- Set default search path
ALTER DATABASE vaf_memory SET search_path TO public, memory;

-- Performance settings for vector operations
-- These are applied to the database and will persist

-- Increase work_mem for vector operations (sorting, hashing)
ALTER DATABASE vaf_memory SET work_mem = '256MB';

-- Enable parallel query execution for vector similarity search
ALTER DATABASE vaf_memory SET max_parallel_workers_per_gather = 4;

-- HNSW index settings (applied via extension)
-- m = 16: Number of connections per layer (balance between speed and accuracy)
-- ef_construction = 64: Build-time accuracy (higher = better quality, slower build)

-- ═══════════════════════════════════════════════════════════════════════════════
-- Row-Level Security (RLS) for user isolation (defense-in-depth)
-- ═══════════════════════════════════════════════════════════════════════════════
-- RLS ensures that even if application-level filtering is bypassed (bug, SQL
-- injection), users can only access their own data. The application sets
-- `app.current_user_scope_id` per-session before executing queries.
--
-- Access rules:
--   1. If app.current_user_scope_id is set → only see memories with matching scope or NULL scope
--   2. If app.current_user_scope_id is empty/unset → see everything (local admin / no auth)
-- ═══════════════════════════════════════════════════════════════════════════════

DO $$
BEGIN
    -- Only enable RLS if memories table exists (idempotent)
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'memories') THEN
        ALTER TABLE memories ENABLE ROW LEVEL SECURITY;

        -- Drop existing policy if it exists (for re-runs)
        DROP POLICY IF EXISTS user_isolation_memories ON memories;

        -- Policy: users see only their own + unscoped (NULL) memories.
        -- When no scope is set (empty string or NULL), all rows are visible (admin mode).
        CREATE POLICY user_isolation_memories ON memories
            USING (
                COALESCE(current_setting('app.current_user_scope_id', true), '') = ''
                OR user_scope_id IS NULL
                OR user_scope_id = current_setting('app.current_user_scope_id', true)::uuid
            );

        RAISE NOTICE 'RLS enabled on memories table';
    END IF;
END
$$;

-- Log successful initialization
DO $$
BEGIN
    RAISE NOTICE 'VAF Memory database initialized successfully with pgvector extension';
END
$$;
