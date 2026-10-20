# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Sessions: every token is its own, and ending one ends it.

Measured before: two sign-ins of one account in the same second minted byte-identical tokens
(no `jti`), and the second session row failed on the unique token hash: a double-submitted
2FA code answered 500.
"""
import re
from datetime import datetime as _real_datetime, timezone

import pytest

from auth_harness import Harness


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


@pytest.fixture
def frozen_second(monkeypatch):
    """Every token minted in the same second."""
    from vaf.auth import crypto
    fixed = _real_datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)

    class _Frozen(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(crypto, "datetime", _Frozen)


def test_tokens_are_unique_per_mint(frozen_second, monkeypatch):
    """MUTATION: remove the jti claim - red."""
    from vaf.auth import crypto
    monkeypatch.setattr(crypto, "_get_jwt_secret", lambda: "s" * 48)
    a = crypto.create_access_token("u1", "alice", "user", "s1", is_2fa_verified=True)
    b = crypto.create_access_token("u1", "alice", "user", "s1", is_2fa_verified=True)
    assert a != b
    pa, pb = crypto.decode_token(a), crypto.decode_token(b)
    assert pa["iat"] == pb["iat"]
    assert re.fullmatch(r"[0-9a-f]{32}", pa["jti"]) and pa["jti"] != pb["jti"]


def test_two_sign_ins_in_one_second_both_succeed(h, frozen_second):
    """The double-submitted sign-in: both answer 200 and both sessions exist."""
    from sqlalchemy.orm import Session
    h.add_user("alice", "alice-password-1")
    with Session(h.engine) as s:
        user = s.get(type(h.user("alice")), h.user("alice").id)
        user.requires_2fa_setup = False          # an account without a second factor
        s.commit()
    c = h.client("192.168.9.1")
    first = h.login(c, "alice", "alice-password-1")
    second = h.login(c, "alice", "alice-password-1")
    assert first.status_code == 200 and second.status_code == 200, second.text
    assert first.json()["access_token"] != second.json()["access_token"]
    assert len(h.sessions()) == 2


# ── ending sessions ──────────────────────────────────────────────────────────────

def _probe(h, token, ip="192.168.11.99"):
    return h.client(ip).get("/api/probe", headers={"Authorization": f"Bearer {token}"}).status_code


def _me(h, token, ip="192.168.11.99"):
    return h.client(ip).get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code


def _two_devices(h, name="alice", password="alice-password-1"):
    user_id, _ = h.add_user(name, password, no_second_factor=True)
    a = h.sign_in(h.client("192.168.11.1"), name, password, None)
    b = h.sign_in(h.client("192.168.11.2"), name, password, None)
    assert _probe(h, a) == 200 and _probe(h, b) == 200
    return user_id, a, b


def test_logout_ends_the_session(h):
    """Logging out deleted the cookie and nothing else (measured: the token still answered 200
    on the API and on /me). MUTATION: logout deletes only the cookie - red."""
    _id, a, b = _two_devices(h)
    c = h.client("192.168.11.1")
    assert c.post("/api/auth/logout", headers={"Authorization": f"Bearer {a}"}).status_code == 200
    assert _probe(h, a) == 401 and _me(h, a) == 401
    assert _probe(h, b) == 200 and _me(h, b) == 200


def test_a_password_change_ends_the_others(h):
    """The session that changed it stays; every other one ends.
    MUTATION: drop the end_account_sessions call - red."""
    _id, a, b = _two_devices(h)
    r = h.client("192.168.11.1").post(
        "/api/auth/change-password", headers={"Authorization": f"Bearer {a}"},
        json={"current_password": "alice-password-1", "new_password": "alice-password-2"})
    assert r.status_code == 200, r.text
    assert _probe(h, a) == 200
    assert _probe(h, b) == 401


def test_admin_resets_end_every_session(h):
    """MUTATION: drop either end_account_sessions call in the admin routes - red."""
    user_id, a, b = _two_devices(h)
    admin = h.client(None)          # the machine owner, tokenless
    r = admin.post(f"/api/users/{user_id}/reset-password")
    assert r.status_code == 200, r.text
    assert _probe(h, a) == 401 and _probe(h, b) == 401
    c = h.sign_in(h.client("192.168.11.3"), "alice", r.json()["temporary_password"], None)
    assert _probe(h, c) == 200
    assert admin.post(f"/api/users/{user_id}/reset-2fa").status_code == 200
    assert _probe(h, c) == 401


def test_deactivation_ends_sessions(h):
    """And a reactivation revives none of them. MUTATION: no ending on deactivation - red."""
    from vaf.core import revocation
    user_id, a, _b = _two_devices(h)
    scope = str(h.user("alice").user_scope_id)
    admin = h.client(None)
    try:
        assert admin.put(f"/api/users/{user_id}", json={"is_active": False}).status_code == 200
        assert _probe(h, a) == 401
        assert admin.put(f"/api/users/{user_id}", json={"is_active": True}).status_code == 200
        assert _probe(h, a) == 401
    finally:
        revocation.restore_account(scope)


def test_the_terminal_reset_ends_sessions(h):
    """`vaf users reset-password` ends every session of the account.
    MUTATION: drop the call in reset_password_from_terminal - red."""
    from vaf.auth.user_admin import find_user, reset_password_from_terminal
    _id, a, b = _two_devices(h)

    async def _reset(db):
        user = await find_user(db, "alice")
        await reset_password_from_terminal(db, user, "alice-password-9")

    h.run_db(_reset)
    assert _probe(h, a) == 401 and _probe(h, b) == 401


def test_resets_close_the_accounts_sockets(h, monkeypatch):
    """A socket keeps the identity it was opened with. MUTATION: no close call - red."""
    from vaf.core.web_interface import WebInterfaceManager
    closed = []
    monkeypatch.setattr(WebInterfaceManager, "close_account_connections",
                        lambda self, scope: closed.append(scope))
    user_id, a, _b = _two_devices(h)
    scope = str(h.user("alice").user_scope_id)
    r = h.client("192.168.11.1").post(
        "/api/auth/change-password", headers={"Authorization": f"Bearer {a}"},
        json={"current_password": "alice-password-1", "new_password": "alice-password-2"})
    assert r.status_code == 200
    admin = h.client(None)
    assert admin.post(f"/api/users/{user_id}/reset-password").status_code == 200
    assert admin.post(f"/api/users/{user_id}/reset-2fa").status_code == 200
    assert closed == [scope, scope, scope]


def test_sessions_have_one_writer():
    """Three routes each built the row by hand; one module owns it now.
    MUTATION: construct a UserSession in a route again - red."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sites = []
    for path in sorted((root / "vaf").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call)
                    and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == "UserSession"):
                sites.append(path.relative_to(root).as_posix())
    assert sites == ["vaf/auth/sessions.py"], sites


# ── the 2FA steps ask the session and the password ───────────────────────────────

def _setup_2fa(h, token, ip="192.168.13.9"):
    return h.client(ip).post("/api/auth/setup-2fa", headers={"Authorization": f"Bearer {token}"})


def test_an_ended_token_cannot_enroll_a_second_factor(h):
    """The two 2FA routes are exempt from the middleware and checked only the signature, so a
    token whose session an admin's 2FA reset had ended enrolled the thief's own second factor
    and came back as a live session. MUTATION: drop the standing check in
    _second_factor_step_token - red."""
    import auth_harness
    user_id, secret = h.add_user("bob", "bob-password-123", totp=True)
    stolen = h.sign_in(h.client("192.168.13.1"), "bob", "bob-password-123", secret)
    assert h.client(None).post(f"/api/users/{user_id}/reset-2fa").status_code == 200
    assert _probe(h, stolen) == 401
    assert _setup_2fa(h, stolen).status_code == 401
    thief_secret = auth_harness.pyotp.random_base32()
    r = h.verify(h.client("192.168.13.2"), stolen, auth_harness.pyotp.TOTP(thief_secret).now())
    assert r.status_code == 401


def test_a_logged_out_token_without_a_second_factor_stays_out(h):
    """An account without a second factor: after a logout its token got a new session through
    setup-2fa and verify-2fa. MUTATION: drop the standing check - red."""
    _id, a, _b = _two_devices(h)
    h.client("192.168.11.1").post("/api/auth/logout", headers={"Authorization": f"Bearer {a}"})
    assert _setup_2fa(h, a).status_code == 401
    assert h.verify(h.client("192.168.13.3"), a, "123456").status_code == 401


def test_a_temp_token_from_before_a_reset_is_refused(h):
    """Somebody with the old password holds a temp token; the owner resets the password in a
    terminal (which also clears the second factor). That token must not enroll a new one.
    MUTATION: drop the password binding (_minted_with_current_password) - red."""
    import auth_harness
    from vaf.auth.user_admin import find_user, reset_password_from_terminal
    _id, secret = h.add_user("alice", "alice-password-1", totp=True)
    temp = h.login(h.client("192.168.13.4"), "alice", "alice-password-1").json()["temp_token"]

    async def _reset(db):
        await reset_password_from_terminal(db, await find_user(db, "alice"), "alice-password-9")

    h.run_db(_reset)
    assert _setup_2fa(h, temp).status_code == 401
    thief_secret = auth_harness.pyotp.random_base32()
    r = h.verify(h.client("192.168.13.5"), temp, auth_harness.pyotp.TOTP(thief_secret).now())
    assert r.status_code == 401
    # The owner, with the new password, gets a fresh temp token that works.
    fresh = h.login(h.client("192.168.13.6"), "alice", "alice-password-9").json()
    assert fresh["needs_2fa_setup"] is True
    assert _setup_2fa(h, fresh["temp_token"]).status_code == 200


def test_the_wizard_still_enrolls_with_the_bootstrap_token(h, monkeypatch):
    """The first-run wizard enrolls with the bootstrap's own token (it has a live session)."""
    import auth_harness
    from vaf.auth import account_seal, user_admin
    from vaf.core.config import Config
    saved = {}
    monkeypatch.setattr(account_seal, "admin_established", lambda: False)
    monkeypatch.setattr(account_seal, "mark_admin_established", lambda: None)
    monkeypatch.setattr(Config, "load", staticmethod(lambda *a, **k: dict(saved)))
    monkeypatch.setattr(Config, "save", staticmethod(lambda c, *a, **k: saved.update(c)))
    monkeypatch.setattr(user_admin, "_prepare_user_home", lambda *a, **k: None)
    local = h.client(None)
    token = local.post("/api/auth/bootstrap",
                       json={"username": "owner", "password": "owner-password-1"}).json()["access_token"]
    r = local.post("/api/auth/setup-2fa", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    code = auth_harness.pyotp.TOTP(r.json()["secret"]).now()
    assert h.verify(local, token, code).status_code == 200


# ── open sockets end with their sessions ─────────────────────────────────────────

def test_the_sign_out_route_ends_every_session_and_closes_the_sockets(h, monkeypatch):
    """What `vaf users reset-password` asks the running service: only it holds the sockets.
    MUTATION: drop the close, or the ending, in the route - red."""
    from vaf.core.web_interface import WebInterfaceManager
    closed = []
    monkeypatch.setattr(WebInterfaceManager, "close_account_connections",
                        lambda self, scope: closed.append(scope))
    user_id, a, b = _two_devices(h)
    r = h.client(None).post(f"/api/users/{user_id}/sign-out")
    assert r.status_code == 200 and r.json() == {"ended": 2}
    assert _probe(h, a) == 401 and _probe(h, b) == 401
    assert closed == [str(h.user("alice").user_scope_id)]
    # Admin only.
    c = h.sign_in(h.client("192.168.14.1"), "alice", "alice-password-1", None)
    r = h.client("192.168.14.1").post(f"/api/users/{user_id}/sign-out",
                                      headers={"Authorization": f"Bearer {c}"})
    assert r.status_code == 403


def test_logout_closes_the_sockets_of_its_tokens(h, monkeypatch):
    """A socket is checked only at its handshake: a logout that ends the session must close
    the sockets opened with it. MUTATION: no close in the logout - red."""
    from vaf.auth.crypto import token_hash_for_db
    from vaf.core.web_interface import WebInterfaceManager
    closed = []
    monkeypatch.setattr(WebInterfaceManager, "close_session_connections",
                        lambda self, token_hash: closed.append(token_hash))
    _id, a, b = _two_devices(h)
    c = h.client("192.168.11.1")
    c.cookies.set("vaf_token", b)
    assert c.post("/api/auth/logout", headers={"Authorization": f"Bearer {a}"}).status_code == 200
    assert sorted(closed) == sorted([token_hash_for_db(a), token_hash_for_db(b)])


def test_the_manager_closes_only_the_sockets_of_one_token():
    import asyncio

    from vaf.core.web_interface import WebInterfaceManager
    mgr = WebInterfaceManager()

    class _WS:
        def __init__(self):
            self.closed = None

        async def close(self, code=1000, reason=""):
            self.closed = code

    mine, other = _WS(), _WS()
    try:
        mgr.set_connection_token(mine, "hash-a")
        mgr.set_connection_token(other, "hash-b")
        assert asyncio.run(mgr.close_token_connections("hash-a")) == 1
        assert mine.closed == 4001 and other.closed is None
        assert mine not in mgr.connection_tokens and other in mgr.connection_tokens
    finally:
        mgr.connection_tokens.pop(mine, None)
        mgr.connection_tokens.pop(other, None)


def test_the_socket_handshake_remembers_its_token():
    """The two token lanes of /ws record which token opened the socket; the local-admin
    fallback has none. MUTATION: drop the record - red."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "web_server.py").read_text(
        encoding="utf-8")
    handshake = src[src.index("async def websocket_endpoint"):]
    handshake = handshake[:handshake.index("elif type == ")]
    assert handshake.count('"token_hash": token_hash_for_db(token),') == 2
    assert 'manager.set_connection_token(websocket, user_context.get("token_hash") or "")' in handshake
