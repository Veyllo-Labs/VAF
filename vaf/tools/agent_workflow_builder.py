"""
Agent Workflow Builder
======================
Gives the main agent the ability to create and run workflows at runtime.

Two modes
---------
  run_temp — Ephemeral: define a multi-step plan, execute it immediately,
             then discard. No file written. Ideal for one-off complex tasks.
             Available to the agent in any session (not admin-only).

  create   — Persistent: save a workflow to ~/.vaf/workflows/ so it appears
             in the WebUI and can be re-used later. Admin-only (same gate as
             create_agent_tool so regular users cannot write arbitrary code).

  list     — List agent-created persistent workflows.
  delete   — Remove an agent-created persistent workflow. Admin-only.

How _agent is injected
-----------------------
execute_tool() in agent.py injects self as tool_args["_agent"], matching the
pattern used by create_agent_tool and python_sandbox. Because run() uses
**kwargs, Python does NOT automatically assign kwargs["_agent"] to
self._agent — run() must do this explicitly at the top:

    _injected = kwargs.get("_agent")
    if _injected is not None:
        self._agent = _injected

This gives the tool access to:
  - self._agent.tools     → full live tool registry for run_temp execution
  - self._agent._current_user_role / _current_user_scope_id → admin check

run_temp execution
-------------------
  1. Build WorkflowStep objects from the step dicts the agent provides.
  2. Collect tools from self._agent.tools (the full live registry).
  3. Run WorkflowEngine.execute() synchronously.
  4. Return the result string.

Nothing is written to disk. The in-memory WORKFLOW_TEMPLATES dict is not
modified either — the engine receives the steps directly.

Persistent workflow files
--------------------------
  Written to ~/.vaf/workflows/{workflow_id}.py.
  First line: # created_by: agent
  Content: WORKFLOW = { ... }   (JSON-serialised — valid Python syntax)
  The agent may only edit/delete files it created (first-line check).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional

from vaf.tools.base import BaseTool


class AgentWorkflowBuilderTool(BaseTool):
    name = "create_agent_workflow"
    description = (
        "Plan and run multi-step workflows. Use action='run_temp' for any complex "
        "task that needs more than one tool — it runs immediately and leaves nothing on disk.\n\n"
        "WHEN TO USE WHICH ACTION:\n"
        "  run_temp — RIGHT NOW task, ad-hoc, no saving. This is the default choice for "
        "complex multi-step work (research → analyse → write report, etc.). "
        "The workflow runs immediately and is discarded when done.\n"
        "  create   — Save a workflow PERMANENTLY so it can be re-used later via "
        "execute_workflow or from the WebUI Workflows tab. Admin-only. "
        "Only use this if the user explicitly wants a reusable workflow.\n"
        "  list     — Show workflows the agent has saved.\n"
        "  delete   — Remove a saved workflow.\n\n"
        "DO NOT use action='execute' — that does not exist. "
        "To run something NOW use action='run_temp'. "
        "To run a previously saved workflow use the separate execute_workflow tool.\n\n"
        "AUTOMATIC CLEANUP (run_temp only): All intermediate files created during the run "
        "are automatically deleted after completion. Only the final write_file output is kept. "
        "If the user wants to keep an additional file, pass keep_files=[\"/path/to/file\"]. "
        "This means: use write_file only for the FINAL deliverable — not for intermediate scripts.\n\n"
        "Each step needs an 'input' (supports {variable} substitution from prior steps) "
        "and a 'tool'. AVAILABLE SUB-AGENTS AND TOOLS FOR STEPS:\n"
        "  coding_agent    — Write/edit code, create HTML/CSS/JS, generate structured files, "
        "run analysis scripts. Default tool. Gets a shared project_path automatically.\n"
        "  research_agent  — Deep multi-source research (10+ sources), detailed reports, "
        "market analysis, technical deep-dives. Use for: patent research, market studies, "
        "competitive analysis. Args: topic (required), depth, language.\n"
        "  document_writer — Create professional Word/PDF documents (contracts, reports, letters). "
        "Gets the shared project_path automatically.\n"
        "  librarian_agent — File system operations: read/list/search files in directories. "
        "Use for: reading existing project files, finding documents.\n"
        "  web_search      — Quick web search (1-3 sources). Use for: news, facts, prices.\n"
        "  write_file      — Write raw content to a specific file path.\n"
        "  read_file       — Read a file. Use for: reading outputs from previous steps.\n"
        "  python_sandbox  — Execute Python code. Use for: data processing, calculations.\n\n"
        "RULE: Prefer research_agent for patent/market/technical research, "
        "coding_agent for file generation and analysis scripts. "
        "Do NOT use coding_agent for research that needs 10+ web sources.\n\n"
        "VARIABLE ANCHORING (automatic):\n"
        "The engine automatically prepends all original workflow variables as "
        "'IMMUTABLE DESIGN PILLARS' to every coding_agent/research_agent/document_writer/"
        "librarian_agent task. You MUST still reference variables in step inputs "
        "({patent_id}, {genre} etc.) — anchoring is a safety net, not a replacement.\n\n"
        "LIVING DOCUMENT PATTERN (recommended for 5+ step workflows):\n"
        "Instead of chaining {prev_step_output} through all steps (which causes drift), "
        "write a shared JSON file that all steps read and update:\n"
        "  Step 1: coding_agent → reads variables, writes /tmp/{workflow_id}/design.json\n"
        "  Step 2: coding_agent → reads design.json, adds its section, writes it back\n"
        "  Step N: coding_agent → always reads the FULL design.json (never loses Step 1 data)\n"
        "Use write_file + read_file tools for this. Avoid {prev_step_output} for large content.\n\n"
        "ASSERTIONS (output verification + selective retry):\n"
        "Each step can declare assertions to verify its output. On failure, only that step "
        "retries (with a correction hint) — not the whole workflow:\n"
        "  'assertions': [{'contains': '{patent_id}', 'error': 'Patent number missing from output'}]\n"
        "  'max_assertion_retries': 1  # default\n"
        "Supported operators: 'contains', 'not_contains'. Use {variable} in the expected value.\n\n"
        "CONSISTENCY REVIEW PATTERN (every 3-4 steps in complex workflows):\n"
        "Insert a check step that verifies original params are still honoured:\n"
        "  {'tool': 'coding_agent', 'description': 'Consistency Check',\n"
        "   'input': 'Check that {genre} and {core_mechanic} are correctly reflected in "
        "{prev_output}. Output ONLY \"OK\" if correct, else \"FAIL: [reason]\"',\n"
        "   'assertions': [{'not_contains': 'FAIL', 'error': 'Consistency check failed'}],\n"
        "   'on_failure': 'step_to_redo'}"
    )

    # ── Contract ──────────────────────────────────────────────────────────────
    # run_temp: available in any session — agent plans its own work.
    # create / delete: admin-only, enforced inside run() via _is_admin().
    permission_level  = "system"        # skip legacy confirmation gate
    side_effect_class = "reversible"    # files can be deleted; temp leaves nothing
    admin_only        = False           # run_temp works for all; create/delete checked internally
    channel_restrictions = ("telegram", "whatsapp", "discord")

    # ── Examples ──────────────────────────────────────────────────────────────
    input_examples = [
        {
            "action": "run_temp",
            "name": "Research and summarize",
            "steps": [
                {"input": "Search for recent news about {topic}", "tool": "web_search",    "output": "news"},
                {"input": "Write a concise summary of:\n{news}",  "tool": "coding_agent",  "output": "summary"},
            ],
            "variables": {"topic": "quantum computing"},
        },
        {
            "action": "create",
            "workflow_id": "daily_brief",
            "name": "Daily Briefing",
            "description": "Searches news and writes a daily brief",
            "triggers": ["daily brief", "morning summary"],
            "steps": [
                {"input": "Search today's top tech news", "tool": "web_search",   "output": "news"},
                {"input": "Write a 3-paragraph brief:\n{news}", "tool": "coding_agent", "output": "brief"},
            ],
        },
        {"action": "list"},
    ]

    # ── Parameters ────────────────────────────────────────────────────────────
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["run_temp", "create", "delete", "list"],
                "description": (
                    "run_temp — Execute a temporary workflow plan immediately, then discard it.\n"
                    "create   — Save a reusable workflow to disk (admin-only).\n"
                    "delete   — Remove an agent-created workflow from disk (admin-only).\n"
                    "list     — Show all workflows the agent has created."
                ),
            },
            "workflow_id": {
                "type": "string",
                "description": (
                    "Lowercase snake_case identifier for the workflow. "
                    "Required for create and delete. Ignored for run_temp (auto-generated)."
                ),
            },
            "name": {
                "type": "string",
                "description": "Human-readable name (required for run_temp and create).",
            },
            "description": {
                "type": "string",
                "description": "What the workflow does. Used in create; optional for run_temp.",
            },
            "steps": {
                "type": "array",
                "description": (
                    "Ordered list of steps. Each step is an object with:\n"
                    "  input       — Prompt/instruction for this step. "
                    "Use {variable_name} for output from previous steps or initial variables.\n"
                    "  tool        — Tool to use (e.g. 'coding_agent', 'web_search', "
                    "'research_agent', 'python_sandbox', 'write_file'). Default: coding_agent.\n"
                    "  output      — Variable name to store this step's result "
                    "(used in later steps as {output}). Default: step_N_output.\n"
                    "  description — Short label shown in progress output. Optional."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "input":       {"type": "string"},
                        "tool":        {"type": "string"},
                        "output":      {"type": "string"},
                        "description": {"type": "string"},
                        "on_success":  {"type": "string", "description": "Jump to this step's output_name on success."},
                        "on_failure":  {"type": "string", "description": "Jump to this step's output_name on failure (suppresses abort)."},
                        "optional":    {"type": "boolean", "description": "Skip on failure instead of aborting."},
                        "assertions":  {
                            "type": "array",
                            "description": "Output checks — if any fail, the step retries with a correction hint.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "contains":     {"type": "string", "description": "Expected substring in output. Supports {variable}."},
                                    "not_contains": {"type": "string", "description": "Substring that must NOT appear in output."},
                                    "error":        {"type": "string", "description": "Message shown when this assertion fails."},
                                },
                            },
                        },
                        "max_assertion_retries": {"type": "integer", "description": "How many times to retry on assertion failure (default: 1)."},
                    },
                    "required": ["input"],
                },
            },
            "variables": {
                "type": "object",
                "description": (
                    "Initial variable values for run_temp "
                    "(e.g. {'topic': 'AI trends'}). These are available as {variable} "
                    "in the first step and passed through automatically."
                ),
                "additionalProperties": True,
            },
            "triggers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Trigger phrases for create mode (optional).",
            },
            "keep_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "For run_temp only: list of absolute file paths to preserve after the workflow "
                    "finishes. All other intermediate files created during the run are automatically "
                    "deleted. Use this when the final step produces a document/report the user wants "
                    "to keep (e.g. keep_files=[\"/home/user/Documents/VAF_Projects/report.pdf\"]). "
                    "The last write_file output is always kept automatically."
                ),
            },
        },
        "required": ["action"],
    }

    # Injected by execute_tool() — gives access to the live tool registry
    # and current session context for admin checks.
    _agent: Optional[Any] = None

    # ─────────────────────────────────────────────────────────────────────────

    def run(self, **kwargs) -> str:                         # noqa: C901
        # Capture agent reference injected by execute_tool() via tool_args["_agent"].
        # The class attribute _agent defaults to None; kwargs is the only delivery
        # mechanism here since run() receives positional self + **kwargs (not self._agent).
        _injected = kwargs.get("_agent")
        if _injected is not None:
            self._agent = _injected

        action = (kwargs.get("action") or "").strip().lower()

        # Infer action from other fields when agent omits it
        if not action:
            if kwargs.get("is_temporary") or (kwargs.get("steps") and not kwargs.get("workflow_id")):
                action = "run_temp"
            elif kwargs.get("workflow_id") and kwargs.get("steps"):
                action = "create"
            elif kwargs.get("workflow_id") and not kwargs.get("steps"):
                action = "delete"

        if action == "run_temp":
            return self._run_temp(kwargs)
        if action == "create":
            return self._create_persistent(kwargs)
        if action == "list":
            return self._list_agent_workflows()
        if action == "delete":
            return self._delete_agent_workflow(kwargs)

        return (
            f"Error: unknown action '{action}'. "
            "Valid values: run_temp, create, list, delete."
        )

    # ─── run_temp ─────────────────────────────────────────────────────────────

    def _run_temp(self, kwargs: dict) -> str:
        """
        Build WorkflowStep objects from kwargs, run them synchronously via
        WorkflowEngine using the agent's live tool registry, and return the
        result.  Nothing is written to disk; WORKFLOW_TEMPLATES is untouched.

        When a WebUI session is active, emits workflow_start / workflow_update /
        workflow_output_stream WebSocket events so the VAFWorkflowRuntime panel
        opens and shows live step progress — identical to a persistent workflow.
        """
        import sys

        name      = (kwargs.get("name") or "Agent Temp Workflow").strip()
        desc      = (kwargs.get("description") or "").strip()
        raw_steps = kwargs.get("steps") or []
        variables = dict(kwargs.get("variables") or {})

        if not raw_steps:
            return "Error: 'steps' is required for action='run_temp'. Provide at least one step."

        # Validate and normalise steps
        normalised: List[dict] = []
        for i, s in enumerate(raw_steps):
            if not isinstance(s, dict):
                return f"Error: step {i + 1} must be an object with at least an 'input' field."
            # Accept 'agent_id' as alias for 'tool' (some LLMs emit this)
            tool_val = s.get("tool") or s.get("agent_id") or "coding_agent"
            # Accept 'input' as dict (convert to JSON string) or string
            raw_input = s.get("input") or s.get("code") or ""
            if isinstance(raw_input, dict):
                raw_input = json.dumps(raw_input, ensure_ascii=False)
            raw_input = str(raw_input).strip()
            if not raw_input:
                return f"Error: step {i + 1} is missing a non-empty 'input' field."
            # Normalise tool aliases the LLM sometimes emits
            _tool_aliases = {
                "python_exec": "python_sandbox",
                "python":      "python_sandbox",
                "bash":        "python_sandbox",
                "search":      "web_search",
                "write":       "write_file",
                "read":        "read_file",
            }
            tool_str = str(tool_val).strip() or "coding_agent"
            tool_str = _tool_aliases.get(tool_str.lower(), tool_str)
            step_dict: dict = {
                "input":       raw_input,
                "tool":        tool_str,
                "output":      s.get("output") or f"step_{i + 1}_output",
                "description": s.get("description") or s.get("name") or f"Step {i + 1}",
            }
            for _f in ("on_success", "on_failure", "optional", "condition",
                       "assertions", "max_assertion_retries", "args"):
                if _f in s:
                    step_dict[_f] = s[_f]
            normalised.append(step_dict)

        # Build WorkflowStep objects directly (no WORKFLOW_TEMPLATES mutation)
        try:
            from vaf.workflows.engine import WorkflowEngine, create_workflow
        except ImportError as exc:
            return f"Error: could not import workflow engine: {exc}"

        # Build as a template dict so create_workflow() can normalise it
        template_dict = {
            "name":        name,
            "description": desc,
            "triggers":    [],
            "steps":       normalised,
        }
        steps = create_workflow(template_dict)

        # Tool registry: prefer agent's live tools (full set), fall back to stubs
        tools = self._collect_tools()
        if not tools:
            return "Error: no tools available for workflow execution."

        # User isolation context (pass through from agent session)
        user_scope_id = None
        username      = "admin"
        agent = self._agent
        if agent is not None:
            user_scope_id = getattr(agent, "_current_user_scope_id", None)
            username      = getattr(agent, "_current_username", None) or "admin"

        # ── WebUI wiring ─────────────────────────────────────────────────────
        # Resolve the active session ID from the agent.  Both attribute names
        # are checked; if neither exists (CLI / test mode) session_id stays
        # None and every _push() call is a no-op.
        session_id = None
        if agent is not None:
            session_id = (
                getattr(agent, "current_session_id", None)
                or getattr(agent, "_session_id", None)
            )

        workflow_id = f"tmp-{uuid.uuid4().hex[:8]}"

        def _push(payload: dict) -> None:
            """Send a WebSocket event; silently swallows all errors."""
            if not session_id:
                return
            try:
                from vaf.core.web_interface import get_web_interface
                get_web_interface()._push_session_update(session_id, payload)
            except Exception:
                pass

        # Send workflow_start — this opens the VAFWorkflowRuntime panel
        ui_steps = [
            {
                "id":     f"step-{idx + 1}",
                "name":   s.description or s.tool,
                "type":   "tool",
                "status": "idle",
            }
            for idx, s in enumerate(steps)
        ]
        _push({
            "type":       "workflow_start",
            "workflowId": workflow_id,
            "name":       name,
            "steps":      ui_steps,
        })

        # ── Step-progress callback ────────────────────────────────────────────
        _STATUS = {"start": "running", "success": "success", "error": "failed", "skip": "skipped"}

        def _ws_callback(event: str, step, current: int, total: int) -> None:
            _push({
                "type":     "workflow_update",
                "stepId":   f"step-{current}",
                "status":   _STATUS.get(event, "running"),
                "progress": int((current / total) * 100),
            })
            if event == "start":
                label = step.description or step.tool
                _push({
                    "type":       "workflow_output_stream",
                    "workflowId": workflow_id,
                    "line":       f"\u2500\u2500\u2500 Step {current}/{total}: {label} [{step.tool}] \u2500\u2500\u2500",
                })
            elif event == "success":
                # Stream the step result so tools that don't write to stdout
                # (browser_agent, research_agent, etc.) still show output.
                _raw = str(getattr(step, "result", "") or "")
                _preview = _raw[:2000] + ("\n\u2026[gek\u00fcrzt]" if len(_raw) > 2000 else "")
                if _preview.strip():
                    for _line in _preview.splitlines():
                        _push({
                            "type":       "workflow_output_stream",
                            "workflowId": workflow_id,
                            "line":       _line,
                        })
                dur = getattr(step, "duration", 0)
                _push({
                    "type":       "workflow_output_stream",
                    "workflowId": workflow_id,
                    "line":       f"\u2713 Schritt {current}/{total} abgeschlossen ({dur:.1f}s)",
                })
            elif event == "error":
                err = getattr(step, "error", "") or ""
                _push({
                    "type":       "workflow_output_stream",
                    "workflowId": workflow_id,
                    "line":       f"\u2717 Schritt {current}/{total} fehlgeschlagen: {err[:300]}",
                })
            elif event == "skip":
                _push({
                    "type":       "workflow_output_stream",
                    "workflowId": workflow_id,
                    "line":       f"\u2014 Schritt {current}/{total} \u00fcbersprungen",
                })

        # ── Stdout / stderr wrapper ───────────────────────────────────────────
        class _WebStreamWriter:
            """
            Forwards every write to the original stream AND splits on newlines
            to emit workflow_output_stream events to the WebUI.
            When session_id is None the WebUI path is skipped entirely.
            """
            def __init__(self, stream):
                self._stream = stream
                self._buf    = ""

            def write(self, data: str) -> None:
                try:
                    self._stream.write(data)
                    self._stream.flush()
                except Exception:
                    pass
                if not session_id:
                    return
                self._buf += data
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    _push({
                        "type":       "workflow_output_stream",
                        "workflowId": workflow_id,
                        "line":       line,
                    })

            def flush(self) -> None:
                try:
                    self._stream.flush()
                except Exception:
                    pass

            def isatty(self) -> bool:
                return getattr(self._stream, "isatty", lambda: False)()

            def fileno(self) -> int:
                return getattr(self._stream, "fileno", lambda: -1)()

        # ── Execute ───────────────────────────────────────────────────────────
        engine = WorkflowEngine(
            tools         = tools,
            callback      = _ws_callback,
            user_scope_id = user_scope_id,
            username      = username,
        )
        engine._workflow_name = name   # used in debug logs

        # Stop wiring: let the Stop button abort the workflow — both between steps
        # (engine loop) and *during* a step (bounded-run inside the engine polls this).
        # IMPORTANT: the Stop button targets the *canonical* session id (what the WebSocket
        # sends / what get_current_session_id() returns, e.g. "orange166279"). That can
        # differ from `agent.current_session_id` captured above, so we check should_stop for
        # ALL plausible session ids — otherwise Stop silently does nothing.
        _stop_sid = session_id
        def _check_stop() -> bool:
            try:
                from vaf.core.task_queue import TaskQueue
                from vaf.core.subagent_ipc import get_current_session_id
                tq = TaskQueue()
                candidates = set()
                if _stop_sid:
                    candidates.add(str(_stop_sid))
                _cur = get_current_session_id()
                if _cur:
                    candidates.add(str(_cur))
                _env_sid = os.environ.get("VAF_SESSION_ID")
                if _env_sid:
                    candidates.add(str(_env_sid))
                return any(tq.should_stop(s) for s in candidates)
            except Exception:
                return False

        _orig_stdout = sys.stdout
        _orig_stderr = sys.stderr
        # VAF_IN_WORKFLOW_TERMINAL activates simple_mode in coding_agent:
        # instead of the Rich Live TUI it prints "[Coder] ..." to sys.stdout,
        # which _WebStreamWriter then captures and forwards as workflow_output_stream.
        _prev_wf_terminal = os.environ.get("VAF_IN_WORKFLOW_TERMINAL")
        _prev_tool_model = os.environ.get("VAF_TOOL_MODEL")
        from vaf.core.config import Config as _CfgWF
        _subagent_model = _CfgWF.get("subagent_model", "")
        result = None
        _wf_exc = None
        try:
            sys.stdout = _WebStreamWriter(sys.stdout)
            sys.stderr = _WebStreamWriter(sys.stderr)
            os.environ["VAF_IN_WORKFLOW_TERMINAL"] = "1"
            if _subagent_model and _subagent_model.lower() != "deepseek-auto":
                os.environ["VAF_TOOL_MODEL"] = _subagent_model
            result = engine.execute(
                steps, variables=variables, check_stop=_check_stop,
                wait_for_subagents=True,   # run sub-agents as killable child processes
            )
        except Exception as exc:
            _wf_exc = exc
        finally:
            sys.stdout = _orig_stdout
            sys.stderr = _orig_stderr
            if _prev_wf_terminal is None:
                os.environ.pop("VAF_IN_WORKFLOW_TERMINAL", None)
            else:
                os.environ["VAF_IN_WORKFLOW_TERMINAL"] = _prev_wf_terminal
            if _prev_tool_model is None:
                os.environ.pop("VAF_TOOL_MODEL", None)
            else:
                os.environ["VAF_TOOL_MODEL"] = _prev_tool_model
            # workflow_start was sent at the top, so the WebUI must ALWAYS be told the run
            # ended — otherwise the chat "workflow running" indicator (and the runtime panel)
            # hang on "running" forever. Pushed after stdout is restored (not via the stream
            # writer). Paused is a valid non-final state (resolved on resume), so skip it.
            try:
                if _wf_exc is not None:
                    _push({"type": "workflow_done", "workflowId": workflow_id,
                           "success": False, "error": str(_wf_exc)})
                elif result is not None and not getattr(result, "paused", False):
                    _push({"type": "workflow_done", "workflowId": workflow_id,
                           "success": bool(result.success), "error": str(result.error or "")})
            except Exception:
                pass

        if _wf_exc is not None:
            return f"Error executing temporary workflow '{name}': {_wf_exc}"

        if result.paused:
            return (
                f"Temporary workflow '{name}' paused (async sub-agent). "
                "The result will arrive when the sub-agent completes."
            )

        # ── Intermediate file cleanup for run_temp ────────────────────────────
        # After a temp workflow completes, delete all intermediate files that were
        # created in the shared project path. The final answer is in result.final_output
        # (text). If the last step produced a file the user wants to keep, the agent
        # should pass keep_files=[path] to preserve it; everything else is removed.
        _proj_path = (result.outputs or {}).get("workflow_project_path", "")
        if _proj_path and os.path.isdir(_proj_path):
            _keep_files = set()
            # Preserve explicitly requested files
            for _kf in (kwargs.get("keep_files") or []):
                _kf = str(_kf).strip()
                if _kf:
                    _keep_files.add(os.path.realpath(_kf))
            # Infer final output file: if last step used write_file, keep that path
            _last_step_output = str(result.final_output or "")
            # write_file tool returns the path it wrote to
            if _last_step_output and os.path.isfile(_last_step_output):
                _keep_files.add(os.path.realpath(_last_step_output))
            # Walk and delete intermediates
            _deleted = 0
            for _root, _dirs, _files in os.walk(_proj_path, topdown=False):
                for _fn in _files:
                    _fp = os.path.realpath(os.path.join(_root, _fn))
                    if _fp not in _keep_files:
                        try:
                            os.unlink(_fp)
                            _deleted += 1
                        except Exception:
                            pass
                for _dn in _dirs:
                    _dp = os.path.join(_root, _dn)
                    try:
                        os.rmdir(_dp)  # only removes if empty
                    except Exception:
                        pass
            # Remove the project dir itself if now empty
            try:
                os.rmdir(_proj_path)
            except Exception:
                pass

        if result.success:
            return f"Temporary workflow '{name}' completed.\n\n{result.final_output}"
        return f"Temporary workflow '{name}' failed: {result.error}"

    def _collect_tools(self) -> Dict[str, Any]:
        """
        Return the agent's live tool registry.  If _agent is not injected
        (e.g. in tests), fall back to instantiating a minimal set.
        """
        agent = self._agent
        if agent is not None and hasattr(agent, "tools") and agent.tools:
            return dict(agent.tools)

        # Minimal fallback for test / offline environments
        tools: Dict[str, Any] = {}
        _optional_imports = [
            ("vaf.tools.search",     "WebSearchTool",      "web_search"),
            ("vaf.tools.coder",      "CodingAgentTool",    "coding_agent"),
            ("vaf.tools.research",   "ResearchAgentTool",  "research_agent"),
            ("vaf.tools.filesystem", "WriteFileTool",      "write_file"),
            ("vaf.tools.filesystem", "ReadFileTool",       "read_file"),
        ]
        for module_path, class_name, tool_name in _optional_imports:
            try:
                import importlib
                mod = importlib.import_module(module_path)
                cls = getattr(mod, class_name)
                tools[tool_name] = cls()
            except Exception:
                pass
        return tools

    # ─── create (persistent) ──────────────────────────────────────────────────

    def _create_persistent(self, kwargs: dict) -> str:
        if not self._is_admin():
            return (
                "Error: Creating persistent workflows requires an admin session. "
                "For a one-shot plan, use action='run_temp' instead."
            )

        workflow_id = (kwargs.get("workflow_id") or "").strip()
        name        = (kwargs.get("name") or "").strip()
        description = (kwargs.get("description") or "").strip()
        triggers    = [str(t) for t in (kwargs.get("triggers") or []) if str(t).strip()]
        raw_steps   = kwargs.get("steps") or []

        if not re.match(r'^[a-z][a-z0-9_]*$', workflow_id):
            return (
                f"Error: workflow_id must be lowercase snake_case, got '{workflow_id}'. "
                "Example: 'daily_brief'."
            )
        if not name:
            return "Error: 'name' is required for action='create'."
        if not raw_steps:
            return "Error: 'steps' is required for action='create'. Provide at least one step."

        normalised = []
        for i, s in enumerate(raw_steps if isinstance(raw_steps, list) else []):
            if not isinstance(s, dict) or not s.get("input", "").strip():
                return f"Error: step {i + 1} missing non-empty 'input' field."
            step_dict2: dict = {
                "input":       s["input"],
                "tool":        s.get("tool", "coding_agent"),
                "output":      s.get("output") or f"step_{i + 1}_output",
                "description": s.get("description") or f"Step {i + 1}",
            }
            for _f in ("on_success", "on_failure", "optional", "condition",
                       "assertions", "max_assertion_retries", "args"):
                if _f in s:
                    step_dict2[_f] = s[_f]
            normalised.append(step_dict2)

        wf_dict = {
            "name":        name,
            "description": description,
            "triggers":    triggers,
            "steps":       normalised,
        }

        user_dir = os.path.expanduser("~/.vaf/workflows")
        os.makedirs(user_dir, exist_ok=True)
        wf_path = os.path.join(user_dir, f"{workflow_id}.py")

        if os.path.exists(wf_path):
            return (
                f"Error: a workflow named '{workflow_id}' already exists. "
                "Use action='delete' first, or choose a different workflow_id."
            )

        content = (
            f"# created_by: agent\n"
            f"# Workflow: {name}\n"
            f"WORKFLOW = {json.dumps(wf_dict, indent=4, ensure_ascii=False)}\n"
        )
        tmp = wf_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, wf_path)

        try:
            from vaf.workflows.templates import reload_workflows, list_templates
            reload_workflows()
        except Exception as exc:
            return (
                f"Workflow '{workflow_id}' saved to disk but registry reload failed: {exc}. "
                "It will be available after the next server restart."
            )

        # Notify the WebUI: refresh list + play a save-preview animation
        try:
            import time as _time
            from vaf.core.web_interface import get_web_interface
            agent = self._agent
            session_id = None
            if agent is not None:
                session_id = (
                    getattr(agent, "current_session_id", None)
                    or getattr(agent, "_session_id", None)
                )
            if session_id:
                wi = get_web_interface()

                # 1. Open the Workflow Runtime panel showing all steps as idle
                ui_steps = [
                    {
                        "id":     f"step-{idx + 1}",
                        "name":   s.get("description") or s.get("tool") or f"Step {idx + 1}",
                        "type":   "tool",
                        "status": "idle",
                    }
                    for idx, s in enumerate(normalised)
                ]
                wi._push_session_update(session_id, {
                    "type":       "workflow_start",
                    "workflowId": workflow_id,
                    "name":       name,
                    "steps":      ui_steps,
                })

                # 2. Console line
                wi._push_session_update(session_id, {
                    "type":       "workflow_output_stream",
                    "workflowId": workflow_id,
                    "line":       f"✓ Saved: ~/.vaf/workflows/{workflow_id}.py",
                })

                # 3. Animate each step to success in sequence
                for idx in range(len(normalised)):
                    _time.sleep(0.28)
                    wi._push_session_update(session_id, {
                        "type":     "workflow_update",
                        "stepId":   f"step-{idx + 1}",
                        "status":   "success",
                        "progress": 100,
                    })

                # 4. Refresh workflow list (panel auto-closes after 2.5 s via store)
                wi._push_session_update(session_id, {
                    "type": "workflow_created",
                    "workflow_id": workflow_id,
                })
                wi._push_session_update(session_id, {
                    "type": "workflows_list",
                    "workflows": list_templates(),
                })
        except Exception:
            pass

        return (
            f"Workflow '{workflow_id}' saved successfully. "
            "It is now available via execute_workflow() and visible in the WebUI."
        )

    # ─── list ─────────────────────────────────────────────────────────────────

    def _list_agent_workflows(self) -> str:
        user_dir = os.path.expanduser("~/.vaf/workflows")
        if not os.path.isdir(user_dir):
            return "You have not created any persistent workflows yet."

        found: List[str] = []
        for fname in sorted(os.listdir(user_dir)):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(user_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    first = fh.readline().strip()
                if "created_by: agent" in first:
                    found.append(fname[:-3])   # strip .py
            except Exception:
                pass

        if not found:
            return "You have not created any persistent workflows yet."

        lines = ["Agent-created persistent workflows:"]
        for wf_id in found:
            lines.append(f"  • {wf_id}")
        lines.append(
            "\nCall execute_workflow(workflow_id=...) to run one, "
            "or action='delete' to remove one."
        )
        return "\n".join(lines)

    # ─── delete ───────────────────────────────────────────────────────────────

    def _delete_agent_workflow(self, kwargs: dict) -> str:
        if not self._is_admin():
            return "Error: Deleting persistent workflows requires an admin session."

        workflow_id = (kwargs.get("workflow_id") or "").strip()
        if not workflow_id:
            return "Error: 'workflow_id' is required for action='delete'."

        user_dir = os.path.expanduser("~/.vaf/workflows")
        wf_path  = os.path.join(user_dir, f"{workflow_id}.py")

        if not os.path.exists(wf_path):
            return (
                f"Error: '{workflow_id}' not found in agent-created workflows. "
                "Use action='list' to see what you have created."
            )

        # Ownership check — only files with the agent marker may be deleted
        try:
            with open(wf_path, "r", encoding="utf-8") as fh:
                first = fh.readline().strip()
            if "created_by: agent" not in first:
                return (
                    f"Error: '{workflow_id}' was not created by the agent "
                    "(missing 'created_by: agent' marker). Cannot delete."
                )
        except Exception as exc:
            return f"Error reading workflow file: {exc}"

        os.remove(wf_path)

        try:
            from vaf.workflows.templates import reload_workflows
            reload_workflows()
        except Exception:
            pass

        return f"Workflow '{workflow_id}' deleted successfully."

    # ─── Private helpers ──────────────────────────────────────────────────────

    def _is_admin(self) -> bool:
        """True when the current agent session belongs to an admin user."""
        agent = self._agent
        if agent is None:
            return False
        try:
            from vaf.core.config import get_local_admin_scope_id
            _role  = getattr(agent, "_current_user_role",     None)
            _scope = getattr(agent, "_current_user_scope_id", None)
            _local = get_local_admin_scope_id()
            return (
                _role == "admin"
                or (_scope is not None and str(_scope) == str(_local))
            )
        except Exception:
            return False
