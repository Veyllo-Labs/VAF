# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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
from vaf.core.config import Config


# Marker prefix for the per-turn tool/reasoning summary that replaces squashed
# intermediate steps. Used to recognize (and preserve) these messages on reload.
TURN_CONTEXT_PREFIX = "[Context:"


# Prefixes that mean a tool result is a FAILURE, anchored to the start so
# "No errors found" is not a false positive. Tools surface failures as
# "<qualifier> Error: ..." (Tool Error / Security Error), NOT a bare
# "Error:", so a leading "error"/"failed" check alone marked those green.
# THE single source of truth for "is this a failed tool result", shared by
# the agent's retry guard (agent.py) and this per-turn summarizer so the two
# copies can never drift again (a drift left this summarizer labeling a failed
# write_file as OK, which a weak local model then reported to the user as
# success - live incident). NOTE: the tool_end observability `ok` flag in
# agent.py intentionally does NOT use this helper - it is a narrower,
# dispatch-level check (it must not mark a "Failed..."-style semantic output
# as not-ok).
#
# A second incident (fresh adversarial review of that fix itself)
# found this list, while internally consistent, covered only a handful of the
# failure-string SHAPES tools actually return - a repo-wide sweep of every
# vaf/tools/*.py file found ~30 more currently-shipping families this missed
# entirely (host_bash's "[BLOCKED]"/"[HOST]", the shared filesystem path-safety
# gate's "Access denied:"/"Invalid path", the whole messaging-tool "X
# unavailable: {e}" family, github_tools.py's error shapes, mcp_client.py's,
# and more - see the tiers below). Grouped by the sweep's tiers for
# maintainability; add new tools' failure shapes here rather than inventing a
# second detector.
_ERROR_PREFIXES = (
    "❌",
    "error",
    "failed",
    "tool error",
    "security error",
    "exception",
    "[error]",
    # State-changing tool gated until a plan is set: it did NOT run, so it
    # must not read as a green success.
    "[plan required]",
    # Bracket/tag gate markers, sibling family to [PLAN REQUIRED] - the tool
    # did NOT run, or its result was blocked/cancelled before completion.
    "[blocked]",
    "[host]",
    "[security]",
    "[cancelled]",
    "[confirm required]",
    "[awaiting user]",
    "[tool blocked]",
    "[librarian_error",
    "[warn]",
    # Shared filesystem path-safety gate (vaf/tools/filesystem.py is_safe_path
    # and every tool that wraps it: read/write/edit/tree/find_files, WhatsApp/
    # Telegram/mail attachment resolution, vision's file lookup, ...).
    "access denied:",
    "invalid path",
    # Other common refusal/precondition-failure openers.
    "cannot ",
    "refused:",
    "blocked:",
    "missing required parameters",
    "tests failed",
    "test run timed out",
    # Send tools' internal-content firewall: the message was NOT sent.
    "message was blocked",
    # Cloud-storage auth precondition (starts every affected result).
    "authentication failed",
    # mcp_client.py's two distinct error idioms.
    "mcp error:",
    "http error:",
    # filesystem.py's move/copy wrapper re-labels an inner path-safety
    # failure under its own prefix; edit_file's outcome marker.
    "source error:",
    "dest error:",
    "edit failed:",
    # Connection/account preconditions common across integrations
    # (calendar, GitHub, cloud storage, WhatsApp contacts).
    "not connected",
    "no calendar account connected",
    "no github account",
    "no whatsapp contact found",
    "could not schedule reminder",
)


# Anchored-regex belt: failure families whose MESSAGE starts the string but
# with a variable lead ("<Noun> unavailable:", "<Verb phrase> failed:", ...),
# so a fixed startswith prefix cannot express them. Anchored at the string
# start with a bounded, single-line, colon-free lead - NEVER a free substring
# scan: content-carrying results (read_file, web fetches, chat/mail reads)
# embed arbitrary text, and an unanchored " failed:"/"unavailable:" flagged
# ordinary successful reads whose CONTENT mentioned failures (adversarial
# review of the first version of this expansion: 10/10 realistic
# content-carrying success strings misclassified).
_ERROR_HEAD_RES = tuple(re.compile(p) for p in (
    # "Telegram unavailable:", "Messenger delivery unavailable:", ...
    r"^[^\n:]{0,40}unavailable:",
    # "Vision is unavailable (no API backend...)"
    r"^[^\n:]{0,40}is unavailable \(",
    # "Screenshot failed:", "CAPTCHA analysis failed:", "Git commit failed:"
    r"^[^\n:]{0,30} failed:",
))


def tool_result_is_error(content: str) -> bool:
    """True when a tool result string represents a failure (prefix-anchored)."""
    if not isinstance(content, str):
        return False
    head = content[:50].lower().strip()
    if head.startswith(_ERROR_PREFIXES):
        return True
    low = content.lower()
    # Anchored-regex belt: variable-lead failure openers (see _ERROR_HEAD_RES).
    for _hr in _ERROR_HEAD_RES:
        if _hr.match(low):
            return True
    # Head-bounded belts: markers a short banner or one explanation line can
    # push off the string start (so neither the prefix anchor nor the
    # start-regexes see them), still specific literal markers, and bounded to
    # the first 200 chars so a long content-carrying result (read_file, web
    # fetch) that merely EMBEDS such text deeper down stays green.
    head200 = low[:200]
    if (
        # python_exec.py: "<warning banner>\n\n[ERROR] (exit=1):..."
        "[error] (exit=" in head200
        # sandbox_test_runner.py: a docker/tar explanation can precede it.
        or "cannot run tests:" in head200
    ):
        return True
    # Short-result belts: permanently-unimplemented tool stubs whose ENTIRE
    # result is one short sentence ("Editing events is not yet supported for
    # CalDAV calendars."). Gated on the result being short - a document or
    # file that CONTAINS such a sentence is content, not an outcome.
    if len(low) <= 240 and ("is not yet supported" in low or "not implemented yet" in low):
        return True
    # Pre-existing whole-content belts (unchanged; kept narrow deliberately -
    # broad substring scans over content-carrying results are how the first
    # version of this expansion misclassified successful reads as failures).
    return (
        "❌" in content
        or "error executing tool" in low
        or "traceback (most recent call last)" in low
        or ("failed" in low and ("tool" in low or "execution" in low))
    )


# Backward-compatible private alias (was the only name before the extraction).
_tool_result_is_error = tool_result_is_error


def summarize_tool_turn(messages: List[Dict], snippet_limit: int = 200) -> Optional[str]:
    """Build a compact, readable summary of a turn's squashed intermediate steps.

    Instead of only listing tool names, include each tool's outcome (OK/FAILED)
    and a short single-line snippet of its result/error, so the agent stays aware
    of WHAT happened (and which errors occurred) on later turns. Returns the
    summary string (always starting with TURN_CONTEXT_PREFIX) or None if there is
    nothing worth summarizing.
    """
    tool_outcomes = []  # (name, status, snippet)
    thoughts_count = 0
    for m in messages or []:
        role = m.get("role", "")
        content = str(m.get("content", "") or "")
        if role == "tool":
            name = m.get("name") or "UnknownTool"
            status = "FAILED" if _tool_result_is_error(content) else "OK"
            snippet = " ".join(content.split())  # collapse whitespace/newlines
            if len(snippet) > snippet_limit:
                snippet = snippet[:snippet_limit].rstrip() + "…"
            tool_outcomes.append((name, status, snippet))
        elif role == "assistant":
            if "<think>" in content or "</think>" in content:
                thoughts_count += 1

    if tool_outcomes:
        lines = [f"{TURN_CONTEXT_PREFIX} tools used this turn]"]
        for name, status, snippet in tool_outcomes:
            lines.append(f"- {name} → {status}: {snippet}" if snippet else f"- {name} → {status}")
        if thoughts_count:
            lines.append(f"(reasoning: {thoughts_count} steps squashed)")
        return "\n".join(lines)
    if thoughts_count:
        return f"{TURN_CONTEXT_PREFIX} Reasoning: {thoughts_count} steps]"
    return None


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
    
    def compress(
        self,
        history: List[Dict],
        preserve_tools: List[str] = None,
        working_memory: Optional[Dict[str, Any]] = None,
    ) -> List[Dict]:
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
            working_memory: Optional working memory snapshot for resume-block enrichment
        """
        from vaf.cli.ui import UI

        if preserve_tools is None:
            preserve_tools = [
                "set_todos", "write_file", "read_file",
                "github_list_repos", "github_get_file", "github_get_file_structure", "github_list_issues", "github_list_pulls",
                "web_search",
                # Sub-agent spawns: keep the "[!] TASK DELEGATED" anchor and its
                # tool_call/tool pairing alive across compression, so a long light chat
                # while the sub-agent runs cannot erase the evidence that work is pending.
                "coding_agent", "research_agent", "document_agent", "librarian_agent",
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
        # Exclude history[0] (the system turn) from the recent slice: when the history is shorter than
        # recent_memory_size, history[-N:] still includes index 0, and prepending system_prompt below
        # would then count the (large) system prompt TWICE -- which made "compression" grow the context
        # by ~the system size (observed 31985 -> 51235). Slice from history[1:] so it can never overlap.
        recent_messages = history[1:][-self.recent_memory_size:]  # Keep raw

        # 5. Build context summary
        context_summary = self._build_context_summary()
        resume_block = self.build_resume_block(history, working_memory=working_memory)
        summary_parts = [part for part in (context_summary, resume_block) if part]
        combined_summary = "\n\n".join(summary_parts)

        # 6. Construct new history
        new_history = [system_prompt]

        if combined_summary:
            new_history.append({
                "role": "system",
                "content": combined_summary
            })

        # Add critical tool results (max 5)
        new_history.extend(critical_tools[-5:])

        new_history.extend(recent_messages)

        new_tokens = self.estimate_tokens(new_history)

        # Safety net for the real failure mode: the summary backfired so the result is BOTH larger than
        # the input AND still over the limit (observed: 30725 -> 43754 tokens over a 32768 limit, which
        # immediately tripped CRITICAL OVERFLOW). Only then drop the summary + critical-tool block and
        # keep just the system turn + recent messages (always smaller; the full history is archived for
        # /restore). A small context that merely grows a little (e.g. 88 -> 150, far under the limit)
        # KEEPS the resume block — it never overflows, and /restore + NEXT_ACTION depend on that block.
        if new_tokens >= current_tokens and new_tokens > self.max_tokens:
            new_history = [system_prompt] + recent_messages
            new_tokens = self.estimate_tokens(new_history)
            UI.event("Context", f"Summary would have grown context — dropped it; kept system + {len(recent_messages)} recent msgs ({current_tokens} → {new_tokens} tokens)", style="warning")
        else:
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

    @staticmethod
    def _resume_text(value: Any) -> str:
        """Normalize values to one-line deterministic resume text."""
        text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
        text = re.sub(r"\s+", " ", text)
        return text

    @staticmethod
    def _truncate_resume_text(text: str, limit: int = 180) -> str:
        """Keep resume fields compact and predictable."""
        text = ContextManager._resume_text(text)
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    @staticmethod
    def _flatten_working_memory_entries(entries: Any) -> List[str]:
        """Extract plain text from working-memory notes/plan/tasks."""
        texts: List[str] = []
        if not isinstance(entries, list):
            return texts
        for entry in entries:
            if isinstance(entry, dict):
                if entry.get("text") is not None:
                    texts.append(str(entry.get("text")))
                elif entry.get("content") is not None:
                    texts.append(str(entry.get("content")))
            elif entry is not None:
                texts.append(str(entry))
        return [ContextManager._resume_text(text) for text in texts if ContextManager._resume_text(text)]

    @staticmethod
    def _extract_file_references(text: str) -> List[str]:
        """Collect likely file references from free-form text."""
        pattern = r"(?:[A-Za-z]:)?[A-Za-z0-9_.\\/\-]+\.[A-Za-z0-9]{1,10}"
        refs: List[str] = []
        for match in re.findall(pattern, text):
            cleaned = match.strip("`\"'()[]{}.,;:")
            if cleaned and "://" not in cleaned and not cleaned.lower().startswith(("http.", "https.")):
                refs.append(cleaned)
        return refs

    @staticmethod
    def _dedupe_preserve_order(values: List[str], limit: int) -> List[str]:
        """Deduplicate values while preserving original order."""
        seen = set()
        result: List[str] = []
        for value in values:
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def _latest_user_message(history: List[Dict]) -> str:
        """Return the latest user message for deterministic fallback logic."""
        for msg in reversed(history):
            if msg.get("role") == "user":
                content = ContextManager._resume_text(msg.get("content", ""))
                if content:
                    return content
        return ""

    def build_resume_block(
        self,
        history: List[Dict],
        working_memory: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Build a deterministic operational resume block for compression/checkpoint."""
        if not bool(Config.get("resume_compaction_enabled", True)):
            return ""

        wm = working_memory if isinstance(working_memory, dict) else {}
        wm_notes = self._flatten_working_memory_entries(wm.get("notes", []))
        wm_plan = self._flatten_working_memory_entries(wm.get("plan", []))

        pending_tasks: List[str] = []
        for task in wm.get("tasks", []) if isinstance(wm.get("tasks"), list) else []:
            if not isinstance(task, dict):
                continue
            if str(task.get("status") or "pending").lower() != "pending":
                continue
            text = self._resume_text(task.get("text", ""))
            if text:
                pending_tasks.append(text)

        latest_user_message = self._latest_user_message(history)

        current_focus = pending_tasks[0] if pending_tasks else ""
        if not current_focus and wm_plan:
            current_focus = wm_plan[-1]
        if not current_focus and self.state.narrative_summary:
            current_focus = self.state.narrative_summary
        if not current_focus:
            current_focus = latest_user_message

        goal = self._resume_text(self.intent.primary_goal)
        if goal and current_focus:
            current_work = f"Working toward {goal}. Current focus: {current_focus}"
        elif goal:
            current_work = f"Working toward {goal}."
        elif current_focus:
            current_work = current_focus
        else:
            current_work = "Continuing the previous task."
        current_work = self._truncate_resume_text(current_work, limit=220)

        pending_items = pending_tasks[:]
        if not pending_items and self.intent.sub_goals:
            pending_items.extend(self.intent.sub_goals[-3:])
        pending_items = self._dedupe_preserve_order(
            [self._truncate_resume_text(item, limit=80) for item in pending_items if item],
            limit=5,
        )
        pending_work = ", ".join(pending_items) if pending_items else "none"

        key_file_candidates: List[str] = []
        for text in wm_notes + wm_plan + pending_tasks:
            key_file_candidates.extend(self._extract_file_references(text))
        key_file_candidates.extend([path for path, _ttl in self.state.files_created[-6:]])
        key_file_candidates.extend([path for path, _ttl in self.state.files_modified[-6:]])
        key_file_candidates.extend([path for path, _ttl in self.state.files_read[-6:]])
        key_files_list = self._dedupe_preserve_order(
            [self._truncate_resume_text(ref, limit=90) for ref in key_file_candidates if ref],
            limit=8,
        )
        key_files = ", ".join(key_files_list) if key_files_list else "none"

        tools_used_list = self._dedupe_preserve_order(
            [self._resume_text(tool) for tool in self.state.tools_used if self._resume_text(tool)],
            limit=10,
        )
        tools_used = ", ".join(tools_used_list) if tools_used_list else "none"

        decision_candidates: List[str] = []
        for text in wm_notes[-5:]:
            if any(token in text.lower() for token in ("decision", "decided", "choose", "chosen", "use ", "using ")):
                decision_candidates.append(text)
        decision_candidates.extend(self.state.key_decisions[-3:])
        key_decisions_list = self._dedupe_preserve_order(
            [self._truncate_resume_text(item, limit=90) for item in decision_candidates if item],
            limit=4,
        )
        key_decisions = ", ".join(key_decisions_list) if key_decisions_list else "none"

        if pending_items:
            next_action = f"Continue with: {pending_items[0]}"
        elif goal:
            next_action = f"Continue work toward {goal}."
        elif latest_user_message:
            next_action = f"Continue the previous request: {latest_user_message}"
        else:
            next_action = "continue previous task"
        next_action = self._truncate_resume_text(next_action, limit=140)

        return (
            "=== RESUME CONTEXT ===\n"
            f"CURRENT_WORK: {current_work}\n"
            f"PENDING_WORK: {pending_work}\n"
            f"KEY_FILES: {key_files}\n"
            f"TOOLS_USED: {tools_used}\n"
            f"KEY_DECISIONS: {key_decisions}\n"
            f"NEXT_ACTION: {next_action}\n"
            "=== END RESUME ==="
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
