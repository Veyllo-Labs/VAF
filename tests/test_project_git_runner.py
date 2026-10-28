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
    planted = first / "pre-commit"
    planted.write_text("#!/bin/sh\n", encoding="utf-8")
    try:
        second = Path(gr.empty_hooks_dir())
        assert second != first and not any(second.iterdir())
    finally:
        planted.unlink()
        monkeypatch.setattr(gr, "_HOOKS_DIR", None)


_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
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
    ran = tmp_path / "ran"
    prog = tmp_path / "prog.sh"
    prog.write_text(f"#!/bin/sh\necho \"$0 $*\" >> '{ran.as_posix()}'\n", encoding="utf-8")
    prog.chmod(0o755)
    return prog, ran


@pytest.fixture
def filter_marker(marker, tmp_path):
    """The same record, passing stdin through as a filter must."""
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
    """The rollback reverts. An empty driver turns that merge into a conflict.
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
    monkeypatch.setattr(gr, "resolve_git", lambda: "/opt/vaf/git/bin/git")
    monkeypatch.setattr(gr.subprocess, "run",
                        lambda argv, **kw: ran.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    gr.run_project_git(["status", "--porcelain"], cwd=str(tmp_path))
    assert len(ran) == 2 and all(argv[0] == "/opt/vaf/git/bin/git" for argv in ran)
    assert ran[-1][-2:] == ["status", "--porcelain"]


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
