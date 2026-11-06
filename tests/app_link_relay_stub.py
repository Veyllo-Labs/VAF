# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A relay of the tests' own, nothing on the network: a scripted HTTP API, an in-memory
WebSocket, and a hostile relay that tampers with, replays, reorders, truncates or
re-addresses the frames between an app and VAF's relay link."""
import asyncio
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from vaf.core.app_link import protocol as pr


class FakeResponse:
    def __init__(self, status_code: int, body: Any = None, headers: Optional[Dict[str, str]] = None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeHttp:
    """`script[path]` is a list of responses handed out in order (the last one repeats) or a
    function of the call."""

    def __init__(self, script: Optional[Dict[str, Any]] = None):
        self.script = script or {}
        self.calls: List[Dict[str, Any]] = []

    async def _call(self, method: str, url: str, **kwargs):
        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
        self.calls.append({"method": method, "url": url, "path": path, **kwargs})
        entry = self.script.get(f"{method} {path}", self.script.get(path))
        if entry is None:
            return FakeResponse(404)
        if callable(entry):
            return entry(self.calls[-1])
        return entry.pop(0) if len(entry) > 1 else entry[0]

    async def post(self, url, json=None, headers=None):
        return await self._call("POST", url, json=json, headers=headers or {})

    async def delete(self, url, params=None, headers=None):
        return await self._call("DELETE", url, params=params or {}, headers=headers or {})

    async def aclose(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def paths(self) -> List[str]:
        return [c["path"] for c in self.calls]


class FakeWS:
    """The relay's end of VAF's WebSocket: `push` hands VAF a frame, `sent` is what VAF sent,
    `close_from_relay(code)` ends it the way the relay would."""

    def __init__(self):
        self.sent: List[Any] = []
        self.close_code: Optional[int] = None
        self._queue: asyncio.Queue = asyncio.Queue()

    def push(self, message) -> None:
        self._queue.put_nowait(message)

    def close_from_relay(self, code: int) -> None:
        self.close_code = code
        self._queue.put_nowait(None)

    async def send(self, data) -> None:
        if self.close_code is not None:
            raise ConnectionError("closed")
        self.sent.append(data)
        if isinstance(data, str) and json.loads(data).get("t") == "ping":
            self.push(json.dumps({"t": "pong"}))

    async def close(self, code: int = 1000) -> None:
        if self.close_code is None:
            self.close_code = code
            self._queue.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self._queue.get()
        if message is None:
            raise StopAsyncIteration
        return message

    def texts(self) -> List[Dict[str, Any]]:
        return [json.loads(m) for m in self.sent if isinstance(m, str)]

    def binaries(self) -> List[bytes]:
        return [m for m in self.sent if isinstance(m, (bytes, bytearray))]


class FakeConnect:
    """`ws_connect` for the connector: records the URL and headers of every upgrade."""

    def __init__(self):
        self.upgrades: List[Tuple[str, Dict[str, str]]] = []
        self.addresses: List[Optional[str]] = []
        self.sockets: List[FakeWS] = []

    def __call__(self, url: str, headers: Dict[str, str], address: Optional[str] = None):
        self.upgrades.append((url, dict(headers)))
        self.addresses.append(address)
        ws = FakeWS()
        self.sockets.append(ws)
        outer = self

        class _Ctx:
            async def __aenter__(self):
                return ws

            async def __aexit__(self, *exc):
                return False
        outer.last = ws
        return _Ctx()


# ── the hostile relay ────────────────────────────────────────────────────────

def tls_records(data: bytes) -> List[bytes]:
    """TLS records out of a byte stream (5-byte header, then the length it names)."""
    out, i = [], 0
    while i + 5 <= len(data):
        length = int.from_bytes(data[i + 3:i + 5], "big")
        out.append(data[i:i + 5 + length])
        i += 5 + length
    return out


class RelayPipe:
    """Stands where the relay is: the app's bytes go into `link.on_binary` behind `cid`, the
    frames VAF sends for that cid come back to the app. `mode` breaks what the app sends
    once the handshake is done."""

    def __init__(self, link, cid: bytes, mode: str = "clean", *, route: Optional[Callable] = None):
        self.link = link
        self.cid = cid
        self.mode = mode
        self.to_client = bytearray()
        self.history: List[bytes] = []
        self.armed = False
        self._held: Optional[bytes] = None
        self._route = route

    def deliver(self, frame: bytes) -> None:
        """A frame VAF sent; the relay hands it to the app of its cid."""
        cid, data = pr.unwrap_relay(frame)
        if cid == self.cid:
            self.to_client += data

    def feed(self, data: bytes) -> None:
        """What the app sends towards VAF."""
        target = self.cid if self._route is None else self._route()
        if not self.armed or self.mode == "clean":
            self.history.append(data)
            self.link.on_binary(pr.wrap_relay(target, data))
            return
        records = tls_records(data)
        if self.mode == "tamper":
            data = bytearray(data)
            data[-3] ^= 0x01
            self.link.on_binary(pr.wrap_relay(target, bytes(data)))
        elif self.mode == "replay":
            self.link.on_binary(pr.wrap_relay(target, data))
            self.link.on_binary(pr.wrap_relay(target, data))
        elif self.mode == "reorder":
            if len(records) >= 2:
                self.link.on_binary(pr.wrap_relay(target, records[1] + records[0] + b"".join(records[2:])))
            elif self._held is None:
                self._held = data
            else:
                self.link.on_binary(pr.wrap_relay(target, data))
                self.link.on_binary(pr.wrap_relay(target, self._held))
                self._held = None
        elif self.mode == "truncate":
            self.link.on_binary(pr.wrap_relay(target, data[:-4]))
        else:
            self.link.on_binary(pr.wrap_relay(target, data))
