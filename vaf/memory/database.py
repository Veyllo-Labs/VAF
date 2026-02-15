"""
Database connection and initialization for VAF Memory System.

Supports:
- PostgreSQL with pgvector extension
- Async operations via asyncpg
- Connection pooling (main thread) / NullPool (daemon threads)
- Schema migrations
"""

import asyncio
import threading
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

# Main thread engine (with connection pooling)
_main_engine: Optional[AsyncEngine] = None
_main_session_factory: Optional[async_sessionmaker[AsyncSession]] = None
_main_thread_id: Optional[int] = None
_engine_lock = threading.Lock()

# Track which thread is the main thread
def _is_main_thread() -> bool:
    """Check if we're running in the main thread."""
    return threading.current_thread() is threading.main_thread()


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
    Get or create the async database engine.

    CRITICAL FOR MEMORY LEAK PREVENTION:
    - Main thread: uses connection pooling (pool_size=2)
    - Daemon threads: uses NullPool (no pooling, connections close immediately)

    This prevents memory leaks from daemon threads creating new event loops
    with their own connection pools that never get disposed.
    """
    global _main_engine, _main_thread_id

    url = get_database_url()

    # Daemon threads get NullPool engines (no pooling = no leak)
    # These engines are created fresh each time and disposed immediately after use
    if not _is_main_thread():
        # Create a throwaway engine with NullPool - closes connections immediately
        engine = create_async_engine(
            url,
            echo=False,  # Never log in daemon threads
            poolclass=NullPool,  # No pooling! Connections close immediately.
        )
        # Don't run migrations from daemon threads
        return engine

    # Main thread: use cached pooled engine
    with _engine_lock:
        if _main_engine is None:
            _main_thread_id = threading.current_thread().ident
            _main_engine = create_async_engine(
                url,
                echo=Config.get("memory_db_echo", False),
                pool_size=2,  # Small pool for main thread
                max_overflow=3,
                pool_timeout=30,
                pool_recycle=300,
            )

    # Run migrations only once from main thread
    if _main_engine is not None:
        try:
            await _run_schema_migrations(_main_engine)
        except Exception:
            pass  # Already migrated or error - continue anyway

    return _main_engine


async def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the async session factory."""
    global _main_session_factory

    engine = await get_engine()

    # Daemon threads: create fresh factory each time (uses NullPool engine)
    if not _is_main_thread():
        return async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    # Main thread: cache the factory
    with _engine_lock:
        if _main_session_factory is None:
            _main_session_factory = async_sessionmaker(
                engine,
                class_=AsyncSession,
                expire_on_commit=False,
                autoflush=False,
            )

    return _main_session_factory


@asynccontextmanager
async def get_db(user_scope_id: Optional[str] = None) -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager for database sessions.

    MEMORY LEAK FIX: For daemon threads, disposes engine after session closes.

    Args:
        user_scope_id: If set, activates PostgreSQL Row-Level Security (RLS)
            for this session by setting ``app.current_user_scope_id``. When
            omitted (local admin / no auth), RLS allows access to all rows.

    Usage:
        async with get_db() as db:
            result = await db.execute(select(Memory))
            memories = result.scalars().all()
    """
    engine = await get_engine()
    factory = await get_session_factory()
    session = factory()
    is_daemon = not _is_main_thread()

    try:
        # Set RLS context for this session (defense-in-depth)
        if user_scope_id:
            await session.execute(
                text("SET LOCAL app.current_user_scope_id = :scope"),
                {"scope": str(user_scope_id)}
            )
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
        # CRITICAL: Dispose NullPool engine immediately for daemon threads
        if is_daemon:
            try:
                await engine.dispose()
            except Exception:
                pass


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

        # Enable Row-Level Security on memories table (defense-in-depth)
        await conn.execute(text("ALTER TABLE memories ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text("DROP POLICY IF EXISTS user_isolation_memories ON memories"))
        await conn.execute(text("""
            CREATE POLICY user_isolation_memories ON memories
                USING (
                    COALESCE(current_setting('app.current_user_scope_id', true), '') = ''
                    OR user_scope_id IS NULL
                    OR user_scope_id = current_setting('app.current_user_scope_id', true)::uuid
                )
        """))

        logger.info("Memory database initialized successfully (RLS enabled)")


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
    """Close database connections and clean up."""
    global _main_engine, _main_session_factory
    if _main_engine is not None:
        try:
            await _main_engine.dispose()
        except Exception:
            pass
    _main_engine = None
    _main_session_factory = None
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
