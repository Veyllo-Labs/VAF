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


def test_stop_ends_a_run_hung_in_browser_startup(monkeypatch):
    """MUTATION 1: resolve the session id inside the browser thread again.
    MUTATION 2: start the stop watchdog only after the browser is up.

    The live incident: the run hung inside startup, produced nothing, and ten
    Stop presses did nothing. Two defects stacked - the run executes on a fresh
    thread whose contextvar context answers None for the session id, which
    disarmed every stop lane; and the only stop lane was a coroutine created
    AFTER the startup phase that was hanging. The session id must cross the
    thread boundary as an argument, and the watchdog thread must be armed
    before anything can block, so a stop during startup cancels the run task.
    """
    import threading
    import time as _time

    # session_context, NOT set_current_session_id: a bare set(None) on the way
    # out DECLARES "no session" on pytest's main thread, and that declaration
    # deliberately beats the VAF_SESSION_ID env fallback - it broke the
    # workspace-resolver tests that run later in the same process. Only the
    # ContextVar token can put "never told" back.
    from vaf.core.subagent_ipc import session_context

    _stub_browser_use(monkeypatch)

    hang = threading.Event()

    def _hanging_resolve(_base):
        hang.wait(20.0)
        raise RuntimeError("resolver unblocked without stop")

    monkeypatch.setattr(BrowserAgentTool, "_resolve_cdp_url",
                        staticmethod(_hanging_resolve))

    sid = "browseragent-hangstop-test"
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
        hang.set()
        tq.clear_stop(sid)

    assert result == "Browser task stopped by user.", result
    assert took < 10.0, f"stop took {took:.1f}s - the watchdog never fired"


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
