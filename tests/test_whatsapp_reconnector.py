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
import os
import random
import sys
import threading
import time
from pathlib import Path

import pytest

from vaf.api import whatsapp_bridge as wa

from test_whatsapp_account_process import (  # noqa: F401 - `world` is a fixture
    ADMIN, ALICE, ALICE_SCOPE, BOB_SCOPE, CAROL_SCOPE, SCOPES, Unkillable, WHOLE, _bridge_with, _client, _quiet_reads,
    _record, world,
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

@pytest.mark.parametrize("who", ["own", "admin", "own_stop_and_start"])
def test_a_stop_during_the_pass_is_not_undone(world, monkeypatch, who):
    """The pass judged Alice down and waits for her connection check; meanwhile Alice stops her
    own process, or an admin stops the whole bridge, or Alice stops and starts her own again.
    Nothing is started afterwards (not her process, not the bridge), and a process she started
    herself meanwhile is not the one the pass judged, so it is left alone. MUTATION: restart
    through `restart_account` (whose cold start brought the account, and the bridge, up
    again); drop the judged-process check (own_stop_and_start)."""
    procs = _bridge_with(world, "admin", "alice")
    stopped, fresh = [], []

    def status(username, wait_timeout=2.0, **k):
        if username == "alice" and not stopped:
            stopped.append(who)
            if who == "own":
                assert _client(ALICE).post("/api/whatsapp/stop").status_code == 200
            elif who == "own_stop_and_start":
                assert _client(ALICE).post("/api/whatsapp/stop").status_code == 200
                assert _client(ALICE).post("/api/whatsapp/start").status_code == 200
                fresh.append(wa._processes["alice"])
            else:
                assert _client(ADMIN).post("/api/whatsapp/stop").status_code == 200
                wa._bridge_thread.join(timeout=5)
            return False
        return username == "admin"
    monkeypatch.setattr(wa, "get_connection_status", status)
    actions = wa.Reconnector(grace=0.0, cooldown=0.0).step(0.0)
    assert stopped and not [a for a in actions if a[1]], actions
    if who == "own_stop_and_start":
        assert world.names().count("alice") == 2 and procs["alice"].returncode is not None
        assert wa._processes["alice"] is fresh[0] and fresh[0].returncode is None
        return
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


def _admin_stop():
    """An admin's Stop as the product sends it: the route, not the bridge function."""
    res = _client(ADMIN).post("/api/whatsapp/stop")
    assert res.status_code == 200, res.text


@pytest.mark.parametrize("when", ["cooldown", "inside_the_start"])
def test_a_stop_after_a_crash_cancels_the_recovery(world, monkeypatch, caplog, when):
    """The bridge thread failed; an admin's Stop (`POST /api/whatsapp/stop`) then lands in the
    recovery's cooldown, or inside the recovery's own start before it publishes the run.
    Nothing comes back, and a start the Stop overtook spends no crash-restart count and logs no
    failure. MUTATION: the route stops only a running bridge (both); drop the whole-Stop check in
    `_bring_back` (cooldown); drop the check in the start's publishing (inside the start); count
    a cancelled start (inside the start)."""
    crash = _crashing_runs(monkeypatch)
    _bridge_with(world, "alice")
    crash.set()
    assert world.wait_for(lambda: not wa.is_bridge_running())
    crash.clear()
    world.spawned.clear()
    if when == "cooldown":
        _admin_stop()
    else:
        real_node = wa._node_path
        hit = []

        def node_then_stop():
            if not hit:
                hit.append(True)
                _admin_stop()
            return real_node()
        monkeypatch.setattr(wa, "_node_path", node_then_stop)
    _record(world, monkeypatch, "_start_bridge")
    reconnector = wa.Reconnector(cooldown=0.0)
    with caplog.at_level(logging.WARNING, logger="vaf.api.whatsapp_bridge"):
        for t in range(4):
            reconnector.step(float(t))
    assert not wa.is_bridge_running()
    assert world.names() == []
    assert reconnector._crash_restarts == 0, "a start a Stop overtook is no try"
    assert not [r for r in caplog.records if "did not start again" in r.getMessage()]
    if when == "cooldown":
        assert world.calls == [], "a Stop since the failed run's start: no recovery is tried"
    else:
        assert len(world.calls) == 1, world.calls


def _crash_and_wait(crash):
    crash.set()
    assert _wait(lambda: not wa.is_bridge_running())
    assert wa.last_run_failed()
    crash.clear()


def _wait(cond, timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def _own_stop(user):
    res = _client(user).post("/api/whatsapp/stop")
    assert res.status_code in (200, 500), res.text
    return res


@pytest.mark.parametrize("when", ["after_the_crash", "inside_the_start"])
def test_an_own_stop_before_the_recovery_publishes_is_kept(world, monkeypatch, when):
    """Alice stops her own WhatsApp after the crash, before the pass (the fuzz's smallest
    input: crash, alice_stop, reconnect), or inside the recovery's start, after the pass read
    the members and before the run is published. The recovery starts Bob, not her.
    MUTATION: publish for a copy of the members taken before the start (drop
    `intended &= recover.members`)."""
    crash = _crashing_runs(monkeypatch)
    _bridge_with(world, "alice", "bob")
    _crash_and_wait(crash)
    world.spawned.clear()
    if when == "after_the_crash":
        _own_stop(ALICE)
    else:
        real_node, hit = wa._node_path, []

        def node_then_stop():
            if not hit:
                hit.append(True)
                _own_stop(ALICE)
            return real_node()
        monkeypatch.setattr(wa, "_node_path", node_then_stop)
    assert [(a[0], a[1]) for a in wa.Reconnector().step(0.0)] == [("*", True)]
    assert world.wait_for(lambda: world.live("bob") and wa._run_outcome.settled.is_set())
    assert world.names() == ["bob"] and not world.live("alice")


@pytest.mark.parametrize("start", ["recovery", "admin_whole_start"])
def test_an_own_stop_after_the_publication_is_kept(world, monkeypatch, start):
    """Alice stops her own WhatsApp after a run was published and before its spawn phase
    reached her: the spawn phase does not start her (it reads the run's current members, it
    never adds the users it read). For a crash recovery and for an admin's whole Start alike.
    MUTATION: the spawn phase adds the users it read to the members."""
    hooked = []

    def stop_alice_once():
        if not hooked:
            hooked.append(True)
            _own_stop(ALICE)
        return []
    if start == "recovery":
        crash = _crashing_runs(monkeypatch)
        _bridge_with(world, "alice", "bob")
        _crash_and_wait(crash)
        world.spawned.clear()
        monkeypatch.setattr("vaf.core.channel_pairing.refile_whatsapp_owner_numbers", stop_alice_once)
        assert [(a[0], a[1]) for a in wa.Reconnector().step(0.0)] == [("*", True)]
    else:
        for name, scope in (("alice", ALICE_SCOPE), ("bob", BOB_SCOPE)):
            world.link(name)
            world.own_switch(scope, True)
        monkeypatch.setattr("vaf.core.channel_pairing.refile_whatsapp_owner_numbers", stop_alice_once)
        assert _client(ADMIN).post("/api/whatsapp/start").status_code == 200
    assert world.wait_for(lambda: wa._run_outcome.settled.is_set() and world.live("bob"))
    assert hooked and world.names() == ["bob"] and not world.live("alice")


def test_a_stop_that_could_not_end_the_process_is_not_undone_when_it_exits(world, monkeypatch):
    """Alice's own Stop could not end her process (it survives the kill and stays registered,
    retired); later it exits. The worker does not restart her: her Stop took her out of the
    run. MUTATION: drop the membership check for a judged process."""
    world.proc_class["alice"] = Unkillable
    procs = _bridge_with(world, "admin", "alice")
    world.proc_class.clear()
    _connected(monkeypatch, "admin")
    try:
        assert _own_stop(ALICE).status_code == 500
        assert wa._processes["alice"] is procs["alice"] and wa._is_retired(procs["alice"])
        procs["alice"].returncode = -9
        reconnector = wa.Reconnector(grace=0.0, cooldown=0.0)
        assert [a for a in _passes(reconnector, 0.0, 1.0, 2.0) if a[0] == "alice"] == []
        assert world.names().count("alice") == 1
    finally:
        procs["alice"].returncode = -9


def test_an_expired_session_is_not_brought_back_by_a_recovery(world, monkeypatch):
    """Alice's session expired (her process stopped itself on the QR line), then the bridge
    thread failed: the recovery brings Bob back and not Alice, whose start would only ask for a
    QR again. MUTATION: the expiry handler leaves her in the run."""
    crash = _crashing_runs(monkeypatch)
    procs = _bridge_with(world, "alice", "bob")
    wa._dispatch_bridge_event("alice", ALICE_SCOPE, "qr", {}, proc=procs["alice"])
    assert "alice" not in wa._processes
    _crash_and_wait(crash)
    world.spawned.clear()
    assert [(a[0], a[1]) for a in wa.Reconnector().step(0.0)] == [("*", True)]
    assert world.wait_for(lambda: wa._run_outcome.settled.is_set() and world.live("bob"))
    assert world.names() == ["bob"]


def test_an_account_that_started_into_the_run_is_its_member(world, monkeypatch):
    """Alice's start brought the bridge up for her alone; Bob then started his own into it. He
    is a member of that run like Alice: a crash recovery brings back both (the rule: whoever
    the run was started for or who started into it, minus stops on request), and a failed
    automatic restart of his is tried again up to the bound. MUTATION: `_spawn_account` does
    not add the account it spawned to the run's members."""
    crash = _crashing_runs(monkeypatch)
    for name, scope in (("alice", ALICE_SCOPE), ("bob", BOB_SCOPE)):
        world.link(name)
        world.own_switch(scope, True)
    assert wa.start_account("alice", ALICE_SCOPE) == (True, wa.ACCOUNT_STARTED)
    assert wa.start_account("bob", BOB_SCOPE) == (True, wa.ACCOUNT_STARTED)

    # A failed automatic restart of his: three tries.
    _connected(monkeypatch, "alice")
    attempts = []
    world.on_spawn = lambda name: attempts.append(name)
    world.fail_spawn.add("bob")
    reconnector = wa.Reconnector(grace=0.0, cooldown=0.0)
    for t in range(6):
        reconnector.step(float(t))
    assert attempts == ["bob"] * 3
    world.fail_spawn.clear()
    world.on_spawn = None
    assert wa.start_account("bob", BOB_SCOPE) == (True, wa.ACCOUNT_STARTED)

    # The crash: both come back.
    _crash_and_wait(crash)
    world.spawned.clear()
    assert [(a[0], a[1]) for a in wa.Reconnector().step(0.0)] == [("*", True)]
    assert world.wait_for(lambda: world.live("alice") and world.live("bob"))
    assert sorted(world.names()) == ["alice", "bob"]


def test_a_bridge_that_stops_taking_processes_meanwhile_is_left_alone(world, monkeypatch):
    """The pass judged Alice down; while it waited for her connection check, the bridge
    stopped taking processes (its stop is under way, the thread has not ended it yet, so her
    registration is still there). Nothing is restarted and her process is not stopped by the
    worker. MUTATION: drop the bridge-open check in `_reconnect_account`."""
    procs = _bridge_with(world, "admin", "alice")

    def status(username, wait_timeout=2.0, **k):
        if username == "alice":
            wa._spawns_open = False
            return False
        return True
    monkeypatch.setattr(wa, "get_connection_status", status)
    assert wa.Reconnector(grace=0.0, cooldown=0.0).step(0.0) == []
    assert wa._processes["alice"] is procs["alice"] and procs["alice"].returncode is None


def test_a_recovery_with_nobody_left_starts_no_bridge(world, monkeypatch, caplog):
    """Between the pass's look at the members and the recovery's own read, Alice's WhatsApp was
    unlinked: nobody is left to run, so no run is published, no try is counted and no failure
    logged. MUTATION: publish the recovery run with nobody in it."""
    crash = _crashing_runs(monkeypatch)
    _bridge_with(world, "alice")
    _crash_and_wait(crash)
    failed = wa._run_outcome
    real_users, reads = wa._get_users_to_run, []

    def unlinked_after_the_first_read():
        reads.append(True)
        return real_users() if len(reads) == 1 else []
    monkeypatch.setattr(wa, "_get_users_to_run", unlinked_after_the_first_read)
    reconnector = wa.Reconnector()
    with caplog.at_level(logging.WARNING, logger="vaf.api.whatsapp_bridge"):
        assert reconnector.step(0.0) == []
    assert len(reads) == 2
    assert wa._run_outcome is failed and not wa.is_bridge_running()
    assert reconnector._crash_restarts == 0
    assert not [r for r in caplog.records if "did not start again" in r.getMessage()]


# -- the recovery's own checks: the current run, eligibility, preflight, the launch -------------

CAROL = {"username": "carol", "role": "user", "user_scope_id": CAROL_SCOPE}


def _during_the_pass_read(monkeypatch, action):
    """Run `action` inside the pass's look at who is runnable (`_bring_back`'s
    `_get_users_to_run`, after it took the failed run's outcome), once."""
    real, done = wa._get_users_to_run, []

    def users():
        if not done and threading.current_thread() is threading.main_thread():
            done.append(True)
            action()
        return real()
    monkeypatch.setattr(wa, "_get_users_to_run", users)
    return done


@pytest.mark.parametrize("meanwhile", ["carol_cold_start_and_stops", "admin_start"])
def test_a_recovery_of_a_replaced_run_is_not_started(world, monkeypatch, caplog, meanwhile):
    """The pass took the failed run's outcome; before its recovery starts, another run replaces
    it. (a) Carol's own cold start publishes a run, Alice and Carol stop their own WhatsApp
    before its spawn phase (their Stops act on that run), and it ends with nobody to run: a
    recovery of the old run would start Alice again. (b) An admin's whole Start brought the
    bridge up: that is not this recovery's success. Either way the recovery answers
    "cancelled": nothing is started by it and no try is counted. MUTATION: drop the check that
    `recover` is the current run."""
    crash = _crashing_runs(monkeypatch)
    _bridge_with(world, "alice", "bob")
    _crash_and_wait(crash)
    failed = wa._run_outcome
    world.link("carol")
    world.own_switch(CAROL_SCOPE, True)
    world.spawned.clear()

    def carol_cold_start_and_stops():
        hold = threading.Event()
        monkeypatch.setattr("vaf.core.channel_pairing.refile_whatsapp_owner_numbers",
                            lambda: hold.wait(timeout=5) and [])
        answer = {}
        start = threading.Thread(target=lambda: answer.update(r=wa.start_account("carol", CAROL_SCOPE)))
        start.start()
        assert _wait(lambda: wa._run_outcome is not failed and wa.is_bridge_running())
        assert _own_stop(ALICE).status_code == 200
        assert _own_stop(CAROL).status_code == 200
        hold.set()
        start.join(timeout=10)
        assert _wait(lambda: not wa.is_bridge_running())

    def admin_start():
        assert _client(ADMIN).post("/api/whatsapp/start").status_code == 200
        assert _wait(lambda: wa._run_outcome.settled.is_set())
    hooked = _during_the_pass_read(monkeypatch, carol_cold_start_and_stops if meanwhile.startswith("carol")
                                   else admin_start)
    spawned_before = list(world.names())
    reconnector = wa.Reconnector()
    with caplog.at_level(logging.WARNING, logger="vaf.api.whatsapp_bridge"):
        assert reconnector.step(0.0) == []
    assert hooked and reconnector._crash_restarts == 0
    assert not [r for r in caplog.records if "after a failed run" in r.getMessage()]
    if meanwhile.startswith("carol"):
        time.sleep(0.1)
        assert world.names() == spawned_before and not world.live("alice") and not world.live("carol")


def test_a_stop_with_no_run_up_puts_no_end_marker_into_a_new_run(world, monkeypatch):
    """An admin's Stop while no run is up, and Alice's own cold start published right after the
    Stop set its stop: the end marker goes to the queue of the run the Stop ended (none), never
    into the new run's queue, whose sender loop would end on it. MUTATION: `stop_bridge` reads
    the queue after its locked step; publication assigns the queue outside its locked step."""
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    real = wa._marks_lock

    class AfterRelease:
        hook = None

        def __enter__(self):
            real.acquire()
            return self

        def __exit__(self, *exc):
            real.release()
            hook, AfterRelease.hook = AfterRelease.hook, None
            if hook:
                hook()
    monkeypatch.setattr(wa, "_marks_lock", AfterRelease())
    AfterRelease.hook = lambda: wa.start_account("alice", ALICE_SCOPE)
    assert _client(ADMIN).post("/api/whatsapp/stop").status_code == 200
    assert world.wait_for(lambda: world.live("alice"))
    assert wa.is_bridge_running() and None not in list(wa._outgoing_queue.queue)


def test_a_recovery_whose_thread_could_not_launch_is_tried_again(world, monkeypatch):
    """The recovery's bridge thread could not be launched: that was a try, and the intent stays,
    so the next pass after the cooldown starts the members. MUTATION: the failed launch leaves
    a run that is not marked failed (the recovery is lost)."""
    crash = _crashing_runs(monkeypatch)
    _bridge_with(world, "alice")
    _crash_and_wait(crash)
    world.spawned.clear()
    launches = []

    class Threads:
        def __getattr__(self, name):
            return getattr(threading, name)

        class Thread(threading.Thread):
            def start(self):
                if getattr(self, "_target", None) is wa._run_bridge:
                    launches.append(True)
                    if len(launches) == 1:
                        raise RuntimeError("can't start new thread")
                return super().start()
    monkeypatch.setattr(wa, "threading", Threads())
    reconnector = wa.Reconnector(cooldown=0.0)
    assert [(a[0], a[1]) for a in reconnector.step(0.0)] == [("*", False)]
    assert not wa.is_bridge_running()
    assert [(a[0], a[1]) for a in reconnector.step(1.0)] == [("*", True)]
    assert world.wait_for(lambda: world.live("alice"))
    assert len(launches) == 2 and reconnector._crash_restarts == 2


@pytest.mark.parametrize("meanwhile", ["admin_stop", "last_member_stops"])
def test_a_recovery_overtaken_during_its_preflight_is_no_try(world, monkeypatch, caplog, meanwhile):
    """The recovery's dependency check fails, and while it ran an admin stopped the bridge, or
    the last member stopped its own WhatsApp: the attempt was overtaken, so it spends no try and
    logs no failure. MUTATION: report the preflight failure without asking whether the attempt
    was overtaken."""
    crash = _crashing_runs(monkeypatch)
    _bridge_with(world, "alice")
    _crash_and_wait(crash)

    def deps_fail_after_a_stop():
        if meanwhile == "admin_stop":
            assert _client(ADMIN).post("/api/whatsapp/stop").status_code == 200
        else:
            assert _own_stop(ALICE).status_code == 200
        return False, "npm ci failed"
    monkeypatch.setattr(wa, "ensure_bridge_deps", deps_fail_after_a_stop)
    reconnector = wa.Reconnector(cooldown=0.0)
    with caplog.at_level(logging.WARNING, logger="vaf.api.whatsapp_bridge"):
        assert reconnector.step(0.0) == []
    assert reconnector._crash_restarts == 0
    assert not [r for r in caplog.records if "npm ci failed" in r.getMessage()
                or "did not start again" in r.getMessage()]


@pytest.mark.parametrize("others", ["alone", "others_run"])
def test_a_recovery_reads_who_is_runnable_at_its_publication(world, monkeypatch, caplog, others):
    """Alice, the failed run's only member, is switched off during the recovery's preflight
    (a settings save). The recovery reads who is runnable again right before it publishes:
    nobody of its members is, so it answers "nobody", publishes no run and spends no try, also
    when another account (Carol, no member) is linked and switched on. MUTATION: publish for
    the members without reading who is runnable again; drop the recovery's own "nobody"
    answer."""
    crash = _crashing_runs(monkeypatch)
    _bridge_with(world, "alice")
    _crash_and_wait(crash)
    failed = wa._run_outcome
    if others == "others_run":
        world.link("carol")
        world.own_switch(CAROL_SCOPE, True)
    real_node = wa._node_path

    def node_after_a_switch_off():
        world.own_switch(ALICE_SCOPE, False)
        return real_node()
    monkeypatch.setattr(wa, "_node_path", node_after_a_switch_off)
    world.spawned.clear()
    reconnector = wa.Reconnector()
    with caplog.at_level(logging.WARNING, logger="vaf.api.whatsapp_bridge"):
        assert reconnector.step(0.0) == []
    assert wa._run_outcome is failed and not wa.is_bridge_running()
    assert reconnector._crash_restarts == 0 and world.names() == []


@pytest.mark.parametrize("start", ["admin_whole_start", "recovery"])
def test_a_stop_after_the_first_filter_is_kept(world, monkeypatch, start):
    """Alice's own Stop lands after the spawn phase filtered its users by the run's members and
    before its loop reaches her (between the two, the run installs its reply callback, outside
    the lock): the check before each spawn skips her. MUTATION: drop that check."""
    hooked = []
    real_callback = wa.set_whatsapp_reply_callback

    def callback_then_stop(cb):
        real_callback(cb)
        if cb is not None and not hooked and threading.current_thread() is wa._bridge_thread:
            hooked.append(True)
            assert _own_stop(ALICE).status_code == 200
    if start == "recovery":
        crash = _crashing_runs(monkeypatch)
        _bridge_with(world, "alice", "bob")
        _crash_and_wait(crash)
        world.spawned.clear()
        monkeypatch.setattr(wa, "set_whatsapp_reply_callback", callback_then_stop)
        assert [(a[0], a[1]) for a in wa.Reconnector().step(0.0)] == [("*", True)]
    else:
        for name, scope in (("alice", ALICE_SCOPE), ("bob", BOB_SCOPE)):
            world.link(name)
            world.own_switch(scope, True)
        monkeypatch.setattr(wa, "set_whatsapp_reply_callback", callback_then_stop)
        assert _client(ADMIN).post("/api/whatsapp/start").status_code == 200
    assert world.wait_for(lambda: wa._run_outcome.settled.is_set() and world.live("bob"))
    assert hooked and world.names() == ["bob"] and not world.live("alice")


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
    _record(world, monkeypatch, "_start_bridge")
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
    assert [c[0] for c in world.calls] == ["_start_bridge"] * 3
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


# -- Fuzz: Stops through the routes, crashes and reconnect passes --------------------------------

FUZZ_USERS = {"alice": {"username": "alice", "role": "user", "user_scope_id": ALICE_SCOPE},
              "bob": {"username": "bob", "role": "user", "user_scope_id": BOB_SCOPE}}
# Draws where a Stop's run was still ending when the oracle first looked (not a violation; the
# end is waited for). Reported with the result.
ENDING_AT_CHECK = [0]
# (operation, who it acts on, "stop" / "start" / None): the explicit operations, as the product
# sends them.
EXPLICIT = (
    ("own_start", "post", "/api/whatsapp/start", None, "start"),
    ("own_stop", "post", "/api/whatsapp/stop", None, "stop"),
    ("own_on", "put", "/api/whatsapp/switch", {"enabled": True}, "start"),
    ("own_off", "put", "/api/whatsapp/switch", {"enabled": False}, "stop"),
    ("admin_start", "post", "/api/whatsapp/start", None, "start"),
    ("admin_stop", "post", "/api/whatsapp/stop", None, "stop"),
    ("admin_restart", "post", "/api/whatsapp/restart", None, "start"),
)


# How long a lane of the route fuzz may run before the fuzz stops (`_LaneHung`).
ROUTE_LANE_HANG = 60.0


class _LaneHung(Exception):
    def __init__(self, plan):
        super().__init__(plan)
        self.plan = plan


def _route_draw(world, rng, crash):
    """One draw: lane A sends 3 to 6 explicit operations through the routes, lane B runs 3 to 6
    automatic events (a crash of the bridge thread, a reconnect pass, a process death), with 0
    to 3 ms between them. Returns (plan, violations)."""
    times = {name: {"stop": None, "start_end": 0.0} for name in FUZZ_USERS}
    clients = {name: _client(user) for name, user in FUZZ_USERS.items()}
    admin = _client(ADMIN)
    # Weighted towards the cases that matter: a Stop next to a crash and a reconnect pass.
    explicit = EXPLICIT + tuple(e for e in EXPLICIT if e[4] == "stop")
    plan_a = [(rng.choice(explicit), rng.choice(sorted(FUZZ_USERS)), rng.uniform(0, 0.003))
              for _ in range(rng.randint(3, 6))]
    plan_b = [(rng.choice(("crash", "crash", "step", "step", "step", "death")), rng.choice(sorted(FUZZ_USERS)),
               rng.uniform(0, 0.003)) for _ in range(rng.randint(3, 6))]
    reconnector = wa.Reconnector(grace=0.0, cooldown=0.0)
    errors = []

    def lane_a():
        for (op, method, path, body, kind), name, pause in plan_a:
            time.sleep(pause)
            client = admin if op.startswith("admin") else clients[name]
            touched = sorted(FUZZ_USERS) if op.startswith("admin") else [name]
            began = time.monotonic()
            call = getattr(client, method)
            res = call(path, json=body) if body is not None else call(path)
            ended = time.monotonic()
            if res.status_code >= 500:
                errors.append(f"{op} {name}: {res.status_code} {res.text}")
            for who in touched:
                if kind == "stop":
                    times[who]["stop"] = began
                else:
                    times[who]["start_end"] = max(times[who]["start_end"], ended)

    def lane_b():
        clock = 0.0
        for event, name, pause in plan_b:
            time.sleep(pause)
            if event == "crash":
                if wa.is_bridge_running():
                    crash.set()
                    _wait(lambda: not wa.is_bridge_running())
                    crash.clear()
            elif event == "step":
                clock += 1.0
                reconnector.step(clock)
            else:
                with wa._process_lock:
                    proc = wa._processes.get(name)
                if proc is not None and proc.returncode is None:
                    proc.returncode = 1

    def guarded(fn):
        def run():
            try:
                fn()
            except BaseException as e:  # noqa: BLE001 - the oracle reports it
                errors.append(f"raised: {e!r}")
        return run
    plan = [p[0][0] + ":" + p[1] for p in plan_a] + [p[0] + ":" + p[1] for p in plan_b]
    threads = [threading.Thread(target=guarded(fn), daemon=True) for fn in (lane_a, lane_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=ROUTE_LANE_HANG)
    if any(t.is_alive() for t in threads):
        # A lane that still runs would act on the next draw's state: no verdict, no next draw.
        raise _LaneHung(plan)
    violations = list(errors)

    def undone():
        return [name for name in FUZZ_USERS
                if times[name]["stop"] is not None and times[name]["stop"] > times[name]["start_end"]
                and wa._runs_own_process(name, SCOPES[name])]
    # A whole Stop only sets the run's stop; its thread ends the run (and its processes) right
    # after. Judged before that end, every Stop would read as undone: wait for it first.
    thread = wa._bridge_thread
    before_the_end = undone()
    if thread is not None and thread.is_alive() and wa._bridge_stop.is_set():
        thread.join(timeout=10)
        if thread.is_alive():
            violations.append("the stopped run did not end within 10 s; no verdict on its Stops")
            return plan, violations
    # The verdict reads process liveness: only once the run's spawn phase is over.
    if wa.is_bridge_running() and not wa._run_outcome.settled.wait(timeout=10):
        violations.append(f"the spawn phase did not settle within 10 s (registered: {sorted(wa._processes)}); "
                          "no verdict on its Stops")
        return plan, violations
    late = undone()
    if before_the_end and not late:
        ENDING_AT_CHECK[0] += 1
    violations.extend(f"{name}: its Stop was undone" for name in late)
    return plan, violations


def test_route_stop_fuzz(world, monkeypatch):
    """Seeded draws of explicit operations through the routes (an account's own Start, Stop and
    switch, an admin's Start, Stop and Restart) on one thread, and crashes of the bridge thread,
    reconnect passes and process deaths on another. Oracle: a Stop that began after every
    start-like operation for that account had ended is never undone at the end. Reconnect
    passes and crash recoveries are not start-like: they must keep every Stop.
    `VAF_FUZZ_DRAWS` raises the draw count outside the suite; the seed is printed on failure."""
    _quiet_reads(monkeypatch)
    monkeypatch.setattr(wa, "TERMINATE_WAIT", 0.05)
    crash = _crashing_runs(monkeypatch)
    ENDING_AT_CHECK[0] = 0
    draws = int(os.environ.get("VAF_FUZZ_DRAWS", "80"))
    seed = int(os.environ.get("VAF_FUZZ_SEED", "4542"))
    master = random.Random(seed)
    failures = []
    for draw in range(draws):
        for name in FUZZ_USERS:
            world.link(name)
            world.own_switch(SCOPES[name], True)
        world.spawned.clear()
        crash.clear()
        draw_rng = random.Random(master.random())
        assert wa.start_bridge()
        assert world.wait_for(lambda: all(world.live(n) for n in FUZZ_USERS), timeout=30)
        try:
            plan, violations = _route_draw(world, draw_rng, crash)
        except _LaneHung as hung:
            pytest.fail(f"seed {seed}, draw {draw}: a lane did not end within {ROUTE_LANE_HANG:.0f} s; plan {hung.plan}. "
                        "No further draw runs on this state.")
        if violations:
            failures.append((draw, plan, violations))
            print(f"draw {draw}: {failures[-1]}")
        wa.stop_bridge()
        if wa._bridge_thread is not None:
            wa._bridge_thread.join(timeout=10)
    print(f"route stop fuzz: {draws} draws, seed {seed}, {len(failures)} mismatches, "
          f"{ENDING_AT_CHECK[0]} with the Stop's run still ending at the first look")
    assert failures == [], f"seed {seed}: {failures[:3]}"


class _StepsOnly:
    """A draw's random source: lane A sends one own Stop, lane B runs reconnect passes."""

    def choice(self, seq):
        if "step" in seq:
            return "step"
        return seq[0] if not isinstance(seq[0], tuple) else next(e for e in seq if e[0] == "own_stop")

    def randint(self, a, b):
        return a

    def uniform(self, a, b):
        return 0.0


def test_a_hung_route_fuzz_lane_stops_the_fuzz(world, monkeypatch):
    """A lane still running after its deadline gives no verdict and no further draw: the draw
    raises with its plan instead of judging a state the lane may still change. MUTATION: judge
    the draw anyway (record the hang and go on)."""
    _bridge_with(world, "alice", "bob")
    _quiet_reads(monkeypatch)
    monkeypatch.setattr(sys.modules[__name__], "ROUTE_LANE_HANG", 0.2)
    release = threading.Event()
    monkeypatch.setattr(wa.Reconnector, "step", lambda self, now: release.wait(30) and [])
    try:
        with pytest.raises(_LaneHung) as hung:
            _route_draw(world, _StepsOnly(), threading.Event())
        assert any(p.startswith("step:") for p in hung.value.plan)
    finally:
        release.set()
