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
from typing import Any, Dict, Iterable, List, Mapping, Sequence

PROTOCOL = "vaf-a2a"
VERSION = 1

# Every key the protocol defines. Anything else in a frame is an unknown field, and
# rule 1 says it survives untouched.
WIRE_KEYS = frozenset({
    "v", "id", "room", "seq", "lamport", "ts", "from", "role", "to", "kind",
    "reply_to", "body", "must_understand", "ext",
})

KINDS = frozenset({
    "say", "ask", "answer", "report", "directive",
    "join", "leave", "role", "hire", "close", "ack",
})

REPORT_STATUSES = frozenset({
    "submitted", "working", "input_required", "completed", "failed", "rejected",
    "canceled",
})

# What each role may EMIT. Not what it may do to a machine: a room hands out no tool.
CAPABILITIES: Dict[str, frozenset] = {
    "leader": frozenset({"say", "ask", "answer", "report", "directive",
                         "role", "hire", "close", "leave", "ack", "join"}),
    "worker": frozenset({"say", "ask", "answer", "report",
                         "hire", "leave", "ack", "join"}),
    "peer": frozenset({"say", "ask", "answer", "leave", "ack", "join"}),
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
