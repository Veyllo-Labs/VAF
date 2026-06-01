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
from dataclasses import dataclass, asdict, field
from enum import Enum

from vaf.core.platform import Platform


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
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PausedWorkflow':
        return cls(**data)


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
        
        # Initialize files if they don't exist
        self._init_queue_files()
    
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
    
    def fail_task(self, task_id: str, error: str):
        """
        Mark a task as failed with an error message.
        Called by sub-agent when it encounters an error.
        """
        completed_at = datetime.now().isoformat()
        
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

    def update_heartbeat(self, task_id: str):
        """Update the last_heartbeat timestamp for a running task."""
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
        for file in [self.pending_file, self.active_file]:
            tasks = self._read_json(file)
            for task in tasks:
                if task.get('task_id') == task_id:
                    task['status'] = status
                    self._write_json(file, tasks)
                    return
    
    def cleanup_old_tasks(self, max_age_hours: int = 24):
        """Remove old completed/failed tasks from the results queue."""
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
        """Get all paused workflows."""
        paused = self._read_json(self.paused_workflows_file)
        return [PausedWorkflow.from_dict(wf) for wf in paused]
    
    def remove_paused_workflow(self, workflow_id: str):
        """Remove a paused workflow (after it's resumed or cancelled)."""
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
    
    # Clean up paused workflows from other sessions
    paused = ipc._read_json(ipc.paused_workflows_file)
    # Paused workflows don't have session_id yet, so we'll add that next
    # For now, clear all paused workflows on new session
    if paused:
        ipc._write_json(ipc.paused_workflows_file, [])

