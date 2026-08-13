# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The hub accelerates delivery. The files stay the truth.

The load-bearing test is the last one: two peers connected to the hub and two peers
reading nothing but the directory must render the room in the SAME order. If that ever
fails, the hub has become a second record and the design is gone.
"""
from pathlib import Path

import pytest

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.frame import canonical_sort_key
from vaf.core.a2a.hub import Hub, NotWriter
from vaf.core.a2a.room import Room


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    return tmp_path


@pytest.fixture()
def wired(rooms):
    """A round with two members, a hub, and a list standing in for two sockets."""
    room = Room.create(kind="round", owner_scope=None, base=rooms, room_id="room-hub")
    alice = room.join(display="Alice", scope_id=None, peer_id="p-alice")
    bob = room.join(display="Bob", scope_id=None, peer_id="p-bob")
    delivered = []
    hub = Hub(room, sink=lambda peer, message: delivered.append((peer, message)))
    return room, hub, alice, bob, delivered


def _texts(delivered, peer):
    return [m.get("body", {}).get("text") for p, m in delivered
            if p == peer and m.get("kind") == "say"]


# ── the commit ─────────────────────────────────────────────────────────────

def test_the_file_exists_before_the_ack_does(wired):
    """MUTATION: acknowledge first and write afterwards.

    A crash between the two must lose an ACKNOWLEDGEMENT, which the peer retries, not a
    message the peer believes arrived. That is the entire ordering contract of the hub.
    """
    room, hub, alice, _bob, _delivered = wired
    seen = []

    def _watching_append(frame, _real=room.store.append):
        seen.append(("written", len(room.store.frames())))
        return _real(frame)

    room.store.append = _watching_append
    token = hub.attach(alice)

    ack = hub.submit(alice, token, {"kind": "say", "body": {"text": "hello"}})

    assert seen, "nothing was written"
    assert ack["status"] == "committed"
    assert [f.body.get("text") for f in room.store.frames() if f.kind == "say"] == ["hello"]


def test_a_write_that_fails_produces_no_committed_ack(wired):
    """MUTATION: report committed regardless of the write.

    An ack that outlives its file is the one failure this design cannot tolerate: every
    other peer would read a conversation the sender believes it took part in.
    """
    room, hub, alice, _bob, _delivered = wired
    token = hub.attach(alice)

    def _explode(_frame):
        raise OSError("disk gone")

    room.store.append = _explode

    with pytest.raises(OSError):
        hub.submit(alice, token, {"kind": "say", "body": {"text": "lost"}})


def test_a_broken_connection_cannot_uncommit_a_frame(rooms):
    """MUTATION: let a sink exception propagate out of submit.

    Fan-out is best effort. One dead socket must cost delivery speed, never the message:
    the peer behind it finds the frame in the directory when it comes back, which is the
    whole reason the files rather than this loop are the record.
    """
    room = Room.create(kind="round", owner_scope=None, base=rooms, room_id="room-broken")
    alice = room.join(display="Alice", scope_id=None, peer_id="p-alice")
    bob = room.join(display="Bob", scope_id=None, peer_id="p-bob")

    def _sink(peer, _message):
        if peer == "p-bob":
            raise RuntimeError("socket closed")

    hub = Hub(room, sink=_sink)
    token = hub.attach(alice)
    hub.attach(bob)

    ack = hub.submit(alice, token, {"kind": "say", "body": {"text": "still committed"}})

    assert ack["status"] == "committed"
    assert [f.body.get("text") for f in room.store.frames() if f.kind == "say"] == \
        ["still committed"]


# ── one writer per peer ────────────────────────────────────────────────────

def test_a_second_connection_for_a_live_peer_is_refused(wired):
    """MUTATION: hand the lease to whoever asked last.

    Two connections acting as one peer would each derive a sequence number from the same
    directory and race for the same file name. Refusing turns a silent collision into a
    sentence the caller can pass on.
    """
    _room, hub, alice, _bob, _delivered = wired
    hub.attach(alice)

    with pytest.raises(NotWriter):
        hub.attach(alice)


def test_submitting_without_the_lease_is_answered_not_raised(wired):
    """A wrong token is a peer's mistake, not the hub's crash: it gets an ack it can
    act on, the same way every other refusal in this protocol is a frame."""
    _room, hub, alice, _bob, _delivered = wired
    hub.attach(alice)

    ack = hub.submit(alice, "not-the-token", {"kind": "say", "body": {"text": "nope"}})
    assert ack["status"] == "not_writer"


def test_an_abandoned_lease_lets_the_peer_back_in(wired):
    """MUTATION: never expire a lease.

    A connection that died holding the lease would otherwise lock its own peer out of
    the room forever, and the peer has no way to release what it no longer holds.
    """
    _room, hub, alice, _bob, _delivered = wired
    clock = [1000.0]
    hub._clock = lambda: clock[0]

    hub.attach(alice)
    clock[0] += 10_000

    hub.attach(alice)  # must not raise


def test_detaching_frees_the_peer_immediately(wired):
    _room, hub, alice, _bob, _delivered = wired
    token = hub.attach(alice)
    hub.detach(alice, token)
    hub.attach(alice)


# ── what a peer is told ────────────────────────────────────────────────────

def test_attaching_replays_what_was_missed_and_says_when_it_is_level(wired):
    """MUTATION: skip the sync marker.

    Without it a reader cannot tell "caught up" from "quiet", and every client would
    have to guess from a pause in traffic.
    """
    room, hub, alice, bob, delivered = wired
    room.say(bob, "while you were away")
    room.say(bob, "and again")

    hub.attach(alice)

    assert _texts(delivered, "p-alice") == ["while you were away", "and again"]
    last = delivered[-1][1]
    assert last["kind"] == "sync" and last["lamport"] > 0


def test_a_peer_is_not_sent_its_own_voice(wired):
    """It wrote the frame and got the ack. Echoing it back is how a client ends up
    rendering everything twice."""
    _room, hub, alice, bob, delivered = wired
    token = hub.attach(alice)
    hub.attach(bob)
    delivered.clear()

    hub.submit(alice, token, {"kind": "say", "body": {"text": "one"}})

    assert _texts(delivered, "p-alice") == []
    assert _texts(delivered, "p-bob") == ["one"]


def test_catching_up_twice_says_the_same_thing(wired):
    """MUTATION: move a cursor inside catch_up.

    Immutable files and non-destructive reads are what make a reconnect idempotent. A
    catch-up that consumed would turn a flaky network into lost messages.
    """
    room, hub, alice, bob, _delivered = wired
    room.say(bob, "one")
    room.say(bob, "two")

    before = room.store.cursor("p-alice")
    first = hub.catch_up(alice, 0)
    second = hub.catch_up(alice, 0)

    assert [f["id"] for f in first] == [f["id"] for f in second]
    assert len(first) >= 2
    assert room.store.cursor("p-alice") == before, (
        "catch_up moved a cursor: a reconnect would then consume what it read")


def test_a_reconnect_gets_exactly_the_rest(wired):
    room, hub, alice, bob, _delivered = wired
    room.say(bob, "before")
    seen = hub.catch_up(alice, 0)
    highest = max(f["lamport"] for f in seen)
    room.say(bob, "after")

    rest = hub.catch_up(alice, highest)
    assert [f["body"]["text"] for f in rest if f["kind"] == "say"] == ["after"]


def test_bookkeeping_is_kept_out_of_the_conversation_view(wired):
    """The same filter the CLI and the wake-up use. Three surfaces, one answer to
    'what counts as something being said'."""
    room, hub, alice, bob, _delivered = wired
    room.say(bob, "a real message")

    kinds = {f["kind"] for f in hub.conversation_since(alice, 0)}
    assert "join" not in kinds and "say" in kinds


# ── the frame rules still apply at the door ────────────────────────────────

def test_a_foreign_major_version_is_refused_at_the_door(wired):
    _room, hub, alice, _bob, _delivered = wired
    token = hub.attach(alice)

    ack = hub.submit(alice, token, {"v": 2, "kind": "say", "body": {"text": "future"}})
    assert ack["status"] == "unsupported_version"


def test_an_unsatisfiable_requirement_is_refused_and_nothing_is_written(wired):
    """MUTATION: ingest first and screen afterwards.

    A peer that cannot honour must_understand has to take NO other action, and writing
    the frame before checking is the loudest possible other action.
    """
    room, hub, alice, _bob, _delivered = wired
    token = hub.attach(alice)
    before = len(room.store.frames())
    on_disk_before = len(list(room.store.lane("p-alice").glob("*.json")))

    ack = hub.submit(alice, token,
                     {"kind": "say", "body": {"text": "x"}, "deadline": "soon",
                      "must_understand": ["deadline"]})

    assert ack["status"] == "unsupported" and ack["fields"] == ["deadline"]
    # Counted on DISK, not through the reader. A first version of this test counted
    # parsed frames and stayed green against "write first, screen after", because the
    # frame it wrote was one the reader then refused - a lesson that cost a real fix in
    # the store: reading is not acting.
    assert len(list(room.store.lane("p-alice").glob("*.json"))) == on_disk_before
    assert len(room.store.frames()) == before


def test_a_declared_extension_satisfies_the_requirement(wired):
    _room, hub, alice, _bob, _delivered = wired
    token = hub.attach(alice)

    ack = hub.submit(alice, token,
                     {"kind": "say", "body": {"text": "x"}, "deadline": "soon",
                      "must_understand": ["deadline"]},
                     understood=("deadline",))
    assert ack["status"] == "committed"


def test_the_rooms_own_refusal_comes_back_as_an_ack(wired):
    """A round refuses a directive. The hub passes the room's sentence on rather than
    inventing a second wording for the same rule."""
    _room, hub, alice, _bob, _delivered = wired
    token = hub.attach(alice)

    ack = hub.submit(alice, token, {"kind": "directive", "body": {"text": "obey"}})
    assert ack["status"] == "refused" and "directive" in ack["reason"]


def test_a_forged_sender_is_overwritten_here_too(wired):
    """The hub is a door, and every door in this protocol resolves authorship rather
    than reading it."""
    room, hub, alice, _bob, _delivered = wired
    token = hub.attach(alice)

    hub.submit(alice, token,
               {"kind": "say", "from": "p-bob", "role": "leader", "body": {"text": "not me"}})

    said = [f for f in room.store.frames() if f.kind == "say"][-1]
    assert said.sender == "p-alice" and said.role == "peer"


# ── the one that decides whether the design still holds ────────────────────

def test_a_file_only_writer_is_invisible_until_a_peer_asks(wired):
    """The hub is not a complete feed, and a client author has to know it.

    Fan-out carries only what passed through this hub. A peer writing straight into the
    directory - the CLI, another process, a machine that never connected - reaches a
    listener when it asks here or reads the files, not before. That is the price of the
    files being the record rather than this object, and it is paid deliberately: the
    alternative is a hub that has to be running for a room to work at all.
    """
    room, hub, alice, bob, delivered = wired
    hub.attach(alice)
    delivered.clear()

    room.say(bob, "written past the hub")

    assert _texts(delivered, "p-alice") == [], "fan-out invented traffic it never saw"
    assert [f["body"]["text"] for f in hub.catch_up(alice, 0) if f["kind"] == "say"] == \
        ["written past the hub"]


def test_hub_peers_and_file_peers_render_the_same_room(rooms):
    """MUTATION: let the hub keep its own ordered list and serve that.

    Two peers on the hub and two peers reading nothing but the directory must agree on
    the order, with no coordination between them. The moment they can disagree, the hub
    has become a second record and the whole design is gone.

    Carol writes past the hub on purpose, so the hub peers only reach the full room by
    catching up - which is exactly what a real client does, and what makes this an
    honest test of the claim rather than a closed loop.
    """
    room = Room.create(kind="round", owner_scope=None, base=rooms, room_id="room-truth")
    alice = room.join(display="Alice", scope_id=None, peer_id="p-alice")
    bob = room.join(display="Bob", scope_id=None, peer_id="p-bob")
    carol = room.join(display="Carol", scope_id=None, peer_id="p-carol")

    streams = {"p-alice": [], "p-bob": []}
    hub = Hub(room, sink=lambda peer, message: streams.setdefault(peer, []).append(message))
    token_a = hub.attach(alice)
    token_b = hub.attach(bob)

    for n in range(4):
        hub.submit(alice, token_a, {"kind": "say", "body": {"text": f"a{n}"}})
        hub.submit(bob, token_b, {"kind": "say", "body": {"text": f"b{n}"}})
        room.say(carol, f"c{n}")             # a file-only peer, writing past the hub

    on_disk = [f.id for f in room.store.frames() if f.kind == "say"]
    assert len(on_disk) == 12

    for identity in (alice, bob):
        # What the connection pushed, plus its own writes, plus what catching up adds.
        pushed = {m["id"] for m in streams[identity.peer_id] if m.get("kind") == "say"}
        own = {f.id for f in room.store.frames()
               if f.sender == identity.peer_id and f.kind == "say"}
        caught = {f["id"] for f in hub.catch_up(identity, 0) if f["kind"] == "say"}
        known = pushed | own | caught

        assert known == set(on_disk), f"{identity.peer_id} cannot see the whole room"
        rendered = sorted((f for f in room.store.frames() if f.id in known),
                          key=canonical_sort_key)
        assert [f.id for f in rendered] == on_disk, identity.peer_id

    # And a peer that never touched the hub at all.
    file_only = Room.open("room-truth", base=rooms)
    assert [f.id for f in file_only.store.frames() if f.kind == "say"] == on_disk
