# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Supervisor watchdog authorization: unit payloads carry user-authored task
text and task_ids enable /cancel, so a non-admin must only ever see and kill
units of sessions their own scope owns. Admin (and tokenless localhost, which
resolves to the local admin) keeps the unfiltered watchdog view."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

import vaf.api.supervisor_routes as srv


class _Task:
    def __init__(self, task_id, session_id, desc):
        self.task_id = task_id
        self.agent_type = "coding"
        self.session_id = session_id
        self.status = "running"
        self.task_description = desc
        self.created_at = datetime.now().isoformat()
        self.last_heartbeat = datetime.now().isoformat()


class _FakeIpc:
    def __init__(self, tasks):
        self._tasks = tasks
        self.failed = []

    def get_active_tasks(self, session_id=None):
        return [t for t in self._tasks if not session_id or t.session_id == session_id]

    def fail_task(self, task_id, msg):
        self.failed.append(task_id)


@pytest.fixture()
def fake_ipc(monkeypatch):
    ipc = _FakeIpc([_Task("t1", "s1", "alice private prompt"), _Task("t2", "s2", "bob private prompt")])
    import vaf.core.subagent_ipc as ipc_mod
    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: ipc)
    monkeypatch.setattr(srv, "_owned_session_ids", lambda scope: {"s1"} if scope == "scope1" else set())

    async def no_names(_ids):
        return {}
    monkeypatch.setattr(srv, "_usernames_for_sessions", no_names)
    return ipc


def _req(user):
    return SimpleNamespace(state=SimpleNamespace(user=user))


def test_non_admin_sees_only_own_sessions(fake_ipc):
    req = _req({"role": "user", "user_scope_id": "scope1", "username": "alice"})
    out = asyncio.run(srv.supervisor_status(req, session=None))
    assert [u["task_id"] for u in out["units"]] == ["t1"]


def test_non_admin_forged_session_gets_empty(fake_ipc):
    req = _req({"role": "user", "user_scope_id": "scope1", "username": "alice"})
    out = asyncio.run(srv.supervisor_status(req, session="s2"))
    assert out["units"] == []


def test_admin_keeps_full_watchdog_view(fake_ipc):
    req = _req({"role": "admin", "user_scope_id": "adm", "username": "root"})
    out = asyncio.run(srv.supervisor_status(req, session=None))
    assert {u["task_id"] for u in out["units"]} == {"t1", "t2"}


def test_tokenless_localhost_resolves_to_admin(fake_ipc):
    out = asyncio.run(srv.supervisor_status(_req(None), session=None))
    assert {u["task_id"] for u in out["units"]} == {"t1", "t2"}


def test_non_admin_cannot_cancel_foreign_unit(fake_ipc):
    req = _req({"role": "user", "user_scope_id": "scope1", "username": "alice"})
    out = srv.supervisor_cancel(srv.CancelBody(task_id="t2"), req)
    assert out["ok"] is False and fake_ipc.failed == []


def test_non_admin_can_cancel_own_unit(fake_ipc, monkeypatch):
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "stop_webui_subagent_process_by_task", staticmethod(lambda tid: 0))
    req = _req({"role": "user", "user_scope_id": "scope1", "username": "alice"})
    out = srv.supervisor_cancel(srv.CancelBody(task_id="t1"), req)
    assert out["ok"] is True and fake_ipc.failed == ["t1"]
