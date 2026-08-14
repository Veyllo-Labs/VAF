# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The wizard asks what the agent is called, the same way the terminal setup does.

The framework half existed the whole time - create_first_admin carries agent_name
and writes it into the workspace identity the system prompt reads - so what is
pinned here is the WIRING the web was missing: the suggestion travels out with
needs-setup, and the chosen name travels back in through bootstrap. Hermetic, the
same way the startup-race tests are: a tiny app, the DB context patched, no
Docker, no web_server import.
"""
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.testclient import TestClient

import vaf.api.auth_routes as auth_routes


def _client(monkeypatch, db_ctx) -> TestClient:
    monkeypatch.setattr(auth_routes, "get_auth_db", db_ctx)
    app = FastAPI()
    app.include_router(auth_routes.router)
    return TestClient(app, raise_server_exceptions=False)


class _AdminResult:
    def scalar_one_or_none(self):
        return object()


class _NoAdminResult:
    def scalar_one_or_none(self):
        return None


def _db(result):
    class _Db:
        async def execute(self, *_a, **_k):
            return result

        def add(self, *_a, **_k):
            pass

        async def commit(self):
            pass

    @asynccontextmanager
    async def ctx(*a, **k):
        yield _Db()

    return ctx


def test_setup_mode_offers_a_name_and_normal_mode_does_not(monkeypatch):
    """MUTATION: mint the suggestion in JavaScript instead.

    The suggestion's shape belongs to suggest_agent_name - the same one the
    terminal setup offers - and it travels with the needs-setup answer so no
    second copy of it can drift. Once an admin exists the field stays out:
    the login page has no business receiving name material.
    """
    c = _client(monkeypatch, _db(_NoAdminResult()))
    data = c.get("/api/auth/needs-setup").json()
    assert data["needs_setup"] is True
    assert isinstance(data.get("agent_name_suggestion"), str)
    assert data["agent_name_suggestion"].strip()

    c2 = _client(monkeypatch, _db(_AdminResult()))
    assert c2.get("/api/auth/needs-setup").json() == {"needs_setup": False}


def _recorder(monkeypatch):
    """Capture what bootstrap hands to create_first_admin, answering as it would."""
    import vaf.auth.user_admin as user_admin

    calls = {}

    async def fake_create_first_admin(_db, *, username, password, agent_name=None):
        calls["agent_name"] = agent_name
        class _User:
            id = uuid.uuid4()
            user_scope_id = uuid.uuid4()
            requires_2fa_setup = True
        _User.username = username
        _User.role = "admin"
        return _User()

    monkeypatch.setattr(user_admin, "create_first_admin", fake_create_first_admin)
    return calls


def test_bootstrap_hands_the_chosen_name_to_the_account(monkeypatch):
    """MUTATION: accept the field and never pass it on.

    That failure is silent twice over: the wizard shows the question, the user
    answers it, the account works - and the agent introduces itself under a
    random suggestion, which reads as the product ignoring the person's first
    decision about it.
    """
    calls = _recorder(monkeypatch)
    c = _client(monkeypatch, _db(_NoAdminResult()))
    r = c.post("/api/auth/bootstrap", json={
        "username": "alice", "password": "longenough1", "agent_name": "Luna",
    })
    assert r.status_code == 200, r.text
    assert calls["agent_name"] == "Luna"


def test_an_empty_name_falls_back_to_the_suggestion_inside(monkeypatch):
    """Whitespace and absence both mean 'no choice made': create_first_admin's
    workspace preparation owns the fallback, so the route must hand it None
    rather than an empty string it would faithfully store."""
    calls = _recorder(monkeypatch)
    c = _client(monkeypatch, _db(_NoAdminResult()))
    r = c.post("/api/auth/bootstrap", json={
        "username": "alice", "password": "longenough1", "agent_name": "   ",
    })
    assert r.status_code == 200, r.text
    assert calls["agent_name"] is None

    calls2 = _recorder(monkeypatch)
    r2 = c.post("/api/auth/bootstrap", json={
        "username": "bob", "password": "longenough1",
    })
    assert r2.status_code == 200, r2.text
    assert calls2["agent_name"] is None
