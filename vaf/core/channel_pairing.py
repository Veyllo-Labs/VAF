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
72 random bits by default, so a guess is not a way in; the bot answers only a sender whose
code is live, so a stranger learns nothing from trying. The second user is the Veyllo App
link (vaf/core/app_link/pairing.py), which asks for 32 bytes (``nbytes``), the size its
protocol fixes for the one-time value in the QR, and asks ``pairing_code_state`` to tell a
spent code from one that never existed. A spent code is remembered only as its SHA-256 and
only until its original expiry.

A relay contact (`telegram_config.relay_whitelist`) is a person whose messages are passed on
to an account, with a fixed reply and no agent. Its proof is the same code, on its own
channel (`RELAY_PAIRING_CHANNEL`, so asking for one does not withdraw the account's owner
code): the account hands the link to the person, and only the Telegram account that sends
it becomes the contact. An id typed into a form proves nothing about who holds it, so the
add by id is an admin's (`pair_telegram_relay(..., paired_by="admin")`), like the wizard's
owner entry.

A WhatsApp owner number (`whatsapp_config.whitelist`) is the number an account chats with its
agent from. It has one writer too, `pair_whatsapp_number`, which changes only the entry of
the account it is told and never matches by name, and `refile_whatsapp_owner_numbers` moves
the entries older versions filed under an admin's own scope to the account they were
registered for.

NAMED BOUNDARY: first-party only, like the channel registry (`vaf/core/channels.py`): no
facade export, no embedder has a shared bot of their own to pair on, and the WhatsApp bridge
and its routes are the harness's (`vaf/api`); no facade name references a messenger
whitelist. Re-measure when one does.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PAIRING_TTL_SECONDS = 600
# The channel a relay code is issued on: codes are kept per (channel, account), so a relay
# code and the account's own pairing code live side by side.
RELAY_PAIRING_CHANNEL = "telegram_relay"
# A relay contact is a person: a Telegram user id is a positive number (a group chat's id is
# negative), and 20 digits hold every 64-bit value.
_RELAY_ID = re.compile(r"[0-9]{1,20}")
RELAY_PAIRED_BY = ("code", "admin")

_codes: Dict[str, Dict[str, Any]] = {}
# sha256(code) -> (channel, original expiry) of a spent code, so a second use is "used".
_spent: Dict[str, Tuple[str, float]] = {}
_lock = threading.Lock()


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _drop_expired(now: float) -> None:
    for code in [c for c, rec in _codes.items() if rec["expires"] <= now]:
        del _codes[code]
    for digest in [d for d, (_ch, until) in _spent.items() if until <= now]:
        del _spent[digest]


def issue_pairing_code(channel: str, user_scope_id: str, username: str, *, nbytes: int = 9) -> str:
    """A fresh code for this account on this channel; an earlier one of theirs is withdrawn.
    ``nbytes`` random bytes, URL-safe base64 (9 for Telegram's code, 32 for the app link)."""
    scope = str(user_scope_id or "").strip()
    if not scope:
        raise ValueError("a pairing needs the account's scope")
    now = time.time()
    code = secrets.token_urlsafe(int(nbytes))
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
        _spent[_digest(code)] = (channel, rec["expires"])
    return rec["scope"], rec["username"]


def pairing_code_state(channel: str, code: str) -> str:
    """"live", "used" (spent, its original lifetime not over) or "unknown" (never issued,
    expired or withdrawn), without spending it."""
    code = str(code or "").strip()
    if not code:
        return "unknown"
    with _lock:
        _drop_expired(time.time())
        rec = _codes.get(code)
        if rec is not None and rec["channel"] == channel:
            return "live"
        spent = _spent.get(_digest(code))
        if spent is not None and spent[0] == channel:
            return "used"
    return "unknown"


def withdraw_pairing_code(channel: str, user_scope_id: str) -> bool:
    """Withdraw this account's live code on this channel. Whether there was one."""
    scope = str(user_scope_id or "").strip()
    with _lock:
        mine = [c for c, rec in _codes.items() if rec["channel"] == channel and rec["scope"] == scope]
        for code in mine:
            del _codes[code]
    return bool(mine)


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
    switch on (`Config.set_own_channel_switch`), which a pairing by code does: asking for one
    is the account saying it wants the lane. A relay entry with this id is removed in the
    same write (recorded as `channel_unpaired`): a paired Telegram account is reached as its
    owner, and the relay writer refuses the reverse order (`pair_telegram_relay`), so an id
    is never on both lists. Under the config lock from load to save.
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
        relay = [e for e in (block.get("relay_whitelist") or []) if isinstance(e, dict)]
        unrelayed = [e for e in relay if str(e.get("telegram_user_id") or "").strip() == tid]
        dirty = new_pairing or bool(unrelayed)
        if dirty:
            block = {**block}
            if new_pairing:
                block["whitelist"] = [e for e in whitelist if e is not previous] + [entry]
            if unrelayed:
                block["relay_whitelist"] = [e for e in relay if all(e is not u for u in unrelayed)]
            config["telegram_config"] = block
        if switch_on and Config.set_own_channel_switch(config, scope, "telegram", True):
            dirty = True
        if dirty:
            Config.save(config)
    # A new owner of this Telegram account is an access change; the same pairing sent again
    # (same id for the same account) is not, and records nothing.
    moved = previous is None or (str(previous.get("user_scope_id") or ""), str(previous.get("vaf_username") or "")) \
        != (entry["user_scope_id"], entry["vaf_username"])
    try:
        from vaf.core.security_events import log_security_event
        for gone in unrelayed:
            log_security_event("channel_unpaired", channel="telegram", username=str(gone.get("vaf_username") or ""),
                               path=tid, detail=f"relay {tid}")
        if moved:
            log_security_event("channel_paired", channel="telegram", username=entry["vaf_username"],
                               path=tid, detail=f"owner {tid}")
    except Exception:
        pass
    return "paired" if new_pairing else "unchanged"


def pair_telegram_relay(telegram_user_id: str, telegram_username: Optional[str], *,
                        user_scope_id: str, username: str, may_take_over: bool, paired_by: str,
                        switch_on: bool = False) -> str:
    """Write one relay entry: messages from this Telegram account are passed on to that VAF
    account. The one writer, for an admin's add by id and for a relay code the bot redeems.

    Returns:
    - "owner": the id is on the whitelist, anybody's owner pairing (the caller's own too).
      Nothing is written: a paired Telegram account is reached as its owner, and as a relay
      entry it would only make its conversation look like the relay account's.
    - "taken": another account's relay contact, and `may_take_over` is False (only an admin
      may move one).
    - "unchanged": the same entry again.
    - "paired": the entry is written, replacing an earlier one with this id.

    `paired_by` ("code" or "admin") records how the id was proven, so an entry written before
    this rule (it has none) stays recognisable. `switch_on` turns the account's own Telegram
    switch on, as a pairing by code does. Raises ValueError for an id that is not a person's
    Telegram id or a missing scope. Under the config lock from load to save.
    """
    from vaf.core.config import Config

    tid = str(telegram_user_id or "").strip()
    scope = str(user_scope_id or "").strip()
    if not _RELAY_ID.fullmatch(tid) or not scope or paired_by not in RELAY_PAIRED_BY:
        raise ValueError("a relay contact needs a Telegram user id, the account's scope and its proof")
    entry = {"telegram_user_id": tid, "telegram_username": (telegram_username or "").strip() or None,
             "user_scope_id": scope, "vaf_username": str(username or "").strip(), "paired_by": paired_by}
    with Config._locked():
        config = Config.load()
        block = config.get("telegram_config")
        block = block if isinstance(block, dict) else {}
        if any(isinstance(e, dict) and str(e.get("telegram_user_id") or "").strip() == tid
               for e in (block.get("whitelist") or [])):
            return "owner"
        relay = [e for e in (block.get("relay_whitelist") or []) if isinstance(e, dict)]
        previous = next((e for e in relay if str(e.get("telegram_user_id") or "").strip() == tid), None)
        if previous is not None and str(previous.get("user_scope_id") or "").strip() != scope and not may_take_over:
            return "taken"
        new_entry = previous is None or any(previous.get(k) != v for k, v in entry.items())
        dirty = new_entry
        if new_entry:
            config["telegram_config"] = {**block, "relay_whitelist": [e for e in relay if e is not previous] + [entry]}
        if switch_on and Config.set_own_channel_switch(config, scope, "telegram", True):
            dirty = True
        if dirty:
            Config.save(config)
    # Access moved only when there was no entry or it was another account's; the same contact
    # added again, or a new display name, records nothing.
    moved = previous is None or (str(previous.get("user_scope_id") or ""), str(previous.get("vaf_username") or "")) \
        != (entry["user_scope_id"], entry["vaf_username"])
    if moved:
        try:
            from vaf.core.security_events import log_security_event
            log_security_event("channel_paired", channel="telegram", username=entry["vaf_username"],
                               path=tid, detail=f"relay {tid}")
        except Exception:
            pass
    return "paired" if new_entry else "unchanged"


def _whatsapp_whitelist(config: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Any]]:
    block = config.get("whatsapp_config")
    block = block if isinstance(block, dict) else {}
    whitelist = block.get("whitelist")
    return block, list(whitelist) if isinstance(whitelist, list) else []


def _whatsapp_entry_key(entry: Dict[str, Any]) -> Tuple[str, str, str]:
    """(number, scope, name) of an owner entry, stripped."""
    return (str(entry.get("phone_number") or "").strip(), str(entry.get("user_scope_id") or "").strip(),
            str(entry.get("vaf_username") or "").strip())


def pair_whatsapp_number(phone_number: str, *, user_scope_id: str, username: str, changed_by: str) -> str:
    """Write one owner entry on the WhatsApp whitelist: this number is the one that account
    chats with its agent from. The one writer, for `POST /api/whatsapp/whitelist/add`.

    Returns "added" (the account had no entry), "updated" (its entry now carries this number
    or this name) or "unchanged". The entry is found by the account's SCOPE only: the first
    entry filed under it is replaced, any other entry is left as it is, one without a scope
    included. A match by name, as the route used to make, let an ordinary account named
    "admin" overwrite the admin's entry filed under that name, and an admin registering for
    another account overwrite the admin's own entry. Who the account is, the caller decides
    (the route resolves a name an admin typed through the account directory).

    Records `channel_unpaired` for a number the entry no longer carries and `channel_paired`
    for the new one, `username` the account that made the change; "unchanged" records nothing.
    Raises ValueError without a number or a scope. Under the config lock from load to save.
    """
    from vaf.core.config import Config

    phone = str(phone_number or "").strip()
    scope = str(user_scope_id or "").strip()
    name = str(username or "").strip()
    if not phone or not scope:
        raise ValueError("an owner number needs the number and the account's scope")
    old_phone = ""
    with Config._locked():
        config = Config.load()
        block, whitelist = _whatsapp_whitelist(config)
        index = next((i for i, e in enumerate(whitelist)
                      if isinstance(e, dict) and str(e.get("user_scope_id") or "").strip() == scope), None)
        if index is None:
            whitelist.append({"phone_number": phone, "user_scope_id": scope, "vaf_username": name})
            block = {**block, "whitelist": whitelist}
            block.setdefault("enabled", True)
            status = "added"
        else:
            previous = whitelist[index]
            if (previous.get("phone_number"), previous.get("user_scope_id"), previous.get("vaf_username")) \
                    == (phone, scope, name):
                return "unchanged"
            old_phone = str(previous.get("phone_number") or "").strip()
            whitelist[index] = {**previous, "phone_number": phone, "user_scope_id": scope, "vaf_username": name}
            block = {**block, "whitelist": whitelist}
            status = "updated"
        config["whatsapp_config"] = block
        Config.save(config)
    # A new number for the account is two changes: the old number lost the owner's access,
    # the new one gained it (`path` keeps them apart in the throttle).
    try:
        from vaf.core.security_events import log_security_event
        who = str(changed_by or "")
        if old_phone and old_phone != phone:
            log_security_event("channel_unpaired", channel="whatsapp", username=who, path=old_phone,
                               detail=f"owner {old_phone} for {name}")
        log_security_event("channel_paired", channel="whatsapp", username=who, path=phone,
                           detail=f"owner {phone} for {name}")
    except Exception:
        pass
    return status


def refile_whatsapp_owner_numbers() -> List[Dict[str, str]]:
    """Move the owner numbers older versions filed under an admin's own scope to the account
    they were registered for. Run once each time the WhatsApp bridge starts
    (`whatsapp_bridge._run_bridge`); returns the changes made, `[{"action": "moved" |
    "removed", "phone_number", "vaf_username", "from_scope", "to_scope"}]`. Never raises.

    Why: an admin who registered a number for another account sent only that account's name,
    and the route filed it under the ADMIN's scope with that name. Every reader goes by scope
    (`messaging_connections._whatsapp_registered_entries`), so the admin's lanes, the ones
    without a scope included, delivered the admin's notes to that number, and the account it
    was registered for read it as the admin's and got its notifications in the web UI only.
    `pair_whatsapp_number` no longer writes that shape, and registering again does not repair
    an old one (it writes the account's own entry by scope and leaves the misfiled one).

    An entry is moved when all of these hold: it has a scope S, a name N and a number; N is
    not the ambiguous "admin" (the admin's legacy alias, and also an ordinary account's name
    while the admin is named otherwise); N resolves to an active account T
    (`config.account_for_username`: the exact name decides, a casefolded match only when one
    account has it) and T is not S; S is an admin's (`config.is_admin_account`), since only
    an admin's route call wrote that shape; and N is not, casefolded, the name of S's own
    account. The last rule holds whatever the lookup answers: with casefold twins such as
    "Weiß" and "weiss" the admin's own entry cannot be told from one registered for the
    twin, and the directory lists accounts in no fixed order. When T already holds the same
    number (`phone_digits_canonical`), the misfiled entry is removed instead of becoming a
    duplicate; an entry that is itself about to move does not count as held. Each move is
    `channel_unpaired` for S's account plus `channel_paired` for T, both naming the other
    side; a removal is `channel_unpaired` only, naming T. A moved entry no longer matches, so
    a second pass changes nothing and records nothing.

    Cost: one `account_for_username` per distinct name, one `is_admin_account` per distinct
    scope and one directory read for the account names in a pass, and none when no scoped
    entry carries a name; under the lock, one canonical number per entry, so the pass is
    linear in the entries. The directory is a database query, which is why this runs at the
    bridge start, off the event loop, and not on every read: every lane with an effect (a
    proactive send, the reply check) needs a running bridge. An empty directory (nothing
    registered, a store that is down) resolves no name, so nothing moves; that is logged.

    Named boundaries:
    - A deleted account whose name was later given to a new account: an entry an admin
      registered for the old one moves to the new one; the entry carries no date to tell them
      apart. The bridge's reply check already admitted that number for the new account by
      name. The `channel_paired` event names it, and an admin removes it.
    - Not moved: an entry under a non-admin's scope with another account's name (a hand edit
      of unknown intent), an entry under the scope of an account that is no longer an admin,
      an entry under the ambiguous name "admin", one whose name matches two accounts only by
      casefold, and one whose name casefold-matches the account it is filed under.
    - A settings save racing this pass can undo a move. The save (`config_routes.patch_config`,
      the WebSocket settings save in `web_server.py`) reads the config outside the lock and
      writes back the whitelist it read, so a move made in between returns to its old place
      while the log says "moved"; the next bridge start moves it again and logs it again. No
      number is lost or filed anywhere new. `whatsapp_bridge._drop_self_number_from_whitelist`
      writes without the lock too. Both are every out-of-request writer's race, not this
      pass's, and are not closed here.
    """
    try:
        return _refile_whatsapp_owner_numbers()
    except Exception:
        logger.warning("WhatsApp: correcting owner numbers filed under the wrong account failed", exc_info=True)
        return []


def _account_names() -> Dict[str, str]:
    """scope -> account name, from the directory and the local admin's config: whose scope an
    entry is filed under, read once per refile pass."""
    from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
    from vaf.core.tool_dispatch import resolve_account_directory

    names = {row["user_scope_id"]: row["username"] for row in resolve_account_directory()}
    admin_scope = str(get_local_admin_scope_id() or "").strip()
    if admin_scope:
        names[admin_scope] = get_local_admin_username()
    return names


def _refile_whatsapp_owner_numbers() -> List[Dict[str, str]]:
    from collections import deque

    from vaf.core.config import (Config, account_for_username, get_local_admin_username, is_admin_account,
                                 resolve_caller_username)
    from vaf.core.contacts_store import phone_digits_canonical

    _block, whitelist = _whatsapp_whitelist(Config.load())
    named = [key for key in (_whatsapp_entry_key(e) for e in whitelist if isinstance(e, dict)) if all(key)]
    if not named:
        return []
    admin_name = get_local_admin_username().strip().casefold()
    # Keyed by the name as stored, stripped: the exact name decides the lookup, so "Weiß" and
    # "WEISS" are two questions.
    targets: Dict[str, Optional[Tuple[str, str]]] = {}
    admins: Dict[str, bool] = {}
    owners: Optional[Dict[str, str]] = None
    candidates: List[Tuple[Tuple[str, str, str], Tuple[str, str]]] = []
    for key in named:
        _phone, scope, name = key
        if name.casefold() == "admin" and admin_name != "admin":
            continue
        if name not in targets:
            targets[name] = account_for_username(name)
        target = targets[name]
        if target is None or target[0] == scope:
            continue
        if scope not in admins:
            admins[scope] = is_admin_account(scope)
        if not admins[scope]:
            continue
        if owners is None:
            owners = _account_names()
        # A name that is, casefolded, the account the entry is filed under stays with that
        # account, whatever the lookup says: with twins such as "Weiß" and "weiss" the admin's
        # own entry cannot be told from one registered for the twin.
        if name.casefold() == str(owners.get(scope) or "").casefold():
            continue
        candidates.append((key, target))
    if targets and not any(targets.values()):
        logger.info("WhatsApp: no owner-number name resolved in the account directory; nothing corrected")
    if not candidates:
        return []

    def number(entry: Dict[str, Any]) -> str:
        raw = str(entry.get("phone_number") or "")
        return phone_digits_canonical(raw) or "raw:" + raw.strip()

    changes: List[Dict[str, str]] = []
    with Config._locked():
        config = Config.load()
        block, whitelist = _whatsapp_whitelist(config)
        positions: Dict[Tuple[str, str, str], Any] = {}
        for i, e in enumerate(whitelist):
            if isinstance(e, dict):
                positions.setdefault(_whatsapp_entry_key(e), deque()).append(i)
        # First every move, then the duplicates: an entry that is about to move away is not a
        # number its scope holds (two entries of one number, each filed under the other
        # account, would otherwise remove the first and move the second, and the first
        # account would lose the number).
        moves: List[Tuple[int, Dict[str, str], str]] = []
        for key, (target, target_name) in candidates:
            # The entry as it was read; one changed since is somebody's newer write.
            slots = positions.get(key)
            if not slots:
                continue
            index = slots.popleft()
            phone, scope, name = key
            moves.append((index, {"phone_number": phone, "vaf_username": name, "from_scope": scope,
                                  "to_scope": target}, target_name))
            whitelist[index] = {**whitelist[index], "user_scope_id": target}
        moved = {index for index, _c, _n in moves}
        # Held: by an entry that stays where it is, or by a move kept earlier in this pass.
        # One canonical number per entry, so the pass stays linear in the entries.
        held = {(str(e.get("user_scope_id") or "").strip(), number(e))
                for i, e in enumerate(whitelist) if isinstance(e, dict) and i not in moved}
        dropped = set()
        to_names: List[str] = []
        for index, change, target_name in moves:
            pair = (change["to_scope"], number(whitelist[index]))
            if pair in held:
                dropped.add(index)
            held.add(pair)
            changes.append({"action": "removed" if index in dropped else "moved", **change})
            to_names.append(target_name)
        whitelist = [e for i, e in enumerate(whitelist) if i not in dropped]
        if changes:
            config["whatsapp_config"] = {**block, "whitelist": whitelist}
            Config.save(config)

    from vaf.core.security_events import log_security_event
    owners = owners or {}
    for change, to_name in zip(changes, to_names):
        scope, phone = change["from_scope"], change["phone_number"]
        filed_under = owners.get(scope) or resolve_caller_username(None, scope)
        logger.warning("WhatsApp: an owner number registered for %r was filed under the account %r; %s",
                       to_name, filed_under,
                       "moved to its own account" if change["action"] == "moved" else "removed as a duplicate")
        try:
            if change["action"] == "moved":
                log_security_event("channel_unpaired", channel="whatsapp", username=filed_under, path=phone,
                                   detail=f"owner {phone} for {to_name}, filed under {filed_under}'s account; "
                                          "moved at WhatsApp start")
                log_security_event("channel_paired", channel="whatsapp", username=to_name, path=phone,
                                   detail=f"owner {phone} for {to_name}, moved from {filed_under}'s entry")
            else:
                log_security_event("channel_unpaired", channel="whatsapp", username=filed_under, path=phone,
                                   detail=f"owner {phone} removed from {filed_under}'s entries: {to_name} "
                                          "already has this number")
        except Exception:
            pass
    return changes
