# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Surgical search/replace edit_file tool.

edit_file changes ONLY the targeted text in an existing file (the alternative to a whole-file
write_file rewrite that drifts). These tests pin: a one-line fix touches one line; a search
that is missing / not unique writes NOTHING; multi-hunk edits are all-or-nothing and reject
overlaps; the CR / trailing-whitespace fallback locates but replaces the original bytes; and
an already-applied edit is skipped rather than failed.
"""
import os

import pytest

import vaf.tools.filesystem as fs
from vaf.tools.filesystem import EditFileTool


@pytest.fixture
def unjail(monkeypatch):
    # Let the tool operate on a tmp path (bypass the workspace jail for the unit test).
    monkeypatch.setattr(fs, "is_safe_path", lambda p: (True, os.path.abspath(p)))


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def test_surgical_edit_touches_only_the_target(tmp_path, unjail):
    src = "def multiply(a, b):\n    return a + b   # BUG\n\ndef add(a, b):\n    return a + b\n"
    p = _write(tmp_path, "m.py", src)
    r = EditFileTool().run(path=p, edits=[{"search": "    return a + b   # BUG", "replace": "    return a * b"}])
    assert "applied 1" in r
    out = (tmp_path / "m.py").read_text()
    assert "def multiply(a, b):\n    return a * b\n" in out
    assert out.count("return a + b") == 1          # add() untouched
    assert "def add(a, b):\n    return a + b\n" in out


def test_not_found_writes_nothing(tmp_path, unjail):
    p = _write(tmp_path, "a.py", "x = 1\n")
    before = (tmp_path / "a.py").read_text()
    r = EditFileTool().run(path=p, edits=[{"search": "y = 2", "replace": "y = 3"}])
    assert r.startswith("EDIT FAILED")
    assert (tmp_path / "a.py").read_text() == before


def test_not_unique_writes_nothing(tmp_path, unjail):
    p = _write(tmp_path, "a.py", "v = 1\nv = 1\n")
    before = (tmp_path / "a.py").read_text()
    r = EditFileTool().run(path=p, edits=[{"search": "v = 1", "replace": "v = 2"}])
    assert r.startswith("EDIT FAILED") and "not unique" in r
    assert (tmp_path / "a.py").read_text() == before


def test_multi_hunk_all_apply(tmp_path, unjail):
    p = _write(tmp_path, "a.py", "a = 1\nb = 2\nc = 3\n")
    r = EditFileTool().run(path=p, edits=[
        {"search": "a = 1", "replace": "a = 10"},
        {"search": "c = 3", "replace": "c = 30"},
    ])
    assert "applied 2" in r
    assert (tmp_path / "a.py").read_text() == "a = 10\nb = 2\nc = 30\n"


def test_multi_hunk_is_all_or_nothing(tmp_path, unjail):
    p = _write(tmp_path, "a.py", "a = 1\nb = 2\n")
    before = (tmp_path / "a.py").read_text()
    # first hunk matches, second does not -> NOTHING is written
    r = EditFileTool().run(path=p, edits=[
        {"search": "a = 1", "replace": "a = 10"},
        {"search": "zzz", "replace": "qqq"},
    ])
    assert r.startswith("EDIT FAILED")
    assert (tmp_path / "a.py").read_text() == before


def test_overlapping_edits_rejected(tmp_path, unjail):
    p = _write(tmp_path, "a.py", "hello world\n")
    before = (tmp_path / "a.py").read_text()
    r = EditFileTool().run(path=p, edits=[
        {"search": "hello world", "replace": "hi world"},
        {"search": "world", "replace": "earth"},
    ])
    assert r.startswith("EDIT FAILED") and "overlap" in r
    assert (tmp_path / "a.py").read_text() == before


def test_crlf_and_trailing_ws_fallback(tmp_path, unjail):
    # File uses CRLF + trailing spaces; the model's search uses LF and no trailing space.
    (tmp_path / "a.py").write_bytes(b"def f():\r\n    return 1   \r\n")
    p = str(tmp_path / "a.py")
    r = EditFileTool().run(path=p, edits=[{"search": "    return 1", "replace": "    return 2"}])
    assert "applied 1" in r
    assert "return 2" in (tmp_path / "a.py").read_text()


def test_idempotent_already_applied_is_skipped(tmp_path, unjail):
    # The fix is already in the file; the model retries the same edit -> skip, not fail.
    p = _write(tmp_path, "a.py", "    return a * b\n")
    r = EditFileTool().run(path=p, edits=[{"search": "    return a + b", "replace": "    return a * b"}])
    assert "already applied" in r.lower() or "no change" in r.lower()
    assert (tmp_path / "a.py").read_text() == "    return a * b\n"


def test_missing_file_errors(tmp_path, unjail):
    r = EditFileTool().run(path=str(tmp_path / "nope.py"), edits=[{"search": "a", "replace": "b"}])
    assert r.startswith("Error") and "does not exist" in r


def test_single_and_flat_edit_forms(tmp_path, unjail):
    p = _write(tmp_path, "a.py", "x = 1\n")
    # single dict
    assert "applied 1" in EditFileTool().run(path=p, edits={"search": "x = 1", "replace": "x = 2"})
    # flat search/replace kwargs
    assert "applied 1" in EditFileTool().run(path=p, search="x = 2", replace="x = 3")
    assert (tmp_path / "a.py").read_text() == "x = 3\n"


def test_jail_rejection_is_returned(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "is_safe_path", lambda p: (False, "Error: outside the workspace"))
    r = EditFileTool().run(path="/etc/passwd", edits=[{"search": "root", "replace": "x"}])
    assert r == "Error: outside the workspace"


def test_whole_file_rewrite_via_edit_is_rescued_as_write(tmp_path, unjail):
    # A weak model "edits" by pasting the whole old file as search + the whole new file as replace,
    # but the search drifted by a word so the exact match fails. Since search ~= the WHOLE file, the
    # replace IS the intended new file -> rescued as a full write instead of being trashed.
    original = "line1\nline2\nline3\nline4\nline5\nline6\n"
    p = _write(tmp_path, "app.py", original)
    new_content = "brand new file\ncompletely different content\n"
    stale_search = original.replace("line3", "lineX")   # same length, whole file, one word off
    r = EditFileTool().run(path=p, edits=[{"search": stale_search, "replace": new_content}])
    assert "full rewrite" in r.lower() or "write_file" in r.lower()
    assert (tmp_path / "app.py").read_text() == new_content   # rescued: replace became the file


def test_partial_huge_chunk_is_not_rescued_and_writes_nothing(tmp_path, unjail):
    # A huge search that is only a FRAGMENT of a much bigger file must NOT be auto-written (that
    # would destroy the untouched rest); it fails with a write_file hint and writes nothing.
    header = "H" * 200 + "\n"
    chunk = "\n".join(f"old_line_{i}" for i in range(300)) + "\n"   # ~3.3 KB, but far below the file
    footer = "F" * 10000 + "\n"
    original = header + chunk + footer
    p = _write(tmp_path, "big.py", original)
    before = (tmp_path / "big.py").read_text()
    stale_chunk = chunk.replace("old_line_5", "MISSING")           # >2000 chars, not the whole file
    r = EditFileTool().run(path=p, edits=[{"search": stale_chunk, "replace": "tiny new chunk"}])
    assert r.startswith("EDIT FAILED")
    assert "write_file" in r                                       # size hint present
    assert (tmp_path / "big.py").read_text() == before            # nothing written
