# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One check for a second-factor code, for every door that asks for one.

WHY ONE. Three doors asked for a TOTP code, each with its own handling of a wrong one:
`/verify-2fa` (counted nowhere: it answered 400, which the address window does not count),
the password change in Settings (counted per address by hand), and an admin's step-up for
lifting a quarantine (counted nowhere). And none remembered a code: `pyotp.verify` accepts
the current step and the ones beside it, so a code read over a shoulder or relayed by a
phishing page worked again for up to 90 seconds (measured: the same code accepted twice).

WHAT IT DOES. `check_second_factor(user, code)` answers None (accepted) or the reason:
- `throttled`: the account's second-factor window is closed (vaf/auth/rate_limit.py);
- `not_set_up`: the account has no second factor;
- `unreadable`: the stored secret cannot be decrypted on this machine (the caller repairs);
- `wrong_code`: not a code of the steps -1, 0, +1. It counts in the window and is the
  security event `twofa_failed`.
- `spent`: the code of a step that is not later than the last step accepted for this account,
  typically the one just used to sign in, typed again for a password change or an admin's
  confirmation within the same 30 seconds. Refused, but not counted: it is a right code, and
  counting it closed a window on its owner. Still the security event `twofa_failed`.

The wrong code that closes the window is said to the account and the local admin at once
(vaf/auth/sign_in_alerts.py): the password was right, so someone else may have it.

NAMED BOUNDARY: the last accepted step lives in this process; after a restart a code seen
before it can be used once more within its 90 seconds.
"""
from __future__ import annotations

import hmac
import threading
import time
from typing import Optional

THROTTLED = "throttled"
NOT_SET_UP = "not_set_up"
UNREADABLE = "unreadable"
WRONG_CODE = "wrong_code"
SPENT = "spent"

_lock = threading.Lock()
_last_step: dict[str, int] = {}


def _matching_step(secret: str, code: str) -> Optional[int]:
    """The step (-1, 0 or +1 around now) whose code this is, or None. Every candidate is
    compared, in constant time, so the answer's timing does not say which one matched."""
    import pyotp
    totp = pyotp.TOTP(secret)
    current = int(time.time()) // totp.interval
    matched = None
    for step in (current - 1, current, current + 1):
        if hmac.compare_digest(totp.generate_otp(step).encode(), code.encode()):
            matched = step
    return matched


def check_second_factor(user, code: str, *, ip: str = "", where: str = "") -> Optional[str]:
    """None when `code` is a fresh, valid code of `user`'s second factor, else the reason.

    `where` names the door in the security log; `ip` is the real client."""
    from vaf.auth import rate_limit
    from vaf.auth.crypto import decrypt_totp_secret

    key = rate_limit.second_factor_key(user)
    username = str(getattr(user, "username", "") or "")
    if rate_limit.second_factor_closed(key):
        if rate_limit.second_factor_refusal_is_new(key):
            rate_limit.announce_throttled(ip, username, "second factor", where)
        return THROTTLED
    if not getattr(user, "totp_secret", None) or not getattr(user, "totp_nonce", None):
        return NOT_SET_UP
    try:
        secret = decrypt_totp_secret(user.totp_secret, user.totp_nonce)
    except Exception:
        return UNREADABLE

    cleaned = str(code or "").strip().replace(" ", "")
    step = _matching_step(secret, cleaned) if cleaned.isdigit() else None
    account = str(getattr(user, "id", "") or "")
    spent = False
    if step is not None:
        with _lock:
            if step > _last_step.get(account, -1):
                _last_step[account] = step
                return None
            spent = True
    if not spent and rate_limit.record_second_factor_failure(key):
        # This wrong code closed the window: the right password, then five wrong codes.
        # The account and the admin hear it once per closure.
        try:
            from vaf.auth import sign_in_alerts
            sign_in_alerts.second_factor_guessing(user, max(1, -(-int(held_for(user)) // 60)),
                                                  where=where)
        except Exception:
            pass
    what = "code already used" if spent else "wrong code"
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("twofa_failed", ip=ip, username=username,
                           detail=f"{where}: {what}" if where else what)
    except Exception:
        pass
    return SPENT if spent else WRONG_CODE


def held_for(user) -> float:
    """Seconds the account's second factor stays held (for a Retry-After)."""
    from vaf.auth import rate_limit
    return rate_limit.second_factor_closed(rate_limit.second_factor_key(user))


def signed_in(user) -> None:
    """A completed sign-in of the account clears its second-factor window."""
    from vaf.auth import rate_limit
    rate_limit.clear_second_factor(rate_limit.second_factor_key(user))


def forget_spent_codes() -> None:
    """Testing hook."""
    with _lock:
        _last_step.clear()
