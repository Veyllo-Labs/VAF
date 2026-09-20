# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Shared ingress policy for external messaging channels."""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple


_SUPPORTED_CHANNELS = ("telegram", "whatsapp", "discord")
# One mode is left, and it is the floor rather than a choice: who may write in is decided by
# the channel switch (`open_to_new_senders`) and by the person's own decision in the contact
# book. `permissive` and the per-channel `allow_contact_fallback` used to be a second way to
# say "let contacts in", in a place nobody looked; a stored `permissive` is read once and
# coerced to this, so an old config keeps working and carries no word that means nothing.
_SUPPORTED_MODES = ("paired_only",)
_LEGACY_MODES = ("permissive",)
# The channels whose bridge admits a contact (contacts_store.admit_front_office_sender, or
# a contact match handed to evaluate_ingress), so a contact with "Can reach your assistant"
# can be let in there at all, and whose bridge enrols a new sender as a contact when the
# channel's Front Office is on. Discord's lane is the local admin's alone and answers
# direct messages only (the bot sees every guild channel it sits in); a stranger's DM is
# admitted as a contact of the admin's book. A separate tuple on purpose: _SUPPORTED_CHANNELS
# is every routable channel and stays equal to ROUTABLE_CHANNELS, and a channel that gains
# a bridge without a contact lane must not become a Front Office channel by being routable.
# tests/test_front_office_settings.py holds this tuple against the bridges' call sites.
#
# Mail is a Front Office channel without being a messenger: it is an INGRESS lane (the
# mail sync hands new mail to the answering lane in vaf/mail/inbound.py) and never a
# routable one (`send_to_user` cannot mail, KNOWN_CHANNELS does not list it). Its policy
# entry carries two fields of its own: `reply_mode` (draft: the answer waits for the
# owner's approval in the outbox; send: it leaves at once) and `opened_at` (the moment
# the channel was switched on; mail sent before it is never answered, so switching on
# never answers a backlog).
MAIL_CHANNEL = "email"
MESSENGER_FRONT_OFFICE_CHANNELS = ("whatsapp", "telegram", "discord")
FRONT_OFFICE_CHANNELS = MESSENGER_FRONT_OFFICE_CHANNELS + (MAIL_CHANNEL,)
_POLICY_CHANNELS = _SUPPORTED_CHANNELS + (MAIL_CHANNEL,)
MAIL_REPLY_MODES = ("draft", "send")
_THROTTLE_MIN = 5
_THROTTLE_MAX = 3600
_DEFAULT_THROTTLE = 60

_log_last: Dict[str, float] = {}
_log_lock = threading.Lock()


def _default_policy() -> Dict[str, Any]:
    return {
        "mode": "paired_only",
        "throttle_seconds": _DEFAULT_THROTTLE,
        "telegram": {"mode": "inherit", "open_to_new_senders": False},
        "whatsapp": {"mode": "inherit", "open_to_new_senders": False},
        "discord": {"mode": "inherit", "open_to_new_senders": False},
        "email": {"mode": "inherit", "open_to_new_senders": False,
                  "reply_mode": "draft", "opened_at": 0},
    }


def normalize_policy(raw: Any) -> Dict[str, Any]:
    """Normalize channel ingress policy to a safe and complete shape."""
    policy = _default_policy()
    if not isinstance(raw, dict):
        return policy

    mode = str(raw.get("mode", "") or "").strip().lower()
    if mode in _SUPPORTED_MODES:
        policy["mode"] = mode
    # A config written before the doors were merged: read it, do not honour it, do not keep
    # the word. Dropping it silently to the default would also lose mail's `opened_at`, so it
    # is coerced here rather than rejected upstream.
    elif mode in _LEGACY_MODES:
        policy["mode"] = "paired_only"

    throttle_raw = raw.get("throttle_seconds")
    try:
        throttle = int(throttle_raw)
    except Exception:
        throttle = _DEFAULT_THROTTLE
    throttle = max(_THROTTLE_MIN, min(_THROTTLE_MAX, throttle))
    policy["throttle_seconds"] = throttle

    for channel in _POLICY_CHANNELS:
        src = raw.get(channel)
        if not isinstance(src, dict):
            continue
        out = dict(policy[channel])
        ch_mode = str(src.get("mode", "") or "").strip().lower()
        if ch_mode in (*_SUPPORTED_MODES, "inherit"):
            out["mode"] = ch_mode
        elif ch_mode in _LEGACY_MODES:
            out["mode"] = "paired_only"
        if "open_to_new_senders" in src:
            out["open_to_new_senders"] = bool(src.get("open_to_new_senders"))
        if channel == MAIL_CHANNEL:
            mode_raw = str(src.get("reply_mode", "") or "").strip().lower()
            if mode_raw in MAIL_REPLY_MODES:
                out["reply_mode"] = mode_raw
            try:
                out["opened_at"] = max(0, int(src.get("opened_at") or 0))
            except (TypeError, ValueError):
                out["opened_at"] = 0
        policy[channel] = out
    return policy


def resolve_channel_policy(channel: str, raw_policy: Any) -> Dict[str, Any]:
    """Resolve effective mode and flags for one channel."""
    channel_name = str(channel or "").strip().lower()
    policy = normalize_policy(raw_policy)
    if channel_name not in _POLICY_CHANNELS:
        return {"mode": policy["mode"], "open_to_new_senders": False,
                "throttle_seconds": policy["throttle_seconds"]}

    ch = dict(policy.get(channel_name) or {})
    ch_mode = ch.get("mode", "inherit")
    mode = policy["mode"] if ch_mode == "inherit" else ch_mode
    if mode not in _SUPPORTED_MODES:
        mode = "paired_only"
    out = {
        "mode": mode,
        # Only a Front Office channel can be open: its bridge enrols the sender as a
        # contact and runs the turn in Front Office mode. On any other channel the flag
        # is inert, whatever config.json says.
        "open_to_new_senders": bool(ch.get("open_to_new_senders", False)) and channel_name in FRONT_OFFICE_CHANNELS,
        "throttle_seconds": int(policy.get("throttle_seconds", _DEFAULT_THROTTLE)),
    }
    if channel_name == MAIL_CHANNEL:
        out["reply_mode"] = str(ch.get("reply_mode") or "draft")
        out["opened_at"] = int(ch.get("opened_at") or 0)
    return out


def front_office_state(raw_policy: Any) -> Dict[str, Any]:
    """Front Office per channel, as the window shows it.

    `channels[ch]` is the switch "Front Office on for this channel": the channel is open to
    everybody the owner has not decided about. `enabled` is true when any Front Office channel
    is switched on. What the switch does NOT say is who the owner allowed or denied by hand:
    an allowed contact is answered on every channel, a denied one on none, and neither state
    is visible here - a window showing only this must say so, or "off" overstates what the
    bridges do. The narrower "contacts only" state is gone with the expert doors: allowing a
    person IS the contacts-only state now, and it is per person rather than per channel.
    """
    channels = {ch: resolve_channel_policy(ch, raw_policy)["open_to_new_senders"] for ch in FRONT_OFFICE_CHANNELS}
    mail = resolve_channel_policy(MAIL_CHANNEL, raw_policy)
    return {"enabled": any(channels.values()), "channels": channels,
            "email_reply_mode": mail["reply_mode"], "email_opened_at": mail["opened_at"]}


def set_email_reply_mode(raw_policy: Any, mode: str) -> Dict[str, Any]:
    """The policy with the mail channel's reply mode set (draft or send). Pure."""
    name = str(mode or "").strip().lower()
    if name not in MAIL_REPLY_MODES:
        raise ValueError(f"not a mail reply mode: {mode!r}")
    policy = normalize_policy(raw_policy)
    entry = dict(policy[MAIL_CHANNEL])
    entry["reply_mode"] = name
    policy[MAIL_CHANNEL] = entry
    return policy


def set_front_office(raw_policy: Any, enabled: bool, channel: Optional[str] = None,
                     now: Optional[float] = None) -> Dict[str, Any]:
    """The policy with Front Office switched on or off for one channel (or every one). Pure:
    returns a normalized copy, the input is untouched.

    On means the channel is open: everybody the owner has not decided about is answered in
    Front Office mode, and a new sender is enrolled as a contact by the bridge - without a
    decision, so closing the channel closes it for them again. Off clears the flag, and that
    is all it clears: the switch writes ONE field and never touches the contact book, so a
    person the owner allowed keeps their access and a person they denied stays out. The
    global mode and the throttle are never written.
    """
    policy = normalize_policy(raw_policy)
    if channel is None:
        targets = FRONT_OFFICE_CHANNELS
    else:
        name = str(channel or "").strip().lower()
        if name not in FRONT_OFFICE_CHANNELS:
            raise ValueError(f"not a Front Office channel: {channel!r}")
        targets = (name,)
    for name in targets:
        entry = dict(policy[name])
        entry["open_to_new_senders"] = bool(enabled)
        if name == MAIL_CHANNEL and enabled:
            # Switching mail on stamps the moment: only mail sent after it is answered,
            # so neither the backlog nor what arrived while the channel was off gets a
            # reply. Re-enabling stamps again for the same reason.
            entry["opened_at"] = int(now if now is not None else time.time())
        policy[name] = entry
    return policy


def evaluate_ingress(
    channel: str,
    raw_policy: Any,
    explicit_match: bool,
    access: Optional[str] = None,
    case_reply: bool = False,
    door_open: Optional[bool] = None,
) -> Tuple[bool, str]:
    """May this sender's message be handed to the agent? (allowed, reason)

    Two inputs decide it, and they answer different questions:

    - the CHANNEL switch (`open_to_new_senders`, "Inbound" in the window) says whether people
      the owner has not decided about are answered on this channel at all;
    - the PERSON's own decision in the contact book (`contacts_store.contact_access`) says
      whether this one human is answered, and it holds on every channel where they have an
      endpoint, open or closed.

    So: the owner's own paired endpoint keeps the full chat; a DENIED contact is never
    answered, whatever the channel says; an ALLOWED contact is answered even on a channel
    switched off, because the owner said so about the person; a sender nobody has decided
    about is answered only while the channel stands open, and the bridge then enrols them.
    Anything else is refused and the message is only stored for the owner's inbox.

    `door_open` is the channel switch as the CALLER knows it, and it replaces the flag read
    here. The flag alone is not the whole door: Telegram answers a stranger only while exactly
    one account is paired on the shared bot, and WhatsApp forwards nothing at all while
    `inbound_to_agent` is off (`messaging_connections.front_office_open` folds both). A caller
    that has asked there passes the answer in, so the row it renders and the bridge that would
    answer cannot disagree. Left out, the raw flag decides as before.

    `case_reply` is MAIL ONLY: a reply that carries the case anchor this agent minted into its
    own outgoing Message-ID (vaf/mail/case_token.py), which is proof that it answers a mail the
    agent sent in that case. A correspondence the owner started themselves is not stranded by a
    switch, and nothing can leave unseen there anyway (mail answers are drafts by default).

    WHAT WAS REMOVED, and why it is not coming back: a 72 hour "reply window" used to admit
    anyone the agent had written to, in every mode. It made the switch mean less than it says -
    the owner had the agent send one message on a channel set to "paired only", and from then
    on that person could write in and be answered for three days (live incident). And the two
    expert doors in config.json (`permissive`, `allow_contact_fallback`) said the same thing as
    the contact's own flag, in a second place where nobody looked; the flag decides now.
    """
    resolved = resolve_channel_policy(channel, raw_policy)
    is_mail = str(channel or "").strip().lower() == MAIL_CHANNEL
    decision = str(access or "").strip().lower()
    door = resolved["open_to_new_senders"] if door_open is None else bool(door_open)
    if explicit_match:
        return True, "explicit_pair"
    if decision == "denied":
        return False, "contact_denied"
    if decision == "allowed":
        return True, "contact_allowed"
    if case_reply and is_mail:
        return True, "open_conversation"
    if door:
        return True, "front_office_open"
    return False, "not_paired"


def should_log_unauthorized(channel: str, sender_id: str, raw_policy: Any) -> bool:
    """Throttle unauthorized logs per channel+sender."""
    resolved = resolve_channel_policy(channel, raw_policy)
    throttle = int(resolved.get("throttle_seconds", _DEFAULT_THROTTLE))
    key = f"{str(channel or '').strip().lower()}:{str(sender_id or '').strip()}"
    now = time.time()
    with _log_lock:
        last = float(_log_last.get(key, 0.0))
        if now - last < throttle:
            return False
        _log_last[key] = now
        return True
