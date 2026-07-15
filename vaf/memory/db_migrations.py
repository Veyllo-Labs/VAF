# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Ordered, idempotent DB schema migrations (PostgreSQL).

This is the explicit seam for NON-additive schema changes — adding an index to a new column,
renaming a column, a data backfill — that the generic "add missing columns" reconcile in
``vaf/memory/database.py`` cannot express. A plain new *column* on an existing model needs NO entry
here: the reconcile adds it automatically.

Mirrors the config-migration shape in ``vaf/core/migrations.py``. Rules for a migration
``fn(conn) -> Awaitable`` (an open AsyncConnection in a transaction):
  - **idempotent**: every migration runs on EVERY startup (we do not track an applied version —
    the live DB is the source of truth), so each must be a no-op the second time. Use
    ``IF NOT EXISTS`` / ``DO $$ ... IF NOT EXISTS ... $$`` existence guards.
  - **additive / backward-safe**: a user may roll back after updating; old code must still read the
    schema. Do not drop or rename a column an older VAF still selects.

``DB_SCHEMA_VERSION`` is informational only (no version gating) — the idempotency guards above are
what make re-running safe.
"""
from typing import Awaitable, Callable, List, Tuple

from sqlalchemy import text

DB_SCHEMA_VERSION = 3


async def _v1_memories_user_scope_id(conn) -> None:
    """Add memories.user_scope_id (+ its index) to a DB created before multi-tenancy.

    This is NON-additive-only because it also creates an index — hence an explicit migration rather
    than the generic column reconcile. Guarded so it is a no-op once applied.
    """
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


async def _v2_chunks_user_scope_id_rls(conn) -> None:
    """Chunks carry the searchable PLAINTEXT text and the embedding vectors
    (which are practically invertible back to text), but had neither a scope
    column nor RLS - the memories-table isolation did not protect what RAG
    actually reads. This adds chunks.user_scope_id (backfilled from the
    parent), an index, and the same fail-closed forced RLS policy as
    memories. Runs as the owner/superuser connection, so the backfill sees
    all parents despite their RLS. Idempotent: column/index guarded, the
    backfill only touches NULL rows, policy is drop-and-recreate.
    """
    await conn.execute(text(
        "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS user_scope_id UUID NULL"))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_chunks_user_scope_id ON chunks (user_scope_id)"))
    await conn.execute(text("""
        UPDATE chunks c SET user_scope_id = m.user_scope_id
        FROM memories m
        WHERE m.id = c.memory_id AND c.user_scope_id IS NULL
    """))
    await conn.execute(text("ALTER TABLE chunks ENABLE ROW LEVEL SECURITY"))
    await conn.execute(text("ALTER TABLE chunks FORCE ROW LEVEL SECURITY"))
    await conn.execute(text("DROP POLICY IF EXISTS user_isolation_chunks ON chunks"))
    await conn.execute(text("""
        CREATE POLICY user_isolation_chunks ON chunks
            USING      (user_scope_id = NULLIF(current_setting('app.current_user_scope_id', true), '')::uuid)
            WITH CHECK (user_scope_id = NULLIF(current_setting('app.current_user_scope_id', true), '')::uuid)
    """))


async def _v3_encrypt_chunks_and_strip_meta_leaks(conn) -> None:
    """Encrypt legacy plaintext chunk texts and remove the unencrypted
    content copies from memory meta.

    - chunks.text: rows without the "enc:gcm:" prefix are encrypted in place
      (embeddings untouched - they were computed from the same plaintext).
    - meta.preview: an unencrypted first-200-chars copy of the content with
      zero consumers - dropped everywhere.
    - meta.title of type=conversation memories: the default title used to be
      the first 50 chars of content, which for short fact memories WAS the
      whole fact in plaintext - reset to a neutral dated label. Caller-
      provided titles on other types are kept.
    Idempotent: prefix/key checks guard every step. Requires the memory
    encryption key from config (same process as the app).
    """
    from sqlalchemy import text as _sql
    from vaf.memory.crypto import encrypt_field, FIELD_PREFIX

    rows = (await conn.execute(_sql(
        "SELECT id, text FROM chunks WHERE text NOT LIKE :pfx"),
        {"pfx": FIELD_PREFIX + "%"})).all()
    for cid, plain in rows:
        if not plain:
            continue
        await conn.execute(
            _sql("UPDATE chunks SET text = :t WHERE id = :i"),
            {"t": encrypt_field(plain), "i": cid})

    await conn.execute(_sql("""
        UPDATE memories SET meta = meta - 'preview'
        WHERE meta ? 'preview'
    """))
    await conn.execute(_sql("""
        UPDATE memories
        SET meta = jsonb_set(meta, '{title}',
                             to_jsonb('Memory ' || to_char(created_at, 'YYYY-MM-DD HH24:MI')))
        WHERE meta->>'type' = 'conversation'
          AND meta->>'title' NOT LIKE 'Memory %'
    """))


# Applied in order on every startup; each must be idempotent (see module docstring).
DB_MIGRATIONS: List[Tuple[int, Callable[[object], Awaitable[None]]]] = [
    (1, _v1_memories_user_scope_id),
    (2, _v2_chunks_user_scope_id_rls),
    (3, _v3_encrypt_chunks_and_strip_meta_leaks),
]


async def run_db_migrations(conn) -> List[int]:
    """Run every ordered migration (idempotent) against ``conn``. Returns the versions run."""
    applied: List[int] = []
    for version, fn in DB_MIGRATIONS:
        await fn(conn)
        applied.append(version)
    return applied
