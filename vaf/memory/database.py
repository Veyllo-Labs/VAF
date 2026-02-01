"""
Database connection and initialization for VAF Memory System.

Supports:
- PostgreSQL with pgvector extension
- Async operations via asyncpg
- Connection pooling
- Schema migrations
"""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession,
    async_sessionmaker,
    AsyncEngine
)
from sqlalchemy import text, event
from sqlalchemy.pool import NullPool
from vaf.core.config import Config
from vaf.memory.models import Base, EMBEDDING_DIM
import logging

logger = logging.getLogger(__name__)

# Engine and session factory per event loop (avoids "attached to a different loop" when tools run in another thread)
_engine_by_loop: dict = {}
_session_factory_by_loop: dict = {}


def get_database_url() -> str:
    """
    Get database URL from config.
    
    Converts postgresql:// to postgresql+asyncpg:// for async support.
    """
    url = Config.get("memory_db_url", "postgresql://vaf:vaf_dev_secret@localhost:5432/vaf_memory")
    
    # Ensure async driver
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif not url.startswith("postgresql+asyncpg://"):
        url = f"postgresql+asyncpg://{url}"
    
    return url


async def _run_schema_migrations(engine: AsyncEngine) -> None:
    """
    Add missing columns/indexes to existing DBs (e.g. user_scope_id added later).
    Safe to run multiple times (IF NOT EXISTS / IF NOT EXISTS).
    """
    try:
        async with engine.begin() as conn:
            # PostgreSQL: add user_scope_id if table exists but column was added in a later model
            await conn.execute(text("""
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'memories')
                       AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'memories' AND column_name = 'user_scope_id')
                    THEN
                        ALTER TABLE memories ADD COLUMN user_scope_id UUID NULL;
                        CREATE INDEX ix_memories_user_scope_id ON memories (user_scope_id);
                    END IF;
                END $$;
            """))
    except Exception as e:
        logger.warning("Schema migration (user_scope_id) skipped or failed: %s", e)


async def get_engine() -> AsyncEngine:
    """
    Get or create the async database engine for the current event loop.
    Cached per-loop so tools (e.g. memory_store) running in another thread's loop get their own engine.
    """
    loop = asyncio.get_running_loop()
    if loop not in _engine_by_loop:
        url = get_database_url()
        _engine_by_loop[loop] = create_async_engine(
            url,
            echo=Config.get("memory_db_echo", False),
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=1800,  # Recycle connections after 30 minutes
        )
        await _run_schema_migrations(_engine_by_loop[loop])
    return _engine_by_loop[loop]


async def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the async session factory for the current event loop."""
    loop = asyncio.get_running_loop()
    if loop not in _session_factory_by_loop:
        engine = await get_engine()
        _session_factory_by_loop[loop] = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory_by_loop[loop]


@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager for database sessions.
    
    Usage:
        async with get_db() as db:
            result = await db.execute(select(Memory))
            memories = result.scalars().all()
    """
    factory = await get_session_factory()
    session = factory()
    
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def init_db(drop_existing: bool = False):
    """
    Initialize the database schema.
    
    Creates:
    - pgvector extension
    - All tables from models
    - HNSW indexes for vector search
    
    Args:
        drop_existing: If True, drop existing tables first (DANGEROUS!)
    """
    engine = await get_engine()
    
    async with engine.begin() as conn:
        # Enable pgvector extension
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        
        if drop_existing:
            logger.warning("Dropping existing memory tables!")
            await conn.run_sync(Base.metadata.drop_all)
        
        # Create tables
        await conn.run_sync(Base.metadata.create_all)
        
        # Create HNSW indexes for vector columns
        # These provide fast approximate nearest neighbor search
        
        # Index for memory embeddings
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_memories_embedding_hnsw 
            ON memories 
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """))
        
        # Index for chunk embeddings (main RAG search)
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_chunks_embedding_hnsw 
            ON chunks 
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """))
        
        logger.info("Memory database initialized successfully")


async def check_db_connection() -> bool:
    """
    Check if database connection is healthy.
    
    Returns:
        True if connection is healthy, False otherwise
    """
    try:
        engine = await get_engine()
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            return result.scalar() == 1
    except Exception as e:
        logger.error(f"Database connection check failed: {e}")
        return False


async def close_db():
    """Close database connections and clean up (all loops)."""
    global _engine_by_loop, _session_factory_by_loop
    for loop, engine in list(_engine_by_loop.items()):
        try:
            await engine.dispose()
        except Exception:
            pass
    _engine_by_loop.clear()
    _session_factory_by_loop.clear()
    logger.info("Database connections closed")


async def get_db_stats() -> dict:
    """
    Get database statistics for monitoring.
    
    Returns:
        Dict with memory count, chunk count, connection count
    """
    async with get_db() as db:
        from vaf.memory.models import Memory, Chunk, Connection
        from sqlalchemy import func, select
        
        memory_count = await db.scalar(
            select(func.count()).select_from(Memory).where(Memory.is_deleted == False)
        )
        chunk_count = await db.scalar(
            select(func.count()).select_from(Chunk)
        )
        connection_count = await db.scalar(
            select(func.count()).select_from(Connection)
        )
        
        return {
            "memories": memory_count or 0,
            "chunks": chunk_count or 0,
            "connections": connection_count or 0,
        }


# Helper for running async init from sync context
def init_db_sync(drop_existing: bool = False):
    """
    Synchronous wrapper for init_db.
    
    Useful for CLI commands or startup scripts.
    """
    asyncio.run(init_db(drop_existing))
