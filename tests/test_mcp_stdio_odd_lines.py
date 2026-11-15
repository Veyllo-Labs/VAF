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
import io
import itertools
import json
import os
import random
import subprocess
import sys
import threading
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("llama_cpp", MagicMock())

from vaf.tools.mcp_client import MCPClientTool

PARSE_ERROR_PREFIX = "the MCP server sent a line that cannot be read"


def deep_line(depth: int) -> str:
    return "[" * depth + "]" * depth


def digit_line() -> str:
    return '{"jsonrpc": "2.0", "method": "notifications/x", "params": {"n": ' + "7" * 4301 + "}}"


class _FakeProcess:
    """stdin and stdout of a server process, as text buffers."""

    def __init__(self, lines):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(line + "\n" for line in lines))

    def poll(self):
        return None

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


@pytest.mark.parametrize("bad", [deep_line(10000), '{"a":' * 10000 + "1" + "}" * 10000,
                                 digit_line()], ids=["list-10000", "object-10000", "digits"])
def test_an_unreadable_line_ends_the_request(bad):
    rid = 3
    answer = json.dumps(_answer(rid))
    proc = _FakeProcess([bad, answer])
    got = MCPClientTool()._json_rpc(proc, "tools/call", {}, request_id=rid)

    assert got["id"] == rid
    assert got["error"]["code"] == -32700
    assert got["error"]["message"].startswith(PARSE_ERROR_PREFIX)
    assert proc.remaining() == answer + "\n", "the reader went past the unreadable line"


# ── answer shapes in call_stdio ─────────────────────────────────────────────────────────────

def _call_with_answer(response: dict) -> str:
    client = MCPClientTool()
    rid = 1  # the first id the fresh client hands out
    line = json.dumps({"jsonrpc": "2.0", "id": rid, **response})
    proc = _FakeProcess([line])
    client._ensure_server = lambda command, env=None: proc
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


# ── a real stub server process ──────────────────────────────────────────────────────────────

# A minimal MCP server. `MODE` decides the line it writes besides its answers:
#   stray: `1` before every answer
#   deep: a line 10,000 levels deep before every tool answer
#   deep_all: that deep line before every answer, the handshake's included
#   deep_banner: that deep line once, at startup
#   string_banner: the JSON string "server ready" once, at startup
_STUB = (
    "import sys, json\n"
    "MODE = sys.argv[1]\n"
    "DEEP = '[' * 10000 + ']' * 10000\n"
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
    "    if method == 'initialize': r = {'jsonrpc': '2.0', 'id': mid, 'result': {'capabilities': {}}}\n"
    "    elif method == 'tools/list': r = {'jsonrpc': '2.0', 'id': mid, 'result': {'tools': [{'name': 'echo', 'description': 'Echo', 'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}}}]}}\n"
    "    elif method == 'tools/call':\n"
    "        t = ((msg.get('params') or {}).get('arguments') or {}).get('text', '')\n"
    "        r = {'jsonrpc': '2.0', 'id': mid, 'result': {'content': [{'type': 'text', 'text': 'echoed: ' + t}]}}\n"
    "    else: r = {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'no'}}\n"
    "    if MODE == 'stray': out('1')\n"
    "    if MODE == 'deep_all' or (MODE == 'deep' and method == 'tools/call'): out(DEEP)\n"
    "    out(json.dumps(r))\n"
)


@pytest.fixture
def stub(tmp_path, monkeypatch):
    if " " in sys.executable or " " in str(tmp_path):
        pytest.skip("the MCP client splits a server command on spaces")
    path = tmp_path / "stub_mcp.py"
    path.write_text(_STUB, encoding="utf-8")
    clients = []
    # Every process the client starts, so each one is stopped by its own handle, also the
    # ones the client terminated and dropped from its cache itself.
    started = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", recording_popen)

    def make(mode):
        client = MCPClientTool()
        clients.append(client)
        return client, f"{sys.executable} {path} {mode}"

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
    tools = _bounded(lambda: client.list_server_tools(command))
    assert [t["name"] for t in tools] == ["echo"]
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "a"})) == "echoed: a"
    assert _bounded(lambda: client.call_stdio(command, "echo", {"text": "b"})) == "echoed: b"

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


# ── fuzz: the reader against a reference model ──────────────────────────────────────────────

_FUZZ_SEED = 0x3CB72
_KINDS = ["blank", "text", "scalar", "array", "null", "notification", "other_id", "deep",
          "digits", "answer"]


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
        if not ok or proc.remaining() != rest:
            mismatches.append((n, [k for k, _ in drawn][:consumed], str(got)[:80]))
            continue

        client = MCPClientTool()
        client._request_ids = itertools.count(rid)  # the call's id is the one the lines answer
        fake = _FakeProcess(lines)
        client._ensure_server = lambda command, env=None, fake=fake: fake
        try:
            text = client.call_stdio("fake", "echo", {})
        except Exception as exc:  # noqa: BLE001
            mismatches.append((n, "call_stdio raised", repr(exc)[:80]))
            continue
        if not isinstance(text, str):
            mismatches.append((n, "call_stdio type", type(text).__name__))
    assert not mismatches, f"{len(mismatches)} of {draws} draws differ: {mismatches[:5]}"
