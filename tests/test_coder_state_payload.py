"""Tests for the coder_state payload builders (WebUI SubAgent window feed)."""
import subprocess

import pytest

from vaf.tools.coder import _build_file_tree, _build_git_state


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


# ─────────────────────────────────────────────────────────────────────────────
# _build_file_tree
# ─────────────────────────────────────────────────────────────────────────────

def test_file_tree_status_mapping(tmp_path):
    (tmp_path / "index.html").write_text("<html>page</html>")     # existed, modified
    (tmp_path / "styles.css").write_text("body{}")                # existed, untouched
    (tmp_path / "script.js").write_text("console.log(1)")         # added this run
    (tmp_path / "live.html").write_text("<html>live</html>")      # being written now

    initial = {"index.html", "styles.css"}
    created = [str(tmp_path / "index.html"), str(tmp_path / "script.js"), str(tmp_path / "live.html")]

    tree = _build_file_tree(str(tmp_path), created, str(tmp_path / "live.html"), initial)
    by_name = {e["name"]: e for e in tree}

    assert by_name["live.html"]["status"] == "W"
    assert by_name["index.html"]["status"] == "M"
    assert by_name["script.js"]["status"] == "A"
    assert by_name["styles.css"]["status"] == ""
    assert all(e["size"] > 0 for e in tree)


def test_file_tree_hides_infrastructure(tmp_path):
    (tmp_path / "index.html").write_text("x")
    (tmp_path / ".gitignore").write_text("x")
    (tmp_path / ".vaf").mkdir()
    (tmp_path / ".vaf" / "tasks.json").write_text("{}")
    (tmp_path / "assets").mkdir()  # directories are not listed (flat v1)

    tree = _build_file_tree(str(tmp_path))
    assert [e["name"] for e in tree] == ["index.html"]


def test_file_tree_missing_dir_is_safe():
    assert _build_file_tree("/nonexistent/dir") == []


# ─────────────────────────────────────────────────────────────────────────────
# _build_git_state
# ─────────────────────────────────────────────────────────────────────────────

def test_git_state_non_git_dir(tmp_path):
    state = _build_git_state(str(tmp_path))
    assert state == {"branch": "", "dirty": 0, "commits": []}


def test_git_state_with_commits_and_dirty_files(tmp_path):
    if _git("--version", cwd=tmp_path).returncode != 0:
        pytest.skip("git not available")
    _git("init", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    (tmp_path / "a.txt").write_text("1")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-qm", "VAF Coder: first", cwd=tmp_path)
    (tmp_path / "a.txt").write_text("2")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-qm", "VAF Coder: second", cwd=tmp_path)
    (tmp_path / "b.txt").write_text("dirty")

    state = _build_git_state(str(tmp_path))
    assert state["branch"] in ("main", "master")
    assert state["dirty"] == 1
    assert [c["msg"] for c in state["commits"]] == ["VAF Coder: second", "VAF Coder: first"]
    assert all(c["sha"] and c["when"] for c in state["commits"])


def test_git_state_respects_commit_limit(tmp_path):
    if _git("--version", cwd=tmp_path).returncode != 0:
        pytest.skip("git not available")
    _git("init", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    for i in range(7):
        (tmp_path / "a.txt").write_text(str(i))
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-qm", f"commit {i}", cwd=tmp_path)

    state = _build_git_state(str(tmp_path), max_commits=5)
    assert len(state["commits"]) == 5
    assert state["commits"][0]["msg"] == "commit 6"
