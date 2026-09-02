# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Signing a frame's CONTENT, so authorship survives leaving the host machine.

A room assigns authorship: `Room.ingest` overwrites `from` and `role` with the
admitted peer's resolved values, which is sound while the host is the one who
admitted the connection and stops being sound the moment somebody reads the
transcript somewhere else. `vaf a2a export` is a claim, not evidence.

WHAT IS SIGNED, and why exactly that. The peer is the authority over CONTENT and
the room over PLACEMENT, so the covered payload is the room's id plus the six
fields `Room.compose` returns, and nothing else:

    {"v": 1, "room": ..., "kind": ..., "to": ..., "body": ...,
     "reply_to": ..., "must_understand": [...], "ext": {...}}

`room` is in there so a signed frame cannot be lifted into another room. `id`,
`ts`, `seq`, `lamport`, `from` and `role` are deliberately out: they are assigned
after the payload arrives, so a sender cannot know them, and signing what another
party fills in is the mistake RFC 9421 warns about (do not sign what an
intermediary alters). A host that files one peer's signed frame into another
peer's lane is caught anyway, because a reader checks the signing key against the
key that lane's own join frame carries.

There is no covered-field list on the wire. RFC 6376 and RFC 9421 both put the
coverage claim inside the signed bytes so it cannot be edited afterwards, and both
need it because coverage varies per message. Here it does not: version 1 covers
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
DOMAIN = b"vaf-a2a-sig/v1\n"

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

VERSION = 1


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


def covered_payload(room_id: str, content: Mapping[str, Any]) -> Dict[str, Any]:
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
    """
    payload: Dict[str, Any] = {"v": VERSION, "room": str(room_id)}
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
    """The `sig` object for a covered payload."""
    private, public = keypair(participant_key, room_id)
    return {
        "alg": ALG,
        "key": public,
        "sig": private.sign(canonical_bytes(payload)).hex(),
    }


def read_signature(value: Any) -> Optional[Dict[str, str]]:
    """A `sig` field as something that could be checked, or None.

    Read defensively: it arrives from a foreign agent, and a shape this peer cannot
    use is not the same as a forgery. None means "there is nothing here to check",
    which a reader must render differently from "this did not verify".
    """
    if not isinstance(value, Mapping):
        return None
    alg = str(value.get("alg") or "")
    key = str(value.get("key") or "")
    sig = str(value.get("sig") or "")
    if alg != ALG or len(key) != 64 or len(sig) != 128:
        return None
    try:
        bytes.fromhex(key)
        bytes.fromhex(sig)
    except ValueError:
        return None
    return {"alg": alg, "key": key, "sig": sig}


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
