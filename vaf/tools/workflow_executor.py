# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Workflow Executor Tool

Allows the Main Agent to manually execute a workflow when:
- No workflow automatically matched the request
- Agent determines a specific workflow would be best
- User explicitly requests a workflow
"""

import os
import sys
import threading
import time
import uuid

from vaf.tools.base import BaseTool


def _wf_log(run_id: str, event: str, **kwargs) -> None:
    """Always-on workflow run log — independent of debug_logs_enabled.
    Writes to ~/.vaf/logs/workflow_YYYY-MM-DD.log (one file per day, auto-GC-able).
    """
    try:
        from pathlib import Path
        from datetime import datetime
        log_dir = Path.home() / ".vaf" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"workflow_{datetime.now().strftime('%Y-%m-%d')}.log"
        ts = datetime.now().isoformat(timespec='milliseconds')
        parts = " | ".join(f"{k}={str(v)[:200]!r}" for k, v in kwargs.items())
        line = f"{ts} [{run_id}] {event}"
        if parts:
            line += f" | {parts}"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


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
        _ws_started = False  # True after workflow_start is pushed; used by outer except
        wf_id = None         # set after run ID is assigned
        try:
            from vaf.workflows.templates import get_template, list_templates
            from vaf.workflows.engine import WorkflowEngine, create_workflow

            # Prefer injected agent's session_id — more reliable than module-global
            _agent = kwargs.get("_agent")
            _agent_session_id = getattr(_agent, "current_session_id", None)

            # Re-delegation guard (mirrors the async terminal lane in
            # agent.py): a snapshot-reset model may call execute_workflow for
            # the SAME workflow again while the first run is still live.
            try:
                from vaf.core.subagent_ipc import get_ipc as _get_ipc

                _dup = [
                    t for t in _get_ipc().get_active_tasks_for_current_session()
                    if getattr(t, "agent_type", "") == f"workflow:{workflow_id}"
                ]
            except Exception:
                _dup = []
            if _dup:
                return (
                    f"Workflow '{workflow_id}' is ALREADY RUNNING for this chat "
                    "- not starting a duplicate. Tell the user the workflow is "
                    "still in progress; the result will arrive when it finishes."
                )

            template = get_template(workflow_id)
            if not template:
                available = list_templates()
                workflow_list = "\n".join([f"- {w['id']}: {w['description']}" for w in available])
                return (
                    f"❌ Workflow '{workflow_id}' not found.\n\n"
                    f"Available workflows:\n{workflow_list}"
                )

            variables = variables or {}
            if isinstance(variables, str):
                import json as _json
                try:
                    variables = _json.loads(variables)
                except Exception:
                    variables = {}
            template_vars = template.get("variables", {})
            defaults = template.get("defaults", {})
            missing = []
            for var_name in template_vars.keys():
                if var_name not in variables:
                    if var_name in defaults:
                        variables[var_name] = defaults[var_name]
                    else:
                        missing.append(var_name)

            steps = create_workflow(template)

            # ── Assign run ID and log the call ────────────────────────────────
            wf_run_id = f"wf-{uuid.uuid4().hex[:8]}"
            wf_id = wf_run_id  # reuse run ID as WebSocket workflowId
            _wf_log(wf_run_id, "CALL",
                    workflow_id=workflow_id,
                    template_name=template.get("name", "?"),
                    num_steps=len(steps),
                    variables=str({k: str(v)[:80] for k, v in variables.items()}))

            # ── Tool registry ─────────────────────────────────────────────────
            # Workflow PRIMITIVES from the SHARED builder (vaf/workflows/tool_overlay.py)
            # - the same list the @workflow CLI subprocess uses. Hand-maintained
            # copies drifted (the CLI lacked python_sandbox -> "Tool not found").
            from vaf.workflows.tool_overlay import workflow_primitives
            tools = workflow_primitives()

            # Overlay the agent's FULL live registry so a saved workflow has every tool the user
            # has in chat (search_tools, custom tools, calendar, memory, github, …) — matching
            # run_temp's _collect_tools(). The primitives above stay (they aren't in agent.tools);
            # everything else the user has is added. Falls back to the primitives when headless.
            if _agent is not None and getattr(_agent, "tools", None):
                tools.update(dict(_agent.tools))

            if not tools:
                return "❌ No tools available for workflow execution."

            # ── WebUI wiring ──────────────────────────────────────────────────
            try:
                from vaf.core.subagent_ipc import get_current_session_id
                session_id = _agent_session_id or get_current_session_id()
            except Exception:
                session_id = _agent_session_id

            _wf_log(wf_run_id, "SESSION_RESOLVED", session_id=str(session_id))

            def _push(payload: dict) -> None:
                if not session_id:
                    _wf_log(wf_run_id, "PUSH_SKIP_NO_SESSION",
                            type=payload.get('type', '?'))
                    return
                _ptype = payload.get('type', '?')
                _wf_log(wf_run_id, "PUSH", type=_ptype)
                try:
                    from vaf.core.web_interface import get_web_interface
                    get_web_interface()._push_session_update(session_id, payload)
                except Exception as _push_err:
                    _wf_log(wf_run_id, "PUSH_ERROR", type=_ptype, err=str(_push_err))

            # ── Variable validation (before workflow_start so no panel opens on error) ──
            if missing:
                _wf_log(wf_run_id, "MISSING_VARS", missing=str(missing))
                var_descriptions = "\n".join([f"  - {var}: {template_vars[var]}" for var in missing])
                return (
                    f"❌ Workflow '{workflow_id}' requires these variables:\n"
                    f"{var_descriptions}\n\n"
                    f"Please provide them using the 'variables' parameter."
                )

            ui_steps = [
                {
                    "id":     f"step-{idx + 1}",
                    "name":   s.description or s.tool,
                    "type":   "tool",
                    "status": "idle",
                }
                for idx, s in enumerate(steps)
            ]
            _wf_log(wf_run_id, "PUSH_WORKFLOW_START", num_ui_steps=len(ui_steps))
            _push({
                "type":       "workflow_start",
                "workflowId": wf_id,
                "name":       template["name"],
                "steps":      ui_steps,
            })
            _ws_started = True  # panel is now open — workflow_done MUST be sent

            _STATUS = {"start": "running", "success": "success", "error": "failed", "skip": "skipped"}

            # ── Dual watchdog ──────────────────────────────────────────────────
            # Timeout A (60s): no visible stdout line for a running step
            # Timeout B (40s): previous step ended but next step never started
            _TIMEOUT_ACTIVE  = 60
            _TIMEOUT_BETWEEN = 40

            _last_activity   = [time.time()]
            _last_cb         = ["none", time.time()]
            _stop_event      = threading.Event()

            def _touch():
                _last_activity[0] = time.time()

            def _watchdog_thread():
                while not _stop_event.wait(timeout=2):
                    now = time.time()

                    # Timeout B: step ended but next hasn't started yet
                    if _last_cb[0] in ("success", "error", "skip") and \
                            now - _last_cb[1] >= _TIMEOUT_BETWEEN:
                        elapsed = round(now - _last_cb[1], 1)
                        msg = f"Schritt-Timeout: Nächster Schritt startete nicht in {_TIMEOUT_BETWEEN}s — breche ab..."
                        _wf_log(wf_run_id, "WATCHDOG_B",
                                last_cb=_last_cb[0], elapsed_s=elapsed)
                        _push({"type": "workflow_output_stream", "workflowId": wf_id, "line": f"⚠ {msg}"})
                        _stop_event.set()
                        break

                    # Timeout A: step is running but completely silent for too long
                    if _last_cb[0] == "start" and now - _last_activity[0] >= _TIMEOUT_ACTIVE:
                        elapsed = round(now - _last_activity[0], 1)
                        msg = f"Step-Timeout: Kein Output in {_TIMEOUT_ACTIVE}s — breche ab..."
                        _wf_log(wf_run_id, "WATCHDOG_A",
                                last_cb=_last_cb[0], silent_s=elapsed)
                        _push({"type": "workflow_output_stream", "workflowId": wf_id, "line": f"⚠ {msg}"})
                        _stop_event.set()
                        break

            threading.Thread(target=_watchdog_thread, daemon=True, name="wf-watchdog").start()

            def _ws_callback(event: str, step, current: int, total: int) -> None:
                _touch()
                _last_cb[0] = event
                _last_cb[1] = time.time()
                _wf_log(wf_run_id, "STEP_CB",
                        cb=event,
                        step=f"{current}/{total}",
                        tool=getattr(step, 'tool', '?'),
                        duration=round(getattr(step, 'duration', 0), 2),
                        error=str(getattr(step, 'error', '') or '')[:120])
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
                        "line":       f"─── Step {current}/{total}: {label} [{step.tool}] ───",
                    })
                elif event == "success":
                    _raw = str(getattr(step, "result", "") or "")
                    _preview = _raw[:2000] + ("\n…[gekürzt]" if len(_raw) > 2000 else "")
                    if _preview.strip():
                        for _line in _preview.splitlines():
                            _push({"type": "workflow_output_stream", "workflowId": wf_id, "line": _line})
                    dur = getattr(step, "duration", 0)
                    _push({"type": "workflow_output_stream", "workflowId": wf_id,
                           "line": f"✓ Schritt {current}/{total} abgeschlossen ({dur:.1f}s)"})
                elif event == "error":
                    err = getattr(step, "error", "") or ""
                    _push({"type": "workflow_output_stream", "workflowId": wf_id,
                           "line": f"✗ Schritt {current}/{total} fehlgeschlagen: {err[:300]}"})

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
                        _touch()  # only reset silence clock when a visible line appears
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
            _exec_result = [None]
            _exec_error  = [None]
            _wf_log(wf_run_id, "ENGINE_START")
            try:
                sys.stdout = _WebStreamWriter(sys.stdout)
                sys.stderr = _WebStreamWriter(sys.stderr)
                os.environ["VAF_IN_WORKFLOW_TERMINAL"] = "1"
                if _subagent_model and _subagent_model.lower() != "deepseek-auto":
                    os.environ["VAF_TOOL_MODEL"] = _subagent_model
                _exec_result[0] = engine.execute(
                    steps,
                    variables=variables,
                    check_stop=lambda: _stop_event.is_set(),
                )
                _wf_log(wf_run_id, "ENGINE_RETURNED",
                        success=getattr(_exec_result[0], 'success', '?'),
                        paused=getattr(_exec_result[0], 'paused', False),
                        error=str(getattr(_exec_result[0], 'error', '') or '')[:200])
            except Exception as exc:
                _exec_error[0] = exc
                _wf_log(wf_run_id, "ENGINE_EXCEPTION", error=str(exc)[:300])
            finally:
                _stop_event.set()   # always stop watchdog when execution ends
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
                # ── Always push workflow_done when panel was opened ──
                if _exec_error[0] is not None:
                    _wf_log(wf_run_id, "PUSH_DONE_ERROR",
                            error=str(_exec_error[0])[:200])
                    _push({"type": "workflow_done", "workflowId": wf_id,
                           "success": False, "error": str(_exec_error[0])})
                elif _exec_result[0] is not None:
                    _r = _exec_result[0]
                    _wf_log(wf_run_id, "PUSH_DONE_RESULT",
                            success=_r.success,
                            paused=getattr(_r, 'paused', False),
                            error=str(_r.error or '')[:200])
                    _push({"type": "workflow_done", "workflowId": wf_id,
                           "success": _r.success, "error": _r.error or ""})
                else:
                    _wf_log(wf_run_id, "PUSH_DONE_UNKNOWN_STATE")
                    _push({"type": "workflow_done", "workflowId": wf_id,
                           "success": False, "error": "Unknown state"})

            if _exec_error[0] is not None:
                return f"❌ Error executing workflow '{workflow_id}': {_exec_error[0]}"

            result = _exec_result[0]
            if result.success:
                _out = str(result.final_output) if result.final_output else ""
                if len(_out) > 3000:
                    _out = _out[:3000] + f"\n\n[... {len(_out) - 3000} weitere Zeichen gekürzt ...]"
                _wf_log(wf_run_id, "RETURN_SUCCESS", output_len=len(_out))
                return f"✅ Workflow '{template['name']}' completed successfully!\n\n{_out}"
            else:
                _wf_log(wf_run_id, "RETURN_FAILED", error=str(result.error or '')[:200])
                return f"❌ Workflow '{template['name']}' failed: {result.error}"

        except Exception as e:
            _wf_log(wf_run_id if wf_id else "??", "OUTER_EXCEPTION",
                    ws_started=_ws_started, error=str(e)[:300])
            if _ws_started and wf_id:
                _push({"type": "workflow_done", "workflowId": wf_id,
                       "success": False, "error": str(e)})
            return f"❌ Error executing workflow: {str(e)}"
