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

import os
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


def find_project_context_file(start_dir: Path, filenames: tuple[str, ...] = DEFAULT_CONTEXT_FILENAMES,
                              *, stop_at: Optional[Path] = None) -> Optional[Path]:
    """
    Search upwards from start_dir for a VAF context file.
    Returns the first match found, preferring nearest parent.

    `stop_at` is the highest folder searched: a chat's project inside a person's own tree
    must not pick up a VAF.md above it, which belongs to someone else (the owner's home, the
    shared projects root). Without it the walk goes to the filesystem root, which is right
    for a terminal started in the person's own directory.
    """
    cur = start_dir.resolve()
    ceiling = stop_at.resolve() if stop_at is not None else None
    if ceiling is not None and cur != ceiling and ceiling not in cur.parents:
        return None

    # Walk upwards until the ceiling or the filesystem root
    while True:
        for name in filenames:
            candidate = cur / name
            if candidate.exists() and candidate.is_file():
                # A link that leads out of the ceiling is not this tree's file: a person
                # could otherwise point VAF.md at someone else's and have it read for them.
                if ceiling is not None:
                    real = candidate.resolve()
                    if real != ceiling and ceiling not in real.parents:
                        continue
                return candidate

        if cur.parent == cur or cur == ceiling:
            return None
        cur = cur.parent


def _opened_within(fd: int, path: Path, ceiling: Path) -> bool:
    """Is the file open on `fd` the one that `path` resolves to INSIDE `ceiling`? The resolved
    location must lie within the ceiling, and the open file must be that very file (same
    device and inode), so a link swapped after the open cannot pass for it."""
    try:
        real = Path(os.path.realpath(path))
        if real != ceiling and ceiling not in real.parents:
            return False
        opened, there = os.fstat(fd), os.stat(real)
    except OSError:
        return False
    return (opened.st_dev, opened.st_ino) == (there.st_dev, there.st_ino)


def load_project_context(start_dir: Path, max_chars: int = 12_000,
                         *, stop_at: Optional[Path] = None) -> Optional[ProjectContext]:
    """
    Load context from VAF.md if present (searching upwards, never above `stop_at`).
    Content is truncated to max_chars to avoid context overflow.
    """
    path = find_project_context_file(start_dir, stop_at=stop_at)
    if not path:
        return None

    # Opened ONCE, and the open file itself is what is checked against the ceiling: a check on
    # the path followed by a second open would read whatever the link points at by then, and
    # a link can be swapped in between.
    ceiling = stop_at.resolve() if stop_at is not None else None
    try:
        with open(path, "rb") as fh:
            if ceiling is not None and not _opened_within(fh.fileno(), path, ceiling):
                return None
            text = fh.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return None

    if not text:
        return None

    if len(text) > max_chars:
        return ProjectContext(path=path, content=text[:max_chars] + "\n\n[... truncated ...]", truncated=True)

    return ProjectContext(path=path, content=text, truncated=False)


