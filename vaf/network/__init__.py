# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Network Module - Local Network Security and Binding

Provides:
- Local network IP detection
- Cross-platform firewall rules
- IP validation for LAN-only access

CRITICAL: This module ensures VAF is NEVER exposed to the internet.
Admitted are this machine, the RFC 1918 networks (192.168.x.x, 10.x.x.x, 172.16-31.x.x)
and the private networks an admin adds (a VPN); with "VPN only" the RFC 1918 part is
replaced by the networks of the detected VPN interfaces. Public networks are refused.
The decision is `inbound_policy()` in binding.py.
"""

from vaf.network.binding import (
    get_local_network_ip,
    get_all_local_ips,
    is_private_ip,
    is_localhost,
    is_allowed_ip,
    PRIVATE_RANGES,
    LocalInterface,
    local_interfaces,
    access_addresses,
    InboundPolicy,
    inbound_policy,
    normalize_allowed_networks,
    firewall_sources,
)
from vaf.network.firewall import (
    setup_firewall,
    cleanup_firewall,
    is_firewall_configured
)

__all__ = [
    # Binding
    "get_local_network_ip",
    "get_all_local_ips", 
    "is_private_ip",
    "is_localhost",
    "is_allowed_ip",
    "PRIVATE_RANGES",
    "LocalInterface",
    "local_interfaces",
    "access_addresses",
    "InboundPolicy",
    "inbound_policy",
    "normalize_allowed_networks",
    "firewall_sources",
    # Firewall
    "setup_firewall",
    "cleanup_firewall",
    "is_firewall_configured",
]
