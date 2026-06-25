# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: the WebSocket session commands (chat/load/delete/rename/hide/artifact_edit) must enforce
ownership. Previously only load_session checked; the siblings trusted the client-supplied sessionId, so a
crafted LAN client could read/rename/delete/hide/take over another user's session.

These tests pin the shared gate `_ws_session_owner_ok`:
  - owner allowed, foreign denied;
  - admin allowed both via role and via the local-admin scope (so the desktop is never locked out);
  - STRICT legacy policy: a session with NO recorded scope is admin-only (not open to everyone);
  - missing session: allowed only with allow_missing (new-chat) or for admin, else denied.
"""
import vaf.core.web_server as ws
import vaf.core.config as cfg

ADMIN = "admin-scope-0000-0000-0000-000000000001"


class _FakeManager:
    def __init__(self, scope, role):
        self._scope, self._role = scope, role

    def get_connection_user(self, _ws):
        return self._scope

    def get_connection_user_role(self, _ws):
        return self._role


class _FakeSessionMgr:
    def __init__(self, sessions):
        self._s = sessions  # id -> metadata dict (absent id => not found)

    def load(self, sid):
        if sid not in self._s:
            raise FileNotFoundError(sid)
        return type("S", (), {"metadata": self._s[sid]})()


def _setup(monkeypatch, conn_scope, conn_role, sessions):
    monkeypatch.setattr(ws, "manager", _FakeManager(conn_scope, conn_role))
    monkeypatch.setattr(ws, "session_mgr", _FakeSessionMgr(sessions))
    monkeypatch.setattr(cfg, "get_local_admin_scope_id", lambda: ADMIN)


def test_owner_allowed(monkeypatch):
    _setup(monkeypatch, "userA", "user", {"s1": {"user_scope_id": "userA"}})
    assert ws._ws_session_owner_ok(None, "s1")[0] is True


def test_foreign_denied(monkeypatch):
    _setup(monkeypatch, "userB", "user", {"s1": {"user_scope_id": "userA"}})
    assert ws._ws_session_owner_ok(None, "s1")[0] is False


def test_admin_by_role_allowed(monkeypatch):
    _setup(monkeypatch, "userB", "admin", {"s1": {"user_scope_id": "userA"}})
    assert ws._ws_session_owner_ok(None, "s1")[0] is True


def test_admin_by_scope_allowed(monkeypatch):
    _setup(monkeypatch, ADMIN, "user", {"s1": {"user_scope_id": "userA"}})
    assert ws._ws_session_owner_ok(None, "s1")[0] is True


def test_no_scope_session_denied_for_nonadmin_strict(monkeypatch):
    # STRICT: a legacy session with no recorded scope is NOT open to a non-admin.
    _setup(monkeypatch, "userA", "user", {"s1": {}})
    assert ws._ws_session_owner_ok(None, "s1")[0] is False


def test_no_scope_session_allowed_for_admin(monkeypatch):
    _setup(monkeypatch, ADMIN, "user", {"s1": {}})
    assert ws._ws_session_owner_ok(None, "s1")[0] is True


def test_missing_allowed_for_new_chat(monkeypatch):
    # First message into a brand-new session id must pass.
    _setup(monkeypatch, "userA", "user", {})
    assert ws._ws_session_owner_ok(None, "web-default-userA", allow_missing=True)[0] is True


def test_missing_mutate_denied_for_nonadmin(monkeypatch):
    # delete/rename/hide on a non-existent id (no allow_missing) deny for a non-admin.
    _setup(monkeypatch, "userA", "user", {})
    assert ws._ws_session_owner_ok(None, "ghost")[0] is False


def test_missing_allowed_for_admin(monkeypatch):
    _setup(monkeypatch, ADMIN, "user", {})
    assert ws._ws_session_owner_ok(None, "ghost")[0] is True
