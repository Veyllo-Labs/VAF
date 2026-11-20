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
import subprocess
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


class StubbornProc(FakeProc):
    """A process that ignores SIGTERM, and with `killable=False` survives the kill too: its
    wait times out for as long as it runs."""

    killable = True

    def terminate(self):
        pass

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("node", timeout)
        return self.returncode

    def kill(self):
        if self.killable:
            self.returncode = -9


class Unkillable(StubbornProc):
    killable = False


class SlowToDie(StubbornProc):
    """Ignores SIGTERM, so `_terminate` waits its full TERMINATE_WAIT before the kill ends it."""

    def wait(self, timeout=None):
        if self.returncode is None:
            time.sleep(timeout or 0)
            raise subprocess.TimeoutExpired("node", timeout)
        return self.returncode


def _run_threads(*targets):
    """Start one thread per target at once, join them, and return the exceptions they raised:
    a thread's exception never reaches `join`, so without this a raising operation passes."""
    errors = []

    def guarded(fn):
        try:
            fn()
        except BaseException as e:  # noqa: BLE001 - every failure is the test's business
            errors.append(e)

    threads = [threading.Thread(target=guarded, args=(fn,)) for fn in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    errors.extend(RuntimeError("thread did not finish") for t in threads if t.is_alive())
    return errors


class World:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.spawned = []          # every FakeProc ever spawned, in order
        self.spawn_delay = 0.0
        self.on_spawn = None       # optional hook(name) run inside the fake spawn
        self.fail_spawn = set()    # names whose spawn fails (Node could not be started)
        self.proc_class = {}       # name -> FakeProc subclass
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
    monkeypatch.setattr(wa, "_pending_starts", {})
    monkeypatch.setattr(wa, "_survivors", set())
    monkeypatch.setattr(wa, "_terminating", 0)
    monkeypatch.setattr(wa, "COLD_START_TIMEOUT", 3.0)

    def spawn(username, auth_dir):
        if w.on_spawn:
            w.on_spawn(username)
        if w.spawn_delay:
            time.sleep(w.spawn_delay)
        if username in w.fail_spawn:
            return None
        proc = w.proc_class.get(username, FakeProc)(username)
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


WHOLE = ("start_bridge", "stop_bridge", "restart_bridge", "restart_bridge_result", "start_missing_accounts")
ACCOUNT = ("start_account", "stop_account", "restart_account")


def _quiet_reads(monkeypatch):
    """The dashboard's read lanes answer at once instead of waiting on a fake process."""
    monkeypatch.setattr(wa, "get_connection_status", lambda username, wait_timeout=2.0: False)
    monkeypatch.setattr(wa, "get_whatsapp_chats", lambda username, wait_timeout=2.0: [])
    monkeypatch.setattr(wa, "get_contact_names", lambda username, wait_timeout=2.0: {})
    monkeypatch.setattr(wa, "get_lid_mappings", lambda username, wait_timeout=2.0: [])


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
    """MUTATION: the route's non-admin branch calls `stop_bridge()`; `stop_account` sets
    `_bridge_stop`; or `stop_account` leaves the account's connection state as connected."""
    procs = _bridge_with(world, "admin", "alice")
    wa._connection_status.update({"admin": True, "alice": True})
    _record(world, monkeypatch, *WHOLE)
    res = _client(ALICE).post("/api/whatsapp/stop")
    assert res.status_code == 200, res.text
    assert world.calls == []
    assert wa._connection_status == {"admin": True, "alice": False}
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
    assert world.calls == [("restart_account", "alice", ALICE_SCOPE), ("stop_account", "alice", ALICE_SCOPE),
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
    assert world.calls[0] == ("restart_bridge_result",) and ("stop_bridge",) in world.calls
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

    assert _run_threads(go, go) == []
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
        assert wa.start_bridge()

    assert _run_threads(go, go) == []
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
    errors = []

    def start():
        try:
            wa.start_account("alice", ALICE_SCOPE)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
    starter = threading.Thread(target=start)
    starter.start()
    assert spawning.wait(timeout=3)
    wa.stop_bridge()
    starter.join(timeout=5)
    assert errors == [] and not starter.is_alive()
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
    _quiet_reads(monkeypatch)
    for user, expected in ((ALICE, False), (ADMIN, True)):
        client = _client(user)
        status = client.get("/api/whatsapp/status")
        assert status.status_code == 200, status.text
        assert status.json()["running"] is expected, (user["username"], status.json())
        dash = client.get("/api/whatsapp/dashboard")
        assert dash.status_code == 200, dash.text
        assert dash.json()["running"] is expected, (user["username"], "dashboard")


# -- Admin refusals, cold starts, stops that do not end, name collisions -----------------------

def test_an_admin_start_is_refused_while_whatsapp_is_switched_off(world, monkeypatch):
    """The admin branch keeps the master switch gate. MUTATION: drop the global-switch check
    from the admin branch of `/start` (or of `/restart`)."""
    world.link("admin")
    world.global_switch(False)
    _record(world, monkeypatch, *WHOLE, *ACCOUNT, through=False)
    for path in ("/api/whatsapp/start", "/api/whatsapp/restart"):
        res = _client(ADMIN).post(path)
        assert res.status_code == 400, (path, res.text)
        assert res.json()["detail"] == "WhatsApp not enabled. Enable in Settings -> Connections."
    assert world.calls == [] and world.names() == []


def test_two_accounts_starting_a_stopped_bridge_both_run(world, monkeypatch):
    """Two different accounts start at once while the bridge is down: the first brings it up
    with its own process, the second must then start into it rather than be told the bridge
    runs. MUTATION S: drop `_lifecycle_lock` from `start_account`'s "bridge is down" branch;
    both then call `start_bridge(only=...)`, the second finds the thread alive, and only one
    process comes up."""
    for name in ("alice", "bob"):
        world.link(name)
        world.own_switch(SCOPES[name], True)

    def slow_deps():
        time.sleep(0.1)
        return True, ""
    monkeypatch.setattr(wa, "ensure_bridge_deps", slow_deps)
    barrier = threading.Barrier(2)
    results = {}

    def start(name):
        def run():
            barrier.wait()
            results[name] = wa.start_account(name, SCOPES[name])
        return run

    assert _run_threads(start("alice"), start("bob")) == []
    assert results["alice"][0] and results["bob"][0], results
    assert world.live("alice") and world.live("bob"), world.names()
    assert sorted(world.names()) == ["alice", "bob"]


def test_an_account_start_during_a_whole_restart_waits_for_it(world, monkeypatch):
    """An admin's restart holds the lifecycle from its stop to its start, so an account start
    that arrives between the two cannot bring the bridge up with that account alone.
    MUTATION T: drop `_lifecycle_lock` from `restart_bridge`; the restart then ends with
    Alice's process only and the admin's never comes back."""
    procs = _bridge_with(world, "admin", "alice")
    stopped, go = threading.Event(), threading.Event()
    real_stop = wa.stop_bridge

    def stop_then_pause():
        old = wa._bridge_thread
        real_stop()
        old.join(timeout=5)
        stopped.set()
        go.wait(timeout=5)
    monkeypatch.setattr(wa, "stop_bridge", stop_then_pause)
    results = {}

    def restart():
        results["restart"] = wa.restart_bridge()

    def alice_starts():
        assert stopped.wait(timeout=5)
        results["alice"] = wa.start_account("alice", ALICE_SCOPE)

    def release():
        assert stopped.wait(timeout=5)
        time.sleep(0.3)
        go.set()

    assert _run_threads(restart, alice_starts, release) == []
    assert results["restart"] is True and results["alice"][0], results
    assert world.wait_for(lambda: world.live("admin") and world.live("alice")), world.names()
    assert procs["admin"].returncode is not None and wa._processes["admin"] is not procs["admin"]


def test_a_cold_start_whose_spawn_fails_says_so(world, monkeypatch):
    """An account start that brings the bridge up waits for its own spawn: when Node cannot be
    started for it, the answer is a failure (500, worded for somebody who cannot fix the
    installation), not "started". The bridge stays up for anybody who starts into it.
    MUTATION: `start_account` answers ACCOUNT_STARTED right after `start_bridge(only=...)`."""
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    world.fail_spawn = {"alice"}
    assert wa.start_account("alice", ALICE_SCOPE) == (False, wa.START_FAILED)
    assert not world.live("alice") and not wa._bridge_stop.is_set() and wa.is_bridge_running()
    _stop_and_join()
    res = _client(ALICE).post("/api/whatsapp/start")
    assert res.status_code == 500, res.text
    assert res.json()["detail"] == "Failed to start WhatsApp. Ask an admin to check the WhatsApp installation."
    # Another account starts into the bridge Alice's failed start brought up.
    world.link("bob")
    world.own_switch(BOB_SCOPE, True)
    world.fail_spawn = set()
    assert wa.start_account("bob", BOB_SCOPE) == (True, wa.ACCOUNT_STARTED)
    assert world.live("bob")


def test_a_stop_cancels_a_pending_cold_start(world, monkeypatch):
    """A Stop that arrives while the bridge thread has not yet spawned the account's process
    cancels that spawn, even though no process is registered yet; a later Start still works.
    MUTATION: `stop_account` leaves the pending start alone."""
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    gate = threading.Event()
    monkeypatch.setattr("vaf.core.channel_pairing.refile_whatsapp_owner_numbers",
                        lambda: gate.wait(timeout=5) and [])
    results = {}

    def start():
        results["start"] = wa.start_account("alice", ALICE_SCOPE)

    def stop_then_release():
        assert world.wait_for(lambda: "alice" in wa._pending_starts and wa.is_bridge_running())
        results["stop"] = wa.stop_account("alice", ALICE_SCOPE)
        gate.set()

    assert _run_threads(start, stop_then_release) == []
    assert results["stop"] is True
    assert results["start"] == (False, wa.START_CANCELLED), results
    time.sleep(0.1)
    assert world.names() == [] and not world.live("alice")
    assert wa.start_account("alice", ALICE_SCOPE) == (True, wa.ACCOUNT_STARTED)
    assert world.live("alice") and world.names() == ["alice"]


@pytest.mark.parametrize("killable", [False, True])
def test_a_process_that_does_not_end_is_not_reported_stopped(world, killable):
    """Terminate, then kill, and wait for the process either way. One that survives both stays
    registered, the stop and the restart answer 500, and a restart starts no second process on
    its login. MUTATION: drop the `wait` after `kill()` in `_terminate`; or map STOP_FAILED to
    400 in the routes."""
    StubbornProc.killable = killable
    world.proc_class["alice"] = StubbornProc
    procs = _bridge_with(world, "admin", "alice")
    try:
        if killable:
            assert wa.stop_account("alice", ALICE_SCOPE) is True
            assert "alice" not in wa._processes and procs["alice"].returncode == -9
            return
        assert wa.stop_account("alice", ALICE_SCOPE) is False
        assert wa._processes["alice"] is procs["alice"] and wa._scope_by_user["alice"] == ALICE_SCOPE
        res = _client(ALICE).post("/api/whatsapp/stop")
        assert res.status_code == 500 and res.json()["detail"] == wa.STOP_FAILED, res.text
        res = _client(ALICE).post("/api/whatsapp/restart")
        assert res.status_code == 500 and res.json()["detail"] == wa.STOP_FAILED, res.text
        assert wa.restart_account("alice", ALICE_SCOPE) == (False, wa.STOP_FAILED)
        assert world.names().count("alice") == 1
    finally:
        StubbornProc.killable = True
        procs["alice"].returncode = -9      # let the teardown's shutdown end it


def test_an_account_named_like_the_local_admin_reaches_only_its_own_process(world, monkeypatch):
    """An ordinary account whose name equals the local admin's, with a scope of its own: its
    stop and restart leave the admin's process alone and its status is not the admin's.
    MUTATION: `stop_account` ignores the scope; `/status` or `/dashboard` asks without it."""
    procs = _bridge_with(world, "admin")
    _quiet_reads(monkeypatch)
    # The namesake's own switch is on, so `/status` reports its `running` unmasked.
    world.own_switch(BOB_SCOPE, True)
    namesake = {"username": "admin", "role": "user", "user_scope_id": BOB_SCOPE}
    client = _client(namesake)
    assert client.post("/api/whatsapp/stop").status_code == 200
    assert world.live("admin") and procs["admin"].returncode is None
    res = client.post("/api/whatsapp/restart")
    assert res.status_code == 400 and res.json()["detail"] == wa.NOT_STARTABLE, res.text
    assert wa._processes["admin"] is procs["admin"] and procs["admin"].returncode is None
    assert wa.stop_account("admin", BOB_SCOPE) is None and world.live("admin")
    for path in ("/api/whatsapp/status", "/api/whatsapp/dashboard"):
        res = client.get(path)
        assert res.status_code == 200 and res.json()["running"] is False, (path, res.text)


def test_a_restart_stops_nothing_for_an_account_that_cannot_start_again(world):
    """An account whose own switch went off while its process runs: its Restart answers 400 and
    leaves that process as it is. MUTATION: drop the `_runnable_account` check that
    `restart_account` makes before it stops."""
    procs = _bridge_with(world, "admin", "alice")
    world.own_switch(ALICE_SCOPE, False)
    res = _client(ALICE).post("/api/whatsapp/restart")
    assert res.status_code == 400 and res.json()["detail"] == wa.NOT_STARTABLE, res.text
    assert wa._processes["alice"] is procs["alice"] and procs["alice"].returncode is None


@pytest.mark.parametrize("how", ["restart", "stop, then start"])
def test_a_survivor_of_the_bridge_shutdown_blocks_a_second_node(world, how):
    """A process that outlives terminate and kill at the bridge's own shutdown stays
    registered, so the next bridge start puts no second Node on its login.
    MUTATION: the shutdown drops survivors from the registry (it used to clear both maps
    before terminating)."""
    world.proc_class["alice"] = Unkillable
    procs = _bridge_with(world, "admin", "alice")
    try:
        if how == "restart":
            assert wa.restart_bridge() is True
        else:
            _stop_and_join()
            assert wa.start_bridge() is True
        assert world.wait_for(lambda: world.names().count("admin") == 2 and world.live("admin")), world.names()
        time.sleep(0.1)
        assert world.names().count("alice") == 1, world.names()
        assert wa._processes["alice"] is procs["alice"] and wa._scope_by_user["alice"] == ALICE_SCOPE
        assert procs["admin"].returncode is not None
    finally:
        procs["alice"].returncode = -9      # it ends at last, for the teardown


def test_a_restart_waits_for_a_slow_shutdown(world, monkeypatch):
    """Every process that needs its kill adds both waits of `_terminate` to the shutdown; the
    restart waits for all of it and then starts every account again. MUTATION: join the old
    bridge for a fixed time shorter than that (the old fixed 12 s against two processes that
    each take 5 s plus the kill); the start then sees the old thread alive, answers "already
    running" and starts nothing."""
    monkeypatch.setattr(wa, "TERMINATE_WAIT", 0.3)
    world.proc_class.update(admin=SlowToDie, alice=SlowToDie)
    procs = _bridge_with(world, "admin", "alice")
    old = wa._bridge_thread
    assert wa.restart_bridge() is True
    assert not old.is_alive() and wa._bridge_thread is not old
    assert all(p.returncode == -9 for p in procs.values())
    assert world.wait_for(lambda: world.live("admin") and world.live("alice")), world.names()
    assert sorted(world.names()) == ["admin", "admin", "alice", "alice"]


def test_a_restart_whose_shutdown_overruns_reports_failure(world, monkeypatch):
    """A shutdown that does not end within its deadline makes the restart a failure (the
    admin's `/restart` answers 500 and says the old process did not stop, not to check the
    installation), never a success with nothing started.
    MUTATION: drop the check that the old bridge thread has ended before the new start; or
    answer the overrun with the installation text."""
    monkeypatch.setattr(wa, "TERMINATE_WAIT", 0.3)
    monkeypatch.setattr(wa, "_shutdown_deadline", lambda: 0.1)
    world.proc_class.update(admin=SlowToDie, alice=SlowToDie)
    _bridge_with(world, "admin", "alice")
    old = wa._bridge_thread
    res = _client(ADMIN).post("/api/whatsapp/restart")
    assert res.status_code == 500, res.text
    assert res.json()["detail"] == wa.SHUTDOWN_OVERRAN == "The old WhatsApp process did not stop in time; try again."
    old.join(timeout=5)
    time.sleep(0.1)
    assert sorted(world.names()) == ["admin", "alice"] and not wa.is_bridge_running()


def test_a_start_the_bridge_finds_nobody_for_is_answered_at_once(world, monkeypatch):
    """The account was switched off between its own start's check and the bridge thread's
    read: the bridge exits with nobody to run and answers the waiting start NOT_STARTABLE at
    once, not after COLD_START_TIMEOUT. MUTATION: drop `_resolve_pending(NOT_STARTABLE)` from
    the bridge's exit with nobody to run."""
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    monkeypatch.setattr(wa, "COLD_START_TIMEOUT", 5.0)
    real_users = wa._get_users_to_run
    monkeypatch.setattr(wa, "_get_users_to_run",
                        lambda: [] if threading.current_thread() is wa._bridge_thread else real_users())
    began = time.monotonic()
    assert wa.start_account("alice", ALICE_SCOPE) == (False, wa.NOT_STARTABLE)
    assert time.monotonic() - began < 2.0
    assert world.names() == [] and wa._pending_starts == {}


def test_a_stop_under_another_scope_leaves_a_pending_start_alone(world):
    """A namesake's stop (the same name, another scope) does not cancel the start the account
    of that name is waiting on. MUTATION: drop the scope half of the pending-start check in
    `stop_account`."""
    pending = wa._PendingStart(ADMIN_SCOPE)
    with wa._spawn_lock:
        wa._pending_starts["admin"] = pending
    assert wa.stop_account("admin", BOB_SCOPE) is None
    assert not pending.cancelled and not pending.done.is_set()
    assert wa.stop_account("admin", ADMIN_SCOPE) is True and pending.cancelled


def _survivor_after_restart(world):
    """A running bridge for the admin and Alice whose process outlived the restart's kill."""
    world.proc_class["alice"] = Unkillable
    procs = _bridge_with(world, "admin", "alice")
    assert wa.restart_bridge() is True
    assert world.wait_for(lambda: world.names().count("admin") == 2 and world.live("admin"))
    return procs["alice"]


def test_a_kept_survivor_is_never_answered_as_running(world, monkeypatch):
    """A process the shutdown could not end has no reader any more: Alice's start and restart
    fail instead of answering "already running", her status is not running, and no lane acts
    through it. MUTATION: `_spawn_account` and the start into a running bridge treat a live
    survivor as the account's process; or `_process_for` answers a survivor."""
    survivor = _survivor_after_restart(world)
    try:
        _quiet_reads(monkeypatch)
        assert wa.start_account("alice", ALICE_SCOPE) == (False, wa.PREVIOUS_NOT_ENDED)
        res = _client(ALICE).post("/api/whatsapp/start")
        assert res.status_code == 500 and res.json()["detail"] == wa.PREVIOUS_NOT_ENDED, res.text
        res = _client(ALICE).post("/api/whatsapp/restart")
        assert res.status_code == 500, res.text
        assert wa.has_process_for_user("alice", ALICE_SCOPE) is False
        assert wa._process_for("alice", ALICE_SCOPE) == (None, None)
        assert _client(ALICE).get("/api/whatsapp/status").json()["running"] is False
        assert world.names().count("alice") == 1 and wa._processes["alice"] is survivor
    finally:
        survivor.returncode = -9


def test_a_new_account_reusing_the_name_of_a_survivor_does_not_start(world):
    """An account that reuses a deleted account's name gets no "already running" from that
    account's process, survivor or not. MUTATION: drop the scope comparison from
    `_blocked_by_previous`."""
    survivor = _survivor_after_restart(world)
    try:
        world.own_switch(CAROL_SCOPE, True)
        SCOPES["alice"] = CAROL_SCOPE           # the name now belongs to another account
        assert wa.start_account("alice", CAROL_SCOPE) == (False, wa.PREVIOUS_NOT_ENDED)
        survivor.returncode = -9
        world.proc_class.clear()
        # The same for a process that still has its reader.
        assert wa.start_account("alice", CAROL_SCOPE) == (True, wa.ACCOUNT_STARTED)
        SCOPES["alice"] = ALICE_SCOPE
        world.own_switch(ALICE_SCOPE, True)
        assert wa.start_account("alice", ALICE_SCOPE) == (False, wa.PREVIOUS_NOT_ENDED)
    finally:
        SCOPES["alice"] = ALICE_SCOPE
        survivor.returncode = -9


def test_after_the_survivor_ends_a_start_succeeds(world):
    """A survivor that has ended blocks nothing: the next start spawns a fresh process and
    the survivor's mark goes with it. MUTATION: `_blocked_by_previous` ignores whether the
    registered process is still alive."""
    survivor = _survivor_after_restart(world)
    survivor.returncode = -9
    assert wa.start_account("alice", ALICE_SCOPE) == (True, wa.ACCOUNT_STARTED)
    assert world.live("alice") and wa._processes["alice"] is not survivor
    assert wa._survivors == set()


def test_a_send_while_the_bridge_is_down_answers_at_once(world, monkeypatch):
    """A process registered while no bridge runs is read by nobody: a send takes the file
    path, which answers at once that the bridge is not running, instead of waiting its whole
    timeout and answering "maybe delivered". MUTATION: drop `not is_bridge_running()` from the
    send's in-process condition."""
    monkeypatch.setattr(wa, "_is_reply_allowed", lambda *a, **k: True)
    with wa._process_lock:
        wa._processes["alice"] = FakeProc("alice")
        wa._scope_by_user["alice"] = ALICE_SCOPE
    assert not wa.is_bridge_running()
    began = time.monotonic()
    out = wa.send_whatsapp_with_confirmation("alice", "491700000042@s.whatsapp.net", "hi",
                                             timeout=3.0, user_scope_id=ALICE_SCOPE)
    assert time.monotonic() - began < 1.0
    assert out.startswith("WhatsApp bridge is not running."), out


def test_a_crashed_bridge_thread_leaves_no_pending_start_behind(world, monkeypatch):
    """When the bridge thread fails before it spawns, the start waiting on it is answered at
    once and cleared, so the next whole start brings that account up. MUTATION: drop the
    `finally` that answers and clears every pending start in `_run_bridge`."""
    world.link("admin")
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    monkeypatch.setattr(wa, "COLD_START_TIMEOUT", 1.0)
    real_users = wa._get_users_to_run
    crashed = []

    def users():
        if threading.current_thread() is wa._bridge_thread and not crashed:
            crashed.append(1)
            raise RuntimeError("account directory unavailable")
        return real_users()
    monkeypatch.setattr(wa, "_get_users_to_run", users)
    began = time.monotonic()
    assert wa.start_account("alice", ALICE_SCOPE) == (False, wa.START_FAILED)
    assert time.monotonic() - began < 0.9
    wa._bridge_thread.join(timeout=5)
    assert wa._pending_starts == {}
    assert wa.start_bridge() is True
    assert world.wait_for(lambda: world.live("admin") and world.live("alice")), world.names()


@pytest.mark.parametrize("where", ["registered", "being terminated"])
def test_the_shutdown_deadline_covers_every_process(world, monkeypatch, where):
    """Both waits of `_terminate` for every process still registered or still being terminated
    by a shutdown that already emptied the registry, one more for a spawn in flight, and a
    margin. MUTATION: drop the count term; drop the margin; ignore the terminations in
    progress."""
    monkeypatch.setattr(wa, "TERMINATE_WAIT", 1.0)
    n = 3
    if where == "registered":
        with wa._process_lock:
            for i in range(n):
                wa._processes[f"u{i}"] = FakeProc(f"u{i}")
    else:
        monkeypatch.setattr(wa, "_terminating", n)
    assert wa._shutdown_deadline() >= 2 * wa.TERMINATE_WAIT * (n + 1) + 1.0


def test_a_dead_survivor_does_not_keep_an_empty_bridge_up(world):
    """The bridge that finds nobody to run counts only live processes: a dead entry left in the
    registry does not keep it up. MUTATION: count every registered entry."""
    dead = FakeProc("alice")
    dead.returncode = -9
    with wa._process_lock:
        wa._processes["alice"] = dead
        wa._scope_by_user["alice"] = ALICE_SCOPE
    assert wa.start_bridge(only="carol")
    wa._bridge_thread.join(timeout=3)
    assert not wa.is_bridge_running()


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
            ok, msg = wa.start_account(name, SCOPES[name])
            # Fake spawns never fail and Node is always there: a start that reports a failure
            # (or ran into its timeout) lost its own spawn.
            if msg == wa.START_FAILED:
                violations.append(f"start of {name} failed")
        elif kind == "stop_account":
            if wa.stop_account(name, SCOPES[name]) is False:
                violations.append(f"stop of {name} did not end its process")
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
        def run():
            for kind, name in ops:
                op(kind, name)
        return run

    violations.extend(f"operation raised: {e!r}" for e in _run_threads(*(worker(ops) for ops in plan)))
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
    with wa._spawn_lock:
        wa._pending_starts.clear()
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
