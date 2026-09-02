# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What the room will store, settled before it is stored.

`Room.compose` answers one question - what content will this room actually write
for this submission - and answers it the same way twice. Everything here defends
that fixed point, because a fixed point is what lets a sender commit to its own
words: without it, a peer asking the room what it is about to record would be told
about a draft the room then rewrites, and no later reader could tell a
normalisation apart from a tampering.

The second half defends the symmetry underneath it. A frame lives twice, once as
the object that minted it and once as the object a reader parses out of the file,
and where the two disagreed one frame had two meanings.
"""
from pathlib import Path

import pytest

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.frame import Frame
from vaf.core.a2a.hub import Hub
from vaf.core.a2a.room import MalformedContent, Room, RoomError


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    return tmp_path


@pytest.fixture()
def room(rooms):
    room = Room.create(kind="round", owner_scope=None, base=rooms, room_id="room-compose")
    alice = room.join(display="Alice", scope_id=None, peer_id="p-alice")
    return room, alice


# Every shape a door hands the room, including the ones that used to be normalised
# somewhere else or not at all.
SUBMISSIONS = [
    {"kind": "say", "body": {"text": "hi"}},
    {"kind": "say"},
    {"kind": "say", "to": {}},
    {"kind": "say", "to": {"peer": "p-bob"}},
    {"kind": "ask", "body": {"text": "?"}, "must_understand": [1, 2]},
    {"kind": "report", "body": {"text": "x", "status": "working"}, "reply_to": ""},
    {"kind": "say", "body": {"text": "x"}, "ext": {"vendor": {"a": 1}}},
    {"kind": "vote", "body": {"text": "q", "options": [" ja, weiter so ", "erst schlafen", "  "]}},
    {"kind": "vote", "body": {"text": "q"}},
]


@pytest.mark.parametrize("payload", SUBMISSIONS)
def test_compose_is_a_fixed_point(room, payload):
    """compose(compose(x)) == compose(x). The whole contract, in one line."""
    once = room[0].compose(payload)
    assert room[0].compose(once) == once


def test_a_ballot_composed_twice_is_the_same_ballot(room):
    """The ballot is the case the fixed point was needed for: the choice is
    resolved against the vote's options, and resolving the resolved form changes
    nothing."""
    r, alice = room
    vote = r.open_vote(alice, "weiter?", options=["ja, weiter so", "erst schlafen"])
    once = r.compose({"kind": "answer", "reply_to": vote.id, "body": {"choice": "ja"}})
    assert once["body"]["choice"] == "ja, weiter so"
    assert r.compose(once) == once


@pytest.mark.parametrize("payload", SUBMISSIONS)
def test_ingest_stores_exactly_what_compose_promised(room, payload):
    """A sender told what will be stored must be told the truth. This is the
    property a signature would rest on, and it is worth pinning before there is
    one: if ingest ever normalises something compose does not, the promise is a
    lie and nobody finds out until a verifier disagrees."""
    r, alice = room
    promised = r.compose(payload)
    frame = r.ingest(payload, identity=alice)
    assert {"kind": frame.kind, "to": frame.to, "body": frame.body,
            "reply_to": frame.reply_to, "must_understand": frame.must_understand,
            "ext": frame.ext} == promised


def test_the_counted_choice_is_the_stored_choice(room):
    """A vote whose options carry whitespace used to produce a verified-looking
    ballot counted under a column nobody was offered: the resolver matched the
    option exactly, stored it with its blank, and the fold counted the stripped
    form. The options are trimmed where the choice is, so the two cannot drift."""
    r, alice = room
    vote = r.open_vote(alice, "weiter?", options=[" ja, weiter so ", "erst schlafen"])
    assert vote.body["options"] == ["ja, weiter so", "erst schlafen"]

    ballot = r.ingest({"kind": "answer", "reply_to": vote.id,
                       "body": {"choice": "ja"}}, identity=alice)
    entry = [v for v in r.votes() if v["id"] == vote.id][0]
    assert list(entry["tally"]) == [ballot.body["choice"]]
    assert ballot.body["choice"] in vote.body["options"]


def test_a_vote_that_names_no_options_still_has_answers(room):
    r, alice = room
    vote = r.open_vote(alice, "weiter?")
    assert vote.body["options"] == ["yes", "no"]


@pytest.mark.parametrize("field,value", [
    ("ext", "x"), ("ext", ["a"]), ("ext", 5),
    ("body", "x"), ("body", [1]),
    ("to", "x"), ("to", 7),
])
def test_a_field_that_is_not_an_object_is_refused_as_a_room_error(room, field, value):
    """These used to reach `dict(value)` and raise a bare ValueError or TypeError
    past whatever door was holding the submission. `Hub.submit` catches `RoomError`
    and nothing else, so on a live room socket the receive loop ended and the peer
    lost its line without an ack. The shape is the room's judgement about a message,
    so it is refused the way every other judgement is."""
    r, alice = room
    with pytest.raises(MalformedContent) as caught:
        r.ingest({"kind": "say", "body": {"text": "hi"}, field: value}, identity=alice)
    assert isinstance(caught.value, RoomError)
    assert field in str(caught.value)


def test_must_understand_as_a_bare_string_is_refused(room):
    """tuple("id") is ('i', 'd'): three field names nobody asked for."""
    r, alice = room
    with pytest.raises(MalformedContent):
        r.ingest({"kind": "say", "must_understand": "id"}, identity=alice)


def test_the_hub_answers_a_malformed_field_instead_of_raising(rooms):
    """The regression that matters at the socket: a peer sending `ext: "x"` gets an
    ack back, rather than an exception that ends its connection unanswered."""
    room = Room.create(kind="round", owner_scope=None, base=rooms, room_id="room-hub-shape")
    alice = room.join(display="Alice", scope_id=None, peer_id="p-alice")
    hub = Hub(room, sink=lambda peer, message: None)
    token = hub.attach(alice)
    answer = hub.submit(alice, token, {"kind": "say", "body": {"text": "hi"}, "ext": "x"})
    assert answer["status"] == "refused"
    assert "ext" in answer["reason"]


# ── the symmetry the fixed point rests on ───────────────────────────────────

@pytest.mark.parametrize("field,given", [
    ("must_understand", [1, 2]),
    ("must_understand", ()),
    ("reply_to", ""),
    ("reply_to", "f-1"),
    ("to", {}),
    ("body", {}),
    ("ext", {}),
])
def test_a_minted_frame_reads_back_as_itself(field, given):
    """`Frame.new` and `Frame.from_dict` must read every field the same way.

    Where they did not, one frame had two meanings depending on which side of a
    file you were standing: `must_understand=[1, 2]` was `(1, 2)` in memory and
    `('1', '2')` after a round trip, and `reply_to=""` was `''`, then absent from
    the file, then `None`. Nothing read those closely enough to break, which is
    exactly why it went unnoticed."""
    minted = Frame.new(room="r", sender="p-a", role="peer", kind="say",
                       seq=1, lamport=1, **{field: given})
    parsed = Frame.from_dict(minted.to_dict(), enforce_requirements=False)
    assert getattr(minted, "sender" if field == "from" else field) == getattr(parsed, field)


def test_minting_a_frame_with_a_field_that_is_not_an_object_says_so():
    """A stored frame stays readable whatever is in the file (rule 2), so
    `from_dict` reads a non-object as empty. A frame being MINTED has a caller in
    front of it, and telling that caller beats writing a shape the reader would
    then silently discard."""
    from vaf.core.a2a.frame import MalformedFrame

    with pytest.raises(MalformedFrame):
        Frame.new(room="r", sender="p-a", role="peer", kind="say",
                  seq=1, lamport=1, ext="not an object")


def test_a_vote_stored_before_the_trimming_rule_still_answers_ballots(room):
    """Reading options through the same rule that writes them heals a room that
    already holds a vote with a stray space in an option. Without it, every ballot
    on such a vote is refused forever: the resolver matches exactly, and nobody
    types the space."""
    r, alice = room
    stale = r.ingest({"kind": "vote",
                      "body": {"text": "weiter?", "options": ["ja, weiter so", "nein"]}},
                     identity=alice)
    # Write the untrimmed form straight into the file, the way an older version did.
    path = r.store.lane(stale.sender) / f"{stale.seq:012d}.json"
    from vaf.core import data_files
    stored = data_files.read_json(path, default=None)
    stored["body"]["options"] = [" ja, weiter so ", "nein"]
    data_files.write_json_atomic(path, stored)

    ballot = r.ingest({"kind": "answer", "reply_to": stale.id,
                       "body": {"choice": "ja"}}, identity=alice)
    assert ballot.body["choice"] == "ja, weiter so"


def test_a_kick_with_a_body_that_is_not_an_object_is_refused(rooms):
    """The same failure `ext` had, one branch further in: the kick target used to be
    read off the body with `.get` before anything had checked it was an object."""
    room = Room.create(kind="chain", owner_scope=None, base=rooms, room_id="room-kick-shape")
    leader = room.join(display="Lead", scope_id=None, peer_id="p-lead", role="leader")
    with pytest.raises(MalformedContent):
        room.ingest({"kind": "kick", "body": "p-someone"}, identity=leader)


# ── the deadline: the one wall clock in a body, and the one that broke a fold ──

@pytest.mark.parametrize("given", ["bald", None, -5, 0, True, [1], {"a": 1},
                                  float("nan"), float("inf")])
def test_an_unusable_deadline_is_dropped_rather_than_stored(room, given):
    """A stored value is read again by every later fold, and a write-once log cannot
    take it back. So the door drops what it cannot use instead of writing it down."""
    r, alice = room
    vote = r.ingest({"kind": "vote", "body": {"text": "q", "options": ["ja", "nein"],
                                              "closes_at": given}}, identity=alice)
    assert "closes_at" not in vote.body


def test_a_deadline_is_stored_as_whole_seconds(room):
    """A deadline is the one value in a body two machines must be able to write down
    identically, and no two languages agree on every float."""
    r, alice = room
    vote = r.ingest({"kind": "vote", "body": {"text": "q", "options": ["ja", "nein"],
                                              "closes_at": 1799999999.7}}, identity=alice)
    assert vote.body["closes_at"] == 1799999999
    assert isinstance(vote.body["closes_at"], int)


def test_a_vote_with_a_deadline_composes_the_same_way_twice(room):
    r, alice = room
    once = r.compose({"kind": "vote", "body": {"text": "q", "options": ["ja"],
                                               "closes_at": 1799999999.7}})
    assert r.compose(once) == once


def test_one_unusable_deadline_does_not_end_voting_in_the_room(room):
    """The regression this pair of readers exists for. `float("bald")` raised inside
    fold_votes, which every vote surface calls - Room.votes, the CLI, the browser and
    the host's own conclusion sweep. The frame cannot be deleted, so a single message
    from any peer would have ended voting in that room permanently."""
    r, alice = room
    good = r.open_vote(alice, "weiter?", options=["ja", "nein"])

    # A frame that reached the store without crossing the door, the way one written
    # by an older version or a foreign implementation would have.
    stale = r.ingest({"kind": "vote", "body": {"text": "alt", "options": ["ja"]}},
                     identity=alice)
    path = r.store.lane(stale.sender) / f"{stale.seq:012d}.json"
    from vaf.core import data_files
    stored = data_files.read_json(path, default=None)
    stored["body"]["closes_at"] = "bald"
    data_files.write_json_atomic(path, stored)

    entries = r.votes()
    assert {e["id"] for e in entries} == {good.id, stale.id}
    assert [e for e in entries if e["id"] == stale.id][0]["closes_at"] == 0.0
