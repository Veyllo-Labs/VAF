"""Shared ingress policy for external messaging channels."""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Tuple


_SUPPORTED_CHANNELS = ("telegram", "whatsapp", "discord")
_SUPPORTED_MODES = ("paired_only", "permissive")
_THROTTLE_MIN = 5
_THROTTLE_MAX = 3600
_DEFAULT_THROTTLE = 60

_log_last: Dict[str, float] = {}
_log_lock = threading.Lock()


def _default_policy() -> Dict[str, Any]:
    return {
        "mode": "paired_only",
        "throttle_seconds": _DEFAULT_THROTTLE,
        "telegram": {"mode": "inherit", "allow_contact_fallback": False},
        "whatsapp": {"mode": "inherit", "allow_contact_fallback": False},
        "discord": {"mode": "inherit", "allow_contact_fallback": False},
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
        policy[channel] = out
    return policy


def resolve_channel_policy(channel: str, raw_policy: Any) -> Dict[str, Any]:
    """Resolve effective mode and flags for one channel."""
    channel_name = str(channel or "").strip().lower()
    policy = normalize_policy(raw_policy)
    if channel_name not in _SUPPORTED_CHANNELS:
        return {"mode": policy["mode"], "allow_contact_fallback": False, "throttle_seconds": policy["throttle_seconds"]}

    ch = dict(policy.get(channel_name) or {})
    ch_mode = ch.get("mode", "inherit")
    mode = policy["mode"] if ch_mode == "inherit" else ch_mode
    if mode not in _SUPPORTED_MODES:
        mode = "paired_only"
    return {
        "mode": mode,
        "allow_contact_fallback": bool(ch.get("allow_contact_fallback", False)),
        "throttle_seconds": int(policy.get("throttle_seconds", _DEFAULT_THROTTLE)),
    }


def evaluate_ingress(channel: str, raw_policy: Any, explicit_match: bool, contact_match: bool) -> Tuple[bool, str]:
    """
    Evaluate whether inbound sender is allowed.

    explicit_match: sender matched explicit pairing (e.g. whitelist / verified admin).
    contact_match: sender matched contact-based fallback.
    """
    resolved = resolve_channel_policy(channel, raw_policy)
    mode = resolved["mode"]
    if explicit_match:
        return True, "explicit_pair"
    if mode == "permissive" and contact_match:
        return True, "contact_fallback"
    if mode == "paired_only" and resolved["allow_contact_fallback"] and contact_match:
        return True, "contact_fallback_override"
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
