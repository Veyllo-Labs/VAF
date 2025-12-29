import sys
import os
import platform
import json
import shutil
import subprocess
import time
import requests
import warnings
import atexit
from datetime import datetime
import re
from rich import print
from rich.markup import escape

# Dependency imports will be handled in setup or assumed present if requirements are installed
from huggingface_hub import hf_hub_download

# DuckDuckGo Search: Try new package first, fallback to legacy with suppression
try:
    from ddgs import DDGS
except ImportError:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from duckduckgo_search import DDGS

from vaf.core.config import Config
from vaf.core.backend import ServerManager
from vaf.tools.search import WebSearchTool
from vaf.tools.filesystem import ListFilesTool, ReadFileTool, WriteFileTool, MoveFileTool

import atexit
import signal

class Agent:
    REQUIRED_PACKAGES = {
        "colorama": "colorama",
        "huggingface_hub": "huggingface_hub",
        "duckduckgo_search": "duckduckgo-search",
        "llama_cpp": "llama-cpp-python"
    }
    
    # Defaults handled by Config, but fallback here
    DEFAULT_FILENAME = "VQ-1_Instruct-q4_k_m.gguf"
    
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.config = Config.load()
        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.models_dir = os.path.join(self.base_dir, "models")
        
        # Determine model filename from config path or just name
        model_name = self.config.get("model")
        # Handle full HuggingFace paths (e.g. user/repo/filename.gguf)
        if model_name.count("/") >= 2:
            parts = model_name.rsplit("/", 1)
            self.repo_id = parts[0]
            self.filename = parts[1]
            if not self.filename.endswith(".gguf"):
                self.filename += ".gguf"
        # Handle standard Repo ID (user/repo) -> assumes default filename or directory
        elif "/" in model_name: 
             self.filename = model_name.split("/")[-1] + ".gguf" 
             self.repo_id = model_name
        else:
             self.filename = model_name
             self.repo_id = "Veyllo/" + model_name.replace(".gguf", "")

        self.model_path = os.path.join(self.models_dir, self.filename)
        
        self.llm = None           # Local Library instance
        self.server = None        # ServerManager instance
        self.use_server = False   # Flag
        
        self.history = []

        # Trust gating state (session-only)
        self._allow_once_tools = set()
        self._noninteractive = os.environ.get("VAF_NONINTERACTIVE", "").strip() in ("1", "true", "yes")
        self._event_sink = None  # optional callable(dict)

        # Session tracking for server shutdown management
        self._session_id = None
        self._register_session()

        # Initialize Tools (Dynamic Loading)
        self.tools = {}
        self._load_tools()
        
        # Register Cleanup Handler (Cross-Platform)
        # WICHTIG: Nur _atexit_cleanup registrieren, nicht shutdown direkt
        # shutdown() wird von Signal-Handlern aufgerufen
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)
        
        # Unix-Specific Hangup Handler (Terminal Close)
        if hasattr(signal, "SIGHUP"):
             signal.signal(signal.SIGHUP, self.shutdown)
        
        # Windows-Specific Console Handler (Catches 'X' button)
        if platform.system() == "Windows":
            import ctypes
            from ctypes import wintypes
            
            # Define handler type: BOOL WINAPI HandlerRoutine(DWORD dwCtrlType)
            HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
            
            def _win_handler(ctrl_type):
                # CTRL_C_EVENT = 0, CTRL_BREAK_EVENT = 1, CTRL_CLOSE_EVENT = 2
                # CTRL_LOGOFF_EVENT = 5, CTRL_SHUTDOWN_EVENT = 6
                if ctrl_type in (0, 2, 5, 6):
                    self.shutdown(signum=f"Win32_{ctrl_type}")
                    return True # True = Handled
                return False

            # Keep reference to prevent GC
            self._win_handler_ref = HandlerRoutine(_win_handler)
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleCtrlHandler(self._win_handler_ref, True)
        
        # Register atexit handler as final backup (nur einmal)
        atexit.register(self._atexit_cleanup)
        
        # Flag to prevent multiple shutdown calls
        self._shutdown_called = False
    
    def _register_session(self):
        """Register this agent instance as an active session."""
        import uuid
        from pathlib import Path
        
        self._session_id = str(uuid.uuid4())
        # OS-unabhängiger Pfad
        from vaf.core.platform import Platform
        sessions_file = Platform.vaf_dir() / "active_sessions.txt"
        sessions_file.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            # Append session ID to file
            with open(sessions_file, "a", encoding="utf-8") as f:
                f.write(f"{self._session_id}\n")
        except Exception:
            pass  # Ignore errors, session tracking is best-effort
    
    def _unregister_session(self):
        """Unregister this agent instance from active sessions."""
        from pathlib import Path
        
        if not self._session_id:
            return
        
        # OS-unabhängiger Pfad
        from vaf.core.platform import Platform
        sessions_file = Platform.vaf_dir() / "active_sessions.txt"
        
        try:
            if sessions_file.exists():
                # Read all sessions, remove this one
                with open(sessions_file, "r", encoding="utf-8") as f:
                    sessions = [line.strip() for line in f if line.strip() != self._session_id]
                
                # Write back (or delete file if empty)
                if sessions:
                    with open(sessions_file, "w", encoding="utf-8") as f:
                        f.write("\n".join(sessions) + "\n")
                else:
                    sessions_file.unlink()
        except Exception:
            pass  # Ignore errors
    
    def _count_active_sessions(self) -> int:
        """Count how many active agent sessions exist, with cleanup of dead sessions."""
        from pathlib import Path
        
        # OS-unabhängiger Pfad
        from vaf.core.platform import Platform
        sessions_file = Platform.vaf_dir() / "active_sessions.txt"
        
        if not sessions_file.exists():
            return 0
        
        try:
            # Prüfe, ob der Server überhaupt noch läuft
            # Wenn nicht und persist_server=False, können wir die Session-Datei bereinigen
            persist = self.config.get("persist_server", False)
            
            # Prüfe Server-Status durch PID-Datei
            server_running = False
            try:
                from vaf.core.backend import ServerManager
                server_mgr = ServerManager()
                pid_file = Path(server_mgr.pid_file)
                
                if pid_file.exists():
                    try:
                        with open(pid_file, 'r', encoding='utf-8') as f:
                            pid = int(f.read().strip())
                        server_running = server_mgr._is_process_running(pid)
                    except (ValueError, OSError):
                        # PID-Datei ist korrupt oder Prozess existiert nicht
                        server_running = False
            except Exception:
                # ServerManager nicht verfügbar oder Fehler, annehmen dass Server nicht läuft
                server_running = False
            
            # Wenn Server nicht läuft und persist_server=False, bereinige Sessions
            if not server_running and not persist:
                # Server läuft nicht mehr, also gibt es keine aktiven Sessions
                try:
                    sessions_file.unlink()
                except Exception:
                    pass
                return 0
            
            # Server läuft noch oder persist_server=True, zähle Sessions in Datei
            with open(sessions_file, "r", encoding="utf-8") as f:
                sessions = [line.strip() for line in f if line.strip()]
            return len(sessions)
        except Exception:
            return 0
    
    def _check_other_vaf_processes(self) -> int:
        """Check if other VAF processes are still running (more reliable than session file)."""
        import os
        from pathlib import Path
        current_pid = os.getpid()
        count = 0
        
        try:
            # DEBUG: Ausgabe für Diagnose
            # print(f"[VAF DEBUG] _check_other_vaf_processes: current_pid={current_pid}")
            
            # Try using psutil if available (more reliable)
            try:
                import psutil
                for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'ppid']):
                    try:
                        if proc.info['pid'] == current_pid:
                            continue  # Skip ourselves
                        
                        # Skip if it's a child process of this one
                        if proc.info.get('ppid') == current_pid:
                            continue
                        
                        cmdline = proc.info.get('cmdline', [])
                        if not cmdline:
                            continue
                        
                        # Check if it's a VAF process
                        cmdline_str = ' '.join(cmdline).lower()
                        # Look for vaf commands: 'vaf run', 'vaf chat', or python scripts with vaf
                        # WICHTIG: Prüfe auch auf 'vaf' allein, da der Befehl variieren kann
                        is_vaf_process = False
                        if any(keyword in cmdline_str for keyword in ['vaf run', 'vaf chat', 'vaf/main.py', '-m vaf', 'vaf\\main.py']):
                            is_vaf_process = True
                        # Auch prüfen, ob 'vaf' im Pfad vorkommt (für verschiedene Installationsarten)
                        elif 'vaf' in cmdline_str and ('python' in cmdline_str or 'pythonw' in cmdline_str):
                            # Prüfe, ob es nicht ein Automation-Subprozess ist
                            if 'automation run' not in cmdline_str:
                                is_vaf_process = True
                        
                        if is_vaf_process:
                            # Verify it's actually a Python process running VAF
                            proc_name = proc.info.get('name', '').lower()
                            if 'python' in proc_name or 'pythonw' in proc_name:
                                print(f"[VAF System] Found VAF process: PID={proc.info['pid']}, cmdline={cmdline_str[:80]}")
                                count += 1
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        continue
                if count > 0:
                    print(f"[VAF System] Found {count} other VAF process(es) via psutil")
                return count
            except ImportError:
                # psutil not available, use platform-specific methods
                pass
            
            # Fallback: Platform-specific process checking
            if platform.system() == "Windows":
                try:
                    # Try wmic first (more reliable)
                    result = subprocess.run(
                        ["wmic", "process", "where", "name='python.exe'", "get", "processid,commandline", "/format:csv"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        timeout=3
                    )
                    lines = result.stdout.split('\n')
                    for line in lines:
                        line_lower = line.lower()
                        # Prüfe auf VAF-Prozesse (verschiedene Varianten)
                        if 'vaf' in line_lower and str(current_pid) not in line:
                            # Exclude automation subprocesses
                            if 'automation run' not in line_lower:
                                # Prüfe auf typische VAF-Befehle
                                if any(keyword in line_lower for keyword in ['vaf run', 'vaf chat', 'vaf\\main.py', 'vaf/main.py', '-m vaf']):
                                    print(f"[VAF System] Found VAF process: {line[:80]}")
                                    count += 1
                                # Oder wenn 'vaf' im Pfad vorkommt (für verschiedene Installationsarten)
                                elif 'vaf' in line_lower and 'python.exe' in line_lower:
                                    print(f"[VAF System] Found VAF process: {line[:80]}")
                                    count += 1
                except Exception:
                    # Fallback to tasklist
                    try:
                        result = subprocess.run(
                            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            creationflags=subprocess.CREATE_NO_WINDOW,
                            timeout=2
                        )
                        lines = result.stdout.split('\n')
                        for line in lines:
                            line_lower = line.lower()
                            if 'vaf' in line_lower and str(current_pid) not in line:
                                if 'automation run' not in line_lower:
                                    count += 1
                    except Exception:
                        pass
            else:
                # Unix/Linux/macOS: Use ps command with better filtering
                try:
                    result = subprocess.run(
                        ["ps", "aux"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=2
                    )
                    lines = result.stdout.split('\n')
                    for line in lines:
                        line_lower = line.lower()
                        if 'vaf' in line_lower and 'python' in line_lower and str(current_pid) not in line:
                            # Exclude automation subprocesses and child processes
                            if 'automation run' not in line_lower:
                                # Make sure it's a main process, not a subprocess
                                # Look for 'vaf run' or 'vaf chat' in the command
                                if any(keyword in line_lower for keyword in ['vaf run', 'vaf chat', 'vaf/main.py', '-m vaf']):
                                    count += 1
                except Exception:
                    pass
            
            if count > 0:
                print(f"[VAF System] Found {count} other VAF process(es) via platform-specific check")
            return count
        except Exception:
            # If all else fails, fall back to session file count
            return self._count_active_sessions()
    
    def _acquire_shutdown_lock(self, timeout=2.0):
        """Acquire a file lock to ensure only one process shuts down the server (Crash-Safe on Windows & Unix)."""
        from pathlib import Path
        import time
        from vaf.core.platform import Platform
        
        lock_file = Platform.vaf_dir() / "shutdown.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                if os.name == 'nt':  # Windows
                    try:
                        import msvcrt
                        # Datei öffnen (nicht exklusiv erstellen, sondern öffnen/erstellen)
                        fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o666)
                        
                        # Versuche, die ersten Bytes zu locken (Non-Blocking)
                        # LK_NBLCK = Non-blocking lock, wirft OSError wenn belegt
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        
                        # PID schreiben (optional, für Debugging)
                        os.ftruncate(fd, 0)
                        os.write(fd, str(os.getpid()).encode())
                        os.fsync(fd)  # Sicherstellen, dass geschrieben wurde
                        
                        return fd  # WICHTIG: FD zurückgeben und OFFEN lassen!
                    except (OSError, IOError, ImportError):
                        # Lock belegt oder msvcrt nicht verfügbar
                        try:
                            if 'fd' in locals():
                                os.close(fd)
                        except:
                            pass
                        time.sleep(0.1)
                        continue
                else:  # Unix/Linux/macOS
                    try:
                        import fcntl
                        fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o666)
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        
                        os.ftruncate(fd, 0)
                        os.write(fd, str(os.getpid()).encode())
                        os.fsync(fd)  # Sicherstellen, dass geschrieben wurde
                        
                        return fd  # WICHTIG: FD zurückgeben und OFFEN lassen!
                    except (OSError, IOError, ImportError):
                        # Lock belegt oder fcntl nicht verfügbar
                        try:
                            if 'fd' in locals():
                                os.close(fd)
                        except:
                            pass
                        time.sleep(0.1)
                        continue
            except Exception:
                time.sleep(0.1)
        
        return None  # Could not acquire lock
    
    def _release_shutdown_lock(self, lock_fd=None):
        """Release the shutdown lock."""
        if lock_fd is None:
            return
        
        try:
            if os.name == 'nt':  # Windows
                try:
                    import msvcrt
                    msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
                except:
                    pass
            else:  # Unix/Linux/macOS
                try:
                    import fcntl
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except:
                    pass
            
            # File descriptor schließen (gibt Lock automatisch frei)
            os.close(lock_fd)
            
            # Optional: Lock-Datei löschen (nicht zwingend nötig, da Lock weg ist)
            try:
                from vaf.core.platform import Platform
                lock_file = Platform.vaf_dir() / "shutdown.lock"
                if lock_file.exists():
                    lock_file.unlink()
            except:
                pass
        except Exception:
            pass
    
    def _atexit_cleanup(self):
        """Called by atexit - kill server if still running."""
        # Prevent multiple calls
        if hasattr(self, '_atexit_called') and self._atexit_called:
            return
        self._atexit_called = True
        
        # Check config preference first
        persist = self.config.get("persist_server", False)
        if persist:
            # User wants server to persist, don't shutdown
            return
        
        # WICHTIG: Verwende File-Lock, damit nur ein Prozess den Server stoppt
        # Dies verhindert Race Conditions wenn beide Terminal-Fenster gleichzeitig beendet werden
        # Lock wird auf offenem File Descriptor gehalten (Crash-Safe auf Windows & Unix)
        lock_fd = self._acquire_shutdown_lock(timeout=2.0)
        if lock_fd is None:
            # Another process is handling shutdown, just exit
            return
        
        try:
            # VEREINFACHTE LOGIK: Kurze Wartezeit, dann prüfen und stoppen
            import time
            time.sleep(0.3)  # Kurze Verzögerung für Race Conditions
            
            # WICHTIG: Vertraue NUR auf die Prozess-Liste, NICHT auf die Session-Datei
            # Die Session-Datei könnte veraltet sein, wenn shutdown() bereits _unregister_session() aufgerufen hat
            # aber die Datei noch nicht aktualisiert wurde
            other_processes = self._check_other_vaf_processes()
            
            # Prüfe, ob andere Prozesse existieren
            if other_processes > 0:
                return
            
            # WICHTIG: Auch wenn wir keine anderen Prozesse finden, prüfe nochmal nach kurzer Wartezeit
            # für den Fall, dass beide Fenster gleichzeitig beendet werden
            time.sleep(0.2)
            other_processes = self._check_other_vaf_processes()
            
            if other_processes > 0:
                return
            
            # Keine anderen Prozesse/Sessions gefunden - Server stoppen
            # Prüfe ob Server überhaupt läuft
            server_running = False
            try:
                from pathlib import Path
                from vaf.core.backend import ServerManager
                server_mgr = ServerManager()
                pid_file = Path(server_mgr.pid_file)
                if pid_file.exists():
                    with open(pid_file, 'r', encoding='utf-8') as f:
                        server_pid = int(f.read().strip())
                    if server_mgr._is_process_running(server_pid):
                        try:
                            response = requests.get("http://127.0.0.1:8080/health", timeout=1)
                            if response.status_code == 200:
                                server_running = True
                        except:
                            pass
            except Exception:
                pass
            
            # Server stoppen (WICHTIG: Immer stoppen, wenn wir hier sind und keine anderen Prozesse existieren)
            print(f"\n[VAF] Stopping server (no active sessions remaining)...")
            
            try:
                if self.server and self.use_server:
                    self.server.stop_server()
                else:
                    from vaf.core.backend import ServerManager
                    server_mgr = ServerManager()
                    server_mgr.stop_server()
            except Exception as e:
                # Versuche es nochmal
                try:
                    from vaf.core.backend import ServerManager
                    server_mgr = ServerManager()
                    server_mgr.stop_server()
                except:
                    pass
        finally:
            # Lock freigeben
            self._release_shutdown_lock(lock_fd)

    def shutdown(self, signum=None, frame=None):
        """Cleanup resources on exit - works for both signal handlers and manual calls."""
        # Prevent multiple shutdown calls
        if hasattr(self, '_shutdown_called') and self._shutdown_called:
            return
        self._shutdown_called = True
        
        # CRITICAL: Check for other sessions BEFORE unregistering this one
        should_keep_server = False
        if self.server and self.use_server:
            # Check config preference FIRST
            persist = self.config.get("persist_server", False)
            if persist:
                should_keep_server = True
                if signum:
                    print(f"\n[VAF] Server process left running (persist_server=True).")
            else:
                # Prüfe ZUERST ob Server überhaupt noch läuft
                # Wenn Server nicht läuft, müssen wir ihn nicht stoppen
                server_still_running = False
                try:
                    from pathlib import Path
                    from vaf.core.backend import ServerManager
                    server_mgr = ServerManager()
                    pid_file = Path(server_mgr.pid_file)
                    if pid_file.exists():
                        with open(pid_file, 'r', encoding='utf-8') as f:
                            server_pid = int(f.read().strip())
                        if server_mgr._is_process_running(server_pid):
                            # Prüfe ob Server antwortet
                            try:
                                response = requests.get("http://127.0.0.1:8080/health", timeout=1)
                                if response.status_code == 200:
                                    server_still_running = True
                            except:
                                pass
                except Exception:
                    pass
                
                if not server_still_running:
                    # Server läuft nicht mehr, müssen wir nicht stoppen
                    self._unregister_session()
                    # Don't call sys.exit() here - let the process exit naturally
                    # sys.exit() causes SystemExit which is caught by atexit handlers
                    return
                
                # Server läuft noch - prüfe andere Sessions
                other_processes = self._check_other_vaf_processes()
                active_sessions = self._count_active_sessions()
                
                # WICHTIG: Wenn wir die letzte Session sind, sollten wir > 1 zählen
                # Aber nach _unregister_session() wird es 0 sein
                # Deshalb prüfen wir BEVOR wir uns entfernen
                if other_processes > 0 or active_sessions > 1:
                    should_keep_server = True
                    if signum:
                        print(f"\n[VAF] Other active sessions detected (processes: {other_processes}, sessions: {active_sessions - 1}), keeping server running.")
        
        # JETZT unregister diese Session
        self._unregister_session()
        
        if should_keep_server:
            # Don't call sys.exit() - let the process exit naturally
            # This prevents SystemExit exception in atexit handlers
            return
        
        if self.server and self.use_server:
            # Double-check mit Verzögerung
            import time
            time.sleep(0.3)  # Etwas länger für Race Conditions
            
            persist = self.config.get("persist_server", False)
            if not persist:
                # Finale Prüfung
                other_processes = self._check_other_vaf_processes()
                active_sessions = self._count_active_sessions()
                
                # Zusätzlich: Prüfe ob Server noch läuft und antwortet
                server_still_running = False
                try:
                    response = requests.get("http://127.0.0.1:8080/health", timeout=1)
                    if response.status_code == 200:
                        server_still_running = True
                except:
                    pass
                
                # WICHTIG: Nur prüfen, ob andere PROZESSE/SESSIONS existieren
                # server_still_running sollte NICHT verhindern, dass der Server gestoppt wird!
                # Wenn keine anderen Prozesse/Sessions existieren, stoppen wir den Server
                if other_processes > 0 or active_sessions > 0:
                    if signum:
                        print(f"\n[VAF] Other active sessions/processes detected after delay, keeping server running.")
                    return
                
                # Wirklich keine anderen Sessions - Server stoppen (egal ob er läuft oder nicht)
                print(f"\n[VAF] Stopping server ({signum or 'Exit'})...")
                try:
                    self.server.stop_server()
                    self.use_server = False
                except Exception as e:
                    try:
                        if self.server: 
                            self.server.stop_server()
                    except:
                        pass
                    # Fallback: Versuche über ServerManager
                    try:
                        from vaf.core.backend import ServerManager
                        server_mgr = ServerManager()
                        server_mgr.stop_server()
                    except:
                        pass
                
                # For signal handlers, we might need to force exit
                # But don't use sys.exit() as it causes SystemExit in atexit
                if signum:
                    import os
                    os._exit(0)  # Force exit without calling atexit handlers
            else:
                if signum:
                    print(f"\n[VAF] Server process left running (persist_server=True).")
                    # Don't call sys.exit() - let process exit naturally
                    return

    def _load_tools(self):
        """
        Scans vaf/tools/ folder and automatically loads all Tool classes.
        You can drop a new .py file there, and it works!
        """
        import pkgutil
        import importlib
        import inspect
        from vaf.tools.base import BaseTool
        import vaf.tools

        # 1. Iterate over all files in vaf/tools/
        package_path = os.path.dirname(vaf.tools.__file__)
        for _, name, _ in pkgutil.iter_modules([package_path]):
            try:
                # 2. Import the module (e.g. vaf.tools.calendar)
                module = importlib.import_module(f"vaf.tools.{name}")
                
                # 3. Find classes that inherit from BaseTool
                for _, obj in inspect.getmembers(module):
                    if inspect.isclass(obj) and issubclass(obj, BaseTool) and obj is not BaseTool:
                        # 4. Register the tool (Filter primitives to force Sub-Agent usage)
                        instance = obj()
                        
                        # Tools intentionally NOT exposed to the Main Agent.
                        # Rationale: keep the Main Agent high-level; delegate OS/filesystem analysis to sub-agents
                        # (e.g., librarian_agent) to avoid prompt/tool confusion and to keep behavior consistent.
                        MAIN_AGENT_EXCLUDED_TOOLS = [
                            "write_file", "read_file", "list_files", "move_file",  # Filesystem
                            "folder_size",   # Deterministic sizing (prefer via librarian_agent)
                            "bash",           # Shell commands (for build/test)
                            "codesearch",     # Code navigation
                            "batch",          # Parallel operations
                        ]
                        
                        # Check if tool is coder-only (built-in or marked with coder_only=True)
                        is_coder_only = (
                            instance.name in MAIN_AGENT_EXCLUDED_TOOLS or 
                            getattr(instance, 'coder_only', False)
                        )
                        
                        if is_coder_only:
                            continue
                        
                        # Exclude automation management tools when running inside an automation
                        # This prevents automations from creating/managing other automations (infinite loops, unexpected behavior)
                        is_in_automation = os.environ.get("VAF_IN_AUTOMATION", "").strip() in ("1", "true", "yes")
                        if is_in_automation:
                            AUTOMATION_EXCLUDED_TOOLS = [
                                "create_automation",
                                "update_automation",
                                "delete_automation",
                                "list_automations",
                                "read_automation",
                                "restore_automation",
                                "list_trash",
                            ]
                            if instance.name in AUTOMATION_EXCLUDED_TOOLS:
                                continue
                            
                        self.tools[instance.name] = instance
                        # Debug info (only if verbose)
                        # print(f"Loaded tool: {instance.name}")
            except Exception as e:
                pass # Silently ignore broken plugins for stability

    def load_model(self, skip_download_check: bool = False):
        from vaf.cli.ui import UI
        from vaf.core.gpu_detection import get_primary_gpu, _check_cuda_available
        
        if not skip_download_check:
            self.ensure_model_exists()
        
        # Check for NVIDIA GPU without CUDA and offer auto-install
        primary_gpu = get_primary_gpu()
        if primary_gpu and primary_gpu.vendor == "nvidia" and not primary_gpu.compute_available:
            if not _check_cuda_available():
                UI.warning("NVIDIA GPU detected but CUDA not available.")
                UI.print("[yellow]VAF can automatically install CUDA-enabled llama-cpp-python.[/yellow]")
                try:
                    response = input("  Auto-install CUDA support? [Y/n]: ").strip().lower()
                    if response in ('', 'y', 'yes', 'j', 'ja'):
                        UI.event("System", "Installing CUDA support...", style="warning")
                        # Install CUDA-enabled llama-cpp-python inline
                        import subprocess
                        system = platform.system()
                        env = os.environ.copy()
                        pip_cmd = [sys.executable, "-m", "pip", "install", "llama-cpp-python", "--no-cache-dir", "--force-reinstall"]
                        
                        if system == "Windows":
                            env["CMAKE_ARGS"] = "-DGGML_CUDA=on"
                            pip_cmd.extend(["--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cu124"])
                        elif system == "Linux":
                            env["CMAKE_ARGS"] = "-DGGML_CUDA=on"
                            pip_cmd.extend(["--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cu124"])
                        
                        subprocess.check_call(pip_cmd, env=env)
                        UI.event("Success", "CUDA support installed! Restarting model load...", style="success")
                        # Re-check after installation
                        primary_gpu = get_primary_gpu()
                except (KeyboardInterrupt, EOFError):
                    UI.print("[dim]Skipping CUDA installation. Using CPU mode.[/dim]")
                except Exception as e:
                    UI.error(f"CUDA installation failed: {e}")
                    UI.print("[yellow]You can manually install with: vaf install-gpu[/yellow]")
        
        n_gpu = self.config.get("gpu_layers", 99) # Default to max for server
        n_ctx = self.config.get("n_ctx", 8192)

        # Check for Python 3.13 or explicit config to force server
        py_ver = sys.version_info
        is_py313 = py_ver.major == 3 and py_ver.minor == 13
        
        # If explicitly requested or Py3.13 detected, try Server Mode first
        if is_py313 or self.config.get("force_server", False):
            UI.event("System", f"Initializing Standalone Server (Py3.13 / GPU Mode)...", style="warning")
            self.server = ServerManager()
            if self.server.start_server(self.model_path, n_gpu_layers=n_gpu, n_ctx=n_ctx):
                self.use_server = True
                # Show GPU status
                if primary_gpu:
                    if primary_gpu.compute_available:
                        UI.event("Info", f"Using HTTP Backend ({primary_gpu.vendor.upper()} GPU)", style="dim")
                    else:
                        UI.event("Info", f"Using HTTP Backend (CPU Mode - GPU compute not available)", style="yellow")
                else:
                    UI.event("Info", "Using HTTP Backend (CPU Mode)", style="dim")
                return # Success
            else:
                UI.error("Server backend failed. Falling back to internal library (CPU).")
        
        # Fallback to Local Library
        try:
            from llama_cpp import Llama
        except ImportError:
            UI.error("llama-cpp-python not found. Run 'vaf install-gpu' to fix.")
            sys.exit(1)

        UI.event("System", f"Loading Library: {self.filename}...", style="dim")
        
        try:
            self.llm = Llama(
                model_path=self.model_path,
                n_gpu_layers=n_gpu, 
                n_ctx=n_ctx,
                verbose=self.verbose
            )
            UI.event("System", "Model Loaded", style="success")
        except Exception as e:
            UI.error(f"Init failed: {e}")
            self.llm = Llama(model_path=self.model_path, n_gpu_layers=0, n_ctx=n_ctx, verbose=False)
            UI.event("System", "CPU Mode Active", style="warning")

    def ensure_model_exists(self):
        from vaf.cli.ui import UI
        if not os.path.exists(self.models_dir):
            os.makedirs(self.models_dir)

        if not os.path.exists(self.model_path):
            UI.event("System", f"Downloading {self.filename}...", style="warning")
            try:
                # Best practice: huggingface_hub automatically uses tqdm if available
                # This is the standard, OS-independent way (works on Windows, Linux, macOS)
                # tqdm shows: progress bar, speed, ETA, file size
                hf_hub_download(
                    repo_id=self.repo_id,
                    filename=self.filename,
                    local_dir=self.models_dir
                )
                
                UI.event("System", "Download complete", style="success")
            except KeyboardInterrupt:
                # Handle cancellation gracefully (OS-independent)
                UI.event("System", "Download cancelled by user", style="warning")
                # Clean up partial download
                if os.path.exists(self.model_path):
                    try:
                        os.remove(self.model_path)
                    except OSError:
                        pass  # Ignore cleanup errors (OS-independent)
                sys.exit(0)
            except Exception as e:
                UI.error(f"Download failed: {e}")
                sys.exit(1)

    def init_chat(self):
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        os_info = f"{platform.system()} {platform.release()}"
        home_dir = os.path.expanduser("~")
        cwd = os.getcwd()
        
        # Optional project context: VAF.md (search upwards from CWD)
        project_ctx = None
        try:
            from pathlib import Path
            from vaf.core.project_context import load_project_context
            project_ctx = load_project_context(Path(cwd))
        except Exception:
            project_ctx = None

        # Dynamic Identity
        model_file = self.filename.lower()
        if "vq-1" in model_file:
            identity = "Du bist VQ-1, ein hilfreicher Assistent von Veyllo Labs."
        else:
            clean_name = self.filename.replace(".gguf", "").replace("-", " ").title()
            identity = f"You are {clean_name}, an AI in the VAF Framework."

        # Build concise system prompt - ACTION ORIENTED
        system_prompt = f"""{identity}
Time: {now_str} | OS: {os_info} | Home: {home_dir} | CWD: {cwd}

**IMPORTANT**: You have access to the CURRENT DATE and TIME above. Use this information directly for date/time questions. DO NOT use web_search for current date/time - you already have this information!

# LANGUAGE_HINT is updated dynamically on each user message (see _refresh_language_hint).
# It MUST live inside the main system prompt (history[0]) so it survives context compression.
LANGUAGE_HINT: auto (always reply in the user's language; this includes clarification questions)
"""

        # Insert project context early so it shapes behavior consistently
        if project_ctx:
            system_prompt += f"""
## PROJECT CONTEXT (VAF.md)
Loaded from: {project_ctx.path}
{project_ctx.content}

"""
        # Dynamic Tool List
        if self.tools:
            system_prompt += "## YOUR TOOLS\n"
            for name, tool in self.tools.items():
                # Short description only
                desc = tool.description.split('.')[0] if '.' in tool.description else tool.description[:60]
                system_prompt += f"- {name}: {desc}\n"

        system_prompt += """
## CRITICAL: ACTION RULES - CALL TOOLS, DON'T JUST TALK ABOUT THEM!

⚠️ **WARNING**: If you write "I'll use web_search" or say you will use a tool but DON'T actually call it, you have FAILED. Thinking about using a tool is NOT the same as using it!

1. **LANGUAGE**: ALWAYS reply in the user's language (same language as the user's most recent message).
   - This includes clarification questions and missing-info prompts.
   - NEVER switch to English if the user is speaking another language.

2. **TOOL USAGE** - CALL IMMEDIATELY, NO DISCUSSION!
   - "Who is X?" → CALL web_search(X) NOW
   - "Weather in X" → CALL web_search("weather X") NOW
   - "News about X" → CALL web_search(X) NOW
   - "Use the internet" → CALL web_search NOW
   - "How many files" → CALL librarian_agent NOW
   - "How many storage devices" → CALL librarian_agent NOW
   - "What drives" → CALL librarian_agent NOW
   - System/storage questions → CALL librarian_agent NOW
   - "Largest files" / "Biggest files" → CALL librarian_agent NOW
   - "Find files by size" → CALL librarian_agent NOW
   - "Multiple files" / "Several files" → CALL librarian_agent NOW
   - "File analysis" → CALL librarian_agent NOW
   - "Data files" → CALL librarian_agent NOW
   - Complex file queries (sorting, filtering, analysis) → CALL librarian_agent NOW
   - Questions about file contents, sizes, types, locations → CALL librarian_agent NOW
   - "Create website/app/code/program/script" → CALL coding_agent NOW
   - "Build/make a tool/app/website" → CALL coding_agent NOW
   - ANY coding/programming/website/app task → CALL coding_agent NOW
   - "Read URL" → CALL webfetch(url) NOW
   - "Create automation" / "Schedule task" → CALL create_automation NOW
   - Unknown person/topic → CALL web_search NOW
   - and so on... check the tools list!

3. **FORBIDDEN RESPONSES** (will be rejected and retried):
   - "I don't have access to real-time data" → WRONG! Call web_search!
   - "I'll use the web_search tool to..." → WRONG! Just CALL it!
   - "Let me search for that" without actually searching → WRONG!
   - Explaining what you WOULD do instead of DOING it → WRONG!

4. **TYPOS**: User makes typos. Interpret phonetically and use context clues.

5. **BE PROACTIVE**: If user says "use the internet" or asks about anything you don't know → CALL web_search IMMEDIATELY. No discussion, no asking, just DO IT.
   EXCEPTION: If the query requires specific information (location, date, name, etc.) that you don't have → ASK FIRST (see rule 6)

6. **ASK FOR MISSING INFORMATION**: If a request requires specific information you don't have, ASK the user FIRST before searching. Do NOT guess or use generic queries that will return wrong results.
   - "What's the weather?" → ASK (in user's language): DE: "Für welche Stadt oder welchen Ort?" / EN: "Which city or location?" THEN search with location
   - "Weather today" → ASK (in user's language): DE: "Wo (Stadt/Ort) soll ich nachsehen?" / EN: "Where are you located?" THEN search with specific location
   - "Show me events" → ASK: "What kind of events and for which date/location?" THEN search
   - "How many files in folder?" → ASK: "Which folder?" if not clear
   - Better to ask once than to search with wrong/generic information and get useless results!

## CORRECT vs WRONG BEHAVIOR

User: "who is elon musk?"
❌ WRONG: "I will use web_search to find..." (talking about tool without calling)
❌ WRONG: "I don't have access to external data..." (lying)
✅ CORRECT: [CALLS web_search("Elon Musk")] → Then answers with results

User: "use the internet"
❌ WRONG: "I'll perform a web search to find..." (just talking)
✅ CORRECT: [CALLS web_search] → Gets results → Answers

User: "how does the context manager work in cursor?"
❌ WRONG: Long explanation of what you WOULD search for
✅ CORRECT: [CALLS web_search("Cursor IDE context manager")] → Answers with facts

User: "what's the weather today?"
❌ WRONG: [CALLS web_search("weather today")] → Gets generic/wrong results
✅ CORRECT: Ask for city/location IN THE USER'S LANGUAGE → Then [CALLS web_search("weather [location]")]

User: "show me events"
❌ WRONG: [CALLS web_search("events")] → Too generic, wrong results
✅ CORRECT: "What kind of events? And for which date or location?" → Then search with specific info

User: "what are the largest files I have?"
❌ WRONG: [CALLS find_files] → Too simple, can't sort/analyze
❌ WRONG: "I'll use find_files to search..." → Just talking
✅ CORRECT: [CALLS librarian_agent("Find and list the largest files on this system, sorted by size")] → Gets detailed analysis

User: "analyze my data files"
❌ WRONG: [CALLS find_files] → Too simple for analysis
✅ CORRECT: [CALLS librarian_agent("Analyze data files on this system: find all data files, show sizes, types, and locations")] → Gets comprehensive analysis

User: "can you create a daily weather summary for Berlin tomorrow at 21:07 on my Desktop?"
❌ WRONG: [CALLS create_automation] then [CALLS web_search] then [CALLS git_init] → Unnecessary tools, automation prompt should handle it
✅ CORRECT: [CALLS create_automation(name="weather_berlin", prompt="Create a weather summary for Berlin tomorrow as HTML file and save to Desktop", frequency="daily", time="21:07", output_path="Desktop")] → Automation will handle web_search and HTML generation when it runs

**IMPORTANT FOR AUTOMATIONS:**
- When creating automations, the prompt should describe WHAT to do, not HOW
- The automation will execute the prompt later, and the agent will call tools then
- DO NOT call web_search, git_init, or other tools AFTER creating automation
- The automation's prompt should be self-contained (e.g., "Create weather summary for Berlin tomorrow as HTML")
"""
        
        # Add automation-specific instructions if running in automation mode
        if os.environ.get("VAF_IN_AUTOMATION", "").strip() in ("1", "true", "yes"):
            # Check if communication tools are available (telegram, discord, slack, etc.)
            comm_tools = [name for name in self.tools.keys() if any(
                keyword in name.lower() for keyword in ["telegram", "discord", "slack", "whatsapp", "signal", "messaging", "chat", "notify", "mail", "email"]
            )]
            has_comm_tools = len(comm_tools) > 0
            
            system_prompt += """
## AUTOMATION MODE - CRITICAL RULES

You are running as an AUTOMATION. This means:
- You are executing a scheduled task WITHOUT direct user interaction
- By default, there is NO user present to answer questions or provide input
- You MUST complete the task autonomously

**MANDATORY BEHAVIOR IN AUTOMATION MODE:**
1. **NEVER ask for missing information** - You cannot wait for user input (unless communication tools are available, see below)
2. **Use reasonable defaults** when information is missing:
   - Weather without location → Use "current location" or a sensible default (e.g., "Berlin" if context suggests it)
   - Dates without specification → Use "today" or the most recent/relevant date
   - Any missing info → Make the best reasonable assumption based on context
3. **If no reasonable default exists**, clearly state what information is missing in your response, but DO NOT block execution
4. **Complete the task** - Even if some information is missing, produce the best possible output with available information
5. **DO NOT wait** - Automations run unattended and must finish without user interaction

**EXCEPTION: Communication Tools Available**
"""
            if has_comm_tools:
                system_prompt += f"""
✅ **Communication tools detected**: {', '.join(comm_tools)}

If the automation prompt explicitly states that you should wait for user input (e.g., "wait for my answer", "ask the user", "request confirmation"), 
you MAY use these communication tools to send a message and wait for a response. However:
- Only do this if the prompt EXPLICITLY requests it
- Use the communication tool to send your question
- Wait for the response before continuing
- If no response comes within a reasonable time, proceed with defaults

**Example with communication tools:**
- Prompt: "Ask the user via Telegram which city they want weather for, then create a report"
- ✅ CORRECT: Send message via telegram tool asking for city, wait for response, then create report
- Prompt: "Create weather report" (no mention of asking user)
- ✅ CORRECT: Use default location and create report (don't ask, even if telegram is available)
"""
            else:
                system_prompt += """
❌ **No communication tools available** - You cannot wait for user input under any circumstances.

**Example in Automation Mode:**
- User prompt: "Create weather report" (no location specified)
- ✅ CORRECT: Use a default location (e.g., "Berlin" or "current location") and create the report
- ❌ WRONG: Ask "Which city?" - there is no user to answer!

Remember: You are an automation. Your job is to complete tasks autonomously, not to ask questions (unless communication tools are available AND the prompt explicitly requests it).
"""

        self.history = [
            {"role": "system", "content": system_prompt}
        ]

    def _detect_user_language(self, text: str) -> str:
        """
        Very small heuristic for per-turn language pinning.

        Returns:
            - "de" if input looks German
            - "en" if input looks English
            - "<iso639-1>" for other languages if confidently detected (optional)
            - "auto" if unclear/other (let the model mirror the user's language)

        Note: We intentionally avoid adding new dependencies here.
        """
        t = (text or "").strip().lower()
        if not t:
            return "auto"

        # Optional: if user has langid installed, use it to recognize many languages offline.
        # We keep this OPTIONAL (no hard dependency) to stay lightweight/cross-platform.
        try:
            # langid is pure-Python and supports many languages (offline).
            import langid  # type: ignore

            # langid can be noisy on very short strings; prefer it when we have some length.
            if len(t) >= 20 and any(ch.isalpha() for ch in t):
                code, _score = langid.classify(t)
                code = (code or "").strip().lower()
                if code:
                    # Normalize some common variants
                    if code == "iw":  # legacy Hebrew code sometimes seen
                        code = "he"
                    return code
        except Exception:
            pass

        # Strong German markers
        if any(ch in t for ch in ("ä", "ö", "ü", "ß")):
            return "de"

        german_cues = (
            "kannst", "bitte", "wetter", "morgen", "heute", "gestern", "wie ", "was ", "wo ", "warum",
            "ich ", "du ", "wir ", "ihr ", "nicht", "und", "für", "über", "dass", "mach", "erstelle", "zeige",
        )
        if any(cue in t for cue in german_cues):
            return "de"

        # Basic English cues (keep conservative; otherwise leave auto)
        english_cues = (
            "please", "weather", "tomorrow", "today", "yesterday", "how ", "what ", "where ", "why ",
            "i ", "you ", "we ", "they ", "don't", "and", "for", "about", "make", "create", "show",
        )
        if any(cue in t for cue in english_cues):
            return "en"

        return "auto"

    def _detect_mixed_languages(self, text: str) -> list[str]:
        """
        Detects mixed languages in text by splitting it into sections.
        Returns a list of detected languages (without duplicates).
        """
        if not text or len(text.strip()) < 10:
            return []
        
        # Split text into sentences/phrases (at punctuation or line breaks)
        import re
        # Split at punctuation but keep it
        sentences = re.split(r'([.!?]\s+|\.\s+|,\s+|\n+)', text)
        sentences = [s.strip() for s in sentences if s.strip() and len(s.strip()) >= 5]
        
        detected_langs = set()
        
        # Analyze each sentence individually
        for sentence in sentences:
            lang = self._detect_user_language(sentence)
            if lang != "auto":
                detected_langs.add(lang)
        
        # If no sentences detected, try the whole text
        if not detected_langs:
            lang = self._detect_user_language(text)
            if lang != "auto":
                detected_langs.add(lang)
        
        return list(detected_langs)

    def _user_requested_translation(self, user_input: str, detected_response_lang: str) -> bool:
        """
        Intelligently checks if the user explicitly requested a translation,
        WITHOUT using hardcoded words.
        
        Strategy:
        1. Detect mixed languages in user input
        2. If response language is one of the mixed languages (but not the main language),
           then it's probably a translation request
        3. If user input is mainly in language A, but response is in language B,
           AND language B appears in user input → translation request
        
        Returns:
            True if user likely requested a translation, False otherwise
        """
        if not user_input or not detected_response_lang:
            return False
        
        # Detect main language of user input
        main_user_lang = self._detect_user_language(user_input)
        
        # If main language is "auto", we can't be sure
        if main_user_lang == "auto":
            return False
        
        # If response language = main language, it's not a translation request
        if detected_response_lang == main_user_lang:
            return False
        
        # Detect mixed languages in user input
        mixed_langs = self._detect_mixed_languages(user_input)
        
        # If response language appears in mixed languages,
        # but is not the main language → probably translation request
        if detected_response_lang in mixed_langs and detected_response_lang != main_user_lang:
            return True
        
        # Additional check: Split input into words/phrases
        # and check if parts are detected in response language
        import re
        words = re.findall(r'\b\w+\b', user_input)
        
        # Check if there are significant parts in response language
        # (at least 2 words that together are detected in response language)
        if len(words) >= 2:
            # Test phrases of 2-4 words
            for phrase_length in [2, 3, 4]:
                for i in range(len(words) - phrase_length + 1):
                    phrase = ' '.join(words[i:i+phrase_length])
                    phrase_lang = self._detect_user_language(phrase)
                    if phrase_lang == detected_response_lang and phrase_lang != main_user_lang:
                        # Found: A phrase in user input is in response language
                        return True
        
        return False

    def _check_language_mismatch(self, user_input: str, assistant_response: str) -> None:
        """
        Check if the assistant responded in a different language than the user.
        If mismatch detected, add a warning to history prompting the model to translate/reformulate.
        
        IMPORTANT: Intelligently ignores mismatches when the user likely requested a translation.
        
        This helps catch cases where the model ignores LANGUAGE_HINT and responds in English
        when the user asked in Turkish, Spanish, etc.
        """
        if not assistant_response or len(assistant_response.strip()) < 10:
            return  # Too short to reliably detect
        
        user_lang = self._detect_user_language(user_input)
        response_lang = self._detect_user_language(assistant_response)
        
        # Skip if either is "auto" (unclear) or if they match
        if user_lang == "auto" or response_lang == "auto":
            return
        if user_lang == response_lang:
            return
        
        # INTELLIGENT check: Did the user explicitly request a translation?
        # Uses existing language detection, no hardcoded words!
        if self._user_requested_translation(user_input, response_lang):
            # User likely requested a translation - that's OK
            # (Silently ignored - no debug output needed)
            return
        
        # Language names for friendly messages (comprehensive list for langid's 97 languages)
        language_names = {
            # Major European languages
            "en": "English", "de": "German", "fr": "French", "es": "Spanish",
            "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "pl": "Polish",
            "ru": "Russian", "uk": "Ukrainian", "sv": "Swedish", "no": "Norwegian",
            "da": "Danish", "fi": "Finnish", "cs": "Czech", "ro": "Romanian",
            "hu": "Hungarian", "el": "Greek", "tr": "Turkish", "bg": "Bulgarian",
            "hr": "Croatian", "sr": "Serbian", "sk": "Slovak", "sl": "Slovenian",
            "et": "Estonian", "lv": "Latvian", "lt": "Lithuanian", "ga": "Irish",
            "mt": "Maltese", "is": "Icelandic", "mk": "Macedonian", "sq": "Albanian",
            "bs": "Bosnian", "ca": "Catalan", "eu": "Basque", "gl": "Galician",
            # Asian languages
            "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "hi": "Hindi",
            "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "ms": "Malay",
            "tl": "Filipino", "my": "Burmese", "km": "Khmer", "lo": "Lao",
            "bn": "Bengali", "ta": "Tamil", "te": "Telugu", "ml": "Malayalam",
            "kn": "Kannada", "gu": "Gujarati", "pa": "Punjabi", "ur": "Urdu",
            "ne": "Nepali", "si": "Sinhala", "ka": "Georgian",
            "hy": "Armenian", "az": "Azerbaijani", "kk": "Kazakh", "ky": "Kyrgyz",
            "uz": "Uzbek", "mn": "Mongolian", "bo": "Tibetan",
            # Middle Eastern & African languages
            "ar": "Arabic", "he": "Hebrew", "fa": "Persian", "ps": "Pashto",
            "sw": "Swahili", "am": "Amharic", "zu": "Zulu", "af": "Afrikaans",
            "so": "Somali", "ha": "Hausa", "yo": "Yoruba", "ig": "Igbo",
            # Other languages
            "eo": "Esperanto", "la": "Latin", "cy": "Welsh", "br": "Breton",
        }
        
        user_lang_name = language_names.get(user_lang, user_lang.upper())
        response_lang_name = language_names.get(response_lang, response_lang.upper())
        
        # Sofortige, direkte Warnung in der Nutzersprache
        if user_lang == "de":
            warning = (
                f"⚠️ **Sprach-Mismatch erkannt**: Du hast auf {response_lang_name} geantwortet, "
                f"aber der Nutzer spricht {user_lang_name}. "
                f"Bitte übersetze deine Antwort sofort ins {user_lang_name} oder formuliere sie auf {user_lang_name} um."
            )
        elif user_lang in language_names:
            # Try to generate a warning in the user's language (simple approach)
            warning = (
                f"⚠️ **Language mismatch detected**: You responded in {response_lang_name}, "
                f"but the user is speaking {user_lang_name}. "
                f"Please translate your response immediately to {user_lang_name} or reformulate it in {user_lang_name}."
            )
        else:
            # Fallback: bilingual
            warning = (
                f"⚠️ **Language mismatch**: You answered in {response_lang_name}, "
                f"but user speaks {user_lang_name}. "
                f"Please respond immediately in {user_lang_name}."
            )
        
        # Insert immediate warning into history (before the response!)
        # This forces the model to correct the response immediately
        self.history.append({
            "role": "system",
            "content": warning
        })
        
        from vaf.cli.ui import UI
        UI.event("Language", f"Mismatch: User={user_lang_name}, Response={response_lang_name} - Auto-correction requested", style="warning")

    def _refresh_language_hint(self, user_input: str) -> None:
        """
        Update LANGUAGE_HINT inside the main system prompt (history[0]).
        This ensures the current response language is consistently enforced,
        including in "no workflow match" situations and after context compression.
        """
        if not self.history or self.history[0].get("role") != "system":
            return

        # Optional user override via config: language = "auto" | "de" | "en"
        configured = (self.config.get("language", "auto") or "auto").strip().lower()
        if configured in ("de", "en"):
            lang = configured
        else:
            lang = self._detect_user_language(user_input)

        # Human-friendly names for common languages (comprehensive list for langid's 97 languages)
        language_names = {
            # Major European languages
            "en": "English",
            "de": "German",
            "fr": "French",
            "es": "Spanish",
            "it": "Italian",
            "pt": "Portuguese",
            "nl": "Dutch",
            "pl": "Polish",
            "ru": "Russian",
            "uk": "Ukrainian",
            "sv": "Swedish",
            "no": "Norwegian",
            "da": "Danish",
            "fi": "Finnish",
            "cs": "Czech",
            "ro": "Romanian",
            "hu": "Hungarian",
            "el": "Greek",
            "tr": "Turkish",
            "bg": "Bulgarian",
            "hr": "Croatian",
            "sr": "Serbian",
            "sk": "Slovak",
            "sl": "Slovenian",
            "et": "Estonian",
            "lv": "Latvian",
            "lt": "Lithuanian",
            "ga": "Irish",
            "mt": "Maltese",
            "is": "Icelandic",
            "mk": "Macedonian",
            "sq": "Albanian",
            "bs": "Bosnian",
            "ca": "Catalan",
            "eu": "Basque",
            "gl": "Galician",
            # Asian languages
            "ja": "Japanese",
            "ko": "Korean",
            "zh": "Chinese",
            "hi": "Hindi",
            "th": "Thai",
            "vi": "Vietnamese",
            "id": "Indonesian",
            "ms": "Malay",
            "tl": "Filipino",
            "my": "Burmese",
            "km": "Khmer",
            "lo": "Lao",
            "bn": "Bengali",
            "ta": "Tamil",
            "te": "Telugu",
            "ml": "Malayalam",
            "kn": "Kannada",
            "gu": "Gujarati",
            "pa": "Punjabi",
            "ur": "Urdu",
            "ne": "Nepali",
            "si": "Sinhala",
            "ka": "Georgian",
            "hy": "Armenian",
            "az": "Azerbaijani",
            "kk": "Kazakh",
            "ky": "Kyrgyz",
            "uz": "Uzbek",
            "mn": "Mongolian",
            "bo": "Tibetan",
            # Middle Eastern & African languages
            "ar": "Arabic",
            "he": "Hebrew",
            "fa": "Persian",
            "ps": "Pashto",
            "sw": "Swahili",
            "am": "Amharic",
            "zu": "Zulu",
            "af": "Afrikaans",
            "so": "Somali",
            "ha": "Hausa",
            "yo": "Yoruba",
            "ig": "Igbo",
            # Other languages
            "eo": "Esperanto",
            "la": "Latin",
            "cy": "Welsh",
            "br": "Breton",
        }

        if lang == "de":
            hint = "LANGUAGE_HINT: de (Antworte auf Deutsch. Stelle Rückfragen ebenfalls auf Deutsch.)"
        elif lang == "en":
            hint = "LANGUAGE_HINT: en (Answer in English. Ask clarifying questions in English.)"
        elif lang and lang != "auto":
            name = language_names.get(lang, lang)
            hint = f"LANGUAGE_HINT: {lang} (Answer in {name}. Ask clarifying questions in the same language.)"
        else:
            hint = "LANGUAGE_HINT: auto (always reply in the user's language; this includes clarification questions)"

        content = str(self.history[0].get("content") or "")
        if "LANGUAGE_HINT:" in content:
            content = re.sub(r"^LANGUAGE_HINT:.*$", hint, content, flags=re.MULTILINE)
        else:
            # Put it near the top so it has high priority
            content = content.rstrip() + "\n" + hint + "\n"

        self.history[0]["content"] = content

    def get_token_usage(self):
        """Calculates current token usage from history."""
        # Simple approximation if server mode (1 char ~= 0.3 tokens)
        if self.use_server:
            # Naive estimation without tokenizer
            text = "".join([str(m.get("content", "")) for m in self.history if m.get("content")])
            return int(len(text) * 0.4), self.config.get("n_ctx", 8192)
            
        if not self.llm: return 0, 8192
        
        try:
            total_tokens = 0
            for msg in self.history:
                content = msg.get("content", "")
                if content:
                    tokens = self.llm.tokenize(content.encode("utf-8"), special=False)
                    total_tokens += len(tokens)
            total_tokens += 100 
            return total_tokens, self.config.get("n_ctx", 8192)
        except:
            return 0, 8192

    def _generate_summary(self, messages: list) -> str:
        """
        Generates a concise narrative summary of the provided messages using the LLM.
        """
        if not messages:
            return ""

        # Prepare text to summarize
        conversation_text = ""
        for msg in messages:
            role = msg.get("role", "unknown")
            content = str(msg.get("content", ""))
            # Skip large tool outputs in summary generation to save tokens
            if role == "tool" and len(content) > 500:
                content = content[:500] + "... [truncated]"
            conversation_text += f"{role.upper()}: {content}\n"

        prompt = (
            f"Summarize the following conversation segment into 2-3 concise sentences.\n"
            f"Focus on the user's goal, key actions taken, and important outcomes.\n"
            f"Ignore minor details.\n\n"
            f"{conversation_text}\n\n"
            f"Summary (max 3 sentences):"
        )

        try:
            # Use a separate, low-temp call for summarization
            # Construct a temporary history for this specific task
            temp_history = [{"role": "user", "content": prompt}]
            
            content = ""
            if self.use_server:
                payload = {
                    "messages": temp_history, 
                    "max_tokens": 200, 
                    "temperature": 0.3,
                    "stream": False
                }
                try:
                    res = requests.post("http://127.0.0.1:8080/v1/chat/completions", json=payload, timeout=30).json()
                    content = res['choices'][0]['message']['content']
                except Exception:
                    pass
            elif self.llm:
                 output = self.llm.create_chat_completion(
                     messages=temp_history,
                     max_tokens=200,
                     temperature=0.3
                 )
                 content = output['choices'][0]['message']['content']
            
            return content.strip()
        except Exception as e:
            from vaf.cli.ui import UI
            UI.event("Debug", f"Summarization failed: {e}", style="dim")
            return ""

    def manage_context(self):
        """
        Cursor-Style Context Management
        
        Features:
        - Intent Context: Tracks user goals
        - State Context: Tracks project state (files, errors)
        - Full Archive: Complete history saved for restoration
        - Smart Compression: Lossy but preserves critical info
        - Narrative Summarization: LLM summarizes old messages (1+1.. -> 5+3..)
        
        Use /restore to recover full context after compression.
        """
        from vaf.cli.ui import UI
        from vaf.core.context import ContextManager
        
        # Initialize context manager if not exists
        if not hasattr(self, '_context_manager'):
            max_tokens = self.config.get("n_ctx", 8192)
            self._context_manager = ContextManager(max_tokens=max_tokens)
        
        cm = self._context_manager
        
        # Check if compression needed
        if not cm.should_compress(self.history):
            return

        # LLM-based Summarization Logic
        # We want to summarize the "middle" chunk that is about to be compressed away.
        # compress() keeps: history[0] (System) and history[-recent_memory_size:] (Recent)
        # So we summarize: history[1 : -recent_memory_size]
        
        recent_count = cm.recent_memory_size
        if len(self.history) > recent_count + 2:
            msgs_to_summarize = self.history[1:-recent_count]
            
            if msgs_to_summarize:
                UI.event("Context", f"Summarizing {len(msgs_to_summarize)} old messages...", style="info")
                
                # If we already have a previous summary, include it contextually
                previous_summary = cm.state.narrative_summary
                
                # Create a synthetic message block to summarize
                # If there's a previous summary, we essentially "re-roll" it with the new old messages
                messages_for_llm = msgs_to_summarize
                if previous_summary:
                    # Prepend previous summary as a context note
                    messages_for_llm = [{"role": "system", "content": f"Previous Summary: {previous_summary}"}] + msgs_to_summarize
                
                new_summary = self._generate_summary(messages_for_llm)
                
                if new_summary:
                    cm.state.narrative_summary = new_summary
                    UI.event("Context", "Summary updated.", style="dim")

        # Compress with Cursor-style algorithm (now includes the new narrative_summary)
        self.history = cm.compress(self.history)
    
    def restore_context(self) -> bool:
        """Restore full context from archive."""
        from vaf.cli.ui import UI
        from vaf.core.context import ContextManager
        
        if not hasattr(self, '_context_manager'):
            UI.error("No context manager initialized.")
            return False
        
        restored = self._context_manager.restore_latest()
        if restored:
            self.history = restored
            tokens = self._context_manager.estimate_tokens(self.history)
            UI.event("Context", f"Restored! {len(self.history)} messages, {tokens} tokens", style="success")
            return True
        else:
            UI.error("No archived context found.")
            return False
    
    def get_context_status(self) -> dict:
        """Get current context status for UI display."""
        from vaf.core.context import ContextManager
        
        if not hasattr(self, '_context_manager'):
            max_tokens = self.config.get("n_ctx", 8192)
            self._context_manager = ContextManager(max_tokens=max_tokens)
        
        return self._context_manager.get_status(self.history)

    def analyze_intent(self, user_input):
        """
        Determines the optimal temperature (0.2 - 0.9) for the user's request.
        Uses a quick, lightweight inference.
        """
        try:
            prompt = (
                f"You are a meta-cognitive strategist. Your ONLY job is to decide the creativity level (Temperature) for the Main Agent.\n"
                f"Analyze the User Request and output ONLY the float value (0.2 - 0.9).\n"
                f"Guidelines:\n"
                f"- 0.2: Factual queries, Math, Logic, or when TOOLS (Web Search, Filesystem) are needed.\n"
                f"- 0.5: General conversation, Explanations.\n"
                f"- 0.9: Creative writing, Brainstorming.\n"
                f"Request: {user_input}\n"
                f"Output ONLY the float value."
            )
            
            # Quick Inference
            messages = [{"role": "user", "content": prompt}]
            
            content = ""
            if self.use_server:
                 # Sub-Agent Mode: Full thinking capacity with 120s timeout
                 payload = {"messages": messages, "max_tokens": 1024, "temperature": 0.2}
                 try:
                     res = requests.post("http://127.0.0.1:8080/v1/chat/completions", json=payload, timeout=120).json()
                     content = res['choices'][0]['message']['content']
                 except: pass
            
            # Parse Float
            match = re.search(r"\d+(\.\d+)?", content)
            if match:
                temp = float(match.group())
                return max(0.2, min(0.9, temp))
                
            return 0.7
            
        except Exception as e:
             from vaf.cli.ui import UI
             UI.event("Debug", f"Intent Analysis Failed: {e}", style="dim")
             return 0.7
    
    def analyze_workflow(self, user_input):
        """
        Brain-based workflow selection - understands languages, not just hardcoded patterns.
        Returns the best matching workflow template ID or None.
        """
        try:
            from vaf.workflows.templates import WORKFLOW_TEMPLATES, list_templates
            
            # Get available workflows
            available_workflows = list_templates()
            workflow_list = "\n".join([
                f"- {w['id']}: {w['name']} - {w['description']}"
                for w in available_workflows
            ])
            
            # Use same logic as analyze_intent (which works!)
            # Build dynamic examples from available workflows
            workflow_ids = list(WORKFLOW_TEMPLATES.keys())
            examples = []
            for wf_id in workflow_ids:
                wf = WORKFLOW_TEMPLATES[wf_id]
                # Get trigger examples from the workflow
                triggers = wf.get("triggers", [])[:3]  # First 3 triggers as examples
                if triggers:
                    examples.append(f"- {', '.join(triggers)} → {wf_id}")
            
            prompt = (
                f"You are a workflow classifier. Your ONLY job is to match the user request to a workflow ID.\n"
                f"Available Workflows:\n{workflow_list}\n\n"
                f"Guidelines:\n"
                f"- Match by INTENT, not exact words (works in ANY language!)\n"
                f"- IGNORE typos and spelling errors - understand the intent\n"
                f"- If request contains TIME (e.g., 'at 21:27', 'um 21:27', 'always at', 'immer um', 'daily at', 'täglich um') → create_scheduled_task\n"
                f"- If request asks to schedule/automate something → create_scheduled_task\n"
                f"- If request is about folder/file locations → none (use librarian_agent, not web_lookup)\n"
                f"- If request is a simple web search → web_lookup\n"
                f"- Examples:\n" + "\n".join(examples) + "\n"
                f"- If no match → none\n\n"
                f"Request: {user_input}\n"
                f"Output ONLY the workflow ID or 'none'."
            )
            
            # Quick Inference (same as analyze_intent)
            messages = [{"role": "user", "content": prompt}]
            
            content = ""
            if self.use_server:
                # Sub-Agent Mode: Full thinking capacity with 120s timeout (same as analyze_intent)
                payload = {"messages": messages, "max_tokens": 1024, "temperature": 0.2}
                try:
                    res = requests.post("http://127.0.0.1:8080/v1/chat/completions", json=payload, timeout=120).json()
                    content = res['choices'][0]['message']['content']
                except Exception as e:
                    # Fall back to pattern matching on error
                    from vaf.workflows.selector import WorkflowSelector
                    selector = WorkflowSelector()
                    result = selector.select(user_input)
                    if result and result.matched and result.confidence >= 0.5:
                        return result.template_id
                    return None
            
            # Parse workflow ID (same pattern as analyze_intent parses float)
            # Dynamically build regex from all available workflow IDs (plug and play!)
            workflow_ids_pattern = '|'.join(re.escape(wf_id) for wf_id in WORKFLOW_TEMPLATES.keys())
            match = re.search(rf'\b({workflow_ids_pattern})\b', content.lower())
            if match:
                workflow_id = match.group(1)
                if workflow_id in WORKFLOW_TEMPLATES:
                    return workflow_id
            
            # Check for "none" response
            if "none" in content.lower():
                return None
            
            # If we couldn't parse, fall back to pattern matching
            from vaf.workflows.selector import WorkflowSelector
            selector = WorkflowSelector()
            result = selector.select(user_input)
            if result and result.matched and result.confidence >= 0.5:
                return result.template_id
            return None
            
        except Exception as e:
            from vaf.cli.ui import UI
            UI.event("Debug", f"Workflow Analysis Failed: {e}", style="dim")
            return None

    def _try_workflow(self, user_input: str, stream_callback=None) -> str:
        """
        Check if user input matches a workflow template and execute if so.
        
        Returns:
            Result string if workflow executed, None if no match
        """
        from vaf.cli.ui import UI
        import os
        
        # Skip workflow matching in automation mode - automations should execute prompts directly
        # Automations have their own workflow_steps if they need multi-step execution
        if os.environ.get("VAF_IN_AUTOMATION", "").strip() in ("1", "true", "yes"):
            return None
        
        try:
            from vaf.workflows import WorkflowSelector, WorkflowEngine, create_workflow
            
            # Check if workflows are enabled (can be disabled in config)
            if not self.config.get("workflows_enabled", True):
                return None
            
            # BRAIN-BASED WORKFLOW SELECTION (multi-language support!)
            # Instead of hardcoded pattern matching, use LLM to understand intent in ANY language
            from vaf.cli.ui import UI as UI_Class
            with UI_Class.console.status("[bold cyan](O_O)  Step 1/2: Analyzing workflow match...[/bold cyan]", spinner="dots"):
                workflow_id = self.analyze_workflow(user_input)
            
            if not workflow_id:
                # No workflow match - fall back to LLM agent
                UI.event("Step 1/2", "Workflow", style="dim")
                return None
            
            UI.event("Brain", f"Workflow matched: {workflow_id}", style="bold cyan")
            
            # Get the matched template
            from vaf.workflows.templates import get_template
            template = get_template(workflow_id)
            if not template:
                return None
            
            UI.event("Workflow", f"Brain matched: {template['name']} (multi-language support!)", style="bold cyan")
            
            # Extract variables using WorkflowSelector (pattern matching + fallback)
            from vaf.cli.ui import UI
            UI.event("Brain", "Extracting variables from user input...", style="dim")
            
            # Use WorkflowSelector to extract variables
            selector = WorkflowSelector()
            variables, missing = selector._extract_variables(user_input, template)
            
            # Get required variables from template
            template_variables = template.get("variables", {})
            required_vars = set(template_variables.keys())
            defaults = template.get("defaults", {})
            
            # Determine which variables are still missing
            missing = [var for var in required_vars if var not in variables]
            
            # Fill in defaults for missing variables
            for var_name in missing:
                if var_name in defaults:
                    variables[var_name] = defaults[var_name]
                    missing.remove(var_name)
            
            # Debug: Log extracted variables
            if variables:
                UI.event("Debug", f"Extracted variables: {list(variables.keys())}", style="dim")
            
            # If variables are still missing, use selector as fallback
            if missing:
                UI.event("Debug", f"Missing variables (using fallback): {missing}", style="dim")
                selector = WorkflowSelector()
                missing_copy = list(missing)  # Use copy to avoid modification during iteration
                for var_name in missing_copy:
                    extracted = selector._extract_value(user_input, var_name, template_variables.get(var_name, ""))
                    if extracted:
                        variables[var_name] = extracted
                        missing.remove(var_name)
                    elif var_name in defaults:
                        variables[var_name] = defaults[var_name]
                        missing.remove(var_name)
                
                # If still missing critical variables, fall back to LLM
                if missing:
                    UI.event("Workflow", f"Missing inputs: {', '.join(missing)} - using defaults or falling back", style="warning")
                    # Use defaults for remaining missing variables
                    for var_name in missing:
                        if var_name in defaults:
                            variables[var_name] = defaults[var_name]
                            missing.remove(var_name)
            
            # Create a SelectorResult for compatibility
            from vaf.workflows.selector import SelectorResult
            result = SelectorResult(
                matched=True,
                template_id=workflow_id,
                template=template,
                confidence=0.9,  # High confidence when brain matches
                variables=variables,
                missing_variables=missing,
                suggestion=None,
            )
            
            template = result.template
            if result.missing_variables:
                UI.event("Workflow", f"Missing inputs: {', '.join(result.missing_variables)}", style="warning")
                # For now, fall back to LLM if variables are missing
                # Future: Could prompt user for missing values
                return None
            
            # Build workflow steps from template
            from vaf.workflows.engine import create_workflow as build_steps
            steps = build_steps(template)
            
            # Create engine with ALL tools (including coder-only tools)
            # Workflows need access to write_file, read_file, coding_agent, etc.
            all_tools = {**self.tools}
            
            # Load additional tools that are normally coder-only
            try:
                from vaf.tools.filesystem import WriteFileTool, ReadFileTool, ListFilesTool, MoveFileTool
                from vaf.tools.bash import BashTool
                from vaf.tools.coder import CodingAgentTool
                
                all_tools["write_file"] = WriteFileTool()
                all_tools["read_file"] = ReadFileTool()
                all_tools["list_files"] = ListFilesTool()
                all_tools["move_file"] = MoveFileTool()
                all_tools["bash"] = BashTool()
                all_tools["coding_agent"] = CodingAgentTool()  # WICHTIG für Website-Workflow!
            except ImportError as e:
                UI.event("Warning", f"Could not load tools: {e}", style="warning")
            
            # Progress callback for streaming
            def workflow_callback(event, step, current, total):
                if stream_callback and event == "success":
                    stream_callback(f"\n✓ Step {current}/{total}: {step.tool}\n")
            
            engine = WorkflowEngine(all_tools, callback=workflow_callback)
            
            # Execute workflow
            # Get defaults from template if available
            template_defaults = result.template.get("defaults", {}) if result.template else {}
            # Set defaults on engine (not as parameter - execute() doesn't accept defaults)
            engine._workflow_defaults = template_defaults
            
            # Execute workflow (without defaults parameter)
            workflow_result = engine.execute(steps, variables=result.variables)
            
            if workflow_result.success:
                # Format the final output
                final_output = str(workflow_result.final_output or "Done.")

                # UX: auto-open outputs (report/files) and/or project folders
                try:
                    import os
                    from pathlib import Path
                    from vaf.core.config import Config
                    from vaf.core.platform import Platform

                    noninteractive = os.environ.get("VAF_NONINTERACTIVE", "").strip().lower() in ("1", "true", "yes")
                    if not noninteractive and bool(Config.get("ux_auto_open_outputs")):
                        out_file = str(workflow_result.outputs.get("output_file") or "")
                        if out_file:
                            p = Path(out_file)
                            # Open HTML reports in browser, otherwise open folder/file in explorer
                            if p.suffix.lower() in (".html", ".htm") and p.exists():
                                # Local HTML report: open non-incognito for maximum compatibility (file:// + private mode can be flaky across browsers)
                                Platform.open_url(p.as_uri(), incognito=False)
                            else:
                                # Prefer opening folder for files
                                if p.exists() and p.is_file():
                                    Platform.open_path(p.parent)
                                elif p.exists():
                                    Platform.open_path(p)
                except Exception:
                    pass
                
                # Check if coding_agent was used and if tasks might be incomplete
                coding_agent_used = any(step.tool == "coding_agent" for step in steps)
                incomplete_tasks_hint = ""
                project_path_hint = ""
                
                if coding_agent_used:
                    # Extract project path from output if available
                    path_match = re.search(r'`([^`]+)`', final_output)
                    if path_match:
                        project_path_hint = path_match.group(1)
                    
                    # Check if the output indicates incomplete tasks
                    if "Task Partially Complete" in final_output or "Tasks: 0/" in final_output or "Remaining tasks" in final_output:
                        # Tasks are incomplete - the Main Agent should intervene and help the coding agent
                        if project_path_hint:
                            # Don't just suggest - ACTIVELY call coding_agent again
                            UI.event("Workflow", "Detected incomplete tasks - Main Agent intervening", style="warning")
                            
                            # Call coding_agent again with explicit instructions
                            from vaf.tools.coder import CodingAgentTool
                            coding_tool = all_tools.get("coding_agent")
                            if coding_tool:
                                # Extract current task status from output
                                task_match = re.search(r'Tasks:\s*(\d+)/(\d+)', final_output)
                                completed_tasks = int(task_match.group(1)) if task_match else 0
                                total_tasks = int(task_match.group(2)) if task_match else 0
                                
                                # Extract remaining tasks from output
                                remaining_tasks = []
                                if "Remaining tasks" in final_output or "Tasks:" in final_output:
                                    # Try to extract task list from output
                                    task_list_match = re.search(r'Tasks:.*?\n(.*?)(?:\n\n|\n─)', final_output, re.DOTALL)
                                    if task_list_match:
                                        task_text = task_list_match.group(1)
                                        # Extract incomplete tasks (lines starting with ○ or ⠴)
                                        for line in task_text.split('\n'):
                                            if '○' in line or '⠴' in line or '⠦' in line or '⠧' in line or '⠇' in line:
                                                # Extract task text (remove status icons)
                                                task_text_clean = re.sub(r'[○⬤⠴⠦⠧⠇]', '', line).strip()
                                                if task_text_clean:
                                                    remaining_tasks.append(task_text_clean)
                                
                                # Build context-aware continue task
                                context_info = f"**Current Status:** {completed_tasks}/{total_tasks} tasks completed.\n"
                                if remaining_tasks:
                                    context_info += f"**Remaining Tasks:**\n" + "\n".join(f"- {t}" for t in remaining_tasks[:5]) + "\n\n"
                                else:
                                    context_info += f"**Note:** Check the TODO list in the project to see remaining tasks.\n\n"
                                
                                continue_task = (
                                    f"Continue working on the EXISTING project at: {project_path_hint}\n\n"
                                    f"{context_info}"
                                    f"**IMPORTANT:**\n"
                                    f"- Read the existing files to understand what's already done\n"
                                    f"- Check the TODO list status (some tasks may already be completed)\n"
                                    f"- Continue from where you left off - do NOT start over\n"
                                    f"- Complete ONLY the remaining tasks\n"
                                    f"- Replace ALL remaining placeholders with real content\n"
                                    f"- Use the original request '{user_input}' as context for content\n"
                                    f"- Do NOT recreate files that are already done - only modify what's needed"
                                )
                                
                                UI.event("Workflow", "↻ Continuing work on remaining tasks...", style="info")
                                continuation_result = coding_tool.run(task=continue_task, project_path=project_path_hint)
                                
                                # Check if NOW everything is complete
                                if "ALL TASKS COMPLETED" in continuation_result or "Tasks: 5/5" in continuation_result:
                                    final_output = continuation_result
                                    incomplete_tasks_hint = ""
                                else:
                                    # Still incomplete after retry - just report it
                                    incomplete_tasks_hint = (
                                        f"\n\n⚠️ **Note**: Some tasks may still be incomplete. "
                                        f"The project is at: `{project_path_hint}`\n"
                                        f"You can continue working on it or ask for specific changes."
                                    )
                                    final_output += f"\n\n---\n{continuation_result}"
                            else:
                                incomplete_tasks_hint = (
                                    f"\n\n⚠️ **Note**: The coding agent has incomplete tasks. "
                                    f"To complete all remaining tasks, use:\n"
                                    f"`coding_agent(task=\"continue and complete all remaining tasks\", project_path=\"{project_path_hint}\")`"
                                )
                        else:
                            incomplete_tasks_hint = (
                                f"\n\n⚠️ **Note**: The coding agent has incomplete tasks. "
                                f"Check the output above for the project path and continue working on it."
                            )
                    else:
                        incomplete_tasks_hint = ""
                
                # Extract project path and create clickable link
                project_link = ""
                if project_path_hint:
                    from pathlib import Path
                    project_path_obj = Path(project_path_hint)
                    if project_path_obj.exists():
                        project_link = f"[link=file:///{project_path_hint.replace(chr(92), '/')}]{project_path_hint}[/link]"

                    # UX: auto-open created project folder in file explorer
                    try:
                        import os
                        from vaf.core.config import Config
                        from vaf.core.platform import Platform
                        noninteractive = os.environ.get("VAF_NONINTERACTIVE", "").strip().lower() in ("1", "true", "yes")
                        if not noninteractive and bool(Config.get("ux_auto_open_outputs")):
                            Platform.open_path(project_path_obj)
                    except Exception:
                        pass
                
                # Detect user language from input (light heuristic)
                user_lang = "de" if any(
                    w in user_input.lower()
                    for w in [
                        "kannst", "erstelle", "mach", "bitte", "für", "über", "recherche", "analyse",
                        "deutsch", "ja", "nein", "wetter", "webseite"
                    ]
                ) else "en"

                # If this workflow produced a saved report/file, prefer a direct "saved at" message over "project ready".
                report_saved_msg = None
                if workflow_id in ("deep_research",) or ("output_file" in workflow_result.outputs and "saved" in workflow_result.outputs):
                    try:
                        from pathlib import Path
                        out_file = str(workflow_result.outputs.get("output_file") or "")
                        p = Path(out_file) if out_file else None
                        link = ""
                        if p and p.exists():
                            ps = str(p)
                            link = f"[link=file:///{ps.replace(chr(92), '/')}]{ps}[/link]"
                        target = link or out_file or str(workflow_result.outputs.get("saved") or "")
                        if user_lang == "de":
                            report_saved_msg = f"**✓ Fertig!** Report gespeichert:\n{target}"
                        else:
                            report_saved_msg = f"**✓ Done!** Report saved:\n{target}"
                    except Exception:
                        pass
                
                # Build completion message based on language and completion status
                if report_saved_msg:
                    # Use the report saved message instead of generic completion
                    completion_msg = report_saved_msg
                elif incomplete_tasks_hint:
                    # Tasks still incomplete even after intervention
                    if user_lang == "de":
                        completion_msg = "**Möchtest du, dass ich hier noch etwas verbessere?** Sag mir einfach, was du ändern möchtest!"
                    else:
                        completion_msg = "**Would you like me to make any changes or improvements?** Just let me know what you'd like to modify!"
                else:
                    # Tasks completed successfully
                    if user_lang == "de":
                        if project_link:
                            completion_msg = f"**✓ Fertig!** Dein Projekt ist bereit:\n{project_link}\n\n**Möchtest du, dass ich hier noch etwas verbessere?**"
                        else:
                            completion_msg = "**✓ Fertig!** Dein Projekt ist bereit.\n\n**Möchtest du, dass ich hier noch etwas verbessere?**"
                    else:
                        if project_link:
                            completion_msg = f"**✓ Done!** Your project is ready:\n{project_link}\n\n**Would you like me to improve anything?**"
                        else:
                            completion_msg = "**✓ Done!** Your project is ready.\n\n**Would you like me to improve anything?**"
                
                output_parts = [
                    final_output,
                    incomplete_tasks_hint,
                    "\n\n---\n",
                    completion_msg
                ]
                return "".join(output_parts)
            else:
                # Workflow failed - let LLM handle it
                UI.event("Workflow", f"Failed: {workflow_result.error}", style="error")
                return None
                
        except ImportError:
            # Workflows module not available
            return None
        except Exception as e:
            from vaf.cli.ui import UI
            UI.event("Debug", f"Workflow error: {e}", style="dim")
            return None

    def chat_step(self, user_input: str, stream_callback=None, auto_retry=False, skip_input=False):
        from vaf.cli.ui import UI
        
        if not self.llm and not self.use_server:
            UI.error("Agent not initialized. Run 'vaf run' first.")
            return

        # Keep language pinned to the user's most recent message.
        # This must happen early so it affects workflow selection + normal chat replies.
        if not skip_input:
            self._refresh_language_hint(user_input)

        # 0. Context Management (Trim/Summarize) - BEFORE adding user input
        self.manage_context()
        
        # ═══════════════════════════════════════════════════════════════════════
        # WORKFLOW ENGINE: ENABLED - Try to match workflow templates first
        # ═══════════════════════════════════════════════════════════════════════
        # If a workflow matches (confidence >= 50%), execute it automatically
        # Otherwise, fall back to LLM agent for flexible handling
        # Workflows provide structured, multi-step pipelines for common tasks
        
        if not skip_input:
            # Try workflow matching BEFORE adding to history
            workflow_result = self._try_workflow(user_input, stream_callback)
            if workflow_result:
                # Workflow executed successfully - return result
                return workflow_result
            # No workflow match or workflow failed - continue with LLM agent
        
        # Snapshot history (before adding user input if not skipped)
        history_snapshot_len = len(self.history)
        
        if not skip_input:
            self.history.append({"role": "user", "content": user_input})
            # 0.5. Context Management AGAIN - AFTER adding user input (in case it pushed us over limit)
            self.manage_context()
            # Re-apply after potential compression to ensure the hint stays in history[0].
            self._refresh_language_hint(user_input)
        else:
            pass

        # 1. Adaptive Temperature Check
        target_temp = self.config.get("temperature", 0.7)
        if not auto_retry and not skip_input:
             # Sub-Agent Intent Analysis (can take time)
             from vaf.cli.ui import UI as UI_Class
             with UI_Class.console.status("[bold cyan](O_O)  Step 2/2: Analyzing Intent...[/bold cyan]", spinner="dots"):
                 dynamic_temp = self.analyze_intent(user_input)
             
             UI.event("Step 2/2", f"Adaptive State: Temperature set to {dynamic_temp} based on intent.", style="dim")
             target_temp = dynamic_temp

        UI.event("Agent", "Thinking...", style="dim")
        
        retries = 0
        MAX_RETRIES = 5
        
        # State for formatting
        is_reasoning = False
        
        # Retry counter for empty responses
        empty_retry_count = 0
        current_temp = target_temp
        
        # Initialize response variables before loop to avoid UnboundLocalError
        full_response = ""
        full_content = ""
        full_reasoning = ""
        clean_content = ""
        streaming_tools = {}
        tool_calls_detected = []
        
        while True:
            # 1. Prepare Request
            full_response = ""     # Reset for this turn
            full_content = ""      # Reset for this turn
            full_reasoning = ""    # Reset for this turn
            
            streaming_tools = {}
            tool_calls_detected = []
            
            if self.use_server:
                # Proactive Context Management: Compress before request to prevent overflow
                # Check token usage and compress if > 85% of limit
                current_tokens, max_tokens = self.get_token_usage()
                if current_tokens > int(max_tokens * 0.85):  # 85% threshold (e.g., > 6963 of 8192)
                    UI.event("Context", f"Proactive compression: {current_tokens}/{max_tokens} tokens ({current_tokens/max_tokens:.0%})", style="info")
                    self.manage_context()
                else:
                    # Still check normal threshold
                    self.manage_context()
                
                # Retry loop for 503 (Model Loading), 500 (Context Overflow), and 400 (Context Size Error)
                response = None
                for _ in range(15): # Try for ~30 seconds
                    try:
                        # CRITICAL: Rebuild payload with current history (may have been compressed)
                        payload = {
                             "messages": self.history,
                             "tools": self.TOOLS, 
                             "tool_choice": "auto",
                             "stream": True,
                             "temperature": current_temp,
                        }
                        
                        response = requests.post(
                            "http://127.0.0.1:8080/v1/chat/completions", 
                            json=payload, 
                            stream=True,
                            timeout=600 
                        )
                        
                        # DEBUG TRACER
                        # UI.event("Debug", f"Raw Response: {response.status_code}")

                        if response.status_code == 503:
                            UI.event("Server", "Model is loading, waiting...", style="warning")
                            time.sleep(2)
                            continue
                        
                        # Handle Context Overflow (500) - automatically compress and retry
                        if response.status_code == 500:
                            error_text = response.text or ""
                            if "context" in error_text.lower() or "exceed" in error_text.lower():
                                UI.event("Context", "Context overflow detected. Compressing history...", style="warning")
                                # Aggressively compress context
                                self.manage_context()
                                # Also truncate old messages if still too large
                                if len(self.history) > 20:
                                    # Keep system prompt, last user message, and last 10 messages
                                    system_msgs = [m for m in self.history if m.get("role") == "system"]
                                    user_msgs = [m for m in self.history if m.get("role") == "user"]
                                    assistant_msgs = [m for m in self.history if m.get("role") == "assistant"]
                                    
                                    # Keep first system message, last user message, last 5 assistant messages
                                    new_history = []
                                    if system_msgs:
                                        new_history.append(system_msgs[0])  # Keep first system prompt
                                    if user_msgs:
                                        new_history.append(user_msgs[-1])  # Keep last user message
                                    if assistant_msgs:
                                        new_history.extend(assistant_msgs[-5:])  # Keep last 5 assistant messages
                                    
                                    self.history = new_history
                                    UI.event("Context", f"Compressed to {len(self.history)} messages. Retrying...", style="info")
                                    # Retry the request with compressed context (payload will be rebuilt in next iteration)
                                    continue
                        
                        # Handle Context Size Error (400) - automatically compress and retry
                        if response.status_code == 400:
                            try:
                                error_data = response.json()
                                error_msg = error_data.get("error", {}).get("message", "")
                                if "exceed_context_size" in error_msg.lower() or "exceed" in error_msg.lower():
                                    UI.event("Context", "Context size exceeded. Compressing history...", style="warning")
                                    # Aggressively compress context
                                    self.manage_context()
                                    # Also truncate old messages if still too large
                                    if len(self.history) > 20:
                                        # Keep system prompt, last user message, and last 10 messages
                                        system_msgs = [m for m in self.history if m.get("role") == "system"]
                                        user_msgs = [m for m in self.history if m.get("role") == "user"]
                                        assistant_msgs = [m for m in self.history if m.get("role") == "assistant"]
                                        
                                        # Keep first system message, last user message, last 5 assistant messages
                                        new_history = []
                                        if system_msgs:
                                            new_history.append(system_msgs[0])  # Keep first system prompt
                                        if user_msgs:
                                            new_history.append(user_msgs[-1])  # Keep last user message
                                        if assistant_msgs:
                                            new_history.extend(assistant_msgs[-5:])  # Keep last 5 assistant messages
                                        
                                        self.history = new_history
                                        UI.event("Context", f"Compressed to {len(self.history)} messages. Retrying...", style="info")
                                        # Retry the request with compressed context (payload will be rebuilt in next iteration)
                                        continue
                            except (json.JSONDecodeError, KeyError):
                                pass  # Not a context size error, fall through to normal error handling
                            
                        if response.status_code != 200:
                            UI.error(f"Server returned {response.status_code}: {response.text}")
                            return
                        
                        # If successful (200), break retry loop
                        break
                    except requests.exceptions.ConnectionError:
                         UI.event("Server", "Connection failed, retrying...", style="warning")
                         time.sleep(2)
                         continue
                    
                if not response or response.status_code != 200:
                    UI.error("Server unavailable after retries.")
                    return

                # DIAGNOSTIC: Check what the server actually gave us
                # UI.event("Debug", f"Status: {response.status_code} | History: {len(self.history)}")
                
                first_token = True
                try:
                    chunk_count = 0
                    for line in response.iter_lines():
                        chunk_count += 1
                        if not line: continue
                        line_text = line.decode('utf-8')
                        
                        # DEBUG: Verify we get data
                        # if chunk_count == 1:
                        #      UI.event("Debug", f"First Chunk: {line_text[:50]}...")

                        
                        if line_text.startswith("data: "):
                            raw_data = line_text[6:]
                            if raw_data.strip() == "[DONE]": break
                            
                            try:
                                chunk = json.loads(raw_data)
                                choices = chunk.get('choices', [])
                                if not choices: continue
                                delta = choices[0].get('delta', {})
                                
                                # Process Tool Calls
                                if 'tool_calls' in delta:
                                    for tc_chunk in delta['tool_calls']:
                                        idx = tc_chunk.get('index', 0)
                                        if idx not in streaming_tools:
                                            streaming_tools[idx] = {"name": "", "arguments": "", "id": ""}
                                        
                                        if 'id' in tc_chunk:
                                            streaming_tools[idx]['id'] += tc_chunk['id']
                                        if 'function' in tc_chunk:
                                            fn = tc_chunk['function']
                                            if 'name' in fn: streaming_tools[idx]['name'] += fn['name']
                                            if 'arguments' in fn: streaming_tools[idx]['arguments'] += fn['arguments']

                                # Process Content & Reasoning
                                content_chunk = delta.get('content', '')
                                reasoning_chunk = delta.get('reasoning_content', '')
                                
                                if reasoning_chunk:
                                    # We wrap each chunk in dim to ensure safe rich printing per-call
                                    # instead of trying to maintain open tags across stream calls
                                    if not is_reasoning:
                                        # Start of reasoning (User visual cue)
                                        is_reasoning = True
                                    
                                    if first_token:
                                        if stream_callback: stream_callback("") 
                                        first_token = False
                                        
                                    if stream_callback: 
                                        # Use rich escape to handle brackets properly
                                        stream_callback(f"[white dim]{escape(reasoning_chunk)}[/]")
                                    full_response += reasoning_chunk
                                    full_reasoning += reasoning_chunk
                                
                                if content_chunk:
                                    if is_reasoning:
                                        # End of reasoning - Add Separator!
                                        if stream_callback: stream_callback("\n\n") 
                                        is_reasoning = False
                                        
                                    if first_token:
                                        if stream_callback: stream_callback("")
                                        first_token = False
                                    
                                    if stream_callback: 
                                        # Content is cyan (handled by run.py style)
                                        stream_callback(f"{escape(content_chunk)}")
                                    full_response += content_chunk
                                    full_content += content_chunk
                                    
                            except: pass
                except Exception as e:
                    UI.error(f"Server Error: {e}")
                    return
            
            else:
                # Library Logic
                try:
                    stream = self.llm.create_chat_completion(
                        messages=self.history,
                        tools=self.TOOLS,
                        tool_choice="auto",
                        max_tokens=8192,
                        temperature=target_temp,
                        stream=True
                    )
                    first_token = True
                    for chunk in stream:
                        choices = chunk.get('choices', [])
                        if not choices: continue
                        delta = choices[0].get('delta', {})
                        
                        if 'tool_calls' in delta:
                            for tc_chunk in delta['tool_calls']:
                                idx = tc_chunk.get('index', 0)
                                if idx not in streaming_tools:
                                    streaming_tools[idx] = {"name": "", "arguments": "", "id": ""}
                                if 'id' in tc_chunk: streaming_tools[idx]['id'] += tc_chunk['id']
                                if 'function' in tc_chunk:
                                    fn = tc_chunk['function']
                                    if 'name' in fn: streaming_tools[idx]['name'] += fn['name']
                                    if 'arguments' in fn: streaming_tools[idx]['arguments'] += fn['arguments']

                        content_chunk = delta.get('content', '')
                        if content_chunk:
                            if first_token:
                                if stream_callback: stream_callback("") 
                                first_token = False
                            if stream_callback: stream_callback(content_chunk)
                            full_response += content_chunk
                            full_content += content_chunk
                except Exception as e:
                     UI.error(f"Inference Error: {e}")
                     return

            # --- Unified Post-Processing ---
            if stream_callback: stream_callback("\n")
            
            # 1. Handle Tool Calls
            # ... (Tool logic unchanged) ...
            if streaming_tools:
                 for idx in sorted(streaming_tools.keys()):
                    tool_data = streaming_tools[idx]
                    try:
                        tool_name = tool_data['name']
                        
                        # CRITICAL: Check if we're retrying a tool that just failed OR repeating a success
                        if len(self.history) >= 1:
                            # Check failure (existing logic) or repetition (new logic)
                            # Find the last tool call in history (it might be followed by a system warning, so search back a bit)
                            # Increased search range to 20 to handle piled up warnings
                            last_tool_msg = None
                            for i in range(len(self.history) - 1, max(-1, len(self.history) - 20), -1):
                                if self.history[i].get('role') == 'tool':
                                    last_tool_msg = self.history[i]
                                    break
                            
                            if last_tool_msg and last_tool_msg.get('name') == tool_name:
                                tool_result = str(last_tool_msg.get('content', '')).lower()
                                
                                # 1. Check for FAILURE (existing logic)
                                is_python_exec_code_error = (
                                    tool_name == "python_exec" and 
                                    "(exit=" in tool_result
                                )
                                is_error = (
                                    "error executing tool" in tool_result or
                                    ("error:" in tool_result and not "❌" in tool_result and not is_python_exec_code_error) or
                                    "server returned" in tool_result and ("400" in tool_result or "500" in tool_result or "404" in tool_result) or
                                    "failed" in tool_result and ("tool" in tool_result or "execution" in tool_result) or
                                    (tool_result.startswith("error") and not "❌" in tool_result and not is_python_exec_code_error)
                                )
                                
                                if is_error:
                                    # Block retry of failure
                                    UI.event("Warning", f"Blocked retry of failed tool: {tool_name}", style="warning")
                                    self.history.append({
                                        "role": "system",
                                        "content": (
                                            f"⚠️ STOP! You tried to call '{tool_name}' again after it failed.\n"
                                            f"The tool returned an error: {last_tool_msg.get('content', '')}\n"
                                            f"DO NOT retry failed tools immediately. Fix the arguments or inform the user."
                                        )
                                    })
                                    continue
                                
                                # 2. Check for SUCCESSFUL REPETITION (New Logic)
                                # If args match (fuzzy check) and result wasn't error -> Block
                                # This prevents double-execution loops
                                try:
                                    # Simple check: if tool name matches and we just ran it successfully
                                    # We assume args match if the agent tries it immediately again
                                    UI.event("Warning", f"Blocked redundant tool call: {tool_name}", style="warning")
                                    self.history.append({
                                        "role": "system",
                                        "content": (
                                            f"⚠️ STOP! You just executed '{tool_name}' successfully.\n"
                                            f"The result is already in the context above (look for the 'tool' message).\n"
                                            f"DO NOT execute it again. Analyze the result and provide your answer."
                                        )
                                    })
                                    continue
                                except: pass
                        
                        tool_calls_detected.append({
                            "id": tool_data['id'] or f"call_{os.urandom(4).hex()}",
                            "type": "function",
                            "function": {"name": tool_name, "arguments": tool_data['arguments']}
                        })
                    except: pass
            
            # Fallback regex matches for models that don't use server API for tools
            if not tool_calls_detected:
                # 1. XML Format: <tool_call>{"name":..., "arguments":...}</tool_call>
                xml_tools = re.findall(r'<tool_call>(.*?)</tool_call>', full_response, re.DOTALL)
                for tool_str in xml_tools:
                     try:
                        tool_data = json.loads(tool_str)
                        if "name" in tool_data and "arguments" in tool_data:
                            tool_calls_detected.append({
                                "id": f"call_{os.urandom(4).hex()}",
                                "type": "function",
                                "function": {"name": tool_data["name"], "arguments": json.dumps(tool_data["arguments"]) if isinstance(tool_data["arguments"], dict) else tool_data["arguments"]}
                            })
                     except: pass
                
                # 2. Raw JSON Code Block: ```json ... ``` (if XML fails and looks like tool)
                if not tool_calls_detected and "```json" in full_response:
                    try:
                        json_match = re.search(r'```json\s*(\[.*?\]|\{.*?\})\s*```', full_response, re.DOTALL)
                        if json_match:
                            data = json.loads(json_match.group(1))
                            if isinstance(data, dict): data = [data] # normalize to list
                            for item in data:
                                if "name" in item and "arguments" in item:
                                     tool_calls_detected.append({
                                        "id": f"call_{os.urandom(4).hex()}",
                                        "type": "function",
                                        "function": {"name": item["name"], "arguments": json.dumps(item["arguments"]) if isinstance(item["arguments"], dict) else item["arguments"]}
                                    })
                    except: pass

            if tool_calls_detected:
                content_for_history = full_response if full_response else "Thinking..." 
                if self.use_server and not full_response: content_for_history = None 
                
                msg = {"role": "assistant", "content": content_for_history, "tool_calls": tool_calls_detected}
                if not content_for_history: del msg["content"]
                
                self.history.append(msg)

                for tc in tool_calls_detected:
                    function_name = tc['function']['name']
                    raw_args = tc['function']['arguments']
                    try:
                        arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except: arguments = {}

                    UI.event("Tool", f"{function_name}", style="highlight")
                    
                    # Extract user question for web_search to enable per-page analysis
                    if function_name == "web_search":
                        # Find the last user message to get the original question
                        user_question = arguments.get('query', '')  # Default to query
                        for msg in reversed(self.history):
                            if msg.get('role') == 'user':
                                user_question = msg.get('content', user_question)
                                break
                        # Add user_question to arguments for web_search
                        arguments['user_question'] = user_question
                    
                    # Show spinner while tool works
                    from vaf.cli.ui import UI as UI_Class
                    result = None
                    try:
                        # Special Case: Tools with their own immersive UI (no spinner needed)
                        if function_name in ("coding_agent", "research_agent"):
                            # Log agent-to-agent communication
                            if function_name == "coding_agent":
                                # Check if this is a follow-up call (after answering questions)
                                tool_args_str = str(arguments)
                                is_followup = any(keyword in tool_args_str.lower() for keyword in [
                                    "answer", "based on", "use", "should be", "according to"
                                ])
                                if is_followup:
                                    from vaf.cli.ui import UI
                                    # Show full message without truncation
                                    UI.event("( OO) Agent Chat", f"( OO) Main Agent → (OO ) Coding Agent: {tool_args_str}", style="green")
                            # No spinner, just run. The tool will print its own updates.
                            result = self.execute_tool(function_name, arguments)
                        else:
                            # IMPORTANT: Some tools require interactive confirmation (trust gate).
                            # Rich's live status spinner can hide/break interactive prompts.
                            # So for risky/gated tools, run WITHOUT the status spinner.
                            from vaf.core.trust import should_gate_tool
                            if should_gate_tool(function_name):
                                result = self.execute_tool(function_name, arguments)
                            else:
                                # Standard Tool Spinner
                                with UI_Class.console.status(
                                    f"[bold cyan](O_O) Executing {function_name}...[/bold cyan]",
                                    spinner="dots"
                                ):
                                    result = self.execute_tool(function_name, arguments)
                    except Exception as e:
                        error_msg = f"Error executing tool: {e}"
                        result = error_msg
                        # Log error for debugging
                        UI.error(f"Tool {function_name} failed: {e}")

                    self.history.append({
                        "role": "tool",
                        "tool_call_id": tc['id'],
                        "name": function_name,
                        "content": str(result)
                    })
                    
                    # If tool returned an error, force the model to acknowledge it
                    result_str = str(result).lower() if result else ""
                    is_tool_error = (
                        "error executing tool" in result_str or
                        ("error:" in result_str and not result_str.startswith("❌")) or  # Allow ❌ errors (user-friendly messages)
                        "server returned" in result_str and ("400" in result_str or "500" in result_str or "404" in result_str) or
                        "failed" in result_str and ("tool" in result_str or "execution" in result_str) or
                        (result_str.startswith("error") and not result_str.startswith("❌"))
                    )
                    
                    if result and is_tool_error:
                        # Add a system message to ensure the model responds to the error
                        # CRITICAL: Prevent retrying the same tool
                        self.history.append({
                            "role": "system",
                            "content": (
                                f"⚠️ CRITICAL: The tool '{function_name}' FAILED with error: {result}\n\n"
                                f"DO NOT call '{function_name}' again. DO NOT retry.\n"
                                f"You MUST inform the user about this error immediately. Do not think - just report the error."
                            )
                        })
                    
                    # Context Management: Check after each tool response to prevent overflow
                    # Tool responses can be large (e.g., web_search results, file contents)
                    # Compress if we're approaching the limit
                    self.manage_context()
                
                UI.event("Debug", "Summarizing intel...", style="dim")
                continue
            
            # 2. Handle Empty / Think-Only Responses
            # CRITICAL: First check if response is truly empty (BEFORE cleaning)
            # This is for tool-intent detection - we need to check the original response
            is_truly_empty = (not full_response) or (len(full_response.strip()) < 3)
            
            # Now perform cleaning for other purposes (empty response handler, etc.)
            # A truly empty content means NO final answer was given to the user.
            # We must strip XML tags, filler words, and common "empty" patterns.
            clean_content = re.sub(r'<[^>]*>', '', full_content)  # Remove XML tags
            clean_content = re.sub(r'```[\s\S]*?```', '', clean_content)  # Remove code blocks
            clean_content = clean_content.replace(".", "").replace("\n", "").replace(":", "").strip()
            
            # Also strip common "empty answer" patterns
            empty_patterns = ["answer", "antwort", "response", "here", "hier", "ok", "okay"]
            temp_content = clean_content.lower()
            for pattern in empty_patterns:
                temp_content = temp_content.replace(pattern, "")
            temp_content = temp_content.strip()
            
            # Consider empty if: no final answer OR only filler words (< 3 real chars after cleaning)
            # NOTE: This checks full_content (final answer), NOT full_reasoning (thinking)
            # The model can think as much as it wants, but must provide a final answer
            is_effectively_empty = len(temp_content) < 3
            
            # Empty Response Handler: Remove responses without final answer and restart from snapshot
            # NO RETRY LIMITS - will loop until we get a response
            # IMPORTANT: This removes assistant messages that have NO final answer (even if they have reasoning)
            # CRITICAL: At this point, the stream is COMPLETE (finish_reason="stop" equivalent)
            # We can immediately check for tool-intent without waiting, as the model has finished generating
            if (not full_response or is_effectively_empty):
                # CRITICAL: Check if agent mentioned a tool name (but didn't actually call it yet)
                # Tool names are language-independent - they're always the same regardless of thinking language
                # IMPORTANT: No time-based waiting needed - stream is already complete, so we can check immediately
                
                # For tool-intent detection, use is_truly_empty (checked BEFORE cleaning)
                # This prevents false positives when agent has long thinking text
                if is_truly_empty:
                    # Get available tool names dynamically (supports user-added tools)
                    available_tool_names = list(self.tools.keys()) if self.tools else []
                    
                    # Check if any tool name appears in the response (case-insensitive)
                    lower_response = (full_response or "").lower()
                    mentioned_tools = [tool_name for tool_name in available_tool_names if tool_name.lower() in lower_response]
                    
                    # CRITICAL: Only reset if BOTH conditions are met:
                    # 1. Response is truly empty (< 3 chars) - checked BEFORE cleaning
                    # 2. Tool was mentioned but not called
                    # This is language-independent - we only check if response is empty, not what language it's in
                    if mentioned_tools and not tool_calls_detected:
                        tool_hint = mentioned_tools[0]
                        UI.event("Insight", f"Detected intent: '{tool_hint}' (resetting to activate)", style="dim")
                        
                        # Find if we have tool calls in history (after snapshot)
                    has_tool_calls = False
                    last_tool_idx = None
                    for i in range(history_snapshot_len, len(self.history)):
                        msg = self.history[i]
                        if msg.get('role') == 'tool':
                            has_tool_calls = True
                            last_tool_idx = i
                    
                    # Reset to appropriate snapshot
                    if has_tool_calls and last_tool_idx is not None:
                        # Keep everything up to and including the tool result
                        self.history = self.history[:last_tool_idx + 1]
                        UI.event("Debug", f"Reset to tool call snapshot (after tool result)", style="dim")
                    else:
                        # No tool calls - check if there's thinking DIRECTLY after user prompt
                        # The user prompt is at history_snapshot_len (if skip_input) or history_snapshot_len (if not skip_input, user was added there)
                        # We want to find the FIRST assistant message with content right after the user prompt
                        
                        # User prompt position: if skip_input, no user was added, so it's before snapshot
                        # If not skip_input, user was added at history_snapshot_len
                        user_prompt_idx = history_snapshot_len
                        if skip_input:
                            # No user input was added, so user prompt is before snapshot
                            # Find the last user message before snapshot
                            for i in range(history_snapshot_len - 1, -1, -1):
                                if self.history[i].get('role') == 'user':
                                    user_prompt_idx = i
                                    break
                        
                        # Check if there's an assistant message with content directly after user prompt
                        first_assistant_after_user = None
                        first_assistant_idx = None
                        
                        # Look at messages right after user prompt (within next 2 messages)
                        for i in range(user_prompt_idx + 1, min(user_prompt_idx + 3, len(self.history))):
                            msg = self.history[i]
                            if msg.get('role') == 'assistant' and msg.get('content'):
                                content = str(msg.get('content', ''))
                                # Keep if it has substantial content (thinking/reasoning)
                                if len(content.strip()) > 20:
                                    first_assistant_after_user = content
                                    first_assistant_idx = i
                                    break  # Only take the FIRST one directly after user prompt
                        
                        if first_assistant_after_user and first_assistant_idx is not None:
                            # Keep user prompt + first thinking - this becomes the new snapshot
                            self.history = self.history[:first_assistant_idx + 1]
                            UI.event("Debug", f"Reset to thinking snapshot (user prompt + {len(first_assistant_after_user)} chars of first thinking)", style="dim")
                        else:
                            # No thinking found - reset to user prompt snapshot (as before)
                            if skip_input:
                                # No user was added, so just reset to snapshot
                                self.history = self.history[:history_snapshot_len]
                            else:
                                # User was added, so keep it
                                self.history = self.history[:history_snapshot_len + 1]  # +1 for user message
                            UI.event("Debug", f"Reset to user prompt snapshot", style="dim")
                    
                    # Add a brief system prompt (will work better now because first thinking is preserved)
                    self.history.append({
                        "role": "system",
                        "content": "You didn't respond. Please answer or continue where you left off."
                    })
                    
                    # Continue the loop - if it fails again, this system message will be removed with the reset
                    continue
                
                # No tool mentioned - truly empty response
                
                # Check retry limit - but DO NOT BREAK (infinite patience)
                empty_retry_count += 1
                
                # Dynamic Temperature Sweep to break loops
                # Oscillate around target_temp: -0.1, +0.1, -0.2, +0.2, ...
                delta = ((empty_retry_count + 1) // 2) * 0.1
                direction = -1 if empty_retry_count % 2 == 1 else 1
                current_temp = target_temp + (delta * direction)
                # Clamp between 0.1 and 0.9
                current_temp = max(0.1, min(0.9, current_temp))
                
                UI.event("Adaptive", f"Tuning creativity: {current_temp:.1f} (attempt {empty_retry_count})", style="info")
                
                if empty_retry_count >= 10:
                    # Only warn after many retries, but keep trying
                    UI.event("Warning", f"High retry count ({empty_retry_count}) for empty response", style="dim")
                
                # Find if we have tool calls in history (after snapshot)
                has_tool_calls = False
                last_tool_idx = None
                for i in range(history_snapshot_len, len(self.history)):
                    msg = self.history[i]
                    if msg.get('role') == 'tool':
                        has_tool_calls = True
                        last_tool_idx = i
                
                # Reset to appropriate snapshot
                if has_tool_calls and last_tool_idx is not None:
                    # Keep everything up to and including the tool result
                    reset_idx = last_tool_idx + 1
                    self.history = self.history[:reset_idx]
                    UI.event("Self-Fix", f"Agent silent - auto-correcting context (snapshot {reset_idx})", style="dim")
                else:
                    # No tool calls - check if there's thinking DIRECTLY after user prompt
                    user_prompt_idx = history_snapshot_len
                    if skip_input:
                        # No user input was added, so user prompt is before snapshot
                        # Find the last user message before snapshot
                        for i in range(history_snapshot_len - 1, -1, -1):
                            if self.history[i].get('role') == 'user':
                                user_prompt_idx = i
                                break
                    
                    # Check if there's an assistant message with content directly after user prompt
                    first_assistant_after_user = None
                    first_assistant_idx = None
                    
                    # Look at messages right after user prompt (within next 2 messages)
                    for i in range(user_prompt_idx + 1, min(user_prompt_idx + 3, len(self.history))):
                        msg = self.history[i]
                        if msg.get('role') == 'assistant' and msg.get('content'):
                            content = str(msg.get('content', ''))
                            # Keep if it has substantial content (thinking/reasoning)
                            if len(content.strip()) > 20:
                                first_assistant_after_user = content
                                first_assistant_idx = i
                                break  # Only take the FIRST one directly after user prompt
                    
                    reset_idx = history_snapshot_len
                    
                    if first_assistant_after_user and first_assistant_idx is not None:
                        # Keep user prompt + first thinking - this becomes the new snapshot
                        reset_idx = first_assistant_idx + 1
                        UI.event("Self-Fix", "Agent silent - preserving thought trace", style="dim")
                    else:
                        # No thinking found - reset to user prompt snapshot (as before)
                        if skip_input:
                            reset_idx = history_snapshot_len
                        else:
                            reset_idx = history_snapshot_len + 1  # +1 for user message
                        UI.event("Self-Fix", "Agent silent - auto-correcting context", style="dim")
                    
                    self.history = self.history[:reset_idx]
                
                # Add a brief system prompt (will work better now because first thinking is preserved)
                self.history.append({
                    "role": "system",
                    "content": "You didn't respond. Please answer or continue where you left off."
                })
                
                # Continue the loop - if it fails again, this system message will be removed with the reset
                continue
            
            # Clean History: Store ONLY the final content (Answer), discarding the reasoning trace.
            history_content = full_content if full_content else full_response
            self.history.append({"role": "assistant", "content": history_content})
            
            # Check for language mismatch: Did the model respond in a different language than the user?
            # This helps catch cases where LANGUAGE_HINT was ignored (e.g., user asks in Turkish, model responds in English)
            if not skip_input and user_input and history_content:
                self._check_language_mismatch(user_input, history_content)
            
            # Context Management: Check after adding assistant response
            # Long reasoning phases can push us over the limit
            self.manage_context()
            
            # --- Proactive Context Compression (User Request) ---
            # "Only Questions and Answers remain in Context"
            # We squash ALL intermediate steps: Tools, Thoughts, System prompts.
            try:
                # User Msg is at history_snapshot_len.
                # New content starts at history_snapshot_len + 1.
                # Final Answer is at -1.
                start_idx = history_snapshot_len + 1
                end_idx = len(self.history) - 1
                
                if end_idx > start_idx:
                    # We have intermediate steps (Tools, Thoughts, etc.)
                    msgs_to_squash = self.history[start_idx:end_idx]
                    
                    # Collect info about what was squashed
                    tools_used = []
                    thoughts_count = 0
                    
                    for m in msgs_to_squash:
                        role = m.get('role', '')
                        content = str(m.get('content', ''))
                        
                        if role == 'tool':
                            tools_used.append(m.get('name', 'UnknownTool'))
                        elif role == 'assistant':
                            # Count thought blocks (reasoning traces)
                            if '<think>' in content or '</think>' in content:
                                thoughts_count += 1
                    
                    # ALWAYS squash intermediate steps (not just when tools used)
                    if msgs_to_squash:
                        unique_tools = list(set(tools_used))
                        
                        # Delete ALL intermediate messages
                        del self.history[start_idx:end_idx]
                        
                        # Build concise summary
                        summary_parts = []
                        if unique_tools:
                            summary_parts.append(f"Tools: {', '.join(unique_tools)}")
                        if thoughts_count > 0:
                            summary_parts.append(f"Reasoning: {thoughts_count} steps")
                        
                        if summary_parts:
                            summary_msg = f"[Context: {' | '.join(summary_parts)}]"
                            self.history.insert(start_idx, {"role": "system", "content": summary_msg})
                        # If nothing to summarize, just delete without inserting
            except Exception as e:
                UI.event("Debug", f"Compression Warning: {e}", style="dim")

            break
            
        # Emergency Fallback: If we exhausted retries and STILL have no answer
        if not clean_content:
             # Check if we have a tool result immediately preceding this "silent" thought block
             last_msg = self.history[-1] if self.history else {}
             prev_msg = self.history[-2] if len(self.history) > 1 else {}
             
             # Case 1: Loop ended with Tool Output -> Model Silent
             if last_msg.get('role') == 'tool':
                 return f"✅ Tool '{last_msg.get('name')}' finished: {last_msg.get('content')[:100]}..."
                 
             # Case 2: Loop ended with Assistant Thought -> Model Silent (Previous was tool)
             if last_msg.get('role') == 'assistant' and prev_msg.get('role') == 'tool':
                  return f"✅ Tool '{prev_msg.get('name')}' finished. (Model provided no commentary)"

             return "..."
        
        # Final empty check - same logic as above
        clean_final = re.sub(r'<[^>]*>', '', full_response)
        clean_final = re.sub(r'```[\s\S]*?```', '', clean_final)
        clean_final = clean_final.replace(".", "").replace("\n", "").replace(":", "").strip()
        temp_final = clean_final.lower()
        for pattern in ["answer", "antwort", "response", "here", "hier", "ok", "okay"]:
            temp_final = temp_final.replace(pattern, "")
        is_final_empty = len(temp_final.strip()) < 3
        
        if is_final_empty and not tool_calls_detected:
             
             if not auto_retry:
                 # NUCLEAR OPTION: Rollback and Try Fresh
                 UI.event("System", "Response loop detected. Cleaning context and retrying fresh...", style="warning")
                 
                 # Restore history to state before this turn (remove user input + failed attempts)
                 # Wait, we need to keep the USER input.
                 # Actually, simpler: Remove everything added AFTER the user input.
                 # But we also added user input at start.
                 # Let's just pop everything until we are back to snapshot length.
                 # But we can't trust current self.history length easily.
                 
                 # 1. Remove the failed attempts (System prompts, Assistant thoughts).
                 # 2. Keep the User prompt.
                 # 3. Recursively call with auto_retry=True.
                 
                 # The user prompt is at index `start_len` (if not skipped).
                 target_len = history_snapshot_len + 1 if not skip_input else history_snapshot_len
                 self.history = self.history[:target_len]
                 
                 return self.chat_step(user_input="", stream_callback=stream_callback, auto_retry=True, skip_input=True)
                 
             fallback_msg = "\n\n*(System: The agent processed the request but failed to generate a final answer. Please see the thought trace above.)*"
             if stream_callback: stream_callback(f"[gold]{fallback_msg}[/gold]")
             self.history.append({"role": "assistant", "content": fallback_msg}) 
             return fallback_msg

    def execute_tool(self, name, args):
        from vaf.cli.ui import UI
        from pathlib import Path
        from vaf.core.trust import should_gate_tool, get_tool_policy, set_tool_policy, mark_trusted_dir, is_trusted_dir, explain_gate
        
        def emit(evt: dict):
            if callable(self._event_sink):
                try:
                    self._event_sink(evt)
                except Exception:
                    pass
        
        def make_json_serializable(obj):
            """
            Recursively convert Path objects and other non-serializable types to strings.
            OS-independent: works with WindowsPath, PosixPath, and PurePath.
            """
            if isinstance(obj, Path):
                return str(obj)
            elif isinstance(obj, dict):
                return {k: make_json_serializable(v) for k, v in obj.items()}
            elif isinstance(obj, (list, tuple)):
                return [make_json_serializable(item) for item in obj]
            else:
                return obj

        # Gate risky tools with once/always/cancel (no persistent deny)
        if should_gate_tool(name):
            policy = get_tool_policy(name)
            cwd = Path.cwd()
            trusted = is_trusted_dir(cwd)
            allowed_once = name in self._allow_once_tools
            
            if policy != "allow" and not trusted and not allowed_once:
                emit({"type": "gate_required", "tool": name, "cwd": str(cwd)})
                if self._noninteractive:
                    return f"[ERROR] Tool '{name}' requires confirmation ({explain_gate(name)}). Re-run interactively or mark folder trusted."
                
                UI.event("Security", f"Tool '{name}' requires confirmation. {explain_gate(name)}", style="warning")
                choice = UI.prompt("Allow? [o]nce / [a]lways / [c]ancel: ").strip().lower()
                if choice in ("o", "once"):
                    self._allow_once_tools.add(name)
                    emit({"type": "gate_decision", "tool": name, "decision": "allow_once"})
                elif choice in ("a", "always"):
                    # Always = trust current folder + allow tool
                    mark_trusted_dir(cwd)
                    set_tool_policy(name, "allow")
                    emit({"type": "gate_decision", "tool": name, "decision": "allow_always"})
                else:
                    emit({"type": "gate_decision", "tool": name, "decision": "cancel"})
                    return f"[CANCELLED] Tool '{name}' cancelled by user."

        # Convert Path objects in args to strings for JSON serialization (OS-independent)
        serializable_args = make_json_serializable(args) if args else {}
        emit({"type": "tool_start", "tool": name, "args": serializable_args})
        try:
            if name in self.tools:
                result = self.tools[name].run(**args)
            else:
                result = f"Error: Unknown tool '{name}'"
        except Exception as e:
            result = f"Tool Error: {e}"

        # If python_sandbox blocked the request, offer a gated fallback to python_exec
        # (once/always/cancel) so the user can explicitly override sandbox restrictions.
        if name == "python_sandbox" and isinstance(result, str) and result.startswith("Security Error:"):
            if "python_exec" in self.tools:
                cwd = Path.cwd()
                policy = get_tool_policy("python_exec")
                trusted = is_trusted_dir(cwd)
                allowed_once = "python_exec" in self._allow_once_tools
                
                if policy != "allow" and not trusted and not allowed_once:
                    if not self._noninteractive:
                        UI.event("Security", "python_sandbox blocked this code. You can run it UNSANDBOXED via python_exec.", style="warning")
                        choice = UI.prompt("Run via python_exec? [o]nce / [a]lways / [c]ancel: ").strip().lower()
                        if choice in ("o", "once"):
                            self._allow_once_tools.add("python_exec")
                        elif choice in ("a", "always"):
                            mark_trusted_dir(cwd)
                            set_tool_policy("python_exec", "allow")
                        else:
                            return result + "\n\n[CANCELLED] Not running unsandboxed."
                    else:
                        return result + "\n\n[INFO] python_exec is available but requires interactive confirmation."
                
                # Execute unsandboxed python if allowed
                if get_tool_policy("python_exec") == "allow" or is_trusted_dir(cwd) or ("python_exec" in self._allow_once_tools):
                    code = (args or {}).get("code", "")
                    # Convert args to JSON-serializable format (OS-independent)
                    python_exec_args = make_json_serializable({"timeout": 30})
                    emit({"type": "tool_start", "tool": "python_exec", "args": python_exec_args})
                    try:
                        unsafe_result = self.tools["python_exec"].run(code=code, timeout=30)
                    except Exception as e:
                        unsafe_result = f"Tool Error: {e}"
                    emit({"type": "tool_end", "tool": "python_exec"})
                    result = unsafe_result

        emit({"type": "tool_end", "tool": name})

        # TRUNCATION: Limit context usage for massive outputs (e.g. list_files on Downloads)
        MAX_LEN = 2000
        if len(str(result)) > MAX_LEN:
            truncated = str(result)[:MAX_LEN]
            result = f"{truncated}\n... [Output Truncated. Total length: {len(str(result))} chars. Use specific filters or read sub-parts.]"
        
        return result

    def set_event_sink(self, sink):
        """Set an optional event sink for structured outputs (e.g. stream-json)."""
        self._event_sink = sink

    # --- Tool Implementations ---

    def perform_web_search(self, query):
        try:
            # 1. Search (Deep Research: Get detailed snippets)
            results = DDGS().text(query, max_results=5, safesearch='strict') # 5 high quality
            if not results: return "No results found."
            
            summary = "### Web Search Results (Deep Research)\n"
            
            # 2. Deep Dive: Fetch content of top 2 results
            # We use a simple fetcher to get the actual page text
            import requests
            
            def fetch_text(url):
                try:
                    r = requests.get(url, timeout=4, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"})
                    if r.status_code != 200: return None
                    
                    html = r.text
                    # 1. Remove Script and Style elements completely
                    html = re.sub(r'<(script|style|noscript)[^>]*>.*?</\1>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
                    
                    # 2. Basic strip tags
                    text = re.sub(r'<[^>]+>', ' ', html)
                    
                    # 3. Clean whitespace
                    text = re.sub(r'\s+', ' ', text).strip()
                    
                    return text[:3000] # Increased limit to catchment more content
                except: return None

            for i, res in enumerate(results):
                title = res['title']
                link = res['href']
                snippet = res['body']
                
                content = ""
                # Deep fetch for top 2
                if i < 2:
                     from vaf.cli.ui import UI
                     UI.event("Deep Research", f"Reading {link[:30]}...", style="dim")
                     page_text = fetch_text(link)
                     if page_text:
                         content = f"\n  [Full Content Preview]: {page_text}..."
                
                summary += f"- **{title}**\n  Snippet: {snippet}\n  Link: {link}{content}\n\n"
                
            return summary
        except Exception as e:
            return f"Error: {e}"

    BLOCKED_DIRS = ["Windows", "Program Files", "Program Files (x86)", "System32", ".git", ".ssh", "node_modules"]

    def is_safe_path(self, path):
        # Implementation of safe path check
        try:
             abs_path = os.path.abspath(os.path.expanduser(path))
             for blocked in self.BLOCKED_DIRS:
                 if blocked in abs_path:
                     return False, f"Access denied: {blocked}"
             return True, abs_path
        except:
             return False, "Invalid path"

    def list_files(self, path="."):
        safe, res = self.is_safe_path(path)
        if not safe: return res
        try:
            items = os.listdir(res)
            output = ""
            for item in items:
                item_path = os.path.join(res, item)
                if os.path.isdir(item_path):
                    output += f"[DIR]  {item}\n"
                else:
                    output += f"[FILE] {item}\n"
            return output if output else "Empty"
        except Exception as e: return str(e)

    def read_file(self, path):
        safe, res = self.is_safe_path(path)
        if not safe: return res
        try:
            with open(res, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read(2000)
                if len(content) == 2000: content += "\n...[Truncated]..."
                return content
        except Exception as e: return str(e)

    def write_file(self, path, content):
        safe, res = self.is_safe_path(path)
        if not safe: return res
        try:
            with open(res, 'w', encoding='utf-8') as f:
                f.write(content)
            return f"Written to {res}"
        except Exception as e: return str(e)

    def move_file(self, src, dst):
        safe_src, res_src = self.is_safe_path(src)
        if not safe_src: return res_src
        safe_dst, res_dst = self.is_safe_path(dst)
        if not safe_dst: return res_dst
        try:
            shutil.move(res_src, res_dst)
            return f"Moved {src} to {dst}"
        except Exception as e: return str(e)

    @property
    def TOOLS(self):
        """Dynamic Tool Schema Generation"""
        schema = []
        for name, tool in self.tools.items():
            schema.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.description,
                    "parameters": getattr(tool, "parameters", {"type": "object", "properties": {}})
                }
            })
        return schema
