# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Certificates minted in the test, for the tests of the served certificate.

Nothing here reads a file from disk: every authority, intermediate and leaf is made with
`cryptography` on the spot, so a test states exactly what it serves. Also the two harnesses
those tests share: a configuration and ssl directory of their own (`isolated_home`), and a
TLS handshake held in memory (`handshake`), which needs no port and no thread.
"""
import ipaddress
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def make_key(kind: str = "rsa"):
    if kind == "rsa":
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if kind == "rsa1024":
        return rsa.generate_private_key(public_exponent=65537, key_size=1024)
    if kind == "ec256":
        return ec.generate_private_key(ec.SECP256R1())
    if kind == "ec521":
        return ec.generate_private_key(ec.SECP521R1())
    if kind == "ed25519":
        return ed25519.Ed25519PrivateKey.generate()
    raise ValueError(kind)


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def _general(name: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        return x509.DNSName(name)


def make_ca(common: str = "Test Root", *, issuer=None, issuer_key=None, key=None,
            ski: bool = True, constraints=None, days: int = 3650):
    """An authority: a root when no issuer is given, else an intermediate signed by it."""
    key = key or make_key()
    now = datetime.now(timezone.utc)
    signer_key = issuer_key or key
    builder = (x509.CertificateBuilder()
               .subject_name(_name(common))
               .issuer_name(issuer.subject if issuer is not None else _name(common))
               .public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(days=1))
               .not_valid_after(now + timedelta(days=days))
               .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(
                   digital_signature=True, key_cert_sign=True, crl_sign=True,
                   content_commitment=False, key_encipherment=False, data_encipherment=False,
                   key_agreement=False, encipher_only=False, decipher_only=False),
                   critical=True))
    if ski:
        builder = builder.add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    if issuer is not None:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signer_key.public_key()),
            critical=False)
    if constraints is not None:
        builder = builder.add_extension(constraints, critical=True)
    return builder.sign(signer_key, hashes.SHA256()), key


def make_leaf(issuer=None, issuer_key=None, *, names=("vaf.lan", "192.168.1.20"),
              days: int = 200, starts_in_days: int = -1, key=None, ca: bool = False,
              eku=(ExtendedKeyUsageOID.SERVER_AUTH,), identifiers: bool = True):
    """A server certificate. Without an issuer it signs itself."""
    key = key or make_key()
    now = datetime.now(timezone.utc)
    self_signed = issuer is None
    signer_key = key if self_signed else issuer_key
    issuer_name = _name("Self Signed Leaf") if self_signed else issuer.subject
    not_before = now + timedelta(days=starts_in_days)
    not_after = now + timedelta(days=days)
    if not_after <= not_before:
        not_before = not_after - timedelta(days=10)
    builder = (x509.CertificateBuilder()
               .subject_name(_name("Self Signed Leaf") if self_signed else _name("leaf"))
               .issuer_name(issuer_name)
               .public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(not_before)
               .not_valid_after(not_after)
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if eku:
        builder = builder.add_extension(x509.ExtendedKeyUsage(list(eku)), critical=False)
    if names:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([_general(n) for n in names]), critical=False)
    if identifiers:
        builder = builder.add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signer_key.public_key()),
            critical=False)
    algorithm = None if isinstance(signer_key, ed25519.Ed25519PrivateKey) else hashes.SHA256()
    return builder.sign(signer_key, algorithm), key


def pem(*certs) -> bytes:
    return b"".join(c.public_bytes(serialization.Encoding.PEM) for c in certs)


def key_pem(key, password: bytes = b"") -> bytes:
    encryption = (serialization.BestAvailableEncryption(password) if password
                  else serialization.NoEncryption())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             encryption)


def fingerprint(cert) -> str:
    return cert.fingerprint(hashes.SHA256()).hex()


def own_chain(names=("vaf.lan", "192.168.1.20"), days: int = 200):
    """A company-style chain: root, intermediate, leaf. Returns (leaf, leaf_key,
    intermediate, root)."""
    root, root_key = make_ca("Example Root")
    inter, inter_key = make_ca("Example Intermediate", issuer=root, issuer_key=root_key)
    leaf, leaf_key = make_leaf(inter, inter_key, names=names, days=days)
    return leaf, leaf_key, inter, root


def isolated_home(tmp_path: Path, monkeypatch, **values) -> Path:
    """A configuration file and an ssl directory of this test's own; returns the ssl
    directory. The session's config file is shared between tests, so a test that saves
    must not save into it."""
    import json

    from vaf.core.config import Config
    from vaf.network import ssl_utils

    from vaf.core.migrations import CONFIG_FORMAT_VERSION

    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    config_file = root / "config.json"
    # At the current format version, so the first read does not migrate and rewrite it.
    config_file.write_text(json.dumps({"config_format_version": CONFIG_FORMAT_VERSION,
                                       **values}), encoding="utf-8")
    monkeypatch.setattr(Config, "APP_DIR", root)
    monkeypatch.setattr(Config, "CONFIG_FILE", config_file)
    monkeypatch.setattr(Config, "_filelock", None)
    ssl_dir = root / "ssl"
    monkeypatch.setattr(ssl_utils, "_ssl_dir_path", lambda: ssl_dir)
    monkeypatch.setattr(ssl_utils, "_LISTENERS", {})
    # Remembered lookups belong to the test that made them.
    monkeypatch.setattr(ssl_utils, "_LOOKUP_MEMO", {})
    return ssl_dir


def handshake(server_context: ssl.SSLContext, client_context: ssl.SSLContext,
              server_hostname=None) -> bytes:
    """One TLS handshake between the two contexts, in memory. Returns the leaf the client
    received (DER); raises what the client's verification raises."""
    c_in, c_out, s_in, s_out = (ssl.MemoryBIO() for _ in range(4))
    client = client_context.wrap_bio(c_in, c_out, server_side=False,
                                     server_hostname=server_hostname)
    server = server_context.wrap_bio(s_in, s_out, server_side=True)
    client_done = server_done = False
    for _ in range(50):
        if not client_done:
            try:
                client.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                pass
        s_in.write(c_out.read())
        if not server_done:
            try:
                server.do_handshake()
                server_done = True
            except ssl.SSLWantReadError:
                pass
            except ssl.SSLError:
                # The client refused and told the server so; the client's error is the answer.
                pass
        c_in.write(s_out.read())
        if client_done and server_done:
            break
    if not client_done:
        client.do_handshake()
    return client.getpeercert(binary_form=True)


def strict_client(ca_pem: bytes) -> ssl.SSLContext:
    context = ssl.create_default_context(cadata=ca_pem.decode("ascii"))
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    context.check_hostname = False
    return context


def server_context(cert_file, key_file) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_file), str(key_file))
    return context
