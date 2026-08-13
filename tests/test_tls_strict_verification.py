# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""VAF's LAN certificates must verify under STRICT X.509 checking.

Python 3.13 turns VERIFY_X509_STRICT on by default in ssl.create_default_context(), and
under it OpenSSL enforces RFC 5280's key identifiers. Certificates generated before this
was fixed verify in curl and in browsers and fail in every correctly written Python
client - including the room client, an embedder's, and anything else that does the right
thing rather than passing verify=False.

Measured before it was fixed, and the table is why both halves are here:

    CA has SKI   leaf has AKI   result
    no           no             error 85, missing authority key identifier
    yes          no             error 85
    no           yes            error 86, missing subject key identifier
    yes          yes            OK

So neither extension alone is worth anything, which is exactly the shape a partial fix
would take.
"""
import shutil
import ssl
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from vaf.network import ssl_utils

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None,
                                reason="openssl is the only strict verifier available here")


@pytest.fixture()
def generated(tmp_path):
    ca_key, ca_cert = ssl_utils._generate_ca(tmp_path)
    cert_path, _key_path = ssl_utils._generate_server_cert(tmp_path, ca_key, ca_cert)
    return tmp_path, ca_cert, cert_path


def _strict_verify(ca_pem, leaf_pem) -> str:
    result = subprocess.run(
        ["openssl", "verify", "-x509_strict", "-CAfile", str(ca_pem), str(leaf_pem)],
        capture_output=True, text=True,
    )
    return (result.stdout + result.stderr).strip()


# ── the property that matters ──────────────────────────────────────────────

def test_a_generated_chain_verifies_under_strict_checking(generated):
    """MUTATION: drop either key identifier from either certificate.

    This is the whole test. Everything below only explains why it fails when it fails.
    """
    ssl_dir, _ca_cert, cert_path = generated
    assert _strict_verify(ssl_dir / "ca.pem", cert_path).endswith("OK")


def test_the_python_default_really_is_strict():
    """The reason this file exists. If a future Python stops defaulting to strict, the
    certificates are still correct - but the urgency in the docstring above would be
    wrong, and somebody should notice."""
    context = ssl.create_default_context()
    assert bool(context.verify_flags & ssl.VERIFY_X509_STRICT)


def test_the_ca_names_its_own_key(generated):
    ssl_dir, ca_cert, _cert_path = generated
    ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)


def test_the_server_certificate_names_itself_and_its_issuer(generated):
    ssl_dir, ca_cert, cert_path = generated
    leaf = x509.load_pem_x509_certificate(cert_path.read_bytes().split(
        b"-----END CERTIFICATE-----")[0] + b"-----END CERTIFICATE-----\n")

    leaf.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
    authority = leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
    subject = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value

    assert authority.key_identifier == subject.digest, (
        "the leaf points at a key that is not the one that signed it")


# ── an unusable certificate is not a valid one ─────────────────────────────

def _legacy_ca(directory):
    """A CA exactly as VAF used to make them: correct, in date, and unverifiable."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "VAF Local Network CA"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "VAF")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    (directory / "ca.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "ca-key.pem").write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()))
    return cert


def test_a_certificate_without_the_identifiers_is_not_valid(tmp_path):
    """MUTATION: keep expiry as the only question.

    A certificate that no strict client can use is not "valid but old". Leaving it in
    place because it has nine years left would leave the machine unreachable to every
    correct client for those nine years.
    """
    _legacy_ca(tmp_path)
    assert ssl_utils._is_cert_valid(tmp_path / "ca.pem", is_ca=True) is False

    ca_key, ca_cert = ssl_utils._generate_ca(tmp_path)
    assert ssl_utils._is_cert_valid(tmp_path / "ca.pem", is_ca=True) is True


def test_a_legacy_ca_is_replaced_rather_than_reused(tmp_path, monkeypatch, caplog):
    """MUTATION: keep loading an existing CA regardless of its extensions.

    The CA is deliberately preserved across server-certificate rotations, so the only
    thing that can replace it is a check like this one - and it has to be loud, because
    every device that installed the old file has to be handed the new one.

    The REAL entry point is driven here, with the ssl directory pointed at a temporary
    home, rather than a hand-built imitation of it: a test that rebuilds the branch it
    is checking proves that the rebuild works.
    """
    import logging

    ssl_dir = tmp_path / "ssl"
    ssl_dir.mkdir()
    legacy = _legacy_ca(ssl_dir)

    monkeypatch.setattr(ssl_utils, "_get_ssl_dir", lambda: ssl_dir)
    # TLS on, or ensure_ssl_certificates returns before it looks at anything.
    stored = {"local_network_tls_enabled": True}
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "set", staticmethod(lambda k, v, *a, **kw: stored.__setitem__(k, v)))
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: stored.get(key, default)))

    with caplog.at_level(logging.WARNING):
        ssl_utils.ensure_ssl_certificates()

    replaced = x509.load_pem_x509_certificate((ssl_dir / "ca.pem").read_bytes())
    assert replaced.fingerprint(hashes.SHA256()) != legacy.fingerprint(hashes.SHA256()), (
        "the unusable CA was kept")
    assert ssl_utils._cert_has_key_identifiers(replaced, is_ca=True)
    assert any("install the new one" in r.message for r in caplog.records), (
        "the replacement was not announced, so nobody re-trusts the new CA")

    assert _strict_verify(ssl_dir / "ca.pem", ssl_dir / "server.pem").endswith("OK")


def test_a_good_ca_is_kept_across_a_run(tmp_path, monkeypatch):
    """The other direction, and the more important one for anybody who already trusts
    this machine: a CA that carries its identifier is NOT replaced."""
    ssl_dir = tmp_path / "ssl"
    ssl_dir.mkdir()
    monkeypatch.setattr(ssl_utils, "_get_ssl_dir", lambda: ssl_dir)
    # TLS on, or ensure_ssl_certificates returns before it looks at anything.
    stored = {"local_network_tls_enabled": True}
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "set", staticmethod(lambda k, v, *a, **kw: stored.__setitem__(k, v)))
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: stored.get(key, default)))

    ssl_utils.ensure_ssl_certificates()
    first = x509.load_pem_x509_certificate((ssl_dir / "ca.pem").read_bytes())
    ssl_utils.ensure_ssl_certificates()
    second = x509.load_pem_x509_certificate((ssl_dir / "ca.pem").read_bytes())

    assert first.fingerprint(hashes.SHA256()) == second.fingerprint(hashes.SHA256())


def test_the_check_distinguishes_a_ca_from_a_leaf():
    """A CA needs to name its own key; a leaf needs to name its issuer as well. Asking
    a CA for an authority identifier it has no reason to carry would regenerate a
    perfectly good one on every start."""
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa

    key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ca")])
    now = datetime.now(timezone.utc)
    ca_only_ski = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(1)
                   .not_valid_before(now).not_valid_after(now + timedelta(days=10))
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                                  critical=False)
                   .sign(key, hashes.SHA256()))

    assert ssl_utils._cert_has_key_identifiers(ca_only_ski, is_ca=True) is True
    assert ssl_utils._cert_has_key_identifiers(ca_only_ski, is_ca=False) is False
