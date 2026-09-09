# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""DELETE /api/memory/chat/{chat_key} empties one chat's memory namespace (vaf/memory/routes.py).

Hard, scoped, fail-closed: the route refuses a missing scope instead of passing None down
(the pipeline would read that as the legacy NULL-scope rows), rejects a malformed key, and
invalidates the graph cache so the node is gone on the next fetch. Declared before the
/{memory_id} catch-all, or the catch-all would swallow it as an invalid UUID.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from vaf.memory import routes

_ROUTES = Path(__file__).resolve().parent.parent / "vaf" / "memory" / "routes.py"


class _FakeGetDb:
    def __init__(self, user_scope_id=None):
        _FakeGetDb.scopes.append(user_scope_id)

    scopes = []

    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePipeline:
    cleared = []

    def __init__(self, db):
        pass

    async def clear_chat_namespace(self, chat_key, user_scope_id):
        _FakePipeline.cleared.append((chat_key, user_scope_id))
        return 3


def _arm(monkeypatch):
    _FakeGetDb.scopes = []
    _FakePipeline.cleared = []
    invalidated = []

    async def _invalidate():
        invalidated.append(True)
    monkeypatch.setattr(routes, "get_db", _FakeGetDb)
    monkeypatch.setattr(routes, "RagPipeline", _FakePipeline)
    monkeypatch.setattr(routes, "get_cache", lambda: SimpleNamespace(invalidate_graph=_invalidate))
    return invalidated


def test_the_route_is_declared_before_the_memory_id_catch_all():
    src = _ROUTES.read_text(encoding="utf-8")
    assert src.index('.delete("/chat/{chat_key}")') < src.index('.delete("/{memory_id}")')


def test_deleting_a_chat_empties_exactly_that_namespace_and_invalidates_the_graph(monkeypatch):
    invalidated = _arm(monkeypatch)
    scope = uuid4()
    out = asyncio.run(routes.delete_chat_namespace("whatsapp_alice_49", user_scope_id=scope))
    assert out == {"status": "deleted", "chat_key": "whatsapp_alice_49", "count": 3, "hard": True}
    assert _FakePipeline.cleared == [("whatsapp_alice_49", scope)]
    assert _FakeGetDb.scopes == [scope] and invalidated == [True]


def test_a_missing_scope_or_a_malformed_key_is_refused(monkeypatch):
    _arm(monkeypatch)
    with pytest.raises(HTTPException) as no_scope:
        asyncio.run(routes.delete_chat_namespace("whatsapp_alice_49", user_scope_id=None))
    assert no_scope.value.status_code == 403
    with pytest.raises(HTTPException) as bad_key:
        asyncio.run(routes.delete_chat_namespace("alice 49/../x", user_scope_id=uuid4()))
    assert bad_key.value.status_code == 400
    assert _FakePipeline.cleared == []
