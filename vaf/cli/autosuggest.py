# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Smart AutoSuggest - Inline word completion like Google Search
Cross-Platform: Windows, macOS, Linux

The learned corpus is made of what a person TYPED, so it belongs to that person: one corpus per
account (`autosuggest_for`), written encrypted and owner-only like the chats it was learned from
(vaf/core/data_files.py). Measured before: the web server learned every message of every account
into one shared file (0644, plaintext) and answered every connection's suggestion request from it,
so a word one person had typed - a name, an address, a password - could be offered to another.
"""
import threading
import re
from pathlib import Path
from typing import Optional, List, Dict, Set
from collections import Counter
from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.document import Document
from prompt_toolkit.buffer import Buffer


class SmartAutoSuggest(AutoSuggest):
    """
    Intelligent inline autocomplete that suggests the next word(s).
    
    Features:
    - Learns from user input history
    - Suggests common phrases and commands
    - Context-aware suggestions
    - Cross-platform (Windows, macOS, Linux)
    
    Usage:
        session = PromptSession(auto_suggest=SmartAutoSuggest())
    """
    
    # Common phrases for coding/AI assistants
    COMMON_PHRASES = {
        # English
        "how": ["how do I", "how can I", "how to"],
        "can": ["can you", "can you help", "can you show me"],
        "what": ["what is", "what are", "what does"],
        "where": ["where is", "where are", "where can I find"],
        "show": ["show me", "show me the", "show me how to"],
        "create": ["create a", "create a new", "create a function"],
        "add": ["add a", "add a new", "add a function"],
        "fix": ["fix the", "fix this", "fix the bug"],
        "find": ["find the", "find all", "find files"],
        "list": ["list all", "list the", "list files in"],
        "count": ["count the", "count files", "count files in"],
        "read": ["read the", "read file", "read the file"],
        "write": ["write a", "write a function", "write code"],
        "explain": ["explain this", "explain the", "explain how"],
        "help": ["help me", "help me with", "help me understand"],
        "please": ["please help", "please show", "please explain"],
        
        # German
        "wie": ["wie viele", "wie kann ich", "wie geht"],
        "was": ["was ist", "was sind", "was bedeutet"],
        "wo": ["wo ist", "wo sind", "wo finde ich"],
        "kannst": ["kannst du", "kannst du mir", "kannst du mir helfen"],
        "zeige": ["zeige mir", "zeige mir die", "zeige mir alle"],
        "erstelle": ["erstelle eine", "erstelle einen", "erstelle ein"],
        "finde": ["finde alle", "finde die", "finde dateien"],
        "lies": ["lies die", "lies datei", "lies die datei"],
        "erkläre": ["erkläre mir", "erkläre das", "erkläre wie"],
        "hilf": ["hilf mir", "hilf mir bei", "hilf mir mit"],
        "bitte": ["bitte hilf", "bitte zeige", "bitte erkläre"],
    }
    
    # Common completions for specific patterns
    PATTERN_COMPLETIONS = {
        r"files? in (\w+)$": "folder",
        r"in my (\w+)$": " folder",
        r"in meinem (\w+)$": " ordner",
        r"the (\w+) file$": "s",
        r"die (\w+) datei$": "en",
        r"how many$": " files",
        r"wie viele$": " dateien",
        r"create a$": " function",
        r"write a$": " script",
        r"show me$": " the",
        r"zeige mir$": " die",
    }
    
    def __init__(self, history_file: Path = None):
        """
        Initialize SmartAutoSuggest.
        
        Args:
            history_file: Path to store learned phrases (optional)
        """
        self.learned_phrases: Dict[str, Counter] = {}
        self.history_file = history_file or (Path.home() / ".vaf" / "autosuggest.json")
        self._save_lock = threading.RLock()
        self._save_timer = None
        self._load_learned()
    
    def _load_learned(self):
        """Load learned phrases from file (a plaintext file from before encryption still opens)."""
        try:
            from vaf.core import data_files
            data = data_files.read_json(self.history_file, default={}) or {}
            self.learned_phrases = {k: Counter(v) for k, v in data.items()}
        except Exception:
            self.learned_phrases = {}
    
    # The learned corpus grows without bound (measured on a real install:
    # 2.5 MB / 48k prefixes), and writing it costs ~140 ms. `learn()` used to
    # do that SYNCHRONOUSLY on every submitted line - in the classic prompt,
    # in the web server, and it would have frozen the full-screen app for that
    # long on every Enter. The write is debounced onto one daemon thread; the
    # in-memory corpus is updated immediately either way, so a suggestion is
    # never stale, only the file lags by a couple of seconds.
    SAVE_DEBOUNCE_SECONDS = 3.0

    def _save_learned(self):
        """Persist the learned corpus - debounced, never on the caller's thread."""
        with self._save_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            timer = threading.Timer(self.SAVE_DEBOUNCE_SECONDS, self._write_learned)
            timer.daemon = True
            self._save_timer = timer
            timer.start()

    def flush(self):
        """Write pending learning out NOW (call on a clean shutdown)."""
        with self._save_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
                self._save_timer = None
        self._write_learned()

    def _write_learned(self):
        try:
            with self._save_lock:
                data = {k: dict(v) for k, v in self.learned_phrases.items()}
                self._save_timer = None
            # Atomic (never a half-written corpus), encrypted, owner-only.
            from vaf.core import data_files
            data_files.write_json_atomic(self.history_file, data)
        except Exception:
            pass
    
    def learn(self, text: str):
        """Learn from user input to improve suggestions."""
        if not text or len(text) < 3:
            return
        
        # Tokenize and learn word sequences
        words = text.lower().split()
        
        for i in range(len(words) - 1):
            prefix = words[i]
            next_word = words[i + 1]
            
            if prefix not in self.learned_phrases:
                self.learned_phrases[prefix] = Counter()
            
            self.learned_phrases[prefix][next_word] += 1
        
        # Also learn 2-word prefixes
        for i in range(len(words) - 2):
            prefix = f"{words[i]} {words[i+1]}"
            next_word = words[i + 2]
            
            if prefix not in self.learned_phrases:
                self.learned_phrases[prefix] = Counter()
            
            self.learned_phrases[prefix][next_word] += 1
        
        self._save_learned()
    
    def forget(self, values) -> bool:
        """Drop every learned word that carries one of `values` (a credential the person handed
        over in the chat, vaf/core/forget_secrets.py) - as a prefix, inside a two-word prefix,
        or as a suggested next word. True when something was dropped."""
        parts = {w for v in values for w in str(v or "").lower().split() if len(w) >= 4}
        if not parts:
            return False

        def carries(text: str) -> bool:
            return any(part in text for part in parts)

        dropped = False
        with self._save_lock:
            for prefix in list(self.learned_phrases):
                if carries(prefix):
                    del self.learned_phrases[prefix]
                    dropped = True
                    continue
                nexts = self.learned_phrases[prefix]
                for word in [w for w in nexts if carries(w)]:
                    del nexts[word]
                    dropped = True
        if dropped:
            self.flush()
        return dropped

    def suggest(self, text: str) -> Optional[str]:
        """The suggestion for `text`, as a plain string.

        The lane-agnostic entry point: prompt_toolkit wants a `Suggestion`
        object, the full-screen app wants a `str` for its own ghost text.
        Without this, one of them would have to reach into a private method.
        """
        if not text or len(text) < 2:
            return None
        return self._get_best_suggestion(text)

    def get_suggestion(self, buffer: Buffer, document: Document) -> Optional[Suggestion]:
        """prompt_toolkit's shape, delegating to `suggest`."""
        found = self.suggest(document.text_before_cursor)
        return Suggestion(found) if found else None
    
    def _get_best_suggestion(self, text: str) -> Optional[str]:
        """Find the best suggestion for the given text."""
        text_lower = text.lower()
        
        # 1. Check pattern-based completions
        for pattern, completion in self.PATTERN_COMPLETIONS.items():
            if re.search(pattern, text_lower):
                return completion
        
        # 2. Get the last word(s)
        words = text_lower.split()
        if not words:
            return None
        
        last_word = words[-1]
        last_two_words = " ".join(words[-2:]) if len(words) >= 2 else None
        
        # 3. Check if we're in the middle of typing a word
        # (if text doesn't end with space, we're still typing)
        is_typing_word = not text.endswith(' ')
        
        if is_typing_word:
            # Complete the current word
            return self._complete_word(text_lower, last_word)
        else:
            # Suggest next word
            return self._suggest_next_word(last_word, last_two_words)
    
    def _complete_word(self, text: str, partial_word: str) -> Optional[str]:
        """Complete a partially typed word."""
        if len(partial_word) < 2:
            return None
        
        # Check common phrases that start with this word
        if partial_word in self.COMMON_PHRASES:
            phrases = self.COMMON_PHRASES[partial_word]
            if phrases:
                # Return the rest of the first phrase
                first_phrase = phrases[0]
                if first_phrase.startswith(partial_word):
                    return first_phrase[len(partial_word):]
        
        # Check if any phrase starts with this partial word
        for word, phrases in self.COMMON_PHRASES.items():
            if word.startswith(partial_word) and word != partial_word:
                # Complete to the full word + phrase
                return word[len(partial_word):]
        
        # Check learned phrases
        for prefix in self.learned_phrases:
            if prefix.startswith(partial_word) and prefix != partial_word:
                return prefix[len(partial_word):]
        
        return None
    
    def _suggest_next_word(self, last_word: str, last_two_words: str = None) -> Optional[str]:
        """Suggest the next word based on context."""
        
        # 1. Check learned phrases (2-word prefix first for better context)
        if last_two_words and last_two_words in self.learned_phrases:
            suggestions = self.learned_phrases[last_two_words]
            if suggestions:
                most_common = suggestions.most_common(1)[0][0]
                return most_common
        
        # 2. Check learned phrases (1-word prefix)
        if last_word in self.learned_phrases:
            suggestions = self.learned_phrases[last_word]
            if suggestions:
                most_common = suggestions.most_common(1)[0][0]
                return most_common
        
        # 3. Check common phrases
        if last_word in self.COMMON_PHRASES:
            phrases = self.COMMON_PHRASES[last_word]
            if phrases:
                first_phrase = phrases[0]
                # Return the part after the prefix word
                if first_phrase.startswith(last_word + " "):
                    return first_phrase[len(last_word) + 1:]
                elif first_phrase.startswith(last_word):
                    return first_phrase[len(last_word):]
        
        return None


class CombinedAutoSuggest(AutoSuggest):
    """
    Combines SmartAutoSuggest with history-based suggestions.
    Tries smart suggestions first, falls back to history.
    """
    
    def __init__(self, history_file: Path = None):
        self.smart = SmartAutoSuggest(history_file)
        self.history_suggestions: List[str] = []
    
    def add_to_history(self, text: str):
        """Add text to history and learn from it."""
        if text and len(text) > 3:
            self.history_suggestions.insert(0, text)
            # Keep last 100 entries
            self.history_suggestions = self.history_suggestions[:100]
            # Also learn
            self.smart.learn(text)
    
    def suggest(self, text: str) -> Optional[str]:
        """Learned corpus first, then this run's own history."""
        if not text:
            return None
        found = self.smart.suggest(text)
        if found:
            return found
        lowered = text.lower()
        for entry in self.history_suggestions:
            if entry.lower().startswith(lowered) and entry != text:
                return entry[len(text):]
        return None

    def flush(self):
        self.smart.flush()

    def get_suggestion(self, buffer: Buffer, document: Document) -> Optional[Suggestion]:
        """prompt_toolkit's shape, delegating to `suggest`."""
        found = self.suggest(document.text_before_cursor)
        return Suggestion(found) if found else None


# ═══════════════════════════════════════════════════════════════════════════════
# USAGE EXAMPLE
# ═══════════════════════════════════════════════════════════════════════════════

_per_account: Dict[str, SmartAutoSuggest] = {}
_per_account_lock = threading.Lock()


def autosuggest_file(user_scope_id: Optional[str]) -> Optional[Path]:
    """Where an account's learned corpus lives, or None for a caller with no account.

    The machine owner keeps `autosuggest.json` in the VAF directory, the file the terminal lanes
    (`vaf run`, the TUI) have always used, because they ARE the owner. Every other account gets
    its own file under `autosuggest/`."""
    scope = str(user_scope_id or "").strip()
    if not scope:
        return None
    from vaf.core.config import get_local_admin_scope_id
    from vaf.core.platform import Platform
    if scope == str(get_local_admin_scope_id()).strip():
        return Platform.vaf_dir() / "autosuggest.json"
    from vaf.core.path_jail import PathEscape, safe_entry_name
    try:
        return Platform.vaf_dir() / "autosuggest" / f"{safe_entry_name(scope)}.json"
    except PathEscape:
        return None


def autosuggest_for(user_scope_id: Optional[str]) -> Optional[SmartAutoSuggest]:
    """The account's own suggester: it learns from what this account types and suggests only
    that. None for a caller with no account, which then neither learns nor gets suggestions."""
    path = autosuggest_file(user_scope_id)
    if path is None:
        return None
    key = str(path)
    with _per_account_lock:
        found = _per_account.get(key)
        if found is None:
            found = _per_account[key] = SmartAutoSuggest(path)
        return found


def _forget_listener(env=None, user_scope_id=None, transcript_scrubbed=False, **_):
    """A credential forgotten in a chat leaves the account's word corpus too."""
    if transcript_scrubbed or not env:
        return
    from vaf.core.config import get_local_admin_scope_id
    suggester = autosuggest_for(user_scope_id or get_local_admin_scope_id())
    if suggester is not None:
        suggester.forget(env.values())


def _register_forget_listener() -> None:
    try:
        from vaf.core.forget_secrets import add_listener
        add_listener(_forget_listener)
    except Exception:
        pass


_register_forget_listener()


def create_autosuggest(history_file: Path = None) -> CombinedAutoSuggest:
    """
    Factory function to create the best autosuggest for VAF.
    
    Usage:
        from vaf.cli.autosuggest import create_autosuggest
        
        session = PromptSession(
            auto_suggest=create_autosuggest()
        )
    """
    return CombinedAutoSuggest(history_file)

