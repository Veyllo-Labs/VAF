# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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


def effective_client_ip(peer_ip: str | None, forwarded_for: str | None) -> str:
    """Resolve who the client REALLY is, accounting for the integrated HTTPS proxy.

    Why this exists: the proxy terminates TLS on 0.0.0.0 and relays every LAN device to the
    backend over loopback, so the raw socket peer is 127.0.0.1 for remote users too. Trusting
    the peer alone therefore treats the whole LAN as local.

    The polarity is deliberately fail-safe: a forwarding hop REMOVES trust, it never grants it.
    ``X-Forwarded-For`` is honoured only when the immediate peer is itself loopback (i.e. the
    request was relayed by our own proxy, which strips any client-supplied copy before setting
    its own - see vaf/network/https_proxy.py). A direct non-loopback peer keeps its socket
    address no matter what it claims, so a client can only make itself look MORE remote,
    never more local.

    Callers with no forwarding header (internal loopback IPC, the desktop via the Next.js
    /api route) resolve to the peer unchanged.
    """
    peer = (peer_ip or "").strip() or "unknown"
    if not is_localhost(peer):
        return peer
    first_hop = ((forwarded_for or "").split(",")[0] or "").strip()
    return first_hop or peer


def assert_safe_remote_host(host: str, *, allow_private: bool = False) -> None:
    """SSRF guard for user-supplied OUTBOUND targets (e.g. an IMAP/SMTP server a user types
    into the email wizard). Resolves the host and raises ValueError if ANY resolved address is
    not globally routable.

    - Multicast / reserved / unspecified / link-local (incl. the 169.254.169.254 cloud-metadata
      endpoint) are NEVER allowed, even with the override.
    - Loopback / RFC-1918 private addresses are allowed only when allow_private=True (so a user
      who genuinely runs a LAN / self-hosted mail server can opt in via email_allow_private_hosts).

    Note: there is an inherent resolve-vs-connect TOCTOU (DNS rebinding); for a mostly-static
    mail-server config the residual risk is low and accepted.
    """
    h = (host or "").strip()
    if not h:
        raise ValueError("No host given")
    try:
        infos = socket.getaddrinfo(h, None)
    except socket.gaierror as e:
        raise ValueError(f"Cannot resolve host: {h}") from e
    addrs = {info[4][0] for info in infos}
    if not addrs:
        raise ValueError(f"Cannot resolve host: {h}")
    for ip in addrs:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError as e:
            raise ValueError(f"Invalid resolved address for host: {h}") from e
        if addr.is_global:
            continue
        if addr.is_multicast or addr.is_reserved or addr.is_unspecified or addr.is_link_local:
            raise ValueError(f"Refusing to connect to non-routable address ({ip}) for host {h}")
        if addr.is_loopback or addr.is_private:
            if allow_private:
                continue
            raise ValueError(
                f"Refusing to connect to private address ({ip}) for host {h}. "
                "Set email_allow_private_hosts=true to allow a LAN / self-hosted server."
            )
        raise ValueError(f"Refusing to connect to non-public address ({ip}) for host {h}")


def assert_ip_safe(ip: str, *, allow_private: bool = False) -> None:
    """SSRF guard on an ALREADY-RESOLVED address. Same policy as
    assert_safe_remote_host, but the caller resolves the host once and then pins
    the connection to this exact IP - closing the resolve-vs-connect (DNS
    rebinding) TOCTOU that assert_safe_remote_host cannot. Raises ValueError if
    the address is not a safe outbound target."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError as e:
        raise ValueError(f"Invalid address: {ip}") from e
    if addr.is_global:
        return
    if addr.is_multicast or addr.is_reserved or addr.is_unspecified or addr.is_link_local:
        raise ValueError(f"Refusing to connect to non-routable address ({ip})")
    if addr.is_loopback or addr.is_private:
        if allow_private:
            return
        raise ValueError(f"Refusing to connect to private address ({ip})")
    raise ValueError(f"Refusing to connect to non-public address ({ip})")


def resolve_pinned_target(host: str, port: int, *, allow_private: bool = False) -> str:
    """Resolve `host` ONCE and validate EVERY resolved address, returning a single
    pinned IP the caller must connect to. Pinning the socket to this exact IP (and
    validating the TLS cert against the original hostname) closes the
    resolve-vs-connect (DNS rebinding) TOCTOU that assert_safe_remote_host cannot:
    there is no second lookup for an attacker to swap. Raises ValueError if ANY
    resolved address is unsafe; propagates socket.gaierror if `host` does not
    resolve (the caller distinguishes 'blocked' from 'unresolvable')."""
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    ips = [info[4][0] for info in infos]
    if not ips:
        raise ValueError(f"Cannot resolve host: {host}")
    for ip in dict.fromkeys(ips):
        assert_ip_safe(ip, allow_private=allow_private)
    return ips[0]


def system_proxy_for(scheme: str, host: str) -> Optional[str]:
    """The site egress proxy to use for `scheme://host`, or None for a direct
    connect.

    Managed networks (the case this exists for) forbid direct outbound traffic and
    publish a proxy through the conventional environment variables. VAF used to
    ignore them and connect directly, which in such a network means the request
    simply fails - and, worse, means the operator cannot see or filter what the
    mail renderer fetches from the internet.

    Deliberately narrow: for http targets only the lowercase `http_proxy` is read,
    because CGI-style servers map an inbound `Proxy:` request header into
    `HTTP_PROXY` and every HTTP client treats the uppercase form as untrusted for
    that reason. NO_PROXY is matched by exact host, dot-suffix or `*`, and a proxy
    value that is not http(s) is ignored rather than half-applied.

    PLATFORM CAVEAT, because a guarantee that only holds on one OS has to say so:
    Windows environment variables are case-insensitive, and Python mirrors that -
    `os.environ.get("http_proxy")` returns whatever `HTTP_PROXY` holds. The
    lowercase-only rule is therefore a POSIX-only protection; on Windows the two
    names are one variable and there is nothing to distinguish. That is acceptable
    here: the CGI vector requires a CGI server mapping request headers into the
    environment, which is not how VAF runs on any platform.

    Reading the environment on every call is intentional: no import-time snapshot
    to go stale, and a test can monkeypatch os.environ.
    """
    import os

    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return None

    no_proxy = (os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or "").strip()
    for entry in (e.strip().lower().lstrip(".") for e in no_proxy.split(",")):
        if not entry:
            continue
        if entry in ("*", h) or h.endswith("." + entry):
            return None

    if (scheme or "").lower() == "https":
        raw = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    else:
        # HTTP_PROXY from the environment is attacker-influenced in CGI-style
        # deployments; the lowercase form is the one every client trusts.
        raw = os.environ.get("http_proxy")
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.lower().startswith(("http://", "https://")):
        logger.warning("Ignoring unsupported proxy scheme in environment: %r", raw[:24])
        return None
    return raw


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


def resolve_lan_access_ports(wait_for_proxy: bool = False, timeout_s: float = 10.0) -> Tuple[int, int]:
    """Return (access_port, frontend_port) that LAN clients actually reach.

    TLS on: the access port is the integrated HTTPS proxy's EFFECTIVE port, which
    can differ from the configured one because of the 443->8443 fallback in
    pick_bindable_port. With wait_for_proxy=True the proxy status is polled up to
    timeout_s for the port it really bound - valid only for callers INSIDE the app
    process, because runtime_status is per-process state; out-of-process callers
    (CLI, installer) must leave it False and get the deterministic assumption:
    configured local_network_https_port, with 443 mapped to 8443. The frontend
    port is the plain backend port in this mode - with TLS the proxy is the only
    LAN-facing listener and the backend port is the secondary one the firewall
    layer handles.

    TLS off: (local_network_port, local_network_port_frontend).
    """
    from vaf.core.config import Config

    tls_on = bool(Config.get("local_network_tls_enabled", False))
    if not tls_on:
        return (
            int(Config.get("local_network_port", 8001) or 8001),
            int(Config.get("local_network_port_frontend", 3000) or 3000),
        )

    access_port: Optional[int] = None
    if wait_for_proxy:
        import time
        from vaf.network import runtime_status
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while time.monotonic() < deadline:
            st = runtime_status.get_proxy_status()
            if st.get("bound") and st.get("effective_https_port"):
                access_port = int(st["effective_https_port"])
                break
            time.sleep(0.5)
    if access_port is None:
        configured = int(Config.get("local_network_https_port", 443) or 443)
        access_port = 8443 if configured == 443 else configured
    return access_port, int(Config.get("local_network_port", 8001) or 8001)


# Lease stores of the network managers VAF's supported distros actually ship:
# NetworkManager (internal client), dhclient, dhcpcd, wicked (openSUSE) and
# systemd-networkd. Every file in these stores names the leased address in
# plain text, which is all the probe needs.
_DHCP_LEASE_GLOBS = (
    "/var/lib/NetworkManager/*.lease*",
    "/var/lib/dhcp/dhclient*.lease*",
    "/var/lib/dhclient/*.lease*",
    "/var/lib/dhcpcd/*",
    "/run/wicked/leaseinfo*",
    "/run/systemd/netif/leases/*",
)


def lan_ip_is_dhcp() -> Optional[bool]:
    """Best-effort answer to "is the LAN address DHCP-assigned?".

    True = a DHCP lease covers the LAN IP, False = the address is configured
    manually, None = undetectable. Warn-only by contract: callers use this purely
    to recommend a static IP or router reservation for server installs, so every
    probe is wrapped, subprocess calls carry short timeouts, and the function
    never raises. An answer of None must stay silent at the call site - lease
    stores can be unreadable for an unprivileged user, and that proves nothing.
    """
    import glob
    import os
    import shutil
    import subprocess

    try:
        lan_ip = get_local_network_ip()
    except Exception:
        return None

    # Probe 1: NetworkManager, when it manages the device. A DHCP-assigned
    # address always carries DHCP4.OPTION entries in `nmcli device show`; a
    # manual address on the same device has none.
    try:
        if shutil.which("nmcli"):
            result = subprocess.run(
                ["nmcli", "-t", "device", "show"],
                capture_output=True, text=True, timeout=5,
                # Extend, never replace: a bare env would drop the keys a
                # subprocess needs on other platforms (SystemRoot on Windows).
                env={**os.environ, "LC_ALL": "C"},
            )
            if result.returncode == 0 and result.stdout:
                for block in result.stdout.split("\n\n"):
                    if f":{lan_ip}/" in block or f":{lan_ip}\n" in block:
                        return "DHCP4.OPTION" in block
    except Exception:
        pass

    # Probe 2: lease files of the other common clients.
    for pattern in _DHCP_LEASE_GLOBS:
        try:
            for path in glob.glob(pattern):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        if lan_ip in fh.read(262144):
                            return True
                except OSError:
                    continue
        except Exception:
            continue

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
