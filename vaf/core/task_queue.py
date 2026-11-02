# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The queue in front of the agent loops: every chat turn, channel turn, wake turn,
compaction and session command waits here until a worker of the headless runner takes it.

The order, in this precedence:

- **Priority bands.** A lower ``priority`` always goes first (``RELOAD_CONFIG`` at 1 before
  every turn at 10, a compaction at 15 after them).
- **Classes** (``queue_policy: weighted_fair`` only): interactive, automation and background
  share the workers by the budget ``queue_weight_*`` (5/3/1). Under ``legacy`` there is one
  lane and only the bands count.
- **People.** Inside one band (and class), the next turn goes to the person served least
  recently (:class:`vaf.core.fair_queue.FairOrder`, keyed on the canonical scope), so one
  person with turns in many chats no longer holds up the others. One person's own turns stay
  in arrival order. Session commands are their own key (``HOUSEKEEPING_KEY``), never the
  sender's: a command is the server's housekeeping, not a person's turn.
- **One turn per chat at a time.** A task whose session is in flight is skipped and keeps its
  place; the next eligible one is taken.

A turn queued with ``on_wait`` hears its place (:class:`vaf.core.fair_queue.WaitInfo`,
resource ``chat``) whenever it changes, and position 0 when a worker takes it after a wait.
The place is the place in line: it ignores whether the turn's own chat is still busy, so a
turn behind its own running turn waits for that one too. One chat reports ONE place, its
first queued turn's (what ``wait_for_session`` answers for a reload); a later turn of the
same chat starts reporting when it becomes the first. The live place equals the reloaded one
because every queued web turn carries ``on_wait`` (both web enqueues pass it): a chat whose
first queued turn has none (a channel or wake turn in the same session) reports through its
first watched turn, while a reload names the first queued one. Places are counted up to
``PLACES_COUNTED``; a turn further back hears ``PLACES_COUNTED + 1``, meaning "place
``PLACES_COUNTED + 1`` or later", that is at least ``PLACES_COUNTED`` turns ahead, with no
ETA. Both bounds keep the cost of a change independent of how many turns
one account queued: measured before them, 2000 queued turns of one account made every
other person's enqueue simulate 2001 picks and fire 2000 callbacks.

Named boundaries: the queue never refuses and has no cap per person (a dropped chat message
is worse than a wait, and the round robin already bounds one person's cost to another at one
turn per round per band); channel turns get the fair order but no waiting message (that would
be an extra outbound message on someone's messenger); thinking and automation runs, the
voice call reply, sub-agents and the coder do not wait here (see ``fair_queue``).
"""
import itertools
import math
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Hashable, List, Optional, Tuple

from vaf.core.fair_queue import (
    METRICS_WINDOW_S,
    _MAX_SAMPLES,
    FairOrder,
    WaitInfo,
    _canonical_key,
    _ms,
    _percentile,
)

# The fair key of every session command: a tuple, so no scope string can ever equal it.
HOUSEKEEPING_KEY = ("housekeeping",)
# The lane every task shares under queue_policy "legacy" (no class weights).
_LEGACY_LANE = "all"
# How many places in line are counted exactly. A turn further back is told
# PLACES_COUNTED + 1 (that place or later: at least PLACES_COUNTED ahead, no ETA): working
# out every place would make every enqueue and every pop cost as much as the whole backlog,
# which one account can grow.
PLACES_COUNTED = 20


@dataclass
class AgentTask:
    """A task for the agent to execute within a specific session."""
    session_id: str
    input_text: str
    source: str = "web"  # 'web' or 'cli'
    callback: Optional[Callable[[str], None]] = None
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    priority: int = 10
    task_class: str = "interactive"
    # The identity of this exchange, stamped at CREATION and not when the
    # runner picks the task up: a file can be announced before the turn even
    # starts (an image attached to the message being sent), and it still
    # belongs to this turn. Everything the turn emits carries it, so the UI
    # can bind an answer and the files it produced to each other instead of
    # guessing from arrival order.
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # When the task entered the queue, on the queue's monotonic clock (the wait samples
    # and the waited time of a place report run from here; created_at is wall time).
    queued_at: float = 0.0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


class _Watch:
    """One task whose place is reported: the last place computed for it (under the queue
    lock) and the sequence number of the last report delivered (under the notify lock).
    Only the first watched task of a chat reports; the others wait their turn silently."""
    __slots__ = ("task", "on_wait", "last_position", "delivered_seq")

    def __init__(self, task: AgentTask, on_wait: Callable[[WaitInfo], None]) -> None:
        self.task = task
        self.on_wait = on_wait
        self.last_position: Optional[int] = None
        self.delivered_seq = -1


class TaskQueue:
    TASK_CLASS_INTERACTIVE = "interactive"
    TASK_CLASS_AUTOMATION = "automation"
    TASK_CLASS_BACKGROUND = "background"
    _VALID_TASK_CLASSES = {
        TASK_CLASS_INTERACTIVE,
        TASK_CLASS_AUTOMATION,
        TASK_CLASS_BACKGROUND,
    }
    _CLASS_ORDER = (TASK_CLASS_INTERACTIVE, TASK_CLASS_AUTOMATION, TASK_CLASS_BACKGROUND)

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(TaskQueue, cls).__new__(cls)
                    cls._instance._cv = threading.Condition()
                    # (lane, priority) -> the fair order of that band. The lane is the task
                    # class under weighted_fair and _LEGACY_LANE under legacy.
                    cls._instance._orders = {}
                    cls._instance.active_task = None
                    cls._instance._stop_requests = set()  # Session IDs that requested stop
                    cls._instance._session_inflight = set()
                    cls._instance._inflight_by_worker = {}
                    cls._instance._inflight_since = {}
                    cls._instance._class_weights = {
                        cls.TASK_CLASS_INTERACTIVE: 5,
                        cls.TASK_CLASS_AUTOMATION: 3,
                        cls.TASK_CLASS_BACKGROUND: 1,
                    }
                    cls._instance._legacy_mode = True
                    try:
                        from vaf.core.config import Config

                        policy = str(Config.get("queue_policy", "legacy") or "legacy").strip().lower()
                        cls._instance._legacy_mode = policy != "weighted_fair"
                        cls._instance._class_weights = {
                            cls.TASK_CLASS_INTERACTIVE: int(Config.get("queue_weight_interactive", 5) or 5),
                            cls.TASK_CLASS_AUTOMATION: int(Config.get("queue_weight_automation", 3) or 3),
                            cls.TASK_CLASS_BACKGROUND: int(Config.get("queue_weight_background", 1) or 1),
                        }
                    except Exception:
                        cls._instance._legacy_mode = True
                    cls._instance._scheduler_budget = {}
                    cls._instance._reset_scheduler_budget_locked()
                    # Place reports and the numbers behind the ETA and the health row.
                    cls._instance._clock = time.monotonic
                    cls._instance._workers = 1
                    # session -> its watched tasks in arrival order (the head reports),
                    # session -> its queued tasks in arrival order, and the sessions whose
                    # reported place is within PLACES_COUNTED (the only ones a change can move).
                    cls._instance._watch = {}
                    cls._instance._queued_by_session = {}
                    cls._instance._shown = set()
                    cls._instance._watch_seq = itertools.count(1)
                    cls._instance._notify_lock = threading.RLock()
                    cls._instance._waits = deque(maxlen=_MAX_SAMPLES)
                    cls._instance._services = deque(maxlen=_MAX_SAMPLES)
                    cls._instance._service_start = {}
        return cls._instance

    def request_stop(self, session_id: str):
        """Request generation stop for a specific session."""
        with self._cv:
            self._stop_requests.add(session_id)

    def should_stop(self, session_id: str) -> bool:
        """Check if a session has requested stop."""
        with self._cv:
            return session_id in self._stop_requests

    def clear_stop(self, session_id: str):
        """Clear the stop request for a session (called after stopping)."""
        with self._cv:
            self._stop_requests.discard(session_id)

    def set_workers(self, workers: int) -> None:
        """How many workers take tasks from this queue (the runner's effective count): the
        ETA divides the places ahead by it, and the health row shows it as the slots."""
        with self._cv:
            self._workers = max(1, int(workers or 1))

    def drop_queued_tasks_for_session(self, session_id: str) -> int:
        """
        Remove all queued (not in-flight) tasks for a given session.
        Returns the number of removed tasks.
        """
        sid = str(session_id or "")
        if not sid:
            return 0
        removed = 0
        deliveries: List[Tuple[_Watch, int, WaitInfo]] = []
        with self._cv:
            if not self._queued_by_session.get(sid):
                return 0
            for band in list(self._orders):
                order = self._orders[band]
                removed += len(order.remove_where(lambda task: str(task.session_id) == sid))
                if not order:
                    del self._orders[band]
            self._queued_by_session.pop(sid, None)
            # The dropped turns will never run: their watchers leave without a word.
            self._watch.pop(sid, None)
            self._shown.discard(sid)
            if removed:
                deliveries = self._recompute_locked()
                self._cv.notify_all()
        self._deliver(deliveries)
        return removed

    def add(
        self,
        session_id: str,
        input_text: str,
        source: str = "web",
        callback: Callable = None,
        priority: int = 10,
        metadata: Optional[Dict[str, Any]] = None,
        *,
        on_wait: Optional[Callable[[WaitInfo], None]] = None,
    ):
        """
        Add a task to the queue.
        Priority: Lower number = Higher priority.
        metadata: Optional dict (e.g. user_scope_id for RAG) passed to the task.
        on_wait: told the task's place (WaitInfo, resource "chat") while it waits, and
        position 0 when a worker takes it after a wait. Called outside the queue lock;
        an exception in it is swallowed.
        """
        md = metadata or {}
        task_class = self._classify_task_class(
            session_id=session_id,
            input_text=input_text,
            source=source,
            metadata=md,
        )
        task = AgentTask(
            session_id=session_id,
            input_text=input_text,
            source=source,
            callback=callback,
            metadata=md,
            priority=priority,
            task_class=task_class,
        )
        deliveries: List[Tuple[_Watch, int, WaitInfo]] = []
        with self._cv:
            task.queued_at = self._clock()
            lane = _LEGACY_LANE if self._legacy_mode else task_class
            band = (lane, int(priority))
            order = self._orders.get(band)
            if order is None:
                order = self._orders[band] = FairOrder()
            order.push(self._fair_key(task), task)
            sid = str(task.session_id)
            self._queued_by_session.setdefault(sid, deque()).append(task)
            touched = ()
            if on_wait is not None:
                watchers = self._watch.setdefault(sid, deque())
                watchers.append(_Watch(task, on_wait))
                if len(watchers) == 1:
                    touched = (sid,)
            if self._watch:
                deliveries = self._recompute_locked(touched=touched)
            self._cv.notify()
        self._deliver(deliveries)
        return task

    def get(self, timeout: float = 0.1, worker_id: Optional[str] = None) -> Optional[AgentTask]:
        """
        Get the next runnable task: by priority band, class budget and person (module
        docstring). Prevents parallel execution of multiple main tasks for the same session.
        """
        worker_key = worker_id or str(threading.get_ident())
        deadline = time.time() + max(0.0, float(timeout))
        task: Optional[AgentTask] = None
        deliveries: List[Tuple[_Watch, int, WaitInfo]] = []
        with self._cv:
            # A worker holds at most one task: asking for the next one means the
            # previous is over, whether or not task_done() was reached. That is a
            # CONTRACT on worker_key - one consumer per key, never two threads
            # sharing an explicit worker_id, or this frees a session the other one
            # is still turning. Releasing here is what
            # _cleanup_stale_inflight_locked() below cannot do -
            # that reaper only handles NUMERIC keys of DEAD threads, so a named
            # worker_id, or a long-lived thread like the CLI's main loop, never
            # self-heals. Without this, one missed task_done() parks every later
            # task for that session forever (_pop_next_task_locked skips in-flight
            # sessions), and the caller then dereferences the None it gets back.
            previous = self._inflight_by_worker.get(worker_key)
            if previous is not None:
                self._session_inflight.discard(previous.session_id)
                if self.active_task is previous:
                    self.active_task = None
            self._end_service_locked(worker_key)
            self._cleanup_stale_inflight_locked()
            while True:
                task = self._pop_next_task_locked()
                if task is not None:
                    self._session_inflight.add(task.session_id)
                    self._inflight_by_worker[worker_key] = task
                    self._inflight_since[worker_key] = time.time()
                    if self.active_task is None:
                        self.active_task = task
                    now = self._clock()
                    sample = not is_system_command(task)
                    if sample:
                        self._waits.append((now, max(0.0, now - task.queued_at)))
                    self._service_start[worker_key] = (now, sample)
                    self._unqueue_locked(task)
                    if self._watch:
                        deliveries = self._recompute_locked(popped=task)
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cv.wait(timeout=remaining)
        self._deliver(deliveries)
        return task

    def reset_runtime_state(self, include_queued: bool = False) -> None:
        """
        Reset runtime-only queue state (in-flight locks and active task pointers).
        Useful after worker crashes/restarts where stale in-flight locks can block dequeue.
        """
        with self._cv:
            self.active_task = None
            self._session_inflight.clear()
            self._inflight_by_worker.clear()
            self._inflight_since.clear()
            self._service_start.clear()
            if include_queued:
                self._orders.clear()
                self._queued_by_session.clear()
                # The cleared tasks will never run: their watchers leave without a word.
                self._watch.clear()
                self._shown.clear()
            self._cv.notify_all()

    def try_claim_session(self, session_id: str) -> bool:
        """Claim a session for OUT-OF-QUEUE work (the headless runner's result drain).

        The drain runs outside get()/task_done(), so without a claim its summary
        chat_step could mutate the same session concurrently with a worker's chat turn
        (history/session.json write races — chat-while-subagent-runs with
        parallel_main_workers>1). Returns False when the session is already in flight;
        the caller must skip that session and retry on a later pass. A successful claim
        also makes queued tasks for the session wait (get() skips in-flight sessions)
        until release_session_claim().
        """
        if not session_id:
            return True  # no session to protect — don't block the drain
        with self._cv:
            if session_id in self._session_inflight:
                return False
            self._session_inflight.add(session_id)
            return True

    def release_session_claim(self, session_id: str) -> None:
        """Release a try_claim_session() claim. Only call after a successful claim —
        the claim/skip contract guarantees this never releases a lock a worker holds."""
        if not session_id:
            return
        with self._cv:
            self._session_inflight.discard(session_id)
            self._cv.notify_all()

    def stalled_tasks(self, threshold_s: float) -> "List[Dict[str, Any]]":
        """Tasks that have been IN FLIGHT longer than the threshold.

        A worker that took a task and never called task_done is invisible in
        every queue count - the queues read empty while a whole worker is gone.
        The one live incident ran three hours: a LOAD_SESSION command entered
        flight and the metrics printed inflight_total=1 until shutdown, with
        nothing anywhere saying WHERE it hung. This is the hook the stall
        reporter reads so the next one dumps its stacks instead.
        """
        now = time.time()
        out: List[Dict[str, Any]] = []
        with self._cv:
            for worker_key, since in self._inflight_since.items():
                age = now - since
                if age < threshold_s:
                    continue
                task = self._inflight_by_worker.get(worker_key)
                preview = ""
                try:
                    preview = str(getattr(task, "input_text", "") or "")[:80]
                except Exception:
                    pass
                out.append({"worker": str(worker_key), "age_s": int(age),
                            "session_id": str(getattr(task, "session_id", "") or ""),
                            "preview": preview})
        return out

    def task_done(self, task: Optional[AgentTask] = None, worker_id: Optional[str] = None):
        """Mark a task as done and release session in-flight lock."""
        worker_key = worker_id or str(threading.get_ident())
        with self._cv:
            resolved = task or self._inflight_by_worker.get(worker_key)
            if resolved is not None:
                self._session_inflight.discard(resolved.session_id)
            self._inflight_by_worker.pop(worker_key, None)
            self._inflight_since.pop(worker_key, None)
            self._end_service_locked(worker_key)
            self.active_task = next(iter(self._inflight_by_worker.values()), None)
            self._cv.notify_all()

    def get_queue_size(self) -> int:
        with self._cv:
            return sum(len(order) for order in self._orders.values())

    def is_busy(self) -> bool:
        with self._cv:
            return bool(self._inflight_by_worker)

    @staticmethod
    def _task_scope(task: "AgentTask") -> Optional[str]:
        """Best-effort user_scope_id for a task. Enqueue sites use two different metadata keys:
        web interactive turns set 'enqueue_user_scope_id'; thinking/timer/automation/compaction set
        'user_scope_id'. Read BOTH. Returns None for housekeeping tasks, which must NOT count as a
        user's turn for fairness gating: a scope-less task, and every system command, also one
        that carries the scope of the account that sent it (the session commands do, so a
        terminal lane can tell whose chat they name). Counted as that account's, a command
        would hold its thinking run back, and revoking the account would stop the shared
        command session and drop every account's queued commands with it."""
        if is_system_command(task):
            return None
        md = task.metadata or {}
        scope = md.get("enqueue_user_scope_id") or md.get("user_scope_id")
        if scope is None:
            return None
        scope = str(scope).strip()
        return scope or None

    @classmethod
    def _fair_key(cls, task: "AgentTask") -> Hashable:
        """Whose turn a task is, for the round robin: a session command is housekeeping, any
        other task its person's canonical scope (the local admin's aliases are one key), and
        a task without a scope the "default" bucket, the local admin's."""
        if is_system_command(task):
            return HOUSEKEEPING_KEY
        return _canonical_key(cls._task_scope(task))

    def _iter_queued(self):
        """Yield every queued AgentTask (caller must hold self._cv)."""
        for order in self._orders.values():
            yield from order.items()

    def _iter_all_tasks(self):
        """Yield every in-flight and queued AgentTask (caller must hold self._cv)."""
        for task in self._inflight_by_worker.values():
            if task is not None:
                yield task
        yield from self._iter_queued()

    def sessions_for_scope(self, user_scope_id: str) -> set:
        """The sessions with an in-flight or queued task of this account. For stopping
        everything one account runs (vaf.core.revocation). Scopes are compared by trust's
        canonical key, as `processes.list_for_scope` does, so the local admin's aliases match;
        housekeeping tasks (`_task_scope`) never do."""
        if not str(user_scope_id or "").strip():
            return set()
        from vaf.core.trust import _scope_key
        key = _scope_key(user_scope_id)
        with self._cv:
            out = set()
            for task in self._iter_all_tasks():
                scope = self._task_scope(task)
                if scope is not None and task.session_id and _scope_key(scope) == key:
                    out.add(str(task.session_id))
            return out

    def is_busy_for_scope(self, target_key: str, canonicalize: Callable[[Any], str]) -> bool:
        """True if any in-flight OR queued task belongs to the same user as `target_key`.

        `target_key` is the canonical key of the user we are gating (already run through the
        caller's canonicalizer, e.g. thinking_mode._key). `canonicalize` maps each task's raw
        metadata scope to its canonical key, so admin aliases (None/'default'/local-admin-UUID)
        collapse and match correctly. Housekeeping tasks (`_task_scope`: system commands and
        scope-less tasks) are skipped: they are not a user's turn and must not block another
        user's thinking run."""
        if not target_key:
            return False
        with self._cv:
            for task in self._iter_all_tasks():
                s = self._task_scope(task)
                if s is None:
                    continue
                try:
                    if canonicalize(s) == target_key:
                        return True
                except Exception:
                    continue
            return False

    def is_busy_for_session(self, session_id: str) -> bool:
        """True if this session has an in-flight OR queued task - "is a turn running
        or about to run in this chat", the per-session sibling of is_busy_for_scope().

        `_session_inflight` is checked first because it is the wider set: it carries
        the worker in-flight sessions AND the drain's try_claim_session() claims,
        which have no task object in `_inflight_by_worker` to iterate. Housekeeping
        commands need no filter here: `enqueue_system_command` enqueues every one under
        `SYSTEM_SESSION_ID`. A web client can name a chat `system` (the ownership gate admits
        an id with no record); such a chat is never a command (`is_system_command`), it only
        shares the commands' per-session serialization.
        """
        sid = str(session_id or "")
        if not sid:
            return False
        with self._cv:
            if sid in self._session_inflight:
                return True
            return bool(self._queued_by_session.get(sid))

    def has_queued(self, session_id: str) -> bool:
        """Whether this chat has a turn waiting in the queue (in flight does not count)."""
        with self._cv:
            return bool(self._queued_by_session.get(str(session_id or "")))

    def get_queue_stats(self) -> Dict[str, Any]:
        with self._cv:
            now = time.time()
            queued = list(self._iter_queued())

            def _oldest_wait(tasks: List[AgentTask]) -> int:
                if not tasks:
                    return 0
                created = min(float(task.created_at) for task in tasks)
                return int(max(0.0, now - created))

            if self._legacy_mode:
                return {
                    "interactive": len(queued),
                    "automation": 0,
                    "background": 0,
                    "inflight_total": len(self._inflight_by_worker),
                    "inflight_sessions": len(self._session_inflight),
                    "oldest_wait_interactive_sec": _oldest_wait(queued),
                    "oldest_wait_automation_sec": 0,
                    "oldest_wait_background_sec": 0,
                    "queue_policy": "legacy",
                }
            by_class = {c: [t for t in queued if t.task_class == c] for c in self._CLASS_ORDER}
            return {
                "interactive": len(by_class[self.TASK_CLASS_INTERACTIVE]),
                "automation": len(by_class[self.TASK_CLASS_AUTOMATION]),
                "background": len(by_class[self.TASK_CLASS_BACKGROUND]),
                "inflight_total": len(self._inflight_by_worker),
                "inflight_sessions": len(self._session_inflight),
                "oldest_wait_interactive_sec": _oldest_wait(by_class[self.TASK_CLASS_INTERACTIVE]),
                "oldest_wait_automation_sec": _oldest_wait(by_class[self.TASK_CLASS_AUTOMATION]),
                "oldest_wait_background_sec": _oldest_wait(by_class[self.TASK_CLASS_BACKGROUND]),
                "queue_policy": "weighted_fair",
            }

    def wait_for_session(self, session_id: str) -> Optional[WaitInfo]:
        """The place of this chat's first queued task in the pick order (a page reload or a
        chat switch shows it again), or None when the chat has nothing queued."""
        sid = str(session_id or "")
        if not sid:
            return None
        with self._cv:
            queued = self._queued_by_session.get(sid)
            if not queued:
                return None
            for place, task in enumerate(self._pick_order_locked(PLACES_COUNTED), start=1):
                if str(task.session_id) == sid:
                    return self._wait_info_locked(task, place)
            return self._wait_info_locked(queued[0], PLACES_COUNTED + 1)

    def metrics(self) -> Dict[str, Any]:
        """The numbers of the health row, in the shape of ResourceQueue.metrics(): counts and
        durations over the last METRICS_WINDOW_S seconds, never a key or a session. It never
        refuses, so every refusal count is zero."""
        with self._cv:
            now = self._clock()
            cutoff = now - METRICS_WINDOW_S
            waits = [v for t, v in self._waits if t >= cutoff]
            services = [v for t, v in self._services if t >= cutoff]
            queued = list(self._iter_queued())
            keys = {self._fair_key(task) for task in queued}
            keys.discard(HOUSEKEEPING_KEY)
            return {
                "name": "chat",
                "slots": self._workers,
                "running": len(self._inflight_by_worker),
                "waiting": len(queued),
                "waiting_users": len(keys),
                "served": len(services),
                "refused": {"timeout": 0, "full": 0, "cancelled": 0},
                "wait_ms": {"p50": _ms(_percentile(waits, 50)),
                            "p95": _ms(_percentile(waits, 95)),
                            "max": _ms(max(waits) if waits else None)},
                "service_ms": {"p50": _ms(_percentile(services, 50)),
                               "p95": _ms(_percentile(services, 95))},
                "last_refusal_age_s": None,
                "by_class": {c: sum(1 for t in queued if t.task_class == c)
                             for c in self._CLASS_ORDER},
                "policy": "legacy" if self._legacy_mode else "weighted_fair",
            }

    def _classify_task_class(
        self,
        session_id: str,
        input_text: str,
        source: str,
        metadata: Dict[str, Any],
    ) -> str:
        explicit = str(metadata.get("task_class") or "").strip().lower()
        if explicit in self._VALID_TASK_CLASSES:
            return explicit

        src = (source or "").strip().lower()
        if src == "automation" or metadata.get("automation_task_id"):
            return self.TASK_CLASS_AUTOMATION
        if src in ("system", "thinking", "background"):
            return self.TASK_CLASS_BACKGROUND
        if metadata.get("compaction") is True:
            return self.TASK_CLASS_BACKGROUND
        if str(session_id or "").strip().lower() == "system":
            return self.TASK_CLASS_BACKGROUND
        return self.TASK_CLASS_INTERACTIVE

    def _fresh_budget(self) -> Dict[str, int]:
        return {cls: max(1, int(weight)) for cls, weight in self._class_weights.items()}

    def _reset_scheduler_budget_locked(self) -> None:
        self._scheduler_budget = self._fresh_budget()

    # ── selection: ONE routine for the real pop and the place simulation ─────

    @staticmethod
    def _has_eligible(order: FairOrder, eligible: Optional[Callable[[Any], bool]]) -> bool:
        return order.has_eligible(eligible)

    def _select_locked(self, orders: Dict[Tuple[str, int], FairOrder], budget: Dict[str, int],
                       eligible: Optional[Callable[[Any], bool]]) -> Optional[AgentTask]:
        """Take the next task from ``orders`` (mutated, as is ``budget``): the lowest priority
        band first; under weighted_fair the class by the budget 5/3/1 first; inside a band
        the person served least recently (FairOrder). ``eligible`` None takes everything."""
        def _pop_bands(bands) -> Optional[AgentTask]:
            for band in bands:
                order = orders[band]
                if not self._has_eligible(order, eligible):
                    continue
                task = order.pop(eligible)
                if not order:
                    del orders[band]
                return task
            return None

        if self._legacy_mode:
            return _pop_bands(sorted(orders, key=lambda b: (b[1], b[0])))

        def _class_bands(cls: str):
            return sorted((b for b in orders if b[0] == cls), key=lambda b: b[1])

        available = [c for c in self._CLASS_ORDER
                     if any(self._has_eligible(orders[b], eligible) for b in _class_bands(c))]
        if not available:
            return None

        # Weighted fairness: consume per-class budget; reset once exhausted.
        class_order = sorted(available, key=lambda c: (-budget.get(c, 0), c))
        for cls in class_order:
            if budget.get(cls, 0) <= 0:
                continue
            task = _pop_bands(_class_bands(cls))
            if task is not None:
                budget[cls] = max(0, budget.get(cls, 0) - 1)
                return task

        budget.clear()
        budget.update(self._fresh_budget())
        class_order = sorted(available, key=lambda c: (-budget.get(c, 0), c))
        for cls in class_order:
            task = _pop_bands(_class_bands(cls))
            if task is not None:
                budget[cls] = max(0, budget.get(cls, 0) - 1)
                return task
        return None

    def _pop_next_task_locked(self) -> Optional[AgentTask]:
        inflight = self._session_inflight
        return self._select_locked(self._orders, self._scheduler_budget,
                                   lambda task: task.session_id not in inflight)

    def _pick_order_locked(self, limit: Optional[int] = None) -> List[AgentTask]:
        """The queued tasks in the order the workers would take them (the first ``limit``
        of them), in-flight chats ignored (the place in line), computed on copies so nothing
        moves. A limited copy holds each person's first ``limit`` tasks only."""
        orders = {band: order.copy(limit) for band, order in self._orders.items()}
        budget = dict(self._scheduler_budget)
        picked: List[AgentTask] = []
        while orders and (limit is None or len(picked) < limit):
            task = self._select_locked(orders, budget, None)
            if task is None:
                break
            picked.append(task)
        return picked

    # ── place reports, ETA, samples ──────────────────────────────────────────

    def _eta_locked(self, position: int) -> Optional[float]:
        if position <= 0:
            return None
        cutoff = self._clock() - METRICS_WINDOW_S
        samples = [v for t, v in self._services if t >= cutoff]
        if len(samples) < 3:
            return None
        return math.ceil(position / self._workers) * _percentile(samples, 50)

    def _wait_info_locked(self, task: AgentTask, position: int) -> WaitInfo:
        eta = self._eta_locked(position) if position <= PLACES_COUNTED else None
        return WaitInfo("chat", position, eta, max(0.0, self._clock() - task.queued_at))

    def _unqueue_locked(self, task: AgentTask) -> None:
        """A worker took this task: it leaves its chat's queued list."""
        sid = str(task.session_id)
        queued = self._queued_by_session.get(sid)
        if not queued:
            return
        try:
            queued.remove(task)
        except ValueError:
            pass
        if not queued:
            del self._queued_by_session[sid]

    def _recompute_locked(self, popped: Optional[AgentTask] = None, touched=()
                          ) -> List[Tuple[_Watch, int, WaitInfo]]:
        """The place reports due now, to be delivered OUTSIDE the lock. Bounded by
        PLACES_COUNTED, never by the backlog: only the first PLACES_COUNTED picks are worked
        out, and only chats that can have moved are looked at (those within the counted
        places now or at their last report, and the ``touched`` ones whose reporting turn
        just changed). Each chat reports its first watched turn's place when it changed;
        a watched turn a worker just took hears 0 if it had a place, and the chat's next
        watched turn takes over. Dropped turns left the watch silently in the drop."""
        out: List[Tuple[_Watch, int, WaitInfo]] = []
        seq = next(self._watch_seq)
        touched = set(touched)
        if popped is not None:
            sid = str(popped.session_id)
            watchers = self._watch.get(sid)
            for watch in list(watchers or ()):
                if watch.task is popped:
                    if watchers[0] is watch:
                        touched.add(sid)
                    watchers.remove(watch)
                    if (watch.last_position or 0) > 0:
                        out.append((watch, seq, WaitInfo(
                            "chat", 0, None, max(0.0, self._clock() - popped.queued_at))))
                    break
            if watchers is not None and not watchers:
                del self._watch[sid]
                self._shown.discard(sid)
        if not self._watch:
            return out
        top = self._pick_order_locked(PLACES_COUNTED)
        places = {id(task): place for place, task in enumerate(top, start=1)}
        candidates = touched | self._shown | {str(t.session_id) for t in top}
        for sid in candidates:
            watchers = self._watch.get(sid)
            if not watchers:
                self._shown.discard(sid)
                continue
            head = watchers[0]
            place = places.get(id(head.task), PLACES_COUNTED + 1)
            if place <= PLACES_COUNTED:
                self._shown.add(sid)
            else:
                self._shown.discard(sid)
            if place != head.last_position:
                head.last_position = place
                out.append((head, seq, self._wait_info_locked(head.task, place)))
        return out

    def _deliver(self, deliveries: List[Tuple[_Watch, int, WaitInfo]]) -> None:
        """Hand the reports out, outside the queue lock. Two threads can finish their
        recomputes in one order and deliver in the other; the sequence number makes a report
        older than the last one a task heard a no-op, so an older place never shows last."""
        if not deliveries:
            return
        with self._notify_lock:
            for watch, seq, info in deliveries:
                if seq <= watch.delivered_seq:
                    continue
                watch.delivered_seq = seq
                try:
                    watch.on_wait(info)
                except Exception:
                    pass

    def _end_service_locked(self, worker_key: str) -> None:
        """The worker's task is over: its service time becomes a sample (session commands
        are not samples; they take milliseconds and would pull the ETA toward zero)."""
        started = self._service_start.pop(worker_key, None)
        if started is None:
            return
        since, sample = started
        if sample:
            now = self._clock()
            self._services.append((now, max(0.0, now - since)))

    def _cleanup_stale_inflight_locked(self) -> None:
        """
        Remove stale in-flight reservations when worker threads are gone.
        Prevents permanent session lock where tasks are queued but never dequeued.
        """
        alive_thread_ids = {str(t.ident) for t in threading.enumerate() if t.ident is not None}
        stale_keys = []
        for key in list(self._inflight_by_worker.keys()):
            # Default worker keys are thread identifiers (stringified ints).
            # Only auto-clean numeric keys; custom keys are left untouched.
            if str(key).isdigit() and str(key) not in alive_thread_ids:
                stale_keys.append(key)
        if not stale_keys:
            return
        for key in stale_keys:
            task = self._inflight_by_worker.pop(key, None)
            self._service_start.pop(key, None)
            if task is not None:
                self._session_inflight.discard(task.session_id)
        if self.active_task is not None and self.active_task.session_id not in self._session_inflight:
            self.active_task = next(iter(self._inflight_by_worker.values()), None)
        self._cv.notify_all()


# The kinds of wake turn a consumer knows how to announce. A wake turn is a task no person
# typed: something the agent set in motion earlier tells it that it is due or done, and the
# agent runs a real turn in that chat. Consumers gate their wake card on
# ``metadata["wake"]`` (the kind), never on the text. ``draft``: the person sent a draft the
# agent's turn had stopped at (vaf/core/outbound_hold.py), and the agent carries on.
WAKE_KINDS = ("timer", "process", "draft")

# The wake kinds whose text is VAF's own report of something that happened: how a background
# command ended (vaf/core/processes.py `wake_text`) and what became of a draft
# (outbound_hold.wake_text). The result-grounding judge counts that text as evidence
# (`Agent._turn_reports`). A timer's text is what the agent wrote for itself earlier, so it
# reports nothing.
WAKE_REPORT_KINDS = ("process", "draft")


def enqueue_wake_turn(*, kind: str, session_id: str, text: str, source: str = "web",
                      user_scope_id: Any = None, username: Optional[str] = None,
                      role: Optional[str] = None, extra: Optional[Dict[str, Any]] = None) -> None:
    """Queue a wake turn for ONE chat, carrying the identity of the person it belongs to.

    One implementation for every wake lane - a fired timer, a finished background process -
    so the routing check (``enqueue_session_id``), the identity keys the runner rebinds
    from, and the kind the cards read cannot drift apart between them. ``extra`` adds the
    lane's own keys (``timer_id``, ``process_id``).
    """
    if kind not in WAKE_KINDS:
        raise ValueError(f"unknown wake kind {kind!r}")
    metadata: Dict[str, Any] = {
        "wake": kind,
        # Mirror normal enqueues so the headless routing-integrity check is satisfied.
        "enqueue_session_id": session_id,
    }
    if kind == "timer":
        metadata["timer"] = True   # the key the timer lane has always carried
    if user_scope_id is not None:
        metadata["user_scope_id"] = user_scope_id
    if username is not None:
        metadata["username"] = username
    if role is not None:
        metadata["role"] = role
    metadata.update(extra or {})
    TaskQueue().add(session_id=session_id, input_text=text, source=source, metadata=metadata)


SYSTEM_SESSION_ID = "system"
SYSTEM_COMMAND_PREFIX = "__CMD__:"
SYSTEM_COMMANDS = ("NEW_SESSION", "LOAD_SESSION", "RENAME_SESSION", "RELOAD_CONFIG")


def enqueue_system_command(command: str, *, priority: int = 10, user_scope_id: Any = None):
    """Queue a housekeeping command for the runner (`command` is the text after the prefix,
    e.g. ``"LOAD_SESSION:<id>"``). The one producer, so the mark `is_system_command` reads is
    set by VAF itself and never by whoever wrote the text: a contact's message reading
    ``__CMD__:RELOAD_CONFIG`` was run as a command, and a web chat named ``system`` can carry
    the same text. Not on the framework facade: the commands are the web server's."""
    name = str(command or "").split(":", 1)[0]
    if name not in SYSTEM_COMMANDS:
        raise ValueError(f"unknown system command {name!r}")
    metadata: Dict[str, Any] = {"task_class": "background", "system_command": True}
    if user_scope_id is not None:
        metadata["user_scope_id"] = user_scope_id
    return TaskQueue().add(session_id=SYSTEM_SESSION_ID, input_text=SYSTEM_COMMAND_PREFIX + command,
                           source="web", priority=priority, metadata=metadata)


def is_system_command(task) -> bool:
    """A command `enqueue_system_command` queued: its session, its mark and its prefix, all
    three. The text alone is anyone's to write (a contact, a chat), and the session id alone
    a web client can name."""
    meta = getattr(task, "metadata", None) or {}
    return (str(getattr(task, "session_id", "") or "") == SYSTEM_SESSION_ID
            and meta.get("system_command") is True
            and str(getattr(task, "input_text", "") or "").startswith(SYSTEM_COMMAND_PREFIX))


def wake_kind(metadata: Optional[Dict[str, Any]]) -> Optional[str]:
    """The wake kind of a task's metadata, or None for a turn a person started."""
    meta = metadata or {}
    kind = meta.get("wake")
    if kind in WAKE_KINDS:
        return kind
    return "timer" if meta.get("timer") else None
