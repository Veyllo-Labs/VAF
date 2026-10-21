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
    # Whether this process could gain root is read from its own /proc; pinned so the
    # warnings do not depend on where the suite runs.
    monkeypatch.setattr(firewall, "elevation_unavailable", lambda: None)
    from vaf.core import db_credentials
    monkeypatch.setattr(db_credentials, "shipped_password_in_use", lambda: False)
    # This machine's own public addresses are read from its interfaces; none unless a test
    # says so, so the output does not depend on where the suite runs.
    monkeypatch.setattr(binding, "public_addresses", lambda: [])
    # Which host firewall runs, and the SSH session the ufw advice names, are the machine's.
    monkeypatch.setattr(firewall, "host_firewall", lambda: "ufw")
    monkeypatch.setattr(firewall, "ufw_enabled", lambda paths=None: True)
    monkeypatch.setattr(firewall, "_ufw_unit_state", lambda: "active")
    monkeypatch.setattr(firewall, "_ufw_command_reads_on", lambda paths=None: True)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
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


def test_a_switched_off_ufw_is_said_by_provision(seams, monkeypatch):
    """Rules added to a ufw that is off do nothing; provision said "Firewall opened" for any
    truthy outcome. MUTATION: fall through to the truthy branch - red."""
    monkeypatch.setattr(firewall, "setup_firewall", lambda p, pf: firewall.FIREWALL_INACTIVE)
    result = runner.invoke(server_cmd.app, ["provision"])
    assert result.exit_code == 0, result.output
    said = " ".join(result.output.split())
    assert "ufw is installed but switched off, so it does not guard port 8443" in said
    assert said.index("sudo ufw allow OpenSSH") < said.index("sudo ufw enable")
    assert "Firewall opened" not in said


@pytest.mark.parametrize("outcome", ["present", "created", True])
def test_a_firewall_with_nothing_admitted_is_not_reported_as_opened(seams, monkeypatch, outcome):
    """A fresh server with a public address starts in "VPN only", and with no VPN up nothing
    besides this machine is admitted: the setup succeeds by opening nothing, and provision
    said "Firewall rule already in place" or "Firewall opened". MUTATION: drop the empty-
    sources branch - red."""
    monkeypatch.setattr(firewall, "setup_firewall", lambda p, pf: outcome)
    monkeypatch.setattr(binding, "firewall_sources", lambda narrow_lan=False, cfg=None: [])
    result = runner.invoke(server_cmd.app, ["provision"])
    assert result.exit_code == 0, result.output
    said = " ".join(result.output.split())
    assert ("Firewall not opened for port 8443: no network besides this machine is admitted "
            "yet.") in said
    assert "sudo -v && vaf server provision" in said
    assert "Firewall opened" not in said and "already in place" not in said


@pytest.mark.parametrize("outcome", ["created", True])
@pytest.mark.parametrize("switch, unit, command_on, why", [
    (None, "active", True,
     "/etc/default/ufw or /etc/ufw/ufw.conf could not be read with certainty"),
    (True, "failed", True,
     "systemd reports its unit as failed (`systemctl status ufw` says why)"),
    (True, "active", False, "ufw's own command reads its ENABLED line as off"),
])
def test_a_ufw_that_cannot_be_confirmed_is_not_reported_as_guarding(seams, monkeypatch, outcome,
                                                                    switch, unit, command_on,
                                                                    why):
    """An uncertain switch still counts as ufw, so the allow rule is added, but nothing knows
    whether ufw runs, nor with a unit whose start failed, nor whether ufw's own command
    applied the rule at once: "Firewall opened" alone would claim a guard. MUTATION: drop the
    branch - red; take it for a confirmed switch too - red (the control)."""
    monkeypatch.setattr(firewall, "setup_firewall", lambda p, pf: outcome)
    monkeypatch.setattr(firewall, "ufw_enabled", lambda paths=None: switch)
    monkeypatch.setattr(firewall, "_ufw_unit_state", lambda: unit)
    monkeypatch.setattr(firewall, "_ufw_command_reads_on", lambda paths=None: command_on)
    said = " ".join(runner.invoke(server_cmd.app, ["provision"]).output.split())
    assert ("Firewall rule added for port 8443, but VAF could not confirm that ufw is "
            f"switched on: {why}") in said
    assert "Check with `sudo ufw status` that it is active." in said
    assert "Firewall opened" not in said

    monkeypatch.setattr(firewall, "ufw_enabled", lambda paths=None: True)
    monkeypatch.setattr(firewall, "_ufw_unit_state", lambda: "active")
    monkeypatch.setattr(firewall, "_ufw_command_reads_on", lambda paths=None: True)
    said = " ".join(runner.invoke(server_cmd.app, ["provision"]).output.split())
    assert "could not confirm" not in said
    assert "Firewall opened for port 8443" in said


@pytest.mark.parametrize("switch, unit, why", [
    (False, "active", "its switch reads as off"),
    (True, "inactive", "systemd reports its unit as inactive"),
])
def test_a_ufw_that_went_off_after_its_rule_was_added_is_not_reported_as_opened(
        seams, monkeypatch, switch, unit, why):
    """Provision read the host firewall again for its report: a ufw switched off or stopped
    after the rule went in answered "ufw_off" there, the unconfirmed check stayed silent and
    provision said "Firewall opened". It now asks about the answer the setup was taken on.
    MUTATION: read the host firewall again for the report - red."""
    reads = []

    def host_firewall():
        reads.append(1)
        return "ufw" if len(reads) == 1 else "ufw_off"

    monkeypatch.setattr(firewall, "host_firewall", host_firewall)
    monkeypatch.setattr(firewall, "ufw_enabled", lambda paths=None: switch)
    monkeypatch.setattr(firewall, "_ufw_unit_state", lambda: unit)
    said = " ".join(runner.invoke(server_cmd.app, ["provision"]).output.split())
    assert ("Firewall rule added for port 8443, but VAF could not confirm that ufw is "
            f"switched on: {why}") in said
    assert "Firewall opened" not in said
    assert len(reads) == 1


def test_the_firewall_message_asks_the_networks_the_way_the_setup_does(seams, monkeypatch):
    """firewalld opens only the LAN subnets this machine sits on, the others all of RFC 1918,
    so a machine with no LAN interface opens nothing under firewalld only. MUTATION: ask
    without narrow_lan - red."""
    asked = []
    monkeypatch.setattr(binding, "firewall_sources",
                        lambda narrow_lan=False, cfg=None: asked.append(narrow_lan) or
                        ([] if narrow_lan else ["10.0.0.0/8"]))
    monkeypatch.setattr(firewall, "host_firewall", lambda: "firewalld")
    said = " ".join(runner.invoke(server_cmd.app, ["provision"]).output.split())
    assert "Firewall not opened for port 8443" in said and asked[0] is True
    monkeypatch.setattr(firewall, "host_firewall", lambda: "ufw")
    said = " ".join(runner.invoke(server_cmd.app, ["provision"]).output.split())
    assert "Firewall opened for port 8443 (admitted networks only)." in said


def test_an_opened_port_names_the_admitted_networks(seams):
    """Under "VPN only" the opened networks are not the LAN subnet."""
    result = runner.invoke(server_cmd.app, ["provision"])
    assert "Firewall opened for port 8443 (admitted networks only)." in " ".join(result.output.split())


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

def test_provision_names_why_it_could_not_elevate(seams, monkeypatch):
    """Run inside the service (through the agent's shell, for one), no setuid helper can
    work: the warning says why instead of a bare "needs elevation", and the manual
    instructions still follow. MUTATION: drop the reason clause - red."""
    monkeypatch.setattr(firewall, "setup_firewall", lambda p, pf: False)
    monkeypatch.setattr(firewall, "elevation_unavailable",
                        lambda: "the no-new-privileges flag is set on this process, as systemd's NoNewPrivileges= sets it")
    result = runner.invoke(server_cmd.app, ["provision"])
    assert result.exit_code == 0, result.output
    said = " ".join(result.output.split())
    assert ("Could not open the OS firewall automatically (this process cannot gain root: "
            "the no-new-privileges flag is set") in said
    assert "firewall-cmd" in said and "sudo ufw allow" in said


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


_APP_SHIPPED = "postgresql://vaf_app:vaf_app_dev_secret@localhost/m"
_OWNER_SHIPPED = "postgresql://vaf:vaf_dev_secret@localhost/m"


def _provision_stopped(monkeypatch, done, key, cause, config):
    """provision with a rotation that stopped at `key`; `config` is what the DSNs say after."""
    from vaf.core import db_credentials

    def _stop():
        raise db_credentials.RotationIncomplete(done, key, cause)

    monkeypatch.setattr(db_credentials, "shipped_password_in_use", lambda: True)
    monkeypatch.setattr(db_credentials, "rotate_shipped_passwords", _stop)
    monkeypatch.setattr(Config, "get", staticmethod(lambda k, d=None: config.get(k, d)))
    result = runner.invoke(server_cmd.app, ["provision"])
    assert result.exit_code == 0, result.output
    return " ".join(result.output.split())


def test_a_database_that_cannot_be_reached_is_said_loudly(seams, monkeypatch):
    out = _provision_stopped(monkeypatch, [], "memory_db_url", ConnectionRefusedError("no database"),
                             {"memory_db_url": _APP_SHIPPED, "memory_db_owner_url": _OWNER_SHIPPED})
    assert "published default password for memory_db_url, memory_db_owner_url" in out
    assert "vaf secure rotate-db" in out


def test_a_partial_rotation_names_what_is_done_and_what_is_left(seams, monkeypatch):
    """Review finding: after the app role was replaced, one warning still said the database
    "still uses the published default password". MUTATION: drop the done line, or warn for
    every key - red."""
    out = _provision_stopped(monkeypatch, ["memory_db_url"], "memory_db_owner_url",
                             ConnectionRefusedError("owner refused"),
                             {"memory_db_url": "postgresql://vaf_app:Xq9-random@localhost/m",
                              "memory_db_owner_url": _OWNER_SHIPPED})
    assert "Replaced and verified: memory_db_url" in out
    assert "published default password for memory_db_owner_url, and" in out


def test_a_password_changed_but_not_saved_is_handed_to_the_operator(seams, monkeypatch):
    """Review finding: a verification failure was reported as "still uses the published
    default password" - false, the role's password was already new, and the connection
    string in the error is the only way back in. MUTATION: treat it like any failure - red."""
    from vaf.core import db_credentials
    cause = db_credentials.RotationUnsaved(
        "The password of the database role 'vaf' was changed, but a connection with the new one "
        "did not verify. The new connection string is: postgresql://vaf:NEW-pass@localhost/m",
        "postgresql://vaf:NEW-pass@localhost/m")
    out = _provision_stopped(monkeypatch, ["memory_db_url"], "memory_db_owner_url", cause,
                             {"memory_db_url": "postgresql://vaf_app:Xq9-random@localhost/m",
                              "memory_db_owner_url": _OWNER_SHIPPED})
    assert "postgresql://vaf:NEW-pass@localhost/m" in out
    assert "published default password" not in out


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


def test_the_rotation_quotes_the_role_keeps_the_host_and_hands_back_a_password_it_could_not_verify(monkeypatch):
    """Review finding: the role went into ALTER ROLE unquoted, the DSN was rebuilt from the
    bare hostname (an IPv6 address lost its brackets), and a new password that did not verify
    was lost: changed in the database, saved nowhere. MUTATION: drop the quoting, or the DSN
    from the error - red."""
    import asyncio
    import contextlib

    import sqlalchemy.ext.asyncio as sa_async

    from vaf.core import db_credentials
    statements, verify_fails = [], {"on": False}

    class _Conn:
        async def execute(self, stmt):
            statements.append(str(stmt))

    class _Engine:
        def __init__(self, url):
            self.url = url

        def begin(self):
            @contextlib.asynccontextmanager
            async def _ctx():
                yield _Conn()
            return _ctx()

        def connect(self):
            @contextlib.asynccontextmanager
            async def _ctx():
                if verify_fails["on"]:
                    raise ConnectionRefusedError("pg_hba says no")
                yield _Conn()
            return _ctx()

        async def dispose(self):
            pass

    monkeypatch.setattr(sa_async, "create_async_engine", lambda url, **kw: _Engine(url))
    dsn = "postgresql://my%22role:vaf_dev_secret@[::1]:5433/vaf_memory"
    rotated = asyncio.run(db_credentials.rotate_role_password(dsn))
    assert statements[0].startswith('ALTER ROLE "my""role" WITH PASSWORD ')
    assert "@[::1]:5433/vaf_memory" in rotated and rotated.startswith("postgresql://my%22role:")

    verify_fails["on"] = True
    with pytest.raises(db_credentials.RotationUnsaved) as err:
        asyncio.run(db_credentials.rotate_role_password("postgresql://vaf:vaf_dev_secret@localhost/vaf_memory"))
    assert "was changed" in str(err.value) and "postgresql://vaf:" in str(err.value)
    assert str(err.value).endswith(err.value.rotated)


def test_a_stopped_rotation_says_what_is_done_and_what_changed_unsaved(monkeypatch):
    """The two ways a role's password ends up new but unsaved: it did not verify, or the config
    refused it. Either way the error carries the connection string. MUTATION: let the
    Config.set failure escape bare, or drop `done` - red."""
    from vaf.core import db_credentials
    cfg = {"memory_db_url": _APP_SHIPPED, "memory_db_owner_url": _OWNER_SHIPPED}
    monkeypatch.setattr(Config, "get", staticmethod(lambda k, d=None: cfg.get(k, d)))
    saved = []
    monkeypatch.setattr(Config, "set", staticmethod(lambda k, v: saved.append(k)))

    async def _owner_unverified(dsn):
        if "vaf_app" in dsn:
            return "postgresql://vaf_app:NEW-app@localhost/m"
        raise db_credentials.RotationUnsaved("did not verify", "postgresql://vaf:NEW@localhost/m")

    monkeypatch.setattr(db_credentials, "rotate_role_password", _owner_unverified)
    with pytest.raises(db_credentials.RotationIncomplete) as err:
        db_credentials.rotate_shipped_passwords()
    assert err.value.done == ["memory_db_url"] and saved == ["memory_db_url"]
    assert err.value.key == "memory_db_owner_url"
    assert isinstance(err.value.cause, db_credentials.RotationUnsaved)
    assert str(err.value.cause).endswith("Save it as memory_db_owner_url in the config, then restart VAF.")

    async def _fine(dsn):
        return "postgresql://vaf_app:NEW-app@localhost/m"

    def _refuse(key, value):
        raise OSError("read-only config")

    monkeypatch.setattr(db_credentials, "rotate_role_password", _fine)
    monkeypatch.setattr(Config, "set", staticmethod(_refuse))
    with pytest.raises(db_credentials.RotationIncomplete) as err:
        db_credentials.rotate_shipped_passwords()
    assert err.value.done == [] and isinstance(err.value.cause, db_credentials.RotationUnsaved)
    assert "postgresql://vaf_app:NEW-app@localhost/m Save it as memory_db_url" in str(err.value.cause)



# --------------------------------------------------------------------------- a public address

PUBLIC = [("eth0", "9.9.9.9")]
VPN_ONLY_ON = '"VPN only" is on'


def _config(monkeypatch, **values):
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {**Config.DEFAULTS, **values}))


def _ordered(monkeypatch, seams):
    """One list for the config writes and the firewall step, to see their order."""
    order = []
    monkeypatch.setattr(Config, "set", lambda key, value: order.append((key, value)))
    monkeypatch.setattr(firewall, "setup_firewall",
                        lambda p, pf: order.append(("firewall", p)) or "created")
    return order


def test_a_first_server_setup_with_a_public_address_starts_vpn_only(seams, monkeypatch):
    """On a rented server the private ranges admitted by default are the provider's network
    and the container bridges. MUTATIONS: set VPN only regardless of a previous server
    setup; set it after the firewall step - red."""
    monkeypatch.setattr(binding, "public_addresses", lambda: PUBLIC)
    _config(monkeypatch, server_mode=False)
    order = _ordered(monkeypatch, seams)
    result = runner.invoke(server_cmd.app, ["provision"])
    assert result.exit_code == 0, result.output
    said = " ".join(result.output.split())
    assert order.index(("local_network_vpn_only", True)) < order.index(("firewall", 8443))
    assert VPN_ONLY_ON in said and "9.9.9.9 (eth0)" in said
    assert "Keep port 8443 closed in the hosting provider's firewall" in said


def test_a_rerun_never_flips_vpn_only(seams, monkeypatch):
    """A re-run is the documented step after every admission change; flipping admission on a
    server that already serves its users would lock them out."""
    monkeypatch.setattr(binding, "public_addresses", lambda: PUBLIC)
    _config(monkeypatch, server_mode=True, local_network_vpn_only=False)
    order = _ordered(monkeypatch, seams)
    result = runner.invoke(server_cmd.app, ["provision"])
    said = " ".join(result.output.split())
    assert ("local_network_vpn_only", True) not in order
    assert "`vaf server vpn-only on`" in said and VPN_ONLY_ON not in said

    _config(monkeypatch, server_mode=False, local_network_vpn_only=True)
    order = _ordered(monkeypatch, seams)
    result = runner.invoke(server_cmd.app, ["provision"])
    assert not [k for k, _v in order if k == "local_network_vpn_only"]


def test_without_a_public_address_vpn_only_is_left_alone(seams, monkeypatch):
    _config(monkeypatch, server_mode=False)
    order = _ordered(monkeypatch, seams)
    result = runner.invoke(server_cmd.app, ["provision"])
    said = " ".join(result.output.split())
    assert not [k for k, _v in order if k == "local_network_vpn_only"]
    assert VPN_ONLY_ON not in said and "vpn-only on" not in said
    assert "hosting provider" not in said


def _status_and_provision(monkeypatch, **cfg):
    _config(monkeypatch, local_network_enabled=True, local_network_tls_enabled=True, **cfg)
    status = runner.invoke(server_cmd.app, ["status"])
    provision = runner.invoke(server_cmd.app, ["provision"])
    assert status.exit_code == 0 and provision.exit_code == 0, (status.output, provision.output)
    return " ".join(status.output.split()), " ".join(provision.output.split())


def test_status_and_provision_print_one_access_list(seams, monkeypatch):
    """MUTATION: keep provision's own access loop (it lacks the stance line) - red."""
    monkeypatch.setattr(binding, "local_interfaces", lambda: [])
    stance = "Never forward the access port or publish it to the internet"
    for said in _status_and_provision(monkeypatch, server_mode=True):
        assert stance in said
        assert "public address" not in said

    monkeypatch.setattr(binding, "public_addresses", lambda: PUBLIC)
    for said in _status_and_provision(monkeypatch, server_mode=True, local_network_vpn_only=True):
        assert stance in said
        assert ("This machine has the public address 9.9.9.9 (eth0): keep port 8443 closed in "
                "the hosting provider's firewall") in said
        assert "No other device can connect yet" in said
        assert "systemctl --user restart vaf" in said


def test_vpn_only_off_names_the_provider_network(monkeypatch):
    monkeypatch.setattr(Config, "set", lambda key, value: None)
    monkeypatch.setattr(server_cmd, "_applied_note", lambda: None)
    monkeypatch.setattr(binding, "public_addresses", lambda: PUBLIC)
    said = " ".join(runner.invoke(server_cmd.app, ["vpn-only", "off"]).output.split())
    assert "9.9.9.9 (eth0)" in said and "hosting provider's own network" in said
    monkeypatch.setattr(binding, "public_addresses", lambda: [])
    said = " ".join(runner.invoke(server_cmd.app, ["vpn-only", "off"]).output.split())
    assert "hosting provider" not in said
