# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The delivery accelerator over a room. The files stay the record of truth.

A FRAME EXISTS WHEN ITS FILE EXISTS. The hub never originates one. When a connected
peer submits, the hub writes on that peer's behalf and answers ``committed`` only after
the write returned. No ack, no frame - and a peer that speaks only to the directory is
fully conformant, which is what makes the file lane and this one equal rather than one
being a fallback for the other.

Transport-free on purpose. There is no FastAPI, no websocket and no asyncio here: the
caller injects a ``sink(peer_id, message)`` and this module never learns what carries
it. That is what lets the same hub be driven by a websocket route, by a test with a
list, and later by whatever else - and it is why the ordering test can put two
hub-connected peers and two directory-reading peers side by side and demand the same
answer from all four.

The relationship is the house pattern rather than a new idea: the sub-agent queue
already pairs a push that wakes the consumer at once with a poll kept as the reliable
fallback. Here the poll is simply reading the room.
"""
from __future__ import annotations

import secrets
import time
from typing import Any, Callable, Dict, List, Optional

from vaf.core.a2a.frame import (
    Frame,
    FrameError,
    UnsupportedRequirement,
    UnsupportedVersion,
    canonical_sort_key,
    screen_inbound,
)
from vaf.core.a2a.room import BOOKKEEPING_KINDS, LEASE_TTL_S, Identity, Room, RoomError

# A writer lease older than this is considered abandoned, so a peer whose connection
# died can reconnect instead of being locked out by its own ghost. The same window the
# room uses to call a member stale: one number, one meaning.
WRITER_LEASE_TTL_S = LEASE_TTL_S


class NotWriter(Exception):
    """Somebody else holds this peer's writer lease.

    Refused rather than merged. Two connections acting as one peer would each derive a
    sequence number from the same directory and race for the same file name, and the
    store would refuse the second write anyway - this turns that collision into a
    sentence the caller can pass on.
    """


class Hub:
    """One room, many connections."""

    def __init__(self, room: Room, *, sink: Callable[[str, Dict[str, Any]], None],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.room = room
        self._sink = sink
        self._clock = clock
        # peer_id -> (token, expires_at). One writer per peer, per the store's contract.
        self._leases: Dict[str, tuple] = {}

    # ── connections ─────────────────────────────────────────────────────────

    def attach(self, identity: Identity, *, cursor: Optional[int] = None) -> str:
        """Take the writer lease for a peer and deliver what it missed.

        Returns the lease token. The backlog goes out through the sink, oldest first, in
        the one canonical order, followed by a ``sync`` marker naming the position the
        peer has now reached - so a reader knows when it is level rather than guessing
        from a pause in traffic.
        """
        peer_id = identity.peer_id
        held = self._leases.get(peer_id)
        if held and held[1] > self._clock():
            raise NotWriter(f"another connection is writing as {peer_id!r}")

        token = secrets.token_hex(8)
        self._leases[peer_id] = (token, self._clock() + WRITER_LEASE_TTL_S)

        position = self.room.store.cursor(peer_id) if cursor is None else int(cursor)
        # A PEER'S OWN FRAMES TRAVEL TOO, and leaving them out was the one thing a
        # reader could not check. What signing buys is that a host can omit but not
        # forge - and omitting is exactly the half a peer would want to check on
        # ITSELF: does the room still hold what I said, unaltered. A backlog that
        # skips the asker answers everything except that.
        #
        # It costs nothing in what anybody sees. The client already drops its own
        # echo unless asked for the whole transcript, and `read --all` has always
        # promised "own echo included" - a promise nothing could keep while the
        # server withheld them. Live fan-out still skips the sender: it holds the
        # ack for what it just wrote, and echoing it back would be noise.
        backlog = list(self.room.store.read_since(position))
        for frame in sorted(backlog, key=canonical_sort_key):
            self._emit(peer_id, frame.to_dict())
        highest = backlog[-1].lamport if backlog else position
        self._emit(peer_id, {"kind": "sync", "room": self.room.room_id, "lamport": highest})
        return token

    def _emit(self, peer_id: str, message: Dict[str, Any]) -> None:
        """Push to one connection. ALWAYS best effort, wherever it is called from.

        A socket that died between two frames must not take the lease, the commit or
        another peer's delivery with it. What the connection missed is in the directory,
        and that is where it looks when it comes back - one rule, so no caller has to
        remember which pushes are safe to fail.
        """
        try:
            self._sink(peer_id, message)
        except Exception:
            pass

    def renew(self, identity: Identity, token: str) -> None:
        """Push the lease out. A live connection is why a peer is not a ghost."""
        self._require_writer(identity.peer_id, token)
        self._leases[identity.peer_id] = (token, self._clock() + WRITER_LEASE_TTL_S)

    def detach(self, identity: Identity, token: str) -> None:
        held = self._leases.get(identity.peer_id)
        if held and held[0] == token:
            del self._leases[identity.peer_id]

    def _require_writer(self, peer_id: str, token: str) -> None:
        held = self._leases.get(peer_id)
        if not held or held[0] != token:
            raise NotWriter(f"{peer_id!r} is not held by this connection")
        if held[1] <= self._clock():
            del self._leases[peer_id]
            raise NotWriter(f"the writer lease for {peer_id!r} has expired")

    # ── the commit ──────────────────────────────────────────────────────────

    def submit(self, identity: Identity, token: str, payload: Dict[str, Any],
               *, understood: tuple = ()) -> Dict[str, Any]:
        """Write one frame on a peer's behalf, then tell everyone.

        The ORDER is the whole contract: the file is written first, and only a write
        that returned produces ``committed``. A crash between the two loses an
        acknowledgement, which the peer retries; the reverse would lose a message while
        claiming it had arrived.
        """
        try:
            self._require_writer(identity.peer_id, token)
        except NotWriter as e:
            return {"kind": "ack", "status": "not_writer", "reason": str(e)}

        try:
            screen_inbound(payload, understood=understood)
        except UnsupportedVersion as e:
            return {"kind": "ack", "status": "unsupported_version", "reason": str(e)}
        except UnsupportedRequirement as e:
            return {"kind": "ack", "status": "unsupported",
                    "fields": list(e.missing), "reason": str(e)}
        except FrameError as e:
            return {"kind": "ack", "status": "malformed", "reason": str(e)}

        try:
            frame = self.room.ingest(payload, identity=identity)
        except RoomError as e:
            return {"kind": "ack", "status": "refused", "reason": str(e)}

        # Committed. Everything below this line is best effort and must never be able
        # to un-commit it: a sink that throws costs delivery speed, not the message.
        self._fanout(frame)
        self.renew(identity, token)
        return {"kind": "ack", "status": "committed", "frame": frame.id,
                "seq": frame.seq, "lamport": frame.lamport}

    def _fanout(self, frame: Frame) -> None:
        payload = frame.to_dict()
        for peer_id in list(self._leases):
            if peer_id == frame.sender:
                continue                      # it wrote this; the ack is its copy
            # One broken connection is not the room's problem. It will find the frame
            # in the directory when it reconnects, which is the whole reason the files
            # rather than this loop are the record.
            self._emit(peer_id, payload)

    # ── reading ─────────────────────────────────────────────────────────────

    def catch_up(self, identity: Identity, lamport: int) -> List[Dict[str, Any]]:
        """Frames after a position, in canonical order, without moving any cursor.

        A reconnecting peer says where it got to and gets exactly the rest. Idempotent
        by construction: the files are immutable and reading takes nothing away, so a
        peer that asks twice is answered twice with the same thing.

        THE HUB IS NOT A COMPLETE FEED, and a client author has to know it. Fan-out
        carries only what passed through this hub; a peer writing straight into the
        directory - the CLI, another process, a machine that never connected - is
        invisible to a listener until it asks here or reads the files itself. That is
        the price of the files being the record rather than this object, and it is paid
        deliberately: the alternative is a hub that must be running for a room to work.
        """
        # The asker's own frames included, for the same reason the backlog carries
        # them: a peer that cannot read back what it wrote cannot check whether the
        # room still holds it.
        return [f.to_dict() for f in self.room.store.read_since(int(lamport))]

    def members(self) -> Dict[str, Dict[str, Any]]:
        """Who is in the room, and which of them this hub currently carries."""
        now = self._clock()
        out = {}
        for peer_id, record in self.room.members().items():
            held = self._leases.get(peer_id)
            out[peer_id] = dict(record, connected=bool(held and held[1] > now))
        return out

    def conversation_since(self, identity: Identity, lamport: int) -> List[Dict[str, Any]]:
        """What a human or an agent would want read to them: no bookkeeping, not their
        own voice. The same filter the CLI and the wake-up apply, so the three surfaces
        cannot disagree about what counts as something being said."""
        return [f for f in self.catch_up(identity, lamport)
                if f.get("kind") not in BOOKKEEPING_KINDS]
