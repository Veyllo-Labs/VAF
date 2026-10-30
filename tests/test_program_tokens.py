# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An account sees its own sessions and ends them, and makes tokens for its programs.

The browser keeps no readable token any more, so a script or a program on another machine
gets one of its own: confirmed with the account's 2FA code, shown once, listed with the
devices and revoked there. It acts as the account and does not expire from disuse.
"""
import asyncio
from datetime import timedelta

import pyotp
import pytest

from auth_harness import Harness, wrong_code


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _status(h, app, token, path="/api/probe", ip="192.168.71.99"):
    c = h.client(ip, app=app)
    return c.get(path, headers=_bearer(token)).status_code


def test_own_sessions_list_and_end(h, monkeypatch):
    """GET lists the caller's live sessions (the machine itself gets the local admin's), DELETE
    ends one of the caller's own (404 for another account's, 409 for the presenting one), and
    end-others ends every other one, closing the account's sockets. MUTATION: let DELETE end
    another account's row; end the presenting session in end-others - red."""
    from vaf.core import config as config_module
    from vaf.core.web_interface import WebInterfaceManager
    closed_accounts, closed_sessions = [], []
    monkeypatch.setattr(WebInterfaceManager, "close_account_connections",
                        lambda self, scope: closed_accounts.append(scope))
    monkeypatch.setattr(WebInterfaceManager, "close_session_connections",
                        lambda self, token_hash: closed_sessions.append(token_hash))
    h.add_user("alice", "alice-password-1", no_second_factor=True)
    h.add_user("bob", "bob-password-12", no_second_factor=True)
    app = h.app()
    laptop = h.client("192.168.71.1", app=app)
    a1 = h.sign_in(laptop, "alice", "alice-password-1", None)
    a2 = h.sign_in(h.client("192.168.71.2", app=app), "alice", "alice-password-1", None)
    a3 = h.sign_in(h.client("192.168.71.3", app=app), "alice", "alice-password-1", None)
    b1 = h.sign_in(h.client("192.168.71.4", app=app), "bob", "bob-password-12", None)

    r = laptop.get("/api/auth/sessions")
    assert r.status_code == 200, r.text
    listed = r.json()["sessions"]
    assert r.json()["idle_days"] == 7
    assert len(listed) == 3 and [s for s in listed if s["current"]][0]["ip"] == "192.168.71.1"
    assert all("token_hash" not in s and "token" not in s for s in listed)
    assert {s["kind"] for s in listed} == {"browser"}

    from vaf.auth.crypto import token_hash_for_db
    ids = {r.token_hash: str(r.id) for r in h.sessions()}
    bobs = ids[token_hash_for_db(b1)]
    assert laptop.delete(f"/api/auth/sessions/{bobs}").status_code == 404
    assert _status(h, app, b1) == 200
    assert laptop.delete("/api/auth/sessions/not-a-uuid").status_code == 404
    r = laptop.delete(f"/api/auth/sessions/{ids[token_hash_for_db(a1)]}")
    assert r.status_code == 409 and r.json()["code"] == "current"
    r = laptop.delete(f"/api/auth/sessions/{ids[token_hash_for_db(a2)]}")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert _status(h, app, a2) == 401 and closed_sessions == [token_hash_for_db(a2)]

    r = laptop.post("/api/auth/sessions/end-others")
    assert r.status_code == 200 and r.json() == {"ended": 1}
    assert _status(h, app, a3) == 401 and _status(h, app, a1) == 200
    assert closed_accounts == [str(h.user("alice").user_scope_id)]
    assert len(laptop.get("/api/auth/sessions").json()["sessions"]) == 1

    # The machine itself, tokenless: the local admin's account.
    monkeypatch.setattr(config_module, "get_local_admin_username", lambda: "bob")
    import vaf.api.auth_routes as auth_routes
    monkeypatch.setattr(auth_routes, "get_local_admin_username", lambda: "bob")
    here = h.client(None, app=app)
    r = here.get("/api/auth/sessions")
    assert r.status_code == 200 and [s["ip"] for s in r.json()["sessions"]] == ["192.168.71.4"]


def _with_second_factor(h):
    _id, secret = h.add_user("alice", "alice-password-1", totp=True)
    app = h.app()
    lan = h.client("192.168.72.1", app=app)
    token = h.sign_in(lan, "alice", "alice-password-1", secret)
    from vaf.auth import second_factor
    second_factor.forget_spent_codes()
    return app, lan, token, secret


def test_a_program_token_needs_the_code_and_acts_as_the_account(h, monkeypatch):
    """MUTATION: skip check_second_factor; drop the path from _RATE_LIMITED_PATHS; let the
    inactivity limit reach program rows - red."""
    app, lan, _browser, secret = _with_second_factor(h)
    r = lan.post("/api/auth/tokens", json={"name": "backup", "days": 30,
                                           "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 201, r.text
    token, session_id = r.json()["token"], r.json()["session"]
    created = h.kinds("program_token_created")
    assert len(created) == 1 and created[0]["username"] == "alice"
    assert "backup" in created[0]["detail"] and token not in str(created)
    # It opens the API and the account's own /me, as Bearer, from another machine.
    me = h.client("192.168.72.50", app=app).get("/api/auth/me", headers=_bearer(token))
    assert me.status_code == 200 and me.json()["username"] == "alice"
    listed = {s["id"]: s for s in lan.get("/api/auth/sessions").json()["sessions"]}
    assert listed[session_id]["kind"] == "program" and listed[session_id]["name"] == "backup"
    assert listed[session_id]["idle_until"] is None
    # Unused for 8 days: still live (a program has no inactivity limit).
    from vaf.auth import sessions
    base = sessions._utc_naive()
    monkeypatch.setattr(sessions, "_utc_naive", lambda: base + timedelta(days=8))
    sessions.forget_cached_sessions()
    assert asyncio.run(sessions.session_standing_async(token)) == "live"
    monkeypatch.setattr(sessions, "_utc_naive", lambda: base)
    sessions.forget_cached_sessions()
    # Revoked: 401.
    assert lan.delete(f"/api/auth/sessions/{session_id}").status_code == 200
    assert _status(h, app, token) == 401


def test_a_wrong_code_counts_and_five_hold_the_account(h):
    app, lan, _browser, secret = _with_second_factor(h)
    r = lan.post("/api/auth/tokens", json={"name": "x", "days": 30, "code": wrong_code(secret)})
    assert r.status_code == 401 and r.json()["code"] == "wrong_code"
    assert h.kinds("twofa_failed")
    from vaf.auth import rate_limit
    assert rate_limit._tracker._attempts.get("192.168.72.1"), "the address window counted it"
    statuses = [h.client(f"192.168.72.{n}", app=app).post(
        "/api/auth/tokens", headers=_bearer(_browser),
        json={"name": "x", "days": 30, "code": wrong_code(secret)}).status_code for n in range(2, 7)]
    assert statuses == [401, 401, 401, 401, 429]
    assert not h.kinds("program_token_created")


@pytest.mark.parametrize("body", [{"name": "", "days": 30}, {"name": "x" * 61, "days": 30},
                                  {"name": "two\nlines", "days": 30}, {"name": "ok", "days": 14}])
def test_a_name_or_lifetime_the_rule_does_not_take(h, body):
    """Refused before the code is checked, so the code is not spent."""
    app, lan, _browser, secret = _with_second_factor(h)
    r = lan.post("/api/auth/tokens", json={**body, "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 400 and r.json()["code"] == "invalid", r.text
    r = lan.post("/api/auth/tokens", json={"name": "ok", "days": 7, "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 201, "the code was not spent by the refusal"


def test_the_machine_itself_gives_the_code_too(h, monkeypatch):
    """The agent's shell reaches the backend tokenless from this machine; that must not become a
    credential that works from the network."""
    import vaf.api.auth_routes as auth_routes
    app, _lan, _browser, secret = _with_second_factor(h)
    monkeypatch.setattr(auth_routes, "get_local_admin_username", lambda: "alice")
    here = h.client(None, app=app)
    assert here.post("/api/auth/tokens", json={"name": "x", "days": 30}).status_code == 401
    r = here.post("/api/auth/tokens", json={"name": "x", "days": 30, "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 201


def test_an_account_without_a_second_factor_gets_none(h):
    h.add_user("bob", "bob-password-12", no_second_factor=True)
    app = h.app()
    lan = h.client("192.168.73.1", app=app)
    h.sign_in(lan, "bob", "bob-password-12", None)
    r = lan.post("/api/auth/tokens", json={"name": "x", "days": 30, "code": "123456"})
    assert r.status_code == 400 and r.json()["code"] == "twofa_not_set_up"


def test_the_account_tab_words_every_refusal():
    """Every code POST /api/auth/tokens answers is said in the reader's language by the
    Account tab, every key it uses exists, and the tab is rendered under the password change.
    MUTATION: drop a code from the map in SessionsSection.tsx - red."""
    import ast
    import inspect
    import json
    import re
    from pathlib import Path

    import vaf.api.auth_routes as auth_routes
    from vaf.auth import sessions
    root = Path(__file__).resolve().parents[1]
    route = inspect.getsource(auth_routes.create_program_token)
    codes = set(re.findall(r'"code": "(\w+)"', route))
    codes |= {node.args[0].value for node in ast.walk(ast.parse(inspect.getsource(sessions)))
              if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ProgramTokenRefused"
              and node.args and isinstance(node.args[0], ast.Constant)}
    assert {"wrong_code", "twofa_not_set_up", "invalid", "record_changed"} <= codes
    page = (root / "web" / "components" / "settings" / "SessionsSection.tsx").read_text(encoding="utf-8")
    known = page.split("const known", 1)[1].split("};", 1)[0]
    mapped = dict(re.findall(r"(\w+): t\('(\w+)'", known))
    assert codes <= set(mapped), sorted(codes - set(mapped))
    catalogue = json.loads((root / "web" / "messages" / "en.json").read_text(encoding="utf-8"))["account"]
    used = set(re.findall(r"\bt\('(\w+)'", page))
    assert used <= set(catalogue), sorted(used - set(catalogue))
    for path in ("/api/auth/sessions", "/api/auth/sessions/end-others", "/api/auth/tokens"):
        assert path in page, path
    modal = (root / "web" / "components" / "SettingsModal.tsx").read_text(encoding="utf-8")
    assert ("{activeTab === 'account' && <><AccountSection /><SessionsSection /><StandingGrantsSection"
            " endpoint=\"/api/security/grants\" own sectionId={slugifySection(tGrants('title'))} /></>}") in modal
    assert "fetch(`/api/users/${target}/sign-out`" in modal


def test_the_account_tab_reads_on_a_phone_and_in_dark():
    """Below md a row's action takes its own line under the facts, a date never breaks between
    its time and AM/PM, the "shown only now" warning stands out (amber has a dark pair in
    globals.css), and the disabled Create button keeps a readable label in dark mode, where
    half opacity on the light button left grey on grey. MUTATION: the row action back to the
    plain link class - red."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    page = (root / "web" / "components" / "settings" / "SessionsSection.tsx").read_text(encoding="utf-8")
    row = re.search(r"const rowClass = '([^']*)'", page).group(1)
    assert "max-md:flex-wrap" in row
    action = re.search(r"const rowAction = `([^`]*)`", page).group(1)
    assert "max-md:basis-full" in action and "max-md:pl-7" in action
    assert page.count("className={rowAction}") == 2       # the device rows and the token rows
    assert ".format(new Date(iso)).replace(/\\s/g, '\\u00a0')" in page
    assert "text-amber-700 mb-2\">{t('tokenShownOnce')}" in page
    create = page[:page.index("{t('tokenCreate')}")].rsplit("<button", 1)[1]
    assert "dark:disabled:opacity-100" in create and "dark:disabled:text-" in create
    css = (root / "web" / "app" / "globals.css").read_text(encoding="utf-8")
    assert css.count("--acc-amber-700t:") == 2


def test_the_devices_intro_does_not_promise_this_computer_stays_signed_in():
    """The machine's own row has a Sign out button, so the intro says only that this computer
    is not signed out for being unused. MUTATION: the old "stays signed in" wording - red."""
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    en = json.loads((root / "web" / "messages" / "en.json").read_bytes())["account"]["devicesIntro"]
    de = json.loads((root / "web" / "messages" / "de.json").read_bytes())["account"]["devicesIntro"]
    assert "not signed out for being unused" in en and "stays signed in" not in en
    assert "wegen Nichtnutzung nicht abgemeldet" in de and "bleibt angemeldet" not in de
