"""
CLI command for running sub-agents in separate terminal windows.
This allows each sub-agent to run in its own terminal window when invoked.

Uses IPC (Inter-Process Communication) to report results back to the main agent.
"""
import typer
import json
import sys
import os
import time
from pathlib import Path
from typing import Optional

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
    Cross-platform: works on Windows, Linux, macOS.
    """
    try:
        print()
        for remaining in range(delay, 0, -1):
            sys.stdout.write(f"\r[*] Terminal closing in {remaining} seconds...  ")
            sys.stdout.flush()
            time.sleep(1)
        sys.stdout.write(f"\r[OK] Terminal closing.                           \n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        pass
    
    sys.exit(0)


@app.command(name="run")
def run_subagent(
    agent_type: str = typer.Argument(..., help="Sub-agent type: coding_agent, librarian_agent, research_agent, or document_agent"),
    task: str = typer.Option("", "--task", "-t", help="Task for the sub-agent"),
    task_id: Optional[str] = typer.Option(None, "--task-id", help="Task ID for IPC tracking"),
    project_path: Optional[str] = typer.Option(None, "--project-path", "-p", help="Project path (for coding_agent)"),
    topic: Optional[str] = typer.Option(None, "--topic", help="Topic (for research_agent)"),
    format: Optional[str] = typer.Option("html", "--format", help="Output format (for research_agent/document_agent)"),
    max_results: Optional[int] = typer.Option(5, "--max-results", help="Max results (for research_agent)"),
    no_auto_close: bool = typer.Option(False, "--no-auto-close", help="Don't auto-close terminal after completion"),
):
    """
    Run a sub-agent in a separate terminal window.
    """
    # Mark that we're in a sub-agent terminal (enables TUI, IPC reporting)
    # NOTE: We do NOT set VAF_NONINTERACTIVE because the terminal IS interactive
    # and we WANT the Live TUI to work (Research Agent, Coding Agent animations)
    os.environ["VAF_IN_SUBAGENT_TERMINAL"] = "1"

    # Provide IDs for debug logger (so we can log actions+reactions per run)
    os.environ["VAF_AGENT_TYPE"] = str(agent_type or "")
    if task_id:
        os.environ["VAF_TASK_ID"] = str(task_id)

    from vaf.core.config import Config
    from vaf.cli.ui import UI
    from vaf.core.subagent_ipc import get_ipc, set_current_session_id
    from vaf.core.subagent_debug import get_subagent_logger_from_env, summarize_result
    import traceback
    
    # Restore session ID from environment (passed from main agent)
    session_id = os.environ.get("VAF_SESSION_ID", "").strip()
    if session_id:
        set_current_session_id(session_id)
    
    lg = get_subagent_logger_from_env()
    if lg:
        lg.event(
            "subagent_start",
            cwd=str(Path.cwd()),
            python=sys.executable,
            argv=list(sys.argv),
        )

    # Get IPC instance if we have a task_id
    ipc = get_ipc() if task_id else None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # HEARTBEAT THREAD
    # ═══════════════════════════════════════════════════════════════════════════
    if ipc and task_id:
        import threading
        
        def heartbeat_worker():
            while True:
                try:
                    ipc.update_heartbeat(task_id)
                except:
                    pass
                time.sleep(3) # Pulse every 3 seconds
        
        # Start heartbeat in background daemon thread
        # It will die automatically when main process exits
        hb_thread = threading.Thread(target=heartbeat_worker, daemon=True)
        hb_thread.start()
        
        # Also mark as running immediately (in case startup takes time)
        try:
            ipc.mark_task_running(task_id)
            if lg:
                lg.event("ipc_mark_task_running", ok=True)
        except: pass
    
    success = False

    def _safe_print(*args, **kwargs):
        """Print that handles pipe/encoding errors (WebUI piped stdout)."""
        try:
            print(*args, **kwargs)
            sys.stdout.flush()
        except (BrokenPipeError, OSError, IOError):
            pass

    try:
        result = None
        
        if agent_type == "coding_agent":
            from vaf.tools.coder import CodingAgentTool
            tool = CodingAgentTool()
            kwargs = {"task": task}
            if project_path:
                kwargs["project_path"] = project_path
            result = tool.run(**kwargs)
            _safe_print(result)
            
        elif agent_type == "librarian_agent":
            from vaf.tools.librarian import LibrarianTool
            tool = LibrarianTool()
            result = tool.run(task=task)
            _safe_print(result)
            
        elif agent_type == "research_agent":
            from vaf.tools.research_agent import ResearchAgentTool
            tool = ResearchAgentTool()
            kwargs = {"topic": topic or task}
            if format:
                kwargs["format"] = format
            if max_results:
                kwargs["max_results"] = max_results
            result = tool.run(**kwargs)
            
            _safe_print()
            _safe_print("="*70)
            _safe_print(result)
            _safe_print("="*70)
        
        elif agent_type == "document_agent":
            from vaf.tools.document_agent import DocumentAgentTool
            tool = DocumentAgentTool()
            effective_task = task
            if (not effective_task or len(effective_task) < 50) and task_id and ipc:
                payload = ipc.get_task_payload(task_id)
                if payload:
                    effective_task = payload
            result = tool.run(task=effective_task)
            
            _safe_print()
            _safe_print("="*70)
            _safe_print(result)
            _safe_print("="*70)
            
        else:
            try:
                UI.error(f"Unknown sub-agent type: {agent_type}")
            except (BrokenPipeError, OSError):
                pass
            if ipc and task_id:
                ipc.fail_task(task_id, f"Unknown sub-agent type: {agent_type}")
                if lg:
                    lg.event("ipc_fail_task", ok=True, error="Unknown sub-agent type")
            
            if not no_auto_close:
                _auto_close_countdown()
            sys.exit(1)
        
        # IPC first (file I/O) - MUST happen before any stdout writes
        if ipc and task_id and result:
            ipc.complete_task(task_id, result)
            if lg:
                lg.event("ipc_complete_task", ok=True, **summarize_result(result))
            try:
                UI.success(f"[OK] Result sent to Main Agent [Task: {task_id}]")
            except (BrokenPipeError, OSError):
                pass
        elif lg:
            lg.event("subagent_result_empty_or_none", ok=True, **summarize_result(result))

        # Notify WebUI of successful completion
        if session_id:
            try:
                from vaf.core.config import Config as _Cfg
                _tls = _Cfg.get("local_network_tls_enabled", False)
                _port = 8005 if _tls else 8001
                import requests as _req
                _title = (agent_type or "sub-agent").replace("_", " ").title()
                _req.post(
                    f"http://127.0.0.1:{_port}/api/subagent/stream",
                    json={
                        "type": "subagent_update",
                        "sessionId": session_id,
                        "taskId": task_id or None,
                        "agentName": _title,
                        "status": "Completed successfully",
                        "presence": "idle",
                    },
                    timeout=1.0,
                )
            except Exception:
                pass

        success = True
            
    except Exception as e:
        error_msg = str(e)
        if lg:
            lg.event(
                "subagent_exception",
                ok=False,
                error=error_msg,
                traceback=traceback.format_exc()[:4000],
            )
        
        # IPC first (file I/O) - MUST happen before any stdout writes
        if ipc and task_id:
            try:
                ipc.fail_task(task_id, error_msg)
                if lg:
                    lg.event("ipc_fail_task", ok=True, error=error_msg)
            except Exception:
                pass
        
        # Notify WebUI immediately so the SubAgentWindow shows the error
        if session_id:
            try:
                from vaf.core.config import Config as _Cfg
                _tls = _Cfg.get("local_network_tls_enabled", False)
                _port = 8005 if _tls else 8001
                import requests as _req
                _title = (agent_type or "sub-agent").replace("_", " ").title()
                _req.post(
                    f"http://127.0.0.1:{_port}/api/subagent/stream",
                    json={
                        "type": "subagent_update",
                        "sessionId": session_id,
                        "taskId": task_id or None,
                        "agentName": _title,
                        "status": f"ERROR: {error_msg[:120]}",
                        "presence": "error",
                    },
                    timeout=1.0,
                )
            except Exception:
                pass

        try:
            UI.error(f"Sub-agent execution failed: {error_msg}")
            traceback.print_exc()
        except (BrokenPipeError, OSError):
            pass
    
    # Auto-close terminal after completion (success or failure)
    if not no_auto_close:
        _auto_close_countdown()
    
    if not success:
        sys.exit(1)


@app.command(name="status")
def check_status():
    """Check status of all sub-agent tasks."""
    from vaf.core.subagent_ipc import get_ipc
    from vaf.cli.ui import UI
    
    ipc = get_ipc()
    
    # Get active tasks
    active = ipc.get_active_tasks()
    if active:
        UI.section("[>] Active Sub-Agent Tasks")
        for task in active:
            UI.info(f"  [{task.task_id}] {task.agent_type}: {task.task_description[:50]}...")
    
    # Get pending results
    results = ipc.get_pending_results()
    if results:
        UI.section("[OK] Completed Tasks (waiting for Main Agent)")
        for task in results:
            status_icon = "[OK]" if task.status == "completed" else "[X]"
            UI.info(f"  {status_icon} [{task.task_id}] {task.agent_type}: {task.status}")
    
    # Get paused workflows
    paused = ipc.get_all_paused_workflows()
    if paused:
        UI.section("[||] Paused Workflows")
        for wf in paused:
            UI.info(f"  [{wf.workflow_id}] {wf.workflow_name} waiting for {wf.waiting_for_task_id}")
    
    if not active and not results and not paused:
        UI.info("No active or pending sub-agent tasks.")


@app.command(name="clear")
def clear_queue():
    """Clear all sub-agent queues (for debugging)."""
    from vaf.core.subagent_ipc import get_ipc
    from vaf.cli.ui import UI
    
    ipc = get_ipc()
    ipc.clear_all()
    UI.success("All sub-agent queues cleared.")


if __name__ == "__main__":
    app()

