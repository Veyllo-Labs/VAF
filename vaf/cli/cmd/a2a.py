# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf a2a`: the door a foreign agent walks through.

Claude Code, Codex, OpenCode or anything else with a shell joins a room here. The
whole contract is a command line and NDJSON on stdout, because that is what a program
driving another program can actually consume - the same shape
`examples/03_stream_json_subprocess.py` already documents for the agent itself.

Two decisions that look small and are not:

`wait` is the most used line of the whole protocol, since a foreign agent blocks on it
between turns, so its behaviour is spelled out rather than implied. It polls (a peer may
be a process that cannot signal this one), it takes a timeout whose expiry is its OWN
exit code rather than an error, it prints a `close` frame before ending, and it moves
the read cursor only AFTER the line is on stdout - an interruption then costs a repeat
instead of a lost message.

There is deliberately no `--scope` flag. Identity here is the machine owner's, because
anyone who can run `vaf a2a` can run `vaf`; the CLI cannot be stricter than the
operating system, and a flag that pretended otherwise would only invite somebody to
pass another tenant's scope. A test asserts the flag's absence.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from typing import List, Optional

import typer

app = typer.Typer(help="Agent-to-agent rooms: join, talk, read")

# Exit codes, so a script or an agent can branch without parsing text.
EXIT_OK = 0
EXIT_ERROR = 1          # something went wrong
EXIT_REFUSED = 2        # the room said no (role, kind, budget, ticket)
EXIT_NO_ROOM = 3        # no such room, or you are not in it
EXIT_TIMEOUT = 4        # `wait` ran out of time: NOT an error, just nothing arrived
EXIT_CLOSED = 5         # `wait` ended because the room was closed

_stop = False


def _install_stop_handler() -> None:
    """A frame being written must finish. Half an NDJSON line is worse than none."""
    def _handler(_signum, _frame):
        global _stop
        _stop = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, AttributeError, OSError):
            pass


#: How an AGENT's shell says which lane it is acting on, and in WHICH room.
#:
#: Format: ``<room_id>|<participant key>``. Both halves matter. The key alone would let
#: a call that outlives its turn keep speaking as the agent in a room it has nothing to
#: do with; bound to one room, a later call elsewhere simply does not match and falls
#: back to the ordinary answer.
#:
#: This grants nothing new: anyone who can set this variable can already run `vaf` on
#: this machine, which is the whole reason there is no `--scope` flag. What it fixes is
#: an ATTRIBUTION. An agent that has its own handle in a room was writing under the
#: machine owner's whenever it reached for a shell instead of its own tool, so the room
#: recorded its work as the owner's - measured live, eight reports in a row, and with a
#: task board that now names who did what, that stopped being a cosmetic difference.
from vaf.core.a2a.room import ROOM_ACTOR_ENV as ACTOR_ENV   # noqa: E402  (one home for the name)


def _key(room_id: str = "") -> str:
    """Who is acting: the machine owner from the TERMINAL lane, unless a room-bound
    actor was handed down.

    The lane is what keeps this apart from the same owner's agent, which is a different
    actor in a room even though it is the same account. See the module docstring for
    why there is no way to name another account here - and `ACTOR_ENV` above for why
    naming a LANE, bound to one room, is a different question from naming a scope.
    """
    from vaf.core.a2a.room import participant_key, resolve_room_actor

    handed = resolve_room_actor(room_id)
    if handed:
        return handed
    try:
        return participant_key("cli")
    except Exception:
        return "cli:local"


def _display() -> str:
    """The name this machine's owner appears under, when they did not give one.

    It used to be the literal "terminal", which is a lane and not a person - so the
    room showed the machine owner as "terminal" beside agents that had names. The
    account already knows what to call them.
    """
    try:
        from vaf.core.config import get_local_admin_username
        return str(get_local_admin_username() or "").strip() or "terminal"
    except Exception:
        return "terminal"


def _scope() -> str:
    """The TENANT this terminal acts for, which is what a room records as its owner.

    Deliberately not `_key()`: that is a PARTICIPANT key, lane included, and the two
    are one prefix apart. Handing the participant key where a tenant is expected
    produces a room whose derived host handles belong to nobody.
    """
    from vaf.core.config import get_local_admin_scope_id
    return str(get_local_admin_scope_id() or "local")


def _self_card(skills: str = "") -> dict:
    """What this participant tells the room about itself.

    Empty when nothing was said rather than filled with a guess: a terminal peer may be
    a person, a script or somebody else's agent, and inventing abilities for it would
    put a claim in the transcript that nobody made.
    """
    described = str(skills or "").strip()
    return {"kind": "terminal", "skills": described[:400]} if described else {}


def _hint(message: str) -> None:
    """A nudge for whoever is reading the terminal, on STDERR.

    Never stdout: this CLI promises one JSON object per line there, and a machine
    peer parsing that stream would break on a sentence. stderr reaches a human or
    an agent reading its own tool output, and is invisible to a pipe.
    """
    typer.echo(message, err=True)


def _fail(message: str, code: int) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _room(room_id: str):
    from vaf.core.a2a.room import Room
    from vaf.core.a2a.store import StoreError, UnsafeName
    try:
        return Room.open(room_id)
    except UnsafeName:
        _fail(f"'{room_id}' is not a valid room id.", EXIT_REFUSED)
    except StoreError:
        _fail(f"There is no room '{room_id}' on this machine.", EXIT_NO_ROOM)


def _open_local(room_id: str):
    """The room if it lives on THIS machine, None if it does not, refusal if the id
    is not an id. The verbs that also speak the wire go through this instead of
    _room, so 'not here' can mean 'try the seat' instead of the exit code."""
    from vaf.core.a2a.room import Room
    from vaf.core.a2a.store import StoreError, UnsafeName
    try:
        return Room.open(room_id)
    except UnsafeName:
        _fail(f"'{room_id}' is not a valid room id.", EXIT_REFUSED)
    except StoreError:
        return None


# ── the remote lane: rooms that live on ANOTHER machine ─────────────────────
#
# One file per room under ~/.vaf/a2a/remote/, owner-only, holding the url, the
# peer this machine is in that room, the SEAT (the durable way back in that the
# ticket redemption handed over exactly once) and the reading position. The seat
# is a bearer secret, which is why the directory is hardened and the file 0600 -
# the same standard the trust anchors next door live by.


def _remote_dir():
    from vaf.core.platform import Platform
    from vaf.core.secure_store import harden_dir
    directory = Platform.vaf_dir() / "a2a" / "remote"
    directory.mkdir(parents=True, exist_ok=True)
    harden_dir(directory)
    return directory


def _remote_path(room_id: str):
    from vaf.core.a2a.store import check_name
    return _remote_dir() / f"{check_name(room_id)}.json"


def _remote_record(room_id: str):
    try:
        with open(_remote_path(room_id), "r", encoding="utf-8") as fh:
            record = json.load(fh)
        return record if isinstance(record, dict) and record.get("seat") else None
    except Exception:
        return None


def _remote_save(room_id: str, record: dict) -> None:
    path = _remote_path(room_id)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _remote_forget(room_id: str) -> None:
    try:
        _remote_path(room_id).unlink(missing_ok=True)
    except Exception:
        pass


def _remote_fail(e) -> None:
    """One refusal shape for the wire. The close codes come from the server's door;
    what a script needs is the same exit codes the local lane uses."""
    code = int(getattr(e, "code", 0) or 0)
    if code == 4004:
        _fail(str(e), EXIT_NO_ROOM)
    if code in (4001, 4003, 4009):
        _fail(str(e), EXIT_REFUSED)
    _fail(str(e), EXIT_ERROR)


def _remote_send(record: dict, room_id: str, kind: str, text: str, *,
                 to_peer: str = "", reply_to: str = "", status: str = "",
                 progress: dict = None, files: list = None) -> None:
    from vaf.core.a2a.client import RemoteRefused, RemoteRoom
    from vaf.core.a2a.trust import TrustRefused

    body = {"text": text}
    if status:
        body["status"] = status
    if progress:
        body["progress"] = progress
    if files:
        body["files"] = files
    payload = {"kind": kind, "body": body}
    if to_peer:
        payload["to"] = {"peer": to_peer}
    # Deliberate: no @-mention resolution on this lane. The member table lives on
    # the host, and a leading "@Name" sent from here travels as TEXT everyone can
    # read - addressing ONE member remotely is `--to <peer-id>`, taken from the
    # line you are answering. A half-resolved mention would be worse than none:
    # it would sometimes wake the wrong agent and never say so.
    if reply_to:
        from vaf.core.a2a.frame import plausible_frame_id
        if not plausible_frame_id(reply_to):
            _fail("reply_to takes the ID of the message you answer (its 'id' field), "
                  "never its text.", EXIT_REFUSED)
        payload["reply_to"] = reply_to

    try:
        with RemoteRoom.connect(record["url"], record["seat"]) as remote:
            ack = remote.submit(payload)
    except (TrustRefused, RemoteRefused) as e:
        _remote_fail(e)
    _remote_ack(ack, room_id, kind)


def _remote_ack(ack: dict, room_id: str, kind: str) -> None:
    """What the room made of a submitted frame, said once for every remote verb.

    One place rather than one per verb, because the two that existed disagreed:
    the second read the frame id from `ack["id"]`, which the hub does not send
    (it commits under `ack["frame"]`), and never looked at `status` at all - so a
    REFUSED remote ballot printed `{"ok": true}` and exited 0. An agent reading
    that believes it voted, and then gets reminded about a vote it thinks it
    answered.
    """
    ack = ack or {}
    if ack.get("status") == "committed":
        _emit({"ok": True, "id": ack.get("frame"), "room": room_id, "kind": kind,
               "lamport": ack.get("lamport"), "seq": ack.get("seq"), "remote": True})
        if kind == "leave":
            # The seat's member is gone; a kept record would only turn the next
            # command into a slower refusal.
            _remote_forget(room_id)
        return
    reason = str(ack.get("reason") or ack.get("status") or "refused")
    _fail(f"The room refused it: {reason}",
          EXIT_REFUSED if ack.get("status") in ("refused", "not_writer") else EXIT_ERROR)


def _remote_frames(record: dict) -> list:
    """Everything the seat may read in a room on another machine, as Frames.

    The backlog up to the sync marker, which is the whole room as far as a reader
    is concerned. Shared by every remote board (tasks, votes) because a second
    copy of this loop would be a second set of decisions about which wire
    messages are frames.
    """
    from vaf.core.a2a.client import RemoteRefused, RemoteRoom
    from vaf.core.a2a.frame import Frame
    from vaf.core.a2a.trust import TrustRefused

    frames = []
    try:
        with RemoteRoom.connect(record["url"], record["seat"]) as remote:
            for message in remote.frames(timeout=2.0):
                kind = str(message.get("kind") or "")
                if kind in ("sync", "ack", "welcome") or "lamport" not in message:
                    if kind == "sync":
                        break
                    continue
                try:
                    frames.append(Frame.from_dict(message))
                except Exception:
                    continue
    except TimeoutError:
        pass
    except (TrustRefused, RemoteRefused) as e:
        _remote_fail(e)
    return frames


def _remote_read_frames(room_id: str, record: dict) -> list:
    """Frames for a remote READ: the session mirror when a daemon holds the
    room, one wire connection otherwise.

    The mirror is free and instant - the daemon already paid for the frames as
    they arrived. The one-shot fallback works without a daemon but pays the
    connection (and, until the server-side lease release lands, may have to
    wait a previous connection's lease out). Both return the same Frames, so
    the verbs cannot drift on which lane served them.
    """
    try:
        from vaf.core.a2a.session import mirror_frames, session_pid
        if session_pid(room_id):
            return mirror_frames(room_id)
    except Exception:
        pass
    return _remote_frames(record)


def _remote_rows(record: dict, frames: list, *, membership: bool) -> list:
    """Frames as the NDJSON rows `read` prints, one shape with the wait lane."""
    from vaf.core.a2a.room import BOOKKEEPING_KINDS

    labels = _remote_labels(frames)
    rows = []
    for frame in sorted(frames, key=lambda f: (f.lamport, f.id)):
        if frame.sender == record.get("peer"):
            continue                     # own echo is not news
        if frame.kind in BOOKKEEPING_KINDS and not membership:
            continue
        if frame.kind == "ping":
            continue
        body = frame.body or {}
        rows.append({"id": frame.id, "peer": frame.sender,
                     "display": labels.get(frame.sender, frame.sender),
                     "role": frame.role, "kind": frame.kind,
                     "text": str(body.get("text") or ""), "body": body,
                     "lamport": frame.lamport, "ts": frame.ts,
                     "reply_to": frame.reply_to, "remote": True})
    return rows


def _remote_labels(frames: list) -> dict:
    """Who is who, from the log alone: the display name each peer joined under.

    The host resolves labels against its member files and disambiguates two agents
    called "Codex" with a tag; a reader on the wire has neither. It uses what the
    join frames say and falls back to the peer id, rather than inventing a second
    tagging scheme that would disagree with the host's on the first collision.
    """
    out: dict = {}
    for frame in sorted(frames, key=lambda f: f.lamport):
        if frame.kind == "join":
            display = str((frame.body or {}).get("display") or "").strip()
            if display:
                out[frame.sender] = display
    return out


def _remote_members(frames: list) -> list:
    """Who is still in the room, folded from join and leave the way roles are."""
    present: dict = {}
    for frame in sorted(frames, key=lambda f: f.lamport):
        if frame.kind == "join":
            present[frame.sender] = True
        elif frame.kind == "leave":
            present.pop(frame.sender, None)
    return sorted(present)


def _remote_tasks(record: dict) -> list:
    """The task board for a room on another machine.

    Folded from the frames the seat may read, with the SAME function the host
    uses - a second fold would be a second opinion about what "working" means,
    and the two would drift on the first status somebody adds.
    """
    from vaf.core.a2a.room import fold_tasks
    return fold_tasks(_remote_frames(record), labels={})


def _remote_votes(record: dict) -> list:
    """Every vote in a room on another machine, with the same fold the host uses.

    Until this existed a remote peer could open a vote and cast a ballot but never
    read the tally, because the fold was a method on a store. With a deadline and
    an abstention in it, that gap stops being an inconvenience: an agent would be
    counted as abstaining from a question it had no way to look up.
    """
    from vaf.core.a2a.room import fold_votes
    frames = _remote_frames(record)
    return fold_votes(frames, labels=_remote_labels(frames),
                      members=_remote_members(frames))


def _remote_submit(record: dict, room_id: str, payload: dict) -> None:
    """Submit one frame over the wire and print what the room made of it.

    The verbs that already had a remote path go through _send; these two carry
    bodies that _send has no arguments for, and inventing two more arguments for
    one caller each would grow that signature for nothing. The ANSWER is read by
    the same `_remote_ack` either way - the two used to differ, and the weaker one
    reported refusals as success.
    """
    from vaf.core.a2a.client import RemoteRefused, RemoteRoom
    from vaf.core.a2a.trust import TrustRefused
    try:
        with RemoteRoom.connect(record["url"], record["seat"]) as remote:
            ack = remote.submit(payload)
    except (TrustRefused, RemoteRefused) as e:
        _remote_fail(e)
        return
    _remote_ack(ack, room_id, str(payload.get("kind") or ""))


def _remote_wait(record: dict, room_id: str, *, n: int, timeout: float,
                 membership: bool) -> None:
    from vaf.core.a2a.client import RemoteRefused, RemoteRoom
    from vaf.core.a2a.room import BOOKKEEPING_KINDS
    from vaf.core.a2a.trust import TrustRefused

    # The ask travels with the lane, not with the machine: a remote peer that
    # never said what it can do is exactly as invisible in the roster as a local
    # one. Read from the handshake kept at join time, so it costs no round trip.
    if not ((record.get("welcome") or {}).get("you") or {}).get("card"):
        _hint(f"You have not said what you can do in this room yet:\n"
              f"  vaf a2a introduce {room_id} --skills \"what you are good at\"")
    started = time.monotonic()
    cursor = int(record.get("cursor") or 0)
    seen = 0
    try:
        with RemoteRoom.connect(record["url"], record["seat"]) as remote:
            frames = remote.frames(timeout=1.0)
            while not _stop:
                if timeout and (time.monotonic() - started) >= timeout:
                    raise typer.Exit(EXIT_TIMEOUT)
                try:
                    message = next(frames)
                except StopIteration:
                    # The server hung up without a close frame: something is wrong,
                    # not something finished.
                    raise typer.Exit(EXIT_ERROR)
                except TimeoutError:
                    continue
                kind = str(message.get("kind") or "")
                if kind in ("sync", "ack", "welcome") or "lamport" not in message:
                    continue
                if int(message.get("lamport") or 0) <= cursor:
                    continue                     # backlog this seat already printed
                if message.get("from") == record.get("peer"):
                    continue                     # own echo is not news
                row = {"id": message.get("id"), "peer": message.get("from"),
                       "role": message.get("role"), "kind": kind,
                       "text": str((message.get("body") or {}).get("text") or ""),
                       "body": message.get("body") or {},
                       "lamport": message.get("lamport"), "ts": message.get("ts"),
                       "reply_to": message.get("reply_to"), "remote": True}
                if kind == "close":
                    _emit(row)
                    raise typer.Exit(EXIT_CLOSED)
                if kind in BOOKKEEPING_KINDS and not membership:
                    continue
                _emit(row)
                # AFTER the line exists on stdout, never before: an interrupted wait
                # costs a repeated line, not a swallowed one - the store's own rule.
                cursor = int(message.get("lamport") or cursor)
                record["cursor"] = cursor
                _remote_save(room_id, record)
                seen += 1
                if seen >= n:
                    raise typer.Exit(EXIT_OK)
    except (TrustRefused, RemoteRefused) as e:
        _remote_fail(e)
    raise typer.Exit(EXIT_OK if seen else EXIT_TIMEOUT)


def _me(room, *, required: bool = True, as_peer: str = ""):
    """Which member of this room is acting.

    By default the machine owner's derived handle. A guest that joined with an
    invitation got a handle of its own, so it names it with ``--as`` or by exporting
    ``VAF_A2A_PEER`` once - without that a foreign agent could join a room and then be
    unable to say anything in it, which the first live run found immediately.

    On one machine this grants nothing new: everyone here is already the same operating
    system user, so the CLI cannot be stricter than the OS. It never crosses a network -
    a remote peer's identity comes from its connection, not from a flag it sends.
    """
    import os
    from vaf.core.a2a.room import Identity

    wanted = str(as_peer or os.environ.get("VAF_A2A_PEER") or "").strip()
    if wanted:
        role = room.role_of(wanted)
        if not role:
            if required:
                _fail(f"'{wanted}' is not a member of '{room.room_id}'.", EXIT_NO_ROOM)
            return None
        record = room.store.member(wanted) or {}
        return Identity(wanted, record.get("display") or wanted, None, role)

    identity = room.identity_for(_key(room.room_id))
    if identity is None and required:
        _fail(f"You have not joined '{room.room_id}'. Run: vaf a2a join {room.room_id}",
              EXIT_NO_ROOM)
    return identity


def _emit(row: dict) -> None:
    """One frame, one line, flushed. A reader on the other end is blocking on it."""
    sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _send(room_id: str, kind: str, text: str, *, to_peer: str = "",
          reply_to: str = "", status: str = "", as_peer: str = "",
          progress: dict = None, files: tuple = ()) -> None:
    from vaf.core.a2a.room import RoomError, attached_files
    room = _open_local(room_id)
    named = attached_files({"files": list(files or ())})
    if room is None:
        record = _remote_record(room_id)
        if record is None:
            _fail(f"There is no room '{room_id}' on this machine.", EXIT_NO_ROOM)
        return _remote_send(record, room_id, kind, text, to_peer=to_peer,
                            reply_to=reply_to, status=status, progress=progress,
                            files=named)
    identity = _me(room, as_peer=as_peer)
    body = {"text": text}
    if status:
        body["status"] = status
    if progress:
        body["progress"] = progress
    if named:
        body["files"] = named
    # An explicit --to, else a leading "@Name", else everyone: the ROOM answers, the
    # one place that knows who is in it. The same call the browser and the agent's
    # tool make, so the three lanes cannot disagree about who a line was aimed at.
    payload = {"kind": kind, "body": body, "to": room.addressee(text, to_peer=to_peer)}
    if reply_to:
        from vaf.core.a2a.frame import plausible_frame_id
        if not plausible_frame_id(reply_to):
            _fail("reply_to takes the ID of the message you answer (its 'id' field), "
                  "never its text.", EXIT_REFUSED)
        payload["reply_to"] = reply_to
    try:
        frame = room.ingest(payload, identity=identity)
    except RoomError as e:
        _fail(str(e), EXIT_REFUSED)
    _emit({"ok": True, "id": frame.id, "room": room_id, "kind": kind,
           "lamport": frame.lamport, "seq": frame.seq})


# ── rooms ───────────────────────────────────────────────────────────────────

@app.command()
def create(
    kind: str = typer.Option("round", help="round (nobody commands) or chain (leader and workers)"),
    topic: str = typer.Option("", help="What the room is for, in a few words."),
    mission: str = typer.Option("", help="What the room is for at LENGTH: the thing every member is reminded of when it comes back after an hour."),
    display: str = typer.Option("", help="Your name in the room."),
    skills: str = typer.Option("", help="One line about what you can do. Everyone in the room sees it."),
    room_id: str = typer.Option("", "--id", help="Use this id instead of a generated one."),
    shared: bool = typer.Option(False, "--shared",
                                help="Let OTHER accounts on this machine be let into this room. Every member then reads everything said in it; admit each account with `vaf a2a share`."),
) -> None:
    """Open a room and join it."""
    from vaf.core.a2a.room import Room, RoomError, derive_peer_id
    from vaf.core.a2a.room import just_opened
    from vaf.core.a2a.store import StoreError

    # The same repeat guard the agent's tool carries, for the same reason: a caller
    # that opens one topic twice within minutes almost always lost track of the first
    # one, and naming it is more useful than making a second. A room id given
    # explicitly is a deliberate act and is never second-guessed.
    if not room_id:
        already = just_opened(_key(), topic)
        if already:
            _fail(f"you opened a room for {topic!r} a moment ago: {already!r}. Use that "
                  f"one, or give this one a topic of its own.", EXIT_REFUSED)

    try:
        # The TENANT, not this lane's participant key. They differ by a prefix and the
        # difference is invisible until something derives from it: a room whose owner
        # was recorded as "cli:<scope>" has host handles nobody holds, so it has no
        # host at all - its own opener cannot close it and cannot remove anybody.
        room = Room.create(kind=kind, owner_scope=_scope(), topic=topic, mission=mission,
                           room_id=room_id or None, multi_scope=shared)
        me = room.join(display=display or _display(),
                       peer_id=derive_peer_id(_key(), room.room_id), scope_id=_scope(),
                       card=_self_card(skills), participant_key=_key())
    except (RoomError, StoreError) as e:
        _fail(str(e), EXIT_REFUSED)
    _emit({"ok": True, "room": room.room_id, "kind": room.kind,
           "peer": me.peer_id, "role": me.role, "shared": bool(shared)})


@app.command()
def share(
    room_id: str = typer.Argument(..., help="A room opened with --shared."),
    account: str = typer.Argument(..., help="The account id to let in."),
) -> None:
    """Let another ACCOUNT on this machine into a shared room.

    Everything said in such a room is readable by every member, so this is a decision
    about a conversation and not a convenience: the account is named here, and only a
    named account can join. Knowing the room's id is not enough, and was never meant
    to be - an id travels in invitations, in prompts and in log lines.
    """
    from vaf.core.a2a.room import RoomError
    room = _room(room_id)
    identity = _me(room)
    try:
        admitted = room.admit(identity, account)
    except RoomError as e:
        _fail(str(e), EXIT_REFUSED)
    # For the administrator's log, and emitted HERE rather than inside the room: an
    # embedder building on `Room.admit` should not find itself writing into VAF's
    # security log, and the actor is known at this layer and not at that one.
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("room_account_admitted", username=_display(),
                           path=room.room_id,
                           detail=f"account {account} may now read room {room.room_id}")
    except Exception:
        pass
    _emit({"ok": True, "room": room.room_id, "accounts": admitted})


@app.command(name="list")
def list_rooms() -> None:
    """List the rooms on this machine you are a member of."""
    from vaf.core.a2a.room import joined_rooms, unread_counts
    key = _key()
    pending = unread_counts(key)
    for room, identity in joined_rooms(key):
        _emit({"room": room.room_id, "kind": room.kind, "topic": room.manifest.get("topic", ""),
               "role": identity.role, "peer": identity.peer_id,
               "unread": pending.get(room.room_id, 0),
               "mode": room.mode_of(identity.peer_id), "closed": room.closed})
    # The rooms waiting for THIS account's answer, after the ones it is in. Not a
    # member yet, so no role, no peer and no unread count: the line names the door.
    from vaf.core.a2a.room import invited_rooms
    for room, invitation in invited_rooms(_scope()):
        _emit({"room": room.room_id, "kind": room.kind, "topic": room.manifest.get("topic", ""),
               "invited": True, "invited_by": invitation.get("minted_by_label", ""),
               "invitation": invitation.get("id", ""),
               "expires_at": invitation.get("expires_at"),
               "accept": f"vaf a2a accept {room.room_id}"})


@app.command()
def invite(
    room_id: str = typer.Argument(..., help="Room to invite into."),
    display: str = typer.Option("guest", help="Name the guest will appear under."),
    ttl: int = typer.Option(3600, help="Seconds the invitation stays valid."),
    account: str = typer.Option(
        "", "--account",
        help="Invite an ACCOUNT on this machine by its user name instead of a foreign "
             "agent. The account sees the room in its own sidebar and accepts or "
             "declines there; nothing is readable until it accepts."),
    shared: bool = typer.Option(
        False, "--shared",
        help="With --account on a room that holds one account: open the room to other "
             "accounts first. Every member then reads everything said in it."),
) -> None:
    """Mint a single-use invitation and print the line, and the briefing, to hand over.

    The invitation is assembled by the room layer, not here: the agent's own room tool
    hands out the same thing when somebody says "open a room and invite Codex", and two
    agents told different things by two inviters is the failure that would follow from
    building it twice.

    With --account there is no briefing to hand over: the invitation IS the row in the
    other account's sidebar, and the answer comes back as `accepted` or `declined` in
    `vaf a2a invitations`.
    """
    from vaf.core.a2a.invite import invitation
    from vaf.core.a2a.room import RoomError
    room = _room(room_id)
    identity = _me(room)
    if account:
        from vaf.core.config import scope_id_for_username
        scope = scope_id_for_username(account)
        if not scope:
            _fail(f"There is no account called {account!r} on this machine.", EXIT_REFUSED)
        try:
            if not room.manifest.get("multi_scope"):
                if not shared:
                    _fail(f"Room {room_id!r} holds one account. Pass --shared to open it "
                          "to other accounts; every member then reads everything said "
                          "in it.", EXIT_REFUSED)
                room.open_to_accounts(identity)
            row = room.invite_account(identity, scope, display=account, ttl_s=float(ttl))
        except RoomError as e:
            _fail(str(e), EXIT_REFUSED)
        # The bell and the sidebar refetch reach a browser only from the process that
        # serves it; from this terminal the security event is what always lands, and
        # the invitee's row appears with their next sidebar refresh, exactly like a
        # room opened from this terminal.
        try:
            from vaf.core.web_interface import announce_room_invitation
            announce_room_invitation(room, row, inviter_scope=_scope(), invitee_scope=scope,
                                     inviter_name=_display())
        except Exception:
            pass
        _emit({"ok": True, "room": room.room_id, "account": account, "invitation": row})
        return
    try:
        row = invitation(room, identity, display=display, ttl_s=float(ttl))
    except RoomError as e:
        _fail(str(e), EXIT_REFUSED)
    _emit({"ok": True, **row})


@app.command()
def invitations(
    room_id: str = typer.Argument(..., help="A room you are in."),
    text: str = typer.Option("", "--text",
                             help="Print the briefing of this OPEN agent invitation again "
                                  "instead of the list."),
) -> None:
    """Every invitation this room handed out, and what became of each: pending,
    accepted (by whom), declined, revoked or expired. Accounts and agents in one list."""
    from vaf.core.a2a.room import RoomError
    room = _room(room_id)
    identity = _me(room)
    try:
        rows = room.invitations(identity)
    except RoomError as e:
        _fail(str(e), EXIT_REFUSED)
    if text:
        from vaf.core.a2a.invite import invitation_text
        record = next((r for r in rows if r["id"] == text), None)
        if record is None:
            _fail(f"No invitation {text!r} in room {room_id!r}.", EXIT_REFUSED)
        try:
            _emit({"ok": True, "room": room.room_id, "invitation": record["id"],
                   "briefing": invitation_text(room, record)})
        except ValueError as e:
            _fail(str(e), EXIT_REFUSED)
        return
    for row in rows:
        _emit(row)


@app.command()
def accept(
    room_id: str = typer.Argument(..., help="A room that invited this account."),
    display: str = typer.Option("", help="Your name in the room."),
) -> None:
    """Accept an invitation into a room: you are admitted and join as yourself."""
    from vaf.core.a2a.room import RoomError, TicketInvalid
    room = _room(room_id)
    try:
        me = room.accept_invitation(_scope(), display=display or _display())
    except (TicketInvalid, RoomError) as e:
        _fail(str(e), EXIT_REFUSED)
    _emit({"ok": True, "room": room.room_id, "peer": me.peer_id, "role": me.role})


@app.command()
def decline(
    room_id: str = typer.Argument(..., help="A room that invited this account."),
) -> None:
    """Decline an invitation into a room. The inviter reads "declined", not silence."""
    from vaf.core.a2a.room import RoomError, TicketInvalid
    room = _room(room_id)
    try:
        row = room.decline_invitation(_scope())
    except (TicketInvalid, RoomError) as e:
        _fail(str(e), EXIT_REFUSED)
    _emit({"ok": True, "room": room.room_id, "invitation": row["id"], "status": row["status"]})


@app.command()
def revoke(
    room_id: str = typer.Argument(..., help="A room you are in."),
    invitation_id: str = typer.Argument(..., help="The invitation to withdraw (from `invitations`)."),
) -> None:
    """Withdraw an invitation that has not been answered yet. Whoever minted it, or
    the room's host or leader."""
    from vaf.core.a2a.room import RoomError
    room = _room(room_id)
    identity = _me(room)
    try:
        row = room.revoke_invitation(identity, invitation_id)
    except RoomError as e:
        _fail(str(e), EXIT_REFUSED)
    _emit({"ok": True, "room": room.room_id, "invitation": row["id"], "status": row["status"]})


@app.command()
def join(
    room_id: str = typer.Argument(..., help="Room to join."),
    ticket: str = typer.Option("", help="Invitation, if the room was not opened by you."),
    display: str = typer.Option("", help="Your name in the room."),
    skills: str = typer.Option("", help="One line about what you can do. Everyone in the room sees it."),
    mode: str = typer.Option(
        "assist",
        help="How far VAF's own agent may act on messages here: observe, assist or autonomous.",
    ),
    url: str = typer.Option(
        "", "--url",
        help="The room's wss address on the machine that hosts it (from the "
             "invitation). Joins over the wire; pin the host's authority first "
             "with `vaf a2a trust`."),
) -> None:
    """Join a room, with an invitation if you were given one."""
    from vaf.core.a2a.room import RoomError, TicketInvalid, derive_peer_id

    if url:
        # The room lives on another machine. The ticket buys ONE connection; what
        # this join keeps is the SEAT the welcome hands over, which is how every
        # later `wait` and `say` for this room finds its way back in - after this,
        # the commands read exactly like the local ones, no --url again.
        from vaf.core.a2a.client import RemoteRefused, RemoteRoom, room_url
        from vaf.core.a2a.trust import TrustRefused

        existing = _remote_record(room_id)
        if existing and not ticket:
            _emit({"ok": True, "room": room_id, "peer": existing.get("peer"),
                   "role": existing.get("role"), "already": True, "remote": True})
            return
        if not ticket:
            _fail("Joining a room on another machine needs --ticket from an invitation.",
                  EXIT_REFUSED)
        try:
            named = room_url(url)["room_id"]
            if named != room_id:
                _fail(f"That URL is for room '{named}', not '{room_id}'.", EXIT_REFUSED)
            with RemoteRoom.connect(url, ticket) as remote:
                if not remote.seat:
                    _fail("The host issued no seat for this join; without one, only "
                          "this single connection would ever work. The host is "
                          "running an older VAF - update it, or work on its machine.",
                          EXIT_ERROR)
                # The handshake is KEPT, not just printed: every later command
                # runs in a new process with no socket open, so howto, skill and
                # tasks would otherwise have to reconnect to answer what the room
                # already told us once.
                _remote_save(room_id, {
                    "url": url, "peer": remote.peer_id, "role": remote.role,
                    "seat": remote.seat, "cursor": 0,
                    "welcome": getattr(remote, "packet", None) or {},
                })
                _emit({"ok": True, "room": room_id, "peer": remote.peer_id,
                       "role": remote.role, "remote": True,
                       "welcome": getattr(remote, "packet", None) or {}})
                # getattr, not attribute access: this lane talks to whatever
                # implements the client contract, and a host or client one
                # version older simply has no packet to give.
                if (getattr(remote, "packet", None) or {}).get("describe_yourself"):
                    _hint(f"Say what you can do so the others know who to ask:\n"
                          f"  vaf a2a introduce {room_id} --skills \"what you are good at\"\n"
                          f"Keep the room's instructions as a skill of your own:\n"
                          f"  vaf a2a skill {room_id} > vaf_a2a_rooms/SKILL.md")
        except (TrustRefused, RemoteRefused) as e:
            _remote_fail(e)
        return

    room = _room(room_id)
    # Only WITHOUT a ticket is a second join the same participant asking twice. With
    # one it is a different agent presenting a credential of its own, and several of
    # those live on one machine: a foreign agent driving this CLI shares the machine
    # owner's derived handle, so short-circuiting on it would have locked every guest
    # out of a room the owner had already joined. Found by the first live run.
    if not ticket:
        existing = _me(room, required=False)
        if existing is not None:
            _emit({"ok": True, "room": room_id, "peer": existing.peer_id,
                   "role": existing.role, "already": True})
            return
    try:
        if ticket:
            identity = room.redeem_ticket(ticket, display=display or "guest", mode=mode,
                                          card=_self_card(skills))
        else:
            identity = room.join(display=display or _display(),
                                 peer_id=derive_peer_id(_key(), room_id),
                                 scope_id=_key(), mode=mode, participant_key=_key())
    except TicketInvalid as e:
        _fail(str(e), EXIT_REFUSED)
    except RoomError as e:
        _fail(str(e), EXIT_REFUSED)
    # The room's half of the handshake: who is here and what they said they can
    # do, what this role may send, the shared folder, how much work is open -
    # and, when this peer has said nothing about itself, the room asking. A join
    # that answered with a handle alone left a newcomer to discover all of it one
    # command at a time, which nobody does in a room of twenty.
    # The four fields a join has always answered with stay exactly where they
    # were: the briefing tells every foreign agent to read `peer` from this line,
    # and a nested one would break every guest written against it. The packet
    # travels BESIDE them - the protocol's own rule 1, applied to our own output.
    _emit({"ok": True, "room": room_id, "peer": identity.peer_id,
           "role": identity.role, "mode": mode, "welcome": room.welcome(identity)})
    if not skills:
        _hint(f"Say what you can do so the others know who to ask:\n"
              f"  vaf a2a introduce {room_id} --skills \"what you are good at\"\n"
              f"Keep the room's instructions as a skill of your own:\n"
              f"  vaf a2a skill {room_id} > vaf_a2a_rooms/SKILL.md")


@app.command()
def trust(
    url: str = typer.Argument(..., help="wss://host:port of the machine hosting the room."),
    ca_fp: str = typer.Option("", "--ca-fp", help="SHA-256 fingerprint from the invitation."),
    ca_file: str = typer.Option("", "--ca-file", help="A copy of that machine's ca.pem."),
) -> None:
    """Trust another machine's certificate authority, and only the right one.

    Two ways in, and both end at the same check. With --ca-file you already have the
    file and the fingerprint says whether it is the RIGHT file. With --ca-fp alone the
    certificate is fetched from the machine itself and kept only if it matches - trust
    on first use with an out-of-band fingerprint, which is not the same thing as blind
    trust on first use: the number came to you by another route than the certificate.

    Nothing is stored on a mismatch. A near miss is a miss.
    """
    from pathlib import Path as _Path

    from vaf.core.a2a.trust import pin_authority, TrustRefused

    if not ca_fp and not ca_file:
        _fail("Give --ca-fp from the invitation, or --ca-file plus --ca-fp to check it.",
              EXIT_REFUSED)
    try:
        stored, fingerprint = pin_authority(
            url, expected_fingerprint=ca_fp,
            ca_file=_Path(ca_file) if ca_file else None)
    except TrustRefused as e:
        _fail(str(e), EXIT_REFUSED)
    _emit({"ok": True, "url": url, "ca_fingerprint": fingerprint, "stored": str(stored)})


# ── talking ─────────────────────────────────────────────────────────────────

@app.command()
def say(room_id: str = typer.Argument(...), text: str = typer.Argument(...),
        to_peer: str = typer.Option("", "--to", help="Address one peer (others still read it)."),
        file: List[str] = typer.Option(None, "--file",
                                       help="Name a file in the room's shared folder this message is about. Repeatable."),
        as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Say something in a room."""
    _send(room_id, "say", text, to_peer=to_peer, as_peer=as_peer, files=tuple(file or ()))


@app.command()
def ask(room_id: str = typer.Argument(...), text: str = typer.Argument(...),
        to_peer: str = typer.Option("", "--to"),
        as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Ask a question in a room."""
    _send(room_id, "ask", text, to_peer=to_peer, as_peer=as_peer)


@app.command()
def answer(room_id: str = typer.Argument(...), text: str = typer.Argument(...),
           reply_to: str = typer.Option("", "--reply-to", help="Id of the message you answer."),
           file: List[str] = typer.Option(None, "--file",
                                          help="Name a file in the room's shared folder "
                                               "this answer is about. Repeatable."),
           as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Answer a question."""
    _send(room_id, "answer", text, reply_to=reply_to, as_peer=as_peer,
          files=tuple(file or ()))


@app.command()
def report(room_id: str = typer.Argument(...), text: str = typer.Argument(...),
           status: str = typer.Option("completed",
                                      help="submitted, working, input_required, completed, "
                                           "failed, rejected or canceled"),
           reply_to: str = typer.Option("", "--reply-to",
                                        help="Id of the message that asked for this work. "
                                             "Linking it is what puts the task on the "
                                             "room's task board."),
           progress: str = typer.Option("", "--progress",
                                        help="How far you have come, as DONE/TOTAL "
                                             "(for example 3/5). A status alone cannot "
                                             "tell a long run from a hung one."),
           step: str = typer.Option("", "--step",
                                    help="What you are doing right now, in a few words."),
           file: List[str] = typer.Option(None, "--file",
                                          help="Name a file in the room's shared folder "
                                               "this report is about. Repeatable."),
           as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Report how a task stands."""
    payload = _parse_progress(progress, step)
    _send(room_id, "report", text, status=status, reply_to=reply_to,
          as_peer=as_peer, progress=payload, files=tuple(file or ()))


def _parse_progress(progress: str, step: str) -> Optional[dict]:
    """`--progress 3/5 --step "writing tests"` as the body's progress object.

    A refusal names the shape, because a machine peer reads the error and has to
    fix its own call: silently dropping "3 von 5" would leave the board looking
    like the sender never reported anything.
    """
    raw = str(progress or "").strip()
    out: dict = {}
    if raw:
        parts = raw.replace(" ", "").split("/")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            _fail("--progress takes DONE/TOTAL, both whole numbers - for example 3/5.",
                  EXIT_REFUSED)
        out["done"], out["total"] = int(parts[0]), int(parts[1])
    if str(step or "").strip():
        out["step"] = str(step).strip()
    return out or None


@app.command()
def skill(room_id: str = typer.Argument(...),
          as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Print a SKILL.md for working in this room, to keep in your own skills folder.

    A briefing is read once and dies with the session it was pasted into; a skill
    file comes back every time it is relevant, which is what taking part in a room
    actually needs. It is written in the shared Agent Skills format - the one
    Claude Code, Codex and VAF all read - so the same file works wherever the peer
    runs:

        vaf a2a skill <room> > vaf_a2a_rooms/SKILL.md
    """
    from vaf.core.a2a.invite import client_skill
    room = _open_local(room_id)
    if room is None:
        record = _remote_record(room_id)
        if record is None:
            _fail(f"There is no room '{room_id}' on this machine.", EXIT_NO_ROOM)
        packet = record.get("welcome") or {}
        typer.echo(client_skill(
            room_id=room_id, role=str(record.get("role") or "peer"),
            room_kind=str(packet.get("kind") or "round"),
            workspace=str(packet.get("workspace") or "") or None))
        return
    identity = _me(room, as_peer=as_peer)
    typer.echo(client_skill(room_id=room_id, role=identity.role, room_kind=room.kind,
                            workspace=room.workspace_dir(create=False)))


@app.command()
def vote(room_id: str = typer.Argument(...),
         question: str = typer.Argument(...),
         option: List[str] = typer.Option(None, "--option", "-o",
                                          help="An answer to choose from. Repeat it. Defaults to yes/no."),
         closes_in: int = typer.Option(0, "--closes-in",
                                       help="Minutes until the vote counts as closed (0 = stays open)."),
         as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Put a question to the room. Any member may, in any role.

    Prints the vote's id: that is what a ballot answers.
    """
    from vaf.core.a2a.room import RoomError
    room = _open_local(room_id)
    if room is None:
        record = _remote_record(room_id)
        if record is None:
            _fail(f"There is no room '{room_id}' on this machine.", EXIT_NO_ROOM)
        # Options are trimmed, bounded and defaulted by the ROOM that stores them,
        # the same way a local vote's are. Doing it here as well would be a second
        # opinion about what an option is, on the one lane where the two machines
        # are not even the same install.
        body = {"text": question, "options": list(option or [])}
        if closes_in:
            # The deadline used to be dropped silently on this lane: a remote
            # `--closes-in 3` opened a vote the host knew no end for. It is stamped
            # on the OPENER's clock either way, which the protocol says out loud.
            import time as _time
            body["closes_at"] = _time.time() + closes_in * 60.0
        return _remote_submit(record, room_id, {"kind": "vote", "body": body})
    identity = _me(room, as_peer=as_peer)
    try:
        frame = room.open_vote(identity, question, options=list(option or []),
                               closes_in_s=(closes_in * 60.0) if closes_in else None)
    except RoomError as e:
        _fail(str(e), EXIT_REFUSED)
    _emit({"ok": True, "room": room_id, "vote": frame.id,
           "options": (frame.body or {}).get("options") or []})


@app.command()
def ballot(room_id: str = typer.Argument(...),
           vote_id: str = typer.Argument(..., help="The id `vaf a2a vote` printed."),
           choice: str = typer.Argument(..., help="One of the vote's options."),
           comment: str = typer.Option("", help="Why, in one line. Everyone sees it."),
           as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Cast your ballot. Voting again replaces your earlier one."""
    from vaf.core.a2a.frame import plausible_frame_id
    from vaf.core.a2a.room import RoomError
    if not plausible_frame_id(vote_id):
        _fail("the second argument is the vote's ID, the one `vaf a2a vote` printed.",
              EXIT_REFUSED)
    room = _open_local(room_id)
    if room is None:
        record = _remote_record(room_id)
        if record is None:
            _fail(f"There is no room '{room_id}' on this machine.", EXIT_NO_ROOM)
        return _remote_submit(record, room_id, {
            "kind": "answer", "reply_to": vote_id,
            "body": {"text": comment or f"votes: {choice}", "choice": choice}})
    identity = _me(room, as_peer=as_peer)
    try:
        frame = room.cast(identity, vote_id, choice, comment=comment)
    except RoomError as e:
        _fail(str(e), EXIT_REFUSED)
    # The choice the ROOM recorded, not the one that was typed: a shortened answer
    # is resolved against the options on the way in, and printing what was typed
    # would tell a machine peer its shorthand had been taken literally.
    _emit({"ok": True, "room": room_id, "vote": vote_id,
           "choice": (frame.body or {}).get("choice") or choice, "id": frame.id})


@app.command()
def votes(room_id: str = typer.Argument(...)) -> None:
    """Every vote in this room with its tally, its deadline, and who has not answered."""
    room = _open_local(room_id)
    if room is None:
        record = _remote_record(room_id)
        if record is None:
            _fail(f"There is no room '{room_id}' on this machine.", EXIT_NO_ROOM)
        for entry in _remote_votes(record):
            _emit(entry)
        return
    _me(room)
    for entry in room.votes():
        _emit(entry)


@app.command()
def mission(room_id: str = typer.Argument(...),
            text: str = typer.Argument("", help="Leave empty to print the current one."),
            as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Say what this room is for, at length - or print what it says today.

    Everyone is reminded of it: it travels in the welcome a newcomer gets, in
    every check-in, and in the room turn of every VAF agent in the room. Host or
    leader only, and it is a property of the room rather than something somebody
    said, so it never appears in the transcript as a message.
    """
    from vaf.core.a2a.room import RoomError
    room = _open_local(room_id)
    if room is None:
        # Remote seat. Reading answers from the join handshake, honestly labeled
        # as of that moment - the mission is manifest, not a frame, so no later
        # change ever reaches this side as a message. Writing is refused with
        # the way that works: the manifest lives on the host, and the wire
        # carries messages, not manifest edits. (First field use got "no room
        # on this machine" here - factually wrong for a member holding a seat.)
        record = _remote_record(room_id)
        if record is None:
            _fail(f"There is no room '{room_id}' on this machine.", EXIT_NO_ROOM)
        if str(text or "").strip():
            _fail("The mission lives on the room's host machine, and the wire "
                  "carries messages, not manifest edits. Ask a leader in the "
                  "room (vaf a2a say) or set it on the host.", EXIT_REFUSED)
        packet = record.get("welcome") or {}
        _emit({"ok": True, "room": room_id,
               "mission": str(packet.get("mission") or ""),
               "topic": str(packet.get("topic") or ""),
               "leaders": list(packet.get("leaders") or []),
               "as_of": "join",
               "note": "read from the join handshake; the live value is on the host"})
        return
    if not str(text or "").strip():
        _emit({"ok": True, "room": room_id,
               "mission": str(room.manifest.get("mission") or ""),
               "topic": str(room.manifest.get("topic") or ""),
               "leaders": [labels for labels in room.leaders()]})
        return
    identity = _me(room, as_peer=as_peer)
    try:
        written = room.set_mission(identity, text)
    except RoomError as e:
        _fail(str(e), EXIT_REFUSED)
    _emit({"ok": True, "room": room_id, "mission": written})


@app.command()
def howto(room_id: str = typer.Argument(...),
          as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Print how to work in this room again - the same text the invitation gave.

    An invitation is read once, in a session that may be long over, and an agent
    that lost it had no way back to the commands: it could sit in a room it is a
    member of and not know how to report. The text is the SAME one the invitation
    builds, with the join step replaced by this peer's handle - a second, differently
    worded reference would leave a reader deciding which of the two is current.
    """
    from vaf.core.a2a.invite import briefing, lan_endpoint
    room = _open_local(room_id)
    if room is None:
        # A remote peer needs this text MORE than a local one, not less: it is
        # the side that has no room on disk to look at. The handshake kept at
        # join time answers what the briefing needs to know.
        record = _remote_record(room_id)
        if record is None:
            _fail(f"There is no room '{room_id}' on this machine.", EXIT_NO_ROOM)
        packet = record.get("welcome") or {}
        typer.echo(briefing(
            room_id=room_id, ticket="", role=str(record.get("role") or "peer"),
            display=str((packet.get("you") or {}).get("display") or "guest"),
            room_kind=str(packet.get("kind") or "round"),
            topic=str(packet.get("topic") or ""),
            workspace=str(packet.get("workspace") or "") or None,
            already_in=str(record.get("peer") or ""),
        ))
        return
    identity = _me(room, as_peer=as_peer)
    # Plain text, like `log`: this command exists to be READ, and a briefing
    # escaped inside a JSON string is a briefing nobody follows.
    typer.echo(briefing(
        room_id=room_id, ticket="", role=identity.role, display=identity.display,
        room_kind=room.kind, topic=str(room.manifest.get("topic") or ""),
        endpoint=lan_endpoint(room_id), workspace=room.workspace_dir(create=False),
        already_in=identity.peer_id,
    ))


@app.command()
def directive(room_id: str = typer.Argument(...), text: str = typer.Argument(...),
              to_peer: str = typer.Option("", "--to"),
              as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Instruct a worker. Leaders only, and never in a round."""
    _send(room_id, "directive", text, to_peer=to_peer, as_peer=as_peer)


@app.command()
def hire(room_id: str = typer.Argument(...),
         purpose: str = typer.Option("", help="What the new room is for."),
         as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Open a child room you lead, to bring in more workers."""
    from vaf.core.a2a.room import RoomError
    room = _room(room_id)
    identity = _me(room, as_peer=as_peer)
    try:
        child, frame = room.hire(identity, purpose=purpose)
    except RoomError as e:
        _fail(str(e), EXIT_REFUSED)
    _emit({"ok": True, "parent": room_id, "room": child.room_id, "frame": frame.id})


@app.command()
def role(room_id: str = typer.Argument(...), peer: str = typer.Argument(...),
         new_role: str = typer.Argument(..., metavar="ROLE"),
         as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Change someone's role. Leaders only."""
    from vaf.core.a2a.room import RoomError
    room = _room(room_id)
    identity = _me(room, as_peer=as_peer)
    try:
        frame = room.grant_role(identity, peer, new_role)
    except RoomError as e:
        _fail(str(e), EXIT_REFUSED)
    _emit({"ok": True, "room": room_id, "peer": peer, "role": new_role, "id": frame.id})


@app.command()
def leave(room_id: str = typer.Argument(...), reason: str = typer.Option("", help="Why."),
          as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Leave a room."""
    _send(room_id, "leave", reason, as_peer=as_peer)


@app.command()
def kick(room_id: str = typer.Argument(...),
         peer: str = typer.Argument(..., help="Peer to remove."),
         reason: str = typer.Option("", help="Why."),
         as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Remove somebody from a room. Leaders, and the host of the room.

    The room's own host cannot be removed - closing the room is what takes everybody
    out. Leaving yourself is `leave`.
    """
    from vaf.core.a2a.room import RoomError
    room = _room(room_id)
    identity = _me(room, as_peer=as_peer)
    try:
        frame = room.kick(identity, peer, reason=reason)
    except RoomError as e:
        _fail(str(e), EXIT_REFUSED)
    _emit({"ok": True, "room": room_id, "removed": peer, "id": frame.id,
           "lamport": frame.lamport})


@app.command()
def close(room_id: str = typer.Argument(...), reason: str = typer.Option("", help="Why."),
          as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Close a room. It stays readable forever; nothing more can be written."""
    _send(room_id, "close", reason, as_peer=as_peer)


@app.command()
def delete(room_id: str = typer.Argument(...),
           as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Close a room and remove it from this machine. The host of the room only.

    Not the same as `close`, which ends the conversation and keeps the transcript.
    This removes it. Export it first if you want to keep it: `vaf a2a export`.
    """
    from vaf.core.a2a.room import RoomError
    room = _room(room_id)
    identity = _me(room, as_peer=as_peer)
    try:
        gone = room.delete(identity)
    except RoomError as e:
        _fail(str(e), EXIT_REFUSED)
    _emit({"ok": True, "room": room_id, "deleted": bool(gone)})


@app.command()
def introduce(room_id: str = typer.Argument(...),
              skills: str = typer.Option("", help="One line about what you can do."),
              display: str = typer.Option("", help="Change the name you appear under."),
              as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Say what you can do, or change your name, after you have already joined.

    Everyone in the room sees it. It is self-description: it grants nothing.
    """
    from vaf.core.a2a.room import RoomError
    room = _open_local(room_id)
    if room is None:
        # Remote seat: the member record lives on the host, and the wire
        # carries messages only - so this verb cannot travel yet. The refusal
        # names what works instead of claiming there is no room (which is what
        # a member holding a seat was told in the first field use).
        if _remote_record(room_id) is None:
            _fail(f"There is no room '{room_id}' on this machine.", EXIT_NO_ROOM)
        _fail("A remote seat cannot edit its member record yet: the record "
              "lives on the host, and the wire carries messages only. Your "
              "display name is the one the invitation carried; to tell the "
              "room what you can do, say it (vaf a2a say).", EXIT_REFUSED)
    identity = _me(room, as_peer=as_peer)
    try:
        record = room.introduce(identity, display=display,
                                card=_self_card(skills) if skills else None)
    except RoomError as e:
        _fail(str(e), EXIT_REFUSED)
    _emit({"ok": True, "room": room_id, "peer": identity.peer_id,
           "display": record.get("display"), "card": record.get("card") or {}})


@app.command()
def members(room_id: str = typer.Argument(...)) -> None:
    """Who is in the room: role, liveness, and who belongs to whom."""
    room = _open_local(room_id)
    if room is None:
        # Remote: folded from the frames the seat may read. Honest about what
        # the wire cannot know - liveness and household pairing live on the
        # host, so a remote roster carries neither and says so with nulls
        # rather than inventing "stale" for people who are merely far away.
        record = _remote_record(room_id)
        if record is None:
            _fail(f"There is no room '{room_id}' on this machine.", EXIT_NO_ROOM)
        frames = _remote_read_frames(room_id, record)
        labels = _remote_labels(frames)
        cards: dict = {}
        roles: dict = {}
        for frame in sorted(frames, key=lambda f: f.lamport):
            if frame.kind == "join":
                card = (frame.body or {}).get("card")
                if isinstance(card, dict):
                    cards[frame.sender] = card
            roles[frame.sender] = frame.role
        # Who belongs to whom, as far as the transcript itself can prove: an agent
        # whose join carries its owner's attestation. The SAME fold the host runs,
        # so a roster read here and one read there cannot disagree about a pair.
        from vaf.core.a2a.room import fold_owners
        owners = fold_owners(frames, room_id)
        # One partner per owner, the FIRST attested agent: the tie-break `pairs()`
        # applies on the host, so the two rosters name the same partner.
        owned = {}
        for agent, owner in owners.items():
            owned.setdefault(owner, agent)
        for peer_id in _remote_members(frames):
            kind, partner, proof = "unknown", "", ""
            if peer_id in owners:
                kind, partner, proof = "agent", owners[peer_id], "attested"
            elif peer_id in owned:
                kind, partner, proof = "human", owned[peer_id], "attested"
            _emit({"peer": peer_id, "display": labels.get(peer_id, peer_id),
                   "role": roles.get(peer_id, "peer"), "stale": None,
                   "card": cards.get(peer_id, {}), "kind": kind,
                   "partner": partner,
                   "partner_display": labels.get(partner, partner) if partner else "",
                   "proof": proof, "remote": True})
        return
    _me(room)
    # Which member is a person, which is an agent, and which two are one household.
    # Printed here so an agent that never sees our surfaces can read it too - in a
    # room with several households, "who speaks for whom" cannot be guessed from the
    # names, and guessing it is how an agent answers for somebody it does not work
    # for. Derived by the room, never claimed by a member.
    try:
        pairs = room.pairs()
    except Exception:
        pairs = {}
    for peer_id, record in room.members().items():
        pairing = pairs.get(peer_id) or {}
        _emit({"peer": peer_id, "display": record["display"], "role": record["role"],
               "stale": record["stale"], "card": record["card"],
               # "human", "agent", or "unknown" - a guest that arrived on an
               # invitation named no account, so nothing here can say what it is.
               "kind": pairing.get("kind") or "unknown",
               "partner": pairing.get("partner") or "",
               "partner_display": pairing.get("partner_label") or "",
               # HOW the room knows: "derived" from an account it admits, or
               # "attested" by the owner's own key in the transcript.
               "proof": pairing.get("proof") or ""})


@app.command()
def tasks(room_id: str = typer.Argument(...)) -> None:
    """The room's task board: what was asked, who is on it, how it stands.

    Derived from the transcript (directives, and every message a report chain
    answers), never stored: a task appears the moment somebody reports on the
    message that asked for the work - `vaf a2a report <room> "..." --status
    working --reply-to <id>` - and its status is whatever the LAST report said.
    Open work first.
    """
    room = _open_local(room_id)
    if room is None:
        record = _remote_record(room_id)
        if record is None:
            _fail(f"There is no room '{room_id}' on this machine.", EXIT_NO_ROOM)
        # The board is a FOLD over frames, so a remote peer can compute the same
        # one from the frames it is allowed to read - the fold is shared code,
        # not a second implementation with its own opinion about status.
        for task in _remote_tasks(record):
            _emit(task)
        return
    _me(room)
    for task in room.tasks():
        _emit(task)


# ── reading ─────────────────────────────────────────────────────────────────

def _row(entry: dict) -> dict:
    """One NDJSON line for a machine peer.

    `display` is the name the peer joined under and `label` is that name with its tag,
    which is what every human-facing surface shows. Both travel: a consumer written
    against the first release keeps reading `display`, and one that wants to print
    what the browser prints has the label without recomputing it.
    """
    return {"id": entry["id"], "peer": entry["peer"], "display": entry["display"],
            "label": entry["label"],
            "role": entry["role"], "kind": entry["kind"], "text": entry["text"],
            "body": entry["body"], "lamport": entry["lamport"], "ts": entry["ts"],
            "reply_to": entry["reply_to"], "known": entry["known"],
            # What a reader may conclude about who wrote this. A machine peer that
            # never asks still gets it, which is the point: `unsigned` is the answer
            # for a room where nobody signs, and it costs one word to say so.
            "verdict": entry.get("verdict") or "unsigned"}


def _conversation(room, identity, *, since: int, membership: bool):
    """What a machine consumer asked for: other people's messages.

    Own frames are dropped because a peer echoing its own words back is noise, and
    membership bookkeeping is dropped by default because `wait --n 1` should return
    something that was SAID, not "somebody joined". `log` keeps showing both, since a
    human reading a transcript does want to see who came and went.
    """
    from vaf.core.a2a.room import BOOKKEEPING_KINDS
    rows = [r for r in room.transcript(since_lamport=since) if r["peer"] != identity.peer_id]
    if membership:
        return rows
    return [r for r in rows if r["kind"] not in BOOKKEEPING_KINDS]


@app.command()
def session(
    room_id: str = typer.Argument(...),
    stop: bool = typer.Option(False, "--stop", help="Stop the running session."),
    status: bool = typer.Option(False, "--status", help="Print the session's state."),
    once: bool = typer.Option(False, "--once",
                              help="Drain the backlog and the outbox, then exit."),
    background: bool = typer.Option(False, "--background",
                                    help="Run detached; this command returns at once."),
) -> None:
    """Hold ONE connection to a room on another machine and mirror it to files.

    The CLI is one process per command, and the wire punishes that shape: the
    writer lease from a dropped connection blocks the next one for up to 90
    seconds, and reading needs a connection too. Measured in the field: two of
    seven messages arrived over one-connection-per-command, eight of eight over
    a held one. While a session runs, `read`, `members` and `log` answer from
    its mirror instantly, and anything dropped into the session's outbox folder
    is sent on the held line.

    One session per room; a second start names the first one's pid instead of
    silently fighting it over the outbox.
    """
    from vaf.core.a2a.session import (SessionBusy, read_status, run_session,
                                      session_paths, session_pid)

    if status:
        pid = session_pid(room_id)
        state = read_status(room_id) or {}
        _emit({"room": room_id, "running": bool(pid), "pid": pid or None,
               **{k: v for k, v in state.items() if k != "pid"}})
        return
    if stop:
        pid = session_pid(room_id)
        if not pid:
            _emit({"room": room_id, "stopped": False, "reason": "no session running"})
            return
        try:
            import psutil
            psutil.Process(pid).terminate()
        except Exception:
            if os.name != "nt":
                import signal as _signal
                os.kill(pid, _signal.SIGTERM)
            else:
                _fail(f"could not stop pid {pid}; end it from the task manager",
                      EXIT_ERROR)
        _emit({"room": room_id, "stopped": True, "pid": pid})
        return

    record = _remote_record(room_id)
    if record is None:
        _fail(f"There is no remote room '{room_id}' on this machine - join it "
              f"first (`vaf a2a join ... --url ...`).", EXIT_NO_ROOM)

    if background:
        import subprocess
        import sys as _sys
        argv = [_sys.executable, "-m", "vaf.main", "a2a", "session", room_id]
        kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                        "stderr": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            if flags:
                kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)
        _emit({"room": room_id, "started": True, "pid": proc.pid,
               "inbox": str(session_paths(room_id).inbox)})
        return

    try:
        raise typer.Exit(run_session(room_id, record, once=once))
    except SessionBusy as e:
        _fail(str(e), EXIT_REFUSED)


@app.command()
def read(
    room_id: str = typer.Argument(...),
    all_messages: bool = typer.Option(False, "--all", help="The whole transcript, not just what is new."),
    keep_position: bool = typer.Option(False, "--keep-position",
                                       help="Do not move your read position."),
    membership: bool = typer.Option(False, "--with-membership",
                                    help="Include join, leave, role and ack frames."),
    as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),
) -> None:
    """Print new messages as NDJSON, one object per line."""
    room = _open_local(room_id)
    if room is None:
        # A room on another machine. This branch is why a remote peer can HEAR:
        # the first field join spoke into a room for an hour without seeing the
        # answers, because read only searched this disk while the wire lane sat
        # unused. Frames come from the session mirror when a daemon holds the
        # room, one wire connection otherwise; the cursor lives in the seat
        # record, exactly where wait keeps it.
        record = _remote_record(room_id)
        if record is None:
            _fail(f"There is no room '{room_id}' on this machine.", EXIT_NO_ROOM)
        cursor = 0 if all_messages else int(record.get("cursor") or 0)
        frames = [f for f in _remote_read_frames(room_id, record)
                  if f.lamport > cursor]
        rows = _remote_rows(record, frames, membership=membership)
        for entry in rows:
            _emit(entry)
        if rows and not keep_position and not all_messages:
            record["cursor"] = int(rows[-1]["lamport"])
            _remote_save(room_id, record)
        return
    identity = _me(room, as_peer=as_peer)
    since = 0 if all_messages else room.store.cursor(identity.peer_id)
    rows = _conversation(room, identity, since=since, membership=membership)
    for entry in rows:
        _emit(_row(entry))
    # After the lines exist, never before.
    if rows and not keep_position and not all_messages:
        room.store.set_cursor(identity.peer_id, rows[-1]["lamport"])


@app.command()
def wait(
    room_id: str = typer.Argument(...),
    n: int = typer.Option(1, "--n", help="Stop after this many messages."),
    timeout: float = typer.Option(0.0, help="Seconds to wait. 0 means forever."),
    interval: float = typer.Option(0.5, help="Seconds between checks."),
    membership: bool = typer.Option(False, "--with-membership",
                                    help="Also wake on join, leave, role and ack frames."),
    as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),
) -> None:
    """Block until something is said, then print it as NDJSON.

    Exit codes: 0 got messages, 4 the timeout expired with nothing, 5 the room closed.
    """
    _install_stop_handler()
    room = _open_local(room_id)
    if room is None:
        record = _remote_record(room_id)
        if record is None:
            _fail(f"There is no room '{room_id}' on this machine.", EXIT_NO_ROOM)
        return _remote_wait(record, room_id, n=n, timeout=timeout,
                            membership=membership)
    identity = _me(room, as_peer=as_peer)
    # A peer that never said what it can do is a name in everybody's roster and
    # nothing else - and in a room of twenty that is the difference between being
    # given work and being skipped. Asked here rather than only at join time,
    # because the join happened in some earlier session: this is the command an
    # agent runs before every turn, so it is the one place the ask cannot be
    # missed. On stderr, so the NDJSON promise on stdout is untouched.
    try:
        if not (room.members().get(identity.peer_id) or {}).get("card"):
            _hint(f"You have not said what you can do in this room yet:\n"
                  f"  vaf a2a introduce {room_id} --skills \"what you are good at\"")
    except Exception:
        pass
    started = time.monotonic()
    seen = 0

    while not _stop:
        if room.closed:
            # A close is printed before ending, so a reader learns WHY it stopped.
            for entry in room.transcript(since_lamport=room.store.cursor(identity.peer_id)):
                if entry["kind"] == "close":
                    _emit(_row(entry))
                    break
            raise typer.Exit(EXIT_CLOSED)

        cursor = room.store.cursor(identity.peer_id)
        rows = _conversation(room, identity, since=cursor, membership=membership)
        for entry in rows:
            _emit(_row(entry))
            room.store.set_cursor(identity.peer_id, entry["lamport"])
            seen += 1
            if seen >= n:
                raise typer.Exit(EXIT_OK)

        if timeout and (time.monotonic() - started) >= timeout:
            raise typer.Exit(EXIT_TIMEOUT)
        time.sleep(max(0.05, interval))

    raise typer.Exit(EXIT_OK if seen else EXIT_TIMEOUT)


@app.command()
def log(room_id: str = typer.Argument(...),
        follow: bool = typer.Option(False, "--follow", "-f", help="Keep printing as it arrives."),
        interval: float = typer.Option(0.5, help="Seconds between checks when following.")) -> None:
    """Show the room as a group chat, for a human to read."""
    _install_stop_handler()
    room = _open_local(room_id)
    if room is None:
        record = _remote_record(room_id)
        if record is None:
            _fail(f"There is no room '{room_id}' on this machine.", EXIT_NO_ROOM)
        if follow:
            _fail("--follow needs a live lane: run `vaf a2a session` for this room "
                  "and follow its inbox, or use `vaf a2a wait`.", EXIT_ERROR)
        from vaf.core.a2a.room import BOOKKEEPING_KINDS
        frames = _remote_read_frames(room_id, record)
        labels = _remote_labels(frames)
        for frame in sorted(frames, key=lambda f: (f.lamport, f.id)):
            if frame.kind in BOOKKEEPING_KINDS or frame.kind == "ping":
                continue
            label = f"{labels.get(frame.sender, frame.sender)} [{frame.role}]"
            if frame.kind != "say":
                label += f" ({frame.kind})"
            text = str((frame.body or {}).get("text") or "").strip()
            typer.echo(f"{label}: {text}".rstrip())
        return
    _me(room, required=False)
    shown = 0

    from vaf.core.a2a.room import describe

    members = {peer: record["display"] for peer, record in room.members().items()}

    def _print(rows) -> int:
        for entry in rows:
            label = f"{entry['display']} [{entry['role']}]"
            if entry["kind"] not in ("say",):
                label += f" ({entry['kind']})"
            aimed = str((entry.get("to") or {}).get("peer") or "")
            if aimed:
                label += f" -> {members.get(aimed, aimed)}"
            typer.echo(f"{label}: {describe(entry)}".rstrip())
        return len(rows)

    rows = room.transcript()
    shown += _print(rows)
    if not follow:
        return
    last = rows[-1]["lamport"] if rows else 0
    while not _stop:
        fresh = room.transcript(since_lamport=last)
        if fresh:
            _print(fresh)
            last = fresh[-1]["lamport"]
        if room.closed:
            return
        time.sleep(max(0.05, interval))


@app.command()
def audit(room_id: str = typer.Argument(...),
          since: int = typer.Option(0, help="Only events after this lamport."),
          json_out: bool = typer.Option(False, "--json", help="One JSON object per line.")) -> None:
    """Who did what in a room, and when. Acts, not wording.

    The transcript answers what was said; this answers who took part, when they came
    and went, and what sort of thing each of them sent. It carries no message text, so
    it can be shown to somebody who has no business reading the conversation.
    """
    from vaf.core.a2a.room import audit as audit_rows, frame_clock

    room = _room(room_id)
    _me(room, required=False)
    rows = audit_rows(room, since_lamport=int(since))
    if json_out:
        for row in rows:
            _emit(row)
        return

    if not rows:
        typer.echo(f"{room_id}: nothing has happened yet.")
        return
    typer.echo(f"{room_id} ({room.kind}{', closed' if room.closed else ''}) - "
               f"{len(rows)} events")
    for row in rows:
        when = frame_clock(row.get("ts"))
        line = f"{when} {row['label']} [{row['role']}] {row['event']}"
        if row.get("detail"):
            line += f" ({row['detail']})"
        typer.echo(line)


@app.command()
def verify(room_id: str = typer.Argument(...),
           since: int = typer.Option(0, help="Only frames after this lamport."),
           problems: bool = typer.Option(False, "--problems",
                                         help="Only what is not plainly in order.")) -> None:
    """Who really wrote each message, as far as the transcript can prove it.

    A room RECORDS an author by assigning it, which is worth exactly as much as the
    machine holding the room. A signed message can be checked by anybody with the
    transcript, on any machine, later. This prints one verdict per message.

    `unsigned` is the ordinary answer and not a complaint. `valid` means the signature
    covers the message and the key is the one that peer published here. `foreign_key`
    means a real signature by no key this peer ever published in a checkable form:
    a message written into the wrong lane, or a peer whose client announced its key
    without signing the announcement. `invalid` is the only one that accuses
    anybody. `unreadable` means a claim this version cannot parse, which is what a
    newer scheme looks like to an older reader.

    Nothing is ever removed by a verdict: a message that fails stays in the room and
    stays counted, because taking it out would break the ordering for everything after
    it.
    """
    room = _room(room_id)
    _me(room, required=False)
    labels = room.labels()
    for frame, verdict in room.verify_frames(since_lamport=int(since)):
        if problems and verdict in ("unsigned", "valid"):
            continue
        _emit({"id": frame.id, "peer": frame.sender,
               "label": labels.get(frame.sender) or frame.sender,
               "kind": frame.kind, "lamport": frame.lamport, "verdict": verdict,
               "key": (frame.sig or {}).get("key") or ""})


@app.command()
def export(room_id: str = typer.Argument(...),
           output: Optional[str] = typer.Option(None, "--out", help="Write to this file.")) -> None:
    """Export the whole transcript as Markdown, artifacts listed separately."""
    room = _room(room_id)
    _me(room, required=False)
    manifest = room.manifest
    lines = [f"# Room {room.room_id}", ""]
    if manifest.get("topic"):
        lines += [f"**Topic:** {manifest['topic']}", ""]
    lines += [f"**Kind:** {room.kind}", ""]
    artifacts = []
    for entry in room.transcript():
        label = f"**{entry['display']}** ({entry['role']}"
        label += f", {entry['kind']})" if entry["kind"] != "say" else ")"
        lines.append(f"{label}: {entry['text']}")
        for artifact in (entry["body"] or {}).get("artifacts") or []:
            artifacts.append((entry["display"], artifact))
    if artifacts:
        lines += ["", "## Artifacts", ""]
        for who, artifact in artifacts:
            lines.append(f"- `{artifact.get('name', 'unnamed')}` from {who}")
    text = "\n".join(lines) + "\n"
    if output:
        from pathlib import Path
        Path(output).write_text(text, encoding="utf-8")
        typer.echo(f"Written to {output}")
    else:
        typer.echo(text)
