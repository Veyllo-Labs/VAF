# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A sign-in from a new address, guessed passwords and a guessed second factor are said at once.

To the account (and for the guessing, to the local admin too) on the main messenger and in the
notifications, in the language of the person they go to, off the request path, once per new
address or per closure. The harness records the jobs (`alert_jobs`); these tests run them.
"""
import threading

import pyotp
import pytest

from auth_harness import Harness, wrong_code


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


@pytest.fixture
def sent(monkeypatch):
    """What reached the messengers and the notifications: (scope, text) each."""
    import vaf.core.messaging_connections as messaging
    import vaf.core.user_notifications as notifications
    out = {"messenger": [], "notification": []}
    monkeypatch.setattr(messaging, "send_to_main_messenger",
                        lambda scope, username, text, *a, **k: out["messenger"].append((str(scope), text)) or (True, "telegram"))
    monkeypatch.setattr(notifications, "append_notification",
                        lambda user_scope_id, kind, title, status="success", summary=None, **k:
                        out["notification"].append((str(user_scope_id), summary, title, status)))
    from vaf.core import vocab
    monkeypatch.setattr(vocab, "resolve_user_language", lambda scope=None, username=None: "en")
    return out


def _run(h):
    jobs, h.alert_jobs[:] = list(h.alert_jobs), []
    for job in jobs:
        job()
    return len(jobs)


def test_a_new_address_is_said_once(h, sent, monkeypatch):
    """MUTATION: ask signed_in_from after open_session (never new); alert the machine itself - red."""
    h.add_user("alice", "alice-password-1", no_second_factor=True)
    scope = str(h.user("alice").user_scope_id)
    app = h.app()
    firefox = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0"}
    lan = h.client("192.168.91.1", app=app)
    lan.headers.update(firefox)
    h.sign_in(lan, "alice", "alice-password-1", None)
    events = h.kinds("login_new_address")
    assert len(events) == 1
    assert events[0]["ip"] == "192.168.91.1" and events[0]["username"] == "alice"
    assert events[0]["detail"] == "Firefox on Linux"
    assert _run(h) == 1
    assert len(sent["messenger"]) == 1 and len(sent["notification"]) == 1
    assert sent["messenger"][0][0] == scope and "192.168.91.1" in sent["messenger"][0][1]
    assert "Firefox on Linux" in sent["messenger"][0][1]
    # The same address again, the machine itself, a token for programs: nothing.
    h.sign_in(h.client("192.168.91.1", app=app), "alice", "alice-password-1", None)
    h.sign_in(h.client(None, app=app), "alice", "alice-password-1", None)
    from vaf.auth import sessions

    async def _mint(db):
        return await sessions.mint_program_token(db, h.user("alice"), name="x", days=7, ip="192.168.91.7")

    h.run_db(_mint)
    assert len(h.kinds("login_new_address")) == 1 and _run(h) == 0


def test_the_first_runs_own_sign_in_is_not_new(h, sent, monkeypatch):
    """The wizard on another device: its bootstrap session made the address known."""
    from vaf.auth import account_seal, user_admin
    from vaf.core.config import Config
    saved = {}
    monkeypatch.setattr(account_seal, "admin_established", lambda: False)
    monkeypatch.setattr(account_seal, "mark_admin_established", lambda: None)
    monkeypatch.setattr(Config, "load", staticmethod(lambda *a, **k: dict(saved)))
    monkeypatch.setattr(Config, "save", staticmethod(lambda c, *a, **k: saved.update(c)))
    monkeypatch.setattr(user_admin, "_prepare_user_home", lambda *a, **k: None)
    monkeypatch.setattr(user_admin, "setup_code_matches", lambda code: True)
    browser = h.client("192.168.91.2")
    answer = browser.post("/api/auth/bootstrap", json={"username": "owner", "password": "owner-password-1",
                                                       "setup_code": "x"}).json()
    secret = browser.post("/api/auth/setup-2fa",
                          headers={"Authorization": f"Bearer {answer['temp_token']}"}).json()["secret"]
    assert h.verify(browser, answer["temp_token"], pyotp.TOTP(secret).now()).status_code == 200
    assert not h.kinds("login_new_address") and _run(h) == 0


def _admin(monkeypatch, h, name="boss"):
    h.add_user(name, "boss-password-12", role="admin", no_second_factor=True)
    import vaf.core.config as config
    monkeypatch.setattr(config, "get_local_admin_scope_id", lambda: str(h.user(name).user_scope_id))
    monkeypatch.setattr(config, "get_local_admin_username", lambda: name)
    return str(h.user(name).user_scope_id)


def _hold_the_account(h, name, app):
    """Wrong passwords from strangers until the account window refuses: 10 failures, then 429."""
    statuses = [h.login(h.client(f"10.9.0.{n}", app=app), name, "a-wrong-password-1").status_code
                for n in range(1, 13)]
    assert statuses[:10] == [401] * 10 and statuses[10:] == [429, 429], statuses


def test_password_guessing_reaches_account_and_admin(h, sent, monkeypatch):
    """MUTATION: send for unknown names - red (the next test); drop the alert - red."""
    h.add_user("alice", "alice-password-1", no_second_factor=True)
    alice = str(h.user("alice").user_scope_id)
    admin = _admin(monkeypatch, h)
    _hold_the_account(h, "alice", h.app())
    assert _run(h) == 1, "once per closure"
    assert sorted(s for s, _t in sent["messenger"]) == sorted([alice, admin])
    assert all(t.startswith("Someone is guessing the password of alice.") for _s, t in sent["messenger"])
    assert len(sent["notification"]) == 2 and all(n[3] == "error" for n in sent["notification"])
    # The admin's own account: one message, not two.
    sent["messenger"].clear()
    _hold_the_account(h, "boss", h.app())
    _run(h)
    assert [s for s, _t in sent["messenger"]] == [admin]


def test_unknown_names_are_not_looked_up_on_the_request(h, sent, monkeypatch):
    """The request does the same work for a name that exists and one that does not: the lookup
    runs in the job. MUTATION: look the name up before spawning - red."""
    from vaf.auth import sign_in_alerts
    looked_up = []
    real = sign_in_alerts._account_of
    monkeypatch.setattr(sign_in_alerts, "_account_of", lambda name: looked_up.append(name) or real(name))
    h.add_user("alice", "alice-password-1", no_second_factor=True)
    _hold_the_account(h, "nobody", h.app())
    assert looked_up == [] and len(h.alert_jobs) == 1
    _run(h)
    assert looked_up == ["nobody"] and sent["messenger"] == [] and sent["notification"] == []


def test_a_held_second_factor_says_the_password_is_known(h, sent, monkeypatch):
    """MUTATION: ignore the closing failure's True; alert on every wrong code - red."""
    _id, secret = h.add_user("alice", "alice-password-1", totp=True)
    alice = str(h.user("alice").user_scope_id)
    admin = _admin(monkeypatch, h)
    app = h.app()
    lan = h.client("192.168.92.1", app=app)
    temp = h.login(lan, "alice", "alice-password-1").json()["temp_token"]
    assert h.verify(lan, temp, wrong_code(secret)).status_code == 401
    assert _run(h) == 0, "one wrong code says nothing"
    for n in range(4):
        h.verify(h.client(f"192.168.92.{n + 2}", app=app), temp, wrong_code(secret))
    assert _run(h) == 1
    assert sorted(s for s, _t in sent["messenger"]) == sorted([alice, admin])
    assert all(t.startswith("Someone has the password of alice.") for _s, t in sent["messenger"])


def test_texts_follow_the_language_and_survive_a_failing_messenger(h, sent, monkeypatch):
    """MUTATION: hardcode English; let the messenger's exception escape - red."""
    import json
    from pathlib import Path

    import vaf.core.messaging_connections as messaging
    from vaf.core import vocab
    data = Path(__file__).resolve().parents[1] / "vaf" / "core" / "vocab" / "data"
    for key in ("signin_new_address", "signin_password_guessing", "signin_second_factor_guessing",
                "signin_device"):
        phrases = json.loads((data / f"{key}.json").read_text(encoding="utf-8"))
        assert {"en", "de", "ja", "ko", "th", "tr", "zh"} <= set(phrases), key
        for lang, texts in phrases.items():
            for text in texts:
                text.format(username="u", device="d", address="a", minutes=15, browser="b", os="o")
    monkeypatch.setattr(vocab, "resolve_user_language", lambda scope=None, username=None: "de")
    from vaf.auth.sign_in_alerts import _device_words
    assert _device_words("de", "Firefox", "Linux") == "Firefox unter Linux"
    assert _device_words("ja", "Safari", "iOS") == "iOSのSafari"
    assert _device_words("en", "curl", "") == "curl"
    assert _device_words("de", "", "") == "ein unbekanntes Gerät"

    def _down(*_a, **_k):
        raise RuntimeError("the messenger is down")

    monkeypatch.setattr(messaging, "send_to_main_messenger", _down)
    h.add_user("alice", "alice-password-1", no_second_factor=True)
    phone = h.client("192.168.93.1")
    phone.headers["User-Agent"] = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                                   "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36")
    h.sign_in(phone, "alice", "alice-password-1", None)
    assert h.kinds("login_new_address")
    _run(h)
    assert len(sent["notification"]) == 1
    assert sent["notification"][0][1].startswith("Neue Anmeldung bei alice.")
    assert sent["notification"][0][2] == "Neue Anmeldung bei alice"
    assert "Chrome unter Android von 192.168.93.1" in sent["notification"][0][1]


def test_no_delivery_runs_on_the_calling_thread(monkeypatch):
    """The real seam: a messenger that hangs must not hold a sign-in."""
    from types import SimpleNamespace

    from vaf.auth import sign_in_alerts
    ran = []
    done = threading.Event()

    def _deliver(*a, **k):
        ran.append(threading.current_thread() is threading.main_thread())
        done.set()

    monkeypatch.setattr(sign_in_alerts, "_deliver", _deliver)
    sign_in_alerts.new_address(SimpleNamespace(user_scope_id="s1", username="alice"), "10.0.0.1", "curl")
    assert done.wait(5) and ran == [False]
