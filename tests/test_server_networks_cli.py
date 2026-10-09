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


def test_vpn_only_spares_a_lan_your_own_entry_still_admits(settings, monkeypatch):
    """Under "VPN only" a LAN an entry covers keeps its devices, and one covered in part
    keeps exactly those addresses; the warning must not claim the whole LAN is out.
    MUTATION: warn for every LAN regardless of the entries - red."""
    monkeypatch.setattr(binding, "local_interfaces", lambda: [LAN, WG])
    settings["local_network_allowed_networks"] = ["192.168.2.0/24"]
    said = _said(runner.invoke(server_cmd.app, ["vpn-only", "on"]))
    assert "no longer connect" not in said

    settings["local_network_vpn_only"] = False
    settings["local_network_allowed_networks"] = ["192.168.2.50/32"]
    said = _said(runner.invoke(server_cmd.app, ["vpn-only", "on"]))
    assert "192.168.2.0/24 can no longer connect, except the addresses your own entries admit" in said
    assert "192.168.2.50/32" in said


def test_an_entry_change_reads_and_writes_under_one_lock(settings, monkeypatch):
    """`networks allow` must not read the list, lose the lock, and write back a list that
    misses what another admin stored in between. MUTATION: read outside the lock - red."""
    import contextlib
    held = {"now": False}
    reads_outside = []

    @contextlib.contextmanager
    def locked(cls):
        held["now"] = True
        try:
            yield
        finally:
            held["now"] = False

    real_get = Config.get

    def get(cls, key, default=None):
        if key == "local_network_allowed_networks" and not held["now"]:
            reads_outside.append(key)
        return real_get(key, default)

    monkeypatch.setattr(Config, "_locked", classmethod(locked))
    monkeypatch.setattr(Config, "get", classmethod(get))
    for args in (["networks", "allow", "10.9.0.0/24"], ["networks", "remove", "10.9.0.0/24"],
                 ["networks", "tailscale", "on"]):
        assert runner.invoke(server_cmd.app, args).exit_code == 0
    assert reads_outside == []
    assert settings["local_network_allowed_networks"] == ["100.64.0.0/10"]


def test_removing_an_entry_says_when_its_range_stays_admitted(settings):
    """An entry inside a network that is admitted anyway (the local networks, a detected VPN
    under "VPN only") is gone from the list, not from the admitted devices.
    MUTATION: always report "No longer admitting" - red."""
    settings["local_network_allowed_networks"] = ["10.20.0.0/16", "100.70.0.0/16"]
    said = _said(runner.invoke(server_cmd.app, ["networks", "remove", "10.20.0.0/16"]))
    assert "Removed 10.20.0.0/16 from your entries" in said
    assert "stays admitted: it lies inside 10.0.0.0/8" in said
    said = _said(runner.invoke(server_cmd.app, ["networks", "remove", "100.70.0.0/16"]))
    assert "No longer admitting 100.70.0.0/16" in said

    settings["local_network_vpn_only"] = True
    settings["local_network_allowed_networks"] = ["10.8.0.0/24"]
    said = _said(runner.invoke(server_cmd.app, ["networks", "remove", "10.8.0.0/24"]))
    assert "stays admitted: it lies inside 10.8.0.0/24" in said


def test_tailscale_off_under_vpn_only_says_it_stays_admitted(settings):
    """Under "VPN only" a detected Tailscale interface admits 100.64.0.0/10 by itself, so
    switching the entry off does not lock those devices out. MUTATION: report "not
    admitted" regardless - red."""
    settings["local_network_vpn_only"] = True
    settings["local_network_allowed_networks"] = ["100.64.0.0/10"]
    said = _said(runner.invoke(server_cmd.app, ["networks", "tailscale", "off"]))
    assert "stays admitted while VPN only is on" in said
    assert settings["local_network_allowed_networks"] == []

    settings["local_network_vpn_only"] = False
    settings["local_network_allowed_networks"] = ["100.64.0.0/10"]
    said = _said(runner.invoke(server_cmd.app, ["networks", "tailscale", "off"]))
    assert "(100.64.0.0/10) not admitted" in said


def test_the_partial_lan_warning_names_only_the_entries_that_touch_it(settings, monkeypatch):
    """An unrelated entry (Tailscale's range) is no exception for the home network.
    MUTATION: list every entry again - red."""
    monkeypatch.setattr(binding, "local_interfaces", lambda: [LAN, WG])
    settings["local_network_allowed_networks"] = ["192.168.2.50/32", "100.64.0.0/10"]
    said = _said(runner.invoke(server_cmd.app, ["vpn-only", "on"]))
    assert "your own entries admit (192.168.2.50/32)" in said
