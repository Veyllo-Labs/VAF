# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Tests for the code-enforced coder rules.

Covers the four enforcement mechanisms:
- single-file deliverable detection (set_todos 1-task rule)
- TaskManager failed/retry semantics (honest stuck handling)
- task goal verification (deterministic + injectable LLM check)
- final git commit on run exit
- debug logger fallback (telemetry without IPC spawn)
"""
import os
import subprocess

import pytest

from vaf.tools.coder import (
    TaskManager,
    _detect_single_file_deliverable,
    _final_commit,
    _verify_task_goal,
)


# ─────────────────────────────────────────────────────────────────────────────
# Single-file deliverable detection
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("task", [
    "Erstelle ein 3D Game als einzelne HTML-Datei mit Three.js",
    "Alles in einer einzigen Datei umsetzen",
    "Schreibe das Tool als Einzeldatei",
    "Build a snake game as a single html file",
    "Write a single-file Python script",
    "put everything in one file please",
    "one single file with all the code",
])
def test_single_file_detector_positive(task):
    assert _detect_single_file_deliverable(task) is True


@pytest.mark.parametrize("task", [
    "Erstelle eine professionelle Webseite mit mehreren Seiten",
    "Erstelle eine Datei für die Config und eine für den Code",
    "Build a multi-page website with about and contact pages",
    "Create a single page application with React",
    "Lies die Datei und korrigiere den Fehler",
    "",
])
def test_single_file_detector_negative(task):
    assert _detect_single_file_deliverable(task) is False


# ─────────────────────────────────────────────────────────────────────────────
# TaskManager: failed / retry semantics
# ─────────────────────────────────────────────────────────────────────────────

def _make_task_mgr(tmp_path, titles):
    mgr = TaskManager(str(tmp_path))
    mgr.set_todos(titles)
    return mgr


def test_fail_current_task_is_terminal_but_not_completed(tmp_path):
    mgr = _make_task_mgr(tmp_path, ["task a", "task b"])
    mgr.fail_current_task("stuck after 16 loops")

    todos = mgr.todos
    assert todos[0]["status"] == "failed"
    assert "stuck" in todos[0]["result"]
    assert mgr.current_task_idx == 1
    assert mgr.is_all_done() is False  # task b still pending

    mgr.complete_current_task("done")
    assert mgr.is_all_done() is True       # terminal semantics incl. failed
    assert mgr.is_all_completed() is False  # strict success says no
    assert len(mgr.failed_tasks()) == 1


def test_reset_task_for_retry_reopens_failed_task(tmp_path):
    mgr = _make_task_mgr(tmp_path, ["task a", "task b", "task c"])
    mgr.complete_current_task("done a")
    mgr.fail_current_task("verification failed")
    mgr.complete_current_task("done c")
    assert mgr.is_all_done() is True

    mgr.reset_task_for_retry(1, "verification failed")
    assert mgr.is_all_done() is False
    assert mgr.current_task_idx == 1
    assert mgr.todos[1]["status"] == "pending"
    assert "Previous attempt" in (mgr.todos[1]["description"] or "")

    # Completing the retried task must skip the already-completed task c
    mgr.complete_current_task("done b on retry")
    assert mgr.is_all_done() is True
    assert mgr.is_all_completed() is True
    assert mgr.failed_tasks() == []


# ─────────────────────────────────────────────────────────────────────────────
# Task goal verification
# ─────────────────────────────────────────────────────────────────────────────

def test_verify_with_clean_file_evidence(tmp_path):
    f = tmp_path / "index.html"
    f.write_text("<html><body>real page content</body></html>")
    verified, evidence = _verify_task_goal("Create page", [str(f)], str(tmp_path))
    assert verified is True
    assert "index.html" in evidence


def test_verify_fails_on_missing_file(tmp_path):
    verified, evidence = _verify_task_goal(
        "Create page", [str(tmp_path / "gone.html")], str(tmp_path)
    )
    assert verified is False
    assert "missing" in evidence


def test_verify_fails_on_active_linter_errors(tmp_path):
    f = tmp_path / "main.py"
    f.write_text("print('hello world this is real code')")
    verified, evidence = _verify_task_goal(
        "Create script", [str(f)], str(tmp_path), linter_active=True
    )
    assert verified is False
    assert "linter" in evidence


def test_verify_without_evidence_and_without_verifier_fails(tmp_path):
    verified, evidence = _verify_task_goal("Add collision detection", [], str(tmp_path))
    assert verified is False


def test_verify_llm_yes_confirms_goal(tmp_path):
    (tmp_path / "index.html").write_text("<html>collision code here</html>")
    verified, evidence = _verify_task_goal(
        "Add collision detection", [], str(tmp_path),
        llm_verify=lambda prompt: "YES - checkCollision() is implemented",
    )
    assert verified is True
    assert "LLM" in evidence


def test_verify_llm_reasoning_style_verdict(tmp_path):
    # Reasoning models bury the verdict at the end of their chain of thought
    # (and may return it via reasoning_content) — the LAST yes/no counts.
    (tmp_path / "index.html").write_text("<html>collision code</html>")

    verified, evidence = _verify_task_goal(
        "Add collision detection", [], str(tmp_path),
        llm_verify=lambda p: (
            "Okay, the goal asks for collision detection. Looking at the code, "
            "there is a checkCollision() call in the game loop... so my answer is YES"
        ),
    )
    assert verified is True

    verified, _ = _verify_task_goal(
        "Add collision detection", [], str(tmp_path),
        llm_verify=lambda p: (
            "The goal asks for collision detection. Yes, there is a loop, but I "
            "cannot find any collision handling at all. Final answer: NO"
        ),
    )
    assert verified is False


def test_verify_llm_no_or_error_fails(tmp_path):
    (tmp_path / "index.html").write_text("<html>something</html>")

    verified, _ = _verify_task_goal(
        "Add collision detection", [], str(tmp_path),
        llm_verify=lambda prompt: "NO - there is no collision handling",
    )
    assert verified is False

    def _boom(prompt):
        raise RuntimeError("llm down")

    verified, evidence = _verify_task_goal(
        "Add collision detection", [], str(tmp_path), llm_verify=_boom
    )
    assert verified is False
    assert "failed" in evidence


# ─────────────────────────────────────────────────────────────────────────────
# Final commit
# ─────────────────────────────────────────────────────────────────────────────

def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path):
    if _git("--version", cwd=tmp_path).returncode != 0:
        pytest.skip("git not available")
    project = tmp_path / "proj"
    project.mkdir()
    _git("init", cwd=project)
    _git("config", "user.name", "Test", cwd=project)
    _git("config", "user.email", "test@example.com", cwd=project)
    return project


def test_final_commit_commits_all_changes(git_repo):
    (git_repo / "index.html").write_text("<html>game</html>")
    note = _final_commit(str(git_repo), "VAF Coder: test run\n\nStatus: COMPLETE (1/1 tasks)")
    assert "committed" in note

    status = _git("status", "--porcelain", cwd=git_repo)
    assert status.stdout.strip() == ""
    log = _git("log", "-1", "--pretty=%s", cwd=git_repo)
    assert "VAF Coder: test run" in log.stdout


def test_final_commit_nothing_to_commit(git_repo):
    (git_repo / "a.txt").write_text("x")
    _final_commit(str(git_repo), "first")
    note = _final_commit(str(git_repo), "second")
    assert "nothing new" in note


def test_final_commit_identity_fallback(tmp_path, monkeypatch):
    if _git("--version", cwd=tmp_path).returncode != 0:
        pytest.skip("git not available")
    project = tmp_path / "proj"
    project.mkdir()
    _git("init", cwd=project)
    # No identity anywhere: helper must fall back to the one-off VAF identity
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(var, raising=False)

    (project / "main.py").write_text("print('x')")
    note = _final_commit(str(project), "VAF Coder: identity fallback")
    assert "committed" in note
    log = _git("log", "-1", "--pretty=%an <%ae>", cwd=project)
    assert "VAF Coder <coder@vaf.local>" in log.stdout


def test_final_commit_refuses_unsafe_and_non_git_dirs(tmp_path):
    assert _final_commit(os.path.expanduser("~"), "msg") == ""
    assert _final_commit(str(tmp_path), "msg") == ""  # no .git


# ─────────────────────────────────────────────────────────────────────────────
# Debug logger fallback (telemetry without IPC spawn)
# ─────────────────────────────────────────────────────────────────────────────

def test_logger_fallback_creates_events_without_env(tmp_path, monkeypatch):
    import vaf.core.subagent_debug as sd

    for var in ("VAF_TASK_ID", "VAF_AGENT_TYPE", "VAF_IN_SUBAGENT_TERMINAL",
                "VAF_IN_WORKFLOW_TERMINAL", "VAF_IN_WORKFLOW"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sd, "get_debug_root_dir", lambda: tmp_path)

    # Default behavior unchanged: no env -> no logger
    assert sd.get_subagent_logger_from_env() is None

    # Fallback: logger with generated local run id
    lg = sd.get_subagent_logger_from_env(create_fallback=True, agent_type="coding_agent")
    assert lg is not None
    assert lg.agent_type == "coding_agent"
    assert lg.task_id.startswith("local-")

    lg.event("loop_start", loop=1)
    assert lg.events_file.exists()
    assert "loop_start" in lg.events_file.read_text()


def test_logger_fallback_needs_agent_type(monkeypatch):
    import vaf.core.subagent_debug as sd

    for var in ("VAF_TASK_ID", "VAF_AGENT_TYPE"):
        monkeypatch.delenv(var, raising=False)
    assert sd.get_subagent_logger_from_env(create_fallback=True) is None
