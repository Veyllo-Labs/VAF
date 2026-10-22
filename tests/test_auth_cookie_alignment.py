# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Invariant: the auth cookie must never outlive the JWT inside it.

Live incident 2026-07-22: hardcoded 30-day cookie lifetimes outlived the 24h JWT,
so the server-side route gate (web/proxy.ts, judges the cookie's exp) and the
client's bearer-based auth check disagreed - the desktop window navigation-looped
between / and /login until the browser engine throttled it. The cookie max_age is
now derived from the token's own exp claim at the single choke point that sets
the cookie; these tests pin that alignment so a hardcoded lifetime cannot sneak
back in.
"""
from datetime import datetime, timezone

from vaf.api.auth_routes import COOKIE_NAME, _cookie_max_age_for, _set_auth_cookie
from vaf.auth.crypto import create_access_token


class _StubURL:
    scheme = "http"


class _StubRequest:
    url = _StubURL()
    headers: dict = {}


def _make_token(hours: float) -> str:
    return create_access_token("u1", "alice", "admin", "s1", expires_hours=hours)


def test_cookie_max_age_matches_token_exp():
    token = _make_token(hours=2)
    max_age = _cookie_max_age_for(token)
    # ~2h remaining, small tolerance for test runtime
    assert 2 * 3600 - 30 <= max_age <= 2 * 3600 + 30


def _configured_hours(monkeypatch, hours):
    from vaf.core.config import Config
    real_get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: hours if key == "local_network_jwt_expiry_hours"
        else real_get(key, default)))


def test_cookie_max_age_never_a_hardcoded_lifetime(monkeypatch):
    """A default token gets the configured lifetime, and its cookie that same life: with the
    configuration at two days, neither is the month a hardcoded value used to give."""
    _configured_hours(monkeypatch, 48)
    token = create_access_token("u1", "alice", "admin", "s1")
    assert 48 * 3600 - 30 <= _cookie_max_age_for(token) <= 48 * 3600


def test_undecodable_token_falls_back_to_configured_expiry(monkeypatch):
    """The configured lifetime, not a constant: 30 days by default (a network session's
    longest life), and whatever an admin set instead."""
    from vaf.core.config import Config
    assert _cookie_max_age_for("not-a-jwt") == int(Config.DEFAULTS["local_network_jwt_expiry_hours"]) * 3600
    _configured_hours(monkeypatch, 48)
    assert _cookie_max_age_for("not-a-jwt") == 48 * 3600


def test_set_auth_cookie_writes_exp_aligned_max_age():
    from starlette.responses import Response

    token = _make_token(hours=1)
    response = Response()
    _set_auth_cookie(_StubRequest(), response, token)

    set_cookie = response.headers.get("set-cookie", "")
    assert COOKIE_NAME in set_cookie
    max_age = int(set_cookie.split("Max-Age=")[1].split(";")[0])
    assert 3600 - 30 <= max_age <= 3600 + 30
    # The exp actually governs: the cookie dies no later than the token.
    from vaf.auth.crypto import decode_token
    exp = decode_token(token)["exp"]
    assert max_age <= exp - datetime.now(timezone.utc).timestamp() + 30
