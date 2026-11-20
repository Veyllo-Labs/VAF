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
import queue
import random
import subprocess
import threading
import time
from pathlib import Path

import pytest

from vaf.api import whatsapp_bridge as wa
from vaf.core.config import Config
from vaf.core.platform import Platform

# The real loops and state write, before any fixture replaces them, for the tests that need a
# run's own threads and the file processes without the bridge read.
REAL_SENDER = wa._sender_loop
REAL_CHAT_SYNC = wa._chat_sync_loop
REAL_STATE = wa._write_bridge_state

ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"
ALICE_SCOPE = "ab12cd34-0000-4000-8000-000000000001"
BOB_SCOPE = "ab12cd34-0000-4000-8000-000000000002"
CAROL_SCOPE = "ab12cd34-0000-4000-8000-000000000003"
SCOPES = {"admin": ADMIN_SCOPE, "alice": ALICE_SCOPE, "bob": BOB_SCOPE, "carol": CAROL_SCOPE}

ADMIN = {"username": "admin", "role": "admin", "user_scope_id": ADMIN_SCOPE}
ALICE = {"username": "alice", "role": "user", "user_scope_id": ALICE_SCOPE}


# Names of fake processes terminated without being retired first: every path that ends a
# process must retire it before the terminate, or a line its reader still holds is acted on.
UNRETIRED_TERMINATES = []


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
        if not wa._is_retired(self):
            UNRETIRED_TERMINATES.append(self.name)
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
        self.loops = []            # (kind, thread, stop) of every sender and chat-sync loop started
        self.states = []           # every state-file write, in order
        self.callbacks = []        # every reply callback set

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
    # The loops a run starts record their run's stop and wait for it, so a run that never
    # stopped them shows as a live thread.
    def loop(kind):
        def run(q=None, stop=None):
            w.loops.append((kind, threading.current_thread(), stop))
            if stop is not None:
                stop.wait()
        return run
    monkeypatch.setattr(wa, "_sender_loop", loop("sender"))
    monkeypatch.setattr(wa, "_chat_sync_loop", loop("chat_sync"))
    monkeypatch.setattr(wa, "_write_bridge_state", lambda running: w.states.append(bool(running)))
    monkeypatch.setattr(wa, "set_whatsapp_reply_callback", lambda cb: w.callbacks.append(cb))
    UNRETIRED_TERMINATES.clear()
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
    monkeypatch.setattr(wa, "get_connection_status", lambda username, wait_timeout=2.0, **k: False)
    monkeypatch.setattr(wa, "get_whatsapp_chats", lambda username, wait_timeout=2.0, **k: [])
    monkeypatch.setattr(wa, "get_contact_names", lambda username, wait_timeout=2.0, **k: {})
    monkeypatch.setattr(wa, "get_lid_mappings", lambda username, wait_timeout=2.0, **k: [])


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
    monkeypatch.setattr(wa, "_sender_loop", lambda *a, **k: sender_loops.append(1))
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
    # The new run has decided about Alice too: a survivor ended before that would be spawned
    # by the run itself rather than by the test's own start.
    assert wa._run_outcome.settled.wait(timeout=3)
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
    assert not wa._is_retired(wa._processes["alice"])


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


# -- A run ends cleanly, however it ends ----------------------------------------------------------

def _loops_of(world, stop):
    return [t for kind, t, s in world.loops if s is stop]


def _real_sender_and_state(world, monkeypatch, chat_sync=True):
    """The run's own sender loop and chat-sync loop, and the state file processes without the
    bridge read, instead of the fixture's stubs."""
    monkeypatch.setattr(wa, "_sender_loop", REAL_SENDER)
    monkeypatch.setattr(wa, "_write_bridge_state", REAL_STATE)
    if chat_sync:
        monkeypatch.setattr(wa, "_chat_sync_loop", REAL_CHAT_SYNC)
        data = world.cfg()
        data["whatsapp_config"]["chat_sync_interval_sec"] = 600
        world.write(data)


def _crash_in_bridge_thread(monkeypatch, where):
    """Make the bridge thread raise once: before it spawns anything, or after the admin's spawn
    (at Alice's)."""
    armed = [True]
    if where == "before_spawn":
        real = wa._get_users_to_run

        def users():
            if armed[0] and threading.current_thread() is wa._bridge_thread:
                armed[0] = False
                raise RuntimeError("account directory unavailable")
            return real()
        monkeypatch.setattr(wa, "_get_users_to_run", users)
    else:
        real = wa._blocked_by_previous

        def blocked(username, user_scope_id):
            if armed[0] and username == "alice" and threading.current_thread() is wa._bridge_thread:
                armed[0] = False
                raise RuntimeError("bridge thread failed after a spawn")
            return real(username, user_scope_id)
        monkeypatch.setattr(wa, "_blocked_by_previous", blocked)
    return armed


@pytest.mark.parametrize("where", ["before_spawn", "after_spawn"])
def test_a_crashed_bridge_thread_ends_its_run(world, monkeypatch, where):
    """A failure in the bridge thread ends the run like a stop: state file not running, the
    run's stop set and its loops ended, every process it spawned retired and terminated, the
    reply callback gone, a waiting start answered; and the next start and stop leave no loop of
    either run. MUTATION: drop `stop.set()` from `_end_run`; run the shutdown only when the
    stop was requested; drop the False write; reuse one cleared Event instead of a new one."""
    world.link("admin")
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    _real_sender_and_state(world, monkeypatch)
    _crash_in_bridge_thread(monkeypatch, where)
    waiting = wa._PendingStart(CAROL_SCOPE)
    with wa._spawn_lock:
        wa._pending_starts["carol"] = waiting
    assert wa.start_bridge()
    run_stop, sender, chat_sync = wa._bridge_stop, wa._sender_thread, wa._chat_sync_thread
    wa._bridge_thread.join(timeout=5)
    assert not wa.is_bridge_running()

    assert wa._read_bridge_state()["running"] is False
    assert run_stop.is_set()
    spawned = list(world.spawned)
    assert [p.name for p in spawned] == ([] if where == "before_spawn" else ["admin"])
    assert all(wa._is_retired(p) and p.returncode is not None for p in spawned)
    assert wa._processes == {} and wa._scope_by_user == {}
    assert world.callbacks[-1] is None
    assert waiting.done.is_set() and (waiting.ok, waiting.message) == (False, wa.START_FAILED)
    assert UNRETIRED_TERMINATES == []

    # The next run starts at once: the failed run's loops end on their own stop, not on
    # whatever the next run's stop says.
    assert wa.start_bridge()
    assert world.wait_for(lambda: world.live("admin") and world.live("alice")), world.names()
    sender.join(timeout=2.5)
    chat_sync.join(timeout=1)
    assert not sender.is_alive() and not chat_sync.is_alive()
    new_sender = wa._sender_thread
    assert new_sender is not sender and new_sender.is_alive()
    _stop_and_join()
    new_sender.join(timeout=2.5)
    assert not new_sender.is_alive()
    assert wa._read_bridge_state()["running"] is False


def test_a_bridge_with_nobody_to_run_ends_its_run(world, monkeypatch):
    """The exit with nobody to run ends the run's loops, and the state file says not running
    once the thread has ended, in 200 starts of 200. MUTATION: write True after the bridge
    thread has started (39 of 200 measured); return from the nobody branch past `_end_run`."""
    assert wa.start_bridge()
    run_stop = wa._bridge_stop
    wa._bridge_thread.join(timeout=5)
    # Its sender loop and its chat-sync loop (a 0 in the config still starts one).
    assert world.wait_for(lambda: len(_loops_of(world, run_stop)) == 2)
    loops = _loops_of(world, run_stop)
    for t in loops:
        t.join(timeout=1)
    assert run_stop.is_set() and not any(t.is_alive() for t in loops)

    monkeypatch.setattr(wa, "_write_bridge_state", REAL_STATE)
    stale = 0
    for _ in range(200):
        assert wa.start_bridge()
        wa._bridge_thread.join(timeout=5)
        stale += wa._read_bridge_state()["running"] is not False
    assert stale == 0, f"{stale} of 200 starts left the state file at running"
    assert world.wait_for(lambda: not any(t.is_alive() for _, t, _s in world.loops), timeout=5)


def test_a_restart_in_the_chat_sync_warm_up_leaves_one_loop(world, monkeypatch):
    """The chat-sync loop waits on its run's stop: a restart during its warm-up ends it at once
    instead of after the warm-up, next to the new run's loop. MUTATION: the warm-up back to
    `time.sleep` steps."""
    _real_sender_and_state(world, monkeypatch)
    world.link("admin")
    assert wa.start_bridge()
    old = wa._chat_sync_thread
    # Into its warm-up first: a restart before the loop has read its config can end it at its
    # first look at the stop, which proves nothing about the warm-up.
    time.sleep(0.3)
    assert old.is_alive()
    assert wa.restart_bridge() is True
    old.join(timeout=1)
    assert not old.is_alive()
    assert wa._chat_sync_thread is not old and wa._chat_sync_thread.is_alive()


class Buffered(FakeProc):
    """A process with one `qr` line still in its pipe, delivered once `release` is set."""

    def __init__(self, name):
        super().__init__(name)
        self.release = threading.Event()
        self.reader = None
        self.started = threading.Event()

        def lines():
            self.reader = threading.current_thread()
            self.started.set()
            self.release.wait(timeout=5)
            yield json.dumps({"type": "qr"}) + "\n"
        self.stdout = lines()


class BufferedUnkillable(Buffered):
    def terminate(self):
        if not wa._is_retired(self):
            UNRETIRED_TERMINATES.append(self.name)

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("node", timeout)
        return self.returncode

    def kill(self):
        pass


@pytest.mark.parametrize("how", ["account_restart", "survivor_replaced", "dead_replaced", "shutdown"])
def test_a_buffered_line_of_an_ended_process_is_not_acted_on(world, monkeypatch, how):
    """A line the old process's reader receives after the process was stopped, restarted,
    shut down or replaced is dropped, so its `qr` does not stop the account's NEW process
    (measured before: it did, for Alice's own restart). MUTATION: `stop_account` does not
    retire; `_spawn_account` does not retire the replaced entry; readers check only the stop."""
    world.proc_class["alice"] = BufferedUnkillable if how == "survivor_replaced" else Buffered
    procs = _bridge_with(world, "admin", "alice")
    old = procs["alice"]
    assert old.started.wait(timeout=3)
    world.proc_class.clear()
    real_dispatch = wa._dispatch_bridge_event
    acted = []

    def dispatch(username, user_scope_id, typ, obj, **kwargs):
        if threading.current_thread() is old.reader:
            acted.append(typ)
        return real_dispatch(username, user_scope_id, typ, obj, **kwargs)
    monkeypatch.setattr(wa, "_dispatch_bridge_event", dispatch)

    if how == "account_restart":
        assert wa.restart_account("alice", ALICE_SCOPE) == (True, wa.ACCOUNT_STARTED)
    elif how == "survivor_replaced":
        assert wa.restart_bridge() is True
        assert world.wait_for(lambda: world.names().count("admin") == 2 and world.live("admin"))
        # The new run has decided about Alice: ended before that, the run would spawn her itself.
        assert wa._run_outcome.settled.wait(timeout=3)
        assert wa._processes["alice"] is old
        old.returncode = -9                     # the survivor ends at last
        assert wa.start_account("alice", ALICE_SCOPE) == (True, wa.ACCOUNT_STARTED)
    elif how == "dead_replaced":
        old.returncode = 1                      # Node exited on its own, still registered
        assert wa.start_account("alice", ALICE_SCOPE) == (True, wa.ACCOUNT_STARTED)
    else:
        assert wa.restart_bridge() is True
        assert world.wait_for(lambda: world.names().count("alice") == 2 and world.live("alice"))
    new = wa._processes["alice"]
    assert new is not old and new.returncode is None

    old.release.set()
    old.reader.join(timeout=3)
    assert not old.reader.is_alive()
    assert acted == []
    assert wa._processes.get("alice") is new and new.returncode is None


def test_an_admin_restart_names_the_accounts_it_could_not_start(world):
    """A restart that skipped Alice because her previous process has not ended says so, in the
    function, the route and the window (outside the stopped banner); without one,
    `not_started` is empty and the message the old one. MUTATION: answer `(True, "", [])` with
    a survivor; the route drops `not_started`; remove the window's line; show it only with
    the stopped banner."""
    procs = _bridge_with(world, "admin")
    ok, message, not_started = wa.restart_bridge_result()
    assert (ok, message, not_started) == (True, "", [])
    res = _client(ADMIN).post("/api/whatsapp/restart")
    assert res.status_code == 200, res.text
    assert res.json() == {"status": "restarted", "not_started": [],
                          "message": "WhatsApp bridge restarted. Wait 20-30 s, then refresh."}
    assert procs["admin"].returncode is not None

    world.proc_class["alice"] = Unkillable
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    assert wa.start_account("alice", ALICE_SCOPE) == (True, wa.ACCOUNT_STARTED)
    survivor = wa._processes["alice"]
    try:
        ok, message, not_started = wa.restart_bridge_result()
        assert ok is True and not_started == ["alice"]
        assert message == ("WhatsApp restarted, but not for alice: the previous connection has not ended yet. "
                           "Press Restart WhatsApp again in a minute. If it still fails, restart VAF.")
        res = _client(ADMIN).post("/api/whatsapp/restart")
        assert res.status_code == 200, res.text
        assert res.json()["not_started"] == ["alice"] and res.json()["message"] == message
        assert wa._processes["alice"] is survivor and world.names().count("alice") == 1
    finally:
        survivor.returncode = -9

    src = (Path(__file__).resolve().parent.parent / "web" / "components" / "connections"
           / "WhatsAppDashboard.tsx").read_text(encoding="utf-8")
    handler = src[src.index("const handleRestartBridge"):src.index("const handleRelink")]
    assert "not_started" in handler
    assert "setRestartNotice(t('restartNotForAccounts', { names:" in handler
    assert "setRestartNotice(null)" in handler.split("try {", 1)[0], "cleared on the next restart"
    # Outside the stopped banner, which the refresh after a restart of the admin's own
    # connection hides.
    stopped = src[src.index("const stoppedBanner"):src.index("const banner = ")]
    assert "restartNotice" not in stopped
    assert "const banner = stoppedBanner || restartNotice ?" in src
    assert src.count("{restartNotice && ") == 2, "the banner line and the settings card"
    for path in sorted((Path(__file__).resolve().parent.parent / "web" / "messages").glob("*.json")):
        text = json.loads(path.read_text(encoding="utf-8"))["settings"]["whatsappDashboard"]["restartNotForAccounts"]
        assert "{names}" in text and "\u2014" not in text, path.name


def _cold_start_behind_a_slow_bridge(world, monkeypatch):
    """A cold start of Alice's whose bridge thread hangs in its first step past the start's
    timeout. Returns (release, answer): set `release` to let the bridge thread go on."""
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    monkeypatch.setattr(wa, "COLD_START_TIMEOUT", 1.0)
    release = threading.Event()
    monkeypatch.setattr("vaf.core.channel_pairing.refile_whatsapp_owner_numbers",
                        lambda: release.wait(timeout=5) and [])
    answer = {}

    def cold():
        res = _client(ALICE).post("/api/whatsapp/start")
        answer["status"], answer["body"] = res.status_code, res.json()
    thread = threading.Thread(target=cold)
    thread.start()
    assert world.wait_for(lambda: "alice" in wa._pending_starts and wa.is_bridge_running())
    return release, answer, thread


def test_a_timed_out_cold_start_whose_account_runs_answers_success(world, monkeypatch):
    """A start into the running bridge brought Alice up while her cold start still waited for
    the bridge thread: the cold start's timeout answers success, not 500. MUTATION: drop the
    live-process check on the timeout."""
    release, answer, thread = _cold_start_behind_a_slow_bridge(world, monkeypatch)
    try:
        assert wa.start_account("alice", ALICE_SCOPE) == (True, wa.ACCOUNT_STARTED)
        thread.join(timeout=5)
        assert answer["status"] == 200, answer
        assert answer["body"]["message"] == wa.ACCOUNT_ALREADY_RUNNING
        assert "alice" not in wa._pending_starts
    finally:
        release.set()
    time.sleep(0.1)
    assert world.live("alice") and world.names() == ["alice"]


@pytest.mark.parametrize("registered", ["nothing", "retired", "another scope"])
def test_a_timed_out_cold_start_without_a_process_still_fails(world, monkeypatch, registered):
    """Without a live, unretired process of Alice's own scope the timeout still answers
    START_FAILED and cancels the pending spawn. MUTATION: accept a retired or another scope's
    process on the timeout."""
    if registered != "nothing":
        other = FakeProc("alice")
        if registered == "retired":
            wa._retire(other)
        with wa._process_lock:
            wa._processes["alice"] = other
            wa._scope_by_user["alice"] = ALICE_SCOPE if registered == "retired" else BOB_SCOPE
    release, answer, thread = _cold_start_behind_a_slow_bridge(world, monkeypatch)
    pending = wa._pending_starts["alice"]
    try:
        thread.join(timeout=5)
        assert answer["status"] == 500, answer
        assert answer["body"]["detail"] == "Failed to start WhatsApp. Ask an admin to check the WhatsApp installation."
        assert pending.cancelled and (pending.ok, pending.message) == (False, wa.START_FAILED)
    finally:
        release.set()
    time.sleep(0.1)
    assert world.names() == []


def test_a_send_after_a_crash_answers_not_running_at_once(world, monkeypatch):
    """After the bridge thread failed, a send from a process without the bridge reads the state
    file and answers at once that the bridge is not running, instead of queueing a file nobody
    serves and waiting its whole timeout. MUTATION: the crash path without the False write."""
    world.link("admin")
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    monkeypatch.setattr(wa, "_write_bridge_state", REAL_STATE)
    monkeypatch.setattr(wa, "_is_reply_allowed", lambda *a, **k: True)
    _crash_in_bridge_thread(monkeypatch, "after_spawn")
    assert wa.start_bridge()
    wa._bridge_thread.join(timeout=5)
    began = time.monotonic()
    out = wa.send_whatsapp_with_confirmation("alice", "491700000042@s.whatsapp.net", "hi",
                                             timeout=3.0, user_scope_id=ALICE_SCOPE)
    assert time.monotonic() - began < 1.0
    assert out.startswith("WhatsApp bridge is not running."), out


def test_a_send_for_a_survivor_says_its_process_has_not_ended(world, monkeypatch):
    """Alice's process outlived the restart's kill: her send answers that the previous process
    has not ended, not that she has no WhatsApp to link; under another account's scope it
    says nothing about her process. MUTATION: the sender loop answers NO_PROCESS_FOR_ACCOUNT
    for a retired process; or ignores the scope."""
    _real_sender_and_state(world, monkeypatch, chat_sync=False)
    monkeypatch.setattr(wa, "_is_reply_allowed", lambda *a, **k: True)
    survivor = _survivor_after_restart(world)
    try:
        out = wa.send_whatsapp_with_confirmation("alice", "491700000042@s.whatsapp.net", "hi",
                                                 timeout=3.0, user_scope_id=ALICE_SCOPE)
        assert wa.PREVIOUS_NOT_ENDED in out and wa.NO_PROCESS_FOR_ACCOUNT not in out, out
        assert survivor.stdin.getvalue() == ""
        # Under another account's scope the survivor is not that account's: no word about it.
        out = wa.send_whatsapp_with_confirmation("alice", "491700000042@s.whatsapp.net", "hi",
                                                 timeout=3.0, user_scope_id=BOB_SCOPE)
        assert wa.NO_PROCESS_FOR_ACCOUNT in out and wa.PREVIOUS_NOT_ENDED not in out, out
    finally:
        survivor.returncode = -9


def test_the_survivor_set_is_gone():
    """A survivor is a registered, alive, retired process: no second bookkeeping, and the
    bridge thread's run has one end (`_end_run`), not a shutdown block of its own and no
    reused stop. (Structural: red while the old set or block exists.)"""
    import inspect
    assert not hasattr(wa, "_survivors") and not hasattr(wa, "_is_survivor")
    assert "_terminate(" not in inspect.getsource(wa._run_bridge_until_stopped)
    assert "_bridge_stop.clear()" not in inspect.getsource(wa)


# -- After the run's end nothing acts for it ----------------------------------------------------

JID = "491700000042@s.whatsapp.net"


def _send_results(monkeypatch):
    results = {}
    monkeypatch.setattr(wa, "_complete_send_request",
                        lambda req_id, ok, err=None, **k: results.__setitem__(req_id, (ok, err)))
    return results


def test_a_send_queued_ahead_of_the_shutdown_is_not_written(world, monkeypatch):
    """An item still in an ended run's queue (ahead of the shutdown's None) is not written to
    the process now registered for the account, which belongs to the next run; its sender
    learns it did not leave. MUTATION: drop the stop check after the process pick."""
    results = _send_results(monkeypatch)
    next_runs = FakeProc("alice")
    with wa._process_lock:
        wa._processes["alice"] = next_runs
        wa._scope_by_user["alice"] = ALICE_SCOPE
    q, stop = queue.Queue(), threading.Event()
    q.put(("alice", JID, "queued before the stop", None, "r1", None, None, None, ALICE_SCOPE))
    q.put(None)
    stop.set()
    REAL_SENDER(q=q, stop=stop)
    assert next_runs.stdin.getvalue() == ""
    assert results == {"r1": (False, wa.BRIDGE_STOPPING)}


def test_a_stop_during_the_process_pick_redirects_nothing(world, monkeypatch):
    """The run ends and the next one registers its process while the loop picks one: the loop
    writes to neither. MUTATION: drop the stop check after the process pick."""
    results = _send_results(monkeypatch)
    q, stop = queue.Queue(), threading.Event()
    replacement = FakeProc("alice")

    def pick_as_the_run_ends(username, user_scope_id=None):
        stop.set()
        return "alice", replacement
    monkeypatch.setattr(wa, "_process_for", pick_as_the_run_ends)
    q.put(("alice", JID, "hi", None, "r2", None, None, None, ALICE_SCOPE))
    q.put(None)
    REAL_SENDER(q=q, stop=stop)
    assert replacement.stdin.getvalue() == ""
    assert results == {"r2": (False, wa.BRIDGE_STOPPING)}


def test_an_expiry_line_of_a_replaced_process_stops_nothing(world, monkeypatch):
    """A `qr` line whose dispatch began before its process was replaced acts on nobody: neither
    the replacement is stopped nor the admin's switch turned off. The current process's own
    line still stops it. MUTATION: drop the registration check in the `qr` handler."""
    procs = _bridge_with(world, "admin", "alice")
    assert wa.restart_account("alice", ALICE_SCOPE) == (True, wa.ACCOUNT_STARTED)
    new_alice = wa._processes["alice"]
    wa._dispatch_bridge_event("alice", ALICE_SCOPE, "qr", {}, proc=procs["alice"])
    assert wa._processes.get("alice") is new_alice and new_alice.returncode is None

    assert wa.restart_account("admin", ADMIN_SCOPE) == (True, wa.ACCOUNT_STARTED)
    new_admin = wa._processes["admin"]
    wa._dispatch_bridge_event("admin", ADMIN_SCOPE, "qr", {}, proc=procs["admin"])
    assert wa._processes.get("admin") is new_admin and new_admin.returncode is None
    assert world.cfg()["whatsapp_config"]["enabled"] is True

    wa._dispatch_bridge_event("alice", ALICE_SCOPE, "qr", {}, proc=new_alice)
    assert "alice" not in wa._processes and new_alice.returncode is not None


def test_a_restart_reports_what_its_spawns_decided(world, monkeypatch):
    """Alice's survivor ends while the new run gets ready: the run starts her, and the restart
    does not name her as skipped. MUTATION: the names taken from a check before the start."""
    world.proc_class["alice"] = Unkillable
    procs = _bridge_with(world, "admin", "alice")
    survivor = procs["alice"]
    world.proc_class.clear()

    def survivor_ends():
        survivor.returncode = -9
        return []
    monkeypatch.setattr("vaf.core.channel_pairing.refile_whatsapp_owner_numbers", survivor_ends)
    try:
        assert wa.restart_bridge_result() == (True, "", [])
        assert world.live("alice") and wa._processes["alice"] is not survivor
    finally:
        survivor.returncode = -9


def test_a_spawn_that_raises_answers_its_waiting_start_at_once(world, monkeypatch):
    """The bridge thread fails at the account its waiting start is for, after it took that
    start out of the waiting list: the start is answered then, not after its timeout.
    MUTATION: drop the answer before the failure is raised."""
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    monkeypatch.setattr(wa, "COLD_START_TIMEOUT", 5.0)
    _crash_in_bridge_thread(monkeypatch, "after_spawn")
    began = time.monotonic()
    assert wa.start_account("alice", ALICE_SCOPE) == (False, wa.START_FAILED)
    assert time.monotonic() - began < 2.0
    wa._bridge_thread.join(timeout=5)
    assert wa._pending_starts == {} and world.names() == []


def test_a_bridge_thread_that_cannot_start_rolls_the_run_back(world, monkeypatch):
    """`Thread.start` fails for the bridge thread after the sender loop started and the state
    file said running: the run is ended on the spot. MUTATION: drop the rollback's stop, its
    False write, or its answer to waiting starts."""
    monkeypatch.setattr(wa, "_write_bridge_state", REAL_STATE)
    world.link("admin")
    waiting = wa._PendingStart(CAROL_SCOPE)
    with wa._spawn_lock:
        wa._pending_starts["carol"] = waiting
    real_start = threading.Thread.start

    def start(self):
        if getattr(self, "_target", None) is wa._run_bridge:
            raise RuntimeError("can't start new thread")
        return real_start(self)
    monkeypatch.setattr(threading.Thread, "start", start)
    assert wa.start_bridge() is False
    monkeypatch.setattr(threading.Thread, "start", real_start)
    assert wa._read_bridge_state()["running"] is False
    assert wa._bridge_stop.is_set() and not wa._spawns_open and wa._bridge_thread is None
    assert waiting.done.is_set() and (waiting.ok, waiting.message) == (False, wa.START_FAILED)
    sender = wa._sender_thread
    sender.join(timeout=2)
    assert not sender.is_alive()
    # The next start works.
    assert wa.start_bridge() is True
    assert world.wait_for(lambda: world.live("admin"))


class Slotted:
    """A process double that refuses new attributes, so it cannot carry the retirement mark."""

    __slots__ = ("name", "returncode", "stdout", "stderr", "stdin")

    def __init__(self, name):
        self.name, self.returncode = name, None
        self.stdout, self.stderr, self.stdin = io.StringIO(""), io.StringIO(""), io.StringIO()

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_one_process_that_refuses_the_mark_does_not_stop_the_end(world):
    """The end of a run terminates every process even when one cannot be marked. MUTATION: an
    unguarded `_retire`."""
    odd, admin = Slotted("bob"), FakeProc("admin")
    with wa._process_lock:
        wa._processes.update(bob=odd, admin=admin)
        wa._scope_by_user.update(bob=BOB_SCOPE, admin=ADMIN_SCOPE)
    wa._end_run(threading.Event(), requested=True)
    assert odd.returncode is not None and admin.returncode is not None
    assert wa._processes == {} and wa._terminating == 0
    assert world.states[-1] is False


def test_stopping_a_dead_survivor_after_the_run_says_not_running(world, monkeypatch):
    """After the run has ended, the survivor it left dies and its account's stop removes it:
    the state file still says not running, so sends from outside answer at once. MUTATION: the
    account stop writes running unconditionally."""
    monkeypatch.setattr(wa, "_write_bridge_state", REAL_STATE)
    world.proc_class["alice"] = Unkillable
    procs = _bridge_with(world, "admin", "alice")
    _stop_and_join()
    survivor = procs["alice"]
    assert wa._processes.get("alice") is survivor
    survivor.returncode = -9
    assert wa.stop_account("alice", ALICE_SCOPE) is True
    assert wa._read_bridge_state()["running"] is False


def test_a_survivor_alone_is_not_the_fallback_process(world, monkeypatch):
    """Alice's survivor is the only registered process: the one-process fallback does not hand
    it her send, which answers that the previous process has not ended. MUTATION: the
    fallback accepts a retired process."""
    _real_sender_and_state(world, monkeypatch, chat_sync=False)
    monkeypatch.setattr(wa, "_is_reply_allowed", lambda *a, **k: True)
    world.proc_class["alice"] = Unkillable
    procs = _bridge_with(world, "alice")
    survivor = procs["alice"]
    try:
        assert wa.restart_bridge() is True
        assert list(wa._processes) == ["alice"] and wa._processes["alice"] is survivor
        out = wa.send_whatsapp_with_confirmation("alice", JID, "hi", timeout=3.0, user_scope_id=ALICE_SCOPE)
        assert survivor.stdin.getvalue() == ""
        assert wa.PREVIOUS_NOT_ENDED in out, out
    finally:
        survivor.returncode = -9


def test_a_rolled_back_start_ends_a_process_a_start_into_it_registered(world, monkeypatch):
    """A start into the running bridge that was waiting for the spawn lock lands in the new run
    after its spawns opened and before its bridge thread failed to start: the rollback ends
    that process like every run's end, rather than leaving it registered without readers.
    MUTATION: the rollback only closes spawns and sets the stop."""
    world.link("admin")
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    entry = wa._runnable_account("alice", ALICE_SCOPE)
    real_start = threading.Thread.start
    landed = []

    def start(self):
        if getattr(self, "_target", None) is wa._run_bridge:
            landed.append(wa._start_into_running(entry))
            raise RuntimeError("can't start new thread")
        return real_start(self)
    monkeypatch.setattr(threading.Thread, "start", start)
    assert wa.start_bridge() is False
    monkeypatch.setattr(threading.Thread, "start", real_start)
    assert landed == [(True, wa.ACCOUNT_STARTED)]
    (alice,) = world.spawned
    assert wa._is_retired(alice) and alice.returncode is not None
    assert wa._processes == {} and world.states[-1] is False


def test_a_stop_resets_the_connection_before_a_replacement_can_report(world, monkeypatch):
    """The stop marks the account's connection down before it releases the spawn lock, so a
    replacement that starts and reports `connected` right after is not marked down by the
    stop that came first. MUTATION: reset the connection after the lock is released."""
    procs = _bridge_with(world, "admin", "alice")
    real_reset = wa._account_stopped
    replacement = {}

    def start_and_connect():
        replacement["start"] = wa.start_account("alice", ALICE_SCOPE)
        wa._dispatch_bridge_event("alice", ALICE_SCOPE, "connected", {"selfJid": "491700000001@s.whatsapp.net"})

    def reset_with_a_replacement_racing(username):
        racer = threading.Thread(target=start_and_connect)
        racer.start()
        racer.join(timeout=0.3)     # blocks on the spawn lock while the stop holds it
        replacement["racer"] = racer
        real_reset(username)
    monkeypatch.setattr(wa, "_account_stopped", reset_with_a_replacement_racing)
    assert wa.stop_account("alice", ALICE_SCOPE) is True
    replacement["racer"].join(timeout=5)
    assert replacement["start"] == (True, wa.ACCOUNT_STARTED)
    assert wa._processes["alice"] is not procs["alice"]
    assert wa._connection_status["alice"] is True


def test_an_expiry_stop_resets_the_connection_before_a_replacement_can_report(world, monkeypatch):
    """The twin of the stop's case, through the `qr` handler: it marks the connection down
    before it releases the spawn lock, so a replacement that starts and reports `connected`
    right after stays connected. MUTATION: in the `qr` handler only, reset the connection after
    the lock is released."""
    procs = _bridge_with(world, "admin", "alice")
    real_reset = wa._account_stopped
    replacement = {}

    def start_and_connect():
        replacement["start"] = wa.start_account("alice", ALICE_SCOPE)
        wa._dispatch_bridge_event("alice", ALICE_SCOPE, "connected", {"selfJid": "491700000001@s.whatsapp.net"})

    def reset_with_a_replacement_racing(username):
        racer = threading.Thread(target=start_and_connect)
        racer.start()
        racer.join(timeout=0.3)     # blocks on the spawn lock while the handler holds it
        replacement["racer"] = racer
        real_reset(username)
    monkeypatch.setattr(wa, "_account_stopped", reset_with_a_replacement_racing)
    wa._dispatch_bridge_event("alice", ALICE_SCOPE, "qr", {}, proc=procs["alice"])
    replacement["racer"].join(timeout=5)
    assert replacement["start"] == (True, wa.ACCOUNT_STARTED)
    assert wa._processes["alice"] is not procs["alice"]
    assert wa._connection_status["alice"] is True


def test_an_account_whose_previous_process_ends_at_the_spawn_is_not_named_skipped(world, monkeypatch):
    """Alice's survivor ends between the spawn gate's check and the spawn: she starts, and the
    restart does not name her. MUTATION: record the skip from the check alone."""
    world.proc_class["alice"] = Unkillable
    procs = _bridge_with(world, "admin", "alice")
    survivor = procs["alice"]
    world.proc_class.clear()
    real_blocked = wa._blocked_by_previous

    def blocked_then_ends(username, user_scope_id):
        answer = real_blocked(username, user_scope_id)
        if username == "alice" and threading.current_thread() is wa._bridge_thread:
            survivor.returncode = -9
        return answer
    monkeypatch.setattr(wa, "_blocked_by_previous", blocked_then_ends)
    try:
        assert wa.restart_bridge_result() == (True, "", [])
        assert world.live("alice") and wa._processes["alice"] is not survivor
    finally:
        survivor.returncode = -9


def test_an_expiry_line_holds_the_spawn_lock_from_its_check_to_its_stop(world, monkeypatch):
    """Alice's restart that arrives right after the expiry handler found her process still
    registered waits for the handler: it then starts her afresh, and the old line does not
    end the new process. MUTATION: the registration check before the lock."""
    procs = _bridge_with(world, "admin", "alice")
    real_check = wa._is_registration
    restart = {}

    def check_then_restart(username, proc):
        answer = real_check(username, proc)
        racer = threading.Thread(target=lambda: restart.__setitem__(
            "answer", wa.restart_account("alice", ALICE_SCOPE)))
        racer.start()
        racer.join(timeout=0.3)
        restart["racer"] = racer
        return answer
    monkeypatch.setattr(wa, "_is_registration", check_then_restart)
    wa._dispatch_bridge_event("alice", ALICE_SCOPE, "qr", {}, proc=procs["alice"])
    restart["racer"].join(timeout=5)
    assert restart["answer"] == (True, wa.ACCOUNT_STARTED)
    new = wa._processes.get("alice")
    assert new is not None and new is not procs["alice"] and new.returncode is None


def test_an_expiry_line_of_a_survivor_changes_nothing(world, monkeypatch):
    """The admin's process outlived the restart's kill: its own late `qr` is a retired
    process's line, so the admin's switch stays on. MUTATION: drop the retirement clause of
    the registration check."""
    world.proc_class["admin"] = Unkillable
    procs = _bridge_with(world, "admin")
    survivor = procs["admin"]
    try:
        assert wa.restart_bridge() is True
        assert wa._processes.get("admin") is survivor and wa._is_retired(survivor)
        wa._dispatch_bridge_event("admin", ADMIN_SCOPE, "qr", {}, proc=survivor)
        assert world.cfg()["whatsapp_config"]["enabled"] is True
    finally:
        survivor.returncode = -9


def test_the_reconnect_restart_does_not_wait_for_the_spawn_report(world, monkeypatch):
    """`restart_bridge` (the reconnect worker's) answers once the new run is up, without
    waiting for its spawn phase, which an admin's restart waits for to name skipped accounts.
    MUTATION: `restart_bridge` asks for the report."""
    _bridge_with(world, "admin")
    monkeypatch.setattr(wa, "RESTART_REPORT_WAIT", 2.0)
    release = threading.Event()
    monkeypatch.setattr("vaf.core.channel_pairing.refile_whatsapp_owner_numbers",
                        lambda: release.wait(timeout=5) and [])
    try:
        began = time.monotonic()
        assert wa.restart_bridge() is True
        assert time.monotonic() - began < 1.0
        assert not wa._run_outcome.settled.is_set()
    finally:
        release.set()
    assert wa._run_outcome.settled.wait(timeout=3)


# -- Fuzz: the spawn gate -------------------------------------------------------------------------

FUZZ_ACCOUNTS = ("admin", "alice", "bob")


def _fuzz_schedule(world, rng, crash):
    """One schedule: 8 operations over 4 threads, fake spawns sleeping 0 to 5 ms. In one of five
    schedules the bridge thread raises once, before or after its first spawn (`crash`, set by
    the test's wrappers). Returns the invariant violations it saw."""
    violations = []
    bridge_threads = []
    threads_lock = threading.Lock()
    crash["armed"] = rng.random() < 0.2
    crash["where"] = rng.choice(("before_spawn", "after_spawn"))
    crash["spawned_by"] = set()

    def on_spawn(name):
        with wa._process_lock:
            current = wa._processes.get(name)
        if current is not None and current.poll() is None:
            violations.append(f"second live process for {name}")
        crash["spawned_by"].add(threading.current_thread())
        time.sleep(rng_spawn.uniform(0, 0.005))
    rng_spawn = random.Random(rng.random())
    world.on_spawn = on_spawn

    def remember_bridge():
        with threads_lock:
            if wa._bridge_thread is not None and wa._bridge_thread not in bridge_threads:
                bridge_threads.append(wa._bridge_thread)

    def op(kind, name):
        if kind == "start_account":
            fired = crash["fired"]
            ok, msg = wa.start_account(name, SCOPES[name])
            # Fake spawns never fail and Node is always there: a start that reports a failure
            # (or ran into its timeout) lost its own spawn, unless an injected failure of a
            # bridge thread happened while it waited (the start a failed run answers).
            if msg == wa.START_FAILED and crash["fired"] == fired:
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
    # Every run ended: its loops have ended, its stop is set, and the state file's last word
    # is "not running"; every process was retired before its terminate.
    for kind, t, stop in world.loops:
        t.join(timeout=5)
        if t.is_alive():
            violations.append(f"{kind} loop of a run still alive")
        if stop is None or not stop.is_set():
            violations.append(f"{kind} loop's run never stopped")
    if world.states and world.states[-1] is not False:
        violations.append(f"last state write {world.states[-1]!r}")
    if UNRETIRED_TERMINATES:
        violations.append(f"terminated without retirement: {UNRETIRED_TERMINATES}")
    return violations


def _fuzz_reset(world):
    world.spawned.clear()
    world.loops.clear()
    world.states.clear()
    UNRETIRED_TERMINATES.clear()
    with wa._spawn_lock:
        wa._pending_starts.clear()
    with wa._process_lock:
        wa._processes.clear()
        wa._scope_by_user.clear()


def test_spawn_gate_fuzz(world, monkeypatch):
    """Seeded interleavings of account starts and stops, the admin's start into a running
    bridge, and the bridge's start and stop, with a failing bridge thread in one schedule of
    five. Oracle: no second live process for one account at any spawn, the registry's two maps
    in step after every operation, nothing registered or alive after the final stop, every
    run's loops ended and its stop set, the last state write False, and no process terminated
    before it was retired. A START_FAILED counts unless an injected failure of a bridge thread
    fired while that start waited (`crash["fired"]` before and after it).
    `VAF_FUZZ_DRAWS` raises the draw count outside the suite."""
    crash = {"armed": False, "fired": 0}
    real_users, real_blocked = wa._get_users_to_run, wa._blocked_by_previous

    def in_bridge_thread():
        return threading.current_thread() is wa._bridge_thread

    def users():
        if crash["armed"] and crash["where"] == "before_spawn" and in_bridge_thread():
            crash["armed"] = False
            crash["fired"] += 1
            raise RuntimeError("bridge thread failed before its first spawn")
        return real_users()

    def blocked(username, user_scope_id):
        if (crash["armed"] and crash["where"] == "after_spawn" and in_bridge_thread()
                and threading.current_thread() in crash["spawned_by"]):
            crash["armed"] = False
            crash["fired"] += 1
            raise RuntimeError("bridge thread failed after its first spawn")
        return real_blocked(username, user_scope_id)
    monkeypatch.setattr(wa, "_get_users_to_run", users)
    monkeypatch.setattr(wa, "_blocked_by_previous", blocked)
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
        found = _fuzz_schedule(world, random.Random(master.random()), crash)
        if found:
            failures.append((draw, found))
    print(f"spawn gate fuzz: {draws} draws, seed {seed}, {crash['fired']} bridge-thread failures injected")
    assert failures == [], f"seed {seed}: {failures[:5]}"
