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
            # Shaped as requests raises a refusal: urllib3's NewConnectionError inside.
            from urllib3.exceptions import NewConnectionError
            raise requests.exceptions.ConnectionError(
                NewConnectionError(None, "Failed to establish a new connection: "
                                         "[Errno 111] Connection refused"))
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


class _Ticks(threading.Event):
    """stop.wait(interval) advances the fake clock instead of sleeping; set after `n`."""

    def __init__(self, clock, n):
        super().__init__()
        self.clock, self.n, self.ticks = clock, n, 0

    def wait(self, timeout=None):
        self.clock["t"] += timeout
        self.ticks += 1
        if self.ticks >= self.n:
            self.set()
        return self.is_set()


def _heartbeats(api, monkeypatch, n):
    import vaf.cli.cmd.run as run_mod

    monkeypatch.setattr(run_mod, "requests", SimpleNamespace(post=api.transport.post))
    stop = _Ticks(api.clock, n)
    worker = threading.Thread(target=run_mod._heartbeat_loop,
                              kwargs={"interval": 5, "stop": stop}, daemon=True)
    worker.start()
    worker.join(timeout=5.0)
    assert not worker.is_alive()


def test_the_heartbeat_dials_every_interval_and_reports_what_it_finds(api, monkeypatch):
    """The tray counts a CLI session idle after server_idle_timeout without a heartbeat, so
    the heartbeat never waits out a pause; it reports a refusal (pausing the push fallback)
    and ends when its stop is set.
    MUTATION: skip the heartbeat while the API is down - red (3 dials, not 6); drop its
    failed() - red; ignore stop - red."""
    _heartbeats(api, monkeypatch, 6)
    assert api.transport.urls == ["http://127.0.0.1:8001/api/heartbeat"] * 6
    assert api.memory.is_down("http://127.0.0.1:8001")


def test_a_heartbeat_that_gets_through_ends_the_pause(api, monkeypatch):
    """The app came back: the next heartbeat reaches it and the push fallback dials again
    at once, not only after the pause.
    MUTATION: drop the heartbeat's reached() - red."""
    api.memory.failed("http://127.0.0.1:8001")
    api.transport.mode = "ok"
    _heartbeats(api, monkeypatch, 1)
    assert not api.memory.is_down("http://127.0.0.1:8001")
    _push()
    assert api.transport.urls[-1] == "http://127.0.0.1:8001/api/subagent/stream"


def test_the_pause_is_ten_seconds():
    """The documented pause (docs/web-ui/WEBUI_WEBSOCKET_FLOW.md, CHANGELOG).
    MUTATION: another value - red."""
    assert web_interface._LOCAL_API_PAUSE_S == 10.0
    assert web_interface.LOCAL_API_DOWN.pause_s == 10.0


def test_the_push_follows_the_tls_port(monkeypatch):
    """With local TLS on, the plain internal port is 8005 and the push goes there.
    MUTATION: hardcode 8001 in the push - red."""
    from vaf.core.config import Config

    monkeypatch.setattr(web_interface, "LOCAL_API_DOWN", OutageMemory(10.0))
    real_get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: True if key == "local_network_tls_enabled" else real_get(key, default)))
    transport = _Transport()
    transport.mode = "ok"
    monkeypatch.setattr(requests, "post", transport.post)
    _push()
    assert transport.urls == ["http://127.0.0.1:8005/api/subagent/stream"]


def test_the_memory_reads_the_monotonic_clock_by_default(monkeypatch):
    """MUTATION: default to time.time - red (the wall clock jumps; the pause must not)."""
    import vaf.core.outage as outage

    now = {"t": 50.0}
    monkeypatch.setattr(outage.time, "monotonic", lambda: now["t"])
    memory = OutageMemory(10.0)
    memory.failed("k")
    now["t"] = 59.0
    assert memory.is_down("k")
    now["t"] = 60.0
    assert not memory.is_down("k")


def test_only_a_failed_connect_counts_as_down():
    """Measured with real sockets on free ports: a refused connect counts; a server that
    accepted and then reset does not (it is listening, and one reset must not pause the
    live view); a connect timeout counts.
    MUTATION: count every requests ConnectionError - red (the reset)."""
    import socket
    import struct

    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    try:
        requests.post(f"http://127.0.0.1:{closed.getsockname()[1]}/x", timeout=0.5)
    except Exception as exc:
        refused = exc
    closed.close()

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def _reset():
        conn, _ = server.accept()
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()

    threading.Thread(target=_reset, daemon=True).start()
    try:
        requests.post(f"http://127.0.0.1:{server.getsockname()[1]}/x", data=b"x", timeout=2)
    except Exception as exc:
        reset = exc
    server.close()

    assert web_interface._is_connect_failure(refused)
    assert isinstance(reset, requests.exceptions.ConnectionError)
    assert not web_interface._is_connect_failure(reset)
    assert web_interface._is_connect_failure(requests.exceptions.ConnectTimeout("connect timed out"))


def test_a_body_that_stalls_is_not_an_outage():
    """requests turns a urllib3 ReadTimeoutError while the response body is read into a
    ConnectionError; the server sent its headers, so it is up: an ordinary failed push.
    MUTATION: count every requests ConnectionError - red."""
    from urllib3.exceptions import ReadTimeoutError

    stalled = requests.exceptions.ConnectionError(
        ReadTimeoutError(None, "/api/subagent/stream", "Read timed out."))
    assert not web_interface._is_connect_failure(stalled)


_SLIM_CHILD = r'''
import sys, threading
with open(sys.argv[1], encoding="utf-8") as f:
    exec(compile(f.read(), "sitecustomize", "exec"))
import requests
posts = []
requests.post = lambda *a, **k: posts.append(a)
import vaf.cli.cmd.run as run_mod
stop = threading.Event()
worker = threading.Thread(target=run_mod._heartbeat_loop, kwargs={"interval": 0.01, "stop": stop})
worker.start()
worker.join(timeout=5)
alive = worker.is_alive()
stop.set()
try:
    import fastapi
    hidden = False
except ImportError:
    hidden = True
print(hidden, alive, len(posts))
'''


def test_the_heartbeat_ends_quietly_on_a_slim_install(tmp_path):
    """Without the [server] extra there is no fastapi, so vaf.core.web_interface cannot be
    imported and no local API runs: the heartbeat ends at once instead of dying with an
    uncaught ModuleNotFoundError in its thread. fastapi is hidden the way
    scripts/hostile_env.py hides optional packages, in a child interpreter.
    MUTATION: import web_interface outside the guard - red (the thread dies with a
    traceback on stderr)."""
    import importlib.util
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("hostile_env", root / "scripts" / "hostile_env.py")
    hostile = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hostile)
    snippet = tmp_path / "hide_fastapi.py"
    snippet.write_text(hostile.sitecustomize_source({"fastapi"}), encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("VAF_HOSTILE_") and key != "PYTEST_CURRENT_TEST"}
    env["PYTHONPATH"] = str(root)
    home = tmp_path / "home"
    (home / ".vaf").mkdir(parents=True)
    # The snippet reads "~" the Windows way (USERPROFILE); both point at a home of its own.
    env["HOME"] = env["USERPROFILE"] = str(home)
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg-config")
    out = subprocess.run([sys.executable, "-c", _SLIM_CHILD, str(snippet)],
                         capture_output=True, text=True, timeout=120, env=env, cwd=str(root))
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["True", "False", "0"]
    assert "Traceback" not in out.stderr
