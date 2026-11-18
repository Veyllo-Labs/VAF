# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The stdio MCP reader survives whatever a local server writes on its output.

The reader decoded every stdout line of the server and skipped only `JSONDecodeError`, then
called `.get("id")` on whatever came out. A server that printed `1` (or `null`, a list, a status
string) before its answers made every `call_stdio` raise `AttributeError`, so all its tools were
unreachable until VAF restarted; a line nested deeper than the decoder allows, or holding an
integer past Python's digit limit, did the same with `RecursionError` / `ValueError`. Now:

- a line that is not JSON, or is JSON but not an object, is skipped (a JSON-RPC message is an
  object);
- a line the decoder cannot read ends the request with a parse error (-32700), because it may
  be the answer itself, and skipping it would leave the reader waiting on `readline`;
- an answer in an unexpected shape comes back as text.
"""
import contextlib
import io
import itertools
import json
import os
import random
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("llama_cpp", MagicMock())

from vaf.tools.mcp_client import MCPClientTool

PARSE_ERROR_PREFIX = "the MCP server sent a line that cannot be read"


def deep_line(depth: int) -> str:
    return "[" * depth + "]" * depth


def digit_line() -> str:
    return '{"jsonrpc": "2.0", "method": "notifications/x", "params": {"n": ' + "7" * 4301 + "}}"


# Python 3.10.7 and later have an integer digit limit; before it, a long integer decodes.
_HAS_DIGIT_LIMIT = hasattr(sys, "set_int_max_str_digits")
_NEEDS_DIGIT_LIMIT = pytest.mark.skipif(not _HAS_DIGIT_LIMIT,
                                        reason="this Python has no integer digit limit")


@contextlib.contextmanager
def _digit_limit():
    """Python's integer digit limit at its default, 4300, for the length of the block.

    The limit is configurable (`PYTHONINTMAXSTRDIGITS`, 0 turns it off), and `digit_line` is
    unreadable only under a limit, so a test that relies on it sets one. On a Python without
    the limit this does nothing, and the digit cases are skipped (`_NEEDS_DIGIT_LIMIT`).
    """
    get = getattr(sys, "get_int_max_str_digits", None)
    put = getattr(sys, "set_int_max_str_digits", None)
    if get is None or put is None:
        yield
        return
    previous = get()
    put(4300)
    try:
        yield
    finally:
        put(previous)


@pytest.fixture
def int_digit_limit():
    with _digit_limit():
        yield


def test_the_digit_limit_fixture_works_on_a_python_without_the_limit(monkeypatch):
    """Python 3.10 before 3.10.7 has no digit limit: the fixture must not fail there, since
    it also wraps the deep-nesting cases and the fuzz."""
    monkeypatch.delattr(sys, "get_int_max_str_digits", raising=False)
    monkeypatch.delattr(sys, "set_int_max_str_digits", raising=False)
    with _digit_limit():
        pass


class _FakeProcess:
    """stdin and stdout of a server process, as text buffers; records being retired."""

    def __init__(self, lines):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(line + "\n" for line in lines))
        self.retired = False

    def poll(self):
        return 0 if self.retired else None

    def terminate(self):
        self.retired = True

    kill = terminate

    def wait(self, timeout=None):
        return 0

    def remaining(self) -> str:
        return self.stdout.read()


def _answer(rid, result=None):
    return {"jsonrpc": "2.0", "id": rid, "result": result if result is not None else {"ok": True}}


# ── _json_rpc on a fake process ─────────────────────────────────────────────────────────────

def test_reader_skips_lines_that_are_not_objects():
    rid = 7
    answer = _answer(rid)
    lines = ["1", "null", '"x"', "[1, 2]", "true",
             json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}),
             json.dumps(_answer(rid + 1)),
             json.dumps(answer),
             "AFTER"]
    proc = _FakeProcess(lines)
    got = MCPClientTool()._json_rpc(proc, "tools/call", {}, request_id=rid)

    assert got == answer
    assert proc.remaining() == "AFTER\n"
    assert not proc.retired, "a process that answered normally was retired"


@pytest.mark.usefixtures("int_digit_limit")
@pytest.mark.parametrize("bad", [
    pytest.param(deep_line(10000), id="list-10000"),
    pytest.param('{"a":' * 10000 + "1" + "}" * 10000, id="object-10000"),
    pytest.param(digit_line(), id="digits", marks=_NEEDS_DIGIT_LIMIT),
])
def test_an_unreadable_line_ends_the_request(bad):
    rid = 3
    answer = json.dumps(_answer(rid))
    proc = _FakeProcess([bad, answer])
    client = MCPClientTool()
    client._server_processes["fake"] = proc
    got = client._json_rpc(proc, "tools/call", {}, request_id=rid)

    assert got["id"] == rid
    assert got["error"]["code"] == -32700
    assert got["error"]["message"].startswith(PARSE_ERROR_PREFIX)
    assert proc.remaining() == answer + "\n", "the reader went past the unreadable line"
    assert proc.retired, "the process with unattributed output on its stream stayed alive"
    assert "fake" not in client._server_processes, "the retired process stayed cached"


# ── answer shapes in call_stdio ─────────────────────────────────────────────────────────────

def _call_with_answer(response: dict) -> str:
    client = MCPClientTool()
    rid = 1  # the first id the fresh client hands out
    line = json.dumps({"jsonrpc": "2.0", "id": rid, **response})
    proc = _FakeProcess([line])
    client._ensure_server = lambda command, env=None, on_process=None: proc
    return client.call_stdio("fake", "echo", {})


@pytest.mark.parametrize("response,text", [
    ({"error": "boom"}, "MCP Error: boom (code: unknown)"),
    ({"result": "no content here"}, "no content here"),
    ({"result": ["content"]}, "['content']"),
    ({"result": {"content": [{"type": "text", "text": ["a", "b"]}]}}, "['a', 'b']"),
    ({"result": {"content": [{"type": "text", "text": "fine"}]}}, "fine"),
    ({"error": {"code": -1, "message": "m"}}, "MCP Error: m (code: -1)"),
], ids=["error-text", "result-text", "result-list", "text-list", "text", "error-dict"])
def test_odd_answer_shapes_come_back_as_text(response, text):
    assert _call_with_answer(response) == text


@pytest.mark.parametrize("result,text,failed", [
    ({"content": [{"type": "text", "text": 409}], "isError": True}, "MCP Error: 409", True),
    ({"content": [{"type": "text", "text": "conflict"}], "isError": True},
     "MCP Error: conflict", True),
    ({"content": [{"type": "text", "text": 409}], "isError": False}, "409", False),
    ({"content": [{"type": "text", "text": 409}]}, "409", False),
], ids=["error-number", "error-text", "explicit-success", "plain-success"])
def test_a_tool_error_stays_an_error_as_text(result, text, failed):
    """`isError` decides, whatever the content looks like, with the prefix the shared result
    classifier reads as a failure; a successful result stays unprefixed."""
    from vaf.core.context import tool_result_is_error
    out = _call_with_answer({"result": result})
    assert out == text
    assert tool_result_is_error(out) is failed


@pytest.mark.parametrize("flag,failed", [
    (True, True), ("true", True), ("True", True), ("yes", True), ("on", True), (1, True),
    (1.0, True),
    (False, False), ("false", False), ("no", False), ("OFF", False), ("f", False), (0, False),
    (0.0, False),
    # Refused by the SDK's model, so the remote lane ends such a call as an error too.
    (None, True), (2, True), (" true", True), ("maybe", True), ([], True),
])
def test_is_error_is_read_as_the_remote_lane_reads_it(flag, failed):
    """The stdio lane reads `isError` with the coercion the remote lane's SDK applies, so the
    same server's failure is booked the same on both lanes."""
    result = {"content": [{"type": "text", "text": "conflict"}], "isError": flag}
    assert _call_with_answer({"result": result}) == ("MCP Error: conflict" if failed
                                                     else "conflict")


@pytest.mark.parametrize("content,text", [
    ([{"type": "text", "text": ""}], "MCP Error: the tool reported an error"),
    ([], "MCP Error: the tool reported an error"),
    ([{"type": "image", "data": "x"}], "MCP Error: the tool reported an error"),
    ([{"type": "text", "text": 409}], "MCP Error: 409"),
    ([{"type": "text", "text": ""}, {"type": "text", "text": " "}],
     "MCP Error: the tool reported an error"),
    ([{"type": "text", "text": " disk full "}], "MCP Error:  disk full "),
], ids=["empty-text", "empty-array", "no-text-item", "number", "only-whitespace", "padded"])
def test_an_error_without_text_gets_the_remote_lanes_wording(content, text):
    """The fallback follows the message the content carries, not the result's own dump."""
    assert _call_with_answer({"result": {"content": content, "isError": True}}) == text


@pytest.mark.parametrize("texts,text", [
    ([""], "MCP Error: the tool reported an error"),
    (["   "], "MCP Error: the tool reported an error"),
    (["", "  "], "MCP Error: the tool reported an error"),
    ([" disk full "], "MCP Error:  disk full "),
], ids=["empty", "only-whitespace", "empty-and-whitespace", "padded"])
def test_both_lanes_word_an_error_without_text_alike(texts, text):
    """One rule for both lanes: an error whose text is empty or only whitespace gets the
    fallback, on the remote lane as on the stdio lane."""
    from types import SimpleNamespace
    from vaf.core.mcp_remote import render_result
    content = [{"type": "text", "text": t} for t in texts]
    assert _call_with_answer({"result": {"content": content, "isError": True}}) == text
    remote = SimpleNamespace(content=[SimpleNamespace(type="text", text=t) for t in texts],
                             structuredContent=None, isError=True)
    assert render_result(remote) == text


def test_the_shared_client_stops_its_servers_at_exit(monkeypatch):
    import atexit
    import vaf.tools.mcp_client as mcp_client
    registered = []
    monkeypatch.setattr(mcp_client, "_shared_client", None)
    monkeypatch.setattr(atexit, "register", registered.append)
    client = mcp_client.get_mcp_client()
    assert registered == [client.stop_all_servers]


def test_a_call_whose_caller_gave_up_never_reaches_the_server():
    """A call that waited for the server's lock past its caller's budget does no I/O."""
    from vaf.core.bounded_run import TIMEOUT_PREFIX, run_bounded
    client = MCPClientTool()
    proc = _FakeProcess([json.dumps(_answer(1))])
    started = []
    client._ensure_server = (lambda command, env=None, on_process=None:
                             started.append(command) or proc)
    finished = threading.Event()

    def call():
        try:
            return client.call_stdio("fake", "echo", {})
        finally:
            finished.set()

    lock = client._lock_for("fake")
    lock.acquire()  # another call holds the server
    try:
        out = run_bounded(call, timeout=1.0, poll=0.05, label="mcp")
    finally:
        lock.release()
    assert str(out).startswith(TIMEOUT_PREFIX)
    assert finished.wait(10), "the abandoned call never returned"
    assert started == [] and proc.stdin.getvalue() == "", "the abandoned call reached the server"

    # Outside a bounded worker nothing sets the flag: the call runs as before.
    assert client.call_stdio("fake", "echo", {}) == "{'ok': True}"


# ── a real stub server process ──────────────────────────────────────────────────────────────

# A minimal MCP server. `MODE` decides the line it writes besides its answers:
#   stray: `1` before every answer
#   deep: a line 10,000 levels deep before every tool answer
#   deep_all: that deep line before every answer, the handshake's included
#   deep_banner: that deep line once, at startup
#   string_banner: the JSON string "server ready" once, at startup
#   dirty_once: on the first tool call of all its processes, an unreadable notification and
#       then an unreadable answer instead of the answer; normal answers after that
#   silent: a tool call or a tool listing is answered with a line that is not JSON and
#       nothing else, and the server leaves a mark that it got the request
#   stubborn: silent, and SIGTERM is ignored
#   slow_init: the handshake answer comes after 2.5 s
# Every request is appended to requests.log next to the stub, by method.
_STUB = (
    "import sys, json, os, signal, time\n"
    "MODE = sys.argv[1]\n"
    "HERE = os.path.dirname(os.path.abspath(__file__))\n"
    "MARK = os.path.join(HERE, 'dirty_once.mark')\n"
    "DEEP = '[' * 10000 + ']' * 10000\n"
    "DEAF = 'deaf' in sys.argv[2:]\n"
    "if MODE == 'stubborn' or DEAF: signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "SILENT = MODE in ('silent', 'stubborn')\n"
    "def out(s):\n"
    "    sys.stdout.write(s + '\\n'); sys.stdout.flush()\n"
    "if MODE == 'deep_banner': out(DEEP)\n"
    "if MODE == 'string_banner': out(json.dumps('server ready'))\n"
    "for line in sys.stdin:\n"
    "    line = line.strip()\n"
    "    if not line: continue\n"
    "    try: msg = json.loads(line)\n"
    "    except Exception: continue\n"
    "    mid = msg.get('id'); method = msg.get('method')\n"
    "    if mid is None: continue\n"
    "    with open(os.path.join(HERE, 'requests.log'), 'a') as log: log.write(str(method) + '\\n')\n"
    "    if method == 'initialize' and MODE == 'slow_init': time.sleep(2.5)\n"
    "    if method == 'initialize': r = {'jsonrpc': '2.0', 'id': mid, 'result': {'capabilities': {}}}\n"
    "    elif method == 'tools/list': r = {'jsonrpc': '2.0', 'id': mid, 'result': {'tools': [{'name': 'echo', 'description': 'Echo', 'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}}}]}}\n"
    "    elif method == 'tools/call':\n"
    "        t = ((msg.get('params') or {}).get('arguments') or {}).get('text', '')\n"
    "        r = {'jsonrpc': '2.0', 'id': mid, 'result': {'content': [{'type': 'text', 'text': 'echoed: ' + t}]}}\n"
    "    else: r = {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'no'}}\n"
    "    if MODE == 'stray': out('1')\n"
    "    if MODE == 'deep_all' or (MODE == 'deep' and method == 'tools/call'): out(DEEP)\n"
    "    if MODE == 'dirty_once' and method == 'tools/call' and not os.path.exists(MARK):\n"
    "        open(MARK, 'w').close()\n"
    "        out(DEEP)\n"
    "        out('{\"jsonrpc\": \"2.0\", \"id\": ' + str(mid) + ', \"result\": ' + DEEP + '}')\n"
    "        continue\n"
    "    if SILENT and method in ('tools/call', 'tools/list'):\n"
    "        out('{not json')\n"
    "        open(os.path.join(HERE, 'silent.mark'), 'w').close()\n"
    "        continue\n"
    "    out(json.dumps(r))\n"
    "while DEAF: time.sleep(3600)\n"
)

# A launcher that runs the stub as its child instead of replacing itself, as a shell script
# or a wrapper does; the child inherits the output pipe. `wait`: the launcher waits for the
# child. `leave`: it exits at once and leaves the child behind, out of its process tree.
_LAUNCHER = (
    "import os, subprocess, sys\n"
    "how, stub, mode = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "child = subprocess.Popen([sys.executable, stub, mode, *sys.argv[4:]])\n"
    "target = os.path.join(os.path.dirname(stub), 'child.pid')\n"
    "with open(target + '.tmp', 'w') as f: f.write(str(child.pid))\n"
    "os.replace(target + '.tmp', target)\n"
    "if how == 'wait': child.wait()\n"
)


def _gone(handle) -> bool:
    """A psutil handle's process has ended (exited, or a zombie waiting to be reaped)."""
    import psutil
    try:
        return not handle.is_running() or handle.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _identity(pid: int):
    """A process as (pid, start time), or None when no such process exists."""
    import psutil
    try:
        return (pid, psutil.Process(pid).create_time())
    except psutil.NoSuchProcess:
        return None


def _wait_until(check, seconds=10.0) -> bool:
    tick = threading.Event()
    for _ in range(int(seconds / 0.05)):
        if check():
            return True
        tick.wait(0.05)
    return check()


@pytest.fixture
def stub(tmp_path, monkeypatch):
    if " " in sys.executable or " " in str(tmp_path):
        pytest.skip("the MCP client splits a server command on spaces")
    path = tmp_path / "stub_mcp.py"
    path.write_text(_STUB, encoding="utf-8")
    launcher = tmp_path / "launcher.py"
    launcher.write_text(_LAUNCHER, encoding="utf-8")
    clients = []
    # Every process the client starts, so each one is stopped by its own handle, also the
    # ones the client terminated and dropped from its cache itself. A subclass, so the
    # client still sees a Popen.
    started = []
    real_popen = subprocess.Popen

    class _RecordingPopen(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            started.append(self)
            self._fixture_identity = _identity(self.pid)

    monkeypatch.setattr(subprocess, "Popen", _RecordingPopen)

    def make(mode, how=None, deaf=False):
        """`deaf`: the stub ignores SIGTERM and the end of its stdin."""
        client = MCPClientTool()
        clients.append(client)
        extra = " deaf" if deaf else ""
        if how is None:
            return client, f"{sys.executable} {path} {mode}{extra}"
        return client, f"{sys.executable} {launcher} {how} {path} {mode}{extra}"

    # Launcher children a test saw, as psutil handles: a handle knows its process by pid and
    # start time, so a pid handed out again later is never mistaken for it.
    children = []
    make.started = started
    make.children = children
    make.dir = tmp_path
    published = tmp_path / "child.pid"

    def child_pid():
        # The launcher publishes the pid after it started the child, which may already have
        # answered by then.
        assert _wait_until(published.exists), "the launcher never published its child's pid"
        return int(published.read_text(encoding="utf-8"))

    make.child_pid = child_pid

    def runs_this_stub(handle) -> bool:
        """The process runs this test's own stub file, so a pid handed out again since then
        to an unrelated process is never taken for the launcher's child."""
        import psutil
        try:
            return str(path) in handle.cmdline()
        except psutil.Error:
            return False

    def child():
        import psutil
        handle = psutil.Process(make.child_pid())
        assert runs_this_stub(handle), "the published pid is not the launcher's child"
        children.append(handle)
        return handle

    make.child = child
    make.requests = lambda: ((tmp_path / "requests.log").read_text(encoding="utf-8").split()
                             if (tmp_path / "requests.log").exists() else [])
    yield make
    try:
        for client in clients:
            client._server_processes.clear()
        # A launcher child the test never looked at, taken only while it provably runs this
        # test's stub.
        try:
            import psutil
            if published.exists():
                handle = psutil.Process(int(published.read_text(encoding="utf-8")))
                if runs_this_stub(handle):
                    children.append(handle)
        except Exception:  # noqa: BLE001 - no launcher child, or one already gone
            pass
        for handle in children:
            try:
                if handle.is_running():
                    handle.kill()
            except Exception:  # noqa: BLE001
                pass
    finally:
        for proc in started:
            # What is left of its group (a deaf child), signalled only while the group's id
            # still provably belongs to it: its leader is the same process, alive or unreaped.
            pgid = getattr(proc, "_vaf_pgid", None)
            if (pgid and os.name != "nt" and proc.returncode is None
                    and _identity(pgid) == proc._fixture_identity):
                try:
                    os.killpg(pgid, 9)
                except (ProcessLookupError, PermissionError):
                    pass
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


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


def test_stub_server_with_a_stray_number_before_every_answer(stub, monkeypatch):
    client, command = stub("stray")
    used = []
    tools = _bounded(lambda: client.list_server_tools(command, on_process=used.append))
    assert [t["name"] for t in tools] == ["echo"]
    process = client._server_processes[command]
    assert used == [process, None], "the listing did not report the process it used, then None"
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "a"})) == "echoed: a"
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "b"})) == "echoed: b"
    assert client._server_processes[command] is process, "a process that answered was replaced"

    import vaf.core.mcp_registry as reg
    import vaf.core.mcp_secrets as secrets
    import vaf.tools.mcp_client as mcp_client
    monkeypatch.setattr(secrets, "effective_env", lambda name, cfg: {})
    monkeypatch.setattr(mcp_client, "get_mcp_client", lambda: client)
    got = _bounded(lambda: reg.call_registered_tool("stub", {"command": command}, "echo",
                                                    {"text": "c"}))
    assert got == "echoed: c"


def test_stub_server_with_a_deep_line_before_every_answer(stub):
    client, command = stub("deep")
    for _ in range(2):
        got = _bounded(lambda: client.call_stdio(command, "echo", {"text": "a"}))
        assert got.startswith("MCP Error:") and "(code: -32700)" in got, got


def test_unread_output_after_an_unreadable_line_does_not_reach_the_next_call(stub):
    """Call A meets an unreadable notification, and its own unreadable answer is still on the
    stream; call B must start on a clean stream and get its own valid answer."""
    client, command = stub("dirty_once")
    first = _bounded(lambda: client.call_stdio(command, "echo", {"text": "a"}))
    assert first.startswith("MCP Error:") and "(code: -32700)" in first, first
    stayed_cached = command in client._server_processes

    second = _bounded(lambda: client.call_stdio(command, "echo", {"text": "b"}))
    assert second == "echoed: b"
    assert not stayed_cached, "the dirty process stayed cached"


def _blocked_call(stub, client, command):
    """Start a call on a silent server on a thread; returns once the server has the request.
    The thread's result lands in the box; `used` holds what `on_process` was told."""
    box, used = {}, []
    thread = threading.Thread(
        target=lambda: box.setdefault("out", client.call_stdio(
            command, "echo", {"text": "a"}, on_process=used.append)),
        daemon=True)
    thread.start()
    assert _wait_until((stub.dir / "silent.mark").exists), "the call never reached the server"
    assert thread.is_alive(), "the call returned although the server never answered"
    return thread, box, used


def test_stopping_a_silent_server_ends_the_call_that_waits_on_it(stub):
    """A call blocked on a server that never answers holds the server's lock; `stop_server`
    (without that lock, and at once) ends the read, the call returns, and the process exits.
    `on_process` names the process the call used, and then None once it is done."""
    client, command = stub("silent")
    thread, box, used = _blocked_call(stub, client, command)
    process = used[0]

    assert _bounded(lambda: client.stop_server(command, process=process), seconds=1.0) is True
    thread.join(10)
    assert not thread.is_alive(), "stopping the server did not end the waiting call"
    assert box["out"] == "Error: No response from MCP server"
    assert used == [process, None]
    assert command not in client._server_processes
    assert _wait_until(lambda: process.poll() is not None), "the stopped server kept running"
    assert client.stop_server(command) is False


def test_stopping_a_server_that_ignores_sigterm_returns_at_once(stub):
    """The stop runs in the background: SIGTERM, a grace, then SIGKILL. The caller does not
    wait for it (it used to wait 5 s, then kill and wait again)."""
    if os.name == "nt":
        pytest.skip("SIGTERM is a POSIX signal")
    client, command = stub("stubborn")
    thread, box, used = _blocked_call(stub, client, command)
    process = used[0]

    started = time.monotonic()
    assert _bounded(lambda: client.stop_server(command, process=process), seconds=1.0) is True
    assert time.monotonic() - started < 0.5
    assert _wait_until(lambda: process.poll() is not None), "SIGKILL never came"
    thread.join(10)
    assert box["out"] == "Error: No response from MCP server"


def test_stop_server_leaves_a_replacement_alone(stub):
    """With `process`, only that process, and only while it is the one cached: a call that
    meanwhile runs on a replacement is not stopped by a caller holding the old handle."""
    client, command = stub("stray")
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "a"})) == "echoed: a"
    current = client._server_processes[command]
    stale = object()

    assert client.stop_server(command, process=stale) is False
    assert client._server_processes[command] is current and current.poll() is None
    assert client._evict(stale) is False
    assert client._server_processes[command] is current
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "b"})) == "echoed: b"


@pytest.mark.parametrize("how", ["wait", "leave"])
def test_stopping_a_server_behind_a_launcher_ends_the_waiting_call(stub, how):
    """A launcher whose child inherits the output pipe: the child keeps the read, and the
    lock, alive after the launcher alone is stopped. The server leads its own process group,
    so the stop reaches the child, also one the launcher left behind out of its tree."""
    if os.name == "nt":
        pytest.skip("POSIX process groups; Windows stops the child tree only")
    client, command = stub("silent", how=how)
    thread, box, used = _blocked_call(stub, client, command)
    child = stub.child()

    assert _bounded(lambda: client.stop_server(command), seconds=1.0) is True
    thread.join(10)
    assert not thread.is_alive(), "the launcher's child kept the call waiting"
    assert box["out"] == "Error: No response from MCP server"
    assert _wait_until(lambda: _gone(child)), "the launcher's child kept running"


def _registry_on(client, monkeypatch, servers=None):
    import vaf.core.mcp_registry as reg
    import vaf.tools.mcp_client as mcp_client
    monkeypatch.setattr(mcp_client, "get_mcp_client", lambda: client)
    if servers is not None:
        monkeypatch.setattr(reg, "load_mcp_manifest", lambda: {"servers": servers})
    return reg


def test_the_connection_test_stops_a_server_that_never_answers(stub, monkeypatch):
    """The connection test gives up at its deadline and stops the silent server it started,
    at once, so its worker thread is not left blocked holding the server's lock."""
    client, command = stub("stubborn" if os.name != "nt" else "silent")
    reg = _registry_on(client, monkeypatch)

    started = time.monotonic()
    status = reg.probe_mcp_server({"command": command, "transport": "stdio"},
                                  timeout_seconds=1)
    took = time.monotonic() - started
    assert status["connected"] is False and status["error"] == "timeout"
    assert took < 2.0, f"the connection test took {took:.1f} s"
    assert command not in client._server_processes, "the silent server was left cached"
    assert len(stub.started) == 1
    assert _wait_until(lambda: stub.started[0].poll() is not None), "the server kept running"


def test_discovery_stops_a_server_that_never_answers(stub, monkeypatch):
    client, command = stub("silent")
    reg = _registry_on(client, monkeypatch, {"quiet": {"command": command, "enabled": True}})

    tools, status = reg.discover_mcp_tools(timeout_seconds=1)
    assert tools == {} and status["quiet"]["error"] == "timeout"
    assert command not in client._server_processes
    assert len(stub.started) == 1
    assert _wait_until(lambda: stub.started[0].poll() is not None), "the server kept running"


def test_a_connection_test_queued_behind_a_call_stops_nothing(stub, monkeypatch):
    """Another caller's call holds the server: a connection test that times out waiting for
    the lock must not stop that call's process, and once the lock is free it asks nothing."""
    client, command = stub("stray")
    assert _bounded(lambda: client.list_server_tools(command))
    healthy = client._server_processes[command]
    listed = threading.Event()
    real_list = client.list_server_tools

    def list_and_tell(*args, **kwargs):
        try:
            return real_list(*args, **kwargs)
        finally:
            listed.set()

    monkeypatch.setattr(client, "list_server_tools", list_and_tell)
    reg = _registry_on(client, monkeypatch)

    lock = client._lock_for(command)
    lock.acquire()  # another caller's call is running on the server
    try:
        status = reg.probe_mcp_server({"command": command, "transport": "stdio"},
                                      timeout_seconds=1)
        assert status["error"] == "timeout"
        assert client._server_processes.get(command) is healthy and healthy.poll() is None
    finally:
        lock.release()
    assert listed.wait(10), "the abandoned listing never returned"
    assert stub.requests().count("tools/list") == 1, "the abandoned listing asked the server"
    assert client._server_processes.get(command) is healthy and healthy.poll() is None
    assert len(stub.started) == 1


def test_a_remote_timeout_never_stops_a_local_server(stub, monkeypatch):
    """A remote entry may still carry the command of a local server; its timeout stops
    nothing local."""
    client, command = stub("stray")
    assert _bounded(lambda: client.list_server_tools(command))
    healthy = client._server_processes[command]
    release = threading.Event()

    class _HungPool:
        def list_tools(self, *args, **kwargs):
            release.wait(10)
            return []

    import vaf.core.mcp_remote as mcp_remote
    monkeypatch.setattr(mcp_remote, "get_remote_pool", lambda: _HungPool())
    reg = _registry_on(client, monkeypatch)
    try:
        status = reg.probe_mcp_server({"command": command, "transport": "http",
                                       "url": "https://mcp.example.invalid/mcp"},
                                      timeout_seconds=1)
    finally:
        release.set()
    assert status["error"] == "timeout"
    assert client._server_processes.get(command) is healthy and healthy.poll() is None


def test_a_call_whose_caller_gave_up_during_the_cold_start_writes_nothing(stub):
    """The server took longer to start than the caller's budget: the call is cancelled once
    the server is up, before the tool call is written."""
    from vaf.core.bounded_run import TIMEOUT_PREFIX, run_bounded
    client, command = stub("slow_init")
    finished = threading.Event()

    def call():
        try:
            return client.call_stdio(command, "echo", {"text": "a"})
        finally:
            finished.set()

    out = run_bounded(call, timeout=1.0, poll=0.05, label="mcp")
    assert str(out).startswith(TIMEOUT_PREFIX)
    assert finished.wait(10), "the abandoned call never returned"
    assert stub.requests() == ["initialize"], stub.requests()


def test_the_cached_servers_are_stopped_at_exit(stub):
    client, command = stub("stray")
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "a"})) == "echoed: a"
    process = client._server_processes[command]

    client.stop_all_servers()
    assert client._server_processes == {}
    assert _wait_until(lambda: process.poll() is not None)


def test_the_connection_test_runs_off_the_event_loop():
    """The WebSocket handler is async: the test waits up to its timeout on the server, so it
    runs on a worker thread, not on the loop every connection shares."""
    import re
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "web_server.py").read_text(
        encoding="utf-8")
    assert re.search(r"await asyncio\.to_thread\(\s*probe_mcp_server,", source)
    assert "probe_mcp_server(" not in source, "the connection test is called on the loop"


# ── round three: exit, the listing's lock, cold starts, the settings snapshot ──────────────

def test_a_stop_still_in_flight_is_finished_at_exit(stub):
    """A server that ignores SIGTERM and the end of its stdin, stopped just before exit: its
    stop runs on a daemon thread, which the interpreter freezes at exit before the SIGKILL.
    `stop_all_servers` finishes it."""
    if os.name == "nt":
        pytest.skip("SIGTERM is a POSIX signal")
    client, command = stub("stray", deaf=True)
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "a"})) == "echoed: a"
    process = client._server_processes[command]

    assert client.stop_server(command, process=process) is True
    assert command not in client._server_processes
    _bounded(client.stop_all_servers)
    assert process.poll() is not None, "the stop in flight was left to the frozen thread"
    assert client._stopping == set()


def test_at_exit_the_group_of_a_server_whose_leader_exited_is_stopped(stub):
    """A launcher left its child behind and exited: the cached leader is an unreaped zombie
    until the exit's own poll, so its group id is still its own, and the child (it ignores
    SIGTERM and the end of its stdin) is stopped with that group."""
    if os.name == "nt":
        pytest.skip("POSIX process groups")
    import psutil
    client, command = stub("stray", how="leave", deaf=True)
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "a"})) == "echoed: a"
    child = stub.child()
    leader = psutil.Process(stub.started[0].pid)
    # Waited for without polling it, so the leader stays unreaped as it is in VAF.
    assert _wait_until(lambda: leader.status() == psutil.STATUS_ZOMBIE), "the launcher stayed"

    _bounded(client.stop_all_servers)
    assert client._server_processes == {}
    assert _wait_until(lambda: _gone(child), seconds=3), "the launcher's child outlived the exit"


def test_the_shared_servers_are_stopped_without_creating_a_client(monkeypatch):
    import vaf.tools.mcp_client as mcp_client
    monkeypatch.setattr(mcp_client, "_shared_client", None)
    mcp_client.stop_shared_servers()
    assert mcp_client._shared_client is None

    calls = []
    fake = type("C", (), {"stop_all_servers": lambda self: calls.append(1)})()
    monkeypatch.setattr(mcp_client, "_shared_client", fake)
    mcp_client.stop_shared_servers()
    assert calls == [1]


def test_the_agents_signal_shutdown_stops_the_servers_before_os_exit(monkeypatch, tmp_path):
    """`Agent.shutdown` after a signal leaves through `os._exit`, which skips `atexit`: the
    real method, run on a stand-in agent down the branch that ends there, must stop the
    shared client's servers first."""
    import types

    import vaf.core.agent as agent_module
    import vaf.core.backend as backend
    import vaf.tools.mcp_client as mcp_client
    order = []

    class _Exited(BaseException):
        pass

    def fake_exit(code):
        order.append("os._exit")
        raise _Exited()

    monkeypatch.setattr(os, "_exit", fake_exit)
    monkeypatch.setattr(mcp_client, "_shared_client",
                        type("C", (), {"stop_all_servers": lambda self: order.append("stop")})())
    speech = types.ModuleType("vaf.core.speech")
    speech.get_speech_manager = lambda: types.SimpleNamespace(stop=lambda: None)
    monkeypatch.setitem(sys.modules, "vaf.core.speech", speech)
    monkeypatch.setattr(agent_module, "requests", types.SimpleNamespace(
        get=lambda *a, **k: types.SimpleNamespace(status_code=200)))
    pid_file = tmp_path / "server.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    class _Manager:
        def __init__(self):
            self.pid_file = str(pid_file)

        def _is_process_running(self, pid):
            return True

        def stop_server(self):
            pass

    monkeypatch.setattr(backend, "ServerManager", _Manager)
    sessions = iter([1, 0])  # this one before it leaves, none after
    agent = types.SimpleNamespace(
        server=types.SimpleNamespace(stop_server=lambda: None), use_server=True,
        config={"persist_server": False}, context_manager=None,
        _unregister_session=lambda: None, _check_other_vaf_processes=lambda: 0,
        _count_active_sessions=lambda: next(sessions))

    with pytest.raises(_Exited):
        agent_module.Agent.shutdown(agent, signum=2)
    assert order == ["stop", "os._exit"], order


def test_abandon_holds_the_listing_until_the_stop_is_issued():
    """The listing reports that it is done with its process under the server's lock; that
    report must wait while `abandon` stops the process, so no other call starts on it."""
    import vaf.core.mcp_registry as reg
    listing = reg._Listing()
    process = object()
    listing.on_process(process)
    stopping, release = threading.Event(), threading.Event()
    seen = []

    class _Client:
        def stop_server(self, command, process=None):
            seen.append(process)
            stopping.set()
            release.wait(10)
            return True

    giving_up = threading.Thread(target=listing.abandon, args=(_Client(), {"command": "x"}),
                                 daemon=True)
    giving_up.start()
    assert stopping.wait(10)
    done = threading.Thread(target=listing.on_process, args=(None,), daemon=True)
    done.start()
    done.join(0.3)
    assert done.is_alive(), "the listing let go of its process before the stop was issued"
    release.set()
    giving_up.join(10)
    done.join(10)
    assert not done.is_alive() and seen == [process]


def test_a_cold_start_whose_caller_gave_up_gets_no_handshake(stub):
    """The caller gave up before the fresh process was reported: no handshake I/O, and the
    process is not left running."""
    from vaf.core.bounded_run import cancel_when
    client, command = stub("stray")
    # What the client writes, recorded on its own side: the stub's log only shows what it
    # read before it was stopped.
    sent = []
    real_rpc = client._json_rpc
    client._json_rpc = lambda process, method, *a, **k: sent.append(method) or real_rpc(
        process, method, *a, **k)
    with cancel_when(lambda: True):
        assert client._ensure_server(command) is None
    assert sent == [], "the handshake was sent for a caller that gave up"
    assert stub.requests() == []
    assert command not in client._server_processes
    assert _wait_until(lambda: stub.started[0].poll() is not None)


def test_a_listing_whose_caller_gave_up_during_the_handshake_sends_no_listing(stub):
    from vaf.core.bounded_run import cancel_when
    client, command = stub("slow_init")
    # The cancel flag is per thread, so the listing runs on this one (the stub answers the
    # handshake after 2.5 s, so it returns).
    with cancel_when(lambda: "initialize" in stub.requests()):
        assert client.list_server_tools(command) == []
    assert stub.requests() == ["initialize"], stub.requests()


def test_a_failing_on_process_leaves_no_uninitialized_server_cached(stub):
    client, command = stub("stray")

    def broken(process):
        if process is not None:
            raise RuntimeError("observer broke")

    assert _bounded(lambda: client.list_server_tools(command, on_process=broken)) == []
    assert command not in client._server_processes, "a server without its handshake stayed"
    assert _wait_until(lambda: stub.started[0].poll() is not None)
    tools = _bounded(lambda: client.list_server_tools(command))
    assert [t["name"] for t in tools] == ["echo"]


def test_an_exited_server_is_retired_with_its_group_before_it_is_replaced(stub, monkeypatch):
    """A launcher that leaves its child behind has exited by the next call: the cache entry
    is replaced by a fresh server, and the child the old one left in its group is stopped
    (it ignores SIGTERM and the end of its stdin, so only the group's SIGKILL ends it)."""
    if os.name == "nt":
        pytest.skip("POSIX process groups")
    client, command = stub("stray", how="leave", deaf=True)
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "a"})) == "echoed: a"
    first_child = stub.child()
    assert _wait_until(lambda: stub.started[0].poll() is not None), "the launcher did not exit"

    # Looked at when the replacement starts, not after it: a background stop that lets the
    # two overlap for the 2 s grace would end the old child before a later look too. The
    # 0.2 s only lets the SIGKILL already sent land.
    at_start = []
    recorder = subprocess.Popen

    class _Looking(recorder):
        def __init__(self, *args, **kwargs):
            at_start.append(_wait_until(lambda: _gone(first_child), seconds=0.2))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _Looking)
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "b"})) == "echoed: b"
    assert at_start == [True], "the replacement started while the old group's child still ran"


def _remote_entry(tmp_path, monkeypatch, url):
    import vaf.core.mcp_registry as reg
    path = tmp_path / "mcp_servers.json"
    monkeypatch.setattr(reg, "get_mcp_manifest_path", lambda: path)
    path.write_text(json.dumps({"servers": {"svc": {"transport": "http", "url": url}}}),
                    encoding="utf-8")
    return reg, path


def test_the_connection_test_sends_the_token_of_the_origin_it_checked(tmp_path, monkeypatch):
    """A save that moves the entry to another host with a new token, landing while the test
    checks the saved origin, must not hand that new token to the tested address."""
    import vaf.core.mcp_remote as mcp_remote
    import vaf.core.mcp_secrets as mcp_secrets
    url_a, url_b = "https://a.example.invalid/mcp", "https://b.example.invalid/mcp"
    reg, path = _remote_entry(tmp_path, monkeypatch, url_a)
    box = {"token": "TOKEN-A"}
    asked = []
    monkeypatch.setattr(mcp_secrets, "server_token",
                        lambda name: asked.append(name) or box["token"])
    monkeypatch.setattr(mcp_secrets, "server_env", lambda name: {})
    sent = []

    class _Pool:
        def list_tools(self, transport, url, headers=None, auth=None, egress=None):
            sent.append((url, dict(headers or {})))
            return [{"name": "t"}]

    monkeypatch.setattr(mcp_remote, "get_remote_pool", lambda: _Pool())

    def save_elsewhere():
        with reg._manifest_lock:  # the lock every Settings save takes
            path.write_text(json.dumps({"servers": {"svc": {"transport": "http", "url": url_b}}}),
                            encoding="utf-8")
            box["token"] = "TOKEN-B"

    real_same_origin = reg.same_origin

    def checking(a, b):
        saver = threading.Thread(target=save_elsewhere, daemon=True)
        box["saver"] = saver
        saver.start()
        saver.join(0.3)  # the save lands now, if nothing holds it off
        return real_same_origin(a, b)

    import vaf.core.mcp_oauth as mcp_oauth
    real_uses_sign_in = mcp_oauth.uses_sign_in

    def uses_sign_in(cfg):
        # Asked again once the test is past its check: from here on the save has landed, so a
        # token read by name now would be the moved entry's.
        saver = box.get("saver")
        if saver is not None and not reg._manifest_lock._is_owned():
            saver.join(5)
        return real_uses_sign_in(cfg)

    monkeypatch.setattr(reg, "same_origin", checking)
    monkeypatch.setattr(mcp_oauth, "uses_sign_in", uses_sign_in)
    status = reg.probe_mcp_server({"transport": "http", "url": url_a}, timeout_seconds=5,
                                  name="svc")
    assert status["connected"] is True, status
    assert sent == [(url_a, {"Authorization": "Bearer TOKEN-A"})], sent
    assert asked == ["svc"], "the token was read again, or for a server without a name"


@pytest.mark.parametrize("save", ["remove", "upsert"])
def test_a_manifest_load_waits_for_a_save_in_progress(tmp_path, monkeypatch, save):
    """A load that moves a secret out of the file writes the manifest back; it must not do so
    in the middle of a Settings save, or it writes an older snapshot over the save."""
    import vaf.core.mcp_oauth as mcp_oauth
    import vaf.core.mcp_remote as mcp_remote
    import vaf.core.mcp_secrets as mcp_secrets
    reg, path = _remote_entry(tmp_path, monkeypatch, "https://a.example.invalid/mcp")
    monkeypatch.setattr(mcp_oauth, "forget_server", lambda *a, **k: 0)
    for name in ("clear_server_secrets", "set_server_env", "clear_server_token",
                 "set_server_token", "set_oauth_client_secret", "clear_oauth_client_secret"):
        monkeypatch.setattr(mcp_secrets, name, lambda *a, **k: None)
    monkeypatch.setattr(mcp_secrets, "server_env", lambda name: {})
    monkeypatch.setattr(mcp_secrets, "oauth_client_secret", lambda name: "")
    monkeypatch.setattr(mcp_remote, "get_remote_pool",
                        lambda: type("P", (), {"close": lambda self, *a, **k: None})())
    saving, order = threading.Event(), []
    real_save = reg.save_mcp_manifest

    def slow_save(data):
        saving.set()
        threading.Event().wait(0.3)
        order.append("saved")
        return real_save(data)

    monkeypatch.setattr(reg, "save_mcp_manifest", slow_save)
    if save == "remove":
        saver = threading.Thread(target=reg.remove_server, args=("svc",), daemon=True)
    else:
        saver = threading.Thread(target=lambda: reg.upsert_server("svc", command="srv",
                                                                  transport="stdio"),
                                 daemon=True)
    saver.start()
    assert saving.wait(10)
    loaded = reg.load_mcp_manifest()
    order.append("loaded")
    saver.join(10)
    assert order == ["saved", "loaded"], "the load ran in the middle of the save"
    if save == "remove":
        assert loaded.get("servers") == {}
    else:
        assert loaded["servers"]["svc"]["command"] == "srv"


def test_stub_server_with_a_deep_line_before_the_handshake_answer_is_dropped(stub):
    client, command = stub("deep_all")
    for _ in range(2):
        got = _bounded(lambda: client.call_stdio(command, "echo", {"text": "a"}))
        assert got == "Error: Failed to initialize MCP server"
        assert command not in client._server_processes, "an uninitialized process stayed cached"


def test_stub_server_with_a_deep_banner_is_dropped(stub):
    client, command = stub("deep_banner")
    assert _bounded(lambda: client.list_server_tools(command)) == []
    assert command not in client._server_processes, "an uninitialized process stayed cached"


def test_stub_server_with_a_string_banner_lists_its_tools(stub):
    client, command = stub("string_banner")
    tools = _bounded(lambda: client.list_server_tools(command))
    assert [t["name"] for t in tools] == ["echo"]


# ── round four: the move into _stopping, the signal handler, shutdown races, the save ──────

def _watch_state_lock(client, process):
    """Where `process` is (in the cache, in `_stopping`) each time the client's state lock is
    taken or let go of, at any depth: a signal handler on the thread that holds the lock
    runs between two of those lines, so it can look at any of them."""
    seen = []

    def note():
        seen.append((process in client._server_processes.values(), process in client._stopping))

    class _Watching:
        def __init__(self, inner):
            self.inner = inner  # the real lock, which `_state_changed` is bound to

        def __enter__(self):
            self.inner.acquire()
            note()
            return self

        def __exit__(self, *exc):
            note()
            self.inner.release()

    client._state_lock = _Watching(client._state_lock)
    return seen


def test_a_retired_process_is_never_in_neither_place(monkeypatch):
    """It enters `_stopping` before it leaves the cache, in one hold of the state lock, so a
    stop of all servers finds it wherever it looks, a handler on the same thread included."""
    import vaf.tools.mcp_client as mcp_client
    before, stopping = [], threading.Event()

    def stop(process, grace=0):
        before.extend(seen)  # the stop's own discard from `_stopping` comes after this
        stopping.set()

    monkeypatch.setattr(mcp_client, "_stop_process_tree", stop)
    client = MCPClientTool()
    process = type("P", (), {"pid": 2_000_000_000})()
    client._server_processes["cmd"] = process
    seen = _watch_state_lock(client, process)
    client._retire(process)
    assert stopping.wait(10), "the stop never ran"
    assert before and (False, False) not in before, before
    assert before[-1] == (False, True), before


def test_an_exited_server_is_in_stopping_while_its_replacement_waits_for_its_stop(monkeypatch):
    """The replacement stops the old group in the foreground, for up to the grace: all that
    time the old process is in `_stopping`, so an exit in that window stops its group."""
    import vaf.tools.mcp_client as mcp_client
    client = MCPClientTool()
    old = type("P", (), {"pid": 2_000_000_000, "poll": lambda self: 0})()
    client._server_processes["cmd"] = old
    during, before = [], []

    def stop(process, grace=0):
        during.append((process in client._server_processes.values(), process in client._stopping))
        before.extend(seen)

    monkeypatch.setattr(mcp_client, "_stop_process_tree", stop)

    def no_start(*args, **kwargs):
        raise OSError("this test starts nothing")

    monkeypatch.setattr(subprocess, "Popen", no_start)
    seen = _watch_state_lock(client, old)
    assert client._ensure_server("cmd") is None
    assert during == [(False, True)], during
    assert before and (False, False) not in before, before
    assert old not in client._stopping, "the old process stayed in _stopping after its stop"


def test_a_process_in_both_places_is_stopped_once_at_exit(monkeypatch):
    """Entering `_stopping` before leaving the cache means a stop of all servers can see a
    process in both: it is stopped once (a second stop would signal a group id that may
    have been handed out again after the first one reaped its leader)."""
    import vaf.tools.mcp_client as mcp_client
    stopped = []
    monkeypatch.setattr(mcp_client, "_stop_process_tree",
                        lambda process, grace=0: stopped.append(process))
    client = MCPClientTool()
    process = type("P", (), {"pid": 2_000_000_000})()
    client._server_processes["cmd"] = process
    client._stopping.add(process)
    client.stop_all_servers()
    assert stopped == [process]
    assert client._server_processes == {} and client._stopping == set()


def test_a_start_is_registered_before_it_stops_counting_as_under_way(monkeypatch):
    """A signal handler on the starting thread runs between two lines of the start. Once
    the start no longer counts as under way, the stop of all servers the handler runs does
    not wait for it, so from that line on the process must be in the cache or `_stopping`."""
    import vaf.tools.mcp_client as mcp_client
    monkeypatch.setattr(mcp_client, "_stop_process_tree", lambda process, grace=0: None)

    class _Fake:
        pid = 2_000_000_000
        stdin = io.StringIO()

        def poll(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: _Fake())
    client = MCPClientTool()
    client._json_rpc = lambda *args, **kwargs: {"result": {}}
    code = MCPClientTool._ensure_server.__code__
    lost = []

    def local(frame, event, arg):
        if event == "line":
            process = frame.f_locals.get("process")
            if (process is not None and client._launching == 0
                    and process not in client._server_processes.values()
                    and process not in client._stopping):
                lost.append(frame.f_lineno)
        return local

    previous = sys.gettrace()
    sys.settrace(lambda frame, event, arg: local if frame.f_code is code else None)
    try:
        started = client._ensure_server("cmd")
    finally:
        sys.settrace(previous)
    assert isinstance(started, _Fake)
    assert lost == [], f"lines where a handler's stop would miss the process: {lost}"


def test_stopping_all_servers_from_a_signal_handler_on_the_locks_holder():
    """A handler runs on the main thread between two bytecodes, possibly while that thread
    holds the state lock: stopping the servers there must not wait for itself."""
    client = MCPClientTool()

    def interrupted():
        with client._state_lock:
            client.stop_all_servers()

    _bounded(interrupted, seconds=3)


def test_a_server_whose_start_overlaps_the_exit_is_stopped_without_a_handshake(stub, monkeypatch):
    """The stop of all servers waits for a start already under way and stops that server
    with the rest; from then on no server is started."""
    gate, inside = threading.Event(), threading.Event()
    recorder = subprocess.Popen

    class _SlowStart(recorder):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            inside.set()
            gate.wait(10)

    monkeypatch.setattr(subprocess, "Popen", _SlowStart)
    client, command = stub("stray")
    sent = []
    real_rpc = client._json_rpc
    client._json_rpc = lambda process, method, *a, **k: sent.append(method) or real_rpc(
        process, method, *a, **k)
    # How long the stop waits for the start: the start wakes it when it ends, so the wait
    # is over well before `_LAUNCH_WAIT`, which only bounds a start that never ends.
    import vaf.tools.mcp_client as mcp_client
    condition, waited = client._state_changed, []

    class _TimedCondition:
        def __getattr__(self, name):
            return getattr(condition, name)

        def wait_for(self, predicate, timeout=None):
            began = time.monotonic()
            try:
                return condition.wait_for(predicate, timeout)
            finally:
                waited.append(time.monotonic() - began)

    client._state_changed = _TimedCondition()
    box = {}
    launch = threading.Thread(target=lambda: box.setdefault("p", client._ensure_server(command)),
                              daemon=True)
    launch.start()
    assert inside.wait(10), "the server never started"
    stopper = threading.Thread(target=client.stop_all_servers, daemon=True)
    stopper.start()
    assert _wait_until(lambda: client._closing), "the stop of all servers never began"
    gate.set()
    stopper.join(10)
    launch.join(10)

    assert box["p"] is None and sent == [], "the overlapping start got its handshake"
    assert len(waited) == 1 and waited[0] < mcp_client._LAUNCH_WAIT / 2, (
        f"the stop was not woken by the start's end: waited {waited}")
    assert command not in client._server_processes and client._stopping == set()
    assert _wait_until(lambda: stub.started[0].poll() is not None), "it escaped the exit"
    assert client._ensure_server(command) is None and len(stub.started) == 1


def test_a_start_that_ends_after_the_exit_took_its_list_stops_its_own_server(stub, monkeypatch):
    """The stop of all servers waits only `_LAUNCH_WAIT` for a start under way; a start
    that ends after it took its list has nobody else to stop its server, so it stops the
    server itself before it returns."""
    import vaf.tools.mcp_client as mcp_client
    monkeypatch.setattr(mcp_client, "_LAUNCH_WAIT", 0.1)
    gate, inside = threading.Event(), threading.Event()
    recorder = subprocess.Popen

    class _LateStart(recorder):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            inside.set()
            gate.wait(10)

    monkeypatch.setattr(subprocess, "Popen", _LateStart)
    client, command = stub("stray")
    box = {}
    launch = threading.Thread(target=lambda: box.setdefault("p", client._ensure_server(command)),
                              daemon=True)
    launch.start()
    try:
        assert inside.wait(10), "the server never started"
        _bounded(client.stop_all_servers)
        assert client._launching == 1, "the start ended before the stop took its list"
    finally:
        gate.set()
    launch.join(10)

    assert not launch.is_alive() and box["p"] is None
    # Killed without a grace, so the stop does not wait to reap it either.
    assert _wait_until(lambda: stub.started[0].poll() is not None, seconds=2), (
        "the late start left its server running")
    assert client._stopping == set() and command not in client._server_processes


# A VAF that exits normally while a start is held past the exit's own sweep: the start
# ends on its daemon thread and stops its server there, while another exit handler (here a
# short wait) keeps the interpreter alive only briefly. argv: the server command, a file
# for the server's pid.
_LATE_EXIT = (
    "import atexit, subprocess, sys, threading, time\n"
    "from unittest.mock import MagicMock\n"
    "sys.modules.setdefault('llama_cpp', MagicMock())\n"
    "import vaf.tools.mcp_client as mc\n"
    "command, pid_file = sys.argv[1], sys.argv[2]\n"
    "mc._LAUNCH_WAIT = 0.1\n"
    "inside, stopping = threading.Event(), threading.Event()\n"
    "real_stop = mc._stop_process_tree\n"
    "def noted_stop(process, grace=mc._STOP_GRACE):\n"
    "    stopping.set()\n"
    "    return real_stop(process, grace)\n"
    "mc._stop_process_tree = noted_stop\n"
    "# Registered first, so it runs after the client's own stop of all servers.\n"
    "atexit.register(lambda: stopping.wait(10) and time.sleep(0.15))\n"
    "client = mc.get_mcp_client()\n"
    "real_popen = subprocess.Popen\n"
    "class Held(real_popen):\n"
    "    def __init__(self, *a, **k):\n"
    "        super().__init__(*a, **k)\n"
    "        with open(pid_file, 'w') as f: f.write(str(self.pid))\n"
    "        inside.set()\n"
    "        while not client._swept: time.sleep(0.01)\n"
    "subprocess.Popen = Held\n"
    "threading.Thread(target=client._ensure_server, args=(command,), daemon=True).start()\n"
    "assert inside.wait(10)\n"
)


def test_a_late_start_at_a_normal_exit_stops_its_server_before_the_interpreter_ends(stub):
    """The late start's own stop runs on a daemon thread, which the interpreter freezes once
    the exit handlers are done: a server that ignores SIGTERM and the end of its stdin must
    get its SIGKILL at once, not after a grace the interpreter does not wait for."""
    if os.name == "nt":
        pytest.skip("SIGTERM is a POSIX signal")
    import psutil
    from pathlib import Path
    client, command = stub("stray", deaf=True)
    driver = stub.dir / "late_exit.py"
    driver.write_text(_LATE_EXIT, encoding="utf-8")
    pid_file = stub.dir / "late.pid"
    root = str(Path(__file__).resolve().parents[1])
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join(p for p in (root, os.environ.get("PYTHONPATH")) if p)}
    ran = subprocess.run([sys.executable, str(driver), command, str(pid_file)], env=env,
                         capture_output=True, text=True, timeout=30)
    assert ran.returncode == 0, ran.stderr[-2000:]
    try:
        handle = psutil.Process(int(pid_file.read_text(encoding="utf-8")))
        # Only while it provably runs this test's stub: a pid handed out again is not it.
        if str(stub.dir) not in " ".join(handle.cmdline()):
            handle = None
    except psutil.Error:
        handle = None  # gone already, or a zombie on its way out
    try:
        assert handle is None or _wait_until(lambda: _gone(handle), seconds=2), (
            "the late start's server outlived the exit")
    finally:
        if handle is not None:
            try:
                handle.kill()
            except psutil.Error:
                pass


def test_an_exit_during_a_replacements_wait_stops_the_old_group(stub):
    """The replacement waits up to the 2 s grace for the old group; an exit in that window
    finds the old process in `_stopping` and stops its group with its own short grace."""
    if os.name == "nt":
        pytest.skip("POSIX process groups")
    client, command = stub("stray", how="leave", deaf=True)
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "a"})) == "echoed: a"
    first_child = stub.child()
    old = stub.started[0]
    replacing = threading.Thread(
        target=lambda: client.call_stdio(command, "echo", {"text": "b"}), daemon=True)
    try:
        # The launcher exits at once; the next call finds it exited and stops its group.
        assert _wait_until(lambda: stub.started[0].poll() is not None)
        replacing.start()
        assert _wait_until(lambda: old in client._stopping, seconds=5), (
            "the old server was in neither place during its stop")
        began = time.monotonic()
        _bounded(client.stop_all_servers)
        # The exit's grace is 0.5 s, the replacement's 2 s: gone by the first, not the second.
        assert _wait_until(lambda: _gone(first_child), seconds=1.0), "the exit missed the group"
        assert time.monotonic() - began < 1.5
    finally:
        replacing.join(10)
    assert len(stub.started) == 1, "a replacement started after the exit"


def _stubbed_secrets(monkeypatch):
    import vaf.core.mcp_remote as mcp_remote
    import vaf.core.mcp_secrets as mcp_secrets
    for name in ("clear_server_secrets", "set_server_env", "clear_server_token",
                 "set_server_token", "set_oauth_client_secret", "clear_oauth_client_secret"):
        monkeypatch.setattr(mcp_secrets, name, lambda *a, **k: None)
    monkeypatch.setattr(mcp_secrets, "server_env", lambda name: {})
    monkeypatch.setattr(mcp_secrets, "oauth_client_secret", lambda name: "")
    monkeypatch.setattr(mcp_remote, "registered_egress", lambda url: None)


def test_a_save_checks_the_address_before_it_takes_the_manifest_lock(tmp_path, monkeypatch):
    """The DNS lookup of a remote address can take seconds; loads must not wait for it."""
    import vaf.network.egress as egress
    reg, path = _remote_entry(tmp_path, monkeypatch, "https://a.example.invalid/mcp")
    _stubbed_secrets(monkeypatch)
    checking, release, order = threading.Event(), threading.Event(), []

    def slow_check(url, policy=None):
        checking.set()
        release.wait(10)
        order.append("checked")

    monkeypatch.setattr(egress, "check_destination", slow_check)
    saver = threading.Thread(target=lambda: reg.upsert_server(
        "other", transport="http", url="https://c.example.invalid/mcp"), daemon=True)
    saver.start()
    try:
        assert checking.wait(10)
        # The check stays blocked until the load is done, so the order is the save's, not
        # the scheduler's.
        _bounded(reg.load_mcp_manifest, seconds=5)
        order.append("loaded")
    finally:
        release.set()
        saver.join(10)
    assert order == ["loaded", "checked"], "a load waited for the save's DNS check"


@pytest.mark.parametrize("save", ["upsert", "remove"])
def test_a_save_closes_sessions_after_it_lets_go_of_the_manifest_lock(tmp_path, monkeypatch,
                                                                      save):
    """Each session close waits up to 10 s; loads must not wait for them. The sign-in
    records still go under the lock, the closes after it."""
    import vaf.core.mcp_oauth as mcp_oauth
    import vaf.core.mcp_remote as mcp_remote
    reg, path = _remote_entry(tmp_path, monkeypatch, "https://a.example.invalid/mcp")
    path.write_text(json.dumps({"servers": {"svc": {
        "transport": "http", "url": "https://a.example.invalid/mcp", "auth": "oauth"}}}),
        encoding="utf-8")
    _stubbed_secrets(monkeypatch)
    import vaf.network.egress as egress
    monkeypatch.setattr(egress, "check_destination", lambda url, policy=None: None)
    closing, release, order = threading.Event(), threading.Event(), []

    def slow_close(*args, **kwargs):
        closing.set()
        release.wait(10)
        order.append("closed")

    def forget(name, cfg=None, *, deferred=None):
        order.append("forgot")
        if deferred is None:
            slow_close()
        else:
            deferred.append(slow_close)
        return 0

    monkeypatch.setattr(mcp_oauth, "forget_server", forget)
    monkeypatch.setattr(mcp_remote, "get_remote_pool",
                        lambda: type("P", (), {"close": lambda self, *a, **k: None})())
    if save == "upsert":
        saver = threading.Thread(target=lambda: reg.upsert_server(
            "svc", transport="http", url="https://b.example.invalid/mcp", auth="oauth"),
            daemon=True)
    else:
        saver = threading.Thread(target=reg.remove_server, args=("svc",), daemon=True)
    saver.start()
    try:
        assert closing.wait(10)
        # The close stays blocked until the load is done.
        _bounded(reg.load_mcp_manifest, seconds=5)
        order.append("loaded")
    finally:
        release.set()
        saver.join(10)
    assert order == ["forgot", "loaded", "closed"], order


@pytest.mark.parametrize("save", ["upsert", "remove"])
def test_a_save_that_fails_after_forgetting_still_closes_the_sessions(tmp_path, monkeypatch,
                                                                       save):
    """The sign-in records are already gone once `forget_server` ran; a write that fails
    after it must not leave the accounts' sessions open. Every close is tried, and the
    save's own error is the one raised."""
    import vaf.core.mcp_oauth as mcp_oauth
    import vaf.core.mcp_secrets as mcp_secrets
    reg, path = _remote_entry(tmp_path, monkeypatch, "https://a.example.invalid/mcp")
    path.write_text(json.dumps({"servers": {"svc": {
        "transport": "http", "url": "https://a.example.invalid/mcp", "auth": "oauth"}}}),
        encoding="utf-8")
    _stubbed_secrets(monkeypatch)
    import vaf.network.egress as egress
    monkeypatch.setattr(egress, "check_destination", lambda url, policy=None: None)
    closed = []

    def failing_close():
        raise OSError("this session would not close")

    def forget(name, cfg=None, *, deferred=None):
        assert deferred is not None
        deferred.extend([failing_close, lambda: closed.append(name)])
        return 0

    def broken(*args, **kwargs):
        raise RuntimeError("the key ring did not keep the write")

    monkeypatch.setattr(mcp_oauth, "forget_server", forget)
    if save == "upsert":
        # A new client for the sign-ins: they are forgotten, then its secret fails to store.
        monkeypatch.setattr(mcp_secrets, "set_oauth_client_secret", broken)
        with pytest.raises(RuntimeError, match="did not keep"):
            reg.upsert_server("svc", transport="http", url="https://a.example.invalid/mcp",
                              auth="oauth", oauth_client_id="other-client",
                              oauth_client_secret="s3cret")
    else:
        monkeypatch.setattr(mcp_secrets, "clear_server_secrets", broken)
        with pytest.raises(RuntimeError, match="did not keep"):
            reg.remove_server("svc")
    assert closed == ["svc"], "the sessions of the forgotten sign-ins stayed open"


def test_forgetting_a_server_can_hand_its_session_closes_back(monkeypatch):
    import vaf.core.mcp_oauth as mcp_oauth
    import vaf.core.mcp_remote as mcp_remote
    import vaf.core.mcp_secrets as mcp_secrets
    closed = []
    monkeypatch.setattr(mcp_remote, "get_remote_pool", lambda: type(
        "P", (), {"close": lambda self, url=None, account=None: closed.append(account)})())
    monkeypatch.setattr(mcp_secrets, "oauth_accounts", lambda server: ["acc"])
    monkeypatch.setattr(mcp_secrets, "oauth_record", lambda server, account: {})
    monkeypatch.setattr(mcp_secrets, "clear_oauth_record", lambda server, account: None)

    # A name of its own: sign-ins another test left pending are kept per server name.
    server = "forget-closes-test"
    deferred = []
    mcp_oauth.forget_server(server, {}, deferred=deferred)
    assert closed == [] and len(deferred) == 1, "a close ran inside the forget"
    deferred[0]()
    assert len(closed) == 1

    mcp_oauth.forget_server(server, {})
    assert len(closed) == 2, "without a list the closes run as before"


# ── fuzz: the reader against a reference model ──────────────────────────────────────────────

_FUZZ_SEED = 0x3CB72
# Without an integer digit limit a long integer decodes, so that kind is drawn only with one.
_KINDS = ["blank", "text", "scalar", "array", "null", "notification", "other_id", "deep",
          *(["digits"] if _HAS_DIGIT_LIMIT else []), "answer"]


def _draw_lines(rng: random.Random, rid: int):
    """Lines and their kinds, recorded at generation for the reference model."""
    out = []
    for _ in range(rng.randint(1, 25)):
        kind = rng.choice(_KINDS)
        if kind == "blank":
            line = rng.choice(["", "   "])
        elif kind == "text":
            line = rng.choice(["server log: ready", "{not json", "Traceback (most recent call",
                               "[unclosed"])
        elif kind == "scalar":
            line = rng.choice(["1", "-3.5", '"server ready"', "true", "false"])
        elif kind == "array":
            line = json.dumps([{"id": rid}] * rng.randint(0, 2))
        elif kind == "null":
            line = "null"
        elif kind == "notification":
            line = json.dumps({"jsonrpc": "2.0", "method": "notifications/progress",
                               "params": {"p": rng.random()}})
        elif kind == "other_id":
            line = json.dumps({"jsonrpc": "2.0", "id": rng.choice([rid + 1, str(rid), None]),
                               "result": {}})
        elif kind == "deep":
            depth = rng.randint(10000, 20000)
            line = deep_line(depth) if rng.random() < 0.5 else (
                '{"a":' * depth + "1" + "}" * depth)
        elif kind == "digits":
            line = digit_line()
        else:
            shape = rng.randrange(5)
            if shape == 0:
                body = {"result": {"content": [{"type": "text", "text": rng.choice(
                    ["t", ["a"], None, 3, {"k": 1}])}]}}
            elif shape == 1:
                body = {"result": rng.choice(["s", 1, None, ["content"], True])}
            elif shape == 2:
                body = {"error": rng.choice(["boom", 1, None, [1], {"code": 5, "message": "m"}])}
            elif shape == 3:
                body = {"result": {"content": rng.choice(["c", [], [1, "x"], None])}}
            else:
                body = {}
            message = {"jsonrpc": "2.0", "id": rid, **body}
            line = json.dumps(message)
        out.append((kind, line))
    return out


def _reference(drawn, rid):
    """(expected, consumed) from the recorded kinds only; expected "PARSE" is the parse error."""
    for index, (kind, line) in enumerate(drawn[:20]):
        consumed = index + 1
        if kind in ("deep", "digits"):
            return "PARSE", consumed
        if kind == "answer":
            return json.loads(line), consumed
    if len(drawn) < 20:
        return None, len(drawn)
    return None, 20


@pytest.mark.usefixtures("int_digit_limit")
def test_mcp_line_fuzz():
    draws = int(os.environ.get("VAF_MCP_LINES_FUZZ_DRAWS", "300"))
    rng = random.Random(_FUZZ_SEED)
    mismatches = []
    for n in range(draws):
        rid = rng.randint(1, 10_000)
        drawn = _draw_lines(rng, rid)
        lines = [line for _, line in drawn]
        expected, consumed = _reference(drawn, rid)
        rest = "".join(line + "\n" for line in lines[consumed:])

        proc = _FakeProcess(lines)
        try:
            got = MCPClientTool()._json_rpc(proc, "tools/call", {}, request_id=rid)
        except Exception as exc:  # noqa: BLE001 - a raise is the mismatch being looked for
            mismatches.append((n, "raised", repr(exc)[:80]))
            continue
        if expected == "PARSE":
            ok = (isinstance(got, dict) and got.get("id") == rid
                  and got.get("error", {}).get("code") == -32700
                  and str(got["error"].get("message", "")).startswith(PARSE_ERROR_PREFIX))
        else:
            ok = got == expected
        # Only an unreadable line retires the process; every other outcome keeps it.
        if not ok or proc.remaining() != rest or proc.retired != (expected == "PARSE"):
            mismatches.append((n, [k for k, _ in drawn][:consumed], str(got)[:80]))
            continue

        client = MCPClientTool()
        client._request_ids = itertools.count(rid)  # the call's id is the one the lines answer
        fake = _FakeProcess(lines)
        client._ensure_server = lambda command, env=None, on_process=None, fake=fake: fake
        try:
            text = client.call_stdio("fake", "echo", {})
        except Exception as exc:  # noqa: BLE001
            mismatches.append((n, "call_stdio raised", repr(exc)[:80]))
            continue
        if not isinstance(text, str):
            mismatches.append((n, "call_stdio type", type(text).__name__))
    assert not mismatches, f"{len(mismatches)} of {draws} draws differ: {mismatches[:5]}"
