"""
Network API: access URL (LAN IP + port) for local network hosting.

Endpoints:
- GET /api/network/access-url  → { "host", "port", "url" } for other devices
- GET /api/network/ws-config → { "useWss", "port" } for WebSocket URL (TLS vs plain)
"""

import logging
from fastapi import APIRouter, Request

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


@router.get("/access-url")
def get_access_url():
    """
    Return host and ports for display; full url (with port) for copy.
    Network mode is always with encryption. The access port is the port the proxy actually bound.
    """
    try:
        from vaf.network.binding import get_local_network_ip
        host = get_local_network_ip()
    except Exception as e:
        logger.debug(f"Could not get LAN IP: {e}")
        host = None
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


@router.get("/status")
def get_network_status():
    """Real runtime status of LAN hosting: whether the integrated HTTPS proxy actually bound, on which
    port (after any privileged-port fallback), the resulting LAN URL, and the last bind error if it
    failed. The UI uses this to show the truth (e.g. "running on https://<ip>:8443" or an error +
    firewall/cert hint) instead of a value merely computed from config."""
    from vaf.network import runtime_status
    st = runtime_status.get_proxy_status()
    tls = Config.get("local_network_tls_enabled", False)
    enabled = Config.get("local_network_enabled", False)
    try:
        from vaf.network.binding import get_local_network_ip
        host = get_local_network_ip()
    except Exception:
        host = None
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


@router.get("/connections")
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
