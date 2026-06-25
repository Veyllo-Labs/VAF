# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Short, in-memory, one-shot self-timers for the agent (CLI + WebUI).

The agent uses the ``set_timer`` tool to schedule a precise short delay ("in 60s say test",
"in 90s check the deploy"). When a timer fires it enqueues an ``AgentTask`` into THIS
process's ``TaskQueue``; the existing consumers (the CLI input loop and the headless worker)
then deliver it into the live session:

- message-only timer -> a proactive assistant message (no LLM turn). The task carries the
  ``__TIMER__:`` marker (see :data:`TIMER_MSG_PREFIX`) which both consumers recognise next to
  their ``__CMD__`` handling (``vaf/cli/cmd/run.py``, ``vaf/core/headless_runner.py``).
- task timer -> a normal turn (``input_text`` is the task prompt), so the agent acts and
  replies in-session.

Design notes:
- The store and scheduler are a process-wide singleton. ``set_timer`` always runs in the same
  process as that surface's queue consumer, so a timer created here is always delivered here —
  no cross-process sharing, no double-fire. The scheduler thread starts lazily on the first
  ``add_timer`` (idempotent).
- In-memory and per process: timers do NOT survive a restart. They are meant for short delays.
  For longer or persistent reminders use ``create_automation`` (frequency='once').
"""
from __future__ import annotations

import threading
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Marker prefix that flags a message-only timer fire in the task queue. Recognised by the CLI
# input loop and the headless worker (alongside their existing ``__CMD__`` handling).
TIMER_MSG_PREFIX = "__TIMER__:"


@dataclass
class Timer:
    """A single pending one-shot timer."""
    id: str
    fire_at: float                  # epoch seconds when it should fire
    session_id: str
    source: str = "web"             # 'web' or 'cli' (delivery surface)
    user_scope_id: Any = None
    username: Optional[str] = None
    role: Optional[str] = None
    message: Optional[str] = None   # message-only timer: text to deliver
    task: Optional[str] = None      # task timer: prompt the agent should act on
    label: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def seconds_remaining(self) -> int:
        return max(0, int(round(self.fire_at - time.time())))


class _TimerStore:
    """Process-wide singleton holding pending timers and the scheduler state."""
    _instance: Optional["_TimerStore"] = None
    _singleton_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._lock = threading.Lock()
                    inst._timers = {}            # id -> Timer
                    inst._scheduler_started = False
                    cls._instance = inst
        return cls._instance

    def add(self, timer: Timer) -> str:
        with self._lock:
            self._timers[timer.id] = timer
        return timer.id

    def cancel(self, timer_id: str) -> bool:
        with self._lock:
            return self._timers.pop(timer_id, None) is not None

    def list(self, session_id: Optional[str] = None) -> List[Timer]:
        with self._lock:
            items = list(self._timers.values())
        if session_id is not None:
            items = [t for t in items if t.session_id == session_id]
        return sorted(items, key=lambda t: t.fire_at)

    def pop_due(self, now: float) -> List[Timer]:
        with self._lock:
            due = [t for t in self._timers.values() if t.fire_at <= now]
            for t in due:
                self._timers.pop(t.id, None)
        return due


def _store() -> _TimerStore:
    return _TimerStore()


def add_timer(
    *,
    session_id: str,
    seconds: float,
    source: str = "web",
    user_scope_id: Any = None,
    username: Optional[str] = None,
    role: Optional[str] = None,
    message: Optional[str] = None,
    task: Optional[str] = None,
    label: str = "",
) -> Timer:
    """Register a one-shot timer in this process and ensure the scheduler is running.

    Exactly one of ``message`` / ``task`` should be set (validated by the tool layer).
    ``seconds`` is clamped to a minimum of 1s.
    """
    timer = Timer(
        id=_uuid.uuid4().hex[:8],
        fire_at=time.time() + max(1.0, float(seconds)),
        session_id=str(session_id),
        source=(source or "web"),
        user_scope_id=user_scope_id,
        username=username,
        role=role,
        message=message,
        task=task,
        label=label or "",
    )
    _store().add(timer)
    start_timer_scheduler()
    return timer


def list_timers(session_id: Optional[str] = None) -> List[Timer]:
    return _store().list(session_id)


def cancel_timer(timer_id: str) -> bool:
    return _store().cancel(timer_id)


def _fire(timer: Timer) -> None:
    """Deliver a due timer by enqueuing an AgentTask into THIS process's TaskQueue."""
    from vaf.core.task_queue import TaskQueue

    if timer.message is not None:
        # A message timer WAKES the agent: the note is fed in as a normal turn (not a passive
        # __TIMER__ delivery), so the agent becomes active -- it reads the note, can think / call
        # tools, and responds. This text is ALSO shown in the chat as the user-side "trigger" bubble
        # (see the headless timer handling), so it is kept short and readable.
        input_text = (
            f"⏰ Timer fired — your note: \"{timer.message}\". "
            f"Act on it now (or, if it is only a reminder, just tell the user). Keep it brief."
        )
    else:
        input_text = timer.task or ""

    metadata: Dict[str, Any] = {
        "timer": True,
        "timer_id": timer.id,
        # Mirror normal enqueues so the headless routing-integrity check is satisfied.
        "enqueue_session_id": timer.session_id,
    }
    if timer.user_scope_id is not None:
        metadata["user_scope_id"] = timer.user_scope_id
    if timer.username is not None:
        metadata["username"] = timer.username
    if timer.role is not None:
        metadata["role"] = timer.role

    TaskQueue().add(
        session_id=timer.session_id,
        input_text=input_text,
        source=timer.source,
        metadata=metadata,
    )


def _scheduler_loop(poll: float = 0.5) -> None:
    store = _store()
    while True:
        try:
            for t in store.pop_due(time.time()):
                try:
                    _fire(t)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(poll)


def start_timer_scheduler() -> None:
    """Start the single per-process scheduler thread (idempotent)."""
    store = _store()
    with store._lock:
        if store._scheduler_started:
            return
        store._scheduler_started = True
    threading.Thread(
        target=_scheduler_loop, name="vaf-timer-scheduler", daemon=True
    ).start()
