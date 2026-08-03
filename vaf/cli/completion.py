# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What completes to what, independent of the widget that shows it.

The classic lane's completer was written against prompt_toolkit's `Completer`
protocol, but only its EDGES were: the quick-path table, the `@` trigger, the
folder/file markers with sizes and the `/command` matching are all plain
stdlib. A second lane wanting the same behaviour would have had to copy ~90
lines of it, so the logic moved here and both lanes render the same candidates.

The `/command` half asks `vaf/cli/commands.py`, which means the completer can
no longer offer a word the dispatcher does not run - the exact drift that made
that registry necessary.

Import-light on purpose: no prompt_toolkit, no textual, no agent.
"""
import os
import sys
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path
from typing import List

MAX_CANDIDATES = 40          # a menu nobody can walk is not a menu


@dataclass(frozen=True)
class Candidate:
    insert: str              # text to put into the buffer
    label: str               # what the menu shows
    meta: str = ""           # right-hand hint: "Folder", "412KB", "Command"
    replace: int = 0         # characters before the cursor this replaces


def quick_paths() -> dict:
    """Shortcut -> absolute path. Built per call because the working directory
    moves and, on Windows, drives come and go."""
    home = Path.home()
    paths = {
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
    if sys.platform == "win32":
        for letter in "CDEFGH":
            drive = f"{letter}:"
            if Path(f"{drive}/").exists():
                paths[drive.lower()] = f"{drive}\\"
                paths[drive] = f"{drive}\\"
    return paths


def _size_label(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size // 1024}KB"
    return f"{size // (1024 * 1024)}MB"


def _path_candidates(fragment: str) -> List[Candidate]:
    """Directory listing for what has been typed after an `@`."""
    out: List[Candidate] = []
    typed = fragment

    if len(typed) < 3:
        for shortcut, full in quick_paths().items():
            if shortcut.startswith(typed.lower()):
                out.append(Candidate(insert=full, label=f"{shortcut} -> {full}",
                                     meta="Quick Path", replace=len(typed)))

    expanded = str(Path(typed).expanduser()) if typed.startswith("~") else typed
    if sys.platform == "win32" and len(expanded) == 2 and expanded[1] == ":":
        expanded += os.sep

    # Split into "directory to list" and "prefix to match".
    if expanded.endswith(os.sep) or expanded.endswith("/"):
        directory, prefix = expanded, ""
    else:
        directory, _, prefix = expanded.rpartition(os.sep if os.sep in expanded else "/")
        directory = directory or ("." if not expanded.startswith("/") else "/")

    try:
        entries = sorted(os.scandir(directory or "."),
                         key=lambda e: (not e.is_dir(), e.name.lower()))
    except (OSError, ValueError):
        return out[:MAX_CANDIDATES]

    for entry in entries:
        if not entry.name.lower().startswith(prefix.lower()):
            continue
        if entry.name.startswith(".") and not prefix.startswith("."):
            continue                       # hidden files only on request
        try:
            is_dir = entry.is_dir()
            meta = "Folder" if is_dir else _size_label(entry.stat().st_size)
        except OSError:
            is_dir, meta = False, ""
        marker = "▸" if is_dir else "·"
        insert = entry.name + (os.sep if is_dir else "")
        if insert == prefix:
            continue                       # nothing left to add - see above
        out.append(Candidate(insert=insert, label=f"{marker} {entry.name}",
                             meta=meta, replace=len(prefix)))
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def _command_candidates(fragment: str) -> List[Candidate]:
    """Commands, from the registry - never a second list."""
    from vaf.cli.commands import COMMANDS

    words = [c.word for c in COMMANDS]
    lowered = fragment.lower()
    prefix = [w for w in words if w.startswith(lowered)]
    # Fuzzy is a TYPO fallback, not an addition: offering `exit` and `listen`
    # for "/set" buries the one match the user is actually after.
    fuzzy = ([] if prefix or not lowered
             else get_close_matches(lowered, words, n=5, cutoff=0.6))

    by_word = {c.word: c for c in COMMANDS}
    out = []
    for word in prefix + fuzzy:
        if word == lowered:
            # Already typed in full: accepting it would change nothing, and an
            # open menu would swallow the Enter meant to SEND the command.
            continue
        cmd = by_word[word]
        out.append(Candidate(insert=word, label=f"/{word}",
                             meta=cmd.help, replace=len(fragment)))
    return out[:MAX_CANDIDATES]


def complete(text_before_cursor: str) -> List[Candidate]:
    """Candidates for the text left of the cursor.

    Two triggers, the same two the classic lane had: `@` anywhere starts a path
    completion for what follows it, and a line starting with `/` completes a
    command. Anything else completes nothing - this is a chat prompt first.
    """
    text = text_before_cursor or ""
    if "@" in text:
        return _path_candidates(text[text.rfind("@") + 1:].strip())
    if text.startswith("/"):
        return _command_candidates(text[1:].strip())
    return []
