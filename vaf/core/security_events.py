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

# Known event kinds (informational contract for consumers; unknown kinds pass through):
#   ip_blocked              - request from outside the allowed LAN ranges (403)
#   unauthenticated_blocked - LAN request without a token (401)
#   token_rejected          - LAN request with an invalid/expired token (401)
#   login_failed            - wrong username/password on /api/auth/login
#   twofa_failed            - wrong/expired 2FA code or temp token
#   ws_rejected             - WebSocket handshake rejected (IP/token)
#   channel_rejected        - unauthorized messenger sender dropped (telegram/
#                             whatsapp/discord); `channel` carries the platform,
#                             `username` the sender id (phone/user id)

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
