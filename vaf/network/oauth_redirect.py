# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Shared OAuth callback base-URL logic.

The OAuth redirect_uri must point at a URL that is ACTUALLY reachable on this machine. Behind the
integrated HTTPS proxy a non-root user cannot bind privileged 443 and the proxy falls back to 8443;
`runtime_status` is the single source of truth for the port it really bound. Email, Cloud (and any
future) OAuth flows share this helper so their callbacks stay consistent and reachable — see
`vaf/api/email_routes.py` and `vaf/cloud/oauth_cloud.py`.
"""
from __future__ import annotations

from vaf.core.config import Config


def effective_https_suffix() -> str:
    """Return the ':<port>' suffix for the HTTPS URL that is ACTUALLY reachable, or '' for 443.

    Uses the proxy's real bound port (`runtime_status.get_proxy_status()`); 443 is privileged and
    cannot be bound by a non-root desktop user, so an unknown/unbound 443 maps to the 8443 fallback."""
    configured = int(Config.get("local_network_https_port", 443) or 443)
    port = configured
    try:
        from vaf.network import runtime_status
        eff = runtime_status.get_proxy_status().get("effective_https_port")
        if eff:
            port = int(eff)            # proxy confirmed the port it really bound (e.g. 8443)
        elif configured == 443:
            port = 8443                # not bound yet / unknown: 443 is privileged -> universal fallback
    except Exception:
        if configured == 443:
            port = 8443
    return "" if port == 443 else f":{port}"


def oauth_callback_base_url(override_key: str) -> str:
    """Base URL (scheme://host[:port]) for an OAuth redirect_uri, pointing at THIS backend.

    `override_key` is the config key holding an explicit override (e.g. behind a reverse proxy) —
    e.g. "email_oauth_callback_base_url" or "cloud_oauth_callback_base_url". Otherwise: in network+TLS
    mode use the integrated HTTPS proxy on its effective port; in localhost mode use the plain backend.
    """
    explicit = (Config.get(override_key) or "").strip().rstrip("/")
    if explicit:
        return explicit
    network_on = bool(Config.get("local_network_enabled", False))
    tls_on = bool(Config.get("local_network_tls_enabled", False))
    if network_on and tls_on:
        return f"https://localhost{effective_https_suffix()}"
    port = int(Config.get("local_network_port", 8001) or 8001)
    return f"http://localhost:{port}"
