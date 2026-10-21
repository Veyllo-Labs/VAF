# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Network Binding - who may reach this machine, and on which addresses

Detects the machine's LAN and VPN interfaces and decides which client addresses are
admitted in network mode. The decision has ONE source, `inbound_policy()`: the access
check (`is_allowed_ip`), every OS firewall backend (`firewall_sources`) and the shown
access addresses (`access_addresses`) all read it, so they cannot disagree.

VAF is served to private networks only, never to the internet: the layers that hold it are
the Security Model of docs/setup/NETWORK_FEATURES.md.
"""

import socket
import ipaddress
import itertools
import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)

# RFC 1918: the local networks VAF admits by default. A home or office LAN, and the
# subnets WireGuard and OpenVPN usually hand out, sit in them.
PRIVATE_RANGES = [
    ipaddress.ip_network('10.0.0.0/8'),        # Class A Private
    ipaddress.ip_network('172.16.0.0/12'),     # Class B Private
    ipaddress.ip_network('192.168.0.0/16'),    # Class C Private
]

# Localhost ranges (always allowed)
LOCALHOST_RANGES = [
    ipaddress.ip_network('127.0.0.0/8'),       # IPv4 Localhost
]

# Shared address space (carrier-grade NAT). Mesh VPNs hand out addresses from it:
# Tailscale and Headscale per device, NetBird as one /10. Internet providers use it
# behind their NAT too, so it is admitted only when an admin says so.
CGNAT_RANGE = ipaddress.ip_network('100.64.0.0/10')

# What an admin may add to the admitted networks: anything inside these. Deliberately
# not 198.18.0.0/15, which the outbound classifier below counts as private because
# fake-IP proxies resolve names into it (no other device sits there), and nothing
# public: a public network is never admitted, because VAF is served to private
# networks only.
_ADMISSIBLE_RANGES = (*PRIVATE_RANGES, CGNAT_RANGE)


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
        # A dual-stack listener reports an IPv4 client as ::ffff:a.b.c.d; the desktop's
        # own 127.0.0.1 must not turn remote because of how the socket was opened.
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        return any(addr in net for net in LOCALHOST_RANGES)
    except ValueError:
        return False


def is_allowed_ip(ip: str) -> bool:
    """Whether a client at `ip` may reach VAF in network mode.

    The main validation function: the IP check middleware and the WebSocket handshake
    ask it. The admitted networks come from `inbound_policy()`, so an admin's VPN
    settings take effect on the next request, without a restart. When the settings
    cannot be read the answer falls back to the long-standing default (this machine
    and the RFC 1918 networks), never to anything wider.
    """
    if ip in ('localhost', '::1'):
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    try:
        networks = inbound_policy().networks
    except Exception as e:
        logger.warning("inbound policy unreadable, using the local networks only: %s", e)
        networks = (*LOCALHOST_RANGES, *PRIVATE_RANGES)
    return any(addr in net for net in networks)


# ---------------------------------------------------------------------------
# This machine's interfaces, and which networks are admitted
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LocalInterface:
    """One address of this machine that another device could reach it on."""
    name: str        # the interface name the OS reports ("enp3s0", "wg0", "Tailscale")
    ip: str
    network: str     # the network its devices come from, as CIDR
    kind: str        # "lan" or "vpn"


# VPN interfaces are told apart by name. Prefixes for the Unix names (wg0, wt0 is
# NetBird, tun0/tap0 OpenVPN, utun3 every VPN on macOS, zt* ZeroTier), words for the
# names Windows shows ("Tailscale", "OpenVPN Wintun"). Named boundary: Windows names a
# WireGuard adapter after its tunnel file ("home"), which reads as a LAN; outside
# "VPN only" that changes nothing (its subnet is private anyway), and in "VPN only"
# its network is added by hand.
_VPN_NAME_PREFIXES = ("wg", "wt", "tun", "tap", "utun", "zt", "ppp", "ipsec", "nordlynx",
                      "tailscale")
_VPN_NAME_WORDS = ("tailscale", "wireguard", "openvpn", "wintun", "tap-windows", "zerotier",
                   "netbird")
# Bridges of containers and virtual machines on this host: nobody connects from them.
_SKIPPED_NAME_PREFIXES = ("docker", "br-", "veth", "virbr", "cni", "flannel", "podman", "lxc",
                          "lxd", "vmnet", "vboxnet", "bridge", "kube", "cali", "weave")
_SKIPPED_NAME_WORDS = ("(wsl", "default switch")


def _interface_kind(name: str) -> Optional[str]:
    """"vpn", "lan", or None for an interface nobody connects from."""
    lowered = (name or "").strip().lower()
    if not lowered or lowered == "lo" or lowered.startswith("loopback"):
        return None
    if lowered.startswith(_SKIPPED_NAME_PREFIXES) or any(w in lowered for w in _SKIPPED_NAME_WORDS):
        return None
    if lowered.startswith(_VPN_NAME_PREFIXES) or any(w in lowered for w in _VPN_NAME_WORDS):
        return "vpn"
    return "lan"


def _interface_network(addr: ipaddress.IPv4Address, netmask: Optional[str]) -> str:
    """The network the devices on an interface come from.

    Mesh VPNs give each device a single address (/32) and route the rest of 100.64/10
    to it, so the interface's own mask names nobody; their network is the whole block.
    Elsewhere the mask is the truth. An interface without one is just its address.
    """
    if addr in CGNAT_RANGE:
        return str(CGNAT_RANGE)
    if netmask:
        try:
            return str(ipaddress.ip_network(f"{addr}/{netmask}", strict=False))
        except ValueError:
            pass
    return f"{addr}/32"


def local_interfaces() -> List[LocalInterface]:
    """This machine's LAN and VPN addresses, in the order the OS reports them.

    IPv4 only, interfaces that are up, addresses inside a private range or the
    shared address space. Loopback, container bridges, link-local and public
    addresses are left out: the first two never carry another device, and a public
    address is not served (`public_addresses()` lists those). psutil is a declared
    dependency; if it fails the list is empty and the callers fall back to what they
    did before.
    """
    try:
        import psutil
        addresses = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except Exception as e:
        logger.debug("interface listing unavailable: %s", e)
        return []
    found: List[LocalInterface] = []
    for name, entries in addresses.items():
        state = stats.get(name)
        if state is not None and not state.isup:
            continue
        kind = _interface_kind(name)
        if kind is None:
            continue
        for entry in entries:
            if getattr(entry, "family", None) != socket.AF_INET or not entry.address:
                continue
            try:
                addr = ipaddress.ip_address(entry.address)
            except ValueError:
                continue
            if not any(addr in net for net in _ADMISSIBLE_RANGES):
                continue
            entry_kind = "vpn" if (kind == "vpn" or addr in CGNAT_RANGE) else "lan"
            found.append(LocalInterface(name=name, ip=str(addr),
                                        network=_interface_network(addr, entry.netmask),
                                        kind=entry_kind))
    return found


def public_addresses() -> List[Tuple[str, str]]:
    """This machine's public IPv4 addresses as (interface, ip), in the order the OS reports
    them, without duplicates.

    A public address means the machine sits on the internet (a rented server, a modem in
    bridge mode), where the access port must be closed in front of VAF. What counts is
    `classify_address(ip) == "public"`, on every interface that is up: no names are skipped,
    because a public address on a bridge (a server whose uplink is bridged, a name such as
    `bridge0` that `local_interfaces` leaves out) puts the machine on the internet as much as
    one on `eth0`. IPv6 deliberately out: the access port
    listens on IPv4 only, and home connections usually carry global IPv6 addresses, so
    counting them would call every home server public.

    Named boundary: a server behind 1:1 NAT (several clouds) sees only a private address and
    is not detected. When psutil fails, the source address of the default route stands in,
    as ("default", ip) when it is public. Never raises.
    """
    found: List[Tuple[str, str]] = []
    try:
        import psutil
        addresses = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except Exception as e:
        logger.debug("interface listing unavailable: %s", e)
        ip = _default_route_ip()
        return [("default", ip)] if ip and classify_address(ip) == "public" else []
    for name, entries in addresses.items():
        state = stats.get(name)
        if state is not None and not state.isup:
            continue
        for entry in entries:
            if getattr(entry, "family", None) != socket.AF_INET or not entry.address:
                continue
            if classify_address(entry.address) == "public" and (name, entry.address) not in found:
                found.append((name, entry.address))
    return found


# Why an entry of the admitted-networks setting was not taken. The codes travel to the
# web UI, which words them itself; the texts are for the CLI and the logs.
REFUSAL_REASONS = {
    "invalid": "not an IPv4 address or network",
    "ipv6": "IPv6 is not served: the access port listens on IPv4 only",
    "everything": "this would admit every address, which is the internet",
    "loopback": "this machine is always admitted",
    "not_private": "not a private network; VAF is never served to the internet",
}


def _setting_list(raw) -> List[str]:
    """A list setting as strings, whether stored as a list or typed as text."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(v) for v in raw]
    return str(raw).replace(",", " ").split()


def normalize_allowed_networks(values) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Sort an admin's network entries into the ones admitted and the ones refused.

    An entry is an IPv4 address (taken as /32) or a network; host bits are dropped,
    so "10.8.0.7/24" reads as 10.8.0.0/24. Taken only when it lies wholly inside a
    private range or the shared address space. Returns (normalized entries without
    duplicates, [(entry, reason code)]) with the codes of `REFUSAL_REASONS`.
    """
    taken: List[str] = []
    refused: List[Tuple[str, str]] = []
    for raw in _setting_list(values):
        value = raw.strip()
        if not value:
            continue
        try:
            net = ipaddress.ip_network(value, strict=False)
        except ValueError:
            refused.append((value, "invalid"))
            continue
        if net.version != 4:
            refused.append((value, "ipv6"))
        elif net.prefixlen == 0:
            refused.append((value, "everything"))
        elif any(net.subnet_of(n) for n in LOCALHOST_RANGES):
            refused.append((value, "loopback"))
        elif not any(net.subnet_of(n) for n in _ADMISSIBLE_RANGES):
            refused.append((value, "not_private"))
        elif str(net) not in taken:
            taken.append(str(net))
    return taken, refused


@dataclass(frozen=True)
class InboundPolicy:
    """Which client networks are admitted, and why."""
    vpn_only: bool
    allowed: Tuple[str, ...]                  # the admin's extra networks, normalized
    refused: Tuple[Tuple[str, str], ...]      # entries that were not taken, with the reason
    vpn: Tuple[str, ...]                      # networks of the detected VPN interfaces
    networks: Tuple[ipaddress.IPv4Network, ...]   # what is admitted, this machine included


def _flag(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def inbound_policy(cfg: Optional[dict] = None, *, detect_vpn: Optional[bool] = None) -> InboundPolicy:
    """The one answer to "which client networks are admitted".

    Always this machine. Then either the local networks (RFC 1918), or with
    `local_network_vpn_only` the networks of the detected VPN interfaces instead -
    none detected means nobody but this machine. Plus the admin's
    `local_network_allowed_networks`, after `normalize_allowed_networks`.

    `cfg` is a loaded configuration (read when omitted). The interfaces are only
    listed when "VPN only" needs them, or when `detect_vpn` asks for them for a
    display, so the per-request check costs no interface scan otherwise.
    """
    if cfg is None:
        from vaf.core.config import Config
        cfg = Config.load()
    vpn_only = _flag(cfg.get("local_network_vpn_only", False))
    allowed, refused = normalize_allowed_networks(cfg.get("local_network_allowed_networks"))
    vpn: List[str] = []
    if vpn_only or detect_vpn:
        for iface in local_interfaces():
            if iface.kind == "vpn" and iface.network not in vpn:
                vpn.append(iface.network)
    networks = list(LOCALHOST_RANGES)
    if vpn_only:
        networks += [ipaddress.ip_network(n) for n in vpn]
    else:
        networks += PRIVATE_RANGES
    networks += [ipaddress.ip_network(n) for n in allowed]
    return InboundPolicy(vpn_only=vpn_only, allowed=tuple(allowed), refused=tuple(refused),
                         vpn=tuple(vpn), networks=tuple(networks))


def _without_contained(cidrs: List[str]) -> List[str]:
    """Drop duplicates and every network that lies inside another one of the list."""
    nets = []
    for c in cidrs:
        net = ipaddress.ip_network(c)
        if net not in nets:
            nets.append(net)
    kept = [n for n in nets if not any(n != o and n.subnet_of(o) for o in nets)]
    return [str(n) for n in sorted(kept, key=lambda n: (int(n.network_address), n.prefixlen))]


def firewall_sources(*, narrow_lan: bool = False, cfg: Optional[dict] = None) -> List[str]:
    """The source networks the OS firewall opens the access port for.

    The same decision as `is_allowed_ip`, without this machine (every firewall admits
    its own loopback). With `narrow_lan` the local part is the subnets this machine
    sits on instead of all of RFC 1918 - firewalld has always opened only those.
    A detected VPN network is opened when it is admitted, so a WireGuard client is
    not stopped by the firewall that its address already passes in the app.
    """
    policy = inbound_policy(cfg, detect_vpn=True)
    if policy.vpn_only:
        sources = list(policy.vpn)
    else:
        if narrow_lan:
            sources = [i.network for i in local_interfaces() if i.kind == "lan"]
        else:
            sources = [str(n) for n in PRIVATE_RANGES]
        sources += [n for n in policy.vpn
                    if any(ipaddress.ip_network(n).subnet_of(a) for a in policy.networks)]
    sources += list(policy.allowed)
    return _without_contained(sources)


def _forwarded_hop(value: str) -> Optional[str]:
    """One ``X-Forwarded-For`` entry as an IP literal, or None when it is not one.

    Spaces and brackets around the entry are dropped; an IPv4-mapped IPv6 address
    (``::ffff:a.b.c.d``, what a dual-stack listener reports for an IPv4 client) becomes the
    IPv4 address, so the desktop's own 127.0.0.1 stays loopback whatever socket wrote it.
    A name (``localhost``), an address with a port, or anything else is not an address.
    """
    hop = value.strip().strip("[]")
    try:
        addr = ipaddress.ip_address(hop)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return str(addr)


def effective_client_ip(peer_ip: str | None, forwarded_for: str | None) -> str:
    """Resolve who the client REALLY is, accounting for a relay on this machine.

    Why this exists: the integrated HTTPS proxy terminates TLS on 0.0.0.0 and relays every
    LAN device to the backend over loopback, so the raw socket peer is 127.0.0.1 for remote
    users too. Trusting the peer alone therefore treats the whole LAN as local.

    The polarity is deliberately fail-safe: a forwarding hop REMOVES trust, it never grants it.
    ``X-Forwarded-For`` is honoured only when the immediate peer is itself loopback (a relay
    on this machine: the integrated proxy, the Next.js /api door, a tunnel or reverse proxy
    the owner put in front). A direct non-loopback peer keeps its socket address no matter
    what it claims.

    Which hop: walk the header from the RIGHT and take the first hop that is not loopback.
    A relay appends the address it saw, and a client can only write to the LEFT of that, so
    the rightmost entries are the ones no client controls; a loopback hop names this machine
    (one relay handing to the next) and is skipped. The FIRST hop, read before, is whatever
    the client wrote. uvicorn's default proxy-header handling (trusting 127.0.0.1) applies the
    rightmost rule to the peer on the backend's loopback listeners; the listeners other devices
    reach switch it off (vaf.network.door.door_options: it trusts FORWARDED_ALLOW_IPS, and with
    "*" a LAN device that wrote 127.0.0.1 was this machine), so there this resolver is the only
    rule, and it does not hang on a server default anywhere. A hop that is not an IP literal (a name such as "localhost", which is_localhost
    accepts) ends the walk as "unknown", which is neither local nor admitted.

    No hops, or only loopback hops (internal loopback IPC, the desktop through the Next.js
    /api door, which forwards its own 127.0.0.1): the peer, unchanged.

    NAMED BOUNDARY: a raw TCP forward on this machine (``ssh -R``, socat) writes no header at
    all, so its clients ARE this machine to every check. Never put one in front of VAF.
    """
    peer = (peer_ip or "").strip() or "unknown"
    if not is_localhost(peer):
        return peer
    if not (forwarded_for or "").strip():
        return peer
    for raw in reversed(forwarded_for.split(",")):
        hop = _forwarded_hop(raw)
        if hop is None:
            return "unknown"
        if not is_localhost(hop):
            return hop
    return peer


def connection_client_ip(conn) -> str:
    """The real client of a Starlette connection (a Request or a WebSocket).

    The ONE place in the backend that reads the socket peer. Every site that decides or records
    who a client is - the IP check, the auth middleware, the rate limiter, the WebSocket
    handshake, the OAuth callback exception, the security log - asks this, so none of them can
    pass the raw peer by mistake. That mistake happened: the IP check read
    ``request.client.host`` while the others resolved the hop, and since the peer is 127.0.0.1
    for everything the integrated proxy relays, the check let a public address through on HTTP
    (measured: 200 relayed, 403 direct). tests/test_client_address_has_one_reader.py refuses a
    raw read anywhere else in vaf/.

    A pure ASGI middleware wraps its scope: ``connection_client_ip(HTTPConnection(scope))``.

    Every ``X-Forwarded-For`` line counts, joined in order (RFC 9110 5.3: repeated lines are
    one comma-separated list). A relay that adds its own line (HAProxy's ``option forwardfor``)
    puts the client it saw in the LAST line; reading the first line alone took the client's own
    line for the answer, so a remote client could name ``127.0.0.1`` there and be this machine
    wherever uvicorn's own proxy-header handling is off.
    """
    client = getattr(conn, "client", None)
    lines = [line for line in conn.headers.getlist("x-forwarded-for") if line]
    return effective_client_ip(client.host if client else None,
                               ", ".join(lines) if lines else None)


# ---------------------------------------------------------------------------
# Which requests come from a web page that is not VAF's own
# ---------------------------------------------------------------------------
#
# A tokenless request from this machine is the owner (the desktop, internal IPC), and the
# vaf_token cookie is ambient: SameSite=Lax keys on the SITE, and every port of localhost is
# one site. Both trusts were therefore available to ANY web page open in a browser on this
# machine: the CORS rule let every localhost and RFC 1918 origin read the answers, and a page
# from anywhere could send simple requests, rebind a DNS name to 127.0.0.1, or open the
# WebSocket with the cookie attached. The browser says where a request comes from (Origin,
# Sec-Fetch-Site, the Host it dialled); these functions read that and nothing else, so a
# client that sends none of it (CLI, sub-agent IPC, the tray, a script) is not judged at all.

_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _host_and_port(value: str | None) -> Optional[Tuple[str, Optional[int]]]:
    """``host``, ``host:port``, ``[v6]`` or ``[v6]:port`` as (lowercase host, port or None).

    None when the value is empty or does not parse: the callers treat that as foreign.
    """
    from urllib.parse import urlsplit

    raw = (value or "").strip()
    if not raw or any(c in raw for c in "/?#@ "):
        return None
    try:
        parts = urlsplit("//" + raw)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    return (host, port) if host else None


def _host_name() -> str:
    try:
        return socket.gethostname() or ""
    except Exception:
        return ""


def machine_dns_names() -> List[str]:
    """This machine's own DNS names besides `localhost`: the host name and, when it differs,
    the FQDN. The local certificate carries exactly these (ssl_utils._collect_sans).

    Every call is a lookup (getfqdn()) and renews the memo own_name_labels() reads, unless
    the names the memo holds come from a lookup that started after this one: the memo keeps
    the lookup that started last, not the one that finished last. So a renewal already
    running when a certificate's lookup begins cannot put older names back once the
    certificate is issued, and the HSTS scope holds the names of the certificate's lookup or
    of a newer one that found an FQDN.

    A lookup that finds no FQDN (getfqdn() failed, or answered the host name itself, which
    is also its answer while DNS is down) brings no names: it keeps the names the memo
    already holds for this host name and their place in the start order, and renews only
    their time. Otherwise a short DNS outage at a renewal would drop the FQDN and hand that
    name, which the certificate carries, a year of HSTS, and a renewal that started while a
    certificate's lookup ran would outrank that lookup and throw away the FQDN it found. On
    the safe side, an FQDN that is removed while VAF runs keeps max-age=0 until a restart."""
    hostname = _host_name()
    with _OWN_LABELS_LOCK:
        order = next(_OWN_LABELS_STARTS)
    started_at = time.monotonic()
    names: List[str] = []
    if hostname and hostname != "localhost":
        names.append(hostname)
        try:
            fqdn = socket.getfqdn()
            if fqdn and fqdn != hostname and fqdn != "localhost":
                names.append(fqdn)
        except Exception:
            pass
    labels = _first_labels(names)
    with _OWN_LABELS_LOCK:
        stored = _OWN_LABELS.get(hostname)
        if stored is None or stored[2] < order:
            if stored is not None and len(names) < 2:
                # No FQDN found: the old names stay, the time is renewed all the same, so a
                # machine without an FQDN is not looked up again on every answer. The names
                # keep their place in the start order as well: this lookup carries nothing
                # new, so it must not outrank one that started before it and is still
                # running (a certificate's lookup that finds the FQDN lands after it).
                labels, order = stored[0], stored[2]
            _OWN_LABELS[hostname] = (labels, started_at, order)
    return names


def _first_labels(names: List[str]) -> FrozenSet[str]:
    return frozenset(label for label in (n.lower().rstrip(".").split(".")[0] for n in names)
                     if label and label != "localhost")


# Host name -> (the first labels of machine_dns_names(), time.monotonic() when the lookup that
# last renewed the entry started, the place in the start order of the lookup the labels came
# from; a lookup that finds no FQDN renews only the time). getfqdn() is a DNS lookup and HSTS
# asks on every answer, so an answer reads this memo; the start fills it
# (ensure_ssl_certificates(), run_https_proxy()). Deliberately not kept for the process life:
# when DNS is not up at the start, getfqdn() answers with the bare host name, and the FQDN the
# certificate may carry would get the year until a restart. An entry older than
# OWN_NAMES_MAX_AGE_S is looked up again in a thread of its own, one at a time, while the
# answers go on with the old one. The start order is a counter taken under the lock, not the
# clock: two starts can read the same time.monotonic() value on a coarse clock, and then the
# clock cannot say which lookup is the newer one.
_OWN_LABELS: Dict[str, Tuple[FrozenSet[str], float, int]] = {}
OWN_NAMES_MAX_AGE_S = 60.0
_OWN_LABELS_LOCK = threading.Lock()
_OWN_LABELS_STARTS = itertools.count(1)
_OWN_LABELS_REFRESHER: Optional[threading.Thread] = None


def _refresh_own_names_in_background() -> None:
    global _OWN_LABELS_REFRESHER
    with _OWN_LABELS_LOCK:
        if _OWN_LABELS_REFRESHER is not None and _OWN_LABELS_REFRESHER.is_alive():
            return
        refresher = threading.Thread(target=machine_dns_names, name="vaf-own-names", daemon=True)
        try:
            refresher.start()
        except RuntimeError:
            return
        _OWN_LABELS_REFRESHER = refresher


def own_name_labels() -> FrozenSet[str]:
    """The first labels, lowercase, of the names the local certificate carries for this
    machine (machine_dns_names(): the host name and the FQDN).

    Read from a memo per host name, so an answer does not wait on DNS: the first answer that
    finds a result older than OWN_NAMES_MAX_AGE_S starts one lookup in a thread of its own,
    and every answer gets the old result until that lookup lands. A lookup that never
    returns (a resolver that hangs) leaves the names as they were, because no second renewal
    starts while it runs; a certificate issued meanwhile still renews them with its own
    lookup. NAMED BOUNDARY: a host name with no entry yet (a host renamed while VAF runs, or
    an answer that came before the start's lookup) is looked up in front of that one
    answer."""
    entry = _OWN_LABELS.get(_host_name())
    if entry is None:
        return _first_labels(machine_dns_names())
    labels, looked_up_at, _order = entry
    if time.monotonic() - looked_up_at >= OWN_NAMES_MAX_AGE_S:
        _refresh_own_names_in_background()
    return labels


HSTS_VALUE = "max-age=31536000"
# max-age=0 makes a browser drop a pin it holds for that name (RFC 6797 6.1.1). Versions
# before this scoping sent a year with includeSubDomains for every dialled name.
HSTS_CLEAR = "max-age=0"


def hsts_for_host(dialled_host: str | None) -> Optional[str]:
    """The Strict-Transport-Security value for an answer to a request that dialled this host,
    or None when the header must not be sent.

    HSTS applies to a host name on EVERY port, for a year: the browser then refuses plain
    HTTP to any port of that name. The local certificate names `localhost`, this machine's
    host name and its FQDN besides its addresses, and those names are how every other service
    on the machine is reached too (the Web UI on port 3000, a NAS page, a printer, an app on
    another port); HSTS on them would make each plain-HTTP one unreachable from that browser
    for a year. So a year is never sent for `localhost` or `*.localhost`, nor for one of this
    machine's own names: any name whose first label is the first label of a name the
    certificate carries for it (own_name_labels(): the host name and the FQDN, which can
    differ), so the host name itself, its FQDN, `<name>.local`, a router's or a VPN's name
    for it. Those get `max-age=0` (HSTS_CLEAR), so a pin an earlier version left behind is
    dropped at the next visit. Nothing is sent for an IP literal (browsers ignore it there,
    RFC 6797 8.1.1, and an address is how other devices usually dial VAF), and never
    `includeSubDomains`. What gets the year is a name set up for VAF on purpose, with a
    certificate of its own, whose first label is not one of the machine's: `vaf.example.lan`
    on a host called `vaf` counts as the machine's own and gets `max-age=0`. VAF serves the
    LAN and the VPN only.

    The machine's names come from a memo (own_name_labels()) that the start fills and a
    lookup beside the answers renews once a minute at most. A renewal that finds no FQDN
    (DNS down for a moment) keeps the names the memo holds. NAMED BOUNDARY: an FQDN that
    did not resolve at the start (DNS not up yet) or that changes while VAF runs counts
    from the first renewal after it, so the answers before that use the names of the
    previous lookup; an FQDN removed while VAF runs keeps max-age=0 until a restart; and
    the one answer that waits on DNS is the first for a host name with no lookup yet (a
    host renamed while VAF runs, or an answer ahead of the start's).
    """
    parsed = _host_and_port(dialled_host)
    if not parsed:
        return None
    # A trailing dot is the same name (`localhost.` is `localhost`).
    host = parsed[0].strip("[]").rstrip(".")
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if host == "localhost" or host.endswith(".localhost"):
        return HSTS_CLEAR
    if host.split(".")[0] in own_name_labels():
        return HSTS_CLEAR
    return HSTS_VALUE


def _origin_tuple(origin: str | None) -> Optional[Tuple[str, str, int]]:
    """An Origin header as (scheme, host, port), the default port filled in. None when it is
    ``null``, not http(s), or carries anything an origin cannot carry."""
    from urllib.parse import urlsplit

    raw = (origin or "").strip()
    if not raw or raw == "null":
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    if parts.username is not None or not parts.hostname:
        return None
    return scheme, parts.hostname.lower(), port or _DEFAULT_PORTS[scheme]


def canonical_origin(origin: str | None) -> Optional[str]:
    """``origin`` rebuilt as ``scheme://host[:port]`` from its parsed parts (lowercase, default
    port omitted, IPv6 bracketed), or None when it is no origin. An empty ``?`` or ``#`` parses to
    nothing and passes :func:`_origin_tuple`, so a caller that hands an origin on must hand on
    THIS, never the string it received - ``http://localhost:3000?`` plus a path is a URL whose
    path became its query."""
    parts = _origin_tuple(origin)
    if parts is None:
        return None
    scheme, host, port = parts
    shown = f"[{host}]" if ":" in host else host
    return f"{scheme}://{shown}" + ("" if port == _DEFAULT_PORTS[scheme] else f":{port}")


def _cannot_be_rebound(hostport: str | None) -> bool:
    """True when a browser that dialled this Host cannot have been pointed at us by DNS.

    DNS rebinding needs a NAME the attacker controls; ``localhost`` and a literal IP address
    never resolve through the attacker's DNS. A reverse proxy that passes the browser's own IP
    as Host (the documented nginx setup) therefore stays valid, and a DNS name does not.
    """
    parsed = _host_and_port(hostport)
    if parsed is None:
        return False
    host = parsed[0]
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def is_own_frontend_origin(origin: str | None) -> bool:
    """True for the Web UI served on this machine: plain http on a loopback name, on the port
    the frontend really runs on (:func:`frontend_port`). This is the one origin that reaches the
    backend CROSS-origin (the WebSocket and ``/api/version`` while the Next server rebuilds),
    so it is also the whole CORS allow list."""
    parts = _origin_tuple(origin)
    if parts is None:
        return False
    scheme, host, port = parts
    return scheme == "http" and host in _LOOPBACK_NAMES and port == frontend_port()


def is_own_origin(origin: str | None, headers, *, scheme: str) -> bool:
    """True when ``origin`` is a page of VAF's own, judged for the request these headers are.

    Own is: this request's own origin (``scheme`` + ``Host``, with a proxy's
    ``X-Forwarded-Proto`` taking the scheme), the origin a TLS proxy was dialled under
    (``https://`` + ``X-Forwarded-Host``: the integrated proxy, nginx), or the Web UI on this
    machine (:func:`is_own_frontend_origin`). ``null`` never is. This answers WHICH origin is
    own only; whether the Host itself may be trusted is :func:`foreign_request_reason`'s first
    step, which runs before this on every browser request. It is also the one answer for an
    origin a page hands the API as data (an OAuth return address), so there are not two.
    """
    own = _origin_tuple(origin)
    if own is None:
        return False
    fwd_proto = (headers.get("x-forwarded-proto") or "").strip().lower()
    scope_scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    this_scheme = fwd_proto if fwd_proto in _DEFAULT_PORTS else scope_scheme
    dialled = _host_and_port(headers.get("host"))
    if dialled and this_scheme in _DEFAULT_PORTS:
        if own == (this_scheme, dialled[0], dialled[1] or _DEFAULT_PORTS[this_scheme]):
            return True
    if fwd_proto == "https":
        proxied = _host_and_port(headers.get("x-forwarded-host"))
        if proxied and own == ("https", proxied[0], proxied[1] or 443):
            return True
    return is_own_frontend_origin(origin)


def foreign_request_reason(headers, *, scheme: str, method: str) -> Optional[str]:
    """Why a browser request comes from a page that is not VAF's own, or None when it may pass.

    ``headers`` is a case-insensitive mapping (Starlette ``Headers``); ``scheme`` is the ASGI
    scope's (``ws``/``wss`` count as ``http``/``https``). The reasons are ``"host"``,
    ``"origin"`` and ``"site"``.

    1. No ``Origin`` and no ``Sec-Fetch-Site``: not a web page, not judged.
    2. The ``Host`` the browser dialled must be ``localhost`` or an IP address (DNS rebinding).
       ``X-Forwarded-Host`` is held to the same rule unless ``X-Forwarded-Proto`` is https: the
       Next.js /api door forwards the browser's Host that way, and a TLS door cannot be rebound
       (the certificate does not match the attacker's name). The real Host is always checked, so
       forwarding headers a same-origin page adds itself buy it nothing.
    3. With an ``Origin``: it must be this request's own origin, the origin a TLS proxy was
       dialled under (``https://`` + X-Forwarded-Host), or the Web UI (:func:`is_own_frontend_origin`).
       ``null`` is never own.
    4. Without one: ``Sec-Fetch-Site`` same-origin or none (typed, bookmarked) passes, and so does
       a top-level GET navigation, a link the person followed (how OAuth callbacks arrive; an
       OAuth callback is protected by its ``state``). A cross-site image, script, frame or form
       from someone else's page does not.

    Named boundaries: a browser that sends no fetch metadata (Safari before 16.4) is judged on
    Origin and Host alone, so its plain cross-site GET is not refused; GET navigations from other
    pages pass, as they do for SameSite=Lax cookies, so a GET must not change state.
    """
    origin = headers.get("origin")
    site = (headers.get("sec-fetch-site") or "").strip().lower()
    if origin is None and not site:
        return None

    host = headers.get("host")
    if not _cannot_be_rebound(host):
        return "host"
    fwd_proto = (headers.get("x-forwarded-proto") or "").strip().lower()
    fwd_host = headers.get("x-forwarded-host")
    if fwd_host is not None and fwd_proto != "https" and not _cannot_be_rebound(fwd_host):
        return "host"

    if origin is not None:
        return None if is_own_origin(origin, headers, scheme=scheme) else "origin"

    if site in ("same-origin", "none"):
        return None
    if ((method or "").upper() in ("GET", "HEAD")
            and (headers.get("sec-fetch-mode") or "").strip().lower() == "navigate"
            and (headers.get("sec-fetch-dest") or "").strip().lower() == "document"):
        return None
    return "site"


# ---------------------------------------------------------------------------
# What an OUTBOUND destination address is
# ---------------------------------------------------------------------------
#
# One answer for every outbound check (mail servers, the mail image proxy, the agent's
# web fetches). Explicit sets rather than ipaddress.is_global / is_private, because those
# tables differ between the Python versions VAF supports and are wrong for the cases that
# matter here: Python 3.13 reports the NAT64 form of 127.0.0.1 (64:ff9b::7f00:1), the
# IPv4-compatible form (::7f00:1) and the deprecated site-local fec0::/10 as GLOBAL.
# Addresses that carry an IPv4 inside an IPv6 are judged by the IPv4 they reach.

_V4_LOOPBACK = (ipaddress.ip_network("127.0.0.0/8"),)
_V4_PRIVATE = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "100.64.0.0/10",     # shared address space: carrier NAT, Tailscale
    "198.18.0.0/15",     # benchmarking; fake-IP proxies resolve every name into it
))
_V4_FORBIDDEN = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8",         # "this network": 0.x reaches the local host on Linux
    "169.254.0.0/16",    # link-local, including the 169.254.169.254 cloud metadata service
    "192.0.0.0/24", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4",       # multicast
    "240.0.0.0/4",       # reserved, including 255.255.255.255
))
_V6_PRIVATE = (ipaddress.ip_network("fc00::/7"),)      # unique local
_V6_FORBIDDEN = tuple(ipaddress.ip_network(n) for n in (
    "::/128", "100::/64", "2001::/23", "2001:db8::/32", "3fff::/20", "5f00::/16",
    "fe80::/10", "fec0::/10", "ff00::/8",
))
_V6_GLOBAL_UNICAST = ipaddress.ip_network("2000::/3")
_V6_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
_V6_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def _embedded_ipv4(addr: ipaddress.IPv6Address) -> Optional[ipaddress.IPv4Address]:
    """The IPv4 an IPv6 address actually reaches, or None when it carries none."""
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    if any(addr in net for net in _V6_NAT64):
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    if addr.sixtofour is not None:
        return addr.sixtofour
    if addr.teredo is not None:
        return addr.teredo[1]
    if addr in _V6_IPV4_COMPATIBLE and int(addr) > 1:   # not :: and not ::1
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    return None


def classify_address(ip: str) -> str:
    """Classify an outbound destination: "public", "private", "loopback" or "forbidden".

    - loopback: this machine (127.0.0.0/8, ::1), where VAF's own backend listens.
    - private: a LAN or a carrier/overlay network (RFC 1918, 100.64/10, 198.18/15, fc00::/7).
    - forbidden: never a destination - link-local and the cloud metadata service,
      multicast, documentation, reserved and unspecified ranges.
    - public: everything else; for IPv6 only the global unicast block 2000::/3.

    An unparseable string is "forbidden". A scoped IPv6 (fe80::1%eth0) parses and is
    judged without its scope.
    """
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return "forbidden"
    if isinstance(addr, ipaddress.IPv6Address):
        inner = _embedded_ipv4(addr)
        if inner is not None:
            addr = inner
        else:
            if addr == ipaddress.IPv6Address("::1"):
                return "loopback"
            if any(addr in net for net in _V6_FORBIDDEN):
                return "forbidden"
            if any(addr in net for net in _V6_PRIVATE):
                return "private"
            return "public" if addr in _V6_GLOBAL_UNICAST else "forbidden"
    if any(addr in net for net in _V4_LOOPBACK):
        return "loopback"
    if any(addr in net for net in _V4_FORBIDDEN):
        return "forbidden"
    if any(addr in net for net in _V4_PRIVATE):
        return "private"
    return "public"


def assert_safe_remote_host(host: str, *, allow_private: bool = False) -> None:
    """SSRF guard for user-supplied OUTBOUND targets (e.g. an IMAP/SMTP server a user types
    into the email wizard). Resolves the host and raises ValueError if ANY resolved address is
    not globally routable.

    - Forbidden addresses (classify_address: link-local incl. the 169.254.169.254 cloud-metadata
      endpoint, multicast, documentation, reserved, unspecified) are NEVER allowed, even with the
      override.
    - Loopback and private addresses are allowed only when allow_private=True (so a user who
      genuinely runs a LAN / self-hosted mail server can opt in via email_allow_private_hosts).

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
        kind = classify_address(ip)
        if kind == "public":
            continue
        if kind == "forbidden":
            raise ValueError(f"Refusing to connect to non-routable address ({ip}) for host {h}")
        if allow_private:
            continue
        raise ValueError(
            f"Refusing to connect to private address ({ip}) for host {h}. "
            "Set email_allow_private_hosts=true to allow a LAN / self-hosted server."
        )


def assert_ip_safe(ip: str, *, allow_private: bool = False) -> None:
    """SSRF guard on an ALREADY-RESOLVED address. Same policy as
    assert_safe_remote_host, but the caller resolves the host once and then pins
    the connection to this exact IP - closing the resolve-vs-connect (DNS
    rebinding) TOCTOU that assert_safe_remote_host cannot. Raises ValueError if
    the address is not a safe outbound target. With allow_private, loopback and
    private addresses pass (a mail server on this machine, e.g. Proton Bridge);
    forbidden ones never do (see classify_address)."""
    kind = classify_address(ip)
    if kind == "public":
        return
    if kind == "forbidden":
        raise ValueError(f"Refusing to connect to non-routable address ({ip})")
    if allow_private:
        return
    raise ValueError(f"Refusing to connect to private address ({ip})")


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


def _port_or(value, default: int) -> int:
    """A port from config, or the default when it is not a usable number.

    The server-mode keys are documented as hand-editable in config.json, so a
    typo reaches this code as a string like "abc". Raising here would abort
    whatever asked - provisioning mid-way, a status line, the firewall thread -
    over one cosmetic value, so a bad entry degrades to the default instead.
    """
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 0 < port < 65536 else default


def frontend_port() -> int:
    """The port the Web UI (Next.js) actually serves on - the one answer every reader uses.

    The frontend moves to the next free port when the configured one is taken and writes the
    port it really bound to a file (``FrontendManager.get_active_port``, removed again on stop).
    That file is the truth while the frontend runs; the config value answers before it starts
    and after it stopped. ``VAF_WEB_UI_PORT``, when set, overrides both. It used to be read in
    five places with a hardcoded fallback of 3000 and was set nowhere, so a frontend that had
    moved to 3001 got its OAuth return sent to a port nothing listened on.
    """
    import os

    override = _port_or(os.environ.get("VAF_WEB_UI_PORT"), 0)
    if override:
        return override
    try:
        from vaf.core.frontend_manager import FrontendManager
        active = _port_or(FrontendManager().get_active_port(), 0)
    except Exception:
        active = 0
    if active:
        return active
    from vaf.core.config import Config
    return _port_or(Config.get("local_network_port_frontend", 3000), 3000)


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
            _port_or(Config.get("local_network_port", 8001), 8001),
            _port_or(Config.get("local_network_port_frontend", 3000), 3000),
        )

    access_port: Optional[int] = None
    if wait_for_proxy:
        import time
        from vaf.network import runtime_status
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while time.monotonic() < deadline:
            st = runtime_status.get_proxy_status()
            if st.get("bound") and st.get("effective_https_port"):
                access_port = _port_or(st["effective_https_port"], 0) or None
                break
            time.sleep(0.5)
    if access_port is None:
        configured = _port_or(Config.get("local_network_https_port", 443), 443)
        access_port = 8443 if configured == 443 else configured
    return access_port, _port_or(Config.get("local_network_port", 8001), 8001)


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


def _default_route_ip() -> Optional[str]:
    """The source address of the default route. A UDP connect sends no packet."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.1)
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None


def get_all_local_ips() -> List[Tuple[str, str]]:
    """Every address another device could reach this machine on: (interface, ip).

    The LAN and VPN interfaces of `local_interfaces()`. Certificates carry all of
    them, so an admin who admits a VPN network later needs no new certificate. When
    the interface listing yields nothing, the default route's source address stands
    in, as long as it is a local one.
    """
    found = [(iface.name, iface.ip) for iface in local_interfaces()]
    if found:
        return found
    ip = _default_route_ip()
    return [('default', ip)] if ip and is_private_ip(ip) else []


# Which LAN interface counts as THE local address when there are several.
_LAN_PREFERENCE = ('eth', 'en', 'wlan', 'wifi', 'lan')


def _preference(iface: LocalInterface) -> Tuple[int, int, str]:
    lowered = iface.name.lower()
    rank = next((i for i, p in enumerate(_LAN_PREFERENCE) if p in lowered), len(_LAN_PREFERENCE))
    return (0 if iface.kind == "lan" else 1, rank, iface.name)


def get_local_network_ip() -> str:
    """This machine's address on its local network (a LAN interface, never a VPN).

    The DHCP warning, the firewall's LAN subnet and room invitations are about the
    local network, so they use this. A device that only has VPN interfaces (a rented
    server) has none: `access_addresses()` is the call for "where can a device reach
    me".

    Raises:
        RuntimeError: If no local network interface is found
    """
    lan = sorted((i for i in local_interfaces() if i.kind == "lan"), key=_preference)
    if lan:
        return lan[0].ip
    ip = _default_route_ip()
    if ip and is_private_ip(ip):
        return ip
    raise RuntimeError(
        "No local network interface found. "
        "Please ensure you are connected to a local network (WiFi or Ethernet)."
    )


def access_addresses(cfg: Optional[dict] = None) -> List[LocalInterface]:
    """The interfaces whose devices are admitted right now, LAN first.

    An interface counts when its own address passes the admission (`is_allowed_ip`
    with the same policy): a LAN address in "VPN only" does not, a Tailscale address
    only once its network is admitted. This is what the settings, `vaf top` and
    `vaf server status` show as access URLs.
    """
    policy = inbound_policy(cfg)
    admitted = []
    for iface in local_interfaces():
        addr = ipaddress.ip_address(iface.ip)
        if any(addr in net for net in policy.networks):
            admitted.append(iface)
    return sorted(admitted, key=_preference)
