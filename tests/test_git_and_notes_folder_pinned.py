# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""git and the project notes work on the folder that was checked, not on what its name
leads to a moment later (vaf.core.git_runner.run_project_git, vaf.core.persistence).

Measured before the change: a process of the account that renamed its own project folder
aside, put a link into another account's repository in its place and swapped back made
`git_log` print the other account's history in 21 of 300 calls; a fresh notes manager left an
empty `.vaf` in the other folder in 6 of 1076. The swaps here are deterministic: made inside a
patched function at the moment that matters, in two scratch repositories, A (the caller's own,
subject A-OWN, untracked A_ONLY) and B (another account's, subject B-SECRET, untracked B_ONLY).
"""
import ast
import inspect
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import vaf.core.git_runner as gr
from vaf.core import folder_mount, path_jail

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

_ROOT = Path(__file__).resolve().parent.parent
A = "ab12cd34-0000-4000-8000-00000000000a"
B = "cd34ef56-0000-4000-8000-00000000000b"

_PROC = pytest.mark.skipif(not os.path.isdir("/proc/self/fd"),
                           reason="the handle branch needs /proc/self/fd (Linux)")
_HANDLES = pytest.mark.skipif(not path_jail._DIR_FD, reason="no folder handles here")


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _symlink_or_skip(link, target):
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


def _repo(path: Path, subject: str, untracked: str) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.name", "T", cwd=path)
    _git("config", "user.email", "t@example.org", cwd=path)
    (path / "f.txt").write_text(f"{subject}\n", encoding="utf-8")
    (path / "sub").mkdir()
    (path / "sub" / "x.txt").write_text("x1\n", encoding="utf-8")
    _git("add", "-A", cwd=path)
    _git("-c", "core.hooksPath=", "commit", "-qm", subject, cwd=path)
    (path / untracked).write_text("untracked\n", encoding="utf-8")
    return path.resolve()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A scratch home with two accounts' project trees: A's repository and B's."""
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(var, str(home / ".xdg" / var.lower()))
    for var in ("VAF_USER_SCOPE_ID", "VAF_USER_ROLE", "VAF_IN_SUBAGENT_TERMINAL",
                "VAF_SESSION_ID", "VAF_PARENT_CWD"):
        monkeypatch.delenv(var, raising=False)
    import vaf.core.subagent_ipc as ipc
    import vaf.tools.filesystem as fs
    from vaf.core.session import get_user_projects_root
    from vaf.core.tool_dispatch import set_account_allowlist_resolver
    fs._shared_room_roots_cache.clear()
    monkeypatch.setattr(fs, "_shared_room_roots", lambda scope: [])
    monkeypatch.setattr(ipc, "get_current_session_id", lambda: None)
    set_account_allowlist_resolver(None)
    a = _repo(get_user_projects_root(A) / "site", "A-OWN", "A_ONLY")
    b = _repo(get_user_projects_root(B) / "proj", "B-SECRET", "B_ONLY")
    yield SimpleNamespace(tmp=tmp_path, a=a, b=b)
    set_account_allowlist_resolver(None)
    fs._shared_room_roots_cache.clear()


def _swap(folder: Path, target: Path) -> Path:
    """Rename ``folder`` aside and put a link to ``target`` in its place."""
    aside = folder.with_name(folder.name + "-aside")
    os.rename(folder, aside)
    _symlink_or_skip(folder, target)
    return aside


def _swap_mid_call(monkeypatch, folder: Path, target: Path):
    """Swap ``folder`` for a link to ``target`` once, after the runner opened the folder and
    before the first git process starts (the driver read)."""
    real = gr._untrusted_drivers
    done = []

    def _swapping(*args, **kwargs):
        if not done:
            done.append(_swap(folder, target))
        return real(*args, **kwargs)

    monkeypatch.setattr(gr, "_untrusted_drivers", _swapping)
    return done


@pytest.fixture
def no_namespace(monkeypatch):
    """The handle pin as it runs where the private mount namespace does not
    (`vaf.core.folder_mount`, tested with the lane on in tests/test_folder_mount.py)."""
    monkeypatch.setattr(folder_mount, "namespace_available", lambda: False)


# ── 1. the primitive ─────────────────────────────────────────────────────────

@_HANDLES
def test_open_folder_returns_the_kernel_path_and_refuses_a_link_on_the_way(world):
    """MUTATION: drop the expect_folder comparison in open_folder - red."""
    fd, real = path_jail.open_folder(str(world.a))
    os.close(fd)
    assert real == str(world.a)
    link = world.tmp / "link"
    _symlink_or_skip(link, world.a)
    fd, real = path_jail.open_folder(str(link))
    os.close(fd)
    assert real == str(world.a)
    with pytest.raises(path_jail.PathEscape, match="a link on the way"):
        path_jail.open_folder(str(link), expect_folder=str(link))
    with pytest.raises(path_jail.PathEscape):
        path_jail.open_folder(str(link), follow=False)
    top = os.open(str(world.a), os.O_RDONLY)
    try:
        fd, real = path_jail.open_folder("sub", dir_fd=top, expect_folder=str(world.a / "sub"))
        os.close(fd)
    finally:
        os.close(top)
    assert real == str(world.a / "sub")
    assert "open_folder" in path_jail.__all__


def test_the_two_hand_copies_use_the_primitive():
    from vaf.core.persistence import PersistenceManager
    for fn in (path_jail._open_in_folder, path_jail.open_for_read):
        src = inspect.getsource(fn)
        assert "open_folder(" in src, fn.__qualname__
        assert "O_DIRECTORY" not in src and "_fd_path" not in src, fn.__qualname__
    # The notes reader holds no copy of its own: the read primitive carries both branches.
    src = inspect.getsource(PersistenceManager._read_file)
    assert "open_for_read(" in src
    assert "os.open(" not in src and "lstat" not in src and "samestat" not in src
    import vaf
    assert not hasattr(vaf, "open_folder") and "open_folder" not in vaf.__all__


# ── 2. every process of one call is pinned ──────────────────────────────────

@_PROC
@pytest.mark.parametrize("args, count", [(["status", "--porcelain"], 4), (["add", "-A"], 5)])
def test_every_git_process_of_a_call_runs_through_the_handles(world, monkeypatch, args, count,
                                                              no_namespace):
    """The global, system and effective config reads, the gitlinks read (add) and the command,
    called from a subfolder: all in the top folder's handle, with the git directory's handle.
    MUTATION: start the config reads with cwd=<path> (one process unpinned) - red."""
    real_run = subprocess.run
    seen = []

    def _recording(argv, **kw):
        cwd = kw["cwd"]
        git_dir = next(a.split("=", 1)[1] for a in argv if a.startswith("--git-dir="))
        work_tree = next(a.split("=", 1)[1] for a in argv if a.startswith("--work-tree="))
        seen.append((cwd, git_dir, work_tree, kw.get("env", {}).get("GIT_COMMON_DIR"),
                     tuple(sorted(kw.get("pass_fds", ()))),
                     os.readlink(cwd) if cwd.startswith("/proc/self/fd/") else cwd))
        return real_run(argv, **kw)

    monkeypatch.setattr(gr.subprocess, "run", _recording)
    proc = gr.run_project_git(args, cwd=str(world.a / "sub"))
    assert proc.returncode == 0, proc.stderr
    assert len(seen) == count
    for cwd, git_dir, work_tree, common, fds, real in seen:
        assert cwd.startswith("/proc/self/fd/") and work_tree == cwd
        assert git_dir.startswith("/proc/self/fd/") and common == git_dir
        assert fds == tuple(sorted((int(cwd.rsplit("/", 1)[1]), int(git_dir.rsplit("/", 1)[1]))))
        assert real == str(world.a)          # git runs in the top folder
    assert len({s[:3] for s in seen}) == 1   # one folder for the whole call


# ── 3. to 5. a swap during the call, and before it ──────────────────────────

@_PROC
@pytest.mark.parametrize("args, own, other", [
    (["log", "--oneline"], "A-OWN", "B-SECRET"),
    (["status", "--porcelain", "--untracked-files=all"], "A_ONLY", "B_ONLY"),
])
def test_a_folder_swapped_after_it_was_opened_is_not_followed(world, monkeypatch, args, own,
                                                             other, no_namespace):
    """MUTATION: layout and working folder by path text again - red (B's output)."""
    done = _swap_mid_call(monkeypatch, world.a, world.b)
    proc = gr.run_project_git(args, cwd=str(world.a))
    assert done, "the swap never ran"
    assert proc.returncode == 0, proc.stderr
    assert own in proc.stdout and other not in proc.stdout


@_HANDLES
@pytest.mark.parametrize("args, other", [(["log", "--oneline"], "B-SECRET"),
                                         (["status", "--porcelain"], "B_ONLY")])
def test_a_folder_that_is_a_link_when_the_call_starts_is_refused(world, args, other):
    """MUTATION: ignore expect_folder in the runner - red."""
    expected = str(world.a)
    _swap(world.a, world.b)
    proc = gr.run_project_git(args, cwd=expected, expect_folder=expected)
    assert proc.returncode == -1 and "a link on the way" in proc.stderr, proc.stderr
    assert other not in proc.stdout + proc.stderr


@_HANDLES
def test_init_in_a_folder_that_is_a_link_creates_nothing_there(world):
    """MUTATION: ignore expect_folder in the runner - red (a .git in the other folder)."""
    other = world.b.parent / "empty"
    other.mkdir()
    fresh = world.a.parent / "fresh"
    fresh.mkdir()
    expected = str(fresh.resolve())
    _swap(fresh, other)
    proc = gr.run_project_git(["init"], cwd=expected, expect_folder=expected)
    assert proc.returncode == -1 and "a link on the way" in proc.stderr, proc.stderr
    assert not (other / ".git").exists()


@_PROC
def test_a_git_folder_swapped_after_the_check_is_not_followed(world, monkeypatch):
    """The git directory is its own handle. MUTATION: --git-dir=/proc/self/fd/<top>/.git (by
    name below the pinned top) - red."""
    real = gr._untrusted_drivers
    done = []

    def _swapping(*args, **kwargs):
        if not done:
            os.rename(world.a / ".git", world.a / ".git-aside")
            _symlink_or_skip(world.a / ".git", world.b / ".git")
            done.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(gr, "_untrusted_drivers", _swapping)
    proc = gr.run_project_git(["log", "--oneline"], cwd=str(world.a))
    assert done and proc.returncode == 0, proc.stderr
    assert "A-OWN" in proc.stdout and "B-SECRET" not in proc.stdout


# ── 6. the git tools: the folder they return is the folder they judged ──────

@_HANDLES
def test_a_folder_swapped_between_the_judgment_and_the_resolution_is_refused(world,
                                                                             monkeypatch):
    """MUTATION: tool_work_folder returns the resolved folder without judging it again - red
    (git_log prints B's history)."""
    import vaf.core.workspace_guard as wg
    from vaf.tools.coder import GitLogTool
    real = wg.project_dir_refusal
    done = []

    def _swapping(path, **kwargs):
        answer = real(path, **kwargs)
        if not done and str(path) == str(world.a):
            done.append(_swap(world.a, world.b))
        return answer

    monkeypatch.setattr(wg, "project_dir_refusal", _swapping)
    out = GitLogTool().run(base_dir=str(world.a), user_scope_id=A, user_role="user")
    assert done, "the swap never ran"
    assert out.startswith("❌ Refused"), out
    assert "B-SECRET" not in out


# ── 7. every caller passes the folder it judged ─────────────────────────────

@pytest.mark.parametrize("rel", ["vaf/tools/coder.py", "vaf/tools/project_git.py"])
def test_every_project_git_call_names_the_folder_it_judged(rel):
    """MUTATION: drop expect_folder= from one call - red."""
    tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name in ("run_project_git", "resolve_commit", "commit_project_state"):
            if not any(kw.arg == "expect_folder" for kw in node.keywords):
                missing.append(node.lineno)
    assert missing == [], f"{rel}: calls without expect_folder at lines {missing}"


def test_the_coder_and_the_history_work_in_the_resolved_folder(world):
    """A folder named through a link inside the account's own tree is allowed and comes
    back as its real path, which the run's git calls compare with. MUTATION: return the
    abspath from _choose_base_dir (or from _resolve_project) - red."""
    from vaf.tools.coder import CodingAgentTool, _choose_base_dir
    from vaf.tools.project_git import _resolve_project
    link = world.a.parent / "via-link"
    _symlink_or_skip(link, world.a)
    choice = _choose_base_dir(CodingAgentTool(), "build it", str(link), scope=A, role="user",
                              content_only=False)
    assert choice.refusal == "" and choice.base_dir == str(world.a)
    assert _resolve_project(str(link), user_scope_id=A, user_role="user") == (str(world.a), "")
    src = inspect.getsource(CodingAgentTool.run)
    assert "os.path.realpath(tempfile.mkdtemp(" in src


# ── 8. the notes folder is made through the checked handle ──────────────────

def _swap_on_mkdir(monkeypatch, folder: Path, target: Path):
    real = os.mkdir
    done = []

    def _swapping(*args, **kwargs):
        if not done:
            done.append(_swap(folder, target))
        return real(*args, **kwargs)

    monkeypatch.setattr(os, "mkdir", _swapping)
    return done, real


@_HANDLES
def test_a_notes_folder_made_again_never_lands_in_a_swapped_in_folder(world, monkeypatch):
    """MUTATION: _recreate makes the folder by path (os.mkdir(folder)) - red."""
    from vaf.core.path_jail import WriteRefused
    from vaf.core.persistence import PersistenceManager
    other = world.b.parent / "notes-free"
    other.mkdir()
    pm = PersistenceManager(str(world.a), expect_folder=str(world.a))
    assert pm.notes_folder() == str(world.a / ".vaf")
    shutil.rmtree(world.a / ".vaf")
    done, real_mkdir = _swap_on_mkdir(monkeypatch, world.a, other)
    with pytest.raises(WriteRefused):
        pm.append_memory("SWAPPED")
    monkeypatch.setattr(os, "mkdir", real_mkdir)
    assert done, "the swap never ran"
    assert not (other / ".vaf").exists()


@_HANDLES
def test_a_fresh_notes_manager_never_makes_a_folder_in_a_swapped_in_folder(world, monkeypatch):
    """MUTATION: __init__ makes .vaf by path again (os.makedirs(self.vaf_path)) - red."""
    from vaf.core.persistence import PersistenceManager
    other = world.b.parent / "notes-free"
    other.mkdir()
    done, real_mkdir = _swap_on_mkdir(monkeypatch, world.a, other)
    pm = PersistenceManager(str(world.a), expect_folder=str(world.a))
    monkeypatch.setattr(os, "mkdir", real_mkdir)
    assert done, "the swap never ran"
    assert not (other / ".vaf").exists()
    assert pm.notes_folder() is None


def test_no_folder_is_made_by_path_outside_the_windows_branch():
    """Every mkdir/makedirs in persistence.py is relative to a folder handle, except in the
    two Windows methods (`*_by_path`)."""
    tree = ast.parse((_ROOT / "vaf" / "core" / "persistence.py").read_text(encoding="utf-8"))
    by_path = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name.endswith("_by_path"):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("mkdir", "makedirs")
                    and not any(kw.arg == "dir_fd" for kw in node.keywords)):
                by_path.append((fn.name, node.lineno))
    assert by_path == []


def test_a_missing_project_folder_is_made_with_its_notes_folder(world):
    """update_codex and add_memory may name a project folder that does not exist yet."""
    from vaf.core.persistence import PersistenceManager
    fresh = world.a.parent / "new" / "deeper"
    pm = PersistenceManager(str(fresh), expect_folder=str(fresh))
    assert pm.notes_folder() == str(fresh / ".vaf")
    pm.append_memory("first")
    assert "first" in (fresh / ".vaf" / "memory.md").read_text(encoding="utf-8")


# ── 9. the path branches (macOS, Windows), simulated ────────────────────────

@pytest.fixture(params=["path", "windows"])
def branch(request, monkeypatch):
    monkeypatch.setattr(gr, "_proc_fd_usable", lambda fd: False)
    if request.param == "windows":
        monkeypatch.setattr(path_jail, "_DIR_FD", False)
    return request.param


@pytest.mark.skipif(os.name == "nt", reason="symlinks to folders need privileges there")
def test_the_path_branches_refuse_a_folder_swapped_before_the_call(world, branch):
    expected = str(world.a)
    assert gr.run_project_git(["log", "--oneline"], cwd=expected,
                              expect_folder=expected).returncode == 0
    _swap(world.a, world.b)
    proc = gr.run_project_git(["log", "--oneline"], cwd=expected, expect_folder=expected)
    assert proc.returncode == -1 and "a link on the way" in proc.stderr, proc.stderr
    assert "B-SECRET" not in proc.stdout


@pytest.mark.skipif(os.name == "nt", reason="symlinks to folders need privileges there")
def test_the_path_branches_refuse_a_folder_swapped_during_the_call(world, branch,
                                                                    monkeypatch):
    """Checked again before each process. MUTATION: drop the comparison in _Pin.check for
    that branch - red (B's history)."""
    expected = str(world.a)
    done = _swap_mid_call(monkeypatch, world.a, world.b)
    proc = gr.run_project_git(["log", "--oneline"], cwd=expected, expect_folder=expected)
    assert done, "the swap never ran"
    assert proc.returncode == -1 and "a link on the way" in proc.stderr, proc.stderr
    assert "B-SECRET" not in proc.stdout


def test_the_path_branches_without_expect_folder_still_run(world, branch):
    proc = gr.run_project_git(["log", "--oneline"], cwd=str(world.a / "sub"))
    assert proc.returncode == 0 and "A-OWN" in proc.stdout, proc.stderr


# ── 10. no handle is left open ──────────────────────────────────────────────

@_PROC
def test_no_file_descriptor_is_left_open(world):
    """MUTATION: drop the os.close of the pin's handles - red."""
    elsewhere = str(world.b)
    nogit = world.tmp / "nogit"
    nogit.mkdir()

    def _count():
        return len(os.listdir("/proc/self/fd"))

    gr.run_project_git(["log", "--oneline"], cwd=str(world.a))   # warm-up (hooks dir etc.)
    before = _count()
    for i in range(50):
        if i % 2:
            assert gr.run_project_git(["log", "--oneline"], cwd=str(world.a / "sub")).returncode == 0
        elif i % 4:
            assert gr.run_project_git(["log"], cwd=str(world.a),
                                      expect_folder=elsewhere).returncode == -1
        else:
            assert gr.run_project_git(["log"], cwd=str(nogit)).returncode == -1
    assert _count() == before


# ── 11. /undo in a subfolder, and what rev-parse prints ─────────────────────

def test_undo_in_a_subfolder_restores_only_that_subfolder(world):
    """git runs in the top folder now, so `checkout -- .` would restore the whole repository.
    MUTATION: checkout <commit> -- . in Snapshot._git_restore - red."""
    from vaf.core.snapshot import Snapshot
    head = _git("rev-parse", "HEAD", cwd=world.a).stdout.strip()
    snap = Snapshot(str(world.a / "sub"))
    (world.a / "sub" / "x.txt").write_text("x2\n", encoding="utf-8")
    (world.a / "f.txt").write_text("changed\n", encoding="utf-8")
    assert snap._git_restore(head) is True
    assert (world.a / "sub" / "x.txt").read_text(encoding="utf-8") == "x1\n"
    assert (world.a / "f.txt").read_text(encoding="utf-8") == "changed\n"


def test_rev_parse_prints_real_paths_under_the_pin(world):
    sub = str(world.a / "sub")
    top = gr.run_project_git(["rev-parse", "--show-toplevel"], cwd=sub).stdout.strip()
    git_dir = gr.run_project_git(["rev-parse", "--absolute-git-dir"], cwd=sub).stdout.strip()
    assert Path(top) == world.a and Path(git_dir) == world.a / ".git"


# ── 12. no walk above a judged folder ───────────────────────────────────────

@pytest.fixture(params=["handle", "windows"])
def any_branch(request, monkeypatch):
    if request.param == "windows":
        monkeypatch.setattr(path_jail, "_DIR_FD", False)
    elif not path_jail._DIR_FD:
        pytest.skip("no folder handles here")
    return request.param


def test_a_judged_folder_without_its_own_repository_is_refused(world, any_branch):
    """With a `.git` renamed away (or never made) in the judged folder, the runner walked up to
    the repository above it, which nobody judged: `log` printed its history and `add -A`
    would stage its whole tree. MUTATION: drop the `.git` requirement under expect_folder
    (in that branch) - red."""
    inner = world.a / "inner"
    inner.mkdir()
    expected = str(inner)
    proc = gr.run_project_git(["log", "--oneline"], cwd=expected, expect_folder=expected)
    assert proc.returncode == -1 and "holds no .git of its own" in proc.stderr, proc.stderr
    assert "A-OWN" not in proc.stdout
    # Without expect_folder (/undo in a subfolder) the repository above still serves.
    assert "A-OWN" in gr.run_project_git(["log", "--oneline"], cwd=expected).stdout


@_HANDLES
def test_a_top_renamed_before_its_git_folder_is_opened_names_the_move(world, monkeypatch):
    """The refusal says what happened ("a link on the way"), not a layout problem.
    MUTATION: let FolderMoved fall into the linked-worktree refusal - red."""
    real = gr._has_git_entry
    done = []

    def _renaming(fd):
        answer = real(fd)
        if answer and not done:
            done.append(os.rename(world.a, world.a.with_name("site-moved")))
        return answer

    monkeypatch.setattr(gr, "_has_git_entry", _renaming)
    proc = gr.run_project_git(["log", "--oneline"], cwd=str(world.a))
    assert done, "the rename never ran"
    assert proc.returncode == -1 and "a link on the way" in proc.stderr, proc.stderr
    assert "linked worktree" not in proc.stderr


@_HANDLES
@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs a folder this process cannot open")
def test_a_folder_that_cannot_be_opened_is_named_with_its_reason(world):
    """MUTATION: report every OSError of the open as "Not a directory" - red."""
    locked = world.a / "locked"
    locked.mkdir(mode=0o000)
    try:
        proc = gr.run_project_git(["status"], cwd=str(locked))
    finally:
        locked.chmod(0o700)
    assert proc.returncode == -1 and "cannot open" in proc.stderr, proc.stderr
    assert "Not a directory" not in proc.stderr


@pytest.mark.skipif(os.name == "nt", reason="symlinks to folders need privileges there")
def test_the_path_branches_refuse_init_in_a_folder_swapped_during_the_call(world, branch,
                                                                         monkeypatch):
    """MUTATION: treat the top's samestat as always the same (path branch), or drop the
    resolved-folder comparison (Windows branch) - red (a .git in the other folder)."""
    other = world.b.parent / "empty"
    other.mkdir()
    fresh = world.a.parent / "fresh"
    fresh.mkdir()
    expected = str(fresh.resolve())
    done = _swap_mid_call(monkeypatch, fresh, other)
    proc = gr.run_project_git(["init"], cwd=expected, expect_folder=expected)
    assert done, "the swap never ran"
    assert proc.returncode == -1 and "a link on the way" in proc.stderr, proc.stderr
    assert not (other / ".git").exists()


def test_the_windows_notes_folder_is_not_taken_from_a_folder_swapped_during_creation(
        world, monkeypatch):
    """Windows makes .vaf by path: a project folder swapped for a link while makedirs runs
    must not become the notes folder. MUTATION: compare with the project folder resolved
    anew instead of expect_folder - red (the other folder's .vaf is used)."""
    from vaf.core.persistence import PersistenceManager
    monkeypatch.setattr(path_jail, "_DIR_FD", False)
    other = world.b.parent / "notes-free"
    other.mkdir()
    real = os.makedirs
    done = []

    def _swapping(*args, **kwargs):
        if not done:
            done.append(_swap(world.a, other))
        return real(*args, **kwargs)

    monkeypatch.setattr(os, "makedirs", _swapping)
    pm = PersistenceManager(str(world.a), expect_folder=str(world.a))
    monkeypatch.setattr(os, "makedirs", real)
    assert done, "the swap never ran"
    assert pm.notes_folder() is None


# ── 13. a .git removed after the runner's own presence check ────────────────

def _inner_repo(world) -> Path:
    """A repository of its own inside A's: the judged folder, with A's above it."""
    return _repo(world.a / "inner", "INNER-OWN", "INNER_ONLY")


@_HANDLES
def test_a_git_folder_gone_after_the_presence_check_does_not_lead_upwards(world, monkeypatch):
    """Handle branch: `.git` renamed away right after the runner saw it. MUTATION: walk
    upwards under expect_folder again (`while not _has_git_entry(fd)`) - red (A's history)."""
    inner = _inner_repo(world)
    real = gr._has_git_entry
    done = []

    def _vanishing(fd):
        answer = real(fd)
        if answer and not done:
            done.append(os.rename(inner / ".git", inner / ".git-aside"))
        return answer

    monkeypatch.setattr(gr, "_has_git_entry", _vanishing)
    proc = gr.run_project_git(["log", "--oneline"], cwd=str(inner), expect_folder=str(inner))
    assert done, "the rename never ran"
    assert proc.returncode != 0, proc.stdout
    assert "A-OWN" not in proc.stdout


def test_a_git_folder_gone_after_the_presence_check_does_not_lead_upwards_by_path(
        world, monkeypatch):
    """Windows branch (taken on any host): `.git` renamed away right after the presence
    check. MUTATION: find the top by walking up (`_top`) under expect_folder again - red."""
    monkeypatch.setattr(path_jail, "_DIR_FD", False)
    inner = _inner_repo(world)
    dotgit = os.path.join(str(inner), ".git")
    real = os.path.lexists
    done = []

    def _vanishing(path):
        answer = real(path)
        if answer and not done and os.fspath(path) == dotgit:
            done.append(os.rename(dotgit, dotgit + "-aside"))
        return answer

    monkeypatch.setattr(os.path, "lexists", _vanishing)
    proc = gr.run_project_git(["log", "--oneline"], cwd=str(inner), expect_folder=str(inner))
    monkeypatch.setattr(os.path, "lexists", real)
    assert done, "the rename never ran"
    assert proc.returncode != 0, proc.stdout
    assert "A-OWN" not in proc.stdout


def test_a_linked_worktree_under_expect_folder_is_not_searched_above(world, any_branch,
                                                                     monkeypatch):
    """With `linked_worktree=True` git finds the `.git` file itself; one removed during the
    call must not let it find the repository above. MUTATION: drop GIT_CEILING_DIRECTORIES
    - red (A's history)."""
    wt = world.a / "wt"
    _git("worktree", "add", "-q", "-b", "side", str(wt), cwd=world.a)
    wt = wt.resolve()
    real = gr._untrusted_drivers
    done = []

    def _removing(*args, **kwargs):
        if not done:
            done.append(os.rename(wt / ".git", wt / "git-file-aside"))
        return real(*args, **kwargs)

    monkeypatch.setattr(gr, "_untrusted_drivers", _removing)
    proc = gr.run_project_git(["log", "--oneline"], cwd=str(wt), expect_folder=str(wt),
                              linked_worktree=True)
    assert done, "the removal never ran"
    assert proc.returncode != 0, proc.stdout
    assert "A-OWN" not in proc.stdout


# ── 14. the notes read on Windows: the opened file is the checked one ───────

@pytest.mark.parametrize("swapped", ["file", "folder"])
def test_the_windows_notes_read_refuses_a_link_present_only_during_the_open(world, monkeypatch,
                                                                           swapped):
    """MUTATION: drop the samestat comparison of the opened file - red (B's text read)."""
    from vaf.core.persistence import PersistenceManager
    monkeypatch.setattr(path_jail, "_DIR_FD", False)
    secret = world.b / ".vaf"
    secret.mkdir()
    (secret / "memory.md").write_text("B-NOTES\n", encoding="utf-8")
    pm = PersistenceManager(str(world.a), expect_folder=str(world.a))
    vaf = world.a / ".vaf"
    (vaf / "memory.md").write_text("A-NOTES\n", encoding="utf-8")
    target = vaf / "memory.md" if swapped == "file" else vaf
    aside = target.with_name(target.name + "-aside")
    link_to = secret / "memory.md" if swapped == "file" else secret
    real_open = os.open
    done = []

    def _swapping(path, flags, *args, **kwargs):
        if os.fspath(path).endswith("memory.md") and not done:
            os.rename(target, aside)
            try:
                os.symlink(link_to, target, target_is_directory=(swapped == "folder"))
            except (OSError, NotImplementedError):
                os.rename(aside, target)
                pytest.skip("symlinks are not available here")
            done.append(True)
            try:
                return real_open(path, flags, *args, **kwargs)
            finally:
                os.unlink(target)
                os.rename(aside, target)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _swapping)
    text = pm.get_memory()
    monkeypatch.setattr(os, "open", real_open)
    assert done, "the swap never ran"
    assert "B-NOTES" not in text


def test_a_linked_worktree_whose_parent_git_cannot_be_limited_is_refused(world, any_branch,
                                                                       monkeypatch):
    """git splits GIT_CEILING_DIRECTORIES on os.pathsep, so a parent named with one sets no
    limit and the search goes on upwards. MUTATION: use the parent as the ceiling without the
    check - red (the repository above prints its history)."""
    holder = world.a.parent / f"pa{os.pathsep}rt"
    try:
        holder.mkdir()
    except OSError:
        pytest.skip("this file system refuses the separator in a name")
    parent = _repo(holder / "repo", "PARENT-OWN", "P_ONLY")
    wt = parent / "wt"
    _git("worktree", "add", "-q", "-b", "side", str(wt), cwd=parent)
    wt = wt.resolve()
    real = gr._untrusted_drivers
    done = []

    def _removing(*args, **kwargs):
        if not done:
            done.append(os.rename(wt / ".git", wt / "git-file-aside"))
        return real(*args, **kwargs)

    monkeypatch.setattr(gr, "_untrusted_drivers", _removing)
    proc = gr.run_project_git(["log", "--oneline"], cwd=str(wt), expect_folder=str(wt),
                              linked_worktree=True)
    assert proc.returncode != 0, proc.stdout
    assert "PARENT-OWN" not in proc.stdout
