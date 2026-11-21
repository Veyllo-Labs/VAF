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
import gc
import io
import json
import math
import os
import select
import selectors
import shutil
import subprocess
import sys
import threading
import time
import types
import weakref
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("llama_cpp", MagicMock())

import vaf.tools.mcp_client as mcp_client  # noqa: E402
from vaf.tools.mcp_client import MCPClientTool  # noqa: E402

SERVER = Path(__file__).resolve().parent / "fixtures" / "mcp_stdio" / "deadline_server.py"
LAUNCHER = Path(__file__).resolve().parent / "fixtures" / "mcp_stdio" / "launcher.py"


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
    """`server(mode, interpreter=None)` -> (client, command), the stub run by `interpreter`
    (by default sys.executable). Every process the client starts is recorded and stopped by
    its own handle at the end, the ones the client retired itself included."""
    if " " in str(tmp_path):
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

    def make(mode, interpreter=None):
        interpreter = interpreter or sys.executable
        if " " in interpreter:
            pytest.skip("the MCP client splits a server command on spaces")
        client = MCPClientTool()
        clients.append(client)
        return client, f"{interpreter} {script} {mode}"

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
    """A regression pin for a Windows server's line ending: a log line and the answer, each
    ending in ``\r\n``. No reader mutation turns this alone red, because JSON reads the
    ``\r`` as whitespace either way; the raw ``\r`` test above and the fuzz's lines ending in
    ``\r`` carry that load."""
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

    def readline(self, size=-1):
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
        def readline(self, size=-1):
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


def test_a_handshake_error_with_the_timeout_code_adds_no_server_text(server):
    """The deadline's words are built here, once the deadline has passed; a server that
    answers the handshake with the same code and words of its own gets the plain line."""
    client, command = server("init_error_32001")
    out = _bounded(lambda: client.call_stdio(command, "echo", {"text": "a"}, timeout=5))
    assert out == "Error: Failed to initialize MCP server"
    assert "INJECTED" not in out


def test_the_previous_message_is_let_go_before_the_next_line_is_decoded(monkeypatch):
    """Holding the last decoded message while the next line decodes kept two at once: a
    request of twenty large lines peaked at twice what one line costs (measured 3279 MiB
    against 1673 MiB)."""
    real = json
    refs, alive = [], []

    class _Message(dict):
        pass

    def loads(text):
        alive.append(sum(ref() is not None for ref in refs))
        value = real.loads(text)
        if isinstance(value, dict):
            value = _Message(value)
            refs.append(weakref.ref(value))
        return value

    monkeypatch.setattr(mcp_client, "json", types.SimpleNamespace(
        loads=loads, dumps=real.dumps, JSONDecodeError=real.JSONDecodeError))
    note = real.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
    answer = real.dumps({"jsonrpc": "2.0", "id": 3, "result": {"ok": True}})
    process = _QuietProcess()
    process.stdout = io.BytesIO(f"{note}\n{note}\n{answer}\n".encode("utf-8"))
    got = MCPClientTool()._json_rpc(process, "tools/call", {}, request_id=3, timeout=5)
    assert got == {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}}
    assert alive == [0, 0, 0], f"decoded messages still held at each decode: {alive}"


# ── the end of the stream, a failed write, a line without end, a deadline already past ────

def _own_process_interpreter() -> str:
    """An interpreter that runs a script in the process the client starts: the base one. In
    a Windows virtual environment sys.executable is a launcher that runs the interpreter as
    its child and holds the pipes, so the end of the output never comes through it (the
    test below). deadline_server.py imports only the standard library, which is why the
    base interpreter, without the venv's packages, can run it; keep it that way."""
    return getattr(sys, "_base_executable", None) or sys.executable


def test_a_server_that_closed_its_output_is_replaced(server):
    """Its process lives on, so before every later call read the closed stream and failed
    the same way until VAF restarted; the end of the output now retires the process."""
    client, command = server("close_stdout", _own_process_interpreter())
    out = _bounded(lambda: client.call_stdio(command, "echo", {"text": "a"}, timeout=5))
    assert out == "Error: No response from MCP server"
    first = server.started[0]
    assert command not in client._server_processes, "the process with a closed output stayed"
    assert _wait_until(lambda: first.poll() is not None), "it was not stopped"
    _bounded(lambda: client.call_stdio(command, "echo", {"text": "b"}, timeout=5))
    assert len(server.started) == 2 and server.started[1].pid != first.pid


def test_a_launcher_that_holds_the_output_is_retired_at_the_deadline(server, tmp_path):
    """A wrapper that runs the server as its child and waits (a Windows venv's python.exe, a
    shell script without `exec`) keeps the output open after the server closed its own, so
    no end arrives: the deadline retires the wrapper instead, and the next call starts a
    fresh process. Measured on the Windows runner, where sys.executable is that launcher."""
    launcher = tmp_path / "launcher.py"
    shutil.copyfile(LAUNCHER, launcher)
    client, command = server("close_stdout")
    command = command.replace(" ", f" {launcher} ", 1)
    out = _bounded(lambda: client.call_stdio(command, "echo", {"text": "a"}, timeout=2))
    assert out == _timeout_text(2)
    first = server.started[0]
    assert command not in client._server_processes, "the held process stayed"
    assert _wait_until(lambda: first.poll() is not None), "it was not stopped"
    _bounded(lambda: client.call_stdio(command, "echo", {"text": "b"}, timeout=2))
    assert len(server.started) == 2 and server.started[1].pid != first.pid


class _BrokenInput(io.BytesIO):
    def write(self, data):
        raise BrokenPipeError("the server is gone")


def test_a_request_that_could_not_be_written_reads_nothing_and_retires():
    """A write that failed sent no request, so whatever the output still holds is not its
    answer, and the pipe cannot be used again."""
    rid = 4
    answer = json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"ok": True}}) + "\n"
    process = _QuietProcess()
    process.stdin = _BrokenInput()
    process.stdout = io.BytesIO(answer.encode("utf-8"))
    client = MCPClientTool()
    assert client._json_rpc(process, "tools/call", {}, request_id=rid, timeout=5) is None
    assert process.poll() is not None, "the process with a broken input was kept"
    assert process.stdout.read() == answer.encode("utf-8"), "the output was read anyway"


def test_a_request_whose_deadline_has_passed_writes_nothing():
    """A handshake can use up the whole budget: the request after it is then not written, so
    the tool does not run after its time is up."""
    process = _QuietProcess()
    client = MCPClientTool()
    got = client._json_rpc(process, "tools/call", {}, request_id=9,
                           deadline=time.monotonic() - 0.01, timeout=1)
    assert got == {"jsonrpc": "2.0", "id": 9,
                   "error": {"code": -32001, "message": "no answer within 1 s"}}
    assert process.stdin.getvalue() == b"", "the request was written after its deadline"
    assert process.poll() is not None


def test_a_line_longer_than_the_limit_ends_the_request(server, monkeypatch):
    """A line without end was held whole, growing the application by about 0.9 GB/s until
    the deadline. The stub writes 4 MiB without a line feed and stops, so the memory a
    reader without the limit would take is bounded here by those 4 MiB."""
    monkeypatch.setattr(mcp_client, "_MAX_LINE", 1024 * 1024)
    client, command = server("long_line")
    out, took = _timed(lambda: client.call_stdio(command, "echo", {"text": "a"}, timeout=5))
    assert out == ("MCP Error: the MCP server sent a line longer than 1 MiB (code: -32700)")
    assert took < 2.5, f"the call took {took:.1f} s"
    first = server.started[0]
    assert command not in client._server_processes
    assert _wait_until(lambda: first.poll() is not None), "the server was not stopped"


def _open_descriptors():
    """This process's open file descriptors, or None where psutil cannot count them."""
    import psutil
    try:
        return psutil.Process().num_fds()
    except (AttributeError, psutil.Error):
        return None


def test_a_retired_process_is_freed_without_a_garbage_collection(tmp_path, monkeypatch):
    """The pipe kept on a process must not refer back to it: that cycle kept every retired
    process and its two pipe descriptors until a full collection, which a large process
    runs rarely (measured: 300 timed-out calls, 600 descriptors left open)."""
    if " " in sys.executable or " " in str(tmp_path):
        pytest.skip("the MCP client splits a server command on spaces")
    script = tmp_path / "deadline_server.py"
    shutil.copyfile(SERVER, script)
    real_popen = subprocess.Popen
    refs = []

    class _Watched(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            refs.append(weakref.ref(self))

    monkeypatch.setattr(subprocess, "Popen", _Watched)
    client = MCPClientTool()
    command = f"{sys.executable} {script} silent"
    before = _open_descriptors()
    gc.collect()
    gc.disable()
    try:
        for _ in range(5):
            assert _bounded(lambda: client.call_stdio(command, "echo", {}, timeout=0.5)) == (
                _timeout_text(0.5))
        assert _wait_until(lambda: not client._stopping, seconds=10), "a stop never ended"
        assert len(refs) == 5
        assert _wait_until(lambda: all(ref() is None for ref in refs), seconds=5), (
            f"{sum(ref() is not None for ref in refs)} retired process(es) still held")
        after = _open_descriptors()
        if before is not None:
            assert after - before < 4, f"{after - before} descriptors left open"
    finally:
        gc.enable()
        client.stop_all_servers()
        for ref in refs:
            process = ref()
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)


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
    assert out == "Error: Failed to initialize MCP server (no answer within 1 s)"
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
