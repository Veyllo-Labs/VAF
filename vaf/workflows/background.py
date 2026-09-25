# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Run a workflow in a process of its own: the chat turn that starts it ends at once, and the
chat hears the result when the run ends.

A workflow used to run INSIDE the chat turn that started it, for as long as it took - a
research run, a document, a coder step - and one chat worker serves every chat by default, so
the whole app waited with it. The router lane (a saved template the router matched) already
ran the whole workflow as its own process (`vaf workflow run`, vaf/cli/cmd/workflow.py) and
reported back through the sub-agent IPC: the task record, the heartbeat, `complete_task`, and
the runner's drain, which delivers the result into the chat once and lets the agent carry on
there. This module is that lane for everyone - the router lane, `execute_workflow` and a
temporary `run_temp` plan - so the three cannot drift apart again.

WHEN a workflow goes to the background, all of these hold:
- `sub_agents_in_separate_terminals` is on (the default), the switch every sub-agent already
  obeys;
- this process is not itself a sub-agent or workflow child (the env markers), which would
  otherwise nest terminals;
- there is a chat to come back to (a session id);
- every tool the plan names exists in the child. The child has no agent registry: it builds
  the workflow primitives (`tool_overlay.PRIMITIVE_NAMES`), which cover every built-in
  template. A plan that names anything else (a custom tool, an MCP tool, a mail or calendar
  tool) runs in the chat as before. NAMED BOUNDARY: building the full registry in the child
  would mean constructing an Agent there, and the measured cost of the plans that need it
  (none of the built-in templates) does not earn that.

The child runs with the chat's identity as data (the session id; the engine resolves the scope
from the session's own metadata, `identity_for_engine`), never with the parent's ambient state.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, Iterable, List, Optional

_CHILD_MARKERS = ("VAF_IN_SUBAGENT_TERMINAL", "VAF_IN_WORKFLOW_TERMINAL")

#: Returned when a twin of the same workflow already runs for this chat.
ALREADY_RUNNING = ("Workflow '{name}' is ALREADY RUNNING for this chat - not starting a "
                   "duplicate. Tell the user the workflow is still in progress; the result "
                   "will arrive when it finishes.")


def enabled(config_get: Optional[Callable[[str, Any], Any]] = None) -> bool:
    """May a workflow started here run in a process of its own at all? `config_get` is the
    caller's own reader (an agent's merged config), else the global config."""
    for key in _CHILD_MARKERS:
        if os.environ.get(key, "").strip().lower() in ("1", "true", "yes"):
            return False
    try:
        if config_get is None:
            from vaf.core.config import Config
            config_get = Config.get
        return bool(config_get("sub_agents_in_separate_terminals", True))
    except Exception:
        return False


def missing_tools(tool_names: Iterable[str]) -> List[str]:
    """The tools a plan names that the child would not have, in order, each once."""
    from vaf.workflows.tool_overlay import PRIMITIVE_NAMES
    out: List[str] = []
    for name in tool_names or ():
        name = str(name or "").strip()
        if name and name not in PRIMITIVE_NAMES and name not in out:
            out.append(name)
    return out


def _note(name: str) -> str:
    """What follows the async marker (spawn_subagent owns the marker itself: its format is
    the contract the agent loop keys on, it ends the turn and tells the person). Words the
    model can act on."""
    return (f"Workflow '{name}' is running in the background. The result arrives in this chat "
            "when it finishes; do not start it again and do not do its steps yourself.")


def _language_env(language: Optional[str]) -> Dict[str, str]:
    return {"VAF_USER_LANGUAGE": str(language)} if language else {}


def spawn_saved(workflow_id: str, variables: Dict[str, Any], *, name: str,
                session_id: Optional[str], task: str = "", language: Optional[str] = None):
    """Start a SAVED template as its own process: the SpawnedSubagent, or None when the
    process could not be started (the IPC task is cancelled then). Raises SpawnRefused when a
    twin of this workflow already holds this chat's slot. For a caller that words its own
    answer (the router lane); a tool calls start_saved."""
    from vaf.core.subagent_spawn import spawn_subagent
    return spawn_subagent(
        f"workflow:{workflow_id}", task or f"workflow: {name}",
        command=("workflow", "run", workflow_id),
        args=("--variables", json.dumps(variables or {}, ensure_ascii=False)),
        include_task_arg=False, session_id=session_id, exclusive=True,
        extra_env=_language_env(language),
        title=f"VAF Workflow: {workflow_id}", marker_note=_note(name))


def start_saved(workflow_id: str, variables: Dict[str, Any], *, name: str,
                session_id: Optional[str], task: str = "",
                language: Optional[str] = None) -> Optional[str]:
    """Start a SAVED template in the background. Returns the tool result to hand back, the
    already-running message when a twin holds the slot, or None when the process could not be
    started (the IPC task is cancelled then; the caller runs the workflow inline as before)."""
    from vaf.core.subagent_spawn import SpawnRefused
    try:
        spawned = spawn_saved(workflow_id, variables, name=name, session_id=session_id,
                              task=task, language=language)
    except SpawnRefused:
        return ALREADY_RUNNING.format(name=workflow_id)
    except Exception:
        return None     # could not be started: the caller runs it inline, as before
    return spawned.marker if spawned is not None else None


def start_temp(name: str, steps: List[Dict[str, Any]], variables: Dict[str, Any], *,
               session_id: Optional[str], description: str = "",
               keep_files: Iterable[str] = (), user_intent: str = "",
               language: Optional[str] = None) -> Optional[str]:
    """Start a TEMPORARY plan in the background. `steps` are the normalised step dicts the
    chat lane built (repaired, validation flags already set), so the child runs exactly the
    plan the chat would have run. The plan travels as the IPC payload sidecar - argv cannot
    carry it reliably - and the child reads it with `--plan-from-task`. Same returns as
    start_saved."""
    from vaf.core.subagent_spawn import SpawnRefused, spawn_subagent
    label = str(name or "temporary workflow").strip() or "temporary workflow"
    agent_type = f"workflow:temp:{label}"
    spec = {"name": label, "description": description or "", "steps": list(steps or []),
            "variables": dict(variables or {}), "keep_files": [str(k) for k in (keep_files or ())],
            "user_intent": user_intent or ""}
    try:
        spawned = spawn_subagent(
            agent_type, f"workflow: {label}",
            # The label stays OFF the argv: it is the model's text, and one that starts with
            # "-" would be read as an option. The child takes it from the plan.
            command=("workflow", "run", "temp", "--plan-from-task"),
            include_task_arg=False, payload=json.dumps(spec, ensure_ascii=False),
            session_id=session_id, exclusive=True, extra_env=_language_env(language),
            title=f"VAF Workflow: {label}", marker_note=_note(label))
    except SpawnRefused:
        return ALREADY_RUNNING.format(name=label)
    except Exception:
        return None     # could not be started: the caller runs it inline, as before
    return spawned.marker if spawned is not None else None


def plan_from_payload(raw: Optional[str]) -> Dict[str, Any]:
    """The child's side of `start_temp`: the plan, or {} for a payload that is not one."""
    try:
        spec = json.loads(raw or "")
    except Exception:
        return {}
    if not isinstance(spec, dict) or not isinstance(spec.get("steps"), list) or not spec["steps"]:
        return {}
    return spec
