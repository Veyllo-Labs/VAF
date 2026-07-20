# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Sub-Agent IPC (Inter-Process Communication) System

Enables communication between main agent and sub-agents running in separate terminals.
Uses file-based message queues for cross-process communication.

Architecture:
- Sub-agents write results to a queue file when finished
- Main agent polls the queue and processes results
- Each task has a unique ID for result matching
- Workflows can pause and resume when waiting for sub-agent results
"""
import contextvars
import json
import os
import uuid
import time
from contextlib import contextmanager
from pathlib import Path

# fcntl is Unix-only, use msvcrt on Windows for file locking
try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False
    try:
        import msvcrt
        HAS_MSVCRT = True
    except ImportError:
        HAS_MSVCRT = False
from typing import Optional, Dict, Any, List
from datetime import datetime
from dataclasses import dataclass, asdict, field, fields
from enum import Enum

from vaf.core.platform import Platform

import threading

# ─────────────────────────────────────────────────────────────────────────────
# <ipc-notification>: an active PUSH so the main agent reacts to a finished
# sub-agent IMMEDIATELY, instead of waiting for the headless runner's ~1s idle
# poll (which only fires when worker 1 happens to be free). Modelled on the
# harness <task-notification>: the producer signals on completion; the consumer
# (headless runner) wakes at once, with the poll kept as a reliable fallback.
#
# In-process producers set the event directly. A sub-agent SUBPROCESS is a
# different process, so its completion is bridged to the parent over the existing
# HTTP channel (_post_to_parent -> /api/subagent/stream), whose handler then calls
# notify_result_ready() in the parent (see web_server.receive_subagent_stream).
_result_ready_event = threading.Event()


def notify_result_ready() -> None:
    """Emit the <ipc-notification> push: a sub-agent result is ready. Idempotent —
    multiple completions collapse to one wake, and one consume drains ALL pending
    results, so no signal is lost."""
    _result_ready_event.set()


def take_result_notification() -> bool:
    """Non-blocking consume: return True (and clear) if a result-ready push is pending.

    The flag is cleared BEFORE the caller reads results, so a completion that lands
    during that read re-sets it and is picked up on the next pass (no missed wake-up)."""
    if _result_ready_event.is_set():
        _result_ready_event.clear()
        return True
    return False


def _push_result_ready(task_id: str, session_id: Optional[str]) -> None:
    """Fire the <ipc-notification> for a finished task: cross-process (POST to the
    parent) from a sub-agent subprocess, or an in-process event set otherwise. Never
    raises — the ~1s poll remains as the fallback path if the push cannot be delivered."""
    try:
        from vaf.core.web_interface import _in_subagent_subprocess
        if _in_subagent_subprocess():
            # Dispatch on the shared bridge pool (like every other subprocess->parent
            # signal) so the sub-agent's final completion step never blocks up to 1.5s
            # on a busy/unreachable parent. The ~1s poll covers a dropped POST anyway.
            from vaf.core.web_interface import _post_to_parent, _BRIDGE_POOL
            _BRIDGE_POOL.submit(_post_to_parent, {
                "type": "ipc_notification",
                "event": "subagent_result",
                "taskId": task_id or "",
                "sessionId": session_id or "",
            })
        else:
            notify_result_ready()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Ownership guard: task_ids an IN-PROCESS workflow engine loop (engine._await_subagent)
# is actively consuming itself. The background main runner drains ALL sessions on a push,
# so without this it could STEAL a workflow step's result out from under the engine's own
# consume loop (timing out a successful step + replying mid-workflow). Only bites with
# parallel_main_workers>1 (default 1 blocks the sole worker, so no idle drain runs).
#
# Time-stamped + TTL, so NO explicit unmark is needed: the engine refreshes the mark every
# poll while it awaits; once it consumes the result and stops refreshing, the stamp expires
# and the (now-gone) task is irrelevant. The engine consumes via consume_result() directly,
# which this guard does NOT touch — it only makes _check_subagent_results skip these ids.
_engine_owned: Dict[str, float] = {}
_engine_owned_lock = threading.Lock()


def mark_engine_owned(task_id: str) -> None:
    """Claim/refresh a task_id as owned by an in-process engine await loop."""
    if not task_id:
        return
    with _engine_owned_lock:
        _engine_owned[task_id] = time.monotonic()


def is_engine_owned(task_id: str, ttl: float = 5.0) -> bool:
    """True if an engine loop claimed this task within `ttl` seconds (and cleans up expired)."""
    if not task_id:
        return False
    with _engine_owned_lock:
        ts = _engine_owned.get(task_id)
        if ts is None:
            return False
        if time.monotonic() - ts > ttl:
            _engine_owned.pop(task_id, None)
            return False
        return True


class SubAgentStatus(Enum):
    """Status of a sub-agent task."""
    PENDING = "pending"      # Task queued, not started
    RUNNING = "running"      # Sub-agent is executing
    COMPLETED = "completed"  # Sub-agent finished successfully
    FAILED = "failed"        # Sub-agent encountered an error
    TIMEOUT = "timeout"      # Sub-agent timed out


@dataclass
class SubAgentTask:
    """Represents a sub-agent task with its metadata and result."""
    task_id: str
    agent_type: str          # e.g., "librarian_agent", "coding_agent"
    task_description: str    # The original task
    status: str              # SubAgentStatus value
    created_at: str          # ISO timestamp
    session_id: Optional[str] = None  # Session that created this task
    completed_at: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    last_heartbeat: Optional[str] = None # ISO timestamp of last liveness check
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SubAgentTask':
        # Handle missing session_id for backwards compatibility
        if 'session_id' not in data:
            data['session_id'] = None
        # Handle missing last_heartbeat for backwards compatibility
        if 'last_heartbeat' not in data:
            data['last_heartbeat'] = None
        return cls(**data)


@dataclass
class PausedWorkflow:
    """
    Represents a workflow that's paused waiting for a sub-agent result.
    
    When a workflow encounters an async sub-agent call, it saves its state
    here and returns control to the user. When the sub-agent finishes,
    the workflow is resumed from this saved state.
    """
    workflow_id: str                    # Unique ID for this workflow execution
    waiting_for_task_id: str            # The sub-agent task we're waiting for
    current_step_index: int             # Which step we're on (0-based)
    outputs: Dict[str, Any]             # Outputs collected so far
    variables: Dict[str, Any]           # Original input variables
    steps_data: List[Dict[str, Any]]    # Serialized workflow steps
    workflow_name: str                  # Name of the workflow (e.g., "deep_research")
    created_at: str                     # When the workflow was paused
    # Ownership. Defaulted so records written before these fields existed still load.
    # Without a session a paused record cannot be routed back to the run that created it,
    # which is why _cleanup_other_sessions_locked used to wipe the whole file (Rule 4.4).
    session_id: Optional[str] = None     # Session that started the run
    user_scope_id: Optional[str] = None  # Owning user (multi-user installs)
    username: Optional[str] = None
    template_id: str = ""                # Template key, e.g. "research_and_document"
    ui_workflow_id: str = ""             # Id the Web UI panel knows this run by
    awaiting_agent_type: str = ""        # Sub-agent the run is waiting for

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PausedWorkflow':
        # Drop unknown keys instead of raising: a record written by a NEWER build (or a
        # hand-edited file) must not break the drain of an older one. Boundary coercion,
        # same reason as SubAgentTask.from_dict above.
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


class SubAgentIPC:
    """
    Inter-Process Communication system for VAF sub-agents.
    
    Uses file-based queues with file locking for thread-safe operations.
    """
    
    def __init__(self):
        # Queue files stored in VAF data directory
        self.queue_dir = Platform.vaf_dir() / "subagent_queue"
        self.queue_dir.mkdir(parents=True, exist_ok=True)

        # Separate files for pending tasks and completed results
        self.pending_file = self.queue_dir / "pending_tasks.json"
        self.results_file = self.queue_dir / "completed_results.json"
        self.active_file = self.queue_dir / "active_tasks.json"
        self.paused_workflows_file = self.queue_dir / "paused_workflows.json"
        self.task_payloads_dir = self.queue_dir / "task_payloads"

        # Serializes read-modify-write mutations (see _mutation_guard).
        self._mutation_tlock = threading.RLock()
        self._mutation_lock_file = self.queue_dir / ".mutation.lock"

        # Initialize files if they don't exist
        self._init_queue_files()

    @contextmanager
    def _mutation_guard(self, timeout_s: float = 5.0):
        """Serialize read-modify-write mutations of the queue files.

        _read_json/_write_json lock only their own single call (and the
        atomic-rename write path bypasses even that), so any
        read -> modify -> write sequence could interleave with another
        mutator's and silently drop its update. This was not theoretical:
        two concurrent mark_task_running calls erased each other's
        active-task entry, which let two execute_workflow racers both past
        the duplicate guard (caught by the guard's concurrency test under
        full-suite load).

        In-process: one RLock (reentrant, so mutators may nest - e.g.
        mark_task_running calls _update_task_status). Across processes: a
        bounded blocking lock on a dedicated lockfile, held only by the
        OUTERMOST guard level - flock is NOT reentrant across a second file
        descriptor within the same process, so a nested acquisition would
        spin against itself for the whole timeout (caught live: every
        mark_task_running stalled 5s). On timeout or platform error we
        proceed WITHOUT the cross-process half (best-effort, same spirit as
        the per-file locks) rather than wedging a chat turn on a stuck lock.
        """
        with self._mutation_tlock:
            # Depth only changes under the RLock, so plain int is safe.
            self._mutation_depth = getattr(self, "_mutation_depth", 0) + 1
            outermost = self._mutation_depth == 1
            fh = None
            locked = False
            try:
                if outermost:
                    try:
                        fh = open(self._mutation_lock_file, "a+")
                        deadline = time.time() + timeout_s
                        while True:
                            try:
                                if HAS_FCNTL:
                                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                                    locked = True
                                elif HAS_MSVCRT:
                                    import msvcrt
                                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                                    locked = True
                                break
                            except (IOError, OSError):
                                if time.time() >= deadline:
                                    break
                                time.sleep(0.02)
                    except Exception:
                        fh = None
                yield
            finally:
                self._mutation_depth -= 1
                if fh is not None:
                    if locked:
                        try:
                            if HAS_FCNTL:
                                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                            elif HAS_MSVCRT:
                                import msvcrt
                                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                        except Exception:
                            pass
                    try:
                        fh.close()
                    except Exception:
                        pass
    
    def _init_queue_files(self):
        """Initialize queue files with empty arrays if they don't exist."""
        self.task_payloads_dir.mkdir(parents=True, exist_ok=True)
        for file in [self.pending_file, self.results_file, self.active_file, self.paused_workflows_file]:
            if not file.exists():
                self._write_json(file, [])
    
    def _lock_file(self, f, exclusive=False):
        """Cross-platform file locking."""
        try:
            if HAS_FCNTL:
                # Unix: use fcntl
                lock_type = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(f.fileno(), lock_type | fcntl.LOCK_NB)
            elif HAS_MSVCRT:
                # Windows: use msvcrt
                import msvcrt
                lock_mode = msvcrt.LK_NBLCK if exclusive else msvcrt.LK_NBRLCK
                msvcrt.locking(f.fileno(), lock_mode, 1)
        except (IOError, OSError):
            pass  # Ignore locking errors
    
    def _unlock_file(self, f):
        """Cross-platform file unlocking."""
        try:
            if HAS_FCNTL:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            elif HAS_MSVCRT:
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except (IOError, OSError):
            pass  # Ignore unlocking errors
    
    def _read_json(self, file_path: Path) -> List[Dict]:
        """Read JSON file with file locking (cross-platform)."""
        if not file_path.exists():
            return []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                self._lock_file(f, exclusive=False)
                try:
                    content = f.read().strip()
                    if not content:
                        return []
                    return json.loads(content)
                finally:
                    self._unlock_file(f)
        except (json.JSONDecodeError, IOError):
            return []
    
    def _write_json(self, file_path: Path, data: List[Dict], max_retries: int = 3):
        """Write JSON file with file locking and retry logic (cross-platform)."""
        last_error = None
        
        for attempt in range(max_retries):
            try:
                # Write to temp file first, then rename (atomic operation)
                temp_file = file_path.with_suffix('.tmp')
                
                with open(temp_file, 'w', encoding='utf-8') as f:
                    self._lock_file(f, exclusive=True)
                    try:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                    finally:
                        self._unlock_file(f)
                
                # Atomic rename
                temp_file.replace(file_path)
                return  # Success
                
            except (IOError, OSError, PermissionError) as e:
                last_error = e
                time.sleep(0.2 * (attempt + 1))  # Exponential backoff
                continue
            except Exception as e:
                # Fallback: direct write
                try:
                    with open(file_path, 'w', encoding='utf-8') as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                    return
                except:
                    last_error = e
        
        # Final fallback after all retries
        if last_error:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except:
                pass  # Silent fail - don't crash
    
    # ═══════════════════════════════════════════════════════════════════════════
    # MAIN AGENT METHODS
    # ═══════════════════════════════════════════════════════════════════════════
    
    def create_task(self, agent_type: str, task_description: str, session_id: str = None) -> str:
        """
        Create a new sub-agent task and return its ID.
        Called by main agent before spawning sub-agent.
        
        Args:
            agent_type: Type of sub-agent (e.g., "librarian_agent")
            task_description: Description of the task
            session_id: Optional session ID to associate with this task.
                       If not provided, uses the global current session ID.
        
        Returns:
            task_id: Unique identifier for tracking this task
        """
        task_id = str(uuid.uuid4())[:8]  # Short ID for readability
        
        # Use provided session_id or fall back to global current session
        effective_session_id = session_id or get_current_session_id()
        
        task = SubAgentTask(
            task_id=task_id,
            agent_type=agent_type,
            task_description=task_description[:500],  # Truncate long descriptions
            status=SubAgentStatus.PENDING.value,
            created_at=datetime.now().isoformat(),
            session_id=effective_session_id
        )

        # Add to pending tasks
        with self._mutation_guard():
            pending = self._read_json(self.pending_file)
            pending.append(task.to_dict())
            self._write_json(self.pending_file, pending)

        # Store full task payload for sub-agents that need it (e.g. document_agent with long tasks)
        # Enables retrieval via get_task_payload() when command-line would exceed OS limits
        self.store_task_payload(task_id, task_description)
        
        return task_id

    def store_task_payload(self, task_id: str, payload: str):
        """Store full task payload in a sidecar file. Used for long tasks exceeding command-line limits."""
        try:
            self.task_payloads_dir.mkdir(parents=True, exist_ok=True)
            path = self.task_payloads_dir / f"{task_id}.txt"
            with open(path, 'w', encoding='utf-8') as f:
                f.write(payload)
        except (IOError, OSError):
            pass

    def get_task_payload(self, task_id: str) -> Optional[str]:
        """Retrieve full task payload. Returns None if not found."""
        try:
            path = self.task_payloads_dir / f"{task_id}.txt"
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f:
                    return f.read()
        except (IOError, OSError):
            pass
        return None
    
    def mark_task_running(self, task_id: str):
        """Mark a task as running (sub-agent started)."""
        with self._mutation_guard():
            self._update_task_status(task_id, SubAgentStatus.RUNNING.value)

            # Move from pending to active
            pending = self._read_json(self.pending_file)
            active = self._read_json(self.active_file)

            for i, task in enumerate(pending):
                if task.get('task_id') == task_id:
                    task['status'] = SubAgentStatus.RUNNING.value
                    active.append(task)
                    pending.pop(i)
                    break

            self._write_json(self.pending_file, pending)
            self._write_json(self.active_file, active)
    
    def cancel_task(self, task_id: str) -> bool:
        """
        Remove a task from pending/active without recording a result.
        
        Used when falling back to in-process execution after a terminal spawn fails,
        so we don't later report a false startup timeout.
        """
        removed = False
        with self._mutation_guard():
            pending = self._read_json(self.pending_file)
            pending_after = [task for task in pending if task.get('task_id') != task_id]
            if len(pending_after) != len(pending):
                removed = True
                self._write_json(self.pending_file, pending_after)

            active = self._read_json(self.active_file)
            active_after = [task for task in active if task.get('task_id') != task_id]
            if len(active_after) != len(active):
                removed = True
                self._write_json(self.active_file, active_after)

        return removed

    def get_pending_results(self, session_id: str = None) -> List[SubAgentTask]:
        """
        Get all completed sub-agent results that haven't been processed yet.
        Called by main agent to check for completed sub-agent work.
        
        Args:
            session_id: If provided, only return results for this session.
                       If None, returns all pending results.
        
        Returns:
            List of completed SubAgentTask objects
        """
        results = self._read_json(self.results_file)
        tasks = [SubAgentTask.from_dict(r) for r in results]
        
        # Filter by session if requested
        if session_id:
            tasks = [t for t in tasks if t.session_id == session_id]
        
        return tasks
    
    def consume_result(self, task_id: str) -> Optional[SubAgentTask]:
        """
        Get and remove a specific result from the queue.
        Called by main agent after processing a sub-agent result.
        
        Returns:
            The SubAgentTask if found, None otherwise
        """
        with self._mutation_guard():
            results = self._read_json(self.results_file)

            for i, result in enumerate(results):
                if result.get('task_id') == task_id:
                    task = SubAgentTask.from_dict(results.pop(i))
                    self._write_json(self.results_file, results)
                    return task

        return None
    
    def get_active_tasks(self, session_id: str = None) -> List[SubAgentTask]:
        """
        Get currently running sub-agent tasks.
        
        Args:
            session_id: If provided, only return tasks for this session.
                       If None, returns all active tasks.
        """
        active = self._read_json(self.active_file)
        tasks = [SubAgentTask.from_dict(t) for t in active]
        
        if session_id:
            tasks = [t for t in tasks if t.session_id == session_id]
        
        return tasks
    
    def get_active_tasks_for_current_session(self) -> List[SubAgentTask]:
        """Get active tasks for the current session only."""
        current = get_current_session_id()
        if not current:
            return []
        return self.get_active_tasks(session_id=current)

    def get_pending_tasks(self, session_id: str = None) -> List[SubAgentTask]:
        """
        Get created-but-not-yet-running tasks (create_task done, spawn still in
        flight: mark_task_running only fires once the child process is up).

        Args:
            session_id: If provided, only return tasks for this session.
                       If None, returns all pending tasks.
        """
        pending = self._read_json(self.pending_file)
        tasks = [SubAgentTask.from_dict(t) for t in pending]

        if session_id:
            tasks = [t for t in tasks if t.session_id == session_id]

        return tasks

    def has_live_task(self, agent_type: str, session_id: str,
                      pending_grace_s: int = 120) -> bool:
        """
        Duplicate-delegation check: is a task of this agent_type either RUNNING
        for the session, or CREATED within the last pending_grace_s seconds and
        not yet running (spawn in flight)?

        THE shared guard predicate for every duplicate-launch check (agent.py
        async workflow lane, workflow_executor.py) - a guard that looks only at
        active_tasks.json has a real race window: between create_task (pending)
        and mark_task_running (active, only after the OS spawned the terminal
        and Python finished importing) a second identical launch sees nothing.
        Pending entries count as live only while YOUNG: a stale pending task
        (crashed spawn - only active tasks get reaped by
        cleanup_stale_active_tasks) must never wedge future launches.

        Returns False when session_id is falsy: without a session the check
        would have to run globally and could block on ANOTHER user's run
        (Rule 4.4 - never key session behavior on cross-session state).
        """
        if not session_id or not agent_type:
            return False
        try:
            if any(getattr(t, "agent_type", "") == agent_type
                   for t in self.get_active_tasks(session_id=session_id)):
                return True
            now = datetime.now().timestamp()
            for t in self.get_pending_tasks(session_id=session_id):
                if getattr(t, "agent_type", "") != agent_type:
                    continue
                try:
                    age = now - datetime.fromisoformat(t.created_at).timestamp()
                except (ValueError, TypeError):
                    continue
                if 0 <= age <= pending_grace_s:
                    return True
        except Exception:
            return False
        return False

    def claim_task_slot(self, task_id: str, agent_type: str, session_id: str,
                        pending_grace_s: int = 120) -> bool:
        """
        Post-registration winner check, the second half of the duplicate-launch
        guard. has_live_task alone is check-then-act: two genuinely concurrent
        launches can both see "nothing live" and both register (verified with a
        two-thread repro). So after registering, a launcher calls this to ask
        "am I the winner among ALL live tasks of my type for this session?" -
        winner = the entry with the smallest (created_at, task_id), a total
        order every racer computes identically from the shared registry, so
        exactly one proceeds and every loser deregisters itself and reports a
        duplicate. Rivals in PENDING count only while young (same grace rule
        and reason as has_live_task); the caller's own entry always counts.

        Returns True when task_id may proceed. A missing OWN entry returns
        False (withdraw): so soon after registering, the realistic cause is a
        concurrent racer's read-modify-write clobbering our registry entry
        (lost update) - and "could not secure a slot" must never mean "run
        anyway". Only an EXCEPTION while reading the registry fails open: a
        broken duplicate check degrades to the pre-guard behavior rather than
        blocking legitimate runs.

        Known micro-residual (accepted): the order is decided by created_at
        (stamped just before the lock-serialized registry write), so a racer
        whose timestamp is OLDER but whose write lands AFTER the other racer's
        claim-read could in theory let both proceed. That needs sub-millisecond
        preemption between stamping and writing, on top of losing the
        pre-registration has_live_task check; closing it fully would need a
        cross-lane claim mutex (Windows-safe) around register+verify, which is
        not worth the machinery for a duplicate-run nicety.
        """
        if not task_id or not session_id or not agent_type:
            return True
        try:
            now = datetime.now().timestamp()
            live = []
            for t in self.get_active_tasks(session_id=session_id):
                if getattr(t, "agent_type", "") == agent_type:
                    live.append(t)
            for t in self.get_pending_tasks(session_id=session_id):
                if getattr(t, "agent_type", "") != agent_type:
                    continue
                if getattr(t, "task_id", "") == task_id:
                    live.append(t)  # own entry counts regardless of age
                    continue
                try:
                    age = now - datetime.fromisoformat(t.created_at).timestamp()
                except (ValueError, TypeError):
                    continue
                if 0 <= age <= pending_grace_s:
                    live.append(t)
            if not any(getattr(t, "task_id", "") == task_id for t in live):
                # Own registration not visible. The realistic cause this soon
                # after registering is a LOST UPDATE: create_task /
                # mark_task_running are read-modify-write on shared JSON files,
                # so a concurrent racer's write can clobber ours. Treating this
                # as fail-open let BOTH racers proceed (caught by the
                # concurrency test under full-suite load); "I could not secure
                # a slot" must mean withdrawing. The racer whose entry survived
                # wins; in the pathological case where every entry was lost,
                # nobody runs and the next attempt starts clean - safe, unlike
                # a duplicate GPU run.
                return False
            winner = min(live, key=lambda t: (getattr(t, "created_at", "") or "",
                                              getattr(t, "task_id", "") or ""))
            return getattr(winner, "task_id", "") == task_id
        except Exception:
            return True
    
    def has_pending_results(self) -> bool:
        """Check if there are any pending sub-agent results."""
        results = self._read_json(self.results_file)
        return len(results) > 0
    
    def get_task_status(self, task_id: str) -> Optional[str]:
        """Get the status of a specific task."""
        # Check all queues
        for file in [self.pending_file, self.active_file, self.results_file]:
            tasks = self._read_json(file)
            for task in tasks:
                if task.get('task_id') == task_id:
                    return task.get('status')
        return None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # SUB-AGENT METHODS
    # ═══════════════════════════════════════════════════════════════════════════
    
    def complete_task(self, task_id: str, result: str):
        """
        Mark a task as completed with its result.
        Called by sub-agent when it finishes successfully.
        
        Robust: If task not found in active/pending, creates a synthetic result entry
        to ensure the result is never lost.
        """
        completed_at = datetime.now().isoformat()

        with self._mutation_guard():
            # Retry reading to handle race conditions
            for attempt in range(3):
                try:
                    active = self._read_json(self.active_file)
                    results = self._read_json(self.results_file)
                    break
                except:
                    time.sleep(0.1 * (attempt + 1))
                    active = []
                    results = []

            task_data = None
            for i, task in enumerate(active):
                if task.get('task_id') == task_id:
                    task_data = active.pop(i)
                    break

            # If not in active, check pending (might have started very quickly)
            if not task_data:
                pending = self._read_json(self.pending_file)
                for i, task in enumerate(pending):
                    if task.get('task_id') == task_id:
                        task_data = pending.pop(i)
                        self._write_json(self.pending_file, pending)
                        break

            # If still not found, create synthetic task data to ensure result isn't lost
            if not task_data:
                task_data = {
                    'task_id': task_id,
                    'agent_type': 'unknown',
                    'task_description': 'Task completed but metadata not found',
                    'status': SubAgentStatus.PENDING.value,
                    'created_at': completed_at,
                    'session_id': get_current_session_id()
                }

            # Mark as completed
            task_data['status'] = SubAgentStatus.COMPLETED.value
            task_data['completed_at'] = completed_at
            task_data['result'] = result
            results.append(task_data)

            # Write with retry
            self._write_json(self.active_file, active)
            self._write_json(self.results_file, results)

        # <ipc-notification>: wake the consumer NOW instead of waiting for its poll.
        _push_result_ready(task_id, task_data.get('session_id'))

    def fail_task(self, task_id: str, error: str):
        """
        Mark a task as failed with an error message.
        Called by sub-agent when it encounters an error.
        """
        completed_at = datetime.now().isoformat()
        
        with self._mutation_guard():
            # Find and move task
            active = self._read_json(self.active_file)
            results = self._read_json(self.results_file)

            task_data = None
            for i, task in enumerate(active):
                if task.get('task_id') == task_id:
                    task_data = active.pop(i)
                    break

            if task_data:
                task_data['status'] = SubAgentStatus.FAILED.value
                task_data['completed_at'] = completed_at
                task_data['error'] = error
                results.append(task_data)

                self._write_json(self.active_file, active)
                self._write_json(self.results_file, results)
            else:
                # Also check pending if it failed fast
                pending = self._read_json(self.pending_file)
                for i, task in enumerate(pending):
                    if task.get('task_id') == task_id:
                        task_data = pending.pop(i)
                        self._write_json(self.pending_file, pending)
                        break

                if task_data:
                    task_data['status'] = SubAgentStatus.FAILED.value
                    task_data['completed_at'] = completed_at
                    task_data['error'] = error
                    results.append(task_data)
                    self._write_json(self.results_file, results)

        # <ipc-notification>: wake the consumer NOW (only if a result was recorded).
        if task_data:
            _push_result_ready(task_id, task_data.get('session_id'))

    def update_heartbeat(self, task_id: str):
        """Update the last_heartbeat timestamp for a running task."""
        with self._mutation_guard():
            active = self._read_json(self.active_file)
            updated = False
            for task in active:
                if task.get('task_id') == task_id:
                    task['last_heartbeat'] = datetime.now().isoformat()
                    updated = True
                    break
            if updated:
                self._write_json(self.active_file, active)

    def check_zombies(self, timeout_seconds: int = 20):
        """
        Check for 'zombie' tasks that have stopped reporting heartbeats.
        This detects crashes (window closed, process killed) where no result was written.
        
        Args:
            timeout_seconds: Time without heartbeat before declaring dead.
        """
        with self._mutation_guard():
            self._check_zombies_locked(timeout_seconds)

    def _check_zombies_locked(self, timeout_seconds: int):
        """Body of check_zombies; caller holds the mutation guard."""
        active = self._read_json(self.active_file)
        results = self._read_json(self.results_file)
        pending = self._read_json(self.pending_file)
        
        now = datetime.now()
        zombies_found = False
        
        # Check active tasks
        still_active = []
        for task in active:
            # Use last_heartbeat if available, else created_at
            last_seen_str = task.get('last_heartbeat') or task.get('created_at')
            try:
                last_seen = datetime.fromisoformat(last_seen_str)
                age = (now - last_seen).total_seconds()
                
                if age > timeout_seconds:
                    # It's a zombie!
                    task['status'] = SubAgentStatus.FAILED.value
                    task['completed_at'] = now.isoformat()
                    task['error'] = f"CRASH DETECTED: Sub-agent stopped responding (no heartbeat for {int(age)}s). The terminal likely closed unexpectedly."
                    results.append(task)
                    zombies_found = True
                else:
                    still_active.append(task)
            except:
                still_active.append(task)

        if zombies_found:
            self._write_json(self.active_file, still_active)
            self._write_json(self.results_file, results)

        # Check pending tasks (stuck in pending too long = crash at startup)
        still_pending = []
        pending_zombies = False
        for task in pending:
            created_str = task.get('created_at')
            try:
                created = datetime.fromisoformat(created_str)
                age = (now - created).total_seconds()
                
                if age > timeout_seconds + 10: # Give pending tasks a bit more time to start
                     # It never started
                    task['status'] = SubAgentStatus.FAILED.value
                    task['completed_at'] = now.isoformat()
                    task['error'] = f"STARTUP FAILED: Sub-agent never started (timeout {int(age)}s). Check if 'vaf' command works in new terminal."
                    results.append(task)
                    pending_zombies = True
                else:
                    still_pending.append(task)
            except:
                still_pending.append(task)
                
        if pending_zombies:
            self._write_json(self.pending_file, still_pending)
            self._write_json(self.results_file, results) # Append to results so main agent sees error

    # ═══════════════════════════════════════════════════════════════════════════
    # UTILITY METHODS
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _update_task_status(self, task_id: str, status: str):
        """Update task status in whatever queue it's in."""
        with self._mutation_guard():
            for file in [self.pending_file, self.active_file]:
                tasks = self._read_json(file)
                for task in tasks:
                    if task.get('task_id') == task_id:
                        task['status'] = status
                        self._write_json(file, tasks)
                        return
    
    def cleanup_old_tasks(self, max_age_hours: int = 24):
        """Remove old completed/failed tasks from the results queue."""
        with self._mutation_guard():
            results = self._read_json(self.results_file)
            cutoff = datetime.now().timestamp() - (max_age_hours * 3600)

            filtered = []
            for result in results:
                completed_at = result.get('completed_at')
                if completed_at:
                    try:
                        task_time = datetime.fromisoformat(completed_at).timestamp()
                        if task_time > cutoff:
                            filtered.append(result)
                    except (ValueError, TypeError):
                        filtered.append(result)  # Keep if can't parse
                else:
                    filtered.append(result)

            self._write_json(self.results_file, filtered)
    
    def cleanup_stale_active_tasks(self, max_age_minutes: int = None):
        """
        Remove tasks that have been active for too long (likely crashed).
        
        Uses config settings for timeout:
        - subagent_timeout_enabled: If False, never timeout tasks
        - subagent_timeout_minutes: Timeout duration (default: 120 minutes)
        """
        from vaf.core.config import Config
        
        # Check if timeout is enabled
        timeout_enabled = Config.get("subagent_timeout_enabled", True)
        if not timeout_enabled:
            return  # Timeout disabled - don't clean up anything
        
        # Get timeout from config or parameter
        if max_age_minutes is None:
            max_age_minutes = Config.get("subagent_timeout_minutes", 120)
        
        with self._mutation_guard():
            active = self._read_json(self.active_file)
            results = self._read_json(self.results_file)
            cutoff = datetime.now().timestamp() - (max_age_minutes * 60)

            still_active = []
            for task in active:
                created_at = task.get('created_at')
                if created_at:
                    try:
                        task_time = datetime.fromisoformat(created_at).timestamp()
                        if task_time > cutoff:
                            still_active.append(task)
                        else:
                            # Mark as timed out and move to results
                            task['status'] = SubAgentStatus.TIMEOUT.value
                            task['completed_at'] = datetime.now().isoformat()
                            task['error'] = f"Sub-agent task timed out after {max_age_minutes} minutes"
                            results.append(task)
                    except (ValueError, TypeError):
                        still_active.append(task)
                else:
                    still_active.append(task)

            self._write_json(self.active_file, still_active)
            self._write_json(self.results_file, results)
    
    def clear_all(self):
        """Clear all queues (for testing/debugging)."""
        self._write_json(self.pending_file, [])
        self._write_json(self.active_file, [])
        self._write_json(self.results_file, [])
        self._write_json(self.paused_workflows_file, [])
    
    # ═══════════════════════════════════════════════════════════════════════════
    # PAUSED WORKFLOW METHODS
    # ═══════════════════════════════════════════════════════════════════════════
    
    def pause_workflow(self, workflow: PausedWorkflow):
        """
        Save a paused workflow state.
        Called when a workflow encounters an async sub-agent and needs to yield control.
        """
        with self._mutation_guard():
            paused = self._read_json(self.paused_workflows_file)
            paused.append(workflow.to_dict())
            self._write_json(self.paused_workflows_file, paused)
    
    def get_paused_workflow_for_task(self, task_id: str) -> Optional[PausedWorkflow]:
        """
        Get a paused workflow that's waiting for a specific task.
        Returns None if no workflow is waiting for this task.
        """
        paused = self._read_json(self.paused_workflows_file)
        for wf in paused:
            if wf.get('waiting_for_task_id') == task_id:
                return PausedWorkflow.from_dict(wf)
        return None
    
    def get_all_paused_workflows(self) -> List[PausedWorkflow]:
        """Get all paused workflows (process-global - prefer the session-scoped variant)."""
        paused = self._read_json(self.paused_workflows_file)
        return [PausedWorkflow.from_dict(wf) for wf in paused]

    def get_paused_workflows_for_session(self, session_id: Optional[str]) -> List[PausedWorkflow]:
        """Paused workflows belonging to one session (Rule 4.4: never build user-facing
        state from process-global data). A falsy session_id yields nothing rather than
        everything - failing closed is the safe direction for a cross-user read."""
        if not session_id:
            return []
        paused = self._read_json(self.paused_workflows_file)
        return [
            PausedWorkflow.from_dict(wf) for wf in paused
            if wf.get('session_id') == session_id
        ]

    def claim_paused_workflow(self, workflow_id: str) -> Optional[PausedWorkflow]:
        """Atomically take ownership of a paused workflow: return it AND remove it in one
        locked step, or return None if somebody else got there first.

        Two independent drains can see the same finished sub-agent - the CLI TUI drain
        (cli/cmd/run.py) and the headless/web drain (agent._process_subagent_result). Read
        followed by a separate remove would let both resume the same run, replaying its
        remaining steps twice. This is the ONE claim both must go through.

        Atomic against other threads via _mutation_guard, and against other PROCESSES only
        as far as the file lock reaches (advisory flock on POSIX, a byte-range lock on
        Windows) - the same guarantee the rest of this queue operates under, not a stronger
        one.
        """
        with self._mutation_guard():
            paused = self._read_json(self.paused_workflows_file)
            claimed = None
            remaining = []
            for wf in paused:
                if claimed is None and wf.get('workflow_id') == workflow_id:
                    claimed = wf
                else:
                    remaining.append(wf)
            if claimed is None:
                return None
            self._write_json(self.paused_workflows_file, remaining)
            return PausedWorkflow.from_dict(claimed)


    def remove_paused_workflow(self, workflow_id: str):
        """Remove a paused workflow (after it's resumed or cancelled)."""
        with self._mutation_guard():
            paused = self._read_json(self.paused_workflows_file)
            paused = [wf for wf in paused if wf.get('workflow_id') != workflow_id]
            self._write_json(self.paused_workflows_file, paused)
    
    def has_paused_workflows(self) -> bool:
        """Check if there are any paused workflows."""
        paused = self._read_json(self.paused_workflows_file)
        return len(paused) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL SESSION TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

# Global IPC instance for convenience
_ipc_instance: Optional[SubAgentIPC] = None

# Current session ID - set when a new session starts.
#
# Stored two ways so multiple worker threads (parallel_main_workers > 1) don't clobber each
# other while the single-worker default stays byte-for-byte unchanged:
#   • _session_ctx (ContextVar): per-thread/context override — each worker thread sets and reads
#     its OWN session id, so concurrent sessions never overwrite one another.
#   • _current_session_id (module global): process-wide fallback used only when the ContextVar is
#     unset (e.g. a background helper thread that reads it without one). At one worker the two are
#     always in sync, so behaviour is identical to before.
_current_session_id: Optional[str] = None
_session_ctx: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "vaf_current_session_id", default=None
)


def get_ipc() -> SubAgentIPC:
    """Get the global IPC instance."""
    global _ipc_instance
    if _ipc_instance is None:
        _ipc_instance = SubAgentIPC()
    return _ipc_instance


def set_current_session_id(session_id: str):
    """
    Set the current session ID for sub-agent tracking.
    Should be called when a new session starts (per worker thread).
    """
    global _current_session_id
    _current_session_id = session_id   # process-wide fallback (single-worker / legacy readers)
    try:
        _session_ctx.set(session_id)   # per-thread override (multi-worker safety)
    except Exception:
        pass


def get_current_session_id() -> Optional[str]:
    """Get the current session ID — the per-thread value if set, else the process-wide fallback."""
    try:
        v = _session_ctx.get()
        if v is not None:
            return v
    except Exception:
        pass
    return _current_session_id


def cleanup_other_sessions():
    """
    Clean up sub-agent tasks from previous sessions.
    Should be called when a new session starts.
    
    - Moves active tasks from other sessions to results with status 'stale'
    - Removes completed results from other sessions (old results)
    - Removes pending tasks from other sessions
    """
    ipc = get_ipc()
    current_session = get_current_session_id()

    if not current_session:
        return

    with ipc._mutation_guard():
        _cleanup_other_sessions_locked(ipc, current_session)


def _cleanup_other_sessions_locked(ipc: SubAgentIPC, current_session: str):
    """Body of cleanup_other_sessions; caller holds the mutation guard."""
    # Read active tasks and results
    active = ipc._read_json(ipc.active_file)
    results = ipc._read_json(ipc.results_file)
    
    # Filter active tasks - move old ones to results as 'stale'
    still_active = []
    stale_tasks = []
    for task in active:
        task_session = task.get('session_id')
        
        # Keep tasks from current session
        if task_session == current_session:
            still_active.append(task)
        elif task_session is None:
            # Old tasks without session_id - mark as stale
            task['status'] = SubAgentStatus.TIMEOUT.value
            task['completed_at'] = datetime.now().isoformat()
            task['error'] = "Task from previous session (no session ID)"
            stale_tasks.append(task)
        else:
            # Tasks from other sessions - mark as stale
            task['status'] = SubAgentStatus.TIMEOUT.value
            task['completed_at'] = datetime.now().isoformat()
            task['error'] = f"Task from previous session ({task_session})"
            stale_tasks.append(task)
    
    # Filter results - REMOVE old results from other sessions
    # Only keep results from current session
    current_results = [r for r in results if r.get('session_id') == current_session]
    
    # Add new stale tasks to results
    current_results.extend(stale_tasks)
    
    ipc._write_json(ipc.active_file, still_active)
    ipc._write_json(ipc.results_file, current_results)
    
    # Also clean up pending tasks from other sessions
    pending = ipc._read_json(ipc.pending_file)
    still_pending = [t for t in pending if t.get('session_id') == current_session]
    ipc._write_json(ipc.pending_file, still_pending)
    
    # Clean up paused workflows from other sessions. Paused records now carry a session_id,
    # so this keeps the CURRENT session's runs alive instead of discarding every paused
    # workflow on a session switch (which silently dropped runs that were still waiting for
    # their sub-agent). Legacy records without a session cannot be routed to anyone and are
    # dropped, same rule as the pending/active queues above.
    paused = ipc._read_json(ipc.paused_workflows_file)
    still_paused = [wf for wf in paused if wf.get('session_id') == current_session]
    if len(still_paused) != len(paused):
        ipc._write_json(ipc.paused_workflows_file, still_paused)

