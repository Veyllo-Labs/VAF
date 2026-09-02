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
    forged = signing.sign(signing.covered_payload(room.room_id, me.peer_id, content),
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
    sig = signing.sign(signing.covered_payload(room.room_id, me.peer_id, content),
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
    sig = signing.sign(signing.covered_payload(room.room_id, me.peer_id, other),
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
    naive = signing.sign(signing.covered_payload(room.room_id, me.peer_id, {
        "kind": "answer", "reply_to": vote.id, "body": {"choice": "ja"},
        "to": {"room": True}, "must_understand": (), "ext": {}}),
        participant_key=key, room_id=room.room_id)
    with pytest.raises(NotPermitted):
        room.ingest({**raw, "sig": naive}, identity=me)

    composed = room.compose(raw)
    good = signing.sign(signing.covered_payload(room.room_id, me.peer_id, composed),
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


# ── the lane the whole feature exists for ───────────────────────────────────

def test_the_host_never_signs_for_a_remote_peer(rooms):
    """The defect this rule was written against, and it is the one that would have
    mattered most.

    A remote peer's key would be derived HERE, from this machine's root secret. A
    signature made for it would say "the host wrote this under that peer's handle"
    while reading as "that peer wrote this" - which against a dishonest host is worth
    nothing, and is worse than nothing, because it makes `valid` mean less than it
    says on the one lane the feature exists for. Unsigned is honest and is what those
    frames were before.
    """
    key = participant_key("remote", "scope-far")
    room = Room.create(kind="round", owner_scope=None, base=rooms, room_id="room-far")
    far = room.join(display="Mac", scope_id="scope-far",
                    peer_id=derive_peer_id(key, "room-far"), participant_key=key)

    assert room.signing_keys() == {}, "a key the peer does not hold must not be published"
    frame = room.say(far, "vom anderen Rechner")
    assert frame.sig is None
    assert room.verdict_for(frame) == "unsigned"


def test_a_remote_peer_that_signs_for_itself_is_valid(rooms):
    """The way a remote peer DOES get a signature: it presents one. Nothing about the
    refusal above stops that, and this is the path a second machine takes."""
    key = participant_key("remote", "scope-far")
    room = Room.create(kind="round", owner_scope=None, base=rooms, room_id="room-far2")
    far = room.join(display="Mac", scope_id="scope-far",
                    peer_id=derive_peer_id(key, "room-far2"), participant_key=key)

    # The announcement is SELF-SIGNED, because the fold no longer counts a key its
    # own join does not attest. A remote peer signs by presenting, here too.
    body = {"display": "Mac", "card": {},
            "sign_key": signing.public_key(key, room.room_id)}
    announce = {"kind": "join", "body": body}
    room.ingest(announce | {"sig": signing.sign(
        signing.covered_payload(room.room_id, far.peer_id, room.compose(announce)),
        participant_key=key, room_id=room.room_id)}, identity=far)

    content = room.compose({"kind": "say", "body": {"text": "selbst signiert"}})
    sig = signing.sign(signing.covered_payload(room.room_id, far.peer_id, content),
                       participant_key=key, room_id=room.room_id)
    frame = room.ingest({"kind": "say", "body": {"text": "selbst signiert"}, "sig": sig},
                        identity=far)
    assert room.verdict_for(frame) == "valid"


@pytest.mark.parametrize("lane,signs", [("agent", True), ("cli", True), ("remote", False)])
def test_only_this_machines_own_actors_are_signed_for(rooms, lane, signs):
    key = participant_key(lane, "scope-x")
    room = Room.create(kind="round", owner_scope=None, base=rooms, room_id=f"room-{lane}")
    who = room.join(display=lane, scope_id="scope-x",
                    peer_id=derive_peer_id(key, f"room-{lane}"), participant_key=key)
    assert bool(room.say(who, "hallo").sig) is signs


# ── the key must attest itself, or a public key is enough to steal a voice ───
#
# The door the suite left open. It had a test for a key NOBODY published and a test
# for a key poisoned in the MEMBER file, so both sides of this one looked covered -
# and the question in between, what happens when the host makes a key known under the
# WRONG handle, was never asked. A public key is public: it sits in the log in plain
# sight, so copying one costs a dishonest host nothing at all.

def test_a_host_cannot_lend_one_peers_key_to_another_lane(signed, rooms):
    """The measured defect, and the whole reason the fold got stricter.

    Alice signs. The host copies her PUBLIC key into Bob's join frame and files her
    frame under Bob's handle. Before the fold asked for attestation this read `valid`,
    which is the strongest verdict the room has, for a sentence Bob never wrote.
    """
    room, alice, _ = signed
    bob = room.join(display="Bob", scope_id=None, peer_id="p-bob")
    spoken = room.say(alice, "nur Alice kann das gesagt haben")
    assert room.verdict_for(spoken) == "valid"
    stolen = spoken.sig["key"]

    joined = [f for f in room.store.frames()
              if f.kind == "join" and f.sender == bob.peer_id][0]
    _rewrite(room, joined, lambda o: o.setdefault("body", {}).__setitem__("sign_key", stolen))

    lifted = data_files.read_json(
        room.store.lane(alice.peer_id) / f"{spoken.seq:012d}.json", default=None)
    lifted["from"], lifted["seq"] = bob.peer_id, 2
    lifted["id"] = "00000000-0000-4000-8000-0000000000bb"
    data_files.write_json_atomic(room.store.lane(bob.peer_id) / f"{2:012d}.json", lifted)

    moved = [f for f in room.store.frames() if f.sender == bob.peer_id and f.seq == 2][0]
    assert room.signing_keys().get(bob.peer_id) != stolen, "an unattested key never binds"
    # `invalid` and not `foreign_key`, because the handle is inside the signed bytes:
    # the move itself broke the signature rather than merely losing its binding.
    assert room.verdict_for(moved) == "invalid"
    assert room.verdict_for(spoken) == "valid", "and Alice is undisturbed"


def test_a_peers_own_attested_join_cannot_be_carried_to_another_handle(signed):
    """The attack the first version of this fix did not stop, and the reason `from`
    is signed.

    Requiring a `join` to be signed by the key it publishes proves POSSESSION, and
    possession said nothing about which handle the announcement was filed under. So a
    host copied the victim's own genuine, fully attested join into another lane, edited
    only the fields no signature covered, and the same key bound under two names. The
    victim's signed sentence, carried across the same way, then read `valid` under
    somebody else's. No key material was needed - only the bytes already on the disk.

    A peer knows its own handle from the moment it is admitted and keeps it for the
    whole room, so unlike `seq`, `lamport`, `id`, `ts` and `role` it is something the
    sender CAN sign. That is the whole difference, and it is why leaving it out looked
    reasonable and was not.
    """
    room, alice, _ = signed
    spoken = room.say(alice, "Ich stimme der Ueberweisung zu.")
    assert room.verdict_for(spoken) == "valid"

    def carry(seq, new_seq, ident):
        stored = data_files.read_json(
            room.store.lane(alice.peer_id) / f"{seq:012d}.json", default=None)
        stored["from"], stored["seq"] = "p-thief", new_seq
        stored["lamport"], stored["id"] = 40 + new_seq, ident
        data_files.write_json_atomic(
            room.store.lane("p-thief") / f"{new_seq:012d}.json", stored)

    carry(1, 1, "00000000-0000-4000-8000-00000000ab01")   # the attested announcement
    carry(spoken.seq, 2, "00000000-0000-4000-8000-00000000ab02")   # and the sentence

    keys = room.signing_keys()
    assert "p-thief" not in keys, "an announcement cannot be carried to another handle"
    assert len(set(keys.values())) == len(keys), "so one key never binds two names"
    lifted = [f for f in room.store.frames() if f.sender == "p-thief" and f.kind == "say"]
    assert [room.verdict_for(f, keys) for f in lifted] == ["invalid"]
    assert room.verdict_for(spoken, keys) == "valid", "and the real one is untouched"


def test_a_join_signed_by_a_different_key_than_it_publishes_binds_nothing(signed):
    """"Signed" is not the question. "Signed BY THAT KEY" is.

    The host leaves a genuine self-signature in place and swaps only the key in the
    body. Without the comparison this would pass as an attested announcement while
    binding a handle to a key nobody proved they hold.
    """
    room, alice, _ = signed
    joined = [f for f in room.store.frames() if f.kind == "join"][0]
    _rewrite(room, joined, lambda o: o.setdefault("body", {}).__setitem__("sign_key", "a" * 64))
    assert room.signing_keys() == {}


def test_a_key_announced_without_a_signature_is_treated_as_no_announcement(rooms):
    """What every guest client did before this rule, stated on its own.

    It is not an accusation and not a forgery - it is a claim with nothing behind it,
    and a reader that cannot check a claim must not act on it.
    """
    room = Room.create(kind="round", owner_scope="s", base=rooms, room_id="room-bare")
    stranger = room.join(display="Gast", scope_id=None, peer_id="p-bare")
    room.ingest({"kind": "join", "body": {"display": "Gast", "card": {},
                                          "sign_key": "b" * 64}}, identity=stranger)
    assert room.signing_keys() == {}


def test_stripping_a_signature_off_a_join_does_not_withdraw_the_key(signed, rooms):
    """The third outcome, and the reason it is not two.

    An unattested key neither binds nor withdraws. Folding it into the withdrawal
    branch would have read tidier and handed the host a new lever it never had: strip
    the `sig` off a stored join and every later frame from an honest peer drops to
    `foreign_key`, which is the verdict that points at somebody.
    """
    room, alice, key = signed
    spoken = room.say(alice, "vor dem Eingriff")
    assert room.verdict_for(spoken) == "valid"

    joined = [f for f in room.store.frames() if f.kind == "join"][0]
    _rewrite(room, joined, lambda o: o.pop("sig", None))

    assert room.signing_keys() == {}, "the first announcement was the only one"
    room.ingest({"kind": "join", "body": {"display": "Alice", "card": {}}},
                identity=alice)
    assert room.verdict_for(spoken) == "foreign_key", "no key, so nothing to bind to"

    # And with an EARLIER attested announcement standing, an unattested one leaves it
    # exactly where it was.
    fresh = Room.create(kind="round", owner_scope="scope-a", base=rooms,
                        room_id="room-sig2")
    me = fresh.join(display="Alice", scope_id="scope-a",
                    peer_id=derive_peer_id(key, "room-sig2"), participant_key=key)
    bound = fresh.signing_keys()
    assert bound, "the first join attested itself"
    fresh.ingest({"kind": "join", "body": {"display": "Alice", "card": {},
                                           "sign_key": "c" * 64}}, identity=me)
    assert fresh.signing_keys() == bound, "an unattested claim changes nothing"


def test_a_peer_really_can_rotate_to_a_different_key(rooms):
    """Rotation, with a key that is actually different.

    The local lane cannot show this: an agent or cli rejoin re-derives the identical
    key from the same two inputs, so the existing rotation test only ever proved
    withdrawal. A presenting peer can hold two keys, and does here.
    """
    room = Room.create(kind="round", owner_scope="s", base=rooms, room_id="room-rot")
    peer = room.join(display="Zwei", scope_id=None, peer_id="p-rot")

    def announce(seed_key):
        body = {"display": "Zwei", "card": {}, "sign_key": signing.public_key(seed_key, room.room_id)}
        payload = {"kind": "join", "body": body}
        return room.ingest(payload | {"sig": signing.sign(
            signing.covered_payload(room.room_id, peer.peer_id, room.compose(payload)),
            participant_key=seed_key, room_id=room.room_id)}, identity=peer)

    first, second = participant_key("cli", "rot-a"), participant_key("cli", "rot-b")
    announce(first)
    assert room.signing_keys()["p-rot"] == signing.public_key(first, room.room_id)
    announce(second)
    assert room.signing_keys()["p-rot"] == signing.public_key(second, room.room_id)

    content = room.compose({"kind": "say", "body": {"text": "mit dem neuen"}})
    frame = room.ingest({"kind": "say", "body": {"text": "mit dem neuen"},
                         "sig": signing.sign(signing.covered_payload(room.room_id,
                                                                 peer.peer_id, content),
                                             participant_key=second, room_id=room.room_id)},
                        identity=peer)
    assert room.verdict_for(frame) == "valid"


def test_a_join_with_a_broken_signature_costs_its_key_and_never_the_walk(signed):
    """The fold now does crypto, so it inherits the rule the verdict already had.

    A garbage signature on a join must cost that join its key and nothing else. If the
    fold could raise, `transcript()` would raise with it - and nothing catches it there.
    """
    room, alice, _ = signed
    joined = [f for f in room.store.frames() if f.kind == "join"][0]
    _rewrite(room, joined, lambda o: o.__setitem__("sig", {"alg": "ed25519", "key": "z" * 64,
                                                           "sig": "z" * 128}))
    assert room.signing_keys() == {}
    assert len(room.verify_frames()) == len(list(room.store.frames()))
    assert len(room.transcript()) >= 1


def test_a_card_that_cannot_be_signed_costs_the_card_and_not_the_key(rooms):
    """The two conditions that must agree, and did not.

    Deriving the key needs only the keyring; signing needs the body to be canonical,
    and a card holding a fractional number is not - no two languages print every float
    alike. Deciding separately let an honest local peer publish a key its own join
    could not attest, which under this fold makes every one of its frames read
    `foreign_key`. Publishing nothing instead would have been worse: a join with no key
    is how a peer WITHDRAWS one. So the card gives way, and it is kept where a card is
    actually read from.
    """
    key = participant_key("cli", "scope-c")
    room = Room.create(kind="round", owner_scope="scope-c", base=rooms, room_id="room-card")
    me = room.join(display="Alice", scope_id="scope-c", card={"load": 0.75},
                   peer_id=derive_peer_id(key, "room-card"), participant_key=key)
    joined = [f for f in room.store.frames() if f.kind == "join"][0]
    assert joined.sig, "the announcement is signed"
    assert (joined.body or {}).get("sign_key"), "and it carries the key"
    assert "card" not in (joined.body or {}), "the card is what gave way"
    assert (room.members()[me.peer_id] or {})["card"] == {"load": 0.75}, \
        "and it is still on the member record, which is where a card is read from"
    assert room.verdict_for(room.say(me, "und das Zimmer laeuft weiter")) == "valid"


def test_an_odd_card_never_retracts_a_binding_the_peer_already_had(rooms):
    """The trap the fallback exists to avoid, pinned so it cannot come back.

    A join with no key withdraws. If an unsignable card had cost the ANNOUNCEMENT
    instead of the card, a peer rejoining with an odd card would have silently
    retracted a key that was already binding its whole history.
    """
    key = participant_key("cli", "scope-d")
    room = Room.create(kind="round", owner_scope="scope-d", base=rooms, room_id="room-odd")
    me = room.join(display="Alice", scope_id="scope-d",
                   peer_id=derive_peer_id(key, "room-odd"), participant_key=key)
    spoken = room.say(me, "vor der zweiten Anmeldung")
    bound = room.signing_keys()
    assert room.verdict_for(spoken) == "valid"

    room.join(display="Alice", scope_id="scope-d", card={"load": 0.75},
              peer_id=me.peer_id, participant_key=key)
    assert room.signing_keys() == bound, "the binding survives an odd card"
    assert room.verdict_for(spoken) == "valid"


def test_a_signed_conversation_does_not_travel_to_another_room(rooms):
    """The room a frame CLAIMS is not the room a reader is in.

    A signature covers the room id, which is what should stop a frame being lifted
    somewhere else. It did not: the verifier rebuilt the covered payload out of the
    frame's own `room` field, so copying a store's files into another room carried the
    verdicts along with them. The door never had this bug - it always used the room's
    own id - so one check disagreed with itself across two functions.
    """
    key = participant_key("cli", "scope-e")
    here = Room.create(kind="round", owner_scope="scope-e", base=rooms, room_id="room-here")
    me = here.join(display="Alice", scope_id="scope-e",
                   peer_id=derive_peer_id(key, "room-here"), participant_key=key)
    said = here.say(me, "nur hier gesagt")
    assert here.verdict_for(said) == "valid"

    there = Room.create(kind="round", owner_scope="scope-e", base=rooms, room_id="room-there")
    for seq in (1, 2):
        src = here.store.lane(me.peer_id) / f"{seq:012d}.json"
        data_files.write_json_atomic(there.store.lane(me.peer_id) / f"{seq:012d}.json",
                                     data_files.read_json(src, default=None))

    assert there.signing_keys() == {}, "an announcement made elsewhere binds nothing here"
    moved = [f for f in there.store.frames() if f.kind == "say"]
    assert [there.verdict_for(f) for f in moved] == ["invalid"]


def test_a_reader_is_told_when_a_line_is_not_what_it_claims(signed):
    """The verdict has to reach the surfaces that render a CONVERSATION, not only the
    one built to answer the question.

    Three projections carried it and the fourth did not: the agent's own room reader
    rebuilds each line field by field and never looked at it, so the participant these
    rooms exist for could not tell a forged line from an ordinary one. `describe` is
    where the wording already lives once for the CLI log, the terminal app and both
    web lanes, which is why it is said there and not a fourth time.
    """
    from vaf.core.a2a.room import describe

    room, alice, key = signed
    spoken = room.say(alice, "wie besprochen")
    rows = {r["id"]: r for r in room.transcript()}
    assert "[" not in describe(rows[spoken.id]), "a good line carries no badge"

    _rewrite(room, spoken, lambda o: o["body"].__setitem__("text", "ganz anders"))
    reread = [f for f in room.store.frames() if f.id == spoken.id][0]
    assert room.verdict_for(reread) == "invalid"
    line = describe({r["id"]: r for r in room.transcript()}[spoken.id])
    assert "signature does not match" in line
    assert "ganz anders" in line, "and the message itself is still shown"


def test_the_agent_can_ask_who_really_wrote_each_line(signed, monkeypatch):
    """The question the agent could not ask, and the way that showed.

    Asked in plain words to verify a room, the agent did not verify it: it opened a
    new one instead. Not a misunderstanding - it had no tool for the question. The
    framework had `verify_frames`, the CLI had `vaf a2a verify`, and the participant
    these rooms exist for had nothing, so it did the nearest thing it could.
    """
    from vaf.tools import room_tools

    room, alice, _ = signed
    monkeypatch.setattr(room_tools, "_acting_key", lambda scope: alice.participant_key)
    monkeypatch.setattr(room_tools, "_open", lambda room_id: room)
    spoken = room.say(alice, "wie besprochen")

    out = room_tools.RoomVerifyTool().run(room_id=room.room_id, user_scope_id="scope-a")
    assert "valid" in out and "provably" in out
    assert "Alice" in out

    quiet = room_tools.RoomVerifyTool().run(room_id=room.room_id, user_scope_id="scope-a",
                                            problems_only=True)
    assert "Nothing here is out of order" in quiet

    _rewrite(room, spoken, lambda o: o["body"].__setitem__("text", "ganz anders"))
    loud = room_tools.RoomVerifyTool().run(room_id=room.room_id, user_scope_id="scope-a",
                                           problems_only=True)
    assert "invalid" in loud and "does not cover this message" in loud
