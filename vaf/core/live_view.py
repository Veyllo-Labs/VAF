# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What a chat is doing right now, for a browser that was not watching it.

A browser socket is subscribed to ONE chat, and the live stream of a chat goes only to its
subscribers (`WebInterfaceManager.broadcast_to_session`). A person who switches from chat A
to chat B therefore receives nothing about A any more: the reply A streams, the tool cards
it starts, the coder window it fills and the files it announces all go to nobody. The stored
transcript does not have them either, because the runner writes a turn when it ENDS. Coming
back to A showed the transcript of the last finished turn and nothing of the running one
until the next live event happened to arrive.

`LiveViewLedger` is the missing answer to "what is this chat doing right now". The web
interface records every session-scoped event where it is delivered, BEFORE it is sent, and
`load_session` sends the record back as one `live_replay` frame right after the transcript.
The record is coalesced, not a log: a streamed round keeps its last text, a tool its start
and its end, a worker view its last state, so a replay is a picture of the moment and not a
re-run of the whole stream.

Rules, each against its failure mode:

- Groups. The TURN group (streamed text, tool cards, created files of the running turn) is
  cleared by `message_complete` and `generation_stopped`, and `snapshot(active=False)` never
  returns it: when the queue says no turn runs, nothing of a turn is replayed, so a turn that
  ended while nobody watched can never come back as a spinner. The WORKER group (sub-agent
  window, coder and other `*_state` views, workflow steps) survives the turn end, because an
  async worker outlives the turn that started it; it expires on its own. The PREVIEW group is
  the place of an Office document's preview in the converter queue.
- Stored as sent. An event is copied (strings capped), never rebuilt field by field, so a
  field a producer adds later reaches the replay without a change here.
- `*_state` without registration. Every type ending in `_state` is kept as its last value
  (merged shallowly, like the page merges it), so the view of an embedder's own agent that
  emits through `emit_agent_state` is not blind after a switch either.
- Bounded: 64 KB per string, 600 entries and 2 MB per chat, 32 chats. What goes first is the
  oldest entry; the turn's prompts and a tool that started and has not ended never go, because
  without them the replay would show an answer without its question or lose the running step.
- Isolation. Keyed by session id only, and never read for prompt content. The only readers are
  the server's `load_session` AFTER its ownership gate and the connect-time auto-load of the
  connection's own newest chat, so a replay reaches exactly the people the live events would
  have reached.
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
TRUNCATED = " [truncated]"

# Seconds.
ENTRY_TTL = 30 * 60          # any entry, after its last write
WORKER_DONE_TTL = 10 * 60    # a worker view after the worker said idle/error, a finished workflow
PREVIEW_TTL = 10 * 60        # a preview place without an update
SWEEP_EVERY = 30.0           # how often one record sweeps the other chats

RING_SIZES = {
    "subagent_output_stream": 200,
    "browser_step_update": 200,
    "workflow_output_stream": 200,
    "subagent_output": 20,
}

TURN, WORKER, PREVIEW = "turn", "worker", "preview"
_TURN_END = frozenset({"message_complete", "generation_stopped"})
_WORKFLOW = frozenset({"workflow_start", "workflow_update", "workflow_done", "workflow_output_stream"})


def _cap(value: Any, limit: int = MAX_STRING, _depth: int = 0) -> Any:
    """A copy of `value` with every string longer than `limit` cut and marked. Containers are
    copied, so a producer that mutates its dict after sending cannot change the record."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + TRUNCATED
    if _depth > 12:
        return value
    if isinstance(value, dict):
        return {k: _cap(v, limit, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_cap(v, limit, _depth + 1) for v in value]
    return value


def _size(value: Any, _depth: int = 0) -> int:
    """A cheap estimate of the serialised size, close enough for a memory bound."""
    if isinstance(value, str):
        return len(value) + 2
    if _depth > 12:
        return 8
    if isinstance(value, dict):
        return 2 + sum(len(str(k)) + 4 + _size(v, _depth + 1) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return 2 + sum(_size(v, _depth + 1) + 1 for v in value)
    return 8


class _Entry:
    __slots__ = ("group", "event", "size", "written", "expires", "protected")

    def __init__(self, group: str, event: dict, now: float, ttl: float, protected: bool = False):
        self.group = group
        self.event = event
        self.size = _size(event)
        self.written = now
        self.expires = now + ttl
        self.protected = protected


class _Chat:
    """The record of one chat: entries in first-seen order, the prompts of its open turns."""

    def __init__(self) -> None:
        self.entries: "OrderedDict[Hashable, _Entry]" = OrderedDict()
        self.prompts: "OrderedDict[str, dict]" = OrderedDict()
        self.bytes = 0
        self.seq = 0
        # The untagged stream (no `round`): a new bubble begins after any other turn event,
        # the way the page starts a new bubble after a tool card.
        self.untagged_seg = 0
        self.last_turn_key: Optional[Hashable] = None
        self.ring_keys: Dict[str, List[Hashable]] = {}

    def put(self, key: Hashable, entry: _Entry) -> None:
        old = self.entries.get(key)
        if old is not None:
            self.bytes -= old.size
        self.entries[key] = entry          # an existing key keeps its first-seen position
        self.bytes += entry.size

    def drop(self, key: Hashable) -> None:
        old = self.entries.pop(key, None)
        if old is not None:
            self.bytes -= old.size

    def drop_group(self, group: str, pred: Optional[Callable[[Hashable], bool]] = None) -> None:
        for key in [k for k, e in self.entries.items() if e.group == group and (pred is None or pred(k))]:
            self.drop(key)


class LiveViewLedger:
    """Per-chat record of the live events the web lane delivered. Thread-safe."""

    def __init__(self, *, max_sessions: int = MAX_SESSIONS, max_entries: int = MAX_ENTRIES,
                 max_bytes: int = MAX_BYTES, clock: Callable[[], float] = time.monotonic) -> None:
        self._max_sessions = max_sessions
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._clock = clock
        self._lock = threading.Lock()
        self._chats: "OrderedDict[str, _Chat]" = OrderedDict()
        self._last_sweep = clock()

    # ── writing ────────────────────────────────────────────────────────────────────────

    def _chat(self, session_id: str) -> _Chat:
        chat = self._chats.get(session_id)
        if chat is None:
            chat = self._chats[session_id] = _Chat()
        self._chats.move_to_end(session_id)
        while len(self._chats) > self._max_sessions:
            self._chats.popitem(last=False)     # the chat written least recently
        return chat

    def record(self, session_id: Optional[str], event: Any) -> None:
        """Record one event as it is about to be delivered to the chat's viewers."""
        if not session_id or not isinstance(event, dict):
            return
        etype = event.get("type")
        if not isinstance(etype, str) or not etype:
            return
        sid = str(session_id)
        with self._lock:
            now = self._clock()
            if sid not in self._chats and not self._wanted(etype, event):
                return
            chat = self._chat(sid)
            self._purge(chat, now)
            self._apply(chat, etype, event, now)
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

    def _apply(self, chat: _Chat, etype: str, event: dict, now: float) -> None:
        if etype in _TURN_END:
            chat.drop_group(TURN)
            chat.last_turn_key = None
            chat.untagged_seg += 1
            if etype == "generation_stopped":
                chat.prompts.clear()        # Stop drops what was still queued, too
            else:
                tid = event.get("turnId")
                if tid and tid in chat.prompts:
                    chat.prompts.pop(tid)
                elif chat.prompts:
                    chat.prompts.popitem(last=False)
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
            chat.put(key, _Entry(TURN, _cap(event), now, ENTRY_TTL))
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
                key = ("tool", None, chat.seq)
                protected = False
            elif sub == "start":
                key = ("tool", str(tool_id), "start")
                # A tool that started and has not ended is the step the person is waiting on.
                protected = ("tool", str(tool_id), "end") not in chat.entries
            else:
                key = ("tool", str(tool_id), "end")
                protected = False
                start = chat.entries.get(("tool", str(tool_id), "start"))
                if start is not None:
                    start.protected = False
            chat.put(key, _Entry(TURN, _cap(event), now, ENTRY_TTL, protected=protected))
            chat.last_turn_key = key
            return

        if etype == "file_created":
            path = event.get("filePath")
            if not path:
                return
            key = ("file", str(path), event.get("turnId") or "")
            chat.put(key, _Entry(TURN, _cap(event), now, ENTRY_TTL))
            chat.last_turn_key = key
            return

        if etype == "document_preview_wait":
            try:
                position = int(event.get("position") or 0)
            except (TypeError, ValueError):
                position = 0
            if position > 0:
                chat.put(("preview",), _Entry(PREVIEW, _cap(event), now, PREVIEW_TTL))
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
            chat.put(("last", etype), _Entry(WORKER, _cap(event, MAX_FRAME), now, ENTRY_TTL))
            return

        if etype in RING_SIZES:
            chat.seq += 1
            key = ("ring", etype, chat.seq)
            chat.put(key, _Entry(WORKER, _cap(event), now, ENTRY_TTL))
            ring = chat.ring_keys.setdefault(etype, [])
            ring.append(key)
            while len(ring) > RING_SIZES[etype]:
                chat.drop(ring.pop(0))
            return

        if etype == "workflow_start":
            chat.drop_group(WORKER, lambda k: isinstance(k, tuple) and len(k) > 1 and k[1] in _WORKFLOW)
            chat.ring_keys.pop("workflow_output_stream", None)
            chat.put(("wf", "workflow_start"), _Entry(WORKER, _cap(event), now, ENTRY_TTL))
            return
        if etype == "workflow_update":
            chat.seq += 1
            step = event.get("stepId")
            key = ("wf", "workflow_update", str(step)) if step else ("wf", "workflow_update", None, chat.seq)
            chat.put(key, _Entry(WORKER, _cap(event), now, ENTRY_TTL))
            return
        if etype == "workflow_done":
            chat.put(("wf", "workflow_done"), _Entry(WORKER, _cap(event), now, WORKER_DONE_TTL))
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
            chat.put(key, _Entry(WORKER, merged, now, ttl))
            return
        # Everything else (history_update, chat_wait, toasts, signals) is not part of the view.

    def _bound(self, chat: _Chat) -> None:
        if len(chat.entries) <= self._max_entries and chat.bytes <= self._max_bytes:
            return
        for key in list(chat.entries):
            if len(chat.entries) <= self._max_entries and chat.bytes <= self._max_bytes:
                break
            entry = chat.entries[key]
            if entry.protected:
                continue
            chat.drop(key)
            ring = chat.ring_keys.get(key[1]) if isinstance(key, tuple) and key[0] == "ring" else None
            if ring and key in ring:
                ring.remove(key)

    def _purge(self, chat: _Chat, now: float) -> None:
        for key in [k for k, e in chat.entries.items() if e.expires <= now]:
            chat.drop(key)
            if isinstance(key, tuple) and key[0] == "ring":
                ring = chat.ring_keys.get(key[1])
                if ring and key in ring:
                    ring.remove(key)
        # A prompt that nothing ever closed (a crashed turn) goes with the turn's entries.
        for tid in [t for t, p in chat.prompts.items() if p.get("_expires", 0) <= now]:
            chat.prompts.pop(tid, None)

    def _sweep(self, now: float) -> None:
        self._last_sweep = now
        for sid in list(self._chats):
            chat = self._chats[sid]
            self._purge(chat, now)
            if not chat.entries and not chat.prompts:
                self._chats.pop(sid, None)

    def set_prompt(self, session_id: Optional[str], turn_id: Optional[str], entry: dict) -> None:
        """The person's message of a turn that was queued and is not stored yet (the runner
        stores it with the answer). Dropped by that turn's `message_complete`."""
        if not session_id or not turn_id or not isinstance(entry, dict):
            return
        with self._lock:
            now = self._clock()
            chat = self._chat(str(session_id))
            stored = _cap(dict(entry))
            stored["_expires"] = now + ENTRY_TTL
            chat.prompts[str(turn_id)] = stored

    def drop_prompt(self, session_id: Optional[str], turn_id: Optional[str] = None) -> None:
        """Drop one turn's prompt, or every prompt of the chat (`turn_id=None`)."""
        if not session_id:
            return
        with self._lock:
            chat = self._chats.get(str(session_id))
            if chat is None:
                return
            if turn_id is None:
                chat.prompts.clear()
            else:
                chat.prompts.pop(str(turn_id), None)

    def forget(self, session_id: Optional[str]) -> None:
        """Drop everything about a chat (deleted, hidden)."""
        if not session_id:
            return
        with self._lock:
            self._chats.pop(str(session_id), None)

    # ── reading ────────────────────────────────────────────────────────────────────────

    def snapshot(self, session_id: Optional[str], *, active: bool) -> dict:
        """`{"prompts": [entry, ...], "events": [event, ...], "turn": bool}` for one chat, events
        in first-seen order; `turn` says whether a running turn is on record (prompt or events).

        `active` is the queue's answer for this chat. When it says no turn runs or waits,
        neither the prompts nor the turn's events are returned: the server says the turn is
        over, and a kept picture of it would only show a spinner that nothing will clear."""
        empty: dict = {"prompts": [], "events": [], "turn": False}
        if not session_id:
            return empty
        with self._lock:
            chat = self._chats.get(str(session_id))
            if chat is None:
                return empty
            self._purge(chat, self._clock())
            events = [dict(e.event) for e in chat.entries.values() if active or e.group != TURN]
            prompts = ([{k: v for k, v in p.items() if k != "_expires"} for p in chat.prompts.values()]
                       if active else [])
            turn = active and (bool(prompts) or any(e.group == TURN for e in chat.entries.values()))
            return {"prompts": prompts, "events": events, "turn": turn}
