# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import json
import os
import time
from typing import List, Dict, Optional, Any, Literal
from dataclasses import dataclass, asdict, field
from pathlib import Path

# Types
TaskStatus = Literal["pending", "in_progress", "completed", "failed", "skipped"]


def coerce_task_title(value) -> str:
    """A task title must be a plain string.

    Some model tool calls (and any ``tasks.json`` written before that was enforced)
    put a dict here, e.g. ``{"text": "...", "status": "pending"}``. A dict title then
    crashes every downstream ``title.lower()`` / ``title[:N]`` — and on Python 3.12+,
    where slices became hashable, ``dict[:50]`` raises ``KeyError: slice(None, 50,
    None)`` rather than a TypeError. Extract the description from the common keys;
    fall back to a JSON string, then ``str()``. Applied at the data-model boundary
    (`Task.__post_init__`) so it covers BOTH fresh ``set_todos`` and loading/resuming
    a previously-persisted (possibly poisoned) plan, and self-heals the file on save.
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, dict):
        for k in ("task", "text", "title", "description", "name", "content"):
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                return v
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value)


@dataclass
class Task:
    id: int
    title: str
    status: TaskStatus = "pending"
    description: Optional[str] = None
    result: Optional[str] = None
    files_created: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def __post_init__(self):
        # Guarantee a string title no matter how the Task was built (set_todos,
        # from_dict/resume, or any future caller). See coerce_task_title.
        if not isinstance(self.title, str):
            self.title = coerce_task_title(self.title)

@dataclass
class ProjectState:
    project_name: str
    created_at: float = field(default_factory=time.time)
    tasks: List[Task] = field(default_factory=list)
    current_task_idx: int = 0
    # The user request this plan was made for. Lets a new invocation tell a DIFFERENT request
    # (plan fresh) from a genuine crash-resume of the SAME request (resume the incomplete plan).
    task: str = ""
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'ProjectState':
        tasks_data = data.get("tasks", [])
        tasks = []
        for t_data in tasks_data:
            # Handle backward compatibility or missing fields
            t = Task(
                id=t_data.get("id", 0),
                title=t_data.get("title", ""),
                status=t_data.get("status", "pending"),
                description=t_data.get("description"),
                result=t_data.get("result"),
                files_created=t_data.get("files_created", []),
                created_at=t_data.get("created_at", time.time()),
                completed_at=t_data.get("completed_at")
            )
            tasks.append(t)
            
        return cls(
            project_name=data.get("project_name", "Unknown"),
            created_at=data.get("created_at", time.time()),
            tasks=tasks,
            current_task_idx=data.get("current_task_idx", 0),
            task=data.get("task", "")
        )

class PersistenceManager:
    """
    Manages the persistent state of the coding agent.
    Handles storage of tasks (JSON) and knowledge (Markdown).
    """
    
    VAF_DIR = ".vaf"
    TASKS_FILE = "tasks.json"
    CODEX_FILE = "codex.md"  # Long-term patterns
    MEMORY_FILE = "memory.md"  # Short-term session memory
    
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.vaf_path = os.path.join(base_dir, self.VAF_DIR)
        self.tasks_path = os.path.join(self.vaf_path, self.TASKS_FILE)
        self.codex_path = os.path.join(self.vaf_path, self.CODEX_FILE)
        self.memory_path = os.path.join(self.vaf_path, self.MEMORY_FILE)
        
        # Ensure .vaf directory exists
        if not os.path.exists(self.vaf_path):
            try:
                os.makedirs(self.vaf_path, exist_ok=True)
            except Exception as e:
                print(f"[PersistenceManager] WARNING: Could not create .vaf dir at {self.vaf_path}: {e}")

    def init_project(self, project_name: str):
        """Initialize a new project state if not exists."""
        if not os.path.exists(self.tasks_path):
            state = ProjectState(project_name=project_name)
            self.save_state(state)
            
            # Init empty memory files with headers
            if not os.path.exists(self.codex_path):
                self._write_file(self.codex_path, "# Project Codex\n\nPersistent patterns, conventions, and architectural decisions.\n\n")
            
            if not os.path.exists(self.memory_path):
                self._write_file(self.memory_path, "# Session Memory\n\nShort-term learnings and scratchpad for the current workflow.\n\n")

    def load_state(self) -> Optional[ProjectState]:
        """Load the project state from tasks.json."""
        if not os.path.exists(self.tasks_path):
            return None
        
        try:
            with open(self.tasks_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return ProjectState.from_dict(data)
        except Exception as e:
            print(f"Error loading state: {e}")
            return None

    def save_state(self, state: ProjectState):
        """Save the project state to tasks.json."""
        try:
            # Ensure .vaf dir exists in case it was missing at init time
            os.makedirs(self.vaf_path, exist_ok=True)
            with open(self.tasks_path, 'w', encoding='utf-8') as f:
                json.dump(state.to_dict(), f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[PersistenceManager] ERROR saving state to {self.tasks_path}: {e}")

    def get_codex(self) -> str:
        """Read the Codex (Long-term memory)."""
        return self._read_file(self.codex_path)

    def append_codex(self, content: str):
        """Append a new pattern to the Codex."""
        timestamp = time.strftime("%Y-%m-%d %H:%M")
        entry = f"\n\n## Entry [{timestamp}]\n{content}"
        self._append_file(self.codex_path, entry)

    def get_memory(self) -> str:
        """Read the Session Memory (Short-term)."""
        return self._read_file(self.memory_path)

    def append_memory(self, content: str):
        """Append to Session Memory."""
        timestamp = time.strftime("%H:%M:%S")
        entry = f"\n- [{timestamp}] {content}"
        self._append_file(self.memory_path, entry)
        
    def clear_memory(self):
        """Clear Session Memory (e.g. after major milestone)."""
        self._write_file(self.memory_path, "# Session Memory\n\nShort-term learnings and scratchpad.\n\n")

    # Helpers
    def _read_file(self, path: str) -> str:
        if not os.path.exists(path):
            return ""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception:
            return ""

    def _write_file(self, path: str, content: str):
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
        except Exception:
            pass

    def _append_file(self, path: str, content: str):
        try:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(content)
        except Exception:
            pass
