"""
Auto-generate self-signed TLS certificates for local network mode.

When the user enables TLS but has no certificate configured, VAF generates
a self-signed CA + server certificate pair stored in ~/.vaf/ssl/.

The generated certs are:
  ca.pem / ca-key.pem       - Local root CA (user can install for trust)
  server.pem / server-key.pem - Server certificate (signed by local CA)

The server cert includes SANs for:
  - localhost / 127.0.0.1
  - All detected local network IPs (e.g. 192.168.1.x)
  - Hostname of the machine

Validity: 365 days (auto-renewed on next startup if expired).
"""

import os
import logging
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import ipaddress

logger = logging.getLogger(__name__)

# Default directory for generated certs
_SSL_DIR_NAME = "ssl"


def _get_ssl_dir() -> Path:
    """Get or create the SSL certificate directory (~/.vaf/ssl/)."""
    from vaf.core.config import Config
    vaf_home = Path(Config.get("vaf_home", str(Path.home() / ".vaf")))
    ssl_dir = vaf_home / _SSL_DIR_NAME
    ssl_dir.mkdir(parents=True, exist_ok=True)
    return ssl_dir


def _generate_ca(ssl_dir: Path) -> tuple:
    """Generate a local root CA key + certificate."""
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    ca_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "VAF Local Network CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "VAF"),
    ])

    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    # Write CA key
    ca_key_path = ssl_dir / "ca-key.pem"
    ca_key_path.write_bytes(
        ca_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    # Restrict permissions (owner only)
    try:
        os.chmod(ca_key_path, 0o600)
    except OSError:
        pass  # Windows may not support chmod

    # Write CA cert
    ca_cert_path = ssl_dir / "ca.pem"
    ca_cert_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))

    logger.info("Generated local CA: %s", ca_cert_path)
    return ca_key, ca_cert


def _collect_sans() -> list:
    """Collect Subject Alternative Names for the server certificate."""
    sans = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        x509.IPAddress(ipaddress.IPv6Address("::1")),
    ]

    # Add hostname
    try:
        hostname = socket.gethostname()
        if hostname and hostname != "localhost":
            sans.append(x509.DNSName(hostname))
            # Also try FQDN
            try:
                fqdn = socket.getfqdn()
                if fqdn and fqdn != hostname and fqdn != "localhost":
                    sans.append(x509.DNSName(fqdn))
            except Exception:
                pass
    except Exception:
        pass

    # Add all local network IPs
    try:
        from vaf.network.binding import get_all_local_ips
        for _iface, ip_str in get_all_local_ips():
            try:
                ip_obj = ipaddress.ip_address(ip_str)
                san_entry = x509.IPAddress(ip_obj)
                if san_entry not in sans:
                    sans.append(san_entry)
            except ValueError:
                pass
    except ImportError:
        pass

    return sans


def _generate_server_cert(ssl_dir: Path, ca_key, ca_cert) -> tuple[Path, Path]:
    """Generate a server certificate signed by the local CA."""
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    server_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "VAF Local Server"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "VAF"),
    ])

    sans = _collect_sans()

    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True,
                content_commitment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName(sans),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    # Write server key
    server_key_path = ssl_dir / "server-key.pem"
    server_key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    try:
        os.chmod(server_key_path, 0o600)
    except OSError:
        pass

    # Write server cert (include CA cert for chain)
    server_cert_path = ssl_dir / "server.pem"
    server_cert_path.write_bytes(
        server_cert.public_bytes(serialization.Encoding.PEM)
        + ca_cert.public_bytes(serialization.Encoding.PEM)
    )

    logger.info("Generated server certificate: %s (SANs: %d entries)", server_cert_path, len(sans))
    return server_cert_path, server_key_path


def _is_cert_valid(cert_path: Path, min_days_remaining: int = 30) -> bool:
    """Check if an existing certificate is still valid."""
    try:
        cert_pem = cert_path.read_bytes()
        cert = x509.load_pem_x509_certificate(cert_pem)
        remaining = cert.not_valid_after_utc - datetime.now(timezone.utc)
        return remaining.days >= min_days_remaining
    except Exception:
        return False


def _cert_has_required_ip_sans(cert_path: Path, required_ips: set[str]) -> bool:
    """Check if certificate SAN includes all required IP addresses."""
    try:
        cert_pem = cert_path.read_bytes()
        cert = x509.load_pem_x509_certificate(cert_pem)
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        san_ips = {str(v) for v in san_ext.get_values_for_type(x509.IPAddress)}
        for ip in (required_ips or set()):
            if ip and ip not in san_ips:
                return False
        return True
    except Exception:
        return False


def ensure_ssl_certificates() -> tuple[Optional[str], Optional[str]]:
    """
    Ensure SSL certificates exist and are valid.

    If TLS is enabled in config but no cert/key paths are set, auto-generates
    a self-signed certificate pair.  If existing certs are expired or missing,
    regenerates them.

    Returns:
        (cert_path, key_path) strings, or (None, None) if TLS is not enabled.
    """
    from vaf.core.config import Config

    tls_enabled = Config.get("local_network_tls_enabled", False)
    if not tls_enabled:
        return None, None

    # Current required IP SANs (for LAN IP changes across restarts)
    required_ips: set[str] = {"127.0.0.1"}
    try:
        from vaf.network.binding import get_local_network_ip
        lan_ip = str(get_local_network_ip() or "").strip()
        if lan_ip:
            required_ips.add(lan_ip)
    except Exception:
        pass

    # Check if user has manually configured cert paths
    ssl_cert = (Config.get("local_network_ssl_cert") or "").strip()
    ssl_key = (Config.get("local_network_ssl_key") or "").strip()

    ssl_dir = _get_ssl_dir()
    auto_server_cert = (ssl_dir / "server.pem").resolve()
    auto_server_key = (ssl_dir / "server-key.pem").resolve()

    if ssl_cert and ssl_key and os.path.isfile(ssl_cert) and os.path.isfile(ssl_key):
        # User-provided certs exist - check validity
        if _is_cert_valid(Path(ssl_cert)):
            try:
                cert_resolved = Path(ssl_cert).resolve()
                key_resolved = Path(ssl_key).resolve()
            except Exception:
                cert_resolved = Path(ssl_cert)
                key_resolved = Path(ssl_key)
            is_auto_managed_cert = cert_resolved == auto_server_cert and key_resolved == auto_server_key
            if is_auto_managed_cert and not _cert_has_required_ip_sans(Path(ssl_cert), required_ips):
                logger.warning(
                    "Existing auto-generated SSL cert SANs missing current LAN IP(s) %s; regenerating.",
                    sorted(required_ips),
                )
            else:
                logger.info("Using user-provided SSL certificate: %s", ssl_cert)
                return ssl_cert, ssl_key
        else:
            logger.warning("User-provided SSL certificate is expired or invalid: %s", ssl_cert)
            # Fall through to auto-generation

    # Auto-generate certificates
    ca_key_path = ssl_dir / "ca-key.pem"
    ca_cert_path = ssl_dir / "ca.pem"
    server_cert_path = ssl_dir / "server.pem"
    server_key_path = ssl_dir / "server-key.pem"

    # Check if existing auto-generated certs are still valid
    if server_cert_path.exists() and server_key_path.exists():
        if _is_cert_valid(server_cert_path) and _cert_has_required_ip_sans(server_cert_path, required_ips):
            logger.info("Using existing auto-generated SSL certificate")
            # Update config with auto-generated paths
            Config.set("local_network_ssl_cert", str(server_cert_path))
            Config.set("local_network_ssl_key", str(server_key_path))
            return str(server_cert_path), str(server_key_path)
        if _is_cert_valid(server_cert_path):
            logger.warning(
                "Auto-generated SSL cert is valid but SANs do not match current LAN IP(s) %s; regenerating.",
                sorted(required_ips),
            )

    # Generate new CA if needed
    if ca_key_path.exists() and ca_cert_path.exists():
        try:
            ca_key = serialization.load_pem_private_key(
                ca_key_path.read_bytes(), password=None
            )
            ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
            logger.info("Loaded existing CA certificate")
        except Exception:
            logger.warning("Failed to load existing CA, regenerating")
            ca_key, ca_cert = _generate_ca(ssl_dir)
    else:
        ca_key, ca_cert = _generate_ca(ssl_dir)

    # Generate server certificate
    cert_path, key_path = _generate_server_cert(ssl_dir, ca_key, ca_cert)

    # Update config with auto-generated paths
    Config.set("local_network_ssl_cert", str(cert_path))
    Config.set("local_network_ssl_key", str(key_path))

    logger.info(
        "Auto-generated SSL certificates in %s. "
        "To avoid browser warnings, install %s as a trusted CA.",
        ssl_dir, ca_cert_path,
    )

    return str(cert_path), str(key_path)
