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
from pathlib import Path

# Dependency imports will be handled in setup or assumed present if requirements are installed
from huggingface_hub import hf_hub_download

# DuckDuckGo Search: Try new package first, fallback to legacy with suppression
try:
    from ddgs import DDGS
except ImportError:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import importlib
        duck_module = importlib.import_module("duckduckgo_search")
        DDGS = getattr(duck_module, "DDGS")

from vaf.core.config import Config
from vaf.core.backend import ServerManager
from vaf.core.platform import Platform
from vaf.core.log_helper import append_domain_log
from vaf.core.system_prompt import SystemPromptManager
from vaf.core.last_interaction import get_last_interaction
from vaf.tools.search import WebSearchTool, get_web_search_results
from vaf.tools.filesystem import ListFilesTool, ReadFileTool, WriteFileTool, MoveFileTool

import atexit
import signal

def _get_debug_log_dir():
    candidates = []
    env_dir = os.environ.get("VAF_LOG_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Platform.data_dir() / "logs")
    candidates.append(Platform.vaf_dir() / "logs")
    candidates.append(Path(__file__).resolve().parents[1] / "logs")
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            continue
    return Path.cwd()

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
    
    def __init__(self, verbose=False, register_signals=True):
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
        self._tokenizer_instance = None
        self._active_tools = None
        self._recent_tools = {}
        self._recent_tool_keep_turns = 2

        # Trust gating state (session-only)
        self._allow_once_tools = set()
        self._noninteractive = os.environ.get("VAF_NONINTERACTIVE", "").strip() in ("1", "true", "yes")
        self._event_sink = None  # optional callable(dict)
        
        # Initialize Context Manager
        from vaf.core.context import ContextManager
        from vaf.core.main_persistence import MainPersistenceManager
        from vaf.core.workspace import WorkspaceManager
        
        # Initialize Workspace Manager (CWD Awareness)
        self.workspace = WorkspaceManager()
        
        # Initialize Main Persistence (creates .vaf/main/ structure)
        # Use current working directory for persistence (project-local)
        try:
            self.main_persistence = MainPersistenceManager(os.getcwd())
        except Exception as e:
            # Fallback if filesystem is read-only or error occurs
            if self.verbose:
                print(f"[WARN] Failed to init main persistence: {e}")
            self.main_persistence = None
        
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
        self.model_display_name = "VQ-1"
        if self.provider != "local":
            api_model = self.config.get(f"api_model_{self.provider}")
            if not api_model:
                api_defaults = {
                    "openai": "gpt-4o",
                    "anthropic": "claude-3-5-sonnet-20241022",
                    "deepseek": "deepseek-chat",
                    "google": "gemini-1.5-flash",
                    "openrouter": "anthropic/claude-3.5-sonnet",
                }
                api_model = api_defaults.get(self.provider, self.provider)
            self.model_display_name = api_model
        elif hasattr(self, 'filename'):
            fname = self.filename.lower()
            if "gemma" in fname: self.model_display_name = "Gemma"
            elif "llama" in fname: self.model_display_name = "Llama"
            elif "mistral" in fname: self.model_display_name = "Mistral"
            elif "phi" in fname: self.model_display_name = "Phi"
            elif "qwen" in fname: self.model_display_name = "Qwen"
            elif "deepseek" in fname: self.model_display_name = "DeepSeek"
        
        # We need tools to init prompt manager, but tools are loaded later.
        # So we init it here with empty dict and update it after tools load.
        self.prompt_manager = SystemPromptManager({}, model_name=self.model_display_name, agent_instance=self) 

        # Initialize State Registry for session state persistence
        from vaf.core.session_state import StateRegistry
        from vaf.core.state_providers.context_state import ContextStateProvider
        from vaf.core.state_providers.tool_activity_state import ToolActivityStateProvider
        
        self.state_registry = StateRegistry()
        
        # Register core state providers
        self.state_registry.register('context', ContextStateProvider(self.context_manager))
        self.tool_activity = ToolActivityStateProvider(self)
        self.state_registry.register('tool_activity', self.tool_activity)

        # Session tracking for server shutdown management
        self._session_id = None
        self._register_session()

        # Initialize Tools (Dynamic Loading)
        self.tools = {}
        self._load_tools()
        # Update Prompt Manager with loaded tools
        self.prompt_manager.tools = list(self.tools.values())
        
        # Register state providers for tools that support it
        self._register_tool_state_providers()
                
        # Register Cleanup Handler (Cross-Platform)
        # WICHTIG: Nur _atexit_cleanup registrieren, nicht shutdown direkt
        # shutdown() wird von Signal-Handlern aufgerufen
        if register_signals:
            try:
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
            except ValueError:
                # Signal registration failed (likely not in main thread)
                if self.verbose:
                    print("[WARN] Could not register signal handlers (not in main thread?)")
        
        # Register atexit handler as final backup (nur einmal)
        atexit.register(self._atexit_cleanup)
        
        # Flag to prevent multiple shutdown calls
        self._shutdown_called = False
        
        # False Promise Detection State
        self._false_promise_retries = 0
        self._max_false_promise_retries = 20

    def _get_tokenizer(self):
        """
        Initializes and returns a lightweight Llama instance for tokenization.
        Caches the instance to avoid re-initialization.
        When use_server is True we must never load the library (~8–18 GB); caller uses estimation or server /tokenize.
        """
        if self._tokenizer_instance:
            return self._tokenizer_instance
        if getattr(self, "use_server", False):
            return None

        try:
            from llama_cpp import Llama  # type: ignore[import-untyped]
        except ImportError:
            # llama-cpp-python not installed - this is OK when using server mode
            # Return None and let caller handle it gracefully
            return None
            
        from vaf.cli.ui import UI
        
        # Ensure model file exists before trying to load it for tokenization
        self.ensure_model_exists()

        try:
            # Initialize with minimal settings for tokenization only
            # No GPU layers, minimal context, no verbose logging
            # This should only load the vocabulary and not the full model weights into VRAM.
            UI.event("System", "Initializing tokenizer...", style="dim")
            try:
                append_domain_log("backend", "LIBRARY_LOAD tokenizer (n_ctx=1)")
            except Exception:
                pass
            self._tokenizer_instance = Llama(
                model_path=self.model_path,
                n_gpu_layers=0,
                n_ctx=1, # We only need the tokenizer, not context
                verbose=False
            )
            return self._tokenizer_instance
        except Exception as e:
            UI.error(f"Could not initialize tokenizer: {e}")
            return None


    def _speak(self, text: str):
        """Helper to speak response via SpeechManager."""
        try:
            from vaf.core.speech import get_speech_manager
            sm = get_speech_manager()
            
            # 1. Clean text first (remove artifacts that might confuse language detection)
            tts_text = sm._clean_markdown(text)
            if not tts_text.strip():
                return

            # 2. Determine language (Prioritize Config > Detect from response > PromptManager fallback)
            tts_lang = "auto"
            
            # Check Config first
            config_lang = self.config.get("language", "auto")
            if config_lang and config_lang != "auto":
                tts_lang = config_lang
            else:
                # Detect language from the actual assistant response
                tts_lang = self._detect_user_language(tts_text)
                # If detection is unclear, fall back to current user language
                if tts_lang == "auto" and hasattr(self, 'prompt_manager') and self.prompt_manager.user_language != "auto":
                    tts_lang = self.prompt_manager.user_language
            
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

        # Cleanup Context Archives (Temporary)
        try:
            if hasattr(self, 'context_manager'):
                self.context_manager.cleanup()
            elif hasattr(self, '_context_manager'):
                self._context_manager.cleanup()
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
                print(f"[ERROR] Failed to load tool {name}: {e}")
                pass # Still ignore for stability, but report error
        
        # Manually register Context Tools for Main Agent
        try:
            from vaf.tools.context_tools import UpdateIntentTool, UpdateWorkingMemoryTool, RequestClarificationTool, MemorySaveTool, MemorySearchTool
            from vaf.tools.user_identity import UpdateUserIdentityTool
            from vaf.tools.send_telegram import SendTelegramTool
            from vaf.tools.send_discord import SendDiscordTool
            from vaf.tools.send_slack import SendSlackTool
            from vaf.tools.mail_inbox import MailInboxTool
            from vaf.tools.read_mail import ReadMailTool
            from vaf.tools.find_mail import FindMailTool
            from vaf.tools.mark_mail_answered import MarkMailAnsweredTool
            from vaf.tools.send_mail import SendMailTool

            # UpdateIntent and UpdateWorkingMemory are for Main Agent
            self.tools["update_intent"] = UpdateIntentTool()
            self.tools["update_working_memory"] = UpdateWorkingMemoryTool()
            self.tools["memory_save"] = MemorySaveTool()
            self.tools["memory_search"] = MemorySearchTool()
            self.tools["update_user_identity"] = UpdateUserIdentityTool()
            self.tools["send_telegram"] = SendTelegramTool()
            self.tools["send_discord"] = SendDiscordTool()
            self.tools["send_slack"] = SendSlackTool()
            self.tools["mail_inbox"] = MailInboxTool()
            self.tools["read_mail"] = ReadMailTool()
            self.tools["find_mail"] = FindMailTool()
            self.tools["mark_mail_answered"] = MarkMailAnsweredTool()
            self.tools["send_mail"] = SendMailTool()
            
            # RequestClarification is strictly for Sub-Agents (via coder_only flag),
            # but we register it here so it's available in the system (even if filtered out later for Main Agent).
            # Note: The filter loop above relies on 'coder_only' attribute.
            # Since we manually adding here, we bypass the loop.
            # BUT: Main Agent should NOT see request_clarification.
            # We don't add request_clarification to self.tools here for the Main Agent.
            # It will be loaded by the Coder Agent separately.
            
        except Exception as e:
            if self.verbose:
                print(f"[WARN] Failed to load context tools: {e}")

        # Provide tool registry to tools that expect it (e.g., list_tools)
        for tool in self.tools.values():
            if hasattr(tool, "available_tools"):
                try:
                    tool.available_tools = self.tools
                except Exception:
                    pass

        # DEBUG: Always print active tools during this debugging session
        print(f"[DEBUG] Active Tools: {list(self.tools.keys())}")

        # Track active async sub-agent tasks
        self._async_subagent_tasks = {}  # task_id -> {"agent_type": str, "task": str, "started_at": datetime}

    def _register_tool_state_providers(self):
        """
        Register state providers for tools that support runtime state persistence.
        Tools can implement create_state_provider() to opt-in to state persistence.
        """
        for tool_name, tool in self.tools.items():
            try:
                if hasattr(tool, 'create_state_provider'):
                    provider = tool.create_state_provider()
                    if provider:
                        self.state_registry.register(f'tool_{tool_name}', provider)
                        if self.verbose:
                            print(f"[DEBUG] Registered state provider for tool: {tool_name}")
            except Exception as e:
                if self.verbose:
                    print(f"[WARN] Failed to register state provider for {tool_name}: {e}")
        
        # Register sandbox state provider specifically if python sandbox exists
        if 'python' in self.tools:
            try:
                from vaf.core.state_providers.sandbox_state import PythonSandboxStateProvider
                sandbox_tool = self.tools['python']
                # Only register if tool has a namespace (sandbox capability)
                if hasattr(sandbox_tool, 'namespace') or hasattr(sandbox_tool, 'sandbox'):
                    target = getattr(sandbox_tool, 'sandbox', sandbox_tool)
                    provider = PythonSandboxStateProvider(target)
                    self.state_registry.register('python_sandbox', provider)
                    if self.verbose:
                        print("[DEBUG] Registered Python sandbox state provider")
            except Exception as e:
                if self.verbose:
                    print(f"[WARN] Failed to register Python sandbox state provider: {e}")

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
            
            # Check for empty result to prevent hallucination
            result_content = (task.result or "").strip()
            # Remove <think> blocks for emptiness check
            clean_result = re.sub(r'<think>.*?</think>', '', result_content, flags=re.DOTALL).strip()
            
            is_empty = not clean_result or len(clean_result) < 5
            
            if is_empty:
                UI.event("Warning", f"Sub-Agent [{task.task_id}] returned no usable data.", style="yellow")
                task_result_msg = (
                    f"**FINAL RESULT (Task is DONE):**\n"
                    f"[!] WARNING: The sub-agent returned an EMPTY result or only internal reasoning.\n"
                    f"There is NO DATA in the report. Do NOT hallucinate contents.\n"
                    f"Inform the user that the sub-agent task completed but provided no information."
                )
            else:
                task_result_msg = f"**FINAL RESULT (Task is DONE):**\n{task.result}"

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
            
            # Add result to history as Background Intel (not a new primary prompt)
            self.history.append({
                "role": "system",
                "content": (
                    f"🧠 **BACKGROUND INTELLIGENCE: Sub-Agent Task Finished**\n"
                    f"Agent: {task.agent_type} (Task ID: {task.task_id[:8]})\n\n"
                    f"{task_result_msg}\n"
                    f"{file_hint}\n"
                    f"--- END OF SUB-AGENT OUTPUT ---\n\n"
                    f"⚠️ **INSTRUCTION:** Use the information above to fulfill the **ORIGINAL USER INTENT**.\n"
                    f"Do NOT just summarize that a task is done. The user wants an answer to their question, not a status report."
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
        
        # Update Main Persistence (Team State)
        if hasattr(self, 'main_persistence') and self.main_persistence:
            try:
                self.main_persistence.update_subagent_status(
                    task_id=task.task_id,
                    agent_type=task.agent_type,
                    status=task.status,
                    result_summary=task.result[:500] if task.result else task.error
                )
            except Exception:
                pass

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
        is_win = platform.system() == "Windows"
        is_mac = platform.system() == "Darwin"
        # On Windows default force_server=True to avoid loading library in-process (double model = 15–20 GB RAM).
        _fs = self.config.get("force_server")
        force_server = (True if is_win else False) if _fs is None else bool(_fs)
        # FACT: On Windows with Python < 3.13 and no force_server, we use the LIBRARY (llama-cpp-python
        # in-process). The Tray may start the native llama-server (8080), but the agent does not use it
        # here — so the model is loaded twice (server process + Python process) unless force_server=True.
        if is_py313 or is_mac or force_server:
            UI.event("System", f"Initializing Standalone Server (Py3.13 / Mac / GPU Mode)...", style="warning")
            # If the server is already running (or still loading), reuse it to avoid duplicates.
            # CRITICAL: When Tray is running, it starts the server on activity. Wait for it first
            # so we never start a SECOND llama-server (would crash PC / double VRAM).
            try:
                response = requests.get("http://127.0.0.1:8080/health", timeout=1)
                if response.status_code in (200, 503):
                    self.server = ServerManager(skip_cleanup=True)
                    self.use_server = True
                    UI.event("System", "Reusing existing HTTP backend on :8080.", style="dim")
                    return
            except Exception:
                pass
            # Wait up to 30s for Tray (or another process) to start the server before we start ourselves.
            for _ in range(30):
                try:
                    r = requests.get("http://127.0.0.1:8080/health", timeout=2)
                    if r.status_code in (200, 503):
                        self.server = ServerManager(skip_cleanup=True)
                        self.use_server = True
                        UI.event("System", "Reusing existing HTTP backend on :8080 (waited).", style="dim")
                        return
                except Exception:
                    pass
                time.sleep(1)
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
                # When force_server is True: do NOT load the library (would double RAM: server + Python process).
                if force_server:
                    self.server = ServerManager()
                    self.use_server = True
                    UI.event("System", "Server not ready yet. Using HTTP backend (8080); retry 503 in chat.", style="warning")
                    return
                UI.error("Server backend failed. Falling back to internal library (CPU).")
        
        # Before loading library: if 8080 is reachable, use server to avoid double model (15–20 GB RAM).
        try:
            r = requests.get("http://127.0.0.1:8080/health", timeout=1)
            if r.status_code in (200, 503):
                self.server = ServerManager(skip_cleanup=True)
                self.use_server = True
                UI.event("System", "Reusing existing HTTP backend on :8080 (avoiding library load).", style="dim")
                return
        except Exception:
            pass

        # When force_server is True we must never load the library (double model = 15–20 GB RAM).
        if force_server:
            self.server = ServerManager(skip_cleanup=True)
            self.use_server = True
            UI.event("System", "Server required (force_server). Using HTTP backend (8080). Start Tray or llama-server.", style="warning")
            return

        # Fallback to Local Library (optional dep: pip install llama-cpp-python)
        try:
            from llama_cpp import Llama  # type: ignore[import-untyped]
        except ImportError:
            UI.error("llama-cpp-python not found. Run 'vaf install-gpu' to fix.")
            sys.exit(1)

        UI.event("System", f"Loading Library: {self.filename}...", style="dim")
        try:
            append_domain_log("backend", "LIBRARY_LOAD main model (force_server was False)")
        except Exception:
            pass
        # Redirect stderr to suppress ggml_metal_init logs on Mac
        
        # Context manager to suppress stderr
        class StderrSuppressor:
            def __enter__(self):
                self.old_stderr = sys.stderr
                self.devnull = open(os.devnull, 'w')
                sys.stderr = self.devnull
            def __exit__(self, exc_type, exc_val, exc_tb):
                sys.stderr = self.old_stderr
                self.devnull.close()

        try:
            # Only suppress if verbose is False (to allow debugging if needed)
            if not self.verbose:
                with StderrSuppressor():
                    self.llm = Llama(
                        model_path=self.model_path,
                        n_gpu_layers=n_gpu, 
                        n_ctx=n_ctx,
                        verbose=self.verbose
                    )
            else:
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
        self.prompt_manager = SystemPromptManager(list(self.tools.values()), model_name=self.model_display_name, agent_instance=self)
                
        # Build initial prompt (Core + Base Rules)
        # We pass self.filename to determine identity (VQ-1 vs Generic), and current user for User identity block
        system_prompt = self.prompt_manager.build_prompt(
            self.filename,
            username=getattr(self, "_current_username", None),
            user_scope_id=getattr(self, "_current_user_scope_id", None),
            current_source=getattr(self, "_current_chat_source", None),
            last_interaction=get_last_interaction(getattr(self, "_current_user_scope_id", None)),
        )
        
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

    def load_session_context(self, session_id: str):
        """
        Swap the agent's context to a specific session.
        Prevents cross-contamination between TUI and Web UI.
        """
        from vaf.core.subagent_ipc import set_current_session_id
        
        # Check if we are already in this session
        if hasattr(self, 'current_session_id') and self.current_session_id == session_id:
            return

        # Load new session data
        from vaf.core.session import SessionManager
        sm = SessionManager()
        try:
            session = sm.load(session_id)
            # Set current user from session metadata so build_prompt() can show User identity block (only override if session has them)
            meta = getattr(session, "metadata", None) or {}
            if meta.get("user_scope_id") is not None:
                self._current_user_scope_id = meta.get("user_scope_id")
            if meta.get("username") is not None:
                self._current_username = meta.get("username")
            # Reset Context (System Prompt)
            self.init_chat() 
            
            # Replay History
            for msg in session.messages:
                role = msg.get("role")
                content = str(msg.get("content") or "")
                if role in ["user", "assistant", "tool", "system"]:
                    # Skip duplicate or operational system prompts
                    if role == "system":
                        # List of operational log prefixes to IGNORE in LLM context
                        ignore_patterns = [
                            "System:", "Info:", "Step ", "Router:", "Queued input",
                            "Initializing Standalone Server", "Starting chat_step",
                            "Generation stopped", "Empty response detected"
                        ]
                        if any(p in content for p in ignore_patterns) and "## PROJECT CONTEXT" not in content:
                            continue
                            
                    self.history.append({"role": role, "content": content})
            
            # Update Pointer
            self.current_session_id = session_id
            set_current_session_id(session_id)
            
        except Exception:
            # New/Empty session
            self.init_chat()
            self.current_session_id = session_id
            set_current_session_id(session_id)
            
        # CRITICAL: Compress history immediately upon load to match context limits
        # Otherwise UI shows massive "Raw Truth" (e.g. 17k tokens) which looks broken,
        # even though chat_step would compress it before sending.
        # We want the UI to show the "Ready State".
        self.manage_context()
            
        # Broadcast new context stats to WebUI immediately
        self._broadcast_context_status()

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

        # 0. Context Persistence Check (Fix for Sub-Agent Errors)
        # If this is a system error message (usually English), preserve the USER'S language context.
        # Otherwise, the error "Sub-Agent failed..." makes the model switch to English.
        is_system_error = (
            "sub-agent failed" in t or
            "error:" in t or
            "[x] sub-agent" in t or
            "task_id" in t
        )
        
        if is_system_error and hasattr(self, 'prompt_manager') and self.prompt_manager.user_language != "auto":
            # Return the previously detected user language instead of the error's language
            return self.prompt_manager.user_language

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

        # Short acknowledgements are often ambiguous; keep prior language to avoid spurious flips.
        try:
            import re
            words = re.findall(r'\b\w+\b', t)
        except Exception:
            words = t.split()
        if len(t) <= 24 and len(words) <= 3:
            if hasattr(self, 'prompt_manager') and self.prompt_manager.user_language != "auto":
                return self.prompt_manager.user_language

        # 2. Fallback to langid (Probabilistic / General Purpose) - Supports 97 languages
        try:
            # langid is pure-Python and supports many languages (offline).
            import langid  # type: ignore

            # Use langid for detection. It is generally robust even for short phrases.
            # We relax the length constraint to > 3 chars to catch "Was ist das" etc.
            if len(t) >= 5 and any(ch.isalpha() for ch in t):
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
        
        # Determine language (simplified check for common cases)
        # Using langid for robust detection
        try:
            import langid
            import re
            
            user_lang = self._detect_user_language(user_input)
            
            # CRITICAL: Remove <think> blocks from response before classifying!
            # Otherwise, the English thinking process triggers false "Language Mismatch" on German responses.
            clean_response = re.sub(r'<think>.*?</think>', '', assistant_response, flags=re.DOTALL).strip()
            if not clean_response:
                 # Fallback if response was ONLY thinking (should not happen usually)
                 clean_response = assistant_response
                 
            response_lang, _ = langid.classify(clean_response)
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
        
        # Store details for retry logging (used when we trigger immediate retry)
        self._last_language_mismatch = {
            "user": user_lang_name,
            "response": response_lang_name,
            "target": user_lang_name,
        }

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

    def _estimate_token_usage(self):
        """
        Estimate token usage without loading the tokenizer.
        Used when server mode is active to avoid blocking on model file.
        Uses ~4 chars per token as a conservative estimate for most LLMs.
        """
        import json as json_module  # Local import to avoid scope issues

        total_chars = 0

        # 1. Estimate chat history tokens
        for msg in self.history:
            content = str(msg.get("content", ""))
            role = str(msg.get("role", ""))
            total_chars += len(content) + len(role) + 20  # 20 for message structure

        # 2. Estimate tool schema tokens
        if hasattr(self, 'TOOLS') and self.TOOLS:
            try:
                schema_str = json_module.dumps(self.TOOLS)
                total_chars += len(schema_str)
            except Exception:
                total_chars += 2000  # Fallback estimate for tools

        # Convert chars to tokens (conservative: ~4 chars per token)
        estimated_tokens = total_chars // 4
        
        # Use actual context manager limit if available (handles API boosts to 128k)
        if hasattr(self, 'context_manager'):
            max_tokens = self.context_manager.max_tokens
        else:
            max_tokens = self.config.get("n_ctx", 8192)

        return estimated_tokens, max_tokens

    def get_token_usage(self):
        """
        Calculates a precise token usage by using the model's tokenizer.
        This includes the chat history and the schemas of all active tools.
        """
        # API Backend: Return ESTIMATED current context usage (Snapshot)
        # We cannot use session_usage because that is CUMULATIVE (billing), 
        # which breaks the context bar and management logic (shows >100% full).
        if self.api_backend:
            return self._estimate_token_usage()

        # Server Mode: Use server /tokenize API for precise count (no local model load).
        # If server is not ready or tokenize fails, fall back to estimation.
        if self.use_server:
            try:
                total = 0
                for msg in self.history:
                    content = str(msg.get("content", ""))
                    role = str(msg.get("role", ""))
                    for text in (content, role):
                        if not text:
                            continue
                        r = requests.post(
                            "http://127.0.0.1:8080/tokenize",
                            json={"content": text},
                            timeout=5
                        )
                        if r.status_code == 200:
                            data = r.json()
                            total += len(data.get("tokens", []))
                    total += 5
                if hasattr(self, "TOOLS") and self.TOOLS:
                    try:
                        schema_str = json.dumps(self.TOOLS)
                        r = requests.post(
                            "http://127.0.0.1:8080/tokenize",
                            json={"content": schema_str},
                            timeout=5
                        )
                        if r.status_code == 200:
                            total += len(r.json().get("tokens", []))
                    except Exception:
                        total += len(self.tools) * 200
                total += 100
                return total, self.config.get("n_ctx", 8192)
            except Exception:
                pass
            return self._estimate_token_usage()

        tokenizer = self._get_tokenizer()
        if not tokenizer:
            # Fallback to a very rough estimation if tokenizer fails
            return len(json.dumps(self.history)) // 2, self.config.get("n_ctx", 8192)

        total_tokens = 0
        
        # 1. Tokenize chat history
        # The llama.cpp server/library adds special tokens for roles, so we
        # convert our history to a string that mimics the input format.
        for msg in self.history:
            # Roughly role + content
            content = str(msg.get("content", ""))
            role = str(msg.get("role", ""))
            # Add a few tokens per message for role and formatting
            total_tokens += len(tokenizer.tokenize(content.encode("utf-8", errors="ignore")))
            total_tokens += len(tokenizer.tokenize(role.encode("utf-8", errors="ignore")))
            total_tokens += 5 # Overhead for message structure

        # 2. Tokenize tool schemas
        # This is the "hidden" context cost.
        if hasattr(self, 'TOOLS') and self.TOOLS:
            # self.TOOLS is a property that returns the list of schema dicts
            tool_schemas = self.TOOLS
            # Convert the schema to a JSON string to tokenize it
            try:
                schema_str = json.dumps(tool_schemas)
                total_tokens += len(tokenizer.tokenize(schema_str.encode("utf-8", errors="ignore")))
            except Exception:
                # Fallback if json serialization fails
                total_tokens += len(self.tools) * 200 # A smaller, safer estimate

        # 3. Add a small safety buffer
        total_tokens += 100

        return total_tokens, self.config.get("n_ctx", 8192)

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

    def _generate_for_compaction(self, user_prompt: str) -> str:
        """
        Single non-streaming LLM call for session compaction. Does not modify history.
        Returns raw reply text (e.g. MEMORY: "..." or NO_REPLY).
        While this runs, empty-response filters in chat_step must not treat short/NO_REPLY as empty.
        max_tokens is configurable (memory_compaction_max_tokens, default 4000) for API/server/local.
        """
        from vaf.core.config import Config
        compaction_max_tokens = int(Config.get("memory_compaction_max_tokens", 4000))
        temp_history = [{"role": "user", "content": user_prompt}]
        content = ""
        self._compaction_in_progress = True
        try:
            if self.use_server:
                import requests
                payload = {
                    "messages": temp_history,
                    "max_tokens": compaction_max_tokens,
                    "temperature": 0.2,
                    "stream": False,
                }
                res = requests.post(
                    "http://127.0.0.1:8080/v1/chat/completions",
                    json=payload,
                    timeout=90,
                ).json()
                content = (res.get("choices") or [{}])[0].get("message", {}).get("content", "")
            elif self.api_backend:
                chunks = list(
                    self.api_backend.chat_completion(
                        messages=temp_history,
                        max_tokens=compaction_max_tokens,
                        temperature=0.2,
                        stream=False,
                    )
                )
                content = "".join(c if isinstance(c, str) else str(c) for c in chunks if c)
            elif self.llm:
                output = self.llm.create_chat_completion(
                    messages=temp_history,
                    max_tokens=compaction_max_tokens,
                    temperature=0.2,
                )
                content = (output.get("choices") or [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Compaction LLM call failed: %s", e)
        finally:
            self._compaction_in_progress = False
        return (content or "").strip()

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
        
        # Broadcast update to WebUI
        self._broadcast_context_status()

    def _broadcast_context_status(self):
        """Send context debug info to WebUI (X-Ray Vision)."""
        try:
            from vaf.core.web_interface import get_web_interface

            tokens, max_tokens = self.get_token_usage()

            # Calculate detailed token breakdown for X-Ray visualization
            system_tokens = 0
            history_tokens = 0
            tools_tokens = 0
            system_content = ""

            for msg in self.history:
                content = str(msg.get("content", ""))
                role = msg.get("role", "")
                # Estimate tokens: ~4 chars per token
                msg_tokens = (len(content) + len(role) + 20) // 4

                if role == "system":
                    system_tokens += msg_tokens
                    if not system_content:
                        system_content = content
                else:
                    history_tokens += msg_tokens

            # Estimate tool schema tokens
            if hasattr(self, 'TOOLS') and self.TOOLS:
                try:
                    import json
                    schema_str = json.dumps(self.TOOLS)
                    tools_tokens = len(schema_str) // 4
                except Exception:
                    tools_tokens = len(self.tools) * 200 if hasattr(self, 'tools') else 0

            # Count user messages for compaction tracking
            # CRITICAL: Use PERSISTENT count from session.runtime_state, NOT from compressed history
            user_turn_count = 0
            compaction_interval = 15
            try:
                from vaf.core.config import Config
                from vaf.core.session import SessionManager
                compaction_interval = int(Config.get("memory_compaction_interval", 15))
                # Try to get persistent count from session
                if hasattr(self, 'current_session_id') and self.current_session_id:
                    try:
                        _sm = SessionManager()
                        _session = _sm.load(self.current_session_id)
                        _runtime = getattr(_session, 'runtime_state', None) or {}
                        user_turn_count = _runtime.get("user_turn_count", 0)
                    except Exception:
                        pass
                # Fallback to history count if no persistent count
                if user_turn_count == 0:
                    user_turn_count = sum(1 for m in self.history if m.get("role") == "user")
            except Exception:
                user_turn_count = sum(1 for m in self.history if m.get("role") == "user")

            get_web_interface().push_update({
                "type": "context_status",
                "stats": {
                    "tokens": tokens,
                    "max_tokens": max_tokens,
                    "percent": round((tokens / max_tokens) * 100, 1) if max_tokens else 0,
                    "message_count": len(self.history),
                    "rag_preview": system_content,
                    # Detailed breakdown for X-Ray visualization
                    "system_tokens": system_tokens,
                    "history_tokens": history_tokens,
                    "tools_tokens": tools_tokens,
                    # Compaction tracking
                    "user_turn_count": user_turn_count,
                    "compaction_interval": compaction_interval,
                    "compaction_progress": round((user_turn_count % compaction_interval) / compaction_interval * 100)
                }
            })
        except Exception:
            pass
    
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
        
        # CRITICAL: Use Agent's get_token_usage() which includes Tool overhead
        # instead of ContextManager's estimate which only counts history text
        tokens, max_tokens = self.get_token_usage()
        
        # Get other status info from context manager
        cm_status = self._context_manager.get_status(self.history)
        
        # Override token count with accurate value
        cm_status['tokens'] = tokens
        cm_status['max_tokens'] = max_tokens
        cm_status['usage_percent'] = tokens / max_tokens if max_tokens > 0 else 0
        
        return cm_status

    def analyze_intent(self, user_input):
        '''
        Determines the optimal temperature (0.1 - 0.9) for the user's request.
        Uses a hybrid approach: rule-based heuristics + optional LLM confirmation.
        '''
        try:
            input_lower = user_input.lower()

            # ============================================================
            # PHASE 1: Fast rule-based heuristics (no LLM call needed)
            # ============================================================

            # LOW TEMPERATURE (0.1-0.3): Factual, logical, precise tasks
            low_temp_patterns = [
                # Math and calculations
                r'\b(berechne|calculate|rechne|compute|solve|löse)\b',
                r'\b\d+\s*[\+\-\*\/\^]\s*\d+',  # Math expressions like "5+3"
                r'\b(math|mathematik|algebra|geometry|calculus)\b',
                # Code and programming
                r'\b(code|programmier|function|class|def |import |return |bug|fix|debug|error|fehler)\b',
                r'\b(python|javascript|typescript|java|rust|go|cpp|c\+\+|sql)\b',
                # Facts and definitions
                r'\b(was ist|what is|define|definition|erkläre genau|explain exactly)\b',
                r'\b(wieviel|how much|how many|wie viele|wann|when|where|wo|who|wer)\b',
                # File operations and tools
                r'\b(datei|file|ordner|folder|directory|lese|read|schreibe|write|erstelle|create)\b',
                r'\b(suche nach|search for|find|finde|grep|look for)\b',
                # Technical/precise
                r'\b(convert|konvertiere|format|parse|validate|check|prüfe)\b',
            ]

            # HIGH TEMPERATURE (0.7-0.9): Creative, open-ended tasks
            high_temp_patterns = [
                # Creative writing
                r'\b(schreibe|write|verfasse|compose|dichte|poem|gedicht|story|geschichte)\b',
                r'\b(kreativ|creative|brainstorm|ideen|ideas|inspire|inspiration)\b',
                r'\b(novel|roman|essay|artikel|article|blog)\b',
                # Open-ended exploration
                r'\b(was denkst du|what do you think|meinung|opinion|vorschlag|suggest)\b',
                r'\b(wie wäre es|how about|was wäre wenn|what if|imagine|stell dir vor)\b',
                r'\b(erzähl|tell me about|describe|beschreibe)\b',
                # Humor and fun
                r'\b(witz|joke|lustig|funny|humor|spaß|fun)\b',
                # Roleplay
                r'\b(roleplay|spiel|pretend|tu so als ob|act as|sei)\b',
            ]

            # Check patterns
            for pattern in low_temp_patterns:
                if re.search(pattern, input_lower):
                    temp = 0.2
                    try:
                        append_domain_log("backend", f"intent_temp_rule_low pattern={pattern[:20]} temp={temp}")
                    except Exception:
                        pass
                    return temp

            for pattern in high_temp_patterns:
                if re.search(pattern, input_lower):
                    temp = 0.8
                    try:
                        append_domain_log("backend", f"intent_temp_rule_high pattern={pattern[:20]} temp={temp}")
                    except Exception:
                        pass
                    return temp

            # ============================================================
            # PHASE 2: LLM-based analysis for ambiguous cases
            # ============================================================
            prompt = (
                "Analyze this request and output ONLY a number between 0.1 and 0.9.\n"
                "0.1-0.3 = factual/logical/code tasks\n"
                "0.4-0.6 = general conversation\n"
                "0.7-0.9 = creative/brainstorming\n"
                f"Request: {user_input[:200]}\n"  # Limit input length
                "Temperature:"
            )

            messages = [{"role": "user", "content": prompt}]
            content = ""

            # Try LLM call if server is available
            if self.use_server:
                payload = {"messages": messages, "max_tokens": 32, "temperature": 0.0}
                try:
                    res = requests.post("http://127.0.0.1:8080/v1/chat/completions", json=payload, timeout=10).json()
                    choices = res.get('choices', [])
                    if choices:
                        msg = choices[0].get('message', {})
                        content = msg.get('content', '') or choices[0].get('text', '')
                    try:
                        append_domain_log("backend", f"intent_llm_response content={content[:30] if content else 'EMPTY'}")
                    except Exception:
                        pass
                except requests.exceptions.Timeout:
                    try:
                        append_domain_log("backend", "intent_llm_timeout")
                    except Exception:
                        pass
                except Exception as e:
                    try:
                        append_domain_log("backend", f"intent_llm_error error={str(e)[:50]}")
                    except Exception:
                        pass

            # Strip <think> blocks and parse
            clean_content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
            match = re.search(r"0?\.\d+|\d\.\d+", clean_content)
            if match:
                temp = float(match.group())
                temp = max(0.1, min(0.9, temp))
                try:
                    append_domain_log("backend", f"intent_llm_parsed temp={temp}")
                except Exception:
                    pass
                return temp

            # ============================================================
            # PHASE 3: Fallback based on input length/complexity
            # ============================================================
            # Short inputs are often commands/queries (lower temp)
            # Long inputs are often explanations/creative (higher temp)
            word_count = len(user_input.split())
            if word_count < 5:
                temp = 0.4  # Short = likely a command
            elif word_count > 50:
                temp = 0.6  # Long = likely needs more creativity
            else:
                temp = 0.5  # Default balanced

            try:
                append_domain_log("backend", f"intent_fallback_wordcount words={word_count} temp={temp}")
            except Exception:
                pass
            return temp

        except Exception as e:
            from vaf.cli.ui import UI
            UI.event("Debug", f"Intent Analysis Failed: {e}", style="dim")
            return 0.5  # Safe default
    
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
            
            # Get available workflows dynamicallly
            available_workflows = list_templates()
            
            # Format workflows like tool definitions for the LLM
            # "ID: Description"
            workflow_definitions = []
            for w in available_workflows:
                workflow_definitions.append(f"- {w['id']}: {w['description']}")
            workflow_list_str = "\n".join(workflow_definitions)
            
            prompt = (
                f"You are the Workflow Router. Your goal is to map a user request to the correct pre-defined workflow ID.\n\n"
                f"AVAILABLE WORKFLOWS:\n"
                f"{workflow_list_str}\n\n"
                f"ROUTING INSTRUCTIONS:\n"
                f"1. Analyze the User Request for INTENT.\n"
                f"2. Check if a Workflow matches that intent EXACTLY.\n"
                f"3. Return the `workflow_id` if a strong match exists.\n"
                f"4. Return `none` if:\n"
                f"   - The request is a simple lookup (weather, news, facts).\n"
                f"   - The request is a generic chat or question.\n"
                f"   - The request is too vague.\n"
                f"   - You would rather use individual tools (web_search, coding_agent) directly.\n\n"
                f"EXAMPLES:\n"
                f"- User: 'Create a website' -> `create_website`\n"
                f"- User: 'Research AI trends and write a report' -> `research_and_document`\n"
                f"- User: 'What is the weather?' -> `none` (Too simple)\n"
                f"- User: 'Who is Elon Musk?' -> `none` (Too simple)\n\n"
                f"USER REQUEST: \"{user_input}\"\n\n"
                f"Think step-by-step. Does this complex task fit a workflow?\n"
                f"Output ONLY the workflow_id or 'none'."
            )
            
            # Quick Inference with reasoning (temperature 0.1 for strict logic)
            messages = [{"role": "user", "content": prompt}]
            
            content = ""
            if self.use_server:
                # Full thinking capacity with 120s timeout
                payload = {"messages": messages, "max_tokens": 1024, "temperature": 0.1}
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

        # Skip workflow matching when Document Editor is active (user has editor content in context).
        # Otherwise the workflow router may pick code_review or other workflows; the agent should
        # use replace_editor_selection / document_editor tools instead.
        if "CURRENT DOCUMENT (Editor)" in (user_input or ""):
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
        msg_async_return = "[WORKFLOW_ASYNC:{task_id}:{workflow_id}] Workflow '{name}' is running in a separate terminal."
        msg_paused_ui = "⏸️  Workflow paused - waiting for sub-agent [Task: {task_id}]"
        msg_paused_hint = "💡 You can continue using VAF. The workflow will resume automatically when the sub-agent finishes."
        msg_paused_return = "⏸️ Workflow '{workflow_id}' is paused, waiting for a sub-agent to complete.\n\nYou can continue using me while we wait. The workflow will automatically resume when the result is ready."
        
        if lang == "de":
            msg_analyzing = "Schritt 1/2: Analysiere Workflow-Übereinstimmung..."
            msg_brain_matched_ui = "Ausgewählt: {name} (Mehrsprachige Unterstützung!)"
            msg_extracting = "Extrahiere Variablen aus Benutzereingabe..."
            msg_running_separate = "Läuft in separatem Terminal [Task: {task_id}]"
            msg_runs_independently = "[>] Workflow läuft unabhängig. Ergebnis wird gemeldet, wenn fertig."
            msg_async_return = "[WORKFLOW_ASYNC:{task_id}:{workflow_id}] Workflow '{name}' läuft in einem separaten Terminal."
            msg_paused_ui = "⏸️  Workflow pausiert - warte auf Sub-Agent [Task: {task_id}]"
            msg_paused_hint = "💡 Du kannst VAF weiter nutzen. Der Workflow wird automatisch fortgesetzt, wenn der Sub-Agent fertig ist."
            msg_paused_return = "⏸️ Workflow '{workflow_id}' ist pausiert und wartet auf einen Sub-Agent.\n\nIch bin weiter für dich da, während wir warten. Der Workflow wird automatisch fortgesetzt, wenn das Ergebnis da ist."
        
        try:
            from vaf.workflows import WorkflowSelector, WorkflowEngine, create_workflow
            
            # Check if workflows are enabled (can be disabled in config)
            if not self.config.get("workflows_enabled", True):
                return None

            explicit_workflow_id, cleaned_input = self._extract_explicit_workflow(user_input)
            if explicit_workflow_id:
                user_input = cleaned_input or user_input
                workflow_id = explicit_workflow_id
                self._workflow_selection_tier = 0
                msg_analyzing = f"Step 1/2: Analyzing workflow match... (User selected: {workflow_id})"
                if lang == "de":
                    msg_analyzing = f"Schritt 1/2: Analysiere Workflow-Übereinstimmung... (User-Auswahl: {workflow_id})"
            
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
            
            UI_Class.event("Info", msg_analyzing, style="dim")
            with UI_Class.console.status(f"[bold cyan](O_O)  {msg_analyzing}[/bold cyan]", spinner="dots"):
                if not explicit_workflow_id:
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
            
            # 🔒 INTENT LOCK (Workflow): Save the fresh user intent to persistence
            if hasattr(self, 'main_persistence') and self.main_persistence:
                try:
                    self.main_persistence.update_user_intent(user_input)
                except Exception:
                    pass

            # Get the matched template
            from vaf.workflows.templates import get_template
            template = get_template(workflow_id)
            if not template:
                return None
            
            # Show workflow selection status with tier information
            tier = getattr(self, '_workflow_selection_tier', 1)
            tier_names = {
                0: "Explicit",
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

            # DEBUG: Log workflow execution path
            try:
                from pathlib import Path
                import datetime
                log_dir = Path(__file__).resolve().parents[2] / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                with open(log_dir / "workflow_debug.log", "a", encoding="utf-8") as f:
                    ts = datetime.datetime.now().isoformat()
                    sep_terminals = self.config.get("sub_agents_in_separate_terminals", False)
                    in_wf = os.environ.get("VAF_IN_WORKFLOW_TERMINAL", "NOT SET")
                    in_sa = os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "NOT SET")
                    f.write(f"{ts} WORKFLOW EXECUTION START\n")
                    f.write(f"{ts} workflow_id={workflow_id}\n")
                    f.write(f"{ts} sub_agents_in_separate_terminals={sep_terminals}\n")
                    f.write(f"{ts} VAF_IN_WORKFLOW_TERMINAL={in_wf}\n")
                    f.write(f"{ts} VAF_IN_SUBAGENT_TERMINAL={in_sa}\n")
            except Exception as e:
                pass

            if self.config.get("sub_agents_in_separate_terminals", False):
                # Don't spawn if already in a workflow/subagent terminal
                in_workflow_terminal = os.environ.get("VAF_IN_WORKFLOW_TERMINAL", "").strip() in ("1", "true", "yes")
                in_subagent_terminal = os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "").strip() in ("1", "true", "yes")

                if not in_workflow_terminal and not in_subagent_terminal:
                    try:
                        # DEBUG: Log each step
                        def _debug_log(msg):
                            try:
                                from pathlib import Path
                                import datetime
                                log_dir = Path(__file__).resolve().parents[2] / "logs"
                                with open(log_dir / "workflow_debug.log", "a", encoding="utf-8") as f:
                                    f.write(f"{datetime.datetime.now().isoformat()} {msg}\n")
                            except:
                                pass

                        _debug_log("STEP 1: Creating IPC task...")
                        # Create IPC task
                        from vaf.core.subagent_ipc import get_ipc, get_current_session_id
                        ipc = get_ipc()
                        task_id = ipc.create_task(
                            agent_type=f"workflow:{workflow_id}",
                            task_description=user_input,
                            session_id=get_current_session_id()
                        )
                        _debug_log(f"STEP 2: IPC task created: {task_id}")

                        # Build command to run workflow in separate terminal
                        import json as json_module
                        import shlex

                        # Serialize variables to JSON
                        variables_json = json_module.dumps(result.variables)
                        _debug_log(f"STEP 3: Variables JSON: {variables_json[:200]}")

                        from vaf.core.platform import Platform

                        # Pass session ID to workflow terminal
                        session_id = get_current_session_id()
                        if session_id:
                            os.environ["VAF_SESSION_ID"] = session_id
                        os.environ["VAF_TASK_ID"] = task_id
                        os.environ["VAF_AGENT_TYPE"] = f"workflow:{workflow_id}"
                        _debug_log(f"STEP 4: Env vars set, session_id={session_id}")

                        # Pass Language Hint to workflow terminal
                        if hasattr(self, 'prompt_manager') and self.prompt_manager.user_language:
                            os.environ["VAF_USER_LANGUAGE"] = self.prompt_manager.user_language

                        # Build command with proper escaping for the platform
                        if Platform.is_windows():
                            # Windows CMD: escape double quotes with backslash (for subprocess shell=True)
                            # Also escape backslashes that precede quotes
                            escaped_json = variables_json.replace('\\', '\\\\').replace('"', '\\"')
                            cmd = f'vaf workflow run "{workflow_id}" --variables "{escaped_json}" --task-id {task_id}'
                        else:
                            # Unix: use shlex.quote for proper escaping
                            cmd = f'vaf workflow run "{workflow_id}" --variables {shlex.quote(variables_json)} --task-id {task_id}'
                        _debug_log(f"STEP 5: Command built: {cmd[:300]}")

                        _debug_log("STEP 6: Calling Platform.open_new_terminal...")
                        result_ok = Platform.open_new_terminal(cmd, title=f"VAF Workflow: {workflow_id}")
                        _debug_log(f"STEP 7: open_new_terminal returned: {result_ok}")
                    except Exception as e:
                        _debug_log(f"ERROR: {type(e).__name__}: {e}")
                        raise
                    
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

    def _extract_explicit_workflow(self, user_input: str) -> tuple[str | None, str]:
        """
        Detect explicit @workflow_id hints in user input.
        Returns (workflow_id, cleaned_input).
        """
        try:
            from vaf.workflows.templates import WORKFLOW_TEMPLATES
            if not WORKFLOW_TEMPLATES:
                return None, user_input
            workflow_ids = list(WORKFLOW_TEMPLATES.keys())
        except Exception:
            return None, user_input

        # Build a case-insensitive lookup with normalization
        def normalize(token: str) -> str:
            return token.lower().replace("-", "_")

        workflow_lookup = {wf_id.lower(): wf_id for wf_id in workflow_ids}
        normalized_lookup = {normalize(wf_id): wf_id for wf_id in workflow_ids}
        token_match = re.search(r'@([a-zA-Z0-9_-]+)', user_input)
        if not token_match:
            return None, user_input

        token = token_match.group(1).strip()
        workflow_id = workflow_lookup.get(token.lower()) or normalized_lookup.get(normalize(token))
        if not workflow_id:
            return None, user_input

        cleaned = re.sub(r'@' + re.escape(token), "", user_input, count=1).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        return workflow_id, cleaned

    def _decay_recent_tools(self) -> None:
        """
        Reduce TTL counters for recently used tools.
        """
        for name in list(self._recent_tools.keys()):
            self._recent_tools[name] -= 1
            if self._recent_tools[name] <= 0:
                del self._recent_tools[name]

    def _record_tool_used(self, name: str | None) -> None:
        """
        Remember a tool for the next N user turns.
        """
        if not name or name not in self.tools:
            return
        # Move to end to preserve recency ordering
        if name in self._recent_tools:
            self._recent_tools.pop(name)
        # Tool stays available for exactly _recent_tool_keep_turns after this turn
        self._recent_tools[name] = self._recent_tool_keep_turns

    def _get_recent_tools(self) -> list[str]:
        return [name for name in self._recent_tools.keys() if name in self.tools]

    def _merge_tool_lists(self, primary: list[str] | None, extra: list[str] | None) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for name in (primary or []):
            if name in self.tools and name not in seen:
                merged.append(name)
                seen.add(name)
        for name in (extra or []):
            if name in self.tools and name not in seen:
                merged.append(name)
                seen.add(name)
        return merged
    
    def _route_tools(self, user_input: str) -> List[str]:
        """
        Dynamically selects relevant tools based on user input AND context.
        Uses a lightweight LLM call to classify intent.
        """
        if not self.tools:
            return []
            
        u_lower = user_input.lower()
        forced_tools = set()
        
        # 0. Context Awareness (Intent)
        context_str = ""
        if hasattr(self, 'context_manager'):
            intent = self.context_manager.intent
            if intent.primary_goal:
                context_str = f"Current Goal: {intent.primary_goal}\n"
            if intent.sub_goals:
                context_str += f"Recent Topics: {', '.join(intent.sub_goals)}\n"
        
        # Add Last Assistant Message (Immediate Context)
        if len(self.history) >= 2:
            last_msg = self.history[-2]
            if last_msg.get('role') == 'assistant':
                content = str(last_msg.get('content', ''))[:300].replace('\n', ' ')
                context_str += f"Last Assistant Message: \"{content}\"\n"
        
        # Heuristic checks (Fast Path)
        if "weather" in u_lower or "wetter" in u_lower:
             if "web_search" in self.tools: forced_tools.add("web_search")
        
        # Coding Heuristics
        if any(kw in u_lower for kw in [
            "code", "function", "class", "script", "program", "debug", "fix", 
            "python", "javascript", "refactor", "implement"
        ]):
            if "coding_agent" in self.tools:
                forced_tools.add("coding_agent")
            if "git_status" in self.tools:
                 forced_tools.add("git_status")
                 forced_tools.add("git_add_commit")
        
        # Git Heuristics
        if any(kw in u_lower for kw in ["git", "commit", "push", "pull", "repo", "branch"]):
            if "git_status" in self.tools:
                forced_tools.add("git_status")
                forced_tools.add("git_add_commit")
                forced_tools.add("git_log")
        
        # Research Heuristics
        if any(kw in u_lower for kw in ["research", "recherche", "analyse", "report", "comprehensive", "umfassend", "deep"]):
             if "research_agent" in self.tools:
                 forced_tools.add("research_agent")
             if "web_search" in self.tools:
                 forced_tools.add("web_search")

        # Web Search Heuristics
        if any(kw in u_lower for kw in [
            "search", "find", "google", "look up", "news", "weather", 
            "wetter", "nachrichten", "suche", "wer ist", "who is", "what is",
            "wer oder was", "who or what", "tell me about", "explain", "erkläre",
            "info", "information", "definition", "meaning", "bedeutung",
            "wie funktioniert", "how does", "what are", "was sind"
        ]):
             if "web_search" in self.tools:
                 forced_tools.add("web_search")

        # 1. Create a simplified list of tools
        tool_info = []
        for name, tool_instance in self.tools.items():
            description = getattr(tool_instance, 'description', 'No description available.')
            tool_info.append(f"- {name}: {description}")
        
        tool_list_str = "\n".join(tool_info)

        # 2. Build the prompt for the router WITH CONTEXT
        # CRITICAL: Router must NEVER chat - only output tool names or nothing. No greeting, no explanation.
        tool_names_list = ", ".join(sorted(self.tools.keys()))
        prompt = (
            f"You are a tool router. Your ONLY output must be a comma-separated list of tool names from this exact list, or nothing.\n"
            f"Allowed names: {tool_names_list}\n\n"
            f"Tools with descriptions:\n{tool_list_str}\n\n"
            f"{context_str}"
            f"User request: \"{user_input}\"\n\n"
            f"CRITICAL: Reply with ONLY tool names from the list above (e.g. web_search, memory_save). No greeting, no 'How can I help', no explanation. Any other text is invalid.\n"
            f"Tools:"
        )

        # 3. Make a lightweight LLM call
        messages = [{"role": "user", "content": prompt}]
        selected_tools_str = ""
        try:
            from vaf.cli.ui import UI
            UI.event("Info", "Routing Tools...", style="dim")
            with UI.console.status("[bold cyan] Routing Tools...[/bold cyan]", spinner="dots"):
                if self.use_server:
                    payload = {
                        "messages": messages,
                        "max_tokens": 2048,  # Increased for reasoning models
                        "temperature": 0.0,  # Deterministic for routing
                        "stream": False
                    }
                    res = None
                    for _router_attempt in range(10):  # Retry on 503 (model loading), ~20s max
                        resp = requests.post(
                            "http://127.0.0.1:8080/v1/chat/completions",
                            json=payload,
                            timeout=120
                        )
                        if resp.status_code == 503:
                            time.sleep(2)
                            continue
                        res = resp.json()
                        if resp.status_code != 200 and isinstance(res.get('error'), dict):
                            err = res.get('error', {})
                            if err.get('code') == 503:
                                time.sleep(2)
                                continue
                        break
                    if res is None and resp is not None:
                        try:
                            res = resp.json()
                        except Exception:
                            res = {}
                    if res is None:
                        res = {}
                    if 'choices' in res and len(res['choices']) > 0:
                        msg = res['choices'][0]['message']
                        # Try content first, then reasoning_content (for reasoning models like VQ-1)
                        selected_tools_str = msg.get('content') or ''
                        # If content is empty but reasoning_content exists, extract tool names from it
                        if not selected_tools_str.strip() and msg.get('reasoning_content'):
                            reasoning = msg.get('reasoning_content', '')
                            # Try to find tool names in the reasoning
                            import re
                            # Look for comma-separated tool names or individual tool names
                            for tool_name in self.tools.keys():
                                if tool_name in reasoning:
                                    if selected_tools_str:
                                        selected_tools_str += ", "
                                    selected_tools_str += tool_name
                    elif 'error' in res:
                        raise Exception(f"Server error: {res['error']}")
                    else:
                        raise Exception("Invalid server response (no choices)")
                elif self.llm:
                     output = self.llm.create_chat_completion(
                         messages=messages,
                         max_tokens=1224,
                         temperature=0.0
                     )
                     selected_tools_str = output['choices'][0]['message']['content']
                elif self.api_backend:
                    # API Backend returns a generator of strings
                    response_chunks = list(self.api_backend.chat_completion(
                        messages=messages,
                        max_tokens=1224,
                        temperature=0.0,
                        stream=False
                    ))
                    selected_tools_str = "".join(str(c) for c in response_chunks)
                else:
                    UI.event("Router Debug", "No backend available for routing", style="yellow")
        except Exception as e:
            # On error, log it but don't crash. Return [] to trigger safety net.
            from vaf.cli.ui import UI
            UI.event("Router Debug", f"LLM Call Failed: {e}", style="red")
            return []

        # 4. Parse the response
        if not selected_tools_str:
            from vaf.cli.ui import UI
            if forced_tools:
                UI.event("Router", f"Script-based selection: {', '.join(forced_tools)}", style="dim")
                return list(forced_tools)
            else:
                UI.event("Router", "No tools selected (fallback)", style="dim")
                return [] # Will trigger safety net in chat_step
            
        import re
        # First, remove any thinking tags (e.g., <think>...</think>)
        clean_str = re.sub(r'<think>.*?</think>', '', selected_tools_str, flags=re.IGNORECASE | re.DOTALL).strip()
        
        # Handle cases where only the closing tag is present (e.g. output started with thinking but start tag was cut)
        if '</think>' in clean_str.lower():
             parts = re.split(r'</think>', clean_str, flags=re.IGNORECASE)
             clean_str = parts[-1].strip()
        
        # Cleanup any remaining stray tags
        clean_str = re.sub(r'</?think>', '', clean_str, flags=re.IGNORECASE).strip()
        # Then remove common prefixes
        clean_str = re.sub(r'^(answer|result|selected|tools|relevant|output):\s*', '', clean_str, flags=re.IGNORECASE).strip()
        tool_names = [name.strip() for name in clean_str.split(',') if name.strip()]
        # Only accept parsed tokens that are actual tool names (never show chat as "LLM-based")
        valid_from_llm = [n for n in tool_names if n in self.tools]
        # Fallback: if LLM chatted instead of listing, scan response for tool name substrings
        if not valid_from_llm and clean_str:
            for t in sorted(self.tools.keys(), key=len, reverse=True):
                if t in clean_str and t not in valid_from_llm:
                    valid_from_llm.append(t)
        
        from vaf.cli.ui import UI
        if forced_tools:
            UI.event("Router", f"Script-based: {', '.join(forced_tools)}", style="dim")
        if valid_from_llm:
            UI.event("Router", f"LLM-based: {', '.join(valid_from_llm)}", style="dim")
        elif tool_names and not valid_from_llm:
            UI.event("Router", "No tools selected (Router response was not a valid tool list)", style="dim")
        elif not forced_tools:
            UI.event("Router", "No tools selected", style="dim")
        
        combined_tools = set(valid_from_llm) | forced_tools
        valid_tools = [name for name in combined_tools if name in self.tools]
        
        return valid_tools

    def _validate_final_answer(self, draft: str, user_intent: str) -> bool:
        """
        Validates if the draft answer is a substantial response or just meta-talk.
        Returns True if valid, False if it's a 'Meta-Response' (Status only).
        """
        if not draft or not user_intent:
            return True
            
        clean_draft = re.sub(r'<[^>]*>', '', draft).strip().lower()
        
        # 1. Check for Meta-Patterns (Status reports without content)
        meta_patterns = [
            "processing result", "ergebnis verarbeitet", 
            "sub-agent has finished", "sub-agent hat fertig",
            "task completed", "aufgabe abgeschlossen",
            "working on your request", "arbeite an deiner anfrage",
            "the result is ready", "das ergebnis ist bereit",
            "i have analyzed the files", "ich habe die dateien analysiert",
            "here are the results", "hier sind die ergebnisse"
        ]
        
        # If the answer is very short and matches a meta pattern, it's likely invalid
        is_meta = any(p in clean_draft for p in meta_patterns) and len(clean_draft) < 200
        
        # 2. Check for Content-Linkage
        # Extract key entities from intent (nouns/objects)
        keywords = [w.lower() for w in re.findall(r'\b\w{4,}\b', user_intent)]
        keyword_hits = sum(1 for k in keywords if k in clean_draft)
        
        # Logic: If it's meta-talk AND has no relation to the user's keywords, it's a drift
        if is_meta and keyword_hits < 1:
            return False
            
        return True

    def chat_step(self, user_input: str, stream_callback=None, auto_retry=False, skip_input=False, disable_workflows=False, disable_tools=False, memory_context=None):
        from vaf.cli.ui import UI
        
        try:
            append_domain_log("backend", "chat_step_start")
        except Exception:
            pass

        self.context_manager.decay_state()
        
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
        new_prompt = None
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
            
            # Rebuild system prompt (includes User identity so e.g. "Hey" -> model knows "that's Mert")
            new_prompt = self.prompt_manager.build_prompt(
                self.filename,
                username=getattr(self, "_current_username", None),
                user_scope_id=getattr(self, "_current_user_scope_id", None),
                current_source=getattr(self, "_current_chat_source", None),
                last_interaction=get_last_interaction(getattr(self, "_current_user_scope_id", None)),
            )
        
        # ------------------------------------------------------------------
        # Context Compression: Check threshold and compress if needed
        # ------------------------------------------------------------------
        if hasattr(self, 'context_manager') and self.context_manager.should_compress(self.history):
            UI.event("Context", f"Threshold reached ({self.context_manager.get_usage_percent(self.history):.0%}) - compressing...", style="warning")
            self.history = self.context_manager.compress(self.history)
            
            # Inject PROACTIVE CONTEXT GLUE (Stability)
            if new_prompt is not None:
                context_glue = self.context_manager._build_context_summary()
                if context_glue:
                    new_prompt += f"\n\n{context_glue}"
            
            # Preserve Project Context if it exists
            if len(self.history) > 0 and self.history[0]["role"] == "system":
                current_content = self.history[0]["content"]
                if new_prompt is not None:
                    if "## PROJECT CONTEXT" in current_content:
                        project_context_part = current_content.split("## PROJECT CONTEXT", 1)[1]
                        new_prompt += f"\n\n## PROJECT CONTEXT{project_context_part}"
                    self.history[0]["content"] = new_prompt
                # UI.event("Brain", f"Context adjusted: {list(self.prompt_manager.active_modules.keys())}", style="dim")
        
        # Apply dynamic system prompt every turn (not only when compressing) so User identity is always current
        if new_prompt is not None and len(self.history) > 0 and self.history[0].get("role") == "system":
            current_content = self.history[0]["content"]
            if "## PROJECT CONTEXT" in current_content and "## PROJECT CONTEXT" not in new_prompt:
                project_context_part = current_content.split("## PROJECT CONTEXT", 1)[1]
                new_prompt = new_prompt + f"\n\n## PROJECT CONTEXT{project_context_part}"
            self.history[0]["content"] = new_prompt

        # Keep language pinned to the user's most recent message.
        # This must happen early so it affects workflow selection + normal chat replies.
        if not skip_input:
            self._refresh_language_hint(user_input)
            
        # Broadcast context status to WebUI (X-Ray Vision)
        self._broadcast_context_status()

        # 0. Context Management (Trim/Summarize) - BEFORE adding user input
        self.manage_context()
        try:
            append_domain_log("backend", "chat_step_after_manage_context")
        except Exception:
            pass

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
        
        # Always add user input if provided, even if skip_input=True (which skips analysis/overhead)
        if user_input:
            self.history.append({"role": "user", "content": user_input})
            
            # ═══════════════════════════════════════════════════════════════════════
            # FIRST-TIME USER: Automatic Greeting & Language Detection
            # ═══════════════════════════════════════════════════════════════════════
            # If user_identity.json is still default (no name, language, or preferences),
            # inject a friendly greeting in the user's detected language asking for their name.
            # This creates a natural onboarding flow without forcing a form.
            if not skip_input:
                try:
                    from vaf.auth.user_workspace import get_user_workspace
                    username = getattr(self, "_current_username", None)
                    if username:
                        ws = get_user_workspace(username)
                        user_identity = ws.get_user_identity()
                        
                        # Check if this is a first-time user (default values only)
                        if ws.is_default_user_identity(user_identity):
                            # Detect language from user's first message
                            try:
                                import langid
                                detected_lang, confidence = langid.classify(user_input)
                                # Map to common 2-letter codes
                                lang_code = detected_lang if detected_lang in ("de", "en", "es", "fr", "it", "zh", "ja") else "en"
                            except Exception:
                                lang_code = "en"  # Fallback to English
                            
                            # Get agent identity for personalized greeting
                            agent_identity = ws.get_identity()
                            agent_name = agent_identity.get("name", "VAF")
                            agent_emoji = agent_identity.get("emoji", "🤖")
                            
                            # Multilingual greetings
                            greetings = {
                                "de": f"Hallo! Ich bin {agent_name} {agent_emoji} – freut mich, dich kennenzulernen! Bevor ich dir helfe, würde ich gerne wissen: **Wie heißt du?** (Das hilft mir, unsere Unterhaltung persönlicher zu gestalten.)",
                                "en": f"Hey! I'm {agent_name} {agent_emoji} – nice to meet you! Before I help you, I'd like to know: **What's your name?** (This helps me make our conversations more personal.)",
                                "es": f"¡Hola! Soy {agent_name} {agent_emoji} – encantado de conocerte! Antes de ayudarte, me gustaría saber: **¿Cómo te llamas?**",
                                "fr": f"Salut ! Je suis {agent_name} {agent_emoji} – ravi de te rencontrer ! Avant de t'aider, j'aimerais savoir : **Comment t'appelles-tu ?**",
                                "it": f"Ciao! Sono {agent_name} {agent_emoji} – piacere di conoscerti! Prima di aiutarti, vorrei sapere: **Come ti chiami?**",
                                "zh": f"你好！我是 {agent_name} {agent_emoji} – 很高兴认识你！在我帮助你之前，我想知道：**你叫什么名字？**",
                                "ja": f"こんにちは！私は {agent_name} {agent_emoji} です – はじめまして！お手伝いする前に、**お名前は何ですか？**",
                            }
                            
                            greeting = greetings.get(lang_code, greetings["en"])
                            
                            # Inject system message BEFORE LLM processes, instructing it to:
                            # 1. Use the greeting above
                            # 2. Address the user's original message
                            # 3. Use update_user_identity tool when they provide their name
                            first_time_instruction = (
                                f"## FIRST-TIME USER DETECTED\\n\\n"
                                f"This user has just sent their first message. Their language appears to be: **{lang_code}** (detected from input).\\n\\n"
                                f"**Your Response should:**\\n"
                                f"1. Start with this greeting: \\\"{greeting}\\\"\\n"
                                f"2. Then briefly address their original message: \\\"{user_input}\\\"\\n"
                                f"3. When they tell you their name (in their next message), use `update_user_identity` tool to save it along with `preferred_language={lang_code}`.\\n\\n"
                                f"Keep it natural and friendly – you're meeting someone new!"
                            )
                            
                            # Insert system instruction right before LLM call
                            # This goes into history so it's immediately before the assistant's response
                            self.history.append({"role": "system", "content": first_time_instruction})
                            
                            UI.event("Onboarding", f"First-time user detected (lang: {lang_code})", style="success")
                except Exception as e:
                    # Don't crash if first-time detection fails - just skip it
                    UI.event("Onboarding", f"First-time check failed: {e}", style="dim")
            
            # 🔒 INTENT LOCK: Save the fresh user intent to persistence
            if hasattr(self, 'main_persistence') and self.main_persistence:
                try:
                    # Update the "North Star" for the session
                    self.main_persistence.update_user_intent(user_input)
                except Exception:
                    pass
            
            # LIVE CONTEXT UPDATE: Ensure intent is fresh for the router immediately
            if hasattr(self, 'context_manager'):
                self.context_manager.update_intent(user_input)
                self.context_manager.update_state({"role": "user", "content": user_input})
        
        # Snapshot history AFTER adding user input. 
        # This index points to the user message we just added.
        # Everything BEFORE and INCLUDING this index is our "safe" context.
        history_snapshot_len = len(self.history) - 1

        # Dynamic Tool Selection (Tool Router)
        # Only route tools on a new, non-empty input
        if not auto_retry and not skip_input and user_input:
            from vaf.cli.ui import UI
            selected_tools = self._route_tools(user_input)

            if selected_tools:
                recent_tools = self._get_recent_tools()
                if recent_tools:
                    selected_tools = self._merge_tool_lists(selected_tools, recent_tools)

            # Decay AFTER merge so tools stay for the full N turns
            self._decay_recent_tools()

            # If router returned only one tool, include list_tools as a fallback helper.
            if (
                selected_tools
                and len(selected_tools) == 1
                and "list_tools" in self.tools
                and "list_tools" not in selected_tools
            ):
                selected_tools = list(selected_tools) + ["list_tools"]

            # Memory/identity tools are ALWAYS included when we have a restricted set (no duplicates).
            # Only skipped when Safety Net = ALL tools (would be redundant).
            if selected_tools:
                for name in ("update_intent", "update_working_memory", "memory_search", "memory_save", "update_user_identity"):
                    if name in self.tools and name not in selected_tools:
                        selected_tools = list(selected_tools) + [name]
                # Messaging tools: only add those for which the user has the connection
                try:
                    from vaf.core.messaging_connections import get_messaging_connections
                    conn = get_messaging_connections(
                        username=getattr(self, "_current_username", None),
                        user_scope_id=getattr(self, "_current_user_scope_id", None),
                    )
                    for ch in conn.get("available") or []:
                        tool_name = {"telegram": "send_telegram", "discord": "send_discord", "slack": "send_slack"}.get(ch)
                        if tool_name and tool_name in self.tools and tool_name not in selected_tools:
                            selected_tools = list(selected_tools) + [tool_name]
                except Exception:
                    pass

            # ALWAYS show final tools as system message for debugging consistency
            final_list = ", ".join(selected_tools) if selected_tools else "None (Safety Net -> ALL)"
            UI.event("Router", f"Final tools: {final_list}", style="dim")
            # Push to Web UI directly so it is never dropped by log throttle
            try:
                from vaf.core.web_interface import get_web_interface
                from vaf.core.subagent_ipc import get_current_session_id
                from datetime import datetime
                session_id = get_current_session_id()
                get_web_interface()._push_session_update(session_id, {
                    "type": "new_log",
                    "entry": {
                        "timestamp": datetime.now().isoformat(),
                        "message": f"Final tools: {final_list}",
                        "level": "info",
                        "source": "Router"
                    }
                })
            except Exception:
                pass

            # SAFETY NET: If router returns empty list, fallback to ALL tools
            # Otherwise the model gets 0 tools and hallucinates using them.
            if not selected_tools:
                UI.event("Router", "Safety Net: Using ALL tools (Router found none)", style="dim")
                self._active_tools = None
            else:
                self._active_tools = selected_tools
        else:
            # On retries or for internal steps, use all tools
            self._active_tools = None

        if not skip_input and user_input:
            # 0.5. Context Management AGAIN - AFTER adding user input (in case it pushed us over limit)
            self.manage_context()
            # Re-apply after potential compression to ensure the hint stays in history[0].
            self._refresh_language_hint(user_input)

        # 1. Adaptive Temperature Check
        # Check if auto-temperature is enabled (default: True for backward compatibility)
        temperature_auto = Config.get("temperature_auto", True)
        target_temp = Config.get("temperature", 0.7)

        if temperature_auto and not auto_retry and not skip_input:
             # Sub-Agent Intent Analysis (can take time)
             from vaf.cli.ui import UI as UI_Class
             UI_Class.event("Info", "Analyzing Intent...", style="dim")
             with UI_Class.console.status("[bold cyan](O_O)  Step 2/2: Analyzing Intent...[/bold cyan]", spinner="dots"):
                 dynamic_temp = self.analyze_intent(user_input)

             UI.event("Step 2/2", f"Adaptive State: Temperature set to {dynamic_temp} based on intent.", style="dim")
             target_temp = dynamic_temp
        elif not temperature_auto:
             UI.event("Temperature", f"Manual: {target_temp}", style="dim")

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
        MAX_EMPTY_RETRIES = 10  # Allow up to 10 attempts before hard stop
        current_temp = target_temp
        
        # Main chat loop with retries for empty responses
        full_response = ""
        full_content = ""
        full_reasoning = ""
        clean_content = ""
        streaming_tools = {}
        tool_calls_detected = []
        
        while empty_retry_count < MAX_EMPTY_RETRIES:
            # 1. Prepare Request
            full_response = ""     # Reset for this turn
            full_content = ""      # Reset for this turn
            full_reasoning = ""    # Reset for this turn
            _generation_stopped = False  # Track if user stopped generation

            streaming_tools = {}
            tool_calls_detected = []
            auto_continue = False  # Track if response was cut off

            # Log which LLM backend is used (for debugging: VQ1 vs API vs server)
            try:
                if self.api_backend:
                    backend_type = f"api({getattr(self.api_backend, 'provider_name', self.provider)})"
                elif self.use_server:
                    backend_type = "server(8080)"
                else:
                    backend_type = "library(llama-cpp-python)"
                append_domain_log("backend", f"chat_step backend={backend_type}")
            except Exception:
                pass

            # API Backend Path (OpenAI, Anthropic, DeepSeek, Google, OpenRouter)
            if self.api_backend:
                try:
                    # CRITICAL: Sanitize history before API call
                    # Fix any old tool_calls with missing/null type field
                    for msg in self.history:
                        if isinstance(msg, dict) and "tool_calls" in msg:
                            for tc in msg["tool_calls"]:
                                # Fix missing 'type'
                                if isinstance(tc, dict) and ("type" not in tc or tc.get("type") is None):
                                    tc["type"] = "function"
                                # Fix missing 'id'
                                if isinstance(tc, dict) and ("id" not in tc or tc.get("id") is None):
                                    import os
                                    tc["id"] = f"call_{os.urandom(4).hex()}"
                    
                    
                    # Prepare messages
                    prepared_messages = self._prepare_messages(self.history)
                    if prepared_messages:
                        if memory_context and memory_context.strip():
                            memory_msg = {"role": "system", "content": (
                                "## Memory context (relevant to this query)\n\n"
                                "Use when relevant; you may cite briefly (e.g. from memory). "
                                "If you call memory_search, pass only a SHORT query (e.g. 'user name'), never your full thinking text.\n\n"
                                + memory_context.strip()
                            )}
                        else:
                            memory_msg = {"role": "system", "content": (
                                "## Memory context (relevant to this query)\n\n"
                                "(No memories found for this query.) "
                                "Use this section to answer 'who am I?' or 'what do you remember?'; if none, say so and offer to remember. "
                                "If the user's question is vague (e.g. 'what is this user' or 'what do you know'), ask them to clarify what they want to know rather than guessing. "
                                "If you call memory_search, pass only a SHORT query (e.g. 'user name'), never your full thinking text. "
                                "Do NOT use memory_save to look up – use memory_search or this block. memory_save only saves NEW facts when the user asks to remember something."
                            )}
                        prepared_messages = [prepared_messages[0], memory_msg] + prepared_messages[1:]
                    # Disable tools if requested
                    current_tools = self.TOOLS if not disable_tools else None
                    tool_choice = "auto" if current_tools else "none" # Default to auto if tools, none otherwise

                    first_token = True
                    # json, sys, escape already imported globally
                    tool_call_accumulator = {}

                    # DEBUG: Track chunks received from API
                    _chunk_count = 0
                    for chunk in self.api_backend.chat_completion(
                        messages=prepared_messages,
                        temperature=current_temp,
                        max_tokens=8192,
                        stream=True,
                        tools=current_tools,
                        tool_choice=tool_choice  # Pass tool_choice if set
                    ):
                        # Check for stop request
                        session_id_for_stop = getattr(self, 'current_session_id', None) or self._session_id
                        if session_id_for_stop:
                            from vaf.core.task_queue import TaskQueue
                            tq = TaskQueue()
                            if tq.should_stop(session_id_for_stop):
                                tq.clear_stop(session_id_for_stop)
                                UI.event("System", "Generation stopped by user", style="warning")
                                if stream_callback:
                                    stream_callback("\n\n[Generation stopped by user]")
                                _generation_stopped = True
                                break

                        _chunk_count += 1
                        if not chunk: continue

                        # DEBUG: Log chunk (consolidated in backend.log)
                        try:
                            chunk_preview = str(chunk)[:100].replace('\n', '\\n')
                            append_domain_log("backend", f"[CHUNK {_chunk_count}] {chunk_preview}")
                        except: pass

                        # Check for error/warning messages from backend
                        if isinstance(chunk, str) and chunk.startswith("[Error]"):
                            UI.error(chunk)
                            content_for_history = chunk
                            break
                            
                        # Try to parse as internal control message (Tools/Status)
                        is_control_msg = False
                        if chunk.startswith("{"):
                            try:
                                data = json.loads(chunk)
                                if isinstance(data, dict) and any(k in data for k in ["tool_calls", "finish_reason", "tool_use"]):
                                    is_control_msg = True
                                    
                                    # Handle Finish Reason
                                    if "finish_reason" in data:
                                        if data["finish_reason"] == "length":
                                            UI.event("System", "Response cut off - Auto-continuing...", style="dim")
                                    
                                    # Handle Tool Calls (Streaming Aggregation)
                                    elif "tool_calls" in data:
                                        for tc in data["tool_calls"]:
                                            idx = tc.get("index", 0)
                                            if idx not in tool_call_accumulator:
                                                tool_call_accumulator[idx] = {
                                                    "index": idx,
                                                    "id": tc.get("id"),
                                                    "type": tc.get("type", "function"),
                                                    "function": {"name": tc.get("function", {}).get("name"), "arguments": ""}
                                                }
                                            if tc.get("id"): tool_call_accumulator[idx]["id"] = tc.get("id")
                                            func_data = tc.get("function", {})
                                            if func_data.get("name"): tool_call_accumulator[idx]["function"]["name"] = func_data.get("name")
                                            if func_data.get("arguments"): tool_call_accumulator[idx]["function"]["arguments"] += func_data.get("arguments")

                                    elif "tool_use" in data:
                                        # Anthropic format
                                        tool_calls_detected.append({
                                            "id": data["tool_use"].get("id", f"call_{os.urandom(4).hex()}"),
                                            "type": "function",
                                            "function": {
                                                "name": data["tool_use"].get("name"),
                                                "arguments": json.dumps(data["tool_use"].get("input", {}))
                                            }
                                        })
                            except json.JSONDecodeError:
                                pass # Not valid JSON, treat as content
                        
                        # If not a control message, treat as regular content
                        if not is_control_msg:
                            full_response += chunk
                            
                            # DEBUG: Log content chunk (consolidated in backend.log)
                            try:
                                append_domain_log("backend", f"[CONTENT] len={len(chunk)} callback={stream_callback is not None}")
                            except: pass
                            
                            # Live Update (TUI)
                            if first_token:
                                UI.event("Response", "", style="bold green", end="")
                                first_token = False
                            
                            # Stream to WebUI / Host Callback
                            if stream_callback:
                                # Delegate printing and web streaming to the callback
                                # Pass RAW chunk so <think> tags can be parsed by the TUI
                                stream_callback(chunk)
                            else:
                                # Fallback for standalone usage: Print directly
                                print(escape(chunk), end="")
                                sys.stdout.flush()
                            
                            # Also populate full_content for downstream checks
                            full_content += chunk

                    # Fallback: Some API streams emit no content (tool-only or SDK quirk)
                    # Ensure WebUI still receives a response when streaming yields nothing.
                    if not full_response and not tool_calls_detected:
                        try:
                            fallback_chunks = list(self.api_backend.chat_completion(
                                messages=prepared_messages,
                                temperature=current_temp,
                                max_tokens=8192,
                                stream=False,
                                tools=current_tools,
                                tool_choice=tool_choice
                            ))
                            fallback_text = "".join(str(c) for c in fallback_chunks)
                            if fallback_text:
                                full_response += fallback_text
                                full_content += fallback_text
                                if stream_callback:
                                    stream_callback(fallback_text)
                        except Exception as e:
                            UI.error(f"API Backend Error (fallback): {e}")
                            # Do not return: let execution continue so the empty-response
                            # handler runs (cooldown + retry) instead of exiting chat_step.

                    # POST-LOOP: Convert accumulator to list
                    for idx, tc_data in sorted(tool_call_accumulator.items()):
                        # Ensure ID and Type
                        if not tc_data.get("id"): 
                            tc_data["id"] = f"call_{os.urandom(4).hex()}"
                        if not tc_data.get("type"): 
                            tc_data["type"] = "function"
                            
                        tool_calls_detected.append(tc_data)
                    
                    # Validate tool calls (guard against missing tool names)
                    if tool_calls_detected:
                        valid_tool_calls = []
                        for tc in tool_calls_detected:
                            func = tc.get("function") or {}
                            name = func.get("name") or tc.get("name")
                            if name:
                                func["name"] = name
                                tc["function"] = func
                                valid_tool_calls.append(tc)
                        if len(valid_tool_calls) != len(tool_calls_detected):
                            UI.event("System", "API returned tool calls with missing names. Dropping invalid entries.", style="warning")
                        tool_calls_detected = valid_tool_calls
                        if not tool_calls_detected:
                            err_msg = "[Error] API returned tool calls without a function name. Please retry."
                            UI.error(err_msg)
                            if stream_callback:
                                stream_callback(err_msg)
                            return err_msg

                    if stream_callback: stream_callback("\n")
                    
                except Exception as e:
                    err_msg = f"[Error] API backend failure: {e}"
                    UI.error(err_msg)
                    if stream_callback:
                        stream_callback(err_msg)
                    return err_msg
            
            elif self.use_server:
                # Proactive Context Management: Compress before request to prevent overflow
                # CRITICAL: Calculate threshold dynamically - Tools consume context but can't be compressed!
                current_tokens, max_tokens = self.get_token_usage()
                
                # Reserve space for response (1500 tokens)
                # current_tokens already includes tool definitions + history from get_token_usage()
                response_buffer = 1500
                safe_limit = max_tokens - response_buffer
                
                if current_tokens > safe_limit:
                    UI.event("Context", f"Proactive compression: {current_tokens}/{max_tokens} tokens", style="warning")
                    self.manage_context()
                    
                    # Double-check after compression
                    current_tokens, _ = self.get_token_usage()
                    if current_tokens > safe_limit:
                        # Still too big - aggressive pruning needed
                        UI.event("Context", "Standard compression insufficient. Pruning aggressively...", style="warning")
                        # Keep only system + last 4 messages
                        if len(self.history) > 5:
                            system_msg = [self.history[0]] if self.history and self.history[0].get("role") == "system" else []
                            self.history = system_msg + self.history[-4:]
                            UI.event("Context", f"Reduced to {len(self.history)} messages", style="info")
                
                # Retry loop for 503 (Model Loading), 500 (Context Overflow), and 400 (Context Size Error)
                response = None
                for _attempt in range(15):  # Try for ~30 seconds
                    try:
                        # CRITICAL: Rebuild payload with current history (may have been compressed)
                        # Prepare messages for specific model quirks (e.g. Gemma)
                        prepared_messages = self._prepare_messages(self.history)
                        if prepared_messages:
                            if memory_context and memory_context.strip():
                                memory_msg = {"role": "system", "content": (
                                    "## Memory context (relevant to this query)\n\n"
                                    "Use when relevant; you may cite briefly (e.g. from memory). "
                                    "If you call memory_search, pass only a SHORT query (e.g. 'user name'), never your full thinking text.\n\n"
                                    + memory_context.strip()
                                )}
                            else:
                                memory_msg = {"role": "system", "content": (
                                    "## Memory context (relevant to this query)\n\n"
                                    "(No memories found for this query.) "
                                    "Use this section to answer 'who am I?' or 'what do you remember?'; if none, say so and offer to remember. "
                                    "If the user's question is vague (e.g. 'what is this user' or 'what do you know'), ask them to clarify what they want to know rather than guessing. "
                                    "If you call memory_search, pass only a SHORT query (e.g. 'user name'), never your full thinking text. "
                                    "Do NOT use memory_save to look up – use memory_search or this block. memory_save only saves NEW facts when the user asks to remember something."
                                )}
                            prepared_messages = [prepared_messages[0], memory_msg] + prepared_messages[1:]
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

                        # X-RAY: Send EXACT payload to WebUI for inspection
                        try:
                            from vaf.core.web_interface import get_web_interface
                            # json is already imported globally
                            # Extract System Prompt (first system message)
                            sys_prompt = next((m["content"] for m in prepared_messages if m["role"] == "system"), "")
                            # Extract History (everything else)
                            hist_msgs = [m for m in prepared_messages if m["role"] != "system"]
                            
                            # Calculate RAG part specifically for the bar
                            rag_part = ""
                            if "## Memory context" in sys_prompt:
                                parts = sys_prompt.split("## Memory context")
                                if len(parts) > 1:
                                    rag_part = parts[1].split("##")[0].strip()

                            get_web_interface().push_update({
                                "type": "real_context_payload",
                                "system": sys_prompt,
                                "rag_preview": rag_part,
                                "history": hist_msgs,
                                # We can't get exact tokens easily here without a tokenizer, 
                                # but we can send the raw text so the frontend can display it perfectly.
                            })
                        except Exception:
                            pass

                        UI.event("Server", "Calling local server (8080)...", style="dim")
                        try:
                            append_domain_log("backend", f"calling_8080 attempt={_attempt + 1}")
                        except Exception:
                            pass
                        # (connect_sec, read_sec): read applies per chunk so we don't hang forever if server stalls
                        # Reduced from 300s to 60s - if no data for 60s, something is wrong
                        try:
                            append_domain_log("backend", f"sending_request payload_size={len(str(payload))}")
                        except Exception:
                            pass
                        response = requests.post(
                            "http://127.0.0.1:8080/v1/chat/completions",
                            json=payload,
                            stream=True,
                            timeout=(30, 60)
                        )
                        try:
                            append_domain_log("backend", f"response_received status={response.status_code}")
                        except Exception:
                            pass

                        # DEBUG TRACER
                        # UI.event("Debug", f"Raw Response: {response.status_code}")

                        if response.status_code == 503:
                            try:
                                append_domain_log("backend", f"server(8080) 503 model_loading retry={_attempt + 1}/15")
                            except Exception:
                                pass
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
                                pass  # Not a context size error, try generic 400 recovery below
                            # Any 400 (e.g. server doesn't report "exceed"): try one round of compression and truncate last user message (sidebar docs)
                            if _attempt < 3:
                                UI.event("Context", "Request rejected (400). Compressing context...", style="warning")
                                self.manage_context()
                                for msg in reversed(self.history):
                                    if msg.get("role") == "user":
                                        content = str(msg.get("content", ""))
                                        if len(content) > 8000:
                                            msg["content"] = content[:8000] + "\n\n... [Document content truncated to fit context]"
                                            UI.event("Context", "Truncated document block in last message.", style="info")
                                        break
                                UI.event("Context", "Retrying request...", style="success")
                                continue
                            
                        if response.status_code != 200:
                            UI.error(f"Server returned {response.status_code}: {response.text}")
                            return
                        
                        # If successful (200), break retry loop
                        break
                    except requests.exceptions.ConnectionError:
                         UI.event("Server", "Connection failed. Attempting to start server...", style="warning")
                         if hasattr(self, 'server') and self.server:
                             try:
                                 self.server.start_server(self.model_path, n_gpu_layers=self.config.get("gpu_layers", 99), n_ctx=self.config.get("n_ctx", 8192))
                             except Exception as start_err:
                                 UI.error(f"Failed to start server: {start_err}")
                         time.sleep(2)
                         continue
                    except requests.exceptions.ReadTimeout:
                         try:
                             append_domain_log("backend", "server(8080) read_timeout no_data_60s")
                         except Exception:
                             pass
                         UI.event("Server", "Local model took too long (no data for 60s). Retrying...", style="warning")
                         time.sleep(2)
                         continue
                    
                if not response or response.status_code != 200:
                    try:
                        append_domain_log("backend", f"server(8080) unavailable_after_retries status={getattr(response, 'status_code', None)}")
                    except Exception:
                        pass
                    status = getattr(response, 'status_code', None) if response else None
                    UI.error("Server unavailable after retries.")
                    if status == 400:
                        return "[Error] Server rejected the request (HTTP 400). The context may be too large. Try closing the Document Editor or starting a new chat."
                    return "[Error] Server unavailable after retries. Try again or reduce context (e.g. close Document Editor, new chat)."

                # DIAGNOSTIC: Check what the server actually gave us
                # UI.event("Debug", f"Status: {response.status_code} | History: {len(self.history)}")

                first_token = True
                # Chunk-level heartbeat: if no meaningful data for 30s, abort
                CHUNK_HEARTBEAT_TIMEOUT = 30
                last_chunk_time = time.time()
                try:
                    chunk_count = 0
                    for line in response.iter_lines():
                        # Check for stop request
                        session_id_for_stop = getattr(self, 'current_session_id', None) or self._session_id
                        if session_id_for_stop:
                            from vaf.core.task_queue import TaskQueue
                            tq = TaskQueue()
                            if tq.should_stop(session_id_for_stop):
                                tq.clear_stop(session_id_for_stop)
                                UI.event("System", "Generation stopped by user", style="warning")
                                if stream_callback:
                                    stream_callback("\n\n[Generation stopped by user]")
                                _generation_stopped = True
                                break

                        chunk_count += 1
                        if not line:
                            # Empty line - check heartbeat timeout
                            if time.time() - last_chunk_time > CHUNK_HEARTBEAT_TIMEOUT:
                                try:
                                    append_domain_log("backend", f"server(8080) heartbeat_timeout no_data_{CHUNK_HEARTBEAT_TIMEOUT}s")
                                except Exception:
                                    pass
                                UI.event("Server", f"No data from server for {CHUNK_HEARTBEAT_TIMEOUT}s. Aborting...", style="warning")
                                if stream_callback:
                                    stream_callback("\n\n[Server stalled - no response data]")
                                break
                            continue
                        line_text = line.decode('utf-8')
                        last_chunk_time = time.time()  # Reset heartbeat on any data

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
                                # Note: Server may return null instead of empty string
                                content_chunk = delta.get('content') or ''
                                reasoning_chunk = delta.get('reasoning_content') or ''

                                # Method 1: Explicit reasoning_content field (DeepSeek R1, etc.)
                                if reasoning_chunk:
                                    if not is_reasoning:
                                        is_reasoning = True
                                        if stream_callback: stream_callback("<think>")

                                    if first_token:
                                        if stream_callback: stream_callback("")
                                        first_token = False

                                    if stream_callback:
                                        stream_callback(reasoning_chunk)
                                    full_response += reasoning_chunk
                                    full_reasoning += reasoning_chunk

                                # Method 2: Content field (may contain inline <think> tags)
                                if content_chunk:
                                    # Close explicit reasoning if we were in it
                                    if is_reasoning and not reasoning_chunk:
                                        if stream_callback: stream_callback("</think>\n\n")
                                        is_reasoning = False

                                    if first_token:
                                        if stream_callback: stream_callback("")
                                        first_token = False

                                    if stream_callback:
                                        # Content is sent raw - inline <think> tags are preserved
                                        # The WebUI/CLI will parse them
                                        stream_callback(content_chunk)
                                    full_response += content_chunk
                                    full_content += content_chunk
                                    
                            except Exception:
                                pass  # Skip malformed chunks

                    # Close thinking tag if still open at end of stream
                    if is_reasoning and stream_callback:
                        stream_callback("</think>")
                        is_reasoning = False

                    try:
                        append_domain_log("backend", f"stream_complete chunks={chunk_count} content_len={len(full_content)}")
                    except Exception:
                        pass

                except requests.exceptions.ReadTimeout:
                    try:
                        append_domain_log("backend", "server(8080) read_timeout_during_stream")
                    except Exception:
                        pass
                    UI.error("Local model sent no data for 5 minutes (timeout). Partial answer saved.")
                    if is_reasoning and stream_callback:
                        stream_callback("</think>")
                    timeout_msg = "\n\n[Antwort wegen Timeout abgebrochen. Bitte erneut versuchen.]"
                    if stream_callback:
                        stream_callback(timeout_msg)
                    full_response += timeout_msg
                    full_content += timeout_msg
                except Exception as e:
                    UI.error(f"Server Error: {e}")
                    return
            
            else:
                # Library Logic (llama-cpp-python) — must handle reasoning_content like server path for VQ1
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
                        # Check for stop request
                        session_id_for_stop = getattr(self, 'current_session_id', None) or self._session_id
                        if session_id_for_stop:
                            from vaf.core.task_queue import TaskQueue
                            tq = TaskQueue()
                            if tq.should_stop(session_id_for_stop):
                                tq.clear_stop(session_id_for_stop)
                                UI.event("System", "Generation stopped by user", style="warning")
                                if stream_callback:
                                    stream_callback("\n\n[Generation stopped by user]")
                                _generation_stopped = True
                                break

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

                        content_chunk = delta.get('content') or ''
                        reasoning_chunk = delta.get('reasoning_content') or ''

                        # Method 1: Explicit reasoning_content (VQ1 / reasoning models via library)
                        if reasoning_chunk:
                            if not is_reasoning:
                                is_reasoning = True
                                if stream_callback: stream_callback("<think>")
                            if first_token:
                                if stream_callback: stream_callback("")
                                first_token = False
                            if stream_callback:
                                stream_callback(reasoning_chunk)
                            full_response += reasoning_chunk
                            full_reasoning += reasoning_chunk

                        # Method 2: Content (may contain inline <think> tags)
                        if content_chunk:
                            if is_reasoning and not reasoning_chunk:
                                if stream_callback: stream_callback("</think>\n\n")
                                is_reasoning = False
                            if first_token:
                                if stream_callback: stream_callback("")
                                first_token = False
                            if stream_callback:
                                stream_callback(content_chunk)
                            full_response += content_chunk
                            full_content += content_chunk

                    if is_reasoning and stream_callback:
                        stream_callback("</think>")
                        is_reasoning = False
                except Exception as e:
                     UI.error(f"Inference Error: {e}")
                     return

            # --- Unified Post-Processing ---
            if stream_callback: stream_callback("\n")

            try:
                append_domain_log("backend", f"post_stream full_response_len={len(full_response)} tools={len(streaming_tools)}")
            except Exception:
                pass

            # 0. FALSE PROMISE DETECTION (Anti-Hallucination)
            # Check if model claimed to use a tool but didn't emit a tool call
            # Only check if we have content and NO tools
            if not streaming_tools and not tool_calls_detected and full_content.strip():
                if self._detect_false_tool_promise(full_content, tool_calls_detected):
                    self._false_promise_retries += 1
                    
                    if self._false_promise_retries > self._max_false_promise_retries:
                        UI.event("System", "Max false promise retries reached - skipping validation", style="error")
                        self._false_promise_retries = 0
                        # Proceed without blocking
                    else:
                        UI.event("System", f"False promise detected (attempt {self._false_promise_retries}) - forcing retry...", style="warning")
                        
                        # Add error to history to force correction
                        self.history.append({
                            "role": "assistant",
                            "content": full_content
                        })
                        self.history.append({
                            "role": "system", 
                            "content": (
                                "CORRECTION NEEDED: You mentioned using a tool (e.g. 'I am using...', 'Let me search...') "
                                "but you did NOT execute the tool call.\n"
                                "Please call the tool using proper function syntax now."
                            )
                        })
                        
                        # Force retry without user input
                        continue

            # Reset retry counter if tool calls were made or we passed the check
            self._false_promise_retries = 0

            # 1. Handle Tool Calls
            # ... (Tool logic unchanged) ...
            try:
                append_domain_log("backend", f"before_tool_loop streaming_tools={len(streaming_tools)}")
            except Exception:
                pass
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
                                        # Allow redundant call if user has provided a new message since the last call
                                        user_message_since_last_call = False
                                        # i is the index of the last tool message
                                        for msg_idx in range(i + 1, len(self.history)):
                                            if self.history[msg_idx].get('role') == 'user':
                                                user_message_since_last_call = True
                                                break
                                        
                                        if user_message_since_last_call:
                                            should_block = False
                                            UI.event("Info", f"Allowing repeated tool call '{tool_name}' due to user intervention.", style="dim")

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
                # Search full_response AND full_reasoning so tool_calls inside <think> are found
                # (e.g. when reasoning is streamed separately from content)
                text_to_search = (full_response + "\n" + (full_reasoning or "")).strip() or full_response
                xml_tools = re.findall(r'<tool_call>(.*?)</tool_call>', text_to_search, re.DOTALL)
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
                if not tool_calls_detected and "```json" in text_to_search:
                    try:
                        json_match = re.search(r'```json\s*(\[.*?\]|\{.*?\})\s*```', text_to_search, re.DOTALL)
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

                # 3. Text Pattern: "1. web_search(...)" or "Answer: web_search(...)"
                # This catches models (like VQ-1) that hallucinate text formats instead of JSON
                if not tool_calls_detected:
                    text_tools = re.findall(r'(?:^|\n)(?:Answer:)?\s*(?:\d+\.\s*)?([a-zA-Z0-9_]+)\s*\((.*?)\)', text_to_search)
                    for func_name, args_str in text_tools:
                        if func_name in self.tools:
                            # Parse arguments (simple quote extraction)
                            # web_search("query") -> {"query": "query"}
                            args = {}
                            
                            # Heuristic: try to map single string argument to first parameter
                            if args_str.strip().startswith('"') or args_str.strip().startswith("'"):
                                clean_arg = args_str.strip().strip('"\'')
                                # Get first parameter name from tool definition
                                tool_def = self.tools[func_name]
                                params = getattr(tool_def, 'parameters', {}).get('properties', {})
                                if params:
                                    first_param = list(params.keys())[0]
                                    args[first_param] = clean_arg
                            
                            # If parsing succeeded, add it
                            if args:
                                tool_calls_detected.append({
                                    "id": f"call_{os.urandom(4).hex()}",
                                    "type": "function",
                                    "function": {"name": func_name, "arguments": json.dumps(args)}
                                })

            try:
                append_domain_log("backend", f"after_regex_fallback tool_calls={len(tool_calls_detected)}")
            except Exception:
                pass
            if tool_calls_detected:
                content_for_history = full_content if full_content else "Thinking..."
                if self.use_server and not full_content:
                    content_for_history = None
                
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
                    
                    # Web UI Event: Tool Start
                    try:
                        from vaf.core.web_interface import get_web_interface
                        from vaf.core.subagent_ipc import get_current_session_id
                        # tc['id'] is available here 
                        get_web_interface().emit_tool_update('start', function_name, tc['id'], data=json.dumps(arguments), session_id=get_current_session_id())
                    except Exception: pass
                    
                    # Tool fillers are not spoken on host TTS (avoid announcing every tool use).
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
                        # API Spam Prevention: Wait 2s on error
                        time.sleep(2)
                        
                    # Web UI Event: Tool End
                    try:
                        from vaf.core.web_interface import get_web_interface
                        from vaf.core.subagent_ipc import get_current_session_id
                        r_str = str(result) if result else ""
                        is_err = "error" in r_str.lower() or "failed" in r_str.lower()
                        
                        # Use is_err to trigger delay if not already triggered by exception
                        # (e.g. tool returned "Error: ..." string without crashing)
                        if is_err and not isinstance(result, str): # Simple check, exact logic varies
                             # But simpler: just check if we haven't slept yet. 
                             # Actually, let's just make sure we slow down loops on ANY error status
                             pass 
                             
                        get_web_interface().emit_tool_update('error' if is_err else 'end', function_name, tc['id'], data=r_str, session_id=get_current_session_id())
                        
                        if is_err and "Error executing tool" not in r_str: # Avoid double sleep if exception already slept
                             time.sleep(2)
                    except Exception: pass

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
                        
                        if function_name == 'document_agent' and "Could not create document plan" in result_str:
                            self.history.append({
                                "role": "tool",
                                "tool_call_id": tc['id'],
                                "name": function_name,
                                "content": processed_result
                            })
                            self.history.append({
                                "role": "system",
                                "content": "[INFO] The document creation failed because the task was too vague. Ask the user for more details about the document they want to create (e.g., what sections, what content should be included, what is the purpose of the document)."
                            })
                        else:
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
                        
                        # Do not speak subagent/tool status via TTS (user requested no "librarian_agent" etc. read aloud)
                        
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
            # CRITICAL: We must detect whether we received an ANSWER for the user, not just thinking.
            # Some models output long reasoning (full_reasoning) but no final answer (full_content).
            # Empty = no meaningful final answer – we do NOT count reasoning as the answer.
            # Fix: First strip complete <think> blocks (content included), THEN strip other tags
            clean_final = re.sub(r'<think>.*?</think>', '', (full_content or ""), flags=re.DOTALL)
            clean_final = re.sub(r'<[^>]*>', '', clean_final)  # Remove remaining XML (e.g. <tool_call>)
            clean_final = re.sub(r'```[\s\S]*?```', '', clean_final)  # Remove code blocks
            clean_final = clean_final.replace(".", "").replace("\n", "").replace(":", "").strip()
            empty_patterns = ["answer", "antwort", "response", "here", "hier", "ok", "okay"]
            temp_final = clean_final.lower()
            for pattern in empty_patterns:
                temp_final = temp_final.replace(pattern, "")
            temp_final = temp_final.strip()
            # Has final answer = user-facing content (full_content only), not just thinking
            # Use >= 2 so short replies like "Hi" or "Ok" are accepted (CoT/first prompt)
            has_final_answer = len(temp_final) >= 2

            # CoT fallback: Some models (e.g. VQ1) send the whole reply in reasoning_content and
            # little or nothing in content. Treat substantial reasoning as a valid answer so we
            # don't trigger infinite "Empty response" retries.
            # UPDATE: Disabled this fallback because it prevents retrying when the model truly
            # forgets to answer (outputting only <think>).
            # if not has_final_answer and not tool_calls_detected and full_reasoning and len(full_reasoning.strip()) > 100:
            #     has_final_answer = True

            # Empty Response Handler: No answer for the user (thinking only counts as empty)
            # NO RETRY LIMITS - will loop until we get a response
            # SKIP if user stopped generation manually
            if _generation_stopped:
                UI.event("System", "Generation was stopped - skipping retry", style="info")
                # Add partial content to history if any
                if full_content.strip() or full_reasoning.strip():
                    self.history.append({
                        "role": "assistant",
                        "content": (full_content.strip() or "[Generation stopped by user]")
                    })
                return full_content.strip() or "[Generation stopped by user]"

            # Skip empty-response filter during compaction (every ~15 msgs); compaction reply is short (NO_REPLY / MEMORY:)
            try:
                append_domain_log("backend", f"empty_check has_final={has_final_answer} tools={len(tool_calls_detected)} content_len={len(full_content)} clean_len={len(temp_final)}")
            except Exception:
                pass
            if (not has_final_answer) and not tool_calls_detected and not getattr(self, "_compaction_in_progress", False):
                UI.event("System", "Empty response detected. Applying snapshot and retry...", style="warning")
                try:
                    append_domain_log("backend", f"empty_response_retry full_content_preview={full_content[:100] if full_content else 'NONE'}")
                except Exception:
                    pass
                # Ensure Web UI shows retry message and remove the faulty assistant bubble
                try:
                    from vaf.core.web_interface import get_web_interface
                    from vaf.core.subagent_ipc import get_current_session_id
                    session_id = get_current_session_id()
                    get_web_interface().log(
                        "Empty response detected. Applying snapshot and retry...",
                        level="warning",
                        source="System",
                        session_id=session_id,
                    )
                    get_web_interface().emit_clear_last_assistant(session_id)
                except Exception:
                    pass
                # Clear stream buffer so the retry sends only new content (no old + new)
                if stream_callback and hasattr(stream_callback, "clear"):
                    try:
                        stream_callback.clear()
                    except Exception:
                        pass

                # First empty only: keep one assistant block (with thinking) and nudge; no temp sweep.
                if empty_retry_count == 0:
                    content = full_response if full_response else ((full_reasoning or "") + "\n\n" + (full_content or "")).strip()
                    if not content:
                        content = "[Empty]"
                    self.history = self.history[:history_snapshot_len + 1] + [
                        {"role": "assistant", "content": content}
                    ]
                    self.history.append({
                        "role": "system",
                        "content": (
                            "You only provided thinking but no final answer for the user. "
                            "Provide a clear, direct answer now (or call the necessary tools)."
                        )
                    })
                    empty_retry_count += 1
                    time.sleep(1)
                    continue

                # Second and later empties: existing logic (drop thinking, temp sweep, emergency clear)
                # Check for tool results that occurred during this turn (after original snapshot)
                # If we executed tools but got no final answer, we must PRESERVE the tools!
                # Otherwise we loop forever: Call Tool -> Empty Ans -> Reset -> Call Tool -> ...
                
                # Identify messages added since original snapshot
                current_len = len(self.history)
                
                # We need to construct a new history list that preserves critical context
                # Start with the snapshot (System + User Prompt)
                new_history = self.history[:history_snapshot_len + 1]
                
                # Scan the messages added since snapshot
                tools_preserved = 0
                
                for i in range(history_snapshot_len + 1, current_len):
                    msg = self.history[i]
                    role = msg.get('role', '')
                    content = str(msg.get('content', ''))
                    
                    # 1. Keep Tool Calls (Assistant Action)
                    # Necessary so the following 'tool' message makes sense
                    if role == 'assistant' and msg.get('tool_calls'):
                        new_history.append(msg)
                        continue
                        
                    # 2. Keep Tool Results (Persistent Memory)
                    if role == 'tool':
                        # Truncate extremely huge outputs to save tokens, but keep generous amount
                        # (2500 chars is enough for file lists, search snippets, etc.)
                        if len(content) > 2500:
                            truncated = content[:2500] + f"\n... [truncated for snapshot, original len: {len(content)}]"
                            msg_clone = msg.copy()
                            msg_clone["content"] = truncated
                            new_history.append(msg_clone)
                        else:
                            new_history.append(msg)
                        tools_preserved += 1
                        continue
                        
                    # 3. Drop "Thinking" (Text-only Assistant Messages)
                    # These led to the empty response loop. We remove them to force a re-think.
                    pass
                
                if tools_preserved > 0:
                    UI.event("Snapshot", f"Advanced snapshot: Preserved {tools_preserved} tool messages", style="success")
                    # Update history with the filtered list
                    self.history = new_history
                    # Advance snapshot pointer so we don't re-process these valid tools
                    history_snapshot_len = len(self.history) - 1
                else:
                    # No tools to preserve, standard reset
                    self.history = self.history[:history_snapshot_len + 1]
                    UI.event("Debug", f"Reset to user prompt snapshot (preserving query)", style="dim")

                # ═══════════════════════════════════════════════════════════════
                # PROACTIVE CONTEXT CLEARING (aggressive clear a few attempts before hard limit)
                # ═══════════════════════════════════════════════════════════════
                if empty_retry_count == 8:
                    # Calculate tokens before
                    tokens_before, _ = self.get_token_usage()
                    
                    UI.event("System", f"Early Warning ({empty_retry_count}) - Aggressive Context Clearing...", style="dim")
                    
                    # Preservation Strategy:
                    # Keep System Prompt + Snapshot (User Prompt)
                    if len(self.history) > history_snapshot_len + 2:
                        kept_history = self.history[:history_snapshot_len + 1]
                        self.history = kept_history
                        
                        # Calculate tokens after (estimate)
                        tokens_after, _ = self.get_token_usage()
                        UI.event("Context", f"Cleared: {tokens_before} -> {tokens_after} Tokens | Snapshot preserved", style="dim")
                    else:
                         UI.event("Context", "History already minimal - skipping clear.", style="dim")

                # ═══════════════════════════════════════════════════════════════
                # CONTEXT OVERFLOW DETECTION (Fix for Issue #VAF-CTX-001)
                # ═══════════════════════════════════════════════════════════════
                MAX_RETRIES_BEFORE_EMERGENCY = 7   # Emergency context clear before hard stop
                HARD_LIMIT = 10                     # Stop after 10 empty-response retries

                # Wait 1 second before retry to avoid hammering the model (use global time module)
                time.sleep(1)

                if empty_retry_count == MAX_RETRIES_BEFORE_EMERGENCY:
                    UI.event("System", f"High retry count ({empty_retry_count}) - Triggering Emergency Context Clearing", style="bold yellow")
                    self.manage_context()

                elif empty_retry_count >= HARD_LIMIT:
                    UI.event("Emergency", f"Model not responding after {empty_retry_count} retries - stopping", style="warning")
                    emergency_summary = "⚠️ **Model Not Responding**\n\nThe model failed to generate a response after multiple attempts. This may be due to:\n- Model overload\n- Context issues\n- Network problems\n\nPlease try again or start a new session."
                    if stream_callback:
                        stream_callback(emergency_summary)
                    return emergency_summary

                # Reset to user prompt snapshot
                self.history = self.history[:history_snapshot_len + 1]
                UI.event("Debug", f"Reset to user prompt snapshot (preserving query)", style="dim")
                
                # Add a brief system prompt to nudge the model
                self.history.append({
                    "role": "system",
                    "content": "You didn't provide a final answer. Please provide a clear response or call the necessary tools."
                })
                
                # Dynamic Temperature Sweep to break loops
                empty_retry_count += 1
                delta = ((empty_retry_count + 1) // 2) * 0.1
                direction = -1 if empty_retry_count % 2 == 1 else 1
                current_temp = target_temp + (delta * direction)
                current_temp = max(0.1, min(0.9, current_temp))
                
                UI.event("Adaptive", f"Tuning creativity: {current_temp:.1f} (attempt {empty_retry_count})", style="info")

                # API guard: avoid infinite silent retries in WebUI
                if self.api_backend and empty_retry_count >= 3:
                    fallback_msg = "[Error] API returned empty responses repeatedly. Please try again."
                    if stream_callback:
                        stream_callback(fallback_msg)
                    return fallback_msg
                
                continue

            
            # Clean History: Store ONLY the final content (Answer), discarding the reasoning trace.
            history_content = full_content if full_content else full_response
            
            # 🛡️ FINAL ANSWER VALIDATION (Intent Lock)
            # Before accepting the answer, check if it's just meta-talk/status drift
            try:
                append_domain_log("backend", f"before_validation tools={len(tool_calls_detected)} auto_retry={auto_retry}")
            except Exception:
                pass
            if not tool_calls_detected and not auto_retry:
                user_intent = ""
                if hasattr(self, 'main_persistence') and self.main_persistence:
                    user_intent = self.main_persistence.get_user_intent()
                elif user_input:
                    user_intent = user_input

                try:
                    append_domain_log("backend", f"calling_validate intent_len={len(user_intent)}")
                except Exception:
                    pass
                if not self._validate_final_answer(history_content, user_intent):
                    UI.event("Validation", "Meta-Response detected. Forcing content-focused answer...", style="warning")
                    self.history.append({
                        "role": "system", 
                        "content": (
                            f"🛑 **STOP!** Your answer is a Meta-Response (Status report).\n"
                            f"The user doesn't want to know that the sub-agent is finished. "
                            f"The user wants an actual answer to: \"{user_intent}\"\n"
                            f"Please provide the ACTUAL information/analysis now."
                        )
                    })
                    # Reset counters and continue to force a real answer
                    empty_retry_count += 1
                    continue

            self.history.append({"role": "assistant", "content": history_content})
            try:
                append_domain_log("backend", f"after_history_append content_len={len(history_content)}")
            except Exception:
                pass

            # NOTE: Language mismatch auto-retry is currently disabled.
            # To re-enable, uncomment the block below.
            #
            # Check for language mismatch: Did the model respond in a different language than the user?
            # This helps catch cases where LANGUAGE_HINT was ignored (e.g., user asks in Turkish, model responds in English)
            # CRITICAL: Do NOT check if tools were called (tool syntax "web_search" looks English but is valid)
            # if not skip_input and user_input and history_content and not tool_calls_detected:
            #     if self._check_language_mismatch(user_input, history_content):
            #         # Mismatch detected! The warning is already in history (as system msg).
            #         # Now we must REMOVE the bad response and RETRY immediately.
            #
            #         # 1. Remove the bad response, keep the system warning for the retry.
            #         # The history is [..., assistant_response, system_warning], so we remove the item at -2.
            #         del self.history[-2]
            #
            #         # 2. Treat as a retry (reuses logic for patience/backoff if needed)
            #         empty_retry_count += 1
            #
            #         mismatch = getattr(self, "_last_language_mismatch", None) or {}
            #         if mismatch:
            #             UI.event(
            #                 "System",
            #                 "Triggering retry for language."
            #                 f" User={mismatch.get('user', 'unknown')},"
            #                 f" Response={mismatch.get('response', 'unknown')},"
            #                 f" Retry={mismatch.get('target', 'unknown')}",
            #                 style="warning",
            #             )
            #         else:
            #             UI.event("System", "Triggering immediate retry for language correction...", style="warning")
            #
            #         # 3. Restart loop - model will see the system warning and try again
            #         continue
            
            # Context Management: Check after adding assistant response
            # Long reasoning phases can push us over the limit
            try:
                append_domain_log("backend", "before_manage_context")
            except Exception:
                pass
            self.manage_context()
            try:
                append_domain_log("backend", "after_manage_context")
            except Exception:
                pass
            
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

            try:
                append_domain_log("backend", "chat_step_break_reached")
            except Exception:
                pass
            break

        # Emergency Fallback: If we exhausted retries and STILL have no answer
        if not clean_content:
             # Check if we have a tool result immediately preceding this "silent" thought block
             last_msg = self.history[-1] if self.history else {}
             prev_msg = self.history[-2] if len(self.history) > 1 else {}
             
             # Case 1: Loop ended with Tool Output -> Model Silent
             if last_msg.get('role') == 'tool':
                 res = f"✅ Tool '{last_msg.get('name')}' finished: {last_msg.get('content')[:100]}..."
                 # Do not speak tool-status via TTS (user requested no "tool X / model provided no commentary")
                 return res
                 
             # Case 2: Loop ended with Assistant Thought -> Model Silent (Previous was tool)
             if last_msg.get('role') == 'assistant' and prev_msg.get('role') == 'tool':
                  res = f"✅ Tool '{prev_msg.get('name')}' finished. (Model provided no commentary)"
                  # Do not speak tool-status via TTS (user requested no "tool X / model provided no commentary")
                  return res

             # No TTS for generic fallback either (avoid "...")
             return "..."
        
        # Final empty check - same logic as above
        clean_final = re.sub(r'<[^>]*>', '', full_response)
        clean_final = re.sub(r'```[\s\S]*?```', '', clean_final)
        clean_final = clean_final.replace(".", "").replace("\n", "").replace(":", "").strip()
        temp_final = clean_final.lower()
        for pattern in ["answer", "antwort", "response", "here", "hier", "ok", "okay"]:
            temp_final = temp_final.replace(pattern, "")
        is_final_empty = len(temp_final.strip()) < 3

        # Skip final empty check during compaction (short NO_REPLY/MEMORY: replies are expected)
        if is_final_empty and not tool_calls_detected and not getattr(self, "_compaction_in_progress", False):
             
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
        
        try:
            append_domain_log("backend", "before_speak")
        except Exception:
            pass
        self._speak(tts_source)
        try:
            append_domain_log("backend", "after_speak")
        except Exception:
            pass

        try:
            append_domain_log("backend", f"chat_step_complete response_len={len(full_response)}")
        except Exception:
            pass

        # Return CLEANED response for the UI (Answer Box)
        # The raw response is already stored in history, so we don't lose information.
        return self._clean_reasoning(full_response)

    def execute_tool(self, name, args):
        from vaf.cli.ui import UI
        from pathlib import Path
        from vaf.core.trust import should_gate_tool, get_tool_policy, set_tool_policy, mark_trusted_dir, is_trusted_dir, explain_gate

        # So tools (e.g. document_writer) can notify Web UI; needed when run directly or via workflow in same process
        try:
            from vaf.core.subagent_ipc import get_current_session_id
            sid = get_current_session_id() or getattr(self, "current_session_id", None)
            if sid:
                os.environ["VAF_SESSION_ID"] = str(sid)
        except Exception:
            pass

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

        def normalize_tool_name(raw_name: str | None) -> str | None:
            if not raw_name:
                return None
            cleaned = raw_name.strip()
            if cleaned.startswith("functions."):
                cleaned = cleaned[len("functions."):]
            return cleaned or None

        def run_multi_tool_use(call_args: dict | None) -> str:
            tool_uses = (call_args or {}).get("tool_uses", [])
            if not tool_uses:
                return "Error: No tool_uses provided."

            results = []
            for item in tool_uses:
                if not isinstance(item, dict):
                    results.append({"tool": "?", "success": False, "result": "Invalid tool entry (not a dict)."})
                    continue

                raw_tool_name = item.get("recipient_name") or item.get("tool") or item.get("name")
                tool_name = normalize_tool_name(raw_tool_name)
                if not tool_name or tool_name == "multi_tool_use.parallel":
                    results.append({"tool": raw_tool_name or "?", "success": False, "result": "Invalid tool name."})
                    continue

                tool_args = item.get("parameters") or item.get("args") or {}
                if isinstance(tool_args, str):
                    try:
                        tool_args = json.loads(tool_args)
                    except Exception:
                        tool_args = {}

                # Run sequentially to preserve tool gating/UI prompts.
                result = self.execute_tool(tool_name, tool_args)
                is_err = isinstance(result, str) and result.lower().startswith(("error", "tool error"))
                results.append({"tool": tool_name, "success": not is_err, "result": result})

            output = ["==== MULTI TOOL RESULTS ====", ""]
            for i, res in enumerate(results, 1):
                status = "OK" if res.get("success") else "ERR"
                output.append(f"[{i}] {status} {res.get('tool')}")
                result_text = str(res.get("result", ""))
                if len(result_text) > 200:
                    result_text = result_text[:200] + "..."
                output.append(f"    {result_text}")
                output.append("")

            return "\n".join(output).strip()

        if name == "multi_tool_use.parallel":
            emit({"type": "tool_start", "tool": name, "args": make_json_serializable(args or {})})
            result = run_multi_tool_use(args if isinstance(args, dict) else {})
            emit({"type": "tool_end", "tool": name})
            return result

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
                tool_args = dict(args) if args else {}
                if name in ("memory_save", "memory_search"):
                    scope_id = getattr(self, "_current_user_scope_id", None)
                    tool_args["user_scope_id"] = scope_id
                    # Debug: Log user scope for RAG troubleshooting (consolidated in rag.log)
                    append_domain_log("rag", f"[Agent] {name} called with user_scope_id={scope_id}")
                if name == "update_user_identity":
                    tool_args["username"] = getattr(self, "_current_username", None) or "admin"
                if name in ("send_telegram", "send_discord", "send_slack"):
                    tool_args["username"] = getattr(self, "_current_username", None) or "admin"
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                if name in ("mail_inbox", "read_mail", "find_mail", "mark_mail_answered"):
                    tool_args["username"] = getattr(self, "_current_username", None) or "admin"
                result = self.tools[name].run(**tool_args)
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
                    self._record_tool_used("python_exec")
                    result = unsafe_result

        emit({"type": "tool_end", "tool": name})
        self._record_tool_used(name)
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
            # Same priority as WebSearchTool: Brave API -> Google CSE -> scrape Google -> DuckDuckGo
            results, search_source, _ = get_web_search_results(query, 5)
            if not results:
                return "No results found."
            summary = f"### Web Search Results (Deep Research – {search_source})\n"
            
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

    def _strip_tool_calls_text(self, response_text: str) -> str:
        """
        Remove raw tool call JSON blocks from the assistant text response.
        This prevents API tool call payloads from appearing in user-visible output.
        """
        if not response_text:
            return response_text

        import json
        import re

        cleaned = response_text

        # Strip fenced JSON blocks that contain tool_calls
        cleaned = re.sub(
            r"```json\s*\{[^`]*\"tool_calls\"[^`]*\}\s*```",
            "",
            cleaned,
            flags=re.DOTALL,
        )

        # Strip leading raw JSON object that contains tool_calls
        stripped = cleaned.lstrip()
        if stripped.startswith("{") and "\"tool_calls\"" in stripped[:2000]:
            json_obj, end_idx = self._extract_json_object(stripped)
            if json_obj:
                try:
                    data = json.loads(json_obj)
                    if isinstance(data, dict) and "tool_calls" in data:
                        cleaned = stripped[end_idx:].lstrip()
                except Exception:
                    pass

        return cleaned.strip()

    def _extract_json_object(self, text: str) -> tuple[str, int]:
        """
        Extract the first JSON object from text starting at '{'.
        Returns (json_text, end_index) or ("", -1) if not found.
        """
        if not text or not text.startswith("{"):
            return "", -1

        depth = 0
        in_string = False
        escape = False

        for idx, ch in enumerate(text):
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
                continue

            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[: idx + 1], idx + 1

        return "", -1

    def _detect_false_tool_promise(self, response_text: str, tool_calls: list) -> bool:
        """
        Intelligently detects if the model claimed to use a tool but didn't execute it.
        Uses a hybrid approach: Structural Analysis (Fast) -> LLM Validator (Accurate).
        
        Args:
            response_text: The model's text response.
            tool_calls: List of tool calls extracted (if any).
            
        Returns:
            True if a false promise is detected (model lied about using tool).
        """
        # 1. Fast Path: If tools were actually called, it's valid.
        if tool_calls and len(tool_calls) > 0:
            return False
            
        # 2. Structural Analysis (Heuristics)
        # -----------------------------------
        # Normalize text
        text = response_text.strip()
        
        # Skip only very short responses (keep long ones as they might contain false promises)
        if len(text) < 10:
            return False

        suspicion_score = 0.0
        import re
        
        # Indicator A: Mentions known tool names in code format (e.g. `read_file`)
        tool_names = list(self.tools.keys())
        formatted_tool_mentions = 0
        for tool in tool_names:
            if f"`{tool}`" in text or f"'{tool}'" in text or f'"{tool}"' in text:
                formatted_tool_mentions += 1
        
        if formatted_tool_mentions > 0:
            suspicion_score += 0.35
            
        # Indicator B: Waiting indicators (Ellipses, "wait", "moment")
        if re.search(r'\.{3,}|…|⏳|⌛|🔄', text):
            suspicion_score += 0.35
            
        # Indicator C: Ends with action-like statement (before punctuation)
        if re.search(r'(:|…|\.{3,})\s*$', text):
            suspicion_score += 0.2
            
        # Indicator D: Action keywords (Multilingual)
        # We check for a few common ones, but rely mostly on structure
        action_keywords = [
            "read", "lesen", "search", "suche", "execute", "führe aus",
            "using", "nutze", "verwende", "opening", "öffne"
        ]
        if any(kw in text.lower() for kw in action_keywords):
            suspicion_score += 0.1

        # 3. Decision Logic
        # -----------------
        # Low suspicion: Trust the model (it's just talking)
        if suspicion_score < 0.4:
            return False
            
        # High suspicion: Verify with LLM (LLM-as-Judge)
        # This prevents false positives where model explains "You can use `read_file`..."
        try:
            # Construct a fast validation prompt
            validator_prompt = (
                f"Analyze this AI response. Did the AI CLAIM to use a tool right now, but didn't execute it?\n"
                f"Response: \"{text}\"\n"
                f"Tools Executed: None\n\n"
                f"Rules:\n"
                f"- FALSE_PROMISE: \"I am using `read_file`...\", \"Let me search...\", \"I'll execute this...\"\n"
                f"- SAFE: \"You can use `read_file`\", \"I recommend `web_search`\", \"The tool is for...\"\n\n"
                f"Answer ONLY 'FALSE_PROMISE' or 'SAFE'."
            )
            
            # Fast inference (low tokens, deterministic)
            messages = [{"role": "user", "content": validator_prompt}]
            result = ""
            
            if self.use_server: # Local Server
                payload = {"messages": messages, "max_tokens": 5, "temperature": 0.0}
                res = requests.post("http://127.0.0.1:8080/v1/chat/completions", json=payload, timeout=120).json()
                result = res['choices'][0]['message']['content']
            elif self.api_backend: # API
                response_chunks = list(self.api_backend.chat_completion(messages, max_tokens=5, temperature=0.0, stream=False))
                result = "".join(str(c) for c in response_chunks)
            elif self.llm: # Local Library
                output = self.llm.create_chat_completion(messages=messages, max_tokens=5, temperature=0.0)
                result = output['choices'][0]['message']['content']
                
            return "FALSE_PROMISE" in result.upper()
            
        except Exception:
            # If validation fails, fallback to heuristic (conservative)
            return suspicion_score > 0.7

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

        # Use active tools if available, otherwise all tools
        tools_to_use = self._active_tools if self._active_tools is not None else self.tools.keys()
        excluded = getattr(self, "_excluded_tools", None) or set()

        for name in tools_to_use:
            if name not in self.tools or name in excluded:
                continue
            tool = self.tools[name]
            
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
