# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Research Agent - Topic-by-topic web research with bounded context.

This tool is designed to avoid "exceed_context_size_error" by:
- Splitting a research task into sections (topics)
- Running web_search per section
- Calling the model per section with only that section's context
- Assembling a final HTML report
"""

from __future__ import annotations

import json
import re
import threading
import time
import concurrent.futures
import unicodedata
import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence
from pathlib import Path

import requests
import os
import sys

from vaf.cli.ui import UI, AnimatedHeader
from vaf.cli.themes import ThemeManager
from vaf.core.config import Config
from vaf.core.platform import Platform
from vaf.core.trust_map import filter_results_by_quality, find_optimal_threshold, rate_url_quality
from vaf.tools.base import BaseTool
from vaf.tools.search import WebSearchTool

# Rich UI primitives (optional but expected in VAF)
from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.text import Text


@dataclass(frozen=True)
class SectionSpec:
    title: str
    query_suffix: str


class _StaticHeader:
    """Non-animated header (safe for environments where frequent Live refresh spams output)."""

    def __init__(self, title: str, left_agt: str, right_agt: str):
        self.title = title
        self.left_agt = left_agt
        self.right_agt = right_agt
        # Get current theme colors
        theme_name = Config.get("theme", "vaf")
        theme = ThemeManager.get_theme(theme_name)
        self.border_color = theme.get("border_active", theme.get("primary", "#00d4ff"))
        self.text_color = theme.get("primary", "#00d4ff")

    def __rich__(self) -> Panel:
        arrow_str = Text(f"<=====>", style=f"bold {self.text_color}")
        art_grid = Text()
        art_grid.append(" \n")
        art_grid.append("   ( OO)     ", style="white")
        art_grid.append_text(arrow_str)
        art_grid.append("     (OO )\n", style="white")
        row3 = f" {self.left_agt:<13}           {self.right_agt:<12}"
        art_grid.append(row3 + "\n", style="white")
        return Panel(
            Align.center(art_grid),
            title=f"[bold {self.text_color}]{self.title}[/bold {self.text_color}]",
            border_style=f"bold {self.border_color}",
            padding=(0, 2),
        )


def _extract_urls(web_search_output: str) -> List[str]:
    # Matches lines like: "- Source: https://..."
    urls = re.findall(r"(?im)^\s*-\s*Source:\s*(https?://\S+)\s*$", web_search_output or "")
    # De-dupe while preserving order
    out: List[str] = []
    seen = set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _truncate(s: str, max_chars: int) -> str:
    s = s or ""
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "\n\n[...truncated...]"

def _visible_text_len(html_fragment: str) -> int:
    """
    Rough heuristic for "how much content" is in an HTML fragment:
    strip tags + collapse whitespace.
    """
    txt = re.sub(r"<[^>]+>", " ", html_fragment or "")
    txt = re.sub(r"\s+", " ", txt).strip()
    return len(txt)

def _visible_word_count(html_fragment: str) -> int:
    """
    Approximate word count for an HTML fragment: strip tags, collapse whitespace, count word-like tokens.
    """
    txt = re.sub(r"<[^>]+>", " ", html_fragment or "")
    txt = re.sub(r"\s+", " ", txt).strip()
    if not txt:
        return 0
    return len(re.findall(r"\b\w+\b", txt, flags=re.UNICODE))

def _detect_language(text: str) -> str:
    """
    Very small heuristic: return 'de' for obviously German input, else 'en'.
    """
    t = (text or "").lower()
    if any(w in t for w in ["über", "künst", "dass", "und", "für", "recherche", "analyse", "was ist", "wie "]):
        return "de"
    # German umlauts/ß
    if any(ch in t for ch in ["ä", "ö", "ü", "ß"]):
        return "de"
    return "en"

def _strip_answer_artifacts(s: str) -> str:
    # Remove standalone "Answer" lines which some models prepend.
    s = re.sub(r"(?im)^\s*answer\s*$", "", s or "")
    return s.strip()

def _strip_untrusted_links(html: str, allowed: Sequence[str]) -> str:
    """
    Remove/harden links not in allowed sources (prevents example.com hallucinations).
    Keeps link text but removes href for untrusted URLs.
    """
    allowed_set = set(allowed or [])

    def repl(m: re.Match) -> str:
        href = m.group(1)
        label = m.group(2)
        if href in allowed_set:
            return m.group(0)
        # Drop the link, keep label
        return label

    # <a href="URL">label</a>
    html = re.sub(r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', repl, html, flags=re.IGNORECASE | re.DOTALL)
    return html


_SOURCE_HEADER_RE = re.compile(
    r"^\s*(?:die\s+quellen|quellen(?:angaben)?|sources?(?:\s+used)?)\b[^<>]{0,120}$",
    re.IGNORECASE,
)
_BARE_URL_RE = re.compile(r"^\s*https?://\S+\s*$")


def _strip_section_source_blocks(html: str) -> str:
    """Remove per-section source lists the model still writes despite instructions.

    Reports cite with numbered [n] markers against ONE global source list; live
    runs produced messy per-section blocks in changing formats: a "Sources:" /
    "Die Quellen ... sind:" paragraph, <ul> lists of bare URLs, or raw URL lines.
    """
    if not html:
        return html
    # <p>Sources:</p> / <p><strong>Quellen:</strong></p> style headers
    def _drop_header_p(m: re.Match) -> str:
        inner = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        return "" if _SOURCE_HEADER_RE.match(inner) else m.group(0)
    html = re.sub(r"<p[^>]*>([\s\S]*?)</p>", _drop_header_p, html, flags=re.IGNORECASE)
    # Lists where half or more of the items are bare URLs
    def _drop_url_list(m: re.Match) -> str:
        items = re.findall(r"<li[^>]*>([\s\S]*?)</li>", m.group(0), flags=re.IGNORECASE)
        if not items:
            return m.group(0)
        bare = sum(1 for it in items if _BARE_URL_RE.match(re.sub(r"<[^>]+>", "", it).strip()))
        return "" if bare * 2 >= len(items) else m.group(0)
    html = re.sub(r"<(ul|ol)[^>]*>[\s\S]*?</\1>", _drop_url_list, html, flags=re.IGNORECASE)
    # Standalone bare-URL text lines (loose text between blocks)
    lines = [ln for ln in html.split("\n") if not _BARE_URL_RE.match(ln)]
    return "\n".join(lines).strip()


_BLOCK_LEVEL_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li",
                     "table", "thead", "tbody", "tr", "td", "th", "div", "blockquote", "pre"}


def _wrap_loose_section_text(html: str) -> str:
    """Wrap top-level bare text in <p> so it inherits paragraph typography.

    Models emit fragments like "<h2>Title</h2>\\n\\nPlain text..." — the loose
    text renders with the viewer's default font instead of the report style.
    Inline tags (a/em/strong) stay inside the wrapped paragraph.
    """
    if not html:
        return html
    tokens = re.split(r"(<[^>]+>)", html)
    out: List[str] = []
    buf: List[str] = []
    depth = 0

    def _flush() -> None:
        chunk = "".join(buf)
        buf.clear()
        if chunk.strip():
            out.append(f"<p>{chunk.strip()}</p>")
        elif chunk:
            out.append(chunk)

    for tok in tokens:
        if tok.startswith("<"):
            m = re.match(r"</?\s*([a-zA-Z0-9]+)", tok)
            name = (m.group(1).lower() if m else "")
            if name in _BLOCK_LEVEL_TAGS:
                _flush()
                depth = max(0, depth - 1) if tok.startswith("</") else depth + 1
                out.append(tok)
            elif depth == 0:
                buf.append(tok)
            else:
                out.append(tok)
        elif depth == 0:
            buf.append(tok)
        else:
            out.append(tok)
    _flush()
    return "".join(out)


def _clamp_runaway_heading(html: str, title: str) -> str:
    """Keep section headings heading-sized.

    Two live failure modes: an UNCLOSED <h2> swallows the whole section (every
    paragraph renders big and bold), or the model packs body text into the
    heading. Rebuilds to a clean <h2>title</h2> and demotes the rest to <p>.
    """
    if not html:
        return html
    m = re.search(r"<h2[^>]*>", html, flags=re.IGNORECASE)
    if not m:
        return html
    after = html[m.end():]
    close = re.search(r"</h2>", after, flags=re.IGNORECASE)
    if not close:
        rest = after.strip()
        return f"<h2>{title}</h2>" + (f"<p>{rest}</p>" if rest and not rest.startswith("<") else rest)
    inner_text = re.sub(r"<[^>]+>", "", after[:close.start()]).strip()
    if len(inner_text) > 120:
        return (
            html[:m.start()]
            + f"<h2>{title}</h2><p>{inner_text}</p>"
            + after[close.end():]
        )
    return html


class ResearchTUI:
    """
    Mini-IDE style TUI for the Research Agent (modeled after CoderTUI).
    Shows:
    - Animated collaboration header
    - Status line: elapsed, loop counter, stage
    - Section progress + word target progress
    - Scrolling log (events)
    """

    LOG_LINES = 14

    def __init__(self, console, topic: str, *, animate: bool = True):
        self.console = console
        self.topic = topic
        self.start_time = time.time()
        self.loop_count = 0
        self.stage = "Initializing..."
        self.section_title = ""
        self.section_idx = 0
        self.section_total = 0
        self.word_count = 0
        self.word_target = 0
        self.word_ok = 0
        self._lock = threading.RLock()
        
        # Get current theme colors for borders and text
        theme_name = Config.get("theme", "vaf")
        theme = ThemeManager.get_theme(theme_name)
        self.border_color = theme.get("border_active", theme.get("primary", "#00d4ff"))
        self.text_color = theme.get("primary", "#00d4ff")
        
        self._header = (
            AnimatedHeader("Collaboration Mode Active", "Main Agt", "Researcher")
            if animate
            else _StaticHeader("Collaboration Mode Active", "Main Agt", "Researcher")
        )
        self._logs: List[str] = []
        self._on_change = None  # Live updater callback (optional)
        self.last_update_time = time.time()  # Track last actual update

    def _touch(self):
        """Update the last modified timestamp."""
        self.last_update_time = time.time()

    def set_on_change(self, cb):
        """Register a callback invoked after state changes (used for event-driven Live refresh)."""
        self._on_change = cb

    def log(self, msg: str):
        cb = None
        with self._lock:
            self._touch()
            ts = time.strftime("%H:%M:%S")
            self._logs.append(f"{ts} {msg}")
            if len(self._logs) > self.LOG_LINES:
                self._logs = self._logs[-self.LOG_LINES:]
            cb = self._on_change
        if cb:
            cb()

    def set_stage(self, stage: str):
        cb = None
        with self._lock:
            self._touch()
            self.stage = stage or ""
            cb = self._on_change
        if cb:
            cb()

    def increment_loop(self):
        cb = None
        with self._lock:
            self._touch()
            self.loop_count += 1
            cb = self._on_change
        if cb:
            cb()

    def set_section(self, idx: int, total: int, title: str):
        cb = None
        with self._lock:
            self._touch()
            self.section_idx = int(idx)
            self.section_total = int(total)
            self.section_title = title or ""
            cb = self._on_change
        if cb:
            cb()

    def set_word_progress(self, current_words: int, target_words: int, ok_words: int):
        cb = None
        with self._lock:
            self._touch()
            self.word_count = max(0, int(current_words))
            self.word_target = max(0, int(target_words))
            self.word_ok = max(0, int(ok_words))
            cb = self._on_change
        if cb:
            cb()

    def _progress_bar(self, current: int, total: int, width: int = 10) -> str:
        if total <= 0:
            return "○" * width
        pct = max(0.0, min(1.0, current / total))
        filled = int(pct * width)
        return ("●" * filled) + ("○" * (width - filled))

    def render(self) -> Group:
        with self._lock:
            # Show time of last actual update for static look, or ticking time for animated
            if self.last_update_time:
                update_time_str = time.strftime("%H:%M:%S", time.localtime(self.last_update_time))
            else:
                update_time_str = time.strftime("%H:%M:%S")

            # Status line
            status = Text()
            status.append(f"Last Update: {update_time_str}", style="dim")
            status.append("  │  ", style="dim")
            status.append(f"Loop: {self.loop_count}", style="dim")
            status.append("  │  ", style="dim")
            status.append(self.stage or "Working...", style="white")

            # Section line
            sec = Text()
            if self.section_total:
                sec.append(f"Section {self.section_idx}/{self.section_total}: ", style="cyan")
                sec.append(self.section_title or "-", style="white")
            else:
                sec.append("Section: -", style="dim")

            # Word progress line
            wc = self.word_count
            tgt = self.word_target
            ok = self.word_ok
            bar_total = tgt if tgt > 0 else 0
            bar = self._progress_bar(min(wc, bar_total), bar_total, width=28)
            word_line = Text()
            if tgt > 0:
                word_line.append("Words: ", style="dim")
                # Color: red < ok, yellow < target, green >= target
                if wc < ok:
                    color = "red"
                elif wc < tgt:
                    color = "yellow"
                else:
                    color = "green"
                word_line.append(bar, style=color)
                # Show minimum target clearly: "0 / min 500 (ok≥400)"
                word_line.append(f"  {wc} / min {tgt} (ok≥{ok})", style="dim")
            else:
                word_line.append("Words: (n/a)", style="dim")

            logs = "\n".join(self._logs) if self._logs else "…"
            log_panel = Panel(
                logs, 
                title=f"[bold {self.text_color}]Research Log[/bold {self.text_color}]", 
                border_style=self.border_color, 
                padding=(0, 1)
            )

            return Group(
                self._header,
                status,
                sec,
                word_line,
                log_panel,
            )


class ResearchAgentTool(BaseTool):
    """
    Sub-agent style tool that produces a research report (HTML by default)
    without using huge single-shot prompts.
    """

    name = "research_agent"
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "Comprehensive multi-section research with detailed analysis. "
        "USE ONLY FOR: Deep research (10+ sources), multi-perspective analysis, detailed reports. "
        "DON'T USE FOR: Simple lookups (weather, news, facts) - use web_search instead! "
        "For multiple simple questions, just call web_search multiple times. "
        "Example: ✅ 'Research AI market trends' ❌ 'Weather + News' (use web_search twice!)"
    )

    parameters = {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Main topic to research"},
            "format": {"type": "string", "description": "Output format: html | markdown | html_fragment", "default": "html"},
            "max_results": {"type": "integer", "description": "web_search results per section (1-10)", "default": 5},
            "deep": {"type": "boolean", "description": "Enable deep previews in web_search (slower)", "default": False},
            "language": {"type": "string", "description": "Force output language: de | en (optional)"},
            # Preferred: word-based thresholds (requested)
            "min_words_target": {"type": "integer", "description": "Target words per section (default: 500)", "default": 500},
            "min_words_ok_ratio": {"type": "number", "description": "Acceptable ratio of target (default: 0.8)", "default": 0.8},
            # Backward compat (deprecated): char-based thresholds
            "min_chars_empty": {"type": "integer", "description": "[Deprecated] If section text < this, treat as empty and retry", "default": 150},
            "min_chars_ok": {"type": "integer", "description": "[Deprecated] If section text < this, treat as too short and expand once", "default": 500},
            "sections": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional explicit section titles. If omitted, uses a sensible default set of 10 sections.",
            },
            # Optional: append-style expansion (used by repair_report and internal expand path)
            # Only meaningful when generating a single html_fragment section.
            "existing_section_html": {"type": "string", "description": "Existing HTML fragment for the section (optional)."},
        },
        "required": ["topic"],
    }

    def _generate_title(self, raw_topic: str) -> str:
        """Extract a clean, professional title from a raw prompt."""
        from vaf.cli.ui import UI

        # Short, instruction-free topics ARE the title already — skip the LLM
        # call entirely (small models leak reasoning or echo the few-shot
        # example, which once produced reports titled "SpaceX Rocket Launch
        # Overview" for a Brandenburg Gate topic).
        _topic_clean = (raw_topic or "").strip().strip('"').strip("'")
        _instruction_words = (
            "erstelle", "schreibe", "recherchiere", "mach", "bitte", "suche",
            "tell me", "write", "create", "research", "give me", "find", "please",
        )
        if 0 < len(_topic_clean) <= 70 and not any(w in _topic_clean.lower() for w in _instruction_words):
            return _topic_clean

        try:
            from vaf.core.config import Config
            import requests
            import time

            UI.event("Research", "Generating clean title...", style="dim")
            
            prompt = (
                f"Task: Extract a clean title from the request.\n"
                f"Example Request: \"tell me about space x rocket launch\"\n"
                f"Title: SpaceX Rocket Launch Overview\n\n"
                f"Request: \"{raw_topic}\"\n"
                f"Title:"
            )
            
            # Unified LLM query using BaseTool method
            content = self.query_llm(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=550,
                temperature=0.7
            )
            
            if content:
                # Reasoning models may return their chain of thought instead of
                # the title (query_llm falls back to reasoning_content): strip
                # think blocks and take the text after the LAST "Title:" marker.
                cleaned = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
                if 'title:' in cleaned.lower():
                    cleaned = re.split(r'(?i)\btitle\s*:', cleaned)[-1].strip()
                title = cleaned.strip('"').strip("'").split("\n")[0].strip()

                # Remove common prefixes if model is chatty
                for prefix in ["Title:", "Report Title:", "Here is a title:", "The title is:", "Answer:"]:
                    if title.lower().startswith(prefix.lower()):
                        title = title[len(prefix):].strip()

                # Reject leaked reasoning ("Thinking Process: ...", "Okay, the user wants...")
                _reasoning_markers = (
                    "thinking process", "okay", "let me", "let's", "first,", "hmm",
                    "the user", "we need", "i need", "alright",
                )
                _looks_like_reasoning = any(title.lower().startswith(m) for m in _reasoning_markers)
                # Reject the few-shot example echoed back as the "answer"
                if "spacex rocket launch" in title.lower():
                    _looks_like_reasoning = True

                if 1 < len(title) <= 120 and not _looks_like_reasoning:
                    UI.event("Research", f"Title: {title}", style="dim")
                    return title
                else:
                    UI.event("Debug", f"Title gen: rejected ('{title[:60]}')", style="warning")
            else:
                UI.event("Debug", f"Title gen: Failed (no response)", style="warning")
        except Exception as e:
            UI.event("Debug", f"Title generation critical error: {e}", style="error")
            pass
            
        return raw_topic

    def run(self, **kwargs) -> str:
        raw_topic = (kwargs.get("topic") or "").strip()
        out_format = (kwargs.get("format") or "html").strip().lower()
        max_results = int(kwargs.get("max_results", 5) or 5)
        deep = bool(kwargs.get("deep", False))
        forced_lang = (kwargs.get("language") or "").strip().lower()
        min_words_target = int(kwargs.get("min_words_target", 500) or 500)
        min_words_ok_ratio = float(kwargs.get("min_words_ok_ratio", 0.8) or 0.8)
        # Backward compat (deprecated)
        min_chars_empty = int(kwargs.get("min_chars_empty", 150) or 150)
        min_chars_ok = int(kwargs.get("min_chars_ok", 500) or 500)
        section_titles: Optional[Sequence[str]] = kwargs.get("sections")
        existing_section_html = str(kwargs.get("existing_section_html") or "").strip()

        # Debug logging - get logger early
        debug_logger = None
        try:
            from vaf.core.subagent_debug import get_subagent_logger_from_env
            debug_logger = get_subagent_logger_from_env()
            if debug_logger:
                debug_logger.event("research_agent_start", topic=raw_topic[:200], format=out_format,
                                   max_results=max_results, deep=deep, forced_lang=forced_lang)
        except Exception:
            pass

        if not raw_topic:
            if debug_logger:
                debug_logger.event("research_agent_error", error="No topic provided")
            return "Error: No topic provided."
            
        # Clean up topic title (remove "write a report about", "hello", etc.)
        # Only do this if we are going to RUN the research locally (not dispatching)
        in_subagent_terminal = os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "").strip() in ("1", "true", "yes")
        from vaf.core.config import Config
        will_dispatch = not in_subagent_terminal and Config.get("sub_agents_in_separate_terminals", False)
        
        # If running locally (or inside subagent), clean the topic for better planning/titles.
        # If dispatching, keep raw topic to pass full context to the sub-agent.
        topic = raw_topic
        if not will_dispatch: 
             topic = self._generate_title(raw_topic)
        
        if out_format not in ("html", "markdown", "html_fragment"):
            return "Error: format must be 'html', 'markdown', or 'html_fragment'."
        
        # ═══════════════════════════════════════════════════════════════════════
        # CHECK IF RUNNING IN SEPARATE TERMINAL MODE
        # ═══════════════════════════════════════════════════════════════════════
        from vaf.core.config import Config
        from vaf.core.platform import Platform
        from vaf.cli.ui import UI
        
        # If already in sub-agent terminal, run normally
        if in_subagent_terminal:
            # Continue with normal execution below
            pass
        elif Config.get("sub_agents_in_separate_terminals", False):
            # Start in new terminal window with IPC tracking
            import shlex
            from vaf.core.subagent_ipc import get_ipc, get_current_session_id
            
            # Create task in IPC system
            ipc = get_ipc()
            # Use RAW topic for description so we see the full request
            task_id = ipc.create_task("research_agent", task_description=raw_topic)
            
            # Pass session/task context to the sub-agent via the CHILD env only (not the parent's
            # process-global os.environ), so concurrent workers don't clobber each other's session.
            session_id = get_current_session_id()
            _sub_env = {"VAF_TASK_ID": task_id, "VAF_AGENT_TYPE": "research_agent"}
            if session_id:
                _sub_env["VAF_SESSION_ID"] = session_id

            # Pass provider configuration to sub-agent
            use_separate_provider = Config.get("subagent_use_separate_provider", False)
            if use_separate_provider:
                subagent_provider = Config.get("subagent_provider", "inherit")
                if subagent_provider != "inherit":
                    _sub_env["VAF_PROVIDER"] = subagent_provider
            
            # Pass RAW topic to sub-agent (it will clean it up)
            cmd_parts = [sys.executable, '-m', 'vaf.main', 'subagent', 'run', 'research_agent', '--topic', raw_topic, '--task-id', task_id]
            if out_format:
                cmd_parts.extend(['--format', out_format])
            if max_results:
                cmd_parts.extend(['--max-results', str(max_results)])
            
            if Platform.is_windows():
                # Windows: properly escape for cmd /k
                escaped_parts = []
                for part in cmd_parts:
                    if ' ' in part or '"' in part:
                        escaped = part.replace('"', '\\"')
                        escaped_parts.append(f'"{escaped}"')
                    else:
                        escaped_parts.append(part)
                cmd = ' '.join(escaped_parts)
                title = f"VAF Research Agent [{task_id}]"
            else:
                # Unix: use shell quoting
                cmd = ' '.join(shlex.quote(str(part)) for part in cmd_parts)
                title = f"VAF Research Agent [{task_id}]"
            
            if Platform.open_new_terminal(cmd, title=title, extra_env=_sub_env):
                # Mark task as running
                ipc.mark_task_running(task_id)
                
                UI.event("Sub-Agent", f"Research Agent started in new terminal [Task: {task_id}]", style="bold cyan")
                # Return special marker for main agent to recognize async task
                return f"[SUBAGENT_ASYNC:{task_id}:research_agent] Sub-Agent running in separate terminal. Topic: {topic[:80]}..."
            else:
                # Fallback: run normally if terminal opening fails
                UI.warning("Failed to open new terminal, running in current window")
                ipc.cancel_task(task_id)

        max_results = max(1, min(max_results, 10))
        lang = forced_lang if forced_lang in ("de", "en") else _detect_language(topic)

        # Word-based targets (preferred)
        min_words_target = max(150, min(min_words_target, 1200))
        min_words_ok_ratio = max(0.5, min(min_words_ok_ratio, 0.95))
        min_words_ok = max(50, int(min_words_target * min_words_ok_ratio))

        # Char thresholds kept only for backward-compat fallback
        min_chars_empty = max(50, min(min_chars_empty, 2000))
        min_chars_ok = max(min_chars_empty + 1, min(min_chars_ok, 5000))

        # Dynamic Planning: Generate sections based on topic analysis
        if section_titles:
            # Build a user-defined section list (query suffix = title)
            specs = [SectionSpec(str(t).strip(), str(t).strip()) for t in section_titles if str(t).strip()]
        else:
            # Generate a custom plan via LLM
            specs = self._generate_plan(topic, lang)

        # Research TUI (Coder-like): shows loop, section progress, word progress, and logs.
        try:
            from rich.live import Live
        except Exception:
            Live = None  # type: ignore[assignment]

        in_workflow = os.environ.get("VAF_IN_WORKFLOW", "").strip().lower() in ("1", "true", "yes")
        noninteractive = os.environ.get("VAF_NONINTERACTIVE", "").strip().lower() in ("1", "true", "yes")
        is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
        # Live only makes sense on a real TTY. If we try Live on a non-TTY, Rich prints each frame as new lines (spam).
        # CRITICAL: If format is "html_fragment" (called by repair_report for single sections), disable Live
        # to prevent multiple Live instances from spamming the console when repair_report fixes multiple sections.
        # Note: We do NOT disable Live for in_workflow (like coding_agent), because Live works fine in workflows.
        is_fragment_mode = (out_format == "html_fragment")
        # Strict check for Live support to avoid "spam" in dumb terminals (Colab, CI, etc.)
        use_live = (Live is not None) and (not noninteractive) and is_tty and (not is_fragment_mode) and UI.console.is_terminal and not UI.console.is_jupyter
        
        # Disable animation by default to prevent flickering/spam in many terminals
        animate_tui = False 

        tui = ResearchTUI(UI.console, topic, animate=animate_tui)

        # Pass debug_logger to inner function via closure
        _debug_lg = debug_logger

        if _debug_lg:
            _debug_lg.event("research_agent_config",
                            use_live=use_live, is_tty=is_tty, noninteractive=noninteractive,
                            in_workflow=in_workflow, is_fragment_mode=is_fragment_mode,
                            console_is_terminal=UI.console.is_terminal,
                            webui_active=os.environ.get("VAF_WEBUI_ACTIVE", ""),
                            in_subagent_terminal=os.environ.get("VAF_IN_SUBAGENT_TERMINAL", ""))

        _WEB_SEARCH_TIMEOUT = int(Config.get("research_web_search_timeout_seconds", 60) or 60)
        _SECTION_TIMEOUT = int(Config.get("research_section_timeout_seconds", 180) or 180)
        _OVERALL_TIMEOUT = int(Config.get("research_overall_timeout_seconds", 900) or 900)

        _is_piped_subprocess = (
            os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "").strip() in ("1", "true", "yes")
            and not sys.stdout.isatty()
        )

        def _emit_progress(message: str, style: str = "dim", presence: str = "online") -> None:
            """Emit progress in a way that is visible in WebUI sub-agent stream."""
            if use_live:
                return
            session_id = os.environ.get("VAF_SESSION_ID", "").strip()
            task_id = os.environ.get("VAF_TASK_ID", "").strip()

            if session_id:
                try:
                    tls_on = Config.get("local_network_tls_enabled", False)
                    port = 8005 if tls_on else 8001
                    payload = {
                        "type": "subagent_update",
                        "sessionId": session_id,
                        "taskId": task_id or None,
                        "agentName": "Research Agent",
                        "status": message,
                        "presence": presence,
                    }
                    if presence == "idle" and task_id:
                        payload["steps"] = [{
                            "id": task_id,
                            "title": "Research Agent",
                            "description": "Completed",
                            "status": "completed",
                            "actions": [],
                        }]
                    requests.post(
                        f"http://127.0.0.1:{port}/api/subagent/stream",
                        json=payload,
                        timeout=0.4,
                    )
                except Exception:
                    pass
            if _is_piped_subprocess:
                try:
                    print(f"[Research] {message}", flush=True)
                except (BrokenPipeError, OSError):
                    pass
                return
            try:
                if noninteractive:
                    print(f"[Research] {message}", flush=True)
                else:
                    UI.event("Research", message, style=style)
            except (BrokenPipeError, OSError):
                pass
            except Exception:
                pass

        # ── WebUI research window state (paper viewer / outline / sources) ──
        # Mirrors the coder's coder_state pattern: one event type, hash+time
        # throttled, silently inactive without a session id.
        _rs_state: Dict[str, Any] = {
            "topic": "",
            "stage": "Initializing",
            "sections": [],            # [{title, status, words, targetWords}]
            "sectionsHtml_ref": None,  # bound to rendered_sections inside the loop
            "sources": [],             # [{url, title, domain}]
            "wordsTarget": 0,
            "loop": 0,
        }
        _rs_last = {"hash": None, "at": 0.0}

        def _emit_research_state(force: bool = False) -> None:
            try:
                session_id = os.environ.get("VAF_SESSION_ID", "").strip()
                if not session_id:
                    return
                now = time.time()
                if not force and now - _rs_last["at"] < 0.4:
                    return
                html_ref = _rs_state.get("sectionsHtml_ref")
                payload = {
                    "topic": _rs_state["topic"],
                    "stage": _rs_state["stage"],
                    "sections": _rs_state["sections"],
                    "sectionsHtml": list(html_ref) if html_ref is not None else [],
                    "sources": _rs_state["sources"][:40],
                    "wordsTarget": _rs_state["wordsTarget"],
                    "loop": _rs_state["loop"],
                }
                payload_hash = hash(json.dumps(payload, sort_keys=True, default=str))
                if payload_hash == _rs_last["hash"]:
                    return
                _rs_last["hash"] = payload_hash
                _rs_last["at"] = now
                from vaf.core.web_interface import get_web_interface
                get_web_interface().emit_research_state(payload, session_id=session_id)
                if _debug_lg:
                    _debug_lg.event(
                        "research_state_emitted",
                        stage=payload["stage"],
                        sections=len(payload["sections"]),
                        sources=len(payload["sources"]),
                    )
            except Exception:
                pass

        def _rs_set_stage(stage: str) -> None:
            _rs_state["stage"] = stage
            _emit_research_state(force=True)

        def _rs_set_section(idx0: int, status: str, words: Optional[int] = None) -> None:
            sections = _rs_state["sections"]
            if 0 <= idx0 < len(sections):
                sections[idx0]["status"] = status
                if words is not None:
                    sections[idx0]["words"] = words
            _emit_research_state(force=status in ("done", "error"))

        def _rs_add_sources(results) -> None:
            try:
                from urllib.parse import urlparse
                known = {s["url"] for s in _rs_state["sources"]}
                for r in (results or []):
                    if not isinstance(r, dict):
                        continue
                    url = r.get("href") or r.get("link") or ""
                    if not url or url in known:
                        continue
                    known.add(url)
                    _rs_state["sources"].append({
                        "url": url,
                        "title": (r.get("title") or url)[:120],
                        "domain": (urlparse(url).netloc or "").replace("www.", ""),
                    })
            except Exception:
                pass

        # Live word progress while a section streams: the terminal TUI (and via
        # its refresher the WebUI status line) follows the text being written —
        # "summarizing..." is no longer a silent multi-minute black box.
        _sect_prog_last = [0.0]

        def _section_progress(text_so_far: str) -> None:
            now = time.time()
            if now - _sect_prog_last[0] < 0.5:
                return
            _sect_prog_last[0] = now
            try:
                tui.set_word_progress(_visible_word_count(text_so_far), min_words_target, min_words_ok)
            except Exception:
                pass

        self._section_progress_cb = _section_progress

        def _web_search_with_timeout(web_tool, timeout_sec: int = 0, **kwargs):
            """Run web search with a hard timeout to prevent indefinite hangs."""
            if timeout_sec <= 0:
                timeout_sec = _WEB_SEARCH_TIMEOUT
            holder: Dict[str, Any] = {"result": None, "error": None}

            def _worker():
                try:
                    holder["result"] = web_tool.run(**kwargs)
                except Exception as exc:
                    holder["error"] = exc

            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            t.join(timeout=timeout_sec)
            if t.is_alive():
                if _debug_lg:
                    _debug_lg.event("web_search_timeout", timeout_sec=timeout_sec, kwargs_keys=list(kwargs.keys()))
                return None
            if holder["error"] is not None:
                raise holder["error"]
            return holder["result"]

        def _run_research_loop() -> str:
            nonlocal _debug_lg
            tui.set_stage(f"Starting (lang={lang})")
            tui.log(f"Start: {topic}")
            if _debug_lg:
                _debug_lg.event("research_loop_start", topic=topic, lang=lang, num_sections=len(specs),
                               section_titles=[s.title for s in specs])
            _emit_progress(f"Starting research: {topic}", style="dim")

            # 🛡️ CHECKPOINT SETUP
            checkpoint_dir = Path(".vaf/tmp/research_checkpoints")
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            topic_hash = hashlib.md5(topic.encode()).hexdigest()[:8]
            sources_checkpoint = checkpoint_dir / f"sources_{topic_hash}.json"

            web = WebSearchTool()
            rendered_sections: List[str] = []
            all_sources: List[str] = []
            
            # Load existing sources if resuming
            if sources_checkpoint.exists():
                try:
                    all_sources = json.loads(sources_checkpoint.read_text(encoding="utf-8"))
                except: pass

            global_quality_warning = ""

            # WebUI research window: initialize outline and bind the section list
            _rs_state["topic"] = topic
            _rs_state["sections"] = [
                {"title": s.title, "status": "planned", "words": 0, "targetWords": min_words_target}
                for s in specs
            ]
            _rs_state["sectionsHtml_ref"] = rendered_sections
            _rs_state["wordsTarget"] = min_words_target * len(specs)

            # ── Stage 5: Query Profile (once before section loop) ────────────
            tui.set_stage("Profiling topic...")
            _rs_set_stage("Profiling topic")
            tui.log("Generating query profile...")
            _query_profile = self._generate_query_profile(topic, lang)
            _profile_fallback_queries = _query_profile.get("fallback_queries", [])
            _profile_exclusions = _query_profile.get("exclusion_concepts", [])
            if _debug_lg:
                _debug_lg.event("query_profile_generated",
                               required=_query_profile.get("required_concepts", []),
                               exclusions=_profile_exclusions,
                               fallbacks=_profile_fallback_queries)
            # ─────────────────────────────────────────────────────────────────

            _loop_start = time.time()

            for idx, spec in enumerate(specs, 1):
                # Overall timeout guard
                elapsed_total = time.time() - _loop_start
                if elapsed_total > _OVERALL_TIMEOUT:
                    tui.log(f"Overall timeout ({_OVERALL_TIMEOUT}s) reached at section {idx}/{len(specs)}")
                    _emit_progress(f"Timeout reached after {int(elapsed_total)}s - finalizing with {len(rendered_sections)} sections", style="warning")
                    if _debug_lg:
                        _debug_lg.event("overall_timeout", elapsed=elapsed_total, sections_done=len(rendered_sections))
                    break

                # 🛡️ RESUME CHECK
                safe_title = re.sub(r'[^a-zA-Z0-9]', '_', spec.title)[:30]
                section_checkpoint = checkpoint_dir / f"sec_{topic_hash}_{idx:02d}_{safe_title}.html"

                if section_checkpoint.exists():
                    tui.log(f"Resume: {spec.title}")
                    _emit_progress(f"[{idx}/{len(specs)}] {spec.title}: (resumed from cache)", style="dim")
                    try:
                        section_html = section_checkpoint.read_text(encoding="utf-8")
                        rendered_sections.append(section_html)
                        tui.set_section(idx, len(specs), spec.title)
                        tui.set_word_progress(_visible_word_count(section_html), min_words_target, min_words_ok)
                        _rs_set_section(idx - 1, "done", words=_visible_word_count(section_html))
                        continue
                    except Exception:
                        tui.log(f"Failed to read checkpoint for {spec.title}, re-generating...")

                # ═══════════════════════════════════════════════════════════
                # PER-SECTION: wrapped in try/except for fault isolation
                # ═══════════════════════════════════════════════════════════
                _section_start = time.time()
                try:
                    tui.increment_loop()
                    tui.set_section(idx, len(specs), spec.title)
                    tui.set_word_progress(0, min_words_target, min_words_ok)
                    section_query = topic if not spec.query_suffix else f"{topic} {spec.query_suffix}"
                    tui.set_stage("Searching web")
                    tui.log(f"web_search: {spec.title}")
                    if _debug_lg:
                        _debug_lg.event("section_start", section_idx=idx, section_title=spec.title,
                                       query=section_query[:200])
                    _emit_progress(f"[{idx}/{len(specs)}] {spec.title}: searching sources...", style="dim")
                    _rs_state["loop"] += 1
                    _rs_state["stage"] = f"Searching web {idx}/{len(specs)}"
                    _rs_set_section(idx - 1, "searching")

                    raw_results = None
                    _initial_had_results = False
                    try:
                        # ── Stage 1: Multi-Query Augmentation + Parallel Search ──
                        tui.log(f"augment_queries: {spec.title}")
                        augmented_queries = self._augment_queries(
                            topic=topic,
                            base_query=section_query,
                            lang=lang,
                            n=4,
                        )
                        tui.log(f"parallel_search: {len(augmented_queries)} queries")
                        raw_results = self._search_parallel(
                            web=web,
                            queries=augmented_queries,
                            max_results_per=max_results * 2,
                            deep=deep,
                            timeout_sec=45,
                        )
                        # ─────────────────────────────────────────────────────────
                        _n = len(raw_results) if isinstance(raw_results, list) else 0
                        _initial_had_results = _n > 0
                        if _debug_lg:
                            _debug_lg.event("web_search_complete", section_idx=idx,
                                           num_results=_n, queries=augmented_queries)
                    except Exception as e:
                        tui.log(f"Error in web_search (return_raw): {str(e)[:100]}")
                        if _debug_lg:
                            _debug_lg.event("web_search_error", section_idx=idx, error=str(e)[:300])
                        raw_results = None

                    # Fast path: no results at all → placeholder immediately, skip retry/expand.
                    # Distinguish a SEARCH OUTAGE (providers unreachable/rate-limited,
                    # no API keys) from a genuine no-hit — masking outages as
                    # "no results found" once hid a full DuckDuckGo IP block.
                    if not _initial_had_results:
                        _search_errs = getattr(self, "_last_search_errors", []) or []
                        if _search_errs:
                            tui.log(f"Search providers failing: {_search_errs[0][:80]}")
                            _emit_progress(
                                f"[{idx}/{len(specs)}] {spec.title}: SEARCH UNAVAILABLE - {_search_errs[0][:70]}",
                                style="error",
                            )
                            if _debug_lg:
                                _debug_lg.event("section_search_outage", section_idx=idx,
                                               section_title=spec.title, errors=_search_errs[:3])
                            placeholder = (
                                f"<h2>{spec.title}</h2>"
                                f"<p><em>{'Suchdienst nicht erreichbar (Rate-Limit oder Netzwerkproblem). Bitte später erneut versuchen oder einen Brave/Google-API-Schlüssel in den Einstellungen hinterlegen.' if lang == 'de' else 'Search providers unreachable (rate limit or network issue). Try again later or configure a Brave/Google API key in the settings.'}</em></p>"
                            )
                        else:
                            tui.log(f"No results for: {spec.title} - using placeholder")
                            _emit_progress(f"[{idx}/{len(specs)}] {spec.title}: no results found, skipping", style="dim")
                            if _debug_lg:
                                _debug_lg.event("section_no_results_skip", section_idx=idx, section_title=spec.title)
                            placeholder = (
                                f"<h2>{spec.title}</h2>"
                                f"<p><em>{'Keine ausreichenden Suchergebnisse für diesen Abschnitt gefunden.' if lang == 'de' else 'No sufficient search results found for this section.'}</em></p>"
                            )
                        rendered_sections.append(placeholder)
                        tui.set_word_progress(0, min_words_target, min_words_ok)
                        _rs_set_section(idx - 1, "error", words=0)
                        if _debug_lg:
                            _debug_lg.event("section_complete", section_idx=idx, section_title=spec.title,
                                           word_count=0, elapsed=time.time() - _section_start)
                        continue  # Next section immediately

                    if not isinstance(raw_results, list):
                        tui.log("Using fallback: regular web_search")
                        fallback_result = _web_search_with_timeout(
                            web, timeout_sec=_WEB_SEARCH_TIMEOUT,
                            query=section_query, max_results=max_results, deep=deep, open_in_browser=False,
                        )
                        results = fallback_result if isinstance(fallback_result, str) else "No results found."
                        sources = _extract_urls(results)
                    else:
                        # ── Stage 4: Keyword Gate (fast regex, no LLM) ───────────
                        raw_results = self._apply_keyword_gate(
                            results=raw_results,
                            topic=topic,
                            section_title=spec.title,
                            exclusion_concepts=_profile_exclusions,
                        )
                        tui.log(f"keyword_gate: {len(raw_results)} results remain")
                        # ─────────────────────────────────────────────────────────

                        filtered_results, lowest_score, quality_warning = filter_results_by_quality(
                            raw_results, min_score=7, max_results=max_results
                        )

                        if len(filtered_results) < max_results:
                            tui.log(f"Few high-quality sources, expanding to medium quality...")
                            filtered_medium, _, _ = filter_results_by_quality(
                                raw_results, min_score=4, max_results=max_results
                            )
                            if len(filtered_medium) > len(filtered_results):
                                filtered_results = filtered_medium
                                quality_warning = "Note: Some sources have medium quality. For critical information, additional sources should be consulted."

                        if len(filtered_results) < 3:
                            tui.log(f"Very few results, allowing lower quality sources with warning...")
                            filtered_low, _, _ = filter_results_by_quality(
                                raw_results, min_score=1, max_results=max_results
                            )
                            if len(filtered_low) > len(filtered_results):
                                filtered_results = filtered_low
                                quality_warning = "Warning: Information is based on unverified sources. Please verify critically."

                        # ── Stage 2: Graceful Degradation (when < 3 results) ─────
                        if len(filtered_results) < 3:
                            tui.log(f"Graceful degradation: running fallback queries")
                            _emit_progress(
                                f"[{idx}/{len(specs)}] {spec.title}: fallback search...", style="dim"
                            )
                            _fallback_qs = _profile_fallback_queries[:3] or [
                                topic,
                                f"{topic} overview",
                                f"{topic} {spec.title.lower()}",
                            ]
                            fallback_raw = self._search_parallel(
                                web=web,
                                queries=_fallback_qs,
                                max_results_per=max_results * 2,
                                deep=deep,
                                timeout_sec=45,
                            )
                            if isinstance(fallback_raw, list) and fallback_raw:
                                existing_hrefs = {
                                    r.get("href", "") or r.get("link", "")
                                    for r in filtered_results
                                }
                                for fr in fallback_raw:
                                    fhref = fr.get("href", "") or fr.get("link", "")
                                    if not fhref or fhref not in existing_hrefs:
                                        filtered_results.append(fr)
                                        if fhref:
                                            existing_hrefs.add(fhref)
                                tui.log(f"After fallback: {len(filtered_results)} results")
                                if _debug_lg:
                                    _debug_lg.event("graceful_degradation_complete",
                                                   section_idx=idx,
                                                   fallback_count=len(fallback_raw),
                                                   total_after=len(filtered_results))
                        # ─────────────────────────────────────────────────────────

                        # ── Stage 3: LLM Relevance Pre-Filter (≥4 results only) ──
                        if len(filtered_results) >= 4:
                            filtered_results = self._filter_by_relevance(
                                topic=topic,
                                section_title=spec.title,
                                results=filtered_results,
                                lang=lang,
                            )
                            tui.log(f"relevance_filter: {len(filtered_results)} results kept")
                        # ─────────────────────────────────────────────────────────

                        if quality_warning:
                            if quality_warning not in global_quality_warning:
                                if global_quality_warning:
                                    global_quality_warning += " " + quality_warning
                                else:
                                    global_quality_warning = quality_warning
                            tui.log(f"{quality_warning}")

                        results = self._format_search_results(section_query, filtered_results, deep=deep)
                        sources = [r.get("href", "") or r.get("link", "") for r in filtered_results if r.get("href") or r.get("link")]

                    for u in sources:
                        if u and u not in all_sources:
                            all_sources.append(u)
                    if isinstance(raw_results, list):
                        _rs_add_sources(filtered_results)

                    try:
                        sources_checkpoint.write_text(json.dumps(all_sources), encoding="utf-8")
                    except Exception:
                        pass

                    # Sections cite [n] against the GLOBAL source list (sidebar and
                    # final report use the same numbering).
                    _global_no = {u: all_sources.index(u) + 1 for u in sources if u in all_sources}

                    tui.set_stage("Summarizing")
                    _emit_progress(f"[{idx}/{len(specs)}] {spec.title}: summarizing...", style="dim")
                    _rs_state["stage"] = f"Summarizing {idx}/{len(specs)}"
                    _rs_set_section(idx - 1, "writing")
                    section_html = self._summarize_section_html(
                        topic=topic,
                        title=spec.title,
                        web_results=_truncate(results, 4500),
                        sources=sources,
                        lang=lang,
                        min_words_target=min_words_target,
                        attempt="initial",
                        existing_section_html=(existing_section_html if (existing_section_html and len(specs) == 1) else ""),
                        source_numbers=_global_no,
                    )

                    word_count = _visible_word_count(section_html)
                    text_len = _visible_text_len(section_html)
                    tui.set_word_progress(word_count, min_words_target, min_words_ok)

                    # Retry only if: section is empty AND initial search had results AND enough budget left
                    _remaining = _SECTION_TIMEOUT - (time.time() - _section_start)
                    is_empty = (word_count == 0 and text_len < min_chars_empty) or (word_count > 0 and word_count < max(30, min_words_ok // 8))
                    if is_empty and _initial_had_results and _remaining > (_WEB_SEARCH_TIMEOUT + 30):
                        tui.set_stage("Retry (deep search)")
                        tui.log(f"retry: {spec.title} (empty/too thin)")
                        _emit_progress(f"[{idx}/{len(specs)}] {spec.title}: retrying (deeper search)...", style="dim")
                        retry_raw = _web_search_with_timeout(
                            web, timeout_sec=_WEB_SEARCH_TIMEOUT,
                            query=section_query, max_results=min(15, max_results + 5), deep=True,
                            open_in_browser=False, return_raw=True,
                        )
                        if isinstance(retry_raw, list) and len(retry_raw) > 0:
                            retry_filtered, _, retry_warning = filter_results_by_quality(retry_raw, min_score=1, max_results=min(10, max_results + 2))
                            retry_results = self._format_search_results(section_query, retry_filtered, deep=True)
                            retry_sources = [r.get("href", "") or r.get("link", "") for r in retry_filtered if r.get("href") or r.get("link")]
                            if retry_warning and retry_warning not in global_quality_warning:
                                if global_quality_warning:
                                    global_quality_warning += " " + retry_warning
                                else:
                                    global_quality_warning = retry_warning

                            for u in retry_sources:
                                if u and u not in all_sources:
                                    all_sources.append(u)
                            _rs_add_sources(retry_filtered)
                            tui.set_stage("Summarizing (retry)")
                            _rs_set_stage(f"Retry (deep search) {idx}/{len(specs)}")
                            _emit_progress(f"[{idx}/{len(specs)}] {spec.title}: summarizing (retry)...", style="dim")
                            section_html = self._summarize_section_html(
                                topic=topic,
                                title=spec.title,
                                web_results=_truncate(retry_results, 4500),
                                sources=retry_sources,
                                lang=lang,
                                min_words_target=min_words_target,
                                attempt="retry",
                                existing_section_html="",
                                source_numbers={u: all_sources.index(u) + 1 for u in retry_sources if u in all_sources},
                            )
                            word_count = _visible_word_count(section_html)
                            text_len = _visible_text_len(section_html)
                            tui.set_word_progress(word_count, min_words_target, min_words_ok)
                        else:
                            tui.log(f"Retry also returned no results, keeping placeholder")
                            _emit_progress(f"[{idx}/{len(specs)}] {spec.title}: retry had no results", style="dim")

                    # Expand only if initial search had results AND section is short AND enough budget left
                    _remaining = _SECTION_TIMEOUT - (time.time() - _section_start)
                    is_short = (word_count > 0 and word_count < min_words_ok) or (word_count == 0 and min_chars_empty <= text_len < min_chars_ok)
                    if is_short and _initial_had_results and _remaining > (_WEB_SEARCH_TIMEOUT + 30):
                        tui.set_stage("Append expand")
                        tui.log(f"append: {spec.title} ({word_count} words)")
                        _emit_progress(f"[{idx}/{len(specs)}] {spec.title}: expanding content...", style="dim")
                        _rs_set_stage(f"Expanding {idx}/{len(specs)}")
                        section_html = self._summarize_section_html(
                            topic=topic,
                            title=spec.title,
                            web_results=_truncate(results, 4500),
                            sources=sources,
                            lang=lang,
                            min_words_target=min_words_target,
                            attempt="append",
                            existing_section_html=section_html,
                            source_numbers=_global_no,
                        )
                        word_count = _visible_word_count(section_html)
                        tui.set_word_progress(word_count, min_words_target, min_words_ok)
                    rendered_sections.append(section_html)
                    _rs_set_section(idx - 1, "done", words=word_count)

                    try:
                        section_checkpoint.write_text(section_html, encoding="utf-8")
                    except Exception:
                        pass

                    if _debug_lg:
                        _debug_lg.event("section_complete", section_idx=idx, section_title=spec.title,
                                       word_count=word_count, elapsed=time.time() - _section_start)

                except Exception as sec_err:
                    elapsed_sec = time.time() - _section_start
                    tui.log(f"ERROR in {spec.title}: {str(sec_err)[:100]}")
                    _emit_progress(f"[{idx}/{len(specs)}] {spec.title}: ERROR - {str(sec_err)[:80]}", style="error")
                    if _debug_lg:
                        _debug_lg.event("section_error", section_idx=idx, section_title=spec.title,
                                       error=str(sec_err)[:500], elapsed=elapsed_sec)
                    placeholder = (
                        f"<h2>{spec.title}</h2>"
                        f"<p><em>{'Fehler beim Generieren dieses Abschnitts.' if lang == 'de' else 'Error generating this section.'}</em></p>"
                    )
                    rendered_sections.append(placeholder)
                    _rs_set_section(idx - 1, "error", words=0)

            total_elapsed = time.time() - _loop_start
            total_words = sum(_visible_word_count(s) for s in rendered_sections)
            _emit_progress(
                f"Research completed: {len(rendered_sections)} sections, ~{total_words} words, "
                f"{len(all_sources)} sources ({int(total_elapsed)}s)",
                style="success",
                presence="idle",
            )
            if _debug_lg:
                _debug_lg.event("research_loop_complete", sections=len(rendered_sections),
                               words=total_words, sources=len(all_sources), elapsed=total_elapsed)

            # Assemble and Save
            tui.set_stage("Finalizing")
            _rs_set_stage("Finalizing")
            html = self._assemble_html(topic, rendered_sections, all_sources, lang, global_quality_warning)
            _rs_set_stage("Completed")

            try:
                for idx, spec in enumerate(specs, 1):
                    safe_title = re.sub(r'[^a-zA-Z0-9]', '_', spec.title)[:30]
                    section_checkpoint = checkpoint_dir / f"sec_{topic_hash}_{idx:02d}_{safe_title}.html"
                    if section_checkpoint.exists(): section_checkpoint.unlink()
                if sources_checkpoint.exists(): sources_checkpoint.unlink()
            except Exception:
                pass

            # Direct sub-agent research (not in a workflow): ALWAYS save a .docx and open it
            # in the native Word editor — regardless of which format the model requested. The
            # requested format only matters to programmatic / workflow callers (handled below).
            if os.environ.get("VAF_IN_SUBAGENT_TERMINAL") == "1" and not in_workflow:
                from vaf.core.session import resolve_agent_output_dir
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe_topic = re.sub(r'[^\w\s-]', '', topic).strip().replace(' ', '_')[:50]
                output_dir = resolve_agent_output_dir(Platform.get_research_dir())
                output_path = output_dir / f"research_{safe_topic}_{timestamp}.docx"
                try:
                    from vaf.core.document_formatting import infer_document_model, save_document_model_as_docx
                    md_sections = [self._section_html_to_markdown(s) for s in rendered_sections]
                    md_clean = self._assemble_markdown(topic, md_sections, all_sources)
                    md_clean = re.sub(r'(?m)^_Generated:.*$', '', md_clean)  # drop metadata so it isn't a stray "Content" section
                    model = infer_document_model(f"Research Report: {topic}", "report", md_clean)
                    save_document_model_as_docx(model, output_path)
                    if _debug_lg:
                        _debug_lg.event("saving_report_docx", output_path=str(output_path), sections=len(model.sections))
                except Exception as e:
                    if _debug_lg:
                        _debug_lg.event("docx_save_failed_fallback_md", error=str(e))
                    UI.warning(f"DOCX export failed ({e}); saving as Markdown instead.")
                    output_path = output_dir / f"research_{safe_topic}_{timestamp}.md"
                    with open(output_path, 'w', encoding='utf-8') as f:
                        f.write(self._assemble_markdown(topic, rendered_sections, all_sources))

                webui_mode = os.environ.get("VAF_WEBUI_ACTIVE", "").strip().lower() in ("1", "true", "yes")
                session_id = os.environ.get("VAF_SESSION_ID", "").strip()
                if webui_mode and session_id:
                    try:
                        from vaf.core.web_interface import notify_document_created
                        # Research reports open read-only in the Document Viewer (sidebar + RAG),
                        # not the editable Document Editor (which document_agent still uses).
                        notify_document_created(session_id, str(output_path), title=output_path.name, open_mode="viewer")
                    except Exception:
                        pass

                word_count = sum(len(s.split()) for s in rendered_sections)
                outline = "; ".join(getattr(sp, "title", str(sp)) for sp in specs[:15])
                if _debug_lg:
                    _debug_lg.event("research_complete", output_path=str(output_path), word_count=word_count, num_sections=len(rendered_sections))
                return (
                    f"TASK COMPLETE — Research Report: {topic}\n\n"
                    f"Saved to: {output_path}\n"
                    f"{len(rendered_sections)} sections, ~{word_count} words\n"
                    f"{len(all_sources)} sources analyzed\n"
                    f"Outline (for your verbal summary to the user): {outline}\n\n"
                    f"The report has been saved and is now open in the Document Editor.\n"
                    f"DO NOT run another research or open the file again. "
                    f"Summarize briefly for the user using the outline and counts above."
                )

            if out_format == "html_fragment":
                # Return only fragments (useful for patching missing sections)
                return "\n\n".join(rendered_sections).strip()

            if out_format == "markdown":
                # Direct sub-agent markdown is handled by the .docx path above; this returns
                # markdown only to programmatic / workflow callers.
                return self._assemble_markdown(topic, rendered_sections, all_sources)

            # ═══════════════════════════════════════════════════════════════
            # SAVE HTML TO FILE + RETURN SHORT SUMMARY (for sub-agent mode)
            # ═══════════════════════════════════════════════════════════════
            html = self._assemble_html(topic, rendered_sections, all_sources, lang=lang, quality_warning=global_quality_warning)
            
            # Check if running as sub-agent (separate terminal) - but NOT when in a workflow.
            # In workflows (e.g. deep_research), the workflow expects full HTML for repair_report + write_file
            # to save to output_file. The subagent flag in workflow context only prevents nested terminals.
            is_subagent = os.environ.get("VAF_IN_SUBAGENT_TERMINAL") == "1"
            
            if is_subagent and not in_workflow:
                # Generate filename
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe_topic = re.sub(r'[^\w\s-]', '', topic).strip().replace(' ', '_')[:50]
                filename = f"research_{safe_topic}_{timestamp}.html"

                # Save to the chat's workspace when a session exists (visible in the
                # WebUI workspace browser); Documents/VAF_Research otherwise.
                from vaf.core.session import resolve_agent_output_dir
                output_dir = resolve_agent_output_dir(Platform.get_research_dir())
                output_path = output_dir / filename

                if _debug_lg:
                    _debug_lg.event("saving_report", output_path=str(output_path), html_len=len(html),
                                   num_sections=len(rendered_sections), num_sources=len(all_sources))

                with open(output_path, 'w', encoding='utf-8') as f:
                    f.write(html)

                if _debug_lg:
                    _debug_lg.event("report_saved", output_path=str(output_path))

                try:
                    UI.success(f"Research saved: {output_path}")
                except (BrokenPipeError, OSError):
                    pass

                if _debug_lg:
                    _debug_lg.event("post_report_saved_ui_done")

                # Only open browser when running locally (not in WebUI/network mode
                # where the browser would open on the server, not on the client).
                webui_mode = os.environ.get("VAF_WEBUI_ACTIVE", "").strip().lower() in ("1", "true", "yes")
                session_id = os.environ.get("VAF_SESSION_ID", "").strip()
                open_result = {"done": False, "ok": False}

                # In WebUI mode, push the report directly into the Document Viewer panel.
                # This avoids an extra "read file ..." sub-agent roundtrip just to show the report.
                if webui_mode and session_id:
                    try:
                        from vaf.core.web_interface import notify_document_created
                        # Research reports open read-only in the Document Viewer (sidebar + RAG),
                        # not the editable Document Editor (which document_agent still uses).
                        notify_document_created(session_id, str(output_path), title=output_path.name, open_mode="viewer")
                    except Exception:
                        pass

                if not webui_mode:
                    abs_path = output_path.absolute()
                    file_url = f"file:///{abs_path.as_posix()}"

                    def _open_report() -> None:
                        try:
                            open_result["ok"] = bool(Platform.open_url(file_url, incognito=True))
                        except Exception:
                            open_result["ok"] = False
                        finally:
                            open_result["done"] = True

                    opener = threading.Thread(target=_open_report, daemon=True)
                    opener.start()
                    opener.join(timeout=2.0)

                if _debug_lg:
                    _debug_lg.event("post_open_url", done=open_result["done"], ok=open_result["ok"],
                                   skipped_webui=webui_mode)

                try:
                    if not webui_mode and open_result["done"] and open_result["ok"]:
                        UI.event("Browser", f"Opened: {filename}", style="success")
                    else:
                        UI.info(f"Report: {output_path}")
                except (BrokenPipeError, OSError):
                    pass

                word_count = sum(len(s.split()) for s in rendered_sections)
                outline = "; ".join(getattr(sp, "title", str(sp)) for sp in specs[:15])
                summary = (
                    f"TASK COMPLETE — Research Report: {topic}\n\n"
                    f"Saved to: {output_path}\n"
                    f"{len(rendered_sections)} sections, ~{word_count} words\n"
                    f"{len(all_sources)} sources analyzed\n"
                    f"Outline (for your verbal summary to the user): {outline}\n\n"
                    f"The report has been saved and is now open in the Document Editor.\n"
                    f"DO NOT run another research or open the file again. "
                    f"Summarize briefly for the user using the outline and counts above."
                )
                if _debug_lg:
                    _debug_lg.event("research_complete", output_path=str(output_path),
                                   word_count=word_count, num_sections=len(rendered_sections))
                return summary
            else:
                # Direct call (not sub-agent): return full HTML as before
                _emit_progress("Research completed.", style="success")
                if _debug_lg:
                    _debug_lg.event("research_complete_inline", html_len=len(html),
                                   num_sections=len(rendered_sections))
                return html

        if not use_live:
            import logging as _logging
            for _noisy in ("httpx", "httpcore"):
                _logging.getLogger(_noisy).setLevel(_logging.WARNING)
            if _is_piped_subprocess:
                _logging.getLogger().setLevel(_logging.WARNING)
            if not in_workflow and not _is_piped_subprocess:
                try:
                    UI.console.print(tui.render())
                except (BrokenPipeError, OSError):
                    pass
                except Exception:
                    pass
            prev_suppress = os.environ.get("VAF_SUPPRESS_WEB_SEARCH_EVENTS")
            os.environ["VAF_SUPPRESS_WEB_SEARCH_EVENTS"] = "1"
            try:
                return _run_research_loop()
            finally:
                if prev_suppress is None:
                    os.environ.pop("VAF_SUPPRESS_WEB_SEARCH_EVENTS", None)
                else:
                    os.environ["VAF_SUPPRESS_WEB_SEARCH_EVENTS"] = prev_suppress

        # Live animation while we work (smooth refresh like coding_agent).
        # CRITICAL: Render once before starting Live to prevent multiple empty renders
        tui.set_stage("Initializing...")
        initial_render = tui.render()
        
        # Live rendering (match coding_agent): use the shared UI.console.
        live = Live(
            initial_render,
            console=UI.console,
            refresh_per_second=15,
            transient=False,  # Keep final output visible after stop
        )
        live.start()

        # Background refresh thread (match coding_agent): update Live regularly for smooth animations.
        animation_running = threading.Event()
        animation_running.set()

        def _refresher():
            while animation_running.is_set():
                try:
                    # IMPORTANT: do NOT force refresh=True; let Live handle in-place updates.
                    live.update(tui.render())
                    time.sleep(1.0 / 15)  # 15 FPS
                except Exception:
                    break

        t = threading.Thread(target=_refresher, daemon=True)
        t.start()

        # Set up event-driven updates (like coding_agent does explicitly)
        # This ensures TUI updates immediately when state changes, not just periodically
        def trigger_update():
            try:
                live.update(tui.render())
            except Exception:
                pass  # Live might be stopped
        
        tui.set_on_change(trigger_update)

        try:
            # Suppress web_search "Reading ..." prints while Live is active (prevents console spam / broken Live updates)
            prev_suppress = os.environ.get("VAF_SUPPRESS_WEB_SEARCH_EVENTS")
            os.environ["VAF_SUPPRESS_WEB_SEARCH_EVENTS"] = "1"
            try:
                result = _run_research_loop()
            finally:
                if prev_suppress is None:
                    os.environ.pop("VAF_SUPPRESS_WEB_SEARCH_EVENTS", None)
                else:
                    os.environ["VAF_SUPPRESS_WEB_SEARCH_EVENTS"] = prev_suppress
            # Ensure we stop the animation before returning
            animation_running.clear()
            try:
                live.stop()
            except Exception:
                pass
            return result
        except Exception as e:
            # On any error, stop animation and re-raise
            animation_running.clear()
            try:
                live.stop()
            except Exception:
                pass
            # Log error to TUI before raising
            UI.event("Research Agent", f"Error: {str(e)[:100]}", style="error")
            raise

    def _generate_plan(self, topic: str, lang: str) -> List[SectionSpec]:
        """Dynamically generate section titles based on topic analysis via LLM."""
        try:
            # 1. Analyze topic and generate plan
            prompt = (
                f"Create a research plan for the topic: '{topic}'.\n"
                f"Language: {lang}\n"
                "Return a JSON list of 6-8 section titles that cover this topic comprehensively.\n"
                "Examples:\n"
                "- Person: Biography, Career, Impact, Controversies\n"
                "- Tech: Features, Architecture, Use Cases, Pros/Cons\n"
                "- Event: Background, Timeline, Key Figures, Aftermath\n\n"
                "Return ONLY the JSON list of strings, e.g. [\"Section 1\", \"Section 2\"]."
            )
            
            # Provider-aware: query_llm uses the configured cloud provider (api_model_{provider}) or
            # the local server, instead of a hardcoded 127.0.0.1:8080 that only exists in local mode
            # (which made research planning silently fall back to the static plan on DeepSeek/cloud).
            content = self.query_llm(
                [{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.3,
            )

            if content:
                # Extract JSON list
                import json
                try:
                    # Find list brackets
                    start = content.find('[')
                    end = content.rfind(']') + 1
                    if start >= 0 and end > start:
                        json_str = content[start:end]
                        titles = json.loads(json_str)
                        if isinstance(titles, list) and all(isinstance(t, str) for t in titles):
                            # Convert to specs
                            return [SectionSpec(t, t.lower()) for t in titles]
                except Exception:
                    pass
        except Exception:
            pass
        
        # Fallback to defaults if generation fails
        return [
            SectionSpec("Overview", ""),
            SectionSpec("Background & History", "history background"),
            SectionSpec("Key Concepts / Features", "concepts features"),
            SectionSpec("Impact & Significance", "impact importance"),
            SectionSpec("Current Status / Latest News", "current status news"),
            SectionSpec("Pros & Cons / Criticism", "pros cons criticism"),
            SectionSpec("Conclusion", "conclusion summary")
        ]

    # ═══════════════════════════════════════════════════════════════════════════
    # PaperNavigator-inspired search improvements (Stages 1–5)
    # ═══════════════════════════════════════════════════════════════════════════

    def _generate_query_profile(self, topic: str, lang: str) -> Dict:
        """Stage 5: One-time LLM call before the section loop to build a topic profile.

        Returns dict with required_concepts, optional_concepts, exclusion_concepts,
        fallback_queries.  On any failure returns {} so all callers degrade gracefully.
        """
        prompt = (
            f"You are a research planner. For the topic: \"{topic}\" (language: {lang})\n"
            "Return ONLY valid JSON with these keys:\n"
            "  required_concepts: list of 3-5 essential terms that good search results MUST contain\n"
            "  optional_concepts: list of 3-5 helpful but non-mandatory terms\n"
            "  exclusion_concepts: list of 2-4 terms that indicate an IRRELEVANT result\n"
            "  fallback_queries: list of 3 alternative search queries if the main query fails\n"
            "Return ONLY the JSON object, no explanation."
        )
        try:
            response = self.query_llm(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                temperature=0.1,
            )
            if not response:
                return {}
            start = response.find("{")
            end = response.rfind("}") + 1
            if start < 0 or end <= start:
                return {}
            parsed = json.loads(response[start:end])
            result = {}
            for key in ("required_concepts", "optional_concepts", "exclusion_concepts", "fallback_queries"):
                val = parsed.get(key, [])
                result[key] = [str(v) for v in val] if isinstance(val, list) else []
            return result
        except Exception:
            return {}

    _QUERY_STOPWORDS = {
        'für', 'und', 'der', 'die', 'das', 'den', 'dem', 'ein', 'eine', 'einen',
        'mit', 'von', 'auf', 'aus', 'bei', 'zur', 'zum', 'über', 'oder', 'als',
        'the', 'and', 'for', 'with', 'from', 'about', 'into', 'over', 'this', 'that',
    }

    def _compress_query(self, text: str, max_words: int = 7) -> str:
        """Searchable short query from a long topic sentence.

        Long prompt-style topics ("Strategie für X GmbH (Y Labs) und Z Framework
        - Marktanalyse, ...") return zero hits verbatim; search engines want a
        handful of keywords. Drops parentheses, punctuation and stopwords.
        """
        t = re.sub(r'\([^)]*\)', ' ', text or '')
        t = re.sub(r'[-–—:;,/"\']+', ' ', t)
        words = [w for w in t.split() if w.lower() not in self._QUERY_STOPWORDS and len(w) > 2]
        return ' '.join(words[:max_words]) or (text or '')[:60]

    def _augment_queries(self, topic: str, base_query: str, lang: str, n: int = 4) -> List[str]:
        """Stage 1a: Generate n-1 alternative search queries for a section via LLM.

        Fallback-safe: even when the LLM fails (reasoning models burning their
        budget, JSON parse errors), this returns MULTIPLE short keyword queries
        instead of one unsearchable full-sentence query.
        """
        short_base = self._compress_query(base_query)
        fallback = [short_base]
        if len(base_query) <= 60 and base_query.lower() != short_base.lower():
            fallback.append(base_query)
        kw4 = ' '.join(short_base.split()[:4])
        if kw4 and kw4.lower() != short_base.lower():
            fallback.append(kw4)

        prompt = (
            f"Generate {n - 1} alternative web search queries for researching this topic.\n"
            f"Main topic: \"{topic}\"\n"
            f"Base query: \"{short_base}\"\n"
            f"Language: {lang}\n"
            "Rules:\n"
            "- Each query must use different keywords or angles than the base query\n"
            "- Queries should be 3-8 words, suitable for a web search engine\n"
            "- Return ONLY a JSON list of strings, no explanation\n"
            f"Example: [\"{short_base} overview\", \"{self._compress_query(topic, 4)} technical details\"]"
        )
        try:
            response = self.query_llm(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=700,
                temperature=0.3,
            )
            if not response:
                return fallback
            # Reasoning models bury the answer at the end — strip think blocks
            # and take the LAST parseable JSON array in the text.
            cleaned = re.sub(r'<think>[\s\S]*?</think>', '', response)
            variants = None
            for m in reversed(re.findall(r'\[[^\[\]]*\]', cleaned)):
                try:
                    candidate = json.loads(m)
                    if isinstance(candidate, list):
                        variants = candidate
                        break
                except Exception:
                    continue
            if variants is None:
                return fallback
            queries = [short_base]
            for v in variants:
                v = str(v).strip()
                # Reject placeholder echoes ("Query 1", "query2") — small models
                # sometimes return the example FORMAT instead of real queries
                if re.fullmatch(r'(?i)query[\s_]*\d+', v):
                    continue
                if v and v.lower() != short_base.lower() and len(v) <= 90 and len(queries) < n:
                    queries.append(v)
            return queries if len(queries) > 1 else fallback
        except Exception:
            return fallback

    def _search_parallel(
        self,
        web: "WebSearchTool",
        queries: List[str],
        max_results_per: int,
        deep: bool,
        timeout_sec: int,
    ) -> List[Dict]:
        """Stage 1b: Run multiple search queries in parallel via ThreadPoolExecutor.

        Merges results and deduplicates by href. Results without href are always kept.
        """
        # Collected per-call so 0 results from FAILING providers (DDG rate-limit,
        # network down, no API keys) is distinguishable from a genuine no-hit —
        # masking outages as "no results found" hid a full search outage once.
        # Provider-level failures are swallowed inside search.py; pull them from
        # its error collector after the searches ran.
        self._last_search_errors = []
        try:
            from vaf.tools.search import reset_search_provider_errors
            reset_search_provider_errors()
        except Exception:
            pass

        if not queries:
            return []

        seen_hrefs: set = set()
        merged: List[Dict] = []
        lock = threading.Lock()

        def _search_one(query: str) -> List[Dict]:
            holder: Dict[str, Any] = {"result": None}

            def _worker():
                try:
                    holder["result"] = web.run(
                        query=query,
                        max_results=max_results_per,
                        deep=deep,
                        open_in_browser=False,
                        return_raw=True,
                    )
                except Exception as exc:
                    with lock:
                        self._last_search_errors.append(str(exc)[:160])

            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            t.join(timeout=timeout_sec)
            raw = holder["result"]
            return raw if isinstance(raw, list) else []

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(queries), 4)) as executor:
                futures = {executor.submit(_search_one, q): q for q in queries}
                for fut in concurrent.futures.as_completed(futures, timeout=60):
                    try:
                        results = fut.result()
                    except Exception:
                        results = []
                    with lock:
                        for r in results:
                            href = r.get("href", "") or r.get("link", "")
                            if not href:
                                merged.append(r)
                            elif href not in seen_hrefs:
                                seen_hrefs.add(href)
                                merged.append(r)
        except concurrent.futures.TimeoutError:
            pass
        except Exception:
            pass

        try:
            from vaf.tools.search import get_search_provider_errors
            self._last_search_errors = get_search_provider_errors()
        except Exception:
            pass

        return merged

    def _apply_keyword_gate(
        self,
        results: List[Dict],
        topic: str,
        section_title: str,
        exclusion_concepts: Optional[List[str]] = None,
    ) -> List[Dict]:
        """Stage 4: Fast regex pre-filter — runs before quality filtering, no LLM cost.

        Keeps results that have ≥2 keyword hits and no exclusion term.
        Safety valve: if fewer than 2 results would pass, returns original list unchanged.
        """
        STOPWORDS = {
            "a", "an", "the", "and", "or", "of", "in", "on", "at", "to",
            "for", "with", "by", "from", "is", "are", "was", "were", "be",
            "been", "being", "have", "has", "had", "do", "does", "did",
            "will", "would", "could", "should", "may", "might", "shall",
            "this", "that", "these", "those", "it", "its", "not", "as",
            "what", "how", "when", "where", "which", "who", "about",
            "der", "die", "das", "ein", "eine", "und", "oder", "von",
            "mit", "für", "ist", "sind", "war", "wurde", "werden", "als",
            "auch", "noch", "nach", "bei", "über", "auf", "aus",
        }

        if not results:
            return results

        raw_terms = f"{topic} {section_title}".lower()
        keywords = [
            w for w in re.findall(r"[\w\u00C0-\u024F]{3,}", raw_terms, re.UNICODE)
            if w not in STOPWORDS
        ]

        if len(keywords) < 2:
            return results

        exclusions = [e.lower() for e in (exclusion_concepts or [])]

        def _passes(result: Dict) -> bool:
            text = (
                (result.get("body", "") or "") + " " +
                (result.get("title", "") or "")
            ).lower()
            for excl in exclusions:
                if excl and re.search(r"\b" + re.escape(excl) + r"\b", text):
                    return False
            hits = sum(1 for kw in keywords if kw in text)
            return hits >= 2

        passed = [r for r in results if _passes(r)]
        return passed if len(passed) >= 2 else results

    def _filter_by_relevance(
        self,
        topic: str,
        section_title: str,
        results: List[Dict],
        lang: str,
    ) -> List[Dict]:
        """Stage 3: LLM batch relevance filter — one cheap call for all snippets.

        Only runs when len(results) >= 4. Falls back to returning original list on
        any failure or if the LLM marks everything irrelevant.
        """
        if len(results) < 4:
            return results

        lines = []
        for i, r in enumerate(results, 1):
            title = (r.get("title", "") or "")[:80]
            body = (r.get("body", "") or "")[:120]
            lines.append(f"[{i}] {title}: {body}")

        prompt = (
            f"Topic: \"{topic}\"\nSection: \"{section_title}\"\n"
            "For each numbered result, reply YES if relevant to the topic+section, NO if not.\n"
            "Format: 1:YES, 2:NO, 3:YES  (comma-separated, NO extra text)\n\n"
            + "\n".join(lines)
        )

        try:
            response = self.query_llm(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=80,
                temperature=0.0,
            )
            if not response:
                return results

            keep_indices: set = set()
            for match in re.finditer(r"(\d+)\s*[:.]\s*(YES|NO)", response.upper()):
                if match.group(2) == "YES":
                    keep_indices.add(int(match.group(1)))

            if not keep_indices:
                return results

            filtered = [r for i, r in enumerate(results, 1) if i in keep_indices]
            return filtered if filtered else results
        except Exception:
            return results

    def _stream_section_completion(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int,
        temperature: float,
        idle_timeout: int = 75,
        on_progress=None,
    ) -> str:
        """Stream a section from the LOCAL llama server with an IDLE timeout.

        The coder's lesson applied here: a fixed total timeout kills legitimate
        long thinking (and the orphaned request keeps the single server slot
        busy, starving the retry). Streaming aborts only when NO tokens arrive
        for `idle_timeout` seconds — the model may think as long as it makes
        progress. Reasoning deltas count as progress but are never returned;
        only `content` is. Closing the response on abort frees the server slot.
        """
        from vaf.core.config import Config
        body = {
            "model": Config.get("model", "") or "user-model",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        content_parts: List[str] = []
        try:
            with requests.post(
                "http://127.0.0.1:8080/v1/chat/completions",
                json=body,
                stream=True,
                timeout=(10, idle_timeout),  # read timeout = per-chunk idle timeout
            ) as resp:
                resp.raise_for_status()
                for raw_line in resp.iter_lines():
                    # Decode explicitly as UTF-8: llama-server sends `text/event-stream`
                    # WITHOUT a charset, so requests would assume ISO-8859-1 and turn
                    # every umlaut into mojibake ("für" -> "fÃ¼r").
                    line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        delta = (json.loads(data_str).get("choices") or [{}])[0].get("delta") or {}
                    except Exception:
                        continue
                    piece = delta.get("content") or ""
                    if piece:
                        content_parts.append(piece)
                        if on_progress:
                            try:
                                on_progress("".join(content_parts))
                            except Exception:
                                pass
                    # reasoning_content deltas keep the connection alive (= progress
                    # for the idle timeout) but are intentionally not collected.
        except Exception as e:
            try:
                from vaf.core.log_helper import append_domain_log
                append_domain_log("backend", f"research section stream ended: {type(e).__name__}: {str(e)[:120]}")
            except Exception:
                pass
        return "".join(content_parts).strip()

    def _sanitize_section_output(self, raw: str, title: str) -> str:
        """Guard against reasoning leaks in section text.

        query_llm falls back to reasoning_content when a reasoning model's
        answer was cut off — a live run once filled every report section with
        "Thinking Process: 1. **Analyze the Request:** ...". Cuts the text to
        the first HTML block; pure chain-of-thought without any HTML is
        rejected (empty return), so the caller's retry path takes over.
        """
        if not raw:
            return ""
        text = re.sub(r'<think>[\s\S]*?</think>', '', raw).strip()
        # Models often wrap the fragment in markdown fences (```html ... ```)
        text = re.sub(r'^```[a-zA-Z]*\s*', '', text)
        text = re.sub(r'\s*```\s*$', '', text).strip()
        # Mask inline-code spans for tag detection: leaked reasoning often QUOTES
        # the prompt's structure examples ("`<h2>Section title</h2>`, 3-6
        # paragraphs, ..."), and cutting at such a quoted tag kept the reasoning.
        masked = re.sub(r'`[^`\n]*`', lambda mm: ' ' * len(mm.group(0)), text)
        m = re.search(r'<(h2|h3|p|div|ul|ol|section|table)\b', masked, re.IGNORECASE)
        if m:
            text = text[m.start():].strip()
            if '<h2' not in text.lower():
                text = f"<h2>{title}</h2>\n{text}"
            # Normalize structure: drop per-section source blocks (the report has
            # one global numbered list), fix runaway headings, wrap loose text.
            text = _strip_section_source_blocks(text)
            text = _clamp_runaway_heading(text, title)
            text = _wrap_loose_section_text(text)
            return text
        low = text.lower()
        reasoning_markers = (
            'thinking process', 'okay', 'let me', "let's", 'first', 'alright',
            'the user', 'we need', 'i need', 'hmm', '1. **analyze',
        )
        if not text or any(low.startswith(mk) for mk in reasoning_markers):
            return ""
        # Plain prose without any tags: keep it, but give the section its heading
        return f"<h2>{title}</h2>\n<p>{text}</p>"

    def _summarize_section_html(
        self,
        topic: str,
        title: str,
        web_results: str,
        sources: Sequence[str],
        lang: str,
        min_words_target: int,
        attempt: str = "initial",
        existing_section_html: str = "",
        source_numbers: Optional[Dict[str, int]] = None,
    ) -> str:
        """
        Call the model for ONE section only (bounded input), return an HTML fragment.

        `source_numbers` maps a source URL to its number in the report's GLOBAL
        source list — sections cite with [n] markers against that list instead
        of writing URLs or per-section source paragraphs.
        """
        # Data sanity check
        if not web_results or len(str(web_results).strip()) < 50:
            if lang == "de":
                return f"<h2>{title}</h2><p><em>Keine ausreichenden Suchergebnisse für diesen Abschnitt gefunden.</em></p>"
            return f"<h2>{title}</h2><p><em>Insufficient search results found for this section.</em></p>"

        # Robust language instruction supporting all detected languages (ISO codes like DE, FR, ES, etc.)
        lang_upper = lang.upper()
        lang_instruction = (
            f"WRITE EXCLUSIVELY IN LANGUAGE: {lang_upper}.\n"
            f"Translate all information from sources into {lang_upper}.\n"
            f"Maintain a professional and consistent style in {lang_upper}."
        )
            
        attempt = (attempt or "initial").strip().lower()

        extra = ""
        if attempt == "retry":
            extra = "Try again with better coverage and specificity while staying evidence-based."
        elif attempt == "expand":
            extra = "Expand significantly with more detail and concrete examples while staying evidence-based."
        elif attempt == "append":
            extra = (
                "You will be given an existing HTML fragment for this section. "
                "DO NOT rewrite it. Keep it, and ONLY APPEND new content to reach the length requirement. "
                "Avoid repeating points already present."
            )

        base_instructions = (
            "Write ONE section of an HTML research report.\n"
            f"Main topic: {topic}\n"
            f"Section title: {title}\n\n"
            f"{lang_instruction}\n"
            f"Length requirement: at least {min_words_target} words.\n"
            f"{extra}\n"
            "CRITICAL FACTUAL INTEGRITY RULE (OVERRIDES ALL OTHER INSTRUCTIONS):\n"
            "Specific facts — proper names, ID numbers, patent numbers, company names, inventor names,\n"
            "filing dates, registration numbers, statistics — MUST come VERBATIM from the search results.\n"
            "If a specific fact is NOT explicitly stated in the search results provided:\n"
            "  DO NOT generate it, guess it, or fill it in from training knowledge.\n"
            "  Instead write: <em>[Data not found in search results]</em>\n"
            "A short accurate report is FAR better than a long hallucinated one.\n\n"
            "CITATION GUIDELINES:\n"
            "1. Support every claim with an explicit search result.\n"
            "2. Cite by putting the bracketed NUMBER of the supporting source directly after the\n"
            "   statement, e.g. 'Die Strategie wurde 2020 verabschiedet [3].' Use ONLY the numbers\n"
            "   from the numbered source list below.\n"
            "3. NEVER write URLs in the text and NEVER add a 'Sources'/'Quellen' paragraph or list\n"
            "   to the section — the report appends ONE global numbered source list automatically.\n"
            "4. If a claim cannot be supported by the provided search results, use <em>[Not found in sources]</em>.\n"
            "Return ONLY an HTML fragment (no <html>, no <head>, no <body>).\n"
        )

        def _numbered_source_lines(urls: Sequence[str]) -> str:
            nums = source_numbers or {}
            return "\n".join(f"[{nums.get(u, i + 1)}] {u}" for i, u in enumerate(urls))

        if attempt == "append" and existing_section_html:
            prompt = (
                base_instructions
                + "You are given the EXISTING section HTML below.\n"
                  "Return the UPDATED FULL section HTML fragment.\n"
                  "- Keep exactly one <h2> at the top\n"
                  "- Keep existing content intact\n"
                  "- Append 2-4 additional paragraphs and, if useful, add 2-4 new bullets (no duplicates)\n"
                  "- Be coherent and avoid repetition\n\n"
                  "EXISTING SECTION HTML:\n"
                + existing_section_html
                + "\n\nWeb search results:\n"
                + str(web_results)
                + "\n\nNumbered source list (cite as [n] after the supported statement):\n"
                + _numbered_source_lines(sources[:6])
            )
        else:
            prompt = (
                base_instructions
                + "Structure:\n"
                  "- <h2>Section title</h2>\n"
                  "- 3-6 paragraphs\n"
                  "- <ul> with 6-10 key bullets\n"
                  "- If uncertain, say so briefly.\n\n"
                  "Web search results:\n"
                + str(web_results)
                + "\n\nNumbered source list (cite as [n] after the supported statement):\n"
                + _numbered_source_lines(sources[:6])
            )

        def call(prompt_text: str, max_tokens: int, temperature: float) -> str:
            """Provider-aware section generation (local or API), no hardcoded endpoint."""
            section_messages = [
                {"role": "system", "content": f"You are a concise research assistant. {lang_instruction}"},
                {"role": "user", "content": prompt_text},
            ]

            # LOCAL provider: stream with an IDLE timeout — the model may think
            # as long as tokens keep flowing (a fixed total timeout used to kill
            # legitimate long reasoning AND left the orphaned request occupying
            # the single server slot, starving the retry).
            if (Config.get("provider", "local") or "local").strip().lower() == "local":
                idle = int(Config.get("research_section_idle_timeout_seconds", 75) or 75)
                return self._stream_section_completion(
                    messages=section_messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    idle_timeout=max(15, idle),
                    on_progress=getattr(self, "_section_progress_cb", None),
                )

            # API providers: non-streaming via query_llm with a hard wall-clock guard.
            timeout_sec = int(Config.get("research_section_llm_timeout_seconds", 240) or 240)
            if timeout_sec < 10:
                timeout_sec = 10

            holder: Dict[str, Any] = {"content": "", "error": None}

            def _worker() -> None:
                try:
                    holder["content"] = self.query_llm(
                        messages=section_messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        # Long-form output: chain-of-thought must NEVER be returned
                        # as the section text. If generation gets cut off mid-
                        # reasoning, we get an empty result and the retry/
                        # placeholder path handles it.
                        allow_reasoning_fallback=False,
                    )
                except Exception as e:
                    holder["error"] = e

            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            t.join(timeout=timeout_sec)

            if t.is_alive():
                return ""
            if holder.get("error") is not None:
                return ""
            return str(holder.get("content") or "").strip()

        try:
            # Generous budget (coder philosophy, max_tokens=32768 there): reasoning
            # models may think as long as they need — the answer still fits after.
            # A tight 2200 budget used to cut generation mid-reasoning, leaving no
            # content at all.
            content = call(prompt_text=prompt, max_tokens=8192, temperature=0.2)
            content = self._sanitize_section_output(content, title)
            content = _strip_answer_artifacts(content)
            content = _strip_untrusted_links(content, sources)
            if content:
                return content

            # Retry once with slightly different settings and a shorter web_results payload.
            retry_prompt = prompt.replace(web_results, _truncate(web_results, 2500))
            content = call(prompt_text=retry_prompt, max_tokens=6144, temperature=0.25)
            content = self._sanitize_section_output(content, title)
            content = _strip_answer_artifacts(content)
            content = _strip_untrusted_links(content, sources)
            if content:
                return content

            # Final deterministic fallback: never return an empty section.
            if lang == "de":
                return (
                    f"<h2>{title}</h2>"
                    "<p><em>Hinweis:</em> Für diesen Abschnitt konnte kein sauberer Abschnitt generiert werden; "
                    "hier ist eine kurze, evidenzbasierte Zusammenfassung aus den Suchergebnissen.</p>"
                    "<ul><li>Siehe die Quellenliste am Ende des Reports für Details.</li></ul>"
                )
            return (
                f"<h2>{title}</h2>"
                "<p><em>Note:</em> Could not generate a clean section; here is a short evidence-based placeholder.</p>"
                "<ul><li>See the Sources section at the end of the report for details.</li></ul>"
            )
        except Exception as e:
            return f"<h2>{title}</h2><p><strong>Error:</strong> {type(e).__name__}: {e}</p>"

    def _assemble_html(self, topic: str, sections: Sequence[str], sources: Sequence[str], lang: str, quality_warning: str = "") -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        # Ordered list: the numbers match the [n] citation markers in the sections.
        source_items = "\n".join(f'<li><a href="{u}">{u}</a></li>' for u in sources[:50])
        sections_html = "\n\n".join(sections)
        # Remove any standalone "Answer" artifacts that slipped through.
        sections_html = _strip_answer_artifacts(sections_html)
        return (
            "<!doctype html>\n"
            f"<html lang=\"{lang}\">\n"
            "<head>\n"
            "  <meta charset=\"utf-8\" />\n"
            f"  <title>Research Report: {topic}</title>\n"
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n"
            "  <style>\n"
            "    body{font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;max-width:900px;margin:32px auto;padding:0 16px;line-height:1.5}\n"
            "    h1{margin:0 0 8px 0}\n"
            "    .meta{color:#666;margin:0 0 24px 0}\n"
            "    h2{margin-top:28px;border-top:1px solid #eee;padding-top:18px}\n"
            "    ul{padding-left:20px}\n"
            "    code{background:#f6f6f6;padding:2px 6px;border-radius:6px}\n"
            "  </style>\n"
            "</head>\n"
            "<body>\n"
            f"  <h1>Research Report: {topic}</h1>\n"
            f"  <p class=\"meta\">Generated: {now}</p>\n"
            f"{sections_html}\n"
            "  <h2>Sources</h2>\n"
            "  <ol>\n"
            f"{source_items}\n"
            "  </ol>\n"
            "</body>\n"
            "</html>\n"
        )

    def _format_search_results(self, query: str, results: List[Dict[str, str]], deep: bool = False) -> str:
        """
        Format raw search results (from DDGS) into the same format as web_search output.
        This allows us to filter by trust map and then format consistently.
        """
        if not results:
            return "No results found."
        
        title = "### Web Search Results\n"
        title += f"Query: {query}\n\n"
        
        summary = title
        preview_count = 0
        preview_limit = min(len(results), 10) if deep else 0
        
        # Helper to fetch text (same as in web_search)
        def fetch_text(url):
            try:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
                r = requests.get(url, timeout=4, headers=headers)
                if r.status_code != 200:
                    return None
                html = r.text
                html = re.sub(r'<(script|style|noscript)[^>]*>.*?</\1>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r'<[^>]+>', ' ', html)
                text = re.sub(r'\s+', ' ', text).strip()
                return text[:3000]
            except:
                return None
        
        for i, res in enumerate(results, 1):
            page_title = res.get("title", "").strip()
            link = res.get("href", "") or res.get("link", "").strip()
            snippet = res.get("body", "") or res.get("snippet", "").strip()
            
            summary += f"{i}. **{page_title}**\n"
            if snippet:
                summary += f"   - Snippet: {snippet}\n"
            if link:
                summary += f"   - Source: {link}\n"
            
            if deep and link and preview_count < preview_limit:
                page_text = fetch_text(link)
                if page_text:
                    summary += f"   - Preview: {page_text[:800]}...\n"
                preview_count += 1
            
            summary += "\n"
        
        return summary.strip()

    def _assemble_markdown(self, topic: str, sections: Sequence[str], sources: Sequence[str]) -> str:
        # Sections may be HTML fragments OR already-markdown (see _section_html_to_markdown);
        # both are joined as-is. The numbered Sources list matches the [n] citation markers.
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        out = [f"# Research Report: {topic}", f"_Generated: {now}_", ""]
        out.extend(sections)
        out.append("\n## Sources\n")
        out.extend([f"{i}. {u}" for i, u in enumerate(sources[:50], 1)])
        return "\n".join(out)

    @staticmethod
    def _section_html_to_markdown(html: str) -> str:
        """Convert a research section HTML fragment to clean Markdown so the report can be
        built into a DocumentModel (infer_document_model expects Markdown, not HTML) and
        saved as .docx. Handles the tags the sections actually use; [n] citations are kept."""
        if not html:
            return ""
        import html as _htmllib
        s = html
        # Inline first so block conversions wrap clean text.
        s = re.sub(r'(?is)<span[^>]*class="[^"]*cite[^"]*"[^>]*>(.*?)</span>', r'\1', s)  # keep [n]
        s = re.sub(r'(?is)<(strong|b)\b[^>]*>(.*?)</\1>', r'**\2**', s)
        s = re.sub(r'(?is)<(em|i)\b[^>]*>(.*?)</\1>', r'*\2*', s)
        s = re.sub(r'(?is)<a\b[^>]*>(.*?)</a>', r'\1', s)
        # Block-level -> markdown.
        s = re.sub(r'(?is)<h2\b[^>]*>(.*?)</h2>', lambda m: f"\n## {m.group(1).strip()}\n", s)
        s = re.sub(r'(?is)<h3\b[^>]*>(.*?)</h3>', lambda m: f"\n### {m.group(1).strip()}\n", s)
        s = re.sub(r'(?is)<li\b[^>]*>(.*?)</li>', lambda m: f"- {m.group(1).strip()}\n", s)
        s = re.sub(r'(?is)</?(ul|ol)\b[^>]*>', "\n", s)
        s = re.sub(r'(?is)<p\b[^>]*>(.*?)</p>', lambda m: f"\n{m.group(1).strip()}\n", s)
        s = re.sub(r'(?is)<br\s*/?>', "\n", s)
        s = re.sub(r'(?is)<[^>]+>', "", s)            # strip any remaining tags
        s = _htmllib.unescape(s)
        s = re.sub(r'[ \t]+', ' ', s)
        s = re.sub(r'\n{3,}', '\n\n', s)
        return s.strip()


