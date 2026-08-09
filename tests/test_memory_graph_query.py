# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""GraphManager.get_graph_data query wiring (vaf/memory/graph.py).

Pins, each against its failure mode:
- limit=0 compiles WITHOUT a LIMIT clause (ALL memories; the old 100-node
  recency window hid every older memory once a learned document filled it),
- a positive limit still compiles WITH one,
- include_deleted=False filters to live rows; True returns live AND deleted
  (the old `is_deleted == include_deleted` returned ONLY deleted rows),
- a user_scope_id lands as an explicit WHERE (second belt next to RLS),
- node payloads carry docTag (the frontend clusters document sections by it).
"""
import asyncio
import uuid
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from vaf.memory.graph import GraphManager


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def unique(self):
        return self

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _SpyDb:
    """Records compiled SQL; returns rows per call (memories first, then connections)."""

    def __init__(self, rows_per_call=None):
        self.sql = []
        self.rows_per_call = rows_per_call or [[]]

    async def execute(self, stmt):
        self.sql.append(str(stmt.compile(dialect=postgresql.dialect())))
        rows = self.rows_per_call[min(len(self.sql) - 1, len(self.rows_per_call) - 1)]
        return _Result(rows)


def _first_sql(**kwargs) -> str:
    db = _SpyDb()
    asyncio.run(GraphManager(db).get_graph_data(**kwargs))
    return db.sql[0]


def _where(**kwargs) -> str:
    """The WHERE clause alone - the projected column list always names every
    column, so asserting on the full SQL matches the wrong thing."""
    sql = _first_sql(**kwargs)
    return sql.split("WHERE", 1)[1] if "WHERE" in sql else ""


def test_limit_zero_means_all():
    assert " LIMIT " not in _first_sql(limit=0)
    assert " LIMIT " in _first_sql(limit=100)


def test_deleted_filter_is_live_by_default_and_additive_on_request():
    assert "is_deleted = false" in _where(limit=0)
    assert "is_deleted" not in _where(limit=0, include_deleted=True)


def test_scope_filter_is_an_explicit_where():
    scope = uuid.uuid4()
    assert "user_scope_id" in _where(limit=0, user_scope_id=scope)
    assert "user_scope_id" not in _where(limit=0)


def test_nodes_carry_doc_tag():
    mem = SimpleNamespace(
        id=uuid.uuid4(),
        meta={"title": "Sektion", "type": "document", "doc_tag": "doc-buch",
              "tags": []},
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 2),
        chunks=[],
        parent_id=None,
    )
    db = _SpyDb(rows_per_call=[[mem], []])
    data = asyncio.run(GraphManager(db).get_graph_data(limit=0))
    assert data["nodes"][0]["data"]["docTag"] == "doc-buch"
