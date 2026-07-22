# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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


def _columns_to_add(table, existing_names, dialect=None):
    """PURE: model columns present on ``table`` but missing from the live table.

    Returns ``[(column_name, compiled_ddl_type), ...]`` (e.g. ``("user_scope_id", "UUID")``,
    ``("embedding", "VECTOR(384)")``). Each type is compiled for PostgreSQL. Columns are always
    added as NULLable downstream — a NOT NULL column cannot be added to a table that already has
    rows; the app fills them via its Python-side defaults on new writes and existing rows get NULL.
    Testable without a database (pass a real ``Base`` table + the set of existing column names).
    """
    if dialect is None:
        from sqlalchemy.dialects import postgresql
        dialect = postgresql.dialect()
    existing = {str(n).lower() for n in (existing_names or set())}
    out = []
    for col in table.columns:
        if col.name.lower() in existing:
            continue
        try:
            ddl_type = col.type.compile(dialect=dialect)
        except Exception:
            # A type we can't render generically — leave it to an explicit db_migrations entry.
            continue
        out.append((col.name, ddl_type))
    return out


async def _reconcile_columns(conn) -> dict:
    """Add any model column missing from an EXISTING live table (generic additive reconcile).

    Brand-new tables are handled by ``create_all``; this only ALTERs tables that already exist, so a
    new additive column on memories/chunks/connections/local_users/user_sessions lands automatically
    after an update — no per-change migration needed. Returns ``{table_name: [added_columns]}``.
    """
    # Ensure auth models are registered on the shared Base so their tables are covered too.
    try:
        import vaf.auth.models  # noqa: F401
    except Exception:
        pass
    from sqlalchemy.dialects import postgresql
    dialect = postgresql.dialect()

    res = await conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ))
    live_tables = {row[0] for row in res.fetchall()}

    added: dict = {}
    for tname, table in Base.metadata.tables.items():
        if tname not in live_tables:
            continue  # create_all creates brand-new tables; nothing to reconcile here
        res = await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t"
        ), {"t": tname})
        existing = {row[0] for row in res.fetchall()}
        to_add = _columns_to_add(table, existing, dialect)
        for name, ddl_type in to_add:
            await conn.execute(text(f'ALTER TABLE "{tname}" ADD COLUMN IF NOT EXISTS "{name}" {ddl_type}'))
        if to_add:
            added[tname] = [n for n, _ in to_add]
    return added


async def _check_embedding_dim(conn) -> None:
    """Loudly flag (never auto-fix) a mismatch between the live pgvector column dimension and the
    dimension this build expects (``EMBEDDING_DIM``). A changed embedding model with a different
    dimension is a hard, manual case (re-embed / reset) — surfacing it beats failing queries silently.
    """
    import re as _re
    for tname in ("memories", "chunks"):
        try:
            res = await conn.execute(text("""
                SELECT format_type(a.atttypid, a.atttypmod)
                FROM pg_attribute a JOIN pg_class c ON a.attrelid = c.oid
                WHERE c.relname = :t AND a.attname = 'embedding' AND a.attnum > 0 AND NOT a.attisdropped
            """), {"t": tname})
            row = res.first()
            if not row or not row[0]:
                continue
            m = _re.search(r"\((\d+)\)", str(row[0]))
            if not m:
                continue
            live_dim = int(m.group(1))
            if live_dim != EMBEDDING_DIM:
                logger.error(
                    "EMBEDDING DIMENSION MISMATCH on %s.embedding: live DB column is vector(%d) but this "
                    "VAF build expects vector(%d) — the configured embedding model changed dimension. "
                    "Vector search will fail until the memory store is re-embedded/reset. Not auto-changing it.",
                    tname, live_dim, EMBEDDING_DIM,
                )
        except Exception as e:
            logger.warning("Embedding-dimension check on %s skipped: %s", tname, e)


async def _run_schema_migrations(engine: AsyncEngine) -> bool:
    """Reconcile the live DB schema with the models (runs once per process; the restart after an
    update triggers it). Three idempotent, phase-isolated parts: (1) ordered explicit migrations
    from ``db_migrations.py`` (indexes/renames/backfills, e.g. memories.user_scope_id + its index);
    (2) a generic "add missing columns" pass so a new additive column on any existing table is
    applied without boilerplate; (3) an embedding-dimension mismatch check. Failures are logged
    LOUDLY (ERROR) — not swallowed — so an out-of-date/half-applied schema is visible rather than
    breaking queries silently. Phases run in separate transactions so one failure can't undo another.

    Returns False when the database was UNREACHABLE (nothing was attempted) so the caller can
    release its once-per-process latch and retry on a later call; True once the phases actually
    ran against a live database (individual phase errors still count as attempted - retrying a
    genuinely failing DDL on every call would only spam the log).
    """
    # 0. Reachability probe - a down/starting container must not consume the one attempt.
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        logger.error("DB migrations skipped - database unreachable, will retry on next use: %s", e)
        return False

    # 1. Ordered explicit migrations (indexes / renames / backfills).
    try:
        from vaf.memory import db_migrations as _dbm
        async with engine.begin() as conn:
            applied = await _dbm.run_db_migrations(conn)
        if applied:
            logger.info("DB ordered migrations ran: %s", applied)
    except Exception as e:
        logger.error("DB ordered migrations FAILED (schema may be out of date): %s", e, exc_info=True)

    # 2. Generic additive reconcile — add any model column missing from an existing table.
    try:
        async with engine.begin() as conn:
            added = await _reconcile_columns(conn)
        if added:
            logger.info("DB schema reconcile added missing columns: %s", added)
    except Exception as e:
        logger.error("DB column reconcile FAILED (new columns may be missing — queries could break): %s",
                     e, exc_info=True)

    # 3. Embedding-dimension mismatch (loud, no auto-fix).
    try:
        async with engine.begin() as conn:
            await _check_embedding_dim(conn)
    except Exception as e:
        logger.warning("Embedding-dimension check skipped: %s", e)

    return True


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

    # Run migrations only once per SUCCESSFUL attempt. DDL must run as the OWNER, not the app data
    # role, so it keeps working after the app DSN is cut over to a non-superuser RLS role.
    # If the database was UNREACHABLE the latch is released so a later call retries: a VAF start
    # racing the DB container must not leave the process on a stale schema for its whole lifetime
    # (live incident 2026-07-15: migrations "ran" against a down container once, were never
    # retried, and every chunk query then failed on the missing user_scope_id column).
    if run_migrations and _main_engine is not None:
        reachable = False
        try:
            reachable = await _run_schema_migrations(await get_owner_engine())
        except Exception:
            reachable = False
        if not reachable:
            with _engine_lock:
                _migrations_attempted = False

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
        # vaf_memory also holds the auth/system tables (local_users, user_sessions) accessed via the same
        # get_db connection, so grant DML on ALL tables, not just the memory ones. Only 'memories' is
        # RLS-protected below; the auth/system tables have no RLS (login needs a cross-user lookup).
        await conn.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO vaf_app"))
        await conn.execute(text("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO vaf_app"))
        await conn.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO vaf_app"))
        await conn.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO vaf_app"))

        # Row-Level Security on memories: fail-CLOSED, matching scripts/rls_enforce.sql so init_db (a fresh
        # install, or a POST /api/memory/init) produces the SAME enforced state as the cutover and can never
        # silently revert it to fail-open. A row is visible/writable ONLY when its user_scope_id equals the
        # per-transaction GUC; NULLIF maps an unset/empty GUC to NULL so an unscoped session matches nothing
        # (deny) and NULL-scoped rows are not blanket-visible. Enforcement applies to the app role (vaf_app,
        # NOSUPERUSER/NOBYPASSRLS); the superuser owner (vaf) still bypasses for cross-user stats/DDL.
        await conn.execute(text("ALTER TABLE memories ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text("ALTER TABLE memories FORCE ROW LEVEL SECURITY"))
        await conn.execute(text("DROP POLICY IF EXISTS user_isolation_memories ON memories"))
        await conn.execute(text("""
            CREATE POLICY user_isolation_memories ON memories
                USING      (user_scope_id = NULLIF(current_setting('app.current_user_scope_id', true), '')::uuid)
                WITH CHECK (user_scope_id = NULLIF(current_setting('app.current_user_scope_id', true), '')::uuid)
        """))

        # Chunks carry the searchable plaintext and the (invertible) embedding
        # vectors - they get the SAME fail-closed forced policy (mirrors
        # scripts/rls_enforce.sql and db_migrations v2, which also backfills
        # the scope column on existing installs).
        await conn.execute(text("ALTER TABLE chunks ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text("ALTER TABLE chunks FORCE ROW LEVEL SECURITY"))
        await conn.execute(text("DROP POLICY IF EXISTS user_isolation_chunks ON chunks"))
        await conn.execute(text("""
            CREATE POLICY user_isolation_chunks ON chunks
                USING      (user_scope_id = NULLIF(current_setting('app.current_user_scope_id', true), '')::uuid)
                WITH CHECK (user_scope_id = NULLIF(current_setting('app.current_user_scope_id', true), '')::uuid)
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


async def get_db_stats(user_scope_id: Optional[str] = None) -> dict:
    """
    Memory statistics for the CURRENT USER.

    Runs on the app engine with the scope GUC set, so the memories count is RLS-filtered and the chunk /
    connection counts JOIN memories so they are scoped too (chunks DO carry their own forced RLS policy
    since migration v2; connections have none and are scoped only via the JOIN).
    A missing scope yields zeros (fail-closed), never global totals.

    Returns:
        Dict with memory count, chunk count, connection count
    """
    from vaf.memory.models import Memory, Chunk, Connection
    from sqlalchemy import func, select

    async with get_db(user_scope_id=str(user_scope_id) if user_scope_id else None) as db:
        memory_count = await db.scalar(
            select(func.count()).select_from(Memory).where(Memory.is_deleted == False)
        )
        # JOIN chunks/connections to memories so RLS-on-memories scopes them (no RLS on the child tables).
        chunk_count = await db.scalar(
            select(func.count()).select_from(Chunk).join(Memory, Chunk.memory_id == Memory.id)
        )
        connection_count = await db.scalar(
            select(func.count()).select_from(Connection).join(Memory, Connection.source_id == Memory.id)
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


async def get_admin_isolation_metrics() -> dict:
    """ADMIN-ONLY aggregate metrics across ALL user scopes.

    Runs on the owner engine (the documented lane for global stats) and returns
    operational numbers for the security dashboard's isolation module:
    how many isolated per-user stores exist, how big each is, the total DB
    size, and a live RAG ANN-search latency probe (a real pgvector distance
    query using an existing embedding as the probe vector, so no embedder is
    involved and the number reflects pure retrieval latency).

    Never raises; individual metrics degrade to None/empty on failure. The
    caller (GET /api/security/overview) is admin-gated - these aggregates are
    cross-scope METADATA (counts/sizes, never content) and must not be exposed
    on any per-user route.
    """
    from sqlalchemy import text
    import time as _time

    out: dict = {"scope_count": 0, "scopes": [], "db_size_bytes": None, "rag_probe_ms": None}
    try:
        engine = await get_owner_engine()
        async with engine.connect() as conn:
            try:
                rows = (await conn.execute(text(
                    "SELECT COALESCE(m.user_scope_id::text, '') AS scope, "
                    "       COUNT(*) AS memories, "
                    "       COALESCE(SUM(c.cnt), 0) AS chunks "
                    "FROM memories m "
                    "LEFT JOIN (SELECT memory_id, COUNT(*) AS cnt FROM chunks GROUP BY memory_id) c "
                    "       ON c.memory_id = m.id "
                    "WHERE m.is_deleted = false "
                    "GROUP BY m.user_scope_id ORDER BY COUNT(*) DESC"
                ))).fetchall()
                # Full scope ids here; the admin route maps them to usernames and
                # shortens for display.
                out["scopes"] = [
                    {"scope": (r.scope or "") or "unscoped", "memories": int(r.memories or 0), "chunks": int(r.chunks or 0)}
                    for r in rows
                ]
                out["scope_count"] = len(out["scopes"])
            except Exception:
                pass
            try:
                out["db_size_bytes"] = int(await conn.scalar(text("SELECT pg_database_size(current_database())")))
            except Exception:
                pass
            try:
                probe = await conn.execute(text(
                    "SELECT embedding FROM chunks WHERE embedding IS NOT NULL LIMIT 1"
                ))
                if probe.first() is not None:
                    t0 = _time.perf_counter()
                    await conn.execute(text(
                        "SELECT c.id FROM chunks c "
                        "WHERE c.embedding IS NOT NULL "
                        "ORDER BY c.embedding <=> (SELECT embedding FROM chunks WHERE embedding IS NOT NULL LIMIT 1) "
                        "LIMIT 5"
                    ))
                    out["rag_probe_ms"] = round((_time.perf_counter() - t0) * 1000.0, 1)
            except Exception:
                pass
    except Exception:
        pass
    return out
