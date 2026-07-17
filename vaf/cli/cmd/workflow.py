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
import re
import sys
import os
import time
from pathlib import Path
from typing import Optional

# ANSI/VT escape sequences (CSI colors/cursor moves, OSC titles, charset
# selects). Rich Live animations are made of these; the web ticker must
# never see them (they rendered as literal garbage and their volume froze
# the tray browser - live incident 2026-07-16).
_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-9;?]*[A-Za-z]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][0-9A-B])"
)


class _WebTickerFilter:
    """Turns a raw terminal stream into a web-safe, rate-capped line ticker.

    The workflow terminal mirrors its stdout into the Web UI panel. Sub-agents
    (research agent) draw Rich Live animations: full-panel redraws many times
    per second, hundreds of ANSI lines - one HTTP POST + one WebSocket event
    + one React render EACH froze the tray browser until the WebSocket
    dropped. This filter enforces ticker semantics at the emit site:

    - strips ANSI escapes and carriage returns,
    - drops lines that are empty after stripping (animation frames),
    - drops consecutive duplicates (Live redraws),
    - hard rate cap: at most MAX_LINES_PER_WINDOW sends per WINDOW seconds;
      excess lines are counted and surfaced as one "[... N lines skipped]".
    """

    WINDOW = 0.25
    MAX_LINES_PER_WINDOW = 15

    def __init__(self, send_line):
        self._send_line = send_line
        self._buffer = ""
        self._last_line = None
        self._window_start = 0.0
        self._window_count = 0
        self._skipped = 0

    def feed(self, data: str) -> None:
        self._buffer += data
        while "\n" in self._buffer:
            raw, self._buffer = self._buffer.split("\n", 1)
            self._handle_line(raw)

    def _handle_line(self, raw: str) -> None:
        line = _ANSI_RE.sub("", raw).replace("\r", "").rstrip()
        if not line.strip():
            return
        if line == self._last_line:
            return
        self._last_line = line

        now = time.monotonic()
        if now - self._window_start >= self.WINDOW:
            self._window_start = now
            self._window_count = 0
        if self._window_count >= self.MAX_LINES_PER_WINDOW:
            self._skipped += 1
            return
        if self._skipped:
            self._send_line(f"[... {self._skipped} lines skipped]")
            self._skipped = 0
            self._window_count += 1
        self._send_line(line)
        self._window_count += 1

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

# Default auto-close delay in seconds
AUTO_CLOSE_DELAY = 5


def _auto_close_countdown(delay: int = AUTO_CLOSE_DELAY):
    """
    Show a countdown and then exit (closing the terminal).

    Only meaningful in a real terminal (CLI mode). In WebUI / piped mode (no tty) the countdown
    is pointless and its output spams the WebUI, so skip it and exit immediately.
    """
    _webui = os.environ.get("VAF_WEBUI_ACTIVE", "").strip().lower() in ("1", "true", "yes")
    _is_tty = False
    try:
        _is_tty = bool(sys.stdout.isatty())
    except Exception:
        _is_tty = False
    if _webui or not _is_tty:
        sys.exit(0)

    print()  # Empty line for spacing

    for remaining in range(delay, 0, -1):
        sys.stdout.write(f"\r[*] Terminal closing in {remaining} seconds...  ")
        sys.stdout.flush()
        time.sleep(1)

    sys.stdout.write(f"\r[OK] Terminal closing.                           \n")
    sys.stdout.flush()
    sys.exit(0)


@app.command(name="run")
def run_workflow(
    workflow_id: str = typer.Argument(..., help="Workflow ID to execute (e.g., deep_research)"),
    variables: str = typer.Option("{}", "--variables", "-v", help="JSON string of variables"),
    task_id: Optional[str] = typer.Option(None, "--task-id", help="Task ID for IPC tracking"),
    no_auto_close: bool = typer.Option(False, "--no-auto-close", help="Don't auto-close terminal"),
):
    """
    Run a complete workflow in a separate terminal.

    The entire workflow executes here with its own context.
    Only the final summary is reported back to the main agent via IPC.
    """
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
            if not no_auto_close:
                _auto_close_countdown()
            sys.exit(1)

        # Load workflow template
        from vaf.workflows.templates import get_template
        template = get_template(workflow_id)

        if not template:
            error_msg = f"Workflow not found: {workflow_id}"
            UI.error(error_msg)
            if debug_logger:
                debug_logger.event("workflow_not_found", workflow_id=workflow_id)
            if ipc and task_id:
                ipc.fail_task(task_id, error_msg)
            if not no_auto_close:
                _auto_close_countdown()
            sys.exit(1)

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

        class WebStreamWriter(_WebTickerFilter):
            """Real terminal gets everything; the web ticker gets the
            filtered, rate-capped view (see _WebTickerFilter)."""

            def __init__(self, stream):
                super().__init__(send_web_line)
                self.stream = stream

            def write(self, data):
                try:
                    self.stream.write(data)
                    self.stream.flush()
                except Exception:
                    pass
                if not session_id:
                    return
                self.feed(data)

            def flush(self):
                try:
                    self.stream.flush()
                except Exception:
                    pass

            def isatty(self):
                return getattr(self.stream, "isatty", lambda: False)()

            def fileno(self):
                return getattr(self.stream, "fileno", lambda: -1)()

        if session_id:
            workflow_output_enabled = True
            sys.stdout = WebStreamWriter(sys.stdout)
            sys.stderr = WebStreamWriter(sys.stderr)

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
        engine = WorkflowEngine(tools, callback=progress_callback)
        engine._workflow_defaults = template.get("defaults", {})
        engine._workflow_name = workflow_id

        # Execute the workflow
        result = engine.execute(steps, variables=vars_dict)

        if result.success:
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
            if output_path:
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
        try:
            stdout_writer = sys.stdout
            if getattr(stdout_writer, "_buffer", ""):
                send_web_line(stdout_writer._buffer)
                stdout_writer._buffer = ""
        except Exception:
            pass

    # Always close the workflow panel — workflow_start was sent, workflow_done must follow
    if session_id:
        send_web_update({
            "type": "workflow_done",
            "workflowId": workflow_id,
            "success": success,
            "error": "" if success else (final_summary or "Workflow failed")
        })

    # Stop heartbeat thread
    if heartbeat_stop_event:
        heartbeat_stop_event.set()
    if heartbeat_thread:
        heartbeat_thread.join(timeout=1)

    # Auto-close terminal after completion
    if not no_auto_close:
        _auto_close_countdown()

    if not success:
        sys.exit(1)


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
