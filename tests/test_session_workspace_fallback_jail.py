# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A chat's stored project folder is used only when it is a folder its caller may use.

A chat records two folders: ``project_path`` (its workspace) and
``runtime_state["last_project_path"]`` (the last project). A record written before the coder
asked the account half can name a folder in ANOTHER account's tree. Measured before the
change: the web workspace resolver (browse, upload, delete and mkdir all go through it) and
the headless runner (the turn's project folder, whose VAF.md goes into the prompt) asked
only `is_unsafe_project_dir`, which knows no account, so account A was served B's folder
and B's VAF.md. Both now ask `project_dir_refusal` for the caller (the chat's owner in the
runner); a refused record is skipped and the chat falls through to its own folder. An admin
reaches every tree, as before.
"""
import shutil

import pytest

import vaf.api.config_routes as config_routes
import vaf.core.web_server as ws
import vaf.tools.filesystem as fs
from vaf.core.headless_runner import _chat_project_paths
from vaf.core.session import SessionManager, get_user_projects_root

A = "ab12cd34-0000-4000-8000-00000000000a"
B = "cd34ef56-0000-4000-8000-00000000000b"


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    fs._shared_room_roots_cache.clear()
    monkeypatch.setattr(fs, "_shared_room_roots", lambda scope: [])
    b_proj = get_user_projects_root(B) / "chat" / "proj"
    b_proj.mkdir(parents=True)
    (b_proj / "VAF.md").write_text("B-PRIVATE-PROJECT-NOTES\n")
    mgr = SessionManager()
    sess = mgr.new(name="fallback-jail", user_scope_id=A)
    mgr.save(sess, sync_state=False)
    chat_dir = get_user_projects_root(A) / sess.id
    yield mgr, sess, b_proj, chat_dir
    if chat_dir.exists():
        shutil.rmtree(chat_dir)
    mgr.delete(sess.id)
    fs._shared_room_roots_cache.clear()


def _as(monkeypatch, scope, role):
    monkeypatch.setattr(config_routes, "get_current_user_or_local_admin",
                        lambda request: {"user_scope_id": scope, "role": role})


def _record(mgr, sess, field, folder):
    if field == "project_path":
        sess.project_path = str(folder)
    else:
        sess.runtime_state["last_project_path"] = str(folder)
    mgr.save(sess, sync_state=False)


@pytest.mark.parametrize("field", ["project_path", "last_project_path"])
def test_the_web_resolver_never_serves_another_accounts_folder(world, monkeypatch, field):
    """MUTATION: restore is_unsafe_project_dir as the resolver's only check - red. Return ""
    on the first refusal instead of trying the next candidate - the create case goes red."""
    mgr, sess, b_proj, chat_dir = world
    _record(mgr, sess, field, b_proj)
    _as(monkeypatch, A, "user")

    assert ws._resolve_session_workspace(sess.id, request=None, create=False) == ""
    assert not chat_dir.exists()
    assert ws._resolve_session_workspace(sess.id, request=None, create=True) == str(chat_dir)
    assert chat_dir.is_dir()


@pytest.mark.parametrize("field", ["project_path", "last_project_path"])
def test_for_an_admin_the_stored_folder_is_served_as_before(world, monkeypatch, field):
    mgr, sess, b_proj, chat_dir = world
    _record(mgr, sess, field, b_proj)
    _as(monkeypatch, "ef56ab78-0000-4000-8000-00000000000e", "admin")
    assert ws._resolve_session_workspace(sess.id, request=None, create=False) == str(b_proj)


def test_the_callers_own_folder_is_still_served(world, monkeypatch):
    mgr, sess, b_proj, chat_dir = world
    mine = get_user_projects_root(A) / "older" / "site"
    mine.mkdir(parents=True)
    try:
        _record(mgr, sess, "last_project_path", mine)
        _as(monkeypatch, A, "user")
        assert ws._resolve_session_workspace(sess.id, request=None) == str(mine)
    finally:
        shutil.rmtree(get_user_projects_root(A) / "older")


@pytest.mark.parametrize("field", ["project_path", "last_project_path"])
def test_the_headless_turn_gets_no_folder_and_no_vaf_md_of_another_account(world, field):
    """The runner's turn reads VAF.md from edit_path (Agent._project_context_block).

    MUTATION: drop the account half in _chat_project_paths (ask is_unsafe_project_dir only) -
    red, B's folder becomes the turn's project and its VAF.md reaches the prompt."""
    from types import SimpleNamespace

    from vaf.core.agent import Agent
    mgr, sess, b_proj, chat_dir = world
    _record(mgr, sess, field, b_proj)

    paths = _chat_project_paths(mgr, sess.id)
    assert str(b_proj) not in (paths.workspace, paths.edit_path)

    turn = SimpleNamespace(_current_chat_source="web", _chat_project_dir=paths.edit_path,
                           config={"n_ctx": 8192}, _answers_somebody_else=lambda: False)
    assert "B-PRIVATE-PROJECT-NOTES" not in Agent._project_context_block(turn)
    # The control: handed B's folder, the same block reads it.
    turn._chat_project_dir = str(b_proj)
    assert "B-PRIVATE-PROJECT-NOTES" in Agent._project_context_block(turn)
