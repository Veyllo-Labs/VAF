# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Shared ingress policy for external messaging channels."""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple


_SUPPORTED_CHANNELS = ("telegram", "whatsapp", "discord")
_SUPPORTED_MODES = ("paired_only", "permissive")
# The channels whose bridge reports a contact match to evaluate_ingress, so a contact with
# "Can reach your assistant" can be let in there at all, and whose bridge can enrol a new
# sender as a contact when the channel's Front Office is on. Discord's bridge passes
# contact_match=False (its one sender is the admin's own DM), so a Front Office switch for
# it would switch nothing, and the open door below never applies to it. A separate tuple
# on purpose: _SUPPORTED_CHANNELS is every routable channel and stays equal to
# ROUTABLE_CHANNELS. tests/test_front_office_settings.py holds this tuple against the
# bridges' call sites.
FRONT_OFFICE_CHANNELS = ("whatsapp", "telegram")
_THROTTLE_MIN = 5
_THROTTLE_MAX = 3600
_DEFAULT_THROTTLE = 60

_log_last: Dict[str, float] = {}
_log_lock = threading.Lock()


def _default_policy() -> Dict[str, Any]:
    return {
        "mode": "paired_only",
        "throttle_seconds": _DEFAULT_THROTTLE,
        "telegram": {"mode": "inherit", "allow_contact_fallback": False, "open_to_new_senders": False},
        "whatsapp": {"mode": "inherit", "allow_contact_fallback": False, "open_to_new_senders": False},
        "discord": {"mode": "inherit", "allow_contact_fallback": False, "open_to_new_senders": False},
    }


def normalize_policy(raw: Any) -> Dict[str, Any]:
    """Normalize channel ingress policy to a safe and complete shape."""
    policy = _default_policy()
    if not isinstance(raw, dict):
        return policy

    mode = str(raw.get("mode", "") or "").strip().lower()
    if mode in _SUPPORTED_MODES:
        policy["mode"] = mode

    throttle_raw = raw.get("throttle_seconds")
    try:
        throttle = int(throttle_raw)
    except Exception:
        throttle = _DEFAULT_THROTTLE
    throttle = max(_THROTTLE_MIN, min(_THROTTLE_MAX, throttle))
    policy["throttle_seconds"] = throttle

    for channel in _SUPPORTED_CHANNELS:
        src = raw.get(channel)
        if not isinstance(src, dict):
            continue
        out = dict(policy[channel])
        ch_mode = str(src.get("mode", "") or "").strip().lower()
        if ch_mode in (*_SUPPORTED_MODES, "inherit"):
            out["mode"] = ch_mode
        if "allow_contact_fallback" in src:
            out["allow_contact_fallback"] = bool(src.get("allow_contact_fallback"))
        if "open_to_new_senders" in src:
            out["open_to_new_senders"] = bool(src.get("open_to_new_senders"))
        policy[channel] = out
    return policy


def resolve_channel_policy(channel: str, raw_policy: Any) -> Dict[str, Any]:
    """Resolve effective mode and flags for one channel."""
    channel_name = str(channel or "").strip().lower()
    policy = normalize_policy(raw_policy)
    if channel_name not in _SUPPORTED_CHANNELS:
        return {"mode": policy["mode"], "allow_contact_fallback": False, "open_to_new_senders": False,
                "throttle_seconds": policy["throttle_seconds"]}

    ch = dict(policy.get(channel_name) or {})
    ch_mode = ch.get("mode", "inherit")
    mode = policy["mode"] if ch_mode == "inherit" else ch_mode
    if mode not in _SUPPORTED_MODES:
        mode = "paired_only"
    return {
        "mode": mode,
        "allow_contact_fallback": bool(ch.get("allow_contact_fallback", False)),
        # Only a Front Office channel can be open: its bridge enrols the sender as a
        # contact and runs the turn in Front Office mode. On any other channel the flag
        # is inert, whatever config.json says.
        "open_to_new_senders": bool(ch.get("open_to_new_senders", False)) and channel_name in FRONT_OFFICE_CHANNELS,
        "throttle_seconds": int(policy.get("throttle_seconds", _DEFAULT_THROTTLE)),
    }


def _contact_door_open(channel: str, raw_policy: Any) -> bool:
    resolved = resolve_channel_policy(channel, raw_policy)
    return resolved["mode"] == "permissive" or bool(resolved["allow_contact_fallback"])


def front_office_state(raw_policy: Any) -> Dict[str, Any]:
    """Front Office per channel, as the window shows it.

    `channels[ch]` is the switch "Front Office on for this channel": the channel is open to
    new senders (`open_to_new_senders`), which implies the contact door. `contacts_only[ch]`
    is the narrower expert state, contact door open but not the channel: only contacts with
    "Can reach your assistant" get in (`allow_contact_fallback` or a permissive mode).
    `enabled` is true when any Front Office channel is switched on. The other doors (the
    owner's own pairing, the WhatsApp reply window) are not read here; a window that shows
    this state must name the reply window separately, or "off" overstates what the bridge does.
    """
    channels = {ch: resolve_channel_policy(ch, raw_policy)["open_to_new_senders"] for ch in FRONT_OFFICE_CHANNELS}
    contacts_only = {ch: (not channels[ch]) and _contact_door_open(ch, raw_policy) for ch in FRONT_OFFICE_CHANNELS}
    return {"enabled": any(channels.values()), "channels": channels, "contacts_only": contacts_only}


def set_front_office(raw_policy: Any, enabled: bool, channel: Optional[str] = None) -> Dict[str, Any]:
    """The policy with Front Office switched on or off for one channel (or every one). Pure:
    returns a normalized copy, the input is untouched.

    On means the channel is open: every sender is answered in Front Office mode unless the
    owner switched that person off in the contact book, and a new sender is enrolled as a
    contact by the bridge; so both `open_to_new_senders` and the contact door
    (`allow_contact_fallback`) are set. The modes are left alone: `permissive` is the state
    the security doctor and the overview warn about, and a deliberately opened Front Office
    is not a misconfiguration. Off clears both flags and, where the channel's resolved mode
    is permissive, pins that channel to paired_only, so "off" is off under the expert
    setting too. The global mode and the throttle are never written.
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
        entry["allow_contact_fallback"] = bool(enabled)
        entry["open_to_new_senders"] = bool(enabled)
        if not enabled and resolve_channel_policy(name, policy)["mode"] == "permissive":
            entry["mode"] = "paired_only"
        policy[name] = entry
    return policy


def evaluate_ingress(
    channel: str,
    raw_policy: Any,
    explicit_match: bool,
    contact_match: bool,
    conversation_match: bool = False,
    sender_opted_out: bool = False,
) -> Tuple[bool, str]:
    """
    Evaluate whether inbound sender is allowed.

    explicit_match: sender matched explicit pairing (e.g. whitelist / verified admin).
    contact_match: sender matched contact-based fallback.
    conversation_match: the agent itself wrote to this sender recently (the bridge
        decides the window). Accepted in every mode, because the door was opened by
        the agent's own outbound message, not by a stranger; the reply lands in Front
        Office (restricted tools), never as the owner. Reason: "open_conversation".
    sender_opted_out: the sender has a contact record whose "Can reach your assistant" is
        OFF. With the channel's Front Office switched on (`open_to_new_senders`) every
        other sender is let in as a Front Office contact, reason "front_office_open", and
        the bridge enrols an unknown one; the record with the flag off is how the owner
        keeps one person out of an open channel.
    """
    resolved = resolve_channel_policy(channel, raw_policy)
    mode = resolved["mode"]
    if explicit_match:
        return True, "explicit_pair"
    if conversation_match:
        return True, "open_conversation"
    if mode == "permissive" and contact_match:
        return True, "contact_fallback"
    if mode == "paired_only" and resolved["allow_contact_fallback"] and contact_match:
        return True, "contact_fallback_override"
    if resolved["open_to_new_senders"] and not sender_opted_out:
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
