# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Each account's WhatsApp switch is its own, and decides its own process only.

THE DEFECT, measured on the code before this change with the fake processes of
test_whatsapp_account_process.py: the global `whatsapp_config.enabled` was the local admin's
own switch AND the master gate of every start, so an ordinary account whose own switch was on
got 400 "WhatsApp is switched off on this VAF" while the local admin's was off; the Connections
toggle saved the switch and then called `POST /stop`, which for any admin (the local admin or a
second one) ends every account's process; an admin's Start with nobody to run answered 200
"started" with nothing running; and a switch-off written between a start's check and its spawn
left the account running with its switch off.

THE RULE: `PUT /api/whatsapp/switch` writes the caller's own switch (through
`Config.set_own_channel_switch`) and then starts or stops the caller's own process, admins
included; the whole-bridge routes keep their meaning; nothing gates a start but the account's
own switch, and the spawn gate asks it under `_spawn_lock`.

Fake processes only: no Node, no network, no port.
"""
import os
import random
import sys
import threading
import time

import pytest

from vaf.api import whatsapp_bridge as wa
from vaf.api import whatsapp_routes as routes
from vaf.core.messaging_connections import channel_enabled_for_scope

from test_whatsapp_account_process import (  # noqa: F401 - `world` is a fixture
    ADMIN, ADMIN_SCOPE, ALICE, ALICE_SCOPE, BOB_SCOPE, SCOPES, WHOLE, _bridge_with, _client,
    _quiet_reads, _record, _stop_and_join, world,
)

# An admin account that is not the local admin: their switch is theirs, never the global one.
SECOND_ADMIN = {"username": "bob", "role": "admin", "user_scope_id": BOB_SCOPE}
CALLERS = {"local_admin": ADMIN, "second_admin": SECOND_ADMIN, "alice": ALICE}


def _own_switch_stored(world, user):
    """What the caller's own switch says on disk, read where it lives."""
    cfg = world.cfg()
    if user is ADMIN:
        return cfg["whatsapp_config"]["enabled"]
    return ((cfg.get("connection_enabled_by_scope") or {}).get(user["user_scope_id"]) or {}).get("whatsapp")


# -- AC1: the local admin's switch gates nobody else -------------------------------------------

def test_the_local_admins_switch_gates_only_their_own_process(world, monkeypatch):
    """MUTATION: restore the `enabled` check in `start_bridge`; restore `_global_switch_on` in
    the non-admin `/start`."""
    world.global_switch(False)
    world.link("admin")
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)

    # Alice's own start, from a stopped bridge.
    res = _client(ALICE).post("/api/whatsapp/start")
    assert res.status_code == 200, res.text
    assert res.json()["message"] == wa.ACCOUNT_STARTED
    assert world.wait_for(lambda: world.live("alice"))
    assert world.names() == ["alice"]
    _stop_and_join()

    # An admin's start of the whole bridge: Alice's process, not the admin's.
    world.spawned.clear()
    res = _client(ADMIN).post("/api/whatsapp/start")
    assert res.status_code == 200, res.text
    assert world.wait_for(lambda: world.live("alice"))
    time.sleep(0.05)
    assert world.names() == ["alice"] and not world.live("admin")
    _stop_and_join()

    # The startup path: `start_bridge()` from a stopped state.
    world.spawned.clear()
    assert wa.start_bridge() is True
    assert world.wait_for(lambda: world.live("alice"))
    time.sleep(0.05)
    assert world.names() == ["alice"]
    _stop_and_join()

    # The other way round: the admin's switch on, Alice's off.
    world.global_switch(True)
    world.own_switch(ALICE_SCOPE, False)
    world.spawned.clear()
    res = _client(ALICE).post("/api/whatsapp/start")
    assert res.status_code == 400, res.text
    assert res.json()["detail"] == wa.NOT_STARTABLE
    assert "alice" not in world.names()


# -- AC2: nobody to run is a 400, not a start -------------------------------------------------

@pytest.mark.parametrize("path", ["start", "restart"])
def test_nobody_to_run_is_a_400_not_a_start(world, monkeypatch, path):
    """The admin is linked with their switch off and nobody else is linked. MUTATION: drop the
    `anyone_to_run` check in `start_bridge` (the route then answers 200)."""
    world.global_switch(False)
    world.link("admin")
    res = _client(ADMIN).post(f"/api/whatsapp/{path}")
    assert res.status_code == 400, res.text
    assert res.json()["detail"] == wa.NOBODY_TO_RUN
    time.sleep(0.05)
    assert world.names() == [] and not wa.is_bridge_running()
    assert wa.start_bridge() is False and not wa.is_bridge_running()


# -- AC3: the switch route acts on the caller alone --------------------------------------------

@pytest.mark.parametrize("who", sorted(CALLERS))
def test_the_switch_route_acts_on_the_caller_alone(world, monkeypatch, who):
    """MUTATION: the route calls `stop_bridge` for admins; the writer writes the global block
    for a second admin; the start before the write."""
    user = CALLERS[who]
    procs = _bridge_with(world, "admin", "alice", "bob")
    me = user["username"]
    others = [n for n in ("admin", "alice", "bob") if n != me]
    before = world.cfg()
    _record(world, monkeypatch, *WHOLE)

    res = _client(user).put("/api/whatsapp/switch", json={"enabled": False})
    assert res.status_code == 200, res.text
    assert res.json() == {"enabled": False, "running": False, "message": "WhatsApp switched off for this account."}
    assert world.calls == [], "no whole-bridge function"
    assert _own_switch_stored(world, user) is False
    for name in others:
        other = next(u for u in (ADMIN, ALICE, SECOND_ADMIN) if u["username"] == name)
        assert _own_switch_stored(world, other) is True, name
        assert wa._processes[name] is procs[name] and procs[name].returncode is None, name
    assert procs[me].returncode is not None and me not in wa._processes
    if user is not ADMIN:
        assert world.cfg()["whatsapp_config"] == before["whatsapp_config"], "the global switch is the local admin's"

    res = _client(user).put("/api/whatsapp/switch", json={"enabled": True})
    assert res.status_code == 200, res.text
    assert res.json() == {"enabled": True, "running": True, "message": wa.ACCOUNT_STARTED}
    assert world.calls == []
    assert _own_switch_stored(world, user) is True
    assert world.live(me) and wa._processes[me] is not procs[me]
    for name in others:
        assert wa._processes[name] is procs[name] and procs[name].returncode is None, name


def test_the_switch_reads_no_account_from_the_body(world, monkeypatch):
    """A body or a query naming another account changes nothing of theirs; a value that is not
    a JSON boolean answers 422 and writes nothing."""
    procs = _bridge_with(world, "admin", "alice")
    res = _client(ALICE).put("/api/whatsapp/switch?username=admin&user_scope_id=" + ADMIN_SCOPE,
                             json={"enabled": False, "username": "admin", "user_scope_id": ADMIN_SCOPE})
    assert res.status_code == 200, res.text
    assert world.cfg()["whatsapp_config"]["enabled"] is True
    assert wa._processes["admin"] is procs["admin"] and procs["admin"].returncode is None
    assert world.cfg()["connection_enabled_by_scope"][ALICE_SCOPE]["whatsapp"] is False
    assert "alice" not in wa._processes

    on_disk = world.cfg()
    for body in ({"enabled": "yes"}, {"enabled": 1}, {"enabled": None}, {}, {"enabled": "false"}):
        res = _client(ADMIN).put("/api/whatsapp/switch", json=body)
        assert res.status_code == 422, (body, res.status_code, res.text)
    assert world.cfg() == on_disk
    assert wa._processes["admin"] is procs["admin"] and procs["admin"].returncode is None


def test_a_refused_start_keeps_the_switch_on(world, monkeypatch):
    """The switch is the account's choice: a start that is refused leaves it on, and the
    answer carries the start's status and reason. MUTATION: roll the switch back on a
    refusal; answer 200 for it."""
    _bridge_with(world, "admin")
    # Not linked: the start refuses with 400.
    res = _client(ALICE).put("/api/whatsapp/switch", json={"enabled": True})
    assert res.status_code == 400, res.text
    assert res.json()["detail"] == wa.NOT_STARTABLE
    assert world.cfg()["connection_enabled_by_scope"][ALICE_SCOPE]["whatsapp"] is True

    # Node could not be started: 500, told in the words for an account that is not an admin.
    world.link("alice")
    world.fail_spawn.add("alice")
    res = _client(ALICE).put("/api/whatsapp/switch", json={"enabled": True})
    assert res.status_code == 500, res.text
    assert res.json()["detail"] == routes.START_FAILED_FOR_ACCOUNT
    assert world.cfg()["connection_enabled_by_scope"][ALICE_SCOPE]["whatsapp"] is True
    assert not world.live("alice")


# -- AC6: a switch-off races no start -----------------------------------------------------------

def _switch_off(name):
    """What the switch route does for a switch-off: the switch, then the stop."""
    routes._write_own_switch(SCOPES[name], False)
    wa.stop_account(name, SCOPES[name])


def _off_after_the_users_are_read(monkeypatch, name):
    """The bridge thread reads who to run, then the switch-off lands, then it spawns."""
    real = wa._get_users_to_run
    fired = []

    def wrapped():
        users = real()
        if not fired and threading.current_thread() is wa._bridge_thread:
            fired.append(True)
            _switch_off(name)
        return users
    monkeypatch.setattr(wa, "_get_users_to_run", wrapped)
    return fired


def _off_after_the_check(monkeypatch, name):
    """The reconnect worker's restart passed its own check (`_runnable_account`), then the
    switch-off lands, then it takes the spawn lock."""
    real = wa._runnable_account
    fired = []

    def wrapped(username, user_scope_id):
        entry = real(username, user_scope_id)
        if not fired and username == name and entry is not None:
            fired.append(True)
            _switch_off(name)
        return entry
    monkeypatch.setattr(wa, "_runnable_account", wrapped)
    return fired


def _off_before_the_spawn(monkeypatch, name):
    """A start passed its own check (`_runnable_account`), then the switch-off lands, then it
    takes the spawn lock (`_start_into_running`)."""
    real = wa._start_into_running
    fired = []

    def wrapped(entry):
        if not fired and entry[1] == name:
            fired.append(True)
            _switch_off(name)
        return real(entry)
    monkeypatch.setattr(wa, "_start_into_running", wrapped)
    return fired


@pytest.mark.parametrize("path", ["start_account", "start_missing_accounts", "bridge_thread", "reconnector"])
def test_a_switch_off_races_no_start(world, monkeypatch, path):
    """The switch-off is written and Alice is stopped between a start's check and its spawn:
    no live, unretired process of hers is left. MUTATION: drop the switch check from
    `_account_may_start`."""
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    if path == "bridge_thread":
        world.link("admin")
        fired = _off_after_the_users_are_read(monkeypatch, "alice")
        assert wa.start_bridge()
        assert world.wait_for(lambda: world.live("admin") and wa._run_outcome.settled.is_set())
    else:
        procs = _bridge_with(world, "admin") if path != "reconnector" else _bridge_with(world, "admin", "alice")
        fired = _off_before_the_spawn(monkeypatch, "alice")
        if path == "start_account":
            assert wa.start_account("alice", ALICE_SCOPE) == (False, wa.NOT_STARTABLE)
        elif path == "start_missing_accounts":
            assert wa.start_missing_accounts() == []
        else:
            # The reconnect's own check is `_runnable_account`, before the spawn lock: the
            # switch-off lands right after it.
            fired = _off_after_the_check(monkeypatch, "alice")
            monkeypatch.setattr(wa, "get_connection_status",
                                lambda username, wait_timeout=2.0, **k: username == "admin")
            procs["alice"].returncode = 1          # her Node exited: a reconnect candidate
            reconnector = wa.Reconnector(grace=0.0, cooldown=0.0)
            # The switch-off's stop removed the process it judged: nothing to restart, or a
            # refusal; never a start.
            assert not [a for a in reconnector.step(1000.0) if a[1]]
    assert fired, "the switch-off never landed in the window"
    time.sleep(0.05)
    assert not channel_enabled_for_scope("whatsapp", ALICE_SCOPE)
    assert not wa._runs_own_process("alice", ALICE_SCOPE)
    assert not [p for p in world.spawned if p.name == "alice" and p.returncode is None]
    assert world.live("admin")


def test_a_start_whose_switch_went_off_is_a_refusal_not_a_failure(world, monkeypatch):
    """A switch turned off without a stop (a settings save) between a start's checks and the
    spawn's own is told as "not switched on" (400), not as a Node failure (500): the cold
    start's waiting answer, and a start into the running bridge whose two gate reads straddle
    the write. MUTATION: answer START_FAILED for a spawn the switch refused."""
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    real_users = wa._get_users_to_run

    def users_then_off():
        users = real_users()
        if threading.current_thread() is wa._bridge_thread:
            routes._write_own_switch(ALICE_SCOPE, False)
        return users
    monkeypatch.setattr(wa, "_get_users_to_run", users_then_off)
    res = _client(ALICE).post("/api/whatsapp/start")
    assert res.status_code == 400, res.text
    assert res.json()["detail"] == wa.NOT_STARTABLE
    assert not world.live("alice")
    _stop_and_join()
    monkeypatch.setattr(wa, "_get_users_to_run", real_users)

    _bridge_with(world, "admin")
    world.own_switch(ALICE_SCOPE, True)
    real_gate = wa._account_may_start
    reads = []

    def gate(scope):
        reads.append(scope)
        if len(reads) == 1:
            answer = real_gate(scope)
            routes._write_own_switch(ALICE_SCOPE, False)
            return answer
        return real_gate(scope)
    monkeypatch.setattr(wa, "_account_may_start", gate)
    assert wa.start_account("alice", ALICE_SCOPE) == (False, wa.NOT_STARTABLE)
    assert len(reads) >= 2 and not world.live("alice")


def test_a_start_whose_bridge_stops_meanwhile_answers_stopping(world, monkeypatch):
    """`stop_bridge` sets the run's stop without the spawn lock, so it can land between a start's
    own check of it and the spawn's: that start is told the bridge is stopping, not that Node
    could not be started. Found by the spawn-gate fuzz of test_whatsapp_account_process.py
    (seed 20261120, draw 134: "start of admin failed"), whose window the gate's switch read
    had widened. MUTATION: answer START_FAILED for every refused spawn."""
    _bridge_with(world, "admin")
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    real_gate = wa._account_may_start
    reads = []

    def gate(scope):
        reads.append(scope)
        if len(reads) == 1:
            wa.stop_bridge()            # lands between the start's check and the spawn's
        return real_gate(scope)
    monkeypatch.setattr(wa, "_account_may_start", gate)
    assert wa.start_account("alice", ALICE_SCOPE) == (False, wa.BRIDGE_STOPPING)
    assert not world.live("alice")


# -- AC12: the admin's view of who runs ----------------------------------------------------------

def test_running_by_account_is_an_admins(world, monkeypatch):
    """MUTATION: send the map to everybody."""
    _bridge_with(world, "admin", "alice")
    _quiet_reads(monkeypatch)
    res = _client(ADMIN).get("/api/whatsapp/dashboard")
    assert res.status_code == 200, res.text
    assert res.json()["running_by_account"] == {"admin": True, "alice": True}
    assert wa.stop_account("alice", ALICE_SCOPE) is True
    assert _client(ADMIN).get("/api/whatsapp/dashboard").json()["running_by_account"] == {"admin": True, "alice": False}
    res = _client(ALICE).get("/api/whatsapp/dashboard")
    assert res.status_code == 200, res.text
    assert res.json()["running_by_account"] is None


# -- Fuzz: the spawn gate under switch-offs -------------------------------------------------------

FUZZ_NAMES = ("admin", "alice", "bob")
# How long a lane may run before it counts as hung, and the longest one seen (reported).
LANE_HANG = 60.0
LONGEST_LANE = [0.0]
# Set once a lane hung: the fuzz stops at that draw.
HUNG = threading.Event()


def _fuzz_draw(world, rng, violations):
    """One schedule: 4 to 10 operations over three threads with 0 to 3 ms between them, on a
    bridge that runs every account at first."""
    ops = ("switch_on", "switch_off", "start_account", "restart_account", "start_missing", "step", "death")
    plan = [(rng.choice(ops), rng.choice(FUZZ_NAMES), rng.uniform(0, 0.003)) for _ in range(rng.randint(4, 10))]
    lanes = [plan[i::3] for i in range(3)]
    reconnector = wa.Reconnector(grace=0.0, cooldown=0.0)
    clock = [0.0]

    def run(kind, name):
        scope = SCOPES[name]
        if kind in ("switch_on", "switch_off"):
            try:
                routes._switch_own_whatsapp(name, scope, kind == "switch_on", name == "admin")
            except routes.HTTPException as e:
                if e.status_code != 400 and e.detail != wa.PREVIOUS_NOT_ENDED:
                    violations.append(f"{kind} {name}: {e.status_code} {e.detail}")
        elif kind == "start_account":
            wa.start_account(name, scope)
        elif kind == "restart_account":
            wa.restart_account(name, scope)
        elif kind == "start_missing":
            wa.start_missing_accounts()
        elif kind == "step":
            clock[0] += 1.0
            reconnector.step(clock[0])
        elif kind == "death":
            with wa._process_lock:
                proc = wa._processes.get(name)
            if proc is not None and proc.returncode is None:
                proc.returncode = 1

    errors, longest = [], [0.0]

    def lane(items):
        began = time.monotonic()
        try:
            for kind, name, pause in items:
                time.sleep(pause)
                run(kind, name)
        except BaseException as e:  # noqa: BLE001 - every failure is the oracle's business
            errors.append(e)
        finally:
            longest[0] = max(longest[0], time.monotonic() - began)

    # Joined in full, never after a short timeout: a lane still running would carry its
    # operations into the next draw. A lane that hangs is a violation that ends the whole fuzz
    # (`HUNG`): no later draw runs next to it, and as a daemon thread it cannot keep the test
    # process from exiting.
    threads = [threading.Thread(target=lane, args=(items,), daemon=True) for items in lanes]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=LANE_HANG)
    if any(t.is_alive() for t in threads):
        HUNG.set()
        violations.append(f"lane hung for {LANE_HANG:.0f} s; the fuzz stops here")
    violations.extend(f"raised: {e!r}" for e in errors)
    LONGEST_LANE[0] = max(LONGEST_LANE[0], longest[0])
    for name in FUZZ_NAMES:
        alive = [p for p in world.spawned if p.name == name and p.returncode is None]
        if len(alive) > 1:
            violations.append(f"{name}: {len(alive)} live processes")
        if not channel_enabled_for_scope("whatsapp", SCOPES[name]) and wa._runs_own_process(name, SCOPES[name]):
            violations.append(f"{name}: runs with the switch off")
    return plan


class _OneOpRng:
    """A draw's random source that plans four `start_missing` operations with no pause."""

    def choice(self, seq):
        return "start_missing" if "start_missing" in seq else seq[0]

    def randint(self, a, b):
        return a

    def uniform(self, a, b):
        return 0.0


def test_a_hung_lane_stops_the_fuzz_and_cannot_hold_the_process(world, monkeypatch):
    """A lane that hangs is a violation, marks the fuzz as stopped, and runs as a daemon
    thread, so the test process can still exit. MUTATION: lanes as non-daemon threads; no
    `HUNG` mark."""
    _bridge_with(world, "admin")
    _quiet_reads(monkeypatch)
    monkeypatch.setattr(sys.modules[__name__], "LANE_HANG", 0.2)
    release = threading.Event()
    monkeypatch.setattr(wa, "start_missing_accounts", lambda: release.wait(30))
    HUNG.clear()
    violations = []
    try:
        _fuzz_draw(world, _OneOpRng(), violations)
        assert HUNG.is_set()
        assert any("hung" in v for v in violations), violations
        stuck = [t for t in threading.enumerate() if t.is_alive() and t is not threading.main_thread()
                 and not t.daemon]
        assert stuck == [], stuck
    finally:
        release.set()
        HUNG.clear()


def test_switch_off_fuzz(world, monkeypatch):
    """Seeded schedules of switch-ons and switch-offs (the route's own work), account starts and
    restarts, the admin's start into the running bridge, reconnector passes and process deaths.
    Oracle: after every thread has joined, no account whose stored switch is off has a live,
    unretired process, and no account has two live processes. `VAF_FUZZ_DRAWS` raises the draw
    count outside the suite; the seed is printed on failure."""
    _quiet_reads(monkeypatch)
    monkeypatch.setattr(wa, "TERMINATE_WAIT", 0.05)
    HUNG.clear()
    draws = int(os.environ.get("VAF_FUZZ_DRAWS", "150"))
    seed = int(os.environ.get("VAF_FUZZ_SEED", "454"))
    master = random.Random(seed)
    failures = []
    for draw in range(draws):
        for name in FUZZ_NAMES:
            world.link(name)
            if name == "admin":
                world.global_switch(True)
            else:
                world.own_switch(SCOPES[name], True)
        world.spawned.clear()
        draw_seed = master.random()
        assert wa.start_bridge()
        if not world.wait_for(lambda: all(world.live(n) for n in FUZZ_NAMES), timeout=30.0):
            failures.append((draw, "setup", [f"not all running: spawned {world.names()}, "
                                             f"registered {sorted(wa._processes)}, config {world.cfg()}"]))
            print(f"draw {draw}: {failures[-1]}")
            _stop_and_join()
            continue
        violations = []
        plan = _fuzz_draw(world, random.Random(draw_seed), violations)
        if violations:
            failures.append((draw, plan, violations))
            print(f"draw {draw}: {failures[-1]}")
        if HUNG.is_set():
            break       # a lane still runs: no shared state is trustworthy from here on
        _stop_and_join()
    print(f"switch-off fuzz: {draws} draws, seed {seed}, {len(failures)} mismatches, "
          f"longest lane {LONGEST_LANE[0]:.2f} s")
    assert failures == [], f"seed {seed}: {failures[:3]}"
