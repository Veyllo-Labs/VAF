# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import json
import os
import time
from typing import List, Dict, Optional, Any, Literal
from dataclasses import dataclass, asdict, field
from pathlib import Path

# Whether a folder can be made relative to a folder handle (mkdir with dir_fd): everywhere but
# Windows. Read once, so that what the notes folder is made with does not depend on a later
# rebinding of os.mkdir.
_MKDIR_DIR_FD = os.mkdir in os.supports_dir_fd

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
    # What the task's context must carry beyond its title, for a task made by the run itself
    # (a code audit fix task's findings). Read into every fresh context of the task, so a
    # resumed or retried run still knows what to fix. `description` cannot hold it: a retry
    # overwrites it with the previous attempt's reason.
    instructions: Optional[str] = None

    def __post_init__(self):
        # Guarantee a string title no matter how the Task was built (set_todos,
        # from_dict/resume, or any future caller). See coerce_task_title.
        if not isinstance(self.title, str):
            self.title = coerce_task_title(self.title)
        # The texts the coder slices into its prompts and answers: a planted dict there
        # raised KeyError on `[:200]`.
        for name in ("description", "result", "instructions"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                setattr(self, name, coerce_task_title(value))

@dataclass
class ProjectState:
    project_name: str
    created_at: float = field(default_factory=time.time)
    tasks: List[Task] = field(default_factory=list)
    current_task_idx: int = 0
    # The user request this plan was made for. Lets a new invocation tell a DIFFERENT request
    # (plan fresh) from a genuine crash-resume of the SAME request (resume the incomplete plan).
    task: str = ""

    def __post_init__(self):
        # The stored request is read into every prompt of a resumed run and stripped on every
        # load: a planted list there raised AttributeError out of the run.
        if not isinstance(self.task, str):
            self.task = coerce_task_title(self.task)

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
                completed_at=t_data.get("completed_at"),
                instructions=t_data.get("instructions"),
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
    is not seen; a sandbox environment cannot make one to a file outside its mount. A missing
    ``.vaf`` (and a missing project folder) is made relative to the checked handle of the
    folder above it (`vaf.core.path_jail.open_folder`, ``mkdir`` with ``dir_fd``), never by
    path: a project folder swapped for a link at that moment left an empty ``.vaf`` in the
    link's target in 6 of 1076 fresh managers when it was made by path. NAMED BOUNDARY,
    Windows (no folder handles): there it is still made by path and resolved afterwards, so
    that empty folder can appear; nothing is written into it, because every write is pinned.
    """
    
    VAF_DIR = ".vaf"
    TASKS_FILE = "tasks.json"
    CODEX_FILE = "codex.md"  # Long-term patterns
    MEMORY_FILE = "memory.md"  # Short-term session memory
    
    def __init__(self, base_dir: str, *, expect_folder=None):
        """``expect_folder``: the REAL path a caller judged for ``base_dir`` (no links on the
        way, compared as given and never resolved). The project folder is opened as a handle
        and its kernel path compared with it, so a folder swapped for a link after the check
        is not used and gets no ``.vaf``. Without it the folder ``base_dir`` names when it is
        opened is the one used."""
        self.base_dir = base_dir
        self.vaf_path = os.path.join(base_dir, self.VAF_DIR)
        self.tasks_path = os.path.join(self.vaf_path, self.TASKS_FILE)
        self.codex_path = os.path.join(self.vaf_path, self.CODEX_FILE)
        self.memory_path = os.path.join(self.vaf_path, self.MEMORY_FILE)
        self._expect = expect_folder

        self._folder: Optional[str] = None
        self._folder_asked = False
        # The .vaf folder is made right away, through the checked folder handle.
        self.notes_folder()

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
    def _by_handle() -> bool:
        from vaf.core import path_jail
        return path_jail._DIR_FD and _MKDIR_DIR_FD

    def _open_base(self):
        """``(fd, real)`` of the project folder; folders missing at its end are made, each
        relative to the handle of the one above. With ``expect_folder`` the existing folder
        must have the expected real path."""
        from vaf.core.path_jail import PathEscape, open_folder
        head = os.path.abspath(self.base_dir)
        expect = os.path.abspath(self._expect) if self._expect is not None else None
        missing = []
        while not os.path.lexists(head):
            head, tail = os.path.split(head)
            if not tail:
                break
            if expect is not None:
                if os.path.basename(expect) != tail:
                    raise PathEscape(f"{self.base_dir} is not {self._expect}")
                expect = os.path.dirname(expect)
            missing.append(tail)
        fd, real = open_folder(head, expect_folder=expect)
        try:
            for name in reversed(missing):
                child, child_real = open_folder(name, dir_fd=fd, follow=False, create=True,
                                                expect_folder=os.path.join(real, name))
                os.close(fd)
                fd, real = child, child_real
            return fd, real
        except BaseException:
            os.close(fd)
            raise

    def _recreate(self, folder: str) -> Optional[str]:
        """``folder`` (a real path) made again, or None when its parent is no longer that real
        folder (a link on the way) or it cannot be made. Made relative to the parent's
        checked handle, so a parent swapped for a link at that moment gets nothing."""
        parent = os.path.dirname(folder)
        try:
            if not self._by_handle():
                return self._recreate_by_path(folder)
            from vaf.core.path_jail import open_folder
            pfd, real = open_folder(parent, expect_folder=parent)
            try:
                name = os.path.basename(folder)
                vfd, _ = open_folder(name, dir_fd=pfd, follow=False, create=True,
                                     expect_folder=os.path.join(real, name))
                os.close(vfd)
            finally:
                os.close(pfd)
            return folder
        except (OSError, ValueError):
            return None

    def _own_folder(self) -> Optional[str]:
        if not self._by_handle():
            return self._own_folder_by_path()
        try:
            from vaf.core.path_jail import open_folder
            bfd, base_real = self._open_base()
            try:
                vfd, real = open_folder(self.VAF_DIR, dir_fd=bfd, follow=False, create=True,
                                        expect_folder=os.path.join(base_real, self.VAF_DIR))
                os.close(vfd)
                return real
            finally:
                os.close(bfd)
        except (OSError, ValueError):
            return None

    # NAMED BOUNDARY, Windows (no folder handles): the two methods below make the folders by
    # path and resolve them afterwards, so a project folder swapped for a link at that moment
    # can get an empty .vaf; nothing is written into it, because every write is pinned to the
    # real path judged here (`open_for_write`'s expect_folder).

    @staticmethod
    def _recreate_by_path(folder: str) -> Optional[str]:
        parent = os.path.dirname(folder)
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

    def _own_folder_by_path(self) -> Optional[str]:
        try:
            if os.path.islink(self.vaf_path):
                return None
            if self._expect is not None and (os.path.normcase(os.path.realpath(self.base_dir))
                                             != os.path.normcase(os.path.abspath(self._expect))):
                return None
            os.makedirs(self.vaf_path, exist_ok=True)
            # With expect_folder, the judged folder, never the project folder resolved anew: a
            # folder swapped for a link during makedirs would resolve into the link's target.
            base = (os.path.abspath(self._expect) if self._expect is not None
                    else os.path.realpath(self.base_dir))
            expected = os.path.join(base, self.VAF_DIR)
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

    @classmethod
    def peek_state(cls, base_dir: str) -> Optional[ProjectState]:
        """The state stored in ``base_dir`` (a judged REAL path), read without making or
        writing anything, or None. Constructing a manager makes ``.vaf`` at once; a caller
        that only asks whether a plan is stored (a resume that may be refused) must leave the
        folder as it found it. A ``.vaf`` that is a link, or a ``tasks.json`` that is one, is
        not read (`open_for_read` with ``expect_folder``, as `_read_file` reads)."""
        from vaf.core.path_jail import open_for_read
        folder = os.path.join(base_dir, cls.VAF_DIR)
        try:
            with open_for_read(os.path.join(folder, cls.TASKS_FILE), expect_folder=folder) as f:
                data = json.loads(f.read().decode("utf-8") or "null")
            return ProjectState.from_dict(data) if isinstance(data, dict) else None
        except Exception:
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

        Read through `vaf.core.path_jail.open_for_read` with ``expect_folder`` the judged
        `notes_folder`: the folder is opened first and its handle's kernel path compared, and
        the file is opened relative to that handle with ``O_NOFOLLOW``, so a ``.vaf`` swapped
        for a link and back around the open cannot leave the read on a file outside. The
        Windows branch (no folder handles) and its named boundary are in that docstring."""
        folder = self.notes_folder()
        if folder is None:
            return ""
        import io
        from vaf.core.path_jail import open_for_read
        try:
            with open_for_read(os.path.join(folder, os.path.basename(path)),
                               expect_folder=folder) as f:
                return io.TextIOWrapper(f, encoding="utf-8").read()
        except (OSError, ValueError):
            return ""

    def _write_file(self, path: str, content: str):
        try:
            with self._open_write(path, "w") as f:
                f.write(content)
        except Exception:
            pass

    def _append_file(self, path: str, content: str):
        with self._open_write(path, "a") as f:
            f.write(content)
