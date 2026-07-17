# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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
import threading
from datetime import datetime
import re
from typing import List, Dict, Any, Optional, Tuple
from rich import print
from rich.markup import escape
from pathlib import Path

# Dependency imports are handled at setup / assumed present when requirements are installed.
# (Model downloads go through vaf.core.backend.ensure_model_available, not a direct hf_hub_download here.)

from vaf.core.config import Config
from vaf.core.backend import ServerManager
from vaf.core.platform import Platform
from vaf.core.log_helper import append_domain_log, get_dated_log_path, log_tool_use, log_timeline_event
from vaf.core.system_prompt import SystemPromptManager
from vaf.core.last_interaction import get_last_interaction
from vaf.tools.search import WebSearchTool, get_web_search_results
from vaf.tools.filesystem import ListFilesTool, ReadFileTool, WriteFileTool, MoveFileTool

import atexit
import signal

def _emit_to_web_ui() -> bool:
    """False when running a background pass (thinking mode or a scheduled automation) – do not push
    tool/log/status updates into any live chat session. Automations deliver only their final result
    (via _push_result_to_web_ui); the live progress noise must stay out of whoever is the active user."""
    if os.environ.get("VAF_THINKING_MODE", "").strip() in ("1", "true", "yes"):
        return False
    if os.environ.get("VAF_IN_AUTOMATION", "").strip() in ("1", "true", "yes"):
        return False
    return True


def _extract_action_text(text: str):
    """Return the inner text of the first <Action>...</Action> block, or None.

    Part of the Action-Tag parser (see docs/agents/ACTION_TAG.md). This reads the agent's
    committed intent from its own output; it is unrelated to the Web UI display parser.
    """
    import re
    if not text:
        return None
    m = re.search(r'<action>([\s\S]*?)</action>', text, re.IGNORECASE)
    return m.group(1).strip() if m else None


# Bookkeeping tools that manage working memory / intent but do NOT do the actual task work.
# The anti-spin guard counts CONSECUTIVE calls to these to catch a "plan forever, never act"
# loop. Kept narrow on purpose: other system-permission tools (thinking_done, batch, builders,
# request_clarification, memory_save) are real actions and must NOT count as spin.
_BOOKKEEPING_TOOLS = frozenset({"update_working_memory", "update_intent", "add_task"})

# Read-only / verification tools that make NO real progress toward the user's goal. The main-loop
# no-progress guard counts CONSECUTIVE turns that use ONLY such tools (a "verify forever" loop, e.g.
# create_automation succeeded but the model keeps calling list_automations/read_automation). Any
# mutating/producing tool (create_*/update_*/delete_*/write_*/send_*/a sub-agent) resets the streak,
# so legitimate varied multi-step work is never penalised. Matched by exact name OR a list_/read_/get_
# prefix. Kept separate from _BOOKKEEPING_TOOLS (that guards plan-spin; this guards read/verify-spin).
_NONPROGRESS_TOOLS = frozenset({
    "list_automations", "read_automation", "list_automation_notes", "list_automation_todos",
    "list_calendar_events", "mail_inbox", "read_mail", "find_mail",
    "list_timers", "list_email_accounts", "git_status",
})
# NOTE: web_search / memory_search are intentionally NOT here — they are genuine information-gathering
# a direct turn may legitimately repeat. A web_search/search spin is still caught by the 5s-emergency
# break, the redundant-exact-args block, and the wall-clock backstop; the thinking read-cap covers
# thinking mode. This set is for pure LIST/READ verification (the create_automation "verify forever" loop).


def _is_nonprogress_tool(name: str) -> bool:
    n = (name or "").strip()
    return n in _NONPROGRESS_TOOLS or n.startswith(("list_", "read_", "get_"))


# Read/gather tools a background thinking run must not call endlessly. The redundant-call block only
# catches EXACT-arg duplicates, so a weak model can spin memory_search with varied queries (or re-list)
# forever (observed: 5 memory_search calls drifting off-topic). The thinking read-cap blocks these by
# NAME after a few calls within one step, telling the model to act on what it already gathered.
_READ_TOOLS_THINKING = frozenset({
    "memory_search", "web_search", "list_automation_notes", "list_automation_todos", "list_automations",
})

# Decision nudge for the PROACTIVE grounding step (there is NO open note/todo there, so the housekeeping
# "resolve the open item / delete_automation_note" block message misleads the weak model into searching
# again instead of committing). Returned when it over-searches or reaches for a blocked tool.
_PROACTIVE_DECIDE_NUDGE = (
    "You have searched enough this run — do NOT call {fn} again. Decide NOW from the real memories you "
    "retrieved: EITHER ask_user(message=\"<one specific suggestion, ideally an automation that takes "
    "recurring work off the user>\", proposed_action=\"create automation: <what + when>\", details=\"<a "
    "VERBATIM quote of one real memory you just saw>\") — OR, only if nothing is genuinely groundable, "
    "thinking_done(\"Nothing grounded.\"). No more searching, no prose."
)


def _synth_tool_call_id() -> str:
    """Mint an id for a tool call VAF created ITSELF (text-recovery fallbacks,
    streams that never delivered an id, sanitizer repairs). The provider never
    issued this call, so the id carries a recognizable prefix: providers that
    only accept their own ids on replay (Veyllo) get such exchanges converted
    to plain text by _downgrade_synthetic_tool_exchanges() pre-send.
    """
    return f"call_synth_{os.urandom(4).hex()}"


# Matches every id VAF ever minted itself: the current call_synth_ prefix and
# the legacy shapes still found in persisted sessions (extracted_<epoch> from
# tool_call_recovery, call_<8hex> from the old inline mints - genuine gateway
# ids are call_00_<...> at 32 chars, so the 8-hex form cannot collide).
_SYNTHETIC_TC_ID_RE = re.compile(r"^(call_synth_|extracted_\d|call_[0-9a-f]{8}$)")


def _downgrade_synthetic_tool_exchanges(messages):
    """Veyllo: replace tool exchanges with VAF-minted ids by plain text on the
    OUTBOUND copy.

    The provider rejects a replayed tool_call id it did not issue itself.
    Recovered calls (deepseek-v4 leaks calls as content) and
    id-less streams only ever have synthetic ids, so their structured replay is
    guaranteed to fail. This folds exactly those exchanges into the same
    summary shape the end-of-turn squash uses - the model keeps call + result
    as context - while provider-issued exchanges replay untouched. A batch
    mixing genuine and synthetic ids is downgraded whole to keep the pairing
    consistent.

    The folded note goes into SYSTEM messages only (squash precedent). An
    earlier version put it into the assistant message - the model then
    PARROTED "[Context: ...]" blocks as its own answer style, and a live call
    read one aloud. The assistant message keeps only its real prose (if any).
    """
    out = []
    synth_calls = {}  # id -> "name(args)" of downgraded calls awaiting results
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            calls = msg["tool_calls"]
            if any(_SYNTHETIC_TC_ID_RE.match(str(tc.get("id") or "")) for tc in calls):
                for tc in calls:
                    fn = tc.get("function", {}) or {}
                    args = str(fn.get("arguments") or "")[:300]
                    synth_calls[tc.get("id")] = f"{fn.get('name')}({args})"
                text = str(msg.get("content") or "").strip()
                if text:
                    out.append({"role": "assistant", "content": text})
                continue
        if msg.get("role") == "tool" and msg.get("tool_call_id") in synth_calls:
            call = synth_calls.pop(msg["tool_call_id"])
            content = " ".join(str(msg.get("content") or "").split())[:600]
            out.append({"role": "system",
                        "content": f"[Context: recovered tool call] {call} -> {content}"})
            continue
        out.append(msg)
    return out


def _parse_paren_tool_calls(text: str, tools) -> list:
    """Fallback 3: 'name(...)' style tool calls written as TEXT.

    Covers the classic 'web_search("query")' hallucination AND leaked plan
    bullets like '- find_mail({"query": "x", "limit": 20})' - deepseek-v4
    intermittently writes its next calls as a markdown list instead of
    structured tool_calls; unrecovered they never execute and the bare plan
    becomes the FINAL ANSWER (live incident: read aloud on a voice call).
    Only names present in `tools` are recovered. Returns [(name, args_dict)].
    """
    out = []
    for func_name, args_str in re.findall(
            r'(?:^|\n)\s*(?:Answer:)?\s*(?:\d+\.\s*|[-*]\s+)?([a-zA-Z0-9_]+)\s*\((.*?)\)', text):
        if func_name not in tools:
            continue
        args = {}
        s = args_str.strip()
        if s.startswith("{"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    args = parsed
            except Exception:
                args = {}
        elif s.startswith('"') or s.startswith("'"):
            # Heuristic: map a single string argument to the first parameter
            clean_arg = s.strip("\"'")
            tool_def = tools[func_name]
            params = getattr(tool_def, "parameters", None)
            if params is None and isinstance(tool_def, dict):
                params = tool_def.get("parameters")
            props = params.get("properties", {}) if isinstance(params, dict) else {}
            if props:
                args[list(props.keys())[0]] = clean_arg
        if args:
            out.append((func_name, args))
    return out


def _parse_qwen_tool_calls(text: str, valid_names=None):
    """Parse Qwen / Hermes style tool calls that a reasoning model sometimes emits as TEXT (often inside
    `<think>`) instead of a native call, so the server never converts them and the call is silently
    dropped (observed: `update_working_memory` written this way -> the plan is never set -> the
    `[PLAN REQUIRED]` gate loops forever):

        <tool_call><function=NAME><parameter=KEY>VALUE</parameter>...</function></tool_call>

    Parameters are parsed as a SEQUENCE, one at a time, right after `<function=NAME>`: each
    `<parameter=KEY>VALUE</parameter>` is consumed in order (VALUE bounded only by its own literal
    `</parameter>`, however long or tag-like its content is), then whatever immediately follows
    (whitespace only, no other characters) decides how the call ends:

    - `</function></tool_call>` - a real close: done, well-formed. Accepted anywhere in the text
      (a strictly-closed call is unambiguous).
    - LENIENT recovery, only when the `<tool_call>` open sits at the start of a line (nothing but
      whitespace before it on its line - how models genuinely emit calls, and what an inline
      explanatory mention wrapped in prose or markup does NOT look like), either of:
        * TWO or more OTHER closing tags in a row (`</a></b>...`) - the model attempted to close
          SOMETHING, just used the wrong names (live incident: a local model trailed off into
          hallucinated `</tasks></working_memory>` instead of its own close). ONE closing tag alone
          is NOT enough: a single wrong-named tag is exactly what an example wrapped in inline markup
          produces (`...</parameter></code>`), and dispatching that is a real execution risk.
        * the next `<tool_call>` beginning immediately - back-to-back text calls where the model
          forgot the first call's close entirely (the original incident shape: the call silently
          vanishes otherwise).
      Recovered with whatever parameters were already parsed, including zero.
    - anything else (prose, another `<parameter=` that itself never closes, or nothing before the
      text simply ends) - REJECTED, call is not recovered.

    Those rules are deliberate and load-bearing, not an afterthought: THREE earlier versions of this
    function failed adversarial review. One matched an unclosed call to end-of-text, one accepted a
    closing-tag-shaped substring ANYWHERE later in the text - both let an incidental, never-truly-closed
    EXPLANATION of the tool-call format ("tool calls look like
    <tool_call><function=web_search><parameter=query>example</parameter> when using this format")
    turn into a genuinely dispatched call - and one accepted a SINGLE wrong-named closing tag sitting
    immediately after the last parameter, which an example wrapped in `<code>`/`<pre>`/list markup
    satisfies by construction. Requiring (a) the open at a line start, (b) the closing attempt
    IMMEDIATELY (whitespace only) after the last parsed parameter, and (c) at least two consecutive
    closing tags (or the next `<tool_call>`) is what tells "the model tried to close its own call and
    botched it" apart from "the model was talking about the call syntax". Parameters are parsed one at
    a time bounded by their OWN `</parameter>`, so a value that itself contains closing-tag-shaped
    text (e.g. a `plan` describing removing stray HTML tags) can never truncate the call early and
    silently drop it (a second bug the same reviews found).

    Returns a list of (name, args_dict). Each parameter VALUE is JSON-decoded when possible (so a list /
    dict / number survives) and otherwise kept as the trimmed string. Tolerant of newlines/whitespace.
    Pure function; `valid_names` (when given) restricts results to known tool names - the anchor is
    still an exact `<tool_call><function=KNOWN_NAME>` open, so this stays as bounded as the strict
    version was, just more forgiving about how the call ends."""
    import re as _re
    import json as _json

    out = []
    text = text or ""
    pos = 0
    open_re = _re.compile(r'<tool_call>\s*<function=([\w.\-]+)\s*>')
    param_re = _re.compile(r'\s*<parameter=([\w.\-]+)\s*>(.*?)</parameter>', _re.DOTALL)
    close_re = _re.compile(r'\s*</function>\s*</tool_call>')
    trailer_re = _re.compile(r'(?:\s*</[\w.\-]+>){2,}')
    next_call_re = _re.compile(r'\s*<tool_call>')

    def _at_line_start(p: int) -> bool:
        nl = text.rfind("\n", 0, p)
        return text[nl + 1:p].strip() == ""

    while True:
        m = open_re.search(text, pos)
        if not m:
            break
        name = (m.group(1) or "").strip()
        cursor = m.end()

        args = {}
        while True:
            pm = param_re.match(text, cursor)
            if not pm:
                break
            key = (pm.group(1) or "").strip()
            raw = (pm.group(2) or "").strip()
            try:
                args[key] = _json.loads(raw)
            except Exception:
                args[key] = raw
            cursor = pm.end()

        cm = close_re.match(text, cursor)
        if cm:
            cursor = cm.end()
            recovered = True
        elif _at_line_start(m.start()):
            tm = trailer_re.match(text, cursor)
            if tm:
                cursor = tm.end()
                recovered = True
            else:
                # Back-to-back calls: the model forgot this call's close and
                # opened the next one right away. Do NOT advance the cursor -
                # the next loop iteration must parse that following call.
                recovered = bool(next_call_re.match(text, cursor))
        else:
            recovered = False

        # Always advance past this call's opening tag, even when rejected, so a
        # malformed occurrence can never stall the search or be re-examined.
        pos = cursor if cursor > m.end() else m.end()
        if not recovered:
            continue
        if not name or (valid_names is not None and name not in valid_names):
            continue
        out.append((name, args))
    return out


def _parse_gemma4_tool_calls(text: str, valid_names=None):
    """Parse Gemma-4 native tool calls from raw model output -> list of (name, args_dict).

    Format: `<|tool_call>call:NAME{key:<|"|>value<|"|>,key2:bare,...}<tool_call|>` (one or more).
    Pure (no Agent state) and delimiter-aware: the `}<tool_call|>` anchor stops a `}` inside a value
    from ending the call, and quoted `<|"|>...<|"|>` values keep their commas/braces verbatim (never a
    raw comma-split). Names are kept only if in `valid_names` (when given). Never raises.
    """
    import re
    out = []
    if not text or "<|tool_call>" not in text:
        return out
    for name, body in re.findall(r'<\|tool_call>call:([\w.:-]+)\{(.*?)\}<tool_call\|>', text, re.DOTALL):
        if valid_names is not None and name not in valid_names:
            continue
        args = {}
        for m in re.finditer(r'(\w+):(?:<\|"\|>([\s\S]*?)<\|"\|>|([^,}]*))', body):
            if m.group(2) is not None:
                args[m.group(1)] = m.group(2)            # quoted: preserve commas / braces verbatim
            else:
                args[m.group(1)] = (m.group(3) or "").strip()
        out.append((name, args))
    return out


def _match_action_to_tools(action_text: str, tool_names):
    """Cheap fuzzy match of the committed <Action> text against tool names (no LLM).

    Full tool name appears in the text => score 1.0; otherwise the fraction of the
    tool name's tokens (split on non-alphanumerics) that occur in the action text.
    Returns a list of (name, score) sorted by score descending.
    """
    import re
    text = (action_text or "").lower()
    text_tokens = set(re.findall(r'[a-z0-9]+', text))
    scored = []
    for name in tool_names:
        n = str(name).lower()
        if n and n in text:
            score = 1.0
        else:
            parts = [p for p in re.split(r'[^a-z0-9]+', n) if p]
            hits = sum(1 for p in parts if p in text_tokens)
            score = (hits / len(parts)) if parts else 0.0
        if score > 0:
            scored.append((name, round(score, 2)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


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
    """The VAF engine: one instance = one conversation over one LLM backend.

    Re-exported as ``vaf.CoreAgent`` for advanced embedding; most consumers
    should use the ``vaf.Agent`` facade instead. The embedder-facing contract
    (constructor, lifecycle, chat_step/execute_tool semantics, concurrency
    rules) is documented in docs/CORE_AGENT.md; the turn-loop design map for
    contributors is docs/agents/AGENT_LOOP.md.

    Effectively single-threaded: per-turn state lives in instance attributes
    and ``history``/``tools`` are mutated without locks. Never drive one
    instance from two threads; create one instance per parallel conversation.
    """

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
        "llama_cpp": "llama-cpp-python"
    }
    
    # Defaults handled by Config, but fallback here
    DEFAULT_FILENAME = "VQ-1_Instruct-q4_k_m.gguf"
    
    def _build_api_backend(self, provider):
        """Construct an APIBackendManager, passing programmatic config overrides through.

        When VAF is embedded as a library via Agent(config={...}), the api_key /
        api_model from the override dict must reach the backend instead of being read
        from ~/.vaf/config.json. The override api_key is RAW (never base64-decoded).
        With no overrides (product mode) behaviour is byte-identical to before.
        """
        from vaf.core.api_backend import APIBackendManager
        ov = getattr(self, "_config_overrides", None) or {}
        api_key = ov.get(f"api_key_{provider}") if ov else None  # RAW, used as-is
        cfg = self.config if ov else None                        # merged cfg only in embed mode
        return APIBackendManager(provider, config=cfg, api_key=api_key)

    def reload_api_backend(self, *, force: bool = False) -> bool:
        """Re-apply provider + API key from the LIVE on-disk config to this RUNNING agent.

        Lets a provider switch take effect without restarting VAF -- e.g. finishing
        onboarding with a Veyllo key, or changing the provider in Settings -- and
        prevents a stale local agent from downloading a GGUF after the user picked a
        cloud provider. Returns True when the active backend actually changed.
        Handles local->cloud, cloud->cloud (key/provider swap) and cloud->local.
        """
        # A sub-agent process is pinned to a provider via VAF_PROVIDER; never override it.
        if os.environ.get("VAF_PROVIDER", "").strip():
            return False
        # Embedded library mode is caller-controlled (config_overrides) -- leave it alone.
        if getattr(self, "_config_overrides", None):
            return False

        lock = getattr(self, "_backend_swap_lock", None)
        if lock is None:
            lock = self._backend_swap_lock = threading.Lock()
        with lock:
            fresh = Config.load()
            new_provider = (fresh.get("provider", "local") or "local").strip()
            old_provider = getattr(self, "provider", "local")

            # No-op: same provider and backend already matches (unless forced, e.g. key change).
            if new_provider == old_provider and not force:
                if new_provider == "local" or getattr(self, "api_backend", None):
                    return False

            # Pick up the rest of the live config (api_model_*, etc.).
            self.config = fresh

            if new_provider != "local":
                try:
                    new_backend = self._build_api_backend(new_provider)
                except Exception as e:
                    if self.verbose:
                        print(f"[Agent] reload_api_backend: cannot build '{new_provider}' backend: {e}")
                    return False
                self.provider = new_provider
                self.api_backend = new_backend
                # Keep the structured-event sink attached across backend swaps.
                if getattr(self, "_event_sink", None) is not None:
                    new_backend.event_sink = self._event_sink
                # Tear down local backends so generation uses the API, not a GGUF.
                self.use_server = False
                self.llm = None
                if getattr(self, "server", None) is not None:
                    try:
                        self.server.stop_server()
                    except Exception:
                        pass
                    self.server = None
                self._tokenizer_instance = None
                # Refresh the displayed model name (mirror __init__ logic at agent.py:377-386).
                api_model = self.config.get(f"api_model_{new_provider}")
                if not api_model:
                    api_model = Config.get_default_model(new_provider) or new_provider
                _tool_model_env = os.environ.get("VAF_TOOL_MODEL", "").strip()
                if _tool_model_env:
                    api_model = _tool_model_env
                self.model_display_name = api_model
            else:
                # cloud -> local: drop the API backend; the local model loads lazily
                # via the download gate / load_model, exactly as on a fresh local start.
                self.provider = "local"
                self.api_backend = None
                self._tokenizer_instance = None

            return True

    def __init__(self, verbose=False, register_signals=True, config_overrides=None, run_kind=None,
                 host_audio=False):
        self.verbose = verbose
        # Per-instance run kind: 'thinking' | 'automation' | 'chat' | None.
        # Tool registration and dispatch decisions MUST use this instance truth,
        # never the process-global env vars: env is shared across threads, so a
        # concurrent automation run makes every other agent in the process look
        # like an automation (live incident 2026-07-13: a thinking question was
        # misrouted into an automation handoff bundle). The env sniff below is
        # only a fallback for constructors that predate the kwarg (embedders,
        # subprocess lanes) - first-party construction sites pass it explicitly.
        if run_kind is None:
            if os.environ.get("VAF_THINKING_MODE", "").strip() in ("1", "true", "yes"):
                run_kind = "thinking"
            elif os.environ.get("VAF_IN_AUTOMATION", "").strip() in ("1", "true", "yes"):
                run_kind = "automation"
        self._run_kind = run_kind
        # Positive opt-in for host-speaker audio (TTS, fillers, answer chime).
        # Only the interactive CLI passes True; every other construction site
        # (headless web/channel queue, automations, thinking runs, gateway,
        # vaf run -p, embedders) stays fail-closed and must never play sound
        # on the machine the server happens to run on.
        self._host_audio_allowed = bool(host_audio)
        self.config = Config.load()
        # Programmatic config injection for embedding VAF as a library.
        # Merges on top of the on-disk config without writing ~/.vaf/config.json,
        # so each Agent instance can carry its own provider/model/api_key/n_ctx.
        # Default None -> behaviour is byte-identical to before.
        if config_overrides:
            self.config = {**self.config, **config_overrides}
        # Keep the raw overrides so the API backend can receive a programmatic
        # api_key/api_model (RAW, not base64) when VAF is embedded as a library.
        self._config_overrides = config_overrides or {}
        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.models_dir = os.path.join(self.base_dir, "models")
        
        # Determine model filename from config path or just name
        model_name = os.environ.get("VAF_MODEL_OVERRIDE", "").strip() or self.config.get("model")
        # "auto" -> VRAM-aware default (gemma-4 E4B Q8 if >10GB VRAM, else E2B).
        if (model_name or "").strip().lower() == "auto":
            from vaf.core.gpu_detection import recommended_default_model
            model_name = recommended_default_model()
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
        self._backend_swap_lock = threading.Lock()  # serializes runtime provider/backend swaps
        
        # Initialize API backend immediately if using API provider
        if self.provider != "local":
            try:
                self.api_backend = self._build_api_backend(self.provider)
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
        self._orchestrator_heavy_calls_this_turn = 0  # Reset each turn; used when orchestrator + small n_ctx
        self._anti_spin_streak = 0  # consecutive bookkeeping (plan/intent) calls; anti-spin guard
        # Task-stuck guard (complement to anti-spin): a weak model can finish a step's work but never
        # call mark_task_done, so the pending-task auto-continue keeps forcing it to "keep going" — it
        # redoes the same step until the hard cap and leaves it unmarked for the next run. Track
        # consecutive auto-continues on the SAME step; escalate, then auto-complete it to break the loop.
        self._autocontinue_step_sig = None  # signature (idx, text) of the step the auto-continue last fired on
        self._autocontinue_stuck = 0        # consecutive auto-continues on that same step without progress
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
        context_limit = max(self.config.get("n_ctx", 32768), 32768)  # 32768 minimum
        
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
        # Canonical Gemma-local mode (single source of truth): computed once here so the tool-call
        # parser, the message-prepare merge, etc. all read these instead of re-matching "gemma" ad hoc.
        self.is_gemma_local = False   # local Gemma GGUF of any version
        self.model_mode = None        # "gemma4" / "gemma3n" / None -- gate for version-specific handling
        if self.provider != "local":
            api_model = self.config.get(f"api_model_{self.provider}")
            if not api_model:
                # Single source: Config.PROVIDER_MODELS (vaf/core/config.py)
                api_model = Config.get_default_model(self.provider) or self.provider
            # Hybrid mode: use VAF_TOOL_MODEL when set (e.g. pro model for sub-agents in workflows)
            _tool_model_env = os.environ.get("VAF_TOOL_MODEL", "").strip()
            if _tool_model_env:
                api_model = _tool_model_env
            self.model_display_name = api_model
        elif hasattr(self, 'filename'):
            fname = self.filename.lower()
            if "gemma" in fname:
                self.model_display_name = "Gemma"
                self.is_gemma_local = True
                if "gemma-4" in fname or "gemma4" in fname:
                    self.model_mode = "gemma4"
                elif "gemma-3n" in fname or "3n" in fname:
                    self.model_mode = "gemma3n"
            elif "llama" in fname: self.model_display_name = "Llama"
            elif "mistral" in fname: self.model_display_name = "Mistral"
            elif "phi" in fname: self.model_display_name = "Phi"
            elif "qwen" in fname: self.model_display_name = "Qwen"
            elif "deepseek" in fname: self.model_display_name = "DeepSeek"
        
        # We need tools to init prompt manager, but tools are loaded later.
        # So we init it here with empty dict and update it after tools load.
        self.prompt_manager = SystemPromptManager({}, model_name=self.model_display_name, agent_instance=self, max_tokens=context_limit) 

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
        self._ww_stale_checked = False  # Whare Wananga: one-time schema-hash invalidation (see TOOLS)
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

        # Result Grounding State (anti-confabulation of tool results)
        self._result_grounding_retries = 0

        # Plan gate State (main agent: plan before a state-changing tool runs)
        self._plan_gate_blocks = 0

        # Team-await State (main agent: don't declare done while sub-agents genuinely run)

    @staticmethod
    def _is_placeholder_plan(plan) -> bool:
        """True when the working-memory plan is an obvious PLACEHOLDER, not a real approach -- a weak
        model writes e.g. "Neuer Test-Plan hier" / "test" just to open the plan gate, which then lets a
        write/dangerous tool through. Such a plan must NOT satisfy the gate. Conservative: flags only
        plans that are near-empty or made up ENTIRELY of generic filler words, so any plan with real
        task content passes. The gate's loop-cap still lets the model proceed after repeated blocks,
        so a false positive never hard-locks."""
        import re as _re
        text = " ".join(str(p) for p in plan) if isinstance(plan, (list, tuple)) else str(plan or "")
        core = _re.sub(r'[^\w\s]', ' ', text)        # drop punctuation / bullets
        core = _re.sub(r'\b\d+\b', ' ', core)         # drop list numbering
        core = _re.sub(r'\s+', ' ', core).strip().lower()
        if len(core) < 6:
            return True
        _filler = {
            "neuer", "neue", "neu", "new", "mein", "meine", "dein", "deine", "my", "your", "ein", "eine",
            "der", "die", "das", "the", "a", "an", "test", "tests", "plan", "plans", "plane", "planen",
            "placeholder", "platzhalter", "todo", "tbd", "tba", "na", "xxx", "hier", "here", "beispiel",
            "example", "dummy", "temp", "temporary", "schritt", "step", "ist", "is", "und", "and", "fuer", "for",
        }
        words = core.split()
        return bool(words) and all(w in _filler for w in words)

    def _anti_spin_step(self, function_name: str):
        """Anti-spin guard: track CONSECUTIVE bookkeeping calls (update_working_memory /
        update_intent / add_task) — a weak model can re-plan the same task forever without ever
        acting. Any non-bookkeeping tool resets the streak. Returns ``(nudge_message_or_None,
        force_disable_tools)``: a firm nudge at the threshold, then a forced tools-off turn two
        steps later. The current call still runs; only the next turn is steered. Governed by
        anti_spin_enabled / anti_spin_max_planning_calls."""
        try:
            from vaf.core.config import Config
            if not Config.get("anti_spin_enabled", True):
                return (None, False)
            if function_name not in _BOOKKEEPING_TOOLS:
                self._anti_spin_streak = 0
                return (None, False)
            self._anti_spin_streak = getattr(self, "_anti_spin_streak", 0) + 1
            spin_max = max(2, int(Config.get("anti_spin_max_planning_calls", 4) or 4))
            if self._anti_spin_streak == spin_max:
                try:
                    append_domain_log("backend", f"[ANTI_SPIN] {self._anti_spin_streak} consecutive planning calls — nudging to act")
                except Exception:
                    pass
                return (
                    f"[!] STOP PLANNING. You have updated your plan/tasks/intent {self._anti_spin_streak} times in a row "
                    "without doing the actual work. Do NOT call update_working_memory, update_intent or add_task again now "
                    "— call the tool the task actually needs (e.g. the sub-agent or a write tool). If the task is already "
                    "done, answer the user in plain text.",
                    False,
                )
            if self._anti_spin_streak >= spin_max + 2:
                try:
                    append_domain_log("backend", f"[ANTI_SPIN] {self._anti_spin_streak} planning calls — forcing action (tools off next turn)")
                except Exception:
                    pass
                self._anti_spin_streak = 0
                return (
                    "You keep updating working memory instead of acting. Tools are disabled for one turn. "
                    "State your result, or the next concrete step you will take, to the user in plain text now.",
                    True,
                )
            return (None, False)
        except Exception:
            return (None, False)

    def _nonprogress_step(self, function_name: str):
        """No-progress guard (MAIN loop): count CONSECUTIVE turns that use ONLY a read-only/verify tool
        (see _is_nonprogress_tool) — the 'verify forever' loop that the create_automation runaway hit
        (the work was already done, but the model kept calling list/read tools). Any mutating/producing
        tool resets the streak, so legitimate varied work is never affected. Returns
        ``(nudge_or_None, force_disable_tools)`` like _anti_spin_step. Skipped in thinking mode (it has
        its own read-cap). Governed by anti_spin_enabled (shared) / nonprogress_max_turns."""
        try:
            from vaf.core.config import Config
            if not Config.get("anti_spin_enabled", True):
                return (None, False)
            if os.environ.get("VAF_THINKING_MODE", "").strip() in ("1", "true", "yes"):
                return (None, False)
            if not _is_nonprogress_tool(function_name):
                self._nonprogress_streak = 0
                return (None, False)
            self._nonprogress_streak = getattr(self, "_nonprogress_streak", 0) + 1
            np_max = max(2, int(Config.get("nonprogress_max_turns", 6) or 6))
            if self._nonprogress_streak == np_max:
                try:
                    append_domain_log("backend", f"[NO_PROGRESS] {self._nonprogress_streak} consecutive read/verify calls — nudging to act")
                except Exception:
                    pass
                return (
                    f"[!] You have called {self._nonprogress_streak} read-only/verify tools in a row without "
                    "making real progress. If the task is already done, answer the user in plain text NOW. "
                    "Otherwise take the concrete next ACTION (a create/update/write/send tool or a sub-agent) — "
                    "do NOT keep listing/reading/searching.",
                    False,
                )
            if self._nonprogress_streak >= np_max + 2:
                try:
                    append_domain_log("backend", f"[NO_PROGRESS] {self._nonprogress_streak} read/verify calls — forcing answer (tools off next turn)")
                except Exception:
                    pass
                self._nonprogress_streak = 0
                return (
                    "You keep verifying/reading instead of finishing. Tools are disabled for one turn. "
                    "Tell the user the result (or the one concrete next step) in plain text now.",
                    True,
                )
            return (None, False)
        except Exception:
            return (None, False)

    def _thinking_read_cap_step(self, function_name: str):
        """Thinking-mode-only per-tool-NAME read cap. Counts calls to a read/gather tool within the
        current step; at the Nth (thinking_read_cap_per_tool, default 3) it returns a block string telling
        the model to act on what it has, instead of executing the call again. Returns the block string or
        None. Gated by VAF_THINKING_MODE so the main chat loop is never affected. The per-step counter
        (self._thinking_read_counts) is reset at the start of each chat_step."""
        try:
            if os.environ.get("VAF_THINKING_MODE", "").strip() not in ("1", "true", "yes"):
                return None
            if function_name not in _READ_TOOLS_THINKING:
                return None
            # Forced-resolution node: gather tools are blocked from the FIRST call, so a forced
            # tool_choice="required" can only be satisfied by a decisive/progress tool. EXCEPTION: the
            # proactive grounding step sets _thinking_allow_search so the model can dig into ONE specific
            # thing with memory_search itself (still per-tool read-capped below, so it cannot churn);
            # everything else stays blocked.
            _proactive = bool(getattr(self, "_thinking_allow_search", False))
            if getattr(self, "_thinking_force_progress", False):
                _allow_search = _proactive and function_name == "memory_search"
                if not _allow_search:
                    try:
                        append_domain_log("backend", f"[THINKING_READ_CAP] forced-node blocked {function_name}")
                    except Exception:
                        pass
                    # Proactive grounding step has NO open item -> give the correct DECISION nudge instead of
                    # the housekeeping "resolve the open item / delete_automation_note" message (which the
                    # weak model reads as nonsense and answers by searching again).
                    if _proactive:
                        return _PROACTIVE_DECIDE_NUDGE.format(fn=function_name)
                    return (
                        f"Gathering is disabled right now — you must resolve the open item. Do NOT call "
                        f"{function_name}. Call ask_user(message=..., source_note_id=...) or "
                        "delete_automation_note(note_id=...) now."
                    )
                # memory_search allowed for the proactive step -> fall through to the (tighter) read-cap.
            from vaf.core.config import Config
            if not Config.get("thinking_read_cap_enabled", True):
                return None
            cap = max(2, int(Config.get("thinking_read_cap_per_tool", 3) or 3))
            if _proactive:
                cap = 2   # the proactive step already has the pre-fetched digest; 2 self-searches is plenty
            counts = getattr(self, "_thinking_read_counts", None)
            if counts is None:
                counts = self._thinking_read_counts = {}
            counts[function_name] = counts.get(function_name, 0) + 1
            if counts[function_name] >= cap:
                try:
                    append_domain_log("backend", f"[THINKING_READ_CAP] blocked {function_name} (#{counts[function_name]})")
                except Exception:
                    pass
                if _proactive:
                    return _PROACTIVE_DECIDE_NUDGE.format(fn=function_name)
                return (
                    f"You have already called {function_name} {counts[function_name]} times this run. "
                    "Stop gathering — you have enough context. ACT on what you already have (handle the "
                    "open note/todo, or ask one specific question), or call thinking_done."
                )
            return None
        except Exception:
            return None

    def _task_stuck_step(self, idx: int, text: str) -> str:
        """Pending-task verification (single-nudge; decouples loop termination from the model's
        bookkeeping). The auto-continue only fires on a final TEXT answer, so a model that finished a
        step but never called mark_task_done lands here. Rather than force-loop until the list is
        empty (which makes weak models redo a done step to the hard cap and carry it unmarked into the
        next run), we VERIFY once and then trust the model. Tracks CONSECUTIVE no-progress finals on
        the SAME step; returns:
          'nudge'    — first time: ask the model to confirm the step done or actually continue,
          'autodone' — it answered again without progress on the same step: trust it, the caller
                       auto-confirms the step (marks it done) and advances/ends — no loop,
          'continue' — only when the guard is disabled (legacy force-loop behaviour).
        The streak resets on real progress (a different step). Config-gated via task_stuck_guard_enabled
        / task_stuck_nudge_turns (default 1) / task_stuck_autodone_turns (default 2). Mirrors
        _anti_spin_step's shape."""
        try:
            from vaf.core.config import Config
            sig = (idx, (text or "").strip().lower()[:80])
            if sig == getattr(self, "_autocontinue_step_sig", None):
                self._autocontinue_stuck = getattr(self, "_autocontinue_stuck", 0) + 1
            else:
                self._autocontinue_step_sig = sig
                self._autocontinue_stuck = 1
            if not bool(Config.get("task_stuck_guard_enabled", True)):
                return "continue"
            autodone_at = max(2, int(Config.get("task_stuck_autodone_turns", 2) or 2))
            nudge_at = max(1, int(Config.get("task_stuck_nudge_turns", 1) or 1))
            if self._autocontinue_stuck >= autodone_at:
                return "autodone"
            if self._autocontinue_stuck >= nudge_at:
                return "nudge"
            return "continue"
        except Exception:
            return "continue"

    def _plan_gate_decision(self, name, tool_instance):
        """Main-agent plan gate: block a state-changing tool until a plan exists in working memory
        ("explore freely, plan before you act"). Returns a block message (str) to show the model, or
        None to allow. Never gates sub-agents (their own loops are untouched) and never blocks on a
        read error (fail-open). Satisfied in the same turn by update_working_memory(plan=[...]); after
        plan_gate_max_blocks it proceeds anyway so nothing hard-locks."""
        try:
            from vaf.core.config import Config
            if not Config.get("plan_gate_enabled", True):
                return None
            # Main agent only: sub-agents / non-interactive runs (automations, CLI one-shot) skip.
            is_subagent = os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "") == "1"
            if self._noninteractive or is_subagent:
                return None
            # Gated set: write/dangerous tools, except python_sandbox.
            level = getattr(tool_instance, "permission_level", "read") if tool_instance else "read"
            if level not in ("write", "dangerous") or name == "python_sandbox":
                return None
            # Plan present AND substantive? (working memory is already session-scoped). A placeholder
            # plan ("Neuer Test-Plan hier" / "test") must NOT open the gate. Fail-open on any error.
            plan_exists = True
            try:
                if self.main_persistence is not None:
                    _plan = self.main_persistence.get_working_memory().get("plan")
                    plan_exists = bool(_plan) and not self._is_placeholder_plan(_plan)
            except Exception:
                return None
            if plan_exists:
                self._plan_gate_blocks = 0
                return None
            # No plan -> gate. Loop-cap escape so it never hard-locks.
            self._plan_gate_blocks += 1
            if self._plan_gate_blocks > int(Config.get("plan_gate_max_blocks", 3)):
                self._plan_gate_blocks = 0
                try:
                    from vaf.cli.ui import UI
                    UI.event("System", f"Plan gate: proceeding without a plan after repeated blocks ('{name}').", style="warning")
                except Exception:
                    pass
                return None
            return (
                f"[PLAN REQUIRED] '{name}' changes state, so set your REAL approach for THIS task first "
                "(a placeholder like \"test\" or \"new plan\" does NOT count): "
                "update_working_memory(plan=[\"<your actual approach in a line or two>\"]). "
                "Keep plan high-level; for multi-step work put the concrete steps in tasks (add_task). "
                "A one-line approach is enough; then call the tool again. "
                "Read/search tools need no plan — use them freely to work out the approach."
            )
        except Exception:
            return None

    # ── Incident 2026-07-13 gates: unconfirmed mutation from a proactive-reply turn ──
    # Stored-state mutations and delegation lanes a misread background-question reply
    # must never trigger without a clear confirmation.
    _PROACTIVE_REPLY_MUTATION_TOOLS = frozenset({
        "update_automation", "delete_automation", "create_automation",
        "create_agent_workflow", "execute_workflow", "create_agent_tool",
    })
    _DELEGATION_TOOLS = frozenset({
        "coding_agent", "librarian_agent", "research_agent", "document_agent",
    })
    _DESTRUCTIVE_TEXT_RE = re.compile(
        r"\b(delete[ds]?|deleting|remove[ds]?|removing|erase[ds]?|erasing|unlink\w*|rm|rmdir"
        r"|l(?:ö|oe)sch\w*|gel(?:ö|oe)scht|entfern\w*)\b",
        re.IGNORECASE,
    )
    _NEGATION_RE = re.compile(r"\b(nicht|nein|kein\w*|no|not|don'?t|stop|niemals|never)\b", re.IGNORECASE)
    _AFFIRMATIVE_RE = re.compile(
        r"^(ja|jep|jup|jo|yes|yep|yeah|ok|okay|okey|klar|gerne|passt|go|los|mach(?:e|s| das| es| mal)?"
        r"|do it|bitte mach|ja bitte|yes please)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _is_clear_affirmative(cls, text) -> bool:
        """Deterministic check: is this reply an unambiguous go-ahead? Anything with a
        negation, or not starting with a known affirmative, counts as NOT confirmed -
        the gate then asks instead of acting (duplicate question beats wrong action)."""
        t = (text or "").strip().lower()
        if not t:
            return False
        if cls._NEGATION_RE.search(t):
            return False
        return bool(cls._AFFIRMATIVE_RE.match(t))

    def _proactive_reply_gate_decision(self, name, tool_instance, tool_args):
        """Gate (a): while this turn is a pickup of the user's reply to a tracked
        background question (_thinking_reply_context set), stored-state mutations and
        destructive delegation require a CLEAR affirmative reply - otherwise the tool
        call is answered with a confirm-style RESULT (invariant 4.1: never a mid-loop
        system message). Live incident: 'nein bitte nicht' was misread and the agent
        mutated an automation and delegated a file deletion, all unconfirmed."""
        try:
            if not getattr(self, "_thinking_reply_context", None):
                return None
            if not Config.get("proactive_reply_mutation_gate_enabled", True):
                return None
            if self._is_clear_affirmative(getattr(self, "_thinking_reply_user_text", None)):
                return None
            gated = name in self._PROACTIVE_REPLY_MUTATION_TOOLS
            if not gated and name in self._DELEGATION_TOOLS:
                try:
                    _args_text = " ".join(str(v) for v in (tool_args or {}).values())
                except Exception:
                    _args_text = ""
                gated = bool(self._DESTRUCTIVE_TEXT_RE.search(_args_text))
            if not gated:
                return None
            return (
                f"[CONFIRM REQUIRED] The user's message is a reply to a background question and is "
                f"NOT a clear confirmation. Do not change stored state or delegate destructive work "
                f"based on it. Answer in text instead: acknowledge their reply and ask ONE short "
                f"confirming question. '{name}' stays blocked for this turn."
            )
        except Exception:
            return None

    def _ask_first_gate_decision(self, name, tool_instance):
        """Gate (c): once the agent's final reply asked the user a blocking question,
        synthetic background turns (runner drain) may deliver results and read, but must
        not launch NEW write-level tools or delegations until a real user message
        arrives. Live incident: drain turns re-delegated file deletion twice AFTER the
        agent itself had asked 'Soll ich die Datei jetzt direkt loeschen?'."""
        try:
            if not getattr(self, "_synthetic_drain_turn", False):
                return None
            if not getattr(self, "_pending_user_question", None):
                return None
            if not Config.get("ask_first_drain_gate_enabled", True):
                return None
            _perm = getattr(tool_instance, "permission_level", "read")
            if _perm not in ("write", "dangerous") and name not in self._DELEGATION_TOOLS:
                return None
            return (
                f"[AWAITING USER] You asked the user a question and they have not answered yet. "
                f"Do not start new write actions or delegations until their reply arrives - "
                f"summarize the available results in text only. '{name}' is blocked in this "
                f"background turn."
            )
        except Exception:
            return None

    def get_live_session_subagents(self) -> list:
        """Session-scoped, heartbeat-verified list of GENUINELY running sub-agent tasks.

        The single source of truth for "is a sub-agent working for THIS chat right now" —
        shared by team_await, the SUB-AGENT ACTIVE prompt block, and the anti-re-delegation
        guard, so those gates can never disagree. Isolation: reads ONLY
        ipc.get_active_tasks_for_current_session() (session-filtered; active_tasks.json is
        one global file for all users, and task descriptions carry raw user text — never
        widen this to all sessions). NEVER read agent._async_subagent_tasks for this: that
        dict lives unkeyed on the shared per-worker agent and would leak across sessions.
        Anti-stuck: zombies are reaped first and stale heartbeats are filtered, so a crashed
        sub-agent can neither block "done" nor pin the prompt block. Fails open (empty list).

        Returns dicts: {task_id, agent_type, task_description, running_seconds}.
        """
        try:
            from vaf.core.subagent_ipc import get_ipc
            ipc = get_ipc()
            try:
                hb_timeout = int(self.config.get("subagent_heartbeat_timeout_seconds", 90) or 90)
            except Exception:
                hb_timeout = 90
            hb_timeout = max(20, min(600, hb_timeout))
            try:
                ipc.check_zombies(timeout_seconds=hb_timeout)
            except Exception:
                pass

            from datetime import datetime as _dt
            now = _dt.now()
            live = []
            for t in ipc.get_active_tasks_for_current_session():
                # Keep only genuinely-alive tasks (fresh heartbeat). Stale ones are
                # dead-but-not-yet-reaped and must NOT count.
                last_seen = getattr(t, "last_heartbeat", None) or getattr(t, "created_at", None)
                try:
                    hb_age = (now - _dt.fromisoformat(last_seen)).total_seconds() if last_seen else 0.0
                except Exception:
                    hb_age = 0.0
                if hb_age >= hb_timeout:
                    continue
                created = getattr(t, "created_at", None)
                try:
                    running = (now - _dt.fromisoformat(created)).total_seconds() if created else hb_age
                except Exception:
                    running = hb_age
                live.append({
                    "task_id": getattr(t, "task_id", "") or "",
                    "agent_type": getattr(t, "agent_type", "sub-agent") or "sub-agent",
                    "task_description": getattr(t, "task_description", "") or "",
                    "running_seconds": int(max(0, running)),
                })
            return live
        except Exception:
            return []

    def _detect_premature_done_claim(self, response_text):
        """Team-await: detect a reply that declares the overall task complete while a sub-agent is
        GENUINELY still running. Returns (blocked, [labels]). Anti-stuck by design: crashed/stale
        sub-agents are reaped first and never block; a finished sub-agent has already left the active
        list; any error fails open. Returns (False, []) when there is no completion claim (cheap, no
        IPC touched)."""
        try:
            text = (response_text or "").lower()
            if not text.strip():
                return (False, [])
            # Pre-filter: only proceed on a strong OVERALL-completion claim (multilingual).
            completion_markers = (
                "task complete", "task is complete", "task is done", "all done", "all set",
                "everything is done", "finished everything", "fully complete", "completed the task",
                "i have completed", "i've completed", "done with everything", "successfully completed",
                "erledigt", "alles erledigt", "fertig", "abgeschlossen", "vollständig abgeschlossen",
                "alles fertig", "habe alles", "ist fertig", "ist abgeschlossen",
            )
            if not any(m in text for m in completion_markers):
                return (False, [])

            live = self.get_live_session_subagents()
            if not live:
                return (False, [])

            # Narrowing (chat-while-subagent-runs): only bounce when the reply ALSO
            # references the delegated work. A bare "Erledigt!" about small talk while a
            # coder runs must pass — casual German is full of completion words, and the
            # bounce erases an already-streamed reply. A genuine premature claim ("der
            # Code ist fertig", "task is done") references the work and is still caught.
            _work_terms = {
                "task", "aufgabe", "auftrag", "projekt", "project", "arbeit",
                "agent", "sub-agent", "subagent", "code", "coding", "coder",
                "research", "recherche", "librarian", "dokument", "document", "bericht", "report",
            }
            for t in live:
                _work_terms.add(str(t.get("agent_type") or "").lower().replace("_agent", ""))
                for _w in str(t.get("task_description") or "").lower().split():
                    _w = _w.strip(".,:;!?()[]\"'")
                    if len(_w) > 5:
                        _work_terms.add(_w)
            if not any(_wt and _wt in text for _wt in _work_terms):
                return (False, [])

            labels = [f"{t['agent_type']} (running {t['running_seconds']}s)" for t in live]
            return (len(labels) > 0, labels)
        except Exception:
            return (False, [])

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
        # Cloud/API backend active: never load a local GGUF just for tokenization
        # (the API backend provides token counting / context window itself).
        if getattr(self, "api_backend", None):
            return None
        # Cloud provider selected but its api_backend is not built yet (e.g. right after first-run
        # setup, before the live provider-switch was applied): still NEVER download a local GGUF
        # for tokenization. This is the bug behind "a Veyllo key is set but a model is still
        # downloaded" - get_token_usage() runs at startup before the provider switch.
        if (getattr(self, "provider", "local") or "local") != "local":
            return None

        try:
            from llama_cpp import Llama  # type: ignore[import-untyped]
        except ImportError:
            # llama-cpp-python not installed - this is OK when using server mode
            # Return None and let caller handle it gracefully
            return None

        from vaf.cli.ui import UI

        # Tokenize ONLY with an already-present model. Do NOT trigger a download here - that is the
        # job of load_model() (which is provider-gated). Otherwise the very first get_token_usage()
        # at startup pulls a multi-GB GGUF before the user has even chosen a provider in setup.
        _mp = getattr(self, "model_path", None)
        if not _mp or not os.path.exists(_mp):
            return None

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
        """Helper to speak response via SpeechManager (host speakers; opt-in only)."""
        if not self._host_audio_allowed:
            return
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
        if not self._host_audio_allowed:
            return
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

    def _load_tools(self, builtin_only: bool = False, reload_modules: bool = False):
        """
        Scans vaf/tools/ folder and automatically loads all Tool classes.
        You can drop a new .py file there, and it works!

        builtin_only=True  -> only re-scan the built-in vaf/tools/*.py set (the
                              live-reload path); skips custom / entry-point / MCP
                              loading, which have their own reload methods and must
                              not be re-initialised (would re-connect MCP servers).
        reload_modules=True -> invalidate the import machinery's directory cache
                              first so a .py file dropped at runtime is discovered.
                              (Edits to an EXISTING built-in tool file still need a
                              restart; reloading modules would break class identity.)
        """
        import pkgutil
        import importlib
        import inspect
        from vaf.tools.base import BaseTool
        import vaf.tools

        # 1. Iterate over all files in vaf/tools/
        package_path = os.path.dirname(vaf.tools.__file__)
        if reload_modules:
            # A .py file dropped into vaf/tools/ at runtime is invisible to
            # pkgutil.iter_modules / import_module until the import machinery's
            # cached directory listing is invalidated.
            importlib.invalidate_caches()
        for _, name, _ in pkgutil.iter_modules([package_path]):
            try:
                # 2. Import the module (e.g. vaf.tools.calendar). A brand-new file
                #    dropped at runtime is imported fresh here and goes live; an
                #    already-imported module returns the cached object (so EDITS to
                #    an existing built-in tool file need a restart). We deliberately
                #    do NOT importlib.reload() existing modules: reloading
                #    vaf.tools.base would create a new BaseTool class object and
                #    break the issubclass() identity check for every other tool.
                try:
                    module = importlib.import_module(f"vaf.tools.{name}")
                except Exception as e:
                    if "github" in name.lower():
                        print(f"[WARN] GitHub tools module '{name}' failed to load (is PyGithub installed?): {e}")
                    else:
                        print(f"[ERROR] Failed to import tool module {name}: {e}")
                    continue
                
                # 3. Find classes that inherit from BaseTool
                for _, obj in inspect.getmembers(module):
                    if inspect.isclass(obj) and issubclass(obj, BaseTool) and obj is not BaseTool:
                        # 4. Register the tool (Filter primitives to force Sub-Agent usage)
                        try:
                            instance = obj()
                        except Exception as e:
                            print(f"[ERROR] Failed to instantiate tool {getattr(obj, 'name', str(obj))}: {e}")
                            continue
                        
                        # Tools intentionally NOT exposed to the Main Agent.
                        # read_file and list_files are intentionally INCLUDED — the main agent
                        # needs them to verify work, answer user questions about files, etc.
                        # librarian_agent is for heavy analysis tasks, not simple file reads.
                        # write_file is intentionally INCLUDED (since the blue378604 audit): the
                        # python_sandbox persistence guard redirects the model to write_file, and
                        # simple single-file artifacts (svg/html/txt) need no coding_agent run.
                        # Main-agent calls get session workspace + per-user jail via execute_tool.
                        MAIN_AGENT_EXCLUDED_TOOLS = [
                            "move_file",      # Move/rename (delegate to sub-agents)
                            "folder_size",   # Deterministic sizing (prefer via librarian_agent)
                            "bash",           # Shell commands (for build/test)
                            "codesearch",     # Code navigation
                            "batch",          # Parallel operations
                            "save_thinking_suggestion",  # Removed: agent asks user via main_messenger when unsure
                        ]
                        
                        # Check if tool is coder-only (built-in or marked with coder_only=True)
                        is_coder_only = (
                            instance.name in MAIN_AGENT_EXCLUDED_TOOLS or 
                            getattr(instance, 'coder_only', False)
                        )
                        # Thinking-mode tool policy (read context + propose — do not mutate/act directly):
                        #  - no Git (VAF is the user's project), no memory_save (read memory, don't write),
                        #  - no update_user_identity (propose profile changes via save_thinking_suggestion),
                        #  - no set_timer / direct user-facing scheduled actions (propose via ask_user; the
                        #    main agent carries it out after the user confirms),
                        #  - no write_file (a propose-only background run must not create files).
                        # Registration gates key on the per-instance run kind (see __init__):
                        # env vars race across threads - a chat agent constructed during a
                        # thinking window would otherwise gain thinking tools and silently
                        # lose git/memory_save/set_timer/write_file for its whole lifetime.
                        _rk_thinking = self._run_kind == "thinking"
                        _rk_automation = self._run_kind == "automation"
                        if _rk_thinking:
                            if instance.name in ("git_add_commit", "git_status", "git_log", "memory_save",
                                                 "update_user_identity", "set_timer", "schedule_reminder",
                                                 "write_file"):
                                continue
                        # thinking_done: ONLY in thinking mode — the main agent must never call this
                        if instance.name == "thinking_done":
                            if not _rk_thinking:
                                continue
                            self.tools[instance.name] = instance
                            continue
                        # ask_user: the explicit, tracked channel to contact the user (a clean message; the
                        # chain-of-thought never leaks; status is tracked). Available in thinking mode AND in
                        # a scheduled automation — a background automation that hits a genuine blocker hands
                        # off via this same channel (it then carries a handoff bundle; see ask_user routing).
                        if instance.name == "ask_user":
                            if not (_rk_thinking or _rk_automation):
                                continue
                            self.tools[instance.name] = instance
                            continue
                        # thinking_workspace_* tools: ONLY in thinking mode
                        if instance.name.startswith("thinking_workspace_"):
                            if not _rk_thinking:
                                continue
                            self.tools[instance.name] = instance
                            continue

                        # save_thinking_suggestion: available in thinking mode for proactive suggestions
                        if instance.name == "save_thinking_suggestion":
                            if _rk_thinking:
                                self.tools[instance.name] = instance
                                continue
                        # thinking_note_add: available in both modes — agent can save notes during
                        # normal chat for the next thinking pass (e.g. "user confirmed X, don't ask again")
                        if instance.name == "thinking_note_add":
                            self.tools[instance.name] = instance
                            continue
                        if is_coder_only:
                            continue
                        
                        # When context is very small (<= 4096), exclude automation management tools to save space.
                        # Automation agent gets the same tools as main agent (no exclusions).
                        is_in_automation = self._run_kind == "automation"
                        n_ctx = self.config.get("n_ctx", 8192)
                        if not is_in_automation and n_ctx <= 4096:
                            SMALL_CTX_EXCLUDED_TOOLS = [
                                "update_automation",
                                "delete_automation",
                                "list_automations",
                                "read_automation",
                                "restore_automation",
                                "list_trash",
                            ]
                            if instance.name in SMALL_CTX_EXCLUDED_TOOLS:
                                continue
                        
                        self.tools[instance.name] = instance
                        # Debug info (only if verbose)
                        # print(f"Loaded tool: {instance.name}")
            except Exception as e:
                print(f"[ERROR] Failed to load tool {name}: {e}")
                pass # Still ignore for stability, but report error
        
        # Live-reload path: only the built-in scan above is needed. Custom /
        # entry-point / MCP tools have their own reload methods and must NOT be
        # re-initialised here (re-running _load_mcp_tools would re-connect servers).
        if builtin_only:
            for tool in self.tools.values():
                if hasattr(tool, "available_tools"):
                    try:
                        tool.available_tools = self.tools
                    except Exception:
                        pass
            return

        # ── Custom tools (user-uploaded via WebUI) ────────────────────────────
        # Loaded from Platform.data_dir()/custom_tools/ so they survive package
        # updates without being overwritten.  Each tool is a plain .py file that
        # contains a BaseTool subclass — same contract as built-in tools.
        # Admin-only at upload time; visibility per user is filtered at the WS
        # level (get_tools handler) not here, so the agent always has the full set.
        self._load_custom_tools()

        # ── Entry-point tools (external tools shipped by third-party pip packages) ─────────────
        # Discovered via the "vaf.tools" entry-point group so a developer can
        # `pip install` a package that extends VAF without touching the core.
        self._load_entry_point_tools()

        # ── MCP tools (external servers from mcp_servers.json, registered as native tools) ──────
        self._load_mcp_tools()

        # Provide tool registry to tools that expect it (e.g., list_tools)
        for tool in self.tools.values():
            if hasattr(tool, "available_tools"):
                try:
                    tool.available_tools = self.tools
                except Exception:
                    pass

        # Track active async sub-agent tasks
        self._async_subagent_tasks = {}  # task_id -> {"agent_type": str, "task": str, "started_at": datetime}

    def _load_custom_tools(self) -> None:
        """
        Load all custom tools registered in custom_tools_registry into self.tools.

        Called once at startup from _load_tools().  For live updates (after the
        admin uploads / deletes a tool via the WebUI) use reload_custom_tools().

        Custom tools follow the same filtering rules as built-in tools:
          - coder_only=True → skip (custom tools are always for the main agent)
          - thinking_done / thinking_workspace_* → skip (custom tools are never
            thinking-mode internals)
        The per-user visibility filter is applied later at the WebSocket layer
        (get_tools handler) so the agent instance always holds the full set.
        """
        try:
            from vaf.core.custom_tools_registry import (
                load_manifest,
                load_custom_tool_class,
            )
        except Exception as exc:
            print(f"[WARN] custom_tools_registry not available: {exc}")
            return

        manifest = load_manifest()
        for tool_name in manifest.get("tools", {}).keys():
            try:
                cls = load_custom_tool_class(tool_name)
                if cls is None:
                    continue
                instance = cls()
                # Skip if somehow a custom tool is marked coder-only
                if getattr(instance, "coder_only", False):
                    continue
                self.tools[instance.name] = instance
            except Exception as exc:
                print(f"[ERROR] Failed to load custom tool '{tool_name}': {exc}")

    def _load_entry_point_tools(self) -> None:
        """
        Discover external tools published by third-party pip packages via the
        ``vaf.tools`` entry-point group, and register each into self.tools.

        This lets developers ship tools as installable packages, e.g. in their
        pyproject.toml / setup.py::

            [options.entry_points]
            vaf.tools =
                my_tool = my_pkg.tools:MyTool

        Each entry point must resolve to a BaseTool subclass. Same filtering as
        custom tools: coder_only=True is skipped (entry-point tools target the
        main agent). Defensive throughout — a broken package never breaks
        startup, and an empty group is a clean no-op.
        """
        try:
            from importlib.metadata import entry_points
            from vaf.tools.base import BaseTool
        except Exception as exc:
            print(f"[WARN] entry-point tool discovery unavailable: {exc}")
            return

        try:
            eps = entry_points(group="vaf.tools")
        except TypeError:
            # Selectable-API fallback: the group= keyword was added in 3.10.
            # The project floor is 3.10, but stay defensive on older interpreters.
            eps = entry_points().get("vaf.tools", [])
        except Exception as exc:
            print(f"[WARN] could not query 'vaf.tools' entry points: {exc}")
            return

        for ep in eps:
            try:
                cls = ep.load()
                if not (isinstance(cls, type) and issubclass(cls, BaseTool)):
                    print(f"[WARN] entry-point tool '{ep.name}' is not a BaseTool subclass; skipped")
                    continue
                instance = cls()
                # Entry-point tools target the main agent; skip coder-only ones.
                if getattr(instance, "coder_only", False):
                    continue
                self.tools[instance.name] = instance
            except Exception as exc:
                print(f"[ERROR] Failed to load entry-point tool '{getattr(ep, 'name', ep)}': {exc}")

    def _load_mcp_tools(self) -> None:
        """Discover the tools of the MCP servers in mcp_servers.json and register each as a native
        tool (``mcp_<server>_<tool>``). Eager + parallel with a per-server timeout; a slow / hung /
        misconfigured server is skipped and never blocks startup. Gated by mcp_native_tools_enabled.
        The raw ``mcp_call`` tool is unaffected (it stays the low-level path)."""
        self._mcp_tool_names = getattr(self, "_mcp_tool_names", set())
        self._mcp_server_status = getattr(self, "_mcp_server_status", {})
        try:
            if not bool(self.config.get("mcp_native_tools_enabled", True)):
                self._mcp_server_status = {}
                return
            from vaf.core.mcp_registry import discover_mcp_tools
            timeout = float(self.config.get("mcp_discovery_timeout_seconds", 5) or 5)
            tools, status = discover_mcp_tools(timeout_seconds=timeout)
            self._mcp_server_status = status
        except Exception as exc:
            print(f"[WARN] MCP tool discovery skipped: {exc}")
            return
        for name, instance in tools.items():
            # Never overwrite a native/custom tool with the same name.
            if name in self.tools and name not in self._mcp_tool_names:
                continue
            self.tools[name] = instance
            self._mcp_tool_names.add(name)

    def reload_mcp_tools(self) -> None:
        """Hot-reload MCP tools after mcp_servers.json changes: drop the previously-registered MCP
        tools (tracked precisely, so native/custom tools are never touched) and re-discover."""
        for name in list(getattr(self, "_mcp_tool_names", set())):
            self.tools.pop(name, None)
        self._mcp_tool_names = set()
        self._load_mcp_tools()
        # Refresh the registry reference for discovery tools (list_tools etc.)
        for tool in self.tools.values():
            if hasattr(tool, "available_tools"):
                try:
                    tool.available_tools = self.tools
                except Exception:
                    pass

    def reload_custom_tools(self) -> None:
        """
        Hot-reload custom tools without restarting the agent.

        Removes all previously loaded custom tool entries from self.tools, then
        re-loads every tool currently listed in the manifest.  Called by the
        WebSocket handlers in web_server.py after create / update / delete
        operations so the live agent immediately reflects the change.

        Thread safety: self.tools is a plain dict; mutations here happen on the
        asyncio event loop thread (the WS handler awaits the FastAPI coroutine
        which calls this synchronously), so no extra lock is needed.
        """
        try:
            from vaf.core.custom_tools_registry import (
                get_all_custom_tool_names,
                load_manifest,
            )
        except Exception as exc:
            print(f"[WARN] reload_custom_tools: registry unavailable: {exc}")
            return

        # Remove all custom tool entries that were previously registered.
        # We identify them by cross-referencing the current manifest names —
        # this avoids accidentally removing built-in tools with the same name.
        all_custom_names = set(get_all_custom_tool_names())
        for name in list(self.tools.keys()):
            if name in all_custom_names:
                del self.tools[name]

        # Re-load from disk (fresh importlib call — picks up source changes too)
        self._load_custom_tools()

        # Refresh the tool registry reference for discovery tools (list_tools etc.)
        for tool in self.tools.values():
            if hasattr(tool, "available_tools"):
                try:
                    tool.available_tools = self.tools
                except Exception:
                    pass

        print(f"[INFO] reload_custom_tools: active custom tools = {list(all_custom_names)}")

    def reload_builtin_tools(self) -> None:
        """Hot-reload built-in tools (vaf/tools/*.py) without a restart: re-scan the
        package so a newly-dropped tool file goes live. Custom / entry-point / MCP
        tools are untouched (they own their reload paths). EDITS to an existing
        built-in tool file, and DELETIONS, still need a restart to take effect."""
        self._load_tools(builtin_only=True, reload_modules=True)

    def _tools_fs_signature(self) -> tuple:
        """Cheap fingerprint of the tool source dirs: built-in vaf/tools/*.py plus
        the custom-tools dir and its manifest.json. Changes when a tool file is
        added, edited, or removed (the manifest catches custom add/remove). A few
        stat() calls; the basis for watcher-free, stdlib-only tool hot-reload."""
        import os as _os
        parts = []
        # Built-in tools
        try:
            import vaf.tools as _vt
            bdir = _os.path.dirname(_vt.__file__)
            for fn in sorted(_os.listdir(bdir)):
                if fn.endswith(".py"):
                    try:
                        st = _os.stat(_os.path.join(bdir, fn))
                    except OSError:
                        continue
                    parts.append(("b/" + fn, st.st_mtime_ns, st.st_size))
        except Exception:
            pass
        # Custom tools (files + manifest)
        try:
            from vaf.core.custom_tools_registry import get_custom_tools_dir
            cdir = get_custom_tools_dir()
            for fn in sorted(_os.listdir(cdir)):
                if fn.endswith(".py") or fn == "manifest.json":
                    try:
                        st = _os.stat(_os.path.join(cdir, fn))
                    except OSError:
                        continue
                    parts.append(("c/" + fn, st.st_mtime_ns, st.st_size))
        except Exception:
            pass
        return tuple(parts)

    def _maybe_refresh_dynamic_tools(self) -> None:
        """Per-turn, stdlib-only freshness check for TOOL files. If the built-in
        tools dir, the custom-tools dir, or the custom-tools manifest changed on
        disk since the last check, hot-reload so a newly created tool is live
        without a restart — including one written by another process (the
        in-process create_custom_tool already reloads its own process). Throttled
        to at most once/second; no background thread, no watcher."""
        try:
            import time as _t
            now = _t.monotonic()
            if now - getattr(self, "_tools_fs_last_check", 0.0) < 1.0:
                return
            self._tools_fs_last_check = now
            sig = self._tools_fs_signature()
            if not hasattr(self, "_tools_fs_sig"):
                # First turn: startup already loaded everything, so just record
                # the baseline; don't reload.
                self._tools_fs_sig = sig
                return
            if sig == self._tools_fs_sig:
                return
            self._tools_fs_sig = sig
            try:
                self.reload_builtin_tools()
            except Exception as exc:
                print(f"[WARN] refresh tools: built-in reload failed: {exc}")
            try:
                self.reload_custom_tools()
            except Exception as exc:
                print(f"[WARN] refresh tools: custom reload failed: {exc}")
        except Exception:
            pass

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
    
    def _check_subagent_results(self, all_sessions: bool = False) -> list:
        """
        Check for completed sub-agent results.
        Called periodically during chat to process async results.

        Args:
            all_sessions: If False (default, the in-chat caller), only the CURRENT
                session's results are returned, since they are injected into the current
                conversation. The background headless runner passes True: it routes each
                result to its own session via load_session_context, so it must drain
                EVERY session — otherwise a completion whose session is not the runner's
                (stale) current session would be missed by the push and only surface on
                that session's next turn.

        Returns:
            List of completed SubAgentTask objects
        """
        try:
            from vaf.core.subagent_ipc import get_ipc, get_current_session_id
            ipc = get_ipc()
            
            # Cleanup stale tasks (crashed sub-agents)
            # None -> the config default applies (subagent_timeout_minutes, 120). The old
            # hardcoded 30 meant every chat turn could force-expire a long coder run —
            # fatal once the user chats WHILE a sub-agent works.
            ipc.cleanup_stale_active_tasks(max_age_minutes=None)
            
            # In-chat caller: only the CURRENT session's results (injected into the
            # current conversation). Runner (all_sessions=True): drain every session and
            # route each by its own session_id — the current-session value is unreliable
            # when the runner is idle, which is exactly when a push arrives.
            current_session = None if all_sessions else get_current_session_id()
            
            # 0. Liveness Check (Detect Crashed Sub-Agents)
            # 20s is too aggressive on some systems (e.g. browser launch / heavy tool finalization)
            # and can produce false positives. Keep configurable with a safe default.
            hb_timeout = int(self.config.get("subagent_heartbeat_timeout_seconds", 90) or 90)
            hb_timeout = max(20, min(600, hb_timeout))
            ipc.check_zombies(timeout_seconds=hb_timeout)
            
            results = ipc.get_pending_results(session_id=current_session)
            # Never drain a result an in-process workflow engine loop is actively awaiting
            # and will consume itself (engine._await_subagent). Without this, the all-sessions
            # runner drain would steal a step result, timing out a successful step. The engine
            # consumes via consume_result() directly, unaffected by this filter.
            from vaf.core.subagent_ipc import is_engine_owned
            results = [t for t in results if not is_engine_owned(getattr(t, "task_id", None))]
            return results
        except Exception:
            return []

    def _validate_subagent_result_heuristic(
        self, user_intent: str, task_description: str, result: str, agent_type: str
    ) -> Tuple[bool, Optional[str]]:
        """Fallback heuristic when LLM validation fails."""
        if not user_intent or not result:
            return True, None

        # Research/document agents with clear completion signals → always accept
        if agent_type in ("research_agent", "document_agent"):
            rl = result.lower()
            if any(x in rl for x in ["task complete", "report has been saved", "saved successfully", "open in the document editor"]):
                return True, None

        intent_lower = user_intent.lower()
        result_lower = result.lower()

        user_folder = None
        if "downloads" in intent_lower or "im downloads" in intent_lower:
            user_folder = "downloads"
        elif "documents" in intent_lower or "dokumente" in intent_lower:
            user_folder = "documents"
        elif "desktop" in intent_lower:
            user_folder = "desktop"

        if user_folder == "downloads":
            if "documents" in result_lower and "downloads" not in result_lower:
                return False, f"Rename the file in Downloads (as requested), not in Documents. {task_description}"
        if user_folder == "documents":
            if "downloads" in result_lower and "documents" not in result_lower:
                return False, f"Rename the file in Documents (as requested), not in Downloads. {task_description}"

        rename_intent = any(x in intent_lower for x in ["umbenennen", "umbenenn", "rename"])
        if rename_intent:
            rename_success = any(x in result_lower for x in ["moved", "umbenannt", "renamed", "→"])
            listing_indicator = any(x in result_lower for x in [
                "contains", "enthält", "files", "dateien", "ordner enthält",
                "folder contains", "dateien im ordner"
            ])
            if listing_indicator and not rename_success:
                return False, f"Rename the file as requested. Do NOT list the folder. {task_description}"

        send_intent = any(x in intent_lower for x in ["send", "schick", "schicken"])
        if send_intent:
            # Channel-agnostic success phrases. The old list contained bare platform
            # names ("telegram", "mail"), which (a) welded the heuristic to two
            # platforms and (b) counted FAILURES as success ("Failed to send Telegram
            # message" contains "telegram").
            send_success = any(x in result_lower for x in [
                "sent to the user", "message sent", "gesendet", "delivered",
                "email sent", "mail sent",
            ])
            if not send_success and len(result_lower) > 50:
                return False, f"Send the file as requested. {task_description}"

        return True, None

    def _extract_subagent_goal(self, name: str, args: dict) -> str:
        """Extract the delegation goal from sub-agent tool args."""
        args = args or {}
        if name == "librarian_agent":
            return (args.get("task") or "").strip()
        if name == "research_agent":
            return (args.get("topic") or "").strip()
        if name == "document_agent":
            return (args.get("task") or "").strip()
        if name == "coding_agent":
            task = (args.get("task") or "").strip()
            proj = (args.get("project_path") or "").strip()
            if task and proj:
                return f"{proj}: {task}"
            return task or proj
        return ""

    def _run_validation_llm(self, messages: list, max_tokens: int = 150) -> str:
        """One validation completion against whichever backend is active (server/api/local).
        Shared by the sub-agent validator and the per-step workflow validator. Returns the
        response content ("" if the backend produced nothing). Raises on backend errors so the
        caller can fall back to its heuristic."""
        if self.use_server:
            res = requests.post(
                "http://127.0.0.1:8080/v1/chat/completions",
                json={"messages": messages, "max_tokens": max_tokens, "temperature": 0, "stream": False},
                timeout=30,
            ).json()
            return (res.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if self.api_backend:
            chunks = list(
                self.api_backend.chat_completion(
                    messages=messages, max_tokens=max_tokens, temperature=0, stream=False
                )
            )
            return "".join(c if isinstance(c, str) else str(c) for c in chunks if c)
        if self.llm:
            output = self.llm.create_chat_completion(
                messages=messages, max_tokens=max_tokens, temperature=0
            )
            return (output.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return ""

    def _reply_needs_user(self, reply: str) -> bool:
        """Stage-3 brake for pending-task auto-continue: does this final reply ask the user a
        question / request input it NEEDS before it can keep working? The foreground Web UI has no
        tool signal for "I'm asking the user" (the question is plain text), so we classify the text.

        Uses a tiny validation LLM when available — robust to phrasing, including a real question
        that carries no "?" (e.g. "Sag mir bitte, welche Datei gemeint ist."). Falls back to a cheap
        heuristic (last line contains "?") when the classifier is disabled, no backend is available,
        or the call errors, so the main loop can never break here."""
        text = (reply or "").strip()
        if not text:
            return False

        def _heuristic() -> bool:
            last_line = text.splitlines()[-1] if text else ""
            return "?" in last_line

        try:
            from vaf.core.config import Config as _Cfg
            if not _Cfg.get("autocontinue_question_classifier_enabled", True):
                return _heuristic()
        except Exception:
            return _heuristic()

        if not (getattr(self, "use_server", False) or getattr(self, "api_backend", None)
                or getattr(self, "llm", None)):
            return _heuristic()

        prompt = (
            "You decide ONE thing about an assistant's reply: does it ask the user a question or "
            "request input/a decision that the assistant NEEDS before it can continue its task?\n"
            "Answer YES only if the assistant is genuinely blocked waiting on the user. A rhetorical "
            "question, a status update, or a courtesy line like 'let me know if you need anything' is "
            "NOT blocking -> answer NO.\n"
            "Reply with exactly one word: YES or NO.\n\n"
            f"ASSISTANT REPLY:\n{text[:1500]}"
        )
        try:
            out = self._run_validation_llm(
                [{"role": "user", "content": prompt}], max_tokens=8
            ).strip().lower()
        except Exception:
            return _heuristic()
        if "yes" in out:
            return True
        if "no" in out:
            return False
        return _heuristic()  # unparseable output -> safe fallback

    def _resolve_user_intent(self) -> str:
        """Best-effort original user intent: delegation intent (written before a sub-agent call),
        then persisted user intent, then the last user message in history. Used by both the
        sub-agent validator and the per-step workflow validator."""
        try:
            if getattr(self, "main_persistence", None):
                delegation = self.main_persistence.get_subagent_delegation_intent()
                if delegation and delegation.get("intent"):
                    return delegation["intent"]
        except Exception:
            pass
        try:
            if getattr(self, "main_persistence", None):
                intent = self.main_persistence.get_user_intent() or ""
                if intent:
                    return intent
        except Exception:
            pass
        if getattr(self, "history", None):
            for msg in reversed(self.history):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    return msg.get("content", "") or ""
        return ""

    def _validate_step_output(
        self, goal: str, result: str, tool: str, user_intent: str = ""
    ) -> Tuple[bool, Optional[str]]:
        """
        Per-workflow-step validation: does this step's OUTPUT fulfil the step's GOAL?

        Unlike _validate_subagent_result_with_llm, this has NO lenient "report saved → accept"
        fast-path — that one would wave through an empty/wrong document just because the tool
        reported success. Here the actual content is judged against the goal. Returns
        (fulfilled, retry_hint). Any failure to decide → (True, None) so a flaky validator can
        never break a workflow.
        """
        result = (result or "").strip()
        goal = (goal or "").strip()
        if not result or not goal:
            return True, None

        # Coding output with an explicit completion signal is trusted (the local model tends to
        # false-negative on perfectly valid code), mirroring the sub-agent validator.
        if tool == "coding_agent" and "[vaf_coding_agent_status: complete]" in result.lower():
            return True, None

        if not (getattr(self, "use_server", False) or getattr(self, "api_backend", None) or getattr(self, "llm", None)):
            return True, None  # no backend to validate with → accept

        prompt = (
            "You are a strict validator for ONE step of a multi-step workflow.\n"
            "Judge ONLY whether the STEP OUTPUT actually fulfils the STEP GOAL — by its CONTENT, "
            "not by whether a tool merely reported success.\n\n"
            f"STEP GOAL: {goal[:600]}\n"
            f"OVERALL USER INTENT: {(user_intent or '')[:400]}\n"
            f"STEP OUTPUT: {result[:1200]}\n\n"
            "Reply with EXACTLY one of:\n"
            "- </true> if the output fulfils the goal\n"
            "- </false> if it does NOT (empty, wrong content, missing the requested data, off-topic)\n\n"
            "If </false>, add on the next line: RETRY: [one concrete instruction to fix it]"
        )
        stricter_prompt = (
            "Reply with EXACTLY </true> or </false>. Nothing else.\n"
            f"GOAL: {goal[:300]}\n"
            f"OUTPUT: {result[:500]}\n"
            "Does the output fulfil the goal? </true> or </false>"
        )

        for attempt in range(3):
            try:
                content = self._run_validation_llm(
                    [{"role": "user", "content": stricter_prompt if attempt > 0 else prompt}],
                    max_tokens=150,
                )
            except Exception:
                return True, None  # backend error → never block the workflow
            resp = (content or "").strip().lower()
            if "</true>" in resp:
                return True, None
            if "</false>" in resp:
                retry_hint = None
                for line in (content or "").splitlines():
                    if "retry:" in line.lower():
                        retry_hint = line.split(":", 1)[-1].strip()
                        break
                return False, (retry_hint or f"The output did not fulfil the goal: {goal[:200]}")
        # No decisive answer after retries → accept (don't burn workflow retries on indecision).
        return True, None

    def _validate_subagent_result_with_llm(
        self, user_intent: str, task_description: str, result: str, agent_type: str
    ) -> Tuple[bool, Optional[str]]:
        """
        LLM-based validation: does the sub-agent result fulfill the user's intent?
        LLM must output </true> or </false> to exit. Max 5 retries if no tag.
        Returns (fulfilled, retry_instruction). Falls back to heuristic if LLM fails.
        """
        if not user_intent or not result:
            return True, None

        # Fast path: clear success indicators – avoid LLM false negatives
        result_lower = result.lower()

        # Coding agent with explicit COMPLETE signal → always accept immediately.
        # Do NOT run LLM validation: the local model often outputs </false> for
        # perfectly valid coding results, triggering a silent retry loop.
        if agent_type == "coding_agent" and "[vaf_coding_agent_status: complete]" in result_lower:
            return True, None

        # Workflow agents always produce a complete deliverable — skip LLM validation.
        # The workflow result only says "completed, saved to /path" without HTML content,
        # so the validator would incorrectly return </false> and trigger a retry loop
        # that spawns a redundant coding_agent run.
        if agent_type.startswith("workflow:"):
            return True, None

        # Coding agent (any result): skip validation to avoid false retry loops.
        if agent_type == "coding_agent":
            return True, None

        # Research/document agents: report saved + opened in editor → always accept
        if agent_type in ("research_agent", "document_agent"):
            if any(x in result_lower for x in ["task complete", "report has been saved", "saved successfully", "open in the document editor"]):
                return True, None

        if any(x in user_intent.lower() for x in ["umbenennen", "umbenenn", "rename"]):
            if any(x in result_lower for x in ["moved", "renamed", "umbenannt", "→"]):
                return True, None

        if not (getattr(self, "use_server", False) or getattr(self, "api_backend", None) or getattr(self, "llm", None)):
            return self._validate_subagent_result_heuristic(
                user_intent, task_description, result, agent_type
            )

        prompt = (
            "You are a validator. Does this sub-agent result fulfill the USER's intent?\n\n"
            f"USER INTENT: {user_intent[:500]}\n"
            f"TASK SENT TO SUB-AGENT: {(task_description or '')[:300]}\n"
            f"SUB-AGENT RESULT: {result[:800]}\n\n"
            "Reply with EXACTLY one of:\n"
            "- </true> if the result fulfills the user's intent\n"
            "- </false> if it does NOT (wrong folder, wrong action, incomplete, etc.)\n\n"
            "If </false>, add on the next line: RETRY: [explicit task for retry]\n"
            "Example: RETRY: Rename file 26-B001-105272426-97758570.PDF in Downloads to Bundesanzeiger_Rechnung.pdf"
        )

        stricter_prompt = (
            "You MUST reply with exactly </true> or </false>. Nothing else. No explanation.\n"
            f"USER INTENT: {user_intent[:300]}\n"
            f"RESULT: {result[:400]}\n"
            "Does the result fulfill the intent? </true> or </false>"
        )

        max_llm_retries = 5
        for attempt in range(max_llm_retries):
            content = ""
            try:
                temp_history = [{"role": "user", "content": stricter_prompt if attempt > 0 else prompt}]
                content = self._run_validation_llm(temp_history, max_tokens=150)
            except Exception as e:
                from vaf.cli.ui import UI
                UI.event("Debug", f"Sub-agent validation LLM call failed: {e}", style="dim")
                return self._validate_subagent_result_heuristic(
                    user_intent, task_description, result, agent_type
                )

            resp = (content or "").strip().lower()
            if "</true>" in resp:
                return True, None
            if "</false>" in resp:
                retry_instruction = None
                for line in (content or "").splitlines():
                    if "retry:" in line.lower():
                        retry_instruction = line.split(":", 1)[-1].strip()
                        break
                if not retry_instruction:
                    # Use intent from delegation file (this agent's own slot), not task_description
                    if hasattr(self, "main_persistence") and self.main_persistence:
                        try:
                            delegation = self.main_persistence.get_subagent_delegation_intent(agent_type)
                            if delegation and delegation.get("intent"):
                                retry_instruction = delegation["intent"]
                        except Exception:
                            pass
                if not retry_instruction:
                    retry_instruction = task_description or "Retry the task"
                return False, retry_instruction

        return self._validate_subagent_result_heuristic(
            user_intent, task_description, result, agent_type
        )

    def _process_subagent_result(self, task) -> bool:
        """
        Process a completed sub-agent result and add it to the conversation.
        
        Args:
            task: SubAgentTask object with the result
        
        Returns:
            True if validation failed and a retry was instructed (caller should prompt agent to retry).
        """
        needs_retry = False
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

                # Research/document agent: inject explicit stop instruction
                if task.agent_type in ("research_agent", "document_agent"):
                    task_result_msg += (
                        "\n\n**IMPORTANT INSTRUCTION:** The report is COMPLETE and already OPEN "
                        "in the Document Editor. Do NOT use any tools (no search_tools, no "
                        "librarian_agent, no read_file). Reply in the user's language with a "
                        "**short summary** (2–5 sentences or a few bullets) of what the report "
                        "covers, using the topic, section titles, and counts above — you are "
                        "**not** reading the HTML file. Then confirm the full text is in the "
                        "Document Editor and offer small adjustments if needed."
                    )

                # Validate: does the result fulfill the user's intent? (LLM-based, heuristic fallback)
                # ONLY the delegation-intent slot written at spawn time is a trustworthy
                # reference. Deliberately NO fallback to the persisted intent or the last
                # user message: with chat-while-subagent-runs, those are casual small talk
                # by the time the result lands — validating the coder result against "wie
                # geht's?" produced forced-retry storms (up to 20). Missing intent → empty
                # user_intent → the validator returns fulfilled immediately (skip).
                user_intent = ""
                try:
                    if hasattr(self, "main_persistence") and self.main_persistence:
                        delegation = self.main_persistence.get_subagent_delegation_intent(task.agent_type)
                        if delegation and delegation.get("intent"):
                            user_intent = delegation["intent"]
                except Exception:
                    pass

                fulfilled, retry_instruction = self._validate_subagent_result_with_llm(
                    user_intent, task.task_description, task.result, task.agent_type
                )

                if fulfilled:
                    if hasattr(self, 'main_persistence') and self.main_persistence:
                        try:
                            self.main_persistence.reset_validation_retry_count()
                        except Exception:
                            pass
                elif retry_instruction:
                    count = 0
                    if hasattr(self, 'main_persistence') and self.main_persistence:
                        try:
                            count = self.main_persistence.increment_validation_retry_count()
                        except Exception:
                            pass

                    if count >= 20:
                        UI.event("Warning", "Sub-agent validation: 20 retries reached. Informing user.", style="yellow")
                        task_result_msg = (
                            f"**FINAL RESULT (Task is DONE):**\n{task.result}\n\n"
                            f"**You have tried 20 times.** The sub-agent result still does not fulfill the user's request.\n"
                            f"**Action:** Tell the user the actual status. Do NOT retry again. Summarize what was attempted and what the current state is."
                        )
                        if hasattr(self, 'main_persistence') and self.main_persistence:
                            try:
                                self.main_persistence.reset_validation_retry_count()
                            except Exception:
                                pass
                    else:
                        needs_retry = True
                        UI.event("Warning", f"Sub-agent result does not fulfill user intent. Retry {count}/20.", style="yellow")
                        # Get intent/goal from delegation (source of truth; this agent's own slot)
                        dlg_intent = user_intent or ""
                        dlg_goal = (task.task_description or "").strip()
                        if hasattr(self, "main_persistence") and self.main_persistence:
                            try:
                                d = self.main_persistence.get_subagent_delegation_intent(task.agent_type)
                                if d:
                                    dlg_intent = d.get("intent", dlg_intent) or dlg_intent
                                    dlg_goal = d.get("goal", dlg_goal) or dlg_goal
                            except Exception:
                                pass
                        task_result_msg = (
                            f"**Sub-agent did NOT fulfill the user's request.** (attempt {count}/20)\n\n"
                            f"**Sub-agent returned:**\n{task.result}\n\n"
                            f"**User intent (from delegation):** {dlg_intent}\n"
                            f"**Delegation goal (what we sent):** {dlg_goal}\n\n"
                            f"**Your task:** Resolve this. Do NOT blindly retry the same call. "
                            f"Use the information above, choose appropriate tools (e.g. librarian_agent with a different/more explicit task, move_file, find_files), and actually fulfill the user's intent."
                        )

            # For research/document agents the report is already open in the
            # Document Editor — the main agent must NOT try to read or re-open it.
            if task.agent_type in ("research_agent", "document_agent"):
                self.history.append({
                    "role": "system",
                    "content": (
                        f"🧠 **Sub-Agent Task Finished**\n"
                        f"Agent: {task.agent_type} (Task ID: {task.task_id[:8]})\n\n"
                        f"{task_result_msg}\n\n"
                        f"--- END OF SUB-AGENT OUTPUT ---\n\n"
                        f"⚠️ **INSTRUCTION:** The report is COMPLETE and already OPEN in the Document Editor. "
                        f"Do NOT call any tools (no read_file, no librarian_agent, no search_tools). "
                        f"Give a **brief content summary** in the user's language from the metadata "
                        f"(topic, sections, word count, sources) in the message above, then confirm "
                        f"the editor shows the full report and offer edits if they want."
                    )
                })
            elif task.agent_type == "coding_agent" or task.agent_type.startswith("workflow:"):
                # Coding agent / workflow: the deliverable is already on disk.
                # Do NOT tell the main agent to "fulfill the original intent" — that
                # causes it to call coding_agent again, creating a duplicate run and
                # a second terminal window.
                file_paths = re.findall(
                    r'(?:Saved to|Output|File|Path|Ausgabe|Datei|saved to):\s*([^\n]+\.(?:html?|pdf|docx?|txt|md|json|csv|xlsx?|py|js|ts))',
                    task.result,
                    re.IGNORECASE
                )
                file_hint = ""
                if file_paths:
                    cleaned_paths = [re.sub(r'\x1b\[[0-9;]*m', '', fp).strip() for fp in file_paths]
                    file_hint = f"\n\n📁 **Created files:**\n" + "\n".join(f"- `{fp}`" for fp in cleaned_paths[:5])

                self.history.append({
                    "role": "system",
                    "content": (
                        f"✅ **Task Complete — {task.agent_type} finished**\n"
                        f"(Task ID: {task.task_id[:8]})\n\n"
                        f"{task_result_msg}"
                        f"{file_hint}\n\n"
                        f"--- END OF TASK OUTPUT ---\n\n"
                        f"**INSTRUCTION:** The task output above is the authoritative result. "
                        f"Do NOT call coding_agent again. "
                        f"Your FIRST action must be to reply to the user in their language — tell them "
                        f"where the project is, what the main file is called, and how to open/use it. "
                        f"Only AFTER sending that reply may you optionally call list_files or read_file "
                        f"once if you need a specific detail to answer a follow-up. "
                        f"Do NOT call tools before replying to the user."
                    )
                })
            else:
                # Extract file paths from result (for other agents)
                file_paths = re.findall(
                    r'(?:Saved to|Output|File|Path|Ausgabe|Datei):\s*([^\n]+\.(?:html?|pdf|docx?|txt|md|json|csv|xlsx?))',
                    task.result,
                    re.IGNORECASE
                )

                file_hint = ""
                if file_paths:
                    cleaned_paths = [re.sub(r'\x1b\[[0-9;]*m', '', fp).strip() for fp in file_paths]
                    file_hint = f"\n\n🔗 **EXTRACTED FILE PATHS (from Sub-Agent output):**\n"
                    for fp in cleaned_paths[:3]:
                        file_hint += f"- `{fp}`\n"
                    file_hint += (
                        f"\n💡 **TIP:** To read/analyze this file, use:\n"
                        f"- `read_file('{cleaned_paths[0]}')` for quick reading\n"
                        f"- `librarian_agent(file='{cleaned_paths[0]}', task='Summarize this document')` for detailed analysis\n"
                    )

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
            err_text = str(task.error or "")
            is_user_cancelled = (
                "[user_cancelled]" in err_text.lower()
                or "stopped/cancelled by user via stop button" in err_text.lower()
                or "stopped by user via stop button" in err_text.lower()
                or "cancelled by user via stop button" in err_text.lower()
            )
            if is_user_cancelled:
                UI.warning(f"⏹ Sub-Agent [{task.task_id}] stopped/cancelled by user.")
                self.history.append({
                    "role": "system",
                    "content": (
                        f"⏹ **Sub-Agent Task STOPPED/CANCELLED BY USER** [Task: {task.task_id}]\n"
                        f"Agent: {task.agent_type}\n\n"
                        f"The user pressed stop. This is an intentional cancel, not a sub-agent failure.\n"
                        f"Do not report this as an error."
                    )
                })
            else:
                UI.error(f"✗ Sub-Agent [{task.task_id}] failed: {task.error}")
                # Boundary coercion (Rule 4.7): persisted task JSON passes through
                # from_dict uncoerced - a legacy record can carry None here, and a
                # TypeError would abort the drain BEFORE consume_result (duplicate
                # re-delivery).
                _task_desc = str(getattr(task, "task_description", "") or "")[:300]
                # WW B-track for the ASYNC lane: sub-agent failures never hit the
                # sync reactive hook (the tool result was only the DELEGATED
                # marker), so attach the failed tool's know-how to THIS message -
                # content extension only, no extra history entry (adjacency), and
                # hard fail-safe before consume_result (exactly-once).
                _kh_block = ""
                try:
                    from vaf.whare_wananga.runtime import async_failure_hint
                    _kh = async_failure_hint(self, task.agent_type, err_text)
                    if _kh:
                        _kh_block = f"\n{_kh}\n"
                        try:
                            append_domain_log(
                                "backend",
                                f"[WW-REACTIVE-ASYNC] {task.agent_type}: know-how attached to drained failure"
                            )
                        except Exception:
                            pass
                except Exception:
                    _kh_block = ""
                self.history.append({
                    "role": "system",
                    "content": (
                        f"[X] **Sub-Agent Task FAILED / TERMINATED** [Task: {task.task_id}]\n"
                        f"Agent: {task.agent_type}\n"
                        f"Error: {task.error}\n"
                        + (f"Original task: {_task_desc}\n" if _task_desc else "")
                        + _kh_block
                        + f"\nIMPORTANT: The task has stopped. Do not say it is still running.\n"
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

        # Log subagent_end to timeline before removing tracking entry
        try:
            meta = self._async_subagent_tasks.get(task.task_id, {})
            started_at_dt = meta.get("started_at")
            duration_s = round((datetime.now() - started_at_dt).total_seconds(), 1) if started_at_dt else None
            log_timeline_event(
                "subagent_end",
                task_id=task.task_id,
                agent_type=getattr(task, "agent_type", meta.get("agent_type", "")),
                status=task.status,
                duration_s=duration_s,
                session=str(getattr(self, "current_session_id", "") or ""),
            )
        except Exception:
            pass

        # Remove from tracking and consume from queue
        if task.task_id in self._async_subagent_tasks:
            del self._async_subagent_tasks[task.task_id]
        
        ipc.consume_result(task.task_id)
        return needs_retry

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

            # Durable 'running' team-state entry: lights up the <team_state> renderer
            # (whose 'running' branch was dead code — only completion was ever written)
            # and survives compression and restarts, unlike the in-memory dict above.
            # Cleared by the existing completion writer in _process_subagent_result.
            try:
                if getattr(self, "main_persistence", None):
                    _task_desc = ""
                    try:
                        _d = self.main_persistence.get_subagent_delegation_intent(agent_type)
                        _task_desc = (_d or {}).get("goal") or (_d or {}).get("intent") or ""
                    except Exception:
                        _task_desc = ""
                    self.main_persistence.update_subagent_status(
                        task_id, agent_type, "running",
                        details=(_task_desc[:200] or "Working..."),
                    )
            except Exception:
                pass

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
        """Make the local backend ready; no-op for API providers.

        Local provider: ensure the GGUF exists (locked, self-healing download)
        and start or reuse the ONE llama server on 127.0.0.1:8080 (or load
        llama-cpp in-process, platform-dependent). NOT called lazily by
        chat_step - in local mode call this (the vaf.Agent facade does) before
        chatting, else the turn aborts with "Agent not initialized".
        """
        from vaf.cli.ui import UI
        from vaf.core.gpu_detection import get_primary_gpu, _check_cuda_available
        
        # Skip model loading if using API backend
        if self.provider != "local":
            UI.event("Backend", f"Using API provider: {self.provider}, skipping model load", style="dim")
            return
        
        if not skip_download_check:
            self.ensure_model_exists()
        
        # GPU info: used below for the standalone-server status line and, ONLY on the
        # in-process library fallback, to decide whether to auto-install a CUDA build.
        # The CUDA auto-install is intentionally NOT done here. The standalone (Vulkan)
        # llama-server path below never loads llama-cpp-python in-process, so installing a
        # CUDA build for it wastes bandwidth -- and loops forever (re-downloading the
        # ~1.6 GB wheel every start) when the system is missing libcudart. It now runs only
        # right before the library is actually loaded -- the one case where it is the backend.
        primary_gpu = get_primary_gpu()

        n_gpu = self.config.get("gpu_layers", 99) # Default to max for server
        n_ctx = max(self.config.get("n_ctx", 32768), 32768)  # 32768 minimum: system prompt ~5.5K + tool schemas ~6K + conversation

        # API Provider Check (Best Practice: Use API if configured)
        if self.provider != "local":
            UI.event("System", f"Initializing API Backend: {self.provider.upper()}...", style="warning")
            try:
                self.api_backend = self._build_api_backend(self.provider)
                UI.event("Success", f"API Backend ready: {self.provider.upper()}", style="success")
                
                # Audit log
                try:
                    from vaf.core.user_notifications import append_notification
                    from vaf.core.config import get_local_admin_scope_id
                    append_notification(
                        user_scope_id=str(get_local_admin_scope_id()),
                        kind="system",
                        title=f"API Backend ready: {self.provider.upper()}",
                        status="success",
                        summary=f"Provider: {self.provider}\nModel: {self.model_display_name}"
                    )
                except: pass
                
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
            # Make sure the model file is on disk BEFORE waiting for / starting any server. The shared
            # ensure is filelock-serialized, so if the Tray is mid-download this BLOCKS here until it
            # finishes -- instead of racing it and starting a server against a missing file (which used to
            # fail and trigger a wasteful ~1.6 GB CUDA reinstall). Self-heals a missing/unknown model.
            if not os.path.exists(self.model_path):
                try:
                    self.ensure_model_exists()
                except Exception as e:
                    UI.error(f"Model preparation failed: {e}")
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
            # Wait for the Tray (or another process) to start the server before we start ourselves.
            # Keep waiting while a model download is still in progress so we never start a competing
            # server mid-download (the 30-iteration floor still applies once no download is active).
            from vaf.core.model_download_state import MODEL_DOWNLOAD
            _waited = 0
            while _waited < 30 or MODEL_DOWNLOAD.active:
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
                _waited += 1
            self.server = ServerManager()
            if self.server.start_server(self.model_path, n_gpu_layers=n_gpu, n_ctx=n_ctx):
                self.use_server = True
                
                # Audit log
                try:
                    from vaf.core.user_notifications import append_notification
                    from vaf.core.config import get_local_admin_scope_id
                    append_notification(
                        user_scope_id=str(get_local_admin_scope_id()),
                        kind="system",
                        title="Local model loaded",
                        status="success",
                        summary=f"Model: {self.filename}\nBackend: Standalone Server"
                    )
                except: pass

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

        # If the model file still isn't on disk (download failed / offline), do NOT load the in-process
        # library or auto-install CUDA -- the cause is a missing model, not a missing GPU backend. Bail
        # cleanly so the caller (e.g. the headless worker) can show "model unavailable" and retry.
        if not os.path.exists(self.model_path):
            UI.error(f"Model file not available: {self.model_path}. Skipping in-process load / CUDA install.")
            return

        # Fallback to Local Library (optional dep: pip install llama-cpp-python).
        # Reaching here means NO standalone/HTTP server backend was used, so the in-process
        # library really is the inference backend -- the ONLY situation where a CUDA build of
        # llama-cpp-python is worth installing. (Gated here so the Vulkan-server path never
        # triggers the wasteful, looping ~1.6 GB CUDA reinstall.)
        if primary_gpu and primary_gpu.vendor == "nvidia" and not primary_gpu.compute_available:
            if not _check_cuda_available():
                UI.warning("NVIDIA GPU detected but CUDA not available.")
                UI.print("[yellow]VAF can automatically install CUDA-enabled llama-cpp-python.[/yellow]")
                # No blocking terminal prompt here: the Web UI / headless worker shares the
                # terminal's stdin, so input() would freeze the chat request forever. Auto-install
                # unless the user opts out via `auto_install_gpu=false`.
                if not bool(self.config.get("auto_install_gpu", True)):
                    UI.print("[yellow]auto_install_gpu is off -- running on CPU. Run 'vaf install-gpu' for CUDA.[/yellow]")
                else:
                    try:
                        UI.event("System", "Auto-installing CUDA-enabled llama-cpp-python...", style="warning")
                        import subprocess
                        system = platform.system()
                        env = os.environ.copy()
                        pip_cmd = [sys.executable, "-m", "pip", "install", "llama-cpp-python", "--no-cache-dir", "--force-reinstall"]
                        if system in ("Windows", "Linux"):
                            env["CMAKE_ARGS"] = "-DGGML_CUDA=on"
                            pip_cmd.extend(["--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cu124"])
                        subprocess.check_call(pip_cmd, env=env)
                        UI.event("Success", "CUDA support installed -- restart VAF to use the GPU.", style="success")
                        primary_gpu = get_primary_gpu()
                    except Exception as e:
                        UI.error(f"CUDA installation failed: {e}")
                        UI.print("[yellow]Running on CPU. Manual install: vaf install-gpu[/yellow]")

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
                
            # Audit log
            try:
                from vaf.core.user_notifications import append_notification
                from vaf.core.config import get_local_admin_scope_id
                append_notification(
                    user_scope_id=str(get_local_admin_scope_id()),
                    kind="system",
                    title="Local model loaded",
                    status="success",
                    summary=f"Model: {self.filename}\nBackend: Internal Library"
                )
            except: pass

            UI.event("System", "Model Loaded", style="success")
        except Exception as e:
            UI.error(f"Init failed: {e}")
            self.llm = Llama(model_path=self.model_path, n_gpu_layers=0, n_ctx=n_ctx, verbose=False)
            UI.event("System", "CPU Mode Active", style="warning")

    def ensure_model_exists(self):
        """Make sure the model GGUF is on disk (download if missing) via the shared, locked, self-healing
        ensure_model_available -- ONE implementation shared with the tray/server path. It fixes a bare
        config filename to its real repo, serializes concurrent downloads (filelock), self-heals a
        missing/unknown model to the VRAM-adaptive default, and never sys.exit()s. Updates
        self.model_path/filename to the resolved (possibly self-healed) file."""
        from vaf.core.backend import ensure_model_available
        model_cfg = os.environ.get("VAF_MODEL_OVERRIDE", "").strip() or self.config.get("model")
        path = ensure_model_available(model_cfg, self.models_dir)
        self.model_path = path
        self.filename = os.path.basename(path)

    def init_chat(self):
        """Build the system prompt and start a fresh conversation.

        Rebuilds the prompt manager from the current tools/config/session
        identity, loads project context (VAF.md in the cwd, capped), and
        RESETS ``self.history`` to just the system message. Call once before
        the first chat_step; calling again discards the running conversation.
        """
        # Initialize Prompt Manager
        n_ctx = max(self.config.get("n_ctx", 32768), 32768)  # 32768 minimum for local models

        # If running in API mode, use a much larger default context limit
        if self.provider != "local":
             if n_ctx <= 32768:
                 n_ctx = 128000
                 
        self.prompt_manager = SystemPromptManager(list(self.tools.values()), model_name=self.model_display_name, agent_instance=self, max_tokens=n_ctx)
                
        # Build initial prompt (Core + Base Rules)
        # We pass self.filename to determine identity (VQ-1 vs Generic), and current user for User identity block
        system_prompt = self.prompt_manager.build_prompt(
            self.filename,
            username=getattr(self, "_current_username", None),
            user_scope_id=getattr(self, "_current_user_scope_id", None),
            current_source=getattr(self, "_current_chat_source", None),
            last_interaction=(
                None if getattr(self, "_is_new_session", False)
                else get_last_interaction(getattr(self, "_current_user_scope_id", None))
            ),
            front_office=getattr(self, "_front_office_mode", False),
            # Session-derived prompt content (workspace line) must key on THIS
            # chat - the process-global fallback races under parallel workers.
            session_id=getattr(self, "current_session_id", None),
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

    def _bind_session_persistence(self, session_id: str) -> None:
        """Re-point the persistence store to this session's isolated dir so plan/tasks/notes/team/
        intent never leak across chats or users. init_chat rebuilds prompt_manager (with a global
        store), so re-point prompt_manager.mpm too. Best-effort: never break a turn on failure."""
        try:
            from vaf.core.main_persistence import MainPersistenceManager
            self.main_persistence = MainPersistenceManager(os.getcwd(), session_id=session_id)
            if getattr(self, "prompt_manager", None) is not None:
                self.prompt_manager.mpm = self.main_persistence
        except Exception:
            pass

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
        # CRITICAL: Pass state_registry so runtime state (ContextManager, etc.) is restored
        sm = SessionManager(state_registry=self.state_registry)
        try:
            session = sm.load(session_id)
            # Set current user from session metadata so build_prompt() shows the right User identity.
            # Assign UNCONDITIONALLY (including None): switching to a session that has no owner must
            # RESET the in-memory identity, otherwise the previous session's user_scope_id/username
            # bleeds into the new session and that user's mail/memory/contacts could be exposed.
            # None is safe downstream (tool dispatch falls back to local admin, admin checks treat
            # None as non-admin). Headless re-applies the authoritative task identity after this load.
            meta = getattr(session, "metadata", None) or {}
            self._current_user_scope_id = meta.get("user_scope_id")
            self._current_username = meta.get("username")
            # Suppress last_interaction preview for empty sessions: the "Prior topic: ..."
            # hint causes the agent to treat the previous session's message as the current
            # one, leading to context bleed into brand new chats.
            self._is_new_session = not any(
                getattr(m, "role", None) in ("user", "assistant")
                for m in (session.messages or [])
            )
            # Reset Context (System Prompt)
            self.init_chat()

            # Replay History (session.messages are Message dataclass instances: .role, .content)
            for msg in session.messages:
                role = getattr(msg, "role", None)
                content = str(getattr(msg, "content", None) or "")
                if role in ["user", "assistant", "tool", "system"]:
                    # Skip duplicate or operational system prompts
                    if role == "system":
                        # List of operational log prefixes to IGNORE in LLM context
                        ignore_patterns = [
                            "System:", "Info:", "Step ", "Router:", "Queued input",
                            "Initializing Standalone Server", "Starting chat_step",
                            "Generation stopped", "Empty response detected"
                        ]
                        # Keep the per-turn "[Context: ...]" tool/reasoning summary — it is
                        # the agent's memory of what it did (and which errors it hit), so it
                        # must survive reload even if a snippet contains an ignored substring.
                        _is_turn_context = content.lstrip().startswith("[Context:")
                        if (
                            any(p in content for p in ignore_patterns)
                            and "## PROJECT CONTEXT" not in content
                            and not _is_turn_context
                        ):
                            continue

                    _tool_calls = getattr(msg, "tool_calls", None)

                    # Strip think blocks from LLM context (saved for UI display only)
                    if role == "assistant":
                        import re as _re
                        content = _re.sub(r'<think>.*?</think>', '', content, flags=_re.DOTALL)
                        content = content.strip()
                        # Keep tool-call messages even if their text is empty — their
                        # tool_calls are what anchor the following role:"tool" result.
                        if not content and not _tool_calls:
                            continue

                    entry = {"role": role, "content": content}
                    # Restore attached images (+ their persisted base_description) for user
                    # turns so multi-turn vision survives a reload / worker-pool pickup. Without
                    # this, _prepare_messages has no base description to inject and the model
                    # loses the image — wrongly claiming it had "guessed" its earlier analysis.
                    # Raw bytes are not sent to the main model (degraded to text); they let the
                    # analyze_image tool re-inspect the image, and skip costly re-description.
                    if role == "user":
                        _img_meta = (getattr(msg, "metadata", None) or {}).get("images")
                        if _img_meta:
                            entry["images"] = _img_meta
                    # Preserve tool-call linkage so restored history keeps valid
                    # tool_use/tool_result pairs (otherwise the pairing cleanup in
                    # _prepare_messages drops the orphaned results).
                    if role == "assistant" and _tool_calls:
                        entry["tool_calls"] = _tool_calls
                    elif role == "tool":
                        _tcid = getattr(msg, "tool_call_id", None)
                        if _tcid:
                            entry["tool_call_id"] = _tcid
                        _tname = getattr(msg, "name", None)
                        if _tname:
                            entry["name"] = _tname

                    self.history.append(entry)
            
            # Update Pointer
            self.current_session_id = session_id
            set_current_session_id(session_id)
            self._bind_session_persistence(session_id)

        except Exception:
            # New/Empty session — definitely no prior history
            self._is_new_session = True
            self.init_chat()
            self.current_session_id = session_id
            set_current_session_id(session_id)
            self._bind_session_persistence(session_id)

        # CRITICAL: Compress history immediately upon load IF needed
        # Otherwise UI shows massive "Raw Truth" (e.g. 17k tokens) which looks broken,
        # even though chat_step would compress it before sending.
        # We want the UI to show the "Ready State".
        current_tokens, max_tokens = self.get_token_usage()
        if current_tokens > max_tokens * 0.9:
            self.manage_context()
            
        # Broadcast new context stats to WebUI immediately
        self._broadcast_context_status()

    def _sanitize_response(self, text: str) -> str:
        """
        Detects and strips leaked tool call JSON fragments from the text response.
        Common when models get confused and output JSON in the text field.
        """
        if not text:
            return ""
            
        # Pattern 1: Full or partial JSON tool call arrays/objects at the end
        # Matches things like: [{"tool_calls": ...}] or {"name": "update_working_memory"...}
        # and even fragmented ones: ", "name": ... or "}, "type": "function", "index": 0}]}
        patterns = [
            r'\[?\s*\{\s*"tool_calls":.*$',
            r'\{\s*"name":\s*"[^"]*",\s*"arguments":.*$',
            r'\{\s*"index":\s*\d+,\s*"id":.*$',
            r'",\s*"name":\s*"[^"]*",\s*"type":\s*"function".*$',
            r'"\}\s*,\s*"type":\s*"function".*$',  # leaked tail: "}, "type": "function", "index": 0}]}
            r'",\s*"name":\s*"[^"]*"\}\s*,\s*"type":\s*"function".*$',  # full: , "name": "x"}, "type": "function"...
            r',\s*"name":\s*"[^"]*"?\s*$',  # trailing fragment: , "name": "update_working_memory" or without closing quote
            r'\}\s*,\s*\{\s*"index":\s*\d+.*$',
            r'\}\s*,\s*\{\s*"name":\s*"[^"]*".*$',
            r'\]\s*\}\s*$', # Closing brackets at the very end
            r'\}\s*\]\s*\}\s*$', # More closing brackets
        ]
        
        cleaned = text
        for pattern in patterns:
            # We use re.sub with a special handling for end of string to avoid destroying internal JSON
            # Only strip if the pattern is found near the end of the text
            if re.search(pattern, cleaned, flags=re.DOTALL | re.MULTILINE):
                # If the match is in the last 20% of the text or at least after 50 chars
                # this prevents stripping legitimate looking JSON in long technical answers
                cleaned = re.sub(pattern, '', cleaned, flags=re.DOTALL | re.MULTILINE)

        # Gemma-4 native tool-call tokens that leaked into the visible text (rare: the server usually
        # converts them via --jinja). Strip them so they are never shown. No-op for other models.
        cleaned = re.sub(r'<\|tool_call>[\s\S]*?<tool_call\|>', '', cleaned)
        cleaned = cleaned.replace('<|tool_call>', '').replace('<tool_call|>', '')

        return cleaned.strip()

    def _clean_reasoning(self, text: str) -> str:
        """Removes internal reasoning/CoT blocks from the model response."""
        import re
        
        # First, apply the JSON sanitizer to catch leaked tool calls
        t = self._sanitize_response(text)
        
        # 1. Remove XML-style thinking blocks. Collapse doubled/nested tags first (weak local models
        # sometimes emit <think><think>...), remove complete blocks, then drop any stray unpaired tag.
        t = re.sub(r'<think>(?:\s*<think>)+', '<think>', t, flags=re.IGNORECASE)
        t = re.sub(r'</think>(?:\s*</think>)+', '</think>', t, flags=re.IGNORECASE)
        # Greedy: first <think> .. LAST </think>. Weak models sometimes quote a </think> mid-reasoning
        # (e.g. quoting an earlier reply), so a non-greedy strip would stop early and leak the rest.
        t = re.sub(r'<think>[\s\S]*</think>', '', t)
        t = re.sub(r'</?think>', '', t)  # drop any stray unpaired <think> / </think>
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
            from vaf.vendor import langid  # type: ignore

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
            from vaf.vendor import langid
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
        Uses weighted ratios (2.8 for code, 3.6 for text) for high accuracy.
        """
        import json as json_module

        total_tokens = 0

        # 1. Estimate chat history tokens using weighted ratios
        for msg in self.history:
            content = str(msg.get("content", ""))
            role = str(msg.get("role", ""))
            
            # Weighted estimation (Code is denser than text)
            ratio = 2.8 if "```" in content else 3.6
            msg_tokens = int((len(content) + len(role)) / ratio) + 5
            total_tokens += msg_tokens

        # 2. Estimate tool schema tokens — only for local/server mode.
        # API backends handle schemas server-side.
        if hasattr(self, 'TOOLS') and self.TOOLS and not self.api_backend:
            try:
                schema_str = json_module.dumps(self.TOOLS)
                # Tool schemas are mostly structured text/code-like
                total_tokens += int(len(schema_str) / 3.0)
            except Exception:
                total_tokens += len(self.tools) * 200

        # Add small safety buffer
        total_tokens += 50
        
        # Use actual context manager limit if available
        if self.api_backend:
            max_tokens = self.config.get("n_ctx", 128000)
            if max_tokens <= 16384: max_tokens = 128000
        elif hasattr(self, 'context_manager'):
            # Use the configured context window (same formula as load_model / the server's n_ctx), NOT
            # whatever the manager was first built with. A manager created before n_ctx was raised would
            # otherwise pin compression/overflow to the 32768 floor while the model + server actually run
            # at e.g. 128000 -- firing premature "Compressing…/CRITICAL OVERFLOW" at a fraction of the
            # real window. Re-sync the manager so the limit always tracks the real context size.
            max_tokens = max(int(Config.get("n_ctx", 32768) or 32768), 32768)  # FRESH read, not the __init__ snapshot
            if self.context_manager.max_tokens != max_tokens:
                try:
                    append_domain_log("backend", f"[CTX-LIMIT] context_manager max_tokens {self.context_manager.max_tokens} -> {max_tokens}")
                except Exception:
                    pass
                self.context_manager.max_tokens = max_tokens
        else:
            max_tokens = self.config.get("n_ctx", 8192)

        return total_tokens, max_tokens

    def get_token_usage(self):
        """
        Calculates a precise token usage by using the model's tokenizer.
        """
        # API Backend: Calculate current history status even if no request was made yet.
        if self.api_backend:
            # Try to get data from last request as primary source of truth
            last_total = 0
            if hasattr(self.api_backend, 'last_request_usage'):
                li = self.api_backend.last_request_usage.get("input_tokens", 0)
                lo = self.api_backend.last_request_usage.get("output_tokens", 0)
                last_total = li + lo
            
            # If last_total is 0 (new session) or too small, calculate based on current history
            current_est = self._estimate_token_usage()[0]
            
            # Real total is the maximum of last request or current estimate
            # (Last request might be smaller if we just compressed, but current history is what counts)
            total = max(last_total, current_est)

            # Use the model's real context window, not the local n_ctx config value
            # (n_ctx is enforced at ≥ 32 768 for local models but is meaningless for API).
            n_ctx = self.api_backend.get_model_context_window(
                model=self.config.get(f"api_model_{self.provider}", "")
            )

            return total, n_ctx

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
            elif self.api_backend:
                # Cloud provider (no local :8080 server) - resolve model via api_model_{provider}.
                chunks = list(self.api_backend.chat_completion(
                    messages=temp_history,
                    max_tokens=200,
                    temperature=0.3,
                    stream=False,
                ))
                content = "".join(c if isinstance(c, str) else str(c) for c in chunks if c)
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
        _prev_compaction_env = os.environ.get("VAF_COMPACTION_IN_PROGRESS")
        os.environ["VAF_COMPACTION_IN_PROGRESS"] = "1"
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
            if _prev_compaction_env is None:
                os.environ.pop("VAF_COMPACTION_IN_PROGRESS", None)
            else:
                os.environ["VAF_COMPACTION_IN_PROGRESS"] = _prev_compaction_env
        return (content or "").strip()

    def _generate_for_document_extraction(self, user_prompt: str) -> str:
        """
        Single non-streaming LLM call for document learning extraction (per page/section).
        Same provider path as compaction but with lower max_tokens (memory_document_extraction_max_tokens, default 800).
        """
        from vaf.core.config import Config
        max_tokens = int(Config.get("memory_document_extraction_max_tokens", 1200) or 1200)
        max_tokens = max(400, min(max_tokens, 4000))  # headroom for reasoning models (<think> first)
        temp_history = [{"role": "user", "content": user_prompt}]
        content = ""
        self._compaction_in_progress = True
        _prev_doc_ext_env = os.environ.get("VAF_COMPACTION_IN_PROGRESS")
        os.environ["VAF_COMPACTION_IN_PROGRESS"] = "1"
        try:
            if self.use_server:
                import requests
                payload = {
                    "messages": temp_history,
                    "max_tokens": max_tokens,
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
                        max_tokens=max_tokens,
                        temperature=0.2,
                        stream=False,
                    )
                )
                content = "".join(c if isinstance(c, str) else str(c) for c in chunks if c)
            elif self.llm:
                output = self.llm.create_chat_completion(
                    messages=temp_history,
                    max_tokens=max_tokens,
                    temperature=0.2,
                )
                content = (output.get("choices") or [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Document extraction LLM call failed: %s", e)
        finally:
            self._compaction_in_progress = False
            if _prev_doc_ext_env is None:
                os.environ.pop("VAF_COMPACTION_IN_PROGRESS", None)
            else:
                os.environ["VAF_COMPACTION_IN_PROGRESS"] = _prev_doc_ext_env
        return (content or "").strip()

    def _generate_for_classification(self, prompt: str) -> str:
        """Single non-streaming, tiny LLM call used by the background thinking run to classify the
        outcome of a proactive question (one word: ACCEPTED / DECLINED / UNCLEAR). Same provider routing
        as _generate_for_document_extraction, deterministic. max_tokens has headroom because a reasoning
        model (e.g. the background-pro DeepSeek) may emit <think> first; the caller scans the text for the
        keyword, so trailing reasoning is harmless. Returns the raw text."""
        temp_history = [{"role": "user", "content": prompt}]
        content = ""
        try:
            if self.use_server:
                import requests
                payload = {
                    "messages": temp_history,
                    "max_tokens": 256,
                    "temperature": 0.0,
                    "stream": False,
                }
                res = requests.post(
                    "http://127.0.0.1:8080/v1/chat/completions",
                    json=payload,
                    timeout=60,
                ).json()
                content = (res.get("choices") or [{}])[0].get("message", {}).get("content", "")
            elif self.api_backend:
                chunks = list(
                    self.api_backend.chat_completion(
                        messages=temp_history,
                        max_tokens=256,
                        temperature=0.0,
                        stream=False,
                    )
                )
                content = "".join(c if isinstance(c, str) else str(c) for c in chunks if c)
            elif self.llm:
                output = self.llm.create_chat_completion(
                    messages=temp_history,
                    max_tokens=256,
                    temperature=0.0,
                )
                content = (output.get("choices") or [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Reply-classification LLM call failed: %s", e)
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
        
        # Determine appropriate context limit
        current_tokens_calc, max_tokens = self.get_token_usage()

        # Initialize or update context manager if max_tokens changed (e.g. switched to API)
        if not hasattr(self, '_context_manager') or self._context_manager.max_tokens != max_tokens:
            UI.event("Context", f"Initializing context manager (limit: {max_tokens} tokens)", style="dim")
            self._context_manager = ContextManager(max_tokens=max_tokens)
        
        # Ensure prompt manager also has latest limit for dynamic decay
        if hasattr(self, 'prompt_manager') and self.prompt_manager.max_tokens != max_tokens:
            self.prompt_manager.max_tokens = max_tokens
            # Refresh dynamic decay settings
            if max_tokens <= 12000:
                self.prompt_manager.decay_start = 2
                self.prompt_manager.module_decay_turns = {"coding": 3, "research": 2, "filesystem": 2}
            elif max_tokens <= 20000:
                self.prompt_manager.decay_start = 2
                self.prompt_manager.module_decay_turns = {"coding": 4, "research": 3, "filesystem": 2}
            else:
                self.prompt_manager.decay_start = 3
                self.prompt_manager.module_decay_turns = {"coding": 5, "research": 4, "filesystem": 3}

        cm = self._context_manager
        
        # Check if compression needed using PRECISE token count (including tools)
        usage_percent = current_tokens_calc / max_tokens if max_tokens else 0
        
        # 1. EMERGENCY PURGE (Hard Reset if > 100%)
        # If we are already over the hard limit, standard compression might be too slow or fail.
        if usage_percent >= 1.0 and len(self.history) > 1:
            msg = f"CRITICAL OVERFLOW ({current_tokens_calc}/{max_tokens}). Emergency purge active!"
            UI.event("Context", msg, style="bold red")
            try:
                from vaf.core.web_interface import get_web_interface
                get_web_interface().log("Context limit reached! Performing emergency cleanup...", level="warning", source="System", session_id=getattr(self, 'current_session_id', None))
            except: pass

            # Absolute Minimum: System Prompt + LAST 6 messages (or as many as available)
            system_msg = [self.history[0]] if self.history and self.history[0].get("role") == "system" else []
            # Keep more than just the last message - try to keep 6
            recent_count = 6
            last_msgs = self.history[-recent_count:] if len(self.history) > 1 else []
            self.history = system_msg + last_msgs
            
            # Force UI update
            self._broadcast_context_status()
            # Continue to standard compression for remaining history if still needed
            current_tokens, _ = self.get_token_usage()
            usage_percent = current_tokens / max_tokens if max_tokens else 0

        if usage_percent < cm.trigger_threshold:
            return

        # Notify WebUI about standard compression
        try:
            from vaf.core.web_interface import get_web_interface
            get_web_interface().log(f"Context usage at {usage_percent:.0%}. Optimizing memory...", level="info", source="System", session_id=getattr(self, 'current_session_id', None))
        except: pass

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
        old_count = len(self.history)
        working_memory = None
        if getattr(self, "main_persistence", None):
            try:
                working_memory = self.main_persistence.get_working_memory()
            except Exception:
                working_memory = None
        self.history = cm.compress(self.history, working_memory=working_memory)
        new_count = len(self.history)
        
        try:
            from vaf.core.web_interface import get_web_interface
            get_web_interface().log(f"Context optimized: {old_count} messages reduced to {new_count}. Stable progress preserved.", level="success", source="System", session_id=getattr(self, 'current_session_id', None))
        except: pass

        # Broadcast update to WebUI
        self._broadcast_context_status()

    # ═══════════════════════════════════════════════════════════════════════════
    # PROACTIVE CHECKPOINT (Plan-Act-Summarize support)
    # ═══════════════════════════════════════════════════════════════════════════

    def checkpoint_and_reset(self, summary: str = "") -> str:
        """
        Agent-initiated checkpoint: archive history, compress, keep only
        system prompt + context glue + working memory injection.
        Called by the checkpoint_context tool after a multi-step plan checkpoint.
        Returns confirmation string.
        """
        from vaf.core.context import ContextManager

        if len(self.history) <= 2:
            return "[checkpoint] Nothing to checkpoint (history too short)."

        # Use _context_manager if available, fall back to context_manager (always initialized in __init__)
        cm = getattr(self, '_context_manager', None) or getattr(self, 'context_manager', None)
        if cm is None:
            _, max_tokens = self.get_token_usage()
            cm = ContextManager(max_tokens=max_tokens)
            self._context_manager = cm

        # 1. Archive full history (same as compress does)
        cm._archive_history(self.history)

        # 2. Intent: use persisted user intent so the glue block keeps the original goal
        #    (not the last user message, e.g. "do step 2")
        if getattr(self, "main_persistence", None):
            intent_text = self.main_persistence.get_user_intent()
            if intent_text:
                cm.update_intent(intent_text)
        # State: update from messages so glue has files/errors/decisions
        for msg in self.history:
            cm.update_state(msg)

        # 3. If caller provided a summary, use it as the narrative
        if summary:
            cm.state.narrative_summary = summary

        # 4. Build compressed history (system prompt + glue + last 6 messages)
        system_msg = self.history[0] if self.history else None
        # Keep more than 2 messages to maintain conversational flow (approx 3 turns)
        recent = self.history[-6:] if len(self.history) >= 6 else self.history[:]

        context_summary = cm._build_context_summary()
        working_memory = None
        if getattr(self, "main_persistence", None):
            try:
                working_memory = self.main_persistence.get_working_memory()
            except Exception:
                working_memory = None
        resume_block = cm.build_resume_block(self.history, working_memory=working_memory)
        restored_parts = [part for part in (context_summary, resume_block) if part]
        glue_msg = {"role": "user", "content": f"[CONTEXT RESTORED]\n" + "\n\n".join(restored_parts)}

        new_history = []
        if system_msg and system_msg.get("role") == "system":
            new_history.append(system_msg)
        new_history.append(glue_msg)
        new_history.extend(recent)

        old_len = len(self.history)
        self.history = new_history

        import logging as _logging
        _logging.getLogger(__name__).info(
            "Checkpoint: %d -> %d messages (archived full history)", old_len, len(new_history)
        )

        # Notify WebUI that history was intentionally compressed so it clears its
        # local cache and doesn't re-inject orphaned old messages on next history_update.
        try:
            from vaf.core.web_interface import get_web_interface
            session_id = getattr(self, "current_session_id", None)
            get_web_interface()._push_session_update(session_id, {
                "type": "context_checkpoint",
                "old_count": old_len,
                "new_count": len(new_history),
            })
        except Exception:
            pass

        return f"[checkpoint] Context reset: {old_len} -> {len(new_history)} messages. Plan and working memory preserved."

    def _broadcast_context_status(self):
        """Send precise context debug info to WebUI (X-Ray Vision)."""
        try:
            from vaf.core.web_interface import get_web_interface
            session_id = getattr(self, "current_session_id", None)
            
            # 1. Get absolute totals (Precise)
            tokens, max_tokens = self.get_token_usage()

            # 2. Calculate detailed breakdown (High Precision)
            system_tokens = 0
            history_tokens = 0
            tools_tokens = 0
            system_content = ""
            
            # Setup tokenizer for breakdown if available
            tokenizer = None
            if not self.api_backend: # Only use local tokenizer for local/server modes
                if self.use_server:
                    pass # We will use the /tokenize API per block
                else:
                    tokenizer = self._get_tokenizer()

            for msg in self.history:
                content = str(msg.get("content", ""))
                role = msg.get("role", "")
                
                # Precise token count for this message
                msg_tokens = 0
                if self.use_server:
                    try:
                        r = requests.post("http://127.0.0.1:8080/tokenize", json={"content": content + role}, timeout=2)
                        if r.status_code == 200:
                            msg_tokens = len(r.json().get("tokens", [])) + 5
                    except: pass
                
                if msg_tokens == 0 and tokenizer:
                    try:
                        msg_tokens = len(tokenizer.tokenize((content + role).encode("utf-8", errors="ignore"))) + 5
                    except: pass
                
                if msg_tokens == 0:
                    # Improved weighted estimation (Code is denser than text)
                    ratio = 2.8 if "```" in content else 3.6
                    msg_tokens = int((len(content) + len(role)) / ratio) + 5

                if role == "system":
                    system_tokens += msg_tokens
                    if not system_content:
                        system_content = content
                else:
                    history_tokens += msg_tokens

            # 3. Handle Tool Schema Tokens
            # Important: this must work for BOTH local and API providers, otherwise
            # the Context Window can show 0 for tools in API mode.
            if hasattr(self, 'TOOLS') and self.TOOLS:
                try:
                    schema_str = json.dumps(self.TOOLS)
                    if self.use_server:
                        r = requests.post("http://127.0.0.1:8080/tokenize", json={"content": schema_str}, timeout=2)
                        if r.status_code == 200:
                            tools_tokens = len(r.json().get("tokens", []))
                    if tools_tokens == 0 and tokenizer:
                        tools_tokens = len(tokenizer.tokenize(schema_str.encode("utf-8")))
                    if tools_tokens == 0:
                        tools_tokens = len(schema_str) // 3
                except Exception:
                    tools_tokens = len(self.tools) * 200

            # 4. Sync persistent turn count
            user_turn_count = 0
            compaction_interval = 15
            try:
                from vaf.core.config import Config
                from vaf.core.session import SessionManager
                compaction_interval = int(Config.get("memory_compaction_interval", 15))
                if session_id:
                    try:
                        _sm = SessionManager(state_registry=self.state_registry)
                        _session = _sm.load(session_id)
                        _runtime = getattr(_session, 'runtime_state', None) or {}
                        user_turn_count = _runtime.get("user_turn_count", 0)
                    except: pass
                if user_turn_count == 0:
                    user_turn_count = sum(1 for m in self.history if m.get("role") == "user")
            except: pass

            # 5. Broadcast to SPECIFIC SESSION (No Multi-Tab Leak!)
            if _emit_to_web_ui() and session_id:
                get_web_interface()._push_session_update(session_id, {
                    "type": "context_status",
                    "stats": {
                        "tokens": tokens,
                        "max_tokens": max_tokens,
                        "percent": round((tokens / max_tokens) * 100, 1) if max_tokens else 0,
                        "message_count": len(self.history),
                        "rag_preview": system_content,
                        "system_tokens": system_tokens,
                        "history_tokens": history_tokens,
                        "tools_tokens": tools_tokens,
                        "user_turn_count": user_turn_count,
                        "compaction_interval": compaction_interval,
                        "compaction_progress": round((user_turn_count % compaction_interval) / compaction_interval * 100)
                    }
                })
        except Exception as e:
            append_domain_log("backend", f"broadcast_status_error: {e}")
    
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
            elif self.api_backend:
                # Cloud provider (no local :8080 server) - resolve model via api_model_{provider}.
                try:
                    chunks = list(self.api_backend.chat_completion(
                        messages=messages, max_tokens=32, temperature=0.0, stream=False))
                    content = "".join(c if isinstance(c, str) else str(c) for c in chunks if c)
                    try:
                        append_domain_log("backend", f"intent_llm_response content={content[:30] if content else 'EMPTY'}")
                    except Exception:
                        pass
                except Exception as e:
                    try:
                        append_domain_log("backend", f"intent_llm_error error={str(e)[:50]}")
                    except Exception:
                        pass
            elif self.llm:
                try:
                    output = self.llm.create_chat_completion(
                        messages=messages, max_tokens=32, temperature=0.0)
                    content = (output.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
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
        # Fresh each turn: the unified router sets this only on a positive skill match.
        self._pending_skill_match = None

        try:
            from vaf.workflows.templates import get_workflow_templates, list_templates
            # Freshness-checked: picks up workflows created since startup (including
            # by another process) so the router can route to them this same turn.
            WORKFLOW_TEMPLATES = get_workflow_templates()

            # Get available workflows dynamicallly
            available_workflows = list_templates()

            # Format workflows like tool definitions for the LLM
            # "ID: Description"
            workflow_definitions = []
            for w in available_workflows:
                workflow_definitions.append(f"- {w['id']}: {w['description']}")
            workflow_list_str = "\n".join(workflow_definitions)

            # Skills are the SECOND routing tier (under workflows). The router only
            # ever sees name+description (progressive disclosure); the body loads
            # later via use_skill. Scope to the current user.
            _skill_scope = getattr(self, "_current_user_scope_id", None)
            try:
                from vaf.skills.templates import list_skills
                available_skills = list_skills(user_scope_id=_skill_scope)
            except Exception:
                available_skills = []
            skill_ids = {s["id"] for s in available_skills}
            skill_list_str = "\n".join(
                f"- {s['id']}: {s['description']}" for s in available_skills
            ) or "(none)"
            
            prompt = (
                f"You are the Router. Map a user request to the single best pre-defined "
                f"WORKFLOW or SKILL, or to nothing.\n\n"
                f"A WORKFLOW is a fixed multi-step pipeline that runs automatically.\n"
                f"A SKILL is a set of expert instructions the agent reads and then follows flexibly.\n\n"
                f"AVAILABLE WORKFLOWS:\n"
                f"{workflow_list_str}\n\n"
                f"AVAILABLE SKILLS:\n"
                f"{skill_list_str}\n\n"
                f"ROUTING INSTRUCTIONS:\n"
                f"1. Analyze the User Request for INTENT.\n"
                f"2. If a WORKFLOW matches that intent strongly, return `workflow:<id>`.\n"
                f"3. Else if a SKILL matches the intent, return `skill:<id>`.\n"
                f"4. Return `none` if:\n"
                f"   - The request is a simple lookup (weather, news, facts).\n"
                f"   - The request is a generic chat or question.\n"
                f"   - The request is too vague.\n"
                f"   - You would rather use individual tools (web_search, coding_agent) directly.\n"
                f"   - Nothing above is a clear match.\n\n"
                f"EXAMPLES:\n"
                f"- User: 'Create a website' -> workflow:create_website\n"
                f"- User: 'Research AI trends and write a report' -> workflow:research_and_document\n"
                f"- User: 'What is the weather?' -> none (Too simple)\n"
                f"- User: 'Who is Elon Musk?' -> none (Too simple)\n"
                f"- User: 'Die Webseite ist buggy, schau dir das an' -> none (Fix/debug request)\n"
                f"- User: 'Fix the layout issue on the site' -> none (Fix/debug request)\n\n"
                f"USER REQUEST: \"{user_input}\"\n\n"
                f"Think step-by-step. Output ONLY one token: workflow:<id>, skill:<id>, or none."
            )
            
            # Quick Inference with reasoning (temperature 0.1 for strict logic)
            messages = [{"role": "user", "content": prompt}]
            
            # Provider-agnostic router inference: the LLM tier is the ONLY tier that
            # can route SKILLS (and do intent-based workflow routing). It must run the
            # same way regardless of provider, mirroring the canonical 3-way pattern
            # used elsewhere in this file (e.g. the false-promise validator):
            #   local llama.cpp server  -> use_server
            #   cloud / API provider    -> api_backend   (e.g. veyllo)
            #   local in-process lib    -> llm
            # Previously only use_server was handled, so on a cloud provider the router
            # fell straight to Tier-3 keyword matching, which knows workflows only and
            # never offers skills.
            content = ""
            router_llm_ok = False
            if self.use_server:
                # Full thinking capacity with 120s timeout
                payload = {"messages": messages, "max_tokens": 1024, "temperature": 0.1}
                try:
                    res = requests.post("http://127.0.0.1:8080/v1/chat/completions", json=payload, timeout=120).json()
                    content = res['choices'][0]['message']['content']
                    router_llm_ok = True
                except Exception:
                    pass
            elif self.api_backend:
                # Cloud / API provider: route through the SAME backend the main agent
                # uses (no hardcoded localhost). chat_completion yields chunks; collect.
                try:
                    chunks = list(self.api_backend.chat_completion(
                        messages, max_tokens=1024, temperature=0.1, stream=False))
                    content = "".join(c if isinstance(c, str) else str(c) for c in chunks if c)
                    router_llm_ok = True
                except Exception:
                    pass
            elif self.llm:
                # Local in-process library (llama-cpp-python)
                try:
                    output = self.llm.create_chat_completion(
                        messages=messages, max_tokens=1024, temperature=0.1)
                    content = output['choices'][0]['message']['content']
                    router_llm_ok = True
                except Exception:
                    pass

            if not router_llm_ok:
                # No LLM router available (no backend, or the call failed) → Tier 3
                # pattern matching (workflows only), then Tier 2 (agent choice).
                self._workflow_selection_tier = 3
                from vaf.workflows.selector import WorkflowSelector
                selector = WorkflowSelector()
                result = selector.select(user_input)
                if result and result.matched and result.confidence >= 0.5:
                    return result.template_id
                self._workflow_selection_tier = 2
                return None

            low = content.lower()

            # 1) SKILL match (skill:<id>). Skills bypass the workflow execution path:
            #    we store a side-channel match and return None, so _try_workflow turns
            #    it into a one-shot [SKILL SUGGESTION] hint instead of running a pipeline.
            if skill_ids:
                skill_ids_pattern = '|'.join(re.escape(sid) for sid in skill_ids)
                m_skill = re.search(rf'skill:\s*({skill_ids_pattern})\b', low)
                if m_skill:
                    matched_skill = m_skill.group(1)
                    name = next(
                        (s["name"] for s in available_skills if s["id"] == matched_skill),
                        matched_skill,
                    )
                    self._pending_skill_match = {"skill_id": matched_skill, "name": name}
                    return None

            # 2) WORKFLOW match (workflow:<id> or a bare id for back-compat).
            workflow_ids_pattern = '|'.join(re.escape(wf_id) for wf_id in WORKFLOW_TEMPLATES.keys())
            if workflow_ids_pattern:
                match = re.search(rf'(?:workflow:\s*)?\b({workflow_ids_pattern})\b', low)
                if match:
                    workflow_id = match.group(1)
                    if workflow_id in WORKFLOW_TEMPLATES:
                        return workflow_id

            # 3) Explicit "none" response
            if "none" in low:
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

        # Skip workflow matching while a sub-agent genuinely runs for this session
        # (chat-while-subagent-runs): the router fires PRE-LLM, so the SUB-AGENT ACTIVE
        # prompt block cannot bind it — a light message keyword-matching a workflow
        # would inject an execute nudge or spawn heavy work next to the running task.
        try:
            if self.get_live_session_subagents():
                return None
        except Exception:
            pass

        # Note: Intent-based keyword guards (fix/debug vs create) have been removed.
        # The router now surfaces matched workflows as [WORKFLOW SUGGESTION] hints so
        # the main agent — which has full conversation context and [SESSION WORKSPACE] —
        # decides whether to execute, edit an existing project, or do something else.
            
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
                if sm.is_tts_enabled() and self._host_audio_allowed:
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
                # The unified router may have matched a SKILL instead. Convert that
                # side-channel match into a one-shot [SKILL SUGGESTION] hint (injected
                # into the user turn by chat_step) and skip the generic "no workflow"
                # note — the agent already has a concrete suggestion.
                _skill_match = getattr(self, "_pending_skill_match", None)
                if _skill_match:
                    self._pending_skill_match = None
                    self._pending_skill_hint = {
                        "skill_id": _skill_match["skill_id"],
                        "name": _skill_match.get("name", _skill_match["skill_id"]),
                    }
                    UI.event(
                        "Step 1/2",
                        f"Skill [suggested: {_skill_match['skill_id']} - Agent deciding]",
                        style="cyan",
                    )
                    return None

                # No SAVED workflow template matched - give agent a brief hint (not the
                # full list). This must not talk the model OUT of workflows for a
                # request that explicitly asked for one: the old fixed wording used
                # "weather" as its example of something that never needs a workflow,
                # which directly contradicted a user request to run a weather lookup
                # AS a workflow, and never mentioned create_agent_workflow(run_temp) -
                # the ad-hoc builder is the right answer here precisely because no
                # SAVED template fits an on-the-fly multi-step request (live incident:
                # the model called list_tools, saw run_temp buried on page 1 of a 15KB
                # dump, and just did the steps manually instead).
                #
                # Detection is a cheap, imprecise substring match (typo-tolerant on
                # purpose: the real incident's message had "workflow" transposed to
                # "workflwo", which a whole-word match would have missed), so it WILL
                # also match unrelated mentions ("workforce" news, "review my workflow
                # doc"). Both branches below stay ADVISORY, not directive - matching
                # this file's own "agent decides" design principle (see "New Flow"
                # above) - specifically so a false match cannot push the model into an
                # unwanted run_temp call; only the STRENGTH of the suggestion differs.
                # "workf(?!orce)" excludes the one common real word that shares the
                # prefix; other topical false positives are left to the model's
                # judgment, which the wording below explicitly invites.
                _user_asked_for_workflow = bool(
                    re.search(r"\bworkf(?!orce)", user_input or "", re.IGNORECASE)
                )
                if _user_asked_for_workflow:
                    _no_match_hint = (
                        "ℹ️ No SAVED workflow template matched this request, and the "
                        "user's message mentions a workflow. If this is genuinely "
                        "multi-step work (2+ chained steps - research, analyse, produce "
                        "a deliverable), create_agent_workflow with action='run_temp' can "
                        "build and run one on the fly. Use your own judgment: a "
                        "single-step lookup, a question, or an unrelated mention of the "
                        "word does not need it - keep using your tools directly for those."
                    )
                else:
                    _no_match_hint = (
                        "ℹ️ No workflow automatically matched for this request. "
                        "For a genuinely multi-step task (research -> analyse -> produce "
                        "a deliverable, 2+ chained steps), consider create_agent_workflow "
                        "with action='run_temp' to run one ad hoc, or the 'list_workflows' "
                        "tool to see saved templates. A single-step lookup or question "
                        "does not need either."
                    )
                self.history.append({"role": "system", "content": _no_match_hint})

                # Show Tier 2 status (Agent Choice)
                UI.event("Step 1/2", f"Workflow [Tier 2: No auto-match - Agent deciding]", style="cyan")
                return None
            
            # 🔒 INTENT LOCK (Workflow): Save the fresh user intent to persistence
            # CRITICAL: Skip intent update if running in thinking mode (background).
            if hasattr(self, 'main_persistence') and self.main_persistence:
                _is_thinking_mode = os.environ.get("VAF_THINKING_MODE", "").strip() in ("1", "true", "yes")
                if not _is_thinking_mode:
                    try:
                        self.main_persistence.update_user_intent(user_input)
                        self.main_persistence.reset_validation_retry_count()
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

            # ═══════════════════════════════════════════════════════════════
            # RECOMMENDATION MODE (non-explicit requests)
            # ═══════════════════════════════════════════════════════════════
            # If the user did NOT explicitly choose a workflow (@workflow_id),
            # do NOT auto-execute. Instead store the detected workflow as a
            # hint so the main agent — which has full conversation context,
            # [SESSION WORKSPACE], and history — can decide whether to start
            # it, edit an existing project, or do something else entirely.
            if not explicit_workflow_id:
                self._pending_workflow_hint = {
                    "workflow_id": result.template_id or workflow_id,
                    "name": template.get("name", workflow_id),
                    "variables": result.variables or {},
                }
                UI.event("Workflow", f"Suggested: {template.get('name', workflow_id)} (agent will decide)", style="dim")
                return None  # Agent runs with [WORKFLOW SUGGESTION] injected

            # Build workflow steps from template (explicit @workflow_id path only)
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

            # DEBUG: Log workflow execution path (workflow_debug_YYYY-MM-DD.log)
            try:
                import datetime
                path = get_dated_log_path("workflow_debug", "log")
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
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
                                import datetime
                                path = get_dated_log_path("workflow_debug", "log")
                                with open(path, "a", encoding="utf-8") as f:
                                    f.write(f"{datetime.datetime.now().isoformat()} {msg}\n")
                            except Exception:
                                pass

                        _debug_log("STEP 1: Creating IPC task...")
                        # Create IPC task
                        from vaf.core.subagent_ipc import get_ipc, get_current_session_id
                        ipc = get_ipc()
                        # Re-delegation guard (same rule as the sub-agent tools):
                        # after an empty-response snapshot reset the model forgets
                        # it already delegated and starts the SAME workflow again
                        # (live incident: duplicate research run, double GPU load).
                        # Session-scoped IPC is the truth (Rule 4.4).
                        try:
                            _dup = [
                                t for t in ipc.get_active_tasks_for_current_session()
                                if getattr(t, "agent_type", "") == f"workflow:{workflow_id}"
                            ]
                        except Exception:
                            _dup = []
                        if _dup:
                            _debug_log(f"BLOCKED duplicate workflow launch: {workflow_id}")
                            return (
                                f"Workflow '{workflow_id}' is ALREADY RUNNING for this chat "
                                "- not starting a duplicate. Tell the user the workflow is "
                                "still in progress; the result will arrive when it finishes."
                            )
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

                        # Session/task context goes into the CHILD env only (not the parent's
                        # global env), so concurrent workers don't clobber each other's session.
                        session_id = get_current_session_id()
                        _sub_env = {"VAF_TASK_ID": task_id, "VAF_AGENT_TYPE": f"workflow:{workflow_id}"}
                        if session_id:
                            _sub_env["VAF_SESSION_ID"] = session_id
                        _debug_log(f"STEP 4: Child env prepared, session_id={session_id}")

                        # Pass Language Hint to workflow terminal
                        if hasattr(self, 'prompt_manager') and self.prompt_manager.user_language:
                            _sub_env["VAF_USER_LANGUAGE"] = self.prompt_manager.user_language

                        # Build command with proper escaping for the platform.
                        # Use sys.executable so the correct venv Python is used regardless
                        # of whether 'vaf' is on PATH (it lives in venv/bin which is not
                        # always on the system PATH when VAF starts as a service).
                        _py = shlex.quote(sys.executable)
                        if Platform.is_windows():
                            # Windows CMD: escape double quotes with backslash (for subprocess shell=True)
                            # Also escape backslashes that precede quotes
                            escaped_json = variables_json.replace('\\', '\\\\').replace('"', '\\"')
                            cmd = f'{sys.executable} -m vaf.main workflow run "{workflow_id}" --variables "{escaped_json}" --task-id {task_id}'
                        else:
                            # Unix: use shlex.quote for proper escaping
                            cmd = f'{_py} -m vaf.main workflow run "{workflow_id}" --variables {shlex.quote(variables_json)} --task-id {task_id}'
                        _debug_log(f"STEP 5: Command built: {cmd[:300]}")

                        _debug_log("STEP 6: Calling Platform.open_new_terminal...")
                        result_ok = Platform.open_new_terminal(cmd, title=f"VAF Workflow: {workflow_id}", extra_env=_sub_env)
                        _debug_log(f"STEP 7: open_new_terminal returned: {result_ok}")
                    except Exception as e:
                        _debug_log(f"ERROR: {type(e).__name__}: {e}")
                        raise
                    
                    UI.event("Workflow", msg_running_separate.format(task_id=task_id[:8]), style="cyan")
                    UI.info(msg_runs_independently)
                    
                    # Return async marker
                    return msg_async_return.format(task_id=task_id, workflow_id=workflow_id, name=template['name'])
            
            # Execute workflow inline (without defaults parameter).
            # Pass check_stop so user can abort via Stop button (checked between each step).
            session_id_for_stop = getattr(self, "current_session_id", None) or getattr(self, "_session_id", None)
            def _workflow_check_stop():
                if not session_id_for_stop:
                    return False
                from vaf.core.task_queue import TaskQueue
                return TaskQueue().should_stop(session_id_for_stop)

            workflow_result = engine.execute(
                steps,
                variables=result.variables,
                check_stop=_workflow_check_stop,
            )

            if workflow_result.error == "Stopped by user" and session_id_for_stop:
                from vaf.core.task_queue import TaskQueue
                TaskQueue().clear_stop(session_id_for_stop)

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
                    # Extract project path from "Full Path": `...` line (not other backtick expressions)
                    _full_path_match = re.search(r'\*\*Full Path\*\*[^`]*`([^`]+)`', final_output)
                    if _full_path_match:
                        project_path_hint = _full_path_match.group(1).strip()
                    else:
                        # Fallback: first standalone absolute path in backticks
                        for m in re.finditer(r'`([^`]+)`', final_output):
                            candidate = m.group(1).strip()
                            if candidate.startswith(('/', 'C:\\', 'D:\\')):
                                project_path_hint = candidate
                                break

                    # Only retry if the coder explicitly did NOT signal COMPLETE.
                    # Strings like "Remaining tasks" also appear in completion hints so
                    # we must not trigger the retry when the work is actually done.
                    _coder_complete = "[VAF_CODING_AGENT_STATUS: COMPLETE]" in final_output

                    # Check if the output indicates incomplete tasks
                    if not _coder_complete and ("Task Partially Complete" in final_output or "Tasks: 0/" in final_output or "Remaining tasks" in final_output):
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
            from vaf.workflows.templates import get_workflow_templates
            WORKFLOW_TEMPLATES = get_workflow_templates()
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

        # GitHub (remote) Heuristics
        if any(kw in u_lower for kw in [
            "github", "repositories", "my repos", "my repositories",
            "list my repos", "show my repos", "issues", "pull request", " pr ",
            "open issues", "open pr", "prs", "meine repos", "github repo"
        ]):
            if "github_list_repos" in self.tools:
                forced_tools.add("github_list_repos")
            if "github_get_file" in self.tools:
                forced_tools.add("github_get_file")
            if "github_get_file_structure" in self.tools:
                forced_tools.add("github_get_file_structure")
            if "github_list_issues" in self.tools:
                forced_tools.add("github_list_issues")
            if "github_list_pulls" in self.tools:
                forced_tools.add("github_list_pulls")

        # Cloud Storage Heuristics (before generic "google" in web search)
        if any(kw in u_lower for kw in [
            "google drive", "onedrive", "drive durchsuchen", "cloud storage",
            "in my drive", "auf meinem drive", "cloud datei", "cloud document",
            "pdf in drive", "suche pdf", "find pdf", "datei in cloud",
            "zeig pdf", "show pdf", "dokument anzeigen", "pdf anzeigen", "öffne pdf"
        ]):
            if "librarian_agent" in self.tools:
                forced_tools.add("librarian_agent")

        # Calendar Heuristics
        if any(kw in u_lower for kw in [
            "calendar", "kalender", "event", "termin", "meeting", "reminder",
            "erinnerung", "appointment", "verabredung", "schedule", "termine",
            "was steht an", "upcoming", "meine termine"
        ]):
            if "list_calendar_events" in self.tools:
                forced_tools.add("list_calendar_events")
            if "create_calendar_event" in self.tools:
                forced_tools.add("create_calendar_event")
        if any(kw in u_lower for kw in [
            "termin ändern", "termin verschieben", "event ändern", "event update",
            "termin updaten", "meeting verschieben", "appointment change", "reschedule"
        ]):
            if "update_calendar_event" in self.tools:
                forced_tools.add("update_calendar_event")
        if any(kw in u_lower for kw in [
            "termin löschen", "termin absagen", "event löschen", "event delete",
            "termin entfernen", "meeting absagen", "appointment cancel"
        ]):
            if "delete_calendar_event" in self.tools:
                forced_tools.add("delete_calendar_event")
        
        # Research Heuristics
        if any(kw in u_lower for kw in ["research", "recherche", "analyse", "report", "comprehensive", "umfassend", "deep"]):
             if "research_agent" in self.tools:
                 forced_tools.add("research_agent")
             if "web_search" in self.tools:
                 forced_tools.add("web_search")

        # Email Heuristics
        if any(kw in u_lower for kw in [
            "mail", "email", "inbox", "posteingang", "nachricht", "send mail",
            "label", "category", "kategorie", "promotions", "social", "primary",
            "newsletter", "rechnung", "bill", "invoice", "order", "bestellung"
        ]):
            if "mail_inbox" in self.tools:
                forced_tools.add("mail_inbox")
            if "find_mail" in self.tools:
                forced_tools.add("find_mail")
            if "list_email_accounts" in self.tools:
                forced_tools.add("list_email_accounts")
        if any(kw in u_lower for kw in ["schreib", "send", "antwort", "reply", "compose"]):
            if "send_mail" in self.tools:
                forced_tools.add("send_mail")

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

        # Multi-step / sequential tasks → activate orchestrator prompt module.
        # Keywords must be specific enough to not trigger on normal single-step requests.
        # Avoid generic terms like "plan", "compare", "alle", "batch", "multiple" —
        # these appear in everyday messages and cause unnecessary orchestrator activation.
        _multi_step_keywords = [
            "step by step", "schritt für schritt", "nacheinander", "sequentially",
            "for each file", "für jede datei", "für jeden eintrag", "for each item",
            "dann für jeden", "then for each",
            "alle dateien nacheinander", "one by one", "eines nach dem anderen",
        ]
        if any(kw in u_lower for kw in _multi_step_keywords):
            if hasattr(self, 'prompt_manager'):
                self.prompt_manager.activate_module("orchestrator")

        # Skill management: when the message is about skills, force the self-service skill tools — but
        # VERB-SCOPED so a turn only adds the relevant subset (stays well under router_max_tools). use_skill
        # is added too so a just-created/edited skill can be loaded right after. The user-isolation (own
        # skills only) is enforced inside each tool, not here.
        if "skill" in u_lower or "fähigkeit" in u_lower:
            def _force_skill_tools(*names):
                for _n in names:
                    if _n in self.tools:
                        forced_tools.add(_n)
            if any(kw in u_lower for kw in (
                "create", "new skill", "erstelle", "neue fähigkeit",
                "lerne", "learn", "as a skill", "als skill", "save as skill", "speichere als skill",
            )):
                _force_skill_tools("create_skill", "read_skill")
            elif any(kw in u_lower for kw in ("edit", "update", "change", "modify", "ändere", "bearbeite")):
                _force_skill_tools("update_skill", "read_skill", "list_skills")
            elif any(kw in u_lower for kw in ("delete", "remove", "lösche", "entferne")):
                _force_skill_tools("delete_skill", "list_skills")
            else:
                _force_skill_tools("list_skills", "read_skill")
            _force_skill_tools("use_skill")

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
                    # API Backend returns a generator of strings.
                    # Use a thread + timeout so slow/reasoning models don't block forever.
                    # NOTE: shutdown(wait=False) — we do NOT join the thread on timeout;
                    # the HTTP connection will eventually close on its own.
                    import concurrent.futures as _cf
                    _ROUTER_TIMEOUT = 15  # seconds

                    def _collect_router_chunks():
                        return list(self.api_backend.chat_completion(
                            messages=messages,
                            max_tokens=1224,
                            temperature=0.0,
                            stream=False
                        ))

                    _ex = _cf.ThreadPoolExecutor(max_workers=1)
                    _fut = _ex.submit(_collect_router_chunks)
                    try:
                        response_chunks = _fut.result(timeout=_ROUTER_TIMEOUT)
                    except _cf.TimeoutError:
                        _timeout_msg = f"Tool Router: Timeout nach {_ROUTER_TIMEOUT}s — falle zurück auf list_tools / search_tools"
                        UI.event("Router", _timeout_msg, style="yellow")
                        try:
                            from vaf.core.web_interface import get_web_interface as _gwi
                            _gwi().log(_timeout_msg, level="warning", source="Router", session_id=getattr(self, "current_session_id", None))
                        except Exception:
                            pass
                        _fallback = [t for t in ("list_tools", "search_tools") if t in self.tools]
                        return list(forced_tools) + [t for t in _fallback if t not in forced_tools]
                    finally:
                        _ex.shutdown(wait=False)
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
        valid_from_llm = []
        for name in tool_names:
            n = name.strip()
            if n in self.tools and n not in valid_from_llm:
                valid_from_llm.append(n)
        
        # Fallback: if LLM chatted instead of listing, scan response for tool name substrings.
        # When using reasoning models (DeepSeek Reasoner, R1) the tool decision lands inside
        # <think>…</think> blocks; clean_str is empty after stripping those.  Fall back to
        # scanning the original selected_tools_str so reasoning-model routing still works.
        _scan_target = clean_str if clean_str else selected_tools_str
        if not valid_from_llm and _scan_target:
            for t in sorted(self.tools.keys(), key=len, reverse=True):
                if t in _scan_target and t not in valid_from_llm:
                    valid_from_llm.append(t)
                if len(valid_from_llm) >= 20: # Safety cap
                    break
        
        from vaf.cli.ui import UI
        if forced_tools:
            UI.event("Router", f"Script-based: {', '.join(forced_tools)}", style="dim")
        if valid_from_llm:
            UI.event("Router", f"LLM-based: {', '.join(valid_from_llm)}", style="dim")
        elif tool_names and not valid_from_llm:
            UI.event("Router", "No tools selected (Router response was not a valid tool list)", style="dim")
        elif not forced_tools:
            UI.event("Router", "No tools selected", style="dim")
        
        # Final deduplication and merging with forced tools
        combined_set = set(valid_from_llm) | set(forced_tools or [])
        # Ensure we only return tools that actually exist
        valid_tools = [name for name in combined_set if name in self.tools]
        
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

    def _render_handoff_bundle(self, scope, req) -> "tuple[str, bool]":
        """For a reply to a background AUTOMATION handoff: load the linked bundle (THIS scope only) and
        render a BOUNDED digest of the automation's working context - its summary (the curated findings)
        plus a short tail of recent steps - for the main agent to continue with. Marks the bundle resolved.
        Returns (digest, curated): digest is '' when there is no bundle / it is missing / expired / for
        another scope / not from an automation (then the caller falls back to the plain-question note);
        curated is True only when the automation stored genuine findings (a summary). A mislabeled bundle
        (source != 'automation') is consumed and yields '' - defense in depth: on 2026-07-13 a thinking
        question stored as an 'automation' bundle framed a user reply as an automation continuation and
        steered the main agent into unintended actions. The full history stays in the bundle until it is
        resolved; only a bounded digest is injected, so the user's chat is never raw-dumped."""
        bundle_id = (req or {}).get("bundle_id") or ""
        if not bundle_id:
            return "", False
        try:
            from vaf.core import handoff_bundle as _hb
            bundle = _hb.load(scope, bundle_id)
        except Exception:
            return "", False
        if not bundle:
            return "", False
        if (bundle.get("source") or "").strip() != "automation":
            try:
                _hb.update_status(scope, bundle_id, "resolved")
            except Exception:
                pass
            return "", False
        parts = []
        summary = (bundle.get("summary") or "").strip()
        if summary:
            parts.append("What the background run worked out (use THIS, do not re-derive): " + summary[:2000])
        steps = []
        for m in (bundle.get("history") or []):
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role in (None, "system", "user"):
                continue
            content = m.get("content")
            if not isinstance(content, str):
                content = str(content) if content is not None else ""
            content = content.strip().replace("\n", " ")
            if not content:
                continue
            label = m.get("name") or role
            steps.append(f"- {label}: {content[:300]}")
        if steps:
            parts.append("Recent steps it took:\n" + "\n".join(steps[-8:])[:4000])
        try:
            _hb.update_status(scope, bundle_id, "resolved")
        except Exception:
            pass
        return "\n".join(parts).strip(), bool(summary)

    @staticmethod
    def _build_reply_pickup_note(q_text, carry, digest, curated, facts) -> str:
        """Build the system note injected when the user replies to a tracked background question.

        Three lanes (incident 2026-07-13: the old note asserted 'CONTINUE the task now' on an
        unvalidated bundle, with no decline or uncertainty lane - the model mutated state on a
        'nein bitte nicht'):
        - genuine rich handoff (digest + curated findings): automation framing, but continuation is
          reply-CONDITIONAL - agree = continue with this context, decline = change nothing, unclear =
          exactly one confirming question first;
        - handoff without curated findings: treated like a plain question (the digest is dropped -
          in the incident it was garbage), no automation-continuation claim;
        - plain background question: as before, plus the decline/uncertainty guidance.
        Pure function for unit-testability."""
        if digest and curated:
            return (
                f"[Context: The user's message below is a REPLY to a question a BACKGROUND "
                f"AUTOMATION of yours raised: \"{q_text}\". Their reply answers THAT question. "
                f"If they CLEARLY agree, continue the task now using THIS context - do not "
                f"re-derive facts, do not restart.{carry} If they DECLINE, change NOTHING - "
                f"acknowledge briefly. If their reply is ambiguous or seems unrelated to the "
                f"question, do NOT act: ask ONE short confirming question first.\n"
                f"{digest}\n"
                f"Answer about THAT question only. For THIS turn, IGNORE any earlier <user_intent> "
                f"or working-memory <Plan> shown above - they are unrelated. The user's reply "
                f"follows immediately after this system note.]"
            )
        return (
            f"[Context: The user's message below is a REPLY to a question YOUR background pass "
            f"asked them: \"{q_text}\".{facts}{carry} If they DECLINE, change NOTHING - "
            f"acknowledge briefly. If their reply is ambiguous or seems unrelated to the question, "
            f"do NOT act: ask ONE short confirming question first. Answer about THAT question "
            f"only. For THIS turn, IGNORE any earlier <user_intent> or working-memory <Plan> shown "
            f"above - they are unrelated to this reply and must not be treated as the topic. The "
            f"user's reply follows immediately after this system note.]"
        )

    def _ensure_image_base_descriptions(self, images: List[Dict]) -> None:
        """Generate the one-time base description for each freshly attached image (in place).

        Vision-as-a-tool design: the main reasoning model is text-only and never receives
        raw image bytes. On the upload turn we run each image ONCE through the vision backend
        to produce a neutral, comprehensive description, stored on the image dict as
        ``base_description``. _prepare_messages then injects that text every turn, and the
        model uses analyze_image to look closer on demand. Idempotent (skips images that
        already carry a description, so a reloaded image is not re-analysed). Never raises.

        Skipped in the legacy ``vision_mode="inline_multimodal"`` mode, where the main model
        sees the raw image directly and no description is needed.
        """
        try:
            if (Config.get("vision_mode", "description_tool") or "").strip() == "inline_multimodal":
                return
            pending = [
                img for img in (images or [])
                if isinstance(img, dict) and (img.get("data") or img.get("path")) and not img.get("base_description")
            ]
            if not pending:
                return
            from vaf.core.vision_infer import describe_image_cached
            _max = int(Config.get("vision_description_max_tokens", 1024) or 1024)
            for img in pending:
                # Shared process-wide cache so the Image Viewer's describe endpoint reuses this
                # (and vice versa) — the same image is never described/billed twice.
                desc = describe_image_cached(img, max_tokens=_max)
                if desc:
                    img["base_description"] = desc
        except Exception as e:
            try:
                from vaf.core.log_helper import append_domain_log
                append_domain_log("backend", f"[BASE_DESCRIPTION] generation failed: {e}")
            except Exception:
                pass

    def chat_step(
        self,
        user_input: str,
        stream_callback=None,
        auto_retry=False,
        skip_input=False,
        disable_workflows=False,
        disable_tools=False,
        memory_context=None,
        thinking_mode: bool = False,
        images: Optional[List[Dict]] = None,
        force_tool_choice: Optional[str] = None,
        allow_memory_search: bool = False,
    ):
        """Run one full turn: routing, LLM/tool loop, guardrails, persistence.

        The REAL answer is streamed via stream_callback and lands in
        history[-1]; the return value is a status (the "..." placeholder or a
        tool summary on normal completion, None when no backend is loaded,
        meaningful strings for workflow results / errors / stop / loop
        protection). Full return contract and parameter semantics:
        docs/CORE_AGENT.md. The vaf.Agent facade wraps this correctly.
        """
        from vaf.cli.ui import UI
        # Turn-local flag: avoids cross-thread leakage from process-wide env vars.
        self._current_turn_thinking_mode = bool(thinking_mode)
        # Forced-resolution node (thinking-mode decision tree): when the caller forces a tool call, also
        # block the gather tools immediately so "required" can ONLY be satisfied by a decisive/progress
        # tool (ask_user / delete_* / thinking_done) — the model cannot escape into search/prose.
        self._force_tool_choice = force_tool_choice if (force_tool_choice and thinking_mode) else None
        self._force_tool_choice_used = False  # force only the FIRST generation, then revert to auto
        self._thinking_force_progress = bool(self._force_tool_choice)
        # Proactive grounding exception: even in a forced node, let the model dig into ONE specific thing
        # with memory_search itself (read-capped). Set only for the proactive step in thinking_mode.
        self._thinking_allow_search = bool(allow_memory_search and thinking_mode)
        # Reset the thinking-reply context per turn so it persists across ALL generations of THIS turn
        # (set below from waiting_for_reply) but never leaks into the next turn.
        self._thinking_reply_context = None
        # Raw user reply of a pickup turn - the proactive-reply mutation gate needs it to decide
        # "clear affirmative" deterministically. Reset per turn with the context above.
        self._thinking_reply_user_text = None
        # Set at reply pickup when the user answers a tracked background question; consumed once at the
        # end-of-turn return to stash the main agent's own reply onto that request. Reset per turn here.
        self._thinking_reply_pending = None
        # Ask-first invariant: a REAL user message clears the pending-question latch; synthetic
        # background turns (runner drain sets _synthetic_drain_turn) must NOT clear it - that is
        # exactly the window in which new write actions stay blocked until the user answers.
        if user_input and not skip_input and not getattr(self, "_synthetic_drain_turn", False):
            self._pending_user_question = None
        
        try:
            append_domain_log("backend", "chat_step_start")
        except Exception:
            pass

        # Hot-reload: make newly-created tools live without a restart. Skills and
        # workflows self-refresh via their registry accessors (used by the router);
        # tool files need this explicit per-turn check (cheap, throttled, stdlib-only).
        if not disable_tools:
            self._maybe_refresh_dynamic_tools()

        self.context_manager.decay_state()

        # 🔒 NUDGE KILLER & CONTEXT SYNC: Clear background waiting status on ANY user interaction.
        if user_input and not skip_input:
            try:
                from vaf.core.thinking_mode import (
                    clear_waiting_for_reply, get_waiting_for_reply, set_waiting_for_reply, _is_presence_ack,
                )
                _scope = getattr(self, "_current_user_scope_id", None)

                # If we were waiting for a reply, store the original question as context so the Main
                # Agent understands vague replies (e.g. "Yes, why?"). We do NOT modify user_input here —
                # _prepare_messages() injects it as a system message the LLM sees but history never stores.
                waiting = get_waiting_for_reply(_scope)
                _presence_reentry = False
                if waiting and (waiting.get("question_text") or "").strip():
                    q_text = waiting["question_text"].strip()
                    _req_id = (waiting.get("request_id") or "").strip() or None
                    if waiting.get("nudge_sent_at_ts") and _is_presence_ack(user_input):
                        # The user replied to a PRESENCE nudge ("are you there?"), not the question itself.
                        # Do NOT record it as the answer: re-arm the wait (resets the nudge timer) and have
                        # the agent warmly RE-ASK its original question. The next real reply is captured
                        # normally. This only triggers AFTER a nudge was sent — a bare "ja" straight to the
                        # question (no nudge yet) is still handled as a normal answer below.
                        _presence_reentry = True
                        # Preserve the delivery channel + escalation state across the re-arm: the question
                        # was sent on the user's main messenger (or already escalated to web), and the reset
                        # nudge/escalation lifecycle must keep targeting that same channel — defaulting them
                        # back to "web" would silently move the follow-up nudge off the user's real channel.
                        set_waiting_for_reply(
                            _scope,
                            username=waiting.get("username") or "admin",
                            display_name=waiting.get("display_name") or waiting.get("username") or "admin",
                            question_text=q_text,
                            request_id=_req_id or None,
                            session_id=waiting.get("session_id"),
                            channel=waiting.get("channel") or "web",
                            escalated_to_web=bool(waiting.get("escalated_to_web")),
                        )
                        self._thinking_reply_context = (
                            f"[Context: You earlier asked the user a background question and then sent a "
                            f"presence nudge ('are you there?'). Their message below only signals they are "
                            f"back — it is NOT an answer to your question. Warmly welcome them back and "
                            f"RE-ASK your question in ONE short, friendly line: \"{q_text}\". Do not treat "
                            f"their message as the answer, and do not start a new topic.]"
                        )
                    else:
                        # If this was a tracked background request (ask_user): do NOT keyword-classify the
                        # reply here. Capture it (status -> 'replied') and let the NEXT thinking run classify
                        # the outcome from the full triple {question, user reply, the main agent's own reply}.
                        # The main agent still ACTS immediately on a clear yes via _carry below — only the
                        # accepted-vs-declined bookkeeping is deferred to the run that owns the question.
                        _action = ""
                        _req = None
                        if _req_id:
                            try:
                                from vaf.core import thinking_requests as _treq
                                _req = _treq.get_request(_scope, _req_id)
                                _action = (_req or {}).get("proposed_action") or ""
                                _treq.record_reply(_scope, _req_id, user_reply=user_input)
                                self._thinking_reply_pending = {"scope": _scope, "request_id": _req_id}
                            except Exception:
                                pass
                        self._thinking_reply_user_text = user_input
                        _carry = f" If they CLEARLY confirm, carry out this proposal now: {_action}." if _action else ""
                        # If this request came from a background AUTOMATION handoff, it links to a bundle that
                        # holds the automation agent's FULL working context. Load it (same scope only) and
                        # integrate it deliberately: a concise note + a BOUNDED digest of the run's summary +
                        # recent steps - so the main agent continues with full context, not a raw transcript
                        # dumped into the chat. A missing/expired/wrong-scope/mislabeled bundle falls back to
                        # the plain-question note (no automation framing).
                        _bundle_ctx, _bundle_curated = (
                            self._render_handoff_bundle(_scope, _req) if _req else ("", False)
                        )
                        # Concrete content the background run gathered behind a teaser message (e.g. the actual
                        # list of tips). Hand it to the main agent so a follow-up ("which ones? list them") is
                        # answered with the REAL findings - not a made-up version (observed 2026-06-22: the
                        # main agent invented incoherent cooling tips because the content was never passed).
                        _details = (_req or {}).get("details") or ""
                        if _details:
                            _facts = (f" The concrete information behind that message - use THIS to answer their "
                                      f"reply, do not invent new facts: {_details}.")
                        else:
                            _facts = (" You do not have the specifics on hand; if the user asks for details, look "
                                      "them up (e.g. web_search) - do NOT make up facts.")
                        self._thinking_reply_context = self._build_reply_pickup_note(
                            q_text, _carry, _bundle_ctx, _bundle_curated, _facts
                        )
                        # Observability (incident lesson: the injected note is mid-list and appears in
                        # no prompt log - the next incident should be a grep, not an inference).
                        try:
                            _lane = ("handoff" if (_bundle_ctx and _bundle_curated)
                                     else ("handoff_uncurated" if _bundle_ctx else "plain"))
                            append_domain_log(
                                "prompt",
                                f"[REPLY_CTX] lane={_lane} req={_req_id} "
                                f"len={len(self._thinking_reply_context or '')} q_head={q_text[:120]!r}",
                            )
                        except Exception:
                            pass
                else:
                    self._thinking_reply_context = None

                # If we're interacting, we're definitely not waiting anymore — UNLESS we just re-armed the
                # wait for a presence re-entry (then keep it open so the user's REAL answer is captured).
                # Pass the input text so it gets saved for the NEXT thinking run summary.
                if not _presence_reentry:
                    clear_waiting_for_reply(_scope, user_reply_text=user_input)
            except Exception:
                pass

        # Check if any backend is available (local, server, or API)
        if not self.llm and not self.use_server and not self.api_backend:
            UI.error("Agent not initialized. Run 'vaf run' first.")
            return
        
        # Clean old workflow messages from context (prevents clutter)
        self._clean_workflow_context()

        # Initialize context info at the start so it's available in all branches
        current_tokens, max_tokens = self.get_token_usage()

        # ------------------------------------------------------------------
        # Sub-Agent Results: Check for completed async tasks
        # ------------------------------------------------------------------
        # Result-ownership: when the headless runner owns delivery (web/desktop) and this
        # is a USER chat turn, leave pending results for the runner's idle drain — it
        # delivers with ALL side effects (SubAgentWindow "Completed", subagent_output,
        # messenger summaries), which this in-chat path skips. Consuming here would mix
        # the sub-agent handoff into a casual reply and silently drop those signals
        # (chat-while-subagent-runs). CLI/no-runner contexts keep the in-chat drain.
        if user_input and getattr(self, "_runner_owns_subagent_delivery", False):
            pending_results = []
        else:
            pending_results = self._check_subagent_results()
        any_needs_retry = False
        if pending_results:
            for task in pending_results:
                if self._process_subagent_result(task):
                    any_needs_retry = True
            
            # If we processed results and no new user input, let model respond to results
            if not user_input and not skip_input:
                 # Only auto-inject generic prompt if we DON'T have a specific input
                 # and we're not in a skip_input mode (which usually implies internal control)
                 if any_needs_retry:
                    self.history.append({
                        "role": "user",
                        "content": (
                            "[System: The sub-agent result did NOT fulfill the user's request. "
                            "You MUST retry immediately by calling the sub-agent again with the exact task specified in the Background Intelligence above. "
                            "Do NOT summarize. Call the tool now.]"
                        )
                    })
                 else:
                    self.history.append({
                        "role": "user",
                        "content": "[System: Sub-Agent results have arrived. Please inform me about the results.]"
                    })

        # ------------------------------------------------------------------
        # Dynamic Context: Update System Prompt
        # ------------------------------------------------------------------
        new_prompt = self.history[0].get("content", "") if self.history and self.history[0].get("role") == "system" else None
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
                front_office=getattr(self, "_front_office_mode", False),
                session_id=getattr(self, "current_session_id", None),
            )
        
        # ------------------------------------------------------------------
        # Context Compression: Check threshold and compress if needed
        # ------------------------------------------------------------------
        compression_happened = False
        if hasattr(self, 'context_manager') and self.context_manager.should_compress(self.history):
            UI.event("Context", f"Threshold reached ({self.context_manager.get_usage_percent(self.history):.0%}) - compressing...", style="warning")
            working_memory = None
            if getattr(self, "main_persistence", None):
                try:
                    working_memory = self.main_persistence.get_working_memory()
                except Exception:
                    working_memory = None
            self.history = self.context_manager.compress(self.history, working_memory=working_memory)
            compression_happened = True
            
        # Apply updated system prompt + context glue + project context
        if new_prompt is not None and len(self.history) > 0 and self.history[0].get("role") == "system":
            # 1. Add Context Glue
            # We add glue if we have a summary OR if context is getting full
            context_glue = self.context_manager._build_context_summary()
            if context_glue and (
                self.context_manager.state.narrative_summary or 
                self.context_manager.get_usage_percent(self.history) > 0.3
            ):
                if context_glue not in new_prompt:
                    new_prompt += f"\n\n{context_glue}"
            
            # 2. Preserve Project Context (always keep at the bottom of system prompt)
            current_content = self.history[0]["content"]
            if "## PROJECT CONTEXT" in current_content and "## PROJECT CONTEXT" not in new_prompt:
                project_context_part = current_content.split("## PROJECT CONTEXT", 1)[1]
                new_prompt = new_prompt.strip() + f"\n\n## PROJECT CONTEXT{project_context_part}"
            
            # 3. Final Apply
            self.history[0]["content"] = new_prompt

        # Keep language pinned to the user's most recent message.
        # This must happen early so it affects workflow selection + normal chat replies.
        if not skip_input:
            self._refresh_language_hint(user_input)
            
        # Broadcast context status to WebUI (X-Ray Vision)
        self._broadcast_context_status()

        # 0. Context Management (Trim/Summarize) - BEFORE adding user input
        # This ensures we have space for the new message and system prompt updates
        self.manage_context()
        
        # ═══════════════════════════════════════════════════════════════════════
        # WORKFLOW ENGINE: ENABLED - Try to match workflow templates first
        # ═══════════════════════════════════════════════════════════════════════
        # If a workflow matches (confidence >= 50%), execute it automatically
        # Otherwise, fall back to LLM agent for flexible handling
        # Workflows provide structured, multi-step pipelines for common tasks
        
        workflow_tried = False
        # Never run the workflow router during a background thinking run: it would match the thinking
        # PROMPT itself (which contains phrases like "automatisch um 7:00"/"create automation") and prepend
        # a "[WORKFLOW SUGGESTION] Create Scheduled Task" nudge — with garbage variables (every var "07:00")
        # — steering the run to fabricate a "create a timer" proposal. The thinking run has no user request
        # to route; it only does its own housekeeping. Use the turn-local param, not the env var (the env
        # var can leak across concurrent threads).
        if not skip_input and not disable_workflows and not thinking_mode:
            workflow_tried = True
            # Try workflow matching BEFORE adding to history
            workflow_result = self._try_workflow(user_input, stream_callback)
            if workflow_result:
                # Workflow executed successfully (explicit @workflow_id path) - return result
                return workflow_result
            # No workflow match or workflow set hint - continue with LLM agent

            # If router found a relevant workflow, inject it as a suggestion so the
            # agent can decide whether to use it based on full conversation context.
            _wf_hint = getattr(self, "_pending_workflow_hint", None)
            if _wf_hint:
                self._pending_workflow_hint = None  # one-shot — clear immediately
                _vars_repr = ", ".join(
                    f'{k}="{v}"' for k, v in (_wf_hint.get("variables") or {}).items()
                ) or "no variables pre-extracted"
                _wf_note = (
                    f"[WORKFLOW SUGGESTION] The workflow \"{_wf_hint['name']}\" "
                    f"({_wf_hint['workflow_id']}) looks relevant to this request.\n"
                    f"Pre-extracted variables: {_vars_repr}\n"
                    f"To start it call: execute_workflow(workflow_id=\"{_wf_hint['workflow_id']}\", "
                    f"variables={{...}})\n"
                    f"IMPORTANT: If the user is asking to edit or modify an existing project "
                    f"(see [SESSION WORKSPACE] above), use coding_agent with project_path instead "
                    f"— do NOT start a creation workflow.\n\n"
                )
                user_input = _wf_note + user_input

            # Skills are the second routing tier. If the router matched a SKILL
            # (and not a workflow), surface it the same way: the router only saw
            # name+description, so we point the agent at use_skill, which loads the
            # full instructions on demand (progressive disclosure). One-shot, cleared
            # immediately. Mutually exclusive with the workflow hint by construction
            # (a skill match is set only when no workflow matched).
            _sk_hint = getattr(self, "_pending_skill_hint", None)
            if _sk_hint:
                self._pending_skill_hint = None  # one-shot — clear immediately
                # Pin use_skill into the active tool set for this turn (the router
                # runs just below) so the agent can actually load the skill.
                self._skill_tool_needed_this_turn = True
                _sk_note = (
                    f"[SKILL SUGGESTION] The skill \"{_sk_hint['name']}\" "
                    f"({_sk_hint['skill_id']}) looks relevant to this request.\n"
                    f"To load its full instructions call: use_skill(skill_id=\"{_sk_hint['skill_id']}\")\n"
                    f"Then follow the instructions and read any bundled files it references.\n\n"
                )
                user_input = _sk_note + user_input

        # Always add user input if provided, even if skip_input=True (which skips analysis/overhead)
        if user_input:
            _user_msg: Dict = {"role": "user", "content": user_input}
            if images:
                # Generate a one-time base description per image so the (text-only) main
                # model and every later turn stay grounded without re-sending raw bytes.
                # The model inspects the stored image on demand via the analyze_image tool.
                self._ensure_image_base_descriptions(images)
                _user_msg["images"] = images  # [{data, mime_type, name, base_description}] — see _prepare_messages
            self.history.append(_user_msg)
            self._orchestrator_heavy_calls_this_turn = 0  # New turn: reset heavy-tool budget for orchestrator gate
            self._plan_gate_blocks = 0  # New turn: fresh plan-gate budget
            self._anti_spin_streak = 0  # New turn: fresh anti-spin streak
            # New turn: age finished team entries; "done HH:MM" lingers a few turns then drops.
            if hasattr(self, 'main_persistence') and self.main_persistence:
                try:
                    self.main_persistence.tick_team_state()
                except Exception:
                    pass

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
                                from vaf.vendor import langid
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
            # CRITICAL: Skip intent update if running in thinking mode (background).
            # This prevents technical thinking prompts from overwriting the actual user intent.
            if hasattr(self, 'main_persistence') and self.main_persistence:
                _is_thinking_mode = bool(thinking_mode)
                if not _is_thinking_mode:
                    try:
                        # Update the "North Star" for the session
                        self.main_persistence.update_user_intent(user_input)
                        self.main_persistence.reset_validation_retry_count()
                    except Exception:
                        pass
            
            # LIVE CONTEXT UPDATE: Ensure intent is fresh for the router immediately
            if hasattr(self, 'context_manager'):
                _is_thinking_mode = bool(thinking_mode)
                if not _is_thinking_mode:
                    self.context_manager.update_intent(user_input)
                self.context_manager.update_state({"role": "user", "content": user_input})
        
        # 0.5 Context Management - AFTER adding user input and results
        # Ensures that the context is optimized before we route tools and call the LLM
        self.manage_context()

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

            # list_tools and search_tools are ALWAYS included when we have a restricted set
            # so the model can discover other tools on-demand (provider-agnostic Tool Search).
            for _discovery_tool in ("list_tools", "search_tools"):
                if selected_tools and _discovery_tool in self.tools and _discovery_tool not in selected_tools:
                    selected_tools = list(selected_tools) + [_discovery_tool]

            # Memory/identity tools are ALWAYS included when we have a restricted set (no duplicates).
            # Only skipped when Safety Net = ALL tools (would be redundant).
            if selected_tools:
                # Convert to set for efficient deduplication
                tools_set = set(selected_tools)
                
                # Add core tools
                for name in ("update_intent", "update_working_memory", "memory_search", "memory_save", "update_user_identity", "set_timer"):
                    if name in self.tools:
                        tools_set.add(name)
                
                # Messaging tools: only add those for which the user has the connection
                try:
                    from vaf.core.messaging_connections import get_messaging_connections
                    conn = get_messaging_connections(
                        username=getattr(self, "_current_username", None),
                        user_scope_id=getattr(self, "_current_user_scope_id", None),
                    )
                    for ch in conn.get("available") or []:
                        tool_name = {"telegram": "send_telegram", "discord": "send_discord", "slack": "send_slack", "whatsapp": "send_whatsapp"}.get(ch)
                        if tool_name and tool_name in self.tools:
                            tools_set.add(tool_name)
                    # Channel-agnostic delivery: pinned whenever ANY messenger is
                    # connected (it resolves the platform itself at run time).
                    if (conn.get("available") or []) and "send_to_user" in self.tools:
                        tools_set.add("send_to_user")
                except Exception:
                    pass
                
                # Convert back to list
                selected_tools = list(tools_set)

            # Cap the number of tools to keep the context window clean.
            # list_tools / search_tools are pinned and don't count against the cap.
            _router_max = int(self.config.get("router_max_tools", 12))
            if selected_tools and len(selected_tools) > _router_max:
                _pinned = {t for t in ("list_tools", "search_tools") if t in selected_tools}
                _rest = [t for t in selected_tools if t not in _pinned]
                selected_tools = list(_pinned) + _rest[:max(0, _router_max - len(_pinned))]

            # SAFETY NET: If router returns empty list, fallback to sensible tools
            # Otherwise the model gets 0 tools and hallucinates using them.
            used_core_subset = False
            if not selected_tools:
                # If context is tight, use CORE_TOOLS subset to avoid HTTP 400 / overflow
                is_small = max_tokens <= 20000
                router_safety_threshold = 0.75 if is_small else 0.85
                
                if current_tokens > (max_tokens * router_safety_threshold):
                    CORE_TOOLS = [
                        "web_search", "memory_search", "memory_save", "list_tools", "search_tools",
                        "update_intent", "update_working_memory", "read_file", "list_files",
                        "coding_agent", "librarian_agent", "research_agent"
                    ]
                    fallback_set = [t for t in CORE_TOOLS if t in self.tools]
                    UI.event("Router", f"Safety Net: Context tight ({current_tokens}/{max_tokens}). Using {len(fallback_set)} Core tools.", style="warning")
                    self._active_tools = fallback_set
                    used_core_subset = True
                else:
                    # Router found no specific tools: give only discovery tools so the model can list/search
                    DISCOVERY_ONLY = ["list_tools", "search_tools"]
                    self._active_tools = [t for t in DISCOVERY_ONLY if t in self.tools]
                    UI.event("Router", "Safety Net: Router found none. Using list_tools, search_tools.", style="dim")
            else:
                self._active_tools = selected_tools

            # If the router suggested a skill this turn, make sure use_skill is in the
            # active set so the agent can actually load it (the [SKILL SUGGESTION] told
            # it to). Pinned after the cap, like the discovery tools. When _active_tools
            # is None (ALL tools) it is already available.
            if getattr(self, "_skill_tool_needed_this_turn", False):
                self._skill_tool_needed_this_turn = False
                if (
                    "use_skill" in self.tools
                    and self._active_tools is not None
                    and "use_skill" not in self._active_tools
                ):
                    self._active_tools = list(self._active_tools) + ["use_skill"]

            # Show final tools once in Web UI (single source; CLI/router logs above already show selection)
            actual_tools = self._active_tools if self._active_tools is not None else list(self.tools.keys())
            final_list = ", ".join(actual_tools)
            if self._active_tools is None:
                final_list = f"ALL ({len(actual_tools)})"
            elif not selected_tools and used_core_subset:
                final_list = f"CORE ({len(actual_tools)})"
            # Single display path: push to Web UI only (avoids duplicate with UI.event→log in Web)
            if _emit_to_web_ui():
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
        else:
            # On retries or for internal steps, use a slightly more generous set if ALL doesn't fit
            self._active_tools = None
            is_small = max_tokens <= 20000
            internal_threshold = 0.80 if is_small else 0.90
            
            if current_tokens > (max_tokens * internal_threshold):
                # Emergency fallback for internal steps/retries
                UI.event("Router", f"Tight context in internal step ({current_tokens}/{max_tokens}). Using internal subset.", style="warning")
                self._active_tools = [t for t in ["web_search", "memory_search", "list_tools", "search_tools", "update_intent", "read_file", "list_files"] if t in self.tools]

        if not skip_input and user_input:
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
        # Loop-protection counter for blocked redundant tool calls (per user turn). Kept SEPARATE from
        # empty_retry_count so a redundant block never climbs into the empty-response abort.
        redundant_block_count = 0
        # API empty guard: delay-retry (3s) up to 4 times before showing system-log error
        api_empty_delay_retries = 0
        API_EMPTY_DELAY_RETRIES_MAX = 4
        current_temp = target_temp
        
        # Main chat loop with retries for empty responses
        full_response = ""
        full_content = ""
        full_reasoning = ""
        clean_content = ""
        streaming_tools = {}
        tool_calls_detected = []
        
        # Tool Loop Protection: Max number of tool-result cycles in one interaction
        # Normal interactions usually need 1-3 tool turns.
        tool_turn_count = 0
        SOFT_LIMIT_TOOL_TURNS = 50   # Inject goal-reminder, agent continues
        MAX_TOOL_TURNS_PER_STEP = 75  # Hard kill (or user-inform + ask-to-continue)
        # A BACKGROUND thinking run must not churn the way the main chat may — it is a short
        # gather/decide/act pass, so cap it far tighter (default 15) to stop tool-spin loops.
        if os.environ.get("VAF_THINKING_MODE", "").strip() in ("1", "true", "yes"):
            MAX_TOOL_TURNS_PER_STEP = max(2, int(Config.get("thinking_max_tool_turns", 15) or 15))
            SOFT_LIMIT_TOOL_TURNS = max(1, MAX_TOOL_TURNS_PER_STEP - 3)
        _hard_stop_injected = False   # True after hard-stop user message was injected once
        self._autocontinue_step_sig = None  # reset the task-stuck guard at the start of each run
        self._autocontinue_stuck = 0
        self._thinking_read_counts = {}  # reset the thinking read-tool cap (per-step, thinking mode only)
        _ww_reactive_injected = set()  # tools whose learned know-how was already re-fed on error this turn
        # Wall-clock BACKSTOP (MAIN loop only): a single user turn can never grind indefinitely regardless of
        # tool count or provider speed. Checked at the TURN BOUNDARY (before the next LLM call), never mid-tool,
        # so a legitimately long self-supervised tool is not aborted. Deliberately GENEROUS (default 1h) — the
        # no-progress guard + per-tool timeouts stop the common case far earlier; this only catches a true
        # infinite/zombie loop and never aborts legitimate long work. (Thinking mode has its own tighter caps.)
        _turn_deadline = time.monotonic() + float(Config.get("chat_step_wall_clock_seconds", 3600) or 3600)
        self._nonprogress_streak = 0  # consecutive turns that used ONLY read-only/verify tools (no real progress)

        while empty_retry_count < MAX_EMPTY_RETRIES:
            # Stop check at the top of every loop iteration — catches stop clicks
            # that happen between tool execution and the next LLM call
            _loop_session = getattr(self, 'current_session_id', None) or getattr(self, '_session_id', None)
            if _loop_session:
                try:
                    from vaf.core.task_queue import TaskQueue as _LTQ
                    _ltq = _LTQ()
                    if _ltq.should_stop(_loop_session):
                        _ltq.clear_stop(_loop_session)
                        _stop_msg = "[Generation stopped by user]"
                        self.history.append({"role": "assistant", "content": _stop_msg})
                        return _stop_msg
                except Exception:
                    pass

            # 1. Prepare Request
            # Recalculate token usage at the start of each retry attempt
            current_tokens, max_tokens = self.get_token_usage()

            full_response = ""     # Reset for this turn
            full_content = ""      # Reset for this turn
            full_reasoning = ""    # Reset for this turn
            _generation_stopped = False  # Track if user stopped generation

            streaming_tools = {}
            tool_calls_detected = []
            # Anthropic only: raw assistant content blocks (thinking + tool_use, with
            # signatures) for verbatim replay so a thinking-enabled tool loop doesn't 400.
            anthropic_blocks_raw = None
            auto_continue = False  # Track if response was cut off

            # When we're about to stream the next assistant turn after tool execution,
            # clear the Web UI stream buffer so only the final reply is shown (not the
            # pre-tool text like "Ich werde get_contact nutzen..." plus tool blocks).
            if len(self.history) > history_snapshot_len + 1 and self.history[-1].get("role") == "tool":
                if stream_callback and hasattr(stream_callback, "clear"):
                    try:
                        stream_callback.clear()
                    except Exception:
                        pass

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
                                    tc["id"] = _synth_tool_call_id()
                    
                    
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
                        # Merge the memory context INTO the first system message instead of inserting a
                        # SECOND system message. Strict local chat templates (e.g. Qwen, Gemma) reject a
                        # second / non-leading system turn ("System message must be at the beginning");
                        # a single system turn is also fine for every other provider.
                        if prepared_messages and prepared_messages[0].get("role") == "system":
                            prepared_messages[0]["content"] = (prepared_messages[0].get("content") or "") + "\n\n" + memory_msg["content"]
                        else:
                            prepared_messages = [prepared_messages[0], memory_msg] + prepared_messages[1:]
                    # Disable tools if requested
                    current_tools = self.TOOLS if not disable_tools else None
                    tool_choice = "auto" if current_tools else "none" # Default to auto if tools, none otherwise
                    if current_tools and getattr(self, "_force_tool_choice", None) and not self._force_tool_choice_used:
                        tool_choice = self._force_tool_choice  # forced-resolution node: model MUST emit a tool
                        self._force_tool_choice_used = True     # only the first generation is forced

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
                                if isinstance(data, dict) and any(k in data for k in ["tool_calls", "finish_reason", "tool_use", "_anthropic_blocks"]):
                                    is_control_msg = True

                                    # Anthropic: raw assistant content blocks for verbatim replay
                                    # (preserves thinking blocks + signatures in the tool loop).
                                    if "_anthropic_blocks" in data:
                                        anthropic_blocks_raw = data["_anthropic_blocks"]
                                    # Handle Finish Reason
                                    elif "finish_reason" in data:
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
                                            "id": data["tool_use"].get("id", _synth_tool_call_id()),
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
                            tc_data["id"] = _synth_tool_call_id()
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
                
                # Reserve space for response (VRAM efficiency / Thinking headroom)
                # For small contexts (e.g. 11k-16k), we need more % for output
                is_small = max_tokens <= 20000
                buffer_percent = 0.25 if is_small else 0.10
                response_buffer = max(2000 if is_small else 1500, int(max_tokens * buffer_percent))
                safe_limit = max_tokens - response_buffer
                
                if current_tokens > safe_limit:
                    UI.event("Context", f"Proactive compression ({current_tokens}/{max_tokens}, buffer={response_buffer})", style="warning")
                    self.manage_context()
                    
                    # Double-check after compression
                    current_tokens, _ = self.get_token_usage()
                    if current_tokens > safe_limit:
                        # Still too big - aggressive pruning needed
                        UI.event("Context", "Standard compression insufficient. Pruning aggressively...", style="warning")
                        
                        # Emergency tool reduction
                        if self._active_tools is None or len(self._active_tools) > 15:
                             UI.event("Context", "Tight context: Using CORE tool subset to save VRAM.", style="warning")
                             CORE_FALLBACK = ["web_search", "memory_search", "memory_save", "list_tools", "update_intent", "read_file", "list_files", "librarian_agent", "coding_agent"]
                             self._active_tools = [t for t in CORE_FALLBACK if t in self.tools]
                             # Re-calculate after tool reduction
                             current_tokens, _ = self.get_token_usage()

                        if current_tokens > safe_limit:
                            # Keep only system + fewer messages for small context
                            keep_count = 4 if is_small else 6
                            if len(self.history) > keep_count + 1:
                                system_msg = [self.history[0]] if self.history and self.history[0].get("role") == "system" else []
                                self.history = system_msg + self.history[-keep_count:]
                                UI.event("Context", f"Aggressive reduction: kept system + {keep_count} recent messages", style="info")
                                # Notify UI
                                self._broadcast_context_status()
                
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
                            # Merge the memory context INTO the first system message instead of inserting a
                            # SECOND system message. Strict local chat templates (e.g. Qwen, Gemma) reject a
                            # second / non-leading system turn ("System message must be at the beginning");
                            # a single system turn is also fine for every other provider.
                            if prepared_messages and prepared_messages[0].get("role") == "system":
                                prepared_messages[0]["content"] = (prepared_messages[0].get("content") or "") + "\n\n" + memory_msg["content"]
                            else:
                                prepared_messages = [prepared_messages[0], memory_msg] + prepared_messages[1:]
                        # Disable tools if requested (forces text response)
                        current_tools = self.TOOLS if not disable_tools else None
                        current_tool_choice = "auto" if not disable_tools else "none"
                        if current_tools and getattr(self, "_force_tool_choice", None) and not self._force_tool_choice_used:
                            current_tool_choice = self._force_tool_choice  # forced-resolution node
                            self._force_tool_choice_used = True             # only the first generation is forced

                        payload = {
                             "messages": prepared_messages,
                             "tools": current_tools,
                             "tool_choice": current_tool_choice,
                             "stream": True,
                             "temperature": current_temp,
                             # Local llama-server sampling. A repetition penalty + top_p/top_k stop the
                             # model degenerating into a verbatim loop (observed: a 60k-token <think> that
                             # repeated the same paragraph until it overflowed the context). max_tokens
                             # caps a single generation so even a loop that slips through is bounded.
                             # These are llama.cpp extensions; this payload only ever goes to :8080.
                             "repeat_penalty": float(Config.get("repeat_penalty", 1.1) or 1.1),
                             "top_p": float(Config.get("top_p", 0.95) or 0.95),
                             "top_k": int(Config.get("top_k", 40) or 40),
                             "max_tokens": int(Config.get("max_generation_tokens", 10000) or 10000),
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

                            # SECURITY (user isolation): this X-ray carries the FULL prepared
                            # prompt (system prompt incl. the "## Memory context" RAG block) and
                            # the whole message history of the current turn. Route it to the turn
                            # owner's connections only, fail-closed - never a global broadcast.
                            _xray_scope = getattr(self, "_current_user_scope_id", None)
                            if _emit_to_web_ui() and _xray_scope:
                                get_web_interface().push_update_to_user(_xray_scope, {
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
                                is_small = max_tokens <= 20000
                                trunc_limit = 3000 if is_small else 6000
                                truncated = False
                                for msg in self.history:
                                    if msg.get("role") != "system":
                                        content = str(msg.get("content", ""))
                                        if len(content) > trunc_limit:
                                            msg["content"] = content[:trunc_limit] + "... [TRUNCATED FOR RECOVERY]"
                                            truncated = True
                                
                                if truncated:
                                    UI.event("Context", f"Truncated messages to {trunc_limit} chars.", style="info")

                                # 3. Message Pruning (existing logic)
                                if len(self.history) > (10 if is_small else 20):
                                    # Keep system prompt + last N messages (preserving order and alternation)
                                    keep_n = 4 if is_small else 6
                                    new_history = []
                                    
                                    # 1. System Prompt
                                    if self.history and self.history[0].get("role") == "system":
                                        new_history.append(self.history[0])
                                    
                                    # 2. Last N messages (User/Assistant/Tool)
                                    recent = self.history[-keep_n:]
                                    
                                    # Truncate heavy messages in the recent block
                                    small_trunc = 1000 if is_small else 2000
                                    for msg in recent:
                                        msg_copy = msg.copy()
                                        content = str(msg_copy.get("content", ""))
                                        if len(content) > small_trunc:
                                             msg_copy["content"] = content[:small_trunc] + "... [TRUNCATED]"
                                        new_history.append(msg_copy)
                                    
                                    self.history = new_history
                                    UI.event("Context", f"Pruned to system + {len(recent)} messages.", style="info")
                                
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
                                    
                                    # 2. Aggressive Content Truncation
                                    is_small = max_tokens <= 20000
                                    trunc_limit = 3000 if is_small else 6000
                                    truncated = False
                                    for msg in self.history:
                                        if msg.get("role") != "system": # Protect system prompt
                                            content = str(msg.get("content", ""))
                                            if len(content) > trunc_limit:
                                                msg["content"] = content[:trunc_limit] + "... [TRUNCATED FOR RECOVERY]"
                                                truncated = True
                                    
                                    if truncated:
                                        UI.event("Context", f"Truncated messages to {trunc_limit} chars.", style="info")

                                    # 3. Message Pruning
                                    if len(self.history) > (10 if is_small else 20):
                                        keep_n = 4 if is_small else 6
                                        new_history = []
                                        
                                        # 1. System Prompt
                                        if self.history and self.history[0].get("role") == "system":
                                            new_history.append(self.history[0])
                                        
                                        # 2. Last N messages
                                        recent = self.history[-keep_n:]
                                        
                                        # Truncate heavy messages
                                        small_trunc = 1000 if is_small else 2000
                                        for msg in recent:
                                            msg_copy = msg.copy()
                                            content = str(msg_copy.get("content", ""))
                                            if len(content) > small_trunc:
                                                 msg_copy["content"] = content[:small_trunc] + "... [TRUNCATED]"
                                            new_history.append(msg_copy)
                                        
                                        self.history = new_history
                                        UI.event("Context", f"Pruned to system + {len(recent)} messages.", style="info")
                                    
                                    UI.event("Context", "Retrying request with optimized context...", style="success")
                                    # Retry the request with compressed context (payload will be rebuilt in next iteration)
                                    continue
                            except (json.JSONDecodeError, KeyError):
                                pass  # Not a context size error, try generic 400 recovery below
                            # A 400 that is NOT about context size (e.g. Qwen's "Assistant response prefill
                            # is incompatible with enable_thinking") cannot be fixed by compressing -- retrying
                            # just burns turns and ends in the same error. Only compress-retry a size 400;
                            # surface anything else immediately.
                            _err_low = ""
                            try:
                                _err_low = str((response.json().get("error", {}) or {}).get("message", "") or "").lower()
                            except Exception:
                                _err_low = str(getattr(response, "text", "") or "").lower()
                            _is_size_400 = (not _err_low) or ("exceed" in _err_low) or (
                                "context" in _err_low and ("size" in _err_low or "length" in _err_low or "token" in _err_low)
                            )
                            if _attempt < 3 and _is_size_400:
                                UI.event("Context", "Request rejected (400). Compressing context...", style="warning")
                                self.manage_context()
                                is_small = max_tokens <= 20000
                                user_trunc = 4000 if is_small else 8000
                                for msg in reversed(self.history):
                                    if msg.get("role") == "user":
                                        content = str(msg.get("content", ""))
                                        if len(content) > user_trunc:
                                            msg["content"] = content[:user_trunc] + "\n\n... [Document content truncated to fit context]"
                                            UI.event("Context", f"Truncated user message to {user_trunc} chars.", style="info")
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
                                        # The WebUI/CLI will parse them.
                                        # TODO(gemma): raw Gemma-4 <|tool_call> tokens (rare -- only when
                                        # the server doesn't convert them) likewise stream raw and can
                                        # briefly flash in the UI. Hide them at the same display/parse
                                        # layer that strips <think> (not here: a call spans chunks). The
                                        # stored answer is already cleaned in _sanitize_response. Accepted
                                        # cosmetic limitation for now.
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
            # Check if model claimed to use a tool but didn't emit a tool call.
            # DISABLED BY DEFAULT for all models: the forced retry on a heuristic/<Action> match caused
            # more harm than good (retry loops, false positives -- especially on weak local models).
            # Opt back in via config `false_promise_detection_enabled: true`. Only runs with content + NO tools.
            if (Config.get("false_promise_detection_enabled", False)
                    and not streaming_tools and not tool_calls_detected and full_content.strip()):
                # Measure only the USER-VISIBLE answer: weak local models (e.g. Gemma)
                # stream their reasoning inline as <think>...</think> in the content
                # field, which would otherwise inflate the length and trip the >800
                # skip below — hiding genuinely short false promises behind a long
                # thinking block.
                _visible = re.sub(r'<think>[\s\S]*?</think>', '', full_content, flags=re.IGNORECASE).strip()
                _response_len = len(_visible)

                # High-confidence signal: the model committed to a tool in its <Action>
                # block — which per the system prompt is emitted ONLY right before a tool
                # call — but then emitted no call at all. This is a definitional false
                # promise, far more reliable than the free-text heuristic, and it is what
                # catches "Using web_search to find the weather..." with no call behind it.
                _act_intent = _extract_action_text(full_response)
                if _act_intent:
                    _avail_fp = list(self._active_tools) if self._active_tools else list(self.tools.keys())
                    _act_matches = _match_action_to_tools(_act_intent, _avail_fp)
                else:
                    _act_matches = []
                _action_promise = bool(_act_matches and _act_matches[0][1] >= 0.7)
                _promised_tool = _act_matches[0][0] if _action_promise else None

                # False promises are always short (1-2 sentences like "Let me search...").
                # A long analytical/conversational response is never a false promise —
                # skip detection to avoid trapping the agent in a retry loop. An explicit
                # <Action> commitment overrides the skip (the length is just thinking).
                _skip_fp_detection = (_response_len > 800) and not _action_promise
                if not _skip_fp_detection and (_action_promise or self._detect_false_tool_promise(_visible, tool_calls_detected)):
                    self._false_promise_retries += 1
                    # An <Action> commitment is high-confidence, so it is never treated as
                    # a "substantial answer" the validator merely misread.
                    _is_substantial = (_response_len > 200) and not _action_promise
                    # For substantial responses, cap at 2 retries: the validator is
                    # likely misclassifying analytical text, and more retries only
                    # deepen the loop (each CORRECTION prompt produces more analysis).
                    _effective_max = 2 if _is_substantial else self._max_false_promise_retries

                    if self._false_promise_retries > _effective_max:
                        UI.event("System", "Max false promise retries reached - skipping validation", style="error")
                        self._false_promise_retries = 0
                        # Proceed without blocking
                    else:
                        UI.event("System", f"False promise detected (attempt {self._false_promise_retries}) - forcing retry...", style="warning")
                        # Only clear the UI bubble when the response is short/empty.
                        # If the model generated a substantial response (>200 chars) that the
                        # false-promise heuristic flagged, do NOT nuke it — the user is actively
                        # reading it. The retry will append a corrected follow-up instead.
                        if _emit_to_web_ui():
                            try:
                                from vaf.core.web_interface import get_web_interface
                                from vaf.core.subagent_ipc import get_current_session_id
                                session_id = get_current_session_id()
                                get_web_interface().log(
                                    f"False promise detected (attempt {self._false_promise_retries}) - forcing retry...",
                                    level="warning",
                                    source="System",
                                    session_id=session_id,
                                )
                                if not _is_substantial:
                                    self._clear_last_assistant_ui(session_id)
                            except Exception:
                                pass
                        # Clear stream buffer so the retry sends only new content (no old + new)
                        if stream_callback and hasattr(stream_callback, "clear"):
                            try:
                                stream_callback.clear()
                            except Exception:
                                pass
                        # Add error to history to force correction
                        self.history.append({
                            "role": "assistant",
                            "content": full_content
                        })
                        if _promised_tool:
                            _correction = (
                                f"CORRECTION NEEDED: You announced an action (\"{_act_intent[:120]}\") "
                                f"but did NOT execute the tool call.\n"
                                f"Call `{_promised_tool}` NOW using a proper function call. "
                                f"Do not describe the call — emit it."
                            )
                        else:
                            _correction = (
                                "CORRECTION NEEDED: You mentioned using a tool (e.g. 'I am using...', 'Let me search...') "
                                "but you did NOT execute the tool call.\n"
                                "Please call the tool using proper function syntax now."
                            )
                        self.history.append({
                            "role": "system",
                            "content": _correction,
                        })
                        
                        # Force retry without user input
                        continue

            # Reset retry counter if tool calls were made or we passed the check
            self._false_promise_retries = 0

            # 0b. RESULT GROUNDING (Anti-Confabulation)
            # When the model produced a final text reply (no new tool call), make sure it isn't
            # claiming a concrete tool OUTCOME that the turn's actual tool results don't support
            # (e.g. "Workflow failed: Tool not found" when execute_workflow was never run). On a
            # mismatch, bounce it back for correction — capped, then proceed so it never loops.
            if not streaming_tools and not tool_calls_detected and full_content.strip():
                _rg_on = True
                try:
                    from vaf.core.config import Config as _CfgRG
                    _rg_on = bool(_CfgRG.get("result_grounding_enabled", True))
                except Exception:
                    _rg_on = True
                if _rg_on:
                    _ungrounded, _claim = self._detect_ungrounded_result_claim(
                        full_content, self._turn_tool_results()
                    )
                    if _ungrounded:
                        self._result_grounding_retries += 1
                        try:
                            from vaf.core.config import Config as _CfgRG2
                            _rg_max = int(_CfgRG2.get("result_grounding_max_retries", 2))
                        except Exception:
                            _rg_max = 2
                        if self._result_grounding_retries > _rg_max:
                            UI.event("System", "Result grounding: max retries reached — proceeding.", style="error")
                            self._result_grounding_retries = 0
                        else:
                            UI.event("System", f"Ungrounded tool-result claim detected (attempt {self._result_grounding_retries}) - forcing correction...", style="warning")
                            if _emit_to_web_ui():
                                try:
                                    from vaf.core.web_interface import get_web_interface
                                    from vaf.core.subagent_ipc import get_current_session_id
                                    _rg_sid = get_current_session_id()
                                    get_web_interface().log(
                                        f"Ungrounded tool-result claim detected (attempt {self._result_grounding_retries}) - forcing correction...",
                                        level="warning", source="System", session_id=_rg_sid,
                                    )
                                    self._clear_last_assistant_ui(_rg_sid)
                                except Exception:
                                    pass
                            if stream_callback and hasattr(stream_callback, "clear"):
                                try:
                                    stream_callback.clear()
                                except Exception:
                                    pass
                            self.history.append({"role": "assistant", "content": full_content})
                            self.history.append({
                                "role": "system",
                                "content": (
                                    "CORRECTION NEEDED: your reply stated a tool outcome — "
                                    f"\"{(_claim or '')[:200]}\" — that no tool actually produced this turn. "
                                    "Do NOT report results you did not get. Either CALL the tool now to "
                                    "actually perform it, or restate WITHOUT claiming a result that did not happen."
                                ),
                            })
                            continue
                    else:
                        self._result_grounding_retries = 0

            # Team-await gate: hold a "task complete" claim while a sub-agent is genuinely running.
            if not streaming_tools and not tool_calls_detected and full_content.strip():
                _ta_on = True
                try:
                    from vaf.core.config import Config as _CfgTA
                    _ta_on = bool(_CfgTA.get("team_await_enabled", True))
                except Exception:
                    _ta_on = True
                if _ta_on:
                    _await_blocked, _await_labels = self._detect_premature_done_claim(full_content)
                    if _await_blocked:
                        # NON-DESTRUCTIVE (chat-while-subagent-runs): a streamed reply is NEVER
                        # erased or regenerated. The old erase-and-retry bounce destroyed the
                        # user's already-visible answer (plus its steps) whenever casual chat
                        # about the SAME topic as the delegated work contained a completion
                        # word — the worst possible UX. Model: the harness itself — a running
                        # background task never deletes a reply; its result arrives later as
                        # its own event. So: keep the reply, log the situation, and append a
                        # history note so the NEXT turn does not build on a false "done".
                        _ta_list = ", ".join(_await_labels)
                        UI.event("System", f"Note: sub-agent still running ({_ta_list}) — reply kept, completion pending.", style="warning")
                        if _emit_to_web_ui():
                            try:
                                from vaf.core.web_interface import get_web_interface
                                from vaf.core.subagent_ipc import get_current_session_id
                                _ta_sid = get_current_session_id()
                                get_web_interface().log(
                                    f"Note: sub-agent still running ({_ta_list}) — the delegated work is NOT finished yet; its result arrives automatically.",
                                    level="info", source="System", session_id=_ta_sid,
                                )
                            except Exception:
                                pass
                        # Note precedes the reply the normal flow appends right after — it
                        # exists for the NEXT request: no overall-completion claims until the
                        # sub-agent result actually arrived.
                        self.history.append({
                            "role": "system",
                            "content": (
                                f"NOTE: sub-agent(s) still running: {_ta_list}. The reply that follows "
                                "was kept as-is, but the DELEGATED work is not finished. Do not state "
                                "overall completion until the sub-agent result arrives (it is delivered "
                                "automatically)."
                            ),
                        })

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
                                        # LOOP PROTECTION. Count on a SEPARATE counter (not empty_retry_count):
                                        # a redundant block must never climb into the empty-response abort that
                                        # left the agent silent. After a few repeats, force ONE final text answer
                                        # from the results already in context instead of looping further.
                                        redundant_block_count += 1
                                        append_domain_log("backend", f"[LOOP_PROTECTION] blocked redundant tool call '{tool_name}' (#{redundant_block_count})")
                                        if redundant_block_count >= 3:
                                            self.history.append({
                                                "role": "system",
                                                "content": (
                                                    "You have already gathered the needed tool results (see the 'tool' messages above). "
                                                    "Answer the user NOW in plain text. Do NOT call any tool."
                                                )
                                            })
                                            disable_tools = True  # next generation: no tools -> a text answer, never an abort
                                            redundant_block_count = 0
                                        else:
                                            self.history.append({
                                                "role": "system",
                                                "content": (
                                                    f"[!] STOP! You just executed '{tool_name}' with these EXACT arguments. "
                                                    f"The result is already in the context above (the 'tool' message). "
                                                    f"Do NOT call it again -- analyze the result and provide your answer."
                                                )
                                            })
                                        continue
                                except Exception as e:
                                    # If check fails, assume it's safe to proceed
                                    pass
                        
                        tool_calls_detected.append({
                            "id": tool_data['id'] or _synth_tool_call_id(),
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
                                "id": _synth_tool_call_id(),
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
                                        "id": _synth_tool_call_id(),
                                        "type": "function",
                                        "function": {"name": item["name"], "arguments": json.dumps(item["arguments"]) if isinstance(item["arguments"], dict) else item["arguments"]}
                                    })
                    except: pass

                # 3. Text Pattern: "1. web_search(...)", "Answer: web_search(...)"
                # or leaked plan bullets '- find_mail({"query": ...})' (deepseek-v4).
                # Shared parser _parse_paren_tool_calls (module level, tested).
                if not tool_calls_detected:
                    for _p_name, _p_args in _parse_paren_tool_calls(text_to_search, self.tools):
                        tool_calls_detected.append({
                            "id": _synth_tool_call_id(),
                            "type": "function",
                            "function": {"name": _p_name, "arguments": json.dumps(_p_args)},
                        })

                # 4. Gemma-4 native tool calls: <|tool_call>call:NAME{key:<|"|>value<|"|>}<tool_call|>
                # Additive defensive net: only for a local Gemma-4 model and only when nothing matched
                # above. The server normally converts these via --jinja; this catches the rare case
                # where a raw call leaks into the text unconverted.
                if not tool_calls_detected and getattr(self, "model_mode", None) == "gemma4":
                    for _g_name, _g_args in _parse_gemma4_tool_calls(text_to_search, self.tools):
                        tool_calls_detected.append({
                            "id": _synth_tool_call_id(),
                            "type": "function",
                            "function": {"name": _g_name, "arguments": json.dumps(_g_args)},
                        })

                # 5. Qwen / Hermes text tool calls: <tool_call><function=NAME><parameter=KEY>VALUE</parameter>...</function></tool_call>
                # A reasoning model (e.g. Qwen) sometimes writes this INSIDE <think> instead of making a
                # native call, so the server never converts it and the call is dropped (observed:
                # update_working_memory written this way -> the plan is never set -> the [PLAN REQUIRED]
                # gate loops). The format is specific and results are filtered to known tools, so it is
                # safe to try for any model once nothing above matched.
                if not tool_calls_detected:
                    for _q_name, _q_args in _parse_qwen_tool_calls(text_to_search, self.tools):
                        tool_calls_detected.append({
                            "id": _synth_tool_call_id(),
                            "type": "function",
                            "function": {"name": _q_name, "arguments": json.dumps(_q_args)},
                        })

                # 6. Claude-style XML / DeepSeek ｜｜DSML｜｜ tool call leaked into content
                # (the same shape the coding agent recovers in its Format 0d). DeepSeek v4
                # intermittently emits a real call as content instead of structured tool_calls;
                # filtered to known tools, so it is safe to try once nothing above matched.
                if not tool_calls_detected:
                    from vaf.core.tool_call_recovery import extract_xml_tool_call
                    _xml_tc = extract_xml_tool_call(text_to_search, self.tools)
                    if _xml_tc:
                        tool_calls_detected.append(_xml_tc)

            try:
                append_domain_log("backend", f"after_regex_fallback tool_calls={len(tool_calls_detected)}")
            except Exception:
                pass

            # ── Action-Tag parser ──────────────────────────────────────────────────
            # Read the agent's committed <Action> intent and fuzzy-match it against the loaded
            # tools, logging quietly (no terminal spam). This is the declared-vs-actual seed for
            # runtime learning; it does NOT drive know-how injection (that is router-driven; see
            # docs/agents/ACTION_TAG.md + WHARE_WANANGA.md "Delivery").
            try:
                _act = _extract_action_text(full_response)
                if _act:
                    _avail = list(self._active_tools) if self._active_tools else list(self.tools.keys())
                    _matches = _match_action_to_tools(_act, _avail)
                    try:
                        append_domain_log(
                            "backend",
                            f"[ACTION-MATCH] action={_act[:120]!r} candidates={len(_avail)} top={_matches[:3]}",
                        )
                    except Exception:
                        pass
            except Exception:
                pass
            # ───────────────────────────────────────────────────────────────────────

            if tool_calls_detected:
                content_for_history = full_content if full_content else "Thinking..."
                # A tool call recovered from the assistant TEXT (a DeepSeek ｜｜DSML｜｜ / Claude
                # <invoke> leak) leaves its raw markup in the content. Strip it so the tags are
                # neither shown in the UI nor replayed to the model next turn. No-op otherwise.
                from vaf.core.tool_call_recovery import strip_tool_call_markup
                content_for_history = strip_tool_call_markup(content_for_history) or "Thinking..."
                if self.use_server and not full_content:
                    content_for_history = None
                
                msg = {"role": "assistant", "content": content_for_history, "tool_calls": tool_calls_detected}
                if not content_for_history: del msg["content"]
                # Anthropic: carry the raw assistant blocks (thinking + tool_use, signed) so
                # _convert_messages_to_anthropic can replay them verbatim next turn. Side-key
                # is JSON-serializable and ignored by every other provider.
                if anthropic_blocks_raw:
                    msg["_anthropic_blocks"] = anthropic_blocks_raw

                self.history.append(msg)

                # Defer system/user messages that tool handlers want to append until
                # AFTER all tool results are in history. A sys message between two
                # consecutive role:tool messages (for the same TC batch) causes
                # DeepSeek 400 "insufficient tool messages following tool_calls".
                _post_tc_messages: list = []

                for tc in tool_calls_detected:
                    function_name = tc['function']['name']

                    # THINKING READ-CAP: in a background thinking run, block a read/gather tool called too
                    # many times this step (memory_search spin etc.). Soft block — the result tells the
                    # model to act; other tools still work. No-op outside thinking mode.
                    _read_block = self._thinking_read_cap_step(function_name)
                    if _read_block:
                        UI.event("Warning", f"Thinking read-cap: blocked {function_name}", style="warning")
                        self.history.append({
                            "role": "tool",
                            "tool_call_id": tc['id'],
                            "name": function_name,
                            "content": _read_block,
                        })
                        continue

                    # Stop button: honour it BETWEEN tools, not only at the loop top. A single LLM
                    # response can carry many tool calls; without this, hitting Stop drains the whole
                    # batch (observed: list_tools fired dozens of times after "stopped by user").
                    _stop_sid = getattr(self, 'current_session_id', None) or getattr(self, '_session_id', None)
                    if _stop_sid:
                        try:
                            from vaf.core.task_queue import TaskQueue as _STQ
                            _stq = _STQ()
                            if _stq.should_stop(_stop_sid):
                                _stq.clear_stop(_stop_sid)
                                _sm = "[Generation stopped by user]"
                                self.history.append({"role": "assistant", "content": _sm})
                                return _sm
                        except Exception:
                            pass

                    # EMERGENCY DEAD-LOOP BREAKER: >=10 tool executions within 5 seconds means a runaway
                    # loop (a model spamming one tool, or a stop that didn't propagate). Abort the whole
                    # turn at once. This is time-based and far below MAX_TOOL_TURNS_PER_STEP (75), so it
                    # stops in seconds instead of grinding through dozens of calls and churning memory.
                    _now = time.monotonic()
                    _et_times = [t for t in getattr(self, "_tool_exec_times", []) if _now - t < 5.0]
                    _et_times.append(_now)
                    self._tool_exec_times = _et_times
                    if len(_et_times) >= 10:
                        self._tool_exec_times = []
                        _emsg = f"⚠️ Emergency stop: {len(_et_times)} tool calls in under 5 seconds — a runaway tool loop was aborted."
                        UI.event("Emergency", _emsg, style="bold red")
                        try:
                            append_domain_log("backend", f"[EMERGENCY_LOOP_BREAK] {len(_et_times)} tool calls <5s, last={function_name}")
                        except Exception:
                            pass
                        self.history.append({"role": "assistant", "content": _emsg})
                        return _emsg

                    # ═══════════════════════════════════════════════════════════════
                    # ANTI-SPIN GUARD: "plan forever, never act" loops
                    # ═══════════════════════════════════════════════════════════════
                    # A weak model can churn the bookkeeping tools (update_working_memory /
                    # update_intent / add_task) over and over — re-planning the same task with
                    # slightly varying text — without ever calling the tool that does the actual
                    # work (observed: ~8 update_working_memory calls, then it gave up and used the
                    # wrong tool). The redundant-call block needs EXACT args and the emergency
                    # breaker needs <5s, so neither catches this slow near-duplicate planning spin.
                    # Count CONSECUTIVE bookkeeping calls (any other tool resets it): nudge at the
                    # threshold, then disable tools for one turn so the model must act or answer.
                    # The current call still runs; only the NEXT turn is steered (no tool-message
                    # reordering). Governed by anti_spin_enabled / anti_spin_max_planning_calls.
                    _spin_msg, _spin_force = self._anti_spin_step(function_name)
                    if _spin_msg:
                        _post_tc_messages.append({"role": "system", "content": _spin_msg})
                    if _spin_force:
                        disable_tools = True  # next generation: no tools -> must act or answer

                    # No-progress guard: same mechanism for a read/verify-only spin (e.g. the
                    # create_automation "zombie" that kept listing/reading after the work was done).
                    # Main loop only; any mutating/producing tool resets the streak.
                    _np_msg, _np_force = self._nonprogress_step(function_name)
                    if _np_msg:
                        _post_tc_messages.append({"role": "system", "content": _np_msg})
                    if _np_force:
                        disable_tools = True

                    # ═══════════════════════════════════════════════════════════════
                    # THINKING DONE PROTECTION: Hardcoded break
                    # ═══════════════════════════════════════════════════════════════
                    if function_name == "thinking_done":
                        raw_args = tc['function']['arguments']
                        try:
                            arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        except: arguments = {}
                        if not isinstance(arguments, dict):
                            arguments = {}
                        summary = (arguments.get("summary") or "Done.").strip() or "Done."

                        # thinking_done is special-cased here and returns BEFORE the normal tool-execution
                        # path, so ThinkingDoneTool.run() never runs. Honor the message fallback inline via
                        # the shared helper, otherwise thinking_done(message=...) would be silently dropped.
                        try:
                            from vaf.core.thinking_mode import deliver_thinking_done_fallback
                            _note = deliver_thinking_done_fallback(
                                getattr(self, "_current_user_scope_id", None),
                                arguments.get("message"),
                                proposed_action=arguments.get("proposed_action"),
                                source_note_id=arguments.get("source_note_id"),
                                source_todo_id=arguments.get("source_todo_id"),
                                username=getattr(self, "_current_username", None),
                                details=arguments.get("details"),
                            )
                            if _note:
                                summary = summary + _note
                        except Exception as _td_err:
                            append_domain_log("backend", f"[LOOP_PROTECTION] thinking_done delivery failed: {_td_err}")

                        UI.event("Debug", "Thinking Mode: done signal received, breaking loop.", style="dim")
                        append_domain_log("backend", f"[LOOP_PROTECTION] thinking_done detected - breaking loop with summary: {summary[:50]}")

                        # Add tool result to history
                        self.history.append({
                            "role": "tool",
                            "tool_call_id": tc['id'],
                            "name": function_name,
                            "content": summary
                        })
                        # Add final assistant message so outer loop sees it
                        self.history.append({"role": "assistant", "content": summary})
                        return summary

                    raw_args = tc['function']['arguments']
                    try:
                        arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except: arguments = {}

                    # Debug: log tool use with session/scope for user-isolation debugging (only when debug logs on)
                    _tl_call_id = tc.get('id', '')
                    _tl_start = time.time()
                    try:
                        from vaf.core.subagent_ipc import get_current_session_id
                        _sid = get_current_session_id() or getattr(self, "current_session_id", None)
                        _scope = getattr(self, "_current_user_scope_id", None)
                        _args_preview = json.dumps(arguments, ensure_ascii=False) if arguments else ""
                        log_tool_use(function_name, session_id=_sid, user_scope_id=_scope, arguments_preview=_args_preview)
                        log_timeline_event('tool_start', tool=function_name, call_id=_tl_call_id,
                                           session=str(_sid or ''), scope=str(_scope or ''),
                                           args=_args_preview[:500])
                    except Exception:
                        pass

                    UI.event("Tool", f"{function_name}", style="highlight")
                    
                    # Web UI Event: Tool Start
                    # NOTE: Do NOT gate on _emit_to_web_ui() here. That function checks the
                    # process-wide VAF_THINKING_MODE env var, which can be set by a concurrent
                    # background thinking process — blocking tool_update for active WebUI sessions.
                    # broadcast routes updates by session_id, so this is always safe.
                    # EXCEPTION — a background run (scheduled automation): it has no own web session, so the
                    # get_current_session_id() fallback would route this tool bubble to whoever is the active
                    # web user (observed: an automation's web_search bubbles leaked into a LAN user's chat).
                    # The per-agent flag is race-free, unlike the process-wide env a concurrent real turn trips.
                    try:
                        if not getattr(self, '_background_run', False):
                            from vaf.core.web_interface import get_web_interface
                            _tool_session = getattr(self, 'current_session_id', None)
                            if not _tool_session:
                                from vaf.core.subagent_ipc import get_current_session_id
                                _tool_session = get_current_session_id()
                            get_web_interface().emit_tool_update('start', function_name, tc['id'], data=json.dumps(arguments), session_id=_tool_session)
                        else:
                            # Proof line: a silent background run suppressed this live tool bubble (it would
                            # otherwise have broadcast into the active web user's chat). Background runs only.
                            # NOTE: append_domain_log is imported at module scope (top of file) — do NOT
                            # re-import it locally here. A local `from … import append_domain_log` makes the
                            # name function-local for ALL of chat_step, so the earlier [LOOP_PROTECTION]
                            # thinking_done call hits UnboundLocalError before this line ever runs.
                            append_domain_log("backend", f"[SILENT-RUN] tool_update 'start' SUPPRESSED (background agent) tool={function_name}")
                    except Exception:
                        pass
                    
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
                        # Special Case: Tools with their own immersive UI (no spinner needed).
                        # The workflow orchestrators MUST be here: they redirect sys.stdout to
                        # the WebUI stream for the whole (possibly long) run, and Rich's
                        # console.status spinner deadlocks on exit when stdout was swapped under
                        # it — which left execute_tool never returning (no tool_end → the chat
                        # tool-bubble hung "running").
                        if function_name in ("coding_agent", "research_agent",
                                             "create_agent_workflow", "execute_workflow"):
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
                    # NOTE: Do NOT gate on _emit_to_web_ui() — same race condition as Tool Start.
                    _tool_end_emitted = False
                    try:
                        from vaf.core.web_interface import get_web_interface
                        r_str = str(result) if result else ""
                        # Same 50-char status convention: only the prefix is the status.
                        # Single source of truth for "is this a failed tool
                        # result" (context.py) - the per-turn summarizer and the
                        # tool_end ok flag use the same helper so they cannot drift.
                        from vaf.core.context import tool_result_is_error
                        is_err = tool_result_is_error(r_str)
                        _tool_session = getattr(self, 'current_session_id', None)
                        if not _tool_session:
                            from vaf.core.subagent_ipc import get_current_session_id
                            _tool_session = get_current_session_id()
                        # Truncate + sanitize: remove surrogate chars that break json.dumps
                        _r_raw = r_str[:800] + (f"\n[…+{len(r_str)-800} chars]" if len(r_str) > 800 else "")
                        _r_ui = _r_raw.encode('utf-8', errors='replace').decode('utf-8')
                        # A background run (scheduled automation) stays silent — see the Tool Start note:
                        # its tool bubbles must not broadcast into the active web user's chat. Mark it
                        # "emitted" so the later not-yet-emitted fallback also stays silent.
                        if not getattr(self, '_background_run', False):
                            get_web_interface().emit_tool_update('error' if is_err else 'end', function_name, tc['id'], data=_r_ui, session_id=_tool_session)
                        else:
                            # append_domain_log is module-level imported — no local re-import (see the note
                            # on the 'start' branch above; a local import makes the name function-local and
                            # breaks the earlier thinking_done call with UnboundLocalError).
                            append_domain_log("backend", f"[SILENT-RUN] tool_update '{'error' if is_err else 'end'}' SUPPRESSED (background agent) tool={function_name}")
                        _tool_end_emitted = True
                        log_timeline_event('tool_end', tool=function_name, call_id=_tl_call_id,
                                           session=str(_tool_session or ''),
                                           status='error' if is_err else 'ok',
                                           duration_s=round(time.time() - _tl_start, 2),
                                           result=r_str[:300])
                        if is_err and "Error executing tool" not in r_str:
                            time.sleep(2)
                    except Exception as _emit_err:
                        UI.event("Debug", f"emit_tool_update failed: {_emit_err}", style="dim")
                    if not _tool_end_emitted and result:
                        _fb = str(result)[:50].lower().strip()
                        if _fb.startswith("❌") or _fb.startswith("error") or _fb.startswith("failed"):
                            time.sleep(2)

                    # Check if this is an async sub-agent task BEFORE adding to history
                    result_str = str(result) if result else ""
                    is_async_subagent = "[SUBAGENT_ASYNC:" in result_str
                    
                    if is_async_subagent:
                        # Replace the async marker with a clear "waiting" message for history
                        task_match = re.search(r'\[SUBAGENT_ASYNC:([^:]+):([^\]]+)\]', result_str)
                        task_id = task_match.group(1) if task_match else "unknown"
                        agent_type = task_match.group(2) if task_match else "sub-agent"
                        log_timeline_event('subagent_start', tool=function_name,
                                           call_id=_tl_call_id,
                                           session=str(getattr(self, 'current_session_id', '') or ''),
                                           task_id=task_id, agent_type=agent_type)
                        
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

                        # PROACTIVE EVIDENCE POOL: in a thinking run, capture real retrieved memory so the
                        # proactive evidence-gate can verify a suggestion is grounded in it (not fabricated).
                        if function_name == "memory_search" and getattr(self, "_current_turn_thinking_mode", False):
                            try:
                                from vaf.core.thinking_mode import add_run_evidence
                                add_run_evidence(getattr(self, "_current_user_scope_id", None), result_str)
                            except Exception:
                                pass

                        if function_name == 'document_agent' and "Could not create document plan" in result_str:
                            self.history.append({
                                "role": "tool",
                                "tool_call_id": tc['id'],
                                "name": function_name,
                                "content": processed_result
                            })
                            _post_tc_messages.append({
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
                    
                    # ── Whare Wananga reactive delivery (B-track): on a tool ERROR, re-feed the
                    #    failed tool's learned know-how so the loop's natural retry is informed.
                    #    Re-check the error locally from the RAW result (not the compressed history
                    #    copy, not the emit-try is_err). Once per (tool, turn); gated; hard
                    #    fail-safe -- must never break the tool loop. ──
                    try:
                        _rs = (result_str or "").strip().lower()
                        # "[error]" and "access denied" are real failure shapes observed
                        # live (sandbox tracebacks, filesystem jail denials) that the
                        # original prefix set missed - the retry ran uninformed.
                        # Deliberately NOT matched: "blocked"/"[security]" guard messages,
                        # which already carry their own instructions.
                        if _rs.startswith(("error", "failed", "tool error", "security error",
                                           "exception", "❌", "[error]", "access denied")) \
                                and function_name not in _ww_reactive_injected:
                            from vaf.whare_wananga.delivery import tool_knowhow, known_pitfall_hit
                            # allow_unverified: the call ALREADY failed, so a tagged hint from a
                            # gate-failing record (declare/stale/draft) costs little and is often
                            # exactly the missing knowledge. The proactive A-track (schema
                            # injection) stays strictly gated.
                            _known = known_pitfall_hit(function_name, result_str, allow_unverified=True)
                            _kh = tool_knowhow(function_name, procedure_first=_known, allow_unverified=True)
                            if _kh:
                                _ww_reactive_injected.add(function_name)
                                if _known:
                                    _lead = (f"[TOOL KNOW-HOW] '{function_name}' just failed on a known pitfall. "
                                             "Follow the learned procedure and retry with corrected arguments.")
                                else:
                                    _lead = (f"[TOOL KNOW-HOW] '{function_name}' returned an unexpected error. "
                                             "Use the learned guidance below and retry if appropriate.")
                                    try:
                                        append_domain_log("backend", f"[WW-SURPRISE] {function_name}: novel error not in learned pitfalls: {result_str[:160]!r}")
                                    except Exception:
                                        pass
                                    # LAZY-corrective: learn the new pitfall from this real surprise
                                    # (background, rate-limited, fail-safe -- never blocks the turn).
                                    try:
                                        from vaf.whare_wananga.runtime import maybe_relearn
                                        maybe_relearn(self, function_name, arguments, result_str)
                                    except Exception:
                                        pass
                                _post_tc_messages.append({"role": "system", "content": _lead + " " + _kh})
                                try:
                                    append_domain_log("backend", f"[WW-REACTIVE] {function_name}: re-fed know-how (known={_known})")
                                except Exception:
                                    pass
                    except Exception:
                        pass

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
                                elif self.api_backend:
                                    # Cloud provider (no local :8080) - resolve model via api_model_{provider}.
                                    chunks = list(self.api_backend.chat_completion(
                                        messages=[{"role": "user", "content": translation_prompt}],
                                        max_tokens=100,
                                        temperature=0.1,
                                        stream=False,
                                    ))
                                    content = "".join(c if isinstance(c, str) else str(c) for c in chunks if c).strip()
                                    response_text = content if content else base_msg
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
                    
                    # If tool returned an error, force the model to acknowledge it.
                    # Convention: only the first ~50 chars are the status prefix —
                    # everything after is payload (web content, file text, etc.) and must never
                    # be inspected. ✅ = success, ❌ = user-facing error, bare "Error/Failed" = crash.
                    _result_raw = str(result) if result else ""
                    _status = _result_raw[:50].lower().strip()
                    is_tool_error = (
                        _status.startswith("❌") or
                        _status.startswith("error") or
                        _status.startswith("failed")
                    )
                    
                    if result and is_tool_error:
                        # Defer this system message until after ALL tool results are appended.
                        # Injecting a sys msg between two consecutive role:tool messages (when the
                        # agent called multiple tools in one response) causes DeepSeek 400
                        # "insufficient tool messages following tool_calls".
                        _post_tc_messages.append({
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

                # Flush deferred messages (error nudges, doc-agent hints) now that ALL
                # tool results are in history — safe to insert non-tool messages here.
                for _ptm in _post_tc_messages:
                    self.history.append(_ptm)

                # ═══════════════════════════════════════════════════════════════
                # LOOP PROTECTION: Turn limit check
                # ═══════════════════════════════════════════════════════════════
                tool_turn_count += 1

                # Hard kill at MAX_TOOL_TURNS_PER_STEP
                # If tool_loop_unlimited=True in config, skip the hard kill entirely.
                _unlimited_loop = bool(self.config.get("tool_loop_unlimited", False))

                # Wall-clock backstop — independent of tool count. If THIS user turn has run past its time
                # budget, stop NOW (at the boundary, after the last tool finished). Catches a slow provider
                # grinding many turns (the create_automation "zombie") that the turn-count and 5s-emergency
                # guards miss. Returns immediately with a clear message — no extra (slow) summarizing turn.
                if not _unlimited_loop and time.monotonic() > _turn_deadline:
                    _wc_budget = float(Config.get("chat_step_wall_clock_seconds", 3600) or 3600)
                    _wc_msg = f"⚠️ [LOOP_PROTECTION] Wall-clock stop after ~{_wc_budget:.0f}s in a single turn ({tool_turn_count} tool turns) — task aborted to keep the agent responsive."
                    UI.event("Emergency", _wc_msg, style="bold red")
                    append_domain_log("backend", f"[LOOP_PROTECTION] wall-clock stop at {_wc_budget:.0f}s / turn {tool_turn_count}")
                    self.history.append({"role": "assistant", "content": _wc_msg})
                    return _wc_msg

                if not _unlimited_loop and tool_turn_count >= MAX_TOOL_TURNS_PER_STEP:
                    if _hard_stop_injected:
                        # Agent called another tool after the hard-stop message — true kill now.
                        _kill_msg = f"⚠️ [LOOP_PROTECTION] Hard stop enforced after {MAX_TOOL_TURNS_PER_STEP} tool turns."
                        UI.event("Emergency", _kill_msg, style="bold red")
                        append_domain_log("backend", _kill_msg)
                        self.history.append({"role": "assistant", "content": _kill_msg})
                        return _kill_msg

                    # First time hitting the limit: inject a message so the agent can inform the user.
                    _hard_stop_injected = True
                    _hl_intent = ""
                    try:
                        if hasattr(self, "main_persistence") and self.main_persistence:
                            _hl_intent = self.main_persistence.get_user_intent() or ""
                    except Exception:
                        pass
                    if not _hl_intent:
                        for _hlmsg in reversed(self.history):
                            if isinstance(_hlmsg, dict) and _hlmsg.get("role") == "user":
                                _c = (_hlmsg.get("content") or "")
                                if _c and not _c.startswith("[System"):
                                    _hl_intent = _c[:400]
                                    break
                    _intent_line = f'\n\nDas originale Ziel war: "{_hl_intent}"' if _hl_intent else ""
                    _hard_stop_injection = (
                        f"[System: HARD STOP — du hast {MAX_TOOL_TURNS_PER_STEP} Tool-Aufrufe verbraucht ohne die Aufgabe abzuschließen.{_intent_line}\n\n"
                        f"Du darfst KEINE weiteren Tools mehr aufrufen. "
                        f"Informiere den User auf Deutsch direkt und freundlich: Erkläre kurz was du bisher erreicht hast und wo du stehst. "
                        f"Frage dann ob er möchte, dass du in einer neuen Antwort weitermachst.]"
                    )
                    UI.event("Emergency", f"Hard stop at {MAX_TOOL_TURNS_PER_STEP} tool turns — asking agent to inform user.", style="bold red")
                    append_domain_log("backend", f"hard_stop_injection at turn {MAX_TOOL_TURNS_PER_STEP}")
                    self.history.append({"role": "user", "content": _hard_stop_injection})
                    # Continue so the agent can produce its final response — no more tool calls allowed.

                # Soft reminder at SOFT_LIMIT_TOOL_TURNS — inject goal reminder, agent continues
                if tool_turn_count == SOFT_LIMIT_TOOL_TURNS:
                    _original_intent = ""
                    try:
                        if hasattr(self, "main_persistence") and self.main_persistence:
                            _original_intent = self.main_persistence.get_user_intent() or ""
                    except Exception:
                        pass
                    if not _original_intent:
                        for _hmsg in reversed(self.history):
                            if isinstance(_hmsg, dict) and _hmsg.get("role") == "user":
                                _c = (_hmsg.get("content") or "")
                                if _c and not _c.startswith("[System"):
                                    _original_intent = _c[:400]
                                    break
                    _intent_hint = f'\n\nDas originale Ziel des Users war: "{_original_intent}"' if _original_intent else ""
                    _reminder = (
                        f"[System: Du hast bereits {SOFT_LIMIT_TOOL_TURNS} Tool-Aufrufe gemacht und hast die Aufgabe noch nicht abgeschlossen.{_intent_hint}\n\n"
                        f"Überdenke deine Strategie: Bist du noch auf dem richtigen Weg? "
                        f"Fokussiere dich auf das Wesentliche und schließe die Aufgabe so direkt wie möglich ab. "
                        f"Du hast noch {MAX_TOOL_TURNS_PER_STEP - SOFT_LIMIT_TOOL_TURNS} weitere Tool-Aufrufe bevor ein Hard-Stop ausgelöst wird.]"
                    )
                    UI.event("Warning", f"Soft limit reached ({SOFT_LIMIT_TOOL_TURNS} tool turns) — injecting goal reminder...", style="yellow")
                    append_domain_log("backend", f"soft_limit_reminder injected at turn {SOFT_LIMIT_TOOL_TURNS}")
                    self.history.append({"role": "user", "content": _reminder})

                # Check stop flag after tool finishes — catches "Stop" clicked during tool execution
                _post_tool_session = getattr(self, 'current_session_id', None) or getattr(self, '_session_id', None)
                if _post_tool_session:
                    try:
                        from vaf.core.task_queue import TaskQueue as _PTQ
                        _ptq = _PTQ()
                        if _ptq.should_stop(_post_tool_session):
                            _ptq.clear_stop(_post_tool_session)
                            UI.event("System", "Generation stopped after tool execution", style="info")
                            _stop_msg = "[Generation stopped by user]"
                            self.history.append({"role": "assistant", "content": _stop_msg})
                            return _stop_msg
                    except Exception:
                        pass

                UI.event("Debug", f"Summarizing intel (turn {tool_turn_count}/{SOFT_LIMIT_TOOL_TURNS} soft / {MAX_TOOL_TURNS_PER_STEP} hard)...", style="dim")
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

            # CoT fallback for reasoning models (DeepSeek Reasoner/R1): These models put the
            # answer primarily in reasoning_content and sometimes little/nothing in content.
            # Without this, we get "API returned empty responses repeatedly" loops.
            if not has_final_answer and not tool_calls_detected and full_reasoning and len(full_reasoning.strip()) > 100:
                if self.api_backend and self.provider == "deepseek":
                    model = (self.config.get("api_model_deepseek", "") or "").lower()
                    if "reasoner" in model or "-r1" in model:
                        has_final_answer = True

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
            # Empty / thinking-only retry. The user wanted it OFF in BACKGROUND thinking runs (it spammed
            # "Empty response detected..." while idle), NOT in foreground turns. In the foreground a
            # thinking-only / no-answer generation MUST still be recovered -- otherwise the turn never
            # closes and the Web UI hangs forever on a loading thinking block (observed). So: ON in the
            # foreground (any provider), OFF only in thinking mode (config flag can still force it on).
            # The API delayed-retry path further below is independent of this gate.
            _is_thinking_mode = os.environ.get("VAF_THINKING_MODE", "").strip() in ("1", "true", "yes")
            _empty_retry_on = (not _is_thinking_mode) or Config.get("empty_response_retry_enabled", False)
            if (not has_final_answer) and not tool_calls_detected and not getattr(self, "_compaction_in_progress", False) and _empty_retry_on:
                UI.event("System", "Empty response detected. Applying snapshot and retry...", style="warning")
                try:
                    append_domain_log("backend", f"empty_response_retry full_content_preview={full_content[:100] if full_content else 'NONE'}")
                except Exception:
                    pass
                # Ensure Web UI shows retry message and remove the faulty assistant bubble
                if _emit_to_web_ui():
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
                        self._clear_last_assistant_ui(session_id)
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
                # In thinking mode (background pass), limit retries aggressively — no user is waiting
                # ═══════════════════════════════════════════════════════════════
                _is_thinking_mode = os.environ.get("VAF_THINKING_MODE", "").strip() in ("1", "true", "yes")
                _proactive_clear_at = 2 if _is_thinking_mode else 8
                if empty_retry_count == _proactive_clear_at:
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
                MAX_RETRIES_BEFORE_EMERGENCY = 2 if _is_thinking_mode else 7
                HARD_LIMIT = 3 if _is_thinking_mode else 10

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

                # API guard: auto-retry after 3s, max 4 retries; then show error as system log (no assistant bubble)
                if self.api_backend and empty_retry_count >= 3:
                    if api_empty_delay_retries < API_EMPTY_DELAY_RETRIES_MAX:
                        api_empty_delay_retries += 1
                        UI.event("System", f"API returned empty repeatedly. Retrying in 3s ({api_empty_delay_retries}/{API_EMPTY_DELAY_RETRIES_MAX})...", style="warning")
                        if _emit_to_web_ui():
                            try:
                                from vaf.core.web_interface import get_web_interface
                                from vaf.core.subagent_ipc import get_current_session_id
                                session_id = get_current_session_id()
                                get_web_interface().log(
                                    f"API returned empty repeatedly. Retrying in 3s (attempt {api_empty_delay_retries}/{API_EMPTY_DELAY_RETRIES_MAX})...",
                                    level="warning",
                                    source="System",
                                    session_id=session_id,
                                )
                            except Exception:
                                pass
                        time.sleep(3)
                        empty_retry_count = 0
                        continue
                    # Max delay retries reached: emit as system log only (no assistant message)
                    fallback_msg = "API returned empty responses repeatedly. Please try again."
                    if _emit_to_web_ui():
                        try:
                            from vaf.core.web_interface import get_web_interface
                            from vaf.core.subagent_ipc import get_current_session_id
                            session_id = get_current_session_id()
                            get_web_interface().log(
                                fallback_msg,
                                level="warning",
                                source="System",
                                session_id=session_id,
                            )
                        except Exception:
                            pass
                    # Signal to headless: do not emit as assistant message; UI already got new_log
                    return "[SYSTEM_LOG_ONLY]" + fallback_msg
                
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

            # If this turn answered a tracked background question, stash the main agent's OWN reply onto
            # the request RIGHT HERE — the moment the final answer is committed to history. This runs for
            # every completed answer, BEFORE the pending-task auto-continue recursion below (whose nested
            # chat_step resets the flag) and BEFORE the early returns. Capturing at the final `return`
            # missed all of those paths, so main_reply was never written and the next run only ever saw
            # half the triple. Consume + clear immediately so a continuation append cannot re-capture.
            _pending = getattr(self, "_thinking_reply_pending", None)
            if _pending:
                try:
                    from vaf.core import thinking_requests as _treq
                    _mr = (self._clean_reasoning(history_content) or "").strip()
                    _treq.record_reply(_pending["scope"], _pending["request_id"], main_reply=_mr[:150])
                except Exception:
                    pass
                self._thinking_reply_pending = None

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

                    # ALWAYS squash intermediate steps (not just when tools used)
                    if msgs_to_squash:
                        # Build a compact summary that preserves each tool's OUTCOME
                        # (OK/FAILED + a short result/error snippet) — not just names —
                        # so the agent stays aware of what it did and which errors it
                        # hit on later turns. This summary is also persisted (see
                        # headless_runner save) and survives session reloads.
                        from vaf.core.context import summarize_tool_turn
                        summary_msg = summarize_tool_turn(msgs_to_squash)

                        # Delete ALL intermediate messages
                        del self.history[start_idx:end_idx]

                        if summary_msg:
                            self.history.insert(start_idx, {"role": "system", "content": summary_msg})
                        # If nothing to summarize, just delete without inserting
            except Exception as e:
                UI.event("Debug", f"Compression Warning: {e}", style="dim")

            # ── Pending-task auto-continue ───────────────────────────────────────────
            # The model gave a final text answer (no tool calls) but may still have pending tasks in
            # working memory. Without this the turn ends here and the step nugget only re-fires on the
            # NEXT user message — so the task list sits there unworked (the bug we saw). Instead, when
            # pending tasks remain, re-inject the step nugget as a system "continue" message and loop
            # again INSIDE this same user turn. Shares the existing tool_turn_count budget (soft 50 /
            # hard 75) — no parallel counter; we just count this as one turn so a pure-text continue
            # loop can't run past the hard stop. Brakes: a genuine question to the user (waiting state
            # or _reply_needs_user classifier), background thinking pass, and a config kill-switch.
            try:
                _ac_on = Config.get("autocontinue_pending_tasks_enabled", True)
                _ac_thinking = os.environ.get("VAF_THINKING_MODE", "").strip() in ("1", "true", "yes")
                _ac_budget_left = tool_turn_count < MAX_TOOL_TURNS_PER_STEP
                # Brake = "the agent needs the user before it can continue". Same principle as the
                # thinking-pass: prefer an EXPLICIT signal over guessing from the text. The thinking-
                # pass sets a persistent waiting_for_reply state when the agent reaches out; we honor
                # that state here (no side effects, safe in the foreground). The foreground Web UI has
                # NO tool signal for "I'm asking the user" — the question is plain text, and a
                # messenger send here usually means a completed task, not a question — so for that case
                # a tiny validation LLM classifies whether the reply is a blocking question
                # (_reply_needs_user), with a last-line "?" heuristic as fallback.
                _ac_needs_user = False
                try:
                    from vaf.core.thinking_mode import get_waiting_for_reply
                    if get_waiting_for_reply(getattr(self, "_current_user_scope_id", None)):
                        _ac_needs_user = True
                except Exception:
                    pass
                if not _ac_needs_user:
                    _ac_needs_user = self._reply_needs_user(history_content)
                # Ask-first invariant: latch "the agent is waiting on the user" so synthetic
                # background turns (runner drain) cannot launch new write actions meanwhile.
                # Only SET here; cleared exclusively by a real user message at chat_step entry
                # (a later non-question background reply does not mean the user answered).
                if _ac_needs_user:
                    try:
                        self._pending_user_question = {"preview": (history_content or "")[-160:]}
                    except Exception:
                        pass
                if (_ac_on and not _ac_thinking and not _ac_needs_user and _ac_budget_left
                        and not tool_calls_detected and not auto_retry
                        and getattr(self, "main_persistence", None)):
                    _wm = self.main_persistence.get_working_memory()
                    _step = self.main_persistence._current_step(_wm.get("tasks", []))
                    if _step is not None:
                        _idx, _text, _done, _total = _step
                        # ── Pending-task verification (single-nudge; see _task_stuck_step) ───
                        # The auto-continue only fires on a FINAL TEXT answer (no tool calls). Instead
                        # of force-looping until the list is empty (which makes a weak model redo a done
                        # step to the hard cap and carry it unmarked into the next run), verify ONCE: ask
                        # the model to confirm the step done or actually continue. If it answers again
                        # without progress on the same step, trust it — auto-confirm the step and move on.
                        _stuck = self._task_stuck_step(_idx, _text)
                        if _stuck == "autodone":
                            try:
                                self.main_persistence.update_working_memory(mark_task_done=_idx)
                            except Exception:
                                pass
                            try:
                                append_domain_log("backend", f"[TASK_VERIFY] step idx={_idx} auto-confirmed done (no progress after verification)")
                            except Exception:
                                pass
                            UI.event("System", f"Step {_done + 1}/{_total} auto-confirmed done (model gave no further progress after the verification nudge).", style="warning")
                            self._autocontinue_step_sig = None
                            self._autocontinue_stuck = 0
                            _wm = self.main_persistence.get_working_memory()
                            _step = self.main_persistence._current_step(_wm.get("tasks", []))
                            if _step is None:
                                # All tasks resolved -> let the model's final answer stand; end the turn.
                                pass
                            else:
                                _idx, _text, _done, _total = _step

                        if _step is not None:
                            tool_turn_count += 1  # share the existing soft(50)/hard(75) budget
                            if _stuck == "nudge":
                                # One-shot verification: let a genuinely-finished model just confirm and
                                # stop; push a stopped-early model to actually do the open step.
                                _ac_content = (
                                    "[System: You gave a final answer, but a step is still marked open — "
                                    "verify it before stopping.\n"
                                    f">> OPEN STEP {_done + 1}/{_total}: \"{_text}\"\n"
                                    f"- If you ALREADY completed it, confirm now: update_working_memory(mark_task_done={_idx}) "
                                    "(or mark_all_done=true for all remaining) — then you may stop.\n"
                                    "- If real work is still left for it, do it now.\n"
                                    "Do NOT repeat work you have already done.]"
                                )
                            else:
                                _ac_content = (
                                    "[System: You still have pending tasks — do NOT stop yet. Work the next "
                                    "step NOW by calling the needed tools.\n"
                                    f">> CURRENT STEP {_done + 1}/{_total}: \"{_text}\" — finish it, then call "
                                    f"update_working_memory(mark_task_done={_idx}). If you ALREADY completed this "
                                    "step, mark that exact index now instead of redoing it — do NOT repeat an "
                                    "action that may already be complete (for example a purchase/payment) just "
                                    "because a step still looks open; mark the correct index or ask the user if "
                                    "unsure. Only stop when every task is done or you have a genuine question "
                                    "that requires the user.]"
                                )
                            self.history.append({"role": "system", "content": _ac_content})
                            UI.event(
                                "System",
                                f"Pending tasks remain — auto-continuing (step {_done + 1}/{_total}, "
                                f"turn {tool_turn_count}/{MAX_TOOL_TURNS_PER_STEP}).",
                                style="dim",
                            )
                            try:
                                append_domain_log(
                                    "backend",
                                    f"autocontinue step={_done + 1}/{_total} turn={tool_turn_count} stuck={self._autocontinue_stuck}",
                                )
                            except Exception:
                                pass
                            continue
            except Exception as _ac_e:
                try:
                    append_domain_log("backend", f"autocontinue_skip {type(_ac_e).__name__}: {_ac_e}")
                except Exception:
                    pass
            # ─────────────────────────────────────────────────────────────────────────

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
                 return self.chat_step(
                     user_input=user_input,
                     stream_callback=stream_callback,
                     auto_retry=True,
                     skip_input=skip_input,
                     thinking_mode=thinking_mode,
                 )
                 
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
        if tts_source.strip() and self._host_audio_allowed:  # host-speaker opt-in only
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
        """Dispatch one tool call through the full pipeline; returns a string.

        Pipeline: argument validation/repair -> policy evaluation (admin-only,
        channel blocks -> "Security Error: ...") -> confirmation gate (in
        noninteractive mode gated tools return an "[ERROR] ... requires
        confirmation" string instead of blocking) -> per-tool kwarg injection
        (identity/session/workspace) -> bounded execution with timeout and
        stop polling. Emits tool_start/tool_end/gate_* events to the optional
        event sink (schema: docs/OBSERVABILITY.md). Never raises for tool
        failures - errors come back as the result string.
        """
        from vaf.cli.ui import UI
        from pathlib import Path
        from vaf.core.trust import get_tool_policy, set_tool_policy, mark_trusted_dir, is_trusted_dir
        from vaf.core.tool_contract import evaluate_tool_policy

        # Thinking-only tool guard (runtime): never allow these in normal chat turns.
        is_thinking_turn = bool(getattr(self, "_current_turn_thinking_mode", False))
        if name == "thinking_done" and not is_thinking_turn:
            return "Error: 'thinking_done' is only available in background thinking mode."
        if str(name).startswith("thinking_workspace_") and not is_thinking_turn:
            return "Error: thinking_workspace tools are only available in background thinking mode."

        # So tools (e.g. document_writer) can notify Web UI; needed when run directly or via workflow in same process
        sid = None
        try:
            from vaf.core.subagent_ipc import get_current_session_id
            sid = get_current_session_id() or getattr(self, "current_session_id", None)
            if sid:
                os.environ["VAF_SESSION_ID"] = str(sid)
        except Exception:
            pass

        current_source = str(getattr(self, "_current_chat_source", "") or "").strip().lower()
        channel_sources = {"telegram", "whatsapp", "discord"}
        channel_session_prefixes = ("telegram_", "whatsapp_", "discord_")
        is_channel_session = (
            current_source in channel_sources
            or (isinstance(sid, str) and sid.startswith(channel_session_prefixes))
        )

        # Determine whether the current session belongs to an admin user.
        # Two sources are checked — whichever is set takes precedence:
        #   1. _current_user_role   — set from the WebSocket session metadata
        #      (e.g. "admin" / "user") when the user is authenticated via the DB.
        #   2. _current_user_scope_id vs get_local_admin_scope_id() — covers the
        #      single-user / local-admin case where no DB role is stored.
        # This value is passed into evaluate_tool_policy() so tools with
        # admin_only=True are hard-blocked for regular users before they run.
        try:
            from vaf.core.config import get_local_admin_scope_id as _get_local_admin
            _current_role  = getattr(self, "_current_user_role", None)
            _current_scope = getattr(self, "_current_user_scope_id", None)
            _local_admin   = _get_local_admin()
            is_admin = (
                _current_role == "admin"
                or (
                    _current_scope is not None
                    and str(_current_scope) == str(_local_admin)
                )
            )
        except Exception:
            # If we cannot determine admin status, default to False (safer).
            is_admin = False

        tool_instance = self.tools.get(name)
        policy_decision = evaluate_tool_policy(
            tool_name=name,
            tool=tool_instance,
            current_source=current_source,
            is_channel_session=is_channel_session,
            is_admin=is_admin,
        )
        if policy_decision.blocked:
            return f"Security Error: {policy_decision.reason}"

        # Plan gate (main agent only): require a plan before a state-changing tool runs.
        # Whare Wananga offline training probes the tool directly to learn its contract. The
        # interactive plan / confirmation gates below are live-chat UX (headless they return
        # [CANCELLED]/[ERROR], which would corrupt the probe and mislead the learner). The trainer
        # has its own safety tiering (error-path / declare / gated), so skip these gates while it
        # drives. Hard security blocks (policy_decision.blocked above) are NOT skipped.
        _ww = getattr(self, "_ww_training", False)
        if not _ww:
            gate_msg = self._plan_gate_decision(name, tool_instance)
            if gate_msg is not None:
                return gate_msg
            # Incident 2026-07-13 gates (order after the plan gate, before execution):
            # (a) a non-affirmative reply to a background question must not mutate state,
            # (c) while the agent awaits the user's answer, drain turns must not start
            # new write work. Both return confirm-style RESULTS (never raise/prompt).
            gate_msg = self._proactive_reply_gate_decision(name, tool_instance, args)
            if gate_msg is not None:
                return gate_msg
            gate_msg = self._ask_first_gate_decision(name, tool_instance)
            if gate_msg is not None:
                return gate_msg

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
            import uuid as _uuid

            if isinstance(obj, (Path, _uuid.UUID)):
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
            _mt_t0 = time.monotonic()
            result = run_multi_tool_use(args if isinstance(args, dict) else {})
            emit({
                "type": "tool_end", "tool": name,
                "duration_ms": int((time.monotonic() - _mt_t0) * 1000),
                "ok": True,  # the wrapper itself; each inner tool reports its own ok
            })
            return result

        # Gate risky tools with once/always/cancel (no persistent deny). Skipped during Whare
        # Wananga training (see _ww_training note above) so probes reach the tool's own validation.
        if policy_decision.requires_confirmation and not _ww:
            policy = get_tool_policy(name)
            cwd = Path.cwd()
            trusted = is_trusted_dir(cwd)
            allowed_once = name in self._allow_once_tools

            if policy != "allow" and not trusted and not allowed_once:
                # Build args preview for the Web UI dialog (truncated, no secrets)
                try:
                    _gate_args_preview = json.dumps(make_json_serializable(args or {}), ensure_ascii=False)[:300]
                except Exception:
                    _gate_args_preview = ""
                _gate_evt = {"type": "gate_required", "tool": name, "cwd": str(cwd),
                             "reason": policy_decision.reason, "args_preview": _gate_args_preview}
                emit(_gate_evt)
                # Also push directly via web_interface so it reaches the WebSocket
                # (emit/_event_sink is None in web context — tool events use this path instead)
                try:
                    from vaf.core.web_interface import get_web_interface as _gwi2
                    _gwi2()._push_session_update(getattr(self, "current_session_id", None), _gate_evt)
                except Exception:
                    pass

                if self._noninteractive:
                    return f"[ERROR] Tool '{name}' requires confirmation ({policy_decision.reason}). Re-run interactively or mark folder trusted."

                # Prefer WebSocket gate when a web session is active (pywebview / browser)
                _session = getattr(self, "current_session_id", None)
                _choice = None
                if _session:
                    try:
                        from vaf.core.web_interface import get_web_interface as _gwi
                        _gate_event, _decision_box = _gwi().register_gate(_session)
                        _granted = _gate_event.wait(timeout=300)  # 5-minute user timeout
                        _choice = _decision_box[0] if _granted else "cancel"
                    except Exception:
                        _choice = "cancel"
                else:
                    # Fallback: terminal prompt (CLI / headless mode)
                    UI.event("Security", f"Tool '{name}' requires confirmation. {policy_decision.reason}", style="warning")
                    _raw = UI.prompt("Allow? [o]nce / [a]lways / [c]ancel: ").strip().lower()
                    _choice = {"o": "allow_once", "once": "allow_once",
                               "a": "allow_always", "always": "allow_always"}.get(_raw, "cancel")

                if _choice == "allow_once":
                    self._allow_once_tools.add(name)
                    emit({"type": "gate_decision", "tool": name, "decision": "allow_once"})
                elif _choice == "allow_always":
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
        _tool_t0 = time.monotonic()
        try:
            if name in self.tools:
                tool_args = dict(args) if args else {}
                # --- Input validation & repair (before runtime-kwarg injection) ---
                # Validate model-supplied args against the tool's declared schema and
                # repair common weak-model shape mistakes (bare string for an array,
                # stringified array, null on an optional field, single-key placeholder).
                # Runs on raw model args only; injected runtime kwargs are added below.
                # Fully defensive: any failure here is a no-op and dispatch proceeds.
                _ti_errors = []
                try:
                    from vaf.core.tool_input_repair import repair_tool_input
                    tool_args, _ti_applied, _ti_errors = repair_tool_input(
                        getattr(self.tools[name], "parameters", None), tool_args,
                        getattr(self.tools[name], "input_aliases", None),
                    )
                    if _ti_applied:
                        try:
                            from vaf.core.log_helper import log_timeline_event as _lte
                            _lte('tool_input_repaired', tool=name,
                                 model=getattr(self, 'model_display_name', None),
                                 repairs=_ti_applied)
                        except Exception:
                            pass
                except Exception:
                    _ti_errors = []
                if name in ("memory_save", "memory_search"):
                    scope_id = getattr(self, "_current_user_scope_id", None)
                    tool_args["user_scope_id"] = scope_id
                    # Debug: Log user scope for RAG troubleshooting (consolidated in rag.log)
                    append_domain_log("rag", f"[Agent] {name} called with user_scope_id={scope_id}")
                if name == "ask_user":
                    # Both background runs must deliver to the RUNNING user's real scope/username: on a
                    # multi-user server a non-admin thinking run that left these blank fell back to the
                    # LOCAL ADMIN inside deliver_tracked_message — which, now that delivery goes to the
                    # configured main_messenger, would push that non-admin's private question to the
                    # admin's Telegram/WhatsApp/Discord. Inject the real scope/username in thinking mode
                    # AND automation; the handoff-bundle `_agent` is automation-only.
                    _au = getattr(self, "_run_kind", None) == "automation"
                    _th = getattr(self, "_run_kind", None) == "thinking"
                    if _au or _th:
                        tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                        tool_args["username"] = getattr(self, "_current_username", None) or "admin"
                    # ALWAYS pass the live agent: the tool branches on the CALLER's
                    # run kind itself (instance truth), so a thinking question can
                    # never take the automation-handoff path because some other
                    # run's env var happened to be set (incident 2026-07-13).
                    tool_args["_agent"] = self
                if name in ("update_intent", "update_working_memory"):
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                if name in ("set_timer", "list_timers", "cancel_timer"):
                    # Timer tools read the live session/source/identity off the agent.
                    tool_args["_agent"] = self
                if name == "schedule_reminder":
                    # Reminders are stored per user scope and fired with the OWNER's
                    # identity - never the process-global fallback (Rule 4.4).
                    tool_args["username"] = getattr(self, "_current_username", None) or "admin"
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                if name == "learn_document":
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                    tool_args["_agent"] = self
                if name == "learn_attached_knowledge":
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                    tool_args["session_id"] = getattr(self, "current_session_id", None)
                    tool_args["_agent"] = self
                if name == "analyze_image":
                    # The vision tool re-inspects the image attached to THIS session on demand.
                    # Pass the LIVE agent: on the upload turn the image lives in agent.history but
                    # is not persisted to disk until the turn ends, so a disk-only read would miss
                    # it (the primary "look closer on turn 1" case). session_id is the disk fallback
                    # (covers images that aged out of history via compaction but remain on disk).
                    tool_args["_agent"] = self
                    tool_args["session_id"] = getattr(self, "current_session_id", None)
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                if name == "librarian_agent":
                    # Drives the per-user filesystem jail (is_safe_path) so the librarian only reads the
                    # caller's own data, never another user's VAF_Projects/<uid8>.
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                if name == "browser_agent":
                    # Scope the persistent cookie/login store per user so one user's browser logins are
                    # never shared with or readable by another (the store dir is keyed by user_scope_id).
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                if name == "use_skill":
                    # Scope skill visibility to the calling user (None = admin).
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                if name in ("list_skills", "read_skill", "create_skill", "update_skill", "delete_skill"):
                    # Self-service skill management is user-isolated: list/read/create/edit/delete operate
                    # on the caller's own (or visible) skills only. None scope = admin (sees/edits all).
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                    tool_args["username"] = getattr(self, "_current_username", None) or "admin"
                if name == "update_user_identity":
                    tool_args["username"] = getattr(self, "_current_username", None) or "admin"
                if name == "document_writer":
                    # Same session race as write_file: the tool resolves the chat
                    # workspace itself - it must key on THIS session, never the
                    # process-global fallback (parallel workers).
                    tool_args["_session_id"] = getattr(self, "current_session_id", None)
                if name == "write_file":
                    # Main-agent file writes: relative paths resolve into THIS chat's workspace,
                    # the Web-UI file_created/document_created emits carry THIS session (emit-site
                    # scoping - never the process-global fallback), and non-admin (remote) users
                    # are jailed to their own VAF_Projects/<uid8> via the shared filesystem jail.
                    # The jail is applied inside WriteFileTool.run (contextvars do not propagate
                    # into the bounded-run worker thread). Direct WriteFileTool() consumers
                    # (coder, workflow engine, librarian, automations) pass none of these kwargs
                    # and keep their exact legacy behavior.
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                    tool_args["_session_id"] = getattr(self, "current_session_id", None)
                    try:
                        from vaf.core.platform import Platform as _PlatWF
                        from vaf.core.session import resolve_agent_output_dir as _resolve_out
                        tool_args["_session_workspace"] = str(_resolve_out(
                            _PlatWF.documents_dir() / "VAF_Projects",
                            session_id=getattr(self, "current_session_id", None),
                        ))
                    except Exception:
                        pass
                if name in ("send_telegram", "send_discord", "send_slack", "send_whatsapp", "send_to_user"):
                    tool_args["username"] = getattr(self, "_current_username", None) or "admin"
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                    tool_args["_agent"] = self  # lets send_whatsapp detect front_office_mode
                if name in ("python_sandbox", "python_exec"):
                    # Inject agent reference so with_vaf_tools=True can call back into the tool registry.
                    tool_args["_agent"] = self
                    if name == "python_sandbox":
                        # export_files copies artifacts into THIS chat's workspace -
                        # key on the session, never the process-global pointer.
                        tool_args["_session_id"] = getattr(self, "current_session_id", None)
                        # Per-user container workdir (/tmp/vaf_<scope12>_<exec>): the tool
                        # reads this kwarg, but the dispatcher never injected it, so every
                        # main-agent run landed in the shared prefix regardless of user.
                        # Direct assignment on purpose: model-supplied args must never
                        # override the server-side identity (spoof guard, like host_bash).
                        tool_args["user_scope_id"] = getattr(
                            self, "_current_user_scope_id", None
                        )
                    if name == "python_sandbox" and is_channel_session:
                        # Non-main channel sessions must not bridge host tools from sandbox code.
                        tool_args["with_vaf_tools"] = False
                if name == "host_bash":
                    # Authoritative channel flag for host_bash's own non-liftable guard. Set
                    # unconditionally so an LLM-supplied value cannot spoof it. host_bash refuses
                    # on channels even when channel_tools_unrestricted lifts the policy block.
                    tool_args["_is_channel_session"] = is_channel_session
                if name == "create_agent_tool":
                    # Inject agent reference so the tool can call reload_custom_tools()
                    # after writing the file — making the new tool live immediately
                    # without a server restart.
                    tool_args["_agent"] = self
                if name == "create_agent_workflow":
                    # Inject agent reference so the tool can:
                    #   - use the live tool registry for run_temp execution
                    #   - check admin status for create/delete actions
                    #   - pass user_scope_id / username to WorkflowEngine
                    tool_args["_agent"] = self
                if name == "execute_workflow":
                    # Inject agent so the tool can reliably get current_session_id
                    # for WebSocket pushes (module-global fallback is unreliable in threads)
                    tool_args["_agent"] = self
                if name == "checkpoint_context":
                    # Inject agent reference so checkpoint_context can call agent.checkpoint_and_reset()
                    tool_args["_agent"] = self
                if name in ("whatsapp_inbox", "find_whatsapp_messages", "read_whatsapp_chat", "whatsapp_call"):
                    tool_args["username"] = getattr(self, "_current_username", None) or "admin"
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                if name in ("telegram_inbox", "find_telegram_messages", "read_telegram_chat"):
                    tool_args["username"] = getattr(self, "_current_username", None) or "admin"
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                if name in ("discord_inbox", "find_discord_messages", "read_discord_chat"):
                    tool_args["username"] = getattr(self, "_current_username", None) or "admin"
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                if name in ("list_contacts", "get_contact", "create_contact", "update_contact", "delete_contact"):
                    tool_args["username"] = getattr(self, "_current_username", None) or "admin"
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                if name in ("mail_inbox", "read_mail", "find_mail", "mark_mail_answered", "label_mail", "list_email_accounts", "send_mail"):
                    tool_args["username"] = getattr(self, "_current_username", None) or "admin"
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                if name in ("add_automation_note", "add_automation_todo", "list_automation_notes", "list_automation_todos", "delete_automation_note", "delete_automation_todo"):
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                if name in ("create_automation", "list_automations", "read_automation", "update_automation", "delete_automation", "restore_automation", "list_trash"):
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                    tool_args["user_role"] = getattr(self, "_current_user_role", None)
                if name in ("thinking_workspace_read", "thinking_workspace_write", "thinking_workspace_handoff"):
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                if name in ("list_calendar_events", "create_calendar_event", "update_calendar_event", "delete_calendar_event"):
                    tool_args["username"] = getattr(self, "_current_username", None) or "admin"
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                if name in ("document_viewer", "document_editor", "replace_editor_selection"):
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                if name in ("github_list_repos", "github_get_file", "github_get_file_structure", "github_list_directory", "github_get_tree", "github_search_files", "github_list_issues", "github_list_pulls", "github_create_issue", "github_update_file"):
                    tool_args["username"] = getattr(self, "_current_username", None) or "admin"
                    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)
                # Pre-write intent/goal before sub-agent invocation for validation/retry
                SUBAGENT_TOOLS = ("librarian_agent", "coding_agent", "research_agent", "document_agent")
                # HARD anti-re-delegation guard: while a sub-agent of the SAME type genuinely
                # runs for this session (heartbeat-verified via the shared liveness helper),
                # refuse to spawn a duplicate. The prompt-level prohibition is soft; the
                # repeated-tool-call dedup is bypassed after any user message — so chatting
                # while a coder runs could otherwise spawn a second coder into the same
                # workspace. Same-type-only: mixed delegation (e.g. research while coding)
                # stays possible. Also skips the intent pre-write below so a refused spawn
                # cannot clobber the RUNNING task's delegation-intent slot.
                _subagent_dup_msg = None
                if name in SUBAGENT_TOOLS:
                    try:
                        _live_same = [
                            t for t in self.get_live_session_subagents()
                            if t.get("agent_type") == name
                        ]
                        if _live_same:
                            _t0 = _live_same[0]
                            _subagent_dup_msg = (
                                f"⚠️ A {name} is ALREADY RUNNING on: "
                                f"{(_t0.get('task_description') or 'the delegated task')[:200]} "
                                f"(running {max(0, int(_t0.get('running_seconds', 0)) // 60)} min).\n"
                                "Not starting a duplicate — the result will arrive automatically "
                                "when it finishes. Tell the user the task is already in progress."
                            )
                    except Exception:
                        _subagent_dup_msg = None
                if name in SUBAGENT_TOOLS and _subagent_dup_msg is None and hasattr(self, "main_persistence") and self.main_persistence:
                    intent = ""
                    try:
                        intent = self.main_persistence.get_user_intent() or ""
                    except Exception:
                        pass
                    if not intent and self.history:
                        for msg in reversed(self.history):
                            if isinstance(msg, dict) and msg.get("role") == "user":
                                intent = msg.get("content", "") or ""
                                break
                    goal = self._extract_subagent_goal(name, tool_args)
                    if intent or goal:
                        self.main_persistence.write_subagent_delegation_intent(intent, goal, name)
                # Self-supervised tools (browser_agent + workflow orchestrators) manage their
                # own cancellation/lifecycle and are legitimately long-running — bounding them
                # here would abandon them mid-work (zombie) while they're still making progress.
                # Run them directly; they handle their own Stop + internal limits.
                from vaf.core.bounded_run import SELF_SUPERVISED_TOOLS as _SELF_SUPERVISED
                if _ti_errors:
                    # Args still violate the tool's declared schema after repair:
                    # return a localized error to the model instead of dispatching
                    # with invalid input. Keep the "Tool Error:" prefix so is_err and
                    # the Whare Wananga reactive-retry keep recognising it unchanged.
                    result = "Tool Error: invalid arguments for '%s': %s" % (name, "; ".join(_ti_errors))
                elif _subagent_dup_msg is not None:
                    # Anti-re-delegation: a same-type sub-agent already runs for this session.
                    result = _subagent_dup_msg
                elif name in _SELF_SUPERVISED:
                    result = self.tools[name].run(**tool_args)
                else:
                    # Bounded execution: never let a single in-process tool block the worker
                    # forever, and poll the user's Stop flag *during* the call so the Stop
                    # button actually works. See vaf/core/bounded_run.py.
                    from vaf.core.bounded_run import run_bounded as _run_bounded, agent_timeout_seconds as _agent_to
                    from vaf.core.config import Config as _CfgTT
                    _tool_to = _agent_to(name)   # per-agent budget
                    def _tool_stop_check():
                        try:
                            if not sid:
                                return False
                            from vaf.core.task_queue import TaskQueue as _TQ
                            return bool(_TQ().should_stop(sid))
                        except Exception:
                            return False
                    result = _run_bounded(
                        lambda: self.tools[name].run(**tool_args),
                        timeout=_tool_to,
                        stop_check=_tool_stop_check,
                        poll=float(_CfgTT.get("tool_stop_poll_seconds", 0.5)),
                        label=name,
                    )
            else:
                result = f"Error: Unknown tool '{name}'"
        except Exception as e:
            result = f"Tool Error: {e}"

        # search_tools post-hook: expand _active_tools with discovered tool names so the
        # model can call them in the very next turn without a router round-trip.
        # The parser is SHARED with the tool module (and its format tests), so the
        # output format and this hook can never drift apart silently.
        if name == "search_tools" and isinstance(result, str) and ":" in result:
            try:
                from vaf.tools.search_tools import extract_discovered_tool_names
                discovered = extract_discovered_tool_names(result, self.tools)
                if discovered and self._active_tools is not None:
                    current = list(self._active_tools)
                    for t in discovered:
                        if t not in current:
                            current.append(t)
                    self._active_tools = current
            except Exception:
                pass

        # If python_sandbox blocked the request, offer a gated fallback to python_exec
        # (once/always/cancel) so the user can explicitly override sandbox restrictions.
        if (
            name == "python_sandbox"
            and not is_channel_session
            and isinstance(result, str)
            and result.startswith("Security Error:")
        ):
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
                    _pe_t0 = time.monotonic()
                    try:
                        unsafe_result = self.tools["python_exec"].run(code=code, timeout=30)
                    except Exception as e:
                        unsafe_result = f"Tool Error: {e}"
                    emit({
                        "type": "tool_end", "tool": "python_exec",
                        "duration_ms": int((time.monotonic() - _pe_t0) * 1000),
                        "ok": not str(unsafe_result).startswith("Tool Error:"),
                    })
                    self._record_tool_used("python_exec")
                    result = unsafe_result

        # ok reflects DISPATCH-level failure (exception -> "Tool Error:", or an
        # unknown tool name) - NOT the semantic success of the tool's output, so
        # this stays an explicit narrow check rather than the broad
        # tool_result_is_error helper (which also flags "Failed..."-style
        # semantic outputs the observability contract must not mark not-ok).
        _tool_ok = not (
            isinstance(result, str)
            and (result.startswith("Tool Error:") or result.startswith("Error: Unknown tool"))
        )
        emit({
            "type": "tool_end", "tool": name,
            "duration_ms": int((time.monotonic() - _tool_t0) * 1000),
            "ok": _tool_ok,
        })
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
        """Set an optional event sink for structured outputs (e.g. stream-json).

        Also attaches the sink to the API backend so llm_start/llm_end events
        flow through the same callback (schema: docs/OBSERVABILITY.md).
        """
        self._event_sink = sink
        backend = getattr(self, "api_backend", None)
        if backend is not None:
            backend.event_sink = sink

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
                f"Analyze this AI response. Did the AI CLAIM to use a tool RIGHT NOW (future/present), but didn't execute it?\n"
                f"Response: \"{text[:600]}\"\n"
                f"Tools Executed: None\n\n"
                f"Rules:\n"
                f"- FALSE_PROMISE (forward-looking, not yet done): \"I am using `read_file`...\", \"Let me search...\", \"I'll execute this now...\"\n"
                f"- SAFE (past analysis, recommendation, or explanation): \"I called `read_file` three times\", \"Ich hab den librarian_agent aufgerufen\", \"You can use `web_search`\", \"The tool had no write access\", analytical summaries of what already happened\n\n"
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

    def _turn_tool_results(self) -> list:
        """The actual tool outcomes of the CURRENT turn: walk history back to the last user message
        and collect the role='tool' entries since then as (tool_name, truncated_result) pairs, in
        order. Used by result grounding to compare the reply's claims against what tools returned."""
        out = []
        hist = getattr(self, "history", None) or []
        for msg in reversed(hist):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == "user":
                break
            if role == "tool":
                name = str(msg.get("name") or "tool")
                content = str(msg.get("content") or "")
                out.append((name, content[:500]))
        out.reverse()
        return out

    def _detect_ungrounded_result_claim(self, response_text: str, turn_results: list):
        """
        Result grounding (anti-confabulation): does the reply assert a concrete tool OUTCOME — a
        success, a failure, a saved/created file, a specific error, or a result/count — that the
        turn's ACTUAL tool results do not support, INCLUDING claiming a result for a tool that was
        never run this turn? Returns (ungrounded, claim).

        Conservative by design: a cheap keyword/regex pre-filter gates the LLM judge so ordinary
        replies cost nothing, and any failure returns (False, None) so the guard never blocks a reply.
        """
        text = (response_text or "").strip()
        if len(text) < 12:
            return False, None

        # Pre-filter: only replies that actually assert a tool outcome are worth the LLM check.
        import re as _re
        _low = text.lower()
        _outcome_kw = (
            "failed", "success", "succeed", "saved", "wrote", "written", "created", "deleted",
            "removed", "sent", "crashed", "error", "not found", "no results", "executed",
            "task complete", "fehlgeschlagen", "gespeichert", "erstellt", "gelöscht", "gesendet",
            "ausgeführt", "bestätigt", "nicht gefunden", "kein ergebnis",
        )
        _has_outcome = any(k in _low for k in _outcome_kw) or bool(
            _re.search(r'[✗✅❌]|found\s+\d+|\b\d+\s+(results|treffer|dateien|files)\b', text, _re.I)
        )
        if not _has_outcome:
            return False, None

        if not (getattr(self, "use_server", False) or getattr(self, "api_backend", None) or getattr(self, "llm", None)):
            return False, None

        _results_block = (
            "\n".join(f"- {n}: {(c or '')[:300]}" for n, c in turn_results)
            if turn_results else "(no tools were run this turn)"
        )
        prompt = (
            "You verify an AI assistant reply against the ACTUAL tool results of this turn.\n"
            "Does the reply assert a concrete tool OUTCOME (a success, a failure, a saved/created "
            "file, a specific error, or a result/count) that the tool results below do NOT support — "
            "including claiming a result for a tool that was never run this turn?\n\n"
            f"ASSISTANT REPLY:\n{text[:900]}\n\n"
            f"ACTUAL TOOL RESULTS THIS TURN:\n{_results_block[:1500]}\n\n"
            "Reply with EXACTLY one of:\n"
            "- GROUNDED (every concrete outcome in the reply is supported by the results, or the reply "
            "makes no concrete outcome claim)\n"
            "- UNGROUNDED (the reply claims an outcome not supported / not actually performed)\n"
            "If UNGROUNDED, add on the next line: CLAIM: [the unsupported claim, short]"
        )
        try:
            content = self._run_validation_llm([{"role": "user", "content": prompt}], max_tokens=60)
        except Exception:
            return False, None
        if "UNGROUNDED" not in (content or "").upper():
            return False, None
        claim = None
        for line in (content or "").splitlines():
            if "claim:" in line.lower():
                claim = line.split(":", 1)[-1].strip()
                break
        return True, (claim or "a tool outcome that did not actually happen this turn")

    def _clear_last_assistant_ui(self, session_id) -> None:
        """Ask the Web UI to drop the just-produced (faulty) assistant bubble before a retry/correction.

        NEVER fires during a thinking (background) run. There the "last assistant" bubble is the user's
        previous real answer -- not anything produced this turn -- so clearing it would REPLACE a real
        message (observed: a background pass wiped a research answer and showed "Nothing actionable").
        Background runs must only ever append below, never replace. `_emit_to_web_ui()` is False in
        thinking mode, so all clears route through this single guard.
        """
        if not _emit_to_web_ui():
            return
        try:
            from vaf.core.web_interface import get_web_interface
            get_web_interface().emit_clear_last_assistant(session_id)
        except Exception:
            pass

    def _consolidate_system_messages(self, messages: List[Dict]) -> List[Dict]:
        """Strict-local-template system-message consolidation (Qwen/Gemma 4: one leading system turn;
        mid-run system nudges -> user turns in place). Delegates to the shared pure helper so the coder
        path (which builds its own history and calls the provider directly) uses the exact same logic."""
        from vaf.core.api_backend import consolidate_system_messages
        return consolidate_system_messages(messages)

    def _prepare_messages(self, messages: List[Dict]) -> List[Dict]:
        """Prepare messages for specific model quirks (e.g. Gemma)."""
        # --- Universal: strip dangling tool_calls --------------------------
        # After context compression the recent_messages slice may contain an
        # assistant message with tool_calls whose corresponding tool response
        # was in the discarded middle section.  APIs (e.g. DeepSeek) reject
        # this with HTTP 400 "insufficient tool messages following tool_calls".
        #
        # POSITION-AWARE: context compression may inject preserved critical
        # role:tool results (from the middle section) BEFORE recent_messages,
        # which can make a role:tool appear at an earlier index than its
        # assistant+tool_calls message. A naive set-membership check would
        # incorrectly consider such a TC "responded to", leaving the API with
        # a TC message that has no following tool response → 400.
        # Fix: only count a role:tool as a valid response if it appears at an
        # index AFTER the assistant+tool_calls message that issued the call.
        # Position-aware AND duplicate-id-safe pairing: each assistant tool_call
        # claims the NEAREST following UNCLAIMED role:tool with the same id. A
        # single global "first response per id" map mis-handles DUPLICATE ids
        # (which occur if a turn ever gets replayed/duplicated into history): the
        # 2nd assistant's call looks unanswered — its only recorded response is the
        # early one — so its tool_calls get stripped, yet the 2nd tool result
        # survives → an orphaned role:tool that APIs (e.g. DeepSeek) 400 on.
        _tool_idxs_by_id: dict = {}
        for _i, _m in enumerate(messages):
            if _m.get("role") == "tool" and _m.get("tool_call_id"):
                _tool_idxs_by_id.setdefault(_m["tool_call_id"], []).append(_i)
        _claimed: set = set()

        cleaned: List[Dict] = []
        for _i, msg in enumerate(messages):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                live_calls = []
                for tc in msg["tool_calls"]:
                    _resp = next(
                        (j for j in _tool_idxs_by_id.get(tc.get("id"), [])
                         if j > _i and j not in _claimed),
                        None,
                    )
                    if _resp is not None:
                        _claimed.add(_resp)
                        live_calls.append(tc)
                if len(live_calls) != len(msg["tool_calls"]):
                    # Some calls have no following response — rebuild the message. Also
                    # drop the Anthropic raw-block replay cache: it holds the ORIGINAL
                    # tool_use blocks, which would now reference dropped tool_results
                    # (-> 400). The converter re-synthesizes from the trimmed tool_calls.
                    msg = {k: v for k, v in msg.items() if k != "_anthropic_blocks"}
                    if live_calls:
                        msg["tool_calls"] = live_calls
                    else:
                        # All calls are dangling — drop tool_calls entirely
                        msg = {k: v for k, v in msg.items() if k != "tool_calls"}
            # MUST stay unconditional (exactly one append per input msg, no
            # continue/skip): _claimed holds indices into `messages`, and the
            # filter below indexes into `cleaned` — they only align while cleaned
            # mirrors messages 1:1.
            cleaned.append(msg)

        # Drop role:tool results that no surviving assistant tool_call claimed
        # (orphans — their assistant+tool_calls was stripped above or lost to
        # compression). Leaving them causes API errors about role:tool placement.
        messages = [
            _m for _i, _m in enumerate(cleaned)
            if not (_m.get("role") == "tool" and _i not in _claimed)
        ]

        # --- Thinking-mode reply context injection ----------------------------
        # When the agent reached out to the user during a background thinking
        # pass and the user is now replying, inject a system note just before
        # the final user message so the LLM understands the reply context.
        # This is done here (not on user_input) so self.history stays clean and
        # the [Context: ...] text never appears in WebUI chat bubbles.
        _thinking_ctx = getattr(self, "_thinking_reply_context", None)
        if _thinking_ctx:
            # Inject for EVERY generation of this turn — do NOT consume here. A reply turn can run
            # multiple generations (the first may make a tool call, then a second produces the answer);
            # consuming on the first left the answer generation context-less (observed: background agent
            # asked about WM dates, user replied, main agent answered "that's vague"). It is reset per
            # turn at chat_step entry instead.
            # Find index of the last user message
            _last_user_idx = None
            for _i in range(len(messages) - 1, -1, -1):
                if messages[_i].get("role") == "user":
                    _last_user_idx = _i
                    break
            if _last_user_idx is not None:
                messages = (
                    messages[:_last_user_idx]
                    + [{"role": "system", "content": _thinking_ctx}]
                    + messages[_last_user_idx:]
                )

        # --- Vision capability check -----------------------------------------
        # Determine whether the active provider+model supports multimodal input.
        # Models that silently return empty responses (e.g. deepseek-chat) must
        # receive a text-only fallback instead of an image_url block.
        _provider = getattr(self, "provider", "local")
        # Read model fresh from config so mid-session model changes (via Settings) are picked up immediately.
        if _provider != "local":
            _model = (self.config.get(f"api_model_{_provider}") or getattr(self, "model_display_name", "")).lower()
            # Re-read from disk in case user changed model in Settings during this session
            try:
                from vaf.core.config import Config as _Cfg
                _live_model = _Cfg.load().get(f"api_model_{_provider}", "")
                if _live_model:
                    _model = _live_model.lower()
            except Exception:
                pass
        else:
            _model = getattr(self, "model_display_name", "").lower()

        # Shared registry predicate (formerly a local copy that had drifted from
        # vision_infer.py's - it did not know veyllo). provider_registry is
        # deliberately import-light, so the lazy import is cheap.
        from vaf.core.provider_registry import model_supports_vision as _model_supports_vision

        # probe_local=False: _prepare_messages runs on EVERY LLM round trip;
        # the historical behavior here was local=capable without a probe, and
        # a blocking HTTP check per turn is not acceptable on this hot path.
        _vision_ok = _model_supports_vision(_provider, _model, probe_local=False)

        # --- Convert inline images to OpenAI multimodal content blocks ------
        # History entries may carry an "images" key: [{data: base64, mime_type: str}].
        # If vision is supported: convert to OpenAI list-content format (Anthropic/Google
        # providers convert further in their own chat_completion methods).
        # If NOT supported: strip images, append human-readable text placeholder instead.
        # vision_mode (default "description_tool"): the main reasoning model is TEXT-ONLY.
        # Each attached image is replaced by a VISUAL CONTEXT text block built from its
        # one-time base description (generated in chat_step, persisted, reload-safe); the
        # model inspects the stored image on demand via analyze_image. No raw base64 ever
        # reaches the main model. "inline_multimodal" restores the legacy behavior below
        # (raw image blocks straight to a multimodal main model / per-turn vision fallback).
        _legacy_inline = (Config.get("vision_mode", "description_tool") or "description_tool").strip() == "inline_multimodal"
        multimodal_messages: List[Dict] = []
        for msg in messages:
            if msg.get("role") == "user" and msg.get("images"):
                imgs = msg["images"]
                text = msg.get("content", "")
                msg = {k: v for k, v in msg.items() if k != "images"}
                if not _legacy_inline:
                    # Option A (default): text-only main model — inject the base description,
                    # never the raw bytes. The image stays on disk for analyze_image to fetch.
                    from vaf.core.vision_infer import build_visual_context_text
                    msg["content"] = build_visual_context_text(imgs, text)
                elif _vision_ok:
                    blocks: List[Dict] = []
                    if text:
                        blocks.append({"type": "text", "text": text})
                    from vaf.core.image_utils import downscale_image_b64 as _downscale_img
                    from vaf.core.vision_infer import image_to_b64 as _img_to_b64
                    _img_max_edge = int(Config.get("vision_image_max_edge", 2000) or 2000)
                    _img_quality = int(Config.get("vision_image_jpeg_quality", 85) or 85)
                    for img in imgs:
                        _got = _img_to_b64(img)  # data (legacy) or path (current)
                        if not _got:
                            continue
                        raw, mime = _got
                        # Shrink oversized images: full-res photos make OpenAI 500 and waste tokens.
                        raw, mime = _downscale_img(raw, mime, _img_max_edge, _img_quality)
                        blocks.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{raw}"},
                        })
                    msg["content"] = blocks
                else:
                    # Non-vision model: try configured vision fallback provider first.
                    _vision_fb_provider = Config.get("vision_provider", "").strip()
                    _vision_fb_model = Config.get("vision_model", "").strip() or None
                    _vision_fb_used = False

                    # Safe default vision model per provider (used when vision_model is
                    # not set): shared registry lookup that tracks Config.PROVIDER_MODELS
                    # for SDK providers and keeps openrouter on an explicitly
                    # vision-capable route (the former local dict did not know veyllo).
                    if not _vision_fb_model and _vision_fb_provider:
                        from vaf.core.provider_registry import default_vision_model as _dvm
                        _vision_fb_model = _dvm(_vision_fb_provider)

                    if _vision_fb_provider and _vision_fb_provider != _provider:
                        try:
                            from vaf.core.api_backend import APIBackendManager as _APIBM
                            from vaf.core.log_helper import append_domain_log as _adl
                            _vb = _APIBM(_vision_fb_provider)
                            # Build a single-turn multimodal message for the vision backend
                            _vb_blocks: List[Dict] = []
                            if text:
                                _vb_blocks.append({"type": "text", "text": text})
                            from vaf.core.image_utils import downscale_image_b64 as _downscale_img
                            from vaf.core.vision_infer import image_to_b64 as _img_to_b64
                            _img_max_edge = int(Config.get("vision_image_max_edge", 2000) or 2000)
                            _img_quality = int(Config.get("vision_image_jpeg_quality", 85) or 85)
                            for _vi in imgs:
                                _got = _img_to_b64(_vi)  # data (legacy) or path (current)
                                if not _got:
                                    continue
                                _raw, _mime = _got
                                _raw, _mime = _downscale_img(_raw, _mime, _img_max_edge, _img_quality)
                                _vb_blocks.append({
                                    "type": "image_url",
                                    "image_url": {"url": f"data:{_mime};base64,{_raw}"},
                                })
                            _vb_msgs = [{"role": "user", "content": _vb_blocks}]
                            _adl("backend", f"[VISION_FALLBACK] calling {_vision_fb_provider}/{_vision_fb_model or 'default'} for {len(imgs)} image(s)")
                            _vb_resp_parts = []
                            for _chunk in _vb.chat_completion(
                                _vb_msgs,
                                model=_vision_fb_model,
                                temperature=0.2,
                                max_tokens=1024,
                                stream=True,
                                tools=None,
                            ):
                                if isinstance(_chunk, str):
                                    _vb_resp_parts.append(_chunk)
                                elif isinstance(_chunk, dict):
                                    _vb_resp_parts.append(_chunk.get("content") or "")
                            _vb_resp = "".join(_vb_resp_parts).strip()
                            _adl("backend", f"[VISION_FALLBACK] response len={len(_vb_resp)} ok={bool(_vb_resp)}")
                            if _vb_resp:
                                _names = [_vi.get("name", "image") for _vi in imgs]
                                _img_list = ", ".join(_names)
                                vision_injection = (
                                    f"[Vision ({_vision_fb_provider}/{_vision_fb_model or 'default'}): "
                                    f"Image(s) {_img_list} analysed]\n{_vb_resp}"
                                )
                                msg["content"] = (vision_injection + "\n\n" + text).strip() if text else vision_injection
                                _vision_fb_used = True
                        except Exception as _vfe:
                            try:
                                from vaf.core.log_helper import append_domain_log as _adl2
                                _adl2("backend", f"[VISION_FALLBACK] FAILED provider={_vision_fb_provider} error={_vfe}")
                            except Exception:
                                pass

                    if not _vision_fb_used:
                        # No vision fallback configured or it failed: inform user.
                        names = [img.get("name", "image") for img in imgs]
                        img_list = ", ".join(f"`{n}`" for n in names)
                        _vision_models = {
                            "deepseek": "a different provider — DeepSeek's API does not support image input. Switch to Anthropic (`claude-sonnet-4-6`) or OpenAI (`gpt-4o`), or configure a Vision Model in Settings → AI & Model.",
                            "openai":   "`gpt-4o` or `gpt-4-turbo`",
                            "anthropic": "`claude-sonnet-4-6` or newer",
                            "google":   "`gemini-2.5-flash` or `gemini-2.5-pro`",
                            "openrouter": "a vision model such as `openai/gpt-4o`",
                        }
                        _vision_hint = _vision_models.get(_provider, "a vision-capable model, or configure a Vision Model in Settings → AI & Model")
                        system_note = (
                            f"[SYSTEM NOTE: The user attached {len(imgs)} image(s) ({img_list}) "
                            f"but the current model ({_model or _provider}) does not support vision. "
                            f"The image data has been stripped and cannot be recovered. "
                            f"Please tell the user: this model cannot see images. "
                            f"To use vision, go to Settings → AI & Model and configure a Vision Model, "
                            f"or switch the primary provider to {_vision_hint}. "
                            f"Do NOT attempt to guess the image content or use list_files/find_files tools.]"
                        )
                        msg["content"] = (system_note + "\n\n" + text).strip() if text else system_note
                        try:
                            from vaf.cli.ui import UI as _UI
                            _UI.warning(
                                f"Vision not supported by {_model or _provider}. "
                                f"Image stripped — configure a Vision Model in Settings or switch provider."
                            )
                        except Exception:
                            pass
            multimodal_messages.append(msg)
        messages = multimodal_messages
        # -------------------------------------------------------------------

        # DeepSeek: restore reasoning_content as a separate field in assistant history messages.
        # When DeepSeek returns reasoning_content we store it inline as <think>...</think> in content.
        # On the next API call, DeepSeek 400s with "reasoning_content in thinking mode must be
        # passed back" unless we include it as a separate "reasoning_content" field in the message.
        if _provider == "deepseek":
            import re as _re
            _think_re = _re.compile(r"<think>(.*?)</think>", _re.DOTALL)
            fixed = []
            for msg in messages:
                if msg.get("role") == "assistant":
                    content = msg.get("content") or ""
                    if isinstance(content, str) and "<think>" in content:
                        match = _think_re.search(content)
                        if match:
                            reasoning = match.group(1).strip()
                            remaining = _think_re.sub("", content).strip()
                            msg = dict(msg)
                            msg["reasoning_content"] = reasoning
                            # content must be a non-null string; use empty string if nothing left
                            msg["content"] = remaining or ""
                fixed.append(msg)
            messages = fixed
        elif _provider == "veyllo":
            # Veyllo rejects replayed tool_call ids it did not issue itself.
            # Exchanges whose ids VAF minted (text-recovered calls, id-less
            # streams) are folded into plain text pre-send; genuine exchanges
            # replay byte-identical. See _downgrade_synthetic_tool_exchanges.
            messages = _downgrade_synthetic_tool_exchanges(messages)

        is_gemma = getattr(self, "is_gemma_local", "gemma" in self.filename.lower())
        if not is_gemma:
            # Strict local chat templates (e.g. Qwen) require exactly ONE system message, at the start.
            # VAF injects system messages mid-conversation (memory context, first-time hint, loop nudges);
            # merge them into the leading one for LOCAL models. API providers accept multiple -> unchanged.
            if getattr(self, "provider", None) == "local":
                return self._consolidate_system_messages(messages)
            return messages
        
        # Gemma 3n: merge System into the first User turn (it has no native system role).
        # Gemma 4: keep a native `system` role (the GGUF template renders it as a <|turn>system block,
        # where tool instructions belong) so they are not buried in the user turn; only the
        # alternation handling is shared between the two.
        is_gemma4 = (getattr(self, "model_mode", None) == "gemma4")
        new_messages = []
        pending_system = ""        # folds into the next/trailing USER turn (3n always; gemma4: NON-leading system)
        leading_system = ""        # gemma4 only: the leading (main) system prompt -> native front system turn
        seen_non_system = False    # have we passed the first non-system message yet?

        for msg in messages:
            role = msg.get("role")
            raw_content = msg.get("content", "")
            # Flatten multimodal list to text for Gemma (vision not supported)
            if isinstance(raw_content, list):
                content = " ".join(
                    b.get("text", "") for b in raw_content if b.get("type") == "text"
                )
            else:
                content = str(raw_content)
            
            if role == "system":
                # Gemma 4: the LEADING system block (before any user/assistant) becomes the native
                # front system turn. A system message that arrives LATER is an interstitial nudge
                # (e.g. the empty-response / false-promise correction); fold it into a trailing USER
                # turn instead -- never to the front -- otherwise the prior assistant message ends up
                # last and the template rejects the request: "Assistant response prefill is
                # incompatible with enable_thinking" (400). Gemma 3n folds all system into user as before.
                if is_gemma4 and not seen_non_system:
                    leading_system += f"{content}\n\n"
                else:
                    pending_system += f"{content}\n\n"
            elif role == "user":
                seen_non_system = True
                if pending_system:
                    content = f"{pending_system}{content}"
                    pending_system = ""

                # Check alternation: If last was user, merge this one
                if new_messages and new_messages[-1]["role"] == "user":
                    new_messages[-1]["content"] += f"\n\n{content}"
                else:
                    new_messages.append({"role": "user", "content": content})

            elif role == "assistant":
                seen_non_system = True
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
                seen_non_system = True
                # Gemma requires User -> Assistant -> User -> ...
                # Tool responses usually follow Assistant (tool calls).
                # But sometimes we have multiple Tool responses.
                # If we send Tool, llama.cpp handles it, but we must ensure it follows Assistant.
                # And after Tool, we need Assistant (or User? No, model replies).
                new_messages.append(msg)

        # Flush leftover NON-leading system as a trailing USER turn (3n: all system; gemma4: only
        # interstitial nudges). This steers the model without leaving a trailing assistant "prefill".
        if pending_system:
            new_messages.append({"role": "user", "content": pending_system.strip()})
        # Gemma 4: the leading/main system prompt goes to the very front as a native system turn
        # (the template renders it as <|turn>system, keeping tool instructions out of the user turn).
        if leading_system:
            new_messages.insert(0, {"role": "system", "content": leading_system.strip()})

        return new_messages

    @property
    def TOOLS(self):
        """Dynamic Tool Schema Generation with Context-Aware Optimization"""
        # Whare Wananga: once per agent, invalidate learned know-how whose tool definition changed
        # (schema-hash mismatch) -> mark it 'stale'. The delivery gate requires status=confirmed, so
        # a stale record is then never injected (A) or re-fed (B) until the tool is re-trained.
        if not getattr(self, "_ww_stale_checked", False):
            self._ww_stale_checked = True
            try:
                from vaf.whare_wananga import store as _ww_store
                _stale = _ww_store.invalidate_stale(self.tools)
                if _stale:
                    append_domain_log("backend", f"[WW-STALE] tool definition changed -> marked stale: {_stale}")
            except Exception:
                pass
        n_ctx = self.config.get("n_ctx", 8192)
        # With 100+ tools, even 32K contexts are "small" — truncate descriptions
        # to keep total tool schema tokens manageable (system prompt ~5.5K + tools budget ~6K).
        is_small_context = n_ctx < 32000

        # Use active tools if available, otherwise all tools
        tools_to_use = self._active_tools if self._active_tools is not None else self.tools.keys()
        excluded = getattr(self, "_excluded_tools", None) or set()

        # Cache the built schema. This property sits on the HOT PATH of every LLM call and was rebuilt --
        # re-running Whare Wananga pitfall injection for each tool -- on EVERY access: thousands of times
        # per session (8478 [WW-INJECT] in one run), churning memory. Rebuild only when the scoping inputs
        # actually change (router re-scope, exclusions, or context size); the router re-scopes per turn, so
        # newly learned pitfalls are still picked up on the next turn.
        _cache_key = (frozenset(tools_to_use), frozenset(excluded), n_ctx)
        if (getattr(self, "_tools_schema_cache_key", None) == _cache_key
                and getattr(self, "_tools_schema_cache", None) is not None):
            return self._tools_schema_cache

        schema = []
        for name in tools_to_use:
            if name not in self.tools or name in excluded:
                continue
            tool = self.tools[name]

            # Use get_description_with_examples() when available (BaseTool subclasses),
            # which appends up to 3 inline example calls so every provider sees usage patterns.
            if hasattr(tool, "get_description_with_examples"):
                description = tool.get_description_with_examples()
            else:
                description = tool.description

            # Context Optimization: Truncate descriptions for small contexts.
            # When examples are present we allow a wider budget (300 chars) so at
            # least one example survives the truncation.
            if is_small_context and description:
                budget = 300 if (getattr(tool, "input_examples", None)) else 150
                if len(description) > budget:
                    description = description[:budget - 3] + "..."

            # Whare Wananga delivery: append the tool's LEARNED pitfalls (tuatea) to its description
            # so the model sees them inline before forming the call. Only when the router has scoped
            # the tool set (_active_tools is not None) -- the all-tools fallback (100+) would blow the
            # token budget. Hard-guarded: this is the critical path of every LLM call, so a failure
            # here must never break tool-calling.
            if self._active_tools is not None:
                try:
                    from vaf.whare_wananga.delivery import tool_pitfalls
                    _pf = tool_pitfalls(
                        name,
                        max_pitfalls=(1 if is_small_context else 3),
                        max_chars=(80 if is_small_context else 320),
                    )
                    if _pf:
                        description = (description or "") + "\n" + _pf
                        try:
                            append_domain_log("backend", f"[WW-INJECT] {name}: +{len(_pf)} chars pitfalls")
                        except Exception:
                            pass
                except Exception:
                    pass

            schema.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": getattr(tool, "parameters", {"type": "object", "properties": {}})
                }
            })
        self._tools_schema_cache = schema
        self._tools_schema_cache_key = _cache_key
        return schema
