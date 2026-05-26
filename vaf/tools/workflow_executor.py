"""
Workflow Executor Tool

Allows the Main Agent to manually execute a workflow when:
- No workflow automatically matched the request
- Agent determines a specific workflow would be best
- User explicitly requests a workflow
"""

import os
import sys
import uuid

from vaf.tools.base import BaseTool


class ExecuteWorkflowTool(BaseTool):
    name = "execute_workflow"
    permission_level = "write"
    side_effect_class = "reversible"
    description = """Execute a specific workflow by ID.

    Use this tool when:
    - You've been informed that workflows are available but none matched automatically
    - You determine that a specific workflow would handle the request best
    - User's request clearly fits a workflow but wasn't auto-detected

    Available workflows are usually shown in the conversation context when relevant.

    Example usage:
    - execute_workflow(workflow_id="legal_contract_research", variables={"contract_type": "employment"})
    - execute_workflow(workflow_id="research_and_document", variables={"topic": "Docker deployment"})
    """

    parameters = {
        "type": "object",
        "properties": {
            "workflow_id": {
                "type": "string",
                "description": "ID of the workflow to execute (e.g., 'legal_contract_research', 'research_and_document')"
            },
            "variables": {
                "type": "object",
                "description": "Variables required by the workflow (e.g., {'topic': 'Docker', 'document_type': 'guide'})",
                "additionalProperties": True
            }
        },
        "required": ["workflow_id"]
    }

    def run(self, workflow_id: str, variables: dict = None, **kwargs) -> str:
        try:
            from vaf.workflows.templates import get_template, list_templates
            from vaf.workflows.engine import WorkflowEngine, create_workflow

            template = get_template(workflow_id)
            if not template:
                available = list_templates()
                workflow_list = "\n".join([f"- {w['id']}: {w['description']}" for w in available])
                return (
                    f"❌ Workflow '{workflow_id}' not found.\n\n"
                    f"Available workflows:\n{workflow_list}"
                )

            variables = variables or {}
            template_vars = template.get("variables", {})
            defaults = template.get("defaults", {})
            missing = []
            for var_name in template_vars.keys():
                if var_name not in variables:
                    if var_name in defaults:
                        variables[var_name] = defaults[var_name]
                    else:
                        missing.append(var_name)

            if missing:
                var_descriptions = "\n".join([
                    f"  - {var}: {template_vars[var]}"
                    for var in missing
                ])
                return (
                    f"❌ Workflow '{workflow_id}' requires these variables:\n"
                    f"{var_descriptions}\n\n"
                    f"Please provide them using the 'variables' parameter."
                )

            steps = create_workflow(template)

            # ── Tool registry (same set as CLI runner) ────────────────────────
            tools = {}
            _optional = [
                ("vaf.tools.search",       "WebSearchTool",      "web_search"),
                ("vaf.tools.webfetch",     "WebFetchTool",       "webfetch"),
                ("vaf.tools.filesystem",   "WriteFileTool",      "write_file"),
                ("vaf.tools.filesystem",   "ReadFileTool",       "read_file"),
                ("vaf.tools.filesystem",   "ListFilesTool",      "list_files"),
                ("vaf.tools.filesystem",   "MoveFileTool",       "move_file"),
                ("vaf.tools.bash",         "BashTool",           "bash"),
                ("vaf.tools.coder",        "CodingAgentTool",    "coding_agent"),
                ("vaf.tools.librarian",    "LibrarianTool",      "librarian_agent"),
                ("vaf.tools.research_agent", "ResearchAgentTool", "research_agent"),
                ("vaf.tools.document_writer", "DocumentWriterTool", "document_writer"),
            ]
            for module_path, class_name, tool_name in _optional:
                try:
                    import importlib
                    mod = importlib.import_module(module_path)
                    tools[tool_name] = getattr(mod, class_name)()
                except Exception:
                    pass

            if not tools:
                return "❌ No tools available for workflow execution."

            # ── WebUI wiring ──────────────────────────────────────────────────
            try:
                from vaf.core.subagent_ipc import get_current_session_id
                session_id = get_current_session_id()
            except Exception:
                session_id = None

            wf_id = f"wf-{uuid.uuid4().hex[:8]}"

            def _push(payload: dict) -> None:
                if not session_id:
                    return
                try:
                    from vaf.core.web_interface import get_web_interface
                    get_web_interface()._push_session_update(session_id, payload)
                except Exception:
                    pass

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
                "workflowId": wf_id,
                "name":       template["name"],
                "steps":      ui_steps,
            })

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
                        "workflowId": wf_id,
                        "line":       f"\u2500\u2500\u2500 Step {current}/{total}: {label} [{step.tool}] \u2500\u2500\u2500",
                    })

            class _WebStreamWriter:
                def __init__(self, stream):
                    self._stream = stream
                    self._buf = ""

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
                            "workflowId": wf_id,
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

            # ── Execute ───────────────────────────────────────────────────────
            engine = WorkflowEngine(
                tools         = tools,
                callback      = _ws_callback,
            )

            _orig_stdout = sys.stdout
            _orig_stderr = sys.stderr
            _prev_wf_terminal = os.environ.get("VAF_IN_WORKFLOW_TERMINAL")
            _prev_tool_model = os.environ.get("VAF_TOOL_MODEL")
            from vaf.core.config import Config as _CfgWE
            _subagent_model = _CfgWE.get("subagent_model", "")
            try:
                sys.stdout = _WebStreamWriter(sys.stdout)
                sys.stderr = _WebStreamWriter(sys.stderr)
                os.environ["VAF_IN_WORKFLOW_TERMINAL"] = "1"
                if _subagent_model and _subagent_model.lower() != "deepseek-auto":
                    os.environ["VAF_TOOL_MODEL"] = _subagent_model
                result = engine.execute(steps, variables=variables)
            except Exception as exc:
                return f"❌ Error executing workflow '{workflow_id}': {exc}"
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

            if result.success:
                return f"✅ Workflow '{template['name']}' completed successfully!\n\n{result.final_output}"
            else:
                return f"❌ Workflow '{template['name']}' failed: {result.error}"

        except Exception as e:
            return f"❌ Error executing workflow: {str(e)}"
