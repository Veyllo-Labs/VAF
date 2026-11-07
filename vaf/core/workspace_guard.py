# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Which host directories an agent may use as a project or mount into a sandbox.

Two questions, asked by everything that hands a directory to an agent:

- `is_unsafe_project_dir(path)`: may this be a project at all? Refuses the
  filesystem root and the home directory (and anything above home), the standard user
  folders themselves (their subfolders are fine), VAF's own state and VAF's own code.
- `assert_safe_workspace(path)`: may this be bound read-write into a jail or a
  container? Refuses VAF's code and VAF's own state, each in either direction
  (containing it or inside it), the home directory and the filesystem root. For a
  container mount on an SELinux host this is what keeps `:z` from relabelling the home
  directory.

VAF's own state is every root `vaf.core.data_stores.is_vaf_state` names: ``~/.vaf`` (or
``$VAF_CONFIG_DIR`` in Docker mode), the data directory and the config directory. A
project there, or one that contains them, would let the agent's own project tools write
the custom tools VAF executes and the settings it obeys.

They lived in the coder (11,000 lines) and the coder's shell, so the core modules that
asked them - the session store, the web server, the headless runner - imported the
coder to do it. They are core questions; the coder is one of the askers.

A third question combines the first with the account: `project_dir_refusal(path, ...)`,
may THIS caller use this folder as a project. It adds no rule of its own: the unsafe
half is `is_unsafe_project_dir`, the account half is the file jail's own
`jail_allows(..., mode="write")`, so a project folder and a file write can never disagree
about whose tree a path is in. The coder asks it for every folder it may work in (an
explicit `project_path`, a path in the task, the chat's last project, the cwd, the folder
it settles on) and the project history and rollback ask it for the folder they open.
Named boundary: it is not on the facade. The account half already is (`vaf.jail_allows`),
the unsafe half encodes VAF's own data layout, and no harness lane asks the combined
question yet (the web workspace resolver asks only the unsafe half, after its own owner
check). Export it when a harness lane or an embedder needs both halves together.
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
    - VAF's own state (`data_stores.is_vaf_state`: ~/.vaf, the data and the config
      directory) and every folder that contains part of it
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

    # VAF's own state and every folder that contains part of it
    from vaf.core.data_stores import holds_vaf_state, is_vaf_state
    if is_vaf_state(p) or holds_vaf_state(p):
        return True

    # VAF program/source tree and everything inside it
    if p == _VAF_ROOT or p.is_relative_to(_VAF_ROOT):
        return True

    return False


# The account half's reason, a constant so a caller that words the two halves differently
# (project_git keeps its unsafe-half text) can tell which half refused.
OUTSIDE_ACCOUNT = "it is outside the folders this account may write"


def project_dir_refusal(path, *, user_scope_id, user_role=None):
    """Why this caller may not use ``path`` as a project folder, or None when it may.

    Two halves, asked in this order: `is_unsafe_project_dir` (no account may use it: the
    home directory, a standard folder itself, VAF's own data or code), then the per-account
    file jail in write mode (`jail_allows`: an empty scope or an admin identity reaches
    everything the first half allows; anyone else reaches their own
    ``VAF_Projects/<uid8>/`` and the shared folders of the rooms they were admitted to).
    The path is resolved by both, so a link is judged by where it points. Nothing on disk
    is changed. Fail-closed: an error is a refusal.

    ``user_role`` must come from the caller's authenticated identity, never from a model's
    argument. The answer is a short English reason meant to follow "cannot work in X:";
    `OUTSIDE_ACCOUNT` names the account half.
    """
    try:
        if is_unsafe_project_dir(str(path)):
            return ("it is the home directory, a standard folder itself, VAF's own data "
                    "or VAF's code, not a project folder")
        from vaf.tools.filesystem import jail_allows
        if not jail_allows(str(path), user_scope_id=user_scope_id, user_role=user_role,
                           mode="write"):
            return OUTSIDE_ACCOUNT
        return None
    except Exception:
        return "it could not be checked"


def assert_safe_workspace(ws: str) -> None:
    """Raise ValueError unless `ws` may be bound read-write into a jail or container."""
    p = Path(ws).resolve()
    root = _VAF_ROOT
    if p == root or root.is_relative_to(p) or p.is_relative_to(root):
        raise ValueError(f"refusing to run: workspace {p} overlaps the VAF source tree {root}")
    from vaf.core.data_stores import holds_vaf_state, is_vaf_state
    if is_vaf_state(p) or holds_vaf_state(p):
        raise ValueError(f"refusing to run: workspace {p} is or contains VAF's own data; "
                         "choose a folder under Documents")
    # Never root a jail at the real HOME, anything above it (/home holds every account's home)
    # or the filesystem root: that would bind-mount a home (incl. ~/.vaf secrets) or the
    # whole system read-write. The same rule as is_unsafe_project_dir.
    home = Path.home().resolve()
    if home.is_relative_to(p) or p == Path(p.anchor):
        raise ValueError(f"refusing to run: workspace {p} is the home directory, a folder above "
                         "it or the root directory, not a project")
