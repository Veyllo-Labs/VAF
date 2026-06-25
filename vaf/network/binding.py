"""
VAF Network Binding - Local Network IP Detection

Detects local network interfaces and provides IP validation utilities.
Used to bind the server to a specific local network interface instead of 0.0.0.0

SECURITY: This is Layer 1 of the three-layer defense against internet exposure.
"""

import socket
import ipaddress
import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# RFC 1918 Private IP Ranges
PRIVATE_RANGES = [
    ipaddress.ip_network('10.0.0.0/8'),        # Class A Private
    ipaddress.ip_network('172.16.0.0/12'),     # Class B Private  
    ipaddress.ip_network('192.168.0.0/16'),    # Class C Private
]

# Localhost ranges (always allowed)
LOCALHOST_RANGES = [
    ipaddress.ip_network('127.0.0.0/8'),       # IPv4 Localhost
]

# All allowed ranges for validation
ALLOWED_RANGES = LOCALHOST_RANGES + PRIVATE_RANGES


def is_private_ip(ip: str) -> bool:
    """
    Check if an IP address is in RFC 1918 private range.
    
    Args:
        ip: IP address string (e.g., "192.168.1.100")
        
    Returns:
        True if IP is private (192.168.x.x, 10.x.x.x, 172.16-31.x.x)
    """
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in PRIVATE_RANGES)
    except ValueError:
        return False


def is_localhost(ip: str) -> bool:
    """
    Check if an IP address is localhost.
    
    Args:
        ip: IP address string
        
    Returns:
        True if IP is localhost (127.x.x.x, ::1)
    """
    try:
        if ip in ('localhost', '::1'):
            return True
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in LOCALHOST_RANGES)
    except ValueError:
        return False


def is_allowed_ip(ip: str) -> bool:
    """
    Check if an IP address is allowed (localhost or private).
    
    This is the main validation function used by the middleware.
    
    Args:
        ip: IP address string
        
    Returns:
        True if IP is allowed for local network access
    """
    try:
        if ip in ('localhost', '::1'):
            return True
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in ALLOWED_RANGES)
    except ValueError:
        return False


def pick_bindable_port(host: str, preferred: int, fallback: int = 8443) -> Optional[int]:
    """Return the first port from [preferred, fallback] that `host` can ACTUALLY bind, or None if
    neither is bindable. A privileged port (<1024, e.g. 443) raises PermissionError for a non-root
    desktop user, so VAF transparently falls back to a non-privileged high port instead of failing
    silently (the previous code only did this on Windows). The probe socket is closed immediately;
    the caller (uvicorn) then binds the chosen port — SO_REUSEADDR makes the brief gap harmless."""
    for port in dict.fromkeys(p for p in (preferred, fallback) if p):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, int(port)))
            return int(port)
        except OSError as e:
            logger.info("Port %s not bindable on %s (%s); trying next", port, host, e)
        finally:
            try:
                s.close()
            except Exception:
                pass
    return None


def get_all_local_ips() -> List[Tuple[str, str]]:
    """
    Get all local network IP addresses.
    
    Returns:
        List of (interface_name, ip_address) tuples for all private IPs
    """
    local_ips = []
    
    try:
        # Method 1: Try netifaces if available (most reliable)
        try:
            import netifaces
            for iface in netifaces.interfaces():
                addrs = netifaces.ifaddresses(iface).get(netifaces.AF_INET, [])
                for addr in addrs:
                    ip = addr.get('addr', '')
                    if ip and is_private_ip(ip):
                        local_ips.append((iface, ip))
            if local_ips:
                return local_ips
        except ImportError:
            logger.debug("netifaces not available, using fallback method")
        
        # Method 2: Use socket to find IPs (fallback)
        # This connects to an external address but doesn't send any data
        hostname = socket.gethostname()
        
        # Try to get all addresses for the hostname
        try:
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                ip = info[4][0]
                if is_private_ip(ip):
                    local_ips.append(('unknown', ip))
        except socket.gaierror:
            pass
        
        # Method 3: Connect trick to find the default route IP
        if not local_ips:
            try:
                # This doesn't actually send any packets
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(0.1)
                # Use a public IP - no actual connection is made
                s.connect(('8.8.8.8', 80))
                ip = s.getsockname()[0]
                s.close()
                if is_private_ip(ip):
                    local_ips.append(('default', ip))
            except Exception:
                pass
        
        return local_ips
        
    except Exception as e:
        logger.error(f"Failed to get local IPs: {e}")
        return []


def get_local_network_ip() -> str:
    """
    Detect the primary local network IP address.
    
    This is the IP that should be used for binding when local_network_enabled=True.
    
    Returns:
        Local network IP address (e.g., "192.168.1.100")
        
    Raises:
        RuntimeError: If no local network interface is found
    """
    local_ips = get_all_local_ips()
    
    if not local_ips:
        raise RuntimeError(
            "No local network interface found. "
            "Please ensure you are connected to a local network (WiFi or Ethernet)."
        )
    
    # Prefer certain interface patterns
    preference_order = ['eth', 'en', 'wlan', 'wifi', 'lan']
    
    # Sort by preference
    def sort_key(item):
        iface, ip = item
        iface_lower = iface.lower()
        for i, pref in enumerate(preference_order):
            if pref in iface_lower:
                return (i, iface)
        return (len(preference_order), iface)
    
    sorted_ips = sorted(local_ips, key=sort_key)
    
    selected_ip = sorted_ips[0][1]
    logger.info(f"Selected local network IP: {selected_ip}")
    
    return selected_ip


def get_local_network_info() -> dict:
    """
    Get comprehensive local network information.
    
    Returns:
        Dict with network info for display in UI
    """
    import platform as plt
    
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "unknown"
    
    local_ips = get_all_local_ips()
    
    try:
        primary_ip = get_local_network_ip()
    except RuntimeError:
        primary_ip = None
    
    return {
        "hostname": hostname,
        "platform": plt.system(),
        "primary_ip": primary_ip,
        "all_interfaces": [
            {"interface": iface, "ip": ip}
            for iface, ip in local_ips
        ]
    }
