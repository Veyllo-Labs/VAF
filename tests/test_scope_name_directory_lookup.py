# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The account behind a scope is named by the account directory, once per listing.

THE DEFECT. An automation's next run is read in its account's time zone, so every listing
named the account behind every tenant task, several times per task (the sort, the next run
on the wire, today's status line). The name came from `thinking_mode._resolve_username_for_scope`,
which opened the harness's auth store directly: no outage memory, no sharing. Measured with
the store unreachable, one `AutomationManager().list()` of six tenant tasks made 18 refused
connects and the next one 18 again; on Windows each costs about two seconds, and the
WebSocket listing ran on the event loop thread. An embedder, which runs no such store, got
the synthetic name and a refused connect per lookup.

THE RULE. The name is read from the account directory the application registered
(`set_account_directory_resolver`; VAF registers `permissions.list_accounts`, which remembers
an unreachable store for 30 seconds), and a listing reads that directory once
(`tool_dispatch.account_directory_once`).

Synthetic scopes, a scratch store, a fake auth store and a fake clock; no database.
"""
import ast
import asyncio
import contextlib
import json
import threading
import types
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from test_automation_access import _write, _ws
from vaf.core import automation as auto_mod
from vaf.core import tool_dispatch
from vaf.core.automation import AutomationManager, AutomationTask
from vaf.core.outage import OutageMemory
from vaf.core.platform import Platform
from vaf.core.tool_dispatch import account_directory_once, resolve_account_directory

LOCAL = "ab12cd34-0000-4000-8000-0000000000a0"     # the machine owner's scope
S_A = "ab12cd34-0000-4000-8000-0000000000c1"
S_B = "ab12cd34-0000-4000-8000-0000000000c2"
S_C = "ab12cd34-0000-4000-8000-0000000000c3"
UNLISTED = "ab12cd34-0000-4000-8000-0000000000ff"
SCOPES = (S_A, S_B, S_C)

_REPO = Path(__file__).resolve().parent.parent


def _synthetic(scope: str) -> str:
    return "scope_" + scope.replace("-", "")[:8]


class _Directory:
    """A counting account directory: alice, bob and carol, one per tenant scope."""

    def __init__(self, rows=None):
        self.calls = 0
        self.rows = rows if rows is not None else [
            {"username": "alice", "user_scope_id": S_A, "active": True, "role": "user"},
            {"username": "bob", "user_scope_id": S_B, "active": True, "role": "user"},
            {"username": "carol", "user_scope_id": S_C, "active": True, "role": "user"},
        ]
        self.loop_running = []

    def __call__(self):
        self.calls += 1
        try:
            asyncio.get_running_loop()
            self.loop_running.append(True)
        except RuntimeError:
            self.loop_running.append(False)
        return [dict(row) for row in self.rows]


class _RefusingStore:
    """`get_auth_db` of a store that cannot be reached; counts the sessions asked for."""

    def __init__(self):
        self.calls = 0

    @contextlib.asynccontextmanager
    async def session(self, *_a, **_kw):
        self.calls += 1
        raise ConnectionRefusedError("store down")
        yield  # pragma: no cover - an async generator that never yields a session


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A scratch automation store with six tenant tasks, two in each of three scopes; the
    machine owner's scope and name pinned; no directory registered; the auth store refuses
    (a test that dials it is counted, never a real database)."""
    import vaf.auth.database as auth_db
    import vaf.core.config as config_mod
    import vaf.core.thinking_workspace as tw

    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(config_mod, "get_local_admin_scope_id", lambda: LOCAL)
    monkeypatch.setattr(config_mod, "get_local_admin_username", lambda: "admin")
    monkeypatch.setattr(tw, "sync_automation_status_to_workspace", lambda **kw: None)
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", None)
    refusing = _RefusingStore()
    monkeypatch.setattr(auth_db, "get_auth_db", refusing.session)
    monkeypatch.setattr(auto_mod, "_manager", None)
    root = tmp_path / "automations"
    root.mkdir()
    for i in range(6):
        scope = SCOPES[i % 3]
        _write(root, AutomationTask(id=f"c0de{i:04d}", name=f"Task {i}", prompt=f"prompt {i}",
                                    frequency="daily", time=f"1{i}:00", enabled=True,
                                    user_scope_id=scope))
    return types.SimpleNamespace(root=root, refusing=refusing)


@pytest.fixture
def directory(store, monkeypatch):
    fake = _Directory()
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", fake)
    return fake


# ── AC1: the framework no longer reads the harness's auth store ──────────────

def _function_source(name: str) -> str:
    path = _REPO / "vaf" / "core" / "thinking_mode.py"
    src = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} is gone from thinking_mode.py")


@pytest.mark.parametrize("name", ["_resolve_username_for_scope", "_registered_scope_ids"])
def test_the_lookups_read_the_directory_not_the_auth_store(name):
    """MUTATION: re-add `from vaf.auth.database import get_auth_db` to either function."""
    body = _function_source(name)
    for banned in ("vaf.auth", "get_auth_db", "asyncio.run", "Thread"):
        assert banned not in body, f"{name} still uses {banned}"
    assert "resolve_account_directory()" in body


# ── AC2: the name is the directory's ─────────────────────────────────────────

def test_a_scope_resolves_to_its_directory_name(directory, store):
    """MUTATIONS: restore the auth-store body (alice becomes the synthetic name); drop the
    `.lower()` on either side (an upper-case spelling misses)."""
    from vaf.core.config import resolve_caller_username

    assert resolve_caller_username(None, S_A, allow_lookup=True) == "alice"
    assert resolve_caller_username(None, S_A.upper(), allow_lookup=True) == "alice"
    assert resolve_caller_username(None, f"  {S_B} ", allow_lookup=True) == "bob"
    assert resolve_caller_username(None, UNLISTED, allow_lookup=True) == _synthetic(UNLISTED)
    assert resolve_caller_username(None, LOCAL, allow_lookup=True) == "admin"
    # A directory that stores the scope in upper case still names it.
    directory.rows = [{"username": "dave", "user_scope_id": S_C.upper()}]
    assert resolve_caller_username(None, S_C, allow_lookup=True) == "dave"
    assert store.refusing.calls == 0


def test_the_owners_name_is_never_given_to_another_scope(store, monkeypatch):
    """A directory row that names a tenant scope with the machine owner's name (any case)
    must not hand that scope the owner's files; the owner's own row still resolves.
    MUTATION: drop the `_is_owner_name_for_other_scope` check."""
    from vaf.core.thinking_mode import _resolve_username_for_scope

    rows = [{"username": "Admin", "user_scope_id": S_A},
            {"username": "admin", "user_scope_id": LOCAL}]
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", lambda: rows)
    assert _resolve_username_for_scope(S_A) == _synthetic(S_A)
    assert _resolve_username_for_scope(LOCAL) == "admin"


def test_without_a_directory_the_name_is_synthetic_and_nothing_dials(store):
    """MUTATION: restore the auth-store body (the refusing store counts a session)."""
    from vaf.core.config import resolve_caller_username
    from vaf.core.thinking_mode import _registered_scope_ids, _resolve_username_for_scope

    assert resolve_caller_username(None, S_A, allow_lookup=True) == _synthetic(S_A)
    assert _resolve_username_for_scope(S_A) == _synthetic(S_A)
    assert _resolve_username_for_scope("") is None
    assert _registered_scope_ids() == set()
    assert store.refusing.calls == 0


def test_registered_scopes_are_the_directorys(directory):
    from vaf.core.thinking_mode import _registered_scope_ids

    assert _registered_scope_ids() == set(SCOPES)


def test_the_next_run_follows_the_directory_accounts_time_zone(directory, monkeypatch):
    """The point of the name: alice's task reads in alice's time zone."""
    import vaf.core.user_time as user_time

    zone = "Pacific/Kiritimati"
    monkeypatch.setattr(user_time, "_load_identity",
                        lambda username, identity: {"timezone": zone} if username == "alice" else {})
    task = AutomationTask(id="c0de9999", name="t", prompt="p", frequency="daily", time="10:00",
                          enabled=True, user_scope_id=S_A)
    due = task.calculate_next_run()
    assert due.astimezone(ZoneInfo(zone)).strftime("%H:%M") == "10:00"


# ── AC3: one directory read per call ─────────────────────────────────────────

def _running_scheduler(monkeypatch) -> AutomationManager:
    mgr = AutomationManager()
    mgr._running = True
    mgr._scheduler_thread = types.SimpleNamespace(is_alive=lambda: True)
    mgr._clock_lock = threading.Lock()
    mgr._clock_jobs = {}
    monkeypatch.setattr(auto_mod, "_scheduler_manager", mgr)
    return mgr


class _NoThread:
    def __init__(self, *a, **kw):
        pass

    def start(self):
        pass

    def is_alive(self):
        return False


def _start_scheduler(monkeypatch):
    # The loop thread is not started: it would read the directory on its own tick.
    proxy = types.SimpleNamespace(**{k: getattr(threading, k) for k in dir(threading)
                                     if not k.startswith("__")})
    proxy.Thread = _NoThread
    monkeypatch.setattr(auto_mod, "threading", proxy)
    mgr = AutomationManager()
    mgr.start_scheduler()
    assert len(mgr._clock_jobs) == 6
    mgr._running = False


def _ws_listing(monkeypatch):
    replies = _ws(monkeypatch, role="user", scope=S_A,
                  frames=[({"type": "get_automations"}, "automations_list")])
    listed = replies[0]["automations"]
    assert len(listed) == 2 and all(row["next_run"] for row in listed), replies[0]


def _cli_listing(monkeypatch):
    from vaf.core import terminal_text

    printed = []
    monkeypatch.setattr(terminal_text.TerminalConsole, "print",
                        lambda self, *a, **kw: printed.append(a))
    auto_mod.list_automations()
    assert printed


def _tool_listing(monkeypatch):
    from vaf.tools.automation import ListAutomationsTool

    out = ListAutomationsTool().run(user_scope_id=S_A, user_role="user")
    assert out.count("Next: ") == 2, out


SITES = {
    "manager_list": lambda mp: AutomationManager().list(),
    "next_run_utc": lambda mp: auto_mod.get_next_automation_run_utc(S_A),
    "refresh_scheduler": lambda mp: (_running_scheduler(mp),
                                     auto_mod.refresh_scheduler_from_disk("test")),
    "start_scheduler": _start_scheduler,
    "websocket": _ws_listing,
    "agent_tool": _tool_listing,
    "cli_list": _cli_listing,
}


@pytest.mark.parametrize("site", sorted(SITES))
def test_one_directory_read_per_listing(site, directory, monkeypatch):
    """MUTATION: remove the `with account_directory_once()` from any one site (the count
    becomes the number of tasks it names, or more)."""
    SITES[site](monkeypatch)
    assert directory.calls == 1


def test_the_websocket_lists_off_the_event_loop(directory, monkeypatch):
    """The listing reads the directory, and the harness's resolver may ask a store: never
    on the loop thread that serves every connection. MUTATION: call `_automations_listing`
    in the `get_automations` branch directly instead of through the executor."""
    _ws_listing(monkeypatch)
    assert directory.loop_running == [False]


# ── AC4: a remembered outage dials nothing ───────────────────────────────────

def test_a_remembered_outage_dials_nothing(store, monkeypatch):
    """VAF's own resolver over a store that refuses. MUTATIONS: restore the auth-store body
    (the store is asked once per tenant task); make `list_accounts` skip the down check
    (every listing asks again)."""
    from vaf.auth import permissions
    from vaf.core.config import resolve_caller_username
    from vaf.core.thinking_mode import _registered_scope_ids

    clock = {"t": 1000.0}
    monkeypatch.setattr(permissions, "_store_url", lambda: "postgresql://test-store")
    monkeypatch.setattr(permissions, "_store_down",
                        OutageMemory(permissions._STORE_DOWN_S, clock=lambda: clock["t"]))
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", permissions.list_accounts)

    def listing():
        tasks = AutomationManager().list()
        assert len(tasks) == 6
        assert all(t.next_run_datetime is not None for t in tasks)
        return tasks

    asked = []
    for _ in range(2):
        before = store.refusing.calls
        listing()
        asked.append(store.refusing.calls - before)
    before = store.refusing.calls
    assert _registered_scope_ids() == set()
    assert resolve_caller_username(None, S_A, allow_lookup=True) == _synthetic(S_A)
    assert auto_mod._resolve_username(S_B) == _synthetic(S_B)
    asked.append(store.refusing.calls - before)
    clock["t"] += permissions._STORE_DOWN_S + 1
    before = store.refusing.calls
    listing()
    asked.append(store.refusing.calls - before)
    assert asked == [1, 0, 0, 1]


# ── AC5: the once-block ──────────────────────────────────────────────────────

def test_once_block_scoping(directory):
    """MUTATIONS: omit the reset at exit (the second block reads nothing); make the holder a
    module global (the two threads share one read); drop the nested-block check (the inner
    block reads again); return the kept rows themselves (an edit reaches the next read)."""
    resolve_account_directory()
    resolve_account_directory()
    assert directory.calls == 2, "outside a block every read asks the resolver"

    for expected in (3, 4):
        with account_directory_once():
            for _ in range(2):
                # A caller that edits what it got does not edit what the block keeps.
                got = resolve_account_directory()
                got[0]["username"] = "mallory"
                got.append({"username": "mallory"})
            again = resolve_account_directory()
        assert directory.calls == expected
        assert [row["username"] for row in again] == ["alice", "bob", "carol"]

    with account_directory_once():
        resolve_account_directory()
        with account_directory_once():
            resolve_account_directory()
        resolve_account_directory()
    assert directory.calls == 5, "a nested block shares the outer read"

    with pytest.raises(RuntimeError):
        with account_directory_once():
            resolve_account_directory()
            raise RuntimeError("boom")
    assert directory.calls == 6
    resolve_account_directory()
    assert directory.calls == 7, "a block left by an exception is reset"

    gate = threading.Barrier(2)
    errors = []

    def worker():
        try:
            with account_directory_once():
                gate.wait(5)
                resolve_account_directory()
                resolve_account_directory()
                gate.wait(5)
        except Exception as e:  # noqa: BLE001 - reported by the assert below
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors
    assert directory.calls == 9, "each thread reads once in its own block"


def test_an_empty_read_is_kept_for_the_block(store, monkeypatch):
    """A raising resolver (a store that is down) is an empty directory, read once per block."""
    calls = []

    def raising():
        calls.append(1)
        raise ConnectionRefusedError("down")

    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", raising)
    with account_directory_once():
        assert resolve_account_directory() == []
        assert resolve_account_directory() == []
    assert len(calls) == 1


def test_the_thread_allowlist_names_no_lookup():
    """The two lookups start no thread any more, so the allowlist names neither."""
    src = (_REPO / "tests" / "test_thinking_run_context_isolation.py").read_text(encoding="utf-8")
    assert '"_resolve_username_for_scope"' not in src
    assert '"_registered_scope_ids"' not in src


def test_the_seed_is_six_tenant_tasks(store):
    """Guards the counts above: a store with fewer tenant tasks would pass any mutation."""
    files = sorted(p.relative_to(store.root).parts[0] for p in store.root.rglob("*.json"))
    assert files == sorted(SCOPES * 2)
    assert all(json.loads(p.read_text(encoding="utf-8"))["user_scope_id"] in SCOPES
               for p in store.root.rglob("*.json"))
