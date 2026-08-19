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
import hashlib
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
    state = {"submissions": [], "pongs": [], "connections": 0}

    def handler(connection):
        state["connections"] += 1
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
    record = json.loads(
        (Path.home() / ".vaf-a2a-guest" / f"{ROOM}.json").read_text(encoding="utf-8"))
    assert record["welcome"] == {"members": [{"peer": "p-owner1", "display": "VAF"}]}, \
        "the welcome kept at join time is what howto reprints later"
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


# ── the held line: keepalive and no swallowed frames ───────────────────────

class _ScriptedWire:
    """A WireSocket stand-in: answers from a script, records what was sent."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []
        self.close_code = 0

    def send_text(self, text):
        self.sent.append(json.loads(text))

    def recv_text(self, timeout=None):
        if not self.script:
            raise TimeoutError()
        return self.script.pop(0)

    def close(self):
        pass


def _connection(peer, script):
    return peer.RoomConnection(_ScriptedWire(script),
                               {"kind": "welcome", "room": ROOM, "peer": "p-x",
                                "role": "peer"})


def test_submit_keeps_the_frames_that_arrive_while_it_waits(peer):
    """MUTATION: drop non-ack messages in submit again.

    A room keeps talking while this side confirms its own send. submit used to
    consume and DISCARD every fanned-out frame that arrived before the ack -
    a message somebody sent in that window was silently never seen.
    """
    say = json.dumps({"kind": "say", "id": "m-1", "body": {"text": "while you typed"}})
    ack = json.dumps({"kind": "ack", "status": "committed"})
    conn = _connection(peer, [say, ack])

    answer = conn.submit({"kind": "say", "body": {"text": "mine"}})
    assert answer["status"] == "committed"
    kept = conn.next_frame(timeout=0)
    assert kept and kept["id"] == "m-1", "the concurrent frame must be kept, not eaten"


def test_renew_speaks_the_transport_verb_and_returns_the_answer(peer):
    """MUTATION: point renew at a frame kind instead of the transport verb.

    The verb is what keeps a held line's writer lease alive (protocol contract
    C9); a frame kind would land in the store and be refused by role rules.
    """
    conn = _connection(peer, [json.dumps({"kind": "ack", "status": "renewed"})])
    answer = conn.renew()
    assert answer["status"] == "renewed"
    assert conn.wire.sent == [{"kind": "renew"}]


def test_wait_renews_between_slices_and_respects_an_old_host(peer):
    """The wait loop holds the line in slices and renews between them; a host
    that refuses the verb once is not asked again (source pin - the loop is
    wired through argparse and files, so its shape is pinned where it lives)."""
    source = WIRE.read_text(encoding="utf-8")
    wait = source.split("def cmd_wait")[1][:2400]
    assert "connection.renew()" in wait
    assert '!= "renewed"' in wait and "renew_spoken = False" in wait


# ── the MCP door: the same verbs, served over stdio ────────────────────────

def _mcp(peer, method, request_id=1, **params):
    request = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        request["id"] = request_id
    if params:
        request["params"] = params
    return peer.handle_mcp_request(request)


def _call(peer, tool, **arguments):
    reply = _mcp(peer, "tools/call", name=tool, arguments=arguments)
    result = reply["result"]
    return result["content"][0]["text"], bool(result.get("isError"))


def test_mcp_initialize_answers_the_version_and_tools(peer):
    """MUTATION: answer another protocol revision, or drop capabilities.tools.

    The revision and the capability flag are what a host checks before it asks
    anything else; VAF's own client pins exactly this pair.
    """
    reply = _mcp(peer, "initialize", protocolVersion="2024-11-05")
    assert reply["id"] == 1
    assert reply["result"]["protocolVersion"] == "2024-11-05"
    assert "tools" in reply["result"]["capabilities"]
    assert reply["result"]["serverInfo"]["name"]


def test_mcp_notifications_get_no_answer(peer):
    """MUTATION: reply to a notification - strict hosts drop the connection."""
    assert peer.handle_mcp_request(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_mcp_tools_list_names_every_room_verb(peer):
    """MUTATION: drop a tool from the registry, or a required key from its
    properties - a host renders exactly what this answer carries."""
    reply = _mcp(peer, "tools/list")
    tools = reply["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "a2a_join", "a2a_rooms", "a2a_read", "a2a_wait", "a2a_say",
        "a2a_answer", "a2a_report", "a2a_leave", "a2a_howto",
        "a2a_files", "a2a_fetch", "a2a_push"}
    for tool in tools:
        assert tool["description"].strip()
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        for required in schema.get("required", []):
            assert required in schema["properties"], (tool["name"], required)


def test_mcp_unknown_method_is_an_error_not_a_crash(peer):
    reply = _mcp(peer, "resources/list")
    assert reply["error"]["code"] == -32601
    assert peer.handle_mcp_request(
        {"jsonrpc": "2.0", "method": "resources/list"}) is None


def test_mcp_call_say_commits_a_frame_on_the_real_wire(peer, server, capsys):
    """MUTATION: fabricate an ack without submitting - the server's submissions
    list is the truth this asserts against."""
    _join(peer, server, capsys)
    before = len(server["state"]["submissions"])
    text, failed = _call(peer, "a2a_say", room=ROOM, text="ueber die MCP-Tuer")
    assert not failed, text
    assert json.loads(text.splitlines()[-1])["status"] == "committed"
    sent = server["state"]["submissions"][before:]
    assert {"kind": "say", "body": {"text": "ueber die MCP-Tuer"}} in sent


def test_mcp_refusals_are_results_not_protocol_errors(peer):
    """MUTATION: route refusals into JSON-RPC errors - the host would declare
    the server broken instead of showing the model the refusal.

    Three nets shape a refusal into an isError result (_drive, the tools/call
    Refused catch, the generic Exception catch); removing any one degrades
    gracefully into the next, so only the deliberate protocol-error rewrite
    this docstring names goes red here. That layering is the point.
    """
    reply = _mcp(peer, "tools/call", name="a2a_read",
                 arguments={"room": "room-nowhere1"})
    assert "error" not in reply
    result = reply["result"]
    assert result["isError"] is True
    assert "no seat" in result["content"][0]["text"]


def test_mcp_call_with_a_missing_argument_names_the_gap(peer, server):
    before = len(server["state"]["submissions"])
    text, failed = _call(peer, "a2a_say", room=ROOM)
    assert failed and "'text'" in text
    assert len(server["state"]["submissions"]) == before, \
        "nothing may reach the room for a call the schema already refuses"


def test_mcp_unknown_tool_is_refused_by_name(peer):
    reply = _mcp(peer, "tools/call", name="a2a_nuke", arguments={})
    assert reply["error"]["code"] == -32602
    assert "a2a_nuke" in reply["error"]["message"]
    assert "a2a_say" in reply["error"]["message"]


def test_mcp_wait_times_out_as_an_error_result(peer, server, capsys):
    """MUTATION: map the wait timeout to a protocol error or a success."""
    _join(peer, server, capsys)
    text, failed = _call(peer, "a2a_wait", room=ROOM, timeout=1)
    assert failed
    assert "nothing was said in time" in text


def test_mcp_stdio_pump_answers_parse_errors_and_keeps_serving(peer):
    """MUTATION: let json.loads raise through the pump - one garbage line from
    a host would kill the whole bridge."""
    lines = "\n".join([
        "this is not json",
        json.dumps({"jsonrpc": "2.0", "id": 7, "method": "initialize"}),
    ]) + "\n"
    proc = subprocess.run([sys.executable, str(WIRE), "mcp"], input=lines,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    assert replies[0]["error"]["code"] == -32700
    assert replies[0]["id"] is None
    assert replies[1]["id"] == 7 and "result" in replies[1], \
        "the pump must survive garbage and keep serving"


def test_vafs_own_mcp_client_drives_the_guest_bridge(peer, server, capsys):
    """MUTATION: any drift from the subset VAF's client speaks - a wrong id
    echo, a reply to the initialized notification, multi-line JSON, a missing
    result.content - goes red here. VAF consuming its own bridge is the proof
    that any MCP host can.
    """
    if " " in sys.executable or " " in str(WIRE):
        pytest.skip("the MCP client splits its server command on spaces")
    from vaf.tools.mcp_client import MCPClientTool

    _join(peer, server, capsys)
    command = f"{sys.executable} {WIRE} mcp"
    tool = MCPClientTool()
    try:
        names = {t["name"] for t in tool.list_server_tools(command)}
        assert "a2a_say" in names and len(names) == 12, names
        before = len(server["state"]["submissions"])
        answer = tool.run(server_command=command, tool_name="a2a_say",
                          arguments={"room": ROOM, "text": "vaf drives its bridge"})
        assert "committed" in answer, answer
        sent = server["state"]["submissions"][before:]
        assert {"kind": "say", "body": {"text": "vaf drives its bridge"}} in sent
    finally:
        for process in tool._server_processes.values():
            process.terminate()


def test_rooms_lists_the_seats_without_the_secrets(peer, server, capsys):
    """MUTATION: print the seat credential - a bearer secret straight into a
    terminal scrollback or a model's context."""
    _join(peer, server, capsys)
    peer.main(["rooms"])
    out = capsys.readouterr().out
    row = json.loads(out.strip().splitlines()[-1])
    assert row["room"] == ROOM and row["peer"] == "p-guest1"
    assert SEAT not in out and "ca_pem" not in out
    text, failed = _call(peer, "a2a_rooms")
    assert not failed and SEAT not in text


def test_howto_reprints_the_room_and_how_to_behave(peer, server, capsys, tmp_path, monkeypatch):
    """MUTATION: raise on a missing seat, or drop the stored welcome packet."""
    _join(peer, server, capsys)
    peer.main(["howto", ROOM])
    out = capsys.readouterr().out
    assert "as_of: join" in out
    assert "p-owner1" in out, "the welcome kept at join time must reprint"
    assert "REQUEST TO ACT" in out
    assert "A2A_PROTOCOL.md" in out

    monkeypatch.setenv("HOME", str(tmp_path / "fresh"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fresh"))
    peer.main(["howto", ROOM])
    fresh = capsys.readouterr().out
    assert "instructions still hold" in fresh and "REQUEST TO ACT" in fresh


def test_every_mcp_tool_is_named_in_the_protocol_doc(peer):
    """The registry/doc copy pair, guarded (rule 2 pattern): a tool a host can
    call that the protocol document does not name is an undocumented surface."""
    doc = (ROOT / "docs" / "agents" / "A2A_PROTOCOL.md").read_text(encoding="utf-8")
    for tool in peer.MCP_TOOLS:
        assert f"`{tool['name']}`" in doc, tool["name"]


# ── the shared folder verbs, driven through the _http seam ─────────────────

def _seam(peer, monkeypatch, answers):
    calls = []

    def fake(record, method, path_and_query, body=b""):
        calls.append({"method": method, "path": path_and_query, "body": body})
        return answers.pop(0)

    monkeypatch.setattr(peer, "_http", fake)
    return calls


def test_fetch_inlines_small_text_and_never_binary(peer, server, capsys, monkeypatch, tmp_path):
    """MUTATION: inline binary bytes - undecodable noise straight into a model's
    context, where the local path was the whole point."""
    _join(peer, server, capsys)
    _seam(peer, monkeypatch, [(200, "kleiner text".encode("utf-8")),
                              (200, b"\x00\xff\x01binary")])
    peer.main(["fetch", ROOM, "brief.txt", "--out", str(tmp_path)])
    small = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert small["text"] == "kleiner text"
    assert (tmp_path / "brief.txt").read_text(encoding="utf-8") == "kleiner text"

    peer.main(["fetch", ROOM, "blob.bin", "--out", str(tmp_path)])
    blob = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "text" not in blob
    assert (tmp_path / "blob.bin").read_bytes() == b"\x00\xff\x01binary"


def test_push_carries_the_bytes_and_files_lists_the_answer(peer, server, capsys, monkeypatch, tmp_path):
    """MUTATION: drop the body from the push request, or the seat from the query."""
    _join(peer, server, capsys)
    capsys.readouterr()
    source = tmp_path / "wording.html"
    source.write_bytes(b"<h1>Entwurf</h1>")
    calls = _seam(peer, monkeypatch, [
        (200, json.dumps({"room": ROOM, "path": "wording.html", "size": 16}).encode()),
        (200, json.dumps({"room": ROOM, "files": [
            {"path": "wording.html", "size": 16, "mtime": 0}]}).encode()),
    ])
    peer.main(["push", ROOM, str(source)])
    assert calls[0]["method"] == "POST"
    assert calls[0]["body"] == b"<h1>Entwurf</h1>"
    assert "seat=" in calls[0]["path"] and "path=wording.html" in calls[0]["path"]

    peer.main(["files", ROOM])
    rows = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines()
            if l.startswith("{")]
    assert {"path": "wording.html", "size": 16, "mtime": 0} in rows


# ── the held line: one connection serving many calls ───────────────────────

@pytest.fixture()
def held(peer):
    """MCP mode without the pump: held mode on, lines torn down afterwards."""
    peer._HELD_MODE = True
    try:
        yield peer
    finally:
        peer._HELD_MODE = False
        peer._stop_lines()


def test_a_held_line_serves_many_calls_on_one_connection(peer, server, capsys, held):
    """MUTATION: drop the `_line_for` branch from any verb, so it opens its own
    connection again.

    Per call, this is what the guest lane cost: a handshake, a backlog replay
    and a race against its own writer lease - the shape that had a remote agent
    locked out of its own room for a whole evening. A held line pays that once.
    The connection counter on the test server is the proof, and it is why this
    test asserts a NUMBER rather than that things merely worked.
    """
    _join(peer, server, capsys)
    after_join = server["state"]["connections"]

    said, failed = _call(peer, "a2a_say", room=ROOM, text="auf der gehaltenen Leitung")
    assert not failed, said
    assert json.loads(said.splitlines()[-1])["status"] == "committed"
    read_text, read_failed = _call(peer, "a2a_read", room=ROOM)
    assert not read_failed, read_text
    again, again_failed = _call(peer, "a2a_say", room=ROOM, text="und noch einmal")
    assert not again_failed, again

    assert server["state"]["connections"] == after_join + 1, (
        "three calls opened more than the one line that was already held")
    sent = [s for s in server["state"]["submissions"] if s.get("kind") == "say"]
    assert {"kind": "say", "body": {"text": "und noch einmal"}} in sent


def test_a_held_wait_answers_from_the_mirror_without_dialling(peer, server, capsys, held):
    """MUTATION: let the held wait open its own connection.

    A wait is where the old shape hurt most: it had to hold a line of its own,
    which is exactly the line a send then could not have. Here the reader is
    already listening, so the wait reads a mirror and the send never queues
    behind it.
    """
    _join(peer, server, capsys)
    line = peer._line_for(ROOM, peer.load_record(ROOM))
    assert line is not None and line.alive()
    before = server["state"]["connections"]

    # The backlog the room replayed on connect is already in the mirror, so a
    # fresh record finds it without a single new connection.
    record = dict(peer.load_record(ROOM), cursor=[0, "", 0])
    rows = line.wait_new(record, timeout=5.0)
    assert rows, "the mirror held nothing the record had not seen"
    assert server["state"]["connections"] == before


def test_a_held_wait_gives_up_honestly_when_nothing_is_said(peer, server, capsys, held):
    _join(peer, server, capsys)
    text, failed = _call(peer, "a2a_wait", room=ROOM, timeout=1)
    assert failed and "nothing was said in time" in text


def test_the_shell_never_holds_a_line(peer, server, capsys):
    """MUTATION: hold lines outside MCP mode.

    One process per command has nothing to hold a line with, and a line built
    for one command would be dropped seconds later with its lease still warm -
    the exact shape that locks the next command out.
    """
    _join(peer, server, capsys)
    assert peer._line_for(ROOM, peer.load_record(ROOM)) is None
    assert peer._LINES == {}


def test_a_dead_line_is_rebuilt_rather_than_reused(peer, server, capsys, held):
    """MUTATION: keep handing out a line whose thread has ended - every later
    call would answer from a mirror nothing writes to any more."""
    _join(peer, server, capsys)
    first = peer._line_for(ROOM, peer.load_record(ROOM))
    assert first is not None
    first._fail("the host went away")
    assert not first.alive()

    second = peer._line_for(ROOM, peer.load_record(ROOM))
    assert second is not None and second is not first
    assert second.alive()


def test_update_verifies_before_it_replaces_and_never_bricks_the_client(
        peer, server, capsys, monkeypatch, tmp_path):
    """MUTATION: write the download before compiling it.

    A truncated or refused download that lands on disk anyway leaves a guest
    with no client at all - and the one command that could fetch a new one is
    the one it just broke.
    """
    _join(peer, server, capsys)
    target = tmp_path / "a2a_client.py"
    target.write_text("# the old client\n", encoding="utf-8")

    _seam(peer, monkeypatch, [(200, b"def broken(:\n")])
    with pytest.raises(SystemExit) as stop:
        peer.main(["update", ROOM, "--out", str(target)])
    assert stop.value.code != 0
    assert "not a usable client" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "# the old client\n"

    fresh = b"# a newer client\nprint('hi')\n"
    _seam(peer, monkeypatch, [(200, fresh)])
    peer.main(["update", ROOM, "--out", str(target)])
    row = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert target.read_bytes() == fresh
    assert row["sha256"] == hashlib.sha256(fresh).hexdigest()
    assert "restart" in row["note"]
    assert not target.with_suffix(".part").exists()
