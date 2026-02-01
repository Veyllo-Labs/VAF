import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Dict
import time

@dataclass
class AgentTask:
    """A task for the agent to execute within a specific session."""
    session_id: str
    input_text: str
    source: str = "web"  # 'web' or 'cli'
    callback: Optional[Callable[[str], None]] = None
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()

class TaskQueue:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(TaskQueue, cls).__new__(cls)
                    cls._instance.queue = queue.PriorityQueue()
                    cls._instance.active_task = None
                    cls._instance._stop_requests = set()  # Session IDs that requested stop
        return cls._instance

    def request_stop(self, session_id: str):
        """Request generation stop for a specific session."""
        self._stop_requests.add(session_id)

    def should_stop(self, session_id: str) -> bool:
        """Check if a session has requested stop."""
        return session_id in self._stop_requests

    def clear_stop(self, session_id: str):
        """Clear the stop request for a session (called after stopping)."""
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
        task = AgentTask(
            session_id, input_text, source, callback, metadata=metadata or {}
        )
        # PriorityQueue stores tuples (priority, timestamp, item).
        # Using timestamp ensures FIFO for same priority.
        self.queue.put((priority, task.created_at, task))
        return task

    def get(self) -> Optional[AgentTask]:
        """Get the next task (blocking)."""
        try:
            _, _, task = self.queue.get(timeout=0.1)
            self.active_task = task
            return task
        except queue.Empty:
            return None

    def task_done(self):
        """Mark current task as done."""
        self.active_task = None
        self.queue.task_done()

    def get_queue_size(self) -> int:
        return self.queue.qsize()

    def is_busy(self) -> bool:
        return self.active_task is not None
