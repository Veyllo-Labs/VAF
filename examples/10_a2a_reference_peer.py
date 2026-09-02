# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A second implementation of the room protocol, written from the document alone.

WHY THIS FILE IMPORTS NOTHING FROM VAF, AND WHY THAT IS THE ENTIRE POINT
-----------------------------------------------------------------------
A test that drives VAF against VAF proves the code agrees with itself. It cannot tell
you whether the protocol is IMPLEMENTABLE, because every rule it exercises is enforced
by the same lines it is checking. Two implementations that never share a line, checked
against literally the same assertions, can.

So this file is written the way a stranger would write it: from
`docs/agents/A2A_PROTOCOL.md`, in the standard library, with no knowledge of how VAF
does any of it. `tests/test_a2a_conformance.py` runs the conformance list against this
module AND against VAF's own, parametrised, so a rule that only one of them keeps is a
failure rather than a footnote. A guard in that file fails if an import of `vaf` ever
appears here.

WHAT IT COVERS. The rules a receiver must implement to take part: the frame shape, the
five forward-compatibility rules, deduplication, canonical ordering, and what each role
may emit. It is deliberately NOT a transport and not a store - a peer reaches a room
over `vaf a2a` or over the socket, and neither of those is a protocol rule.

Run it to watch the rules work:

    python examples/10_a2a_reference_peer.py
"""
import json
from typing import Any, Dict, Iterable, List, Mapping, Sequence

PROTOCOL = "vaf-a2a"
VERSION = 1

# Every key the protocol defines. Anything else in a frame is an unknown field, and
# rule 1 says it survives untouched.
WIRE_KEYS = frozenset({
    "v", "id", "room", "seq", "lamport", "ts", "from", "role", "to", "kind",
    "reply_to", "body", "must_understand", "ext",
    # An OPTIONAL claim by the sender that it wrote this frame's content. A peer
    # that does not check it still relays and renders it unchanged, which is rule
    # 1 doing exactly the job it was written for.
    "sig",
})

KINDS = frozenset({
    "say", "ask", "answer", "report", "directive",
    "join", "leave", "role", "hire", "close", "ack", "kick",
    # The room checking in on one member that has gone quiet. Emitted by the host
    # of a room, addressed to a single peer, carrying that peer's own situation.
    # A peer never sends one; it reads it, catches up, and acts only if something
    # is actually needed - the frame is an invitation and not an instruction.
    "ping",
    # A question the room decides together. The ballots are ordinary `answer`
    # frames whose reply_to points at the vote and whose body carries a `choice`,
    # so a peer that only implements `answer` can still take part in one.
    "vote",
    # How a vote ended: written once by the host when every member has answered
    # or the deadline passed, answering the vote (reply_to) and carrying the
    # counts plus whoever let it run out. A peer never sends one.
    "tally",
})

REPORT_STATUSES = frozenset({
    "submitted", "working", "input_required", "completed", "failed", "rejected",
    "canceled",
})

# What each role may EMIT. Not what it may do to a machine: a room hands out no tool.
CAPABILITIES: Dict[str, frozenset] = {
    "leader": frozenset({"say", "ask", "answer", "report", "directive",
                         "role", "hire", "close", "leave", "ack", "join", "kick",
                         "vote"}),
    "worker": frozenset({"say", "ask", "answer", "report",
                         "hire", "leave", "ack", "join", "vote"}),
    "peer": frozenset({"say", "ask", "answer", "report", "leave", "ack", "join",
                       "vote"}),
}

ROOM_KINDS = ("chain", "round")


class Refused(Exception):
    """This peer will not process the frame."""


class Malformed(Refused):
    """The frame is not a frame."""


class UnsupportedVersion(Refused):
    """Rule 4: another MAJOR version. Leave the room, do not guess."""


class UnsupportedRequirement(Refused):
    """Rule 5: must_understand names something this peer does not comprehend."""


class NotPermitted(Refused):
    """The sender's role does not allow this kind here."""


# ── rules 4 and 5: the door ────────────────────────────────────────────────

def screen(payload: Mapping[str, Any], understood: Iterable[str] = ()) -> None:
    """Refuse a frame this peer must not process. Raises rather than answering.

    A verdict returned as a value can be forgotten by the caller; an exception cannot.
    """
    if not isinstance(payload, Mapping):
        raise Malformed(f"a frame is an object, got {type(payload).__name__}")

    raw = payload.get("v", VERSION)
    try:
        version = int(raw)
    except (TypeError, ValueError):
        raise UnsupportedVersion(f"unreadable version {raw!r}") from None
    if version != VERSION:
        raise UnsupportedVersion(f"version {version} is not {VERSION}")

    required = payload.get("must_understand") or ()
    if isinstance(required, (str, bytes)):
        raise Malformed("must_understand is a list of field names")
    comprehended = WIRE_KEYS | {str(name) for name in understood}
    missing = [str(name) for name in required if str(name) not in comprehended]
    if missing:
        raise UnsupportedRequirement(f"cannot understand: {', '.join(missing)}")


REQUIRED = ("id", "room", "seq", "lamport", "from", "role", "kind")


def validate(frame: Mapping[str, Any]) -> None:
    """Check a COMPLETE frame, one that has been through a room.

    Separate from `screen` on purpose, and the difference matters to an implementer:
    `screen` runs at the door on something a peer SUBMITTED, which has no `id`, `seq`,
    `lamport` or `from` yet because the room assigns those and refuses to honour them
    if the sender tried. `validate` runs on what comes back out.

    `seq` and `lamport` are ONE-based. A zero for either is malformed, not "the first
    one" - worth stating because an implementation counting from zero produces frames
    that are refused, and the refusal arrives from another machine.
    """
    if not isinstance(frame, Mapping):
        raise Malformed(f"a frame is an object, got {type(frame).__name__}")
    for field in REQUIRED:
        if field not in frame:
            raise Malformed(f"missing required field {field!r}")
    try:
        seq, lamport = int(frame["seq"]), int(frame["lamport"])
    except (TypeError, ValueError):
        raise Malformed("seq and lamport are integers") from None
    if seq < 1 or lamport < 1:
        raise Malformed("seq and lamport start at 1")


# ── rule 1: an unknown field survives a relay ──────────────────────────────

def relay(frame: Mapping[str, Any]) -> Dict[str, Any]:
    """Hand a frame on exactly as it arrived.

    Dropping unknown keys is right at a STORAGE boundary and fatal at a RELAY one: the
    field a later version added is the field this peer does not recognise.
    """
    return dict(frame)


# ── rule 2: an unknown kind is shown, never dropped ────────────────────────

def is_actionable(frame: Mapping[str, Any]) -> bool:
    """Whether this peer should ACT on the frame. An unknown kind is displayed and
    kept, but never acted on - and never removed, because removing it tears the lamport
    chain for everybody reading later."""
    return str(frame.get("kind") or "") in KINDS


# ── ordering ───────────────────────────────────────────────────────────────

def sort_key(frame: Mapping[str, Any]):
    """The total order: lamport, then sender, then that sender's sequence.

    `ts` is ADVISORY and appears nowhere here. Two machines in one room do not agree
    about the time, so a reader that sorted by it would see a different conversation
    from everybody else.
    """
    return (int(frame.get("lamport") or 0), str(frame.get("from") or ""),
            int(frame.get("seq") or 0))


def order(frames: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [dict(f) for f in sorted(frames, key=sort_key)]


def dedupe(frames: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """At-least-once delivery is the promise, so a receiver deduplicates. On `id` and
    on nothing else: two different frames may share every other field."""
    seen, out = set(), []
    for frame in frames:
        key = str(frame.get("id") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(dict(frame))
    return out


# ── signatures: the BYTES, which are the part that drifts ──────────────────

SIG_DOMAIN = b"vaf-a2a-sig/v2\n"

#: The SIGNATURE version, which is not the protocol version above and only looked
#: like it while both were 1. It counts what a signature COVERS: v2 added the
#: sender's handle, so v1 signatures no longer verify anywhere.
SIG_VERSION = 2

#: What a signature covers, in the order the document lists them. Not carried on the
#: wire: `v` inside the signed bytes is what pins the set, so a different coverage
#: would be a different version.
COVERED = ("kind", "to", "body", "reply_to", "must_understand", "ext")


class NotCanonical(Refused):
    """A payload two implementations would not serialise identically."""


def _canonical(value: Any, path: str = "payload") -> None:
    """Refuse what cannot be written down the same way twice.

    A fractional number is the one that bites in practice: no two languages print
    every float alike, so a deadline written by one peer verifies nowhere else. NaN
    and infinity are not JSON at all. A key that is not a string collapses two
    different objects onto one set of signed bytes.
    """
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise NotCanonical(f"{path} is not a number a payload can carry")
        raise NotCanonical(f"{path} is fractional; a signed payload carries whole numbers")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise NotCanonical(f"{path} has a key that is not a string: {key!r}")
            _canonical(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _canonical(item, f"{path}[{index}]")


def signing_bytes(frame: Mapping[str, Any], room_id: str = "") -> bytes:
    """The exact bytes a signature over this frame is computed on.

    Written from the document's Signing section and nothing else, which is the whole
    point of this file: if these bytes and VAF's differ by one byte, every signature
    crossing between the two implementations fails, and it fails silently. `room` is
    covered so a signed frame cannot be lifted into another room; `id`, `ts`, `seq`,
    `lamport`, `from` and `role` are placement, assigned after the payload arrives,
    and a sender cannot sign what somebody else fills in.

    `room_id` is the room the READER is in, and covering that rather than the frame's
    own field is what makes the sentence above true. A frame carries a `room` of its
    own; believing it means a store copied into another room verifies there as well,
    which is the lift the field was supposed to prevent.

    `from` is read off the frame, and that is right where `room` is wrong. A reader
    is judging the handle the frame claims, so covering the claimed value is what makes
    editing it break the signature. A room is not claimed by the frame at all: the
    reader already knows which room it opened.
    """
    payload: Dict[str, Any] = {"v": SIG_VERSION,
                               "room": str(room_id or frame.get("room") or ""),
                               "from": str(frame.get("from") or "")}
    for field in COVERED:
        value = frame.get(field)
        if field == "must_understand":
            value = [str(name) for name in (value or [])]
        elif field == "reply_to":
            value = str(value) if value else None
        elif field == "to":
            # Absent means THE ROOM. A sender that omits it would otherwise sign
            # {} while the room stores {"room": true}, and the signature would be
            # refused for a message nobody tampered with.
            value = (dict(value) if isinstance(value, Mapping) else {}) or {"room": True}
        elif field in ("body", "ext"):
            value = dict(value) if isinstance(value, Mapping) else {}
        else:                                   # kind, which is a name
            # The last field taken raw was `ext`, and taking it raw meant one side
            # wrote null where the other wrote {} and nothing verified across the
            # two. `kind` is the only one left, so it is coerced here too rather
            # than left as the next instance of the same class.
            value = str(value or "")
        payload[field] = value
    _canonical(payload)
    return SIG_DOMAIN + json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def signing_keys(frames: Iterable[Mapping[str, Any]], room_id: str,
                 verify: Any = None) -> Dict[str, str]:
    """Which public key each peer published, folded from the `join` frames.

    A FOLD, like the roles, so any reader recomputes it from the transcript alone.
    Never from a peer record: that is mutable and lives on the host's disk, and a
    host that could swap a key there could forge every later frame from that peer.

    A KEY COUNTS ONLY IF ITS OWN JOIN IS SIGNED BY IT, which is why this fold takes
    the same injected `verify` the verdict does. Lying in a write-once lane is not
    enough by itself: a public key is public, so a host that writes the log can copy
    one peer's key into another peer's join and file the first peer's signed frames
    under the second handle. A join signed by the key it carries cannot be produced
    without the private half.

    Three outcomes: an attested key binds (the last wins, so rejoining rotates), no
    key withdraws, and an UNATTESTED key does nothing at all - it must not withdraw,
    or stripping a `sig` off a stored join would downgrade an honest peer's whole
    history.

    WITHOUT A VERIFIER nothing can be attested and this returns an empty mapping.
    That is the same refusal to guess `verdict` makes, and it costs a reader nothing:
    without a verifier every signed frame is `unchecked` before the keys are ever
    consulted.
    """
    keys: Dict[str, str] = {}
    for frame in sorted(frames, key=sort_key):
        if str(frame.get("kind") or "") != "join":
            continue
        sender = str(frame.get("from") or "")
        # A body that is not an object is a malformed frame, not a key. Reading it
        # with .get would raise, and a fold that raises takes the reader with it.
        body = frame.get("body")
        published = str(body.get("sign_key") or "") if isinstance(body, Mapping) else ""
        if not published:
            keys.pop(sender, None)
            continue
        sig = frame.get("sig")
        if not isinstance(sig, Mapping) or str(sig.get("key") or "") != published:
            continue
        if verdict(frame, {sender: published}, verify, room_id) == "valid":
            keys[sender] = published
    return keys


def verdict(frame: Mapping[str, Any], keys: Mapping[str, str],
            verify: Any = None, room_id: str = "") -> str:
    """What a reader may conclude about who wrote this frame's content.

    `verify(public_key_hex, signature_hex, message)` is injected because Ed25519 is
    not in the standard library and this file is about the PROTOCOL, not about curve
    arithmetic. Without one, a frame that carries a signature is `unchecked` rather
    than trusted - the honest answer for a reader that cannot do the sum.

    Five answers, and the distinctions are the point: `unsigned` (nothing claimed,
    the ordinary case), `unreadable` (a claim this version cannot parse, which is
    what a newer scheme looks like to an older reader), `valid`, `foreign_key` (a
    real signature by no key this peer ever published in a checkable form, which is
    what a frame written into the wrong lane looks like, and equally what a peer whose
    client announced a key without signing the announcement looks like), `invalid`
    (the only verdict that accuses).

    It NEVER raises and never removes a frame: a bad signature downgrades what may
    be concluded, nothing more.
    """
    sig = frame.get("sig")
    if not isinstance(sig, Mapping) or not sig:
        return "unsigned"
    key, blob = str(sig.get("key") or ""), str(sig.get("sig") or "")
    # An absent `v` IS version 1. A signature from another version was made over bytes
    # that mean something else, so there is nothing here this reader can check - and
    # that is not `invalid`, the one verdict that accuses somebody.
    try:
        version = int(sig.get("v") or 1)
    except (TypeError, ValueError):
        return "unreadable"
    if (str(sig.get("alg") or "") != "ed25519" or version != SIG_VERSION
            or len(key) != 64 or len(blob) != 128):
        return "unreadable"
    if verify is None:
        return "unchecked"
    try:
        if not verify(key, blob, signing_bytes(frame, room_id)):
            return "invalid"
    except Exception:
        return "unreadable"
    published = keys.get(str(frame.get("from") or ""))
    return "valid" if published and published == key else "foreign_key"


def gaps(seqs: Iterable[int]) -> List[int]:
    """Which of a sender's sequence numbers are missing. Per-sender FIFO is gapless, so
    holding 5 and 7 is knowing that 6 has not arrived."""
    numbers = sorted({int(n) for n in seqs})
    if not numbers:
        return []
    return [n for n in range(numbers[0], numbers[-1]) if n not in set(numbers)]


# ── roles ──────────────────────────────────────────────────────────────────

def may_emit(role: str, kind: str, room_kind: str = "round") -> bool:
    """Whether a role may send this kind in this sort of room.

    The round rule is separate from the capability table on purpose: it is about the
    ROOM, not about the sender. Nobody at all may command in a round.
    """
    if room_kind == "round" and kind == "directive":
        return False
    if kind not in KINDS:
        return True                    # rule 2: unknown kinds are not this peer's call
    return kind in CAPABILITIES.get(role, frozenset())


def check_emit(role: str, kind: str, room_kind: str = "round") -> None:
    if not may_emit(role, kind, room_kind):
        raise NotPermitted(f"a {role} may not emit {kind!r} in a {room_kind}")


def main() -> None:
    print("A2A reference peer - the rules, exercised\n")

    frames = [
        {"v": 1, "id": "c", "room": "r", "from": "p-b", "seq": 0, "lamport": 2,
         "ts": 1.0, "role": "peer", "kind": "say", "body": {"text": "third"}},
        {"v": 1, "id": "a", "room": "r", "from": "p-a", "seq": 0, "lamport": 1,
         "ts": 9.0, "role": "peer", "kind": "say", "body": {"text": "first"}},
        {"v": 1, "id": "b", "room": "r", "from": "p-a", "seq": 1, "lamport": 2,
         "ts": 5.0, "role": "peer", "kind": "say", "body": {"text": "second"}},
    ]
    print("ordered by (lamport, from, seq), NOT by ts:")
    for frame in order(frames):
        print(f"   {frame['lamport']:>2} {frame['from']} #{frame['seq']}  "
              f"{frame['body']['text']}")

    print("\nan unknown field survives a relay:")
    carried = relay({"v": 1, "id": "x", "kind": "say", "mood": "curious"})
    print(f"   {carried}")

    print("\nan unknown kind is shown but not acted on:")
    print(f"   actionable({{'kind': 'telemetry'}}) = "
          f"{is_actionable({'kind': 'telemetry'})}")

    print("\nrules 4 and 5 refuse:")
    for payload, why in (({"v": 2, "kind": "say"}, "another major version"),
                         ({"v": 1, "kind": "say", "must_understand": ["priority"]},
                          "a field this peer does not know")):
        try:
            screen(payload)
        except Refused as refusal:
            print(f"   {why}: {type(refusal).__name__} - {refusal}")

    print("\nnobody commands in a round:")
    for role in ("leader", "worker", "peer"):
        print(f"   {role:>6} may directive in a round: "
              f"{may_emit(role, 'directive', 'round')}   in a chain: "
              f"{may_emit(role, 'directive', 'chain')}")


if __name__ == "__main__":
    main()
