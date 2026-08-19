# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A held connection to a room on another machine, turned into local files.

THE PROBLEM THIS SOLVES. The CLI is one process per command, so every command
opened its own socket and dropped it - and the server's writer lease punishes
exactly that shape: until the host notices the old socket is gone (up to the
90 second lease TTL), the next connection from the same peer is refused.
Measured in the first field use: two of seven messages arrived over
one-connection-per-command, eight of eight over a single held connection.
Reading has the same cost, because reading also needs a connection - so read
and write competed for one resource, and a conversation is exactly
read-think-answer.

THE SHAPE. One long-lived process holds ONE connection and mirrors it to disk:

    inbox.ndjson    every frame the room pushes, appended as it arrives
    outbox/*.json   drop a frame payload here; it is sent, answered with a
                    sibling .ack file, and removed - or kept with a .error
                    file when the send failed, so a retry stays possible
    status.json     pid, connected-since, counters, the last error

The other commands then read and write FILES, and a room on another machine
looks local to them.

THE LEASE IS KEPT ALIVE, not just held. The server renews the writer lease only
on a successful submit, so a held line that reads and thinks for longer than
the 90 second TTL lost its write right while staying connected and receiving -
and a conversation is exactly read-think-answer. Measured by the first foreign
agent to hold a session (a Claude agent on another machine driving this CLI):
session connected, message dropped in the outbox, refused as not_writer. The
session therefore sends a `renew` transport message on an interval, which the
host answers with `renewed` - the server half of protocol contract C9 ("leases
are renewed while attached"). A host too old to know the verb answers with a
refusal once; the session then stops asking and behaves as before the verb
existed.

The mechanic follows the field prototype by Opus (Claude Code on the reporting
machine), which sent the very messages that described it. What the prototype
left open on purpose - single-instance behaviour, liveness checks, where the
directory lives - is decided here.

ONE SESSION PER ROOM, enforced with a lock file carrying the holder's pid. Two
daemons on one seat would fight over the outbox and the server would refuse the
second connection anyway (same peer, lease held) - refusing early, with the
first holder's pid in the message, beats a silent second process that drains
nothing. A lock whose pid is dead is stale and is taken over: crashes must not
require manual cleanup.

PID LIVENESS is checked with psutil where available and os.kill(pid, 0) only on
POSIX. Never os.kill on Windows: there, any signal value outside the two
CTRL events TERMINATES the target process - a liveness probe that kills what it
probes.

The session directory lives beside the seat record (`Platform.vaf_dir()/a2a/`),
because it is the same trust domain: whoever holds the seat file may speak as
that seat, and the mirror holds nothing the seat could not read. Owner-only,
like the seat.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

# Wire kinds that are transport bookkeeping, not room content. They are not
# mirrored: the inbox is what a READER consumes, and two thirds of a held
# connection's traffic was measured to be exactly this noise.
_TRANSPORT_KINDS = frozenset({"ack", "welcome"})

# How long to keep retrying the first connect while the previous connection's
# lease drains on the server: twice the lease TTL plus slack. The value the
# server holds is LEASE_TTL_S = 90 (vaf/core/a2a/hub.py); a single TTL can be
# missed by moments when the old socket died just before we started.
CONNECT_RETRY_WINDOW_S = 200.0
CONNECT_RETRY_PAUSE_S = 15.0

# How often a held line renews its writer lease: a third of the server's 90s
# TTL (WRITER_LEASE_TTL_S in vaf/core/a2a/hub.py), so two renewals may be lost
# or arrive late before the lease lapses.
RENEW_INTERVAL_S = 30.0


class SessionBusy(RuntimeError):
    """Another live session already holds this room; carries its pid."""

    def __init__(self, pid: int) -> None:
        super().__init__(f"a session for this room is already running (pid {pid})")
        self.pid = pid


@dataclass(frozen=True)
class SessionPaths:
    base: Path
    inbox: Path
    outbox: Path
    status: Path
    lock: Path


def session_paths(room_id: str) -> SessionPaths:
    from vaf.core.a2a.store import check_name
    from vaf.core.platform import Platform
    from vaf.core.secure_store import harden_dir

    base = Platform.vaf_dir() / "a2a" / "session" / check_name(room_id)
    (base / "outbox").mkdir(parents=True, exist_ok=True)
    harden_dir(base)
    return SessionPaths(
        base=base,
        inbox=base / "inbox.ndjson",
        outbox=base / "outbox",
        status=base / "status.json",
        lock=base / "session.lock",
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil
        return bool(psutil.pid_exists(pid))
    except Exception:
        pass
    if os.name == "nt":
        # No psutil and no safe probe: os.kill on Windows would TERMINATE the
        # probed process. Claiming "alive" keeps the lock conservative - a
        # stale lock then needs --stop, which is the safe failure.
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def read_status(room_id: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(session_paths(room_id).status.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def session_pid(room_id: str) -> int:
    """The pid of a LIVE session for this room, or 0.

    Liveness is checked, never believed: a status file that survived a crash
    must not send every later command into a dead mailbox.
    """
    paths = session_paths(room_id)
    try:
        pid = int(json.loads(paths.lock.read_text(encoding="utf-8")).get("pid") or 0)
    except Exception:
        return 0
    return pid if _pid_alive(pid) else 0


def _acquire_lock(paths: SessionPaths) -> None:
    payload = json.dumps({"pid": os.getpid(), "started": time.time()})
    while True:
        try:
            fd = os.open(str(paths.lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            return
        except FileExistsError:
            try:
                holder = int(json.loads(
                    paths.lock.read_text(encoding="utf-8")).get("pid") or 0)
            except Exception:
                holder = 0
            if holder and _pid_alive(holder):
                raise SessionBusy(holder)
            # Stale: the holder is gone. Take the lock over rather than asking
            # a human to delete a file a crash left behind.
            paths.lock.unlink(missing_ok=True)


def _release_lock(paths: SessionPaths) -> None:
    try:
        holder = int(json.loads(paths.lock.read_text(encoding="utf-8")).get("pid") or 0)
        if holder == os.getpid():
            paths.lock.unlink(missing_ok=True)
    except Exception:
        pass


def _write_status(paths: SessionPaths, **fields: Any) -> None:
    payload = {"pid": os.getpid(), "updated": time.time(), **fields}
    tmp = paths.status.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(paths.status)


def drain_outbox(paths: SessionPaths,
                 submit: Callable[[dict], dict]) -> "tuple[int, int]":
    """Send every queued payload once. Returns (sent, rejected).

    The fate of a payload is decided by the ROOM'S ANSWER, never by whether the
    wire held - the wire holding is how a refusal arrives at all. First field
    use measured the difference: an ack saying ``not_writer`` was filed as
    success, the payload deleted, ``sent: 1`` counted - a rejected message that
    read as delivered to everyone watching the status file.

    - ``committed``: the ack lands beside the payload and the payload leaves.
    - ``not_writer``: the lease had lapsed; the message was turned away
      unjudged, so the payload STAYS for the next round.
    - anything else (refused, malformed, unsupported): the room judged it and
      said no - retrying repeats the refusal forever, so the payload moves
      aside as ``.rejected`` with the room's answer inside.

    A TRANSPORT failure (submit raising) still keeps the payload and re-raises:
    that send did not happen at all. An unreadable file is moved aside instead
    of retried forever.
    """
    sent = 0
    rejected = 0
    for path in sorted(paths.outbox.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            path.with_suffix(".rejected").write_text(
                f"unreadable payload: {e}", encoding="utf-8")
            path.unlink(missing_ok=True)
            continue
        try:
            ack = submit(payload)
        except Exception as e:
            path.with_suffix(".error").write_text(
                f"{type(e).__name__}: {e}", encoding="utf-8")
            raise
        status = str((ack or {}).get("status") or "")
        if status == "not_writer":
            path.with_suffix(".error").write_text(
                json.dumps(ack, ensure_ascii=False), encoding="utf-8")
            continue
        if status != "committed":
            path.with_suffix(".rejected").write_text(
                json.dumps(ack, ensure_ascii=False), encoding="utf-8")
            path.with_suffix(".error").unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            rejected += 1
            continue
        path.with_suffix(".ack").write_text(
            json.dumps(ack, ensure_ascii=False), encoding="utf-8")
        path.with_suffix(".error").unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        sent += 1
    return sent, rejected


def mirror_frames(room_id: str, *, since_lamport: int = 0) -> List[Any]:
    """The mirrored inbox as Frames, newest-comprehensible order, past a cursor.

    Malformed lines are skipped rather than fatal - the mirror is an append-only
    file a crash may have cut mid-line - and duplicates across daemon restarts
    collapse on the frame id, so a reader never sees one message twice.
    """
    from vaf.core.a2a.frame import Frame

    paths = session_paths(room_id)
    frames: List[Any] = []
    seen: set = set()
    try:
        lines = paths.inbox.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if int(data.get("lamport") or 0) <= since_lamport:
                continue
            frame = Frame.from_dict(data)
        except Exception:
            continue
        if frame.id in seen:
            continue
        seen.add(frame.id)
        frames.append(frame)
    frames.sort(key=lambda f: (f.lamport, f.id))
    return frames


def run_session(
    room_id: str,
    record: Dict[str, Any],
    *,
    connect: Optional[Callable[..., Any]] = None,
    once: bool = False,
    idle_s: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Hold the connection and serve the mailbox until stopped. Returns an exit code.

    `once` drains the backlog and the outbox and exits on the first quiet round
    - the testing mode, and the shape a cron-like caller would use.
    """
    from vaf.core.a2a.client import RemoteRefused, RemoteRoom

    paths = session_paths(room_id)
    _acquire_lock(paths)
    dial = connect or (lambda: RemoteRoom.connect(record["url"], record["seat"],
                                                  open_timeout=15))
    room = None
    try:
        deadline = clock() + CONNECT_RETRY_WINDOW_S
        attempt = 0
        while room is None:
            attempt += 1
            try:
                room = dial()
            except RemoteRefused as e:
                # Almost always the previous connection's lease still draining
                # on the server. The close arrives as a bare 1000 today, so
                # this cannot be told apart from a real refusal - a finding of
                # its own; until it is fixed, waiting the window out is the
                # honest move and the status file says what is happening.
                if clock() >= deadline:
                    _write_status(paths, connected=False,
                                  last_error=f"gave up: {e}", attempts=attempt)
                    return 1
                _write_status(paths, connected=False, waiting_for_lease=True,
                              attempts=attempt, last_error=str(e))
                sleep(CONNECT_RETRY_PAUSE_S)

        started = time.time()
        received = sent = rejected = 0
        renew_supported = True
        last_renew = clock()
        _write_status(paths, connected=True, since=started,
                      received=0, sent=0, rejected=0)

        with paths.inbox.open("a", encoding="utf-8") as inbox:
            while True:
                quiet = True
                try:
                    for message in room.frames(timeout=idle_s):
                        kind = str(message.get("kind") or "")
                        if kind == "sync":
                            break               # level with the room; serve the outbox
                        if kind in _TRANSPORT_KINDS:
                            continue
                        inbox.write(json.dumps(message, ensure_ascii=False) + "\n")
                        inbox.flush()
                        received += 1
                        quiet = False
                except TimeoutError:
                    pass                        # a quiet room is not an error
                except Exception as e:
                    _write_status(paths, connected=False, since=started,
                                  received=received, sent=sent, rejected=rejected,
                                  last_error=str(e))
                    return 1

                try:
                    _sent, _rejected = drain_outbox(
                        paths, lambda p: room.submit(p, timeout=30))
                    sent += _sent
                    rejected += _rejected
                except Exception as e:
                    _write_status(paths, connected=False, since=started,
                                  received=received, sent=sent, rejected=rejected,
                                  last_error=str(e))
                    return 1

                # Keep the lease alive while the line lives (contract C9). The
                # host renews on successful submits only, and a conversation is
                # read-think-answer - thinking outlasts the 90s TTL, so a quiet
                # session lost its write right while connected and receiving.
                if renew_supported and clock() - last_renew >= RENEW_INTERVAL_S:
                    last_renew = clock()
                    try:
                        ack = room.submit({"kind": "renew"}, timeout=10) or {}
                    except Exception as e:
                        _write_status(paths, connected=False, since=started,
                                      received=received, sent=sent,
                                      rejected=rejected, last_error=str(e))
                        return 1
                    status = str(ack.get("status") or "")
                    if status == "not_writer":
                        # The lease is gone and this line cannot take it back -
                        # exit, so a restart re-attaches cleanly and ITS cursor
                        # decides the backlog. Limping on connected-but-mute
                        # is exactly the state this keepalive exists to end.
                        _write_status(paths, connected=False, since=started,
                                      received=received, sent=sent,
                                      rejected=rejected,
                                      last_error="writer lease lost; "
                                                 "restart the session")
                        return 1
                    if status != "renewed":
                        # A host too old to know the verb refuses it once; from
                        # here the session behaves as before the verb existed
                        # (leases renew on submits only) instead of asking a
                        # question the host will refuse every 30 seconds.
                        renew_supported = False

                _write_status(paths, connected=True, since=started,
                              received=received, sent=sent, rejected=rejected,
                              **({} if renew_supported
                                 else {"lease_keepalive": "unsupported by host"}))
                if once and quiet:
                    return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if room is not None:
            try:
                room.close()        # the clean close is what frees the lease promptly
            except Exception:
                pass
        try:
            status = read_status(room_id) or {}
            status.update(connected=False)
            _write_status(paths, **{k: v for k, v in status.items()
                                    if k not in ("pid", "updated")})
        except Exception:
            pass
        _release_lock(paths)
