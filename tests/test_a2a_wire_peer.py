# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The wire peer, driven against an implementation it shares no code with.

The client under test is `examples/12_a2a_wire_peer.py`, the file a stranger's
harness downloads and runs. Its WebSocket layer is hand-written from RFC 6455,
so the counterparty here is deliberately the `websockets` library - a server
that would refuse a malformed handshake, unmasked client frames or a missed
pong, which makes every passing test a small interoperability proof rather than
the client agreeing with itself.

TLS is real: a CA and a leaf are generated per run, the client pins the CA the
way a guest would (--ca-file plus the fingerprint), and a wrong fingerprint
must refuse before anything is spoken.
"""
import datetime
import importlib.util
import ipaddress
import json
import ssl
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
WIRE = ROOT / "examples" / "12_a2a_wire_peer.py"
ROOM = "room-ab12cd34"


def _load():
    spec = importlib.util.spec_from_file_location("a2a_wire_peer", WIRE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["a2a_wire_peer"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def peer():
    return _load()


@pytest.fixture(autouse=True)
def guest_home(tmp_path, monkeypatch):
    """The client keeps its seat under the home directory; every test gets its own."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


# ── a real authority, minted for this run ──────────────────────────────────

def _make_pki(directory: Path) -> dict:
    """A CA and a leaf for 127.0.0.1, strict-verification clean (SKI/AKI and
    key usages present - the client turns VERIFY_X509_STRICT on, exactly like
    the framework's own trust module)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "wire-peer-test-ca")])
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True,
            crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(ca_ski, critical=False)
        .sign(ca_key, hashes.SHA256()))

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(
            [x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
             x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=True,
            data_encipherment=False, key_agreement=False, key_cert_sign=False,
            crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                       critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
                       critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski),
                       critical=False)
        .sign(ca_key, hashes.SHA256()))

    ca_pem = directory / "ca.pem"
    leaf_pem = directory / "leaf.pem"
    leaf_keyfile = directory / "leaf.key"
    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    leaf_pem.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    leaf_keyfile.write_bytes(leaf_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    import hashlib
    return {"ca": ca_pem, "leaf": leaf_pem, "key": leaf_keyfile,
            "fingerprint": hashlib.sha256(
                ca_cert.public_bytes(serialization.Encoding.DER)).hexdigest()}


TICKET = "t-ticket1"
SEAT = "s-p-guest1-secret1"
BIG = "x" * 70000   # forces the 8-byte length path on the receiving side

BACKLOG = [
    {"v": 1, "id": "f-one", "room": ROOM, "seq": 1, "lamport": 1, "ts": 1.0,
     "from": "p-owner1", "role": "peer", "kind": "say",
     "body": {"text": "welcome to the room"}},
    {"v": 1, "id": "f-two", "room": ROOM, "seq": 2, "lamport": 2, "ts": 2.0,
     "from": "p-owner1", "role": "peer", "kind": "ask",
     "body": {"text": "a question for the guest"}},
    {"v": 1, "id": "f-two", "room": ROOM, "seq": 2, "lamport": 2, "ts": 2.0,
     "from": "p-owner1", "role": "peer", "kind": "ask",
     "body": {"text": "a question for the guest"}},          # duplicate, dropped on id
    {"v": 1, "id": "f-mine", "room": ROOM, "seq": 1, "lamport": 3, "ts": 3.0,
     "from": "p-guest1", "role": "peer", "kind": "say",
     "body": {"text": "the guest's own echo"}},
    {"v": 1, "id": "f-big", "room": ROOM, "seq": 3, "lamport": 4, "ts": 4.0,
     "from": "p-owner1", "role": "peer", "kind": "say", "body": {"text": BIG}},
]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """The counterparty: a `websockets` server that speaks the room's door.

    Before every ack it pings and WAITS for the pong, so a client that cannot
    answer pings fails loudly here instead of dying quietly on a live keepalive.
    """
    pki = _make_pki(tmp_path_factory.mktemp("pki"))
    from websockets.sync.server import serve

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pki["leaf"], pki["key"])
    state = {"submissions": [], "pongs": []}

    def handler(connection):
        parts = urlsplit(connection.request.path)
        token = (parse_qs(parts.query).get("token") or [""])[0]
        room = parts.path.rsplit("/", 1)[-1]
        welcome = {"kind": "welcome", "room": room, "peer": "p-guest1",
                   "role": "peer", "protocol": "vaf-a2a", "v": 1}
        if token == TICKET:
            welcome["seat"] = SEAT
            welcome["welcome"] = {"members": [{"peer": "p-owner1", "display": "VAF"}]}
        elif token != SEAT:
            connection.close(4001, "credential refused")
            return
        connection.send(json.dumps(welcome))
        for frame in BACKLOG[:-1]:
            connection.send(json.dumps(frame))
        last = json.dumps(BACKLOG[-1])
        connection.send([last[:10], last[10:]])              # fragmented on purpose
        connection.send(json.dumps({"kind": "sync", "room": room, "lamport": 4}))
        try:
            for raw in connection:
                payload = json.loads(raw)
                state["submissions"].append(payload)
                state["pongs"].append(connection.ping().wait(5))
                connection.send(json.dumps(
                    {"kind": "ack", "status": "committed",
                     "frame": {"id": f"f-new-{len(state['submissions'])}",
                               "kind": payload.get("kind")}}))
        except Exception:
            pass

    ws_server = serve(handler, "127.0.0.1", 0, ssl=context)
    thread = threading.Thread(target=ws_server.serve_forever, daemon=True,
                              name="wire-peer-test-server")
    thread.start()
    port = ws_server.socket.getsockname()[1]
    yield {"port": port, "pki": pki, "state": state,
           "url": f"wss://127.0.0.1:{port}/ws/a2a/{ROOM}"}
    ws_server.shutdown()


def _join(peer, server, capsys):
    peer.main(["join", "--url", server["url"], "--ticket", TICKET,
               "--ca-fp", server["pki"]["fingerprint"],
               "--ca-file", str(server["pki"]["ca"])])
    return json.loads(capsys.readouterr().out)


# ── join: trust, redemption, the seat ──────────────────────────────────────

def test_join_redeems_the_ticket_and_keeps_the_seat(peer, server, capsys):
    summary = _join(peer, server, capsys)
    assert summary["peer"] == "p-guest1"
    assert summary["room"] == ROOM
    assert summary["history"] == len(BACKLOG)
    record = peer.load_record(ROOM)
    assert record["seat"] == SEAT
    assert record["ca_pem"].startswith("-----BEGIN CERTIFICATE-----")
    if sys.platform != "win32":
        mode = peer.record_path(ROOM).stat().st_mode & 0o777
        assert mode == 0o600, "a bearer credential is owner-only or it is public"


def test_join_refuses_a_wrong_fingerprint_before_anything_is_spoken(peer, server, capsys):
    with pytest.raises(SystemExit) as bang:
        peer.main(["join", "--url", server["url"], "--ticket", TICKET,
                   "--ca-fp", "ab" * 32, "--ca-file", str(server["pki"]["ca"])])
    assert bang.value.code == 2
    assert "does not match" in capsys.readouterr().err
    assert not peer.record_path(ROOM).exists(), (
        "a refused authority must leave nothing behind")


# ── read: order, dedupe, the cursor, the echo ──────────────────────────────

def test_read_prints_the_conversation_once_and_remembers(peer, server, capsys):
    _join(peer, server, capsys)
    record = peer.load_record(ROOM)
    record["cursor"] = [0, "", 0]                 # rewind: join starts after history
    peer.save_record(record)

    peer.main(["read", ROOM])
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["id"] for row in rows] == ["f-one", "f-two", "f-big"], (
        "expected the duplicate dropped, the own echo skipped, lamport order kept")
    assert rows[-1]["body"]["text"] == BIG, "the fragmented frame arrived torn"

    peer.main(["read", ROOM])
    assert capsys.readouterr().out == "", "a second read must not repeat the news"


def test_read_all_shows_the_whole_transcript_echo_included(peer, server, capsys):
    _join(peer, server, capsys)
    peer.main(["read", ROOM, "--all"])
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["id"] for row in rows] == ["f-one", "f-two", "f-mine", "f-big"]


# ── speaking: the ack, the pong, the big frame out ─────────────────────────

def test_say_reaches_the_room_and_reports_the_ack(peer, server, capsys):
    _join(peer, server, capsys)
    peer.main(["say", ROOM, "hello over the wire"])
    ack = json.loads(capsys.readouterr().out)
    assert ack["status"] == "committed"
    sent = server["state"]["submissions"][-1]
    assert sent == {"kind": "say", "body": {"text": "hello over the wire"}}
    assert server["state"]["pongs"][-1] is True, (
        "the server pinged before acking; an unanswered ping kills live rooms")


def test_answer_carries_the_reply_link(peer, server, capsys):
    _join(peer, server, capsys)
    peer.main(["answer", ROOM, "the guest's answer", "--reply-to", "f-two"])
    assert json.loads(capsys.readouterr().out)["status"] == "committed"
    sent = server["state"]["submissions"][-1]
    assert sent["kind"] == "answer"
    assert sent["reply_to"] == "f-two"


def test_a_big_outbound_frame_survives(peer, server, capsys):
    _join(peer, server, capsys)
    peer.main(["say", ROOM, "y" * 70000])
    assert json.loads(capsys.readouterr().out)["status"] == "committed"
    assert server["state"]["submissions"][-1]["body"]["text"] == "y" * 70000


def test_a_dead_seat_is_refused_with_the_reason(peer, server, capsys):
    _join(peer, server, capsys)
    record = peer.load_record(ROOM)
    record["seat"] = "s-p-guest1-wrong"
    peer.save_record(record)
    with pytest.raises(SystemExit) as bang:
        peer.main(["read", ROOM])
    assert bang.value.code == 2
    assert "credential was refused" in capsys.readouterr().err


# ── the rules that need no socket ──────────────────────────────────────────

def test_plain_ws_is_refused_before_any_socket_exists(peer):
    with pytest.raises(peer.Refused):
        peer.split_room_url("ws://h:8443/ws/a2a/room-ab12cd34")
    with pytest.raises(peer.Refused):
        peer.split_room_url("wss://h:8443/something/else")


def test_fingerprints_match_the_way_humans_paste_them(peer):
    assert peer.fingerprints_match("AB:CD:12", "abcd12")
    assert not peer.fingerprints_match("abcd12", "abcd13")
    assert not peer.fingerprints_match("", "")


def test_the_client_runs_standalone(peer):
    """The file is downloaded and run by strangers; `--help` failing would mean
    the argparse wiring only works when imported by this suite."""
    result = subprocess.run([sys.executable, str(WIRE), "--help"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0
    assert "join" in result.stdout
