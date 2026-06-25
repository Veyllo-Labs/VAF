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
_migrations_attempted = False
# Owner/superuser engine (DDL, migrations, global stats) — kept separate from the app data engine.
_owner_engine: Optional[AsyncEngine] = None

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


def get_owner_database_url() -> str:
    """
    Owner/superuser DSN used for DDL, schema migrations and genuinely-global maintenance queries
    (e.g. get_db_stats, which counts across all users).

    Kept separate from the app data DSN (get_database_url) so that at the RLS cutover the app/data
    connection can switch to a NON-superuser role (which RLS actually enforces) while DDL and the
    global stats query keep working as the table owner. Empty config -> falls back to the app DSN,
    which is correct today because both are still the owner role 'vaf'.
    """
    url = (Config.get("memory_db_owner_url") or "").strip() or get_database_url()
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
    global _main_engine, _main_thread_id, _migrations_attempted

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

    run_migrations = False
    with _engine_lock:
        if not _migrations_attempted:
            _migrations_attempted = True
            run_migrations = True

    # Run migrations only once per process (best effort). DDL must run as the OWNER, not the app data
    # role, so it keeps working after the app DSN is cut over to a non-superuser RLS role.
    if run_migrations and _main_engine is not None:
        try:
            await _run_schema_migrations(await get_owner_engine())
        except Exception:
            pass  # Already migrated or error - continue anyway

    return _main_engine


async def get_owner_engine() -> AsyncEngine:
    """
    Engine on the owner/superuser DSN (get_owner_database_url). Used ONLY for DDL, schema migrations
    and genuinely-global maintenance queries (get_db_stats). NEVER use it for per-user data — that goes
    through get_db(user_scope_id=...) on the app engine so RLS applies. Daemon threads get a throwaway
    NullPool engine (disposed by the caller); the main thread caches a tiny pool.
    """
    global _owner_engine
    url = get_owner_database_url()
    if not _is_main_thread():
        return create_async_engine(url, echo=False, poolclass=NullPool)
    with _engine_lock:
        if _owner_engine is None:
            _owner_engine = create_async_engine(
                url,
                echo=Config.get("memory_db_echo", False),
                pool_size=1,
                max_overflow=2,
                pool_timeout=30,
                pool_recycle=300,
            )
    return _owner_engine


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
            # asyncpg does not accept bind parameters in SET LOCAL directly.
            # Use set_config(..., true) to scope the value to current transaction.
            await session.execute(
                text("SELECT set_config('app.current_user_scope_id', :scope, true)"),
                {"scope": str(user_scope_id)},
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


@asynccontextmanager
async def get_owner_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Session on the OWNER engine (no RLS GUC) for genuinely-global maintenance queries such as
    cross-user stats. NEVER use this for per-user data — that goes through get_db(user_scope_id=...)
    on the app engine so RLS applies.
    """
    engine = await get_owner_engine()
    session = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )()
    is_daemon = not _is_main_thread()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
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
    engine = await get_owner_engine()
    
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

        # Non-superuser application role for per-user data access (the role RLS actually enforces).
        # Idempotent; created with the dev-default password only if absent (production overrides the
        # password out-of-band). The app keeps connecting as the owner until the RLS cutover, so this
        # changes nothing until memory_db_url is switched to this role.
        await conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vaf_app') THEN
                    CREATE ROLE vaf_app LOGIN PASSWORD 'vaf_app_dev_secret'
                        NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
                END IF;
            END $$;
        """))
        await conn.execute(text("GRANT USAGE ON SCHEMA public TO vaf_app"))
        await conn.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON memories, chunks, connections TO vaf_app"))
        await conn.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO vaf_app"))

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
    # Global counts across all users -> run on the OWNER engine (bypasses RLS by design), so the
    # stats stay correct after the app data role is cut over to the non-superuser RLS role.
    async with get_owner_db() as db:
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
