"""
Network API: access URL (LAN IP + port) for local network hosting.

Endpoints:
- GET /api/network/access-url  → { "host", "port", "url" } for other devices
- GET /api/network/ws-config → { "useWss", "port" } for WebSocket URL (TLS vs plain)
"""

import logging
from fastapi import APIRouter

from vaf.core.config import Config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/network", tags=["network"])


@router.get("/ws-config")
def get_ws_config():
    """
    Return WebSocket connection config so the frontend can use wss:// when backend has TLS.
    When TLS is on, clients reach us via the integrated HTTPS proxy (443 or 8443 on Windows),
    so return that port so the frontend builds wss://host:proxy_port/ws (proxied), not wss://host:8001.
    Callable without auth so the client can build the WebSocket URL before or after login.
    """
    tls = Config.get("local_network_tls_enabled", False)
    # When TLS on, client uses the HTTPS proxy port (same origin); else backend port
    if tls:
        import platform
        p = Config.get("local_network_https_port", 443)
        if platform.system() == "Windows" and p == 443:
            p = 8443  # Windows often uses 8443 when 443 needs admin
        port = p
    else:
        port = Config.get("local_network_port", 8001)
    return {"useWss": tls, "port": port}


@router.get("/access-url")
def get_access_url():
    """
    Return the URL other devices should use (integrated HTTPS proxy).
    Port matches ws-config (443 or 8443 on Windows when TLS on).
    """
    try:
        from vaf.network.binding import get_local_network_ip
        host = get_local_network_ip()
    except Exception as e:
        logger.debug(f"Could not get LAN IP: {e}")
        host = None
    tls = Config.get("local_network_tls_enabled", False)
    port = 443
    if tls:
        import platform
        port = Config.get("local_network_https_port", 443)
        if platform.system() == "Windows" and port == 443:
            port = 8443
    if not host:
        return {"host": None, "port": port, "url": None}
    url = f"https://{host}" if port == 443 else f"https://{host}:{port}"
    return {"host": host, "port": port, "url": url}


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
