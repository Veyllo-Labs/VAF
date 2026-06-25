# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Project Context Loader (VAF.md)

Inspired by Gemini CLI's "context files" concept (e.g. GEMINI.md).
This module finds and loads a project-local context file that provides
stable instructions for the agent across sessions.

Rules:
- OS-independent path handling (Path)
- Safe size limits to avoid context blowups
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_CONTEXT_FILENAMES = (
    "VAF.md",
    # Hidden folder variant (nice for repos)
    str(Path(".vaf") / "VAF.md"),
)


@dataclass(frozen=True)
class ProjectContext:
    path: Path
    content: str
    truncated: bool = False


def find_project_context_file(start_dir: Path, filenames: tuple[str, ...] = DEFAULT_CONTEXT_FILENAMES) -> Optional[Path]:
    """
    Search upwards from start_dir for a VAF context file.
    Returns the first match found, preferring nearest parent.
    """
    cur = start_dir.resolve()

    # Walk upwards until filesystem root
    while True:
        for name in filenames:
            candidate = cur / name
            if candidate.exists() and candidate.is_file():
                return candidate

        if cur.parent == cur:
            return None
        cur = cur.parent


def load_project_context(start_dir: Path, max_chars: int = 12_000) -> Optional[ProjectContext]:
    """
    Load context from VAF.md if present (searching upwards).
    Content is truncated to max_chars to avoid context overflow.
    """
    path = find_project_context_file(start_dir)
    if not path:
        return None

    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return None

    if not text:
        return None

    if len(text) > max_chars:
        return ProjectContext(path=path, content=text[:max_chars] + "\n\n[... truncated ...]", truncated=True)

    return ProjectContext(path=path, content=text, truncated=False)


