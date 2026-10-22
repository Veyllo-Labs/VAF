# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""How long a session lives, by kind, and the last use that keeps one alive.

A sign-in from another device lives 30 days at most and ends after a week without use; the
window on this machine and a token for programs have no inactivity limit. One rule decides it
(`sessions.session_row_live`) for the standing check every request asks, the session list
and `/me`. The clock is `sessions._utc_naive`, moved forward here; the rows were written at
the real now.
"""
import asyncio
import json
from datetime import timedelta

import pytest

from auth_harness import Harness


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


def _real_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clock(monkeypatch, delta):
    """The sessions' clock, `delta` after the real now."""
    from vaf.auth import sessions
    base = _real_now()
    monkeypatch.setattr(sessions, "_utc_naive", lambda: base + delta)
    sessions.forget_cached_sessions()


def _idle_days(monkeypatch, tmp_path, days):
    from vaf.core.config import Config
    raw = tmp_path / "config.json"
    raw.write_text(json.dumps({"local_network_session_idle_days": days}), encoding="utf-8")
    monkeypatch.setattr(Config, "CONFIG_FILE", raw)


def _world(monkeypatch):
    """A fresh store, the clock back at the real now, and one account with a sign-in from the
    LAN, one from this machine and a token for programs. Returns (harness, app, tokens)."""
    from vaf.auth import sessions
    _clock(monkeypatch, timedelta(0))
    h = Harness(monkeypatch)
    h.add_user("alice", "alice-password-1", no_second_factor=True)
    app = h.app()
    lan = h.sign_in(h.client("192.168.41.1", app=app), "alice", "alice-password-1", None)
    here = h.sign_in(h.client(None, app=app), "alice", "alice-password-1", None)

    async def _mint(db):
        return await sessions.mint_program_token(db, h.user("alice"), name="backup", days=30)

    program, _id = h.run_db(_mint)
    return h, app, {"lan": lan, "here": here, "program": program}


def _standing(token):
    from vaf.auth import sessions
    sessions.forget_cached_sessions()
    return asyncio.run(sessions.session_standing_async(token))


def _listed(h, token):
    from vaf.auth import sessions
    from vaf.auth.crypto import token_hash_for_db

    async def _list(db):
        return await sessions.list_account_sessions(db, h.user("alice").id)

    listed = {s["id"] for s in h.run_db(_list)}
    rows = {str(r.id) for r in h.sessions() if r.token_hash == token_hash_for_db(token)}
    return bool(rows & listed)


def _me_from_this_machine(h, app, token):
    """/me decides by its own query here: the middleware lets the machine itself through
    tokenless once the token is refused."""
    return h.client(None, app=app).get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})


def test_program_rows_say_so(h):
    """MUTATION: drop `kind` from the row's device_info - red."""
    from vaf.auth import sessions
    from vaf.auth.crypto import create_access_token
    h.add_user("alice", "alice-password-1")
    user = h.user("alice")
    token = create_access_token(str(user.id), "alice", "user", str(user.user_scope_id),
                                is_2fa_verified=True)

    async def _open(db):
        return await sessions.open_session(db, user, token, ip="10.1.2.3", kind="program", name="x")

    row_id = h.run_db(_open)
    row = [r for r in h.sessions() if str(r.id) == row_id][0]
    assert row.device_info["device_type"] == "program" and row.device_info["name"] == "x"
    assert sessions.session_kind(row.device_info) == sessions.KIND_PROGRAM


def test_inactivity_ends_network_sessions_only(monkeypatch, tmp_path):
    """MUTATION: apply the limit to the machine itself or to programs; ignore the key; let
    /me keep its own predicate - red."""
    _h, _app, t = _world(monkeypatch)
    _clock(monkeypatch, timedelta(days=6, hours=23))
    assert _standing(t["lan"]) == "live"

    h, app, t = _world(monkeypatch)
    _clock(monkeypatch, timedelta(days=7, minutes=1))
    assert _standing(t["lan"]) == "ended"
    assert not _listed(h, t["lan"])
    assert _me_from_this_machine(h, app, t["lan"]).status_code == 401

    for kind in ("here", "program"):
        h, app, t = _world(monkeypatch)
        _clock(monkeypatch, timedelta(days=8))
        assert _standing(t[kind]) == "live", kind
        assert _listed(h, t[kind]), kind
        assert _me_from_this_machine(h, app, t[kind]).status_code == 200, kind

    # Every kind ends at its expiry (a month: the network default, the window's 30 days and
    # the token's chosen 30), with the inactivity limit out of the way.
    _idle_days(monkeypatch, tmp_path, 365)
    _h, _app, t = _world(monkeypatch)
    _clock(monkeypatch, timedelta(days=31))
    assert {k: _standing(v) for k, v in t.items()} == {"lan": "ended", "here": "ended",
                                                        "program": "ended"}

    # The key is read: one day without use is enough when it says so.
    _idle_days(monkeypatch, tmp_path, 1)
    _h, _app, t = _world(monkeypatch)
    _clock(monkeypatch, timedelta(hours=23))
    assert _standing(t["lan"]) == "live"
    _h, _app, t = _world(monkeypatch)
    _clock(monkeypatch, timedelta(days=1, minutes=1))
    assert _standing(t["lan"]) == "ended"
    assert _standing(t["here"]) == "live"


def test_the_idle_key_is_held_to_a_sane_range(monkeypatch, tmp_path):
    from vaf.auth import sessions
    for stored, read in ((0, 1), (-5, 1), (9999, 365), ("x", 7), (14, 14)):
        _idle_days(monkeypatch, tmp_path, stored)
        assert sessions.idle_days() == read, stored


def test_last_use_is_written_at_most_hourly(monkeypatch):
    """MUTATION: write on every cache miss; touch a row past the limit - red."""
    from sqlalchemy import event

    from vaf.auth.crypto import token_hash_for_db
    updates = []

    def _count(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE USER_SESSIONS"):
            updates.append(statement)

    h, _app, t = _world(monkeypatch)
    event.listen(h.engine, "before_cursor_execute", _count)

    def _row(token):
        return [r for r in h.sessions() if r.token_hash == token_hash_for_db(token)][0]

    before = _row(t["lan"]).last_activity
    _clock(monkeypatch, timedelta(hours=2))
    from vaf.auth import sessions
    two_hours_on = sessions._utc_naive()
    assert _standing(t["lan"]) == "live"
    assert len(updates) == 1 and _row(t["lan"]).last_activity == two_hours_on
    _clock(monkeypatch, timedelta(hours=2, minutes=40))
    assert _standing(t["lan"]) == "live"
    assert len(updates) == 1, "a second check within the hour writes nothing"
    # Past the limit, counted from the last use just written: ended, and not written.
    _clock(monkeypatch, timedelta(hours=2, days=7, minutes=1))
    assert _standing(t["lan"]) == "ended"
    assert len(updates) == 1 and _row(t["lan"]).last_activity == two_hours_on
    assert before < two_hours_on


@pytest.mark.parametrize("agent,label", [
    ("Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0", "Firefox on Linux"),
    ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/130.0.0.0 Mobile Safari/537.36", "Chrome on Android"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like "
     "Gecko) Version/17.6 Mobile/15E148 Safari/604.1", "Safari on iOS"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0", "Edge on Windows"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) "
     "Version/17.6 Safari/605.1.15", "Safari on macOS"),
    ("", "Unknown device"),
    ("curl/8.5.0", "curl"),
])
def test_device_labels(agent, label):
    """MUTATION: ask Chrome before Edge (an Edge agent reads "Chrome") - red."""
    from vaf.auth.sessions import device_label
    assert device_label(agent) == label


def test_a_session_of_this_machine_opens_nothing_elsewhere(monkeypatch):
    """The window on this machine gets 30 days and no inactivity limit because the person is at
    their own desk. That reason holds where the token is USED, not where it was minted: the same
    token presented by another device is refused, fresh or idle, while it keeps working here.
    MUTATION: drop the address check from the standing - red."""
    h, app, t = _world(monkeypatch)
    here = t["here"]
    lan = h.client("192.168.50.9", app=app)
    assert lan.get("/api/probe", headers={"Authorization": f"Bearer {here}"}).status_code == 401
    _clock(monkeypatch, timedelta(days=20))
    assert lan.get("/api/probe", headers={"Authorization": f"Bearer {here}"}).status_code == 401
    local = h.client(None, app=app)
    r = local.get("/api/probe", headers={"Authorization": f"Bearer {here}"})
    assert r.status_code == 200 and r.json()["user"]["username"] == "alice"
    # A session from another device and a token for programs are not bound to an address.
    assert lan.get("/api/probe", headers={"Authorization": f"Bearer {t['program']}"}).status_code == 200
    # The room door, the third reader of an account's token, applies the same rule.
    from vaf.main import _a2a_credential_verifier
    _clock(monkeypatch, timedelta(0))
    assert _a2a_credential_verifier(here, client_ip="192.168.50.9") is None
    assert _a2a_credential_verifier(here, client_ip="127.0.0.1")["username"] == "alice"
