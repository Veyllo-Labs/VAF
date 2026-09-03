# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Signing a frame's CONTENT, so authorship survives leaving the host machine.

A room assigns authorship: `Room.ingest` overwrites `from` and `role` with the
admitted peer's resolved values, which is sound while the host is the one who
admitted the connection and stops being sound the moment somebody reads the
transcript somewhere else. `vaf a2a export` is a claim, not evidence.

WHAT IS SIGNED, and why exactly that. The peer is the authority over CONTENT and
over WHO IT IS, the room over everything it assigns afterwards. So the covered
payload is the room's id, the sender's handle, and the six fields `Room.compose`
returns:

    {"v": 2, "room": ..., "from": ..., "kind": ..., "to": ..., "body": ...,
     "reply_to": ..., "must_understand": [...], "ext": {...}}

`room` is in there so a signed frame cannot be lifted into another room, and `from`
so it cannot be lifted onto another name. `from` looks at first like the other
assigned fields and is not: a peer learns its handle when it is admitted and keeps
it for the whole room, so it CAN sign it, while `id`, `ts`, `seq`, `lamport` and
`role` are decided per frame after the payload arrives and remain deliberately out -
signing what another party fills in is the mistake RFC 9421 warns about.

Leaving `from` out cost more than it looked. Measured: a host that writes the log
copies a peer's own fully attested `join` file into another lane, edits only the
uncovered fields, and the same key binds under two names; the peer's signed sentence,
moved the same way, reads `valid` under the other one. No key material is needed for
that, only the bytes the host already stores. The `join` being self-signed does not
help, because the signature proves possession of the key and said nothing about which
handle the announcement was filed under. Covering `from` is what actually closes it,
and it is why version 1 is not merely improved but replaced: a different coverage is
a different version, so the domain separator says `v2` and a v1 signature no longer
verifies anywhere.

There is no covered-field list on the wire. RFC 6376 and RFC 9421 both put the
coverage claim inside the signed bytes so it cannot be edited afterwards, and both
need it because coverage varies per message. Here it does not: version 2 covers
exactly the fields above, `v` is inside the signed bytes, and a different coverage
would be a different version. A constant list on every frame would be the ritual
without the reason.

WHY A SIGNATURE CAN BE STRICT HERE. `Room.compose` is idempotent, so a sender can
ask the room what it will store, be told, and sign exactly that. A frame whose
recomposed content differs from what was signed is therefore refused rather than
stored with a note: "sign what you compare, and compare what you sign". Tolerating
the difference is the canonicalization-divergence class, and RFC 9413 is explicit
that quietly accepting input outside the specification entrenches the error in
every implementation that follows.

The strictness is at the DOOR only. On the READ path a signature that does not
verify downgrades the frame to unsigned and nothing more, the way RFC 6376 6.1
treats a bad DKIM signature: it never removes the message. Here it must not, since
removing a frame tears the logical clock chain for every reader after it.

Module level is stdlib only, the way the rest of this package is: a room peer may
be a slim install or a foreign agent, and `cryptography` is imported inside the
functions that actually need it.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional, Tuple

# The bytes every signature is computed over start with this, so they can never be
# mistaken for the input of some other signature in this system.
DOMAIN = b"vaf-a2a-sig/v2\n"

# The keyring entry every room key is derived from. One secret per machine account;
# the per-room keys come out of it and are never stored.
ROOT_SECRET = "a2a_signing_root_key"

# What the info string of the derivation is built from, kept beside the domain so a
# reader sees both in one place.
DERIVE_INFO = b"vaf-a2a-sign/v1/"

ALG = "ed25519"

# The content fields a signature covers, in the order the specification lists them.
# Not sent on the wire: `v` inside the signed bytes is what pins them.
COVERED = ("kind", "to", "body", "reply_to", "must_understand", "ext")

VERSION = 2


class SigningError(Exception):
    """Base for every refusal this module raises."""


class NotCanonical(SigningError):
    """The payload cannot be written down the same way twice.

    A signature is only worth anything if two implementations independently produce
    the same bytes for the same content. Anything that stops them is refused here
    rather than signed and discovered later by a verifier that disagrees.
    """


class BadSignature(SigningError):
    """The signature object is malformed. Not the same as one that fails to verify."""


def _check_canonical(value: Any, path: str = "body") -> None:
    """Refuse anything two implementations would not serialise identically.

    Three things qualify, and each has a reason rather than a preference:

    - A NON-INTEGER NUMBER. No two languages agree on how to print every float, so
      a deadline written by one peer would not verify at another. The protocol has
      exactly one number that decides anything, `vote.body.closes_at`, and the room
      already stores it as whole seconds.
    - NaN AND INFINITY. `json.dumps` emits `NaN` and `Infinity`, which are not JSON
      at all, so a foreign parser would refuse the bytes it is asked to verify.
    - A KEY THAT IS NOT A STRING. `json.dumps` turns `{1: "a"}` into `{"1": "a"}`,
      so two different objects would produce one set of signed bytes.
    """
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise NotCanonical(f"{path} is not a number this payload can carry")
        raise NotCanonical(
            f"{path} is a fractional number ({value!r}); a signed payload carries "
            f"whole numbers, because no two languages print every float alike"
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise NotCanonical(f"{path} has a key that is not a string: {key!r}")
            _check_canonical(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check_canonical(item, f"{path}[{index}]")


def covered_payload(room_id: str, sender: str,
                    content: Mapping[str, Any]) -> Dict[str, Any]:
    """The object a signature is computed over, built from `Room.compose`'s output.

    EVERY field is normalised to its empty form rather than passed through, and that
    is not tidiness. A frame on disk does not carry its empty fields: `to_dict` emits
    `ext` only when it holds something. So the party that VERIFIES is reading a frame
    where `ext` is absent, while the party that SIGNED had it as `{}` from compose. If
    absent and empty produced different bytes, every frame with an empty extension
    namespace - which is nearly all of them - would verify for the signer and for
    nobody else.

    It went unnoticed here because VAF verifies through parsed `Frame` objects, whose
    reader already coerces absent to empty. A foreign implementation reading the JSON
    does not, and would have found the two sides disagreeing about a value neither of
    them changed. Caught by checking the bytes against the two VAF-free peers rather
    than by checking VAF against itself.

    `must_understand` becomes a list for the same reason: JSON has no tuples, and a
    verifier reads one back as a list.

    `sender` is the handle the room files this under, and a caller must pass the one
    the ROOM resolved rather than one a frame claims: on the signing side that is the
    peer's own id, and on the verifying side it is whose lane the reader is judging.
    Passing a frame's self-declared value back to the verifier would check the
    signature against the very field an attacker edits.
    """
    payload: Dict[str, Any] = {"v": VERSION, "room": str(room_id), "from": str(sender)}
    for field in COVERED:
        value = content.get(field)
        if field == "must_understand":
            value = [str(name) for name in (value or ())]
        elif field == "reply_to":
            value = str(value) if value else None
        elif field == "to":
            # ABSENT MEANS THE ROOM, and the default belongs in the covered form
            # rather than only in `compose`. Without it a sender that omits `to`
            # signs `{}` while the room stores `{"room": true}` and the signature
            # is refused - found by the first test in which a foreign peer signed
            # and VAF checked, which is the mirror of the `ext` divergence and the
            # reason both directions have to be pinned.
            value = (dict(value) if isinstance(value, Mapping) else {}) or {"room": True}
        elif field in ("body", "ext"):
            value = dict(value) if isinstance(value, Mapping) else {}
        else:                                   # kind, which is a name
            value = str(value or "")
        payload[field] = value
    return payload


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    """The exact bytes that get signed, domain-separated.

    The serialisation is the form this codebase already trusts to make a hash chain
    verify across processes (`vaf/core/log_helper.py`, and its verifier in
    `vaf/api/logs_routes.py`): sorted keys, no whitespace, UTF-8 rather than escapes.
    Reused rather than reinvented, so there is one answer in the tree to "what does
    canonical mean here" instead of two that could drift.
    """
    _check_canonical(payload, "payload")
    return DOMAIN + json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def room_seed(participant_key: str, room_id: str) -> bytes:
    """The 32 seed bytes of this participant's key IN THIS ROOM.

    Derived, never stored, from one account-level secret. The inputs are the same
    two the room handle is derived from, and that is the point: a participant gets a
    different key in every room, so two transcripts still cannot be correlated by
    reading the keys off them, and nothing has to be kept in sync per room.

    Losing the account secret loses the ability to sign as that participant. It does
    not lose the transcript, and it does not invalidate what was already signed.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    from vaf.core.data_keyring import get_data_secret

    root = get_data_secret(ROOT_SECRET)
    info = DERIVE_INFO + f"{participant_key}:{room_id}".encode("utf-8")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(
        root.encode("utf-8")
    )


def keypair(participant_key: str, room_id: str) -> Tuple[Any, str]:
    """This participant's private key in this room, and its public half as hex."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.from_private_bytes(room_seed(participant_key, room_id))
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private, public.hex()


def public_key(participant_key: str, room_id: str) -> str:
    """The public half alone, for the card a peer publishes when it joins."""
    return keypair(participant_key, room_id)[1]


def sign(payload: Mapping[str, Any], *, participant_key: str, room_id: str) -> Dict[str, str]:
    """The `sig` object for a covered payload.

    `v` rides on the wire as well as inside the signed bytes, and the two do different
    jobs. Inside, it pins what the signature COVERS so the claim cannot be edited.
    Outside, it lets a reader tell an older scheme from a forgery without doing the
    sum - which matters because the verdicts differ by more than a shade: `invalid` is
    the one that accuses somebody, and a message signed under version 1 has accused
    nobody. DKIM carries its `v=` on the wire for the same reason.
    """
    private, public = keypair(participant_key, room_id)
    return {
        "alg": ALG,
        "v": VERSION,
        "key": public,
        "sig": private.sign(canonical_bytes(payload)).hex(),
    }


def read_signature(value: Any) -> Optional[Dict[str, str]]:
    """A `sig` field as something that could be checked, or None.

    Read defensively: it arrives from a foreign agent, and a shape this peer cannot
    use is not the same as a forgery. None means "there is nothing here to check",
    which a reader must render differently from "this did not verify".

    A version other than this one reads as nothing to check, in BOTH directions. A
    newer scheme is the case the `unreadable` verdict was written for; an older one is
    its mirror and matters more in practice, because a version 1 signature is a real
    signature by an honest peer over bytes that no longer mean the same thing. Trying
    it and reporting `invalid` would accuse everybody who signed anything before the
    coverage changed. An absent `v` IS version 1, which is what makes those frames
    land here rather than in the accusation.
    """
    if not isinstance(value, Mapping):
        return None
    alg = str(value.get("alg") or "")
    key = str(value.get("key") or "")
    sig = str(value.get("sig") or "")
    try:
        version = int(value.get("v") or 1)
    except (TypeError, ValueError):
        return None
    if alg != ALG or version != VERSION or len(key) != 64 or len(sig) != 128:
        return None
    try:
        bytes.fromhex(key)
        bytes.fromhex(sig)
    except ValueError:
        return None
    return {"alg": alg, "v": version, "key": key, "sig": sig}


def verify(payload: Mapping[str, Any], signature: Any) -> bool:
    """Whether this signature really is over this payload, by the key it names.

    Answers only that. Whether the key BELONGS to the peer the frame is filed under
    is a separate question, answered by folding the room's join frames, and keeping
    the two apart is what stops a host that moves a frame between lanes from passing
    unnoticed.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    read = read_signature(signature)
    if read is None:
        return False
    try:
        message = canonical_bytes(payload)
    except NotCanonical:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(read["key"])).verify(
            bytes.fromhex(read["sig"]), message
        )
    except (InvalidSignature, ValueError):
        return False
    return True


# ── who an agent belongs to: the owner's attestation ───────────────────────
#
# The room derives "which agent is whose" on the host by recomputing handles from
# the accounts it admits, and that derivation reaches nobody who arrived on a ticket
# and nobody reading the transcript on another machine. So the transcript can carry
# the answer itself: an agent's `join` may hold its OWNER's attestation, a signature
# by the owner's room key over the agent's handle and key. The shape is borrowed from
# Nostr's owner-authorisation tag and the doctrine with it - the attestation is
# authorisation EVIDENCE, never identity; the agent stays the author of everything it
# signs, and nothing here grants it anything, because authority in a room is local.

# Domain-separated from a frame signature's input, so neither can be mistaken for
# the other even over identical JSON.
OWNER_DOMAIN = b"vaf-a2a-owner/v1\n"
OWNER_VERSION = 1


def owner_payload(room_id: str, owner: str, agent: str, agent_key: str) -> Dict[str, Any]:
    """What an owner attests: THIS handle, holding THIS key, in THIS room, is mine.

    The KEY is covered and not only the handle, because a handle is assigned by the
    host and a key is held by the agent: attesting a lane would vouch for whoever the
    host lets write there, attesting a key vouches for the party that can sign with
    it. Rotating the agent's key therefore needs a fresh attestation, which is the
    right cost. `room` keeps it from being lifted into another room, `agent` from
    being lifted onto another agent.
    """
    return {"v": OWNER_VERSION, "room": str(room_id), "owner": str(owner),
            "agent": str(agent), "agent_key": str(agent_key)}


def owner_bytes(payload: Mapping[str, Any]) -> bytes:
    """The exact bytes an attestation is computed over, in the one canonical form."""
    _check_canonical(payload, "attestation")
    return OWNER_DOMAIN + json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def attest(room_id: str, *, owner_key: str, owner_peer: str, agent_peer: str,
           agent_key: str) -> Dict[str, Any]:
    """The `owner` block an agent's join carries, signed with the OWNER's room key.

    `owner_key` is the owner's participant key (the `cli` lane of the account the
    agent belongs to), so this is only ever produced for a household this machine
    holds both halves of - the same rule that lets the host sign for its own lanes.
    """
    private, public = keypair(owner_key, room_id)
    message = owner_bytes(owner_payload(room_id, owner_peer, agent_peer, agent_key))
    return {"v": OWNER_VERSION, "peer": str(owner_peer), "key": public,
            "sig": private.sign(message).hex()}


def read_attestation(value: Any) -> Optional[Dict[str, Any]]:
    """An `owner` block as something that could be checked, or None.

    Read the way a signature is read: a shape this reader cannot use is not a
    forgery, and a version other than this one is nothing to check.
    """
    if not isinstance(value, Mapping):
        return None
    peer = str(value.get("peer") or "")
    key = str(value.get("key") or "")
    sig = str(value.get("sig") or "")
    try:
        version = int(value.get("v") or 1)
    except (TypeError, ValueError):
        return None
    if version != OWNER_VERSION or not peer or len(key) != 64 or len(sig) != 128:
        return None
    try:
        bytes.fromhex(key)
        bytes.fromhex(sig)
    except ValueError:
        return None
    return {"v": version, "peer": peer, "key": key, "sig": sig}


def verify_attestation(room_id: str, agent_peer: str, agent_key: str, block: Any) -> bool:
    """Whether this block really is the named owner's signature over THIS agent, HERE.

    Answers only that. Whether the owner's key is the one bound to the owner's own
    handle is the fold's second question (`fold_owners`), and keeping the two apart
    is what stops a block made with a fresh keypair from reading as a household.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    read = read_attestation(block)
    if read is None:
        return False
    try:
        message = owner_bytes(owner_payload(room_id, read["peer"], agent_peer, agent_key))
    except NotCanonical:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(read["key"])).verify(
            bytes.fromhex(read["sig"]), message
        )
    except (InvalidSignature, ValueError):
        return False
    return True
