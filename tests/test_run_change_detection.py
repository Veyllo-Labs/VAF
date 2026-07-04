# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""DOCUMENT phase git change-detection (_detect_run_changes).

The doc phase must know exactly which files the model changed THIS run so it documents
the right things. These tests pin: added/modified/mid-run-committed files are detected,
deletions are excluded, and a fresh repo with no baseline commit (empty run_start_sha)
falls back to the empty tree and reports the whole deliverable.
"""
import subprocess

from vaf.tools.coder import _detect_run_changes, _EMPTY_TREE_SHA


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t.local", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )


def _head(cwd):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                          capture_output=True, text=True).stdout.strip()


def test_added_modified_and_untracked_detected(tmp_path):
    d = str(tmp_path)
    _git(d, "init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    _git(d, "add", "-A"); _git(d, "commit", "-q", "-m", "init")
    base = _head(d)

    (tmp_path / "a.py").write_text("x = 2\n")          # modified (tracked)
    (tmp_path / "b.py").write_text("y = 1\n")          # untracked
    changed = _detect_run_changes(d, base)
    assert set(changed) == {"a.py", "b.py"}


def test_mid_run_committed_file_is_detected(tmp_path):
    d = str(tmp_path)
    _git(d, "init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    _git(d, "add", "-A"); _git(d, "commit", "-q", "-m", "init")
    base = _head(d)

    (tmp_path / "c.py").write_text("z = 1\n")           # the model commits mid-run
    _git(d, "add", "-A"); _git(d, "commit", "-q", "-m", "work")
    changed = _detect_run_changes(d, base)
    assert "c.py" in changed


def test_deletions_are_excluded(tmp_path):
    d = str(tmp_path)
    _git(d, "init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 1\n")
    _git(d, "add", "-A"); _git(d, "commit", "-q", "-m", "init")
    base = _head(d)

    (tmp_path / "a.py").unlink()                        # deleted -> must NOT appear
    (tmp_path / "b.py").write_text("y = 2\n")           # modified -> appears
    changed = _detect_run_changes(d, base)
    assert "b.py" in changed
    assert "a.py" not in changed


def test_fresh_repo_no_baseline_uses_empty_tree(tmp_path):
    d = str(tmp_path)
    _git(d, "init", "-q")
    (tmp_path / "app.py").write_text("x = 1\n")         # never committed
    (tmp_path / "util.py").write_text("y = 1\n")
    changed = _detect_run_changes(d, "")               # empty baseline
    assert set(changed) == {"app.py", "util.py"}


def test_empty_tree_constant_is_gits_canonical():
    assert _EMPTY_TREE_SHA == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
