# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The app link's certificates: the server leaf from the local CA, the separate device CA,
the CSR check, and the one-time code sizes the pairing needs."""
import hashlib
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from tests.cert_factory import isolated_home
from vaf.core import channel_pairing
from vaf.core.app_link import pki
from vaf.network import ssl_utils


@pytest.fixture
def ssl_home(tmp_path, monkeypatch):
    ssl_dir = isolated_home(tmp_path, monkeypatch)
    pki.reset_context()
    yield ssl_dir
    pki.reset_context()


def _leaf(ssl_dir):
    chain, _key, ca_der = ssl_utils.app_link_server_files()
    certs = x509.load_pem_x509_certificates(chain.read_bytes())
    return certs, x509.load_der_x509_certificate(ca_der)


def test_leaf_names_usage_and_issuer(ssl_home):
    certs, ca = _leaf(ssl_home)
    leaf = certs[0]
    assert certs[1] == ca
    sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert sorted(sans.get_values_for_type(x509.DNSName)) == ["link.vaf.internal",
                                                              "pair.vaf.internal"]
    assert not sans.get_values_for_type(x509.IPAddress)
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert isinstance(leaf.public_key(), ec.EllipticCurvePublicKey)
    leaf.verify_directly_issued_by(ca)
    assert ca.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "VAF Local Network CA"
    # The QR's ca is the SHA-256 of that CA, the value ca_fingerprint shows in hex.
    assert pki.ca_sha256().hex() == ssl_utils.ca_fingerprint()
    # A strict client that pins the CA verifies the leaf for the link name.
    context = ssl.create_default_context(cadata=ca.public_bytes(serialization.Encoding.PEM).decode())
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    store = context.get_ca_certs()
    assert store


def test_leaf_kept_then_reissued_after_renewal(ssl_home):
    first, ca1 = _leaf(ssl_home)
    again, _ = _leaf(ssl_home)
    assert again[0] == first[0]
    ssl_utils.renew_authority(reload=False)
    renewed, ca2 = _leaf(ssl_home)
    assert ca2 != ca1 and renewed[0] != first[0]
    renewed[0].verify_directly_issued_by(ca2)


def test_leaf_from_a_foreign_signer_is_replaced(ssl_home):
    certs, ca = _leaf(ssl_home)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_key = serialization.load_pem_private_key((ssl_home / "app-link-key.pem").read_bytes(), None)
    forged = (x509.CertificateBuilder().subject_name(certs[0].subject).issuer_name(ca.subject)
              .public_key(leaf_key.public_key()).serial_number(1)
              .not_valid_before(certs[0].not_valid_before_utc)
              .not_valid_after(certs[0].not_valid_after_utc)
              .add_extension(certs[0].extensions.get_extension_for_class(
                  x509.SubjectAlternativeName).value, critical=False)
              .sign(other_key, hashes.SHA256()))
    (ssl_home / "app-link.pem").write_bytes(forged.public_bytes(serialization.Encoding.PEM))
    fixed, _ = _leaf(ssl_home)
    fixed[0].verify_directly_issued_by(ca)
    assert fixed[0] != forged


def test_device_ca_is_separate_and_client_only(ssl_home):
    _certs, server_ca = _leaf(ssl_home)
    _key, device_ca = pki.device_ca()
    assert device_ca.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != \
        server_ca.public_key().public_bytes(serialization.Encoding.DER,
                                            serialization.PublicFormat.SubjectPublicKeyInfo)
    eku = device_ca.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    assert pki.device_ca()[1] == device_ca
    if hasattr(__import__("os"), "geteuid"):
        assert (ssl_home / pki.DEVICE_CA_KEY).stat().st_mode & 0o077 == 0


def _csr(key, *, sign_with=None):
    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "phone")]))
    return builder.sign(sign_with or key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)


def test_csr_refusals_and_device_cert(ssl_home):
    key = ec.generate_private_key(ec.SECP256R1())
    public = pki.check_csr(_csr(key))
    assert pki.spki_sha256(public) == hashlib.sha256(pki.spki_der(key.public_key())).hexdigest()
    der = pki.issue_device_cert(public, "dev-1")
    cert = x509.load_der_x509_certificate(der)
    cert.verify_directly_issued_by(pki.device_ca()[1])
    assert list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == \
        [ExtendedKeyUsageOID.CLIENT_AUTH]
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "dev-1"
    with pytest.raises(pki.CsrRefused):
        pki.check_csr(_csr(ec.generate_private_key(ec.SECP384R1())))
    with pytest.raises(pki.CsrRefused):
        pki.check_csr(_csr(rsa.generate_private_key(public_exponent=65537, key_size=2048)))
    with pytest.raises(pki.CsrRefused):
        pki.check_csr(b"garbage")
    good = bytearray(_csr(key))
    good[-5] ^= 0xFF
    with pytest.raises(pki.CsrRefused):
        pki.check_csr(bytes(good))


def test_display_fingerprint_shape():
    key = ec.generate_private_key(ec.SECP256R1()).public_key()
    shown = pki.display_fingerprint(key)
    groups = shown.split(" ")
    assert len(groups) == 4 and all(len(g) == 4 and g == g.upper() for g in groups)
    assert shown.replace(" ", "").lower() == pki.spki_sha256(key)[:16]


def test_server_context_settings(ssl_home):
    context = pki.server_context()
    assert context.minimum_version == ssl.TLSVersion.TLSv1_3
    assert context.verify_mode == ssl.CERT_OPTIONAL
    assert context.num_tickets == 0
    assert pki.server_context() is context
    ssl_utils.renew_authority(reload=False)
    assert pki.server_context() is not context


# ── the one-time codes ───────────────────────────────────────────────────────

def test_pairing_code_size_and_state():
    code = channel_pairing.issue_pairing_code("veyllo_app", "scope-a", "alice", nbytes=32)
    assert len(code) == 43
    telegram = channel_pairing.issue_pairing_code("telegram", "scope-a", "alice")
    assert len(telegram) == 12
    assert channel_pairing.pairing_code_state("veyllo_app", code) == "live"
    assert channel_pairing.pairing_code_state("telegram", code) == "unknown"
    assert channel_pairing.redeem_pairing_code("veyllo_app", code) == ("scope-a", "alice")
    assert channel_pairing.pairing_code_state("veyllo_app", code) == "used"
    assert channel_pairing.redeem_pairing_code("veyllo_app", code) is None
    assert channel_pairing.pairing_code_state("veyllo_app", "x" * 43) == "unknown"
    again = channel_pairing.issue_pairing_code("veyllo_app", "scope-a", "alice", nbytes=32)
    assert channel_pairing.withdraw_pairing_code("veyllo_app", "scope-a") is True
    assert channel_pairing.pairing_code_state("veyllo_app", again) == "unknown"
    channel_pairing.withdraw_pairing_code("telegram", "scope-a")
