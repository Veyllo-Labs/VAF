# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""_resolve_session_workspace(..., create=True): opening a chat in the WebUI
always has a workspace to point at, even before anything was ever saved -
the folder chip is a standing "this chat has a workspace" affordance, not a
"you already saved something" indicator (see docs/web-ui/WEB_UI.md, "Session
Workspace Window", and the workspace chip comment in web/app/page.tsx).
create=False (the default, and every non-WebUI-open call site) keeps the old
read-only behavior: no folder, no path.
"""
import shutil

import pytest

import vaf.api.config_routes as config_routes
import vaf.core.web_server as ws
from vaf.core.platform import Platform
from vaf.core.session import SessionManager


@pytest.fixture
def owned_session(monkeypatch):
    mgr = SessionManager()
    sess = mgr.new(name="eager-create-test", user_scope_id="beef9999-0000-0000-0000-000000000000")
    mgr.save(sess, sync_state=False)
    chat_dir = Platform.documents_dir() / "VAF_Projects" / "beef9999" / sess.id

    monkeypatch.setattr(
        config_routes,
        "get_current_user_or_local_admin",
        lambda request: {"user_scope_id": "beef9999-0000-0000-0000-000000000000"},
    )

    yield sess, chat_dir

    if chat_dir.exists():
        shutil.rmtree(chat_dir)
    mgr.delete(sess.id)


def test_create_false_returns_empty_when_nothing_exists_yet(owned_session):
    sess, chat_dir = owned_session
    assert not chat_dir.exists()

    result = ws._resolve_session_workspace(sess.id, request=None, create=False)

    assert result == ""
    assert not chat_dir.exists()  # read-only path must never create


def test_create_true_makes_the_folder_and_returns_it(owned_session):
    sess, chat_dir = owned_session
    assert not chat_dir.exists()

    result = ws._resolve_session_workspace(sess.id, request=None, create=True)

    assert result == str(chat_dir)
    assert chat_dir.is_dir()


def test_create_true_is_idempotent_and_does_not_touch_existing_content(owned_session):
    sess, chat_dir = owned_session
    chat_dir.mkdir(parents=True)
    (chat_dir / "keep.txt").write_text("already here")

    result = ws._resolve_session_workspace(sess.id, request=None, create=True)

    assert result == str(chat_dir)
    assert (chat_dir / "keep.txt").read_text() == "already here"


def test_create_true_never_creates_for_an_unowned_session(monkeypatch, owned_session):
    sess, chat_dir = owned_session
    # A different user than the session owner asks for it.
    monkeypatch.setattr(
        config_routes,
        "get_current_user_or_local_admin",
        lambda request: {"user_scope_id": "someone-else-0000-0000-000000000000"},
    )

    with pytest.raises(Exception):  # HTTPException(403) - ownership still enforced
        ws._resolve_session_workspace(sess.id, request=None, create=True)

    assert not chat_dir.exists()
