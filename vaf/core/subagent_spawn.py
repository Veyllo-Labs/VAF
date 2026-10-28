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
  break async detection in agent.py. It is honoured only for a task THIS module
  started inside the caller's open `spawn_ledger`, that the queue still holds, for
  the calling session (`delegated_task`). The same text anywhere else - a tool name
  the model chose, a fetched page, a file, a message, the marker of a task some
  other lane started - is text: no acknowledgement, no registration, no pause.
"""
from __future__ import annotations

import contextvars
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence, Tuple

# The one reader of the marker's shape. Every consumer goes through delegated_task
# (guarded by tests/test_subagent_spawn.py).
_MARKER_RE = re.compile(r"\[SUBAGENT_ASYNC:([^:]+):([^\]]+)\]")
_MARKER_NEEDLE = "[SUBAGENT_ASYNC:"

# The tasks spawned inside the innermost open spawn_ledger of this context. A worker thread
# that runs a tool under a copied context (run_bounded) sees the same set object, so a spawn
# deep inside a tool lands in the ledger of the call that dispatched it.
_spawn_ledger_ctx: "contextvars.ContextVar[Optional[set]]" = contextvars.ContextVar(
    "vaf_spawn_ledger", default=None)


class SpawnRefused(Exception):
    """`exclusive=True` and another live run of the same agent type already holds this chat's
    slot (`claim_task_slot` lost). Nothing was started and the IPC task is cancelled. Not a
    failed spawn - the caller must NOT fall back to running the work itself, or the duplicate
    the guard exists to stop runs anyway."""


@dataclass(frozen=True)
class SpawnedSubagent:
    """A successfully spawned, IPC-tracked child."""
    task_id: str
    marker: str  # "[SUBAGENT_ASYNC:<task_id>:<agent_type>] <note>" - return this from the tool


@contextmanager
def spawn_ledger():
    """Collect the task ids `spawn_subagent` starts inside this block; yields the set.

    Every block starts EMPTY, nested ones included, and hands its ids up to the enclosing
    ledger when it ends. A workflow run inside a chat call therefore pauses only on a task
    one of its own steps started - never on one the call started before it - while the call
    still recognises the marker the run hands back. The set stays readable after the block.
    """
    parent = _spawn_ledger_ctx.get()
    ledger: set = set()
    token = _spawn_ledger_ctx.set(ledger)
    try:
        yield ledger
    finally:
        _spawn_ledger_ctx.reset(token)
        if parent is not None:
            parent.update(ledger)


def _record_spawn(task_id: str) -> None:
    ledger = _spawn_ledger_ctx.get()
    if ledger is not None:
        ledger.add(str(task_id))


def delegated_task(text, *, spawned: Optional[Iterable[str]] = None) -> Optional[Tuple[str, str]]:
    """The sub-agent task a tool result really delegated to: ``(task_id, agent_type)``, or None.

    The marker's text is not the proof, because any text can carry it: a tool name the model
    chose (refusals echo it), a fetched page, a file, a contact's message. A marker counts
    only when all three hold:

    - ``spawned`` holds its id: this call started that task (`spawn_ledger`). Default: the
      ledger open in this context; none open means nothing was spawned. Without this, the
      marker of a task another lane started - an id that is in the queue and names no chat -
      would be registered as this lane's own, and the lane then takes that task's result
      (`get_pending_results_for(own_task_ids=...)`);
    - the queue holds the task (`SubAgentIPC.find_task`);
    - when the record and this context both name a session, it is the same one
      (`get_current_session_id`, the resolver `create_task` used).

    ``agent_type`` is the queue's, not the text's. The queue is read only when the text
    carries the marker. Never raises: a queue that cannot be read answers None, because this
    is a guard; the miss is logged as ``[ASYNC-MARKER]``.
    """
    text = str(text or "")
    if _MARKER_NEEDLE not in text:
        return None
    own = _spawn_ledger_ctx.get() if spawned is None else spawned
    own = {str(t) for t in (own or ())}
    try:
        from vaf.core.subagent_ipc import get_current_session_id, get_ipc
        for m in _MARKER_RE.finditer(text):
            task_id = m.group(1)
            if task_id not in own:
                continue
            record = get_ipc().find_task(task_id)
            if record is None:
                continue
            here = get_current_session_id()
            if record.session_id and here and str(record.session_id) != str(here):
                continue
            return (record.task_id, record.agent_type or m.group(2))
    except Exception as exc:                                    # noqa: BLE001
        _log(f"[ASYNC-MARKER] queue unreadable, marker not honoured: {type(exc).__name__}")
        return None
    _log("[ASYNC-MARKER] marker ignored: no task this call started carries its id")
    return None


def _log(line: str) -> None:
    try:
        from vaf.core.log_helper import append_domain_log
        append_domain_log("backend", line)
    except Exception:
        pass


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
    command: Optional[Sequence[str]] = None,
    exclusive: bool = False,
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
        command: the child's CLI verb after `vaf.main`; default `subagent run <agent_type>`
            (the dispatcher). A whole workflow runs as `workflow run <id>` instead
            (vaf/cli/cmd/workflow.py), which has its own lifecycle and the same IPC contract.
        exclusive: register-then-verify against a concurrent twin (`claim_task_slot`): after
            the task is registered, exactly one of several racing launches of this agent type
            in this chat wins; a loser's task is cancelled and SpawnRefused is raised. The
            pre-check a caller does with `has_live_task` is check-then-act and misses a twin
            launched in the same instant.

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
    if exclusive:
        try:
            won = ipc.claim_task_slot(task_id, agent_type, session_id or get_current_session_id())
        except Exception:
            won = True     # a registry that cannot answer blocks nothing (the old lanes' rule)
        if not won:
            try:
                ipc.cancel_task(task_id)
            except Exception:
                pass
            raise SpawnRefused(agent_type)
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
    # ...and the conversation of its own such a turn runs in, so the child's working-memory
    # tools (a coder's request_clarification) write into that room's store. With neither a
    # session nor this, they wrote into the store of "no session known", which every
    # sessionless lane of every account reads.
    try:
        from vaf.core.subagent_ipc import get_current_conversation_id
        _conversation = None if effective_session else get_current_conversation_id()
        if _conversation:
            sub_env["VAF_CONVERSATION_ID"] = str(_conversation)
    except Exception:
        pass
    provider = subagent_provider_override()
    if provider:
        sub_env["VAF_PROVIDER"] = provider
    if extra_env:
        sub_env.update({str(k): str(v) for k, v in extra_env.items() if v is not None})

    head = [str(c) for c in command] if command else ["subagent", "run", agent_type]
    cmd_parts = [sys.executable, "-m", "vaf.main", *head]
    if include_task_arg:
        cmd_parts += ["--task", task]
    cmd_parts += [str(a) for a in args]
    cmd_parts += ["--task-id", task_id]

    display = agent_type.replace("_", " ").title()
    cmd = _escape_cmd(cmd_parts, Platform.is_windows())
    term_title = title or f"VAF {display} [{task_id}]"

    try:
        opened = Platform.open_new_terminal(cmd, title=term_title, extra_env=sub_env)
    except Exception:
        # A spawn that raised is a spawn that failed: fall through to the cancel below, or the
        # task would sit pending with nobody to run it.
        opened = False
    if opened:
        ipc.mark_task_running(task_id)
        _record_spawn(task_id)
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
