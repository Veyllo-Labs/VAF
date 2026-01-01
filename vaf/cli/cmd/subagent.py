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

app = typer.Typer()

# Default auto-close delay in seconds
AUTO_CLOSE_DELAY = 5


def _auto_close_countdown(delay: int = AUTO_CLOSE_DELAY):
    """
    Show a countdown and then exit (closing the terminal).
    Cross-platform: works on Windows, Linux, macOS.
    """
    from vaf.cli.ui import UI
    
    print()  # Empty line for spacing
    
    for remaining in range(delay, 0, -1):
        # Use carriage return to update the same line
        sys.stdout.write(f"\r[*] Terminal closing in {remaining} seconds...  ")
        sys.stdout.flush()
        time.sleep(1)
    
    sys.stdout.write(f"\r[OK] Terminal closing.                           \n")
    sys.stdout.flush()
    
    # Exit the process - this will close the terminal if it was opened for this process
    sys.exit(0)


@app.command(name="run")
def run_subagent(
    agent_type: str = typer.Argument(..., help="Sub-agent type: coding_agent, librarian_agent, or research_agent"),
    task: str = typer.Option("", "--task", "-t", help="Task for the sub-agent"),
    task_id: Optional[str] = typer.Option(None, "--task-id", help="Task ID for IPC tracking"),
    project_path: Optional[str] = typer.Option(None, "--project-path", "-p", help="Project path (for coding_agent)"),
    topic: Optional[str] = typer.Option(None, "--topic", help="Topic (for research_agent)"),
    format: Optional[str] = typer.Option("html", "--format", help="Output format (for research_agent)"),
    max_results: Optional[int] = typer.Option(5, "--max-results", help="Max results (for research_agent)"),
    no_auto_close: bool = typer.Option(False, "--no-auto-close", help="Don't auto-close terminal after completion"),
):
    """
    Run a sub-agent in a separate terminal window.
    
    This command is called automatically when sub_agents_in_separate_terminals is enabled.
    Results are reported back to the main agent via IPC.
    Terminal auto-closes after 5 seconds (use --no-auto-close to disable).
    """
    from vaf.core.config import Config
    from vaf.cli.ui import UI
    from vaf.core.subagent_ipc import get_ipc, set_current_session_id
    
    # Mark that we're in a sub-agent terminal (enables TUI, IPC reporting)
    # NOTE: We do NOT set VAF_NONINTERACTIVE because the terminal IS interactive
    # and we WANT the Live TUI to work (Research Agent, Coding Agent animations)
    os.environ["VAF_IN_SUBAGENT_TERMINAL"] = "1"
    
    # Restore session ID from environment (passed from main agent)
    session_id = os.environ.get("VAF_SESSION_ID", "").strip()
    if session_id:
        set_current_session_id(session_id)
    
    # Get IPC instance if we have a task_id
    ipc = get_ipc() if task_id else None
    
    success = False
    
    try:
        result = None
        
        if agent_type == "coding_agent":
            from vaf.tools.coder import CodingAgentTool
            tool = CodingAgentTool()
            kwargs = {"task": task}
            if project_path:
                kwargs["project_path"] = project_path
            result = tool.run(**kwargs)
            print(result)
            
        elif agent_type == "librarian_agent":
            from vaf.tools.librarian import LibrarianTool
            tool = LibrarianTool()
            result = tool.run(task=task)
            print(result)
            
        elif agent_type == "research_agent":
            from vaf.tools.research_agent import ResearchAgentTool
            tool = ResearchAgentTool()
            kwargs = {"topic": topic or task}
            if format:
                kwargs["format"] = format
            if max_results:
                kwargs["max_results"] = max_results
            result = tool.run(**kwargs)
            
            # Show result in terminal (summary, not full HTML)
            print()
            print("="*70)
            print(result)
            print("="*70)
            
        else:
            UI.error(f"Unknown sub-agent type: {agent_type}")
            if ipc and task_id:
                ipc.fail_task(task_id, f"Unknown sub-agent type: {agent_type}")
            
            # Auto-close even on error
            if not no_auto_close:
                _auto_close_countdown()
            sys.exit(1)
        
        # Report success to IPC
        if ipc and task_id and result:
            ipc.complete_task(task_id, result)
            UI.success(f"[OK] Result sent to Main Agent [Task: {task_id}]")
        
        success = True
            
    except Exception as e:
        error_msg = str(e)
        UI.error(f"Sub-agent execution failed: {error_msg}")
        import traceback
        traceback.print_exc()
        
        # Report failure to IPC
        if ipc and task_id:
            ipc.fail_task(task_id, error_msg)
    
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

