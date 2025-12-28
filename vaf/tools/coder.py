"""
VAF Coding Agent - Agentic Loop Implementation

Design Philosophy (like OpenCode/Cursor):
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

class CoderTUI:
    """
    Terminal UI for the Coder Agent.
    Shows real-time progress like a mini-IDE with live streaming output.
    """
    
    # Number of lines to show in live stream (scrolling view)
    STREAM_LINES = 10
    
    def __init__(self, console: Console, task: str, task_mgr=None):
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
        
        # Create animated header once - it will be reused in render()
        # Render fresh each time when Live is running to keep animation alive
        self._header = AnimatedHeader("Collaboration Mode Active", "Main Agt", "Coder")
        self._live_started = False  # Track if Live has been started
        self._header_visible = False  # Track if header has been shown (sticky - once shown, stays visible)
        
        # Store Live and animation_running so they can be stopped from outside
        self._live = None  # Will be set when Live is created
        self._animation_running = None  # Will be set when animation thread starts
        
        # Active Code Preview (for showing what's being written)
        self.active_code_preview = None  # {filename, content, language}
        
        # Stream Code Detection
        self._code_buffer = ""
        self._in_code_block = False
        self._code_lang = "text"

    def set_code_preview(self, filename: str, content: str, language: str = "python"):
        """Set the code preview to show at the top."""
        with self._lock:
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
            self.active_code_preview = None
    
    def add_file(self, filename: str, size: int = 0, status: str = "creating"):
        """Add a file to the display."""
        with self._lock:
            self.files[filename] = {
                "status": status,
                "size": size,
                "preview": ""
            }
    
    def update_file(self, filename: str, status: str = None, size: int = None, preview: str = None):
        """Update file info."""
        with self._lock:
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
            self.current_action = action
            self.actions_log.append(f"{time.strftime('%H:%M:%S')} {action}")
            if len(self.actions_log) > 10:
                self.actions_log.pop(0)
    
    def increment_loop(self):
        """Increment loop counter."""
        self.loop_count += 1
    
    # ═══════════════════════════════════════════════════════════════════
    # STREAMING METHODS (Cursor-style live output)
    # ═══════════════════════════════════════════════════════════════════
    
    def start_stream(self):
        """Start a new stream (agent is generating)."""
        with self._lock:
            self.stream_active = True
            self.current_stream = ""
    
    def append_stream(self, chunk: str):
        """Append text to the current stream (each chunk is a new line)."""
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
    
    def end_stream(self):
        """End the current stream."""
        with self._lock:
            self.stream_active = False
    
    def clear_stream(self):
        """Clear the stream buffer for fresh output."""
        with self._lock:
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
    
    def render(self) -> Group:
        """Render the TUI as a Rich Panel - ALL IN ONE WINDOW."""
        with self._lock:
            elapsed = int(time.time() - self.start_time)
            elapsed_str = f"{elapsed//60}:{elapsed%60:02d}"
            
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
                        "content": code_content,
                        "language": language,
                        "timestamp": time.time()  # Always fresh
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
                    # Truncate content if too long (keep first 15 lines)
                    lines = content.split('\n')
                    if len(lines) > 15:
                        content = '\n'.join(lines[:15]) + f"\n... ({len(lines)-15} more lines)"
                    
                    syntax = Syntax(
                        content, 
                        self.active_code_preview["language"], 
                        theme="monokai", 
                        line_numbers=True,
                        word_wrap=True
                    )
                    
                    preview_panel = Panel(
                        syntax,
                        title=f"[bold yellow]📝 Editing: {self.active_code_preview['filename']}[/bold yellow]",
                        border_style="yellow",
                        padding=(1, 2),
                        width=WIDTH
                    )

            # ═══════════════════════════════════════════════════════════════
            # HEADER - Render fresh for animation, but only when Live is running AND we have content
            # ═══════════════════════════════════════════════════════════════
            
            # FIX: Only show header after Live has started AND we have content
            # Use a flag to ensure header is only shown once Live is fully running
            # Once shown, keep it visible (sticky) to prevent flickering
            if not self._live_started:
                # Don't show header yet - wait for Live to start
                header = None
            else:
                # Live is running - check if we have content OR if header was already shown
                has_content = (
                    self._header_visible or  # Sticky: once shown, stays visible
                    len(self.files) > 0 or  # Files have been created
                    len(self.stream_buffer) > 0 or  # Stream content exists
                    (self.current_action and 
                     self.current_action not in ["Initializing...", "Starting...", "Ready", ""] and
                     len(self.current_action.strip()) > 0)
                )
                
                if has_content:
                    # Render fresh each time to keep animation alive
                    header = self._header.__rich__()
                    # Mark as visible once we have content (sticky)
                    if not self._header_visible:
                        self._header_visible = True
                else:
                    # No content yet - don't show header
                    header = None
            
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
            status.append(f"Time: {elapsed_str}", style="dim")
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
            
            if not all_text.strip():
                all_text = "Ready\n"
            
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
                # Filter out redacted reasoning tags
                if "</think>" in line or "<think>" in line:
                    continue
                # Skip empty lines at the end
                if not line.strip() and not filtered_lines:
                    continue
                filtered_lines.append(line)
            
            # Remove trailing empty lines
            while filtered_lines and not filtered_lines[-1].strip():
                filtered_lines.pop()
            
            for i, line in enumerate(filtered_lines):
                # Determine style
                if "✅" in line or "done" in line.lower():
                    style = "green"
                elif "❌" in line or "error" in line.lower():
                    style = "red"
                elif "🔧" in line or "Calling" in line:
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
                Text(f"Task: {self.task[:WIDTH-10]}{'...' if len(self.task) > WIDTH-10 else ''}", style="bold"),
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
        
        # Also check for very long idle time (5+ minutes without any response)
        # This catches cases where model completely stopped responding
        if idle_minutes > 5:
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
    Manages a TODO list that the agent MUST work through.
    Prevents chaos/infinite loops by enforcing structure.
    """
    
    def __init__(self):
        self.todos: List[Dict] = []
        self.current_task_idx = 0
        self.completed: List[str] = []
        self.phase = "planning"  # planning, executing, verifying, done
    
    def set_todos(self, todos: List[str]):
        """Set the TODO list (from agent's planning phase)."""
        self.todos = [{"task": t, "status": "pending"} for t in todos]
        self.phase = "executing"
    
    def get_current_task(self) -> Optional[str]:
        """Get the current task to work on."""
        if self.current_task_idx >= len(self.todos):
            return None
        return self.todos[self.current_task_idx]["task"]
    
    def complete_current_task(self, result: str = "done"):
        """Mark current task as complete and move to next."""
        if self.current_task_idx < len(self.todos):
            self.todos[self.current_task_idx]["status"] = "completed"
            self.todos[self.current_task_idx]["result"] = result
            self.completed.append(self.todos[self.current_task_idx]["task"])
            self.current_task_idx += 1
        
        if self.current_task_idx >= len(self.todos):
            self.phase = "verifying"
    
    def get_progress(self) -> str:
        """Get progress string for display."""
        if not self.todos:
            return "Planning..."
        done = len([t for t in self.todos if t["status"] == "completed"])
        return f"Task {done}/{len(self.todos)}"
    
    def get_todos_for_prompt(self) -> str:
        """Get formatted TODO list for prompt injection."""
        if not self.todos:
            return ""
        
        lines = ["## CURRENT TODO LIST:"]
        for i, todo in enumerate(self.todos):
            status = "✅" if todo["status"] == "completed" else "⏳" if i == self.current_task_idx else "○"
            marker = ">>> " if i == self.current_task_idx else "    "
            lines.append(f"{marker}{status} {i+1}. {todo['task']}")
        
        if self.current_task_idx < len(self.todos):
            lines.append(f"\n## CURRENT TASK: {self.get_current_task()}")
            lines.append("Complete THIS task, then call `task_done` tool.")
        
        return "\n".join(lines)
    
    def is_all_done(self) -> bool:
        """Check if all tasks are completed."""
        return self.phase == "verifying" or (self.todos and all(t["status"] == "completed" for t in self.todos))


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
# Git Tools for Coding Agent
# ═══════════════════════════════════════════════════════════════════════════════

class GitInitTool(BaseTool):
    """Initialize a Git repository in the project directory."""
    name = "git_init"
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
            
            subprocess.run(['git', 'init'], cwd=base_dir, check=True, capture_output=True)
            return "✅ Git repository initialized successfully."
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            return f"❌ Error initializing Git: {e}"


class GitAddCommitTool(BaseTool):
    """Add files to Git staging area and commit them."""
    name = "git_add_commit"
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
            if files:
                for f in files:
                    file_path = os.path.join(base_dir, f) if not os.path.isabs(f) else f
                    if os.path.exists(file_path):
                        subprocess.run(['git', 'add', file_path], cwd=base_dir, check=True, capture_output=True)
            else:
                subprocess.run(['git', 'add', '.'], cwd=base_dir, check=True, capture_output=True)
            
            # Commit
            subprocess.run(['git', 'commit', '-m', message], cwd=base_dir, check=True, capture_output=True)
            return f"✅ Committed changes: {message}"
        except subprocess.CalledProcessError as e:
            return f"❌ Error committing: {e}"
        except FileNotFoundError:
            return "❌ Git not found. Please install Git."


class GitStatusTool(BaseTool):
    """Get the current Git status of the repository."""
    name = "git_status"
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
            
            result = subprocess.run(['git', 'status', '--short'], cwd=base_dir, capture_output=True, text=True, check=True)
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
            
            result = subprocess.run(
                ['git', 'log', f'--max-count={limit}', '--oneline'],
                cwd=base_dir,
                capture_output=True,
                text=True,
                check=True
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
    
    # Class-level lock to prevent multiple instances running simultaneously
    _instance_lock = threading.Lock()
    _active_instance = None  # Track active CoderTUI instance
    
    description = """Autonomous code generation Sub-Agent. USE THIS FOR:
- Creating websites (HTML, CSS, JavaScript)
- Building web applications
- Writing Python scripts
- Any coding/programming task

Creates complete, working files in a project directory. 
Do NOT plan or describe - just call this tool with the task."""

    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The coding task to complete."
            },
            "project_path": {
                "type": "string",
                "description": "Optional: Path to existing project directory to continue working on. If provided, agent will work in this directory instead of creating a new one."
            }
        },
        "required": ["task"]
    }

    def _generate_project_directory(self, task: str) -> str:
        """Generate a user-friendly project directory name based on task. OS-independent."""
        task_lower = task.lower()
        
        # Detect project type
        if any(kw in task_lower for kw in ['website', 'webseite', 'homepage', 'landing page', 'seite']):
            prefix = "Webseite"
        elif any(kw in task_lower for kw in ['webapp', 'web app', 'application', 'anwendung']):
            prefix = "Webapp"
        elif any(kw in task_lower for kw in ['script', 'skript', 'python script']):
            prefix = "Script"
        elif any(kw in task_lower for kw in ['project', 'projekt']):
            prefix = "Projekt"
        else:
            prefix = "Projekt"
        
        # Extract key words from task (remove common words)
        stop_words = {'the', 'a', 'an', 'for', 'in', 'on', 'at', 'to', 'of', 'and', 'or', 'but', 'mit', 'für', 'in', 'auf', 'zu', 'von', 'und', 'oder', 'aber', 'eine', 'ein', 'der', 'die', 'das'}
        words = re.findall(r'\b[a-zA-ZäöüÄÖÜß]{3,}\b', task)
        keywords = [w for w in words if w.lower() not in stop_words][:3]  # Max 3 keywords
        
        # Create name
        if keywords:
            name_part = ' '.join(keywords[:2]).title()  # Max 2 keywords
            # Clean name (remove special chars, limit length) - OS-independent
            name_part = re.sub(r'[^a-zA-Z0-9\s]', '', name_part)[:25]
            project_name = f"{prefix} {name_part}".strip()
        else:
            # Fallback: use first meaningful words
            words = task.split()[:3]
            name_part = ' '.join([w for w in words if len(w) > 3])[:20]
            project_name = f"{prefix} {name_part}".strip() if name_part else f"{prefix} {int(time.time())}"
        
        # Ensure valid directory name (OS-independent - handles Windows, Linux, macOS)
        # Windows: < > : " / \ | ? *
        # Linux/macOS: / (and null byte, but we don't use that)
        invalid_chars = r'[<>:"/\\|?*]'
        project_name = re.sub(invalid_chars, '_', project_name)
        project_name = project_name.strip('. ')  # Remove leading/trailing dots and spaces
        
        # Get base directory - OS-independent using os.path
        if platform.system() == "Windows":
            docs_dir = os.path.join(os.path.expanduser("~"), "Documents")
        elif platform.system() == "Darwin":  # macOS
            docs_dir = os.path.join(os.path.expanduser("~"), "Documents")
        else:  # Linux and others
            docs_dir = os.path.expanduser("~")
        
        base_dir = os.path.join(docs_dir, "VAF_Projects", project_name)
        
        # Handle duplicates - OS-independent
        counter = 1
        original_base = base_dir
        while os.path.exists(base_dir):
            base_dir = f"{original_base}_{counter}"
            counter += 1
        
        return base_dir
    
    def _ensure_git_repo(self, base_dir: str):
        """Initialize Git repository if not already initialized. OS-independent."""
        git_dir = os.path.join(base_dir, '.git')
        if os.path.exists(git_dir):
            return  # Already a git repo
        
        try:
            import subprocess
            # Initialize git repo - OS-independent (git works on all platforms)
            # Add timeout to prevent hanging
            subprocess.run(['git', 'init'], cwd=base_dir, check=True, capture_output=True, timeout=30)
            
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
                subprocess.run(['git', 'add', '.'], cwd=base_dir, check=True, capture_output=True, timeout=30)
                subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=base_dir, check=True, capture_output=True, timeout=30)
            except:
                pass  # No files to commit yet or commit failed - ignore
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, Exception):
            # Git not available, failed or timed out - continue without git (graceful degradation)
            # Do NOT crash the agent for git issues
            pass

    def run(self, **kwargs) -> str:
        task = kwargs.get('task', '')
        if not task:
            return "Error: No task provided."

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
        
        tui = CoderTUI(UI.console, task, task_mgr)
        
        # Mark this as the active instance
        with CodingAgentTool._instance_lock:
            CodingAgentTool._active_instance = tui
        
        # Set initial action before first render
        tui.set_action("Initializing...")
        
        # CRITICAL: Render once before starting Live to prevent multiple empty renders
        tui.set_action("Starting...")
        initial_render = tui.render()
        
        # Use Rich's Live with auto-refresh for animation
        # Higher refresh rate for smooth spinner animation (15 FPS)
        # Live needs to be updated regularly for animations to work
        live = Live(
            initial_render,
            console=UI.console,
            refresh_per_second=15,  # Higher rate for smooth spinner animation
            transient=False,  # Keep final output visible after stop
        )
        
        # Store Live in TUI so it can be stopped from outside
        tui._live = live
        
        live.start()
        
        # CRITICAL: Mark Live as started AFTER live.start() to prevent race conditions
        # This ensures no header is rendered before Live takes over
        tui._live_started = True
        
        # CRITICAL: Force first update AFTER _live_started is True
        # This ensures header appears only after Live is fully running
        # This prevents empty headers from appearing before Live takes over
        live.update(tui.render())
        
        # Start a background thread to update Live regularly for smooth animations
        # This ensures the spinner and time display update continuously
        import threading
        animation_running = threading.Event()
        animation_running.set()  # Start as running
        
        # Store animation_running in TUI so it can be stopped from outside
        tui._animation_running = animation_running
        
        def animation_updater():
            """Continuously update Live display for smooth animations."""
            while animation_running.is_set():
                try:
                    live.update(tui.render())
                    time.sleep(1.0 / 15)  # 15 FPS
                except:
                    break
        
        animation_thread = threading.Thread(target=animation_updater, daemon=True)
        animation_thread.start()
        
        def stop_live():
            """Stop animation and live display cleanly."""
            animation_running.clear()  # Stop animation thread
            try:
                live.stop()
            except Exception:
                pass  # Ignore errors when stopping
        
        # Server health check
        tui.set_action("Checking server...")
        try:
            health = requests.get("http://127.0.0.1:8080/health", timeout=5)
            if health.status_code != 200:
                return f"❌ Server Error: VAF Server nicht bereit (Status {health.status_code}). Bitte starten Sie den VAF Server auf Port 8080."
            tui.set_action("Server ready")
        except requests.exceptions.ConnectionError:
            return "❌ Connection Error: VAF Server nicht erreichbar (Port 8080). Bitte starten Sie den VAF Server."
        except Exception as e:
            return f"❌ Server Check Failed: {e}. Bitte überprüfen Sie, ob der VAF Server läuft."
        
        # ═══════════════════════════════════════════════════════════════════
        # TOOLS - File tools + TODO management (NOT coding_agent!)
        # ═══════════════════════════════════════════════════════════════════
        tui.set_action("Loading tools...")
        live.update(tui.render())
        
        # IMPORTANT: coding_agent must NOT have access to itself!
        # Note: base_dir will be set later, after project directory is created
        from vaf.tools.linter import LinterTool
        self.local_tools = {
            "write_file": WriteFileTool(),
            "read_file": ReadFileTool(),
            "list_files": ListFilesTool(),
            "python_sandbox": PythonSandboxTool(),
            "linter": LinterTool(),
            # Git tools will be added after base_dir is set
            # NO: coding_agent, librarian_agent - prevents recursion
        }
        if HAS_CODING_TOOLS:
            self.local_tools["bash"] = BashTool()
            self.local_tools["codesearch"] = CodeSearchTool()

        # Setup working directory
        tui.set_action("Creating project...")
        live.update(tui.render())
        
        # ═══════════════════════════════════════════════════════════════════
        # CHECK FOR CONTENT_ONLY MODE (before creating project directory)
        # ═══════════════════════════════════════════════════════════════════
        
        # Skip template if task explicitly requests content-only (no project structure)
        # This is used by automations that need just HTML/content, not a full project
        skip_template = (
            "CONTENT_ONLY" in task.upper() or 
            "NO_TEMPLATE" in task.upper() or
            "ONLY THE" in task.upper() or  # "Generate ONLY the HTML..."
            "RETURN ONLY" in task.upper() or  # "Return ONLY the HTML code..."
            "NO PROJECT" in task.upper() or
            "NO FILE PATHS" in task.upper()
        )
        
        # Check if continuing existing project
        project_path = kwargs.get('project_path', '')
        if project_path:
            # Continue existing project
            base_dir = os.path.abspath(os.path.expanduser(project_path))
            if not os.path.exists(base_dir):
                return f"❌ Error: Project directory not found: {base_dir}\nPlease provide a valid path to an existing project."
            tui.append_stream(f"Continuing project: {os.path.basename(base_dir)}")
        elif skip_template:
            # CONTENT_ONLY mode: Use a temporary directory instead of creating a project
            import tempfile
            base_dir = tempfile.mkdtemp(prefix="vaf_content_")
            tui.append_stream("Content-only mode: Using temporary directory")
        else:
            # Normal mode: Create project directory
            base_dir = self._generate_project_directory(task)
            os.makedirs(base_dir, exist_ok=True)
            tui.append_stream(f"New project: {os.path.basename(base_dir)}")
        
        # Initialize Git repository if not already initialized (skip for CONTENT_ONLY)
        if not skip_template:
            self._ensure_git_repo(base_dir)
        
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
        
        # ═══════════════════════════════════════════════════════════════════
        # TEMPLATE ANALYSIS - Use LLM with own context BEFORE starting work
        # ═══════════════════════════════════════════════════════════════════
        
        template_type = None
        template_files = []
        
        if not skip_template:
            tui.set_action("Analyzing task for template...")
            live.update(tui.render())
            
            # Use LLM to intelligently detect template type (has its own context)
            # This runs BEFORE the main coding work begins
            template_type, decision_info = TemplateManager.detect_template_type_with_llm(task)
            
            # Output detailed decision process
            tui.append_stream("─" * 60)
            tui.append_stream("Template Selection Process:")
            for line in decision_info.split('\n'):
                if line.strip():
                    tui.append_stream(f"  {line}")
            tui.append_stream("─" * 60)
            
            if template_type:
                tui.append_stream(f"Selected template: {template_type}")
            else:
                tui.append_stream("No template selected")
                tui.append_stream("-> Will use web_deep_search to research implementation")
                tui.append_stream("-> Then create TODO list and implement from scratch")
            live.update(tui.render())
        
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
            live.update(tui.render())
        
        # ═══════════════════════════════════════════════════════════════════
        # SYSTEM PROMPT
        # ═══════════════════════════════════════════════════════════════════
        
        tui.set_action("Building prompt...")
        live.update(tui.render())
        existing_files_info = ""
        if template_files:
            existing_files_info = f"""
## ⚠️ CRITICAL: TEMPLATE FILES EXIST - DO NOT REPLACE THEM!

The following files were already created from a template:
{chr(10).join(['- ' + os.path.basename(f) for f in template_files])}

### 🚨 MANDATORY TEMPLATE WORKFLOW (Templates are REQUIRED structure!):

**STEP 1: READ FIRST** - `read_file(path="{base_dir}/index.html")` for EVERY template file BEFORE modifying
**STEP 2: PRESERVE ALL** - Keep EVERY section (nav, hero, services, about, contact, footer), class, ID
**STEP 3: ONLY REPLACE** - Replace `{{PLACEHOLDER}}` text with real content - nothing else
**STEP 4: WRITE BACK** - Write modified version (not complete rewrite)

### ❌ FORBIDDEN:
- DO NOT rewrite from scratch
- DO NOT remove sections (nav, hero, services, about, contact, footer)
- DO NOT remove classes or IDs
- DO NOT ignore template structure

### ✅ CORRECT:
Template: `<nav class="nav"><div class="logo">{{BUSINESS_NAME}}</div></nav>`
✅ Correct: `<nav class="nav"><div class="logo">Testler Handwerksmeister</div></nav>` (only replaced placeholder)
❌ Wrong: `<header><h1>Testler Handwerksmeister</h1></header>` (removed nav - will be BLOCKED!)

**If you remove template structure, write_file will be BLOCKED!**
"""
        
        system_prompt = f"""You are the VAF Coding Sub-Agent - an autonomous code generator.

## PROJECT DIRECTORY
`{base_dir}`
All files must use ABSOLUTE paths in this directory.
{existing_files_info}

## TOOLS
- `set_todos`: REQUIRED FIRST - set your task breakdown
- `task_done`: Mark current task complete, get next task
- `write_file`: Create/write files (path, content)
- `read_file`: Read file contents (path)  
- `list_files`: List directory (path)
- `python_sandbox`: Execute Python code safely for calculations, algorithms, and data processing (code)
- `linter`: Check code files for syntax errors and linting issues (path, optional file_type)
- `web_fetch`: Fetch webpage HTML (url, optional selector)
- `web_deep_search`: Deep search web for solutions/ideas (query, max_results). Use when you don't know how to fix an error or need inspiration. Returns summarized results without bloating context.
- `git_init`: Initialize Git repository (if not already initialized)
- `git_add_commit`: Add files and commit changes with a message (message, optional files array)
- `git_status`: Check Git status (shows modified/staged/untracked files)
- `git_log`: View commit history (optional limit parameter)
{"- `bash`: Execute shell commands" if HAS_CODING_TOOLS else ""}

## MANDATORY WORKFLOW (you MUST follow this!)

### PHASE 1: PLANNING (first response)
Your FIRST action MUST be to call set_todos. Output it EXACTLY like this:
```
<tool_call>
set_todos(tasks=["Task 1", "Task 2", "Task 3", ...])
</tool_call>
```
**IMPORTANT: Call `set_todos` ONLY ONCE at the very beginning!**

### PHASE 2: EXECUTION (IMMEDIATELY AFTER PLANNING)
**DO NOT WAIT** after calling `set_todos`. Immediately start working on the first task!
For EACH task in your TODO list:
1. Focus ONLY on the current task
2. Call the necessary tools (write_file, etc.)
3. When task is complete, call `task_done`
4. The system will give you the next task

### PHASE 3: COMPLETION
After all tasks are done, say "ALL TASKS COMPLETED"

## RULES
- You MUST call `set_todos` in your FIRST response (and ONLY ONCE)
- **CRITICAL**: **DO NOT WAIT** after `set_todos` - start coding immediately!
- **CRITICAL**: DO NOT call `coding_agent`! You ARE the coding agent. Use `write_file` directly.
- **CRITICAL**: Create TODOs that MATCH the task complexity and requirements
- You MUST call `task_done` after completing each task
- Work on ONE task at a time (the system tracks progress)
- Do NOT skip tasks or do multiple at once
- Write COMPLETE code, not placeholders
- **CRITICAL**: You MUST complete ALL tasks in the TODO list - DO NOT stop until every task is done
- **CRITICAL**: You MUST use `write_file` to actually modify/create files - reading files alone is not enough
- **CRITICAL**: DO NOT claim completion if any tasks remain incomplete
- **CRITICAL**: DO NOT skip tasks or stop working prematurely - work through the entire TODO list

{("## TEMPLATE FILES RULES (if templates exist above)\n"
"🚨 **CRITICAL: Templates are REQUIRED structure - NOT suggestions!**\n\n"
"**MANDATORY WORKFLOW:**\n"
"1. **READ FIRST**: `read_file(path=\"...\")` for EVERY template file BEFORE modifying\n"
"2. **PRESERVE ALL**: Keep ALL structure from template (sections, classes, IDs, functions, imports, etc.)\n"
"3. **ONLY REPLACE**: Replace `{{PLACEHOLDER}}` text with real content - nothing else\n"
"4. **DO NOT REMOVE**: Never remove structural elements (sections, functions, classes, imports, etc.)\n"
"5. **DO NOT REWRITE**: Never write new file from scratch - always modify existing template\n\n"
"**Examples:**\n"
"- **HTML**: Template `<nav class=\"nav\"><div class=\"logo\">{{BUSINESS_NAME}}</div></nav>` → Keep nav structure, only replace placeholder\n"
"- **Python**: Template `def {{FUNCTION_NAME}}({{PARAMS}}):` → Keep function structure, only replace placeholders\n"
"- **Any file**: Preserve all template structure, only replace `{{PLACEHOLDER}}` text\n\n"
"**If you remove template structure, write_file will be BLOCKED!**") if template_files else (
"## NO TEMPLATE SELECTED - Research and Plan First\n\n"
"Since no template was selected for this task, you should:\n"
"1. **Use `web_deep_search`** to get information on how to implement this task\n"
"   - `web_deep_search` returns a simple answer (like `web_search`), no separate context\n"
"   - Example: `web_deep_search(query=\"how to create [task description]\", max_results=3)`\n"
"2. **Analyze the results** and understand the best approach\n"
"3. **Create a TODO list** with `set_todos` based on the research findings\n"
"4. **Then implement** the solution from scratch\n\n"
"**Example workflow:**\n"
"- `web_deep_search(query=\"Python script generate random lottery numbers save HTML\", max_results=3)`\n"
"- Analyze: \"I need to use random module, generate 6 unique numbers, create HTML structure\"\n"
"- `set_todos(tasks=[\"Generate 6 unique random numbers\", \"Create HTML structure\", \"Save to HTML file\"])`\n"
"- Start implementing")}

## QUALITY REQUIREMENTS
- **HTML**: Full structure, navigation, real content, min 500 bytes
- **CSS**: Reset, typography, colors, responsive, min 400 bytes
- **JavaScript**: Working code, error handling, min 100 bytes
- **Python**: Complete functionality, error handling, docstrings, min 100 bytes
- **Other files**: Complete, working code/content appropriate for the file type

## ASKING QUESTIONS TO MAIN AGENT
If you need information that's not in the task, you can ask the main agent questions!
Simply include your question in your response using this format:
❓ QUESTION: [Your question here]

Examples:
- ❓ QUESTION: What should the company name be?
- ❓ QUESTION: Which color scheme should I use for the website?
- ❓ QUESTION: What should the headline say?

The main agent will answer your questions based on the original user task and call you again with the information.
You can continue working once you have the answers.

Current Task: {task}

START by calling set_todos with your planned tasks - make sure the TODOs match the task requirements!"""
        
        # Build user message with STRONG emphasis on set_todos first
        user_msg = f"Task: {task}\n\n"
        if template_files:
            user_msg += "⚠️ TEMPLATE FILES EXIST! You MUST:\n"
            user_msg += "1. Call `set_todos` FIRST with your task breakdown\n"
            user_msg += "2. Then call `read_file` to read each template file\n"
            user_msg += "3. Then call `write_file` to modify (not replace) each file\n\n"
        else:
            user_msg += "⚠️ IMPORTANT: Your FIRST action MUST be to call `set_todos` with your task breakdown!\n"
            user_msg += "You CANNOT use write_file or other tools until you've set your TODO list.\n\n"
        
        user_msg += "Start now by calling `set_todos` with your task breakdown (as many tasks as needed)."
        
        # Initialize context manager for this coding agent session
        from vaf.core.context import ContextManager
        max_tokens = 8192  # Same as main agent
        context_manager = ContextManager(max_tokens=max_tokens)
        
        history = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg}
        ]
        
        # Snapshot history (before processing)
        history_snapshot_len = len(history)
        
        # ═══════════════════════════════════════════════════════════════
        # HELPER: Create fresh context for a new task
        # ═══════════════════════════════════════════════════════════════
        def create_fresh_context_for_task(current_task: str) -> List[Dict[str, Any]]:
            """
            Creates a completely fresh context for a new task.
            This isolates each task with its own context, preventing confusion from previous tasks.
            """
            # Detect task type for dynamic context
            task_lower = current_task.lower()
            is_html_task = any(kw in task_lower for kw in ['html', 'website', 'webpage', 'web seite'])
            is_script_task = any(kw in task_lower for kw in ['script', 'python', 'skript', '.py'])
            is_app_task = any(kw in task_lower for kw in ['app', 'application', 'anwendung'])
            
            # Rebuild existing_files_info (in case template files changed)
            fresh_existing_files_info = ""
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
            
            # Rebuild system prompt with current state
            fresh_system_prompt = f"""You are the VAF Coding Sub-Agent - an autonomous code generator.

## PROJECT DIRECTORY
`{base_dir}`
All files must use ABSOLUTE paths in this directory.
{fresh_existing_files_info}

## TOOLS
- `set_todos`: REQUIRED FIRST - set your task breakdown
- `task_done`: Mark current task complete, get next task
- `write_file`: Create/write files (path, content)
- `read_file`: Read file contents (path)  
- `list_files`: List directory (path)
- `python_sandbox`: Execute Python code safely for calculations, algorithms, and data processing (code)
- `linter`: Check code files for syntax errors and linting issues (path, optional file_type)
- `web_fetch`: Fetch webpage HTML (url, optional selector)
- `web_deep_search`: Deep search web for solutions/ideas (query, max_results). Use when you don't know how to fix an error or need inspiration. Returns summarized results without bloating context.
- `git_init`: Initialize Git repository (if not already initialized)
- `git_add_commit`: Add files and commit changes with a message (message, optional files array)
- `git_status`: Check Git status (shows modified/staged/untracked files)
- `git_log`: View commit history (optional limit parameter)
{"- `bash`: Execute shell commands" if HAS_CODING_TOOLS else ""}

## MANDATORY WORKFLOW (you MUST follow this!)

### PHASE 1: PLANNING (first response)
Your FIRST action MUST be to call set_todos. Output it EXACTLY like this:
```
<tool_call>
set_todos(tasks=["Task 1", "Task 2", "Task 3", ...])
</tool_call>
```

**CRITICAL: Create TODOs that MATCH the actual task!**
- If task says "CONTENT_ONLY" or "ONLY THE" → Create simple TODOs for single file/content
- If task says "multi-page website" → Create TODOs for multiple pages
- If task says "weather report HTML" → Create TODOs for weather HTML, not multi-page website
- Analyze the task carefully and create appropriate TODOs

Examples:
```
Task: "CONTENT_ONLY: Generate ONLY the complete HTML document content for a weather report"
✅ CORRECT: set_todos(tasks=["Generate complete HTML document with weather data", "Add embedded CSS styling", "Verify HTML is complete and valid"])
❌ WRONG: set_todos(tasks=["Read and customize index.html", "Create additional pages", ...])  # Too complex for single file!
❌ WRONG: set_todos(tasks=["Generate HTML", "Embed YouTube video", ...])  # Don't add features not mentioned in task!

Task: "Create a multi-page website for a restaurant"
✅ CORRECT: set_todos(tasks=["Create index.html (homepage)", "Create about.html page", "Create menu.html page", "Create contact.html page", "Add navigation", "Style all pages"])
❌ WRONG: set_todos(tasks=["Generate HTML document"])  # Too simple for multi-page!
```

### PHASE 2: EXECUTION (work through each task)
For EACH task in your TODO list:
1. Focus ONLY on the current task
2. Call the necessary tools (write_file, etc.)
3. When task is complete, call `task_done`
4. The system will give you the next task

### PHASE 3: COMPLETION
After all tasks are done, say "ALL TASKS COMPLETED"

## RULES
- You MUST call `set_todos` in your FIRST response
- **CRITICAL**: Create TODOs that MATCH the task complexity and requirements
- You MUST call `task_done` after completing each task
- Work on ONE task at a time (the system tracks progress)
- Do NOT skip tasks or do multiple at once
- Write COMPLETE code, not placeholders
- **CRITICAL**: You MUST complete ALL tasks in the TODO list - DO NOT stop until every task is done
- **CRITICAL**: You MUST use `write_file` to actually modify/create files - reading files alone is not enough
- **CRITICAL**: DO NOT claim completion if any tasks remain incomplete
- **CRITICAL**: DO NOT skip tasks or stop working prematurely - work through the entire TODO list

{("## TEMPLATE FILES RULES (if templates exist above)\n"
"🚨 **CRITICAL: Templates are REQUIRED structure - NOT suggestions!**\n\n"
"**MANDATORY WORKFLOW:**\n"
"1. **READ FIRST**: `read_file(path=\"...\")` for EVERY template file BEFORE modifying\n"
"2. **PRESERVE ALL**: Keep ALL structure from template (sections, classes, IDs, functions, imports, etc.)\n"
"3. **ONLY REPLACE**: Replace `{{PLACEHOLDER}}` text with real content - nothing else\n"
"4. **DO NOT REMOVE**: Never remove structural elements (sections, functions, classes, imports, etc.)\n"
"5. **DO NOT REWRITE**: Never write new file from scratch - always modify existing template\n\n"
"**Examples:**\n"
"- **HTML**: Template `<nav class=\"nav\"><div class=\"logo\">{{BUSINESS_NAME}}</div></nav>` → Keep nav structure, only replace placeholder\n"
"- **Python**: Template `def {{FUNCTION_NAME}}({{PARAMS}}):` → Keep function structure, only replace placeholders\n"
"- **Any file**: Preserve all template structure, only replace `{{PLACEHOLDER}}` text\n\n"
"**If you remove template structure, write_file will be BLOCKED!**") if template_files else (
"## NO TEMPLATE SELECTED - Research and Plan First\n\n"
"Since no template was selected for this task, you should:\n"
"1. **Use `web_deep_search`** to get information on how to implement this task\n"
"   - `web_deep_search` returns a simple answer (like `web_search`), no separate context\n"
"   - Example: `web_deep_search(query=\"how to create [task description]\", max_results=3)`\n"
"2. **Analyze the results** and understand the best approach\n"
"3. **Create a TODO list** with `set_todos` based on the research findings\n"
"4. **Then implement** the solution from scratch\n\n"
"**Example workflow:**\n"
"- `web_deep_search(query=\"Python script generate random lottery numbers save HTML\", max_results=3)`\n"
"- Analyze: \"I need to use random module, generate 6 unique numbers, create HTML structure\"\n"
"- `set_todos(tasks=[\"Generate 6 unique random numbers\", \"Create HTML structure\", \"Save to HTML file\"])`\n"
"- Start implementing")}

## QUALITY REQUIREMENTS
- **HTML**: Full structure, navigation, real content, min 500 bytes
- **CSS**: Reset, typography, colors, responsive, min 400 bytes
- **JavaScript**: Working code, error handling, min 100 bytes
- **Python**: Complete functionality, error handling, docstrings, min 100 bytes
- **Other files**: Complete, working code/content appropriate for the file type

## ASKING QUESTIONS TO MAIN AGENT
If you need information that's not in the task, you can ask the main agent questions!
Simply include your question in your response using this format:
❓ QUESTION: [Your question here]

Examples:
- ❓ QUESTION: What should the company name be?
- ❓ QUESTION: Which color scheme should I use for the website?
- ❓ QUESTION: What should the headline say?

The main agent will answer your questions based on the original user task and call you again with the information.
You can continue working once you have the answers.

## CURRENT TASK
You are working on a specific task from a TODO list. The TODO list has already been set.
Your current task is: **{current_task}**

{task_mgr.get_todos_for_prompt()}

Focus ONLY on completing this task. Use the necessary tools (read_file, write_file, etc.) to complete it.
When finished, call `task_done(summary="...")` to mark it complete and move to the next task."""
            
            # Build user message for this specific task
            fresh_user_msg = f"""## CURRENT TASK

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
            
            # Return fresh context (only system + user, no old history)
            return [
                {"role": "system", "content": fresh_system_prompt},
                {"role": "user", "content": fresh_user_msg}
            ]
        
        # Tools schema - includes TODO management tools
        tools_schema = [
            {
                "type": "function",
                "function": {
                    "name": "set_todos",
                    "description": "REQUIRED FIRST ACTION: Set your TODO list for this task. Call this FIRST with a list of specific subtasks.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tasks": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of specific tasks to complete, e.g. ['Create index.html', 'Add CSS styling', 'Test output']"
                            }
                        },
                        "required": ["tasks"]
                    }
                }
            },
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
                    "name": "read_file",
                    "description": "Read a file.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
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
            },
            {
                "type": "function",
                "function": {
                    "name": "python_sandbox",
                    "description": "Execute Python code safely in a sandboxed environment. Use for mathematical calculations, data processing, algorithms, and scientific computations.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": "Python code to execute (e.g., 'result = 2 + 2 * 3' or 'import math; print(math.sqrt(16))')"
                            }
                        },
                        "required": ["code"]
                    }
                }
            }
        ]
        
        # web_fetch tool for inspecting web pages
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
        
        # web_deep_search tool for finding solutions to errors or getting ideas
        tools_schema.append({
                    "type": "function",
                    "function": {
                "name": "web_deep_search",
                "description": "Deep search the web for solutions, error fixes, or ideas. Returns summarized results without bloating context. Use when you don't know how to fix an error or need inspiration.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query (e.g., 'Python TypeError fix', 'JavaScript async await best practices', 'CSS responsive design tutorial')"
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results to return (default: 5, max: 10)",
                            "default": 5
                        }
                            },
                            "required": ["query"]
                        }
                    }
        })
        
        # Git tools
        tools_schema.append({
            "type": "function",
            "function": {
                "name": "git_init",
                "description": "Initialize a Git repository in the project directory. Creates .git directory and .gitignore file.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        })
        
        tools_schema.append({
            "type": "function",
            "function": {
                "name": "git_add_commit",
                "description": "Add files to Git staging area and create a commit with a message. Use this to save your work progress.",
                "parameters": {
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
            }
        })
        
        tools_schema.append({
            "type": "function",
            "function": {
                "name": "git_status",
                "description": "Check the current Git status: shows modified, staged, and untracked files.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        })
        
        tools_schema.append({
            "type": "function",
            "function": {
                "name": "git_log",
                "description": "View the Git commit history. Shows recent commits with messages.",
                "parameters": {
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
                    }
        })
        
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
        
        # ═══════════════════════════════════════════════════════════════════
        # AGENTIC LOOP - NO MAX_STEPS!
        # ═══════════════════════════════════════════════════════════════════
        
        loop = AgenticLoop(timeout_minutes=15)
        files_created = list(template_files)  # Start with template files
        
        tui.set_action("Agentic Loop")
        tui.append_stream(f"{os.path.basename(base_dir)}")
        live.update(tui.render())
        
        while True:
            loop.increment_loop()
            tui.increment_loop()
            
            # Check if we should continue
            should_continue, reason = loop.should_continue()
            if not should_continue:
                break
            
            # Update TUI
            tui.set_action(f"Loop {loop.loop_count}")
            live.update(tui.render())
            
            # ═══════════════════════════════════════════════════════════════
            # CONTEXT MANAGEMENT - Prevent token overflow
            # ═══════════════════════════════════════════════════════════════
            
            # Proactive compression: Check token usage and compress if > 85% of limit
            estimated_tokens = context_manager.estimate_tokens(history)
            if estimated_tokens > int(context_manager.max_tokens * 0.85):
                tui.set_action(f"Proactive compression: {estimated_tokens}/{context_manager.max_tokens} tokens...")
                live.update(tui.render())
                history = context_manager.compress(history)
            # Also check normal threshold
            elif context_manager.should_compress(history):
                tui.set_action("Compressing context...")
                live.update(tui.render())
                history = context_manager.compress(history)
            tui.set_action(f"Loop {loop.loop_count}")
            live.update(tui.render())
            
            # ═══════════════════════════════════════════════════════════════
            # LLM REQUEST
            # ═══════════════════════════════════════════════════════════════
            
            # Get model name
            model_name = "user-model"
            try:
                m_res = requests.get("http://127.0.0.1:8080/v1/models", timeout=2)
                if m_res.status_code == 200:
                    data = m_res.json()
                    if 'data' in data and len(data['data']) > 0:
                        model_name = data['data'][0]['id']
            except:
                pass

            # Clean history - MUST be properly indented!
            clean_history = []
            for msg in history:
                clean_msg = {k: v for k, v in msg.items() if k in ['role', 'content', 'tool_calls', 'tool_call_id', 'name']}
                if clean_msg.get('content') is None:
                    clean_msg['content'] = ""
                # Skip empty messages with no content and no tool calls
                if not clean_msg.get('content') and not clean_msg.get('tool_calls'):
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
            # Only clear stream if buffer is empty or only has separator
            # This prevents clearing content that was just added
            with tui._lock:
                buffer_has_content = len(tui.stream_buffer) > 1 or (
                    len(tui.stream_buffer) == 1 and 
                    tui.stream_buffer[0] != "--- New response ---" and
                    not tui.stream_buffer[0].startswith("📝 Response at")
                )
                if not buffer_has_content:
                    # Only clear if buffer is empty or only has separator
                    tui.stream_buffer = []
                    tui.current_stream = ""
                    tui.current_line_buffer = ""
            
            tui.start_stream()  # Mark stream as active!
            # Only add separator if buffer is empty
            # Use timestamp instead of "--- New response ---"
            timestamp = time.strftime("%H:%M:%S")
            separator = f"📝 Response at {timestamp}"
            with tui._lock:
                if not tui.stream_buffer or tui.stream_buffer == ["--- New response ---"] or (len(tui.stream_buffer) == 1 and tui.stream_buffer[0].startswith("📝 Response at")):
                    tui.stream_buffer = [separator]
                else:
                    # Add separator before new content
                    tui.stream_buffer.append(separator)
            live.update(tui.render())
            
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
                stream_response = requests.post(
                    "http://127.0.0.1:8080/v1/chat/completions",
                    json={
                        "model": model_name,
                        "messages": clean_history,
                        "max_tokens": 8192,
                        "temperature": 0.3,
                        "tools": tools_schema,
                        "tool_choice": tool_choice,  # Dynamic: forced for set_todos, auto after
                        "stream": True  # STREAMING enabled!
                    },
                    timeout=180,
                    stream=True
                )
                
                # Handle Context Size Error (400) - automatically compress and retry
                if stream_response.status_code == 400:
                    try:
                        error_data = stream_response.json()
                        error_msg = error_data.get("error", {}).get("message", "")
                        if "exceed_context_size" in error_msg.lower() or "exceed" in error_msg.lower():
                            tui.set_action("Context size exceeded. Compressing...")
                            live.update(tui.render())
                            # Aggressively compress context
                            history = context_manager.compress(history)
                            # Also truncate old messages if still too large
                            if len(history) > 20:
                                # Keep system prompt, last user message, and last 10 messages
                                system_msgs = [m for m in history if m.get("role") == "system"]
                                user_msgs = [m for m in history if m.get("role") == "user"]
                                assistant_msgs = [m for m in history if m.get("role") == "assistant"]
                                
                                # Keep first system message, last user message, last 5 assistant messages
                                new_history = []
                                if system_msgs:
                                    new_history.append(system_msgs[0])  # Keep first system prompt
                                if user_msgs:
                                    new_history.append(user_msgs[-1])  # Keep last user message
                                if assistant_msgs:
                                    new_history.extend(assistant_msgs[-5:])  # Keep last 5 assistant messages
                                
                                history = new_history
                            tui.set_action(f"Compressed to {len(history)} messages. Retrying...")
                            live.update(tui.render())
                            # Retry the request with compressed context (continue loop)
                            continue
                    except (json.JSONDecodeError, KeyError, ValueError):
                        pass  # Not a context size error, fall through to normal error handling
                
                if stream_response.status_code != 200:
                    error_text = stream_response.text[:200] if stream_response.text else ""
                    return f"Error: Server {stream_response.status_code} - {error_text}"
                
                # Collect streamed response
                collected_content = ""
                collected_tool_calls = []
                current_line = ""  # Buffer for current line
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
                        
                        delta = choices[0].get('delta', {})
                        
                        # Stream content (agent's thoughts) - LIVE!
                        # Check both 'content' in delta and delta.get('content')
                        text_chunk = delta.get('content', '')
                        if text_chunk:
                            # Filter out redacted reasoning tags immediately
                            text_chunk = re.sub(r'</?redacted_reasoning>', '', text_chunk, flags=re.IGNORECASE)
                            text_chunk = re.sub(r'</?think>', '', text_chunk, flags=re.IGNORECASE)
                            
                            collected_content += text_chunk
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
                                    if 'name' in tc_delta['function']:
                                        tc['function']['name'] += tc_delta['function']['name']
                                        tui.set_action(f"Calling {tc['function']['name']}")
                                        live.update(tui.render())
                                    if 'arguments' in tc_delta['function']:
                                        tc['function']['arguments'] += tc_delta['function']['arguments']
                    
                    except json.JSONDecodeError:
                        continue
                
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
                                tui.append_stream(f"[FALLBACK] Extracted TODO list from markdown: {len(tasks)} tasks")
                    
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
                        tool_infos = []
                        for tc in tool_calls:
                            fn_name = tc.get('function', {}).get('name', 'unknown')
                            fn_args = tc.get('function', {}).get('arguments', '')
                            
                            # Try to parse arguments as JSON to show them nicely
                            try:
                                if fn_args:
                                    args_dict = json.loads(fn_args)
                                    # Format arguments nicely (truncate long values)
                                    args_str = ", ".join([
                                        f"{k}={str(v)[:50]}{'...' if len(str(v)) > 50 else ''}"
                                        for k, v in args_dict.items()
                                    ])
                                    tool_infos.append(f"🔧 {fn_name}({args_str})")
                                else:
                                    tool_infos.append(f"🔧 {fn_name}()")
                            except (json.JSONDecodeError, TypeError):
                                # If parsing fails, just show the raw arguments (truncated)
                                args_display = fn_args[:100] + "..." if len(fn_args) > 100 else fn_args
                                tool_infos.append(f"🔧 {fn_name}({args_display})")
                        
                        # Add each tool call as a separate line
                        for tool_info in tool_infos:
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
                
                # Final update to ensure everything is displayed
                live.update(tui.render())
                
            except requests.exceptions.ConnectionError:
                tui.end_stream()  # Mark stream as finished even on error
                return "Error: VAF Server offline."
            except Exception as e:
                tui.end_stream()  # Mark stream as finished even on error
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
                
                if is_effectively_empty:
                    # Log why we are restarting
                    tui.append_stream(f"Empty response detected (len={len(clean_content)}) - retrying...")
                
                # ═══════════════════════════════════════════════════════════════
                # NEW: Tool-Intent Detection (like main agent)
                # ═══════════════════════════════════════════════════════════════
                # Check if agent mentioned a tool name (but didn't actually call it yet)
                # This prevents the agent from getting stuck when it mentions a tool but doesn't call it
                # For tool-intent detection, use is_effectively_empty (checked BEFORE cleaning)
                if is_effectively_empty:
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
                        live.update(tui.render())
                        
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
                        
                        # Add a brief system prompt (will work better now because first thinking is preserved)
                        history.append({
                            "role": "system",
                            "content": "You didn't respond. Please answer or continue where you left off."
                        })
                        
                        # Continue the loop - if it fails again, this system message will be removed with the reset
                        continue
                
                if is_effectively_empty:
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
                    
                    # CRITICAL: If no TODOs set, nudge agent to set them FIRST!
                    # This is the most common cause of "doing nothing" - agent skips TODO setup
                    if not task_mgr.todos:
                        tui.set_action("No TODO list - nudging...")
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
                        continue
                    
                    # Add a brief system prompt (will be removed on next retry if needed)
                    history.append({
                        "role": "system",
                        "content": "You didn't respond. Please answer or continue where you left off."
                    })
                    
                    # Continue the loop (no retry limit - will loop until we get a response)
                    # If it fails again, this system message will be removed with the reset
                    continue
            
            # Only record activity if we have content or tool calls
            if tool_calls or (msg_content and msg_content.strip()):
                loop.record_activity()
            
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
            # CRITICAL CHECKS - RUN REGARDLESS OF msg_content
            # These checks ensure the agent doesn't skip work!
            # ═══════════════════════════════════════════════════════════════
            
            # FIRST: No TODOs set yet? Auto-generate or nudge
            if not task_mgr.todos and loop.loop_count >= 1:
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
                    
                    # Check for Script/Python task
                    is_script_task = any(kw in task_lower for kw in ['script', 'python', 'calculate', 'generate', 'tool', 'cli'])
                    
                    if is_content_only:
                        # ... (existing content only logic)
                        if "html" in task_lower or "webpage" in task_lower:
                            auto_todos = [
                                "Generate complete HTML document with all required content",
                                "Add embedded CSS styling",
                                "Verify HTML is complete and valid"
                            ]
                        else:
                            auto_todos = [
                                "Analyze task requirements",
                                "Generate requested content",
                                "Verify content is complete"
                            ]
                    # Python Script Task (Specific logic for scripts)
                    elif is_script_task and not is_content_only:
                         auto_todos = [
                            "Create main Python script",
                            "Implement core logic and functions",
                            "Add error handling and comments",
                            "Test script execution"
                        ]
                    # Detect if it's EXPLICITLY a multi-page website (not just "page" in text)
                    elif any(kw in task_lower for kw in ['multi-page', 'multiple pages', 'several pages', 'about page', 'contact page', 'services page', 'create pages']):
                        auto_todos = [
                            "Read and customize index.html (homepage)",
                            "Create additional pages (about, contact, services)",
                            "Update styles.css for all pages",
                            "Add navigation between pages",
                            "Test all pages and links"
                        ]
                    # Detect if it's a website project (but not CONTENT_ONLY)
                    elif any(kw in task_lower for kw in ['website', 'webseite', 'homepage', 'landing page']) and not is_content_only:
                        auto_todos = [
                            "Read and analyze existing template files (if any)",
                            "Create or customize index.html with task-specific content",
                            "Update styles.css with appropriate styling",
                            "Add JavaScript functionality if needed",
                            "Verify all files are complete and working"
                        ]
                    # Fallback for other tasks
                    else:
                        auto_todos = [
                            "Analyze task requirements",
                            "Create main files",
                            "Add styling or logic",
                            "Test and verify output"
                        ]
                    
                    # Set the auto-generated TODOs
                    task_mgr.set_todos(auto_todos)
                    tui.append_stream(f"Auto-generated {len(auto_todos)} tasks (model didn't call set_todos)")
                    for i, t in enumerate(auto_todos, 1):
                        tui.append_stream(f"   {i}. {t}")
                    live.update(tui.render())
                    
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
            
            # If model claims completion without TODOs, force it to set them first
            if completion_signals and not task_mgr.todos:
                tui.set_action("set_todos first")
                history.append({
                    "role": "system",
                    "content": "⚠️ You cannot complete yet. First call set_todos with your task breakdown, then work through each task and call task_done after each."
                })
                continue
            
            # Premature completion (said DONE but TODOs not finished)
            # CRITICAL: This check MUST run even if completion_signals is False, to catch any completion attempts
            if task_mgr.todos and not task_mgr.is_all_done():
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
            if msg_content and task_mgr.todos and not task_mgr.is_all_done():
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
            if task_mgr.todos and not task_mgr.is_all_done():
                remaining = [t["task"] for t in task_mgr.todos if t["status"] != "completed"]
                current = task_mgr.get_current_task()
                completed_count = len([t for t in task_mgr.todos if t["status"] == "completed"])
                total_count = len(task_mgr.todos)
                
                # Check if model is making progress on the current task
                # If no tool calls OR only read_file calls without write_file, nudge to continue
                has_write_file = any(tc.get('function', {}).get('name') == 'write_file' for tc in tool_calls) if tool_calls else False
                has_read_file = any(tc.get('function', {}).get('name') == 'read_file' for tc in tool_calls) if tool_calls else False
                only_reading = has_read_file and not has_write_file
                
                # Nudge if:
                # 1. No tool calls at all (idle)
                # 2. Only reading files but not writing (not making progress)
                # 3. Loop count >= 2 (gave model time to start)
                should_nudge = (
                    (not tool_calls or only_reading) and 
                    loop.loop_count >= 2 and
                    current
                )
                
                if should_nudge:
                    tui.set_action(f"{completed_count}/{total_count} tasks - Continue working!")
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
                continue
            
            # Only accept completion if:
            # 1. TODO list is done AND at least 3 loops completed (minimum work done)
            # 2. Must have actually used write_file (not just template files)
            min_loops_required = 3  # Require at least 3 loops to complete
            has_done_real_work = len(files_created) > len(template_files)  # Created more than just templates
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
                    
                    # Normal mode: Return project summary
                    files_list = _format_file_links(files_created, base_dir)
                    dir_link = _get_clickable_path(base_dir)
                    open_instructions = _get_open_instructions(files_created, base_dir)
                    
                    # Try to open the folder automatically
                    folder_opened = _open_folder(base_dir)
                    folder_status = "✅ Folder opened in file manager" if folder_opened else "📂 Folder ready"
                    
                    return (
                        f"### ✅ Task Completed\n\n"
                        f"**📁 Project Directory**: {dir_link}\n"
                        f"**Full Path**: `{base_dir}`\n"
                        f"{folder_status}\n\n"
                        f"**📄 Files ({len(files_created)})**:\n{files_list}\n\n"
                        f"{open_instructions}\n\n"
                        f"**⏱️ Time**: {loop.get_elapsed_str()}\n"
                        f"**🔄 Loops**: {loop.loop_count}\n\n"
                        f"**🔧 To continue working on this project, use:**\n"
                        f"`coding_agent(task=\"your task\", project_path=\"{base_dir}\")`"
                    )
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
            if msg_content:
                file_mentions = re.findall(r'[\w-]+\.(html|css|js|py|ts|json)', msg_content.lower())
                if file_mentions and len(files_created) <= len(template_files):
                    tui.set_action("Plan detected - forcing execution")
                    history.append({
                        "role": "system",
                        "content": f"⚠️ You mentioned files but didn't create them!\n"
                                   f"Files mentioned: {', '.join(file_mentions[:5])}\n"
                                   f"Call write_file NOW."
                    })
                    continue
            
            # ═══════════════════════════════════════════════════════════════
            # EXECUTE TOOL CALLS
            # ═══════════════════════════════════════════════════════════════

            # Track repeated errors to prevent infinite loops
            if not hasattr(loop, 'error_history'):
                loop.error_history = []

            for tc in tool_calls:
                fn_name = tc['function']['name']
                fn_args_str = tc['function']['arguments']
                
                try:
                    fn_args = json.loads(fn_args_str)
                except:
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
                
                # ===== TODO MANAGEMENT TOOLS =====
                elif fn_name == "set_todos":
                    tasks = fn_args.get("tasks", [])
                    
                    # CRITICAL: Prevent resetting TODOs if work has already started!
                    if task_mgr.todos and task_mgr.current_task_idx > 0:
                        result = (
                            f"⚠️ REJECTED: You are already working on Task {task_mgr.current_task_idx + 1}/{len(task_mgr.todos)}!\n"
                            f"Current task: {task_mgr.get_current_task()}\n\n"
                            f"You cannot reset the TODO list in the middle of execution.\n"
                            f"Finish your current tasks using `task_done`."
                        )
                        tui.append_stream("set_todos rejected - work in progress!")
                    elif tasks:
                        task_mgr.set_todos(tasks)
                        tui.set_action(f"TODO: {len(tasks)} tasks")
                        tui.append_stream(f"Created TODO list with {len(tasks)} tasks")
                        for i, t in enumerate(tasks[:5], 1):
                            tui.append_stream(f"   {i}. {t[:50]}")
                        if len(tasks) > 5:
                            tui.append_stream(f"   ... and {len(tasks)-5} more")
                        result = f"✅ TODO list set: {len(tasks)} tasks. Current: {task_mgr.get_current_task()}"
                    else:
                        result = "⚠️ No tasks provided!"
                        
                elif fn_name == "task_done":
                    # Track consecutive task_done calls for dead loop detection
                    if not hasattr(loop, 'consecutive_task_done'):
                        loop.consecutive_task_done = 0
                    loop.consecutive_task_done += 1
                    
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
                        
                        # Normal mode: Return project summary
                        files_list = _format_file_links(files_created, base_dir)
                        dir_link = _get_clickable_path(base_dir)
                        open_instructions = _get_open_instructions(files_created, base_dir)
                            
                        # Try to open the folder automatically
                        folder_opened = _open_folder(base_dir)
                        folder_status = "✅ Folder opened in file manager" if folder_opened else "📂 Folder ready"
                        
                        # Check for placeholders
                        placeholder_check = QualityChecker.check_placeholders(files_created, base_dir)
                        placeholder_warning = ""
                        if placeholder_check['has_placeholders']:
                            placeholder_warning = "\n\n### ⚠️ Unchanged Placeholders Found\n"
                            for fname, placeholders in placeholder_check['files_with_placeholders'].items():
                                placeholder_warning += f"\n**{fname}**:\n"
                                for p in placeholders[:5]:
                                    placeholder_warning += f"- {p}\n"
                        
                        return (
                                f"### ✅ Task Completed\n\n"
                                f"**📁 Project Directory**: {dir_link}\n"
                                f"**Full Path**: `{base_dir}`\n"
                                f"{folder_status}\n\n"
                                f"**📄 Files ({len(files_created)})**:\n{files_list}\n\n"
                                f"{open_instructions}\n\n"
                                f"**⏱️ Time**: {loop.get_elapsed_str()}\n"
                                f"**🔄 Loops**: {loop.loop_count}\n\n"
                                f"**🔧 To continue working on this project, use:**\n"
                                f"`coding_agent(task=\"your task\", project_path=\"{base_dir}\")`"
                            f"{placeholder_warning}"
                        )
                    
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
                    else:
                        current = task_mgr.get_current_task()
                        
                        # VALIDATE: Check if current task is actually done
                        # For file-related tasks, check if files were created/modified
                        task_lower = current.lower() if current else ""
                        is_create_task = any(kw in task_lower for kw in ['create', 'generate', 'write', 'build', 'implement', 'make'])
                        
                        # CRITICAL: If task is to create something, but no files were created -> BLOCK
                        # We check if ANY files were created in the whole session (files_created)
                        # Ideally we should check if files were created *during this task*, but checking total is a good baseline safe-guard against "lazy" agents
                        if is_create_task and not files_created:
                            result = (
                                f"⚠️ ERROR: You claimed to complete '{current}', but you haven't created any files!\n\n"
                                f"For a task like '{current}', you MUST call `write_file` to generate the content.\n"
                                f"Calling `task_done` alone does NOT create files.\n\n"
                                f"**Action required:**\n"
                                f"1. Call `write_file(path='...', content='...')` with the code\n"
                                f"2. THEN call `task_done`"
                            )
                            tui.append_stream(f"task_done blocked - no files created!")
                            tui.set_action("Waiting for write_file...")
                        
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
                                # Find template file path for the error message
                                template_path_hint = ""
                                if template_files and recent_files:
                                    for tf in template_files:
                                        if any(os.path.basename(tf) == os.path.basename(f) for f in recent_files):
                                            template_path_hint = tf
                                            break
                                
                                result = (
                                    f"🚨 TASK NOT COMPLETE!\n\n"
                                    f"Task: {current}\n\n"
                                    f"**Problem:** Template structure was destroyed!\n"
                                    f"You removed essential elements from the template:\n\n" +
                                    "\n".join(f"- {elem}" for elem in missing_template_elements) +
                                    f"\n\n**Action required:**\n"
                                    f"1. Read the original template file: `read_file(path=\"{template_path_hint}\")`\n"
                                    f"2. Restore ALL template sections (nav, hero, services, about, contact, footer)\n"
                                    f"3. Only replace {{PLACEHOLDER}} text with real content\n"
                                    f"4. Keep ALL classes, IDs, and structural elements\n"
                                    f"5. Write back the corrected file\n"
                                    f"6. THEN call task_done again\n\n"
                                    f"DO NOT call task_done until template structure is fully preserved!"
                                )
                                tui.append_stream(f"{current[:40]} - template structure destroyed!")
                                tui.set_action(f"{task_mgr.get_progress()} - Fix template!")
                            elif placeholder_check['has_placeholders']:
                                # Task not really done - placeholders still present
                                placeholder_details = []
                                files_list = []
                                
                                # Detect task type and extract context
                                task_lower = task.lower()
                                task_type = "project"  # generic default
                                task_context = ""
                                
                                # Detect task type
                                if any(kw in task_lower for kw in ["website", "webseite", "webpage", "landing"]):
                                    task_type = "website"
                                    # Extract business type for websites
                                    task_words = task_lower.split()
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
                                task_mgr.complete_current_task(summary)
                                next_task = task_mgr.get_current_task()
                                
                                tui.append_stream(f"Completed: {current[:40] if current else 'task'}")
                                tui.set_action(f"{task_mgr.get_progress()}")
                                
                                # ═══════════════════════════════════════════════════════════════
                                # CREATE FRESH CONTEXT FOR NEW TASK - Isolated context per task
                                # ═══════════════════════════════════════════════════════════════
                                if next_task:
                                    # Create completely fresh context for the new task
                                    # This isolates each task with its own context, preventing confusion
                                    history = create_fresh_context_for_task(next_task)
                                    history_snapshot_len = len(history)  # Update snapshot for new context
                                    tui.append_stream("Fresh context created for new task")
                                
                                if next_task:
                                    result = f"✅ Task completed!\n\n## NEXT TASK:\n{next_task}\n\nFocus only on this task now."
                                    tui.append_stream(f"Next: {next_task[:40]}")
                                elif task_mgr.is_all_done():
                                    result = "🎉 ALL TASKS COMPLETED! Verify your work and say 'ALL TASKS COMPLETED'."
                                    tui.append_stream("All tasks done!")
                                else:
                                    result = "✅ Task completed. Continue with remaining work."
                        else:
                            # Non-file task or no files created yet - allow completion
                            summary = fn_args.get("summary", "done")
                            task_mgr.complete_current_task(summary)
                            next_task = task_mgr.get_current_task()
                            
                            tui.append_stream(f"✅ Completed: {current[:40] if current else 'task'}")
                            tui.set_action(f"📋 {task_mgr.get_progress()}")
                            
                            # ═══════════════════════════════════════════════════════════════
                            # CREATE FRESH CONTEXT FOR NEW TASK - Isolated context per task
                            # ═══════════════════════════════════════════════════════════════
                            if next_task:
                                # Create completely fresh context for the new task
                                # This isolates each task with its own context, preventing confusion
                                history = create_fresh_context_for_task(next_task)
                                history_snapshot_len = len(history)  # Update snapshot for new context
                                tui.append_stream("🔄 Fresh context created for new task")
                            
                            if next_task:
                                result = f"✅ Task completed!\n\n## NEXT TASK:\n{next_task}\n\nFocus only on this task now."
                                tui.append_stream(f"➡️ Next: {next_task[:40]}")
                            elif task_mgr.is_all_done():
                                result = "🎉 ALL TASKS COMPLETED! Verify your work and say 'ALL TASKS COMPLETED'."
                                tui.append_stream("🎉 All tasks done!")
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
                
                elif fn_name == "web_deep_search":
                    query = fn_args.get("query", "")
                    max_results = min(fn_args.get("max_results", 5), 10)  # Max 10 results
                    tui.set_action(f"🔍 Deep search: {query[:40]}...")
                    live.update(tui.render())
                    
                    try:
                        # Try to import DuckDuckGo search
                        try:
                            from ddgs import DDGS
                        except ImportError:
                            try:
                                from duckduckgo_search import DDGS
                            except ImportError:
                                result = "Error: DuckDuckGo search not available. Install with: pip install duckduckgo-search"
                                tui.append_stream("❌ Search tool not available")
                                continue
                        
                        # Perform search
                        results = list(DDGS().text(query, max_results=max_results, safesearch='strict'))
                        if not results:
                            result = f"No results found for: {query}"
                            tui.append_stream("No results")
                            continue
                        
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
                    except Exception as e:
                        result = f"Error during deep search: {e}"
                        tui.append_stream(f"❌ Search failed: {str(e)[:30]}")
                
                elif fn_name in self.local_tools:
                    tool = self.local_tools[fn_name]
                    
                    # Fix relative paths and show in stream
                    if fn_name == "write_file" and "path" in fn_args:
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
                        
                        if not os.path.isabs(fn_args["path"]):
                            fn_args["path"] = os.path.join(base_dir, fn_args["path"])
                        
                        fname = os.path.basename(fn_args["path"])
                        path = fn_args["path"]
                        
                        # Check if this is a template file being overwritten
                        is_template_file = any(path == tf or os.path.basename(path) == os.path.basename(tf) for tf in template_files)
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
                                        # BLOCK the write - template structure not preserved
                                        result = (
                                            f"🚨 BLOCKED: Template structure destroyed!\n\n"
                                            f"You tried to overwrite template file {fname}, but removed essential elements:\n" +
                                            "\n".join(f"- {elem}" for elem in missing_elements) +
                                            f"\n\n**MANDATORY:**\n"
                                            f"1. Read template FIRST: `read_file(path=\"{path}\")`\n"
                                            f"2. Keep ALL sections (nav, hero, services, about, contact, footer)\n"
                                            f"3. Only replace {{PLACEHOLDER}} text with real content\n"
                                            f"4. Do NOT remove sections, classes, or IDs\n\n"
                                            f"**Example:** Template has `<nav class=\"nav\">` → Keep it! Only replace {{BUSINESS_NAME}}.\n\n"
                                            f"DO NOT rewrite from scratch - work WITH the template!"
                                        )
                                        tui.append_stream(f"BLOCKED: Template structure destroyed in {fname}")
                                        tui.set_action(f"⚠️ Fix template preservation!")
                                        history.append({
                                            "role": "system",
                                            "content": result
                                        })
                                        # Add as tool result so agent gets feedback
                                        history.append({
                                            "role": "tool",
                                            "tool_call_id": tc['id'],
                                            "name": fn_name,
                                            "content": result
                                        })
                                        continue  # Skip this write_file call
                                    else:
                                        # Structure preserved - allow write but warn
                                        tui.append_stream(f"Template structure preserved in {fname}")
                            except Exception as e:
                                # If validation fails, still warn but allow (graceful degradation)
                                tui.append_stream(f"Could not validate template: {e}")
                            
                            # Warn if template file is being overwritten
                            tui.append_stream(f"Overwriting template file {fname}")
                            tui.append_stream("   Make sure you read it first and preserve the structure!")
                            live.update(tui.render())
                        
                        tui.add_file(fname, 0, "writing")
                        tui.set_action(f"📝 Writing: {fname}")
                        
                        # Update Code Preview Panel
                        tui.set_code_preview(fname, fn_args.get("content", ""), "code")

                        # Show code preview (first 5 lines) - DON'T clear stream!
                        code_content = fn_args.get("content", "")
                        tui.append_stream(f"Creating {fname}:")
                        code_lines = code_content.split('\n')[:5]
                        for line in code_lines:
                            tui.append_stream(line[:65] if len(line) <= 65 else line[:62] + "...")
                        if len(code_content.split('\n')) > 5:
                            tui.append_stream("...")
                        
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
                    
                    try:
                        result = tool.run(**fn_args)
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
                        
                        # Track created files
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
                                files_created.append(path)
                                tui.update_file(os.path.basename(path), "done", size)
                                tui.append_stream(f"{os.path.basename(path)} ({size}B)")
                                result = f"✓ Created {path} ({size} bytes)"
                                
                                # Automatically run linter after successful write_file
                                try:
                                    linter_tool = self.local_tools.get("linter")
                                    if linter_tool:
                                        tui.set_action("🔍 Linting...")
                                        live.update(tui.render())
                                        lint_result = linter_tool.run(path=path)
                                        
                                        # If linter found issues, add them to history for agent to see
                                        if lint_result and not lint_result.startswith("✓") and not lint_result.startswith("[INFO]"):
                                            # Issues found - add as system message so agent can fix them
                                            lint_msg = (
                                                f"🔍 Linter checked {os.path.basename(path)} and found issues:\n\n"
                                                f"{lint_result}\n\n"
                                                f"**Action required:** Review and fix the issues above before completing the task."
                                            )
                                            history.append({
                                                "role": "system",
                                                "content": lint_msg
                                            })
                                            tui.append_stream(f"Linter found issues in {os.path.basename(path)}")
                                        elif lint_result.startswith("✓"):
                                            tui.append_stream(f"✓ Linter: No issues")
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
                
                # Add result to history
                history.append({
                    "role": "tool",
                    "tool_call_id": tc['id'],
                    "name": fn_name,
                    "content": result_str[:3000]
                })

        # ═══════════════════════════════════════════════════════════════════
        # LOOP ENDED (timeout or max empty)
        # ═══════════════════════════════════════════════════════════════════
        
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
            if task_mgr and task_mgr.todos:
                completed_count = len([t for t in task_mgr.todos if t["status"] == "completed"])
                total_count = len(task_mgr.todos)
                remaining = [t["task"] for t in task_mgr.todos if t["status"] != "completed"]
                task_status = f"\n\n**📋 Task Status**: {completed_count}/{total_count} tasks completed"
                if remaining:
                    has_incomplete_tasks = True
                    task_status += f"\n**⚠️ Remaining tasks ({len(remaining)})**:\n" + "\n".join(f"- {t}" for t in remaining[:5])
                    if len(remaining) > 5:
                        task_status += f"\n- ... and {len(remaining) - 5} more"
                    task_status += f"\n\n**💡 To complete all tasks, continue with:**\n"
                    task_status += f"`coding_agent(task=\"complete all remaining tasks\", project_path=\"{base_dir}\")`"
            
            # Determine completion status
            if has_incomplete_tasks:
                completion_header = "### ⚠️ Task Partially Complete"
                completion_note = "**Note**: Loop ended before all tasks were completed. Please continue to finish all remaining tasks."
            else:
                completion_header = "### ✅ Task Completed"
                completion_note = "**Note**: All tasks have been completed. Review files for any final adjustments."
            
            return (
                f"{completion_header}\n\n"
                f"**📁 Project Directory**: {dir_link}\n"
                f"**Full Path**: `{base_dir}`\n"
                f"{folder_status}\n\n"
                f"**📄 Files ({len(files_created)})**:\n{files_list}\n\n"
                f"{open_instructions}\n\n"
                f"**⏱️ Time**: {loop.get_elapsed_str()}\n"
                f"**🔄 Loops**: {loop.loop_count}\n"
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
                f"### ❌ Task Failed\n\n"
                f"**📁 Project Directory**: {dir_link}\n"
                f"**Full Path**: `{base_dir}`\n"
                f"{folder_status}\n\n"
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
# TUI BUG FIX DOCUMENTATION - Multiple Empty Header Boxes Issue
# ═══════════════════════════════════════════════════════════════════════════════
#
# PROBLEM:
# Multiple empty "Collaboration Mode Active" header boxes were appearing in the TUI,
# especially before the actual content was displayed. This happened because:
#
# 1. AnimatedHeader.__rich__() creates a new Panel object every time it's called
# 2. render() was being called multiple times (by animation_updater thread at 15 FPS)
# 3. Header was being rendered before Live.start() was called
# 4. Race conditions between render() calls and Live initialization
# 5. Rich's Live may output initial_render before live.start() fully takes over
# 6. Multiple coding_agent tool calls could create multiple TUI instances
#
# SYMPTOMS:
# - Multiple empty header boxes appearing before actual content
# - Header boxes appearing even when _live_started was False
# - Header boxes appearing between "Debug" messages (especially "Summarizing intel...")
# - Header boxes appearing when coding_agent is called multiple times in quick succession
#
# ROOT CAUSE:
# The header was being rendered in render() even when:
# - Live hadn't started yet (initial_render = tui.render() before live.start())
# - No actual content was present (just placeholder actions like "Initializing...")
# - Multiple render() calls happened in quick succession
# - Rich's Live system output initial_render before fully taking over
# - Multiple coding_agent tool calls created multiple CoderTUI instances, each trying to render headers
#
# SOLUTION (FINAL):
# 1. Added _live_started flag to track when Live is actually running
# 2. Added _header_visible flag for sticky header (once shown, stays visible to prevent flickering)
# 3. Set _live_started = True ONLY AFTER live.start() (prevents race conditions)
# 4. Check _live_started FIRST in render() - if False, return header = None immediately
# 5. Only check for content if _live_started is True
# 6. Only render header when BOTH conditions are met: Live is running AND content exists
# 7. Force first update AFTER _live_started = True: live.update(tui.render()) after live.start()
# 8. Removed all manual stop_live() calls - cleanup happens at end of run() method
# 9. Header is sticky: once _header_visible = True, header stays visible even if content temporarily disappears
# 10. Added instance lock mechanism to prevent multiple coding_agent instances running simultaneously
# 11. Stop previous instance when new one starts (prevents multiple headers from concurrent calls)
#
# KEY CHANGES:
# - In CodingAgentTool class (line ~1160): Added _instance_lock and _active_instance for singleton-like behavior
# - In __init__ (line ~185): Added _header_visible = False flag
# - In render() method (line ~337): Check _live_started first, then content, use sticky flag
# - In run() method (line ~1306): Stop previous instance if active before starting new one
# - In run() method (line ~1330): Set _live_started = True AFTER live.start()
# - In run() method (line ~1335): Force first update AFTER _live_started = True
# - In run() method (line ~4357): Cleanup stop_live() at end of method
# - In run() method (line ~4371): Clear active instance when done
# - Content check excludes placeholder actions: "Initializing...", "Starting...", "Ready"
# - Removed all manual stop_live() calls throughout the method
#
# HOW TO FIX SIMILAR ISSUES:
# 1. Always check if Live is running before rendering animated components
# 2. Set flags AFTER critical operations (e.g., after live.start(), not before)
# 3. Use early returns to prevent unnecessary rendering
# 4. Cache rendered components if they don't need animation, or render fresh if they do
# 5. Be careful with threading - animation_updater calls render() 15x/sec
# 6. Test with initial_render to ensure no components render before Live takes over
# 7. Force first update AFTER Live is fully started to ensure proper initialization
# 8. Use sticky flags for components that should remain visible once shown
# 9. Ensure cleanup happens at end of method, not scattered throughout
# 10. If tool can be called multiple times, use instance lock to prevent concurrent execution
# 11. Stop previous instance cleanly when new one starts (set _live_started = False on old instance)
#
# LESSONS LEARNED:
# - Rich's Live system needs to be fully initialized before rendering animated components
# - Race conditions can occur between render() calls and Live initialization
# - Always check state flags in the correct order (most restrictive first)
# - AnimatedHeader creates new Panel objects - be careful with multiple render() calls
# - Rich's Live may output initial_render before live.start() fully takes over
# - Force first update after live.start() to ensure header appears only when Live is ready
# - Sticky flags prevent flickering when content temporarily disappears
# - Cleanup should be centralized at end of method, not scattered
# - Multiple tool calls can create multiple TUI instances - use instance lock to prevent this
# - Stopping previous instance (_live_started = False) prevents zombie headers
#
# KNOWN LIMITATIONS:
# - If coding_agent is called multiple times in quick succession, the previous instance
#   is stopped before the new one starts, which may cause a brief flicker
# - The instance lock prevents concurrent execution, so only one coding_agent can run at a time
#
# POTENTIAL FUTURE IMPROVEMENTS:
# - Consider queuing multiple tool calls instead of stopping previous instance
# - Add visual indicator when previous instance is being stopped
# - Delay header rendering until first content update after Live starts (already implemented)
#
# RELATED FILES:
# - vaf/tools/coder.py: CoderTUI.render() method (line ~308)
# - vaf/tools/coder.py: CoderTUI.__init__() method (line ~183)
# - vaf/tools/coder.py: CodingAgentTool.run() method (line ~1300)
# - vaf/cli/tui.py: AnimatedHeader class (line ~693)
# - vaf/tools/research_agent.py: ResearchTUI (reference implementation that works)
#
# Date: Fixed after multiple attempts - final solution uses:
#   - _live_started flag check
#   - _header_visible sticky flag
#   - Force update after live.start()
#   - Centralized cleanup at end of method
#   - Instance lock mechanism to prevent multiple concurrent instances
#   - Stop previous instance when new one starts
# ═══════════════════════════════════════════════════════════════════════════════
