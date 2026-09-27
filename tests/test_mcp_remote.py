# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Remote MCP servers, against real ones: servers built with the official SDK, served on a
free local port for the test.

Measured before the change, on the same servers: the HTTP path POSTed to `<url>/tools/list`
and `<url>/tools/call` without JSON-RPC, without `initialize` and without a session, and got
nothing back; the SSE path returned "not yet fully implemented"; discovery skipped every
server that had a URL and no command, so a remote server never registered a tool; and a token
had nowhere to go. Each test below fails on that code.
"""
import json
import socket
import threading
import time

import pytest

TOKEN = "good-token"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Recorder:
    """An ASGI wrapper that remembers each request's headers and can demand a bearer token."""

    def __init__(self, app, require_token=None):
        self.app = app
        self.require_token = require_token
        self.requests = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            self.requests.append(headers)
            if self.require_token and headers.get("authorization") != f"Bearer {self.require_token}":
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


def _server():
    from mcp.server.fastmcp import FastMCP
    server = FastMCP("probe")

    @server.tool()
    def echo(text: str) -> str:
        """Echo the text."""
        return f"echo:{text}"

    @server.tool()
    def broken() -> str:
        """Always fails."""
        raise ValueError("it broke")

    return server


def _serve(app):
    import uvicorn
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return server, port


@pytest.fixture(scope="module")
def remote():
    """Three servers: Streamable HTTP, the same behind a token, and SSE."""
    http_rec = _Recorder(_server().streamable_http_app())
    auth_rec = _Recorder(_server().streamable_http_app(), require_token=TOKEN)
    sse_rec = _Recorder(_server().sse_app())
    servers = []
    urls = {}
    for key, app, path in (("http", http_rec, "/mcp"), ("auth", auth_rec, "/mcp"), ("sse", sse_rec, "/sse")):
        srv, port = _serve(app)
        servers.append(srv)
        urls[key] = f"http://127.0.0.1:{port}{path}"
    yield type("R", (), {"urls": urls, "http": http_rec, "auth": auth_rec, "sse": sse_rec})
    from vaf.core.mcp_remote import get_remote_pool
    get_remote_pool().close()
    for srv in servers:
        srv.should_exit = True


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    """mcp_servers.json in a scratch place; the key ring is per test already (conftest)."""
    import vaf.core.mcp_registry as reg
    path = tmp_path / "mcp_servers.json"
    monkeypatch.setattr(reg, "get_mcp_manifest_path", lambda: path)

    def write(servers):
        path.write_text(json.dumps({"servers": servers}), encoding="utf-8")

    def read():
        return json.loads(path.read_text(encoding="utf-8"))

    return type("M", (), {"write": staticmethod(write), "read": staticmethod(read), "path": path})


# -- the protocol ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["http", "sse"])
def test_a_remote_server_registers_its_tools_and_answers(remote, manifest, kind):
    import vaf.core.mcp_registry as reg
    manifest.write({"probe": {"transport": kind, "url": remote.urls[kind], "enabled": True}})
    tools, status = reg.discover_mcp_tools(timeout_seconds=15)
    assert status["probe"]["connected"] is True, status
    assert {"mcp_probe_echo", "mcp_probe_broken"} <= set(tools)
    assert tools["mcp_probe_echo"].run(text="hi") == "echo:hi"
    assert tools["mcp_probe_broken"].run().startswith("MCP Error:"), "an error result says so"


def test_one_session_serves_every_call(remote, manifest):
    """The SDK keeps a session (Mcp-Session-Id); every call after `initialize` carries the same."""
    import vaf.core.mcp_registry as reg
    manifest.write({"probe": {"transport": "http", "url": remote.urls["http"]}})
    before = len(remote.http.requests)
    tools, _ = reg.discover_mcp_tools(timeout_seconds=15)
    for word in ("a", "b", "c"):
        assert tools["mcp_probe_echo"].run(text=word) == f"echo:{word}"
    ids = {r.get("mcp-session-id") for r in remote.http.requests[before:] if r.get("mcp-session-id")}
    assert len(ids) == 1, ids


def test_the_raw_mcp_call_reaches_a_remote_server(remote):
    from vaf.tools.mcp_client import get_mcp_client
    out = get_mcp_client().run(transport="http", server_url=remote.urls["http"], tool_name="echo",
                               arguments={"text": "raw"})
    assert out == "echo:raw"
    assert "server_url is required" in get_mcp_client().run(transport="http", tool_name="echo")


# -- the token -------------------------------------------------------------------------------

def test_the_stored_token_is_sent_and_a_missing_one_is_explained(remote, manifest):
    import vaf.core.mcp_registry as reg
    manifest.write({"guarded": {"transport": "http", "url": remote.urls["auth"]}})
    tools, status = reg.discover_mcp_tools(timeout_seconds=15)
    assert status["guarded"]["connected"] is False
    assert "401" in (status["guarded"]["error"] or ""), status

    reg.upsert_server("guarded", transport="http", url=remote.urls["auth"], token=TOKEN)
    tools, status = reg.discover_mcp_tools(timeout_seconds=15)
    assert status["guarded"]["connected"] is True
    assert tools["mcp_guarded_echo"].run(text="ok") == "echo:ok"
    assert TOKEN not in manifest.path.read_text(encoding="utf-8"), "the token never lands in the file"


def test_the_editor_can_test_a_token_before_saving(remote, manifest):
    import vaf.core.mcp_registry as reg
    cfg = {"transport": "http", "url": remote.urls["auth"]}
    assert reg.probe_mcp_server(cfg, 15)["connected"] is False
    ok = reg.probe_mcp_server(cfg, 15, token=TOKEN)
    assert ok["connected"] is True and "echo" in ok["tools"]
