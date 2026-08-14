# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The remote lane: seats, the socket client's contract, and the CLI glue.

The client runs on a machine nobody here can see, against a host nobody there can
see, so everything a stranger cannot debug is pinned: what a seat opens and what it
never opens, what the client refuses BEFORE a socket exists, and that the CLI's
remote lane advances its reading position only after a line is on stdout.
"""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.client import CLOSE_REASONS, RemoteRefused, parse_welcome, room_url
from vaf.core.a2a.room import Room, TicketInvalid

runner = CliRunner()


# ── seats: how a spent ticket comes back ───────────────────────────────────

@pytest.fixture()
def room(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    r = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-seat")
    r.join(display="Owner", scope_id=None, peer_id="p-owner")
    return r


def test_a_seat_reopens_the_same_member_and_only_with_its_secret(room):
    """MUTATION: compare the secret with ==, or skip the hash comparison.

    The seat is what makes an invitation usable more than once without making the
    TICKET reusable - so it must land on exactly the member it was minted for, and
    a guessed secret must open nothing.
    """
    guest = room.join(display="Codex", scope_id=None, peer_id="p-codex")
    seat = room.issue_seat(guest)

    back = room.redeem_seat(seat)
    assert back.peer_id == "p-codex"
    assert back.role == "peer"
    # Reusable, unlike a ticket: the second redemption is the whole point.
    assert room.redeem_seat(seat).peer_id == "p-codex"

    with pytest.raises(TicketInvalid):
        room.redeem_seat(f"{Room.SEAT_PREFIX}p-codex-{'0' * 32}")


def test_the_member_file_keeps_the_hash_and_never_the_secret(room):
    """MUTATION: store the secret so the server can print it again.

    The member file is at rest on the host; a stored secret would turn every member
    record into a credential.
    """
    guest = room.join(display="Codex", scope_id=None, peer_id="p-codex")
    seat = room.issue_seat(guest)
    secret = seat.rsplit("-", 1)[-1]

    record = json.dumps(room.store.member("p-codex"))
    assert secret not in record
    assert "seat_hash" in record


def test_a_seat_dies_with_its_membership(room):
    guest = room.join(display="Codex", scope_id=None, peer_id="p-codex")
    seat = room.issue_seat(guest)
    room.ingest({"kind": "leave", "body": {}}, identity=guest)

    with pytest.raises(TicketInvalid):
        room.redeem_seat(seat)


def test_a_seat_only_opens_the_room_whose_store_holds_its_hash(room, tmp_path):
    guest = room.join(display="Codex", scope_id=None, peer_id="p-codex")
    seat = room.issue_seat(guest)

    other = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-b")
    other.join(display="Owner", scope_id=None, peer_id="p-owner2")
    with pytest.raises(TicketInvalid):
        other.redeem_seat(seat)


def test_admit_issues_a_seat_for_a_ticket_and_for_nothing_else(room):
    """MUTATION: mint a seat on every path.

    An account credential can always reconnect as itself; minting it a seat would
    write a second credential into the store for no one. Only the path that just
    SPENT its way in gets a way back.
    """
    from vaf.core.a2a.room import Identity
    from vaf.core.a2a.wire import admit

    owner = Identity("p-owner", "Owner", None, "peer")
    ticket = room.mint_ticket(owner, display="Codex")

    identity, seat = admit(room, ticket)
    assert seat and seat.startswith(Room.SEAT_PREFIX)

    again, none = admit(room, seat)
    assert again.peer_id == identity.peer_id
    assert none is None, "a seat presented back minted another seat"


# ── the client's contract, before any socket exists ────────────────────────

def test_a_plain_ws_url_is_refused_before_any_socket_exists():
    """MUTATION: allow ws:// for testing.

    The credential rides in the query string. Unencrypted, that is a bearer token
    in the clear on somebody's network - so the refusal happens at URL parse time,
    where no code path can have sent anything yet.
    """
    with pytest.raises(RemoteRefused):
        room_url("ws://host:8443/ws/a2a/room-x")
    with pytest.raises(RemoteRefused):
        room_url("https://host:8443/ws/a2a/room-x")
    with pytest.raises(RemoteRefused):
        room_url("wss://host:8443/api/other")

    parts = room_url("wss://host:8443/ws/a2a/room-x")
    assert parts == {"origin": "wss://host:8443", "room_id": "room-x",
                     "path": "/ws/a2a/room-x"}


def test_the_welcome_is_checked_and_never_guessed():
    """MUTATION: read frames from whatever answered.

    A server that is not a vaf-a2a v1 room must be left, not interpreted - frames
    read from it would be attributed to a room that never said them.
    """
    good = parse_welcome({"kind": "welcome", "room": "room-x", "peer": "p-1",
                          "role": "peer", "protocol": "vaf-a2a", "v": 1,
                          "seat": "s-p-1-aa"})
    assert good == {"room": "room-x", "peer": "p-1", "role": "peer", "seat": "s-p-1-aa"}

    no_seat = parse_welcome({"kind": "welcome", "room": "room-x", "peer": "p-1",
                             "role": "peer", "protocol": "vaf-a2a", "v": 1})
    assert no_seat["seat"] is None

    with pytest.raises(RemoteRefused):
        parse_welcome({"kind": "ack", "status": "committed"})
    with pytest.raises(RemoteRefused):
        parse_welcome({"kind": "welcome", "protocol": "vaf-a2a", "v": 2})
    with pytest.raises(RemoteRefused):
        parse_welcome({"kind": "welcome", "protocol": "other", "v": 1})


def test_every_close_code_the_door_sends_has_a_sentence():
    """The server closes with a machine code; a person reads a sentence. A code
    with no sentence surfaces as a bare number on a stranger's terminal."""
    import re

    source = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "web_server.py"
              ).read_text(encoding="utf-8")
    endpoint = source.split("async def a2a_room_endpoint")[1].split("\nasync def ")[0]
    sent = {int(c) for c in re.findall(r"close\(code=(\d{4})", endpoint)}
    wire = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "a2a" / "wire.py"
            ).read_text(encoding="utf-8")
    sent |= {int(c) for c in re.findall(r"code=(\d{4})", wire)}

    missing = {c for c in sent if c >= 4000} - set(CLOSE_REASONS)
    assert not missing, f"close codes with no human sentence: {sorted(missing)}"


# ── the CLI glue, with the socket faked at the module seam ─────────────────

class _FakeRemote:
    """Stands in for RemoteRoom: scripted welcome, frames and acks."""

    script = {}

    def __init__(self):
        s = type(self).script
        self.room_id = s.get("room", "room-far")
        self.peer_id = s.get("peer", "p-far")
        self.role = s.get("role", "peer")
        self.seat = s.get("seat")

    @classmethod
    def connect(cls, url, credential, **kw):
        cls.script.setdefault("connects", []).append((url, credential))
        return cls()

    def frames(self, *, timeout=None):
        yield from type(self).script.get("frames", [])
        raise TimeoutError()

    def submit(self, payload, **kw):
        type(self).script.setdefault("submitted", []).append(payload)
        return type(self).script.get("ack", {"kind": "ack", "status": "committed",
                                             "frame": "f-1", "seq": 1, "lamport": 9})

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


@pytest.fixture()
def far(tmp_path, monkeypatch):
    """A remote room record on disk, the socket faked at the module seam the CLI
    imports from, and the store pointed away from the real machine."""
    import vaf.cli.cmd.a2a as cli
    import vaf.core.a2a.client as client_mod

    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path / "local")
    monkeypatch.setattr(cli, "_remote_dir", lambda: tmp_path / "remote")
    (tmp_path / "remote").mkdir(parents=True, exist_ok=True)
    _FakeRemote.script = {"peer": "p-far", "seat": "s-p-far-aa"}
    monkeypatch.setattr(client_mod, "RemoteRoom", _FakeRemote)
    (tmp_path / "remote" / "room-far.json").write_text(json.dumps({
        "url": "wss://h:8443/ws/a2a/room-far", "peer": "p-far",
        "role": "peer", "seat": "s-p-far-aa", "cursor": 0}), encoding="utf-8")
    return tmp_path


def test_join_url_stores_the_seat_for_every_later_command(far, monkeypatch):
    """MUTATION: emit the join and forget the seat.

    Without the stored seat, the ticket is spent and every later wait or say has
    nothing to present - the invitation worked exactly once and then the room
    looks broken from the other machine.
    """
    from vaf.cli.cmd.a2a import app

    (far / "remote" / "room-new.json").unlink(missing_ok=True)
    _FakeRemote.script = {"room": "room-new", "peer": "p-new", "role": "peer",
                          "seat": "s-p-new-bb"}
    result = runner.invoke(app, ["join", "room-new", "--ticket", "t-x",
                                 "--url", "wss://h:8443/ws/a2a/room-new"])
    assert result.exit_code == 0, result.output

    saved = json.loads((far / "remote" / "room-new.json").read_text(encoding="utf-8"))
    assert saved["seat"] == "s-p-new-bb"
    assert saved["url"] == "wss://h:8443/ws/a2a/room-new"


def test_say_finds_the_remote_room_with_no_url_flag(far):
    from vaf.cli.cmd.a2a import app

    result = runner.invoke(app, ["say", "room-far", "hello over there"])
    assert result.exit_code == 0, result.output
    assert _FakeRemote.script["submitted"][-1]["body"]["text"] == "hello over there"
    assert _FakeRemote.script["connects"][-1] == (
        "wss://h:8443/ws/a2a/room-far", "s-p-far-aa"), "the seat was not presented"
    assert json.loads(result.output.strip().splitlines()[-1])["remote"] is True


def test_remote_wait_prints_before_it_advances(far):
    """MUTATION: advance the stored cursor before writing the line.

    The store's own rule, on the client's side of the wire: an interrupted wait
    costs a repeated line, never a swallowed one.
    """
    from vaf.cli.cmd.a2a import app

    _FakeRemote.script["frames"] = [
        {"kind": "sync", "room": "room-far", "lamport": 3},
        {"kind": "say", "id": "f-a", "from": "p-other", "role": "peer",
         "lamport": 3, "ts": 1.0, "body": {"text": "first"}},
        {"kind": "say", "id": "f-b", "from": "p-other", "role": "peer",
         "lamport": 4, "ts": 2.0, "body": {"text": "second"}},
    ]
    result = runner.invoke(app, ["wait", "room-far", "--n", "1"])
    assert result.exit_code == 0, result.output

    lines = [json.loads(l) for l in result.output.strip().splitlines()]
    assert [l["text"] for l in lines] == ["first"]
    saved = json.loads((far / "remote" / "room-far.json").read_text(encoding="utf-8"))
    assert saved["cursor"] == 3, "the cursor moved past a line that was never printed"


def test_remote_wait_skips_own_echo_and_ends_on_close(far):
    from vaf.cli.cmd.a2a import app, EXIT_CLOSED

    _FakeRemote.script["frames"] = [
        {"kind": "say", "id": "f-own", "from": "p-far", "role": "peer",
         "lamport": 5, "ts": 1.0, "body": {"text": "my own words"}},
        {"kind": "close", "id": "f-c", "from": "p-owner", "role": "peer",
         "lamport": 6, "ts": 2.0, "body": {"reason": "done"}},
    ]
    result = runner.invoke(app, ["wait", "room-far", "--n", "5"])
    assert result.exit_code == EXIT_CLOSED, result.output
    kinds = [json.loads(l)["kind"] for l in result.output.strip().splitlines()]
    assert kinds == ["close"], "an own echo was printed, or the close was not"


def test_a_room_that_is_neither_local_nor_seated_keeps_its_exit_code(far):
    from vaf.cli.cmd.a2a import app, EXIT_NO_ROOM

    result = runner.invoke(app, ["say", "room-nowhere", "hello"])
    assert result.exit_code == EXIT_NO_ROOM
