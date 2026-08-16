# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Two halves of one evening: a worker that hung for three hours with no trace,
and a room answer that evaporated because it came as plain text.

The stall reporter exists because the incident's root cause is UNKNOWABLE: the
metrics printed inflight_total=1 from 19:34 to shutdown, every queue count read
zero, and the stuck thread's stack died with the process. The reporter turns
the next such hang into a named line of code while it can still be named.

The stray-answer fallback exists because the same evening's "the agent is dead"
was, in one case, an agent that HAD answered: a finished 264-character reply,
zero tool calls, silently discarded because room turns only deliver through
room_send.
"""
import time
from types import SimpleNamespace

import pytest

import vaf.core.headless_runner as hr
from vaf.core.task_queue import TaskQueue


@pytest.fixture
def queue():
    tq = TaskQueue()
    tq.reset_runtime_state(include_queued=True)
    yield tq
    tq.reset_runtime_state(include_queued=True)


def _enqueue(queue, text="__CMD__:LOAD_SESSION:ab12cd34", session="system"):
    queue.add(session_id=session, input_text=text)


# ── the stall probe ──────────────────────────────────────────────────────────

def test_a_long_held_task_is_reported_as_stalled(queue, monkeypatch):
    _enqueue(queue)
    got = queue.get(timeout=0.1)
    assert got is not None
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 600)
    stalls = queue.stalled_tasks(threshold_s=300)
    assert len(stalls) == 1
    assert stalls[0]["age_s"] >= 600
    assert "LOAD_SESSION" in stalls[0]["preview"]


def test_a_finished_task_never_reads_as_stalled(queue, monkeypatch):
    _enqueue(queue)
    got = queue.get(timeout=0.1)
    queue.task_done(task=got)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 600)
    assert queue.stalled_tasks(threshold_s=300) == []


def test_a_young_task_is_left_alone(queue):
    _enqueue(queue)
    queue.get(timeout=0.1)
    assert queue.stalled_tasks(threshold_s=300) == []


# ── the reporter ─────────────────────────────────────────────────────────────

def test_the_reporter_dumps_stacks_once_per_stall(queue, monkeypatch, tmp_path):
    _enqueue(queue)
    queue.get(timeout=0.1)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 600)
    monkeypatch.setattr(hr, "get_dated_log_path",
                        lambda domain, ext: tmp_path / f"{domain}.{ext}")
    lines = []
    monkeypatch.setattr(hr, "append_domain_log_always",
                        lambda domain, line: lines.append(line))

    reported: set = set()
    assert hr.report_stalls(queue, threshold_s=300, reported=reported) == 1
    assert hr.report_stalls(queue, threshold_s=300, reported=reported) == 0, \
        "a three-hour hang must write one report, not one per metrics tick"
    assert any("[STALL]" in line for line in lines)
    dump = (tmp_path / "stall.log").read_text()
    assert "stall report" in dump
    assert "Thread" in dump or "File" in dump, "the stack dump is the whole point"


def test_the_reporter_is_wired_into_the_metrics_tick():
    """A reporter nobody calls reports nothing. Source-checked because the
    metrics loop lives inside a 2700-line function no test can enter."""
    import ast
    from pathlib import Path

    src = (Path(hr.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "report_stalls"
                and not isinstance(node, ast.FunctionDef)):
            return
    raise AssertionError("report_stalls is never called from the runner")


# ── the stray room answer ────────────────────────────────────────────────────

class _Room:
    def __init__(self):
        self.said = []

    def identity_for(self, key):
        return SimpleNamespace(peer_id="p-agent")

    def say(self, identity, text, **kw):
        self.said.append(text)


@pytest.fixture
def room(monkeypatch):
    fake = _Room()
    import vaf.core.a2a.room as room_mod
    monkeypatch.setattr(room_mod.Room, "open", classmethod(lambda cls, rid: fake))
    monkeypatch.setattr(hr, "append_domain_log_always", lambda *a, **k: None)
    return fake


def _agent(history):
    return SimpleNamespace(history=history)


WAKE = {"room_id": "room-x", "scope": "aaaa1111-2222-3333-4444-555555555555"}


def test_a_plain_text_answer_reaches_the_room(room):
    agent = _agent([{"role": "user", "content": "wake"}])
    delivered = hr.deliver_stray_room_answer(agent, WAKE, "here is my answer", 1)
    assert delivered is True
    assert room.said == ["here is my answer"]


def test_an_answer_already_sent_via_room_send_is_not_doubled(room):
    agent = _agent([
        {"role": "user", "content": "wake"},
        {"role": "tool", "name": "room_send", "content": "committed"},
    ])
    delivered = hr.deliver_stray_room_answer(agent, WAKE, "same text", 1)
    assert delivered is False
    assert room.said == []


def test_placeholders_are_not_answers(room):
    agent = _agent([])
    assert hr.deliver_stray_room_answer(agent, WAKE, "...", 0) is False
    assert hr.deliver_stray_room_answer(agent, WAKE, "", 0) is False
    assert hr.deliver_stray_room_answer(agent, WAKE, None, 0) is False
    assert room.said == []


def test_a_room_send_from_an_earlier_turn_does_not_mask_this_one(room):
    """Only the CURRENT turn's history counts: hist_before marks where it began."""
    agent = _agent([
        {"role": "tool", "name": "room_send", "content": "old turn"},
        {"role": "user", "content": "wake"},
    ])
    delivered = hr.deliver_stray_room_answer(agent, WAKE, "new answer", 1)
    assert delivered is True
    assert room.said == ["new answer"]
