# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The reconnect worker reconnects accounts, not the bridge (`whatsapp_bridge.Reconnector`).

THE DEFECT, measured on the code before this change with the real `_whatsapp_reconnect_worker`
driven by a fake clock: Alice alone ran and sent her own `POST /stop`; the bridge kept running
with no process, the worker counted that as "nothing connected" and restarted the whole bridge,
and Alice was live again on a new spawn. `restart_bridge()` answering False was called again
and again with nothing logged. With the global switch off the worker did nothing at all, and a
bridge whose thread had crashed stayed down until a Start or a VAF restart.

THE RULE: a pass restarts a registered, switched-on account whose own connection stays down
(`_reconnect_account`: exactly the process it judged, into the running bridge; bounded,
every refusal logged), leaves alone an account without a
registration (stopped by its owner, an admin or its expiry) and a survivor that still runs,
and brings a crashed bridge back at most CRASH_RESTARTS_MAX times in a row.

Fake processes and a fake clock: no Node, no network, no sleep in the worker.
"""
import ast
import logging
import threading
from pathlib import Path

import pytest

from vaf.api import whatsapp_bridge as wa

from test_whatsapp_account_process import (  # noqa: F401 - `world` is a fixture
    ADMIN, ALICE, ALICE_SCOPE, BOB_SCOPE, Unkillable, WHOLE, _bridge_with, _client, _record, world,
)

ROOT = Path(__file__).resolve().parents[1]


def _connected(monkeypatch, *names):
    """The connection check answers at once: connected exactly for `names`."""
    live = set(names)
    monkeypatch.setattr(wa, "get_connection_status", lambda username, wait_timeout=2.0, **k: username in live)
    return live


def _passes(reconnector, *times):
    actions = []
    for t in times:
        actions.extend(reconnector.step(t))
    return actions


# -- AC7 -----------------------------------------------------------------------------------------

def test_an_own_stop_is_not_undone(world, monkeypatch):
    """Alice alone runs and stops her own process: the bridge stays up with nobody, and no
    pass, however many, starts her again. MUTATION: treat "no registration" as a candidate."""
    _bridge_with(world, "alice")
    _connected(monkeypatch)
    res = _client(ALICE).post("/api/whatsapp/stop")
    assert res.status_code == 200, res.text
    assert wa.is_bridge_running() and "alice" not in wa._processes
    _record(world, monkeypatch, *WHOLE, "restart_account", "start_account", "_reconnect_account")
    reconnector = wa.Reconnector()
    assert _passes(reconnector, *(120.0 * i for i in range(1, 11))) == []
    assert world.calls == [] and world.names() == ["alice"]


def test_only_the_down_account_is_restarted(world, monkeypatch):
    """The admin's connection is up, Alice's is down: after the grace, Alice is restarted
    through `_reconnect_account` and the admin's process stays the same object. MUTATION: call
    `restart_bridge` instead."""
    procs = _bridge_with(world, "admin", "alice")
    _connected(monkeypatch, "admin")
    _record(world, monkeypatch, *WHOLE, "restart_account", "_reconnect_account")
    reconnector = wa.Reconnector()
    assert reconnector.step(0.0) == [], "down, but not for the grace yet"
    assert reconnector.step(60.0) == []
    assert reconnector.step(120.0) == [("alice", True, wa.ACCOUNT_STARTED)]
    assert [c[:3] for c in world.calls] == [("_reconnect_account", "alice", ALICE_SCOPE)]
    assert wa._processes["admin"] is procs["admin"] and procs["admin"].returncode is None
    assert procs["alice"].returncode is not None and world.live("alice")
    assert wa._processes["alice"] is not procs["alice"]


def test_a_refused_reconnect_is_logged_and_bounded(world, monkeypatch, caplog):
    """Every refusal is logged with its reason; after three tries without a connection, one
    ERROR line, and the account is left alone until it is seen connected. MUTATION: drop the
    tries bound; ignore the answer."""
    _bridge_with(world, "admin", "alice")
    live = _connected(monkeypatch, "admin")
    tries = []
    monkeypatch.setattr(wa, "_reconnect_account",
                        lambda name, scope, judged: tries.append(name) or (False, "no route to WhatsApp"))
    reconnector = wa.Reconnector()
    with caplog.at_level(logging.WARNING, logger="vaf.api.whatsapp_bridge"):
        actions = _passes(reconnector, *(120.0 * i for i in range(0, 15)))
    assert tries == ["alice"] * 3
    assert actions == [("alice", False, "no route to WhatsApp")] * 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "reconnecting alice failed" in r.getMessage()]
    assert len(warnings) == 3 and all("no route to WhatsApp" in r.getMessage() for r in warnings)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "did not reconnect after 3 restarts" in errors[0].getMessage()

    # Seen connected once, then down again: a fresh count.
    live.add("alice")
    reconnector.step(2000.0)
    live.discard("alice")
    _passes(reconnector, 2100.0, 2200.0)
    assert tries == ["alice"] * 4


def test_the_local_admins_switch_off_does_not_stop_reconnects(world, monkeypatch):
    """The local admin's own switch is off: Alice's reconnect still happens. MUTATION: put the
    global-switch check back in front of the pass."""
    procs = _bridge_with(world, "alice")
    world.global_switch(False)
    _connected(monkeypatch)
    reconnector = wa.Reconnector()
    assert _passes(reconnector, 0.0, 120.0) == [("alice", True, wa.ACCOUNT_STARTED)]
    assert procs["alice"].returncode is not None and world.live("alice")


def test_a_skipped_account_starts_once_its_survivor_ended(world, monkeypatch):
    """An admin's restart skipped Alice because her old process survived its kill: while it
    runs she is left alone; once it has ended, she is started. MUTATION: drop the
    dead-retired case (a retired registration is left alone whether or not it runs)."""
    world.proc_class["alice"] = Unkillable
    procs = _bridge_with(world, "admin", "alice")
    world.proc_class.clear()
    survivor = procs["alice"]
    try:
        ok, _, not_started = wa.restart_bridge_result()
        assert ok and not_started == ["alice"]
        assert wa._processes["alice"] is survivor and wa._is_retired(survivor)
        _connected(monkeypatch, "admin")
        reconnector = wa.Reconnector()
        assert _passes(reconnector, 0.0, 120.0, 240.0) == [], "a survivor that runs is left alone"
        survivor.returncode = -9
        assert _passes(reconnector, 360.0, 480.0) == [("alice", True, wa.ACCOUNT_STARTED)]
        assert world.live("alice") and wa._processes["alice"] is not survivor
        assert not wa._is_retired(wa._processes["alice"])
    finally:
        survivor.returncode = -9


# -- a Stop during the pass, a failed automatic restart ------------------------------------------

@pytest.mark.parametrize("who", ["own", "admin"])
def test_a_stop_during_the_pass_is_not_undone(world, monkeypatch, who):
    """The pass judged Alice down and waits for her connection check; meanwhile Alice stops her
    own process, or an admin stops the whole bridge. Nothing is started afterwards: not her
    process, not the bridge. MUTATION: restart through `restart_account` (whose cold start
    brought the account, and the bridge, up again)."""
    procs = _bridge_with(world, "admin", "alice")
    stopped = []

    def status(username, wait_timeout=2.0, **k):
        if username == "alice" and not stopped:
            stopped.append(who)
            if who == "own":
                assert _client(ALICE).post("/api/whatsapp/stop").status_code == 200
            else:
                assert _client(ADMIN).post("/api/whatsapp/stop").status_code == 200
                wa._bridge_thread.join(timeout=5)
            return False
        return username == "admin"
    monkeypatch.setattr(wa, "get_connection_status", status)
    actions = wa.Reconnector(grace=0.0, cooldown=0.0).step(0.0)
    assert stopped and not [a for a in actions if a[1]], actions
    assert world.names().count("alice") == 1 and procs["alice"].returncode is not None
    assert not world.live("alice")
    if who == "admin":
        assert not wa.is_bridge_running() and not world.live("admin")
    else:
        assert wa._processes["admin"] is procs["admin"] and world.live("admin")


def test_a_failed_automatic_restart_is_tried_again(world, monkeypatch):
    """The worker's restart stopped Alice's process and the next spawn failed: the following
    passes try again, up to the bound, although no registration is left. MUTATION: drop the
    restart intent (a missing registration ends the retries)."""
    _bridge_with(world, "admin", "alice")
    _connected(monkeypatch, "admin")
    attempts = []
    world.on_spawn = lambda name: attempts.append(name)
    world.fail_spawn.add("alice")
    reconnector = wa.Reconnector(grace=0.0, cooldown=0.0)
    for t in range(6):
        reconnector.step(float(t))
    assert attempts == ["alice"] * 3
    assert "alice" not in wa._processes

    # A spawn that works again on a later try brings her up.
    world.fail_spawn.clear()
    assert wa.start_account("alice", ALICE_SCOPE) == (True, wa.ACCOUNT_STARTED)
    reconnector = wa.Reconnector(grace=0.0, cooldown=0.0)
    world.fail_spawn.add("alice")
    reconnector.step(10.0)                 # her process is judged down and restarted: fails
    assert "alice" not in wa._processes
    world.fail_spawn.clear()
    assert reconnector.step(11.0) == [("alice", True, wa.ACCOUNT_STARTED)]
    assert world.live("alice")


def test_a_stop_ends_a_failed_automatic_restart(world, monkeypatch):
    """After the worker's restart failed to spawn, Alice stops her WhatsApp herself: no later
    pass tries again. MUTATION: keep the intent across a stop on request."""
    _bridge_with(world, "admin", "alice")
    _connected(monkeypatch, "admin")
    attempts = []
    world.on_spawn = lambda name: attempts.append(name)
    world.fail_spawn.add("alice")
    reconnector = wa.Reconnector(grace=0.0, cooldown=0.0)
    reconnector.step(0.0)
    assert attempts == ["alice"]
    assert _client(ALICE).post("/api/whatsapp/stop").status_code == 200
    world.fail_spawn.clear()
    for t in range(1, 6):
        assert reconnector.step(float(t)) == []
    assert attempts == ["alice"] and not world.live("alice")


# -- crash recovery: whom it brings back, and a Stop that cancels it -----------------------------

class _Crashable:
    """A run's stop as the bridge thread sees it, which raises in its wait once `crash` is set:
    a running bridge thread that fails after its spawn phase."""

    def __init__(self, real, crash):
        self.real, self.crash = real, crash

    def is_set(self):
        return self.real.is_set()

    def set(self):
        self.real.set()

    def wait(self, timeout=None):
        while not self.real.is_set():
            if self.crash.is_set():
                raise RuntimeError("bridge thread failed")
            self.real.wait(0.01)
        return True


def _crashing_runs(monkeypatch):
    crash = threading.Event()
    real = wa._run_bridge_until_stopped
    monkeypatch.setattr(wa, "_run_bridge_until_stopped",
                        lambda only, stop, outcome=None: real(only, _Crashable(stop, crash), outcome))
    return crash


def test_a_recovery_brings_back_only_the_failed_runs_members(world, monkeypatch):
    """Alice and Bob run; Bob stops his own WhatsApp; then the bridge thread fails. The recovery
    starts Alice and not Bob, who is still linked and switched on. MUTATION: recover with a
    whole `start_bridge()`."""
    crash = _crashing_runs(monkeypatch)
    _bridge_with(world, "alice", "bob")
    assert wa.stop_account("bob", BOB_SCOPE) is True
    crash.set()
    assert world.wait_for(lambda: not wa.is_bridge_running())
    assert wa.last_run_failed()
    crash.clear()
    world.spawned.clear()
    actions = wa.Reconnector().step(0.0)
    assert [(a[0], a[1]) for a in actions] == [("*", True)]
    assert world.wait_for(lambda: world.live("alice"))
    assert world.names() == ["alice"] and not world.live("bob")


@pytest.mark.parametrize("when", ["cooldown", "inside_the_start"])
def test_a_stop_after_a_crash_cancels_the_recovery(world, monkeypatch, when):
    """The bridge thread failed; an admin's Stop then lands in the recovery's cooldown, or
    inside the recovery's own start before it publishes the run. Nothing comes back.
    MUTATION: drop the whole-Stop check in `_bring_back` (cooldown); drop the check in
    `start_bridge`'s publishing (inside the start)."""
    crash = _crashing_runs(monkeypatch)
    _bridge_with(world, "alice")
    crash.set()
    assert world.wait_for(lambda: not wa.is_bridge_running())
    crash.clear()
    world.spawned.clear()
    if when == "cooldown":
        wa.stop_bridge()
    else:
        real_node = wa._node_path
        hit = []

        def node_then_stop():
            if not hit:
                hit.append(True)
                wa.stop_bridge()
            return real_node()
        monkeypatch.setattr(wa, "_node_path", node_then_stop)
    _record(world, monkeypatch, "start_bridge")
    reconnector = wa.Reconnector(cooldown=0.0)
    for t in range(4):
        reconnector.step(float(t))
    assert not wa.is_bridge_running()
    assert world.names() == []
    if when == "cooldown":
        assert world.calls == [], "a Stop since the failed run's start: no recovery is tried"


# -- AC8 -----------------------------------------------------------------------------------------

def test_a_crashed_bridge_is_brought_back_three_times(world, monkeypatch):
    """A bridge thread that raises ends its run as failed; with Alice runnable, the passes
    start the bridge again, at most three times in a row and a cooldown apart. MUTATION: drop
    `failed` from `_run_bridge`'s `except`; drop the bound."""
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)

    def crash(only, stop, outcome=None):
        raise RuntimeError("bridge thread failed")
    monkeypatch.setattr(wa, "_run_bridge_until_stopped", crash)
    _record(world, monkeypatch, "start_bridge")
    assert wa.start_bridge()
    assert world.wait_for(lambda: not wa.is_bridge_running())
    assert wa.last_run_failed()
    world.calls.clear()

    reconnector = wa.Reconnector()
    started = []
    for t in (120.0 * i for i in range(0, 12)):
        actions = reconnector.step(t)
        started.extend(actions)
        assert world.wait_for(lambda: not wa.is_bridge_running())
    assert [c[0] for c in world.calls] == ["start_bridge"] * 3
    assert [(a[0], a[1]) for a in started] == [("*", True)] * 3


@pytest.mark.parametrize("how", ["requested", "nobody"])
def test_a_stopped_bridge_stays_down(world, monkeypatch, how):
    """A stop on request, or a run that found nobody to run, is never brought back. The
    nobody case: a whole start for Alice whose bridge thread found her switched off; she is
    switched on again by a settings save afterwards, still the run's member and runnable, and
    nothing crashed, so nothing starts. MUTATION: bring back any bridge that is not running."""
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    if how == "requested":
        _bridge_with(world, "alice")
        wa.stop_bridge()
    else:
        real_users = wa._get_users_to_run

        def nobody_in_the_thread():
            return [] if threading.current_thread() is wa._bridge_thread else real_users()
        monkeypatch.setattr(wa, "_get_users_to_run", nobody_in_the_thread)
        assert wa.start_bridge()
        wa._bridge_thread.join(timeout=5)
        monkeypatch.setattr(wa, "_get_users_to_run", real_users)
        assert ("alice", ALICE_SCOPE.lower()) in wa._run_outcome.members
    wa._bridge_thread.join(timeout=5)
    assert not wa.is_bridge_running() and not wa.last_run_failed()
    _record(world, monkeypatch, *WHOLE, "start_account", "restart_account", "_reconnect_account")
    reconnector = wa.Reconnector()
    assert _passes(reconnector, *(120.0 * i for i in range(0, 10))) == []
    assert world.calls == []


# -- AC9 -----------------------------------------------------------------------------------------

def _function(tree, name):
    return next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def test_the_worker_always_starts_and_calls_the_reconnector():
    """The startup creates the worker whatever the config says, and the worker's body is one
    `Reconnector.step` per pass. MUTATION: put the `create_task` back under the `if`."""
    tree = ast.parse((ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8"))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    sites = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "create_task" and n.args and isinstance(n.args[0], ast.Call)
             and getattr(n.args[0].func, "id", None) == "_whatsapp_reconnect_worker"]
    assert len(sites) == 1
    node = sites[0]
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.If):
            assert "whatsapp_config" not in ast.unparse(node.test) and "linked" not in ast.unparse(node.test), \
                ast.unparse(node.test)

    worker = _function(tree, "_whatsapp_reconnect_worker")
    names = {n.id for n in ast.walk(worker) if isinstance(n, ast.Name)} | \
            {n.attr for n in ast.walk(worker) if isinstance(n, ast.Attribute)}
    assert "Reconnector" in names and "step" in names
    assert not names & {"restart_bridge", "any_process_connected", "whatsapp_config"}, names
    assert not hasattr(wa, "any_process_connected")
