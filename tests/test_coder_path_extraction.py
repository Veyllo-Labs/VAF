# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Coder explicit-path handling: FILE paths must never become project directories.

Live incident 2026-07-11 (runs 9db44519 / afb0e5f7): a task text contained the
deliverable FILE path .../marktmodell_juli2026.html; the bare-path regex used it
as base_dir. Run 1 crashed (os.makedirs on an existing file), run 2 created a
DIRECTORY named like the file and nested the real HTML inside it. These tests pin
the fix: extraction keeps file extensions and stops at quotes, file-shaped paths
split into (dirname, target-file hint), and _ensure_git_repo refuses non-dirs.
"""
import os
import tempfile
from pathlib import Path

from vaf.tools.coder import (
    CodingAgentTool,
    _extract_explicit_task_path,
    _looks_like_file_path,
    _split_explicit_path,
)


# ── _looks_like_file_path ────────────────────────────────────────────────────

def test_existing_file_is_file_path(tmp_path):
    f = tmp_path / "artifact.bin"  # extension NOT in the allowlist
    f.write_text("x")
    assert _looks_like_file_path(str(f)) is True


def test_existing_directory_wins_even_with_file_like_name(tmp_path):
    d = tmp_path / "marktmodell_juli2026.html"
    d.mkdir()
    assert _looks_like_file_path(str(d)) is False


def test_nonexistent_with_known_extension_is_file():
    assert _looks_like_file_path("/nonexistent/dir/page.html") is True
    assert _looks_like_file_path("/nonexistent/dir/chart.svg") is True


def test_nonexistent_without_known_extension_is_directory():
    assert _looks_like_file_path("/nonexistent/dir/myproject") is False
    assert _looks_like_file_path("/nonexistent/dir/project.v2") is False
    assert _looks_like_file_path("/nonexistent/dir/site.backup") is False


def test_dot_directories_stay_directories():
    # splitext(".vaf") == (".vaf", "") - never a file target
    assert _looks_like_file_path("/nonexistent/.vaf") is False


def test_empty_and_none_are_not_file_paths():
    assert _looks_like_file_path("") is False
    assert _looks_like_file_path(None) is False


# ── _split_explicit_path ─────────────────────────────────────────────────────

def test_split_file_path_into_dirname_and_hint():
    d, hint = _split_explicit_path("/home/user/Documents/VAF_Projects/u1/chat1/marktmodell_juli2026.html")
    assert d == "/home/user/Documents/VAF_Projects/u1/chat1"
    assert hint == "marktmodell_juli2026.html"


def test_split_directory_path_unchanged():
    d, hint = _split_explicit_path("/home/user/Documents/VAF_Projects/u1/myproject")
    assert d == "/home/user/Documents/VAF_Projects/u1/myproject"
    assert hint == ""


def test_split_windows_file_path_on_any_host():
    d, hint = _split_explicit_path(r"C:\Users\mert\Documents\proj\site.html")
    assert d == r"C:\Users\mert\Documents\proj"
    assert hint == "site.html"


def test_split_non_string_is_coerced():
    # str(Path(...)) yields backslashes on Windows - compare separator-neutrally
    # (the coercion itself, not the separator style, is under test here).
    d, hint = _split_explicit_path(Path("/nonexistent/dir/page.html"))
    assert d.replace("\\", "/") == "/nonexistent/dir"
    assert hint == "page.html"


# ── _extract_explicit_task_path ──────────────────────────────────────────────

def test_phrase_form_keeps_file_extension():
    # Pre-fix the phrase char class excluded '.', truncating '/data/proj/site.html'
    # to '/data/proj/site'.
    got = _extract_explicit_task_path("Write it in directory /data/proj/site.html please")
    assert got == "/data/proj/site.html"


def test_bare_unix_path_stops_at_quotes():
    # Pre-fix \S+ swallowed the closing quote: '/home/x/proj/site.html"' survived rstrip.
    got = _extract_explicit_task_path('Speichere unter "/home/user/proj/site.html".')
    assert got == "/home/user/proj/site.html"


def test_bare_windows_path_keeps_extension():
    # Pre-fix the Windows arm excluded '.', truncating the extension away.
    got = _extract_explicit_task_path(r"speichere unter C:\Users\mert\Documents\proj\site.html")
    assert got == r"C:\Users\mert\Documents\proj\site.html"


def test_incident_task_text_extracts_full_file_path():
    # Same shape as the live incident task, but under a guaranteed-nonexistent
    # root: on the incident machine the original path EXISTS as a directory
    # (the bug's leftover), and an existing directory deliberately wins over the
    # extension heuristic (continue-project case).
    task = (
        "Erstelle eine vollständige, standalone HTML-Datei und speichere sie unter "
        "/home/nobody9x/Documents/VAF_Projects/ab12cd34/green123456/marktmodell_juli2026.html"
    )
    got = _extract_explicit_task_path(task)
    assert got == "/home/nobody9x/Documents/VAF_Projects/ab12cd34/green123456/marktmodell_juli2026.html"
    d, hint = _split_explicit_path(got)
    assert d == "/home/nobody9x/Documents/VAF_Projects/ab12cd34/green123456"
    assert hint == "marktmodell_juli2026.html"


def test_trailing_punctuation_is_stripped():
    assert _extract_explicit_task_path("files go to /home/x/proj, thanks") == "/home/x/proj"
    assert _extract_explicit_task_path("save at /home/x/proj/site.html.") == "/home/x/proj/site.html"


def test_directory_phrase_still_works():
    got = _extract_explicit_task_path("arbeite im Verzeichnis /home/x/myproject bitte")
    assert got == "/home/x/myproject"


def test_no_path_returns_none():
    assert _extract_explicit_task_path("Create a new website about dogs") is None
    assert _extract_explicit_task_path("") is None
    assert _extract_explicit_task_path(None) is None


# ── _ensure_git_repo guard ───────────────────────────────────────────────────

def test_ensure_git_repo_refuses_file_path(tmp_path):
    # Pre-fix subprocess(cwd=<file>) raised NotADirectoryError (uncaught).
    f = tmp_path / "not_a_dir.html"
    f.write_text("x")
    CodingAgentTool()._ensure_git_repo(str(f))  # must not raise
    assert not (tmp_path / ".git").exists()


def test_ensure_git_repo_refuses_nonexistent_path(tmp_path):
    CodingAgentTool()._ensure_git_repo(str(tmp_path / "missing"))  # must not raise


# ── makedirs failure classes used by run()'s graceful fallback ───────────────

def test_makedirs_on_existing_file_raises_oserror(tmp_path):
    # run() catches OSError broadly: the file-in-the-way case is FileExistsError
    # everywhere, but a file as a path COMPONENT maps to different OSError
    # subclasses per platform (NotADirectoryError on POSIX, varying WinError
    # mappings on Windows) - the exact subclass is deliberately not pinned.
    blocker = tmp_path / "page.html"
    blocker.write_text("placeholder")
    try:
        os.makedirs(str(blocker), exist_ok=True)
        raised = None
    except OSError as e:
        raised = e
    assert raised is not None

    nested = blocker / "sub"
    try:
        os.makedirs(str(nested), exist_ok=True)
        raised = None
    except OSError as e:
        raised = e
    assert raised is not None
