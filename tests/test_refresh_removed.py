# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The refresh token is gone.

`/api/auth/refresh` exchanged a 7-day refresh token for an access token flagged as having
passed the second factor, with no session row. The refresh token was stored nowhere, survived
a logout and a password change (measured: after the password change `/refresh` answered 200
and the new token opened the API), and no client in the web UI, the CLI, the TUI or the tests
used it: an irrevocable 7-day credential nobody needed.
"""
import pytest

from auth_harness import Harness


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


def test_refresh_is_gone(h, monkeypatch):
    """MUTATION: keep the route, or answer a sign-in with a refresh_token again - red."""
    from vaf.auth import account_seal, crypto, user_admin
    from vaf.auth.middleware import AUTH_EXEMPT_PATHS
    from vaf.core.config import Config
    assert not hasattr(crypto, "create_refresh_token")
    assert "/api/auth/refresh" not in AUTH_EXEMPT_PATHS

    saved = {}
    monkeypatch.setattr(account_seal, "admin_established", lambda: False)
    monkeypatch.setattr(account_seal, "mark_admin_established", lambda: None)
    monkeypatch.setattr(Config, "load", staticmethod(lambda *a, **k: dict(saved)))
    monkeypatch.setattr(Config, "save", staticmethod(lambda c, *a, **k: saved.update(c)))
    monkeypatch.setattr(user_admin, "_prepare_user_home", lambda *a, **k: None)
    local = h.client(None)
    answers = [local.post("/api/auth/bootstrap",
                          json={"username": "owner", "password": "owner-password-1"})]
    h.add_user("alice", "alice-password-1", no_second_factor=True)
    answers.append(h.login(h.client("192.168.12.1"), "alice", "alice-password-1"))
    _id, secret = h.add_user("bob", "bob-password-123", totp=True)
    c = h.client("192.168.12.2")
    temp = h.login(c, "bob", "bob-password-123").json()["temp_token"]
    import auth_harness
    answers.append(h.verify(c, temp, auth_harness.code_at(secret)))
    for r in answers:
        assert r.status_code == 200, r.text
        assert "refresh_token" not in r.json()

    r = h.client("192.168.12.3").post("/api/auth/refresh", json={"refresh_token": "x"})
    assert r.status_code in (401, 404, 405)
    r = local.post("/api/auth/refresh", json={"refresh_token": "x"})
    assert r.status_code in (404, 405)
