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
Only RFC 1918 private IPs are allowed (192.168.x.x, 10.x.x.x, 172.16-31.x.x)
"""

from vaf.network.binding import (
    get_local_network_ip,
    get_all_local_ips,
    is_private_ip,
    is_localhost,
    is_allowed_ip,
    PRIVATE_RANGES
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
    # Firewall
    "setup_firewall",
    "cleanup_firewall",
    "is_firewall_configured",
]
