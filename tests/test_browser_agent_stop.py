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
