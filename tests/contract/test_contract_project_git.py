# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: `vaf.run_project_git` (docs/EMBEDDING.md, "git in a folder a model writes").

What an embedder runs git through once a model has written the folder, `.git` included:
the call shape, the result type, that it never raises, and that the repository's own config
does not get to pick the hooks directory or a filter. The exact list of pinned settings is
not pinned here; that the repository's values lose is.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

import vaf

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "c@example.org"],
                 ["config", "user.name", "C"], ["config", "core.hooksPath", "harmless-hooks"],
                 ["config", "filter.x.clean", "harmless-clean"]):
        subprocess.run(["git", *args], cwd=r, check=True, capture_output=True)
    return r


def test_it_returns_a_completed_process(tmp_path):
    r = _repo(tmp_path)
    (r / "a.txt").write_text("a\n", encoding="utf-8")
    proc = vaf.run_project_git(["status", "--porcelain"], cwd=str(r))
    assert isinstance(proc, subprocess.CompletedProcess)
    assert proc.returncode == 0 and "a.txt" in proc.stdout


def test_the_repositorys_config_does_not_choose_hooks_or_filters(tmp_path):
    r = _repo(tmp_path)
    hooks = vaf.run_project_git(["rev-parse", "--git-path", "hooks"], cwd=str(r)).stdout.strip()
    assert "harmless" not in hooks and Path(hooks).is_dir() and not any(Path(hooks).iterdir())
    clean = vaf.run_project_git(["config", "--get", "filter.x.clean"], cwd=str(r)).stdout
    assert "harmless" not in clean


def test_a_leading_config_of_the_callers_own_is_kept(tmp_path):
    r = _repo(tmp_path)
    proc = vaf.run_project_git(["-c", "user.name=Embedder", "config", "--get", "user.name"],
                               cwd=str(r))
    assert proc.stdout.strip() == "Embedder"


def test_it_never_raises(tmp_path):
    proc = vaf.run_project_git(["status"], cwd=str(tmp_path / "missing"))
    assert proc.returncode != 0 and proc.stderr


def test_a_git_file_is_refused_unless_the_caller_accepts_a_linked_worktree(tmp_path):
    r = _repo(tmp_path)
    (r / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True, capture_output=True)
    subprocess.run(["git", "-c", "core.hooksPath=", "commit", "-qm", "a"], cwd=r, check=True,
                   capture_output=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "side", str(wt)], cwd=r, check=True,
                   capture_output=True)
    assert vaf.run_project_git(["status"], cwd=str(wt)).returncode != 0
    assert vaf.run_project_git(["status"], cwd=str(wt), linked_worktree=True).returncode == 0


def test_the_work_tree_is_the_folder_that_holds_git(tmp_path):
    r = _repo(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    subprocess.run(["git", "config", "core.worktree", str(outside)], cwd=r, check=True,
                   capture_output=True)
    (r / "sub").mkdir()
    top = vaf.run_project_git(["rev-parse", "--show-toplevel"], cwd=str(r / "sub")).stdout.strip()
    assert Path(top) == r.resolve()
