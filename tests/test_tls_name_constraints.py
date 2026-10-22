# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The local authority can only vouch for private names, and an existing one is kept.

A device that installs VAF's CA trusts everything that CA signs. Without name constraints a
copy of its key (the key is an unencrypted owner-only file) could sign a certificate for any
bank's name, and that device would accept it. A new CA carries critical NameConstraints
built from the ranges the admission check serves and the private DNS names; measured under
strict verification in OpenSSL and Python. An existing CA is kept as it is, so every device
that installed it keeps working; `renew_authority` replaces it when the person chooses.
"""
import ipaddress
import logging
import shutil
import ssl
import subprocess

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from cert_factory import (handshake, isolated_home, make_ca, make_key, make_leaf, pem,
                          server_context, strict_client)
from vaf.network import ssl_utils

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None,
                                   reason="openssl is the second strict verifier")

TLS_ON = {"local_network_tls_enabled": True}


@pytest.fixture
def host(monkeypatch):
    """This machine is "vafhost" with no FQDN and one LAN address."""
    import vaf.network.binding as binding
    names = ["vafhost"]
    monkeypatch.setattr(binding, "machine_dns_names", lambda: list(names))
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [("enp3s0", "192.168.1.20")])
    monkeypatch.setattr(binding, "local_interfaces", lambda: [])
    return names


def _ca_files(ssl_dir):
    key = serialization.load_pem_private_key((ssl_dir / "ca-key.pem").read_bytes(), None)
    cert = x509.load_pem_x509_certificate((ssl_dir / "ca.pem").read_bytes())
    return cert, key


def _openssl_strict(ca_file, leaf_file) -> str:
    result = subprocess.run(["openssl", "verify", "-x509_strict", "-CAfile", str(ca_file),
                             str(leaf_file)], capture_output=True, text=True)
    return (result.stdout + result.stderr).strip()


def _foreign_leaf(tmp_path, ca_cert, ca_key, name):
    leaf, key = make_leaf(ca_cert, ca_key, names=(name,))
    leaf_file = tmp_path / f"foreign-{name}.pem"
    key_file = tmp_path / f"foreign-{name}.key"
    leaf_file.write_bytes(pem(leaf))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return leaf_file, key_file


@needs_openssl
def test_a_new_ca_refuses_foreign_names(tmp_path, host):
    """AC 4. MUTATION: drop the NameConstraints extension - red (both foreign leaves verify);
    drop the DNS subtrees only - red (the bank's name verifies)."""
    ssl_dir = tmp_path / "ssl"
    ssl_dir.mkdir()
    ca_key, ca_cert = ssl_utils._generate_ca(ssl_dir)
    own_cert, own_key = ssl_utils._generate_server_cert(ssl_dir, ca_key, ca_cert)
    ca_pem = (ssl_dir / "ca.pem").read_bytes()

    assert _openssl_strict(ssl_dir / "ca.pem", own_cert).endswith("OK")
    handshake(server_context(own_cert, own_key), strict_client(ca_pem))

    for name in ("www.examplebank.com", "203.0.113.9"):
        leaf_file, key_file = _foreign_leaf(tmp_path, ca_cert, ca_key, name)
        assert "permitted subtree violation" in _openssl_strict(ssl_dir / "ca.pem", leaf_file)
        with pytest.raises(ssl.SSLCertVerificationError) as refused:
            handshake(server_context(leaf_file, key_file), strict_client(ca_pem))
        assert "permitted subtree violation" in str(refused.value)


def test_the_constraints_come_from_the_admission_ranges(tmp_path, host):
    """Both name types are listed (a type without permitted subtrees is open, RFC 5280), the
    extension is critical, and the address list is the admission's own constants."""
    from vaf.network.binding import CGNAT_RANGE, LOCALHOST_RANGES, PRIVATE_RANGES
    ssl_dir = tmp_path / "ssl"
    ssl_dir.mkdir()
    _key, cert = ssl_utils._generate_ca(ssl_dir)
    ext = cert.extensions.get_extension_for_class(x509.NameConstraints)
    assert ext.critical
    permitted = ext.value.permitted_subtrees
    networks = [s.value for s in permitted if isinstance(s, x509.IPAddress)]
    names = [s.value for s in permitted if isinstance(s, x509.DNSName)]
    assert networks == [*PRIVATE_RANGES, CGNAT_RANGE, *LOCALHOST_RANGES,
                        ipaddress.ip_network("::1/128")]
    assert names == ["localhost", "local", "home.arpa", "internal", "lan"]
    assert ext.value.excluded_subtrees is None


@needs_openssl
@pytest.mark.parametrize("host, foreign", [("studio", "www.google.studio"),
                                           ("example.com", "www.google.example.com")])
def test_a_host_named_like_a_public_domain_permits_no_public_name(tmp_path, host, foreign,
                                                                  monkeypatch):
    """A DNS constraint permits every name below it. With the machine's own names in the
    constraints, a host called "studio" (a public TLD) let the CA vouch for
    www.google.studio, and a server named after its domain for www.google.example.com
    (measured, OpenSSL and Python strict). MUTATION: put machine_dns_names() back into the
    permitted names - red."""
    import vaf.network.binding as binding
    monkeypatch.setattr(binding, "machine_dns_names", lambda: [host])
    ssl_dir = tmp_path / "ssl"
    ssl_dir.mkdir()
    ca_key, ca_cert = ssl_utils._generate_ca(ssl_dir)
    leaf_file, key_file = _foreign_leaf(tmp_path, ca_cert, ca_key, foreign)
    assert "permitted subtree violation" in _openssl_strict(ssl_dir / "ca.pem", leaf_file)
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(server_context(leaf_file, key_file),
                  strict_client((ssl_dir / "ca.pem").read_bytes()))


def test_an_existing_unrestricted_ca_is_kept(tmp_path, monkeypatch, host):
    """AC 5 and the second half of AC 6: devices that installed it keep working, and the
    certificate it issues carries today's names. MUTATION: regenerate an unrestricted CA at
    the start - red."""
    ssl_dir = isolated_home(tmp_path, monkeypatch, **TLS_ON)
    ssl_dir.mkdir(parents=True)
    old, old_key = make_ca("VAF Local Network CA")
    (ssl_dir / "ca.pem").write_bytes(pem(old))
    (ssl_dir / "ca-key.pem").write_bytes(old_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    host[:] = ["renamed-box"]

    ssl_utils.ensure_ssl_certificates()
    (ssl_dir / "server.pem").unlink()          # a second issue from the same CA
    ssl_utils.ensure_ssl_certificates()

    assert ssl_utils.ca_fingerprint() == old.fingerprint(ssl_utils.hashes.SHA256()).hex()
    assert ssl_utils.local_authority()["restricted"] is False
    leaf = ssl_utils._leaf_of(str(ssl_dir / "server.pem"))
    dns = leaf.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert dns == ["localhost", "renamed-box", "renamed-box.local"], (
        "an unrestricted CA issues today's names, and the mDNS name with them")


@needs_openssl
def test_the_leaf_stays_inside_the_constraints(tmp_path, monkeypatch, host, caplog):
    """AC 6. The machine's host name is outside a restricted CA's subtrees (only names under
    local, home.arpa, internal and lan are in), and one name outside makes a strict verifier
    refuse the WHOLE certificate, so the leaf leaves it out, at WARNING.
    MUTATION: remove the SAN filter - red (strict verification fails)."""
    ssl_dir = isolated_home(tmp_path, monkeypatch, **TLS_ON)
    host[:] = ["renamed-box", "vafhost.lan"]

    with caplog.at_level(logging.WARNING, logger="vaf.network.ssl_utils"):
        ssl_utils.ensure_ssl_certificates()

    leaf = ssl_utils._leaf_of(str(ssl_dir / "server.pem"))
    dns = leaf.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert dns == ["localhost", "vafhost.lan", "renamed-box.local"], (
        "a name under lan stays, the bare host name goes, its mDNS name stays")
    warning = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
               and "renamed-box" in r.getMessage()]
    assert warning and "or by renamed-box.local" in warning[0]
    assert "localhost, local, home.arpa, internal, lan" in warning[0], (
        "the warning names what the CA in use vouches for")
    assert _openssl_strict(ssl_dir / "ca.pem", ssl_dir / "server.pem").endswith("OK")


def test_a_torn_ca_pair_is_replaced(tmp_path, monkeypatch, host, caplog):
    """AC 7. MUTATION: remove the key-belongs-to-the-certificate check - red."""
    ssl_dir = isolated_home(tmp_path, monkeypatch, **TLS_ON)
    ssl_dir.mkdir(parents=True)
    old, _right_key = make_ca("VAF Local Network CA")
    (ssl_dir / "ca.pem").write_bytes(pem(old))
    (ssl_dir / "ca-key.pem").write_bytes(make_key().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))

    with caplog.at_level(logging.WARNING, logger="vaf.network.ssl_utils"):
        ssl_utils.ensure_ssl_certificates()

    cert, key = _ca_files(ssl_dir)
    assert cert.fingerprint(ssl_utils.hashes.SHA256()) != old.fingerprint(
        ssl_utils.hashes.SHA256())
    assert ssl_utils._key_belongs_to(cert, key)
    assert ssl_utils.local_authority()["restricted"] is True
    assert any("install the new one" in r.getMessage() for r in caplog.records)


def test_only_one_half_of_the_ca_is_replaced_loudly(tmp_path, monkeypatch, host, caplog):
    ssl_dir = isolated_home(tmp_path, monkeypatch, **TLS_ON)
    ssl_dir.mkdir(parents=True)
    old, _key = make_ca("VAF Local Network CA")
    (ssl_dir / "ca.pem").write_bytes(pem(old))
    with caplog.at_level(logging.WARNING, logger="vaf.network.ssl_utils"):
        ssl_utils.ensure_ssl_certificates()
    assert (ssl_dir / "ca-key.pem").exists()
    assert any("install the new one" in r.getMessage() for r in caplog.records)


@needs_openssl
def test_renew_authority(tmp_path, monkeypatch, host):
    """AC 14. MUTATION: keep the old ca-key.pem (sign the new leaf with it) - red."""
    import vaf.core.security_events as se
    events = []
    monkeypatch.setattr(se, "log_security_event", lambda kind, **kw: events.append((kind, kw)))
    ssl_dir = isolated_home(tmp_path, monkeypatch, **TLS_ON)
    ssl_dir.mkdir(parents=True)
    old, old_key = make_ca("VAF Local Network CA")
    old_key_pem = old_key.private_bytes(serialization.Encoding.PEM,
                                        serialization.PrivateFormat.TraditionalOpenSSL,
                                        serialization.NoEncryption())
    (ssl_dir / "ca.pem").write_bytes(pem(old))
    (ssl_dir / "ca-key.pem").write_bytes(old_key_pem)
    ssl_utils.ensure_ssl_certificates()
    old_fp = ssl_utils.ca_fingerprint()

    result = ssl_utils.renew_authority(username="admin", via="in a terminal on this machine")

    assert result.old_authority == old_fp and result.new_authority == ssl_utils.ca_fingerprint()
    assert result.new_authority != old_fp
    assert (ssl_dir / "ca-key.pem").read_bytes() != old_key_pem
    assert ssl_utils.local_authority()["restricted"] is True
    assert _openssl_strict(ssl_dir / "ca.pem", ssl_dir / "server.pem").endswith("OK")
    assert ssl_utils.served_authority()[1] == result.new_authority
    assert [k for k, _ in events] == ["tls_certificate_changed"]
    assert old_fp[:16] in events[0][1]["detail"] and result.new_authority[:16] in events[0][1]["detail"]


def test_renewal_keeps_an_own_certificate_served(tmp_path, monkeypatch, host):
    from cert_factory import key_pem, own_chain
    ssl_dir = isolated_home(tmp_path, monkeypatch, **TLS_ON)
    leaf, key, inter, root = own_chain(names=("vafhost", "192.168.1.20"))
    ssl_utils.install_certificate(pem(leaf, inter, root), key_pem(key))
    served = ssl_utils.served_certificate()

    result = ssl_utils.renew_authority()

    assert ssl_utils.served_certificate() == served and served.source == "own"
    assert result.reload is None
    assert (ssl_dir / "server.pem").exists()
