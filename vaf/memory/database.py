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

# Global engine and session factory
_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


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


async def get_engine() -> AsyncEngine:
    """
    Get or create the async database engine.
    
    Uses connection pooling for performance.
    """
    global _engine
    
    if _engine is None:
        url = get_database_url()
        _engine = create_async_engine(
            url,
            echo=Config.get("memory_db_echo", False),
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=1800,  # Recycle connections after 30 minutes
        )
    
    return _engine


async def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the async session factory."""
    global _session_factory
    
    if _session_factory is None:
        engine = await get_engine()
        _session_factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    
    return _session_factory


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
    """Close database connections and clean up."""
    global _engine, _session_factory
    
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
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
