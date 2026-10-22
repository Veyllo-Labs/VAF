# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf doctor` on the served certificate and the local authority.

Four findings, all read from `ssl_utils.certificate_state`, which creates and writes
nothing: an own certificate that expires soon or has expired (it is served all the same),
one that cannot be used (VAF's own is served instead), and a local CA that may vouch for
any name. None of them while Local Network is off.
"""
import pytest

import vaf.network.binding as binding
from cert_factory import isolated_home, key_pem, make_ca, make_key, make_leaf, pem
from vaf.core.config import Config
from vaf.core.security_misconfig import collect_security_findings
from vaf.network import ssl_utils

CODES = ("network_certificate_expiring", "network_certificate_expired",
         "network_certificate_unusable", "network_ca_unrestricted")


@pytest.fixture
def machine(monkeypatch):
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [("enp3s0", "192.168.1.20")])
    monkeypatch.setattr(binding, "local_interfaces", lambda: [])
    monkeypatch.setattr(binding, "public_addresses", lambda: [])
    monkeypatch.setattr(binding, "machine_dns_names", lambda: ["vafhost"])


def _ours(cfg=None):
    found = collect_security_findings(cfg if cfg is not None else Config.load())
    return {f["code"]: f["severity"] for f in found if f["code"] in CODES}


def _own(tmp_path, monkeypatch, *, days=200, key=None, network=True):
    root, root_key = make_ca()
    leaf, leaf_key = make_leaf(root, root_key, days=days)
    (tmp_path / "own.pem").write_bytes(pem(leaf, root))
    (tmp_path / "own.key").write_bytes(key_pem(key or leaf_key))
    return isolated_home(tmp_path, monkeypatch, local_network_enabled=network,
                         local_network_tls_enabled=True, local_network_firewall_enabled=False,
                         local_network_ssl_cert=str(tmp_path / "own.pem"),
                         local_network_ssl_key=str(tmp_path / "own.key"))


@pytest.mark.parametrize("days, code, severity", [
    (12, "network_certificate_expiring", "medium"),
    (-3, "network_certificate_expired", "high"),
])
def test_an_own_certificate_near_or_past_its_end(tmp_path, monkeypatch, machine, days, code,
                                                 severity):
    """MUTATION: move the 30-day threshold (to 10 days) - red for 12 days."""
    _own(tmp_path, monkeypatch, days=days)
    assert _ours() == {code: severity}


def test_a_healthy_own_certificate_is_quiet(tmp_path, monkeypatch, machine):
    _own(tmp_path, monkeypatch, days=200)
    assert _ours() == {}


def test_an_unusable_own_certificate(tmp_path, monkeypatch, machine):
    ssl_dir = _own(tmp_path, monkeypatch, key=make_key())
    ssl_utils.ensure_ssl_certificates()            # VAF's own, restricted, is served now
    found = _ours()
    assert found == {"network_certificate_unusable": "high"}
    assert ssl_utils.served_certificate().problem == "key_mismatch"
    assert (ssl_dir / "server.pem").exists()


def test_an_unrestricted_local_authority(tmp_path, monkeypatch, machine):
    from cryptography.hazmat.primitives import serialization
    ssl_dir = isolated_home(tmp_path, monkeypatch, local_network_enabled=True,
                            local_network_tls_enabled=True, local_network_firewall_enabled=False)
    ssl_dir.mkdir(parents=True)
    old, old_key = make_ca("VAF Local Network CA")
    (ssl_dir / "ca.pem").write_bytes(pem(old))
    (ssl_dir / "ca-key.pem").write_bytes(old_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    ssl_utils.ensure_ssl_certificates()
    assert _ours() == {"network_ca_unrestricted": "low"}
    ssl_utils.renew_authority()
    assert _ours() == {}


def test_nothing_while_local_network_is_off(tmp_path, monkeypatch, machine):
    _own(tmp_path, monkeypatch, days=-3, network=False)
    assert _ours() == {}


def test_the_check_is_read_only(tmp_path, monkeypatch, machine):
    """A home without an ssl folder still has none afterwards. MUTATION: let the state
    create the ssl folder (_get_ssl_dir in served_certificate) - red."""
    ssl_dir = isolated_home(tmp_path, monkeypatch, local_network_enabled=True,
                            local_network_tls_enabled=True, local_network_firewall_enabled=False)
    before = Config.CONFIG_FILE.read_bytes()
    assert _ours() == {}
    assert not ssl_dir.exists()
    assert Config.CONFIG_FILE.read_bytes() == before


def test_with_tls_off_only_the_authority_is_reported(tmp_path, monkeypatch, machine):
    """No listener serves a certificate then (network_tls_disabled is that finding), but the
    devices that installed the local CA trust it all the same, so an unrestricted one is
    still reported. MUTATION: gate the authority finding on TLS too - red."""
    from cryptography.hazmat.primitives import serialization
    ssl_dir = isolated_home(tmp_path, monkeypatch, local_network_enabled=True,
                            local_network_tls_enabled=True, local_network_firewall_enabled=False)
    ssl_dir.mkdir(parents=True)
    old, old_key = make_ca("VAF Local Network CA")
    (ssl_dir / "ca.pem").write_bytes(pem(old))
    (ssl_dir / "ca-key.pem").write_bytes(old_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    assert _ours() == {"network_ca_unrestricted": "low"}
    cfg = dict(Config.load(), local_network_tls_enabled=False)
    assert _ours(cfg) == {"network_ca_unrestricted": "low"}
    own_tmp = tmp_path / "own"
    own_tmp.mkdir()
    _own(own_tmp, monkeypatch, days=-3)
    ssl_utils.ensure_ssl_certificates()
    assert "network_certificate_expired" in _ours()
    cfg = dict(Config.load(), local_network_tls_enabled=False)
    assert "network_certificate_expired" not in _ours(cfg)


def test_the_unrestricted_authority_finding_names_what_a_renewal_leaves_out(tmp_path,
                                                                            monkeypatch,
                                                                            machine):
    """MUTATION: drop the names from the finding - red."""
    from cryptography.hazmat.primitives import serialization
    monkeypatch.setattr(binding, "machine_dns_names", lambda: ["mypc", "mypc.fritz.box"])
    ssl_dir = isolated_home(tmp_path, monkeypatch, local_network_enabled=True,
                            local_network_tls_enabled=True, local_network_firewall_enabled=False)
    ssl_dir.mkdir(parents=True)
    old, old_key = make_ca("VAF Local Network CA")
    (ssl_dir / "ca.pem").write_bytes(pem(old))
    (ssl_dir / "ca-key.pem").write_bytes(old_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    found = [f for f in collect_security_findings(Config.load())
             if f["code"] == "network_ca_unrestricted"]
    assert found and "mypc, mypc.fritz.box" in found[0]["message"]
    assert "public sites" in found[0]["message"]
