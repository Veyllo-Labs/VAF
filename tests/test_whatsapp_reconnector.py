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
(`restart_account`, bounded, every refusal logged), leaves alone an account without a
registration (stopped by its owner, an admin or its expiry) and a survivor that still runs,
and brings a crashed bridge back at most CRASH_RESTARTS_MAX times in a row.

Fake processes and a fake clock: no Node, no network, no sleep in the worker.
"""
import ast
import logging
from pathlib import Path

import pytest

from vaf.api import whatsapp_bridge as wa

from test_whatsapp_account_process import (  # noqa: F401 - `world` is a fixture
    ALICE, ALICE_SCOPE, Unkillable, WHOLE, _bridge_with, _client, _record, world,
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
    _record(world, monkeypatch, *WHOLE, "restart_account", "start_account")
    reconnector = wa.Reconnector()
    assert _passes(reconnector, *(120.0 * i for i in range(1, 11))) == []
    assert world.calls == [] and world.names() == ["alice"]


def test_only_the_down_account_is_restarted(world, monkeypatch):
    """The admin's connection is up, Alice's is down: after the grace, Alice is restarted
    through `restart_account` and the admin's process stays the same object. MUTATION: call
    `restart_bridge` instead."""
    procs = _bridge_with(world, "admin", "alice")
    _connected(monkeypatch, "admin")
    _record(world, monkeypatch, *WHOLE, "restart_account")
    reconnector = wa.Reconnector()
    assert reconnector.step(0.0) == [], "down, but not for the grace yet"
    assert reconnector.step(60.0) == []
    assert reconnector.step(120.0) == [("alice", True, wa.ACCOUNT_STARTED)]
    assert world.calls == [("restart_account", "alice", ALICE_SCOPE)]
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
    monkeypatch.setattr(wa, "restart_account", lambda name, scope: tries.append(name) or (False, "no route to WhatsApp"))
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
    """A stop on request, or a run that found nobody to run, is never brought back.
    MUTATION: bring back any bridge that is not running."""
    world.link("alice")
    world.own_switch(ALICE_SCOPE, True)
    if how == "requested":
        _bridge_with(world, "alice")
        wa.stop_bridge()
    else:
        assert wa.start_bridge(only="nobody")
    wa._bridge_thread.join(timeout=5)
    assert not wa.is_bridge_running() and not wa.last_run_failed()
    _record(world, monkeypatch, *WHOLE, "start_account", "restart_account")
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
