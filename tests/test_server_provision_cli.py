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

