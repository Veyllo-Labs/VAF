# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Append-only security event log: blocked/rejected access attempts.

This is the network/auth slice of the dashboard's "gate events" audit log: who
tried to reach VAF and was turned away (non-LAN IPs, tokenless or invalid-token
LAN requests, failed logins/2FA, rejected WebSocket handshakes), and who was let
in through a door that stays open (a messenger sender paired, a contact given
assistant access). Not in here: a messenger sender the agent refused to answer.
That is the channel's everyday traffic, recorded in the channel's own inbound
log (log_helper.log_channel_inbound); as a security event it lit the alert dot
on every stranger's message and buried the real signals. Two sinks per event,
written together:

- ``security_events_<date>.jsonl`` - structured source of truth for the
  Overview dashboard (``GET /api/security/events``).
- ``security_<date>.log`` - human-readable mirror; the Logs window's file rail
  lists domains from ``<domain>_<date>.log`` automatically, so this file shows
  up there as the ``security`` domain without extra wiring.

Rules:
- NEVER log secrets: no passwords, no 2FA codes, no tokens. Usernames and IPs
  are fine (the reader endpoints are admin-only).
- Never raises; logging must not be able to break the request path.
- One event, one line in the readable mirror: control characters and line separators in a
  value from outside become spaces there (a login name with a newline forged a second line).
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
    "ip_blocked": "A client outside the admitted networks, refused before any route (403, a "
                  "WebSocket 4003); with Local Network off, any client that is not this "
                  "machine. `detail` is the path",
    "unauthenticated_blocked": "LAN request without a token (401)",
    "token_rejected": "LAN request with an invalid or expired token (401)",
    "login_failed": "Wrong username or password on /api/auth/login",
    "twofa_failed": "Wrong or expired 2FA code or temp token",
    "login_throttled": "Sign-ins were held after repeated failures (429): `detail` names the "
                       "window that closed (address, account or second factor), `ip` the "
                       "client refused first, `username` the account. Once per closure",
    "login_new_address": "A sign-in completed from an address that completed none for this "
                         "account within 30 days (another device; the machine itself is not "
                         "counted). `ip` the address, `username` the account, `detail` the "
                         "device. The account is told on its main messenger and in its "
                         "notifications (vaf/auth/sign_in_alerts.py)",
    "password_changed": "An account's password was set anew: by the account itself or by an "
                        "admin in Settings, or in a terminal on this machine (`vaf users "
                        "reset-password`, with the proof it took in `detail`)",
    "program_token_created": "A token for programs was made for an account: in Settings, "
                             "Account with the account's 2FA code, or in a terminal on this "
                             "machine (`vaf users token`) with its proof. `username` the "
                             "account, `detail` the token's name, its days and where it was "
                             "made; never the token. It acts as the account until it ends or "
                             "is revoked",
    "backup_restored": "Everything VAF keeps on this machine was replaced by a backup's "
                       "(`vaf backup restore`). `path` the backup file, `detail` the proof the "
                       "person gave in the terminal, the safety backup of the state before and "
                       "whose backup it was",
    "backup_restore_failed": "A restore from a backup did not complete: it stopped half way "
                             "(this machine holds part of the backup), or sandbox environments "
                             "could not be recreated. `path` the backup file, `detail` what is "
                             "missing, the proof, the safety backup to go back to and whose "
                             "backup it was",
    "tls_certificate_changed": "The certificate every TLS listener serves was switched: to the "
                               "person's own (`vaf server cert set`, Settings), back to VAF's "
                               "own (`vaf server cert reset`), or the local authority was "
                               "renewed (`vaf server ca renew`). `username` who did it, "
                               "`detail` what is served now with the leaf and authority "
                               "fingerprint prefixes, and where it was done",
    "account_seal_broken": "An account row whose seal does not verify (changed in the database "
                           "outside VAF) was refused for a login, a token or its rights. "
                           "`path` names the check",
    "ws_rejected": "Rejected network WebSocket handshake (IP/token)",
    "foreign_origin_blocked": "A request or WebSocket that a browser marked as coming from "
                              "another web page (Origin, Sec-Fetch-Site, or a DNS name as "
                              "Host) was refused (403 / 4003), in every mode. `detail` the "
                              "reason (origin, site, host) and the origin or host",
    # messenger pairing: who may talk to the agent (the drops themselves are channel traffic)
    "channel_paired": "A sender was given access on a messenger channel: an owner number, a "
                      "Telegram whitelist or relay entry, the Discord admin, or a LID bound to "
                      "an allowed number. `channel` carries the platform, `username` who changed "
                      "it, `path` the paired id (so two changes seconds apart stay two events), "
                      "`detail` the role and the id. A write that changes nothing records nothing",
    "channel_unpaired": "A sender lost that access again; same fields",
    "owner_endpoint_reset": "The stored chat where VAF reaches an account on a messenger was "
                            "not that account's own paired chat and was reset; `channel` the "
                            "platform, `username` the account, `path` the discarded id, "
                            "`detail` what it was (a contact by name) and what the account is "
                            "reached on now",
    "contact_access_changed": "A contact's 'can reach your assistant' flag was switched, so the "
                              "contact may (or may no longer) talk to the agent's Front Office; "
                              "`username` who changed it, `path` the contact id, `detail` the "
                              "contact and the new state",
    "front_office_changed": "Front Office was switched on or off for a messenger channel in "
                            "Settings: on, everyone who writes there is answered in Front Office "
                            "mode and the channel's contacts in the book are allowed; `channel` "
                            "the platform, `username` who switched it, `detail` the new state and "
                            "the number of contacts granted. One event per channel that really "
                            "changed",
    # mail
    "mail_high_risk_send_blocked": "Outgoing mail stopped as high-risk before sending",
    "mail_image_proxy_blocked": "Remote image proxy refused a host",
    # outbound fetches
    "egress_blocked": "One of the agent's fetches (webfetch, download_file, a web search page "
                      "read, an MCP server, WebDAV) was refused because its host resolves to "
                      "this machine, the cloud metadata service or another address that is "
                      "never fetched (or to the LAN while egress_allow_private_hosts is off). "
                      "`username` the account, `detail` host, address and class",
    "mail_spoofed_own_domain": "A mail claiming one of the account's own domains, or the "
                               "owner's own address, did not authenticate with the provider "
                               "(DMARC fail or no aligned pass): somebody writes as the owner. "
                               "`username` the "
                               "account's owner, `channel` email, `detail` the masked sender",
    "mail_case_token_misuse": "A mail carried a case anchor that is not this account's (another "
                              "scope, another account, or tampered); the mail was not attributed. "
                              "`detail` the masked sender",
    "mail_auto_reply_capped": "The answering lane reached its per-address cap for the day; "
                              "further mail from that address waits for the owner. Once per "
                              "address and day, `detail` the masked address",
    # skills
    "skill_blocked": "HIGH scan result stopped a skill install or update",
    "skill_override": "Admin explicitly accepted a HIGH result (install or quarantine restore)",
    "skill_scan_alert": "Periodic re-scan found a worsened risk level (below high)",
    "skill_quarantined": "Skill quarantined (auto on worsened-to-high, or manual isolate)",
    "skill_removed": "Quarantined skill deleted from the dashboard",
    # rooms shared across accounts
    "room_account_admitted": "An account was let into a room shared across accounts; "
                             "every member of such a room reads everything said in it. "
                             "`path` carries the room id, `username` who admitted",
    "room_account_invited": "An account was invited into a room shared across accounts and "
                            "may accept or decline; nothing is readable until it accepts. "
                            "`path` carries the room id, `username` who invited, `detail` whom",
    # arriving content (attachments, uploads, shared folders, messenger files)
    "upload_blocked": "Arriving content matched the machine's known-bad hash list and was "
                      "refused; `channel` carries the lane it arrived on, `path` the digest prefix",
    "upload_flagged": "Arriving content tripped the static scanner (advisory only, not blocked)",
    "threat_listed": "A digest was added to the known-bad list",
    "threat_delisted": "A digest was removed from the known-bad list (admin, second factor)",
    # data at rest
    "cli_password_gate_failed": "Three wrong admin passwords at the interactive terminal",
    "default_db_password": "The memory database is still using the shipped default password; "
                           "fix: `vaf secure rotate-db` (ask your agent, or type it in a terminal)",
    # browser
    "browser_handover_failed": "A cross-user browser handover could not verify its profile "
                               "wipe and was refused (no lease granted); `username` carries "
                               "the requesting scope, `detail` the failing step",
    "browser_pool_fallback": "A user with the per-user browser pool ACTIVE was served the "
                             "shared fallback browser (or refused, under strict mode); "
                             "`detail` carries why no dedicated instance was available",
    "browser_image_stale": "The browser image exceeded its age budget and the fresh "
                           "rebuild FAILED, so the stack keeps serving a browser engine "
                           "without current security fixes; `detail` carries the age and "
                           "the build error",
}

_THROTTLE_S = 5.0
_last_emit: Dict[str, float] = {}
# The throttle is for what arrives from outside in bursts (a rejected sender, a scanner). A
# change a person made to the whole machine is never a burst, and a second one seconds after
# the first (the same backup restored twice, a certificate set and set back) is a second
# event, not a repeat.
_NEVER_THROTTLED = frozenset({"backup_restored", "backup_restore_failed",
                              "tls_certificate_changed"})
_lock = threading.Lock()


# What ends or breaks a line for a log reader: the C0 controls (newline, carriage return,
# vertical tab, form feed, ...), DEL, the C1 controls (NEL among them) and the Unicode line and
# paragraph separators. Each becomes a space in the readable line.
_LINE_BREAKERS = {c: " " for c in [*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0), 0x2028, 0x2029]}


def one_line(value) -> str:
    """`value` as one line of a log: line breaks and other control characters become spaces."""
    return str(value or "").translate(_LINE_BREAKERS)


def _field(value) -> str:
    """A key=value field of the readable line. A value that could pose as more fields or
    another event (a space, '=', a bracket or a quote in a username typed at the login) is
    quoted, so "x ip=127.0.0.1 [password_changed]" stays one visibly quoted name."""
    text = one_line(value)
    if any(ch.isspace() or ch in '=[]"' for ch in text):
        return json.dumps(text, ensure_ascii=False)
    return text


def _free_text(value) -> str:
    """The detail part of the readable line: free text, read as such, so spaces stay plain.
    Quoted when it carries what could pose as a field or another event ('=', a bracket or a
    quote): a refused request's own Origin header lands here, and any LAN client sets it."""
    text = one_line(value)
    if any(ch in '=[]"' for ch in text):
        return json.dumps(text, ensure_ascii=False)
    return text


def log_security_event(kind: str, *, ip: str = "", username: str = "",
                       path: str = "", detail: str = "", channel: str = "") -> None:
    """Append one security event to both sinks. Throttled, never raises."""
    try:
        now = time.time()
        # Per-source throttle: distinct senders/users must not swallow each
        # other's events (e.g. two different rejected phone numbers).
        # `path` is part of the key because two events of the same kind from the
        # same person are not the same event when they are about different things -
        # two rooms admitted seconds apart used to collapse into one line, and an
        # audit that silently drops the second is worse than no audit.
        key = f"{kind}|{ip}|{username}|{channel}|{path}"
        with _lock:
            if kind not in _NEVER_THROTTLED:
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

            # One event, one line. Every value here can carry text from outside (a username
            # typed at the login, a request path, an address header), and a line break in it
            # wrote a forged second line into the readable log (measured: a login name with
            # a newline produced "... [password_changed] user=admin ..."). The JSONL keeps the
            # value as it was; JSON escapes it.
            human = " ".join(
                x for x in (
                    f"[{one_line(kind)}]",
                    f"channel={_field(entry.get('channel'))}" if channel else "",
                    f"ip={_field(ip)}" if ip else "",
                    f"user={_field(entry.get('username'))}" if username else "",
                    f"path={_field(entry.get('path'))}" if path else "",
                    _free_text(entry.get("detail", "")),
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
