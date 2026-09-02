# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The VAF-less lane: what a harness with no VAF gets from the invitation and the host.

The room's server is the router, so an invitation must be enough on its own. That
splits into three promises, each pinned here: the briefing renders the guest section
exactly when a wire endpoint exists; the checksum it prints is computed from the file
it names; and the two downloads answer without an account, because a guest holds a
join ticket and nothing else.
"""
import hashlib
import re
from pathlib import Path

import pytest

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.invite import guest_client_path, guest_client_sha256, invitation
from vaf.core.a2a.room import Room

ROOT = Path(__file__).resolve().parents[1]
WIRE_PEER = ROOT / "examples" / "12_a2a_wire_peer.py"


@pytest.fixture()
def circle(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    room = Room.create(kind="round", owner_scope="scope-a", base=tmp_path,
                       room_id="room-guest", topic="Guest lane")
    owner = room.join(display="VAF", scope_id="scope-a", peer_id="p-owner")
    return room, owner


# ── the briefing side ──────────────────────────────────────────────────────

def test_the_guest_section_rides_the_wire_endpoint(circle, monkeypatch):
    """MUTATION: drop the guest block, or render it without the checksum.

    A briefing that only speaks vaf commands hands a VAF-less harness an
    invitation it cannot act on; a download instruction without the checksum
    hands it code over a channel it cannot verify. The two belong together or
    not at all.
    """
    monkeypatch.setattr("vaf.core.a2a.invite.lan_endpoint",
                        lambda room_id: {"origin": "wss://h:8443",
                                         "url": f"wss://h:8443/ws/a2a/{room_id}",
                                         "ca_fingerprint": "cd" * 32})
    room, owner = circle
    row = invitation(room, owner)

    assert row["client_url"] == "https://h:8443/api/a2a/client.py"
    assert row["client_sha256"] == guest_client_sha256()
    assert row["client_url"] in row["briefing"]
    assert row["client_sha256"] in row["briefing"]
    assert (f"a2a_client.py join --url wss://h:8443/ws/a2a/room-guest"
            in row["briefing"].replace("\\\n       ", ""))
    assert row["ticket"] in row["briefing"]


def test_without_an_endpoint_there_is_no_guest_section(circle, monkeypatch):
    """Without wss there is no lane a VAF-less guest could use - the local lane
    IS the vaf command - so naming the client would promise a door that is not
    there."""
    monkeypatch.setattr("vaf.core.a2a.invite.lan_endpoint", lambda room_id: {})
    room, owner = circle
    row = invitation(room, owner)

    assert "client_url" not in row
    assert "a2a_client.py" not in row["briefing"]


def test_the_checksum_is_computed_from_the_file_it_names():
    """MUTATION: hardcode the hash. The next edit to the wire peer would make
    every invitation in the world name a checksum the download fails."""
    assert guest_client_path() == WIRE_PEER
    assert guest_client_sha256() == hashlib.sha256(WIRE_PEER.read_bytes()).hexdigest()


def test_a_wheel_install_points_at_the_repository(monkeypatch):
    """No file, no checksum, no dead download line - the repository copy instead."""
    monkeypatch.setattr("vaf.core.a2a.invite.guest_client_path", lambda: None)
    from vaf.core.a2a.invite import GUEST_CLIENT_REPO_URL, briefing
    text = briefing(room_id="room-guest", ticket="t-ab12", role="peer",
                    display="guest", endpoint={"origin": "wss://h:8443",
                                               "url": "wss://h:8443/ws/a2a/room-guest",
                                               "ca_fingerprint": "cd" * 32})
    assert GUEST_CLIENT_REPO_URL in text
    assert "/api/a2a/client.py" not in text


# ── the download side ──────────────────────────────────────────────────────

def test_the_download_endpoints_need_no_account():
    """MUTATION: drop the exemptions. A guest holds a ticket, not an account;
    behind auth both downloads answer 401 and the whole lane is a promise the
    server refuses to keep."""
    from vaf.auth.middleware import AUTH_EXEMPT_PATHS
    assert "/api/a2a/client.py" in AUTH_EXEMPT_PATHS
    assert "/api/a2a/ca.pem" in AUTH_EXEMPT_PATHS


@pytest.fixture()
def client():
    # No context manager, deliberately: entering one fires the app's startup
    # handlers, which spawn the server's real background machinery into the
    # test process and pollute the rest of the suite. The routes under test
    # need no startup state; every other TestClient in this suite does the same.
    from fastapi.testclient import TestClient

    from vaf.core.web_server import app
    return TestClient(app)


def test_the_host_serves_the_client_file_byte_for_byte(client):
    response = client.get("/api/a2a/client.py")
    assert response.status_code == 200
    assert response.content == WIRE_PEER.read_bytes(), (
        "the download differs from the file whose checksum the invitation carries")


def test_a_host_without_the_file_names_the_repository(client, monkeypatch):
    monkeypatch.setattr("vaf.core.a2a.invite.guest_client_path", lambda: None)
    response = client.get("/api/a2a/client.py")
    assert response.status_code == 404
    assert "raw.githubusercontent.com" in response.json()["detail"]


def test_the_ca_download_is_honest_about_a_missing_authority(client, monkeypatch, tmp_path):
    monkeypatch.setattr("vaf.network.ssl_utils.ca_certificate_path", lambda: None)
    assert client.get("/api/a2a/ca.pem").status_code == 404

    pem = tmp_path / "ca.pem"
    pem.write_bytes(b"-----BEGIN CERTIFICATE-----\nab12\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr("vaf.network.ssl_utils.ca_certificate_path", lambda: pem)
    response = client.get("/api/a2a/ca.pem")
    assert response.status_code == 200
    assert response.content == pem.read_bytes()


def test_the_briefing_offers_the_mcp_door_beside_the_shell(circle, monkeypatch):
    """MUTATION: drop the MCP lines from `_guest_block`, or move them outside
    the endpoint guard.

    An MCP-speaking harness should learn from the SAME briefing that the file
    it just fetched is also an MCP server - a second document to find would be
    a door most guests never open.
    """
    monkeypatch.setattr("vaf.core.a2a.invite.lan_endpoint",
                        lambda room_id: {"origin": "wss://h:8443",
                                         "url": f"wss://h:8443/ws/a2a/{room_id}",
                                         "ca_fingerprint": "cd" * 32})
    room, owner = circle
    row = invitation(room, owner)

    assert '"command": "python3"' in row["briefing"]
    assert '"a2a_client.py", "mcp"' in row["briefing"]
    assert "a2a_join" in row["briefing"]


def test_without_an_endpoint_the_mcp_offer_stays_away_too(circle, monkeypatch):
    monkeypatch.setattr("vaf.core.a2a.invite.lan_endpoint", lambda room_id: {})
    room, owner = circle
    row = invitation(room, owner)

    assert '"mcp"' not in row["briefing"]


def test_the_protocol_pointer_is_the_same_in_both_files():
    """MUTATION: let the guest file's PROTOCOL_DOC constant drift from the
    canonical URL the invitation prints."""
    from vaf.core.a2a.invite import PROTOCOL_DOC_URL

    assert PROTOCOL_DOC_URL in WIRE_PEER.read_text(encoding="utf-8")


# ── the shared folder over the wire: seat-authenticated list/fetch/push ────

@pytest.fixture()
def seated(tmp_path, monkeypatch):
    """A hosted room, a member with a SEAT, and its workspace on disk."""
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    room = Room.create(kind="round", owner_scope="scope-a", base=tmp_path,
                       room_id="room-seatfs", topic="Files over the wire")
    member = room.join(display="Opus", scope_id=None, peer_id="p-far1")
    seat = room.issue_seat(member)
    workspace = room.workspace_dir(create=True)
    (workspace / "notes.txt").write_text("hallo raum", encoding="utf-8")
    return {"room": room, "seat": seat, "workspace": workspace}


def test_a_wrong_seat_is_refused_without_an_existence_oracle(client, seated):
    """MUTATION: drop the seat check, or answer differently for a room that
    does not exist - either leaks what the seat must prove."""
    refused = client.get("/api/a2a/rooms/room-seatfs/files",
                         params={"seat": "s-p-far1-wrong"})
    missing = client.get("/api/a2a/rooms/room-nowhere1/files",
                         params={"seat": "s-p-far1-wrong"})
    assert refused.status_code == 403
    assert missing.status_code == 403
    assert refused.json()["detail"] == missing.json()["detail"], \
        "a wrong seat must learn nothing about which rooms exist"
    naked = client.get("/api/a2a/rooms/room-seatfs/files")
    assert naked.status_code == 403


def test_path_traversal_never_leaves_the_workspace(client, seated, tmp_path):
    """MUTATION: drop the resolve containment - a push writes outside the room."""
    outside = tmp_path / "escape.txt"
    for bad in ("../escape.txt", "/etc/escape.txt", "a/../../escape.txt"):
        answer = client.post(f"/api/a2a/rooms/room-seatfs/file",
                             params={"seat": seated["seat"], "path": bad},
                             content=b"boom")
        assert answer.status_code == 400, bad
    assert not outside.exists()
    fetched = client.get(f"/api/a2a/rooms/room-seatfs/file",
                         params={"seat": seated["seat"], "path": "../room.json"})
    assert fetched.status_code == 400

    # The resolve containment is what catches a path with no `..` in it that
    # still leaves: a symlink inside the workspace pointing outside.
    escape_dir = tmp_path / "outside-dir"
    escape_dir.mkdir()
    (seated["workspace"] / "link").symlink_to(escape_dir)
    through = client.post("/api/a2a/rooms/room-seatfs/file",
                          params={"seat": seated["seat"], "path": "link/escape.txt"},
                          content=b"boom")
    assert through.status_code == 400
    assert not (escape_dir / "escape.txt").exists(), \
        "a symlink must not carry a write out of the workspace"


def test_an_upload_over_the_cap_is_refused_with_the_limit_named(client, seated, monkeypatch):
    """MUTATION: drop the cap - a guest could fill the host's disk."""
    import vaf.core.web_server as ws

    monkeypatch.setattr(ws, "_A2A_WORKSPACE_UPLOAD_CAP", 8)
    answer = client.post("/api/a2a/rooms/room-seatfs/file",
                         params={"seat": seated["seat"], "path": "big.bin"},
                         content=b"123456789")
    assert answer.status_code == 413
    assert "8" in answer.json()["detail"]


def test_push_list_fetch_round_trip_byte_identical(client, seated):
    """MUTATION: mangle the bytes anywhere on the path."""
    payload = "ein Umlaut-Test: äöü".encode("utf-8") + b"\x00\xff"
    pushed = client.post("/api/a2a/rooms/room-seatfs/file",
                         params={"seat": seated["seat"], "path": "sub/roundtrip.bin"},
                         content=payload)
    assert pushed.status_code == 200
    listed = client.get("/api/a2a/rooms/room-seatfs/files",
                        params={"seat": seated["seat"]})
    names = {row["path"] for row in listed.json()["files"]}
    assert {"notes.txt", "sub/roundtrip.bin"} <= names
    fetched = client.get("/api/a2a/rooms/room-seatfs/file",
                         params={"seat": seated["seat"], "path": "sub/roundtrip.bin"})
    assert fetched.status_code == 200
    assert fetched.content == payload


def test_the_wire_has_no_delete(client):
    """Source pin on the named boundary: destruction of the shared folder stays
    with the members on the machine that owns it."""
    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    lane = source.split("the room workspace over the wire")[1][:9000]
    assert "@app.delete" not in lane
    assert "DELETING over the" in lane and "stays with" in lane


def test_every_guest_verb_is_wired_to_a_handler():
    """MUTATION: register a subcommand with `func=` instead of `handler=`.

    `main` dispatches on `args.handler`, so a verb registered under any other name
    parses, prints its own `--help`, and dies with an AttributeError the moment
    somebody actually runs it. That is exactly how `verify` shipped: twelve
    subparsers used `handler=`, the thirteenth used `func=`, and the tests called
    `cmd_verify` directly and never went through the parser at all. It was found by
    a stranger on another machine running the verb for its intended purpose.

    Asserted against the parser OBJECT rather than the source text, so a verb added
    with a different spelling still has to answer for itself.
    """
    import argparse
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("a2a_guest_wiring", WIRE_PEER)
    guest = importlib.util.module_from_spec(spec)
    sys.modules["a2a_guest_wiring"] = guest
    spec.loader.exec_module(guest)

    captured = {}
    real = argparse.ArgumentParser.parse_args

    def capture(self, argv=None, namespace=None):
        # The parser is built inside main(); this is the seam that hands it over
        # without asking the file to be restructured for a test.
        captured["parser"] = self
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = capture
    try:
        try:
            guest.main(["rooms"])
        except SystemExit:
            pass
    finally:
        argparse.ArgumentParser.parse_args = real

    parser = captured.get("parser")
    assert parser is not None, "the guest client's parser could not be reached"
    actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert actions, "the guest client has no subcommands"

    unwired = sorted(name for name, sub in actions[0].choices.items()
                     if "handler" not in (sub._defaults or {}))
    assert not unwired, f"these verbs parse but cannot run: {unwired}"


def test_the_document_names_every_verb_the_client_actually_has():
    """The registry/doc copy pair for VERBS, guarded the way the MCP tools already are.

    A verb the document does not name is a surface a stranger cannot find, and a verb
    the document names that the file does not have is an instruction that fails in
    their hands. Set equality in BOTH directions, because both have gone wrong here.
    """
    import argparse
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("a2a_guest_verbs", WIRE_PEER)
    guest = importlib.util.module_from_spec(spec)
    sys.modules["a2a_guest_verbs"] = guest
    spec.loader.exec_module(guest)

    doc = (Path(__file__).resolve().parents[1] / "docs" / "agents" /
           "A2A_PROTOCOL.md").read_text(encoding="utf-8")
    # From "and speaks" to the end of that sentence. Slicing there and not earlier
    # matters: the seat path in the line above carries a dot of its own.
    described = doc.split("and speaks", 1)[1].split(".", 1)[0]
    named = set(re.findall(r"`([a-z]+)`", described))

    captured = {}
    real = argparse.ArgumentParser.parse_args

    def capture(self, argv=None, namespace=None):
        captured["parser"] = self
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = capture
    try:
        try:
            guest.main(["rooms"])
        except SystemExit:
            pass
    finally:
        argparse.ArgumentParser.parse_args = real

    actions = [a for a in captured["parser"]._actions
               if isinstance(a, argparse._SubParsersAction)]
    verbs = set(actions[0].choices)
    assert verbs == named, (f"the document and the client disagree: "
                            f"only in the file {sorted(verbs - named)}, "
                            f"only in the document {sorted(named - verbs)}")
