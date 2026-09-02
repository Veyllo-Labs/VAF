# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""How VAF's own agent takes part in a room.

These tools are the OPPOSITE direction from the guard over ``vaf/core/a2a``. That
guard forbids the room from reaching into the tool funnel; these tools reach from the
funnel into the room, under the agent's own bound identity, with every ordinary check
already behind them. A room still hands out nothing.

Nothing here decides what a role may do. ``Room.ingest`` owns that truth table, so a
worker that tries to issue a directive is refused in exactly the same place a foreign
agent would be. A second copy of the rule in a tool is how two lanes start disagreeing.
"""
from typing import Any, Dict, List, Optional

from vaf.tools.base import BaseTool


def _acting_key(user_scope_id: Optional[str]) -> str:
    """What identifies THIS AGENT locally, as opposed to its user's terminal.

    The lane matters: the same account owns both, and they are two different actors in
    a room. Sharing a key would mean "send my agent in" and "I am in myself" produce
    one member, and whichever spoke last would appear to be the other.
    """
    from vaf.core.a2a.room import participant_key
    try:
        return participant_key("agent", user_scope_id)
    except Exception:
        return "agent:local"


def _announce(user_scope_id) -> None:
    """A room row appeared or changed: tell the browser to refetch its list.

    Everything else that changes a room happens inside a WebSocket command, which
    answers on the spot. A room OPENED or JOINED by the agent has no socket command in
    flight, so nothing looked at the store and the row was missing until the whole
    interface was reloaded by hand.
    """
    try:
        from vaf.core.web_interface import notify_rooms_changed
        notify_rooms_changed(user_scope_id)
    except Exception:
        pass


def _card(skills: str = "") -> Dict[str, Any]:
    """What this agent tells the room about itself.

    The protocol has carried this slot since the first release and nothing filled it,
    so every panel and every foreign agent saw a name, a role, and nothing about what
    the thing behind the name can actually DO. A room is agents deciding who to ask;
    without this they are deciding by name alone.

    It is SELF-DESCRIPTION and is displayed as such. It never grants anything - a card
    claiming a role changes no role, which is checked - so the honest thing is to let
    the agent say what it is good for and let the reader weigh it.
    """
    from vaf.core.config import Config

    described = str(skills or "").strip()
    if not described:
        # A default that is true of every VAF agent rather than a flattering guess at
        # this one: what it can do depends on the tools it was built with, and naming
        # abilities it may not have would be worse than naming none.
        described = ("general assistant: reads and writes files in its own workspace, "
                     "runs code, searches, and can delegate work to sub-agents")
    return {
        "kind": "VAF agent",
        "skills": described[:400],
        "model": str(Config.get("model_name", "") or ""),
    }


def _open(room_id: str):
    from vaf.core.a2a.room import Room
    return Room.open(str(room_id))


def _own_display(kwargs: Dict[str, Any]) -> str:
    """The name this agent introduces itself under when the model names none.

    The agent's OWN persona name first - the same identity every other surface
    greets with - and the product name only as the last resort. This is the
    A2A-card idea applied to the name: an agent presents ITS identity, not its
    vendor's. Measured live: a room opened without an explicit display seated
    the agent as "VAF" while its persona was named, and the user asked whether
    the agent had renamed itself.

    Resolved through the registered persona resolver, never by importing the
    auth layer from here (the shrink-only baseline in
    tests/test_framework_auth_layering.py is the ledger of that debt): the
    harness registers its user-store answer in vaf/main.py, and an embedder
    that registers nothing keeps the product name as the last resort.
    """
    explicit = str(kwargs.get("display") or "").strip()
    if explicit:
        return explicit
    try:
        from vaf.core.tool_dispatch import resolve_agent_display_name
        return resolve_agent_display_name(kwargs.get("username")) or "VAF"
    except Exception:
        return "VAF"


class RoomJoinTool(BaseTool):
    """
    Join an agent-to-agent room so you can read it and speak in it.

    A room is a group chat shared with other agents, which may be VAF agents or
    foreign ones. Use this when the user asks you to join, enter or take part in a
    room and gives you its id.
    """
    name = "room_join"
    category    = "rooms"
    description = (
        "Join an existing A2A chat (agent-to-agent room, group chat with other agents) "
        "by id, so you can read and write in it. Use when the user asks you to enter "
        "or take part in one, or gives you a room id. To START one instead, use "
        "room_open."
    )
    identity_kwargs = ("user_scope_id", "user_role", "username")
    permission_level = "write"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "room_id": {"type": "string", "description": "Id of the room to join."},
            "display": {"type": "string",
                        "description": "Name other participants see. Defaults to the agent's name."},
            "skills": {
                "type": "string",
                "description": (
                    "One line about what YOU can do that is useful in this room, so the "
                    "other agents know who to ask - for example 'writes and reviews "
                    "Python, reads this machine's logs'. Everyone in the room sees it."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["observe", "assist", "autonomous"],
                "description": (
                    "How far you may act on what arrives here. 'observe' = read only, "
                    "'assist' = talk and ask the user before changing anything (default), "
                    "'autonomous' = act without asking. Only set this when the USER says so."
                ),
            },
        },
        "required": ["room_id"],
    }
    input_aliases = {"room_id": ["room", "id"], "display": ["name", "as"],
                     "skills": ["abilities", "can"]}

    def run(self, **kwargs) -> str:
        from vaf.core.a2a.room import DEFAULT_MODE, RoomError, derive_peer_id
        from vaf.core.a2a.store import StoreError

        room_id = str(kwargs.get("room_id") or "").strip()
        if not room_id:
            return "Error: room_id is required."
        key = _acting_key(kwargs.get("user_scope_id"))
        display = _own_display(kwargs)
        mode = str(kwargs.get("mode") or DEFAULT_MODE)

        try:
            room = _open(room_id)
        except StoreError:
            return f"Error: there is no room called '{room_id}' on this machine."

        peer_id = derive_peer_id(key, room_id)
        existing = room.identity_for(key)
        if existing is not None:
            room.set_mode(existing, mode)
            # Calling this again is how a member updates what it says about itself.
            # Without it, anybody who joined without a card was stuck on "said nothing
            # about what it can do" forever, in a room whose whole point is agents
            # deciding who to ask.
            said = str(kwargs.get("skills") or "").strip()
            if said or kwargs.get("display"):
                room.introduce(existing, display=display if kwargs.get("display") else "",
                               card=_card(said) if said else None)
            _announce(kwargs.get("user_scope_id"))
            return (f"Already a member of '{room_id}' as {existing.display} "
                    f"({existing.role}); mode is now {mode}"
                    + (", and your description is updated." if said else "."))
        try:
            identity = room.join(
                display=display, peer_id=peer_id,
                scope_id=kwargs.get("user_scope_id"), mode=mode,
                card=_card(str(kwargs.get("skills") or "")),
                participant_key=key,
            )
        except RoomError as e:
            return f"Could not join '{room_id}': {e}"
        _announce(kwargs.get("user_scope_id"))
        return (f"Joined room '{room_id}' as {identity.display} ({identity.role}), "
                f"mode {mode}. Members: "
                f"{', '.join(m['display'] for m in room.members().values())}.")


class RoomSendTool(BaseTool):
    """
    Say something in an agent-to-agent room you have joined.

    One door for every kind of message. What you are allowed to send follows from
    your role in that room, which the room decides, not this tool.
    """
    name = "room_send"
    category    = "rooms"
    description = (
        "Write a message into an A2A chat (agent room) you have joined. This is the "
        "ONLY way the other agents can read you - text you write outside a tool call "
        "goes to your own user instead. "
        "kind: say (normal message), ask (a question), answer (a reply), "
        "report (result of a task, with a status), directive (an instruction, "
        "leaders only, and never in a round)."
    )
    identity_kwargs = ("user_scope_id", "user_role")
    permission_level = "write"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "room_id": {"type": "string", "description": "Id of the room."},
            "text": {"type": "string", "description": "What to say."},
            "kind": {
                "type": "string",
                "enum": ["say", "ask", "answer", "report", "directive", "vote"],
                "description": "What sort of message this is. Defaults to 'say'.",
            },
            "to_peer": {"type": "string",
                        "description": ("Optional peer id to address. Everyone can still "
                                        "read it. Usually you do not know the id: start "
                                        "the text with the member's name as the room shows "
                                        "it instead (\"@Codex51 the logs are clean\") and "
                                        "the room resolves it.")},
            "reply_to": {"type": "string", "description": "Optional id of the message you answer."},
            "status": {
                "type": "string",
                "enum": ["submitted", "working", "input_required",
                         "completed", "failed", "rejected", "canceled"],
                "description": "For kind=report: how the task stands.",
            },
            "progress_done": {
                "type": "integer",
                "description": ("For kind=report: how many steps of the work are "
                                "done. Send it again as the number grows - the "
                                "others read where you are without asking."),
            },
            "progress_total": {
                "type": "integer",
                "description": "For kind=report: how many steps there are in total.",
            },
            "step": {
                "type": "string",
                "description": ("For kind=report: what you are doing right now, in "
                                "a few words."),
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": ("For kind=vote: the answers to choose from. "
                                "Defaults to yes/no."),
            },
            "choice": {
                "type": "string",
                "description": ("Casting a ballot: use kind=answer with reply_to "
                                "set to the vote's id, and put your choice here. "
                                "Voting again replaces your earlier ballot."),
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": ("Files in the room's SHARED FOLDER this message is "
                                "about, by name (e.g. 'wording.html'). Save the file "
                                "there first; this names it, so the others see what "
                                "you left instead of having to read it out of your "
                                "sentence."),
            },
        },
        "required": ["room_id", "text"],
    }
    input_aliases = {"room_id": ["room", "id"], "text": ["message", "content"],
                     "files": ["file", "attachments", "attach"]}

    def run(self, **kwargs) -> str:
        from vaf.core.a2a.frame import KINDS
        from vaf.core.a2a.room import RoomError
        from vaf.core.a2a.store import StoreError

        room_id = str(kwargs.get("room_id") or "").strip()
        text = str(kwargs.get("text") or "").strip()
        kind = str(kwargs.get("kind") or "say").strip() or "say"
        if not room_id or not text:
            return "Error: room_id and text are required."
        if kind not in KINDS:
            return (f"Error: '{kind}' is not a kind of message. Use say, ask, answer, "
                "report, directive or vote.")

        try:
            room = _open(room_id)
        except StoreError:
            return f"Error: there is no room called '{room_id}' on this machine."

        identity = room.identity_for(_acting_key(kwargs.get("user_scope_id")))
        if identity is None:
            return f"Error: you have not joined '{room_id}'. Use room_join first."
        if room.closed:
            return f"Room '{room_id}' is closed; nothing more can be written to it."

        body: Dict[str, Any] = {"text": text}
        if kind == "vote":
            body["options"] = list(kwargs.get("options") or [])
        if kind == "answer" and str(kwargs.get("choice") or "").strip():
            # A ballot is an answer that names its choice - the protocol already
            # has "this answers that", so a vote needed no second way to say it.
            body["choice"] = str(kwargs["choice"])
        if kind == "report":
            body["status"] = str(kwargs.get("status") or "completed")
            # Normalised by the reader that consumes it, so this tool cannot put
            # a shape on the wire that the room would refuse from a stranger.
            from vaf.core.a2a.frame import read_progress
            raw = {k: v for k, v in (
                ("done", kwargs.get("progress_done")),
                ("total", kwargs.get("progress_total")),
                ("step", kwargs.get("step")),
            ) if v is not None}
            progress = read_progress({"progress": raw}) if raw else None
            if progress:
                body["progress"] = progress
        # Cleaned by the room's own reader, so this tool cannot put a shape on
        # the wire that the room would refuse from a stranger - the same
        # function every surface reads such a reference with.
        from vaf.core.a2a.room import attached_files
        named = attached_files({"files": kwargs.get("files")})
        if named:
            body["files"] = named
        # WHO this is for is the room's answer, not this tool's: an explicit peer,
        # else a leading "@Name" resolved against the members, else everyone. The
        # tool used to set only the first and the skill promised the agent the
        # second, so every mention it wrote woke the whole room as plain text.
        payload: Dict[str, Any] = {"kind": kind, "body": body,
                                   "to": room.addressee(text, to_peer=str(kwargs.get("to_peer") or ""))}
        if kwargs.get("reply_to"):
            from vaf.core.a2a.frame import plausible_frame_id
            if not plausible_frame_id(kwargs["reply_to"]):
                return ("Error: reply_to takes the ID of the message you answer "
                        "(the 'id' field of the line you read), never its text. "
                        "Send again with that id.")
            payload["reply_to"] = str(kwargs["reply_to"])

        try:
            frame = room.ingest(payload, identity=identity)
        except RoomError as e:
            # The room's refusal is the message. Rewording it here would create a
            # second explanation of the same rule.
            return f"Refused by the room: {e}"
        return f"Sent to '{room_id}' as {identity.display} ({identity.role}). Message id {frame.id}."


class RoomReadTool(BaseTool):
    """
    Read what has been said in your rooms since you last looked.

    Reading takes nothing away from anyone else, and it moves only your own position.
    """
    name = "room_read"
    category    = "rooms"
    description = (
        "Read new messages from an A2A chat (agent room), or list your A2A chats and "
        "how many unread messages each has when no room_id is given."
    )
    identity_kwargs = ("user_scope_id", "user_role")
    permission_level = "read"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "room_id": {"type": "string",
                        "description": "Room to read. Leave empty to list all your rooms."},
            "limit": {"type": "integer", "description": "Most recent messages to show (default 30)."},
            "all_messages": {"type": "boolean",
                             "description": "Read the whole transcript instead of only what is new."},
        },
        "required": [],
    }
    input_aliases = {"room_id": ["room", "id"]}

    def run(self, **kwargs) -> str:
        from vaf.core.a2a.room import joined_rooms, unread_counts
        from vaf.core.a2a.store import StoreError

        key = _acting_key(kwargs.get("user_scope_id"))
        room_id = str(kwargs.get("room_id") or "").strip()
        limit = max(1, int(kwargs.get("limit") or 30))

        if not room_id:
            rooms = joined_rooms(key)
            if not rooms:
                return "You have not joined any rooms."
            pending = unread_counts(key)
            lines = ["Your rooms:"]
            for room, identity in rooms:
                lines.append(
                    f"- {room.room_id} ({room.kind}) as {identity.display} [{identity.role}], "
                    f"{pending.get(room.room_id, 0)} unread, mode "
                    f"{room.mode_of(identity.peer_id)}"
                )
            return "\n".join(lines)

        try:
            room = _open(room_id)
        except StoreError:
            return f"Error: there is no room called '{room_id}' on this machine."
        identity = room.identity_for(key)
        if identity is None:
            return f"Error: you have not joined '{room_id}'. Use room_join first."

        since = 0 if kwargs.get("all_messages") else room.store.cursor(identity.peer_id)
        rows = room.transcript(since_lamport=since)
        rows = [r for r in rows if r["peer"] != identity.peer_id]
        # WHAT THE ROOM IS WORKING ON, beside what was said. Reading a room and not
        # being told that somebody else already took the thing you were about to take
        # is how two agents do one job twice - and a foreign agent has had this all
        # along (`vaf a2a tasks`) while the agent whose room it is had no way to ask
        # at all. Appended to the reader rather than given a sixth tool: it is the
        # same question ("what is going on here"), one line further.
        board = _board_summary(room, identity)
        if not rows:
            return (f"Nothing new in '{room_id}'." + (f"\n\n{board}" if board else ""))
        shown = rows[-limit:]
        # The cursor moves only after the text exists, so an interruption between the
        # two costs a repeat rather than a lost message.
        rendered = _render(shown) + (f"\n\n{board}" if board else "")
        room.store.set_cursor(identity.peer_id, shown[-1]["lamport"])
        return rendered


def _board_summary(room, identity) -> str:
    """The room's open work, in a few lines, for whoever is reading it.

    Capped hard: a room with thirty tasks would otherwise turn a read into a wall and
    push out the messages it was asked for. Silent work is COUNTED rather than listed -
    the room cannot tell a long run from an abandoned one, and a reader mostly needs to
    know that some exists.
    """
    try:
        board = room.tasks()
    except Exception:
        return ""
    done = ("completed", "failed", "rejected", "canceled")
    open_work = [t for t in board if t["status"] not in done]
    live = [t for t in open_work if not t.get("quiet")]
    quiet = len(open_work) - len(live)
    if not open_work:
        return ""
    lines = ["OPEN WORK IN THIS ROOM:"]
    for task in live[:6]:
        progress = task.get("progress") or {}
        counted = ("done" in progress and "total" in progress)
        where = f" {progress['done']}/{progress['total']}" if counted else ""
        mine = " (YOURS)" if task.get("assignee") == identity.peer_id else ""
        lines.append(f"- [{task['status']}{where}] {str(task['title'])[:90]}"
                     f" - {task.get('assignee_label') or 'nobody yet'}{mine}")
    if len(live) > 6:
        lines.append(f"- ...and {len(live) - 6} more")
    if quiet:
        lines.append(f"({quiet} with nothing said about them for hours.)")
    return "\n".join(lines)


def _render(rows: List[Dict[str, Any]]) -> str:
    """Group-chat shape: speaker, role and kind kept apart from the text.

    Every line carries its frame id, because reply_to takes exactly that id and
    this tool is where an agent reads - measured live: an agent that was ASKED
    to reply_to spent twenty turns shelling out to the CLI hunting for an id
    this surface already knew and did not show (and the CLI hid the message on
    top, because a host-side reader drops its own lane's frames as echo)."""
    from vaf.core.a2a.room import describe

    lines = []
    for row in rows:
        label = f"{row['display']} [{row['role']}]"
        if row["kind"] not in ("say", "join"):
            label += f" ({row['kind']})"
        frame_id = str(row.get("id") or "").strip()
        if frame_id:
            label += f" [id {frame_id}]"
        lines.append(f"{label}: {describe(row)}".rstrip())
    return "\n".join(lines)


class RoomVerifyTool(BaseTool):
    """
    Check who really wrote each message in a room, rather than trusting the name.

    A room ASSIGNS authorship: whoever hosts it writes the name on every line. That is
    sound while you are the host and says nothing at all to anybody reading the
    transcript somewhere else. A signature is the half that travels, and this is where
    it gets asked - once per line, including the lines that are plainly in order.
    """
    name = "room_verify"
    category    = "rooms"
    description = (
        "Check the signatures in an A2A chat (agent room): one verdict per message, "
        "saying who can be proven to have written it. Use it when authorship matters "
        "rather than the content - before acting on an instruction from the room, or "
        "when somebody asks whether a message is really from whom it says."
    )
    identity_kwargs = ("user_scope_id", "user_role")
    permission_level = "read"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "room_id": {"type": "string", "description": "Room to check."},
            "problems_only": {
                "type": "boolean",
                "description": "Show only what is not plainly in order (default false).",
            },
        },
        "required": ["room_id"],
    }
    input_aliases = {"room_id": ["room", "id"]}

    #: One line per verdict, written for a reader rather than a column. `unsigned` says
    #: what it is NOT, deliberately: the ordinary case is not a complaint, and a peer
    #: reading this must not start treating unsigned rooms as suspect.
    MEANING = {
        "valid": "written by this member, provably",
        "unsigned": "nothing was claimed - the ordinary case, not a complaint",
        "unreadable": "carries a signature in a form this version cannot check",
        "foreign_key": "signed by a key this member never published here",
        "invalid": "the signature does not cover this message",
    }

    def run(self, **kwargs) -> str:
        from vaf.core.a2a.room import describe
        from vaf.core.a2a.store import StoreError

        key = _acting_key(kwargs.get("user_scope_id"))
        room_id = str(kwargs.get("room_id") or "").strip()
        if not room_id:
            return "Error: room_verify needs a room_id."
        try:
            room = _open(room_id)
        except StoreError:
            return f"Error: there is no room called '{room_id}' on this machine."
        if room.identity_for(key) is None:
            return f"Error: you have not joined '{room_id}'. Use room_join first."

        members = {p: m.get("display") or p for p, m in (room.members() or {}).items()}
        rows = []
        tally: Dict[str, int] = {}
        for frame, verdict in room.verify_frames():
            tally[verdict] = tally.get(verdict, 0) + 1
            if kwargs.get("problems_only") and verdict in ("valid", "unsigned"):
                continue
            said = describe({"kind": frame.kind, "body": frame.body,
                             "text": (frame.body or {}).get("text") or ""})
            rows.append(f"- {members.get(frame.sender, frame.sender)} "
                        f"[{frame.kind} #{frame.seq}]: {verdict} - "
                        f"{self.MEANING.get(verdict, verdict)}"
                        + (f"\n    {said}" if said else ""))

        if not tally:
            return f"'{room_id}' has nothing in it yet."
        counted = ", ".join(f"{n} {name}" for name, n in sorted(tally.items()))
        head = f"Signatures in '{room_id}': {counted}."
        if not rows:
            return f"{head}\nNothing here is out of order."
        return head + "\n" + "\n".join(rows)


class RoomOpenTool(BaseTool):
    """
    Open a new agent-to-agent room and join it yourself.

    Use this when the user asks you to start a room, open a group chat with other
    agents, or bring somebody in to work with you. Opening a room does not invite
    anybody; use room_invite for each agent that should take part.
    """
    name = "room_open"
    category    = "rooms"
    description = (
        "START AN A2A CHAT (agent-to-agent chat, agent room, group chat with other "
        "agents) and join it yourself. THIS ALREADY EXISTS - never build, code or "
        "install anything for it, and never use the coding agent: opening the chat is "
        "this one call. Use it whenever the user asks for an A2A chat, an agent room, "
        "a group chat with another agent, or to work together with Claude, Claude "
        "Code, Codex, OpenCode or any other agent. kind: 'round' for a conversation "
        "among equals where nobody gives orders, 'chain' when you lead and the agents "
        "you invite report to you."
    )
    identity_kwargs = ("user_scope_id", "user_role", "username")
    permission_level = "write"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "topic": {"type": "string",
                      "description": "What the room is about. Shown to everyone who joins."},
            "kind": {
                "type": "string",
                "enum": ["round", "chain"],
                "description": (
                    "'round' = everybody equal, nobody may give orders (default). "
                    "'chain' = you lead, invited agents are workers who report to you."
                ),
            },
            "display": {"type": "string",
                        "description": "Name other participants see. Defaults to the agent's name."},
            "skills": {
                "type": "string",
                "description": (
                    "One line about what YOU can do that is useful in this room, so the "
                    "agents you invite know who to ask. Everyone in the room sees it."
                ),
            },
        },
        "required": [],
    }
    input_aliases = {"topic": ["subject", "about"], "kind": ["type"],
                     "display": ["name", "as"], "skills": ["abilities", "can"]}

    def run(self, **kwargs) -> str:
        from vaf.core.a2a.room import ROOM_KINDS, Room, RoomError, derive_peer_id

        # "Open room X" is the natural phrasing for ENTERING an existing room,
        # and this tool only ever CREATES one - measured live: a room_id passed
        # here was silently dropped and a stray empty room appeared in the
        # user's sidebar. A miscall must answer with the correction, never do
        # something else without a word.
        stray_id = str(kwargs.get("room_id") or kwargs.get("room") or "").strip()
        if stray_id:
            return (
                f"Error: room_open only STARTS A NEW room and cannot enter an "
                f"existing one - nothing was created. '{stray_id}' already "
                f"exists: read it with room_read, write with room_send, and "
                f"bring agents in with room_invite (all take room_id)."
            )

        kind = str(kwargs.get("kind") or "round").strip().lower()
        if kind not in ROOM_KINDS:
            return f"Error: kind must be one of {', '.join(ROOM_KINDS)}."
        scope = kwargs.get("user_scope_id")
        key = _acting_key(scope)
        display = _own_display(kwargs)
        topic = str(kwargs.get("topic") or "").strip()

        # A REPEAT, refused. Measured live: this tool ran twice inside one task,
        # twenty-one seconds apart, and the room got opened, spoken in and invited
        # into twice - after which the agent explained the second room to its user
        # as a "double submission", which the queue log shows never happened. The
        # answer names the room that already exists, because that is what the caller
        # wanted in the first place and a refusal it can act on beats a duplicate it
        # has to apologise for.
        from vaf.core.a2a.room import just_opened
        existing = just_opened(key, topic)
        if existing:
            return (f"You already opened a room for '{topic}' a moment ago: "
                    f"'{existing}'. Use that one - room_send writes into it, "
                    f"room_invite brings somebody in. Open a second room under the "
                    f"same topic only by giving it a topic of its own.")

        try:
            room = Room.create(kind=kind, owner_scope=scope, topic=topic)
            # The opener joins as itself. In a chain that seat is the leader's, which
            # is what makes "open a room and bring somebody in" mean what a user
            # expects it to mean.
            identity = room.join(display=display, scope_id=scope,
                                 peer_id=derive_peer_id(key, room.room_id),
                                 card=_card(str(kwargs.get("skills") or "")),
                                 participant_key=key)
        except RoomError as e:
            return f"Could not open the room: {e}"

        _announce(scope)
        return (f"Opened room '{room.room_id}' ({kind}"
                f"{f', about: {topic}' if topic else ''}) and joined it as "
                f"{identity.display} ({identity.role}). "
                f"Use room_invite with this room id to bring an agent in.")


class RoomInviteTool(BaseTool):
    """
    Invite another agent into a room, and get the briefing to hand over.

    Every call mints a NEW single-use invitation, so calling it again is how a second
    or third agent is brought into the same room. The result contains a block of
    instructions meant to be given to the other agent exactly as it is.
    """
    name = "room_invite"
    category    = "rooms"
    description = (
        "Invite another agent into an A2A chat (agent room) you are in, and return the "
        "ready-made briefing to hand over. Call it again for each further agent. Use "
        "when the user asks you to invite somebody - including agents that are not "
        "VAF, such as Claude, Claude Code, Codex or OpenCode. INVITING IS THIS CALL "
        "PLUS SHOWING THE TEXT: nothing to install, no API key, no account, no setup, "
        "no code, and you never join on the other agent's behalf. To invite a PERSON "
        "who has a VAF account on this server, pass their user name as `account`: "
        "they get the room in their own sidebar and accept or decline there - no "
        "text to hand over."
    )
    identity_kwargs = ("user_scope_id", "user_role")
    permission_level = "write"
    # The briefing IS the deliverable, and the result text below orders the model
    # to pass it on "unchanged and complete". The funnel's default cap cut it mid
    # block anyway (live incident: the agent refused to hand over the torn half
    # and spent the turn hunting the rest in encrypted stores and capped logs).
    # Bounded by construction: one briefing, assembled once in invite.invitation.
    result_is_deliverable = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "room_id": {"type": "string", "description": "Id of the room to invite into."},
            "display": {"type": "string",
                        "description": "Name the invited agent will appear under, e.g. 'Codex'."},
            "ttl": {"type": "integer",
                    "description": "Seconds the invitation stays valid. Default 3600."},
            "account": {"type": "string",
                        "description": ("User name of a VAF ACCOUNT on this server to invite "
                                        "instead of a foreign agent. The person answers in "
                                        "their own sidebar; the room becomes shared across "
                                        "accounts (every member reads everything).")},
        },
        "required": ["room_id"],
    }
    input_aliases = {"room_id": ["room", "id"], "display": ["name", "who", "guest"],
                     "ttl": ["expires_in", "valid_for"],
                     "account": ["user", "username", "person"]}

    def run(self, **kwargs) -> str:
        from vaf.core.a2a.invite import invitation
        from vaf.core.a2a.room import RoomError
        from vaf.core.a2a.store import StoreError

        room_id = str(kwargs.get("room_id") or "").strip()
        if not room_id:
            return "Error: room_id is required."
        display = str(kwargs.get("display") or "guest").strip() or "guest"
        try:
            ttl = float(kwargs.get("ttl") or 3600)
        except (TypeError, ValueError):
            ttl = 3600.0

        try:
            room = _open(room_id)
        except StoreError:
            return f"Error: there is no room called '{room_id}' on this machine."

        identity = room.identity_for(_acting_key(kwargs.get("user_scope_id")))
        if identity is None:
            return (f"Error: you are not a member of '{room_id}', and only a member "
                    f"may invite. Join it first.")
        account = str(kwargs.get("account") or "").strip()
        if account:
            return self._invite_account(room, identity, account, ttl,
                                        inviter_scope=kwargs.get("user_scope_id"))
        try:
            # Assembled by the room layer, exactly as `vaf a2a invite` gets it. Two
            # inviters telling a guest two different things is the whole reason that
            # assembly does not live in either caller.
            row = invitation(room, identity, display=display, ttl_s=ttl)
        except RoomError as e:
            return f"Could not invite into '{room_id}': {e}"

        return (
            f"Invitation for {display} to join '{room_id}' as {row['role']}, valid for "
            f"{row['expires_in']} seconds.\n\n"
            "YOUR JOB IS DONE APART FROM ONE THING: show the block below to your user "
            "so they can pass it on. Nothing is installed, no key is needed, nothing "
            "is set up, and you do NOT join for the other agent - it redeems this "
            "invitation itself, wherever it already runs. If the other agent is not "
            "reachable, that is your user's to solve and not a task to start.\n\n"
            "GIVE THE BLOCK BELOW EXACTLY AS IT IS, unchanged and complete. It is "
            "written for the other agent to read, not for you to summarise, and it is "
            "single-use: a shortened version leaves it unable to join or unsure what "
            "to do once it has.\n\n"
            "----- copy from here -----\n"
            f"{row['briefing']}"
            "----- to here -----"
        )

    def _invite_account(self, room, identity, account: str, ttl: float, *,
                        inviter_scope) -> str:
        """The account door, on the same primitive the browser's panel and the CLI
        use: `Room.invite_account`. The agent is the host's hand here, so opening a
        one-account room to other accounts is done for the user and SAID, because a
        room every member reads is the one consequence they have to know about."""
        import time as _time

        from vaf.core.a2a.room import RoomError
        from vaf.core.config import resolve_caller_username, scope_id_for_username

        scope = scope_id_for_username(account)
        if not scope:
            return (f"Error: there is no VAF account called '{account}' on this server. "
                    f"Ask the user for the exact user name.")
        opened = False
        try:
            if not room.manifest.get("multi_scope"):
                room.open_to_accounts(identity)
                opened = True
            row = room.invite_account(identity, scope, display=account, ttl_s=ttl)
        except RoomError as e:
            return f"Could not invite '{account}' into '{room.room_id}': {e}"
        try:
            from vaf.core.web_interface import announce_room_invitation
            announce_room_invitation(
                room, row, inviter_scope=inviter_scope, invitee_scope=scope,
                inviter_name=resolve_caller_username(None, inviter_scope, allow_lookup=True))
        except Exception:
            pass
        _announce(inviter_scope)
        minutes = max(1, int(float(row.get("expires_at") or 0) - _time.time()) // 60)
        return (
            f"Invited the account '{account}' into '{room.room_id}'. They now see the room "
            f"in their own sidebar and decide there; the invitation stays open for about "
            f"{minutes} minutes and you will see 'accepted' or 'declined' in the room's "
            f"invitations. Nothing to hand over and nothing to install."
            + (" NOTE: the room was private to this account and is now SHARED - every "
               "member reads everything said in it from here on; newcomers see only "
               "what is written after they join." if opened else "")
        )
