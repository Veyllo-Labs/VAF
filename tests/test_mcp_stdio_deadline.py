# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Every request to a local MCP server has a deadline, and missing it frees the server.

Measured before: the stdio loop waited on `readline` and on the write with no deadline, while
the caller held the server's lock. A non-JSON answer, a JSON scalar, an answer holding a raw
carriage return (text mode split it), a server whose unread stderr pipe had filled up, and a
large request to a server that stopped reading each held that call, and every later call to
the server, forever. Now the pipes are read and written on helper threads, the request waits
until its deadline (`timeout=`, by default `tool_timeout_seconds`) or until its caller gives
up, and then the process is retired, so the next call starts a fresh one.

The stub server is tests/fixtures/mcp_stdio/deadline_server.py, run as a real process. Every
call runs under `_bounded`, so a regression fails instead of hanging the suite.
"""
import ast
import io
import math
import os
import select
import selectors
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("llama_cpp", MagicMock())

import vaf.tools.mcp_client as mcp_client  # noqa: E402
from vaf.tools.mcp_client import MCPClientTool  # noqa: E402

SERVER = Path(__file__).resolve().parent / "fixtures" / "mcp_stdio" / "deadline_server.py"


def _bounded(fn, seconds=10.0):
    """Run `fn` on a thread and give its result, failing when it takes longer than `seconds`."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised below, in the test's thread
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"the call did not return within {seconds} s"
    if "error" in box:
        raise box["error"]
    return box["value"]


def _wait_until(check, seconds=5.0) -> bool:
    tick = threading.Event()
    for _ in range(int(seconds / 0.05)):
        if check():
            return True
        tick.wait(0.05)
    return check()


def _timed(fn, seconds=10.0):
    began = time.monotonic()
    value = _bounded(fn, seconds)
    return value, time.monotonic() - began


@pytest.fixture
def server(tmp_path, monkeypatch):
    """`server(mode)` -> (client, command). Every process the client starts is recorded and
    stopped by its own handle at the end, the ones the client retired itself included."""
    if " " in sys.executable or " " in str(tmp_path):
        pytest.skip("the MCP client splits a server command on spaces")
    script = tmp_path / "deadline_server.py"
    shutil.copyfile(SERVER, script)
    started, clients = [], []
    real_popen = subprocess.Popen

    class _RecordingPopen(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            started.append(self)

    monkeypatch.setattr(subprocess, "Popen", _RecordingPopen)

    def make(mode):
        client = MCPClientTool()
        clients.append(client)
        return client, f"{sys.executable} {script} {mode}"

    make.started = started
    make.requests = lambda: ((tmp_path / "requests.log").read_text(encoding="utf-8").split()
                             if (tmp_path / "requests.log").exists() else [])
    yield make
    for client in clients:
        client._server_processes.clear()
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


def _tool_budget(monkeypatch, seconds):
    """`tool_timeout_seconds` set to `seconds` for this test."""
    from vaf.core.config import Config
    real_get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: seconds if key == "tool_timeout_seconds"
        else real_get(key, default)))


def _timeout_text(seconds) -> str:
    return f"MCP Error: no answer within {seconds} s (code: -32001)"


# ── the deadline on a read ─────────────────────────────────────────────────────────────────

def test_a_non_json_answer_ends_at_the_deadline(server):
    client, command = server("nonjson_once")
    out, took = _timed(lambda: client.call_stdio(command, "echo", {"text": "a"}, timeout=1))
    assert out == _timeout_text(1)
    assert took < 2.5, f"the call took {took:.1f} s"


def _second_call_is_fresh(server, client, command):
    """After a call that missed its deadline: the lock is free, the old process is gone, and
    the next call answers from a new process."""
    first = server.started[0]
    assert command not in client._server_processes, "the process that missed it stayed cached"
    out = _bounded(lambda: client.call_stdio(command, "echo", {"text": "b"}, timeout=5), 5)
    assert out == "echoed: b"
    assert client._server_processes[command].pid != first.pid, "the old process answered"
    assert _wait_until(lambda: first.poll() is not None), "the old process kept running"


def test_the_deadline_frees_the_lock_and_retires_the_process(server):
    client, command = server("nonjson_once")
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "a"}, timeout=1)) == (
        _timeout_text(1))
    _second_call_is_fresh(server, client, command)


def test_a_skipped_scalar_then_silence_ends_at_the_deadline(server):
    client, command = server("scalar_once")
    out, took = _timed(lambda: client.call_stdio(command, "echo", {"text": "a"}, timeout=1))
    assert out == _timeout_text(1)
    assert took < 2.5, f"the call took {took:.1f} s"
    _second_call_is_fresh(server, client, command)


# ── what the pipes carry ───────────────────────────────────────────────────────────────────

def test_a_server_that_logs_to_stderr_keeps_answering(server):
    """2 KB of log per request filled a pipe nobody read after 32 requests; stderr now goes
    to the null device."""
    client, command = server("stderr_chatty")
    for n in range(40):
        out, took = _timed(lambda: client.call_stdio(command, "echo", {"text": str(n)},
                                                     timeout=1.5), 5)
        assert out == f"echoed: {n}", f"call {n + 1}: {out}"
        assert took < 2.0, f"call {n + 1} took {took:.1f} s"
    process = client._server_processes[command]
    assert process.stderr is None
    assert len(server.started) == 1


def test_a_raw_carriage_return_in_an_answer_is_read(server):
    client, command = server("cr_answer")
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "cr ok"},
                                              timeout=5)) == "echoed: cr ok"


def test_an_answer_ending_in_crlf_is_read(server):
    client, command = server("crlf_answer")
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "crlf ok"},
                                              timeout=5)) == "echoed: crlf ok"


def test_the_pipe_is_binary_and_decoded_as_utf8(server):
    """Text mode decodes with the locale's code page: cp1252 on a Windows host has no 0x90,
    so the UTF-8 of "Ő" made the answer read as no answer."""
    client, command = server("utf8_answer")
    out = _bounded(lambda: client.call_stdio(command, "echo", {"text": "utf8 Ő ok"},
                                             timeout=5))
    assert out == "echoed: utf8 Ő ok"
    process = client._server_processes[command]
    assert not isinstance(process.stdout, io.TextIOBase)
    assert not isinstance(process.stdin, io.TextIOBase)


def test_a_line_that_is_not_utf8_is_skipped(server):
    client, command = server("badbytes_then_answer")
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "a"},
                                              timeout=5)) == "echoed: a"
    process = client._server_processes[command]
    assert not getattr(process, "_vaf_retired", False), "a log line retired a healthy server"
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "b"},
                                              timeout=5)) == "echoed: b"
    assert client._server_processes[command] is process


# ── the deadline on a write, the caller's budget, the default ──────────────────────────────

def test_a_write_to_a_server_that_stopped_reading_ends_at_the_deadline(server):
    client, command = server("deaf_after_init")
    big = {"text": "x" * 200_000}
    out, took = _timed(lambda: client.call_stdio(command, "echo", big, timeout=1))
    assert out == _timeout_text(1)
    assert took < 2.5, f"the call took {took:.1f} s"
    process = server.started[0]
    assert command not in client._server_processes
    assert _wait_until(lambda: process.poll() is not None), "the deaf server kept running"


def test_the_callers_budget_ends_the_read(server):
    """`run_bounded` gives up at its own timeout and raises the worker's cancel flag: the read
    behind it ends within the poll, long before the call's own 60 s, and frees the server."""
    from vaf.core.bounded_run import TIMEOUT_PREFIX, run_bounded
    client, command = server("silent_once")
    out = run_bounded(lambda: client.call_stdio(command, "echo", {"text": "a"}, timeout=60),
                      timeout=1.0, poll=0.05, label="mcp")
    assert str(out).startswith(TIMEOUT_PREFIX)
    first = server.started[0]
    assert _wait_until(lambda: getattr(first, "_vaf_retired", False), seconds=2), (
        "the read went on after its caller gave up")
    lock = client._lock_for(command)
    assert lock.acquire(timeout=2), "the server's lock stayed held"
    lock.release()
    _second_call_is_fresh(server, client, command)


def test_the_default_deadline_is_the_tool_budget(server, monkeypatch):
    _tool_budget(monkeypatch, 1)
    client, command = server("silent")
    out, took = _timed(lambda: client.call_stdio(command, "echo", {"text": "a"}))
    assert out == _timeout_text(1)
    assert took < 2.5, f"the call took {took:.1f} s"


class _QuietProcess:
    """A server that answers the handshake's caller nothing at all: its read blocks until the
    process is stopped. Not an OS process, so `_retire` only terminates it."""

    def __init__(self):
        self.stdin = io.BytesIO()
        self._stopped = threading.Event()
        self.stdout = self

    def readline(self):
        self._stopped.wait(30)
        return b""

    def poll(self):
        return 0 if self._stopped.is_set() else None

    def terminate(self):
        self._stopped.set()

    kill = terminate


@pytest.mark.parametrize("nonsense", [0, -1, math.nan, math.inf, "soon", None],
                         ids=["zero", "negative", "nan", "inf", "text", "none"])
def test_a_timeout_that_is_not_a_positive_number_falls_back_to_the_budget(nonsense,
                                                                          monkeypatch):
    _tool_budget(monkeypatch, 0.2)
    client = MCPClientTool()
    quiet = _QuietProcess()
    client._ensure_server = lambda command, env=None, **kwargs: quiet
    out, took = _timed(lambda: client.call_stdio("quiet", "echo", {}, timeout=nonsense), 5)
    assert out == _timeout_text(0.2)
    assert took < 1.5, f"the call took {took:.1f} s"
    assert quiet.poll() is not None, "the quiet process was not retired"


def test_a_read_whose_wait_gave_up_is_handed_to_the_next_wait():
    """The line a read brings in after its wait gave up is not lost to a second read, and
    never read twice: the next wait gets the same read, and once it is taken a new one."""
    release = threading.Event()
    lines = iter([b"first\n", b"second\n"])

    class _SlowOutput:
        def readline(self):
            release.wait(10)
            return next(lines, b"")

    process = type("P", (), {"stdout": _SlowOutput(), "stdin": io.BytesIO()})()
    pipe = mcp_client._pipe_for(process)
    assert mcp_client._pipe_for(process) is pipe
    waiting = pipe.next_line()
    assert mcp_client._await(waiting, time.monotonic() + 0.05) == mcp_client._TIMED_OUT
    again = pipe.next_line()
    assert again is waiting, "a second read was started while the first was in flight"
    release.set()
    assert mcp_client._await(again, time.monotonic() + 5) == mcp_client._DONE
    assert pipe.take(again) == b"first\n"
    assert _bounded(lambda: pipe.take(pipe.next_line()), 5) == b"second\n"


# ── the handshake, and retiring once ───────────────────────────────────────────────────────

def _count_stops(monkeypatch):
    stops = []
    real = mcp_client._stop_process_tree

    def counting(process, grace=mcp_client._STOP_GRACE):
        stops.append(process)
        return real(process, grace)

    monkeypatch.setattr(mcp_client, "_stop_process_tree", counting)
    return stops


def test_the_handshake_has_the_deadline_and_stops_once(server, monkeypatch):
    stops = _count_stops(monkeypatch)
    client, command = server("mute_init")
    out, took = _timed(lambda: client.call_stdio(command, "echo", {"text": "a"}, timeout=1))
    assert out == "Error: Failed to initialize MCP server"
    assert took < 2.5, f"the call took {took:.1f} s"
    process = server.started[0]
    assert _wait_until(lambda: process.poll() is not None), "the mute server kept running"
    threading.Event().wait(0.3)  # a second stop, if one was issued, runs on its own thread
    assert [p for p in stops if p is process] == [process], (
        f"the process was stopped {len(stops)} times")
    assert server.requests() == ["initialize"]


# ── Windows: no select, no non-blocking pipes ──────────────────────────────────────────────

@pytest.fixture
def no_posix_pipe_io(monkeypatch):
    """What a Windows anonymous pipe does not support, made to fail on this host too."""

    def refuse(*args, **kwargs):
        raise OSError("not on a Windows pipe")

    monkeypatch.setattr(select, "select", refuse)
    monkeypatch.setattr(select, "poll", refuse, raising=False)
    monkeypatch.setattr(selectors, "DefaultSelector", refuse)
    monkeypatch.setattr(os, "set_blocking", refuse, raising=False)
    try:
        import fcntl
    except ImportError:
        fcntl = None
    if fcntl is not None:
        monkeypatch.setattr(fcntl, "fcntl", refuse)


@pytest.mark.usefixtures("no_posix_pipe_io")
def test_the_deadline_works_without_select_simulated_windows(server):
    client, command = server("nonjson_once")
    out, took = _timed(lambda: client.call_stdio(command, "echo", {"text": "a"}, timeout=1))
    assert out == _timeout_text(1)
    assert took < 2.5, f"the call took {took:.1f} s"

    client, command = server("cr_answer")
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "cr ok"},
                                              timeout=5)) == "echoed: cr ok"


_POSIX_MODULES = {"select", "selectors", "fcntl", "termios"}
_POSIX_NAMES = {"set_blocking", "O_NONBLOCK", "setitimer"}


def test_no_posix_only_io_in_the_mcp_client():
    """The stdio client must run on Windows pipes: no `select` (it takes only sockets there),
    no non-blocking pipe mode, no alarm signal."""
    tree = ast.parse(Path(mcp_client.__file__).read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] in _POSIX_MODULES]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in _POSIX_MODULES:
                found.append(node.module)
        elif isinstance(node, ast.Attribute):
            if node.attr in _POSIX_NAMES:
                found.append(node.attr)
            if (node.attr == "alarm" and isinstance(node.value, ast.Name)
                    and node.value.id == "signal"):
                found.append("signal.alarm")
        elif isinstance(node, ast.Name) and node.id in _POSIX_NAMES:
            found.append(node.id)
    assert found == [], f"POSIX-only I/O in the MCP client: {found}"


# ── no thread outlives its I/O ─────────────────────────────────────────────────────────────

def _pipe_threads():
    return [t for t in threading.enumerate() if t.name == "vaf-mcp-pipe" and t.is_alive()]


def test_no_pipe_thread_outlives_its_call(server):
    before = set(_pipe_threads())  # what an earlier test left to its own stop
    client, command = server("ok")
    for n in range(50):
        assert _bounded(lambda: client.call_stdio(command, "echo", {"text": str(n)},
                                                  timeout=5)) == f"echoed: {n}"
    left = lambda: [t for t in _pipe_threads() if t not in before]  # noqa: E731
    assert _wait_until(lambda: not left(), seconds=1.0), (
        f"{len(left())} pipe thread(s) still alive after the calls")
