# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""git in a folder a model writes starts nothing but git (vaf.core.git_runner.run_project_git).

The coding agent's jailed shell writes the project folder, `.git` included, and VAF runs git
there on the host: a hook, an fsmonitor, a filter, a textconv, a merge driver or gpg named in
that `.git/config` would run outside the jail. The expected values are spelled out here rather
than read from the module, so dropping one from the module fails its own case.
"""
import ast
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import vaf.core.git_runner as gr

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="a shell script as the planted program")
_ROOT = Path(__file__).resolve().parent.parent


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    _git("init", "-q", cwd=r)
    _git("config", "user.name", "T", cwd=r)
    _git("config", "user.email", "t@example.org", cwd=r)
    (r / "a.txt").write_text("one\n", encoding="utf-8")
    _git("add", "-A", cwd=r)
    _git("commit", "-qm", "base", cwd=r)
    return r


def _pairs(argv):
    """The `-c key=value` pairs of an argv, in order."""
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-c"]


# ── the exact settings and environment ───────────────────────────────────────

_PINNED = [
    "core.fsmonitor=false",
    "core.pager=cat",
    "core.editor=true",
    "core.askPass=",
    "core.sshCommand=ssh",
    "diff.external=",
    "commit.gpgSign=false",
    "tag.gpgSign=false",
    "log.showSignature=false",
    "credential.helper=",
    "diff.ignoreSubmodules=all",
    "status.submoduleSummary=false",
    "submodule.recurse=false",
]


@pytest.mark.parametrize("pair", _PINNED)
def test_every_call_pins_the_setting(repo, pair):
    """MUTATION: drop the pair from PROJECT_GIT_CONFIG."""
    assert pair in _pairs(gr.project_git_command(["status"], str(repo)))


def test_hooks_point_at_an_empty_directory_outside_the_project(repo):
    """MUTATION: drop core.hooksPath."""
    pairs = _pairs(gr.project_git_command(["status"], str(repo)))
    hooks = [p.split("=", 1)[1] for p in pairs if p.startswith("core.hooksPath=")]
    assert len(hooks) == 1
    d = Path(hooks[0])
    assert d.is_dir() and not any(d.iterdir())
    assert not str(d.resolve()).startswith(str(repo.resolve()))


def test_a_planted_hook_in_the_vaf_directory_is_not_used(repo, monkeypatch):
    """The directory is checked on every call: a file in it means a fresh one.
    MUTATION: return the memoized directory without checking it is empty."""
    first = Path(gr.empty_hooks_dir())
    # Recorded now, so teardown puts the VAF directory back rather than the fresh one.
    monkeypatch.setattr(gr, "_HOOKS_DIR", gr._HOOKS_DIR)
    planted = first / "pre-commit"
    planted.write_text("#!/bin/sh\n", encoding="utf-8")
    second = None
    try:
        second = Path(gr.empty_hooks_dir())
        assert second != first and not any(second.iterdir())
    finally:
        planted.unlink()
        if second is not None and second != first:
            second.rmdir()


_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "GIT_EDITOR": "true",
    "GIT_ASKPASS": "",
}


@pytest.mark.parametrize("name", sorted(_ENV))
def test_the_environment_carries_the_setting(name, monkeypatch):
    """MUTATION: drop the entry from PROJECT_GIT_ENV."""
    monkeypatch.setenv(name, "from-the-parent")
    assert gr.project_git_env()[name] == _ENV[name]


def test_the_system_config_stays_readable(monkeypatch):
    """The folder cannot write it, and Git for Windows keeps its LFS filter, autocrlf and
    credential manager there. MUTATION: add GIT_CONFIG_NOSYSTEM back to PROJECT_GIT_ENV."""
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    assert "GIT_CONFIG_NOSYSTEM" not in gr.project_git_env()


@pytest.mark.parametrize("name", ["GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                                  "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"])
def test_an_inherited_repository_location_is_dropped(name, monkeypatch, tmp_path):
    """Set by a parent git (VAF started from a hook), it would move the repository.
    MUTATION: drop the entry from _LAYOUT_ENV."""
    monkeypatch.setenv(name, str(tmp_path))
    assert name not in gr.project_git_env()


def test_the_environment_is_the_whole_environment(monkeypatch):
    """git on Windows needs PATH, SYSTEMROOT and the rest. MUTATION: start from {}."""
    monkeypatch.setenv("VAF_TEST_CARRIED", "yes")
    env = gr.project_git_env()
    assert env["VAF_TEST_CARRIED"] == "yes" and env.get("PATH") == os.environ.get("PATH")


@pytest.mark.parametrize("sub, flags", [
    ("diff", ["--no-ext-diff", "--no-textconv"]),
    ("log", ["--no-ext-diff", "--no-textconv"]),
    ("show", ["--no-ext-diff", "--no-textconv"]),
    ("commit", ["--no-verify"]),
])
def test_the_subcommand_gets_its_flags_right_after_it(repo, sub, flags):
    """MUTATION: drop the subcommand's entry from _SUBCOMMAND_FLAGS."""
    argv = gr.project_git_command([sub, "-x"], str(repo))
    i = argv.index(sub)
    assert argv[i + 1:i + 1 + len(flags)] == flags and argv[-1] == "-x"


def test_a_caller_config_comes_first_so_the_pinned_values_win(repo):
    """A later -c wins in git; the caller's identity stays, a caller's hooksPath loses.
    MUTATION: put the caller's pairs after the pinned ones."""
    argv = gr.project_git_command(["-c", "user.name=VAF Coder", "-c", "core.hooksPath=/x",
                                   "commit", "-m", "m"], str(repo))
    pairs = _pairs(argv)
    assert pairs[0] == "user.name=VAF Coder" and pairs[1] == "core.hooksPath=/x"
    assert pairs[2].startswith("core.hooksPath=") and pairs[2] != "core.hooksPath=/x"
    assert argv[argv.index("commit") + 1] == "--no-verify"


# ── drivers the repository names ─────────────────────────────────────────────

def test_every_driver_the_repository_names_is_switched_off(repo, tmp_path):
    """Read from the repository's config, an included file counts as the repository's.
    MUTATION: drop a section from _DRIVER_OVERRIDES, or a variable in it."""
    _git("config", "filter.fx.clean", "harmless-clean", cwd=repo)
    _git("config", "diff.dx.textconv", "harmless-textconv", cwd=repo)
    _git("config", "merge.mx.driver", "harmless-merge", cwd=repo)
    inc = tmp_path / "inc.cfg"
    inc.write_text('[filter "Inc Name"]\n\tsmudge = harmless\n', encoding="utf-8")
    _git("config", "include.path", inc.as_posix(), cwd=repo)
    pairs = _pairs(gr.project_git_command(["status"], str(repo)))
    for name in ("fx", "Inc Name"):
        for var, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")):
            assert f"filter.{name}.{var}={value}" in pairs
    assert "diff.dx.textconv=" in pairs
    assert "merge.mx.driver=" in pairs


def test_a_driver_only_the_persons_global_config_names_keeps_running(repo, tmp_path, monkeypatch):
    """`git lfs install` writes its filter into the global config, which the project cannot
    write; switching it off would commit LFS files whole. Redefined in the repository, it is
    switched off. MUTATION: count the global scope as untrusted."""
    glob = tmp_path / "global.cfg"
    glob.write_text('[filter "lfs"]\n\tclean = git-lfs clean -- %f\n\trequired = true\n',
                    encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(glob))
    assert not any(p.startswith("filter.lfs.") for p in _pairs(gr.project_git_command(["status"], str(repo))))
    _git("config", "filter.lfs.smudge", "harmless", cwd=repo)
    assert "filter.lfs.clean=" in _pairs(gr.project_git_command(["status"], str(repo)))


def test_a_driver_named_by_a_key_without_a_value_is_switched_off(repo):
    """`required` alone is a valid line; -z then prints the key with no newline after it.
    MUTATION: take only keys that are followed by a value."""
    with open(repo / ".git" / "config", "a", encoding="utf-8") as f:
        f.write('[filter "nv"]\n\trequired\n')
    assert "filter.nv.clean=" in _pairs(gr.project_git_command(["status"], str(repo)))


def test_a_driver_in_the_per_worktree_config_is_switched_off(repo):
    """With extensions.worktreeConfig on, config.worktree is a scope --local does not read.
    MUTATION: drop --worktree from _UNTRUSTED_SCOPES."""
    _git("config", "extensions.worktreeConfig", "true", cwd=repo)
    _git("config", "--worktree", "filter.wt.clean", "harmless", cwd=repo)
    assert "filter.wt.clean=" in _pairs(gr.project_git_command(["status"], str(repo)))


def test_an_older_git_without_scope_labels_or_worktree_config_gets_the_same_pins(repo, tmp_path,
                                                                                  monkeypatch):
    """git before 2.26 has no --show-scope, before 2.20 no --worktree: both end in "unknown
    option" (exit 129). The repository's drivers must still be found and the command run.
    MUTATION: require the --worktree read, or read with --show-scope again."""
    _git("config", "filter.fx.clean", "harmless", cwd=repo)
    inc = tmp_path / "inc.cfg"
    inc.write_text('[merge "mx"]\n\tdriver = harmless\n', encoding="utf-8")
    _git("config", "include.path", inc.as_posix(), cwd=repo)
    real_run = subprocess.run
    refused = []

    def old_git(argv, **kw):
        if any(a in ("--show-scope", "--worktree") for a in argv):
            refused.append(argv)
            return subprocess.CompletedProcess(argv, 129, "", "error: unknown option\n")
        return real_run(argv, **kw)

    monkeypatch.setattr(gr.subprocess, "run", old_git)
    pairs = _pairs(gr.project_git_command(["status"], str(repo)))
    assert "filter.fx.clean=" in pairs and "merge.mx.driver=" in pairs
    assert gr.run_project_git(["status", "--porcelain"], cwd=str(repo)).returncode == 0
    assert refused, "the simulation never saw an option an older git lacks"


def test_init_in_a_new_folder_works(tmp_path):
    """There is no repository config to read before init. MUTATION: read the drivers for init."""
    proj = tmp_path / "fresh"
    proj.mkdir()
    proc = gr.run_project_git(["init"], cwd=str(proj))
    assert proc.returncode == 0, proc.stderr
    assert (proj / ".git").is_dir()


def test_the_runner_never_asks_for_scope_labels():
    """--show-scope needs git 2.26. MUTATION: put it back into the driver read."""
    assert "--show-scope" not in (_ROOT / "vaf" / "core" / "git_runner.py").read_text(encoding="utf-8")


def test_a_driver_name_a_command_line_cannot_carry_is_refused(repo):
    """`-c filter.a=b.clean=` would set `filter.a`: the run is refused, not unprotected.
    MUTATION: skip the name check."""
    with open(repo / ".git" / "config", "a", encoding="utf-8") as f:
        f.write('[filter "a=b"]\n\tclean = harmless\n')
    with pytest.raises(ValueError):
        gr.project_git_command(["status"], str(repo))
    proc = gr.run_project_git(["status"], cwd=str(repo))
    assert proc.returncode == -1 and "a=b" in proc.stderr


# ── precedence, measured with git itself ─────────────────────────────────────

def test_the_runners_values_win_over_the_repositorys_config(repo):
    """Harmless strings in .git/config; read back through the runner, the command line wins."""
    _git("config", "core.hooksPath", "harmless-hooks", cwd=repo)
    _git("config", "filter.x.clean", "harmless-clean", cwd=repo)
    _git("config", "diff.x.textconv", "harmless-textconv", cwd=repo)
    for key in ("core.hooksPath", "filter.x.clean", "diff.x.textconv"):
        out = gr.run_project_git(["config", "--show-origin", "--get", key], cwd=str(repo)).stdout
        origin, _, value = out.rstrip("\n").partition("\t")
        assert origin == "command line:", (key, out)
        assert "harmless" not in value
    hooks = gr.run_project_git(["rev-parse", "--git-path", "hooks"], cwd=str(repo)).stdout.strip()
    assert Path(hooks) == Path(gr.empty_hooks_dir())


# ── nothing the repository names runs ────────────────────────────────────────

@pytest.fixture
def marker(tmp_path):
    """A program that records that it ran. It reads no stdin: an external diff command that
    did run would otherwise wait on it until the timeout."""
    if os.name == "nt":
        pytest.skip("a shell script as the planted program")
    ran = tmp_path / "ran"
    prog = tmp_path / "prog.sh"
    prog.write_text(f"#!/bin/sh\necho \"$0 $*\" >> '{ran.as_posix()}'\n", encoding="utf-8")
    prog.chmod(0o755)
    return prog, ran


@pytest.fixture
def filter_marker(marker, tmp_path):
    """The same record, passing stdin through as a filter must."""
    if os.name == "nt":
        pytest.skip("a shell script as the planted program")
    prog, ran = marker
    filt = tmp_path / "filter.sh"
    filt.write_text(f"#!/bin/sh\necho \"$0 $*\" >> '{ran.as_posix()}'\ncat\n", encoding="utf-8")
    filt.chmod(0o755)
    return filt, ran


@_POSIX_ONLY
def test_no_hook_runs_on_add_commit_or_reset(repo, marker):
    """--no-verify alone still runs post-index-change, reference-transaction and post-commit
    (measured). MUTATION: drop core.hooksPath."""
    prog, ran = marker
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for h in ("pre-commit", "commit-msg", "post-commit", "post-index-change",
              "reference-transaction", "post-checkout"):
        (hooks / h).write_text(f"#!/bin/sh\n'{prog.as_posix()}' {h}\n", encoding="utf-8")
        (hooks / h).chmod(0o755)
    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    for step in (["add", "-A"], ["commit", "-m", "c"], ["reset", "--hard", "HEAD~1"]):
        assert gr.run_project_git(step, cwd=str(repo)).returncode == 0, step
    assert not ran.exists(), ran.read_text(encoding="utf-8")


@_POSIX_ONLY
def test_no_fsmonitor_runs_on_status(repo, marker):
    """MUTATION: drop core.fsmonitor=false."""
    prog, ran = marker
    _git("config", "core.fsmonitor", prog.as_posix(), cwd=repo)
    gr.run_project_git(["status", "--porcelain"], cwd=str(repo))
    assert not ran.exists()


@_POSIX_ONLY
def test_no_filter_runs_on_add_or_checkout(repo, filter_marker):
    """MUTATION: drop the filter overrides."""
    prog, ran = filter_marker
    (repo / ".gitattributes").write_text("*.x filter=fx\n", encoding="utf-8")
    _git("config", "filter.fx.clean", f"{prog.as_posix()} clean", cwd=repo)
    _git("config", "filter.fx.smudge", f"{prog.as_posix()} smudge", cwd=repo)
    _git("config", "filter.fx.required", "true", cwd=repo)
    (repo / "f.x").write_text("data\n", encoding="utf-8")
    for step in (["add", "-A"], ["commit", "-m", "x"], ["checkout", "HEAD~1", "--", "."]):
        assert gr.run_project_git(step, cwd=str(repo)).returncode == 0, step
    assert not ran.exists(), ran.read_text(encoding="utf-8")


@_POSIX_ONLY
def test_no_textconv_or_external_diff_runs_on_diff_or_log(repo, marker, filter_marker):
    """MUTATION: drop --no-ext-diff from the diff flags (the driver command cannot be emptied)."""
    prog, ran = marker
    filt, _ = filter_marker
    (repo / ".gitattributes").write_text("*.txt diff=dx\n", encoding="utf-8")
    _git("config", "diff.dx.textconv", filt.as_posix(), cwd=repo)
    _git("config", "diff.dx.command", f"{prog.as_posix()} command", cwd=repo)
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    assert "changed" in gr.run_project_git(["diff"], cwd=str(repo)).stdout
    gr.run_project_git(["log", "-p", "-1"], cwd=str(repo))
    assert not ran.exists(), ran.read_text(encoding="utf-8")


@_POSIX_ONLY
def test_a_merge_driver_does_not_run_on_revert(repo, marker):
    """The rollback reverts. An empty driver makes that merge fail (a conflict, or an error).
    MUTATION: drop the merge section from _DRIVER_OVERRIDES."""
    prog, ran = marker
    (repo / ".gitattributes").write_text("*.m merge=mx\n", encoding="utf-8")
    (repo / "a.m").write_text("1\n2\n3\n4\n5\n6\n7\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "c1", cwd=repo)
    (repo / "a.m").write_text("1\ntwo\n3\n4\n5\n6\n7\n", encoding="utf-8")
    _git("commit", "-qam", "c2", cwd=repo)
    (repo / "a.m").write_text("1\ntwo\n3\n4\n5\nsix\n7\n", encoding="utf-8")
    _git("commit", "-qam", "c3", cwd=repo)
    _git("config", "merge.mx.driver", f"{prog.as_posix()} %O %A %B", cwd=repo)
    gr.run_project_git(["revert", "--no-commit", "HEAD~1"], cwd=str(repo))
    assert not ran.exists(), ran.read_text(encoding="utf-8")


@_POSIX_ONLY
def test_gpg_does_not_run_when_the_log_shows_a_signed_commit(repo, marker):
    """log.showSignature in the repository's config hands a signed commit to gpg.program.
    MUTATION: drop log.showSignature=false."""
    prog, ran = marker
    tree = _git("rev-parse", "HEAD^{tree}", cwd=repo).stdout.strip()
    head = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    body = (f"tree {tree}\nparent {head}\nauthor T <t@example.org> 1700000000 +0000\n"
            f"committer T <t@example.org> 1700000000 +0000\n"
            "gpgsig -----BEGIN PGP SIGNATURE-----\n \n AAAA\n -----END PGP SIGNATURE-----\n\nsigned\n")
    obj = repo.parent / "commit.txt"
    obj.write_text(body, encoding="utf-8")
    signed = _git("hash-object", "-t", "commit", "-w", str(obj), cwd=repo).stdout.strip()
    _git("update-ref", "HEAD", signed, cwd=repo)
    _git("config", "log.showSignature", "true", cwd=repo)
    _git("config", "gpg.program", prog.as_posix(), cwd=repo)
    gr.run_project_git(["log", "--oneline", "-1"], cwd=str(repo))
    assert not ran.exists(), ran.read_text(encoding="utf-8")


# ── failure shapes ───────────────────────────────────────────────────────────

def test_a_missing_folder_is_named_as_such(tmp_path):
    proc = gr.run_project_git(["status"], cwd=str(tmp_path / "gone"))
    assert proc.returncode == -1 and "Not a directory" in proc.stderr


def test_a_missing_git_is_reported_not_raised(repo, monkeypatch):
    monkeypatch.setattr(gr, "resolve_git", lambda: str(repo / "no-such-git"))
    proc = gr.run_project_git(["status"], cwd=str(repo))
    assert proc.returncode == -1 and proc.stderr == "Git is not installed."


def test_every_git_process_starts_the_git_vaf_resolved(monkeypatch, tmp_path):
    """On Windows without git on PATH VAF brings its own; the driver read and the command
    itself both start it. MUTATION: start "git" instead of resolve_git() in either."""
    ran = []
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(gr, "resolve_git", lambda: "/opt/vaf/git/bin/git")
    monkeypatch.setattr(gr.subprocess, "run",
                        lambda argv, **kw: ran.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    gr.run_project_git(["status", "--porcelain"], cwd=str(tmp_path))
    # The repository's config, its per-worktree config, then the command.
    assert len(ran) == 3 and all(argv[0] == "/opt/vaf/git/bin/git" for argv in ran)
    assert ran[-1][-2:] == ["status", "--porcelain"]
    # The driver read looks at the same repository the command will use.
    # MUTATION: read the drivers without the layout options.
    assert all(argv[1].startswith("--git-dir=") for argv in ran)


# ── no in-scope module runs git any other way ────────────────────────────────

_IN_SCOPE = ("vaf/tools/coder.py", "vaf/tools/project_git.py", "vaf/core/code_audit.py",
             "vaf/core/snapshot.py")
_OTHER_RUNNERS = {"_run_git", "run_git", "resolve_git"}
_SPAWNERS = {"run", "Popen", "call", "check_call", "check_output"}


def _git_starts_other_than_the_runner(source: str):
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in _OTHER_RUNNERS:
            hits.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in _OTHER_RUNNERS:
            hits.append((node.lineno, node.attr))
        elif isinstance(node, ast.alias) and node.name in _OTHER_RUNNERS:
            hits.append((0, f"import {node.name}"))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr in _SPAWNERS and node.args
              and isinstance(node.args[0], (ast.List, ast.Tuple)) and node.args[0].elts
              and isinstance(node.args[0].elts[0], ast.Constant)
              and node.args[0].elts[0].value == "git"):
            hits.append((node.lineno, "subprocess git"))
    return hits


@pytest.mark.parametrize("rel", _IN_SCOPE)
def test_in_scope_modules_run_git_only_through_the_project_runner(rel):
    """MUTATION: put one `subprocess.run(['git', ...])` or `run_git(...)` back."""
    source = (_ROOT / rel).read_text(encoding="utf-8")
    assert _git_starts_other_than_the_runner(source) == []
    assert "run_project_git" in source


def test_the_guard_sees_each_shape_it_forbids():
    """The guard itself, against the shapes it exists for."""
    for snippet in ("import subprocess\nsubprocess.run(['git', 'status'])\n",
                    "from vaf.tools.project_git import _run_git\n",
                    "from vaf.core.git_runner import run_git\nrun_git(['status'])\n",
                    "import subprocess\nsubprocess.Popen(('git', 'log'))\n"):
        assert _git_starts_other_than_the_runner(snippet), snippet


# ── where git works: the project's own repository and folder ─────────────────

def _plain(*args, cwd):
    """git with the layout given explicitly, to look at a repository from outside."""
    return _git(*args, cwd=cwd)


def test_the_repository_and_work_tree_are_pinned_from_a_subfolder(repo):
    """From a subfolder the pin is the folder that holds .git, not the caller's folder.
    MUTATION: pin --work-tree to cwd."""
    sub = repo / "pkg" / "inner"
    sub.mkdir(parents=True)
    argv = gr.project_git_command(["status"], str(sub))
    top = repo.resolve()
    assert argv[1:3] == [f"--git-dir={(top / '.git').as_posix()}", f"--work-tree={top.as_posix()}"]


def test_a_subfolder_caller_still_works(repo):
    """Paths stay relative to the caller's folder, the top is the repository's."""
    sub = repo / "pkg"
    sub.mkdir()
    (sub / "m.py").write_text("x = 1\n", encoding="utf-8")
    top = gr.run_project_git(["rev-parse", "--show-toplevel"], cwd=str(sub)).stdout.strip()
    assert Path(top) == repo.resolve()
    assert gr.run_project_git(["add", "m.py"], cwd=str(sub)).returncode == 0
    staged = gr.run_project_git(["diff", "--cached", "--name-only"], cwd=str(sub)).stdout
    assert staged.split() == ["pkg/m.py"]


def test_core_worktree_does_not_move_git_out_of_the_project(repo, tmp_path):
    """core.worktree in .git/config made add -A pull an outside folder in and a checkout
    write into it. MUTATION: return no layout options."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside\n", encoding="utf-8")
    _git("config", "core.worktree", outside.as_posix(), cwd=repo)
    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    assert Path(gr.run_project_git(["rev-parse", "--show-toplevel"], cwd=str(repo)).stdout.strip()) \
        == repo.resolve()
    assert gr.run_project_git(["add", "-A"], cwd=str(repo)).returncode == 0
    staged = _plain(f"--git-dir={(repo / '.git').as_posix()}", "ls-files", cwd=repo).stdout.split()
    assert "secret.txt" not in staged and "b.txt" in staged
    (repo / "a.txt").unlink()
    assert gr.run_project_git(["checkout", "HEAD", "--", "."], cwd=str(repo)).returncode == 0
    assert (repo / "a.txt").exists() and not (outside / "a.txt").exists()


def test_the_git_directories_stay_inside_the_project(repo):
    """With the pin, git's own answer for both directories is the project's .git."""
    top = repo.resolve()
    for flag in ("--absolute-git-dir", "--git-common-dir"):
        out = gr.run_project_git(["rev-parse", flag], cwd=str(repo)).stdout.strip()
        assert Path(out).resolve() == top / ".git", flag


def _gitdir_file(repo, tmp_path):
    other = tmp_path / "other"
    _git("init", "-q", str(other), cwd=tmp_path)
    # Moved, not deleted: git's object files are read-only on Windows, and rmtree refuses them.
    shutil.move(str(repo / ".git"), str(tmp_path / "old-git"))
    (repo / ".git").write_text(f"gitdir: {(other / '.git').as_posix()}\n", encoding="utf-8")


def _commondir(repo, tmp_path):
    (repo / ".git" / "commondir").write_text(str(tmp_path), encoding="utf-8")


def _alternates(repo, tmp_path):
    (repo / ".git" / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (repo / ".git" / "objects" / "info" / "alternates").write_text(str(tmp_path), encoding="utf-8")


def _linked_dotgit(repo, tmp_path):
    moved = tmp_path / "moved-git"
    shutil.move(str(repo / ".git"), str(moved))
    os.symlink(moved, repo / ".git", target_is_directory=True)


def _linked_ref(repo, tmp_path):
    target = tmp_path / "foreign-heads"
    target.mkdir()
    heads = repo / ".git" / "refs" / "heads"
    shutil.move(str(heads), str(target / "heads"))
    os.symlink(target / "heads", heads, target_is_directory=True)


def _linked_index(repo, tmp_path):
    target = tmp_path / "foreign-index"
    shutil.move(str(repo / ".git" / "index"), str(target))
    os.symlink(target, repo / ".git" / "index")


def _linked_object_dir(repo, tmp_path):
    target = tmp_path / "foreign-pack"
    target.mkdir()
    pack = repo / ".git" / "objects" / "pack"
    shutil.rmtree(pack)
    os.symlink(target, pack, target_is_directory=True)


def _relink(repo, tmp_path, rel):
    """Replace .git/<rel> with a link to a file outside that holds the same bytes."""
    path = repo / ".git" / rel
    target = tmp_path / ("outside-" + rel.replace("/", "-"))
    target.write_bytes(path.read_bytes() if path.exists() else b"")
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, path)


def _linked_reflog(repo, tmp_path):
    """A commit appends a reflog line, ending with the subject, through the link (measured)."""
    _relink(repo, tmp_path, "logs/HEAD")


def _linked_branch_reflog(repo, tmp_path):
    branch = _git("symbolic-ref", "--short", "HEAD", cwd=repo).stdout.strip()
    _relink(repo, tmp_path, f"logs/refs/heads/{branch}")


def _linked_info_exclude(repo, tmp_path):
    _relink(repo, tmp_path, "info/exclude")


def _linked_orig_head(repo, tmp_path):
    _relink(repo, tmp_path, "ORIG_HEAD")


def _linked_sequencer_file(repo, tmp_path):
    (repo / ".git" / "sequencer").mkdir()
    _relink(repo, tmp_path, "sequencer/todo")


_LINKS = [_linked_dotgit, _linked_ref, _linked_index, _linked_object_dir, _linked_reflog,
          _linked_branch_reflog, _linked_info_exclude, _linked_orig_head, _linked_sequencer_file]


@pytest.mark.parametrize("plant", [_gitdir_file, _commondir, _alternates, *(
    pytest.param(f, marks=_POSIX_ONLY) for f in _LINKS)], ids=lambda f: f.__name__)
def test_a_git_folder_that_leads_into_another_repository_is_refused(repo, tmp_path, plant):
    """A .git file, a commondir, alternates, or a link inside .git: log and checkout read the
    other repository, commit and reset move its branch. MUTATION: drop that check."""
    plant(repo, tmp_path)
    assert gr.project_repo_problem(str(repo))
    proc = gr.run_project_git(["status"], cwd=str(repo))
    assert proc.returncode == -1 and proc.stderr


def test_a_linked_worktree_runs_only_when_the_caller_says_so(repo, tmp_path):
    """A person's own `git worktree add` checkout. MUTATION: ignore linked_worktree."""
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "side", str(wt), cwd=repo)
    assert gr.run_project_git(["status"], cwd=str(wt)).returncode == -1
    proc = gr.run_project_git(["rev-parse", "--show-toplevel"], cwd=str(wt), linked_worktree=True)
    assert proc.returncode == 0 and Path(proc.stdout.strip()) == wt.resolve()
    argv = gr.project_git_command(["status"], str(wt), linked_worktree=True)
    assert argv[1] == f"--work-tree={wt.resolve().as_posix()}" and "--git-dir" not in argv[2]


def test_a_folder_without_a_repository_is_refused(tmp_path):
    proc = gr.run_project_git(["status"], cwd=str(tmp_path))
    assert proc.returncode == -1 and "Not a git repository" in proc.stderr


@_POSIX_ONLY
def test_init_does_not_follow_a_git_link(tmp_path):
    """git init through a dangling .git link created the repository at the link's target
    (measured). MUTATION: drop the init link check."""
    proj = tmp_path / "proj"
    proj.mkdir()
    target = tmp_path / "outside" / "target"
    target.parent.mkdir()
    os.symlink(target, proj / ".git", target_is_directory=True)
    assert gr.run_project_git(["init"], cwd=str(proj)).returncode == -1
    assert not target.exists()


@_POSIX_ONLY
def test_the_init_tool_treats_a_git_link_as_present(tmp_path):
    """MUTATION: os.path.exists in GitInitTool (a dangling link is then 'missing')."""
    from vaf.tools.coder import GitInitTool
    proj = tmp_path / "proj"
    proj.mkdir()
    os.symlink(tmp_path / "nowhere", proj / ".git", target_is_directory=True)
    assert "already initialized" in GitInitTool().run(base_dir=str(proj))


def test_project_history_names_the_refusal(repo, tmp_path):
    """MUTATION: drop project_repo_problem from _resolve_project."""
    from vaf.tools.project_git import ProjectHistoryTool
    _commondir(repo, tmp_path)
    assert ProjectHistoryTool().run(project_path=str(repo)).startswith("Error: Refused:")


@_POSIX_ONLY
@pytest.mark.parametrize("whole_folder", [True, False], ids=["hooks-folder", "one-hook"])
def test_a_linked_hook_is_not_a_refusal(repo, tmp_path, whole_folder):
    """Hooks are never run, and a person may link the folder or one hook in their own
    repository. MUTATION: check the hooks entry, or descend into hooks/."""
    shared = tmp_path / "shared-hooks"
    shared.mkdir()
    (shared / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    hooks = repo / ".git" / "hooks"
    if hooks.exists():
        shutil.rmtree(hooks)
    if whole_folder:
        os.symlink(shared, hooks, target_is_directory=True)
    else:
        hooks.mkdir()
        os.symlink(shared / "pre-commit", hooks / "pre-commit")
    assert gr.project_repo_problem(str(repo)) == ""


@_POSIX_ONLY
def test_no_reflog_line_reaches_a_file_outside(repo, tmp_path):
    """The case the reflog check exists for, end to end. MUTATION: skip logs/ in the walk."""
    _linked_reflog(repo, tmp_path)
    outside = tmp_path / "outside-logs-HEAD"
    before = outside.read_bytes()
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    gr.run_project_git(["add", "-A"], cwd=str(repo))
    gr.run_project_git(["commit", "-m", "x; echo planted"], cwd=str(repo))
    assert outside.read_bytes() == before


# ── a repository inside the project ───────────────────────────────────────────

@pytest.mark.parametrize("rest, expected", [
    (["-A"], ["-A", "--", ":/", ":(top,exclude,literal)sub"]),
    (["."], ["--", ".", ":(top,exclude,literal)sub"]),
    (["--", "x.py"], ["--", "x.py", ":(top,exclude,literal)sub"]),
    (["m.py"], ["--", "m.py", ":(top,exclude,literal)sub"]),
    ([], []),
])
def test_add_excludes_every_gitlink(rest, expected):
    assert gr._add_excluding(rest, ["sub"]) == expected


@pytest.fixture
def embedded(repo, filter_marker):
    """A project that holds a repository the model made with `git init`, staged as a gitlink,
    whose OWN config names a clean filter, and which is dirty."""
    filt, ran = filter_marker
    sub = repo / "sub"
    sub.mkdir()
    _git("init", "-q", cwd=sub)
    _git("config", "user.name", "T", cwd=sub)
    _git("config", "user.email", "t@example.org", cwd=sub)
    (sub / ".gitattributes").write_text("*.x filter=fx\n", encoding="utf-8")
    (sub / "f.x").write_text("one\n", encoding="utf-8")
    _git("add", "-A", cwd=sub)
    _git("commit", "-qm", "s", cwd=sub)
    _git("-c", "core.hooksPath=", "add", "-A", cwd=repo)
    _git("-c", "core.hooksPath=", "commit", "-qm", "with an embedded repository", cwd=repo)
    _git("config", "filter.fx.clean", f"{filt.as_posix()} clean", cwd=sub)
    (sub / "f.x").write_text("two\n", encoding="utf-8")
    later = time.time() + 5
    os.utime(sub / "f.x", (later, later))
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    return repo, ran


@_POSIX_ONLY
@pytest.mark.parametrize("args", [["status", "--porcelain"], ["diff"], ["diff", "--name-only", "HEAD"]])
def test_status_and_diff_start_no_git_in_an_embedded_repository(embedded, args):
    """Measured without the pin: the filter in sub/.git/config ran.
    MUTATION: drop diff.ignoreSubmodules=all."""
    repo, ran = embedded
    assert gr.run_project_git(args, cwd=str(repo)).returncode == 0
    assert not ran.exists(), ran.read_text(encoding="utf-8")


@_POSIX_ONLY
@pytest.mark.parametrize("args", [["add", "-A"], ["add", "."]])
def test_add_starts_no_git_in_an_embedded_repository_and_still_adds(embedded, args):
    """add ignores diff.ignoreSubmodules. MUTATION: do not exclude the gitlinks."""
    repo, ran = embedded
    assert gr.run_project_git(args, cwd=str(repo)).returncode == 0
    assert not ran.exists(), ran.read_text(encoding="utf-8")
    staged = gr.run_project_git(["diff", "--cached", "--name-only"], cwd=str(repo)).stdout.split()
    assert staged == ["a.txt"]


@_POSIX_ONLY
def test_stash_create_is_refused_beside_an_embedded_repository(embedded):
    """stash create takes no pathspec to exclude it with. MUTATION: drop the refusal."""
    repo, ran = embedded
    proc = gr.run_project_git(["stash", "create"], cwd=str(repo))
    assert proc.returncode == -1 and "sub" in proc.stderr
    assert not ran.exists()


@_POSIX_ONLY
def test_the_coder_and_rollback_still_work_beside_an_embedded_repository(embedded):
    """The final commit, the diff view and a rollback, with the embedded repository left alone."""
    from vaf.tools.coder import _build_file_diffs, _final_commit
    from vaf.tools.project_git import ProjectRollbackTool
    repo, ran = embedded
    base = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert "a.txt" in _build_file_diffs(str(repo), base, [])
    assert _final_commit(str(repo), "VAF Coder: change").startswith("Git: committed")
    assert _git("show", "--name-only", "--format=", "HEAD", cwd=repo).stdout.split() == ["a.txt"]
    out = ProjectRollbackTool().run(commit=base[:7], project_path=str(repo))
    assert "restored" in out, out
    assert (repo / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert not ran.exists(), ran.read_text(encoding="utf-8")
