"""
VAF TUI - Modern Terminal User Interface
Beautiful input box, panels, and interactive elements
"""
import os
import sys
import time
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich.live import Live
from rich.layout import Layout
from rich.style import Style
from rich.box import ROUNDED, HEAVY, DOUBLE, MINIMAL
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.table import Table
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion, PathCompleter, WordCompleter, merge_completers
from prompt_toolkit.styles import Style as PTStyle
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.formatted_text import HTML
from pathlib import Path

from vaf.cli.autosuggest import create_autosuggest, CombinedAutoSuggest
from typing import Optional, Callable
from datetime import datetime

from vaf.cli.themes import ThemeManager

# ═══════════════════════════════════════════════════════════════════════════════
# BOX STYLES (using built-in Rich boxes)
# ═══════════════════════════════════════════════════════════════════════════════

# Use built-in boxes from Rich
MODERN_BOX = ROUNDED  # Nice rounded corners
INPUT_BOX = ROUNDED   # For input fields

# ═══════════════════════════════════════════════════════════════════════════════
# TUI CLASS
# ═══════════════════════════════════════════════════════════════════════════════

class TUI:
    """Modern Terminal User Interface for VAF."""
    
    def __init__(self, theme: str = None):
        self.theme_name = theme or ThemeManager.current()
        self.theme = ThemeManager.get_theme(self.theme_name)
        self.console = Console(
            force_terminal=True,
            color_system="truecolor"
        )
        self._setup_history()
        self._setup_autosuggest()
    
    def _setup_history(self):
        """Setup command history."""
        history_dir = Path.home() / ".vaf"
        history_dir.mkdir(exist_ok=True)
        self.history_file = history_dir / "history"
    
    def _setup_autosuggest(self):
        """Setup smart autosuggest."""
        history_dir = Path.home() / ".vaf"
        self.autosuggest = create_autosuggest(history_dir / "autosuggest.json")
    
    @property
    def primary(self) -> str:
        return self.theme["primary"]
    
    @property
    def secondary(self) -> str:
        return self.theme["secondary"]
    
    @property
    def accent(self) -> str:
        return self.theme["accent"]
    
    @property
    def text_color(self) -> str:
        return self.theme["text"]
    
    @property
    def muted(self) -> str:
        return self.theme["text_muted"]
    
    @property
    def border_color(self) -> str:
        return self.theme["border"]
    
    @property
    def bg(self) -> str:
        return self.theme["background"]
    
    # ═══════════════════════════════════════════════════════════════════════════
    # LOGO & BRANDING
    # ═══════════════════════════════════════════════════════════════════════════
    
    def logo(self, subtitle: str = None):
        """Display the VAF logo."""
        logo_art = f"""
[{self.primary}]
O))         O))       O))))))))
 O))       O))))      O))      
  O))     O))  O))    O))      
   O))   O))    O))   O))))))  
    O)) O)) )))) O))  O))      
     O))))        O)) O))       
      O))          O))O))      (OO ) 
[/{self.primary}][{self.muted}]文 Veyllo Agentic Framework [/{self.muted}]"""
        
        self.console.print(Align.center(logo_art.strip()))
        
        if subtitle:
            self.console.print(Align.center(f"[{self.muted}]{subtitle}[/{self.muted}]"))
        
        self.console.print()
        # Show shortcuts bar
        shortcuts = f"[{self.muted}][bold]S[/bold] Settings  [bold]C[/bold] Model  [bold]T[/bold] Theme  [bold]H[/bold] History  [bold]?[/bold] Help  [bold]/exit[/bold] Quit[/{self.muted}]"
        self.console.print(shortcuts, justify="center")
        self.console.print()
    
    def logo_minimal(self):
        """Display a minimal logo."""
        self.console.print(f"[{self.primary} bold]文 VAF[/{self.primary} bold] [{self.muted}]• Veyllo Agentic Framework[/{self.muted}]")
        self.console.print()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # INPUT BOX
    # ═══════════════════════════════════════════════════════════════════════════
    
    def input_box(
        self, 
        prompt: str = "Message", 
        placeholder: str = "Type your message...",
        multiline: bool = False
    ) -> Optional[str]:
        """
        Modern input box with styling.
        Returns user input or None if cancelled.
        """
        # Build completer with cross-platform path support
        class VAFCompleter(Completer):
            def __init__(self, tui):
                self.tui = tui
                self.path_completer = PathCompleter(expanduser=True)
                # Complete list of all available commands (without / prefix for completer)
                self.all_commands = [
                    'exit', 'quit', 'q', 'clear', 'settings', 'model', 'help',
                    'session', 'theme', 'undo', 'history', 'export', 'tools',
                    'restore', 'context'  # Context management
                ]
                # Use FuzzyCompleter for better matching (finds "settings" when typing "s")
                from prompt_toolkit.completion import FuzzyCompleter
                self.cmd_completer = FuzzyCompleter(
                    WordCompleter(self.all_commands, ignore_case=True),
                    WORD=False  # Allow partial word matching
                )
                
                # Cross-platform quick paths
                home = Path.home()
                self.quick_paths = {
                    "~": str(home),
                    "~/": str(home) + os.sep,
                    "desktop": str(home / "Desktop"),
                    "downloads": str(home / "Downloads"),
                    "documents": str(home / "Documents"),
                    "pictures": str(home / "Pictures"),
                    "videos": str(home / "Videos"),
                    "music": str(home / "Music"),
                    "home": str(home),
                    ".": str(Path.cwd()),
                    "./": str(Path.cwd()) + os.sep,
                    "..": str(Path.cwd().parent),
                    "../": str(Path.cwd().parent) + os.sep,
                }
                
                # Platform-specific additions
                if sys.platform == "win32":
                    # Windows: Add drive letters
                    for letter in "CDEFGH":
                        drive = f"{letter}:"
                        if Path(f"{drive}/").exists():
                            self.quick_paths[drive.lower()] = f"{drive}\\"
                            self.quick_paths[drive] = f"{drive}\\"
            
            def get_completions(self, document, complete_event):
                text = document.text_before_cursor
                
                # File path completion (triggered by @)
                if "@" in text:
                    start_pos = text.rfind("@")
                    path_text = text[start_pos+1:].strip()
                    
                    # Quick path shortcuts (show when @ is typed with little/no text)
                    if len(path_text) < 3:
                        for shortcut, full_path in self.quick_paths.items():
                            if shortcut.startswith(path_text.lower()):
                                # Show shortcut with description
                                display = f"{shortcut} → {full_path}"
                                yield Completion(
                                    full_path,
                                    start_position=-len(path_text),
                                    display=display,
                                    display_meta="Quick Path"
                                )
                    
                    # Expand ~ to home directory
                    if path_text.startswith("~"):
                        path_text = str(Path.home()) + path_text[1:]
                    
                    # Handle Windows drive letters (C:, D:, etc.)
                    if sys.platform == "win32" and len(path_text) >= 2:
                        if path_text[1] == ":" and not path_text.endswith(os.sep):
                            if len(path_text) == 2:
                                path_text += os.sep
                    
                    # Use PathCompleter for actual path completion
                    from prompt_toolkit.document import Document
                    dummy_doc = Document(path_text, cursor_position=len(path_text))
                    
                    for c in self.path_completer.get_completions(dummy_doc, complete_event):
                        # Enhance display with file/folder indicators
                        try:
                            full_path = Path(path_text) / c.text if path_text else Path(c.text)
                            if not full_path.is_absolute():
                                full_path = Path.cwd() / full_path
                            
                            if full_path.exists():
                                if full_path.is_dir():
                                    display = f"📁 {c.text}"
                                    meta = "Folder"
                                else:
                                    # Show file size
                                    size = full_path.stat().st_size
                                    if size < 1024:
                                        size_str = f"{size}B"
                                    elif size < 1024*1024:
                                        size_str = f"{size//1024}KB"
                                    else:
                                        size_str = f"{size//(1024*1024)}MB"
                                    display = f"📄 {c.text}"
                                    meta = size_str
                            else:
                                display = c.text
                                meta = ""
                        except:
                            display = c.text
                            meta = ""
                        
                        yield Completion(
                            c.text,
                            start_position=c.start_position,
                            display=display,
                            display_meta=meta
                        )
                
                # Command completion (triggered by /)
                elif text.startswith("/"):
                    # Extract the command part (everything after /)
                    cmd_text = text[1:].strip()
                    
                    # Create a document with just the command part (without /)
                    from prompt_toolkit.document import Document
                    cmd_doc = Document(cmd_text, cursor_position=len(cmd_text))
                    
                    # Get completions from fuzzy completer (works with partial matches like "s" -> "settings")
                    for c in self.cmd_completer.get_completions(cmd_doc, complete_event):
                        # Calculate start position: replace everything after / with the completion
                        # If cmd_text is "s" and completion is "settings", we want to replace "s" with "settings"
                        # start_position should be negative to replace from cursor backwards
                        start_pos = c.start_position if c.start_position < 0 else -len(cmd_text)
                        
                        # Add the / prefix back and adjust start position
                        yield Completion(
                            c.text,
                            start_position=start_pos,
                            display=f"/{c.text}",
                            display_meta=c.display_meta or "Command"
                        )
        
        # Custom key bindings
        kb = KeyBindings()
        
        @kb.add(Keys.Tab)
        def _(event):
            b = event.current_buffer
            # First try to accept auto-suggestion
            if b.suggestion:
                b.insert_text(b.suggestion.text)
            # Then try completion menu
            elif b.complete_state:
                b.complete_next()
            else:
                b.start_completion(select_first=False)
        
        @kb.add(Keys.Right)
        def _(event):
            b = event.current_buffer
            # Accept suggestion with right arrow at end of line
            if b.suggestion and b.cursor_position == len(b.text):
                b.insert_text(b.suggestion.text)
            else:
                # Normal right arrow behavior
                b.cursor_right()
        
        @kb.add(Keys.Escape)
        def _(event):
            event.app.exit(result=None)
        
        if multiline:
            @kb.add(Keys.ControlJ)  # Ctrl+Enter to submit in multiline
            def _(event):
                event.current_buffer.validate_and_handle()
        
        # Prompt toolkit style matching theme
        pt_style = PTStyle.from_dict({
            'prompt': self.primary,
            'bottom-toolbar': f'bg:{self.theme["background_panel"]} {self.muted}',
        })
        
        # Create session with smart autosuggest
        try:
            session = PromptSession(
                history=FileHistory(str(self.history_file)),
                auto_suggest=self.autosuggest,  # Smart inline suggestions
                completer=VAFCompleter(self),
                style=pt_style,
                key_bindings=kb,
                complete_while_typing=True,
                multiline=multiline,
            )
            
            # Print the input box header
            self._print_input_header(prompt)
            
            # Get input
            result = session.prompt(
                HTML(f'<style fg="{self.primary}">❯</style> '),
                placeholder=HTML(f'<style fg="{self.muted}">{placeholder}</style>'),
            )
            
            # Learn from user input for better future suggestions
            if result:
                self.autosuggest.add_to_history(result)
            
            return result
            
        except KeyboardInterrupt:
            return None
        except EOFError:
            return None
    
    def _print_input_header(self, prompt: str):
        """Print the input box header."""
        # Top border with prompt label
        width = self.console.width - 4
        label = f" {prompt} "
        
        # Calculate border parts
        label_len = len(label)
        left_border = "─" * 2
        right_border = "─" * (width - label_len - 4)
        
        header = f"╭{left_border}[{self.primary}]{label}[/{self.primary}]{right_border}╮"
        self.console.print(header, style=self.border_color)
    
    def input_simple(self, prompt: str = "vaf> ") -> Optional[str]:
        """Simple inline input without box."""
        try:
            session = PromptSession(
                history=FileHistory(str(self.history_file)),
                auto_suggest=self.autosuggest,  # Smart inline suggestions
            )
            result = session.prompt(HTML(f'<style fg="{self.primary}">{prompt}</style>'))
            if result:
                self.autosuggest.add_to_history(result)
            return result
        except (KeyboardInterrupt, EOFError):
            return None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # PANELS & BOXES
    # ═══════════════════════════════════════════════════════════════════════════
    
    def panel(
        self, 
        content: str, 
        title: str = None, 
        style: str = "primary",
        expand: bool = True
    ):
        """Display content in a styled panel."""
        color = getattr(self, style, self.primary)
        
        panel = Panel(
            content,
            title=f"[bold]{title}[/bold]" if title else None,
            title_align="left",
            border_style=color,
            box=ROUNDED,
            expand=expand,
            padding=(0, 1),
        )
        self.console.print(panel)
    
    def message_box(
        self,
        content: str,
        role: str = "assistant",
        timestamp: datetime = None
    ):
        """Display a chat message in a styled box."""
        if role == "user":
            color = self.accent
            icon = "👤"
            title = "You"
        elif role == "assistant":
            color = self.primary
            icon = "🤖"
            title = "VAF"
        elif role == "system":
            color = self.muted
            icon = "⚙️"
            title = "System"
        else:
            color = self.secondary
            icon = "💬"
            title = role.title()
        
        time_str = ""
        if timestamp:
            time_str = f" [{self.muted}]{timestamp.strftime('%H:%M')}[/{self.muted}]"
        
        panel = Panel(
            Markdown(content) if role == "assistant" else content,
            title=f"{icon} [bold]{title}[/bold]{time_str}",
            title_align="left",
            border_style=color,
            box=ROUNDED,
            padding=(0, 1),
        )
        self.console.print(panel)
    
    def code_block(self, code: str, language: str = "python", title: str = None):
        """Display syntax-highlighted code."""
        syntax = Syntax(
            code,
            language,
            theme="dracula",
            line_numbers=True,
            word_wrap=True,
        )
        
        if title:
            panel = Panel(
                syntax,
                title=f"[bold]{title}[/bold]",
                title_align="left",
                border_style=self.primary,
                box=ROUNDED,
            )
            self.console.print(panel)
        else:
            self.console.print(syntax)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # STATUS & EVENTS
    # ═══════════════════════════════════════════════════════════════════════════
    
    def event(self, type_name: str, message: str, style: str = "info"):
        """Print an event/status message."""
        color_map = {
            "info": self.primary,
            "success": self.theme["success"],
            "warning": self.theme["warning"],
            "error": self.theme["error"],
            "dim": self.muted,
        }
        color = color_map.get(style, self.primary)
        
        # Bar indicator
        bar = f"[{color}]│[/{color}]"
        type_str = f"[{self.muted}]{type_name:<10}[/{self.muted}]"
        msg_str = f"[{color}]{message}[/{color}]"
        
        self.console.print(f"{bar} {type_str} {msg_str}")
    
    def success(self, message: str):
        """Print success message."""
        self.event("✓ Success", message, "success")
    
    def error(self, message: str):
        """Print error message."""
        self.event("✗ Error", message, "error")
    
    def warning(self, message: str):
        """Print warning message."""
        self.event("⚠ Warning", message, "warning")
    
    def info(self, message: str):
        """Print info message."""
        self.event("ℹ Info", message, "info")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # PROGRESS & LOADING
    # ═══════════════════════════════════════════════════════════════════════════
    
    def spinner(self, message: str = "Loading..."):
        """Create a spinner context manager."""
        return self.console.status(
            f"[{self.primary}]{message}[/{self.primary}]",
            spinner="dots"
        )
    
    def progress_bar(self, current: int, total: int, label: str = "Tokens"):
        """Display a progress bar (right-aligned)."""
        percent = min(100, int((current / total) * 100))
        
        # Color based on progress
        if percent < 50:
            color = self.primary
        elif percent < 80:
            color = self.theme["warning"]
        else:
            color = self.theme["error"]
        
        # Create bar
        bar_width = 30
        filled = int((percent / 100) * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        
        self.console.print(
            f"[{self.muted}]{label}:[/{self.muted}] "
            f"[{color}]{bar}[/{color}] "
            f"[bold]{percent}%[/bold] ({current}/{total})",
            justify="right"
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # LISTS & TABLES
    # ═══════════════════════════════════════════════════════════════════════════
    
    def list_items(self, items: list, title: str = None, numbered: bool = False):
        """Display a list of items."""
        if title:
            self.console.print(f"[{self.primary} bold]{title}[/{self.primary} bold]")
        
        for i, item in enumerate(items, 1):
            if numbered:
                prefix = f"[{self.muted}]{i:2}.[/{self.muted}]"
            else:
                prefix = f"[{self.primary}]•[/{self.primary}]"
            self.console.print(f"  {prefix} {item}")
        
        self.console.print()
    
    def table(self, headers: list, rows: list, title: str = None):
        """Display a table."""
        table = Table(
            title=title,
            title_style=f"bold {self.primary}",
            border_style=self.border_color,
            header_style=f"bold {self.primary}",
            box=ROUNDED,
        )
        
        for header in headers:
            table.add_column(header)
        
        for row in rows:
            table.add_row(*[str(cell) for cell in row])
        
        self.console.print(table)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # DIALOGS
    # ═══════════════════════════════════════════════════════════════════════════
    
    def confirm(self, message: str, default: bool = True) -> bool:
        """Show a confirmation dialog."""
        default_str = "Y/n" if default else "y/N"
        self.console.print(f"[{self.primary}]?[/{self.primary}] {message} [{self.muted}]{default_str}[/{self.muted}] ", end="")
        
        try:
            response = input().strip().lower()
            if not response:
                return default
            return response in ('y', 'yes', 'ja', 'j')
        except (KeyboardInterrupt, EOFError):
            return False
    
    def select(self, options: list, title: str = "Select an option") -> Optional[int]:
        """Show a selection menu."""
        self.console.print(f"\n[{self.primary} bold]{title}[/{self.primary} bold]")
        
        for i, option in enumerate(options, 1):
            self.console.print(f"  [{self.muted}]{i}.[/{self.muted}] {option}")
        
        self.console.print()
        
        try:
            choice = input(f"[{self.primary}]Enter number: [{self.muted}]1-{len(options)}[/{self.muted}] ")
            index = int(choice) - 1
            if 0 <= index < len(options):
                return index
        except (ValueError, KeyboardInterrupt, EOFError):
            pass
        
        return None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # UTILITIES
    # ═══════════════════════════════════════════════════════════════════════════
    
    def clear(self):
        """Clear the terminal - REAL clear, not just scroll."""
        import os
        import sys
        
        # Method 1: OS-specific command (most reliable)
        if sys.platform == "win32":
            os.system("cls")
        else:
            os.system("clear")
        
        # Method 2: ANSI escape codes (backup, works in most modern terminals)
        # \033[2J = clear screen, \033[H = cursor to home position
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    
    def print(self, text: str = "", **kwargs):
        """Print text with theme colors."""
        self.console.print(text, **kwargs)
    
    def rule(self, title: str = None):
        """Print a horizontal rule."""
        self.console.rule(
            title=title,
            style=self.border_color
        )
    
    def newline(self, count: int = 1):
        """Print newlines."""
        for _ in range(count):
            self.console.print()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # SHORTCUT BAR
    # ═══════════════════════════════════════════════════════════════════════════
    
    def shortcuts_bar(self):
        """Display keyboard shortcuts bar."""
        shortcuts = [
            ("S", "Settings"),
            ("C", "Model"),
            ("T", "Theme"),
            ("H", "History"),
            ("?", "Help"),
        ]
        
        parts = []
        for key, label in shortcuts:
            parts.append(f"[{self.primary} bold]{key}[/{self.primary} bold] [{self.muted}]{label}[/{self.muted}]")
        
        self.console.print(" │ ".join(parts), justify="center")
        self.console.print()

# ═══════════════════════════════════════════════════════════════════════════════
# ANIMATED HEADER (for Sub-Agent collaboration)
# ═══════════════════════════════════════════════════════════════════════════════

import time
import math

class AnimatedHeader:
    """Renders the collab art with a wave animation on the arrow."""
    def __init__(self, title: str, left_agt: str, right_agt: str):
        self.title = title
        self.left_agt = left_agt
        self.right_agt = right_agt
        self.arrow_chars = ["<", "=", "=", "=", "=", "=", ">"]
        
        # Get current theme colors
        from vaf.core.config import Config
        theme_name = Config.get("theme", "vaf")
        theme = ThemeManager.get_theme(theme_name)
        self.border_color = theme.get("border_active", theme.get("primary", "#00d4ff"))
        self.text_color = theme.get("primary", "#00d4ff")

    def __rich__(self) -> Panel:
        # Wave Animation Logic
        t = time.time() * 8
        pos = int(t) % 14 
        if pos > 6: pos = 12 - pos
        
        arrow_str = Text()
        for i, char in enumerate(self.arrow_chars):
            dist = abs(i - pos)
            if dist == 0:
                style = "bold white"
            elif dist == 1:
                style = f"bold {self.text_color}"
            else:
                style = f"dim {self.text_color}"
            arrow_str.append(char, style=style)

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
            padding=(0, 2)
        )


# ═══════════════════════════════════════════════════════════════════════════════
# STATIC UI CLASS (Backward Compatibility)
# ═══════════════════════════════════════════════════════════════════════════════

class UI:
    """
    Static UI class for backward compatibility.
    Wraps TUI methods in static interface used by existing code.
    """
    from rich.theme import Theme as RichTheme
    
    theme = RichTheme({
        "info": "bold cyan",
        "warning": "bold yellow",
        "error": "bold red",
        "success": "bold green",
        "dim": "dim",
        "highlight": "bold magenta",
        "normal": "white"
    })
    console = Console(theme=theme)

    @staticmethod
    def print(text: str = "", style: str = None, end: str = "\n"):
        UI.console.print(text, style=style, end=end, markup=True)

    @staticmethod
    def println(*args):
        text = "".join(str(arg) for arg in args)
        UI.console.print(text)

    @staticmethod
    def clear():
        """Clear the terminal - REAL clear, not just scroll."""
        import os
        import sys
        
        # OS-specific command (most reliable)
        if sys.platform == "win32":
            os.system("cls")
        else:
            os.system("clear")
        
        # ANSI escape codes (backup for modern terminals)
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()

    @staticmethod
    def event(type_name: str, title: str, style: str = "info"):
        """Print event in OpenCode style."""
        color_map = {
            "info": "cyan",
            "warning": "yellow",
            "error": "red",
            "success": "green",
            "highlight": "magenta",
            "dim": "dim"
        }
        color = color_map.get(style, "white")
        bar = f"[{color}]|[/{color}]"
        type_str = f"[dim] {type_name:<7}[/dim]"
        title_str = f"[{color}]{title}[/{color}]"
        UI.console.print(f"{bar}{type_str} {title_str}")

    @staticmethod
    def prompt(prompt_text: str = "> ") -> str:
        """Smart input with autocomplete."""
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.completion import Completer, Completion, PathCompleter, WordCompleter
            from prompt_toolkit.styles import Style as PStyle
            from prompt_toolkit.document import Document
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.keys import Keys
            
            kb = KeyBindings()

            @kb.add(Keys.Tab)
            def _(event):
                b = event.current_buffer
                if b.complete_state:
                    b.complete_next()
                else:
                    b.start_completion(select_first=False)

            class VAFCompleter(Completer):
                def __init__(self):
                    self.path_completer = PathCompleter(expanduser=True)
                    # Complete list of commands (without / prefix for completer)
                    all_commands = [
                        'exit', 'quit', 'q', 'clear', 'settings', 'model', 'help',
                        'session', 'theme', 'undo', 'history', 'export', 'tools',
                        'restore', 'context'
                    ]
                    # Use FuzzyCompleter for better matching
                    from prompt_toolkit.completion import FuzzyCompleter
                    self.cmd_completer = FuzzyCompleter(
                        WordCompleter(all_commands, ignore_case=True),
                        WORD=False  # Allow partial word matching
                    )
                    self.shortcuts = WordCompleter([
                        "~/", "~/Desktop", "~/Documents", "~/Downloads"
                    ])

                def get_completions(self, document, complete_event):
                    text = document.text_before_cursor
                    
                    if "@" in text:
                        start_pos = text.rfind("@")
                        path_text = text[start_pos+1:]
                        dummy_doc = Document(path_text, cursor_position=len(path_text))
                        if len(path_text) < 3:
                            for c in self.shortcuts.get_completions(dummy_doc, complete_event):
                                yield c
                        for c in self.path_completer.get_completions(dummy_doc, complete_event):
                            yield c
                    elif text.startswith("/"):
                        # Extract command part (without /)
                        cmd_text = text[1:].strip()
                        cmd_doc = Document(cmd_text, cursor_position=len(cmd_text))
                        
                        # Get fuzzy completions
                        for c in self.cmd_completer.get_completions(cmd_doc, complete_event):
                            start_pos = c.start_position if c.start_position < 0 else -len(cmd_text)
                            yield Completion(
                                c.text,
                                start_position=start_pos,
                                display=f"/{c.text}",
                                display_meta=c.display_meta or "Command"
                            )

            style = PStyle.from_dict({"prompt": "#5f5fff bold"})
            session = PromptSession(
                completer=VAFCompleter(), 
                style=style, 
                key_bindings=kb,
                complete_while_typing=True
            )
            return session.prompt(prompt_text)

        except ImportError:
            return UI.console.input(f"[dim]{prompt_text}[/dim]")
        except Exception:
            return UI.console.input(f"[dim]{prompt_text}[/dim]")

    @staticmethod
    def logo():
        """Display VAF logo."""
        logo_text = r"""
[bold cyan]
O))         O))       O))))))))
 O))       O))))      O))      
  O))     O))  O))    O))      
   O))   O))    O))   O))))))  
    O)) O)) )))) O))  O))      
     O))))        O)) O))      
      O))          O))O))      
[/bold cyan][dim]文 Veyllo Agentic Framework[/dim]
        """
        UI.console.print(Align.center(logo_text.strip()))
        UI.console.print("\n[dim]Shortcuts: [bold]S[/bold] Settings | [bold]C[/bold] Model | Type [bold]exit[/bold] to Quit[/dim]", justify="center")
        UI.console.print()

    @staticmethod
    def print_usage_bar(current: int, total: int):
        """Display token usage bar."""
        percent = min(100, int((current / total) * 100))
        
        color = "green"
        if percent > 70: color = "yellow"
        if percent > 90: color = "red"
        
        bar_length = 30
        filled = int((percent / 100) * bar_length)
        bar = "▰" * filled + "▱" * (bar_length - filled)
        
        text = f"[{color}]{bar}[/{color}]  [bold]{percent}%[/bold] ({current}/{total} Tok)"
        UI.console.print(f" Context: {text}", justify="right")
        UI.print()

    @staticmethod
    def panel(text: str, title: str = None, style: str = "info"):
        UI.console.rule(f"[{style}]{title or ''}[/{style}]", style=style)
        if text:
            UI.console.print(text, style=style, justify="center")
        UI.console.print()

    @staticmethod
    def error(text: str):
        UI.event("Error", text, style="error")

    @staticmethod
    def success(text: str):
        UI.event("Success", text, style="success")

    @staticmethod
    def warning(text: str):
        UI.event("Warning", text, style="warning")

    @staticmethod
    def info(text: str):
        UI.event("Info", text, style="info")


# ═══════════════════════════════════════════════════════════════════════════════
# SINGLETON INSTANCE
# ═══════════════════════════════════════════════════════════════════════════════

_tui_instance: Optional[TUI] = None

def get_tui(theme: str = None) -> TUI:
    """Get or create TUI instance."""
    global _tui_instance
    if _tui_instance is None or theme:
        _tui_instance = TUI(theme)
    return _tui_instance

