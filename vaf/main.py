import sys
import subprocess
import importlib.util
import os

def bootstrap():
    """Checks for ALL dependencies and auto-installs if confirmed."""
    # Map: pip package name -> Python import name
    # Some packages have different names when importing
    DEPENDENCIES = {
        # Core CLI & UI
        "typer": "typer",
        "rich": "rich",
        "prompt_toolkit": "prompt_toolkit",
        "colorama": "colorama",
        "shellingham": "shellingham",
        # Networking & Web
        "requests": "requests",
        "beautifulsoup4": "bs4",           # pip name != import name
        "html2text": "html2text",
        # AI/ML
        "huggingface_hub": "huggingface_hub",
        # Search
        "ddgs": "ddgs",
        # Automation
        "schedule": "schedule",
        "inquirer": "inquirer",
    }
    
    missing = []
    for pip_name, import_name in DEPENDENCIES.items():
        if importlib.util.find_spec(import_name) is None:
            missing.append(pip_name)
    
    if not missing:
        return  # All good!
    
    # Find requirements.txt (could be in project root or vaf folder)
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    req_file = os.path.join(base_dir, "requirements.txt")
    if not os.path.exists(req_file):
        req_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
    
    print(f"\n{'='*60}")
    print("  VAF - Dependency Check")
    print(f"{'='*60}")
    print(f"\n  Missing packages ({len(missing)}):")
    for pkg in missing:
        print(f"    • {pkg}")
    print()
    
    response = input("  Auto-install via pip? [Y/n]: ").strip().lower()
    
    if response in ('', 'y', 'yes', 'j', 'ja'):
        print("\n  Installing dependencies...")
        try:
            if os.path.exists(req_file):
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", "-r", req_file, "-q"],
                    stdout=subprocess.DEVNULL
                )
            else:
                # Fallback: install missing packages directly
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install"] + missing + ["-q"],
                    stdout=subprocess.DEVNULL
                )
            print("  ✓ Installation complete!\n")
            importlib.invalidate_caches()
        except Exception as e:
            print(f"  ✗ Install failed: {e}")
            print(f"  Run manually: pip install -r requirements.txt")
            sys.exit(1)
    else:
        print("\n  Please install manually:")
        print(f"    pip install -r requirements.txt")
        print("  or:")
        print(f"    pip install {' '.join(missing)}")
        sys.exit(1)

bootstrap()

import typer
from vaf.cli.cmd import run, models, info, scaffold, generate, automate, debug, git
from vaf.core.session import session_app
from vaf.core.snapshot import snapshot_app
from vaf.core.automation import automation_app

app = typer.Typer(
    name="vaf",
    help="VAF - Veyllo Agentic Framework: Local AI tool for developers",
    no_args_is_help=True
)

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

# Start agent (interactive chat)
app.add_typer(run.app, name="run", help="Start the agent")

# Model management
app.add_typer(models.app, name="models", help="Manage models")

# Project templates
app.add_typer(scaffold.app, name="scaffold", help="Create project templates")

# Code generation
app.add_typer(generate.app, name="generate", help="Generate code with AI")

# Automation
app.add_typer(automate.app, name="automate", help="Tests, builds, linting")

# Debugging
app.add_typer(debug.app, name="debug", help="AI error analysis")

# Git integration
app.add_typer(git.app, name="git", help="Git with AI support")

# Session management
app.add_typer(session_app, name="session", help="Manage conversations")

# Snapshot/Undo
app.add_typer(snapshot_app, name="snapshot", help="Code snapshots and undo")

# Scheduled Automations
app.add_typer(automation_app, name="automation", help="Time-based task automation")

# ═══════════════════════════════════════════════════════════════════════════════
# STANDALONE COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

app.command(name="info")(info.info)
app.command(name="install-gpu")(info.install_gpu)

# ═══════════════════════════════════════════════════════════════════════════════
# THEME COMMAND
# ═══════════════════════════════════════════════════════════════════════════════

@app.command(name="theme")
def theme_command(
    name: str = typer.Argument(None, help="Theme name to set"),
    list_themes: bool = typer.Option(False, "--list", "-l", help="List available themes")
):
    """
    Manage terminal themes.
    
    Examples:
        vaf theme --list        # List all themes
        vaf theme dracula       # Set Dracula theme
        vaf theme nord          # Set Nord theme
    """
    from vaf.cli.themes import ThemeManager
    from vaf.cli.tui import TUI
    from rich.console import Console
    from rich.table import Table
    
    console = Console()
    
    if list_themes or not name:
        themes = ThemeManager.list_themes()
        current = ThemeManager.current()
        
        table = Table(title="Available Themes", show_header=True)
        table.add_column("Name", style="cyan")
        table.add_column("Description")
        table.add_column("Current")
        
        for theme_name in themes:
            theme_data = ThemeManager.get_theme(theme_name)
            is_current = "✓" if theme_name == current else ""
            table.add_row(
                theme_name,
                theme_data.get("name", theme_name.title()),
                is_current
            )
        
        console.print(table)
        console.print(f"\nUsage: [cyan]vaf theme <name>[/cyan]")
        
    else:
        if ThemeManager.set_theme(name):
            # Show preview
            tui = TUI(name)
            tui.logo_minimal()
            console.print(f"[green]✓ Theme set to: {name}[/green]")
            console.print()
            
            # Preview colors
            theme = ThemeManager.get_theme(name)
            console.print("[bold]Theme Preview:[/bold]")
            console.print(f"  [{theme['primary']}]Primary[/{theme['primary']}] | " +
                         f"[{theme['secondary']}]Secondary[/{theme['secondary']}] | " +
                         f"[{theme['accent']}]Accent[/{theme['accent']}]")
            console.print(f"  [{theme['success']}]Success[/{theme['success']}] | " +
                         f"[{theme['warning']}]Warning[/{theme['warning']}] | " +
                         f"[{theme['error']}]Error[/{theme['error']}]")
        else:
            console.print(f"[red]✗ Theme not found: {name}[/red]")
            console.print(f"Use [cyan]vaf theme --list[/cyan] to see available themes.")

# ═══════════════════════════════════════════════════════════════════════════════
# CHAT COMMAND (MODERN TUI)
# ═══════════════════════════════════════════════════════════════════════════════

@app.command(name="chat")
def chat_command(
    theme: str = typer.Option("vaf", "--theme", "-t", help="UI theme"),
    model: str = typer.Option(None, "--model", "-m", help="Model to use"),
    session: str = typer.Option(None, "--session", "-s", help="Load existing session")
):
    """
    Start interactive chat with modern UI.
    
    Examples:
        vaf chat                  # Start new chat
        vaf chat --theme dracula  # Use Dracula theme
        vaf chat -s abc123        # Resume session
    """
    from vaf.cli.tui import TUI
    from vaf.core.session import SessionManager, Session
    from vaf.cli.themes import ThemeManager
    
    # Set theme
    ThemeManager.set_theme(theme)
    tui = TUI(theme)
    
    # Session management
    manager = SessionManager()
    
    if session:
        try:
            current_session = manager.load(session)
            tui.info(f"Loaded session: {current_session.name}")
        except FileNotFoundError:
            tui.warning(f"Session not found: {session}")
            current_session = manager.new(model=model or "")
    else:
        current_session = manager.new(model=model or "")
    
    # Welcome
    tui.clear()
    tui.logo()
    tui.rule("Interactive Chat")
    tui.newline()
    
    # Shortcuts info
    tui.print(f"[{tui.muted}]Commands: /help, /exit, /clear, /theme, /session, /undo[/{tui.muted}]")
    tui.print(f"[{tui.muted}]Press Tab for completion, @ for files[/{tui.muted}]")
    tui.newline()
    
    # Chat loop
    while True:
        try:
            user_input = tui.input_box(
                prompt="Message",
                placeholder="Type your message... (@ for files, / for commands)"
            )
            
            if user_input is None:
                # Ctrl+C or Escape
                break
            
            user_input = user_input.strip()
            
            if not user_input:
                continue
            
            # Handle commands
            if user_input.startswith("/"):
                cmd = user_input[1:].lower().split()[0]
                args = user_input[1:].split()[1:] if len(user_input.split()) > 1 else []
                
                if cmd in ("exit", "quit", "q"):
                    # Save session
                    if current_session.messages:
                        manager.save(current_session)
                        tui.info(f"Session saved: {current_session.id}")
                    break
                    
                elif cmd == "clear":
                    tui.clear()
                    tui.logo_minimal()
                    continue
                    
                elif cmd == "help":
                    tui.panel("""
**Available Commands:**

/exit, /quit    - Exit chat (saves session)
/clear          - Clear screen
/help           - Show this help
/theme <name>   - Change theme
/session list   - List saved sessions
/session save   - Save current session
/undo           - Undo last change
/model <name>   - Switch model
/export <file>  - Export conversation
                    """, title="Help", style="info")
                    continue
                    
                elif cmd == "theme":
                    if args:
                        new_theme = args[0]
                        if ThemeManager.set_theme(new_theme):
                            tui = TUI(new_theme)
                            tui.success(f"Theme changed to: {new_theme}")
                        else:
                            tui.error(f"Theme not found: {new_theme}")
                    else:
                        themes = ", ".join(ThemeManager.list_themes())
                        tui.info(f"Available themes: {themes}")
                    continue
                    
                elif cmd == "session":
                    if args and args[0] == "save":
                        path = manager.save(current_session)
                        tui.success(f"Session saved: {path}")
                    elif args and args[0] == "list":
                        sessions = manager.list(limit=10)
                        if sessions:
                            tui.list_items(
                                [f"{s['id']} - {s['name']} ({s['message_count']} msgs)" for s in sessions],
                                title="Recent Sessions",
                                numbered=True
                            )
                        else:
                            tui.info("No saved sessions")
                    else:
                        tui.info(f"Current session: {current_session.id}")
                    continue
                    
                elif cmd == "undo":
                    from vaf.core.snapshot import Snapshot
                    snap = Snapshot()
                    if snap.undo():
                        tui.success("Undone to last snapshot")
                    else:
                        tui.warning("No snapshot to undo")
                    continue
                    
                elif cmd == "export":
                    if args:
                        filepath = args[0]
                        content = manager.export(current_session, format="markdown")
                        with open(filepath, 'w', encoding='utf-8') as f:
                            f.write(content)
                        tui.success(f"Exported to: {filepath}")
                    else:
                        tui.warning("Usage: /export <filename>")
                    continue
                    
                else:
                    tui.warning(f"Unknown command: /{cmd}")
                    continue
            
            # Add user message to session
            current_session.add_message("user", user_input)
            
            # Display user message
            tui.message_box(user_input, role="user")
            tui.newline()
            
            # TODO: Here you would call the actual LLM
            # For now, show a placeholder response
            with tui.spinner("Thinking..."):
                import time
                time.sleep(0.5)  # Simulated delay
            
            # Placeholder response
            response = f"I received your message: '{user_input[:50]}...'\n\nThis is a placeholder response. The actual LLM integration happens in `vaf run`."
            
            current_session.add_message("assistant", response)
            tui.message_box(response, role="assistant")
            tui.newline()
            
        except KeyboardInterrupt:
            tui.newline()
            if tui.confirm("Exit chat?"):
                if current_session.messages:
                    manager.save(current_session)
                    tui.info(f"Session saved: {current_session.id}")
                break
    
    tui.print(f"\n[{tui.muted}]Goodbye![/{tui.muted}]")

# ═══════════════════════════════════════════════════════════════════════════════
# PROJECT CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

@app.command(name="init")
def init_project(
    language: str = typer.Option("auto", "--lang", "-l", help="Project language"),
    path: str = typer.Option(".", "--path", "-p", help="Project directory")
):
    """
    Initialize a vaf.config.json in the project.
    
    Examples:
        vaf init
        vaf init --lang python
    """
    from vaf.cli.ui import UI
    from vaf.core.project_config import ProjectConfig
    
    config_path = ProjectConfig.init(path, language)
    
    UI.success(f"Configuration created: {config_path}")
    UI.print()
    UI.print("[bold]Contents:[/bold]")
    
    config = ProjectConfig.load(path)
    for key, value in config.items():
        if not key.startswith("_"):
            UI.print(f"  [cyan]{key}[/cyan]: {value}")

# ═══════════════════════════════════════════════════════════════════════════════
# QUICK START ALIASES
# ═══════════════════════════════════════════════════════════════════════════════

@app.callback(invoke_without_command=True)
def callback(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-v", help="Show version")
):
    """VAF - Veyllo Agentic Framework"""
    if version:
        from vaf.cli.ui import UI
        UI.print("[cyan bold]VAF[/cyan bold] - Veyllo Agentic Framework")
        UI.print("Version: [green]0.2.0[/green]")
        UI.print("https://github.com/Veyllo-Labs/Veyllo-App")
        raise typer.Exit()

if __name__ == "__main__":
    app()
