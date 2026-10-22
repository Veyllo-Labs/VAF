# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A sign-in ends in one place, with the lifetime of its door, and the browser's credential is
the HttpOnly cookie.

`/verify-2fa` and `/login` for an account without a second factor each minted, opened the
session and set the cookie by hand; the 30 days for the machine itself sat in the second copy
only, which no account VAF creates reaches, so the desktop window lived a day. Every sign-in
also left the session of the cookie it replaced live for its whole life.
"""
import ast
from datetime import timedelta
from pathlib import Path

import pyotp
import pytest

from auth_harness import Harness

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


def _hours(token):
    from vaf.auth.crypto import decode_token
    payload = decode_token(token)
    return (payload["exp"] - payload["iat"]) / 3600


def _cookie(response):
    return response.cookies.get("vaf_token")


def _configured(monkeypatch, hours):
    from vaf.core.config import Config
    real_get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: hours if key == "local_network_jwt_expiry_hours"
        else (True if key == "local_network_enabled" else real_get(key, default))))


def _first_run(monkeypatch):
    from vaf.auth import account_seal, user_admin
    from vaf.core.config import Config
    saved = {}
    monkeypatch.setattr(account_seal, "admin_established", lambda: False)
    monkeypatch.setattr(account_seal, "mark_admin_established", lambda: None)
    monkeypatch.setattr(Config, "load", staticmethod(lambda *a, **k: dict(saved)))
    monkeypatch.setattr(Config, "save", staticmethod(lambda c, *a, **k: saved.update(c)))
    monkeypatch.setattr(user_admin, "_prepare_user_home", lambda *a, **k: None)


def test_lifetimes_per_door(h, monkeypatch):
    """The machine itself: 30 days; another device: the configured lifetime; the first-run
    wizard's own session: an hour. MUTATION: verify-2fa mints with the configured lifetime for
    the machine itself; bootstrap mints the configured lifetime - red."""
    app = h.app()
    _configured(monkeypatch, 48)
    _id, secret = h.add_user("alice", "alice-password-1", totp=True)
    here = h.client(None, app=app)
    temp = h.login(here, "alice", "alice-password-1").json()["temp_token"]
    r = h.verify(here, temp, pyotp.TOTP(secret).now())
    assert r.status_code == 200 and _hours(_cookie(r)) == 30 * 24
    from vaf.auth import second_factor
    second_factor.forget_spent_codes()
    lan = h.client("192.168.51.1", app=app)
    temp = h.login(lan, "alice", "alice-password-1").json()["temp_token"]
    r = h.verify(lan, temp, pyotp.TOTP(secret).now())
    assert r.status_code == 200 and _hours(_cookie(r)) == 48
    # The same doors without a second factor.
    h.add_user("bob", "bob-password-12", no_second_factor=True)
    assert _hours(_cookie(h.login(h.client(None, app=app), "bob", "bob-password-12"))) == 30 * 24
    assert _hours(_cookie(h.login(h.client("192.168.51.2", app=app), "bob", "bob-password-12"))) == 48


def test_the_wizards_own_session_lives_an_hour(h, monkeypatch):
    """MUTATION: bootstrap mints the configured lifetime - red."""
    _first_run(monkeypatch)
    r = h.client(None).post("/api/auth/bootstrap",
                            json={"username": "owner", "password": "owner-password-1"})
    assert r.status_code == 200, r.text
    assert _hours(_cookie(r)) == 1


def test_one_way_to_complete_a_sign_in():
    """One open_session for a completed sign-in in the routes, outside the wizard's own: the
    two sign-in doors call _complete_sign_in. MUTATION: open a session in /login again - red."""
    src = (ROOT / "vaf" / "api" / "auth_routes.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def calls(fn, name):
        return [n for n in ast.walk(functions[fn]) if isinstance(n, ast.Call)
                and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == name]

    opened = [fn for fn in functions if calls(fn, "open_session")]
    assert sorted(opened) == ["_complete_sign_in", "bootstrap"], opened
    assert len(calls("_complete_sign_in", "open_session")) == 1
    assert calls("verify_2fa", "_complete_sign_in") and calls("_sign_in", "_complete_sign_in")
    assert sorted(fn for fn in functions if calls(fn, "_set_auth_cookie")) == [
        "_complete_sign_in", "bootstrap"]


def test_a_new_sign_in_ends_the_cookie_it_replaces(h):
    """A browser signed in as A that signs in again, as A or as B: the token it carried
    before answers 401. MUTATION: drop the ending of the replaced session - red."""
    h.add_user("alice", "alice-password-1", no_second_factor=True)
    h.add_user("bob", "bob-password-12", no_second_factor=True)
    app = h.app()
    browser = h.client("192.168.52.1", app=app)
    first = _cookie(h.login(browser, "alice", "alice-password-1"))
    second = _cookie(h.login(browser, "alice", "alice-password-1"))
    third = _cookie(h.login(browser, "bob", "bob-password-12"))
    other = h.client("192.168.52.2", app=app)

    def status(token):
        other.cookies.clear()
        return other.get("/api/probe", headers={"Authorization": f"Bearer {token}"}).status_code

    assert first and second and third and len({first, second, third}) == 3
    assert status(first) == 401 and status(second) == 401 and status(third) == 200


def test_a_refused_cookie_is_deleted_for_every_reason(h, monkeypatch):
    """Ended after the inactivity limit, expired, or garbage: a network browser gets the 401
    with the cookie deleted, so the page gate renders the sign-in page instead of bouncing;
    the same token as Bearer leaves the cookie alone. The machine itself keeps its tokenless
    path. MUTATION: delete the cookie for a Bearer token too - red."""
    from vaf.auth import sessions
    from vaf.auth.crypto import create_access_token
    h.add_user("alice", "alice-password-1", no_second_factor=True)
    app = h.app()
    idle = _cookie(h.login(h.client("192.168.53.1", app=app), "alice", "alice-password-1"))
    user = h.user("alice")
    expired = create_access_token(str(user.id), "alice", "user", str(user.user_scope_id),
                                  expires_hours=-1, is_2fa_verified=True)
    base = sessions._utc_naive()
    monkeypatch.setattr(sessions, "_utc_naive", lambda: base + timedelta(days=7, minutes=1))
    sessions.forget_cached_sessions()
    for token in (idle, expired, "not.a.token"):
        browser = h.client("192.168.53.2", app=app)
        browser.cookies.set("vaf_token", token)
        r = browser.get("/api/auth/me")
        assert r.status_code == 401, token
        assert "vaf_token=" in r.headers.get("set-cookie", "") and "max-age=0" in r.headers[
            "set-cookie"].lower(), token
        program = h.client("192.168.53.3", app=app)
        r = program.get("/api/probe", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401 and "set-cookie" not in r.headers, token
        here = h.client(None, app=app)
        here.cookies.set("vaf_token", token)
        assert here.get("/api/probe").status_code == 200, token


def test_no_answer_hands_out_a_token(h, monkeypatch):
    """Bootstrap, /login (with and without a second factor) and /verify-2fa: the session's
    token is in the HttpOnly cookie only, never in JSON a page script could read and keep.
    MUTATION: /verify-2fa answers access_token again - red."""
    _first_run(monkeypatch)
    app = h.app()
    here = h.client(None, app=app)
    r = here.post("/api/auth/bootstrap", json={"username": "owner", "password": "owner-password-1"})
    assert r.status_code == 200 and set(r.json()) == {"temp_token", "user"}, r.text
    _id, secret = h.add_user("alice", "alice-password-1", totp=True)
    h.add_user("bob", "bob-password-12", no_second_factor=True)
    lan = h.client("192.168.54.1", app=app)
    r = h.login(lan, "alice", "alice-password-1")
    assert set(r.json()) == {"requires_2fa", "needs_2fa_setup", "temp_token"}
    assert _cookie(r) is None
    r = h.verify(lan, r.json()["temp_token"], pyotp.TOTP(secret).now())
    assert r.status_code == 200 and r.json() == {"ok": True} and _cookie(r)
    r = h.login(h.client("192.168.54.2", app=app), "bob", "bob-password-12")
    assert r.status_code == 200 and set(r.json()) == {"user"} and _cookie(r)
    for token_like in ("access_token", "refresh_token"):
        assert token_like not in r.text


def test_the_web_ui_keeps_no_readable_token():
    """No access token in the page's storage, in a header built from storage, or in the
    socket's address: the HttpOnly cookie is the browser's only credential, and exactly one
    call removes the copy an older VAF left behind, in a module every page loads.
    MUTATION: keep a localStorage.setItem('vaf_token' in the login page; put token= back
    into the socket address - red."""
    import re
    web = ROOT / "web"
    sources = {}
    for top in ("app", "components", "lib"):
        for path in (web / top).rglob("*"):
            if path.suffix in (".ts", ".tsx") and path.is_file():
                sources[path.relative_to(ROOT).as_posix()] = path.read_text(encoding="utf-8")
    storage = {rel: re.findall(r"(?:localStorage|sessionStorage)\.\w+\(\s*['\"]vaf_token['\"]", src)
               for rel, src in sources.items()}
    storage = {rel: calls for rel, calls in storage.items() if calls}
    assert storage == {"web/components/HostnameNormalizer.tsx": ["localStorage.removeItem('vaf_token'"]}
    layout = (web / "app" / "layout.tsx").read_text(encoding="utf-8")
    assert "<HostnameNormalizer />" in layout
    assert not [rel for rel, src in sources.items() if "access_token" in src]
    assert not re.search(r"['\"`?&]token=", sources["web/app/page.tsx"]), "a token in the socket address"
    assert not [rel for rel, src in sources.items()
                if re.search(r"Authorization:\s*`Bearer \$\{(?:t|token|accessToken)\}`", src)]
    page = sources["web/app/page.tsx"]
    assert page.count("new WebSocket(") == 1 and "new WebSocket(wsUrl)" in page
    assert "const wsUrl = (base ? base + '/ws' : '/ws');" in page


def test_only_a_refusal_means_the_session_ended():
    """A 5xx from /api/auth/me (the backend restarting) says nothing about the session: the page
    shows its retry screen instead of "your session ended". MUTATION: redirect on any non-OK - red."""
    page = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    check = page[page.index("The HttpOnly cookie is the only credential this page has"):]
    check = check[:check.index(".catch(")]
    branch = check.index("} else if (res.status !== 401 && res.status !== 403) {")
    assert "setAuthError('error');" in check[branch:check.index("} else {", branch + 10)]
    assert check.index("/login?reason=ended") > branch
