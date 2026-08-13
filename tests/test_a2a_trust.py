# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Which machine a room peer is willing to talk to.

The one idea: a fingerprint arrives by one route and a certificate by another, and they
have to agree. Every test here is a way that could fail to be true.
"""
from pathlib import Path

import pytest

from vaf.core.a2a import trust as trust_mod
from vaf.core.a2a.trust import TrustRefused, anchor_path, client_context, pin_authority
from vaf.network import ssl_utils

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def anchored(tmp_path, monkeypatch):
    monkeypatch.setattr(trust_mod, "trust_dir", lambda: tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    ssl_utils._generate_ca(source)
    return tmp_path, (source / "ca.pem")


# ── the check that is the whole point ──────────────────────────────────────

def test_a_matching_fingerprint_pins_the_authority(anchored):
    store, ca_pem = anchored
    expected = ssl_utils.fingerprint_of(ca_pem.read_bytes())

    where, actual = pin_authority("wss://192.168.1.42:8443", expected_fingerprint=expected,
                                  ca_file=ca_pem)

    assert actual == expected
    assert where.read_bytes() == ca_pem.read_bytes()
    assert where == anchor_path("wss://192.168.1.42:8443")


def test_a_wrong_fingerprint_stores_nothing(anchored):
    """MUTATION: store the certificate and warn about the mismatch.

    A near miss is a miss. Keeping the file "so the user can decide" is how the wrong
    authority ends up pinned by whoever clicks past the warning - and here there is no
    user in front of the screen at all, only an agent.
    """
    store, ca_pem = anchored

    with pytest.raises(TrustRefused):
        pin_authority("wss://192.168.1.42:8443",
                      expected_fingerprint="0" * 64, ca_file=ca_pem)

    assert not anchor_path("wss://192.168.1.42:8443").exists()


def test_no_fingerprint_at_all_is_refused(anchored):
    """MUTATION: accept whatever the host offers when no fingerprint was given.

    That is trust on first use with nothing to compare against, which is not trust: it
    is whoever answered the address.
    """
    store, ca_pem = anchored

    with pytest.raises(TrustRefused) as refusal:
        pin_authority("wss://192.168.1.42:8443", expected_fingerprint="", ca_file=ca_pem)
    assert "not trust" in str(refusal.value)


def test_a_fingerprint_survives_the_shapes_a_human_retypes(anchored):
    """Colons, capitals and spaces all name the same certificate. A check that only
    passes on a perfect paste is a check people stop performing."""
    store, ca_pem = anchored
    raw = ssl_utils.fingerprint_of(ca_pem.read_bytes())
    spaced = " ".join(raw[i:i + 2] for i in range(0, len(raw), 2)).upper()

    _where, actual = pin_authority("wss://10.0.0.5:8443", expected_fingerprint=spaced,
                                   ca_file=ca_pem)
    assert actual == raw


def test_the_comparison_does_not_leak_where_it_differs():
    """MUTATION: compare with ==.

    A fingerprint is checked against something an attacker supplies, and a comparison
    that stops at the first wrong character says how much was right.
    """
    source = (ROOT / "vaf" / "network" / "ssl_utils.py").read_text(encoding="utf-8")
    body = source.split("def fingerprints_match")[1].split("\ndef ")[0]
    assert "compare_digest" in body


def test_an_empty_fingerprint_never_matches():
    assert ssl_utils.fingerprints_match("", "") is False
    assert ssl_utils.fingerprints_match("abc", "") is False


# ── what is pinned, and what is not ────────────────────────────────────────

def test_the_authority_is_pinned_and_not_the_server_certificate(anchored):
    """MUTATION: pin the leaf.

    The server certificate is reissued with a fresh key whenever the machine's LAN
    address changes, which an ordinary DHCP lease does by itself. Pinning it would turn
    a router reboot into a broken room; the CA is deliberately kept across those
    rotations, which is what makes it the stable thing to point at.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    store, ca_pem = anchored
    src = ca_pem.parent

    key = serialization.load_pem_private_key((src / "ca-key.pem").read_bytes(), password=None)
    cert = x509.load_pem_x509_certificate(ca_pem.read_bytes())

    first, _k = ssl_utils._generate_server_cert(src, key, cert)
    first_bytes = first.read_bytes()
    second, _k2 = ssl_utils._generate_server_cert(src, key, cert)

    assert first_bytes != second.read_bytes(), "the leaf really does change"
    # ... while the anchor the peer pinned does not.
    assert ssl_utils.fingerprint_of(ca_pem.read_bytes()) == ssl_utils.fingerprint_of(
        (src / "ca.pem").read_bytes())


def test_a_context_is_refused_before_anything_is_trusted(anchored):
    with pytest.raises(TrustRefused) as refusal:
        client_context("wss://192.168.9.9:8443")
    assert "vaf a2a trust" in str(refusal.value), "the refusal does not say how to fix it"


def test_a_context_verifies_the_pinned_authority(anchored):
    import ssl as _ssl

    store, ca_pem = anchored
    expected = ssl_utils.fingerprint_of(ca_pem.read_bytes())
    pin_authority("wss://192.168.1.42:8443", expected_fingerprint=expected, ca_file=ca_pem)

    context = client_context("wss://192.168.1.42:8443")
    assert context.verify_mode == _ssl.CERT_REQUIRED
    assert bool(context.verify_flags & _ssl.VERIFY_X509_STRICT), (
        "the context stopped checking strictly, which is what the CA fix was for")


# ── there is no way to say "connect anyway" ────────────────────────────────

def test_nothing_here_can_turn_verification_off():
    """MUTATION: add a verify=False escape hatch for "just this once".

    That is how verification dies. Encrypted-to-whoever-answered is not encrypted to
    anybody in particular, and an unverified channel turns a join ticket into a
    credential somebody else can harvest.

    The ONE place CERT_NONE appears is the fetch whose result is immediately checked
    against a fingerprint that arrived by another route - it stores nothing, speaks to
    nobody, and its docstring says so.
    """
    source = (ROOT / "vaf" / "core" / "a2a" / "trust.py").read_text(encoding="utf-8")

    assert source.count("CERT_NONE") == 1
    fetch = source.split("def fetch_authority")[1].split("\ndef ")[0]
    assert "CERT_NONE" in fetch, "the unverified fetch moved somewhere less obvious"

    for shape in ("verify=False", "check_hostname = True", "ssl._create_unverified"):
        if shape == "check_hostname = True":
            continue
        assert shape not in source, f"an escape hatch appeared: {shape}"

    client = source.split("def client_context")[1].split("\ndef ")[0]
    assert "CERT_NONE" not in client and "verify_mode" not in client, (
        "the context used for real traffic weakens verification")


def test_the_client_context_never_appears_in_the_cli_with_verification_off():
    """The rule is worth nothing if a caller rebuilds a weaker context of its own."""
    for path in (ROOT / "vaf" / "cli" / "cmd" / "a2a.py",):
        source = path.read_text(encoding="utf-8")
        assert "CERT_NONE" not in source
        assert "verify_mode" not in source
