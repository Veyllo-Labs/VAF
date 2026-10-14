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
    # The manual firewall instructions read the admitted networks; a fixed answer keeps the
    # degraded-firewall tests from depending on what the host's interfaces happen to be.
    monkeypatch.setattr(binding, "firewall_sources", lambda narrow_lan=False, cfg=None: ["192.168.1.0/24"])
    monkeypatch.setattr(firewall, "setup_firewall",
                        lambda p, pf: calls["firewall"].append((p, pf)) or "created")
    monkeypatch.setattr(server_cmd.platform, "system", lambda: "Linux")
    from vaf.core import db_credentials
    monkeypatch.setattr(db_credentials, "shipped_password_in_use", lambda: False)
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



def test_the_manual_instructions_name_every_admitted_network(seams, monkeypatch):
    """One firewalld rule and one ufw rule per admitted network, not just the first.
    MUTATION: print the ufw rule for sources[0] only - red."""
    monkeypatch.setattr(firewall, "setup_firewall", lambda p, pf: False)
    monkeypatch.setattr(binding, "firewall_sources",
                        lambda narrow_lan=False, cfg=None: ["10.8.0.0/24", "192.168.1.0/24"])
    result = runner.invoke(server_cmd.app, ["provision"])
    assert result.exit_code == 0, result.output
    said = " ".join(result.output.split())
    for source in ("10.8.0.0/24", "192.168.1.0/24"):
        assert f'source address="{source}" port port="8443"' in said
        assert f"sudo ufw allow from {source} to any port 8443 proto tcp" in said

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


def test_no_admitted_network_means_no_example_rule(seams, monkeypatch):
    """With nothing admitted besides this machine ("VPN only" with no VPN up) there is no
    rule worth suggesting; an example network would open the port to devices that are not
    admitted. MUTATION: fall back to 192.168.1.0/24 again - red."""
    monkeypatch.setattr(firewall, "setup_firewall", lambda p, pf: False)
    monkeypatch.setattr(binding, "firewall_sources", lambda narrow_lan=False, cfg=None: [])
    result = runner.invoke(server_cmd.app, ["provision"])
    assert result.exit_code == 0, result.output
    said = " ".join(result.output.split())
    assert "192.168.1.0/24" not in said and "firewall-cmd --permanent" not in said
    assert "No admitted network could be determined" in said


def test_provision_replaces_the_published_database_passwords(seams, monkeypatch):
    """A server must not keep them: whoever has one writes every table, the accounts included.
    MUTATION: drop the rotation from provision - red."""
    from vaf.core import db_credentials
    rotated = []
    monkeypatch.setattr(db_credentials, "shipped_password_in_use", lambda: True)
    monkeypatch.setattr(db_credentials, "rotate_shipped_passwords",
                        lambda: rotated.append(1) or ["memory_db_url", "memory_db_owner_url"])
    result = runner.invoke(server_cmd.app, ["provision"])
    assert result.exit_code == 0, result.output
    assert rotated == [1] and "replaced with random ones" in result.output


def test_a_database_that_cannot_be_reached_is_said_loudly(seams, monkeypatch):
    from vaf.core import db_credentials

    def _down():
        raise ConnectionRefusedError("no database")

    monkeypatch.setattr(db_credentials, "shipped_password_in_use", lambda: True)
    monkeypatch.setattr(db_credentials, "rotate_shipped_passwords", _down)
    result = runner.invoke(server_cmd.app, ["provision"])
    assert result.exit_code == 0 and "vaf secure rotate-db" in result.output


def test_the_shipped_password_check_reads_both_roles(monkeypatch):
    """The owner role is the stronger one; checking only the app DSN read "all clear".
    MUTATION: read only memory_db_url - red."""
    from vaf.core import db_credentials
    cfg = {"memory_db_url": "postgresql://vaf_app:Xq9-random@localhost/vaf_memory",
           "memory_db_owner_url": "postgresql://vaf:vaf_dev_secret@localhost/vaf_memory"}
    monkeypatch.setattr(Config, "get", staticmethod(lambda k, d=None: cfg.get(k, d)))
    assert db_credentials.shipped_password_in_use()
    cfg["memory_db_owner_url"] = "postgresql://vaf:Zz8-random@localhost/vaf_memory"
    assert not db_credentials.shipped_password_in_use()

