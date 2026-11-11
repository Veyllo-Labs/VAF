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
It is on the facade (`vaf.project_dir_refusal`), because harness lanes ask both halves
together: the web workspace resolver serves a chat's stored folder to the caller, and the
headless runner hands the same stored folder to a turn as its project (and reads its
VAF.md into the prompt). A folder recorded before the account half existed can name
another account's tree, and the unsafe half alone does not see that. The jailed shell
(`vaf.tools.workspace_exec.run_in_workspace`) asks it too, for the folder it is about to
bind.

The home directory's configuration is the fourth thing no agent may use: `is_host_config`
(and `holds_host_config` for a folder above it). Files there run or steer programs outside
every VAF containment at the next login, shell or Python start (shell start files,
autostart entries, systemd user units, the Python user site, launch agents, the PowerShell
profiles). Two parts, so there is no list of file names to drift:

- every entry of the home directory whose name starts with a dot, and all below it
  (deny-by-default, the same polarity as ``~/.vaf``);
- explicit roots that hold configuration outside a dot folder: ``$XDG_CONFIG_HOME`` and
  ``$XDG_DATA_HOME`` when set to an absolute path, ``%APPDATA%`` and ``%LOCALAPPDATA%``,
  ``~/Library/LaunchAgents`` and ``Documents/PowerShell`` / ``Documents/WindowsPowerShell``.

The file tools refuse writes there (``vaf.tools.filesystem.is_safe_path``), a project or a
mount there is unsafe, and the sandbox copy-out refuses members that would land there.
Reading is not refused: the rule is about what runs later, not about secrets (``~/.ssh``
and key file names are refused to reads by their own screen).

NAMED BOUNDARIES of the host rule. A path inside the system temp folder is exempt from an
explicit root that CONTAINS the temp folder, and from nothing else: Windows' temp folder
lies inside ``%LOCALAPPDATA%`` and every staging lane writes there. Exempting temp from
every rule would switch the rule off exactly where it is tested, because a scratch home
(the tests, macOS' ``/var/folders``) lies in temp. A ``TMPDIR`` inside a home dot folder
makes temp writes through the file tools refused.
An entry named only by dots (``~/...``) is folded away by the shared normaliser and is not
a dot entry; no program reads configuration from such a name. The rule answers for a path
at the moment it is asked; a folder swapped for a link afterwards is the race
`vaf.core.path_jail.open_for_write` narrows for VAF's own state.
"""

from __future__ import annotations

import os
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
    - the home directory's configuration (`is_host_config`: the dot entries of home,
      autostart, the Python user site, ...) and every folder that contains part of it
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

    # The home directory's configuration, and every folder that contains part of it
    if is_host_config(p) or holds_host_config(p):
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
            return ("it is the home directory, a standard folder itself, VAF's own data, "
                    "the home directory's configuration or VAF's code, not a project folder")
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
    if is_host_config(p) or holds_host_config(p):
        raise ValueError(f"refusing to run: workspace {p} is or contains the home directory's "
                         "configuration (dot folders, autostart, the Python user site); "
                         "choose a folder under Documents")
    # Never root a jail at the real HOME, anything above it (/home holds every account's home)
    # or the filesystem root: that would bind-mount a home (incl. ~/.vaf secrets) or the
    # whole system read-write. The same rule as is_unsafe_project_dir.
    home = Path.home().resolve()
    if home.is_relative_to(p) or p == Path(p.anchor):
        raise ValueError(f"refusing to run: workspace {p} is the home directory, a folder above "
                         "it or the root directory, not a project")


# Seams, read at call time so a test's HOME, USERPROFILE and environment redirect applies,
# and so the Windows layout can be proven from another host (the ntpath seam in
# vaf.core.data_stores spells the paths these return).
def _home() -> str:
    return os.fspath(Path.home())


def _documents_dirs() -> list:
    from vaf.core.platform import Platform
    return [os.fspath(Platform.documents_dir())]


def _temp_roots() -> list:
    import tempfile
    return [tempfile.gettempdir()]


def _explicit_host_config_roots() -> list:
    """The configuration roots outside a home dot folder, as path strings (see the module
    docstring). Not gated by platform: on a host where one means nothing, refusing a write
    there costs nothing."""
    from vaf.core import data_stores as _ds
    pm = _ds._pathmod
    home = _home()
    out = []
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
        value = (os.environ.get(var) or "").strip()
        # The XDG specification ignores a relative value; so does this rule, rather than
        # refusing a folder relative to whatever the current directory is.
        if value and pm.isabs(value):
            out.append(value)
    out.append(pm.join(home, "Library", "LaunchAgents"))
    for docs in [pm.join(home, "Documents")] + _documents_dirs():
        out.append(pm.join(docs, "PowerShell"))
        out.append(pm.join(docs, "WindowsPowerShell"))
    return out


def _spelled(paths) -> list:
    from vaf.core.data_stores import _spellings
    out = []
    for path in paths:
        for parts in _spellings(path):
            if parts and parts not in out:
                out.append(parts)
    return out


def _inside(parts, root) -> bool:
    return parts[:len(root)] == root


def is_inside_temp(path) -> bool:
    """Whether ``path`` lies strictly inside the system temp folder, in either spelling of
    both. Fail-closed for a caller that grants something on True: any error answers False."""
    try:
        temps = _spelled(_temp_roots())
        return any(len(parts) > len(t) and _inside(parts, t)
                   for parts in _spelled([path]) for t in temps)
    except Exception:
        return False


def is_host_config(path) -> bool:
    """Whether ``path`` is part of the home directory's configuration, which other programs
    run or obey: an entry of the home directory whose name starts with a dot, or anything
    below one, or a path inside one of the explicit roots (``$XDG_CONFIG_HOME``,
    ``$XDG_DATA_HOME``, ``%APPDATA%``, ``%LOCALAPPDATA%``, ``~/Library/LaunchAgents``, the
    PowerShell profile folders). The module docstring says why and names the boundaries.

    Compared component by component in every spelling (as given and resolved, case-folded,
    stream suffixes and trailing dots cut) by the normaliser `is_vaf_state` uses, for the
    path and for the home directory. A path inside the system temp folder is exempt from an
    explicit root that contains the temp folder (Windows' temp inside ``%LOCALAPPDATA%``),
    never from the dot rule. Fail-closed: any error answers True."""
    try:
        mine = _spelled([path])
        for home in _spelled([_home()]):
            for parts in mine:
                if len(parts) > len(home) and _inside(parts, home) \
                        and parts[len(home)].startswith("."):
                    return True
        temps = _spelled(_temp_roots())
        for root in _spelled(_explicit_host_config_roots()):
            if not any(_inside(parts, root) for parts in mine):
                continue
            # The temp folder inside this root is where staging lanes write, not configuration.
            if any(_inside(t, root) and _inside(parts, t) for parts in mine for t in temps):
                continue
            return True
        return False
    except Exception:
        return True


def holds_host_config(path) -> bool:
    """Whether ``path`` contains part of the home directory's configuration: it is the home
    directory or a folder above it (either spelling), or an explicit root of `is_host_config`
    lies strictly inside it. Fail-closed: any error answers True."""
    try:
        mine = _spelled([path])
        for home in _spelled([_home()]):
            if any(_inside(home, parts) for parts in mine):
                return True
        for root in _spelled(_explicit_host_config_roots()):
            if any(len(parts) < len(root) and _inside(root, parts) for parts in mine):
                return True
        return False
    except Exception:
        return True
