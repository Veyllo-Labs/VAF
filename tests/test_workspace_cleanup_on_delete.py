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
import os
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


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_delete_keeps_a_workspace_with_an_unreadable_subtree(session):
    """The fail-safe must be real: os.walk's default (onerror=None) silently
    SKIPS a permission-denied subdirectory, so a subtree full of files was
    classified 'empty' and rmtree'd (audit finding, fbf9250..HEAD range).
    With onerror=raise the walk error now takes the documented 'treat as has
    content' path - anything we cannot fully inspect is kept."""
    mgr, sess, chat_dir = session
    locked = chat_dir / "locked"
    locked.mkdir(parents=True)
    (locked / "data.txt").write_text("real content behind a permission wall")
    locked.chmod(0o000)
    try:
        mgr.delete(sess.id)
        assert chat_dir.is_dir()  # kept: could not prove it is empty
    finally:
        locked.chmod(0o755)  # so the fixture teardown can rmtree


def test_delete_skips_workspace_removal_while_a_subagent_runs(session, monkeypatch):
    """Delete-vs-concurrent-write race: a live sub-agent/workflow may drop its
    first output file between the emptiness check and the rmtree. With a
    RUNNING (or pending) IPC task for this session, workspace removal is
    skipped outright - the session record still goes."""
    import types

    import vaf.core.subagent_ipc as ipc_mod

    mgr, sess, chat_dir = session
    chat_dir.mkdir(parents=True)

    fake_task = types.SimpleNamespace(task_id="t1", agent_type="workflow:research")

    class _FakeIpc:
        def get_active_tasks(self, session_id=None):
            return [fake_task] if session_id == str(sess.id) else []

        def get_pending_tasks(self, session_id=None):
            return []

    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: _FakeIpc())

    assert mgr.delete(sess.id) is True
    assert chat_dir.is_dir()  # empty, but a live run may be about to write


def test_delete_removes_empty_workspace_when_other_sessions_have_subagents(session, monkeypatch):
    """The guard is session-scoped: someone ELSE's running sub-agent must not
    keep this chat's empty folder alive."""
    import types

    import vaf.core.subagent_ipc as ipc_mod

    mgr, sess, chat_dir = session
    chat_dir.mkdir(parents=True)

    other = types.SimpleNamespace(task_id="t2", agent_type="workflow:research")

    class _FakeIpc:
        def get_active_tasks(self, session_id=None):
            return [] if session_id == str(sess.id) else [other]

        def get_pending_tasks(self, session_id=None):
            return []

    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: _FakeIpc())

    assert mgr.delete(sess.id) is True
    assert not chat_dir.exists()
