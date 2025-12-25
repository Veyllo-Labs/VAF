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
        # If it's a path or full huggingface ID, extract filename or use as is
        if "/" in model_name: 
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

        # Initialize Tools (Dynamic Loading)
        self.tools = {}
        self._load_tools()
        
        # Register Cleanup Handler (Cross-Platform)
        atexit.register(self.shutdown)
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
        
        # Register atexit handler as final backup
        atexit.register(self._atexit_cleanup)
    
    def _atexit_cleanup(self):
        """Called by atexit - kill server if still running."""
        if self.server and self.use_server:
            persist = self.config.get("persist_server", False)
            if not persist:
                try:
                    self.server.stop_server()
                except:
                    pass

    def shutdown(self, signum=None, frame=None):
        """Cleanup resources on exit - works for both signal handlers and manual calls."""
        if self.server and self.use_server:
            # Check config preference
            persist = self.config.get("persist_server", False)
            if not persist:
                try:
                    print(f"\n[VAF] Stopping server (Signal: {signum or 'Exit'})...")
                    self.server.stop_server()
                    self.use_server = False
                except Exception as e:
                    # Force kill if graceful stop failed
                    try:
                        if self.server: 
                            self.server.stop_server()
                    except:
                        pass
                
                # If triggered by signal, force exit
                if signum:
                    import os
                    os._exit(0)  # Force exit - sys.exit might not work in signal handlers
            else:
                if signum:
                    print(f"\n[VAF] Server process left running (persist_server=True).")
                    sys.exit(0)

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
                            
                        self.tools[instance.name] = instance
                        # Debug info (only if verbose)
                        # print(f"Loaded tool: {instance.name}")
            except Exception as e:
                pass # Silently ignore broken plugins for stability

    def load_model(self, skip_download_check: bool = False):
        from vaf.cli.ui import UI
        if not skip_download_check:
            self.ensure_model_exists()
        
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
                UI.event("Info", "Using HTTP Backend", style="dim")
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

## YOUR TOOLS
"""
        # Dynamic Tool List
        if self.tools:
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

    def _check_language_mismatch(self, user_input: str, assistant_response: str) -> None:
        """
        Check if the assistant responded in a different language than the user.
        If mismatch detected, add a warning to history prompting the model to translate/reformulate.
        
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
        
        # Generate warning message in the user's language (if we can) or bilingual
        if user_lang == "de":
            warning = (
                f"⚠️ **Sprach-Mismatch erkannt**: Du hast auf {response_lang_name} geantwortet, "
                f"aber der Nutzer spricht {user_lang_name}. "
                f"Bitte übersetze deine Antwort ins {user_lang_name} oder formuliere sie auf {user_lang_name} um."
            )
        elif user_lang in language_names:
            # Try to generate a warning in the user's language (simple approach)
            warning = (
                f"⚠️ **Language mismatch detected**: You responded in {response_lang_name}, "
                f"but the user is speaking {user_lang_name}. "
                f"Please translate your response to {user_lang_name} or reformulate it in {user_lang_name}."
            )
        else:
            # Fallback: bilingual
            warning = (
                f"⚠️ **Language mismatch**: You answered in {response_lang_name}, "
                f"but user speaks {user_lang_name}. "
                f"Please respond in {user_lang_name}."
            )
        
        # Add warning to history so model sees it and can correct on next turn
        self.history.append({
            "role": "system",
            "content": warning
        })
        
        from vaf.cli.ui import UI
        UI.event("Language", f"Mismatch: User={user_lang_name}, Response={response_lang_name}", style="warning")

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

    def manage_context(self):
        """
        Cursor-Style Context Management
        
        Features:
        - Intent Context: Tracks user goals
        - State Context: Tracks project state (files, errors)
        - Full Archive: Complete history saved for restoration
        - Smart Compression: Lossy but preserves critical info
        
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
        
        # Compress with Cursor-style algorithm
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
            import re
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
            import re
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
        
        try:
            from vaf.workflows import WorkflowSelector, WorkflowEngine, create_workflow
            
            # Check if workflows are enabled (can be disabled in config)
            if not self.config.get("workflows_enabled", True):
                return None
            
            # BRAIN-BASED WORKFLOW SELECTION (multi-language support!)
            # Instead of hardcoded pattern matching, use LLM to understand intent in ANY language
            from vaf.cli.ui import UI as UI_Class
            with UI_Class.console.status("[bold cyan]🧠 Brain: Analyzing workflow match...[/bold cyan]", spinner="dots"):
                workflow_id = self.analyze_workflow(user_input)
            
            if not workflow_id:
                # No workflow match - fall back to LLM agent
                UI.event("Brain", "No workflow match", style="dim")
                return None
            
            UI.event("Brain", f"Workflow matched: {workflow_id}", style="bold cyan")
            
            # Get the matched template
            from vaf.workflows.templates import get_template
            template = get_template(workflow_id)
            if not template:
                return None
            
            UI.event("Workflow", f"Brain matched: {template['name']} (multi-language support!)", style="bold cyan")
            
            # Extract variables using selector (still useful for variable extraction)
            selector = WorkflowSelector()
            result = selector.select(user_input)
            
            # Use selector's variable extraction even if pattern matching didn't work
            # (Brain found the workflow, but selector can still extract variables)
            variables = result.variables if result.matched else {}
            
            # Get required variables from template (in case selector didn't match)
            template_variables = template.get("variables", {})
            required_vars = set(template_variables.keys())
            
            # Determine which variables are missing
            missing = [var for var in required_vars if var not in variables]
            
            # Debug: Log extracted variables
            if variables:
                from vaf.cli.ui import UI
                UI.event("Debug", f"Extracted variables: {list(variables.keys())}", style="dim")
            
            # If variables are missing, try to extract them from the input using improved extraction
            if missing:
                from vaf.cli.ui import UI
                UI.event("Debug", f"Missing variables: {missing}", style="dim")
                # Use selector's improved _extract_value method for better extraction
                missing_copy = list(missing)  # Use copy to avoid modification during iteration
                for var_name in missing_copy:
                    extracted = selector._extract_value(user_input, var_name, template_variables.get(var_name, ""))
                    if extracted:
                        variables[var_name] = extracted
                        missing.remove(var_name)
                    else:
                        # For description/query/topic/task_description variables, use cleaned input as fallback
                        if var_name in ("description", "query", "topic", "task_description"):
                            # Try to extract a cleaned version first
                            cleaned = selector._extract_value(user_input, var_name, template_variables.get(var_name, ""))
                            if cleaned and len(cleaned) > 5:
                                variables[var_name] = cleaned
                            else:
                                # Last resort: use full input but clean it
                                # Remove time patterns, frequency words, etc.
                                import re
                                cleaned_input = user_input
                                # Remove time patterns (HH:MM format)
                                cleaned_input = re.sub(r'\b\d{1,2}:\d{2}\b', '', cleaned_input)
                                # Remove frequency words (works in any language)
                                frequency_words = ["immer", "täglich", "daily", "always", "every day", "um", "at"]
                                for word in frequency_words:
                                    cleaned_input = re.sub(rf'\b{word}\b', '', cleaned_input, flags=re.IGNORECASE)
                                # Remove format mentions
                                format_words = ["html", "markdown", "txt", "text"]
                                for word in format_words:
                                    cleaned_input = re.sub(rf'\b{word}\b', '', cleaned_input, flags=re.IGNORECASE)
                                # Remove path mentions
                                path_words = ["desktop", "documents", "downloads", "on my", "to my"]
                                for word in path_words:
                                    cleaned_input = re.sub(rf'\b{word}\b', '', cleaned_input, flags=re.IGNORECASE)
                                # Clean up whitespace
                                cleaned_input = re.sub(r'\s+', ' ', cleaned_input).strip()
                                if cleaned_input:
                                    variables[var_name] = cleaned_input
                                else:
                                    variables[var_name] = user_input
                            missing.remove(var_name)
                        else:
                            UI.event("Workflow", f"Missing input: {var_name}", style="warning")
                
                # If still missing critical variables, fall back to LLM
                if missing:
                    UI.event("Workflow", f"Missing inputs: {', '.join(missing)} - falling back to LLM", style="warning")
                return None
            
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
                    if not noninteractive and bool(Config.get("ux_auto_open_outputs", False)):
                        out_file = str(workflow_result.outputs.get("output_file") or "")
                        if out_file:
                            p = Path(out_file)
                            # Open HTML reports in browser, otherwise open folder/file in explorer
                            if p.suffix.lower() in (".html", ".htm") and p.exists():
                                Platform.open_url(p.as_uri())
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
                    import re
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
                                import re
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
                        if not noninteractive and bool(Config.get("ux_auto_open_outputs", False)):
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
                            return f"**✓ Fertig!** Report gespeichert:\n{target}"
                        return f"**✓ Done!** Report saved:\n{target}"
                    except Exception:
                        pass
                
                # Build completion message based on language and completion status
                if incomplete_tasks_hint:
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
             with UI_Class.console.status("[bold magenta](O_O) Analyzing Intent...[/bold magenta]", spinner="dots"):
                 dynamic_temp = self.analyze_intent(user_input)
             
             UI.event("Brain", f"Adaptive State: Temperature set to {dynamic_temp} based on intent.", style="dim")
             target_temp = dynamic_temp

        UI.event("Agent", "Thinking...", style="dim")
        
        retries = 0
        MAX_RETRIES = 5
        
        # State for formatting
        is_reasoning = False
        
        while True:
            full_response = ""     # For history (legacy/combined)
            full_content = ""      # For empty check
            full_reasoning = ""    # For empty check
            
            streaming_tools = {}
            tool_calls_detected = []
            
            if self.use_server:
                # Proactive Context Management: Compress before request to prevent overflow
                self.manage_context()
                
                # Retry loop for 503 (Model Loading) and 500 (Context Overflow)
                response = None
                for _ in range(15): # Try for ~30 seconds
                    try:
                        # CRITICAL: Rebuild payload with current history (may have been compressed)
                        payload = {
                             "messages": self.history,
                             "tools": self.TOOLS, 
                             "tool_choice": "auto",
                             "stream": True,
                             "temperature": target_temp,
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
                        
                        # CRITICAL: Check if we're retrying a tool that just failed
                        if len(self.history) >= 2:
                            last_tool = self.history[-2]
                            if last_tool.get('role') == 'tool' and last_tool.get('name') == tool_name:
                                tool_result = str(last_tool.get('content', '')).lower()
                                # Check if last tool call failed
                                # Don't treat user-friendly error messages (with ❌) as tool execution errors
                                # These are informational messages that should be shown to the user
                                is_error = (
                                    "error executing tool" in tool_result or
                                    ("error:" in tool_result and not "❌" in tool_result) or  # Allow ❌ errors (user-friendly)
                                    "server returned" in tool_result and ("400" in tool_result or "500" in tool_result or "404" in tool_result) or
                                    "failed" in tool_result and ("tool" in tool_result or "execution" in tool_result) or
                                    (tool_result.startswith("error") and not "❌" in tool_result)
                                )
                                
                                if is_error:
                                    # Block retry - add error message and skip tool call
                                    UI.event("Warning", f"Blocked retry of failed tool: {tool_name}", style="warning")
                                    self.history.append({
                                        "role": "system",
                                        "content": (
                                            f"⚠️ STOP! You tried to call '{tool_name}' again after it failed.\n"
                                            f"The tool returned an error: {last_tool.get('content', '')}\n"
                                            f"DO NOT retry failed tools. You MUST inform the user about the error instead."
                                        )
                                    })
                                    # Don't add this tool call - force model to respond with error message
                                    continue
                        
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
                    
                    # Show spinner while tool works
                    from vaf.cli.ui import UI as UI_Class
                    result = None
                    try:
                        # Special Case: Coding Agent has its own immersive UI
                        if function_name == "coding_agent":
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
                
                UI.event("Debug", "Tool finished. Sending result to model...", style="dim")
                continue
            
            # 2. Handle Empty / Think-Only Responses
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
            
            # Consider empty if: no content OR only filler words (< 3 real chars after cleaning)
            is_effectively_empty = len(temp_content) < 3
            
            # Case A: Totally Empty (Server failure or silent stop)
            if (not full_response or is_effectively_empty) and retries < MAX_RETRIES:
                 # Silent retry for empty response
                 self.history.append({"role": "system", "content": "You generated an empty or near-empty response (just 'Answer:' or similar). Please process the user request and generate a valid Tool Call or provide a REAL, SUBSTANTIVE answer."})
                 retries += 1
                 continue

            # Case B: Think Only (No answer)
            # If we have reasoning but NO content (or empty-ish content)
            if full_reasoning and is_effectively_empty and retries < MAX_RETRIES:
                # We append the thought process so proper context is maintained
                self.history.append({"role": "assistant", "content": full_response})
                # Graduated Retry Strategy
                
                # Check if we just finished a tool call
                last_was_tool = len(self.history) >= 2 and self.history[-2].get('role') == 'tool'
                
                if last_was_tool:
                     tool_name = self.history[-2].get('name', '')
                     tool_result = self.history[-2].get('content', '')
                     tool_result_str = str(tool_result).lower()
                     
                     # Check if tool returned an error (multiple error patterns)
                     is_error = (
                         "error executing tool" in tool_result_str or
                         "error:" in tool_result_str or
                         "server returned" in tool_result_str and ("400" in tool_result_str or "500" in tool_result_str or "404" in tool_result_str) or
                         "failed" in tool_result_str and ("tool" in tool_result_str or "execution" in tool_result_str) or
                         tool_result_str.startswith("error")
                     )
                     
                     # CRITICAL: Check librarian_agent FIRST (before generic error handling)
                     # This ensures the specific prompt is always used, even on errors
                     if tool_name == "librarian_agent":
                         # ALWAYS use specific prompt for librarian_agent (even on errors)
                         # This ensures the model gives a final answer instead of just thinking
                         if is_error:
                             prompt = (
                                 f"⚠️ The librarian_agent FAILED: {tool_result}\n\n"
                                 f"STOP thinking. You MUST immediately inform the user about this error.\n"
                                 f"Tell them: 'The librarian tool failed with error: [error details]'\n"
                                 f"Do not explain what you tried. Just report the error directly."
                             )
                         else:
                             # Success case: Get the answer from tool result
                             prompt = "The librarian agent has finished. You have the answer in the tool result. STOP thinking and output the FINAL ANSWER directly to the user now. Do not explain the process, just give the answer."
                     elif is_error:
                         # CRITICAL: Prevent retrying the same tool - force error message to user
                         prompt = (
                             f"⚠️ CRITICAL: The tool '{tool_name}' FAILED with error: {tool_result}\n\n"
                             f"DO NOT call '{tool_name}' again. DO NOT retry. DO NOT think about alternatives.\n"
                             f"You MUST immediately inform the user about this error in plain language.\n"
                             f"Tell them what went wrong and that the tool failed. Do not generate more thoughts."
                         )
                     elif tool_name == "write_file":
                         prompt = "File written successfully. Analyze the result and inform the user."
                     elif tool_name == "web_search":
                         prompt = (
                             "🚨 CRITICAL: Web search is COMPLETE. You have ALL the search results.\n\n"
                             "STOP thinking. STOP analyzing. STOP planning.\n"
                             "DO NOT call web_search again. DO NOT call any other tools.\n"
                             "You MUST output the FINAL ANSWER to the user RIGHT NOW.\n\n"
                             "Extract the key information from the search results and give a direct, concise answer.\n"
                             "Do NOT explain your process. Do NOT mention the tool. Just give the answer."
                         )
                     elif tool_name == "coding_agent":
                         # Extract original user task from history (look for the most recent user message that triggered coding_agent)
                         original_task = None
                         for msg in reversed(self.history):
                             if msg.get("role") == "user" and "coding_agent" not in str(msg.get("content", "")).lower():
                                 original_task = msg.get("content", "")
                                 break
                         
                         # Check if coding agent result indicates incomplete work or questions
                         tool_result_str = str(tool_result).lower()
                         
                         # Check for incomplete tasks (e.g., "Tasks: 0/5", "Tasks: 2/5")
                         task_match = re.search(r'tasks?\s*:\s*(\d+)/(\d+)', tool_result_str, re.IGNORECASE)
                         has_incomplete_tasks = False
                         if task_match:
                             completed = int(task_match.group(1))
                             total = int(task_match.group(2))
                             has_incomplete_tasks = completed < total
                         
                         # Also check for explicit incomplete indicators
                         has_incomplete_tasks = has_incomplete_tasks or any(pattern in tool_result_str for pattern in [
                             "tasks remaining", "remaining tasks", "incomplete", "not finished",
                             "still working", "continue", "next task"
                         ])
                         
                         # Check for questions or help requests
                         has_questions = any(pattern in tool_result_str for pattern in [
                             "what should", "how should", "which", "should i", "do you want",
                             "what do you", "can you tell", "please specify", "need to know",
                             "unclear", "not sure", "help", "question", "?", "what is",
                             "i need", "i don't know", "unsure"
                         ])
                         
                         # Check for placeholders that need filling
                         has_placeholders = any(pattern in tool_result_str for pattern in [
                             "placeholder", "template", "muster", "example", "todo:", "fix placeholders",
                             "unchanged placeholders", "generic text", "replace placeholders"
                         ])
                         
                         # Check if coding agent claims completion but has incomplete work
                         claims_completion = any(pattern in tool_result_str for pattern in [
                             "task completed", "all tasks completed", "finished", "done", "complete"
                         ])
                         
                         # Build context about original task
                         task_context = f"\n\n**Original user task:** {original_task[:200]}" if original_task else ""
                         
                         if (has_incomplete_tasks or has_placeholders) and not claims_completion:
                             # Coding agent has incomplete work - help it complete
                             remaining_info = f"Tasks: {task_match.group(1)}/{task_match.group(2)} incomplete" if task_match else "Tasks incomplete"
                             prompt = (
                                 f"⚠️ The coding agent has INCOMPLETE work. {remaining_info}.\n\n"
                                 f"Result excerpt: {tool_result[:400]}{task_context}\n\n"
                                 f"**Your job:** Help the coding agent complete ALL remaining tasks.\n"
                                 f"1. Read the original user task above to understand the full requirements\n"
                                 f"2. If placeholders exist, instruct the coding agent to fill them with relevant content based on the task context\n"
                                 f"3. If tasks are incomplete, instruct the coding agent to continue working on ALL remaining tasks\n"
                                 f"4. DO NOT report to the user yet - help the coding agent finish first\n"
                                 f"5. Call coding_agent again with clear instructions to complete the work\n\n"
                                 f"Example instruction: 'Fill all placeholders with content relevant to the task. Complete all remaining tasks in the TODO list.'"
                             )
                         elif has_questions:
                             # Coding agent has questions - answer them based on task context
                             prompt = (
                                 f"❓ The coding agent has QUESTIONS that need answers.\n\n"
                                 f"Result excerpt: {tool_result[:400]}{task_context}\n\n"
                                 f"**Your job:** Answer the coding agent's questions based on the original user task above.\n"
                                 f"1. Review the original user task to understand what they want\n"
                                 f"2. Answer the coding agent's questions with specific, helpful information derived from the task context\n"
                                 f"3. If the task doesn't specify something, make reasonable assumptions based on the task context\n"
                                 f"4. DO NOT report to the user yet - answer the coding agent first\n"
                                 f"5. Call coding_agent again with your answers\n\n"
                                 f"Example: If asked 'What should the company name be?' and the task mentions 'craftsman in Berlin', answer: 'Use a generic craftsman business name like \"Berlin Handwerkskunst\" or similar based on the task context.'"
                             )
                         elif has_placeholders and claims_completion:
                             # Coding agent claims completion but has placeholders - force it to fill them
                             prompt = (
                                 f"🚨 The coding agent claims completion but has UNFILLED PLACEHOLDERS!\n\n"
                                 f"Result excerpt: {tool_result[:400]}{task_context}\n\n"
                                 f"**Your job:** Force the coding agent to fill ALL placeholders before completion.\n"
                                 f"1. Read the original user task above to understand what content should replace placeholders\n"
                                 f"2. Instruct the coding agent to fill ALL placeholders with relevant content based on the task context\n"
                                 f"3. DO NOT accept completion until ALL placeholders are filled\n"
                                 f"4. Call coding_agent again with: 'Fill all remaining placeholders with content relevant to the task. Replace generic text with specific details.'\n\n"
                                 f"DO NOT report to the user until placeholders are filled!"
                             )
                         elif has_incomplete_tasks and claims_completion:
                             # Coding agent claims completion but has incomplete tasks - force it to finish
                             remaining_info = f"Only {task_match.group(1)}/{task_match.group(2)} tasks completed" if task_match else "Tasks incomplete"
                             prompt = (
                                 f"🚨 The coding agent claims completion but has INCOMPLETE TASKS! {remaining_info}.\n\n"
                                 f"Result excerpt: {tool_result[:400]}{task_context}\n\n"
                                 f"**Your job:** Force the coding agent to complete ALL remaining tasks.\n"
                                 f"1. Read the original user task above to understand what needs to be done\n"
                                 f"2. Instruct the coding agent to continue working on ALL remaining tasks in the TODO list\n"
                                 f"3. DO NOT accept completion until ALL tasks are done\n"
                                 f"4. Call coding_agent again with: 'Continue working on all remaining tasks. Complete the TODO list before claiming completion.'\n\n"
                                 f"DO NOT report to the user until all tasks are completed!"
                             )
                         else:
                             # Coding agent is truly done - now inform user with brief summary
                             prompt = (
                                 f"✅ The coding agent has COMPLETED its work successfully.\n\n"
                                 f"Result excerpt: {tool_result[:400]}\n\n"
                                 f"**Your job:** Give the user a brief, friendly summary.\n"
                                 f"1. Summarize what was created (e.g., 'I've created a website for...')\n"
                                 f"2. Mention the project location/path if provided\n"
                                 f"3. Ask if the user wants any changes or additions\n"
                                 f"4. Keep it concise - the coding agent already provided technical details\n\n"
                                 f"Example: 'I've created your website! The files are in [path]. Would you like me to make any changes or additions?'"
                             )
                     else:
                         prompt = "The tool has finished execution. Analyze the result and proceed with the next step."
                else:
                    # Did the model plan a tool use but fail to execute?
                    lower_response = full_response.lower()

                    def _is_missing_info_clarification(resp: str) -> bool:
                        """
                        If the assistant is asking for missing info (e.g., city for weather),
                        do NOT force a tool call. Clarification is the correct next step.
                        """
                        r = (resp or "").strip().lower()
                        if not r:
                            return False
                        has_q = "?" in r
                        patterns = [
                            # German
                            "welche stadt", "welchen ort", "für welche stadt", "für welchen ort",
                            "wo (stadt", "wo (stadt/ort", "wo soll ich", "welche region",
                            "bitte den namen der stadt", "stadt oder ort", "stadt/ort",
                            # English
                            "which city", "what city", "which location", "what location",
                            "where should i", "which region", "what region",
                        ]
                        asks_location = any(p in r for p in patterns)
                        asks_more = any(p in r for p in ["need more information", "please specify", "bitte gib", "bitte sag"])
                        return (has_q and (asks_location or asks_more)) or asks_location

                    detected_tools = [t for t in ["write_file", "read_file", "list_files", "web_search", "run_command", "librarian_agent", "find_files"] if t in lower_response]
                    if detected_tools and retries < 2 and not _is_missing_info_clarification(lower_response):
                        prompt = f"You planned to use '{detected_tools[0]}'. Stop thinking and output the JSON for '{detected_tools[0]}' now."
                    elif retries < 3:
                        prompt = "You have reflected enough. Now strictly output the next Tool Call (JSON) or your Final Answer."
                    else:
                        prompt = "STOP THINKING. You MUST now output a JSON Tool Call or a text Answer. Do not generate more thoughts."

                self.history.append({"role": "system", "content": prompt})
                
                # Silent retry
                retries += 1
                continue
            
            # ═══════════════════════════════════════════════════════════════════════
            # Case C: "Tool Talk" without Tool Action
            # Model says "I'll use web_search" but didn't actually call it!
            # IMPORTANT: Only trigger if response is SHORT or has intent patterns.
            # Long explanatory answers that MENTION tools are OK!
            # ═══════════════════════════════════════════════════════════════════════
            
            # Track tool-talk retries separately (max 2)
            if not hasattr(self, '_tool_talk_retries'):
                self._tool_talk_retries = 0
            
            # Only check if: no tools called, not too many retries, and response is short
            response_length = len(full_content.strip())
            is_short_response = response_length < 300  # Short responses are suspicious
            
            # CRITICAL: Don't trigger tool-intent detection if web_search was already executed in this conversation
            # (The model might mention it in "thinking" after getting results, but shouldn't be forced to call it again)
            web_search_already_executed = any(
                msg.get("role") == "tool" and msg.get("name") == "web_search"
                for msg in self.history[-10:]  # Check last 10 messages
            )
            
            if not tool_calls_detected and self._tool_talk_retries < 2 and is_short_response and not web_search_already_executed:
                lower_response = full_response.lower()
                
                # STRICT patterns: These indicate INTENT to use a tool (not just mentioning)
                intent_patterns = [
                    "i'll use", "i will use", "let me use", "i'll perform", "i'll search",
                    "ich werde nutzen", "ich werde verwenden", "lass mich suchen",
                    "let me search", "i need to search", "ich muss suchen",
                    "i should search", "ich sollte suchen",
                    "perform a search", "eine suche durchführen",
                ]
                
                # Check for intent patterns (NOT just tool name mentions!)
                has_intent = any(pattern in lower_response for pattern in intent_patterns)
                
                # Only trigger on clear intent, not just tool mentions
                if has_intent:
                    # If the assistant is asking for missing info (e.g., city/location), allow it.
                    # We should only force tool calls when we have enough info to execute.
                    def _is_missing_info_clarification(resp: str) -> bool:
                        r = (resp or "").strip().lower()
                        if not r:
                            return False
                        has_q = "?" in r
                        patterns = [
                            "welche stadt", "welchen ort", "für welche stadt", "für welchen ort",
                            "wo (stadt", "wo (stadt/ort", "wo soll ich", "welche region",
                            "bitte den namen der stadt", "stadt oder ort", "stadt/ort",
                            "which city", "what city", "which location", "what location",
                            "where should i", "which region", "what region",
                        ]
                        asks_location = any(p in r for p in patterns)
                        asks_more = any(p in r for p in ["need more information", "please specify", "bitte gib", "bitte sag"])
                        return (has_q and (asks_location or asks_more)) or asks_location

                    if _is_missing_info_clarification(lower_response):
                        self._tool_talk_retries = 0
                        # Skip tool-talk forcing; clarification is acceptable.
                    else:
                        # Detect WHICH tool was mentioned for specific guidance
                        tool_names = ["web_search", "webfetch", "coding_agent", "librarian_agent", 
                                     "write_file", "read_file", "bash", "list_files"]
                        mentioned_tools = [t for t in tool_names if t in lower_response]
                        tool_hint = mentioned_tools[0] if mentioned_tools else "web_search"
                        
                        UI.event("Debug", f"Tool-Intent '{tool_hint}' without action. Retry {self._tool_talk_retries + 1}/2", style="warning")
                        
                        # DON'T add failed response - give SPECIFIC instruction
                        self.history.append({
                            "role": "system", 
                            "content": f"⚠️ You said you would use '{tool_hint}' but DIDN'T!\n"
                                       f"Your NEXT message MUST be a tool call to '{tool_hint}'.\n"
                                       f"NO explanations. NO apologies. OUTPUT '{tool_hint}' TOOL CALL NOW."
                        })
                        self._tool_talk_retries += 1
                        retries += 1
                        continue
            
            # Reset tool-talk counter on successful response
            self._tool_talk_retries = 0
            
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

        emit({"type": "tool_start", "tool": name, "args": args})
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
                    emit({"type": "tool_start", "tool": "python_exec", "args": {"timeout": 30}})
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
            import re
            
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
