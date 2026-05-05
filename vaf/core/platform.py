"""
VAF Platform - Cross-platform utilities
Handles OS-specific differences for Windows, macOS, and Linux
"""
import os
import sys
import platform
import shutil
import shlex
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, Any
import webbrowser

# ═══════════════════════════════════════════════════════════════════════════════
# PLATFORM DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

class Platform:
    """Cross-platform utilities and detection."""
    
    # Platform constants
    WINDOWS = "windows"
    MACOS = "darwin"
    LINUX = "linux"
    
    @staticmethod
    def current() -> str:
        """Get current platform name."""
        if sys.platform == "win32":
            return Platform.WINDOWS
        elif sys.platform == "darwin":
            return Platform.MACOS
        else:
            return Platform.LINUX
    
    @staticmethod
    def is_windows() -> bool:
        return sys.platform == "win32"
    
    @staticmethod
    def is_macos() -> bool:
        return sys.platform == "darwin"
    
    @staticmethod
    def is_linux() -> bool:
        return sys.platform.startswith("linux")
    
    @staticmethod
    def is_unix() -> bool:
        """Returns True for macOS and Linux."""
        return not Platform.is_windows()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # DIRECTORIES
    # ═══════════════════════════════════════════════════════════════════════════
    
    @staticmethod
    def home_dir() -> Path:
        """Get user home directory (cross-platform)."""
        return Path.home()

    @staticmethod
    def documents_dir() -> Path:
        """
        Get the user's Documents directory (cross-platform best-effort).

        Note: This intentionally avoids OS-specific shell calls. Most platforms follow:
        - Windows: %USERPROFILE%\\Documents
        - macOS/Linux: ~/Documents
        """
        return Path.home() / "Documents"
    
    @staticmethod
    def downloads_dir() -> Path:
        """
        Get the user's Downloads directory (cross-platform best-effort).
        
        Returns:
            Path to user's Downloads folder
        """
        return Path.home() / "Downloads"
    
    @staticmethod
    def get_research_dir() -> Path:
        """
        Get the directory for storing research reports.
        Tries Documents/VAF_Research first, falls back to Downloads/VAF_Research.
        Creates the directory if it doesn't exist.
        
        Returns:
            Path to research directory
        """
        # Try Documents first
        docs = Platform.documents_dir()
        if docs.exists():
            research_dir = docs / "VAF_Research"
            research_dir.mkdir(exist_ok=True)
            return research_dir
        
        # Fallback to Downloads
        downloads = Platform.downloads_dir()
        research_dir = downloads / "VAF_Research"
        research_dir.mkdir(exist_ok=True)
        return research_dir

    # ═══════════════════════════════════════════════════════════════════════════
    # UX HELPERS (Open Browser / File Explorer)
    # ═══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def open_url(url: str, incognito: bool = True) -> bool:
        """
        Open a URL in the user's default browser (best-effort).
        
        Args:
            url: The URL to open
            incognito: If True, open in incognito/private mode (default: True for privacy)
        
        Returns:
            True if successful, False otherwise
        """
        try:
            if not url:
                return False
            
            # If incognito is disabled, use standard method
            if not incognito:
                return bool(webbrowser.open_new_tab(url))
            
            # Try to detect and use browser-specific incognito flags
            import subprocess
            
            # Common browser executables and their incognito flags
            browsers = []
            
            if Platform.is_windows():
                # Common Windows browser paths to check if not in PATH
                common_paths = [
                    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
                    os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                    os.path.expandvars(r"%ProgramFiles%\Mozilla Firefox\firefox.exe"),
                    os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
                ]
                
                # Check PATH first, then common locations
                browsers = [
                    ("chrome.exe", ["--incognito", url]),
                    ("msedge.exe", ["--inprivate", url]),
                    ("brave.exe", ["--incognito", url]),
                    ("firefox.exe", ["-private-window", url]),
                ]
                
                # Add found absolute paths to the list
                for path in common_paths:
                    if os.path.exists(path):
                        # Determine flag based on browser name
                        lower_path = path.lower()
                        flag = "--incognito"
                        if "firefox" in lower_path: flag = "-private-window"
                        elif "edge" in lower_path: flag = "--inprivate"
                        
                        browsers.insert(0, (path, [flag, url])) # Prioritize found paths

            elif Platform.is_macos():
                browsers = [
                    # Chrome/Chromium
                    ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", ["--incognito", url]),
                    ("/Applications/Chromium.app/Contents/MacOS/Chromium", ["--incognito", url]),
                    ("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", ["--incognito", url]),
                    # Firefox
                    ("/Applications/Firefox.app/Contents/MacOS/firefox", ["-private-window", url]),
                    # Safari (macOS only)
                    ("/Applications/Safari.app/Contents/MacOS/Safari", ["--private", url]),
                ]
            else:  # Linux
                browsers = [
                    # Chrome/Chromium
                    ("google-chrome", ["--incognito", url]),
                    ("chromium-browser", ["--incognito", url]),
                    ("chromium", ["--incognito", url]),
                    ("brave-browser", ["--incognito", url]),
                    # Firefox
                    ("firefox", ["-private-window", url]),
                ]
            
            # Try each browser in order
            for browser_cmd, args in browsers:
                browser_path = shutil.which(browser_cmd) if not browser_cmd.startswith("/") else (browser_cmd if os.path.exists(browser_cmd) else None)
                
                if browser_path:
                    try:
                        # Use Popen to avoid blocking
                        subprocess.Popen(
                            [browser_path] + args,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True  # Detach from parent process
                        )
                        return True
                    except Exception:
                        continue  # Try next browser
            
            # Fallback: Use standard webbrowser (no incognito, but at least opens)
            return bool(webbrowser.open_new_tab(url))
            
        except Exception:
            # Final fallback: standard method
            try:
                return bool(webbrowser.open_new_tab(url))
            except Exception:
                return False

    @staticmethod
    def open_path(path: Path) -> bool:
        """
        Open a file or folder with the OS default handler.
        - Folders: file explorer
        - Files: default associated app (HTML opens in browser)
        """
        try:
            p = Path(path).expanduser()
            if not p.exists():
                return False

            if Platform.is_windows():
                # os.startfile is the most reliable on Windows
                os.startfile(str(p))  # type: ignore[attr-defined]
                return True

            # macOS/Linux: use open/xdg-open if available, else fall back to webbrowser
            import subprocess

            if Platform.is_macos() and shutil.which("open"):
                subprocess.Popen(["open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True

            if Platform.is_linux() and shutil.which("xdg-open"):
                subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True

            # Fallback: try browser (works for file:// on many platforms)
            return bool(webbrowser.open(str(p.as_uri())))
        except Exception:
            return False
    
    @staticmethod
    def config_dir() -> Path:
        """
        Get appropriate config directory for each platform.
        - Windows: %APPDATA%/vaf
        - macOS: ~/Library/Application Support/vaf
        - Linux: ~/.config/vaf
        """
        if Platform.is_windows():
            base = os.environ.get("APPDATA", str(Path.home()))
            return Path(base) / "vaf"
        elif Platform.is_macos():
            return Path.home() / "Library" / "Application Support" / "vaf"
        else:
            xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
            return Path(xdg_config) / "vaf"
    
    @staticmethod
    def data_dir() -> Path:
        """
        Get appropriate data directory for each platform.
        - Windows: %LOCALAPPDATA%/vaf
        - macOS: ~/Library/Application Support/vaf
        - Linux: ~/.local/share/vaf
        """
        if Platform.is_windows():
            base = os.environ.get("LOCALAPPDATA", os.environ.get("APPDATA", str(Path.home())))
            return Path(base) / "vaf"
        elif Platform.is_macos():
            return Path.home() / "Library" / "Application Support" / "vaf"
        else:
            xdg_data = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
            return Path(xdg_data) / "vaf"
    
    @staticmethod
    def cache_dir() -> Path:
        """
        Get appropriate cache directory for each platform.
        - Windows: %LOCALAPPDATA%/vaf/cache
        - macOS: ~/Library/Caches/vaf
        - Linux: ~/.cache/vaf
        """
        if Platform.is_windows():
            base = os.environ.get("LOCALAPPDATA", os.environ.get("APPDATA", str(Path.home())))
            return Path(base) / "vaf" / "cache"
        elif Platform.is_macos():
            return Path.home() / "Library" / "Caches" / "vaf"
        else:
            xdg_cache = os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
            return Path(xdg_cache) / "vaf"
    
    @staticmethod
    def vaf_dir() -> Path:
        """
        Get VAF directory (simplified, uses ~/.vaf on all platforms).
        This is the backward-compatible option.
        """
        return Path.home() / ".vaf"

    @staticmethod
    def get_context_log_dir() -> Path:
        """
        Directory for Soul/RAG context logs. Resolution order:
        1. VAF_LOG_DIR env (e.g. d:\\VAF\\logs)
        2. Platform.vaf_dir() / "logs"
        3. Repo root / logs (from this file: vaf/core/platform.py -> parents[2] = repo)
        """
        env_dir = os.environ.get("VAF_LOG_DIR")
        if env_dir:
            return Path(env_dir)
        try:
            return Platform.vaf_dir() / "logs"
        except Exception:
            pass
        return Path(__file__).resolve().parents[2] / "logs"

    # ═══════════════════════════════════════════════════════════════════════════
    # AUTOSTART (Tray)
    # ═══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _tray_command_args() -> list[str]:
        """Build the command used to launch the tray app."""
        python_bin = Path(sys.executable)
        if Platform.is_windows():
            if python_bin.name.lower() == "python.exe":
                candidate = python_bin.with_name("pythonw.exe")
                if candidate.exists():
                    python_bin = candidate
        return [str(python_bin), "-m", "vaf.main", "tray"]

    @staticmethod
    def set_tray_autostart(enable: bool) -> bool:
        """
        Enable/disable OS login autostart for the tray app.

        Returns:
            True if the change was applied successfully, False otherwise.
        """
        try:
            cmd_args = Platform._tray_command_args()
            if Platform.is_windows():
                base = os.environ.get("APPDATA", str(Path.home()))
                startup_dir = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
                startup_dir.mkdir(parents=True, exist_ok=True)
                entry_path = startup_dir / "VAF Tray.cmd"
                if enable:
                    python_cmd = f'"{cmd_args[0]}"' if " " in cmd_args[0] else cmd_args[0]
                    cmd_line = " ".join([python_cmd] + cmd_args[1:])
                    entry_path.write_text(f"@echo off\nstart \"\" {cmd_line}\n", encoding="utf-8")
                else:
                    if entry_path.exists():
                        entry_path.unlink()
                return True

            if Platform.is_macos():
                agents_dir = Path.home() / "Library" / "LaunchAgents"
                agents_dir.mkdir(parents=True, exist_ok=True)
                entry_path = agents_dir / "com.vaf.tray.plist"
                if enable:
                    args_xml = "\n".join([f"            <string>{arg}</string>" for arg in cmd_args])
                    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
    <dict>
        <key>Label</key>
        <string>com.vaf.tray</string>
        <key>ProgramArguments</key>
        <array>
{args_xml}
        </array>
        <key>RunAtLoad</key>
        <true/>
    </dict>
</plist>
"""
                    entry_path.write_text(plist, encoding="utf-8")
                    if shutil.which("launchctl"):
                        try:
                            subprocess.run(["launchctl", "unload", str(entry_path)], check=False)
                            subprocess.run(["launchctl", "load", str(entry_path)], check=False)
                        except Exception:
                            pass
                else:
                    if entry_path.exists():
                        if shutil.which("launchctl"):
                            try:
                                subprocess.run(["launchctl", "unload", str(entry_path)], check=False)
                            except Exception:
                                pass
                        entry_path.unlink()
                return True

            if Platform.is_linux():
                config_dir = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
                autostart_dir = config_dir / "autostart"
                autostart_dir.mkdir(parents=True, exist_ok=True)
                entry_path = autostart_dir / "vaf-tray.desktop"
                if enable:
                    exec_cmd = " ".join(shlex.quote(arg) for arg in cmd_args)
                    desktop_entry = "\n".join([
                        "[Desktop Entry]",
                        "Type=Application",
                        "Name=VAF Tray",
                        f"Exec={exec_cmd}",
                        "X-GNOME-Autostart-enabled=true",
                        "NoDisplay=false",
                        ""
                    ])
                    entry_path.write_text(desktop_entry, encoding="utf-8")
                else:
                    if entry_path.exists():
                        entry_path.unlink()
                return True

            return False
        except Exception:
            return False
    
    # ═══════════════════════════════════════════════════════════════════════════
    # SHELL
    # ═══════════════════════════════════════════════════════════════════════════
    
    @staticmethod
    def default_shell() -> str:
        """Get the default shell for the current platform."""
        if Platform.is_windows():
            # Prefer PowerShell if available
            if shutil.which("pwsh"):
                return "pwsh"  # PowerShell Core
            elif shutil.which("powershell"):
                return "powershell"
            return "cmd"
        elif Platform.is_macos():
            # macOS Catalina+ uses zsh by default
            if os.path.exists("/bin/zsh"):
                return "/bin/zsh"
            return "/bin/bash"
        else:
            # Linux: bash or sh
            if os.path.exists("/bin/bash"):
                return "/bin/bash"
            return "/bin/sh"
    
    @staticmethod
    def shell_args(shell: str = None) -> Dict[str, Any]:
        """Get subprocess arguments for shell execution."""
        shell = shell or Platform.default_shell()
        
        if Platform.is_windows():
            return {
                "shell": True,
                "env": {**os.environ, "PYTHONIOENCODING": "utf-8"}
            }
        else:
            return {
                "shell": True,
                "executable": shell,
                "env": {**os.environ, "PYTHONIOENCODING": "utf-8"}
            }
    
    # ═══════════════════════════════════════════════════════════════════════════
    # COMMANDS
    # ═══════════════════════════════════════════════════════════════════════════
    
    @staticmethod
    def clear_command() -> str:
        """Get the clear screen command."""
        return "cls" if Platform.is_windows() else "clear"
    
    @staticmethod
    def list_command() -> str:
        """Get the list directory command."""
        return "dir" if Platform.is_windows() else "ls -la"
    
    @staticmethod
    def which(program: str) -> Optional[str]:
        """Find a program in PATH (cross-platform which/where)."""
        return shutil.which(program)
    
    @staticmethod
    def has_command(program: str) -> bool:
        """Check if a command is available."""
        return Platform.which(program) is not None
    
    @staticmethod
    def has_git() -> bool:
        """Check if Git is installed."""
        return Platform.has_command("git")
    
    @staticmethod
    def open_new_terminal(command: str, title: str = None) -> bool:
        """
        Open a new terminal window and execute a command. OS-independent.

        Args:
            command: Command to execute in the new terminal
            title: Optional title for the terminal window

        Returns:
            True if successful, False otherwise
        """
        import subprocess
        from pathlib import Path
        import datetime

        # CRITICAL: Log IMMEDIATELY at function entry - NO try/except to ensure we see this
        log_dir = Path(os.environ.get("VAF_LOG_DIR", str(Path(__file__).resolve().parents[2] / "logs")))
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        platform_log = log_dir / f"platform_subprocess_{date_str}.log"
        with open(platform_log, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().isoformat()
            webui_check = os.environ.get("VAF_WEBUI_ACTIVE", "NOT SET")
            f.write(f"{ts} === open_new_terminal CALLED ===\n")
            f.write(f"{ts} Command: {command[:500]}\n")
            f.write(f"{ts} VAF_WEBUI_ACTIVE={webui_check}\n")
            f.write(f"{ts} VAF_TASK_ID={os.environ.get('VAF_TASK_ID', 'NOT SET')}\n")
            f.write(f"{ts} VAF_SESSION_ID={os.environ.get('VAF_SESSION_ID', 'NOT SET')}\n")

        try:

            webui_active = os.environ.get("VAF_WEBUI_ACTIVE", "").strip().lower() in ("1", "true", "yes")
            if webui_active:
                # Copy current environment and ensure all VAF_ vars are passed
                env = os.environ.copy()

                # stdout is piped (not a real terminal), so Rich Live TUI would
                # flood the pipe buffer (4 KB on Windows) and deadlock the process.
                env["VAF_NONINTERACTIVE"] = "1"

                # Force Python to use unbuffered stdout in the child process.
                # Without this, piped stdout uses ~8 KB full buffering and the
                # parent's _stream_output thread sees nothing until the child exits.
                env["PYTHONUNBUFFERED"] = "1"

                # Log the command being executed for debugging
                import logging
                logger = logging.getLogger("vaf.platform")

                logger.debug(f"Spawning sub-agent command: {command}")
                logger.debug(f"VAF_TASK_ID={env.get('VAF_TASK_ID', 'NOT SET')}")
                logger.debug(f"VAF_AGENT_TYPE={env.get('VAF_AGENT_TYPE', 'NOT SET')}")

                proc = subprocess.Popen(
                    command,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                    env=env  # Explicitly pass environment
                )
                # Track spawned sub-agent process for hard-stop from Web UI.
                Platform.register_webui_subagent_process(
                    proc=proc,
                    session_id=os.environ.get("VAF_SESSION_ID", "").strip() or None,
                    task_id=os.environ.get("VAF_TASK_ID", "").strip() or None,
                    command=command,
                )
                try:
                    import requests
                    from vaf.core.config import Config
                    session_id = os.environ.get("VAF_SESSION_ID", "").strip()
                    task_id = os.environ.get("VAF_TASK_ID", "").strip()
                    agent_type = os.environ.get("VAF_AGENT_TYPE", "").strip()

                    # Resolve correct internal API port: when TLS is active the
                    # main backend on 8001 expects HTTPS; the non-SSL internal
                    # channel runs on 8005 instead.
                    tls_on = Config.get("local_network_tls_enabled", False)
                    _api_port = 8005 if tls_on else 8001
                    _api_base = f"http://127.0.0.1:{_api_port}"

                    def _send_web_update(data: dict):
                        if not session_id:
                            return
                        try:
                            data["sessionId"] = session_id
                            requests.post(
                                f"{_api_base}/api/subagent/stream",
                                json=data,
                                timeout=0.5
                            )
                        except Exception:
                            pass

                    if session_id and (task_id or agent_type):
                        title = (agent_type or "Sub-Agent").replace("_", " ").title()
                        cfg = Config.load()
                        main_provider = cfg.get("provider", "local")
                        subagent_provider = cfg.get("subagent_provider", "inherit")
                        use_separate = cfg.get("subagent_use_separate_provider", False)
                        effective_provider = subagent_provider if use_separate and subagent_provider != "inherit" else main_provider
                        if effective_provider != "local":
                            model = cfg.get(f"api_model_{effective_provider}", "") or cfg.get("model", "")
                        else:
                            model = cfg.get("model", "")
                        steps = [{
                            "id": task_id or "subagent",
                            "title": title,
                            "description": "Sub-agent running...",
                            "status": "running",
                            "actions": []
                        }]
                        _send_web_update({
                            "type": "subagent_update",
                            "agentName": title,
                            "status": "Running sub-agent task...",
                            "presence": "online",
                            "provider": effective_provider,
                            "model": model,
                            "file": "",
                            "code": "",
                            "steps": steps
                        })

                    def _stream_output():
                        if not proc.stdout:
                            return
                        import queue as _queue

                        output_lines = []
                        _line_q: _queue.Queue = _queue.Queue()

                        def _pipe_drain():
                            """Fast reader: drains OS pipe into unbounded in-memory queue
                            so the subprocess never blocks on a full pipe buffer."""
                            try:
                                for raw in proc.stdout:
                                    _line_q.put(raw)
                            except Exception:
                                pass
                            finally:
                                _line_q.put(None)

                        threading.Thread(target=_pipe_drain, daemon=True).start()

                        while True:
                            raw = _line_q.get()
                            if raw is None:
                                break
                            clean = raw.rstrip("\r\n")
                            if not clean:
                                continue
                            output_lines.append(clean)
                            _send_web_update({
                                "type": "subagent_output_stream",
                                "taskId": task_id or None,
                                "agentType": agent_type or None,
                                "line": clean
                            })

                        try:
                            proc.wait(timeout=900)
                        except subprocess.TimeoutExpired:
                            logger.warning(f"Sub-agent process {proc.pid} did not exit within 900s, killing")
                            proc.kill()
                            proc.wait(timeout=10)
                        try:
                            Platform.unregister_webui_subagent_process(proc.pid)
                        except Exception:
                            pass
                        if proc.returncode != 0:
                            error_msg = f"Sub-agent process exited with code {proc.returncode}"
                            if output_lines:
                                error_msg += f". Last output: {' | '.join(output_lines[-5:])}"
                            logger.error(error_msg)
                            try:
                                from vaf.core.subagent_ipc import get_ipc
                                ipc = get_ipc()
                                if task_id:
                                    ipc.fail_task(task_id, error_msg)
                            except Exception:
                                pass
                            _agent_title = (agent_type or "Sub-Agent").replace("_", " ").title()
                            _send_web_update({
                                "type": "subagent_update",
                                "agentName": _agent_title,
                                "status": f"Process exited with error (code {proc.returncode})",
                                "presence": "error",
                                "steps": [{
                                    "id": task_id or "subagent",
                                    "title": _agent_title,
                                    "description": "Process error",
                                    "status": "failed",
                                    "actions": [],
                                }],
                            })
                        else:
                            _agent_title = (agent_type or "Sub-Agent").replace("_", " ").title()
                            _send_web_update({
                                "type": "subagent_update",
                                "agentName": _agent_title,
                                "status": "Completed",
                                "presence": "idle",
                                "steps": [{
                                    "id": task_id or "subagent",
                                    "title": _agent_title,
                                    "description": "Completed",
                                    "status": "completed",
                                    "actions": [],
                                }],
                            })

                    import threading
                    threading.Thread(target=_stream_output, daemon=True).start()
                except Exception as e:
                    logger.error(f"Error setting up sub-agent streaming: {e}")
                return True
            if Platform.is_windows():
                # Windows: Use start cmd /c to open new window and close when done
                # /c = execute command then terminate (terminal closes after exit)
                # /k = execute command and remain (keeps terminal open - DON'T use!)
                # Note: os is imported at module level, don't re-import here!
                cwd = os.getcwd()
                if title:
                    # Use /D to set working directory and ensure command is properly quoted
                    cmd = f'start "{title}" /D "{cwd}" cmd /c "{command}"'
                else:
                    cmd = f'start /D "{cwd}" cmd /c "{command}"'
                try:
                    subprocess.Popen(cmd, shell=True, cwd=cwd)
                    return True
                except Exception as e:
                    # Fallback: try without title
                    try:
                        cmd = f'start cmd /c "{command}"'
                        subprocess.Popen(cmd, shell=True, cwd=cwd)
                        return True
                    except:
                        return False
                
            elif Platform.is_macos():
                # macOS: Use osascript to open Terminal.app
                escaped_command = command.replace('"', '\\"')
                script = f'''
                tell application "Terminal"
                    activate
                    do script "{escaped_command}"
                end tell
                '''
                subprocess.Popen(['osascript', '-e', script])
                return True
                
            else:
                # Linux: Try different terminal emulators
                terminals = [
                    ('gnome-terminal', ['--', 'bash', '-c', f'{command}; exec bash']),
                    ('xterm', ['-e', 'bash', '-c', f'{command}; exec bash']),
                    ('konsole', ['-e', 'bash', '-c', f'{command}; exec bash']),
                    ('x-terminal-emulator', ['-e', 'bash', '-c', f'{command}; exec bash']),
                ]
                
                for term_name, term_args in terminals:
                    if Platform.has_command(term_name):
                        if title:
                            # Some terminals support title
                            if term_name == 'gnome-terminal':
                                term_args.insert(0, f'--title={title}')
                            elif term_name == 'xterm':
                                term_args.insert(0, f'-T')
                                term_args.insert(1, title)
                        subprocess.Popen([term_name] + term_args)
                        return True
                
                # Fallback: try xdg-terminal or just run in background
                return False
                
        except Exception:
            return False

    # Registry of sub-agent processes spawned from WebUI path.
    # Key: pid, Value: metadata dict
    _webui_subagent_processes: Dict[int, Dict[str, Any]] = {}
    _webui_subagent_lock = None

    @staticmethod
    def _get_webui_subagent_lock():
        if Platform._webui_subagent_lock is None:
            import threading
            Platform._webui_subagent_lock = threading.Lock()
        return Platform._webui_subagent_lock

    @staticmethod
    def register_webui_subagent_process(proc, session_id: Optional[str], task_id: Optional[str], command: str) -> None:
        """Register a spawned WebUI sub-agent process for stop/cancel handling."""
        if not proc or not getattr(proc, "pid", None):
            return
        lock = Platform._get_webui_subagent_lock()
        with lock:
            Platform._webui_subagent_processes[int(proc.pid)] = {
                "session_id": (session_id or "").strip(),
                "task_id": (task_id or "").strip(),
                "command": str(command or ""),
                "created_at": time.time(),
            }

    @staticmethod
    def unregister_webui_subagent_process(pid: int) -> None:
        lock = Platform._get_webui_subagent_lock()
        with lock:
            Platform._webui_subagent_processes.pop(int(pid), None)

    @staticmethod
    def stop_webui_subagent_processes(session_id: str = None) -> int:
        """
        Hard-stop tracked WebUI sub-agent processes.
        If session_id is None, stops ALL tracked processes (used on startup).
        If session_id is given, stops only processes for that session.
        Returns number of processes targeted.
        """
        lock = Platform._get_webui_subagent_lock()
        with lock:
            if session_id is None:
                matches = list(Platform._webui_subagent_processes.items())
            else:
                sid = (session_id or "").strip()
                if not sid:
                    return 0
                matches = [
                    (pid, meta)
                    for pid, meta in Platform._webui_subagent_processes.items()
                    if (meta.get("session_id") or "").strip() == sid
                ]
        if not matches:
            return 0

        stopped = 0
        for pid, _meta in matches:
            try:
                try:
                    import psutil  # type: ignore
                    p = psutil.Process(pid)
                    children = p.children(recursive=True)
                    for c in children:
                        try:
                            c.terminate()
                        except Exception:
                            pass
                    try:
                        p.terminate()
                    except Exception:
                        pass
                    # Give processes a brief grace period
                    gone, alive = psutil.wait_procs([p] + children, timeout=1.5)
                    for a in alive:
                        try:
                            a.kill()
                        except Exception:
                            pass
                except Exception:
                    # Fallback without psutil
                    if Platform.is_windows():
                        subprocess.run(
                            ["taskkill", "/PID", str(pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                        )
                    else:
                        os.kill(pid, 15)
                stopped += 1
            except Exception:
                pass
            finally:
                try:
                    Platform.unregister_webui_subagent_process(pid)
                except Exception:
                    pass
        return stopped
    
    # ═══════════════════════════════════════════════════════════════════════════
    # PATHS
    # ═══════════════════════════════════════════════════════════════════════════
    
    @staticmethod
    def normalize_path(path: str) -> Path:
        """Normalize a path for the current platform."""
        p = Path(path)
        # Expand ~ on all platforms
        if str(p).startswith("~"):
            p = p.expanduser()
        return p.resolve()
    
    @staticmethod
    def to_posix(path: Path) -> str:
        """Convert path to POSIX format (forward slashes)."""
        return path.as_posix()
    
    @staticmethod
    def path_separator() -> str:
        """Get the path separator for the current platform."""
        return os.sep
    
    @staticmethod
    def pathlist_separator() -> str:
        """Get the PATH list separator (: on Unix, ; on Windows)."""
        return os.pathsep
    
    # ═══════════════════════════════════════════════════════════════════════════
    # SYSTEM INFO
    # ═══════════════════════════════════════════════════════════════════════════
    
    @staticmethod
    def info() -> Dict[str, str]:
        """Get system information."""
        return {
            "platform": Platform.current(),
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python": platform.python_version(),
            "home": str(Platform.home_dir()),
            "shell": Platform.default_shell(),
            "git": "yes" if Platform.has_git() else "no",
        }
    
    @staticmethod
    def arch() -> str:
        """Get system architecture."""
        machine = platform.machine().lower()
        if machine in ("x86_64", "amd64"):
            return "x64"
        elif machine in ("arm64", "aarch64"):
            return "arm64"
        elif machine in ("i386", "i686", "x86"):
            return "x86"
        return machine
    
    # ═══════════════════════════════════════════════════════════════════════════
    # TERMINAL
    # ═══════════════════════════════════════════════════════════════════════════
    
    @staticmethod
    def supports_unicode() -> bool:
        """Check if terminal supports Unicode."""
        if Platform.is_windows():
            # Windows Terminal and modern consoles support Unicode
            return os.environ.get("WT_SESSION") is not None or \
                   os.environ.get("TERM_PROGRAM") == "vscode"
        return True  # Unix terminals generally support Unicode
    
    @staticmethod
    def supports_color() -> bool:
        """Check if terminal supports colors."""
        # Check NO_COLOR env var (standard)
        if os.environ.get("NO_COLOR"):
            return False
        
        # Check FORCE_COLOR env var
        if os.environ.get("FORCE_COLOR"):
            return True
        
        # Check if stdout is a TTY
        if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
            return False
        
        if Platform.is_windows():
            # Windows Terminal, VS Code, etc. support colors
            return os.environ.get("WT_SESSION") is not None or \
                   os.environ.get("TERM_PROGRAM") == "vscode" or \
                   os.environ.get("ANSICON") is not None
        
        # Unix: check TERM
        term = os.environ.get("TERM", "")
        return term != "dumb"
    
    @staticmethod
    def terminal_size() -> tuple:
        """Get terminal size (columns, lines)."""
        try:
            size = os.get_terminal_size()
            return (size.columns, size.lines)
        except OSError:
            return (80, 24)  # Default fallback

    @staticmethod
    def is_process_running(pid: int) -> bool:
        """
        Check if a process with the given PID is still running.
        Cross-platform implementation.
        """
        if pid <= 0:
            return False
        
        if Platform.is_windows():
            # Windows implementation
            try:
                # tasklist is slow, but available on all Windows versions
                # kernel32.OpenProcess is faster but requires ctypes
                import subprocess
                output = subprocess.check_output(
                    f'tasklist /fi "PID eq {pid}" /nh',
                    shell=True,
                    stderr=subprocess.STDOUT
                ).decode('utf-8', errors='ignore')
                return str(pid) in output
            except Exception:
                return True # Assume running on error
        else:
            # Unix implementation (macOS/Linux)
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False
            except Exception:
                return True


# ═══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def is_windows() -> bool:
    return Platform.is_windows()

def is_macos() -> bool:
    return Platform.is_macos()

def is_linux() -> bool:
    return Platform.is_linux()

def get_shell() -> str:
    return Platform.default_shell()

def get_vaf_dir() -> Path:
    return Platform.vaf_dir()

