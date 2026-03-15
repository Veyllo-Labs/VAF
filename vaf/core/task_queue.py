import heapq
import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

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

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()

class TaskQueue:
    TASK_CLASS_INTERACTIVE = "interactive"
    TASK_CLASS_AUTOMATION = "automation"
    TASK_CLASS_BACKGROUND = "background"
    _VALID_TASK_CLASSES = {
        TASK_CLASS_INTERACTIVE,
        TASK_CLASS_AUTOMATION,
        TASK_CLASS_BACKGROUND,
    }

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(TaskQueue, cls).__new__(cls)
                    cls._instance._cv = threading.Condition()
                    cls._instance._legacy_heap = []
                    cls._instance._queues = {
                        cls.TASK_CLASS_INTERACTIVE: [],
                        cls.TASK_CLASS_AUTOMATION: [],
                        cls.TASK_CLASS_BACKGROUND: [],
                    }
                    cls._instance._counter = itertools.count()
                    cls._instance.active_task = None
                    cls._instance._stop_requests = set()  # Session IDs that requested stop
                    cls._instance._session_inflight = set()
                    cls._instance._inflight_by_worker = {}
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

    def add(
        self,
        session_id: str,
        input_text: str,
        source: str = "web",
        callback: Callable = None,
        priority: int = 10,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """
        Add a task to the queue.
        Priority: Lower number = Higher priority.
        metadata: Optional dict (e.g. user_scope_id for RAG) passed to the task.
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
        with self._cv:
            item = (int(priority), task.created_at, next(self._counter), task)
            if self._legacy_mode:
                heapq.heappush(self._legacy_heap, item)
            else:
                heapq.heappush(self._queues[task_class], item)
            self._cv.notify()
        return task

    def get(self, timeout: float = 0.1, worker_id: Optional[str] = None) -> Optional[AgentTask]:
        """
        Get the next runnable task with weighted fairness.
        Prevents parallel execution of multiple main tasks for the same session.
        """
        worker_key = worker_id or str(threading.get_ident())
        deadline = time.time() + max(0.0, float(timeout))
        with self._cv:
            while True:
                task = self._pop_next_task_locked()
                if task is not None:
                    self._session_inflight.add(task.session_id)
                    self._inflight_by_worker[worker_key] = task
                    if self.active_task is None:
                        self.active_task = task
                    return task
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cv.wait(timeout=remaining)

    def task_done(self, task: Optional[AgentTask] = None, worker_id: Optional[str] = None):
        """Mark a task as done and release session in-flight lock."""
        worker_key = worker_id or str(threading.get_ident())
        with self._cv:
            resolved = task or self._inflight_by_worker.get(worker_key)
            if resolved is not None:
                self._session_inflight.discard(resolved.session_id)
            self._inflight_by_worker.pop(worker_key, None)
            self.active_task = next(iter(self._inflight_by_worker.values()), None)
            self._cv.notify_all()

    def get_queue_size(self) -> int:
        with self._cv:
            if self._legacy_mode:
                return len(self._legacy_heap)
            return sum(len(h) for h in self._queues.values())

    def is_busy(self) -> bool:
        with self._cv:
            return bool(self._inflight_by_worker)

    def get_queue_stats(self) -> Dict[str, Any]:
        with self._cv:
            if self._legacy_mode:
                oldest = 0
                if self._legacy_heap:
                    oldest = int(max(0.0, time.time() - float(self._legacy_heap[0][1])))
                return {
                    "interactive": len(self._legacy_heap),
                    "automation": 0,
                    "background": 0,
                    "inflight_total": len(self._inflight_by_worker),
                    "inflight_sessions": len(self._session_inflight),
                    "oldest_wait_interactive_sec": oldest,
                    "oldest_wait_automation_sec": 0,
                    "oldest_wait_background_sec": 0,
                    "queue_policy": "legacy",
                }
            now = time.time()
            def _oldest_wait(task_class: str) -> int:
                heap = self._queues[task_class]
                if not heap:
                    return 0
                created = min(float(item[1]) for item in heap)
                return int(max(0.0, now - created))
            return {
                "interactive": len(self._queues[self.TASK_CLASS_INTERACTIVE]),
                "automation": len(self._queues[self.TASK_CLASS_AUTOMATION]),
                "background": len(self._queues[self.TASK_CLASS_BACKGROUND]),
                "inflight_total": len(self._inflight_by_worker),
                "inflight_sessions": len(self._session_inflight),
                "oldest_wait_interactive_sec": _oldest_wait(self.TASK_CLASS_INTERACTIVE),
                "oldest_wait_automation_sec": _oldest_wait(self.TASK_CLASS_AUTOMATION),
                "oldest_wait_background_sec": _oldest_wait(self.TASK_CLASS_BACKGROUND),
                "queue_policy": "weighted_fair",
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
        text = (input_text or "").strip()
        if src == "automation" or metadata.get("automation_task_id"):
            return self.TASK_CLASS_AUTOMATION
        if src in ("system", "thinking", "background"):
            return self.TASK_CLASS_BACKGROUND
        if metadata.get("compaction") is True:
            return self.TASK_CLASS_BACKGROUND
        if str(session_id or "").strip().lower() == "system":
            return self.TASK_CLASS_BACKGROUND
        if text.startswith("__CMD__:"):
            return self.TASK_CLASS_BACKGROUND
        return self.TASK_CLASS_INTERACTIVE

    def _reset_scheduler_budget_locked(self) -> None:
        self._scheduler_budget = {
            cls: max(1, int(weight))
            for cls, weight in self._class_weights.items()
        }

    def _class_has_runnable_locked(self, task_class: str) -> bool:
        heap = self._queues[task_class]
        for _prio, _created, _idx, task in heap:
            if task.session_id not in self._session_inflight:
                return True
        return False

    def _pop_runnable_from_class_locked(self, task_class: str) -> Optional[AgentTask]:
        heap = self._queues[task_class]
        parked = []
        selected = None
        while heap:
            item = heapq.heappop(heap)
            task = item[3]
            if task.session_id in self._session_inflight:
                parked.append(item)
                continue
            selected = task
            break
        for item in parked:
            heapq.heappush(heap, item)
        return selected

    def _pop_next_task_locked(self) -> Optional[AgentTask]:
        if self._legacy_mode:
            parked = []
            selected = None
            while self._legacy_heap:
                item = heapq.heappop(self._legacy_heap)
                task = item[3]
                if task.session_id in self._session_inflight:
                    parked.append(item)
                    continue
                selected = task
                break
            for item in parked:
                heapq.heappush(self._legacy_heap, item)
            return selected

        classes = (
            self.TASK_CLASS_INTERACTIVE,
            self.TASK_CLASS_AUTOMATION,
            self.TASK_CLASS_BACKGROUND,
        )
        available = [c for c in classes if self._class_has_runnable_locked(c)]
        if not available:
            return None

        # Weighted fairness: consume per-class budget; reset once exhausted.
        class_order = sorted(
            available,
            key=lambda c: (-self._scheduler_budget.get(c, 0), c),
        )
        for cls in class_order:
            if self._scheduler_budget.get(cls, 0) <= 0:
                continue
            task = self._pop_runnable_from_class_locked(cls)
            if task is not None:
                self._scheduler_budget[cls] = max(
                    0, self._scheduler_budget.get(cls, 0) - 1
                )
                return task

        self._reset_scheduler_budget_locked()
        class_order = sorted(
            available,
            key=lambda c: (-self._scheduler_budget.get(c, 0), c),
        )
        for cls in class_order:
            task = self._pop_runnable_from_class_locked(cls)
            if task is not None:
                self._scheduler_budget[cls] = max(
                    0, self._scheduler_budget.get(cls, 0) - 1
                )
                return task
        return None
