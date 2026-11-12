# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A process without a web server stops dialing the local API for every event.

The push fallback (vaf/core/web_interface.py) runs inline for every session event when no
server loop is known, and the CLI heartbeat (vaf/cli/cmd/run.py) posts every few seconds.
On Windows a connect to the closed port costs about 0.5 to 2 s each. After a failed
connect both answer from vaf.core.outage.OutageMemory for a pause instead. These tests
fake the clock and the transport; no port is dialed."""
import threading
from types import SimpleNamespace

import pytest
import requests

from vaf.core import web_interface
from vaf.core.outage import OutageMemory


class _Transport:
    """requests.post that refuses (as a closed port does), answers, or times out reading."""

    def __init__(self):
        self.mode = "refuse"
        self.urls = []

    def post(self, url, **kwargs):
        self.urls.append(url)
        if self.mode == "refuse":
            raise requests.exceptions.ConnectionError("[Errno 111] Connection refused")
        if self.mode == "slow":
            raise requests.exceptions.ReadTimeout("read timed out")
        return SimpleNamespace(status_code=200)


@pytest.fixture()
def api(monkeypatch):
    clock = {"t": 1000.0}
    memory = OutageMemory(web_interface._LOCAL_API_PAUSE_S, clock=lambda: clock["t"])
    monkeypatch.setattr(web_interface, "LOCAL_API_DOWN", memory)
    monkeypatch.setattr(web_interface, "internal_api_base", lambda: "http://127.0.0.1:8001")
    transport = _Transport()
    monkeypatch.setattr(requests, "post", transport.post)
    return SimpleNamespace(clock=clock, memory=memory, transport=transport)


def _push(n=1):
    for _ in range(n):
        # The method reads nothing from its instance; the singleton stays untouched.
        web_interface.WebInterfaceManager._http_fallback_push(
            None, {"type": "agent_message_update", "sessionId": "s1"})


def test_the_memory_pauses_after_a_failure_and_forgets_on_success():
    """MUTATION: is_down ignoring the clock - red; reached() keeping the entry - red."""
    clock = {"t": 0.0}
    memory = OutageMemory(10.0, clock=lambda: clock["t"])
    assert not memory.is_down("k")
    memory.failed("k")
    assert memory.is_down("k")
    clock["t"] = 9.9
    assert memory.is_down("k")
    clock["t"] = 10.0
    assert not memory.is_down("k")
    memory.failed("k")
    memory.reached("k")
    assert not memory.is_down("k")


def test_a_refused_push_is_not_dialed_again_until_the_pause_ends(api):
    """Many events with no server: one dial per pause, not one per event; a server that
    comes up is reached at the first event after the pause.
    MUTATION: drop the is_down check - red (50 dials); drop failed() - red."""
    _push(50)
    assert len(api.transport.urls) == 1
    assert api.transport.urls[0] == "http://127.0.0.1:8001/api/subagent/stream"
    api.clock["t"] += web_interface._LOCAL_API_PAUSE_S - 0.1
    _push(10)
    assert len(api.transport.urls) == 1
    api.transport.mode = "ok"
    api.clock["t"] += 0.2
    _push(3)
    assert len(api.transport.urls) == 4


def test_a_successful_push_forgets_the_outage(api):
    """MUTATION: drop reached() after a successful post - red (the old entry stays)."""
    api.memory.failed("http://127.0.0.1:8001")
    api.clock["t"] += web_interface._LOCAL_API_PAUSE_S + 0.1
    api.transport.mode = "ok"
    _push()
    assert len(api.transport.urls) == 1
    assert "http://127.0.0.1:8001" not in api.memory._until


def test_a_slow_server_is_not_taken_for_a_missing_one(api):
    """A read timeout means the server is listening: no pause.
    MUTATION: count every exception as a connect failure - red."""
    api.transport.mode = "slow"
    _push(3)
    assert len(api.transport.urls) == 3


def test_the_heartbeat_pauses_too_and_ends_on_its_stop(api, monkeypatch):
    """The heartbeat shares the memory: with no server it dials once per pause, not
    every interval; and it ends when its stop is set.
    MUTATION: heartbeat without the is_down check - red (6 dials); ignore stop - red."""
    import vaf.cli.cmd.run as run_mod

    monkeypatch.setattr(run_mod, "requests", SimpleNamespace(post=api.transport.post))

    class _Ticks(threading.Event):
        """stop.wait(interval) advances the fake clock instead of sleeping; set after six."""

        def __init__(self):
            super().__init__()
            self.ticks = 0

        def wait(self, timeout=None):
            api.clock["t"] += timeout
            self.ticks += 1
            if self.ticks >= 6:
                self.set()
            return self.is_set()

    stop = _Ticks()
    worker = threading.Thread(target=run_mod._heartbeat_loop, kwargs={"interval": 5, "stop": stop},
                              daemon=True)
    worker.start()
    worker.join(timeout=5.0)
    assert not worker.is_alive()
    # t = 0 refused (down until 10), 5 paused, 10 refused, 15 paused, 20 refused, 25 paused.
    assert api.transport.urls == ["http://127.0.0.1:8001/api/heartbeat"] * 3
