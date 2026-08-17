# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Archiving a chat: a move, not a second format - and never across accounts.

Deleting a chat used to be the only option, so a conversation the agent would
later have needed was gone for tidiness. Archiving keeps the session FILE, which
is already the whole conversation, already encrypted at rest and already carries
its owner - so anything that can read a session can read an archived one, which
is what makes it usable by the memory lane later. The isolation tests are the
point of the file: an archive that leaks is worse than none.
"""
import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("llama_cpp", MagicMock())

from vaf.core.session import SessionManager


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return SessionManager(storage_dir=str(tmp_path / "sessions"))


def _make(mgr, name, scope):
    s = mgr.new(name=name, user_scope_id=scope)
    s.add_message("user", "the quick brown fox")
    s.add_message("assistant", "jumped over the lazy dog")
    mgr.save(s)
    return s


def test_archiving_moves_the_chat_out_of_the_list_but_keeps_it(mgr):
    s = _make(mgr, "kept", "ab12cd34")
    assert mgr.archive(s.id, user_scope_id="ab12cd34") is True

    assert not (mgr.storage_dir / f"{s.id}.json").exists(), "it must leave the sidebar"
    assert [r["id"] for r in mgr.list(user_scope_id="ab12cd34")] == []
    archived = mgr.list_archived(user_scope_id="ab12cd34")
    assert [r["id"] for r in archived] == [s.id]
    assert archived[0]["message_count"] == 2


def test_an_archived_chat_is_still_a_readable_session(mgr):
    """The reason it is a move: every existing reader keeps working on it."""
    s = _make(mgr, "readable", "ab12cd34")
    mgr.archive(s.id, user_scope_id="ab12cd34")

    path = mgr.archive_dir("ab12cd34") / f"{s.id}.json"
    data = mgr._read_session_file(path)
    assert [m["content"] for m in data["messages"]][0] == "the quick brown fox"
    assert data["metadata"]["user_scope_id"] == "ab12cd34"


def test_one_account_never_sees_anothers_archive(mgr):
    mine = _make(mgr, "mine", "ab12cd34")
    theirs = _make(mgr, "theirs", "ef56ab78")
    mgr.archive(mine.id, user_scope_id="ab12cd34")
    mgr.archive(theirs.id, user_scope_id="ef56ab78")

    assert [r["id"] for r in mgr.list_archived(user_scope_id="ab12cd34")] == [mine.id]
    assert [r["id"] for r in mgr.list_archived(user_scope_id="ef56ab78")] == [theirs.id]
    assert mgr.archive_dir("ab12cd34") != mgr.archive_dir("ef56ab78")


def test_a_file_in_the_wrong_folder_still_does_not_leak(mgr):
    """Isolation must not rest on the directory alone: the owner is re-checked
    from inside the file, so a stray copy cannot be read by the wrong account."""
    theirs = _make(mgr, "theirs", "ef56ab78")
    mgr.archive(theirs.id, user_scope_id="ef56ab78")

    stray = mgr.archive_dir("ef56ab78") / f"{theirs.id}.json"
    mine = mgr.archive_dir("ab12cd34")
    mine.mkdir(parents=True, exist_ok=True)
    (mine / stray.name).write_bytes(stray.read_bytes())

    assert mgr.list_archived(user_scope_id="ab12cd34") == [], (
        "a file carrying another account's scope must be ignored, not listed"
    )


def test_archiving_a_missing_chat_reports_failure(mgr):
    """The caller deletes when the move fails, so a False must mean False."""
    assert mgr.archive("nope123456", user_scope_id="ab12cd34") is False


def test_the_delete_handler_archives_before_deleting():
    """Order matters: archiving MOVES the file, so a delete afterwards would
    have nothing to remove - and a failed move must still delete, or a chat the
    user asked to be gone quietly stays."""
    import pathlib
    import re

    src = pathlib.Path("vaf/core/web_server.py").read_text(encoding="utf-8")
    block = src[src.index('elif type == "delete_session"'):]
    block = block[:block.index('elif type == "hide_session"')]
    assert 'cmd.get("archive")' in block, "the handler ignores the dialog's checkbox"
    assert re.search(r"if not session_mgr\.archive\([^)]*\):\s*\n\s*session_mgr\.delete", block), (
        "a failed archive must fall through to a real delete"
    )
    # And ownership is still checked before either path runs.
    assert "_ws_session_owner_ok" in block
