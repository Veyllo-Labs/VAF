# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import os
from pathlib import Path
from typing import Optional, List, Dict

class WorkspaceManager:
    """
    Manages the current working directory (CWD) and project context.
    Provides methods to detect project roots, resolve paths, and track CWD changes.
    """
    
    # Files that indicate a project root
    PROJECT_MARKERS = [
        ".git",
        ".vaf",
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "pom.xml",
        "go.mod",
        "Cargo.toml",
        "composer.json",
        "Gemfile",
        "mix.exs"
    ]
    
    def __init__(self, start_dir: Optional[str] = None):
        self.cwd = Path(start_dir or os.getcwd()).resolve()
        self.project_root = self.find_project_root(self.cwd)
        
    def find_project_root(self, path: Path) -> Optional[Path]:
        """Find the root of the project by looking for markers."""
        current = path.resolve()
        
        # Traverse up to root
        for _ in range(10): # Max depth 10
            for marker in self.PROJECT_MARKERS:
                if (current / marker).exists():
                    return current
            
            if current.parent == current: # Hit filesystem root
                break
            current = current.parent
            
        return None

    def is_project_root(self, path: str) -> bool:
        """Check if a specific path is a project root."""
        p = Path(path).resolve()
        for marker in self.PROJECT_MARKERS:
            if (p / marker).exists():
                return True
        return False

    def get_cwd(self) -> str:
        """Get current working directory."""
        return str(self.cwd)
    
    def set_cwd(self, path: str) -> bool:
        """
        Update the tracked CWD. 
        Note: This does NOT change os.getcwd() of the process automatically, 
        it just tracks the logical workspace.
        """
        p = Path(path).resolve()
        if p.exists() and p.is_dir():
            self.cwd = p
            # Re-evaluate project root if we moved outside
            if self.project_root and self.project_root not in p.parents and p != self.project_root:
                self.project_root = self.find_project_root(p)
            return True
        return False

    def get_context_info(self) -> Dict[str, str]:
        """Get summary of workspace context for LLM injection."""
        return {
            "cwd": str(self.cwd),
            "project_root": str(self.project_root) if self.project_root else "None",
            "is_in_project": bool(self.project_root),
            "rel_path": str(self.cwd.relative_to(self.project_root)) if self.project_root else "."
        }
