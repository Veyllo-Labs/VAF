# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""DB schema reconcile — keeps an existing user's Postgres in step with the models after `vaf update`.

The verified gap: SQLAlchemy create_all only creates NEW tables, never ALTERs an existing one, so a
new column on memories/chunks/local_users would make queries fail after an update. These tests pin
the generic "add missing columns" reconcile, the ordered explicit-migration seam, and the loud
embedding-dimension mismatch check — all without a live Postgres (async via asyncio.run + fakes).
"""
import asyncio
import logging

import vaf.auth.models  # noqa: F401 - register auth tables on the shared Base
from vaf.memory.database import _columns_to_add, _reconcile_columns, _check_embedding_dim
from vaf.memory.models import Base, EMBEDDING_DIM


# --- pure column diff -----------------------------------------------------------------------------

def test_columns_to_add_pure():
    t = Base.metadata.tables["memories"]
    allcols = {c.name for c in t.columns}
    assert _columns_to_add(t, allcols) == []                      # in sync -> nothing
    add = dict(_columns_to_add(t, allcols - {"user_scope_id", "embedding"}))
    assert add["user_scope_id"] == "UUID"                          # missing cols come back with DDL
    assert add["embedding"] == "VECTOR(384)"                       # pgvector type renders correctly
    assert _columns_to_add(t, {c.upper() for c in allcols}) == []  # existing-name match is case-insensitive


def test_columns_to_add_covers_auth_tables():
    # auth tables share the same Base -> the reconcile covers them too
    cols = dict(_columns_to_add(Base.metadata.tables["local_users"], set()))
    assert "user_scope_id" in cols and "username" in cols


# --- async fakes (no live DB) ---------------------------------------------------------------------

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _ReconcileConn:
    """Fake AsyncConnection for _reconcile_columns: answers the two information_schema queries and
    records every ALTER it is asked to run."""
    def __init__(self, live_tables, columns):
        self.live_tables = set(live_tables)
        self.columns = columns
        self.executed = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append((sql, params))
        if "information_schema.tables" in sql:
            return _Result([(t,) for t in self.live_tables])
        if "information_schema.columns" in sql:
            t = (params or {}).get("t")
            return _Result([(c,) for c in self.columns.get(t, set())])
        return _Result([])


def test_reconcile_adds_only_missing_columns_on_existing_tables():
    cols = {c.name for c in Base.metadata.tables["memories"].columns} - {"user_scope_id"}
    conn = _ReconcileConn(live_tables={"memories"}, columns={"memories": cols})
    added = asyncio.run(_reconcile_columns(conn))
    assert added == {"memories": ["user_scope_id"]}
    assert any('ADD COLUMN IF NOT EXISTS "user_scope_id"' in sql for sql, _ in conn.executed)


def test_reconcile_noop_when_in_sync():
    full = {c.name for c in Base.metadata.tables["memories"].columns}
    conn = _ReconcileConn(live_tables={"memories"}, columns={"memories": full})
    added = asyncio.run(_reconcile_columns(conn))
    assert added == {}
    assert not any("ALTER TABLE" in sql for sql, _ in conn.executed)


def test_reconcile_skips_tables_that_do_not_exist_yet():
    # No live tables -> create_all will make them; reconcile must touch nothing.
    conn = _ReconcileConn(live_tables=set(), columns={})
    added = asyncio.run(_reconcile_columns(conn))
    assert added == {}
    assert not any("ALTER TABLE" in sql for sql, _ in conn.executed)


# --- ordered migration seam -----------------------------------------------------------------------

class _RecordingConn:
    def __init__(self):
        self.sqls = []

    async def execute(self, stmt, params=None):
        self.sqls.append(str(stmt))
        return _Result([])


def test_db_migrations_shape_and_run():
    from vaf.memory import db_migrations as dbm
    assert isinstance(dbm.DB_MIGRATIONS, list) and dbm.DB_MIGRATIONS
    assert dbm.DB_MIGRATIONS[0][0] == 1                            # user_scope_id is the first migration
    conn = _RecordingConn()
    applied = asyncio.run(dbm.run_db_migrations(conn))
    assert applied == [v for v, _ in dbm.DB_MIGRATIONS]            # every (idempotent) migration runs
    assert any("user_scope_id" in sql for sql in conn.sqls)


# --- embedding-dimension mismatch (loud, no auto-fix) ---------------------------------------------

class _DimConn:
    def __init__(self, rendered):
        self._rendered = rendered

    async def execute(self, stmt, params=None):
        return _Result([(self._rendered,)] if self._rendered else [])


def test_embedding_dim_mismatch_logs_error(caplog):
    with caplog.at_level(logging.ERROR):
        asyncio.run(_check_embedding_dim(_DimConn("vector(128)")))   # live 128 != EMBEDDING_DIM (384)
    assert "DIMENSION MISMATCH" in caplog.text


def test_embedding_dim_match_is_silent(caplog):
    with caplog.at_level(logging.ERROR):
        asyncio.run(_check_embedding_dim(_DimConn(f"vector({EMBEDDING_DIM})")))
    assert "MISMATCH" not in caplog.text
