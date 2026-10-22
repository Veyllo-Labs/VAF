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
    """Every token minted in the same second.

    The second is the real one, taken once here: the tokens are decoded against the real
    clock, so a fixed date would expire them one token lifetime after it."""
    from vaf.auth import crypto
    fixed = _real_datetime.now(timezone.utc).replace(microsecond=0)

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
    assert first.cookies.get("vaf_token") != second.cookies.get("vaf_token")
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
    # The same answer as an expired step, so the page says "sign in again" (and the old
    # token's holder learns nothing more). MUTATION: answer without the code - red.
    assert r.status_code == 401 and r.json().get("code") == "sign_in_expired", r.text
    # The owner, with the new password, gets a fresh temp token that works.
    fresh = h.login(h.client("192.168.13.6"), "alice", "alice-password-9").json()
    assert fresh["needs_2fa_setup"] is True
    assert _setup_2fa(h, fresh["temp_token"]).status_code == 200


def test_the_wizard_enrolls_with_the_temp_token_and_the_cookie(h, monkeypatch):
    """The first-run wizard on another device with no full token in JSON: the bootstrap answers
    a step token for the second factor and sets the wizard's own session as the cookie; the
    soul and the settings are saved through that cookie, the second factor is set up and
    verified with the step token, and the sign-in then ends the wizard's session and leaves a
    cookie that opens /me. MUTATION: bootstrap answers access_token again; skip ending the
    replaced session - red."""
    import auth_harness
    from vaf.auth import account_seal, user_admin
    from vaf.core.config import Config
    saved = {}
    monkeypatch.setattr(account_seal, "admin_established", lambda: False)
    monkeypatch.setattr(account_seal, "mark_admin_established", lambda: None)
    monkeypatch.setattr(Config, "load", staticmethod(lambda *a, **k: dict(saved)))
    monkeypatch.setattr(Config, "save", staticmethod(lambda c, *a, **k: saved.update(c)))
    monkeypatch.setattr(user_admin, "_prepare_user_home", lambda *a, **k: None)
    monkeypatch.setattr(user_admin, "setup_code_matches", lambda code: code == "CODE-CODE")

    from fastapi import Request

    async def _whoami(request: Request):
        return {"user": getattr(request.state, "user", None)}

    app = h.app(extra_routes=[("/api/user/soul", _whoami, ["PUT"]), ("/api/config", _whoami, ["PATCH"])])
    browser = h.client("192.168.16.1", app=app)
    r = browser.post("/api/auth/bootstrap", json={"username": "owner", "password": "owner-password-1",
                                                  "setup_code": "CODE-CODE"})
    assert r.status_code == 200, r.text
    answer = r.json()
    assert "access_token" not in answer and answer["temp_token"]
    wizard_session = r.cookies.get("vaf_token")
    assert wizard_session
    for method, path in (("PUT", "/api/user/soul"), ("PATCH", "/api/config")):
        saved_with = browser.request(method, path, json={})
        assert saved_with.status_code == 200 and saved_with.json()["user"]["username"] == "owner"
    r = browser.post("/api/auth/setup-2fa", headers={"Authorization": f"Bearer {answer['temp_token']}"})
    assert r.status_code == 200, r.text
    code = auth_harness.pyotp.TOTP(r.json()["secret"]).now()
    r = h.verify(browser, answer["temp_token"], code)
    assert r.status_code == 200 and r.json() == {"ok": True}, r.text
    signed_in = r.cookies.get("vaf_token")
    assert signed_in and signed_in != wizard_session
    assert _probe(h, wizard_session) == 401
    me = h.client("192.168.16.1", app=app)
    me.cookies.set("vaf_token", signed_in)
    assert me.get("/api/auth/me").status_code == 200


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


def test_logout_ends_every_presented_token(h):
    """The Bearer token and the cookie can differ (a tab that signed in again); a logout ends
    both. MUTATION: end only the first presented token - red."""
    _id, a, b = _two_devices(h)
    c = h.client("192.168.11.1")
    c.cookies.set("vaf_token", b)
    assert c.post("/api/auth/logout", headers={"Authorization": f"Bearer {a}"}).status_code == 200
    assert _probe(h, a) == 401
    assert _probe(h, b) == 401


# ── a refused cookie does not keep the browser in a loop ─────────────────────────

def _deletes_the_cookie(r):
    cookie = r.headers.get("set-cookie") or ""
    return "vaf_token=" in cookie and ("max-age=0" in cookie.lower() or "expires=" in cookie.lower())


def test_a_refused_cookie_is_deleted_in_the_same_answer(h):
    """The page gate (web/proxy.ts) reads only the cookie's expiry: a cookie whose session
    ended kept the browser bouncing between / and /login, because the middleware refused it
    without deleting it. MUTATION: drop the delete_cookie - red."""
    _id, a, b = _two_devices(h)
    h.client("192.168.11.1").post("/api/auth/logout", headers={"Authorization": f"Bearer {a}"})
    browser = h.client("192.168.15.1")
    browser.cookies.set("vaf_token", a)
    r = browser.get("/api/auth/me")
    assert r.status_code == 401 and _deletes_the_cookie(r), r.headers
    # The web UI sends the same token as Bearer AND cookie: the cookie goes too.
    both = h.client("192.168.15.2")
    both.cookies.set("vaf_token", a)
    r = both.get("/api/probe", headers={"Authorization": f"Bearer {a}"})
    assert r.status_code == 401 and _deletes_the_cookie(r)


def test_a_bearer_token_leaves_the_cookie_alone(h):
    """A program's own Bearer token says nothing about the browser's cookie. MUTATION: delete
    the cookie for any refused token - red."""
    _id, a, b = _two_devices(h)
    h.client("192.168.11.1").post("/api/auth/logout", headers={"Authorization": f"Bearer {a}"})
    c = h.client("192.168.15.3")
    c.cookies.set("vaf_token", b)
    r = c.get("/api/probe", headers={"Authorization": f"Bearer {a}"})
    assert r.status_code == 401 and not _deletes_the_cookie(r)
    r = h.client("192.168.15.4").get("/api/probe", headers={"Authorization": f"Bearer {a}"})
    assert r.status_code == 401 and "set-cookie" not in r.headers


def test_the_machine_itself_keeps_its_tokenless_path(h):
    """A client on this machine with an ended cookie falls through to the tokenless path, as
    before (the desktop is never locked out)."""
    _id, a, _b = _two_devices(h)
    h.client("192.168.11.1").post("/api/auth/logout", headers={"Authorization": f"Bearer {a}"})
    local = h.client(None)
    local.cookies.set("vaf_token", a)
    assert local.get("/api/probe").status_code == 200


# ── what this process ended stays ended when the store goes away ─────────────────

def _store_goes_away(monkeypatch):
    import contextlib

    import vaf.auth.database as database
    from vaf.auth import permissions, sessions

    @contextlib.asynccontextmanager
    async def _down(*_a, **_k):
        raise ConnectionError("the database is gone")
        yield  # pragma: no cover

    monkeypatch.setattr(database, "get_auth_db", _down)
    permissions.invalidate_permissions_cache()
    sessions.forget_cached_sessions()


def test_an_ended_session_stays_ended_when_the_store_cannot_be_asked(h, monkeypatch):
    """A store that cannot be asked keeps a token (a database restart must not sign every
    device out), but not one whose session this process ended: a burst that takes the
    connections must not let a signed-out token back in. MUTATION: ask the store before the
    remembered endings, or stop remembering them - red."""
    _id, a, b = _two_devices(h)
    h.client("192.168.11.1").post("/api/auth/logout", headers={"Authorization": f"Bearer {a}"})
    c = h.sign_in(h.client("192.168.11.3"), "alice", "alice-password-1", None)
    r = h.client("192.168.11.3").post(
        "/api/auth/change-password", headers={"Authorization": f"Bearer {c}"},
        json={"current_password": "alice-password-1", "new_password": "alice-password-2"})
    assert r.status_code == 200
    _store_goes_away(monkeypatch)
    assert _probe(h, a) == 401          # logged out here
    assert _probe(h, b) == 401          # ended by the password change here
    assert _probe(h, c) == 200          # still live, the store cannot be asked: kept


def test_the_sign_out_route_remembers_an_ending_written_elsewhere(h, monkeypatch):
    """The terminal's reset ends the rows in its own process; the service, told through the
    sign-out route, holds them too. MUTATION: drop remember_account_endings - red."""
    from sqlalchemy import update
    from sqlalchemy.orm import Session

    from vaf.auth import sessions
    from vaf.auth.models import UserSession
    user_id, a, _b = _two_devices(h)
    with Session(h.engine) as s:                      # another process's write
        s.execute(update(UserSession).values(is_active=False))
        s.commit()
    sessions.forget_cached_sessions()
    assert h.client(None).post(f"/api/users/{user_id}/sign-out").status_code == 200
    _store_goes_away(monkeypatch)
    assert _probe(h, a) == 401


# ── the list, one session by id, the admin's sign-out ────────────────────────────

def test_the_list_is_live_own_and_hashless(h):
    """Only the account's own live rows, the fields the pages show, `current` for the
    presenting token, never a token hash. MUTATION: list ended rows - red."""
    from vaf.auth import sessions
    _id, a, b = _two_devices(h)
    h.add_user("bob", "bob-password-12", no_second_factor=True)
    h.sign_in(h.client("192.168.17.1"), "bob", "bob-password-12", None)
    h.client("192.168.11.2").post("/api/auth/logout", headers={"Authorization": f"Bearer {b}"})

    async def _list(db):
        return await sessions.list_account_sessions(db, h.user("alice").id, current_token=a)

    listed = h.run_db(_list)
    assert len(listed) == 1 and listed[0]["current"] is True and listed[0]["ip"] == "192.168.11.1"
    assert set(listed[0]) == {"id", "kind", "name", "device", "ip", "created_at", "last_used_at",
                              "expires_at", "idle_until", "current"}
    assert listed[0]["kind"] == "browser" and listed[0]["idle_until"]


def test_one_session_ends_by_id(h):
    """Only the account's own row. MUTATION: end another account's row - red."""
    from vaf.auth import sessions
    from vaf.auth.crypto import token_hash_for_db
    _id, a, b = _two_devices(h)
    h.add_user("bob", "bob-password-12", no_second_factor=True)
    c = h.sign_in(h.client("192.168.17.2"), "bob", "bob-password-12", None)
    ids = {r.token_hash: r.id for r in h.sessions()}

    async def _end(db, owner, row):
        return await sessions.end_account_session(db, h.user(owner).id, row)

    assert h.run_db(lambda db: _end(db, "alice", ids[token_hash_for_db(c)])) is None
    assert _probe(h, c) == 200
    assert h.run_db(lambda db: _end(db, "alice", ids[token_hash_for_db(b)])) == token_hash_for_db(b)
    assert _probe(h, b) == 401 and _probe(h, a) == 200
    assert h.run_db(lambda db: _end(db, "alice", ids[token_hash_for_db(b)])) is None


def test_admin_signs_out_an_account(h, monkeypatch):
    """Settings' "Sign out everywhere" and the terminal's sign-out ask this route: every token of
    the account ends, its sockets close; admin only; an unknown account is 404.
    MUTATION: no close_account_sockets call; no require_admin - red."""
    import uuid
    from vaf.core.web_interface import WebInterfaceManager
    closed = []
    monkeypatch.setattr(WebInterfaceManager, "close_account_connections",
                        lambda self, scope: closed.append(scope))
    user_id, a, b = _two_devices(h)
    admin = h.client(None)
    assert admin.post(f"/api/users/{uuid.uuid4()}/sign-out").status_code == 404
    assert admin.post("/api/users/not-an-id/sign-out").status_code == 404
    other = h.sign_in(h.client("192.168.17.3"), "alice", "alice-password-1", None)
    r = h.client("192.168.17.3").post(f"/api/users/{user_id}/sign-out",
                                      headers={"Authorization": f"Bearer {other}"})
    assert r.status_code == 403
    r = admin.post(f"/api/users/{user_id}/sign-out")
    assert r.status_code == 200 and r.json() == {"ended": 3}
    assert _probe(h, a) == 401 and _probe(h, b) == 401 and _probe(h, other) == 401
    assert closed == [str(h.user("alice").user_scope_id)]
