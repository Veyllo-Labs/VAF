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
