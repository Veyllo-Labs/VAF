# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Taking an account's access away takes effect at once, for work already running too.

The measured gaps, each pinned here: a deactivated or deleted account's token kept working
for its whole lifetime; a missing account row read as "no restriction" in the tool
allowlist; the permission cache was cleared BEFORE the commit, so a lookup in between
re-cached the old answer; a turn queued before a demotion ran with the old role; a waiting
confirmation dialog held the turn for five minutes whatever was pressed; automations kept
firing; and a standing "always" could be neither listed nor taken back. A foreground host
command and Stop: tests/test_foreground_stop.py. Each test names the mutation it catches.
"""
import asyncio
import contextlib
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from vaf.core import revocation

SCOPE = "ab12cd34-0000-4000-8000-0000000000a1"
OTHER = "ab12cd34-0000-4000-8000-0000000000b2"
PY = f'"{sys.executable}"'


@pytest.fixture(autouse=True)
def _clean_marks():
    yield
    for scope in (SCOPE, OTHER):
        revocation.restore_account(scope)


def _wait(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ── the permission lookup ────────────────────────────────────────────────────

class _Rows:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


def _fake_store(monkeypatch, row=None, fail=False):
    import vaf.auth.database as database
    from vaf.auth import permissions

    class _Session:
        async def execute(self, _query):
            if fail:
                raise ConnectionError("store down")
            return _Rows(row)

    @contextlib.asynccontextmanager
    async def _db():
        yield _Session()

    monkeypatch.setattr(database, "get_auth_db", _db)
    permissions.invalidate_permissions_cache()


def test_an_inactive_account_is_allowed_no_tool(monkeypatch):
    """Deactivating was documented as THE lever to block everything and did not touch the
    tools. MUTATION: return the permissions of the row without looking at is_active."""
    from vaf.auth.permissions import resolve_allowed_tools
    _fake_store(monkeypatch, row=({"tools": []}, False))
    assert resolve_allowed_tools(SCOPE) == frozenset()


def test_a_deleted_account_is_allowed_no_tool(monkeypatch):
    """No row read as "no restriction", so a deleted account's live turn had every tool.
    MUTATION: answer None for a missing row."""
    from vaf.auth.permissions import resolve_allowed_tools, resolve_allowed_workflows
    _fake_store(monkeypatch, row=None)
    assert resolve_allowed_tools(SCOPE) == frozenset()
    assert resolve_allowed_workflows(SCOPE) == frozenset()


def test_an_active_account_with_an_empty_list_stays_unrestricted(monkeypatch):
    """The creation default `[]` keeps meaning "no restriction" for an account that stands."""
    from vaf.auth.permissions import resolve_allowed_tools
    _fake_store(monkeypatch, row=({"tools": []}, True))
    assert resolve_allowed_tools(SCOPE) is None


def test_an_unreachable_store_keeps_the_desktop_default(monkeypatch):
    from vaf.auth.permissions import resolve_allowed_tools
    _fake_store(monkeypatch, fail=True)
    assert resolve_allowed_tools(SCOPE) is None


@pytest.mark.parametrize("standing,token_role,expected", [
    (("active", "user"), "user", True),
    (("inactive", "user"), "user", False),
    (("missing", ""), "user", False),
    (("active", "user"), "admin", False),     # demoted since the token was issued
    (("unknown", ""), "admin", True),         # store unreachable: the desktop default
])
def test_a_token_stands_only_while_its_account_does(standing, token_role, expected):
    """MUTATION: drop the role comparison, and a demoted admin's token keeps admin rights."""
    from vaf.auth.permissions import token_still_stands
    assert token_still_stands(standing, token_role) is expected


# ── the HTTP lane ────────────────────────────────────────────────────────────

def _app():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from vaf.auth.middleware import AuthMiddleware

    async def _ok(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/api/supervisor/status", _ok)])
    app.add_middleware(AuthMiddleware)
    return app


@pytest.mark.parametrize("standing,status", [
    (("inactive", "user"), 401),
    (("missing", ""), 401),
    (("active", "admin"), 401),   # the token says user, the store says admin: issued before a change
    (("active", "user"), 200),
    (("unknown", ""), 200),
])
def test_a_network_token_of_an_account_that_no_longer_stands_is_401(monkeypatch, standing, status):
    """MUTATION: skip the standing check in the middleware, and every row answers 200."""
    from starlette.testclient import TestClient

    import vaf.auth.crypto as crypto
    import vaf.auth.permissions as permissions
    monkeypatch.setattr(crypto, "decode_token", lambda tok: {
        "type": "access", "sub": "u1", "username": "alice", "role": "user",
        "user_scope_id": SCOPE})

    async def _standing(scope):
        return standing

    monkeypatch.setattr(permissions, "account_standing_async", _standing)
    client = TestClient(_app(), client=("192.168.1.50", 40000))
    r = client.get("/api/supervisor/status", headers={"Authorization": "Bearer x"})
    assert r.status_code == status


def test_the_socket_handshake_asks_the_same_question():
    """The WebSocket lane has no HTTP middleware in front of it, and it has two token lanes:
    the network one and the localhost-only one (local network off), which requires a token
    too and kept a demoted admin's role. MUTATION: drop the check from either lane."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "web_server.py").read_text(
        encoding="utf-8")
    handshake = src[src.index("async def websocket_endpoint"):]
    handshake = handshake[:handshake.index("elif type == ")]
    assert handshake.count("await token_account_stands(payload)") == 2


# ── the admin route ──────────────────────────────────────────────────────────

def _fake_user_route(monkeypatch, *, active=True, role="user", tools=None):
    import vaf.api.user_routes as ur
    import vaf.auth.permissions as permissions

    calls = []
    user = SimpleNamespace(id="11111111-0000-4000-8000-000000000001", user_scope_id=SCOPE,
                           role=role, is_active=active, permissions={"tools": tools or []},
                           updated_at=None)

    class _Result:
        def scalar_one_or_none(self):
            return user

    class _Session:
        async def execute(self, _query):
            return _Result()

        async def commit(self):
            calls.append("commit")

        async def delete(self, _obj):
            calls.append("delete")

    @contextlib.asynccontextmanager
    async def _db():
        yield _Session()

    async def _others(db, target_id):
        return 1

    monkeypatch.setattr(ur, "get_auth_db", _db)
    monkeypatch.setattr(ur, "count_other_active_admins", _others)
    monkeypatch.setattr(permissions, "invalidate_permissions_cache",
                        lambda scope=None: calls.append("invalidate"))
    monkeypatch.setattr(revocation, "revoke_account", lambda s: calls.append("revoke"))
    monkeypatch.setattr(revocation, "restore_account", lambda s: calls.append("restore"))
    monkeypatch.setattr(revocation, "stop_account_work", lambda s: calls.append("stop"))
    return calls


ADMIN = {"user_id": "99999999-0000-4000-8000-000000000009", "role": "admin",
         "user_scope_id": OTHER}


def test_the_cache_is_cleared_after_the_commit_and_the_work_stopped(monkeypatch):
    """Cleared BEFORE the commit, a lookup in between re-cached the old answer for the
    cache's lifetime. MUTATION: invalidate before db.commit()."""
    import vaf.api.user_routes as ur
    calls = _fake_user_route(monkeypatch)
    asyncio.run(ur.update_user("11111111-0000-4000-8000-000000000001",
                               ur.UserUpdate(is_active=False), admin=ADMIN))
    assert calls == ["commit", "invalidate", "revoke"]


def test_reactivating_lifts_the_mark(monkeypatch):
    import vaf.api.user_routes as ur
    calls = _fake_user_route(monkeypatch, active=False)
    asyncio.run(ur.update_user("11111111-0000-4000-8000-000000000001",
                               ur.UserUpdate(is_active=True), admin=ADMIN))
    assert calls == ["commit", "invalidate", "restore"]


def test_narrowing_the_tools_stops_running_work(monkeypatch):
    """A turn already running keeps the tools it started with otherwise. MUTATION: leave
    `narrowed` out of the stop decision."""
    import vaf.api.user_routes as ur
    calls = _fake_user_route(monkeypatch, tools=["read_file", "host_bash"])
    asyncio.run(ur.update_user("11111111-0000-4000-8000-000000000001",
                               ur.UserUpdate(tools=["read_file"]), admin=ADMIN))
    assert calls == ["commit", "invalidate", "stop"]


def test_widening_stops_nothing(monkeypatch):
    import vaf.api.user_routes as ur
    calls = _fake_user_route(monkeypatch, tools=["read_file"])
    asyncio.run(ur.update_user("11111111-0000-4000-8000-000000000001",
                               ur.UserUpdate(tools=["read_file", "host_bash"]), admin=ADMIN))
    assert calls == ["commit", "invalidate"]


def test_deleting_an_account_revokes_it(monkeypatch):
    import vaf.api.user_routes as ur
    calls = _fake_user_route(monkeypatch)
    asyncio.run(ur.delete_user("11111111-0000-4000-8000-000000000001", admin=ADMIN))
    assert calls == ["delete", "commit", "invalidate", "revoke"]


# ── work already running ─────────────────────────────────────────────────────

class _Note:
    name = "read_note"
    description = "returns a fixed note"
    parameters = {"type": "object", "properties": {}}
    identity_kwargs = ()

    def run(self, **kwargs):
        return "NOTE: ok"


def test_the_funnel_refuses_a_revoked_account_before_its_admin_exemption():
    """A turn queued before a demotion or a deletion carries the old role. MUTATION: drop
    the is_revoked check in ToolCaller.execute, and the admin role runs the tool."""
    from vaf.core.tool_dispatch import ToolCaller
    caller = ToolCaller({"read_note": _Note()}, user_scope_id=SCOPE, user_role="admin")
    revocation.revoke_account(SCOPE)
    assert caller.execute("read_note", {}).startswith("Security Error:")
    revocation.restore_account(SCOPE)
    assert caller.execute("read_note", {}) == "NOTE: ok"


def test_a_running_tool_call_ends_when_its_account_is_revoked():
    """Stop alone polled the chat's flag; a revocation reaches every lane through the
    funnel's own stop check. MUTATION: pass the caller's stop_check through unchanged."""
    from vaf.core.bounded_run import STOPPED_PREFIX, cancel_requested
    from vaf.core.tool_dispatch import ToolCaller

    class _Slow(_Note):
        name = "slow"
        timeout_seconds = 30

        def run(self, **kwargs):
            while not cancel_requested():
                time.sleep(0.05)
            return "cancelled"

    caller = ToolCaller({"slow": _Slow()}, user_scope_id=SCOPE, user_role="user", poll=0.1)
    threading.Timer(0.3, revocation.revoke_account, args=(SCOPE,)).start()
    started = time.monotonic()
    out = caller.execute("slow", {})
    assert out.startswith(STOPPED_PREFIX) and time.monotonic() - started < 3


def test_revoking_stops_the_accounts_background_commands_and_tells_the_listeners(
        monkeypatch, tmp_path):
    """Stop spares background commands on purpose; a revocation does not. MUTATION: leave
    the processes out of stop_account_work."""
    import psutil

    from vaf.core import processes, task_queue
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "config_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(processes, "_registry", {})
    monkeypatch.setattr(task_queue.TaskQueue, "add", lambda self, **kw: None)
    heard = []
    revocation.add_revocation_listener(heard.append)
    try:
        mine = processes.start(f'{PY} -c "import time; time.sleep(30)"',
                               session_id="web_chat-1", user_scope_id=SCOPE)
        theirs = processes.start(f'{PY} -c "import time; time.sleep(30)"',
                                 session_id="web_chat-2", user_scope_id=OTHER)
        summary = revocation.revoke_account(SCOPE)
        assert summary["processes"] == 1
        assert _wait(lambda: not mine.running)
        assert theirs.running and psutil.pid_exists(theirs.popen.pid)
        assert heard == [SCOPE]
    finally:
        revocation.remove_revocation_listener(heard.append)
        processes.terminate_all()


def test_the_queue_finds_an_accounts_work_under_any_of_its_names(monkeypatch):
    """The local admin's turns carry its scope or the canonical "default"; both are its work,
    as processes.list_for_scope already reads them. MUTATION: compare the raw scope strings."""
    import vaf.core.config as config
    from vaf.core.task_queue import TaskQueue
    monkeypatch.setattr(config, "get_local_admin_scope_id", lambda: "admin-scope-0001")
    tq = TaskQueue()
    chats = ["web_chat-q1", "web_chat-q2", "web_chat-q3", "web_chat-q4"]
    try:
        tq.add(session_id=chats[0], input_text="a", metadata={"user_scope_id": "admin-scope-0001"})
        tq.add(session_id=chats[1], input_text="b", metadata={"enqueue_user_scope_id": "default"})
        tq.add(session_id=chats[2], input_text="c", metadata={})
        tq.add(session_id=chats[3], input_text="d", metadata={"user_scope_id": OTHER})
        assert tq.sessions_for_scope("admin-scope-0001") >= {chats[0], chats[1]}
        assert not tq.sessions_for_scope("admin-scope-0001") & {chats[2], chats[3]}
        assert tq.sessions_for_scope(OTHER) == {chats[3]}
    finally:
        for chat in chats:
            tq.drop_queued_tasks_for_session(chat)


def test_a_waiting_confirmation_is_answered_cancel_when_the_account_is_revoked():
    """The dialog held the turn for five minutes whatever was pressed meanwhile. MUTATION:
    wait for the gate in one block again."""
    from vaf.core.agent import Agent
    from vaf.core.web_interface import get_web_interface

    # Built before the thread starts: two first calls at once both run its __init__, and the
    # second one empties the gate table the first had just registered in.
    web = get_web_interface()
    me = SimpleNamespace(current_session_id="web_chat-gate", _current_user_scope_id=SCOPE)
    box = {}
    t = threading.Thread(target=lambda: box.update(
        answer=Agent._ask_user_about_gate(me, "host_bash", "runs on the host")), daemon=True)
    t.start()
    try:
        assert _wait(lambda: "web_chat-gate" in web._pending_gates, 5)
        started = time.monotonic()
        revocation.revoke_account(SCOPE)
        t.join(timeout=3)
        assert box.get("answer") == "cancel" and time.monotonic() - started < 1.5
    finally:
        web.cancel_gate("web_chat-gate")
        t.join(timeout=3)


def test_stop_answers_a_waiting_confirmation_at_once():
    """The Stop button left the dialog open. MUTATION: drop cancel_gate from stop_session."""
    from vaf.core.web_interface import get_web_interface
    web = get_web_interface()
    web.open_gate("web_chat-stop", {"tool": "host_bash"})
    gate = web._pending_gates["web_chat-stop"]
    try:
        result = revocation.stop_session("web_chat-stop")
        assert result["gate_cancelled"] is True
        assert gate["event"].is_set() and gate["decision"][0] == "cancel"
    finally:
        get_web_interface().cancel_gate("web_chat-stop")


# ── work that has not started yet ────────────────────────────────────────────

def test_account_stands_reads_the_directory(monkeypatch):
    """MUTATION: treat a scope missing from a non-empty directory as standing."""
    from vaf.core import tool_dispatch
    rows = [{"username": "alice", "user_scope_id": SCOPE, "active": True},
            {"username": "bob", "user_scope_id": OTHER, "active": False}]
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", lambda: rows)
    assert revocation.account_stands(SCOPE) is True
    assert revocation.account_stands(OTHER) is False
    assert revocation.account_stands("ab12cd34-0000-4000-8000-0000000000c3") is False
    assert revocation.account_stands(None) is True
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", lambda: [])
    assert revocation.account_stands(OTHER) is True       # nothing known: the desktop default
    revocation.revoke_account(SCOPE)
    assert revocation.account_stands(SCOPE) is False


def test_an_automation_of_an_account_without_access_does_not_run(monkeypatch, tmp_path):
    """Automations kept firing for a deactivated or deleted account. MUTATION: drop the
    account_stands check at the top of run_task."""
    from vaf.core import automation
    from vaf.core.lock_manager import LockManager
    manager = automation.AutomationManager(storage_dir=str(tmp_path))
    taken = []
    monkeypatch.setattr(LockManager, "acquire", lambda lock_id: taken.append(lock_id) or True)
    task = automation.AutomationTask(name="daily report", prompt="hi", user_scope_id=SCOPE)
    revocation.revoke_account(SCOPE)
    out = manager.run_task(task, new_terminal=False)
    assert out.startswith("[SKIPPED]") and taken == []


def test_a_workflow_automation_stops_and_delivers_nothing_once_revoked(monkeypatch, tmp_path):
    """The workflow lane delivered and returned on its own, before the revocation check, and
    ran its steps with no stop check at all. MUTATION: drop check_stop from engine.execute, or
    the revocation check in front of its delivery."""
    import vaf.core.agent as agent_mod
    import vaf.workflows.engine as engine_mod
    from vaf.core import automation
    from vaf.core.lock_manager import LockManager

    class _Agent:
        tools = {}
        _current_username = "alice"
        _tool_authorizer = None

        def __init__(self, *a, **k):
            pass

        def load_model(self):
            pass

        def init_chat(self):
            pass

        def shutdown(self):
            pass

    seen = {}

    class _Engine:
        def __init__(self, *a, **k):
            pass

        def execute(self, steps, variables=None, check_stop=None, **k):
            revocation.revoke_account(SCOPE)          # taken away while the steps run
            seen["stop"] = check_stop() if check_stop else None
            return SimpleNamespace(success=True, paused=False, final_output="", error=None)

    pushed = []
    monkeypatch.setattr(agent_mod, "Agent", _Agent)
    monkeypatch.setattr(engine_mod, "WorkflowEngine", _Engine)
    monkeypatch.setattr(automation, "bind_identity", lambda *a, **k: None)
    monkeypatch.setattr(automation, "resolve_scope_identity", lambda *a, **k: None)
    monkeypatch.setattr(automation, "_push_result_to_web_ui", lambda *a, **k: pushed.append(a))
    monkeypatch.setattr(LockManager, "acquire", lambda lock_id: True)
    monkeypatch.setattr(LockManager, "release", lambda lock_id: None)
    manager = automation.AutomationManager(storage_dir=str(tmp_path))
    task = automation.AutomationTask(name="report", user_scope_id=SCOPE,
                                     workflow_steps=[{"tool": "write_file", "args": {"path": "r.md"}}])
    out = manager.run_task(task, new_terminal=False)
    assert seen["stop"] is True
    assert out.startswith("[REVOKED]") and pushed == []


def test_a_prompt_automation_revoked_mid_run_is_not_stamped_or_saved(monkeypatch, tmp_path):
    """The prompt lane wrote the output file and stamped the run as successful before it
    asked whether the account still stands. MUTATION: move the check back behind them."""
    import vaf.core.agent as agent_mod
    from vaf.core import automation
    from vaf.core.lock_manager import LockManager

    class _Agent:
        tools = {}
        history = []
        _current_username = "alice"
        _tool_authorizer = None

        def __init__(self, *a, **k):
            pass

        def load_model(self):
            pass

        def init_chat(self):
            pass

        def chat_step(self, prompt, stream_callback=None, **k):
            revocation.revoke_account(SCOPE)          # taken away while the prompt runs
            if stream_callback:
                stream_callback("the report")

        def _clean_reasoning(self, text):
            return text

        def shutdown(self):
            pass

    pushed = []
    monkeypatch.setattr(agent_mod, "Agent", _Agent)
    monkeypatch.setattr(automation, "bind_identity", lambda *a, **k: None)
    monkeypatch.setattr(automation, "resolve_scope_identity", lambda *a, **k: None)
    monkeypatch.setattr(automation, "_push_result_to_web_ui", lambda *a, **k: pushed.append(a))
    monkeypatch.setattr(LockManager, "acquire", lambda lock_id: True)
    monkeypatch.setattr(LockManager, "release", lambda lock_id: None)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    manager = automation.AutomationManager(storage_dir=str(tmp_path / "tasks"))
    task = automation.AutomationTask(name="report", prompt="write the report", user_scope_id=SCOPE,
                                     output_path=str(out_dir), output_format="markdown")
    out = manager.run_task(task, new_terminal=False)
    assert out.startswith("[REVOKED]") and pushed == []
    assert task.last_run is None, "a revoked run was stamped as a success"
    assert list(out_dir.iterdir()) == [], "a revoked run wrote its output file"


# ── standing grants ──────────────────────────────────────────────────────────

@pytest.fixture
def _trust_dir(monkeypatch, tmp_path):
    from vaf.core import trust
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "config_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(trust, "_chat_grants", {})
    return tmp_path


def test_standing_grants_are_listed_and_taken_back(_trust_dir, tmp_path):
    """A grant skips the question before any event is emitted, and nothing could list or
    revoke one. MUTATION: leave the chat grants out of revoke_standing_grants."""
    from vaf.core import trust
    folder = tmp_path / "project"
    folder.mkdir()
    trust.set_tool_policy("python_exec", "allow", SCOPE)
    trust.mark_trusted_dir(folder, SCOPE)
    trust.grant_tool_for_chat("python_exec", SCOPE, "web_chat-1")
    trust.set_tool_policy("python_exec", "allow", OTHER)

    grants = trust.list_standing_grants(SCOPE)
    assert grants["tools"] == {"python_exec": {"always": True, "chats": 1}}
    assert len(grants["dirs"]) == 1

    removed = trust.revoke_standing_grants(SCOPE, tools=["python_exec"])
    assert removed["tools"] == ["python_exec"]
    assert trust.get_tool_policy("python_exec", SCOPE) == "ask"
    assert not trust.has_chat_grant("python_exec", SCOPE, "web_chat-1")
    assert trust.get_tool_policy("python_exec", OTHER) == "allow"     # another person's stays

    assert trust.revoke_standing_grants(SCOPE, everything=True)["dirs"] == grants["dirs"]
    assert trust.list_standing_grants(SCOPE) == {"tools": {}, "dirs": []}


def _grant_app(user):
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware

    from vaf.api.security_routes import router as security_router
    from vaf.api.user_routes import router as user_router

    class _As(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.user = user
            return await call_next(request)

    app = FastAPI()
    app.include_router(security_router)
    app.include_router(user_router)
    app.add_middleware(_As)
    return app


def test_an_account_sees_and_revokes_only_its_own_grants(_trust_dir):
    """MUTATION: read the grants without the caller's scope (the overview did: the local
    admin's, whoever was looking)."""
    from starlette.testclient import TestClient

    from vaf.core import trust
    trust.set_tool_policy("python_exec", "allow", SCOPE)
    trust.set_tool_policy("host_bash", "allow", OTHER)
    client = TestClient(_grant_app({"username": "alice", "role": "user", "user_scope_id": SCOPE}))
    assert list(client.get("/api/security/grants").json()["tools"]) == ["python_exec"]
    r = client.post("/api/security/grants/revoke", json={"tools": ["python_exec", "host_bash"]})
    assert r.json()["removed"]["tools"] == ["python_exec"]
    assert trust.get_tool_policy("host_bash", OTHER) == "allow"


def test_only_an_admin_reads_or_revokes_another_accounts_grants(_trust_dir, monkeypatch):
    from starlette.testclient import TestClient

    import vaf.api.user_routes as ur
    from vaf.core import trust
    trust.set_tool_policy("python_exec", "allow", SCOPE)

    async def _scope(user_id):
        return SCOPE

    monkeypatch.setattr(ur, "_scope_of_user", _scope)
    target = "/api/users/11111111-0000-4000-8000-000000000001/grants"
    user = TestClient(_grant_app({"username": "bob", "role": "user", "user_scope_id": OTHER}))
    assert user.get(target).status_code == 403
    assert user.post(f"{target}/revoke", json={"everything": True}).status_code == 403
    admin = TestClient(_grant_app({"username": "root", "role": "admin", "user_scope_id": OTHER}))
    assert list(admin.get(target).json()["tools"]) == ["python_exec"]
    assert admin.post(f"{target}/revoke", json={"everything": True}).json()["removed"]["tools"] == ["python_exec"]
    assert json.loads(json.dumps(trust.list_standing_grants(SCOPE))) == {"tools": {}, "dirs": []}


def test_the_grants_list_never_shows_another_accounts_grants():
    """Switching accounts in the user editor left the previous one's list - and its revoke
    buttons - on screen until the next answer, and a failed fetch kept it for good.
    A failed fetch says so, with a retry, instead of hiding the section.
    MUTATION: keep the old list on a failed fetch, accept a late answer for another account,
    or hide the section when the list could not be loaded."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "web" / "components" / "settings"
           / "StandingGrantsSection.tsx").read_text(encoding="utf-8")
    assert ("if (endpointRef.current === requested && seq === loadSeqRef.current) {\n"
            "            setLoaded(next ? { endpoint: requested, grants: next } : null);") in src
    assert "next = null;" in src
    assert "useEffect(() => { setLoaded(null); setLoadFailed(false); void load(); }, [load]);" in src
    empty = src[src.index("if (!data) {"):src.index("const tools = Object.entries")]
    assert "if (!loadFailed) return null;" in empty and "t('loadFailed')" in empty
    assert "onClick={() => void load()}" in empty
    # A revoke that answers after the admin moved to another account touches neither that
    # account's error line nor its list.
    revoke = src[src.index("const revoke = async"):src.index("if (!data) {")]
    assert revoke.index("if (endpointRef.current !== requested) return;") < revoke.index(
        "if (!ok) setFailed(true);") < revoke.index("void load();")


def test_a_chat_known_only_from_a_background_command_keeps_its_next_turn(monkeypatch):
    """Narrowing an account's tools stops its work. A chat with no turn running or queued,
    found only through a background command, kept the stop flag, and the runner swallowed
    that chat's next message. MUTATION: leave the flag set."""
    from vaf.core import processes
    from vaf.core.task_queue import TaskQueue
    tq = TaskQueue()
    monkeypatch.setattr(TaskQueue, "sessions_for_scope", lambda self, key: {"web_chat-run"})
    monkeypatch.setattr(processes, "list_for_scope",
                        lambda key: [SimpleNamespace(session_id="web_chat-idle")])
    monkeypatch.setattr(processes, "stop", lambda record: "stopped")
    try:
        summary = revocation.stop_account_work(SCOPE)
        assert summary == {"sessions": 2, "processes": 1}
        assert tq.should_stop("web_chat-run") and not tq.should_stop("web_chat-idle")
    finally:
        tq.clear_stop("web_chat-run")
        tq.clear_stop("web_chat-idle")


def test_the_machine_owner_stands_under_any_of_its_names(monkeypatch):
    """The owner's work may carry its scope or the canonical "default"; neither is an
    account the directory lists. MUTATION: compare the raw scope strings."""
    import vaf.core.config as config
    from vaf.core import tool_dispatch
    monkeypatch.setattr(config, "get_local_admin_scope_id", lambda: "admin-scope-0001")
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver",
                        lambda: [{"username": "alice", "user_scope_id": SCOPE, "active": True}])
    assert revocation.account_stands("default") is True
    assert revocation.account_stands("admin-scope-0001") is True
    assert revocation.account_stands(OTHER) is False
