"""
VAF Themes - Terminal Color Themes
Based on popular IDE/Editor themes
"""
import json
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass

@dataclass
class ThemeColors:
    """Theme color definitions."""
    primary: str
    secondary: str
    accent: str
    error: str
    warning: str
    success: str
    info: str
    text: str
    text_muted: str
    background: str
    background_panel: str
    border: str
    border_active: str

# ═══════════════════════════════════════════════════════════════════════════════
# BUILT-IN THEMES
# ═══════════════════════════════════════════════════════════════════════════════

THEMES: Dict[str, Dict[str, str]] = {
    "vaf": {
        "name": "VAF Default",
        "primary": "#00d4ff",      # Cyan
        "secondary": "#bd93f9",    # Purple
        "accent": "#50fa7b",       # Green
        "error": "#ff5555",        # Red
        "warning": "#f1fa8c",      # Yellow
        "success": "#50fa7b",      # Green
        "info": "#8be9fd",         # Cyan light
        "text": "#f8f8f2",         # White
        "text_muted": "#6272a4",   # Gray
        "background": "#0d1117",   # Dark
        "background_panel": "#161b22",
        "border": "#30363d",
        "border_active": "#00d4ff",
    },
    
    "dracula": {
        "name": "Dracula",
        "primary": "#bd93f9",      # Purple
        "secondary": "#ff79c6",    # Pink
        "accent": "#8be9fd",       # Cyan
        "error": "#ff5555",        # Red
        "warning": "#f1fa8c",      # Yellow
        "success": "#50fa7b",      # Green
        "info": "#ffb86c",         # Orange
        "text": "#f8f8f2",         # White
        "text_muted": "#6272a4",   # Comment
        "background": "#282a36",
        "background_panel": "#21222c",
        "border": "#44475a",
        "border_active": "#bd93f9",
    },
    
    "nord": {
        "name": "Nord",
        "primary": "#88c0d0",      # Frost
        "secondary": "#81a1c1",    # Frost dark
        "accent": "#a3be8c",       # Green
        "error": "#bf616a",        # Red
        "warning": "#ebcb8b",      # Yellow
        "success": "#a3be8c",      # Green
        "info": "#5e81ac",         # Blue
        "text": "#eceff4",         # Snow
        "text_muted": "#4c566a",   # Gray
        "background": "#2e3440",   # Polar night
        "background_panel": "#3b4252",
        "border": "#434c5e",
        "border_active": "#88c0d0",
    },
    
    "tokyonight": {
        "name": "Tokyo Night",
        "primary": "#7aa2f7",      # Blue
        "secondary": "#bb9af7",    # Purple
        "accent": "#9ece6a",       # Green
        "error": "#f7768e",        # Red
        "warning": "#e0af68",      # Yellow
        "success": "#9ece6a",      # Green
        "info": "#7dcfff",         # Cyan
        "text": "#c0caf5",         # White
        "text_muted": "#565f89",   # Gray
        "background": "#1a1b26",
        "background_panel": "#16161e",
        "border": "#292e42",
        "border_active": "#7aa2f7",
    },
    
    "catppuccin": {
        "name": "Catppuccin Mocha",
        "primary": "#cba6f7",      # Mauve
        "secondary": "#f5c2e7",    # Pink
        "accent": "#94e2d5",       # Teal
        "error": "#f38ba8",        # Red
        "warning": "#f9e2af",      # Yellow
        "success": "#a6e3a1",      # Green
        "info": "#89b4fa",         # Blue
        "text": "#cdd6f4",         # Text
        "text_muted": "#6c7086",   # Overlay
        "background": "#1e1e2e",   # Base
        "background_panel": "#181825",
        "border": "#313244",
        "border_active": "#cba6f7",
    },
    
    "gruvbox": {
        "name": "Gruvbox Dark",
        "primary": "#fe8019",      # Orange
        "secondary": "#d3869b",    # Purple
        "accent": "#b8bb26",       # Green
        "error": "#fb4934",        # Red
        "warning": "#fabd2f",      # Yellow
        "success": "#b8bb26",      # Green
        "info": "#83a598",         # Blue
        "text": "#ebdbb2",         # FG
        "text_muted": "#928374",   # Gray
        "background": "#282828",   # BG
        "background_panel": "#1d2021",
        "border": "#3c3836",
        "border_active": "#fe8019",
    },
    
    "monokai": {
        "name": "Monokai Pro",
        "primary": "#a9dc76",      # Green
        "secondary": "#ab9df2",    # Purple
        "accent": "#78dce8",       # Cyan
        "error": "#ff6188",        # Red/Pink
        "warning": "#ffd866",      # Yellow
        "success": "#a9dc76",      # Green
        "info": "#78dce8",         # Cyan
        "text": "#fcfcfa",         # White
        "text_muted": "#727072",   # Gray
        "background": "#2d2a2e",
        "background_panel": "#221f22",
        "border": "#403e41",
        "border_active": "#a9dc76",
    },
    
    "github": {
        "name": "GitHub Dark",
        "primary": "#58a6ff",      # Blue
        "secondary": "#bc8cff",    # Purple
        "accent": "#3fb950",       # Green
        "error": "#f85149",        # Red
        "warning": "#d29922",      # Yellow
        "success": "#3fb950",      # Green
        "info": "#58a6ff",         # Blue
        "text": "#c9d1d9",         # Text
        "text_muted": "#8b949e",   # Gray
        "background": "#0d1117",
        "background_panel": "#161b22",
        "border": "#30363d",
        "border_active": "#58a6ff",
    },
    
    "onedark": {
        "name": "One Dark",
        "primary": "#61afef",      # Blue
        "secondary": "#c678dd",    # Purple
        "accent": "#98c379",       # Green
        "error": "#e06c75",        # Red
        "warning": "#e5c07b",      # Yellow
        "success": "#98c379",      # Green
        "info": "#56b6c2",         # Cyan
        "text": "#abb2bf",         # Text
        "text_muted": "#5c6370",   # Gray
        "background": "#282c34",
        "background_panel": "#21252b",
        "border": "#3e4451",
        "border_active": "#61afef",
    },
    
    "synthwave": {
        "name": "Synthwave '84",
        "primary": "#ff7edb",      # Pink
        "secondary": "#fede5d",    # Yellow
        "accent": "#72f1b8",       # Green
        "error": "#fe4450",        # Red
        "warning": "#f97e72",      # Orange
        "success": "#72f1b8",      # Green
        "info": "#36f9f6",         # Cyan
        "text": "#ffffff",         # White
        "text_muted": "#848bbd",   # Gray
        "background": "#262335",
        "background_panel": "#1e1a2b",
        "border": "#495495",
        "border_active": "#ff7edb",
    },
    
    "matrix": {
        "name": "Matrix",
        "primary": "#00ff41",      # Matrix Green
        "secondary": "#008f11",    # Dark Green
        "accent": "#00ff41",       # Green
        "error": "#ff0000",        # Red
        "warning": "#ffff00",      # Yellow
        "success": "#00ff41",      # Green
        "info": "#00ff41",         # Green
        "text": "#00ff41",         # Green
        "text_muted": "#008f11",   # Dark Green
        "background": "#0d0208",   # Black
        "background_panel": "#000000",
        "border": "#003b00",
        "border_active": "#00ff41",
    },
    
    "light": {
        "name": "Light",
        "primary": "#0969da",      # Blue
        "secondary": "#8250df",    # Purple
        "accent": "#1a7f37",       # Green
        "error": "#cf222e",        # Red
        "warning": "#9a6700",      # Yellow
        "success": "#1a7f37",      # Green
        "info": "#0969da",         # Blue
        "text": "#24292f",         # Dark
        "text_muted": "#57606a",   # Gray
        "background": "#ffffff",   # White
        "background_panel": "#f6f8fa",
        "border": "#d0d7de",
        "border_active": "#0969da",
    },
}

# ═══════════════════════════════════════════════════════════════════════════════
# THEME MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class ThemeManager:
    """Manages terminal themes."""
    
    _current: str = "vaf"
    _themes: Dict[str, Dict[str, str]] = THEMES.copy()
    
    @classmethod
    def get_theme(cls, name: str = None) -> Dict[str, str]:
        """Get a theme by name or current theme."""
        theme_name = name or cls._current
        return cls._themes.get(theme_name, cls._themes["vaf"])
    
    @classmethod
    def set_theme(cls, name: str) -> bool:
        """Set the current theme."""
        if name in cls._themes:
            cls._current = name
            return True
        return False
    
    @classmethod
    def current(cls) -> str:
        """Get current theme name."""
        return cls._current
    
    @classmethod
    def list_themes(cls) -> list:
        """List all available themes."""
        return list(cls._themes.keys())
    
    @classmethod
    def get_rich_theme(cls, name: str = None) -> dict:
        """Convert theme to Rich theme format."""
        theme = cls.get_theme(name)
        return {
            "primary": theme["primary"],
            "secondary": theme["secondary"],
            "accent": theme["accent"],
            "error": theme["error"],
            "warning": theme["warning"],
            "success": theme["success"],
            "info": theme["info"],
            "text": theme["text"],
            "dim": theme["text_muted"],
            "border": theme["border"],
        }
    
    @classmethod
    def load_custom_theme(cls, path: str) -> bool:
        """Load a custom theme from JSON file."""
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            
            name = data.get("name", Path(path).stem)
            cls._themes[name.lower()] = data
            return True
        except Exception:
            return False

# Export default
Theme = ThemeManager

