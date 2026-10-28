# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The git executable and the two ways VAF runs it, for the framework and the CLI alike.

It lived in the CLI (`vaf/cli/cmd/git.py`), which the framework may not import, so a core
module that needs git (vaf.core.code_audit) would have had to start a fifth runner of its
own. The CLI imports it from here now.

`run_git` runs git as the person would in a repository they own. `run_project_git` runs it in
a folder a model writes: the coding agent's project, whose `.git` directory its jailed shell
can write like any other file. git reads that directory's config and hooks on every call and
starts the programs they name (hooks on commit, add and checkout, an fsmonitor hook on status,
clean and smudge filters on add and checkout, textconv and external diff drivers on diff, merge
drivers on revert, gpg on `log` once `log.showSignature` is set), outside the jail and with the
account's rights. `run_project_git` pins all of them off with `-c`, which outranks every
config file, so that git starts nothing but itself there. It also pins WHERE git works: the
same config can move the work tree anywhere (`core.worktree`), and a `.git` file, a
`commondir` or a symbolic link inside `.git` points the project at another repository. The
runner passes `--git-dir=<top>/.git --work-tree=<top>`, `<top>` being the nearest folder at or
above the caller's that holds a `.git`, and refuses a `.git` that leads anywhere else.

NAMED BOUNDARY, the driver race: the names of the filter and merge drivers are read in one git
call and switched off in the next, and a process writing `.git/config` at the same time can add
a driver in between. Measured: a writer flipping the config in a loop, 189 of 400 reads saw
the driver and a marker filter ran 138 times. A coder `bash` command cannot be that writer: its
jail has its own process namespace and dies with the command (`--unshare-pid`,
`--die-with-parent`; the docker fallback is `--rm`). A process in a running sandbox environment
can, because the environment mounts the project folder read-write for as long as it runs.
Hooks, the fsmonitor, textconv, external diff and signing are pinned without a name and do not
race; only a filter or a merge driver added in that window can run.

NAMED BOUNDARY: `vaf git` (`vaf/cli/cmd/git.py`) and `vaf update` (`vaf/cli/cmd/update.py`)
stay on `run_git`. A person starts them in their own repository or in VAF's own checkout, and
there that repository's hooks and drivers are the person's own choice, which VAF has no
business switching off.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

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


def resolve_commit(rev: str, cwd: str = ".", *, linked_worktree: bool = False) -> Optional[str]:
    """The full id of the commit `rev` names, or None when it names none.

    A revision a caller hands in goes through here before it reaches any other git command
    line. git reads an argument that starts with a dash as an option wherever it stands, and
    `git diff --output=<file>` writes over any file the process may write; the id that comes
    back is hex only, so it is safe in every position."""
    rev = (rev or "").strip()
    if not rev or rev.startswith("-"):
        return None
    proc = run_project_git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], cwd=cwd,
                           linked_worktree=linked_worktree)
    out = (proc.stdout or "").strip()
    return out if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", out) else None


# ── git in a folder a model writes ───────────────────────────────────────────

# Every setting through which a repository's config names a program for the commands VAF runs
# in a project (init, add, commit, status, diff, log, ls-files, rev-parse, stash create,
# revert, reset, checkout, cat-file, grep, merge-base, check-ignore), pinned to a value that
# starts none. Each one is measured to start a program from `.git/config` without it.
PROJECT_GIT_CONFIG: Tuple[Tuple[str, str], ...] = (
    ("core.fsmonitor", "false"),        # an fsmonitor hook on every status, diff and add
    ("core.pager", "cat"),
    ("core.editor", "true"),            # a no-op; every message VAF commits is given with -m
    ("core.askPass", ""),               # empty: no askpass program, the prompt is refused below
    ("core.sshCommand", "ssh"),
    ("diff.external", ""),
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("log.showSignature", "false"),     # set, `git log` hands every signed commit to gpg.program
    ("credential.helper", ""),          # empty resets the helper list
)

# The environment beside them. GIT_ASKPASS is set EMPTY rather than removed: git reads it
# before core.askPass and SSH_ASKPASS, and an empty one ends that search. The system config
# stays: the folder cannot write it, and Git for Windows keeps its LFS filter, autocrlf and
# credential manager there.
PROJECT_GIT_ENV: Tuple[Tuple[str, str], ...] = (
    ("GIT_TERMINAL_PROMPT", "0"),
    ("GIT_PAGER", "cat"),
    ("GIT_EDITOR", "true"),
    ("GIT_ASKPASS", ""),
)

# Per named driver. An empty clean/smudge/process runs no filter, `required=false` keeps the
# missing filter from failing the command, an empty merge driver makes a merge that needed it
# a conflict (measured with `git revert`). A diff driver's `command` cannot be emptied (git
# then tries to run "" and dies), so diffs carry --no-ext-diff instead, added below.
_DRIVER_OVERRIDES: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "filter": (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")),
    "diff": (("textconv", ""),),
    "merge": (("driver", ""),),
}

# Config scopes the project's own folder cannot write. A driver named only there (the filter
# `git lfs install` writes into the global config) is the person's own and keeps running; a
# driver named, or redefined, in the repository's config is switched off whole.
_TRUSTED_SCOPES = frozenset({"global", "system", "command"})

# Flags added right after the subcommand, so no caller has to remember them.
_SUBCOMMAND_FLAGS: Dict[str, Tuple[str, ...]] = {
    "diff": ("--no-ext-diff", "--no-textconv"),
    "log": ("--no-ext-diff", "--no-textconv"),
    "show": ("--no-ext-diff", "--no-textconv"),
    "commit": ("--no-verify",),
}

# A driver name git would read differently on a -c command line: `-c` splits at the first
# "=", so `filter.a=b.clean=` would set `filter.a` instead.
_UNSAFE_DRIVER_NAME = re.compile(r"[=\x00-\x1f\x7f]")

# Variables that would move the repository away from the folder the runner pins.
_LAYOUT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
               "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")

_HOOKS_DIR: Optional[str] = None


class ProjectGitRefused(ValueError):
    """The folder's git layout leads somewhere the runner will not follow."""


def _is_empty_dir(path: str) -> bool:
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is None
    except OSError:
        return False


def empty_hooks_dir() -> str:
    """An empty directory VAF owns, for `core.hooksPath`: git finds no hook in it.

    Under VAF's own data directory, which no project jail mounts. Checked to be empty on every
    call; when it is not, or cannot be made, a fresh temporary directory takes its place.
    Forward slashes, which git reads on Windows too."""
    global _HOOKS_DIR
    if _HOOKS_DIR and _is_empty_dir(_HOOKS_DIR):
        return _HOOKS_DIR
    candidate = ""
    try:
        from vaf.core.platform import Platform
        d = Path(Platform.data_dir()) / "git-no-hooks"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        if _is_empty_dir(str(d)):
            candidate = d.as_posix()
    except OSError:
        candidate = ""
    if not candidate:
        candidate = Path(tempfile.mkdtemp(prefix="vaf-no-hooks-")).as_posix()
    _HOOKS_DIR = candidate
    return candidate


def project_git_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The whole environment (git on Windows needs PATH, SYSTEMROOT and the rest) with the
    project settings on top."""
    env = dict(os.environ if base is None else base)
    for name in _LAYOUT_ENV:
        env.pop(name, None)
    env.update(PROJECT_GIT_ENV)
    return env


def _top(cwd: str) -> Optional[str]:
    """The nearest folder at or above `cwd` that holds a `.git` entry (file, folder or link)."""
    d = os.path.realpath(cwd)
    while True:
        if os.path.lexists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _foreign_link(dotgit: str) -> str:
    """What in `.git` leads into another repository, or "".

    git follows `commondir` and `objects/info/alternates` to another git directory, and a
    symbolic link anywhere it reads or writes (refs, objects, the index) takes the read or the
    write to the link's target; a project VAF made has none of them."""
    for rel in ("commondir", os.path.join("objects", "info", "alternates")):
        if os.path.lexists(os.path.join(dotgit, rel)):
            return f".git/{Path(rel).as_posix()} points at another repository"
    for folder in (dotgit, os.path.join(dotgit, "objects")):
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        rel = Path(os.path.relpath(entry.path, dotgit)).as_posix()
                        return f".git/{rel} is a link"
        except OSError:
            continue
    for root, dirs, files in os.walk(os.path.join(dotgit, "refs")):
        for name in dirs + files:
            if os.path.islink(os.path.join(root, name)):
                rel = Path(os.path.relpath(os.path.join(root, name), dotgit)).as_posix()
                return f".git/{rel} is a link"
    return ""


def project_repo_problem(cwd: str, *, linked_worktree: bool = False) -> str:
    """Why `run_project_git` would refuse the repository at or above `cwd`, or "".

    `linked_worktree=True` accepts a `.git` file and a shared git directory (`git worktree
    add`), for a repository a person points VAF at themselves."""
    top = _top(cwd)
    if top is None:
        return f"Not a git repository: {cwd}"
    if linked_worktree:
        return ""
    dotgit = os.path.join(top, ".git")
    if os.path.islink(dotgit) or not os.path.isdir(dotgit):
        return (f"{Path(top).as_posix()}/.git is not a folder: a linked worktree or a link to "
                f"another repository")
    problem = _foreign_link(dotgit)
    return f"{Path(top).as_posix()}: {problem}" if problem else ""


def _layout_options(sub: str, cwd: str, linked_worktree: bool) -> List[str]:
    """The options that pin git to the project's own repository. Raises ProjectGitRefused."""
    if sub == "init":
        # init creates the repository: there is none to pin yet, but a `.git` link would make
        # it create the repository at the link's target.
        if os.path.islink(os.path.join(cwd, ".git")):
            raise ProjectGitRefused(f"{Path(os.path.abspath(cwd)).as_posix()}/.git is a link")
        return []
    problem = project_repo_problem(cwd, linked_worktree=linked_worktree)
    if problem:
        raise ProjectGitRefused(problem)
    top = Path(_top(cwd) or cwd)
    if linked_worktree:
        return [f"--work-tree={top.as_posix()}"]
    return [f"--git-dir={(top / '.git').as_posix()}", f"--work-tree={top.as_posix()}"]


def _untrusted_drivers(cwd: str, env: Dict[str, str], timeout: float,
                       layout: Sequence[str] = ()) -> Dict[str, List[str]]:
    """Driver names per section ("filter", "diff", "merge") that repository config defines.

    A read-only `git config` call, pinned to the same repository as the command: reading config
    starts no program. Raises RuntimeError when the names cannot be read, so the caller refuses
    instead of running unprotected."""
    argv = [resolve_git(), *layout, "config", "--show-scope", "-z", "--get-regexp",
            r"^(filter|diff|merge)\."]
    proc = subprocess.run(argv, **_popen_kwargs(cwd, env, timeout))
    # Exit 1 is "no such key"; anything else is an error worth refusing on.
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"cannot read the repository's drivers: {(proc.stderr or '').strip()[:200]}")
    found: Dict[str, List[str]] = {}
    tokens = (proc.stdout or "").split("\0")
    for scope, entry in zip(tokens[0::2], tokens[1::2]):
        key = entry.split("\n", 1)[0]
        section, _, rest = key.partition(".")
        name, dot, _var = rest.rpartition(".")
        if not dot or not name or scope in _TRUSTED_SCOPES:
            continue   # no subsection (diff.renames, merge.ff), or the person's own config
        names = found.setdefault(section.lower(), [])
        if name not in names:
            names.append(name)
    return found


def _popen_kwargs(cwd: str, env: Dict[str, str], timeout: float) -> dict:
    kwargs = {"cwd": cwd, "env": env, "capture_output": True, "text": True, "timeout": timeout,
              "encoding": "utf-8", "errors": "replace", "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kwargs


def project_git_command(args: Sequence[str], cwd: str, *, env: Optional[Dict[str, str]] = None,
                        timeout: float = 60, linked_worktree: bool = False) -> List[str]:
    """The full argv `run_project_git` starts for `args` in `cwd`.

    Order: git, the repository it may work on (`--git-dir`, `--work-tree`), the caller's own
    leading `-c` pairs (an identity for a commit), then the pinned settings, so a pinned value
    always comes last and wins, then the subcommand with its added flags and the rest. Raises
    ValueError (ProjectGitRefused among them) for a layout the runner will not follow or a
    driver name that cannot be pinned on a command line, RuntimeError when the names cannot be
    read."""
    args = list(args)
    env = project_git_env() if env is None else env
    i = 0
    while i + 1 < len(args) and args[i] == "-c":
        i += 2
    caller_config, sub, rest = args[:i], args[i:i + 1], args[i + 1:]
    layout = _layout_options(sub[0] if sub else "", cwd, linked_worktree)
    pinned: List[str] = ["-c", f"core.hooksPath={empty_hooks_dir()}"]
    for key, value in PROJECT_GIT_CONFIG:
        pinned += ["-c", f"{key}={value}"]
    for section, names in _untrusted_drivers(cwd, env, timeout, layout).items():
        for name in names:
            if _UNSAFE_DRIVER_NAME.search(name):
                raise ValueError(f"the repository defines a {section} driver whose name cannot "
                                 f"be switched off on a command line: {name!r}")
            for var, value in _DRIVER_OVERRIDES.get(section, ()):
                pinned += ["-c", f"{section}.{name}.{var}={value}"]
    added = list(_SUBCOMMAND_FLAGS.get(sub[0], ())) if sub else []
    return [resolve_git(), *layout, *caller_config, *pinned, *sub, *added, *rest]


def run_project_git(args: Sequence[str], cwd: str, *, timeout: float = 60,
                    linked_worktree: bool = False) -> subprocess.CompletedProcess:
    """Run git in a folder whose `.git` someone else may have written, starting nothing but git.

    `args` is what follows `git`, optionally led by `-c key=value` pairs of the caller's own.
    Hooks, the fsmonitor, pagers, editors, askpass, credential helpers, signing, external diff
    and textconv, and the filter and merge drivers the repository's config names are all off;
    diffs and logs get --no-ext-diff --no-textconv and commits --no-verify. git works on the
    repository whose `.git` is at or nearest above `cwd`, and on its folder as the work tree,
    whatever that config says; a `.git` that is a file or a link, or that leads into another
    repository, is refused unless `linked_worktree=True` (then only the work tree is pinned).
    Never raises: a missing git, a timeout or a refusal comes back as returncode -1 with the
    reason in stderr.
    """
    argv: List[str] = ["git", *args]
    if not os.path.isdir(cwd):
        # subprocess reports a missing cwd as FileNotFoundError, which reads as "no git".
        return subprocess.CompletedProcess(argv, -1, "", f"Not a directory: {cwd}")
    try:
        env = project_git_env()
        argv = project_git_command(args, cwd, env=env, timeout=timeout,
                                   linked_worktree=linked_worktree)
        return subprocess.run(argv, **_popen_kwargs(cwd, env, timeout))
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, -1, "", "Git is not installed.")
    except Exception as e:
        return subprocess.CompletedProcess(argv, -1, "", str(e))
