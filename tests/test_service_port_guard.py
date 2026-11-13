# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The suite never connects to VAF's own service ports (tests/conftest.py).

In every test that drives a turn, the push fallback and the terminal heartbeat post to
127.0.0.1:8001; with the app open on the same machine, test events reached it. The
conftest refuses the ports of scripts/hostile_env.py in every plain run, and steps aside
in the hostile run, which refuses the same ports itself and charges each refusal.

A real VAF port is never dialed here: a listener of the test's own on a free port stands
in, added to `conftest.REFUSED_SERVICE_PORTS`, which the guard reads per call. The one
child that dials 8001 does so only with a guard installed (tests/_home_witness.py).
"""
import errno
import importlib.util
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import conftest

_ROOT = Path(__file__).resolve().parent.parent

# The hostile run's sitecustomize refuses its own fixed list and ignores the set these
# tests extend; test_the_hostile_run_counts_what_the_guard_would_refuse covers that run.
plain_run_only = pytest.mark.skipif(
    conftest.HOSTILE_COUNTS_REFUSALS,
    reason="the hostile run refuses the service ports itself (scripts/hostile_env.py)")


def _hostile():
    spec = importlib.util.spec_from_file_location("hostile_env", _ROOT / "scripts" / "hostile_env.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _listener(family=socket.AF_INET, host="127.0.0.1"):
    server = socket.create_server((host, 0), family=family, backlog=8)
    server.settimeout(0.3)
    return server, server.getsockname()[1]


def _nothing_arrived(server) -> bool:
    try:
        conn, _peer = server.accept()
    except (TimeoutError, socket.timeout):
        return True
    conn.close()
    return False


def _refuse(monkeypatch, port):
    monkeypatch.setattr(conftest, "REFUSED_SERVICE_PORTS",
                        conftest.REFUSED_SERVICE_PORTS | {port})


def test_this_file_reads_the_suites_own_conftest():
    assert Path(conftest.__file__).resolve() == _ROOT / "tests" / "conftest.py"


@plain_run_only
def test_a_listed_port_is_refused_without_a_dial(monkeypatch):
    """Blocking connect, create_connection by address and by name, and connect_ex: each is
    refused with the conftest named, and the listener on the port accepts nothing.
    MUTATION: do not install the wrapper - red (the listener accepts); drop the port
    check - red (test_an_unlisted_local_port_is_reached goes red too)."""
    server, port = _listener()
    _refuse(monkeypatch, port)
    for host in ("127.0.0.1", "localhost", "127.1"):
        with pytest.raises(ConnectionRefusedError, match=r"never dialed: .*\(tests/conftest\.py\)"):
            socket.create_connection((host, port), timeout=2)
    with pytest.raises(ConnectionRefusedError, match=r"tests/conftest\.py"):
        socket.socket().connect(("localhost", port))
    assert socket.socket().connect_ex(("127.0.0.1", port)) == errno.ECONNREFUSED
    assert _nothing_arrived(server)
    server.close()


@plain_run_only
def test_a_listed_port_is_refused_on_ipv6_loopback(monkeypatch):
    """MUTATION: judge IPv4 loopback only - red."""
    try:
        server, port = _listener(socket.AF_INET6, "::1")
    except OSError:
        pytest.skip("no IPv6 loopback on this host")
    _refuse(monkeypatch, port)
    with pytest.raises(ConnectionRefusedError, match=r"tests/conftest\.py"):
        socket.create_connection(("::1", port), timeout=2)
    assert _nothing_arrived(server)
    server.close()


def test_an_unlisted_local_port_is_reached():
    """The tests' own servers on free local ports work as before.
    MUTATION: refuse every local port - red."""
    server, port = _listener()
    assert port not in conftest.REFUSED_SERVICE_PORTS
    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    assert not _nothing_arrived(server)
    client.close()
    server.close()


class _OneBeat(threading.Event):
    """The heartbeat's stop: its first wait ends the loop, so exactly one beat is sent."""

    def wait(self, timeout=None):
        self.set()
        return True


@plain_run_only
def test_the_push_fallback_and_the_heartbeat_reach_nothing(monkeypatch):
    """With real requests, both local API senders are refused before a SYN leaves and
    pause the way a runner without an app makes them: the base counts as down.
    MUTATION: raise RuntimeError instead of ConnectionRefusedError in the guard - red
    (it is no connect failure, so nothing pauses)."""
    import requests

    import vaf.cli.cmd.run as run_mod
    from vaf.core import web_interface
    from vaf.core.outage import OutageMemory

    server, port = _listener()
    _refuse(monkeypatch, port)
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(web_interface, "internal_api_base", lambda: base)
    monkeypatch.setattr(run_mod, "requests", requests)

    memory = OutageMemory(web_interface._LOCAL_API_PAUSE_S)
    monkeypatch.setattr(web_interface, "LOCAL_API_DOWN", memory)
    # The method reads nothing from its instance; the singleton stays untouched.
    web_interface.WebInterfaceManager._http_fallback_push(
        None, {"type": "agent_message_update", "sessionId": "s1"})
    assert memory.is_down(base)

    memory = OutageMemory(web_interface._LOCAL_API_PAUSE_S)
    monkeypatch.setattr(web_interface, "LOCAL_API_DOWN", memory)
    run_mod._heartbeat_loop(interval=5, stop=_OneBeat())
    assert memory.is_down(base)

    assert _nothing_arrived(server)
    server.close()


def _product_ports():
    """Every port VAF's own services listen on, read from the product's own defaults."""
    from vaf.api import network_routes
    from vaf.core.config import Config
    from vaf.core.service_stack import SERVICES

    ports = {spec.default_port for spec in SERVICES if spec.default_port}
    ports |= {int(Config.DEFAULTS["local_network_port"]),
              int(Config.DEFAULTS["local_network_port_frontend"]),
              int(Config.DEFAULTS["local_network_https_port"]),
              8443,     # the HTTPS proxy's documented port when 443 needs admin rights
              network_routes._INTERNAL_API_PORT}
    return ports


def test_every_product_port_is_on_the_one_list(monkeypatch):
    """One list for the plain and the hostile run, and it holds every port the product
    listens on by default; outside the hostile run the guard is installed.
    MUTATION: remove 6379 from _VAF_SERVICE_PORTS - red."""
    from vaf.core.config import Config

    monkeypatch.delenv("LLAMA_SERVER_PORT", raising=False)
    listed = set(_hostile()._VAF_SERVICE_PORTS)
    missing = sorted((_product_ports() | {Config.get_llama_server_port()}) - listed)
    assert not missing, f"VAF listens on {missing} by default; add them to _VAF_SERVICE_PORTS"
    assert listed == {443, 3000, 5002, 5003, 5005, 5432, 6379, 8001, 8005, 8080, 8443, 9222}
    assert conftest.REFUSED_SERVICE_PORTS == frozenset(listed)
    installed = getattr(socket.socket.connect, "_vaf_service_port_guard", False)
    assert installed is (not conftest.HOSTILE_COUNTS_REFUSALS)
    assert getattr(socket.socket.connect_ex, "_vaf_service_port_guard", False) is installed


@pytest.mark.skipif(os.name == "nt", reason="the refusal counter is not installed on a Windows host")
def test_the_hostile_run_counts_what_the_guard_would_refuse(tmp_path):
    """In the hostile run the conftest guard steps aside, so a dial to a listed port is
    still refused by hostile's own sitecustomize and charged to the test that made it.
    Safe under mutation: both layers refuse without dialing, and the witness refuses to
    dial at all when neither is installed.
    MUTATION: install the conftest guard unconditionally - red (no line is charged)."""
    hostile = _hostile()
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(hostile.sitecustomize_source(set()), encoding="utf-8")
    home = tmp_path / "home"
    (home / ".vaf").mkdir(parents=True)
    log = tmp_path / "refusals.log"
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("VAF_HOSTILE_", "VAF_HOME_WITNESS_"))
           and key not in ("PYTHONPATH", "PYTEST_CURRENT_TEST", "PYTEST_ADDOPTS")}
    env.update(
        PYTHONPATH=os.pathsep.join([str(site), str(_ROOT)]),
        HOME=str(home), USERPROFILE=str(home),
        VAF_HOSTILE_REFUSAL_LOG=str(log),
        VAF_HOSTILE_REFUSE_PORTS=",".join(str(port) for port in hostile._VAF_SERVICE_PORTS),
        VAF_HOME_WITNESS_DIAL="8001",
    )
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/_home_witness.py", "-q", "-p", "no:cacheprovider"],
        cwd=str(_ROOT), env=env, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
    lines = [line.split("\t") for line in log.read_text(encoding="utf-8").splitlines()] \
        if log.exists() else []
    charged = [(who, address) for who, address, _cost in lines]
    assert ("tests/_home_witness.py::test_witness (call)", "127.0.0.1:8001") in charged, charged
