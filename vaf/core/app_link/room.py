# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Veyllo App room: one conversation per account with three parties, the person, the
agent in their Veyllo App and VAF's own agent, shown at the top of the chat list once a
Veyllo App is paired.

The room has no store of its own. Its record is the two sessions that already exist: the
account's app chat ``veyllo_app_<key>`` (the person and VAF's agent) and the app agent's
request session ``veyllo_app_<key>_agent`` (devices.ask_session_id). They stay two, because a
request of the app's agent left in the person's chat was read as the person's open question.
``transcript`` is a read-only projection over both. A NEW room line (the person writing to the
app's agent, the app's agent writing into the room, a task VAF's agent handed the app and its
result) is appended to the app chat (``append_background_message``) with its ``kind``
(agent_origin.APP_ROOM_KINDS) and ``metadata.app_room``, because the app chat is where VAF's
agent runs the person's turns: it reads the room there, as other parties' words
(``session.model_text``: a label, a fence, nothing for a task line).

One line, one addressee. A line the person writes on VAF goes to the agent in the Veyllo App
(``room_msg`` with ``"to": "app_agent"``) unless it starts with "@" and the name of the
account's agent (or "@VAF"), the leading-mention rule of the agent rooms
(a2a.room.leading_mention); then it is a turn of VAF's agent in the app chat and the app only
shows it (``"to": "vaf_agent"``). What the app's agent writes into the room (``room_say``)
starts no turn of VAF's agent, also when it names it: the app's agent asks VAF's agent through
``agent_ask``, which carries that lane's limits. So a line addressed to somebody else costs no
model call on VAF. VAF's agent joins a line when it is named, when the person writes to it
from the app (``msg``) or when the app's agent asks it, and in any of those turns it may hand
the app a task through ``user_phone``, which this module records in the room.

A line to the app's agent goes to ONE device: the standing device that announced the room
(cap ``room``) and was used most recently. It waits in that device's outbox as a typed entry
(``"t": "room"``) and is acknowledged by ``ack``; the acknowledged ids are kept per account
(devices.room_ack_record), which is how a line reads delivered.

NAMED BOUNDARY, joining without being named: VAF's agent does not decide on its own to
speak up on a line addressed to the app's agent. The voice lane's un-named chime-in would do
that, at the price of an embedding per line and a configured topic list (VOICE_REFLEX.md);
a room line costs nothing as long as nobody names VAF's agent.

NAMED BOUNDARY, the app's half of "one line, one addressee": VAF sends the addressee and
never asks the app's agent to answer a line addressed to VAF's agent; that the app's agent
stays quiet on it is the app's to keep (docs/integrations/VEYLLO_APP.md).

NAMED BOUNDARY, first-party like the rest of the link (app_link/__init__.py): no facade export.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from vaf.core import agent_origin as origin
from vaf.core.app_link import CHANNEL, devices
from vaf.core.app_link import protocol as pr

logger = logging.getLogger(__name__)

ROW_ID = "app:veyllo_app"
ROW_KIND = "app_room"
ROOM_KIND = "veyllo_app"
TITLE = "Veyllo App"
PERSON = "person"
VAF_AGENT = "vaf_agent"
APP_AGENT = "app_agent"
#: The capability a Veyllo App announces in ``hello`` when it takes ``room_msg`` and sends
#: ``room_say``.
CAP = "room"
TRANSCRIPT_LIMIT = 300
#: The label of the line a confirmation of the person on the Veyllo App leaves.
CONFIRMED_NOTE = "Confirmed in the Veyllo App"

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class RoomRefused(Exception):
    """A line the room does not take: ``code`` names why, ``status`` is the HTTP status."""

    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


# ── who is who ────────────────────────────────────────────────────────────────

def agent_label(username: str) -> str:
    """The name VAF's agent goes by for the account (the persona), or "VAF"."""
    from vaf.core.app_link import agent_name
    return agent_name(username) or "VAF"


def addressee(text: Any, username: str) -> str:
    """Who a line the person writes on VAF is for: VAF's agent when it starts with "@" and
    that agent's name or "VAF" (case-insensitive), the agent in the Veyllo App otherwise."""
    from vaf.core.a2a.room import leading_mention
    name = (leading_mention(text) or "").lower()
    if name and name in {"vaf", agent_label(username).lower()}:
        return VAF_AGENT
    return APP_AGENT


def lane_on(user_scope_id: Any) -> bool:
    from vaf.core.app_link import settings
    from vaf.core.messaging_connections import channel_enabled_for_scope
    scope = str(user_scope_id or "").strip()
    return bool(scope) and bool(settings().get("enabled", True)) \
        and channel_enabled_for_scope(CHANNEL, scope)


def capable_devices(user_scope_id: Any) -> List[Dict[str, Any]]:
    """The account's standing devices that announced the room, most recently used first."""
    from vaf.core.app_link import bridge
    rows = [r for r in bridge.standing_devices(user_scope_id) if CAP in (r.get("caps") or [])]
    return sorted(rows, key=lambda r: -float(r.get("last_seen_at") or 0))


def state(user_scope_id: Any) -> Dict[str, Any]:
    """The room's state for its row and its view: how many devices are paired, whether one is
    connected, whether a line to the app's agent can be written and, when not, why."""
    from vaf.core.app_link import bridge
    scope = str(user_scope_id or "").strip()
    paired = devices.for_scope(scope) if scope else []
    reason = ""
    if not lane_on(scope):
        reason = "channel_off"
    elif not bridge.standing_devices(scope):
        reason = "no_device"
    elif not capable_devices(scope):
        reason = "needs_newer_app"
    return {"devices": len(paired), "connected": bool(bridge.connected_devices(scope)),
            "can_write": not reason, "reason": reason}


# ── the record: two sessions, one projection ──────────────────────────────────

def _load(session_id: str):
    from vaf.core.session import get_manager
    try:
        return get_manager().load(session_id, restore_state=False, repoint=False)
    except (FileNotFoundError, ValueError):
        return None
    except Exception:
        logger.debug("app room: %s cannot be read", session_id, exc_info=True)
        return None


def _epoch(stamp: Any) -> float:
    try:
        return datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _meta(message) -> Dict[str, Any]:
    meta = getattr(message, "metadata", None)
    return meta if isinstance(meta, dict) else {}


def _room_meta(message) -> Dict[str, Any]:
    value = _meta(message).get("app_room")
    return value if isinstance(value, dict) else {}


def _append(scope: str, content: str, *, role: str, kind: str, meta: Dict[str, Any]) -> bool:
    """One room line into the account's app chat (created when it has none yet)."""
    from vaf.core.session import get_manager
    sid = devices.chat_session_id(scope)
    saved = get_manager().append_background_message(
        sid, content, role=role, kind=kind, create=True, name=TITLE, user_scope_id=scope,
        metadata={"app_room": meta})
    return saved is not None


def _line_text(scope: str, line_id: str) -> Optional[str]:
    """The text of the room line with this id in the account's app chat, or None."""
    session = _load(devices.chat_session_id(scope))
    for message in reversed(getattr(session, "messages", None) or []):
        if getattr(message, "kind", None) in origin.APP_ROOM_KINDS \
                and str(_room_meta(message).get("id") or "") == str(line_id):
            return str(message.content or "")
    return None


def _delivery(scope: str, line_id: str, acked: Dict[str, float]) -> str:
    if line_id in acked:
        return "delivered"
    if devices.held_in_outbox(scope, line_id):
        return "waiting"
    return "not_delivered"


def transcript(user_scope_id: Any, username: str = "",
               limit: int = TRANSCRIPT_LIMIT) -> List[Dict[str, Any]]:
    """The room as its view shows it: who wrote what to whom, in time order, over the app chat
    and the request session of the account. Each line: ``id``, ``peer`` and ``label`` (the
    speaker), ``to`` (``{"peer": ...}``, the addressee), ``kind`` (``say``, or ``note`` for a
    line nobody said), ``text``, ``ts`` (Unix seconds) and, for a line to the app's agent,
    ``state`` (delivered, waiting, not_delivered). Tool and system messages, empty answers and
    failed turns are not shown. A task VAF's agent handed the app during a turn is placed right
    after the person's message of that turn (both carry the turn's id), so a question stands
    above the task it caused."""
    from vaf.core.app_link.confirmations import CONFIRM_WAKE_PREFIX
    from vaf.core.session import TURN_ID_KEY, model_text
    scope = str(user_scope_id or "").strip()
    if not scope:
        return []
    labels = {PERSON: "You", VAF_AGENT: agent_label(username), APP_AGENT: TITLE}
    acked = devices.room_acked(scope)
    lines: List[Tuple[Tuple, Dict[str, Any]]] = []
    turn_anchor: Dict[str, Tuple[float, int]] = {}
    deferred: List[Tuple[str, int, float, int, Dict[str, Any]]] = []

    def _line(lid: str, peer: str, to: str, text: str, ts: float, kind: str = "say",
              **extra) -> Dict[str, Any]:
        out = {"id": lid, "peer": peer, "label": labels.get(peer, ""), "role": "",
               "to": {"peer": to} if to else {"room": True},
               "toLabel": labels.get(to, "") if to else "", "kind": kind,
               "text": text, "ts": ts}
        out.update(extra)
        return out

    chat_sid = devices.chat_session_id(scope)
    for base, sid in ((0, chat_sid), (1_000_000, devices.ask_session_id(scope))):
        session = _load(sid)
        if session is None:
            continue
        ask = base > 0
        for index, message in enumerate(session.messages):
            role = getattr(message, "role", "")
            kind = getattr(message, "kind", None)
            ts = _epoch(getattr(message, "timestamp", ""))
            seq = base + index
            room = _room_meta(message)
            lid = str(room.get("id") or f"{'ask' if ask else 'chat'}:{index}")
            content = str(getattr(message, "content", "") or "")
            key = (ts, seq, 0, 0)
            line: Optional[Dict[str, Any]] = None
            if kind in origin.APP_ROOM_KINDS and not ask:
                if kind == origin.APP_ROOM_OUT_KIND:
                    line = _line(lid, PERSON, APP_AGENT, content, ts,
                                 state=_delivery(scope, lid, acked))
                elif kind == origin.APP_ROOM_IN_KIND:
                    line = _line(lid, APP_AGENT, PERSON, content, ts)
                elif kind == origin.APP_TASK_KIND:
                    line = _line(lid, VAF_AGENT, APP_AGENT, content, ts)
                else:
                    line = _line(lid, APP_AGENT, VAF_AGENT, content, ts)
                turn = str(room.get("turn_id") or "")
                if turn and kind in (origin.APP_TASK_KIND, origin.APP_TASK_RESULT_KIND):
                    deferred.append((turn, seq, ts, len(deferred), line))
                    continue
            elif role == "user":
                if not content.strip():
                    continue
                if ask and content.startswith(CONFIRM_WAKE_PREFIX):
                    line = _line(lid, "", "", CONFIRMED_NOTE, ts, kind="note")
                elif ask or kind == origin.APP_AGENT_KIND:
                    line = _line(lid, APP_AGENT, VAF_AGENT, content, ts)
                else:
                    line = _line(lid, PERSON, VAF_AGENT, content, ts)
                    turn = str(_meta(message).get(TURN_ID_KEY) or "")
                    if turn:
                        turn_anchor[turn] = (ts, seq)
            elif role == "assistant" and not getattr(message, "tool_calls", None):
                readable = model_text(message)
                text = _THINK.sub("", readable or "").strip()
                if not text:
                    continue
                line = _line(lid, VAF_AGENT, APP_AGENT if ask else PERSON, text, ts)
            if line is not None:
                lines.append((key, line))
    for turn, seq, ts, order, line in deferred:
        anchor = turn_anchor.get(turn)
        key = (anchor[0], anchor[1], 1, order) if anchor else (ts, seq, 0, 0)
        lines.append((key, line))
    lines.sort(key=lambda item: item[0])
    return [line for _key, line in lines][-max(1, int(limit)):]


def unread(user_scope_id: Any, username: str = "") -> int:
    """Lines of the other two parties newer than what the account last saw in the room."""
    seen = devices.room_seen(user_scope_id)
    return sum(1 for line in transcript(user_scope_id, username)
               if line["peer"] != PERSON and float(line.get("ts") or 0) > seen)


def mark_seen(user_scope_id: Any, username: str = "") -> None:
    """The account looked at the room: everything shown so far counts as read."""
    lines = transcript(user_scope_id, username)
    if lines:
        devices.set_room_seen(user_scope_id, max(float(line.get("ts") or 0) for line in lines))


def list_row(user_scope_id: Any, username: str = "") -> Optional[Dict[str, Any]]:
    """The room's row for a chat list, or None while the account has no paired device. Its
    id names no session, so no session loader takes it."""
    scope = str(user_scope_id or "").strip()
    if not scope or not devices.for_scope(scope):
        return None
    if not username:
        username = str(devices.for_scope(scope)[0].get("username") or "")
    lines = transcript(scope, username)
    seen = devices.room_seen(scope)
    last = lines[-1] if lines else None
    return {"id": ROW_ID, "kind": ROW_KIND, "name": TITLE, "updated_at": "",
            "message_count": len(lines),
            "unread": sum(1 for line in lines
                          if line["peer"] != PERSON and float(line.get("ts") or 0) > seen),
            "app_room": state(scope),
            "last_ts": float(last["ts"]) if last else 0.0,
            "last": {"text": str(last["text"])[:160], "peer": last["peer"],
                     "mine": last["peer"] == PERSON, "ts": float(last["ts"])} if last else None}


# ── the person writes ─────────────────────────────────────────────────────────

def _changed(scope: str) -> None:
    """Tell the account's open views that the room changed (they fetch it again)."""
    try:
        from vaf.core.web_interface import get_web_interface
        get_web_interface().push_update_to_user(scope, {"type": "app_room_changed"})
    except Exception:
        logger.debug("app room: the change notice failed", exc_info=True)


def _room_msg(line_id: str, text: str, ts: float, to: str) -> Dict[str, Any]:
    return {"t": "room_msg", "id": line_id, "text": text, "ts": int(ts), "from": PERSON, "to": to}


def _deliver(scope: str, target: Dict[str, Any], line_id: str, text: str, ts: float,
             to: str) -> str:
    """The typed outbox entry for the target device, then the line now when it is connected;
    the offline hook (the relay's push notice) otherwise. ``sent`` or ``kept``."""
    from vaf.core.app_link import bridge
    did = target["device_id"]
    devices.outbox_publish(scope, [did], line_id, ts=ts, kind="room", to=to)
    if bridge._send(did, _room_msg(line_id, text, ts, to)):
        return "sent"
    bridge._went_offline(target)
    return "kept"


def post(user_scope_id: Any, username: str, text: Any) -> Dict[str, Any]:
    """A line the person writes in the room on VAF: to the agent in the Veyllo App, or, led
    by "@" and the name of the account's agent, to VAF's agent (``addressee``). The account is
    the caller's; nothing here reads an account or a device from the line. Raises
    ``RoomRefused`` and then stores and sends nothing. Answers ``{id, to, state, device}``."""
    from vaf.core.app_link import bridge
    scope = str(user_scope_id or "").strip()
    body = str(text or "").strip()
    if not scope:
        raise RoomRefused("no_device", 409)
    if not body or len(body) > pr.MAX_TEXT:
        raise RoomRefused("bad_text", 400)
    if not lane_on(scope):
        raise RoomRefused("channel_off", 409)
    standing = sorted(bridge.standing_devices(scope),
                      key=lambda r: -float(r.get("last_seen_at") or 0))
    if not standing:
        raise RoomRefused("no_device", 409)
    owner = str(standing[0].get("username") or username or "")
    to = addressee(body, owner)
    capable = capable_devices(scope)
    target = capable[0] if capable else None
    if to == APP_AGENT and target is None:
        raise RoomRefused("needs_newer_app", 409)
    if not bridge._rate_ok((target or standing[0])["device_id"]):
        raise RoomRefused("rate_limited", 429)
    line_id = str(uuid.uuid4())
    now = time.time()
    if to == APP_AGENT:
        _append(scope, body, role="user", kind=origin.APP_ROOM_OUT_KIND,
                meta={"id": line_id, "to": APP_AGENT, "device": str(target.get("model") or "")})
    else:
        # A turn of the person, queued exactly as a message from the app queues one, and
        # kept in the channel store as that message is (the app's copy replays from there).
        try:
            from vaf.core.channel_message_store import append_message
            append_message(username=owner, chat_id=devices.chat_key(scope), body=body,
                           direction="in", message_id=line_id, content_type="text",
                           channel=CHANNEL, user_scope_id=scope)
        except Exception:
            logger.debug("app room: storing the line failed", exc_info=True)
        from vaf.core.task_queue import TaskQueue
        TaskQueue().add(session_id=devices.chat_session_id(scope), input_text=body,
                        source=CHANNEL,
                        metadata={"user_scope_id": scope, "username": owner,
                                  "origin_channel": CHANNEL, "task_class": "interactive",
                                  "channel_message_id": line_id})
    state_now = "queued"
    if target is not None:
        state_now = _deliver(scope, target, line_id, body, now, to)
    _changed(scope)
    return {"id": line_id, "to": to, "state": state_now,
            "device": str(target.get("model") or "") if target else ""}


# ── the app writes, acknowledges, or does not know the room ──────────────────

def _stored(scope: str, line_id: str) -> bool:
    return _line_text(scope, line_id) is not None


def on_room_say(session, row: Dict[str, Any], message: Dict[str, Any]) -> None:
    """A line the app's agent wrote into the room: stored once in the app chat, acknowledged,
    and the account's views told. No turn of VAF's agent is queued, whatever the text says."""
    from vaf.core.app_link import bridge, settings
    did = row["device_id"]
    scope = str(row["user_scope_id"])
    mid = message["id"]
    if not devices.stands(did):
        return
    if bridge._seen_before(did, mid) or _stored(scope, mid):
        session.send_json({"t": "ack", "id": mid})
        return
    if not bridge._rate_ok(did):
        session.send_json({"t": "error", "code": "rate_limited", "ref": mid})
        return
    from vaf.core.channel_ingress_policy import evaluate_ingress
    from vaf.core.config import Config
    allowed, _reason = evaluate_ingress(CHANNEL, Config.get("channel_ingress_policy"),
                                        explicit_match=True)
    if not allowed or not settings().get("enabled", True):
        session.send_json({"t": "error", "code": "refused", "ref": mid})
        return
    meta: Dict[str, Any] = {"id": mid, "device": str(row.get("model") or "")}
    if message.get("reply_to"):
        meta["reply_to"] = message["reply_to"]
    if not _append(scope, message["text"].strip(), role="user",
                   kind=origin.APP_ROOM_IN_KIND, meta=meta):
        return
    bridge._remember(did, mid)
    session.send_json({"t": "ack", "id": mid})
    _changed(scope)


def on_ack(row: Dict[str, Any], line_id: str) -> None:
    """A device acknowledged a room line: it reads delivered from now on."""
    scope = str(row["user_scope_id"])
    devices.room_ack_record(scope, line_id)
    _changed(scope)


def app_lacks_room(row: Dict[str, Any]) -> None:
    """The device answered ``room_msg`` with ``unknown_type``: it is not taken for a room
    device again until a later ``hello`` or ``caps`` announces the room, and its waiting room
    lines are dropped."""
    did = row["device_id"]
    devices.drop_cap(did, CAP)
    devices.outbox_drop_kind(did, "room")
    _changed(str(row["user_scope_id"]))


def replay_message(row: Dict[str, Any], entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The ``room_msg`` of a waiting room entry, or None when its line is gone. Never for a
    device that does not announce the room (its stored row: the session's copy predates the
    caps of its ``hello``)."""
    row = devices.get(row["device_id"]) or row
    if CAP not in (row.get("caps") or []):
        return None
    scope = str(row["user_scope_id"])
    to = str(entry.get("to") or APP_AGENT)
    if to == APP_AGENT:
        text = _line_text(scope, entry["id"])
    else:
        from vaf.core import channel_message_store as store
        stored = store.get_message(str(row.get("username") or ""), devices.chat_key(scope),
                                   entry["id"], direction="in", user_scope_id=scope,
                                   channel=CHANNEL)
        text = str(stored.get("body") or "") if stored else None
    if not text:
        return None
    return _room_msg(entry["id"], text, float(entry.get("ts") or time.time()), to)


# ── the main agent hands the app a task (user_phone) ──────────────────────────

def record_task(user_scope_id: Any, task_id: str, instruction: str,
                device_row: Optional[Dict[str, Any]], turn_id: Optional[str] = None) -> None:
    """The task VAF's agent just sent to the app, as a room line (whatever chat called the
    tool: the room is its record). Never raises."""
    scope = str(user_scope_id or "").strip()
    if not scope:
        return
    try:
        meta = {"id": str(task_id), "to": APP_AGENT,
                "device": str((device_row or {}).get("model") or "")}
        if turn_id:
            meta["turn_id"] = str(turn_id)
        _append(scope, str(instruction or "").strip() or "-", role="assistant",
                kind=origin.APP_TASK_KIND, meta=meta)
        _changed(scope)
    except Exception:
        logger.debug("app room: recording a task failed", exc_info=True)


def record_task_result(user_scope_id: Any, task_id: str, text: str,
                       turn_id: Optional[str] = None) -> None:
    """How the task ended (the app's answer, a refusal, a timeout, a cancel, a drop), as a
    room line after the task. Never raises."""
    scope = str(user_scope_id or "").strip()
    if not scope:
        return
    try:
        meta = {"id": str(uuid.uuid4()), "to": VAF_AGENT, "reply_to": str(task_id)}
        if turn_id:
            meta["turn_id"] = str(turn_id)
        _append(scope, str(text or "").strip() or "-", role="assistant",
                kind=origin.APP_TASK_RESULT_KIND, meta=meta)
        _changed(scope)
    except Exception:
        logger.debug("app room: recording a task result failed", exc_info=True)


# ── the view ──────────────────────────────────────────────────────────────────

def view(user_scope_id: Any, username: str = "") -> Dict[str, Any]:
    """The room as a room view reads it (the shape of an agent room's ``room_transcript``, with
    ``roomKind`` ``veyllo_app``), built for this account only: the three parties, VAF's agent
    as the account's own agent, the room's state, and the projection. No task board, votes,
    invitations or read positions: none of them exists in this room."""
    scope = str(user_scope_id or "").strip()
    rows = devices.for_scope(scope) if scope else []
    if not username and rows:
        username = str(rows[0].get("username") or "")
    labels = {PERSON: "You", VAF_AGENT: agent_label(username), APP_AGENT: TITLE}
    busy = False
    try:
        from vaf.core.task_queue import TaskQueue
        queue = TaskQueue()
        busy = any(queue.is_busy_for_session(sid) for sid in
                   (devices.chat_session_id(scope), devices.ask_session_id(scope))) \
            if rows else False
    except Exception:
        busy = False
    room_state = state(scope)
    return {
        "room": {
            "id": ROW_ID, "roomId": ROW_ID, "roomKind": ROOM_KIND, "title": TITLE,
            "me": PERSON, "agentPeer": VAF_AGENT, "closed": False, "members": 3,
            "members_list": [
                {"peer": PERSON, "label": labels[PERSON], "role": "", "kind": "human"},
                {"peer": VAF_AGENT, "label": labels[VAF_AGENT], "role": "", "kind": "agent"},
                {"peer": APP_AGENT, "label": labels[APP_AGENT], "role": "", "kind": "agent"},
            ],
            "typing": [{"peer": VAF_AGENT, "label": labels[VAF_AGENT], "kind": "turn"}]
            if busy else [],
            "appRoom": {"devices": room_state["devices"],
                        "connected": room_state["connected"],
                        "canWrite": room_state["can_write"], "reason": room_state["reason"],
                        "agentName": labels[VAF_AGENT],
                        # The two sessions behind the room, for the drafts that wait in
                        # them (the person's chat and the app agent's requests).
                        "sessions": [devices.chat_session_id(scope),
                                     devices.ask_session_id(scope)] if rows else []},
            "canManage": False, "canInvite": False,
        },
        "messages": transcript(scope, username) if rows else [],
    }
