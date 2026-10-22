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


# ── the authority a peer pins is the one the served chain ends in ──────────────

def _serve_tls(cert_file, key_file):
    """A TLS listener on a free loopback port serving this pair to every connection, until
    the returned stop is called."""
    import socket
    import ssl as _ssl
    import threading

    from cert_factory import server_context
    context = server_context(cert_file, key_file)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.2)
    stopped = threading.Event()

    def _accept():
        while not stopped.is_set():
            try:
                raw, _addr = listener.accept()
            except OSError:
                continue
            try:
                with context.wrap_socket(raw, server_side=True) as tls:
                    tls.recv(1)
            except (OSError, _ssl.SSLError):
                pass
    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()

    def _stop():
        stopped.set()
        thread.join(timeout=5)
        listener.close()
    return listener.getsockname()[1], _stop


def _own_chain_served(tmp_path, monkeypatch, *, with_root: bool):
    import vaf.core.a2a.invite as invite_mod
    import vaf.network.binding as binding
    from cert_factory import isolated_home, key_pem, own_chain, pem

    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [("lo", "127.0.0.1")])
    monkeypatch.setattr(binding, "local_interfaces", lambda: [])
    monkeypatch.setattr(binding, "get_local_network_ip", lambda: "127.0.0.1")
    isolated_home(tmp_path, monkeypatch, local_network_enabled=True,
                  local_network_tls_enabled=True)
    monkeypatch.setattr(trust_mod, "trust_dir", lambda: tmp_path / "trust")
    (tmp_path / "trust").mkdir()
    leaf, key, inter, root = own_chain(names=("127.0.0.1",))
    chain = pem(leaf, inter, root) if with_root else pem(leaf, inter)
    ssl_utils.install_certificate(chain, key_pem(key))
    served = ssl_utils.served_certificate()
    port, stop = _serve_tls(served.cert_path, served.key_path)
    monkeypatch.setattr(invite_mod, "_effective_port", lambda: (port, True))
    return invite_mod.lan_endpoint("room-x"), (inter, root), port, stop


@pytest.mark.skipif(not hasattr(__import__("ssl").SSLSocket, "get_unverified_chain"),
                    reason="the peer reads the chain with get_unverified_chain (Python 3.13)")
@pytest.mark.parametrize("with_root", [True, False])
def test_an_own_chain_is_the_authority_peers_pin(tmp_path, monkeypatch, with_root):
    """AC 15: the invitation names the top of the chain this machine serves, and a peer that
    fetches it over TLS (no ca_file) pins it and then verifies a real handshake. The chain
    without its root pins the intermediate. MUTATION: keep ca_fingerprint() in lan_endpoint
    - red (TrustRefused: the invitation names an authority the chain does not end in)."""
    import socket

    from cert_factory import fingerprint
    endpoint, (inter, root), port, stop = _own_chain_served(tmp_path, monkeypatch,
                                                          with_root=with_root)
    try:
        expected = fingerprint(root if with_root else inter)
        assert endpoint["ca_fingerprint"] == expected
        url = f"wss://127.0.0.1:{port}"
        _where, pinned = pin_authority(url, expected_fingerprint=endpoint["ca_fingerprint"])
        assert pinned == expected
        context = client_context(url)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            # As every room client does: the host of the room URL, which the name check reads.
            with context.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
                assert tls.getpeercert(binary_form=True)
    finally:
        stop()


def test_the_download_serves_the_authority_the_invitation_names(tmp_path, monkeypatch):
    """AC 15: /api/a2a/ca.pem and the invitation read one function."""
    from cert_factory import fingerprint
    endpoint, (_inter, root), _port, stop = _own_chain_served(tmp_path, monkeypatch,
                                                             with_root=True)
    stop()
    authority = ssl_utils.served_authority()
    assert authority[1] == endpoint["ca_fingerprint"] == fingerprint(root)
    assert ssl_utils.fingerprint_of(authority[0]) == fingerprint(root)


def test_automatic_authority_is_the_local_ca(tmp_path, monkeypatch):
    """AC 15: with VAF's own certificate nothing changes for existing invitations: the last
    certificate of server.pem is the local CA."""
    import vaf.core.a2a.invite as invite_mod
    import vaf.network.binding as binding
    from cert_factory import isolated_home
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [("enp3s0", "192.168.1.20")])
    monkeypatch.setattr(binding, "local_interfaces", lambda: [])
    monkeypatch.setattr(binding, "get_local_network_ip", lambda: "192.168.1.20")
    monkeypatch.setattr(binding, "is_allowed_ip", lambda _ip: True)
    isolated_home(tmp_path, monkeypatch, local_network_enabled=True,
                  local_network_tls_enabled=True)
    monkeypatch.setattr(invite_mod, "_effective_port", lambda: (8443, True))
    ssl_utils.ensure_ssl_certificates()

    assert ssl_utils.served_authority()[1] == ssl_utils.ca_fingerprint()
    assert invite_mod.lan_endpoint("room-x")["ca_fingerprint"] == ssl_utils.ca_fingerprint()


def test_a_pinned_intermediate_verifies_without_the_interpreter_default(anchored, monkeypatch,
                                                                       tmp_path):
    """AC 16: Python 3.10-3.12 do not set VERIFY_X509_PARTIAL_CHAIN, and without it a pinned
    intermediate fails with "unable to get issuer certificate". The context demands it like
    STRICT. MUTATION: drop the explicit flag in trust.py - red."""
    import ssl as _ssl

    from cert_factory import handshake, key_pem, own_chain, pem, server_context
    real = _ssl.create_default_context

    def _like_older_pythons(*a, **kw):
        context = real(*a, **kw)
        context.verify_flags &= ~_ssl.VERIFY_X509_PARTIAL_CHAIN
        return context
    monkeypatch.setattr(trust_mod.ssl, "create_default_context", _like_older_pythons)
    leaf, key, inter, _root = own_chain(names=("192.168.1.42",))
    (tmp_path / "inter.pem").write_bytes(pem(inter))
    (tmp_path / "chain.pem").write_bytes(pem(leaf, inter))
    (tmp_path / "leaf.key").write_bytes(key_pem(key))
    url = "wss://192.168.1.42:8443"
    pin_authority(url, expected_fingerprint=ssl_utils.fingerprint_of(pem(inter)),
                  ca_file=tmp_path / "inter.pem")

    context = client_context(url)

    assert context.verify_flags & _ssl.VERIFY_X509_PARTIAL_CHAIN
    assert context.verify_flags & _ssl.VERIFY_X509_STRICT
    handshake(server_context(tmp_path / "chain.pem", tmp_path / "leaf.key"), context,
              server_hostname="192.168.1.42")


# ── the pin names the authority, the name check names the host ─────────────────

def _public_chain(tmp_path):
    """A host certificate and an attacker's, both from one public-style intermediate."""
    from cert_factory import key_pem, make_ca, make_leaf, pem
    root, root_key = make_ca("Public Root")
    inter, inter_key = make_ca("Public Intermediate R10", issuer=root, issuer_key=root_key)
    files = {}
    for name, names in (("host", ("vafhost.example.org", "192.168.1.20")),
                        ("attacker", ("attacker.example.net",))):
        leaf, key = make_leaf(inter, inter_key, names=names)
        (tmp_path / f"{name}.pem").write_bytes(pem(leaf, inter))
        (tmp_path / f"{name}.key").write_bytes(key_pem(key))
        files[name] = (tmp_path / f"{name}.pem", tmp_path / f"{name}.key")
    (tmp_path / "inter.pem").write_bytes(pem(inter))
    return inter, files


@pytest.mark.parametrize("url, host", [("wss://192.168.1.20:8443", "192.168.1.20"),
                                       ("wss://vafhost.example.org:8443", "vafhost.example.org")])
def test_another_certificate_from_the_same_authority_is_refused(tmp_path, monkeypatch, url,
                                                                host):
    """With a certificate from a public authority the invitation pins that authority, which
    vouches for anybody: a leaf for attacker.example.net from the same intermediate passed
    the pin and the handshake (measured). The certificate must name the host dialled.
    MUTATION: check_hostname = False in client_context - red."""
    import ssl as _ssl

    from cert_factory import fingerprint, handshake, server_context
    monkeypatch.setattr(trust_mod, "trust_dir", lambda: tmp_path)
    inter, files = _public_chain(tmp_path)
    pin_authority(url, expected_fingerprint=fingerprint(inter), ca_file=tmp_path / "inter.pem")

    handshake(server_context(*files["host"]), client_context(url), server_hostname=host)
    with pytest.raises(_ssl.SSLCertVerificationError) as refused:
        handshake(server_context(*files["attacker"]), client_context(url), server_hostname=host)
    assert "mismatch" in str(refused.value)


def _served(tmp_path, monkeypatch, names, *, own=True):
    import vaf.network.binding as binding
    from cert_factory import isolated_home, key_pem, own_chain, pem
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [("enp3s0", "192.168.1.20")])
    monkeypatch.setattr(binding, "local_interfaces", lambda: [])
    isolated_home(tmp_path, monkeypatch, local_network_enabled=True,
                  local_network_tls_enabled=True)
    if own:
        leaf, key, inter, root = own_chain(names=names)
        ssl_utils.install_certificate(pem(leaf, inter, root), key_pem(key))
    else:
        ssl_utils.ensure_ssl_certificates()


def _resolving(names, to="192.168.1.20"):
    """A stand-in for the lookup: these names resolve to ``to``, every other one to nothing."""
    return lambda name: frozenset({to}) if name in names else frozenset()


@pytest.mark.parametrize("names, machine, resolves, expected", [
    (("vaf.office.example", "192.168.1.20"), [], [], "192.168.1.20"),
    # The only name is checked like any other: it may be a website's.
    (("vaf.office.example",), [], [], None),
    (("vaf.office.example",), [], ["vaf.office.example"], "vaf.office.example"),
    (("vaf.office.example",), ["vaf", "vaf.office.example"], [], "vaf.office.example"),
    # The apex beside a wildcard is usually the public website, not this machine.
    (("*.example.org", "example.org"), [], [], None),
    (("example.org", "www.example.org", "vaf.example.org"), [], [], None),
    (("example.org", "vaf.example.org"), ["vaf", "vaf.example.org"], [], "vaf.example.org"),
    (("example.org", "vaf.example.org"), [], ["vaf.example.org"], "vaf.example.org"),
    (("*.office.example",), [], [], None),
    # The machine's own name, covered by the wildcard rather than listed.
    (("*.example.org", "example.org"), ["vaf", "vaf.example.org"], [], "vaf.example.org"),
    # An address written as a DNS name: TLS checks an address against address names only.
    (("192.168.1.20.example", "192.168.1.21"), [], ["192.168.1.20.example"],
     "192.168.1.20.example"),
])
def test_the_invitation_names_a_host_the_certificate_covers(tmp_path, monkeypatch, names,
                                                            machine, resolves, expected):
    """The peer checks the certificate against the host the invitation names, so the
    invitation names one the served certificate covers and that is this machine: the LAN
    address, else the machine's name where the certificate covers it (a wildcard too), else
    one that resolves to the address, the only name of a certificate included. MUTATIONS:
    take the first DNS name again - red (the apex); match the machine's name only exactly,
    not through the wildcard - red."""
    import vaf.network.binding as binding
    _served(tmp_path, monkeypatch, names)
    monkeypatch.setattr(binding, "machine_dns_names", lambda: list(machine))
    monkeypatch.setattr(ssl_utils, "_addresses_of", _resolving(resolves))
    assert ssl_utils.served_room_host("192.168.1.20") == expected


def test_a_dns_name_that_is_an_address_is_not_a_room_host(tmp_path, monkeypatch):
    """A DNS SAN "192.168.1.20" is no name a peer can dial by: TLS checks the dialled address
    against the IP names. MUTATION: drop the IP-literal skip - red."""
    from cryptography import x509 as _x509
    from cert_factory import isolated_home, key_pem, make_ca, make_leaf, pem
    import vaf.network.binding as binding
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [("enp3s0", "192.168.1.20")])
    monkeypatch.setattr(binding, "local_interfaces", lambda: [])
    isolated_home(tmp_path, monkeypatch, local_network_enabled=True,
                  local_network_tls_enabled=True)
    root, root_key = make_ca()
    leaf, key = make_leaf(root, root_key, names=("vafhost.lan",))
    # Rebuild the leaf with the address as a DNS name, which make_leaf would turn into an IP.
    from datetime import datetime, timedelta, timezone
    from cryptography.hazmat.primitives import hashes as _hashes
    now = datetime.now(timezone.utc)
    leaf = (_x509.CertificateBuilder().subject_name(leaf.subject).issuer_name(root.subject)
            .public_key(key.public_key()).serial_number(7)
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=100))
            .add_extension(_x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(_x509.SubjectAlternativeName([_x509.DNSName("192.168.1.20")]),
                           critical=False)
            .add_extension(_x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .add_extension(_x509.AuthorityKeyIdentifier.from_issuer_public_key(
                root_key.public_key()), critical=False)
            .sign(root_key, _hashes.SHA256()))
    cert_file, key_file = tmp_path / "c.pem", tmp_path / "k.pem"
    cert_file.write_bytes(pem(leaf, root))
    key_file.write_bytes(key_pem(key))
    from vaf.core.config import Config
    with Config._locked():
        cfg = Config.load()
        cfg["local_network_ssl_cert"], cfg["local_network_ssl_key"] = str(cert_file), str(key_file)
        Config.save(cfg)
    host, why = ssl_utils.room_host_choice("192.168.1.20")
    assert host is None and "neither" in why
    assert "installing a certificate that names that address" in why, "the step to take"


def test_vafs_own_certificate_without_the_address_names_no_host(tmp_path, monkeypatch, caplog):
    """An address that came after the certificate was issued is not in it; the invitation
    then has no lane for other machines and the log says what to do."""
    import logging

    import vaf.core.a2a.invite as invite_mod
    import vaf.network.binding as binding
    _served(tmp_path, monkeypatch, (), own=False)
    assert ssl_utils.served_room_host("192.168.1.20") == "192.168.1.20"
    assert ssl_utils.served_room_host("192.168.9.9") is None
    monkeypatch.setattr(binding, "get_local_network_ip", lambda: "192.168.9.9")
    monkeypatch.setattr(binding, "is_allowed_ip", lambda _ip: True)
    with caplog.at_level(logging.WARNING):
        assert invite_mod.lan_endpoint("room-x") == {}
    assert any("vaf server cert reset" in r.getMessage() for r in caplog.records)
    endpoint, gap = invite_mod.lan_lane("room-x")
    assert endpoint == {} and "vaf server cert reset" in gap
    # The reason as it stands: the inviter's row and the invitee's note lead in themselves
    # (MUTATION: the lead-in back in lan_lane - red).
    assert gap.startswith("VAF's own certificate"), gap


def test_the_room_host_lookups_share_one_budget_and_run_once_per_name(tmp_path, monkeypatch):
    """getaddrinfo has no timeout of its own (a .local name nobody answers took 10 s), and an
    invitation is minted while a person waits. The lookups run together and the choice
    returns when the budget is spent, not when the slowest one gives up; no name is looked up
    twice. MUTATIONS: wait for every lookup - red (the time); look a name up twice - red."""
    import threading
    import time
    from collections import Counter

    import vaf.network.binding as binding
    _served(tmp_path, monkeypatch, ("example.org", "a.example.org", "b.example.org"))
    monkeypatch.setattr(ssl_utils, "_LOOKUP_POOL", None)
    monkeypatch.setattr(ssl_utils, "_LOOKUP_IN_FLIGHT", None)
    release = threading.Event()
    looked_up, lock = [], threading.Lock()

    def _slow(name):
        with lock:
            looked_up.append(name)
        release.wait(5)
        return frozenset()

    monkeypatch.setattr(ssl_utils, "_addresses_of", _slow)
    monkeypatch.setattr(binding, "machine_dns_names", lambda: (release.wait(5), [])[1])
    monkeypatch.setattr(ssl_utils, "_LOOKUP_BUDGET_S", 0.3)
    try:
        started = time.monotonic()
        host, why = ssl_utils.room_host_choice("192.168.1.20")
        took = time.monotonic() - started
    finally:
        release.set()
    assert host is None and "gave no answer in time (they were still running" in why
    # When to try again, and what to check if it still fails: an answer that is not the
    # host's address leaves no lane, so the hint promises none (MUTATION: the old "has the
    # lane" promise - red).
    assert "try again in about a minute" in why and "has the lane" not in why
    assert "check that one of these names resolves to the host's address" in why
    assert took < 2.0, took
    counts = Counter(looked_up)
    assert set(counts) <= {"example.org", "a.example.org", "b.example.org"}
    assert all(n == 1 for n in counts.values()), counts


def test_the_only_name_is_checked_like_any_other(tmp_path, monkeypatch):
    """A certificate made for a website names the website: an invitation that sent a peer
    there would hand its ticket to that server. So the only name counts once it is this
    machine's name or resolves to this machine. MUTATION: take the only name unchecked
    again - red."""
    import vaf.network.binding as binding
    _served(tmp_path, monkeypatch, ("example.org",))
    monkeypatch.setattr(binding, "machine_dns_names", lambda: ["vaf"])
    monkeypatch.setattr(ssl_utils, "_addresses_of", _resolving({"example.org"}, "203.0.113.7"))
    host, why = ssl_utils.room_host_choice("192.168.1.20")
    assert host is None
    assert "example.org" in why and "hosts file" in why, "the split-DNS fix is named"
    # One name is "it"; and a hosts line on the host alone would pass this check while the
    # devices still reach the website, so the reason says they must resolve it too
    # (MUTATION: the plural wording for one name - red).
    assert "but it is not the host's name" in why
    assert "other machines must resolve it to that address too" in why


def test_a_name_that_resolves_to_an_admitted_vpn_address_is_this_machine(tmp_path, monkeypatch):
    """A certificate made for the machine's VPN name resolves to the VPN address, not the LAN
    one, and a peer on that VPN reaches it there. MUTATION: compare with the LAN address
    only - red."""
    from types import SimpleNamespace

    import vaf.network.binding as binding
    _served(tmp_path, monkeypatch, ("m.tailnet.example",))
    monkeypatch.setattr(binding, "machine_dns_names", lambda: ["m"])
    monkeypatch.setattr(binding, "access_addresses",
                        lambda cfg=None: [SimpleNamespace(ip="100.64.0.7")])
    monkeypatch.setattr(ssl_utils, "_addresses_of",
                        _resolving({"m.tailnet.example"}, "100.64.0.7"))
    assert ssl_utils.room_host_choice("192.168.1.20") == ("m.tailnet.example", None)


def test_the_lookups_of_every_invitation_share_eight_threads(tmp_path, monkeypatch):
    """Lookups that hang outlive the choice that asked; one pool for all choices keeps them
    to eight threads however many invitations are made (measured before: 800 at twenty
    connections), and the ones queued behind them wait, one per name. MUTATION: a pool per choice
    - red."""
    import threading

    import vaf.network.binding as binding
    names = tuple(f"n{i}.example.org" for i in range(20))
    _served(tmp_path, monkeypatch, names)
    monkeypatch.setattr(ssl_utils, "_LOOKUP_POOL", None)
    monkeypatch.setattr(ssl_utils, "_LOOKUP_IN_FLIGHT", None)
    monkeypatch.setattr(ssl_utils, "_LOOKUP_BUDGET_S", 0.05)
    release = threading.Event()
    started, lock = [], threading.Lock()

    def _hang(name):
        with lock:
            started.append(name)
        release.wait(10)
        return frozenset()

    def _lookup_threads():
        return {t for t in threading.enumerate() if t.name.startswith("vaf-host-lookup")}

    monkeypatch.setattr(ssl_utils, "_addresses_of", _hang)
    monkeypatch.setattr(binding, "machine_dns_names", lambda: (release.wait(10), [])[1])
    before = _lookup_threads()
    try:
        for _ in range(10):
            assert ssl_utils.room_host_choice("192.168.1.20")[0] is None
        grown = _lookup_threads() - before
        ran = len(started)
    finally:
        release.set()
    assert len(grown) <= ssl_utils._LOOKUP_WORKERS, len(grown)
    assert ran <= ssl_utils._LOOKUP_WORKERS, "more lookups ran than the pool has threads"


def test_an_answer_is_reused_for_a_minute(tmp_path, monkeypatch):
    """Repeated invitations do not ask the resolver again. MUTATION: ignore the memo - red."""
    import vaf.network.binding as binding
    _served(tmp_path, monkeypatch, ("a.example.org", "b.example.org"))
    monkeypatch.setattr(binding, "machine_dns_names", lambda: ["vaf"])
    asked = []

    def _getaddrinfo(name, *_a, **_k):
        asked.append(name)
        return [(2, 1, 6, "", ("203.0.113.7", 0))]

    monkeypatch.setattr(ssl_utils.socket, "getaddrinfo", _getaddrinfo)
    for _ in range(3):
        assert ssl_utils.room_host_choice("192.168.1.20")[0] is None
    assert sorted(asked) == ["a.example.org", "b.example.org"]


def test_every_reason_says_what_fixes_it(tmp_path, monkeypatch):
    """A wildcard alone needs a host name under it, and the two rare reasons point at the
    command that says more. MUTATIONS: drop the wildcard reason - red; drop the pointer -
    red."""
    import vaf.network.binding as binding
    _served(tmp_path, monkeypatch, ("*.example.org",))
    monkeypatch.setattr(binding, "machine_dns_names", lambda: ["studio"])
    host, why = ssl_utils.room_host_choice("192.168.1.20")
    assert host is None and "only *.example.org" in why and "vaf.example.org" in why
    monkeypatch.setattr(ssl_utils, "served_chain", lambda cfg=None: [])
    host, why = ssl_utils.room_host_choice("192.168.1.20")
    assert host is None and "vaf server cert show" in why


def test_a_lookup_still_running_is_not_waited_for_again(tmp_path, monkeypatch):
    """While DNS hangs, every choice used to wait its full budget again and start the same
    lookups once more, so one account opening many connections made everybody's invitations
    queue. Only the choice that starts a lookup pays the time now. MUTATION: no shared
    running lookups - red."""
    import threading
    import time

    import vaf.network.binding as binding
    _served(tmp_path, monkeypatch, ("a.example.org", "b.example.org"))
    monkeypatch.setattr(ssl_utils, "_LOOKUP_POOL", None)
    monkeypatch.setattr(ssl_utils, "_LOOKUP_IN_FLIGHT", None)
    monkeypatch.setattr(ssl_utils, "_LOOKUP_BUDGET_S", 0.5)
    release = threading.Event()
    started = []

    def _hang(name):
        started.append(name)
        release.wait(10)
        return frozenset()

    monkeypatch.setattr(ssl_utils, "_addresses_of", _hang)
    monkeypatch.setattr(binding, "machine_dns_names", lambda: ["vaf"])
    try:
        first = time.monotonic()
        assert ssl_utils.room_host_choice("192.168.1.20")[0] is None
        first = time.monotonic() - first
        second = time.monotonic()
        host, why = ssl_utils.room_host_choice("192.168.1.20")
        second = time.monotonic() - second
        ran = sorted(started)
    finally:
        release.set()
    assert first >= 0.4 and second < 0.2, (first, second)
    assert host is None and "gave no answer" in why
    assert ran == ["a.example.org", "b.example.org"], "a running lookup was started again"


def _gaierror(code):
    import socket

    def _raise(*_a, **_k):
        raise socket.gaierror(code, "resolver")
    return _raise


def test_a_resolver_failure_is_held_briefly_then_asked_again(tmp_path, monkeypatch):
    """EAI_AGAIN (SERVFAIL, a timeout) is no answer. Remembered for a minute as "no address",
    it gave every invitation no lane and advice to fix a DNS entry that may be fine; not held
    at all, every choice started every name again and waited its full budget (another
    account's invitation waited 10 s at 20 connections). It is held for a few seconds, as no
    answer. MUTATIONS: hold nothing - red (asked again at once); hold it like an answer -
    red (not asked again after the hold)."""
    import socket
    import time

    import vaf.network.binding as binding
    _served(tmp_path, monkeypatch, ("a.example.org", "b.example.org"))
    monkeypatch.setattr(binding, "machine_dns_names", lambda: ["vaf"])
    monkeypatch.setattr(ssl_utils, "_LOOKUP_FAILURE_HOLD_S", 0.5)
    asked = []
    failing = _gaierror(socket.EAI_AGAIN)
    monkeypatch.setattr(ssl_utils.socket, "getaddrinfo",
                        lambda name, *a, **k: asked.append(name) or failing())
    for _ in range(3):
        host, why = ssl_utils.room_host_choice("192.168.1.20")
        assert host is None and "gave no answer" in why and "hosts file" not in why
    assert sorted(asked) == ["a.example.org", "b.example.org"], "asked again within the hold"
    time.sleep(0.8)
    ssl_utils.room_host_choice("192.168.1.20")
    assert sorted(asked) == ["a.example.org", "a.example.org", "b.example.org",
                             "b.example.org"], "not asked again after the hold"


def test_the_failure_hold_backs_off_and_an_answer_ends_the_series(monkeypatch):
    """Each failure of a name in a row doubles its hold, up to the minute an answer is kept;
    an answer starts the series over. In one comparison run with a 5 s hold that never grew,
    a resolver that fails after 1.5 s still made another account wait 7.5 s at 20
    connections; backing off brought that to 3.2 s. MUTATION: hold every failure 5 s - red."""
    monkeypatch.setattr(ssl_utils, "_LOOKUP_MEMO", {})
    monkeypatch.setattr(ssl_utils, "_LOOKUP_FAILURES", {})

    def _hold():
        return ssl_utils._LOOKUP_MEMO["dns:x"][2]

    holds = []
    for _ in range(6):
        ssl_utils._remember("dns:x", ssl_utils._NO_ANSWER)
        holds.append(_hold())
    assert holds == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]
    ssl_utils._remember("dns:x", frozenset({"192.168.1.20"}))
    assert _hold() == 60.0
    ssl_utils._remember("dns:x", ssl_utils._NO_ANSWER)
    assert _hold() == 5.0, "an answer starts the series over"


def test_a_name_that_does_not_exist_is_remembered(tmp_path, monkeypatch):
    """EAI_NONAME is an answer, and it is remembered like any other. MUTATION: remember no
    failure at all - red."""
    import socket

    import vaf.network.binding as binding
    _served(tmp_path, monkeypatch, ("a.example.org", "b.example.org"))
    monkeypatch.setattr(binding, "machine_dns_names", lambda: ["vaf"])
    asked = []
    missing = _gaierror(socket.EAI_NONAME)
    monkeypatch.setattr(ssl_utils.socket, "getaddrinfo",
                        lambda name, *a, **k: asked.append(name) or missing())
    for _ in range(2):
        host, why = ssl_utils.room_host_choice("192.168.1.20")
        assert host is None and "hosts file" in why
    assert sorted(asked) == ["a.example.org", "b.example.org"]


def test_a_name_without_an_answer_gets_a_sentence_of_its_own(tmp_path, monkeypatch):
    """No answer covers a slow lookup and a resolver that failed at once, so the text names
    both, in a sentence of its own after the fix. MUTATION: the old "; ... gave no answer
    within 2 s" tail - red."""
    import threading

    import vaf.network.binding as binding
    _served(tmp_path, monkeypatch, ("a.example.org", "b.example.org"))
    monkeypatch.setattr(ssl_utils, "_LOOKUP_POOL", None)
    monkeypatch.setattr(ssl_utils, "_LOOKUP_IN_FLIGHT", None)
    monkeypatch.setattr(ssl_utils, "_LOOKUP_BUDGET_S", 0.2)
    release = threading.Event()

    def _lookup(name):
        if name == "b.example.org":
            release.wait(5)
        return frozenset({"203.0.113.7"})

    monkeypatch.setattr(ssl_utils, "_addresses_of", _lookup)
    monkeypatch.setattr(binding, "machine_dns_names", lambda: ["vaf"])
    try:
        host, why = ssl_utils.room_host_choice("192.168.1.20")
    finally:
        release.set()
    assert host is None
    assert (". No answer came in time for b.example.org (the lookup was still running, or "
            "the resolver failed); try again in about a minute") in why


def test_the_retry_check_names_only_names_that_are_looked_up(tmp_path, monkeypatch):
    """A wildcard is never looked up, so the hint to check that a name resolves names the
    others only. MUTATION: build the hint from every listed name again - red."""
    import socket

    import vaf.network.binding as binding
    _served(tmp_path, monkeypatch, ("*.example.org", "a.example.org", "b.example.org"))
    monkeypatch.setattr(binding, "machine_dns_names", lambda: ["vaf"])
    failing = _gaierror(socket.EAI_AGAIN)
    monkeypatch.setattr(ssl_utils.socket, "getaddrinfo", lambda *a, **k: failing())
    host, why = ssl_utils.room_host_choice("192.168.1.20")
    hint = why.split("check that ", 1)[1]
    assert host is None
    assert hint.startswith("one of a.example.org, b.example.org resolves"), hint
    assert "*" not in hint
