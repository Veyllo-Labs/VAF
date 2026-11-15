# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression test for browser_agent stop handling.

The in-process browser run is only stoppable via _stop_monitor. The old monitor
called agent_task.cancel() once and returned — a cancel that lands during a
blocking LLM call (run_in_executor) or that browser-use swallows mid-step left
the run going to max_steps. The monitor must now (1) use browser-use's
cooperative agent.stop() and (2) keep trying until the run actually ends.
"""
import asyncio

from vaf.tools.browser_agent import BrowserAgentTool
from vaf.core.task_queue import TaskQueue


class _FakeAgent:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_stop_monitor_signals_cooperative_stop_and_ends_run():
    async def scenario():
        tq = TaskQueue()
        sid = "browseragent-stop-test"
        tq.clear_stop(sid)
        agent = _FakeAgent()
        done = asyncio.Event()

        async def fake_run():
            # Emulate browser-use: honour the cooperative stop flag at step
            # boundaries; ignore plain cancellation for a while (as a swallowed
            # mid-step CancelledError would).
            for _ in range(500):
                if agent.stopped:
                    return "graceful-stop"
                try:
                    await asyncio.sleep(0.02)
                except asyncio.CancelledError:
                    # swallow once, like browser-use can mid-step
                    continue
            return "ran-to-end"

        agent_task = asyncio.create_task(fake_run())
        monitor = asyncio.create_task(
            BrowserAgentTool._stop_monitor(sid, agent, agent_task, done)
        )

        await asyncio.sleep(0.1)
        tq.request_stop(sid)  # user presses Stop

        try:
            result = await asyncio.wait_for(agent_task, timeout=3.0)
        finally:
            done.set()
            monitor.cancel()
            tq.clear_stop(sid)

        # Cooperative stop must have been signalled, and the run must have ended
        # because of it (not run to max steps).
        assert agent.stopped is True
        assert result == "graceful-stop"

    asyncio.run(scenario())


def test_stop_monitor_noop_without_session_id():
    # No session id → monitor must return immediately and never touch the agent.
    async def scenario():
        agent = _FakeAgent()
        done = asyncio.Event()
        fut = asyncio.get_event_loop().create_future()
        await asyncio.wait_for(
            BrowserAgentTool._stop_monitor(None, agent, fut, done), timeout=1.0
        )
        assert agent.stopped is False

    asyncio.run(scenario())


def _stub_browser_use(monkeypatch):
    """browser_use is an optional package the suite must not require: the tool
    imports it inside _run_browser, so sys.modules stubs are enough for tests
    that never reach a real browser."""
    import sys
    import types

    bu = types.ModuleType("browser_use")
    bu.Agent = object
    session_mod = types.ModuleType("browser_use.browser.session")
    session_mod.BrowserSession = object
    profile_mod = types.ModuleType("browser_use.browser.profile")
    profile_mod.BrowserProfile = object
    browser_pkg = types.ModuleType("browser_use.browser")
    for name, mod in (("browser_use", bu),
                      ("browser_use.browser", browser_pkg),
                      ("browser_use.browser.session", session_mod),
                      ("browser_use.browser.profile", profile_mod)):
        monkeypatch.setitem(sys.modules, name, mod)


class _RefusingDevTools:
    """`urllib.request.urlopen` for a browser that is not answering: every call to the
    DevTools version endpoint is refused at once, recorded with the thread that made it,
    and no socket is opened (a running vaf-browser on the developer's machine is never
    reached). Other URLs answer an empty JSON object."""

    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.calls = 0
        self.threads = set()

    def __call__(self, request, timeout=None):
        import threading
        import urllib.error
        url = request if isinstance(request, str) else request.full_url
        if not url.endswith("/json/version"):
            class _Empty:
                def read(self): return b"{}"
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return _Empty()
        with self.lock:
            self.calls += 1
            self.threads.add(threading.current_thread())
        raise urllib.error.URLError("refused")


def _run_and_stop(monkeypatch, sid):
    """Start a browser run against a browser that is not answering and press Stop after
    0.8 s. Returns (result, seconds the run took, the fake DevTools)."""
    import threading
    import time as _time
    import urllib.request as _req

    # session_context, NOT set_current_session_id: a bare set(None) on the way
    # out DECLARES "no session" on pytest's main thread, and that declaration
    # deliberately beats the VAF_SESSION_ID env fallback - it broke the
    # workspace-resolver tests that run later in the same process. Only the
    # ContextVar token can put "never told" back.
    from vaf.core.subagent_ipc import session_context

    _stub_browser_use(monkeypatch)
    devtools = _RefusingDevTools()
    monkeypatch.setattr(_req, "urlopen", devtools)
    # Not lowered to hide anything: the fixed product gives up within 0.1 s of the
    # stop either way. 8 s only bounds what a MUTATED run leaves behind.
    monkeypatch.setenv("VAF_BROWSER_CDP_WAIT_S", "8")

    tq = TaskQueue()
    tq.clear_stop(sid)
    try:
        with session_context(sid):
            def _press_stop():
                _time.sleep(0.8)
                tq.request_stop(sid)

            threading.Thread(target=_press_stop, daemon=True).start()
            t0 = _time.monotonic()
            result = BrowserAgentTool().run(task="probe")
            took = _time.monotonic() - t0
    finally:
        tq.clear_stop(sid)
    return result, took, devtools


def _assert_no_worker_keeps_dialing(devtools, join_s):
    import time as _time

    assert devtools.threads, "the run never waited for the browser's DevTools port"
    for worker in devtools.threads:
        worker.join(join_s)
    alive = [w.name for w in devtools.threads if w.is_alive()]
    assert not alive, (
        f"the stopped run left {alive} dialing the DevTools port; it ends only at the "
        f"CDP deadline, holding the browser for the next run and the browser window")
    calls = devtools.calls
    _time.sleep(1.0)
    assert devtools.calls == calls, (
        f"{devtools.calls - calls} DevTools connects after the stopped run had ended")


def test_stop_ends_a_run_hung_in_browser_startup(monkeypatch):
    """MUTATION 1: resolve the session id inside the browser thread again.
    MUTATION 2: start the stop watchdog only after the browser is up.
    MUTATION 3: drop the cancel checks from resolve_cdp_ws_url's retry loop.
    MUTATION 4: drop the cancel_when around hand_jar_to_run in _run_browser.

    The live incident: the run hung inside startup, produced nothing, and ten
    Stop presses did nothing. Two defects stacked - the run executes on a fresh
    thread whose contextvar context answers None for the session id, which
    disarmed every stop lane; and the only stop lane was a coroutine created
    AFTER the startup phase that was hanging. The session id must cross the
    thread boundary as an argument, and the watchdog thread must be armed
    before anything can block, so a stop during startup cancels the run task.

    Where the run really hangs is the jar handover: it waits in an executor
    thread for a browser that is not answering, and the stop does not wait for
    that thread. Measured before the fix: the thread kept dialing the DevTools
    port for 29.1 s after the run had returned and held the handover mutex as
    long, so the browser window and the next run waited too. It must end with
    the run.
    """
    from vaf.core.browser_interactive import _manager_for_run

    result, took, devtools = _run_and_stop(monkeypatch, "browseragent-hangstop-test")

    assert result == "Browser task stopped by user.", result
    assert took < 10.0, f"stop took {took:.1f}s - the watchdog never fired"
    mutex = _manager_for_run(None)._handover_mutex
    assert mutex.acquire(timeout=1.0), "the stopped run's handover still holds the browser"
    mutex.release()
    _assert_no_worker_keeps_dialing(devtools, join_s=3.0)


def test_stop_ends_the_runs_own_cdp_wait(monkeypatch):
    """MUTATION: drop the cancel_when around _resolve_cdp_url in _run_browser.

    Past the handover the run resolves the browser's DevTools URL itself, in an
    executor thread the stop does not wait for either: the same leftover, a
    thread dialing a browser that is not answering until the CDP deadline.
    """
    import vaf.core.browser_interactive as bi

    monkeypatch.setattr(bi, "hand_jar_to_run", lambda **kwargs: None)
    # Past the handover the run grants its window a live stream (a push to the web
    # app's port) and mirrors the workspace (docker): neither is what this pins.
    monkeypatch.setattr(bi, "agent_stream_started", lambda *a, **k: None)
    monkeypatch.setattr(bi, "sync_workspace_for_run", lambda **kwargs: [])
    result, took, devtools = _run_and_stop(monkeypatch, "browseragent-ownwait-test")

    assert result == "Browser task stopped by user.", result
    assert took < 10.0, f"stop took {took:.1f}s - the watchdog never fired"
    _assert_no_worker_keeps_dialing(devtools, join_s=1.0)


def _another_run_ends_while_this_one_waits(monkeypatch, sid):
    """Run A of another account drives the same browser in parallel and ends
    normally while run B (this test's, session `sid`) waits for the browser's
    DevTools port. A is its run hooks around B's wait: the eviction at its start
    and agent_run_ended at its end, which ends A's ticket and no other. B must go
    on waiting; then B is stopped and ends."""
    import threading
    import time as _time
    import urllib.request as _req

    import vaf.core.browser_interactive as bi
    from vaf.core.subagent_ipc import session_context

    _stub_browser_use(monkeypatch)
    devtools = _RefusingDevTools()
    monkeypatch.setattr(_req, "urlopen", devtools)
    monkeypatch.setenv("VAF_BROWSER_CDP_WAIT_S", "8")
    tq = TaskQueue()
    tq.clear_stop(sid)
    box = {}

    def _run_b():
        with session_context(sid):
            box["result"] = BrowserAgentTool().run(task="probe")

    bi.stop_for_agent_run()                     # A starts first
    run_b = threading.Thread(target=_run_b, daemon=True)
    try:
        run_b.start()
        deadline = _time.monotonic() + 5.0
        while devtools.calls < 2 and _time.monotonic() < deadline:
            _time.sleep(0.05)
        assert devtools.calls >= 2, "run B never waited for the browser"
        bi.agent_run_ended()                    # A ends normally
        at_a_end = devtools.calls
        # B's next dial comes one backoff pause (1 s) after its second one, so a fixed 1 s
        # sleep here raced it on a slow runner: wait for the dial, or for B to end.
        deadline = _time.monotonic() + 4.0
        while (devtools.calls <= at_a_end and run_b.is_alive()
               and _time.monotonic() < deadline):
            _time.sleep(0.05)
        assert run_b.is_alive(), f"run B ended when run A did: {box.get('result')}"
        assert devtools.calls > at_a_end, "run B stopped waiting when run A ended"
        tq.request_stop(sid)                    # now B is stopped
        run_b.join(5.0)
    finally:
        tq.clear_stop(sid)
    assert not run_b.is_alive(), "the stop did not end run B"
    assert box.get("result") == "Browser task stopped by user.", box.get("result")
    _assert_no_worker_keeps_dialing(devtools, join_s=3.0)


def test_another_runs_end_does_not_cut_this_runs_handover(monkeypatch):
    """MUTATION: arm the handover with "a run of this browser has ended" instead of
    this run's own end: with VAF_BROWSER_MAX_PARALLEL above 1 one run's normal end
    then cuts another run's handover, which refuses the run."""
    _another_run_ends_while_this_one_waits(monkeypatch, "browseragent-parallel-handover")


def test_another_runs_end_does_not_cut_this_runs_own_cdp_wait(monkeypatch):
    """MUTATION: arm the run's own CDP resolve with "a run of this browser has ended"
    instead of this run's own end."""
    import vaf.core.browser_interactive as bi

    monkeypatch.setattr(bi, "hand_jar_to_run", lambda **kwargs: None)
    monkeypatch.setattr(bi, "agent_stream_started", lambda *a, **k: None)
    monkeypatch.setattr(bi, "sync_workspace_for_run", lambda **kwargs: [])
    _another_run_ends_while_this_one_waits(monkeypatch, "browseragent-parallel-resolve")


def test_a_finished_run_parks_the_browser_on_a_blank_tab(monkeypatch):
    """MUTATION: stop parking the browser when a run ends.

    Closing the browser-use session only drops OUR connection; the container's
    Chromium keeps every tab as the run left it. Measured live: one visit to an
    animated page left vaf-browser at 1027% CPU - ten cores - minutes after the
    agent had finished and reported.

    THE MECHANISM CHANGED, THE INVARIANTS DID NOT. Parking used to open a fresh
    blank tab and then close every busy page. It cannot any more: the streamed
    browser window is launched in app mode (no tab strip, no toolbar) and only
    that first window is one - a tab created through CDP comes up as an ordinary
    browser window with all its chrome. The old order therefore closed the app
    window along with the busy pages and left the ordinary one behind, which
    showed a whole browser UI inside the streamed window. The surviving page is
    EMPTIED by navigating it instead. What this pins is what it always meant to:
    no page is left rendering, and the browser is never left with zero pages.
    """
    calls = []

    class _Resp:
        def __init__(self, body): self._b = body
        def read(self): return self._b.encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import urllib.request as _req

    def _fake_urlopen(request, timeout=None):
        calls.append((request.get_method(), request.full_url))
        if request.full_url.endswith("/json/list"):
            return _Resp('[{"type": "page", "id": "t1", "url": "https://busy.example/anim",'
                         '  "webSocketDebuggerUrl": "ws://stub/t1"},'
                         ' {"type": "page", "id": "t2", "url": "about:blank",'
                         '  "webSocketDebuggerUrl": "ws://stub/t2"}]')
        return _Resp("{}")

    emptied = []
    import vaf.core.browser_interactive as _bi
    monkeypatch.setattr(_bi, "_navigate_blank", lambda ws: emptied.append(ws))
    monkeypatch.setattr(_req, "urlopen", _fake_urlopen)
    BrowserAgentTool._park_browser_idle("http://localhost:9222")

    urls = [u for _, u in calls]
    closed = [u.rsplit("/", 1)[-1] for u in urls if "/json/close/" in u]
    assert closed == ["t2"], f"exactly the extra page must be closed, closed={closed}"
    assert not any("/json/new" in u for u in urls), (
        "parking must not create a tab: a CDP-made tab is an ordinary browser window, "
        "and creating one is what used to cost the app window")
    assert emptied == ["ws://stub/t1"], (
        f"the surviving page must be emptied by navigating, emptied={emptied}")


def test_the_run_parks_the_browser_when_it_ends():
    """MUTATION: drop the park call from the run's finally.

    The helper is worthless if nothing calls it when a run ends - and the end
    is exactly where the CPU burn starts."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vaf" / "tools"
           / "browser_agent.py").read_text(encoding="utf-8")
    # The RUN's finally, anchored on the teardown only it does - the file has
    # three finally blocks and the other two are the semaphore and the loop.
    teardown = src.split("stop_screenshots.set()", 1)[1][:3000]
    assert "_park_browser_idle" in teardown, (
        "a finished run no longer parks the shared browser")
    assert teardown.index("browser.stop()") < teardown.index("_park_browser_idle"), (
        "parking must happen after the session is stopped, not instead of it")


def test_watchdog_kills_the_container_when_the_loop_is_starved():
    """MUTATION: drop the container-kill escalation from the watchdog.

    A synchronous block inside browser-use/CDP starves the whole private event
    loop: no coroutine ticks, so a scheduled cancel never lands and the
    cooperative monitor is as frozen as the run. The only lever left is
    severing the blocked resource itself - the watchdog restarts the browser
    container after its grace period, the sync read dies, the loop revives,
    and the pending cancel finally lands.
    """
    import threading
    import time as _time

    sid = "browseragent-starved-test"
    tq = TaskQueue()
    tq.clear_stop(sid)
    severed = threading.Event()

    async def scenario():
        async def starving_run():
            # The analogue of a blocking CDP read: a sync wait inside the
            # coroutine, which freezes every other task on this loop until
            # the "socket" (the event) dies. The await after it is where the
            # revived loop delivers the cancel the watchdog kept scheduling -
            # real browser-use code awaits again the moment the read errors.
            # The deadline exists for the MUTATED run: without the escalation
            # nothing ever severs the block, and the test must fail in seconds
            # rather than starve the whole suite.
            deadline = _time.monotonic() + 6.0
            while not severed.is_set() and _time.monotonic() < deadline:
                _time.sleep(0.05)
            await asyncio.sleep(5)
            return "unblocked"

        outer = asyncio.current_task()
        run_ended = threading.Event()
        watchdog = threading.Thread(
            target=BrowserAgentTool._stop_watchdog,
            args=(sid, asyncio.get_running_loop(), outer, run_ended,
                  severed.set),
            kwargs={"grace_s": 0.6},
            daemon=True,
        )
        watchdog.start()
        tq.request_stop(sid)
        try:
            await starving_run()
            raise AssertionError("the starved run was never cancelled")
        finally:
            run_ended.set()

    t0 = _time.monotonic()
    try:
        asyncio.run(scenario())
    except asyncio.CancelledError:
        pass
    finally:
        tq.clear_stop(sid)
    took = _time.monotonic() - t0

    assert severed.is_set(), "the container kill never fired"
    assert took < 8.0, f"unblocking took {took:.1f}s"
