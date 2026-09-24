# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Background host processes: started detached, read, written to, stopped - and a wake turn
in the chat that started them when they end.

A long command blocked the agent's whole turn: a local test server, an upload, a build that
outlives any sensible wait. This is a shell's ``&`` for the agent. ``start()`` runs the
command and returns at once; the output goes to a private log; the agent reads the log,
types into the process (a server console), stops it; and when the process exits on its own,
the chat it came from gets a new turn through the same wake lane a fired timer uses
(``task_queue.enqueue_wake_turn``), with the person's identity.

Boundaries, each deliberate:

- **One chat, one person.** A process is visible only to the (user scope, session) that
  started it; every lookup takes both, and a foreign id reads as unknown.
- **A private log.** ``<config dir>/processes/<session>/<id>.log``: the directory is
  owner-only, the file 0600, and neither sits in the project folder a person browses. The
  logs of this run are removed when VAF exits.
- **A cap.** At most ``MAX_PER_CHAT`` running processes per chat.
- **They end with VAF.** ``terminate_all()`` runs at exit (an atexit hook, and the tray's
  quit calls it before its hard exit). A crash that kills VAF without either leaves them
  running; the registry is in memory and cannot know them after a restart.
- **Not where nobody can be woken.** The wake turn reaches the chat through the process
  that started the command, so a messaging-channel session (no wake delivery there) and a
  sub-agent child process (it exits long before the command) are refused by the tool.

The process tree is stopped with ``Platform.terminate_process_tree``, the same code that
stops sub-agent children: a command run through a shell is only the child of that shell.
"""
from __future__ import annotations

import atexit
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_PER_CHAT = 8

# How much of the log's end a wake turn carries. The agent reads more with the log action.
WAKE_TAIL_CHARS = 1500


class ProcessRefused(Exception):
    """A start that this module will not perform; the message says why, for the model."""


@dataclass
class BackgroundProcess:
    id: str
    command: str
    session_id: str
    user_scope_id: Any
    username: Optional[str]
    role: Optional[str]
    source: str
    log_path: Path
    started_at: float = field(default_factory=time.time)
    exit_code: Optional[int] = None
    finished_at: Optional[float] = None
    stopped_by_agent: bool = False
    popen: Any = None
    # The process group the command leads (POSIX, start_new_session: the pid). Recorded
    # at start, so a group whose leader has exited - a detached server left behind by the
    # shell - can still be stopped. After the leader has exited only the GROUP is ever
    # signalled, never the pid, which may belong to an unrelated process by then.
    pgid: Optional[int] = None

    @property
    def running(self) -> bool:
        return self.exit_code is None

    def describe(self) -> str:
        took = (self.finished_at or time.time()) - self.started_at
        state = "running" if self.running else (
            "stopped" if self.stopped_by_agent else f"exited with code {self.exit_code}")
        return f"{self.id}: {state}, {int(took)} s - {self.command[:120]}"


_lock = threading.Lock()
_registry: Dict[str, BackgroundProcess] = {}
_atexit_registered = False


def _owner_key(user_scope_id: Any, session_id: Optional[str]) -> tuple:
    from vaf.core.trust import _scope_key
    return (_scope_key(user_scope_id), str(session_id or "").strip())


def _log_dir(session_id: str) -> Path:
    from vaf.core.path_jail import safe_entry_name
    from vaf.core.platform import Platform
    from vaf.core.secure_store import harden_dir
    root = Platform.config_dir() / "processes"
    folder = root / safe_entry_name(session_id)
    folder.mkdir(parents=True, exist_ok=True)
    harden_dir(root)
    harden_dir(folder)
    return folder


def _open_private(path: Path):
    """The log file, created owner-only before anything is written into it."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(fd, "wb")


def start(command: str, *, session_id: str, user_scope_id: Any = None,
          username: Optional[str] = None, role: Optional[str] = None,
          source: str = "web", cwd: Optional[str] = None) -> BackgroundProcess:
    """Start ``command`` detached and return its record. Raises ProcessRefused."""
    global _atexit_registered
    command = str(command or "").strip()
    session_id = str(session_id or "").strip()
    if not command:
        raise ProcessRefused("no command given")
    if not session_id:
        raise ProcessRefused("a background command needs a chat to report back to, "
                             "and this call has none")
    owner = _owner_key(user_scope_id, session_id)
    with _lock:
        running = [p for p in _registry.values()
                   if _owner_key(p.user_scope_id, p.session_id) == owner and p.running]
        if len(running) >= MAX_PER_CHAT:
            raise ProcessRefused(
                f"this chat already runs {len(running)} background commands (the limit is "
                f"{MAX_PER_CHAT}); stop one first:\n" + "\n".join(p.describe() for p in running))

    proc_id = "p-" + uuid.uuid4().hex[:8]
    try:
        log_path = _log_dir(session_id) / f"{proc_id}.log"
        log_file = _open_private(log_path)
    except Exception as e:
        raise ProcessRefused(f"no private log could be created for it: {e}") from None
    kwargs: Dict[str, Any] = {
        "shell": True, "stdin": subprocess.PIPE, "stdout": log_file,
        "stderr": subprocess.STDOUT, "cwd": cwd or None,
        "env": {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
    }
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    try:
        popen = subprocess.Popen(command, **kwargs)
    except Exception as e:
        raise ProcessRefused(f"the command could not be started: {e}") from None
    finally:
        # The child holds its own handle now; ours is not needed after the spawn.
        try:
            log_file.close()
        except Exception:
            pass

    record = BackgroundProcess(
        id=proc_id, command=command, session_id=session_id, user_scope_id=user_scope_id,
        username=username, role=role, source=source or "web", log_path=log_path, popen=popen,
        pgid=None if os.name == "nt" else popen.pid,
    )
    with _lock:
        _registry[proc_id] = record
        if not _atexit_registered:
            atexit.register(terminate_all)
            _atexit_registered = True
    threading.Thread(target=_watch, args=(record,), name=f"vaf-process-{proc_id}",
                     daemon=True).start()
    return record


def _watch(record: BackgroundProcess) -> None:
    """Wait for the process; on its own exit, wake the chat it belongs to."""
    try:
        code = record.popen.wait()
    except Exception:
        code = -1
    with _lock:
        record.exit_code = code
        record.finished_at = time.time()
        woken_by_agent = record.stopped_by_agent
    try:
        if record.popen.stdin:
            record.popen.stdin.close()
    except Exception:
        pass
    if woken_by_agent:
        return   # the agent stopped it and already knows
    try:
        from vaf.core.task_queue import enqueue_wake_turn
        enqueue_wake_turn(
            kind="process", session_id=record.session_id, text=wake_text(record),
            source=record.source, user_scope_id=record.user_scope_id,
            username=record.username, role=record.role,
            extra={"process_id": record.id},
        )
    except Exception:
        pass


def wake_text(record: BackgroundProcess) -> str:
    """What the agent reads when its background command has ended."""
    took = int((record.finished_at or time.time()) - record.started_at)
    tail = read_tail(record, max_chars=WAKE_TAIL_CHARS).strip() or "(no output)"
    return (
        f"⚙ Background command finished: {record.command[:160]} ({record.id}, exit "
        f"{record.exit_code}, after {took} s).\n"
        f"End of its output:\n{tail}\n\n"
        f"Continue with what it was started for, or tell the user how it went. "
        f"host_process(action=\"log\", id=\"{record.id}\") shows more of the output."
    )


def get(proc_id: str, *, session_id: Optional[str], user_scope_id: Any = None
        ) -> Optional[BackgroundProcess]:
    """The record, for its own chat and person only; None for anything else."""
    with _lock:
        record = _registry.get(str(proc_id or "").strip())
    if record is None:
        return None
    if _owner_key(record.user_scope_id, record.session_id) != _owner_key(user_scope_id, session_id):
        return None
    return record


def list_for(*, session_id: Optional[str], user_scope_id: Any = None) -> List[BackgroundProcess]:
    owner = _owner_key(user_scope_id, session_id)
    with _lock:
        return [p for p in _registry.values()
                if _owner_key(p.user_scope_id, p.session_id) == owner]


def read_tail(record: BackgroundProcess, *, max_chars: int = 4000) -> str:
    """The end of the log. A long log is read from its end, never loaded whole."""
    try:
        size = record.log_path.stat().st_size
        with open(record.log_path, "rb") as fh:
            fh.seek(max(0, size - max_chars))
            data = fh.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > max_chars:
        cut = text.find("\n")
        text = f"... ({size - len(data)} earlier bytes not shown)\n" + (
            text[cut + 1:] if 0 <= cut < len(text) - 1 else text)
    return text


def write(record: BackgroundProcess, text: str) -> str:
    """Send one line to the process's standard input (a server console, a prompt)."""
    if not record.running:
        return f"{record.id} has already ended (exit {record.exit_code})."
    line = str(text or "")
    if not line.endswith("\n"):
        line += "\n"
    try:
        record.popen.stdin.write(line.encode("utf-8"))
        record.popen.stdin.flush()
    except Exception as e:
        return f"{record.id} did not accept the input: {e}"
    return f"sent to {record.id}: {line.strip()[:200]}"


def stop(record: BackgroundProcess) -> str:
    from vaf.core.platform import Platform
    if not record.running:
        # The command itself has ended; what it detached into its group may not have.
        try:
            Platform.terminate_process_group(record.pgid, grace=1.0)
        except Exception:
            pass
        return (f"{record.id} had already ended (exit {record.exit_code}); anything it left "
                f"running in its process group is stopped now.")
    with _lock:
        record.stopped_by_agent = True
    try:
        Platform.terminate_process_tree(record.popen.pid, grace=3.0, pgid=record.pgid)
    except Exception as e:
        return f"{record.id} could not be stopped: {e}"
    try:
        record.popen.wait(timeout=5)
    except Exception:
        pass
    return f"stopped {record.id}"


def terminate_all() -> int:
    """Stop every background process of this VAF and remove this run's logs."""
    with _lock:
        records = list(_registry.values())
        _registry.clear()
    from vaf.core.platform import Platform
    stopped = 0
    for record in records:
        was_running = record.running
        record.stopped_by_agent = True
        try:
            if was_running:
                Platform.terminate_process_tree(record.popen.pid, grace=2.0, pgid=record.pgid)
                stopped += 1
            else:
                # The shell has ended; its group may still hold what it detached (a server
                # started with "&"). The group only - the pid may not be ours any more.
                Platform.terminate_process_group(record.pgid, grace=1.0)
        except Exception:
            pass
        try:
            record.log_path.unlink()
        except OSError:
            pass
    return stopped
