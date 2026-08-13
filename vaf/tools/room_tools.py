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


class RoomJoinTool(BaseTool):
    """
    Join an agent-to-agent room so you can read it and speak in it.

    A room is a group chat shared with other agents, which may be VAF agents or
    foreign ones. Use this when the user asks you to join, enter or take part in a
    room and gives you its id.
    """
    name = "room_join"
    description = (
        "Join an agent-to-agent room by id so you can read and write in it. "
        "Use when the user asks you to enter or take part in a room."
    )
    identity_kwargs = ("user_scope_id", "user_role")
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
        display = str(kwargs.get("display") or "VAF").strip() or "VAF"
        mode = str(kwargs.get("mode") or DEFAULT_MODE)

        try:
            room = _open(room_id)
        except StoreError:
            return f"Error: there is no room called '{room_id}' on this machine."

        peer_id = derive_peer_id(key, room_id)
        existing = room.identity_for(key)
        if existing is not None:
            room.set_mode(existing, mode)
            return (f"Already a member of '{room_id}' as {existing.display} "
                    f"({existing.role}); mode is now {mode}.")
        try:
            identity = room.join(
                display=display, peer_id=peer_id,
                scope_id=kwargs.get("user_scope_id"), mode=mode,
                card=_card(str(kwargs.get("skills") or "")),
            )
        except RoomError as e:
            return f"Could not join '{room_id}': {e}"
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
    description = (
        "Write a message into an agent-to-agent room you have joined. "
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
                "enum": ["say", "ask", "answer", "report", "directive"],
                "description": "What sort of message this is. Defaults to 'say'.",
            },
            "to_peer": {"type": "string",
                        "description": "Optional peer id to address. Everyone can still read it."},
            "reply_to": {"type": "string", "description": "Optional id of the message you answer."},
            "status": {
                "type": "string",
                "enum": ["submitted", "working", "input_required",
                         "completed", "failed", "rejected", "canceled"],
                "description": "For kind=report: how the task stands.",
            },
        },
        "required": ["room_id", "text"],
    }
    input_aliases = {"room_id": ["room", "id"], "text": ["message", "content"]}

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
            return f"Error: '{kind}' is not a kind of message. Use say, ask, answer, report or directive."

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
        if kind == "report":
            body["status"] = str(kwargs.get("status") or "completed")
        payload: Dict[str, Any] = {"kind": kind, "body": body}
        if kwargs.get("to_peer"):
            payload["to"] = {"peer": str(kwargs["to_peer"])}
        if kwargs.get("reply_to"):
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
    description = (
        "Read new messages from an agent-to-agent room, or list your rooms and how "
        "many unread messages each has when no room_id is given."
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
        if not rows:
            return f"Nothing new in '{room_id}'."
        shown = rows[-limit:]
        # The cursor moves only after the text exists, so an interruption between the
        # two costs a repeat rather than a lost message.
        rendered = _render(shown)
        room.store.set_cursor(identity.peer_id, shown[-1]["lamport"])
        return rendered


def _render(rows: List[Dict[str, Any]]) -> str:
    """Group-chat shape: speaker, role and kind kept apart from the text."""
    from vaf.core.a2a.room import describe

    lines = []
    for row in rows:
        label = f"{row['display']} [{row['role']}]"
        if row["kind"] not in ("say", "join"):
            label += f" ({row['kind']})"
        lines.append(f"{label}: {describe(row)}".rstrip())
    return "\n".join(lines)


class RoomOpenTool(BaseTool):
    """
    Open a new agent-to-agent room and join it yourself.

    Use this when the user asks you to start a room, open a group chat with other
    agents, or bring somebody in to work with you. Opening a room does not invite
    anybody; use room_invite for each agent that should take part.
    """
    name = "room_open"
    description = (
        "Open a new agent-to-agent room and join it. kind: 'round' for a conversation "
        "among equals where nobody gives orders, 'chain' when you lead and the agents "
        "you invite report to you. Use when the user asks you to start a room."
    )
    identity_kwargs = ("user_scope_id", "user_role")
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

        kind = str(kwargs.get("kind") or "round").strip().lower()
        if kind not in ROOM_KINDS:
            return f"Error: kind must be one of {', '.join(ROOM_KINDS)}."
        scope = kwargs.get("user_scope_id")
        key = _acting_key(scope)
        display = str(kwargs.get("display") or "VAF").strip() or "VAF"
        topic = str(kwargs.get("topic") or "").strip()

        try:
            room = Room.create(kind=kind, owner_scope=scope, topic=topic)
            # The opener joins as itself. In a chain that seat is the leader's, which
            # is what makes "open a room and bring somebody in" mean what a user
            # expects it to mean.
            identity = room.join(display=display, scope_id=scope,
                                 peer_id=derive_peer_id(key, room.room_id),
                                 card=_card(str(kwargs.get("skills") or "")))
        except RoomError as e:
            return f"Could not open the room: {e}"

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
    description = (
        "Create an invitation for one more agent to join a room you are in, and return "
        "the ready-made briefing to hand to that agent. Call it again for each further "
        "agent. Use when the user asks you to invite somebody into a room."
    )
    identity_kwargs = ("user_scope_id", "user_role")
    permission_level = "write"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "room_id": {"type": "string", "description": "Id of the room to invite into."},
            "display": {"type": "string",
                        "description": "Name the invited agent will appear under, e.g. 'Codex'."},
            "ttl": {"type": "integer",
                    "description": "Seconds the invitation stays valid. Default 3600."},
        },
        "required": ["room_id"],
    }
    input_aliases = {"room_id": ["room", "id"], "display": ["name", "who", "guest"],
                     "ttl": ["expires_in", "valid_for"]}

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
            "GIVE THE BLOCK BELOW TO THAT AGENT EXACTLY AS IT IS, unchanged and "
            "complete. It is written for the agent to read, not for you to summarise, "
            "and it is single-use: a shortened version leaves the other agent unable "
            "to join or unsure what to do once it has.\n\n"
            "----- copy from here -----\n"
            f"{row['briefing']}"
            "----- to here -----"
        )
