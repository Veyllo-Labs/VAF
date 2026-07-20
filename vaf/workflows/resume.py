# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Continuing a workflow that paused on an async sub-agent step, from the result drain.

A workflow step that hands off to an async sub-agent does not finish the run: the engine
saves a PausedWorkflow record and returns `paused=True`. The run continues only when
somebody notices that the awaited sub-agent has reported back. Until this module existed,
only the interactive CLI TUI did that noticing (`vaf/cli/cmd/run.py`); the web and headless
drain had no lookup at all (live incident 2026-07-20).

SCOPE, deliberately narrow: this module finishes a run whose awaited step was the LAST one.
That is pure bookkeeping - `WorkflowEngine.resume_workflow` returns without calling
`execute()` when no steps remain - so it is safe to do inline on the drain thread.

A run with steps still ahead of it is NOT continued here, and its record is left untouched.
Running those steps from the drain was designed, reviewed and rejected, because every
available shape was unsafe:

- On a worker thread: `WorkflowEngine.execute` mutates PROCESS-GLOBAL os.environ around
  sub-agent steps (VAF_AGENT_TYPE / VAF_TASK_ID / VAF_IN_SUBAGENT_TERMINAL) and restores it
  afterwards. Doing that concurrently with the main thread's chat turn re-opens the exact
  incident CLAUDE.md Rule 4.5 exists for: a leaked VAF_IN_SUBAGENT_TERMINAL makes every
  coder run execute in-process and serializes all chat behind it. A bare thread also
  inherits no contextvars, so the session ContextVar would be unset inside it.
- Inline on the drain thread: that drain is a SINGLE loop serving every session, so a
  multi-minute workflow there stalls result delivery for all users.

Both need a proper owner - a child process with its own environment, heartbeat, stop
support and panel events, the way `vaf/cli/cmd/workflow.py` already runs a whole workflow.
That is its own piece of work. Until then the record simply stays on disk: the deliverable
still reaches the user (the sub-agent delivers its own result through the drain), and
nothing is destroyed.
"""
from typing import Any, Optional, Tuple

from vaf.core.subagent_ipc import PausedWorkflow, get_ipc

# Keep the delivered text bounded: a workflow's final output can be a whole document, and it
# lands in the model's context. Same ceiling the CLI resume path uses.
_MAX_DELIVERED = 2000


def find_paused_workflow_for(task_id: str) -> Optional[PausedWorkflow]:
    """The paused run waiting for this task, or None. Read-only; does not claim."""
    try:
        return get_ipc().get_paused_workflow_for_task(task_id)
    except Exception:
        return None


def remaining_step_count(record: PausedWorkflow) -> int:
    """How many steps are still ahead of the awaited one."""
    try:
        return max(0, len(record.steps_data or []) - (int(record.current_step_index) + 1))
    except Exception:
        # Unknown shape: treat as "there is more", so the safe branch is taken and nothing
        # is claimed or destroyed.
        return 1


def _completion_message(record: PausedWorkflow, result) -> str:
    """The system message a finished run contributes to the conversation."""
    name = record.workflow_name or "workflow"
    if getattr(result, "paused", False):
        # Should not happen on this path (nothing was left to execute), but a hand-edited or
        # inconsistent record could still produce it. Never label a live run as stopped.
        return (
            f"**Workflow still running** ({name})\n"
            f"It handed off to "
            f"{getattr(result, 'waiting_for_agent', '') or 'a background helper'} again. "
            f"Nothing went wrong; tell the user it is still working."
        )
    if getattr(result, "success", False):
        out = str(getattr(result, "final_output", "") or "")
        if len(out) > _MAX_DELIVERED:
            out = out[:_MAX_DELIVERED] + f"\n\n[... {len(out) - _MAX_DELIVERED} more characters ...]"
        return (
            f"**Workflow completed** ({name})\n\n{out}\n\n"
            f"All steps are done. Present this to the user; do not redo any of it."
        )
    return (
        f"**Workflow stopped** ({name})\n"
        f"Reason: {getattr(result, 'error', None) or 'unknown'}\n"
        f"Tell the user which part did not finish."
    )


def try_resume_paused_workflow(agent: Any, task: Any) -> Tuple[bool, Optional[str]]:
    """Finish the workflow that was waiting for `task`, when nothing is left to run.

    Returns (handled, message):
      handled - True when this call owns the outcome, so the caller must not ALSO deliver
                the raw sub-agent result for this task.
      message - text for the conversation when handled.

    Returns (False, None) for every case it does not own, including a run with steps still
    ahead of it, so the caller's ordinary delivery keeps working exactly as before.

    Never raises. A problem here must not be able to break the result drain, because that
    same drain is what delivers ordinary sub-agent results (Rule 4.3).
    """
    try:
        ipc = get_ipc()
        task_id = getattr(task, "task_id", None)
        if not task_id:
            return False, None

        pending = ipc.get_paused_workflow_for_task(task_id)
        if pending is None:
            return False, None

        # Ownership (Rule 4.4): a paused record belongs to the session that created it.
        # Records written before session tracking existed carry no session and stay with
        # whoever drains them, which matches the previous behavior.
        current_session = getattr(agent, "current_session_id", None)
        if pending.session_id and current_session and pending.session_id != current_session:
            return False, None

        if getattr(task, "status", "") != "completed":
            # The awaited helper failed, timed out or was cancelled, so the run can never
            # continue. Drop the record so it does not linger as an orphan, but do NOT claim
            # ownership of the outcome: the drain's own failure handling is what tells the
            # user what went wrong, and swallowing that would lose the only report there is.
            try:
                ipc.remove_paused_workflow(pending.workflow_id)
            except Exception:
                pass
            return False, None

        if remaining_step_count(pending) > 0:
            # Steps still ahead. Not continued here on purpose - see the module docstring.
            # The record is left intact so a proper runner can pick it up later, and the
            # caller still delivers the sub-agent's own result.
            try:
                from vaf.core.log_helper import append_domain_log
                append_domain_log(
                    "backend",
                    f"[WORKFLOW-RESUME] '{pending.workflow_name}' has "
                    f"{remaining_step_count(pending)} step(s) left after {task_id}; left "
                    f"paused (no in-drain runner)."
                )
            except Exception:
                pass
            return False, None

        # Nothing left to run: claim it atomically (the CLI drain may be looking at the same
        # finished task) and close the run inline. resume_workflow returns without calling
        # execute() in this case, so no tool runs and no process-global environment is
        # touched on this thread.
        claimed = ipc.claim_paused_workflow(pending.workflow_id)
        if claimed is None:
            return False, None   # another drain got there first

        from vaf.workflows.engine import WorkflowEngine

        engine = WorkflowEngine(
            getattr(agent, "tools", {}) or {},
            user_scope_id=claimed.user_scope_id,
            username=claimed.username,
        )
        engine._workflow_name = claimed.workflow_name
        engine._template_id = claimed.template_id
        engine._session_id = claimed.session_id
        engine._ui_workflow_id = claimed.ui_workflow_id

        result = engine.resume_workflow(claimed, getattr(task, "result", "") or "")
        return True, _completion_message(claimed, result)
    except Exception:
        return False, None
