# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A checked folder handed to a child process, proven inside the child to be that folder
(vaf.core.folder_mount), and its four users: git, the coder's jailed shell (its argv is pinned
in tests/test_workspace_exec.py), the shell's docker fallback and a sandbox environment's mount
(tests/test_environments.py).

Two scratch repositories, A (the caller's own: subject A-OWN, untracked A_ONLY) and B (another
account's: B-SECRET, B_ONLY), from the git pin's suite. The swaps are deterministic: made at
the moment that matters, inside a patched start. The tests that start bubblewrap skip where
the lane does not run (GitHub's Linux runners have no bwrap); the rest run everywhere the
guard's `sh` and `stat` do (Linux)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import vaf.core.git_runner as gr
import vaf.tools.workspace_exec as wx
from vaf.core import containers, folder_mount, path_jail
from test_git_and_notes_folder_pinned import A, _git, _swap, world  # noqa: F401 - fixture

_LINUX = pytest.mark.skipif(not sys.platform.startswith("linux"),
                            reason="the guard runs only where the lane does (Linux)")
_LANE = pytest.mark.skipif(not folder_mount.namespace_available(),
                           reason=f"no private mount namespace here: "
                                  f"{folder_mount.namespace_unavailable_reason()}")
_NO_ACCOUNT = {"user_scope_id": None, "user_role": None}


def _identity(path) -> str:
    st = os.stat(path)
    return f"{st.st_dev}:{st.st_ino}"


def _read_started(r: int) -> bytes:
    os.set_blocking(r, False)
    try:
        return os.read(r, 1)
    except BlockingIOError:
        return b""


# ── 1. the namespace's shape ────────────────────────────────────────────────

@pytest.mark.skipif(not path_jail._DIR_FD, reason="no folder handles here")
def test_the_namespace_has_a_root_of_its_own_and_binds_the_folder_last(tmp_path):
    """A shared root (`--dev-bind / /`) shows a swap made on the host after the mount, and
    wrote empty folders on the host (measured). MUTATION: `--dev-bind / /` in namespace_argv
    - red."""
    with folder_mount.HeldFolder(tmp_path) as held:
        argv = folder_mount.namespace_argv(held, "/work", extra_ro=["/ro/one"])
    assert argv[0] == "bwrap" and argv[-1] == "--"
    assert "--dev-bind" not in argv and "--bind-try" not in argv
    assert argv[argv.index("--tmpfs") + 1] == "/tmp"
    bind = argv.index("--bind")
    assert argv[bind + 1:bind + 3] == [str(tmp_path.resolve()), "/work"]
    assert bind > argv.index("--tmpfs") and bind > argv.index("--ro-bind-try")
    assert all(argv[i + 1] in folder_mount.SYSTEM_DIRS
               for i, a in enumerate(argv) if a == "--ro-bind")
    # The jailed shell takes the system folders from here: no list of its own.
    src = Path(wx.__file__).read_text(encoding="utf-8")
    assert '"/lib64"' not in src and '"/sbin"' not in src


@pytest.mark.skipif(not path_jail._DIR_FD, reason="no folder handles here")
def test_a_folder_inside_a_read_only_bind_gets_no_namespace(tmp_path):
    """Below another bind the folder shares that bind's view of the host, and a swap there
    showed inside (measured). MUTATION: drop the overlap refusal in namespace_argv - red."""
    inner = tmp_path / "prefix" / "proj"
    inner.mkdir(parents=True)
    with folder_mount.HeldFolder(inner) as held:
        with pytest.raises(ValueError, match="read-only"):
            folder_mount.namespace_argv(held, str(inner), extra_ro=[str(tmp_path / "prefix")])
        assert folder_mount.namespace_argv(held, str(inner), extra_ro=[str(tmp_path / "other")])
    assert folder_mount.overlaps_ro("/opt/x/proj") == "/opt"
    assert folder_mount.overlaps_ro(str(inner), [str(tmp_path)]) == str(tmp_path)
    assert folder_mount.overlaps_ro(str(inner)) == ""


@pytest.mark.skipif(not path_jail._DIR_FD, reason="no folder handles here")
def test_git_and_the_shell_leave_the_lane_for_a_folder_inside_a_read_only_bind(
        world, monkeypatch):
    """MUTATION: ask overlaps_ro nowhere in the git runner - red (the call would start a
    namespace whose view is not private, refused by namespace_argv)."""
    monkeypatch.setattr(folder_mount, "namespace_available", lambda: True)
    monkeypatch.setattr(gr, "_namespace_ro", lambda: [str(world.a.parent)])
    seen = _record(monkeypatch)
    proc = gr.run_project_git(["log", "--oneline"], cwd=str(world.a))
    assert proc.returncode == 0 and "A-OWN" in proc.stdout, proc.stderr
    assert seen and not any(argv[0] == "bwrap" for argv, _ in seen)
    monkeypatch.setattr(folder_mount, "overlaps_ro", lambda real, extra=(): "/opt")
    monkeypatch.setattr(wx, "_bwrap_available", lambda: True)
    monkeypatch.setattr(wx, "_docker_available", lambda: False)
    monkeypatch.setattr(folder_mount, "bwrap_installed", lambda: True)
    monkeypatch.setattr(folder_mount, "namespace_unavailable_reason", lambda: "")
    ran = []
    monkeypatch.setattr(wx, "_run", _starting_run(ran))
    rc, out, err, mode = wx.run_in_workspace(str(world.a), "true", **_NO_ACCOUNT)
    assert (rc, mode) == (-2, "refused") and "binds read-only" in err and ran == []


def test_the_lane_stays_off_the_facade():
    import vaf
    for name in ("HeldFolder", "folder_mount", "namespace_available", "run_guarded"):
        assert not hasattr(vaf, name) and name not in vaf.__all__
    assert "NAMED BOUNDARY, the facade" in folder_mount.__doc__


# ── 2. the guard, in plain sh ───────────────────────────────────────────────

@_LINUX
def test_the_guard_runs_the_payload_only_in_the_checked_folder(tmp_path):
    """MUTATION: end the guard's comparison with `|| true` - red (the payload runs in the
    wrong folder and the pipe holds a byte)."""
    own = tmp_path / "own"
    own.mkdir()
    payload = ["/bin/sh", "-c", "pwd; touch made"]
    for identity, ok in (("1:2", False), (_identity(own), True)):
        r, w = os.pipe()
        try:
            proc = subprocess.run(folder_mount.guard_argv(str(own), identity, payload,
                                                          started_fd=w),
                                  pass_fds=(w,), capture_output=True, text=True, timeout=30)
            os.close(w)
            w = -1
            started = _read_started(r)
        finally:
            os.close(r)
            if w >= 0:
                os.close(w)
        if ok:
            assert proc.returncode == 0 and proc.stdout.strip() == str(own), proc
            assert started == b"x" and (own / "made").exists()
        else:
            assert proc.returncode == 125 and folder_mount.GUARD_MARKER in proc.stderr
            assert "is not the folder that was checked" in proc.stderr
            assert started == b"" and not (own / "made").exists()
    assert folder_mount.guard_argv("/w", "1:2", ["true"])[6] == "-"


def test_the_guard_reports_its_start_without_a_multi_digit_fd_redirection():
    """dash (/bin/sh on Debian and Ubuntu) reads one digit in `>&<n>`: at fd 12 the guard
    exited 2 and ran nothing (measured), and a pipe's number is 10 or more in any process with
    a few files open. This host's /bin/sh may be bash, so the form itself is pinned here.
    MUTATION: back to `printf x >&"$s"` - red."""
    assert '>&"$' not in folder_mount.GUARD and ">&$" not in folder_mount.GUARD
    assert 'exec $s' not in folder_mount.GUARD
    assert '"/proc/self/fd/$s"' in folder_mount.GUARD


@_LINUX
@pytest.mark.parametrize("shell", ["/bin/sh", "dash", "busybox"])
def test_a_started_fd_of_ten_and_more_is_reported_under_every_sh(tmp_path, shell):
    """The started pipe forced above fd 10, under /bin/sh and, where installed, dash and
    busybox's sh (the Ubuntu runner has dash)."""
    import shutil
    prefix = []
    if shell == "dash":
        found = shutil.which("dash")
        if not found:
            pytest.skip("dash is not installed here")
        prefix = None
    elif shell == "busybox":
        found = shutil.which("busybox")
        if not found:
            pytest.skip("busybox is not installed here")
    own = tmp_path / "own"
    own.mkdir()
    filler = [os.open(os.devnull, os.O_RDONLY) for _ in range(12)]
    try:
        def _runner(argv, **kw):
            assert max(kw["pass_fds"]) >= 10
            argv = list(argv)
            if shell == "dash":
                argv[0] = found
            elif shell == "busybox":
                argv[0:1] = [found, "sh"]
            return subprocess.run(argv, **kw)

        with folder_mount.HeldFolder(own) as held:
            proc = folder_mount.run_guarded(prefix or [], held, str(own),
                                            ["/bin/sh", "-c", "echo PAYLOAD-RAN > ran"],
                                            runner=_runner, capture_output=True, text=True,
                                            timeout=30)
    finally:
        for fd in filler:
            os.close(fd)
    assert proc.returncode == 0 and not folder_mount.guard_refused(proc), proc
    assert (own / "ran").read_text().strip() == "PAYLOAD-RAN"


# ── 3. the view inside is private ───────────────────────────────────────────

@_LANE
def test_a_folder_swapped_on_the_host_after_the_mount_stays_the_held_one(world):
    """The namespace is set up and the guard has passed (its byte arrived); the payload waits
    on a pipe while the host renames A aside and links A to B, then lists A.
    MUTATION: a shared root (`--dev-bind / /` instead of the tmpfs root and its binds) - red
    (it lists B's files)."""
    a = str(world.a)
    rs = ws = rr = wr = -1
    with folder_mount.HeldFolder(a, expect_folder=a) as held:
        try:
            rs, ws = os.pipe()
            rr, wr = os.pipe()
            argv = (folder_mount.namespace_argv(held, a)
                    + folder_mount.guard_argv(a, held.identity,
                                              ["/bin/sh", "-c", f"read x <&{rr}; ls -a {a}"],
                                              started_fd=ws))
            proc = subprocess.Popen(argv, pass_fds=(ws, rr), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            os.close(ws)
            os.close(rr)
            ws = rr = -1
            assert os.read(rs, 1) == b"x"          # set up, checked, the payload waits
            _swap(world.a, world.b)
            os.write(wr, b"go\n")
            os.close(wr)
            wr = -1
            out, err = proc.communicate(timeout=60)
        finally:
            for fd in (rs, ws, rr, wr):
                if fd >= 0:
                    os.close(fd)
    assert proc.returncode == 0, err
    assert "A_ONLY" in out.split() and "B_ONLY" not in out.split()


# ── 4. a start that ran is never repeated ───────────────────────────────────

@_LINUX
def test_only_a_start_that_ran_nothing_is_repeated(tmp_path):
    """MUTATION: repeat on any non-zero exit - red (the payload's line three times)."""
    own = tmp_path / "own"
    own.mkdir()
    with folder_mount.HeldFolder(own) as held:
        proc = folder_mount.run_guarded([], held, str(own),
                                        ["/bin/sh", "-c", f"echo x >> {own}/count; exit 1"],
                                        capture_output=True, text=True, timeout=30)
        assert proc.returncode == 1 and not folder_mount.guard_refused(proc)
        assert (own / "count").read_text().splitlines() == ["x"]
        starts = []

        def _never_starts(argv, **kw):
            starts.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "bwrap: setup failed\n")

        proc = folder_mount.run_guarded(["bwrap"], held, str(own), ["true"],
                                        runner=_never_starts)
    assert len(starts) == 3
    assert folder_mount.guard_refused(proc) and proc.returncode == -1
    assert "could not start in a private view" in proc.stderr and "setup failed" in proc.stderr
    assert "moved or replaced by a link" in proc.stderr


# ── 5. one probe, a real start ──────────────────────────────────────────────

@pytest.fixture
def fresh_probe():
    folder_mount._reset_probe()
    yield
    folder_mount._reset_probe()


def test_the_lane_is_a_real_start_probed_once(monkeypatch, fresh_probe):
    """MUTATION: decide on `shutil.which("bwrap")` alone (no probe start) - red."""
    calls = []
    real = folder_mount.run_guarded

    def _counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(folder_mount, "run_guarded", _counting)
    monkeypatch.setattr(folder_mount.sys, "platform", "linux")
    monkeypatch.setattr(folder_mount.shutil, "which", lambda name: None)
    assert folder_mount.namespace_available() is False
    assert "not installed" in folder_mount.namespace_unavailable_reason()
    folder_mount._reset_probe()
    monkeypatch.setattr(folder_mount.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(folder_mount, "namespace_argv", lambda *a, **k: ["false"])
    assert folder_mount.namespace_available() is False
    assert folder_mount.namespace_available() is False          # cached
    assert len(calls) == 1
    assert "installed but cannot start" in folder_mount.namespace_unavailable_reason()
    folder_mount._reset_probe()
    monkeypatch.setattr(folder_mount.sys, "platform", "darwin")
    assert folder_mount.namespace_available() is False
    assert len(calls) == 1


def test_a_failed_start_is_probed_again_later_a_missing_bwrap_is_not(monkeypatch, fresh_probe):
    """One failed start (a timeout under load) must not keep the lane off until a restart.
    MUTATION: keep every answer for the process - red."""
    probes = []
    real = folder_mount._probe

    def _counting():
        probes.append(1)
        return real()

    monkeypatch.setattr(folder_mount, "_probe", _counting)
    monkeypatch.setattr(folder_mount.sys, "platform", "linux")
    monkeypatch.setattr(folder_mount.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(folder_mount, "namespace_argv", lambda *a, **k: ["false"])
    assert folder_mount.namespace_available() is False
    assert folder_mount.namespace_available() is False
    assert len(probes) == 1                                     # within the interval
    monkeypatch.setattr(folder_mount, "PROBE_RETRY_S", 0.0)
    assert folder_mount.namespace_available() is False
    assert len(probes) == 2                                     # asked again
    folder_mount._reset_probe()
    monkeypatch.setattr(folder_mount.shutil, "which", lambda name: None)
    assert folder_mount.namespace_available() is False
    assert folder_mount.namespace_available() is False
    assert len(probes) == 3                                     # final: not installed


def test_a_probe_folder_that_cannot_be_made_is_an_answer_not_an_error(monkeypatch, fresh_probe):
    """MUTATION: make the folder outside the try - red (the OSError reaches every git call)."""
    monkeypatch.setattr(folder_mount.sys, "platform", "linux")
    monkeypatch.setattr(folder_mount.shutil, "which", lambda name: "/usr/bin/" + name)

    def _full(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(folder_mount.tempfile, "mkdtemp", _full)
    assert folder_mount.namespace_available() is False
    assert "No space left" in folder_mount.namespace_unavailable_reason()


# ── 6. to 9. git ────────────────────────────────────────────────────────────

def _record(monkeypatch):
    """Every process the runner starts (not this file's own `git` calls). The probe is
    settled first, so its start is not among them."""
    folder_mount.namespace_available()
    real = subprocess.run
    seen = []

    def _recording(argv, **kw):
        if argv[0] != "git":
            cwd = kw.get("cwd") or ""
            # Where a handle path leads, read while the handle is open.
            seen.append((list(argv), {**kw, "real_cwd": os.path.realpath(cwd) if cwd else ""}))
        return real(argv, **kw)

    monkeypatch.setattr(gr.subprocess, "run", _recording)
    return seen


@_LANE
@pytest.mark.parametrize("args, reads", [(["status", "--porcelain"], 3), (["add", "-A"], 4)])
def test_one_namespace_start_per_call_and_the_reads_through_the_handles(world, monkeypatch,
                                                                       args, reads):
    """The command starts in the namespace; the read-only config and index reads (the global,
    system and effective driver reads, which also name the global ignore file, and the
    gitlinks read for add) run through the handles, which pin them fully (no work tree is set up), so a call pays
    one namespace start. MUTATION: start the reads in the namespace too - red; start the
    command outside it - red."""
    seen = _record(monkeypatch)
    a = str(world.a)
    proc = gr.run_project_git(args, cwd=a, expect_folder=a)
    assert proc.returncode == 0, proc.stderr
    starts = [(argv, kw) for argv, kw in seen if argv[0] == "bwrap"]
    rest = [(argv, kw) for argv, kw in seen if argv[0] != "bwrap"]
    assert len(starts) == 1 and seen[-1][0][0] == "bwrap" and len(rest) == reads
    for argv, kw in rest:
        assert any(x in ("config", "ls-files") for x in argv), argv
        assert kw["cwd"].startswith("/proc/self/fd/") and kw["real_cwd"] == a
        assert f"--work-tree={kw['cwd']}" in argv
    argv, kw = starts[0]
    home = os.path.expanduser("~")
    assert kw["cwd"] == "/"
    bind = argv.index("--bind")
    assert argv[bind + 1:bind + 3] == [a, a]
    ro = [argv[i + 1] for i, x in enumerate(argv) if x == "--ro-bind-try"]
    assert gr.empty_hooks_dir() in ro and os.path.join(home, ".gitconfig") in ro
    cut = argv.index("--")
    guard, git = argv[cut + 1:cut + 9], argv[cut + 8:]
    assert guard[:3] == ["/bin/sh", "-c", folder_mount.GUARD]
    assert guard[4:6] == [a, _identity(a)]
    started = int(guard[6])
    git_dir = next(x.split("=", 1)[1] for x in git if x.startswith("--git-dir="))
    assert git_dir.startswith("/proc/self/fd/")
    assert f"--work-tree={a}" in git
    assert kw["env"]["GIT_COMMON_DIR"] == git_dir
    assert sorted(kw["pass_fds"]) == sorted((int(git_dir.rsplit("/", 1)[1]), started))


@_LANE
def test_the_global_ignore_file_hides_files_inside_the_namespace_too(world):
    """`core.excludesFile` usually names a file in the home folder, which the namespace did
    not bind: status listed, and `add -A` staged, a file the person's global ignore hides
    (measured). MUTATION: bind no trusted files - red."""
    ignore = Path(os.path.expanduser("~")) / ".gitignore_global"
    ignore.write_text("*.secret\n", encoding="utf-8")
    _git("config", "--global", "core.excludesFile", "~/.gitignore_global", cwd=world.a)
    (world.a / "notes.secret").write_text("token\n", encoding="utf-8")
    a = str(world.a)
    status = gr.run_project_git(["status", "--porcelain"], cwd=a, expect_folder=a)
    assert status.returncode == 0 and "notes.secret" not in status.stdout, status.stdout
    assert gr.run_project_git(["add", "-A"], cwd=a, expect_folder=a).returncode == 0
    staged = _git("diff", "--cached", "--name-only", cwd=world.a).stdout.split()
    assert "notes.secret" not in staged and "A_ONLY" in staged


def _swap_at_first_namespace_start(monkeypatch, folder: Path, target: Path):
    real = subprocess.run
    done = []

    def _swapping(argv, **kw):
        if argv and argv[0] == "bwrap" and not done:
            done.append(_swap(folder, target))
        return real(argv, **kw)

    monkeypatch.setattr(gr.subprocess, "run", _swapping)
    return done


@_LANE
@pytest.mark.parametrize("args, other", [
    (["log", "--oneline"], "B-SECRET"),
    (["status", "--porcelain", "--untracked-files=all"], "B_ONLY"),
    (["diff", "HEAD"], "B-SECRET"),
])
def test_a_folder_swapped_before_git_starts_is_never_read(world, monkeypatch, args, other):
    """After the runner opened A and before the first git process, A becomes a link to B and
    stays one. MUTATION: drop the guard's identity comparison - red (status lists B_ONLY)."""
    (world.b / "f.txt").write_text("B-SECRET changed\n", encoding="utf-8")
    a = str(world.a)
    done = _swap_at_first_namespace_start(monkeypatch, world.a, world.b)
    proc = gr.run_project_git(args, cwd=a, expect_folder=a)
    assert done, "the swap never ran"
    assert other not in proc.stdout + proc.stderr
    if proc.returncode != 0:
        assert proc.returncode == -1 and "private view" in proc.stderr, proc.stderr


@_LANE
def test_a_reset_into_a_swapped_folder_writes_nothing_there(world, monkeypatch):
    """`reset --hard` (as project_rollback runs it) wrote A's files into the swapped-in folder
    in 2 of 247 calls under the handle pin alone. MUTATION: drop the guard's identity
    comparison - red (B's f.txt becomes A's)."""
    target = world.b / "f.txt"
    before = (target.read_bytes(), target.stat().st_mtime_ns)
    a = str(world.a)
    done = _swap_at_first_namespace_start(monkeypatch, world.a, world.b)
    proc = gr.run_project_git(["reset", "--hard", "HEAD"], cwd=a, expect_folder=a)
    assert done
    assert proc.returncode == -1 and "private view" in proc.stderr, proc.stderr
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before


@_LANE
def test_git_in_the_namespace_works_as_before(world, monkeypatch):
    """Real paths, init, a commit through the coder's tool, and /undo in a subfolder.
    MUTATION: `--work-tree=/proc/self/fd/<top>` inside the namespace - red."""
    from vaf.core.snapshot import Snapshot
    from vaf.tools.coder import GitAddCommitTool
    seen = _record(monkeypatch)
    sub = str(world.a / "sub")
    top = gr.run_project_git(["rev-parse", "--show-toplevel"], cwd=sub).stdout.strip()
    git_dir = gr.run_project_git(["rev-parse", "--absolute-git-dir"], cwd=sub).stdout.strip()
    assert Path(top) == world.a and Path(git_dir) == world.a / ".git"
    fresh = world.a.parent / "fresh"
    fresh.mkdir()
    real = str(fresh.resolve())
    assert gr.run_project_git(["init", "-q"], cwd=real, expect_folder=real).returncode == 0
    assert (fresh / ".git" / "HEAD").is_file()
    (world.a / "new.txt").write_text("new\n", encoding="utf-8")
    out = GitAddCommitTool().run(base_dir=str(world.a), message="lane commit",
                                 user_scope_id=A, user_role="user")
    assert "lane commit" in _git("log", "-1", "--format=%s", cwd=world.a).stdout, out
    head = _git("rev-parse", "HEAD", cwd=world.a).stdout.strip()
    snap = Snapshot(sub)
    (world.a / "sub" / "x.txt").write_text("x2\n", encoding="utf-8")
    (world.a / "f.txt").write_text("changed\n", encoding="utf-8")
    assert snap._git_restore(head) is True
    assert (world.a / "sub" / "x.txt").read_text(encoding="utf-8") == "x1\n"
    assert (world.a / "f.txt").read_text(encoding="utf-8") == "changed\n"
    commands = [argv for argv, _ in seen if not any(x in ("config", "ls-files") for x in argv)]
    assert commands and all(argv[0] == "bwrap" for argv in commands)


@_LANE
def test_git_writes_only_inside_the_folder_it_holds(world, monkeypatch):
    """A command that writes outside the checked folder fails, instead of writing into the
    namespace's own memory and losing it with an exit code of 0, and says why.
    MUTATION: a writable root for the git namespace (no `--remount-ro /`) - red."""
    elsewhere = world.a.parent / "elsewhere"
    a = str(world.a)
    proc = gr.run_project_git(["worktree", "add", "-q", "-b", "side", str(elsewhere)], cwd=a,
                              expect_folder=a)
    assert proc.returncode != 0 and not elsewhere.exists(), proc
    # The hint keys on the C locale's words; git's own message follows LANG.
    monkeypatch.setenv("LC_ALL", "C")
    proc = gr.run_project_git(["worktree", "add", "-q", "-b", "side2", str(elsewhere)], cwd=a,
                              expect_folder=a)
    assert proc.returncode != 0 and "only that folder is writable" in proc.stderr, proc
    with folder_mount.HeldFolder(a) as held:
        argv = folder_mount.namespace_argv(held, a, scratch_tmp=False, readonly_root=True)
    assert "--tmpfs" not in argv
    assert argv.index("--remount-ro") > argv.index("--bind") and argv[argv.index("--remount-ro") + 1] == "/"


@pytest.mark.skipif(not path_jail._DIR_FD, reason="no folder handles here")
def test_a_linked_worktree_never_starts_the_namespace(world, monkeypatch):
    """A linked worktree's `.git` is a file git follows itself: only the work tree is pinned,
    and git has to reach the shared git folder outside it. MUTATION: use the lane for a
    linked worktree - red."""
    monkeypatch.setattr(folder_mount, "namespace_available", lambda: True)
    linked = world.a.parent / "linked"
    _git("worktree", "add", "-q", str(linked), cwd=world.a)
    seen = _record(monkeypatch)
    proc = gr.run_project_git(["status", "--porcelain"], cwd=str(linked), linked_worktree=True)
    assert proc.returncode == 0, proc.stderr
    assert seen and not any(argv[0] == "bwrap" for argv, _ in seen)


@pytest.mark.skipif(not path_jail._DIR_FD, reason="no folder handles here")
def test_without_the_lane_git_starts_through_the_handles_alone(world, monkeypatch):
    """The fallback is the handle pin, unchanged. MUTATION: ignore namespace_available - red."""
    monkeypatch.setattr(folder_mount, "namespace_available", lambda: False)
    seen = _record(monkeypatch)
    proc = gr.run_project_git(["log", "--oneline"], cwd=str(world.a))
    assert proc.returncode == 0 and "A-OWN" in proc.stdout, proc.stderr
    assert seen and not any(argv[0] == "bwrap" for argv, _ in seen)


# ── 10. and 11. the jailed shell ────────────────────────────────────────────

def _starting_run(ran, *, start=True):
    """A stand-in for `workspace_exec._run`: records, and reports a start like the guard does
    (one byte into the started pipe) unless ``start`` is False."""
    def _run(argv, hard_timeout, **kwargs):
        ran.append(argv)
        if start:
            for fd in kwargs.get("pass_fds", ()):
                os.write(fd, b"x")
        return 0, "ok", ""
    return _run


@pytest.mark.skipif(not path_jail._DIR_FD, reason="no folder handles here")
def test_a_shell_folder_swapped_after_the_check_is_refused_before_anything_starts(
        world, monkeypatch):
    """MUTATION: hold the workspace without expect_folder - red (the jail starts, in B)."""
    real = wx._assert_safe_workspace
    done = []

    def _swapping(ws):
        real(ws)
        if not done:
            done.append(_swap(world.a, world.b))

    ran = []
    monkeypatch.setattr(wx, "_assert_safe_workspace", _swapping)
    monkeypatch.setattr(wx, "_bwrap_available", lambda: True)
    monkeypatch.setattr(wx, "_run", _starting_run(ran))
    rc, out, err, mode = wx.run_in_workspace(str(world.a), "touch marker", **_NO_ACCOUNT)
    assert done and (rc, mode) == (-2, "refused")
    assert "moved or replaced by a link after it was checked" in err and "again" in err
    assert ran == []


@_LANE
def test_the_real_jail_refuses_a_folder_swapped_before_it_starts(world, monkeypatch):
    """MUTATION: start the jail without the guard - red (B/marker appears)."""
    real = wx._run
    done = []

    def _swapping(argv, *args, **kwargs):
        if not done:
            done.append(_swap(world.a, world.b))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(wx, "_run", _swapping)
    rc, out, err, mode = wx.run_in_workspace(str(world.a), "touch marker", **_NO_ACCOUNT)
    assert done and (rc, mode) == (-2, "refused"), (rc, out, err)
    assert "private view" in err and "is not the folder that was checked" in err
    assert not (world.b / "marker").exists() and not (done[0] / "marker").exists()


@_LANE
def test_a_shell_command_that_failed_ran_once(tmp_path):
    rc, out, err, mode = wx.run_in_workspace(str(tmp_path), "echo x >> count; exit 3",
                                             **_NO_ACCOUNT)
    assert (rc, mode) == (3, "host-jail (bwrap)"), err
    assert (tmp_path / "count").read_text().splitlines() == ["x"]


@pytest.mark.skipif(not path_jail._DIR_FD, reason="no folder handles here")
@pytest.mark.parametrize("shares", [True, False])
def test_the_container_fallback_carries_the_guard_on_a_shared_kernel(tmp_path, monkeypatch,
                                                                     shares):
    """MUTATION: drop the guard in _run_in_container - red."""
    ws = str(tmp_path.resolve())
    ran = []
    monkeypatch.setattr(wx, "_bwrap_available", lambda: False)
    monkeypatch.setattr(wx, "_docker_available", lambda: True)
    monkeypatch.setattr(containers, "docker_shares_kernel", lambda: shares)
    monkeypatch.setattr(wx, "_run", lambda argv, *a, **k: ran.append(argv) or (0, "ok", ""))
    monkeypatch.setattr(wx.subprocess, "run",
                        lambda argv, **k: subprocess.CompletedProcess(argv, 0, "", ""))
    rc, out, err, mode = wx.run_in_workspace(ws, "echo x", **_NO_ACCOUNT)
    assert (rc, mode) == (0, "container")
    argv = ran[0]
    assert f"{ws}:/workspace:z" in argv
    image = argv.index(wx.SANDBOX_IMAGE)
    if shares:
        assert argv[image + 1:image + 4] == ["/bin/sh", "-c", folder_mount.GUARD]
        assert argv[image + 5:image + 8] == ["/workspace", _identity(ws), "-"]
        assert argv[image + 8:image + 10] == ["sh", "-c"]
    else:
        assert argv[image + 1:image + 3] == ["sh", "-c"] and len(argv) == image + 4
    # The guard's refusal (docker itself also exits 125, without the marker).
    monkeypatch.setattr(wx, "_run", lambda argv, *a, **k: (
        125, "", f"{folder_mount.GUARD_MARKER} /workspace is not the folder that was checked\n"))
    rc, out, err, mode = wx.run_in_workspace(ws, "echo x", **_NO_ACCOUNT)
    if shares:
        assert (rc, mode) == (-2, "refused") and "did not get the folder" in err
    else:
        assert (rc, mode) == (125, "container")
    monkeypatch.setattr(wx, "_run", lambda argv, *a, **k: (125, "", "docker: no such image\n"))
    assert wx.run_in_workspace(ws, "echo x", **_NO_ACCOUNT)[3] == "container"


# ── 12. no handle is left open ──────────────────────────────────────────────

@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="counts /proc/self/fd")
def test_no_file_descriptor_is_left_open(world, monkeypatch):
    """50 git calls and 50 shell starts, half of them refused. MUTATION: leave the started
    pipe's write end open in run_guarded - red."""
    a = str(world.a)
    elsewhere = str(world.b)

    def _count():
        return len(os.listdir("/proc/self/fd"))

    gr.run_project_git(["log", "--oneline"], cwd=a, expect_folder=a)    # warm-up
    before = _count()
    for i in range(50):
        if i % 2:
            assert gr.run_project_git(["log", "--oneline"], cwd=a,
                                      expect_folder=a).returncode == 0
        else:
            assert gr.run_project_git(["log"], cwd=a, expect_folder=elsewhere).returncode == -1
    monkeypatch.setattr(wx, "_bwrap_available", lambda: True)
    for i in range(50):
        monkeypatch.setattr(wx, "_run", _starting_run([], start=bool(i % 2)))
        rc, _, _, mode = wx.run_in_workspace(a, "true", **_NO_ACCOUNT)
        assert mode == ("host-jail (bwrap)" if i % 2 else "refused")
    assert _count() == before


# ── docker's kernel ─────────────────────────────────────────────────────────

@pytest.fixture
def fresh_kernel_answer(monkeypatch):
    monkeypatch.setattr(containers, "_SHARES_KERNEL", None)
    monkeypatch.setattr(containers.sys, "platform", "linux")
    import platform
    monkeypatch.setattr(platform, "release", lambda: "7.3.1-test")


@pytest.mark.parametrize("answer, expected", [
    (subprocess.CompletedProcess([], 0, "7.3.1-test\n", ""), True),
    (subprocess.CompletedProcess([], 0, "6.10.14-linuxkit\n", ""), False),
    (subprocess.CompletedProcess([], 1, "", "Cannot connect to the Docker daemon"), True),
    (FileNotFoundError("docker"), True),
])
def test_docker_shares_the_kernel_only_when_it_says_so(monkeypatch, fresh_kernel_answer,
                                                      answer, expected):
    """No answer on Linux checks anyway (the check refuses on other numbers), and is asked
    again. MUTATION: compare with nothing (always True) - red; no answer is False - red."""
    calls = []

    def _docker(args, timeout=60, **kw):
        calls.append(args)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(containers, "docker", _docker)
    assert containers.docker_shares_kernel() is expected
    assert containers.docker_shares_kernel() is expected
    assert calls[0] == ["info", "--format", "{{.KernelVersion}}"]
    # An answer is kept; no answer is asked again.
    answered = not isinstance(answer, Exception) and answer.returncode == 0
    assert len(calls) == (1 if answered else 2)


def test_docker_shares_no_kernel_off_linux(monkeypatch, fresh_kernel_answer):
    monkeypatch.setattr(containers.sys, "platform", "darwin")
    monkeypatch.setattr(containers, "docker", lambda *a, **k: pytest.fail("asked docker"))
    assert containers.docker_shares_kernel() is False


@pytest.mark.skipif(not path_jail._DIR_FD, reason="no folder handles here")
def test_a_shell_jail_that_cannot_start_says_why(tmp_path, monkeypatch):
    """Three empty starts are not always a moved folder: the refusal carries the last start's
    own words and what to do. MUTATION: drop proc.stderr from the shell's refusal - red."""
    monkeypatch.setattr(wx, "_bwrap_available", lambda: True)
    monkeypatch.setattr(wx, "_run", lambda argv, t, **k: (
        1, "", "bwrap: No permissions to create new namespace\n"))
    rc, out, err, mode = wx.run_in_workspace(str(tmp_path), "true", **_NO_ACCOUNT)
    assert (rc, mode) == (-2, "refused")
    assert "No permissions to create new namespace" in err and "try again" in err


@pytest.mark.parametrize("installed", [True, False])
def test_no_sandbox_names_the_real_cause(tmp_path, monkeypatch, installed):
    """bubblewrap installed but unable to start is not "install bubblewrap".
    MUTATION: one wording for both - red."""
    monkeypatch.setattr(wx, "_bwrap_available", lambda: False)
    monkeypatch.setattr(wx, "_docker_available", lambda: False)
    monkeypatch.setattr(folder_mount, "bwrap_installed", lambda: installed)
    monkeypatch.setattr(folder_mount, "namespace_unavailable_reason", lambda: (
        "bubblewrap is installed but cannot start a namespace here: setting up uid map: "
        "Permission denied"))
    rc, out, err, mode = wx.run_in_workspace(str(tmp_path), "true", **_NO_ACCOUNT)
    assert (rc, mode) == (-2, "refused")
    if installed:
        assert "cannot start a namespace here" in err and "Start Docker" in err
        assert "Install bubblewrap" not in err
    else:
        assert "Install bubblewrap or start Docker" in err
