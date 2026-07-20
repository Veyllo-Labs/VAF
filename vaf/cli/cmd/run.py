# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import typer
import sys
import os
import signal
import time
import requests
import platform
import subprocess
from datetime import datetime
from rich.console import Console
from rich.panel import Panel
from vaf.core.agent import Agent
from vaf.cli.ui import UI
from vaf.core.log_helper import get_dated_log_path
from vaf.core.web_server import start_background_server
from vaf.core.web_interface import get_web_interface
import threading


def _make_cli_agent(verbose: bool = False, host_audio: bool = True) -> Agent:
    """Create the interactive CLI agent and bind it to the local-admin identity.

    Why this exists
    ---------------
    The CLI has no authentication: the local user is always the admin. Unlike the
    Web/Channel path (``vaf.core.gateway.run_agent_step``), the CLI calls
    ``Agent.chat_step()`` directly and never resolves a ``user_scope_id``. Without
    binding it here the agent runs under scope ``None`` -> the ``"default"`` bucket,
    which is a *different* identity from the admin scope the WebUI uses. Because
    ``last_interaction`` and memory/RAG are both keyed by ``user_scope_id``, this made
    the CLI show stale/empty context (e.g. "last interaction 10 days ago", "no memories
    about you") even though the same human was active in the WebUI.

    What it does
    ------------
    Applies the exact fallback the gateway uses for local (unauthenticated) calls: binds
    the agent to ``get_local_admin_scope_id()`` / ``get_local_admin_username()``. This is
    re-applied on *every* agent (re)creation — initial start and config/session reloads —
    because ``Agent.__init__`` does not set a scope, so a reload would otherwise drop back
    to ``"default"``.

    Scope note
    ----------
    CLI-only. The Web/Channel paths keep their server-validated JWT scope and are NOT
    affected; the multi-user separation there is untouched.
    """
    # host_audio: the interactive CLI is the only lane with a human at this
    # machine's speakers; run_prompt (-p) passes False (machine/script lane).
    a = Agent(verbose=verbose, run_kind="chat", host_audio=host_audio)
    try:
        from uuid import UUID
        from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
        # Mirror gateway.run_agent_step's local-admin fallback: store the scope as a UUID
        # (memory tools expect a UUID). If the configured value is not a valid UUID, leave
        # the scope unset rather than guessing — identical to the gateway's behaviour.
        a._current_user_scope_id = UUID(str(get_local_admin_scope_id()))
        a._current_username = get_local_admin_username()
    except Exception:
        # Identity binding must never block the CLI from starting; fall back to defaults.
        pass
    return a


def _quiet_cli_http_logs() -> None:
    """Silence per-request httpx/httpcore INFO logs on the CLI console.

    The OpenAI/DeepSeek SDK logs every call as
    ``INFO:httpx:HTTP Request: POST .../chat/completions "HTTP/1.1 200 OK"``.
    ``vaf.core.gateway`` calls ``logging.basicConfig(level=INFO)`` on import, which routes
    those records to the console. We raise the *logger* level (not the root logger) for
    httpx/httpcore so those INFO records are dropped at the source — this keeps working
    regardless of what the root logger is set to, and mirrors the existing approach in
    ``vaf.tools.research_agent``.

    Called only from the interactive CLI commands (``vaf run`` / ``vaf prompt``), never at
    import time, so the headless/server/tray processes keep their logging untouched.
    """
    import logging
    for _name in ("httpx", "httpcore"):
        logging.getLogger(_name).setLevel(logging.WARNING)


def _heartbeat_loop(interval=5):
    """Background thread to send heartbeats to the persistent server (Tray App)."""
    import uuid
    client_id = str(uuid.uuid4())
    while True:
        try:
            # TLS-aware internal base: with local_network_tls_enabled the public
            # 8001 port speaks HTTPS - a hardcoded plain-HTTP POST dies silently.
            from vaf.core.web_interface import internal_api_base
            requests.post(
                f"{internal_api_base()}/api/heartbeat",
                json={"client_id": client_id, "timestamp": time.time()},
                timeout=1
            )
        except:
            pass # Use silence if server is not running or unreachable
        time.sleep(interval)


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
            # Check if a paused workflow is waiting for this result, and CLAIM it before
            # acting on it. The headless/web drain can see the same finished task, and a
            # read-then-resume would let both replay the remaining steps.
            # claim_paused_workflow pops the record atomically: exactly one winner, and the
            # loser falls through to the ordinary result handling below.
            paused_wf = ipc.get_paused_workflow_for_task(task.task_id)
            if paused_wf is not None and task.status == "completed":
                paused_wf = ipc.claim_paused_workflow(paused_wf.workflow_id)

            if task.status == "completed":
                # Detect if this is a workflow result (agent_type starts with "workflow:")
                is_workflow = task.agent_type.startswith("workflow:")
                
                if paused_wf:
                    tui.console.print()
                    tui.console.print(Panel(
                        f"[bold green][OK] Sub-Agent complete - resuming workflow[/bold green]\n\n"
                        f"[cyan]Task:[/cyan] {task.task_id}\n"
                        f"[cyan]Agent:[/cyan] {task.agent_type}\n"
                        f"[cyan]Workflow:[/cyan] {paused_wf.workflow_name}",
                        title="[>] Workflow Resuming",
                        border_style="green"
                    ))

                    # Resume the workflow (record already claimed above)
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

                # WW B-track for the ASYNC lane: this CLI TUI drain consumes results
                # BEFORE the runner drain ever sees them, so it must attach the failed
                # tool's know-how itself (same shared helper as
                # Agent._process_subagent_result; fail-safe).
                _kh_block = ""
                try:
                    from vaf.whare_wananga.runtime import async_failure_hint
                    _kh = async_failure_hint(agent, task.agent_type, str(task.error or ""))
                    if _kh:
                        _kh_block = f"\n{_kh}"
                except Exception:
                    _kh_block = ""

                agent.history.append({
                    "role": "system",
                    "content": f"**Sub-Agent Error [{task.task_id}]** ({task.agent_type}):\n{task.error}"
                               + _kh_block
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
        kwargs = {'capture_output': True, 'text': True, 'timeout': 5}
        if platform.system() == "Windows":
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(['git', '--version'], **kwargs)
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
                    timeout=300,  # 5 minutes
                    creationflags=subprocess.CREATE_NO_WINDOW
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
                    creationflags=subprocess.CREATE_NO_WINDOW
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
        # Check if server is alive first
        try:
            requests.get("http://127.0.0.1:8080/health", timeout=2)
        except:
            return "error:server_unreachable"

        # Try warmup with long timeout (loading large models can take minutes)
        for attempt in range(3):
            try:
                response = requests.post(
                    "http://127.0.0.1:8080/v1/chat/completions",
                    json=warmup_payload,
                    timeout=300 # 5 minutes max for initial load
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
                # If 5 minutes wasn't enough, it's likely stuck or swapping heavily
                return "timeout"
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

def _cleanup_before_exit():
    """Helper to clean up empty sessions on exit."""
    try:
        from vaf.core.session import SessionManager
        count = SessionManager().cleanup_empty()
        if count > 0:
            print(f"\n[VAF] Cleaned up {count} empty session(s).")
    except:
        pass

def signal_handler(sig, frame):
    """Handles Ctrl+C to suppress Windows batch prompt and clean up."""
    if global_agent:
        try:
           global_agent.shutdown()
        except:
           pass
    
    _cleanup_before_exit()
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
    session: str = typer.Option(None, "--session", "-s", help="Load a previous session by ID"),
    web: bool = typer.Option(False, "--web/--no-web", help="Start Web UI server (default: False)")
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

    # CLI-only: drop the noisy "INFO:httpx:HTTP Request ..." lines from the chat console.
    _quiet_cli_http_logs()

    from vaf.core.config import Config

    # One-line "update available" hint (throttled; opt out via update_check_on_start).
    try:
        from vaf.cli.cmd.update import maybe_notify_update
        maybe_notify_update()
    except Exception:
        pass

    # Determine UI mode from flag (default to modern)
    ui_mode = "classic" if classic else "modern"
    
    # Get theme from config if not specified
    if not theme:
        theme = Config.get("theme", "vaf")
    
    # Run appropriate interface
    if ui_mode == "modern":
        _run_modern(message, verbose, theme, session, web_enabled=web)
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
    from vaf.cli.ui import UI
    from vaf.core.session import SessionManager

    # CLI-only: drop the noisy "INFO:httpx:HTTP Request ..." lines from the console.
    _quiet_cli_http_logs()

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

    # Non-interactive machine lane: never play audio on this host's speakers.
    agent = _make_cli_agent(verbose=False, host_audio=False)
    agent.init_chat()
    # Same local-mode guard as `vaf prompt` (main.py): without it every
    # local-mode run of this alias returned an empty answer.
    if agent.api_backend is None and not agent.llm and not agent.use_server:
        agent.load_model()

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
    
    try:
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

    finally:
        _cleanup_before_exit()

    if save_session:
        s = mgr.new(model=agent.config.get("model", ""), project_path=os.getcwd())
        s.add_message("user", prompt)
        s.add_message("assistant", result_text or "")
        mgr.save(s)
        ndjson_emit({"type": "session_saved", "id": s.id})

    ndjson_emit({"type": "end"})


def _run_modern(message: str, verbose: bool, theme: str, session_id: str = None, web_enabled: bool = True):
    """Run with modern TUI interface."""
    global global_agent
    
    try:
        from vaf.cli.tui import TUI
        from vaf.cli.themes import ThemeManager
        from vaf.core.session import SessionManager
        from vaf.core.config import Config
    except ImportError as e:
        UI.error(f"Modern TUI not available: {e}")
        UI.info("Falling back to classic interface...")
        _run_classic(message, verbose, session_id)
        return
    
    # Set theme
    ThemeManager.set_theme(theme)
    tui = TUI(theme)
    
    # Process handle for Web UI
    npm_process = None
    
    # Session management
    session_mgr = SessionManager()
    
    # Note: After agent is created, we update session_mgr with state_registry
    # This is done later to enable automatic state sync on save/load
    
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
            session_mgr.save(current_session) # Initial save
    else:
        current_session = session_mgr.new()
        session_mgr.save(current_session) # Initial save so it shows in Web UI
    
    # ═══════════════════════════════════════════════════════════════
    # TRAY APP CHECK / AUTO-START
    # ═══════════════════════════════════════════════════════════════
    # Check if the persistent server (WebUI port 8001) is running.
    # If not, try to start 'vaf tray' in the background.
    
    tray_running = False
    try:
        from vaf.core.web_interface import internal_api_base
        requests.get(f"{internal_api_base()}/health", timeout=0.2)
        tray_running = True
    except:
        pass
    
    if not tray_running and web_enabled:
        tui.event("System", "Starting background service (Tray App)...", style="dim")
        try:
            # Launch vaf tray detached
            if platform.system() == "Windows":
                # Windows: DETACHED_PROCESS to hide console
                subprocess.Popen(
                    [sys.executable, "-m", "vaf.main", "tray"],
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                    close_fds=True
                )
            else:
                # macOS/Linux: nohup equivalent
                subprocess.Popen(
                    [sys.executable, "-m", "vaf.main", "tray"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setpgrp
                )
            
            # Wait briefly for it to initialize (max 3s)
            with tui.spinner("Waiting for background service..."):
                from vaf.core.web_interface import internal_api_base
                for _ in range(30):
                    try:
                        requests.get(f"{internal_api_base()}/health", timeout=0.2)
                        tray_running = True
                        break
                    except:
                        time.sleep(0.1)
                        
            if tray_running:
                tui.event("System", "Background service started.", style="success")
            else:
                 tui.event("Warning", "Could not verify background service startup (proceeding anyway).", style="warning")

        except Exception as e:
            tui.event("Warning", f"Failed to auto-start tray app: {e}", style="warning")

    # ═══════════════════════════════════════════════════════════════
    # SESSION-BASED SUB-AGENT CLEANUP
    # ═══════════════════════════════════════════════════════════════
    # Set current session ID for sub-agent tracking and clean up stale tasks
    from vaf.core.subagent_ipc import set_current_session_id, cleanup_other_sessions

    set_current_session_id(current_session.id)
    cleanup_other_sessions()  # Remove active tasks from previous sessions
    
    # ═══════════════════════════════════════════════════════════════
    # LEHRE-CHAT CLEANUP - Delete all old empty sessions
    # ═══════════════════════════════════════════════════════════════
    # Clean up empty sessions (Lehre-Chats) from previous runs
    # This prevents accumulation of teaching sessions in the database
    # Note: Current session is excluded from cleanup to allow it to exist during runtime
    try:
        cleanup_count = session_mgr.cleanup_empty(exclude_session_id=current_session.id)
        if cleanup_count > 0:
            tui.event("Cleanup", f"Removed {cleanup_count} empty Lehre-Chat(s) from previous runs", style="dim")
    except Exception as e:
        # Don't block startup if cleanup fails
        tui.event("Warning", f"Cleanup warning: {e}", style="warning")

    
    # Initialize agent
    tui.clear()
    tui.logo()
    tui.event("System", "Initializing VAF...", style="dim")
    
    # Check Config for Web UI preference (defaults to True)
    config_web_enabled = Config.get("web_ui_enabled")
    if config_web_enabled is None:
        config_web_enabled = True
    
    # CLI flag overrides Config only if explicitly disabled via --no-web
    # In Typer, the default for 'web' is True.
    # If the user does NOT pass --no-web, 'web' is True.
    # In that case, we should respect the config.
    # If the user passes --no-web, 'web' is False. We should disable.
    
    # Simple logic: If it's enabled in CLI, respect config. If disabled in CLI, it's disabled.
    if web_enabled:
        web_enabled = config_web_enabled
        
    # Start Heartbeat Thread (keeps persistent server model loaded)
    hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    hb_thread.start()
    
    # ═══════════════════════════════════════════════════════════════
    # GIT CHECK - Must be installed before proceeding
    # ═══════════════════════════════════════════════════════════════
    if not _check_and_install_git(tui):
        tui.error("Cannot proceed without Git. Please install Git and restart VAF.")
        sys.exit(1)
    
    try:
        agent = _make_cli_agent(verbose=verbose)
        global_agent = agent
        
        # Register Agent with Web Interface for 2-way control
        try:
            get_web_interface().register_agent(agent)
        except Exception:
            pass
        
        # Connect agent's state registry to session manager for automatic state sync
        if hasattr(agent, 'state_registry'):
            session_mgr.state_registry = agent.state_registry
        
        # CRITICAL FIX: Sync Agent's internal session ID with the User's Session ID
        # The Agent generates a random UUID in __init__, but we want it to use the persistent session ID
        # so that WebSocket updates (which use agent._session_id) match what the Frontend expects.
        try:
            # 1. Provide a way to use the existing session ID if possible
            if hasattr(agent, '_session_id') and agent._session_id != current_session.id:
                # Unregister the temporary random session ID
                agent._unregister_session()
                # Update to the real session ID
                agent._session_id = current_session.id 
                # Register the real session ID
                agent._register_session()
        except Exception as e:
            if verbose:
                tui.error(f"Failed to sync session ID: {e}")
        
        # Initialize backend (local or API)
        if agent.provider == "local":
            # Download model first (if needed) - this shows tqdm progress bar
            # Do this BEFORE the spinner, so tqdm output is visible
            agent.ensure_model_exists()
            
            # Now load the model (this is fast if model already exists)
            # Skip download check since we already did it
            with tui.spinner("Loading model..."):
                agent.load_model(skip_download_check=True)
                agent.init_chat()
        else:
            # API provider - initialize API backend
            tui.event("System", f"Using API provider: {agent.provider.upper()}", style="dim")
            # load_model() also initializes API backend, so we need to call it
            agent.load_model(skip_download_check=True)
            agent.init_chat()
        
        # Show success after spinner ends (Backend Ready message moved here)
        if agent.use_server:
            tui.event("Server", "Backend Ready (GPU Accelerated)", style="success")
        elif agent.api_backend:
            tui.event("API", f"Backend Ready ({agent.provider.upper()})", style="success")
        
        # ═══════════════════════════════════════════════════════════════
        # MODEL WARMUP - Actually load model into VRAM before user prompt
        # Skip for API providers (no local model to warm up)
        # ═══════════════════════════════════════════════════════════════
        if not agent.api_backend:
            tui.event("System", "Warming up model...", style="dim")
            _warmup_model(tui)
            
            # Force tokenizer initialization here so the log message appears during boot
            # instead of during the first user interaction.
            agent.get_token_usage()
        else:
            tui.event("System", "API Backend ready - no warmup needed", style="dim")
        
        # ═══════════════════════════════════════════════════════════════
        # VOICE RESOURCE PRELOADING - Download/Init TTS/STT
        # ═══════════════════════════════════════════════════════════════
        # If TTS/STT are enabled, preload their resources NOW
        # (instead of lazy-loading mid-chat, which is disruptive)
        
        if agent.config.get("speech_tts_enabled", False):
            try:
                tui.event("Speech", "Preloading TTS resources...", style="dim")
                from vaf.core.speech import get_speech_manager
                sm = get_speech_manager()
                
                # Ensure Piper binary is installed
                sm._check_piper()
                
                # Download voice model for configured language
                lang = agent.config.get("speech_language", "en-US")[:2]  # Get language code (de, en, etc.)
                sm._ensure_voice_model(lang)
                
                tui.event("Speech", "TTS resources ready", style="success")
            except Exception as e:
                tui.warning(f"TTS preload failed: {e}")
        
        if agent.config.get("speech_stt_enabled", False):
            try:
                tui.event("Speech", "Checking STT microphone...", style="dim")
                from vaf.core.speech import get_speech_manager
                sm = get_speech_manager()
                
                # Verify microphone is accessible (triggers initialization)
                if sm.stt_mic:
                    tui.event("Speech", "STT microphone ready", style="success")
                else:
                    import importlib.util as _ilu
                    if _ilu.find_spec("pyaudio") is None:
                        # pyaudio is the optional speech extra now - without it the mic is not
                        # "missing", capture is simply not installed; say so instead of guessing.
                        tui.warning('STT enabled but pyaudio is not installed - CLI mic capture needs the optional speech extra: pip install pyaudio (or pip install "vaf[speech]")')
                    else:
                        tui.warning("STT enabled but no microphone detected")
            except Exception as e:
                tui.warning(f"STT check failed: {e}")
        
        # Preload Language Identification (if installed) to prevent lag during chat
        try:
            from vaf.vendor import langid
            tui.event("System", "Preloading language detection...", style="dim")
            langid.classify("test")
        except ImportError:
            pass

        # WEB UI STARTUP (Shared Logic)
        # ═══════════════════════════════════════════════════════════════
        if web_enabled:
            try:
                # 1. Start Python Backend (FastAPI + WebSocket)
                # Respect local_network_enabled setting for host binding
                local_network_enabled = Config.get("local_network_enabled", False)
                api_host = "0.0.0.0" if local_network_enabled else "127.0.0.1"
                tui.event("WebUI", f"Starting Backend API (Port 8001, Host: {api_host})...", style="dim")
                start_background_server(host=api_host, port=8001)
                
                # 2. Start Next.js Frontend
                from vaf.core.frontend_manager import FrontendManager
                fm = FrontendManager()
                
                # Callback to pipe logs to TUI
                def tui_log_callback(msg, style="dim"):
                    if style == "error": tui.error(msg)
                    elif style == "warning": tui.warning(msg)
                    elif style == "success": tui.event("WebUI", msg, style="success")
                    else: tui.event("WebUI", msg, style=style)

                port = fm.start_frontend(log_callback=tui_log_callback)
                
                if port:
                    # Wait for ready (CLI specific wait logic)
                    dashboard_url = f"http://localhost:{port}"
                    server_ready = False
                    for _ in range(30): # 15 seconds
                        try:
                            requests.get(dashboard_url, timeout=0.5)
                            server_ready = True
                            break
                        except:
                            time.sleep(0.5)
                    
                    if server_ready:
                        tui.event("WebUI", f"Dashboard active at {dashboard_url}", style="success")
                    else:
                        tui.warning("Dashboard launch taking longer than expected...")
                else:
                    tui.warning("Skipping frontend launch (failed or directory missing).")

            except Exception as e:
                tui.error(f"Web UI Startup FAILED: {e}")
                import traceback
                traceback.print_exc()

    except Exception as e:
        tui.error(f"Startup failed: {e}")
        sys.exit(1)
    
    # Welcome
    tui.clear()
    tui.logo()
    tui.newline()

    # (Previous Web UI startup block removed from here)
    # ═══════════════════════════════════════════════════════════════


    # Handle initial message
    if message:
        tui.message_box(message, role="user")
        _process_agent_message(agent, message, tui, current_session)
    
    # ═══════════════════════════════════════════════════════════════
    # PROACTIVE RESULT NOTIFIER (Background)
    # ═══════════════════════════════════════════════════════════════

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

    # Flag used to break from input_box when Web UI sends a message (no wake word)
    wake_word_detected_flag = threading.Event()

    # Interactive loop
    console_broken = False
    while True:
        try:
            # Note: Sub-agent status now shown in live toolbar (updates every second)
            
            # Show token usage (or message count for API)
            try:
                used, total = agent.get_token_usage()
                
                # Calculate stats for Web UI
                stats = {
                    "used": used,
                    "total": total,
                    "percent": 0.0,
                    "api": bool(agent.api_backend)  # Convert to boolean for JSON serialization
                }
                
                if agent.api_backend:
                    tui.console.print(f"[dim]Tokens: In: {used:,} | Out: {total:,}[/dim]", justify="right")
                else:
                    tui.progress_bar(used, total, label="Tokens")
                    if total > 0:
                        stats["percent"] = round((used / total) * 100, 1)
                
                # Broadcast to Web UI
                try:
                    get_web_interface().emit_stats(stats, session_id=current_session.id)
                except Exception:
                    pass
                
                # CRITICAL: Flush output before handing over control to prompt_toolkit
                # This prevents buffer conflicts on Windows
                sys.stdout.flush()
            except Exception:
                pass # Ignore rendering errors in status bar

            # Clear input-break flag from previous iteration (used by Web UI message path)
            wake_word_detected_flag.clear()

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
                    # if response:
                    #    tui.message_box(response, title="Answer", role="assistant")
                except Exception as e:
                    tui.error(f"Error processing result: {e}")
                
                # After processing results, the conversation flow continues
                # Fall through to show input prompt for user's next message
                tui.console.print()

            # Show input prompt for user
            if console_broken:
                try:
                    tui.console.print("\n[dim]> (Standard Input Mode)[/dim]")
                    user_input = input("Message: ")
                except EOFError:
                    break
            else:
                try:
                    # Check for Web UI Input BEFORE blocking on keyboard
                    # We need a non-blocking check or a patched input_box that accepts external events
                    # Since prompt_toolkit is hard to interrupt, we use a simple polling loop here 
                    # if we are waiting.
                    
                    # BUT prompt_toolkit's session.prompt() is blocking.
                    # We can use the 'wake_word_event' mechanism we already have!
                    # We can set the event when web input arrives.
                    
                    # 1. Check queue immediately
                    from vaf.core.task_queue import TaskQueue
                    tq = TaskQueue()
                    is_web_input = False
                    
                    if tq.get_queue_size() > 0:
                        task = tq.get()
                        agent.load_session_context(task.session_id)
                        
                        # Sync current_session with task session
                        try:
                            current_session = session_mgr.load(task.session_id)
                        except:
                            # Create session if not found (fallback)
                            current_session = session_mgr.new()
                            current_session.id = task.session_id
                            session_mgr.save(current_session)
                            
                        raw_input = task.input_text
                        is_web_input = True
                        
                        # CRITICAL: Sync agent session ID with current session
                        # This ensures WebSocket updates go to the correct session
                        if hasattr(agent, '_session_id') and agent._session_id != current_session.id:
                            agent._unregister_session()
                            agent._session_id = current_session.id
                            agent._register_session()
                        
                        # Timer fired: render the message proactively as the assistant — no LLM turn.
                        from vaf.core.timers import TIMER_MSG_PREFIX as _TIMER_PREFIX
                        if str(raw_input).startswith(_TIMER_PREFIX):
                            timer_msg = str(raw_input)[len(_TIMER_PREFIX):]
                            tui.message_box(timer_msg, role="assistant")
                            try:
                                if current_session is not None:
                                    current_session.add_message("assistant", timer_msg)
                                    session_mgr.save(current_session)
                                if isinstance(getattr(agent, "history", None), list):
                                    agent.history.append({"role": "assistant", "content": timer_msg})
                            except Exception:
                                pass
                            continue

                        # Handle System Commands from Web UI
                        if str(raw_input).startswith("__CMD__"):
                            try:
                                cmd_parts = str(raw_input).strip().split(":")
                                if len(cmd_parts) < 2:
                                    continue
                                    
                                cmd_type = cmd_parts[1].strip()
                                
                                if cmd_type == "NEW_SESSION":
                                    current_session = session_mgr.new()
                                    session_mgr.save(current_session) # Save immediately so ID is valid
                                    set_current_session_id(current_session.id)
                                    agent.init_chat()
                                    tui.event("WebUI", "Started new session", style="success")
                                    continue # Loop back to prompt
                                    
                                elif cmd_type == "LOAD_SESSION":
                                    sid = cmd_parts[2].strip()
                                    try:
                                        current_session = session_mgr.load(sid)
                                        set_current_session_id(current_session.id)
                                        # Restore agent history
                                        agent.init_chat()
                                        for msg in current_session.messages:
                                            if msg.get("role") in ["user", "assistant"]:
                                                agent.history.append({"role": msg.get("role"), "content": msg.get("content")})
                                        tui.event("WebUI", f"Switched to session: {current_session.name}", style="success")
                                    except Exception as e:
                                        tui.error(f"Failed to switch session: {e}")
                                    continue # Loop back to prompt

                                elif cmd_type == "RENAME_SESSION":
                                    # Format: __CMD__:RENAME_SESSION:id:new_name
                                    try:
                                        cmd_parts = str(raw_input).split(":", 3)
                                        sid = cmd_parts[2].strip()
                                        new_name = cmd_parts[3].strip()
                                        if current_session and current_session.id == sid:
                                            current_session.name = new_name
                                            # tui.event("WebUI", f"Session renamed to: {new_name}", style="dim")
                                    except: pass
                                    continue

                                elif cmd_type == "RELOAD_CONFIG":
                                    # Reload agent config and apply provider change (same logic as headless_runner)
                                    try:
                                        tui.event("System", "Config updated from WebUI", style="dim")
                                        from vaf.core.config import Config
                                        new_cfg = Config.load()
                                        if hasattr(agent, "config"):
                                            agent.config = new_cfg
                                        new_provider = new_cfg.get("provider", "local")
                                        old_provider = getattr(agent, "provider", "local")
                                        if old_provider != new_provider:
                                            agent.provider = new_provider
                                            if new_provider != "local":
                                                try:
                                                    from vaf.core.api_backend import APIBackendManager
                                                    agent.api_backend = APIBackendManager(new_provider)
                                                except Exception:
                                                    agent.api_backend = None
                                                agent.use_server = False
                                                agent.llm = None
                                            else:
                                                agent.api_backend = None
                                            tui.event("System", f"Provider switched to {new_provider.upper()}", style="dim")
                                    except Exception:
                                        pass
                                    continue
                                
                                else:
                                    # Consume unknown commands silently
                                    tui.warning(f"Ignored unknown command: {cmd_type}")
                                    continue

                            except Exception as e:
                                tui.error(f"Command processing error: {e}")
                                continue
                        
                        # Normal Chat Input
                        user_input = raw_input
                        tui.console.print(f"\n[bold green]Web UI Input:[/bold green] {user_input}")
                    else:
                        # 2. Wait for input with interrupt support
                        # We pass the queue to input_box to poll it internally? 
                        # Or simpler: we rely on the wake_word_flag to break the input loop!
                        
                        # Hack: We use a separate thread to set the wake_word_flag if web input arrives
                        # This breaks the prompt_toolkit loop, we check queue, and process.
                        
                        def web_input_watcher():
                            while True:
                                if tq.get_queue_size() > 0:
                                    wake_word_detected_flag.set() # Force exit from input_box
                                    break
                                time.sleep(0.2)
                                if wake_word_detected_flag.is_set(): # Already set by voice
                                    break
                        
                        # Start watcher just for this input cycle
                        watcher = threading.Thread(target=web_input_watcher, daemon=True)
                        watcher.start()
                        
                        user_input = tui.input_box(
                            prompt="Message",
                            placeholder="Type your message... (@ for files, / for commands, L + Enter for voice)",
                            check_for_auto_exit=True,  # Enable auto-exit when sub-agent results are ready
                            wake_word_event=wake_word_detected_flag
                        )
                        
                        # If we broke out due to flag, check if it was Web Input
                        if user_input == "__WAKE_WORD_TRIGGERED__":
                            if tq.get_queue_size() > 0:
                                task = tq.get()
                                agent.load_session_context(task.session_id)
                                
                                # Sync current_session with task session
                                try:
                                    current_session = session_mgr.load(task.session_id)
                                except:
                                    # Create session if not found (fallback)
                                    current_session = session_mgr.new()
                                    current_session.id = task.session_id
                                    session_mgr.save(current_session)
                                    
                                raw_input = task.input_text
                                is_web_input = True
                                
                                # CRITICAL: Sync agent session ID with current session
                                if hasattr(agent, '_session_id') and agent._session_id != current_session.id:
                                    agent._unregister_session()
                                    agent._session_id = current_session.id
                                    agent._register_session()
                                
                                # Handle System Commands (Copy logic from above)
                                if str(raw_input).startswith("__CMD__"):
                                    try:
                                        cmd_parts = str(raw_input).strip().split(":")
                                        if len(cmd_parts) < 2:
                                            continue
                                            
                                        cmd_type = cmd_parts[1].strip()
                                        
                                        if cmd_type == "NEW_SESSION":
                                            current_session = session_mgr.new()
                                            session_mgr.save(current_session)
                                            set_current_session_id(current_session.id)
                                            agent.init_chat()
                                            tui.event("WebUI", "Started new session", style="success")
                                            wake_word_detected_flag.clear()
                                            continue
                                            
                                        elif cmd_type == "LOAD_SESSION":
                                            sid = cmd_parts[2].strip()
                                            try:
                                                current_session = session_mgr.load(sid)
                                                set_current_session_id(current_session.id)
                                                agent.init_chat()
                                                for msg in current_session.messages:
                                                    if msg.get("role") in ["user", "assistant"]:
                                                        agent.history.append({"role": msg.get("role"), "content": msg.get("content")})
                                                tui.event("WebUI", f"Switched to session: {current_session.name}", style="success")
                                            except Exception as e:
                                                tui.error(f"Failed to switch session: {e}")
                                            wake_word_detected_flag.clear()
                                            continue

                                        elif cmd_type == "RENAME_SESSION":
                                            try:
                                                cmd_parts = str(raw_input).split(":", 3)
                                                sid = cmd_parts[2].strip()
                                                new_name = cmd_parts[3].strip()
                                                if current_session and current_session.id == sid:
                                                    current_session.name = new_name
                                            except: pass
                                            wake_word_detected_flag.clear()
                                            continue
                                        
                                        elif cmd_type == "RELOAD_CONFIG":
                                            # Reload agent config and apply provider change (same logic as headless_runner)
                                            try:
                                                tui.event("System", "Config updated from WebUI", style="dim")
                                                from vaf.core.config import Config
                                                new_cfg = Config.load()
                                                if hasattr(agent, "config"):
                                                    agent.config = new_cfg
                                                new_provider = new_cfg.get("provider", "local")
                                                old_provider = getattr(agent, "provider", "local")
                                                if old_provider != new_provider:
                                                    agent.provider = new_provider
                                                    if new_provider != "local":
                                                        try:
                                                            from vaf.core.api_backend import APIBackendManager
                                                            agent.api_backend = APIBackendManager(new_provider)
                                                        except Exception:
                                                            agent.api_backend = None
                                                        agent.use_server = False
                                                        agent.llm = None
                                                    else:
                                                        agent.api_backend = None
                                                    tui.event("System", f"Provider switched to {new_provider.upper()}", style="dim")
                                            except Exception:
                                                pass
                                            wake_word_detected_flag.clear()
                                            continue

                                        else:
                                            # Consume unknown commands silently
                                            tui.warning(f"Ignored unknown command: {cmd_type}")
                                            wake_word_detected_flag.clear()
                                            continue

                                    except Exception as e:
                                        tui.error(f"Command processing error: {e}")
                                        wake_word_detected_flag.clear()
                                        continue
                                
                                user_input = raw_input
                                tui.console.print(f"\n[bold green]Web UI Input:[/bold green] {user_input}")
                                # Clear flag so we don't trigger voice logic
                                wake_word_detected_flag.clear()
                            # Else it was real voice (handled below)
                
                except Exception as e:
                    # Catch TUI errors (like NoConsoleScreenBufferError) and switch to fallback
                    tui.error(f"Console interaction failed: {e}")
                    tui.warning("Switching to standard input mode (no auto-complete/history)")
                    console_broken = True
                    try:
                        user_input = input("Message: ")
                    except EOFError:
                        break
            
            # CRITICAL: Stop TTS immediately upon any user input (Interruption)
            # This allows the user to "barge in" and stop the agent from speaking
            try:
                from vaf.core.speech import get_speech_manager
                get_speech_manager().stop()
            except Exception:
                pass

            # Handle Wake Word Trigger (Auto-Exit from Input Box)
            if user_input == "__WAKE_WORD_TRIGGERED__":
                wake_word_detected_flag.clear()
                
                # CRITICAL: Stop TTS before starting STT
                try:
                    from vaf.core.speech import get_speech_manager
                    sm = get_speech_manager()
                    sm.stop()
                except Exception:
                    pass

                # Start listening UI immediately
                captured = tui.listen_overlay()
                if captured:
                    tui.event("User (Voice)", captured, style="normal")
                    _process_agent_message(agent, captured, tui, current_session)
                
                # Loop back immediately to skip normal input prompt logic
                continue

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
                    # if response:
                    #    tui.message_box(response, title="Answer", role="assistant")
                except Exception as e:
                    tui.error(f"Error processing result: {e}")
                
                # Auto-continue immediately (no countdown needed)
                tui.console.print()
                continue
            
            if user_input is None:
                break
            
            user_input = user_input.strip()
            
            # If user pressed empty Enter, treat it as "check for more results"
            # This allows the loop to quickly re-check instead of waiting for manual input
            if not user_input:
                # Empty input - just loop back to check for results
                continue
            
            # Context Isolation for TUI Input
            # If input was not fetched from TaskQueue (is_web_input=False), it came from TUI manually.
            # Ensure we are in the correct session context.
            if not is_web_input:
                # Force TUI to use its own dedicated session ID
                target_session_id = "cli-main" 
                agent.load_session_context(target_session_id)
                # Note: 'current_session' variable might be stale from Web Task, but Agent context is now correct.
                # Ideally we should reload current_session object too, but Agent history is the critical part.
                try:
                    current_session = session_mgr.load(target_session_id)
                except: pass
            
            # ═══════════════════════════════════════════════════════════════
            # KEYBOARD SHORTCUTS (single letters)
            # ═══════════════════════════════════════════════════════════════
            
            if user_input.lower() in ("s", "settings"):
                from vaf.cli.cmd import settings
                reload_needed = settings.main_menu(agent=agent)
                
                # CRITICAL FIX: Restore original stdout handles
                # Libraries like 'inquirer' (via colorama) wrap stdout, confusing prompt_toolkit.
                # We must reset to the raw stream to ensure Win32 APIs work.
                if hasattr(sys, '__stdout__'):
                    sys.stdout = sys.__stdout__
                
                tui.clear()
                tui.logo_minimal()
                
                if reload_needed:
                    tui.event("System", "Applying changes, reloading agent...", style="warning")
                    agent.shutdown()
                    agent = _make_cli_agent(verbose=verbose)
                    global_agent = agent
                    agent.load_model()
                    agent.init_chat()
                else:
                    tui.event("System", "Settings updated", style="dim")
                continue
            
            if user_input.lower() in ("c", "model"):
                from vaf.cli.cmd import settings
                settings.select_model_menu()
                
                # Re-initialize TUI to fix console state after inquirer usage
                tui = TUI(theme)
                
                tui.clear()
                tui.logo_minimal()
                
                tui.event("System", "Applying changes, reloading agent...", style="warning")
                agent.shutdown()
                agent = _make_cli_agent(verbose=verbose)
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
  L  - Voice Input (STT)
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
                    agent = _make_cli_agent(verbose=verbose)
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
                    usage_bar = "●" * int(status['usage_percent'] * 10) + "○" * (10 - int(status['usage_percent'] * 10))
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
                    wake_word_detected_flag.clear()

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
                
                elif cmd in ("restart", "reload", "r"):
                    tui.event("System", "Restarting VAF...", style="warning")

                    # Kill Web UI process before restart (Critically important!)
                    if npm_process:
                        try:
                            npm_process.terminate()
                            try:
                                npm_process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                npm_process.kill()
                        except:
                            pass
                        
                    # Restart Process
                    time.sleep(0.5)
                    
                    # Robust restart logic
                    try:
                        # If running as executable (vaf.exe)
                        if sys.argv[0].endswith('.exe'):
                            os.execl(sys.argv[0], sys.argv[0], *sys.argv[1:])
                        else:
                            # If running as script (python vaf ...)
                            # Check if run as module (-m vaf)
                            if sys.argv[0].endswith('__main__.py'):
                                # python -m vaf run ...
                                args = [sys.executable, "-m", "vaf"] + sys.argv[1:]
                                os.execl(sys.executable, *args)
                            else:
                                # python vaf/main.py run ...
                                args = [sys.executable] + sys.argv
                                os.execl(sys.executable, *args)
                    except Exception as e:
                        tui.error(f"Restart failed: {e}")
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
            # Save traceback to log for debugging (crash_YYYY-MM-DD.log)
            try:
                crash_path = get_dated_log_path("crash", "log")
                crash_path.parent.mkdir(parents=True, exist_ok=True)
                with open(crash_path, "a", encoding="utf-8") as f:
                    f.write(f"\n--- {datetime.now().isoformat()} ---\n")
                    f.write(traceback.format_exc())
                tui.info(f"Traceback saved to {crash_path}")
            except Exception:
                # If logging fails, just print traceback
                traceback.print_exc()

    # Cleanup
    # Kill Web UI process if running
    if npm_process:
        try:
            # tui.event("System", "Stopping Web Dashboard...", style="dim")
            npm_process.terminate()
            try:
                npm_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                npm_process.kill()
        except:
            pass

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
    
    # Get session ID for scoped web interface updates
    session_id = session.id if session else None
    
    # Update Web Interface (with session scope)
    web_iface = get_web_interface()
    web_iface.update_status("thinking", session_id=session_id)
    web_iface.log(f"User: {user_input[:50]}...", level="info", source="user", session_id=session_id)
    
    # Delayed import to avoid circular dependencies
    try:
        from vaf.tools.coder import CodingAgentTool
    except ImportError as e:
        CodingAgentTool = None
    
    # State for styling <think> blocks
    think_state = {"active": False}
    # State for filtering "Resposta" artifact at start
    start_filter = {"buffer": "", "done": False}
    
    import re
    def convert_rich_to_xml(text):
        if not text: return ""
        # 1. Convert Rich "dim" styling to XML <think> tags
        text = re.sub(r'\[dim\]', '<think>', text, flags=re.IGNORECASE)
        text = re.sub(r'\[white dim\]', '<think>', text, flags=re.IGNORECASE)
        text = re.sub(r'\[.*?dim.*?\]', '<think>', text, flags=re.IGNORECASE)

        # Replace end tags
        text = text.replace('[/dim]', '</think>')
        if '<think>' in text and '</think>' not in text:
             text = text.replace('[/]', '</think>')

        # Strip remaining Rich tags
        text = re.sub(r'\[\/?[^\]]+\]', '', text)

        # 2. Auto-detect thinking when model doesn't use <think> tags
        # Look for English reasoning followed by German answer (VQ-1 pattern)
        if '<think>' not in text and len(text) > 100:
            # Common German sentence starters that indicate the actual answer
            german_starters = r'(Ich bin|Ich kann|Ich werde|Ich habe|Hallo|Guten|Gerne|Natürlich|Ja,|Nein,|Das ist|Die |Der |Ein |Eine |Hier ist|Hier sind|Klar|Sicher|Selbstverständlich)'

            # Find where German answer starts (after English reasoning)
            match = re.search(german_starters, text)
            if match and match.start() > 50:
                # Check if text before looks like English reasoning
                before = text[:match.start()]
                # English reasoning indicators
                if re.search(r'\b(I should|I need|I will|I\'ll|Let me|The user|should be|because|since|First|Also)\b', before, re.IGNORECASE):
                    reasoning = before.strip()
                    answer = text[match.start():].strip()
                    text = f"<think>{reasoning}</think>\n\n{answer}"

        return text

    def stream_callback(text):
        nonlocal response_parts
        response_parts.append(text)

        # Construct full text so far
        current_full_text = "".join(response_parts)

        # Convert Terminal-Styling to Web-Styling (XML)
        clean_text = convert_rich_to_xml(current_full_text)

        # UI FIX: Strip leading whitespace strings to prevent large gaps/bubbles
        # This handles "\n\nText" AND "<think>...</think>\n\nText"
        # Regex explanation:
        # ^(\s*<think>[\s\S]*?</think>)? : Group 1 (Optional) matches <think> block (and prec space)
        # \s+                            : Matches the whitespace FOLLOWING it (or start)
        def _cleanup_ws(m): return m.group(1) or ""
        clean_text = re.sub(r'^(\s*<think>[\s\S]*?</think>)?\s+', _cleanup_ws, clean_text)

        # DEBUG: Log to file what's being sent to WebUI (stream_debug_YYYY-MM-DD.txt)
        try:
            stream_log = get_dated_log_path("stream_debug", "txt")
            stream_log.parent.mkdir(parents=True, exist_ok=True)
            with open(stream_log, "a", encoding="utf-8") as f:
                f.write(f"[CHUNK] text={repr(text[:30] if len(text)>30 else text)} | clean={repr(clean_text[:50] if len(clean_text)>50 else clean_text)} | will_emit={bool(clean_text and clean_text.strip())}\n")
        except Exception:
            pass

        # Force update to ensure frontend knows we are answering
        # Only emit if there is meaningful content (prevent empty bubbles)
        if clean_text and clean_text.strip():
            web_iface.emit_agent_message("assistant", clean_text, session_id=session.id)
        
        # CRITICAL: Suppress Main Agent output if Coder TUI is active!
        # This prevents "leaking stdout" that breaks the TUI layout
        if CodingAgentTool and CodingAgentTool._active_instance is not None:
            return
            
        remaining_text = text
        
        # START FILTER: Handle "Resposta" artifact at the very beginning
        # The model sometimes outputs "Resposta" as the first word. User wants it gray/hidden.
        if not start_filter["done"]:
            start_filter["buffer"] += text
            buf = start_filter["buffer"]
            target = "resposta" 
            
            # Use a slightly loose check (ignore case)
            if len(buf) >= len(target):
                if buf[:len(target)].lower() == target:
                     # Match! Print "Resposta" part as DIM
                     tui.console.print(buf[:len(target)], end="", markup=True, style="dim")
                     # Process rest normally
                     remaining_text = buf[len(target):]
                     start_filter["done"] = True
                else:
                     # Mismatch (e.g. "Hello") - flush buffer
                     remaining_text = buf
                     start_filter["done"] = True
            elif target.startswith(buf.strip().lower()) and len(buf) < 20: 
                # Prefix match (e.g. "Res"), wait for more. Limit length to avoid infinite buffering.
                return
            else:
                # Mismatch (e.g. "W") - flush
                remaining_text = buf
                start_filter["done"] = True
        
        # Parse <think> tags for styling
        current_text = remaining_text
        while current_text:
            if not think_state["active"]:
                # Normal mode: search for start of thinking
                idx = current_text.find("<think>")
                if idx != -1:
                    # Found start tag - print prior part normally
                    if idx > 0:
                        tui.console.print(current_text[:idx], end="", markup=True, style=f"bold {tui.primary}")
                    
                    # Switch to thinking mode (print tag HIDDEN)
                    # tui.console.print("<think>", end="", markup=True, style="dim")
                    think_state["active"] = True
                    current_text = current_text[idx+7:] # +7 len of <think>
                else:
                    # No tag, print all normally
                    tui.console.print(current_text, end="", markup=True, style=f"bold {tui.primary}")
                    current_text = ""
            else:
                # Thinking mode: search for end of thinking
                idx = current_text.find("</think>")
                if idx != -1:
                    # Found end tag - print prior part dim
                    tui.console.print(current_text[:idx], end="", markup=True, style="dim")
                    
                    # Print end tag also HIDDEN
                    # tui.console.print("</think>", end="", markup=True, style="dim")
                    
                    # Switch back to normal mode
                    think_state["active"] = False
                    current_text = current_text[idx+8:] # +8 len of </think>
                else:
                    # No end tag, print all dim
                    tui.console.print(current_text, end="", markup=True, style="dim")
                    current_text = ""
    
    try:
        with tui.spinner("Thinking..."):
            pass  # Just show spinner briefly
        
        result_text = agent.chat_step(user_input, stream_callback=stream_callback)
        
        # Recognize special async acknowledgment marker
        is_async_ack = False
        if result_text and str(result_text).startswith("[ASYNC_ACK]"):
            is_async_ack = True
            result_text = str(result_text).replace("[ASYNC_ACK]", "")
            
            # FIX: Ensure Web UI gets this update (since it wasn't streamed)
            # The async ack bypasses the normal stream loop in chat_step
            try:
                # We need to construct a clean XML version (handle Rich tags if any)
                clean_ack = convert_rich_to_xml(str(result_text))
                web_iface.emit_agent_message("assistant", clean_ack, session_id=session.id)
            except: pass
        
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
                # Fallback print DISABLED to prevent duplicate output.
                # The agent.py streaming loop already handles printing to stdout (white).
                # This fallback was causing double printing (Blue) if response_parts logic failed.
                pass 
                # response_parts = [str(result_text)]
                # tui.console.print(str(result_text), markup=True, style=f"bold {tui.primary}")
        tui.newline()
        
        # Save response to session (USE CLEAN TEXT!)
        full_response_raw = "".join(response_parts)
        # Use the same converter to ensure consistency
        full_response_clean = convert_rich_to_xml(full_response_raw)
        
        session.add_message("assistant", full_response_clean)
        
        # AUTO-SAVE SESSION (Critical for Web UI persistence)
        # We assume session object handles its own persistence path if it was loaded/created properly.
        # But session object doesn't know the manager. We need to save via manager or session method.
        # The session object is a simple data class usually.
        # We need to access the session manager to save.
        
        try:
            from vaf.core.session import SessionManager
            # Simple instantiation is cheap
            mgr = SessionManager()
            mgr.save(session)
            
            # Notify Web UI about session list update (so the timestamp/preview updates)
            # This keeps the sidebar fresh
            try:
                web_iface = get_web_interface()
                sessions_list = mgr.list(limit=20)
                web_iface.push_update({
                    "type": "session_list", 
                    "sessions": [{"id": s["id"], "title": s["name"], "date": s["updated_at"]} for s in sessions_list]
                })
            except:
                pass
                
        except Exception:
            pass # Don't crash chat if save fails
        
        # Update Web Interface (with session scope)
        web_iface.log("Response complete", level="info", source="system", session_id=session_id)
        web_iface.update_status("idle", session_id=session_id)
        # Clears Web UI "generating" / stop button (frontend listens for message_complete).
        try:
            web_iface.emit_message_complete(full_response_clean or "", session_id=session_id)
        except Exception:
            pass

        # Emit Token Stats
        try:
            used, total = agent.get_token_usage()
            stats = {
                "used": used,
                "total": total,
                "percent": (used / total) if total else 0.0,
                "api": bool(getattr(agent, 'api_backend', False))
            }
            web_iface.emit_stats(stats, session_id=session_id)
        except Exception:
            pass # Ignore stats errors
        
    except Exception as e:
        tui.error(f"Agent error: {e}")
        get_web_interface().log(f"Agent error: {e}", level="error", session_id=session_id)
        get_web_interface().update_status("idle", session_id=session_id)
        try:
            get_web_interface().emit_message_complete("", session_id=session_id)
        except Exception:
            pass


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
        agent = _make_cli_agent(verbose=verbose)
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
            # Show token usage (or message count for API)
            used, total = agent.get_token_usage()
            if agent.api_backend:
                # For API: show as In[X] Out[Y]
                UI.console.print(f"[dim]                      Tokens: In: {used:,} | Out: {total:,}[/dim]", justify="right")
            else:
                # For local: show as token progress bar
                UI.print_usage_bar(used, total)
                
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
                agent = _make_cli_agent(verbose=verbose) 
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
                agent = _make_cli_agent(verbose=verbose)  
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
                    # Show Usage Bar (or message count for API)
                    used, total = agent.get_token_usage()
                    if agent.api_backend:
                        UI.console.print(f"[dim]                      Tokens: In: {used:,} | Out: {total:,}[/dim]", justify="right")
                        UI.print()
                    else:
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
                agent = _make_cli_agent(verbose=verbose)
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
        
        # Show Usage Bar (or message count for API)
        used, total = agent.get_token_usage()
        
        # Calculate stats for Web UI
        stats = {
            "used": used,
            "total": total,
            "percent": round((used / total) * 100, 1) if total > 0 else 0.0,
            "api": agent.api_backend
        }
        
        if agent.api_backend:
            # For API: show as In[X] Out[Y]
            UI.console.print(f"[dim]                      Tokens: In: {used:,} | Out: {total:,}[/dim]", justify="right")
            UI.print()
        else:
            # For local: show as token progress bar
            UI.print_usage_bar(used, total)
        # Broadcast to Web UI
        try:
            get_web_interface().emit_stats(stats)
        except Exception:
            pass

def output_stream(text):
    UI.console.print(text, end="", markup=True, style="bold cyan")