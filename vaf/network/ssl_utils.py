# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
The certificate every TLS listener of VAF serves, and the local authority behind it.

Which pair is served (``served_certificate``): with TLS on, the person's own certificate
when ``local_network_ssl_cert`` / ``local_network_ssl_key`` name one that loads, expired or
not; otherwise VAF's own, issued here by a local CA in ``~/.vaf/ssl/``:

  ca.pem / ca-key.pem          - the local CA (devices install ca.pem to trust this machine)
  server.pem / server-key.pem  - the server certificate it signs, with the CA appended
  custom/<fp>.pem / <fp>.key   - an own certificate installed through ``install_certificate``

The two keys hold the person's own pair or nothing; VAF never writes its own paths into
them, and never replaces an own certificate. One that cannot load (missing, encrypted, the
key does not belong to it) is answered with VAF's own for that run and an ERROR line, the
configuration untouched, so fixing the file at that path is enough. An own certificate near
or past its expiry is still served: serving another authority instead teaches people to
click warnings away.

VAF's own server certificate carries ``localhost``, 127.0.0.1, ::1, every LAN and VPN
address, ``<host>.local`` (the mDNS name), and the host name and FQDN (under a restricted CA
only when they lie under one of its private names), is valid for 365 days and is issued again at a start that finds it under
30 days, unusable to strict verifiers, or missing an address. The CA is kept across those
re-issues: every device that installed it keeps trusting the machine.

A NEW CA is restricted (critical NameConstraints, ``_authority_constraints``): it can vouch
only for private, shared and loopback addresses, ``localhost`` and names under ``local``,
``home.arpa``, ``internal`` and ``lan``. A copy of its key then cannot vouch for a public
name on a device that installed it, on clients that enforce the constraints (OpenSSL and
Python, measured; Chrome on Linux 112 and later, Firefox and Chrome on macOS by their
documentation). An existing CA is kept as it is, so installed devices keep working;
``renew_authority`` replaces it when the person chooses.

``inspect_certificate`` is the one check of a certificate somebody wants to serve (key,
chain, dates, key strength, names); ``install_certificate`` and ``reset_certificate`` switch
the served pair, and ``reload_certificate`` loads it into every running listener without a
restart (``register_tls_listener``). ``served_authority`` is what a room peer on another
machine pins: the last certificate of the served chain; ``served_room_host`` the host its
invitation names, one the served certificate covers, which the peer checks the certificate
against.

``server_tls_options(certfile, keyfile)`` is the one TLS policy of every listener VAF serves
TLS on (the integrated HTTPS proxy, the desktop's TLS port, ``vaf run --web``), and
``register_tls_listener`` sits beside it: every listener built from those options registers
for the reload.

Named boundaries:
- Not on the ``vaf`` facade. The served pair is read by three listeners, all of them VAF's
  own doors (the proxy, the desktop's TLS port, ``vaf run --web``), and by no embedder-facing
  listener: an embedder who serves an agent runs its own door and its own TLS, the same
  boundary as ``vaf.network.binding`` ("one HTTP door today", docs/EMBEDDING.md). The one
  embedder-visible effect goes through ``vaf.room_invitation``, whose ``ca_fingerprint`` is
  ``served_authority``.
- The CA key is an unencrypted owner-only file. The constraints limit what a copy can sign;
  they do not make the key secret again.
- PKCS#12 files and passphrase-protected keys are refused with the conversion command;
  VAF runs no ACME client.
- Name constraints were not measured on Windows or Safari; a client that ignores them is no
  worse off than with an unrestricted CA. ``lan`` is not a reserved name, only one no public
  registry has delegated.
- ``vaf run --web`` has no internal channel, so a change made in a terminal reaches it at
  its next start.
"""

import ipaddress
import logging
import os
import re
import socket
import ssl
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

logger = logging.getLogger(__name__)

# Default directory for generated certs
_SSL_DIR_NAME = "ssl"
# Own certificates installed through install_certificate, named by their fingerprint.
_CUSTOM_DIR_NAME = "custom"

# The two keys that name the person's own pair. Empty means VAF's own.
CERT_KEY = "local_network_ssl_cert"
KEY_KEY = "local_network_ssl_key"
CERTIFICATE_KEYS = (CERT_KEY, KEY_KEY)

# A certificate with fewer days left is re-issued (VAF's own) or warned about (an own one).
RENEW_BEFORE_DAYS = 30
# What one part of an upload may hold. A real chain is a few KiB; a bundle of the leaf, two
# intermediates and the key stays far under it.
PEM_PART_MAX_BYTES = 64 * 1024
CHAIN_MAX_CERTIFICATES = 8

# The TLS 1.2 suites of Mozilla's "intermediate" profile (server-side TLS guidelines 6.0):
# forward-secret key exchange (ECDHE) and an AEAD cipher, nothing else. OpenSSL's DEFAULT,
# which the HTTPS proxy used to ask for, also accepted static RSA key exchange (no forward
# secrecy: a key stolen later decrypts recorded traffic) and CBC suites with SHA-1 (measured:
# AES128-SHA, AES128-GCM-SHA256, AES256-SHA256 and ECDHE-RSA-AES128-SHA all accepted).
SERVER_TLS12_CIPHERS = ":".join((
    "ECDHE-ECDSA-AES128-GCM-SHA256",
    "ECDHE-RSA-AES128-GCM-SHA256",
    "ECDHE-ECDSA-AES256-GCM-SHA384",
    "ECDHE-RSA-AES256-GCM-SHA384",
    "ECDHE-ECDSA-CHACHA20-POLY1305",
    "ECDHE-RSA-CHACHA20-POLY1305",
))


def server_tls_options(certfile: str, keyfile: str) -> dict:
    """The TLS keyword arguments of a ``uvicorn.Config`` for a listener VAF serves.

    One policy for every TLS listener, so none of them can drift: the integrated HTTPS proxy
    asked for OpenSSL's DEFAULT, the desktop's TLS port and ``vaf run --web`` for nothing,
    which means OpenSSL's default list on uvicorn 0.48 and later and uvicorn's own old
    default ("TLSv1") on the 0.27 to 0.40 releases ``requirements.txt`` still allows.

    - ``ssl_ciphers`` limits TLS 1.2 to ``SERVER_TLS12_CIPHERS``. ``set_ciphers`` does not
      touch TLS 1.3, whose suites are all AEAD and forward secret.
    - The key exchange groups stay at OpenSSL's default (on OpenSSL 3.5 the post-quantum
      hybrid ``X25519MLKEM768`` comes first, measured).
    - The TLS 1.2 floor is Python's own for ``PROTOCOL_TLS_SERVER`` on every supported
      version (3.10 and later, measured). It is pinned by a test, not set by a line here: a
      line that sets what is already set cannot fail its test.
    - The list drops clients from before about 2014 that know only CBC or static-RSA suites
      over TLS 1.2 (the oldest the profile keeps: Android 4.4.2, Firefox 27, Chrome 31,
      IE 11 on Windows 7, Safari 9).

    A listener built from these options registers with ``register_tls_listener`` once its
    ``uvicorn.Server`` exists. The reload loads the new pair into the context uvicorn built
    from these options, so the policy stays as it is.
    """
    return {
        "ssl_certfile": certfile,
        "ssl_keyfile": keyfile,
        "ssl_version": ssl.PROTOCOL_TLS_SERVER,
        "ssl_ciphers": SERVER_TLS12_CIPHERS,
    }


# ── the running listeners, for the reload ───────────────────────────────────

# name -> (uvicorn.Server, the event loop it serves on). Name-keyed: a listener started
# again (the tray's restart) replaces its entry.
_LISTENERS: Dict[str, Tuple[Any, Any]] = {}
_LISTENERS_LOCK = threading.Lock()
# name -> the certificate file's bytes as that listener loaded them (register_tls_listener,
# replaced only by a load that succeeded).
_LOADED_CHAINS: Dict[str, bytes] = {}
# One change of the served pair at a time in this process: install, reset, renewal, reload.
_CHANGE_LOCK = threading.RLock()
# How long a listener's own loop may take to load the new pair.
_RELOAD_WAIT_S = 10.0


def register_tls_listener(name: str, server: Any, loop: Any = None) -> None:
    """Register a running TLS listener so ``reload_certificate`` reaches it.

    ``server`` is the ``uvicorn.Server`` built from ``server_tls_options``; ``loop`` is the
    event loop it serves on. The new pair is loaded on that loop: OpenSSL does not promise
    that a context can be changed while another thread runs a handshake on it, and every
    handshake of the listener runs on its loop.

    The certificate file it is about to load is read here too: ``served_chain`` reports what
    a listener loaded, never a file replaced since (a ``vaf server cert reset`` or renewal
    rewrites ``server.pem`` in place, and a listener whose reload timed out still serves the
    previous one)."""
    try:
        chain = Path(str(server.config.ssl_certfile)).read_bytes()
    except Exception:
        chain = b""
    with _LISTENERS_LOCK:
        _LISTENERS[str(name)] = (server, loop)
        _LOADED_CHAINS[str(name)] = chain


def _no_passphrase() -> bytes:
    """The password callback for every load: never OpenSSL's terminal prompt, which would
    hang a service that meets an encrypted key."""
    return b""


# ── where the files live ─────────────────────────────────────────────────────

def _ssl_dir_path() -> Path:
    """The SSL directory (~/.vaf/ssl/), whether or not it exists. Read-only callers use
    this; nothing here creates it."""
    from vaf.core.config import Config
    vaf_home = Path(Config.get("vaf_home", str(Path.home() / ".vaf")))
    return vaf_home / _SSL_DIR_NAME


def _get_ssl_dir() -> Path:
    """The SSL directory, created when missing. For the callers that write into it."""
    ssl_dir = _ssl_dir_path()
    ssl_dir.mkdir(parents=True, exist_ok=True)
    return ssl_dir


def _write_secret(path: Path, data: bytes) -> None:
    """A private key, owner-only from its first byte (mkstemp's 0600, then a rename): a
    write followed by a chmod left it readable under the umask in between."""
    from vaf.core.secure_store import atomic_write_bytes
    atomic_write_bytes(path, data)


def _write_public(path: Path, data: bytes) -> None:
    """A certificate: public, so the mode of the file it replaces (0644 for a new one)."""
    from vaf.core.secure_store import atomic_write_bytes
    atomic_write_bytes(path, data, keep_mode=True)


# ── the local authority ──────────────────────────────────────────────────────

# The DNS names a restricted CA vouches for besides localhost, each with every name below it:
# mDNS's local (RFC 6762), RFC 8375's home.arpa and internal (set aside for private use in
# 2024) are reserved; lan, which routers hand out (OpenWrt's default), is not reserved, only
# never delegated. Deliberately not this machine's own host name or FQDN: a DNS constraint
# permits every name below it, and a host named "studio" or a server named after its domain
# ("example.com") would let the CA vouch for www.google.studio or www.google.example.com
# (measured, OpenSSL and Python strict).
_PRIVATE_DNS_SUFFIXES = ("local", "home.arpa", "internal", "lan")
_IPV6_LOOPBACK = ipaddress.ip_network("::1/128")

def _authority_constraints() -> x509.NameConstraints:
    """What a new local CA may vouch for: the address ranges the admission check serves
    (``binding.PRIVATE_RANGES``, ``CGNAT_RANGE``, ``LOCALHOST_RANGES``, plus ::1),
    ``localhost`` and the names under ``_PRIVATE_DNS_SUFFIXES``. Both name types are listed:
    RFC 5280 leaves a type that has no permitted subtree unconstrained, so listing addresses
    alone would leave every DNS name open (and the other way round)."""
    from vaf.network.binding import CGNAT_RANGE, LOCALHOST_RANGES, PRIVATE_RANGES
    networks = [*PRIVATE_RANGES, CGNAT_RANGE, *LOCALHOST_RANGES, _IPV6_LOOPBACK]
    names = ["localhost", *_PRIVATE_DNS_SUFFIXES]
    permitted: List[x509.GeneralName] = [x509.IPAddress(n) for n in networks]
    permitted += [x509.DNSName(n) for n in names]
    return x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None)


def _constraints_of(cert: x509.Certificate) -> Optional[x509.NameConstraints]:
    try:
        return cert.extensions.get_extension_for_class(x509.NameConstraints).value
    except x509.ExtensionNotFound:
        return None


def _subtree_matches(name: x509.GeneralName, subtree: x509.GeneralName) -> bool:
    if isinstance(name, x509.DNSName) and isinstance(subtree, x509.DNSName):
        value = name.value.lower().rstrip(".")
        base = subtree.value.lower().rstrip(".").lstrip(".")
        return value == base or value.endswith("." + base)
    if isinstance(name, x509.IPAddress) and isinstance(subtree, x509.IPAddress):
        address, network = name.value, subtree.value
        if isinstance(network, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            return address.version == network.version and address in network
    return False


def _permits(constraints: Optional[x509.NameConstraints], name: x509.GeneralName) -> bool:
    """Whether an authority with these constraints may vouch for ``name`` (RFC 5280: a name
    type without permitted subtrees is open; an excluded subtree always refuses)."""
    if constraints is None:
        return True
    for subtree in constraints.excluded_subtrees or []:
        if _subtree_matches(name, subtree):
            return False
    same_type = [s for s in constraints.permitted_subtrees or [] if type(s) is type(name)]
    if not same_type:
        return True
    return any(_subtree_matches(name, s) for s in same_type)


def _generate_ca(ssl_dir: Path) -> tuple:
    """Generate a local root CA key + certificate, restricted to private names."""
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
        # RFC 5280 requires a CA certificate to name its own key, and OpenSSL enforces
        # it under X509_V_FLAG_X509_STRICT - which Python 3.13 turns ON by default in
        # ssl.create_default_context(). Without this, verification of anything this CA
        # signed fails with "Missing Subject Key Identifier", so a correctly written
        # Python client cannot connect at all while curl and browsers still can.
        # Measured, not assumed: only CA-SKI plus leaf-AKI together verify under strict.
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        # Critical, as RFC 5280 requires of a CA's name constraints: a verifier that does
        # not understand them must refuse rather than ignore them.
        .add_extension(_authority_constraints(), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    ca_key_path = ssl_dir / "ca-key.pem"
    _write_secret(ca_key_path, ca_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    ca_cert_path = ssl_dir / "ca.pem"
    _write_public(ca_cert_path, ca_cert.public_bytes(serialization.Encoding.PEM))

    logger.info("Generated local CA: %s", ca_cert_path)
    return ca_key, ca_cert


def _key_belongs_to(cert: x509.Certificate, key) -> bool:
    """Whether a private key is the one whose public half the certificate carries."""
    def _spki(public_key) -> bytes:
        return public_key.public_bytes(serialization.Encoding.DER,
                                       serialization.PublicFormat.SubjectPublicKeyInfo)
    try:
        return _spki(cert.public_key()) == _spki(key.public_key())
    except Exception:
        return False


_NEW_CA_WARNING = ("Generating a new CA: any device that installed the old ca.pem must "
                   "install the new one.")


def _load_or_create_ca(ssl_dir: Path) -> tuple:
    """The local CA, kept when it is usable and created when it is not.

    Kept across server-certificate rotations on purpose: anybody who installed it in a
    trust store keeps trusting this machine. Replaced, at WARNING, because every device
    that trusted the old file has to be handed the new one, when it lacks its key
    identifier (no strict client can verify it), when its key does not belong to it, or
    when only one of the two files is there."""
    ca_key_path = ssl_dir / "ca-key.pem"
    ca_cert_path = ssl_dir / "ca.pem"
    if ca_key_path.exists() and ca_cert_path.exists():
        try:
            ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
            ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
        except Exception:
            logger.warning("Failed to load the existing CA at %s. %s", ca_cert_path,
                           _NEW_CA_WARNING)
            return _generate_ca(ssl_dir)
        if not _cert_has_key_identifiers(ca_cert, is_ca=True):
            logger.warning(
                "The local CA at %s predates the key identifiers that strict TLS "
                "verification requires, so no properly verifying client could "
                "connect. %s", ca_cert_path, _NEW_CA_WARNING)
            return _generate_ca(ssl_dir)
        if not _key_belongs_to(ca_cert, ca_key):
            logger.warning("The CA key at %s does not belong to %s, so nothing it signs "
                           "verifies. %s", ca_key_path, ca_cert_path, _NEW_CA_WARNING)
            return _generate_ca(ssl_dir)
        logger.info("Loaded existing CA certificate")
        return ca_key, ca_cert
    if ca_cert_path.exists() or ca_key_path.exists():
        logger.warning("Only one half of the local CA is in %s. %s", ssl_dir, _NEW_CA_WARNING)
    return _generate_ca(ssl_dir)


def _collect_sans() -> list:
    """Collect Subject Alternative Names for the server certificate."""
    sans = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        x509.IPAddress(ipaddress.IPv6Address("::1")),
    ]

    # The host name and its FQDN (the same list HSTS keeps off, binding.hsts_for_host), and
    # the name mDNS gives the machine, <host>.local: a restricted CA vouches for that one,
    # so a device keeps a name to open VAF by when the host name itself is left out.
    from vaf.network.binding import machine_dns_names
    names = machine_dns_names()
    for name in names:
        sans.append(x509.DNSName(name))
    mdns = _mdns_name(names)
    if mdns and x509.DNSName(mdns) not in sans:
        sans.append(x509.DNSName(mdns))

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


_MDNS_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")


def _mdns_name(names: List[str]) -> Optional[str]:
    """``<host>.local``, the name mDNS (Avahi, Bonjour) gives this machine, from the first of
    ``machine_dns_names()`` (the host name), or None."""
    label = str(names[0] if names else "").strip().lower().split(".")[0]
    if not label or label == "localhost" or not _MDNS_LABEL.fullmatch(label):
        return None
    return f"{label}.local"


def _sans_inside(constraints: Optional[x509.NameConstraints], sans: list) -> list:
    """The names a CA with these constraints may sign. One name outside makes a strict
    verifier refuse the whole certificate (measured: private names plus one public address,
    "permitted subtree violation"), so a name outside is left off, at WARNING, with the names
    this CA does vouch for (an authority from before the restriction carries every name)."""
    if constraints is None:
        return list(sans)
    kept, dropped = [], []
    for name in sans:
        (kept if _permits(constraints, name) else dropped).append(name)
    if dropped:
        permitted = [s.value for s in constraints.permitted_subtrees or []
                     if isinstance(s, x509.DNSName)]
        local = [n.value for n in kept if isinstance(n, x509.DNSName)
                 and n.value.lower().endswith(".local")]
        logger.warning(
            "The local CA vouches only for private addresses and for %s and the names under "
            "them, so the server certificate leaves out %s: a device that opens VAF there sees "
            "a certificate warning. Open VAF by its address%s instead. (An authority created "
            "before VAF restricted them keeps carrying every name.)",
            ", ".join(permitted), ", ".join(str(n.value) for n in dropped),
            f" or by {local[0]}" if local else "")
    return kept


def names_a_new_authority_leaves_out() -> List[str]:
    """This machine's names that a newly created local CA (``_authority_constraints``) may
    not vouch for, so its server certificate leaves them out: what a renewal takes away from
    a device that opens VAF by name. Looks up the FQDN (``machine_dns_names``)."""
    try:
        from vaf.network.binding import machine_dns_names
        names = machine_dns_names()
    except Exception:
        return []
    constraints = _authority_constraints()
    left: List[str] = []
    for name in [*names, _mdns_name(names)]:
        if not name or name in left:
            continue
        try:
            if not _permits(constraints, x509.DNSName(name)):
                left.append(name)
        except Exception:
            left.append(name)
    return left


def mdns_name() -> Optional[str]:
    """``<host>.local`` for this machine, or None (no lookup: the host name only)."""
    import socket as _socket
    try:
        return _mdns_name([_socket.gethostname()])
    except Exception:
        return None


def _generate_server_cert(ssl_dir: Path, ca_key, ca_cert) -> tuple[Path, Path]:
    """Generate a server certificate signed by the local CA."""
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    server_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "VAF Local Server"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "VAF"),
    ])

    sans = _sans_inside(_constraints_of(ca_cert), _collect_sans())

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
        # The other half of the pair above: the leaf must point at the key that signed
        # it, or strict verification stops at depth 0 with "Missing Authority Key
        # Identifier". Its own subject key identifier goes with it, which is what a
        # verifier uses to build the chain in the first place.
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName(sans),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    server_key_path = ssl_dir / "server-key.pem"
    _write_secret(server_key_path, server_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))

    # The certificate with its CA appended: the CA is the last certificate of the chain, the
    # authority a room peer pins (served_authority).
    server_cert_path = ssl_dir / "server.pem"
    _write_public(server_cert_path,
                  server_cert.public_bytes(serialization.Encoding.PEM)
                  + ca_cert.public_bytes(serialization.Encoding.PEM))

    logger.info("Generated server certificate: %s (SANs: %d entries)", server_cert_path, len(sans))
    return server_cert_path, server_key_path


def _cert_has_key_identifiers(cert: x509.Certificate, *, is_ca: bool) -> bool:
    """Whether a certificate carries the identifiers strict verification demands.

    Not a style question. A certificate without them is unusable to any client that
    verifies properly, which is every Python client since 3.13 turned
    VERIFY_X509_STRICT on by default - so a certificate missing them is not "valid but
    old", it is a certificate nobody strict can use.
    """
    try:
        cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
    except x509.ExtensionNotFound:
        return False
    if is_ca:
        return True
    try:
        cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
    except x509.ExtensionNotFound:
        return False
    return True


def _is_cert_valid(cert_path: Path, min_days_remaining: int = RENEW_BEFORE_DAYS,
                   *, is_ca: bool = False) -> bool:
    """Whether VAF's own certificate is still valid AND usable.

    Expiry was the only question until a certificate generated before the key identifiers
    were added turned out to verify nowhere strict. Such a file is reissued rather than
    kept: an unusable certificate that has not expired is still unusable. Asked of VAF's
    own files only; an own certificate is never replaced.
    """
    try:
        cert_pem = cert_path.read_bytes()
        cert = x509.load_pem_x509_certificate(cert_pem)
        if not _cert_has_key_identifiers(cert, is_ca=is_ca):
            return False
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


# ── PEM parts ────────────────────────────────────────────────────────────────

_PEM_BEGIN = b"-----BEGIN "
_PEM_DASHES = b"-----"
_PEM_LABEL = re.compile(rb"[A-Z0-9 ]{1,40}")


def _pem_blocks(data: bytes) -> Tuple[List[bytes], List[bytes]]:
    """(certificate blocks, private key blocks) in the order they appear.

    One pass with ``find``: each BEGIN line is matched with the END line of its label, and
    the scan continues after it. A regex with a lazy body and a back reference took 770 ms on
    one 64 KiB part of repeated BEGIN lines (measured)."""
    data = bytes(data or b"")
    certs: List[bytes] = []
    keys: List[bytes] = []
    at = 0
    while True:
        start = data.find(_PEM_BEGIN, at)
        if start < 0:
            break
        label_start = start + len(_PEM_BEGIN)
        label_end = data.find(_PEM_DASHES, label_start)
        label = data[label_start:label_end] if label_end > 0 else b""
        if label_end < 0 or not _PEM_LABEL.fullmatch(label):
            at = label_start
            continue
        end_line = b"-----END " + label + _PEM_DASHES
        end = data.find(end_line, label_end)
        if end < 0:
            break
        block = data[start:end + len(end_line)] + b"\n"
        if label == b"CERTIFICATE":
            certs.append(block)
        elif label.endswith(b"PRIVATE KEY"):
            keys.append(block)
        at = end + len(end_line)
    return certs, keys


def _key_problem(key_block: bytes) -> Tuple[Optional[str], Any]:
    """(problem code or None, the loaded key)."""
    if b"ENCRYPTED" in key_block.split(b"\n", 2)[0] or b"Proc-Type: 4,ENCRYPTED" in key_block:
        return "key_encrypted", None
    try:
        return None, serialization.load_pem_private_key(key_block, password=None)
    except TypeError:
        # "Password was not given but private key is encrypted" (measured).
        return "key_encrypted", None
    except Exception:
        return "no_key", None


_WEAK_SSL_REASONS = ("TOO_SMALL", "TOO_WEAK", "MD_TOO_WEAK")


def _ssl_load_problem(error: Exception) -> str:
    reason = str(getattr(error, "reason", "") or "")
    if any(part in reason for part in _WEAK_SSL_REASONS):
        return "weak_key"
    if "KEY_VALUES_MISMATCH" in reason:
        return "key_mismatch"
    if isinstance(error, ssl.SSLError):
        return "key_mismatch"
    return "unreadable"


def _load_problem(cert_path: str, key_path: str) -> Optional[str]:
    """Load a pair into a THROWAWAY server context, as uvicorn will: None when it loads.

    The check comes before any live listener sees the pair, because a failed load into a
    live context leaves it without a certificate and every later handshake fails (measured
    on uvicorn 0.49)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(cert_path, key_path, password=_no_passphrase)
    except (ssl.SSLError, ValueError, TypeError) as e:
        return _ssl_load_problem(e)
    except OSError:
        return "missing"
    return None


def _same_file(left: str, right: Path) -> bool:
    try:
        return (os.path.normcase(str(Path(left).expanduser().resolve()))
                == os.path.normcase(str(Path(right).resolve())))
    except Exception:
        return False


# (cert path, key path, both files' stamps) -> the check's answer. served_certificate() is
# asked by every caller that needs the served pair (the settings, the doctor, the remote
# access section), and the check reads and parses the private key twice: 187 ms for an RSA
# 4096 key, 2.7 s for RSA 8192 (measured). A changed file has a new stamp (an atomic
# replace a new inode), so it is checked again.
_PAIR_CHECKS: Dict[tuple, Optional[str]] = {}
_PAIR_CHECKS_LOCK = threading.Lock()


def _file_stamp(path: str) -> tuple:
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _pair_problem(cert_path: str, key_path: str) -> Optional[str]:
    """Why a configured pair cannot be served, or None. Never prompts for a passphrase.
    Answered from ``_PAIR_CHECKS`` while neither file changed."""
    if not cert_path or not key_path or not os.path.isfile(cert_path) or not os.path.isfile(key_path):
        return "missing"
    try:
        stamp = (cert_path, key_path, _file_stamp(cert_path), _file_stamp(key_path))
    except OSError:
        return "missing"
    with _PAIR_CHECKS_LOCK:
        if stamp in _PAIR_CHECKS:
            return _PAIR_CHECKS[stamp]
    problem = _check_pair(cert_path, key_path)
    with _PAIR_CHECKS_LOCK:
        if len(_PAIR_CHECKS) > 32:
            _PAIR_CHECKS.clear()
        _PAIR_CHECKS[stamp] = problem
    return problem


def _check_pair(cert_path: str, key_path: str) -> Optional[str]:
    try:
        cert_bytes = Path(cert_path).read_bytes()
        key_bytes = Path(key_path).read_bytes()
    except OSError:
        return "missing"
    if not _pem_blocks(cert_bytes)[0]:
        return "unreadable"
    keys = _pem_blocks(key_bytes)[1]
    if not keys:
        return "no_key"
    problem, _key = _key_problem(keys[0])
    if problem:
        return problem
    return _load_problem(cert_path, key_path)


# ── which pair is served ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class ServedCertificate:
    """The pair the TLS listeners serve.

    ``source`` is what is served: ``own``, ``automatic`` (VAF's own) or ``none`` (TLS off).
    ``problem`` is set when the configuration names an own pair that cannot load, and VAF's
    own is served instead; ``own_cert_path`` is the path the configuration names."""
    source: str
    cert_path: Optional[str]
    key_path: Optional[str]
    problem: Optional[str] = None
    own_cert_path: str = ""


def _cfg_get(cfg: Optional[dict], key: str, default=None):
    if cfg is None:
        from vaf.core.config import Config
        return Config.get(key, default)
    return cfg.get(key, default)


def _flag(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def served_certificate(cfg: Optional[dict] = None) -> ServedCertificate:
    """Which pair the TLS listeners serve. Read-only: creates and writes nothing.

    TLS off: ``none``. Both keys empty, or naming VAF's own files (an older install wrote
    them there): ``automatic``. Anything else is the person's own pair, served whenever it
    loads, expired or not; one that cannot load leaves VAF's own served with ``problem``
    saying why."""
    if not _flag(_cfg_get(cfg, "local_network_tls_enabled", False)):
        return ServedCertificate("none", None, None)
    ssl_dir = _ssl_dir_path()
    auto_cert, auto_key = ssl_dir / "server.pem", ssl_dir / "server-key.pem"
    cert = str(_cfg_get(cfg, CERT_KEY, "") or "").strip()
    key = str(_cfg_get(cfg, KEY_KEY, "") or "").strip()
    if (not cert and not key) or (_same_file(cert, auto_cert) and _same_file(key, auto_key)):
        return ServedCertificate("automatic", str(auto_cert), str(auto_key))
    problem = _pair_problem(cert, key)
    if problem is None:
        return ServedCertificate("own", cert, key, own_cert_path=cert)
    return ServedCertificate("automatic", str(auto_cert), str(auto_key), problem=problem,
                             own_cert_path=cert)


def _leaf_of(path: Optional[str]) -> Optional[x509.Certificate]:
    if not path:
        return None
    try:
        blocks = _pem_blocks(Path(path).read_bytes())[0]
        return x509.load_pem_x509_certificate(blocks[0]) if blocks else None
    except Exception:
        return None


def _log_own_certificate(cert_path: str) -> None:
    leaf = _leaf_of(cert_path)
    if leaf is None:
        return
    now = datetime.now(timezone.utc)
    until = leaf.not_valid_after_utc
    if until <= now:
        logger.error("Your certificate %s expired on %s. VAF serves it anyway, so every device "
                     "warns: renew it and run `vaf server cert set`.", cert_path,
                     until.date().isoformat())
    elif (until - now).days < RENEW_BEFORE_DAYS:
        logger.warning("Your certificate %s expires on %s (%d days left). Renew it and run "
                       "`vaf server cert set`; VAF does not replace it.", cert_path,
                       until.date().isoformat(), (until - now).days)
    else:
        logger.info("Using your own certificate: %s", cert_path)


def _required_ips() -> set:
    """Every address a device may reach this machine on, LAN and VPN alike, so a changed
    LAN address or a VPN that came up since the last start gets a certificate that names
    it. One that went away does not force a new one (a superset still verifies)."""
    required: set = {"127.0.0.1"}
    try:
        from vaf.network.binding import get_all_local_ips
        required.update(str(ip).strip() for _iface, ip in get_all_local_ips() if str(ip).strip())
    except Exception:
        pass
    return required


def _ensure_automatic(*, reissue: bool = False) -> Tuple[str, str]:
    """VAF's own pair, issued when missing, near its end, unusable or missing an address
    (``reissue`` issues a new one in any case). The CA is kept (``_load_or_create_ca``)."""
    required_ips = _required_ips()
    ssl_dir = _get_ssl_dir()
    server_cert_path = ssl_dir / "server.pem"
    server_key_path = ssl_dir / "server-key.pem"

    if not reissue and server_cert_path.exists() and server_key_path.exists():
        if _is_cert_valid(server_cert_path) and _cert_has_required_ip_sans(server_cert_path, required_ips):
            logger.info("Using existing auto-generated SSL certificate")
            return str(server_cert_path), str(server_key_path)
        if _is_cert_valid(server_cert_path):
            logger.warning(
                "Auto-generated SSL cert is valid but SANs do not match current LAN IP(s) %s; regenerating.",
                sorted(required_ips),
            )

    ca_key, ca_cert = _load_or_create_ca(ssl_dir)
    cert_path, key_path = _generate_server_cert(ssl_dir, ca_key, ca_cert)
    logger.info(
        "Auto-generated SSL certificates in %s. "
        "To avoid browser warnings, install %s as a trusted CA.",
        ssl_dir, ssl_dir / "ca.pem",
    )
    return str(cert_path), str(key_path)


def ensure_ssl_certificates() -> tuple[Optional[str], Optional[str]]:
    """The pair to serve, VAF's own issued when it is the one to serve.

    Returns ``(cert_path, key_path)``, or ``(None, None)`` while TLS is off. Never writes
    ``local_network_ssl_cert`` / ``_key``: they name the person's own pair or nothing, and
    an own pair is never replaced (``served_certificate``).
    """
    from vaf.core.config import Config

    if not Config.get("local_network_tls_enabled", False):
        return None, None

    # The machine's own names for the HSTS scope, looked up here at the start (getfqdn is a
    # DNS lookup) rather than on the first answer. A certificate issued below looks them up
    # again (_collect_sans), and that lookup renews the memo with the names it carries.
    try:
        from vaf.network.binding import own_name_labels
        own_name_labels()
    except Exception:
        pass

    with _CHANGE_LOCK:
        served = served_certificate()
        if served.source == "own":
            _log_own_certificate(str(served.cert_path))
            return served.cert_path, served.key_path
        if served.problem:
            logger.error("The configured certificate %s cannot be used: %s Serving VAF's own "
                         "certificate for this run; fix the file at that path, or go back with "
                         "`vaf server cert reset`.", served.own_cert_path,
                         REPORT_REASONS.get(served.problem, served.problem))
        return _ensure_automatic()


# ── reading certificates ─────────────────────────────────────────────────────

def certificate_ip_addresses() -> Optional[set]:
    """The IP addresses the certificate in use names, or None when there is none to read.

    For the settings: an address that came up after the start (a VPN switched on later)
    is admitted at once but enters the certificate only at the next start."""
    try:
        leaf = _leaf_of(served_certificate().cert_path)
        if leaf is None:
            return None
        san_ext = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return {str(v) for v in san_ext.get_values_for_type(x509.IPAddress)}
    except Exception:
        return None


def ca_certificate_path() -> Optional[Path]:
    """Where this machine's local CA lives, or None if it has none yet."""
    path = _ssl_dir_path() / "ca.pem"
    return path if path.exists() else None


def ca_fingerprint() -> Optional[str]:
    """The SHA-256 fingerprint of this machine's local CA, lowercase hex, or None.

    What a device that installed VAF's own authority compares, and what ``vaf server ca``
    and the settings show. Stable across server-certificate rotations: the CA is kept when
    only the leaf is reissued. What a room peer pins is ``served_authority``, which is this
    CA while VAF's own certificate is served.
    """
    path = ca_certificate_path()
    if path is None:
        return None
    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
        return cert.fingerprint(hashes.SHA256()).hex()
    except Exception:
        return None


def fingerprint_of(cert_pem: bytes) -> str:
    """The SHA-256 fingerprint of a PEM certificate, lowercase hex."""
    return x509.load_pem_x509_certificate(cert_pem).fingerprint(hashes.SHA256()).hex()


def colon_fingerprint(hex_fingerprint: str) -> str:
    """A fingerprint the way browsers and operating systems show it: AB:CD:..."""
    value = "".join(ch for ch in str(hex_fingerprint or "").upper() if ch in "0123456789ABCDEF")
    return ":".join(value[i:i + 2] for i in range(0, len(value), 2))


def fingerprints_match(left: str, right: str) -> bool:
    """Compare two fingerprints without leaking where they differ.

    Constant time, and tolerant of the shapes a human retypes: colons, spaces and case
    are normalised, because a fingerprint that only matches when pasted perfectly is a
    fingerprint people stop checking.
    """
    import hmac

    def _norm(value: str) -> str:
        return "".join(ch for ch in str(value or "").lower() if ch in "0123456789abcdef")

    a, b = _norm(left), _norm(right)
    if not a or not b:
        return False
    return hmac.compare_digest(a, b)


def _loaded_chain() -> Optional[bytes]:
    """The certificate bytes a running listener of this process loaded, or None."""
    with _LISTENERS_LOCK:
        listeners = [(name, entry, _LOADED_CHAINS.get(name)) for name, entry in _LISTENERS.items()]
    for _name, (server, _loop), chain in listeners:
        config = getattr(server, "config", None)
        if (getattr(config, "ssl", None) is not None and getattr(server, "started", False)
                and not getattr(server, "should_exit", False) and chain):
            return chain
    return None


def served_chain(cfg: Optional[dict] = None) -> List[x509.Certificate]:
    """The certificates devices are served, leaf first; empty while TLS is off or when there
    is none to read. Never reads the private key: what a running listener of this process
    loaded when there is one (kept until a reload succeeds: the file may have been replaced
    by a change whose reload timed out), else ``served_certificate``'s file, whose key check
    is cached."""
    if not _flag(_cfg_get(cfg, "local_network_tls_enabled", False)):
        return []
    data = _loaded_chain() if cfg is None else None
    if data is None:
        served = served_certificate(cfg)
        if served.source == "none" or not served.cert_path:
            return []
        try:
            data = Path(served.cert_path).read_bytes()
        except OSError:
            return []
    try:
        return [x509.load_pem_x509_certificate(b) for b in _pem_blocks(data)[0]]
    except Exception:
        return []


def served_authority(cfg: Optional[dict] = None) -> Optional[Tuple[bytes, str]]:
    """(PEM, SHA-256 hex) of the authority a room peer on another machine pins, or None.

    The LAST certificate of the served file, which is what a peer fetches from the TLS
    chain (``vaf.core.a2a.trust.fetch_authority``): the local CA while VAF's own
    certificate is served (its ``server.pem`` carries the CA last), the top of the chain
    an own certificate came with otherwise. Such a top may vouch for other hosts too (a
    public intermediate does), so the peer also checks that the certificate names the
    address it dialled (``served_room_host``, ``trust.client_context``)."""
    chain = served_chain(cfg)
    if not chain:
        return None
    top = chain[-1]
    pem = top.public_bytes(serialization.Encoding.PEM)
    return pem, top.fingerprint(hashes.SHA256()).hex()


# The overall time the lookups of one room-host choice may take. getaddrinfo has no timeout
# of its own: a .local name nobody answers took 10 s (measured), DNS that is down longer.
_LOOKUP_BUDGET_S = 2.0
# The lookups of every room-host choice in the process share this many threads: lookups
# that hang occupy these and no more, and the ones queued behind them are cancelled when a
# choice's budget is spent (measured before: 800 threads at 20 connections inviting).
_LOOKUP_WORKERS = 8
# An answer is reused this long, so repeated invitations do not ask the resolver again. A
# DNS change shows in an invitation made at most this long after it.
_LOOKUP_MEMO_S = 60.0
_LOOKUP_MEMO: Dict[str, Tuple[float, Any]] = {}
_LOOKUP_LOCK = threading.Lock()
_LOOKUP_POOL = None
# The lookups still running, shared by every choice: a name whose lookup an earlier choice
# started is not asked again, and a later choice waits for it only until that choice's
# budget ran out. While DNS hangs, only the choice that starts a lookup pays the 2 s; every
# choice used to pay it again and start the same lookups once more (measured).
_LOOKUP_IN_FLIGHT = None
# The failures of getaddrinfo that are an answer: the name does not exist, or has no
# address. Anything else (EAI_AGAIN, a resolver that timed out or failed for now, among
# them) is no answer: it is not remembered, and the choice says the name gave none instead
# of advising a DNS fix for a name that may resolve fine a moment later.
_DEFINITE_MISSES = frozenset(code for code in (getattr(socket, "EAI_NONAME", None),
                                               getattr(socket, "EAI_NODATA", None))
                             if code is not None)


def _lookups():
    """(the lookup pool, its running lookups), made on first use."""
    global _LOOKUP_POOL, _LOOKUP_IN_FLIGHT
    from vaf.core.bounded_run import DaemonPool, InFlight
    with _LOOKUP_LOCK:
        if _LOOKUP_POOL is None:
            _LOOKUP_POOL = DaemonPool(_LOOKUP_WORKERS, "vaf-host-lookup")
        if _LOOKUP_IN_FLIGHT is None:
            _LOOKUP_IN_FLIGHT = InFlight()
        return _LOOKUP_POOL, _LOOKUP_IN_FLIGHT


def _remember(key: str, value: Any) -> Any:
    with _LOOKUP_LOCK:
        _LOOKUP_MEMO[key] = (time.monotonic(), value)
    return value


def _remembered(key: str) -> Tuple[bool, Any]:
    with _LOOKUP_LOCK:
        entry = _LOOKUP_MEMO.get(key)
    if entry is not None and time.monotonic() - entry[0] < _LOOKUP_MEMO_S:
        return True, entry[1]
    return False, None


def _addresses_of(name: str) -> frozenset:
    """The addresses ``name`` resolves to here, none when it does not exist (a DNS lookup,
    unbounded: it runs on the lookup pool). The answer is remembered, also when it arrives
    after the choice that asked for it stopped waiting. A resolver that failed for now
    raises: no answer, and nothing remembered (``_DEFINITE_MISSES``)."""
    try:
        found = frozenset(str(info[4][0]).split("%")[0]
                          for info in socket.getaddrinfo(name, None))
    except socket.gaierror as exc:
        if exc.errno not in _DEFINITE_MISSES:
            raise
        found = frozenset()
    except Exception:
        found = frozenset()  # a name the resolver cannot even take, such as an overlong label
    return _remember("dns:" + name, found)


def _machine_names() -> List[str]:
    """This machine's host name, FQDN (a lookup) and ``<host>.local``, remembered like an
    address lookup."""
    from vaf.network.binding import machine_dns_names
    found = list(machine_dns_names())
    return _remember("machine", [*found, _mdns_name(found) or ""])


def _own_addresses(address: str, cfg: Optional[dict]) -> set:
    """The addresses a name of this machine may resolve to: ``address`` and every admitted
    one (a VPN's too, where a peer dials it: a certificate made for the VPN name resolves
    there, not to the LAN address)."""
    mine = {address} if address else set()
    try:
        from vaf.network.binding import access_addresses
        mine.update(str(iface.ip) for iface in access_addresses(cfg))
    except Exception:
        pass
    return mine


_CERT_SHOW = "`vaf server cert show` on the host says why"


def room_host_choice(address: str, cfg: Optional[dict] = None) -> Tuple[Optional[str], Optional[str]]:
    """(the host a room invitation names for a peer on another machine, or None; why not).

    A peer checks the served certificate against the host it dials, so the invitation names
    one the certificate covers: ``address`` (this machine's LAN address) when the served leaf
    names it. Otherwise, for a certificate of the person's own (its chain does not end in the
    local CA), a name of it that is this machine: this machine's host name, FQDN or
    ``<host>.local`` where the certificate covers it (a wildcard included), else one of its
    names that resolves here to ``address`` or another admitted address of this machine. The
    only name of a certificate is checked like any other: a certificate made for a website
    names the website, and a peer sent there would hand its ticket to that server. Never the
    apex beside a wildcard (``example.org`` next to ``*.example.org`` is usually the public
    website), a wildcard, ``localhost`` or an address written as a DNS name (TLS checks an
    address against the address names only). The lookups of every choice share one pool of
    ``_LOOKUP_WORKERS`` threads, a choice waits ``_LOOKUP_BUDGET_S`` for them in all, and an
    answer is reused for ``_LOOKUP_MEMO_S``. Where this machine uses another DNS than the
    devices (split DNS), a name the devices resolve may not resolve here: a line in this
    machine's hosts file, or the name as its host name, makes it count.
    VAF's own certificate names every address this machine had when it was issued; an
    address that came later enters it at the next start or with ``vaf server cert reset``.
    The reasons are worded for whoever reads them, the host or an invited agent."""
    chain = served_chain(cfg)
    if not chain:
        return None, f"TLS serves no certificate ({_CERT_SHOW})"
    try:
        names = _san_names(chain[0])
    except Exception:
        return None, f"the names of the served certificate cannot be read ({_CERT_SHOW})"
    if address and _covers(names, address):
        return address, None
    local_ca = ca_fingerprint()
    if local_ca and fingerprints_match(chain[-1].fingerprint(hashes.SHA256()).hex(), local_ca):
        return None, (f"VAF's own certificate does not name the host's address {address}, "
                      "which came after it was issued: `vaf server cert reset` on the host "
                      "issues one that names it, without a restart")
    candidates: List[str] = []
    wildcards: List[str] = []
    for name in names:
        if not isinstance(name, x509.DNSName):
            continue
        value = name.value.strip().rstrip(".").lower()
        if not value or value == "localhost":
            continue
        if value.startswith("*"):
            wildcards.append(value)
            continue
        try:
            ipaddress.ip_address(value)
            continue
        except ValueError:
            pass
        if value not in candidates:
            candidates.append(value)
    if not candidates and not wildcards:
        return None, (f"the served certificate names neither the host's address {address} "
                      "nor a DNS name a peer could dial: installing a certificate that names "
                      "that address, or a DNS name of the host, fixes that")

    from vaf.core.bounded_run import run_bounded_all
    answers: Dict[str, Any] = {}
    calls: Dict[str, Any] = {}
    for key, call in [("machine", _machine_names),
                      *(("dns:" + c, (lambda n=c: _addresses_of(n))) for c in candidates)]:
        hit, value = _remembered(key)
        if hit:
            answers[key] = value
        else:
            calls[key] = call
    if calls:
        pool, in_flight = _lookups()
        answers.update(run_bounded_all(calls, timeout=_LOOKUP_BUDGET_S, pool=pool,
                                       in_flight=in_flight))
    for mine in answers.get("machine") or []:
        mine = str(mine or "").strip().rstrip(".").lower()
        if mine and mine != "localhost" and _covers(names, mine):
            return mine, None
    own = _own_addresses(address, cfg)
    for candidate in candidates:
        if own & set(answers.get("dns:" + candidate) or ()):
            return candidate, None
    silent = [c for c in candidates if "dns:" + c not in answers]
    if "machine" not in answers:
        silent.append("the host's own names")
    late = (f"; {', '.join(silent)} gave no answer within {_LOOKUP_BUDGET_S:g} s"
            if silent else "")
    if not candidates:
        example = "vaf" + wildcards[0][1:]
        return None, (f"the served certificate names only {', '.join(wildcards)}, and none of "
                      f"the host's names is under it: a host name such as {example} fixes "
                      f"that{late}")
    listed = ", ".join([*candidates, *wildcards])
    if all(c in silent for c in candidates):
        return None, (f"the served certificate names {listed}, and the lookups on the host "
                      f"gave no answer within {_LOOKUP_BUDGET_S:g} s (its resolver may be "
                      "down for now): an invitation made once it answers has the lane")
    one = len(candidates) == 1 and not wildcards
    head = (f"the served certificate names {listed}, but it is not the host's name and does "
            f"not resolve to its address {address} on the host" if one else
            f"the served certificate names {listed}, but none of them is the host's name or "
            f"resolves to its address {address} on the host")
    return None, (f"{head}: making {'it' if one else 'one of them'} the host's name, or "
                  f"pointing {'it' if one else 'one'} at that address in DNS, fixes that. The "
                  "other machines must resolve it to that address too; where the host uses "
                  "another DNS than they do, a line in its hosts file covers the host"
                  f"{late}")


def served_room_host(address: str, cfg: Optional[dict] = None) -> Optional[str]:
    """The host a room invitation names for a peer on another machine, or None
    (``room_host_choice`` says why)."""
    return room_host_choice(address, cfg)[0]


# ── the check of a certificate somebody wants to serve ──────────────────────

# One text per code: the CLI and the logs print it, the settings word the code in the
# person's language and show this text for a code they do not know.
REPORT_REASONS: Dict[str, str] = {
    # errors: nothing is installed
    "too_large": "A part is larger than 64 KiB or holds more than 8 certificates.",
    "unreadable": "No PEM certificate could be read.",
    "missing": "The certificate or key file does not exist.",
    "no_key": "No private key was found, or it could not be read.",
    "key_encrypted": ("The private key is protected by a passphrase. VAF needs it without one: "
                      "`openssl pkey -in key.pem -out plain-key.pem`, or from a PKCS#12 file "
                      "`openssl pkcs12 -in cert.p12 -noenc -out bundle.pem`."),
    "key_mismatch": "The private key does not belong to the certificate.",
    "expired": "The certificate expired on {date}.",
    "not_yet_valid": "The certificate is valid only from {date}.",
    "not_for_servers": ("The certificate is not made for a server: it is an authority, or its "
                        "extended key usage leaves out server authentication."),
    "weak_key": ("The key is too weak or of a kind browsers do not accept: use RSA with at "
                 "least 2048 bits, or ECDSA on P-256 or P-384."),
    "chain_broken": ("The chain is not in order: each certificate must be issued by the one "
                     "after it, from the server certificate up."),
    "no_names": ("The certificate names no host name or address (subject alternative names); "
                 "browsers ignore the common name."),
    # warnings: installed anyway
    "expires_soon": "The certificate expires on {date}, in {days} days.",
    "names_missing": ("The certificate does not name {names}: a device that reaches VAF there "
                      "gets a certificate warning."),
    "no_key_identifiers": ("A certificate of the chain lacks its key identifiers, which strict "
                           "clients (Python since 3.13, VAF's room peers among them) refuse."),
    "no_issuer": ("Only the server certificate was given. Devices that do not have its issuer "
                  "fail, and a room peer would pin the certificate itself, so every renewal "
                  "breaks its pin: add the chain."),
    "authority_changes": ("The authority changes to {fingerprint}. Devices must trust it, and "
                          "a machine in a room here runs `vaf a2a trust <address> --ca-fp "
                          "{fingerprint}` again."),
}


@dataclass
class ReportItem:
    code: str
    text: str
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "text": self.text, "params": dict(self.params)}


@dataclass
class CertificateReport:
    """What the check found. ``errors`` refuse the certificate, ``warnings`` do not;
    ``facts`` describe it. Never any key material."""
    errors: List[ReportItem] = field(default_factory=list)
    warnings: List[ReportItem] = field(default_factory=list)
    facts: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self) -> List[str]:
        return [i.code for i in self.errors] + [i.code for i in self.warnings]

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok,
                "errors": [i.to_dict() for i in self.errors],
                "warnings": [i.to_dict() for i in self.warnings],
                "facts": dict(self.facts)}


class CertificateRefused(Exception):
    """The certificate failed the check; nothing was written."""

    def __init__(self, report: CertificateReport):
        self.report = report
        super().__init__("; ".join(i.text for i in report.errors) or "refused")


def _item(code: str, **params) -> ReportItem:
    return ReportItem(code, REPORT_REASONS[code].format(**params), params)


def _key_description(public_key) -> Tuple[str, Any]:
    if isinstance(public_key, rsa.RSAPublicKey):
        return "RSA", public_key.key_size
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        return "EC", {"secp256r1": "P-256", "secp384r1": "P-384",
                      "secp521r1": "P-521"}.get(public_key.curve.name, public_key.curve.name)
    return type(public_key).__name__.replace("PublicKey", "").lstrip("_"), None


def _weak(public_key) -> bool:
    """Weaker than browsers accept: RSA under 2048 bits, a curve other than P-256 or P-384,
    and every other key type (Ed25519, Ed448, DSA), which browsers do not take."""
    if isinstance(public_key, rsa.RSAPublicKey):
        return public_key.key_size < 2048
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        return public_key.curve.name not in ("secp256r1", "secp384r1")
    return True


def _san_names(cert: x509.Certificate) -> List[x509.GeneralName]:
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    return [n for n in ext if isinstance(n, (x509.DNSName, x509.IPAddress))]


def _covers(names: List[x509.GeneralName], target: str) -> bool:
    """Whether a certificate with these names verifies for ``target``: an address exactly,
    a DNS name exactly or by a wildcard for one label, case-insensitively."""
    try:
        address = ipaddress.ip_address(target)
    except ValueError:
        address = None
    if address is not None:
        return any(isinstance(n, x509.IPAddress) and n.value == address for n in names)
    wanted = target.lower().rstrip(".")
    for name in names:
        if not isinstance(name, x509.DNSName):
            continue
        value = name.value.lower().rstrip(".")
        if value == wanted:
            return True
        if value.startswith("*.") and "." in wanted and wanted.split(".", 1)[1] == value[2:]:
            return True
    return False


def _self_signed(cert: x509.Certificate) -> bool:
    if cert.issuer != cert.subject:
        return False
    try:
        cert.verify_directly_issued_by(cert)
        return True
    except Exception:
        return False


def _facts(certs: List[x509.Certificate]) -> Dict[str, Any]:
    leaf, top = certs[0], certs[-1]
    now = datetime.now(timezone.utc)
    key_type, key_size = _key_description(leaf.public_key())
    return {
        "subject": leaf.subject.rfc4514_string(),
        "issuer": leaf.issuer.rfc4514_string(),
        "names": [str(n.value) for n in _san_names(leaf)],
        "not_before": leaf.not_valid_before_utc.isoformat(),
        "not_after": leaf.not_valid_after_utc.isoformat(),
        "days_left": (leaf.not_valid_after_utc - now).days,
        "key_type": key_type,
        "key_size": key_size,
        "sha256": leaf.fingerprint(hashes.SHA256()).hex(),
        "authority_subject": top.subject.rfc4514_string(),
        "authority_sha256": top.fingerprint(hashes.SHA256()).hex(),
        "self_signed": _self_signed(leaf),
        "chain_length": len(certs),
    }


def _default_reach() -> Tuple[List[str], List[str]]:
    """The addresses devices are admitted on now, and the host name."""
    addresses: List[str] = []
    try:
        from vaf.network.binding import access_addresses
        addresses = [i.ip for i in access_addresses()]
    except Exception:
        pass
    names: List[str] = []
    try:
        host = socket.gethostname().strip().lower()
        if host and host != "localhost":
            names.append(host)
    except Exception:
        pass
    return addresses, names


def uncovered_names(names: List[x509.GeneralName], addresses: Optional[List[str]] = None,
                    host_names: Optional[List[str]] = None) -> List[str]:
    """The admitted addresses and the host name a certificate with these names does not
    cover, in that order."""
    if addresses is None or host_names is None:
        default_addresses, default_names = _default_reach()
        addresses = default_addresses if addresses is None else addresses
        host_names = default_names if host_names is None else host_names
    missing: List[str] = []
    for target in [*addresses, *host_names]:
        if target and target not in missing and not _covers(names, target):
            missing.append(target)
    return missing


@dataclass
class _Bundle:
    certs: List[x509.Certificate]
    key: Any


def _read_bundle(cert_pem: bytes, key_pem: bytes, chain_pem: bytes,
                 report: CertificateReport) -> Optional[_Bundle]:
    """The certificates (leaf first) and the key of an upload, or None with the error in the
    report. The certificate part may carry the chain and the key as well (one PEM bundle);
    a key given on its own wins."""
    parts = [bytes(cert_pem or b""), bytes(key_pem or b""), bytes(chain_pem or b"")]
    if any(len(p) > PEM_PART_MAX_BYTES for p in parts):
        report.errors.append(_item("too_large"))
        return None
    cert_blocks, bundled_keys = _pem_blocks(parts[0])
    _ignored, given_keys = _pem_blocks(parts[1])
    chain_blocks, chain_keys = _pem_blocks(parts[2])
    blocks = cert_blocks + chain_blocks
    if len(blocks) > CHAIN_MAX_CERTIFICATES:
        report.errors.append(_item("too_large"))
        return None
    if not blocks:
        report.errors.append(_item("unreadable"))
        return None
    try:
        certs = [x509.load_pem_x509_certificate(b) for b in blocks]
        # cryptography parses the extensions and the public key on first access; one that
        # does not parse (a broken subject alternative name, a key algorithm it does not
        # know) raised from the middle of the check before.
        for cert in certs:
            _ = cert.extensions
            _ = cert.public_key()
    except Exception:
        report.errors.append(_item("unreadable"))
        return None
    keys = given_keys or bundled_keys or chain_keys
    key = None
    if not keys:
        report.errors.append(_item("no_key"))
    else:
        problem, key = _key_problem(keys[0])
        if problem:
            report.errors.append(_item(problem))
    return _Bundle(certs, key)


def _pem_of(certs: List[x509.Certificate]) -> bytes:
    return b"".join(c.public_bytes(serialization.Encoding.PEM) for c in certs)


def _key_pem_of(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _throwaway_load(certs: List[x509.Certificate], key) -> Optional[str]:
    """Load the pair into a throwaway server context, the check uvicorn itself makes. From a
    private temporary directory (0700, removed at once): the ssl module loads files only."""
    with tempfile.TemporaryDirectory(prefix="vaf-cert-check-") as tmp:
        cert_file = Path(tmp) / "cert.pem"
        key_file = Path(tmp) / "key.pem"
        cert_file.write_bytes(_pem_of(certs))
        fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(_key_pem_of(key))
        return _load_problem(str(cert_file), str(key_file))


def inspect_certificate(cert_pem: bytes, key_pem: bytes = b"", chain_pem: bytes = b"", *,
                        addresses: Optional[List[str]] = None,
                        host_names: Optional[List[str]] = None,
                        current_authority: Optional[str] = None) -> CertificateReport:
    """Check a certificate before it is served. Writes nothing that outlives the call.

    ``addresses`` / ``host_names`` are what the name check holds the certificate to (the
    admitted addresses and the host name by default); ``current_authority`` is the
    fingerprint served now (``served_authority`` by default, "" for none)."""
    report = CertificateReport()
    bundle = _read_bundle(cert_pem, key_pem, chain_pem, report)
    if bundle is None:
        return report
    certs = bundle.certs
    leaf = certs[0]
    report.facts = _facts(certs)
    now = datetime.now(timezone.utc)

    if leaf.not_valid_after_utc <= now:
        report.errors.append(_item("expired", date=leaf.not_valid_after_utc.date().isoformat()))
    elif leaf.not_valid_before_utc > now:
        report.errors.append(_item("not_yet_valid",
                                   date=leaf.not_valid_before_utc.date().isoformat()))
    try:
        if leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            report.errors.append(_item("not_for_servers"))
    except x509.ExtensionNotFound:
        pass
    if "not_for_servers" not in report.codes():
        try:
            usages = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            if ExtendedKeyUsageOID.SERVER_AUTH not in list(usages):
                report.errors.append(_item("not_for_servers"))
        except x509.ExtensionNotFound:
            pass
    if _weak(leaf.public_key()):
        report.errors.append(_item("weak_key"))
    for lower, upper in zip(certs, certs[1:]):
        try:
            lower.verify_directly_issued_by(upper)
        except Exception:
            report.errors.append(_item("chain_broken"))
            break
    names = _san_names(leaf)
    if not names:
        report.errors.append(_item("no_names"))
    if bundle.key is not None and "weak_key" not in report.codes():
        problem = _throwaway_load(certs, bundle.key)
        if problem and problem not in report.codes():
            report.errors.append(_item(problem if problem in REPORT_REASONS else "key_mismatch"))

    days = (leaf.not_valid_after_utc - now).days
    if leaf.not_valid_after_utc > now and days < RENEW_BEFORE_DAYS:
        report.warnings.append(_item("expires_soon", days=days,
                                     date=leaf.not_valid_after_utc.date().isoformat()))
    if names:
        missing = uncovered_names(names, addresses, host_names)
        if missing:
            report.warnings.append(_item("names_missing", names=", ".join(missing)))
    self_signed = _self_signed(leaf)
    identifiers = _cert_has_key_identifiers(leaf, is_ca=self_signed) and all(
        _cert_has_key_identifiers(c, is_ca=True) for c in certs[1:])
    if not identifiers:
        report.warnings.append(_item("no_key_identifiers"))
    if not self_signed and len(certs) == 1:
        report.warnings.append(_item("no_issuer"))
    if current_authority is None:
        served = served_authority()
        current_authority = served[1] if served else ""
    top = report.facts["authority_sha256"]
    if current_authority and not fingerprints_match(current_authority, top):
        report.warnings.append(_item("authority_changes", fingerprint=colon_fingerprint(top)))
    return report


# ── switching the served pair ────────────────────────────────────────────────

@dataclass
class ReloadResult:
    """What the reload did. ``listeners`` maps each registered listener to ``loaded``,
    ``restored`` (the new pair failed on it and the previous one was loaded back),
    ``failed`` (neither loads: its handshakes fail until a restart), ``timeout`` (its loop
    did not get to the load in time; it was cancelled and the previous pair stays),
    ``unconfirmed`` (the load started and had not finished in time) or ``not_started``.
    ``refused`` is the problem code when the served pair failed the throwaway check and no
    listener was touched."""
    listeners: Dict[str, str] = field(default_factory=dict)
    refused: Optional[str] = None
    source: str = "none"

    @property
    def ok(self) -> bool:
        return self.refused is None and all(
            s in ("loaded", "not_started") for s in self.listeners.values())

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "listeners": dict(self.listeners), "refused": self.refused,
                "source": self.source}


@dataclass
class ChangeResult:
    """What a change of the served pair did: the check's report (an install), the reload
    (None when the caller reloads elsewhere), and the authority a room peer pins before and
    after (for a renewal, the local CA's)."""
    report: Optional[CertificateReport] = None
    reload: Optional[ReloadResult] = None
    old_authority: Optional[str] = None
    new_authority: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"report": self.report.to_dict() if self.report else None,
                "reload": self.reload.to_dict() if self.reload else None,
                "old_authority": self.old_authority, "new_authority": self.new_authority}


def _run_on_loop(loop, fn):
    """Run ``fn`` on the listener's own loop and wait for it; directly when that loop is not
    running or is the one running here.

    A loop that does not get to it within ``_RELOAD_WAIT_S`` (a blocked listener) has it
    cancelled, so the pair it serves stays as it is and the answer says so: ``timeout``. One
    that started it and did not finish in time may still apply it: ``unconfirmed``. Which of
    the two is decided under one lock with the call itself, so a call that starts while the
    wait ends is either cancelled or reported as started, never both."""
    import asyncio
    import concurrent.futures
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is None or running is loop or not getattr(loop, "is_running", lambda: False)():
        return fn()
    done: "concurrent.futures.Future" = concurrent.futures.Future()
    gate = threading.Lock()
    state = {"started": False, "cancelled": False}

    def _call() -> None:
        with gate:
            if state["cancelled"]:
                return
            state["started"] = True
        try:
            done.set_result(fn())
        except BaseException as e:  # noqa: BLE001 - handed back to the waiting thread
            done.set_exception(e)

    handle = loop.call_soon_threadsafe(_call)
    try:
        return done.result(timeout=_RELOAD_WAIT_S)
    except concurrent.futures.TimeoutError:
        with gate:
            if state["started"]:
                return "unconfirmed"
            state["cancelled"] = True
        handle.cancel()
        return "timeout"
    except Exception:
        return "failed"


def _reload_listener(name: str, server, loop, cert: str, key: str, chain: bytes) -> str:
    config = getattr(server, "config", None)
    context = getattr(config, "ssl", None)
    if (context is None or not getattr(server, "started", False)
            or getattr(server, "should_exit", False)):
        return "not_started"
    previous = (getattr(config, "ssl_certfile", None), getattr(config, "ssl_keyfile", None))

    def _load() -> str:
        try:
            context.load_cert_chain(cert, key, password=_no_passphrase)
        except Exception:
            # The live context lost its pair with the failed load (measured): load the
            # previous one back so the listener keeps serving it.
            try:
                context.load_cert_chain(previous[0], previous[1], password=_no_passphrase)
                return "restored"
            except Exception:
                return "failed"
        config.ssl_certfile, config.ssl_keyfile = cert, key
        with _LISTENERS_LOCK:
            _LOADED_CHAINS[name] = chain
        return "loaded"

    return _run_on_loop(loop, _load)


def prune_custom_certificates() -> None:
    """Remove files in ``custom/`` that neither the configuration nor a running listener
    names any more. A listener that kept its previous pair keeps that pair's files."""
    custom = _ssl_dir_path() / _CUSTOM_DIR_NAME
    if not custom.is_dir():
        return
    keep: List[str] = [str(_cfg_get(None, k, "") or "").strip() for k in CERTIFICATE_KEYS]
    with _LISTENERS_LOCK:
        listeners = list(_LISTENERS.values())
    for server, _loop in listeners:
        config = getattr(server, "config", None)
        keep += [str(getattr(config, "ssl_certfile", "") or ""),
                 str(getattr(config, "ssl_keyfile", "") or "")]
    kept = {os.path.normcase(str(Path(p).resolve())) for p in keep if p}
    for path in custom.iterdir():
        try:
            if path.is_file() and os.path.normcase(str(path.resolve())) not in kept:
                path.unlink()
        except OSError:
            pass


def reload_certificate() -> ReloadResult:
    """Load the served pair into every registered listener, without a restart.

    The pair is resolved as at the start (``ensure_ssl_certificates``) and loaded into a
    throwaway context first: when that fails, no listener is touched. Then each listener
    loads it on its own loop; one whose load fails gets its previous pair back. New
    connections get the new pair, connections already open keep theirs (measured on
    uvicorn 0.49: ``server.config.ssl`` is the context it built at ``config.load()``).
    Files of earlier own certificates are removed once nothing names them."""
    with _CHANGE_LOCK:
        try:
            cert, key = ensure_ssl_certificates()
        except Exception as e:
            logger.error("Certificate reload: the served pair could not be prepared: %s", e)
            return ReloadResult(refused="unreadable")
        if not cert or not key:
            prune_custom_certificates()
            return ReloadResult(source="none")
        source = served_certificate().source
        problem = _load_problem(cert, key)
        if problem:
            logger.error("Certificate reload refused, nothing changed: %s",
                         REPORT_REASONS.get(problem, problem))
            return ReloadResult(refused=problem, source=source)
        try:
            chain = Path(cert).read_bytes()
        except OSError:
            return ReloadResult(refused="missing", source=source)
        with _LISTENERS_LOCK:
            listeners = list(_LISTENERS.items())
        result = ReloadResult(source=source)
        for name, (server, loop) in listeners:
            result.listeners[name] = _reload_listener(name, server, loop, cert, key, chain)
        for name, status in result.listeners.items():
            if status in ("restored", "failed"):
                logger.error("Certificate reload: the %s listener %s.", name,
                             "kept the previous certificate" if status == "restored"
                             else "has no certificate now; restart VAF")
        prune_custom_certificates()
        return result


def _authority_now() -> str:
    served = served_authority()
    return served[1] if served else ""


def _event(detail: str, via: str, username: str) -> None:
    """The security event of a change: what is served now and, in ``via``, where the change
    was made ("in Settings", "in a terminal on this machine")."""
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("tls_certificate_changed", username=str(username or ""),
                           detail=f"{detail}, {via}" if via else detail)
    except Exception:
        pass


def install_certificate(cert_pem: bytes, key_pem: bytes = b"", chain_pem: bytes = b"", *,
                        username: str = "", via: str = "", reload: bool = True) -> ChangeResult:
    """Serve an own certificate from now on.

    Checked first (``inspect_certificate``): any error raises ``CertificateRefused`` with
    nothing written. Then the chain (leaf first) and the key are written to
    ``custom/<first 16 hex of the leaf's fingerprint>.pem`` / ``.key`` (the key owner-only
    from creation), both keys switch in ONE save, and the running listeners reload
    (``reload=False`` when the caller asks the running VAF to reload instead)."""
    from vaf.core.config import Config
    from vaf.core.secure_store import harden_dir

    with _CHANGE_LOCK:
        report = inspect_certificate(cert_pem, key_pem, chain_pem)
        if not report.ok:
            raise CertificateRefused(report)
        report_probe = CertificateReport()
        bundle = _read_bundle(cert_pem, key_pem, chain_pem, report_probe)
        if bundle is None or bundle.key is None:  # pragma: no cover - the check above read it
            raise CertificateRefused(report_probe)
        old_authority = _authority_now()
        leaf_fp = report.facts["sha256"]
        custom = _get_ssl_dir() / _CUSTOM_DIR_NAME
        custom.mkdir(parents=True, exist_ok=True)
        harden_dir(custom)
        cert_path = custom / f"{leaf_fp[:16]}.pem"
        key_path = custom / f"{leaf_fp[:16]}.key"
        _write_secret(key_path, _key_pem_of(bundle.key))
        _write_public(cert_path, _pem_of(bundle.certs))
        with Config._locked():
            cfg = Config.load()
            cfg[CERT_KEY] = str(cert_path)
            cfg[KEY_KEY] = str(key_path)
            Config.save(cfg)
        reloaded = reload_certificate() if reload else None
        new_authority = report.facts["authority_sha256"]
        _event(f"own certificate, leaf {leaf_fp[:16]}, authority {new_authority[:16]}", via,
               username)
        return ChangeResult(report=report, reload=reloaded, old_authority=old_authority or None,
                            new_authority=new_authority)


def reset_certificate(*, username: str = "", via: str = "", reload: bool = True) -> ChangeResult:
    """Go back to VAF's own certificate: both keys cleared in ONE save and a freshly issued
    server certificate from the KEPT local CA, so every device that installed it keeps
    trusting this machine (deleting ``~/.vaf/ssl`` would replace the CA too)."""
    from vaf.core.config import Config

    with _CHANGE_LOCK:
        old_authority = _authority_now()
        with Config._locked():
            cfg = Config.load()
            cfg[CERT_KEY] = ""
            cfg[KEY_KEY] = ""
            Config.save(cfg)
        if Config.get("local_network_tls_enabled", False):
            _ensure_automatic(reissue=True)
        reloaded = reload_certificate() if reload else None
        if not reload:
            prune_custom_certificates()
        new_authority = _authority_now()
        _event(f"VAF's own certificate, authority {new_authority[:16]}", via, username)
        return ChangeResult(reload=reloaded, old_authority=old_authority or None,
                            new_authority=new_authority or None)


def renew_authority(*, username: str = "", via: str = "", reload: bool = True) -> ChangeResult:
    """Replace the local CA with a new, restricted one and issue VAF's own server
    certificate from it. The old CA key is gone (the new file replaces it); an own
    certificate stays served. Every device that installed the old CA installs the new one,
    and machines in rooms here trust the new fingerprint."""
    with _CHANGE_LOCK:
        ssl_dir = _get_ssl_dir()
        old = ca_fingerprint()
        ca_key, ca_cert = _generate_ca(ssl_dir)
        _generate_server_cert(ssl_dir, ca_key, ca_cert)
        new = ca_cert.fingerprint(hashes.SHA256()).hex()
        reloaded = None
        if reload and served_certificate().source == "automatic":
            reloaded = reload_certificate()
        _event(f"authority renewed, old {str(old or '')[:16]}, new {new[:16]}", via, username)
        return ChangeResult(reload=reloaded, old_authority=old, new_authority=new)


def without_certificate_keys(payload):
    """A Settings save without the two certificate keys. They change only through the
    checked route (``install_certificate``, behind a second factor): a save sends the whole
    page, and a stolen session could otherwise point them at a file it planted. Both save
    lanes pass it: the WebSocket ``save_config`` and ``PATCH /api/config``."""
    if isinstance(payload, dict) and any(k in payload for k in CERTIFICATE_KEYS):
        return {k: v for k, v in payload.items() if k not in CERTIFICATE_KEYS}
    return payload


# ── the state, for the settings, the CLI and the doctor ─────────────────────

def local_authority() -> Optional[Dict[str, Any]]:
    """The local CA: path, fingerprint, valid until, restricted, and for an unrestricted one
    the machine's names a renewal would leave out of VAF's own certificate. Read-only."""
    path = ca_certificate_path()
    if path is None:
        return None
    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
    except Exception:
        return None
    restricted = _constraints_of(cert) is not None
    return {"path": str(path), "fingerprint": cert.fingerprint(hashes.SHA256()).hex(),
            "not_after": cert.not_valid_after_utc.isoformat(),
            "restricted": restricted,
            "left_out_on_renewal": [] if restricted else names_a_new_authority_leaves_out()}


def certificate_state(cfg: Optional[dict] = None, *, step_up_required: bool = False) -> Dict[str, Any]:
    """Everything the settings, ``vaf server cert show``, ``vaf server ca`` and the doctor
    show about the served certificate. Read-only: creates nothing, writes nothing."""
    served = served_certificate(cfg)
    state: Dict[str, Any] = {
        "tls": served.source != "none",
        "source": served.source,
        "own_configured": bool(served.own_cert_path),
        "own_path": served.own_cert_path or None,
        "problem": ({"code": served.problem, "text": REPORT_REASONS.get(served.problem, "")}
                    if served.problem else None),
        "certificate": None,
        "authority": None,
        "local_ca": local_authority(),
        "uncovered": [],
        "mdns_name": mdns_name(),
        "step_up_required": bool(step_up_required),
    }
    if served.source == "none" or not served.cert_path:
        return state
    try:
        blocks = _pem_blocks(Path(served.cert_path).read_bytes())[0]
        certs = [x509.load_pem_x509_certificate(b) for b in blocks]
        facts = _facts(certs) if certs else None
    except Exception:
        certs, facts = [], None
    if not certs or facts is None:
        return state
    state["certificate"] = facts
    state["authority"] = {"subject": facts["authority_subject"],
                          "fingerprint": facts["authority_sha256"]}
    addresses = None
    if cfg is not None:
        try:
            from vaf.network.binding import access_addresses
            addresses = [i.ip for i in access_addresses(cfg)]
        except Exception:
            addresses = []
    state["uncovered"] = uncovered_names(_san_names(certs[0]), addresses)
    return state
