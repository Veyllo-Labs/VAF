# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A timer set in the terminal app arrives in the terminal app.

MEASURED. `TaskQueue` appeared in `vaf/cli/tui_app/agent_bridge.py` exactly once -
inside `boot_bridge`'s docstring, listing the queue watcher as a deliberate
omission. So a fired timer enqueued an `AgentTask` that nothing in that process
ever popped, and it died with the process. In a plain terminal run that queue has
exactly ONE producer: `vaf/core/timers.py:_fire`. Every other producer (the three
messaging bridges, the web server's five sites, the headless self-enqueue) lives
in a process the app lane never shares, because an explicit `--web` routes to the
modern lane instead.

THE FOUR RULES PINNED HERE, because each is silent when broken:

1. "My session" is `self.session.id`, NEVER `get_current_session_id()`. The lane
   thread was never told which session it serves: the context defaults to a
   sentinel, a fresh thread inherits an empty context, and no lane writes the
   environment variable any more. That call answers None here, so a guard built on
   it would drop EVERY timer - the original bug with better wording.
2. Never filter on `task.source`. A timer set from this very app carries "web",
   because the timer tool falls back to it and nothing under `vaf/cli` sets the
   chat source.
3. The turn runs INLINE on the lane thread. `_submit`'s wrapper clears `_busy` in
   its finally the moment the drain returns, so a re-submitted turn would leave
   the flag clear while the claim is still held - and the next tick's `get()`
   releases this worker's previous claim and can pop a second task for the same
   session. That release is a repair for consumers that forget `task_done()`;
   leaning on it here would turn it into a way to run two turns at once.
4. `task_done()` in a `finally`, not left to that self-heal. After the lane takes
   its stop sentinel there is no next `get()` for this key.

WHAT IS DELIBERATELY NOT BUILT. No `__CMD__` handling: all four producers of those
tasks are in the web server and cannot reach this process. No `AgentTask.kind`
either - the measured N is three, none of them is this lane, and a "timer" member
would delete nothing because the discriminator is already `metadata["timer"]`.
"""
import threading
import types

import pytest

from vaf.core.task_queue import TaskQueue


@pytest.fixture(autouse=True)
def _fresh_queue():
    """The queue is a process singleton with no per-test teardown of its own."""
    TaskQueue._instance = None  # type: ignore[attr-defined]
    yield
    TaskQueue().reset_runtime_state(include_queued=True)
    TaskQueue._instance = None  # type: ignore[attr-defined]


SESSION = "green123456"
OTHER = "red654321"


class _Events:
    """Records what the lane told the UI.

    The callback set is FIXED, like the real events object and like the recorder
    in tests/test_tui_agent_bridge.py. A stand-in whose __getattr__ answers any
    name would make the optional-callback discipline untestable: a direct call to
    a missing callback would be silently recorded instead of raising.
    """

    KNOWN = {"presence", "event_note", "system_note", "agent_message_start",
             "agent_message_done", "turn_started", "turn_finished", "context"}

    def __init__(self, with_wake: bool = True):
        self.calls = []
        self._names = set(self.KNOWN) | ({"wake_message"} if with_wake else set())

    def __getattr__(self, name):
        if name not in self.__dict__.get("_names", ()):
            raise AttributeError(name)

        def _rec(*args):
            self.calls.append((name, *args))
        return _rec


def _bridge(events=None, session_id=SESSION):
    """A bridge with no lane thread: the drain is driven directly, on this thread."""
    from vaf.cli.tui_app.agent_bridge import AgentBridge

    b = AgentBridge.__new__(AgentBridge)
    b.events = events or _Events()
    b.session = types.SimpleNamespace(id=session_id)
    b._stopping = threading.Event()
    b._busy = False
    b._submitted = []
    b._submit = lambda fn: b._submitted.append(fn)
    b.turns = []
    b._run_turn = lambda text, inline_attachments=True: b.turns.append(
        (text, inline_attachments))
    return b


def _enqueue(session_id=SESSION, text="wake up", timer=True):
    TaskQueue().add(session_id=session_id, input_text=text, source="web",
                    metadata={"timer": True} if timer else {})


# ── rule 1: the lane knows its own session, and asks nobody ──────────────────

def test_a_timer_arrives_on_a_thread_that_was_never_told_its_session(monkeypatch) -> None:
    """The headline. A guard built on get_current_session_id() answers None on
    this thread and would drop every timer."""
    monkeypatch.delenv("VAF_SESSION_ID", raising=False)
    from vaf.core.subagent_ipc import get_current_session_id

    b = _bridge()
    _enqueue()
    done = []
    t = threading.Thread(target=lambda: (b._drain_queue_once(),
                                         done.append(get_current_session_id())))
    t.start(), t.join(timeout=10)

    assert done == [None], "the lane thread was told a session after all - re-check the premise"
    assert b.turns == [("wake up", False)]


def test_the_wake_text_is_not_run_through_the_attachment_inliner() -> None:
    """`@path` is a prompt affordance for text a human typed. A timer's text is
    authored by the model's own set_timer call."""
    b = _bridge()
    _enqueue(text="check @/etc/hostname now")
    b._drain_queue_once()
    assert b.turns == [("check @/etc/hostname now", False)]


# ── rule 2: source is never a filter ─────────────────────────────────────────

def test_a_timer_stamped_web_is_still_delivered_here() -> None:
    """A timer set in THIS app carries source="web" - the tool falls back to it
    and nothing under vaf/cli sets the chat source."""
    b = _bridge()
    TaskQueue().add(session_id=SESSION, input_text="hi", source="web",
                    metadata={"timer": True})
    b._drain_queue_once()
    assert len(b.turns) == 1


# ── the foreign session ──────────────────────────────────────────────────────

def test_a_task_for_another_session_is_dropped_and_said_to_be_gone() -> None:
    """Never swap: the transcript is only ever cleared by the UI thread, so a
    lane-side swap would leave the old conversation under a new session id."""
    ev = _Events()
    b = _bridge(ev)
    _enqueue(session_id=OTHER)
    b._drain_queue_once()

    assert b.turns == []
    notes = [c for c in ev.calls if c[0] == "event_note"]
    assert notes, "a dropped task told the user nothing"
    assert "gone" in notes[0][2] and "deferred" in notes[0][2], (
        "the note must say the timer is gone; it is not queued for later, the "
        "scheduler removed it from its in-memory store before firing"
    )


# ── rule 3 and 4: the claim's lifetime ───────────────────────────────────────

def test_the_turn_runs_inline_and_never_through_the_lane_again() -> None:
    b = _bridge()
    _enqueue()
    b._drain_queue_once()
    assert b._submitted == [], (
        "the turn was re-submitted; _busy would clear while the claim is still "
        "held, and the next tick could pop a second task for this session"
    )


def test_the_claim_is_released_even_when_the_turn_raises() -> None:
    b = _bridge()

    def _boom(text, inline_attachments=True):
        raise RuntimeError("turn failed")

    b._run_turn = _boom
    _enqueue()
    with pytest.raises(RuntimeError):
        b._drain_queue_once()
    assert TaskQueue().is_busy() is False
    assert not TaskQueue()._session_inflight  # type: ignore[attr-defined]


def test_an_empty_queue_costs_one_non_blocking_look() -> None:
    b = _bridge()
    b._drain_queue_once()
    assert b.turns == []


# ── shutdown ─────────────────────────────────────────────────────────────────

def test_nothing_is_claimed_once_stopping_is_set() -> None:
    b = _bridge()
    b._stopping.set()
    _enqueue()
    b.queue_tick()
    b._drain_queue_once()
    assert b._submitted == [] and b.turns == []
    assert TaskQueue().get_queue_size() == 1, "the task was claimed during teardown"


def test_a_tick_already_on_the_lane_stops_before_it_turns() -> None:
    """The real window: the lane is FIFO and returns only on its sentinel, so a
    closure queued just before the app exited still runs during teardown."""
    b = _bridge()
    _enqueue()
    b._stopping.set()
    b._drain_queue_once()
    assert b.turns == []


def test_a_busy_lane_is_not_given_more_queue_work() -> None:
    b = _bridge()
    b._busy = True
    b.queue_tick()
    assert b._submitted == []


# ── the wake card ────────────────────────────────────────────────────────────

def test_the_wake_card_precedes_the_turn_and_only_for_a_timer() -> None:
    ev = _Events()
    b = _bridge(ev)
    order = []
    b._run_turn = lambda text, inline_attachments=True: order.append("turn")
    ev.wake_message = lambda text, kind: order.append("wake")
    _enqueue()
    b._drain_queue_once()
    assert order == ["wake", "turn"]

    order.clear()
    _enqueue(timer=False)
    b._drain_queue_once()
    assert order == ["turn"], "a task that is not a timer got a wake card"


def test_an_events_object_without_the_wake_callback_survives() -> None:
    """Optional-callback discipline: an events object that predates this stays
    quiet rather than raising into the turn."""
    ev = _Events(with_wake=False)
    b = _bridge(ev)
    from vaf.cli.tui_app.agent_bridge import AgentBridge

    b._emit = types.MethodType(AgentBridge._emit, b)
    _enqueue()
    b._drain_queue_once()
    assert len(b.turns) == 1
    assert not [c for c in ev.calls if c[0] == "event_note" and c[1] == "Error"]


# ── wiring, so the stage is not tested while the wiring is absent ────────────

def test_the_second_interval_is_wired_to_the_queue_tick() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "vaf" / "cli" / "tui_app"
           / "app.py").read_text(encoding="utf-8")
    assert "self.set_interval(1.0, self._bridge.queue_tick)" in src
    assert "self.set_interval(2.5, self._bridge.drain_tick)" in src, (
        "the sub-agent drain must keep its own cadence"
    )


def test_unmount_stops_the_queue_lane_while_the_loop_is_still_alive() -> None:
    """shutdown() runs after app.run() returned, which is too late to stop a
    closure already queued on the lane."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "vaf" / "cli" / "tui_app"
           / "app.py").read_text(encoding="utf-8")
    tail = src.split("def on_unmount", 1)[1][:400]
    assert "begin_stopping()" in tail


def test_the_dead_timer_marker_is_gone_from_the_tree() -> None:
    """It was documented as the delivery path and produced by nobody; two
    consumers still stripped its prefix."""
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    out = subprocess.run(["git", "grep", "-l", "TIMER_MSG_PREFIX"], cwd=str(root),
                         capture_output=True, text=True)
    assert out.stdout.strip() == "", f"still present in: {out.stdout}"
