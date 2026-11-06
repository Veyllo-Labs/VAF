# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Pairing a Veyllo App with the signed-in account, in this order:

1. the account opens a pairing window with ITS OWN second factor (``open_window``, called by
   the route after the code was checked); no route takes another account's id;
2. ``issue`` mints the one-time value K (``channel_pairing``, 32 bytes) and, for the relay
   way, the relay value P (random, never derived from K) and returns the QR. Neither is
   logged nor stored on disk; an earlier live K of the account is withdrawn;
3. the app opens TLS with SNI ``pair.vaf.internal`` and sends ``pair`` (``on_pair``): the
   CSR is checked FIRST (a bad CSR does not burn K), then K: a spent K is ``used``, an
   unknown one ``expired``, a second pairing while one awaits confirmation ``busy``. Then K
   is spent and the app waits (``pair_wait``). Nothing exists yet;
4. the account confirms in the web UI that the fingerprint on the phone is the one shown
   (``confirm``) within 120 seconds: only then is the device row created, its certificate
   issued, the account's Veyllo App lane switched on, and ``paired`` sent.

State lives in this process (the web server's, where the K store is), like Telegram's codes.
"""
from __future__ import annotations

import ipaddress
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from vaf.core.app_link import CHANNEL
from vaf.core.app_link import protocol as pr

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 600
CONFIRM_SECONDS = 120


class PairingRefused(Exception):
    """Why a QR cannot be issued: ``window_closed``, ``relay_off`` or ``no_direct_path``."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class Pairing:
    scope: str
    username: str
    mode: str
    k: str
    exp: float
    qr: str
    p: Optional[bytes] = None
    state: str = "issued"
    member: Optional[str] = None
    cid: Optional[str] = None
    relay_error: str = ""
    device: Dict[str, Any] = field(default_factory=dict)
    public_key: Any = None
    session: Any = None
    awaiting_since: float = 0.0
    device_id: str = ""


_lock = threading.RLock()
_windows: Dict[str, float] = {}
_issued: Dict[str, Pairing] = {}
_awaiting: Dict[str, Pairing] = {}
# The outcome of the last pairing per account, for the setup window's poll.
_last: Dict[str, Pairing] = {}


def _clock() -> float:
    return time.time()


def _refused(reason: str, transport: str = "", username: str = "") -> None:
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("app_pairing_refused", channel=CHANNEL, username=username,
                           detail=f"{reason}, {transport or 'web'}")
    except Exception:
        pass


# ── the window ───────────────────────────────────────────────────────────────

def open_window(user_scope_id: str, username: str) -> float:
    """Open the account's pairing window. The CALLER verified the account's own second
    factor; this only remembers that it did."""
    scope = str(user_scope_id or "").strip()
    if not scope:
        raise ValueError("a pairing needs the account's scope")
    until = _clock() + WINDOW_SECONDS
    with _lock:
        _windows[scope] = until
    return until


def window_until(user_scope_id: str) -> Optional[float]:
    with _lock:
        until = _windows.get(str(user_scope_id or "").strip())
    return until if until and until > _clock() else None


# ── the QR ───────────────────────────────────────────────────────────────────

def direct_addresses() -> List[str]:
    """``wss://<address>:<https port>/api/app-link`` for every address devices are admitted
    on, only while Local Network and its TLS are on (the protocol is wss only)."""
    from vaf.core.config import Config
    if not (Config.get("local_network_enabled", False) and Config.get("local_network_tls_enabled", False)):
        return []
    try:
        from vaf.network.binding import access_addresses, resolve_lan_access_ports
        port, _ = resolve_lan_access_ports(wait_for_proxy=False)
        out = []
        for iface in access_addresses():
            host = iface.ip
            if ipaddress.ip_address(host).version == 6:
                host = f"[{host}]"
            url = f"wss://{host}:{int(port)}/api/app-link"
            if url not in out:
                out.append(url)
        return out
    except Exception:
        return []


def issue(user_scope_id: str, username: str, mode: str) -> Pairing:
    from vaf.core import channel_pairing
    from vaf.core.app_link import pki, settings
    scope = str(user_scope_id or "").strip()
    if mode not in ("relay", "local"):
        raise ValueError("mode is relay or local")
    if window_until(scope) is None:
        raise PairingRefused("window_closed")
    cfg = settings()
    direct = direct_addresses()
    p = None
    if mode == "relay":
        if not cfg.get("relay_enabled", True):
            raise PairingRefused("relay_off")
        p = secrets.token_bytes(pr.P_BYTES)
    elif not direct:
        raise PairingRefused("no_direct_path")
    k = channel_pairing.issue_pairing_code(CHANNEL, scope, username, nbytes=pr.K_BYTES)
    exp = int(_clock()) + pr.PAIRING_TTL_SECONDS
    from vaf.core.app_link import agent_name
    name = agent_name(username)
    qr = pr.build_qr("r" if mode == "relay" else "l", k=pr.b64url_decode(k),
                     ca_sha256=pki.ca_sha256(), exp=exp, p=p,
                     api=str(cfg.get("relay_api_base") or "") if mode == "relay" else None,
                     direct=direct, name=name or None)
    pairing = Pairing(scope=scope, username=str(username or ""), mode=mode, k=k, exp=exp, qr=qr, p=p)
    with _lock:
        _issued[scope] = pairing
        _last.pop(scope, None)
    return pairing


def withdraw(user_scope_id: str) -> bool:
    """Withdraw the account's live QR (its K, and the relay claim of its P)."""
    from vaf.core import channel_pairing
    scope = str(user_scope_id or "").strip()
    with _lock:
        had = _issued.pop(scope, None) is not None
    channel_pairing.withdraw_pairing_code(CHANNEL, scope)
    return had


def live_relay_pairings() -> List[Pairing]:
    """The relay pairings whose P may still be claimed (issued or waiting, unexpired)."""
    now = _clock()
    with _lock:
        rows = list(_issued.values()) + list(_awaiting.values())
    return [p for p in rows if p.mode == "relay" and p.p and p.exp > now and not p.member
            and p.state in ("issued", "claimed", "awaiting_confirm")]


def claimed_relay_pairings() -> List[Pairing]:
    """The live relay pairings the relay has claimed: the app may connect through it now."""
    now = _clock()
    with _lock:
        rows = list(_issued.values()) + list(_awaiting.values())
    return [p for p in rows if p.mode == "relay" and p.member and p.exp > now
            and p.state in ("claimed", "awaiting_confirm")]


def _by_p(p: bytes) -> Optional[Pairing]:
    with _lock:
        for pairing in list(_issued.values()) + list(_awaiting.values()):
            if pairing.p is not None and secrets.compare_digest(pairing.p, p):
                return pairing
    return None


def mark_claimed(p: bytes, member: str, cid: Optional[str]) -> None:
    pairing = _by_p(p)
    if pairing is None:
        return
    pairing.member = member
    pairing.cid = cid or pairing.cid
    if pairing.state == "issued":
        pairing.state = "claimed"


def mark_relay_outcome(p: bytes, state: str, error: str = "") -> None:
    """The relay said P is ``used_elsewhere`` (409) or ``expired`` (410): the K goes at once."""
    from vaf.core import channel_pairing
    pairing = _by_p(p)
    if pairing is None:
        return
    pairing.state = state
    pairing.relay_error = error
    with _lock:
        if _issued.get(pairing.scope) is pairing:
            del _issued[pairing.scope]
        _last[pairing.scope] = pairing
    channel_pairing.withdraw_pairing_code(CHANNEL, pairing.scope)
    if state == "used_elsewhere":
        _refused("used elsewhere", "relay", pairing.username)


def set_relay_error(error: str) -> None:
    with _lock:
        for pairing in _issued.values():
            if pairing.mode == "relay":
                pairing.relay_error = error


# ── the pair message ─────────────────────────────────────────────────────────

def _pair_error(session, code: str, reason: str, username: str = "") -> None:
    _refused(reason, getattr(session, "transport", ""), username)
    session.send_json({"t": "pair_error", "code": code})
    session.close("pair_error", bye=True)


def on_pair(session, message: Dict[str, Any]) -> None:
    from vaf.core import channel_pairing
    from vaf.core.app_link import pki
    try:
        public_key = pki.check_csr(message["csr"])
    except pki.CsrRefused:
        _pair_error(session, "bad_csr", "bad csr")
        return
    k = message["k"]
    state = channel_pairing.pairing_code_state(CHANNEL, k)
    if state == "used":
        _pair_error(session, "used", "used code")
        return
    if state != "live":
        _pair_error(session, "expired", "unknown or expired code")
        return
    with _lock:
        pairing = next((p for p in _issued.values() if secrets.compare_digest(p.k, k)), None)
        waiting = _awaiting.get(pairing.scope) if pairing is not None else None
    if pairing is None:
        _pair_error(session, "expired", "unknown or expired code")
        return
    if waiting is not None and waiting.session is not None and not waiting.session.closed:
        _pair_error(session, "busy", "busy", pairing.username)
        return
    owner = channel_pairing.redeem_pairing_code(CHANNEL, k)
    if owner is None or owner[0] != pairing.scope:
        _pair_error(session, "expired", "unknown or expired code", pairing.username)
        return
    pairing.public_key = public_key
    pairing.session = session
    pairing.state = "awaiting_confirm"
    pairing.awaiting_since = _clock()
    pairing.device = {"model": message["device"], "platform": message["platform"],
                      "app_version": message["app_version"],
                      "fingerprint": pki.display_fingerprint(public_key),
                      "transport": session.transport}
    if session.relay_cid:
        pairing.cid = session.relay_cid
    with _lock:
        if _issued.get(pairing.scope) is pairing:
            del _issued[pairing.scope]
        _awaiting[pairing.scope] = pairing
    session.send_json({"t": "pair_wait"})


# ── the confirmation ─────────────────────────────────────────────────────────

def _switch_lane_on(scope: str) -> None:
    from vaf.core.config import Config
    with Config._locked():
        config = Config.load()
        by_scope = config.get("connection_enabled_by_scope")
        by_scope = by_scope if isinstance(by_scope, dict) else {}
        mine = by_scope.get(scope) if isinstance(by_scope.get(scope), dict) else {}
        if mine.get(CHANNEL) is not True:
            config["connection_enabled_by_scope"] = {**by_scope, scope: {**mine, CHANNEL: True}}
            Config.save(config)


def _finish(pairing: Pairing, state: str) -> None:
    pairing.state = state
    with _lock:
        if _awaiting.get(pairing.scope) is pairing:
            del _awaiting[pairing.scope]
        _last[pairing.scope] = pairing


def confirm(user_scope_id: str, accept: bool) -> str:
    """The account's answer to "is this your device?". Only the account's own pairing."""
    from cryptography import x509

    from vaf.core.app_link import devices, pki
    scope = str(user_scope_id or "").strip()
    with _lock:
        pairing = _awaiting.get(scope)
    if pairing is None:
        return "none"
    session = pairing.session
    now = _clock()
    if not accept:
        _finish(pairing, "denied")
        _pair_error(session, "denied", "denied", pairing.username)
        return "denied"
    if now - pairing.awaiting_since > CONFIRM_SECONDS or now > pairing.exp or session is None \
            or session.closed:
        _finish(pairing, "expired")
        if session is not None and not session.closed:
            _pair_error(session, "expired", "confirmation too late", pairing.username)
        return "expired"
    device_id = str(uuid.uuid4())
    der = pki.issue_device_cert(pairing.public_key, device_id)
    serial = str(x509.load_der_x509_certificate(der).serial_number)
    devices.add_device(user_scope_id=scope, username=pairing.username,
                       spki_sha256=pki.spki_sha256(pairing.public_key), cert_serial=serial,
                       model=pairing.device.get("model", ""),
                       platform=pairing.device.get("platform", ""),
                       app_version=pairing.device.get("app_version", ""), device_id=device_id,
                       transport=session.transport, relay_member=pairing.member,
                       relay_cid=pairing.cid)
    _switch_lane_on(scope)
    pairing.device_id = device_id
    _finish(pairing, "paired")
    way = "relay" if session.transport == "relay" else "direct"
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("channel_paired", channel=CHANNEL, username=pairing.username,
                           path=device_id,
                           detail=f"{pairing.device.get('model', '')}, "
                                  f"{pairing.device.get('platform', '')}, {way}, "
                                  f"{pairing.device.get('fingerprint', '')}")
    except Exception:
        pass
    from vaf.core.app_link import agent_name
    agent = agent_name(pairing.username)
    import base64
    session.send_json({"t": "paired", "cert": base64.b64encode(der).decode("ascii"),
                       "chain": [base64.b64encode(pki.device_ca_der()).decode("ascii")],
                       "device_id": device_id, "account": pairing.username,
                       "agent": agent or "VAF"})
    session.close("paired", bye=True)
    return "paired"


def current(user_scope_id: str) -> Dict[str, Any]:
    """What the setup window shows: the state of the account's pairing and, while it waits
    for confirmation, the device as the app described it (model, never a name the person
    chose) with its fingerprint."""
    scope = str(user_scope_id or "").strip()
    now = _clock()
    with _lock:
        pairing = _awaiting.get(scope) or _issued.get(scope) or _last.get(scope)
    if pairing is None:
        return {"state": "none"}
    if pairing.state in ("issued", "claimed") and pairing.exp <= now:
        return {"state": "expired"}
    out: Dict[str, Any] = {"state": pairing.state, "mode": pairing.mode,
                           "expires_at": int(pairing.exp)}
    if pairing.state == "awaiting_confirm":
        out["device"] = {k: pairing.device.get(k, "") for k in
                         ("model", "platform", "app_version", "fingerprint")}
    if pairing.relay_error:
        out["relay_error"] = pairing.relay_error
    if pairing.state == "paired":
        out["device_id"] = pairing.device_id
    return out


def sweep() -> None:
    """A confirmation not given within 120 seconds ends the pairing as expired."""
    now = _clock()
    with _lock:
        late = [p for p in _awaiting.values() if now - p.awaiting_since > CONFIRM_SECONDS]
        for scope in [s for s, until in _windows.items() if until <= now]:
            del _windows[scope]
        for scope in [s for s, p in _issued.items() if p.exp <= now]:
            _last[scope] = _issued.pop(scope)
            _last[scope].state = "expired"
    for pairing in late:
        _finish(pairing, "expired")
        if pairing.session is not None and not pairing.session.closed:
            _pair_error(pairing.session, "expired", "confirmation too late", pairing.username)


def reset() -> None:
    """Forget every window and pairing (tests)."""
    with _lock:
        _windows.clear()
        _issued.clear()
        _awaiting.clear()
        _last.clear()
