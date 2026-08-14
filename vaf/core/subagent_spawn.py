# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Spawn ONE sub-agent child process, IPC-tracked - the single implementation.

Five tools carried this block copy-pasted (coder, librarian, document, research,
browser), each ~50 lines, drifting apart in small ways: browser_agent's Windows
branch quoted without escaping inner quotes, research_agent hand-built the
payload lane, and every copy repeated the same failure guard. Per Rule 0b the
fix for the measured Nth copy is the primitive, not the Nth application - this
module is that primitive, and the sixth consumer (the learn job) never gets a
copy of its own.

What stays AT THE CALL SITE, deliberately, as data rather than knobs:
- the two gates (already-inside-a-child via VAF_IN_SUBAGENT_TERMINAL, and the
  `sub_agents_in_separate_terminals` setting) - whether to spawn is the tool's
  decision, this module only knows how;
- agent-specific env (caller scope/role, VAF_TURN_ID, VAF_ALLOWED_TOOLS) via
  `extra_env` - identity crosses the boundary as DATA, never ambient state;
- agent-specific argv (--project-path, --topic ...) via `args`;
- what happens on spawn failure (most tools fall through to in-process
  execution) - this module only guarantees the IPC record is cancelled so the
  runner never waits on a task that was never started.

Contract points:
- The child env is built as a dict for `Platform.open_new_terminal(extra_env=)`,
  NEVER written into the parent's os.environ - concurrent workers would clobber
  each other's session (Rule 4.4).
- On spawn failure the created IPC task is cancelled before returning None;
  a task left pending would count against `has_live_task` duplicate guards and
  make the drain report a zombie that never lived.
- The marker string is the contract the agent loop keys on
  (`[SUBAGENT_ASYNC:<task_id>:<agent_type>]`, see markers.py) - format changes
  break async detection in agent.py.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence


@dataclass(frozen=True)
class SpawnedSubagent:
    """A successfully spawned, IPC-tracked child."""
    task_id: str
    marker: str  # "[SUBAGENT_ASYNC:<task_id>:<agent_type>] <note>" - return this from the tool


def _escape_cmd(cmd_parts: Sequence[str], windows: bool) -> str:
    """One escaping implementation for both platforms.

    The Windows branch quotes any part carrying spaces or quotes and escapes
    inner quotes - browser_agent's private copy quoted without escaping, so a
    task containing a double quote broke the child's argv.
    """
    if windows:
        escaped = []
        for part in cmd_parts:
            part = str(part)
            if " " in part or '"' in part:
                escaped.append('"' + part.replace('"', '\\"') + '"')
            else:
                escaped.append(part)
        return " ".join(escaped)
    import shlex
    return " ".join(shlex.quote(str(part)) for part in cmd_parts)


def spawn_subagent(
    agent_type: str,
    task: str,
    *,
    args: Sequence[str] = (),
    include_task_arg: bool = True,
    payload: Optional[str] = None,
    extra_env: Optional[Mapping[str, str]] = None,
    title: Optional[str] = None,
    marker_note: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Optional[SpawnedSubagent]:
    """Create the IPC task, build the child command and open the terminal.

    Args:
        agent_type: the dispatcher branch in `vaf/cli/cmd/subagent.py` (also the
            IPC record's agent_type and the marker's second field).
        task: human-readable task description; recorded on the IPC task and, when
            `include_task_arg`, passed as `--task` on the child argv.
        args: extra argv appended flag-paired, e.g. ("--project-path", p).
        include_task_arg: False keeps `task` OFF the argv (Windows command-line
            length limits); the child then reads `ipc.get_task_payload(task_id)`.
        payload: overrides the stored task payload when the machine-readable spec
            differs from the human description (e.g. a JSON job spec).
        extra_env: agent-specific child env; caller wins over the base keys.
        title: terminal window title; default "VAF <Agent Type> [<task_id>]".
        marker_note: text after the marker; default names the task's first 80 chars.
        session_id: session for the IPC record; default = the calling context's.

    Returns SpawnedSubagent on success. Returns None when the terminal could not
    be opened - the IPC task is already cancelled then, and the caller decides
    what a failed spawn means (most tools fall through to in-process execution).
    """
    from vaf.cli.ui import UI
    from vaf.core.config import subagent_provider_override
    from vaf.core.platform import Platform
    from vaf.core.subagent_ipc import get_current_session_id, get_ipc

    ipc = get_ipc()
    task_id = ipc.create_task(agent_type, task_description=task, session_id=session_id)
    if payload is not None:
        ipc.store_task_payload(task_id, payload)

    # Child env as DATA, never the parent's os.environ (Rule 4.4: concurrent
    # workers must not clobber each other's session).
    sub_env = {"VAF_TASK_ID": task_id, "VAF_AGENT_TYPE": agent_type}
    effective_session = session_id or get_current_session_id()
    if effective_session:
        sub_env["VAF_SESSION_ID"] = str(effective_session)
    # The ordering ROOM travels beside the session, because a room turn may
    # legitimately run with NO session at all (the runner's room frame binds no
    # chat) - measured live: a sessionless room turn spawned a coder whose
    # entire live feed was dropped at the first session gate. The room is the
    # durable routing anchor for everything this child streams.
    try:
        from vaf.core.subagent_ipc import get_current_room_id
        _room = get_current_room_id()
        if _room:
            sub_env["VAF_ROOM_ID"] = str(_room)
    except Exception:
        pass
    provider = subagent_provider_override()
    if provider:
        sub_env["VAF_PROVIDER"] = provider
    if extra_env:
        sub_env.update({str(k): str(v) for k, v in extra_env.items() if v is not None})

    cmd_parts = [sys.executable, "-m", "vaf.main", "subagent", "run", agent_type]
    if include_task_arg:
        cmd_parts += ["--task", task]
    cmd_parts += [str(a) for a in args]
    cmd_parts += ["--task-id", task_id]

    display = agent_type.replace("_", " ").title()
    cmd = _escape_cmd(cmd_parts, Platform.is_windows())
    term_title = title or f"VAF {display} [{task_id}]"

    if Platform.open_new_terminal(cmd, title=term_title, extra_env=sub_env):
        ipc.mark_task_running(task_id)
        UI.event("Sub-Agent", f"{display} started in new terminal [Task: {task_id}]",
                 style="bold cyan")
        note = marker_note or f"Sub-Agent running in separate terminal. Task: {task[:80]}..."
        return SpawnedSubagent(
            task_id=task_id,
            marker=f"[SUBAGENT_ASYNC:{task_id}:{agent_type}] {note}",
        )

    # Failure guard every copy carried: without the cancel, the runner waits on
    # a task that never started and duplicate guards count a ghost.
    UI.warning("Failed to open new terminal, running in current window")
    ipc.cancel_task(task_id)
    return None
