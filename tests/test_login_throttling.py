# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Sign-ins hold against guessing (vaf/auth/rate_limit.py, the login route).

Measured before, each against the real router behind the real middlewares:
- 30 wrong 2FA codes answered 400 each and never 429: a wrong code was counted nowhere.
- 20 guesses at another account, every four with a login to one's own account between them:
  zero 429, because a correct password cleared the address window.
- 24 guesses at one account from 6 addresses: zero 429, there was no account window.

The harness (tests/auth_harness.py) is the attacker's view: a LAN client is the proxy peer
with X-Forwarded-For.
"""
import time

import pytest

import auth_harness
from auth_harness import Harness, wrong_code

BODY = {"detail": "Too many failed attempts. Try again in 15 minutes."}


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


@pytest.fixture
def argon2_calls(monkeypatch):
    """Counts every Argon2 verification, wherever it runs."""
    from vaf.auth import crypto
    real = crypto._ph
    calls = []

    class _Counting:
        def verify(self, hashed, password):
            calls.append(1)
            return real.verify(hashed, password)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(crypto, "_ph", _Counting())
    return calls


def _held(r):
    assert r.status_code == 429, (r.status_code, r.text)
    assert r.json() == BODY
    assert r.headers["retry-after"] == "900"


# ── the address window ───────────────────────────────────────────────────────────

def test_wrong_codes_are_counted(h):
    """MUTATION: answer a wrong code with 400 again - the sixth request is not held."""
    _id, secret = h.add_user("alice", "alice-password-1", totp=True)
    c = h.client("192.168.1.60")
    bad = wrong_code(secret)
    for _ in range(5):
        temp = h.login(c, "alice", "alice-password-1").json()["temp_token"]
        r = h.verify(c, temp, bad)
        assert r.status_code == 401
    _held(h.login(c, "alice", "alice-password-1"))


def test_a_correct_password_does_not_empty_the_window(h):
    """An account holder's own login between guesses used to clear the window.
    MUTATION: clear the address window on a 200 of /login again - not held."""
    h.add_user("alice", "alice-password-1")
    h.add_user("bob", "bob-password-12", totp=True)
    c = h.client("192.168.1.61")
    for _ in range(4):
        assert h.login(c, "alice", "guess-guess").status_code == 401
    assert h.login(c, "bob", "bob-password-12").status_code == 200
    assert h.login(c, "alice", "guess-guess").status_code == 401
    _held(h.login(c, "alice", "guess-guess"))
    assert [f["detail"] for f in h.kinds("login_throttled")] == ["address"]


# ── the account window ───────────────────────────────────────────────────────────

def _close_account_window(h, name, n=10):
    for i in range(n):
        r = h.login(h.client(f"10.9.0.{i + 1}"), name, "guess-guess")
        assert r.status_code == 401, (i, r.text)


def test_the_account_window_closes_across_addresses(h, argon2_calls):
    """Ten failures from ten addresses hold the eleventh, from an address never seen, before
    any hashing. MUTATION: key the account window per address, or check it after the
    password - red."""
    h.add_user("alice", "alice-password-1")
    _close_account_window(h, "alice")
    before = len(argon2_calls)
    _held(h.login(h.client("10.9.1.1"), "alice", "alice-password-1"))
    assert len(argon2_calls) == before, "a held login must not cost a hash"
    # One event per closure, however many are refused.
    _held(h.login(h.client("10.9.1.2"), "alice", "guess-guess"))
    events = h.kinds("login_throttled")
    assert [(e["detail"], e["username"]) for e in events] == [("account", "alice")]


def test_unknown_names_close_the_same_way(h, argon2_calls):
    """The answer must not say which names exist."""
    _close_account_window(h, "nobody")
    before = len(argon2_calls)
    _held(h.login(h.client("10.9.1.1"), "nobody", "guess-guess"))
    assert len(argon2_calls) == before


def test_the_owner_at_the_machine_and_known_addresses_pass(h):
    """Somebody who knows a name must not lock its owner out of their own devices.
    MUTATION: drop either exemption - red."""
    _id, secret = h.add_user("alice", "alice-password-1", totp=True)
    known = h.client("192.168.1.70")
    h.sign_in(known, "alice", "alice-password-1", secret)
    _close_account_window(h, "alice")
    # A new address is held ...
    assert h.login(h.client("10.9.1.1"), "alice", "alice-password-1").status_code == 429
    # ... the machine itself and the address that signed in before are not.
    assert h.login(h.client(None), "alice", "alice-password-1").status_code == 200
    assert h.login(known, "alice", "alice-password-1").status_code == 200


def test_the_account_threshold_is_read(h, monkeypatch):
    """MUTATION: ignore local_network_rate_limit_account_attempts - red."""
    from vaf.core.config import Config
    real_get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda k, d=None: 3 if k == "local_network_rate_limit_account_attempts" else real_get(k, d)))
    h.add_user("alice", "alice-password-1")
    _close_account_window(h, "alice", n=3)
    assert h.login(h.client("10.9.1.1"), "alice", "guess-guess").status_code == 429


def test_the_account_attempts_key_is_registered():
    """Config key registry copies: DEFAULTS, the schema row and its count line."""
    from pathlib import Path

    from vaf.core.config import Config
    assert Config.DEFAULTS["local_network_rate_limit_account_attempts"] == 10
    schema = (Path(__file__).resolve().parents[1] / "docs" / "setup" / "CONFIG_SCHEMA.md"
              ).read_text(encoding="utf-8")
    assert "| `local_network_rate_limit_account_attempts` | `10` |" in schema
    assert f"({len(Config.DEFAULTS)} keys)" in schema


# ── the temp token and the tracker ───────────────────────────────────────────────

def test_the_temp_token_lives_fifteen_minutes(h):
    """It lived the full access token's day. MUTATION: default lifetime - red."""
    from vaf.auth.crypto import decode_token
    h.add_user("alice", "alice-password-1", totp=True)
    data = h.login(h.client(), "alice", "alice-password-1").json()
    payload = decode_token(data["temp_token"])
    assert payload["exp"] - payload["iat"] == 900


def test_the_tracker_sweeps_only_dead_keys(monkeypatch):
    """Memory stays bounded, and flooding the table cannot empty a live window.
    MUTATION: no sweep (the dead keys stay), or sweep live keys (the live one goes) - red."""
    from vaf.auth import rate_limit
    clock = [0.0]
    monkeypatch.setattr(rate_limit, "_now", lambda: clock[0])
    monkeypatch.setattr(rate_limit, "_window_seconds", lambda: 60.0)
    t = rate_limit._AttemptTracker()
    for i in range(t.MAX_KEYS):
        t.record_failure(f"dead-{i}")
    clock[0] = 950.0
    t.record_failure("live")
    clock[0] = 1000.0
    t.record_failure("trigger")
    assert t.keys() == 2
    assert t.is_blocked("live", 1, 60)


def test_the_harness_is_the_attackers_view(h):
    """The floor under the tests above: a LAN client really is one here (an address outside
    the admitted networks is refused, as through the real proxy)."""
    c = h.client("203.0.113.9")
    assert c.post("/api/auth/login", json={"username": "a", "password": "b"}).status_code == 403
    assert auth_harness.PROXY_PEER[0] == "127.0.0.1"
    assert time.time() > 0
