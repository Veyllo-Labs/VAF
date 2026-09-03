# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The A2A wire contract: one frame, and the rules that keep it readable by
implementations that do not know each other.

Five rules make up the whole forward-compatibility promise, and every one of them
is testable in a few lines:

1. An unknown TOP-LEVEL field is PRESERVED. A peer that relays a frame relays it
   unchanged; a peer that only renders may ignore the field but must never strip
   it from what it stores.

   Deliberate divergence from ``session.Message.from_dict``, which filters to
   known fields. That filter is correct at a STORAGE boundary, where an unknown
   key is legacy debris. It is catastrophic at a RELAY boundary, where an unknown
   key is a newer peer's meaning travelling through an older one. This codebase
   has shipped the dropping bug twice already (Message.from_dict, and the field
   by field rebuild in the web client), so the divergence is stated here rather
   than discovered later.

2. An unknown ``kind`` renders as an opaque event. It is not acted on and it is
   not removed: removing it would tear the lamport chain for every later reader.

3. An unknown key under ``ext`` may be ignored. ``ext`` is the ONLY namespace with
   that permission; everything outside it falls under rule 1.

4. An unknown MAJOR version means leaving the room. Half speaking a protocol you
   do not know is worse than being absent, so parsing raises instead of guessing.

5. ``must_understand`` lets a sender list fields the receiver must comprehend. A
   receiver that does not refuses with ``ack{status:"unsupported"}`` and takes no
   other action. This is the escape hatch that lets a future breaking feature ship
   without forcing a major bump on everyone.

Ordering is LOGICAL, never chronological. ``ts`` is advisory and exists so a human
reading a transcript sees wall-clock times; ``lamport`` is what orders. That choice
is what makes clock skew between two machines a non-event, and it is the reason the
cross-machine step needs no change to this module.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

# The protocol name and MAJOR version. A peer announcing a different major leaves.
PROTOCOL = "vaf-a2a"
VERSION = 1

# What a frame can be. Unknown values parse (rule 2) but are not actionable.
KINDS = frozenset({
    "say", "ask", "answer", "report", "directive",
    "join", "leave", "role", "hire", "close", "ack", "kick",
    # The room asking one peer whether it is still with it. Emitted by the host
    # only, addressed to a single peer, and carrying that peer's own situation -
    # see Room.ping. A kind rather than a body flag on `say`, so an
    # implementation that has never heard of it shows it and does not act on it
    # (rule 2) instead of mistaking a status probe for something somebody said.
    "ping",
    # A question the room decides together: the frame that OPENS a vote. The
    # ballots are ordinary `answer` frames with reply_to pointing at it and a
    # `choice` in the body - a second kind for them would be a second way to say
    # "this answers that", which the protocol already has.
    "vote",
    # How a vote ENDED, written once by the host when every member has answered
    # or the deadline passed: the counts, who chose what, and who let it run out.
    # A frame rather than a projection, because the outcome is the one part of a
    # vote that must survive being read by somebody who was not there - and
    # because the write itself is what makes closing happen exactly once. It
    # answers the vote (reply_to), so a peer that only knows `answer` still sees
    # the result attached to the question.
    "tally",
    # The cheapest thing a member can say: an emoji on ONE message (reply_to) and
    # nothing else. Shown to everybody, counted for nobody's badge, and never a
    # wake - a room where "seen" costs a message wakes everyone to read nothing.
    # The one time it does cost a turn is at a peer that has never heard of it,
    # which shows it (rule 2) the way it shows any kind it does not know.
    "reaction",
})

# A peer's standing in one room. Roles are derived from the log, never asserted
# by the sender: Room.ingest overwrites whatever arrived here.
ROLES = frozenset({"leader", "worker", "peer"})

# The task vocabulary for report.body.status, taken from the open A2A standard so
# the two stay interchangeable if a bridge is ever built. Without it a directive
# has only "given" and "reported", with no way to say "working" or "rejected".
REPORT_STATUSES = frozenset({
    "submitted", "working", "input_required",
    "completed", "failed", "rejected", "canceled",
})


def read_progress(body: Any) -> Optional[Dict[str, Any]]:
    """`report.body.progress` as something a surface can draw, or None.

    A status says WHETHER work is running; this says how far it has come, which
    is the difference between a task board and a spinner: `working` sits
    unchanged for ten minutes and looks identical to a hang.

    It lives in the body, so the wire form is untouched and a peer that knows
    nothing of it keeps working (rule 1: unknown content is preserved, not
    interpreted). Reading it is DEFENSIVE for the same reason display names
    are never trusted - it arrives from a foreign agent, so anything unusable
    is dropped rather than passed on: counts must be whole and not negative,
    a total that is smaller than the count is the sender's error and not a
    reason to lie, and the step text is capped. None means "this report says
    nothing about progress", which a renderer must show differently from
    "0 of 0".
    """
    if not isinstance(body, dict):
        return None
    raw = body.get("progress")
    if not isinstance(raw, dict):
        return None
    out: Dict[str, Any] = {}
    for key in ("done", "total"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
            continue
        number = int(value)
        if number < 0:
            continue
        out[key] = number
    if "done" in out and "total" in out and out["done"] > out["total"]:
        out["done"] = out["total"]
    step = raw.get("step")
    if isinstance(step, str) and step.strip():
        out["step"] = step.strip()[:120]
    return out or None


def read_deadline(body: Any) -> Optional[int]:
    """`vote.body.closes_at` as WHOLE SECONDS, or None when it says nothing usable.

    Read defensively for the same reason `read_progress` is: the value arrives from a
    foreign agent, and it lands in a write-once file that every later reader parses
    again. A `closes_at` of `"bald"` used to reach `float()` inside the vote fold and
    raise, and because the frame can never be removed, that one message would have
    ended voting in that room permanently, for everybody, on every surface that folds
    a tally.

    WHOLE SECONDS rather than the float a clock hands out. A deadline is the one wall
    clock in this protocol that decides anything, so it is also the one value in a
    body that two machines have to serialize identically. Sub-second precision buys
    nothing here - a vote lives minutes - and it costs the ability to write the value
    down the same way twice.
    """
    if not isinstance(body, Mapping):
        return None
    raw = body.get("closes_at")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if raw != raw or raw in (float("inf"), float("-inf")):  # NaN / inf
        return None
    seconds = int(raw)
    return seconds if seconds > 0 else None


# Every key this version defines. Anything else in a frame is an unknown field and
# falls under rule 1. Used by the must_understand check as the baseline of what a
# conforming peer of this version necessarily comprehends.
WIRE_KEYS = frozenset({
    "v", "id", "room", "seq", "lamport", "ts", "from", "role",
    "to", "kind", "reply_to", "body", "must_understand", "ext",
    # A peer's OPTIONAL claim that it wrote this frame's content. Absent from
    # almost every frame, and a peer that has never heard of it reads the
    # conversation exactly as before (rule 1). See vaf/core/a2a/signing.py for
    # what it covers and why it covers only that.
    "sig",
})

_REQUIRED = ("id", "room", "seq", "lamport", "from", "role", "kind", "to")


class FrameError(Exception):
    """Base for every refusal this module raises."""


class MalformedFrame(FrameError):
    """A required field is missing or of the wrong shape."""


class UnsupportedVersion(FrameError):
    """Rule 4: the frame announces a major version this peer does not speak.

    The caller's obligation is to emit ``leave{reason:"unsupported_version"}`` and
    disconnect, not to try parsing anyway.
    """

    def __init__(self, version: Any) -> None:
        super().__init__(
            f"frame announces {PROTOCOL} major version {version!r}, this peer speaks {VERSION}"
        )
        self.version = version


class UnsupportedRequirement(FrameError):
    """Rule 5: the frame requires fields this peer does not comprehend.

    The caller's obligation is to answer ``ack{status:"unsupported"}`` and take NO
    other action - not to process the parts it happens to recognise.
    """

    def __init__(self, missing: Sequence[str]) -> None:
        super().__init__("frame requires fields this peer does not understand: "
                         + ", ".join(sorted(missing)))
        self.missing = tuple(sorted(missing))


def object_field(value: Any, name: str) -> Dict[str, Any]:
    """A frame field that must be an object, where somebody is ASSERTING it.

    Two different situations read the same field and must answer differently.
    ``from_dict`` is reading a file back and takes a non-object as ``{}`` on
    purpose: a stored frame has to stay readable whatever is in it, and refusing to
    parse one would remove it from the transcript, which is rule 2's mistake. This
    is the other situation - a caller minting a frame, or a peer submitting one -
    where there is somebody in front of it to tell. Telling them beats writing a
    shape the reader would then silently discard, because the two halves of the
    frame would afterwards disagree about what it says.

    Anything empty, of any type, means "nothing here" and is the same answer as
    absent. Only a non-empty value that is not an object is a refusal.
    """
    if not value:
        return {}
    if not isinstance(value, Mapping):
        raise MalformedFrame(
            f"{name!r} must be an object, got {type(value).__name__}"
        )
    return dict(value)


def required_names(value: Any) -> Tuple[str, ...]:
    """``must_understand`` as ``from_dict`` will read it back.

    A single string is refused rather than iterated: ``tuple("id")`` is
    ``('i', 'd')``, three field names nobody asked for.
    """
    if not value:
        return ()
    if isinstance(value, (str, bytes)):
        raise MalformedFrame("must_understand must be a list of field names")
    return tuple(str(f) for f in value)


@dataclass
class Frame:
    """One message in a room.

    ``sender`` carries the wire's ``from``, which is a Python keyword and therefore
    cannot be an attribute name. The wire spelling is the one that matters and is
    what ``to_dict`` emits.

    ``sender`` and ``role`` are NEVER trusted as they arrive. Room.ingest assigns
    both from the admitted peer, the same way the tool dispatcher assigns identity
    over whatever a model produced.
    """

    id: str
    room: str
    seq: int
    lamport: int
    sender: str
    role: str
    kind: str
    to: Dict[str, Any]
    ts: float = 0.0
    body: Dict[str, Any] = field(default_factory=dict)
    reply_to: Optional[str] = None
    must_understand: Tuple[str, ...] = ()
    ext: Dict[str, Any] = field(default_factory=dict)
    # The sender's OPTIONAL claim that it wrote this frame's content, over the
    # fields it actually controls. None is the ordinary case and means nothing was
    # claimed, which a reader must render differently from a claim that failed.
    sig: Optional[Dict[str, Any]] = None
    v: int = VERSION
    # Everything the source carried, so an unknown field survives a round trip
    # byte for byte (rule 1). Not part of equality-by-meaning, hence repr=False.
    _raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def new(
        cls,
        *,
        room: str,
        sender: str,
        role: str,
        kind: str,
        seq: int,
        lamport: int,
        to: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        reply_to: Optional[str] = None,
        must_understand: Iterable[str] = (),
        ext: Optional[Dict[str, Any]] = None,
        sig: Optional[Mapping[str, Any]] = None,
        frame_id: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> "Frame":
        """Mint a frame. ``frame_id`` and ``ts`` are injectable so a test can pin
        both without patching the clock or the uuid module.

        Every field is read the way ``from_dict`` will read it back, and that
        symmetry is load-bearing rather than tidy. A frame lives twice: once as the
        object the minting process holds, and once as the object a reader parses out
        of the file. Where the two coercions differed, one frame had two meanings -
        ``must_understand=[1, 2]`` was ``(1, 2)`` in memory and ``('1', '2')`` after
        a round trip, and ``reply_to=""`` was ``''``, then absent, then ``None``.
        Nothing read those fields closely enough to break, which is precisely why it
        went unnoticed; anything that compares a frame against its stored form needs
        them to agree.
        """
        return cls(
            id=frame_id or str(uuid.uuid4()),
            room=str(room),
            seq=int(seq),
            lamport=int(lamport),
            sender=str(sender),
            role=str(role),
            kind=str(kind),
            to=object_field(to, "to") or {"room": True},
            ts=float(ts) if ts is not None else time.time(),
            body=object_field(body, "body"),
            reply_to=(str(reply_to) if reply_to else None),
            must_understand=required_names(must_understand),
            ext=object_field(ext, "ext"),
            sig=(dict(sig) if isinstance(sig, Mapping) and sig else None),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, understood: Iterable[str] = (),
                  enforce_requirements: bool = True) -> "Frame":
        """Parse a frame, enforcing rules 4 and 5 and preserving rule 1.

        ``understood`` names extension fields THIS peer comprehends beyond
        ``WIRE_KEYS``. It is how an application opts in to a field that a sender may
        mark as required.

        ``enforce_requirements=False`` turns rule 5 off, and exactly one caller is
        allowed to use it: a READER reconstructing a stored transcript. Rule 5 governs
        whether a peer may ACT on a frame, and refusing to parse one while merely
        reading would delete it from the room's history - which is rule 2's mistake
        wearing rule 5's clothes, because a frame removed from the log tears the lamport
        chain for every reader after it. A reader renders what it cannot fully
        understand; it just never acts on it.
        """
        if not isinstance(data, Mapping):
            raise MalformedFrame(f"frame must be a mapping, got {type(data).__name__}")

        # Rule 4 first: a frame from a version we do not speak is not ours to
        # validate. Checking anything else first would report the wrong problem.
        raw_version = data.get("v", VERSION)
        try:
            version = int(raw_version)
        except (TypeError, ValueError):
            raise UnsupportedVersion(raw_version) from None
        if version != VERSION:
            raise UnsupportedVersion(raw_version)

        # Rule 5 before any other validation, because a peer that cannot honour the
        # requirement must take NO other action - including complaining about the
        # rest of the frame, which it may be misreading precisely for that reason.
        required_fields = data.get("must_understand") or ()
        if isinstance(required_fields, (str, bytes)):
            raise MalformedFrame("must_understand must be a list of field names")
        if enforce_requirements:
            comprehended = WIRE_KEYS | frozenset(str(f) for f in understood)
            missing = [str(f) for f in required_fields if str(f) not in comprehended]
            if missing:
                raise UnsupportedRequirement(missing)

        for key in _REQUIRED:
            if data.get(key) in (None, ""):
                raise MalformedFrame(f"frame is missing required field {key!r}")
        if not isinstance(data.get("to"), Mapping):
            raise MalformedFrame("'to' must be an object")

        try:
            seq = int(data["seq"])
            lamport = int(data["lamport"])
        except (TypeError, ValueError):
            raise MalformedFrame("'seq' and 'lamport' must be integers") from None
        if seq < 1 or lamport < 1:
            raise MalformedFrame("'seq' and 'lamport' start at 1")

        body = data.get("body")
        ext = data.get("ext")
        return cls(
            id=str(data["id"]),
            room=str(data["room"]),
            seq=seq,
            lamport=lamport,
            sender=str(data["from"]),
            role=str(data["role"]),
            kind=str(data["kind"]),
            to=dict(data["to"]),
            ts=float(data.get("ts") or 0.0),
            body=dict(body) if isinstance(body, Mapping) else {},
            reply_to=(str(data["reply_to"]) if data.get("reply_to") else None),
            must_understand=tuple(str(f) for f in required_fields),
            ext=dict(ext) if isinstance(ext, Mapping) else {},
            sig=(dict(data["sig"]) if isinstance(data.get("sig"), Mapping) and data["sig"]
                 else None),
            v=version,
            _raw=dict(data),
        )

    # ── serialisation ───────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """The frame as it goes back on the wire or into the log.

        Starts from everything the source carried, so an unknown top-level field
        survives untouched (rule 1), then lays the parsed fields over it. A frame
        minted by ``new()`` has no source and gets the canonical shape.
        """
        out: Dict[str, Any] = dict(self._raw)
        out.update({
            "v": self.v,
            "id": self.id,
            "room": self.room,
            "seq": self.seq,
            "lamport": self.lamport,
            "ts": self.ts,
            "from": self.sender,
            "role": self.role,
            "kind": self.kind,
            "to": self.to,
            "body": self.body,
        })
        # Optional fields are emitted only when they carry something. A source that
        # spelled them out explicitly keeps them through _raw.
        if self.reply_to:
            out["reply_to"] = self.reply_to
        if self.must_understand:
            out["must_understand"] = list(self.must_understand)
        if self.ext:
            out["ext"] = self.ext
        if self.sig:
            out["sig"] = self.sig
        return out

    # ── what a reader may conclude ──────────────────────────────────────────

    @property
    def kind_known(self) -> bool:
        """Rule 2: an unknown kind is rendered, never acted on, never dropped."""
        return self.kind in KINDS

    @property
    def unknown_fields(self) -> Tuple[str, ...]:
        """Top-level keys this version does not define. Present so a renderer can
        show that a newer peer said more than we understand, instead of pretending
        the frame was complete."""
        return tuple(sorted(k for k in self._raw if k not in WIRE_KEYS))

    def addresses(self, peer_id: str, role: Optional[str] = None) -> bool:
        """Whether this frame is aimed at the given peer.

        A ROUTING HINT, not a confidentiality boundary: every member of a room can
        read every frame in it. Anything that must not be seen belongs in a child
        room. Saying otherwise here would be a security lie, so it is said plainly.
        """
        to = self.to or {}
        if to.get("room"):
            return True
        if to.get("peer") == peer_id:
            return True
        if role and to.get("role") == role:
            return True
        peers = to.get("peers")
        return bool(isinstance(peers, (list, tuple)) and peer_id in peers)


def screen_inbound(payload: Mapping[str, Any], *, understood: Iterable[str] = ()) -> None:
    """Apply rules 4 and 5 to something a peer just submitted.

    A frame arriving at a door has not been assigned its ``id``, ``seq``, ``lamport`` or
    ``from`` yet - the room does that, and refuses to honour them if the sender tried.
    ``from_dict`` therefore cannot be used here: it would demand fields the sender is
    not supposed to provide.

    What it CAN and must check is the pair that decides whether this peer may be
    understood at all, and both raise rather than returning a verdict, so a caller that
    forgets to look cannot proceed by accident.
    """
    if not isinstance(payload, Mapping):
        raise MalformedFrame(f"frame must be a mapping, got {type(payload).__name__}")

    raw_version = payload.get("v", VERSION)
    try:
        version = int(raw_version)
    except (TypeError, ValueError):
        raise UnsupportedVersion(raw_version) from None
    if version != VERSION:
        raise UnsupportedVersion(raw_version)

    required_fields = payload.get("must_understand") or ()
    if isinstance(required_fields, (str, bytes)):
        raise MalformedFrame("must_understand must be a list of field names")
    comprehended = WIRE_KEYS | frozenset(str(f) for f in understood)
    missing = [str(f) for f in required_fields if str(f) not in comprehended]
    if missing:
        raise UnsupportedRequirement(missing)


# ── ordering ────────────────────────────────────────────────────────────────

def plausible_frame_id(value: Any) -> bool:
    """Whether this could be a frame id at all - not whether the frame exists.

    Found live, on the first real collaboration: a model handed room_send the
    MESSAGE TEXT as reply_to, the room stored it faithfully, and the task board
    split one task into two. The room itself must stay tolerant (a reply to a
    frame that has not ARRIVED yet is legal under at-least-once delivery, and a
    foreign implementation may mint ids of its own shape), so the door that
    refuses is the SENDING tool, and this is the one rule both doors share:
    an id has no whitespace and fits in 64 characters. A paragraph does neither.
    """
    text = str(value or "")
    return 0 < len(text) <= 64 and not any(ch.isspace() for ch in text)


def canonical_sort_key(frame: "Frame") -> Tuple[int, str, int]:
    """The total order every reader computes identically without coordination.

    ``(lamport, sender, seq)``. Lamport carries causality; the sender breaks a tie
    between concurrent writers deterministically; seq breaks a tie within one
    sender. ``ts`` is deliberately absent - it is wall clock from another machine
    and ordering by it would make clock skew a correctness bug.

    The same shape as ``subagent_ipc.claim_task_slot``, which already establishes
    "a total order every racer computes identically from shared state" in-house.
    """
    return (frame.lamport, frame.sender, frame.seq)


def next_lamport(seen: Iterable[int]) -> int:
    """``1 + max(seen)``, and 1 for an empty room. The whole causality rule."""
    highest = 0
    for value in seen:
        if value > highest:
            highest = value
    return highest + 1


def in_canonical_order(frames: Iterable["Frame"]) -> list:
    """Frames sorted the one way every conforming peer sorts them."""
    return sorted(frames, key=canonical_sort_key)
