# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""resolve_lan_access_ports: the ONE source of "which port do LAN clients reach".

The logic used to live inline in the web server's startup handler; the CLI
provisioning verb needs the identical answer out of process, where the proxy's
runtime status does not exist. These tests pin both halves: the in-process poll
for the port the proxy actually bound, and the deterministic out-of-process
assumption (configured port, 443 mapped to the 8443 fallback)."""
import vaf.network.binding as binding
from vaf.core.config import Config
from vaf.network import runtime_status


def _cfg(monkeypatch, values):
    monkeypatch.setattr(Config, "get", lambda key, default=None: values.get(key, default))


def test_tls_off_returns_backend_and_frontend_ports(monkeypatch):
    _cfg(monkeypatch, {
        "local_network_tls_enabled": False,
        "local_network_port": 8001,
        "local_network_port_frontend": 3000,
    })
    assert binding.resolve_lan_access_ports() == (8001, 3000)


def test_tls_on_out_of_process_assumes_the_443_to_8443_fallback(monkeypatch):
    _cfg(monkeypatch, {
        "local_network_tls_enabled": True,
        "local_network_https_port": 443,
        "local_network_port": 8001,
    })
    assert binding.resolve_lan_access_ports(wait_for_proxy=False) == (8443, 8001)


def test_tls_on_a_nonstandard_configured_port_is_kept(monkeypatch):
    _cfg(monkeypatch, {
        "local_network_tls_enabled": True,
        "local_network_https_port": 9443,
        "local_network_port": 8001,
    })
    assert binding.resolve_lan_access_ports(wait_for_proxy=False) == (9443, 8001)


def test_tls_on_in_process_uses_the_port_the_proxy_actually_bound(monkeypatch):
    _cfg(monkeypatch, {
        "local_network_tls_enabled": True,
        "local_network_https_port": 443,
        "local_network_port": 8001,
    })
    monkeypatch.setattr(runtime_status, "get_proxy_status",
                        lambda: {"bound": True, "effective_https_port": 8443})
    assert binding.resolve_lan_access_ports(wait_for_proxy=True) == (8443, 8001)


def test_in_process_wait_times_out_to_the_configured_assumption(monkeypatch):
    _cfg(monkeypatch, {
        "local_network_tls_enabled": True,
        "local_network_https_port": 443,
        "local_network_port": 8001,
    })
    monkeypatch.setattr(runtime_status, "get_proxy_status", lambda: {"bound": False})
    # timeout_s=0 skips the poll loop entirely - no sleeping in the suite
    assert binding.resolve_lan_access_ports(wait_for_proxy=True, timeout_s=0) == (8443, 8001)
