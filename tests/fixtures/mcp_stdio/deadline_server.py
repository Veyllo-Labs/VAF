# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A minimal stdio MCP server for tests/test_mcp_stdio_deadline.py.

Run as ``python deadline_server.py MODE``. It writes raw bytes to stdout, so what reaches the
client is exactly what the mode says, whatever the platform's text mode would make of it.
MODE decides how it misbehaves:

- ``ok``: answers normally;
- ``nonjson_once``: on the first tool call of all its processes, ``{not json`` and then
  nothing; normal after that (a mark file next to this script remembers it);
- ``scalar_once``: the same with the JSON scalar ``1``;
- ``silent_once``: the same with no output at all;
- ``silent``: every tool call goes unanswered;
- ``stderr_chatty``: 2 KB on stderr before every answer;
- ``cr_answer``: the answer with a raw carriage return as whitespace between its tokens;
- ``crlf_answer``: a log line and then the answer, each ending in a carriage return and a
  line feed, as a Windows server's text output does;
- ``utf8_answer``: the answer carries a character outside Latin-1, as UTF-8;
- ``badbytes_then_answer``: a line of bytes that are not UTF-8, then the answer;
- ``deaf_after_init``: answers the handshake, then never reads its input again;
- ``mute_init``: reads, and never answers anything, the handshake included;
- ``close_stdout``: answers the handshake, then closes its output and lives on, reading;
- ``long_line``: answers a tool call with 4 MiB that hold no line feed, then nothing.

Every request is appended to ``requests.log`` next to this script, by method.
"""
import json
import os
import sys
import time

MODE = sys.argv[1]
HERE = os.path.dirname(os.path.abspath(__file__))
MARK = os.path.join(HERE, "misbehaved.mark")
OUT = sys.stdout.buffer
CLOSED = False


def out(data: bytes) -> None:
    OUT.write(data)
    OUT.flush()


def first_time() -> bool:
    """True on the first tool call across all processes of this script."""
    if os.path.exists(MARK):
        return False
    open(MARK, "w").close()
    return True


def answer(mid, method, msg):
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": "echo"}]}}
    if method == "tools/call":
        text = ((msg.get("params") or {}).get("arguments") or {}).get("text", "")
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": "echoed: " + text}]}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "no"}}


for raw in sys.stdin.buffer:
    raw = raw.strip()
    if not raw:
        continue
    try:
        msg = json.loads(raw.decode("utf-8"))
    except Exception:
        continue
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        continue
    with open(os.path.join(HERE, "requests.log"), "a") as log:
        log.write(str(method) + "\n")
    if MODE == "mute_init" or CLOSED:
        continue
    r = answer(mid, method, msg)
    line = json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n"
    if method == "tools/call":
        if MODE == "nonjson_once" and first_time():
            out(b"{not json\n")
            continue
        if MODE == "scalar_once" and first_time():
            out(b"1\n")
            continue
        if MODE == "silent_once" and first_time():
            continue
        if MODE == "silent":
            continue
        if MODE == "cr_answer":
            line = json.dumps(r, separators=(",\r", ":\r")).encode("utf-8") + b"\n"
        elif MODE == "crlf_answer":
            out(b"server log: working\r\n")
            line = json.dumps(r).encode("utf-8") + b"\r\n"
        elif MODE == "long_line":
            out(b"x" * (4 * 1024 * 1024))
            continue
        elif MODE == "badbytes_then_answer":
            out(b"\xff\xfe log\n")
    if MODE == "stderr_chatty":
        sys.stderr.write("x" * 2047 + "\n")
        sys.stderr.flush()
    out(line)
    if MODE == "close_stdout" and method == "initialize":
        OUT.close()
        os.close(1)
        CLOSED = True
    if MODE == "deaf_after_init" and method == "initialize":
        while True:
            time.sleep(3600)
