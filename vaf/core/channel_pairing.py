# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Pairing an account's own messenger identity with a shared bot, and the one writer of a
Telegram pairing.

The Telegram bot is one per installation and every account pairs its own Telegram account
on it (`channels.Channel.accounts == "shared"`). The only way to pair used to be the setup
wizard, which needs the bot token and runs a second bot for a verification code: an admin's
step, so another account could not pair itself at all. Here the account asks for a one-time
code, sends it to the RUNNING bot (`/start <code>`, which a t.me deep link fills in), and the
bot links the Telegram account that sent it to the account that asked. Proof of both ends:
the code is shown only to the signed-in account, and only the Telegram account that sends it
can be the one linked.

The codes live in this process (the Telegram bridge runs in the web server's process), for
`PAIRING_TTL_SECONDS`, one per account and channel, and a code is spent by its first use.
72 random bits, so a guess is not a way in; the bot answers only a sender whose code is live,
so a stranger learns nothing from trying.

NAMED BOUNDARY: first-party only, like the channel registry (`vaf/core/channels.py`): no
facade export, no embedder has a shared bot of their own to pair on. Re-measure when one does.
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple

PAIRING_TTL_SECONDS = 600

_codes: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


def _drop_expired(now: float) -> None:
    for code in [c for c, rec in _codes.items() if rec["expires"] <= now]:
        del _codes[code]


def issue_pairing_code(channel: str, user_scope_id: str, username: str) -> str:
    """A fresh code for this account on this channel; an earlier one of theirs is withdrawn."""
    scope = str(user_scope_id or "").strip()
    if not scope:
        raise ValueError("a pairing needs the account's scope")
    now = time.time()
    code = secrets.token_urlsafe(9)
    with _lock:
        _drop_expired(now)
        for old in [c for c, rec in _codes.items() if rec["channel"] == channel and rec["scope"] == scope]:
            del _codes[old]
        _codes[code] = {"channel": channel, "scope": scope, "username": str(username or "").strip(),
                        "expires": now + PAIRING_TTL_SECONDS}
    return code


def redeem_pairing_code(channel: str, code: str) -> Optional[Tuple[str, str]]:
    """(scope, username) of the account that asked for this code, or None. Spent on use."""
    code = str(code or "").strip()
    if not code:
        return None
    with _lock:
        _drop_expired(time.time())
        rec = _codes.get(code)
        if rec is None or rec["channel"] != channel:
            return None
        del _codes[code]
    return rec["scope"], rec["username"]


def pairing_pending(channel: str, user_scope_id: str) -> bool:
    """Whether this account holds a live code on this channel."""
    scope = str(user_scope_id or "").strip()
    with _lock:
        _drop_expired(time.time())
        return any(rec["channel"] == channel and rec["scope"] == scope for rec in _codes.values())


def pair_telegram_account(telegram_user_id: str, telegram_username: Optional[str], *,
                          user_scope_id: str, username: str, may_take_over: bool,
                          switch_on: bool = False) -> str:
    """Write one owner entry on the Telegram whitelist: this Telegram account belongs to that
    VAF account. The one writer, for the setup wizard's step and for a pairing code.

    Returns "paired", "unchanged" (the same pairing again), or "taken" (the Telegram account
    is another account's and `may_take_over` is False: only an admin may move it). The block
    need not be verified yet: the wizard pairs before it saves the bot as set up, and a code
    is only issued for a running bot (`POST /api/telegram/pair`). `switch_on` also turns the account's own Telegram
    switch on (`connection_enabled_by_scope`), which a pairing by code does: asking for one
    is the account saying it wants the lane. Under the config lock from load to save.
    """
    from vaf.core.config import Config

    tid = str(telegram_user_id or "").strip()
    scope = str(user_scope_id or "").strip()
    if not tid or not scope:
        raise ValueError("a pairing needs a Telegram id and the account's scope")
    entry = {"telegram_user_id": tid, "telegram_username": (telegram_username or "").strip() or None,
             "user_scope_id": scope, "vaf_username": str(username or "").strip()}
    with Config._locked():
        config = Config.load()
        block = config.get("telegram_config")
        block = block if isinstance(block, dict) else {}
        whitelist = [e for e in (block.get("whitelist") or []) if isinstance(e, dict)]
        previous = next((e for e in whitelist if str(e.get("telegram_user_id") or "").strip() == tid), None)
        if previous is not None and str(previous.get("user_scope_id") or "").strip() != scope and not may_take_over:
            return "taken"
        new_pairing = previous is None or any(previous.get(k) != v for k, v in entry.items())
        dirty = new_pairing
        if new_pairing:
            config["telegram_config"] = {**block, "whitelist": [e for e in whitelist if e is not previous] + [entry]}
        if switch_on:
            by_scope = config.get("connection_enabled_by_scope")
            by_scope = by_scope if isinstance(by_scope, dict) else {}
            mine = by_scope.get(scope) if isinstance(by_scope.get(scope), dict) else {}
            if mine.get("telegram") is not True:
                config["connection_enabled_by_scope"] = {**by_scope, scope: {**mine, "telegram": True}}
                dirty = True
        if dirty:
            Config.save(config)
    # A new owner of this Telegram account is an access change; the same pairing sent again
    # (same id for the same account) is not, and records nothing.
    moved = previous is None or (str(previous.get("user_scope_id") or ""), str(previous.get("vaf_username") or "")) \
        != (entry["user_scope_id"], entry["vaf_username"])
    if moved:
        try:
            from vaf.core.security_events import log_security_event
            log_security_event("channel_paired", channel="telegram", username=entry["vaf_username"],
                               path=tid, detail=f"owner {tid}")
        except Exception:
            pass
    return "paired" if new_pairing else "unchanged"
