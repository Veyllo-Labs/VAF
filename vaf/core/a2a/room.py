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

from vaf.core.a2a.frame import KINDS, Frame
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
                         "role", "hire", "close", "leave", "ack", "join", "kick"}),
    "worker": frozenset({"say", "ask", "answer", "report",
                         "hire", "leave", "ack", "join"}),
    "peer": frozenset({"say", "ask", "answer", "leave", "ack", "join"}),
}

# How much of a room's traffic the LOCAL user has authorised their agent to act on.
# Written by the peer into its own member record, never read from a frame: an agent's
# autonomy is granted locally and can never be handed over by a remote leader.
ROOM_MODES = ("observe", "assist", "autonomous")
DEFAULT_MODE = "assist"

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
        base: Optional[Path] = None,
        room_id: Optional[str] = None,
        backlog: str = "",
        depth: int = 0,
        parent_room: Optional[str] = None,
        parent_frame: Optional[str] = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_children: int = DEFAULT_MAX_CHILDREN,
        multi_scope: bool = False,
    ) -> "Room":
        if kind not in ROOM_KINDS:
            raise RoomError(f"unknown room kind {kind!r}; expected one of {ROOM_KINDS}")
        store = RoomStore(room_id or new_room_id(), base=base)
        store.create({
            "kind": kind,
            "topic": str(topic or ""),
            "owner_scope": str(owner_scope) if owner_scope else None,
            # A late joiner in a round sees only what happened after it arrived, the
            # same rule voice_context.recent(since=...) already applies to a guest.
            "backlog": backlog or ("all" if kind == "chain" else "since_join"),
            "depth": int(depth),
            "parent_room": parent_room,
            "parent_frame": parent_frame,
            "max_depth": int(max_depth),
            "max_children": int(max_children),
            # Cross-tenant rooms are deliberately off. subagent_ipc records what a
            # shared record carrying model text across tenants costs here.
            "multi_scope": bool(multi_scope),
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
        self.ingest(
            {"kind": "join", "to": {"room": True},
             "body": {"display": identity.display, "card": dict(card) if card else {}}},
            identity=identity,
        )
        return identity

    def _check_tenant(self, scope_id: Optional[str]) -> None:
        """A room belongs to one tenant unless it says otherwise.

        A foreign agent carries no scope at all and is not a tenant, so it is not
        caught here; what bounds it is the ticket, which opens exactly one room.
        """
        if self.manifest.get("multi_scope"):
            return
        owner = owner_tenant(self.manifest.get("owner_scope"))
        scope_id = owner_tenant(scope_id)
        if owner and scope_id and scope_id != owner:
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

        host_acting = kind in ("close", "kick") and self.is_host(identity)
        if kind in KINDS and not host_acting and not self.may(role, kind):
            raise NotPermitted(f"a {role} may not emit {kind!r} in this room")

        frame = Frame.new(
            room=self.room_id,
            sender=identity.peer_id,
            role=role,
            kind=kind,
            seq=self.store.next_seq(identity.peer_id),
            lamport=self.store.next_lamport(),
            to=data.get("to") or {"room": True},
            body=data.get("body") or {},
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
               artifacts: Optional[List[Dict[str, Any]]] = None, **kw) -> Frame:
        """A report carries a STATUS from the open A2A vocabulary and may carry
        artifacts. Both live in the body, so neither costs a frame change, and an
        artifact kept out of the chat text stays findable by a machine."""
        body: Dict[str, Any] = {"text": text, "status": status}
        if artifacts:
            body["artifacts"] = list(artifacts)
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
        """
        if not self.is_host(identity):
            raise NotPermitted(
                "only the machine hosting a room can delete it; leaving is `leave`")
        if not self.closed:
            self.close(identity, reason=reason or self.TERMINATED_BY_USER)
        return self.store.destroy()

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


def unread_frames(key: str, *, base: Optional[Path] = None) -> List[Tuple[Room, Identity, List[Any], List[Any]]]:
    """What has arrived for this participant: (room, identity, WAKING, CONTEXT).

    Its OWN frames are excluded: an agent must not be woken by its own voice, which
    is the loop that turns a two-agent room into a runaway conversation. Membership
    bookkeeping is excluded for the same kind of reason, one level quieter.
    """
    pending: List[Tuple[Room, Identity, List[Any]]] = []
    for room, identity in joined_rooms(key, base=base):
        if room.closed:
            continue
        cursor = room.store.cursor(identity.peer_id)
        unread = [
            frame for frame in room.store.read_since(cursor)
            if frame.sender != identity.peer_id
            and frame.kind not in BOOKKEEPING_KINDS
        ]
        # Two lists, and the difference between them is the whole addressing rule.
        # WAKING: only what is aimed at this peer. A message for somebody else must not
        # cost a turn, or "@Bob" would wake the entire room to read a note for Bob.
        # CONTEXT: everything unread, so a peer that IS woken sees the conversation it
        # is joining rather than one line out of it - a reply written blind to what the
        # others were just told is worse than no reply.
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

