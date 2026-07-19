# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Durable rolling transcript buffer for the voice reflex system.

The reflex system (docs/agents/VOICE_REFLEX.md) needs a store the policy layer can
read: every heard utterance kept as context (a `store_only` verdict lands here), so
the agent can chime in on something it heard earlier and keep listening while the
owner talks to someone else. Today the only store is a 16-entry ephemeral ring buffer
on the per-connection voice-call record that dies at call end; this is its durable,
bounded successor.

Design (matches the RAG memory-safety posture: bounded, fail-open, isolated):
- Keyed per (user_scope_id, session) - a transcript is session/scope-scoped content
  and must never be process-global (user-isolation invariant); the spoken language is
  a trait of the speaker, the transcript of the conversation.
- Bounded on every axis: max entries per key, a retention age, and an LRU cap over all
  keys - a long-running or many-speaker session can never grow without bound.
- In-memory only; nothing is persisted (privacy: the rolling transcript is context,
  not a record). Retention is enforced on read AND write.
- Every function catches everything and degrades to empty, so the realtime path is
  never broken by a buffer problem.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from typing import Deque, List, Optional, Tuple

# (ts, speaker_label, verdict, text)
Entry = Tuple[float, Optional[str], Optional[str], str]

_MAX_ENTRIES = 200          # utterances kept per (scope, session)
_MAX_AGE_S = 20 * 60.0      # retention: entries older than this are dropped
_MAX_KEYS = 256             # LRU cap across all speakers/sessions
_MAX_TEXT = 600             # per-utterance text cap stored

_BUFFERS: "OrderedDict[str, Deque[Entry]]" = OrderedDict()
_LOCK = threading.Lock()


def _key(scope: Optional[str], session: Optional[str]) -> str:
    return f"{scope or 'local'}::{session or '_'}"


def _now(ts: Optional[float]) -> float:
    return time.time() if ts is None else float(ts)


def _prune_locked(buf: "Deque[Entry]", now: float) -> None:
    cutoff = now - _MAX_AGE_S
    while buf and buf[0][0] < cutoff:
        buf.popleft()
    while len(buf) > _MAX_ENTRIES:
        buf.popleft()


def record(scope: Optional[str], session: Optional[str], text: str, *,
           label: Optional[str] = None, verdict: Optional[str] = None,
           ts: Optional[float] = None) -> None:
    """Append one heard utterance to the (scope, session) transcript. Best-effort."""
    try:
        core = str(text or "").strip()
        if not core:
            return
        core = core[:_MAX_TEXT]
        now = _now(ts)
        key = _key(scope, session)
        with _LOCK:
            buf = _BUFFERS.get(key)
            if buf is None:
                buf = deque()
                _BUFFERS[key] = buf
            _BUFFERS.move_to_end(key)
            buf.append((now, label, verdict, core))
            _prune_locked(buf, now)
            while len(_BUFFERS) > _MAX_KEYS:
                _BUFFERS.popitem(last=False)
    except Exception:
        pass


def recent(scope: Optional[str], session: Optional[str], n: int = 12,
           since: Optional[float] = None) -> List[Entry]:
    """The last `n` still-fresh entries (oldest first), pruning expired ones. When
    `since` is given (a wall-clock ts, same clock as `record`), only entries at or
    after it are returned - used to scope a group-conversation context to what was
    said AFTER guest engagement started (so an engaged guest never sees the owner's
    earlier, pre-engagement private talk)."""
    try:
        key = _key(scope, session)
        now = time.time()
        k = max(0, int(n))
        with _LOCK:
            buf = _BUFFERS.get(key)
            if not buf:
                return []
            # A read refreshes LRU recency too: a key that is actively polled
            # (e.g. the policy layer deciding whether to chime in while the owner
            # is briefly silent) must not be evicted just because it stopped
            # RECEIVING new utterances.
            _BUFFERS.move_to_end(key)
            _prune_locked(buf, now)
            if not buf or k == 0:
                # k == 0 means "no entries", NOT the whole buffer ([-0:] == [:]).
                return []
            entries = list(buf)
            if since is not None:
                entries = [e for e in entries if e[0] >= float(since)]
            return entries[-k:]
    except Exception:
        return []


def digest(scope: Optional[str], session: Optional[str], n: int = 12,
           per_item: int = 160, total_cap: int = 1200,
           since: Optional[float] = None) -> str:
    """Bounded, prompt-ready text of the recent transcript (speaker-labelled),
    for the policy layer or a chime-in prompt. Empty on any problem. `since` scopes
    the window to entries at or after a wall-clock ts (see `recent`)."""
    try:
        lines: List[str] = []
        used = 0
        for _ts, label, _verdict, text in recent(scope, session, n, since=since):
            who = f"[{label}] " if label else ""
            line = (who + text)[:per_item]
            if used + len(line) + 1 > total_cap:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)
    except Exception:
        return ""


def clear(scope: Optional[str], session: Optional[str]) -> None:
    """Drop a (scope, session) transcript, e.g. at call/session end (retention)."""
    try:
        with _LOCK:
            _BUFFERS.pop(_key(scope, session), None)
    except Exception:
        pass
