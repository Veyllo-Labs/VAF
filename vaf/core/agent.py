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
from typing import List, Dict, Any
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
from vaf.core.system_prompt import SystemPromptManager
from vaf.tools.search import WebSearchTool
from vaf.tools.filesystem import ListFilesTool, ReadFileTool, WriteFileTool, MoveFileTool

import atexit
import signal

class Agent:
    # Language names mapping (ISO 639-1 codes to native names)
    # Used for multilingual instructions - comprehensive list supporting 97+ languages
    LANGUAGE_NAMES_NATIVE = {
        # Major European languages
        "en": "English", "de": "Deutsch", "fr": "Français", "es": "Español",
        "it": "Italiano", "pt": "Português", "nl": "Nederlands", "pl": "Polski",
        "ru": "Русский", "uk": "Українська", "sv": "Svenska", "no": "Norsk",
        "da": "Dansk", "fi": "Suomi", "cs": "Čeština", "ro": "Română",
        "hu": "Magyar", "el": "Ελληνικά", "tr": "Türkçe", "bg": "Български",
        "hr": "Hrvatski", "sr": "Српски", "sk": "Slovenčina", "sl": "Slovenščina",
        "et": "Eesti", "lv": "Latviešu", "lt": "Lietuvių", "ga": "Gaeilge",
        "mt": "Malti", "is": "Íslenska", "mk": "Македонски", "sq": "Shqip",
        "bs": "Bosanski", "ca": "Català", "eu": "Euskara", "gl": "Galego",
        # Asian languages
        "ja": "日本語", "ko": "한국어", "zh": "中文", "hi": "हिन्दी",
        "th": "ไทย", "vi": "Tiếng Việt", "id": "Bahasa Indonesia", "ms": "Bahasa Melayu",
        "tl": "Filipino", "my": "မြန်မာ", "km": "ខ្មែរ", "lo": "ລາວ",
        "bn": "বাংলা", "ta": "தமிழ்", "te": "తెలుగు", "ml": "മലയാളം",
        "kn": "ಕನ್ನಡ", "gu": "ગુજરાતી", "pa": "ਪੰਜਾਬੀ", "ur": "اردو",
        "ne": "नेपाली", "si": "සිංහල", "ka": "ქართული", "hy": "Հայերեն",
        "az": "Azərbaycan", "kk": "Қазақ", "ky": "Кыргызча", "uz": "Oʻzbek",
        "mn": "Монгол", "bo": "བོད་",
        # Middle Eastern & African languages  
        "ar": "العربية", "he": "עברית", "fa": "فارسی", "ps": "پښتو",
        "sw": "Kiswahili", "am": "አማርኛ", "zu": "isiZulu", "af": "Afrikaans",
        "so": "Soomaali", "ha": "Hausa", "yo": "Yorùbá", "ig": "Igbo",
        # Other languages
        "eo": "Esperanto", "la": "Latina", "cy": "Cymraeg", "br": "Brezhoneg",
        "auto": "the user's language"
    }
    
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
        
        # Backend initialization (Local or API)
        # Check for provider override from environment (for sub-agents)
        env_provider = os.environ.get("VAF_PROVIDER", "").strip()
        if env_provider:
            self.provider = env_provider
        else:
            self.provider = self.config.get("provider", "local")
        
        self.llm = None           # Local Library instance
        self.server = None        # ServerManager instance
        self.api_backend = None   # API Backend instance
        self.use_server = False   # Flag
        
        # Initialize API backend immediately if using API provider
        if self.provider != "local":
            try:
                from vaf.core.api_backend import APIBackendManager
                self.api_backend = APIBackendManager(self.provider)
            except Exception as e:
                # If API backend fails, we'll fallback to local in load_model()
                pass
        
        self.history = []

        # Trust gating state (session-only)
        self._allow_once_tools = set()
        self._noninteractive = os.environ.get("VAF_NONINTERACTIVE", "").strip() in ("1", "true", "yes")
        self._event_sink = None  # optional callable(dict)
        
        # Initialize Context Manager
        from vaf.core.context import ContextManager
        
        # Determine appropriate context limit
        context_limit = self.config.get("n_ctx", 8192)
        
        # If running in API mode, use a much larger default context limit
        # (unless user manually set n_ctx to something huge)
        if self.provider != "local":
             # 128k is a safe baseline for modern APIs (GPT-4o, Gemini 1.5 Flash, Claude 3.5 Sonnet)
             # Only override if n_ctx is the default/small value
             if context_limit <= 16384:
                 context_limit = 128000
                 
        self.context_manager = ContextManager(max_tokens=context_limit)
        
        # Initialize Prompt Manager
        from vaf.core.system_prompt import SystemPromptManager
        
        # Extract model name for identity
        model_display_name = "VQ-1"
        if hasattr(self, 'filename'):
            fname = self.filename.lower()
            if "gemma" in fname: model_display_name = "Gemma"
            elif "llama" in fname: model_display_name = "Llama"
            elif "mistral" in fname: model_display_name = "Mistral"
            elif "phi" in fname: model_display_name = "Phi"
            elif "qwen" in fname: model_display_name = "Qwen"
            elif "deepseek" in fname: model_display_name = "DeepSeek"
        
        # We need tools to init prompt manager, but tools are loaded later.
        # So we init it here with empty dict and update it after tools load.
        self.prompt_manager = SystemPromptManager({}, model_name=model_display_name) 

        # Session tracking for server shutdown management
        self._session_id = None
        self._register_session()

        # Initialize Tools (Dynamic Loading)
        self.tools = {}
        self._load_tools()
        # Update Prompt Manager with loaded tools
        self.prompt_manager.tools = self.tools
        
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

    def _speak(self, text: str):
        """Helper to speak response via SpeechManager."""
        try:
            from vaf.core.speech import get_speech_manager
            sm = get_speech_manager()
            
            # 1. Clean text first (remove artifacts that might confuse language detection)
            tts_text = sm._clean_markdown(text)
            if not tts_text.strip():
                return

            # 2. Determine language (Prioritize Config > PromptManager > Auto-Detect)
            tts_lang = "auto"
            
            # Check Config first
            config_lang = self.config.get("language", "auto")
            if config_lang and config_lang != "auto":
                tts_lang = config_lang
            # Check PromptManager
            elif hasattr(self, 'prompt_manager') and self.prompt_manager.user_language != "auto":
                tts_lang = self.prompt_manager.user_language
            # Fallback to detection on CLEANED text
            else:
                tts_lang = self._detect_user_language(tts_text)
            
            # 3. Speak
            sm.speak(tts_text, lang=tts_lang)
        except Exception:
            pass
    
    def _speak_filler(self, filler_type: str = "thinking", tool_name: str = None, query: str = None):
        """
        Speak filler phrases during thinking/tool execution to avoid dead silence.
        Provides natural feedback in the user's language.
        
        Args:
            filler_type: "thinking" or "tool"
            tool_name: Name of the tool being executed (for tool fillers)
            query: Search query or task description (for context-aware fillers)
        """
        try:
            from vaf.core.speech import get_speech_manager
            sm = get_speech_manager()
            
            # Only speak if TTS is enabled
            if not sm.is_tts_enabled():
                return
            
            # Determine language (Prioritize Config > PromptManager > Auto-Detect)
            lang = "auto"
            
            # Check Config first
            config_lang = self.config.get("language", "auto")
            if config_lang and config_lang != "auto":
                lang = config_lang
            # Check PromptManager
            elif hasattr(self, 'prompt_manager') and self.prompt_manager.user_language != "auto":
                lang = self.prompt_manager.user_language
            # Fallback to detection
            else:
                # Try to detect from last user message
                for msg in reversed(self.history):
                    if msg.get('role') == 'user':
                        lang = self._detect_user_language(msg.get('content', ''))
                        break
            
            # Import filler phrases from separate config file
            from vaf.core.speech_fillers import THINKING_FILLERS, TOOL_FILLERS
            
            # Select appropriate filler
            filler_text = ""
            
            if filler_type == "thinking":
                # Random thinking filler
                import random
                lang_key = lang if lang in THINKING_FILLERS else "en"
                fillers = THINKING_FILLERS.get(lang_key, THINKING_FILLERS["en"])
                filler_text = random.choice(fillers)
            
            elif filler_type == "tool" and tool_name:
                # Tool-specific filler
                lang_key = lang if lang in TOOL_FILLERS.get(tool_name, {}) else "en"
                
                # Get tool-specific filler or generic fallback
                if tool_name in TOOL_FILLERS:
                    filler_template = TOOL_FILLERS[tool_name].get(lang_key, TOOL_FILLERS[tool_name].get("en", ""))
                    
                    # Replace {query} placeholder if present
                    if query and "{query}" in filler_template:
                        # Truncate long queries
                        short_query = query[:50] + "..." if len(query) > 50 else query
                        filler_text = filler_template.format(query=short_query)
                    else:
                        filler_text = filler_template
                else:
                    # Generic tool filler
                    if lang == "de":
                        filler_text = f"Ich nutze {tool_name}"
                    elif lang == "tr":
                        filler_text = f"{tool_name} kullanıyorum"
                    elif lang == "es":
                        filler_text = f"Usando {tool_name}"
                    elif lang == "fr":
                        filler_text = f"J'utilise {tool_name}"
                    else:
                        filler_text = f"Using {tool_name}"
            
            # Speak the filler
            if filler_text:
                sm.speak(filler_text, lang=lang)
                
        except Exception:
            pass  # Silently fail - fillers are optional
    
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
        
        # Stop Speech
        try:
            from vaf.core.speech import get_speech_manager
            get_speech_manager().stop()
        except:
            pass
        
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
                        # OR if context is extremely small (<= 4096) to save space
                        is_in_automation = os.environ.get("VAF_IN_AUTOMATION", "").strip() in ("1", "true", "yes")
                        n_ctx = self.config.get("n_ctx", 8192)
                        
                        if is_in_automation or n_ctx <= 4096:
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
        
        # Track active async sub-agent tasks
        self._async_subagent_tasks = {}  # task_id -> {"agent_type": str, "task": str, "started_at": datetime}

    # ═══════════════════════════════════════════════════════════════════════════
    # SUB-AGENT IPC METHODS
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _check_subagent_results(self) -> list:
        """
        Check for completed sub-agent results.
        Called periodically during chat to process async results.
        
        Returns:
            List of completed SubAgentTask objects
        """
        try:
            from vaf.core.subagent_ipc import get_ipc, get_current_session_id
            ipc = get_ipc()
            
            # Cleanup stale tasks (crashed sub-agents)
            ipc.cleanup_stale_active_tasks(max_age_minutes=30)
            
            # CRITICAL: Only get results for CURRENT session (not old sessions!)
            current_session = get_current_session_id()
            
            # 0. Liveness Check (Detect Crashed Sub-Agents)
            # Check for zombies that haven't updated heartbeat in >20s
            ipc.check_zombies(timeout_seconds=20)
            
            results = ipc.get_pending_results(session_id=current_session)
            return results
        except Exception:
            return []
    
    def _process_subagent_result(self, task):
        """
        Process a completed sub-agent result and add it to the conversation.
        
        Args:
            task: SubAgentTask object with the result
        """
        from vaf.cli.ui import UI
        from vaf.core.subagent_ipc import get_ipc
        import re
        
        ipc = get_ipc()
        
        if task.status == "completed":
            UI.success(f"✓ Sub-Agent [{task.task_id}] delivered result!")
            
            # Extract file paths from result (research reports, generated documents, etc.)
            file_paths = re.findall(
                r'(?:Saved to|Output|File|Path|Ausgabe|Datei):\s*([^\n]+\.(?:html?|pdf|docx?|txt|md|json|csv|xlsx?))',
                task.result,
                re.IGNORECASE
            )
            
            file_hint = ""
            if file_paths:
                # Clean up paths (remove ANSI codes, extra spaces)
                cleaned_paths = [re.sub(r'\x1b\[[0-9;]*m', '', fp).strip() for fp in file_paths]
                file_hint = f"\n\n🔗 **EXTRACTED FILE PATHS (from Sub-Agent output):**\n"
                for fp in cleaned_paths[:3]:  # Limit to first 3 files
                    file_hint += f"- `{fp}`\n"
                file_hint += (
                    f"\n💡 **TIP:** To read/analyze this file, use:\n"
                    f"- `read_file('{cleaned_paths[0]}')` for quick reading\n"
                    f"- `librarian_agent(file='{cleaned_paths[0]}', task='Summarize this document')` for detailed analysis\n"
                )
            
            # Add result to history as if it was a tool response
            self.history.append({
                "role": "system",
                "content": (
                    f"📬 **Sub-Agent Task COMPLETED / TERMINATED** [Task: {task.task_id}]\n"
                    f"Agent: {task.agent_type}\n"
                    f"Original Task: {task.task_description[:200]}\n\n"
                    f"**FINAL RESULT (Task is DONE):**\n{task.result}"
                    f"{file_hint}\n\n"
                    f"IMPORTANT: This task is completely finished. The agent is NO LONGER working on it.\n"
                    f"Analyze the result above and answer the user."
                )
            })
        elif task.status == "failed":
            UI.error(f"✗ Sub-Agent [{task.task_id}] failed: {task.error}")
            
            self.history.append({
                "role": "system",
                "content": (
                    f"[X] **Sub-Agent Task FAILED / TERMINATED** [Task: {task.task_id}]\n"
                    f"Agent: {task.agent_type}\n"
                    f"Error: {task.error}\n\n"
                    f"IMPORTANT: The task has stopped. Do not say it is still running.\n"
                    f"Inform the user about the error."
                )
            })
        elif task.status == "timeout":
            UI.warning(f"⏰ Sub-Agent [{task.task_id}] did not respond (Timeout)")
            
            self.history.append({
                "role": "system",
                "content": (
                    f"⏰ **Sub-Agent Task TIMEOUT / TERMINATED** [Task: {task.task_id}]\n"
                    f"Agent: {task.agent_type}\n"
                    f"Der Sub-Agent hat nicht rechtzeitig geantwortet.\n\n"
                    f"Bitte informiere den User über dieses Problem."
                )
            })
        
        # Remove from tracking and consume from queue
        if task.task_id in self._async_subagent_tasks:
            del self._async_subagent_tasks[task.task_id]
        
        ipc.consume_result(task.task_id)
    
    def _handle_async_subagent_marker(self, result: str) -> bool:
        """
        Check if a tool result contains an async sub-agent marker.
        If so, track the task and return True.
        
        Args:
            result: Tool result string
            
        Returns:
            True if this was an async sub-agent task, False otherwise
        """
        import re
        from datetime import datetime
        
        # Pattern: [SUBAGENT_ASYNC:task_id:agent_type]
        match = re.search(r'\[SUBAGENT_ASYNC:([^:]+):([^\]]+)\]', result)
        if match:
            task_id = match.group(1)
            agent_type = match.group(2)
            
            # Track the async task
            self._async_subagent_tasks[task_id] = {
                "agent_type": agent_type,
                "started_at": datetime.now()
            }
            
            return True
        return False
    
    def get_active_subagents(self) -> dict:
        """Get currently running async sub-agent tasks."""
        return self._async_subagent_tasks.copy()
    
    def has_pending_subagent_results(self) -> bool:
        """Check if there are any pending sub-agent results."""
        try:
            from vaf.core.subagent_ipc import get_ipc
            return get_ipc().has_pending_results()
        except Exception:
            return False

    def load_model(self, skip_download_check: bool = False):
        from vaf.cli.ui import UI
        from vaf.core.gpu_detection import get_primary_gpu, _check_cuda_available
        
        # Skip model loading if using API backend
        if self.provider != "local":
            UI.event("System", f"Using API provider: {self.provider}, skipping model load", style="dim")
            return
        
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

        # API Provider Check (Best Practice: Use API if configured)
        if self.provider != "local":
            UI.event("System", f"Initializing API Backend: {self.provider.upper()}...", style="warning")
            try:
                from vaf.core.api_backend import APIBackendManager
                self.api_backend = APIBackendManager(self.provider)
                UI.event("Success", f"API Backend ready: {self.provider.upper()}", style="success")
                return  # Success - no local model needed
            except ValueError as e:
                UI.error(f"API Backend initialization failed: {e}")
                UI.event("System", "Falling back to local backend...", style="warning")
                # Fall through to local backend
        
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
        # Initialize Prompt Manager
        self.prompt_manager = SystemPromptManager(self.tools)
        
        # Build initial prompt (Core + Base Rules)
        # We pass self.filename to determine identity (VQ-1 vs Generic)
        system_prompt = self.prompt_manager.build_prompt(self.filename)
        
        # Optional: Load Project Context (VAF.md)
        # Limit to 25% of total context or max 12k chars (whichever is smaller)
        # 1 token ~ 3 chars -> 25% of n_ctx tokens * 3 = max chars
        n_ctx = self.config.get("n_ctx", 8192)
        max_context_chars = int(min(12_000, (n_ctx * 0.25) * 3))
        
        try:
            from pathlib import Path
            from vaf.core.project_context import load_project_context
            # Use current working directory for context search
            cwd = os.getcwd()
            project_ctx = load_project_context(Path(cwd), max_chars=max_context_chars)
            
            if project_ctx:
                 system_prompt += f"\n\n## PROJECT CONTEXT (VAF.md)\nLoaded from: {project_ctx.path}\n{project_ctx.content}\n"
        except Exception:
            pass

        self.history = [
            {"role": "system", "content": system_prompt}
        ]

    def _clean_reasoning(self, text: str) -> str:
        """Removes internal reasoning/CoT blocks from the model response."""
        import re
        t = text
        
        # 1. Remove XML-style thinking blocks
        t = re.sub(r'<think>.*?</think>', '', t, flags=re.DOTALL)
        t = re.sub(r'<redacted_reasoning>.*?</redacted_reasoning>', '', t, flags=re.DOTALL)
        
        # 2. Remove VQ-1 specific thinking patterns
        # These are patterns where the model "talks to itself" at the start of a response
        thought_patterns = [
            r'^Okay, the user.*?(?:\n\n|\n|\Z)',
            r'^First, I should.*?(?:\n\n|\n|\Z)',
            r'^Let me check.*?(?:\n\n|\n|\Z)',
            r'^I need to.*?(?:\n\n|\n|\Z)',
            r'^I will.*?(?:\n\n|\n|\Z)',
            r'^The user wants.*?(?:\n\n|\n|\Z)',
        ]
        for pattern in thought_patterns:
            # Loop to remove multiple paragraphs of thinking
            while re.search(pattern, t, flags=re.DOTALL | re.MULTILINE | re.IGNORECASE):
                t = re.sub(pattern, '', t, count=1, flags=re.DOTALL | re.MULTILINE | re.IGNORECASE).strip()

        # 3. Aggressive Reasoning Filter: "Answer in [Lang]"
        # If the model explicitly tells itself to answer in a language, 
        # everything BEFORE that instruction is likely reasoning/garbage.
        answer_instruction = re.search(r'(?:Answer|Antworte|Respond) (?:in|auf) [A-Z][a-z]+(?: \([A-Z][a-z]+\))?[\.:]?\s*', t, flags=re.IGNORECASE)
        if answer_instruction:
             # Keep only what comes AFTER "Answer in German."
             t = t[answer_instruction.end():].strip()
        
        return t.strip()

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
        # 1. Strong hardcoded markers (Fast & Accurate for specific context)
        # Check for Umlauts (German)
        if any(ch in t for ch in ("ä", "ö", "ü", "ß")):
            return "de"

        # Check for strong German keywords
        german_cues = (
            "kannst", "bitte", "wetter", "morgen", "heute", "gestern", "wie ", "was ", "wo ", "warum",
            "ich ", "du ", "wir ", "ihr ", "nicht", "und", "für", "über", "dass", "mach", "erstelle", "zeige",
            "ein ", "eine ", "der ", "die ", "das ", "den ", "dem ", "des ", "ist ", "sind ", "hat ", "haben ", 
            "auf ", "mit ", "von ", "zu ", "bei ", "oder ", "aber ", "als ", "wenn ", "lesen", "dokumente",
        )
        if any(cue in t for cue in german_cues):
            return "de"

        # Check for strong English keywords
        english_cues = (
            "please", "weather", "tomorrow", "today", "yesterday", "how ", "what ", "where ", "why ",
            "i ", "you ", "we ", "they ", "don't", "and", "for", "about", "make", "create", "show",
            "read", "document", "can ", "is ", "are ", "the ", "with ", "from ",
        )
        if any(cue in t for cue in english_cues):
            return "en"

        # 2. Fallback to langid (Probabilistic / General Purpose)
        try:
            # langid is pure-Python and supports many languages (offline).
            import langid  # type: ignore

            # Use langid for detection. It is generally robust even for short phrases.
            # We relax the length constraint to > 3 chars to catch "Was ist das" etc.
            if len(t) >= 3 and any(ch.isalpha() for ch in t):
                code, score = langid.classify(t)
                code = (code or "").strip().lower()
                
                if code:
                    # Normalize some common variants
                    if code == "iw":  # legacy Hebrew code sometimes seen
                        code = "he"
                    
                    # Return if valid code
                    return code
        except Exception:
            pass

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

    def _check_language_mismatch(self, user_input: str, assistant_response: str) -> bool:
        """
        Check if the assistant responded in a different language than the user.
        If so, inject a system warning into history.
        
        Returns:
            True if mismatch detected and correction requested, False otherwise.
        """
        # ... (rest of implementation)
        
        # Determine language (simplified check for common cases)
        # Using langid for robust detection
        try:
            import langid
            user_lang, _ = langid.classify(user_input)
            response_lang, _ = langid.classify(assistant_response)
        except ImportError:
            # Fallback if langid missing
            return False
        
        # Skip if either is "auto" (unclear) or if they match
        if user_lang == "auto" or response_lang == "auto":
            return False
        if user_lang == response_lang:
            return False
        
        # INTELLIGENT check: Did the user explicitly request a translation?
        # Uses existing language detection, no hardcoded words!
        if self._user_requested_translation(user_input, response_lang):
            # User likely requested a translation - that's OK
            # (Silently ignored - no debug output needed)
            return False
        
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
            "uz": "Oʻzbek", "mn": "Mongol", "bo": "Tibetan",
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
                f"[!] **Sprach-Mismatch erkannt**: Du hast auf {response_lang_name} geantwortet, "
                f"aber der Nutzer spricht {user_lang_name}. "
                f"Bitte übersetze deine Antwort sofort ins {user_lang_name}."
            )
        elif user_lang in language_names:
            # Try to generate a warning in the user's language (simple approach)
            warning = (
                f"[!] **Language mismatch detected**: You responded in {response_lang_name}, "
                f"but the user is speaking {user_lang_name}. "
                f"Please translate your response immediately to {user_lang_name}."
            )
        else:
            # Fallback: bilingual
            warning = (
                f"[!] **Language mismatch**: You answered in {response_lang_name}, "
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
        
        return True

    def _refresh_language_hint(self, user_input: str) -> None:
        """
        Update LANGUAGE_HINT inside the main system prompt (history[0]).
        This ensures the current response language is consistently enforced,
        including in "no workflow match" situations and after context compression.
        """
        if not self.history or self.history[0].get("role") != "system":
            return

        # Use already detected language if available in prompt_manager
        if hasattr(self, 'prompt_manager') and self.prompt_manager.user_language != "auto":
            lang = self.prompt_manager.user_language
        else:
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
        # API Backend: Return message count instead of tokens
        if self.api_backend:
            # Count input/output messages (like a notebook)
            user_msgs = sum(1 for m in self.history if m.get("role") == "user")
            assistant_msgs = sum(1 for m in self.history if m.get("role") == "assistant")
            # Return as (input_count, output_count) - will be displayed as In[X] Out[Y]
            return user_msgs, assistant_msgs
        
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
        Brain-powered workflow selection with reasoning process.
        LLM decides based on INTENT and THINKING, not trigger matching!
        Similar to adaptive temperature system.
        
        Returns:
            (workflow_id, tier) tuple where tier is 1, 2, or 3
            or (None, 2) if no match (Tier 2: Agent Choice)
        """
        # Track which tier was used (for status display)
        self._workflow_selection_tier = 1  # Default: Tier 1 (LLM Reasoning)
        
        try:
            from vaf.workflows.templates import WORKFLOW_TEMPLATES, list_templates
            
            # Get available workflows (NO trigger examples - LLM must think!)
            available_workflows = list_templates()
            workflow_list = "\n".join([
                f"- {w['id']}: {w['description']}"
                for w in available_workflows
            ])
            
            prompt = (
                f"You are an intelligent workflow orchestrator. Analyze the request and select the most appropriate workflow.\n\n"
                f"Available Workflows:\n{workflow_list}\n\n"
                f"═══════════════════════════════════════════════════════════\n"
                f"REASONING PROCESS (think step-by-step):\n"
                f"═══════════════════════════════════════════════════════════\n\n"
                f"1. INTENT ANALYSIS:\n"
                f"   - What is the user trying to achieve?\n"
                f"   - What output type (document/code/data/analysis)?\n\n"
                f"2. RESEARCH REQUIREMENT:\n"
                f"   - Does this need current/external information?\n"
                f"   - Keywords: recherche, research, rechtssicher, legally sound, aktuell, current, basierend auf\n\n"
                f"3. OUTPUT TYPE:\n"
                f"   - Legal contract (Vertrag, contract, Arbeitsvertrag, Mietvertrag)?\n"
                f"   - Technical docs (API, guide, manual, technisch, dokumentation)?\n"
                f"   - General document (report, letter, bericht, brief)?\n"
                f"   - Code/implementation?\n"
                f"   - Website/HTML?\n\n"
                f"4. COMPLEXITY:\n"
                f"   - Multi-stage (research → create)?\n"
                f"   - Scheduled/automated (time mentioned)?\n"
                f"   - Simple creation?\n"
                f"   - Simple lookup (no workflow)?\n\n"
                f"═══════════════════════════════════════════════════════════\n"
                f"DECISION RULES (prioritized by specificity):\n"
                f"═══════════════════════════════════════════════════════════\n\n"
                f"Priority 1 - Scheduled/Automated Tasks:\n"
                f"  • TIME mentioned (21:07, um 9:00, at 10am, täglich, daily) → create_scheduled_task\n\n"
                f"Priority 2 - Research + Legal Contracts:\n"
                f"  • (rechtssicher OR legally sound) + contract/vertrag → legal_contract_research\n"
                f"  • Contract + (research OR recherche OR current laws) → legal_contract_research\n\n"
                f"Priority 3 - Research + Technical Docs:\n"
                f"  • (technical OR technisch) + (research OR recherche) → technical_doc_research\n"
                f"  • API/guide/manual + research → technical_doc_research\n\n"
                f"Priority 4 - Research + General Document:\n"
                f"  • (research OR recherche) + document/guide/report → research_and_document\n"
                f"  • 'basierend auf recherche' + document → research_and_document\n\n"
                f"Priority 5 - Research + Code:\n"
                f"  • (research OR recherche) + (code OR implement) → research_and_code\n\n"
                f"Priority 6 - Simple Creation (no research):\n"
                f"  • Website/HTML → create_website\n"
                f"  • Document without research → create_document\n"
                f"  • File creation → create_file\n\n"
                f"Priority 7 - Analysis:\n"
                f"  • Deep research (10 sources, multi-perspective) → deep_research\n"
                f"  • Website analysis → analyze_website\n\n"
                f"🚨 Priority 8 - NO WORKFLOW (Agent handles with direct tools):\n"
                f"  • Simple lookups (weather, news, facts, 'what is X?') → none\n"
                f"  • Multiple simple questions ('Weather + News') → none (agent calls web_search 2x!)\n"
                f"  • Person queries ('Who is X?') → none (agent uses web_search)\n"
                f"  • File/folder locations → none (agent uses librarian)\n"
                f"  • Single tool usage → none\n"
                f"  • Quick status checks → none\n\n"
                f"🔥 CRITICAL: 'Weather + News' = none (NOT deep_research!)\n"
                f"🔥 CRITICAL: 'Politics + Finance news' = none (NOT deep_research!)\n\n"
                f"═══════════════════════════════════════════════════════════\n\n"
                f"Request: \"{user_input}\"\n\n"
                f"Think carefully about INTENT, then output ONLY the workflow_id or 'none'."
            )
            
            # Quick Inference with reasoning (temperature 0.2 for consistent logic)
            messages = [{"role": "user", "content": prompt}]
            
            content = ""
            if self.use_server:
                # Full thinking capacity with 120s timeout
                payload = {"messages": messages, "max_tokens": 1024, "temperature": 0.2}
                try:
                    res = requests.post("http://127.0.0.1:8080/v1/chat/completions", json=payload, timeout=120).json()
                    content = res['choices'][0]['message']['content']
                except Exception as e:
                    # ERROR: LLM server request failed
                    # Return None → Tier 2 (Agent Choice) will handle
                    self._workflow_selection_tier = 2  # Agent will get workflow list
                    return None
            else:
                # No LLM server available → Use Tier 3 (Pattern Matching) immediately
                self._workflow_selection_tier = 3
                from vaf.workflows.selector import WorkflowSelector
                selector = WorkflowSelector()
                result = selector.select(user_input)
                if result and result.matched and result.confidence >= 0.5:
                    return result.template_id
                # Even pattern matching found nothing → Tier 2 (Agent Choice)
                self._workflow_selection_tier = 2
                return None
            
            # Parse workflow ID (dynamically from all available workflows)
            workflow_ids_pattern = '|'.join(re.escape(wf_id) for wf_id in WORKFLOW_TEMPLATES.keys())
            match = re.search(rf'\b({workflow_ids_pattern})\b', content.lower())
            if match:
                workflow_id = match.group(1)
                if workflow_id in WORKFLOW_TEMPLATES:
                    return workflow_id
            
            # Check for "none" response
            if "none" in content.lower():
                return None
            
            # If we couldn't parse the LLM response:
            # Return None → Tier 2 (Agent Choice) will handle
            # Agent gets workflow list and can decide
            self._workflow_selection_tier = 2  # Let agent choose from list
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
            
        # Determine language for UI messages
        lang = "auto"
        if hasattr(self, 'prompt_manager') and self.prompt_manager.user_language != "auto":
            lang = self.prompt_manager.user_language
        else:
            lang = self._detect_user_language(user_input)
            
        # Fallback to config if detection failed
        if lang == "auto":
            lang = (self.config.get("language", "auto") or "auto").strip().lower()

        # If still auto, try to use speech_language preference as fallback for fillers
        if lang == "auto":
            speech_lang = self.config.get("speech_language", "")
            if speech_lang:
                lang = speech_lang[:2].lower() # e.g. "de-DE" -> "de"

        # Localized messages
        msg_analyzing = "Step 1/2: Analyzing workflow match..."
        msg_brain_matched_ui = "Selected: {name} (multi-language support!)"
        msg_extracting = "Extracting variables from user input..."
        msg_running_separate = "Running in separate terminal [Task: {task_id}]"
        msg_runs_independently = "[>] Workflow runs independently. Result will be reported when done."
        msg_async_return = "[WORKFLOW_ASYNC:{task_id}:{workflow_id}] Workflow '{name}' is running in a separate terminal.\n\nYou can continue using me while the workflow runs. I'll notify you when the result is ready."
        msg_paused_ui = "⏸️  Workflow paused - waiting for sub-agent [Task: {task_id}]"
        msg_paused_hint = "💡 You can continue using VAF. The workflow will resume automatically when the sub-agent finishes."
        msg_paused_return = "⏸️ Workflow '{workflow_id}' is paused, waiting for a sub-agent to complete.\n\nYou can continue using me while we wait. The workflow will automatically resume when the result is ready."
        
        if lang == "de":
            msg_analyzing = "Schritt 1/2: Analysiere Workflow-Übereinstimmung..."
            msg_brain_matched_ui = "Ausgewählt: {name} (Mehrsprachige Unterstützung!)"
            msg_extracting = "Extrahiere Variablen aus Benutzereingabe..."
            msg_running_separate = "Läuft in separatem Terminal [Task: {task_id}]"
            msg_runs_independently = "[>] Workflow läuft unabhängig. Ergebnis wird gemeldet, wenn fertig."
            msg_async_return = "[WORKFLOW_ASYNC:{task_id}:{workflow_id}] Workflow '{name}' läuft in einem separaten Terminal.\n\nIch bin weiter für dich da, während der Workflow läuft. Ich melde mich, wenn das Ergebnis da ist."
            msg_paused_ui = "⏸️  Workflow pausiert - warte auf Sub-Agent [Task: {task_id}]"
            msg_paused_hint = "💡 Du kannst VAF weiter nutzen. Der Workflow wird automatisch fortgesetzt, wenn der Sub-Agent fertig ist."
            msg_paused_return = "⏸️ Workflow '{workflow_id}' ist pausiert und wartet auf einen Sub-Agent.\n\nIch bin weiter für dich da, während wir warten. Der Workflow wird automatisch fortgesetzt, wenn das Ergebnis da ist."
        
        try:
            from vaf.workflows import WorkflowSelector, WorkflowEngine, create_workflow
            
            # Check if workflows are enabled (can be disabled in config)
            if not self.config.get("workflows_enabled", True):
                return None
            
            # BRAIN-BASED WORKFLOW SELECTION (multi-language support!)
            # Instead of hardcoded pattern matching, use LLM to understand intent in ANY language
            from vaf.cli.ui import UI as UI_Class
            
            # PLAY THINKING FILLER HERE (to mask latency)
            try:
                from vaf.core.speech import get_speech_manager
                from vaf.core.speech_fillers import THINKING_FILLERS
                import random
                
                sm = get_speech_manager()
                if sm.is_tts_enabled():
                    # Get generic thinking fillers for current language
                    fillers = THINKING_FILLERS.get(lang, THINKING_FILLERS.get("en", []))
                    if fillers:
                        filler = random.choice(fillers)
                        sm.speak(filler, lang=lang)
            except Exception:
                pass  # Ignore speech errors during thinking
            
            with UI_Class.console.status(f"[bold cyan](O_O)  {msg_analyzing}[/bold cyan]", spinner="dots"):
                workflow_id = self.analyze_workflow(user_input)
            
            if not workflow_id:
                # No workflow match - give agent brief hint (not full list!)
                # Agent can use list_workflows tool if they need to see options
                self.history.append({
                    "role": "system",
                    "content": (
                        "ℹ️ No workflow automatically matched for this request. "
                        "You can handle it directly with your tools, or if you think a multi-step workflow "
                        "would be beneficial, use the 'list_workflows' tool to see available options. "
                        "Most simple requests (weather, news, questions) don't need workflows."
                    )
                })
                
                # Show Tier 2 status (Agent Choice)
                UI.event("Step 1/2", f"Workflow [Tier 2: No auto-match - Agent deciding]", style="cyan")
                return None
            
            # Get the matched template
            from vaf.workflows.templates import get_template
            template = get_template(workflow_id)
            if not template:
                return None
            
            # Show workflow selection status with tier information
            tier = getattr(self, '_workflow_selection_tier', 1)
            tier_names = {
                1: "LLM Reasoning",
                2: "Agent Choice", 
                3: "Pattern Matching"
            }
            tier_name = tier_names.get(tier, "Unknown")
            
            UI.event("Step 1/2", f"Workflow [Tier {tier}: {tier_name} → '{workflow_id}']", style="bold cyan")
            UI.event("Workflow", msg_brain_matched_ui.format(name=template['name']), style="bold cyan")
            
            # Extract variables using WorkflowSelector (pattern matching + fallback)
            from vaf.cli.ui import UI
            UI.event("Brain", msg_extracting, style="dim")
            
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
            # Set workflow name for paused state tracking
            engine._workflow_name = workflow_id
            
            # ═══════════════════════════════════════════════════════════════
            # ASYNC WORKFLOW: Run entire workflow in separate terminal
            # ═══════════════════════════════════════════════════════════════
            # When sub_agents_in_separate_terminals is enabled, spawn the
            # ENTIRE workflow in a new terminal. This prevents context overflow
            # because large intermediate results (like HTML reports) never
            # touch the main agent's context.
            if self.config.get("sub_agents_in_separate_terminals", False):
                # Don't spawn if already in a workflow/subagent terminal
                in_workflow_terminal = os.environ.get("VAF_IN_WORKFLOW_TERMINAL", "").strip() in ("1", "true", "yes")
                in_subagent_terminal = os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "").strip() in ("1", "true", "yes")
                
                if not in_workflow_terminal and not in_subagent_terminal:
                    # Create IPC task
                    from vaf.core.subagent_ipc import get_ipc, get_current_session_id
                    ipc = get_ipc()
                    task_id = ipc.create_task(
                        agent_type=f"workflow:{workflow_id}",
                        task_description=user_input,
                        session_id=get_current_session_id()
                    )
                    
                    # Build command to run workflow in separate terminal
                    import json as json_module
                    import shlex
                    
                    # Serialize variables to JSON
                    variables_json = json_module.dumps(result.variables)
                    
                    from vaf.core.platform import Platform
                    
                    # Pass session ID to workflow terminal
                    session_id = get_current_session_id()
                    if session_id:
                        os.environ["VAF_SESSION_ID"] = session_id
                    
                    # Pass Language Hint to workflow terminal
                    if hasattr(self, 'prompt_manager') and self.prompt_manager.user_language:
                        os.environ["VAF_USER_LANGUAGE"] = self.prompt_manager.user_language
                    
                    # Build command with proper escaping for the platform
                    if Platform.is_windows():
                        # Windows: escape double quotes in JSON
                        escaped_json = variables_json.replace('"', '\\"')
                        cmd = f'vaf workflow run "{workflow_id}" --variables "{escaped_json}" --task-id {task_id}'
                    else:
                        # Unix: use shlex.quote for proper escaping
                        cmd = f'vaf workflow run "{workflow_id}" --variables {shlex.quote(variables_json)} --task-id {task_id}'
                    
                    Platform.open_new_terminal(cmd, title=f"VAF Workflow: {workflow_id}")
                    
                    UI.event("Workflow", msg_running_separate.format(task_id=task_id[:8]), style="cyan")
                    UI.info(msg_runs_independently)
                    
                    # Return async marker
                    return msg_async_return.format(task_id=task_id, workflow_id=workflow_id, name=template['name'])
            
            # Execute workflow inline (without defaults parameter)
            workflow_result = engine.execute(steps, variables=result.variables)
            
            # Handle paused workflows (async sub-agent case)
            if workflow_result.paused:
                UI.event("Workflow", msg_paused_ui.format(task_id=workflow_result.waiting_for_task), style="cyan")
                UI.info(msg_paused_hint)
                return msg_paused_return.format(workflow_id=workflow_id)
            
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
                                        f"\n\n[!] **Note**: Some tasks may still be incomplete. "
                                        f"The project is at: `{project_path_hint}`\n"
                                        f"You can continue working on it or ask for specific changes."
                                    )
                                    final_output += f"\n\n---\n{continuation_result}"
                            else:
                                incomplete_tasks_hint = (
                                    f"\n\n[!] **Note**: The coding agent has incomplete tasks. "
                                    f"To complete all remaining tasks, use:\n"
                                    f"`coding_agent(task=\"continue and complete all remaining tasks\", project_path=\"{project_path_hint}\")`"
                                )
                        else:
                            incomplete_tasks_hint = (
                                f"\n\n[!] **Note**: The coding agent has incomplete tasks. "
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

    def _clean_workflow_context(self):
        """
        Remove old workflow system messages from conversation history.
        Prevents workflow lists from cluttering context.
        """
        self.history = [
            msg for msg in self.history 
            if not (msg.get("role") == "system" and 
                    "Available workflows:" in msg.get("content", ""))
        ]
    
    def chat_step(self, user_input: str, stream_callback=None, auto_retry=False, skip_input=False, disable_workflows=False, disable_tools=False):
        from vaf.cli.ui import UI
        
        # Check if any backend is available (local, server, or API)
        if not self.llm and not self.use_server and not self.api_backend:
            UI.error("Agent not initialized. Run 'vaf run' first.")
            return
        
        # Clean old workflow messages from context (prevents clutter)
        self._clean_workflow_context()

        # ------------------------------------------------------------------
        # Sub-Agent Results: Check for completed async tasks
        # ------------------------------------------------------------------
        pending_results = self._check_subagent_results()
        if pending_results:
            for task in pending_results:
                self._process_subagent_result(task)
            
            # If we processed results and no new user input, let model respond to results
            if not user_input and not skip_input:
                 # Only auto-inject generic prompt if we DON'T have a specific input
                 # and we're not in a skip_input mode (which usually implies internal control)
                 self.history.append({
                    "role": "user",
                    "content": "[System: Sub-Agent results have arrived. Please inform me about the results.]"
                })

        # ------------------------------------------------------------------
        # Dynamic Context: Update System Prompt
        # ------------------------------------------------------------------
        if hasattr(self, 'prompt_manager') and user_input and not skip_input:
            # Detect language first so it can be used in build_prompt (e.g. for localized date)
            # Respect configured language if set
            configured_lang = (self.config.get("language", "auto") or "auto").strip().lower()
            if configured_lang in ("de", "en"):
                lang = configured_lang
            else:
                lang = self._detect_user_language(user_input)
            
            self.prompt_manager.user_language = lang
            
            # Analyze intent and active relevant modules
            self.prompt_manager.analyze_context(user_input, language=lang)
            
            # Rebuild system prompt
            new_prompt = self.prompt_manager.build_prompt(self.filename)
        
        # ------------------------------------------------------------------
        # Context Compression: Check threshold and compress if needed
        # ------------------------------------------------------------------
        if hasattr(self, 'context_manager') and self.context_manager.should_compress(self.history):
            UI.event("Context", f"Threshold reached ({self.context_manager.get_usage_percent(self.history):.0%}) - compressing...", style="warning")
            self.history = self.context_manager.compress(self.history)
            
            # Inject PROACTIVE CONTEXT GLUE (Stability)
            # This ensures the agent always knows which files exist, what errors happened, etc.
            context_glue = self.context_manager._build_context_summary()
            if context_glue:
                new_prompt += f"\n\n{context_glue}"
            
            # Preserve Project Context if it exists
            if len(self.history) > 0 and self.history[0]["role"] == "system":
                current_content = self.history[0]["content"]
                if "## PROJECT CONTEXT" in current_content:
                    project_context_part = current_content.split("## PROJECT CONTEXT", 1)[1]
                    new_prompt += f"\n\n## PROJECT CONTEXT{project_context_part}"
                
                # Update system prompt in history
                self.history[0]["content"] = new_prompt
                # UI.event("Brain", f"Context adjusted: {list(self.prompt_manager.active_modules.keys())}", style="dim")

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
        
        workflow_tried = False
        if not skip_input and not disable_workflows:
            workflow_tried = True
            # Try workflow matching BEFORE adding to history
            workflow_result = self._try_workflow(user_input, stream_callback)
            if workflow_result:
                # Workflow executed successfully - return result
                return workflow_result
            # No workflow match or workflow failed - continue with LLM agent
        
        # Snapshot history (before adding user input if not skipped)
        history_snapshot_len = len(self.history)
        
        # Always add user input if provided, even if skip_input=True (which skips analysis/overhead)
        if user_input:
            self.history.append({"role": "user", "content": user_input})
        
        if not skip_input and user_input:
            # 0.5. Context Management AGAIN - AFTER adding user input (in case it pushed us over limit)
            self.manage_context()
            # Re-apply after potential compression to ensure the hint stays in history[0].
            self._refresh_language_hint(user_input)

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
        
        # TTS Filler: "Einen kleinen Moment..." (avoid dead silence)
        # Skip if already played during workflow match analysis (Step 1/2)
        if not workflow_tried:
            self._speak_filler("thinking")
        
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
            auto_continue = False  # Track if response was cut off
            
            # API Backend Path (OpenAI, Anthropic, DeepSeek, Google, OpenRouter)
            if self.api_backend:
                try:
                    # Prepare messages
                    prepared_messages = self._prepare_messages(self.history)
                    
                    # Disable tools if requested
                    current_tools = self.TOOLS if not disable_tools else None
                    
                    first_token = True
                    for chunk in self.api_backend.chat_completion(
                        messages=prepared_messages,
                        temperature=current_temp,
                        max_tokens=8192,
                        stream=True,
                        tools=current_tools
                    ):
                        # Handle JSON chunks (Tools or Finish Reason)
                        if chunk.startswith("{"):
                            try:
                                data = json.loads(chunk)
                                
                                # Handle Finish Reason (e.g. "length")
                                if "finish_reason" in data:
                                    if data["finish_reason"] == "length":
                                        auto_continue = True
                                        UI.event("System", "Response cut off - Auto-continuing...", style="dim")
                                
                                # Handle Tools
                                elif "tool_calls" in data:
                                    for tc in data["tool_calls"]:
                                        tool_calls_detected.append(tc)
                                elif "tool_use" in data:
                                    # Anthropic format conversion
                                    tool_calls_detected.append({
                                        "function": {
                                            "name": data["tool_use"].get("name"),
                                            "arguments": json.dumps(data["tool_use"].get("input", {}))
                                        }
                                    })
                            except json.JSONDecodeError:
                                pass
                        else:
                            # Regular content
                            if first_token:
                                if stream_callback: stream_callback("")
                                first_token = False
                            
                            if stream_callback:
                                stream_callback(f"{escape(chunk)}")
                            full_response += chunk
                            full_content += chunk
                    
                    if stream_callback: stream_callback("\n")
                    
                except Exception as e:
                    UI.error(f"API Backend Error: {e}")
                    return
            
            elif self.use_server:
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
                        # Prepare messages for specific model quirks (e.g. Gemma)
                        prepared_messages = self._prepare_messages(self.history)
                        
                        # Disable tools if requested (forces text response)
                        current_tools = self.TOOLS if not disable_tools else None
                        current_tool_choice = "auto" if not disable_tools else "none"
                        
                        payload = {
                             "messages": prepared_messages,
                             "tools": current_tools, 
                             "tool_choice": current_tool_choice,
                             "stream": True,
                             "temperature": current_temp,
                        }
                        
                        # Remove keys if None (some APIs don't like null tools)
                        if current_tools is None:
                            if "tools" in payload: del payload["tools"]
                            if "tool_choice" in payload: del payload["tool_choice"]
                        
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
                                UI.event("Context", "Context overflow detected (500). Compressing history...", style="warning")
                                # 1. Standard Compression
                                self.manage_context()
                                
                                # 2. Aggressive Content Truncation (if Standard wasn't enough)
                                truncated = False
                                for msg in self.history:
                                    if msg.get("role") != "system":
                                        content = str(msg.get("content", ""))
                                        if len(content) > 6000:
                                            msg["content"] = content[:6000] + "... [TRUNCATED FOR RECOVERY]"
                                            truncated = True
                                
                                if truncated:
                                    UI.event("Context", "Truncated large messages for recovery.", style="info")

                                # 3. Message Pruning (existing logic)
                                if len(self.history) > 20:
                                    # Keep system prompt, last user message, and last 10 messages
                                    system_msgs = [m for m in self.history if m.get("role") == "system"]
                                    user_msgs = [m for m in self.history if m.get("role") == "user"]
                                    assistant_msgs = [m for m in self.history if m.get("role") == "assistant"]
                                    
                                    # Keep system prompt + last 6 messages (preserving order and alternation)
                                    # This ensures we don't break User -> Assistant -> User flow
                                    new_history = []
                                    
                                    # 1. System Prompt
                                    if self.history and self.history[0].get("role") == "system":
                                        new_history.append(self.history[0])
                                    
                                    # 2. Last 6 messages (User/Assistant/Tool)
                                    # Ensure we include the latest interaction
                                    recent = self.history[-6:]
                                    
                                    # Truncate heavy messages in the recent block to save space
                                    for msg in recent:
                                        # Use copy to avoid mutating if we refer to same dict objects
                                        msg_copy = msg.copy()
                                        content = str(msg_copy.get("content", ""))
                                        if len(content) > 2000:
                                             msg_copy["content"] = content[:2000] + "... [TRUNCATED]"
                                        new_history.append(msg_copy)
                                    
                                    self.history = new_history
                                    UI.event("Context", f"Compressed to {len(self.history)} messages (Aggressive Pruning - Order Preserved).", style="info")
                                
                                UI.event("Context", "Retrying request with optimized context...", style="success")
                                # Retry the request with compressed context (payload will be rebuilt in next iteration)
                                continue
                        
                        # Handle Context Size Error (400) - automatically compress and retry
                        if response.status_code == 400:
                            try:
                                error_data = response.json()
                                error_msg = error_data.get("error", {}).get("message", "")
                                if "exceed_context_size" in error_msg.lower() or "exceed" in error_msg.lower():
                                    UI.event("Context", "Context size exceeded. Compressing history...", style="warning")
                                    # 1. Standard Compression
                                    self.manage_context()
                                    
                                    # 2. Aggressive Content Truncation (if Standard wasn't enough or history is short but fat)
                                    # Truncate very large messages to ensure we fit
                                    truncated = False
                                    for msg in self.history:
                                        if msg.get("role") != "system": # Protect system prompt
                                            content = str(msg.get("content", ""))
                                            if len(content) > 6000: # 6000 chars ~ 2000 tokens
                                                msg["content"] = content[:6000] + "... [TRUNCATED FOR RECOVERY]"
                                                truncated = True
                                    
                                    if truncated:
                                        UI.event("Context", "Truncated large messages for recovery.", style="info")

                                    # 3. Message Pruning (existing logic for long history)
                                    if len(self.history) > 20:
                                        # Keep system prompt, last user message, and last 10 messages
                                        system_msgs = [m for m in self.history if m.get("role") == "system"]
                                        user_msgs = [m for m in self.history if m.get("role") == "user"]
                                        assistant_msgs = [m for m in self.history if m.get("role") == "assistant"]
                                        
                                        # Keep system prompt + last 6 messages (preserving order and alternation)
                                        # This ensures we don't break User -> Assistant -> User flow
                                        new_history = []
                                        
                                        # 1. System Prompt
                                        if self.history and self.history[0].get("role") == "system":
                                            new_history.append(self.history[0])
                                        
                                        # 2. Last 6 messages (User/Assistant/Tool)
                                        recent = self.history[-6:]
                                        
                                        # Truncate heavy messages
                                        for msg in recent:
                                            msg_copy = msg.copy()
                                            content = str(msg_copy.get("content", ""))
                                            if len(content) > 2000:
                                                 msg_copy["content"] = content[:2000] + "... [TRUNCATED]"
                                            new_history.append(msg_copy)
                                        
                                        self.history = new_history
                                        UI.event("Context", f"Compressed to {len(self.history)} messages (Aggressive Pruning - Order Preserved).", style="info")
                                    
                                    UI.event("Context", "Retrying request with optimized context...", style="success")
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
                                            f"[!] STOP! You tried to call '{tool_name}' again after it failed.\n"
                                            f"The tool returned an error: {last_tool_msg.get('content', '')}\n"
                                            f"DO NOT retry failed tools immediately. Fix the arguments or inform the user."
                                        )
                                    })
                                    continue
                                
                                # 2. Check for SUCCESSFUL REPETITION (New Logic)
                                # If args match (strict check) and result wasn't error -> Block
                                # This prevents double-execution loops
                                try:
                                    # Strict check: only block if arguments are IDENTICAL to the last call
                                    last_call_id = last_tool_msg.get('tool_call_id')
                                    should_block = False
                                    
                                    if last_call_id:
                                        # Find the assistant message that made this call
                                        # Search backwards from the tool message index (i)
                                        for h_idx in range(i - 1, -1, -1):
                                            m = self.history[h_idx]
                                            if m.get('role') == 'assistant' and 'tool_calls' in m:
                                                found_call = False
                                                for prev_tc in m['tool_calls']:
                                                    if prev_tc.get('id') == last_call_id:
                                                        # Found the original call! Compare args.
                                                        prev_args = prev_tc['function']['arguments']
                                                        curr_args = tool_data['arguments']
                                                        
                                                        # Normalize JSON strings for comparison
                                                        try:
                                                            p_json = json.loads(prev_args) if isinstance(prev_args, str) else prev_args
                                                            c_json = json.loads(curr_args) if isinstance(curr_args, str) else curr_args
                                                            # Compare dictionaries/lists directly
                                                            if p_json == c_json:
                                                                should_block = True
                                                        except:
                                                            # Fallback to string comparison
                                                            if str(prev_args).strip() == str(curr_args).strip():
                                                                should_block = True
                                                        found_call = True
                                                        break
                                                if found_call: break

                                    if should_block:
                                        UI.event("Warning", f"Blocked redundant tool call: {tool_name}", style="warning")
                                        self.history.append({
                                            "role": "system",
                                            "content": (
                                                f"[!] STOP! You just executed '{tool_name}' successfully with these EXACT arguments.\n"
                                                f"The result is already in the context above (look for the 'tool' message).\n"
                                                f"DO NOT execute it again. Analyze the result and provide your answer."
                                            )
                                        })
                                        continue
                                except Exception as e:
                                    # If check fails, assume it's safe to proceed
                                    pass
                        
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
                    
                    # TTS Filler: "Ich suche im Internet..." (context-aware feedback)
                    # Extract query/context for filler
                    filler_query = None
                    if function_name == "web_search":
                        filler_query = arguments.get('query', '')
                    elif function_name in ("coding_agent", "research_agent"):
                        filler_query = arguments.get('task', '')
                    
                    self._speak_filler("tool", tool_name=function_name, query=filler_query)
                    
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

                    # Check if this is an async sub-agent task BEFORE adding to history
                    result_str = str(result) if result else ""
                    is_async_subagent = "[SUBAGENT_ASYNC:" in result_str
                    
                    if is_async_subagent:
                        # Replace the async marker with a clear "waiting" message for history
                        task_match = re.search(r'\[SUBAGENT_ASYNC:([^:]+):([^\]]+)\]', result_str)
                        task_id = task_match.group(1) if task_match else "unknown"
                        agent_type = task_match.group(2) if task_match else "sub-agent"
                        
                        # Clear tool response that indicates waiting - NO actual data
                        self.history.append({
                            "role": "tool",
                            "tool_call_id": tc['id'],
                            "name": function_name,
                            "content": (
                                f"[!] TASK DELEGATED TO SUB-AGENT - NO RESULT YET!\n\n"
                                f"Task-ID: {task_id}\n"
                                f"Agent: {agent_type}\n"
                                f"Status: RUNNING in separate terminal\n\n"
                                f"IMPORTANT: This tool call returned NO DATA.\n"
                                f"The sub-agent is still working. The result will appear later.\n"
                                f"DO NOT make up an answer! Just confirm the sub-agent is working."
                            )
                        })
                    else:
                        # ═══════════════════════════════════════════════════════════════
                        # SEAMLESS COMPRESSION: Prune large tool output while extracting facts
                        # ═══════════════════════════════════════════════════════════════
                        processed_result = self.context_manager.process_tool_output(function_name, result_str)
                        
                        self.history.append({
                            "role": "tool",
                            "tool_call_id": tc['id'],
                            "name": function_name,
                            "content": processed_result
                        })
                    
                    # Check if this was an async sub-agent task
                    if result and self._handle_async_subagent_marker(result_str):
                        # Extract task_id from marker for display
                        task_match = re.search(r'\[SUBAGENT_ASYNC:([^:]+):([^\]]+)\]', str(result))
                        task_id = task_match.group(1) if task_match else "unknown"
                        agent_type = task_match.group(2) if task_match else "sub-agent"
                        
                        # ═══════════════════════════════════════════════════════════════
                        # NON-BLOCKING: Register task and continue immediately
                        # ═══════════════════════════════════════════════════════════════
                        # The sub-agent runs in its own terminal. We don't wait - 
                        # the result will be picked up on the next chat interaction.
                        
                        UI.event("Sub-Agent", f"[>] {agent_type} [Task: {task_id}] running in background", style="bold magenta")
                        
                        # ═══════════════════════════════════════════════════════════════
                        # CRITICAL: Force agent to ONLY acknowledge, NOT answer
                        # ═══════════════════════════════════════════════════════════════
                        # Detect user's language from the last user message
                        user_lang = "auto"
                        for msg in reversed(self.history):
                            if msg.get("role") == "user":
                                user_lang = self._detect_user_language(msg.get("content", ""))
                                break
                        
                        # Programmatic response (no LLM generation)
                        # Prevents "blabbering" or hallucinations while sub-agent runs
                        if user_lang == "de":
                            response_text = (
                                f"Der {agent_type} arbeitet jetzt an deiner Anfrage [Task: {task_id}]. "
                                f"Ich melde mich, sobald das Ergebnis bereitsteht."
                            )
                        elif user_lang == "en":
                            # Default to English
                            response_text = (
                                f"The {agent_type} is now working on your request [Task: {task_id}]. "
                                f"I'll show you the result as soon as it's ready."
                            )
                        else:
                            # Dynamic translation for other languages (using isolated LLM call)
                            target_lang_name = self.LANGUAGE_NAMES_NATIVE.get(user_lang, "English")
                            base_msg = (
                                f"The {agent_type} is now working on your request [Task: {task_id}]. "
                                f"I'll show you the result as soon as it's ready."
                            )
                            
                            try:
                                # Use a fresh, stateless call to translate ONLY this message
                                # This prevents the main agent context from interfering ("blabbering")
                                translation_prompt = (
                                    f"Translate the following status message into {target_lang_name}.\n"
                                    "Keep the technical terms like '{agent_type}' and '[Task: {task_id}]' unchanged.\n"
                                    "Output ONLY the translation, nothing else.\n\n"
                                    f"Message: \"{base_msg}\""
                                )
                                
                                if self.use_server:
                                    # Server mode translation
                                    res = requests.post(
                                        "http://127.0.0.1:8080/v1/chat/completions",
                                        json={
                                            "model": self.config.get("model", ""),
                                            "messages": [{"role": "user", "content": translation_prompt}],
                                            "max_tokens": 100,
                                            "temperature": 0.1
                                        },
                                        timeout=10
                                    )
                                    if res.status_code == 200:
                                        content = res.json()['choices'][0]['message']['content'].strip()
                                        response_text = content if content else base_msg
                                    else:
                                        response_text = base_msg
                                else:
                                    # Local Llama translation
                                    res = self.llm.create_chat_completion(
                                        messages=[{"role": "user", "content": translation_prompt}],
                                        max_tokens=100,
                                        temperature=0.1
                                    )
                                    content = res['choices'][0]['message']['content'].strip()
                                    response_text = content if content else base_msg
                            except Exception:
                                # Fallback to English on error
                                response_text = base_msg
                            
                        # Add response to history
                        self.history.append({
                            "role": "assistant",
                            "content": response_text
                        })
                        
                        UI.event("System", "Async task started - returning status immediately", style="dim")
                        
                        # TTS for the acknowledgment (BEFORE return!)
                        # CRITICAL: Start TTS thread BEFORE returning, otherwise thread never starts
                        self._speak(response_text)
                        
                        # Give TTS thread time to start (threading.Thread.start() needs a moment)
                        # Without this, the return statement kills the function before thread starts
                        import time
                        time.sleep(0.05)  # 50ms is enough for thread initialization
                        
                        # Add a special marker that the CLI can use to force-print this message
                        return f"[ASYNC_ACK]{response_text}"
                    
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
                                f"[!] CRITICAL: The tool '{function_name}' FAILED with error: {result}\n\n"
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
                
                # ═══════════════════════════════════════════════════════════════
                # PROACTIVE CONTEXT CLEARING (User Request: "1 verschen" -> 15 attempts)
                # ═══════════════════════════════════════════════════════════════
                if empty_retry_count == 15:
                    # Calculate tokens before
                    tokens_before, _ = self.get_token_usage()
                    
                    UI.event("System", f"Early Warning ({empty_retry_count}) - Aggressive Context Clearing...", style="dim")
                    
                    # Preservation Strategy:
                    # 1. System Prompt (Index 0)
                    # 2. Snapshot (User Prompt + First Thinking) - controlled by history_snapshot_len
                    # 3. Last Message (System prompt injected at end of loop)
                    
                    # We want to clear the "middle" where the mess accumulates
                    if len(self.history) > history_snapshot_len + 2:
                        # Keep System Prompt + Snapshot (User Prompt)
                        # history_snapshot_len points to where the NEW content started for this turn
                        # We want to keep everything UP TO that snapshot point
                        kept_history = self.history[:history_snapshot_len]
                        
                        # Add user input if it was added this turn (it's usually at history_snapshot_len)
                        if len(self.history) > history_snapshot_len:
                             kept_history.append(self.history[history_snapshot_len])
                        
                        # Force update
                        self.history = kept_history
                        
                        # Calculate tokens after (estimate)
                        tokens_after, _ = self.get_token_usage()
                        
                        UI.event("Context", f"Cleared: {tokens_before} -> {tokens_after} Tokens | Snapshot preserved", style="dim")
                    else:
                         UI.event("Context", "History already minimal - skipping clear.", style="dim")
                
                # ═══════════════════════════════════════════════════════════════
                # PROACTIVE CONTEXT CLEARING (User Request: "1 verschen" -> 15 attempts)
                # ═══════════════════════════════════════════════════════════════
                if empty_retry_count == 15:
                    # Calculate tokens before
                    tokens_before, _ = self.get_token_usage()
                    
                    UI.event("System", f"Early Warning ({empty_retry_count}) - Aggressive Context Clearing...", style="dim")
                    
                    # Preservation Strategy:
                    # 1. System Prompt (Index 0)
                    # 2. Snapshot (User Prompt + First Thinking) - controlled by history_snapshot_len
                    # 3. Last Message (System prompt injected at end of loop)
                    
                    # We want to clear the "middle" where the mess accumulates
                    if len(self.history) > history_snapshot_len + 2:
                        # Keep System Prompt + Snapshot (User Prompt)
                        # history_snapshot_len points to where the NEW content started for this turn
                        # We want to keep everything UP TO that snapshot point
                        kept_history = self.history[:history_snapshot_len]
                        
                        # Add user input if it was added this turn (it's usually at history_snapshot_len)
                        if len(self.history) > history_snapshot_len:
                             kept_history.append(self.history[history_snapshot_len])
                        
                        # Force update
                        self.history = kept_history
                        
                        # Calculate tokens after (estimate)
                        tokens_after, _ = self.get_token_usage()
                        
                        UI.event("Context", f"Cleared: {tokens_before} -> {tokens_after} Tokens | Snapshot preserved", style="dim")
                    else:
                         UI.event("Context", "History already minimal - skipping clear.", style="dim")

                # ═══════════════════════════════════════════════════════════════
                # CONTEXT OVERFLOW DETECTION (Fix for Issue #VAF-CTX-001)
                # ═══════════════════════════════════════════════════════════════
                # If retry count exceeds 50 attempts, assume context overflow
                # LLM cannot generate response because context is too full
                MAX_RETRIES_BEFORE_EMERGENCY = 50
                
                # Extended limits to allow for auto-clearing at 50
                HARD_LIMIT = 70
                
                if empty_retry_count == MAX_RETRIES_BEFORE_EMERGENCY:
                    UI.event("System", f"High retry count ({empty_retry_count}) - Triggering Emergency Context Clearing", style="bold yellow")
                    # Force context management to reduce tokens
                    self.manage_context()
                    # Continue the loop to retry generation with reduced context
                
                elif empty_retry_count >= HARD_LIMIT:
                    UI.event("Emergency", f"Context overflow detected after {empty_retry_count} retries - emergency fallback!", style="warning")
                    
                    # Emergency fallback: Provide a SHORT summary instead of full response
                    emergency_summary = "⚠️ **Context Overflow Detected**\n\n"
                    emergency_summary += "The conversation has become too long for me to process effectively. "
                    emergency_summary += "I've attempted to respond multiple times but cannot generate a complete answer.\n\n"
                    
                    # Extract key info from recent context (last 3 tool results)
                    recent_tool_results = []
                    for i in range(len(self.history) - 1, max(0, len(self.history) - 20), -1):
                        msg = self.history[i]
                        if msg.get('role') == 'tool' and len(recent_tool_results) < 3:
                            tool_name = msg.get('name', 'unknown')
                            content = str(msg.get('content', ''))[:200]  # First 200 chars only
                            recent_tool_results.append(f"- **{tool_name}**: {content}...")
                    
                    if recent_tool_results:
                        emergency_summary += "**Recent tool results:**\n" + "\n".join(recent_tool_results) + "\n\n"
                    
                    emergency_summary += "**Suggestion:** Start a new conversation or ask me to focus on a specific aspect."
                    
                    # Return emergency response and break the loop
                    if stream_callback:
                        stream_callback(emergency_summary)
                    return emergency_summary
                
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
                if self._check_language_mismatch(user_input, history_content):
                    # Mismatch detected! The warning is already in history (as system msg).
                    # Now we must REMOVE the bad response and RETRY immediately.
                    
                    # 1. Remove the bad response (it was just added)
                    self.history.pop()  # Removes the assistant message
                    
                    # 2. Treat as a retry (reuses logic for patience/backoff if needed)
                    empty_retry_count += 1
                    
                    UI.event("System", "Triggering immediate retry for language correction...", style="warning")
                    
                    # 3. Restart loop - model will see the system warning and try again
                    continue
            
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
                 res = f"✅ Tool '{last_msg.get('name')}' finished: {last_msg.get('content')[:100]}..."
                 self._speak(res)
                 return res
                 
             # Case 2: Loop ended with Assistant Thought -> Model Silent (Previous was tool)
             if last_msg.get('role') == 'assistant' and prev_msg.get('role') == 'tool':
                  res = f"✅ Tool '{prev_msg.get('name')}' finished. (Model provided no commentary)"
                  self._speak(res)
                  return res

             self._speak("...")
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
                 target_len = history_snapshot_len 
                 self.history = self.history[:target_len]
                 return self.chat_step(user_input=user_input, stream_callback=stream_callback, auto_retry=True, skip_input=skip_input)
                 
             fallback_msg = "\n\n*(System: The agent processed the request but failed to generate a final answer. Please see the thought trace above.)*"
             if stream_callback: stream_callback(f"[gold]{fallback_msg}[/gold]")
             self.history.append({"role": "assistant", "content": fallback_msg}) 
             self._speak(fallback_msg)
             return fallback_msg

        # ═══════════════════════════════════════════════════════════════
        # TEXT-TO-SPEECH INTEGRATION
        # ═══════════════════════════════════════════════════════════════
        # CRITICAL: Use full_content (pure answer) if available to skip reasoning!
        tts_source = full_content if full_content.strip() else full_response
        
        # Play "answer ready" sound before TTS starts (after thinking is complete)
        if tts_source.strip():  # Only if we have actual content to speak
            try:
                from vaf.core.speech import get_speech_manager
                sm = get_speech_manager()
                sm.play_answer_ready_sound()
            except Exception:
                pass  # Silently fail - sound is optional
        
        self._speak(tts_source)

        # Return CLEANED response for the UI (Answer Box)
        # The raw response is already stored in history, so we don't lose information.
        return self._clean_reasoning(full_response)

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
            # Sub-agent debug logging (actions + system reactions, no thoughts/prompts)
            try:
                from vaf.core.subagent_debug import get_subagent_logger_from_env
                lg = get_subagent_logger_from_env()
                if lg:
                    lg.event("agent_event", payload=evt)
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
        # Sanitize heavy fields (content/code/command) before logging
        try:
            from vaf.core.subagent_debug import sanitize_args
            dbg_args = sanitize_args(name, serializable_args)
        except Exception:
            dbg_args = serializable_args
        emit({"type": "tool_start", "tool": name, "args": dbg_args})
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
                    # Check if running as sub-agent (cannot handle interactive prompts reliably for security)
                    is_subagent = os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "") == "1"
                    
                    if not self._noninteractive and not is_subagent:
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
                        return result + "\n\n[INFO] python_exec is available but requires interactive confirmation (not available in sub-agent mode)."
                
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
        # Log system reaction (result summary only)
        try:
            from vaf.core.subagent_debug import get_subagent_logger_from_env, summarize_result
            lg = get_subagent_logger_from_env()
            if lg:
                lg.event("tool_result", tool=name, **summarize_result(result))
        except Exception:
            pass

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

    def _prepare_messages(self, messages: List[Dict]) -> List[Dict]:
        """Prepare messages for specific model quirks (e.g. Gemma)."""
        is_gemma = "gemma" in self.filename.lower()
        if not is_gemma:
            return messages
        
        # Gemma Logic: Merge System into first User, Ensure Alternation
        new_messages = []
        pending_system = ""
        
        for msg in messages:
            role = msg.get("role")
            content = str(msg.get("content", ""))
            
            if role == "system":
                pending_system += f"{content}\n\n"
            elif role == "user":
                if pending_system:
                    content = f"{pending_system}{content}"
                    pending_system = ""
                
                # Check alternation: If last was user, merge this one
                if new_messages and new_messages[-1]["role"] == "user":
                    new_messages[-1]["content"] += f"\n\n{content}"
                else:
                    new_messages.append({"role": "user", "content": content})
            
            elif role == "assistant":
                # Check alternation: If last was assistant, merge this one
                if new_messages and new_messages[-1]["role"] == "assistant":
                    if content: # Only merge if content exists
                        prev_content = new_messages[-1].get("content", "") or ""
                        new_messages[-1]["content"] = f"{prev_content}\n\n{content}"
                    # If tool calls are present, we might need to handle differently, but merging content is safe
                    if "tool_calls" in msg:
                        # Append tool calls to the previous message if it doesn't have them
                        if "tool_calls" not in new_messages[-1]:
                            new_messages[-1]["tool_calls"] = msg["tool_calls"]
                        else:
                            new_messages[-1]["tool_calls"].extend(msg["tool_calls"])
                else:
                    new_messages.append(msg)
            
            elif role == "tool":
                # Gemma requires User -> Assistant -> User -> ...
                # Tool responses usually follow Assistant (tool calls).
                # But sometimes we have multiple Tool responses.
                # If we send Tool, llama.cpp handles it, but we must ensure it follows Assistant.
                # And after Tool, we need Assistant (or User? No, model replies).
                new_messages.append(msg)

        # If system prompt is left over (no user message yet), add as user
        if pending_system:
             new_messages.append({"role": "user", "content": pending_system.strip()})
             
        return new_messages

    @property
    def TOOLS(self):
        """Dynamic Tool Schema Generation with Context-Aware Optimization"""
        schema = []
        n_ctx = self.config.get("n_ctx", 8192)
        is_small_context = n_ctx < 8000
        
        for name, tool in self.tools.items():
            description = tool.description
            
            # Context Optimization: Truncate descriptions for small contexts
            if is_small_context and description and len(description) > 150:
                description = description[:147] + "..."
            
            schema.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": getattr(tool, "parameters", {"type": "object", "properties": {}})
                }
            })
        return schema
