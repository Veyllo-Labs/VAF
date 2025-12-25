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
    files_created: List[str] = field(default_factory=list)
    files_read: List[str] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)
    errors_encountered: List[str] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    key_decisions: List[str] = field(default_factory=list)
    code_snippets: Dict[str, str] = field(default_factory=dict)  # filename -> snippet
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
        self.trigger_threshold = 0.70  # Trigger at 70% (reduced from 75% to prevent overflow)
        self.recent_memory_size = 10   # Keep last N messages raw
        
        # Context layers
        self.intent = IntentContext()
        self.state = StateContext()
        
        # Archive for restoration
        self.archive: List[ContextSnapshot] = []
        self.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # TOKEN ESTIMATION
    # ═══════════════════════════════════════════════════════════════════════════
    
    def estimate_tokens(self, messages: List[Dict]) -> int:
        """
        Estimate token count with safety margin.
        - Text: ~4 chars/token (conservative)
        - Code: ~3.5 chars/token
        - Add 10% safety margin for special tokens, formatting, etc.
        """
        total = 0
        for msg in messages:
            content = str(msg.get("content", ""))
            # Count role tokens (e.g., "user", "assistant", "system")
            role = msg.get("role", "")
            if role:
                total += len(role) / 4.0  # Estimate role tokens
            
            # Count content tokens
            if "```" in content:
                total += len(content) / 3.5
            else:
                total += len(content) / 4.0
        
        # Add 10% safety margin for special tokens, formatting, etc.
        total = int(total * 1.1)
        return total
    
    def get_usage_percent(self, history: List[Dict]) -> float:
        """Get context usage as percentage."""
        tokens = self.estimate_tokens(history)
        return tokens / self.max_tokens
    
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
        
        # File operations
        created = re.findall(r'(?:created|wrote|saved|erstellt|geschrieben)[:\s]+[`"\']?([^\s`"\'<>]+\.\w{1,10})', content, re.I)
        read = re.findall(r'(?:read|loaded|opened|gelesen|geöffnet)[:\s]+[`"\']?([^\s`"\'<>]+\.\w{1,10})', content, re.I)
        modified = re.findall(r'(?:modified|updated|changed|geändert|aktualisiert)[:\s]+[`"\']?([^\s`"\'<>]+\.\w{1,10})', content, re.I)
        
        for f in created[:3]:
            if f not in self.state.files_created:
                self.state.files_created.append(f)
        for f in read[:3]:
            if f not in self.state.files_read:
                self.state.files_read.append(f)
        for f in modified[:3]:
            if f not in self.state.files_modified:
                self.state.files_modified.append(f)
        
        # Limit lists
        self.state.files_created = self.state.files_created[-10:]
        self.state.files_read = self.state.files_read[-10:]
        self.state.files_modified = self.state.files_modified[-10:]
        
        # Errors
        if 'error' in content.lower() or 'failed' in content.lower() or 'fehler' in content.lower():
            for line in content.split('\n'):
                if any(e in line.lower() for e in ['error', 'failed', 'fehler', 'exception']):
                    error = line.strip()[:100]
                    if error and error not in self.state.errors_encountered:
                        self.state.errors_encountered.append(error)
                        break
        self.state.errors_encountered = self.state.errors_encountered[-5:]
        
        # Tools used (from tool role)
        if role == "tool":
            tool_name = message.get("name", "unknown")
            if tool_name not in self.state.tools_used:
                self.state.tools_used.append(tool_name)
        self.state.tools_used = self.state.tools_used[-10:]
        
        # Key decisions (from assistant without thinking)
        if role == "assistant" and "<think>" not in content:
            # Extract first meaningful statement
            sentences = re.split(r'[.!?]', content)
            for sent in sentences[:2]:
                if len(sent.strip()) > 30 and len(sent.strip()) < 150:
                    decision = sent.strip()
                    if decision not in self.state.key_decisions:
                        self.state.key_decisions.append(decision)
                    break
        self.state.key_decisions = self.state.key_decisions[-5:]
        
        # Code snippets (keep small snippets)
        code_blocks = re.findall(r'```(\w+)?\n(.+?)```', content, re.DOTALL)
        for lang, code in code_blocks[:2]:
            if len(code) < 500:  # Only small snippets
                # Generate a key based on content
                key = f"{lang or 'code'}_{hashlib.md5(code.encode()).hexdigest()[:6]}"
                self.state.code_snippets[key] = code[:300]
        # Keep max 5 snippets
        if len(self.state.code_snippets) > 5:
            keys = list(self.state.code_snippets.keys())
            for k in keys[:-5]:
                del self.state.code_snippets[k]
        
        self.state.last_updated = datetime.now().isoformat()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # CONTEXT COMPRESSION (Cursor-Style)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def should_compress(self, history: List[Dict]) -> bool:
        """Check if compression is needed."""
        usage = self.get_usage_percent(history)
        return usage >= self.trigger_threshold
    
    def compress(self, history: List[Dict]) -> List[Dict]:
        """
        Cursor-style compression:
        1. Archive full history for potential restoration
        2. Keep system prompt
        3. Keep recent messages raw
        4. Summarize old messages into Intent + State context
        """
        from vaf.cli.ui import UI
        
        if len(history) <= self.recent_memory_size + 2:
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
        
        # 3. Build compressed history
        system_prompt = history[0]  # Always keep
        recent_messages = history[-self.recent_memory_size:]  # Keep raw
        
        # 4. Build context summary
        context_summary = self._build_context_summary()
        
        # 5. Construct new history
        new_history = [system_prompt]
        
        if context_summary:
            new_history.append({
                "role": "system",
                "content": context_summary
            })
        
        new_history.extend(recent_messages)
        
        new_tokens = self.estimate_tokens(new_history)
        UI.event("Context", f"Compressed: {len(history)} → {len(new_history)} msgs, {current_tokens} → {new_tokens} tokens", style="success")
        UI.event("Context", f"Full history archived. Use /restore to recover.", style="dim")
        
        return new_history
    
    def _build_context_summary(self) -> str:
        """Build a structured context summary."""
        parts = []
        
        # Intent Context
        if self.intent.primary_goal or self.intent.sub_goals:
            parts.append("## 🎯 Intent Context")
            if self.intent.primary_goal:
                parts.append(f"**Primary Goal:** {self.intent.primary_goal}")
            if self.intent.sub_goals:
                parts.append("**Sub-tasks:** " + ", ".join(self.intent.sub_goals[-3:]))
            if self.intent.constraints:
                parts.append("**Constraints:** " + ", ".join(self.intent.constraints[-3:]))
            if self.intent.keywords:
                parts.append("**Keywords:** " + ", ".join(self.intent.keywords[-5:]))
        
        # State Context
        state_items = []
        if self.state.files_created:
            state_items.append(f"Files created: {', '.join(self.state.files_created[-5:])}")
        if self.state.files_modified:
            state_items.append(f"Files modified: {', '.join(self.state.files_modified[-5:])}")
        if self.state.errors_encountered:
            state_items.append(f"Errors: {'; '.join(self.state.errors_encountered[-2:])}")
        if self.state.tools_used:
            state_items.append(f"Tools used: {', '.join(self.state.tools_used[-5:])}")
        if self.state.key_decisions:
            state_items.append("Decisions:\n  • " + "\n  • ".join(self.state.key_decisions[-3:]))
        
        if state_items:
            parts.append("\n## 📁 State Context")
            parts.extend(state_items)
        
        if not parts:
            return ""
        
        return "[COMPRESSED CONTEXT - Full history archived]\n\n" + "\n".join(parts)
    
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
        archive_file = self.ARCHIVE_DIR / f"context_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        try:
            with open(archive_file, 'w', encoding='utf-8') as f:
                json.dump({
                    "timestamp": snapshot.timestamp,
                    "history": snapshot.history,
                    "intent": self.intent.__dict__,
                    "state": {k: v for k, v in self.state.__dict__.items() if k != 'code_snippets'},
                    "token_count": snapshot.token_count
                }, f, indent=2, ensure_ascii=False)
        except Exception:
            pass  # Silent fail for disk archive
    
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

