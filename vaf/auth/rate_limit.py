# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Throttling for the sign-in doors: windows of failed attempts, each only emptied by time.

- ADDRESS window (`RateLimitMiddleware`): failed attempts per REAL client address on the
  rate-limited paths; an address at the threshold gets 429 there. A correct password used to
  clear it, and `/login` answers 200 for a correct password BEFORE the second factor, so any
  account holder could put a login of their own between guesses at other accounts and guess
  without limit (measured: 20 guesses, each 4 interleaved with an own login, zero 429).
- ACCOUNT window (`reserve_account_attempt`, asked by the login route BEFORE it checks the
  password): failed logins per account NAME, from any number of addresses, so a guesser with
  many addresses (a botnet, many VPN peers) is held as well (measured before: 24 guesses at
  one account from 6 addresses, zero 429). Unknown names are held the same way, so the
  answer says nothing about which names exist. The login route exempts the machine itself
  and addresses that completed a sign-in for the account before, so a stranger who knows a
  name cannot lock its owner out of their own devices.

- SECOND-FACTOR window (`vaf/auth/second_factor.py` asks it): wrong codes per account and
  password, at `/verify-2fa`, at the password change and at an admin's step-up alike (three
  doors with three accountings before: one counted per address by hand, two not at all). Five
  wrong codes hold the account's code for the window, every further closure within a day
  twice as long, at most a day. No exemption for the machine: it cannot be reached without
  the password. A completed sign-in clears it; a new password opens a fresh one.

The address and account windows count an attempt BEFORE it runs (`_AttemptTracker.reserve`:
check and count are one step under the lock) and take it back once it did not fail. Checked
first and counted after the awaited password work, a burst of concurrent requests all passed the
check before the first failure was recorded (measured: 60 of 60 reached the password check). So
the attempts in flight count toward the threshold, and at most that many reach the password.

Each first refusal of a closure is the security event `login_throttled`.

Config keys:
  local_network_rate_limit_attempts          - address window threshold (default 5)
  local_network_rate_limit_account_attempts  - account window threshold (default 10)
  local_network_rate_limit_window_minutes    - the windows' length (default 15)

NAMED BOUNDARY: the windows live in this process; a restart empties them.
"""

import logging
import time
from threading import Lock
from typing import Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from vaf.core.config import Config

logger = logging.getLogger(__name__)

# Paths subject to rate limiting
_RATE_LIMITED_PATHS: set[str] = {
    "/api/auth/login",
    "/api/auth/bootstrap",
    "/api/auth/verify-2fa",
    # A logged-in caller's own password change asks for the current password (and the 2FA
    # code): a session left open must not become an unlimited guessing oracle.
    "/api/auth/change-password",
    # Credential-testing endpoint: brute-force surface for mailbox passwords. The route
    # records a failure (via record_login_failure) when a login test fails, so the same
    # IP-based sliding-window block applies here as for /api/auth/login.
    "/api/email/accounts/test",
    # First-run Veyllo key validation (onboarding): unauthenticated + first-run-gated; the route
    # records a failure on a bad/invalid key, so the same per-IP window applies.
    "/api/auth/test-veyllo-key",
    # The first-run setup code: 80 bits, and every wrong one is a 401 counted here.
    "/api/auth/setup-code",
}


def _now() -> float:
    """The clock every window reads (a seam for tests)."""
    return time.monotonic()


class _AttemptTracker:
    """Thread-safe failure times per key, for a sliding window.

    Memory is bounded without letting a flood reset anything: when a record finds more than
    MAX_KEYS keys, it drops the keys whose failures all lie outside their window. A key with
    a failure inside its window is never dropped, so filling the table with fresh addresses
    cannot empty the window of the one being guessed at.
    """

    MAX_KEYS = 10_000

    def __init__(self):
        self._lock = Lock()
        self._attempts: dict[str, list[float]] = {}
        self._horizon: dict[str, float] = {}
        # The newest failure a refusal was already announced for: during a closure no new
        # failure is recorded (the refused request never reaches the check), so this names
        # the closure, and its first refusal is announced once.
        self._announced: dict[str, float] = {}

    def record_failure(self, key: str) -> None:
        window_seconds = _window_seconds()
        with self._lock:
            now = _now()
            self._attempts.setdefault(key, []).append(now)
            self._horizon[key] = max(window_seconds, self._horizon.get(key, 0.0))
            if len(self._attempts) > self.MAX_KEYS:
                self._sweep(now)

    def _sweep(self, now: float) -> None:
        for key in list(self._attempts):
            horizon = self._horizon.get(key, 0.0)
            live = [t for t in self._attempts[key] if t > now - horizon]
            if live:
                self._attempts[key] = live
            else:
                self._forget(key)

    def _forget(self, key: str) -> None:
        self._attempts.pop(key, None)
        self._horizon.pop(key, None)
        self._announced.pop(key, None)

    def reserve(self, key: str, max_attempts: int) -> Optional[float]:
        """Count one attempt now if the window is open: returns its mark for `release`, or
        None when the window is closed. Check and count are ONE step under the lock: checked
        first and counted after the awaited password work, a burst of concurrent requests all
        passed the check before the first failure was recorded (measured: 60 of 60 reached
        the password check, threshold 5)."""
        window = _window_seconds()
        with self._lock:
            now = _now()
            live = [t for t in self._attempts.get(key, ()) if t > now - window]
            if len(live) >= max_attempts:
                if live:
                    self._attempts[key] = live
                return None
            live.append(now)
            self._attempts[key] = live
            self._horizon[key] = max(window, self._horizon.get(key, 0.0))
            if len(self._attempts) > self.MAX_KEYS:
                self._sweep(now)
            return now

    def release(self, key: str, mark: Optional[float]) -> None:
        """Take back an attempt `reserve` counted: it did not fail."""
        if mark is None:
            return
        with self._lock:
            attempts = self._attempts.get(key)
            if attempts and mark in attempts:
                attempts.remove(mark)
                if not attempts:
                    self._forget(key)

    def is_blocked(self, key: str, max_attempts: int, window_seconds: float) -> bool:
        with self._lock:
            cutoff = _now() - window_seconds
            live = [t for t in self._attempts.get(key, ()) if t > cutoff]
            if live:
                self._attempts[key] = live
            else:
                self._forget(key)
            return len(live) >= max_attempts

    def first_refusal(self, key: str) -> bool:
        """True once per closure: the first time a closed key is refused."""
        with self._lock:
            attempts = self._attempts.get(key)
            if not attempts:
                return False
            newest = attempts[-1]
            if self._announced.get(key) == newest:
                return False
            self._announced[key] = newest
            return True

    def keys(self) -> int:
        with self._lock:
            return len(self._attempts)

    def clear(self, key: str) -> None:
        with self._lock:
            self._forget(key)


class _SecondFactorTracker(_AttemptTracker):
    """Wrong codes per account and password, each closure longer than the last.

    A closure starts when a key reaches the threshold within the window and lasts
    `window x 2^(n-1)`, n counting this key's closures in the last day, at most a day. A new
    closure starts from a fresh count. Clearing (a completed sign-in) ends the count and the
    closure, not the day's history, so a guesser who comes back is held longer again.
    """

    DAY = 86400.0

    def __init__(self):
        super().__init__()
        self._closed_until: dict[str, float] = {}
        self._closures: dict[str, list[float]] = {}

    def record_wrong_code(self, key: str, threshold: int) -> bool:
        """Count one wrong code; True when it closed the window."""
        window = _window_seconds()
        with self._lock:
            now = _now()
            live = [t for t in self._attempts.get(key, ()) if t > now - window] + [now]
            self._horizon[key] = window
            if len(live) < threshold:
                self._attempts[key] = live
                if len(self._attempts) > self.MAX_KEYS:
                    self._sweep(now)
                return False
            closures = [c for c in self._closures.get(key, ()) if c > now - self.DAY] + [now]
            self._closures[key] = closures
            self._closed_until[key] = now + min(window * 2 ** (len(closures) - 1), self.DAY)
            self._attempts.pop(key, None)
            return True

    def closed_for(self, key: str) -> float:
        """Seconds this key stays closed, 0 when open."""
        with self._lock:
            left = self._closed_until.get(key, 0.0) - _now()
            return left if left > 0 else 0.0

    def first_refusal(self, key: str) -> bool:
        with self._lock:
            until = self._closed_until.get(key)
            if until is None or self._announced.get(key) == until:
                return False
            self._announced[key] = until
            return True

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)
            self._closed_until.pop(key, None)

    def _sweep(self, now: float) -> None:
        for key in set(self._attempts) | set(self._closed_until) | set(self._closures):
            window = self._horizon.get(key, 0.0)
            live = [t for t in self._attempts.get(key, ()) if t > now - window]
            recent = [c for c in self._closures.get(key, ()) if c > now - self.DAY]
            if live or recent or self._closed_until.get(key, 0.0) > now:
                if live:
                    self._attempts[key] = live
                else:
                    self._attempts.pop(key, None)
                if recent:
                    self._closures[key] = recent
                continue
            self._forget(key)

    def _forget(self, key: str) -> None:
        super()._forget(key)
        self._closed_until.pop(key, None)
        self._closures.pop(key, None)


_tracker = _AttemptTracker()           # per real client address
_account_tracker = _AttemptTracker()   # per account name
_second_factor_tracker = _SecondFactorTracker()   # per account and password

# Wrong codes per account before its second factor is held. Six digits, three of them valid at
# any moment: five guesses leave a chance of 1.5 in 100,000 per window.
SECOND_FACTOR_ATTEMPTS = 5


def _int_config(key: str, default: int) -> int:
    try:
        return int(Config.get(key, default))
    except Exception:
        return default


def window_minutes() -> int:
    return max(1, _int_config("local_network_rate_limit_window_minutes", 15))


def _window_seconds() -> float:
    return window_minutes() * 60.0


def too_many_attempts(retry_after_seconds: Optional[float] = None) -> JSONResponse:
    """The one 429 every window answers with: the same body for an address, an account or a
    second factor, so the answer does not say which window closed."""
    minutes = window_minutes()
    retry = int(retry_after_seconds if retry_after_seconds is not None else minutes * 60)
    return JSONResponse(
        status_code=429,
        content={"detail": f"Too many failed attempts. Try again in {max(1, -(-retry // 60))} minutes."},
        headers={"Retry-After": str(max(1, retry))},
    )


def announce_throttled(ip: str, username: str, which: str, path: str = "") -> None:
    """The security event for a closure's first refusal. Never raises."""
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("login_throttled", ip=ip, username=username, path=path, detail=which)
    except Exception:
        pass


def client_key(request: Request) -> str:
    """The identity this sliding window is keyed on: the REAL client, not the socket peer.

    The integrated HTTPS proxy relays every LAN device to the backend over loopback, so
    ``request.client.host`` is 127.0.0.1 for all of them. Keying on it put every device in one
    shared bucket: five failed logins by anyone locked out the whole network for the window, and
    an attacker could not be told apart from a legitimate user.

    Use this from EVERY site that records or checks a failure. The recording path and the
    checking path must agree on the key - if one used the peer and the other the real client, the
    limiter would record under one key, look up another, and silently stop blocking anything.
    """
    from vaf.network.binding import connection_client_ip
    return connection_client_ip(request)


def record_login_failure(ip: str) -> None:
    """Record a failed attempt in the address window (for a route whose failure is not a 401).

    Pass ``client_key(request)``, never the socket peer.
    """
    _tracker.record_failure(ip)
    logger.info("Rate-limit: recorded failed attempt for %s", ip)


# ── the account window ───────────────────────────────────────────────────────────

# Longer than any stored name (the column holds 50): a name typed longer cannot be an account,
# and every failed login keeps its key for the window, so it is cut rather than kept whole.
_ACCOUNT_KEY_MAX = 64


def account_key(username: str) -> str:
    """One account name, however it was typed: what the login looks it up by, cut to a length
    no stored name exceeds."""
    return str(username or "").strip().lower()[:_ACCOUNT_KEY_MAX]


def reserve_account_attempt(username: str) -> Optional[float]:
    """Count a login for this account name before its password is checked, from any number
    of addresses: the mark to take back with `release_account_attempt` when the password
    was right, or None when the name has reached its threshold in the window."""
    threshold = max(1, _int_config("local_network_rate_limit_account_attempts", 10))
    return _account_tracker.reserve(account_key(username), threshold)


def release_account_attempt(username: str, mark: Optional[float]) -> None:
    _account_tracker.release(account_key(username), mark)


def record_account_failure(username: str) -> None:
    """A login for this name failed on the password or on an unknown name."""
    _account_tracker.record_failure(account_key(username))


def account_refusal_is_new(username: str) -> bool:
    return _account_tracker.first_refusal(account_key(username))


# ── the second-factor window ──────────────────────────────────────────────────────────

def second_factor_key(user) -> str:
    """The account, and its current password: a password change opens a fresh window (the
    last 16 characters of the Argon2 hash change with every new hash)."""
    return f"{getattr(user, 'id', '')}:{str(getattr(user, 'password_hash', '') or '')[-16:]}"


def second_factor_closed(key: str) -> float:
    """Seconds this account's second factor stays held, 0 when it may be tried."""
    return _second_factor_tracker.closed_for(key)


def record_second_factor_failure(key: str) -> bool:
    """One wrong code; True when it closed the window."""
    return _second_factor_tracker.record_wrong_code(key, SECOND_FACTOR_ATTEMPTS)


def second_factor_refusal_is_new(key: str) -> bool:
    return _second_factor_tracker.first_refusal(key)


def clear_second_factor(key: str) -> None:
    """A completed sign-in of the account."""
    _second_factor_tracker.clear(key)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Block requests from addresses that reached the failure threshold (the address window).

    Only applies to the rate-limited sign-in paths. Other paths pass through unaffected.
    A failure is a 401 answer; nothing clears the window but time.
    """

    async def dispatch(self, request: Request, call_next: Callable):
        path = request.url.path

        if path not in _RATE_LIMITED_PATHS:
            return await call_next(request)

        # Only rate-limit POST requests (the actual login attempts)
        if request.method != "POST":
            return await call_next(request)

        client_ip = client_key(request)

        max_attempts = _int_config("local_network_rate_limit_attempts", 5)

        # Counted BEFORE the request runs, and taken back afterwards unless it failed (a 401:
        # bad credentials, a wrong code). A success clears nothing else: a correct password is
        # not the end of a sign-in, and an account holder's own login must not empty the
        # window of their guesses at other accounts.
        mark = _tracker.reserve(client_ip, max_attempts)
        if mark is None:
            logger.warning(
                "Rate-limit: blocked %s on %s (%d attempts in %d min)",
                client_ip, path, max_attempts, window_minutes(),
            )
            if _tracker.first_refusal(client_ip):
                announce_throttled(client_ip, "", "address", path)
            return too_many_attempts()

        try:
            response = await call_next(request)
        except BaseException:
            _tracker.release(client_ip, mark)
            raise
        if response.status_code != 401:
            _tracker.release(client_ip, mark)
        return response
