# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""ORIENT phase deterministic project scan (_build_orientation_summary).

The scan seeds the planner with an existing project's file inventory + doc heads so the
planner is not blind (the measured cause of the existing-project doom-loop). It must be a
pure, bounded, best-effort read: fresh project -> no-op notice; populated -> inventory +
README head; large/deep trees stay bounded; skip-dirs are never descended.
"""
import os

from vaf.tools.coder import (
    _ORIENT_MAX_FILES,
    _build_orientation_summary,
)


def test_fresh_and_missing_dir_are_noops(tmp_path):
    assert "Fresh project" in _build_orientation_summary(str(tmp_path))
    assert "Fresh project" in _build_orientation_summary(str(tmp_path / "does_not_exist"))


def test_only_infra_files_is_still_fresh(tmp_path):
    (tmp_path / ".gitignore").write_text("x\n")
    (tmp_path / ".hidden").write_text("x\n")
    (tmp_path / "PARTIAL_foo.py").write_text("x\n")
    assert "Fresh project" in _build_orientation_summary(str(tmp_path))


def test_populated_project_lists_code_and_reads_readme(tmp_path):
    (tmp_path / "calc").mkdir()
    (tmp_path / "calc" / "operations.py").write_text("def add(a, b): return a + b\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_operations.py").write_text("def test_add(): pass\n")
    (tmp_path / "README.md").write_text("# calc\n\nA tiny calculator.\n\n## Operations\n- add\n")
    (tmp_path / "requirements.txt").write_text("pytest\n")

    out = _build_orientation_summary(str(tmp_path))
    assert "EXISTING PROJECT ORIENTATION" in out
    # real code files (not just top-level dir names) reach the planner
    assert "calc/operations.py" in out
    assert "tests/test_operations.py" in out
    assert "requirements.txt" in out
    # README is detected and its head quoted
    assert "Docs present:" in out and "README.md" in out
    assert "A tiny calculator." in out
    # infra file is skipped
    assert ".gitignore" not in out


def test_file_cap_is_enforced(tmp_path):
    for i in range(150):
        (tmp_path / f"f{i:03d}.py").write_text("x = 1\n")
    out = _build_orientation_summary(str(tmp_path))
    listed = [ln for ln in out.splitlines() if ln.startswith("- ") and not ln.startswith("- ...")]
    assert len(listed) <= _ORIENT_MAX_FILES
    assert "truncated at" in out


def test_skip_dirs_are_not_descended(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    for junk in ("node_modules", ".git", "__pycache__", "venv"):
        d = tmp_path / junk / "deep"
        d.mkdir(parents=True)
        (d / "junk.py").write_text("x = 1\n")
    out = _build_orientation_summary(str(tmp_path))
    assert "app.py" in out
    assert "node_modules" not in out and "junk.py" not in out


def test_depth_is_bounded(tmp_path):
    # A path deeper than the cap must not appear.
    deep = tmp_path / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    (deep / "toodeep.py").write_text("x = 1\n")
    (tmp_path / "top.py").write_text("x = 1\n")
    out = _build_orientation_summary(str(tmp_path))
    assert "top.py" in out
    assert "toodeep.py" not in out
