# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Signing a frame's content: the bytes, the keys, and what a verifier may conclude.

Two implementations that never met have to produce the same bytes for the same
content, or a signature proves nothing. Most of this file defends that, because it
is the half that fails quietly: a signature that verifies on the machine that made
it and nowhere else looks exactly like one that works.

The other half defends the two separations the design rests on. A signature says
"the holder of this key wrote this content in this room" and deliberately nothing
about placement, so the covered payload carries no `seq`, `from` or `ts`. And
whether the key BELONGS to the peer a frame is filed under is a different question
with a different answer, folded from the room's join frames.
"""
import hashlib
import inspect
import json

import pytest

from vaf.core.a2a import signing


PAYLOAD = {
    "v": 1,
    "room": "room-abc",
    "kind": "say",
    "to": {"room": True},
    "body": {"text": "guten Morgen"},
    "reply_to": None,
    "must_understand": [],
    "ext": {},
}

# A seed nobody derives: this file pins the ARITHMETIC, not a real key.
SEED = bytes(range(32))


def _key_from_seed(seed=SEED):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.from_private_bytes(seed)
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private, public.hex()


# ── the bytes ───────────────────────────────────────────────────────────────

def test_the_canonical_bytes_are_pinned():
    """A test vector, so a change to the serialisation is a decision rather than an
    accident. A stranger implementing this from the document reproduces this hash or
    their signatures will not verify against ours."""
    canonical = signing.canonical_bytes(PAYLOAD)
    assert canonical.startswith(b"vaf-a2a-sig/v1\n")
    assert hashlib.sha256(canonical).hexdigest() == (
        hashlib.sha256(
            b"vaf-a2a-sig/v1\n"
            + json.dumps(PAYLOAD, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    assert b'"body":{"text":"guten Morgen"}' in canonical
    assert b", " not in canonical, "whitespace would differ between implementations"


def test_key_order_does_not_change_the_bytes():
    """The property the whole scheme rests on: two peers that built the same content
    in a different order sign the same thing."""
    shuffled = {k: PAYLOAD[k] for k in reversed(list(PAYLOAD))}
    assert signing.canonical_bytes(shuffled) == signing.canonical_bytes(PAYLOAD)


def test_non_ascii_is_carried_as_utf8_not_as_escapes():
    """`ensure_ascii=True` would write \\u00fc, which is a second valid encoding of
    the same string and therefore a second set of signed bytes for one message."""
    canonical = signing.canonical_bytes({**PAYLOAD, "body": {"text": "grün"}})
    assert "grün".encode("utf-8") in canonical


def test_the_form_is_the_one_the_tree_already_uses():
    """Reused rather than reinvented. If the hash-chain helper ever changes its
    serialisation, this fails and somebody decides whether both move together."""
    from vaf.core import log_helper

    house = inspect.getsource(log_helper)
    assert 'sort_keys=True, ensure_ascii=False, separators=(",", ":")' in house
    ours = inspect.getsource(signing.canonical_bytes)
    assert 'sort_keys=True, ensure_ascii=False, separators=(",", ":")' in ours


@pytest.mark.parametrize("body,why", [
    ({"closes_at": 1.5}, "a fractional number"),
    ({"n": float("nan")}, "NaN"),
    ({"n": float("inf")}, "infinity"),
    ({"nested": {"deep": [1, 2.5]}}, "a float inside a list inside an object"),
])
def test_a_payload_that_cannot_be_written_down_twice_is_refused(body, why):
    with pytest.raises(signing.NotCanonical):
        signing.canonical_bytes({**PAYLOAD, "body": body})


def test_a_key_that_is_not_a_string_is_refused():
    """json.dumps turns {1: 'a'} into {'1': 'a'}, so two different objects would
    produce one set of signed bytes."""
    with pytest.raises(signing.NotCanonical):
        signing.canonical_bytes({**PAYLOAD, "body": {1: "a"}})


def test_whole_numbers_and_booleans_are_fine():
    """Everything the protocol actually carries has to pass, or the rule is not a
    rule but an obstacle."""
    canonical = signing.canonical_bytes({**PAYLOAD, "body": {
        "closes_at": 1799999999, "done": 3, "ok": True, "nothing": None,
        "options": ["ja", "nein"],
    }})
    assert b'"closes_at":1799999999' in canonical


# ── what is covered ─────────────────────────────────────────────────────────

def test_the_covered_payload_carries_content_and_the_room_and_nothing_else():
    """Placement is the room's: a sender cannot know `seq`, `lamport` or `id`, and
    signing what somebody else fills in is the mistake this split exists to avoid."""
    content = {"kind": "say", "to": {"room": True}, "body": {"text": "hi"},
               "reply_to": None, "must_understand": (), "ext": {}}
    payload = signing.covered_payload("room-abc", content)
    assert set(payload) == {"v", "room", *signing.COVERED}
    for absent in ("id", "ts", "seq", "lamport", "from", "role"):
        assert absent not in payload


def test_must_understand_is_carried_as_a_list():
    """compose returns a tuple and JSON has none; a verifier reads a list back, so
    the two sides would otherwise disagree about a value neither of them changed."""
    payload = signing.covered_payload("r", {"must_understand": ("deadline",)})
    assert payload["must_understand"] == ["deadline"]
    signing.canonical_bytes(payload)


# ── signing and verifying ───────────────────────────────────────────────────

def test_a_signature_verifies_over_the_payload_it_was_made_for():
    private, public = _key_from_seed()
    sig = {"alg": "ed25519", "key": public,
           "sig": private.sign(signing.canonical_bytes(PAYLOAD)).hex()}
    assert signing.verify(PAYLOAD, sig) is True


@pytest.mark.parametrize("change", [
    {"body": {"text": "guten Abend"}},
    {"room": "room-xyz"},
    {"kind": "directive"},
    {"to": {"peer": "p-bob"}},
    {"reply_to": "f-1"},
    {"ext": {"vendor": 1}},
])
def test_changing_any_covered_field_breaks_the_signature(change):
    private, public = _key_from_seed()
    sig = {"alg": "ed25519", "key": public,
           "sig": private.sign(signing.canonical_bytes(PAYLOAD)).hex()}
    assert signing.verify({**PAYLOAD, **change}, sig) is False


def test_another_key_does_not_verify():
    private, _ = _key_from_seed()
    _, other_public = _key_from_seed(bytes(range(1, 33)))
    sig = {"alg": "ed25519", "key": other_public,
           "sig": private.sign(signing.canonical_bytes(PAYLOAD)).hex()}
    assert signing.verify(PAYLOAD, sig) is False


@pytest.mark.parametrize("sig", [
    None, "", 5, [], {},
    {"alg": "rsa", "key": "a" * 64, "sig": "b" * 128},
    {"alg": "ed25519", "key": "a" * 63, "sig": "b" * 128},
    {"alg": "ed25519", "key": "z" * 64, "sig": "b" * 128},
    {"alg": "ed25519", "key": "a" * 64},
])
def test_a_signature_this_peer_cannot_read_is_not_a_forgery(sig):
    """None means "nothing here to check", which a reader has to render differently
    from "this did not verify". Both refuse, and only one accuses somebody."""
    assert signing.read_signature(sig) is None
    assert signing.verify(PAYLOAD, sig) is False


def test_verifying_an_uncanonical_payload_refuses_rather_than_raising():
    """A verifier walks a whole transcript. One frame it cannot serialise must cost
    that frame its verdict, never the walk."""
    private, public = _key_from_seed()
    sig = {"alg": "ed25519", "key": public,
           "sig": private.sign(signing.canonical_bytes(PAYLOAD)).hex()}
    assert signing.verify({**PAYLOAD, "body": {"n": 1.5}}, sig) is False


# ── keys ────────────────────────────────────────────────────────────────────

def test_a_participant_gets_a_different_key_in_every_room():
    """The room handle is derived so two transcripts cannot be correlated. A key
    that stayed the same across rooms would hand that back."""
    here = signing.public_key("cli:scope-a", "room-one")
    there = signing.public_key("cli:scope-a", "room-two")
    assert here != there
    assert len(here) == 64


def test_the_same_participant_in_the_same_room_gets_the_same_key_every_time():
    """Derived, never stored: a restart must land on the key the transcript already
    names, or every earlier frame stops verifying."""
    assert signing.public_key("cli:scope-a", "room-one") == \
           signing.public_key("cli:scope-a", "room-one")


def test_two_participants_in_one_room_get_different_keys():
    assert signing.public_key("cli:scope-a", "room-one") != \
           signing.public_key("agent:scope-a", "room-one")


def test_signing_and_verifying_with_a_derived_key():
    sig = signing.sign(PAYLOAD, participant_key="cli:scope-a", room_id="room-abc")
    assert sig["key"] == signing.public_key("cli:scope-a", "room-abc")
    assert signing.verify(PAYLOAD, sig) is True
    assert signing.verify({**PAYLOAD, "body": {"text": "other"}}, sig) is False
