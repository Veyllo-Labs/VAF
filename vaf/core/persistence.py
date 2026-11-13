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

    The files live in the project's own ``.vaf`` folder, and that is the only folder they are
    read from or written to. Code in a sandbox environment (the coder's jailed ``bash``,
    ``sandbox_exec`` on a mounted project) can put links in a project, which the host sees as
    ordinary entries: a ``.vaf/memory.md`` linked to ``~/.bashrc``, a ``.vaf/codex.md``
    linked to another account's file, or ``.vaf`` itself linked to another project's. So a
    ``.vaf`` that is a link is not used at all (`notes_folder` answers None), every write
    opens through `vaf.core.path_jail.open_for_write` pinned to the real path of that folder
    (a link as the last part, or a folder swapped for a link later, is refused), and a read
    opens the file relative to that folder's checked handle, never through a link. A caller that decides
    who may write the project asks its rule for `notes_folder` (the git and notes tools ask
    the caller's file jail). NAMED BOUNDARY: a hard link is a second name of the same file and
    is not seen; a sandbox environment cannot make one to a file outside its mount.
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
        
        self._folder: Optional[str] = None
        self._folder_asked = False
        # Ensure .vaf directory exists (never through a link: lexists sees one, dangling too)
        if not os.path.lexists(self.vaf_path):
            try:
                os.makedirs(self.vaf_path, exist_ok=True)
            except Exception as e:
                print(f"[PersistenceManager] WARNING: Could not create .vaf dir at {self.vaf_path}: {e}")

    def notes_folder(self) -> Optional[str]:
        """The real path of this project's ``.vaf`` folder, or None when it is a link (or a
        junction), not a folder, or cannot be made. Asked once; every read and write of this
        manager is pinned to that answer. A folder deleted meanwhile (a clean-up, the run's
        own shell) is made anew at that same real path, never resolved again: the project
        folder may have been swapped for a link since, and the answer the caller's rule
        judged must stay the folder that is written."""
        if self._folder is not None and not os.path.lexists(self._folder):
            self._folder = self._recreate(self._folder)
        if not self._folder_asked:
            self._folder_asked = True
            self._folder = self._own_folder()
        return self._folder

    @staticmethod
    def _recreate(folder: str) -> Optional[str]:
        """``folder`` (a real path) made again, or None when its parent is no longer that real
        folder (a link on the way) or it cannot be made."""
        parent = os.path.dirname(folder)
        try:
            if os.path.normcase(os.path.realpath(parent)) != os.path.normcase(parent):
                return None
            try:
                os.mkdir(folder)
            except FileExistsError:
                pass
            if (os.path.islink(folder)
                    or os.path.normcase(os.path.realpath(folder)) != os.path.normcase(folder)):
                return None
            return folder
        except (OSError, ValueError):
            return None

    def _own_folder(self) -> Optional[str]:
        try:
            if os.path.islink(self.vaf_path):
                return None
            os.makedirs(self.vaf_path, exist_ok=True)
            expected = os.path.join(os.path.realpath(self.base_dir), self.VAF_DIR)
            # A junction is no link to islink; resolved, it lands somewhere else.
            if os.path.normcase(os.path.realpath(self.vaf_path)) != os.path.normcase(expected):
                return None
            return expected
        except (OSError, ValueError):
            return None

    def _open_write(self, path: str, mode: str):
        from vaf.core.path_jail import WriteRefused, open_for_write
        folder = self.notes_folder()
        if folder is None:
            raise WriteRefused(f"refusing to write {path}: the project's .vaf is a link or "
                               "not a folder")
        return open_for_write(os.path.join(folder, os.path.basename(path)), mode,
                              encoding="utf-8", expect_folder=folder)

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
            data = json.loads(self._read_file(self.tasks_path) or "null")
            if not isinstance(data, dict):
                return None
            return ProjectState.from_dict(data)
        except Exception as e:
            print(f"Error loading state: {e}")
            return None

    def save_state(self, state: ProjectState):
        """Save the project state to tasks.json."""
        try:
            with self._open_write(self.tasks_path, "w") as f:
                json.dump(state.to_dict(), f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[PersistenceManager] ERROR saving state to {self.tasks_path}: {e}")

    def get_codex(self) -> str:
        """Read the Codex (Long-term memory)."""
        return self._read_file(self.codex_path)

    def append_codex(self, content: str):
        """Append a new pattern to the Codex. Raises `WriteRefused` (an OSError) when the
        file or its folder is a link, so the caller does not report a write that did not
        happen."""
        timestamp = time.strftime("%Y-%m-%d %H:%M")
        entry = f"\n\n## Entry [{timestamp}]\n{content}"
        self._append_file(self.codex_path, entry)

    def get_memory(self) -> str:
        """Read the Session Memory (Short-term)."""
        return self._read_file(self.memory_path)

    def append_memory(self, content: str):
        """Append to Session Memory. Raises like `append_codex`."""
        timestamp = time.strftime("%H:%M:%S")
        entry = f"\n- [{timestamp}] {content}"
        self._append_file(self.memory_path, entry)
        
    def clear_memory(self):
        """Clear Session Memory (e.g. after major milestone)."""
        self._write_file(self.memory_path, "# Session Memory\n\nShort-term learnings and scratchpad.\n\n")

    # Helpers
    def _read_file(self, path: str) -> str:
        """The file's text, or "" when it is missing, a link, not a regular file, or not in
        this project's own .vaf folder. The coder reads codex and memory into its prompt, so
        a link to another account's file would carry that file into the run.

        The folder is opened first (``O_DIRECTORY | O_NOFOLLOW``), its handle's kernel path
        compared with `notes_folder` (the same handle check `open_for_write` makes,
        `vaf.core.path_jail._fd_path`), and the file opened relative to that handle with
        ``O_NOFOLLOW``: a ``.vaf`` swapped for a link and back around the open cannot leave
        the read on a file outside. NAMED BOUNDARY, Windows (no folder handles, no
        ``O_NOFOLLOW``): a link at the name is refused and the folder's resolved path is
        compared after the open, so a folder link that is in place only while the file is
        opened is not seen there."""
        folder = self.notes_folder()
        if folder is None:
            return ""
        name = os.path.basename(path)
        from vaf.core import path_jail
        cloexec = getattr(os, "O_CLOEXEC", 0)
        fd = -1
        try:
            if path_jail._DIR_FD:
                dfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | cloexec)
                try:
                    if os.path.normcase(path_jail._fd_path(dfd, folder)) != os.path.normcase(folder):
                        return ""
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | cloexec,
                                 dir_fd=dfd)
                finally:
                    os.close(dfd)
            else:
                full = os.path.join(folder, name)
                if os.path.islink(full) or not os.path.isfile(full):
                    return ""
                fd = os.open(full, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                real = os.path.dirname(os.path.realpath(full))
                if os.path.normcase(real) != os.path.normcase(folder):
                    return ""
            import stat as _stat
            if not _stat.S_ISREG(os.fstat(fd).st_mode):
                return ""
            f = os.fdopen(fd, "r", encoding="utf-8")
            fd = -1
            with f:
                return f.read()
        except (OSError, ValueError):
            return ""
        finally:
            if fd >= 0:
                os.close(fd)

    def _write_file(self, path: str, content: str):
        try:
            with self._open_write(path, "w") as f:
                f.write(content)
        except Exception:
            pass

    def _append_file(self, path: str, content: str):
        with self._open_write(path, "a") as f:
            f.write(content)
