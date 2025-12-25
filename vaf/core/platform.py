"""
VAF Platform - Cross-platform utilities
Handles OS-specific differences for Windows, macOS, and Linux
"""
import os
import sys
import platform
import shutil
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
                browsers = [
                    # Chrome/Chromium
                    ("chrome.exe", ["--incognito", url]),
                    ("msedge.exe", ["--inprivate", url]),
                    ("brave.exe", ["--incognito", url]),
                    # Firefox
                    ("firefox.exe", ["-private-window", url]),
                ]
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
        
        try:
            if Platform.is_windows():
                # Windows: Use start cmd /k to open new window
                if title:
                    cmd = f'start "{title}" cmd /k "{command}"'
                else:
                    cmd = f'start cmd /k "{command}"'
                subprocess.Popen(cmd, shell=True)
                return True
                
            elif Platform.is_macos():
                # macOS: Use osascript to open Terminal.app
                script = f'''
                tell application "Terminal"
                    activate
                    do script "{command.replace('"', '\\"')}"
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

