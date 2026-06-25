# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Coding Agent - Agentic Loop Implementation

Design Philosophy: Plan-Do-Check-Act:
- NO max_steps - runs until DONE
- Agent decides when task is complete
- Quality checks prevent premature completion
- Templates for deterministic base files
- TUI display showing real-time progress
"""

import requests
import json
import os
import re
import sys
import time
import platform
import threading
import subprocess
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import asdict

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich.syntax import Syntax
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.align import Align

from vaf.cli.tui import AnimatedHeader

from vaf.cli.ui import UI
from vaf.tools.base import BaseTool
from vaf.tools.filesystem import WriteFileTool, ReadFileTool, ListFilesTool, MoveFileTool
from vaf.tools.python_sandbox import PythonSandboxTool
from vaf.tools.coder_templates import TemplateManager
from vaf.core.persistence import PersistenceManager, ProjectState, Task


# ═══════════════════════════════════════════════════════════════════════════════
# Project Directory Safety
# ═══════════════════════════════════════════════════════════════════════════════

def is_unsafe_project_dir(path: str) -> bool:
    """True if `path` must never be used as a project/work directory for agents.

    Agents may only create projects under safe locations (normally
    Documents/VAF_Projects). Unsafe are:
    - the filesystem root and the user's home directory itself
    - the standard user directories themselves (Documents, Desktop, Downloads, ...)
      (subdirectories of them are fine, e.g. Documents/VAF_Projects/...)
    - anything inside the VAF config dir (~/.vaf)
    - anything inside the VAF program/source tree

    Also used by web_server/headless_runner to refuse persisting or re-injecting
    poisoned last_project_path values (self-heal for sessions that recorded
    /home/<user> as a project before this guard existed).
    """
    try:
        p = Path(path).expanduser().resolve()
    except Exception:
        return True

    home = Path.home().resolve()

    # Filesystem root, home itself, or anything above home (e.g. /home, /Users)
    if p == Path(p.anchor) or home.is_relative_to(p):
        return True

    # Standard user dirs themselves (their subdirs are allowed)
    standard_dirs = {
        home / d for d in (
            "Documents", "Desktop", "Downloads", "Pictures",
            "Music", "Videos", "Public", "Templates",
        )
    }
    if p in standard_dirs:
        return True

    # VAF config dir (~/.vaf) and everything inside it
    vaf_cfg = home / ".vaf"
    if p == vaf_cfg or p.is_relative_to(vaf_cfg):
        return True

    # VAF program/source tree and everything inside it
    vaf_root = Path(__file__).resolve().parents[2]
    if p == vaf_root or p.is_relative_to(vaf_root):
        return True

    return False


# ═══════════════════════════════════════════════════════════════════════════════
# Planning Rules (code-enforced)
# ═══════════════════════════════════════════════════════════════════════════════

_SINGLE_FILE_PATTERNS = [
    # German: "einzelne HTML-Datei", "einzigen Datei", "Einzeldatei"
    r'\beinzeln\w*\s+(?:\w+[-\s]+)?datei\b',
    r'\beinzig\w*\s+(?:\w+[-\s]+)?datei\b',
    r'\beinzeldatei\b',
    # English: "single file", "single html file", "single-file", "one single file",
    # "everything/all in one file"
    r'\bsingle[-\s]+(?:\w+[-\s]+)?file\b',
    r'\bone\s+single\s+(?:\w+\s+)?file\b',
    r'\b(?:everything|all)\s+in\s+one\s+(?:\w+\s+)?file\b',
]


def _detect_single_file_deliverable(task: str) -> bool:
    """True if the task explicitly asks for a single-file deliverable.

    Backs the planning rule "single-file deliverable -> exactly 1 task" with a
    code-level check (set_todos enforcement). Deliberately conservative: only
    unambiguous phrasings match — a bare "eine Datei" does not, because it
    appears in many multi-file tasks too.
    """
    task_lower = (task or "").lower()
    return any(re.search(p, task_lower) for p in _SINGLE_FILE_PATTERNS)


def _final_commit(base_dir: str, message: str) -> str:
    """Commit the project state at the end of a coder run (every exit path).

    The model rarely calls the git tools itself, so without this the deliverable
    stays untracked forever (runs used to end with only the 'Initial commit').
    Commits on PARTIAL/FAILED runs too — the message carries the status, and an
    honest snapshot beats losing work. Returns a one-line status for the summary.
    """
    if is_unsafe_project_dir(base_dir):
        return ""
    if not os.path.isdir(os.path.join(base_dir, '.git')):
        return ""

    run_kwargs = {'cwd': base_dir, 'capture_output': True, 'text': True}
    if platform.system() == "Windows":
        run_kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

    try:
        subprocess.run(['git', 'add', '-A'], check=True, **run_kwargs)
        status = subprocess.run(['git', 'status', '--porcelain'], check=True, **run_kwargs)
        if not (status.stdout or "").strip():
            return "Git: nothing new to commit"

        commit = subprocess.run(['git', 'commit', '-m', message], **run_kwargs)
        if commit.returncode != 0:
            err = (commit.stderr or "") + (commit.stdout or "")
            if any(s in err for s in ("user.name", "user.email", "identity")):
                # No git identity configured on this machine: retry with a
                # one-off VAF identity (does not touch the user's git config).
                commit = subprocess.run(
                    ['git', '-c', 'user.name=VAF Coder', '-c', 'user.email=coder@vaf.local',
                     'commit', '-m', message],
                    **run_kwargs,
                )
            if commit.returncode != 0:
                return f"Git commit failed: {(commit.stderr or '').strip()[:120]}"

        short = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], **run_kwargs)
        sha = (short.stdout or "").strip()
        return f"Git: committed final state ({sha})" if sha else "Git: committed final state"
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return f"Git commit skipped: {str(e)[:120]}"


# ═══════════════════════════════════════════════════════════════════════════════
# WebUI coder_state payload builders (file tree + git state)
# ═══════════════════════════════════════════════════════════════════════════════

def _build_file_tree(
    base_dir: str,
    files_created=(),
    current_file: str = "",
    initial_files=(),
) -> List[Dict[str, Any]]:
    """Project file list for the SubAgent window explorer.

    Status per file: "W" = being written right now, "A" = added this run,
    "M" = modified this run (existed at run start), "" = untouched.
    Hidden entries (.git/, .vaf/, dotfiles) are excluded — the explorer shows
    the deliverable, not infrastructure.
    """
    created = {os.path.abspath(f) for f in (files_created or ())}
    initial = set(initial_files or ())
    current_name = os.path.basename(current_file or "")
    tree: List[Dict[str, Any]] = []
    try:
        for entry in sorted(os.listdir(base_dir)):
            if entry.startswith('.'):
                continue
            path = os.path.join(base_dir, entry)
            if not os.path.isfile(path):
                continue
            if entry == current_name:
                status = "W"
            elif os.path.abspath(path) in created:
                status = "M" if entry in initial else "A"
            else:
                status = ""
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            tree.append({"name": entry, "size": size, "status": status})
    except Exception:
        pass
    return tree


def _build_git_state(base_dir: str, max_commits: int = 5) -> Dict[str, Any]:
    """Branch, dirty-count and recent commits for the SubAgent window."""
    state: Dict[str, Any] = {"branch": "", "dirty": 0, "commits": []}
    if not os.path.isdir(os.path.join(base_dir, '.git')):
        return state
    try:
        from vaf.tools.project_git import _run_git
        branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=base_dir)
        state["branch"] = (branch.stdout or "").strip()
        status = _run_git(["status", "--porcelain"], cwd=base_dir)
        state["dirty"] = len([ln for ln in (status.stdout or "").splitlines() if ln.strip()])
        log = _run_git(
            ["log", f"-n{max_commits}", "--date=relative", "--pretty=format:%h|%ad|%s"],
            cwd=base_dir,
        )
        for line in (log.stdout or "").splitlines():
            sha, when, msg = (line.split("|", 2) + ["", ""])[:3]
            if sha:
                state["commits"].append({"sha": sha, "when": when, "msg": msg})
    except Exception:
        pass
    return state


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-Platform Clickable Links
# ═══════════════════════════════════════════════════════════════════════════════

def _get_clickable_path(path: str) -> str:
    """Returns a terminal-clickable file:// URL."""
    abs_path = os.path.abspath(path)
    file_url = Path(abs_path).as_uri()
    return f"[link={file_url}]{abs_path}[/link]"


def _open_folder(path: str) -> bool:
    """Open folder in file manager. OS-independent. Returns True if successful."""
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return False
    
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(abs_path)  # Windows (non-blocking)
        elif system == "Darwin":  # macOS
            subprocess.Popen(["open", abs_path])  # Non-blocking
        else:  # Linux
            subprocess.Popen(["xdg-open", abs_path])  # Non-blocking
        return True
    except Exception:
        return False


def _get_open_instructions(files: list, base_dir: str) -> str:
    """Generate instructions on how to open/run the created files. OS-independent."""
    if not files:
        return ""
    
    instructions = []
    
    # Detect file types
    html_files = [f for f in files if f.lower().endswith('.html')]
    python_files = [f for f in files if f.lower().endswith('.py')]
    js_files = [f for f in files if f.lower().endswith('.js')]
    
    if html_files:
        # Website detected
        main_html = html_files[0] if html_files else None
        if main_html:
            full_path = main_html if os.path.isabs(main_html) else os.path.join(base_dir, main_html)
            file_link = _get_clickable_path(full_path)
            instructions.append(f"🌐 **To view the website:**")
            instructions.append(f"   - Click this link: {file_link}")
            instructions.append(f"   - Or open `{os.path.basename(main_html)}` in your browser")
            instructions.append(f"   - Or double-click the file in the project folder")
    
    if python_files:
        # Python script detected
        main_py = python_files[0] if python_files else None
        if main_py:
            full_path = main_py if os.path.isabs(main_py) else os.path.join(base_dir, main_py)
            file_link = _get_clickable_path(full_path)
            instructions.append(f"🐍 **To run the Python script:**")
            instructions.append(f"   - Click this link: {file_link}")
            instructions.append(f"   - Or run: `python \"{full_path}\"`")
            instructions.append(f"   - Or double-click the file (if Python is associated)")
    
    if not instructions:
        # Generic instructions
        instructions.append(f"📂 **To open files:**")
        instructions.append(f"   - Open the project folder in your file manager")
        instructions.append(f"   - Double-click any file to open it")
    
    return "\n".join(instructions)


# ═══════════════════════════════════════════════════════════════════════════════
# Linter Helper
# ═══════════════════════════════════════════════════════════════════════════════
def _run_linter_for_files(files: List[str], history: List[Dict], local_tools: Dict[str, Any]):
    """
    Run the linter tool for supported file types and record results into history.
    Supported types: .py, .js/.ts/.tsx/.jsx
    """
    linter_tool = local_tools.get("linter") if local_tools else None
    if not linter_tool:
        return
    
    ext_map = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "javascript",
        ".tsx": "javascript",
        ".jsx": "javascript",
    }
    
    for f in files:
        ext = Path(f).suffix.lower()
        file_type = ext_map.get(ext)
        if not file_type:
            continue
        try:
            res = linter_tool.run(path=f, file_type=file_type)
            history.append({
                "role": "system",
                "content": f"Linter result for {os.path.basename(f)} ({file_type}):\n{res}"
            })
        except Exception as e:
            history.append({
                "role": "system",
                "content": f"Linter failed for {os.path.basename(f)}: {e}"
            })


def _format_file_links(files: list, base_dir: str) -> str:
    """Formats files as clickable terminal links."""
    if not files:
        return "- No files created"
    
    lines = []
    for f in files:
        full_path = f if os.path.isabs(f) else os.path.join(base_dir, f)
        try:
            size = os.path.getsize(full_path)
            size_str = f"{size:,} bytes" if size < 1024 else f"{size/1024:.1f} KB"
        except:
            size_str = "?"
        
        link = _get_clickable_path(full_path)
        filename = os.path.basename(f)
        
        # Add icon based on file type
        ext = os.path.splitext(filename)[1].lower()
        icon = "🌐" if ext == ".html" else "🐍" if ext == ".py" else "📜" if ext == ".js" else "📄"
        
        lines.append(f"- {icon} {filename} ({size_str}) → {link}")
    
    return "\n".join(lines)


# Import optional tools
try:
    from vaf.tools.bash import BashTool
    from vaf.tools.codesearch import CodeSearchTool
    HAS_CODING_TOOLS = True
except ImportError:
    HAS_CODING_TOOLS = False


# ═══════════════════════════════════════════════════════════════════════════════
# TUI Display - Mini-IDE Style
# ═══════════════════════════════════════════════════════════════════════════════

class _NoopLive:
    """Drop-in replacement for Rich's Live when running in workflow/simple mode.
    All method calls are silently ignored so code that calls live.update() /
    live.stop() doesn't need to be guarded with 'if live is not None'."""
    def start(self): pass
    def stop(self): pass
    def update(self, *args, **kwargs): pass
    def refresh(self, *args, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass


class CoderTUI:
    """
    Terminal UI for the Coder Agent.
    Shows real-time progress like a mini-IDE with live streaming output.
    """
    
    # Number of lines to show in live stream (scrolling view)
    STREAM_LINES = 10
    
    def __init__(self, console: Console, task: str, task_mgr=None, animate: bool = True, simple_mode: bool = False):
        # simple_mode=True: no Rich Live display; just print progress lines.
        # Used when the coding agent runs inside a workflow terminal so the
        # workflow's display is not replaced by the full-screen coder TUI.
        self.simple_mode = simple_mode
        self.console = console
        self.task = task
        self.files: Dict[str, Dict] = {}  # filename -> {status, size, preview}
        self.current_action = "Initializing..."
        self.actions_log: List[str] = []
        self.start_time = time.time()
        self.loop_count = 0
        self._lock = threading.RLock()  # RLock allows reentrant locking (render -> get_stream_text)
        self.task_mgr = task_mgr  # TaskManager instance for TODO display
        
        # Live stream buffer (Cursor-style)
        self.stream_buffer: List[str] = []
        self.stream_active = False
        self.current_stream = ""
        self.current_line_buffer = ""  # Current incomplete line being built
        
        # Create header based on animation preference
        if animate:
            self._header = AnimatedHeader("Collaboration Mode Active", "Main Agt", "Coder")
        else:
            from vaf.cli.tui import _StaticHeader
            self._header = _StaticHeader("Collaboration Mode Active", "Main Agt", "Coder")
            
        self._live_started = False  # Track if Live has been started
        self._header_visible = False  # Track if header has been shown (sticky - once shown, stays visible)
        
        # Store Live and animation_running so they can be stopped from outside
        self._live = None  # Will be set when Live is created
        self._animation_running = None  # Will be set when animation thread starts
        self._live_update_callback = None  # Callback to trigger live updates
        self._last_update_time = 0  # Throttle updates to prevent deadlocks
        self._update_throttle_ms = 50  # Minimum 50ms between updates (20 FPS max)
        self._needs_update = False  # Flag to signal that update is needed
        
        # Last meaningful status update time (for static display)
        self.last_status_update_time = time.time()
        
        # Active Code Preview (for showing what's being written)
        self.active_code_preview = None  # {filename, content, language}
        
        # Stream Code Detection
        self._code_buffer = ""
        self._in_code_block = False
        self._code_lang = "text"

    def _touch(self):
        """Update last status change time."""
        self.last_status_update_time = time.time()

    def set_code_preview(self, filename: str, content: str, language: str = "python"):
        """Set the code preview to show at the top."""
        with self._lock:
            self._touch()
            # Auto-detect language from extension if generic
            if language == "code":
                ext = os.path.splitext(filename)[1].lower()
                if ext in ['.py']: language = "python"
                elif ext in ['.js', '.ts']: language = "javascript"
                elif ext in ['.html']: language = "html"
                elif ext in ['.css']: language = "css"
                elif ext in ['.json']: language = "json"
                elif ext in ['.md']: language = "markdown"
                elif ext in ['.sh', '.bash']: language = "bash"
                
            self.active_code_preview = {
                "filename": filename,
                "content": content,
                "language": language,
                "timestamp": time.time()
            }
    
    def clear_code_preview(self):
        """Clear the code preview."""
        with self._lock:
            self._touch()
            self.active_code_preview = None
    
    def add_file(self, filename: str, size: int = 0, status: str = "creating"):
        """Add a file to the display."""
        with self._lock:
            self._touch()
            self.files[filename] = {
                "status": status,
                "size": size,
                "preview": ""
            }
    
    def update_file(self, filename: str, status: str = None, size: int = None, preview: str = None):
        """Update file info."""
        if self.simple_mode and status == "done":
            size_str = f" ({size:,} bytes)" if size else ""
            print(f"[Coder] ✅ Written: {filename}{size_str}", flush=True)
            return
        with self._lock:
            self._touch()
            if filename not in self.files:
                self.files[filename] = {"status": "unknown", "size": 0, "preview": ""}
            if status:
                self.files[filename]["status"] = status
            if size is not None:
                self.files[filename]["size"] = size
            if preview:
                self.files[filename]["preview"] = preview[:200]
    
    def set_action(self, action: str):
        """Set current action."""
        with self._lock:
            self._touch()
            self.current_action = action
            self.actions_log.append(f"{time.strftime('%H:%M:%S')} {action}")
            if len(self.actions_log) > 10:
                self.actions_log.pop(0)
    
    def increment_loop(self):
        """Increment loop counter."""
        self._touch()
        self.loop_count += 1
    
    # ═══════════════════════════════════════════════════════════════════
    # STREAMING METHODS (Cursor-style live output)
    # ═══════════════════════════════════════════════════════════════════
    
    def start_stream(self):
        """Start a new stream (agent is generating)."""
        with self._lock:
            self._touch()
            self.stream_active = True
            self.current_stream = ""
            # Clear old preview to avoid confusion
            self.active_code_preview = None
    
    def append_stream(self, chunk: str):
        """Append text to the current stream (each chunk is a new line)."""
        # In simple_mode (workflow terminal), just print directly without buffering
        if self.simple_mode:
            chunk = re.sub(r'</?redacted_reasoning>', '', chunk, flags=re.IGNORECASE)
            chunk = re.sub(r'</?think>', '', chunk, flags=re.IGNORECASE)
            if chunk.strip():
                print(f"[Coder] {chunk.strip()}", flush=True)
            return
        with self._lock:
            # Filter out redacted reasoning tags
            chunk = re.sub(r'</?redacted_reasoning>', '', chunk, flags=re.IGNORECASE)
            chunk = re.sub(r'</?think>', '', chunk, flags=re.IGNORECASE)
            
            # Each chunk becomes a separate line
            if self.current_stream and not self.current_stream.endswith('\n'):
                self.current_stream += '\n'
            self.current_stream += chunk
            
            # ALWAYS add to buffer - no conditions, ensures content stays visible
            # Add chunk directly to buffer (even if empty, for spacing)
            self.stream_buffer.append(chunk)
            # Keep only last N*2 lines to prevent memory issues
            if len(self.stream_buffer) > self.STREAM_LINES * 2:
                self.stream_buffer = self.stream_buffer[-self.STREAM_LINES * 2:]
            
            # Remove trailing empty lines
            while len(self.stream_buffer) > 1 and not self.stream_buffer[-1].strip():
                self.stream_buffer.pop()
            # Keep only last N*2 lines to prevent memory issues
            if len(self.stream_buffer) > self.STREAM_LINES * 2:
                self.stream_buffer = self.stream_buffer[-self.STREAM_LINES * 2:]
            
            # Remove trailing empty lines (but keep at least one line if buffer is not empty)
            while len(self.stream_buffer) > 1 and not self.stream_buffer[-1].strip():
                self.stream_buffer.pop()
        
        # CRITICAL: Set flag for animation thread to update
        # DON'T call render() here - it would cause deadlock (we're inside lock)
        # Instead, set a flag that animation thread will check
        current_time = time.time() * 1000  # Convert to milliseconds
        time_since_last_update = current_time - self._last_update_time
        
        # Only set flag if throttle time has passed
        if time_since_last_update >= self._update_throttle_ms:
            self._last_update_time = current_time
            self._needs_update = True  # Signal animation thread to update
    
    def end_stream(self):
        """End the current stream."""
        with self._lock:
            self._touch()
            self.stream_active = False
    
    def clear_stream(self):
        """Clear the stream buffer for fresh output."""
        with self._lock:
            self._touch()
            self.stream_buffer = []
            self.current_stream = ""
            self.current_line_buffer = ""
    
    def get_stream_text(self) -> str:
        """Get the current stream text for display."""
        with self._lock:
            if not self.stream_buffer and not self.current_stream:
                if self.stream_active:
                    return "[yellow]Waiting for model response...[/yellow]"
                return "[dim]Ready[/dim]"
            
            # Use current_stream if buffer is empty
            text_to_show = self.stream_buffer if self.stream_buffer else [self.current_stream[-200:] if self.current_stream else ""]
            
            # Format lines with syntax-like highlighting
            formatted = []
            for line in text_to_show:
                if not line:
                    continue
                # Truncate long lines
                if len(line) > 70:
                    line = line[:67] + "..."
                
                # Basic syntax highlighting hints
                if line.strip().startswith('```'):
                    line = f"[cyan]{line}[/cyan]"
                elif line.strip().startswith('#'):
                    line = f"[yellow]{line}[/yellow]"
                elif 'write_file' in line or 'read_file' in line:
                    line = f"[green]{line}[/green]"
                elif line.strip().startswith('//') or line.strip().startswith('/*'):
                    line = f"[dim]{line}[/dim]"
                else:
                    line = f"[white]{line}[/white]"
                
                formatted.append(line)
            
            return "\n".join(formatted) if formatted else "[dim]Processing...[/dim]"
    
    def get_code_preview(self, content: str, ext: str = "html") -> str:
        """Get a code preview for display."""
        lines = content.split('\n')[:8]  # First 8 lines
        preview = '\n'.join(lines)
        if len(content.split('\n')) > 8:
            preview += "\n..."
        return preview
    
    def __rich__(self) -> Group:
        """Allow Rich to render this object directly in Live display."""
        return self.render()

    def render(self) -> Group:
        """Render the TUI as a Rich Panel - ALL IN ONE WINDOW."""
        with self._lock:
            # Determine time display based on header type
            if hasattr(self._header, 'frame_idx'): # AnimatedHeader
                elapsed = int(time.time() - self.start_time)
                time_str = f"Time: {elapsed//60}:{elapsed%60:02d}"
            else: # _StaticHeader
                update_time_str = time.strftime("%H:%M:%S", time.localtime(self.last_status_update_time))
                time_str = f"Last Update: {update_time_str}"
            
            # Get terminal width - platform-independent
            # Works on Windows, macOS, and Linux
            try:
                TERM_WIDTH = self.console.width
                # Use full width minus small margins for borders
                WIDTH = TERM_WIDTH - 2  # Minimal margin for borders
                if WIDTH < 60:
                    WIDTH = 60
                # Max width for readability (platform-independent)
                if WIDTH > 120:
                    WIDTH = 120
            except (AttributeError, OSError):
                # Fallback for all platforms if console.width fails
                TERM_WIDTH = 120
                WIDTH = 118
            
            # ═══════════════════════════════════════════════════════════════
            # AUTO-DETECT CODE IN STREAM (Robust)
            # ═══════════════════════════════════════════════════════════════
            
            # Reconstruct full stream text from buffer to check for code blocks
            full_text = "\n".join(self.stream_buffer)
            
            # Find the last unclosed code block
            # We look for the last ``` that doesn't have a closing ``` after it
            matches = list(re.finditer(r'```(\w*)', full_text))
            
            if matches and len(matches) % 2 == 1:
                # We are inside a code block!
                last_match = matches[-1]
                start_idx = last_match.end()
                language = last_match.group(1) or "text"

                code_content = full_text[start_idx:].strip()

                # Only show if substantial content
                if len(code_content) > 20:
                    self.active_code_preview = {
                        "filename": "Generating...",
                        "content": code_content,  # Will auto-scroll if > 40 lines
                        "language": language,
                        "timestamp": time.time()  # Always fresh
                    }
            elif self.stream_active and len(full_text) > 50:
                # Heuristic fallback: Check for code patterns without markdown
                lines = full_text.split('\n')
                # Look at last few lines
                recent_lines = lines[-10:]
                code_score = 0
                detected_lang = "text"
                
                for line in recent_lines:
                    l = line.strip()
                    if l.startswith(("import ", "def ", "class ", "from ", "return ", "print(", "async ")):
                        code_score += 1
                        detected_lang = "python"
                    elif l.startswith(("<html", "<!DOCTYPE", "<body", "<div", "<script", "<head", "<style")):
                        code_score += 1
                        detected_lang = "html"
                    elif "function" in l or "const " in l or "let " in l or "var " in l or "=>" in l:
                        code_score += 1
                        detected_lang = "javascript"
                
                if code_score >= 2:
                    # Heuristic match! Show as code preview
                    # Show recent part (auto-scroll to end)
                    recent_text = full_text[-2000:]  # Capture more context for scrolling
                    self.active_code_preview = {
                        "filename": "Generating (detected)...",
                        "content": recent_text,
                        "language": detected_lang,
                        "timestamp": time.time()
                    }
            
            # ═══════════════════════════════════════════════════════════════
            # CODE PREVIEW - Show what's being written/read (Topmost)
            # ═══════════════════════════════════════════════════════════════
            
            preview_panel = None
            if self.active_code_preview:
                # Expire preview after 10 seconds to keep UI clean
                if time.time() - self.active_code_preview["timestamp"] > 10:
                    self.active_code_preview = None
                else:
                    content = self.active_code_preview["content"]
                    lines = content.split('\n')

                    # AUTO-SCROLL: Show last 25 lines for live generation view
                    # This keeps the view focused on what's currently being generated
                    MAX_VISIBLE_LINES = 25

                    if len(lines) > MAX_VISIBLE_LINES:
                        # Show last N lines (auto-scroll to bottom)
                        start_line = len(lines) - MAX_VISIBLE_LINES + 1
                        skipped_lines = len(lines) - MAX_VISIBLE_LINES
                        display_content = f"... ({skipped_lines} lines above)\n" + '\n'.join(lines[-MAX_VISIBLE_LINES:])
                    else:
                        start_line = 1
                        display_content = content
                    
                    syntax = Syntax(
                        display_content,
                        self.active_code_preview["language"],
                        theme="monokai",
                        line_numbers=True,
                        start_line=start_line,
                        word_wrap=True
                    )

                    # Show auto-scroll indicator in title if content is scrolled
                    title_text = f"📝 Editing: {self.active_code_preview['filename']}"
                    if len(lines) > MAX_VISIBLE_LINES:
                        # Add scroll indicator to show we're auto-scrolling
                        title_text += f" [dim](showing last {MAX_VISIBLE_LINES} lines)[/dim]"

                    preview_panel = Panel(
                        syntax,
                        title=f"[bold yellow]{title_text}[/bold yellow]",
                        border_style="yellow",
                        padding=(1, 2),
                        width=WIDTH
                    )

            # ═══════════════════════════════════════════════════════════════
            # HEADER - Render fresh for animation
            # ═══════════════════════════════════════════════════════════════
            
            # Always render header when active
            header = self._header.__rich__()
            
            # ═══════════════════════════════════════════════════════════════
            # STATUS LINE (fixed - no markup in strings)
            # ═══════════════════════════════════════════════════════════════
            
            indicator = "*" if self.stream_active else "o"
            indicator_style = "green" if self.stream_active else "yellow"
            
            # Clean current_action from any markup
            clean_action = self.current_action.replace("[yellow]", "").replace("[/yellow]", "")
            clean_action = clean_action.replace("[green]", "").replace("[/green]", "")
            clean_action = clean_action.replace("[white]", "").replace("[/white]", "")
            clean_action = clean_action.replace("[dim]", "").replace("[/dim]", "")
            
            status = Text()
            status.append(time_str, style="dim")
            status.append("  │  ", style="dim")
            status.append(f"Loop: {self.loop_count}", style="dim")
            status.append("  │  ", style="dim")
            status.append(indicator, style=indicator_style)
            status.append(f" {clean_action}", style="white")
            
            # ═══════════════════════════════════════════════════════════════
            # FILES TABLE
            # ═══════════════════════════════════════════════════════════════
            
            files_table = Table(show_header=False, box=None, padding=(0, 1), width=WIDTH-2)
            files_table.add_column("", width=2, no_wrap=True)  # Icon column - Emoji + space
            files_table.add_column("File", style="cyan", width=25, no_wrap=True)
            files_table.add_column("Size", justify="right", style="dim", width=12, no_wrap=True)
            files_table.add_column("Status", width=10, no_wrap=True)
            
            icons = {"creating": "[", "writing": "|", "done": "]", "error": "X"}
            
            for fname, info in self.files.items():
                icon = icons.get(info["status"], "•")
                size_str = f"{info['size']:,}B" if info["size"] else "-"
                # Add space after icon for compact spacing
                files_table.add_row(f"{icon} ", fname[:24], size_str, info["status"])
            
            if not self.files:
                files_table.add_row("", "[dim]Waiting for files...[/dim]", "", "")
            
            # ═══════════════════════════════════════════════════════════════
            # TODO LIST - Show progress above output
            # ═══════════════════════════════════════════════════════════════
            
            todo_section = None
            # Always show TODO section if task_mgr exists, even if empty (to show planning status)
            if self.task_mgr:
                if self.task_mgr.todos:
                    # Create TODO display with spinner for current task
                    todo_lines = []
                    completed_count = len([t for t in self.task_mgr.todos if t["status"] == "completed"])
                    total_count = len(self.task_mgr.todos)
                    
                    # Spinner animation (rotating characters) - faster animation
                    spinner_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
                    spinner_idx = int(time.time() * 12) % len(spinner_chars)  # Increased from 8 to 12 FPS
                    spinner = spinner_chars[spinner_idx]
                    
                    for i, todo in enumerate(self.task_mgr.todos):
                        if todo["status"] == "completed":
                            icon = "*"
                            style = "green"
                        elif i == self.task_mgr.current_task_idx:
                            icon = spinner  # Animated spinner for current task
                            style = "yellow"
                        else:
                            icon = "o"
                            style = "dim"
                        
                        # Truncate long task names
                        task_text = todo["task"]
                        if len(task_text) > 50:
                            task_text = task_text[:47] + "..."
                        
                        todo_lines.append((icon, task_text, style))
                    
                    # Create TODO text
                    todo_text = Text()
                    todo_text.append(f"Tasks: {completed_count}/{total_count} ", style="cyan")
                    if todo_lines:
                        todo_text.append("\n")
                    for i, (icon, task_text, style) in enumerate(todo_lines):
                        todo_text.append(f"{icon} {task_text}", style=style)
                        if i < len(todo_lines) - 1:
                            todo_text.append("\n")
                    
                    todo_section = Panel(
                        todo_text,
                        title="[bold cyan]TODO Progress[/bold cyan]",
                        border_style="cyan",
                        padding=(0, 1),
                        width=WIDTH
                    )
                else:
                    # Show planning status if no TODOs set yet
                    planning_text = Text()
                    planning_text.append("Planning tasks...", style="dim yellow")
                    todo_section = Panel(
                        planning_text,
                        title="[bold cyan]TODO Progress[/bold cyan]",
                        border_style="cyan",
                        padding=(0, 1),
                        width=WIDTH
                    )
            
            # ═══════════════════════════════════════════════════════════════
            # OUTPUT STREAM - Scrolling view (newest at bottom)
            # ═══════════════════════════════════════════════════════════════
            
            # Use full width for output (minus padding for borders)
            max_line_len = WIDTH - 6  # Account for panel borders and padding
            
            # Join all buffer content and wrap into display lines
            all_text = ""
            for line in self.stream_buffer:
                # Clean markup
                clean = line.replace("[white]", "").replace("[/white]", "")
                clean = clean.replace("[dim]", "").replace("[/dim]", "")
                clean = clean.replace("[yellow]", "").replace("[/yellow]", "")
                clean = clean.replace("[green]", "").replace("[/green]", "")
                all_text += clean + "\n"
            
            # Add current incomplete line if stream is active (for live typing effect)
            # This shows text as it's being typed, even before a newline
            if self.stream_active:
                # Always show current_line_buffer if stream is active (even if empty)
                if self.current_line_buffer:
                    # Clean markup from incomplete line
                    clean_incomplete = self.current_line_buffer.replace("[white]", "").replace("[/white]", "")
                    clean_incomplete = clean_incomplete.replace("[dim]", "").replace("[/dim]", "")
                    clean_incomplete = clean_incomplete.replace("[yellow]", "").replace("[/yellow]", "")
                    clean_incomplete = clean_incomplete.replace("[green]", "").replace("[/green]", "")
                    # Add incomplete line (even if it's just whitespace, to show typing)
                    all_text += clean_incomplete + "\n"
            
            # CRITICAL: Show separator or stream status even if buffer seems empty
            # This ensures user sees that something is happening
            if not all_text.strip():
                if self.stream_active:
                    all_text = "[yellow]Waiting for model response...[/yellow]\n"
                else:
                    all_text = "[dim]Ready[/dim]\n"
            
            # Wrap into fixed-width lines (like terminal)
            wrapped_lines = []
            for line in all_text.split("\n"):
                if not line:
                    wrapped_lines.append("")
                elif len(line) <= max_line_len:
                    wrapped_lines.append(line)
                else:
                    # Wrap long line into multiple lines
                    for i in range(0, len(line), max_line_len):
                        wrapped_lines.append(line[i:i+max_line_len])
            
            # Take LAST lines (scroll view - newest at bottom)
            display_lines = wrapped_lines[-self.STREAM_LINES:]
            
            # Build output text with colors
            output_text = Text()
            # Filter out empty lines at the end
            filtered_lines = []
            for line in display_lines:
                # SKIP: Do not filter out reasoning tags - show them dimmed instead!
                # if "</think>" in line or "<think>" in line:
                #    continue
                
                # Skip empty lines at the end
                if not line.strip() and not filtered_lines:
                    continue
                filtered_lines.append(line)
            
            # Remove trailing empty lines
            while filtered_lines and not filtered_lines[-1].strip():
                filtered_lines.pop()
            
            for i, line in enumerate(filtered_lines):
                # Determine style
                if "<think>" in line or "</think>" in line:
                    style = "dim"
                elif "[OK]" in line or "done" in line.lower() or "✅" in line:
                    style = "green"
                elif "[ERROR]" in line or "[X]" in line or "error" in line.lower() or "❌" in line:
                    style = "red"
                elif "[TOOL]" in line or "Calling" in line or "🔧" in line:
                    style = "yellow"
                elif line.strip().startswith("<") or line.strip().startswith("{"):
                    style = "dim cyan"
                else:
                    style = "white"
                
                output_text.append(line, style=style)
                if i < len(filtered_lines) - 1:
                    output_text.append("\n")
            
            # ═══════════════════════════════════════════════════════════════
            # COMPOSE PANEL - Use AnimatedHeader like Librarian
            # ═══════════════════════════════════════════════════════════════
            
            # Header is already set above - use it here
            # Main content panel (without outer border, header has its own)
            main_content_items = [
                Text(""),  # Spacer after header
                # Show full task without truncation - wrap if needed
                Text(f"Task: {self.task}", style="bold"),
                status,
                Text("─" * (WIDTH-2), style="dim cyan"),  # Full width separator
                files_table,
            ]
            
            # Add TODO section if available
            if todo_section:
                main_content_items.append(Text("─" * (WIDTH-2), style="dim cyan"))  # Separator
                main_content_items.append(todo_section)
            
            main_content_items.append(Text("─" * (WIDTH-2), style="dim cyan"))  # Full width separator
            main_content_items.append(Panel(output_text, title="[bold cyan]Coding Agent[/bold cyan]", border_style="dim cyan", width=WIDTH, padding=(0, 1)))  # Full width
            
            main_content = Group(*main_content_items)
            
            # ═══════════════════════════════════════════════════════════════
            # FOOTER - Fake Input Box (Sticky at Bottom)
            # ═══════════════════════════════════════════════════════════════
            
            # Get terminal height to push footer to bottom
            try:
                term_height = self.console.height
            except:
                term_height = 24
            
            # Calculate border parts
            label = " Message (Agent working) "
            label_len = len(label)
            left_border = "─" * 2
            right_border = "─" * (WIDTH - label_len - 4)
            
            # Use theme color from header for consistency
            color = self.active_code_preview["language"] if self.active_code_preview else "cyan"
            # Fallback if color is not a valid rich color
            if color not in ["cyan", "yellow", "green", "magenta", "blue", "red", "white"]:
                color = "cyan"
                
            footer_header = Text.from_markup(f"╭{left_border}[bold {color}]{label}[/]{right_border}╮", style="dim")
            footer_prompt = Text.from_markup(f"[bold {color}]❯[/] ...", style="dim")
            
            # Combine header and main content
            group_items = []
            if preview_panel:
                group_items.append(preview_panel)
            
            if header:
                group_items.append(header)
            
            group_items.append(main_content)
            
            return Group(*group_items)


# ═══════════════════════════════════════════════════════════════════════════════
# Agentic Loop Controller
# ═══════════════════════════════════════════════════════════════════════════════

class AgenticLoop:
    """
    Controls the agentic execution loop.
    
    NO max_steps - runs until:
    1. Agent signals completion AND quality checks pass
    2. Critical failure
    3. Timeout (safety, configurable)
    
    Distinguishes between:
    - Thinking (normal, wait)
    - Context full (need summary/restart)  
    - Stuck/Loop (intervention needed)
    """
    
    def __init__(
        self,
        timeout_minutes: int = 15,  # Safety timeout only
        idle_threshold_seconds: int = 60,
        max_consecutive_empty: int = 5
    ):
        self.timeout_minutes = timeout_minutes
        self.idle_threshold = idle_threshold_seconds
        self.max_empty = max_consecutive_empty
        
        self.start_time = time.time()
        self.last_activity = time.time()
        self.consecutive_empty = 0
        self.recent_actions: List[str] = []
        self.loop_count = 0
        self.state = "working"  # working, thinking, stuck, context_full
        self.thinking_since = None
        self.last_token_count = 0
        self.same_action_count = 0
        self.idle_loop_count = 0 # Track loops without tool calls
    
    def record_activity(self):
        """Record agent activity."""
        self.last_activity = time.time()
        self.consecutive_empty = 0
        self.state = "working"
        self.thinking_since = None
    
    def record_empty(self):
        """Record empty response."""
        self.consecutive_empty += 1
    
    def record_thinking(self):
        """Record that model is thinking (long response time)."""
        if self.state != "thinking":
            self.state = "thinking"
            self.thinking_since = time.time()
    
    def record_action(self, action: str):
        """Record an action for doom loop detection."""
        # Check if same action repeated
        if self.recent_actions and self.recent_actions[-1] == action:
            self.same_action_count += 1
        else:
            self.same_action_count = 0
        
        self.recent_actions.append(action)
        if len(self.recent_actions) > 10:  # Keep more history
            self.recent_actions.pop(0)
        self.record_activity()
    
    def increment_loop(self):
        """Increment loop counter."""
        self.loop_count += 1
    
    def should_continue(self) -> tuple[bool, str]:
        """
        Check if loop should continue.
        Returns: (should_continue, reason_if_not)
        
        IMPORTANT: Timeout only triggers if model is IDLE (not responding),
        NOT if model is actively working. This allows long-running tasks.
        """
        # Check if model is idle (no activity for a while)
        idle_time = self.get_idle_time()
        idle_minutes = idle_time / 60
        
        # Safety timeout ONLY if model is idle (not responding)
        # If model is still active, allow it to continue working
        if idle_time > (self.idle_threshold * 2):  # 2x idle threshold = really stuck
            elapsed = (time.time() - self.start_time) / 60
            if elapsed > self.timeout_minutes:
                return False, f"Safety timeout: No activity for {int(idle_minutes)} min (total: {int(elapsed)} min)"
        
        # Also check for long idle time (2+ minutes without any response)
        # This catches cases where model completely stopped responding
        # Reduced from 5 minutes to 2 minutes for faster failure detection
        if idle_minutes > 2:
            return False, f"Model stopped responding (idle for {int(idle_minutes)} min)"
        
        # NOTE: Empty response handling is now done via context reset in the main loop
        # No need to stop the loop here - it will reset context and continue
        # This allows the loop to continue until we get a response
        
        return True, ""
    
    def is_idle(self) -> bool:
        """Check if agent is idle."""
        return (time.time() - self.last_activity) > self.idle_threshold
    
    def get_idle_time(self) -> int:
        """Get seconds since last activity."""
        return int(time.time() - self.last_activity)
    
    def detect_doom_loop(self) -> bool:
        """Detect if stuck in a loop (same action 3+ times)."""
        if len(self.recent_actions) < 3:
            return False
        # Same exact action 3 times in a row
        if self.same_action_count >= 2:  # 3rd time = stuck
            return True
        # Ping-pong pattern (A-B-A-B)
        if len(self.recent_actions) >= 4:
            last4 = self.recent_actions[-4:]
            if last4[0] == last4[2] and last4[1] == last4[3] and last4[0] != last4[1]:
                return True
        return False
    
    def get_state_display(self) -> str:
        """Get display string for current state."""
        if self.state == "thinking":
            think_time = int(time.time() - self.thinking_since) if self.thinking_since else 0
            return f"🤔 Thinking ({think_time}s)"
        elif self.state == "stuck":
            return "⚠️ Stuck - trying to recover"
        elif self.state == "context_full":
            return "📝 Context full - summarizing"
        else:
            return f"⚡ Working (loop {self.loop_count})"
    
    def get_elapsed_str(self) -> str:
        """Get formatted elapsed time."""
        elapsed = int(time.time() - self.start_time)
        return f"{elapsed//60}:{elapsed%60:02d}"


# ═══════════════════════════════════════════════════════════════════════════════
# Task Manager - Forces structured TODO workflow
# ═══════════════════════════════════════════════════════════════════════════════

class TaskManager:
    """
    Manages a persistent TODO list that the agent MUST work through.
    Uses PersistenceManager (tasks.json) to survive restarts.
    """
    
    def __init__(self, base_dir: Optional[str] = None):
        self.pm: Optional[PersistenceManager] = None
        self.state: Optional[ProjectState] = None
        if base_dir:
            self.initialize(base_dir)
            
    def initialize(self, base_dir: str):
        """Initialize persistence with the project directory."""
        self.pm = PersistenceManager(base_dir)
        project_name = os.path.basename(base_dir)
        # Load existing state or create new
        loaded_state = self.pm.load_state()
        if loaded_state:
            # If ALL tasks were already completed, this is a NEW invocation (not a
            # crash-resume).  Discard the stale state so the agent plans fresh.
            all_done = (
                bool(loaded_state.tasks)
                and all(t.status == "completed" for t in loaded_state.tasks)
            )
            if all_done:
                # init_project() only creates the file if it doesn't exist, so we
                # must explicitly overwrite the stale state with a fresh ProjectState.
                fresh = ProjectState(project_name=project_name)
                self.pm.save_state(fresh)
                loaded_state = self.pm.load_state()
            self.state = loaded_state
        else:
            # Create fresh state (project name from dir name)
            self.pm.init_project(project_name)
            self.state = self.pm.load_state() # Reload to get clean state
            # FALLBACK: if disk write failed silently, create in-memory state
            # so that set_todos() and write_file are never blocked by state=None
            if not self.state:
                print(f"[TaskManager] WARNING: PersistenceManager.save_state() failed for {base_dir}; using in-memory ProjectState")
                self.state = ProjectState(project_name=project_name)
    
    @property
    def todos(self) -> List[Dict]:
        """Backward compatibility for todos list."""
        if not self.state:
            return []
        # Convert Task objects to dicts for legacy compatibility
        # MAP 'title' -> 'task' to fix KeyError in legacy code
        todos_list = []
        for t in self.state.tasks:
            d = asdict(t)
            d['task'] = t.title # Legacy field name
            todos_list.append(d)
        return todos_list
        
    @property
    def current_task_idx(self) -> int:
        return self.state.current_task_idx if self.state else 0
        
    @current_task_idx.setter
    def current_task_idx(self, value: int):
        if self.state:
            self.state.current_task_idx = value
            if self.pm: self.pm.save_state(self.state)

    def set_todos(self, todos: List[str]):
        """Set the TODO list (from agent's planning phase)."""
        if not self.state or not self.pm:
            return
            
        new_tasks = []
        for i, t_str in enumerate(todos):
            new_tasks.append(Task(id=i+1, title=t_str, status="pending"))
            
        self.state.tasks = new_tasks
        self.state.current_task_idx = 0
        self.pm.save_state(self.state)
    
    def get_current_task(self) -> Optional[str]:
        """Get the current task to work on."""
        if not self.state or self.state.current_task_idx >= len(self.state.tasks):
            return None
        return self.state.tasks[self.state.current_task_idx].title
    
    def complete_current_task(self, result: str = "done"):
        """Mark current task as complete and move to next."""
        if not self.state or not self.pm:
            return

        if self.state.current_task_idx < len(self.state.tasks):
            task = self.state.tasks[self.state.current_task_idx]
            task.status = "completed"
            task.result = result
            task.completed_at = time.time()
            self.state.current_task_idx += 1
            self._advance_to_next_actionable()
            self.pm.save_state(self.state)

    def fail_current_task(self, reason: str):
        """Mark current task as failed (honest terminal state) and move to next.

        Used when stuck detection fires and the task goal could not be verified.
        A failed task is never reported as completed; the final summary lists it.
        """
        if not self.state or not self.pm:
            return

        if self.state.current_task_idx < len(self.state.tasks):
            task = self.state.tasks[self.state.current_task_idx]
            task.status = "failed"
            task.result = reason
            task.completed_at = time.time()
            self.state.current_task_idx += 1
            self._advance_to_next_actionable()
            self.pm.save_state(self.state)

    def _advance_to_next_actionable(self):
        """Skip terminal tasks so retried runs never re-enter completed work.

        No-op in the normal sequential flow; relevant after reset_task_for_retry,
        where completed tasks may sit between pending retry candidates.
        """
        if not self.state:
            return
        i = self.state.current_task_idx
        while i < len(self.state.tasks) and self.state.tasks[i].status in self._TERMINAL_STATUSES:
            i += 1
        self.state.current_task_idx = i

    def reset_task_for_retry(self, idx: int, note: str = ""):
        """Put a task back to pending for a retry attempt.

        The previous failure reason is archived in `description` so the retry
        context can inject it as a hint.
        """
        if not self.state or not self.pm:
            return
        if 0 <= idx < len(self.state.tasks):
            task = self.state.tasks[idx]
            prev = note or task.result or ""
            if prev:
                task.description = f"Previous attempt: {prev}"[:500]
            task.status = "pending"
            task.result = None
            task.completed_at = None
            if idx < self.state.current_task_idx:
                self.state.current_task_idx = idx
            self.pm.save_state(self.state)

    def failed_tasks(self) -> List[Dict]:
        """All tasks in status 'failed' (legacy dict format, like .todos)."""
        return [t for t in self.todos if t["status"] == "failed"]
    
    def get_progress(self) -> str:
        """Get progress string for display."""
        if not self.state or not self.state.tasks:
            return "Planning..."
        done = len([t for t in self.state.tasks if t.status == "completed"])
        return f"Task {done}/{len(self.state.tasks)}"
    
    def get_todos_for_prompt(self) -> str:
        """Get formatted TODO list for prompt injection."""
        if not self.state or not self.state.tasks:
            return ""
        
        lines = ["## CURRENT TODO LIST:"]
        for i, task in enumerate(self.state.tasks):
            status = "✅" if task.status == "completed" else "⏳" if i == self.state.current_task_idx else "○"
            marker = ">>> " if i == self.state.current_task_idx else "    "
            lines.append(f"{marker}{status} {i+1}. {task.title}")
        
        if self.state.current_task_idx < len(self.state.tasks):
            lines.append(f"\n## CURRENT TASK: {self.get_current_task()}")
            lines.append("Complete THIS task, then call `task_done` tool.")
        
        return "\n".join(lines)
    
    _TERMINAL_STATUSES = ("completed", "failed", "skipped")

    def is_all_done(self) -> bool:
        """True when no actionable tasks remain (all in a terminal status).

        Terminal includes 'failed': a task that failed verification must not
        keep the loop alive forever, but it is also never reported as completed
        — use is_all_completed() to distinguish COMPLETE from PARTIAL.
        """
        if not self.state:
            return False
        return bool(self.state.tasks) and all(
            t.status in self._TERMINAL_STATUSES for t in self.state.tasks
        )

    def is_all_completed(self) -> bool:
        """True only when every task genuinely completed (strict success)."""
        if not self.state:
            return False
        return bool(self.state.tasks) and all(t.status == "completed" for t in self.state.tasks)


# ═══════════════════════════════════════════════════════════════════════════════
# Quality Checker
# ═══════════════════════════════════════════════════════════════════════════════

class QualityChecker:
    """Verifies that created files meet quality requirements."""
    
    # Minimum sizes for different file types
    MIN_SIZES = {
        '.html': 500,
        '.css': 400,
        '.js': 100,
        '.py': 100,
        '.json': 50,
        '.md': 50,
    }
    DEFAULT_MIN = 100
    
    # Required files for different task types
    TASK_REQUIREMENTS = {
        'website': ['.html', '.css'],
        'webapp': ['.html', '.css', '.js'],
        'script': ['.py'],
    }
    
    @classmethod
    def detect_task_type(cls, task: str) -> Optional[str]:
        """Detect task type from description."""
        task_lower = task.lower()
        
        website_keywords = ['website', 'webseite', 'webpage', 'homepage', 'landing', 'seite']
        webapp_keywords = ['webapp', 'web app', 'application', 'anwendung']
        script_keywords = ['script', 'skript', 'python', 'automatisierung']
        
        if any(kw in task_lower for kw in webapp_keywords):
            return 'webapp'
        if any(kw in task_lower for kw in website_keywords):
            return 'website'
        if any(kw in task_lower for kw in script_keywords):
            return 'script'
        
        return None
    
    @classmethod
    def check_files(cls, files: List[str], task: str, base_dir: str) -> Dict[str, Any]:
        """
        Check if files meet quality requirements.
        
        Returns: {
            'passed': bool,
            'missing_types': List[str],
            'small_files': List[str],
            'errors': List[str]
        }
        """
        result = {
            'passed': True,
            'missing_types': [],
            'small_files': [],
            'errors': []
        }
        
        if not files:
            result['passed'] = False
            result['errors'].append("No files created")
            return result
        
        # Get task type and required extensions
        task_type = cls.detect_task_type(task)
        required_exts = cls.TASK_REQUIREMENTS.get(task_type, [])
        
        # Check for missing types
        for ext in required_exts:
            if not any(f.lower().endswith(ext) for f in files):
                result['missing_types'].append(ext)
                result['passed'] = False
        
        # Check file sizes
        for f in files:
            try:
                fpath = f if os.path.isabs(f) else os.path.join(base_dir, f)
                if not os.path.exists(fpath):
                    result['errors'].append(f"File not found: {f}")
                    result['passed'] = False
                    continue
                
                size = os.path.getsize(fpath)
                ext = os.path.splitext(f.lower())[1]
                min_size = cls.MIN_SIZES.get(ext, cls.DEFAULT_MIN)
                
                if size < min_size:
                    result['small_files'].append(f"{os.path.basename(f)} ({size}B < {min_size}B)")
                    result['passed'] = False
            except Exception as e:
                result['errors'].append(f"Error checking {f}: {e}")
        
        return result
    
    @classmethod
    def check_placeholders(cls, files: List[str], base_dir: str) -> Dict[str, Any]:
        """
        Check for unchanged template placeholders in files.
        
        Returns: {
            'has_placeholders': bool,
            'files_with_placeholders': {filename: [placeholder_list]},
            'total_placeholders': int
        }
        """
        # Common template placeholder patterns
        PLACEHOLDER_PATTERNS = [
            # Generic placeholders
            (r'\{\{.*?\}\}', 'Mustache template'),
            (r'\$\{.*?\}', 'Template literal'),
            (r'%\w+%', 'Percent placeholder'),
            
            # Common dummy text
            (r'Lorem ipsum', 'Lorem ipsum'),
            (r'Mein Unternehmen', 'German placeholder'),
            (r'My Company', 'English placeholder'),
            (r'Ihr Unternehmen', 'German placeholder'),
            (r'Your Company', 'English placeholder'),
            (r'Beispieltext', 'German sample text'),
            (r'Sample text', 'English sample text'),
            (r'Platzhalter', 'German placeholder'),
            (r'Placeholder', 'English placeholder'),
            (r'\bplaceholder\b', 'Placeholder word'),  # Case-insensitive will catch this
            (r'\bplace.?holder\b', 'Placeholder variant'),
            
            # Contact placeholders
            (r'example@example\.com', 'Example email'),
            (r'info@example\.com', 'Example email'),
            (r'email@domain\.com', 'Placeholder email'),
            (r'\+49\s*123', 'Placeholder phone'),
            (r'123-456-7890', 'Placeholder phone'),
            (r'Musterstraße', 'German placeholder street'),
            (r'123 Main St', 'Placeholder address'),
            
            # Service placeholders
            (r'Service\s*\d', 'Numbered service'),
            (r'Leistung\s*\d', 'German numbered service'),
            (r'Produkt\s*\d', 'Numbered product'),
            (r'Product\s*\d', 'Numbered product'),
            
            # Price placeholders
            (r'XX,XX\s*€', 'Price placeholder'),
            (r'\$XX\.XX', 'Price placeholder'),
            (r'€\s*\d{1,2}[.,]\d{2}', 'Generic price'),
            
            # Description placeholders
            (r'Beschreibung hier', 'German placeholder'),
            (r'Description here', 'English placeholder'),
            (r'Text hier einfügen', 'German placeholder'),
            (r'Insert text here', 'English placeholder'),
            (r'Hier kommt', 'German placeholder'),
            
            # Image placeholders
            (r'placeholder\.(?:jpg|png|gif|svg)', 'Placeholder image'),
            (r'image-placeholder', 'Placeholder image'),
            (r'via\.placeholder', 'Placeholder image service'),
        ]
        
        result = {
            'has_placeholders': False,
            'files_with_placeholders': {},
            'total_placeholders': 0
        }
        
        for f in files:
            try:
                fpath = f if os.path.isabs(f) else os.path.join(base_dir, f)
                if not os.path.exists(fpath):
                    continue
                
                # Only check text files
                ext = os.path.splitext(f.lower())[1]
                if ext not in ['.html', '.css', '.js', '.py', '.json', '.md', '.txt']:
                    continue
                
                with open(fpath, 'r', encoding='utf-8', errors='ignore') as file:
                    content = file.read()
                
                found_placeholders = []
                for pattern, desc in PLACEHOLDER_PATTERNS:
                    matches = re.findall(pattern, content, re.IGNORECASE)
                    if matches:
                        # Deduplicate and limit
                        unique_matches = list(set(matches))[:3]
                        for match in unique_matches:
                            found_placeholders.append(f"{desc}: '{match[:30]}'")
                
                if found_placeholders:
                    result['files_with_placeholders'][os.path.basename(f)] = found_placeholders
                    result['total_placeholders'] += len(found_placeholders)
                    result['has_placeholders'] = True

            except Exception:
                pass

        return result


# ═══════════════════════════════════════════════════════════════════════════════
# Task Goal Verification (used by stuck detection)
# ═══════════════════════════════════════════════════════════════════════════════

def _pick_main_deliverable(base_dir: str) -> str:
    """Best-effort path of the project's main deliverable file ('' if none).

    Preferred well-known entry files first, then the largest code file.
    Infrastructure files (hidden, .git/.vaf internals) are ignored.
    """
    preferred = ("index.html", "main.py", "app.py", "main.js", "index.js")
    try:
        for name in preferred:
            p = os.path.join(base_dir, name)
            if os.path.isfile(p):
                return p
        candidates = []
        for entry in os.listdir(base_dir):
            if entry.startswith('.'):
                continue
            p = os.path.join(base_dir, entry)
            if os.path.isfile(p) and os.path.splitext(entry)[1] in (
                '.html', '.py', '.js', '.ts', '.css', '.java', '.cpp', '.c', '.go', '.rs', '.sh'
            ):
                candidates.append((os.path.getsize(p), p))
        if candidates:
            return max(candidates)[1]
    except Exception:
        pass
    return ""


def _verify_task_goal(
    task_title: str,
    task_files: List[str],
    base_dir: str,
    linter_active: bool = False,
    llm_verify=None,
) -> "tuple[bool, str]":
    """Check whether a task's goal is achieved before force-completing it.

    Deterministic first: if the task wrote its own files, they must exist and be
    free of template placeholders, and no linter error may be active. Without
    file evidence (goal may already be implemented by an earlier task), one
    bounded LLM check decides; `llm_verify` is an injectable callable
    (prompt -> response text) so this stays unit-testable. Any failure or
    ambiguity verifies as False — a stuck task must never become a silent
    'completed'.

    Returns (verified, evidence/reason).
    """
    # 1) Deterministic: file evidence from this task
    if task_files:
        missing = [f for f in task_files if not os.path.exists(f)]
        if missing:
            return False, f"task wrote files but they are missing: {', '.join(os.path.basename(m) for m in missing[:3])}"
        if linter_active:
            return False, "task files written but linter errors are still active"
        placeholder_check = QualityChecker.check_placeholders(task_files, base_dir)
        if placeholder_check.get('has_placeholders'):
            return False, f"task files contain {placeholder_check['total_placeholders']} unresolved placeholders"
        names = ', '.join(os.path.basename(f) for f in task_files[:5])
        return True, f"files written and clean: {names}"

    # 2) No file evidence -> one bounded LLM check against the main deliverable
    if llm_verify is None:
        return False, "no files written for this task and no verifier available"

    main_file = _pick_main_deliverable(base_dir)
    if not main_file:
        return False, "no files written for this task and no deliverable exists yet"

    try:
        with open(main_file, 'r', encoding='utf-8', errors='replace') as f:
            code_excerpt = f.read(6000)
        prompt = (
            "You are a strict code verifier. Answer with YES or NO as the first word, "
            "then one short line of evidence.\n\n"
            f"Goal to verify: {task_title}\n\n"
            f"Code ({os.path.basename(main_file)}):\n```\n{code_excerpt}\n```\n\n"
            "Is this goal already fully implemented in the code above?"
        )
        response = str(llm_verify(prompt) or "").strip()
        if not response:
            return False, "LLM check returned no answer"
        # Reasoning models bury the verdict at the END of their chain of thought
        # — take the last standalone YES/NO instead of requiring the first word.
        verdicts = re.findall(r'\b(YES|NO)\b', response.upper())
        if verdicts and verdicts[-1] == "YES":
            evidence = response[-200:].replace('\n', ' ')
            return True, f"verified by LLM check: {evidence}"
        if verdicts:
            return False, f"LLM check did not confirm goal: {response[-150:]}"
        return False, f"LLM check gave no clear verdict: {response[-150:]}"
    except Exception as e:
        return False, f"verification check failed: {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# Context State Management - Robust context switching with rollback
# ═══════════════════════════════════════════════════════════════════════════════

from dataclasses import dataclass, field

@dataclass
class ContextState:
    """
    Encapsulates current context state for robust switching.

    Each ContextState represents either:
    - Main Context: Planning phase (set_todos)
    - Task Context: Execution phase (work on specific task)

    This allows clean context switching with rollback on failure.
    """
    context_manager: Any  # ContextManager instance
    history: List[Dict]
    phase: str  # "main", "task_0", "task_1", etc.
    task_idx: Optional[int] = None

    # State tracking per context
    files_created: List[str] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    last_tool_call: Optional[str] = None

    def is_main(self) -> bool:
        """Check if this is the main context (planning phase)."""
        return self.phase == "main"

    def is_task(self) -> bool:
        """Check if this is a task context (execution phase)."""
        return self.phase.startswith("task_")

    def get_task_idx(self) -> Optional[int]:
        """Get task index if in task context."""
        if self.is_task():
            try:
                return int(self.phase.split("_")[1])
            except (IndexError, ValueError):
                return None
        return None

    def clone(self):
        """Create a shallow copy for rollback."""
        return ContextState(
            context_manager=self.context_manager,
            history=self.history.copy(),
            phase=self.phase,
            task_idx=self.task_idx,
            files_created=self.files_created.copy(),
            tools_used=self.tools_used.copy(),
            last_tool_call=self.last_tool_call
        )

    def record_file_created(self, filepath: str):
        """Record that a file was created in this context."""
        if filepath not in self.files_created:
            self.files_created.append(filepath)

    def record_tool_call(self, tool_name: str):
        """Record tool usage in this context."""
        if tool_name not in self.tools_used:
            self.tools_used.append(tool_name)
        self.last_tool_call = tool_name

    def has_created_files(self) -> bool:
        """Check if any files were created in this context."""
        return len(self.files_created) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Git Tools for Coding Agent
# ═══════════════════════════════════════════════════════════════════════════════

class GitInitTool(BaseTool):
    """Initialize a Git repository in the project directory."""
    name = "git_init"
    permission_level = "write"
    side_effect_class = "reversible"
    description = "Initialize a Git repository in the current project directory. Creates .git directory and .gitignore file."
    
    parameters = {
        "type": "object",
        "properties": {},
        "required": []
    }
    
    def run(self, **kwargs) -> str:
        # base_dir will be passed from the CodingAgentTool context
        base_dir = kwargs.get('base_dir', '.')
        try:
            import subprocess
            git_dir = os.path.join(base_dir, '.git')
            if os.path.exists(git_dir):
                return "✅ Git repository already initialized."
            
            kwargs = {'cwd': base_dir, 'check': True, 'capture_output': True}
            if platform.system() == "Windows":
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            subprocess.run(['git', 'init'], **kwargs)
            return "✅ Git repository initialized successfully."
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            return f"❌ Error initializing Git: {e}"


class GitAddCommitTool(BaseTool):
    """Add files to Git staging area and commit them."""
    name = "git_add_commit"
    permission_level = "write"
    side_effect_class = "irreversible"
    description = "Add files to Git staging area and create a commit with a message. Use this to save your work progress."
    
    parameters = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Commit message describing the changes"
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional: Specific files to add (default: all files)"
            }
        },
        "required": ["message"]
    }
    
    def run(self, **kwargs) -> str:
        base_dir = kwargs.get('base_dir', '.')
        message = kwargs.get('message', 'Update')
        files = kwargs.get('files', [])
        
        try:
            import subprocess
            git_dir = os.path.join(base_dir, '.git')
            if not os.path.exists(git_dir):
                return "❌ Git repository not initialized. Call git_init first."
            
            # Add files
            run_kwargs = {'cwd': base_dir, 'check': True, 'capture_output': True}
            if platform.system() == "Windows":
                run_kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

            if files:
                for f in files:
                    file_path = os.path.join(base_dir, f) if not os.path.isabs(f) else f
                    if os.path.exists(file_path):
                        subprocess.run(['git', 'add', file_path], **run_kwargs)
            else:
                subprocess.run(['git', 'add', '.'], **run_kwargs)

            # Commit
            subprocess.run(['git', 'commit', '-m', message], **run_kwargs)
            return f"✅ Committed changes: {message}"
        except subprocess.CalledProcessError as e:
            return f"❌ Error committing: {e}"
        except FileNotFoundError:
            return "❌ Git not found. Please install Git."


class GitStatusTool(BaseTool):
    """Get the current Git status of the repository."""
    name = "git_status"
    permission_level = "read"
    side_effect_class = "none"
    description = "Check the current Git status: shows modified, staged, and untracked files."
    
    parameters = {
        "type": "object",
        "properties": {},
        "required": []
    }
    
    def run(self, **kwargs) -> str:
        base_dir = kwargs.get('base_dir', '.')
        try:
            import subprocess
            git_dir = os.path.join(base_dir, '.git')
            if not os.path.exists(git_dir):
                return "❌ Git repository not initialized. Call git_init first."
            
            run_kwargs = {'cwd': base_dir, 'capture_output': True, 'text': True, 'check': True}
            if platform.system() == "Windows":
                run_kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(['git', 'status', '--short'], **run_kwargs)
            if result.stdout.strip():
                return f"Git Status:\n{result.stdout}"
            else:
                return "✅ Working directory clean (no changes)"
        except subprocess.CalledProcessError as e:
            return f"❌ Error getting status: {e}"
        except FileNotFoundError:
            return "❌ Git not found. Please install Git."


class GitLogTool(BaseTool):
    """View Git commit history."""
    name = "git_log"
    permission_level = "read"
    side_effect_class = "none"
    description = "View the Git commit history. Shows recent commits with messages."
    
    parameters = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Number of commits to show (default: 10)",
                "default": 10
            }
        },
        "required": []
    }
    
    def run(self, **kwargs) -> str:
        base_dir = kwargs.get('base_dir', '.')
        limit = kwargs.get('limit', 10)
        
        try:
            import subprocess
            git_dir = os.path.join(base_dir, '.git')
            if not os.path.exists(git_dir):
                return "❌ Git repository not initialized. Call git_init first."
            
            run_kwargs = {'cwd': base_dir, 'capture_output': True, 'text': True, 'check': True}
            if platform.system() == "Windows":
                run_kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                ['git', 'log', f'--max-count={limit}', '--oneline'],
                **run_kwargs
            )
            if result.stdout.strip():
                return f"Recent commits:\n{result.stdout}"
            else:
                return "No commits yet."
        except subprocess.CalledProcessError as e:
            return f"❌ Error getting log: {e}"
        except FileNotFoundError:
            return "❌ Git not found. Please install Git."


# ═══════════════════════════════════════════════════════════════════════════════
# Main Coding Agent Tool
# ═══════════════════════════════════════════════════════════════════════════════

class CodingAgentTool(BaseTool):
    name = "coding_agent"
    permission_level = "write"
    side_effect_class = "reversible"
    
    # Class-level lock to prevent multiple instances running simultaneously
    _instance_lock = threading.Lock()
    _active_instance = None  # Track active CoderTUI instance
    
    description = """Autonomous code generation Sub-Agent. 
    **PRIMARY TOOL for:**
    - **Coding:** "Write code", "Fix bug", "Refactor", "Create script"
    - **Web:** "Create website", "Build app", "HTML/CSS/JS"
    - **Languages:** Python, JavaScript, Java, C++, etc.
    
    Creates complete, working files in a project directory. 
    Do NOT plan or describe - just call this tool with the task."""

    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The coding task to complete. Use this parameter (not 'prompt' or 'description')."
            },
            "project_path": {
                "type": "string",
                "description": "Optional: Absolute path to an EXISTING project directory to continue working on. ONLY provide this when editing/fixing an existing project whose path you already know (e.g. from [SESSION WORKSPACE]). Do NOT invent or guess paths. For new projects, omit this entirely — the agent will create the project directory automatically in the correct location."
            }
        },
        "required": ["task"]
    }

    def _determine_base_dir(self, task: str, provided_path: str = None) -> str:
        """
        Intelligently determine the base directory for the coding task.
        
        Logic:
        1. If path provided -> Use it.
        2. If CWD is a project root (.git, .vaf, etc.) AND task is NOT "create new project" -> Use CWD.
        3. Else -> Generate new project folder in VAF_Projects.
        """
        # 1. Explicit path (unless it points to an unsafe location like /home/<user>)
        if provided_path:
            _abs = os.path.abspath(os.path.expanduser(provided_path))
            if not is_unsafe_project_dir(_abs):
                return _abs

        cwd = os.getcwd()

        # 2. Check if CWD is a project root — but never for unsafe locations.
        # Sub-agent terminals spawn with CWD=$HOME; ~/.vaf (config dir) and a stray
        # ~/.git would otherwise make the home directory look like a project root
        # and the agent would dump files directly into /home/<user>.
        project_markers = [".git", ".vaf", "package.json", "pyproject.toml", "requirements.txt"]
        is_project_root = (
            not is_unsafe_project_dir(cwd)
            and any(os.path.exists(os.path.join(cwd, m)) for m in project_markers)
        )

        # 3. Detect "Create New" intent
        task_lower = task.lower()
        create_keywords = ["create new", "start new", "new project", "generate new", "scaffold"]
        is_create_intent = any(kw in task_lower for kw in create_keywords)

        # DECISION:
        if is_project_root and not is_create_intent:
            # We are in a project and user didn't explicitly ask for a NEW one -> Stay here!
            return cwd
            
        # Fallback: Create new project folder
        return self._generate_project_directory(task)

    def _get_session_project_path(self, task: str) -> str:
        """Return last_project_path from session runtime_state if task looks like editing existing code.

        Avoids creating a new directory when the user says 'fix the UI', 'update the website', etc.
        Returns empty string if task is clearly a new-project request or if no session path exists.
        """
        task_l = task.lower()
        # Explicit new-project keywords → always create fresh
        _new_kw = [
            "create new", "new project", "start new", "scaffold",
            "neue webseite", "neues projekt", "new website",
            "erstelle eine neue", "erstelle mir eine neue",
        ]
        if any(k in task_l for k in _new_kw):
            return ""
        # Must contain at least one edit signal
        _edit_kw = [
            "fix", "edit", "update", "modify", "change", "improve", "adjust", "correct", "repair",
            "bug", "error", "issue", "the ui", "die ui", "the website", "die webseite",
            "fehler", "aktualisier", "bearbeit", "verbessert", "korrigier", "anpass",
            "schau", "prüf", "ändert", "überarbeit",
        ]
        if not any(k in task_l for k in _edit_kw):
            return ""
        try:
            from vaf.core.subagent_ipc import get_current_session_id
            _sid = get_current_session_id()
            if not _sid:
                return ""
            from vaf.core.session import SessionManager as _SM
            _sm = _SM()
            _sess = _sm.load(_sid)
            _last = (getattr(_sess, "runtime_state", None) or {}).get("last_project_path", "")
            if _last and os.path.isdir(_last) and not is_unsafe_project_dir(_last):
                return _last
        except Exception:
            pass
        return ""

    def _generate_project_directory(self, task: str) -> str:
        """Generate a user-friendly project directory name based on task. OS-independent."""
        # Strip embedded file paths before keyword extraction so path components
        # (e.g. "Webseite Erstelle Professionelle" inside a path string) don't
        # pollute the project name.
        task_for_naming = re.sub(r'/[^\s]+', ' ', task)   # Unix paths
        task_for_naming = re.sub(r'[A-Za-z]:\\[^\s]+', ' ', task_for_naming)  # Windows paths
        task_for_naming = task_for_naming.strip()
        task_lower = task_for_naming.lower()

        # Detect project type
        # Note: check 'html' before 'script' so <script>-Tag in HTML tasks doesn't misclassify
        if any(kw in task_lower for kw in ['game', 'spiel ', 'spiel,', 'spiel.']):
            prefix = "Game"
        elif any(kw in task_lower for kw in ['website', 'webseite', 'homepage', 'landing page', 'seite', '.html', 'index.html', 'html datei', 'html-datei', 'html file']):
            prefix = "Webseite"
        elif any(kw in task_lower for kw in ['webapp', 'web app', 'application', 'anwendung']):
            prefix = "Webapp"
        elif any(kw in task_lower for kw in ['python script', 'python skript', '.py script', 'bash script', 'shell script']):
            # Narrow match: only explicit "python/bash script", NOT bare "script" which also appears in <script>-Tag HTML
            prefix = "Script"
        elif any(kw in task_lower for kw in ['project', 'projekt']):
            prefix = "Projekt"
        else:
            prefix = "Projekt"
        
        # Extract key words from task (paths already stripped, remove common words)
        stop_words = {
            'the', 'a', 'an', 'for', 'in', 'on', 'at', 'to', 'of', 'and', 'or', 'but',
            'mit', 'für', 'in', 'auf', 'zu', 'von', 'und', 'oder', 'aber',
            'eine', 'ein', 'einen', 'einem', 'einer', 'der', 'die', 'das', 'den', 'dem',
            'read', 'file', 'path', 'return', 'contents', 'full', 'datei', 'pfad', 'inhalt',
            # Task verbs/adjectives — say nothing about WHAT is being built
            'erstelle', 'erstellen', 'erstell', 'create', 'baue', 'bauen', 'build',
            'schreibe', 'schreiben', 'schreib', 'write', 'mache', 'machen', 'make',
            'generiere', 'generieren', 'generate',
            'einfach', 'einfache', 'einfaches', 'einfachen', 'simple', 'basic',
            'neue', 'neues', 'neuen', 'new', 'kleines', 'kleine', 'small',
            'professionelle', 'professionell', 'professionelles', 'professional',
            # Project-type words — already covered by the name prefix
            'webseite', 'website', 'homepage', 'seite', 'webapp', 'app',
            'script', 'skript', 'projekt', 'project', 'anwendung', 'application',
        }
        words = re.findall(r'\b[a-zA-ZäöüÄÖÜß]{3,}\b', task_for_naming)
        keywords = [w for w in words if w.lower() not in stop_words][:3]  # Max 3 keywords

        # Create name
        if keywords:
            name_part = ' '.join(keywords[:2]).title()  # Max 2 keywords
            # Clean name (remove special chars, limit length) - OS-independent
            name_part = re.sub(r'[^a-zA-Z0-9\s]', '', name_part)[:25]
            project_name = f"{prefix} {name_part}".strip()
        else:
            # Fallback: use first meaningful words from stripped text
            words = task_for_naming.split()[:3]
            name_part = ' '.join([w for w in words if len(w) > 3])[:20]
            project_name = f"{prefix} {name_part}".strip() if name_part else f"{prefix} {int(time.time())}"
        
        # Ensure valid directory name (OS-independent - handles Windows, Linux, macOS)
        # Windows: < > : " / \ | ? *
        # Linux/macOS: / (and null byte, but we don't use that)
        invalid_chars = r'[<>:"/\\|?*]'
        project_name = re.sub(invalid_chars, '_', project_name)
        project_name = project_name.strip('. ')  # Remove leading/trailing dots and spaces
        
        # Get base directory - OS-independent using Platform class
        from vaf.core.platform import Platform
        docs_dir = Platform.documents_dir()

        # Per-user isolation: put projects under VAF_Projects/{user_scope_id[:8]}/ when
        # running in a multi-user context. Falls back to VAF_Projects/ for local/admin.
        # Per-chat isolation: each chat gets its own folder below that
        # (VAF_Projects/[user]/[session_id]/<ProjectName>), so projects from
        # different chats never mix.
        _user_prefix = ""
        _session_folder = ""
        try:
            from vaf.core.subagent_ipc import get_current_session_id as _get_sid
            _sid = _get_sid()
            if _sid:
                _session_folder = re.sub(r'[^a-zA-Z0-9_-]', '', _sid)[:32]
                from vaf.core.session import SessionManager as _SM2
                _s = _SM2().load(_sid)
                _uid = (_s.metadata or {}).get("user_scope_id", "")
                if _uid:
                    _user_prefix = _uid[:8]
        except Exception:
            pass

        projects_root = os.path.join(
            docs_dir, "VAF_Projects",
            *(p for p in (_user_prefix, _session_folder) if p)
        )
        os.makedirs(projects_root, exist_ok=True)
        
        base_dir = os.path.join(projects_root, project_name)
        
        # Handle duplicates safely (no while loop)
        if os.path.exists(base_dir):
            timestamp = time.strftime("%H%M%S")
            base_dir = f"{base_dir}_{timestamp}"
        
        return base_dir
    
    def _ensure_git_repo(self, base_dir: str):
        """Initialize Git repository if not already initialized. OS-independent."""
        # Never git-init unsafe locations (home dir, Documents root, ...) — a .git
        # there would make them look like project roots forever after.
        if is_unsafe_project_dir(base_dir):
            return
        git_dir = os.path.join(base_dir, '.git')
        if os.path.exists(git_dir):
            return  # Already a git repo
        
        try:
            import subprocess
            run_kwargs = {'cwd': base_dir, 'check': True, 'capture_output': True}
            if platform.system() == "Windows":
                run_kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            # Initialize git repo - OS-independent (git works on all platforms)
            subprocess.run(['git', 'init'], **run_kwargs)

            # Create .gitignore - OS-independent
            gitignore_path = os.path.join(base_dir, '.gitignore')
            if not os.path.exists(gitignore_path):
                gitignore_content = """# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
env/
venv/
*.egg-info/

# Node
node_modules/
npm-debug.log

# IDE
.vscode/
.idea/
*.swp
*.swo

# OS
.DS_Store
Thumbs.db

# VAF
*.tmp
*.log
"""
                with open(gitignore_path, 'w', encoding='utf-8') as f:
                    f.write(gitignore_content)

            # Initial commit if there are files - OS-independent
            try:
                subprocess.run(['git', 'add', '.'], **run_kwargs)
                subprocess.run(['git', 'commit', '-m', 'Initial commit'], **run_kwargs)
            except:
                pass  # No files to commit yet
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Git not available or failed - continue without git (graceful degradation)
            pass

    def run(self, **kwargs) -> str:
        # Accept 'prompt' as an alias for 'task' (LLMs sometimes use either name)
        task = kwargs.get('task', '') or kwargs.get('prompt', '')
        if not task:
            return "Error: No task provided."

        # ── History/Rollback delegation fast path ────────────────────────────
        # The Main Agent talks to the coder like a tool: a task such as
        # "zeig die History" or "rollback auf <id>" is answered directly and
        # deterministically — no agentic loop, no terminal spawn, no LLM.
        # The coder owns the project history; the Main Agent only delegates.
        try:
            from vaf.tools.project_git import (
                ProjectHistoryTool,
                ProjectRollbackTool,
                _detect_history_rollback_intent,
            )
            _hr_kind, _hr_commit = _detect_history_rollback_intent(task)
            if _hr_kind:
                _hr_path = kwargs.get('project_path', '')
                if _hr_kind == "history":
                    return ProjectHistoryTool().run(project_path=_hr_path)
                if not _hr_commit:
                    history = ProjectHistoryTool().run(project_path=_hr_path)
                    return (
                        f"{history}\n\n"
                        "Rollback needs a version id. Ask the user which version to restore, "
                        'then call coding_agent again with task="rollback auf <id>".'
                    )
                return ProjectRollbackTool().run(commit=_hr_commit, project_path=_hr_path)
        except Exception as _hr_e:
            return f"Error: history/rollback handling failed: {_hr_e}"

        # ── Read-task guard ───────────────────────────────────────────────────
        # coding_agent is a CODE CREATOR, not a file reader.
        # If the main agent tries to use it purely to read/return file contents,
        # reject immediately so it falls back to read_file / librarian_agent.
        # Strip embedded file paths before keyword checks — otherwise words like
        # "Erstelle" inside a path (e.g. /VAF_Projects/Webseite Erstelle .../index.html)
        # would falsely trigger the create-intent keywords.
        _task_stripped_guard = re.sub(r'/[^\s]+', ' ', task)           # Unix paths
        _task_stripped_guard = re.sub(r'[A-Za-z]:\\[^\s]+', ' ', _task_stripped_guard)  # Windows paths
        _task_lower_guard = _task_stripped_guard.lower()
        _read_only_indicators = [
            "read the file", "read file", "return its full contents",
            "return the full contents", "return its contents",
            "output exactly what's in it", "do not summarize, do not analyze",
            "do not modify", "just read",
            "datei lesen", "dateiinhalt zurückgeben", "inhalt zurückgeben",
        ]
        _has_create_intent = any(kw in _task_lower_guard for kw in [
            "create", "write", "build", "generate", "make", "fix", "refactor",
            "erstelle", "schreib", "baue", "generiere", "mach",
        ])
        if any(p in _task_lower_guard for p in _read_only_indicators) and not _has_create_intent:
            return (
                "ERROR: coding_agent is a code-creation tool and cannot be used to read files. "
                "Use the `read_file` tool directly to read file contents. "
                "Example: read_file(path='/path/to/file.html')"
            )
        # ── End read-task guard ───────────────────────────────────────────────

        # Sub-agent debug logger (only active inside sub-agent terminals)
        try:
            from vaf.core.subagent_debug import get_subagent_logger_from_env
            # create_fallback: loop telemetry must persist in EVERY run mode
            # (direct CLI, in-process, IPC spawn) — not only when an IPC task id exists.
            lg = get_subagent_logger_from_env(create_fallback=True, agent_type="coding_agent")
            if lg:
                lg.event(
                    "coding_agent_tool_run_invoked",
                    cwd=str(os.getcwd()),
                    kwargs_keys=list(kwargs.keys()),
                )
        except Exception:
            lg = None  # type: ignore[assignment]

        # ═══════════════════════════════════════════════════════════════════
        # CHECK IF RUNNING IN SEPARATE TERMINAL MODE
        # ═══════════════════════════════════════════════════════════════════
        from vaf.core.config import Config
        from vaf.core.platform import Platform
        from vaf.cli.ui import UI
        
        # If already in sub-agent terminal, run normally
        if os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "").strip() in ("1", "true", "yes"):
            # Continue with normal execution below
            pass
        elif Config.get("sub_agents_in_separate_terminals", False):
            # Start in new terminal window with IPC tracking
            project_path = kwargs.get('project_path', '')
            
            # Build command with proper escaping
            import shlex
            from vaf.core.subagent_ipc import get_ipc, get_current_session_id
            
            # Create task in IPC system
            ipc = get_ipc()
            task_id = ipc.create_task("coding_agent", task_description=task)
            
            # Pass session/task context to the sub-agent via the CHILD env only (not the parent's
            # process-global os.environ), so concurrent workers don't clobber each other's session.
            session_id = get_current_session_id()
            _sub_env = {"VAF_TASK_ID": task_id, "VAF_AGENT_TYPE": "coding_agent"}
            if session_id:
                _sub_env["VAF_SESSION_ID"] = session_id

            # Pass provider configuration to sub-agent
            use_separate_provider = Config.get("subagent_use_separate_provider", False)
            if use_separate_provider:
                subagent_provider = Config.get("subagent_provider", "inherit")
                if subagent_provider != "inherit":
                    _sub_env["VAF_PROVIDER"] = subagent_provider
            
            # CRITICAL FIX: Use current python executable instead of 'vaf' command
            # This ensures we use the exact same code/environment as the main process
            import sys
            cmd_parts = [sys.executable, '-m', 'vaf.main', 'subagent', 'run', 'coding_agent', '--task', task, '--task-id', task_id]
            if project_path:
                cmd_parts.extend(['--project-path', project_path])
            
            if Platform.is_windows():
                # Windows: properly escape for cmd /k
                # Use double quotes for the entire command and escape inner quotes
                escaped_parts = []
                for part in cmd_parts:
                    if ' ' in part or '"' in part:
                        escaped_part = part.replace('"', '\\"')
                        escaped_parts.append(f'"{escaped_part}"')
                    else:
                        escaped_parts.append(part)
                cmd = ' '.join(escaped_parts)
                title = f"VAF Coding Agent [{task_id}]"
            else:
                # Unix: use shell quoting
                cmd = ' '.join(shlex.quote(str(part)) for part in cmd_parts)
                title = f"VAF Coding Agent [{task_id}]"
            
            if Platform.open_new_terminal(cmd, title=title, extra_env=_sub_env):
                # Mark task as running
                ipc.mark_task_running(task_id)
                
                UI.event("Sub-Agent", f"Coding Agent started in new terminal [Task: {task_id}]", style="bold cyan")
                # Return special marker for main agent to recognize async task
                return f"[SUBAGENT_ASYNC:{task_id}:coding_agent] Sub-Agent running in separate terminal. Task: {task[:80]}..."
            else:
                # Fallback: run normally if terminal opening fails
                UI.warning("Failed to open new terminal, running in current window")
                ipc.cancel_task(task_id)
        
        # ═══════════════════════════════════════════════════════════════════
        # PREVENT MULTIPLE INSTANCES - Stop previous instance if running
        # ═══════════════════════════════════════════════════════════════════
        
        with CodingAgentTool._instance_lock:
            # If there's an active instance, stop it cleanly before starting new one
            if CodingAgentTool._active_instance is not None:
                try:
                    old_tui = CodingAgentTool._active_instance
                    # Stop the previous instance's Live display completely
                    old_tui._live_started = False
                    # Stop animation thread
                    if old_tui._animation_running is not None:
                        old_tui._animation_running.clear()
                    # Stop Live display
                    if old_tui._live is not None:
                        try:
                            old_tui._live.stop()
                        except Exception:
                            pass
                    
                    # Give Rich time to clear the screen
                    time.sleep(0.5)
                except Exception:
                    pass  # Ignore errors when stopping previous instance
            # Mark this as the new active instance (will be set after TUI creation)

        # ═══════════════════════════════════════════════════════════════════
        # START TUI - SMOOTH UPDATES (like OpenCode)
        # ═══════════════════════════════════════════════════════════════════
        
        # Task Manager for structured TODO workflow
        task_mgr = TaskManager()
        
        # Create a fresh console for the Coder to avoid conflicts
        local_console = Console(force_terminal=True)

        # When running inside a workflow terminal, use simple_mode (no full-screen TUI).
        # This prevents the Rich Live display from replacing the workflow's output.
        _in_workflow_terminal = os.environ.get("VAF_IN_WORKFLOW_TERMINAL", "").strip() in ("1", "true", "yes")

        # Disable animation to prevent terminal spam/flicker
        tui = CoderTUI(local_console, task, task_mgr, animate=False, simple_mode=_in_workflow_terminal)

        # Mark this as the active instance
        with CodingAgentTool._instance_lock:
            CodingAgentTool._active_instance = tui

        # In simple_mode, skip Rich Live entirely — just print progress lines
        if _in_workflow_terminal:
            live = _NoopLive()
            print(f"[Coder] Starting coding agent for: {task[:80]}...", flush=True)
        else:
            # Use Rich's Live with auto-refresh for animation (12 FPS)
            live = Live(
                tui,
                console=local_console,
                refresh_per_second=12,
                transient=False,
            )
            # Store Live in TUI so it can be stopped from outside
            tui._live = live
            live.start()

        # CRITICAL: Start animation thread IMMEDIATELY after live.start()
        # This ensures animation continues even during blocking operations (like template selection)
        import threading
        animation_running = threading.Event()
        animation_running.set()

        # Store for cleanup
        tui._animation_running = animation_running
        
        def animation_updater():
            while animation_running.is_set():
                try:
                    # Update live display
                    # Check if update is needed (set by append_stream) or always update for animation
                    needs_update = tui._needs_update
                    if needs_update:
                        tui._needs_update = False  # Clear flag
                    
                    # Always update (for smooth animation), but check flag for immediate updates
                    try:
                        # Render with lock (safe - we're in separate thread)
                        rendered = tui.render()
                        live.update(rendered)
                    except Exception:
                        pass  # Don't fail if render is blocked
                    
                    # Sleep shorter if update was needed, longer otherwise
                    sleep_time = 0.05 if needs_update else 0.1
                    time.sleep(sleep_time)
                except:
                    break
        
        animation_thread = threading.Thread(target=animation_updater, daemon=True)
        animation_thread.start()
        
        # CRITICAL: Callback is no longer needed - we use flag-based updates instead
        # Animation thread will check _needs_update flag to trigger immediate updates
        # This prevents deadlocks (callback won't call render() while lock is held)
        
        def stop_live():
            """Stop live display cleanly."""
            try:
                live.stop()
            except Exception:
                pass

        # ── Resolve LLM endpoint ─────────────────────────────────────────────
        # When VAF runs in API mode (DeepSeek, OpenRouter, …) the local
        # llama-server on :8080 is NOT running.  Detect the active provider
        # and build the correct base_url / api_key / model so that all three
        # hardcoded 127.0.0.1:8080 calls below route to the right backend.
        _provider = (
            os.environ.get("VAF_PROVIDER", "").strip().lower()
            or Config.get("provider", "local")
        )
        # base_url already contains /v1 (from api_backend.py); store full endpoint URLs
        # so we never double up the /v1 path segment.
        _API_PROVIDERS = {
            "deepseek":   ("https://api.deepseek.com/v1",       "deepseek-v4-flash"),
            "openai":     ("https://api.openai.com/v1",         "gpt-4o"),
            "openrouter": ("https://openrouter.ai/api/v1",      "anthropic/claude-sonnet-4.6"),
            "anthropic":  ("https://api.anthropic.com/v1",      "claude-sonnet-4-6"),
            "google":     ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
        }
        _is_api_mode = _provider in _API_PROVIDERS
        if _is_api_mode:
            _api_base, _llm_default_model = _API_PROVIDERS[_provider]
            _llm_api_key = Config.get_api_key(_provider) or ""
            _llm_model = Config.get(f"api_model_{_provider}", _llm_default_model) or _llm_default_model
            # Resolve deepseek-auto → concrete model name (API rejects "deepseek-auto" directly)
            if _provider == "deepseek" and str(_llm_model).lower() == "deepseek-auto":
                _in_wf = os.environ.get("VAF_IN_WORKFLOW_TERMINAL", "").strip() in ("1", "true", "yes")
                _sa = Config.get("subagent_model", "").strip()
                if _in_wf and _sa and _sa.lower() != "deepseek-auto":
                    _llm_model = _sa
                elif _in_wf:
                    _llm_model = "deepseek-v4-pro"
                else:
                    _llm_model = "deepseek-v4-flash"
            # Full URLs — base already ends with /v1 so just append the path segment
            _llm_chat_url = f"{_api_base}/chat/completions"
            _llm_models_url = f"{_api_base}/models"
        else:
            _llm_api_key = ""
            _llm_model = None  # fetched from /v1/models below
            _llm_chat_url = "http://127.0.0.1:8080/v1/chat/completions"
            _llm_models_url = "http://127.0.0.1:8080/v1/models"

        # Server health check (local llama-server only; API providers are always reachable)
        tui.set_action("Checking server...")
        live.update(tui.render())  # Force immediate update
        time.sleep(0.05) # Prevent lock contention
        if not _is_api_mode:
            try:
                health = requests.get("http://127.0.0.1:8080/health", timeout=5)
                if health.status_code != 200:
                    return f"❌ Server Error: VAF Server not ready (Status {health.status_code}). Please start VAF Server on port 8080."
                tui.set_action("Server ready")
                live.update(tui.render())  # Force immediate update
                time.sleep(0.05)
            except requests.exceptions.ConnectionError:
                return "❌ Connection Error: VAF Server unreachable (Port 8080). Please start VAF Server."
            except Exception as e:
                return f"❌ Server Check Failed: {e}. Please check if VAF Server is running."
        else:
            tui.set_action(f"API mode ({_provider})")
            live.update(tui.render())
            time.sleep(0.05)
        
        # ═══════════════════════════════════════════════════════════════════
        # TOOLS - File tools + TODO management (NOT coding_agent!)
        # ═══════════════════════════════════════════════════════════════════
        tui.set_action("Loading tools...")
        live.update(tui.render())  # Force immediate update
        time.sleep(0.05)
        
        # IMPORTANT: coding_agent must NOT have access to itself!
        # ... imports ...
        from vaf.tools.linter import LinterTool
        self.local_tools = {
            "write_file": WriteFileTool(),
            "read_file": ReadFileTool(),
            "list_files": ListFilesTool(),
            "python_sandbox": PythonSandboxTool(),
            "linter": LinterTool(),
        }
        if HAS_CODING_TOOLS:
            self.local_tools["bash"] = BashTool()
            self.local_tools["codesearch"] = CodeSearchTool()

        # Setup working directory
        tui.set_action("Creating project...")
        live.update(tui.render())  # Force immediate update
        time.sleep(0.05)
        
        # ═══════════════════════════════════════════════════════════════════
        # CHECK FOR CONTENT_ONLY MODE (before creating project directory)
        # ═══════════════════════════════════════════════════════════════════
        
        # Detect user preferences for template usage (as hints, not hard blocks)
        # The LLM will consider these preferences but can override if a template would genuinely help
        user_template_preference = None
        task_upper = task.upper()

        if "NO_TEMPLATE" in task_upper or "FROM_SCRATCH" in task_upper or "WITHOUT TEMPLATE" in task_upper:
            user_template_preference = "no_template"
        elif "CONTENT_ONLY" in task_upper or "ONLY THE CODE" in task_upper or "ONLY THE HTML" in task_upper or "RETURN ONLY" in task_upper:
            user_template_preference = "content_only"
        elif "SIMPLE" in task_upper or "BASIC" in task_upper or "MINIMAL" in task_upper:
            user_template_preference = "simple"

        # Legacy: Hard skip for CONTENT_ONLY mode (creates temp dir instead of project)
        # This is ONLY for automations that need temp content without project structure
        skip_template = (
            "CONTENT_ONLY" in task_upper and ("AUTOMATION" in task_upper or "NO PROJECT" in task_upper or "NO FILE PATHS" in task_upper)
        )
        
        # Check if continuing existing project
        project_path = kwargs.get('project_path', '')
        if project_path and is_unsafe_project_dir(project_path):
            # Sessions poisoned before the safety guard may still tell the main
            # agent the "project" lives in /home/<user>. Ignore and fall back to
            # the normal VAF_Projects flow instead of writing into unsafe dirs.
            tui.append_stream(f"Ignoring unsafe project path: {project_path}")
            project_path = ''
        if project_path:
            # Continue existing project OR create new one at specific path
            base_dir = os.path.abspath(os.path.expanduser(project_path))
            if not os.path.exists(base_dir):
                try:
                    os.makedirs(base_dir, exist_ok=True)
                    tui.append_stream(f"Created project directory: {base_dir}")
                except Exception as e:
                    return f"❌ Error: Could not create project directory: {base_dir}\n{e}"
            else:
                tui.append_stream(f"Using existing directory: {os.path.basename(base_dir)}")
        elif skip_template:
            # CONTENT_ONLY mode: Use a temporary directory instead of creating a project
            import tempfile
            base_dir = tempfile.mkdtemp(prefix="vaf_content_")
            tui.append_stream("Content-only mode: Using temporary directory")
        else:
            # Normal mode: first try to extract an explicit absolute path from the task text,
            # then fall back to keyword-based directory generation.
            _explicit_path = None
            try:
                import re as _re_pb
                # Match "im Verzeichnis /path", "in directory /path", "in folder /path", etc.
                _m = _re_pb.search(
                    r'(?:im\s+Verzeichnis|in\s+directory|in\s+folder|path\s*[:=])\s*([/\\][^\s\n"\'.,)]+)',
                    task, _re_pb.IGNORECASE
                )
                if not _m:
                    # Bare absolute path (Unix home/tmp/mnt or Windows C:\)
                    _m = _re_pb.search(r'(/(?:home|tmp|mnt|root)/\S+|C:\\[^\s\n"\'.,)]+)', task)
                if _m:
                    _explicit_path = os.path.abspath(
                        os.path.expanduser(_m.group(1).rstrip('.,)/\\'))
                    )
                    # A bare path mentioned in the task may include the filename
                    # (".../index.html in /home/mert") or point to an unsafe dir.
                    if _explicit_path and is_unsafe_project_dir(_explicit_path):
                        _explicit_path = None
            except Exception:
                pass

            if _explicit_path:
                base_dir = _explicit_path
                tui.append_stream(f"Using path from task: {base_dir}")
            else:
                # Check session's last project path before generating a new directory
                _session_proj = self._get_session_project_path(task)
                if _session_proj:
                    base_dir = _session_proj
                    tui.append_stream(f"Continuing existing project: {os.path.basename(_session_proj)}")
                else:
                    base_dir = self._determine_base_dir(task)

            os.makedirs(base_dir, exist_ok=True)
            tui.append_stream(f"Project directory: {os.path.basename(base_dir)}")
        
        time.sleep(0.05)
        
        # Initialize Git repository if not already initialized (skip for CONTENT_ONLY)
        if not skip_template:
            tui.set_action("Git initialization...")
            self._ensure_git_repo(base_dir)
            
        # Initialize Persistence for TaskManager
        task_mgr.initialize(base_dir)

        # Snapshot of visible files at run start — lets the WebUI file tree
        # distinguish files added (A) vs modified (M) during this run.
        try:
            _initial_file_names = {
                _e for _e in os.listdir(base_dir)
                if not _e.startswith('.') and os.path.isfile(os.path.join(base_dir, _e))
            }
        except Exception:
            _initial_file_names = set()
        
        # Add Git tools with base_dir context (create wrappers that pass base_dir)
        def make_git_tool_wrapper(tool_class, base_dir):
            """Create a wrapper that automatically passes base_dir to the tool."""
            class GitToolWrapper:
                def __init__(self, tool, base_dir):
                    self.tool = tool
                    self.base_dir = base_dir
                
                def run(self, **kwargs):
                    kwargs['base_dir'] = self.base_dir
                    return self.tool.run(**kwargs)
            
            return GitToolWrapper(tool_class(), base_dir)
        
        # Add Git tools with base_dir context (only if not CONTENT_ONLY)
        if not skip_template:
            self.local_tools["git_init"] = make_git_tool_wrapper(GitInitTool, base_dir)
            self.local_tools["git_add_commit"] = make_git_tool_wrapper(GitAddCommitTool, base_dir)
            self.local_tools["git_status"] = make_git_tool_wrapper(GitStatusTool, base_dir)
            self.local_tools["git_log"] = make_git_tool_wrapper(GitLogTool, base_dir)

            # Version history + safe rollback (coder-owned; also reachable for the
            # model so it can restore a known-good state at its own discretion)
            from vaf.tools.project_git import ProjectHistoryTool, ProjectRollbackTool
            self.local_tools["project_history"] = make_git_tool_wrapper(ProjectHistoryTool, base_dir)
            self.local_tools["project_rollback"] = make_git_tool_wrapper(ProjectRollbackTool, base_dir)
            
            # Add Knowledge Tools (wrapped to inject base_dir)
            from vaf.tools.knowledge import UpdateCodexTool, AddMemoryTool
            self.local_tools["update_codex"] = make_git_tool_wrapper(UpdateCodexTool, base_dir)
            self.local_tools["add_memory"] = make_git_tool_wrapper(AddMemoryTool, base_dir)
        
        # ═══════════════════════════════════════════════════════════════════
        # TEMPLATE ANALYSIS - Use LLM with own context BEFORE starting work
        # ═══════════════════════════════════════════════════════════════════
        
        template_type = None
        template_files = []

        # EDIT MODE GUARD: never apply templates to a project that already has
        # code files. TemplateManager.generate_files() writes into base_dir and
        # would overwrite the user's work — an "add the Impressum" follow-up run
        # once replaced a finished website with placeholder scaffolding because
        # the task text contained "Website" and the keyword fallback forced the
        # template. Existing projects always go through normal planning, where
        # the fresh task context injects the existing file list for editing.
        _existing_code_exts = (
            '.html', '.css', '.js', '.ts', '.py', '.java', '.cpp', '.c',
            '.go', '.rs', '.sh', '.json',
        )
        _existing_code_files = []
        try:
            for _entry in os.listdir(base_dir):
                if _entry.startswith('.'):
                    continue
                _p = os.path.join(base_dir, _entry)
                if os.path.isfile(_p) and os.path.splitext(_entry)[1].lower() in _existing_code_exts:
                    _existing_code_files.append(_entry)
        except Exception:
            pass

        if _is_api_mode:
            tui.append_stream("[INFO] API provider detected — skipping template (not needed for capable models)")
        elif _existing_code_files and not skip_template:
            tui.append_stream(
                f"[INFO] Existing project with {len(_existing_code_files)} code file(s) - "
                f"skipping template (edit mode)"
            )
            try:
                if lg:
                    lg.event(
                        "template_skipped_existing_project",
                        existing_files=_existing_code_files[:10],
                    )
            except Exception:
                pass

        if not skip_template and not _is_api_mode and not _existing_code_files:
            tui.set_action("Analyzing task for template...")
            # CRITICAL: Force immediate update BEFORE blocking operation
            tui.append_stream("[INFO] Starting template selection...")
            if user_template_preference:
                pref_labels = {
                    "no_template": "User prefers NO template",
                    "content_only": "User wants CONTENT_ONLY",
                    "simple": "User wants SIMPLE/MINIMAL"
                }
                tui.append_stream(f"[HINT] {pref_labels.get(user_template_preference, user_template_preference)} (LLM will decide)")
            tui._needs_update = True  # Force animation thread to update immediately
            # CRITICAL: Force immediate update (outside lock, safe)
            try:
                live.update(tui.render())
            except Exception:
                pass  # Don't fail if render is blocked
            time.sleep(0.15)  # Give animation thread time to render before blocking operation
            
            # Use LLM to intelligently detect template type (has its own context)
            # This runs BEFORE the main coding work begins
            # Use snippet to prevent context overflow/hangs
            task_snippet = task[:1000]
            
            # CRITICAL: Add timeout wrapper to prevent hanging
            # Pass user preference as hint to LLM (Option B: LLM gets hint but decides)
            try:
                template_type, decision_info = TemplateManager.detect_template_type_with_llm(task_snippet, user_template_preference)
            except requests.exceptions.Timeout:
                tui.append_stream("[WARN] Template selection timed out - continuing without template")
                template_type, decision_info = None, "Template selection timed out - will create from scratch"
            except Exception as e:
                tui.append_stream(f"[WARN] Template selection failed: {str(e)[:50]} - continuing without template")
                template_type, decision_info = None, f"Template selection failed - will create from scratch"
            
            # ═══════════════════════════════════════════════════════════════════
            # FALLBACK: Keyword-Based Template Detection
            # ═══════════════════════════════════════════════════════════════════
            # If LLM returned None, but task clearly matches a template, force it!
            # This prevents workflow instructions from confusing the LLM
            if not template_type:
                task_lower = task.lower()
                # Check for clear template indicators
                if any(kw in task_lower for kw in ['website', 'webseite', 'webpage', 'homepage', 'web page', 'landing page', '.html', 'index.html', 'html datei', 'html file']):
                    template_type = "website"
                    decision_info += "\n\n[FALLBACK] Keyword detection overrode LLM decision"
                    decision_info += "\nDetected keywords: website/html → Forcing 'website' template"
                    tui.append_stream("[FALLBACK] Keyword match detected → Forcing 'website' template")
                elif any(kw in task_lower for kw in ['python script', 'python skript', '.py script']):
                    template_type = "python_script"
                    decision_info += "\n\n[FALLBACK] Keyword detection overrode LLM decision"
                    decision_info += "\nDetected keywords: python script → Forcing 'python_script' template"
                    tui.append_stream("[FALLBACK] Keyword match detected → Forcing 'python_script' template")

            # Output detailed decision process
            # CRITICAL: Force immediate update to show template selection
            tui.append_stream("─" * 60)
            tui.append_stream("[SEARCH] Template Selection Process:")
            for line in decision_info.split('\n'):
                if line.strip():
                    tui.append_stream(f"  {line}")
            tui.append_stream("─" * 60)

            if template_type:
                tui.append_stream(f"[OK] Selected template: {template_type}")
            else:
                tui.append_stream("[INFO] No template selected")
                tui.append_stream("-> Will use web_deep_search to research implementation")
                tui.append_stream("-> Then create TODO list and implement from scratch")
            
            # CRITICAL: Force immediate update after all messages
            tui._needs_update = True  # Force animation thread to update
            time.sleep(0.3)  # Give animation thread time to render all messages
            
            # DEBUG: Verify buffer has content (only in development)
            with tui._lock:
                if not tui.stream_buffer:
                    # Buffer is empty - this shouldn't happen!
                    tui.append_stream("[WARN] WARNING: Buffer was empty after template selection!")
                    tui._needs_update = True  # Force update
                    time.sleep(0.1)  # Give time to render
        
        # ═══════════════════════════════════════════════════════════════════
        # FIX: Template-Auswahl TUI-Anzeige (2024)
        # ═══════════════════════════════════════════════════════════════════
        # PROBLEM: Template-Auswahl-Meldungen wurden nicht in der TUI angezeigt,
        # weil die blockierende LLM-Anfrage (detect_template_type_with_llm) den
        # Hauptthread blockierte, bevor die Meldungen gerendert werden konnten.
        #
        # LÖSUNG:
        # 1. Flag-basierte Updates: Statt direkter live.update() Aufrufe (die
        #    Deadlocks verursachen können), setzen wir tui._needs_update = True
        #    und lassen den Animation Thread die Updates übernehmen.
        # 2. Animation Thread startet JETZT direkt nach live.start() (Zeile 1494),
        #    nicht erst nach der Template-Auswahl. Das stellt sicher, dass die
        #    Animation auch während blockierender Operationen läuft.
        # 3. Längere Wartezeiten (0.2-0.3s) geben dem Animation Thread Zeit,
        #    die Meldungen zu rendern, bevor die nächste blockierende Operation
        #    startet.
        # 4. Alle Template-Auswahl-Meldungen werden mit append_stream() hinzugefügt
        #    und bleiben im stream_buffer, auch wenn die LLM-Anfrage blockiert.
        #
        # ERGEBNIS: Template-Auswahl-Meldungen sind jetzt in der TUI sichtbar,
        #           auch während der blockierenden LLM-Anfrage.
        # ═══════════════════════════════════════════════════════════════════
        
        if template_type:
            tui.set_action(f"Template: {template_type}")
            live.update(tui.render())
            
            # Extract placeholders from task
            placeholders = TemplateManager.extract_placeholders_from_task(task, template_type)
            
            # Generate template files
            template_files = TemplateManager.generate_files(template_type, base_dir, placeholders)
            
            for f in template_files:
                fname = os.path.basename(f)
                size = os.path.getsize(f)
                tui.add_file(fname, size, "done")
            
            tui.append_stream(f"{len(template_files)} template files")
            # append_stream() now triggers live.update() automatically via callback

        # ═══════════════════════════════════════════════════════════════════
        # GUIDED TEMPLATE MODE - Helper Functions
        # ═══════════════════════════════════════════════════════════════════

        def generate_guided_todos(template_files: list, task_description: str) -> list:
            """
            Generate ACTION-ORIENTED step-by-step todos for template modification.
            Each task is concrete and action-focused, not vague.
            This creates a "rail-guided" workflow that even 4B models can follow.
            """
            todos = []

            # Extract key info from task for better context
            task_short = task_description[:80] if len(task_description) > 80 else task_description

            for tf in template_files:
                fname = os.path.basename(tf)

                # Task 1: Read (concrete action)
                todos.append(f"Call read_file to read {fname} and identify all {{{{PLACEHOLDERS}}}}")

                # Task 2: Replace (concrete action with context)
                if fname.endswith('.html'):
                    todos.append(f"Call write_file to replace placeholders in {fname} with content for: {task_short}")
                elif fname.endswith('.css'):
                    todos.append(f"Call write_file to update styles in {fname} if needed for: {task_short}")
                elif fname.endswith('.js'):
                    todos.append(f"Call write_file to update JavaScript in {fname} if needed for: {task_short}")
                else:
                    todos.append(f"Call write_file to update {fname} with content for: {task_short}")

            # Final verification step (concrete action)
            todos.append(f"Call read_file to verify all placeholders are replaced with real content")

            return todos

        def create_guided_task_prompt(task_idx: int, task_description: str, template_file: str, user_task: str) -> str:
            """
            Generate ultra-simple, step-by-step prompt for template tasks.
            Uses concrete examples instead of abstract rules.
            """
            fname = os.path.basename(template_file)

            # Match new action-oriented task format
            if "Call read_file to read" in task_description:
                return f"""🎯 ACTION-FOCUSED GUIDED MODE - You MUST use tools!

## TASK: {task_description}

**Main Goal:** {user_task}

**ACTION STEPS - DO NOW:**
1. Call: `read_file(path="{template_file}")`
2. Identify {{{{PLACEHOLDERS}}}} (like {{{{BUSINESS_NAME}}}}, {{{{TITLE}}}})
3. Call: `task_done(summary="Read {fname}")`

**NO THINKING - JUST DO IT!**
Tools: read_file, task_done"""

            elif "Call write_file to replace" in task_description or "Call write_file to update" in task_description:
                return f"""🎯 ACTION-FOCUSED GUIDED MODE - You MUST use tools!

## TASK: {task_description}

**Main Goal:** {user_task}

**ACTION STEPS - DO NOW:**
1. Replace ALL {{{{PLACEHOLDERS}}}} with content for: "{user_task}"
2. Call: `write_file(path="{template_file}", content="[FULL content]")`
3. Call: `task_done(summary="Updated {fname}")`

**TEMPLATE PRESERVATION (CRITICAL):**
✅ ONLY replace {{{{PLACEHOLDER}}}} text
✅ KEEP ALL tags (<div>, <section>, <nav>)
✅ KEEP ALL class/ID names
❌ NO structure changes!

**Example:**
Before: `<div class="logo">{{{{NAME}}}}</div>`
After:  `<div class="logo">Hair Salon</div>` ✅
NOT:    `<h1>Hair Salon</h1>` ❌ (changed tag!)

**NO THINKING - CALL WRITE_FILE NOW!**
Tools: write_file, read_file, task_done"""

            elif "verify" in task_description.lower() or "Call read_file to verify" in task_description:
                return f"""🎯 ACTION-FOCUSED GUIDED MODE - Final Verification!

## TASK: {task_description}

**Main Goal:** {user_task}

**ACTION STEPS - DO NOW:**
1. Call `read_file(path="...")` for EACH file
2. Check: ALL {{{{PLACEHOLDERS}}}} replaced?
3. Check: Structure intact?
4. Call: `task_done(summary="Verified")`

**Files to check:**
{chr(10).join(['- ' + os.path.basename(tf) for tf in template_files])}

**NO THINKING - READ FILES NOW!**
Tools: read_file, task_done"""

            else:
                # Fallback for any other task
                return f"""You are working on task: {task_description}

**Tools available:** read_file, write_file, task_done"""

        # ═══════════════════════════════════════════════════════════════════
        # Animation thread is already started immediately after live.start()
        # No need to start it again here
        # ═══════════════════════════════════════════════════════════════════

        def stop_live():
            """Stop live display cleanly."""
            animation_running.clear()
            try:
                live.stop()
            except Exception:
                pass

        # ═══════════════════════════════════════════════════════════════════
        # SYSTEM PROMPT
        # ═══════════════════════════════════════════════════════════════════
        
        tui.set_action("Building prompt...")
        live.update(tui.render())
        existing_files_info = ""
        
        if template_files:
            # Check if this is a script template (flexible) or structure template (strict)
            is_script_template = template_type in ['python_script', 'python_cli', 'java_application', 'node_app']
            
            if is_script_template:
                # FLEXIBLE RULES for scripts
                existing_files_info = f"""
## 📄 TEMPLATE FILES CREATED
The following files were created as a starting point:
{chr(10).join(['- ' + os.path.basename(f) for f in template_files])}

### ✅ TEMPLATE INSTRUCTIONS (FLEXIBLE):
1. **READ FIRST**: `read_file` the template to understand the structure.
2. **EXPAND & REWRITE**: You are FREE to add imports, functions, classes, and logic.
3. **IMPLEMENT TASK**: The template is just a skeleton. You MUST fill it with the actual logic for: "{task}"
4. **REPLACE PLACEHOLDERS**: Replace any `{{...}}` placeholders with real code/values.
"""
            else:
                # SOFT GUIDANCE for websites/servers (template as starting point)
                existing_files_info = f"""
## 📋 TEMPLATE FILES AVAILABLE AS REFERENCE

The following template files exist as a starting point:
{chr(10).join(['- ' + os.path.basename(f) for f in template_files])}

### 💡 RECOMMENDED WORKFLOW:

**STEP 1: READ FIRST** - `read_file(path="...")` for each template file to understand the structure
**STEP 2: CUSTOMIZE** - Replace `{{PLACEHOLDER}}` text with real content for the task
**STEP 3: ADAPT AS NEEDED** - You are free to add, remove, or restructure sections as the task requires
**STEP 4: WRITE FILES** - Write the final files with `write_file`

### 📌 GUIDANCE (not strict rules):
- The template provides a good baseline structure (nav, hero, services, about, contact, footer)
- Feel free to modify the structure if the task calls for it
- Replace all `{{PLACEHOLDER}}` markers with actual content
- The template is a starting point, not a constraint
"""
        
        system_prompt = f"""<identity>
You are a Senior software developer Sub-agent. Your task is to complete coding tasks autonomously and efficiently.
</identity>

<context>
project_directory: {base_dir}
All file paths must be OS-independent (use forward slashes or Path objects).
</context>

<goal>
Complete this task: "{task}"
</goal>

<tools>
- `set_todos(tasks=[...])`: REQUIRED FIRST step - break down the task into specific subtasks.
- `web_search(query)`: Search web for docs, examples, or research BEFORE planning (optional).
- `write_file(path, content)`: Create/update files - YOU MUST CALL THIS to actually create code.
- `read_file(path)`: Read existing files.
- `task_done(summary)`: Mark current task complete - ONLY call after you've actually written files.
</tools>

<philosophy>
**YOU ARE A DOER, NOT A TALKER!**
- EVERY response MUST include at least ONE tool call (read_file, write_file, task_done, etc.)
- Explaining what you'll do WITHOUT doing it = FAILURE
- Short thinking → Immediate action → Results
- If you find yourself writing long explanations, STOP and use tools instead!
</philosophy>

<workflow>
1. **RESEARCH** (optional): Use `web_search` if you need docs/examples BEFORE planning.
2. **PLAN**: Call `set_todos` with a list of specific steps.
3. **ACT**: Use `write_file` to CREATE the actual code files - this is MANDATORY!
4. **VERIFY**: Use `read_file` if needed to check existing code.
5. **FINISH**: Call `task_done` ONLY after you've actually written files with `write_file`.
</workflow>

<rules>
- **ACTION REQUIRED**: Every response MUST contain at least one tool call - no exceptions!
- **IMPORTANT**: If you use `web_search` for research, you MUST then call `set_todos` immediately - do not loop searches!
- You MUST call `write_file` before calling `task_done` - no exceptions!
- Thinking about code or describing code is NOT enough - you must actually create files.
- DO NOT call `task_done` without first calling `write_file` for the current task.
- Work on ONE task at a time, complete it fully, then move to the next.
- **REMEMBER YOUR TASK**: You are working on: "{task}" - keep this in mind with every action!
</rules>

<planning_rules>
- **Website / HTML tasks → ALWAYS use separate files**: For any website or HTML project, write CSS in a separate `styles.css` file and reference it from `index.html` via `<link>`. NEVER put large blocks of CSS inline in HTML — this causes output truncation. Each file = one task.
- **Single-file output → 1 task only**: Only for non-website single-file deliverables (e.g. a Python script, a config file). Do NOT split into sub-tasks like "Add CSS", "Add JavaScript", "Verify".
- **Multi-file output → one task per output file**: Create exactly one task per file you need to write (e.g. styles.css, index.html, app.js). Execute them in whatever order makes sense. Write ONLY the file named in the current task — do not rewrite files from previous tasks.
- **NO planning tasks**: NEVER create tasks like "Plan the structure", "Design the layout", or "Review". Every task MUST result in at least one `write_file` call. Planning is mental — do it before calling `set_todos`.
- **NO meta-files**: NEVER write planning documents (PLAN.md, STRUCTURE.md, NOTES.md, TODO.md, etc.) to the project directory. These pollute the deliverable. Plan mentally, then write code files only.
</planning_rules>
"""
        # ═══════════════════════════════════════════════════════════════════
        # GUIDED TEMPLATE MODE - Auto-Generate TODOs
        # ═══════════════════════════════════════════════════════════════════
        # If templates exist, we automatically generate step-by-step tasks
        # This creates a "rail-guided" workflow that even small models can follow

        guided_mode = bool(template_files)  # Flag for later use

        if guided_mode:
            # AUTO-GENERATE todos for template mode
            auto_todos = generate_guided_todos(template_files, task)
            tui.append_stream(f"[GUIDED MODE] Auto-generated {len(auto_todos)} step-by-step tasks")

            # Build simplified user message for guided mode
            user_msg = f"Task: {task}\n\n"
            user_msg += "✅ GUIDED TEMPLATE MODE ACTIVATED!\n\n"
            user_msg += "I've prepared a step-by-step guide with specific tasks for you.\n"
            user_msg += "Each task tells you EXACTLY what to do - just follow the instructions.\n\n"
            user_msg += f"**Your tasks ({len(auto_todos)} steps):**\n"
            for i, todo in enumerate(auto_todos):
                user_msg += f"{i+1}. {todo}\n"
            user_msg += "\n**START NOW with Task 1.**\n"
            user_msg += "The system prompt will give you detailed instructions for each step."
        else:
            # NORMAL MODE - LLM must plan
            user_msg = f"Task: {task}\n\n"
            user_msg += "⚠️ IMPORTANT: Your FIRST action MUST be to call `set_todos` with your task breakdown!\n"
            user_msg += "You CANNOT use write_file or other tools until you've set your TODO list.\n\n"
            user_msg += "Start now by calling `set_todos` with your task breakdown (as many tasks as needed)."
        
        # ═══════════════════════════════════════════════════════════════
        # HIERARCHICAL CONTEXT STRUCTURE
        # ═══════════════════════════════════════════════════════════════
        # 1. MAIN CONTEXT (Coding Agent) - For Template + Task List (set_todos phase)
        # 2. TASK CONTEXTS - Each task gets its own ContextManager + History
        
        from vaf.core.context import ContextManager
        from vaf.core.config import Config
        
        # Use same max_tokens as main agent (from user config)
        max_tokens = Config.get("n_ctx", 8192)
        
        # ═══════════════════════════════════════════════════════════════
        # CONTEXT STATE INITIALIZATION (Hierarchical Context Architecture)
        # ═══════════════════════════════════════════════════════════════
        # Main Context: For Template/Planning phase (set_todos)
        # Task Contexts: Each task gets fresh ContextManager + History

        main_context_manager = ContextManager(max_tokens=max_tokens)
        main_history = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg}
        ]

        # Initialize Main ContextState (needed before switch_to_task_context)
        current_state = ContextState(
            context_manager=main_context_manager,
            history=main_history,
            phase="main"
        )

        # ═══════════════════════════════════════════════════════════════════
        # GUIDED MODE: Auto-set TODOs and immediately switch to Task 1
        # ═══════════════════════════════════════════════════════════════════
        guided_mode_skipped_planning = False
        if guided_mode:
            # Automatically set the generated TODOs
            task_mgr.set_todos(auto_todos)
            tui.append_stream(f"[GUIDED MODE] TODOs automatically set ({len(auto_todos)} tasks)")
            tui.append_stream(f"[GUIDED MODE] Skipping planning phase - switching to Task 1")
            # Set flag to skip the planning phase in main loop
            guided_mode_skipped_planning = True

        # Backup for rollback on errors
        last_stable_state = current_state.clone()

        # Storage for all context states (main + tasks)
        context_states: Dict[str, ContextState] = {
            "main": current_state
        }

        # Legacy support - these will be kept in sync with current_state
        # For compatibility with existing code that uses these variables
        current_context_manager = current_state.context_manager
        history = current_state.history
        history_snapshot_len = len(history)

        # Helper function to sync legacy variables
        def sync_legacy_vars():
            """Sync legacy variables with current_state."""
            nonlocal current_context_manager, history, history_snapshot_len
            current_context_manager = current_state.context_manager
            history = current_state.history
            history_snapshot_len = len(history)
        
        # ═══════════════════════════════════════════════════════════════
        # HELPER: Context Switch Functions with Rollback
        # ═══════════════════════════════════════════════════════════════

        def switch_to_task_context(task_idx: int, task_description: str) -> bool:
            """
            Switch to task context with rollback on failure.
            Returns True on success, False on failure.
            """
            nonlocal current_state, last_stable_state, context_states

            try:
                # Save current state for rollback
                last_stable_state = current_state.clone()

                # Check if task context already exists
                task_phase = f"task_{task_idx}"
                if task_phase in context_states:
                    # Reuse existing task context
                    current_state = context_states[task_phase]
                    tui.append_stream(f"🔄 Resumed Task {task_idx+1} context")
                else:
                    # Create fresh task context
                    completed_info = _build_completed_info()
                    task_cm, task_hist = create_fresh_context_for_task(
                        task_idx, task_description, completed_info
                    )

                    # Create new state
                    new_state = ContextState(
                        context_manager=task_cm,
                        history=task_hist,
                        phase=task_phase,
                        task_idx=task_idx
                    )

                    # Store and activate
                    context_states[task_phase] = new_state
                    current_state = new_state

                    tui.append_stream(f"🔄 Switched to Task {task_idx+1} context (fresh)")

                # Sync legacy variables for compatibility
                sync_legacy_vars()

                # Log context switch
                try:
                    if lg:
                        lg.event("context_switch",
                            from_phase=last_stable_state.phase,
                            to_phase=current_state.phase,
                            task_idx=task_idx,
                            success=True)
                except Exception:
                    pass

                return True

            except Exception as e:
                # Rollback to last stable state
                tui.append_stream(f"[ERROR] Context switch failed: {str(e)[:100]}")
                current_state = last_stable_state
                sync_legacy_vars()

                try:
                    if lg:
                        lg.event("context_switch_failed",
                            error=str(e),
                            rolled_back=True)
                except Exception:
                    pass

                return False

        def _build_completed_info() -> str:
            """Build summary of completed tasks for context injection."""
            if not task_mgr.todos:
                return ""

            completed = []
            for i, todo in enumerate(task_mgr.todos):
                if todo["status"] == "completed":
                    result = todo.get("result", "done")
                    completed.append(f"✅ Task {i+1}: {todo['task']} - {result}")

            if not completed:
                return ""

            return "\n".join(completed)

        def _build_state_glue() -> str:
            """Build Context Glue summary from current state."""
            glue = "### 📁 PROJECT STATE\n"

            if files_created:
                glue += f"**Created:** {', '.join([os.path.basename(f) for f in files_created[:10]])}\n"
                if len(files_created) > 10:
                    glue += f"... and {len(files_created)-10} more files\n"
            else:
                glue += "**No files created yet**\n"

            glue += f"\n### 🎯 CURRENT PROGRESS\n"
            glue += f"Phase: {current_state.phase}\n"
            if current_state.is_task():
                glue += f"Task {current_state.task_idx + 1}: {task_mgr.get_current_task()}\n"
                glue += f"Files in this context: {len(current_state.files_created)}\n"

            return glue

        # ═══════════════════════════════════════════════════════════════
        # HELPER: Create fresh context for a new task (with new ContextManager)
        # ═══════════════════════════════════════════════════════════════
        def create_fresh_context_for_task(task_idx: int, current_task: str, completed_info: str = "") -> tuple[ContextManager, List[Dict[str, Any]]]:
            """
            Creates a completely fresh context for a new task.
            This isolates each task with its own ContextManager and history, preventing confusion from previous tasks.

            Args:
                task_idx: Index of the current task
                current_task: Description of the current task
                completed_info: Information about previously completed tasks to provide continuity

            Returns:
                (ContextManager, List[Dict]): New ContextManager and fresh history for this task
            """
            # Create NEW ContextManager for this task (isolated from other tasks)
            task_context_manager = ContextManager(max_tokens=max_tokens)
            
            # ... (rest of detection logic)
            task_lower = current_task.lower()
            is_html_task = any(kw in task_lower for kw in ['html', 'website', 'webpage', 'web seite'])
            is_script_task = any(kw in task_lower for kw in ['script', 'python', 'skript', '.py'])
            
            # Format completed info if present
            completed_section = ""
            if completed_info:
                completed_section = f"\n## PROGRESS SO FAR\nYou have already completed these tasks:\n{completed_info}\n"

            # Rebuild existing_files_info (in case template files changed)
            fresh_existing_files_info = ""
            # ... (template logic stays same)

            if template_files:
                # Generic template rules that work for all file types
                template_file_list = chr(10).join(['- ' + os.path.basename(f) for f in template_files])
                template_example = ""
                
                # Add type-specific examples
                if is_html_task:
                    template_example = """
**Example (HTML):**
Template: `<nav class="nav"><div class="logo">{{BUSINESS_NAME}}</div></nav>`
✅ Correct: `<nav class="nav"><div class="logo">Testler Handwerksmeister</div></nav>` (only replaced placeholder)
❌ Wrong: `<header><h1>Testler Handwerksmeister</h1></header>` (removed nav structure - will be BLOCKED!)"""
                elif is_script_task:
                    template_example = """
**Example (Python Script):**
Template: `def {{FUNCTION_NAME}}({{PARAMS}}):\n    # {{DESCRIPTION}}\n    pass`
✅ Correct: `def process_data(file_path):\n    # Process the input file\n    pass` (only replaced placeholders)
❌ Wrong: `def new_function(): pass` (removed template structure - will be BLOCKED!)"""
                else:
                    template_example = """
**Example:**
Template: `{{PLACEHOLDER}}`
✅ Correct: Replace `{{PLACEHOLDER}}` with actual content, keep all other structure
❌ Wrong: Remove template structure or rewrite from scratch - will be BLOCKED!"""
                
                fresh_existing_files_info = f"""
## ⚠️ CRITICAL: TEMPLATE FILES EXIST - DO NOT REPLACE THEM!

The following files were already created from a template:
{template_file_list}

### 🚨 MANDATORY TEMPLATE WORKFLOW (Templates are REQUIRED structure!):

**STEP 1: READ FIRST** - `read_file(path="...")` for EVERY template file BEFORE modifying
**STEP 2: PRESERVE ALL** - Keep ALL structure, classes, IDs, functions, imports, etc. from template
**STEP 3: ONLY REPLACE** - Replace `{{PLACEHOLDER}}` text with real content - nothing else
**STEP 4: WRITE BACK** - Write modified version (not complete rewrite)

### ❌ FORBIDDEN:
- DO NOT rewrite from scratch
- DO NOT remove sections, functions, classes, imports, or structural elements
- DO NOT ignore template structure
- DO NOT change file structure unless explicitly required by the task

### ✅ CORRECT:
{template_example}

**If you remove template structure, write_file will be BLOCKED!**
"""
            
            # ═══════════════════════════════════════════════════════════════════
            # GUIDED MODE: Use Ultra-Simple Task-Specific Prompts
            # ═══════════════════════════════════════════════════════════════════
            if guided_mode:
                # Use the simplified, step-by-step prompts for guided mode
                # This maps to the specific template file for this task
                file_idx = task_idx // 2  # Each file gets 2 tasks (read, replace)
                if file_idx < len(template_files):
                    template_file = template_files[file_idx]
                else:
                    template_file = template_files[0]  # Fallback to first file

                fresh_system_prompt = create_guided_task_prompt(task_idx, current_task, template_file, task)
                fresh_user_msg = f"Start working on this task now. Follow the exact steps in the system prompt."
            else:
                # ═══════════════════════════════════════════════════════════════════
                # NORMAL MODE: Use Complex Full System Prompt
                # ═══════════════════════════════════════════════════════════════════
                
                # Load persistent context (Ralph-style persistence)
                codex_content = task_mgr.pm.get_codex() if task_mgr.pm else ""
                memory_content = task_mgr.pm.get_memory() if task_mgr.pm else ""
                
                persistent_context = ""
                if codex_content:
                    persistent_context += f"\n## PROJECT CODEX (Patterns & Conventions)\n{codex_content}\n"
                if memory_content:
                    persistent_context += f"\n## SESSION MEMORY (Recent Learnings)\n{memory_content}\n"

                # Scan project dir for already-created CODE files (exclude infrastructure files
                # like .gitignore, .vaf/, .git/ — the model must never be told about these
                # because it will read them in a confused loop instead of writing code)
                _INFRA_FILES = {'.gitignore', '.gitattributes', '.editorconfig', '.env.example'}

                _existing_project_files = []
                try:
                    if os.path.isdir(base_dir):
                        for _fname in sorted(os.listdir(base_dir)):
                            _fpath = os.path.join(base_dir, _fname)
                            # Skip hidden dirs (.git, .vaf), hidden files, infra files, PARTIAL_ backups
                            if _fname.startswith('.') or _fname.startswith('PARTIAL_'):
                                continue
                            if _fname in _INFRA_FILES:
                                continue
                            if os.path.isfile(_fpath):
                                _existing_project_files.append(_fname)
                except Exception:
                    pass
                _existing_note = ""
                if _existing_project_files:
                    _existing_note = (
                        "\n## FILES ALREADY IN PROJECT\n"
                        "The following code files already exist — these are your deliverables:\n"
                        + "\n".join(f"- `{f}`" for f in _existing_project_files)
                        + "\n\n**RULES:**\n"
                        "- To ADD content to an existing file: use `read_file` first, then `write_file` with the COMPLETE updated content.\n"
                        "- If the file already contains everything your task requires: call `task_done` immediately.\n"
                        "- NEVER use `python_sandbox` for project file I/O — use `read_file` / `write_file` only.\n"
                    )
                else:
                    # Empty project — be very direct: write NOW, don't read infra files
                    _existing_note = (
                        "\n## PROJECT STATE\n"
                        "The project directory is **empty** — no code files exist yet.\n"
                        "**Your immediate action: call `write_file` to create the first file. Do NOT read .gitignore or any hidden files.**\n"
                    )

                # Rebuild system prompt with current state
                # IMPORTANT: Keep task-context prompts SMALL to avoid n_ctx overflow.
                # The agent may have more tools available locally, but task execution should
                # only advertise the minimal tool set required to complete the task.
                fresh_system_prompt = f"""<identity>
You are a Senior software developer Sub-agent.
</identity>

<context>
project_directory: {base_dir}
All files must be saved inside this directory.
{completed_section}
{_existing_note}
{fresh_existing_files_info}
{persistent_context}
</context>

<tools>
- `read_file(path)` - Read file contents
- `list_files(path)` - List directory contents
- `write_file(path, content)` - Create/modify files with actual code
- `web_search(query)` - Search web for docs, examples, solutions
- `task_done(summary)` - Mark task complete and move to next
- `update_codex(title, content)` - Save important patterns/conventions
- `add_memory(note)` - Save short-term notes
</tools>

<current_task>
Task {task_idx + 1}: {current_task}
</current_task>

<rules>
- Focus ONLY on the current task — write ONLY the file(s) named in it, nothing else
- Do NOT rewrite files from previous tasks (they are already done)
- Use `write_file` to actually create/modify files (do not just describe code)
- **CRITICAL**: After `web_search`, immediately use the results to call `write_file` - DO NOT just think or plan
- When finished, call `task_done(summary="...")`
- If you discover a reusable pattern, use `update_codex` to save it.
</rules>
"""

                # Build user message for this specific task (ONLY current task, not entire list)
                fresh_user_msg = f"""## YOUR CURRENT TASK (Task {task_idx + 1})

**Task:** {current_task}

**Original Project Task:** {task}

**Project Directory:** `{base_dir}`

**Instructions:**
- Focus ONLY on completing the task above
- Use the necessary tools (read_file, write_file, etc.) to complete it
- When finished, call `task_done(summary="...")` to mark it complete"""

                if template_files:
                    fresh_user_msg += f"\n\n⚠️ TEMPLATE FILES EXIST! You MUST:\n"
                    fresh_user_msg += f"1. Call `read_file` to read each template file BEFORE modifying\n"
                    fresh_user_msg += f"2. Call `write_file` to modify (not replace) each file\n"
                    fresh_user_msg += f"3. Preserve ALL template structure (sections, functions, classes, imports, etc.)"

                fresh_user_msg += "\n\nStart working on this task now."
            
            # Create fresh history (only system + user, no old history)
            task_history = [
                {"role": "system", "content": fresh_system_prompt},
                {"role": "user", "content": fresh_user_msg}
            ]
            
            # Store in task contexts (handled by switch_to_task_context now)
            # No longer needed - context_states manages this
            
            # Return both ContextManager and history
            return task_context_manager, task_history
        
        # ═══════════════════════════════════════════════════════════════════
        # DYNAMIC TOOLS SCHEMA - Based on context (MAIN vs TASK)
        # ═══════════════════════════════════════════════════════════════════
        def _dedupe_tools_schema(schema: List[Dict]) -> List[Dict]:
            """Remove duplicate tools by function name (keeps first occurrence)."""
            seen: set[str] = set()
            deduped: List[Dict] = []
            for t in schema:
                name = (t or {}).get("function", {}).get("name")
                if not name or name in seen:
                    continue
                seen.add(str(name))
                deduped.append(t)
            return deduped

        def get_tools_schema_for_context(is_main_context: bool) -> List[Dict]:
            """Get tools schema based on context type. Enforces strict phase separation."""

            # ═══════════════════════════════════════════════════════════════════
            # GUIDED MODE TOOL RESTRICTION
            # ═══════════════════════════════════════════════════════════════════
            # In guided mode with templates, restrict tools to minimal set
            # This prevents small models from getting confused by too many options
            if guided_mode and bool(task_mgr.todos) and not is_main_context:
                # GUIDED EXECUTION: Only these 3 tools
                return [
                    {
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "description": "Read a file. Use start_line/end_line to read large files in sections (e.g. start_line=1 end_line=100, then start_line=101 end_line=200). Returns '[Lines X-Y of N total]' header so you know the total line count.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "start_line": {"type": "integer", "description": "First line (1-indexed). Omit to read from beginning."},
                                    "end_line": {"type": "integer", "description": "Last line (inclusive). Omit to read to end of file."},
                                },
                                "required": ["path"]
                            }
                        }
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "description": "Write content to a file. Keep the structure intact, only replace placeholders.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": f"Absolute path (use {base_dir}/)"},
                                    "content": {"type": "string", "description": "Complete file content"}
                                },
                                "required": ["path", "content"]
                            }
                        }
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "task_done",
                            "description": "Mark current task as complete and move to next task.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "summary": {"type": "string", "description": "Brief summary"}
                                },
                                "required": ["summary"]
                            }
                        }
                    }
                ]

            # ═══════════════════════════════════════════════════════════════════
            # NORMAL MODE (No guided template mode)
            # ═══════════════════════════════════════════════════════════════════

            # Common tools (Research/Read-only) available in both phases
            common_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file. Use start_line/end_line to read large files in sections. Returns '[Lines X-Y of N total]' header so you know the total line count.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "start_line": {"type": "integer", "description": "First line (1-indexed). Omit to read from beginning."},
                                "end_line": {"type": "integer", "description": "Last line (inclusive). Omit to read to end of file."},
                            },
                            "required": ["path"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "list_files",
                        "description": "List directory contents.",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"]
                        }
                    }
                }
            ]

            if is_main_context:
                # PLANNING PHASE: ONLY set_todos + read tools
                # write_file and task_done are HIDDEN to force planning
                return [
                    {
                        "type": "function",
                        "function": {
                            "name": "set_todos",
                            "description": (
                                "REQUIRED FIRST ACTION: Set your TODO list for this task. "
                                "**ONLY available in planning phase.** "
                                "Call this FIRST with a list of specific subtasks. "
                                "Once TODOs are set, this tool is no longer available in task execution context. "
                                "RULE: If the result is a single file (e.g. one HTML with inline CSS+JS), use EXACTLY ONE task. "
                                "NEVER create planning tasks (Plan structure, Design layout, Verify) — every task must write at least one file. "
                                "NEVER create meta-files like PLAN.md or STRUCTURE.md."
                            ),
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "tasks": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "List of file-creation tasks. Single-file projects: exactly 1 task. Multi-file: one task per output file. e.g. ['Create complete index.html with all sections, inline CSS and inline JS']"
                                    }
                                },
                                "required": ["tasks"]
                            }
                        }
                    }
                ] + common_tools
            else:
                # EXECUTION PHASE: write_file + task_done + web_search + sandbox + read tools
                # set_todos is HIDDEN to prevent re-planning loops
                return [
                    {
                        "type": "function",
                        "function": {
                            "name": "task_done",
                            "description": "Mark the current task as complete and move to the next one. Call this after completing each task.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "summary": {"type": "string", "description": "Brief summary of what was done"}
                                },
                                "required": ["summary"]
                            }
                        }
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "description": "Write content to a file.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": f"Absolute path (use {base_dir}/)"},
                                    "content": {"type": "string", "description": "File content"}
                                },
                                "required": ["path", "content"]
                            }
                        }
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "python_sandbox",
                            "description": (
                                "Execute Python code safely for calculations, data processing, or algorithms. "
                                "⛔ FORBIDDEN: Do NOT use this to read or write project files — use read_file / write_file instead. "
                                "⛔ FORBIDDEN: Do NOT write helper scripts (read_chunks.py, etc.) to the project directory. "
                                "✅ USE FOR: math, string processing, JSON parsing, algorithm testing, non-file computations."
                            ),
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "code": {
                                        "type": "string",
                                        "description": "Python code for computation only. No open(), write(), or file I/O on project files."
                                    }
                                },
                                "required": ["code"]
                            }
                        }
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "web_deep_search",
                            "description": "Search the web for solutions, documentation, or examples. Use when you need to research APIs, fix errors, or find implementation examples.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string", "description": "Search query (e.g., 'Python pandas read CSV example', 'fix React useEffect infinite loop')"},
                                    "max_results": {"type": "integer", "description": "Maximum number of results (default: 5, max: 10)", "default": 5}
                                },
                                "required": ["query"]
                            }
                        }
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "description": "Search the web for solutions, documentation, examples, or research. Alias for web_deep_search.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string", "description": "Search query"},
                                    "max_results": {"type": "integer", "description": "Maximum number of results (default: 5, max: 10)", "default": 5}
                                },
                                "required": ["query"]
                            }
                        }
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "update_codex",
                            "description": "Save a persistent pattern or convention to the project Codex (Long-term memory).",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string", "description": "Short title"},
                                    "content": {"type": "string", "description": "The pattern/rule content"}
                                },
                                "required": ["title", "content"]
                            }
                        }
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "add_memory",
                            "description": "Add a note to the session memory (Short-term scratchpad).",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "note": {"type": "string", "description": "The note to add"}
                                },
                                "required": ["note"]
                            }
                        }
                    }
                ] + common_tools
        
        # Initialize tools_schema (will be rebuilt dynamically in loop)
        tools_schema = []
        
        # ═══════════════════════════════════════════════════════════════════
        # LOAD PLUG-AND-PLAY TOOLS AUTOMATICALLY
        # ═══════════════════════════════════════════════════════════════════
        def load_plug_and_play_tools() -> List[Dict]:
            """Load all Plug-and-Play-Tools from vaf/tools/ directory automatically."""
            plug_and_play_tools = []
            
            try:
                import pkgutil
                import importlib
                import inspect
                from vaf.tools.base import BaseTool
                import vaf.tools
                
                # Tools that should NOT be available to coding agent
                EXCLUDED_TOOLS = [
                    "coding_agent",  # Prevent recursion
                    "librarian_agent",  # Different sub-agent
                    "research_agent",  # Different sub-agent
                ]
                
                # Iterate over all files in vaf/tools/
                package_path = os.path.dirname(vaf.tools.__file__)
                for _, name, _ in pkgutil.iter_modules([package_path]):
                    try:
                        # Import the module
                        module = importlib.import_module(f"vaf.tools.{name}")
                        
                        # Find classes that inherit from BaseTool
                        for _, obj in inspect.getmembers(module):
                            if inspect.isclass(obj) and issubclass(obj, BaseTool) and obj is not BaseTool:
                                instance = obj()
                                
                                # Skip excluded tools
                                if instance.name in EXCLUDED_TOOLS:
                                    continue
                                
                                # Skip tools that are already manually added
                                MANUALLY_ADDED_TOOLS = [
                                    "set_todos", "task_done", "write_file", "read_file", 
                                    "list_files", "python_sandbox", "web_fetch", "web_deep_search",
                                    "git_init", "git_add_commit", "git_status", "git_log", "bash"
                                ]
                                if instance.name in MANUALLY_ADDED_TOOLS:
                                    continue
                                
                                # Get parameters schema
                                params = getattr(instance, 'parameters', {})
                                if not isinstance(params, dict):
                                    params = {"type": "object", "properties": {}}
                                if "type" not in params:
                                    params["type"] = "object"
                                if "properties" not in params:
                                    params["properties"] = {}
                                
                                # Add to plug-and-play tools
                                plug_and_play_tools.append({
                                    "type": "function",
                                    "function": {
                                        "name": instance.name,
                                        "description": instance.description or f"Tool: {instance.name}",
                                        "parameters": params
                                    }
                                })
                                
                                # Also add to local_tools for execution
                                self.local_tools[instance.name] = instance
                                
                    except Exception as e:
                        # Skip tools that fail to load
                        continue
                        
            except Exception as e:
                # If loading fails, continue without plug-and-play tools
                pass
            
            return plug_and_play_tools
        
        # Load plug-and-play tools
        plug_and_play_tools = load_plug_and_play_tools()
        if plug_and_play_tools:
            tui.append_stream(f"[INFO] Loaded {len(plug_and_play_tools)} plug-and-play tool(s)")
        
        # Old tools_schema code removed - now built dynamically in loop
        # Tools are added dynamically in the loop based on context (MAIN vs TASK)
        
        # ═══════════════════════════════════════════════════════════════════
        # AGENTIC LOOP - NO MAX_STEPS!
        # ═══════════════════════════════════════════════════════════════════
        
        loop = AgenticLoop(timeout_minutes=15)
        files_created = list(template_files)  # Start with template files
        
        # Track files created per task (for validation)
        task_file_map: Dict[int, List[str]] = {}  # task_idx -> [list of files]
        write_file_calls_in_session = 0  # Track total write_file calls
        write_file_calls_in_last_3_loops = 0  # Track recent activity
        recent_loop_write_files = []  # Track write_file calls per loop

        # Planning-rule enforcement: single-file deliverable -> exactly 1 task.
        # First violation is rejected with instructions; the second one is
        # auto-coerced so a small model cannot loop in the planning phase.
        singlefile_rejections = 0

        # Stuck/goal-verification state: per-task retry budget plus one final
        # retry round for failed tasks at the end of the run.
        task_retry_counts: Dict[int, int] = {}
        final_retry_done = False

        def _llm_verify_call(prompt: str) -> str:
            """One bounded, non-streaming LLM call for stuck-task goal verification.

            Bounded (1000 tokens, temp 0, 90s timeout): if the model is stuck on
            the task itself, this simpler YES/NO question is still answerable;
            any error propagates to _verify_task_goal -> not verified.
            Reasoning models (e.g. Qwen) may spend their whole budget thinking
            and leave `content` empty — fall back to `reasoning_content`, the
            verdict parser picks the last YES/NO from it.
            """
            _vr_headers = {"Content-Type": "application/json"}
            if _llm_api_key:
                _vr_headers["Authorization"] = f"Bearer {_llm_api_key}"
            _vr_body = {
                "model": _llm_model or "user-model",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1000,
                "temperature": 0.0,
                "stream": False,
            }
            _vr_resp = requests.post(_llm_chat_url, headers=_vr_headers, json=_vr_body, timeout=90)
            _vr_resp.raise_for_status()
            _vr_data = _vr_resp.json()
            _vr_msg = (_vr_data.get("choices") or [{}])[0].get("message") or {}
            return _vr_msg.get("content") or _vr_msg.get("reasoning_content") or ""

        def _maybe_start_final_retry() -> bool:
            """One last retry round for failed tasks before the run ends.

            Called at every all-done exit point. If tasks failed during the main
            phase, they are reset to pending exactly once and re-attempted with
            an enriched context (completed-task summaries, project file list and
            the failure history). Returns True when a retry round was started —
            the caller must `continue` the loop instead of breaking.
            """
            nonlocal final_retry_done
            if final_retry_done:
                return False
            failed = task_mgr.failed_tasks()
            if not failed:
                return False
            final_retry_done = True

            failure_notes = []
            for t in failed:
                idx = int(t["id"]) - 1
                failure_notes.append(f"- Task {t['id']}: {t['task']} -> {t.get('result') or 'failed'}")
                task_mgr.reset_task_for_retry(idx, t.get("result") or "failed in main phase")
                # The final round is the last attempt: stuck detection fails the
                # task definitively instead of granting another inner retry.
                task_retry_counts[idx] = 1
                if hasattr(loop, 'task_start_loop'):
                    loop.task_start_loop.pop(idx, None)
                context_states.pop(f"task_{idx}", None)

            retry_idx = task_mgr.current_task_idx
            retry_title = task_mgr.get_current_task() or ""
            tui.append_stream(f"[RETRY] Final retry round: re-attempting {len(failed)} failed task(s)...")
            try:
                if lg:
                    lg.event("final_retry_started", failed_count=len(failed), first_task_idx=retry_idx)
            except Exception:
                pass

            if switch_to_task_context(retry_idx, retry_title):
                history.append({
                    "role": "system",
                    "content": (
                        "FINAL RETRY ROUND - these tasks failed earlier in this run:\n"
                        + "\n".join(failure_notes)
                        + "\n\nThis is the last attempt. Use the existing project files, make the "
                          "required changes with write_file, then call task_done."
                    ),
                })
            return True

        # WebUI live state: file tree, git, progress for the VS-Code-style
        # SubAgent window. Hash-throttled so unchanged payloads are not resent.
        _last_coder_state_hash = [None]

        def _emit_coder_state(current_file: str = ""):
            try:
                _sid = os.environ.get("VAF_SESSION_ID", "").strip()
                if not _sid:
                    from vaf.core.subagent_ipc import get_current_session_id as _gsid
                    _sid = _gsid() or ""
                if not _sid:
                    return  # no WebUI session -> nothing to feed
                # Real task list for the window's Tasks section: the generic
                # heartbeat steps only know "Sub-Agent running"; this is the
                # actual plan with live per-task status.
                _cur_idx = getattr(task_mgr, "current_task_idx", -1) if task_mgr else -1
                _tasks_payload = []
                for _ti, _td in enumerate(task_mgr.todos if task_mgr else []):
                    _tstatus = _td["status"]
                    if _tstatus in ("pending", "in_progress") and _ti == _cur_idx:
                        _tstatus = "running"
                    _tasks_payload.append({"title": _td["task"][:120], "status": _tstatus})

                payload = {
                    "fileTree": _build_file_tree(
                        base_dir, files_created, current_file, _initial_file_names
                    ),
                    "git": _build_git_state(base_dir),
                    "tasks": _tasks_payload[:12],
                    "loop": loop.loop_count,
                    "taskProgress": task_mgr.get_progress() if task_mgr else "",
                    "linterOk": not bool(getattr(current_state, 'linter_errors_active', False)),
                    "projectName": os.path.basename(base_dir),
                    "projectPath": base_dir,
                }
                payload_hash = hash(json.dumps(payload, sort_keys=True, default=str))
                if payload_hash == _last_coder_state_hash[0]:
                    return
                _last_coder_state_hash[0] = payload_hash
                from vaf.core.web_interface import get_web_interface
                get_web_interface().emit_coder_state(payload, session_id=_sid)
                if lg:
                    lg.event(
                        "coder_state_emitted",
                        files=len(payload["fileTree"]),
                        commits=len(payload["git"]["commits"]),
                        loop=payload["loop"],
                        task_progress=payload["taskProgress"],
                    )
            except Exception:
                pass

        # Live editor feed: the code currently streaming out of the model.
        # Time-throttled; content is tail-capped so huge files stay cheap.
        _last_code_emit_at = [0.0]

        def _emit_live_code(filename: str, content: str, force: bool = False):
            try:
                now = time.time()
                if not force and now - _last_code_emit_at[0] < 0.35:
                    return
                _last_code_emit_at[0] = now
                _sid = os.environ.get("VAF_SESSION_ID", "").strip()
                if not _sid:
                    from vaf.core.subagent_ipc import get_current_session_id as _gsid2
                    _sid = _gsid2() or ""
                if not _sid:
                    return
                tail = content or ""
                if len(tail) > 6000:
                    tail = tail[-6000:]
                    cut = tail.find('\n')
                    if 0 <= cut < 200:
                        tail = tail[cut + 1:]
                from vaf.core.web_interface import get_web_interface
                get_web_interface().emit_coder_code(filename or "", tail, session_id=_sid)
                if lg:
                    lg.event("live_code_emitted", file=filename, chars=len(tail))
            except Exception:
                pass
        
        # Dynamic Temperature State
        current_temp = 0.3
        consecutive_empty = 0
        
        # Task-spezifische Empty-Counter (isoliert pro Task-Thread)
        task_empty_counters: Dict[int, int] = {}  # task_idx -> consecutive_empty count
        main_empty_counter = 0  # For main context
        
        # Idle Loop Counter (Track loops without tool calls)
        idle_loop_count = 0
        
        tui.set_action("Agentic Loop")
        tui.append_stream(f"[INFO] Starting agentic loop for: {os.path.basename(base_dir)}")
        live.update(tui.render())
        
        # CRITICAL: Debug output to show loop is starting
        tui.append_stream("[INFO] Loop initialized - waiting for first LLM response...")
        live.update(tui.render())
        
        # GLOBAL TRACE LOGGER (repo-local via subagent debug logger; no thoughts/prompts)
        def _trace(msg):
            try:
                if lg:
                    lg.event("coder_trace", message=str(msg)[:1000])
            except Exception:
                pass

        _trace("=== AGENTIC LOOP STARTED ===")
        
        while True:
            loop.increment_loop()
            tui.increment_loop()
            _trace(f"--- LOOP {loop.loop_count} START ---")
            try:
                if lg:
                    current_task = task_mgr.get_current_task() if task_mgr else ""
                    lg.event(
                        "loop_start",
                        loop=loop.loop_count,
                        progress=task_mgr.get_progress() if task_mgr else "",
                        current_task_idx=getattr(task_mgr, "current_task_idx", None),
                        current_task_preview=(current_task[:180] if current_task else ""),
                        no_action_since=getattr(loop, "no_action_since", None),
                    )
            except Exception:
                pass

            # Feed the WebUI SubAgent window (file tree, git, progress).
            # Hash-throttled inside, so unchanged states cost one local hash only.
            _emit_coder_state()

            # ═══════════════════════════════════════════════════════════════
            # GUIDED MODE: Skip planning phase and go directly to Task 1
            # ═══════════════════════════════════════════════════════════════
            if guided_mode_skipped_planning:
                tui.append_stream("[GUIDED MODE] Switching to Task 1 context immediately")
                first_task = task_mgr.get_current_task()
                if first_task:
                    # Switch to Task 1 context
                    success = switch_to_task_context(0, first_task)
                    if success:
                        sync_legacy_vars()  # Update legacy variables
                        tui.append_stream(f"[GUIDED MODE] Now executing: {first_task[:50]}")
                    else:
                        tui.append_stream("[WARN] Failed to switch to Task 1 - continuing in planning mode")
                # Clear flag so we don't do this again
                guided_mode_skipped_planning = False

            # Initialize write_file tracking for this loop (will be updated if write_file is called)
            if loop.loop_count > len(recent_loop_write_files):
                recent_loop_write_files.append(False)

            # Check if we should continue
            should_continue, reason = loop.should_continue()
            if not should_continue:
                _trace(f"Loop stopped: {reason}")
                try:
                    if lg:
                        lg.event("loop_stop", loop=loop.loop_count, reason=str(reason))
                except Exception:
                    pass
                break
            
            # Update TUI
            tui.set_action(f"Loop {loop.loop_count}")
            live.update(tui.render())
            
            # ═══════════════════════════════════════════════════════════════
            # CONTEXT MANAGEMENT - Prevent token overflow (per context)
            # ═══════════════════════════════════════════════════════════════
            
            _trace("Checking context size...")
            # Proactive compression: Check token usage and compress if > 85% of limit
            estimated_tokens = current_state.context_manager.estimate_tokens(current_state.history)
            if estimated_tokens > int(current_state.context_manager.max_tokens * 0.85):
                tui.set_action(f"Proactive compression: {estimated_tokens}/{current_state.context_manager.max_tokens} tokens...")
                live.update(tui.render())

                # Compress with Context Glue preservation
                current_state.history = current_state.context_manager.compress(current_state.history)

                # Update in context_states storage
                context_states[current_state.phase] = current_state

                # Sync legacy vars
                sync_legacy_vars()

                tui.append_stream(f"[INFO] Context compressed (was {estimated_tokens} tokens)")

            # Also check normal threshold
            elif current_state.context_manager.should_compress(current_state.history):
                tui.set_action("Compressing context...")
                live.update(tui.render())

                # Compress with Context Glue preservation
                current_state.history = current_state.context_manager.compress(current_state.history)

                # Update in context_states storage
                context_states[current_state.phase] = current_state

                # Sync legacy vars
                sync_legacy_vars()
            tui.set_action(f"Loop {loop.loop_count}")
            live.update(tui.render())

            # ═══════════════════════════════════════════════════════════════
            # PROACTIVE AUTO-EXIT CHECK - Break immediately if all tasks done
            # ═══════════════════════════════════════════════════════════════
            if task_mgr and task_mgr.is_all_done():
                if _maybe_start_final_retry():
                    continue
                tui.append_stream("🎉 [AUTO-EXIT] All tasks completed!")
                _trace(f"[AUTO-EXIT] Loop {loop.loop_count}: is_all_done=True, breaking immediately")
                try:
                    if lg:
                        lg.event("auto_exit", loop=loop.loop_count, reason="all_tasks_done_proactive_check")
                except Exception:
                    pass
                break

            # ═══════════════════════════════════════════════════════════════
            # LLM REQUEST
            # ═══════════════════════════════════════════════════════════════
            
            # Get model name
            if _llm_model:
                # API mode: model already resolved from config
                model_name = _llm_model
            else:
                model_name = "user-model"
                try:
                    m_res = requests.get(_llm_models_url, timeout=2)
                    if m_res.status_code == 200:
                        data = m_res.json()
                        if 'data' in data and len(data['data']) > 0:
                            model_name = data['data'][0]['id']
                except:
                    pass

            # Clean history - MUST be properly indented!
            clean_history = []
            _think_re = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
            # Build two index sets from raw history:
            # 1. _valid_tool_call_ids: tool_call_ids present in assistant messages (used to drop
            #    orphaned role='tool' responses that have no matching tool_call).
            # 2. _responded_ids: tool_call_ids that actually have a role='tool' response
            #    (used to strip dangling tool_calls from assistant messages — an assistant
            #    message whose tool_call has no response causes "insufficient tool messages" 400).
            _valid_tool_call_ids: set = set()
            _responded_ids: set = set()
            for msg in history:
                for _tc in (msg.get("tool_calls") or []):
                    _tcid = _tc.get("id") if isinstance(_tc, dict) else None
                    if _tcid:
                        _valid_tool_call_ids.add(_tcid)
                if msg.get("role") == "tool" and msg.get("tool_call_id"):
                    _responded_ids.add(msg["tool_call_id"])
            for msg in history:
                clean_msg = {k: v for k, v in msg.items() if k in ['role', 'content', 'tool_calls', 'tool_call_id', 'name']}
                # Drop orphaned tool messages — a tool result with no matching assistant tool_call
                # causes a 400 from strict providers (DeepSeek, OpenAI).
                if clean_msg.get("role") == "tool":
                    _tcid = clean_msg.get("tool_call_id")
                    if _tcid and _tcid not in _valid_tool_call_ids:
                        continue  # Skip — orphaned
                # Strip dangling tool_calls from assistant messages — a tool_call with no
                # matching role='tool' response causes "insufficient tool messages" 400.
                # This can happen after context compression discards old tool results.
                if clean_msg.get("role") == "assistant" and clean_msg.get("tool_calls"):
                    _live_calls = [
                        _tc for _tc in clean_msg["tool_calls"]
                        if ((_tc.get("id") if isinstance(_tc, dict) else None) in _responded_ids)
                    ]
                    if len(_live_calls) != len(clean_msg["tool_calls"]):
                        clean_msg = dict(clean_msg)
                        if _live_calls:
                            clean_msg["tool_calls"] = _live_calls
                        else:
                            # All tool_calls are dangling — remove the key entirely
                            clean_msg = {k: v for k, v in clean_msg.items() if k != "tool_calls"}
                # Handle <think>...</think> in assistant history messages.
                # DeepSeek requires reasoning_content as a separate field — for ALL assistant
                # messages that had reasoning, including tool-calling ones. Every turn's
                # reasoning_content must be passed back exactly.
                # Other providers (OpenAI, Anthropic, Google) just need it stripped from content.
                if clean_msg.get("role") == "assistant" and isinstance(clean_msg.get("content"), str):
                    if "<think>" in clean_msg["content"]:
                        if _provider == "deepseek":
                            _tc_match = re.search(r"<think>(.*?)</think>", clean_msg["content"], re.DOTALL)
                            if _tc_match:
                                clean_msg["reasoning_content"] = _tc_match.group(1).strip()
                        clean_msg["content"] = _think_re.sub("", clean_msg["content"]).strip() or ""
                if clean_msg.get('content') is None:
                    clean_msg['content'] = ""
                # Skip empty messages with no content, no tool calls, and no reasoning_content.
                # Must NOT skip messages that have reasoning_content (DeepSeek requires them
                # to be sent back even when content="" — dropping them causes 400 errors).
                if not clean_msg.get('content') and not clean_msg.get('tool_calls') and not clean_msg.get('reasoning_content'):
                    continue
                clean_history.append(clean_msg)

            # Inject TODO status if tasks are set
            if task_mgr.todos:
                todo_status = task_mgr.get_todos_for_prompt()
                clean_history.append({
                    "role": "system",
                    "content": f"[TODO STATUS]\n{todo_status}"
                })
            
            # ═══════════════════════════════════════════════════════════════
            # LLM REQUEST (with STREAMING for live output!)
            # ═══════════════════════════════════════════════════════════════
            
            tui.set_action("Generating...")

            # Determine if we're in main context or task context
            is_main_context = current_state.is_main()

            # Debug: Log current context state
            if loop.loop_count <= 2:  # Only log first few loops to avoid spam
                tui.append_stream(f"[DEBUG] Loop {loop.loop_count}: phase={current_state.phase}, is_main={is_main_context}")
                tui.append_stream(f"[DEBUG] Loop {loop.loop_count}: context_states keys: {list(context_states.keys())}")

            if is_main_context:
                context_info = "[MAIN] Planning/Setup"
                context_prefix = "[MAIN]"
            else:
                # We're in a task context
                task_idx = current_state.get_task_idx()
                if task_idx is not None and task_idx < len(task_mgr.todos):
                    current_task = task_mgr.todos[task_idx]["task"]
                    context_info = f"[TASK {task_idx + 1}] {current_task[:50]}"
                    context_prefix = f"[TASK {task_idx + 1}]"
                else:
                    context_info = "[TASK] Execution"
                    context_prefix = "[TASK]"
            
            # Helper function to add context prefix to messages
            def append_with_context(msg: str):
                """Add context prefix to message for clarity."""
                if not msg.startswith(context_prefix):
                    tui.append_stream(f"{context_prefix} {msg}")
                else:
                    tui.append_stream(msg)
            
            append_with_context(f"[INFO] Preparing LLM request (Loop {loop.loop_count}, context={context_info})...")
            live.update(tui.render())
            
            # Rebuild tools_schema dynamically based on context
            tools_schema = get_tools_schema_for_context(is_main_context)

            # IMPORTANT: Only advertise "full coder toolbelt" in MAIN context (planning/setup).
            # In TASK contexts we keep tool schema minimal to avoid n_ctx overflow.
            if is_main_context:
                # Web tools (planning/setup)
                tools_schema.append({
                    "type": "function",
                    "function": {
                        "name": "web_fetch",
                        "description": "Fetch a webpage's HTML content. Use to inspect how pages are built or verify your output.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "description": "URL to fetch (http:// or https://)"},
                                "selector": {"type": "string", "description": "Optional CSS selector to extract specific element"}
                            },
                            "required": ["url"]
                        }
                    }
                })

                tools_schema.append({
                    "type": "function",
                    "function": {
                        "name": "web_deep_search",
                        "description": "Deep search the web for solutions, error fixes, or ideas. Returns summarized results without bloating context.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "Search query"},
                                "max_results": {"type": "integer", "description": "Maximum number of results (default: 5, max: 10)", "default": 5}
                            },
                            "required": ["query"]
                        }
                    }
                })

                # Alias for web_deep_search (some LLMs prefer shorter names)
                tools_schema.append({
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "Search the web for solutions, documentation, examples, or research. Use when you need information from the internet.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "Search query"},
                                "max_results": {"type": "integer", "description": "Maximum number of results (default: 5, max: 10)", "default": 5}
                            },
                            "required": ["query"]
                        }
                    }
                })

                # Git tools (planning/setup)
                tools_schema.append({
                    "type": "function",
                    "function": {
                        "name": "git_init",
                        "description": "Initialize a Git repository in the project directory.",
                        "parameters": {"type": "object", "properties": {}, "required": []}
                    }
                })
                tools_schema.append({
                    "type": "function",
                    "function": {
                        "name": "git_add_commit",
                        "description": "Add files to Git staging area and create a commit with a message.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "message": {"type": "string", "description": "Commit message describing the changes"},
                                "files": {"type": "array", "items": {"type": "string"}, "description": "Optional: Specific files to add (default: all files)"}
                            },
                            "required": ["message"]
                        }
                    }
                })
                tools_schema.append({
                    "type": "function",
                    "function": {
                        "name": "git_status",
                        "description": "Check the current Git status.",
                        "parameters": {"type": "object", "properties": {}, "required": []}
                    }
                })
                tools_schema.append({
                    "type": "function",
                    "function": {
                        "name": "git_log",
                        "description": "View the Git commit history.",
                        "parameters": {
                            "type": "object",
                            "properties": {"limit": {"type": "integer", "description": "Number of commits to show (default: 10)", "default": 10}},
                            "required": []
                        }
                    }
                })
                tools_schema.append({
                    "type": "function",
                    "function": {
                        "name": "project_history",
                        "description": "Show the project's version history (commit id, date, description, changed files).",
                        "parameters": {
                            "type": "object",
                            "properties": {"limit": {"type": "integer", "description": "Maximum versions to show (default 15)"}},
                            "required": []
                        }
                    }
                })
                tools_schema.append({
                    "type": "function",
                    "function": {
                        "name": "project_rollback",
                        "description": (
                            "Restore the project to an earlier version (safe: backs up current state first, "
                            "rollback is itself undoable). ONLY when the task asks for it or you must return "
                            "to a known-good state after breaking the project."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {"commit": {"type": "string", "description": "Version id from project_history"}},
                            "required": ["commit"]
                        }
                    }
                })

                # Bash (planning/setup)
                if HAS_CODING_TOOLS:
                    tools_schema.append({
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "description": "Execute shell command.",
                            "parameters": {
                                "type": "object",
                                "properties": {"command": {"type": "string"}},
                                "required": ["command"]
                            }
                        }
                    })

                # Plug-and-play tools can be large - keep them out of TASK contexts.
                tools_schema.extend(plug_and_play_tools)

            tools_schema = _dedupe_tools_schema(tools_schema)
            
            # Only clear stream if buffer is empty or only has separator
            # CRITICAL: Do NOT clear if buffer has template selection or other important messages
            # This prevents clearing content that was just added (like template selection)
            with tui._lock:
                # CRITICAL: Check if buffer has important content BEFORE clearing
                # Convert buffer to string for keyword checking
                buffer_text = " ".join(str(line) for line in tui.stream_buffer)
                has_important_content = any(
                    keyword in buffer_text 
                    for keyword in [
                        "Template Selection", "Selected template", "Template:", 
                        "Analyzing task", "template files", "New project",
                        "Creating project", "Loading tools", "Checking server",
                        "[SEARCH]", "[OK]", "[INFO]", "[WARN]", "[ERROR]", "[TOOL]", "─"  # Text markers and separators indicate important content
                    ]
                )
                
                buffer_has_content = len(tui.stream_buffer) > 1 or (
                    len(tui.stream_buffer) == 1 and 
                    tui.stream_buffer[0] != "--- New response ---" and
                    not tui.stream_buffer[0].startswith("📝 Response at")
                )
                
                # NEVER clear if buffer has important content
                if not buffer_has_content and not has_important_content:
                    # Only clear if buffer is truly empty (no important content)
                    tui.stream_buffer = []
                    tui.current_stream = ""
                    tui.current_line_buffer = ""
            
            tui.start_stream()  # Mark stream as active!
            # Only add separator if buffer is empty
            # Use timestamp instead of "--- New response ---"
            timestamp = time.strftime("%H:%M:%S")
            separator = f"[NOTE] Response at {timestamp}"
            with tui._lock:
                if not tui.stream_buffer or tui.stream_buffer == ["--- New response ---"] or (len(tui.stream_buffer) == 1 and tui.stream_buffer[0].startswith("[NOTE] Response at")):
                    tui.stream_buffer = [separator]
                else:
                    # Add separator before new content
                    tui.stream_buffer.append(separator)
            # CRITICAL: Force immediate update to show separator
            # Use try/except to prevent deadlocks
            try:
                live.update(tui.render())
            except Exception:
                pass  # Don't fail if render is blocked
            time.sleep(0.01)  # Small delay to ensure render happens
            
            # Streaming request for live output
            # CRITICAL: Force tool calls when no TODOs are set!
            if not task_mgr.todos:
                # Force the model to make ANY tool call (more compatible with different APIs)
                # Some APIs don't support specific function forcing
                tool_choice = "required"  # Forces model to call SOME tool
                tui.set_action("Forcing tool call...")
            else:
                # Allow model to choose tools freely after TODOs are set
                tool_choice = "auto"
            
            try:
                # CRITICAL: Add debug output before LLM request with context info
                append_with_context(f"[INFO] Sending LLM request (Loop {loop.loop_count}, temp={current_temp:.2f}, timeout=180s, context={context_info})...")
                tui._needs_update = True
                # CRITICAL: Force immediate update (outside lock, safe)
                try:
                    live.update(tui.render())
                except Exception:
                    pass  # Don't fail if render is blocked
                time.sleep(0.1)  # Give animation thread time to render
                
                _trace("Sending LLM request...")
                _req_headers = {"Content-Type": "application/json"}
                if _llm_api_key:
                    _req_headers["Authorization"] = f"Bearer {_llm_api_key}"
                # DeepSeek API does not support tool_choice="required" or specific function
                # forcing — only "auto" and "none". Both deepseek-v4-flash and deepseek-v4-pro
                # are internally reasoning models and reject "required" with a 400 error.
                # Workaround: when we would normally force a tool call (no TODOs yet), inject
                # an explicit system instruction instead so the model still calls set_todos first.
                _messages_for_request = list(clean_history)
                _effective_tool_choice = tool_choice
                if _provider == "deepseek" and tool_choice == "required":
                    _effective_tool_choice = "auto"
                    # Inject a strong imperative so the model acts as if tool_choice=required
                    _messages_for_request.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM INSTRUCTION] You have not yet called set_todos. "
                            "You MUST call the set_todos tool RIGHT NOW as your very next action. "
                            "Do NOT write any text. Do NOT explain anything. "
                            "Call set_todos immediately with a full task breakdown."
                        ),
                    })
                # Strict local chat templates (Qwen, non-Gemma) require ONE leading system message. The
                # coder builds its own history (TODO STATUS, nudges, the deepseek prompt above) and calls
                # the provider directly, so it must consolidate mid-conversation system turns the same way
                # the main agent's _prepare_messages does -- else the Qwen server 500s "System message
                # must be at the beginning". Gated local + non-Gemma (Gemma needs its own handling).
                if _provider == "local" and "gemma" not in str(model_name).lower():
                    from vaf.core.api_backend import consolidate_system_messages
                    _messages_for_request = consolidate_system_messages(_messages_for_request)
                _req_body = {
                    "model": model_name,
                    "messages": _messages_for_request,
                    "max_tokens": 32768,
                    "temperature": current_temp,
                    "stream": True,
                }
                _req_body["tools"] = tools_schema
                _req_body["tool_choice"] = _effective_tool_choice
                # Debug: Log message structure (roles + field names) for each loop ≥ 2
                # This captures what's actually sent to the API without flooding the log.
                if loop.loop_count >= 2:
                    try:
                        if lg:
                            _msg_summary = []
                            for _m in _messages_for_request:
                                _fields = [_m.get("role", "?")]
                                if _m.get("tool_calls"):
                                    _fields.append(f"tc={[_t['function']['name'] for _t in _m['tool_calls']]}")
                                if _m.get("reasoning_content"):
                                    _fields.append(f"RC={len(_m['reasoning_content'])}chars")
                                if _m.get("tool_call_id"):
                                    _fields.append(f"tcid={_m['tool_call_id'][:8]}")
                                _content_val = _m.get("content") or ""
                                _fields.append(f"content={len(_content_val)}chars")
                                _msg_summary.append(" ".join(_fields))
                            lg.event("request_msg_structure", loop=loop.loop_count, messages=_msg_summary)
                    except Exception:
                        pass
                stream_response = requests.post(
                    _llm_chat_url,
                    headers=_req_headers,
                    json=_req_body,
                    timeout=300,
                    stream=True
                )
                
                # Show request status immediately
                if stream_response.status_code == 200:
                    append_with_context(f"[OK] Request successful (Status: {stream_response.status_code}) - Streaming response...")
                    _trace("LLM request successful (200)")
                else:
                    append_with_context(f"[WARN] Request returned status {stream_response.status_code}")
                    _trace(f"LLM request failed ({stream_response.status_code})")
                tui._needs_update = True
                # CRITICAL: Force immediate update (outside lock, safe)
                try:
                    live.update(tui.render())
                except Exception:
                    pass  # Don't fail if render is blocked
                
                # Handle Context Size Error (400) - automatically compress and retry
                if stream_response.status_code == 400:
                    try:
                        error_data = stream_response.json()
                        error_msg = str(error_data) # Convert full JSON to string to search
                        
                        # Only compress for actual context-size errors, not other 400s
                        if "context" in error_msg.lower() or "length" in error_msg.lower() or "token" in error_msg.lower() or "maximum" in error_msg.lower():
                            # Track consecutive 400 errors
                            if not hasattr(loop, 'consecutive_400'):
                                loop.consecutive_400 = 0
                            loop.consecutive_400 += 1
                            
                            tui.set_action(f"Context error (400). Compression Level {loop.consecutive_400}...")
                            append_with_context(f"[WARN] Context limit reached (Level {loop.consecutive_400}). Compressing...")
                            live.update(tui.render())
                            
                            new_history = []
                            
                            # 1. System Prompt (Always keep)
                            system_msgs = [m for m in history if m.get("role") == "system"]
                            if system_msgs:
                                new_history.append(system_msgs[0])
                            
                            # Compression Strategy based on severity
                            if loop.consecutive_400 == 1:
                                # Level 1: Keep System + Last User + Last 3 Messages.
                                # Use -3 (not -2) so the assistant message with tool_calls is
                                # included whenever the last messages are [assistant, tool, system].
                                # Without it the tool result is orphaned → immediate Level 2 400.
                                user_msgs = [m for m in history if m.get("role") == "user"]
                                if user_msgs:
                                    new_history.append(user_msgs[-1])
                                if len(history) > 3:
                                    recent = history[-3:]
                                    for msg in recent:
                                        if len(str(msg.get('content', ''))) < 2000:
                                            new_history.append(msg)
                            elif loop.consecutive_400 == 2:
                                # Level 2: Keep ONLY System + Last User (Summary)
                                user_msgs = [m for m in history if m.get("role") == "user"]
                                if user_msgs:
                                    # Create a summarized placeholder for the user message if it's too big
                                    last_user = user_msgs[-1]
                                    if len(last_user.get('content', '')) > 500:
                                        new_history.append({"role": "user", "content": "Context cleared. Please continue working on the current task."})
                                    else:
                                        new_history.append(last_user)
                            else:
                                # Level 3: Nuclear Option - System Only + Generic Prompt
                                new_history.append({"role": "user", "content": "Context exceeded. Please continue the current task from where you left off."})
                            
                            # CRITICAL: Re-inject TODO status after compression so agent knows where it is
                            if task_mgr.todos:
                                todo_status = task_mgr.get_todos_for_prompt()
                                new_history.append({
                                    "role": "system", 
                                    "content": f"[CONTEXT RECOVERED]\nHere is your current status:\n{todo_status}\n\nResume work immediately."
                                })
                                
                            # Force update history
                            history = new_history
                            
                            # Update history in appropriate dict
                            # Update current context state
                            current_state.history = history
                            context_states[current_state.phase] = current_state
                                        
                            tui.set_action(f"Compressed to {len(history)} msgs. Retrying...")
                            live.update(tui.render())
                            time.sleep(1) # Breathe
                            continue
                    except Exception as e:
                        tui.append_stream(f"[ERROR] Failed to handle 400 error: {e}")
                        pass
                
                # Reset consecutive 400 counter on success
                if stream_response.status_code == 200:
                    if hasattr(loop, 'consecutive_400'):
                        loop.consecutive_400 = 0
                    # Reset stream-retry counter on any successful connection
                    if hasattr(loop, '_stream_retries'):
                        loop._stream_retries = 0
                
                if stream_response.status_code != 200:
                    error_text = stream_response.text[:200] if stream_response.text else "No error details"
                    append_with_context(f"[ERROR] Server returned {stream_response.status_code}: {error_text}")
                    tui._needs_update = True
                    time.sleep(0.1)
                    return f"Error: Server {stream_response.status_code} - {error_text}"
                
                # Collect streamed response
                collected_content = ""
                collected_tool_calls = []
                current_line = ""  # Buffer for current line
                _in_reasoning_phase = False  # Track DeepSeek reasoning phase for <think> wrapping
                update_counter = 0  # Throttle updates for performance
                
                for line in stream_response.iter_lines():
                    if not line:
                        continue
                    
                    # Platform-independent UTF-8 decoding (Windows, macOS, Linux)
                    try:
                        line_str = line.decode('utf-8')
                    except UnicodeDecodeError:
                        # Fallback for platforms with different encoding
                        line_str = line.decode('utf-8', errors='replace')
                    
                    # DEBUG RAW STREAM
                    # if len(line_str) > 6 and not line_str.startswith('data: [DONE]'):
                         # Show more of the delta to see where the text is
                         # tui.append_stream(f"[RAW] {line_str[6:120]}...")
                    
                    if not line_str.startswith('data: '):
                        continue
                    
                    data_str = line_str[6:]  # Remove 'data: ' prefix
                    if data_str == '[DONE]':
                        # Flush remaining line
                        if current_line.strip():
                            tui.append_stream(current_line)
                        with tui._lock:
                            tui.current_line_buffer = ""  # Clear after flushing
                        tui.end_stream()  # Mark stream as finished
                        live.update(tui.render())
                        break
                    
                    try:
                        chunk = json.loads(data_str)
                        choices = chunk.get('choices', [])
                        if not choices:
                            continue
                        
                        # Check finish_reason
                        finish_reason = choices[0].get('finish_reason')
                        if finish_reason == 'length':
                            tui.append_stream("[WARN] Response truncated (length limit).")
                            
                            # CRITICAL: If we were writing a file, save what we have so far!
                            # This prevents the "infinite loop of starting over"
                            if 'write_file' in collected_content or (collected_tool_calls and collected_tool_calls[-1]['function']['name'] == 'write_file'):
                                tui.append_stream("[INFO] Attempting to save partial content...")

                                # Try to find filename from args
                                partial_filename = "PARTIAL_CONTENT.txt"
                                partial_content = ""
                                _args_json_complete = False  # True if JSON parsed OK (write_file will run normally)

                                # Extract content from collected tool calls if available
                                if collected_tool_calls:
                                    last_tc = collected_tool_calls[-1]
                                    if last_tc['function']['name'] == 'write_file':
                                        args_str = last_tc['function']['arguments']

                                        # Check if the JSON args are actually complete (common when finish_reason='length'
                                        # fires at the end of a large but complete response)
                                        try:
                                            _parsed_test = json.loads(args_str)
                                            if 'path' in _parsed_test and 'content' in _parsed_test:
                                                _args_json_complete = True  # write_file will execute normally
                                        except Exception:
                                            pass  # JSON truncated — partial save is needed

                                        # Simple regex to get path and content
                                        p_match = re.search(r'"path"\s*:\s*"([^"]+)"', args_str)
                                        if p_match:
                                            partial_filename = f"PARTIAL_{os.path.basename(p_match.group(1))}"

                                        if not _args_json_complete:
                                            # Get content (even if truncated)
                                            c_match = re.search(r'"content"\s*:\s*"(.*)', args_str, re.DOTALL)
                                            if c_match:
                                                partial_content = c_match.group(1)
                                                # Clean up
                                                partial_content = partial_content.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')

                                if _args_json_complete:
                                    # JSON is complete — write_file will dispatch normally, no partial needed
                                    tui.append_stream(f"[INFO] JSON complete despite finish_reason=length; write_file will run normally (no PARTIAL_ saved)")
                                elif partial_content and len(partial_content) > 50:
                                    # Actually truncated — save partial so agent can resume from it
                                    try:
                                        with open(os.path.join(base_dir, partial_filename), 'w', encoding='utf-8') as f:
                                            f.write(partial_content)
                                        tui.append_stream(f"[INFO] Saved {len(partial_content)} bytes to {partial_filename}")
                                        
                                        # Instruct agent to APPEND instead of overwrite
                                        history.append({
                                            "role": "system",
                                            "content": (
                                                f"⚠️ RESPONSE TRUNCATED! I saved the partial code to `{partial_filename}`.\n"
                                                f"DO NOT start over! Read `{partial_filename}` to see where you stopped.\n"
                                                f"Then call `write_file` with the *rest* of the code, or append to it.\n"
                                                f"Better yet: Split the file into smaller parts (e.g. header.html, body.html) to avoid limits."
                                            )
                                        })
                                    except Exception as e:
                                        tui.append_stream(f"[ERROR] Failed to save partial: {e}")
                                else:
                                    # Standard truncation message if no file involved
                                    history.append({
                                        "role": "system",
                                        "content": "Your previous response was truncated because it reached the token limit. Please continue exactly where you left off. Do not repeat the beginning."
                                    })
                            else:
                                # Standard truncation message
                                history.append({
                                    "role": "system",
                                    "content": "Your previous response was truncated because it reached the token limit. Please continue exactly where you left off. Do not repeat the beginning."
                                })
                                
                            # Update context state
                            current_state.history = history
                            context_states[current_state.phase] = current_state
                            # We don't break here, we let the loop handle the next request
                        
                        delta = choices[0].get('delta', {})

                        # Handle reasoning_content (DeepSeek native field) and regular content
                        # separately. Reasoning is wrapped in <think>...</think> in collected_content
                        # so clean_history can later extract it as reasoning_content for DeepSeek
                        # passback. Without this, mixing reasoning into content causes a 400 error.
                        _reasoning_chunk = delta.get('reasoning_content', '') or delta.get('thought', '') or ''
                        _content_chunk = delta.get('content', '') or ''

                        if _reasoning_chunk:
                            if not _in_reasoning_phase:
                                _in_reasoning_phase = True
                                collected_content += "<think>"
                            _reasoning_chunk = re.sub(r'</?redacted_reasoning>', '', _reasoning_chunk, flags=re.IGNORECASE)
                            _reasoning_chunk = re.sub(r'</?think>', '', _reasoning_chunk, flags=re.IGNORECASE)
                            collected_content += _reasoning_chunk
                            text_chunk = _reasoning_chunk
                            is_reasoning = True
                        elif _content_chunk:
                            if _in_reasoning_phase:
                                _in_reasoning_phase = False
                                collected_content += "</think>"
                            _content_chunk = re.sub(r'</?redacted_reasoning>', '', _content_chunk, flags=re.IGNORECASE)
                            _content_chunk = re.sub(r'</?think>', '', _content_chunk, flags=re.IGNORECASE)
                            collected_content += _content_chunk
                            text_chunk = _content_chunk
                            is_reasoning = False
                        else:
                            text_chunk = ''
                            is_reasoning = False

                        # Reasoning chunks are accumulated in collected_content for
                        # DeepSeek RC passback but must NOT be shown in the TUI —
                        # the <think> content would flood the terminal with internal
                        # reasoning and cause apparent text duplication.
                        if text_chunk and not is_reasoning:
                            current_line += text_chunk

                            # Update TUI's current line buffer for live display
                            with tui._lock:
                                tui.current_line_buffer = current_line

                            # If we have a newline, flush the complete line to buffer
                            if '\n' in current_line:
                                parts = current_line.split('\n', 1)
                                line_to_add = parts[0]
                                # Add complete line to stream buffer - ALWAYS add
                                tui.append_stream(line_to_add)
                                current_line = parts[1] if len(parts) > 1 else ""

                                # Update current line buffer after flushing
                                with tui._lock:
                                    tui.current_line_buffer = current_line
                            # NOTE: If no newline yet, content stays in current_line_buffer
                            # and is displayed via render() - it will be flushed at stream end

                            # Update immediately for live feel (shows current_line_buffer)
                            # This ensures content is visible even without newlines
                            live.update(tui.render())
                        
                        # Handle tool calls
                        if 'tool_calls' in delta:
                            for tc_delta in delta['tool_calls']:
                                idx = tc_delta.get('index', 0)
                                
                                # Extend list if needed
                                while len(collected_tool_calls) <= idx:
                                    collected_tool_calls.append({
                                        'id': '',
                                        'type': 'function',
                                        'function': {'name': '', 'arguments': ''}
                                    })
                                
                                tc = collected_tool_calls[idx]
                                
                                if 'id' in tc_delta:
                                    tc['id'] = tc_delta['id']
                                if 'function' in tc_delta:
                                    # DEBUG: Trace tool streaming
                                    fn_name_chunk = tc_delta['function'].get('name', '')
                                    if fn_name_chunk:
                                        tui.append_stream(f"[DEBUG] Stream tool idx={idx} name+={fn_name_chunk}")
                                    
                                    if 'name' in tc_delta['function']:
                                        tc['function']['name'] += tc_delta['function']['name']
                                        tui.set_action(f"Calling {tc['function']['name']}")
                                        # CRITICAL: Add tool call to stream buffer immediately for visibility
                                        # Use append_with_context if available, otherwise determine context manually
                                        tool_msg = f"[TOOL] Calling {tc['function']['name']}..."
                                        if 'append_with_context' in locals():
                                            append_with_context(tool_msg)
                                        else:
                                            # Fallback: determine context manually
                                            is_main_ctx = (current_context_manager == main_context_manager)
                                            if is_main_ctx:
                                                prefix = "[MAIN]"
                                            else:
                                                # Get task index from current_state
                                                current_task_idx = current_state.get_task_idx()
                                                prefix = f"[TASK {current_task_idx + 1}]" if current_task_idx is not None else "[TASK]"
                                            tui.append_stream(f"{prefix} {tool_msg}")
                                        live.update(tui.render())
                                    if 'arguments' in tc_delta['function']:
                                        tc['function']['arguments'] += tc_delta['function']['arguments']
                                        # Update UI to show activity
                                        tool_name = tc['function']['name']
                                        tui.set_action(f"Building args for {tool_name}...")
                                        tui._needs_update = True  # Force update
                                        
                                        # STREAMING CODE PREVIEW: If writing file, show content live!
                                        if tool_name == 'write_file':
                                            full_args = tc['function']['arguments']
                                            
                                            # 1. Try to extract PATH to show file in list immediately
                                            # Look for complete path string: "path": "..."
                                            path_match = re.search(r'"path"\s*:\s*"([^"]+)"', full_args)
                                            fname_preview = "Streaming..."
                                            if path_match:
                                                path_hint = path_match.group(1)
                                                if path_hint:
                                                    fname_preview = os.path.basename(path_hint)
                                                    # Add file to list immediately
                                                    if fname_preview not in tui.files:
                                                        tui.add_file(fname_preview, 0, "writing")
                                                        tui._needs_update = True

                                            # 2. Try to extract CONTENT for preview
                                            # Try to find "content": "..."
                                            content_start = full_args.find('"content":')
                                            if content_start != -1:
                                                # Find start of string value
                                                val_start = full_args.find('"', content_start + 10)
                                                if val_start != -1:
                                                    current_content = full_args[val_start+1:]
                                                    # Unescape JSON string (basic) to make it readable
                                                    current_content = current_content.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
                                                    
                                                    # Pass FULL content to render so it can calculate line numbers correctly
                                                    tui.set_code_preview(fname_preview, current_content, "code")
                                                    # WebUI live editor: stream the code as it is generated
                                                    _emit_live_code(fname_preview, current_content)
                    
                    except json.JSONDecodeError:
                        try:
                            if lg:
                                lg.event("stream_json_error", data=data_str[:100])
                        except Exception:
                            pass
                        continue
                
                # Close any open <think> block before building the message
                if _in_reasoning_phase:
                    collected_content += "</think>"
                    _in_reasoning_phase = False

                # Flush any remaining line after stream ends
                # CRITICAL: ALWAYS add current_line to buffer if it has content
                # This ensures content that didn't have a newline is still visible
                if current_line and current_line.strip():
                    # Add directly to buffer to ensure it's there
                    with tui._lock:
                        tui.stream_buffer.append(current_line)
                        # Also add to collected_content if it's not already there
                        if current_line not in collected_content:
                            collected_content += current_line
                        tui.current_line_buffer = ""  # Clear after flushing
                        # Keep buffer size manageable
                        if len(tui.stream_buffer) > tui.STREAM_LINES * 2:
                            tui.stream_buffer = tui.stream_buffer[-tui.STREAM_LINES * 2:]
                else:
                    # Clear current_line_buffer even if empty
                    with tui._lock:
                        tui.current_line_buffer = ""
                
                # Build final message object
                msg = {
                    'role': 'assistant',
                    'content': collected_content or None,
                    'tool_calls': collected_tool_calls if collected_tool_calls else None
                }
                
                content = collected_content
                tool_calls = collected_tool_calls if collected_tool_calls else []
                
                # DEBUG: Log what we actually got from the stream
                debug_msg = f"[DEBUG] Stream end. Content: {len(content or '')} chars. Tool calls: {len(tool_calls)}."
                if tool_calls:
                    debug_msg += f" Tools: {[tc['function']['name'] for tc in tool_calls]}"
                tui.append_stream(debug_msg)

                # Structured metadata for debugging "stuck" behavior (no raw LLM text)
                try:
                    if lg:
                        tool_names = []
                        for tc in tool_calls or []:
                            try:
                                tool_names.append(str(tc.get("function", {}).get("name", "")))
                            except Exception:
                                pass
                        lg.event(
                            "llm_stream_end",
                            loop=getattr(loop, "loop_count", None),
                            content_len=len(content or ""),
                            tool_calls_count=len(tool_calls or []),
                            tool_names=tool_names,
                        )
                except Exception:
                    pass
                
                # FALLBACK: Try to extract tool calls from text content
                # Some models output JSON in text instead of using proper tool_calls
                if not tool_calls and content:
                    extracted_tool_call = None
                    
                    # Try to find tool calls in various formats (not just set_todos)
                    
                    # Format 0: <tool_call> tags (our recommended format for non-function-calling models)
                    # First try set_todos (most common)
                    tool_call_match = re.search(r'<tool_call>\s*set_todos\s*\(\s*(?:tasks\s*=\s*)?\[(.*?)\]\s*\)\s*</tool_call>', content, re.DOTALL | re.IGNORECASE)
                    if tool_call_match:
                        try:
                            tasks_str = tool_call_match.group(1)
                            task_matches = re.findall(r'["\']([^"\']+)["\']', tasks_str)
                            if task_matches:
                                extracted_tool_call = {
                                    'id': f'extracted_{int(time.time())}',
                                    'type': 'function',
                                    'function': {
                                        'name': 'set_todos',
                                        'arguments': json.dumps({'tasks': task_matches})
                                    }
                                }
                                tui.append_stream(f"[EXTRACTED] <tool_call> set_todos with {len(task_matches)} tasks")
                        except:
                            pass
                    
                    # Format 0b: Try to extract write_file and other tools from <tool_call> tags
                    if not extracted_tool_call:
                        # Match: <tool_call>{"name": "write_file", "arguments": {...}}</tool_call>
                        tool_call_json_match = re.search(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', content, re.DOTALL)
                        if tool_call_json_match:
                            try:
                                tool_call_json = json.loads(tool_call_json_match.group(1))
                                if 'name' in tool_call_json and 'arguments' in tool_call_json:
                                    extracted_tool_call = {
                                        'id': f'extracted_{int(time.time())}',
                                        'type': 'function',
                                        'function': {
                                            'name': tool_call_json['name'],
                                            'arguments': json.dumps(tool_call_json['arguments']) if isinstance(tool_call_json['arguments'], dict) else str(tool_call_json['arguments'])
                                        }
                                    }
                                    tui.append_stream(f"[EXTRACTED] {tool_call_json['name']} from <tool_call> JSON")
                            except:
                                pass
                    
                    # Format 0c: Try JSON format in content: {"name": "write_file", "arguments": {...}}
                    if not extracted_tool_call:
                        json_tool_match = re.search(r'\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"arguments"\s*:\s*(\{.*?\})\s*\}', content, re.DOTALL)
                        if json_tool_match:
                            tool_name = json_tool_match.group(1)
                            tool_args_str = json_tool_match.group(2)
                            try:
                                # Validate JSON arguments
                                json.loads(tool_args_str)
                                extracted_tool_call = {
                                    'id': f'extracted_{int(time.time())}',
                                    'type': 'function',
                                    'function': {
                                        'name': tool_name,
                                        'arguments': tool_args_str
                                    }
                                }
                                tui.append_stream(f"[EXTRACTED] {tool_name} from JSON format")
                            except:
                                pass
                    if tool_call_match:
                        try:
                            tasks_str = tool_call_match.group(1)
                            task_matches = re.findall(r'["\']([^"\']+)["\']', tasks_str)
                            if task_matches:
                                extracted_tool_call = {
                                    'id': f'extracted_{int(time.time())}',
                                    'type': 'function',
                                    'function': {
                                        'name': 'set_todos',
                                        'arguments': json.dumps({'tasks': task_matches})
                                    }
                                }
                                tui.append_stream(f"[EXTRACTED] <tool_call> set_todos with {len(task_matches)} tasks")
                        except:
                            pass
                    
                    # Format 1: JSON object {"name": "set_todos", "arguments": {...}}
                    if not extracted_tool_call:
                        json_match = re.search(r'\{\s*"name"\s*:\s*"set_todos".*?"tasks"\s*:\s*\[(.*?)\]', content, re.DOTALL)
                        if json_match:
                            try:
                                # Extract tasks array
                                tasks_str = json_match.group(1)
                                # Parse individual task strings
                                task_matches = re.findall(r'"([^"]+)"', tasks_str)
                                if task_matches:
                                    extracted_tool_call = {
                                        'id': f'extracted_{int(time.time())}',
                                        'type': 'function',
                                        'function': {
                                            'name': 'set_todos',
                                            'arguments': json.dumps({'tasks': task_matches})
                                        }
                                    }
                                    tui.append_stream(f"[FALLBACK] Extracted set_todos from JSON with {len(task_matches)} tasks")
                            except:
                                pass
                    
                    # Format 2: set_todos(tasks=[...]) function call syntax
                    if not extracted_tool_call:
                        func_match = re.search(r'set_todos\s*\(\s*(?:tasks\s*=\s*)?\[(.*?)\]', content, re.DOTALL)
                        if func_match:
                            try:
                                tasks_str = func_match.group(1)
                                task_matches = re.findall(r'["\']([^"\']+)["\']', tasks_str)
                                if task_matches:
                                    extracted_tool_call = {
                                        'id': f'extracted_{int(time.time())}',
                                        'type': 'function',
                                        'function': {
                                            'name': 'set_todos',
                                            'arguments': json.dumps({'tasks': task_matches})
                                        }
                                    }
                                    tui.append_stream(f"[FALLBACK] Extracted set_todos call with {len(task_matches)} tasks")
                            except:
                                pass
                    
                    # Format 3: Markdown list that looks like TODOs
                    if not extracted_tool_call and not task_mgr.todos:
                        # Look for numbered or bulleted lists
                        list_matches = re.findall(r'(?:^|\n)\s*(?:\d+\.|[-*•])\s*(.+?)(?=\n|$)', content)
                        if len(list_matches) >= 2:  # At least 2 items to be a TODO list
                            # Filter out very short or header-like items
                            tasks = [t.strip() for t in list_matches if len(t.strip()) > 10]
                            if len(tasks) >= 2:
                                extracted_tool_call = {
                                    'id': f'extracted_{int(time.time())}',
                                    'type': 'function', 
                                    'function': {
                                        'name': 'set_todos',
                                        'arguments': json.dumps({'tasks': tasks[:15]})  # Allow up to 15 tasks
                                    }
                                }
                                append_with_context(f"[FALLBACK] Extracted TODO list from markdown: {len(tasks)} tasks")
                    
                    # Format 4: Markdown code blocks with filename (The "Lazy Agent" Catcher)
                    if not extracted_tool_call:
                        # Find all code blocks
                        code_blocks = list(re.finditer(r'```(?:\w+)?\n(.*?)```', content, re.DOTALL))
                        if code_blocks:
                            # Check if we can associate a filename with the LAST code block
                            best_block = code_blocks[-1]
                            code_content = best_block.group(1)
                            
                            filename = None
                            
                            # Strategy A: Look for filename in text before block
                            start_idx = best_block.start()
                            pre_text = content[max(0, start_idx-300):start_idx]
                            file_match = re.search(r'(?:^|[\s`\'">])([\w\-\./]+\.(?:py|js|html|css|json|md|txt|java|c|cpp|h|go|rs|php|ts|jsx|tsx|vue))', pre_text)
                            if file_match:
                                filename = file_match.group(1).strip('`\'".>')
                            
                            # Strategy B: Infer filename from current task if not found
                            if not filename and task_mgr.todos:
                                current_task = task_mgr.get_current_task()
                                if current_task:
                                    task_file_match = re.search(r'([\w\-\./]+\.(?:py|js|html|css|json|md|txt))', current_task)
                                    if task_file_match:
                                        filename = task_file_match.group(1)
                                        tui.append_stream(f"[FALLBACK] Inferred filename {filename} from task description")

                            if filename and len(code_content) > 20: # Ensure valid content
                                extracted_tool_call = {
                                    'id': f'extracted_{int(time.time())}',
                                    'type': 'function', 
                                    'function': {
                                        'name': 'write_file',
                                        'arguments': json.dumps({
                                            'path': filename,
                                            'content': code_content
                                        })
                                    }
                                }
                                tui.append_stream(f"[FALLBACK] Extracted write_file({filename}) from markdown code block")
                    
                    if extracted_tool_call:
                        tool_calls = [extracted_tool_call]
                        tui.append_stream("Tool call extracted from text response!")
                        live.update(tui.render())
                
                # EXTRACT content from msg if collected_content is empty (fallback)
                if not content and msg.get('content'):
                    content = msg['content']
                
                # CRITICAL: Before ending stream, ensure current_line_buffer is flushed
                # This ensures content that didn't have a newline is still added to buffer
                with tui._lock:
                    if tui.current_line_buffer and tui.current_line_buffer.strip():
                        # Add current_line_buffer to stream_buffer before ending stream
                        tui.stream_buffer.append(tui.current_line_buffer)
                        # Also add to collected_content if it's not already there
                        if tui.current_line_buffer not in collected_content:
                            collected_content += tui.current_line_buffer
                        tui.current_line_buffer = ""  # Clear after flushing
                        # Keep buffer size manageable
                        if len(tui.stream_buffer) > tui.STREAM_LINES * 2:
                            tui.stream_buffer = tui.stream_buffer[-tui.STREAM_LINES * 2:]
                
                # Mark stream as finished
                tui.end_stream()
                
                # CRITICAL: ALWAYS add content to buffer - NO duplicate checks, just add it
                # This ensures content is ALWAYS visible, even if it was partially added during streaming
                with tui._lock:
                    # Get all possible content sources
                    all_content = collected_content or content or msg.get('content', '') or ''
                    
                    if all_content and all_content.strip():
                        # SIMPLE: Just add all content lines to buffer
                        # Don't check for duplicates - if content exists, show it
                        for line in all_content.split('\n'):
                            tui.stream_buffer.append(line)
                        # Keep buffer size manageable
                        if len(tui.stream_buffer) > tui.STREAM_LINES * 2:
                            tui.stream_buffer = tui.stream_buffer[-tui.STREAM_LINES * 2:]
                        # CRITICAL: Update display immediately after adding content
                        live.update(tui.render())
                    elif tool_calls:
                        # If we have tool calls but no content, show that we're executing tools
                        # Show tool names and parameters (truncated if too long)
                        # CRITICAL: Check if tool calls are already in buffer (from streaming)
                        tool_infos = []
                        for tc in tool_calls:
                            fn_name = tc.get('function', {}).get('name', 'unknown')
                            fn_args = tc.get('function', {}).get('arguments', '')
                            
                            # Check if this tool call is already in buffer (from streaming detection)
                            tool_already_shown = False
                            with tui._lock:
                                for line in tui.stream_buffer:
                                    if f"[TOOL] Calling {fn_name}" in line:
                                        tool_already_shown = True
                                        break
                            
                            # Only add if not already shown
                            if not tool_already_shown:
                                # Try to parse arguments as JSON to show them nicely
                                try:
                                    if fn_args:
                                        args_dict = json.loads(fn_args)
                                        # Format arguments nicely (truncate long values)
                                        args_str = ", ".join([
                                            f"{k}={str(v)[:50]}{'...' if len(str(v)) > 50 else ''}"
                                            for k, v in args_dict.items()
                                        ])
                                        tool_infos.append(f"[TOOL] {fn_name}({args_str})")
                                    else:
                                        tool_infos.append(f"[TOOL] {fn_name}()")
                                except (json.JSONDecodeError, TypeError):
                                    # If parsing fails, just show the raw arguments (truncated)
                                    args_display = fn_args[:100] + "..." if len(fn_args) > 100 else fn_args
                                    tool_infos.append(f"[TOOL] {fn_name}({args_display})")
                        
                        # Add each tool call as a separate line (only if not already shown)
                        if tool_infos:
                            with tui._lock:
                                for tool_info in tool_infos:
                                    # Add context prefix to tool calls
                                    if not tool_info.startswith(context_prefix):
                                        tui.stream_buffer.append(f"{context_prefix} {tool_info}")
                                    else:
                                        tui.stream_buffer.append(tool_info)
                                # Keep buffer size manageable
                                if len(tui.stream_buffer) > tui.STREAM_LINES * 2:
                                    tui.stream_buffer = tui.stream_buffer[-tui.STREAM_LINES * 2:]
                            # CRITICAL: Update display immediately after adding tool info
                            live.update(tui.render())
                    else:
                        # No content and no tool calls - this is an empty response
                        # Only show info if buffer is empty (just separator)
                        with tui._lock:
                            # Check if buffer only has separator (old or new format)
                            buffer_only_separator = (
                                len(tui.stream_buffer) <= 1 or 
                                (len(tui.stream_buffer) == 1 and (
                                    tui.stream_buffer[0] == "--- New response ---" or
                                    tui.stream_buffer[0].startswith("📝 Response at")
                                ))
                            )
                            if buffer_only_separator:
                                # Show that this was an empty response (no content, no tool calls)
                                # This is normal when the agent is waiting for tool results
                                empty_info = "⏳ Waiting for tool results..."
                                # Replace separator with info if buffer only has separator
                                if len(tui.stream_buffer) == 1 and tui.stream_buffer[0].startswith("📝 Response at"):
                                    tui.stream_buffer[0] = f"{tui.stream_buffer[0]} - {empty_info}"
                                else:
                                    tui.stream_buffer.append(empty_info)
                                # Keep buffer size manageable
                                if len(tui.stream_buffer) > tui.STREAM_LINES * 2:
                                    tui.stream_buffer = tui.stream_buffer[-tui.STREAM_LINES * 2:]
                                # CRITICAL: Update display immediately after adding info
                                live.update(tui.render())
                
                # Show summary of what was received
                content_len = len(collected_content.strip()) if collected_content else 0
                tool_count = len(collected_tool_calls) if collected_tool_calls else 0
                
                if content_len > 0 or tool_count > 0:
                    summary_parts = []
                    if content_len > 0:
                        summary_parts.append(f"{content_len} chars")
                    if tool_count > 0:
                        tool_names = [tc.get('function', {}).get('name', 'unknown') for tc in collected_tool_calls]
                        summary_parts.append(f"{tool_count} tool call(s): {', '.join(tool_names)}")
                    if summary_parts:
                        append_with_context(f"[OK] Response received: {', '.join(summary_parts)}")
                else:
                    append_with_context(f"[WARN] Empty response received (no content, no tool calls)")
                
                tui._needs_update = True
                # CRITICAL: Force immediate update (outside lock, safe)
                try:
                    live.update(tui.render())
                except Exception:
                    pass  # Don't fail if render is blocked
                
                # Final update to ensure everything is displayed
                live.update(tui.render())
                
            except requests.exceptions.Timeout:
                try:
                    if lg:
                        lg.event("llm_timeout", timeout=300)
                except Exception:
                    pass
                tui.end_stream()
                append_with_context(f"[ERROR] Request timed out after 180s - no response from server")
                tui._needs_update = True
                time.sleep(0.1)
                return "Error: Request timed out after 180 seconds"
            except requests.exceptions.ConnectionError:
                try:
                    if lg:
                        lg.event("llm_connection_error", error="ConnectionError")
                except Exception:
                    pass
                tui.end_stream()  # Mark stream as finished even on error
                return "Error: VAF Server offline."
            except Exception as e:
                try:
                    if lg:
                        lg.event("llm_stream_error", error=str(e), traceback=str(e))
                except Exception:
                    pass
                tui.end_stream()

                # Retry transient network errors (ConnectionResetError, ChunkedEncodingError).
                # These typically happen when the DeepSeek API drops a long-running stream
                # (large files take 2-5+ minutes to generate). History is intact at this
                # point — no assistant message or tool results were added yet — so continuing
                # the outer while-True loop safely resends the same request.
                _e_str = str(e)
                _is_transient = (
                    isinstance(e, (ConnectionResetError,
                                   requests.exceptions.ChunkedEncodingError))
                    or "Connection reset" in _e_str
                    or "ConnectionResetError" in _e_str
                    or "ChunkedEncodingError" in _e_str
                    or "RemoteDisconnected" in _e_str
                )
                _stream_retries = getattr(loop, "_stream_retries", 0)
                if _is_transient and _stream_retries < 3:
                    loop._stream_retries = _stream_retries + 1
                    tui.append_stream(
                        f"[WARN] Stream connection lost ({loop._stream_retries}/3). "
                        f"Retrying in 5s..."
                    )
                    time.sleep(5)
                    continue  # restart the while-True loop with the same history

                return f"Error: Stream failed - {e}"
                
            # ═══════════════════════════════════════════════════════════════
            # PROCESS STREAMED RESPONSE
            # ═══════════════════════════════════════════════════════════════
            
            tui.set_action("Processing...")
            live.update(tui.render())
            
            # FINAL FALLBACK: Always show content if we have it
            # Extract from msg if content is still empty
            if not content and 'msg' in locals() and msg.get('content'):
                content = msg['content']
            
            # Always show content if we have it - DIRECTLY to buffer, no checks
            if content and content.strip():
                # Add content directly to buffer - ensures it stays visible
                with tui._lock:
                    # Check if content is already in buffer
                    content_already_there = False
                    for line in tui.stream_buffer:
                        if content[:50].strip() in line:
                            content_already_there = True
                            break
                    
                    if not content_already_there:
                        # Add ALL content lines directly to buffer
                        for line in content.split('\n'):
                            tui.stream_buffer.append(line)
                            # Keep buffer size manageable
                            if len(tui.stream_buffer) > tui.STREAM_LINES * 2:
                                tui.stream_buffer = tui.stream_buffer[-tui.STREAM_LINES * 2:]
                        live.update(tui.render())
            
            # Tool calls are already displayed above (line 1272-1304)
            # Only display here if they weren't already shown (shouldn't happen, but safety check)
            # This prevents duplicate tool call displays

            history.append(msg)
            # Update context state
            current_state.history = history
            context_states[current_state.phase] = current_state

            # FORCE DISPLAY CONTENT FROM MSG - Platform-independent, always show
            # Extract content from msg if we have it - NO CHECKS, JUST SHOW IT
            msg_content = msg.get('content', '') or content or collected_content or ''
            
            # Filter out redacted reasoning tags
            if msg_content:
                msg_content = re.sub(r'</?redacted_reasoning>', '', msg_content, flags=re.IGNORECASE)
                msg_content = re.sub(r'</?think>', '', msg_content, flags=re.IGNORECASE)
                msg_content = msg_content.strip()
            
            # ═══════════════════════════════════════════════════════════════
            # HANDLE NO TOOL CALLS - CRITICAL: Check TODOs FIRST!
            # ═══════════════════════════════════════════════════════════════
            
            if not tool_calls:
                # Empty response handler: Check if we have a final answer (not just thinking)
                # NOTE: This checks final answer content, NOT reasoning/thinking
                # The model can think as much as it wants, but must provide a final answer
                
                # CRITICAL: First check if response is truly empty (BEFORE cleaning)
                # This is for tool-intent detection - we need to check the original response
                response_text = msg_content or content or ""
                
                # RELAXED CHECK: Only consider truly empty if very short
                # VQ-1 and smaller models might give short answers like "Okay, I'll do it."
                clean_content = response_text.strip()
                is_effectively_empty = len(clean_content) < 5

                # IMPORTANT: A response with tool calls is NEVER truly empty — the model is
                # doing real work. Suppress all empty-response handling when tool_calls exist.
                if is_effectively_empty and not tool_calls:
                    tui.append_stream("[WARN] Empty response detected. Applying snapshot and retry...")
                    # Increment context-specific empty counter
                    if is_main_context:
                        main_empty_counter += 1
                        consecutive_empty = main_empty_counter
                    else:
                        # Get task index from current_state
                        current_task_idx = current_state.get_task_idx()
                        if current_task_idx is not None:
                            if current_task_idx not in task_empty_counters:
                                task_empty_counters[current_task_idx] = 0
                            task_empty_counters[current_task_idx] += 1
                            consecutive_empty = task_empty_counters[current_task_idx]
                        else:
                            consecutive_empty = 0  # Fallback
                    
                    # CRITICAL FIX: Also increment global idle_loop_count to trigger Nudge/Reset
                    idle_loop_count += 1
                    
                    # If we are stuck in empty loops for too long, FORCE a smart reset with context preservation
                    if idle_loop_count >= 10:
                         _trace("Idle/Empty limit reached (10). Performing smart context reset with preservation.")

                         # Get rich context for recovery
                         todo_status = task_mgr.get_todos_for_prompt() if task_mgr.todos else "Resume current task."

                         # Build Context Glue
                         state_glue = _build_state_glue()

                         # SMART RESET: Extract critical messages from history
                         critical_msgs = []
                         for msg in current_state.history:
                             role = msg.get("role", "")
                             content = msg.get("content", "")

                             # Keep system prompt
                             if role == "system" and len(critical_msgs) == 0:
                                 critical_msgs.append(msg)

                             # Keep tool results (especially write_file, read_file, set_todos)
                             elif role == "tool":
                                 tool_name = msg.get("name", "")
                                 if tool_name in ["write_file", "read_file", "set_todos"]:
                                     # Truncate long results
                                     truncated = content[:500] + "..." if len(content) > 500 else content
                                     critical_msgs.append({
                                         "role": "tool",
                                         "name": tool_name,
                                         "content": truncated,
                                         "tool_call_id": msg.get("tool_call_id", "")
                                     })

                         # Reset history with preserved context
                         current_state.history = critical_msgs + [
                             {
                                 "role": "system",
                                 "content": (
                                     f"### CONTEXT RESET - You were stuck in an empty loop\n\n"
                                     f"{state_glue}\n\n"
                                     f"**YOUR STATUS:**\n{todo_status}\n\n"
                                     f"**FILES CREATED IN THIS CONTEXT:**\n" +
                                     "\n".join(f"- {f}" for f in current_state.files_created) +
                                     f"\n\n**INSTRUCTION:**\n"
                                     f"Resume work immediately. Call a tool NOW (write_file, read_file, task_done)."
                                 )
                             }
                         ]

                         # Update in storage and sync
                         context_states[current_state.phase] = current_state
                         sync_legacy_vars()

                         tui.set_action(f"♻️ SMART RESET (preserved critical context)")
                         idle_loop_count = 0
                         consecutive_empty = 0
                         
                    elif idle_loop_count >= 5:
                         nudge_msg = f"🛑 SYSTEM ALERT: You are sending empty responses for {idle_loop_count} loops. STOP. Call `task_done` or `write_file` NOW."
                         history.append({
                            "role": "user",
                            "content": nudge_msg
                         })
                         tui.set_action(f"⚠️ EMPTY LOOP ({idle_loop_count}) - FORCING ACTION")
                         _trace(f"Triggered Empty Response Nudge (Count: {idle_loop_count})")
                         # Reset empty counter to avoid immediate retry, let the model see the message
                         consecutive_empty = 0 
                         
                    # Dynamic Temperature Sweep to break loops
                    # Oscillate around 0.3: +0.1, +0.2, +0.3... then down
                    # For code, we prefer staying low, but if stuck, go higher
                    delta = ((consecutive_empty + 1) // 2) * 0.1
                    direction = 1 if consecutive_empty % 2 == 1 else -1
                    current_temp = 0.3 + (delta * direction)
                    # Clamp between 0.1 and 0.8 (don't go too crazy for code)
                    current_temp = max(0.1, min(0.8, current_temp))
                    
                    # Log why we are restarting with more details (including context)
                    tool_count = len(tool_calls) if tool_calls else 0
                    append_with_context(f"[WARN] Empty response detected (content_len={len(clean_content)}, tool_calls={tool_count})")
                    append_with_context(f"[INFO] Retrying with increased temperature: {current_temp:.2f} (attempt {consecutive_empty})")
                    tui._needs_update = True
                    # CRITICAL: Force immediate update (outside lock, safe)
                    try:
                        live.update(tui.render())
                    except Exception:
                        pass  # Don't fail if render is blocked
                
                # ═══════════════════════════════════════════════════════════════
                # NEW: Tool-Intent Detection (like main agent)
                # ═══════════════════════════════════════════════════════════════
                # Check if agent mentioned a tool name (but didn't actually call it yet)
                # This prevents the agent from getting stuck when it mentions a tool but doesn't call it
                # For tool-intent detection, use is_effectively_empty (checked BEFORE cleaning)
                # Skip when tool_calls is set — the model IS calling tools, no need to nudge.
                if is_effectively_empty and not tool_calls:
                    # Get available tool names dynamically
                    available_tool_names = []
                    # Check if we have access to tools (coding_agent has its own tools)
                    if hasattr(self, 'tools') and self.tools:
                        available_tool_names = list(self.tools.keys())
                    # Also check for common tool names used by coding_agent
                    common_tool_names = ["set_todos", "read_file", "write_file", "task_done", "web_search", "coding_agent"]
                    all_tool_names = list(set(available_tool_names + common_tool_names))
                    
                    # Check if any tool name appears in the response (case-insensitive)
                    full_response_text = (msg_content or content or "").lower()
                    mentioned_tools = [tool_name for tool_name in all_tool_names if tool_name.lower() in full_response_text]
                    
                    # CRITICAL: Only reset if BOTH conditions are met:
                    # 1. Response is empty or effectively empty (checked by empty response handler above)
                    # 2. Tool was mentioned but not called
                    # This is language-independent - we only check if response is empty, not what language it's in
                    if mentioned_tools and not tool_calls:
                        tool_hint = mentioned_tools[0]
                        tui.set_action(f"Tool-Intent detected for '{tool_hint}' without action - resetting...")
                        append_with_context(f"[WARN] Tool mentioned ('{tool_hint}') but not called - resetting context...")
                        tui._needs_update = True
                        time.sleep(0.1)
                        
                        # Find the last tool call or user message to restart from
                        last_tool_idx = None
                        last_user_idx = None
                        
                        # Search backwards through history to find last tool or user message
                        for i in range(len(history) - 1, -1, -1):
                            msg = history[i]
                            if msg.get('role') == 'tool':
                                last_tool_idx = i
                                break
                            elif msg.get('role') == 'user' and last_user_idx is None:
                                last_user_idx = i
                        
                        # Determine where to restart from
                        if last_tool_idx is not None:
                            restart_idx = last_tool_idx + 1
                            restart_from = "tool call"
                        elif last_user_idx is not None:
                            # Check if there's thinking DIRECTLY after user prompt
                            user_prompt_idx = last_user_idx
                            
                            # Check if there's an assistant message with content directly after user prompt
                            first_assistant_after_user = None
                            first_assistant_idx = None
                            
                            # Look at messages right after user prompt (within next 2 messages)
                            for i in range(user_prompt_idx + 1, min(user_prompt_idx + 3, len(history))):
                                msg = history[i]
                                if msg.get('role') == 'assistant' and msg.get('content'):
                                    content_str = str(msg.get('content', ''))
                                    # Keep if it has substantial content (thinking/reasoning)
                                    if len(content_str.strip()) > 20:
                                        first_assistant_after_user = content_str
                                        first_assistant_idx = i
                                        break  # Only take the FIRST one directly after user prompt
                            
                            if first_assistant_after_user and first_assistant_idx is not None:
                                # Keep user prompt + first thinking - this becomes the new snapshot
                                restart_idx = first_assistant_idx + 1
                                restart_from = f"thinking snapshot (user prompt + {len(first_assistant_after_user)} chars of first thinking)"
                            else:
                                # No thinking found - restart from user message (as before)
                                restart_idx = last_user_idx
                                restart_from = "user message"
                        else:
                            # No history to restart from, keep everything
                            restart_idx = 0
                            restart_from = "beginning"
                        
                        # Remove all assistant/system messages after the restart point
                        removed_count = 0
                        while len(history) > restart_idx:
                            last_msg = history[-1]
                            if last_msg.get('role') in ('assistant', 'system'):
                                history.pop()
                                removed_count += 1
                            else:
                                break
                        
                        if removed_count > 0:
                            tui.set_action(f"Reset to {restart_from} (Tool-Intent: {tool_hint})")
                            live.update(tui.render())
                            # CRITICAL: Update history in appropriate dict after reset
                            # Update context state
                            current_state.history = history
                            context_states[current_state.phase] = current_state
                        
                        # Add a brief system prompt (will work better now because first thinking is preserved)
                        history.append({
                            "role": "system",
                            "content": "You didn't respond. Please answer or continue where you left off."
                        })
                        # Update context state
                        current_state.history = history
                        context_states[current_state.phase] = current_state
                        
                        # Continue the loop - if it fails again, this system message will be removed with the reset
                        continue
                
                if is_effectively_empty and not tool_calls:
                    # Empty Response Handler: Remove responses without final answer and restart from last tool call or user message
                    # NO RETRY LIMITS - will loop until we get a response
                    
                    # Find the last tool call or user message to restart from
                    last_tool_idx = None
                    last_user_idx = None
                    
                    # Search backwards through history to find last tool or user message
                    for i in range(len(history) - 1, -1, -1):
                        msg = history[i]
                        if msg.get('role') == 'tool':
                            last_tool_idx = i
                            break
                        elif msg.get('role') == 'user' and last_user_idx is None:
                            last_user_idx = i
                    
                    # Determine where to restart from
                    if last_tool_idx is not None:
                        # Restart from after the last tool result
                        # Keep everything up to and including the tool result:
                        # - Assistant with tool_calls (before tool result) - KEPT
                        # - Tool result (role: 'tool') - KEPT
                        # - Assistant messages without final answer after tool result - REMOVED
                        restart_idx = last_tool_idx + 1
                        restart_from = "tool call"
                    elif last_user_idx is not None:
                        # Restart from the user message (no tool calls yet)
                        # NEW: Check if there's thinking DIRECTLY after user prompt
                        user_prompt_idx = last_user_idx
                        
                        # Check if there's an assistant message with content directly after user prompt
                        first_assistant_after_user = None
                        first_assistant_idx = None
                        
                        # Look at messages right after user prompt (within next 2 messages)
                        for i in range(user_prompt_idx + 1, min(user_prompt_idx + 3, len(history))):
                            msg = history[i]
                            if msg.get('role') == 'assistant' and msg.get('content'):
                                content_str = str(msg.get('content', ''))
                                # Keep if it has substantial content (thinking/reasoning)
                                if len(content_str.strip()) > 20:
                                    first_assistant_after_user = content_str
                                    first_assistant_idx = i
                                    break  # Only take the FIRST one directly after user prompt
                        
                        if first_assistant_after_user and first_assistant_idx is not None:
                            # Keep user prompt + first thinking - this becomes the new snapshot
                            restart_idx = first_assistant_idx + 1  # Keep up to and including first thinking
                            restart_from = f"thinking snapshot (user prompt + {len(first_assistant_after_user)} chars of first thinking)"
                        else:
                            # No thinking found - restart from user message (as before)
                            restart_idx = last_user_idx
                            restart_from = "user message"
                    else:
                        # No history to restart from, keep everything
                        restart_idx = 0
                        restart_from = "beginning"
                    
                    # Remove all assistant/system messages after the restart point that have no final answer
                    # IMPORTANT: Tool results (role: 'tool') and assistant with tool_calls are KEPT
                    # Only assistant messages without final answer are removed (even if they contain reasoning)
                    removed_count = 0
                    while len(history) > restart_idx:
                        last_msg = history[-1]
                        # Remove assistant and system messages that came after the restart point
                        if last_msg.get('role') in ('assistant', 'system'):
                            history.pop()
                            removed_count += 1
                        else:
                            # Stop if we hit a non-assistant/system message (e.g., tool, user)
                            # This ensures we never remove tool results or user messages
                            break
                    
                    if removed_count > 0:
                        tui.set_action(f"Removed {removed_count} response(s) without final answer, restarting from {restart_from}...")
                        live.update(tui.render())
                        # CRITICAL: Update context state after reset
                        current_state.history = history
                        context_states[current_state.phase] = current_state
                    
                    # CRITICAL: If no TODOs set, nudge agent to set them FIRST!
                    # This is the most common cause of "doing nothing" - agent skips TODO setup
                    # Use MAIN context for planning phase
                    if not task_mgr.todos:
                        tui.set_action("No TODO list - nudging...")
                        current_context_manager = main_context_manager
                        history = main_history
                        history.append({
                            "role": "system",
                            "content": (
                                "⚠️ STOP! You sent an empty response without setting up tasks.\n\n"
                                "You MUST:\n"
                                "1. Call `set_todos` with your task breakdown FIRST\n"
                                "2. Then call `read_file` to read the template files\n"
                                "3. Then call `write_file` to modify/create files\n\n"
                                "DO NOT send empty responses. Call a tool NOW!"
                            )
                        })
                        main_history = history  # Update main history
                        continue
                    
                    # Add a brief system prompt (will be removed on next retry if needed)
                    # Use current context (main or task)
                    history.append({
                        "role": "system",
                        "content": "You didn't respond. Please answer or continue where you left off."
                    })
                    
                    # Update history in appropriate dict
                    if current_context_manager == main_context_manager:
                        main_history = history
                    else:
                        # Update current context state
                        current_state.history = history
                        context_states[current_state.phase] = current_state
                    
                    # Continue the loop (no retry limit - will loop until we get a response)
                    # If it fails again, this system message will be removed with the reset
                    continue
            
            # Only record activity if we have content or tool calls
            if tool_calls or (msg_content and msg_content.strip()):
                loop.record_activity()
                # Reset temperature state on success
                # Reset context-specific empty counter
                if is_main_context:
                    main_empty_counter = 0
                else:
                    # Get task index from current_state
                    current_task_idx = current_state.get_task_idx()
                    if current_task_idx is not None:
                        task_empty_counters[current_task_idx] = 0
                
                consecutive_empty = 0
                current_temp = 0.3
            
            # Initialize completion_signals BEFORE msg_content check
            completion_signals = False
            
            # Check for completion signals in both content and msg_content
            all_text_to_check = ""
            if content:
                all_text_to_check += content.upper()
            if msg_content:
                all_text_to_check += " " + msg_content.upper()
            
            if all_text_to_check:
                completion_signals = any(s in all_text_to_check for s in ["DONE", "COMPLETE", "FINISHED", "FERTIG", "ALL TASKS COMPLETED", "TASK COMPLETED", "ALL DONE"])
            
            if msg_content and msg_content.strip():
                # Always show content - platform-independent, no complex checks
                with tui._lock:
                    # Simple check: if buffer only has separator, add content
                    if len(tui.stream_buffer) <= 1:
                        # Force add ALL content - works on Windows, macOS, Linux
                        for line in msg_content.split('\n'):
                            # Filter out redacted tags from each line
                            clean_line = re.sub(r'</?redacted_reasoning>', '', line, flags=re.IGNORECASE)
                            clean_line = re.sub(r'</?think>', '', clean_line, flags=re.IGNORECASE)
                            if clean_line.strip() or clean_line == '':
                                tui.append_stream(clean_line)
                        live.update(tui.render())
            
            # ═══════════════════════════════════════════════════════════════
            # TODO FEEDBACK: If agent had a chance to set TODOs but didn't, tell it
            # ═══════════════════════════════════════════════════════════════
            # After the first loop, TODOs must be set. If they're still empty, inject
            # explicit feedback into the history so the model knows what it missed.
            # IMPORTANT: Skip this check when tool_calls is set — the tool result must
            # immediately follow the assistant+tool_calls message. Injecting a user
            # message between them causes "insufficient tool messages" 400 errors.
            # If tool_calls is non-empty, the write_file handler already rejects writes
            # when todos are missing and returns the error as the tool result.
            if not task_mgr.todos and loop.loop_count >= 2 and not tool_calls:
                called_set_todos = any(
                    tc.get("function", {}).get("name") == "set_todos"
                    for tc in (tool_calls or [])
                )
                if not called_set_todos:
                    tui.append_stream("[FEEDBACK] Agent did not call set_todos — injecting reminder...")
                    history.append({
                        "role": "user",
                        "content": (
                            "⚠️ You have not called set_todos yet.\n"
                            "Before writing any code, you MUST call set_todos with a list of tasks.\n"
                            "This is required — do it NOW as your very next action."
                        ),
                    })
                    current_state.history = history

            # ═══════════════════════════════════════════════════════════════
            # CRITICAL CHECKS - RUN REGARDLESS OF msg_content
            # These checks ensure the agent doesn't skip work!
            # ═══════════════════════════════════════════════════════════════

            # ═══════════════════════════════════════════════════════════════
            # ACTION-REQUIRED MODE: Idle Detection with Task-Context Preservation
            # ═══════════════════════════════════════════════════════════════
            # Track loops without tool calls and force action while preserving task context

            if not tool_calls:
                idle_loop_count += 1
            else:
                idle_loop_count = 0

            # Update TUI with Idle Status
            tui.set_action(f"Thinking... (Idle: {idle_loop_count})")

            # Get current task context for better nudging
            current_task_context = ""
            if task_mgr and task_mgr.todos:
                current_task = task_mgr.get_current_task()
                if current_task:
                    current_task_context = f"\n\n**Your current task:** {current_task}\n**Main goal:** {task}"
            else:
                current_task_context = f"\n\n**Main goal:** {task}"

            if idle_loop_count >= 3:
                # ESCALATION STRATEGY with Task-Context Preservation
                if idle_loop_count == 3:
                    nudge_msg = f"""⚠️ ACTION REQUIRED!

You've been thinking for {idle_loop_count} loops without using any tools.
{current_task_context}

**YOU MUST take action NOW:**
- Call `write_file` to create/update files
- Call `read_file` to check existing code
- Call `task_done` if the current task is complete

STOP THINKING. START DOING."""

                elif idle_loop_count == 5:
                    nudge_msg = f"""🚨 FINAL WARNING (Idle: {idle_loop_count})

You are STUCK in analysis paralysis!
{current_task_context}

**IMMEDIATE ACTION REQUIRED:**
1. If you need to write code → Call `write_file` NOW
2. If task is done → Call `task_done` NOW
3. If you need info → Call `read_file` NOW

DO NOT respond with more text. ONLY use tools."""

                else:
                    nudge_msg = f"""🛑 SYSTEM OVERRIDE (Idle: {idle_loop_count})

STOP ALL THINKING IMMEDIATELY.
{current_task_context}

**MANDATORY:** Your next response MUST contain a tool call.
No explanations. No planning. Just ACTION.

Call `write_file`, `read_file`, or `task_done` RIGHT NOW."""

                # Inject as SYSTEM message for maximum authority while preserving context
                history.append({
                    "role": "system",
                    "content": nudge_msg
                })

                # Update context state
                current_state.history = history
                context_states[current_state.phase] = current_state

                tui.set_action(f"⚠️ FORCING ACTION (Idle: {idle_loop_count})")
                _trace(f"Triggered Action-Required Mode (Count: {idle_loop_count}, Task: {current_task_context[:100]})")

                # If we hit 8 idle loops, force context compression with task reminder
                if idle_loop_count >= 8:
                    _trace("Idle limit critical (8). Compressing context with task reminder.")
                    # Keep system prompt + task reminder + last 3 messages
                    system_msgs = [m for m in history if m.get("role") == "system"]
                    recent_msgs = history[-3:] if len(history) > 3 else []

                    history = system_msgs[:1] + [
                        {
                            "role": "system",
                            "content": f"""🔄 CONTEXT RESET - You were stuck in thinking loops.

{current_task_context}

**RESET:** Start fresh but remember your goal.
**ACTION:** Call tools immediately - no more thinking!"""
                        }
                    ] + recent_msgs

                    # Update context state
                    current_state.history = history
                    context_states[current_state.phase] = current_state
                    idle_loop_count = 0  # Reset counter after compression

            # Initialize flags to prevent NameError
            has_task_done = False
            has_write_file = False
            only_reading = False
            
            # FIRST: No TODOs set yet? Auto-generate or nudge
            # IMPORTANT: Skip if the model IS calling a tool (e.g. set_todos) — dispatching
            # the tool will set the todos; jumping to continue here would skip that dispatch.
            if not task_mgr.todos and loop.loop_count >= 1 and not tool_calls:
                # After 5 loops: AUTO-GENERATE TODOs from task description
                # Increased from 3 to 5 to give agent more time to plan
                if loop.loop_count >= 5:
                    tui.set_action("Auto-generating TODOs...")
                    
                    # Generate sensible TODOs from the task description
                    auto_todos = []
                    task_lower = task.lower()
                    task_upper = task.upper()
                    
                    # Check for CONTENT_ONLY mode first (highest priority)
                    is_content_only = (
                        "CONTENT_ONLY" in task_upper or 
                        "ONLY THE" in task_upper or 
                        "RETURN ONLY" in task_upper or
                        "NO PROJECT" in task_upper or
                        "NO FILE PATHS" in task_upper
                    )
                    
                    # Check for Script/Python task (STRICTER - use Regex word boundaries)
                    script_keywords = ['script', 'python', 'bash', 'shell', 'cli', 'automation']
                    is_script_task = False
                    trigger_kw = None
                    
                    for kw in script_keywords:
                        if re.search(rf'\b{kw}\b', task_lower):
                            is_script_task = True
                            trigger_kw = kw
                            break
                    
                    # If HTML/Web is mentioned, it is NOT a script task unless explicitly stated
                    if "html" in task_lower or "web" in task_lower:
                        is_script_task = False
                    
                    if is_script_task:
                        tui.append_stream(f"[DEBUG] Detected script task (keyword: '{trigger_kw}')")
                    
                    if _detect_single_file_deliverable(task):
                        # Planning rule: single-file deliverable -> exactly 1 task,
                        # also when the supervisor generates the plan itself.
                        auto_todos = [
                            "Create the complete deliverable in a single file with all required functionality"
                        ]
                    elif is_content_only:
                        if "html" in task_lower or "webpage" in task_lower:
                            auto_todos = [
                                "Generate complete HTML document with all required content",
                                "Add embedded CSS styling",
                                "Verify HTML is complete and valid"
                            ]
                        else:
                            auto_todos = [
                                "Generate requested content",
                                "Verify content is complete"
                            ]
                    elif is_script_task and not is_content_only:
                        auto_todos = [
                            "Create main Python script",
                            "Implement core logic and functions",
                            "Add error handling and comments",
                            "Test script execution"
                        ]
                    elif any(kw in task_lower for kw in ['multi-page', 'multiple pages', 'several pages', 'about page', 'contact page', 'services page', 'create pages']):
                        auto_todos = [
                            "Read and customize index.html (homepage)",
                            "Create additional pages (about, contact, services)",
                            "Update styles.css for all pages",
                            "Add navigation between pages",
                            "Test all pages and links"
                        ]
                    elif any(kw in task_lower for kw in ['website', 'webseite', 'webseit', 'websit', 'homepage', 'landing page']) and not is_content_only:
                        if template_files:
                            auto_todos = [
                                "Read and analyze existing template files",
                                "Customize index.html with task-specific content",
                                "Update styles.css with appropriate styling",
                                "Add JavaScript functionality if needed",
                                "Verify all files are complete and working"
                            ]
                        else:
                            auto_todos = [
                                "Create index.html with task-specific content",
                                "Create styles.css with appropriate styling",
                                "Add JavaScript functionality if needed",
                                "Verify all files are complete and working"
                            ]
                    else:
                        auto_todos = [
                            "Create project structure and main files",
                            "Add logic and functionality",
                            "Test and verify output"
                        ]
                    
                    # Set the auto-generated TODOs
                    task_mgr.set_todos(auto_todos)
                    try:
                        if lg:
                            lg.event(
                                "todos_set",
                                source="auto_generated",
                                tasks_count=len(auto_todos),
                                tasks_preview=[str(t)[:120] for t in auto_todos[:5]],
                                current_task_idx=getattr(task_mgr, "current_task_idx", None),
                                current_task_preview=(str(task_mgr.get_current_task() or "")[:180]),
                            )
                    except Exception:
                        pass
                    tui.set_action(f"TODO: {len(auto_todos)} tasks")
                    tui.append_stream(f"Auto-generated {len(auto_todos)} tasks (model didn't call set_todos)")
                    for i, t in enumerate(auto_todos, 1):
                        tui.append_stream(f"   {i}. {t}")
                    
                    # CRITICAL: Force immediate TUI update to show TODOs
                    tui._needs_update = True
                    try:
                        live.update(tui.render())
                    except Exception:
                        pass
                    time.sleep(0.1)  # Give animation thread time to render
                    
                    # CRITICAL: Switch to task context for first task (same as in set_todos handler)
                    if task_mgr.current_task_idx == 0:
                        first_task = task_mgr.get_current_task()
                        if first_task:
                            new_context_manager, new_history = create_fresh_context_for_task(0, first_task)
                            # CRITICAL: Update variables to switch to task context
                            current_context_manager = new_context_manager
                            history = new_history
                            # Context state already managed by switch_to_task_context
                            # No additional storage needed
                            history_snapshot_len = len(history)
                            tui.append_stream(f"🔄 Switched to Task 1 context: {first_task[:40]}")
                            # Debug: Verify context switch worked
                            is_now_task = (current_context_manager != main_context_manager)
                            tui.append_stream(f"[DEBUG] Context switch verified: is_task_context={is_now_task}")
                            # Force update after context switch
                            tui._needs_update = True
                            try:
                                live.update(tui.render())
                            except Exception:
                                pass
                            time.sleep(0.1)
                    
                    # CRITICAL: Nudge the model to start working on the TODOs
                    current_task = task_mgr.get_current_task()
                    if current_task:
                        history.append({
                            "role": "system",
                            "content": (
                                f"✅ TODO list auto-generated with {len(auto_todos)} tasks.\n\n"
                                f"**Current task:** {current_task}\n\n"
                                f"**MANDATORY WORKFLOW:**\n"
                                f"1. Start working on the FIRST task: {current_task}\n"
                                f"2. You MUST call `write_file` to create the code for this task. `task_done` does NOT write code.\n"
                                f"3. Call `task_done` ONLY when the file is created\n"
                                f"4. Move to the next task\n\n"
                                f"DO NOT stop - work through ALL {len(auto_todos)} tasks!"
                            )
                        })
                    # Don't continue - let the loop proceed with the TODOs
                else:
                    tui.set_action("No TODO list!")
                    nudge_msg = "⚠️ You haven't set a TODO list yet!\nCall `set_todos` with your task breakdown FIRST."
                    history.append({"role": "system", "content": nudge_msg})
                    continue
            
            # SECOND: Check for Task-Stuck (Infinite Loop on same task)
            # After 15 loops on the same task: verify the goal first. Verified ->
            # complete with evidence. Not verified -> one retry in a fresh context,
            # then an honest 'failed' (never a silent fake 'completed').
            current_task_idx = task_mgr.current_task_idx
            if current_task_idx < len(task_mgr.todos):
                # Check how long we've been on this task
                if not hasattr(loop, 'task_start_loop'):
                    loop.task_start_loop = {}

                if current_task_idx not in loop.task_start_loop:
                    loop.task_start_loop[current_task_idx] = loop.loop_count

                loops_on_task = loop.loop_count - loop.task_start_loop[current_task_idx]

                if loops_on_task > 15:
                    tui.set_action("Task stuck - verifying goal...")
                    current_title = task_mgr.get_current_task() or ""
                    verified, evidence = _verify_task_goal(
                        current_title,
                        task_file_map.get(current_task_idx, []),
                        base_dir,
                        linter_active=bool(getattr(current_state, 'linter_errors_active', False)),
                        llm_verify=_llm_verify_call,
                    )
                    retry_count = task_retry_counts.get(current_task_idx, 0)
                    try:
                        if lg:
                            lg.event(
                                "task_stuck_verification",
                                task_idx=current_task_idx,
                                loops_on_task=loops_on_task,
                                verified=verified,
                                evidence=evidence[:300],
                                retry_count=retry_count,
                            )
                    except Exception:
                        pass

                    if verified:
                        tui.append_stream(f"[AUTO] Task {current_task_idx+1} stuck for {loops_on_task} loops - goal verified, completing.")
                        task_mgr.complete_current_task(
                            f"Auto-completed after stuck detection - goal verified: {evidence}"
                        )
                    elif retry_count < 1:
                        # One immediate retry: fresh context (drop the poisoned one)
                        # plus an explicit hint about what went wrong.
                        task_retry_counts[current_task_idx] = retry_count + 1
                        task_mgr.reset_task_for_retry(
                            current_task_idx, f"stuck after {loops_on_task} loops; {evidence}"
                        )
                        loop.task_start_loop[current_task_idx] = loop.loop_count
                        context_states.pop(f"task_{current_task_idx}", None)
                        tui.append_stream(f"[AUTO] Task {current_task_idx+1} stuck, goal not verified - retrying with fresh context.")
                        if switch_to_task_context(current_task_idx, current_title):
                            history.append({
                                "role": "system",
                                "content": (
                                    f"RETRY: The previous attempt at this task got stuck after {loops_on_task} loops "
                                    f"and the goal could not be verified ({evidence}). "
                                    f"Take a DIFFERENT approach now: read the relevant file, make the required "
                                    f"change with write_file, then call task_done."
                                ),
                            })
                        continue
                    else:
                        tui.append_stream(f"[FAIL] Task {current_task_idx+1} failed - goal not verified after retry.")
                        task_mgr.fail_current_task(
                            f"Stuck after {loops_on_task} loops, retry exhausted - {evidence}"
                        )

                    # Move to next task context (after verified-complete or fail)
                    next_task = task_mgr.get_current_task()
                    if next_task:
                        task_idx = task_mgr.current_task_idx
                        if switch_to_task_context(task_idx, next_task):
                            pass  # Context switched successfully
                        tui.append_stream(f"[AUTO] Switched to Task {task_idx + 1}: {next_task[:40]}")

                    continue # Skip to next loop with new task
            
            # If model claims completion without TODOs, force it to set them first.
            # IMPORTANT: Skip if model IS calling a tool (e.g. set_todos) — words like
            # "complete" are common in coding context ("write a complete HTML file") and
            # must NOT intercept an actual tool call.
            if completion_signals and not task_mgr.todos and not tool_calls:
                tui.set_action("set_todos first")
                history.append({
                    "role": "system",
                    "content": "⚠️ You cannot complete yet. First call set_todos with your task breakdown, then work through each task and call task_done after each."
                })
                continue
            
            # Premature completion (said DONE but TODOs not finished)
            # CRITICAL: This check MUST run even if completion_signals is False, to catch any completion attempts
            # IMPORTANT: Skip if the model IS calling a tool — it's doing real work, not claiming completion.
            #            Words like "complete", "ready", "finished" are common in coding context
            #            ("create a complete CSS file") and must NOT intercept actual tool calls.
            if task_mgr.todos and not task_mgr.is_all_done() and not tool_calls:
                # Check if model is trying to complete (either via signal or by not working on tasks)
                is_trying_to_complete = completion_signals or (
                    not tool_calls and
                    msg_content and
                    len(msg_content.strip()) < 100  # Short messages might be completion attempts
                ) or (
                    msg_content and
                    any(phrase in msg_content.lower() for phrase in [
                        "i'm done", "i am done", "i'm finished", "i am finished",
                        "task is complete", "tasks are complete", "all done",
                        "fertig", "abgeschlossen", "erledigt", "website is ready",
                        "website is complete", "website is finished", "ready",
                        "completed", "finished", "done with", "fertig mit"
                    ])
                )

                if is_trying_to_complete:
                    remaining = [t["task"] for t in task_mgr.todos if t["status"] != "completed"]
                    completed_count = len([t for t in task_mgr.todos if t["status"] == "completed"])
                    total_count = len(task_mgr.todos)
                    
                    tui.set_action(f"{len(remaining)}/{total_count} tasks remaining!")
                    history.append({
                        "role": "system",
                        "content": (
                            f"🚨 NOT DONE YET!\n\n"
                            f"Progress: {completed_count}/{total_count} tasks completed\n\n"
                            f"You still have {len(remaining)} tasks to complete:\n" +
                            "\n".join(f"- {t}" for t in remaining[:10]) +
                            f"\n\n**MANDATORY WORKFLOW:**\n"
                            f"1. Work on the CURRENT task: {task_mgr.get_current_task()}\n"
                            f"2. Use tools (write_file, read_file, etc.) to complete the task\n"
                            f"3. Call `task_done` ONLY after the task is actually finished\n"
                            f"4. Repeat for each remaining task\n\n"
                            f"DO NOT claim completion until ALL {total_count} tasks are done!\n"
                            f"DO NOT skip tasks or call task_done without completing the work!"
                        )
                    })
                    continue
            
            # Additional check: If agent claims completion in response but didn't call task_done
            # This catches cases where the agent says "done" but hasn't actually completed tasks
            # IMPORTANT: Skip if model IS calling a tool — phrase matches like "complete"/"ready"
            #            are common in coding context and must NOT intercept real tool calls.
            if msg_content and task_mgr.todos and not task_mgr.is_all_done() and not tool_calls:
                # Check for completion phrases in the response
                completion_phrases = [
                    "all tasks completed", "all done", "finished", "complete",
                    "fertig", "abgeschlossen", "website is ready", "website is complete",
                    "website is finished", "i'm done", "i am done", "ready",
                    "completed", "done with", "fertig mit"
                ]
                if any(phrase in msg_content.lower() for phrase in completion_phrases):
                    # Agent claimed completion but didn't call task_done
                    remaining = [t["task"] for t in task_mgr.todos if t["status"] != "completed"]
                    completed_count = len([t for t in task_mgr.todos if t["status"] == "completed"])
                    total_count = len(task_mgr.todos)
                    current = task_mgr.get_current_task()
                    
                    tui.set_action(f"{completed_count}/{total_count} tasks - NOT DONE!")
                    history.append({
                        "role": "system",
                        "content": (
                            f"🚨 YOU CANNOT BE DONE YET!\n\n"
                            f"Progress: {completed_count}/{total_count} tasks completed\n\n"
                            f"You claimed completion, but you haven't called `task_done` for any tasks!\n\n"
                            f"**Current task:** {current}\n"
                            f"**Remaining tasks:**\n" +
                            "\n".join(f"- {t}" for t in remaining) +
                            f"\n\n**MANDATORY WORKFLOW:**\n"
                            f"1. Work on the FIRST task: {current}\n"
                            f"2. Use tools (read_file, write_file, etc.) to complete it\n"
                            f"3. Call `task_done` when finished\n"
                            f"4. Repeat for ALL {total_count} tasks\n\n"
                            f"DO NOT claim completion until you've called `task_done` for ALL tasks!\n"
                            f"DO NOT say 'done' or 'finished' until ALL tasks are completed!"
                        )
                    })
                    continue
            
            # CRITICAL: Always check if tasks are incomplete, regardless of completion signals
            # This prevents the model from stopping work prematurely
            # IMPORTANT: Skip nudges if tool_calls exist - let them execute first!
            can_complete = False
            if task_mgr.todos and not task_mgr.is_all_done() and not tool_calls:
                remaining = [t["task"] for t in task_mgr.todos if t["status"] != "completed"]
                current = task_mgr.get_current_task()
                completed_count = len([t for t in task_mgr.todos if t["status"] == "completed"])
                total_count = len(task_mgr.todos)
                
                # Check if model is making progress on the current task
                # If no tool calls OR only read_file calls without write_file, nudge to continue
                has_write_file = any(tc.get('function', {}).get('name') == 'write_file' for tc in tool_calls) if tool_calls else False
                has_read_file = any(tc.get('function', {}).get('name') == 'read_file' for tc in tool_calls) if tool_calls else False
                has_task_done_call = any(tc.get('function', {}).get('name') == 'task_done' for tc in tool_calls) if tool_calls else False
                only_reading = has_read_file and not has_write_file
                
                # Track write_file activity in recent loops
                # Update tracking: check if write_file was called in last 3 loops
                if loop.loop_count > 3:
                    # Keep only last 3 loops
                    recent_loop_write_files = recent_loop_write_files[-3:]
                # Mark this loop as having no write_file yet (will be updated if write_file is called)
                if loop.loop_count > len(recent_loop_write_files):
                    recent_loop_write_files.append(False)
                has_write_file_in_recent_loops = any(recent_loop_write_files)

                # Track consecutive loops without any tool action
                if not hasattr(loop, "no_action_since"):
                    loop.no_action_since = 0
                if tool_calls:
                    loop.no_action_since = 0
                else:
                    loop.no_action_since += 1
                
                # Nudge if:
                # 1. No tool calls at all (idle)
                # 2. Only reading files but not writing (not making progress)
                # 3. No write_file calls in recent loops (stuck)
                # 4. Loop count >= 1 (earlier nudge for faster response)
                
                # CRITICAL: Also nudge if we created the file for the current task but haven't called task_done!
                current_task_idx = task_mgr.current_task_idx
                files_for_task = task_file_map.get(current_task_idx, [])
                task_file_exists = len(files_for_task) > 0
                
                # Only nudge if we are NOT currently writing a file!
                should_nudge = (
                    (not has_write_file and (not tool_calls or only_reading or not has_write_file_in_recent_loops)) or
                    (task_file_exists and not has_task_done_call) # Nudge IMMEDIATELY if file exists but task not done
                ) and loop.loop_count >= 1 and current
                
                if should_nudge:
                    tui.set_action(f"{completed_count}/{total_count} tasks - Continue working!")

                    # Log WHY we are nudging (helps debug "stuck" situations)
                    try:
                        if lg:
                            lg.event(
                                "nudge",
                                loop=loop.loop_count,
                                progress=f"{completed_count}/{total_count}",
                                current_task_idx=current_task_idx,
                                current_task_preview=(current[:180] if current else ""),
                                no_tool_calls=not bool(tool_calls),
                                only_reading=bool(only_reading),
                                no_write_in_recent_loops=not bool(has_write_file_in_recent_loops),
                                task_file_exists=bool(task_file_exists),
                                has_task_done_call=bool(has_task_done_call),
                                no_action_since=getattr(loop, "no_action_since", None),
                                forced_completion_hint=bool(task_file_exists and getattr(loop, "no_action_since", 0) >= 2),
                            )
                    except Exception:
                        pass
                    
                    # If we already have files for this task and keep thinking, emit a forced completion hint
                    if task_file_exists and loop.no_action_since >= 2:
                        completion_signals = True  # allow completion path
                        history.append({
                            "role": "system",
                            "content": (
                                "✅ You have already created/updated files for this task: "
                                f"{', '.join([os.path.basename(f) for f in files_for_task])}.\n\n"
                                "If anything is still missing, use tools now (write_file/read_file/etc.).\n"
                                "If the task is finished, call `task_done(summary=\"...\")` NOW.\n"
                                "Do not keep thinking without a tool or `task_done`."
                            )
                        })
                        loop.no_action_since = 0
                    
                    if task_file_exists:
                        nudge_content = (
                            f"🚨 [bold red]CRITICAL: TASK ALREADY FINISHED![/]\n\n"
                            f"**Task:** {current}\n"
                            f"**Status:** The file(s) {', '.join([os.path.basename(f) for f in files_for_task])} have been successfully created/modified.\n\n"
                            f"**MANDATORY ACTION:**\n"
                            f"You are wasting tokens by thinking. Call `task_done(summary='...')` NOW to finish this task.\n"
                            f"Do NOT write any more code for this task. MOVE TO THE NEXT ONE."
                        )
                        tui.set_action(f"Task finished - forcing task_done...")
                    else:
                        nudge_content = (
                            f"⚠️ TASKS INCOMPLETE - Continue working!\n\n"
                            f"**Progress:** {completed_count}/{total_count} tasks completed\n"
                            f"**Remaining:** {len(remaining)} tasks\n\n"
                            f"**Current task:** {current}\n\n"
                        )
                    
                    if only_reading:
                        nudge_content += (
                            f"You've read files, but you need to WRITE changes!\n\n"
                            f"**Action required:**\n"
                            f"1. Use `write_file` to modify the files you read\n"
                            f"2. Complete the current task: {current}\n"
                            f"3. Call `task_done` when the task is finished\n"
                            f"4. Move to the next task\n\n"
                        )
                    else:
                        nudge_content += (
                            f"**MANDATORY WORKFLOW:**\n"
                            f"1. Work on the CURRENT task: {current}\n"
                            f"2. Use tools (write_file, read_file, etc.) to complete the task\n"
                            f"3. Call `task_done` ONLY after the task is actually finished\n"
                            f"4. Repeat for each remaining task\n\n"
                        )
                    
                    nudge_content += (
                        f"**Remaining tasks:**\n" +
                        "\n".join(f"- {t}" for t in remaining[:10]) +
                        f"\n\nDO NOT stop until ALL {total_count} tasks are completed!\n"
                        f"DO NOT skip tasks or claim completion prematurely!"
                    )
                    
                    history.append({
                        "role": "system",
                        "content": nudge_content
                    })
                    _trace("Sent nudge to continue/finish task")
                    continue

                # AUTO-COMPLETE SAFETY: If files exist for current task but we keep idling, auto-complete to break deadlock
                if task_file_exists and loop.no_action_since >= 4:
                    # Same goal verification as stuck detection: file evidence must
                    # be clean (exists, no placeholders, no linter errors). If not,
                    # keep the task open — stuck detection escalates with retry/fail.
                    _ac_verified, _ac_evidence = _verify_task_goal(
                        task_mgr.get_current_task() or "",
                        files_for_task,
                        base_dir,
                        linter_active=bool(getattr(current_state, 'linter_errors_active', False)),
                        llm_verify=None,
                    )
                    if not _ac_verified:
                        tui.append_stream(f"[AUTO] Inactivity auto-complete blocked - {_ac_evidence[:80]}")
                        history.append({
                            "role": "system",
                            "content": (
                                f"Auto-complete blocked: {_ac_evidence}. "
                                f"Fix this, then call task_done."
                            ),
                        })
                        continue
                    summary = (
                        f"Auto-completed after inactivity - goal verified: {_ac_evidence}"
                    )
                    task_mgr.complete_current_task(summary)
                    next_task = task_mgr.get_current_task()
                    tui.append_stream(f"✅ Auto-completed: {current[:60] if current else 'task'}")
                    tui.set_action(f"📋 {task_mgr.get_progress()}")
                    loop.no_action_since = 0

                    # Archive/compress current history to keep context small
                    current_context_manager.compress(history)

                    if next_task:
                        completed_info = "\n".join([f"- {t['task']}: {t.get('result', 'done')}" for t in task_mgr.todos if t['status'] == 'completed'])
                        task_idx = task_mgr.current_task_idx
                        current_context_manager, history = create_fresh_context_for_task(task_idx, next_task, completed_info)
                        history_snapshot_len = len(history)
                        tui.append_stream(f"🔄 Fresh context created for Task {task_idx + 1}: {next_task[:40]}")
                        result = f"✅ Task auto-completed.\n\n## NEXT TASK:\n{next_task}\n\nFocus only on this task now."
                    elif task_mgr.is_all_done():
                        if _maybe_start_final_retry():
                            continue
                        result = "🎉 ALL TASKS COMPLETED! Verify your work and say 'ALL TASKS COMPLETED'."
                        tui.append_stream("🎉 All tasks done (auto-complete)!")
                        break  # Exit loop - all tasks completed
                    else:
                        result = "✅ Task auto-completed. Continue with remaining work."

                    # Skip rest of loop, since we advanced the task
                    continue
            
                # Only accept completion if:
                # 1. TODO list is done AND at least 3 loops completed (minimum work done)
                # 2. Must have actually used write_file (not just template files)
                min_loops_required = 3  # Require at least 3 loops to complete
                has_done_real_work = (
                    len(files_created) > len(template_files) or
                    len(task_file_map.get(task_mgr.current_task_idx, [])) > 0
                )  # Created/modified files for current task
                can_complete = task_mgr.is_all_done() and loop.loop_count >= min_loops_required and has_done_real_work
            
            # CRITICAL: Only allow completion if ALL conditions are met
            if completion_signals and can_complete:
                # Verify quality
                tui.set_action("Checking quality...")
                quality = QualityChecker.check_files(files_created, task, base_dir)
                
                # CRITICAL: Check for placeholders - they BLOCK completion
                placeholder_check = QualityChecker.check_placeholders(files_created, base_dir)
                if placeholder_check['has_placeholders']:
                    # Placeholders found - BLOCK completion
                    placeholder_list = []
                    for fname, placeholders in placeholder_check['files_with_placeholders'].items():
                        placeholder_list.append(f"**{fname}**: {len(placeholders)} placeholders")
                        for p in placeholders[:3]:  # Show first 3 per file
                            placeholder_list.append(f"  - {p}")
                    
                    tui.set_action(f"{placeholder_check['total_placeholders']} placeholders found!")
                    history.append({
                        "role": "system",
                        "content": (
                            f"🚨 COMPLETION BLOCKED: {placeholder_check['total_placeholders']} placeholders found!\n\n"
                            f"You MUST replace ALL placeholders before completion:\n\n" +
                            "\n".join(placeholder_list[:10]) +  # Limit to 10 items
                            f"\n\n**Action required:**\n"
                            f"1. Read each file with placeholders\n"
                            f"2. Replace ALL placeholder text with real content\n"
                            f"3. Write the files back with replacements\n"
                            f"4. Call task_done only after ALL placeholders are replaced\n\n"
                            f"DO NOT claim completion until ALL placeholders are replaced!"
                        )
                    })
                    continue
                
                if quality['passed']:
                    # SUCCESS!
                    
                    # CONTENT_ONLY mode: Return actual file content
                    if skip_template and files_created:
                        main_file = None
                        main_file_patterns = ['index.html', 'main.py', 'app.py', 'script.py', 'main.js', 'app.js']
                        for pattern in main_file_patterns:
                            for f in files_created:
                                if os.path.basename(f).lower() == pattern.lower():
                                    main_file = f
                                    break
                            if main_file:
                                break
                        if not main_file and files_created:
                            main_file = files_created[0]
                        
                        if main_file:
                            try:
                                full_path = main_file if os.path.isabs(main_file) else os.path.join(base_dir, main_file)
                                if os.path.exists(full_path):
                                    with open(full_path, 'r', encoding='utf-8') as f:
                                        content = f.read()
                                    # Clean up temporary directory
                                    try:
                                        import shutil
                                        shutil.rmtree(base_dir)
                                    except:
                                        pass
                                    return content
                            except Exception:
                                pass  # Fallback to summary
                    
                    # Normal mode: exit the loop — the common post-loop summary
                    # handles status (COMPLETE vs. PARTIAL incl. failed tasks) and
                    # the final commit for every exit path identically.
                    if _maybe_start_final_retry():
                        continue
                    tui.append_stream("Completion signal accepted - finishing run")
                    break
                else:
                    # Quality check failed - force more work
                    feedback = []
                    if quality['missing_types']:
                        feedback.append(f"Missing: {', '.join(quality['missing_types'])}")
                    if quality['small_files']:
                        feedback.append(f"Too small: {', '.join(quality['small_files'])}")
                    if quality['errors']:
                        feedback.append(f"Errors: {', '.join(quality['errors'])}")
                    
                    tui.set_action(f"Quality: {feedback[0] if feedback else 'check failed'}")
                    
                    history.append({
                        "role": "system",
                        "content": f"⚠️ INCOMPLETE!\n{chr(10).join(feedback)}\n\nFix these issues and try again."
                    })
                    continue
            
            # Plan-only detection (mentioned files but didn't create them)
            # CRITICAL FIX: Only check this if NO tool calls! 
            # If tool calls exist, execute them instead of complaining.
            if msg_content and not tool_calls:
                file_mentions = re.findall(r'[\w-]+\.(html|css|js|py|ts|json)', msg_content.lower())
                if file_mentions and len(files_created) <= len(template_files):
                    tui.set_action("Plan detected - forcing execution")
                    history.append({
                        "role": "system",
                        "content": f"⚠️ You mentioned files but didn't create them!\n"
                                   f"Files mentioned: {', '.join(file_mentions[:5])}\n"
                                   f"Call write_file NOW."
                    })
                    
                    # Update history in appropriate dict
                    if current_context_manager == main_context_manager:
                        main_history = history
                    else:
                        # Update current context state
                        current_state.history = history
                        context_states[current_state.phase] = current_state
                    continue
            
            # ═══════════════════════════════════════════════════════════════
            # EXECUTE TOOL CALLS
            # ═══════════════════════════════════════════════════════════════

            # Track repeated errors to prevent infinite loops
            if not hasattr(loop, 'error_history'):
                loop.error_history = []
            
            # CRITICAL FIX: Ensure tool calls are actually executed!
            if tool_calls:
                tui.append_stream(f"[DEBUG] EXEC: Found {len(tool_calls)} tool calls to execute")
                loop.record_activity() # Reset idle timer

            # Initialize task flags at the start of the loop
            current_task_name = task_mgr.get_current_task()
            task_lower_name = current_task_name.lower() if current_task_name else ""
            is_create_task_init = any(kw in task_lower_name for kw in ['create', 'generate', 'write', 'build', 'implement', 'make'])
            is_file_task = is_create_task_init or any(kw in task_lower_name for kw in ['edit', 'modify', 'update', 'fix', 'change', 'add', 'file'])

            # CRITICAL: Extensive Debugging for Tool Execution
            # Route debug traces into the per-subagent debug logger (repo-local).
            def _log_to_file(msg):
                try:
                    if lg:
                        lg.event("coder_debug", message=str(msg)[:1000])
                except Exception:
                    pass

            if tool_calls:
                msg = f"[DEBUG-X] Found {len(tool_calls)} potential tool calls in response."
                tui.append_stream(msg)
                _log_to_file(msg)
                for i, tc in enumerate(tool_calls):
                    # Log name and full arguments to see what's happening
                    tc_name = tc.get('function', {}).get('name')
                    tc_args = tc.get('function', {}).get('arguments')
                    msg = f"[DEBUG-X] Tool {i+1}: {tc_name} | Args Len: {len(tc_args) if tc_args else 0}"
                    tui.append_stream(msg)
                    _log_to_file(f"Tool {i+1} Name: {tc_name}")
                    # Do NOT log raw arguments (can include file contents). Length is enough for debugging.
                    _log_to_file(f"Tool {i+1} Args Len: {len(tc_args) if tc_args else 0}")

            for tc in tool_calls:
                fn_name = tc['function']['name'].strip()
                fn_args_str = tc['function']['arguments']

                # CRITICAL: Save history reference NOW, before tool execution.
                # Some tools (set_todos) trigger a context switch via sync_legacy_vars(),
                # which reassigns `history` to the NEW context. The tool result must go
                # into the history that was active when this tool was called, otherwise
                # we get an orphaned role='tool' message in the fresh task context,
                # causing a 400: "Messages with role 'tool' must be a response to a
                # preceding message with 'tool_calls'".
                _history_at_dispatch = history

                # Messages that tool handlers want to append to history (nudges, linter
                # output, truncation warnings, etc.) MUST be added AFTER the tool result,
                # not before. Adding them before the tool result creates an invalid sequence:
                #   assistant+tool_calls → system(nudge) → role:tool
                # which causes "insufficient tool messages" 400 errors on strict APIs.
                # Handlers must use _post_tool_messages.append(...) instead of history.append.
                _post_tool_messages: list = []

                # DEBUG: Trace execution flow
                msg = f"[DEBUG-X] EXEC START: Processing '{fn_name}'"
                tui.append_stream(msg)
                _log_to_file(msg)
                
                # Check if base_dir exists and is writable
                if fn_name == "write_file":
                    if not os.path.exists(base_dir):
                        msg = f"[DEBUG-X] CRITICAL: Base dir {base_dir} does not exist!"
                        tui.append_stream(msg)
                        _log_to_file(msg)
                        try:
                            os.makedirs(base_dir, exist_ok=True)
                            msg = f"[DEBUG-X] Created base dir."
                            tui.append_stream(msg)
                            _log_to_file(msg)
                        except Exception as e:
                            msg = f"[DEBUG-X] FAILED to create base dir: {e}"
                            tui.append_stream(msg)
                            _log_to_file(msg)
                
                if fn_name in self.local_tools:
                     # tui.append_stream(f"[DEBUG] Found in local_tools")
                     pass
                elif fn_name not in ["task_done", "set_todos", "web_fetch", "web_deep_search", "coding_agent", "git_init", "git_add_commit", "git_status", "git_log"]:
                     msg = f"[DEBUG-X] Tool '{fn_name}' NOT found in local_tools. Available: {list(self.local_tools.keys())}"
                     tui.append_stream(msg)
                     _log_to_file(msg)
                
                json_error = None
                try:
                    parsed = json.loads(fn_args_str)
                    if isinstance(parsed, dict):
                        fn_args = parsed
                        msg = f"[DEBUG-X] JSON parse OK."
                        tui.append_stream(msg)
                        _log_to_file(msg)
                    elif isinstance(parsed, list) and fn_name == "set_todos":
                        # Auto-fix for set_todos(["task1", "task2"])
                        fn_args = {"tasks": parsed}
                        msg = f"[INFO] Auto-corrected list args for set_todos"
                        tui.append_stream(msg)
                        _log_to_file(msg)
                    elif isinstance(parsed, str) and fn_name == "set_todos":
                        # Auto-fix for set_todos("task1")
                        fn_args = {"tasks": [parsed]}
                        msg = f"[INFO] Auto-corrected string arg for set_todos"
                        tui.append_stream(msg)
                        _log_to_file(msg)
                    else:
                        msg = f"[WARN] JSON parsed to {type(parsed)}, expected dict. Using empty dict."
                        tui.append_stream(msg)
                        _log_to_file(msg)
                        fn_args = {}
                except Exception as e:
                    json_error = str(e)
                    msg = f"[ERROR] JSON parse error for {fn_name}: {e}"
                    tui.append_stream(msg)
                    _log_to_file(msg)
                    msg = f"[DEBUG] Raw args (len={len(fn_args_str)}): {fn_args_str[:100]}..."
                    tui.append_stream(msg)
                    _log_to_file(msg)
                    
                    # ROBUST FALLBACK: Regex Extraction
                    # If JSON fails (common with long file content), try to extract path and content directly
                    
                    # 1. Handle write_file (Path + Content)
                    if fn_name == "write_file":
                        extracted_args = {}
                        # Extract path
                        path_match = re.search(r'"path"\s*:\s*"([^"]+)"', fn_args_str)
                        if path_match:
                            extracted_args["path"] = path_match.group(1)
                        
                        # Extract content
                        content_match = re.search(r'"content"\s*:\s*"(.*)', fn_args_str, re.DOTALL)
                        if content_match:
                            content_raw = content_match.group(1)
                            end_quote_match = re.search(r'(.*)"\s*\}\s*$', content_raw, re.DOTALL)
                            if end_quote_match:
                                content_clean = end_quote_match.group(1)
                            else:
                                content_clean = content_raw.rstrip('"} ]')
                                if content_clean.endswith('"'): content_clean = content_clean[:-1]
                            
                            content_clean = content_clean.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
                            extracted_args["content"] = content_clean
                            
                        if "path" in extracted_args and "content" in extracted_args:
                            # CRITICAL: Check if we are just rewriting the same content
                            path = extracted_args["path"]
                            content = extracted_args["content"]
                            full_path = path if os.path.isabs(path) else os.path.join(base_dir, path)
                            
                            is_duplicate_write = False
                            if os.path.exists(full_path):
                                try:
                                    with open(full_path, 'r', encoding='utf-8-sig') as f: # Handle BOM
                                        existing_content = f.read()
                                    # Compare stripped content to ignore whitespace differences
                                    if content.strip() == existing_content.strip():
                                        is_duplicate_write = True
                                except:
                                    pass
                            
                            if is_duplicate_write:
                                tui.append_stream(f"[INFO] Skipping duplicate write for {path} (content identical)")
                                
                                # CRITICAL: Track file in current context state
                                current_state.record_file_created(full_path)
                                current_state.record_tool_call("write_file")

                                # Legacy tracking
                                current_task_idx = task_mgr.current_task_idx
                                if current_task_idx not in task_file_map:
                                    task_file_map[current_task_idx] = []
                                if full_path not in task_file_map[current_task_idx]:
                                    task_file_map[current_task_idx].append(full_path)

                                if full_path not in files_created:
                                    files_created.append(full_path)
                                
                                # Add a fake success message to history
                                history.append({
                                    "role": "tool", 
                                    "tool_call_id": tc['id'],
                                    "name": "write_file",
                                    "content": f"✓ File {path} already exists with identical content. No changes made. You can proceed to the next task."
                                })
                                # Update context state
                                current_state.history = history
                                context_states[current_state.phase] = current_state
                                continue # Skip to next tool
                            else:
                                fn_args = extracted_args
                                tui.append_stream(f"[INFO] Regex extracted args for write_file")
                    
                    # 2. Handle single-argument tools (web_search, etc.)
                    elif fn_name in ["web_fetch", "web_deep_search", "web_search", "bash", "python_sandbox"]:
                        arg_map = {
                            "web_fetch": "url",
                            "web_deep_search": "query",
                            "web_search": "query", # Map alias
                            "bash": "command",
                            "python_sandbox": "code"
                        }
                        main_arg = arg_map.get(fn_name)
                        # Extract "arg": "value..."
                        match = re.search(rf'"{main_arg}"\s*:\s*"(.*)', fn_args_str, re.DOTALL)
                        if match:
                            val_raw = match.group(1)
                            end_quote_match = re.search(r'(.*)"\s*[\},]', val_raw, re.DOTALL)
                            if end_quote_match:
                                val_clean = end_quote_match.group(1)
                            else:
                                val_clean = val_raw.rstrip('"} ]')
                                if val_clean.endswith('"'): val_clean = val_clean[:-1]
                            
                            val_clean = val_clean.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
                            fn_args = {main_arg: val_clean}
                            
                            # Handle alias mapping for execution
                            if fn_name == "web_search":
                                fn_name = "web_deep_search" # Remap to actual tool name
                                
                            tui.append_stream(f"[INFO] Regex extracted {main_arg} for {fn_name}")
                    else:
                        fn_args = {}

                # Reset consecutive_task_done counter if a different tool is called
                if fn_name != "task_done" and hasattr(loop, 'consecutive_task_done'):
                    loop.consecutive_task_done = 0

                # Doom loop detection
                action_sig = f"{fn_name}:{json.dumps(fn_args, sort_keys=True)[:80]}"
                loop.record_action(action_sig)
                
                if loop.detect_doom_loop():
                    tui.append_stream("Doom loop detected - trying different approach...")
                    history.append({
                        "role": "system",
                        "content": "⚠️ DOOM LOOP! Try a different approach or move to next task."
                    })
                    loop.recent_actions.clear()
                    continue
                
                # Execute tool
                result = "Error: Tool not found"
                
                # CRITICAL: Prevent recursion (calling self)
                if fn_name == "coding_agent":
                    result = (
                        "⚠️ ERROR: RECURSION DETECTED!\n\n"
                        "You are ALREADY the Coding Agent.\n"
                        "DO NOT call `coding_agent` again.\n"
                        "Use `write_file`, `read_file`, etc. directly to complete the task."
                    )
                    tui.append_stream("Recursion blocked: Agent tried to call itself")
                
                # ALIAS: web_search -> web_deep_search
                if fn_name == "web_search":
                    fn_name = "web_deep_search"
                    tui.append_stream("[INFO] Redirecting web_search -> web_deep_search")
                
                # ===== TODO MANAGEMENT TOOLS =====
                if fn_name == "set_todos":
                    tasks = fn_args.get("tasks", [])

                    # HARD RULE (code-enforced): single-file deliverable -> exactly 1 task.
                    # The system prompt states this, but small models ignore it; the
                    # 3D-game run produced 9 tasks for one HTML file. First violation
                    # is rejected with instructions, the second is auto-coerced.
                    singlefile_rejected = False
                    if (
                        isinstance(tasks, list) and len(tasks) > 1
                        and not current_state.is_task()
                        and _detect_single_file_deliverable(task)
                    ):
                        singlefile_rejections += 1
                        if singlefile_rejections == 1:
                            singlefile_rejected = True
                            result = (
                                f"⚠️ REJECTED: This is a SINGLE-FILE deliverable, but you submitted {len(tasks)} tasks!\n\n"
                                f"**Planning rule:** single-file deliverable -> exactly ONE task.\n"
                                f"Call set_todos again with exactly ONE task that produces the complete file, e.g.:\n"
                                f'set_todos(tasks=["Create the complete deliverable in a single file with all required features"])'
                            )
                            tui.append_stream(f"set_todos rejected - single-file deliverable needs exactly 1 task (got {len(tasks)})")
                            _log_to_file(f"[DEBUG-X] set_todos REJECTED: single-file rule ({len(tasks)} tasks)")
                            try:
                                if lg:
                                    lg.event("set_todos_singlefile_rejected", tasks_count=len(tasks))
                            except Exception:
                                pass
                        else:
                            coerced = f"Create the complete single-file deliverable: {task.strip()}"[:240]
                            tasks = [coerced]
                            tui.append_stream("Single-file rule: auto-coerced plan to exactly 1 task")
                            _log_to_file("[DEBUG-X] set_todos auto-coerced to 1 task (single-file rule)")
                            try:
                                if lg:
                                    lg.event("set_todos_singlefile_coerced", coerced_task=coerced)
                            except Exception:
                                pass

                    # Check current phase - CONTEXT-AWARE
                    if singlefile_rejected:
                        pass  # rejection result already set above
                    elif current_state.is_task():
                        # BLOCK: Cannot change todos during task execution
                        current_task = task_mgr.get_current_task()
                        result = (
                            f"⚠️ REJECTED: Cannot modify TODO list during task execution!\n\n"
                            f"You are currently working on Task {current_state.task_idx + 1}: {current_task}\n\n"
                            f"**Options:**\n"
                            f"1. Complete current task with task_done\n"
                            f"2. Continue working on the current task\n\n"
                            f"TODO list can only be modified in planning phase (Main Context)."
                        )
                        tui.append_stream("set_todos rejected - in task execution phase!")
                        _log_to_file(f"[DEBUG-X] set_todos REJECTED: Task execution phase")

                    elif task_mgr.todos and len(task_mgr.todos) > 0:
                        # ALLOW: Re-planning in Main Context (e.g., after all tasks completed)
                        if task_mgr.is_all_done():
                            # All tasks done, allow new plan
                            old_count = len(task_mgr.todos)
                            task_mgr.todos.clear()  # Clear old todos
                            task_mgr.current_task_idx = 0
                            task_mgr.phase = "planning"

                            task_mgr.set_todos(tasks)
                            result = (
                                f"✅ TODO list updated (previous plan completed)\n"
                                f"Old: {old_count} tasks → New: {len(tasks)} tasks\n\n"
                                f"Current task: {task_mgr.get_current_task()}"
                            )
                            tui.append_stream(f"Re-planning: {len(tasks)} new tasks")

                            # Switch to first task
                            if tasks and switch_to_task_context(0, tasks[0]):
                                tui.append_stream(f"🔄 Switched to Task 1: {tasks[0][:40]}")
                        else:
                            # Tasks not done yet, reject
                            remaining = len([t for t in task_mgr.todos if t["status"] != "completed"])
                            result = (
                                f"⚠️ REJECTED: Cannot modify TODO list with {remaining} tasks remaining!\n\n"
                                f"Complete all tasks first, then you can create a new plan."
                            )
                            tui.append_stream(f"set_todos rejected - {remaining} tasks remaining!")
                            _log_to_file(f"[DEBUG-X] set_todos REJECTED: {remaining} tasks remaining")

                    elif tasks:
                        # First time setting todos
                        task_mgr.set_todos(tasks)

                        # Logging
                        try:
                            if lg:
                                lg.event(
                                    "todos_set",
                                    source="model_tool_call",
                                    tasks_count=len(tasks),
                                    tasks_preview=[str(t)[:120] for t in (tasks[:5] if isinstance(tasks, list) else [])],
                                    current_task_idx=getattr(task_mgr, "current_task_idx", None),
                                    current_task_preview=(str(task_mgr.get_current_task() or "")[:180]),
                                )
                        except Exception:
                            pass

                        tui.set_action(f"TODO: {len(tasks)} tasks")
                        tui.append_stream(f"Created TODO list with {len(tasks)} tasks")
                        for i, t in enumerate(tasks[:5], 1):
                            tui.append_stream(f"   {i}. {t[:50]}")
                        if len(tasks) > 5:
                            tui.append_stream(f"   ... and {len(tasks)-5} more")

                        result = f"✅ TODO list set: {len(tasks)} tasks. Current: {task_mgr.get_current_task()}"

                        # CRITICAL: Force immediate TUI update to show TODOs
                        tui._needs_update = True
                        try:
                            live.update(tui.render())
                        except Exception:
                            pass
                        time.sleep(0.1)  # Give animation thread time to render

                        # Switch to task context for first task using new switch function
                        if task_mgr.current_task_idx == 0:
                            first_task = task_mgr.get_current_task()
                            if first_task and switch_to_task_context(0, first_task):
                                tui.append_stream(f"🔄 Switched to Task 1 context: {first_task[:40]}")
                                # Force update after context switch
                                tui._needs_update = True
                                try:
                                    live.update(tui.render())
                                except Exception:
                                    pass
                                time.sleep(0.1)
                    else:
                        result = "⚠️ No tasks provided!"
                        
                elif fn_name == "task_done":
                    # Track consecutive task_done calls for dead loop detection
                    if not hasattr(loop, 'consecutive_task_done'):
                        loop.consecutive_task_done = 0
                    loop.consecutive_task_done += 1

                    # CONTEXT-AWARE: Check if files were created IN THIS TASK CONTEXT
                    if current_state.is_task():
                        has_files = current_state.has_created_files()
                        current_task = task_mgr.get_current_task()

                        # Check if this is a non-file-creation task (verify, test, check, review)
                        task_lower = current_task.lower() if current_task else ""
                        is_non_file_task = any(kw in task_lower for kw in [
                            'verify', 'test', 'check', 'review', 'validate', 'confirm',
                            'ensure', 'make sure', 'überprüfen', 'testen', 'prüfen'
                        ])

                        # TEMPLATE MODE: Extra strict - Check if this is a "Read" task
                        is_read_only_task = any(kw in task_lower for kw in [
                            'read and understand', 'read file', 'analyze', 'review existing',
                            'lesen und verstehen', 'datei lesen'
                        ])

                        # CRITICAL: If current_task is None (out of bounds), skip file checks
                        # This happens when current_task_idx exceeds the number of tasks
                        # In this case, we should allow task_done to proceed to trigger the "all done" logic
                        if current_task is None:
                            # Out of bounds - allow task_done to proceed
                            tui.append_stream("[DEBUG] current_task is None - allowing task_done to check is_all_done()")
                        elif not has_files and write_file_calls_in_session == 0:
                            # No write_file calls at all - BLOCK task_done
                            current = task_mgr.get_current_task()
                            result = (
                                f"🚨 [bold red]NO FILES CREATED IN THIS TASK![/]\n\n"
                                f"Task: '{current}'\n\n"
                                f"This task context has NOT called write_file yet.\n"
                                f"Total files in project: {len(files_created)}\n"
                                f"Files in THIS context: {len(current_state.files_created)}\n\n"
                                f"**Action required:**\n"
                                f"1. Call write_file for THIS task: '{current}'\n"
                                f"2. Then call task_done again\n"
                            )
                            tui.append_stream(f"❌ task_done REJECTED - No files in this context!")
                        elif not has_files and not is_non_file_task and not is_read_only_task:
                            # Files exist globally, but not in this context
                            # AND this is not a verify/test/read-only task
                            current = task_mgr.get_current_task()

                            # TEMPLATE MODE: Extra strict validation
                            if guided_mode and template_files:
                                result = (
                                    f"🚨 [bold red]TASK INCOMPLETE - NO ACTION TAKEN![/]\n\n"
                                    f"Task: '{current}'\n\n"
                                    f"**TEMPLATE MODE VIOLATION:**\n"
                                    f"You have NOT called write_file for this specific task.\n"
                                    f"Total files in project: {len(files_created)}\n"
                                    f"Files created in THIS task: {len(current_state.files_created)}\n\n"
                                    f"**MANDATORY ACTION:**\n"
                                    f"1. Call `write_file` to update/create files for: '{current}'\n"
                                    f"2. Actually implement the changes described in the task\n"
                                    f"3. Then call `task_done` again\n\n"
                                    f"⚠️ In Template Mode, EVERY task must result in file modifications!\n"
                                    f"**Remember your main goal:** {task}"
                                )
                                tui.append_stream(f"❌ task_done BLOCKED - Template Mode requires write_file per task!")
                            else:
                                result = (
                                    f"[bold yellow]WARNING: NO FILES FOR THIS TASK![/]\n\n"
                                    f"Task: '{current}'\n\n"
                                    f"You have created {len(files_created)} files total, but NONE in this task context.\n\n"
                                    f"**Action required:**\n"
                                    f"1. Call write_file to create files for THIS specific task\n"
                                    f"2. Complete the task by actually writing the code\n"
                                    f"3. Then call task_done again\n\n"
                                    f"**Remember your main goal:** {task}"
                                )
                                tui.append_stream(f"⚠️ task_done WARNING - No files for current task!")
                        else:
                            # Files were created in this context - allow task_done
                            # Continue with normal task_done logic below
                            pass
                    else:
                        # Not in task context - this shouldn't happen
                        result = "⚠️ task_done called outside task context!"
                        tui.append_stream("task_done ERROR - not in task context!")

                    # If we passed the checks above, proceed with task_done logic
                    # Only proceed if result is not already set (not blocked above)
                    if 'result' not in locals() or result is None:
                        # CRITICAL: Block premature completion via consecutive task_done calls
                        # Only allow completion if ALL tasks are actually completed
                        if loop.consecutive_task_done >= 3:
                            # Check if all tasks are really done
                            if not task_mgr.is_all_done():
                                remaining = [t["task"] for t in task_mgr.todos if t["status"] != "completed"]
                                result = (
                                    f"🚨 BLOCKED: Cannot complete with {len(remaining)} tasks remaining!\n\n"
                                    f"You called task_done {loop.consecutive_task_done} times, but these tasks are still pending:\n" +
                                    "\n".join(f"- {t}" for t in remaining[:5]) +
                                    f"\n\n**Action required:**\n"
                                    f"1. Complete EACH remaining task by actually doing the work\n"
                                    f"2. Call task_done ONLY after completing a task\n"
                                    f"3. DO NOT call task_done multiple times to skip tasks\n\n"
                                    f"Work on the remaining tasks now!"
                                )
                                tui.append_stream(f"Blocked premature completion - {len(remaining)} tasks remaining!")
                                tui.set_action(f"{task_mgr.get_progress()} - Complete remaining tasks!")
                                # Reset counter to prevent infinite loop
                                loop.consecutive_task_done = 0
                            else:
                                # All tasks are done - allow completion
                                tui.append_stream("All tasks completed!")

                        # ═══════════════════════════════════════════════════════
                        # LINTER CHECK - Prevent task_done with linter errors
                        # ═══════════════════════════════════════════════════════
                        # Check recent history for linter errors
                        has_recent_linter_errors = False
                        for msg in current_state.history[-10:]:  # Check last 10 messages
                            if msg.get("role") == "system" and "❌ LINTER CHECK FAILED" in msg.get("content", ""):
                                has_recent_linter_errors = True
                                break

                        if has_recent_linter_errors:
                            # Block task_done if there are recent linter errors
                            current = task_mgr.get_current_task()
                            result = (
                                f"🚨 [bold red]TASK_DONE BLOCKED - LINTER ERRORS![/]\n\n"
                                f"Task: '{current}'\n\n"
                                f"You have recent linter errors that must be fixed first.\n\n"
                                f"**ACTION REQUIRED:**\n"
                                f"1. Review the linter errors in the conversation above\n"
                                f"2. Fix the code issues\n"
                                f"3. Call write_file again with corrected code\n"
                                f"4. Wait for linter to PASS\n"
                                f"5. Then call task_done\n\n"
                                f"✅ You must see 'LINTER CHECK PASSED' before calling task_done!"
                            )
                            tui.append_stream(f"❌ task_done BLOCKED - Fix linter errors first!")
                        elif 'result' not in locals() or result is None:
                            # Normal task_done processing
                            # CONTENT_ONLY mode: Return actual file content
                            if skip_template and files_created:
                                main_file = None
                                main_file_patterns = ['index.html', 'main.py', 'app.py', 'script.py', 'main.js', 'app.js']
                                for pattern in main_file_patterns:
                                    for f in files_created:
                                        if os.path.basename(f).lower() == pattern.lower():
                                            main_file = f
                                            break
                                    if main_file:
                                        break
                                if not main_file and files_created:
                                    main_file = files_created[0]

                                if main_file:
                                    try:
                                        full_path = main_file if os.path.isabs(main_file) else os.path.join(base_dir, main_file)
                                        if os.path.exists(full_path):
                                            with open(full_path, 'r', encoding='utf-8') as f:
                                                content = f.read()
                                            # Clean up temporary directory
                                            try:
                                                import shutil
                                                shutil.rmtree(base_dir)
                                            except:
                                                pass
                                            return content
                                    except Exception:
                                        pass  # Fallback to summary

                            # No early summary return here: the proactive auto-exit
                            # check at the top of the loop owns run termination
                            # (final retry round, final commit, honest status signal).
                            result = "✅ Acknowledged. Continue with the remaining tasks or finish via task completion."
                            tui.append_stream("task_done acknowledged (legacy path)")
                    
                    # CRITICAL: Cannot call task_done without TODOs!
                    if not task_mgr.todos:
                        result = (
                            "🚨 BLOCKED: task_done requires a TODO list!\n\n"
                            "You MUST call set_todos FIRST. Example:\n"
                            "<tool_call>\n"
                            "set_todos(tasks=[\"Task 1\", \"Task 2\", \"Task 3\"])\n"
                            "</tool_call>\n\n"
                            "STOP calling task_done. Call set_todos NOW."
                        )
                        tui.append_stream("task_done BLOCKED - no TODOs! Call set_todos first!")
                        _log_to_file(f"[DEBUG-X] task_done BLOCKED: No TODOs")
                    else:
                        current = task_mgr.get_current_task()
                        
                        # VALIDATE: Check if current task is actually done
                        # For file-related tasks, check if files were created/modified
                        task_lower = current.lower() if current else ""
                        is_create_task = any(kw in task_lower for kw in ['create', 'generate', 'write', 'build', 'implement', 'make'])
                        is_file_task = is_create_task or any(kw in task_lower for kw in ['edit', 'modify', 'update', 'fix', 'change', 'add', 'file'])
                        
                        # CRITICAL: If task is to create something, but no files were created -> BLOCK
                        # We check if ANY files were created in the whole session (files_created)
                        # Ideally we should check if files were created *during this task*, but checking total is a good baseline safe-guard against "lazy" agents
                        if is_create_task and not files_created:
                            result = (
                                f"🚨 [bold red]CRITICAL ERROR: HALLUCINATION DETECTED![/]\n\n"
                                f"You claimed to complete task: '{current}'\n"
                                f"BUT YOU HAVE NOT CREATED ANY FILES!\n\n"
                                f"**MANDATORY ACTION:**\n"
                                f"1. You MUST call `write_file(path='...', content='...')` with the actual code/content NOW.\n"
                                f"2. DO NOT call `task_done` again until the file physically exists.\n"
                                f"3. Start working on the code for '{current}' immediately."
                            )
                            tui.append_stream(f"❌ task_done REJECTED - No files created for task!")
                            _log_to_file(f"[DEBUG-X] task_done REJECTED: No files created")
                            tui.set_action("⚠️ Waiting for write_file...")
                        
                        elif is_file_task and files_created:
                            # Check if recently created files have placeholders
                            recent_files = files_created[-5:]  # Check last 5 files
                            placeholder_check = QualityChecker.check_placeholders(recent_files, base_dir)
                            
                            # ALSO check if template structure was preserved
                            template_structure_preserved = True
                            missing_template_elements = []
                            if template_files:
                                for recent_file in recent_files:
                                    # Check if this is a template file
                                    matching_template = None
                                    for tf in template_files:
                                        if recent_file == tf or os.path.basename(recent_file) == os.path.basename(tf):
                                            matching_template = tf
                                            break
                                    
                                    if matching_template:
                                        # This is a template file - check structure
                                        try:
                                            with open(recent_file, 'r', encoding='utf-8') as f:
                                                current_content = f.read()
                                            with open(matching_template, 'r', encoding='utf-8') as f:
                                                original_content = f.read()
                                            
                                            # Check key elements
                                            file_name = os.path.basename(recent_file)
                                            if '<nav' in original_content or 'nav-links' in original_content or 'class="nav"' in original_content:
                                                if '<nav' not in current_content and 'nav-links' not in current_content and 'class="nav"' not in current_content:
                                                    template_structure_preserved = False
                                                    missing_template_elements.append(f"{file_name}: Navigation removed")
                                            if 'hero' in original_content or 'id="home"' in original_content or 'class="hero"' in original_content:
                                                if 'hero' not in current_content and 'id="home"' not in current_content and 'class="hero"' not in current_content:
                                                    template_structure_preserved = False
                                                    missing_template_elements.append(f"{file_name}: Hero section removed")
                                            if 'services' in original_content or 'id="services"' in original_content or 'class="services"' in original_content:
                                                if 'services' not in current_content and 'id="services"' not in current_content and 'class="services"' not in current_content:
                                                    template_structure_preserved = False
                                                    missing_template_elements.append(f"{file_name}: Services section removed")
                                            if 'about' in original_content or 'id="about"' in original_content or 'class="about"' in original_content:
                                                if 'about' not in current_content and 'id="about"' not in current_content and 'class="about"' not in current_content:
                                                    template_structure_preserved = False
                                                    missing_template_elements.append(f"{file_name}: About section removed")
                                            if 'contact' in original_content or 'id="contact"' in original_content or 'class="contact"' in original_content:
                                                if 'contact' not in current_content and 'id="contact"' not in current_content and 'class="contact"' not in current_content:
                                                    template_structure_preserved = False
                                                    missing_template_elements.append(f"{file_name}: Contact section removed")
                                            if '<footer' in original_content or 'class="footer"' in original_content:
                                                if '<footer' not in current_content and 'class="footer"' not in current_content:
                                                    template_structure_preserved = False
                                                    missing_template_elements.append(f"{file_name}: Footer removed")
                                        except Exception as e:
                                            # If check fails, assume preserved (graceful degradation)
                                            pass
                            
                            if not template_structure_preserved:
                                # Just log a warning but allow task_done to proceed
                                _missing_str = ", ".join(missing_template_elements)
                                tui.append_stream(f"Note: template structure changed ({_missing_str})")

                            if placeholder_check['has_placeholders']:
                                # Task not really done - placeholders still present
                                placeholder_details = []
                                files_list = []
                                
                                # Detect task type and extract context
                                task_lower = task.lower()
                                task_type = "project"  # generic default
                                task_context = ""
                                
                                # Detect task type
                                task_words = task_lower.split()
                                if any(kw in task_lower for kw in ["website", "webseite", "webpage", "landing"]):
                                    task_type = "website"
                                    # Extract business type for websites
                                    for i, word in enumerate(task_words):
                                        if word in ["für", "for"] and i + 1 < len(task_words):
                                            task_context = task_words[i + 1]
                                            break
                                elif any(kw in task_lower for kw in ["script", "skript", "automation", "automatisierung"]):
                                    task_type = "script"
                                    # Extract purpose for scripts
                                    if "to" in task_words or "zum" in task_words or "zur" in task_words:
                                        idx = next((i for i, w in enumerate(task_words) if w in ["to", "zum", "zur"]), -1)
                                        if idx >= 0 and idx + 1 < len(task_words):
                                            task_context = " ".join(task_words[idx+1:idx+4])
                                elif any(kw in task_lower for kw in ["app", "application", "anwendung", "tool"]):
                                    task_type = "application"
                                elif any(kw in task_lower for kw in ["api", "backend", "server"]):
                                    task_type = "api"
                                
                                for fname, placeholders in placeholder_check['files_with_placeholders'].items():
                                    files_list.append(fname)
                                    placeholder_details.append(f"\n**{fname}**:")
                                    for ph in placeholders[:3]:  # Show up to 3 placeholders per file
                                        placeholder_details.append(f"  - {ph}")
                                
                                # Build specific replacement examples based on what was found AND task type
                                examples = []
                                all_placeholders_str = str(placeholder_check['files_with_placeholders']).lower()
                                
                                # Generic placeholders (all types)
                                if '{{' in all_placeholders_str or '${' in all_placeholders_str:
                                    if task_type == "website":
                                        examples.append(
                                            "Replace {{PLACEHOLDER}} / ${PLACEHOLDER} with:\n"
                                            f"  → Actual content (e.g., {{{{BUSINESS_NAME}}}} → 'Berlin {task_context.title() if task_context else 'Company'}')"
                                        )
                                    elif task_type == "script":
                                        examples.append(
                                            "Replace {{PLACEHOLDER}} / ${PLACEHOLDER} with:\n"
                                            f"  → Actual values (e.g., {{{{API_KEY}}}} → config value, {{{{FILE_PATH}}}} → '/path/to/file')"
                                        )
                                    else:
                                        examples.append(
                                            "Replace {{PLACEHOLDER}} / ${PLACEHOLDER} with:\n"
                                            "  → Actual content matching the variable name and project context"
                                        )
                                
                                # Task-type specific placeholders
                                if task_type == "website":
                                    if 'mein unternehmen' in all_placeholders_str or 'my company' in all_placeholders_str:
                                        context_hint = f" ({task_context})" if task_context else ""
                                        examples.append(
                                            "Replace 'Mein Unternehmen' / 'My Company' with:\n"
                                            f"  → Actual business name{context_hint}"
                                        )
                                    
                                    if 'musterstraße' in all_placeholders_str or '123 main st' in all_placeholders_str:
                                        examples.append(
                                            "Replace 'Musterstraße' / '123 Main St' with:\n"
                                            "  → Realistic address matching the location in task"
                                        )
                                    
                                    if 'service' in all_placeholders_str or 'leistung' in all_placeholders_str:
                                        context_hint = f" for {task_context}" if task_context else ""
                                        examples.append(
                                            "Replace 'Service 1', 'Service 2', etc. with:\n"
                                            f"  → Specific services relevant to the task{context_hint}"
                                        )
                                    
                                    if 'example@example' in all_placeholders_str:
                                        examples.append(
                                            "Replace 'example@example.com' with:\n"
                                            "  → Realistic email matching the business"
                                        )
                                    
                                    if '+49 123' in all_placeholders_str or '123-456' in all_placeholders_str:
                                        examples.append(
                                            "Replace placeholder phone numbers with:\n"
                                            "  → Realistic phone format matching the location"
                                        )
                                
                                elif task_type in ["script", "application", "api"]:
                                    if 'example' in all_placeholders_str or 'sample' in all_placeholders_str:
                                        examples.append(
                                            "Replace 'example' / 'sample' placeholders with:\n"
                                            f"  → Real values matching the {task_type} purpose"
                                        )
                                    
                                    if 'your_' in all_placeholders_str or 'your-' in all_placeholders_str:
                                        examples.append(
                                            "Replace 'your_*' / 'your-*' placeholders with:\n"
                                            f"  → Specific names/values for this {task_type}"
                                        )
                                    
                                    if 'todo' in all_placeholders_str or 'fixme' in all_placeholders_str:
                                        examples.append(
                                            "Replace 'TODO' / 'FIXME' comments with:\n"
                                            "  → Actual implementation or remove them"
                                        )
                                
                                # Universal placeholders
                                if 'lorem ipsum' in all_placeholders_str:
                                    examples.append(
                                        "Replace 'Lorem ipsum' with:\n"
                                        f"  → Real, meaningful text for this {task_type}"
                                    )
                                
                                examples_text = "\n\n".join(examples) if examples else "Replace all placeholder text with real, contextual content matching the task"
                                
                                result = (
                                    f"🚨 TASK NOT COMPLETE - PLACEHOLDERS STILL PRESENT!\n\n"
                                    f"**Current Task:** {current}\n"
                                    f"**Original Request:** {task[:100]}\n"
                                    f"**Project Type:** {task_type.title()}\n\n"
                                    f"**Why blocked:** The files still contain {placeholder_check['total_placeholders']} placeholder(s) "
                                    f"that must be replaced with real content.\n\n"
                                    f"**Placeholders found in:**{''.join(placeholder_details)}\n\n"
                                    f"**What you MUST do now:**\n\n"
                                    f"{examples_text}\n\n"
                                    f"**Step-by-step:**\n"
                                    f"1. Call `read_file(path=\"{files_list[0]}\")` to see the current content\n"
                                    f"2. Identify ALL placeholders listed above\n"
                                    f"3. Create new content with ALL placeholders replaced (use task context: '{task[:80]}')\n"
                                    f"4. Call `write_file(path=\"{files_list[0]}\", content=\"...\")` with the complete, fixed content\n"
                                    f"5. Repeat for other files: {', '.join(files_list[1:]) if len(files_list) > 1 else 'none'}\n"
                                    f"6. ONLY THEN call `task_done(summary=\"...\")`\n\n"
                                    f"**IMPORTANT:** Use the task '{task[:80]}' as context. Replace ALL placeholders at once, "
                                    f"not one by one. Do NOT just change one word - replace EVERYTHING!"
                                )
                                tui.append_stream(f"{current[:40]} - {placeholder_check['total_placeholders']} placeholders!")
                                tui.set_action(f"{task_mgr.get_progress()} - Fix {placeholder_check['total_placeholders']} placeholders!")
                            else:
                                # Task is done - proceed
                                summary = fn_args.get("summary", "done")
                        else:
                            # All tasks are done - allow completion
                            tui.append_stream("All tasks completed!")
                        
                        # Fix: Ensure files_for_current_task is defined
                        files_for_current_task = task_file_map.get(task_mgr.current_task_idx, [])
                        
                        # CONTENT_ONLY mode: Return actual file content
                        if skip_template and files_created:
                            main_file = None
                            main_file_patterns = ['index.html', 'main.py', 'app.py', 'script.py', 'main.js', 'app.js']
                            for pattern in main_file_patterns:
                                for f in files_created:
                                    if os.path.basename(f).lower() == pattern.lower():
                                        main_file = f
                                        break
                                if main_file:
                                    break

                            # CRITICAL FIX: Mark task as completed!
                            # This code runs AFTER the for loop, regardless of whether main_file was found
                            summary = fn_args.get("summary", "done")
                            task_mgr.complete_current_task(summary)
                            # Run linter for files of this task (if supported types)
                            _run_linter_for_files(files_for_current_task or files_created, history, self.local_tools)
                            next_task = task_mgr.get_current_task()

                            tui.append_stream(f"Completed: {current[:40] if current else 'task'}")
                            tui.set_action(f"{task_mgr.get_progress()}")

                            # ═══════════════════════════════════════════════════════════════
                            # TASK CHECKPOINTING (The Glue) - Archive old context
                            # ═══════════════════════════════════════════════════════════════
                            # Before switching, compress/archive the current task's history
                            # This ensures we don't lose the "lessons learned" but free up tokens
                            current_context_manager.compress(history)

                            # ═══════════════════════════════════════════════════════════════
                            # CREATE FRESH CONTEXT FOR NEW TASK - Isolated context per task
                            # ═══════════════════════════════════════════════════════════════
                            if next_task:
                                # Build summary of completed work for context continuity
                                completed_info = "\n".join([f"- {t['task']}: {t.get('result', 'done')}" for t in task_mgr.todos if t['status'] == 'completed'])

                                # Create completely fresh context for the new task
                                # This isolates each task with its own ContextManager and history
                                task_idx = task_mgr.current_task_idx
                                current_context_manager, history = create_fresh_context_for_task(task_idx, next_task, completed_info)
                                history_snapshot_len = len(history)  # Update snapshot for new context
                                tui.append_stream(f"🔄 Fresh context created for Task {task_idx + 1}: {next_task[:40]}")

                            if next_task:
                                result = f"✅ Task completed!\n\n## NEXT TASK:\n{next_task}\n\nFocus only on this task now."
                                tui.append_stream(f"Next: {next_task[:40]}")
                            elif task_mgr.is_all_done():
                                if _maybe_start_final_retry():
                                    continue
                                result = "🎉 ALL TASKS COMPLETED! Verify your work and say 'ALL TASKS COMPLETED'."
                                tui.append_stream("All tasks done!")
                                break  # Exit loop - all tasks completed
                            else:
                                result = "✅ Task completed. Continue with remaining work."
                        else:
                            # Non-file task or no files created yet - allow completion
                            summary = fn_args.get("summary", "done")

                            # CRITICAL FIX: Check if all tasks are done BEFORE trying to complete
                            # This handles the case where current_task_idx is out of bounds
                            if task_mgr.current_task_idx >= len(task_mgr.todos) and task_mgr.is_all_done():
                                if _maybe_start_final_retry():
                                    continue
                                result = "🎉 ALL TASKS COMPLETED! Verify your work and say 'ALL TASKS COMPLETED'."
                                tui.append_stream("🎉 [EARLY-EXIT] All tasks completed!")
                                _log_to_file(f"[DEBUG-X] Early exit: task_idx {task_mgr.current_task_idx} >= {len(task_mgr.todos)}, all done")
                                break  # Exit loop immediately - all tasks completed

                            task_mgr.complete_current_task(summary)
                            _run_linter_for_files(files_for_current_task, history, self.local_tools)
                            next_task = task_mgr.get_current_task()
                            
                            tui.append_stream(f"✅ Completed: {current[:40] if current else 'task'}")
                            tui.set_action(f"📋 {task_mgr.get_progress()}")
                            
                            # ═══════════════════════════════════════════════════════════════
                            # TASK CHECKPOINTING (The Glue) - Archive old context
                            # ═══════════════════════════════════════════════════════════════
                            # Before switching, compress/archive the current task's history
                            current_context_manager.compress(history)
                            
                            # ═══════════════════════════════════════════════════════════════
                            # CREATE FRESH CONTEXT FOR NEW TASK - Isolated context per task
                            # ═══════════════════════════════════════════════════════════════
                            if next_task:
                                # Build summary of completed work for context continuity
                                completed_info = "\n".join([f"- {t['task']}: {t.get('result', 'done')}" for t in task_mgr.todos if t['status'] == 'completed'])
                                
                                # Create completely fresh context for the new task
                                # This isolates each task with its own ContextManager and history
                                task_idx = task_mgr.current_task_idx
                                current_context_manager, history = create_fresh_context_for_task(task_idx, next_task, completed_info)
                                history_snapshot_len = len(history)  # Update snapshot for new context
                                tui.append_stream(f"🔄 Fresh context created for Task {task_idx + 1}: {next_task[:40]}")
                            
                            if next_task:
                                result = f"✅ Task completed!\n\n## NEXT TASK:\n{next_task}\n\nFocus only on this task now."
                                tui.append_stream(f"➡️ Next: {next_task[:40]}")
                            elif task_mgr.is_all_done():
                                if _maybe_start_final_retry():
                                    continue
                                result = "🎉 ALL TASKS COMPLETED! Verify your work and say 'ALL TASKS COMPLETED'."
                                tui.append_stream("🎉 All tasks done!")
                                break  # Exit loop - all tasks completed
                            else:
                                result = "✅ Task completed. Continue with remaining work."
                
                elif fn_name == "web_fetch":
                    url = fn_args.get("url", "")
                    selector = fn_args.get("selector", "")
                    tui.set_action(f"🌐 Fetching: {url[:30]}...")
                    live.update(tui.render())
                    
                    try:
                        headers = {"User-Agent": "Mozilla/5.0 VAF-Coder/1.0"}
                        resp = requests.get(url, headers=headers, timeout=10)
                        resp.raise_for_status()
                        html = resp.text
                        
                        # If selector provided, try to extract
                        if selector:
                            try:
                                from bs4 import BeautifulSoup
                                soup = BeautifulSoup(html, 'html.parser')
                                elements = soup.select(selector)
                                if elements:
                                    html = "\n".join(str(e) for e in elements[:5])
                                else:
                                    html = f"No elements found for selector: {selector}"
                            except ImportError:
                                html = html[:3000] + "..." if len(html) > 3000 else html
                        else:
                            html = html[:3000] + "..." if len(html) > 3000 else html
                        
                        result = f"Fetched {len(html)} chars from {url}\n\n{html}"
                        tui.append_stream(f"Fetched {url[:30]}")
                    except Exception as e:
                        result = f"Error fetching {url}: {e}"
                        tui.append_stream(f"❌ Fetch failed: {str(e)[:30]}")
                        _log_to_file(f"[DEBUG-X] web_fetch ERROR: {str(e)[:100]}")
                
                elif fn_name == "web_deep_search":
                    query = fn_args.get("query", "")
                    max_results = min(fn_args.get("max_results", 5), 10)  # Max 10 results
                    tui.set_action(f"🔍 Deep search: {query[:40]}...")
                    live.update(tui.render())

                    # DEBUG: Log that we're executing web_deep_search
                    _log_to_file(f"[DEBUG-X] web_deep_search EXEC START: query='{query[:50]}'")

                    try:
                        # Use VAF's direct DuckDuckGo search (no third-party package)
                        from vaf.tools.search import _search_duckduckgo
                        results = _search_duckduckgo(query, max_results)
                        if not results:
                            result = f"No results found for: {query}"
                            tui.append_stream("No results")

                        # Only build summary if we have results
                        if results:
                            # Helper to fetch and summarize page content (context-aware, limited)
                            def fetch_summary(url):
                                try:
                                    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                                    r = requests.get(url, timeout=5, headers=headers)
                                    if r.status_code != 200:
                                        return None

                                    html = r.text
                                    # Remove scripts, styles
                                    html = re.sub(r'<(script|style|noscript)[^>]*>.*?</\1>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
                                    # Strip tags
                                    text = re.sub(r'<[^>]+>', ' ', html)
                                    # Clean whitespace
                                    text = re.sub(r'\s+', ' ', text).strip()

                                    # Limit to 1500 chars per result to keep context small
                                    return text[:1500]
                                except:
                                    return None

                            # Build summarized results
                            summary = f"### Deep Search Results: {query}\n\n"
                            for i, res in enumerate(results[:max_results], 1):
                                title = res.get('title', 'No title')
                                link = res.get('href', '')
                                snippet = res.get('body', '')

                                # Fetch full content for top 3 results only (to keep context small)
                                content = ""
                                if i <= 3:
                                    tui.set_action(f"📖 Reading {link[:30]}...")
                                    live.update(tui.render())
                                    page_text = fetch_summary(link)
                                    if page_text:
                                        content = f"\n  [Content]: {page_text}..."

                                summary += f"{i}. **{title}**\n   {snippet}\n   {link}{content}\n\n"

                            result = summary
                            tui.append_stream(f"Found {len(results)} results")
                            _log_to_file(f"[DEBUG-X] web_deep_search SUCCESS: {len(results)} results, result_len={len(result)}")
                    except Exception as e:
                        result = f"Error during deep search: {e}"
                        tui.append_stream(f"❌ Search failed: {str(e)[:30]}")
                        _log_to_file(f"[DEBUG-X] web_deep_search ERROR: {str(e)[:100]}")

                    # DEBUG: Log result status
                    result_preview = result[:100] if 'result' in locals() and result else "NO RESULT SET"
                    _log_to_file(f"[DEBUG-X] web_deep_search END: result_preview='{result_preview}'")

                    # CRITICAL: Add action nudge after web_search to prevent "thinking loops"
                    # The agent has the search results now and MUST use them appropriately
                    if 'result' in locals() and result:
                        if current_state.is_task():
                            # In task execution mode, web_search must lead to write_file
                            result += (
                                "\n\n---\n"
                                "✅ **Search completed!** You now have the information you need.\n\n"
                                "**NEXT ACTION REQUIRED:**\n"
                                f"1. Use the search results above to complete your current task: '{task_mgr.get_current_task()}'\n"
                                "2. Call `write_file(path='...', content='...')` with the actual code NOW\n"
                                "3. DO NOT just think or plan - write the actual file immediately\n\n"
                                "**Example:**\n"
                                "```\n"
                                "write_file(path='index.html', content='<!DOCTYPE html>...')\n"
                                "```\n\n"
                                "Start writing the file NOW using the information from the search results!"
                            )
                        elif current_state.is_main():
                            # In planning mode, web_search must lead to set_todos
                            result += (
                                "\n\n---\n"
                                "✅ **Research completed!** You now have the information you need.\n\n"
                                "**NEXT ACTION REQUIRED:**\n"
                                "1. Use the search results above to plan your task breakdown\n"
                                "2. Call `set_todos(tasks=[...])` NOW with your task list\n"
                                "3. DO NOT do more searches - proceed with planning\n\n"
                                "**Example:**\n"
                                "```\n"
                                "set_todos(tasks=['Create index.html', 'Create styles.css', 'Add JavaScript'])\n"
                                "```\n\n"
                                "Call set_todos NOW based on the search results!"
                            )
                
                elif fn_name in self.local_tools:
                    tool = self.local_tools[fn_name]
                    
                    # Fix relative paths and show in stream
                    if fn_name == "write_file":
                        is_template_file = False  # Initialize to prevent UnboundLocalError
                        if "path" not in fn_args:
                            msg = f"Error: Missing 'path' argument"
                            if 'json_error' in locals() and json_error:
                                msg += f". JSON was malformed/truncated: {json_error}"
                            
                            tui.append_stream(f"[ERROR] {msg}")
                            result = msg
                            # Continue to let result be added to history
                        else:
                            path = fn_args["path"]
                            fname = os.path.basename(path)
                            
                            # CRITICAL: Add file to display IMMEDIATELY, before any validation or execution
                            # This ensures it appears in "Waiting for files..." section right away
                            if fname not in tui.files:
                                tui.add_file(fname, 0, "writing")
                                tui._needs_update = True  # Force immediate update
                                # Force immediate render to show file
                                try:
                                    live.update(tui.render())
                                except Exception:
                                    pass  # Don't fail if render is blocked
                            
                            # GUARD: Block writing of meta/planning files that pollute the project
                            _path_arg = fn_args.get("path", "")
                            _basename_lower = os.path.basename(_path_arg).lower()
                            _META_FILE_PATTERNS = {"plan.md", "structure.md", "structure_plan.md", "notes.md", "todo.md", "design.md", "layout.md", "readme.md", "read_chunks.py"}
                            if _basename_lower in _META_FILE_PATTERNS:
                                result = (
                                    f"⛔ BLOCKED: Writing '{os.path.basename(_path_arg)}' is not allowed.\n\n"
                                    "Meta files (PLAN.md, STRUCTURE.md, NOTES.md, read_chunks.py, etc.) must NOT be written to the project directory.\n"
                                    "Planning is mental — do it in your head. Only write actual deliverable files (index.html, styles.css, app.py, etc.).\n\n"
                                    "Call write_file with an actual output file instead."
                                )
                                tui.append_stream(f"[GUARD] write_file blocked: {_basename_lower} is a meta file")
                                history.append({"role": "tool", "tool_call_id": tc['id'], "name": fn_name, "content": result})
                                continue

                            # CRITICAL: Must set TODOs before writing files!
                            if not task_mgr.todos:
                                result = (
                                    "⚠️ ERROR: You must call `set_todos` FIRST before writing files!\n\n"
                                    "REQUIRED WORKFLOW:\n"
                                    "1. Call `set_todos` with your task breakdown\n"
                                    "2. Call `read_file` to read template files\n"
                                    "3. THEN call `write_file` to create/modify files\n\n"
                                    "Call `set_todos` NOW with your task list."
                                )
                                tui.append_stream("write_file rejected - call set_todos FIRST!")
                                history.append({
                                    "role": "tool",
                                    "tool_call_id": tc['id'],
                                    "name": fn_name,
                                    "content": result
                                })
                                live.update(tui.render())
                                continue  # Skip this tool call
                            
                            # OS-independent path handling using Path
                            from pathlib import Path as PathLib
                            path_obj = PathLib(fn_args["path"])
                            if not path_obj.is_absolute():
                                # Use Path.joinpath for OS-independent path joining
                                fn_args["path"] = str(PathLib(base_dir) / fn_args["path"])
                            
                            fname = os.path.basename(fn_args["path"])
                            path = fn_args["path"]
                            
                            # Check if this is a template file being overwritten
                            is_template_file = any(path == tf or os.path.basename(path) == os.path.basename(tf) for tf in template_files)
                            
                            # Generate Diff or Content Preview
                            preview_content = ""
                            preview_lang = "python" # Default
                            
                            if os.path.exists(path):
                                try:
                                    with open(path, 'r', encoding='utf-8') as f:
                                        old_content = f.readlines()
                                    new_lines = fn_args.get("content", "").splitlines(keepends=True)
                                    
                                    import difflib
                                    diff = list(difflib.unified_diff(
                                        old_content, 
                                        new_lines, 
                                        fromfile=f"a/{fname}", 
                                        tofile=f"b/{fname}",
                                        n=3 # Context lines
                                    ))
                                    
                                    if diff:
                                        preview_content = "".join(diff)
                                        preview_lang = "diff"
                                    else:
                                        preview_content = fn_args.get("content", "") # No changes
                                except:
                                    preview_content = fn_args.get("content", "")
                            else:
                                # New file
                                preview_content = fn_args.get("content", "")
                                ext = os.path.splitext(fname)[1].lower()
                                if ext in ['.js', '.ts']: preview_lang = "javascript"
                                elif ext == '.html': preview_lang = "html"
                                elif ext == '.css': preview_lang = "css"
                            
                            # Update Code Preview Panel with Diff or Content
                            tui.set_code_preview(fname, preview_content, preview_lang)

                        if is_template_file:
                            # CRITICAL: Validate that template structure is preserved
                            try:
                                # Read the original template file
                                original_content = ""
                                template_path = None
                                for tf in template_files:
                                    if path == tf or os.path.basename(path) == os.path.basename(tf):
                                        template_path = tf
                                        with open(tf, 'r', encoding='utf-8') as f:
                                            original_content = f.read()
                                        break
                                
                                if original_content:
                                    # Check if key template elements are preserved
                                    new_content = fn_args.get("content", "")
                                    
                                    # Extract key structural elements from template
                                    template_has_nav = '<nav' in original_content or 'nav-links' in original_content or 'class="nav"' in original_content
                                    template_has_hero = 'hero' in original_content or 'id="home"' in original_content or 'class="hero"' in original_content
                                    template_has_services = 'services' in original_content or 'id="services"' in original_content or 'class="services"' in original_content
                                    template_has_about = 'about' in original_content or 'id="about"' in original_content or 'class="about"' in original_content
                                    template_has_contact = 'contact' in original_content or 'id="contact"' in original_content or 'class="contact"' in original_content
                                    template_has_footer = '<footer' in original_content or 'class="footer"' in original_content
                                    
                                    # Check if new content preserves these elements
                                    missing_elements = []
                                    if template_has_nav and ('<nav' not in new_content and 'nav-links' not in new_content and 'class="nav"' not in new_content):
                                        missing_elements.append("Navigation (nav/nav-links)")
                                    if template_has_hero and ('hero' not in new_content and 'id="home"' not in new_content and 'class="hero"' not in new_content):
                                        missing_elements.append("Hero section (id='home' or class='hero')")
                                    if template_has_services and ('services' not in new_content and 'id="services"' not in new_content and 'class="services"' not in new_content):
                                        missing_elements.append("Services section (id='services')")
                                    if template_has_about and ('about' not in new_content and 'id="about"' not in new_content and 'class="about"' not in new_content):
                                        missing_elements.append("About section (id='about')")
                                    if template_has_contact and ('contact' not in new_content and 'id="contact"' not in new_content and 'class="contact"' not in new_content):
                                        missing_elements.append("Contact section (id='contact')")
                                    if template_has_footer and '<footer' not in new_content and 'class="footer"' not in new_content:
                                        missing_elements.append("Footer")
                                    
                                    if missing_elements:
                                        # Log info but allow the write (template is guidance, not a rule)
                                        _miss_str = ", ".join(missing_elements)
                                        tui.append_stream(f"Note: template sections changed in {fname}: {_miss_str}")
                                    else:
                                        # Structure matches template - note it
                                        tui.append_stream(f"Template structure preserved in {fname}")
                            except Exception as e:
                                # If validation fails, still warn but allow (graceful degradation)
                                tui.append_stream(f"Could not validate template: {e}")
                            
                            # Note that template file is being overwritten
                            tui.append_stream(f"Updating template file {fname}")
                            live.update(tui.render())
                        
                        tui.add_file(fname, 0, "writing")
                        tui.set_action(f"📝 Writing: {fname}")

                        # Update Code Preview Panel
                        tui.set_code_preview(fname, fn_args.get("content", ""), "code")
                        # WebUI live editor: final full content for this file
                        _emit_live_code(fname, fn_args.get("content", ""), force=True)

                        # Log start of writing
                        if 'append_with_context' in locals():
                            append_with_context(f"Writing {fname}...")
                        else:
                            tui.append_stream(f"Writing {fname}...")
                        
                        live.update(tui.render())
                    elif fn_name == "read_file":
                        tui.set_action(f"📖 Reading...")
                        live.update(tui.render())
                    elif fn_name == "bash":
                        tui.set_action(f"⚡ {fn_args.get('command', '')[:25]}")
                        live.update(tui.render())
                    elif fn_name.startswith("git_"):
                        # Git tools
                        if fn_name == "git_init":
                            tui.set_action("🔧 Initializing Git...")
                        elif fn_name == "git_add_commit":
                            msg = fn_args.get('message', '')[:30]
                            tui.set_action(f"🔧 Committing: {msg}...")
                        elif fn_name == "git_status":
                            tui.set_action("🔧 Checking Git status...")
                        elif fn_name == "git_log":
                            tui.set_action("🔧 Viewing Git log...")
                        live.update(tui.render())
                    elif fn_name == "project_history":
                        tui.set_action("🔧 Reading version history...")
                        live.update(tui.render())
                    elif fn_name == "project_rollback":
                        tui.set_action(f"🔧 Rollback to {fn_args.get('commit', '')[:10]}...")
                        live.update(tui.render())
                    
                    try:
                        # GUARD: Block python_sandbox from reading/writing project files.
                        # This prevents the model from using sandbox as a backdoor to modify
                        # project files instead of using write_file / read_file.
                        # Only triggers on actual file-write patterns — not stdout.write() or in-memory ops.
                        if fn_name == "python_sandbox":
                            code = fn_args.get("code", "")
                            _sandbox_writes_files = (
                                # open() in write/append mode
                                (bool(re.search(r'\bopen\s*\(', code)) and ("'w'" in code or '"w"' in code or "'a'" in code or '"a"' in code))
                                # .write( on a file handle variable (not stdout/stderr/StringIO)
                                or bool(re.search(r'\b(?!stdout|stderr|StringIO)\w+\.write\s*\(', code))
                                # Direct reference to the project base_dir path
                                or base_dir in code
                            )
                            if _sandbox_writes_files:
                                result = (
                                    "⛔ BLOCKED: python_sandbox cannot write to project files.\n\n"
                                    "Use write_file(path='...', content='...') instead to write or update project files.\n"
                                    "Use read_file(path='...') to read project files.\n"
                                    "python_sandbox is only for pure computation (math, algorithms, string processing)."
                                )
                                history.append({"role": "tool", "tool_call_id": tc['id'], "content": result})
                                _log_to_file(f"[GUARD] python_sandbox blocked (project file I/O detected)")
                                tui.append_stream("[GUARD] python_sandbox blocked — use write_file/read_file for project files")
                                continue

                        # Structured per-subagent debug logging (action + reaction)
                        t0 = time.time()
                        try:
                            if lg:
                                from vaf.core.subagent_debug import sanitize_args
                                lg.event("tool_start", tool=fn_name, args=sanitize_args(fn_name, fn_args))
                        except Exception:
                            pass

                        result = tool.run(**fn_args)

                        try:
                            if lg:
                                from vaf.core.subagent_debug import summarize_result
                                lg.event(
                                    "tool_end",
                                    tool=fn_name,
                                    duration_ms=int((time.time() - t0) * 1000),
                                    ok=True,
                                    **summarize_result(result),
                                )
                        except Exception:
                            pass

                        tui.append_stream(f"[DEBUG] Tool {fn_name} result: {str(result)[:100]}...")
                        result_str = str(result)
                        
                        # Check if result indicates an error (even if no exception was raised)
                        is_error_result = (
                            result_str.startswith("❌") or 
                            result_str.startswith("Error:") or
                            "permission denied" in result_str.lower() or
                            "locked" in result_str.lower() or
                            "file write error" in result_str.lower() or
                            "cannot write" in result_str.lower()
                        )
                        
                        # Track created files (after execution)
                        if fn_name == "write_file" and "path" in fn_args:
                            path = fn_args["path"]
                            
                            if is_error_result:
                                # Error occurred - track it
                                tui.update_file(os.path.basename(path), "error")
                                tui.append_stream(f"❌ Error: {result_str[:80]}")
                                
                                # Track repeated errors
                                error_key = f"{fn_name}:{path}:{result_str[:100]}"
                                loop.error_history.append(error_key)
                                
                                # If same error 3 times in a row, stop and report
                                if len(loop.error_history) >= 3:
                                    last_3 = loop.error_history[-3:]
                                    if len(set(last_3)) == 1:  # All same error
                                        return (
                                            f"### ❌ Repeated Error Detected\n\n"
                                            f"The same error occurred 3 times in a row:\n"
                                            f"**File**: {path}\n"
                                            f"**Error**: {result_str}\n\n"
                                            f"**Possible causes:**\n"
                                            f"- File is locked by another program (editor, browser, etc.)\n"
                                            f"- Insufficient permissions\n"
                                            f"- Disk space issues\n\n"
                                            f"**Solution:**\n"
                                            f"1. Close any programs that might have the file open\n"
                                            f"2. Check file permissions\n"
                                            f"3. Try a different file path\n\n"
                                            f"Stopped to prevent infinite loop."
                                        )
                            elif os.path.exists(path):
                                # Success - clear error history
                                loop.error_history = []
                                size = os.path.getsize(path)

                                # CONTEXT STATE TRACKING
                                current_state.record_file_created(path)
                                current_state.record_tool_call("write_file")

                                # Legacy tracking
                                files_created.append(path)

                                # Track write_file call for current task (legacy)
                                current_task_idx = task_mgr.current_task_idx
                                if current_task_idx not in task_file_map:
                                    task_file_map[current_task_idx] = []
                                task_file_map[current_task_idx].append(path)

                                # Persist file into task's files_created list so tasks.json reflects it
                                if task_mgr.state and current_task_idx < len(task_mgr.state.tasks):
                                    _t = task_mgr.state.tasks[current_task_idx]
                                    if path not in _t.files_created:
                                        _t.files_created.append(path)
                                    if task_mgr.pm:
                                        try:
                                            task_mgr.pm.save_state(task_mgr.state)
                                        except Exception:
                                            pass

                                # Track write_file calls in session
                                write_file_calls_in_session += 1
                                recent_loop_write_files.append(True)  # Track this loop had write_file

                                # WebUI: refresh explorer with this file marked as writing
                                _emit_coder_state(current_file=path)
                                
                                tui.update_file(os.path.basename(path), "done", size)
                                
                                # Use append_with_context if available
                                size_str = f"{size:,}B" if size < 1024 else f"{size/1024:.1f} KB"
                                file_msg = f"] {os.path.basename(path)} ({size_str}) done"
                                
                                # FULL PATH VISIBILITY
                                full_path_msg = f"[SAVED] 💾 {path}"
                                _trace(f"💾 FILE SAVED: {path}") 
                                
                                if 'append_with_context' in locals():
                                    append_with_context(file_msg)
                                    append_with_context(full_path_msg)
                                else:
                                    tui.append_stream(file_msg)
                                    tui.append_stream(full_path_msg)
                                
                                # Show first 10 lines of saved content
                                code_content = fn_args.get("content", "")
                                code_lines = code_content.split('\n')[:10]
                                for line in code_lines:
                                    if 'append_with_context' in locals():
                                        append_with_context(f"  {line[:65]}")
                                    else:
                                        tui.append_stream(f"  {line[:65]}")
                                if len(code_content.split('\n')) > 10:
                                    if 'append_with_context' in locals():
                                        append_with_context("  ...")
                                    else:
                                        tui.append_stream("  ...")
                                
                                result = f"✓ Created {path} ({size} bytes)"

                                # CONTEXT BLOAT FIX: Strip 'content' from this tool call in history.
                                # write_file arguments can be 30KB+. After 3-4 writes the history
                                # blows past the API context limit (400 errors). Replace the content
                                # field with a short placeholder so the model still knows what was
                                # written but doesn't re-read 34KB of HTML on every loop.
                                # NOTE: When the LLM args contain HTML special chars, json.loads()
                                # of the raw arguments string fails.  In that case we fall back to
                                # fn_args (already extracted via regex) to know the content length.
                                _tc_id = tc.get('id', '')
                                _strip_orig_len = len(fn_args.get('content', '')) if fn_args else 0
                                for _hmsg in reversed(history):
                                    if _hmsg.get('role') == 'assistant' and _hmsg.get('tool_calls'):
                                        for _htc in _hmsg['tool_calls']:
                                            if _htc.get('id') == _tc_id:
                                                try:
                                                    _raw_args = _htc.get('function', {}).get('arguments', '{}')
                                                    try:
                                                        _parsed = json.loads(_raw_args)
                                                        if 'content' in _parsed:
                                                            _strip_orig_len = len(_parsed['content'])
                                                            _parsed['content'] = f'[content omitted — {_strip_orig_len} bytes written to disk]'
                                                            _htc['function']['arguments'] = json.dumps(_parsed, ensure_ascii=False)
                                                    except json.JSONDecodeError:
                                                        # Raw args are malformed (unescaped chars in HTML content).
                                                        # Replace the entire arguments string with a clean placeholder.
                                                        _placeholder = {
                                                            'path': fn_args.get('path', ''),
                                                            'content': f'[content omitted — {_strip_orig_len} bytes written to disk]'
                                                        }
                                                        _htc['function']['arguments'] = json.dumps(_placeholder, ensure_ascii=False)
                                                except Exception:
                                                    pass
                                                break
                                        break

                                # PARTIAL CLEANUP: Remove PARTIAL_{basename} if a full file was written.
                                # The PARTIAL_ file is a temporary save from a truncated stream
                                # response. Once the real file exists it's just confusing clutter.
                                _partial_path = os.path.join(
                                    os.path.dirname(path),
                                    f"PARTIAL_{os.path.basename(path)}"
                                )
                                if os.path.exists(_partial_path):
                                    try:
                                        os.remove(_partial_path)
                                        tui.append_stream(f"[Coder] Removed stale {os.path.basename(_partial_path)}")
                                    except Exception:
                                        pass

                                # TRUNCATION DETECTION: Check if HTML/CSS/JS ends with proper closing tag.
                                # When max_tokens is hit mid-content the model stops streaming and the
                                # written file is structurally incomplete (e.g. only CSS, no body).
                                _trunc_ext = os.path.splitext(path)[1].lower()
                                if _trunc_ext in ('.html', '.htm', '.css', '.js'):
                                    try:
                                        _fsize = os.path.getsize(path)
                                        with open(path, 'rb') as _tf:
                                            _tf.seek(max(0, _fsize - 300))
                                            _tail = _tf.read().decode('utf-8', errors='ignore').strip()
                                        _is_trunc = False
                                        if _trunc_ext in ('.html', '.htm'):
                                            _is_trunc = '</html>' not in _tail.lower()
                                        elif _trunc_ext == '.css':
                                            _is_trunc = not _tail.endswith('}')
                                        elif _trunc_ext == '.js':
                                            _is_trunc = not (_tail.endswith('}') or _tail.endswith(';'))
                                        if _is_trunc:
                                            _fname_trunc = os.path.basename(path)
                                            _ext_trunc = _trunc_ext
                                            tui.append_stream(f"⚠️ [Coder] Truncated file detected: {_fname_trunc} — forcing completion")
                                            if _ext_trunc in ('.html', '.htm'):
                                                _trunc_fix = (
                                                    f"The HTML file is cut off — you MUST finish it: write the missing body sections "
                                                    f"and close with `</body></html>`. Do NOT rewrite other files."
                                                )
                                            elif _ext_trunc == '.css':
                                                _trunc_fix = (
                                                    f"The CSS file is cut off — you MUST finish it: write the remaining rules and "
                                                    f"close all unclosed `{{}}` blocks. Do NOT write any other file."
                                                )
                                            else:
                                                _trunc_fix = (
                                                    f"The JS file is cut off — you MUST finish it: complete all unclosed functions "
                                                    f"and add the final `}}` or `;`. Do NOT write any other file."
                                                )
                                            _post_tool_messages.append({
                                                "role": "system",
                                                "content": (
                                                    f"⚠️ CRITICAL: `{_fname_trunc}` is TRUNCATED (output was cut off). "
                                                    f"Current task: complete THIS file only.\n\n"
                                                    f"{_trunc_fix}\n\n"
                                                    f"Use `read_file` to see where it ends, then `write_file` with the full corrected content."
                                                )
                                            })
                                    except Exception:
                                        pass

                                # Notify Web UI so it shows a blue download chip.
                                try:
                                    _fc_sid = os.environ.get("VAF_SESSION_ID")
                                    if not _fc_sid:
                                        from vaf.core.subagent_ipc import get_current_session_id
                                        _fc_sid = get_current_session_id()
                                    if _fc_sid:
                                        from vaf.core.web_interface import notify_file_created
                                        notify_file_created(_fc_sid, path, title=os.path.basename(path))
                                except Exception:
                                    pass

                                # Nudge after write — queued to go AFTER the tool result
                                # so the sequence is: assistant+TC → tool_result → system(nudge).
                                _post_tool_messages.append({
                                    "role": "system",
                                    "content": (
                                        f"✅ File `{os.path.basename(path)}` successfully written.\n\n"
                                        f"**DECISION TIME:**\n"
                                        f"1. Is this task complete? -> Call `task_done(summary='...')` NOW.\n"
                                        f"2. Is more code needed? -> Call `write_file` again.\n\n"
                                        f"Do NOT just think about it. ACT."
                                    )
                                })
                                # Reset idle loop counter on write
                                if hasattr(loop, 'idle_loop_count'):
                                    loop.idle_loop_count = 0
                                
                                # ═══════════════════════════════════════════════════════
                                # AUTOMATIC LINTER CHECK - Prevent zombie task_done
                                # ═══════════════════════════════════════════════════════
                                try:
                                    linter_tool = self.local_tools.get("linter")
                                    if linter_tool:
                                        tui.set_action("🔍 Linting...")
                                        live.update(tui.render())
                                        lint_result = linter_tool.run(path=path)

                                        # Clear PASS/FAIL classification
                                        has_errors = lint_result and not lint_result.startswith("✓") and not lint_result.startswith("[INFO]")

                                        if has_errors:
                                            # ❌ FAIL: Linter found issues
                                            lint_msg = (
                                                f"╔═══════════════════════════════════════════════════════╗\n"
                                                f"║  ❌ LINTER CHECK FAILED                                ║\n"
                                                f"╚═══════════════════════════════════════════════════════╝\n\n"
                                                f"File: {os.path.basename(path)}\n"
                                                f"Status: FAIL - Code has linter errors\n\n"
                                                f"**Errors found:**\n{lint_result}\n\n"
                                                f"**ACTION REQUIRED:**\n"
                                                f"1. Fix the linter errors listed above\n"
                                                f"2. Call write_file again with corrected code\n"
                                                f"3. Do NOT call task_done until linter passes\n\n"
                                                f"🚨 Files with linter errors will cause issues!"
                                            )
                                            _post_tool_messages.append({
                                                "role": "system",
                                                "content": lint_msg
                                            })
                                            tui.append_stream(f"❌ LINTER FAIL: {os.path.basename(path)} has errors!")
                                        else:
                                            # ✅ PASS: No linter errors
                                            lint_msg = (
                                                f"✅ LINTER CHECK PASSED\n"
                                                f"File: {os.path.basename(path)}\n"
                                                f"Status: PASS - No linter errors\n"
                                            )
                                            _post_tool_messages.append({
                                                "role": "system",
                                                "content": lint_msg
                                            })
                                            tui.append_stream(f"✅ LINTER PASS: {os.path.basename(path)}")
                                except Exception as lint_error:
                                    # Don't fail the write_file if linter fails
                                    tui.append_stream(f"Linter check failed: {str(lint_error)[:50]}")
                            else:
                                # File not found after write
                                tui.update_file(os.path.basename(path), "error")
                                tui.append_stream(f"❌ Failed: {os.path.basename(path)}")
                                result = f"⚠️ File not found after write: {path}"
                        elif not is_error_result:
                            # Clear error history on success (for other tools)
                            loop.error_history = []
                            
                    except Exception as e:
                        error_msg = str(e)
                        result = f"Error: {error_msg}"
                        result_str = result
                        tui.append_stream(f"❌ Error: {error_msg[:50]}")
                        try:
                            if lg:
                                from vaf.core.subagent_debug import summarize_result
                                lg.event("tool_end", tool=fn_name, ok=False, error=error_msg, **summarize_result(result))
                        except Exception:
                            pass
                        if fn_name == "write_file" and "path" in fn_args:
                            tui.update_file(os.path.basename(fn_args["path"]), "error")
                            
                            # Track repeated errors
                            error_key = f"{fn_name}:{fn_args.get('path', '')}:{error_msg[:100]}"
                            loop.error_history.append(error_key)
                            
                            # If same error 3 times in a row, stop and report
                            if len(loop.error_history) >= 3:
                                last_3 = loop.error_history[-3:]
                                if len(set(last_3)) == 1:  # All same error
                                    return (
                                        f"### ❌ Repeated Error Detected\n\n"
                                        f"The same error occurred 3 times in a row:\n"
                                        f"**File**: {fn_args.get('path', 'unknown')}\n"
                                        f"**Error**: {error_msg}\n\n"
                                        f"**Possible causes:**\n"
                                        f"- File is locked by another program (editor, browser, etc.)\n"
                                        f"- Insufficient permissions\n"
                                        f"- Disk space issues\n\n"
                                        f"**Solution:**\n"
                                        f"1. Close any programs that might have the file open\n"
                                        f"2. Check file permissions\n"
                                        f"3. Try a different file path\n\n"
                                        f"Stopped to prevent infinite loop."
                                    )
                
                # Update TUI after tool execution
                live.update(tui.render())
                
                # Check for error patterns in result and prevent bash echo of errors
                if not 'result_str' in locals():
                    result_str = str(result)
                
                is_error = (
                    result_str.startswith("❌") or 
                    result_str.startswith("Error:") or
                    "permission denied" in result_str.lower() or
                    "locked" in result_str.lower() or
                    "file write error" in result_str.lower() or
                    "cannot write" in result_str.lower()
                )
                
                # If result is an error and agent tries to use bash to echo it, block it
                if is_error and fn_name == "bash":
                    # Extract the command to check if it's trying to echo an error
                    command = fn_args.get('command', '')
                    if 'echo' in command.lower() and ('error' in command.lower() or '❌' in command or 'file write' in command.lower()):
                        # Block this - don't let agent echo errors via bash
                        result = (
                            f"⚠️ Cannot use bash to display errors.\n"
                            f"The previous operation failed. Please:\n"
                            f"1. Read the error message above\n"
                            f"2. Fix the underlying issue (e.g., close file, check permissions)\n"
                            f"3. Try the operation again with write_file\n"
                            f"DO NOT use bash to echo error messages."
                        )
                        result_str = result
                        tui.append_stream("Blocked bash echo of error - fix the issue instead")
                
                # Add result to the history that was active WHEN this tool was called.
                # Using _history_at_dispatch (saved before execution) ensures the result
                # lands in the correct context even if a context switch happened during
                # execution (e.g. set_todos triggers switch_to_task_context which
                # reassigns the `history` variable via sync_legacy_vars).
                _log_to_file(f"[DEBUG-X] Adding tool result to history: fn_name={fn_name}, result_len={len(result_str)}")
                # read_file can return large files; give it more room so the agent
                # can verify file contents without hitting the truncation limit.
                _result_char_limit = 8000 if fn_name == "read_file" else 3000
                _history_at_dispatch.append({
                    "role": "tool",
                    "tool_call_id": tc['id'],
                    "name": fn_name,
                    "content": result_str[:_result_char_limit]
                })

                # Flush deferred post-tool messages (nudges, linter output, truncation
                # warnings) AFTER the tool result so the API sees the valid sequence:
                #   assistant+tool_calls → role:tool → system(nudge) [valid]
                # rather than:
                #   assistant+tool_calls → system(nudge) → role:tool [INVALID — 400]
                for _ptm in _post_tool_messages:
                    _history_at_dispatch.append(_ptm)

        # ═══════════════════════════════════════════════════════════════════
        # LOOP ENDED (timeout or max empty)
        # ═══════════════════════════════════════════════════════════════════

        # End-of-run PARTIAL cleanup: remove any PARTIAL_{name} files that
        # have a corresponding real file (these are leftover from streaming
        # truncation events that happened after the last write_file success).
        try:
            if base_dir and os.path.isdir(base_dir):
                for _fname in os.listdir(base_dir):
                    if _fname.startswith("PARTIAL_"):
                        _real_name = _fname[len("PARTIAL_"):]
                        _partial_fp = os.path.join(base_dir, _fname)
                        _real_fp = os.path.join(base_dir, _real_name)
                        if os.path.exists(_real_fp):
                            try:
                                os.remove(_partial_fp)
                                tui.append_stream(f"[Coder] End-of-run: removed stale {_fname}")
                            except Exception:
                                pass
        except Exception:
            pass

        # ═══════════════════════════════════════════════════════════════
        # FINAL COMMIT - persist the run result on every exit path.
        # The model rarely calls the git tools itself; without this the
        # deliverable stays untracked (runs ended with only 'Initial commit').
        # Runs with failed/remaining tasks commit too - the message carries
        # the status, an honest snapshot beats losing work.
        # ═══════════════════════════════════════════════════════════════
        final_commit_note = ""
        if not skip_template:
            try:
                _total_tasks = len(task_mgr.todos) if task_mgr and task_mgr.todos else 0
                _completed_tasks = (
                    len([t for t in task_mgr.todos if t["status"] == "completed"]) if _total_tasks else 0
                )
                _run_status = "COMPLETE" if (task_mgr and task_mgr.is_all_completed()) else "PARTIAL"
                _commit_msg = (
                    f"VAF Coder: {' '.join(task.split())[:60]}\n\n"
                    f"Status: {_run_status} ({_completed_tasks}/{_total_tasks} tasks)"
                )
                final_commit_note = _final_commit(base_dir, _commit_msg)
                if final_commit_note:
                    tui.append_stream(f"[Coder] {final_commit_note}")
                try:
                    if lg:
                        lg.event("final_commit", note=final_commit_note, status=_run_status)
                except Exception:
                    pass
                # WebUI: show the final commit in the source-control section
                _emit_coder_state()
            except Exception:
                final_commit_note = ""
        git_line = f"**💾 {final_commit_note}**\n" if final_commit_note else ""

        if files_created:
            # ═══════════════════════════════════════════════════════════════
            # CONTENT_ONLY MODE: Return actual file content instead of summary
            # ═══════════════════════════════════════════════════════════════
            if skip_template:
                # CONTENT_ONLY mode: Return the actual content of the created file(s)
                # Priority: Single file > Main file (index.html, main.py, etc.) > First file
                main_file = None
                
                # Find main file (prioritize common main files)
                main_file_patterns = ['index.html', 'main.py', 'app.py', 'script.py', 'main.js', 'app.js']
                for pattern in main_file_patterns:
                    for f in files_created:
                        if os.path.basename(f).lower() == pattern.lower():
                            main_file = f
                            break
                    if main_file:
                        break
                
                # If no main file found, use first file
                if not main_file and files_created:
                    main_file = files_created[0]
                
                # If exactly one file, use it
                if len(files_created) == 1:
                    main_file = files_created[0]
                
                # Read and return the content
                if main_file:
                    try:
                        full_path = main_file if os.path.isabs(main_file) else os.path.join(base_dir, main_file)
                        if os.path.exists(full_path):
                            with open(full_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                            
                            # Clean up temporary directory in CONTENT_ONLY mode
                            try:
                                import shutil
                                shutil.rmtree(base_dir)
                            except:
                                pass  # Ignore cleanup errors
                            
                            return content
                    except Exception as e:
                        # Fallback to summary if reading fails
                        pass
            
            # Normal mode: Return project summary
            files_list = _format_file_links(files_created, base_dir)
            dir_link = _get_clickable_path(base_dir)
            open_instructions = _get_open_instructions(files_created, base_dir)
            
            # Try to open the folder automatically
            folder_opened = _open_folder(base_dir)
            folder_status = "✅ Folder opened in file manager" if folder_opened else "📂 Folder ready"
            
            # Check for unchanged placeholders
            placeholder_check = QualityChecker.check_placeholders(files_created, base_dir)
            placeholder_warning = ""
            if placeholder_check['has_placeholders']:
                placeholder_warning = "\n\n### ⚠️ Unchanged Placeholders Found\n"
                for fname, placeholders in placeholder_check['files_with_placeholders'].items():
                    placeholder_warning += f"\n**{fname}**:\n"
                    for p in placeholders[:5]:
                        placeholder_warning += f"- {p}\n"
                placeholder_warning += f"\n*Total: {placeholder_check['total_placeholders']} placeholders may need customization*"
            
            # Check task status for Main Agent
            task_status = ""
            has_incomplete_tasks = False
            failed_list = []
            if task_mgr and task_mgr.todos:
                completed_count = len([t for t in task_mgr.todos if t["status"] == "completed"])
                total_count = len(task_mgr.todos)
                failed_list = task_mgr.failed_tasks()
                remaining = [t["task"] for t in task_mgr.todos if t["status"] not in ("completed", "failed")]
                task_status = f"\n\n**📋 Task Status**: {completed_count}/{total_count} tasks completed"
                if failed_list:
                    has_incomplete_tasks = True
                    task_status += f"\n**❌ Failed tasks ({len(failed_list)})** (goal verification failed after retries):\n"
                    task_status += "\n".join(
                        f"- {t['task']}: {(t.get('result') or 'failed')[:120]}" for t in failed_list[:5]
                    )
                if remaining:
                    has_incomplete_tasks = True
                    task_status += f"\n**⚠️ Remaining tasks ({len(remaining)})**:\n" + "\n".join(f"- {t}" for t in remaining[:5])
                    if len(remaining) > 5:
                        task_status += f"\n- ... and {len(remaining) - 5} more"
                if failed_list or remaining:
                    task_status += f"\n\n**💡 To complete all tasks, continue with:**\n"
                    task_status += f"`coding_agent(task=\"complete all remaining tasks\", project_path=\"{base_dir}\")`"

            # Determine completion status
            # EXPLICIT SIGNAL for Main Agent: [VAF_CODING_AGENT_STATUS]
            if has_incomplete_tasks:
                completion_header = "### ⚠️ Task Partially Complete"
                if failed_list:
                    completion_note = (
                        "**Note**: Some tasks FAILED goal verification despite retries — they were NOT silently "
                        "marked as done. Review the failed tasks above before continuing."
                    )
                else:
                    completion_note = "**Note**: Loop ended before all tasks were completed. Please continue to finish all remaining tasks."
                status_signal = "[VAF_CODING_AGENT_STATUS: PARTIAL]"
            else:
                completion_header = "### ✅ Task Completed"
                completion_note = "**Note**: All tasks have been completed. Review files for any final adjustments."
                status_signal = "[VAF_CODING_AGENT_STATUS: COMPLETE]"
            
            return (
                f"{status_signal}\n\n"  # Explicit signal for Main Agent parsing
                f"{completion_header}\n\n"
                f"**📁 Project Directory**: {dir_link}\n"
                f"**Full Path**: `{base_dir}`\n"
                f"{folder_status}\n\n"
                f"**📄 Files ({len(files_created)})**:\n{files_list}\n\n"
                f"{open_instructions}\n\n"
                f"**⏱️ Time**: {loop.get_elapsed_str()}\n"
                f"**🔄 Loops**: {loop.loop_count}\n"
                f"{git_line}"
                f"{task_status}\n\n"
                f"{completion_note}\n\n"
                f"**🔧 To continue working on this project, use:**\n"
                f"`coding_agent(task=\"your task\", project_path=\"{base_dir}\")`"
                f"{placeholder_warning}"
            )
        else:
            # Even if no files were created, show the directory
            dir_link = _get_clickable_path(base_dir)
            folder_opened = _open_folder(base_dir)
            folder_status = "✅ Folder opened in file manager" if folder_opened else "📂 Folder ready"
            
            return (
                f"[VAF_CODING_AGENT_STATUS: FAILED]\n\n"  # Explicit signal for Main Agent
                f"### ❌ Task Failed\n\n"
                f"**📁 Project Directory**: {dir_link}\n"
                f"**Full Path**: `{base_dir}`\n"
                f"{folder_status}\n"
                f"{git_line}\n"
                f"No files were created in {loop.loop_count} loops.\n\n"
                f"**💡 Suggestion**: Try a more specific task description.\n\n"
                f"**🔧 To retry, use:**\n"
                f"`coding_agent(task=\"your task\", project_path=\"{base_dir}\")`"
            )
        
        # CRITICAL: Always stop Live display at the end of the method
        # This prevents "zombie" TUI threads that cause multiple header boxes
        stop_live()
        
        # Clear active instance when done
        with CodingAgentTool._instance_lock:
            if CodingAgentTool._active_instance == tui:
                CodingAgentTool._active_instance = None


# ═══════════════════════════════════════════════════════════════════════════════
# VAF CODER DEVELOPMENT & STABILITY LOG - THE "MASTER FIX" REFERENCE
# ═══════════════════════════════════════════════════════════════════════════════
#
# OVERVIEW:
# Today's development session focused on transforming the Coding Sub-Agent from 
# an experimental tool into a production-grade autonomous powerhouse. 
# We solved critical issues ranging from UI glitches to deep logic loops.
#
#  MAJOR FIXES & ARCHITECTURAL IMPROVEMENTS:
#
# 1.  ZOMBIE HEADERS & DOUBLE TUI FIX
#    - PROBLEM: Multiple "Collaboration Mode" boxes piling up in terminal.
#    - SOLUTION: Implemented a strict Singleton-like Pattern using `_instance_lock` 
#      and `_active_instance`. When a new `run()` starts, it explicitly stops 
#      the previous instance's `Live` context and thread.
#    - REASONING: Prevented concurrent UI threads from fighting over `stdout`.
#
# 2.  THE "LOOP 0" INITIALIZATION HANG
#    - PROBLEM: Agent stuck at "Creating project..." forever.
#    - CAUSE: Deadlock between the manual `animation_thread` and Main Thread 
#      competing for the TUI's `RLock` during heavy startup logging.
#    - SOLUTION: Removed the manual animation thread. Replaced with Rich's native 
#      `refresh_per_second` + `__rich__` method. Added small `time.sleep(0.05)` 
#      pauses during init to give the render thread air.
#
# 3.  AGENTIC LOOP STABILITY (THE "LOOTTO LOOP")
#    - PROBLEM: Model stuck in "Empty response -> Reset -> Empty response" loops.
#    - SOLUTION A (Main Agent): Implemented "Adaptive Temperature Sweep". 
#      Retries now oscillate creativity (0.1, 0.5, 0.2, 0.6...) to break 
#      deterministic "stuck" states.
#    - SOLUTION B (Coder): Relaxed the `is_effectively_empty` filter to allow 
#      shorter affirmations from smaller models (like VQ-1).
#
# 4.  ANTI-HALLUCINATION GUARD
#    - PROBLEM: Agent calling `task_done` without actually writing any code.
#    - SOLUTION: Implemented a strict validation check. If a task implies creation 
#      (create, generate, write) but `files_created` is empty, `task_done` is 
#      rejected with a high-priority "CRITICAL ERROR" system message.
#
# 5.  VISUAL TRANSPARENCY (THE "PREVIEW" PANEL)
#    - PROBLEM: User felt "blind" during long generation phases.
#    - SOLUTION A: Added a top-level `Code Preview` panel.
#    - SOLUTION B: Implemented Live Stream Detection. Regex scans the stream 
#      for unclosed ` ``` ` blocks and updates the preview in real-time.
#    - SOLUTION C: Diff View. Using `difflib.unified_diff`, the agent now shows 
#      red/green +/- changes when overwriting existing files.
#
# 6.  RECURSION BLOCK
#    - PROBLEM: Coding Agent trying to call `coding_agent` tool inside itself.
#    - SOLUTION: Hard-coded intercept in the tool execution loop. Returns a 
#      System Error to the model explaining it is already the Coding Agent.
#
# 7.  OPTIMIZED PATHS
#    - PROBLEM: Slow file system checks causing perceived hangs.
#    - SOLUTION: Converted project path generation to O(1) by using short 
#      timestamps instead of incremental `while os.path.exists` loops.
#
# 8.  STDOUT LEAK PROTECTION (Main Agent Silence)
#    - PROBLEM: Main Agent "Thinking" text leaking into the Coder TUI.
#    - SOLUTION: Patched `vaf/cli/cmd/run.py` and `vaf/cli/tui.py` to suppress 
#      all `UI.event` and `stream_callback` output if an active Coder instance 
#      is detected.
#
# FINAL RESULT: 
# A rock-solid, transparent, and persistent coding environment that handles 
# failures autonomously and keeps the user informed at every millisecond.
#
# Date: Sonntag, 28. Dezember 2025
# ═══════════════════════════════════════════════════════════════════════════════

#   - Stop previous instance when new one starts
# ═══════════════════════════════════════════════════════════════════════════════
