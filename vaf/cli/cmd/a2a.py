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
import signal
import sys
import time
from typing import Optional

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


def _key() -> str:
    """Who is acting: the machine owner, from the TERMINAL lane.

    The lane is what keeps this apart from the same owner's agent, which is a different
    actor in a room even though it is the same account. See the module docstring for
    why there is no way to name another account here.
    """
    from vaf.core.a2a.room import participant_key
    try:
        return participant_key("cli")
    except Exception:
        return "cli:local"


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

    identity = room.identity_for(_key())
    if identity is None and required:
        _fail(f"You have not joined '{room.room_id}'. Run: vaf a2a join {room.room_id}",
              EXIT_NO_ROOM)
    return identity


def _emit(row: dict) -> None:
    """One frame, one line, flushed. A reader on the other end is blocking on it."""
    sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _send(room_id: str, kind: str, text: str, *, to_peer: str = "",
          reply_to: str = "", status: str = "", as_peer: str = "") -> None:
    from vaf.core.a2a.room import RoomError
    room = _room(room_id)
    identity = _me(room, as_peer=as_peer)
    body = {"text": text}
    if status:
        body["status"] = status
    payload = {"kind": kind, "body": body}
    if to_peer:
        payload["to"] = {"peer": to_peer}
    else:
        # "@Name ..." addresses one member. The ROOM resolves it - a lookup here would
        # be a second copy of the member table, and it would drift the moment somebody
        # joins. Only a LEADING mention counts: "ask @Bob about it" is a sentence about
        # Bob said to everyone, and turning it into a private aside would hide it.
        mention = room.address_from_mention(text)
        if mention:
            payload["to"] = mention
    if reply_to:
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
    topic: str = typer.Option("", help="What the room is for."),
    display: str = typer.Option("", help="Your name in the room."),
    room_id: str = typer.Option("", "--id", help="Use this id instead of a generated one."),
) -> None:
    """Open a room and join it."""
    from vaf.core.a2a.room import Room, RoomError, derive_peer_id
    from vaf.core.a2a.store import StoreError
    try:
        room = Room.create(kind=kind, owner_scope=_key(), topic=topic,
                           room_id=room_id or None)
        me = room.join(display=display or "terminal",
                       peer_id=derive_peer_id(_key(), room.room_id), scope_id=_key())
    except (RoomError, StoreError) as e:
        _fail(str(e), EXIT_REFUSED)
    _emit({"ok": True, "room": room.room_id, "kind": room.kind,
           "peer": me.peer_id, "role": me.role})


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


@app.command()
def invite(
    room_id: str = typer.Argument(..., help="Room to invite into."),
    display: str = typer.Option("guest", help="Name the guest will appear under."),
    ttl: int = typer.Option(3600, help="Seconds the invitation stays valid."),
) -> None:
    """Mint a single-use invitation and print the line, and the briefing, to hand over.

    The invitation is assembled by the room layer, not here: the agent's own room tool
    hands out the same thing when somebody says "open a room and invite Codex", and two
    agents told different things by two inviters is the failure that would follow from
    building it twice.
    """
    from vaf.core.a2a.invite import invitation
    from vaf.core.a2a.room import RoomError
    room = _room(room_id)
    identity = _me(room)
    try:
        row = invitation(room, identity, display=display, ttl_s=float(ttl))
    except RoomError as e:
        _fail(str(e), EXIT_REFUSED)
    _emit({"ok": True, **row})


@app.command()
def join(
    room_id: str = typer.Argument(..., help="Room to join."),
    ticket: str = typer.Option("", help="Invitation, if the room was not opened by you."),
    display: str = typer.Option("", help="Your name in the room."),
    mode: str = typer.Option(
        "assist",
        help="How far VAF's own agent may act on messages here: observe, assist or autonomous.",
    ),
) -> None:
    """Join a room, with an invitation if you were given one."""
    from vaf.core.a2a.room import RoomError, TicketInvalid, derive_peer_id
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
            identity = room.redeem_ticket(ticket, display=display or "guest", mode=mode)
        else:
            identity = room.join(display=display or "terminal",
                                 peer_id=derive_peer_id(_key(), room_id),
                                 scope_id=_key(), mode=mode)
    except TicketInvalid as e:
        _fail(str(e), EXIT_REFUSED)
    except RoomError as e:
        _fail(str(e), EXIT_REFUSED)
    _emit({"ok": True, "room": room_id, "peer": identity.peer_id, "role": identity.role,
           "mode": mode})


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
        as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Say something in a room."""
    _send(room_id, "say", text, to_peer=to_peer, as_peer=as_peer)


@app.command()
def ask(room_id: str = typer.Argument(...), text: str = typer.Argument(...),
        to_peer: str = typer.Option("", "--to"),
        as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Ask a question in a room."""
    _send(room_id, "ask", text, to_peer=to_peer, as_peer=as_peer)


@app.command()
def answer(room_id: str = typer.Argument(...), text: str = typer.Argument(...),
           reply_to: str = typer.Option("", "--reply-to", help="Id of the message you answer."),
           as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Answer a question."""
    _send(room_id, "answer", text, reply_to=reply_to, as_peer=as_peer)


@app.command()
def report(room_id: str = typer.Argument(...), text: str = typer.Argument(...),
           status: str = typer.Option("completed",
                                      help="submitted, working, input_required, completed, "
                                           "failed, rejected or canceled"),
           as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Report how a task stands."""
    _send(room_id, "report", text, status=status, as_peer=as_peer)


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
def close(room_id: str = typer.Argument(...), reason: str = typer.Option("", help="Why."),
          as_peer: str = typer.Option("", "--as", help="Act as this peer (a guest's own handle; or export VAF_A2A_PEER)."),) -> None:
    """Close a room. It stays readable forever; nothing more can be written."""
    _send(room_id, "close", reason, as_peer=as_peer)


@app.command()
def members(room_id: str = typer.Argument(...)) -> None:
    """Who is in the room, with their role and whether they are still awake."""
    room = _room(room_id)
    _me(room)
    for peer_id, record in room.members().items():
        _emit({"peer": peer_id, "display": record["display"], "role": record["role"],
               "stale": record["stale"], "card": record["card"]})


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
            "reply_to": entry["reply_to"], "known": entry["known"]}


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
    room = _room(room_id)
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
    room = _room(room_id)
    identity = _me(room, as_peer=as_peer)
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
    room = _room(room_id)
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
