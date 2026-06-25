# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Smart AutoSuggest - Inline word completion like Google Search
Cross-Platform: Windows, macOS, Linux
"""
import os
import json
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
        self._load_learned()
    
    def _load_learned(self):
        """Load learned phrases from file."""
        try:
            if self.history_file.exists():
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.learned_phrases = {
                        k: Counter(v) for k, v in data.items()
                    }
        except Exception:
            self.learned_phrases = {}
    
    def _save_learned(self):
        """Save learned phrases to file."""
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.history_file, 'w', encoding='utf-8') as f:
                data = {k: dict(v) for k, v in self.learned_phrases.items()}
                json.dump(data, f, indent=2)
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
    
    def get_suggestion(self, buffer: Buffer, document: Document) -> Optional[Suggestion]:
        """
        Get inline suggestion for current input.
        
        Returns:
            Suggestion object with the completion text, or None
        """
        text = document.text_before_cursor
        
        if not text or len(text) < 2:
            return None
        
        # Get suggestion
        suggestion = self._get_best_suggestion(text)
        
        if suggestion:
            return Suggestion(suggestion)
        
        return None
    
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
    
    def get_suggestion(self, buffer: Buffer, document: Document) -> Optional[Suggestion]:
        """Get suggestion from smart suggester or history."""
        text = document.text_before_cursor
        
        if not text:
            return None
        
        # 1. Try smart suggestion first
        smart_suggestion = self.smart.get_suggestion(buffer, document)
        if smart_suggestion:
            return smart_suggestion
        
        # 2. Fall back to history matching
        text_lower = text.lower()
        for hist_entry in self.history_suggestions:
            if hist_entry.lower().startswith(text_lower) and hist_entry != text:
                # Return the rest of the history entry
                return Suggestion(hist_entry[len(text):])
        
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# USAGE EXAMPLE
# ═══════════════════════════════════════════════════════════════════════════════

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

