"""
VAF Tool Bridge — provider-agnostic Programmatic Tool Calling.

Starts a short-lived HTTP server on localhost that the sandbox code can call
via the injected `vaf_tools` stub.  Only one call is processed per request
so there is no concurrency issue; the sandbox code is sequential.

Architecture
------------
Host (VAF process)                    Docker sandbox
────────────────────────────────────  ──────────────────────────────────────
ToolBridgeServer (port auto-assigned) ← vaf_tools.call("web_search", {...})
  → execute real VAF tool                   HTTP POST /call  (JSON body)
  → return JSON result                      ← JSON response
  → sandbox resumes with result

Only stdout of the final code returns to the model context.  Intermediate
tool results are consumed entirely within the running script — they never
appear as chat messages — matching Anthropic's "Programmatic Tool Calling"
semantics while working with every backend (OpenAI, Google, local, etc.).

Security
--------
- Binds to 127.0.0.1 only on the host; Docker maps via host.docker.internal /
  172.17.0.1.
- A single-use secret token per execution prevents stale processes from
  calling arbitrary tools.
- Tool allowlist: only tools the agent has loaded are callable; the full
  VAF trust/gate layer still applies.
"""
from __future__ import annotations

import json
import logging
import platform
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("vaf.tool_bridge")

# --------------------------------------------------------------------------- #
#  Stub source injected into the sandbox workspace as vaf_tools.py             #
# --------------------------------------------------------------------------- #

_VAF_TOOLS_STUB = '''"""
vaf_tools — injected stub for Programmatic Tool Calling.

Call any VAF tool from inside the sandbox:
    import vaf_tools
    result = vaf_tools.call("web_search", {"query": "Berlin weather"})
    print(result)

The call is synchronous; it blocks until the host returns the tool result.
"""
import json as _json
import os as _os
import urllib.request as _req
import urllib.error as _uerr

_BRIDGE_URL = _os.environ.get("VAF_BRIDGE_URL", "")
_BRIDGE_TOKEN = _os.environ.get("VAF_BRIDGE_TOKEN", "")


def call(tool_name: str, args: dict | None = None) -> str:
    """Call a VAF tool synchronously and return its string result."""
    if not _BRIDGE_URL:
        raise RuntimeError("vaf_tools: VAF_BRIDGE_URL not set (bridge not available)")
    payload = _json.dumps({
        "tool": tool_name,
        "args": args or {},
        "token": _BRIDGE_TOKEN,
    }).encode()
    try:
        request = _req.Request(
            _BRIDGE_URL + "/call",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _req.urlopen(request, timeout=120) as resp:
            body = resp.read().decode()
        data = _json.loads(body)
        if "error" in data:
            raise RuntimeError(f"Tool error: {data['error']}")
        return data.get("result", "")
    except _uerr.URLError as e:
        raise RuntimeError(f"vaf_tools: bridge unreachable ({e})") from e


def available() -> list:
    """Return list of tool names available through the bridge."""
    if not _BRIDGE_URL:
        return []
    payload = _json.dumps({"token": _BRIDGE_TOKEN}).encode()
    try:
        request = _req.Request(
            _BRIDGE_URL + "/list",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _req.urlopen(request, timeout=10) as resp:
            body = resp.read().decode()
        return _json.loads(body).get("tools", [])
    except Exception:
        return []
'''


# --------------------------------------------------------------------------- #
#  Host-side bridge server                                                      #
# --------------------------------------------------------------------------- #

class _BridgeHandler(BaseHTTPRequestHandler):
    """Handle /call and /list requests from the sandbox stub."""

    def log_message(self, format, *args):  # noqa: A002
        logger.debug("ToolBridge: " + format, *args)

    def _read_json(self) -> Optional[Dict[str, Any]]:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return None

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        body = self._read_json()
        if body is None:
            self._send_json({"error": "bad request"}, 400)
            return

        # Token check
        if body.get("token") != self.server.bridge_token:
            self._send_json({"error": "forbidden"}, 403)
            return

        if self.path == "/call":
            tool_name = str(body.get("tool", ""))
            args = body.get("args") or {}
            if not tool_name:
                self._send_json({"error": "tool name required"}, 400)
                return
            try:
                result = self.server.call_tool(tool_name, args)
                self._send_json({"result": str(result)})
            except Exception as exc:
                logger.warning("ToolBridge /call error for %s: %s", tool_name, exc)
                self._send_json({"error": str(exc)})

        elif self.path == "/list":
            tools = self.server.list_tools()
            self._send_json({"tools": tools})

        else:
            self._send_json({"error": "not found"}, 404)


class ToolBridgeServer:
    """
    Short-lived HTTP server that lets sandbox code call back into VAF tools.

    Usage::

        bridge = ToolBridgeServer(call_tool_fn, list_tools_fn, token)
        bridge.start()
        env = bridge.sandbox_env()   # inject into Docker via -e flags
        # ... run sandbox ...
        bridge.stop()

    ``call_tool_fn(name, args) -> str`` is called on the host side in the
    bridge thread (not the sandbox thread); make sure it is thread-safe or
    uses a simple synchronous lock if needed.
    """

    def __init__(
        self,
        call_tool: Callable[[str, Dict[str, Any]], str],
        list_tools: Callable[[], list],
        token: str,
    ) -> None:
        self._call_tool = call_tool
        self._list_tools = list_tools
        self._token = token
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._port: int = 0

    # ---------------------------------------------------------------------- #

    @property
    def port(self) -> int:
        return self._port

    def _resolve_host_gateway(self) -> str:
        """
        Docker containers reach the host via:
          - host.docker.internal  (Windows / macOS Docker Desktop)
          - 172.17.0.1            (Linux Docker bridge gateway)

        On Linux we always return the bridge gateway address because the host's
        LAN IP (what getsockname would return) is NOT reachable from inside the
        container — only the bridge gateway is.
        """
        system = platform.system()
        if system == "Linux":
            return "172.17.0.1"
        # Windows and macOS Docker Desktop expose a stable DNS alias.
        return "host.docker.internal"

    def start(self) -> None:
        """Start the bridge server on a random free port."""
        server = HTTPServer(("0.0.0.0", 0), _BridgeHandler)
        server.bridge_token = self._token
        server.call_tool = self._call_tool
        server.list_tools = self._list_tools
        self._port = server.server_address[1]
        self._server = server

        self._thread = threading.Thread(
            target=server.serve_forever, daemon=True, name="vaf-tool-bridge"
        )
        self._thread.start()
        logger.info("ToolBridge started on port %d", self._port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
        logger.info("ToolBridge stopped")

    def sandbox_env(self) -> Dict[str, str]:
        """Return env vars to inject into the Docker sandbox."""
        host = self._resolve_host_gateway()
        url = f"http://{host}:{self._port}"
        return {
            "VAF_BRIDGE_URL": url,
            "VAF_BRIDGE_TOKEN": self._token,
        }

    def stub_source(self) -> str:
        """Return the vaf_tools.py source to inject into the sandbox workspace."""
        return _VAF_TOOLS_STUB
