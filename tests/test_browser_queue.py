# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The browser agent's runs wait for their browser in the fair queue.

One queue per browser container (``browser:<container>``): runs take turns by person,
one person has at most two runs waiting, a run that gives up says where it stood, Stop
ends a run that still waits, and a per-user instance's queue (named after a scope
hash) never reaches the numbers. ``_run_browser`` and the browser hooks are stubbed, so
these tests drive the real ``run()`` through the real queue with no browser at all.
"""
import ast
import inspect
import re
import textwrap
import threading
import time

import pytest

import vaf.core.browser_interactive as bi
import vaf.core.fair_queue as fair_queue
import vaf.tools.browser_agent as ba

POOLED = "vaf-browser-u-0123456789ab"


class _Endpoint:
    def __init__(self, name):
        self.container_name = name
        self.cdp_base = "http://127.0.0.1:49222"


@pytest.fixture
def rig(monkeypatch):
    """The real run() and queue, with the browser behind them replaced: the pool
    resolves to the shared browser (or the pooled instance set in ``pool``), the hooks
    are counted, and a stubbed _run_browser records who drove and waits for its event."""
    import vaf.core.browser_pool as bp
    import vaf.core.subagent_ipc as ipc_mod
    from vaf.core.task_queue import TaskQueue

    monkeypatch.setattr(fair_queue, "_registry", {})
    monkeypatch.setattr(ba, "_BROWSER_MAX_PARALLEL", 1)
    TaskQueue._instance = None  # type: ignore[attr-defined]
    monkeypatch.delenv("VAF_SPAWN_BROWSER_SUBAGENT", raising=False)
    state = {"pool": None, "order": [], "hold": {}, "entered": {}, "abandoned": [],
             "ended": [], "sids": {}}

    class _Pool:
        def resolve(self, scope):
            return _Endpoint(state["pool"]) if state["pool"] else None

    monkeypatch.setattr(bp, "get_browser_pool", lambda: _Pool())
    monkeypatch.setattr(bi, "register_run_browser", lambda endpoint: None)
    monkeypatch.setattr(bi, "stop_for_agent_run", lambda container_name=None: None)
    monkeypatch.setattr(bi, "agent_run_abandoned",
                        lambda container_name=None: state["abandoned"].append(
                            threading.current_thread().name))
    monkeypatch.setattr(bi, "agent_run_ended",
                        lambda notify=True, container_name=None: state["ended"].append(
                            threading.current_thread().name))
    monkeypatch.setattr(ipc_mod, "get_current_session_id",
                        lambda: state["sids"].get(threading.current_thread().name))

    async def _drive(self, task, **kwargs):
        state["order"].append(task)
        state["entered"].setdefault(task, threading.Event()).set()
        hold = state["hold"].get(task)
        if hold is not None:
            hold.wait(10)
        return f"done {task}"

    monkeypatch.setattr(ba.BrowserAgentTool, "_run_browser", _drive)
    state["results"] = {}
    state["threads"] = []

    def start(task, scope, sid=None, hold=False):
        if hold:
            state["hold"][task] = threading.Event()
        name = f"run-{task}"
        if sid:
            state["sids"][name] = sid

        def go():
            state["results"][task] = ba.BrowserAgentTool().run(task=task, user_scope_id=scope)

        t = threading.Thread(target=go, name=name, daemon=True)
        state["threads"].append(t)
        t.start()
        return t

    state["start"] = start
    yield state
    for ev in state["hold"].values():
        ev.set()
    for t in state["threads"]:
        t.join(5)
    TaskQueue._instance = None  # type: ignore[attr-defined]


def _wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _waiting(name="vaf-browser"):
    return ba._browser_queue(name).metrics()["waiting"]


def _queue_up(rig, task, scope, **kwargs):
    """Start a run and return once it waits in the queue."""
    before = _waiting()
    rig["start"](task, scope, **kwargs)
    assert _wait_until(lambda: _waiting() == before + 1), f"{task} never queued"


def test_the_semaphores_are_gone():
    """The private per-container semaphores are deleted, and the queue is registered
    in exactly one place. MUTATION: reintroduce a threading.Semaphore gate - red."""
    src = inspect.getsource(ba)
    for gone in ("_BROWSER_SEMAPHORE", "_BROWSER_SEMAPHORES", "_BROWSER_SEM_LOCK",
                 "_browser_semaphore", "_BROWSER_QUEUE_TIMEOUT", "threading.Semaphore"):
        assert gone not in src, gone
    assert src.count("resource_queue(") == 1


def test_every_call_for_a_browser_passes_the_same_numbers(monkeypatch):
    """The registry refuses a second registration with other numbers, so every call
    for one container must ask for the same ones. MUTATION: let a pooled call ask for
    another slot count the second time - red (ValueError)."""
    monkeypatch.setattr(fair_queue, "_registry", {})
    shared = ba._browser_queue("vaf-browser")
    assert ba._browser_queue("vaf-browser") is shared
    pooled = ba._browser_queue(POOLED)
    assert ba._browser_queue(POOLED) is pooled
    assert shared is not pooled
    assert shared.listed is True and pooled.listed is False
    assert shared.max_waiting == ba.BROWSER_MAX_WAITING
    assert shared.max_waiting_per_key == ba.BROWSER_MAX_WAITING_PER_KEY


def test_runs_on_one_browser_take_turns_by_person(rig):
    """A1 drives the shared browser; A2, A3 and B1 wait in that order. B1 drives
    before A3: the second person does not wait behind the first person's backlog.
    MUTATION: key the slot on a constant (order A2, A3, B1) - red."""
    rig["start"]("A1", "scope-a", hold=True)
    assert rig["entered"].setdefault("A1", threading.Event()).wait(5)
    _queue_up(rig, "A2", "scope-a")
    _queue_up(rig, "A3", "scope-a")
    _queue_up(rig, "B1", "scope-b")
    rig["hold"]["A1"].set()
    for t in rig["threads"]:
        t.join(5)
    assert rig["order"] == ["A1", "A2", "B1", "A3"], rig["order"]
    assert all(rig["results"][k] == f"done {k}" for k in ("A1", "A2", "A3", "B1"))


def test_a_persons_share_of_the_browser_line(rig):
    """With A1 driving and A2, A3 waiting, A's next run is told at once that their
    own runs already wait; B is still admitted to wait. MUTATION: drop
    max_waiting_per_key from _browser_queue - red."""
    rig["start"]("A1", "scope-a", hold=True)
    assert rig["entered"].setdefault("A1", threading.Event()).wait(5)
    _queue_up(rig, "A2", "scope-a")
    _queue_up(rig, "A3", "scope-a")
    started = time.monotonic()
    out = ba.BrowserAgentTool().run(task="A4", user_scope_id="scope-a")
    assert time.monotonic() - started < 2
    assert out.startswith("Browser agent is busy"), out
    assert "2 of this person's browser runs are already waiting" in out, out
    assert rig["abandoned"] == [threading.current_thread().name]
    _queue_up(rig, "B1", "scope-b")
    rig["hold"]["A1"].set()
    for t in rig["threads"]:
        t.join(5)
    assert sorted(rig["order"]) == ["A1", "A2", "A3", "B1"]


def test_a_timed_out_run_says_where_it_stood(rig, monkeypatch):
    """A run that cannot get its browser in time says so with its place and its wait,
    gives its count back and does not end the run ahead. MUTATIONS: drop the place
    from the text (red); call agent_run_ended on a refusal (red)."""
    monkeypatch.setattr(ba, "BROWSER_WAIT_S", 0.3)
    rig["start"]("A1", "scope-a", hold=True)
    assert rig["entered"].setdefault("A1", threading.Event()).wait(5)
    out = ba.BrowserAgentTool().run(task="B1", user_scope_id="scope-b")
    assert out.startswith("Browser agent is busy"), out
    assert "place 1" in out, out
    assert re.search(r"waited 0\.\d s", out), out
    me = threading.current_thread().name
    assert rig["abandoned"] == [me]
    assert me not in rig["ended"]
    assert "B1" not in rig["order"]
    assert ba._browser_queue("vaf-browser").metrics()["refused"]["timeout"] == 1


def _string_constants(fn):
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_no_em_dash_in_the_run_answers():
    """Every literal run() and the busy text can return is free of the em dash.
    MUTATION: restore the old em-dash busy literal - red."""
    for fn in (ba.BrowserAgentTool.run, ba._busy_text):
        for text in _string_constants(fn):
            assert "\u2014" not in text, text


def test_stop_ends_a_waiting_run(rig):
    """Stop pressed while the run still waits for its browser ends the wait within a
    poll, before the run drives anything. MUTATION: pass no `cancelled` - red."""
    from vaf.core.task_queue import TaskQueue

    rig["start"]("A1", "scope-a", hold=True)
    assert rig["entered"].setdefault("A1", threading.Event()).wait(5)
    _queue_up(rig, "B1", "scope-b", sid="sess-b")
    pressed = time.monotonic()
    TaskQueue().request_stop("sess-b")
    assert _wait_until(lambda: "B1" in rig["results"], timeout=5)
    assert time.monotonic() - pressed < 1.0
    assert rig["results"]["B1"] == "Browser task stopped by user."
    assert "B1" not in rig["order"]
    assert rig["abandoned"] == ["run-B1"]
    assert ba._browser_queue("vaf-browser").metrics()["refused"]["cancelled"] == 1


def test_stop_reaches_only_its_own_session(rig):
    """Two runs wait for two chats; Stop in one chat ends only that chat's run.
    MUTATION: let the wait ask whether ANY session pressed Stop - red."""
    from vaf.core.task_queue import TaskQueue

    rig["start"]("A1", "scope-a", hold=True)
    assert rig["entered"].setdefault("A1", threading.Event()).wait(5)
    _queue_up(rig, "B1", "scope-b", sid="sess-b")
    _queue_up(rig, "C1", "scope-c", sid="sess-c")
    TaskQueue().request_stop("sess-b")
    assert _wait_until(lambda: "B1" in rig["results"], timeout=5)
    time.sleep(0.6)
    assert "C1" not in rig["results"], rig["results"].get("C1")
    assert _waiting() == 1
    rig["hold"]["A1"].set()
    assert _wait_until(lambda: "C1" in rig["results"], timeout=5)
    assert rig["results"]["C1"] == "done C1"


def test_a_pooled_queue_never_shows_its_name(rig, monkeypatch):
    """A per-user instance's queue is named after a scope hash: it is left out of the
    numbers, and its busy text names no container. MUTATIONS: list the pooled queues
    (red); put str(busy) into the busy text (red)."""
    rig["start"]("S1", "scope-a")
    for t in rig["threads"]:
        t.join(5)
    rig["pool"] = POOLED
    rig["start"]("P1", "scope-x", hold=True)
    assert rig["entered"].setdefault("P1", threading.Event()).wait(5)
    monkeypatch.setattr(ba, "BROWSER_WAIT_S", 0.2)
    out = ba.BrowserAgentTool().run(task="P2", user_scope_id="scope-x")
    assert out.startswith("Browser agent is busy"), out
    assert "vaf-browser-u-" not in out and "browser:" not in out, out
    names = [m["name"] for m in fair_queue.queue_metrics()]
    assert "browser:vaf-browser" in names, names
    assert not [n for n in names if "vaf-browser-u-" in n], names


def test_a_run_inside_a_run_does_not_deadlock(rig):
    """A thread that already holds this browser's slot cannot start another run on it:
    the answer comes at once, the count goes back, and nothing waits on itself.
    MUTATION: let the RuntimeError escape run() - red."""
    outcome = {}

    def inner():
        try:
            with ba._browser_queue("vaf-browser").slot("scope-a"):
                outcome["out"] = ba.BrowserAgentTool().run(task="nested",
                                                           user_scope_id="scope-a")
        except BaseException as e:  # noqa: BLE001 - the test reports what escaped
            outcome["raised"] = e

    t = threading.Thread(target=inner, name="run-outer", daemon=True)
    t.start()
    t.join(2)
    assert not t.is_alive(), "a run inside a run waited on itself"
    assert "raised" not in outcome, outcome.get("raised")
    assert outcome["out"].startswith("Error (browser_agent): this run already drives"), outcome
    assert rig["abandoned"] == ["run-outer"] and rig["ended"] == []
    assert "nested" not in rig["order"]


def test_health_shows_the_shared_browser_and_the_preview(rig, monkeypatch):
    """`vaf health` and the admin endpoint read queue_metrics(): a turned-away browser
    run and a turned-away preview degrade the queues row with a line each, and a
    pooled browser's queue never shows. MUTATION: register the preview queue unlisted
    - red."""
    from vaf.core import health
    from vaf.core import web_server as ws

    monkeypatch.setattr(ba, "BROWSER_WAIT_S", 0.2)
    rig["start"]("A1", "scope-a", hold=True)
    assert rig["entered"].setdefault("A1", threading.Event()).wait(5)
    assert ba.BrowserAgentTool().run(task="B1", user_scope_id="scope-b").startswith(
        "Browser agent is busy")
    with ba._browser_queue(POOLED).slot("scope-x"):
        pass

    preview = ws._gotenberg_queue()
    holding, let_go = threading.Event(), threading.Event()

    def hold():
        with preview.slot("scope-a"):
            holding.set()
            let_go.wait(5)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert holding.wait(5)
    with pytest.raises(fair_queue.QueueBusy):
        with preview.slot("scope-b", deadline_s=0.2):
            pass
    let_go.set()
    t.join(5)

    check = health.queues_check(fair_queue.queue_metrics())
    assert check["status"] == health.DEGRADED, check
    assert "gotenberg:" in check["reason"] and "browser:vaf-browser:" in check["reason"], check
    assert "vaf-browser-u-" not in repr(check), check
