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

What a device may write into the room is bounded twice: the device's 30 messages per minute
(shared with its other messages) and ``ROOM_SAY_BYTES_PER_HOUR`` of ``room_say`` text per
account. Every line loads and saves the app chat and makes the account's open screens fetch the
room again, so the byte bound is what keeps a paired device from filling the chat file (65536
characters times 30 a minute is about 115 MB an hour).

The row of a chat list (``list_row``) is read on every listing, so the projection is cached per
account and agent name, keyed on the two session files (modification time and size), the device
list's generation and the turns of the room's sessions that run or wait. The full projection is
built again only when one of them changed.

NAMED BOUNDARY, first-party like the rest of the link (app_link/__init__.py): no facade export.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

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
#: How much ``room_say`` text one account takes per hour. A device at its full rate of 30
#: messages a minute with ordinary chat lines (up to about 1 KB each) stays below it; a device
#: that sends 65536-character lines is stopped after about 32 of them in an hour.
ROOM_SAY_BYTES_PER_HOUR = 2 * 1024 * 1024
#: Codes of how a task VAF's agent handed the app ended, for a view that words them itself.
TASK_RESULT_CODES = ("answered", "declined", "unavailable", "timeout", "failed", "cancelled",
                     "disconnected", "file_refused")

_lock = threading.Lock()
_say_bytes: Dict[str, Deque[Tuple[float, int]]] = {}
# (scope, username) -> (key, the last TRANSCRIPT_LIMIT lines, {"count", "unread", "last"})
_cache: Dict[Tuple[str, str], Tuple[Any, List[Dict[str, Any]], Dict[str, Any]]] = {}


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


def handle(username: str) -> str:
    """The one word after "@" that always reaches VAF's agent: the first run of letters and
    digits (of any script) of its name ("Mia Lisa" -> "Mia", "Dr. Mia" -> "Dr", "Mia." ->
    "Mia"), or "VAF"."""
    import re
    found = re.search(r"[^\W_]+", agent_label(username))
    return found.group(0) if found else "VAF"


def addressee(text: Any, username: str) -> str:
    """Who a line the person writes on VAF is for: VAF's agent when it starts with "@" and the
    ``handle`` of that agent's name (a name of several words written out begins with it too)
    or "VAF", case-insensitive, followed by the end of the line or anything that is not a
    letter or digit ("@Miami" does not name Mia, "@Mia." and "@Mia：" do); the agent in the
    Veyllo App otherwise: the agent rooms' leading-mention rule for a known name
    (a2a.room.mentions_at_start)."""
    from vaf.core.a2a.room import mentions_at_start
    if mentions_at_start(text, handle(username)) or mentions_at_start(text, "VAF"):
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


def _no_capable_reason(standing: List[Dict[str, Any]]) -> str:
    """Why no device takes a room line although devices stand: one that has not said what it
    supports yet (paired, never connected since) is not taken for an older app."""
    if any(not r.get("caps_known") for r in standing):
        return "not_connected_yet"
    return "needs_newer_app"


def state(user_scope_id: Any) -> Dict[str, Any]:
    """The room's state for its row and its view: how many devices are paired, whether one is
    connected, whether a line to the app's agent can be written and, when not, why
    (``channel_off``, ``no_device``, ``not_connected_yet``, ``needs_newer_app``)."""
    from vaf.core.app_link import bridge
    scope = str(user_scope_id or "").strip()
    paired = devices.for_scope(scope) if scope else []
    standing = bridge.standing_devices(scope) if scope else []
    reason = ""
    if not lane_on(scope):
        reason = "channel_off"
    elif not standing:
        reason = "no_device"
    elif not capable_devices(scope):
        reason = _no_capable_reason(standing)
    return {"devices": len(paired), "connected": bool(bridge.connected_devices(scope)),
            "can_write": not reason, "reason": reason}


# ── the record: two sessions, one projection ──────────────────────────────────

def _manager():
    from vaf.core.session import get_manager
    return get_manager()


def _load(session_id: str):
    try:
        return _manager().load(session_id, restore_state=False, repoint=False)
    except (FileNotFoundError, ValueError):
        return None
    except Exception:
        logger.debug("app room: %s cannot be read", session_id, exc_info=True)
        return None


def _file_signature(session_id: str) -> Tuple:
    """The stored session's files as (name, modification time, size), for the cache key."""
    from vaf.core.session import session_file_paths
    out = []
    try:
        for path in session_file_paths(_manager().storage_dir, session_id):
            try:
                st = path.stat()
            except OSError:
                continue
            out.append((path.name, st.st_mtime_ns, st.st_size))
    except Exception:
        return (time.time(),)
    return tuple(out)


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
    """One room line into the account's app chat (created when it has none yet), through the
    background writer and its per-session lock, which the runner's save of a turn holds too."""
    sid = devices.chat_session_id(scope)
    saved = _manager().append_background_message(
        sid, content, role=role, kind=kind, create=True, name=TITLE, user_scope_id=scope,
        metadata={"app_room": meta})
    return saved is not None


def _line_text(scope: str, line_id: str, kinds=origin.APP_ROOM_KINDS) -> Optional[str]:
    """The text of the room line with this id and one of ``kinds`` in the app chat, or None."""
    session = _load(devices.chat_session_id(scope))
    for message in reversed(getattr(session, "messages", None) or []):
        if getattr(message, "kind", None) in kinds \
                and str(_room_meta(message).get("id") or "") == str(line_id):
            return str(message.content or "")
    return None


def _delivery(scope: str, line_id: str, acked: Dict[str, float], room: Dict[str, Any],
              ts: float, now: float) -> Tuple[str, str]:
    """(state, reason) of a line to the app's agent: ``delivered``; ``waiting`` while a device's
    queue holds it; ``not_delivered`` with why: ``removed`` (its device is no longer paired),
    ``lane_off`` (the Veyllo App is off for the account), ``expired`` (older than the queue
    keeps anything) or ``dropped`` (the app turned out not to take room lines)."""
    if line_id in acked:
        return "delivered", ""
    if devices.held_in_outbox(scope, line_id):
        return "waiting", ""
    device_id = str(room.get("device_id") or "")
    if device_id and devices.get(device_id) is None:
        return "not_delivered", "removed"
    if not lane_on(scope):
        return "not_delivered", "lane_off"
    if now - ts > devices.OUTBOX_MAX_AGE_SECONDS:
        return "not_delivered", "expired"
    return "not_delivered", "dropped"


def _sessions(scope: str) -> Tuple[str, str]:
    """The room's two sessions: the account's app chat and the app agent's request session."""
    chat = f"{CHANNEL}_{devices.chat_key(scope)}"
    return chat, chat + devices.ASK_SESSION_SUFFIX


def _pending_turns(sids: Tuple[str, str]) -> List[Tuple[str, Any]]:
    """The turns of the room's two sessions that run or wait, oldest first, as (session, task):
    the person's question and the app agent's request before the runner stores them."""
    try:
        from vaf.core.task_queue import TaskQueue
        queue = TaskQueue()
    except Exception:
        return []
    out = []
    for sid in sids:
        try:
            out += [(sid, task) for task in queue.pending_for_session(sid)]
        except Exception:
            continue
    return out


#: The note a wake leaves in the room, by its kind (task_queue.WAKE_KINDS), in English for the
#: terminal; the web words it by ``code``.
WAKE_NOTES = {"timer": "A timer fired.", "process": "A background command finished.",
              "draft": "A draft was sent.", "confirm": CONFIRMED_NOTE}


def _legacy_wake_prefixes() -> Tuple[Tuple[str, str], ...]:
    from vaf.core.app_link.confirmations import CONFIRM_WAKE_PREFIX
    from vaf.core.outbound_hold import DRAFT_WAKE_PREFIX
    return (("timer", "⏰ Timer fired"), ("process", "⚙ Background command finished"),
            ("draft", DRAFT_WAKE_PREFIX), ("confirm", CONFIRM_WAKE_PREFIX))


def _stored_wake(kind: Any, content: str) -> Optional[str]:
    """The wake kind of a stored user line: its stored kind, or, for a line stored before
    wakes carried one and only when it has no kind, how its text begins (the web chat's rule
    for the same lines)."""
    if kind in WAKE_NOTES:
        return str(kind)
    if kind is None:
        for wake, prefix in _legacy_wake_prefixes():
            if content.startswith(prefix):
                return wake
    return None


def _pending_author(sid: str, sids: Tuple[str, str], task: Any) -> Optional[str]:
    """Who a turn that runs or waits is shown as: ``person`` for a line the person wrote (in
    the app chat, from the Veyllo App lane, carrying the message id that the bridge's message
    handler and ``post`` set), ``app_agent`` for a request of the app's agent (its request
    session, ``app_agent_ask``), a wake kind (a note), or None for anything else (the
    compaction, a system command, a turn some other lane queued), which the room does not
    show."""
    from vaf.core.task_queue import is_system_command, wake_kind
    meta = getattr(task, "metadata", None) or {}
    if is_system_command(task) or meta.get("compaction"):
        return None
    wake = wake_kind(meta)
    if wake:
        return wake if wake in WAKE_NOTES else None
    if getattr(task, "source", None) != CHANNEL:
        return None
    if sid == sids[1]:
        return APP_AGENT if meta.get("app_agent_ask") else None
    if meta.get("app_agent_ask"):
        return None
    if meta.get("channel_message_id") or meta.get("app_message_id"):
        return PERSON
    return None


def _forgotten(session_id: str) -> Dict[str, str]:
    """The credentials forgotten in this chat (forget_secrets.session_env), or {}."""
    try:
        from vaf.core.forget_secrets import session_env
        return dict(session_env(session_id))
    except Exception:
        return {}


def _cache_key(scope: str, username: str, sids: Tuple[str, str],
               pending: List[Tuple[str, Any]]) -> Tuple:
    return (agent_label(username), _file_signature(sids[0]), _file_signature(sids[1]),
            devices.generation(), lane_on(scope),
            tuple(str(getattr(t, "turn_id", "")) for _sid, t in pending),
            # A credential forgotten in the chat changes what a waiting question shows.
            hash(tuple(sorted(_forgotten(sids[0]).items()))))


def _projection(scope: str, username: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """(the last TRANSCRIPT_LIMIT lines, {"count", "unread", "last"}), from the cache while
    nothing behind it changed."""
    sids = _sessions(scope)
    pending = _pending_turns(sids)
    key = _cache_key(scope, username, sids, pending)
    with _lock:
        hit = _cache.get((scope, username))
    if hit is not None and hit[0] == key:
        return hit[1], hit[2]
    lines = _build(scope, username, sids, pending)
    last = lines[-1] if lines else None
    summary = {"count": len(lines), "unread": _unread(lines, devices.room_seen(scope)),
               "last": dict(last) if last else None}
    tail = lines[-TRANSCRIPT_LIMIT:]
    with _lock:
        _cache[(scope, username)] = (key, tail, summary)
    return tail, summary


def transcript(user_scope_id: Any, username: str = "",
               limit: int = TRANSCRIPT_LIMIT) -> List[Dict[str, Any]]:
    """The room as its view shows it: who wrote what to whom, in time order, over the app chat
    and the request session of the account. Each line: ``id``, ``peer`` and ``label`` (the
    speaker), ``to`` (``{"peer": ...}``, the addressee), ``kind`` (``say``, or ``note`` for a
    line nobody said), ``text``, ``ts`` (Unix seconds) and, as they apply: ``state`` and
    ``reason`` for a line to the app's agent, ``device`` (the device a line went to), ``code``
    (how a task ended, or ``turn_failed`` for an answer that failed) and ``pending`` (a question
    whose turn runs or waits; the runner stores it with the answer). Tool and system messages
    and empty answers are not shown. A task VAF's agent handed the app during a turn is placed
    right after the person's message of that turn (both carry the turn's id), so a question
    stands above the task it caused, and a stored question keeps the time it was asked."""
    scope = str(user_scope_id or "").strip()
    if not scope:
        return []
    tail, _summary = _projection(scope, username)
    return [dict(line) for line in tail[-max(1, min(int(limit), TRANSCRIPT_LIMIT)):]]


def _build(scope: str, username: str, sids: Tuple[str, str],
           pending: List[Tuple[str, Any]]) -> List[Dict[str, Any]]:
    from vaf.core.completion import strip_think_blocks
    from vaf.core.session import ASKED_AT_KEY, TURN_ID_KEY, is_failed_turn, model_text
    labels = {PERSON: "You", VAF_AGENT: agent_label(username), APP_AGENT: TITLE}
    acked = devices.room_acked(scope)
    now = time.time()
    lines: List[Tuple[Tuple, Dict[str, Any]]] = []
    turn_anchor: Dict[str, Tuple[float, int]] = {}
    deferred: List[Tuple[str, int, float, int, Dict[str, Any]]] = []
    stored_turns = set()

    def _note(lid: str, wake: str, ts: float) -> Dict[str, Any]:
        return _line(lid, "", "", WAKE_NOTES[wake], ts, kind="note", code=wake)

    def _line(lid: str, peer: str, to: str, text: str, ts: float, kind: str = "say",
              **extra) -> Dict[str, Any]:
        out = {"id": lid, "peer": peer, "label": labels.get(peer, ""), "role": "",
               "to": {"peer": to} if to else {"room": True},
               "toLabel": labels.get(to, "") if to else "", "kind": kind,
               "text": text, "ts": ts}
        out.update({k: v for k, v in extra.items() if v not in (None, "")})
        return out

    for base, sid in ((0, sids[0]), (1_000_000, sids[1])):
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
            line: Optional[Dict[str, Any]] = None
            if kind in origin.APP_ROOM_KINDS and not ask:
                if kind == origin.APP_ROOM_OUT_KIND:
                    state_now, reason = _delivery(scope, lid, acked, room, ts, now)
                    line = _line(lid, PERSON, APP_AGENT, content, ts, state=state_now,
                                 reason=reason, device=str(room.get("device") or ""),
                                 resend_of=str(room.get("resend_of") or ""))
                elif kind == origin.APP_ROOM_IN_KIND:
                    line = _line(lid, APP_AGENT, PERSON, content, ts)
                elif kind == origin.APP_TASK_KIND:
                    line = _line(lid, VAF_AGENT, APP_AGENT, content, ts,
                                 device=str(room.get("device") or ""))
                else:
                    line = _line(lid, APP_AGENT, VAF_AGENT, content, ts,
                                 code=str(room.get("code") or ""),
                                 seconds=room.get("seconds"))
                turn = str(room.get("turn_id") or "")
                if turn and kind in (origin.APP_TASK_KIND, origin.APP_TASK_RESULT_KIND):
                    deferred.append((turn, seq, ts, len(deferred), line))
                    continue
            elif role == "user":
                if not content.strip():
                    continue
                meta = _meta(message)
                turn = str(meta.get(TURN_ID_KEY) or "")
                asked = meta.get(ASKED_AT_KEY)
                if isinstance(asked, (int, float)) and asked > 0:
                    ts = float(asked)
                if turn:
                    stored_turns.add(turn)
                # A wake (a timer, a background command, a draft sent, a confirmation) is
                # nobody's words: a neutral note, by its stored kind, or for a line stored
                # before the kind by how its text begins and only without a kind. A request of
                # the app's agent is that agent's text, whatever it begins with.
                wake = _stored_wake(kind, content)
                if wake:
                    line = _note(lid, wake, ts)
                elif ask or kind == origin.APP_AGENT_KIND:
                    line = _line(lid, APP_AGENT, VAF_AGENT, content, ts)
                else:
                    line = _line(lid, PERSON, VAF_AGENT, content, ts)
                    if turn:
                        turn_anchor[turn] = (ts, seq)
            elif role == "assistant" and not getattr(message, "tool_calls", None):
                to = APP_AGENT if ask else PERSON
                if is_failed_turn(message):
                    # The answer failed: the room says so instead of showing nothing after
                    # the question.
                    line = _line(lid, VAF_AGENT, to, "", ts, code="turn_failed")
                else:
                    text = strip_think_blocks(model_text(message) or "").strip()
                    if not text:
                        continue
                    line = _line(lid, VAF_AGENT, to, text, ts)
            if line is not None:
                lines.append(((ts, seq, 0, 0), line))
    # The questions and requests whose turn runs or waits: the runner stores them with the
    # answer, so until then they come from the queue, at the time they were asked.
    # Only a turn a person or the app's agent started is shown as their words, by the marks
    # the lanes that start them set (`_pending_author`); a wake is a neutral note, anything
    # else (the compaction, a system command) is not shown. Its text is shown with the
    # chat's forgotten credentials replaced, like every other copy of the chat.
    from vaf.core.forget_secrets import scrub_text
    forgotten = _forgotten(sids[0])
    for order, (sid, task) in enumerate(pending):
        turn = str(getattr(task, "turn_id", "") or "")
        text = str(getattr(task, "input_text", "") or "").strip()
        if not turn or turn in stored_turns or not text:
            continue
        author = _pending_author(sid, sids, task)
        if author is None:
            continue
        ts = float(getattr(task, "created_at", 0) or 0)
        seq = 2_000_000 + order
        if author in WAKE_NOTES:
            line = _note(f"turn:{turn}", author, ts)
        elif author == APP_AGENT:
            line = _line(f"turn:{turn}", APP_AGENT, VAF_AGENT, scrub_text(text, forgotten), ts,
                         pending=True)
        else:
            line = _line(f"turn:{turn}", PERSON, VAF_AGENT, scrub_text(text, forgotten), ts,
                         pending=True)
            turn_anchor.setdefault(turn, (ts, seq))
        lines.append(((ts, seq, 0, 0), line))
    for turn, seq, ts, order, line in deferred:
        anchor = turn_anchor.get(turn)
        key = (anchor[0], anchor[1], 1, order) if anchor else (ts, seq, 0, 0)
        lines.append((key, line))
    lines.sort(key=lambda item: item[0])
    # A line the person sent again offers no second resend.
    resent = {str(line.get("resend_of")) for _key, line in lines if line.get("resend_of")}
    for _key, line in lines:
        if line["id"] in resent:
            line["resent"] = True
    return [line for _key, line in lines]


def _unread(lines: List[Dict[str, Any]], seen: float) -> int:
    """Lines of the other two parties newer than what the account last saw in the room."""
    return sum(1 for line in lines
               if line["peer"] not in (PERSON, "") and float(line.get("ts") or 0) > seen)


def mark_seen(user_scope_id: Any, upto: Optional[float] = None, username: str = "") -> bool:
    """The account looked at the room up to ``upto`` (the newest line of what it was shown;
    without it, of the projection now). Never moves back. Whether it moved."""
    if upto is None:
        lines = transcript(user_scope_id, username)
        upto = max((float(line.get("ts") or 0) for line in lines), default=0.0)
    if not upto or float(upto) <= devices.room_seen(user_scope_id):
        return False
    devices.set_room_seen(user_scope_id, float(upto))
    return True


def list_row(user_scope_id: Any, username: str = "") -> Optional[Dict[str, Any]]:
    """The room's row for a chat list, or None while the account has no paired device. Its
    id names no session, so no session loader takes it."""
    scope = str(user_scope_id or "").strip()
    rows = devices.for_scope(scope) if scope else []
    if not rows:
        return None
    if not username:
        username = str(rows[0].get("username") or "")
    _tail, summary = _projection(scope, username)
    last = summary["last"]
    return {"id": ROW_ID, "kind": ROW_KIND, "name": TITLE, "updated_at": "",
            "message_count": summary["count"], "unread": summary["unread"],
            "app_room": state(scope),
            "last_ts": float(last["ts"]) if last else 0.0,
            "last": {"text": str(last["text"])[:160], "peer": last["peer"],
                     "mine": last["peer"] == PERSON, "ts": float(last["ts"])} if last else None}


# ── the person writes ─────────────────────────────────────────────────────────

def changed(user_scope_id: Any) -> None:
    """Tell the account's open screens that the room changed (a line, a delivery, a device
    paired or removed); they fetch the list and an open room again."""
    scope = str(user_scope_id or "").strip()
    if not scope:
        return
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


def post(user_scope_id: Any, username: str, text: Any,
         resend_of: Optional[str] = None) -> Dict[str, Any]:
    """A line the person writes in the room on VAF: to the agent in the Veyllo App, or, led
    by "@" and the name of the account's agent, to VAF's agent (``addressee``). The account is
    the caller's; nothing here reads an account or a device from the line. Raises
    ``RoomRefused`` and then stores and sends nothing. Answers ``{id, to, state, device}``.
    ``resend_of`` names an earlier line of the person to the app's agent that this one sends
    again; that line then stops offering a resend (an id that is no such line is ignored)."""
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
        raise RoomRefused(_no_capable_reason(standing), 409)
    if not bridge._rate_ok((target or standing[0])["device_id"]):
        raise RoomRefused("rate_limited", 429)
    line_id = str(uuid.uuid4())
    now = time.time()
    if to == APP_AGENT:
        meta = {"id": line_id, "to": APP_AGENT, "device": str(target.get("model") or ""),
                "device_id": target["device_id"]}
        if resend_of and _line_text(scope, str(resend_of),
                                    kinds=(origin.APP_ROOM_OUT_KIND,)) is not None:
            meta["resend_of"] = str(resend_of)
        _append(scope, body, role="user", kind=origin.APP_ROOM_OUT_KIND, meta=meta)
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
    changed(scope)
    return {"id": line_id, "to": to, "state": state_now,
            "device": str(target.get("model") or "") if target else ""}


# ── the app writes, acknowledges, or does not know the room ──────────────────

def _stored_incoming(scope: str, line_id: str) -> bool:
    """Whether a line of the app's agent with this id is stored already. Only those: an id
    that names one of the person's own lines is not a repeat of the app's."""
    return _line_text(scope, line_id, kinds=(origin.APP_ROOM_IN_KIND,)) is not None


def _say_budget_ok(scope: str, size: int, now: Optional[float] = None) -> bool:
    """Take ``size`` bytes of the account's ``ROOM_SAY_BYTES_PER_HOUR``, or False when they do
    not fit the last hour (nothing is taken then)."""
    now = time.time() if now is None else now
    with _lock:
        spent = _say_bytes.setdefault(scope, deque())
        while spent and now - spent[0][0] > 3600:
            spent.popleft()
        if sum(n for _t, n in spent) + size > ROOM_SAY_BYTES_PER_HOUR:
            return False
        spent.append((now, size))
        return True


def _say_refund(scope: str, size: int) -> None:
    """Give back the newest charge of ``size`` bytes (a line that was not stored)."""
    with _lock:
        spent = _say_bytes.get(scope)
        if not spent:
            return
        for index in range(len(spent) - 1, -1, -1):
            if spent[index][1] == size:
                del spent[index]
                return


def on_room_say(session, row: Dict[str, Any], message: Dict[str, Any]) -> None:
    """A line the app's agent wrote into the room: stored once in the app chat, acknowledged,
    and the account's views told. No turn of VAF's agent is queued, whatever the text says."""
    from vaf.core.app_link import bridge, settings
    did = row["device_id"]
    scope = str(row["user_scope_id"])
    mid = message["id"]
    if not devices.stands(did):
        return
    if bridge._seen_before(did, mid) or _stored_incoming(scope, mid):
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
    text = message["text"].strip()
    if not _say_budget_ok(scope, len(text.encode("utf-8"))):
        session.send_json({"t": "error", "code": "rate_limited", "ref": mid})
        return
    meta: Dict[str, Any] = {"id": mid, "device": str(row.get("model") or "")}
    if message.get("reply_to"):
        meta["reply_to"] = message["reply_to"]
    if not _append(scope, text, role="user",
                   kind=origin.APP_ROOM_IN_KIND, meta=meta):
        # Not stored: the bytes go back to the hour's budget, so a retry is not charged twice.
        _say_refund(scope, len(text.encode("utf-8")))
        return
    bridge._remember(did, mid)
    session.send_json({"t": "ack", "id": mid})
    changed(scope)


def on_ack(row: Dict[str, Any], line_id: str) -> None:
    """A device acknowledged a room line: it reads delivered from now on."""
    scope = str(row["user_scope_id"])
    devices.room_ack_record(scope, line_id)
    changed(scope)


def app_lacks_room(row: Dict[str, Any]) -> None:
    """The device answered ``room_msg`` with ``unknown_type``: it is not taken for a room
    device again until a later ``hello`` or ``caps`` announces the room, and its waiting room
    lines are dropped."""
    did = row["device_id"]
    devices.drop_cap(did, CAP)
    devices.outbox_drop_kind(did, "room")
    changed(str(row["user_scope_id"]))


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
        changed(scope)
    except Exception:
        logger.debug("app room: recording a task failed", exc_info=True)


def record_task_result(user_scope_id: Any, task_id: str, text: str,
                       turn_id: Optional[str] = None, *, code: str = "answered",
                       seconds: Optional[int] = None) -> None:
    """How the task ended, as a room line after the task: ``text`` (the app's answer, or the
    sentence of how it ended) and ``code`` (`TASK_RESULT_CODES`; with ``seconds`` for a
    timeout), so a view can word an ending in its own language. Never raises."""
    scope = str(user_scope_id or "").strip()
    if not scope:
        return
    try:
        meta: Dict[str, Any] = {"id": str(uuid.uuid4()), "to": VAF_AGENT,
                                "reply_to": str(task_id),
                                "code": code if code in TASK_RESULT_CODES else "failed"}
        if seconds is not None:
            meta["seconds"] = int(seconds)
        if turn_id:
            meta["turn_id"] = str(turn_id)
        _append(scope, str(text or "").strip() or "-", role="assistant",
                kind=origin.APP_TASK_RESULT_KIND, meta=meta)
        changed(scope)
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
                        # The one word after "@" that always reaches VAF's agent.
                        "handle": handle(username),
                        # The two sessions behind the room, for the drafts that wait in
                        # them (the person's chat and the app agent's requests).
                        "sessions": [devices.chat_session_id(scope),
                                     devices.ask_session_id(scope)] if rows else []},
            "canManage": False, "canInvite": False,
        },
        "messages": transcript(scope, username) if rows else [],
    }
