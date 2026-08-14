# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The room on disk: one file per frame, written once, never modified.

Why not one growing file
-----------------------
Under encryption there is no append. ``data_files.write_bytes_atomic`` seals the
WHOLE payload (``VAFENC1:`` + nonce + AES-GCM over everything) and
``secure_store._atomic_write_bytes`` is temp file + fsync + ``os.replace``. An
"append" would therefore be decrypt, modify, re-encrypt, rewrite: a read-modify-write
over the entire history, once per message, with N writers racing on it.

That is the exact shape two other stores in this tree already refused, both for the
same reason (``learn_ledger``: one file per job, one writer; ``learn_job``: one
writer, one reader, own directory), and it is the failure mode the sub-agent queue
lives with, where the mutation guard degrades to an UNLOCKED read-modify-write after
five seconds.

Write-once files have no read-modify-write at all. There is no lost update to guard
against, so there is no lock to degrade. Two peers writing at the same moment touch
two different files in two different directories and BOTH survive, which is the one
thing the shared queue cannot do.

Sequence numbers come from the directory
----------------------------------------
``next_seq`` reads the highest file name in the sender's own lane and adds one. It
is deliberately NOT a counter in memory: a crash between "counter incremented" and
"file written" would tear a permanent hole in a per-sender sequence this store
promises to be gapless, and a file-only peer has no outbox to heal it from. The
directory IS the counter, so the count cannot outlive the data it counts.

What is promised
----------------
- Per-sender FIFO, gapless, exactly once at rest. A reader holding 005 and 007 KNOWS
  006 is missing, and ``gaps()`` says so rather than closing over it.
- Reads are NON-DESTRUCTIVE. The reader's position lives in the reader's own cursor
  file. This is the deliberate opposite of ``subagent_ipc.consume_result``, where the
  first reader wins and the record is gone.
- Every file goes through the same encrypted, atomic, owner-only primitive sessions
  use, so a room is protected exactly as well as a conversation is.

What is NOT promised, and the doc says so out loud: exactly-once DELIVERY, real-time
ordering between peers (``ts`` is advisory), a consistent view of membership at an
instant, or any ordering between two rooms.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from vaf.core import data_files
from vaf.core.a2a.frame import Frame, canonical_sort_key, next_lamport
from vaf.core.platform import Platform
from vaf.core.secure_store import harden_dir

# On-disk format tag. A store that writes a file a later version must recognise
# carries one; the digits are arbitrary and fixed once. Pinned as a literal in
# tests/test_persisted_format_tags.py, so changing it fails the suite rather than
# silently orphaning rooms a user already has.
ROOM_FORMAT = "a2aroom-1-7f4c1e"

# Frame files are zero-padded so the plain name sort IS the numeric sort, and wide
# enough that a room would have to outlive the machine to overflow it.
_SEQ_WIDTH = 12
_FRAME_NAME = re.compile(r"^(\d{%d})\.json$" % _SEQ_WIDTH)

# Room and peer ids arrive from outside - a foreign agent types them on a command
# line. They become path components, so they are validated rather than trusted.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class StoreError(Exception):
    """Base for refusals from the room store."""


class UnsafeName(StoreError):
    """A room or peer id that cannot be a path component.

    Rejected rather than sanitised: silently rewriting an identifier would make two
    different rooms share a directory, which is worse than refusing the name.
    """


class FrameExists(StoreError):
    """Write-once: something tried to rewrite a frame that is already on disk.

    Reaching this means two writers were acting as one peer, which the member lease
    exists to prevent. Refusing here turns a silent overwrite into a visible bug.
    """


def check_name(value: str, *, what: str = "id") -> str:
    """Validate an identifier that is about to become a path component."""
    text = str(value or "")
    if ".." in text or not _SAFE_COMPONENT.match(text):
        raise UnsafeName(f"unsafe {what}: {value!r}")
    return text


def rooms_root(base: Optional[Path] = None) -> Path:
    """Where rooms live. ``base`` exists so tests never touch the real store."""
    root = Path(base) if base else Path(Platform.vaf_dir()) / "a2a" / "rooms"
    return root


def new_room_id() -> str:
    """A room id that is safe as a path component and readable in a log line."""
    import uuid
    return "room-" + uuid.uuid4().hex[:12]


def new_peer_id() -> str:
    """A ROOM-LOCAL handle. Deliberately not a scope UUID: the scope identifies a
    tenant and must never travel in a frame, where every member could read it."""
    import uuid
    return "p-" + uuid.uuid4().hex[:10]


class RoomStore:
    """The files of one room. Knows nothing about roles or permissions."""

    def __init__(self, room_id: str, *, base: Optional[Path] = None) -> None:
        self.room_id = check_name(room_id, what="room id")
        self.root = rooms_root(base) / self.room_id

    # ── paths ───────────────────────────────────────────────────────────────

    @property
    def manifest_path(self) -> Path:
        return self.root / "room.json"

    @property
    def log_dir(self) -> Path:
        return self.root / "log"

    @property
    def members_dir(self) -> Path:
        return self.root / "members"

    @property
    def cursors_dir(self) -> Path:
        return self.root / "cursors"

    @property
    def tickets_dir(self) -> Path:
        return self.root / "tickets"

    def lane(self, peer_id: str) -> Path:
        """One peer's write-once lane. The path IS the authorship record: a frame in
        this directory was written by this peer, and no metadata can disagree."""
        return self.log_dir / check_name(peer_id, what="peer id")

    def exists(self) -> bool:
        return self.manifest_path.exists()

    # ── manifest ────────────────────────────────────────────────────────────

    def create(self, manifest: Dict[str, Any]) -> Dict[str, Any]:
        """Write room.json. Refuses to clobber an existing room."""
        if self.exists():
            raise StoreError(f"room {self.room_id!r} already exists")
        for directory in (self.root, self.log_dir, self.members_dir,
                          self.cursors_dir, self.tickets_dir):
            directory.mkdir(parents=True, exist_ok=True)
            harden_dir(directory)
        record = dict(manifest)
        record["format"] = ROOM_FORMAT
        record["room_id"] = self.room_id
        record.setdefault("created_at", time.time())
        # Written from day one even on a single machine, so the cross-machine step
        # adds a member rather than a field. An older reader ignores what it does
        # not know (frame rule 1), so this stays additive.
        record.setdefault("host", {})
        data_files.write_json_atomic(self.manifest_path, record, indent=2)
        return record

    def destroy(self) -> bool:
        """Remove this room from this machine, files and all.

        The counterpart of `create`, and it lives here for the same reason: this is the
        one place that knows every directory a room is made of, so a caller cannot
        delete four of five and leave a room that half exists.

        Deliberately NOT the same act as closing. Closing is a protocol event - a frame
        every participant reads, saying the conversation is over - and the transcript
        survives it. This removes the transcript. Both are wanted: a bin means "gone"
        everywhere else in this product, and a conversation nobody can end is a
        conversation nobody can leave behind.

        Returns whether anything was there. Never raises for an absent room: deleting
        what is already gone is the caller getting what they asked for.
        """
        import shutil

        if not self.root.exists():
            return False
        shutil.rmtree(self.root, ignore_errors=False)
        return True


    def manifest(self) -> Optional[Dict[str, Any]]:
        return data_files.read_json(self.manifest_path, default=None)

    def update_manifest(self, **fields: Any) -> Dict[str, Any]:
        """Single writer: the machine hosting the room. Never called by a peer."""
        record = self.manifest()
        if record is None:
            raise StoreError(f"room {self.room_id!r} does not exist")
        record.update(fields)
        record["format"] = ROOM_FORMAT
        data_files.write_json_atomic(self.manifest_path, record, indent=2)
        return record

    # ── frames ──────────────────────────────────────────────────────────────

    def sequence_numbers(self, peer_id: str) -> List[int]:
        """Every seq present in a peer's lane, ascending. Temp files never match:
        they are written as ``.tmp-*.part``, which fails the name pattern twice."""
        lane = self.lane(peer_id)
        if not lane.is_dir():
            return []
        found = []
        for entry in lane.iterdir():
            match = _FRAME_NAME.match(entry.name)
            if match:
                found.append(int(match.group(1)))
        return sorted(found)

    def next_seq(self, peer_id: str) -> int:
        """The directory is the counter. See the module docstring for why."""
        present = self.sequence_numbers(peer_id)
        return (present[-1] + 1) if present else 1

    def gaps(self, peer_id: str) -> List[int]:
        """Sequence numbers this peer must have written but that are not here.

        Reported rather than smoothed over: a torn write is invisible by
        construction (the reader never sees the temp file), so the gap is the ONLY
        evidence that something is missing.
        """
        present = self.sequence_numbers(peer_id)
        if not present:
            return []
        return [n for n in range(1, present[-1] + 1) if n not in set(present)]

    def peers_with_frames(self) -> List[str]:
        if not self.log_dir.is_dir():
            return []
        return sorted(p.name for p in self.log_dir.iterdir() if p.is_dir())

    def append(self, frame: Frame) -> Frame:
        """Write one frame into its sender's lane, once and forever."""
        lane = self.lane(frame.sender)
        lane.mkdir(parents=True, exist_ok=True)
        harden_dir(lane)
        path = lane / f"{frame.seq:0{_SEQ_WIDTH}d}.json"
        if path.exists():
            raise FrameExists(
                f"frame {frame.sender}/{frame.seq} already exists - two writers are "
                f"acting as one peer"
            )
        data_files.write_json_atomic(path, frame.to_dict())
        return frame

    def frames(self, peer_id: Optional[str] = None) -> List[Frame]:
        """Every frame in the room (or one lane), in canonical order.

        Non-destructive: reading changes nothing on disk. A frame that cannot be
        parsed is skipped rather than allowed to abort the whole read - one bad file
        must not make a room unreadable.
        """
        peers = [check_name(peer_id, what="peer id")] if peer_id else self.peers_with_frames()
        collected: List[Frame] = []
        for peer in peers:
            lane = self.log_dir / peer
            if not lane.is_dir():
                continue
            for entry in sorted(lane.iterdir()):
                if not _FRAME_NAME.match(entry.name):
                    continue
                payload = data_files.read_json(entry, default=None)
                if not isinstance(payload, dict):
                    continue
                try:
                    # A stored frame is READ here, never acted on, so rule 5 is off:
                    # a frame whose must_understand this reader cannot satisfy must
                    # still appear in the transcript. Dropping it would remove it from
                    # the room's history and tear the lamport chain for everything
                    # after it - the frame is rendered, and the decision not to ACT on
                    # it belongs to whoever is acting.
                    collected.append(Frame.from_dict(payload, enforce_requirements=False))
                except Exception:
                    continue
        return sorted(collected, key=canonical_sort_key)

    def read_since(self, lamport: int = 0, *, peer_id: Optional[str] = None) -> List[Frame]:
        """Frames strictly after a lamport position, in canonical order."""
        return [f for f in self.frames(peer_id) if f.lamport > int(lamport or 0)]

    def highest_lamport(self) -> int:
        frames = self.frames()
        return frames[-1].lamport if frames else 0

    def next_lamport(self) -> int:
        """One past everything this store has seen. The causality rule, applied."""
        return next_lamport(f.lamport for f in self.frames())

    # ── members: one writer per file, and that writer is the peer ───────────

    def member_path(self, peer_id: str) -> Path:
        return self.members_dir / f"{check_name(peer_id, what='peer id')}.json"

    def put_member(self, peer_id: str, record: Dict[str, Any]) -> Dict[str, Any]:
        """The peer's own record: lease, display, transport, local room mode.

        One writer per file is what makes this lock-free. A leader that must record
        something ABOUT another peer writes a frame into its own lane instead.
        """
        payload = dict(record)
        payload["peer_id"] = check_name(peer_id, what="peer id")
        payload["updated_at"] = time.time()
        data_files.write_json_atomic(self.member_path(peer_id), payload, indent=2)
        return payload

    def member(self, peer_id: str) -> Optional[Dict[str, Any]]:
        return data_files.read_json(self.member_path(peer_id), default=None)

    def members(self) -> Dict[str, Dict[str, Any]]:
        if not self.members_dir.is_dir():
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for entry in sorted(self.members_dir.iterdir()):
            if entry.suffix != ".json":
                continue
            record = data_files.read_json(entry, default=None)
            if isinstance(record, dict):
                out[entry.stem] = record
        return out

    # ── cursors: the reader's own position, never the sender's ─────────────

    def cursor(self, peer_id: str) -> int:
        record = data_files.read_json(
            self.cursors_dir / f"{check_name(peer_id, what='peer id')}.json", default=None
        )
        if isinstance(record, dict):
            try:
                return int(record.get("lamport") or 0)
            except (TypeError, ValueError):
                return 0
        return 0

    def set_cursor(self, peer_id: str, lamport: int) -> None:
        """Moved by the reader AFTER it has the frames in hand. Moving it first
        would lose a frame to any interruption between the two steps."""
        data_files.write_json_atomic(
            self.cursors_dir / f"{check_name(peer_id, what='peer id')}.json",
            {"peer_id": peer_id, "lamport": int(lamport), "updated_at": time.time()},
            indent=2,
        )

    def cursors(self) -> Dict[str, Dict[str, Any]]:
        """Every reader's position and WHEN it last moved: {peer: {lamport, updated_at}}.

        The one place the cursor file's shape is read for somebody other than its own
        peer. A consumer that globbed the directory and parsed the JSON itself would
        be a second copy of this file format, and the format is the store's to know.
        `updated_at` is what makes an engagement signal possible at all: a cursor at
        the newest frame says a peer has SEEN it, and the timestamp says how long ago
        - both facts the reader already wrote, neither invented here.
        """
        out: Dict[str, Dict[str, Any]] = {}
        try:
            files = sorted(self.cursors_dir.glob("*.json"))
        except OSError:
            return out
        for path in files:
            record = data_files.read_json(path, default=None)
            if not isinstance(record, dict):
                continue
            peer = str(record.get("peer_id") or path.stem)
            try:
                out[peer] = {"lamport": int(record.get("lamport") or 0),
                             "updated_at": float(record.get("updated_at") or 0.0)}
            except (TypeError, ValueError):
                continue
        return out

    # ── tickets ─────────────────────────────────────────────────────────────

    def put_ticket(self, ticket_id: str, record: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(record)
        payload["ticket_id"] = check_name(ticket_id, what="ticket id")
        data_files.write_json_atomic(
            self.tickets_dir / f"{payload['ticket_id']}.json", payload, indent=2
        )
        return payload

    def ticket(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        return data_files.read_json(
            self.tickets_dir / f"{check_name(ticket_id, what='ticket id')}.json", default=None
        )

    def drop_ticket(self, ticket_id: str) -> None:
        """Remove a ticket without claiming it. For expiry and cleanup only.

        A REDEMPTION must go through claim_ticket instead: this one cannot tell a
        winner from a loser, so two concurrent redeemers would both proceed.
        """
        try:
            (self.tickets_dir / f"{check_name(ticket_id, what='ticket id')}.json").unlink()
        except OSError:
            pass

    def claim_ticket(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        """Take sole ownership of a ticket, or return None because somebody else did.

        The RENAME is the gate, not a step after it. Read-then-delete lets two
        handshakes arriving at the same moment both read a valid ticket, both delete
        it (one deletion silently failing), and both join - one invitation, N members,
        which is exactly what a single-use bearer credential must not permit.

        ``os.replace`` into ``tickets/spent/`` is atomic on POSIX and on Windows, so
        exactly one caller can win. The record is read AFTER the win, from the file the
        winner now owns, because reading first would put the decision back before the
        race.
        """
        name = check_name(ticket_id, what="ticket id")
        source = self.tickets_dir / f"{name}.json"
        spent_dir = self.tickets_dir / "spent"
        try:
            spent_dir.mkdir(parents=True, exist_ok=True)
            harden_dir(spent_dir)
            os.replace(str(source), str(spent_dir / f"{name}.json"))
        except OSError:
            return None
        return data_files.read_json(spent_dir / f"{name}.json", default=None)


def list_rooms(base: Optional[Path] = None) -> List[str]:
    """Room ids present on this machine, newest activity last is NOT implied - the
    order is by name, because a listing must not depend on a clock."""
    root = rooms_root(base)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "room.json").exists())


def iter_room_stores(base: Optional[Path] = None) -> Iterable[RoomStore]:
    for room_id in list_rooms(base):
        yield RoomStore(room_id, base=base)
