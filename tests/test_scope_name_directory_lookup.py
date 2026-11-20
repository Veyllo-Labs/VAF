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

def _function_source(rel: str, name: str) -> str:
    src = (_REPO / rel).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} is gone from {rel}")


@pytest.mark.parametrize("rel, name, reads", [
    ("vaf/core/thinking_mode.py", "_resolve_username_for_scope", "account_name_for_scope("),
    ("vaf/core/thinking_mode.py", "_registered_scope_ids", "resolve_account_directory()"),
    ("vaf/core/config.py", "account_name_for_scope", "account_directory_index()"),
])
def test_the_lookups_read_the_directory_not_the_auth_store(rel, name, reads):
    """MUTATION: re-add `from vaf.auth.database import get_auth_db` to any of them."""
    body = _function_source(rel, name)
    for banned in ("vaf.auth", "get_auth_db", "asyncio.run", "Thread"):
        assert banned not in body, f"{name} still uses {banned}"
    assert reads in body


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


OWNER = "ownerx"


def _owner_named(monkeypatch, name=OWNER):
    import vaf.core.config as config_mod
    monkeypatch.setattr(config_mod, "get_local_admin_username", lambda: name)


def _rows(*pairs):
    return [{"username": name, "user_scope_id": scope} for name, scope in pairs]


# Each case: the directory, then {scope: the name it must resolve to}. A name the rule
# refuses resolves to the scope's synthetic name, never to the name another account has.
REFUSED = {
    # MUTATION: drop the "exactly one scope carries it" check (both scopes get "alice").
    "duplicate": (_rows(("alice", S_A), ("alice", S_B)),
                  {S_A: _synthetic(S_A), S_B: _synthetic(S_B)}),
    # Same MUTATION, and: index names without casefolding (Alice and alice are one folder
    # on a case-insensitive file system).
    "case_twin": (_rows(("Alice", S_A), ("alice", S_B)),
                  {S_A: _synthetic(S_A), S_B: _synthetic(S_B)}),
    # MUTATION: compare with the owner's name only instead of `is_local_admin_name`.
    "literal_admin": (_rows(("admin", S_A)), {S_A: _synthetic(S_A)}),
    "literal_admin_upper": (_rows(("ADMIN", S_A)), {S_A: _synthetic(S_A)}),
    # The owner's name, any case. Refused twice over: by `is_local_admin_name`, and by the
    # one-scope rule, which counts the owner's scope from the config (see the test below
    # for the first alone).
    "owner_name": (_rows((OWNER, S_A), ("OwnerX", S_B)),
                   {S_A: _synthetic(S_A), S_B: _synthetic(S_B)}),
    # MUTATION: drop the `safe_entry_name` check (each maps to users/ownerx or escapes).
    "path_alias": (_rows((OWNER + "/", S_A), (OWNER + "/.", S_B), ("./" + OWNER, S_C)),
                   {S_A: _synthetic(S_A), S_B: _synthetic(S_B), S_C: _synthetic(S_C)}),
    "path_escape": (_rows(("..", S_A), ("a\\b", S_B), ("bob\x00", S_C)),
                    {S_A: _synthetic(S_A), S_B: _synthetic(S_B), S_C: _synthetic(S_C)}),
    # MUTATION: drop the `scope_` prefix check (UNLISTED's synthetic name is taken).
    "synthetic_prefix": (_rows((_synthetic(UNLISTED), S_A), ("Scope_1234abcd", S_B)),
                         {S_A: _synthetic(S_A), S_B: _synthetic(S_B)}),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_a_directory_name_another_account_could_have_is_refused(case, store, monkeypatch):
    from vaf.core.thinking_mode import _resolve_username_for_scope

    _owner_named(monkeypatch)
    rows, expected = REFUSED[case]
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", lambda: rows)
    assert {scope: _resolve_username_for_scope(scope) for scope in expected} == expected
    # The owner's own scope answers the owner's name from the config.
    assert _resolve_username_for_scope(LOCAL) == OWNER


def test_the_owners_name_is_refused_without_an_owner_scope(store, monkeypatch):
    """With no owner scope in the config the one-scope rule cannot see the owner, so
    `is_local_admin_name` alone keeps the owner's name from a tenant. MUTATION: drop
    `is_local_admin_name`."""
    import vaf.core.config as config_mod
    from vaf.core.thinking_mode import _resolve_username_for_scope

    _owner_named(monkeypatch)
    monkeypatch.setattr(config_mod, "get_local_admin_scope_id", lambda: "")
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver",
                        lambda: _rows(("OwnerX", S_A)))
    assert _resolve_username_for_scope(S_A) == _synthetic(S_A)


def test_a_name_one_account_alone_carries_is_kept(store, monkeypatch):
    """The rule refuses only what another account could have: two distinct names, a name
    with a dot or a space inside, and a scope listed twice under one name all resolve."""
    from vaf.core.thinking_mode import _resolve_username_for_scope

    _owner_named(monkeypatch)
    rows = _rows(("alice", S_A), ("bob.smith", S_B), ("carol", S_C), ("Carol", S_C.upper()))
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", lambda: rows)
    assert [_resolve_username_for_scope(s) for s in SCOPES] == ["alice", "bob.smith", "carol"]


def test_an_unreadable_owner_name_fails_closed(store, monkeypatch):
    """When the owner's name cannot be read, no directory name is trusted: a row with the
    owner's name for a tenant would otherwise pass. MUTATION: read the owner's name in a
    try/except that answers "" (the guard then fails open)."""
    import vaf.core.config as config_mod
    from vaf.core.thinking_mode import _resolve_username_for_scope

    def broken():
        raise OSError("config unreadable")

    monkeypatch.setattr(config_mod, "get_local_admin_username", broken)
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver",
                        lambda: _rows((OWNER, S_A), ("alice", S_B)))
    assert _resolve_username_for_scope(S_A) == _synthetic(S_A)
    assert _resolve_username_for_scope(S_B) == _synthetic(S_B)


def test_the_name_lookup_builds_one_index_per_block(store, monkeypatch):
    """A listing names one account per task: inside a block every lookup uses one index,
    and what a caller got back cannot change it. MUTATION: drop the `held.index` reuse in
    `account_directory_index` (one index per lookup, a scan of the whole directory each)."""
    from vaf.core.thinking_mode import _resolve_username_for_scope

    rows = [{"username": f"user{i}", "user_scope_id": f"ab12cd34-0000-4000-8000-{i:012d}"}
            for i in range(200)]
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", lambda: rows)
    built = []
    real = tool_dispatch.AccountDirectoryIndex

    class Counting(real):
        __slots__ = ()

        def __init__(self, got):
            built.append(1)
            super().__init__(got)

    monkeypatch.setattr(tool_dispatch, "AccountDirectoryIndex", Counting)
    with account_directory_once():
        names = [_resolve_username_for_scope(row["user_scope_id"]) for row in rows]
        index = tool_dispatch.account_directory_index()
        with pytest.raises(TypeError):
            index.rows[0]["username"] = "mallory"
        with pytest.raises(TypeError):
            index.by_scope[rows[1]["user_scope_id"]][0]["username"] = "mallory"
        assert _resolve_username_for_scope(rows[0]["user_scope_id"]) == "user0"
    assert names == [row["username"] for row in rows]
    assert len(built) == 1


def test_a_registered_scope_matches_whatever_its_case(store, monkeypatch):
    """The dead-session cap keeps a registered account whose directory scope and
    last_interaction.json key differ in case, either way round; an unregistered scope
    silent as long is dropped. MUTATIONS: drop the `.lower()` in `_registered_scope_ids`;
    drop it at the cap's membership test."""
    import time as _time

    import vaf.core.thinking_mode as tm

    data = store.root.parent / "data"
    data.mkdir()
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: data))
    eight_days = _time.time() - 8 * 24 * 3600
    (data / "last_interaction.json").write_text(json.dumps({
        S_A: {"ts": eight_days}, S_B.upper(): {"ts": eight_days}, UNLISTED: {"ts": eight_days},
    }), encoding="utf-8")
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver",
                        lambda: _rows(("alice", S_A.upper()), ("bob", S_B)))
    assert tm.get_idle_user_scope_ids(10) == [S_A, S_B.upper()]


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
