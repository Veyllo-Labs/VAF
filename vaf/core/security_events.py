# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Append-only security event log: blocked/rejected access attempts.

This is the network/auth slice of the dashboard's "gate events" audit log: who
tried to reach VAF and was turned away (non-LAN IPs, tokenless or invalid-token
LAN requests, failed logins/2FA, rejected WebSocket handshakes). Two sinks per
event, written together:

- ``security_events_<date>.jsonl`` - structured source of truth for the
  Overview dashboard (``GET /api/security/events``).
- ``security_<date>.log`` - human-readable mirror; the Logs window's file rail
  lists domains from ``<domain>_<date>.log`` automatically, so this file shows
  up there as the ``security`` domain without extra wiring.

Rules:
- NEVER log secrets: no passwords, no 2FA codes, no tokens. Usernames and IPs
  are fine (the reader endpoints are admin-only).
- Never raises; logging must not be able to break the request path.
- Flood throttle: repeated identical (kind, ip) events within a short window
  are dropped (an attacker hammering an endpoint must not grow the log
  unboundedly). Pattern mirrors channel_ingress_policy.should_log_unauthorized.
- Always on (independent of debug_logs_enabled): rejected access attempts are
  audit signal, not debug noise.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any, Dict, List

from vaf.core.log_helper import get_app_log_dir

# ── The event kinds, as DATA ─────────────────────────────────────────────────────────
#
# This used to be a prose comment listing the kinds, and every consumer kept its own
# copy of the list. Measured 2026-07-31, all four disagreed: the comment named 7 kinds,
# the dashboard's label switch named the same 7, the design doc's table named 12, and
# the code emitted 14. Half of everything that reaches the log had no label anywhere, so
# a reader would have been shown a raw identifier like `skill_blocked`.
#
# Nothing here VALIDATES: an unknown kind still passes through, because auditing must
# never drop an event over a bookkeeping mismatch. This is the SSOT that consumers read
# and that `tests/test_security_event_kinds_sync.py` holds against the emit sites and
# against the dashboard, so the four copies cannot drift apart again silently.
#
# Adding a kind: emit it, add the row here, add a label in the dashboard's `evKindLabel`
# plus the two message catalogs. The guard names whichever of those you forgot.
SECURITY_EVENT_KINDS: dict[str, str] = {
    # network / auth perimeter
    "ip_blocked": "Request from outside the allowed LAN ranges (403)",
    "unauthenticated_blocked": "LAN request without a token (401)",
    "token_rejected": "LAN request with an invalid or expired token (401)",
    "login_failed": "Wrong username or password on /api/auth/login",
    "twofa_failed": "Wrong or expired 2FA code or temp token",
    "ws_rejected": "Rejected network WebSocket handshake (IP/token)",
    "channel_rejected": "Unauthorized messenger sender dropped at ingress; "
                        "`channel` carries the platform, `username` the sender id",
    # mail
    "mail_high_risk_send_blocked": "Outgoing mail stopped as high-risk before sending",
    "mail_image_proxy_blocked": "Remote image proxy refused a host",
    # skills
    "skill_blocked": "HIGH scan result stopped a skill install or update",
    "skill_override": "Admin explicitly accepted a HIGH result (install or quarantine restore)",
    "skill_scan_alert": "Periodic re-scan found a worsened risk level (below high)",
    "skill_quarantined": "Skill quarantined (auto on worsened-to-high, or manual isolate)",
    "skill_removed": "Quarantined skill deleted from the dashboard",
}

_THROTTLE_S = 5.0
_last_emit: Dict[str, float] = {}
_lock = threading.Lock()


def log_security_event(kind: str, *, ip: str = "", username: str = "",
                       path: str = "", detail: str = "", channel: str = "") -> None:
    """Append one security event to both sinks. Throttled, never raises."""
    try:
        now = time.time()
        # Per-source throttle: distinct senders/users must not swallow each
        # other's events (e.g. two different rejected phone numbers).
        key = f"{kind}|{ip}|{username}|{channel}"
        with _lock:
            if now - _last_emit.get(key, 0.0) < _THROTTLE_S:
                return
            _last_emit[key] = now
            # keep the throttle map bounded
            if len(_last_emit) > 512:
                cutoff = now - _THROTTLE_S
                for k in [k for k, v in _last_emit.items() if v < cutoff]:
                    _last_emit.pop(k, None)

            stamp = datetime.now()
            day = stamp.strftime("%Y-%m-%d")
            log_dir = get_app_log_dir()
            log_dir.mkdir(parents=True, exist_ok=True)

            entry: Dict[str, Any] = {"ts": stamp.isoformat(timespec="seconds"), "kind": str(kind)}
            if channel:
                entry["channel"] = str(channel)[:32]
            if ip:
                entry["ip"] = str(ip)
            if username:
                entry["username"] = str(username)[:80]
            if path:
                entry["path"] = str(path)[:200]
            if detail:
                entry["detail"] = str(detail)[:200]

            with (log_dir / f"security_events_{day}.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

            human = " ".join(
                x for x in (
                    f"[{kind}]",
                    f"channel={entry.get('channel')}" if channel else "",
                    f"ip={ip}" if ip else "",
                    f"user={entry.get('username')}" if username else "",
                    f"path={entry.get('path')}" if path else "",
                    entry.get("detail", ""),
                ) if x
            )
            # ISO timestamp first: the Logs window's line parser renders it as the
            # timestamp column (parseLogLine expects "<iso>\s<rest>").
            with (log_dir / f"security_{day}.log").open("a", encoding="utf-8") as f:
                f.write(f"{stamp.isoformat()} {human}\n")
    except Exception:
        pass


def read_security_events(date: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Return the last ``limit`` structured events for ``date`` (YYYY-MM-DD), oldest first.

    Never raises; a missing file or bad lines yield fewer/no events.
    """
    events: List[Dict[str, Any]] = []
    try:
        path = get_app_log_dir() / f"security_events_{date}.jsonl"
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line in lines[-max(1, int(limit)):]:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    events.append(obj)
            except Exception:
                continue
    except Exception:
        return events
    return events
