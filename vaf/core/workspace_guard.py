# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Which host directories an agent may use as a project or mount into a sandbox.

Two questions, asked by everything that hands a directory to an agent:

- `is_unsafe_project_dir(path)`: may this be a project at all? Refuses the
  filesystem root and the home directory (and anything above home), the standard user
  folders themselves (their subfolders are fine), VAF's own data directory and VAF's
  own code.
- `assert_safe_workspace(path)`: may this be bound read-write into a jail or a
  container? Refuses VAF's code in either direction (containing it or inside it), the
  home directory and the filesystem root. For a container mount on an SELinux host
  this is what keeps `:z` from relabelling the home directory.

They lived in the coder (11,000 lines) and the coder's shell, so the core modules that
asked them - the session store, the web server, the headless runner - imported the
coder to do it. They are core questions; the coder is one of the askers.
"""

from __future__ import annotations

from pathlib import Path

# The checkout or installed package root: vaf/core/workspace_guard.py -> parents[2].
_VAF_ROOT = Path(__file__).resolve().parents[2]

_STANDARD_DIRS = ("Documents", "Desktop", "Downloads", "Pictures",
                  "Music", "Videos", "Public", "Templates")


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
    if p in {home / d for d in _STANDARD_DIRS}:
        return True

    # VAF config dir (~/.vaf) and everything inside it
    vaf_cfg = home / ".vaf"
    if p == vaf_cfg or p.is_relative_to(vaf_cfg):
        return True

    # VAF program/source tree and everything inside it
    if p == _VAF_ROOT or p.is_relative_to(_VAF_ROOT):
        return True

    return False


def assert_safe_workspace(ws: str) -> None:
    """Raise ValueError unless `ws` may be bound read-write into a jail or container."""
    p = Path(ws).resolve()
    root = _VAF_ROOT
    if p == root or root.is_relative_to(p) or p.is_relative_to(root):
        raise ValueError(f"refusing to run: workspace {p} overlaps the VAF source tree {root}")
    # Never root a jail at the real HOME, anything above it (/home holds every account's home)
    # or the filesystem root: that would bind-mount a home (incl. ~/.vaf secrets) or the
    # whole system read-write. The same rule as is_unsafe_project_dir.
    home = Path.home().resolve()
    if home.is_relative_to(p) or p == Path(p.anchor):
        raise ValueError(f"refusing to run: workspace {p} is the home directory, a folder above "
                         "it or the root directory, not a project")
