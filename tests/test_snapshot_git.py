# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Snapshots and /undo run git through the project runner (vaf.core.snapshot).

`/undo` can run inside a coding agent's project, whose `.git` and `.vaf/snapshots/index.json`
the agent's shell writes."""
import os
import shutil
import subprocess

import pytest

from vaf.core.snapshot import Snapshot

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    for args in (["init", "-q"], ["config", "user.name", "T"],
                 ["config", "user.email", "t@example.org"]):
        subprocess.run(["git", *args], cwd=r, check=True, capture_output=True)
    (r / "a.txt").write_text("one\n", encoding="utf-8")
    return r


def test_track_and_restore_round_trip(repo):
    snap = Snapshot(str(repo))
    sid = snap.track(message="first")
    assert sid
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    assert "two" in snap.diff(sid)
    assert snap.restore(sid) is True
    assert (repo / "a.txt").read_text(encoding="utf-8") == "one\n"


def test_an_option_shaped_snapshot_id_reaches_no_command_line(repo, tmp_path):
    """`git diff --output=<file>` writes over that file. MUTATION: append the id unresolved."""
    snap = Snapshot(str(repo))
    snap.track(message="first")
    target = tmp_path / "written"
    assert snap.diff(f"--output={target.as_posix()}") == ""
    assert not target.exists()


@pytest.mark.skipif(os.name == "nt", reason="a shell script as the hook")
def test_a_hook_planted_in_the_project_does_not_run(repo, tmp_path):
    """MUTATION: route the snapshot commit around run_project_git (plain git + --no-verify)."""
    ran = tmp_path / "ran"
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for h in ("post-commit", "post-index-change", "reference-transaction"):
        (hooks / h).write_text(f"#!/bin/sh\necho {h} >> '{ran.as_posix()}'\n", encoding="utf-8")
        (hooks / h).chmod(0o755)
    assert Snapshot(str(repo)).track(message="first")
    assert not ran.exists()
