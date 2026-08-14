# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Deciding which machine a room peer is willing to talk to.

The whole of it: a fingerprint arrives by one route, a certificate by another, and they
have to agree. That is what separates this from trust on first use - the number came to
the human in the invitation, so the certificate cannot vouch for itself.

WHAT IS PINNED IS THE AUTHORITY, NEVER THE SERVER CERTIFICATE. The leaf is reissued with
a fresh key whenever the machine's LAN address changes, which an ordinary DHCP lease does
on its own; pinning it would turn a router reboot into a broken room. The CA is kept
across those rotations on purpose, so it is the stable thing to point at.

There is no way to say "connect anyway". A connection whose certificate cannot be
verified is refused, and a test greps this module for the shapes that would undo that -
because "just this once" is how verification dies, and an unverified channel turns a join
ticket into a credential somebody else can harvest.
"""
from __future__ import annotations

import socket
import ssl
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

from vaf.core.a2a.store import check_name
from vaf.core.platform import Platform
from vaf.core.secure_store import harden_dir


class TrustRefused(Exception):
    """The certificate on offer is not the one the invitation named."""


def trust_dir() -> Path:
    """Where pinned authorities live: one PEM per host, owner-only."""
    directory = Path(Platform.vaf_dir()) / "a2a" / "trust"
    directory.mkdir(parents=True, exist_ok=True)
    harden_dir(directory)
    return directory


def _host_port(url: str) -> Tuple[str, int]:
    parsed = urlparse(url if "://" in url else f"wss://{url}")
    host = parsed.hostname or ""
    if not host:
        raise TrustRefused(f"no host in {url!r}")
    return host, int(parsed.port or 443)


def anchor_path(url: str) -> Path:
    """The pinned CA for a host, whether or not it exists yet."""
    host, port = _host_port(url)
    # The file name is a path component built from something a stranger supplies.
    safe = check_name(f"{host.replace(':', '-')}-{port}", what="host")
    return trust_dir() / f"{safe}.pem"


def fetch_authority(url: str) -> bytes:
    """The CA the host offers, as PEM, fetched WITHOUT trusting it.

    Unverified on purpose and only here: this is the fetch whose result the caller is
    about to check against a fingerprint that arrived by another route. Nothing is
    stored and nothing is spoken to until that check passes, so the connection made
    here carries no secret and accepts no data.
    """
    host, port = _host_port(url)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                chain = tls.get_unverified_chain()
    except Exception as e:
        raise TrustRefused(f"could not reach {host}:{port} - {e}") from None
    if not chain:
        raise TrustRefused(f"{host}:{port} offered no certificate chain")
    # The authority is the last link: the leaf comes first, its issuer after it.
    return ssl.DER_cert_to_PEM_cert(chain[-1]).encode("ascii")


def pin_authority(url: str, *, expected_fingerprint: str,
                  ca_file: Optional[Path] = None) -> Tuple[Path, str]:
    """Store a host's CA, and only if it is the one the invitation named.

    Returns (where it was stored, its fingerprint). Raises rather than returning a
    verdict, so a caller cannot proceed by forgetting to look.
    """
    from vaf.network.ssl_utils import fingerprint_of, fingerprints_match

    if not expected_fingerprint:
        raise TrustRefused(
            "no fingerprint to check against - an unchecked certificate is not trust, "
            "it is whoever answered the address")

    pem = ca_file.read_bytes() if ca_file else fetch_authority(url)
    try:
        actual = fingerprint_of(pem)
    except Exception as e:
        raise TrustRefused(f"that is not a certificate - {e}") from None

    if not fingerprints_match(actual, expected_fingerprint):
        raise TrustRefused(
            f"the certificate at {url} does not match the invitation: expected "
            f"{expected_fingerprint[:16]}..., found {actual[:16]}...")

    target = anchor_path(url)
    target.write_bytes(pem)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target, actual


def client_context(url: str) -> ssl.SSLContext:
    """A TLS context that verifies the pinned authority for this host, and nothing less.

    Hostname checking is off and the reason is not laziness: rooms are dialled by IP
    address, and the certificate's names cover the addresses this machine had when it
    was issued. What replaces it is stronger than a name match, because the authority
    is pinned per host rather than taken from a store of hundreds: only the machine
    whose CA was carried across in the invitation can present a certificate this
    context accepts.
    """
    anchor = anchor_path(url)
    if not anchor.exists():
        raise TrustRefused(
            f"nothing trusted for {url} yet - run: vaf a2a trust {url} --ca-fp <fingerprint>")
    context = ssl.create_default_context(cafile=str(anchor))
    # Strictness is DEMANDED, not inherited: Python 3.13 turns VERIFY_X509_STRICT
    # on by default and 3.10-3.12 do not, so a context that relied on the default
    # verified less on exactly the interpreters most installs run. Found by CI
    # running the suite on 3.10 while every local run was 3.13 - the class of
    # failure the hostile-env script cannot catch, because it shares the
    # interpreter with the developer.
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    context.check_hostname = False
    return context
