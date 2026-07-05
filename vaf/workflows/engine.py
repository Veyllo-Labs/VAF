# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Workflow Engine - Core execution logic for multi-step pipelines

The engine executes a sequence of tool calls, automatically passing
outputs from one step as inputs to the next.
"""

import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, List, Optional, Callable
from enum import Enum

from vaf.cli.ui import UI


# Steps whose output is a content deliverable that can meaningfully be re-generated with a
# correction hint. Only these are eligible for the opt-in per-step output validation — a
# correction-retry on a deterministic tool (write_file, web_search, …) would just repeat itself.
VALIDATABLE_TOOLS = frozenset({
    "document_agent", "document_writer", "research_agent",
    "coding_agent", "browser_agent", "librarian_agent",
})


# Relative NEW-artifact path args per step tool, resolved against the shared
# workflow project dir. move_file's src usually points at an existing file (handled
# by the existing-file guard below); dst is the new location.
_WORKFLOW_REL_PATH_ARGS = {"write_file": ("path",), "move_file": ("src", "dst")}
# Folder aliases the filesystem tools resolve themselves (Desktop/Documents/...);
# joining them onto the project dir would defeat that convention.
_WORKFLOW_FOLDER_ALIASES = {
    "desktop", "documents", "downloads", "pictures", "videos", "music",
    "dokumente", "bilder", "musik", "herunterladen",
}


def _inject_workflow_paths(step_tool: str, args: Dict[str, Any], workflow_project_path: Optional[str]) -> None:
    """Route file-producing steps into the shared per-run project directory (in place).

    - coding_agent / document_writer get it as project_path (unless the step set one).
    - write_file / move_file relative NEW-artifact paths are resolved against it, so a
      bare filename does not resolve against the backend process cwd (observed live
      2026-07-03: a workflow draft written to the user's home root, where the file
      endpoint rightly refuses to serve it).

    Left untouched: absolute / ~-anchored paths (explicit choice); a folder-alias
    first segment (Desktop/Documents/...) the filesystem tool resolves itself; and a
    relative path that ALREADY points at an existing file - that is an in-place update
    of a real user file (e.g. a code_review step that read the same relative path),
    which must stay cwd-relative so the read and the write agree.
    """
    if not workflow_project_path:
        return
    if step_tool in ("coding_agent", "document_writer"):
        if "project_path" not in args:
            args["project_path"] = workflow_project_path
        return
    for _arg in _WORKFLOW_REL_PATH_ARGS.get(step_tool, ()):
        p = args.get(_arg)
        if not (isinstance(p, str) and p.strip()):
            continue
        if os.path.isabs(os.path.expanduser(p)):
            continue  # explicit absolute/~ target
        if p.split("/", 1)[0].split("\\", 1)[0].lower() in _WORKFLOW_FOLDER_ALIASES:
            continue  # tool resolves the folder alias itself
        if os.path.exists(p):
            continue  # in-place update of an existing file (read/write must agree)
        args[_arg] = os.path.join(workflow_project_path, p)


def _strip_rich_links(text: str) -> str:
    """Replace Rich link markup with just the display text.

    When coding_agent (or any tool) returns Rich markup like
      [link=file:///home/user/My%20Project]/home/user/My Project[/link]
    and that output is used as a template variable in the next workflow step,
    the URL-encoded path inside [link=...] gets embedded in the downstream task
    text.  The coder's path-extraction regex (\\S+) then matches straight through
    the ']' separator and produces a mangled path like
      /home/user/My%20Project]/home/user/My
    causing the agent to create a nested directory with a URL-encoded name.

    Stripping [link=...]...[/link] and replacing it with only the display text
    prevents this entirely.  Style tags ([bold], [dim], etc.) are left as-is
    since they are harmless in LLM context.
    """
    return re.sub(r'\[link=[^\]]*\](.*?)\[/link\]', r'\1', text, flags=re.DOTALL)


class StepStatus(Enum):
    """Status of a workflow step."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class WorkflowStep:
    """A single step in a workflow."""
    tool: str                           # Tool name to execute
    input_template: str                 # Input with {variables} (single-arg tools)
    output_name: str                    # Name for this step's output
    description: str = ""               # Human-readable description
    optional: bool = False              # Skip on failure instead of abort
    condition: Optional[str] = None     # Only run if condition met; supports AND/OR/NOT operators
    args_template: Optional[Dict[str, Any]] = None  # Multi-arg tool inputs (safer than JSON strings)
    on_success: Optional[str] = None    # Jump to step with this output_name (or index) on success
    on_failure: Optional[str] = None    # Jump to step with this output_name (or index) on failure (suppresses abort)
    assertions: Optional[List[Dict[str, str]]] = None  # Output checks: [{"contains": "{var}", "error": "msg"}]
    max_assertion_retries: int = 1      # How many times to retry this step on assertion failure
    validate: bool = False              # Opt-in: LLM-check this step's output against its goal, retry on mismatch

    # Runtime state
    status: StepStatus = StepStatus.PENDING
    result: Any = None
    error: Optional[str] = None
    duration: float = 0.0


@dataclass
class WorkflowResult:
    """Result of a complete workflow execution."""
    success: bool
    outputs: Dict[str, Any]             # All step outputs by name
    final_output: Any                   # Last step's output
    steps: List[WorkflowStep]           # All steps with status
    total_duration: float
    error: Optional[str] = None
    paused: bool = False                # True if workflow is paused waiting for sub-agent
    workflow_id: Optional[str] = None   # ID for resuming paused workflow
    waiting_for_task: Optional[str] = None  # Task ID we're waiting for


def _temporal_builtins(username: Optional[str] = None) -> Dict[str, str]:
    """Built-in {date}/{time}/... variables seeded into every workflow run so step templates can
    reference them without the caller declaring them. Filename-safe forms are used for the values
    that commonly land in write_file paths ({date}/{time}/{today}/{timestamp} contain no ':' or '/'
    -> safe on Windows too); {now}/{datetime}/{iso_date} keep human/ISO formatting."""
    # Resolve "now" in the user's timezone (single source of truth) so {date}/{now}/... in
    # workflow step templates match the user's wall clock, not the server's.
    from vaf.core.user_time import user_now
    now = user_now(username)
    return {
        "date": now.strftime("%Y-%m-%d"),
        "today": now.strftime("%Y-%m-%d"),
        "current_date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H-%M"),
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "iso_date": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "timestamp": now.strftime("%Y%m%d_%H%M%S"),
        "year": now.strftime("%Y"),
        "month": now.strftime("%m"),
        "day": now.strftime("%d"),
    }


class WorkflowEngine:
    """
    Executes multi-step workflows with automatic output chaining.
    
    Example:
        engine = WorkflowEngine(tools)
        result = engine.execute([
            WorkflowStep("web_search", "{query}", "research"),
            WorkflowStep("coding_agent", "Create code based on:\n{research}", "code"),
            WorkflowStep("write_file", '{"path": "output.py", "content": "{code}"}', "saved"),
        ], variables={"query": "Python web scraping"})
    """
    
    def __init__(
        self,
        tools: Dict[str, Any],
        callback: Callable = None,
        user_scope_id: Optional[str] = None,
        username: Optional[str] = None,
    ):
        """
        Initialize the workflow engine.

        Args:
            tools: Dict mapping tool names to tool instances
            callback: Optional callback for progress updates
            user_scope_id: Optional user scope UUID for tool isolation (memory, calendar, etc.)
            username: Optional username for tools that need it (messaging, contacts, etc.)
        """
        self.tools = tools
        self.callback = callback or (lambda *args: None)
        self.user_scope_id = user_scope_id
        self.username = username or "admin"

        # Per-step output validation (opt-in). Set by the caller (run_temp) when an agent is
        # available: _validate_step(goal, result, tool, user_intent) -> (fulfilled, retry_hint).
        # Left None elsewhere (persistent/scheduled workflows) → validation simply does not run.
        self._validate_step: Optional[Callable[[str, str, str, str], tuple]] = None
        self._workflow_user_intent: str = ""

        # Initialize context manager for workflow execution (like main agent)
        from vaf.core.context import ContextManager
        self.context_manager = ContextManager(max_tokens=8192)
    
    def execute(
        self,
        steps: List[WorkflowStep],
        variables: Dict[str, Any] = None,
        stop_on_error: bool = True,
        check_stop: Optional[Callable[[], bool]] = None,
        wait_for_subagents: bool = False,
    ) -> WorkflowResult:
        """
        Execute a workflow with the given steps.

        Args:
            steps: List of workflow steps to execute
            variables: Initial variables (user inputs)
            stop_on_error: Stop workflow on first error (default: True)
            check_stop: Optional callback; if it returns True, workflow aborts (e.g. user clicked Stop).
            wait_for_subagents: when True (synchronous run_temp workflows), heavy sub-agent
                steps run as **killable child processes** and the engine waits for their IPC
                result bounded + stop-aware, instead of running them in-process. This avoids
                the in-process hang and the abandoned-thread-holds-the-LLM-lock problem.
                browser_agent stays in-process (it self-manages its own stop/limits).

        Returns:
            WorkflowResult with all outputs and status
        """
        # Store defaults for use in template resolution (defaults not passed for automations)
        self._workflow_defaults = {}

        start_time = time.time()
        # `variables` should be a mapping, but the LLM sometimes emits a JSON string or a
        # list. `dict(<list>)` raises "dictionary update sequence element #0 has length N;
        # 2 is required" and aborts the whole workflow — coerce defensively so a malformed
        # value degrades to "missing variable" (a clear per-step error) instead of a crash.
        _vars = variables or {}
        if isinstance(_vars, str):
            try:
                import json as _json
                _vars = _json.loads(_vars)
            except Exception:
                _vars = {}
        if not isinstance(_vars, dict):
            _vars = {}
        outputs: Dict[str, Any] = dict(_vars)
        # Merge defaults into outputs if not already present
        for key, value in self._workflow_defaults.items():
            if key not in outputs:
                outputs[key] = value

        # ── Built-in temporal variables ────────────────────────────────────────
        # LLM-generated workflows routinely reference {date}/{time}/etc. in step templates
        # (the automation generation prompt even demonstrates `..._{date}.html`), but nothing
        # ever provided a value — so a single placeholder killed the whole run with
        # "Missing variable: 'date'". Seed a fixed set of temporal built-ins here so they
        # ALWAYS resolve. setdefault() means a real user-supplied variable of the same name
        # always wins.
        for _k, _v in _temporal_builtins(self.username).items():
            outputs.setdefault(_k, _v)

        final_output = None
        error = None

        # ── Shared Workflow Project Path ───────────────────────────────────────
        # Generate ONE project directory for the entire workflow run. All coding_agent
        # and document_writer steps will use it automatically (via auto-inject below)
        # so each step doesn't create its own scattered directory.
        # Also exposed as {workflow_project_path} for use in step templates.
        _workflow_project_path: Optional[str] = None
        if "workflow_project_path" not in outputs:
            try:
                import re as _re
                from vaf.core.platform import Platform as _Platform
                _docs = _Platform.documents_dir()

                # User-scope prefix + per-chat folder — same logic as
                # coder._generate_project_directory
                _user_prefix = ""
                _session_folder = ""
                try:
                    from vaf.core.subagent_ipc import get_current_session_id as _get_sid2
                    _sid2 = _get_sid2()
                    if _sid2:
                        _session_folder = _re.sub(r'[^a-zA-Z0-9_-]', '', _sid2)[:32]
                        from vaf.core.session import SessionManager as _SM2
                        _s2 = _SM2().load(_sid2)
                        _uid2 = (_s2.metadata or {}).get("user_scope_id", "")
                        if _uid2:
                            _user_prefix = _uid2[:8]
                except Exception:
                    pass
                if not _user_prefix and self.user_scope_id:
                    _user_prefix = self.user_scope_id[:8]

                _proj_root = os.path.join(
                    _docs, "VAF_Projects",
                    *(p for p in (_user_prefix, _session_folder) if p)
                )

                # Derive a clean directory name from the workflow name
                _wf_label = getattr(self, "_workflow_name", "") or "Workflow"
                _wf_dir = _re.sub(r'[^a-zA-Z0-9 _-]', '', _wf_label).strip()[:40] or "Workflow"
                _workflow_project_path = os.path.join(_proj_root, _wf_dir)
                os.makedirs(_workflow_project_path, exist_ok=True)
                outputs["workflow_project_path"] = _workflow_project_path
            except Exception:
                pass
        else:
            _workflow_project_path = outputs["workflow_project_path"]

        # Don't show "Starting workflow" message - will show Step 1/N instead
        
        # Get workflow-level debug logger
        workflow_debug_lg = None
        try:
            from vaf.core.subagent_debug import get_subagent_logger_from_env
            workflow_debug_lg = get_subagent_logger_from_env()
            if workflow_debug_lg:
                workflow_debug_lg.event("workflow_execute_start",
                                       workflow_name=getattr(self, "_workflow_name", ""),
                                       num_steps=len(steps),
                                       variables=list(variables.keys()) if variables else [])
        except Exception:
            pass

        # While-loop with manual index to support on_success/on_failure jumps.
        # Infinite-loop guard: allow at most len(steps) * 3 iterations.
        _step_idx = 0          # 0-based current step index
        _jump_count = 0        # tracks non-sequential advances
        _max_jumps = len(steps) * 3

        while _step_idx < len(steps):
            if _jump_count > _max_jumps:
                UI.error(f"  [FAIL] Workflow aborted: too many step jumps (possible infinite loop). Limit={_max_jumps}")
                error = "Workflow aborted: too many step jumps (possible infinite loop)"
                break

            # User requested stop (e.g. Stop button in Web UI)
            if check_stop and check_stop():
                UI.event("System", "Workflow stopped by user", style="warning")
                error = "Stopped by user"
                break

            step = steps[_step_idx]
            i = _step_idx + 1   # 1-based display index (matches original)
            step_start = time.time()
            step.status = StepStatus.RUNNING

            # Debug log step start
            if workflow_debug_lg:
                workflow_debug_lg.event("workflow_step_start", step_index=i, step_tool=step.tool,
                                       step_description=step.description[:100] if step.description else "")

            # Check condition if specified
            if step.condition and not self._evaluate_condition(step.condition, outputs):
                step.status = StepStatus.SKIPPED
                self.callback("skip", step, i, len(steps))
                UI.event("Workflow", f"Step {i}/{len(steps)}: {step.tool} [Skipped]", style="dim")
                if workflow_debug_lg:
                    workflow_debug_lg.event("workflow_step_skipped", step_index=i, step_tool=step.tool)
                _step_idx += 1
                continue

            # Progress callback
            try:
                from vaf.core.log_helper import append_domain_log
                append_domain_log("webui", f"[engine.execute] BEFORE_CALLBACK step={i}/{len(steps)} tool={step.tool}")
            except Exception:
                pass
            self.callback("start", step, i, len(steps))

            # Show workflow progress (like "Step 1/2" display)
            step_desc = step.description if step.description else step.tool
            UI.event(f"Step {i}/{len(steps)}", step_desc, style="bold cyan")
            
            # Check if tool exists
            if step.tool not in self.tools:
                step.status = StepStatus.FAILED
                step.error = f"Tool not found: {step.tool}"
                error = step.error
                UI.error(f"  → {step.error}")

                if stop_on_error and not step.optional and not step.on_failure:
                    break
                _step_idx = self._branch_step(_step_idx, step, steps, outputs, error)
                _jump_count += 1
                continue
            
            # Build tool args (prefer args_template to avoid fragile JSON templating)
            # Get defaults from workflow template if available
            defaults = getattr(self, '_workflow_defaults', {})
            try:
                if step.args_template is not None:
                    args: Dict[str, Any] = {}
                    # Debug: Prepare variable info for coding_agent
                    available_vars = list(outputs.keys()) if step.tool == "coding_agent" else []
                    
                    for k, v in step.args_template.items():
                        if isinstance(v, str):
                            # Debug: Show variable resolution for coding_agent
                            if step.tool == "coding_agent" and k == "task":
                                UI.event("System", f"Resolving variables in {step.tool} task parameter...", style="dim")
                                if available_vars:
                                    UI.event("System", f"Available variables: {', '.join(available_vars)}", style="dim")
                                # Show template before resolution
                                template_preview = v[:200] + "..." if len(v) > 200 else v
                                UI.event("System", f"Template (before): {template_preview}", style="dim")
                            
                            resolved_value = self._resolve_template(v, outputs, defaults)
                            args[k] = resolved_value
                            
                            # Debug: Show resolved value for coding_agent
                            if step.tool == "coding_agent" and k == "task":
                                resolved_preview = resolved_value[:300] + "..." if len(resolved_value) > 300 else resolved_value
                                UI.event("System", f"Resolved task (first 300 chars): {resolved_preview}", style="dim")
                                # Show variable sizes
                                if available_vars:
                                    for var_name in available_vars:
                                        var_value = str(outputs.get(var_name, ""))
                                        var_size = len(var_value)
                                        UI.event("System", f"  Variable '{var_name}': {var_size} chars", style="dim")
                        else:
                            args[k] = v
                else:
                    resolved_input = self._resolve_template(step.input_template, outputs, defaults)
            except KeyError as e:
                step.status = StepStatus.FAILED
                step.error = f"Missing variable: {e}"
                error = step.error
                UI.error(f"  → {step.error}")
                if stop_on_error and not step.optional and not step.on_failure:
                    break
                _step_idx = self._branch_step(_step_idx, step, steps, outputs, error)
                _jump_count += 1
                continue
            
            # Execute the tool
            try:
                tool = self.tools[step.tool]

                # Parse input - could be explicit args_template, JSON dict, or simple string
                if step.args_template is None:
                    if resolved_input.strip().startswith("{") and resolved_input.strip().endswith("}"):
                        import json
                        try:
                            args = json.loads(resolved_input)
                        except json.JSONDecodeError:
                            # Not valid JSON, treat as single argument
                            args = self._infer_args(step.tool, resolved_input)
                    else:
                        args = self._infer_args(step.tool, resolved_input)
                
                # ═══════════════════════════════════════════════════════════════
                # CONTEXT MANAGEMENT: Truncate large inputs before passing to tools
                # ═══════════════════════════════════════════════════════════════
                # For tools that accept large text inputs (like coding_agent),
                # truncate very large variable values to prevent context overflow.
                # Exceptions: write_file "content" must NOT be truncated (full documents).
                MAX_INPUT_SIZE = 5000  # Max chars per input parameter
                NO_TRUNCATE = (("write_file", "content"),)  # Full content required
                args_snapshot = {k: v for k, v in args.items()}  # Snapshot for retry
                
                for key, value in args.items():
                    if (step.tool, key) in NO_TRUNCATE:
                        continue
                    if isinstance(value, str) and len(value) > MAX_INPUT_SIZE:
                        truncated = value[:MAX_INPUT_SIZE] + f"\n\n[... {len(value) - MAX_INPUT_SIZE} more characters truncated to prevent context overflow ...]"
                        UI.event("Workflow", f"  [INFO] Truncated {key} input: {len(value)} → {MAX_INPUT_SIZE} chars", style="dim")
                        args[key] = truncated
                
                # ── Variable Anchoring: pin original vars to every sub-agent task ──
                # Prevents "Stille Post" (telephone game) — original workflow variables
                # are injected as IMMUTABLE DESIGN PILLARS so later steps can't drift.
                _ANCHOR_TOOLS = {"coding_agent", "research_agent", "document_writer", "librarian_agent"}
                _orig_vars = {k: v for k, v in (variables or {}).items()}
                if step.tool in _ANCHOR_TOOLS and _orig_vars and "task" in args:
                    _anchor_lines = "\n".join(
                        f"  {k}: {str(v)[:150]}" for k, v in _orig_vars.items()
                    )
                    _correction_hint = getattr(step, '_assert_correction', None)
                    _correction_block = (
                        f"\n\n[CORRECTION REQUIRED — Previous attempt failed]\n"
                        f"{_correction_hint}"
                        if _correction_hint else ""
                    )
                    args["task"] = (
                        f"## IMMUTABLE DESIGN PILLARS — DO NOT DEVIATE FROM THESE\n"
                        f"{_anchor_lines}\n\n"
                        f"## STRICT FACTUAL DATA POLICY\n"
                        f"NEVER invent, guess, or hallucinate specific facts (names, IDs, dates, numbers, companies).\n"
                        f"If a specific fact cannot be confirmed via search results or tool output:\n"
                        f"  Write exactly: [DATA NOT FOUND: <what was not found>]\n"
                        f"A short accurate output is always better than a longer hallucinated one.\n\n"
                        f"---\n\n"
                        + args["task"]
                        + _correction_block
                    )

                # Correction hint for non-anchor validatable tools (document_agent, browser_agent):
                # the anchor block above only injects for anchor tools. Append the previous-attempt
                # correction to whichever primary instruction arg the tool uses, so a validation
                # retry actually carries the fix forward.
                _corr_hint = getattr(step, '_assert_correction', None)
                if _corr_hint and step.tool in VALIDATABLE_TOOLS and step.tool not in _ANCHOR_TOOLS:
                    for _ck in ("task", "input", "prompt", "instruction"):
                        if isinstance(args.get(_ck), str):
                            args[_ck] = (
                                args[_ck]
                                + f"\n\n[CORRECTION REQUIRED — Previous attempt did not fulfil the goal]\n{_corr_hint}"
                            )
                            break

                # Snapshot outputs before tool execution (for retry on failure)
                outputs_snapshot = {k: v for k, v in outputs.items()}
                
                # Run the tool with retry logic (like main agent)
                #
                # NOTE: Some tools (e.g. research_agent) use Rich Live/ANSI animations.
                # When running inside the workflow engine, those animations can spam the
                # output because workflow execution isn't always a "true" interactive TTY.
                # We set an env flag so tools can gracefully switch to a non-Live mode.
                prev_in_workflow = os.environ.get("VAF_IN_WORKFLOW")
                os.environ["VAF_IN_WORKFLOW"] = "1"

                # Sub-agent debug logging context for in-workflow sub-agent tools.
                # These tools may run in-process (not via `subagent run`), so we set
                # VAF_AGENT_TYPE/VAF_TASK_ID here to enable per-run logs.
                prev_agent_type = os.environ.get("VAF_AGENT_TYPE")
                prev_task_id = os.environ.get("VAF_TASK_ID")
                prev_in_subagent_term = os.environ.get("VAF_IN_SUBAGENT_TERMINAL")
                prev_spawn_browser = os.environ.get("VAF_SPAWN_BROWSER_SUBAGENT")
                subagent_step_task_id = None
                is_subagent_tool = step.tool in ("coding_agent", "librarian_agent", "research_agent")
                # In wait-mode these heavy sub-agents run as KILLABLE CHILD PROCESSES
                # (spawn → IPC wait), not in-process. document_agent is spawnable too (it
                # isn't in is_subagent_tool, so it already spawns). browser_agent only spawns
                # when opted-in via VAF_SPAWN_BROWSER_SUBAGENT (set below) so standalone
                # browser usage stays in-process.
                _spawnable = step.tool in (
                    "coding_agent", "librarian_agent", "research_agent", "document_agent",
                    "browser_agent",
                )
                _spawn_and_wait = bool(wait_for_subagents) and _spawnable
                if _spawn_and_wait and step.tool == "browser_agent":
                    # Tell BrowserAgentTool.run to spawn a killable child process for this step.
                    os.environ["VAF_SPAWN_BROWSER_SUBAGENT"] = "1"
                    os.environ.pop("VAF_IN_SUBAGENT_TERMINAL", None)
                if is_subagent_tool:
                    subagent_step_task_id = f"{step.tool}-{i}-{str(uuid.uuid4())[:6]}"
                    os.environ["VAF_AGENT_TYPE"] = step.tool
                    os.environ["VAF_TASK_ID"] = subagent_step_task_id
                    if _spawn_and_wait:
                        # WAIT-MODE: force a child-process spawn (do NOT run in-process).
                        os.environ.pop("VAF_IN_SUBAGENT_TERMINAL", None)
                    else:
                        # Legacy in-process path: prevent nested terminal spawning.
                        if os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "").strip().lower() not in ("1", "true", "yes"):
                            os.environ["VAF_IN_SUBAGENT_TERMINAL"] = "1"
                    try:
                        from vaf.core.subagent_debug import get_subagent_logger_from_env
                        lg = get_subagent_logger_from_env()
                        if lg:
                            lg.event(
                                "workflow_subagent_step_start",
                                workflow_name=getattr(self, "_workflow_name", ""),
                                step_index=i,
                                total_steps=len(steps),
                            )
                    except Exception:
                        pass
                
                # User isolation: inject user_scope_id/username for tools that need it (same as agent)
                def _inject_user_scope(tool_name: str, a: Dict[str, Any]) -> None:
                    if self.user_scope_id is not None or self.username:
                        if tool_name in ("memory_save", "memory_search"):
                            a["user_scope_id"] = self.user_scope_id
                        elif tool_name == "update_user_identity":
                            a["username"] = self.username
                        elif tool_name in ("send_telegram", "send_discord", "send_slack", "send_whatsapp"):
                            a["username"] = self.username
                            a["user_scope_id"] = self.user_scope_id
                        elif tool_name in ("whatsapp_inbox", "find_whatsapp_messages", "read_whatsapp_chat", "whatsapp_call"):
                            a["username"] = self.username
                            a["user_scope_id"] = self.user_scope_id
                        elif tool_name in ("list_contacts", "get_contact", "create_contact", "update_contact", "delete_contact"):
                            a["username"] = self.username
                            a["user_scope_id"] = self.user_scope_id
                        elif tool_name in ("mail_inbox", "read_mail", "find_mail", "mark_mail_answered", "label_mail", "list_email_accounts", "send_mail"):
                            a["username"] = self.username
                            a["user_scope_id"] = self.user_scope_id
                        elif tool_name in ("add_automation_note", "add_automation_todo", "list_automation_notes", "list_automation_todos", "delete_automation_note", "delete_automation_todo"):
                            a["user_scope_id"] = self.user_scope_id
                        elif tool_name in ("list_calendar_events", "create_calendar_event", "update_calendar_event", "delete_calendar_event"):
                            a["username"] = self.username
                            a["user_scope_id"] = self.user_scope_id

                # Retry logic for context errors (like main agent)
                max_retries = 3
                retry_count = 0
                result = None

                # Bounded execution: a single tool/sub-agent call may never block the
                # worker forever. Pick a wall-clock timeout (sub-agents get more) and poll
                # the user's Stop flag during the call so a hung step can't freeze the
                # backend and the Stop button works mid-step. See vaf/core/bounded_run.py.
                from vaf.core.bounded_run import (
                    run_bounded, is_abort_sentinel, SELF_SUPERVISED_TOOLS, agent_timeout_seconds,
                )
                from vaf.core.config import Config as _CfgTO
                # As a WORKFLOW STEP, browser_agent must be BOUNDED: it runs in-process and
                # blocks the workflow, so leaving it unbounded lets a hung browser freeze the
                # whole workflow (and the main agent waiting on it). It gets a generous budget
                # (browser_timeout_seconds) + its own _stop_monitor/max_steps. Only the
                # workflow orchestrators stay self-supervised here (they are never steps in
                # practice). Standalone browser_agent (via execute_tool) stays self-supervised.
                _self_supervised = (step.tool in SELF_SUPERVISED_TOOLS) and step.tool != "browser_agent"
                _step_timeout = agent_timeout_seconds(step.tool)   # per-agent budget
                _stop_poll = float(_CfgTO.get("tool_stop_poll_seconds", 0.5))

                while retry_count < max_retries:
                    _inject_user_scope(step.tool, args)
                    # Route file-producing steps into the shared project dir. Inside the
                    # retry loop (idempotent) so a context-error retry keeps the routing.
                    _inject_workflow_paths(step.tool, args, _workflow_project_path)
                    try:
                        if _spawn_and_wait:
                            # Spawn the sub-agent as a killable child process (returns
                            # [SUBAGENT_ASYNC:id] fast), then wait for its IPC result bounded
                            # + stop-aware. Isolated → no in-process hang, and no abandoned
                            # thread can hold the main agent's LLM lock.
                            _spawn_out = tool.run(**args)
                            result = self._await_subagent(
                                _spawn_out, step.tool, check_stop, _step_timeout, _stop_poll,
                            )
                        elif _self_supervised:
                            result = tool.run(**args)   # self-managed stop + internal limits
                        else:
                            result = run_bounded(
                                lambda: tool.run(**args),
                                timeout=_step_timeout,
                                stop_check=check_stop,
                                poll=_stop_poll,
                                label=step.tool,
                            )
                        
                        # Check if result contains context overflow error (like main agent)
                        result_str = str(result)
                        is_context_error_in_result = (
                            "context" in result_str.lower() and 
                            ("exceed" in result_str.lower() or "size" in result_str.lower() or "token" in result_str.lower())
                        ) or (
                            "400" in result_str and "context" in result_str.lower()
                        )
                        
                        # If context error in result, retry with more aggressive truncation
                        if is_context_error_in_result:
                            retry_count += 1
                            if retry_count < max_retries:
                                UI.event("Workflow", f"  [RETRY {retry_count}/{max_retries}] Context overflow in result, truncating inputs...", style="warning")
                                
                                # Restore args from snapshot
                                args = {k: v for k, v in args_snapshot.items()}
                                
                                # More aggressive truncation on retry (like main agent's aggressive compression)
                                new_max = MAX_INPUT_SIZE // (retry_count + 1)
                                for key, value in args.items():
                                    if isinstance(value, str) and len(value) > new_max:
                                        args[key] = value[:new_max] + f"\n\n[... {len(value) - new_max} more characters truncated (retry {retry_count}) ...]"
                                        UI.event("Workflow", f"  [RETRY] Aggressively truncated {key}: {len(value)} → {new_max} chars", style="dim")
                                
                                # Restore outputs from snapshot before retry
                                outputs = {k: v for k, v in outputs_snapshot.items()}
                                
                                # Continue retry loop
                                continue
                            else:
                                # Max retries reached - treat as error
                                step.status = StepStatus.FAILED
                                step.error = f"Context size error after {max_retries} retries"
                                step.result = result
                                step.duration = time.time() - step_start
                                error = step.error
                                
                                UI.error(f"  [FAIL] {step.error}")
                                self.callback("error", step, i, len(steps))
                                
                                if stop_on_error and not step.optional:
                                    break
                                continue
                        
                        # Success - exit retry loop
                        break
                        
                    except Exception as e:
                        error_str = str(e).lower()
                        # Check if it's a context size error (like main agent handles 500)
                        is_context_error = (
                            "context" in error_str and 
                            ("exceed" in error_str or "size" in error_str or "token" in error_str)
                        ) or (
                            hasattr(e, 'response') and 
                            hasattr(e.response, 'status_code') and 
                            e.response.status_code == 500 and
                            "context" in str(e.response.text or "").lower()
                        )
                        
                        if is_context_error:
                            retry_count += 1
                            if retry_count < max_retries:
                                # Restore from snapshot and truncate more aggressively (like main agent)
                                UI.event("Workflow", f"  [RETRY {retry_count}/{max_retries}] Context overflow detected, truncating inputs...", style="warning")
                                
                                # Restore args from snapshot
                                args = {k: v for k, v in args_snapshot.items()}
                                
                                # More aggressive truncation on retry (like main agent's aggressive compression)
                                new_max = MAX_INPUT_SIZE // (retry_count + 1)
                                for key, value in args.items():
                                    if isinstance(value, str) and len(value) > new_max:
                                        args[key] = value[:new_max] + f"\n\n[... {len(value) - new_max} more characters truncated (retry {retry_count}) ...]"
                                        UI.event("Workflow", f"  [RETRY] Aggressively truncated {key}: {len(value)} → {new_max} chars", style="dim")
                                
                                # Restore outputs from snapshot before retry
                                outputs = {k: v for k, v in outputs_snapshot.items()}
                                
                                continue
                            else:
                                # Max retries reached
                                raise Exception(f"Context size error after {max_retries} retries: {e}")
                        else:
                            # Not a context error - re-raise immediately
                            raise
                
                # Sub-agent step end (result summary only)
                if is_subagent_tool:
                    try:
                        from vaf.core.subagent_debug import get_subagent_logger_from_env, summarize_result
                        lg = get_subagent_logger_from_env()
                        if lg:
                            lg.event(
                                "workflow_subagent_step_end",
                                ok=True,
                                workflow_name=getattr(self, "_workflow_name", ""),
                                step_index=i,
                                total_steps=len(steps),
                                **summarize_result(result),
                            )
                    except Exception:
                        pass

                # Cleanup environment variable (after while loop)
                if prev_in_workflow is None:
                    os.environ.pop("VAF_IN_WORKFLOW", None)
                else:
                    os.environ["VAF_IN_WORKFLOW"] = prev_in_workflow

                # Restore sub-agent debug env vars (avoid leaking into other tools)
                if is_subagent_tool:
                    if prev_agent_type is None:
                        os.environ.pop("VAF_AGENT_TYPE", None)
                    else:
                        os.environ["VAF_AGENT_TYPE"] = prev_agent_type
                    if prev_task_id is None:
                        os.environ.pop("VAF_TASK_ID", None)
                    else:
                        os.environ["VAF_TASK_ID"] = prev_task_id
                    if prev_in_subagent_term is None:
                        os.environ.pop("VAF_IN_SUBAGENT_TERMINAL", None)
                    else:
                        os.environ["VAF_IN_SUBAGENT_TERMINAL"] = prev_in_subagent_term

                # Restore the browser-spawn opt-in flag (set for a browser_agent step above).
                if prev_spawn_browser is None:
                    os.environ.pop("VAF_SPAWN_BROWSER_SUBAGENT", None)
                else:
                    os.environ["VAF_SPAWN_BROWSER_SUBAGENT"] = prev_spawn_browser

                # ═══════════════════════════════════════════════════════════════
                # ASYNC SUB-AGENT HANDLING: Pause workflow and yield control
                # ═══════════════════════════════════════════════════════════════
                # If result contains async marker, save state and return immediately
                # The workflow will be resumed when the sub-agent finishes
                import re
                result_str_check = str(result) if result else ""
                async_match = re.search(r'\[SUBAGENT_ASYNC:([^:]+):([^\]]+)\]', result_str_check)
                
                if async_match:
                    task_id = async_match.group(1)
                    agent_type = async_match.group(2)
                    
                    # Generate workflow ID for tracking
                    workflow_id = str(uuid.uuid4())[:8]
                    
                    UI.event("Workflow", f"  ⏸️  Pausing workflow - {agent_type} [Task: {task_id}] running in background", style="cyan")
                    UI.info(f"  💡 You can continue using the main agent. Workflow will resume automatically.")
                    
                    from vaf.core.subagent_ipc import get_ipc, PausedWorkflow
                    
                    # Serialize current state
                    steps_data = []
                    for s in steps:
                        steps_data.append({
                            'tool': s.tool,
                            'input_template': s.input_template,
                            'output_name': s.output_name,
                            'description': s.description,
                            'optional': s.optional,
                            'condition': s.condition,
                            'args_template': s.args_template,
                            'on_success': s.on_success,
                            'on_failure': s.on_failure,
                            'validate': s.validate,
                            'status': s.status.value,
                            'result': s.result,
                            'error': s.error,
                            'duration': s.duration,
                        })
                    
                    # Save paused workflow state
                    paused_wf = PausedWorkflow(
                        workflow_id=workflow_id,
                        waiting_for_task_id=task_id,
                        current_step_index=i - 1,  # 0-based index (i is 1-based)
                        outputs=outputs,
                        variables=dict(variables or {}),
                        steps_data=steps_data,
                        workflow_name=getattr(self, '_workflow_name', 'unknown'),
                        created_at=datetime.now().isoformat()
                    )
                    
                    ipc = get_ipc()
                    ipc.pause_workflow(paused_wf)
                    
                    # Return paused result - control goes back to user
                    return WorkflowResult(
                        success=False,  # Not complete yet
                        outputs=outputs,
                        final_output=None,
                        steps=steps,
                        total_duration=time.time() - start_time,
                        error=None,
                        paused=True,
                        workflow_id=workflow_id,
                        waiting_for_task=task_id
                    )
                
                # A bounded-run timeout/stop returns a sentinel string. Treat it as a hard
                # abort of the whole workflow — never branch or feed it onward.
                if is_abort_sentinel(result):
                    step.status = StepStatus.FAILED
                    step.error = str(result)
                    step.result = result
                    step.duration = time.time() - step_start
                    error = step.error
                    UI.error(f"  [ABORTED] {step.error}")
                    self.callback("error", step, i, len(steps))
                    break

                # Check if result indicates failure (even if no exception was raised)
                if result is None:
                    # Should not happen, but handle gracefully
                    step.status = StepStatus.FAILED
                    step.error = "Tool returned no result"
                    step.duration = time.time() - step_start
                    error = step.error
                    UI.error(f"  [FAIL] {step.error}")
                    self.callback("error", step, i, len(steps))
                    if stop_on_error and not step.optional and not step.on_failure:
                        break
                    _step_idx = self._branch_step(_step_idx, step, steps, outputs, error)
                    _jump_count += 1
                    continue

                result_str = str(result)
                # A tool that raised is wrapped by execute_tool as "Tool Error: …"; tools
                # also surface failures as "Security Error: …". These do NOT start with a
                # bare "Error:", so without the explicit prefixes below the step was scored
                # as a success and the workflow continued (and reported "completed") on top
                # of a failed step.
                _result_lower = result_str.lower()
                is_error_result = (
                    result_str.startswith("### ❌") or
                    result_str.startswith("❌") or
                    result_str.startswith("Error:") or
                    _result_lower.startswith("tool error:") or
                    _result_lower.startswith("security error:") or
                    "Task Failed" in result_str or
                    "task failed" in _result_lower or
                    (result_str.startswith("###") and "❌" in result_str[:200]) or
                    ("error:" in _result_lower and _result_lower.startswith("error"))
                )

                if is_error_result:
                    step.status = StepStatus.FAILED
                    step.error = f"Tool returned error message: {result_str[:200]}"
                    step.result = result
                    step.duration = time.time() - step_start
                    error = step.error

                    UI.error(f"  [FAIL] {step.error}")
                    self.callback("error", step, i, len(steps))

                    if stop_on_error and not step.optional and not step.on_failure:
                        break
                    _step_idx = self._branch_step(_step_idx, step, steps, outputs, error)
                    _jump_count += 1
                    continue

                # ── Assertion Gate: Selective Step-Retry ──────────────────────────
                if step.assertions:
                    _assert_retry = getattr(step, '_assert_retry_count', 0)
                    _assert_failures = []
                    for _a in step.assertions:
                        _op = "contains" if "contains" in _a else "not_contains"
                        _expected = self._resolve_template(_a[_op], outputs)
                        _in_result = _expected in str(result)
                        if _op == "contains" and not _in_result:
                            _assert_failures.append(_a.get("error", f"Output must contain: '{_expected}'"))
                        elif _op == "not_contains" and _in_result:
                            _assert_failures.append(_a.get("error", f"Output must not contain: '{_expected}'"))

                    if _assert_failures:
                        _msg = "; ".join(_assert_failures)
                        UI.event("Workflow", f"  [ASSERT FAIL] {_msg}", style="warning")

                        if _assert_retry < step.max_assertion_retries:
                            step._assert_retry_count = _assert_retry + 1
                            step._assert_correction = (
                                f"The following conditions must be met in your output:\n"
                                + "\n".join(f"  - {f}" for f in _assert_failures)
                            )
                            step.status = StepStatus.PENDING
                            outputs = {k: v for k, v in outputs_snapshot.items()}
                            # Stay on current step — _step_idx is not advanced
                            self.callback("start", step, i, len(steps))
                            continue
                        else:
                            UI.event("Workflow", f"  [ASSERT FAIL] Max retries reached — step failed", style="error")
                            step.status = StepStatus.FAILED
                            step.error = f"Assertion failed after {step.max_assertion_retries} retries: {_msg}"
                            step.duration = time.time() - step_start
                            error = step.error
                            self.callback("error", step, i, len(steps))
                            if stop_on_error and not step.optional and not step.on_failure:
                                break
                            _step_idx = self._branch_step(_step_idx, step, steps, outputs, error)
                            _jump_count += 1
                            continue

                # ── Validation Gate: does the output fulfil the step's goal? ───────
                # Opt-in (step.validate) and only for content/agent tools where a correction
                # retry can change the result. Unlike the assertion gate, exhausting the
                # retries does NOT fail the step — the last version is accepted and we move on.
                from vaf.core.config import Config as _CfgVal
                if (
                    step.validate
                    and step.tool in VALIDATABLE_TOOLS
                    and self._validate_step is not None
                    and _CfgVal.get("workflow_step_validation_enabled", True)
                ):
                    _val_retry = getattr(step, '_validation_retry_count', 0)
                    _val_max = int(_CfgVal.get("workflow_step_validation_max_retries", 3))
                    _goal = (step.description or step.input_template or "").strip()
                    try:
                        _ok, _hint = self._validate_step(
                            _goal, str(result), step.tool, self._workflow_user_intent
                        )
                    except Exception as _ve:
                        # Never let a validator error break the workflow — accept the result.
                        UI.event("Workflow", f"  [VALIDATE] validator error, accepting result: {_ve}", style="dim")
                        _ok, _hint = True, None

                    if not _ok and _val_retry < _val_max:
                        step._validation_retry_count = _val_retry + 1
                        step._assert_correction = (
                            _hint or "The output did not fulfil the step's goal. Redo it correctly."
                        )
                        step.status = StepStatus.PENDING
                        outputs = {k: v for k, v in outputs_snapshot.items()}
                        UI.event(
                            "Workflow",
                            f"  [VALIDATE RETRY {step._validation_retry_count}/{_val_max}] {step._assert_correction}",
                            style="warning",
                        )
                        # Stay on the current step (_step_idx not advanced) — re-run with correction.
                        self.callback("start", step, i, len(steps))
                        continue
                    elif not _ok:
                        UI.event(
                            "Workflow",
                            f"  [VALIDATE] {_val_max} retries reached — accepting current result and continuing",
                            style="warning",
                        )

                step.status = StepStatus.SUCCESS
                step.result = result
                step.duration = time.time() - step_start

                # Debug log step success
                if workflow_debug_lg:
                    workflow_debug_lg.event("workflow_step_complete", step_index=i, step_tool=step.tool,
                                           duration=step.duration, result_len=len(str(result)) if result else 0)

                # Store output for next steps — strip Rich link markup so that
                # file:// URLs like [link=file:///path%20with%20spaces]…[/link]
                # don't embed URL-encoded paths into subsequent steps' task text,
                # which would confuse the coder's path-extraction regex.
                outputs[step.output_name] = _strip_rich_links(str(result))
                final_output = result

                # Truncate for display
                display_result = str(result)[:100] + "..." if len(str(result)) > 100 else str(result)
                # Use ASCII markers for maximum terminal compatibility (avoid UnicodeEncodeError on some Windows consoles)
                UI.event("Workflow", f"  [OK] {step.output_name}: {display_result}", style="success")

                step.result = result
                step.status = StepStatus.SUCCESS
                step.duration = time.time() - step_start
                self.callback("success", step, i, len(steps))

                # Branching: on_success jump (optional)
                _step_idx = self._branch_step(_step_idx, step, steps, outputs, None)
                if step.on_success:
                    _jump_count += 1
                continue

            except Exception as e:
                step.status = StepStatus.FAILED
                step.error = str(e)
                step.duration = time.time() - step_start
                error = step.error

                UI.error(f"  [FAIL] Error: {step.error}")
                self.callback("error", step, i, len(steps))

                if stop_on_error and not step.optional and not step.on_failure:
                    break
                _step_idx = self._branch_step(_step_idx, step, steps, outputs, error)
                _jump_count += 1
                continue

        total_duration = time.time() - start_time

        # Determine overall success
        success = all(
            s.status in (StepStatus.SUCCESS, StepStatus.SKIPPED)
            for s in steps
        )

        # Debug log workflow end
        if workflow_debug_lg:
            workflow_debug_lg.event("workflow_execute_end", success=success,
                                   total_duration=total_duration, error=error[:200] if error else None,
                                   final_output_len=len(str(final_output)) if final_output else 0)

        if success:
            UI.event("Workflow", f"Completed in {total_duration:.1f}s", style="success")
        else:
            UI.event("Workflow", f"Failed after {total_duration:.1f}s", style="error")

        return WorkflowResult(
            success=success,
            outputs=outputs,
            final_output=final_output,
            steps=steps,
            total_duration=total_duration,
            error=error
        )

    def _await_subagent(self, spawn_output, tool_name: str, check_stop, timeout: float, poll: float):
        """
        Wait for a spawned sub-agent's IPC result, bounded + stop-aware.

        `spawn_output` is what the sub-agent tool returned after spawning a child process —
        normally a ``[SUBAGENT_ASYNC:<task_id>:<type>]`` marker. We poll the IPC results queue
        for that task. Because the sub-agent runs in its OWN process, the worker is never
        blocked in-process and nothing can hold the main agent's LLM lock.

        If the spawn fell back to in-process (no marker), `spawn_output` already IS the result.

        Guards (in order): the child's IPC result; the user's Stop; **liveness** — if the
        child stops sending heartbeats (~every 3 s) for `subagent_liveness_timeout_seconds`
        it is dead/stuck and gets killed + failed fast; and a worst-case hard `timeout`.
        On stop / liveness-fail / hard-cap the child process is actually KILLED (it runs in
        its own process, so this is a clean kill — no abandoned thread).

        If the spawn fell back to in-process (no marker), `spawn_output` already IS the result.
        Returns the sub-agent's result string, or a bounded_run sentinel on stop/timeout
        (the caller treats it as a hard workflow abort via ``is_abort_sentinel``).
        """
        import re as _re, time as _time
        from vaf.core.bounded_run import TIMEOUT_PREFIX, STOPPED_PREFIX
        m = _re.search(r'\[SUBAGENT_ASYNC:([^:]+):([^\]]+)\]', str(spawn_output or ""))
        if not m:
            return spawn_output  # ran in-process / spawn fell back → already the result
        task_id = m.group(1)
        try:
            from vaf.core.subagent_ipc import get_ipc, get_current_session_id, mark_engine_owned
            ipc = get_ipc()
        except Exception:
            return spawn_output  # IPC unavailable — degrade to the marker text

        try:
            from vaf.core.config import Config as _Cfg
            _liveness = float(_Cfg.get("subagent_liveness_timeout_seconds", 60))
        except Exception:
            _liveness = 60.0
        _sid = None
        try:
            _sid = get_current_session_id()
        except Exception:
            _sid = None

        def _kill_child():
            """Actually terminate the spawned child process (it's a separate process)."""
            try:
                from vaf.core.platform import Platform
                if _sid:
                    Platform.stop_webui_subagent_processes(str(_sid))
            except Exception:
                pass

        deadline = _time.monotonic() + max(1.0, float(timeout))
        poll = max(0.1, float(poll))
        _last_zombie_check = 0.0
        _started = _time.monotonic()
        _last_status = 0.0
        while True:
            # Claim ownership each poll so the background main runner never steals this
            # step's result out from under this loop (see subagent_ipc.mark_engine_owned).
            mark_engine_owned(task_id)
            try:
                task = ipc.consume_result(task_id)
            except Exception:
                task = None
            if task is not None:
                # If the result is a failure (incl. a zombie failed by check_zombies below),
                # reap any lingering process; on success there is nothing to kill.
                if str(getattr(task, "status", "") or "") == "failed" or getattr(task, "error", None):
                    _kill_child()
                return getattr(task, "result", None) or getattr(task, "error", None) or ""

            # User pressed Stop → kill the child and abort.
            if check_stop is not None:
                try:
                    stop = bool(check_stop())
                except Exception:
                    stop = False
                if stop:
                    _kill_child()
                    try:
                        ipc.fail_task(task_id, "[USER_CANCELLED] Stopped by user via stop button.")
                    except Exception:
                        pass
                    return (f"{STOPPED_PREFIX} '{tool_name}' was cancelled by the user "
                            f"before it finished.")

            _now = _time.monotonic()
            # Liveness: no heartbeat for too long → check_zombies fails the stale task, which
            # we then consume above and reap. This is the primary guard ("no sign of life").
            if _now - _last_zombie_check >= 5.0:
                _last_zombie_check = _now
                try:
                    ipc.check_zombies(timeout_seconds=int(_liveness))
                except Exception:
                    pass

            # Watchdog status as terminal output: a workflow-step sub-agent has no tool bubble to
            # carry the inline watchdog, so surface its liveness here. The line goes to stdout →
            # workflow_output_stream → the Workflow Runtime terminal.
            if _now - _last_status >= 5.0:
                _last_status = _now
                _hb = None
                try:
                    from datetime import datetime as _dt
                    for _t in ipc.get_active_tasks():
                        if getattr(_t, "task_id", None) == task_id and getattr(_t, "last_heartbeat", None):
                            _hb = max(0, int((_dt.now() - _dt.fromisoformat(_t.last_heartbeat)).total_seconds()))
                            break
                except Exception:
                    _hb = None
                _hb_txt = (f"no heartbeat for {_hb}s" if (_hb is not None and _hb > int(_liveness))
                           else (f"heartbeat {_hb}s ago" if _hb is not None else "starting…"))
                try:
                    UI.event("Watchdog", f"{tool_name} · running {int(_now - _started)}s · {_hb_txt}", style="dim")
                except Exception:
                    pass

            # Worst-case hard cap.
            if _now >= deadline:
                _kill_child()
                try:
                    ipc.fail_task(task_id, f"[TIMEOUT] sub-agent exceeded {int(timeout)}s.")
                except Exception:
                    pass
                return (f"{TIMEOUT_PREFIX} '{tool_name}' did not finish within "
                        f"{int(timeout)}s (worst-case cap) and was killed.")

            _time.sleep(poll)

    def resume_workflow(self, paused_wf, subagent_result: str) -> WorkflowResult:
        """
        Resume a paused workflow with the sub-agent's result.
        
        Args:
            paused_wf: The saved workflow state
            subagent_result: The result from the sub-agent
            
        Returns:
            WorkflowResult with completion status
        """
        from vaf.core.subagent_ipc import get_ipc, PausedWorkflow
        
        UI.event("Workflow", f"▶️  Resuming workflow [{paused_wf.workflow_id}]...", style="bold cyan")
        
        # Restore steps from saved state
        steps: List[WorkflowStep] = []
        for step_data in paused_wf.steps_data:
            step = WorkflowStep(
                tool=step_data['tool'],
                input_template=step_data['input_template'],
                output_name=step_data['output_name'],
                description=step_data.get('description', ''),
                optional=step_data.get('optional', False),
                condition=step_data.get('condition'),
                args_template=step_data.get('args_template'),
                on_success=step_data.get('on_success'),
                on_failure=step_data.get('on_failure'),
                validate=step_data.get('validate', False),
            )
            # Restore status
            step.status = StepStatus(step_data['status'])
            step.result = step_data.get('result')
            step.error = step_data.get('error')
            step.duration = step_data.get('duration', 0.0)
            steps.append(step)
        
        # Restore outputs and add the sub-agent result
        outputs = dict(paused_wf.outputs)
        current_step = steps[paused_wf.current_step_index]
        
        # Update the current step with the result
        current_step.status = StepStatus.SUCCESS
        current_step.result = subagent_result
        outputs[current_step.output_name] = subagent_result
        
        UI.event("Workflow", f"  ✓ Got result for step: {current_step.tool}", style="success")
        
        # Remove from paused workflows
        ipc = get_ipc()
        ipc.remove_paused_workflow(paused_wf.workflow_id)
        
        # Continue with remaining steps
        remaining_steps = steps[paused_wf.current_step_index + 1:]
        
        if not remaining_steps:
            # Workflow is complete
            UI.event("Workflow", "Completed!", style="success")
            return WorkflowResult(
                success=True,
                outputs=outputs,
                final_output=subagent_result,
                steps=steps,
                total_duration=0.0,  # Can't track across pause
                error=None
            )
        
        # Execute remaining steps
        UI.event("Workflow", f"Continuing with {len(remaining_steps)} remaining steps...", style="info")
        
        # Create new steps list with completed + remaining
        remaining_result = self.execute(
            remaining_steps,
            variables=outputs,  # Use all outputs as variables
            stop_on_error=True
        )
        
        # If remaining steps also paused, propagate that
        if remaining_result.paused:
            return remaining_result
        
        # Merge results
        all_steps = steps[:paused_wf.current_step_index + 1] + remaining_result.steps
        all_outputs = {**outputs, **remaining_result.outputs}
        
        return WorkflowResult(
            success=remaining_result.success,
            outputs=all_outputs,
            final_output=remaining_result.final_output,
            steps=all_steps,
            total_duration=remaining_result.total_duration,
            error=remaining_result.error
        )
    
    def _resolve_template(self, template: str, variables: Dict[str, Any], defaults: Dict[str, Any] = None) -> str:
        """
        Replace {variable} placeholders with actual values.
        
        Supports:
        - Simple: {query}
        - Nested: {step1.field}
        - Default: {var|default_value}
        """
        if defaults is None:
            defaults = {}
        
        def replacer(match):
            key = match.group(1)
            
            # Handle default values: {var|default}
            if "|" in key:
                key, default = key.split("|", 1)
                return str(variables.get(key.strip(), default.strip()))
            
            # Handle nested access: {step.field}
            if "." in key:
                parts = key.split(".")
                value = variables
                for part in parts:
                    if isinstance(value, dict):
                        value = value.get(part)
                    else:
                        value = getattr(value, part, None)
                    if value is None:
                        raise KeyError(key)
                return str(value)
            
            # Simple variable - check variables first, then defaults
            if key in variables:
                return str(variables[key])
            elif key in defaults:
                return str(defaults[key])
            else:
                # Non-fatal: a single unknown/hallucinated placeholder must not kill the whole
                # workflow (it used to raise KeyError -> "Missing variable: '...'" -> Workflow
                # Failed). Pass the literal "{key}" through so the template stays debuggable and a
                # downstream coding_agent can still see/handle it. Common temporal vars
                # ({date}/{time}/...) are seeded in execute(), so this only fires for genuinely
                # unknown names. Nested {a.b} still raises above and is handled by the caller.
                return "{" + key + "}"
        
        return re.sub(r"\{([^}]+)\}", replacer, template)
    
    def _infer_args(self, tool_name: str, value: str) -> Dict[str, Any]:
        """
        Infer the argument name for a tool based on its expected parameters.
        """
        # Common tool argument mappings
        ARG_MAPPINGS = {
            "web_search": "query",  # deep=False injected separately below
            "webfetch": "url",
            "coding_agent": "task",
            "librarian_agent": "task",
            "document_agent": "task",
            "document_writer": "task",
            "browser_agent": "task",
            "research_agent": "topic",
            "read_file": "path",
            "write_file": "content",  # Usually needs path too
            "bash": "command",
            "list_files": "path",
            "move_file": "source",
        }
        
        arg_name = ARG_MAPPINGS.get(tool_name, "input")
        args = {arg_name: value}
        # In workflow context, disable per-result LLM synthesis for web_search.
        # deep=True triggers a query_llm() call per result which blocks on DeepSeek latency.
        if tool_name == "web_search":
            args["deep"] = False
        return args
    
    # ─── Branching helpers ──────────────────────────────────────────────────────

    def _find_step_index(self, steps: List["WorkflowStep"], target: str) -> int:
        """Return 0-based index of step whose output_name matches *target*.

        Falls back to treating *target* as a 0-based integer string (e.g. "3").
        Returns -1 when not found (caller should not jump).
        """
        for idx, s in enumerate(steps):
            if s.output_name == target:
                return idx
        try:
            numeric = int(target)
            if 0 <= numeric < len(steps):
                return numeric
        except (ValueError, TypeError):
            pass
        return -1

    def _branch_step(
        self,
        current_idx: int,
        step: "WorkflowStep",
        steps: List["WorkflowStep"],
        outputs: Dict[str, Any],
        error: Optional[str],
    ) -> int:
        """Return the next 0-based step index applying on_success / on_failure.

        - On success (error is None) and step.on_success set  → jump to target
        - On failure (error is not None) and step.on_failure set → jump to target,
          and reset step.status to SKIPPED so the workflow isn't marked failed.
        - Otherwise advance by 1 (normal sequential flow).
        """
        if error is None and step.on_success:
            target_idx = self._find_step_index(steps, step.on_success)
            if target_idx >= 0:
                UI.event("Workflow", f"  → branch on_success → {step.on_success}", style="dim")
                return target_idx
        elif error is not None and step.on_failure:
            target_idx = self._find_step_index(steps, step.on_failure)
            if target_idx >= 0:
                step.status = StepStatus.SKIPPED  # Don't count as fatal failure
                UI.event("Workflow", f"  → branch on_failure → {step.on_failure}", style="dim")
                return target_idx

        return current_idx + 1  # Default: next step

    # ─── Condition evaluation ────────────────────────────────────────────────────

    def _evaluate_condition(self, condition: str, variables: Dict[str, Any]) -> bool:
        """
        Evaluate a condition string with optional AND / OR / NOT logic.

        Supported syntax (evaluated left-to-right, no parentheses):
        - Simple truthy:          ``{var}``
        - NOT:                    ``NOT {var}``
        - AND:                    ``{a} AND {b}``
        - OR:                     ``{a} OR {b}``
        - Combined:               ``{a} AND NOT {b} OR {c}``
        - Legacy comparisons:     ``{var} == "value"``,  ``{var} != "value"``,
                                  ``{var} contains "text"``

        Each operand is resolved via _resolve_template() first and then
        tested for truthiness (non-empty, non-"false"/"0"/"no" string).
        """
        if not condition or not condition.strip():
            return True

        # --- tokenise into [(operator, operand_expr), ...] ------------------
        # Split on AND / OR boundaries (case-insensitive, surrounded by spaces)
        token_re = re.compile(r'\b(AND|OR)\b', re.IGNORECASE)
        parts = token_re.split(condition.strip())

        # parts alternates:  [expr, "AND"|"OR", expr, ...]
        tokens: list[tuple[str, str]] = []   # (operator, operand_expr)
        op = ""
        for part in parts:
            stripped = part.strip()
            if not stripped:
                continue
            if stripped.upper() in ("AND", "OR"):
                op = stripped.upper()
            else:
                tokens.append((op, stripped))
                op = ""

        if not tokens:
            return True

        def _eval_operand(expr: str) -> bool:
            """Resolve a single operand (may be prefixed with NOT)."""
            negated = False
            e = expr.strip()
            if e.upper().startswith("NOT "):
                negated = True
                e = e[4:].strip()

            # Resolve template variables
            resolved = self._resolve_template(e, variables)

            # Check for legacy comparison operators in the resolved string
            # e.g. "hello == hello" or "hello contains ell"
            for op_str in (" == ", " != ", " contains "):
                if op_str in resolved:
                    lhs, rhs = resolved.split(op_str, 1)
                    lhs = lhs.strip().strip('"')
                    rhs = rhs.strip().strip('"')
                    if op_str == " == ":
                        result = lhs == rhs
                    elif op_str == " != ":
                        result = lhs != rhs
                    else:  # contains
                        result = rhs in lhs
                    return not result if negated else result

            # Plain truthy check
            r = resolved.strip()
            if r.lower() in ("true", "yes", "1"):
                truthy = True
            elif r.lower() in ("false", "no", "0", ""):
                truthy = False
            else:
                truthy = bool(r)

            return not truthy if negated else truthy

        # --- evaluate tokens left-to-right ----------------------------------
        result = _eval_operand(tokens[0][1])   # first token, operator is ""
        for operator, expr in tokens[1:]:
            val = _eval_operand(expr)
            if operator == "AND":
                result = result and val
            else:  # OR
                result = result or val

        return result


# ═══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def create_workflow(template: Dict[str, Any]) -> List[WorkflowStep]:
    """
    Create workflow steps from a template definition.
    
    Args:
        template: Dict with 'steps' list
        
    Returns:
        List of WorkflowStep objects
    """
    steps = []
    for i, step_def in enumerate(template.get("steps", [])):
        steps.append(WorkflowStep(
            tool=step_def["tool"],
            input_template=step_def.get("input", ""),
            args_template=step_def.get("args"),
            output_name=step_def.get("output", f"step_{i+1}"),
            description=step_def.get("description", ""),
            optional=step_def.get("optional", False),
            condition=step_def.get("condition"),
            on_success=step_def.get("on_success"),
            on_failure=step_def.get("on_failure"),
            assertions=step_def.get("assertions"),
            max_assertion_retries=step_def.get("max_assertion_retries", 1),
            validate=step_def.get("validate", False),
        ))
    return steps

