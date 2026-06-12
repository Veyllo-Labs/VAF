"""Tests for the coder-owned project history and rollback capability."""
import os
import subprocess

import pytest

from vaf.tools.project_git import (
    ProjectHistoryTool,
    ProjectRollbackTool,
    _detect_history_rollback_intent,
)


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def project(tmp_path):
    if _git("--version", cwd=tmp_path).returncode != 0:
        pytest.skip("git not available")
    proj = tmp_path / "Webseite Foo"
    proj.mkdir()
    _git("init", cwd=proj)
    _git("config", "user.name", "Test", cwd=proj)
    _git("config", "user.email", "test@example.com", cwd=proj)

    (proj / "index.html").write_text("<html>version 1</html>")
    _git("add", "-A", cwd=proj)
    _git("commit", "-qm", "VAF Coder: initial website", cwd=proj)

    (proj / "index.html").write_text("<html>version 2 with Impressum</html>")
    (proj / "styles.css").write_text("body { color: red; }")
    _git("add", "-A", cwd=proj)
    _git("commit", "-qm", "VAF Coder: add Impressum", cwd=proj)
    return proj


def _sha(proj, ref="HEAD"):
    return _git("rev-parse", "--short", ref, cwd=proj).stdout.strip()


def test_history_lists_versions_and_files(project):
    out = ProjectHistoryTool().run(project_path=str(project))
    assert "VAF Coder: initial website" in out
    assert "VAF Coder: add Impressum" in out
    assert "index.html" in out
    assert "rollback" in out
    # newest first
    assert out.index("add Impressum") < out.index("initial website")


def test_tools_accept_base_dir_from_coder_wrapper(project):
    # Inside the coder loop the GitToolWrapper passes base_dir instead of project_path
    out = ProjectHistoryTool().run(base_dir=str(project))
    assert "VAF Coder: initial website" in out


# ─────────────────────────────────────────────────────────────────────────────
# Delegation intent detection (Main Agent -> coding_agent task text)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("task,kind,commit", [
    ("history", "history", ""),
    ("zeig mir die History", "history", ""),
    ("Zeige den Verlauf des Projekts", "history", ""),
    ("git log bitte", "history", ""),
    ("rollback auf a2200c1", "rollback", "a2200c1"),
    ("Rollback to 89648df please", "rollback", "89648df"),
    ("setze das Projekt zurück auf a2200c1", "rollback", "a2200c1"),
    ("mach einen Rollback", "rollback", ""),
])
def test_intent_detector_matches_delegations(task, kind, commit):
    assert _detect_history_rollback_intent(task) == (kind, commit)


@pytest.mark.parametrize("task", [
    "Erstelle eine Webseite über die History von Rom",
    "Baue eine Versionen-Übersicht in die Seite ein",
    "Write a blog post about the history of computers",
    "Füge dem Impressum eine Adresse hinzu",
    "Fix the contact form validation",
    "",
])
def test_intent_detector_ignores_normal_coding_tasks(task):
    assert _detect_history_rollback_intent(task) == ("", "")


# ─────────────────────────────────────────────────────────────────────────────
# Fast path through CodingAgentTool.run (no agentic loop, no LLM)
# ─────────────────────────────────────────────────────────────────────────────

def test_coding_agent_fast_path_history(project):
    from vaf.tools.coder import CodingAgentTool
    out = CodingAgentTool().run(task="zeig mir die History", project_path=str(project))
    assert "VAF Coder: initial website" in out
    assert "VAF Coder: add Impressum" in out


def test_coding_agent_fast_path_rollback(project):
    from vaf.tools.coder import CodingAgentTool
    first = _sha(project, "HEAD~1")
    out = CodingAgentTool().run(task=f"rollback auf {first}", project_path=str(project))
    assert "restored" in out
    assert (project / "index.html").read_text() == "<html>version 1</html>"


def test_coding_agent_fast_path_rollback_without_id_returns_history(project):
    from vaf.tools.coder import CodingAgentTool
    out = CodingAgentTool().run(task="mach bitte einen Rollback", project_path=str(project))
    assert "VAF Coder: initial website" in out
    assert "Ask the user which version" in out
    # nothing changed
    assert (project / "index.html").read_text() == "<html>version 2 with Impressum</html>"


def test_history_rejects_non_git_and_unsafe_dirs(tmp_path):
    out = ProjectHistoryTool().run(project_path=str(tmp_path))
    assert "not a git repository" in out
    out = ProjectHistoryTool().run(project_path=os.path.expanduser("~"))
    assert "Error" in out


def test_rollback_restores_old_version_as_new_commit(project):
    first = _sha(project, "HEAD~1")
    out = ProjectRollbackTool().run(commit=first, project_path=str(project))
    assert "restored" in out

    # Content matches version 1, styles.css from version 2 is gone
    assert (project / "index.html").read_text() == "<html>version 1</html>"
    assert not (project / "styles.css").exists()

    # History preserved: rollback is a NEW commit on top
    log = _git("log", "--pretty=%s", cwd=project).stdout.splitlines()
    assert len(log) == 3
    assert log[0].startswith("Rollback to")

    # The undo hint points at the pre-rollback HEAD
    assert 'project_rollback(commit="' in out


def test_rollback_backs_up_uncommitted_changes(project):
    first = _sha(project, "HEAD~1")
    (project / "index.html").write_text("<html>uncommitted work</html>")

    out = ProjectRollbackTool().run(commit=first, project_path=str(project))
    assert "Backup before rollback" in out
    assert (project / "index.html").read_text() == "<html>version 1</html>"

    log = _git("log", "--pretty=%s", cwd=project).stdout.splitlines()
    assert any("Backup before rollback" in line for line in log)

    # The uncommitted content is recoverable from the backup commit
    backup_sha = next(
        _git("log", "--pretty=%h %s", cwd=project).stdout.splitlines()[i].split()[0]
        for i, line in enumerate(_git("log", "--pretty=%h %s", cwd=project).stdout.splitlines())
        if "Backup before rollback" in line
    )
    show = _git("show", f"{backup_sha}:index.html", cwd=project)
    assert show.stdout == "<html>uncommitted work</html>"


def test_rollback_to_current_version_is_a_noop(project):
    head = _sha(project)
    out = ProjectRollbackTool().run(commit=head, project_path=str(project))
    assert "Nothing to do" in out
    assert len(_git("log", "--pretty=%s", cwd=project).stdout.splitlines()) == 2


def test_rollback_rejects_unknown_commit(project):
    out = ProjectRollbackTool().run(commit="deadbeef", project_path=str(project))
    assert "not found" in out


def test_rollback_requires_commit(project):
    out = ProjectRollbackTool().run(project_path=str(project))
    assert "commit is required" in out


def test_rollback_of_rollback_restores_newest_state(project):
    first = _sha(project, "HEAD~1")
    pre_rollback_head = _sha(project)

    ProjectRollbackTool().run(commit=first, project_path=str(project))
    assert (project / "index.html").read_text() == "<html>version 1</html>"

    out = ProjectRollbackTool().run(commit=pre_rollback_head, project_path=str(project))
    assert "restored" in out
    assert (project / "index.html").read_text() == "<html>version 2 with Impressum</html>"
    assert (project / "styles.css").exists()
