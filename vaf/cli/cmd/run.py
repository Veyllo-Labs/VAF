import typer
import sys
import signal
import time
import requests
import platform
import subprocess
from rich.console import Console
from rich.panel import Panel
from vaf.core.agent import Agent
from vaf.cli.ui import UI

app = typer.Typer()
console = Console()


# ═══════════════════════════════════════════════════════════════════════════════
# SUB-AGENT STATUS FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def _check_subagent_results(tui, agent):
    """
    Check for completed sub-agent results and display them.
    Also resumes any paused workflows waiting for these results.
    Called at the beginning of each chat interaction.
    
    Returns:
        list: List of result texts (strings) found and processed
    """
    try:
        from vaf.core.subagent_ipc import get_ipc, get_current_session_id
        ipc = get_ipc()
        
        # CRITICAL: Only get results for CURRENT session (not old sessions!)
        current_session = get_current_session_id()
        results = ipc.get_pending_results(session_id=current_session)
        if not results:
            return []
        
        found_results_text = []
        
        for task in results:
            # Check if a paused workflow is waiting for this result
            paused_wf = ipc.get_paused_workflow_for_task(task.task_id)
            
            if task.status == "completed":
                # Detect if this is a workflow result (agent_type starts with "workflow:")
                is_workflow = task.agent_type.startswith("workflow:")
                
                if paused_wf:
                    # Resume the workflow!
                    tui.console.print()
                    tui.console.print(Panel(
                        f"[bold green][OK] Sub-Agent complete - resuming workflow[/bold green]\n\n"
                        f"[cyan]Task:[/cyan] {task.task_id}\n"
                        f"[cyan]Agent:[/cyan] {task.agent_type}\n"
                        f"[cyan]Workflow:[/cyan] {paused_wf.workflow_name}",
                        title="[>] Workflow Resuming",
                        border_style="green"
                    ))
                    
                    # Resume the workflow
                    _resume_paused_workflow(tui, agent, paused_wf, task.result)
                elif is_workflow:
                    # Workflow completed in separate terminal
                    workflow_name = task.agent_type.split(":", 1)[1] if ":" in task.agent_type else task.agent_type
                    result_preview = task.result[:500] if task.result else ""
                    has_more = len(str(task.result)) > 500 if task.result else False
                    
                    tui.console.print()
                    tui.console.print(Panel(
                        f"[bold green][OK] Workflow completed[/bold green]\n\n"
                        f"[cyan]Workflow:[/cyan] {workflow_name}\n"
                        f"[cyan]Task:[/cyan] {task.task_id}\n"
                        f"[cyan]Duration:[/cyan] {_format_duration(task.created_at, task.completed_at)}\n\n"
                        f"[bold]Result:[/bold]\n{result_preview}{'...' if has_more else ''}",
                        title="[OK] Workflow Complete",
                        border_style="green"
                    ))
                    
                    # Add short summary to history (workflow returns short summary, not full content)
                    agent.history.append({
                        "role": "system",
                        "content": f"**Workflow Result [{workflow_name}]** (Task: {task.task_id}):\n\n{task.result}"
                    })
                    
                    # Add to return list for main agent processing
                    found_results_text.append(f"Workflow '{workflow_name}' completed:\n{task.result}")
                else:
                    # Regular sub-agent result (not part of workflow)
                    result_preview = task.result[:500] if task.result else ""
                    has_more = len(str(task.result)) > 500 if task.result else False
                    
                    tui.console.print()
                    tui.console.print(Panel(
                        f"[bold green][OK] Sub-Agent result received[/bold green]\n\n"
                        f"[cyan]Task:[/cyan] {task.task_id}\n"
                        f"[cyan]Agent:[/cyan] {task.agent_type}\n"
                        f"[cyan]Duration:[/cyan] {_format_duration(task.created_at, task.completed_at)}\n\n"
                        f"[bold]Result:[/bold]\n{result_preview}{'...' if has_more else ''}",
                        title="[OK] Sub-Agent Complete",
                        border_style="green"
                    ))
                    
                    # Add TRUNCATED result to history to avoid context overflow
                    # The full result is displayed above, so we just need a summary
                    result_len = len(str(task.result)) if task.result else 0
                    if result_len > 2000:
                        # For large results, only add first 2000 chars + note
                        truncated = task.result[:2000] + f"\n\n[... {result_len - 2000} more characters - see output above ... ]"
                        agent.history.append({
                            "role": "system",
                            "content": f"**Sub-Agent Result [{task.task_id}]** ({task.agent_type}):\n\n{truncated}"
                        })
                    else:
                        # Small results can be added fully
                        agent.history.append({
                            "role": "system",
                            "content": f"**Sub-Agent Result [{task.task_id}]** ({task.agent_type}):\n\n{task.result}"
                        })
                    
                    # Add to return list for main agent processing
                    found_results_text.append(f"Sub-Agent '{task.agent_type}' completed:\n{task.result}")
                
            elif task.status == "failed":
                tui.console.print()
                tui.console.print(Panel(
                    f"[bold red][X] Sub-Agent failed[/bold red]\n\n"
                    f"[cyan]Task:[/cyan] {task.task_id}\n"
                    f"[cyan]Agent:[/cyan] {task.agent_type}\n\n"
                    f"[red]Error:[/red] {task.error}",
                    title="[X] Sub-Agent Error",
                    border_style="red"
                ))
                
                if paused_wf:
                    # Remove the paused workflow - it failed
                    ipc.remove_paused_workflow(paused_wf.workflow_id)
                
                agent.history.append({
                    "role": "system",
                    "content": f"**Sub-Agent Error [{task.task_id}]** ({task.agent_type}):\n{task.error}"
                })
                
                # Add to return list so main agent can comment on failure
                found_results_text.append(f"Sub-Agent '{task.agent_type}' FAILED:\n{task.error}")
                
            elif task.status == "timeout":
                tui.console.print()
                tui.console.print(Panel(
                    f"[bold yellow][!] Sub-Agent Timeout[/bold yellow]\n\n"
                    f"[cyan]Task:[/cyan] {task.task_id}\n"
                    f"[cyan]Agent:[/cyan] {task.agent_type}",
                    title="[!] Timeout",
                    border_style="yellow"
                ))
            
            # Consume the result
            ipc.consume_result(task.task_id)
        
        return found_results_text
        
    except Exception as e:
        # Log error but don't crash
        import traceback
        traceback.print_exc()
        return []


def _resume_paused_workflow(tui, agent, paused_wf, subagent_result: str):
    """
    Resume a paused workflow with the sub-agent's result.
    """
    try:
        from vaf.workflows.engine import WorkflowEngine
        
        # Create engine with agent's tools
        engine = WorkflowEngine(agent.tools)
        engine._workflow_name = paused_wf.workflow_name
        
        # Resume the workflow
        result = engine.resume_workflow(paused_wf, subagent_result)
        
        if result.paused:
            # Workflow paused again (another sub-agent call)
            tui.info(f"[||] Workflow paused again - waiting for next sub-agent [Task: {result.waiting_for_task}]")
        elif result.success:
            # Workflow completed!
            output_str = str(result.final_output) if result.final_output else ""
            output_preview = output_str[:500]
            has_more = len(output_str) > 500
            
            tui.console.print()
            tui.console.print(Panel(
                f"[bold green][OK] Workflow completed[/bold green]\n\n"
                f"[bold]Final Output:[/bold]\n{output_preview}{'...' if has_more else ''}",
                title="[OK] Workflow Complete",
                border_style="green"
            ))
            
            # Add TRUNCATED result to history to avoid context overflow
            if len(output_str) > 2000:
                truncated = output_str[:2000] + f"\n\n[... {len(output_str) - 2000} more characters - see output above ... ]"
                agent.history.append({
                    "role": "system",
                    "content": f"**Workflow Completed** ({paused_wf.workflow_name}):\n\n{truncated}"
                })
            else:
                agent.history.append({
                    "role": "system",
                    "content": f"**Workflow Completed** ({paused_wf.workflow_name}):\n\n{output_str}"
                })
        else:
            # Workflow failed
            tui.error(f"Workflow failed: {result.error}")
            
    except Exception as e:
        tui.error(f"Failed to resume workflow: {e}")
        import traceback
        traceback.print_exc()


# Note: Static banner replaced by live-updating toolbar in tui.py
# The toolbar shows sub-agent status and updates every second


def _format_duration(start_time_str: str, end_time_str: str = None) -> str:
    """Format duration between two ISO timestamps, or from start to now."""
    from datetime import datetime
    
    try:
        start = datetime.fromisoformat(start_time_str)
        end = datetime.fromisoformat(end_time_str) if end_time_str else datetime.now()
        
        delta = end - start
        seconds = int(delta.total_seconds())
        
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            return f"{seconds // 60}m {seconds % 60}s"
        else:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            return f"{hours}h {minutes}m"
    except Exception:
        return "?"

# Global reference for signal handling
global_agent = None


# ═══════════════════════════════════════════════════════════════════════════════
# Git Installation Check (OS-Independent)
# ═══════════════════════════════════════════════════════════════════════════════

def _check_git_installed() -> bool:
    """Check if Git is installed on the system. OS-independent."""
    try:
        result = subprocess.run(
            ['git', '--version'],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _get_git_install_instructions() -> str:
    """Get OS-specific Git installation instructions."""
    system = platform.system()
    
    if system == "Windows":
        return (
            "Git is not installed. To install Git on Windows:\n"
            "1. Download from: https://git-scm.com/download/win\n"
            "2. Or use winget: winget install Git.Git\n"
            "3. Or use chocolatey: choco install git\n"
            "After installation, restart the terminal and try again."
        )
    elif system == "Darwin":  # macOS
        return (
            "Git is not installed. To install Git on macOS:\n"
            "1. Using Homebrew: brew install git\n"
            "2. Or download from: https://git-scm.com/download/mac\n"
            "3. Or install Xcode Command Line Tools: xcode-select --install\n"
            "After installation, restart the terminal and try again."
        )
    else:  # Linux
        return (
            "Git is not installed. To install Git on Linux:\n"
            "1. Debian/Ubuntu: sudo apt-get update && sudo apt-get install git\n"
            "2. RHEL/CentOS/Fedora: sudo yum install git (or sudo dnf install git)\n"
            "3. Arch Linux: sudo pacman -S git\n"
            "4. openSUSE: sudo zypper install git\n"
            "After installation, restart the terminal and try again."
        )


def _try_install_git() -> tuple[bool, str]:
    """
    Try to install Git automatically (OS-independent).
    Returns: (success, message)
    """
    system = platform.system()
    
    try:
        if system == "Windows":
            # Try winget first (Windows 10/11)
            try:
                result = subprocess.run(
                    ['winget', 'install', '--id', 'Git.Git', '--silent', '--accept-package-agreements', '--accept-source-agreements'],
                    capture_output=True,
                    text=True,
                    timeout=300  # 5 minutes
                )
                if result.returncode == 0:
                    return True, "✅ Git installed successfully via winget. Please restart your terminal."
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            
            # Try chocolatey
            try:
                result = subprocess.run(
                    ['choco', 'install', 'git', '-y'],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    shell=True
                )
                if result.returncode == 0:
                    return True, "✅ Git installed successfully via Chocolatey. Please restart your terminal."
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            
            return False, "❌ Could not install Git automatically. Please install manually."
            
        elif system == "Darwin":  # macOS
            # Try Homebrew
            try:
                result = subprocess.run(
                    ['brew', 'install', 'git'],
                    capture_output=True,
                    text=True,
                    timeout=300
                )
                if result.returncode == 0:
                    return True, "✅ Git installed successfully via Homebrew."
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            
            # Try xcode-select
            try:
                result = subprocess.run(
                    ['xcode-select', '--install'],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode == 0:
                    return True, "✅ Xcode Command Line Tools installation started. Please complete the installation."
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            
            return False, "❌ Could not install Git automatically. Please install manually."
            
        else:  # Linux
            # Try different package managers
            package_managers = [
                (['sudo', 'apt-get', 'update', '&&', 'sudo', 'apt-get', 'install', '-y', 'git'], 'apt-get'),
                (['sudo', 'yum', 'install', '-y', 'git'], 'yum'),
                (['sudo', 'dnf', 'install', '-y', 'git'], 'dnf'),
                (['sudo', 'pacman', '-S', '--noconfirm', 'git'], 'pacman'),
                (['sudo', 'zypper', 'install', '-y', 'git'], 'zypper'),
            ]
            
            for cmd, manager in package_managers:
                try:
                    # Check if package manager exists
                    check_cmd = cmd[0] if cmd[0] != 'sudo' else cmd[1]
                    check_result = subprocess.run(
                        ['which', check_cmd],
                        capture_output=True,
                        text=True
                    )
                    if check_result.returncode == 0:
                        # Try to install
                        result = subprocess.run(
                            cmd,
                            capture_output=True,
                            text=True,
                            timeout=300,
                            shell=True if '&&' in ' '.join(cmd) else False
                        )
                        if result.returncode == 0:
                            return True, f"✅ Git installed successfully via {manager}."
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    continue
            
            return False, "❌ Could not install Git automatically. Please install manually."
            
    except Exception as e:
        return False, f"❌ Error during Git installation: {e}"


def _check_and_install_git(tui) -> bool:
    """
    Check if Git is installed, try to install if not, and return True if available.
    OS-independent.
    """
    tui.event("System", "Checking Git installation...", style="dim")
    
    if _check_git_installed():
        tui.event("System", "Git is installed", style="success")
        return True
    
    # Git not installed - try to install
    tui.event("Warning", "Git is not installed", style="warning")
    tui.event("System", "Attempting automatic installation...", style="dim")
    
    with tui.spinner("Installing Git..."):
        success, message = _try_install_git()
    
    if success:
        tui.event("System", message, style="success")
        # Wait a moment and check again
        time.sleep(2)
        if _check_git_installed():
            return True
        else:
            instructions = _get_git_install_instructions()
            tui.error("Git Installation Required")
            tui.info(instructions)
            tui.info("\nAfter installing Git, please restart VAF and try again.")
            return False
    else:
        instructions = _get_git_install_instructions()
        tui.error("Git Installation Required")
        tui.info(message)
        tui.info(instructions)
        tui.info("\nAfter installing Git, please restart VAF and try again.")
        return False


def _warmup_model(tui):
    """
    Send a minimal request to actually load the model into VRAM.
    This prevents "Model is loading..." message after first user prompt.
    """
    warmup_payload = {
        "model": "vq1-model",
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 1,
        "temperature": 0.0,
    }
    
    # Inner function to do the actual warmup
    def do_warmup():
        for attempt in range(30):
            try:
                response = requests.post(
                    "http://127.0.0.1:8080/v1/chat/completions",
                    json=warmup_payload,
                    timeout=60
                )
                
                if response.status_code == 200:
                    return "success"
                elif response.status_code == 503:
                    # Model still loading
                    time.sleep(2)
                    continue
                else:
                    return f"error:{response.status_code}"
                    
            except requests.exceptions.ConnectionError:
                time.sleep(2)
                continue
            except requests.exceptions.Timeout:
                time.sleep(2)
                continue
            except Exception as e:
                return f"exception:{e}"
        
        return "timeout"
    
    # Run warmup with animated spinner
    with tui.spinner("Loading into VRAM..."):
        result = do_warmup()
    
    # Handle result after spinner ends
    if result == "success":
        tui.event("System", "Model ready!", style="green")
        return True
    elif result == "timeout":
        tui.event("Warning", "Model warmup timed out, continuing anyway...", style="warning")
        return False
    elif result.startswith("error:"):
        status = result.split(":")[1]
        tui.event("Warning", f"Warmup got status {status}", style="warning")
        return False
    else:
        error = result.split(":", 1)[1] if ":" in result else result
        tui.event("Warning", f"Warmup error: {error}", style="warning")
        return False

def signal_handler(sig, frame):
    """Handles Ctrl+C to suppress Windows batch prompt and clean up."""
    if global_agent:
        try:
           global_agent.shutdown()
        except:
           pass
    sys.exit(0)

# Register the signal handler immediately
signal.signal(signal.SIGINT, signal_handler)

@app.callback(invoke_without_command=True)
def run(
    ctx: typer.Context,
    message: str = typer.Argument(None, help="Initial message to the agent"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
    classic: bool = typer.Option(False, "--classic", "-c", help="Use classic interface (no TUI)"),
    theme: str = typer.Option(None, "--theme", "-t", help="UI theme"),
    session: str = typer.Option(None, "--session", "-s", help="Load a previous session by ID")
):
    """
    Start the VAF Agent interaction loop.
    
    Uses modern TUI by default. Use --classic for simple prompt.
    
    Examples:
        vaf run                      Start fresh session
        vaf run --session abc123     Resume session abc123
        vaf run --classic            Use simple text interface
    """
    if ctx.invoked_subcommand:
        return

    from vaf.core.config import Config
    
    # Determine UI mode from flag (default to modern)
    ui_mode = "classic" if classic else "modern"
    
    # Get theme from config if not specified
    if not theme:
        theme = Config.get("theme", "vaf")
    
    # Run appropriate interface
    if ui_mode == "modern":
        _run_modern(message, verbose, theme, session)
    else:
        _run_classic(message, verbose, session)


@app.command("prompt")
def run_prompt(
    prompt: str = typer.Option(..., "--prompt", "-p", help="Prompt text (non-interactive)"),
    output_format: str = typer.Option("text", "--output-format", help="text | json | stream-json"),
    session: str = typer.Option(None, "--session", "-s", help="Load an existing session ID"),
    save_session: bool = typer.Option(False, "--save-session", help="Save this interaction as a session"),
):
    """
    Convenience alias: `vaf run prompt ...` -> same behavior as `vaf prompt ...`.

    Note: this does NOT (yet) send the prompt into an already-running interactive `vaf run`.
    It starts a new non-interactive run (but can load/save sessions).
    """
    import json
    import os
    import sys
    from vaf.cli.ui import UI
    from vaf.core.session import SessionManager

    fmt = (output_format or "text").strip().lower()
    if fmt not in ("text", "json", "stream-json"):
        raise typer.BadParameter("output-format must be one of: text, json, stream-json")

    os.environ["VAF_NONINTERACTIVE"] = "1"

    if fmt in ("json", "stream-json"):
        UI.event = staticmethod(lambda *args, **kwargs: None)
        UI.error = staticmethod(lambda *args, **kwargs: None)
        UI.warning = staticmethod(lambda *args, **kwargs: None)
        UI.success = staticmethod(lambda *args, **kwargs: None)
        UI.info = staticmethod(lambda *args, **kwargs: None)

    mgr = SessionManager()
    loaded_session = None
    if session:
        try:
            loaded_session = mgr.load(session)
        except FileNotFoundError:
            loaded_session = None

    agent = Agent(verbose=False)
    agent.init_chat()

    if loaded_session:
        for m in loaded_session.get_history():
            if m.get("role") == "system":
                continue
            agent.history.append({"role": m.get("role", "user"), "content": m.get("content", "")})

    def ndjson_emit(evt: dict):
        line = json.dumps(evt, ensure_ascii=False)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    if fmt == "stream-json":
        agent.set_event_sink(ndjson_emit)
        ndjson_emit({"type": "start"})

    result_text = agent.chat_step(
        prompt,
        stream_callback=(lambda s: ndjson_emit({"type": "text_delta", "text": s})) if fmt == "stream-json" else None,
    )

    if fmt == "text":
        if result_text:
            print(result_text)
        raise typer.Exit(0)

    if fmt == "json":
        payload = {"ok": True, "output": result_text or ""}
        print(json.dumps(payload, ensure_ascii=False))
        if save_session:
            s = mgr.new(model=agent.config.get("model", ""), project_path=os.getcwd())
            s.add_message("user", prompt)
            s.add_message("assistant", result_text or "")
            mgr.save(s)
        raise typer.Exit(0)

    if save_session:
        s = mgr.new(model=agent.config.get("model", ""), project_path=os.getcwd())
        s.add_message("user", prompt)
        s.add_message("assistant", result_text or "")
        mgr.save(s)
        ndjson_emit({"type": "session_saved", "id": s.id})

    ndjson_emit({"type": "end"})


def _run_modern(message: str, verbose: bool, theme: str, session_id: str = None):
    """Run with modern TUI interface."""
    global global_agent
    
    try:
        from vaf.cli.tui import TUI
        from vaf.cli.themes import ThemeManager
        from vaf.core.session import SessionManager
    except ImportError as e:
        UI.error(f"Modern TUI not available: {e}")
        UI.info("Falling back to classic interface...")
        _run_classic(message, verbose, session_id)
        return
    
    # Set theme
    ThemeManager.set_theme(theme)
    tui = TUI(theme)
    
    # Session management
    session_mgr = SessionManager()
    
    # Load existing session or create new one
    if session_id:
        try:
            current_session = session_mgr.load(session_id)
            tui.success(f"Restored session: {current_session.name}")
            tui.info(f"  {len(current_session.messages)} previous messages loaded")
        except FileNotFoundError:
            tui.error(f"Session not found: {session_id}")
            tui.info("Starting new session...")
            current_session = session_mgr.new()
    else:
        current_session = session_mgr.new()
    
    # ═══════════════════════════════════════════════════════════════
    # SESSION-BASED SUB-AGENT CLEANUP
    # ═══════════════════════════════════════════════════════════════
    # Set current session ID for sub-agent tracking and clean up stale tasks
    from vaf.core.subagent_ipc import set_current_session_id, cleanup_other_sessions
    set_current_session_id(current_session.id)
    cleanup_other_sessions()  # Remove active tasks from previous sessions
    
    # Initialize agent
    tui.clear()
    tui.logo()
    tui.event("System", "Initializing VAF...", style="dim")
    
    # ═══════════════════════════════════════════════════════════════
    # GIT CHECK - Must be installed before proceeding
    # ═══════════════════════════════════════════════════════════════
    if not _check_and_install_git(tui):
        tui.error("Cannot proceed without Git. Please install Git and restart VAF.")
        sys.exit(1)
    
    try:
        agent = Agent(verbose=verbose)
        global_agent = agent
        
        # Download model first (if needed) - this shows tqdm progress bar
        # Do this BEFORE the spinner, so tqdm output is visible
        agent.ensure_model_exists()
        
        # Now load the model (this is fast if model already exists)
        # Skip download check since we already did it
        with tui.spinner("Loading model..."):
            agent.load_model(skip_download_check=True)
            agent.init_chat()
        
        # Show success after spinner ends (Backend Ready message moved here)
        if agent.use_server:
            tui.event("Server", "Backend Ready (GPU Accelerated)", style="success")
        
        # ═══════════════════════════════════════════════════════════════
        # MODEL WARMUP - Actually load model into VRAM before user prompt
        # ═══════════════════════════════════════════════════════════════
        tui.event("System", "Warming up model...", style="dim")
        _warmup_model(tui)
        
    except Exception as e:
        tui.error(f"Startup failed: {e}")
        sys.exit(1)
    
    # Welcome
    tui.clear()
    tui.logo()
    tui.newline()
    
    # Handle initial message
    if message:
        tui.message_box(message, role="user")
        _process_agent_message(agent, message, tui, current_session)
    
    # ═══════════════════════════════════════════════════════════════
    # PROACTIVE RESULT NOTIFIER (Background)
    # ═══════════════════════════════════════════════════════════════
    import threading
    from prompt_toolkit.patch_stdout import patch_stdout
    
    def result_notifier():
        """Polls for results and notifies user while at prompt."""
        from vaf.core.subagent_ipc import get_ipc, get_current_session_id
        ipc = get_ipc()
        last_count = 0
        while True:
            try:
                # Only notify if session is active
                curr_sess = get_current_session_id()
                if curr_sess:
                    res = ipc.get_pending_results(session_id=curr_sess)
                    count = len(res)
                    if count > last_count:
                        # New results found!
                        tui.notification(f"✓ {count} Sub-Agent result(s) ready! → Press Enter to process")
                    last_count = count
                time.sleep(2) # Poll every 2 seconds (faster check)
            except:
                time.sleep(10)
    
    # Start polling thread
    notifier_thread = threading.Thread(target=result_notifier, daemon=True)
    notifier_thread.start()
    
    # Interactive loop
    while True:
        try:
            # Note: Sub-agent status now shown in live toolbar (updates every second)
            
            # CHECK FOR SUB-AGENT RESULTS before showing input prompt
            # This prevents unnecessary wait for user input when results are ready
            found_results = _check_subagent_results(tui, agent)
            if found_results:
                # Results available! Process immediately without waiting for user input
                tui.info("[i] Processing sub-agent results...")
                try:
                    user_lang = "auto"
                    for msg in reversed(agent.history):
                        if msg.get("role") == "user":
                            user_lang = agent._detect_user_language(msg.get("content", ""))
                            break
                    
                    native_lang = agent.LANGUAGE_NAMES_NATIVE.get(user_lang, user_lang)
                    combined_results = "\n\n---\n\n".join(r[:1000] for r in found_results)
                    
                    def simple_stream_callback(text):
                        tui.console.print(text, end="", markup=True, style=f"bold {tui.primary}")

                    if user_lang == "de":
                        instruction_prompt = (
                            f"Hier sind die Ergebnisse der Sub-Agenten:\n\n"
                            f"{combined_results}\n\n"
                            f"Bitte erstelle eine KURZE ZUSAMMENFASSUNG dieser Ergebnisse für den Benutzer auf DEUTSCH.\n"
                            f"Konzentriere dich auf den Inhalt (was wurde gefunden/getan).\n"
                            f"Bleib prägnant aber informativ.\n"
                            f"Du kannst `read_file` nutzen, wenn du den Inhalt sehen musst.\n"
                            f"ANTWORTE AUSSCHLIESSLICH AUF DEUTSCH."
                        )
                    else:
                        instruction_prompt = (
                            f"The sub-agent(s) have completed their tasks.\n\n"
                            f"**RESULTS:**\n{combined_results}\n\n"
                            f"Please provide a BRIEF SUMMARY of these results for the user in {native_lang}.\n"
                            f"Focus on the content (what was found/done).\n"
                            f"Keep it concise but informative.\n"
                            f"You may use `read_file` if you need to see the content before summarizing.\n"
                            f"RESPOND EXCLUSIVELY IN {native_lang.upper()}."
                        )

                    response = agent.chat_step(
                        instruction_prompt,
                        stream_callback=simple_stream_callback,
                        skip_input=False,
                        disable_workflows=True,
                        disable_tools=False
                    )
                    if response:
                        tui.message_box(response, title="Answer", role="assistant")
                except Exception as e:
                    tui.error(f"Error processing result: {e}")
                
                # After processing results, the conversation flow continues
                # Fall through to show input prompt for user's next message
                tui.console.print()
            
            # Show input prompt for user
            user_input = tui.input_box(
                prompt="Message",
                placeholder="Type your message... (@ for files, / for commands)",
                check_for_auto_exit=True  # Enable auto-exit when sub-agent results are ready
            )
            
            # CRITICAL: Check again for sub-agent results AFTER user input
            # (in case results arrived while user was typing)
            immediate_results = _check_subagent_results(tui, agent)
            if immediate_results:
                # New results found! Process them immediately and loop back
                tui.info("[i] Processing new sub-agent results...")
                try:
                    # Same processing logic as above
                    user_lang = "auto"
                    for msg in reversed(agent.history):
                        if msg.get("role") == "user":
                            user_lang = agent._detect_user_language(msg.get("content", ""))
                            break
                    
                    native_lang = agent.LANGUAGE_NAMES_NATIVE.get(user_lang, user_lang)
                    combined_results = "\n\n---\n\n".join(r[:1000] for r in immediate_results)
                    
                    def simple_stream_callback(text):
                        tui.console.print(text, end="", markup=True, style=f"bold {tui.primary}")

                    if user_lang == "de":
                        instruction_prompt = (
                            f"Hier sind die Ergebnisse der Sub-Agenten:\n\n"
                            f"{combined_results}\n\n"
                            f"Bitte erstelle eine KURZE ZUSAMMENFASSUNG dieser Ergebnisse für den Benutzer auf DEUTSCH.\n"
                            f"Konzentriere dich auf den Inhalt (was wurde gefunden/getan).\n"
                            f"Bleib prägnant aber informativ.\n"
                            f"Du kannst `read_file` nutzen, wenn du den Inhalt sehen musst.\n"
                            f"ANTWORTE AUSSCHLIESSLICH AUF DEUTSCH."
                        )
                    else:
                        instruction_prompt = (
                            f"The sub-agent(s) have completed their tasks.\n\n"
                            f"**RESULTS:**\n{combined_results}\n\n"
                            f"Please provide a BRIEF SUMMARY of these results for the user in {native_lang}.\n"
                            f"Focus on the content (what was found/done).\n"
                            f"Keep it concise but informative.\n"
                            f"You may use `read_file` if you need to see the content before summarizing.\n"
                            f"RESPOND EXCLUSIVELY IN {native_lang.upper()}."
                        )

                    response = agent.chat_step(
                        instruction_prompt,
                        stream_callback=simple_stream_callback,
                        skip_input=False,
                        disable_workflows=True,
                        disable_tools=False
                    )
                    if response:
                        tui.message_box(response, title="Answer", role="assistant")
                except Exception as e:
                    tui.error(f"Error processing result: {e}")
                
                # Show countdown and auto-continue
                tui.console.print()
                for remaining in range(3, 0, -1):
                    tui.console.print(f"\r[dim]Auto-continuing in {remaining}s (press Ctrl+C to type a message)...[/dim]", end="")
                    time.sleep(1.0)
                tui.console.print("\r[dim]Auto-continuing...                                                    [/dim]")
                continue
            
            if user_input is None:
                break
            
            user_input = user_input.strip()
            
            # If user pressed empty Enter, treat it as "check for more results"
            # This allows the loop to quickly re-check instead of waiting for manual input
            if not user_input:
                # Empty input - just loop back to check for results
                continue
            
            # ═══════════════════════════════════════════════════════════════
            # KEYBOARD SHORTCUTS (single letters)
            # ═══════════════════════════════════════════════════════════════
            
            if user_input.lower() in ("s", "settings"):
                from vaf.cli.cmd import settings
                settings.main_menu(agent=agent)
                tui.clear()
                tui.logo_minimal()
                
                tui.event("System", "Reloading...", style="dim")
                agent.shutdown()
                agent = Agent(verbose=verbose)
                global_agent = agent
                agent.load_model()
                agent.init_chat()
                continue
            
            if user_input.lower() in ("c", "model"):
                from vaf.cli.cmd import settings
                settings.select_model_menu()
                tui.clear()
                tui.logo_minimal()
                
                tui.event("System", "Reloading...", style="dim")
                agent.shutdown()
                agent = Agent(verbose=verbose)
                global_agent = agent
                agent.load_model()
                agent.init_chat()
                continue
            
            if user_input.lower() in ("t", "theme"):
                from vaf.cli.cmd import settings
                settings.select_theme_menu()
                # Reload TUI with new theme
                from vaf.core.config import Config
                new_theme = Config.get("theme", "vaf")
                tui = TUI(new_theme)
                tui.clear()
                tui.logo_minimal()
                tui.success(f"Theme changed to: {new_theme}")
                continue
            
            if user_input.lower() in ("h", "history"):
                # Show session history
                sessions = session_mgr.list(limit=10)
                if sessions:
                    tui.table(
                        headers=["ID", "Name", "Messages", "Updated"],
                        rows=[[s["id"], s["name"][:25], s["message_count"], s["updated_at"][:10]] for s in sessions],
                        title="Recent Sessions"
                    )
                else:
                    tui.info("No saved sessions yet.")
                continue
            
            if user_input == "?":
                tui.panel("""
**Keyboard Shortcuts:**
  S  - Open Settings
  C  - Change Model  
  T  - Change Theme
  H  - Session History
  ?  - This Help

**Commands (with or without /):**
  exit, quit    - Exit VAF
  clear         - Clear conversation
  tools         - Show loaded tools
  help          - Show full help
  halt, stop    - Stop speech output (TTS)

**Special:**
  @filename     - Attach file content
  Tab           - Accept autocomplete
  ->            - Accept suggestion
                """, title="Help", style="info")
                continue
            
            # ═══════════════════════════════════════════════════════════════
            # SLASH COMMANDS (also works without / if typed alone)
            # ═══════════════════════════════════════════════════════════════
            
            # Known commands that work with or without /
            KNOWN_COMMANDS = {"exit", "quit", "q", "clear", "help", "settings", 
                             "theme", "tools", "undo", "restore", "context", "session", "listen", "l", "halt", "stop", "quiet", "stfu"}
            
            # Check if input is a command (with / or standalone single word)
            is_command = False
            cmd = ""
            args = []
            
            if user_input.startswith("/"):
                # Traditional /command
                cmd = user_input[1:].lower().split()[0]
                args = user_input.split()[1:] if len(user_input.split()) > 1 else []
                is_command = True
            else:
                # Check if it's a single word that matches a known command
                words = user_input.strip().split()
                if len(words) == 1 and words[0].lower() in KNOWN_COMMANDS:
                    cmd = words[0].lower()
                    is_command = True
                elif len(words) >= 2 and words[0].lower() in {"theme"}:
                    # Special case: "theme dark" works without /
                    cmd = words[0].lower()
                    args = words[1:]
                    is_command = True
            
            if is_command:
                
                if cmd in ("exit", "quit", "q"):
                    _handle_exit(tui, session_mgr, current_session)
                    break
                
                elif cmd == "clear":
                    agent.init_chat()
                    tui.clear()
                    tui.logo_minimal()
                    tui.success("Conversation cleared.")
                    continue
                
                elif cmd == "help":
                    tui.panel("""
**Commands (/ optional when typed alone):**

exit, quit      - Exit (saves session)
clear           - Clear conversation
help            - Show this help
settings        - Open settings
theme <name>    - Change theme (e.g., "theme dark")
tools           - Show loaded tools
listen, l       - Start voice input (STT)
halt, stop      - Stop speech output (TTS)
undo            - Undo last code change
context         - Show context status
restore         - Restore full context

**Note:** Commands work without / when typed alone.
         "exit" = "/exit", but "I want to exit" -> sent to AI

**Shortcuts:**
@filename       - Attach file content
Tab             - Autocomplete
?               - Quick help
                        """, title="Help", style="info")
                    continue
                
                elif cmd == "settings":
                    from vaf.cli.cmd import settings
                    settings.main_menu(agent=agent)
                    tui.clear()
                    tui.logo_minimal()
                    
                    tui.event("System", "Reloading...", style="dim")
                    agent.shutdown()
                    agent = Agent(verbose=verbose)
                    global_agent = agent
                    agent.load_model()
                    agent.init_chat()
                    continue
                
                elif cmd == "theme":
                    if args:
                        new_theme = args[0]
                        if ThemeManager.set_theme(new_theme):
                            tui = TUI(new_theme)
                            tui.success(f"Theme: {new_theme}")
                        else:
                            tui.error(f"Unknown theme: {new_theme}")
                    else:
                        themes = ", ".join(ThemeManager.list_themes())
                        tui.info(f"Themes: {themes}")
                    continue
                
                elif cmd == "tools":
                    if hasattr(agent, 'tools'):
                        tools_list = [f"{name}: {tool.description[:50]}..." for name, tool in agent.tools.items()]
                        tui.list_items(tools_list, title="Loaded Tools", numbered=True)
                    else:
                        tui.warning("No tools loaded")
                    continue
                
                elif cmd == "undo":
                    try:
                        from vaf.core.snapshot import Snapshot
                        snap = Snapshot()
                        if snap.undo():
                            tui.success("Undone to last snapshot")
                        else:
                            tui.warning("No snapshot available")
                    except Exception as e:
                        tui.error(f"Undo failed: {e}")
                    continue
                
                elif cmd == "restore":
                    # Restore full context from archive
                    if agent.restore_context():
                        tui.success("Full context restored from archive!")
                    continue
                
                elif cmd == "context":
                    # Show context status
                    status = agent.get_context_status()
                    usage_bar = "█" * int(status['usage_percent'] * 20) + "░" * (20 - int(status['usage_percent'] * 20))
                    tui.panel(f"""
**Context Status:**

Tokens: {status['tokens']:,} / {status['max_tokens']:,}
Usage:  [{usage_bar}] {status['usage_percent']:.0%}
Messages: {status['messages']}

**Tracked State:**
• Intent: {status['intent_goal'] or 'Not set'}
• Files touched: {status['files_touched']}
• Errors logged: {status['errors']}
• Archives available: {status['archives_available']}

**Commands:**
/restore - Restore full context after compression
/clear   - Clear all context
""", title="📊 Context Manager", style="info")
                    continue
                
                elif cmd == "session":
                    # Session management
                    if not args:
                        # List all sessions
                        sessions = session_mgr.list(limit=20)
                        if not sessions:
                            tui.info("No saved sessions found.")
                        else:
                            from rich.table import Table
                            table = Table(title="Saved Sessions", show_header=True, header_style="bold cyan")
                            table.add_column("ID", style="cyan", width=12)
                            table.add_column("Name", width=30)
                            table.add_column("Messages", justify="right", width=10)
                            table.add_column("Updated", width=12)
                            table.add_column("Summary", width=40)
                            
                            for s in sessions:
                                updated = s["updated_at"][:10] if s["updated_at"] else "?"
                                table.add_row(
                                    s["id"][:12],
                                    s["name"][:30],
                                    str(s["message_count"]),
                                    updated,
                                    s["summary"][:40] if s.get("summary") else "-"
                                )
                            
                            tui.console.print(table)
                            tui.info(f"\nUse: /session <id> to load a session")
                    elif args[0] == "list":
                        # Explicit list command
                        sessions = session_mgr.list(limit=50)
                        if not sessions:
                            tui.info("No saved sessions found.")
                        else:
                            from rich.table import Table
                            table = Table(title="All Saved Sessions", show_header=True, header_style="bold cyan")
                            table.add_column("ID", style="cyan", width=12)
                            table.add_column("Name", width=30)
                            table.add_column("Messages", justify="right", width=10)
                            table.add_column("Updated", width=12)
                            
                            for s in sessions:
                                updated = s["updated_at"][:10] if s["updated_at"] else "?"
                                table.add_row(
                                    s["id"][:12],
                                    s["name"][:30],
                                    str(s["message_count"]),
                                    updated
                                )
                            
                            tui.console.print(table)
                    elif args[0] == "current":
                        # Show current session info
                        if current_session:
                            tui.panel(f"""
**Current Session:**
ID: {current_session.id}
Name: {current_session.name}
Messages: {len(current_session.messages)}
Created: {current_session.created_at}
""", title="📋 Current Session", style="info")
                        else:
                            tui.warning("No active session")
                    else:
                        # Try to load session by ID
                        session_id = args[0]
                        try:
                            loaded_session = session_mgr.load(session_id)
                            tui.success(f"Loaded session: {loaded_session.name}")
                            tui.info(f"  {len(loaded_session.messages)} messages")
                            
                            # Ask if user wants to switch to this session
                            if tui.confirm("Switch to this session?", default=False):
                                current_session = loaded_session
                                # Restore messages to agent history
                                agent.init_chat()
                                for msg in loaded_session.messages:
                                    if msg.get("role") == "user":
                                        agent.history.append({"role": "user", "content": msg.get("content", "")})
                                    elif msg.get("role") == "assistant":
                                        agent.history.append({"role": "assistant", "content": msg.get("content", "")})
                                tui.success("Session restored to agent context!")
                        except FileNotFoundError:
                            tui.error(f"Session not found: {session_id}")
                        except Exception as e:
                            tui.error(f"Failed to load session: {e}")
                    continue
                
                elif cmd in ("listen", "l"):
                    # CRITICAL: Stop TTS before starting STT to prevent interference
                    try:
                        from vaf.core.speech import get_speech_manager
                        sm = get_speech_manager()
                        sm.stop()
                    except Exception:
                        pass  # Ignore errors, STT should work even if TTS fails to stop
                    
                    captured = tui.listen_overlay()
                    if captured:
                        user_input = captured
                        # Don't continue - fall through to message processing
                    else:
                        continue
                
                elif cmd in ("halt", "stop", "quiet", "stfu"):
                    # Stop TTS immediately
                    try:
                        from vaf.core.speech import get_speech_manager
                        sm = get_speech_manager()
                        sm.stop()
                        tui.success("🔇 Sprachausgabe gestoppt")
                    except Exception as e:
                        tui.error(f"Fehler beim Stoppen: {e}")
                    continue
                
                else:
                    tui.warning(f"Unknown command: /{cmd}")
                    continue
            
            # File attachments
            if "@" in user_input:
                import re
                def replace_file(match):
                    path = match.group(1)
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            content = f.read()
                        return f"\n\n--- FILE: {path} ---\n{content}\n----------------\n"
                    except Exception as e:
                        tui.error(f"Failed to attach {path}: {e}")
                        return match.group(0) 
                
                user_input = re.sub(r'@([\w\./\\-]+)', replace_file, user_input)
            
            # Add to session
            current_session.add_message("user", user_input)
            
            # Process with agent (user input already visible from input box)
            _process_agent_message(agent, user_input, tui, current_session)
            
        except KeyboardInterrupt:
            tui.newline()
            if tui.confirm("Exit?"):
                _handle_exit(tui, session_mgr, current_session)
                break
        except Exception as e:
            tui.error(f"Error in interaction loop: {e}")
            import traceback
            # Save traceback to log for debugging
            with open("logs/crash.log", "a", encoding="utf-8") as f:
                # Make sure logs dir exists
                os.makedirs("logs", exist_ok=True)
                f.write(f"\n--- {datetime.now().isoformat()} ---\n")
                f.write(traceback.format_exc())
            tui.info("Traceback saved to logs/crash.log")
    
    # Cleanup
    agent.shutdown()
    tui.print(f"\n[{tui.muted}]Goodbye![/{tui.muted}]")


def _handle_exit(tui, session_mgr, session):
    """Handle exit with session save prompt."""
    if not session.messages:
        tui.info("No messages to save.")
        return
    
    tui.newline()
    tui.print(f"[{tui.primary}]💾 Save Session?[/{tui.primary}]")
    tui.print(f"[{tui.muted}]   This session has {len(session.messages)} messages.[/{tui.muted}]")
    
    # Ask for confirmation
    save = tui.confirm("Save session before exit?", default=True)
    
    if save:
        # Save the session
        filepath = session_mgr.save(session)
        session_id = session.id
        
        tui.newline()
        tui.success(f"Session saved!")
        tui.print(f"   [{tui.muted}]ID:[/{tui.muted}] [{tui.accent}]{session_id}[/{tui.accent}]")
        tui.print(f"   [{tui.muted}]Location:[/{tui.muted}] {filepath}")
        
        tui.newline()
        tui.print(f"[{tui.primary}]📂 To restore this session later:[/{tui.primary}]")
        tui.print(f"   [{tui.accent}]vaf session load {session_id}[/{tui.accent}]")
        tui.print(f"   [{tui.muted}]or[/{tui.muted}]")
        tui.print(f"   [{tui.accent}]vaf run --session {session_id}[/{tui.accent}]")
        tui.newline()
    else:
        tui.info("Session not saved.")


def _process_agent_message(agent, user_input: str, tui, session):
    """Process a message through the agent."""
    response_parts = []
    
    # Delayed import to avoid circular dependencies
    try:
        from vaf.tools.coder import CodingAgentTool
    except ImportError:
        CodingAgentTool = None
    
    def stream_callback(text):
        response_parts.append(text)
        
        # CRITICAL: Suppress Main Agent output if Coder TUI is active!
        # This prevents "leaking stdout" that breaks the TUI layout
        if CodingAgentTool and CodingAgentTool._active_instance is not None:
            return
            
        tui.console.print(text, end="", markup=True, style=f"bold {tui.primary}")
    
    try:
        with tui.spinner("Thinking..."):
            pass  # Just show spinner briefly
        
        result_text = agent.chat_step(user_input, stream_callback=stream_callback)
        
        # Recognize special async acknowledgment marker
        is_async_ack = False
        if result_text and str(result_text).startswith("[ASYNC_ACK]"):
            is_async_ack = True
            result_text = str(result_text).replace("[ASYNC_ACK]", "")
        
        # IMPORTANT: Workflows may stream only progress ticks (e.g. "✓ Step 1/2: ...")
        # which are not the actual final answer. In that case, still print the returned result.
        if result_text:
            import re
            has_real_content = False
            for part in response_parts:
                p = str(part)
                # Treat workflow progress ticks as non-answer
                if re.search(r"✓\s*Step\s+\d+/\d+\s*:", p):
                    continue
                # Anything with non-whitespace counts as real content
                if p.strip():
                    has_real_content = True
                    break

            # If it's an async ack, we ALWAYS print it even if there was content
            if is_async_ack:
                tui.newline()
                tui.console.print(str(result_text), markup=True, style=f"bold {tui.primary}")
                # Ensure it's in the response parts for session saving
                response_parts.append(str(result_text))
            elif (not response_parts) or (not has_real_content):
                response_parts = [str(result_text)]
                tui.console.print(str(result_text), markup=True, style=f"bold {tui.primary}")
        tui.newline()
        
        # Save response to session
        full_response = "".join(response_parts)
        session.add_message("assistant", full_response)
        
        # Show token usage
        used, total = agent.get_token_usage()
        tui.progress_bar(used, total, label="Tokens")
        
    except Exception as e:
        tui.error(f"Agent error: {e}")


def _run_classic(message: str, verbose: bool, session_id: str = None):
    """Run with classic interface."""
    global global_agent
    
    # Session support for classic mode
    if session_id:
        UI.info(f"Session restore in classic mode not fully supported.")
        UI.info(f"Use: vaf run --session {session_id} (without --classic)")
    
    UI.event("System", "Initializing VAF...", style="dim")
    
    # ═══════════════════════════════════════════════════════════════
    # GIT CHECK - Must be installed before proceeding
    # ═══════════════════════════════════════════════════════════════
    # Create a simple TUI-like object for classic mode
    class SimpleTUI:
        def event(self, category, message, style="dim"):
            if style == "success":
                UI.success(f"{category}: {message}")
            elif style == "warning":
                UI.warning(f"{category}: {message}")
            elif style == "dim":
                UI.info(f"{category}: {message}")
            else:
                UI.info(f"{category}: {message}")
        
        def error(self, message):
            UI.error(message)
        
        def info(self, message):
            UI.info(message)
        
        def spinner(self, message):
            from contextlib import contextmanager
            @contextmanager
            def spinner_ctx():
                UI.info(message)
                try:
                    yield
                finally:
                    pass  # No cleanup needed
            return spinner_ctx()
    
    simple_tui = SimpleTUI()
    if not _check_and_install_git(simple_tui):
        UI.error("Cannot proceed without Git. Please install Git and restart VAF.")
        sys.exit(1)
    
    try:
        agent = Agent(verbose=verbose)
        global_agent = agent
        # Download model first (if needed) - this shows tqdm progress bar
        agent.ensure_model_exists()
        # Now load the model (skip download check since we already did it)
        agent.load_model(skip_download_check=True)
        agent.init_chat()
    except Exception as e:
        UI.error(f"Startup failed: {e}")
        sys.exit(1)

    UI.clear()
    UI.logo()
    
    # Check for direct message argument
    if message:
        UI.event("User", message, style="normal")
        agent.chat_step(message, stream_callback=lambda x: output_stream(x))
        
    # Interactive Loop
    while True:
        try:
            UI.print()
            user_input = UI.prompt("vaf> ")
        except KeyboardInterrupt:
            UI.print("\n[yellow]Exiting...[/yellow]")
            agent.shutdown()
            sys.exit(0)
        except EOFError:
            break
            
        if not user_input:
             continue

        # ═══════════════════════════════════════════════════════════════
        # COMMANDS (work with or without / when typed alone)
        # ═══════════════════════════════════════════════════════════════
        
        input_lower = user_input.lower().strip()
        input_words = input_lower.split()
        
        # Single word commands (work without /)
        if len(input_words) == 1:
            single_cmd = input_words[0].lstrip('/')
            
            if single_cmd in ("exit", "quit", "q"):
                agent.shutdown()
                break
            
            elif single_cmd in ("settings", "s"):
                from vaf.cli.cmd import settings
                settings.main_menu(agent=agent if 'agent' in locals() else global_agent)
                UI.clear()
                UI.logo()
                UI.event("System", "Reloading Configuration...", style="dim")
                agent.shutdown()
                agent = Agent(verbose=verbose) 
                global_agent = agent
                agent.load_model()
                agent.init_chat()
                continue
            
            elif single_cmd in ("model", "c"):
                from vaf.cli.cmd import settings
                settings.select_model_menu()
                UI.clear()
                UI.logo()
                UI.event("System", "Reloading Configuration...", style="dim")
                agent.shutdown()
                agent = Agent(verbose=verbose)  
                global_agent = agent
                agent.load_model()
                agent.init_chat()
                continue
            
            elif single_cmd == "clear":
                agent.init_chat()
                UI.clear()
                UI.logo()
                UI.success("Conversation cleared.")
                continue
            
            elif single_cmd == "help":
                UI.panel("Available Commands (/ optional)", style="cyan")
                UI.print("  exit, quit     Exit VAF")
                UI.print("  clear          Reset conversation")
                UI.print("  settings       Open Settings")
                UI.print("  tools          Show loaded tools")
                UI.print("  listen, l      Start voice input (STT)")
                UI.print("  halt, stop     Stop speech output (TTS)")
                UI.print("  help           Show this help")
                UI.print("  @filename      Attach a file")
                UI.print("")
                UI.print("  [bold]TIP:[/bold] Use [cyan]vaf run --modern[/cyan] for new UI!")
                continue
            
            elif single_cmd == "tools":
                from vaf.cli.cmd import settings
                settings.show_tools_menu(agent)
                UI.clear()
                UI.logo()
                continue
            
            elif single_cmd in ("listen", "l"):
                # CRITICAL: Stop TTS before starting STT to prevent interference
                try:
                    from vaf.core.speech import get_speech_manager
                    sm = get_speech_manager()
                    sm.stop()
                except Exception:
                    pass  # Ignore errors, STT should work even if TTS fails to stop
                
                from vaf.cli.tui import get_tui
                captured = get_tui().listen_overlay()
                if captured:
                    UI.event("User (Voice)", captured, style="normal")
                    agent.chat_step(captured, stream_callback=lambda x: output_stream(x))
                    # Show Usage Bar
                    used, total = agent.get_token_usage()
                    UI.print_usage_bar(used, total)
                continue
            
            elif single_cmd in ("halt", "stop", "quiet", "stfu"):
                # Stop TTS immediately
                try:
                    from vaf.core.speech import get_speech_manager
                    sm = get_speech_manager()
                    sm.stop()
                    UI.success("🔇 Sprachausgabe gestoppt")
                except Exception as e:
                    UI.error(f"Fehler beim Stoppen: {e}")
                continue

            elif single_cmd == "install-gpu":
                from vaf.cli.cmd import info
                info.install_gpu()
                UI.console.input("[dim]Press Enter to reload...[/dim]")
                
                UI.clear() 
                UI.logo()
                
                UI.event("System", "Reloading...", style="dim")
                agent.shutdown()
                agent = Agent(verbose=verbose)
                global_agent = agent 
                agent.load_model()
                agent.init_chat()
                continue
            
            # Unknown single-word starting with / is an error
            elif user_input.startswith("/"):
                UI.error(f"Unknown command: {single_cmd}")
                continue
            
            # Otherwise, it's just a single word message to the AI
            # (fall through to chat)

        # File Attachments (@filename)
        if "@" in user_input:
            import re
            def replace_file(match):
                path = match.group(1)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read()
                    return f"\n\n--- FILE: {path} ---\n{content}\n----------------\n"
                except Exception as e:
                    UI.error(f"Failed to attach {path}: {e}")
                    return match.group(0) 
            
            user_input = re.sub(r'@([\w\./\\-]+)', replace_file, user_input)

        agent.chat_step(user_input, stream_callback=lambda x: output_stream(x))
        
        # Show Usage Bar
        used, total = agent.get_token_usage()
        UI.print_usage_bar(used, total)

def output_stream(text):
    UI.console.print(text, end="", markup=True, style="bold cyan")