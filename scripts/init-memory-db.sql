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

-- Log successful initialization
DO $$
BEGIN
    RAISE NOTICE 'VAF Memory database initialized successfully with pgvector extension';
END
$$;
