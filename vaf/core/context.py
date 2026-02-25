"""
VAF Context Manager - Cursor-Style Intelligent Context Management

Features:
- Intent Context: Tracks what the user wants to achieve
- State Context: Tracks project state (files, errors, decisions)
- Full History Archive: Complete history stored for restoration
- Smart Summarization: Lossy compression that preserves critical info
- Restoration: Can restore full context when needed

Inspired by Cursor's context management system.
"""

import re
import json
import hashlib
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional


@dataclass
class IntentContext:
    """Tracks what the user is trying to achieve."""
    primary_goal: str = ""              # Main objective
    sub_goals: List[str] = field(default_factory=list)  # Sub-tasks
    constraints: List[str] = field(default_factory=list)  # Limitations/requirements
    keywords: List[str] = field(default_factory=list)  # Key terms
    last_updated: str = ""


@dataclass
class StateContext:
    """Tracks project/conversation state."""
    files_created: List[tuple[str, int]] = field(default_factory=list)
    files_read: List[tuple[str, int]] = field(default_factory=list)
    files_modified: List[tuple[str, int]] = field(default_factory=list)
    errors_encountered: List[str] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    key_decisions: List[str] = field(default_factory=list)
    code_snippets: Dict[str, str] = field(default_factory=dict)  # filename -> snippet
    narrative_summary: str = ""  # LLM-generated summary of past conversation
    last_updated: str = ""


@dataclass
class ContextSnapshot:
    """Complete snapshot for restoration."""
    timestamp: str
    history: List[Dict]
    intent: IntentContext
    state: StateContext
    token_count: int


class ContextManager:
    """
    Cursor-Style Context Manager for VAF.
    
    Maintains:
    - Intent Context: What the user wants
    - State Context: Current project state
    - Full Archive: Complete history for restoration
    """
    
    ARCHIVE_DIR = Path.home() / ".vaf" / "context_archive"
    
    def __init__(self, max_tokens: int = 8192):
        self.max_tokens = max_tokens
        
        # DYNAMIC LIMITS: React to small context sizes (VRAM efficiency)
        # Very small (e.g. 4k–8k): keep more raw so 1–2 turns visible after tool use
        # For 12k: recent 10; For 16k: recent 12; For >32k: recent 20 (default)
        if max_tokens <= 8192:
            self.trigger_threshold = 0.70
            self.recent_memory_size = 12
        elif max_tokens <= 12000:
            self.trigger_threshold = 0.70
            self.recent_memory_size = 10
        elif max_tokens <= 20000:
            self.trigger_threshold = 0.75
            self.recent_memory_size = 12
        elif max_tokens <= 64000:
            self.trigger_threshold = 0.85
            self.recent_memory_size = 50
        elif max_tokens <= 128000:
            self.trigger_threshold = 0.85
            self.recent_memory_size = 100
        else:
            # Ultra-large windows (e.g. Gemini 1M+, Claude 200k)
            self.trigger_threshold = 0.90
            self.recent_memory_size = 200
        
        # Context layers
        self.intent = IntentContext()
        self.state = StateContext()
        
        # Archive for restoration
        self.archive: List[ContextSnapshot] = []
        self.created_archives: List[Path] = []  # Track created files for cleanup
        self.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # TOKEN ESTIMATION
    # ═══════════════════════════════════════════════════════════════════════════
    
    def estimate_tokens(self, messages: List[Dict], base_tokens: int = 0) -> int:
        """
        Estimate token count with safety margin.
        - Text: ~3.0 chars/token (safer for multilingual/complex text)
        - Code: ~2.5 chars/token (code uses more tokens due to symbols)
        - base_tokens: Tokens already consumed (tools, system prompt overhead)
        - Add 10% safety margin for special tokens, formatting, etc.
        """
        total = base_tokens
        
        # Conservative estimation for small contexts
        is_small = self.max_tokens <= 16384
        text_ratio = 2.5 if is_small else 3.0
        code_ratio = 2.0 if is_small else 2.5
        
        for msg in messages:
            content = str(msg.get("content", ""))
            # Count role tokens (e.g., "user", "assistant", "system")
            role = msg.get("role", "")
            if role:
                total += len(role) / text_ratio  # Estimate role tokens
            
            # Count content tokens
            if "```" in content:
                total += len(content) / code_ratio
            else:
                total += len(content) / text_ratio
        
        # Add 10% safety margin for special tokens, formatting, etc.
        total = int(total * 1.1)
        return total
    
    def get_usage_percent(self, history: List[Dict]) -> float:
        """Get context usage as percentage."""
        tokens = self.estimate_tokens(history)
        return tokens / self.max_tokens

    def decay_state(self):
        """Decay the TTL of state items."""
        
        def decay_list(items: List[tuple[str, int]]) -> List[tuple[str, int]]:
            new_items = []
            for item, ttl in items:
                if ttl > 1:
                    new_items.append((item, ttl - 1))
            return new_items

        self.state.files_created = decay_list(self.state.files_created)
        self.state.files_read = decay_list(self.state.files_read)
        self.state.files_modified = decay_list(self.state.files_modified)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # INTENT TRACKING
    # ═══════════════════════════════════════════════════════════════════════════
    
    def update_intent(self, user_message: str):
        """Extract and update intent from user message."""
        msg_lower = user_message.lower()
        
        # Detect primary goals
        goal_patterns = [
            (r"(?:ich möchte|i want to|please|bitte)\s+(.+?)(?:\.|$)", "goal"),
            (r"(?:erstelle|create|build|make)\s+(.+?)(?:\.|$)", "create"),
            (r"(?:fixe|fix|repair|repariere)\s+(.+?)(?:\.|$)", "fix"),
            (r"(?:erkläre|explain|was ist|what is)\s+(.+?)(?:\.|$)", "explain"),
            (r"(?:suche|search|find|finde)\s+(.+?)(?:\.|$)", "search"),
        ]
        
        for pattern, intent_type in goal_patterns:
            match = re.search(pattern, msg_lower)
            if match:
                goal = match.group(1).strip()[:100]
                if intent_type == "goal":
                    self.intent.primary_goal = goal
                else:
                    if goal not in self.intent.sub_goals:
                        self.intent.sub_goals.append(f"{intent_type}: {goal}")
                        # Keep max 5 sub-goals
                        self.intent.sub_goals = self.intent.sub_goals[-5:]
        
        # Extract keywords (nouns, technical terms)
        keywords = re.findall(r'\b([A-Z][a-z]+|[a-z]+_[a-z]+|\w+\.\w+)\b', user_message)
        for kw in keywords[:5]:
            if kw not in self.intent.keywords and len(kw) > 3:
                self.intent.keywords.append(kw)
        self.intent.keywords = self.intent.keywords[-10:]  # Keep max 10
        
        # Extract constraints
        constraint_patterns = [
            r"(?:ohne|without|nicht|don't|no)\s+(.+?)(?:\.|,|$)",
            r"(?:muss|must|should|sollte)\s+(.+?)(?:\.|,|$)",
        ]
        for pattern in constraint_patterns:
            matches = re.findall(pattern, msg_lower)
            for match in matches[:2]:
                constraint = match.strip()[:50]
                if constraint and constraint not in self.intent.constraints:
                    self.intent.constraints.append(constraint)
        self.intent.constraints = self.intent.constraints[-5:]
        
        self.intent.last_updated = datetime.now().isoformat()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # STATE TRACKING
    # ═══════════════════════════════════════════════════════════════════════════
    
    def update_state(self, message: Dict):
        """Extract and update state from any message."""
        content = str(message.get("content", ""))
        role = message.get("role", "")
        
        # 0. KEY FACT EXTRACTION (Heuristic)
        # Look for lines that look like status updates or important data points
        # common in DHL tracking, research reports, etc.
        fact_patterns = [
            r'(?:status|zustand|lage|lieferung|delivery|sendung)[:\s]+(.+)',
            r'(?:ergebnis|result|outcome)[:\s]+(.+)',
            r'(?:datum|date|deadline)[:\s]+(.+)',
        ]
        for pattern in fact_patterns:
            matches = re.findall(pattern, content, re.I)
            for m in matches[:2]:
                fact = m.strip()[:100]
                if fact and len(fact) > 10:
                    if fact not in self.state.key_decisions:
                        self.state.key_decisions.append(f"Fact: {fact}")
        
        # Dynamic TTL based on context size
        is_small = self.max_tokens <= 16384
        default_ttl = 3 if is_small else 5
        
        def update_file_list(file_list: List[tuple[str, int]], new_files: List[str]):
            for f in new_files:
                found = False
                for i, (path, ttl) in enumerate(file_list):
                    if path == f:
                        file_list[i] = (path, default_ttl)
                        found = True
                        break
                if not found:
                    file_list.append((f, default_ttl))
            return file_list[-15:]

        # 1. Proactive Fact Extraction (The "Glue")
        # File operations
        created = re.findall(r'(?:created|wrote|saved|erstellt|geschrieben)[:\s]+[`"\']?([^\s`"\'<>]+\.\w{1,10})', content, re.I)
        read = re.findall(r'(?:read|loaded|opened|gelesen|geöffnet)[:\s]+[`"\']?([^\s`"\'<>]+\.\w{1,10})', content, re.I)
        modified = re.findall(r'(?:modified|updated|changed|geändert|aktualisiert)[:\s]+[`"\']?([^\s`"\'<>]+\.\w{1,10})', content, re.I)
        
        self.state.files_created = update_file_list(self.state.files_created, created[:10])
        self.state.files_read = update_file_list(self.state.files_read, read[:10])
        self.state.files_modified = update_file_list(self.state.files_modified, modified[:10])

        # Errors
        if 'error' in content.lower() or 'failed' in content.lower() or 'fehler' in content.lower():
            for line in content.split('\n'):
                if any(e in line.lower() for e in ['error', 'failed', 'fehler', 'exception']):
                    error = line.strip()[:150]
                    if error and error not in self.state.errors_encountered:
                        self.state.errors_encountered.append(error)
                        break
        self.state.errors_encountered = self.state.errors_encountered[-8:]
        
        # Tools used (from tool role)
        if role == "tool":
            tool_name = message.get("name", "unknown")
            if tool_name not in self.state.tools_used:
                self.state.tools_used.append(tool_name)
        self.state.tools_used = self.state.tools_used[-15:]
        
        # Key decisions (from assistant without thinking)
        if role == "assistant" and "<think>" not in content:
            # Extract first meaningful statement
            sentences = re.split(r'[.!?]', content)
            for sent in sentences[:3]:
                clean_sent = sent.strip()
                if 30 < len(clean_sent) < 200:
                    if clean_sent not in self.state.key_decisions:
                        self.state.key_decisions.append(clean_sent)
                    break
        self.state.key_decisions = self.state.key_decisions[-10:]
        
        # Code snippets (keep small snippets)
        code_blocks = re.findall(r'```(\w+)?\n(.+?)```', content, re.DOTALL)
        for lang, code in code_blocks[:2]:
            if len(code) < 800:  # Increased slightly
                # Generate a key based on content
                key = f"{lang or 'code'}_{hashlib.md5(code.encode()).hexdigest()[:6]}"
                self.state.code_snippets[key] = code[:500]
        # Keep max 8 snippets
        if len(self.state.code_snippets) > 8:
            keys = list(self.state.code_snippets.keys())
            for k in keys[:-8]:
                del self.state.code_snippets[k]
        
        self.state.last_updated = datetime.now().isoformat()

    def process_tool_output(self, tool_name: str, content: str) -> str:
        """
        Seamlessly compress a tool output BEFORE it enters history.
        Extracts facts into StateContext and returns a pruned version of the content.
        """
        content_str = str(content)
        lines = content_str.split('\n')
        line_count = len(lines)
        char_count = len(content_str)

        # 1. Update State immediately
        self.update_state({"role": "tool", "name": tool_name, "content": content_str})

        # Dynamic limits based on context size
        is_small_context = self.max_tokens < 6000
        max_raw_chars = 1500 if is_small_context else 3000
        max_raw_lines = 30 if is_small_context else 60
        
        # 2. If content is small, leave it raw
        if char_count < max_raw_chars and line_count < max_raw_lines:
            return content_str

        # 3. Aggressive Pruning for large outputs
        pruned_msg = f"[SEAMLESS COMPRESSION: Tool '{tool_name}' output pruned ({char_count} chars, {line_count} lines)]\n"
        
        if tool_name in ["read_file", "list_files", "web_search", "webfetch", "github_get_file", "github_list_repos", "mail_inbox", "whatsapp_inbox", "list_email_accounts", "telegram_inbox"]:
            # Dynamic pruning window - preserved even more content (40 lines head, 30 lines tail)
            head_lines = 20 if is_small_context else 40
            tail_lines = 15 if is_small_context else 30
            
            head = "\n".join(lines[:head_lines])
            tail = "\n".join(lines[-tail_lines:])
            hidden_count = max(0, line_count - (head_lines + tail_lines))
            return f"{pruned_msg}\n{head}\n\n[... {hidden_count} lines hidden ...]\n\n{tail}\n\nNOTE: The facts from this output are stored in the State Context."
        
        # Default pruning - preserved more content (1500 chars)
        trunc_limit = 800 if is_small_context else 1500
        return f"{pruned_msg}\n{content_str[:trunc_limit]}...\n\n[... truncated for context stability ...]"

    # ═══════════════════════════════════════════════════════════════════════════
    # CONTEXT COMPRESSION (Cursor-Style)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def should_compress(self, history: List[Dict]) -> bool:
        """Check if compression is needed."""
        usage = self.get_usage_percent(history)
        return usage >= self.trigger_threshold
    
    def compress(self, history: List[Dict], preserve_tools: List[str] = None) -> List[Dict]:
        """
        Cursor-style compression with tool result preservation:
        1. Archive full history for potential restoration
        2. Keep system prompt
        3. Preserve critical tool results (write_file, read_file, set_todos)
        4. Keep recent messages raw
        5. Summarize old messages into Intent + State context

        Args:
            history: Full history to compress
            preserve_tools: List of tool names whose results should be preserved
        """
        from vaf.cli.ui import UI

        if preserve_tools is None:
            preserve_tools = [
                "set_todos", "write_file", "read_file",
                "github_list_repos", "github_get_file", "github_list_issues", "github_list_pulls",
                "web_search",
            ]

        # REMOVED: history length check. If tokens are full, we MUST compress.
        # But we still need at least system + 1 message to make sense.
        if len(history) < 3:
            return history

        current_tokens = self.estimate_tokens(history)
        UI.event("Context", f"Compressing ({current_tokens}/{self.max_tokens} tokens, {self.get_usage_percent(history):.0%})...", style="info")

        # 1. Archive full history for restoration
        self._archive_history(history)

        # 2. Update Intent and State from ALL messages
        for msg in history:
            if msg.get("role") == "user":
                self.update_intent(msg.get("content", ""))
            self.update_state(msg)

        # 3. Extract critical tool results from middle section
        critical_tools = []
        middle_section = history[1:-self.recent_memory_size] if len(history) > self.recent_memory_size + 1 else []

        for msg in middle_section:
            if msg.get("role") == "tool" and msg.get("name") in preserve_tools:
                # Truncate content but keep structure
                content = msg.get("content", "")
                truncated = content[:300] + "..." if len(content) > 300 else content
                critical_tools.append({
                    "role": "tool",
                    "name": msg.get("name"),
                    "content": truncated,
                    "tool_call_id": msg.get("tool_call_id", "")
                })

        # 4. Build compressed history
        system_prompt = history[0]  # Always keep
        recent_messages = history[-self.recent_memory_size:]  # Keep raw

        # 5. Build context summary
        context_summary = self._build_context_summary()

        # 6. Construct new history
        new_history = [system_prompt]

        if context_summary:
            new_history.append({
                "role": "system",
                "content": context_summary
            })

        # Add critical tool results (max 5)
        new_history.extend(critical_tools[-5:])

        new_history.extend(recent_messages)

        new_tokens = self.estimate_tokens(new_history)
        UI.event("Context", f"Compressed: {len(history)} → {len(new_history)} msgs, {current_tokens} → {new_tokens} tokens", style="success")
        if critical_tools:
            UI.event("Context", f"Preserved {len(critical_tools[-5:])} critical tool results", style="dim")
        UI.event("Context", f"Full history archived. Use /restore to recover.", style="dim")

        return new_history
    
    def _build_context_summary(self) -> str:
        """Build a high-density structured context summary (The 'Glue')."""
        parts = []
        is_small = self.max_tokens <= 16384
        
        # 1. NARRATIVE (High Priority)
        if self.state.narrative_summary:
            header = "## RECENT SUMMARY" if is_small else "### 📝 CONVERSATION SUMMARY"
            parts.append(f"{header}\n{self.state.narrative_summary}")

        # 2. PROJECT STATE
        state_parts = []
        file_limit = 8 if is_small else 15
        if self.state.files_created:
            state_parts.append(f"**Files Created:** {', '.join([p for p, t in self.state.files_created[-file_limit:]])}")
        if self.state.files_modified:
            state_parts.append(f"**Files Modified:** {', '.join([p for p, t in self.state.files_modified[-file_limit:]])}")
        if self.state.files_read:
            state_parts.append(f"**Files Read:** {', '.join([p for p, t in self.state.files_read[-file_limit:]])}")
        
        if state_parts:
            header = "## PROJECT STATE" if is_small else "### 📁 PROJECT & FILE STATE"
            parts.append(f"{header}\n" + "\n".join(state_parts))

        # 3. ERRORS (Critical)
        if self.state.errors_encountered:
            err_limit = 5 if is_small else 10
            header = "## ERRORS" if is_small else "### ⚠️ ERRORS ENCOUNTERED"
            parts.append(f"{header}\n• " + "\n• ".join(self.state.errors_encountered[-err_limit:]))

        # 4. DECISIONS & PROGRESS
        if self.state.key_decisions:
            dec_limit = 5 if is_small else 10
            header = "## DECISIONS" if is_small else "### 🎯 KEY DECISIONS & PROGRESS"
            parts.append(f"{header}\n• " + "\n• ".join(self.state.key_decisions[-dec_limit:]))
        
        # 5. INTENT (Goal)
        if self.intent.primary_goal:
            header = "## PRIMARY GOAL" if is_small else "### 🎯 PRIMARY GOAL"
            parts.append(f"{header}\n{self.intent.primary_goal}")

        # 6. TOOLS USED (small context only — reduces "forgot I was connected" confusion)
        if is_small and self.state.tools_used:
            tools_str = ", ".join(self.state.tools_used[-10:])
            parts.append(f"## TOOLS USED THIS SESSION\n{tools_str}")
        
        if not parts:
            return ""
        
        main_header = "## CONTEXT GLUE" if is_small else "### COMPRESSED CONTEXT STATE (STABLE PROGRESS GLUE)"
        return (
            f"{main_header}\n\n"
            + "\n\n".join(parts)
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # ARCHIVE & RESTORATION
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _archive_history(self, history: List[Dict]):
        """Archive full history for potential restoration."""
        snapshot = ContextSnapshot(
            timestamp=datetime.now().isoformat(),
            history=history.copy(),
            intent=IntentContext(**{k: v for k, v in self.intent.__dict__.items()}),
            state=StateContext(**{k: v for k, v in self.state.__dict__.items() if k != 'code_snippets'}),
            token_count=self.estimate_tokens(history)
        )
        
        # Keep in memory (max 3 snapshots)
        self.archive.append(snapshot)
        if len(self.archive) > 3:
            self.archive.pop(0)
        
        # Also save to disk
        archive_file = self.ARCHIVE_DIR / f"context_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{hashlib.md5(str(datetime.now()).encode()).hexdigest()[:6]}.json"
        try:
            with open(archive_file, 'w', encoding='utf-8') as f:
                json.dump({
                    "timestamp": snapshot.timestamp,
                    "history": snapshot.history,
                    "intent": self.intent.__dict__,
                    "state": {k: v for k, v in self.state.__dict__.items() if k != 'code_snippets'},
                    "token_count": snapshot.token_count
                }, f, indent=2, ensure_ascii=False)
            self.created_archives.append(archive_file)
        except Exception:
            pass  # Silent fail for disk archive
    
    def cleanup(self):
        """Cleanup temporary archive files created during this session."""
        for file_path in self.created_archives:
            try:
                if file_path.exists():
                    file_path.unlink()
            except Exception:
                pass
        self.created_archives = []

    def restore_latest(self) -> Optional[List[Dict]]:
        """Restore the most recent archived history."""
        if self.archive:
            snapshot = self.archive[-1]
            return snapshot.history
        
        # Try disk archive
        try:
            archives = sorted(self.ARCHIVE_DIR.glob("context_*.json"), reverse=True)
            if archives:
                with open(archives[0], 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get("history", [])
        except Exception:
            pass
        
        return None
    
    def list_archives(self) -> List[Dict[str, Any]]:
        """List available archives for restoration."""
        archives = []
        
        # Memory archives
        for i, snap in enumerate(self.archive):
            archives.append({
                "index": i,
                "source": "memory",
                "timestamp": snap.timestamp,
                "messages": len(snap.history),
                "tokens": snap.token_count
            })
        
        # Disk archives
        try:
            for f in sorted(self.ARCHIVE_DIR.glob("context_*.json"), reverse=True)[:5]:
                with open(f, 'r', encoding='utf-8') as fp:
                    data = json.load(fp)
                    archives.append({
                        "file": f.name,
                        "source": "disk",
                        "timestamp": data.get("timestamp", "unknown"),
                        "messages": len(data.get("history", [])),
                        "tokens": data.get("token_count", 0)
                    })
        except Exception:
            pass
        
        return archives
    
    def restore_from_file(self, filename: str) -> Optional[List[Dict]]:
        """Restore history from a specific archive file."""
        try:
            filepath = self.ARCHIVE_DIR / filename
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get("history", [])
        except Exception:
            return None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # STATUS & INFO
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_status(self, history: List[Dict]) -> Dict[str, Any]:
        """Get current context status."""
        tokens = self.estimate_tokens(history)
        return {
            "tokens": tokens,
            "max_tokens": self.max_tokens,
            "usage_percent": tokens / self.max_tokens,
            "messages": len(history),
            "intent_goal": self.intent.primary_goal,
            "files_touched": len(self.state.files_created) + len(self.state.files_modified),
            "errors": len(self.state.errors_encountered),
            "archives_available": len(self.archive) + len(list(self.ARCHIVE_DIR.glob("context_*.json")))
        }
