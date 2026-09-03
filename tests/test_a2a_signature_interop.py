# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Three implementations, one set of signed bytes.

A signature is worth nothing until somebody who did not write the signer can check
it. So the bytes are pinned across all three implementations that exist: VAF, the
VAF-free reference peer (the rules, written from the document), and the VAF-free
wire peer (the single file a guest downloads and runs with no dependencies).

The failure this guards against is the quiet one. If two of them disagree by a
single byte, every signature crossing between them is refused, both sides believe
the other is lying, and nothing in either log says why. It was found the honest way:
a foreign agent on another machine tried seven plausible serialisations against a
real transcript and none verified, because it had no specification to work from.
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from vaf.core.a2a import signing
from vaf.core.a2a.frame import Frame
from vaf.core.a2a.room import content_signature


def _pair(seed=bytes(range(32))):
    """A keypair that is nobody's: this file pins arithmetic, not real identities."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.from_private_bytes(seed)
    return private, private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "examples" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def peers():
    return (_load("a2a_reference_peer_sig", "10_a2a_reference_peer.py"),
            _load("a2a_wire_peer_sig", "12_a2a_wire_peer.py"))


# Shapes chosen for what actually differs between implementations: key order,
# non-ASCII, an absent versus an empty optional, nesting, and whole numbers.
FRAMES = [
    {"v": 1, "id": "f-1", "room": "room-a", "seq": 1, "lamport": 1, "ts": 1.5,
     "from": "p-a", "role": "peer", "kind": "say", "to": {"room": True},
     "body": {"text": "hallo"}},
    {"v": 1, "id": "f-2", "room": "room-a", "seq": 2, "lamport": 2, "ts": 2.0,
     "from": "p-a", "role": "peer", "kind": "say", "to": {"room": True},
     "body": {"text": "grün, mit Ümlaut und einem Emoji-freien Satz"}},
    {"v": 1, "id": "f-3", "room": "room-a", "seq": 3, "lamport": 3, "ts": 3.0,
     "from": "p-a", "role": "peer", "kind": "answer", "reply_to": "f-1",
     "to": {"peer": "p-b"}, "body": {"choice": "ja, weiter so", "text": "votes: ja"}},
    {"v": 1, "id": "f-4", "room": "room-a", "seq": 4, "lamport": 4, "ts": 4.0,
     "from": "p-a", "role": "peer", "kind": "vote", "to": {"room": True},
     "body": {"text": "weiter?", "options": ["ja", "nein"], "closes_at": 1799999999}},
    {"v": 1, "id": "f-5", "room": "room-a", "seq": 5, "lamport": 5, "ts": 5.0,
     "from": "p-a", "role": "peer", "kind": "report", "to": {"role": "leader"},
     "body": {"text": "läuft", "status": "working",
              "progress": {"done": 3, "total": 5, "step": "writing the tests"}},
     "must_understand": [], "ext": {"vendor": {"nested": [1, 2, {"deep": True}]}}},
]


def _vaf_bytes(frame):
    content = {field: frame.get(field) for field in signing.COVERED}
    return signing.canonical_bytes(
        signing.covered_payload(frame["room"], frame.get("from") or "", content))


@pytest.mark.parametrize("frame", FRAMES, ids=lambda f: f["id"])
def test_all_three_implementations_sign_the_same_bytes(peers, frame):
    reference, guest = peers
    mine = _vaf_bytes(frame)
    assert reference.signing_bytes(frame) == mine
    assert guest.signing_bytes(frame) == mine


def test_the_bytes_are_what_the_document_describes(peers):
    """Sorted keys, no whitespace, UTF-8 rather than escapes, behind a domain prefix.
    Spelled out here so a change to any of the four is a decision."""
    # The umlaut frame answers the encoding question and nothing else: its own text
    # contains ", ", so it cannot also answer the whitespace one.
    assert "grün".encode("utf-8") in _vaf_bytes(FRAMES[1]), \
        "escaped non-ASCII would be a second valid encoding of one message"

    raw = _vaf_bytes(FRAMES[0])
    assert raw.startswith(b"vaf-a2a-sig/v2\n")
    assert b", " not in raw and b'": ' not in raw, "a separator carrying whitespace"
    payload = json.loads(raw[len(b"vaf-a2a-sig/v2\n"):].decode("utf-8"))
    assert list(payload) == sorted(payload)
    assert set(payload) == {"v", "room", "from", *signing.COVERED}


def test_a_kind_that_is_not_a_string_is_read_the_same_way_by_all_three(peers):
    """The last field that was taken raw. `ext` taken raw meant one side wrote
    `null` where another wrote `{}`, and nothing verified across the two; `kind` was
    the only one left, so it is coerced everywhere rather than left as the next
    instance of the same class. Found by a foreign implementation reading the
    normalisation and asking which field was missing from it."""
    reference, guest = peers
    odd = dict(FRAMES[0], kind=7)
    assert reference.signing_bytes(odd) == _vaf_bytes(odd) == guest.signing_bytes(odd)

    absent = {k: v for k, v in FRAMES[0].items() if k != "kind"}
    assert reference.signing_bytes(absent) == _vaf_bytes(absent) == guest.signing_bytes(absent)


@pytest.mark.parametrize("given", [None, {}, {"room": True}])
def test_an_absent_addressee_means_the_room_everywhere(peers, given):
    """The mirror of the `ext` divergence, and the reason both directions matter.

    A sender that omits `to` signs it as empty while the room stores it as "to the
    room". Every implementation therefore applies the default inside the covered
    form, so a payload as SENT and the same payload as STORED produce one set of
    bytes. Found the first time a foreign peer signed and VAF checked; checking
    VAF's signatures with foreign code could not have shown it, because VAF only
    ever signed content that had already been through compose.
    """
    reference, guest = peers
    frame = {k: v for k, v in FRAMES[0].items() if k != "to"}
    if given is not None:
        frame["to"] = given
    expected = _vaf_bytes(dict(FRAMES[0], to={"room": True}))
    assert _vaf_bytes(frame) == expected
    assert reference.signing_bytes(frame) == expected
    assert guest.signing_bytes(frame) == expected


def test_placement_is_not_covered_by_any_of_them(peers):
    """A sender cannot know its sequence number, so it must not be asked to sign one.
    Changing placement must leave every implementation's bytes untouched."""
    reference, guest = peers
    moved = dict(FRAMES[0], id="other", ts=999.0, seq=77, lamport=99, role="leader")
    for produce in (_vaf_bytes, reference.signing_bytes, guest.signing_bytes):
        assert produce(moved) == produce(FRAMES[0])


def test_the_handle_is_covered_by_all_of_them(peers):
    """`from` sits with the placement fields and does not belong there.

    A peer learns its handle when it is admitted and keeps it for the whole room, so
    unlike a sequence number it is something the sender CAN sign - and leaving it out
    meant a host could carry a frame, signature intact, onto somebody else's name.
    """
    reference, guest = peers
    renamed = dict(FRAMES[0], **{"from": "p-somebody-else"})
    for produce in (_vaf_bytes, reference.signing_bytes, guest.signing_bytes):
        assert produce(renamed) != produce(FRAMES[0])


def test_the_room_is_covered_by_all_of_them(peers):
    """Otherwise a signed frame could be lifted into another room and still verify."""
    reference, guest = peers
    elsewhere = dict(FRAMES[0], room="room-b")
    for produce in (_vaf_bytes, reference.signing_bytes, guest.signing_bytes):
        assert produce(elsewhere) != produce(FRAMES[0])


def test_the_guest_verifies_a_real_signature_in_pure_python(peers):
    """The point of the whole slice: a machine with nothing installed can check a
    signature rather than take the host's word for it."""
    _reference, guest = peers
    key = "cli:scope-a"
    signed = dict(FRAMES[0])
    signed["sig"] = signing.sign(
        signing.covered_payload(signed["room"], signed["from"],
                                {f: signed.get(f) for f in signing.COVERED}),
        participant_key=key, room_id=signed["room"])

    published = signing.public_key(key, signed["room"])
    joined = {"v": 1, "id": "j", "room": signed["room"], "seq": 1, "lamport": 1,
              "from": "p-a", "role": "peer", "kind": "join", "to": {"room": True},
              "body": {"display": "A", "card": {}, "sign_key": published}}
    # SELF-SIGNED, by the very key it publishes. VAF made this signature and the
    # guest is about to accept it, which is the interop half of the same rule.
    joined["sig"] = signing.sign(
        signing.covered_payload(joined["room"], joined["from"],
                                {f: joined.get(f) for f in signing.COVERED}),
        participant_key=key, room_id=joined["room"])
    keys = guest.signing_keys([joined, signed], signed["room"])

    assert keys == {"p-a": published}
    assert guest.verdict_for(signed, keys, signed["room"]) == "valid"


def test_the_guest_notices_a_changed_message(peers):
    _reference, guest = peers
    key = "cli:scope-a"
    signed = dict(FRAMES[0])
    signed["sig"] = signing.sign(
        signing.covered_payload(signed["room"], signed["from"],
                                {f: signed.get(f) for f in signing.COVERED}),
        participant_key=key, room_id=signed["room"])
    published = signing.public_key(key, signed["room"])

    tampered = dict(signed, body={"text": "untergeschoben"})
    assert guest.verdict_for(tampered, {"p-a": published}, tampered["room"]) == "invalid"


def test_a_real_signature_by_an_unpublished_key_is_not_valid_for_the_guest(peers):
    _reference, guest = peers
    signed = dict(FRAMES[0])
    signed["sig"] = signing.sign(
        signing.covered_payload(signed["room"], signed["from"],
                                {f: signed.get(f) for f in signing.COVERED}),
        participant_key="cli:stranger", room_id=signed["room"])
    published = signing.public_key("cli:scope-a", signed["room"])

    assert guest.verdict_for(signed, {"p-a": published}, signed["room"]) == "foreign_key"


@pytest.mark.parametrize("sig", [None, {}, "x", {"alg": "rsa", "key": "a" * 64, "sig": "b" * 128},
                                 {"alg": "ed25519", "key": "zz", "sig": "b" * 128}])
def test_neither_peer_treats_an_unreadable_claim_as_a_forgery(peers, sig):
    """"Nothing to check" and "this did not verify" are different answers, and only
    one of them accuses somebody."""
    reference, guest = peers
    frame = dict(FRAMES[0], sig=sig) if sig is not None else dict(FRAMES[0])
    assert guest.verdict_for(frame, {}, frame["room"]) in ("unsigned", "unreadable")
    assert reference.verdict(frame, {}, None, frame["room"]) in ("unsigned", "unreadable")


def test_the_reference_peer_says_unchecked_rather_than_guessing(peers):
    """Ed25519 is not in the standard library and that file is about the protocol,
    not curve arithmetic. Without a verifier it reports that it could not check,
    which a reader must not confuse with a frame nobody signed."""
    reference, _guest = peers
    signed = dict(FRAMES[0], sig={"alg": "ed25519", "v": signing.VERSION,
                                  "key": "a" * 64, "sig": "b" * 128})
    assert reference.verdict(signed, {}, None, signed["room"]) == "unchecked"


def test_the_guest_client_names_its_own_generation():
    """A single-file client is downloaded to be used as a library, so a peer holding
    an older copy has to be able to tell. `RoomConnection.frames()` became
    `backlog()` and `next_frame()`, and the way that was found out was an
    AttributeError on somebody else's machine."""
    guest = (ROOT / "examples" / "12_a2a_wire_peer.py").read_text(encoding="utf-8")
    assert "CLIENT_VERSION" in guest
    assert "\nVERSION = 1\n" in guest, "the protocol version is a different number"


# ── the direction that was missing ──────────────────────────────────────────

@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    import vaf.core.a2a.store as store_mod
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    return tmp_path


def test_vaf_accepts_and_verifies_a_signature_the_guest_made(peers, rooms):
    """The half that could not be tested until the guest could sign.

    Everything before this checked VAF's signatures with foreign code. That found
    one real divergence and would have missed its mirror image: a field the GUEST
    normalises differently on the way OUT looks fine to every verifier here,
    because VAF would then be checking its own idea of the bytes against a
    signature made under somebody else's. Signing and checking have to be pinned in
    both directions or half the interop surface is untested.
    """
    from vaf.core.a2a.room import Room, RoomError

    _reference, guest = peers
    room = Room.create(kind="round", owner_scope="s", base=rooms, room_id="room-rev")
    stranger = room.join(display="Gast", scope_id=None, peer_id="p-stranger")

    record = {"room": room.room_id}
    seed = guest.seat_signing_seed(record)
    assert len(record["sign_seed"]) == 64, "the seat keeps its own seed"

    # The guest publishes its key the way the client does: a join of its own.
    published = guest.ed25519_public(seed).hex()
    room.ingest(guest.sign_payload({"kind": "join", "body": {"display": "Gast",
                                                            "card": {},
                                                            "sign_key": published}},
                                   room.room_id, seed, stranger.peer_id),
                identity=stranger)
    assert room.signing_keys()["p-stranger"] == published

    payload = guest.sign_payload({"kind": "say", "body": {"text": "vom Gast signiert"}},
                                 room.room_id, seed, stranger.peer_id)
    frame = room.ingest(payload, identity=stranger)

    assert frame.sig["key"] == published
    assert room.verdict_for(frame) == "valid"

    # And the room refuses a guest signature over something else, the same way it
    # refuses one of its own.
    other = guest.sign_payload({"kind": "say", "body": {"text": "etwas anderes"}},
                               room.room_id, seed, stranger.peer_id)
    with pytest.raises(RoomError):
        room.ingest({"kind": "say", "body": {"text": "das hier"}, "sig": other["sig"]},
                    identity=stranger)


def test_a_guest_signature_survives_the_round_trip_to_disk(peers, rooms):
    """A guest's frame is read back by VAF's parser, not by the guest's. That is the
    seam the `ext` divergence lived in, from the other side."""
    from vaf.core.a2a.room import Room

    _reference, guest = peers
    room = Room.create(kind="round", owner_scope="s", base=rooms, room_id="room-rev2")
    stranger = room.join(display="Gast", scope_id=None, peer_id="p-stranger")
    record = {"room": room.room_id}
    seed = guest.seat_signing_seed(record)
    room.ingest(guest.sign_payload(
        {"kind": "join", "body": {"display": "Gast", "card": {},
                                  "sign_key": guest.ed25519_public(seed).hex()}},
        room.room_id, seed, stranger.peer_id), identity=stranger)

    for text in ("kurz", "mit Ümlaut und grün", "a" * 500):
        room.ingest(guest.sign_payload({"kind": "say", "body": {"text": text}},
                                       room.room_id, seed, stranger.peer_id),
                    identity=stranger)

    verdicts = [v for f, v in room.verify_frames() if f.kind == "say"]
    assert verdicts == ["valid", "valid", "valid"]

    # And the guest reaches the same conclusion about its own frames, from the JSON.
    stored = [f.to_dict() for f in room.store.frames()]
    keys = guest.signing_keys(stored, room.room_id)
    assert [guest.verdict_for(f, keys, room.room_id) for f in stored
            if f["kind"] == "say"] == \
        ["valid", "valid", "valid"]


# ── the same fold rule in all three, which is the only way it means anything ──

def _announcement(room_id, key, sender="p-a"):
    """A join that publishes a key and attests it, as any of the three would read it."""
    body = {"display": "A", "card": {}, "sign_key": signing.public_key(key, room_id)}
    frame = {"v": 1, "id": "j-1", "room": room_id, "seq": 1, "lamport": 1, "ts": 1.0,
             "from": sender, "role": "peer", "kind": "join", "to": {"room": True},
             "body": body}
    frame["sig"] = signing.sign(
        signing.covered_payload(room_id, frame["from"],
                                {f: frame.get(f) for f in signing.COVERED}),
        participant_key=key, room_id=room_id)
    return frame


def test_all_three_folds_refuse_a_key_its_join_does_not_attest(peers, rooms):
    """The lift, asked of every implementation the protocol advertises.

    A fold that is strict in one reader and lax in another is worse than a lax one
    everywhere: the same transcript would then be authoritative or forged depending on
    who opened it, and neither log would say why.
    """
    from vaf.core.a2a.room import Room, derive_peer_id, participant_key

    reference, guest = peers
    key = participant_key("cli", "scope-a")
    room = Room.create(kind="round", owner_scope="scope-a", base=rooms, room_id="room-3f")
    joined = _announcement("room-3f", key)
    unattested = {**joined, "id": "j-2", "from": "p-b", "sig": None}

    def verify(key_hex, blob_hex, message):
        """The curve arithmetic the reference peer deliberately does not carry.

        Borrowed from the guest, which does it in the standard library - so the
        reference peer is checked against an implementation that is not VAF's.
        """
        return guest.ed25519_verify(bytes.fromhex(key_hex), bytes.fromhex(blob_hex),
                                    message)

    room.join(display="A", scope_id="scope-a",
              peer_id=derive_peer_id(key, "room-3f"), participant_key=key)
    stored = [f.to_dict() for f in room.store.frames()]

    # The guest does the curve arithmetic itself, so it can answer outright.
    assert guest.signing_keys([joined], "room-3f")["p-a"] == joined["body"]["sign_key"]
    assert guest.signing_keys([unattested], "room-3f") == {}
    assert guest.signing_keys(stored, "room-3f"), "and it agrees about a key VAF wrote"

    # The reference peer takes verification as an injected primitive and refuses to
    # guess without one - which is exactly what it must do about attestation too.
    assert reference.signing_keys([joined], "room-3f") == {}, "no verifier, nothing attested"
    assert reference.verdict(joined, {}, None, "room-3f") == "unchecked", "and so it accuses nobody"
    assert reference.signing_keys([joined], "room-3f", verify)["p-a"] == \
        joined["body"]["sign_key"]
    assert reference.signing_keys([unattested], "room-3f", verify) == {}

    # A join that IS signed, by the key it names, whose body was changed afterwards.
    # The sharper case: everything about the announcement looks right until the sum is
    # actually done, so a fold that stops at "there is a signature and it names this
    # key" would bind a handle to a key on the strength of a broken proof.
    tampered = {**joined, "body": {**joined["body"], "display": "jemand anders"}}
    assert guest.signing_keys([tampered], "room-3f") == {}
    assert reference.signing_keys([tampered], "room-3f", verify) == {}


def test_the_guest_clients_own_join_publishes_a_key_the_room_will_bind(peers, rooms):
    """The one frame whose correctness cannot be checked by reading a transcript.

    Every other frame the client sends is judged after the fact by its verdict. Get
    the ANNOUNCEMENT wrong and there is no verdict to read: every later frame simply
    says `foreign_key` and nothing says why. It went untested because the interop
    tests all build a join by hand, so the client's own could have shipped unsigned.
    """
    from vaf.core.a2a.room import Room

    _reference, guest = peers
    room = Room.create(kind="round", owner_scope="s", base=rooms, room_id="room-cli")
    stranger = room.join(display="Gast", scope_id=None, peer_id="p-cli")

    record = {"room": room.room_id}
    seed = guest.seat_signing_seed(record)
    announcement = guest.join_announcement("Gast", room.room_id, seed, stranger.peer_id)
    assert announcement["sig"]["key"] == announcement["body"]["sign_key"], \
        "it must be signed by the very key it publishes"

    room.ingest(announcement, identity=stranger)
    assert room.signing_keys()["p-cli"] == guest.ed25519_public(seed).hex()

    spoken = room.ingest(guest.sign_payload({"kind": "say", "body": {"text": "hallo"}},
                                            room.room_id, seed, stranger.peer_id),
                         identity=stranger)
    assert room.verdict_for(spoken) == "valid"


def test_an_upgraded_guest_recovers_its_own_past_without_losing_its_handle(peers, rooms):
    """The migration, and the reason `announce` exists as a verb at all.

    A guest that joined with a client too old to sign its announcement holds a key the
    room will not bind: everything it ever said reads `foreign_key`. Redeeming a fresh
    invitation would fix the signing and lose the peer - a new invitation mints a new
    handle, so the whole history stays behind under the old one.

    Sending the announcement over the SEAT keeps the handle, and because the seed lives
    in the seat and does not change, it is the same key. So a past that could not be
    verified becomes verifiable, with not one stored byte rewritten.

    The recovery is real but bounded, and the bound is worth knowing before anybody
    promises it to a user: it works for frames whose SIGNATURE this reader can still
    check, which means frames of the current signature version. A frame signed under an
    older version is not revived by any announcement, because the proof it carries was
    made over something the current rule no longer asks about. Those read `unreadable`
    forever, which is the honest word for them.
    """
    from vaf.core.a2a.room import Room

    _reference, guest = peers
    room = Room.create(kind="round", owner_scope="s", base=rooms, room_id="room-mig")
    stranger = room.join(display="Gast", scope_id=None, peer_id="p-old")
    record = {"room": room.room_id}
    seed = guest.seat_signing_seed(record)

    # THE OLD CLIENT: it announces its key without signing the announcement.
    room.ingest({"kind": "join", "body": {"display": "Gast", "card": {},
                                          "sign_key": guest.ed25519_public(seed).hex()}},
                identity=stranger)
    spoken = room.ingest(guest.sign_payload({"kind": "say",
                                             "body": {"text": "vor dem Update"}},
                                            room.room_id, seed, stranger.peer_id),
                         identity=stranger)
    assert room.verdict_for(spoken) == "foreign_key", "announced, but nothing attested it"

    # THE UPGRADE: exactly what `announce` sends, over the seat it already holds.
    room.ingest(guest.join_announcement("Gast", room.room_id, seed, stranger.peer_id),
                identity=stranger)

    assert room.signing_keys()["p-old"] == guest.ed25519_public(seed).hex()
    assert room.verdict_for(spoken) == "valid", "the past binds, unrewritten"
    assert [f.sender for f in room.store.frames()].count("p-old") == 4, \
        "and it is still the same peer"


def test_the_announce_verb_sends_the_seats_own_key_and_not_a_fresh_one(peers, monkeypatch):
    """Driving the VERB, not the function under it.

    The function was covered and the command around it was not, which is how the same
    gap shipped twice in this file's history: a helper proven correct, reached by a
    caller nobody ran. If `announce` minted a key instead of reading the seat's, every
    assertion about recovery above would still pass and the verb would be useless -
    the peer would bind a key none of its past was signed with.
    """
    _reference, guest = peers
    record = {"room": "room-verb", "url": "wss://x", "seat": "s-1", "ca_pem": "",
              "display": "Gast"}
    expected = guest.ed25519_public(guest.seat_signing_seed(record)).hex()
    sent = []

    class Line:
        room, peer = "room-verb", "p-verb"

        def submit(self, payload):
            sent.append(payload)

        def close(self):
            pass

    monkeypatch.setattr(guest, "load_record", lambda room: dict(record))
    monkeypatch.setattr(guest, "save_record", lambda rec: Path("seat"))
    monkeypatch.setattr(guest, "_open", lambda rec: Line())

    guest.cmd_announce(argparse.Namespace(room="room-verb"))

    assert len(sent) == 1 and sent[0]["kind"] == "join"
    assert sent[0]["body"]["sign_key"] == expected, "the seat's key, not a new one"
    assert sent[0]["sig"]["key"] == expected, "and the announcement attests it"


def test_all_three_read_a_version_one_signature_as_unreadable_rather_than_forged(peers):
    """What every frame signed before this change now looks like, in all three.

    Version 2 added the sender's handle to what a signature covers, so a version 1
    signature is a real signature over bytes that no longer mean the same thing. Every
    reader has to land on `unreadable` and not `invalid`: the difference is whether an
    honest peer's whole history is quietly accused the day the coverage changes.
    """
    reference, guest = peers
    private, public = _pair()
    old = dict(FRAMES[0])
    old["sig"] = {"alg": "ed25519", "key": public,
                  "sig": private.sign(guest.signing_bytes(old, old["room"])).hex()}

    def verify(key_hex, blob_hex, message):
        return guest.ed25519_verify(bytes.fromhex(key_hex), bytes.fromhex(blob_hex),
                                    message)

    assert guest.verdict_for(old, {"p-a": public}, old["room"]) == "unreadable"
    assert reference.verdict(old, {"p-a": public}, verify, old["room"]) == "unreadable"

    assert content_signature(Frame.from_dict(old), old["room"])[0] == "unreadable"


# ── who an agent belongs to: the same fold in all three ───────────────────

def _household_frames(rooms, room_id):
    """The host's own household as VAF writes it: the person's attested join and the
    agent's, carrying the person's attestation. Returned as stored dicts, which is
    what a reader on the wire holds."""
    from vaf.core.a2a.room import Room, derive_peer_id, participant_key

    room = Room.create(kind="round", owner_scope="scope-a", base=rooms, room_id=room_id)
    person_key, agent_key = participant_key("cli", "scope-a"), participant_key("agent", "scope-a")
    person = room.join(display="Alice", scope_id="scope-a",
                       peer_id=derive_peer_id(person_key, room_id), participant_key=person_key)
    agent = room.join(display="Nobel", scope_id="scope-a",
                      peer_id=derive_peer_id(agent_key, room_id), participant_key=agent_key)
    return room, person, agent, [f.to_dict() for f in room.store.frames()]


def test_all_three_fold_the_same_household(peers, rooms):
    """A pair that one reader sees and another does not is worse than no pair: the
    same transcript would say "Alice's agent" here and "unknown" there."""
    from vaf.core.a2a.room import fold_owners

    reference, guest = peers
    room, person, agent, stored = _household_frames(rooms, "room-3h")
    expected = {agent.peer_id: person.peer_id}

    def verify(key_hex, blob_hex, message):
        return guest.ed25519_verify(bytes.fromhex(key_hex), bytes.fromhex(blob_hex), message)

    assert fold_owners(room.store.frames(), "room-3h") == expected
    assert guest.owners(stored, "room-3h") == expected
    assert reference.owners(stored, "room-3h", verify) == expected
    assert reference.owners(stored, "room-3h") == {}, "no verifier, nothing owned"

    # A changed block. The agent's join is still self-signed, so the tamper is in
    # the covered body and breaks the join's own signature: the key is gone and the
    # block with it, in every reader alike.
    tampered = [dict(f) for f in stored]
    for frame in tampered:
        if frame.get("from") == agent.peer_id and frame.get("kind") == "join":
            frame["body"] = {**frame["body"], "owner": {**frame["body"]["owner"], "peer": "p-x"}}
    assert fold_owners([Frame.from_dict(f) for f in tampered], "room-3h") == {}
    assert guest.owners(tampered, "room-3h") == {}
    assert reference.owners(tampered, "room-3h", verify) == {}


def test_a_guest_made_attestation_binds_in_vaf(peers, rooms):
    """The direction that matters: a household on ANOTHER machine, whose owner made
    the block with the guest client, is paired by the host and by every reader."""
    from vaf.core.a2a.room import Room, fold_owners

    reference, guest = peers
    room = Room.create(kind="round", owner_scope="s", base=rooms, room_id="room-gh")
    ana = room.join(display="Ana", scope_id=None, peer_id="p-ana")
    iris = room.join(display="Iris", scope_id=None, peer_id="p-iris")
    ana_seed = guest.seat_signing_seed({"room": "room-gh"})
    iris_seed = guest.seat_signing_seed({"room": "room-gh"})

    room.ingest(guest.join_announcement("Ana", "room-gh", ana_seed, ana.peer_id), identity=ana)
    block = guest.attest_owner("room-gh", ana_seed, ana.peer_id, iris.peer_id,
                               guest.ed25519_public(iris_seed).hex())
    room.ingest(guest.join_announcement("Iris", "room-gh", iris_seed, iris.peer_id, block),
                identity=iris)

    assert fold_owners(room.store.frames(), "room-gh") == {iris.peer_id: ana.peer_id}
    assert room.pairs()[iris.peer_id]["proof"] == "attested"
    stored = [f.to_dict() for f in room.store.frames()]
    assert guest.owners(stored, "room-gh") == {iris.peer_id: ana.peer_id}

    def verify(key_hex, blob_hex, message):
        return guest.ed25519_verify(bytes.fromhex(key_hex), bytes.fromhex(blob_hex), message)

    assert reference.owners(stored, "room-gh", verify) == {iris.peer_id: ana.peer_id}

    # The negative every reader must agree on: a block naming an owner who never
    # spoke with that key. It verifies with the key it names, and binds nothing.
    room2 = Room.create(kind="round", owner_scope="s", base=rooms, room_id="room-gh2")
    ana2 = room2.join(display="Ana", scope_id=None, peer_id="p-ana")
    iris2 = room2.join(display="Iris", scope_id=None, peer_id="p-iris")
    stranger_seed = guest.seat_signing_seed({"room": "room-gh2"})
    room2.ingest(guest.join_announcement("Ana", "room-gh2", ana_seed, ana2.peer_id),
                 identity=ana2)
    forged = guest.attest_owner("room-gh2", stranger_seed, ana2.peer_id, iris2.peer_id,
                                guest.ed25519_public(iris_seed).hex())
    room2.ingest(guest.join_announcement("Iris", "room-gh2", iris_seed, iris2.peer_id, forged),
                 identity=iris2)
    stored2 = [f.to_dict() for f in room2.store.frames()]
    assert fold_owners(room2.store.frames(), "room-gh2") == {}
    assert guest.owners(stored2, "room-gh2") == {}
    assert reference.owners(stored2, "room-gh2", verify) == {}


def test_the_attest_verb_prints_a_block_the_room_binds(peers, rooms, monkeypatch, capsys):
    """Driving the VERB: the owner runs it from their own seat and hands the printed
    line to their agent. If it minted a key instead of reading the seat's, the block
    would name a key the owner never spoke with and bind nothing."""
    from vaf.core.a2a.room import Room, fold_owners

    _reference, guest = peers
    room = Room.create(kind="round", owner_scope="s", base=rooms, room_id="room-verb2")
    ana = room.join(display="Ana", scope_id=None, peer_id="p-ana")
    iris = room.join(display="Iris", scope_id=None, peer_id="p-iris")
    record = {"room": "room-verb2", "peer": ana.peer_id, "display": "Ana"}
    ana_seed = guest.seat_signing_seed(record)
    iris_seed = guest.seat_signing_seed({"room": "room-verb2"})
    room.ingest(guest.join_announcement("Ana", "room-verb2", ana_seed, ana.peer_id), identity=ana)

    monkeypatch.setattr(guest, "load_record", lambda room_id: dict(record))
    monkeypatch.setattr(guest, "save_record", lambda rec: Path("seat"))
    guest.cmd_attest(argparse.Namespace(room="room-verb2", agent=iris.peer_id,
                                        agent_key=guest.ed25519_public(iris_seed).hex()))
    block = json.loads(capsys.readouterr().out.strip())
    assert block["peer"] == ana.peer_id and block["key"] == guest.ed25519_public(ana_seed).hex()

    room.ingest(guest.join_announcement("Iris", "room-verb2", iris_seed, iris.peer_id, block),
                identity=iris)
    assert fold_owners(room.store.frames(), "room-verb2") == {iris.peer_id: ana.peer_id}

    with pytest.raises(guest.Refused):
        guest.cmd_attest(argparse.Namespace(room="room-verb2", agent=iris.peer_id,
                                            agent_key="short"))


def test_the_members_verb_reads_the_household_back(peers, rooms, monkeypatch, capsys):
    """MUTATION: print `unknown` for everybody, or read the pair from a peer record.
    The guest client had no roster verb at all; this is where a guest agent learns
    whose user is speaking to it."""
    from vaf.core.a2a.room import Room

    _reference, guest = peers
    room = Room.create(kind="round", owner_scope="s", base=rooms, room_id="room-mem")
    ana = room.join(display="Ana", scope_id=None, peer_id="p-ana")
    iris = room.join(display="Iris", scope_id=None, peer_id="p-iris")
    room.join(display="Codex", scope_id=None, peer_id="p-codex")
    ana_seed, iris_seed = guest.seat_signing_seed({}), guest.seat_signing_seed({})
    room.ingest(guest.join_announcement("Ana", "room-mem", ana_seed, ana.peer_id), identity=ana)
    block = guest.attest_owner("room-mem", ana_seed, ana.peer_id, iris.peer_id,
                               guest.ed25519_public(iris_seed).hex())
    room.ingest(guest.join_announcement("Iris", "room-mem", iris_seed, iris.peer_id, block),
                identity=iris)
    stored = [f.to_dict() for f in room.store.frames()]

    class Line:
        def backlog(self):
            return list(stored)

        def close(self):
            pass

    monkeypatch.setattr(guest, "load_record", lambda room_id: {"room": "room-mem", "peer": "p-codex"})
    monkeypatch.setattr(guest, "_line_for", lambda room_id, record: None)
    monkeypatch.setattr(guest, "_open", lambda rec: Line())
    guest.cmd_members(argparse.Namespace(room="room-mem"))
    rows = {json.loads(line)["display"]: json.loads(line)
            for line in capsys.readouterr().out.strip().splitlines() if line.strip()}
    assert rows["Iris"]["kind"] == "agent" and rows["Iris"]["partner"] == ana.peer_id
    assert rows["Iris"]["proof"] == "attested"
    assert rows["Ana"]["kind"] == "human" and rows["Ana"]["partner_display"] == "Iris"
    assert rows["Codex"]["kind"] == "unknown" and rows["Codex"]["proof"] == ""


def test_the_guest_roster_names_the_same_first_agent_as_the_host(peers, rooms, monkeypatch, capsys):
    """MUTATION: invert the fold with a comprehension in the guest, so the LAST wins.
    Three rosters read one transcript; a person with two attested agents must be
    shown with the same partner on all of them."""
    from vaf.core.a2a.room import Room

    _reference, guest = peers
    room = Room.create(kind="round", owner_scope="s", base=rooms, room_id="room-mem2")
    ana = room.join(display="Ana", scope_id=None, peer_id="p-ana")
    ana_seed = guest.seat_signing_seed({})
    room.ingest(guest.join_announcement("Ana", "room-mem2", ana_seed, ana.peer_id), identity=ana)
    for display, peer_id in (("Iris", "p-iris"), ("Second", "p-second")):
        agent = room.join(display=display, scope_id=None, peer_id=peer_id)
        seed = guest.seat_signing_seed({})
        block = guest.attest_owner("room-mem2", ana_seed, ana.peer_id, agent.peer_id,
                                   guest.ed25519_public(seed).hex())
        room.ingest(guest.join_announcement(display, "room-mem2", seed, agent.peer_id, block),
                    identity=agent)
    assert room.pairs()[ana.peer_id]["partner"] == "p-iris"
    stored = [f.to_dict() for f in room.store.frames()]

    class Line:
        def backlog(self):
            return list(stored)

        def close(self):
            pass

    monkeypatch.setattr(guest, "load_record", lambda room_id: {"room": "room-mem2", "peer": "p-x"})
    monkeypatch.setattr(guest, "_line_for", lambda room_id, record: None)
    monkeypatch.setattr(guest, "_open", lambda rec: Line())
    guest.cmd_members(argparse.Namespace(room="room-mem2"))
    rows = {json.loads(line)["display"]: json.loads(line)
            for line in capsys.readouterr().out.strip().splitlines() if line.strip()}
    assert rows["Ana"]["partner"] == "p-iris", "the guest roster names a different partner"
    assert rows["Second"]["partner"] == ana.peer_id
