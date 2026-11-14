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

The folder is also pinned in time. A process of the account (a sandbox environment with the
project mounted) can rename its project folder aside and put a link into another account's
repository in its place between a caller's check and git's start; measured, `git_log` then
printed the other account's history in 21 of 300 calls. On Linux the runner opens the
caller's folder, walks up to the top through `..` handles and opens `.git` without following
a link (`vaf.core.path_jail.open_folder`), then starts every git process of the call (the
driver reads, the gitlinks read, the command) in `/proc/self/fd/<top>` with
`--git-dir=/proc/self/fd/<git>`, `--work-tree=/proc/self/fd/<top>` and `GIT_COMMON_DIR` on the
git handle, handing both handles over (`pass_fds`). git therefore runs in the top folder, and
relative paths and pathspecs are read from there. `expect_folder` is the real path a caller
judged: a folder that is not it when it is opened is refused, and so is one without a `.git`
of its own (no walk up to a repository above it, which nobody judged: without that, a `.git`
renamed away in that moment made `log` print a parent repository's history and `add -A`
would stage the parent's whole tree). The judged folder is the top, decided once: a `.git`
that is there when it is looked at and gone again a moment later is refused, never answered
by the repository above (with a racer flipping `.git`, 1000 calls: 0 of 269 answered calls
printed the parent's history, 242 of 418 while the runner still looked twice), and git's
own search for a linked worktree's `.git` file stops at the judged folder
(`GIT_CEILING_DIRECTORIES`; a parent whose name holds `os.pathsep` cannot be named there and
is refused). Measured with a racer looping
rename-and-link, 1000 calls each: log 0 leaks of 930 answered (6 of 992 before), status 3 of
570 (10 of 629), diff 10 of 868 (20 of 865).

git's own text walk is closed where the private mount lane runs (Linux with a bubblewrap that
can start a namespace, `vaf.core.folder_mount.namespace_available`; not for
`linked_worktree`). git keeps `--git-dir=/proc/self/fd/<n>` as given, but it turns the work
tree into a path and every command that sets up a work tree (status, diff, add, checkout,
revert, reset) changes into that path, and `git init` writes the new repository by path. So
the command of the call starts in a bwrap namespace whose root is its own tmpfs, holding the
system folders read-only; the hooks folder, the person's global git config and the global
ignore and attributes files it names (`core.excludesFile`, `core.attributesFile`) read-only;
and the top folder bound read-write at its real path, the only place git can write there (the
root is read-only, with no scratch /tmp, so a command that writes outside the folder, such as
`worktree add <elsewhere>`, fails instead of writing into the namespace's memory and losing
it, and the runner adds a line saying why); `vaf.core.folder_mount`'s guard checks inside,
before git starts, that this path is the very folder the top handle holds (`dev:ino`), and git
runs there with `--work-tree=<top>` and the git handle as before. A start the guard refused is
repeated (at most three starts, never once git ran) and then refused with the last start's
cause. Inside the namespace the path names only the bound folder: a rename or a link on the
host after the mount does not reach it. The read of the repository's own drivers starts in
the same view as the command: an include in the project's config can name a path that exists
only inside the namespace (`/proc/self/task/2/...`, git's own task there), and a read outside
missed the filter it defines while the command inside ran it, a filter that could reach the
host's loopback (measured). The reads whose answer does not depend on the view, the person's
global and system config (the project cannot write them) and the index (the gitlinks), run
through the handles as in the fallback: they set up no work tree, so the text walk does not
happen there (measured fully pinned). The namespace shares the network, so a git-lfs smudge
of the person's own config can download; only drivers of the global and system config run
in it, the repository's are switched off by the read above. Measured, the same racer, 1000
status calls: 0 leaks of 963 answered with the lane (2 of 957 with the handles alone), and
`reset --hard` wrote into the other folder in 0 of 300 calls. The price is two namespace
starts per call: about 15 against 5 ms for a status, and a coding run's state update for the
SubAgent window (about a dozen calls) 190 against 65 ms, measured under the lowest CPU
priority on four cores. A top inside a folder the namespace binds
read-only (a git install prefix, /opt) keeps the handles alone: its view there would not be
private (`folder_mount.overlaps_ro`).

NAMED BOUNDARY, what the handles do not reach where the lane does not run (macOS, Windows,
Linux without a working bubblewrap, `linked_worktree`), with the numbers above:
- git's own text walk of the work tree, as described above: the status and diff leaks above
  are in the microseconds between git's realpath and its change into the path (strace). That
  moment reads AND writes: a command that writes the work tree (checkout, reset, revert;
  `/undo` and `project_rollback`) can write the project's own files into the swapped-in
  folder. Measured, 300 calls each with `expect_folder`: `reset --hard` wrote into the other
  folder in 2 of 247 answered calls, `checkout -- .` in 0 of 255. A log, rev-parse, cat-file
  or config call stays fully pinned. `git init` writes the new repository by path.
- where the lane runs, git reads only the global config files it is given and the ignore and
  attributes files they name: any other file an include in them points to is not there
  inside the namespace (a driver such an include names does not run in the command; an
  ignore or attributes file outside the project that the project's own config names is not
  bound either, so status may list a file it would hide).
- entries written inside `.git` after `_foreign_link` scanned it: an `objects/info/alternates`
  written in that moment let `cat-file` read the other repository's object; a `commondir`
  file makes `rev-parse HEAD` print the other repository's commit id (its objects stay out
  of reach, because `GIT_COMMON_DIR` is pinned).
- macOS (folder handles and a kernel path, no `/proc`): the folders are opened and compared
  the same way, and before each git process the path must still name the opened folder
  (`samestat`), else the call is refused; git itself then runs by path, so the moment between
  that comparison and git's start remains. Not measured on a Mac; `/dev/fd` as a working
  folder is not used, because it is unmeasured. Nor is a case-insensitive volume: the kernel
  path keeps the case on disk, so an `expect_folder` typed in another case would be refused
  there, never wrongly accepted (unmeasured).
- Windows (no folder handles): before each git process the caller's folder must still resolve
  to `expect_folder`, when one is given; git runs by path.
- `rev-parse --git-dir` and `--git-common-dir` print the handle spelling (`/proc/self/fd/<n>`,
  meaningless outside that git process); `--absolute-git-dir` and `--show-toplevel` print
  the real paths, inside the namespace too.

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
import stat
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


def resolve_commit(rev: str, cwd: str = ".", *, linked_worktree: bool = False,
                   expect_folder=None) -> Optional[str]:
    """The full id of the commit `rev` names, or None when it names none.

    A revision a caller hands in goes through here before it reaches any other git command
    line. git reads an argument that starts with a dash as an option wherever it stands, and
    `git diff --output=<file>` writes over any file the process may write; the id that comes
    back is hex only, so it is safe in every position. ``expect_folder`` as for
    `run_project_git`."""
    rev = (rev or "").strip()
    if not rev or rev.startswith("-"):
        return None
    proc = run_project_git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], cwd=cwd,
                           linked_worktree=linked_worktree, expect_folder=expect_folder)
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
    # status and diff otherwise start a `git status` inside every submodule, an embedded
    # repository the model made with `git init` included, and that child reads its own
    # config, whose filters this runner never read (measured). `add` and `stash create` do not
    # honour diff.ignoreSubmodules; they are handled where the subcommand flags are added.
    ("diff.ignoreSubmodules", "all"),
    ("status.submoduleSummary", "false"),
    ("submodule.recurse", "false"),     # reset and checkout otherwise recurse into submodules
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
# fail: a conflict on git 2.56, an error that stops the revert on 2.25 (both measured). A diff driver's `command` cannot be emptied (git
# then tries to run "" and dies), so diffs carry --no-ext-diff instead, added below.
_DRIVER_OVERRIDES: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "filter": (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")),
    "diff": (("textconv", ""),),
    "merge": (("driver", ""),),
}

# Which drivers to switch off is decided by comparing two reads, not by asking which file a
# value came from. The EFFECTIVE config (no scope given) is what the command itself will see,
# conditional includes evaluated across all files: an `includeIf "hasconfig:remote.*.url:..."`
# in .git/config whose remote lives only in config.worktree named a filter that a read of
# either file alone missed, and that filter ran (measured). The TRUSTED config is the global
# and the system file, which the project folder cannot write. A driver all of whose effective
# (key, value) pairs are trusted ones is the person's own (the filter `git lfs install` writes)
# and keeps running; any other is switched off whole: one only the project names, one the
# project redefines or adds to, one from an include. A driver a parent process passes in
# GIT_CONFIG_PARAMETERS is switched off too, being neither file. No scope labels (git 2.26) are
# asked for, so git 2.25 (Ubuntu 20.04) works. Every read follows includes (--includes; a
# specific scope reads none otherwise, measured).
_DRIVER_KEYS = r"^(filter|diff|merge)\."
_TRUSTED_READS: Tuple[str, ...] = ("--global", "--system")
# The trusted reads also ask for the person's global ignore and attributes files: inside the
# git namespace only what is bound exists, and `core.excludesFile` usually names a file in the
# home folder (`~/.gitignore_global`). Unbound, status listed, and `add -A` staged, a file that
# person's global ignore hides (measured: a `.secret` file staged). Read from the global and
# system files only, which the project folder cannot write; `~` expanded as git does. A value
# the project's own config sets is not bound (inside the namespace git then has no such file).
_TRUSTED_FILE_KEYS = ("core.excludesfile", "core.attributesfile")
_TRUSTED_KEYS = r"^(filter|diff|merge)\.|^core\.(excludesfile|attributesfile)$"

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


# git's own words for a write outside the folder in the namespace (the C locale's strerror;
# under another LANG the hint is not added, the refusal itself stands).
_EROFS = "Read-only file system"
_EROFS_HINT = ("\nvaf: git runs in a private view of the project folder, where only that folder "
               "is writable; a command that writes elsewhere (a worktree, an archive or a "
               "bundle outside the project) needs plain git on a repository you trust.\n")


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
    symbolic link anywhere it reads or writes takes the read or the write to the link's target:
    a reflog line (which ends with a commit subject the model wrote) is appended through a
    linked `logs/HEAD`, measured. A project VAF made has none of them. Everything under `.git`
    is looked at except inside `objects/` (only its top entries: the object folders are many,
    and git writes a new object beside them, never through one) and `lfs/`, and except
    `hooks/`, which the runner never uses and which a person may well link in their own
    repository. Half a millisecond on a working repository's `.git`."""
    for rel in ("commondir", os.path.join("objects", "info", "alternates")):
        if os.path.lexists(os.path.join(dotgit, rel)):
            return f".git/{Path(rel).as_posix()} points at another repository"

    def _link(path: str) -> str:
        return f".git/{Path(os.path.relpath(path, dotgit)).as_posix()} is a link"

    try:
        with os.scandir(os.path.join(dotgit, "objects")) as entries:
            for entry in entries:
                if entry.is_symlink():
                    return _link(entry.path)
    except OSError:
        pass
    for root, dirs, files in os.walk(dotgit):
        at_top = os.path.normcase(root) == os.path.normcase(dotgit)
        for name in dirs + files:
            if at_top and name == "hooks":
                continue
            if os.path.islink(os.path.join(root, name)):
                return _link(os.path.join(root, name))
        dirs[:] = [d for d in dirs
                   if d != "objects" and not (at_top and d in ("hooks", "lfs"))]
    return ""


def project_repo_problem(cwd: str, *, linked_worktree: bool = False) -> str:
    """Why `run_project_git` would refuse the repository at or above `cwd`, or "".

    `linked_worktree=True` accepts a `.git` file and a shared git directory (`git worktree
    add`), for a repository a person points VAF at themselves."""
    top = _top(cwd)
    if top is None:
        return f"Not a git repository: {cwd}"
    return _repo_problem_at(top, linked_worktree=linked_worktree)


def _repo_problem_at(top: str, *, linked_worktree: bool = False) -> str:
    """`project_repo_problem` for the repository whose top is `top`, without a walk."""
    if linked_worktree:
        return ""
    dotgit = os.path.join(top, ".git")
    if os.path.islink(dotgit) or not os.path.isdir(dotgit):
        return (f"{Path(top).as_posix()}/.git is not a folder: a linked worktree or a link to "
                f"another repository")
    problem = _foreign_link(dotgit)
    return f"{Path(top).as_posix()}: {problem}" if problem else ""


class _NoFolder(Exception):
    """The caller's folder is missing or is not a folder."""


def _proc_fd_usable(fd: int) -> bool:
    """True when ``/proc/self/fd/<fd>`` names the open handle ``fd`` (Linux). Then a child
    process reaches the very folder the handle holds through that name: its working folder,
    `--git-dir` and `--work-tree` go through the handle, never through the folder's path."""
    try:
        return os.path.samestat(os.stat(f"/proc/self/fd/{fd}"), os.fstat(fd))
    except OSError:
        return False


class _Pin:
    """The repository one `run_project_git` call works on, the same for every git process the
    call starts (the driver reads, the gitlinks read and the command).

    Three branches, decided per call:
    - by handle (Linux): the top folder and its `.git` are open handles, and every process
      runs in `/proc/self/fd/<top>` with `--git-dir=/proc/self/fd/<git>`,
      `--work-tree=/proc/self/fd/<top>` and `GIT_COMMON_DIR` on the git handle (`pass_fds`);
      where the private mount lane runs (`namespace`), every process starts in that
      namespace instead, with the top bound at its real path and checked inside to be the
      held folder, `--work-tree=<top>` and the same git handle;
    - by path with handles (macOS: handles and a kernel path, no `/proc`): the folders are
      opened and compared the same way, and before each process the path must still name
      the opened folder (`samestat`), else the call is refused; git then runs by path;
    - by path (Windows, no folder handles): before each process the caller's folder must
      still resolve to ``expect_folder`` when one is given; git runs by path.
    In every branch git runs IN the top folder, so relative paths and pathspecs are read
    from the top."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.top = ""                   # the top folder's real path
        self.top_fd = -1
        self.git_fd = -1                # -1 for init and for a linked worktree
        self.by_handle = False
        self.expected: Optional[str] = None   # the Windows branch's comparison
        # A linked worktree with expect_folder: git finds the `.git` file itself, and must
        # not search above the judged folder when it is gone (GIT_CEILING_DIRECTORIES).
        self.ceiling: Optional[str] = None
        # The top held for the private mount namespace (`vaf.core.folder_mount`), set where
        # that lane runs; git then works on the top bound at its own path. `namespace_ro` is
        # what the namespace binds read-only besides the system folders.
        self.namespace = None
        self.namespace_ro: List[str] = []
        self._fds: List[int] = []

    def own(self, fd: int) -> int:
        self._fds.append(fd)
        return fd

    def release(self, fd: int) -> None:
        if fd in self._fds:
            self._fds.remove(fd)
            os.close(fd)

    def close(self) -> None:
        while self._fds:
            os.close(self._fds.pop())

    def work_tree(self, read: bool = False) -> str:
        if self.namespace is not None and not read:
            # Inside the namespace the real path names only the bound folder; git realpaths
            # it to the same text, so `--show-toplevel` and a subfolder's pathspec stay real.
            return self.top
        return f"/proc/self/fd/{self.top_fd}" if self.by_handle else Path(self.top).as_posix()

    def git_dir(self) -> str:
        if self.by_handle:
            return f"/proc/self/fd/{self.git_fd}"
        return (Path(self.top) / ".git").as_posix()

    def layout(self, sub: str, linked_worktree: bool, read: bool = False) -> List[str]:
        """``read=True``: the layout of a read-only config or index read, which runs through
        the handles even where the command runs in the namespace (see `run`)."""
        if sub == "init":
            return []
        if linked_worktree:
            return [f"--work-tree={self.work_tree(read)}"]
        return [f"--git-dir={self.git_dir()}", f"--work-tree={self.work_tree(read)}"]

    def check(self) -> None:
        """Before each process of the call: the folder is still the one that was judged."""
        if self.by_handle:
            return
        if self.top_fd >= 0:
            try:
                same = os.path.samestat(os.stat(self.top), os.fstat(self.top_fd))
                if same and self.git_fd >= 0:
                    same = os.path.samestat(os.lstat(os.path.join(self.top, ".git")),
                                            os.fstat(self.git_fd))
            except OSError:
                same = False
            if not same:
                raise ProjectGitRefused(f"{Path(self.top).as_posix()} is no longer the folder "
                                        "that was opened (a link on the way)")
        elif self.expected is not None:
            real = os.path.normcase(os.path.realpath(self.cwd))
            if real != self.expected:
                raise ProjectGitRefused(f"its folder is {real}, not {self.expected} "
                                        "(a link on the way)")

    def run(self, argv: Sequence[str], env: Dict[str, str], timeout: float, *,
            read: bool = False):
        """Start one git process of the call. ``read=True`` runs it through the handles, never
        in the namespace: for the reads whose answer cannot depend on the view, the person's
        global and system config (the project cannot write them) and the index (the gitlinks).
        They set up no work tree, so git's text walk does not happen there (measured fully
        pinned under the racer). The read of the repository's own drivers is NOT one of them:
        an include in the project's config can name a path that exists only inside the
        namespace (`/proc/self/task/2/...`, git's own task there), and a read outside missed
        the filter it defines while the command inside ran it (measured). It runs in the same
        view as the command. Two namespace starts per call instead of four or five."""
        self.check()
        if self.by_handle and self.git_fd >= 0:
            # After project_git_env removed every layout variable: the common directory
            # (refs, objects, config) is the pinned `.git` too, not a `commondir` file's.
            env = {**env, "GIT_COMMON_DIR": self.git_dir()}
        if self.ceiling is not None:
            env = {**env, "GIT_CEILING_DIRECTORIES": self.ceiling}
        if self.namespace is not None and not read:
            from vaf.core import folder_mount
            kwargs = _popen_kwargs("/", env, timeout)
            if self.git_fd >= 0:
                kwargs["pass_fds"] = (self.git_fd,)
            proc = folder_mount.run_guarded(
                folder_mount.namespace_argv(self.namespace, self.top, extra_ro=self.namespace_ro,
                                            scratch_tmp=False, readonly_root=True),
                self.namespace, self.top, list(argv), **kwargs)
            if folder_mount.guard_refused(proc):
                raise ProjectGitRefused(proc.stderr)
            return proc
        kwargs = _popen_kwargs(self.work_tree(read=True), env, timeout)
        if self.by_handle:
            kwargs["pass_fds"] = tuple(fd for fd in (self.top_fd, self.git_fd) if fd >= 0)
        return subprocess.run(list(argv), **kwargs)


def _has_git_entry(dir_fd: int) -> bool:
    try:
        os.stat(".git", dir_fd=dir_fd, follow_symlinks=False)
        return True
    except OSError:
        return False


def _no_git_here(cwd: str) -> "ProjectGitRefused":
    """The refusal for a judged folder without its own `.git`: the repository above it is a
    folder nobody judged (another account's tree, the home folder's dotfiles repository), and
    `add -A` there would stage all of it."""
    return ProjectGitRefused(f"Not a git repository: {cwd} holds no .git of its own, and git "
                             "is not run on a repository above the folder that was checked")


def _ceiling(top: str) -> str:
    """The `GIT_CEILING_DIRECTORIES` value that stops git's own search at `top`. git reads the
    variable as a list split on `os.pathsep`, so a parent spelled with one (`:` on POSIX, `;`
    on Windows) would set no ceiling at all and the search would go on upwards: refused."""
    parent = os.path.dirname(top)
    if os.pathsep in parent:
        raise ProjectGitRefused(f"{Path(top).as_posix()}: the folder above it has "
                                f"{os.pathsep!r} in its name, which git's search limit cannot "
                                "carry; a linked worktree is not run there")
    return parent


def _pin_by_handle(pin: _Pin, sub: str, linked_worktree: bool, expect_folder) -> None:
    """Open the caller's folder, walk up through `..` handles to the top, open its `.git`.
    With `expect_folder` there is no walk: the judged folder must hold `.git` itself."""
    from vaf.core.path_jail import FolderMoved, PathEscape, open_folder
    try:
        fd, real = open_folder(pin.cwd, expect_folder=expect_folder)
    except PathEscape as e:
        raise ProjectGitRefused(f"{pin.cwd}: {e}") from None
    except (FileNotFoundError, NotADirectoryError):
        raise _NoFolder() from None
    except OSError as e:
        raise ProjectGitRefused(f"cannot open {pin.cwd}: {e.strerror or e}") from None
    pin.own(fd)
    if sub != "init":
        if expect_folder is not None and not _has_git_entry(fd):
            raise _no_git_here(pin.cwd)
        # With expect_folder the judged folder is the top, decided once: a `.git` gone again
        # by now is refused by its open below, never answered by a repository further up.
        while expect_folder is None and not _has_git_entry(fd):
            parent = os.path.dirname(real)
            if parent == real:
                raise ProjectGitRefused(f"Not a git repository: {pin.cwd}")
            try:
                up, up_real = open_folder("..", dir_fd=fd, expect_folder=parent)
            except PathEscape as e:
                raise ProjectGitRefused(f"{pin.cwd}: {e}") from None
            except OSError:
                raise ProjectGitRefused(f"Not a git repository: {pin.cwd}") from None
            pin.release(fd)
            fd, real = pin.own(up), up_real
    pin.top_fd, pin.top = fd, real
    top = Path(real).as_posix()
    if sub == "init":
        # init creates the repository: there is none to pin yet, but a `.git` link would make
        # it create the repository at the link's target.
        try:
            if stat.S_ISLNK(os.stat(".git", dir_fd=fd, follow_symlinks=False).st_mode):
                raise ProjectGitRefused(f"{top}/.git is a link")
        except FileNotFoundError:
            pass
    elif not linked_worktree:
        try:
            gfd, git_real = open_folder(".git", dir_fd=fd, follow=False,
                                        expect_folder=os.path.join(real, ".git"))
        except FolderMoved as e:
            # The top was renamed or swapped after it was opened: not a layout question.
            raise ProjectGitRefused(f"{top}/.git: {e}") from None
        except FileNotFoundError:
            raise _no_git_here(pin.cwd) from None
        except (PathEscape, OSError):
            raise ProjectGitRefused(f"{top}/.git is not a folder: a linked worktree or a link "
                                    f"to another repository") from None
        pin.git_fd = pin.own(gfd)
    elif expect_folder is not None:
        pin.ceiling = _ceiling(real)
    pin.by_handle = _proc_fd_usable(pin.top_fd) and (pin.git_fd < 0
                                                     or _proc_fd_usable(pin.git_fd))
    if pin.git_fd >= 0:
        problem = _foreign_link(pin.git_dir() if pin.by_handle else os.path.join(real, ".git"))
        if problem:
            raise ProjectGitRefused(f"{top}: {problem}")
    if pin.by_handle and not linked_worktree:
        from vaf.core import folder_mount
        if folder_mount.namespace_available():
            ro = _namespace_ro()
            # A top inside a read-only bound folder (/opt, a git install prefix) would share
            # that bind's view of the host: no private view there, the handle pin stays.
            if not folder_mount.overlaps_ro(pin.top, ro):
                # The handle the pin judged, so the identity the guard checks is that folder's.
                pin.namespace = folder_mount.HeldFolder.adopt(pin.top_fd, pin.top)
                pin.namespace_ro = ro


def _namespace_ro() -> List[str]:
    """What git reads outside the project inside its namespace, bound read-only where it
    exists: the empty hooks folder, a git installed outside the system folders (its install
    prefix, `<prefix>/bin/git`, so its helpers and libraries come along; the executable alone
    when that prefix is the home folder or above it), and the person's global (and a moved
    system) config, so the driver reads and the command see the same trusted files as on the
    host. `/etc/gitconfig` is in /etc."""
    from vaf.core.folder_mount import SYSTEM_DIRS
    ro = [empty_hooks_dir()]
    home = os.path.expanduser("~")

    def _outside_system(path: str) -> bool:
        return not any(path == d or path.startswith(d + "/") for d in SYSTEM_DIRS + ("/dev", "/proc"))

    def _install_root(path: str) -> str:
        root = os.path.dirname(os.path.dirname(path))
        real_home = os.path.realpath(home)
        if root in ("", "/") or real_home == root or real_home.startswith(root.rstrip("/") + "/"):
            return path
        return root

    exe = resolve_git()
    if os.path.isabs(exe):
        for p in dict.fromkeys((exe, os.path.realpath(exe))):
            if _outside_system(p):
                ro.append(_install_root(p))
    global_config = os.environ.get("GIT_CONFIG_GLOBAL")
    if global_config:
        ro.append(os.path.abspath(os.path.expanduser(global_config)))
    else:
        ro.append(os.path.join(home, ".gitconfig"))
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        ro.append(os.path.join(xdg, "git"))
    system_config = os.environ.get("GIT_CONFIG_SYSTEM")
    if system_config:
        ro.append(os.path.abspath(os.path.expanduser(system_config)))
    return [p for p in ro if os.path.isabs(p) and _outside_system(p)]


def _pin_by_path(pin: _Pin, sub: str, linked_worktree: bool) -> None:
    """The Windows branch, and the layout `project_git_command` shows: decided on the path."""
    if sub == "init":
        if os.path.islink(os.path.join(pin.cwd, ".git")):
            raise ProjectGitRefused(f"{Path(os.path.abspath(pin.cwd)).as_posix()}/.git is a link")
        pin.top = os.path.realpath(pin.cwd)
        return
    if pin.expected is not None:
        # The judged folder is the top, decided once: no `_top` walk that a `.git` renamed
        # away in the meantime would send to a repository further up. git then gets this
        # folder's `.git` by name (--git-dir), and fails on its own if it is gone.
        top = os.path.realpath(pin.cwd)
        if not os.path.lexists(os.path.join(top, ".git")):
            raise _no_git_here(pin.cwd)
        problem = _repo_problem_at(top, linked_worktree=linked_worktree)
        if problem:
            raise ProjectGitRefused(problem)
        pin.top = top
        if linked_worktree:
            pin.ceiling = _ceiling(top)
        return
    problem = project_repo_problem(pin.cwd, linked_worktree=linked_worktree)
    if problem:
        raise ProjectGitRefused(problem)
    pin.top = _top(pin.cwd) or os.path.realpath(pin.cwd)


def _split_args(args: Sequence[str]) -> Tuple[List[str], List[str], List[str]]:
    """(the caller's leading `-c` pairs, [subcommand] or [], the rest)."""
    args = list(args)
    i = 0
    while i + 1 < len(args) and args[i] == "-c":
        i += 2
    return args[:i], args[i:i + 1], args[i + 1:]


def _untrusted_drivers(pin: _Pin, env: Dict[str, str], timeout: float,
                       layout: Sequence[str] = (),
                       command_layout: Optional[Sequence[str]] = None) -> Dict[str, List[str]]:
    """Driver names per section ("filter", "diff", "merge") whose effective config is not
    entirely the person's global or system config (see _TRUSTED_READS).

    Read-only `git config` calls, pinned to the same repository as the command: reading config
    starts no program. Raises RuntimeError when a read fails, so the caller refuses instead of
    running unprotected."""
    trusted = set()
    for scope in _TRUSTED_READS:
        try:
            trusted |= _driver_pairs([scope], pin, env, timeout, layout, _TRUSTED_KEYS)
        except RuntimeError:
            # An unreadable trusted file (no HOME, a broken ~/.gitconfig) trusts nothing:
            # that only switches more drivers off. Only the effective read fails closed.
            pass
    # The same two reads name the person's global ignore and attributes files, which the
    # namespace binds read-only (see _TRUSTED_KEYS).
    for key, value in sorted(trusted, key=str):
        if key.lower() in _TRUSTED_FILE_KEYS and value:
            path = os.path.expanduser(value)
            if os.path.isabs(path) and path not in pin.namespace_ro:
                pin.namespace_ro.append(path)
    found: Dict[str, List[str]] = {}
    # The effective read sees exactly what the command will see: same view, same layout.
    effective = _driver_pairs([], pin, env, timeout,
                              layout if command_layout is None else command_layout, read=False)
    for key, value in sorted(effective, key=str):
        if (key, value) in trusted:
            continue
        section, _, rest = key.partition(".")
        name, dot, _var = rest.rpartition(".")
        if not dot or not name:
            continue   # no subsection: diff.renames, merge.ff
        names = found.setdefault(section.lower(), [])
        if name not in names:
            names.append(name)
    return found


def _driver_pairs(scope: Sequence[str], pin: _Pin, env: Dict[str, str], timeout: float,
                  layout: Sequence[str], keys: Optional[str] = None, *,
                  read: bool = True) -> set:
    """The (key, value) pairs one `git config --get-regexp` read returns; value None for a key
    written without one. Exit 1 is "no such key" (or no such file); anything else raises.
    ``read=False`` starts it where the command will start (see `_Pin.run`)."""
    argv = [resolve_git(), *layout, "config", *scope, "--includes", "-z", "--get-regexp",
            keys or _DRIVER_KEYS]
    proc = pin.run(argv, env, timeout, read=read)
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"cannot read the repository's drivers: "
                           f"{(proc.stderr or '').strip()[:200]}")
    pairs = set()
    # -z: "key\nvalue\0" per entry, "key\0" for a key without a value.
    for entry in (proc.stdout or "").split("\0"):
        if entry:
            key, newline, value = entry.partition("\n")
            pairs.add((key, value if newline else None))
    return pairs


def _popen_kwargs(cwd: str, env: Dict[str, str], timeout: float) -> dict:
    kwargs = {"cwd": cwd, "env": env, "capture_output": True, "text": True, "timeout": timeout,
              "encoding": "utf-8", "errors": "replace", "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kwargs


def _command(args: Sequence[str], pin: _Pin, env: Dict[str, str], timeout: float,
             linked_worktree: bool) -> List[str]:
    """The argv for `args` on `pin`'s repository; every read on the way runs on `pin` too."""
    caller_config, sub, rest = _split_args(args)
    layout = pin.layout(sub[0] if sub else "", linked_worktree)
    reads = pin.layout(sub[0] if sub else "", linked_worktree, read=True)
    pinned: List[str] = ["-c", f"core.hooksPath={empty_hooks_dir()}"]
    for key, value in PROJECT_GIT_CONFIG:
        pinned += ["-c", f"{key}={value}"]
    for section, names in _untrusted_drivers(pin, env, timeout, reads, layout).items():
        for name in names:
            if _UNSAFE_DRIVER_NAME.search(name):
                raise ValueError(f"the repository defines a {section} driver whose name cannot "
                                 f"be switched off on a command line: {name!r}")
            for var, value in _DRIVER_OVERRIDES.get(section, ()):
                pinned += ["-c", f"{section}.{name}.{var}={value}"]
    added = list(_SUBCOMMAND_FLAGS.get(sub[0], ())) if sub else []
    if sub and sub[0] in ("add", "stash"):
        links = _gitlinks(pin, reads, pinned, env, timeout)
        if links and sub[0] == "stash":
            raise ProjectGitRefused(f"the project holds another repository at {links[0]}; "
                                    f"git stash would run git inside it")
        if links:
            rest = _add_excluding(rest, links)
    return [resolve_git(), *layout, *caller_config, *pinned, *sub, *added, *rest]


def project_git_command(args: Sequence[str], cwd: str, *, env: Optional[Dict[str, str]] = None,
                        timeout: float = 60, linked_worktree: bool = False) -> List[str]:
    """The full argv `run_project_git` starts for `args` in `cwd`, with the folders spelled
    as paths.

    Order: git, the repository it may work on (`--git-dir`, `--work-tree`), the caller's own
    leading `-c` pairs (an identity for a commit), then the pinned settings, so a pinned value
    always comes last and wins, then the subcommand with its added flags and the rest. Where
    the runner holds the folders by handle (Linux), it starts this same argv with
    `/proc/self/fd/<n>` in place of the two paths. Raises ValueError (ProjectGitRefused among
    them) for a layout the runner will not follow or a driver name that cannot be pinned on a
    command line, RuntimeError when the names cannot be read. Where the private mount lane
    runs (`vaf.core.folder_mount`), the runner starts it inside that namespace, with the top's
    real path as the work tree and the git directory's handle."""
    env = project_git_env() if env is None else env
    pin = _Pin(cwd)
    _, sub, _ = _split_args(args)
    _pin_by_path(pin, sub[0] if sub else "", linked_worktree)
    return _command(args, pin, env, timeout, linked_worktree)


def _gitlinks(pin: _Pin, layout: Sequence[str], pinned: Sequence[str], env: Dict[str, str],
              timeout: float) -> List[str]:
    """Repository-relative paths of the index's gitlinks (submodules and embedded
    repositories). `git add` over one, and `git stash create` always, start a child git in it
    that reads that repository's own config, filters included, whatever diff.ignoreSubmodules
    says (measured). Reading the index starts nothing."""
    argv = [resolve_git(), *layout, *pinned, "ls-files", "-s", "-z", "--full-name", "--", ":/"]
    proc = pin.run(argv, env, timeout, read=True)
    if proc.returncode != 0:
        raise RuntimeError(f"cannot read the index: {(proc.stderr or '').strip()[:200]}")
    links = []
    for entry in (proc.stdout or "").split("\0"):
        head, _, path = entry.partition("\t")
        if path and head.split(" ", 1)[0] == "160000":
            links.append(path)
    return links


def _add_excluding(rest: List[str], links: Sequence[str]) -> List[str]:
    """`git add` arguments with every gitlink excluded, so add starts no git inside one.
    Their recorded commit is then not updated by VAF's commits."""
    if "--" in rest:
        cut = rest.index("--")
        opts, paths = rest[:cut], rest[cut + 1:]
    else:
        cut = next((i for i, a in enumerate(rest) if not a.startswith("-")), len(rest))
        opts, paths = rest[:cut], rest[cut:]
    if not paths:
        if not any(o in ("-A", "--all", "-u", "--update") for o in opts):
            return rest        # nothing named: git refuses on its own
        paths = [":/"]         # the whole tree, as -A and -u mean without a pathspec
    return [*opts, "--", *paths, *(f":(top,exclude,literal){p}" for p in links)]


def run_project_git(args: Sequence[str], cwd: str, *, timeout: float = 60,
                    linked_worktree: bool = False,
                    expect_folder=None) -> subprocess.CompletedProcess:
    """Run git in a folder whose `.git` someone else may have written, starting nothing but git.

    `args` is what follows `git`, optionally led by `-c key=value` pairs of the caller's own.
    Hooks, the fsmonitor, pagers, editors, askpass, credential helpers, signing, external diff
    and textconv, and the filter and merge drivers the repository's config names are all off;
    diffs and logs get --no-ext-diff --no-textconv and commits --no-verify. git works on the
    repository whose `.git` is at or nearest above `cwd`, and on its folder as the work tree,
    whatever that config says; a `.git` that is a file or a link, or that leads into another
    repository, is refused unless `linked_worktree=True` (then only the work tree is pinned).

    git runs IN that top folder: relative paths and pathspecs are read from the top, not from
    `cwd`. The folder is held for the whole call (see `_Pin`): every git process of it works
    on the folder that was opened at the start, whatever is renamed or linked in its place
    meanwhile. `expect_folder` is the REAL path a caller judged for `cwd` (no links on the
    way, compared as given and never resolved): a `cwd` that is any other folder when it is
    opened is refused, so a folder swapped for a link between the caller's check and this
    call is never worked on. With `expect_folder` the folder must hold `.git` itself: git is
    never run on a repository above the judged folder, which nobody judged.

    Never raises: a missing git, a timeout or a refusal comes back as returncode -1 with the
    reason in stderr.
    """
    from vaf.core import path_jail
    argv: List[str] = ["git", *args]
    _, sub, _ = _split_args(args)
    pin = _Pin(cwd)
    try:
        env = project_git_env()
        if path_jail._DIR_FD:
            _pin_by_handle(pin, sub[0] if sub else "", linked_worktree, expect_folder)
        else:
            if expect_folder is not None:
                pin.expected = os.path.normcase(os.path.abspath(os.fspath(expect_folder)))
            if not os.path.isdir(cwd):
                raise _NoFolder()
            pin.check()
            _pin_by_path(pin, sub[0] if sub else "", linked_worktree)
        argv = _command(args, pin, env, timeout, linked_worktree)
        proc = pin.run(argv, env, timeout)
        if pin.namespace is not None and proc.returncode != 0 and _EROFS in (proc.stderr or ""):
            proc.stderr = (proc.stderr or "") + _EROFS_HINT
        return proc
    except _NoFolder:
        # subprocess reports a missing cwd as FileNotFoundError, which reads as "no git".
        return subprocess.CompletedProcess(argv, -1, "", f"Not a directory: {cwd}")
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, -1, "", "Git is not installed.")
    except Exception as e:
        return subprocess.CompletedProcess(argv, -1, "", str(e))
    finally:
        pin.close()
