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
pattern used by create_agent_tool and python_sandbox. This gives the tool
access to:
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
        "Create and execute workflows to solve complex multi-step tasks.\n\n"
        "Two primary modes:\n"
        "  run_temp — Define a workflow plan and execute it immediately. "
        "No file is saved; the plan is discarded after it runs. "
        "Use this when a task is too complex for a single tool call but you "
        "don't need to save the workflow for future use.\n"
        "  create   — Save a workflow permanently so it can be re-used via "
        "execute_workflow() or from the Workflows tab in the WebUI.\n\n"
        "Each step has an 'input' prompt (supports {variable} substitution) "
        "and a 'tool' to call (e.g. coding_agent, web_search, research_agent)."
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
        },
        "required": ["action"],
    }

    # Injected by execute_tool() — gives access to the live tool registry
    # and current session context for admin checks.
    _agent: Optional[Any] = None

    # ─────────────────────────────────────────────────────────────────────────

    def run(self, **kwargs) -> str:                         # noqa: C901
        action = (kwargs.get("action") or "").strip().lower()

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
        """
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
            if not s.get("input", "").strip():
                return f"Error: step {i + 1} is missing a non-empty 'input' field."
            normalised.append({
                "input":       s["input"],
                "tool":        s.get("tool", "coding_agent"),
                "output":      s.get("output") or f"step_{i + 1}_output",
                "description": s.get("description") or f"Step {i + 1}",
            })

        # Build WorkflowStep objects directly (no WORKFLOW_TEMPLATES mutation)
        try:
            from vaf.workflows.engine import WorkflowEngine, WorkflowStep, create_workflow
        except ImportError as exc:
            return f"Error: could not import workflow engine: {exc}"

        # Build as a template dict so create_workflow() can normalise it
        template_dict = {
            "name":   name,
            "description": desc,
            "triggers": [],
            "steps": normalised,
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

        engine = WorkflowEngine(
            tools         = tools,
            user_scope_id = user_scope_id,
            username      = username,
        )
        engine._workflow_name = name   # used in debug logs

        try:
            result = engine.execute(steps, variables=variables)
        except Exception as exc:
            return f"Error executing temporary workflow '{name}': {exc}"

        if result.paused:
            return (
                f"Temporary workflow '{name}' paused (async sub-agent). "
                "The result will arrive when the sub-agent completes."
            )

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
            normalised.append({
                "input":       s["input"],
                "tool":        s.get("tool", "coding_agent"),
                "output":      s.get("output") or f"step_{i + 1}_output",
                "description": s.get("description") or f"Step {i + 1}",
            })

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
            from vaf.workflows.templates import reload_workflows
            reload_workflows()
        except Exception as exc:
            return (
                f"Workflow '{workflow_id}' saved to disk but registry reload failed: {exc}. "
                "It will be available after the next server restart."
            )

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
