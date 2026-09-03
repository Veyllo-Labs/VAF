# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Event triggers for automations: an automation that runs when something happens in a room.

Until this existed an automation could only be due by the clock, and the reason was deeper
than the frequency enum: the "is it due" decision was not in VAF at all. Clock automations
hand it to the `schedule` package, which cannot express a condition. This module IS that
decision for events, and everything else was already there and is reused as it stands: the
run half of an automation (`AutomationManager._run_scheduled_task`) is trigger-agnostic, the
arrival detector is the room store's own `read_since`, and the matcher is `text_match`.

Two trigger kinds, both over an A2A room the automation's owner is a member of:

- ``room_message``: a conversational frame arrives, optionally containing a text (folded
  the way every match in this tree is folded, so "Deploy" finds "deploy").
- ``room_reaction``: an emoji lands on a message, optionally a specific emoji. With point 4
  this is the approval button: the person's emoji on the agent's report runs the automation
  that was waiting for it.

Three rules, each written after the failure it prevents:

- **Loop guard.** The frames of the owner's OWN AGENT never fire a trigger. The automation
  runs as that agent, so a run that posts into the room and thereby re-triggers itself is a
  loop nothing else in the tree would stop. The owner's own person (the cli lane) DOES fire
  one, which is the whole point of a reaction trigger.
- **Membership guard.** A task fires only for a room its owner is in on some lane. A trigger
  naming a room the account is not in is not an error, it is nothing - the same answer the
  wake poll gives a room it has no seat in.
- **The cursor starts now, never at zero.** A trigger created today must not fire on last
  week's transcript. The first time a process sees a task it starts watching at the room's
  newest frame; after a fire the position is persisted on the task (``trigger.cursor``), so a
  restart cannot fire the same frames twice and, from then on, frames that arrived while the
  process was down are caught up on the next tick, bundled into one run.

Pure enough to run from any scheduler: VAF's loop calls ``RoomTriggerWatch.tick`` every tick
beside the reminders; an embedder with a scheduler of their own calls it the same way.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, NamedTuple, Optional

# The frequency value of a triggered automation. A string here rather than the enum,
# because the enum lives in automation.py and this module must not import it at load.
ON_EVENT = "on_event"

TRIGGER_KINDS = ("room_message", "room_reaction")

# The same cap a reaction itself has: an emoji, or a short token like "+1".
EMOJI_WIDTH = 16
MATCH_WIDTH = 200


class TriggerHit(NamedTuple):
    """One task that is due, and why."""
    task: Any
    frames: List[Any]
    newest: int
    labels: Dict[str, str]


def read_trigger(value: Any) -> Optional[Dict[str, Any]]:
    """A trigger as something this code can act on, or None.

    Read defensively: a record arrives from a file or a caller and is coerced at the
    boundary rather than trusted. A room id that cannot be a path component, a kind this
    version does not know, a cursor that is not a number - each reads as "no trigger",
    which the scheduler treats as a task that is never due, never as an exception.
    """
    from vaf.core.a2a.store import UnsafeName, check_name

    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind") or "").strip()
    if kind not in TRIGGER_KINDS:
        return None
    try:
        room_id = check_name(str(value.get("room_id") or ""), what="room id")
    except UnsafeName:
        return None
    out: Dict[str, Any] = {"kind": kind, "room_id": room_id}
    match = str(value.get("match") or "").strip()[:MATCH_WIDTH]
    if match and kind == "room_message":
        out["match"] = match
    emoji = str(value.get("emoji") or "").strip()[:EMOJI_WIDTH]
    if emoji and kind == "room_reaction":
        out["emoji"] = emoji
    sender = str(value.get("from") or "").strip()
    if sender:
        out["from"] = sender
    try:
        cursor = int(value.get("cursor") or 0)
    except (TypeError, ValueError):
        cursor = 0
    if cursor > 0:
        out["cursor"] = cursor
    return out


def trigger_label(trigger: Any) -> str:
    """One line a person reads: what has to happen, where."""
    read = read_trigger(trigger)
    if read is None:
        return "on an event (no room)"
    if read["kind"] == "room_reaction":
        what = f"reaction {read['emoji']}" if read.get("emoji") else "any reaction"
    else:
        what = f"message containing '{read['match']}'" if read.get("match") else "any message"
    who = f" from {read['from']}" if read.get("from") else ""
    return f"on {what}{who} in room {read['room_id']}"


def matching_frames(trigger: Any, frames: Iterable[Any], *,
                    exclude_senders: Iterable[str] = ()) -> List[Any]:
    """The frames among `frames` that satisfy the trigger, in the order given."""
    from vaf.core.a2a.room import NON_CONVERSATION_KINDS
    from vaf.core.text_match import contains_any

    read = read_trigger(trigger)
    if read is None:
        return []
    excluded = set(exclude_senders)
    wanted_from = read.get("from")
    hits = []
    for frame in frames:
        if frame.sender in excluded:
            continue
        if wanted_from and frame.sender != wanted_from:
            continue
        body = frame.body or {}
        if read["kind"] == "room_reaction":
            if frame.kind != "reaction":
                continue
            if read.get("emoji") and str(body.get("emoji") or "") != read["emoji"]:
                continue
        else:
            # Something SAID: not bookkeeping, not a check-in, not a reaction, and not
            # a kind this version cannot read the text of.
            if frame.kind in NON_CONVERSATION_KINDS or not frame.kind_known:
                continue
            if read.get("match") and not contains_any(str(body.get("text") or ""), [read["match"]]):
                continue
        hits.append(frame)
    return hits


class RoomTriggerWatch:
    """The "is it due" decision for event-driven automations, asked once per tick.

    Holds one cursor per task for the life of the process. Stateless otherwise: every
    tick opens the rooms the enabled triggered tasks name and reads what arrived since
    the cursor. The caller runs the hits and persists ``newest`` as ``trigger.cursor``.
    """

    def __init__(self, *, base=None) -> None:
        self._base = base
        self._cursors: Dict[str, int] = {}

    def tick(self, tasks: Iterable[Any]) -> List[TriggerHit]:
        from vaf.core.a2a.room import Room, derive_peer_id, participant_key

        hits: List[TriggerHit] = []
        for task in tasks:
            if str(getattr(task, "frequency", "") or "") != ON_EVENT:
                continue
            read = read_trigger(getattr(task, "trigger", None))
            if read is None:
                continue
            scope = str(getattr(task, "user_scope_id", "") or "")
            if not scope:
                # A root task has no account, so it has no lane in any room and the
                # loop guard could not name the agent to exclude. Never due.
                continue
            try:
                room = Room.open(read["room_id"], base=self._base)
            except Exception:
                continue
            if room.closed:
                continue
            keys = {lane: participant_key(lane, scope) for lane in ("cli", "agent")}
            if not any(room.identity_for(key) is not None for key in keys.values()):
                continue
            cursor = self._cursors.get(task.id)
            if cursor is None:
                # First sight in this process: the persisted position after a fire, or
                # the room as it stands now. Never zero. Remembered at once, hit or
                # not: recomputing "now" on every quiet tick would walk the cursor
                # past everything that arrives and the trigger would never fire.
                cursor = int(read.get("cursor") or 0) or room.store.highest_lamport()
                self._cursors[task.id] = cursor
            frames = room.store.read_since(cursor)
            if not frames:
                continue
            newest = int(frames[-1].lamport)
            self._cursors[task.id] = newest
            own_agent = derive_peer_id(keys["agent"], room.room_id)
            matched = matching_frames(read, frames, exclude_senders={own_agent})
            if matched:
                try:
                    labels = room.labels()
                except Exception:
                    labels = {}
                hits.append(TriggerHit(task, matched, newest, labels))
        return hits


def trigger_context(trigger: Any, frames: Iterable[Any],
                    labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """What a run is told about why it runs: the room, the rule, and the frames as rows."""
    read = read_trigger(trigger) or {}
    names = labels or {}
    rows = [{"id": str(frame.id), "from": str(frame.sender),
             "who": names.get(frame.sender) or str(frame.sender),
             "kind": str(frame.kind),
             "text": str((frame.body or {}).get("text") or ""),
             "reply_to": str(frame.reply_to or "")} for frame in frames]
    return {"kind": str(read.get("kind") or ""), "room_id": str(read.get("room_id") or ""),
            "label": trigger_label(read) if read else "", "frames": rows}


def prompt_with_trigger(prompt: str, context: Dict[str, Any]) -> str:
    """The prompt lane's form: the task's own prompt, then what triggered it, then what
    to do about that. Appended at run time and never stored on the task."""
    lines = []
    for row in context.get("frames", []):
        label = f"- {row['who']} [{row['kind']}] [id {row['id']}]"
        if row["kind"] == "reaction" and row.get("reply_to"):
            label += f" on {row['reply_to']}"
        lines.append(f"{label}: {row['text']}".rstrip())
    block = (f"\n\nTRIGGERED BY AN EVENT in room {context.get('room_id', '')} "
             f"({context.get('label', '')}):\n" + "\n".join(lines) +
             "\n\nAct on what triggered you. If the room expects an answer and you are a member "
             "of it, answer IN THE ROOM with room_send (reply_to the id above); otherwise do the "
             "task and deliver the result as usual.")
    return (prompt or "") + block


def trigger_variables(context: Dict[str, Any]) -> Dict[str, str]:
    """The workflow lane's form: template variables a step may name as {trigger_text} etc."""
    rows = context.get("frames", [])
    first = rows[0] if rows else {}
    return {"trigger_room": str(context.get("room_id") or ""),
            "trigger_kind": str(context.get("kind") or ""),
            "trigger_text": "\n".join(f"{r['who']}: {r['text']}".rstrip() for r in rows),
            "trigger_frame_id": str(first.get("id") or ""),
            "trigger_from": str(first.get("who") or "")}
