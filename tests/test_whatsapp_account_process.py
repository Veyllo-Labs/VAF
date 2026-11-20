# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""WhatsApp start, stop and restart act on the caller's own process unless the caller is an
admin.

THE DEFECT, measured on the code before this change, with a signed-in ordinary account Alice
whose login and own switch were both present while the admin's process ran: her `POST /stop`
ended every account's process, her `POST /restart` restarted every account, her `POST /start`
answered "already running" and started nothing for her, `GET /status` told her `running: true`
without a process of her own, and an expired session of hers stopped the whole bridge and
turned the global switch off.

THE RULE: an admin drives the whole bridge (`start_bridge`, `stop_bridge`, `restart_bridge`,
and `start_missing_accounts` while it runs); anybody else drives their own account's process
(`start_account`, `stop_account`, `restart_account`) as their sign-in names it. Every spawn and
every stop runs under `_spawn_lock`, the bridge's shutdown included, so one account never gets
two processes and no process outlives the bridge.

Fake processes only: no Node, no network, no port.
"""
import io
import json
import os
import random
import threading
import time

import pytest

from vaf.api import whatsapp_bridge as wa
from vaf.core.config import Config
from vaf.core.platform import Platform

ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"
ALICE_SCOPE = "ab12cd34-0000-4000-8000-000000000001"
BOB_SCOPE = "ab12cd34-0000-4000-8000-000000000002"
CAROL_SCOPE = "ab12cd34-0000-4000-8000-000000000003"
SCOPES = {"admin": ADMIN_SCOPE, "alice": ALICE_SCOPE, "bob": BOB_SCOPE, "carol": CAROL_SCOPE}

ADMIN = {"username": "admin", "role": "admin", "user_scope_id": ADMIN_SCOPE}
ALICE = {"username": "alice", "role": "user", "user_scope_id": ALICE_SCOPE}


class FakeProc:
    """What the bridge needs of a Node process. Empty pipes, so the reader threads end at once."""

    def __init__(self, name):
        self.name = name
        self.returncode = None
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
        self.stdin = io.StringIO()

    def poll(self):
        return self.returncode

    def terminate(self):
        if self.returncode is None:
            self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        if self.returncode is None:
            self.returncode = -9


class World:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.spawned = []          # every FakeProc ever spawned, in order
        self.spawn_delay = 0.0
        self.on_spawn = None       # optional hook(name) run inside the fake spawn
        self.calls = []            # recorded bridge-level calls

    # config on disk
    def cfg(self):
        return json.loads(Config.CONFIG_FILE.read_text(encoding="utf-8"))

    def write(self, data):
        Config.CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")

    def global_switch(self, on):
        data = self.cfg()
        data.setdefault("whatsapp_config", {})["enabled"] = bool(on)
        self.write(data)

    def own_switch(self, scope, on):
        data = self.cfg()
        data.setdefault("connection_enabled_by_scope", {}).setdefault(scope, {})["whatsapp"] = bool(on)
        self.write(data)

    def link(self, name):
        d = Config.APP_DIR / "users" / name / "whatsapp"
        d.mkdir(parents=True, exist_ok=True)
        (d / "creds.json").write_text("{}", encoding="utf-8")

    # processes
    def names(self):
        return [p.name for p in self.spawned]

    def live(self, name):
        with wa._process_lock:
            proc = wa._processes.get(name)
        return proc is not None and proc.poll() is None

    def wait_for(self, cond, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.005)
        return cond()


@pytest.fixture
def world(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    cfg = app / "config.json"
    monkeypatch.setattr(Config, "CONFIG_FILE", cfg)
    monkeypatch.setattr(Config, "APP_DIR", app)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr("vaf.core.config.scope_id_for_username", lambda name: SCOPES.get(str(name or "")))
    w = World(tmp_path)
    w.write({"local_admin_scope_id": ADMIN_SCOPE, "local_admin_username": "admin",
             "whatsapp_config": {"enabled": True, "chat_sync_interval_sec": 0}})

    # A fresh bridge: every piece of module state the lifecycle touches.
    monkeypatch.setattr(wa, "_processes", {})
    monkeypatch.setattr(wa, "_scope_by_user", {})
    monkeypatch.setattr(wa, "_connection_status", {})
    monkeypatch.setattr(wa, "_bridge_stop", threading.Event())
    monkeypatch.setattr(wa, "_spawns_open", False)
    monkeypatch.setattr(wa, "_bridge_thread", None)
    monkeypatch.setattr(wa, "_sender_thread", None)
    monkeypatch.setattr(wa, "_chat_sync_thread", None)
    monkeypatch.setattr(wa, "_outgoing_queue", None)

    def spawn(username, auth_dir):
        if w.on_spawn:
            w.on_spawn(username)
        if w.spawn_delay:
            time.sleep(w.spawn_delay)
        proc = FakeProc(username)
        w.spawned.append(proc)
        return proc

    node_js = tmp_path / "wa-bridge.js"
    node_js.write_text("", encoding="utf-8")
    monkeypatch.setattr(wa, "_run_user_process", spawn)
    monkeypatch.setattr(wa, "_node_path", lambda: "node")
    monkeypatch.setattr(wa, "_wa_bridge_path", lambda: node_js)
    monkeypatch.setattr(wa, "ensure_bridge_deps", lambda: (True, ""))
    monkeypatch.setattr(wa, "_sender_loop", lambda: None)
    monkeypatch.setattr(wa, "_chat_sync_loop", lambda: None)
    monkeypatch.setattr(wa, "_write_bridge_state", lambda running: None)
    monkeypatch.setattr(wa, "set_whatsapp_reply_callback", lambda cb: None)
    monkeypatch.setattr("vaf.core.channel_pairing.refile_whatsapp_owner_numbers", lambda: [])
    yield w
    # Teardown before monkeypatch restores the module: no bridge thread survives the test.
    wa._bridge_stop.set()
    thread = wa._bridge_thread
    if thread is not None:
        thread.join(timeout=5)


def _record(world, monkeypatch, *names, through=True):
    """Replace bridge functions with recorders that log (name, args) and, by default, call
    the real one."""
    for name in names:
        real = getattr(wa, name)

        def rec(*args, _name=name, _real=real, **kwargs):
            world.calls.append((_name,) + tuple(args) + tuple(f"{k}={v}" for k, v in sorted(kwargs.items())))
            return _real(*args, **kwargs) if through else None
        monkeypatch.setattr(wa, name, rec)


WHOLE = ("start_bridge", "stop_bridge", "restart_bridge", "start_missing_accounts")
ACCOUNT = ("start_account", "stop_account", "restart_account")


def _client(user):
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from vaf.api import whatsapp_routes

    app = FastAPI()

    @app.middleware("http")
    async def signed_in(request, call_next):
        if user is not None:
            request.state.user = dict(user)
        return await call_next(request)

    app.include_router(whatsapp_routes.router)
    return TestClient(app)


def _bridge_with(world, *names):
    """A running bridge whose processes are exactly `names` (each linked and switched on)."""
    for name in names:
        world.link(name)
        if name != "admin":
            world.own_switch(SCOPES[name], True)
    assert wa.start_bridge()
    assert world.wait_for(lambda: all(world.live(n) for n in names)), world.names()
    return {n: wa._processes[n] for n in names}


def _stop_and_join():
    wa.stop_bridge()
    wa._bridge_thread.join(timeout=5)
    assert not wa._bridge_thread.is_alive()


# -- AC1, AC2 -----------------------------------------------------------------------------------

def test_a_non_admin_stop_stops_only_their_own_process(world, monkeypatch):
    """MUTATION: the route's non-admin branch calls `stop_bridge()`; or `stop_account` sets
    `_bridge_stop`."""
    procs = _bridge_with(world, "admin", "alice")
    _record(world, monkeypatch, *WHOLE)
    res = _client(ALICE).post("/api/whatsapp/stop")
    assert res.status_code == 200, res.text
    assert world.calls == []
    assert procs["alice"].returncode is not None and "alice" not in wa._processes
    assert world.live("admin") and procs["admin"].returncode is None
    assert wa._scope_by_user == {"admin": ADMIN_SCOPE}
    assert not wa._bridge_stop.is_set() and wa.is_bridge_running()


def test_a_non_admin_restart_restarts_only_their_own_process(world, monkeypatch):
    """MUTATION: the non-admin branch calls `restart_bridge()`."""
    procs = _bridge_with(world, "admin", "alice")
    _record(world, monkeypatch, *WHOLE, *ACCOUNT)
    res = _client(ALICE).post("/api/whatsapp/restart")
    assert res.status_code == 200, res.text
    assert world.calls == [("restart_account", "alice", ALICE_SCOPE), ("stop_account", "alice"),
                           ("start_account", "alice", ALICE_SCOPE)]
    assert procs["alice"].returncode is not None
    assert world.live("alice") and wa._processes["alice"] is not procs["alice"]
    assert wa._processes["admin"] is procs["admin"] and procs["admin"].returncode is None
    assert world.names().count("admin") == 1 and world.names().count("alice") == 2


# -- AC3, AC4 -----------------------------------------------------------------------------------

def test_a_newly_linked_non_admin_starts_without_a_restart(world, monkeypatch):
    """MUTATION: `start_account` answers "already running" while the bridge runs (the old
    route's behaviour)."""
    procs = _bridge_with(world, "admin")
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    _record(world, monkeypatch, *WHOLE)
    res = _client(ALICE).post("/api/whatsapp/start")
    assert res.status_code == 200, res.text
    assert res.json()["message"] == wa.ACCOUNT_STARTED
    assert world.live("alice") and world.names() == ["admin", "alice"]
    assert wa._processes["admin"] is procs["admin"] and procs["admin"].returncode is None
    assert world.calls == []
    # A second start finds the process alive and spawns nothing.
    res = _client(ALICE).post("/api/whatsapp/start")
    assert res.status_code == 200 and res.json()["message"] == wa.ACCOUNT_ALREADY_RUNNING
    assert world.names() == ["admin", "alice"]


def test_a_non_admin_start_on_a_stopped_bridge_starts_only_them(world):
    """MUTATION: drop the `only` filter in `_run_bridge`."""
    world.link("admin")
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    assert not wa.is_bridge_running()
    ok, msg = wa.start_account("alice", ALICE_SCOPE)
    assert ok and msg == wa.ACCOUNT_STARTED
    assert world.wait_for(lambda: world.live("alice"))
    time.sleep(0.1)
    assert world.names() == ["alice"]
    assert not world.live("admin")
    _stop_and_join()
    assert wa._processes == {} and all(p.returncode is not None for p in world.spawned)


# -- AC5 ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["not linked", "lane off", "scope mismatch", "global off"])
@pytest.mark.parametrize("running", [False, True])
def test_a_non_admin_cannot_start_what_is_not_theirs(world, monkeypatch, case, running):
    """MUTATION: remove the `_get_users_to_run` membership check, or the scope equality in it."""
    if running:
        _bridge_with(world, "admin")
    if case != "not linked":
        world.link("alice")
    world.own_switch(ALICE_SCOPE, case != "lane off")
    # The scope-mismatch caller signs in as "alice" with another account's scope, whose own
    # switch is on too.
    world.own_switch(BOB_SCOPE, True)
    user = dict(ALICE, user_scope_id=BOB_SCOPE) if case == "scope mismatch" else ALICE
    if case == "global off":
        world.global_switch(False)
    before = world.names()
    _record(world, monkeypatch, *WHOLE, "start_account", "restart_account", "_spawn_account")
    for path in ("/api/whatsapp/start", "/api/whatsapp/restart"):
        res = _client(user).post(path)
        assert res.status_code == 400, (path, res.status_code, res.text)
        expected = ("WhatsApp is switched off on this VAF. An admin has to switch it on first."
                    if case == "global off" else wa.NOT_STARTABLE)
        assert res.json()["detail"] == expected
    time.sleep(0.05)
    assert world.names() == before
    assert not world.live("alice")
    assert not any(c[0] in WHOLE or c[0] == "_spawn_account" for c in world.calls), world.calls
    if case == "global off":
        assert world.calls == []


# -- AC6 ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("user", [ADMIN, None], ids=["signed-in admin", "tokenless desktop"])
def test_an_admin_keeps_whole_bridge_control(world, monkeypatch, user):
    """MUTATION: the admin's `/start` back to an "already running" no-op; the admin's `/stop`
    routed to `stop_account`."""
    client = _client(user)
    world.link("admin")
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    _record(world, monkeypatch, *WHOLE, *ACCOUNT)

    # Start while down: the whole bridge, everybody switched on.
    res = client.post("/api/whatsapp/start")
    assert res.status_code == 200, res.text
    assert world.calls == [("start_bridge",)]
    assert world.wait_for(lambda: world.live("admin") and world.live("alice"))

    # Start while running: a newly linked, switched-on account starts without a restart.
    world.calls.clear()
    world.link("bob")
    world.own_switch(BOB_SCOPE, True)
    res = client.post("/api/whatsapp/start")
    assert res.status_code == 200, res.text
    assert world.calls == [("start_missing_accounts",)]
    assert world.live("bob") and sorted(world.names()) == ["admin", "alice", "bob"]
    world.calls.clear()
    res = client.post("/api/whatsapp/start")
    assert res.json()["message"] == "WhatsApp bridge already running."
    assert sorted(world.names()) == ["admin", "alice", "bob"]

    # Restart: the whole bridge.
    world.calls.clear()
    res = client.post("/api/whatsapp/restart")
    assert res.status_code == 200, res.text
    assert world.calls[0] == ("restart_bridge",) and ("stop_bridge",) in world.calls
    assert not any(c[0] in ACCOUNT for c in world.calls)
    assert world.wait_for(lambda: all(world.live(n) for n in ("admin", "alice", "bob")))

    # Stop: the whole bridge.
    world.calls.clear()
    res = client.post("/api/whatsapp/stop")
    assert res.status_code == 200, res.text
    assert world.calls == [("stop_bridge",)]
    wa._bridge_thread.join(timeout=5)
    assert wa._processes == {} and all(p.returncode is not None for p in world.spawned)


# -- AC7 ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("bridge", ["running", "down"])
def test_concurrent_starts_spawn_one_process(world, bridge):
    """MUTATION: drop `_spawn_lock` from `start_account`'s spawn into a running bridge."""
    if bridge == "running":
        _bridge_with(world, "admin")
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    world.spawn_delay = 0.05
    barrier = threading.Barrier(2)
    results = []

    def go():
        barrier.wait()
        results.append(wa.start_account("alice", ALICE_SCOPE))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert [ok for ok, _ in results] == [True, True], results
    assert world.wait_for(lambda: world.live("alice"))
    time.sleep(0.15)
    assert world.names().count("alice") == 1, world.names()


def test_concurrent_bridge_starts_start_one_bridge(world, monkeypatch):
    """MUTATION: drop `_lifecycle_lock` from `start_bridge` (the routes run it off the event
    loop now, so two starts are no longer serialised by it)."""
    world.link("admin")
    sender_loops = []

    def slow_deps():
        time.sleep(0.05)
        return True, ""
    monkeypatch.setattr(wa, "ensure_bridge_deps", slow_deps)
    monkeypatch.setattr(wa, "_sender_loop", lambda: sender_loops.append(1))
    barrier = threading.Barrier(2)

    def go():
        barrier.wait()
        wa.start_bridge()

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert world.wait_for(lambda: world.live("admin"))
    assert len(sender_loops) == 1
    assert world.names() == ["admin"]


def test_no_orphan_after_bridge_stop(world):
    """A spawn in flight when the bridge stops is either refused or terminated by the
    shutdown, never left running. MUTATION: drop `_spawn_lock` from `_run_bridge`'s shutdown."""
    _bridge_with(world, "admin")
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    spawning = threading.Event()

    def hook(name):
        if name == "alice":
            spawning.set()
    world.on_spawn = hook
    world.spawn_delay = 0.2
    starter = threading.Thread(target=lambda: wa.start_account("alice", ALICE_SCOPE))
    starter.start()
    assert spawning.wait(timeout=3)
    wa.stop_bridge()
    starter.join(timeout=5)
    wa._bridge_thread.join(timeout=5)
    assert not wa._bridge_thread.is_alive()
    assert wa._processes == {} and wa._scope_by_user == {}
    assert [p.name for p in world.spawned if p.returncode is None] == []


def test_an_account_start_into_a_bridge_with_nobody_keeps_it_up(world, monkeypatch):
    """The bridge that finds nobody to run exits, unless an account start raced in first; a
    start after its exit is refused instead of leaving a process without a bridge.
    MUTATION: the early exit ignores `_processes`; or it leaves `_spawns_open` set."""
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    gate = threading.Event()
    real_users = wa._get_users_to_run

    def users():
        if threading.current_thread() is wa._bridge_thread:
            gate.wait(timeout=3)     # the bridge thread reads nobody, after the start below
            return []
        return real_users()
    monkeypatch.setattr(wa, "_get_users_to_run", users)
    # start_bridge(only="carol"): carol is nobody here, so the bridge thread will find no user.
    assert wa.start_bridge(only="carol")
    ok, msg = wa.start_account("alice", ALICE_SCOPE)      # races in while the thread reads
    assert ok and msg == wa.ACCOUNT_STARTED, msg
    gate.set()
    time.sleep(0.1)
    assert wa.is_bridge_running() and world.live("alice")
    _stop_and_join()
    assert wa._processes == {} and all(p.returncode is not None for p in world.spawned)

    # A bridge that exits with nobody refuses a late spawn.
    monkeypatch.setattr(wa, "_get_users_to_run", lambda: [])
    assert wa.start_bridge(only="carol")
    wa._bridge_thread.join(timeout=5)
    with wa._spawn_lock:
        assert wa._spawn_account(ALICE_SCOPE, "alice", world.tmp) is False
    assert not world.live("alice")


# -- AC8 ----------------------------------------------------------------------------------------

def test_session_expiry_stops_only_that_account(world, monkeypatch):
    """MUTATION: the handler back to `stop_bridge()`; or the switch write made unconditional."""
    procs = _bridge_with(world, "admin", "alice")
    _record(world, monkeypatch, "stop_bridge", "restart_bridge")

    wa._dispatch_bridge_event("alice", ALICE_SCOPE, "qr", {})
    assert world.calls == []
    assert world.cfg()["whatsapp_config"]["enabled"] is True
    assert world.cfg()["connection_enabled_by_scope"][ALICE_SCOPE]["whatsapp"] is True
    assert "alice" not in wa._processes and procs["alice"].returncode is not None
    assert wa._processes["admin"] is procs["admin"] and procs["admin"].returncode is None
    assert not wa._bridge_stop.is_set()

    # The local admin's own expiry: their switch IS the global one.
    world.link("alice")
    assert wa.start_account("alice", ALICE_SCOPE)[0]
    wa._dispatch_bridge_event("admin", ADMIN_SCOPE, "qr", {})
    assert world.calls == []
    assert world.cfg()["whatsapp_config"]["enabled"] is False
    assert procs["admin"].returncode is not None and "admin" not in wa._processes
    assert world.live("alice")
    assert not wa._bridge_stop.is_set()


# -- AC9 ----------------------------------------------------------------------------------------

def test_status_running_is_the_callers_own_process(world, monkeypatch):
    """MUTATION: `running = is_bridge_running()` restored in `/status` or in `/dashboard`."""
    _bridge_with(world, "admin")
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    monkeypatch.setattr(wa, "get_connection_status", lambda username, wait_timeout=2.0: False)
    monkeypatch.setattr(wa, "get_whatsapp_chats", lambda username, wait_timeout=2.0: [])
    monkeypatch.setattr(wa, "get_contact_names", lambda username, wait_timeout=2.0: {})
    monkeypatch.setattr(wa, "get_lid_mappings", lambda username, wait_timeout=2.0: [])
    for user, expected in ((ALICE, False), (ADMIN, True)):
        client = _client(user)
        status = client.get("/api/whatsapp/status")
        assert status.status_code == 200, status.text
        assert status.json()["running"] is expected, (user["username"], status.json())
        dash = client.get("/api/whatsapp/dashboard")
        assert dash.status_code == 200, dash.text
        assert dash.json()["running"] is expected, (user["username"], "dashboard")


# -- Fuzz: the spawn gate -------------------------------------------------------------------------

FUZZ_ACCOUNTS = ("admin", "alice", "bob")


def _fuzz_schedule(world, rng):
    """One schedule: 8 operations over 4 threads, fake spawns sleeping 0 to 5 ms. Returns the
    invariant violations it saw."""
    violations = []
    bridge_threads = []
    threads_lock = threading.Lock()

    def on_spawn(name):
        with wa._process_lock:
            current = wa._processes.get(name)
        if current is not None and current.poll() is None:
            violations.append(f"second live process for {name}")
        time.sleep(rng_spawn.uniform(0, 0.005))
    rng_spawn = random.Random(rng.random())
    world.on_spawn = on_spawn

    def remember_bridge():
        with threads_lock:
            if wa._bridge_thread is not None and wa._bridge_thread not in bridge_threads:
                bridge_threads.append(wa._bridge_thread)

    def op(kind, name):
        if kind == "start_account":
            wa.start_account(name, SCOPES[name])
        elif kind == "stop_account":
            wa.stop_account(name)
        elif kind == "start_missing":
            wa.start_missing_accounts()
        elif kind == "stop_bridge":
            wa.stop_bridge()
        elif kind == "start_bridge":
            # "carol" is linked with her switch off: a bridge started for her finds nobody.
            wa.start_bridge(only=None if name == "admin" else name)
        remember_bridge()
        with wa._process_lock:
            if set(wa._processes) != set(wa._scope_by_user):
                violations.append(f"registry out of step after {kind}")

    kinds = ("start_account", "start_account", "stop_account", "start_missing", "stop_bridge", "start_bridge")
    def draw():
        kind = rng.choice(kinds)
        return kind, rng.choice(FUZZ_ACCOUNTS + ("carol",) if kind == "start_bridge" else FUZZ_ACCOUNTS)

    plan = [[draw() for _ in range(2)] for _ in range(4)]

    def worker(ops):
        for kind, name in ops:
            op(kind, name)

    workers = [threading.Thread(target=worker, args=(ops,)) for ops in plan]
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout=5)
    wa.stop_bridge()
    remember_bridge()
    for t in bridge_threads:
        t.join(timeout=5)
        if t.is_alive():
            violations.append("bridge thread did not end")
    with wa._process_lock:
        if wa._processes or wa._scope_by_user:
            violations.append(f"registered after the stop: {sorted(wa._processes)}")
    alive = [p.name for p in world.spawned if p.returncode is None]
    if alive:
        violations.append(f"orphans: {alive}")
    return violations


def _fuzz_reset(world):
    world.spawned.clear()
    with wa._process_lock:
        wa._processes.clear()
        wa._scope_by_user.clear()
    wa._bridge_stop.clear()


def test_spawn_gate_fuzz(world):
    """Seeded interleavings of account starts and stops, the admin's start into a running
    bridge, and the bridge's start and stop. Oracle: no second live process for one account at
    any spawn, the registry's two maps in step after every operation, and nothing registered
    or alive after the final stop. `VAF_FUZZ_DRAWS` raises the draw count outside the suite."""
    for name in FUZZ_ACCOUNTS:
        world.link(name)
        if name != "admin":
            world.own_switch(SCOPES[name], True)
    world.link("carol")
    draws = int(os.environ.get("VAF_FUZZ_DRAWS", "25"))
    seed = int(os.environ.get("VAF_FUZZ_SEED", "20261120"))
    master = random.Random(seed)
    failures = []
    for draw in range(draws):
        _fuzz_reset(world)
        found = _fuzz_schedule(world, random.Random(master.random()))
        if found:
            failures.append((draw, found))
    assert failures == [], f"seed {seed}: {failures[:5]}"
