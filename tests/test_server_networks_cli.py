# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf server networks` and `vaf server vpn-only`: the CLI half of the remote-access
settings. Same two keys, same check as the web UI (binding.normalize_allowed_networks)."""
import pytest
from typer.testing import CliRunner

import vaf.network.binding as binding
from vaf.cli.cmd import server as server_cmd
from vaf.core.config import Config

runner = CliRunner()

LAN = binding.LocalInterface("enp3s0", "192.168.2.10", "192.168.2.0/24", "lan")
WG = binding.LocalInterface("wg0", "10.8.0.1", "10.8.0.0/24", "vpn")
TS = binding.LocalInterface("tailscale0", "100.101.102.103", "100.64.0.0/10", "vpn")


@pytest.fixture
def settings(monkeypatch):
    values = {"local_network_enabled": True, "local_network_tls_enabled": True}
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, k, d=None: values.get(k, Config.DEFAULTS.get(k) if d is None else d)))
    monkeypatch.setattr(Config, "set", classmethod(lambda cls, k, v: values.__setitem__(k, v)))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {**Config.DEFAULTS, **values}))
    monkeypatch.setattr(binding, "local_interfaces", lambda: [LAN, WG, TS])
    monkeypatch.setattr(binding, "resolve_lan_access_ports",
                        lambda wait_for_proxy=False: (8443, 8001))
    return values


def test_allow_takes_a_private_network_normalized(settings):
    result = runner.invoke(server_cmd.app, ["networks", "allow", "10.9.0.7/24"])
    assert result.exit_code == 0, result.output
    assert settings["local_network_allowed_networks"] == ["10.9.0.0/24"]
    again = runner.invoke(server_cmd.app, ["networks", "allow", "10.9.0.0/24"])
    assert again.exit_code == 0 and "already" in _said(again)
    assert settings["local_network_allowed_networks"] == ["10.9.0.0/24"]


@pytest.mark.parametrize("entry, words", [
    ("8.8.8.0/24", "not a private network"),
    ("0.0.0.0/0", "every address"),
    ("fd7a:115c:a1e0::/48", "IPv6"),
    ("nonsense", "not an IPv4"),
])
def test_allow_refuses_what_is_not_a_private_network(settings, entry, words):
    """MUTATION: store the entry before checking it - red."""
    result = runner.invoke(server_cmd.app, ["networks", "allow", entry])
    assert result.exit_code == 1
    assert words in _said(result)
    assert "local_network_allowed_networks" not in settings


def test_remove_drops_the_entry_and_refuses_an_unknown_one(settings):
    settings["local_network_allowed_networks"] = ["10.9.0.0/24", "100.64.0.0/10"]
    result = runner.invoke(server_cmd.app, ["networks", "remove", "10.9.0.9/24"])
    assert result.exit_code == 0, result.output
    assert settings["local_network_allowed_networks"] == ["100.64.0.0/10"]
    assert runner.invoke(server_cmd.app, ["networks", "remove", "10.9.0.0/24"]).exit_code == 1


def test_the_tailscale_switch_adds_and_removes_the_shared_address_space(settings):
    assert runner.invoke(server_cmd.app, ["networks", "tailscale", "on"]).exit_code == 0
    assert settings["local_network_allowed_networks"] == ["100.64.0.0/10"]
    assert runner.invoke(server_cmd.app, ["networks", "tailscale", "on"]).exit_code == 0
    assert settings["local_network_allowed_networks"] == ["100.64.0.0/10"]
    assert runner.invoke(server_cmd.app, ["networks", "tailscale", "off"]).exit_code == 0
    assert settings["local_network_allowed_networks"] == []
    assert runner.invoke(server_cmd.app, ["networks", "tailscale", "maybe"]).exit_code == 2


def test_vpn_only_names_who_is_locked_out(settings):
    result = runner.invoke(server_cmd.app, ["vpn-only", "on"])
    assert result.exit_code == 0, result.output
    assert settings["local_network_vpn_only"] is True
    said = _said(result)
    assert "192.168.2.0/24" in said and "no longer connect" in said
    assert "10.8.0.0/24" in said


def test_vpn_only_without_a_vpn_warns_that_nobody_gets_in(settings, monkeypatch):
    monkeypatch.setattr(binding, "local_interfaces", lambda: [LAN])
    result = runner.invoke(server_cmd.app, ["vpn-only", "on"])
    assert result.exit_code == 0
    assert "No VPN interface is up" in _said(result)


def _said(result) -> str:
    """The output with the console's line wrapping undone."""
    return " ".join(result.output.split())


def test_status_shows_the_interfaces_with_their_access_url(settings):
    result = runner.invoke(server_cmd.app, ["status"])
    assert result.exit_code == 0, result.output
    said = _said(result)
    assert "https://192.168.2.10:8443" in said
    assert "https://10.8.0.1:8443" in said
    # Tailscale is not admitted yet, and the status says how to change that.
    assert "100.101.102.103 not admitted" in said
    assert "networks tailscale on" in said

    settings["local_network_allowed_networks"] = ["100.64.0.0/10", "8.8.8.8"]
    said = _said(runner.invoke(server_cmd.app, ["status"]))
    assert "https://100.101.102.103:8443" in said
    assert "Ignored entry 8.8.8.8" in said


def test_list_reports_the_vpn_networks_under_vpn_only(settings):
    settings["local_network_vpn_only"] = True
    result = runner.invoke(server_cmd.app, ["networks", "list"])
    assert result.exit_code == 0, result.output
    said = _said(result)
    assert "10.8.0.0/24" in said and "100.64.0.0/10" in said
    assert "local networks" not in said
