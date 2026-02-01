"""
Network API: access URL (LAN IP + port) for local network hosting.

Endpoints:
- GET /api/network/access-url  → { "host", "port", "url" } for other devices
"""

import logging
from fastapi import APIRouter

from vaf.core.config import Config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/network", tags=["network"])


@router.get("/access-url")
def get_access_url():
    """
    Return the URL other devices on the network should use to reach VAF.
    Uses the machine's LAN IP (e.g. 192.168.1.100) and the frontend port.
    """
    port = Config.get("local_network_port_frontend", 3000)
    try:
        from vaf.network.binding import get_local_network_ip
        host = get_local_network_ip()
    except Exception as e:
        logger.debug(f"Could not get LAN IP: {e}")
        host = None
    if not host:
        return {"host": None, "port": port, "url": None}
    scheme = "https" if port == 443 else "http"
    url = f"{scheme}://{host}:{port}" if port not in (80, 443) else f"{scheme}://{host}"
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
