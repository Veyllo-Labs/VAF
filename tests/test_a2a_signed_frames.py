# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A signature on a frame, and what a reader may conclude from one.

The room has always ASSIGNED authorship: ingest overwrites `from` with the admitted
peer, which is sound while the host is the one who admitted the connection and says
nothing at all to somebody reading the transcript on another machine. These tests
defend the part that does travel.

Two separations carry the whole design and both are defended here. A signature
covers CONTENT and never placement, so a sender can produce one without knowing the
sequence number it will be given. And whether the key BELONGS to the peer a frame is
filed under is a second question, folded from the room's join frames rather than read
from the member files, because a member file is mutable and the log is not.
"""
from pathlib import Path

import pytest

import vaf.core.a2a.store as store_mod
from vaf.core import data_files
from vaf.core.a2a import signing
from vaf.core.a2a.room import (MalformedContent, NotPermitted, Room, derive_peer_id,
                               participant_key)


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    return tmp_path


@pytest.fixture()
def signed(rooms):
    """A room whose one member holds a key on this machine."""
    key = participant_key("cli", "scope-a")
    room = Room.create(kind="round", owner_scope="scope-a", base=rooms, room_id="room-sig")
    me = room.join(display="Alice", scope_id="scope-a",
                   peer_id=derive_peer_id(key, "room-sig"), participant_key=key)
    return room, me, key


def _rewrite(room, frame, mutate):
    """Change a stored frame on disk, the way a dishonest host would."""
    path = room.store.lane(frame.sender) / f"{frame.seq:012d}.json"
    stored = data_files.read_json(path, default=None)
    mutate(stored)
    data_files.write_json_atomic(path, stored)


# ── the ordinary paths ──────────────────────────────────────────────────────

def test_a_peer_that_holds_a_key_signs_without_being_asked(signed):
    room, me, _ = signed
    frame = room.say(me, "signiert gesprochen")
    assert frame.sig and frame.sig["alg"] == "ed25519"
    assert room.verdict_for(frame) == "valid"


def test_the_join_publishes_the_key_and_the_fold_finds_it(signed):
    room, me, key = signed
    assert room.signing_keys() == {me.peer_id: signing.public_key(key, room.room_id)}


def test_a_peer_without_a_key_here_keeps_sending_unsigned_frames(rooms):
    """A guest has no account on this machine and therefore no key. Nothing about
    that is an error: an unsigned frame is what every frame was until now."""
    room = Room.create(kind="round", owner_scope=None, base=rooms, room_id="room-guest")
    guest = room.join(display="Guest", scope_id=None, peer_id="p-guest")
    frame = room.say(guest, "hallo")
    assert frame.sig is None
    assert room.verdict_for(frame) == "unsigned"
    assert room.signing_keys() == {}


def test_a_signature_survives_the_round_trip_through_the_file(signed):
    room, me, _ = signed
    frame = room.say(me, "hallo")
    reread = [f for f in room.store.frames() if f.id == frame.id][0]
    assert reread.sig == frame.sig
    assert room.verdict_for(reread) == "valid"


# ── what a dishonest host can and cannot do ─────────────────────────────────

def test_changing_a_stored_frame_makes_the_verdict_say_so(signed):
    """The claim the whole feature makes: a host can still omit, but it cannot
    forge. The frame stays in the transcript, because removing it would tear the
    logical clock for every reader after it."""
    room, me, _ = signed
    frame = room.say(me, "das Original")
    _rewrite(room, frame, lambda s: s["body"].__setitem__("text", "etwas anderes"))

    reread = [f for f in room.store.frames() if f.id == frame.id][0]
    assert reread.body["text"] == "etwas anderes"
    assert room.verdict_for(reread) == "invalid"
    assert len(room.store.frames()) == len(room.verify_frames())


def test_swapping_only_the_key_breaks_the_signature(signed):
    room, me, _ = signed
    frame = room.say(me, "hallo")
    other = signing.public_key(participant_key("cli", "scope-b"), room.room_id)
    _rewrite(room, frame, lambda s: s["sig"].__setitem__("key", other))

    reread = [f for f in room.store.frames() if f.id == frame.id][0]
    assert room.verdict_for(reread) == "invalid"


def test_a_real_signature_by_a_key_the_peer_never_published_is_not_valid(signed):
    """The verdict that separates "somebody tampered" from "this is not who it says".
    The signature verifies perfectly; it is simply by a key this peer never put in
    the room. That is what a frame written into the wrong lane looks like, and it is
    why the key check is a second question and not folded into the first."""
    room, me, _ = signed
    frame = room.say(me, "hallo")
    stranger = participant_key("cli", "scope-b")

    content = {field: getattr(frame, field) for field in signing.COVERED}
    forged = signing.sign(signing.covered_payload(room.room_id, content),
                          participant_key=stranger, room_id=room.room_id)
    _rewrite(room, frame, lambda s: s.__setitem__("sig", forged))

    reread = [f for f in room.store.frames() if f.id == frame.id][0]
    assert room.verdict_for(reread) == "foreign_key"


def test_the_key_is_folded_from_the_log_and_not_from_the_member_file(signed):
    """The member file is mutable and lives on the host's disk. If the fold read it,
    a host could swap a key there and forge every later frame from that peer."""
    room, me, key = signed
    record = room.store.member(me.peer_id) or {}
    record["sign_key"] = "f" * 64
    record["card"] = {"sign_key": "f" * 64}
    room.store.put_member(me.peer_id, record)

    assert room.signing_keys()[me.peer_id] == signing.public_key(key, room.room_id)
    assert room.verdict_for(room.say(me, "hallo")) == "valid"


def test_rejoining_rotates_the_key_and_rejoining_without_one_withdraws_it(signed, rooms):
    room, me, key = signed
    assert room.signing_keys()

    room.join(display="Alice", scope_id="scope-a", peer_id=me.peer_id)
    assert room.signing_keys() == {}, "a rejoin that publishes nothing withdraws the claim"


# ── what a peer may present ─────────────────────────────────────────────────

def test_a_presented_signature_over_the_composed_content_is_accepted(signed):
    """The path a foreign implementation takes: ask what will be stored, sign that,
    hand both over."""
    room, me, key = signed
    payload = {"kind": "say", "body": {"text": "von aussen signiert"}}
    content = room.compose(payload)
    sig = signing.sign(signing.covered_payload(room.room_id, content),
                       participant_key=key, room_id=room.room_id)

    frame = room.ingest({**payload, "sig": sig}, identity=me)
    assert frame.sig == sig
    assert room.verdict_for(frame) == "valid"


def test_a_signature_over_something_else_is_refused(signed):
    """Refused rather than stored with a note. One message with two valid readings
    is the canonicalisation-divergence class, and it is what lets a verifier and a
    renderer be made to disagree."""
    room, me, key = signed
    other = room.compose({"kind": "say", "body": {"text": "etwas anderes"}})
    sig = signing.sign(signing.covered_payload(room.room_id, other),
                       participant_key=key, room_id=room.room_id)

    with pytest.raises(NotPermitted):
        room.ingest({"kind": "say", "body": {"text": "das hier"}, "sig": sig},
                    identity=me)


def test_a_ballot_must_be_signed_in_its_resolved_form(signed):
    """The case the fixed point was built for. A sender that signs its shorthand
    signs something the room does not store, and is told so."""
    room, me, key = signed
    vote = room.open_vote(me, "weiter?", options=["ja, weiter so", "erst schlafen"])

    raw = {"kind": "answer", "reply_to": vote.id, "body": {"choice": "ja"}}
    naive = signing.sign(signing.covered_payload(room.room_id, {
        "kind": "answer", "reply_to": vote.id, "body": {"choice": "ja"},
        "to": {"room": True}, "must_understand": (), "ext": {}}),
        participant_key=key, room_id=room.room_id)
    with pytest.raises(NotPermitted):
        room.ingest({**raw, "sig": naive}, identity=me)

    composed = room.compose(raw)
    good = signing.sign(signing.covered_payload(room.room_id, composed),
                        participant_key=key, room_id=room.room_id)
    frame = room.ingest({**raw, "sig": good}, identity=me)
    assert frame.body["choice"] == "ja, weiter so"
    assert room.verdict_for(frame) == "valid"


@pytest.mark.parametrize("presented", ["x", 5, [], {"alg": "rsa"},
                                       {"alg": "ed25519", "key": "a" * 63, "sig": "b" * 128}])
def test_a_sig_field_the_room_cannot_read_is_refused_as_malformed(signed, presented):
    room, me, _ = signed
    with pytest.raises(MalformedContent):
        room.ingest({"kind": "say", "body": {"text": "hi"}, "sig": presented},
                    identity=me)


# ── the verdict itself ──────────────────────────────────────────────────────

def test_a_verdict_never_raises_and_never_drops_a_frame(signed):
    """A verifier walks a whole transcript. One frame it cannot judge must cost that
    frame its verdict, never the walk, and never the frame."""
    room, me, _ = signed
    frames = [room.say(me, f"nummer {n}") for n in range(3)]
    _rewrite(room, frames[1], lambda s: s.__setitem__("sig", {"alg": "ed25519"}))

    verdicts = room.verify_frames()
    assert len(verdicts) == len(room.store.frames())
    assert [v for f, v in verdicts if f.id == frames[1].id] == ["unreadable"]


def test_a_signature_is_not_part_of_the_content_it_covers(signed):
    """compose settles content. A signature is a claim ABOUT that content, so it
    cannot be inside it, or it would have to cover itself."""
    room, _, _ = signed
    composed = room.compose({"kind": "say", "body": {"text": "hi"},
                             "sig": {"alg": "ed25519"}})
    assert "sig" not in composed
    assert room.compose(composed) == composed


# ── the projections that would otherwise drop it ────────────────────────────

def test_every_transcript_row_carries_a_verdict(signed):
    """Four surfaces render this transcript. A surface that decided authorship for
    itself would be a second opinion about it, so the verdict travels with the row -
    and the verdict travels, never the signature, because a renderer has no use for
    128 hex characters and every projection here that carried raw material it did not
    need has ended up dropping or leaking it."""
    room, me, _ = signed
    room.say(me, "hallo")
    rows = room.transcript()
    assert rows and all("verdict" in row for row in rows)
    assert [r["verdict"] for r in rows if r["kind"] == "say"] == ["valid"]


def test_the_machine_facing_row_carries_it_too(signed):
    """`vaf a2a read` is what a foreign agent parses. A field it never receives is a
    field it cannot act on, whatever the room knows."""
    from vaf.cli.cmd.a2a import _row

    room, me, _ = signed
    room.say(me, "hallo")
    entry = [r for r in room.transcript() if r["kind"] == "say"][0]
    assert _row(entry)["verdict"] == "valid"


def test_a_room_where_nobody_signs_says_unsigned_rather_than_nothing(rooms):
    """The ordinary case has to have a word of its own, or a consumer cannot tell
    "nobody claimed anything" from "this surface forgot to tell me"."""
    from vaf.cli.cmd.a2a import _row

    room = Room.create(kind="round", owner_scope=None, base=rooms, room_id="room-plain")
    guest = room.join(display="Guest", scope_id=None, peer_id="p-guest")
    room.say(guest, "hallo")
    entry = [r for r in room.transcript() if r["kind"] == "say"][0]
    assert entry["verdict"] == "unsigned"
    assert _row(entry)["verdict"] == "unsigned"
