# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The room protocol spoken from the OTHER machine.

This is the client half of the socket lane: what a peer runs when the room's files
are not on its own disk. It connects to ``wss://<host>:<port>/ws/a2a/<room_id>``
with a credential in the query string (a join ticket, a seat, or an account token -
the server's door decides which it is), reads the welcome, replays the backlog, and
then speaks frames both ways.

WHY SYNC. The consumers are one-command CLI processes (`vaf a2a wait --url` blocks
by design) and this house keeps engine building blocks synchronous; an embedder that
wants async wraps a thread, the same answer the completion primitive gives.

WHY wss ONLY. The credential travels in the query string, because the integrated
proxy strips Authorization headers and subprotocols. Over TLS with a pinned
authority the query string is inside the encrypted channel; over plain ws it would
be a bearer credential on the wire in the clear, so plain ws is refused here rather
than documented against. The SSL context comes from ``trust.client_context`` - the
pinned per-host authority, never the system store, never verification off.

WHAT THE SERVER SAYS, so a reader of this file need not open three others:

- On accept: ``{"kind": "welcome", "room", "peer", "role", "protocol", "v"}``,
  carrying ``seat`` exactly once when a ticket was just redeemed. Keep it: the
  ticket is spent, the seat is the only way back in.
- Then the backlog, one frame per message, oldest first, closed by
  ``{"kind": "sync", "room", "lamport"}`` - everything after the sync marker is
  live traffic.
- Every submitted frame is answered with ``{"kind": "ack", "status": ...}``:
  ``committed`` (with frame/seq/lamport) or a refusal (``refused``, ``malformed``,
  ``not_writer``, ``unsupported``, ``unsupported_version``).
- Refusal close codes: 4001 credential, 4003 ticket/room mismatch, 4004 no such
  room, 4009 another connection is writing as this peer.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, Optional
from urllib.parse import urlencode, urlsplit

from vaf.core.a2a.trust import TrustRefused, client_context

#: How the close codes read to a person. The server sends a machine code; the CLI
#: shows a sentence; keeping the mapping here keeps the two ends of the wire in
#: one file each.
CLOSE_REASONS = {
    4001: "the credential was refused",
    4003: "the ticket or seat does not open this room",
    4004: "there is no such room on that machine",
    4009: "another connection is already writing as this peer",
}


class RemoteRefused(Exception):
    """The server would not have this connection, and said why."""

    def __init__(self, reason: str, *, code: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


def room_url(url: str) -> Dict[str, str]:
    """Split a room URL into what the client needs, refusing what it must.

    Returns {"origin": "wss://host:port", "room_id": ..., "path": ...}. A plain
    ``ws://`` URL is refused HERE, before any socket exists: the credential rides
    in the query string, and unencrypted it would be a bearer token in the clear.
    """
    parts = urlsplit(str(url or "").strip())
    if parts.scheme != "wss":
        raise RemoteRefused(
            f"a room is dialled over wss, not {parts.scheme or 'a bare path'!r}: "
            "the credential travels in the URL and must never cross a wire in the clear")
    room_id = parts.path.rsplit("/", 1)[-1] if "/ws/a2a/" in parts.path else ""
    if not room_id:
        raise RemoteRefused("that URL does not name a room (expected .../ws/a2a/<room-id>)")
    return {"origin": f"wss://{parts.netloc}", "room_id": room_id,
            "path": parts.path}


def parse_welcome(message: Dict[str, Any]) -> Dict[str, Any]:
    """The welcome as facts, or a refusal - never a guess.

    Separate from the socket so the contract is testable without one: a server that
    answered with something other than a vaf-a2a v1 welcome is not a room, and
    reading frames from it anyway would attribute whatever it says to a room that
    never said it.
    """
    if not isinstance(message, dict) or message.get("kind") != "welcome":
        raise RemoteRefused("the server did not answer with a room welcome")
    if message.get("protocol") != "vaf-a2a" or int(message.get("v") or 0) != 1:
        raise RemoteRefused(
            f"the server speaks {message.get('protocol')!r} v{message.get('v')!r}, "
            "not vaf-a2a v1 - leave rather than guess")
    # `packet` is the room's handshake - roster, capabilities, shared folder,
    # open work. Optional by construction: a host running an older VAF sends the
    # four fields and nothing else, and a client that demanded more would refuse
    # a room it can work in perfectly well.
    packet = message.get("welcome")
    return {"room": str(message.get("room") or ""),
            "peer": str(message.get("peer") or ""),
            "role": str(message.get("role") or ""),
            "seat": str(message.get("seat") or "") or None,
            "packet": packet if isinstance(packet, dict) else None}


class RemoteRoom:
    """One connection to one room on another machine.

    Use it as a context manager. ``connect`` performs the handshake and consumes
    the welcome; ``frames`` yields the backlog and then live traffic; ``submit``
    sends one payload and returns the server's ack for it.
    """

    def __init__(self, socket, welcome: Dict[str, Any]) -> None:
        self._socket = socket
        self.room_id = welcome["room"]
        self.peer_id = welcome["peer"]
        self.role = welcome["role"]
        self.seat = welcome.get("seat")
        # The room's handshake, or None from a host that does not send one yet.
        self.packet = welcome.get("packet")

    @classmethod
    def connect(cls, url: str, credential: str, *,
                open_timeout: float = 10.0) -> "RemoteRoom":
        from websockets.exceptions import InvalidStatus
        from websockets.sync.client import connect

        target = room_url(url)
        query = urlencode({"token": str(credential or "")})
        try:
            context = client_context(target["origin"])
        except TrustRefused:
            raise
        try:
            socket = connect(f"{target['origin']}{target['path']}?{query}",
                             ssl=context, open_timeout=open_timeout)
        except InvalidStatus as e:
            raise RemoteRefused(
                f"the server refused the connection (HTTP {getattr(getattr(e, 'response', None), 'status_code', '?')})"
            ) from None
        except OSError as e:
            raise RemoteRefused(f"could not reach {target['origin']}: {e}") from None
        try:
            welcome = parse_welcome(json.loads(socket.recv(timeout=open_timeout)))
        except RemoteRefused:
            socket.close()
            raise
        except Exception:
            code = getattr(socket, "close_code", None) or 0
            socket.close()
            raise RemoteRefused(
                CLOSE_REASONS.get(code, "the connection ended before the welcome"),
                code=int(code or 0)) from None
        return cls(socket, welcome)

    def frames(self, *, timeout: Optional[float] = None) -> Iterator[Dict[str, Any]]:
        """Everything the server sends, as dicts: backlog, the sync marker, live
        frames, and acks for other writers' traffic fanned out to this peer. The
        iterator ends when the connection does; a timeout ends ONE wait, yielding
        None is not a thing - the caller that wants a tick loop passes a timeout
        and catches TimeoutError."""
        from websockets.exceptions import ConnectionClosed

        while True:
            try:
                raw = self._socket.recv(timeout=timeout)
            except ConnectionClosed:
                return
            try:
                message = json.loads(raw)
            except Exception:
                continue
            if isinstance(message, dict):
                yield message

    def submit(self, payload: Dict[str, Any], *,
               timeout: float = 10.0) -> Dict[str, Any]:
        """Send one payload, return the ack that answers it.

        Frames fanned out for OTHER senders can arrive between the send and the
        ack; they are buffered nowhere and simply yielded to the next ``frames``
        consumer - here only the ack is awaited, because interleaving a read loop
        with a write loop in one thread is how a CLI command hangs forever.
        """
        from websockets.exceptions import ConnectionClosed

        self._socket.send(json.dumps(payload, ensure_ascii=False))
        while True:
            try:
                raw = self._socket.recv(timeout=timeout)
            except ConnectionClosed as e:
                code = getattr(e, "code", 0) or getattr(getattr(e, "rcvd", None), "code", 0)
                raise RemoteRefused(CLOSE_REASONS.get(code, "the connection ended"),
                                    code=int(code or 0)) from None
            try:
                message = json.loads(raw)
            except Exception:
                continue
            if isinstance(message, dict) and message.get("kind") == "ack":
                return message

    def close(self) -> None:
        try:
            self._socket.close()
        except Exception:
            pass

    def __enter__(self) -> "RemoteRoom":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
