# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Tests for the shared per-chat workspace resolver.

Every file-creating sub-agent (documents, research, coder projects) routes its
output through this resolver so all artifacts of a chat land in the chat's own
folder (VAF_Projects/<uid[:8]>/<session_id>/) and show up in the WebUI
workspace browser.
"""
import shutil
from pathlib import Path

import pytest

from vaf.core.platform import Platform
from vaf.core.session import (
    SessionManager,
    get_session_workspace_dir,
    resolve_agent_output_dir,
)


@pytest.fixture
def session(monkeypatch):
    monkeypatch.delenv("VAF_SESSION_ID", raising=False)
    mgr = SessionManager()
    sess = mgr.new(name="ws-resolver-test", user_scope_id="cafe1234-0000-0000-0000-000000000000")
    mgr.save(sess, sync_state=False)
    chat_dir = Platform.documents_dir() / "VAF_Projects" / "cafe1234" / sess.id
    yield sess, chat_dir
    if chat_dir.exists():
        shutil.rmtree(chat_dir)


def test_resolver_returns_none_without_session(monkeypatch):
    monkeypatch.delenv("VAF_SESSION_ID", raising=False)
    monkeypatch.setattr("vaf.core.subagent_ipc.get_current_session_id", lambda: None)
    assert get_session_workspace_dir() is None


def test_resolver_without_create_requires_existing_dir(session):
    sess, chat_dir = session
    assert get_session_workspace_dir(sess.id) is None  # not created yet
    chat_dir.mkdir(parents=True)
    assert get_session_workspace_dir(sess.id) == chat_dir


def test_resolver_create_builds_user_scoped_chat_dir(session):
    sess, chat_dir = session
    result = get_session_workspace_dir(sess.id, create=True)
    assert result == chat_dir
    assert chat_dir.is_dir()


def test_resolver_uses_env_session_id(session, monkeypatch):
    sess, chat_dir = session
    chat_dir.mkdir(parents=True)
    monkeypatch.setenv("VAF_SESSION_ID", sess.id)
    assert get_session_workspace_dir() == chat_dir


def test_agent_output_dir_prefers_workspace(session, monkeypatch):
    sess, chat_dir = session
    monkeypatch.setenv("VAF_SESSION_ID", sess.id)
    default = Path("/tmp/vaf_legacy_docs_test")
    out = resolve_agent_output_dir(default)
    assert out == chat_dir
    assert chat_dir.is_dir()


def test_agent_output_dir_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.delenv("VAF_SESSION_ID", raising=False)
    monkeypatch.setattr("vaf.core.subagent_ipc.get_current_session_id", lambda: None)
    default = tmp_path / "VAF_Documents"
    out = resolve_agent_output_dir(default)
    assert out == default
    assert default.is_dir()
