"""
VAF UI - User Interface Components
This module re-exports from tui.py for backward compatibility.
"""

# Re-export everything from tui.py
from vaf.cli.tui import (
    # Main classes
    TUI,
    UI,
    AnimatedHeader,
    
    # Singleton
    get_tui,
    
    # Box styles
    MODERN_BOX,
    INPUT_BOX,
)

# For backwards compatibility - UI is the main export
__all__ = [
    "UI",
    "TUI", 
    "AnimatedHeader",
    "get_tui",
    "MODERN_BOX",
    "INPUT_BOX",
]
