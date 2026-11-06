# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Keys and certificates of the app link.

Two authorities, kept apart on purpose:
- the LOCAL CA of ``vaf/network/ssl_utils.py`` signs the server leaf the link serves
  (``app_link_server_files``); the app pins that CA by the SHA-256 in the QR;
- the DEVICE CA here signs the client certificate a phone gets at pairing (EKU clientAuth
  only), and it is the only verify location of the link's server context, so a certificate
  of the local CA, a browser's certificate or anything self-signed never passes as a device.

NAMED BOUNDARY: the device CA key sits owner-only in the ssl directory beside the server CA
key, like it. A stolen device CA key mints certificates that still need a device row with
that exact public key and serial (devices.py), so it opens nothing alone. There is no
rotation command: replacing it means every device pairs again.
"""
from __future__ import annotations

import hashlib
import logging
import ssl
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from vaf.core.app_link.protocol import ALPN, SNI_LINK, SNI_PAIR

logger = logging.getLogger(__name__)

DEVICE_CA_CERT = "device-ca.pem"
DEVICE_CA_KEY = "device-ca-key.pem"
DEVICE_CERT_DAYS = 730

_lock = threading.RLock()
_context: Optional[ssl.SSLContext] = None
_context_stamp: Tuple = ()


class CsrRefused(ValueError):
    """A CSR that is not PKCS#10 DER over a P-256 key with a valid signature."""


def _ssl_dir() -> Path:
    from vaf.network import ssl_utils
    return ssl_utils._get_ssl_dir()


def _generate_device_ca(ssl_dir: Path):
    from vaf.network import ssl_utils
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "VAF Device CA"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "VAF")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=True, crl_sign=True,
            content_commitment=False, key_encipherment=False, data_encipherment=False,
            key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    ssl_utils._write_secret(ssl_dir / DEVICE_CA_KEY, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    ssl_utils._write_public(ssl_dir / DEVICE_CA_CERT, cert.public_bytes(serialization.Encoding.PEM))
    logger.info("Generated the device CA of the app link: %s", ssl_dir / DEVICE_CA_CERT)
    return key, cert


def device_ca():
    """(key, certificate) of the device CA, created on first use."""
    from vaf.network import ssl_utils
    with _lock:
        ssl_dir = _ssl_dir()
        key_path, cert_path = ssl_dir / DEVICE_CA_KEY, ssl_dir / DEVICE_CA_CERT
        if key_path.exists() and cert_path.exists():
            try:
                key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
                cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
                if ssl_utils._key_belongs_to(cert, key):
                    return key, cert
                logger.warning("The device CA key does not belong to its certificate; every "
                               "Veyllo App device has to pair again.")
            except Exception:
                logger.warning("The device CA cannot be read; every Veyllo App device has to "
                               "pair again.")
        return _generate_device_ca(ssl_dir)


def device_ca_der() -> bytes:
    return device_ca()[1].public_bytes(serialization.Encoding.DER)


def check_csr(der: bytes):
    """The public key of a PKCS#10 CSR in DER whose signature is valid and whose key is EC
    P-256. The subject is ignored: the device's name comes from the pairing message, its
    identity from the key."""
    try:
        csr = x509.load_der_x509_csr(bytes(der))
    except Exception:
        raise CsrRefused("not a CSR") from None
    try:
        valid = csr.is_signature_valid
    except Exception:
        valid = False
    if not valid:
        raise CsrRefused("bad signature")
    key = csr.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise CsrRefused("not a P-256 key")
    return key


def issue_device_cert(public_key, device_id: str) -> bytes:
    """A client certificate for this device from the device CA, DER."""
    ca_key, ca_cert = device_ca()
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(device_id))]))
        .issuer_name(ca_cert.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=DEVICE_CERT_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_encipherment=False, content_commitment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                       critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


def spki_der(public_key) -> bytes:
    return public_key.public_bytes(serialization.Encoding.DER,
                                   serialization.PublicFormat.SubjectPublicKeyInfo)


def spki_sha256(public_key) -> str:
    return hashlib.sha256(spki_der(public_key)).hexdigest()


def display_fingerprint(public_key) -> str:
    """What the web confirm and the app both show: the first 8 bytes of the SHA-256 of the
    SubjectPublicKeyInfo, uppercase hex in 4 groups ("A1B2 C3D4 E5F6 0718")."""
    head = hashlib.sha256(spki_der(public_key)).hexdigest()[:16].upper()
    return " ".join(head[i:i + 4] for i in range(0, 16, 4))


def ca_sha256() -> bytes:
    """The SHA-256 of the local CA's DER: the QR's ``ca`` (``ssl_utils.ca_fingerprint`` in
    hex)."""
    from vaf.network import ssl_utils
    return hashlib.sha256(ssl_utils.app_link_server_files()[2]).digest()


def _sni_callback(sslobj: Any, name: Optional[str], _context: ssl.SSLContext):
    if name not in (SNI_LINK, SNI_PAIR):
        return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
    # The session reads the purpose back from here (a server-side SSLObject does not expose
    # the name the client sent).
    try:
        sslobj.vaf_sni = name
    except Exception:
        return ssl.ALERT_DESCRIPTION_INTERNAL_ERROR
    return None


def _stamp(*paths: Path) -> Tuple:
    out = []
    for path in paths:
        try:
            st = path.stat()
            out.append((str(path), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((str(path), 0, 0))
    return tuple(out)


def server_context() -> ssl.SSLContext:
    """The link's server context: TLS 1.3 only, the app-link leaf, the device CA as the ONLY
    verify location (client certificate optional, since pairing has none), ALPN
    ``vaf-link/1``, no session tickets (every session is a full handshake with the device's
    certificate: no resumption, no early data), and every SNI but the two refused. Rebuilt
    when the leaf, the local CA or the device CA changed on disk."""
    global _context, _context_stamp
    from vaf.network import ssl_utils
    with _lock:
        chain_path, key_path, _ca_der = ssl_utils.app_link_server_files()
        device_ca()
        ssl_dir = _ssl_dir()
        stamp = _stamp(chain_path, key_path, ssl_dir / DEVICE_CA_CERT)
        if _context is not None and stamp == _context_stamp:
            return _context
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.maximum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(str(chain_path), str(key_path))
        context.verify_mode = ssl.CERT_OPTIONAL
        context.load_verify_locations(cafile=str(ssl_dir / DEVICE_CA_CERT))
        context.set_alpn_protocols([ALPN])
        context.num_tickets = 0
        context.options |= ssl.OP_NO_TICKET
        context.sni_callback = _sni_callback
        _context, _context_stamp = context, stamp
        return context


def reset_context() -> None:
    """Forget the cached context (tests, and after the ssl directory moved)."""
    global _context, _context_stamp
    with _lock:
        _context, _context_stamp = None, ()
