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
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from vaf.core.a2a import signing

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
    return signing.canonical_bytes(signing.covered_payload(frame["room"], content))


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
    assert raw.startswith(b"vaf-a2a-sig/v1\n")
    assert b", " not in raw and b'": ' not in raw, "a separator carrying whitespace"
    payload = json.loads(raw[len(b"vaf-a2a-sig/v1\n"):].decode("utf-8"))
    assert list(payload) == sorted(payload)
    assert set(payload) == {"v", "room", *signing.COVERED}


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
    moved = dict(FRAMES[0], id="other", ts=999.0, seq=77, lamport=99,
                 **{"from": "p-somebody-else", "role": "leader"})
    for produce in (_vaf_bytes, reference.signing_bytes, guest.signing_bytes):
        assert produce(moved) == produce(FRAMES[0])


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
        signing.covered_payload(signed["room"],
                                {f: signed.get(f) for f in signing.COVERED}),
        participant_key=key, room_id=signed["room"])

    published = signing.public_key(key, signed["room"])
    joined = {"v": 1, "id": "j", "room": signed["room"], "seq": 1, "lamport": 1,
              "from": "p-a", "role": "peer", "kind": "join", "to": {"room": True},
              "body": {"display": "A", "card": {}, "sign_key": published}}
    keys = guest.signing_keys([joined, signed])

    assert keys == {"p-a": published}
    assert guest.verdict_for(signed, keys) == "valid"


def test_the_guest_notices_a_changed_message(peers):
    _reference, guest = peers
    key = "cli:scope-a"
    signed = dict(FRAMES[0])
    signed["sig"] = signing.sign(
        signing.covered_payload(signed["room"],
                                {f: signed.get(f) for f in signing.COVERED}),
        participant_key=key, room_id=signed["room"])
    published = signing.public_key(key, signed["room"])

    tampered = dict(signed, body={"text": "untergeschoben"})
    assert guest.verdict_for(tampered, {"p-a": published}) == "invalid"


def test_a_real_signature_by_an_unpublished_key_is_not_valid_for_the_guest(peers):
    _reference, guest = peers
    signed = dict(FRAMES[0])
    signed["sig"] = signing.sign(
        signing.covered_payload(signed["room"],
                                {f: signed.get(f) for f in signing.COVERED}),
        participant_key="cli:stranger", room_id=signed["room"])
    published = signing.public_key("cli:scope-a", signed["room"])

    assert guest.verdict_for(signed, {"p-a": published}) == "foreign_key"


@pytest.mark.parametrize("sig", [None, {}, "x", {"alg": "rsa", "key": "a" * 64, "sig": "b" * 128},
                                 {"alg": "ed25519", "key": "zz", "sig": "b" * 128}])
def test_neither_peer_treats_an_unreadable_claim_as_a_forgery(peers, sig):
    """"Nothing to check" and "this did not verify" are different answers, and only
    one of them accuses somebody."""
    reference, guest = peers
    frame = dict(FRAMES[0], sig=sig) if sig is not None else dict(FRAMES[0])
    assert guest.verdict_for(frame, {}) in ("unsigned", "unreadable")
    assert reference.verdict(frame, {}, None) in ("unsigned", "unreadable")


def test_the_reference_peer_says_unchecked_rather_than_guessing(peers):
    """Ed25519 is not in the standard library and that file is about the protocol,
    not curve arithmetic. Without a verifier it reports that it could not check,
    which a reader must not confuse with a frame nobody signed."""
    reference, _guest = peers
    signed = dict(FRAMES[0], sig={"alg": "ed25519", "key": "a" * 64, "sig": "b" * 128})
    assert reference.verdict(signed, {}, None) == "unchecked"


def test_the_guest_client_names_its_own_generation():
    """A single-file client is downloaded to be used as a library, so a peer holding
    an older copy has to be able to tell. `RoomConnection.frames()` became
    `backlog()` and `next_frame()`, and the way that was found out was an
    AttributeError on somebody else's machine."""
    guest = (ROOT / "examples" / "12_a2a_wire_peer.py").read_text(encoding="utf-8")
    assert "CLIENT_VERSION" in guest
    assert "VERSION = 1" in guest, "the protocol version is a different number"


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
    room.ingest({"kind": "join", "body": {"display": "Gast", "card": {},
                                          "sign_key": published}}, identity=stranger)
    assert room.signing_keys()["p-stranger"] == published

    payload = guest.sign_payload({"kind": "say", "body": {"text": "vom Gast signiert"}},
                                 room.room_id, seed)
    frame = room.ingest(payload, identity=stranger)

    assert frame.sig["key"] == published
    assert room.verdict_for(frame) == "valid"

    # And the room refuses a guest signature over something else, the same way it
    # refuses one of its own.
    other = guest.sign_payload({"kind": "say", "body": {"text": "etwas anderes"}},
                               room.room_id, seed)
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
    room.ingest({"kind": "join", "body": {"display": "Gast", "card": {},
                                          "sign_key": guest.ed25519_public(seed).hex()}},
                identity=stranger)

    for text in ("kurz", "mit Ümlaut und grün", "a" * 500):
        room.ingest(guest.sign_payload({"kind": "say", "body": {"text": text}},
                                       room.room_id, seed), identity=stranger)

    verdicts = [v for f, v in room.verify_frames() if f.kind == "say"]
    assert verdicts == ["valid", "valid", "valid"]

    # And the guest reaches the same conclusion about its own frames, from the JSON.
    stored = [f.to_dict() for f in room.store.frames()]
    keys = guest.signing_keys(stored)
    assert [guest.verdict_for(f, keys) for f in stored if f["kind"] == "say"] == \
        ["valid", "valid", "valid"]
