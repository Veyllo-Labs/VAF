# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Deleting a chat also removes its workspace folder, but ONLY if empty.

Design (live feedback): the WebUI shows a "this chat has a workspace"
indicator for every open chat, even before anything was ever saved into it
(the folder is created eagerly). To avoid littering VAF_Projects with
abandoned empty folders, SessionManager.delete() removes the workspace too
when it is still empty at delete time - never when it holds real content.
"""
import shutil

import pytest

from vaf.core.platform import Platform
from vaf.core.session import SessionManager, get_session_workspace_dir


@pytest.fixture
def session():
    mgr = SessionManager()
    sess = mgr.new(name="ws-cleanup-test", user_scope_id="dead5678-0000-0000-0000-000000000000")
    mgr.save(sess, sync_state=False)
    chat_dir = Platform.documents_dir() / "VAF_Projects" / "dead5678" / sess.id
    yield mgr, sess, chat_dir
    if chat_dir.exists():
        shutil.rmtree(chat_dir)
    mgr.delete(sess.id)


def test_delete_removes_a_truly_empty_workspace(session):
    mgr, sess, chat_dir = session
    chat_dir.mkdir(parents=True)
    assert chat_dir.is_dir()

    mgr.delete(sess.id)

    assert not chat_dir.exists()


def test_delete_ignores_dotfiles_when_deciding_empty(session):
    """A workspace only ever auto-labeled (channel label file) but never
    actually used to save anything must still count as empty."""
    mgr, sess, chat_dir = session
    chat_dir.mkdir(parents=True)
    (chat_dir / ".vaf_workspace.json").write_text('{"label": "x"}')

    mgr.delete(sess.id)

    assert not chat_dir.exists()


def test_delete_keeps_a_workspace_with_real_content(session):
    mgr, sess, chat_dir = session
    chat_dir.mkdir(parents=True)
    (chat_dir / "report.html").write_text("<html>real output</html>")

    mgr.delete(sess.id)

    assert chat_dir.is_dir()  # the session record is gone, the files stay
    assert (chat_dir / "report.html").read_text() == "<html>real output</html>"


def test_delete_removes_a_workspace_containing_only_empty_subfolders(session):
    """Recursive emptiness: a tree of nothing but empty subfolders (no files
    anywhere) must still be classified as empty and cleaned up - a single
    top-level listdir would have wrongly kept it (an empty subfolder is a
    non-dot entry at the top level)."""
    mgr, sess, chat_dir = session
    (chat_dir / "a" / "b").mkdir(parents=True)

    mgr.delete(sess.id)

    assert not chat_dir.exists()


def test_delete_keeps_a_workspace_with_a_real_subfolder(session):
    mgr, sess, chat_dir = session
    (chat_dir / "sub").mkdir(parents=True)
    (chat_dir / "sub" / "data.txt").write_text("x")

    mgr.delete(sess.id)

    assert chat_dir.is_dir()
    assert (chat_dir / "sub" / "data.txt").exists()


def test_delete_is_a_no_op_when_no_workspace_ever_existed(session):
    """The common case: a chat that was chatted in but never saved anything -
    no folder was even created. Delete must not error."""
    mgr, sess, chat_dir = session
    assert not chat_dir.exists()

    assert mgr.delete(sess.id) is True
    assert not chat_dir.exists()


def test_delete_still_removes_the_session_when_workspace_cleanup_errors(session, monkeypatch):
    """A broken workspace lookup must never block deleting the session record."""
    mgr, sess, chat_dir = session
    chat_dir.mkdir(parents=True)

    def _boom(*a, **k):
        raise RuntimeError("disk error")

    monkeypatch.setattr("vaf.core.session.get_session_workspace_dir", _boom)
    assert mgr.delete(sess.id) is True
    # Cleanup was skipped (the patched resolver raised before finding a real
    # path), so the folder is untouched - but the session record is gone
    # either way, which is the invariant this test protects.
    assert chat_dir.is_dir()
