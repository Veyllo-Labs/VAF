# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What a chat is doing right now, for a browser that was not watching it.

A browser socket is subscribed to ONE chat, and the live stream of a chat goes only to its
subscribers (`WebInterfaceManager.broadcast_to_session`). A person who switches from chat A
to chat B therefore receives nothing about A any more: the reply A streams, the tool cards
it starts, the coder window it fills and the files it announces all go to nobody. The stored
transcript does not have them either, because the runner writes a turn after it ENDS. Coming
back to A showed the transcript of the last finished turn and nothing of the running one
until the next live event happened to arrive.

`LiveViewLedger` is the missing answer to "what is this chat doing right now". The web
interface records every session-scoped event where it is delivered, BEFORE it is sent, and
`load_session` sends the record back as one `live_replay` frame right after the transcript.
The record is coalesced, not a log: a streamed round keeps its last text, a tool its start
and its end, a worker view its last state, so a replay is a picture of the moment and not a
re-run of the whole stream.

Rules, each against its failure mode:

- Groups. The TURN group (streamed text, tool cards, created files and the end of the running
  turn) lives until the turn LEAVES THE QUEUE (`turn_left_queue`, called by the runner right
  before `task_done`), not until its `message_complete`: the runner sends `message_complete`
  first and saves the chat afterwards, so a turn dropped at `message_complete` would be in
  neither the record nor the stored transcript for that window. Its `message_complete` is
  kept and replayed, so a turn that ended but is not stored yet is drawn as ended.
  `snapshot(active=False)` never returns the group: when the queue says no turn runs, nothing
  of a turn is replayed, so a turn that ended while nobody watched can never come back as a
  spinner. A turn event with another turn id drops what a previous turn left behind (a turn
  that died without leaving the queue cleanly). The WORKER group (sub-agent window, coder and
  other `*_state` views, workflow steps) survives the turn, because an async worker outlives
  the turn that started it; it expires on its own, and a last value moves to the end on each
  write, so the replay plays the worker's states in the order they happened. The PREVIEW
  group is the place of an Office document's preview in the converter queue.
- Prompts. The person's message of each queued or running web turn, by turn id (the runner
  stores it with the answer). At most MAX_PROMPTS per chat; they count toward the chat's entry
  and byte limits and are the last thing dropped.
- Stored as sent. An event is copied (strings capped), never rebuilt field by field, so a
  field a producer adds later reaches the replay without a change here.
- `*_state` without registration. Every type ending in `_state` is kept as its last value
  (merged shallowly, like the page merges it), so the view of an embedder's own agent that
  emits through `emit_agent_state` is not blind after a switch either.
- Bounded: 64 KB per string (content nested deeper than MAX_DEPTH is replaced), 600 entries
  and 2 MB per chat, 32 chats. Eviction goes by rank, oldest first within a rank: output lines,
  tool results and frames first; streamed text, files and step updates next; worker states,
  the turn's end and the preview place after that; a tool that started and has not ended
  last of the entries, then the prompts. The limits always hold.
- Forgotten credentials. A value the agent stored from the chat (`vaf/core/forget_secrets.py`)
  is replaced in every entry and prompt when it is forgotten (the web interface's forget
  listener calls `scrub`) and in every event recorded afterwards, so a replay never shows what
  the transcript no longer shows.
- Isolation. Keyed by session id, never read for prompt content, and stamped with the chat's
  owning account when the record is created. The only readers are the server's `load_session`
  AFTER its ownership gate and the connect-time auto-load of the connection's own newest
  chat, and both pass the chat's current owner: a record whose owner differs (a deleted chat's
  id claimed by another account while a late event re-created the record) is dropped, not
  returned.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Hashable, List, Optional

MAX_SESSIONS = 32
MAX_ENTRIES = 600
MAX_BYTES = 2 * 1024 * 1024
MAX_STRING = 64 * 1024
MAX_FRAME = int(1.5 * 1024 * 1024)
MAX_PROMPTS = 20
MAX_DEPTH = 12
TRUNCATED = " [truncated]"
TOO_DEEP = "[nested too deep]"

# Seconds.
ENTRY_TTL = 30 * 60          # any entry, after its last write
WORKER_DONE_TTL = 10 * 60    # a worker view after the worker said idle/error, a finished workflow
PREVIEW_TTL = 10 * 60        # a preview place without an update
TURN_TTL = 6 * 60 * 60       # a running tool and the prompts: only a safety net for memory, the
                             # queue's active state and turn_left_queue decide what is shown
SWEEP_EVERY = 30.0           # how often one record sweeps the other chats

RING_SIZES = {
    "subagent_output_stream": 200,
    "browser_step_update": 200,
    "workflow_output_stream": 200,
    "subagent_output": 20,
}

TURN, WORKER, PREVIEW = "turn", "worker", "preview"
_TURN_EVENTS = frozenset({"agent_message_update", "tool_update", "file_created",
                          "clear_last_assistant"})
_WORKFLOW = frozenset({"workflow_start", "workflow_update", "workflow_done", "workflow_output_stream"})

# Eviction ranks: the lowest goes first.
R_LINES, R_TURN, R_STATE, R_RUNNING = 0, 1, 2, 3


def _cap(value: Any, limit: int = MAX_STRING, _depth: int = 0) -> Any:
    """A copy of `value` with every string longer than `limit` cut and marked, and content
    nested deeper than MAX_DEPTH replaced. Containers are copied, so a producer that mutates
    its dict after sending cannot change the record."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + TRUNCATED
    if isinstance(value, (dict, list, tuple)) and _depth >= MAX_DEPTH:
        return TOO_DEEP
    if isinstance(value, dict):
        return {k: _cap(v, limit, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_cap(v, limit, _depth + 1) for v in value]
    return value


def _size(value: Any) -> int:
    """A cheap estimate of the serialised size, close enough for a memory bound. `_cap` has
    already bounded the depth."""
    if isinstance(value, str):
        return len(value) + 2
    if isinstance(value, dict):
        return 2 + sum(len(str(k)) + 4 + _size(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return 2 + sum(_size(v) + 1 for v in value)
    return 8


def _forgotten(session_id: str) -> Dict[str, str]:
    """The credentials forgotten in this chat, or {}. Never raises."""
    try:
        from vaf.core.forget_secrets import session_env
        return session_env(session_id)
    except Exception:
        return {}


def _scrub(obj: Any, env: Dict[str, str]) -> Any:
    if not env:
        return obj
    from vaf.core.forget_secrets import scrub_obj
    return scrub_obj(obj, env)


class _Entry:
    __slots__ = ("group", "event", "size", "written", "expires", "protected", "rank")

    def __init__(self, group: str, event: dict, now: float, ttl: float, rank: int,
                 protected: bool = False):
        self.group = group
        self.event = event
        self.size = _size(event)
        self.written = now
        self.expires = now + ttl
        self.rank = rank
        self.protected = protected


class _Chat:
    """The record of one chat: entries in first-seen order, the prompts of its open turns."""

    def __init__(self, owner: Optional[str]) -> None:
        self.owner = owner
        self.entries: "OrderedDict[Hashable, _Entry]" = OrderedDict()
        self.prompts: "OrderedDict[str, dict]" = OrderedDict()
        self.bytes = 0
        self.seq = 0
        self.turn_id: Optional[str] = None
        # The untagged stream (no `round`): a new bubble begins after any other turn event,
        # the way the page starts a new bubble after a tool card.
        self.untagged_seg = 0
        self.last_turn_key: Optional[Hashable] = None
        self.ring_keys: Dict[str, List[Hashable]] = {}

    @property
    def prompt_bytes(self) -> int:
        return sum(p["_size"] for p in self.prompts.values())

    def put(self, key: Hashable, entry: _Entry, *, to_end: bool = False) -> None:
        old = self.entries.get(key)
        if old is not None:
            self.bytes -= old.size
        self.entries[key] = entry          # an existing key keeps its first-seen position...
        if to_end:
            self.entries.move_to_end(key)  # ...unless its order is the order of its writes
        self.bytes += entry.size

    def drop(self, key: Hashable) -> None:
        old = self.entries.pop(key, None)
        if old is not None:
            self.bytes -= old.size
        if isinstance(key, tuple) and key and key[0] == "ring":
            ring = self.ring_keys.get(key[1])
            if ring and key in ring:
                ring.remove(key)

    def drop_group(self, group: str, pred: Optional[Callable[[Hashable], bool]] = None) -> None:
        for key in [k for k, e in self.entries.items() if e.group == group and (pred is None or pred(k))]:
            self.drop(key)

    def clear_turn(self) -> None:
        self.drop_group(TURN)
        self.last_turn_key = None
        self.untagged_seg += 1


class LiveViewLedger:
    """Per-chat record of the live events the web lane delivered. Thread-safe.

    `owner_of(session_id)` names the account that owns a chat; it is asked once, when a chat's
    record is created by an event, and the answer is stamped on the record (None when it cannot
    be told, and such a record is then never replayed to an owner)."""

    def __init__(self, *, max_sessions: int = MAX_SESSIONS, max_entries: int = MAX_ENTRIES,
                 max_bytes: int = MAX_BYTES, max_prompts: int = MAX_PROMPTS,
                 clock: Callable[[], float] = time.monotonic,
                 owner_of: Optional[Callable[[str], Optional[str]]] = None) -> None:
        self._max_sessions = max_sessions
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._max_prompts = max_prompts
        self._clock = clock
        self._owner_of = owner_of
        self._lock = threading.Lock()
        self._chats: "OrderedDict[str, _Chat]" = OrderedDict()
        self._last_sweep = clock()

    # ── writing ────────────────────────────────────────────────────────────────────────

    def _chat(self, session_id: str, owner: Optional[str]) -> _Chat:
        chat = self._chats.get(session_id)
        if chat is None:
            chat = self._chats[session_id] = _Chat(owner)
        self._chats.move_to_end(session_id)
        while len(self._chats) > self._max_sessions:
            self._chats.popitem(last=False)     # the chat written least recently
        return chat

    def _lookup_owner(self, sid: str) -> Optional[str]:
        if self._owner_of is None:
            return None
        try:
            owner = self._owner_of(sid)
            return str(owner) if owner is not None else None
        except Exception:
            return None

    def record(self, session_id: Optional[str], event: Any) -> None:
        """Record one event as it is about to be delivered to the chat's viewers."""
        if not session_id or not isinstance(event, dict):
            return
        etype = event.get("type")
        if not isinstance(etype, str) or not etype:
            return
        sid = str(session_id)
        with self._lock:
            known = sid in self._chats
        if not known and not self._wanted(etype, event):
            return
        # Outside the lock: the owner is read from the store, once per new record.
        owner = None if known else self._lookup_owner(sid)
        env = _forgotten(sid)
        with self._lock:
            now = self._clock()
            chat = self._chat(sid, owner)
            self._purge(chat, now)
            self._apply(chat, etype, _scrub(event, env), now)
            self._bound(chat)
            if not chat.entries and not chat.prompts:
                self._chats.pop(sid, None)
            if now - self._last_sweep >= SWEEP_EVERY:
                self._sweep(now)

    @staticmethod
    def _wanted(etype: str, event: dict) -> bool:
        """Does this event create a record at all? (A clear for a chat without one does not.)"""
        if etype in ("agent_message_update", "tool_update", "file_created", "subagent_update",
                     "browser_frame_update", "workflow_start", "workflow_update", "workflow_done"):
            return True
        if etype in RING_SIZES or etype.endswith("_state"):
            return True
        if etype == "document_preview_wait":
            try:
                return int(event.get("position") or 0) > 0
            except (TypeError, ValueError):
                return False
        return False

    def _enter_turn(self, chat: _Chat, event: dict) -> None:
        """A turn event of another turn than the one on record: what that one left behind
        (it died without leaving the queue cleanly) is not this turn's."""
        tid = event.get("turnId")
        if not isinstance(tid, str) or not tid:
            return
        if chat.turn_id and chat.turn_id != tid:
            chat.clear_turn()
            chat.prompts.pop(chat.turn_id, None)
        chat.turn_id = tid

    def _apply(self, chat: _Chat, etype: str, event: dict, now: float) -> None:
        if etype in _TURN_EVENTS:
            self._enter_turn(chat, event)
            # A turn that still produces events is not abandoned: its prompts stay.
            for prompt in chat.prompts.values():
                prompt["_expires"] = now + TURN_TTL

        if etype in ("message_complete", "generation_stopped"):
            # The end of the turn is part of its picture until the turn leaves the queue.
            if etype == "generation_stopped":
                # Stop drops what was still queued; the stopped turn itself stays until it
                # leaves the queue (it may still be stored).
                for tid in [t for t in chat.prompts if t != chat.turn_id]:
                    chat.prompts.pop(tid, None)
            if not chat.entries and not chat.prompts:
                return
            tid = event.get("turnId") or chat.turn_id or ""
            chat.put(("end", str(tid)), _Entry(TURN, _cap(event), now, ENTRY_TTL, R_STATE))
            chat.last_turn_key = None
            return

        if etype == "agent_message_update":
            tid = event.get("turnId") or ""
            rnd = event.get("round")
            if isinstance(rnd, int) and not isinstance(rnd, bool):
                key: Hashable = ("msg", tid, "r", rnd)
            else:
                prev = chat.last_turn_key
                if not (isinstance(prev, tuple) and prev[:3] == ("msg", tid, "u")):
                    chat.untagged_seg += 1
                key = ("msg", tid, "u", chat.untagged_seg)
            chat.put(key, _Entry(TURN, _cap(event), now, ENTRY_TTL, R_TURN))
            chat.last_turn_key = key
            return

        if etype == "clear_last_assistant":
            rnd = event.get("round")
            if isinstance(rnd, int) and not isinstance(rnd, bool):
                chat.drop_group(TURN, lambda k: isinstance(k, tuple) and k[0] == "msg"
                                and k[2] == "r" and k[3] == rnd)
            else:
                untagged = [k for k in chat.entries
                            if isinstance(k, tuple) and k[0] == "msg" and k[2] == "u"]
                if untagged:
                    chat.drop(untagged[-1])
            chat.last_turn_key = None
            return

        if etype == "tool_update":
            tool_id = event.get("toolId")
            sub = event.get("subType")
            if not tool_id:
                chat.seq += 1
                chat.put(("tool", None, chat.seq), _Entry(TURN, _cap(event), now, ENTRY_TTL, R_TURN))
                chat.last_turn_key = ("tool", None, chat.seq)
                return
            if sub == "start":
                key = ("tool", str(tool_id), "start")
                # A tool that started and has not ended is the step the person is waiting on.
                running = ("tool", str(tool_id), "end") not in chat.entries
                chat.put(key, _Entry(TURN, _cap(event), now, TURN_TTL if running else ENTRY_TTL,
                                     R_RUNNING if running else R_TURN, protected=running))
            else:
                key = ("tool", str(tool_id), "end")
                chat.put(key, _Entry(TURN, _cap(event), now, ENTRY_TTL, R_LINES))
                start = chat.entries.get(("tool", str(tool_id), "start"))
                if start is not None:
                    start.protected = False
                    start.rank = R_TURN
                    start.expires = min(start.expires, now + ENTRY_TTL)
            chat.last_turn_key = key
            return

        if etype == "file_created":
            path = event.get("filePath")
            if not path:
                return
            key = ("file", str(path), event.get("turnId") or "")
            chat.put(key, _Entry(TURN, _cap(event), now, ENTRY_TTL, R_TURN))
            chat.last_turn_key = key
            return

        if etype == "document_preview_wait":
            try:
                position = int(event.get("position") or 0)
            except (TypeError, ValueError):
                position = 0
            if position > 0:
                chat.put(("preview",), _Entry(PREVIEW, _cap(event), now, PREVIEW_TTL, R_STATE))
            else:
                chat.drop(("preview",))
            return
        if etype in ("document_preview_busy", "sidebar_documents_set"):
            chat.drop(("preview",))
            return

        if etype == "browser_frame_update":
            frame = event.get("frame")
            if isinstance(frame, str) and len(frame) > MAX_FRAME:
                chat.drop(("last", etype))
                return
            chat.put(("last", etype), _Entry(WORKER, _cap(event, MAX_FRAME), now, ENTRY_TTL, R_LINES),
                     to_end=True)
            return

        if etype in RING_SIZES:
            chat.seq += 1
            key = ("ring", etype, chat.seq)
            chat.put(key, _Entry(WORKER, _cap(event), now, ENTRY_TTL, R_LINES))
            ring = chat.ring_keys.setdefault(etype, [])
            ring.append(key)
            while len(ring) > RING_SIZES[etype]:
                chat.drop(ring[0])
            return

        if etype == "workflow_start":
            chat.drop_group(WORKER, lambda k: isinstance(k, tuple) and len(k) > 1 and k[1] in _WORKFLOW)
            chat.ring_keys.pop("workflow_output_stream", None)
            chat.put(("wf", "workflow_start"), _Entry(WORKER, _cap(event), now, ENTRY_TTL, R_STATE))
            return
        if etype == "workflow_update":
            chat.seq += 1
            step = event.get("stepId")
            key = ("wf", "workflow_update", str(step)) if step else ("wf", "workflow_update", None, chat.seq)
            chat.put(key, _Entry(WORKER, _cap(event), now, ENTRY_TTL, R_TURN))
            return
        if etype == "workflow_done":
            chat.put(("wf", "workflow_done"), _Entry(WORKER, _cap(event), now, WORKER_DONE_TTL, R_STATE))
            for key, entry in chat.entries.items():
                if isinstance(key, tuple) and len(key) > 1 and (key[0] == "wf" or key[1] in _WORKFLOW):
                    entry.expires = min(entry.expires, now + WORKER_DONE_TTL)
            return

        if etype == "subagent_update" or etype.endswith("_state"):
            key = ("last", etype)
            old = chat.entries.get(key)
            merged = dict(old.event) if old is not None else {}
            fresh = _cap(event)
            for k, v in fresh.items():
                # The page keeps the previous step list when an update carries none.
                if k == "steps" and not (isinstance(v, list) and v):
                    continue
                merged[k] = v
            ttl = ENTRY_TTL
            presence = str(event.get("presence") or "").strip().lower()
            if etype == "subagent_update" and presence in ("idle", "error"):
                ttl = WORKER_DONE_TTL
            # Moved to the end on each write: the replay plays a worker's states in the order
            # they happened (a frame after an idle would otherwise wake the window again).
            chat.put(key, _Entry(WORKER, merged, now, ttl, R_STATE), to_end=True)
            return
        # Everything else (history_update, chat_wait, toasts, signals) is not part of the view.

    def _over(self, chat: _Chat) -> bool:
        return (len(chat.entries) + len(chat.prompts) > self._max_entries
                or chat.bytes + chat.prompt_bytes > self._max_bytes)

    def _bound(self, chat: _Chat) -> None:
        """Drop entries by rank, oldest first within a rank, until both limits hold. A tool
        that is still running is the last entry to go and then a prompt; the limits always
        hold, whatever arrives."""
        if not self._over(chat):
            return
        for rank in (R_LINES, R_TURN, R_STATE, R_RUNNING):
            for key in [k for k, e in chat.entries.items() if e.rank == rank]:
                if not self._over(chat):
                    return
                chat.drop(key)
        while self._over(chat) and chat.prompts:
            chat.prompts.popitem(last=False)

    def _purge(self, chat: _Chat, now: float) -> None:
        for key in [k for k, e in chat.entries.items() if e.expires <= now]:
            chat.drop(key)
        for tid in [t for t, p in chat.prompts.items() if p.get("_expires", 0) <= now]:
            chat.prompts.pop(tid, None)

    def _sweep(self, now: float) -> None:
        self._last_sweep = now
        for sid in list(self._chats):
            chat = self._chats[sid]
            self._purge(chat, now)
            if not chat.entries and not chat.prompts:
                self._chats.pop(sid, None)

    def set_prompt(self, session_id: Optional[str], turn_id: Optional[str], entry: dict, *,
                   owner: Optional[str] = None) -> None:
        """The person's message of a turn that was queued and is not stored yet (the runner
        stores it with the answer). Dropped when that turn leaves the queue.

        `owner` is the chat's owning account. A record kept for another owner (or for none)
        is dropped first: whatever it holds is not this chat's."""
        if not session_id or not turn_id or not isinstance(entry, dict):
            return
        sid = str(session_id)
        env = _forgotten(sid)
        if owner is None:
            with self._lock:
                known = sid in self._chats
            owner = None if known else self._lookup_owner(sid)
        with self._lock:
            now = self._clock()
            chat = self._chats.get(sid)
            if chat is not None and owner is not None and chat.owner != str(owner):
                self._chats.pop(sid, None)
                chat = None
            chat = self._chat(sid, str(owner) if owner is not None else None)
            stored = _scrub(_cap(dict(entry)), env)
            stored["turnId"] = str(turn_id)
            stored["_expires"] = now + TURN_TTL
            stored["_size"] = _size(stored)
            chat.prompts[str(turn_id)] = stored
            while len(chat.prompts) > self._max_prompts:
                chat.prompts.popitem(last=False)
            self._bound(chat)

    def turn_left_queue(self, session_id: Optional[str], turn_id: Optional[str]) -> None:
        """The turn is out of the queue (the runner calls this right before `task_done`): what
        it showed is in the stored transcript now, or will never be. Its events and its prompt
        go; the prompts of turns still queued stay."""
        if not session_id:
            return
        with self._lock:
            chat = self._chats.get(str(session_id))
            if chat is None:
                return
            if turn_id is None or chat.turn_id in (None, str(turn_id)):
                chat.clear_turn()
                chat.turn_id = None
            if turn_id is not None:
                chat.prompts.pop(str(turn_id), None)
            if not chat.entries and not chat.prompts:
                self._chats.pop(str(session_id), None)

    def scrub(self, session_id: Optional[str], env: Dict[str, str]) -> None:
        """Replace forgotten values in everything kept for the chat (entries and prompts)."""
        if not session_id or not env:
            return
        with self._lock:
            chat = self._chats.get(str(session_id))
            if chat is None:
                return
            for key, entry in list(chat.entries.items()):
                clean = _scrub(entry.event, env)
                if clean != entry.event:
                    chat.bytes -= entry.size
                    entry.event = clean
                    entry.size = _size(clean)
                    chat.bytes += entry.size
            for tid, prompt in list(chat.prompts.items()):
                chat.prompts[tid] = _scrub(prompt, env)

    def forget(self, session_id: Optional[str]) -> None:
        """Drop everything about a chat (deleted, hidden)."""
        if not session_id:
            return
        with self._lock:
            self._chats.pop(str(session_id), None)

    # ── reading ────────────────────────────────────────────────────────────────────────

    def snapshot(self, session_id: Optional[str], *, active: bool,
                 owner: Optional[str] = None) -> dict:
        """`{"prompts": [entry, ...], "events": [event, ...], "turn": bool}` for one chat,
        events in record order; `turn` says whether a running turn is on record.

        `active` is the queue's answer for this chat. When it says no turn runs or waits,
        neither the prompts nor the turn's events are returned: the server says the turn is
        over, and a kept picture of it would only show a spinner that nothing will clear.
        `owner` is the chat's current owning account: a record stamped for another one is
        dropped and nothing is returned. Every caller in the product passes it."""
        empty: dict = {"prompts": [], "events": [], "turn": False}
        if not session_id:
            return empty
        sid = str(session_id)
        with self._lock:
            chat = self._chats.get(sid)
            if chat is None:
                return empty
            if owner is not None and chat.owner != str(owner):
                self._chats.pop(sid, None)
                return empty
            self._purge(chat, self._clock())
            events = [dict(e.event) for e in chat.entries.values() if active or e.group != TURN]
            prompts = ([{k: v for k, v in p.items() if not k.startswith("_")}
                        for p in chat.prompts.values()] if active else [])
            turn = active and (bool(prompts) or any(e.group == TURN for e in chat.entries.values()))
            return {"prompts": prompts, "events": events, "turn": turn}
