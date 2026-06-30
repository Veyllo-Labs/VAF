# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import sys
import subprocess
import importlib.util
import os

# CRITICAL: Patch stdout/stderr/stdin for pythonw (no console) - must be before any print/input
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")
if sys.stdin is None:
    sys.stdin = open(os.devnull, "r")

# Windows consoles default to a legacy code page (cp1252) that raises
# UnicodeEncodeError in the logging StreamHandler on non-ASCII output (e.g.
# arrows in log messages). Force UTF-8 on the std streams so logging and print
# never crash regardless of the active console code page.
for _vaf_stream in (sys.stdout, sys.stderr):
    try:
        _vaf_stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        pass

def bootstrap():
    """Checks for ALL dependencies and auto-installs if confirmed."""
    # Safety hatch for App Bundles / CI
    if os.environ.get("VAF_SKIP_DEP_CHECK"):
        return

    # Map: pip package name -> Python import name
    # Some packages have different names when importing
    DEPENDENCIES = {
        # Core CLI & UI
        "typer": "typer",
        "rich": "rich",
        "prompt_toolkit": "prompt_toolkit",
        "colorama": "colorama",
        "shellingham": "shellingham",
        "psutil": "psutil",
        # Networking & Web
        "requests": "requests",
        "beautifulsoup4": "bs4",           # pip name != import name
        "html2text": "html2text",
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "websockets": "websockets",
        "pydantic": "pydantic",
        "discord.py": "discord",
        # AI/ML
        "huggingface_hub": "huggingface_hub",
        "tqdm": "tqdm",  # Progress bars for downloads
        # Automation
        "schedule": "schedule",
        "inquirer": "inquirer",
        # Speech (TTS/STT/Wake Word)
        "SpeechRecognition": "speech_recognition",
        "pyaudio": "pyaudio",
        # "openwakeword": "openwakeword", # Optional/Unstable on Mac
        # "onnxruntime": "onnxruntime",   # Optional/Unstable on Mac
        # Document Processing (PDF, Word, Excel, PowerPoint)
        "PyPDF2": "PyPDF2",
        "python-docx": "docx",             # pip name != import name
        "openpyxl": "openpyxl",
        "python-pptx": "pptx",             # pip name != import name
        # Code Quality & Linting
        "ruff": "ruff",
        # System Tray
        "pystray": "pystray",
        "pillow": "PIL",
    }
    
    # NOTE: do NOT special-case macOS to require rumps. rumps was removed (it conflicts with
    # pywebview for macOS main-thread ownership); the tray uses pystray on every platform, and
    # pystray + pyobjc-framework-Cocoa are in requirements.txt for darwin. Demanding rumps here
    # caused an infinite "missing rumps" prompt, since `pip install -r requirements.txt` never
    # installs it. The platform-agnostic check above already covers pystray.
    
    missing = []
    for pip_name, import_name in DEPENDENCIES.items():
        if importlib.util.find_spec(import_name) is None:
            missing.append(pip_name)
    
    # Check for optional extras (like hf_xet for huggingface_hub)
    optional_extras = []
    try:
        # Check if hf_xet is available (optional extra for huggingface_hub)
        import huggingface_hub
        try:
            import hf_xet
        except ImportError:
            # hf_xet is not installed, but huggingface_hub is
            # Add it to optional extras to install
            optional_extras.append("huggingface_hub[hf_xet]")
    except ImportError:
        pass  # huggingface_hub itself is missing, will be handled above
    
    if not missing and not optional_extras:
        return  # All good!
    
    # Find requirements.txt (could be in project root or vaf folder)
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    req_file = os.path.join(base_dir, "requirements.txt")
    if not os.path.exists(req_file):
        req_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
    
    print(f"\n{'='*60}")
    print("  VAF - Dependency Check")
    print(f"{'='*60}")
    
    if missing:
        print(f"\n  Missing packages ({len(missing)}):")
        for pkg in missing:
            print(f"    • {pkg}")
    
    if optional_extras:
        print(f"\n  Optional extras (recommended, {len(optional_extras)}):")
        for extra in optional_extras:
            print(f"    • {extra} (improves performance)")
    
    if not missing and not optional_extras:
        return  # All good!
    
    print()

    # Non-interactive environments (CI, piped stdin, cron, systemd) cannot answer the prompt.
    # Do not block or crash on input(); skip the auto-install and continue so meta-commands like
    # `vaf --version` / `vaf --help` still work. Users in a real terminal get the prompt as before.
    try:
        _interactive = sys.stdin.isatty()
    except Exception:
        _interactive = False
    if not _interactive:
        print("  Non-interactive environment — skipping auto-install.")
        print("  To install manually: pip install -r requirements.txt")
        return
    try:
        response = input("  Auto-install via pip? [Y/n]: ").strip().lower()
    except EOFError:
        print("\n  No input available — skipping auto-install.")
        return

    if response in ('', 'y', 'yes', 'j', 'ja'):
        print("\n  Installing dependencies...")
        try:
            if os.path.exists(req_file):
                # Install from requirements.txt (includes all packages and extras)
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", "-r", req_file, "-q"],
                    stdout=subprocess.DEVNULL
                )
            else:
                # Fallback: install missing packages directly
                packages_to_install = missing + optional_extras
                if packages_to_install:
                    cmd = [sys.executable, "-m", "pip", "install"] + packages_to_install
                    if sys.platform == "darwin":
                        cmd.append("--break-system-packages")
                    
                    subprocess.check_call(cmd)
            print("  ✓ Installation complete!\n")
            importlib.invalidate_caches()
        except subprocess.CalledProcessError as e:
            # Check for externally managed environment error in specific valid cases
            # (Note: stdout is devnull, so we blindly guess or need to capture it. 
            #  Given we are in interactive TUI, capturing stderr is tricky impacting UI)
            print(f"  ✗ Install failed (Exit Code: {e.returncode})")
            
            # Suggest --break-system-packages for macOS users
            if sys.platform == "darwin" and "externally-managed-environment" in str(e): # Popen/run would be needed to capture this
                 pass # We can't see the output with check_call(..., stdout=DEVNULL)
            
            print(f"  Run manually: pip install -r requirements.txt")
            if sys.platform == "darwin":
                 print("  Note: On macOS, you may need: pip install --break-system-packages -r requirements.txt")
            sys.exit(1)
        except Exception as e:
            print(f"  ✗ Install failed: {e}")
            sys.exit(1)
    else:
        print("\n  Please install manually:")
        print(f"    pip install -r requirements.txt")
        print("  or:")
        print(f"    pip install {' '.join(missing)}")
        sys.exit(1)

    # Check for Piper TTS if enabled
    try:
        from vaf.core.config import Config
        
        # Language & Speech Onboarding
        if not Config.get("speech_language"):
            print(f"\n{'='*60}")
            print("  VAF - Speech Setup")
            print(f"{'='*60}\n")
            
            try:
                import inquirer
                questions = [
                    inquirer.List('lang',
                                  message="Select your primary language (Speech Input/Output)",
                                  choices=[
                                      ('English (US)', 'en-US'),
                                      ('German (DE)', 'de-DE'),
                                      ('Turkish (TR)', 'tr-TR'),
                                      ('French (FR)', 'fr-FR'),
                                      ('Spanish (ES)', 'es-ES'),
                                      ('Chinese (CN)', 'zh-CN'),
                                      ('Italian (IT)', 'it-IT'),
                                      ('Portuguese (PT)', 'pt-PT'),
                                      ('Russian (RU)', 'ru-RU'),
                                      ('Arabic (AR)', 'ar-JO'),
                                      ('Catalan (CA)', 'ca-ES'),
                                      ('Czech (CS)', 'cs-CZ'),
                                      ('Welsh (CY)', 'cy-GB'),
                                      ('Danish (DA)', 'da-DK'),
                                      ('Greek (EL)', 'el-GR'),
                                      ('Persian (FA)', 'fa-IR'),
                                      ('Finnish (FI)', 'fi-FI'),
                                      ('Hungarian (HU)', 'hu-HU'),
                                      ('Icelandic (IS)', 'is-IS'),
                                      ('Georgian (KA)', 'ka-GE'),
                                      ('Kazakh (KK)', 'kk-KZ'),
                                      ('Luxembourgish (LB)', 'lb-LU'),
                                      ('Latvian (LV)', 'lv-LV'),
                                      ('Nepali (NE)', 'ne-NP'),
                                      ('Dutch (NL)', 'nl-NL'),
                                      ('Norwegian (NO)', 'no-NO'),
                                      ('Polish (PL)', 'pl-PL'),
                                      ('Romanian (RO)', 'ro-RO'),
                                      ('Slovak (SK)', 'sk-SK'),
                                      ('Slovenian (SL)', 'sl-SI'),
                                      ('Serbian (SR)', 'sr-RS'),
                                      ('Swedish (SV)', 'sv-SE'),
                                      ('Swahili (SW)', 'sw-CD'),
                                      ('Ukrainian (UK)', 'uk-UA'),
                                      ('Vietnamese (VI)', 'vi-VN'),
                                  ]
                    ),
                    inquirer.Confirm('enable_speech', message="Enable Speech Features (TTS/STT) now?", default=True)
                ]
                answers = inquirer.prompt(questions)
                if answers:
                    Config.set("speech_language", answers['lang'])
                    if answers['enable_speech']:
                        Config.set("speech_tts_enabled", True)
                        Config.set("speech_stt_enabled", True)
                        print("  ✓ Speech features enabled.\n")
                    else:
                        print("  ✓ Language saved (Speech disabled for now).\n")
            except Exception:
                pass # Skip if inquirer fails

        if Config.get("speech_tts_enabled", False):
            from vaf.core.speech import get_speech_manager
            # This will trigger auto-install of Piper and default voice if missing
            get_speech_manager()._check_piper()
            # Ensure at least German voice is ready
            get_speech_manager()._ensure_voice_model("de")
    except:
        pass

bootstrap()

import typer
from vaf.cli.cmd import run, models, info, scaffold, generate, automate, debug, git, subagent, workflow, bridge, server, security, service, ww, update
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
# Self-update (check for / apply new VAF releases)
app.add_typer(update.app, name="update", help="Check for and apply VAF updates")

# Session management
app.add_typer(session_app, name="session", help="Manage conversations")

# Snapshot/Undo
app.add_typer(snapshot_app, name="snapshot", help="Code snapshots and undo")

# Scheduled Automations
app.add_typer(automation_app, name="automation", help="Time-based task automation")

# Bridges (Discord, Slack, etc.)
app.add_typer(bridge.app, name="bridge", help="Bridge VAF to external platforms")

# Server/Hosting management
app.add_typer(server.app, name="server", help="Manage local network server mode")
app.add_typer(ww.app, name="ww", help="Whare Wananga tool self-learning (train / inspect)")

# Security diagnostics
app.add_typer(security.app, name="security", help="Run security diagnostics")

# Sub-Agent execution (internal use - for separate terminal windows)
app.add_typer(subagent.app, name="subagent", help="Run sub-agents in separate terminals")

# Workflow execution (internal use - for separate terminal windows)
app.add_typer(workflow.app, name="workflow", help="Run workflows in separate terminals")

# ═══════════════════════════════════════════════════════════════════════════════
# STANDALONE COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

app.command(name="info")(info.info)
app.command(name="install-gpu")(info.install_gpu)
app.command(name="doctor")(security.doctor)

app.command(name="start",   help="Start VAF as a background service")(service.cmd_start)
app.command(name="stop",    help="Stop the VAF background service")(service.cmd_stop)
app.command(name="restart", help="Restart the VAF background service")(service.cmd_restart)
app.command(name="status",  help="Show VAF service status")(service.cmd_status)

@app.command(name="tray")
def tray_command():
    """Start the VAF System Tray application (Persistent Background Service)."""
    def _log_tray_error(msg: str, err: str = ""):
        """Write to logs/tray_startup_YYYY-MM-DD.txt for diagnostics (works even before tray import)."""
        try:
            from vaf.core.log_helper import get_dated_log_path
            fpath = get_dated_log_path("tray_startup", "txt")
            fpath.parent.mkdir(parents=True, exist_ok=True)
            import datetime
            ts = datetime.datetime.now().isoformat()
            with open(fpath, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}")
                if err:
                    f.write(f" | ERROR: {err}")
                f.write("\n")
        except Exception:
            pass

    try:
        _log_tray_error("Tray command started (main.py)")
        # Check if launched from native macOS Swift wrapper
        if os.environ.get("VAF_NATIVE_WRAPPER") == "1":
            # Native wrapper handles tray icon - run headless (backend + frontend only)
            print("[VAF] Native wrapper detected - running headless (no Python tray)")
            from vaf.tray import run_headless
            run_headless()
        else:
            from vaf.tray import run_app
            run_app()
    except ImportError as e:
        _log_tray_error("Tray ImportError", str(e))
        print(f"Error starting tray app: {e}")
        print("Please ensure requirements are installed: pip install -r requirements.txt")
    except Exception as e:
        import traceback
        _log_tray_error("Tray exception", f"{e}\n{traceback.format_exc()}")
        print(f"Tray app error: {e}")

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
                
                elif cmd in ("listen", "l"):
                    # Speech-to-Text Input
                    captured = tui.listen_overlay()
                    if captured:
                        # Replace user input with captured text and proceed to send
                        user_input = captured
                        # Don't continue - fall through to message processing
                    else:
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
# NON-INTERACTIVE PROMPT COMMAND (SCRIPTING)
# ═══════════════════════════════════════════════════════════════════════════════

@app.command(name="prompt")
def prompt_command(
    prompt: str = typer.Option(..., "--prompt", "-p", help="Prompt text (non-interactive)"),
    output_format: str = typer.Option("text", "--output-format", help="text | json | stream-json"),
    session: str = typer.Option(None, "--session", "-s", help="Load an existing session ID"),
    save_session: bool = typer.Option(False, "--save-session", help="Save this interaction as a session"),
):
    """
    Non-interactive mode (scripting).

    Examples:
        vaf prompt -p "Explain this repo" --output-format json
        vaf prompt -p "Search latest release notes" --output-format stream-json
    """
    import json
    import os
    import sys
    from vaf.core.agent import Agent
    from vaf.core.session import SessionManager
    from vaf.cli.ui import UI

    fmt = (output_format or "text").strip().lower()
    if fmt not in ("text", "json", "stream-json"):
        raise typer.BadParameter("output-format must be one of: text, json, stream-json")

    # In non-interactive mode we never block on prompts (trust gating will return errors).
    os.environ["VAF_NONINTERACTIVE"] = "1"

    # Silence rich UI events for machine-readable outputs
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

    # Restore session history if provided (keeps system prompt at history[0])
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

# ═══════════════════════════════════════════════════════════════════════════════
# TRUST COMMAND (Trusted folders / capability gating)
# ═══════════════════════════════════════════════════════════════════════════════

@app.command(name="trust")
def trust_command(
    path: str = typer.Argument(".", help="Folder to trust (default: current directory)"),
    list_trusted: bool = typer.Option(False, "--list", "-l", help="List trusted folders"),
    remove: str = typer.Option(None, "--remove", "-r", help="Remove a trusted folder by exact path"),
    status: bool = typer.Option(False, "--status", help="Show whether current folder is trusted"),
):
    """
    Manage trusted folders (used by the once/always/cancel gate for risky tools).
    """
    from pathlib import Path
    from vaf.cli.ui import UI
    from vaf.core.trust import load_trust_state, save_trust_state, mark_trusted_dir, is_trusted_dir

    state = load_trust_state()

    if list_trusted:
        if not state.trusted_dirs:
            UI.print("No trusted folders configured.")
            raise typer.Exit(0)
        UI.print("Trusted folders:")
        for p in sorted(state.trusted_dirs):
            UI.print(f"- {p}")
        raise typer.Exit(0)

    if remove:
        before = set(state.trusted_dirs)
        if remove in state.trusted_dirs:
            state.trusted_dirs.remove(remove)
            save_trust_state(state)
            UI.success("Removed trusted folder.")
        else:
            UI.warning("Path not found in trusted folders.")
        raise typer.Exit(0)

    p = Path(path).expanduser().resolve()

    if status:
        UI.print(f"Trusted: {'yes' if is_trusted_dir(p) else 'no'}")
        raise typer.Exit(0)

    mark_trusted_dir(p)
    UI.success(f"Trusted folder added: {p}")
    UI.print("Tip: 'always' decisions in the gate will also mark the current folder trusted.")

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
        from vaf import __version__
        UI.print("[cyan bold]VAF[/cyan bold] - Veyllo Agentic Framework")
        UI.print(f"Version: [green]{__version__}[/green]")
        UI.print("https://github.com/Veyllo-Labs/VAF")
        raise typer.Exit()

def main():
    """Entry point for console script."""
    # Check if we are running in a Py2App bundle (Frozen)
    if getattr(sys, "frozen", False):
        tray_command()
    else:
        app()

if __name__ == "__main__":
    main()
