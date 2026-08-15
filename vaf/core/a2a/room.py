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
from typing import Any, Dict, Iterable, List, Optional, Tuple

from vaf.core.a2a.frame import (KINDS, REPORT_STATUSES, Frame, canonical_sort_key,
                                read_progress)
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
    """

    __slots__ = ("peer_id", "display", "scope_id", "role")

    def __init__(self, peer_id: str, display: str, scope_id: Optional[str], role: str) -> None:
        self.peer_id = check_name(peer_id, what="peer id")
        self.display = str(display or peer_id)
        self.scope_id = str(scope_id) if scope_id else None
        self.role = role

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
    ) -> Identity:
        """Admit a peer and record the join in the log.

        ``role`` is a request, honoured only where the room's own rule allows it.
        Whatever a ``card`` claims is displayed, never believed: the card is a self
        description for humans and for a leader choosing workers, and it has no say
        in the fold that decides roles.
        """
        if self.closed:
            raise RoomError(f"room {self.room_id!r} is closed")
        self._check_tenant(scope_id)
        if mode not in ROOM_MODES:
            raise RoomError(f"unknown room mode {mode!r}; expected one of {ROOM_MODES}")

        resolved = self.default_role()
        if role and role in CAPABILITIES and self.kind == "chain" and not self.roles():
            resolved = role
        identity = Identity(peer_id or new_peer_id(), display, scope_id, resolved)

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
            "card": dict(card) if card else {},
        })
        joined = self.ingest(
            {"kind": "join", "to": {"room": True},
             "body": {"display": identity.display, "card": dict(card) if card else {}}},
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
        if not (self.is_host(identity) or self.role_of(identity.peer_id) == "leader"):
            raise NotPermitted(
                "only the room's host or its leader lets another account in")
        if not self.manifest.get("multi_scope"):
            raise RoomError(
                f"room {self.room_id!r} holds one account; open a shared room to let "
                "other accounts in")
        wanted = owner_tenant(tenant)
        if not wanted:
            raise RoomError("name the account to let in")
        current = [t for t in (self.manifest.get("tenants") or []) if t]
        if wanted not in current and wanted != owner_tenant(self.manifest.get("owner_scope")):
            current.append(wanted)
            self.store.update_manifest(tenants=current)
            self.manifest["tenants"] = current
        return self.tenants()

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
        choices = [str(o).strip()[:60] for o in (options or []) if str(o).strip()]
        body = {"text": str(question or "").strip()[:400],
                "options": choices or ["yes", "no"]}
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

        The resolution that COUNTS happens in `ingest`, which every ballot crosses
        whatever lane it came from. It is called here as well, and only so this
        method can word its own sentence with the option the room will record;
        resolving an already-resolved choice returns it unchanged.
        """
        options = self._vote_options(vote_id)
        resolved = str(choice or "").strip()[:60]
        if options:
            resolved = self._resolve_choice(resolved, options)
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
                return [str(o) for o in ((frame.body or {}).get("options") or [])]
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

    # ── the gate every frame passes ─────────────────────────────────────────

    def may(self, role: str, kind: str) -> bool:
        """The truth table, in one place, so no caller can hold a second copy."""
        if self.kind == "round" and kind == "directive":
            return False
        return kind in CAPABILITIES.get(role, frozenset())

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
            target = str((data.get("body") or {}).get("peer") or "")
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

        body = data.get("body") or {}
        if kind == "answer" and data.get("reply_to") and body.get("choice"):
            # A BALLOT arriving from anywhere - our own tool, a shell on this
            # machine, a peer over the wire, a third-party implementation. The
            # choice is resolved HERE rather than in each of those lanes, because
            # every lane that resolved it itself would be another place to forget:
            # measured live, the remote lane did forget, and a shortened "ja"
            # became its own column in the tally beside "ja, weiter so".
            #
            # Only when a `choice` is present, so an ordinary answer never pays for
            # the lookup. Resolving an already-resolved choice returns it unchanged,
            # so a lane that resolves first (to word its own text) costs nothing but
            # the second lookup.
            options = self._vote_options(str(data.get("reply_to") or ""))
            if options:
                body = dict(body)
                body["choice"] = self._resolve_choice(str(body["choice"]), options)

        frame = Frame.new(
            room=self.room_id,
            sender=identity.peer_id,
            role=role,
            kind=kind,
            seq=self.store.next_seq(identity.peer_id),
            lamport=self.store.next_lamport(),
            to=data.get("to") or {"room": True},
            body=body,
            reply_to=data.get("reply_to"),
            must_understand=data.get("must_understand") or (),
            ext=data.get("ext") or {},
        )
        return self.store.append(frame)

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
                    ttl_s: float = 3600.0) -> str:
        role = self.role_of(identity.peer_id) or ""
        if not role:
            raise NotAMember("only a member may invite")
        ticket_id = "t-" + secrets.token_hex(12)
        self.store.put_ticket(ticket_id, {
            "room": self.room_id,
            "display": display or "guest",
            "expires_at": time.time() + float(ttl_s),
            # Recorded for the transcript, not consulted for permission: what a guest
            # may do is decided when they act, not when they were invited.
            "minted_by": identity.peer_id,
        })
        return ticket_id

    def redeem_ticket(self, ticket_id: str, *, display: str = "",
                      mode: str = DEFAULT_MODE,
                      card: Optional[Dict[str, Any]] = None) -> Identity:
        """Spend a ticket and join. The claim IS the check, so it is single use.

        Nothing is read before the claim. Reading first would put the decision back in
        front of the race and let two handshakes arriving together both redeem the same
        invitation, which is the one thing a single-use bearer credential must not do.
        """
        record = self.store.claim_ticket(ticket_id)
        if record is None:
            raise TicketInvalid("this invitation has already been used, or does not exist")
        if str(record.get("room")) != self.room_id:
            raise TicketInvalid("this ticket is not for this room")
        if float(record.get("expires_at") or 0.0) < time.time():
            raise TicketInvalid("this ticket has expired")
        return self.join(display=display or str(record.get("display") or "guest"),
                         scope_id=None, mode=mode, card=card or {})

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
        return Identity(peer_id, display or record.get("display") or peer_id, scope_id, role)

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


def describe(entry: Dict[str, Any]) -> str:
    """A transcript row as a line a human reads.

    Bookkeeping frames carry no text - a join says who, not what - so a renderer that
    prints the body alone shows "Worker (join):" and nothing after it. The wording
    lives here, once, because four surfaces render the same transcript (the CLI's
    log, the terminal app, the classic lane and the browser) and four copies of a
    phrase are four chances to drift.
    """
    kind = str(entry.get("kind") or "")
    body = entry.get("body") or {}
    text = str(entry.get("text") or "")
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


# Membership bookkeeping. It belongs in the transcript and it is NOT worth waking a
# participant for: an agent that starts a whole turn because somebody joined is noise,
# and who is present is answered by members() whenever it does read. An unknown kind is
# deliberately absent from this set, so a newer peer's message still wakes an older one.
BOOKKEEPING_KINDS = frozenset({"join", "leave", "ack", "role"})


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
        closes_at = float(body.get("closes_at") or 0.0)
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


def fold_tasks(frames: List[Frame], *, labels: Dict[str, str]) -> List[Dict[str, Any]]:
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
            entry["updated_ts"] = frame.ts
            entry["updated_lamport"] = frame.lamport

    for entry in tasks.values():
        if entry["assignee"]:
            entry["assignee_label"] = (labels.get(entry["assignee"])
                                       or entry["assignee"])
    done = ("completed", "failed", "rejected", "canceled")
    return sorted(tasks.values(),
                  key=lambda e: (e["status"] in done, -e["updated_lamport"]))
