# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Sub-agents work WHERE their caller works.

Terminal children never inherited the spawning agent's cwd on Linux
(gnome-terminal is a D-Bus client of a long-lived server: they start in
$HOME) or macOS (Apple event to the running Terminal.app) - so a coder
spawned from a project checkout never saw that project, failed the
project-root check and fell back to VAF_Projects. Seven spawn sites each
build their own extra_env; the cwd travels through ONE producer instead
(`open_new_terminal` stamps VAF_PARENT_CWD) and ONE consumer
(`Platform.adopt_parent_cwd()` in the sub-agent and workflow entries).
"""
import os
import subprocess as real_subprocess
from pathlib import Path
from types import SimpleNamespace

from vaf.core.platform import Platform


# ── the producer ────────────────────────────────────────────────────────────────────

class _FakeProc:
    pid = 4242
    stdout = None

    def wait(self, timeout=None):
        return 0


def _spawn_captured(monkeypatch, extra_env):
    captured = {}

    def fake_popen(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(real_subprocess, "Popen", fake_popen)
    # Piped lane: env travels as a REAL env dict, so the stamp is observable
    # without opening a terminal window.
    Platform.open_new_terminal("echo x", extra_env=extra_env)
    return captured.get("env") or {}


def test_the_spawn_stamps_the_callers_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    env = _spawn_captured(monkeypatch, {"VAF_SPAWN_MODE": "piped"})
    assert env.get("VAF_PARENT_CWD") == str(tmp_path)


def test_an_explicit_caller_value_wins(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    env = _spawn_captured(monkeypatch, {"VAF_SPAWN_MODE": "piped",
                                        "VAF_PARENT_CWD": "/elsewhere"})
    assert env.get("VAF_PARENT_CWD") == "/elsewhere"


# ── the consumer ────────────────────────────────────────────────────────────────────

def test_adopt_changes_into_the_parents_directory(monkeypatch, tmp_path):
    target = tmp_path / "project"
    target.mkdir()
    monkeypatch.chdir(tmp_path)              # teardown restores the real cwd
    monkeypatch.setenv("VAF_PARENT_CWD", str(target))
    assert Platform.adopt_parent_cwd() is True
    assert Path(os.getcwd()) == target


def test_a_missing_directory_is_a_quiet_no(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VAF_PARENT_CWD", str(tmp_path / "gone"))
    assert Platform.adopt_parent_cwd() is False
    assert Path(os.getcwd()) == tmp_path

    monkeypatch.delenv("VAF_PARENT_CWD")
    assert Platform.adopt_parent_cwd() is False
    assert Path(os.getcwd()) == tmp_path


# ── the entries actually consume it ─────────────────────────────────────────────────

def test_both_child_entries_adopt_the_cwd():
    """Wiring pin: the producer is worthless if no entry calls the consumer.
    Source-level on purpose - running the real entries spawns agents."""
    for rel in ("vaf/cli/cmd/subagent.py", "vaf/cli/cmd/workflow.py"):
        src = (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
        assert "adopt_parent_cwd()" in src, f"{rel} no longer adopts the caller's cwd"


# ── what it was all for: the coder sees the project ─────────────────────────────────

def test_the_coder_stays_in_a_project_cwd(monkeypatch, tmp_path):
    """With the cwd adopted, the existing project-root check finally fires for
    a spawned coder: a marker file plus a non-create task means "work HERE"."""
    from vaf.tools.coder import CodingAgentTool

    project = tmp_path / "myrepo"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.chdir(project)

    dummy = SimpleNamespace(
        _generate_project_directory=lambda task: "GENERATED-NEW")
    base = CodingAgentTool._determine_base_dir(dummy, "fix the failing tests")
    assert base == str(project)

    base = CodingAgentTool._determine_base_dir(dummy, "create new project for a blog")
    assert base == "GENERATED-NEW", "create-intent must still get a fresh folder"


# ── the verdict must not call an edit a failure ─────────────────────────────────────

def _git(args, cwd):
    import subprocess
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True)


def test_an_edit_only_run_is_an_outcome_not_a_failure(tmp_path):
    """The create-bookkeeping stays empty when a run only MODIFIES files; the
    live incident reported "FAILED / No files were created" over a committed,
    successful README edit. Git knows better and is asked."""
    from vaf.tools.coder import _rescue_edited_outcome

    repo = tmp_path / "r"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@example.invalid"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "README.md").write_text("# alt\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "base"], repo)
    baseline = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    (repo / "README.md").write_text("# alt\n\n## Installation\n")
    out = _rescue_edited_outcome([], str(repo), baseline)
    assert out == ["README.md"], out

    # A truly empty run stays an honest failure.
    _git(["checkout", "-q", "--", "README.md"], repo)
    assert _rescue_edited_outcome([], str(repo), baseline) == []

    # Non-empty bookkeeping passes through untouched (no needless git work).
    assert _rescue_edited_outcome(["a.py"], str(repo), baseline) == ["a.py"]


def test_the_verdict_consults_the_rescue():
    """Wiring pin inside the 6000-line run(): the helper is worthless if the
    verdict does not call it before the files_created split."""
    import inspect

    import vaf.tools.coder as c

    src = inspect.getsource(c.CodingAgentTool.run)
    assert "files_created = _rescue_edited_outcome(" in src
