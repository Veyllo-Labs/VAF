# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The chat-turn queue takes turns by person (vaf/core/task_queue.py on FairOrder).

Measured before the change, one worker, both policies: alice queued turns in three chats,
then bob one, and the order handed out was A1, A2, A3, B1. Bob waited behind all of alice's
turns. What has to hold now: inside a priority band (and class) the next turn goes to the
person served least recently; one person's own turns keep their order; a busy chat is passed
by that person's next chat exactly as before; priorities and the class budget decide first,
unchanged; and a queued turn is told its place.
"""
import json
import threading

import pytest

from vaf.core.fair_queue import WaitInfo
from vaf.core.task_queue import (
    HOUSEKEEPING_KEY,
    TaskQueue,
    enqueue_system_command,
)

ALICE = "ab12cd34-0000-4000-8000-00000000a11c"
BOB = "ab12cd34-0000-4000-8000-0000000000b0"
CAROL = "ab12cd34-0000-4000-8000-0000000000c0"


def _fresh(legacy: bool) -> TaskQueue:
    TaskQueue._instance = None  # type: ignore[attr-defined]
    tq = TaskQueue()
    tq._legacy_mode = legacy  # type: ignore[attr-defined]
    tq._class_weights = {  # type: ignore[attr-defined]
        tq.TASK_CLASS_INTERACTIVE: 5,
        tq.TASK_CLASS_AUTOMATION: 3,
        tq.TASK_CLASS_BACKGROUND: 1,
    }
    tq._reset_scheduler_budget_locked()  # type: ignore[attr-defined]
    return tq


@pytest.fixture(autouse=True)
def _singleton_restored():
    yield
    TaskQueue._instance = None  # type: ignore[attr-defined]


def _turn(tq, sid, text, scope, **kw):
    return tq.add(session_id=sid, input_text=text, source="web",
                  metadata={"enqueue_user_scope_id": scope, "user_scope_id": scope,
                            "task_class": "interactive"}, **kw)


def _drain(tq, worker="w"):
    out = []
    while True:
        task = tq.get(timeout=0.0, worker_id=worker)
        if task is None:
            return out
        out.append(task.input_text)
        tq.task_done(task=task, worker_id=worker)


POLICIES = pytest.mark.parametrize("legacy", [True, False], ids=["legacy", "weighted_fair"])


# ── AC2: the round robin ─────────────────────────────────────────────────────────

@POLICIES
def test_one_person_with_three_chats_does_not_hold_up_another(legacy):
    """MUTATION: key every task on one constant (the order goes back to A1, A2, A3, B1)."""
    tq = _fresh(legacy)
    for i in (1, 2, 3):
        _turn(tq, f"alice-{i}", f"A{i}", ALICE)
    _turn(tq, "bob-1", "B1", BOB)
    assert _drain(tq) == ["A1", "B1", "A2", "A3"]


# ── AC3: each person's own order ─────────────────────────────────────────────────

@POLICIES
def test_one_chat_keeps_its_order(legacy):
    """MUTATION: pop the LAST eligible item of a key - red."""
    tq = _fresh(legacy)
    for i in (1, 2, 3):
        _turn(tq, "alice-chat", f"x{i}", ALICE)
    assert _drain(tq) == ["x1", "x2", "x3"]


@POLICIES
def test_a_busy_chat_is_passed_by_the_same_persons_next_chat(legacy):
    """A later turn passes an earlier one of the same person only while the earlier one's
    chat is in flight (the rule before this change).
    MUTATION: drop the in-flight `eligible` - the busy chat's second turn goes to a second
    worker, red."""
    tq = _fresh(legacy)
    _turn(tq, "s1", "s1-first", ALICE)
    _turn(tq, "s1", "s1-second", ALICE)
    _turn(tq, "s2", "s2-only", ALICE)
    first = tq.get(timeout=0.0, worker_id="w1")
    second = tq.get(timeout=0.0, worker_id="w2")
    assert (first.input_text, second.input_text) == ("s1-first", "s2-only")
    assert tq.get(timeout=0.0, worker_id="w3") is None
    tq.task_done(task=first, worker_id="w1")
    assert tq.get(timeout=0.0, worker_id="w3").input_text == "s1-second"


# ── AC4: priorities and classes as before ────────────────────────────────────────

def test_legacy_priorities_hold():
    """RELOAD_CONFIG (1) before every turn, a compaction (15) after every turn at 10 of
    everybody, also when it arrived first.
    MUTATION: collapse the priorities into one band - the compaction runs before B1, red."""
    tq = _fresh(True)
    _turn(tq, "alice-1", "A1", ALICE)
    _turn(tq, "alice-2", "A2", ALICE)
    tq.add(session_id="bob-1", input_text="__COMPACTION__", priority=15,
           metadata={"compaction": True, "user_scope_id": BOB})
    _turn(tq, "bob-1", "B1", BOB)
    enqueue_system_command("RELOAD_CONFIG", priority=1)
    assert _drain(tq) == ["__CMD__:RELOAD_CONFIG", "A1", "B1", "A2", "__COMPACTION__"]


def test_weighted_class_sequence_is_unchanged():
    """The class sequence of a fixed mix (6 interactive turns of 3 people, a compaction and a
    session command) is the one recorded on the code before this change, literally.
    MUTATION: one band for all classes under weighted_fair - red."""
    tq = _fresh(False)
    for who, scope in (("A", ALICE), ("B", BOB), ("C", CAROL)):
        for i in (1, 2):
            _turn(tq, f"{who}-{i}", f"{who}{i}", scope)
    tq.add(session_id="A-1", input_text="__COMPACTION__", priority=15,
           metadata={"compaction": True, "user_scope_id": ALICE})
    enqueue_system_command("LOAD_SESSION:x", user_scope_id=BOB)
    classes = []
    while True:
        task = tq.get(timeout=0.0, worker_id="w")
        if task is None:
            break
        classes.append(task.task_class)
        tq.task_done(task=task, worker_id="w")
    assert classes == ["interactive", "interactive", "interactive", "interactive",
                       "background", "interactive", "interactive", "background"]
    assert "background" in classes[:6]


@POLICIES
def test_compaction_does_not_take_the_persons_turn(legacy):
    """A person's compaction sits in its own band: it never takes that person's place in
    the round, even queued before the turn.
    MUTATION: collapse the priorities into one band - the compaction goes first, red."""
    tq = _fresh(legacy)
    tq.add(session_id="alice-0", input_text="__COMPACTION__", priority=15,
           metadata={"compaction": True, "user_scope_id": ALICE})
    _turn(tq, "alice-1", "A1", ALICE)
    _turn(tq, "bob-1", "B1", BOB)
    assert _drain(tq) == ["A1", "B1", "__COMPACTION__"]


# ── AC5: the fair key ────────────────────────────────────────────────────────────

def test_fair_key_rules(monkeypatch):
    """The local admin's aliases are one key, a scope-less turn is "default", a session
    command is housekeeping even with its sender's scope.
    MUTATION: key commands on their sender's scope - the order becomes A1, B1, command."""
    import vaf.core.config as config
    monkeypatch.setattr(config, "get_local_admin_scope_id", lambda: "ab12cd34-local-admin")
    tq = _fresh(True)
    admin_uuid = _turn(tq, "a1", "x", "ab12cd34-local-admin")
    admin_word = _turn(tq, "a2", "x", "default")
    scopeless = tq.add(session_id="a3", input_text="x", source="web", metadata={})
    command = enqueue_system_command("LOAD_SESSION:a1", user_scope_id=ALICE)
    assert TaskQueue._fair_key(admin_uuid) == TaskQueue._fair_key(admin_word) == "default"
    assert TaskQueue._fair_key(scopeless) == "default"
    assert TaskQueue._fair_key(command) == HOUSEKEEPING_KEY

    tq = _fresh(True)
    _turn(tq, "alice-1", "A1", ALICE)
    enqueue_system_command("LOAD_SESSION:alice-1", user_scope_id=ALICE)
    _turn(tq, "bob-1", "B1", BOB)
    assert _drain(tq) == ["A1", "__CMD__:LOAD_SESSION:alice-1", "B1"]


# ── AC6: the place report ────────────────────────────────────────────────────────

def _busy(tq):
    """One task in flight on another worker, so what is added next waits."""
    tq.add(session_id="busy-chat", input_text="busy", source="web",
           metadata={"enqueue_user_scope_id": CAROL})
    held = tq.get(timeout=0.0, worker_id="busy")
    assert held is not None
    return held


def _heard():
    got = []
    return got, (lambda info: got.append(info))


@POLICIES
def test_a_waiting_turn_hears_its_place_and_its_start(legacy):
    """Places follow the pick order (not the arrival), move up as tasks ahead are taken, and
    the start is position 0.
    MUTATION: positions by arrival index - B1 hears 3 instead of 2, red."""
    tq = _fresh(legacy)
    _busy(tq)
    a1, on_a1 = _heard()
    a2, on_a2 = _heard()
    b1, on_b1 = _heard()
    _turn(tq, "alice-1", "A1", ALICE, on_wait=on_a1)
    _turn(tq, "alice-2", "A2", ALICE, on_wait=on_a2)
    _turn(tq, "bob-1", "B1", BOB, on_wait=on_b1)
    assert [i.position for i in a1] == [1]
    assert [i.position for i in b1] == [2]
    assert [i.position for i in a2] == [2, 3]   # B1 arrived later and went ahead of it
    assert all(isinstance(i, WaitInfo) and i.resource == "chat" for i in a1 + a2 + b1)
    assert tq.wait_for_session("bob-1").position == 2

    taken = tq.get(timeout=0.0, worker_id="w")
    assert taken.input_text == "A1"
    assert [i.position for i in a1] == [1, 0]
    assert [i.position for i in b1] == [2, 1]
    assert [i.position for i in a2] == [2, 3, 2]
    assert tq.wait_for_session("alice-1") is None


def test_stop_moves_the_others_up():
    """A task dropped by Stop hears nothing more, and those behind it move up.
    MUTATION: no recompute after a drop - C1 stays at 3, red."""
    tq = _fresh(True)
    _busy(tq)
    a, on_a = _heard()
    b, on_b = _heard()
    c, on_c = _heard()
    _turn(tq, "alice-1", "A1", ALICE, on_wait=on_a)
    _turn(tq, "bob-1", "B1", BOB, on_wait=on_b)
    _turn(tq, "carol-1", "C1", CAROL, on_wait=on_c)
    assert tq.drop_queued_tasks_for_session("bob-1") == 1
    assert [i.position for i in b] == [2]
    assert [i.position for i in c] == [3, 2]
    assert [i.position for i in a] == [1]
    tq.get(timeout=0.0, worker_id="w")
    assert [i.position for i in b] == [2]


def test_the_callback_runs_outside_the_lock():
    """A callback that waits on ANOTHER thread taking the queue lock returns (the lock is a
    reentrant Condition, so the same thread would pass either way); an exception in it is
    swallowed.
    MUTATION: call on_wait under _cv - the helper thread blocks, the watchdog says so, red."""
    tq = _fresh(True)
    _busy(tq)
    blocked = []

    def on_wait(info):
        helper = threading.Thread(target=tq.get_queue_size, daemon=True)
        helper.start()
        helper.join(2.0)
        blocked.append(helper.is_alive())

    _turn(tq, "alice-1", "A1", ALICE, on_wait=on_wait)
    tq.get(timeout=0.0, worker_id="w")
    assert blocked and not any(blocked)

    def boom(info):
        raise RuntimeError("a broken display")

    task = _turn(tq, "bob-1", "B1", BOB, on_wait=boom)
    assert task is not None and tq.get(timeout=0.0, worker_id="w2").input_text == "B1"


def test_an_older_place_is_never_delivered_last(monkeypatch):
    """Two threads finish their recomputes in one order and deliver in the other: the older
    place is dropped.
    MUTATION: deliver without the sequence check - the watcher ends on the stale 2, red."""
    tq = _fresh(True)
    _busy(tq)
    heard, on_t = _heard()
    _turn(tq, "alice-1", "T", ALICE, on_wait=on_t)
    assert [i.position for i in heard] == [1]

    real_deliver = tq._deliver
    computed, release = threading.Event(), threading.Event()
    calls = {"n": 0}

    def gated(deliveries):
        calls["n"] += 1
        if calls["n"] == 1:          # thread A: computed T at 2, held before delivering
            computed.set()
            release.wait(5)
        real_deliver(deliveries)

    monkeypatch.setattr(tq, "_deliver", gated)
    # Thread A queues an urgent task ahead of T (T moves to 2) and stalls before delivery.
    a = threading.Thread(target=lambda: tq.add(session_id="urgent", input_text="U",
                                               source="web", priority=1,
                                               metadata={"user_scope_id": BOB}))
    a.start()
    assert computed.wait(5)
    # Thread B (here) takes the urgent task: T is back at 1, computed AND delivered first.
    assert tq.get(timeout=0.0, worker_id="w").input_text == "U"
    release.set()
    a.join(5)
    assert [i.position for i in heard] == [1, 1]
    assert heard[-1].position == 1


# ── AC7: the ETA ─────────────────────────────────────────────────────────────────

def test_eta_needs_three_turns_and_counts_workers():
    """None below three service samples (session commands are none), then
    ceil(position / workers) * p50(service).
    MUTATIONS: count session commands as samples (an ETA after two turns); drop
    ceil(position / workers) (set_workers(2) changes nothing) - red."""
    tq = _fresh(True)
    now = [1000.0]
    tq._clock = lambda: now[0]

    def serve(sid, scope, seconds, command=False):
        if command:
            enqueue_system_command(f"LOAD_SESSION:{sid}")
        else:
            _turn(tq, sid, "t", scope)
        task = tq.get(timeout=0.0, worker_id="w")
        now[0] += seconds
        tq.task_done(task=task, worker_id="w")

    serve("s1", ALICE, 10.0)
    serve("s2", BOB, 10.0)
    serve("cmd", None, 0.01, command=True)
    _busy(tq)
    _turn(tq, "q1", "x", ALICE)
    assert tq.wait_for_session("q1").eta_s is None
    tq.drop_queued_tasks_for_session("q1")
    now[0] += 10.0
    tq.task_done(worker_id="busy")

    serve("s3", CAROL, 10.0)
    _busy(tq)
    for i, scope in enumerate((ALICE, BOB, CAROL), start=1):
        _turn(tq, f"q{i}", "x", scope)
    assert [tq.wait_for_session(f"q{i}").eta_s for i in (1, 2, 3)] == [10.0, 20.0, 30.0]
    tq.set_workers(2)
    assert [tq.wait_for_session(f"q{i}").eta_s for i in (1, 2, 3)] == [10.0, 10.0, 20.0]


# ── AC8: the numbers ─────────────────────────────────────────────────────────────

def test_metrics_shape_and_no_scopes():
    """The ResourceQueue.metrics() shape, name "chat", slots = workers, never a refusal, the
    classes, and no scope or session anywhere.
    MUTATIONS: add the waiting keys; drop `refused` - red."""
    from vaf.core.fair_queue import ResourceQueue
    tq = _fresh(False)
    tq.set_workers(2)
    _busy(tq)
    _turn(tq, "alice-1", "A1", ALICE)
    _turn(tq, "bob-1", "B1", BOB)
    tq.add(session_id="alice-1", input_text="__COMPACTION__", priority=15,
           metadata={"compaction": True, "user_scope_id": ALICE})
    enqueue_system_command("LOAD_SESSION:alice-1", user_scope_id=ALICE)
    m = tq.metrics()
    shape = set(ResourceQueue("probe").metrics())
    assert set(m) == shape | {"by_class", "policy"}
    assert m["name"] == "chat" and m["slots"] == 2 and m["running"] == 1
    assert m["waiting"] == 4 and m["waiting_users"] == 2
    assert m["refused"] == {"timeout": 0, "full": 0, "cancelled": 0}
    assert m["last_refusal_age_s"] is None
    assert m["by_class"] == {"interactive": 2, "automation": 0, "background": 2}
    assert m["policy"] == "weighted_fair"
    text = json.dumps(m)
    for secret in (ALICE, BOB, CAROL, "alice-1", "bob-1", "busy-chat"):
        assert secret not in text
