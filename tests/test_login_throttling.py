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


# ── the second-factor window ─────────────────────────────────────────────────────

class _Clock:
    """The windows' clock, moved by hand."""

    def __init__(self, monkeypatch):
        from vaf.auth import rate_limit
        self.t = 1_000_000.0
        monkeypatch.setattr(rate_limit, "_now", lambda: self.t)


def _change_password(h, token, ip, code, new="a-brand-new-password"):
    c = h.client(ip)
    return c.post("/api/auth/change-password", headers={"Authorization": f"Bearer {token}"},
                  json={"current_password": "alice-password-1", "new_password": new, "code": code})


def _step_up(h, user_id, code):
    import asyncio

    from fastapi import HTTPException

    from vaf.api.security_routes import verify_admin_totp
    try:
        asyncio.run(verify_admin_totp({"user_id": user_id}, code))
        return 200
    except HTTPException as e:
        return e.status_code


def _wrong_at_verify(h, secret, ip):
    c = h.client(ip)
    temp = h.login(c, "alice", "alice-password-1").json()["temp_token"]
    return h.verify(c, temp, wrong_code(secret)).status_code


def test_the_second_factor_window_closes_and_doubles(h, monkeypatch):
    """Five wrong codes, at any of the three doors, hold the account's code; the next closure
    within a day lasts twice as long. MUTATION: key it per address, or drop the doubling - red."""
    clock = _Clock(monkeypatch)
    user_id, secret = h.add_user("alice", "alice-password-1", totp=True)
    token = h.sign_in(h.client("192.168.2.1"), "alice", "alice-password-1", secret)
    assert _wrong_at_verify(h, secret, "192.168.2.2") == 401
    assert _wrong_at_verify(h, secret, "192.168.2.3") == 401
    assert _change_password(h, token, "192.168.2.4", wrong_code(secret)).json()["code"] == "wrong_code"
    assert _change_password(h, token, "192.168.2.5", wrong_code(secret)).json()["code"] == "wrong_code"
    assert _step_up(h, user_id, wrong_code(auth_harness.pyotp.random_base32())) == 403
    # Held at all three doors, the right code included, from an address never seen.
    c = h.client("192.168.2.6")
    temp = h.login(c, "alice", "alice-password-1").json()["temp_token"]
    r = h.verify(c, temp, auth_harness.code_at(secret))
    assert r.status_code == 429
    r = _change_password(h, token, "192.168.2.7", auth_harness.code_at(secret))
    assert r.status_code == 429 and r.json()["code"] == "throttled"
    assert 0 < int(r.headers["retry-after"]) <= 900       # the page names the wait
    assert _step_up(h, user_id, auth_harness.code_at(secret)) == 429
    assert [e["detail"] for e in h.kinds("login_throttled")] == ["second factor"]
    # The first closure lasts the window ...
    clock.t += 899
    assert h.verify(c, temp, auth_harness.code_at(secret)).status_code == 429
    clock.t += 2
    for i in range(5):
        assert _wrong_at_verify(h, secret, f"192.168.3.{i + 1}") == 401
    # ... the second one twice as long.
    clock.t += 1799
    assert h.verify(c, temp, auth_harness.code_at(secret)).status_code == 429
    clock.t += 2
    # (the next step's code: the sign-in above spent the current one)
    assert h.verify(c, temp, auth_harness.code_at(secret, 1)).status_code == 200


def test_success_clears_it(h):
    """A completed sign-in ends the count. MUTATION: no clear on success - red."""
    _id, secret = h.add_user("alice", "alice-password-1", totp=True)
    for i in range(4):
        assert _wrong_at_verify(h, secret, f"192.168.4.{i + 1}") == 401
    h.sign_in(h.client("192.168.4.9"), "alice", "alice-password-1", secret)
    for i in range(4):
        assert _wrong_at_verify(h, secret, f"192.168.5.{i + 1}") == 401
    c = h.client("192.168.5.9")
    temp = h.login(c, "alice", "alice-password-1").json()["temp_token"]
    assert h.verify(c, temp, auth_harness.code_at(secret, 1)).status_code == 200


def test_a_new_password_opens_a_fresh_window(h):
    """The window is keyed on the account AND its password. MUTATION: key without the hash
    suffix - red."""
    _id, secret = h.add_user("alice", "alice-password-1", totp=True)
    for i in range(5):
        assert _wrong_at_verify(h, secret, f"192.168.6.{i + 1}") == 401
    from sqlalchemy.orm import Session

    from vaf.auth.crypto import hash_password
    with Session(h.engine) as s:
        user = s.get(type(h.user("alice")), h.user("alice").id)
        user.password_hash = hash_password("alice-password-2")
        s.commit()
    c = h.client("192.168.6.9")
    temp = h.login(c, "alice", "alice-password-2").json()["temp_token"]
    assert h.verify(c, temp, auth_harness.code_at(secret)).status_code == 200


def test_step_up_shares_it(h):
    """A stolen admin session guessed the step-up code without limit.
    MUTATION: verify the step-up code without check_second_factor - red."""
    user_id, secret = h.add_user("alice", "alice-password-1", role="admin", totp=True)
    for _ in range(5):
        assert _step_up(h, user_id, wrong_code(secret)) == 403
    c = h.client("192.168.7.1")
    temp = h.login(c, "alice", "alice-password-1").json()["temp_token"]
    assert h.verify(c, temp, auth_harness.code_at(secret)).status_code == 429


# ── a burst is counted before it is checked ──────────────────────────────────────

def _burst(h, n, ip_for):
    """`n` wrong logins for alice, all in flight at once, through the real stack."""
    import asyncio

    import httpx
    app = h.app()

    async def main():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://vaf") as c:
            async def one(i):
                return await c.post("/api/auth/login",
                                    json={"username": "alice", "password": f"guess-{i}"},
                                    headers={"X-Forwarded-For": ip_for(i)})
            return await asyncio.gather(*[one(i) for i in range(n)])

    return [r.status_code for r in asyncio.run(main())]


def test_a_burst_from_one_address_gets_the_address_limit(h, argon2_calls):
    """Each window was checked before the awaited password work and counted after it, so a
    burst passed the check before any failure was recorded (measured: 60 of 60 through, 60
    Argon2 checks, threshold 5). MUTATION: count after the response again - red."""
    h.add_user("alice", "alice-password-1")
    before = len(argon2_calls)
    statuses = _burst(h, 40, lambda i: "192.168.20.1")
    assert len(argon2_calls) - before <= 5, len(argon2_calls) - before
    assert statuses.count(401) <= 5 and statuses.count(429) >= 35, statuses


def test_a_burst_across_addresses_gets_the_account_limit(h, argon2_calls):
    """The same for one account from many addresses. MUTATION: check the account window
    without counting the attempt first - red."""
    h.add_user("alice", "alice-password-1")
    before = len(argon2_calls)
    statuses = _burst(h, 40, lambda i: f"10.21.0.{i + 1}")
    assert len(argon2_calls) - before <= 10, len(argon2_calls) - before
    assert statuses.count(429) >= 30, statuses


def test_a_correct_login_is_not_counted(h):
    """An attempt is counted before it runs and taken back unless it failed: correct logins
    from one address, and from many, never close a window.
    MUTATION: never take an attempt back (in either window) - red."""
    h.add_user("alice", "alice-password-1", no_second_factor=True)
    one = h.client("192.168.22.1")
    for _ in range(7):
        assert h.login(one, "alice", "alice-password-1").status_code == 200
    for i in range(12):
        assert h.login(h.client(f"10.22.0.{i + 1}"), "alice", "alice-password-1").status_code == 200


def test_an_address_is_known_for_thirty_days_only(h):
    """The account window's exemption: an address that completed a sign-in for the account
    within 30 days. One older than that is a stranger again. MUTATION: drop or widen the
    30-day bound in signed_in_from - red."""
    from datetime import datetime, timedelta

    from sqlalchemy import update
    from sqlalchemy.orm import Session

    from vaf.auth.models import UserSession
    h.add_user("alice", "alice-password-1", no_second_factor=True)
    known = h.client("192.168.23.1")
    h.sign_in(known, "alice", "alice-password-1", None)
    with Session(h.engine) as s:
        s.execute(update(UserSession).values(created_at=datetime.utcnow() - timedelta(days=31)))
        s.commit()
    _close_account_window(h, "alice")
    assert h.login(known, "alice", "alice-password-1").status_code == 429
    with Session(h.engine) as s:
        s.execute(update(UserSession).values(created_at=datetime.utcnow() - timedelta(days=29)))
        s.commit()
    assert h.login(known, "alice", "alice-password-1").status_code == 200


def test_a_long_name_is_kept_short():
    """Every failed login keeps its name as a key for the window: a name typed longer than
    any account can be is cut. MUTATION: drop the cut - red."""
    from vaf.auth import rate_limit
    assert len(rate_limit.account_key(" X" * 5000)) == 64
    assert rate_limit.account_key("  Alice ") == "alice"


def test_the_step_up_records_the_callers_address():
    """The three step-up routes pass the real client to the check, so its security events name
    the device. MUTATION: drop the address at a call site - red."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vaf" / "api" / "security_routes.py").read_text(
        encoding="utf-8")
    assert src.count('await verify_admin_totp(user, str(payload.get("code") or ""), ip=_caller_ip(request))') == 3


# ── an attempt that neither failed nor succeeded is taken back ───────────────────

@pytest.mark.parametrize("stop", ["error", "disconnect"])
def test_an_error_or_a_disconnect_counts_in_neither_window(h, monkeypatch, stop):
    """A login that ends in an exception (a database error) or is cancelled while it waits
    (the client went away) proved nothing about the password: both windows take their attempt
    back, or every store hiccup and every disconnect would count toward locking the caller's
    own address and account. The disconnect cancels the request's task the way the server does,
    so the middleware's await itself raises CancelledError. MUTATION: drop the middleware's
    release on an exception, catch only Exception there, or turn the login's try/finally into
    straight-line code - red."""
    import asyncio

    import httpx

    from vaf.api import auth_routes
    from vaf.auth import rate_limit
    h.add_user("alice", "alice-password-1", no_second_factor=True)
    app = h.app()

    async def main():
        waiting = asyncio.Event()

        async def _password_check(*_a, **_k):
            if stop == "error":
                raise RuntimeError("the store went away")
            waiting.set()
            await asyncio.Event().wait()          # until the client goes away

        monkeypatch.setattr(auth_routes, "verify_password_async", _password_check)
        transport = httpx.ASGITransport(app=app, client=auth_harness.PROXY_PEER)
        async with httpx.AsyncClient(transport=transport, base_url="http://vaf") as c:
            request = asyncio.ensure_future(c.post(
                "/api/auth/login", json={"username": "alice", "password": "alice-password-1"},
                headers={"X-Forwarded-For": "192.168.24.1"}))
            if stop == "disconnect":
                await waiting.wait()
                assert rate_limit._tracker._attempts.get("192.168.24.1")    # counted while in flight
                request.cancel()
            try:
                await request
            except BaseException as e:            # the error or the cancellation, as raised
                return type(e).__name__
            return "answered"

    outcome = asyncio.run(main())
    assert outcome == {"error": "RuntimeError", "disconnect": "CancelledError"}[stop]
    assert not rate_limit._tracker._attempts.get("192.168.24.1")
    assert not rate_limit._account_tracker._attempts.get("alice")
