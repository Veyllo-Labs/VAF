# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf server provision`: the one-shot server-mode provisioning verb.

install.sh's server mode calls this instead of writing config JSON inline, so
the contract matters: the three server keys go through Config, the firewall gets
the RESOLVED access port, a failed firewall degrades to manual instructions with
exit 0 (the installer must keep going), and only a non-Linux platform refuses."""
import pytest
from typer.testing import CliRunner

import vaf.cli.cmd.server as server_cmd
import vaf.network.binding as binding
import vaf.network.firewall as firewall
import vaf.network.ssl_utils as ssl_utils
from vaf.core.config import Config

runner = CliRunner()


@pytest.fixture
def seams(monkeypatch):
    """Everything provision composes, replaced at its SOURCE module (the verb
    imports late exactly so these patches take effect)."""
    calls = {"set": [], "firewall": []}
    monkeypatch.setattr(Config, "set", lambda key, value: calls["set"].append((key, value)))
    monkeypatch.setattr(ssl_utils, "ensure_ssl_certificates",
                        lambda: ("/tmp/certs/server.pem", "/tmp/certs/server-key.pem"))
    monkeypatch.setattr(binding, "resolve_lan_access_ports",
                        lambda wait_for_proxy=False: (8443, 8001))
    monkeypatch.setattr(binding, "lan_ip_is_dhcp", lambda: None)
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [("eth0", "192.168.1.10")])
    monkeypatch.setattr(firewall, "setup_firewall",
                        lambda p, pf: calls["firewall"].append((p, pf)) or "created")
    monkeypatch.setattr(server_cmd.platform, "system", lambda: "Linux")
    return calls


def test_provision_sets_the_three_server_keys(seams):
    result = runner.invoke(server_cmd.app, ["provision"])
    assert result.exit_code == 0, result.output
    for key in ("server_mode", "local_network_enabled", "local_network_tls_enabled"):
        assert (key, True) in seams["set"], seams["set"]


def test_the_firewall_gets_the_resolved_access_port(seams):
    result = runner.invoke(server_cmd.app, ["provision"])
    assert result.exit_code == 0, result.output
    assert seams["firewall"] == [(8443, 8001)]


def test_no_firewall_skips_the_firewall_step(seams):
    result = runner.invoke(server_cmd.app, ["provision", "--no-firewall"])
    assert result.exit_code == 0, result.output
    assert seams["firewall"] == []


def test_an_already_present_rule_is_reported_not_reopened(seams, monkeypatch):
    monkeypatch.setattr(firewall, "setup_firewall",
                        lambda p, pf: seams["firewall"].append((p, pf)) or "present")
    result = runner.invoke(server_cmd.app, ["provision"])
    assert result.exit_code == 0, result.output
    assert "already in place" in result.output


def test_a_failed_firewall_degrades_to_manual_instructions_with_exit_zero(seams, monkeypatch):
    monkeypatch.setattr(firewall, "setup_firewall", lambda p, pf: False)
    result = runner.invoke(server_cmd.app, ["provision"])
    assert result.exit_code == 0, "a degraded firewall must not fail the installer"
    assert "firewall-cmd" in result.output
    assert "ufw" in result.output


def test_a_dhcp_assigned_lan_ip_earns_a_static_ip_warning(seams, monkeypatch):
    monkeypatch.setattr(binding, "lan_ip_is_dhcp", lambda: True)
    result = runner.invoke(server_cmd.app, ["provision"])
    assert result.exit_code == 0, result.output
    assert "DHCP" in result.output


def test_non_linux_is_refused(seams, monkeypatch):
    monkeypatch.setattr(server_cmd.platform, "system", lambda: "Windows")
    result = runner.invoke(server_cmd.app, ["provision"])
    assert result.exit_code == 1
    assert seams["set"] == [], "a refusal must not touch the config"
