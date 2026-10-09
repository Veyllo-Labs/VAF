# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Network API: access URL (LAN IP + port) for local network hosting.

Endpoints:
- GET /api/network/access-url  → { "host", "port", "url" } for other devices (admin)
- GET /api/network/status → the proxy's real binding (admin)
- GET /api/network/connections → the live connection map (admin)
- GET/PUT /api/network/remote-access → VPN interfaces and who is admitted (admin)
- GET /api/network/ws-config → { "useWss", "port" } for WebSocket URL (TLS vs plain); open,
  the login page needs it before anybody is signed in

Everything but /ws-config answers an admin only: the connection map lists every connected
device's address and user name, and the rest describes the machine's network. The
settings tab that reads them was admin-only; the routes were not (any signed-in account
could read the map).
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from vaf.api.user_routes import require_admin
from vaf.core.config import Config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/network", tags=["network"])

# Always-on internal plain-HTTP channel (started alongside TLS in vaf/tray.py start_uvicorn). The Next.js
# /api proxy and the local desktop reach the backend through it to avoid the self-signed TLS cert.
_INTERNAL_API_PORT = 8005


def _access_port() -> int:
    """The HTTPS access port to advertise: the port the integrated proxy ACTUALLY bound (e.g. 8443 after
    a 443 fallback), or the configured port before it has bound. Single source of truth so the UI never
    shows a port nothing is listening on."""
    configured = Config.get("local_network_https_port", 443)
    try:
        from vaf.network import runtime_status
        return runtime_status.effective_https_port(default=configured)
    except Exception:
        return configured


@router.get("/ws-config")
def get_ws_config(request: Request):
    """Return the WebSocket transport the CALLER should use — and it differs for LAN vs the local desktop.

    LAN clients reach us through the integrated HTTPS proxy, which stamps `X-Forwarded-Proto: https`; they
    get a wss:// URL on the effective proxy port (e.g. 8443 after the 443 fallback) — same secure origin.

    The local DESKTOP window loads the frontend on plain http://127.0.0.1:3000, and its /api calls hit the
    internal 8005 channel WITHOUT that header. It must get a PLAIN ws:// URL to a local port, never wss://:
    the proxy serves a self-signed cert that QtWebEngine rejects (ERR_CERT_AUTHORITY_INVALID), which kills
    the socket and leaves the desktop UI unable to connect. The always-on internal 8005 channel is that
    plain local endpoint (same intent as the Next.js /api → 8005 proxy, see web/lib/utils.ts).

    Callable without auth so the client can build the URL before or after login.
    """
    tls = Config.get("local_network_tls_enabled", False)
    if not tls:
        # No TLS → the backend itself is plain HTTP; only localhost can reach it anyway.
        return {"useWss": False, "port": Config.get("local_network_port", 8001)}
    came_via_proxy = request.headers.get("x-forwarded-proto", "").lower() == "https"
    if came_via_proxy:
        # Remote/LAN client behind the HTTPS proxy → secure same-origin wss on the effective proxy port.
        return {"useWss": True, "port": _access_port()}
    # Local desktop (plain http on :3000) → plain internal channel, no self-signed cert to trust.
    return {"useWss": False, "port": _INTERNAL_API_PORT}


def _primary_host() -> Optional[str]:
    """The address other devices should use: the first admitted LAN address, else the first
    admitted VPN address (a server reached only over a VPN has no LAN)."""
    try:
        from vaf.network.binding import access_addresses
        reachable = access_addresses()
        return reachable[0].ip if reachable else None
    except Exception as e:
        logger.debug("Could not list the access addresses: %s", e)
        return None


@router.get("/access-url", dependencies=[Depends(require_admin)])
def get_access_url():
    """
    Return host and ports for display; full url (with port) for copy.
    Network mode is always with encryption. The access port is the port the proxy actually bound.
    """
    host = _primary_host()
    access_port = _access_port()
    backend_port = Config.get("local_network_port", 8001)
    if not host:
        return {
            "host": None,
            "port": access_port,
            "backend_port": backend_port,
            "ports": {"access": access_port, "backend": backend_port},
            "url": None,
        }
    url = f"https://{host}" if access_port == 443 else f"https://{host}:{access_port}"
    return {
        "host": host,
        "port": access_port,
        "backend_port": backend_port,
        "ports": {"access": access_port, "backend": backend_port},
        "url": url,
    }


@router.get("/status", dependencies=[Depends(require_admin)])
def get_network_status():
    """Real runtime status of LAN hosting: whether the integrated HTTPS proxy actually bound, on which
    port (after any privileged-port fallback), the resulting LAN URL, and the last bind error if it
    failed. The UI uses this to show the truth (e.g. "running on https://<ip>:8443" or an error +
    firewall/cert hint) instead of a value merely computed from config."""
    from vaf.network import runtime_status
    st = runtime_status.get_proxy_status()
    tls = Config.get("local_network_tls_enabled", False)
    enabled = Config.get("local_network_enabled", False)
    host = _primary_host()
    eff = st.get("effective_https_port")
    url = None
    if host and eff:
        url = f"https://{host}" if eff == 443 else f"https://{host}:{eff}"
    return {
        "enabled": enabled,
        "tls": tls,
        "host": host,
        "configured_https_port": st.get("configured_https_port") or Config.get("local_network_https_port", 443),
        "effective_https_port": eff,
        "proxy_bound": bool(st.get("bound")),
        "error": st.get("error"),
        "url": url,
    }


@router.get("/connections", dependencies=[Depends(require_admin)])
def get_connections():
    """
    Return list of active network connections (Topology).
    """
    try:
        from vaf.network.connection_tracker import get_tracker
        tracker = get_tracker()
        return tracker.get_active_connections()
    except ImportError:
        return []
    except Exception as e:
        logger.error(f"Failed to get connections: {e}")
        return []


# ── remote access: VPN interfaces and who is admitted ──────────────────────
#
# The web half of the "Remote access (VPN)" settings; `vaf server networks` and
# `vaf server vpn-only` are the CLI half. Both write the same two keys through the same
# check (binding.normalize_allowed_networks). The access check reads them on every
# request; the tray sees the save and re-applies the OS firewall, without a restart.

_MESH_VPN_NETWORK = "100.64.0.0/10"


def _remote_access_state(request: Request) -> dict:
    from vaf.network.binding import (connection_client_ip, inbound_policy, is_allowed_ip,
                                     local_interfaces)
    from vaf.network.ssl_utils import certificate_ip_addresses

    policy = inbound_policy(detect_vpn=True)
    port = _access_port()
    suffix = "" if port == 443 else f":{port}"
    in_cert = certificate_ip_addresses()
    interfaces = []
    for iface in local_interfaces():
        admitted = is_allowed_ip(iface.ip)
        interfaces.append({
            "name": iface.name,
            "ip": iface.ip,
            "network": iface.network,
            "kind": iface.kind,
            "admitted": admitted,
            "url": f"https://{iface.ip}{suffix}" if admitted else None,
            "in_certificate": None if in_cert is None else iface.ip in in_cert,
            # A VPN address with no network around it (a WireGuard set up with /32)
            # names none of its peers; its subnet has to be listed by hand.
            "single_address": iface.network.endswith("/32"),
        })
    return {
        "enabled": Config.get_bool("local_network_enabled", False),
        "access_port": port,
        "interfaces": interfaces,
        "allowed": list(policy.allowed),
        "refused": [{"value": v, "reason": r} for v, r in policy.refused],
        "vpn_only": policy.vpn_only,
        "vpn_networks": list(policy.vpn),
        "mesh_vpn_network": _MESH_VPN_NETWORK,
        "mesh_vpn_admitted": any(a == _MESH_VPN_NETWORK for a in policy.allowed),
        "your_address": connection_client_ip(request),
    }


@router.get("/remote-access", dependencies=[Depends(require_admin)])
def get_remote_access(request: Request):
    """The VPN interfaces with their access URL, and who is admitted."""
    return _remote_access_state(request)


class RemoteAccessUpdate(BaseModel):
    allowed: List[str]
    vpn_only: bool
    # Set after the person saw the lockout warning: they keep the change although it
    # shuts out the address they are connected from.
    confirm: bool = False
    # The state this change was made on, as the page last loaded it. The body replaces the
    # whole list, so a page opened before another admin removed a network would bring that
    # network back; with these the save is refused when the stored state moved on.
    base_allowed: Optional[List[str]] = None
    base_vpn_only: Optional[bool] = None


@router.put("/remote-access", dependencies=[Depends(require_admin)])
def put_remote_access(body: RemoteAccessUpdate, request: Request):
    """Replace the admitted networks and the "VPN only" switch.

    422 with the refused entries when one is not a private network. 409 "stale" when
    `base_allowed`/`base_vpn_only` are sent and the stored settings no longer match them
    (another admin, or the CLI, changed them since the page loaded): the answer carries
    the current state to redo the change on. 409 "lockout" when the change would shut out
    the address this request comes from, unless `confirm` is set: the person would lose
    this very page, so they are asked once before it happens.

    Checked and written under the config lock against the configuration loaded inside
    it, so a setting another process changed meanwhile keeps its new value. One load and
    one save for both keys - what Config.set does per key - so the observers see one
    change and the firewall is re-applied once, not twice.
    """
    from vaf.network.binding import (connection_client_ip, inbound_policy,
                                     normalize_allowed_networks)
    import ipaddress

    allowed, refused = normalize_allowed_networks(body.allowed)
    if refused:
        raise HTTPException(status_code=422, detail={
            "code": "refused", "refused": [{"value": v, "reason": r} for v, r in refused]})

    with Config._locked():
        cfg = Config.load()
        if body.base_allowed is not None or body.base_vpn_only is not None:
            stored = inbound_policy(cfg)
            base_allowed, _ = normalize_allowed_networks(body.base_allowed or [])
            moved = ((body.base_allowed is not None and base_allowed != list(stored.allowed))
                     or (body.base_vpn_only is not None and bool(body.base_vpn_only) != stored.vpn_only))
            if moved:
                raise HTTPException(status_code=409, detail={
                    "code": "stale", "state": _remote_access_state(request)})
        proposed = {**cfg, "local_network_allowed_networks": allowed,
                    "local_network_vpn_only": bool(body.vpn_only)}
        caller = connection_client_ip(request)
        kept = caller in ("localhost", "::1")
        if not kept:
            try:
                addr = ipaddress.ip_address(caller)
                kept = any(addr in net for net in inbound_policy(proposed).networks)
            except ValueError:
                kept = False
        if not kept and not body.confirm:
            raise HTTPException(status_code=409, detail={"code": "lockout", "address": caller})
        Config.save(proposed)
    return _remote_access_state(request)
