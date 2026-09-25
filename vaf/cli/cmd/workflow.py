# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
CLI command for running workflows in separate terminal windows.
This allows entire workflows to run independently with their own context.

Used when sub_agents_in_separate_terminals is enabled - the whole workflow
runs in its own terminal and only reports the final summary back.
"""
import typer
import json
import sys
import os
import time
from pathlib import Path
from typing import Optional

# The ONE web mirror/ticker for the whole codebase (vaf/core/web_ticker.py). This lane
# used to own the only hardened copy; three other lanes had unfiltered ones.
from vaf.core.web_ticker import MirroredStdout

# Fix Windows encoding issues - must be done BEFORE any output
if sys.platform == "win32":
    # Set UTF-8 encoding for subprocess output
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    # Enable Windows console UTF-8 mode if available
    try:
        import ctypes
        # Set console output code page to UTF-8 (65001)
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass

app = typer.Typer()

# The child owns its terminal window: closes on success, holds on failure or with
# --no-auto-close. Single implementation, shared with the sub-agent runner.
from vaf.cli.autoclose import AUTO_CLOSE_DELAY, finish_terminal  # noqa: F401


@app.command(name="run")
def run_workflow(
    workflow_id: str = typer.Argument(..., help="Workflow ID to execute (e.g., deep_research)"),
    variables: str = typer.Option("{}", "--variables", "-v", help="JSON string of variables"),
    task_id: Optional[str] = typer.Option(None, "--task-id", help="Task ID for IPC tracking"),
    no_auto_close: bool = typer.Option(False, "--no-auto-close", help="Don't auto-close terminal"),
    plan_from_task: bool = typer.Option(
        False, "--plan-from-task",
        help="Run the temporary plan stored as this task's IPC payload (run_temp in the "
             "background) instead of a saved template; WORKFLOW_ID is then only a label"),
):
    """
    Run a complete workflow in a separate terminal.

    The entire workflow executes here with its own context.
    Only the final summary is reported back to the main agent via IPC.

    A saved template by its id, or with --plan-from-task the temporary plan a chat started in
    the background (vaf/workflows/background.py): the same normalised steps the chat would have
    run, with the same per-step validation (vaf/workflows/step_validation.py) and the same
    cleanup of throwaway files afterwards.
    """
    # Work where the caller works (see Platform.adopt_parent_cwd): a workflow
    # step that runs the coder must see the same project the main agent saw.
    from vaf.core.platform import Platform
    Platform.adopt_parent_cwd()

    # Initialize debug logger early
    debug_logger = None
    try:
        # Set env vars needed for debug logger BEFORE calling get_subagent_logger_from_env
        if task_id:
            os.environ["VAF_TASK_ID"] = task_id
        os.environ["VAF_AGENT_TYPE"] = f"workflow:{workflow_id}"
        os.environ["VAF_IN_SUBAGENT_TERMINAL"] = "1"

        from vaf.core.subagent_debug import get_subagent_logger_from_env
        debug_logger = get_subagent_logger_from_env()
        if debug_logger:
            debug_logger.event("workflow_cli_start", workflow_id=workflow_id, task_id=task_id,
                              variables_raw=variables[:200] if variables else "")
    except Exception as e:
        print(f"[DEBUG] Logger init failed: {e}", file=sys.stderr)

    # IMPORTANT: Mark task as running FIRST before any other imports
    # This ensures the main agent knows we've started even if later imports fail
    ipc = None
    try:
        from vaf.core.subagent_ipc import get_ipc, set_current_session_id
        session_id = os.environ.get("VAF_SESSION_ID", "").strip()
        if session_id:
            set_current_session_id(session_id)

        # Get IPC instance if we have a task_id
        ipc = get_ipc() if task_id else None

        # Mark task as running IMMEDIATELY
        if ipc and task_id:
            ipc.mark_task_running(task_id)
            if debug_logger:
                debug_logger.event("workflow_ipc_marked_running", task_id=task_id)
    except Exception as e:
        # If IPC fails, log to stderr but continue
        print(f"[WARNING] IPC initialization failed: {e}", file=sys.stderr)
        if debug_logger:
            debug_logger.event("workflow_ipc_error", error=str(e)[:200])

    try:
        from vaf.cli.ui import UI
        from vaf.core.config import Config
    except Exception as e:
        error_msg = f"Failed to import required modules: {e}"
        print(f"[ERROR] {error_msg}", file=sys.stderr)
        if ipc and task_id:
            ipc.fail_task(task_id, error_msg)
        sys.exit(1)

    # Mark that we're in a workflow terminal
    os.environ["VAF_IN_WORKFLOW_TERMINAL"] = "1"
    os.environ["VAF_IN_SUBAGENT_TERMINAL"] = "1"  # Prevents sub-agents from spawning more terminals

    # Start heartbeat thread to keep the task alive
    heartbeat_stop_event = None
    heartbeat_thread = None
    if ipc and task_id:
        import threading
        heartbeat_stop_event = threading.Event()

        def _heartbeat_loop():
            while not heartbeat_stop_event.is_set():
                try:
                    ipc.update_heartbeat(task_id)
                except Exception:
                    pass
                # Send heartbeat every 5 seconds
                heartbeat_stop_event.wait(5)

        heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        heartbeat_thread.start()

    success = False
    paused = False
    final_summary = ""

    try:
        # Parse variables
        try:
            vars_dict = json.loads(variables)
            if debug_logger:
                debug_logger.event("workflow_variables_parsed", variables=list(vars_dict.keys()))
        except json.JSONDecodeError as e:
            error_msg = f"Invalid JSON for variables: {variables}"
            UI.error(error_msg)
            if debug_logger:
                debug_logger.event("workflow_json_error", error=str(e), variables_raw=variables[:100])
            if ipc and task_id:
                ipc.fail_task(task_id, error_msg)
            finish_terminal(success=False, no_auto_close=no_auto_close)

        # Load workflow template - or the temporary plan the chat handed over
        plan = None
        if plan_from_task:
            from vaf.workflows.background import plan_from_payload
            plan = plan_from_payload(ipc.get_task_payload(task_id) if (ipc and task_id) else None)
            template = ({"name": plan["name"], "description": plan.get("description", ""),
                         "steps": plan["steps"], "variables": {}, "defaults": {}}
                        if plan else None)
            if plan:
                vars_dict = dict(plan.get("variables") or {})
                # The id the web panel and the logs know this run by. The argv label is fixed
                # ("temp"), so two temporary runs in one chat would otherwise share one panel.
                workflow_id = f"temp-{task_id or os.getpid()}"
        else:
            from vaf.workflows.templates import get_template
            template = get_template(workflow_id)

        if not template:
            error_msg = ("No workflow plan was handed to this task" if plan_from_task
                         else f"Workflow not found: {workflow_id}")
            UI.error(error_msg)
            if debug_logger:
                debug_logger.event("workflow_not_found", workflow_id=workflow_id)
            if ipc and task_id:
                ipc.fail_task(task_id, error_msg)
            finish_terminal(success=False, no_auto_close=no_auto_close)

        if debug_logger:
            debug_logger.event("workflow_template_loaded", workflow_name=template.get('name'),
                              num_steps=len(template.get('steps', [])))

        # Fill missing variables from the template defaults - the in-chat
        # executor does the same (workflow_executor.py); without this an
        # unset {filename} stays a LITERAL "{filename}" in step args and a
        # file named "{filename}" lands on disk (observed live).
        for _var_name in (template.get("variables") or {}).keys():
            if _var_name not in vars_dict and _var_name in (template.get("defaults") or {}):
                vars_dict[_var_name] = template["defaults"][_var_name]

        UI.success(f"[OK] Starting workflow: {template['name']}")
        UI.info(f"Variables: {vars_dict}")

        # Build workflow steps
        from vaf.workflows.engine import create_workflow, WorkflowEngine
        steps = create_workflow(template)

        # Load all tools needed for workflow: the SHARED primitives builder
        # (vaf/workflows/tool_overlay.py) - this subprocess has no live agent
        # registry to overlay, so the shared list must cover every tool a
        # built-in template names. The previous hand-maintained copy here
        # lacked python_sandbox, failing youtube_summary's first step with
        # "Tool not found" (live incident).
        from vaf.workflows.tool_overlay import workflow_primitives
        tools = workflow_primitives()
        if not tools:
            UI.warning("No workflow tools could be constructed.")

        # Web UI Reporting Setup
        import requests
        session_id = os.environ.get("VAF_SESSION_ID")
        workflow_output_enabled = False

        def send_web_update(data):
            if not session_id: return
            try:
                # Add session ID to every update. TLS-aware base (Rule 2 single
                # source): with local_network_tls_enabled the public 8001 port
                # speaks HTTPS and a hardcoded plain-HTTP POST dies silently -
                # the UI then never saw workflow_start and showed the SubAgent
                # window instead of the Workflow Runtime (live incident).
                from vaf.core.web_interface import internal_api_base
                data["sessionId"] = session_id
                requests.post(f"{internal_api_base()}/api/workflow/update", json=data, timeout=0.2)
            except: pass

        def send_web_line(line: str):
            if not session_id: return
            try:
                send_web_update({
                    "type": "workflow_output_stream",
                    "workflowId": workflow_id,
                    "line": line
                })
            except:
                pass

        if session_id:
            # interactive=None keeps the REAL stream's isatty(): this lane owns a visible
            # terminal window, so its Rich TUI and colours must survive.
            workflow_output_enabled = True
            sys.stdout = MirroredStdout(sys.stdout, send_web_line)
            sys.stderr = MirroredStdout(sys.stderr, send_web_line)

        # Send initial workflow structure
        if session_id:
            ui_steps = []
            for idx, s in enumerate(steps):
                ui_steps.append({
                    "id": f"step-{idx+1}", # Use 1-based index or match loop
                    "name": s.description or s.tool,
                    "type": "tool",
                    "status": "idle"
                })
            send_web_update({
                "type": "workflow_start",
                "workflowId": workflow_id,
                "name": template['name'],
                "steps": ui_steps
            })

        # Progress callback
        def progress_callback(event, step, current, total):
            # Web UI Update
            if session_id:
                step_id = f"step-{current}" # Match ID above
                status = "running"
                if event == "success": status = "success"
                elif event == "error": status = "failed"
                elif event == "skip": status = "skipped"

                send_web_update({
                    "type": "workflow_update",
                    "stepId": step_id,
                    "status": status,
                    "progress": int((current / total) * 100)
                })

            if event == "start":
                UI.event("Workflow", f"Step {current}/{total}: {step.tool}...", style="cyan")
                # No TTS tool confirmations (user request: avoid "Ich schreibe die Datei" etc.)

            elif event == "success":
                UI.event("Workflow", f"[OK] Step {current}/{total}: {step.tool}", style="green")
            elif event == "error":
                UI.event("Workflow", f"[X] Step {current}/{total}: {step.tool} failed", style="red")

        # Create engine and execute
        # The only consumer with no agent object at all - this runs as its own subprocess and
        # builds its tools from workflow_primitives(). The identity therefore comes from the
        # session's own metadata, the way the engine already derives the project path.
        # No authorize= here, for the same reason: a callable cannot cross a process
        # boundary. The account allowlist still holds in this lane - its answer comes from
        # the resolver this process registers itself at vaf.main import.
        from vaf.workflows.engine import identity_for_engine
        engine = WorkflowEngine(
            tools, callback=progress_callback,
            **identity_for_engine(session_id=session_id),
        )
        engine._workflow_defaults = template.get("defaults", {})
        engine._workflow_name = template["name"] if plan else workflow_id
        # A temporary plan has no saved template (the chat lane's own rule for run_temp).
        engine._template_id = "" if plan else workflow_id
        engine._session_id = session_id
        engine._ui_workflow_id = workflow_id
        if plan and any(getattr(s, "validate", False) for s in steps):
            # The checks the chat lane turned on for this plan's content steps run here too,
            # with a model asked through complete() - this process has no agent.
            from vaf.workflows.step_validation import validator_for_runner
            engine._validate_step = validator_for_runner()
            engine._workflow_user_intent = plan.get("user_intent", "")

        # Execute the workflow
        result = engine.execute(steps, variables=vars_dict)

        if getattr(result, "paused", False):
            # PAUSED, NOT FAILED. The run handed a step to an async sub-agent and is still
            # alive; the drain resumes it when that helper reports back. The old code fell
            # through to the failure branch below and sent ipc.fail_task("Unknown error") to
            # the main agent - a fabricated crash for a healthy run.
            #
            # cancel_task, never fail_task/complete_task: this terminal is done, but the
            # WORK is not, and the result belongs to the sub-agent's own delivery. cancel_task
            # is a no-op if the task is already gone (subagent_ipc), so it is safe either way.
            from vaf.workflows.engine import paused_tool_message
            _agent = getattr(result, "waiting_for_agent", "") or "a background helper"
            _task = str(getattr(result, "waiting_for_task", "") or "?")
            _done = sum(1 for s in steps
                        if getattr(getattr(s, "status", None), "value", "") == "success")
            UI.info(paused_tool_message(template["name"], _done + 1, len(steps), _agent, _task))
            if ipc and task_id:
                ipc.cancel_task(task_id)
            paused = True
            success = False
        elif result.success:
            # Extract final output summary
            final_output = str(result.final_output) if result.final_output else ""

            # Resolve output path: prefer workflow outputs (e.g. output_file from deep_research)
            output_path = None
            if result.outputs.get("output_file"):
                p = result.outputs["output_file"]
                output_path = str(p) if p else None
            if not output_path and ("written successfully" in final_output.lower() or "saved" in final_output.lower()):
                import re
                path_match = re.search(
                    r'(?:to|saved|written)[:\s]+([A-Za-z]:[^\s]+\.(?:html|md|txt|json|docx)|/[^\s]+\.(?:html|md|txt|json|docx))',
                    final_output, re.IGNORECASE
                )
                if path_match:
                    output_path = path_match.group(1).strip()

            # Create SHORT summary (not full content!)
            if plan:
                # A temporary plan: its throwaway files go (the deliverable stays), and the
                # agent gets what the chat lane gives it - every step's result head next to
                # the final output, and the order not to redo the work.
                from vaf.workflows.engine import remove_temp_intermediates, summarize_run_steps
                remove_temp_intermediates((result.outputs or {}).get("workflow_project_path", ""),
                                          keep_files=plan.get("keep_files") or [],
                                          final_output=result.final_output)
                _steps_summary = summarize_run_steps(steps)
                final_summary = (
                    f"Temporary workflow '{template['name']}' completed.\n"
                    "THE WORK IS DONE. Do NOT redo any step, do NOT re-run searches, do NOT "
                    "rebuild files. Present the results below to the user, including any file "
                    f"path shown.\n\n{final_output[:1500]}{'...' if len(final_output) > 1500 else ''}"
                    + (f"\n\nStep results:\n{_steps_summary}" if _steps_summary else ""))
            elif output_path:
                final_summary = f"Workflow '{template['name']}' completed successfully.\nOutput saved to: {output_path}"
            elif "written successfully" in final_output.lower() or "saved" in final_output.lower():
                final_summary = f"Workflow '{template['name']}' completed successfully.\n{final_output[:200]}"
            else:
                final_summary = f"Workflow '{template['name']}' completed successfully.\nResult: {final_output[:200]}{'...' if len(final_output) > 200 else ''}"

            UI.success(f"\n[OK] {final_summary}")
            success = True

            # Report SUCCESS with SHORT summary (not full content!)
            if ipc and task_id:
                ipc.complete_task(task_id, final_summary)
                UI.success(f"[OK] Result sent to Main Agent [Task: {task_id}]")

            # Notify Web UI so Document Editor opens with the created document
            if session_id and output_path:
                try:
                    from vaf.core.web_interface import notify_document_created
                    notify_document_created(
                        session_id,
                        output_path,
                        title=template.get('name', 'Document'),
                    )
                except Exception:
                    send_web_update({
                        "type": "document_ready",
                        "filePath": output_path,
                        "title": template.get('name', 'Document'),
                    })
        else:
            error_msg = result.error or "Unknown error"
            UI.error(f"Workflow failed: {error_msg}")

            if ipc and task_id:
                ipc.fail_task(task_id, error_msg)

    except Exception as e:
        error_msg = str(e)
        UI.error(f"Workflow execution failed: {error_msg}")
        import traceback
        traceback.print_exc()

        if ipc and task_id:
            ipc.fail_task(task_id, error_msg)

    if workflow_output_enabled:
        # Flush both mirrors' tails. The old code reached into the writer's private buffer
        # and only ever did stdout, so stderr's last partial line was silently dropped.
        for _w in (sys.stdout, sys.stderr):
            try:
                _w.close_ticker()
            except Exception:
                pass

    # Close the workflow panel — workflow_start was sent, so workflow_done must follow.
    # EXCEPT when the run only paused: workflow_done carries success=False and the panel
    # paints that red, which would report a crash for a run that is still going. The panel
    # stays open until the drain resumes the run and a real terminal state arrives.
    if session_id and not paused:
        send_web_update({
            "type": "workflow_done",
            "workflowId": workflow_id,
            "success": success,
            "error": "" if success else (final_summary or "Workflow failed")
        })
    elif session_id and paused:
        send_web_update({
            "type": "workflow_output_stream",
            "workflowId": workflow_id,
            "line": "[paused] waiting for background helper",
        })

    # Stop heartbeat thread
    if heartbeat_stop_event:
        heartbeat_stop_event.set()
    if heartbeat_thread:
        heartbeat_thread.join(timeout=1)

    # The window closes on success and holds on failure, so an error stays readable.
    # A paused run exits 0 on purpose: the piped WebUI watcher (Platform._stream_output)
    # turns any non-zero exit into ipc.fail_task plus a red "Process exited with error"
    # SubAgent card, which would recreate the fabricated failure through a second route.
    finish_terminal(
        success=bool(success or paused),
        no_auto_close=no_auto_close,
        exit_code=0 if (success or paused) else 1,
    )


@app.command(name="list")
def list_workflows():
    """List all available workflows."""
    from rich.console import Console
    from vaf.workflows.templates import WORKFLOW_TEMPLATES

    console = Console()
    console.print("\n[bold cyan]Available Workflows[/bold cyan]\n")

    for wf_id, template in WORKFLOW_TEMPLATES.items():
        console.print(f"  [green]{wf_id}[/green]: {template.get('name', wf_id)}")
        if template.get('description'):
            console.print(f"      [dim]{template['description']}[/dim]")


if __name__ == "__main__":
    app()
