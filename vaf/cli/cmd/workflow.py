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

app = typer.Typer()

# Default auto-close delay in seconds
AUTO_CLOSE_DELAY = 5


def _auto_close_countdown(delay: int = AUTO_CLOSE_DELAY):
    """
    Show a countdown and then exit (closing the terminal).
    """
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
    from vaf.cli.ui import UI
    from vaf.core.config import Config
    
    # Mark that we're in a workflow terminal
    os.environ["VAF_IN_WORKFLOW_TERMINAL"] = "1"
    os.environ["VAF_IN_SUBAGENT_TERMINAL"] = "1"  # Prevents sub-agents from spawning more terminals
    
    # Restore session ID from environment (passed from main agent)
    from vaf.core.subagent_ipc import get_ipc, set_current_session_id
    session_id = os.environ.get("VAF_SESSION_ID", "").strip()
    if session_id:
        set_current_session_id(session_id)
    
    # Get IPC instance if we have a task_id
    ipc = get_ipc() if task_id else None
    
    # Mark task as running
    if ipc and task_id:
        ipc.mark_task_running(task_id)
    
    success = False
    final_summary = ""
    
    try:
        # Parse variables
        try:
            vars_dict = json.loads(variables)
        except json.JSONDecodeError:
            UI.error(f"Invalid JSON for variables: {variables}")
            if ipc and task_id:
                ipc.fail_task(task_id, f"Invalid JSON for variables: {variables}")
            if not no_auto_close:
                _auto_close_countdown()
            sys.exit(1)
        
        # Load workflow template
        from vaf.workflows.templates import get_template
        template = get_template(workflow_id)
        
        if not template:
            UI.error(f"Workflow not found: {workflow_id}")
            if ipc and task_id:
                ipc.fail_task(task_id, f"Workflow not found: {workflow_id}")
            if not no_auto_close:
                _auto_close_countdown()
            sys.exit(1)
        
        UI.success(f"[OK] Starting workflow: {template['name']}")
        UI.info(f"Variables: {vars_dict}")
        
        # Build workflow steps
        from vaf.workflows.engine import create_workflow, WorkflowEngine
        steps = create_workflow(template)
        
        # Load all tools needed for workflow
        from vaf.tools.filesystem import WriteFileTool, ReadFileTool, ListFilesTool, MoveFileTool
        from vaf.tools.bash import BashTool
        from vaf.tools.search import WebSearchTool
        from vaf.tools.webfetch import WebFetchTool
        
        tools = {
            "write_file": WriteFileTool(),
            "read_file": ReadFileTool(),
            "list_files": ListFilesTool(),
            "move_file": MoveFileTool(),
            "bash": BashTool(),
            "web_search": WebSearchTool(),
            "webfetch": WebFetchTool(),
        }
        
        # Load sub-agent tools
        try:
            from vaf.tools.coder import CodingAgentTool
            from vaf.tools.librarian import LibrarianTool
            from vaf.tools.research_agent import ResearchAgentTool
            tools["coding_agent"] = CodingAgentTool()
            tools["librarian_agent"] = LibrarianTool()
            tools["research_agent"] = ResearchAgentTool()
        except ImportError as e:
            UI.warning(f"Could not load some tools: {e}")
        
        # Load utility tools
        try:
            from vaf.tools.report_filename import ReportFilenameTool
            from vaf.tools.repair_report import RepairReportTool
            tools["report_filename"] = ReportFilenameTool()
            tools["repair_report"] = RepairReportTool()
        except ImportError as e:
            UI.warning(f"Could not load utility tools: {e}")
        
        # Progress callback
        def progress_callback(event, step, current, total):
            if event == "start":
                UI.event("Workflow", f"Step {current}/{total}: {step.tool}...", style="cyan")
                
                # Try to speak filler for this tool
                try:
                    from vaf.core.speech import get_speech_manager
                    from vaf.core.speech_fillers import TOOL_FILLERS
                    from vaf.core.config import Config
                    
                    sm = get_speech_manager()
                    if sm.is_tts_enabled():
                        lang = Config.get("language", "de")
                        # Get filler for this tool
                        filler = TOOL_FILLERS.get(step.tool, {}).get(lang)
                        
                        # Fallback to English if filler not found for current language
                        if not filler and lang != "en":
                            filler = TOOL_FILLERS.get(step.tool, {}).get("en")
                        
                        # If no specific filler, but it's a "thinking" moment, maybe use generic?
                        # For now, only use if we have a specific match
                        if filler:
                            # Format with args if available (e.g. {query})
                            # We need to reconstruct args from resolved input
                            try:
                                # This is a bit hacky as we don't have the resolved args here easily
                                # But we can try to use raw input if it's simple
                                pass
                            except:
                                pass
                                
                            sm.speak(filler, lang=lang)
                except Exception:
                    pass
                    
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
            
            # Create SHORT summary (not full content!)
            if "written successfully" in final_output.lower() or "saved" in final_output.lower():
                # File was written - extract path
                import re
                path_match = re.search(r'(?:to|saved|written)\s+(.+\.(?:html|md|txt|json))', final_output, re.IGNORECASE)
                if path_match:
                    file_path = path_match.group(1).strip()
                    final_summary = f"Workflow '{template['name']}' completed successfully.\nOutput saved to: {file_path}"
                else:
                    final_summary = f"Workflow '{template['name']}' completed successfully.\n{final_output[:200]}"
            else:
                # Other output - truncate
                final_summary = f"Workflow '{template['name']}' completed successfully.\nResult: {final_output[:200]}{'...' if len(final_output) > 200 else ''}"
            
            UI.success(f"\n[OK] {final_summary}")
            success = True
            
            # Report SUCCESS with SHORT summary (not full content!)
            if ipc and task_id:
                ipc.complete_task(task_id, final_summary)
                UI.success(f"[OK] Result sent to Main Agent [Task: {task_id}]")
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

