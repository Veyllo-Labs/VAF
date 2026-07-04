# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Workflow engine step-arg routing into the shared per-run project directory.

Live incident 2026-07-03: a create_file workflow's write_file step carried a bare
relative filename; the engine only injected the project dir into coding_agent /
document_writer steps, so the write resolved against the backend process cwd and
the file landed in the user's home root (outside every folder the file endpoint
may serve). _inject_workflow_paths now routes relative write_file paths too.
"""
import os

from vaf.workflows.engine import _inject_workflow_paths

PROJ = "/tmp/proj/Workflow"


def test_relative_write_file_path_is_resolved_into_project_dir():
    args = {"path": "vaf_reddit_selfhosted_draft", "content": "x"}
    _inject_workflow_paths("write_file", args, PROJ)
    assert args["path"] == os.path.join(PROJ, "vaf_reddit_selfhosted_draft")


def test_relative_subpath_keeps_structure():
    args = {"path": "notes/day1.txt", "content": "x"}
    _inject_workflow_paths("write_file", args, PROJ)
    assert args["path"] == os.path.join(PROJ, "notes/day1.txt")


def test_absolute_and_home_paths_stay_untouched():
    for explicit in ("/etc/hosts-copy.txt", "~/Documents/mine.txt"):
        args = {"path": explicit, "content": "x"}
        _inject_workflow_paths("write_file", args, PROJ)
        assert args["path"] == explicit


def test_coding_agent_gets_project_path_but_step_override_wins():
    args = {}
    _inject_workflow_paths("coding_agent", args, PROJ)
    assert args["project_path"] == PROJ
    args = {"project_path": "/custom"}
    _inject_workflow_paths("coding_agent", args, PROJ)
    assert args["project_path"] == "/custom"


def test_no_project_dir_is_a_noop():
    args = {"path": "draft.txt", "content": "x"}
    _inject_workflow_paths("write_file", args, None)
    assert args["path"] == "draft.txt"


def test_existing_relative_file_is_not_rerouted(tmp_path, monkeypatch):
    """A relative path that already points at an existing file is an in-place update
    (e.g. a code_review step that read the same path) - read and write must agree, so
    it stays cwd-relative and is NOT joined onto the project dir."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "existing.py").write_text("code")
    args = {"path": "existing.py", "content": "improved"}
    _inject_workflow_paths("write_file", args, PROJ)
    assert args["path"] == "existing.py"


def test_folder_alias_paths_stay_untouched():
    for alias in ("Documents/report.md", "Desktop/note.txt", "downloads/x.csv"):
        args = {"path": alias, "content": "x"}
        _inject_workflow_paths("write_file", args, PROJ)
        assert args["path"] == alias


def test_move_file_relative_dst_is_routed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src.txt").write_text("x")  # existing src stays cwd-relative
    args = {"src": "src.txt", "dst": "moved.txt"}
    _inject_workflow_paths("move_file", args, PROJ)
    assert args["src"] == "src.txt"  # existing -> in place
    assert args["dst"] == os.path.join(PROJ, "moved.txt")  # new -> routed


def test_injection_is_idempotent():
    """Called once per retry - a second call must not double-join the path."""
    args = {"path": "draft.txt", "content": "x"}
    _inject_workflow_paths("write_file", args, PROJ)
    once = args["path"]
    _inject_workflow_paths("write_file", args, PROJ)
    assert args["path"] == once
