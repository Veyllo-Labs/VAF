# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The git executable and one way to run it, for the framework and the CLI alike.

It lived in the CLI (`vaf/cli/cmd/git.py`), which the framework may not import, so a core
module that needs git (vaf.core.code_audit) would have had to start a fifth runner of its
own. The CLI imports it from here now.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import List, Optional

_GIT_EXE: Optional[str] = None


def resolve_git() -> str:
    """Resolve the git executable, memoized.

    Prefer `git` on PATH. Otherwise fall back to the portable MinGit that the VAF installer
    (bootstrap.ps1) and the patch script download on Windows but do NOT persist to PATH - without
    this, `vaf update` (and every other git call) fails with "Git is not installed." on a machine
    that has no system git, even though VAF already fetched one. Returns "git" as a last resort so
    the FileNotFoundError path still reports cleanly.
    """
    global _GIT_EXE
    if _GIT_EXE:
        return _GIT_EXE
    exe = shutil.which("git")
    if not exe and os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            for cand in (
                os.path.join(local, "Veyllo", "git", "cmd", "git.exe"),   # bootstrap.ps1
                os.path.join(local, "VAF", "mingit", "cmd", "git.exe"),    # patch.bat
            ):
                if os.path.isfile(cand):
                    exe = cand
                    break
    _GIT_EXE = exe or "git"
    return _GIT_EXE


def run_git(args: List[str], cwd: str = ".", timeout: float = 60) -> tuple[int, str, str]:
    """Execute a git command (resolving VAF's bundled portable git if git is not on PATH)."""
    kwargs = {"cwd": cwd, "capture_output": True, "text": True, "timeout": timeout,
              "encoding": "utf-8", "errors": "replace"}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run([resolve_git()] + list(args), **kwargs)
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        return -1, "", "Git is not installed."
    except Exception as e:
        return -1, "", str(e)


def resolve_commit(rev: str, cwd: str = ".") -> Optional[str]:
    """The full id of the commit `rev` names, or None when it names none.

    A revision a caller hands in goes through here before it reaches any other git command
    line. git reads an argument that starts with a dash as an option wherever it stands, and
    `git diff --output=<file>` writes over any file the process may write; the id that comes
    back is hex only, so it is safe in every position."""
    rev = (rev or "").strip()
    if not rev or rev.startswith("-"):
        return None
    code, out, _err = run_git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], cwd=cwd)
    out = out.strip()
    return out if code == 0 and re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", out) else None
