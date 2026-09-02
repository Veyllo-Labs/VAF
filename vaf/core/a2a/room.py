# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Membership, roles, and the rules that decide what a peer may say.

The one sentence this module exists to enforce
----------------------------------------------
A ROOM IS A MESSAGE BUS, NOT AN AUTHORITY. It hands out no tool, lifts no
restriction, and carries no identity into the tool funnel.

A foreign agent in a room is a full agent with its own capabilities running on its
own side; VAF lends it nothing. A VAF agent in a room still calls its tools under
its own bound identity, through the same funnel as always, and a ``directive`` that
arrives is INPUT, never a warrant. What a room assigns is a ROLE, and a role governs
what a peer may EMIT, not what it may do to the machine.

That is also why no synthetic tenant identity is invented for a peer. Three gates in
this tree read "no scope" or "admin" as unrestricted (the account allowlist, the file
jail, and an allowlist lookup that returns None for a scope with no record), and a
made-up identity would walk straight into them. A room peer never reaches those gates
at all, because it never triggers a tool call.

Roles are derived, never stored as mutable state
------------------------------------------------
The role of a peer at any point is the FOLD over the ``join``, ``role`` and ``leave``
frames in the log. Nothing rewrites a role in place, so any reader can recompute the
whole membership history from the transcript and two readers cannot disagree.

A worker that hires becomes a leader in a CHILD room, not by promotion
---------------------------------------------------------------------
Promotion inside one room needs agreement about who promoted whom, seen by whom, at
which lamport. That is distributed consensus. Creating a child room is a single act
by a single writer and needs none. The parent keeps the ``hire`` frame and the
child's ``report``, and never the child's transcript: that containment is what lets
the chain of command grow without every ancestor drowning in its descendants' chatter.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from vaf.core.a2a.frame import (KINDS, REPORT_STATUSES, Frame, MalformedFrame,
                                canonical_sort_key, object_field, read_deadline,
                                read_progress,
                                required_names)
from vaf.core.a2a.store import (
    RoomStore,
    StoreError,
    check_name,
    list_rooms,
    new_peer_id,
    new_room_id,
)

# What a room is for. `chain` has one leader and N workers and is where a directive
# means something; `round` is a conversation among equals. A two-member chain covers
# the direct case, so there is no third kind to keep in sync.
ROOM_KINDS = ("chain", "round")

# What each role may EMIT. Not what it may do to the machine - see the module
# docstring. `ack` and `leave` are open to everyone because refusing them would let a
# peer become unable to say it is leaving.
CAPABILITIES: Dict[str, frozenset] = {
    "leader": frozenset({"say", "ask", "answer", "report", "directive",
                         "role", "hire", "close", "leave", "ack", "join", "kick",
                         "vote"}),
    "worker": frozenset({"say", "ask", "answer", "report",
                         "hire", "leave", "ack", "join", "vote"}),
    "peer": frozenset({"say", "ask", "answer", "report", "leave", "ack", "join",
                       "vote"}),
}

# How much of a room's traffic the LOCAL user has authorised their agent to act on.
# Written by the peer into its own member record, never read from a frame: an agent's
# autonomy is granted locally and can never be handed over by a remote leader.
ROOM_MODES = ("observe", "assist", "autonomous")
DEFAULT_MODE = "assist"

# How long a vote waits for a member before the room reminds it, and how long
# after that reminder it stops waiting and counts the member as abstaining. The
# two together are the default life of a vote that named no deadline of its own:
# a question nobody ever answers must still end, or a room fills up with open
# questions that everybody has stopped reading.
#
# They live here rather than in the surface that runs the clock because the
# ANSWER they produce - "this vote is over, and these members abstained" - is
# part of the protocol: a second implementation must reach the same tally from
# the same frames, and it cannot if the deadline is a setting of ours.
VOTE_REMIND_AFTER_S = 60.0
VOTE_ABSTAIN_AFTER_S = 120.0

DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_CHILDREN = 8

# A lease older than this makes a peer "stale" to readers. It never makes them gone:
# only a leave frame does that, and only the peer or a leader writes one.
LEASE_TTL_S = 90.0
#: How long an invitation waits for its answer unless the inviter says otherwise.
#: One hour, the same default the agent ticket has always had: long enough to
#: walk over and paste it, short enough that a forgotten one is not a standing door.
DEFAULT_INVITE_TTL_S = 3600.0
#: What an invitation can be. Written once here so every surface names them alike.
INVITATION_STATES = ("pending", "accepted", "declined", "revoked", "expired")


# Which lane a participant acts from. The lane is part of the key because the machine
# owner's agent and the machine owner's terminal are the SAME account and two different
# actors: without it they collapse into one member, and "send my agent into the room"
# cannot be told from "I am in the room myself".
#
# "remote" is a connection arriving over the network. It exists so a peer on the wire
# can never derive the local agent's or the local terminal's handle no matter what it
# presents - landing on the agent's seat would put a stranger's words where the owner's
# own agent speaks from.
PARTICIPANT_LANES = ("agent", "cli", "remote")


def participant_key(lane: str, scope_id: Optional[str] = None) -> str:
    """The local identity a room handle is derived from.

    One home for it, because three places used to build this string by hand - the
    agent's room tools, the wake-up in the agent loop, and the CLI - and three copies
    of an identity derivation is three chances for two of them to disagree about who
    is speaking.
    """
    if lane not in PARTICIPANT_LANES:
        raise RoomError(f"unknown participant lane {lane!r}; expected one of {PARTICIPANT_LANES}")
    scope = str(scope_id or "")
    if not scope:
        from vaf.core.config import get_local_admin_scope_id
        scope = str(get_local_admin_scope_id() or "local")
    return f"{lane}:{scope}"


#: How a running agent hands its lane down to the shells it starts, bound to ONE room.
#:
#: A shell inherits the agent's environment, and `vaf a2a` answers as the machine owner
#: by design - so an agent that reached for a shell command instead of its own tool wrote
#: under its USER's handle, and the room recorded the agent's work as the person's.
#:
#: The room id travels with the key because a shell can outlive the turn that started it
#: (a coder subprocess does): elsewhere the value simply does not match and the ordinary
#: answer stands. It grants nothing - whoever can set it can already run `vaf`.
ROOM_ACTOR_ENV = "VAF_A2A_ROOM_ACTOR"


def room_actor_value(room_id: str, key: str) -> str:
    """The hand-down as it is written into the environment.

    Format and parse live next to each other on purpose: the writer is the agent runner
    and the reader is the CLI, two processes that share nothing but this string, and one
    of them changing the separator alone is a silence, not an error.
    """
    return f"{str(room_id or '').strip()}|{str(key or '').strip()}"


def resolve_room_actor(room_id: str, environ: Optional[dict] = None) -> str:
    """The handed-down participant key for this room, or "" - never a guess.

    Empty for any other room, which is what keeps a stale hand-down from speaking where
    it has no business. The caller falls back to its ordinary identity on "".
    """
    import os as _os

    raw = str((environ if environ is not None else _os.environ).get(ROOM_ACTOR_ENV) or "").strip()
    room = str(room_id or "").strip()
    if not raw or not room or "|" not in raw:
        return ""
    where, _, key = raw.partition("|")
    return key.strip() if where.strip() == room and key.strip() else ""


def contribution_count(frames: List[Frame]) -> int:
    """How many times somebody actually SAID something, as opposed to frame count.

    Anything that paces itself by "messages" must count with this, never with
    ``len(frames)``. A room produces protocol bookkeeping alongside every visible
    contribution - pings to quiet members, joins, role changes, vote tallies -
    and that bookkeeping grows with the number of participants while the
    conversation does not. Measured on a live three-voice room: the memory
    compaction interval of "every 15 messages" fired every ~2.5 of the owner's
    messages, because it counted frames. With thirty agents a frame counter
    would not drift, it would collapse: 15 frames would arrive inside a single
    exchange.

    The counted kinds are the ones the transcript renders as speech. A vote's
    question and a ballot carry content too, but they are already summarised by
    their tally frame; counting all three would count one decision thrice.
    """
    speech = {"say", "ask", "answer", "report"}
    return sum(1 for f in frames if f.kind in speech)


def transcript(frames: List[Frame], *, labels: Dict[str, str],
               max_chars: int = 12000) -> str:
    """The room's conversation as plain lines, newest kept, for something to LEARN from.

    A second rendering of the same frames, deliberately not the one a room turn shows an
    agent. That one carries frame ids, addressing hints and "read along, do not answer"
    markers, because it is routing information for the turn it belongs to. None of that
    is a fact about the world, and a memory that swallowed it would remember the
    plumbing instead of the conversation.

    Every line names its SPEAKER. A two-party chat can get away with "User:" and
    "Assistant:" because the roles are the whole cast; in a room the same fact means
    something different depending on who said it, and half the speakers are agents
    nobody here controls.

    Bookkeeping and pings are left out - a join, an ack or a "still on this?" is the
    room talking about itself, not about the world. Oldest lines are dropped first when
    the budget runs out, so the tail that survives is the part still being talked about.
    """
    skip = NON_CONVERSATION_KINDS
    lines: List[str] = []
    total = 0
    for frame in reversed(list(frames or [])):
        if frame.kind in skip:
            continue
        text = str((frame.body or {}).get("text") or "").strip()
        if not text:
            continue
        who = labels.get(frame.sender) or frame.sender
        shared = ", ".join(f["path"] for f in attached_files(frame.body))
        line = f"{who}: {text}".replace("\n", " ").strip()
        if shared:
            # A file somebody left in the room is a fact about the work, and a
            # memory that dropped it would remember the sentence and lose the
            # thing the sentence was about.
            line = f"{line} [shared: {shared}]"
        if total + len(line) + 2 > max_chars:
            break
        lines.append(line)
        total += len(line) + 2
    lines.reverse()
    return "\n\n".join(lines)


def owner_tenant(value: Optional[str]) -> str:
    """A room's owning TENANT, healing a participant key that was stored as one.

    `vaf a2a create` recorded `participant_key("cli")` where a tenant belongs - one
    prefix apart, invisible until something derived from it. A room written that way
    has host handles nobody holds, so it has NO host: its own opener cannot close it
    and cannot remove anybody, and the tenant check compares two strings that can never
    match.

    Healed on READ rather than by rewriting manifests, because the rooms already on
    disk belong to conversations that are still going and a migration pass over
    somebody's live rooms is a worse answer than a strip() at the one place that asks.
    """
    text = str(value or "").strip()
    for lane in PARTICIPANT_LANES:
        if text.startswith(f"{lane}:"):
            return text[len(lane) + 1:]
    return text


def derive_peer_id(key: str, room_id: str) -> str:
    """The room-local handle one participant always gets in one room.

    Derived rather than stored, for three reasons: it survives a restart with no
    index to keep in sync, a re-join lands on the same handle so a peer does not
    accumulate ghosts of itself, and the SAME participant gets a DIFFERENT handle in
    every room, so reading two transcripts cannot correlate them.

    ``key`` is whatever identifies the participant locally - a tenant's scope for a
    VAF agent, the local admin's for the terminal. It is hashed, so it never appears
    in a frame that every member of the room can read.
    """
    digest = hashlib.blake2s(
        f"{key}:{room_id}".encode("utf-8"), digest_size=8
    ).hexdigest()
    return "p-" + digest[:10]


def peer_tag(peer_id: str, *, width: int = 2) -> str:
    """A short, stable number for a peer: the "51" in "Codex51".

    Derived from the handle rather than counted, so it is the same after a restart and
    needs nothing written down. Digits rather than letters because a human reads them
    back to somebody else, and "Codex51" survives being said out loud in a way that
    "CodexA7" does not.
    """
    digest = hashlib.blake2s(str(peer_id or "").encode("utf-8"), digest_size=8).hexdigest()
    return str(int(digest[:8], 16) % (10 ** width)).zfill(width)


class RoomError(Exception):
    """Base for a refusal that is about the room's rules, not its files."""


class NotPermitted(RoomError):
    """The peer's role does not allow this kind of frame."""


class WrongRoomKind(RoomError):
    """The frame is not meaningful in this kind of room.

    A `directive` in a `round` lands here. "Nobody commands" is enforced at ingest or
    it is not a rule, only etiquette.
    """


class NotAMember(RoomError):
    """The acting identity has not joined this room."""


class BudgetExceeded(RoomError):
    """The hiring budget (depth or children) is spent. Refused, never silent."""


class TicketInvalid(RoomError):
    """A join ticket is unknown, spent, expired, or minted for another room."""


class MalformedContent(RoomError):
    """A submission is the wrong SHAPE, before any rule about it applies.

    Separate from the refusals above because it answers a different question. Those
    say "you may not"; this says "I cannot tell what you sent". It is a `RoomError`
    so every door already handles it: the hub answers `ack{status:"refused"}`, the
    CLI exits 2, and the agent's tool hands the sentence back to the model.

    Before it existed, a field like `ext: "x"` reached `dict(value)` and raised a
    bare ValueError past `Hub.submit`, which catches `RoomError` and nothing else.
    On the socket that ended the receive loop: the peer lost its connection and
    never got an ack for the frame it had just sent, which is the one failure a
    protocol built on acknowledged writes cannot afford to have.
    """


# The lanes whose key material is the HOST'S OWN, and therefore the only ones the
# host may sign for. `agent` and `cli` are the machine owner's two actors: their keys
# come out of this machine's keyring because they ARE this machine.
#
# `remote` is deliberately absent, and it is the whole point rather than an omission.
# A remote peer's key would be derived here, from this machine's root secret, so a
# signature made for it would say "the host wrote this under that peer's handle" while
# reading as "that peer wrote this". Against a dishonest host that is worth nothing,
# and it is worse than nothing: it would make `valid` mean less than it says on the one
# lane the feature exists for. A remote peer signs by PRESENTING its own signature, or
# its frames stay unsigned, which is honest and is what they were before.
SELF_SIGNING_LANES = ("agent", "cli")


def _may_self_sign(participant_key: str) -> bool:
    """Whether this machine may sign on that participant's behalf."""
    return str(participant_key).split(":", 1)[0] in SELF_SIGNING_LANES


def _published_key(participant_key: str, room_id: str) -> Optional[str]:
    """This participant's public signing key in this room, or None if there is none.

    NEVER raises. Signing is optional in both directions, so a machine whose keyring
    cannot be opened, or an install without the crypto library, keeps rooms working
    exactly as before rather than failing to join one. A missing key is the status
    quo; an exception here would be a regression for everybody who never asked for a
    signature.
    """
    try:
        from vaf.core.a2a import signing
        return signing.public_key(participant_key, room_id)
    except Exception:
        return None


def _sign_content(participant_key: str, room_id: str, sender: str,
                  content: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """A signature over what the room is about to store, or None. NEVER raises.

    The content handed in is `compose`'s output, which is exactly what `ingest` goes
    on to write. That is the whole reason compose is a fixed point: without it this
    would be signing a draft.
    """
    try:
        from vaf.core.a2a import signing
        payload = signing.covered_payload(room_id, sender, content)
        return signing.sign(payload, participant_key=participant_key, room_id=room_id)
    except Exception:
        return None


def content_signature(frame: "Frame", room_id: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Whether a frame's own signature really covers its own content, HERE.

    `room_id` is the room doing the reading, and it is a parameter rather than
    `frame.room` because the frame's own room field is exactly the thing an attacker
    supplies. Trusting it made a whole conversation portable: copy the files from one
    room's store into another's and every frame still read `valid`, because the
    verifier rebuilt the covered payload out of the room id the frame claimed. The
    door never had this bug - `_settle_signature` has always used the room's own id -
    so the two halves of one check disagreed about which room they were in.

    ONE implementation, because two questions need this answer and they must not be
    able to disagree: what a reader may conclude about a frame (`verdict_for`), and
    whether a `join` really attests the key it publishes (`signing_keys`). Answering
    it twice is how the two could drift into judging the same frame differently.

    Four answers, and NONE of them is about whose key it is - that is deliberately
    the caller's second question:

    - `unsigned`: nothing was claimed.
    - `unreadable`: a `sig` this peer cannot parse, or crypto this install lacks.
    - `invalid`: a signature that does not cover this content.
    - `signed`: it does, by the key it names, which comes back alongside.

    NEVER raises. `transcript()` folds keys with nothing catching it, and an install
    without the crypto library has to keep rooms working exactly as they did before
    signatures existed at all.
    """
    if not frame.sig:
        return "unsigned", None
    try:
        from vaf.core.a2a import signing
        read = signing.read_signature(frame.sig)
        if read is None:
            return "unreadable", None
        content = {field: getattr(frame, field) for field in signing.COVERED}
        if not signing.verify(signing.covered_payload(room_id, frame.sender, content),
                              read):
            return "invalid", None
    except Exception:
        return "unreadable", None
    return "signed", read


def content_object(value: Any, name: str) -> Dict[str, Any]:
    """A submitted field that must be an object, refused the way a room refuses.

    The rule about what a frame field may BE belongs to the frame module and is
    implemented once there. What differs here is who is being told: a caller minting
    a `Frame` is holding a programming error and gets `MalformedFrame`, while a peer
    that just submitted this over a socket is holding a bad message and gets a
    `RoomError` every door already answers.
    """
    try:
        return object_field(value, name)
    except MalformedFrame as e:
        raise MalformedContent(str(e)) from None


class RoomClosed(RoomError):
    """The room has been closed. It stays readable; it accepts nothing more.

    Its own kind rather than a plain refusal, because a caller usually wants to say
    something different about it: a refused directive is a mistake to correct, a closed
    room is a conversation that is over.
    """


class Identity:
    """Who is acting, as the ROOM sees them.

    ``scope_id`` is the VAF tenant when the peer is one of ours, and None for a
    foreign agent that has no account here. It never travels in a frame: a scope UUID
    identifies a tenant, and every member of a room can read every frame in it.

    ``participant_key`` is the lane-and-scope pair this peer was derived from, and it
    is what lets the room SIGN on that peer's behalf: the signing key comes out of the
    same two inputs as the handle. It is None for a guest, which has no account here
    and therefore no key on this machine, and None is not an error - it means this
    peer's frames go out unsigned, which is what they have always done.
    """

    __slots__ = ("peer_id", "display", "scope_id", "role", "participant_key")

    def __init__(self, peer_id: str, display: str, scope_id: Optional[str], role: str,
                 participant_key: Optional[str] = None) -> None:
        self.peer_id = check_name(peer_id, what="peer id")
        self.display = str(display or peer_id)
        self.scope_id = str(scope_id) if scope_id else None
        self.role = role
        self.participant_key = str(participant_key) if participant_key else None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Identity({self.peer_id!r}, {self.display!r}, role={self.role!r})"


class Room:
    """One room: its manifest, its members, and the rules for what may be said."""

    def __init__(self, store: RoomStore) -> None:
        self.store = store
        manifest = store.manifest()
        if manifest is None:
            raise StoreError(f"room {store.room_id!r} does not exist")
        self.manifest = manifest

    # ── lifecycle ───────────────────────────────────────────────────────────

    @property
    def room_id(self) -> str:
        return self.store.room_id

    @property
    def kind(self) -> str:
        return str(self.manifest.get("kind") or "round")

    @property
    def closed(self) -> bool:
        return any(f.kind == "close" for f in self.store.frames())

    @classmethod
    def create(
        cls,
        *,
        kind: str = "round",
        owner_scope: Optional[str] = None,
        topic: str = "",
        mission: str = "",
        base: Optional[Path] = None,
        room_id: Optional[str] = None,
        backlog: str = "",
        depth: int = 0,
        parent_room: Optional[str] = None,
        parent_frame: Optional[str] = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_children: int = DEFAULT_MAX_CHILDREN,
        multi_scope: bool = False,
        tenants: Optional[Iterable[str]] = None,
    ) -> "Room":
        if kind not in ROOM_KINDS:
            raise RoomError(f"unknown room kind {kind!r}; expected one of {ROOM_KINDS}")
        store = RoomStore(room_id or new_room_id(), base=base)
        store.create({
            "kind": kind,
            "topic": str(topic or ""),
            # What the room is FOR, at more length than a title. A topic fits in a
            # sidebar row; this is the paragraph an agent is reminded of when it
            # comes back after an hour and has to decide what is worth saying. It
            # lives in the manifest for the same reason the topic does - it is a
            # property of the room, not something somebody said - which means it
            # reaches a remote peer with the welcome or the next check-in rather
            # than the instant it changes. Named boundary, not an oversight: a
            # frame for it would put mutable state into a write-once transcript.
            "mission": str(mission or "")[:2000],
            "owner_scope": str(owner_scope) if owner_scope else None,
            # WHAT A NEW MEMBER MAY READ. "all" everywhere a room holds one account,
            # because an agent invited into a conversation has to be able to catch up -
            # that is what the invitation asks it to do, and it is what this has always
            # done in practice. A room that admits SEVERAL accounts starts a newcomer
            # at its own join instead: the history there belongs to other people, and
            # handing it over on arrival is a leak dressed as a courtesy.
            "backlog": backlog or ("since_join" if multi_scope else "all"),
            "depth": int(depth),
            "parent_room": parent_room,
            "parent_frame": parent_frame,
            "max_depth": int(max_depth),
            "max_children": int(max_children),
            # Cross-tenant rooms are deliberately off. subagent_ipc records what a
            # shared record carrying model text across tenants costs here.
            "multi_scope": bool(multi_scope),
            # And when they are on, they still admit only the accounts named here.
            # The room id is not a secret; the guest list is the door.
            "tenants": [t for t in (owner_tenant(x) for x in (tenants or [])) if t],
        })
        return cls(store)

    @classmethod
    def open(cls, room_id: str, *, base: Optional[Path] = None) -> "Room":
        return cls(RoomStore(room_id, base=base))

    def update(self, **fields: Any) -> Dict[str, Any]:
        """Change the manifest through the room, so the in-memory copy cannot drift.

        The manifest is read once and kept, because ``kind`` is consulted on every
        single ingest and re-reading it would mean decrypting a file per message.
        The cost of that choice is exactly this method: a write that went straight to
        the store would leave this object describing a room that no longer exists
        that way.
        """
        self.manifest = self.store.update_manifest(**fields)
        return self.manifest

    # ── membership ──────────────────────────────────────────────────────────

    def default_role(self) -> str:
        """The role a new member gets. In a round nobody commands, so everyone is a
        peer; in a chain the first member leads and the rest work."""
        if self.kind == "round":
            return "peer"
        return "leader" if not self.roles() else "worker"

    def join(
        self,
        *,
        display: str,
        peer_id: Optional[str] = None,
        scope_id: Optional[str] = None,
        card: Optional[Dict[str, Any]] = None,
        mode: str = DEFAULT_MODE,
        role: Optional[str] = None,
        participant_key: Optional[str] = None,
    ) -> Identity:
        """Admit a peer and record the join in the log.

        ``role`` is a request, honoured only where the room's own rule allows it.
        Whatever a ``card`` claims is displayed, never believed: the card is a self
        description for humans and for a leader choosing workers, and it has no say
        in the fold that decides roles.

        ``participant_key`` is the lane-and-scope pair the handle was derived from. Given
        one, the join PUBLISHES this peer's public signing key BESIDE the card, which is
        what later makes its signatures checkable by anybody holding the transcript.

        Unlike the card, the key is not merely self-asserted: the join carrying it is
        SIGNED BY THAT KEY, and `signing_keys` counts no announcement that is not. A
        card claims and is believed by nobody; a key claims and proves possession in
        the same frame, which is what stops the announcement being forged into somebody
        else's lane. If the body cannot be signed the card gives way rather than the
        key, because a join with no key is how a peer withdraws one.

        A peer with no key here simply publishes none and keeps sending unsigned frames.
        """
        if self.closed:
            raise RoomError(f"room {self.room_id!r} is closed")
        self._check_tenant(scope_id)
        if mode not in ROOM_MODES:
            raise RoomError(f"unknown room mode {mode!r}; expected one of {ROOM_MODES}")

        resolved = self.default_role()
        if role and role in CAPABILITIES and self.kind == "chain" and not self.roles():
            resolved = role
        identity = Identity(peer_id or new_peer_id(), display, scope_id, resolved,
                            participant_key=participant_key)
        card = dict(card or {})
        # BESIDE the card, never inside it. A card answers "what can this peer do",
        # and every surface that asks whether a member has introduced itself reads
        # "is the card empty". A key is not self-description, it is how you check
        # what the peer says, and putting it in the card made a peer that had said
        # nothing about itself look as though it had.
        published = (_published_key(participant_key, self.room_id)
                     if participant_key and _may_self_sign(participant_key) else None)

        # An existing member's mode is NEVER overwritten by a join. The mode is the
        # local user's standing decision about how far their own agent may act, and a
        # join is the one operation a remote party can cause: letting it carry a mode
        # would hand a stranger the switch. set_mode is the only way it changes, and it
        # is reachable from the local lanes alone.
        existing = self.store.member(identity.peer_id) or {}
        self.store.put_member(identity.peer_id, {
            "display": identity.display,
            "mode": str(existing.get("mode") or mode),
            "lease": time.time(),
            "card": dict(card),
        })
        body: Dict[str, Any] = {"display": identity.display, "card": dict(card)}
        if published:
            # PUBLISH ONLY A KEY THIS JOIN CAN ATTEST. A reader believes a published
            # key because the join carrying it is signed by that same key, so a join
            # that publishes one it cannot sign publishes nothing usable - and worse
            # than nothing, because this peer's own later frames would then read
            # `foreign_key`, which accuses somebody. The two conditions were decided
            # in two places and could disagree: deriving the key needs only the
            # keyring, while signing needs the body to be canonical, and a card
            # holding a fractional number is not. Measured with `card={"load": 0.75}`.
            # One extra signature per join, once, is the price of them agreeing.
            # WITHOUT THE CARD IF THE CARD IS WHAT BLOCKS IT. Deriving the key needs
            # only the keyring, while signing needs the body to be canonical, and a
            # card holding a fractional number is not - no two languages print every
            # float alike. Publishing the key unsigned was not an option: it binds
            # nothing, so every later frame this peer signs would read `foreign_key`,
            # the verdict that points at somebody. Publishing NOTHING was worse still,
            # because a join with no key is how a peer WITHDRAWS one, so an odd card
            # would have retracted a binding the peer never meant to give up. So the
            # card, which is self-description and is kept in the member record either
            # way, is what gives.
            for shape in (dict(body) | {"sign_key": published},
                          {"display": identity.display, "sign_key": published}):
                try:
                    probe = {"kind": "join", "to": {"room": True}, "body": shape}
                    if _sign_content(identity.participant_key or "", self.room_id,
                                     identity.peer_id, self.compose(probe)):
                        body = shape
                        break
                except Exception:
                    continue
        joined = self.ingest(
            {"kind": "join", "to": {"room": True}, "body": body},
            identity=identity,
        )
        # WHAT A NEW MEMBER MAY READ, honoured here rather than promised in the
        # manifest and forgotten. `since_join` puts the newcomer's cursor on its own
        # join, so it reads the room from its arrival; `all` leaves the cursor at zero
        # and it reads everything, which is what a chain wants - work handed down needs
        # the thread that led to it.
        #
        # Only for a member that is actually NEW: a rejoin keeps the position it had,
        # or every reconnect of a peer with a flaky wire would silently swallow
        # whatever arrived while it was away.
        if not existing and str(self.manifest.get("backlog") or "") == "since_join":
            try:
                self.store.set_cursor(identity.peer_id, joined.lamport)
            except Exception:
                pass
        return identity

    def _check_tenant(self, scope_id: Optional[str]) -> None:
        """A room belongs to one tenant unless it says otherwise.

        A foreign agent carries no scope at all and is not a tenant, so it is not
        caught here; what bounds it is the ticket, which opens exactly one room.
        """
        owner = owner_tenant(self.manifest.get("owner_scope"))
        scope_id = owner_tenant(scope_id)
        if not (owner and scope_id) or scope_id == owner:
            return
        if self.manifest.get("multi_scope"):
            # A cross-account room admits the accounts it ADMITTED, not everyone who
            # can name it. Without this list, `multi_scope` would mean that an agent
            # told a room id inside a room message could walk into a conversation
            # belonging to five other people - the room id is not a secret and was
            # never designed to be one.
            if scope_id in self.tenants():
                return
            raise NotAMember(
                f"room {self.room_id!r} did not admit this account; a cross-account "
                f"room lists the accounts it takes"
            )
        raise NotAMember(
            f"room {self.room_id!r} belongs to another account; cross-account "
            f"rooms are off by default"
        )

    def host_peers(self) -> frozenset:
        """The handles belonging to the account that owns this room.

        DERIVED, never stored, and that is what makes it safe to decide anything on:
        a member file is written by the member, so a peer that could name itself here
        would be naming its own protection. These come out of the same derivation the
        owner's own lanes use, so nobody else can land on one.

        They are the peers that cannot be removed from the room. Getting rid of the
        machine owner's own agent is not a membership operation - it is closing the
        room, which takes everybody out at once and is the honest way to say it.
        """
        owner = owner_tenant(self.manifest.get("owner_scope"))
        if not owner:
            return frozenset()
        return frozenset(
            derive_peer_id(participant_key(lane, owner), self.room_id)
            for lane in PARTICIPANT_LANES
        )

    def tenants(self) -> List[str]:
        """Every ACCOUNT this room admits, owner first.

        A room belongs to one account unless it says otherwise, and "otherwise" is not
        "anybody who learns the room id" - that would let an agent join a room whose id
        it was told IN A ROOM MESSAGE. So a cross-account room carries the list of the
        accounts it admitted, and that list is the door.

        It lives in the manifest rather than in the member files for the reason every
        other decision here follows: a member file is written by the member, so an
        account that could write itself in would be admitting itself.
        """
        owner = owner_tenant(self.manifest.get("owner_scope"))
        admitted = [owner_tenant(t) for t in (self.manifest.get("tenants") or [])]
        return list(dict.fromkeys([t for t in [owner] + admitted if t]))

    def admit(self, identity: Identity, tenant: str) -> List[str]:
        """Let another ACCOUNT into this room. Host or leader only.

        The counterpart to the door in `_check_tenant`: a room shared across accounts
        takes the accounts it was told to take, and this is where it is told. Written
        to the manifest, which only the room writes - an account that could add itself
        to a member file would be admitting itself.

        Refused on a room that holds one account rather than quietly turning it into a
        shared one: opening a room that every member reads is a decision somebody makes
        deliberately, not a side effect of inviting one more person.
        """
        self._check_may_admit(identity)
        wanted = owner_tenant(tenant)
        if not wanted:
            raise RoomError("name the account to let in")
        self._admit_tenant(wanted)
        return self.tenants()

    def _check_may_admit(self, identity: Identity) -> None:
        """The one rule for letting another account in, shared by the direct door
        (`admit`) and the consenting one (`invite_account`): host or leader, and
        only into a room that was deliberately opened to other accounts."""
        if not (self.is_host(identity) or self.role_of(identity.peer_id) == "leader"):
            raise NotPermitted(
                "only the room's host or its leader lets another account in")
        if not self.manifest.get("multi_scope"):
            raise RoomError(
                f"room {self.room_id!r} holds one account; open a shared room to let "
                "other accounts in")

    def _admit_tenant(self, tenant: str) -> None:
        """Write one account onto the guest list. Permission is the CALLER's question."""
        current = [t for t in (self.manifest.get("tenants") or []) if t]
        if tenant not in current and tenant != owner_tenant(self.manifest.get("owner_scope")):
            current.append(tenant)
            self.store.update_manifest(tenants=current)
            self.manifest["tenants"] = current

    def open_to_accounts(self, identity: Identity) -> Dict[str, Any]:
        """Turn a one-account room into one other accounts may be invited into.

        Host only, and a separate act rather than a side effect of the first
        invitation: every member of a shared room reads everything said in it, so
        this is the decision, and the invitation that follows is only who. A room
        that is already shared is left as it is.

        Newcomers to a shared room start reading at their own join (`backlog`),
        which `Room.create` decides at creation; a room opened up later has to
        make the same decision here, or the first invited account would receive
        the whole history of a conversation it was never part of.
        """
        if not self.is_host(identity):
            raise NotPermitted("only the room's host opens it to other accounts")
        if self.manifest.get("multi_scope"):
            return self.manifest
        return self.update(multi_scope=True, backlog="since_join")

    def household_peers(self, tenant: str) -> Dict[str, str]:
        """The handles ONE account holds in this room, by lane.

        The same derivation `host_peers` uses, kept apart from it because they answer
        different questions: that one asks "may this peer be removed", this one asks
        "which seats belong together".
        """
        tenant = owner_tenant(tenant)
        if not tenant:
            return {}
        return {lane: derive_peer_id(participant_key(lane, tenant), self.room_id)
                for lane in PARTICIPANT_LANES}

    def pairs(self, *, tenants: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
        """Which member is a person, which is an agent, and who belongs to whom.

        DERIVED, never believed - and that is the whole point. A member file is
        written by the member, so a `speaks_for` field in one would be a peer naming
        its own partner; anyone could claim to be somebody's user. Here the room
        RECOMPUTES each handle from an account it knows (`derive_peer_id` over lane
        and tenant) and accepts the pair only when the handle comes out identical.
        A stranger cannot produce that handle without the tenant's scope id, and the
        scope id appears in no frame.

        What it therefore CANNOT answer: a guest that redeemed a ticket carries no
        tenant at all (`scope_id=None`, a randomly minted handle), so no derivation
        reaches it. Those pairs come from the invitation instead - one ticket that
        seats two - and are marked `proof: "invitation"` by that path, never by this
        one.

        `tenants` defaults to the accounts the ROOM admits (`Room.tenants()`), which
        is the whole list for a single-tenant room and the guest list for one across
        tenant lines. It comes from the manifest, which only the room writes - so
        every member can be told about every pair without anything here reading a user
        store, and without a member having any say in it.

        The most common answer on a fresh room is "no partner": a person becomes a
        member only when they first act in the room, so an agent sitting there alone
        is the normal starting state and not a fault to report.
        """
        members = self.members()
        labels = self.labels()
        wanted = list(tenants) if tenants is not None else self.tenants()
        out: Dict[str, Dict[str, Any]] = {
            peer: {"peer": peer, "kind": "unknown", "partner": "",
                   "partner_label": "", "proof": ""}
            for peer in members
        }
        for tenant in dict.fromkeys(t for t in (owner_tenant(x) for x in wanted) if t):
            handles = self.household_peers(tenant)
            person = handles.get("cli") or ""
            agent = handles.get("agent") or ""
            # The lane says WHAT a seat is; the match against a known account is what
            # makes saying it safe.
            if person in out:
                out[person].update(kind="human", proof="derived")
            if agent in out:
                out[agent].update(kind="agent", proof="derived")
            if person in out and agent in out:
                out[person].update(partner=agent,
                                   partner_label=labels.get(agent) or agent)
                out[agent].update(partner=person,
                                  partner_label=labels.get(person) or person)
        return out

    def is_host(self, identity: "Identity") -> bool:
        """Whether this participant is the account whose machine holds the room.

        Deliberately keyed on the TENANT and not on the role: an invited agent has no
        scope at all (a redeemed ticket sets it to None), so no guest can ever be the
        host no matter what it presents, while the same account arriving from another
        of its own machines still is. That is the same line `_check_tenant` already
        draws, read the other way round.
        """
        hosts = self.host_peers()
        # The HANDLE decides, not the scope carried on the identity object. Both answer
        # the same question, but only one of them is always there: `identity_for` builds
        # an Identity from the log and leaves scope_id None, so every caller that looked
        # a member up rather than joining them got False - the browser among them, which
        # is how the host of a room stayed unable to close or clear it after two rounds
        # of fixing exactly that. A handle cannot be presented by anybody else: a guest's
        # is minted at random and a derived one needs the owner's own tenant.
        if hosts and getattr(identity, "peer_id", None) in hosts:
            return True
        owner = owner_tenant(self.manifest.get("owner_scope"))
        scope = owner_tenant(getattr(identity, "scope_id", None))
        return bool(owner) and bool(scope) and scope == owner

    # What every participant sees when a human ends the room. One wording, because a
    # closing reason is the last thing anybody reads in a transcript and three
    # surfaces would otherwise each phrase it their own way.
    TERMINATED_BY_USER = "This chat has been terminated by the user or Host AI system."

    def introduce(self, identity: "Identity", *, display: str = "",
                  card: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Update what a member says about itself, after it has already joined.

        The card used to be writable only at JOIN, so anybody who arrived without one
        was stuck reading "said nothing about what it can do" forever - and a room is
        agents deciding who to ask. The same applied to the name: a peer that joined as
        "terminal" could never become the person behind it.

        Writes ONLY this peer's own member file, which is the property the whole store
        rests on. It is self-description either way: a card here changes no role, and
        the role fold does not look at this file.
        """
        record = self.store.member(identity.peer_id) or {}
        if not self.role_of(identity.peer_id):
            raise NotAMember(f"{identity.peer_id!r} has not joined room {self.room_id!r}")
        if display:
            record["display"] = str(display)[:80]
        if card:
            record["card"] = dict(card)
        record.setdefault("lease", time.time())
        self.store.put_member(identity.peer_id, record)
        return record

    def heartbeat(self, identity: "Identity") -> None:
        """Refresh this peer's lease. Only ever writes the peer's OWN file."""
        record = self.store.member(identity.peer_id) or {}
        record["lease"] = time.time()
        self.store.put_member(identity.peer_id, record)

    def members(self) -> Dict[str, Dict[str, Any]]:
        """Members with their resolved role and liveness.

        A lapsed lease makes a peer ``stale``, never ``gone``: a sleeping laptop is not
        a departure. Membership ends in exactly two ways, both of them frames somebody
        wrote on purpose - a ``leave`` from the peer itself, or a ``kick`` from a leader
        or the host.
        """
        roles = self.roles()
        now = time.time()
        out: Dict[str, Dict[str, Any]] = {}
        for peer_id, record in self.store.members().items():
            if peer_id not in roles:
                continue
            lease = float(record.get("lease") or 0.0)
            out[peer_id] = {
                "display": record.get("display") or peer_id,
                "role": roles[peer_id],
                "card": record.get("card") or {},
                "stale": (now - lease) > LEASE_TTL_S,
            }
        return out

    def open_vote(self, identity: Identity, question: str, *,
                  options: Optional[List[str]] = None,
                  closes_in_s: Optional[float] = None, **kw) -> Frame:
        """Put a question to the room. Any member may, in any role.

        A vote is a QUESTION and never an instruction, which is why it is not a
        leader's privilege: twenty agents that may only be asked cannot decide
        anything together, and a room of equals has nobody to ask permission
        from. What a role governs is what a peer may EMIT - and asking is
        something every role already does.

        Options are free text and default to yes/no. A ballot is an ordinary
        `answer` with `reply_to` pointing here, so nothing new had to be invented
        for "this answers that", and an implementation that has never heard of
        `vote` still shows the question and the answers to it (rule 2).
        """
        body = {"text": str(question or "").strip()[:400],
                "options": list(options or [])}
        if closes_in_s and closes_in_s > 0:
            # Advisory, like every wall clock in this protocol: a reader marks a
            # vote closed by comparing it, and nothing is ever refused because of
            # it - clocks differ between the machines in a room.
            body["closes_at"] = time.time() + float(closes_in_s)
        return self.ingest({"kind": "vote", "body": body, **kw}, identity=identity)

    def cast(self, identity: Identity, vote_id: str, choice: str,
             *, comment: str = "") -> Frame:
        """Cast a ballot: an answer to the vote, carrying the choice.

        The LAST ballot a peer casts is the one that counts, the same rule the
        task board uses for status - changing your mind is a thing that happens,
        and a write-once log cannot take anything back anyway.

        The choice is RESOLVED against the vote's options rather than stored as
        typed. Measured in the first live vote: an agent offered "ja, weiter so"
        and "erst schlafen" answered "ja", which is unmistakable to a human and
        became its own third column in the tally. Matching is case-insensitive
        and accepts an unambiguous prefix, because agents shorten; anything that
        matches nothing is REFUSED with the options named, because a machine peer
        reads that refusal and can retry, while a silently accepted invention
        turns the count into noise nobody notices.

        The resolution that COUNTS happens in `compose`, which every ballot crosses
        whatever lane it came from. This method ASKS compose what will be stored, so
        that the sentence it writes names the option the room is about to record
        rather than a second opinion about it. Handing the answer back is free:
        compose is a fixed point, so composing an already-composed choice returns it
        unchanged.
        """
        resolved = self.compose({"kind": "answer", "reply_to": vote_id,
                                 "body": {"choice": choice}})["body"]["choice"]
        return self.ingest({
            "kind": "answer", "reply_to": vote_id,
            "body": {"text": comment or f"votes: {resolved}", "choice": resolved},
        }, identity=identity)

    def _vote_options(self, vote_id: str) -> List[str]:
        """The options of the vote this ballot answers, or [] when the id names
        something else - a ballot on a message that is not a vote is a mistake the
        fold ignores anyway, and refusing it here would be a second place to say
        so."""
        for frame in self.store.frames():
            if frame.id == vote_id and frame.kind == "vote":
                return self._trimmed_options((frame.body or {}).get("options"))
        return []

    @staticmethod
    def _resolve_choice(choice: str, options: List[str]) -> str:
        """One of the options, or a refusal naming them."""
        lowered = choice.casefold()
        for option in options:
            if option.casefold() == lowered:
                return option
        starts = [o for o in options if o.casefold().startswith(lowered)] if lowered else []
        if len(starts) == 1:
            return starts[0]
        raise RoomError(
            f"{choice!r} is not one of this vote's options ("
            + ", ".join(repr(o) for o in options)
            + "). Vote with one of them, or say what you think in a message."
        )

    def votes(self) -> List[Dict[str, Any]]:
        """Every vote in this room, folded with its ballots.

        The fold itself is a free function over frames (`fold_votes`), for the
        same reason the task board is: a peer reading this room over the wire has
        frames and no store, and two folds would be two opinions about who
        abstained.
        """
        return fold_votes(self.store.frames(),
                          labels=self.labels(),
                          members=list(self.members().keys()))

    def task_nudges(self, *, now: Optional[float] = None
                    ) -> List[Tuple[str, Dict[str, Any]]]:
        """Open work nothing has been said about, and who to ask about it.

        Asked ONCE per silence, not once per task and not once per sweep: a nudge that
        repeats every half hour is the nagging the check-in was built to avoid, and a
        nudge that never repeats leaves a task that went quiet twice asked about once.
        The rule that gives both is derived rather than remembered - the room asks
        again only when the task has been reported on SINCE the last time it asked.
        """
        frames = self.store.frames()
        board = fold_tasks(frames, labels=self.labels(), now=now)
        moment = time.time() if now is None else float(now)
        asked: Dict[str, float] = {}
        for frame in frames:
            task_id = str((frame.body or {}).get("task") or "")
            if frame.kind == "ping" and task_id:
                asked[task_id] = max(asked.get(task_id, 0.0), float(frame.ts or 0.0))
        out: List[Tuple[str, Dict[str, Any]]] = []
        for task in board:
            if task["status"] in ("completed", "failed", "rejected", "canceled"):
                continue
            if not task["assignee"] or task["silent_for_s"] < TASK_NUDGE_AFTER_S:
                continue
            last = asked.get(task["id"], 0.0)
            if last and last >= float(task["updated_ts"] or 0.0):
                continue          # already asked, and nothing has happened since
            out.append((task["assignee"], task))
        return out

    def task_nudge_body(self, task: Dict[str, Any], *,
                        now: Optional[float] = None) -> Dict[str, Any]:
        """What the room asks about a task that has gone quiet.

        A question and not a reprimand: the room cannot tell a long run from an
        abandoned one, which is the whole reason it has to ask. Both answers are
        useful and both are cheap - a report saying "still on it" is one line, and so
        is one saying it is dropped.
        """
        moment = time.time() if now is None else float(now)
        silent = int(max(0.0, moment - float(task["updated_ts"] or 0.0)) // 60)
        progress = task.get("progress") or {}
        where = ""
        if "done" in progress and "total" in progress:
            where = f" Last count: {progress['done']} of {progress['total']}."
        text = (
            "Room check-in on one piece of work: nothing has been said about this for "
            f"about {max(1, silent)} minutes.\n"
            f'"{str(task["title"])[:200]}" [{task["status"]}]{where}\n'
            "If it is still running, say where it is - a report on the same task "
            f'(reply_to "{task["id"]}") with progress is enough. If it is finished or '
            "dropped, report that instead, so the board stops showing work nobody is "
            "doing. A long run is not a problem; a silent one cannot be told apart "
            "from an abandoned one, which is why this asks."
        )
        return {
            "text": text,
            # What it is about, and what makes asking once derivable.
            "task": task["id"],
            "state": {"kind": "task_nudge", "task": task["id"],
                      "title": str(task["title"])[:200], "status": task["status"],
                      "silent_minutes": max(1, silent),
                      "progress": task.get("progress")},
        }

    def nudge_task(self, identity: Identity, peer_id: str,
                   task: Dict[str, Any]) -> Frame:
        """Ask ONE member about ONE quiet task. HOST ONLY, like every check-in."""
        if not self.is_host(identity):
            raise NotPermitted("only the machine hosting a room asks about its work")
        if not self.role_of(peer_id):
            raise NotAMember(f"{peer_id!r} is not in room {self.room_id!r}")
        return self.ingest({"kind": "ping", "to": {"peer": peer_id},
                            "body": self.task_nudge_body(task)}, identity=identity)

    def vote_reminders(self, *, now: Optional[float] = None
                       ) -> List[Tuple[str, Dict[str, Any]]]:
        """Who still owes a ballot and has waited long enough to be reminded.

        Once per member per vote, and that "once" is derived rather than
        remembered: the reminder IS a frame in the host's lane naming the vote it
        is about, so a host that restarts mid-vote does not start over, and two
        surfaces asking the same question get the same answer.
        """
        frames = self.store.frames()
        entries = fold_votes(frames, labels=self.labels(),
                             members=list(self.members().keys()), now=now)
        now = time.time() if now is None else float(now)
        already = {((frame.to or {}).get("peer"), (frame.body or {}).get("vote"))
                   for frame in frames
                   if frame.kind == "ping" and (frame.body or {}).get("vote")}
        out: List[Tuple[str, Dict[str, Any]]] = []
        for entry in entries:
            # A vote that is already over is not reminded about - it is concluded.
            if entry["concluded"] or entry["due"] or now < entry["remind_at"]:
                continue
            out.extend((peer, entry) for peer in entry["waiting_peers"]
                       if (peer, entry["id"]) not in already)
        return out

    def vote_reminder_body(self, entry: Dict[str, Any], *,
                           now: Optional[float] = None) -> Dict[str, Any]:
        """What the room tells a member that has not answered a vote.

        Everything needed to answer travels IN the frame - the question, the
        options, both ways to cast, and what silence will mean - for the reason
        `ping_body` gives: the recipient may be a foreign agent that sees none of
        our surfaces and has only this text.

        Both ways to cast are named because the room does not know which kind of
        agent is reading: a VAF agent has the room tools, an invited agent has a
        shell. Naming one would be right half the time.
        """
        now = time.time() if now is None else float(now)
        left = max(0, int(entry["deadline"] - now))
        options = " | ".join(str(o) for o in entry["options"])
        text = (
            "Room vote: you have not answered this one yet.\n"
            f'"{entry["question"]}"\n'
            f"Options: {options}\n"
            f'Cast a ballot with room_send kind "answer", reply_to "{entry["id"]}" '
            'and choice set to one of the options - or, from a shell, '
            f'`vaf a2a ballot {self.room_id} {entry["id"]} "<option>"`.\n'
            f"About {max(1, left // 60)} minute(s) left; a member that has not "
            "answered by then is counted as abstaining. This is a question and "
            "not an order - abstaining is a valid answer, and so is saying in the "
            "room why you would rather not choose."
        )
        return {
            "text": text,
            # The vote this is about, which is also what makes the reminder
            # once-only without anybody remembering anything.
            "vote": entry["id"],
            "state": {
                "kind": "vote_reminder",
                "vote": entry["id"],
                "question": entry["question"],
                "options": list(entry["options"]),
                "deadline": entry["deadline"],
                "seconds_left": left,
                "voted": entry["voted"],
                "waiting_for": list(entry["waiting_for"]),
            },
        }

    def remind_vote(self, identity: Identity, peer_id: str,
                    entry: Dict[str, Any]) -> Frame:
        """Nudge ONE member about ONE open vote. HOST ONLY, like every check-in.

        A `ping` and not a kind of its own: the room already has a frame for
        "talking to one member about its own attention", and it is already the one
        surfaces keep out of the conversation. A second hidden kind would be a
        second thing every renderer has to learn to hide.
        """
        if not self.is_host(identity):
            raise NotPermitted("only the machine hosting a room reminds its members")
        if not self.role_of(peer_id):
            raise NotAMember(f"{peer_id!r} is not in room {self.room_id!r}")
        return self.ingest({"kind": "ping", "to": {"peer": peer_id},
                            "body": self.vote_reminder_body(entry)},
                           identity=identity)

    def tally_body(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """The outcome of a vote, as the room will say it.

        Written as prose AND as data in the same body, the way `ping_body` is: the
        prose is what a person and a foreign agent read in the transcript, the data
        is what a surface counts without parsing a sentence.
        """
        counts = entry["tally"]
        top = [c for c, n in counts.items() if n == max(counts.values())] if counts else []
        winner = top[0] if len(top) == 1 else ""
        # WHO voted for what, not only how many. The names are in the frame either
        # way, and a count without them is the one thing a reader cannot check -
        # which is the same reason ballots are public at all. Capped per option so a
        # room of twenty does not turn one line into a roll call.
        by_choice: Dict[str, List[str]] = {}
        for ballot in entry["ballots"]:
            by_choice.setdefault(ballot["choice"], []).append(ballot["label"])
        parts = []
        for choice, n in counts.items():
            who = by_choice.get(choice) or []
            named = ", ".join(who[:6]) + (f" +{len(who) - 6}" if len(who) > 6 else "")
            parts.append(f"{choice} {n}" + (f" ({named})" if named else ""))
        spread = ", ".join(parts)
        abstained = list(entry["abstained"])
        # The question is QUOTED here, not repeated: a vote may carry four hundred
        # characters, and one live question had listed all eight of its own options
        # in the text - the result read as a wall with the counts hiding at the end.
        # The whole question is two lines up in the transcript for anyone who wants
        # it; this line exists to say how it went.
        question = entry["question"].strip().replace("\n", " ")
        if len(question) > 120:
            question = question[:117].rstrip() + "..."
        if not counts:
            text = f'Vote closed: "{question}" - nobody answered.'
        else:
            text = f'Vote closed: "{question}" - {spread}.'
            if winner:
                text += f" Result: {winner}."
        if abstained:
            text += (" Did not answer in time, counted as abstaining: "
                     + ", ".join(abstained) + ".")
        elif counts:
            text += " Everybody voted."
        return {
            "text": text,
            "vote": entry["id"],
            "question": entry["question"],
            "options": list(entry["options"]),
            "tally": dict(counts),
            "winner": winner,
            "ballots": [{"peer": b["peer"], "label": b["label"], "choice": b["choice"]}
                        for b in entry["ballots"]],
            "abstained": abstained,
            "everyone_voted": bool(entry["everyone_voted"]),
        }

    def conclude_votes(self, identity: Identity, *,
                       now: Optional[float] = None) -> List[Frame]:
        """End every vote that is over, by writing its result into the room. HOST ONLY.

        A vote is over when every member has answered - nobody waits for a clock
        everybody has already beaten - or when its deadline has passed, in which
        case the members that never answered are named as abstaining.

        Single-write intent, not exactly-once: the fold says whether a result frame
        already exists, and the host's own lane is re-read immediately before the
        write, which is cheap because one writer owns one lane. Two host processes
        writing in the same instant would still produce two results; the fold takes
        the last, so the tally stays right and the transcript carries a duplicate
        line. That is the honest limit of a write-once log without a lock, and it
        is the same one the rest of this store lives with.
        """
        if not self.is_host(identity):
            raise NotPermitted("only the machine hosting a room closes its votes")
        written: List[Frame] = []
        entries = fold_votes(self.store.frames(), labels=self.labels(),
                             members=list(self.members().keys()), now=now)
        for entry in entries:
            if not entry["due"]:
                continue
            mine = self.store.frames(peer_id=identity.peer_id)
            if any(f.kind == "tally" and f.reply_to == entry["id"] for f in mine):
                continue
            written.append(self.ingest(
                {"kind": "tally", "reply_to": entry["id"], "to": {"room": True},
                 "body": self.tally_body(entry)}, identity=identity))
        return written

    def set_mission(self, identity: Identity, mission: str) -> str:
        """Say what this room is for, at length. Host or leader only.

        Not a frame, by the same rule the topic follows: it describes the room
        rather than reporting what happened in it, and a renamed purpose must not
        appear in the transcript as something a member said. The peers learn it
        from the welcome they get on joining and from every check-in after that.

        Host OR leader, because in a chain the leader is the one who knows what
        the work is for, while a round has no leader at all and its host is the
        only one who can answer.
        """
        if not (self.is_host(identity) or self.role_of(identity.peer_id) == "leader"):
            raise NotPermitted(
                "only the room's host or its leader says what the room is for")
        text = str(mission or "").strip()[:2000]
        self.store.update_manifest(mission=text)
        self.manifest["mission"] = text
        return text

    def leaders(self) -> List[str]:
        """Peers that lead this room, if any. A round has none by design."""
        return [peer for peer, role in self.roles().items() if role == "leader"]

    def welcome(self, identity: Identity) -> Dict[str, Any]:
        """Everything a peer needs to work here, answered at the moment it joins.

        A join used to answer with a handle and a role, which is the least a room
        could say: the newcomer then had to discover the members, the shared
        folder, what its role may emit and what is already being worked on, one
        command at a time - and a room of twenty peers is precisely where nobody
        does that. This is the room's half of the handshake.

        The other half is the ASK. A peer that says nothing about itself shows up
        in everybody's roster as a name and nothing else, so the packet carries
        `describe_yourself`: the room asking, rather than a card invented on the
        newcomer's behalf. What comes back stays SELF-DESCRIPTION - it is shown,
        never read as permission, exactly like a display name.

        Data, not prose: the CLI renders it, a remote client reads it as JSON, and
        an embedder can put it anywhere. Nothing here is a new frame kind, so no
        implementation has to learn anything to keep working.
        """
        members = self.members()
        labels = self.labels()
        caps = sorted(CAPABILITIES.get(identity.role, frozenset()))
        try:
            workspace = self.workspace_dir(create=False)
        except Exception:
            workspace = None
        return {
            "room": self.room_id,
            "kind": self.kind,
            "topic": str(self.manifest.get("topic") or ""),
            # What the room is for, and who leads it - the two questions a newcomer
            # in a room of twenty asks first, and the two nobody used to answer.
            "mission": str(self.manifest.get("mission") or ""),
            "leaders": [labels.get(p) or (members.get(p) or {}).get("display") or p
                        for p in self.leaders()],
            "closed": self.closed,
            "you": {
                "peer": identity.peer_id,
                "display": identity.display,
                "role": identity.role,
                "may_send": caps,
                "card": (members.get(identity.peer_id) or {}).get("card") or {},
            },
            "members": [
                {"peer": peer, "label": labels.get(peer) or rec["display"],
                 "role": rec["role"], "stale": rec["stale"],
                 # The card is what each member SAID it can do. Empty is a fact
                 # worth carrying: it tells a newcomer that asking is fair.
                 "card": rec.get("card") or {}}
                for peer, rec in sorted(members.items(),
                                        key=lambda kv: labels.get(kv[0]) or kv[1]["display"])
            ],
            "workspace": str(workspace) if workspace else "",
            "tasks_open": sum(1 for t in self.tasks()
                              if t["status"] not in ("completed", "failed",
                                                     "rejected", "canceled")),
            "describe_yourself": (
                (members.get(identity.peer_id) or {}).get("card", {}) == {}
            ),
        }

    def idle_peers(self, *, quiet_for_s: float, now: Optional[float] = None) -> List[str]:
        """Members that have neither read nor written here for that long.

        Derived from the cursors and the log, which already record both - reading
        advances a cursor only AFTER the frame is in hand, so "read_at" is a fact
        about attention and not about a socket being open. A lease says a process
        is alive; this says a PARTICIPANT has drifted off, which is the thing worth
        a nudge.

        Per peer, deliberately. A room-wide "nobody said anything lately" would
        wake twenty agents to tell nineteen of them what they already know; the
        one that drifted is the one to ask, and the protocol addresses a single
        peer without waking the rest.
        """
        moment = time.time() if now is None else now
        facts = self.activity()
        out = []
        for peer in self.members():
            fact = facts.get(peer) or {}
            # Joining is itself a frame the peer WROTE, so a fresh member carries
            # its own arrival time here and is never idle in its first hour. That
            # is why there is no separate join timestamp to keep: the log already
            # answers "when was this peer last present in any way".
            last_seen = max(float(fact.get("read_at") or 0.0),
                            float(fact.get("last_wrote_ts") or 0.0))
            if last_seen and (moment - last_seen) >= quiet_for_s:
                out.append(peer)
        return out

    def check_ins(self) -> Dict[str, float]:
        """When the room last checked in on each member, from the log itself.

        {peer: epoch of the newest idle check-in addressed to it}.

        Derived rather than remembered, the way `vote_reminders` and `task_nudges`
        already derive their once-rules: the check-in IS a frame, so a host that
        restarts does not start over. This was the one interval rule kept in
        process memory instead, and every restart of the app re-asked every idle
        member within seconds - measured on a day of live restarts, a quarter of a
        busy room's frames were check-ins.

        Vote reminders and task nudges ride the same frame kind but are about a
        ballot or a task, not about the member's attention, and each keeps its own
        once-rule - so they neither reset nor suppress this clock.
        """
        out: Dict[str, float] = {}
        for frame in self.store.frames():
            if frame.kind != "ping":
                continue
            body = frame.body or {}
            if body.get("vote") or body.get("task"):
                continue
            to = (frame.to or {}).get("peer") if isinstance(frame.to, dict) else ""
            if to:
                out[str(to)] = max(out.get(str(to), 0.0), float(frame.ts or 0.0))
        return out

    def ping_body(self, peer_id: str) -> Dict[str, Any]:
        """What the room tells ONE peer when it checks in on it.

        Role-shaped, because "you have drifted off" means something different to
        each of them: a leader has people waiting on decisions, a worker either
        has work or should ask for some, and a peer in a round is there for a
        purpose the room was opened for. The text is built HERE rather than in the
        surface that renders it, because the peer receiving it may be a foreign
        agent that never sees any of our surfaces - it gets the frame and nothing
        else.

        It is an invitation and never an order. A room is input, not authority:
        that line holds for a message from another agent and it holds for the
        room's own probe, or a host would have a remote control for everybody
        else's agent. Silence stays a valid answer, and the text says so.
        """
        role = self.role_of(peer_id) or "peer"
        board = self.tasks()
        mine = [t for t in board
                if t["assignee"] == peer_id
                and t["status"] not in ("completed", "failed", "rejected", "canceled")]
        members = self.members()
        labels = self.labels()
        topic = str(self.manifest.get("topic") or "").strip()
        mission = str(self.manifest.get("mission") or "").strip()

        def _line(task: Dict[str, Any]) -> str:
            progress = task.get("progress") or {}
            counted = ("done" in progress and "total" in progress)
            where = f" ({progress['done']}/{progress['total']})" if counted else ""
            step = f" - {progress['step']}" if progress.get("step") else ""
            return f"{task['title'][:70]} [{task['status']}{where}]{step}"

        if role == "leader":
            workers = [labels.get(p) or rec["display"] for p, rec in members.items()
                       if rec["role"] == "worker"]
            open_all = [t for t in board
                        if t["status"] not in ("completed", "failed", "rejected", "canceled")]
            text = (
                "Room check-in. You lead here. Look at how the work stands, and "
                "decide for yourself whether anything is needed: a worker waiting "
                "on an answer, a task worth handing out, or a report worth asking "
                "for. Nothing here is an instruction: if the room is fine, say "
                "nothing."
            )
            if workers:
                text += f"\nYour workers: {', '.join(workers[:12])}."
            text += (f"\nOpen work: {len(open_all)}."
                     + ("\n  " + "\n  ".join(_line(t) for t in open_all[:6])
                        if open_all else ""))
        elif role == "worker":
            if mine:
                text = ("Room check-in. You have work open here. Say where it "
                        "stands - a report on the same task with progress is "
                        "enough - or finish it. If it is blocked, say what you "
                        "need.\n  " + "\n  ".join(_line(t) for t in mine[:6]))
            else:
                leaders = [labels.get(p) or rec["display"] for p, rec in members.items()
                           if rec["role"] == "leader"]
                text = ("Room check-in. Nothing is assigned to you right now. If "
                        "you want work, ask "
                        + (f"{leaders[0]}" if leaders else "whoever leads here")
                        + " for some; if you are busy elsewhere, ignore this.")
        else:
            text = ("Room check-in. This room was opened for a reason"
                    + (f': "{topic}"' if topic else "")
                    + ". Read what has happened since you last looked, and if you "
                      "see something worth adding - a question, a proposal, work "
                      "you could take on - say it. If the room needs nothing from "
                      "you, say nothing.")
            if mine:
                text += ("\nYou have open work here:\n  "
                         + "\n  ".join(_line(t) for t in mine[:6]))

        # The purpose travels with every check-in, because forgetting what the room
        # is for is exactly what an hour of idleness does to an agent.
        if mission:
            text += f"\n\nWhat this room is for: {mission}"
        return {
            "text": text,
            "state": {
                "role": role,
                "mission": mission,
                "members": len(members),
                "tasks_open": sum(
                    1 for t in board
                    if t["status"] not in ("completed", "failed", "rejected", "canceled")),
                "your_tasks": [
                    {"id": t["id"], "title": t["title"][:120], "status": t["status"],
                     "progress": t.get("progress")}
                    for t in mine[:6]
                ],
                "workspace": str(self.workspace_dir(create=False) or ""),
            },
        }

    def ping(self, identity: Identity, peer_id: str) -> Frame:
        """Ask ONE peer whether it is still with this room. HOST ONLY.

        Addressed rather than broadcast, which is what keeps a check-in from
        costing every member a turn: the wake path only wakes a peer a frame is
        aimed at.
        """
        if not self.is_host(identity):
            raise NotPermitted("only the machine hosting a room checks in on its members")
        if not self.role_of(peer_id):
            raise NotAMember(f"{peer_id!r} is not in room {self.room_id!r}")
        return self.ingest({"kind": "ping", "to": {"peer": peer_id},
                            "body": self.ping_body(peer_id)}, identity=identity)

    def roles(self) -> Dict[str, str]:
        """The fold over join / role / leave. Recomputed, never cached to disk.

        MUTATION TARGET: reading a role out of the member file instead. That file is
        written by the peer itself, so a peer could then name its own role.
        """
        resolved: Dict[str, str] = {}
        hosts = self.host_peers()
        for frame in self.store.frames():
            if frame.kind == "join":
                resolved.setdefault(frame.sender, frame.role)
            elif frame.kind == "leave":
                resolved.pop(frame.sender, None)
            elif frame.kind == "kick":
                # Removing somebody else, which `leave` deliberately cannot do: one
                # writer per lane means a host cannot write into the lane of the peer
                # it is removing, so the removal is a frame in the HOST's own lane that
                # later readers fold in. Honoured only from a leader or from one of the
                # room's own host handles, and never against a host handle - the
                # protection is derived, so a peer cannot claim it for itself.
                target = str(frame.body.get("peer") or "")
                by_leader = frame.role == "leader"
                by_host = frame.sender in hosts
                if (by_leader or by_host) and target not in hosts:
                    resolved.pop(target, None)
            elif frame.kind == "role":
                target = str(frame.body.get("peer") or "")
                granted = str(frame.body.get("role") or "")
                # Only a leader may re-cast a role, and only within this room's
                # vocabulary. The sender's role here is the one the fold already
                # resolved, not one the frame claimed.
                if frame.role == "leader" and target in resolved and granted in CAPABILITIES:
                    resolved[target] = granted
        return resolved

    def role_of(self, peer_id: str) -> Optional[str]:
        return self.roles().get(peer_id)

    # ── who a signature belongs to ──────────────────────────────────────────

    def signing_keys(self) -> Dict[str, str]:
        """Which public key each peer published, folded from the log.

        A FOLD over the `join` frames, for the same reason roles are one: any reader
        recomputes it from the transcript alone and two readers cannot disagree. It
        deliberately does NOT read the member files: a member file is mutable and
        lives on the host's disk, so a host that could swap a key there could forge
        every later frame from that peer.

        A KEY COUNTS ONLY IF ITS OWN JOIN IS SIGNED BY IT. Position in a write-once
        lane is not enough, and believing it was is what made the rest of this module
        claim more than it delivered. A public key is PUBLIC - it sits in the log in
        plain sight - so a host that writes the log could copy one peer's key into
        another peer's join frame and then file the first peer's signed frames under
        the second handle, where they read `valid`. Measured, not theorised. Requiring
        the join to carry a signature by the very key it publishes closes it, because
        the host would need the private half to produce one.

        What it does NOT close, said plainly: a host can still mint a fresh keypair
        and publish it under a peer that never signed anything, and a self-signature
        proves possession, never ownership. The gain is narrower and real - a key
        already in the room cannot be re-pointed at a different handle, so a forged
        attribution now has to happen inside the victim's OWN lane, where the victim's
        gapless sequence makes it something they can find.

        Three things a `join` can do to a peer's key, and the third is the one worth
        stating: publishing an ATTESTED key binds it (the LAST one wins, so rejoining
        is how a peer rotates); publishing NO key withdraws the claim; and publishing
        a key that is not attested does NOTHING AT ALL. It neither binds nor
        withdraws, because a claim this reader cannot check must not be allowed to
        undo one it already checked.

        That third rule denies the host one specific move - stripping a `sig` to
        RETRACT a binding an earlier join already made - and it is worth being exact
        about how little that is. A host holding the disk can always deny: strip every
        announcement, or delete the files outright, and the peer's frames read
        `foreign_key` with nothing to appeal to. Denial cannot be closed by any rule
        that asks for a proof, because the proof is on the same disk. What the rule
        buys is that denial has to be complete and visible rather than surgical.

        The honest limit is unchanged: a reader holding only part of a transcript
        folds only what it holds.
        """
        keys: Dict[str, str] = {}
        for frame in self.store.frames():
            if frame.kind != "join":
                continue
            published = str((frame.body or {}).get("sign_key") or "")
            if not published:
                keys.pop(frame.sender, None)
                continue
            state, read = content_signature(frame, self.room_id)
            if state == "signed" and read["key"] == published:
                keys[frame.sender] = published
        return keys

    def verdict_for(self, frame: Frame, keys: Optional[Dict[str, str]] = None) -> str:
        """What a reader may conclude about who wrote this frame's content.

        Five answers, and the distinctions between them are the point:

        - `unsigned`: nothing was claimed. The ordinary case, and not a complaint.
        - `unreadable`: something is in `sig` that this peer cannot even parse. Not
          an accusation - a newer scheme would look like this to an older reader.
        - `valid`: the signature covers this content AND the key is the one this
          peer published in the room. The full claim.
        - `foreign_key`: the signature is real, but by no key this peer published
          in a form a reader can check. TWO causes, and they are worth telling
          apart before anybody is accused. A host that moved a frame between lanes
          looks like this - and so does an honest peer whose client published its
          key in a `join` it did not sign, which is what every guest did before the
          fold began asking. The log says which: look for a `join` in that peer's
          lane carrying a `sign_key` that its own signature does not attest.
        - `invalid`: a signature that does not cover this content. The only verdict
          that accuses anybody.

        A verdict NEVER removes a frame and never raises. The store already skips a
        file it cannot parse, so a verifier that threw would silently delete frames
        and tear the logical clock for every reader after them. A bad signature
        downgrades what may be concluded and nothing else, the way RFC 6376 treats a
        failed DKIM signature.
        """
        state, read = content_signature(frame, self.room_id)
        if state != "signed":
            return state
        published = (self.signing_keys() if keys is None else keys).get(frame.sender)
        return "valid" if published and published == read["key"] else "foreign_key"

    def verify_frames(self, since_lamport: int = 0) -> List[Tuple[Frame, str]]:
        """Every frame with what a reader may conclude about its authorship.

        The keys are folded ONCE for the whole walk: the fold reads the room, and
        asking it per frame would re-read the room per frame.
        """
        keys = self.signing_keys()
        return [(f, self.verdict_for(f, keys)) for f in self.store.read_since(since_lamport)]

    # ── the gate every frame passes ─────────────────────────────────────────

    def may(self, role: str, kind: str) -> bool:
        """The truth table, in one place, so no caller can hold a second copy."""
        if self.kind == "round" and kind == "directive":
            return False
        return kind in CAPABILITIES.get(role, frozenset())

    # The one member of a vote's body a reader counts on, and the width every lane
    # already trimmed it to by hand before `compose` became the single place.
    CHOICE_WIDTH = 60

    def compose(self, payload: Any) -> Dict[str, Any]:
        """What this room will actually STORE as a frame's content, normalised.

        Six things about a submission are the room's to settle: the kind, who it is
        addressed to, the body, what it answers, what it demands of a receiver, and
        the extension namespace. Everything else on a frame - `id`, `ts`, `seq`,
        `lamport`, `from`, `role` - is placement rather than content, is assigned
        from the admitted peer and the store, and is deliberately not here.

        **The contract is that compose is IDEMPOTENT**: `compose(compose(x))` equals
        `compose(x)` for every submission it does not refuse. That is the whole
        reason it is a method of its own rather than four lines inside `ingest`.
        A sender can ask what the room will store, be told, and hand exactly that
        back. Without a fixed point there is no honest way for a peer to commit to
        its own words, because it would be committing to a draft the room then
        rewrites, and no way for a later reader to tell a normalisation apart from
        a tampering.

        The ballot is the case that made the property necessary. A `choice` is
        resolved against its vote's options HERE, once, because every lane that
        resolved it for itself was another place to forget: measured live, the
        remote lane did forget, and a shortened "ja" became its own column in the
        tally beside "ja, weiter so". Resolving twice returns the same answer, which
        is what lets the normalisation and the fixed point coexist. The vote's own
        options are trimmed by the same hand, because a resolver that matches
        exactly cannot also be the thing that decides what an option is: an option
        stored as `"ja "` would resolve, store, and then be counted under `"ja"`,
        which is not one of the choices anyone was offered.
        """
        data = dict(payload.to_dict() if isinstance(payload, Frame) else payload)
        kind = str(data.get("kind") or "say")
        reply_to = str(data["reply_to"]) if data.get("reply_to") else None

        to = content_object(data.get("to"), "to") or {"room": True}
        body = content_object(data.get("body"), "body")
        ext = content_object(data.get("ext"), "ext")
        try:
            demanded = required_names(data.get("must_understand"))
        except MalformedFrame as e:
            raise MalformedContent(str(e)) from None

        if kind == "vote":
            body["options"] = self._vote_choices(body.get("options"))
            # The one wall clock in this protocol that decides something, and
            # therefore the one value in a body two machines must be able to write
            # down identically. Whole seconds; anything unusable is dropped rather
            # than stored, because a stored `"bald"` would be read again by every
            # later fold and cannot be taken back out of a write-once log.
            deadline = read_deadline(body)
            if deadline is None:
                body.pop("closes_at", None)
            else:
                body["closes_at"] = deadline
        elif kind == "answer" and reply_to and body.get("choice"):
            # Only when a `choice` is present, so an ordinary answer never pays for
            # the lookup.
            options = self._vote_options(reply_to)
            trimmed = str(body["choice"]).strip()[:self.CHOICE_WIDTH]
            body["choice"] = self._resolve_choice(trimmed, options) if options else trimmed

        return {"kind": kind, "to": to, "body": body, "reply_to": reply_to,
                "must_understand": demanded, "ext": ext}

    @classmethod
    def _trimmed_options(cls, options: Any) -> List[str]:
        """A vote's options, trimmed and bounded, with the empty ones dropped.

        The one place that decides what an option IS. Read as well as written
        through it, so a vote stored before that rule existed still answers ballots
        instead of refusing every one of them: a stored `"ja "` is read as `"ja"`,
        which is what a member typing `ja` is offering.
        """
        if isinstance(options, (str, bytes)) or not isinstance(options, Iterable):
            return []
        return [o for o in (str(x).strip()[:cls.CHOICE_WIDTH] for x in options) if o]

    def _vote_choices(self, options: Any) -> List[str]:
        """The options as a vote will STORE them: never empty, so a question always
        has an answer somebody can give. Empty means yes/no, which is what every
        reader already assumed."""
        return self._trimmed_options(options) or ["yes", "no"]

    def ingest(self, payload: Any, *, identity: Identity) -> Frame:
        """Accept one frame from an admitted peer, or refuse it.

        ``from`` and ``role`` are OVERWRITTEN with the admitted peer's resolved
        values. They are never honoured as they arrive: this is the same rule the
        tool dispatcher applies to model output, where identity is assigned over
        whatever the model produced rather than defaulted from it.
        """
        data = dict(payload.to_dict() if isinstance(payload, Frame) else payload)
        kind = str(data.get("kind") or "say")

        # A join carries its own admission, so the fold does not know the peer yet.
        role = identity.role if kind == "join" else (self.role_of(identity.peer_id) or "")
        if not role:
            raise NotAMember(f"{identity.peer_id!r} has not joined room {self.room_id!r}")

        # A closed room takes nothing more, from anybody, including the host that
        # closed it. Closing was a displayed flag and nothing else until a test asked
        # what it actually stopped: the transcript said "closed", every surface showed
        # it, and writes went on being accepted. Revoking the ability to write IS the
        # act - without this, closing a room told the participants their access was
        # gone while leaving it exactly where it was.
        if self.closed:
            raise RoomClosed(
                f"room {self.room_id!r} is closed; it stays readable and takes nothing more"
            )

        if self.kind == "round" and kind == "directive":
            raise WrongRoomKind(
                "a round has no command direction; nobody may issue a directive here"
            )
        # Closing is the ONE act the host may perform whatever its role, and it is not
        # a role power that leaked. The capability table answers "what may a peer do IN
        # the conversation"; ending the room is a different question, answered by whose
        # machine is storing it. Without this a round could never be closed at all -
        # a round has no leader by design, so its own host would be locked out of
        # ending a conversation living in their own files.
        if kind == "kick":
            # Shaped before it is read: a body that is not an object used to reach
            # `.get` and raise an AttributeError out of whichever door was holding
            # the submission, which is the same failure `ext: "x"` had.
            target = str(content_object(data.get("body"), "body").get("peer") or "")
            if not target:
                raise RoomError("a kick names the peer it removes")
            if target in self.host_peers():
                # Said as a refusal rather than ignored, because the caller usually has
                # a person in front of it who needs to hear the alternative.
                raise NotPermitted(
                    "the room's own host cannot be removed from it; closing the room "
                    "is what takes everybody out")
            if target == identity.peer_id:
                raise NotPermitted("use leave to go yourself")
            if not self.role_of(target):
                raise NotAMember(f"{target!r} is not in room {self.room_id!r}")

        # `ping` joins close and kick as an act of the machine that HOLDS the room
        # rather than of a role in the conversation: a round has no leader, and the
        # timer that notices an idle peer runs on the host. A guest cannot ping -
        # is_host is keyed on the tenant, and a redeemed ticket has none.
        # What the ROOM itself may say, as opposed to what a member may. `tally`
        # belongs here for the same reason `ping` does: a result a member could
        # write is a result a member could invent.
        host_acting = kind in ("close", "kick", "ping", "tally") and self.is_host(identity)
        if kind in KINDS and not host_acting and not self.may(role, kind):
            raise NotPermitted(f"a {role} may not emit {kind!r} in this room")

        # What the room will store, settled in one place and idempotently, so a
        # sender can be told it in advance and hand exactly it back. Everything
        # below is placement: assigned from the admitted peer and the store, and
        # never honoured as it arrived.
        content = self.compose(data)
        signature = self._settle_signature(data.get("sig"), content, identity)
        frame = Frame.new(
            room=self.room_id,
            sender=identity.peer_id,
            role=role,
            seq=self.store.next_seq(identity.peer_id),
            lamport=self.store.next_lamport(),
            sig=signature,
            **content,
        )
        return self.store.append(frame)

    def _settle_signature(self, presented: Any, content: Mapping[str, Any],
                          identity: Identity) -> Optional[Dict[str, Any]]:
        """The signature this frame will carry: the one presented, a fresh one, or none.

        A PRESENTED signature is checked against the content the room is ABOUT TO
        STORE, not against what arrived, and refused when the two disagree. That is
        only answerable because `compose` is a fixed point: a sender can ask what will
        be stored and sign exactly it. Storing the frame anyway with a note saying the
        content was normalised afterwards would be the canonicalisation-divergence
        class, where one message has two valid readings and a verifier and a renderer
        can be made to disagree. The standards this follows are unanimous that the
        receiving end refuses instead (RFC 9413 on why quietly accepting input outside
        the specification entrenches the error, RFC 9421 on not signing what another
        party fills in).

        The refusal is JUDGED, which matters downstream: a held connection files a
        judged refusal aside rather than retrying it, and re-sending a signature that
        did not verify would repeat forever.

        With nothing presented, the room signs on the peer's behalf when it holds that
        peer's key. It never fails for want of one: an unsigned frame is what every
        frame was until now.
        """
        read = None
        if presented is not None:
            from vaf.core.a2a import signing
            read = signing.read_signature(presented)
            if read is None:
                raise MalformedContent(
                    "'sig' is not a signature this room can read; expected "
                    "{'alg': 'ed25519', 'key': <64 hex>, 'sig': <128 hex>}")
            if not signing.verify(
                    signing.covered_payload(self.room_id, identity.peer_id, content), read):
                raise NotPermitted(
                    "this signature does not cover the content the room would store. "
                    "Compose the message first, then sign exactly what compose returned")
            return read

        if identity.participant_key and _may_self_sign(identity.participant_key):
            return _sign_content(identity.participant_key, self.room_id,
                                 identity.peer_id, content)
        return None

    # ── convenience, all of it routed through ingest ────────────────────────

    def say(self, identity: Identity, text: str, **kw) -> Frame:
        return self.ingest({"kind": "say", "body": {"text": text}, **kw}, identity=identity)

    def ask(self, identity: Identity, text: str, **kw) -> Frame:
        return self.ingest({"kind": "ask", "body": {"text": text}, **kw}, identity=identity)

    def answer(self, identity: Identity, text: str, *, reply_to: str = "", **kw) -> Frame:
        return self.ingest({"kind": "answer", "body": {"text": text},
                            "reply_to": reply_to or None, **kw}, identity=identity)

    def directive(self, identity: Identity, text: str, **kw) -> Frame:
        return self.ingest({"kind": "directive", "body": {"text": text}, **kw},
                           identity=identity)

    def report(self, identity: Identity, text: str, *, status: str = "completed",
               artifacts: Optional[List[Dict[str, Any]]] = None,
               progress: Optional[Dict[str, Any]] = None, **kw) -> Frame:
        """A report carries a STATUS from the open A2A vocabulary and may carry
        artifacts and progress. All three live in the body, so none of them costs
        a frame change: an artifact kept out of the chat text stays findable by a
        machine, and progress turns ten minutes of unchanged `working` into
        something a reader can tell apart from a hang.

        The progress it writes is normalised by the same reader that consumes it
        (`read_progress`), so this room cannot emit a shape it would itself
        refuse from somebody else."""
        body: Dict[str, Any] = {"text": text, "status": status}
        if artifacts:
            body["artifacts"] = list(artifacts)
        clean = read_progress({"progress": progress}) if progress else None
        if clean:
            body["progress"] = clean
        return self.ingest({"kind": "report", "body": body, **kw}, identity=identity)

    def leave(self, identity: Identity, reason: str = "") -> Frame:
        return self.ingest({"kind": "leave", "body": {"reason": reason}}, identity=identity)

    def delete(self, identity: Identity, *, reason: str = "") -> bool:
        """Close the room and remove it from this machine. HOST ONLY.

        Closing first is not a formality. The farewell is a frame, so a peer that reads
        the room before the files go tells its own user WHY its access ended, instead of
        finding a conversation that simply is not there any more. On one machine the two
        happen in the same breath; over a wire they do not, and the order is what makes
        the difference survivable.

        Host only, and for a blunter reason than kicking: this deletes somebody else's
        transcript as well as your own. A guest that could do it could end everybody's
        work on its way out and leave no record that it had been there.

        The shared folder goes with it, the same way a chat's workspace goes with the
        chat (session.SessionManager.delete): deleting the conversation IS the
        statement that its files are no longer wanted.
        """
        if not self.is_host(identity):
            raise NotPermitted(
                "only the machine hosting a room can delete it; leaving is `leave`")
        if not self.closed:
            self.close(identity, reason=reason or self.TERMINATED_BY_USER)
        try:
            ws = self.workspace_dir(create=False)
            if ws is not None and ws.is_dir():
                import shutil
                shutil.rmtree(ws, ignore_errors=True)
        except Exception:
            pass
        return self.store.destroy()

    def workspace_dir(self, *, create: bool = False) -> Optional[Path]:
        """The room's shared folder on the HOST machine: VAF_Projects/<uid8>/<room_id>/.

        Host-local by design and NOT part of the wire protocol: a remote peer reads
        frames, never this directory. It lives under the owning account's projects
        root - the same tree every chat workspace lives in - so the same browser,
        the same upload lane and the same cleanup already know how to handle it,
        and the room id is its folder name for the same reason a chat's session id
        is (both are shaped by their own id rules, safe as a path segment).

        Members from OTHER tenants do not reach it today: their file jail ends at
        their own projects root. Opening the folder across the tenant line is a
        containment decision, not a path question, and it is deliberately not made
        here. A guest with no tenant at all never had a jail exception to lose.

        Returns None when the room has no owner tenant to anchor the path under
        (a manifest without an owner is a legacy shape the loader tolerates).
        """
        owner = owner_tenant(self.manifest.get("owner_scope"))
        if not owner:
            return None
        from vaf.core.session import get_user_projects_root
        root = get_user_projects_root(owner)
        if root is None:
            return None
        path = root / self.room_id
        if create and not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            topic = str(self.manifest.get("topic") or "").strip()
            if topic:
                try:
                    from vaf.core.session import write_workspace_label
                    write_workspace_label(path, topic)
                except Exception:
                    pass
        return path

    def kick(self, identity: Identity, peer_id: str, reason: str = "") -> Frame:
        """Remove another peer from the room.

        The frame goes into the ACTING peer's lane, never the removed one's: one
        writer per lane is the property the whole store rests on, and a removal that
        wrote into somebody else's directory would trade it away for a convenience.
        Every reader folds the same frame and reaches the same membership.
        """
        return self.ingest({"kind": "kick",
                            "body": {"peer": str(peer_id), "reason": reason}},
                           identity=identity)

    def close(self, identity: Identity, reason: str = "") -> Frame:
        return self.ingest({"kind": "close", "body": {"reason": reason}}, identity=identity)

    def grant_role(self, identity: Identity, peer_id: str, role: str) -> Frame:
        return self.ingest({"kind": "role", "body": {"peer": peer_id, "role": role}},
                           identity=identity)

    # ── the snowball ────────────────────────────────────────────────────────

    def children(self) -> List[str]:
        return [str(f.body.get("child_room") or "")
                for f in self.store.frames() if f.kind == "hire"]

    def hire(self, identity: Identity, *, purpose: str = "",
             kind: str = "chain", base: Optional[Path] = None) -> Tuple["Room", Frame]:
        """Open a child room in which this peer is the leader.

        The peer's role in THIS room does not change. The parent keeps the hire frame
        and, later, the child's report; it never receives the child's transcript.
        """
        role = self.role_of(identity.peer_id) or ""
        if not self.may(role, "hire"):
            raise NotPermitted(f"a {role} may not hire")

        depth = int(self.manifest.get("depth") or 0)
        max_depth = int(self.manifest.get("max_depth") or DEFAULT_MAX_DEPTH)
        max_children = int(self.manifest.get("max_children") or DEFAULT_MAX_CHILDREN)
        if depth + 1 > max_depth:
            raise BudgetExceeded(
                f"hiring would reach depth {depth + 1}, over the room's limit of {max_depth}"
            )
        if len(self.children()) >= max_children:
            raise BudgetExceeded(
                f"this room has already hired {max_children} times, its limit"
            )

        child = Room.create(
            kind=kind,
            owner_scope=self.manifest.get("owner_scope"),
            topic=purpose,
            base=base,
            depth=depth + 1,
            parent_room=self.room_id,
            max_depth=max_depth,
            max_children=max_children,
            multi_scope=bool(self.manifest.get("multi_scope")),
        )
        frame = self.ingest(
            {"kind": "hire", "body": {"child_room": child.room_id, "purpose": purpose}},
            identity=identity,
        )
        child.update(parent_frame=frame.id)
        # The hirer leads the child. It writes into the child's log as a new peer,
        # so the two rooms share no lane and no sequence.
        child.join(display=identity.display, scope_id=identity.scope_id, role="leader")
        return child, frame

    # ── tickets: a bearer credential for exactly one room ──────────────────

    def mint_ticket(self, identity: Identity, *, display: str = "",
                    ttl_s: float = 3600.0, tenant: Optional[str] = None) -> str:
        role = self.role_of(identity.peer_id) or ""
        if not role:
            raise NotAMember("only a member may invite")
        ticket_id = "t-" + secrets.token_hex(12)
        wanted = owner_tenant(tenant)
        now = time.time()
        self.store.put_ticket(ticket_id, {
            "room": self.room_id,
            "display": display or "guest",
            "minted_at": now,
            "expires_at": now + float(ttl_s),
            # Recorded for the transcript, not consulted for permission: what a guest
            # may do is decided when they act, not when they were invited.
            "minted_by": identity.peer_id,
            # Which door this opens. An AGENT ticket is a bearer credential, redeemed
            # on the wire by whoever holds it. An ACCOUNT ticket names the one tenant
            # on this machine that may accept it, and opens nothing on the wire at
            # all - knowing its id is not holding a credential.
            "kind": "account" if wanted else "agent",
            "tenant": wanted or None,
        })
        return ticket_id

    def redeem_ticket(self, ticket_id: str, *, display: str = "",
                      mode: str = DEFAULT_MODE,
                      card: Optional[Dict[str, Any]] = None) -> Identity:
        """Spend a ticket and join. The claim IS the check, so it is single use.

        Nothing that decides between two redeemers is read before the claim. Reading
        first would put the decision back in front of the race and let two handshakes
        arriving together both redeem the same invitation, which is the one thing a
        single-use bearer credential must not do. The one read that does come first
        decides nothing between redeemers: an ACCOUNT invitation is not a bearer
        credential and is refused on this door without being consumed, so the
        account it names can still accept it.
        """
        peek = self.store.ticket(ticket_id)
        if isinstance(peek, dict) and peek.get("tenant"):
            raise TicketInvalid("this invitation is for an account on this machine and "
                                "is accepted from that account, not redeemed here")
        record = self.store.claim_ticket(ticket_id)
        if record is None:
            raise TicketInvalid("this invitation has already been used, or does not exist")
        if str(record.get("room")) != self.room_id:
            raise TicketInvalid("this ticket is not for this room")
        if float(record.get("expires_at") or 0.0) < time.time():
            self._settle_ticket(record, "expired")
            raise TicketInvalid("this ticket has expired")
        identity = self.join(display=display or str(record.get("display") or "guest"),
                             scope_id=None, mode=mode, card=card or {})
        # The outcome, written where a listing can find it: a spent ticket with no
        # record of who redeemed it reads as "used" and answers none of the questions
        # the inviter has - did they arrive, and under which name.
        self._settle_ticket(record, "accepted", redeemed_by=identity.peer_id)
        return identity

    # ── invitations: the tickets read as a list, and the account door ─────────

    def _settle_ticket(self, record: Mapping[str, Any], status: str, **fields: Any) -> None:
        """Best effort by design: the claim already happened, and a listing that is
        one field short is better than an accepted invitation raising afterwards."""
        try:
            self.store.settle_ticket(str(record.get("ticket_id") or ""), status=status,
                                     decided_at=time.time(), **fields)
        except Exception:
            pass

    def _invitation_row(self, record: Mapping[str, Any], *, now: float) -> Dict[str, Any]:
        """One ticket as an invitation: the same fields whichever door it opens."""
        expires_at = float(record.get("expires_at") or 0.0)
        status = str(record.get("status") or "")
        if not status:
            status = "expired" if expires_at and expires_at < now else "pending"
        minted_by = str(record.get("minted_by") or "")
        redeemed_by = str(record.get("redeemed_by") or "")
        return {
            "id": str(record.get("ticket_id") or ""),
            "kind": str(record.get("kind") or ("account" if record.get("tenant") else "agent")),
            "display": str(record.get("display") or ""),
            "tenant": str(record.get("tenant") or "") or None,
            "status": status,
            "minted_by": minted_by,
            "minted_by_label": self.label_for(minted_by) if minted_by else "",
            "minted_at": float(record.get("minted_at") or 0.0),
            "expires_at": expires_at,
            "decided_at": float(record.get("decided_at") or 0.0) or None,
            "redeemed_by": redeemed_by or None,
            "redeemed_by_label": self.label_for(redeemed_by) if redeemed_by else "",
        }

    def invitations(self, identity: Identity) -> List[Dict[str, Any]]:
        """Every invitation this room handed out, with what became of it. Members only.

        Both doors in one list - the agent tickets the wire redeems and the account
        invitations another tenant accepts - because the person who invited does not
        think of them as two lists: "who did I invite, and who has arrived" is one
        question. A pending ticket past its time is SETTLED here as expired rather
        than merely shown so: the credential leaves the pending directory the first
        time anybody looks, which is the cleanup `drop_ticket` promised and no caller
        ever ran.
        """
        if not self.role_of(identity.peer_id):
            raise NotAMember("only a member sees a room's invitations")
        now = time.time()
        rows: List[Dict[str, Any]] = []
        for record in self.store.tickets():
            if (not record.get("status")
                    and float(record.get("expires_at") or 0.0) < now
                    and self.store.claim_ticket(str(record.get("ticket_id") or ""))):
                self._settle_ticket(record, "expired")
                record = dict(record, status="expired", decided_at=now)
            rows.append(self._invitation_row(record, now=now))
        rows.sort(key=lambda r: (r["status"] != "pending", -(r["minted_at"] or 0.0)))
        return rows

    def invitation_for(self, tenant: str) -> Optional[Dict[str, Any]]:
        """The pending invitation for ONE account, or None. Needs no membership.

        Answered for the invitee, who by definition is not a member yet: what it
        reveals is that account's own invitation and nothing about anybody else's.
        """
        wanted = owner_tenant(tenant)
        if not wanted:
            return None
        now = time.time()
        for record in self.store.tickets():
            if record.get("status") or owner_tenant(record.get("tenant")) != wanted:
                continue
            if float(record.get("expires_at") or 0.0) < now:
                continue
            return self._invitation_row(record, now=now)
        return None

    def invite_account(self, identity: Identity, tenant: str, *, display: str = "",
                       ttl_s: float = DEFAULT_INVITE_TTL_S) -> Dict[str, Any]:
        """Invite another ACCOUNT on this machine, and let it decide.

        The consenting counterpart to `admit`, under the same rule (host or leader,
        and only into a room opened to other accounts): the account is named here,
        but it joins only when it accepts, and until then it can see that it was
        invited and by whom, and nothing of what was said. A pending invitation for
        the same account is returned rather than doubled, for the reason `just_opened`
        gives: a second invitation to the same person is nearly always a lost track
        of the first.
        """
        self._check_may_admit(identity)
        wanted = owner_tenant(tenant)
        if not wanted:
            raise RoomError("name the account to invite")
        if wanted == owner_tenant(self.manifest.get("owner_scope")):
            raise RoomError("that account holds this room already")
        for lane in PARTICIPANT_LANES:
            if self.role_of(derive_peer_id(participant_key(lane, wanted), self.room_id)):
                raise RoomError("that account is already in this room")
        existing = self.invitation_for(wanted)
        if existing is not None:
            return existing
        ticket = self.mint_ticket(identity, display=display or "account",
                                  ttl_s=float(ttl_s), tenant=wanted)
        row = self.invitation_for(wanted)
        return row if row is not None else {"id": ticket, "kind": "account",
                                            "tenant": wanted, "status": "pending"}

    def accept_invitation(self, tenant: str, *, display: str) -> Identity:
        """The invited account says yes: it is admitted and joins as itself.

        Two things happen and they happen in this order. The guest list is written
        first, because `join` walks through `_check_tenant` like every other join and
        must find the account there - the invitation is what permits the admission,
        so the room writes it on the invitee's behalf; there is no host in the loop
        at this moment and there does not need to be, the host decided when it
        invited. The join is the person's own lane (the CLI lane, which the browser
        shares with the terminal), so the room shows one person and not two.

        The claim is single use exactly as on the wire door: two browsers accepting
        at once produce one member, and the loser is told the invitation is gone.
        """
        wanted = owner_tenant(tenant)
        pending = self.invitation_for(wanted)
        if pending is None:
            raise TicketInvalid("there is no open invitation for this account here")
        record = self.store.claim_ticket(pending["id"])
        if record is None or owner_tenant(record.get("tenant")) != wanted:
            raise TicketInvalid("this invitation has already been answered")
        if float(record.get("expires_at") or 0.0) < time.time():
            self._settle_ticket(record, "expired")
            raise TicketInvalid("this invitation has expired")
        self._admit_tenant(wanted)
        key = participant_key("cli", wanted)
        identity = self.join(display=display or str(record.get("display") or "guest"),
                             scope_id=wanted, peer_id=derive_peer_id(key, self.room_id),
                             participant_key=key)
        self._settle_ticket(record, "accepted", redeemed_by=identity.peer_id)
        return identity

    def decline_invitation(self, tenant: str) -> Dict[str, Any]:
        """The invited account says no. The invitation is spent, and the answer is
        kept, so the inviter reads "declined" and not "never got round to it"."""
        wanted = owner_tenant(tenant)
        pending = self.invitation_for(wanted)
        if pending is None:
            raise TicketInvalid("there is no open invitation for this account here")
        record = self.store.claim_ticket(pending["id"])
        if record is None or owner_tenant(record.get("tenant")) != wanted:
            raise TicketInvalid("this invitation has already been answered")
        self._settle_ticket(record, "declined")
        return dict(pending, status="declined", decided_at=time.time())

    def revoke_invitation(self, identity: Identity, invitation_id: str) -> Dict[str, Any]:
        """Take an invitation back before it is answered. Whoever minted it, or the
        host or leader, and only while it is still pending: an answered invitation
        is an answer, not something to withdraw."""
        role = self.role_of(identity.peer_id)
        if not role:
            raise NotAMember("only a member may withdraw an invitation")
        record = self.store.ticket(invitation_id)
        if not isinstance(record, dict):
            raise TicketInvalid("that invitation is not open any more")
        may = (str(record.get("minted_by") or "") == identity.peer_id
               or self.is_host(identity) or role == "leader")
        if not may:
            raise NotPermitted("only whoever invited, or the room's host or leader, "
                               "withdraws an invitation")
        claimed = self.store.claim_ticket(invitation_id)
        if claimed is None:
            raise TicketInvalid("that invitation is not open any more")
        self._settle_ticket(claimed, "revoked")
        return self._invitation_row(dict(claimed, status="revoked", decided_at=time.time()),
                                    now=time.time())

    # ── seats: how a redeemed ticket comes back ─────────────────────────────

    SEAT_PREFIX = "s-"

    def issue_seat(self, identity: Identity) -> str:
        """A durable way back in for a peer whose ticket is spent.

        A ticket is single use by design, and that design is right: a bearer
        credential pasted into a chat window must die on first use. But a CLI is one
        process per command, so an invited agent connects once to join and then again
        for every wait and say - without this, the second connection has nothing to
        present and the invitation only ever works once.

        The seat is the answer: minted at redemption, handed over exactly once in the
        welcome, and bound to this member in THIS room's store. Only its HASH is kept
        (the member file is at rest on the host; a stored secret would make every
        member file a credential), and the member file is one the ROOM writes here -
        the seat is the room's promise about who may sit down again, not a claim the
        peer gets to make about itself.
        """
        secret = secrets.token_hex(16)
        record = self.store.member(identity.peer_id) or {}
        record["seat_hash"] = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        self.store.put_member(identity.peer_id, record)
        return f"{self.SEAT_PREFIX}{identity.peer_id}-{secret}"

    def redeem_seat(self, credential: str) -> Identity:
        """The member a seat belongs to, or a refusal. Reusable, unlike a ticket."""
        raw = str(credential or "")
        if not raw.startswith(self.SEAT_PREFIX):
            raise TicketInvalid("that is not a seat credential")
        body = raw[len(self.SEAT_PREFIX):]
        peer_id, sep, secret = body.rpartition("-")
        if not sep or not peer_id or not secret:
            raise TicketInvalid("that seat credential is malformed")
        record = self.store.member(peer_id) or {}
        stored = str(record.get("seat_hash") or "")
        presented = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        # compare_digest, not ==: the hash is stored, the secret is presented, and a
        # timing oracle on the comparison would leak the stored half byte by byte.
        if not stored or not secrets.compare_digest(stored, presented):
            raise TicketInvalid("this seat does not exist in this room")
        role = self.role_of(peer_id)
        if not role:
            raise TicketInvalid("this seat's member has left the room")
        return Identity(peer_id, record.get("display") or peer_id, None, role)

    # ── reading ─────────────────────────────────────────────────────────────

    def identity_for(self, key: str, *, display: str = "",
                     scope_id: Optional[str] = None) -> Optional[Identity]:
        """The Identity this participant already has here, or None if not a member."""
        peer_id = derive_peer_id(key, self.room_id)
        role = self.role_of(peer_id)
        if not role:
            return None
        record = self.store.member(peer_id) or {}
        return Identity(peer_id, display or record.get("display") or peer_id, scope_id,
                        role, participant_key=key)

    def activity(self) -> Dict[str, Dict[str, Any]]:
        """Who is engaged with the newest part of the conversation, as FACTS.

        {peer: {"read_to": lamport, "read_at": epoch, "last_wrote": lamport}}

        Facts, not a verdict, on purpose: whether "read the newest message two
        seconds ago and has not answered" should be painted as a typing indicator
        is presentation taste, and taste ages faster than data. The surfaces decide
        the window and the wording; this only joins what the store already records
        - each reader's own cursor (position and when it moved) with the last frame
        each sender wrote.

        Nothing here is a protocol concept. A frame kind for "typing" would persist
        an ephemeral state into a write-once transcript and demand that every
        foreign implementation understand it; deriving the signal on the host asks
        nothing of anybody.
        """
        cursors = self.store.cursors()
        last_wrote: Dict[str, int] = {}
        wrote_at: Dict[str, float] = {}
        for frame in self.store.frames():
            if frame.lamport > last_wrote.get(frame.sender, 0):
                last_wrote[frame.sender] = frame.lamport
                # The wall clock of that frame, carried alongside the lamport it
                # orders by. Ordering never reads it (clocks differ between the
                # machines in a room); "how long since this peer did anything" is
                # a question about duration, and lamports do not measure time.
                wrote_at[frame.sender] = float(frame.ts or 0.0)
        out: Dict[str, Dict[str, Any]] = {}
        for peer in set(cursors) | set(last_wrote):
            record = cursors.get(peer) or {}
            out[peer] = {
                "read_to": int(record.get("lamport") or 0),
                "read_at": float(record.get("updated_at") or 0.0),
                "last_wrote": int(last_wrote.get(peer, 0)),
                "last_wrote_ts": float(wrote_at.get(peer, 0.0)),
            }
        return out

    def tasks(self) -> List[Dict[str, Any]]:
        """The work in flight, DERIVED - there is deliberately no task frame kind.

        The fold itself is `fold_tasks` at module level, because a peer reading a
        room over the WIRE has frames and no store, and a second fold there would
        be a second opinion about what "working" means. See that function for why
        a task is a chain of reports rather than a stored entity.
        """
        return fold_tasks(sorted(self.store.frames(), key=canonical_sort_key),
                          labels=self.labels())

    def mode_of(self, peer_id: str) -> str:
        """The LOCAL user's standing decision about how far their agent may go here.

        Read from the member file, which only this peer writes, and never from a
        frame. A remote leader can ask for anything; it can never grant autonomy.
        """
        record = self.store.member(peer_id) or {}
        mode = str(record.get("mode") or DEFAULT_MODE)
        return mode if mode in ROOM_MODES else DEFAULT_MODE

    def set_mode(self, identity: Identity, mode: str) -> str:
        if mode not in ROOM_MODES:
            raise RoomError(f"unknown room mode {mode!r}; expected one of {ROOM_MODES}")
        record = self.store.member(identity.peer_id) or {}
        record["mode"] = mode
        self.store.put_member(identity.peer_id, record)
        return mode

    def label_for(self, peer_id: str) -> str:
        """What a human calls this member: their name plus a short tag, "Codex51".

        Two agents joining as "Codex" are indistinguishable in a transcript, and a
        person reading it has no way to ask one of them anything. The tag is derived
        from the peer handle, so it survives restarts and needs no counter written into
        the room - the kind of shared, incrementing state this store spends its whole
        design avoiding.

        UNIQUE WITHIN THE ROOM, which is the property that matters and the reason this
        lives on the room rather than in a helper: a collision is resolved by taking
        more of the digest, and a room without name collisions has no undeliverable
        mentions at all.
        """
        members = self.store.members()
        record = members.get(peer_id) or {}
        base = str(record.get("display") or peer_id)

        # A UNIQUE NAME IS LEFT ALONE. Every name used to carry a tag, which made
        # "Nobel" into "Nobel88" for no reason anybody could see: the name was already
        # the only one in the room. A tag is the answer to a COLLISION, so it appears
        # when there is one and not before.
        same = sorted(other for other, rec in members.items()
                      if str(rec.get("display") or other) == base)
        if len(same) < 2:
            return base

        # Two agents called Codex: they become Codex1 and Codex2, numbered by the order
        # their handles sort in. Small numbers rather than a digest, because a human
        # reads them back to somebody else - and stable, because the ordering comes from
        # the handles, which do not move. A newcomer joining later takes the next free
        # number without renaming anybody who is already being spoken to.
        return f"{base}{same.index(peer_id) + 1}" if peer_id in same else base

    def labels(self) -> Dict[str, str]:
        """Every member's human label, resolved together so they cannot clash."""
        return {peer: self.label_for(peer) for peer in self.store.members()}

    def peer_by_display(self, name: str) -> Optional[str]:
        """The peer behind a display name, or None if it is unknown or ambiguous.

        Lives HERE and nowhere else: only the room knows who is in it, and a resolver
        in the CLI or the terminal app would be a second copy of the member table that
        drifts the first time somebody joins while it is cached.

        Ambiguity is refused rather than guessed. Two members called "Codex" and a
        message addressed at one of them is a message that must not be delivered to a
        coin toss.
        """
        wanted = str(name or "").strip().lstrip("@").lower()
        if not wanted:
            return None
        hits = [peer for peer, record in self.members().items()
                if str(record.get("display") or "").strip().lower() == wanted]
        if len(hits) == 1:
            return hits[0]
        # The tagged label resolves too, and it has to: every surface SHOWS the label,
        # so "Codex51" is the name a reader has in front of them when they type a
        # mention. Matching only the bare display would make the addressed message
        # arrive at the room instead of at the person it named - a quiet mis-delivery
        # rather than an error. Labels are unique within the room by construction, so
        # a hit here is never the coin toss the paragraph above refuses.
        for peer, label in self.labels().items():
            if str(label).strip().lower() == wanted:
                return peer
        # A peer id addresses itself, which is what a machine consumer will use.
        return name if name in self.roles() else None

    def address_from_mention(self, text: str) -> Optional[Dict[str, Any]]:
        """The ``to`` a leading "@Name" asks for, or None.

        Only a mention at the START addresses a message. "@Bob can you look" is aimed
        at Bob; "ask @Bob about it" is a sentence ABOUT Bob, said to the room, and
        turning that into a private aside would quietly hide it from everyone else.
        """
        stripped = str(text or "").lstrip()
        if not stripped.startswith("@"):
            return None
        name = stripped[1:].split(None, 1)[0].rstrip(",:;")
        peer = self.peer_by_display(name)
        return {"peer": peer} if peer else None

    def transcript(self, since_lamport: int = 0) -> List[Dict[str, Any]]:
        """The room as a group chat: who said what, in canonical order.

        The speaker is kept APART from the text, the rule voice_turn already follows,
        so a renderer never has to parse a name back out of a message.
        """
        members = self.store.members()
        # Resolved once for the whole transcript rather than per line: the labels have
        # to be decided against each other (that is what makes them unique), and doing
        # it per row would re-read the member table for every frame in the room.
        labels = self.labels()
        # Folded ONCE for the whole transcript, like the labels above and for the same
        # reason: the fold reads the room, so asking it per row would read the room per
        # row.
        keys = self.signing_keys()
        rows = []
        for frame in self.store.read_since(since_lamport):
            record = members.get(frame.sender) or {}
            rows.append({
                "peer": frame.sender,
                "display": record.get("display") or frame.sender,
                # The name a human uses, tag included. It travels WITH the row because
                # four surfaces render this transcript, and a surface that tagged names
                # for itself would call the same peer something different from the next
                # one - which is exactly what a mention has to resolve against.
                "label": labels.get(frame.sender) or record.get("display") or frame.sender,
                "role": frame.role,
                "kind": frame.kind,
                "text": str((frame.body or {}).get("text") or ""),
                # Cleaned here rather than in each renderer: four surfaces draw
                # this transcript, and a reference a peer wrote is untrusted
                # input to every one of them.
                "files": attached_files(frame.body),
                "body": frame.body,
                "lamport": frame.lamport,
                "ts": frame.ts,
                "id": frame.id,
                "reply_to": frame.reply_to,
                # Carried so a renderer can mark who a line was aimed at. Without it
                # every surface would have to re-read the frame to answer a question
                # the transcript already knows.
                "to": dict(frame.to or {}),
                "known": frame.kind_known,
                # What a reader may conclude about who wrote this, carried WITH the row
                # because four surfaces render this transcript and a surface that
                # decided for itself would be a second opinion about authorship. The
                # verdict travels, never the signature: a renderer has no use for 128
                # hex characters, and every projection that carried raw material it did
                # not need has ended up leaking or dropping it.
                "verdict": self.verdict_for(frame, keys),
            })
        return rows



# ── one wording for the three renderers ─────────────────────────────────────

def frame_clock(ts: Any) -> str:
    """A frame's wall clock as HH:MM, or empty if it cannot be one.

    `ts` is ADVISORY in the protocol and this is the only thing it is for: telling a
    human roughly when something was said. Ordering never reads it, because the clocks
    of two machines in one room do not agree.

    An unusable value renders as EMPTY rather than as a wrong time. The rule is worth a
    home of its own: a missing timestamp in a transcript is a gap somebody notices, and
    a wrong one is a fact somebody believes.
    """
    import time as _time
    try:
        return _time.strftime("%H:%M", _time.localtime(float(ts)))
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


# How many files one frame may point at. A message names what it is about; a
# frame listing a hundred files is a directory listing wearing a message's
# clothes, and every renderer would have to invent its own cut-off.
FILE_REFS_CAP = 20


def attached_files(body: Any) -> List[Dict[str, Any]]:
    """The shared-folder files a frame points at, cleaned. Never raises.

    A REFERENCE, never a payload: the bytes live in the room's shared folder
    and travel by the workspace lane, so a frame stays a message and a
    transcript stays readable. What rides in the frame is the name, so a
    receiver knows machine-readably that something was left for it instead of
    having to read a sentence and guess which word was the filename.

    Read defensively in BOTH directions, which is why one function serves the
    writing tools and the reading surfaces alike: a frame is written by a peer
    nobody here controls, and a renderer may turn a name into a link. An
    absolute path or a traversal is dropped rather than shown, and the list is
    capped - a cleaned reference that points nowhere is a name nobody can
    follow, which is the harmless outcome.
    """
    raw = (body or {}).get("files") if isinstance(body, dict) else None
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip().replace("\\", "/")
        if (not path or path.startswith("/") or path.startswith("~")
                or ".." in path.split("/") or len(path) > 300):
            continue
        row: Dict[str, Any] = {"path": path}
        try:
            size = int(item.get("size"))
            if size >= 0:
                row["size"] = size
        except (TypeError, ValueError):
            pass
        out.append(row)
        if len(out) >= FILE_REFS_CAP:
            break
    return out


#: How a verdict reads to somebody who is not reading a verdict column. Only the ones
#: that should change what a reader concludes are in here: `valid` and `unsigned` are
#: deliberately absent, because a mark on every line is a mark nobody sees.
SIGNATURE_DOUBTS = {
    "invalid": "signature does not match this message",
    "foreign_key": "signed by a key this member never published here",
    "unreadable": "signature in a form this version cannot check",
}


def describe(entry: Dict[str, Any]) -> str:
    """A transcript row as a line a human reads.

    Bookkeeping frames carry no text - a join says who, not what - so a renderer that
    prints the body alone shows "Worker (join):" and nothing after it. The wording
    lives here, once, because four surfaces render the same transcript (the CLI's
    log, the terminal app, the classic lane and the browser) and four copies of a
    phrase are four chances to drift.

    A SIGNATURE IS NAMED ONLY WHEN IT IS NOT PLAINLY IN ORDER. `unsigned` is the
    ordinary case and saying so on every line would be noise; `valid` is the good case
    and a badge on every line is the kind that gets scanned past within a day. What a
    reader must not miss is the opposite: a line whose name it should not take at face
    value. Asking the whole question for every line is what `vaf a2a verify` is for.
    """
    kind = str(entry.get("kind") or "")
    body = entry.get("body") or {}
    text = str(entry.get("text") or "")
    doubt = SIGNATURE_DOUBTS.get(str(entry.get("verdict") or ""))
    shared = ", ".join(f["path"] for f in attached_files(body))
    if shared and kind in ("say", "ask", "answer", "report", "directive"):
        # Said once here, so the CLI log, the terminal app and both web lanes
        # agree on how a shared file reads.
        text = f"{text} [shared: {shared}]".strip()
    if doubt and kind in ("say", "ask", "answer", "report", "directive"):
        text = f"{text} [{doubt}]".strip()
    if kind == "join":
        card = body.get("card") or {}
        skills = str(card.get("skills") or "").strip()
        return f"joined{f' - {skills}' if skills else ''}"
    if kind == "leave":
        reason = str(body.get("reason") or "").strip()
        return f"left{f' - {reason}' if reason else ''}"
    if kind == "close":
        reason = str(body.get("reason") or "").strip()
        return f"closed the room{f' - {reason}' if reason else ''}"
    if kind == "hire":
        return f"opened {body.get('child_room') or 'a child room'}" + (
            f" for {body['purpose']}" if body.get("purpose") else "")
    if kind == "kick":
        reason = str(body.get("reason") or "").strip()
        return f"removed {body.get('peer')} from the room" + (f" - {reason}" if reason else "")
    if kind == "role":
        return f"made {body.get('peer')} a {body.get('role')}"
    if kind == "ping":
        # One line in a log, whatever the frame carries for the agent that reads
        # it: the body is a briefing meant for ONE peer, and a transcript reader
        # wants to know that a check-in happened, not to read somebody else's.
        state = body.get("state") or {}
        open_work = state.get("tasks_open")
        return ("checked in" + (f" - {open_work} open" if open_work else ""))
    if kind == "vote":
        # The question plus what may be answered to it. Every surface that renders
        # this already says WHICH kind of frame it is, so the prefix would be the
        # badge again; the options are the part a reader cannot get anywhere else,
        # and without them `vaf a2a log` showed a question with no answers.
        options = [str(o) for o in (body.get("options") or []) if str(o).strip()]
        return f"{text} ({' | '.join(options)})" if options else text
    if kind == "tally":
        # The result is prose already, written once by the room when the vote
        # ended - a renderer that summarised it again would be a second opinion
        # about an outcome that is meant to read the same everywhere.
        return text
    if kind == "ack":
        return f"ack: {body.get('status') or 'ok'}"
    if kind == "report" and body.get("status"):
        return f"[{body['status']}] {text}".strip()
    if not entry.get("known", True):
        return f"<message type {kind!r} this version does not understand> {text}".strip()
    return text


# What each kind of frame IS, as an event. Separate from `describe` on purpose: that
# renders a conversation for somebody reading along, this answers "who did what, and
# when" for somebody checking. An unknown kind keeps its own name rather than being
# dropped, because a gap in an audit is worse than a line nobody recognises.
AUDIT_EVENTS = {
    "join": "joined",
    "leave": "left",
    "role": "role changed",
    "kick": "removed somebody",
    "hire": "opened a child room",
    "close": "closed the room",
    "say": "message sent",
    "ask": "question asked",
    "answer": "answer sent",
    "report": "report sent",
    "directive": "instruction sent",
    "ack": "acknowledged",
    "ping": "checked in on a member",
    "vote": "vote opened",
    "tally": "vote closed",
}


def audit(room: "Room", *, since_lamport: int = 0) -> List[Dict[str, Any]]:
    """Who did what in a room, and when - at the level of the act, never its wording.

    Everything here is derived from frames that were already written. There is no
    second record and nothing new is stored, which is the property that makes it worth
    trusting: an audit built from its own log could disagree with the transcript, and
    then somebody would have to decide which one lied. This one cannot, because it IS
    the transcript, read a different way.

    THE MESSAGE TEXT IS DELIBERATELY ABSENT. An audit answers "did the worker report
    before or after the leader asked" and "when did this agent leave"; reading what was
    actually said is the transcript's job, and it is a different question with a
    different reason for asking. Keeping the two apart means an audit can be shown to
    somebody who has no business reading the conversation.

    NAMED BOUNDARY: none of this is copied into the security event log, and joins are
    not security events. What that log holds today is REFUSALS - a handshake turned
    away at the room socket is written there, which is the shape it is good at: rare,
    each one worth a look. A successful join is membership in a conversation, and
    writing every one of them there would bury the refusals in traffic.
    The measurement behind that boundary, so it can be revisited honestly: an admission
    from ANOTHER machine would be a different case, because it means a stranger's agent
    was let in - and there are exactly zero of those today, since no client speaks the
    room socket yet. When one exists, this is the paragraph to come back to.
    """
    labels = room.labels()
    members = room.store.members()
    rows: List[Dict[str, Any]] = []
    for frame in room.store.read_since(since_lamport):
        body = frame.body or {}
        record = members.get(frame.sender) or {}
        detail = ""
        if frame.kind == "report":
            detail = str(body.get("status") or "")
        elif frame.kind == "kick":
            detail = str(labels.get(str(body.get("peer") or "")) or body.get("peer") or "")
        elif frame.kind == "role":
            detail = f"{body.get('peer') or '?'} -> {body.get('role') or '?'}"
        elif frame.kind == "hire":
            detail = str(body.get("child_room") or "")
        elif frame.kind in ("leave", "close"):
            detail = str(body.get("reason") or "")
        elif frame.to and frame.to.get("peer"):
            # Not the wording, but the fact that it was aimed at one participant -
            # which is exactly the sort of thing an audit is asked about.
            detail = f"to {labels.get(frame.to['peer']) or frame.to['peer']}"
        rows.append({
            "lamport": frame.lamport,
            "ts": frame.ts,
            "peer": frame.sender,
            "label": labels.get(frame.sender) or record.get("display") or frame.sender,
            "role": frame.role,
            "kind": frame.kind,
            "event": AUDIT_EVENTS.get(frame.kind, f"{frame.kind} sent"),
            "detail": detail,
        })
    return rows


# ── how a local participant finds its own rooms ─────────────────────────────

def joined_rooms(key: str, *, base: Optional[Path] = None) -> List[Tuple[Room, Identity]]:
    """Every room on this machine this participant has joined, with its identity.

    A room whose manifest is unreadable is SKIPPED rather than allowed to abort the
    scan: one damaged room must not make a participant deaf in all the others.
    """
    found: List[Tuple[Room, Identity]] = []
    for room_id in list_rooms(base):
        try:
            room = Room.open(room_id, base=base)
        except Exception:
            continue
        identity = room.identity_for(key)
        if identity is not None:
            found.append((room, identity))
    return found


def invited_rooms(tenant: str, *, base: Optional[Path] = None) -> List[Tuple[Room, Dict[str, Any]]]:
    """Every open room on this machine that has invited this ACCOUNT and is still
    waiting for its answer, with the invitation.

    The counterpart of `joined_rooms` for the moment before a join: a sidebar shows
    an invited room so the person can answer, and it is the same directory scan,
    tolerant of the same damage. A closed room withdraws its invitations by being
    closed - nothing could be joined there any more.
    """
    found: List[Tuple[Room, Dict[str, Any]]] = []
    if not owner_tenant(tenant):
        return found
    for room_id in list_rooms(base):
        try:
            room = Room.open(room_id, base=base)
        except Exception:
            continue
        if room.closed:
            continue
        try:
            row = room.invitation_for(tenant)
        except Exception:
            continue
        if row is not None:
            found.append((room, row))
    return found


#: How long a room stays "just opened" for the duplicate check below. Measured
#: rather than chosen: the failure it exists for was an agent that repeated a whole
#: room_open -> say -> say -> invite sequence inside ONE task, twenty-one seconds
#: apart, and then explained the second room to its user as a "double submission"
#: that the queue log shows never happened. Minutes, not hours - a weekly standup
#: room opened under the same topic last week is a different room, not a mistake.
JUST_OPENED_S = 600.0


def just_opened(key: str, topic: str, *, within_s: float = JUST_OPENED_S,
                base: Optional[Path] = None, now: Optional[float] = None) -> Optional[str]:
    """The room this participant just opened under this topic, or None.

    A REPEAT detector, deliberately not a uniqueness rule. Two rooms may share a
    topic and often should; what is almost never meant is the same participant
    opening the same topic twice within minutes, which is what an agent does when it
    loses track of a tool call it already made. So the window is short and the answer
    names the existing room, which is what the caller actually wants: use that one.

    Asks only about rooms this participant is IN, through the same walk `joined_rooms`
    does, so it cannot see or leak a room belonging to another tenant. A closed room
    never matches - reopening a topic after closing it is a new conversation.
    """
    wanted = str(topic or "").strip().casefold()
    if not wanted:
        return None
    moment = time.time() if now is None else float(now)
    for room, _identity in joined_rooms(key, base=base):
        if str(room.manifest.get("topic") or "").strip().casefold() != wanted:
            continue
        try:
            born = float(room.manifest.get("created_at") or 0.0)
        except (TypeError, ValueError):
            continue
        if born and 0 <= (moment - born) <= float(within_s) and not room.closed:
            return room.room_id
    return None


# Membership bookkeeping. It belongs in the transcript and it is NOT worth waking a
# participant for: an agent that starts a whole turn because somebody joined is noise,
# and who is present is answered by members() whenever it does read. An unknown kind is
# deliberately absent from this set, so a newer peer's message still wakes an older one.
BOOKKEEPING_KINDS = frozenset({"join", "leave", "ack", "role"})

# What counts as NOTHING WAS SAID on a surface built for people: the bookkeeping above
# plus the room's own check-in lane (`ping` carries idle check-ins, vote reminders and
# task nudges - the room talking to one member about its own attention). Three surfaces
# ask this same question and must answer it the same way: the learning transcript, the
# cross-chat corpus, and the sidebar's unread badge. The badge answering it differently
# is how a person got a notification for a frame no view would ever show them.
# Deliberately NOT folded into BOOKKEEPING_KINDS itself: the wake computation reads
# that one, and a ping MUST keep waking the member it is addressed to.
NON_CONVERSATION_KINDS = frozenset(BOOKKEEPING_KINDS | {"ping"})


def local_room_tenants(*, base: Optional[Path] = None) -> List[str]:
    """Every account that OWNS a room on this machine, plus the local admin.

    Derived from the manifests that are on disk anyway, never from a user store: this
    package stays thin on purpose, and the question it answers here is not "who has an
    account" but "whose rooms is this machine holding" - which is exactly the set that
    can have an agent waiting for a turn.

    The local admin is included because a machine with no room yet still has one
    account that can be given one.
    """
    found: List[str] = []
    for room_id in list_rooms(base):
        try:
            manifest = Room.open(room_id, base=base).manifest
        except Exception:
            continue
        owner = owner_tenant(manifest.get("owner_scope"))
        if owner:
            found.append(owner)
    try:
        from vaf.core.config import get_local_admin_scope_id
        admin = owner_tenant(get_local_admin_scope_id())
        if admin:
            found.append(admin)
    except Exception:
        pass
    return list(dict.fromkeys(found))


def unread_frames(key, *, base: Optional[Path] = None) -> List[Tuple[Room, Identity, List[Any], List[Any]]]:
    """What has arrived for this participant: (room, identity, WAKING, CONTEXT).

    Its OWN frames are excluded: an agent must not be woken by its own voice, which
    is the loop that turns a two-agent room into a runaway conversation. Membership
    bookkeeping is excluded for the same kind of reason, one level quieter.

    ``key`` may be one participant key or SEVERAL, and several is not a convenience:
    one process serves every tenant on this machine, so asking on behalf of only the
    account that happened to be bound last means every other tenant's agent is never
    polled at all. The rooms are walked ONCE and every key is tried against each, so
    asking for five accounts costs one directory scan, not five.
    """
    wanted = [key] if isinstance(key, str) else [str(k) for k in (key or []) if k]
    pending: List[Tuple[Room, Identity, List[Any], List[Any]]] = []
    for room_id in list_rooms(base):
        try:
            room = Room.open(room_id, base=base)
        except Exception:
            continue
        if room.closed:
            continue
        for participant in wanted:
            identity = room.identity_for(participant)
            if identity is None:
                continue
            cursor = room.store.cursor(identity.peer_id)
            unread = [
                frame for frame in room.store.read_since(cursor)
                if frame.sender != identity.peer_id
                and frame.kind not in BOOKKEEPING_KINDS
            ]
            # Two lists, and the difference between them is the whole addressing rule.
            # WAKING: only what is aimed at this peer. A message for somebody else must
            # not cost a turn, or "@Bob" would wake the entire room to read a note for
            # Bob. CONTEXT: everything unread, so a peer that IS woken sees the
            # conversation it is joining rather than one line out of it - a reply
            # written blind to what the others were just told is worse than no reply.
            waking = [f for f in unread if f.addresses(identity.peer_id, identity.role)]
            if waking:
                pending.append((room, identity, waking, unread))
    return pending


def unread_counts(key: str, *, base: Optional[Path] = None) -> Dict[str, int]:
    """How many messages are waiting for this participant, per room.

    Its own function because four surfaces asked the same question with the same
    comprehension - the CLI listing, the terminal app, the classic lane and the agent's
    room_read - and a four-way copy of an unpacking is four places to fix when the shape
    changes. It changed once already, which is how this got written.
    """
    return {room.room_id: len(waking) for room, _identity, waking, _context
            in unread_frames(key, base=base)}



def fold_votes(frames: List[Frame], *, labels: Dict[str, str],
               members: List[str], now: Optional[float] = None,
               remind_after_s: float = VOTE_REMIND_AFTER_S,
               abstain_after_s: float = VOTE_ABSTAIN_AFTER_S) -> List[Dict[str, Any]]:
    """Every vote in a room, folded from frames alone: tally, deadline, outcome.

    A free function over frames rather than a method, for the reason `fold_tasks`
    gives: a peer reading a room over the WIRE has frames and no store, and two
    folds are two opinions about who abstained.

    WHAT A DEADLINE IS HERE. A vote that named no `closes_at` still ends, because
    a question nobody answers is not a decision anybody can act on. The room waits
    `remind_after_s` for a member, reminds it once, and waits `abstain_after_s`
    more before counting it as abstaining - so the default life of a vote is the
    two added together, and a vote that DID name a deadline keeps it, with the
    reminder moved to `abstain_after_s` before the end.

    The `ts` of the opening frame is the clock this runs on, and that is a
    deliberate exception to "wall clocks are advisory here": a duration has to be
    measured from something, and the alternative - a lamport count - answers "how
    much was said" rather than "how long has it been". The consequence is stated
    rather than hidden: two machines whose clocks differ by a minute disagree by a
    minute about when a vote ends, and the one holding the room is the one that
    writes the result.

    The result IS a frame (`tally`, written once by the host), so `concluded` is
    not a guess this fold makes: an ended vote looks the same to every reader,
    including one that arrives afterwards and folds the same log.
    """
    frames = sorted(frames, key=canonical_sort_key)
    now = time.time() if now is None else float(now)
    opened: Dict[str, Dict[str, Any]] = {}
    for frame in frames:
        if frame.kind != "vote":
            continue
        body = frame.body or {}
        options = [str(o) for o in (body.get("options") or []) if str(o).strip()]
        ts = float(frame.ts or 0.0)
        # Read through the same defensive reader the door writes through. The door
        # only protects frames written after it existed; this protects the fold from
        # every frame already in the log, and from any peer that reaches a store
        # without crossing a door. Without it one unusable value ends voting in that
        # room for good, because a write-once log cannot take the frame back.
        closes_at = float(read_deadline(body) or 0.0)
        deadline = closes_at or (ts + remind_after_s + abstain_after_s)
        opened[frame.id] = {
            "id": frame.id,
            "question": str(body.get("text") or ""),
            "options": options or ["yes", "no"],
            "asked_by": frame.sender,
            "asked_by_label": labels.get(frame.sender) or frame.sender,
            "ts": ts,
            "closes_at": closes_at,
            "deadline": deadline,
            # One reminder, `abstain_after_s` before the end - late enough that a
            # busy agent is not nagged for thinking, early enough that being
            # reminded is still worth something.
            "remind_at": max(ts, deadline - abstain_after_s),
            "ballots": {},
            "result": None,
        }
    for frame in frames:
        if not frame.reply_to:
            continue
        entry = opened.get(frame.reply_to)
        if entry is None:
            continue
        if frame.kind == "tally":
            # Written by the host, once. The last one wins for the same reason the
            # last ballot does: a write-once log cannot take anything back.
            entry["result"] = dict(frame.body or {})
            continue
        if frame.kind != "answer":
            continue
        choice = str((frame.body or {}).get("choice") or "").strip()
        if not choice:
            continue
        entry["ballots"][frame.sender] = {
            "choice": choice,
            "label": labels.get(frame.sender) or frame.sender,
            "ts": frame.ts,
        }
    out = []
    for entry in opened.values():
        tally: Dict[str, int] = {}
        for ballot in entry["ballots"].values():
            tally[ballot["choice"]] = tally.get(ballot["choice"], 0) + 1
        entry["tally"] = dict(sorted(tally.items(), key=lambda kv: -kv[1]))
        entry["voted"] = len(entry["ballots"])
        # Who has NOT answered yet: in a room of twenty the useful question is
        # never "how many", it is "who are we still waiting for". The peer that
        # ASKED is not among them - it put the question, and a room where asking
        # obliges you to answer your own question would have nobody left to ask.
        entry["waiting_peers"] = sorted(
            peer for peer in members
            if peer not in entry["ballots"] and peer != entry["asked_by"])
        entry["waiting_for"] = sorted(
            labels.get(peer) or peer for peer in entry["waiting_peers"])
        entry["everyone_voted"] = not entry["waiting_peers"]
        entry["concluded"] = entry["result"] is not None
        # Nobody has to wait for a clock that everybody has already beaten: a vote
        # every member answered is over the moment the last ballot lands.
        entry["due"] = (not entry["concluded"]
                        and (entry["everyone_voted"] or now >= entry["deadline"]))
        # Who let it run out. Only meaningful once it is over - before that these
        # are simply the members the room is still waiting for.
        if entry["concluded"]:
            entry["abstained"] = [str(a) for a in (entry["result"].get("abstained") or [])]
        elif now >= entry["deadline"]:
            entry["abstained"] = list(entry["waiting_for"])
        else:
            entry["abstained"] = []
        entry["closed"] = bool(entry["concluded"] or entry["everyone_voted"]
                               or now >= entry["deadline"])
        entry["ballots"] = [
            {"peer": peer, **ballot} for peer, ballot in entry["ballots"].items()]
        out.append(entry)
    return sorted(out, key=lambda e: (e["closed"], -e["ts"]))


#: When a task with no final report stops counting as work in progress. The room can
#: never know that something FINISHED - only a report says that - but it can say that
#: nothing has been said about it for a long time, and a board that cannot is a board
#: that fills up with work nobody is doing. Measured on the first long-lived room: ten
#: entries counted as running, eight of them last reported on between 26 and 32 hours
#: earlier.
#:
#: Two hours rather than ten minutes, because a long run is supposed to say where it is
#: (`report.body.progress` exists for exactly that), and each such report moves this
#: line. Silence for two hours IS the signal.
TASK_QUIET_AFTER_S = 7200.0

#: When the room ASKS about a task nothing has been said about. Earlier than the line
#: above on purpose, so the two form an escalation rather than two verdicts: after half
#: an hour the room asks whether the work is still running, and only after two hours
#: with no answer does the board stop counting it. Thirty minutes because a coding run
#: legitimately takes twenty - asking sooner would interrupt work rather than find
#: abandoned work.
TASK_NUDGE_AFTER_S = 1800.0


def fold_tasks(frames: List[Frame], *, labels: Dict[str, str],
               now: Optional[float] = None,
               quiet_after_s: float = TASK_QUIET_AFTER_S) -> List[Dict[str, Any]]:
    """The task board, folded from frames alone.

    A task exists when somebody reports on something: the chain of `report` frames
    hanging off a root (via reply_to) IS the task, its status the last report's
    status and its progress the last progress anybody gave. A `directive` is a task
    from the moment it is given, even unreported - in a chain, giving work is the
    point. An `ask` or `say` becomes a task exactly when its assignee first reports
    on it, which keeps "are you there?" off the board without anybody classifying
    anything.

    A fold rather than a stored entity: mutable state in a write-once transcript
    would have to be understood by every foreign implementation. This asks nothing
    of anybody - a peer that only ever sends `report --reply-to <id> --status
    working` shows up on every surface that renders it.

    A free function rather than a method, because a peer reading a room over the
    WIRE has frames and no store. Two folds would be two opinions about what
    "working" means, and they would drift on the first status anybody adds.
    """
    frames = sorted(frames, key=canonical_sort_key)
    by_id: Dict[str, Frame] = {f.id: f for f in frames}

    def _root_of(frame: Frame) -> Frame:
        seen = set()
        current = frame
        while current.reply_to and current.reply_to in by_id \
                and current.id not in seen:
            seen.add(current.id)
            parent = by_id[current.reply_to]
            if parent.kind != "report":
                return parent
            current = parent
        return current

    tasks: Dict[str, Dict[str, Any]] = {}

    def _ensure(root: Frame) -> Dict[str, Any]:
        entry = tasks.get(root.id)
        if entry is None:
            entry = {
                "id": root.id,
                "title": str((root.body or {}).get("text") or "")[:160]
                         or f"({root.kind})",
                "requester": root.sender,
                "requester_label": labels.get(root.sender) or root.sender,
                "assignee": str((root.to or {}).get("peer") or ""),
                "assignee_label": "",
                "status": "submitted",
                "progress": None,
                "reports": 0,
                # WHAT CAME OF IT, in the words of whoever reported last. A history
                # that says a task was completed and not what came of it answers half
                # the question anybody opens a history for.
                "result": "",
                "created_ts": root.ts,
                "updated_ts": root.ts,
                "updated_lamport": root.lamport,
            }
            tasks[root.id] = entry
        return entry

    for frame in frames:
        if frame.kind == "directive":
            _ensure(frame)
        elif frame.kind == "report":
            root = _root_of(frame)
            entry = _ensure(root)
            entry["reports"] += 1
            status = str((frame.body or {}).get("status") or "").strip()
            # A report without a status still says "I am on it" - the same default
            # the wire's own vocabulary would pick.
            entry["status"] = status if status in REPORT_STATUSES else "working"
            # How far it has come, from the same report that decides the status -
            # replaced only by a later report that says something, so a peer that
            # reports progress once and then only status does not lose it.
            progress = read_progress(frame.body)
            if progress is not None:
                entry["progress"] = progress
            entry["assignee"] = entry["assignee"] or frame.sender
            entry["result"] = str((frame.body or {}).get("text") or "")[:400] or entry["result"]
            entry["updated_ts"] = frame.ts
            entry["updated_lamport"] = frame.lamport

    moment = time.time() if now is None else float(now)
    for entry in tasks.values():
        if entry["assignee"]:
            entry["assignee_label"] = (labels.get(entry["assignee"])
                                       or entry["assignee"])
        # QUIET, never "finished": nobody said it ended, so the board does not say so
        # either. What it says is that nothing has been said about it for a while, and
        # a surface can then stop counting it among the work in progress without
        # anybody having to close it by hand.
        entry["quiet"] = bool(entry["status"] not in ("completed", "failed",
                                                      "rejected", "canceled")
                              and quiet_after_s > 0
                              and (moment - float(entry["updated_ts"] or 0.0)) > quiet_after_s)
        entry["silent_for_s"] = max(0.0, moment - float(entry["updated_ts"] or 0.0))
    done = ("completed", "failed", "rejected", "canceled")
    return sorted(tasks.values(),
                  key=lambda e: (e["status"] in done, -e["updated_lamport"]))
