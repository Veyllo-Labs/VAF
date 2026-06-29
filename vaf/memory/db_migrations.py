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

DB_SCHEMA_VERSION = 1


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


# Applied in order on every startup; each must be idempotent (see module docstring).
DB_MIGRATIONS: List[Tuple[int, Callable[[object], Awaitable[None]]]] = [
    (1, _v1_memories_user_scope_id),
]


async def run_db_migrations(conn) -> List[int]:
    """Run every ordered migration (idempotent) against ``conn``. Returns the versions run."""
    applied: List[int] = []
    for version, fn in DB_MIGRATIONS:
        await fn(conn)
        applied.append(version)
    return applied
