# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A room client for a machine that has no VAF at all. Standard library only.

`examples/10_a2a_reference_peer.py` proves the protocol RULES are implementable
from the document alone; this file proves the same for the TRANSPORT: pin the
host's authority against the fingerprint from the invitation, dial the room
over wss, redeem the ticket, keep the seat, and speak frames both ways. Together
the two files are a complete foreign peer, and neither imports a line of VAF -
a guard in `tests/test_a2a_conformance.py` fails the suite if one ever does.

WHY THE STANDARD LIBRARY, INCLUDING THE WEBSOCKET CLIENT. This file is meant to
be DOWNLOADED by a stranger's agent from the room host itself
(`https://<host>:<port>/api/a2a/client.py`), checked against the sha256 its
invitation carries, read, and then run. Every dependency it pulled in would be
code the reader did not review arriving over a channel they have not yet
decided to trust; zero dependencies keeps the whole of what runs in one file.

HOW TRUST WORKS HERE, because it is the part worth reading twice. The download
and the CA fetch are deliberately UNVERIFIED connections - there is nothing to
verify against yet. What makes them safe is that the invitation travelled by
another route and carries the checksums: the sha256 of this file, and the
fingerprint of the host's certificate authority. A tampered download fails the
hash; a wrong authority fails the fingerprint; and after `join` has pinned the
authority, every later connection verifies against it and nothing less.

Usage (the invitation carries these lines filled in):

    python3 a2a_client.py join --url wss://<host>:<port>/ws/a2a/<room> \
        --ticket <ticket> --ca-fp <fingerprint>
    python3 a2a_client.py wait <room>          # block until something is said
    python3 a2a_client.py read <room>          # new messages since last read
    python3 a2a_client.py say <room> "text"
    python3 a2a_client.py answer <room> "text" --reply-to <frame id>
    python3 a2a_client.py report <room> "done" --status completed --reply-to <id>
    python3 a2a_client.py leave <room>

State (the seat credential and the reading position) lives owner-only under
`~/.vaf-a2a-guest/<room>.json`. Losing the seat means being invited again;
that is the honest outcome for a bearer secret nobody wrote down.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import socket
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

PROTOCOL = "vaf-a2a"
VERSION = 1

#: RFC 6455 handshake constant, not a secret.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: What the server's refusal close codes mean, from the protocol document.
CLOSE_REASONS = {
    4001: "the credential was refused",
    4003: "the ticket or seat does not open this room",
    4004: "there is no such room on that machine",
    4009: "another connection is already writing as this peer",
}

#: Connection plumbing, never conversation: consumed here, not printed as messages.
_TRANSPORT_KINDS = frozenset({"welcome", "sync", "ack"})


class Refused(Exception):
    """The other end would not have it, and this is why."""

    def __init__(self, reason: str, code: int = 0):
        super().__init__(reason)
        self.reason, self.code = reason, code


# ── trust: the fingerprint decides, never the connection ───────────────────

def fingerprint_of(pem_text: str) -> str:
    """sha256 of the certificate's DER bytes, lowercase hex - the standard
    certificate fingerprint, the same number `vaf a2a invite` prints."""
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem_text)).hexdigest()


def fingerprints_match(left: str, right: str) -> bool:
    """Tolerant of colons, spaces and case: a fingerprint that only matches when
    pasted perfectly is a fingerprint people stop checking."""
    def norm(value):
        return "".join(c for c in str(value or "").lower() if c in "0123456789abcdef")
    a, b = norm(left), norm(right)
    return bool(a) and hmac.compare_digest(a.encode(), b.encode())


def fetch_ca(host: str, port: int, timeout: float = 10.0) -> str:
    """The host's CA as PEM text, fetched WITHOUT trusting the channel.

    Unverified on purpose and only here: the caller is about to check the result
    against a fingerprint that arrived by another route (the invitation), and
    nothing is spoken to until that check passes. Two sources, the download
    endpoint first, the TLS chain as fallback for a host that predates it.
    """
    import urllib.request
    bare = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    bare.check_hostname = False
    bare.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(
                f"https://{host}:{port}/api/a2a/ca.pem",
                context=bare, timeout=timeout) as resp:
            return resp.read().decode("ascii", "replace")
    except Exception:
        pass
    # The authority is the LAST link of the chain the server offers: the leaf
    # comes first, its issuer after it. Needs Python 3.13; older interpreters
    # get a clear ask instead of a worse guess.
    if not hasattr(ssl.SSLSocket, "get_unverified_chain"):
        raise Refused(
            "could not download the host's authority, and this Python cannot read "
            "the TLS chain (needs 3.13). Ask the host for its ca.pem and pass --ca-file")
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with bare.wrap_socket(raw, server_hostname=host) as tls:
                chain = tls.get_unverified_chain()
    except OSError as e:
        raise Refused(f"could not reach {host}:{port} - {e}") from None
    if not chain:
        raise Refused(f"{host}:{port} offered no certificate chain")
    return ssl.DER_cert_to_PEM_cert(chain[-1])


def pinned_context(ca_pem: str) -> ssl.SSLContext:
    """Verify the pinned authority and nothing less. Hostname checking is off
    because rooms are dialled by IP; what replaces it is stronger - only the
    machine whose CA the invitation named can present a certificate this
    context accepts."""
    context = ssl.create_default_context(cadata=ca_pem)
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    context.check_hostname = False
    return context


# ── the wire: a minimal RFC 6455 client ────────────────────────────────────

class WireSocket:
    """One WebSocket connection, client side, text frames.

    Small on purpose: masked frames out, unmasked frames in, ping answered with
    pong, close answered with close. A read timeout is an exception, and after
    one the connection is only good for closing - a frame may be half-read.
    """

    def __init__(self, sock: ssl.SSLSocket):
        self._sock = sock
        self._buffer = b""
        self.close_code = 0
        self.close_reason = ""

    @classmethod
    def connect(cls, host: str, port: int, resource: str, ca_pem: str,
                timeout: float = 10.0) -> "WireSocket":
        raw = socket.create_connection((host, port), timeout=timeout)
        tls = pinned_context(ca_pem).wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (f"GET {resource} HTTP/1.1\r\n"
                   f"Host: {host}:{port}\r\n"
                   "Upgrade: websocket\r\n"
                   "Connection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n")
        tls.sendall(request.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = tls.recv(4096)
            if not chunk:
                raise Refused("the server closed the connection during the handshake")
            head += chunk
            if len(head) > 65536:
                raise Refused("that is not a WebSocket handshake")
        header_block, _, rest = head.partition(b"\r\n\r\n")
        lines = header_block.decode("latin-1").split("\r\n")
        status = lines[0].split(" ", 2)
        if len(status) < 2 or status[1] != "101":
            raise Refused(f"the server refused the connection (HTTP {status[1] if len(status) > 1 else '?'})")
        accept = ""
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                accept = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        if accept != expected:
            raise Refused("the server's handshake answer does not prove it read ours")
        ws = cls(tls)
        ws._buffer = rest
        return ws

    def _read_exact(self, n: int) -> bytes:
        while len(self._buffer) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise Refused("the connection ended mid-frame")
            self._buffer += chunk
        out, self._buffer = self._buffer[:n], self._buffer[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += n.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, "big")
        mask = os.urandom(4)
        header += mask
        self._sock.sendall(bytes(header) +
                           bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self, timeout=None):
        """The next complete text message, or None once the server closed.
        Raises TimeoutError when nothing arrived in time."""
        self._sock.settimeout(timeout)
        parts = []
        while True:
            b1, b2 = self._read_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = int.from_bytes(self._read_exact(2), "big")
            elif n == 127:
                n = int.from_bytes(self._read_exact(8), "big")
            mask = self._read_exact(4) if b2 & 0x80 else b""
            data = self._read_exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:                      # ping: answer, keep reading
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:                      # pong: not ours to want
                continue
            if opcode == 0x8:                      # close: mirror it, report it
                self.close_code = int.from_bytes(data[:2], "big") if len(data) >= 2 else 1005
                self.close_reason = data[2:].decode("utf-8", "replace")
                try:
                    self._send_frame(0x8, data[:2])
                except OSError:
                    pass
                return None
            if opcode in (0x0, 0x1, 0x2):
                parts.append(data)
                if fin:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._send_frame(0x8, (1000).to_bytes(2, "big"))
            self._sock.settimeout(2)
            while self.recv_text(timeout=2) is not None:
                pass
        except Exception:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


# ── the room, spoken over the wire ─────────────────────────────────────────

def split_room_url(url: str) -> dict:
    parts = urlsplit(str(url or "").strip())
    if parts.scheme != "wss":
        raise Refused("a room is dialled over wss, never in the clear: the "
                      "credential travels in the URL")
    room_id = parts.path.rsplit("/", 1)[-1] if "/ws/a2a/" in parts.path else ""
    if not room_id:
        raise Refused("that URL does not name a room (expected .../ws/a2a/<room-id>)")
    return {"host": parts.hostname or "", "port": int(parts.port or 443),
            "path": parts.path, "room": room_id}


class RoomConnection:
    """One connection to one room: welcome consumed, frames both ways."""

    def __init__(self, wire: WireSocket, welcome: dict):
        self.wire = wire
        self.room = str(welcome.get("room") or "")
        self.peer = str(welcome.get("peer") or "")
        self.role = str(welcome.get("role") or "")
        self.seat = str(welcome.get("seat") or "") or None
        self.packet = welcome.get("welcome") if isinstance(welcome.get("welcome"), dict) else None
        # Frames that arrived while an ack was being awaited. submit() used to
        # DROP them - a message somebody sent while this side was confirming its
        # own would silently never be seen. next_frame() drains this first.
        self._buffer: list = []

    @classmethod
    def connect(cls, url: str, credential: str, ca_pem: str,
                timeout: float = 10.0) -> "RoomConnection":
        target = split_room_url(url)
        resource = f"{target['path']}?token={quote(str(credential or ''))}"
        wire = WireSocket.connect(target["host"], target["port"], resource,
                                  ca_pem, timeout=timeout)
        try:
            raw = wire.recv_text(timeout=timeout)
        except TimeoutError:
            wire.close()
            raise Refused("the server sent no welcome") from None
        if raw is None:
            code = wire.close_code
            raise Refused(CLOSE_REASONS.get(code, "the connection ended before the welcome"),
                          code=code)
        try:
            welcome = json.loads(raw)
        except ValueError:
            wire.close()
            raise Refused("the server did not answer with a room welcome") from None
        if not isinstance(welcome, dict) or welcome.get("kind") != "welcome":
            wire.close()
            raise Refused("the server did not answer with a room welcome")
        if welcome.get("protocol") != PROTOCOL or int(welcome.get("v") or 0) != VERSION:
            wire.close()
            raise Refused(f"the server speaks {welcome.get('protocol')!r} "
                          f"v{welcome.get('v')!r}, not {PROTOCOL} v{VERSION} - "
                          "leave rather than guess")
        return cls(wire, welcome)

    def backlog(self, timeout: float = 15.0) -> list:
        """Everything up to the sync marker, oldest first. After it, live traffic."""
        frames = []
        while True:
            try:
                raw = self.wire.recv_text(timeout=timeout)
            except TimeoutError:
                return frames               # an older host may not send the marker
            if raw is None:
                return frames
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("kind") == "sync":
                return frames
            frames.append(message)

    def next_frame(self, timeout=None):
        """The next live frame, or None once the connection ends."""
        if self._buffer:
            return self._buffer.pop(0)
        while True:
            raw = self.wire.recv_text(timeout=timeout)
            if raw is None:
                return None
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if isinstance(message, dict):
                return message

    def submit(self, payload: dict, timeout: float = 10.0) -> dict:
        """Send one payload, return the ack that answers it. Frames fanned out
        for other senders while the ack is awaited are KEPT for next_frame(),
        never dropped - a room keeps talking while this side confirms."""
        self.wire.send_text(json.dumps(payload, ensure_ascii=False))
        while True:
            try:
                raw = self.wire.recv_text(timeout=timeout)
            except TimeoutError:
                raise Refused("the server did not answer the frame") from None
            if raw is None:
                raise Refused(CLOSE_REASONS.get(self.wire.close_code, "the connection ended"),
                              code=self.wire.close_code)
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("kind") == "ack":
                return message
            self._buffer.append(message)

    def renew(self, timeout: float = 10.0) -> dict:
        """Keep the writer lease alive on a HELD connection.

        The host renews a lease only on a successful submit, and a connection
        that reads and thinks for longer than the 90 second lease TTL loses its
        write right while staying connected. Holding a line, send this at least
        every 90 seconds (25 is comfortable); the host answers
        {"kind": "ack", "status": "renewed"}. A host too old to know the verb
        answers with a refusal - take that ONE answer as "not spoken here" and
        stop asking; such a host renews on submits only. One-shot commands
        (connect, act, close) never need it: the close frees the lease.
        """
        return self.submit({"kind": "renew"}, timeout=timeout)

    def close(self) -> None:
        self.wire.close()


# ── the seat record: the way back in ───────────────────────────────────────

def state_dir() -> Path:
    directory = Path.home() / ".vaf-a2a-guest"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


def _checked_room_id(room_id: str) -> str:
    cleaned = str(room_id or "").strip()
    if not cleaned or not all(c.isalnum() or c in "_.-" for c in cleaned) \
            or cleaned[0] in "._-" or len(cleaned) > 64:
        raise Refused(f"that is not a room id: {cleaned!r}")
    return cleaned


def record_path(room_id: str) -> Path:
    return state_dir() / f"{_checked_room_id(room_id)}.json"


def load_record(room_id: str) -> dict:
    path = record_path(room_id)
    if not path.exists():
        raise Refused(f"no seat for {room_id} here - join with the invitation first")
    return json.loads(path.read_text(encoding="utf-8"))


def save_record(record: dict) -> Path:
    path = record_path(record["room"])
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


# ── reading: order, dedupe, cursor ─────────────────────────────────────────

def sort_key(frame: dict):
    """The total order: lamport, then sender, then that sender's sequence.
    `ts` is advisory and appears nowhere here."""
    return (int(frame.get("lamport") or 0), str(frame.get("from") or ""),
            int(frame.get("seq") or 0))


def fold_new(frames: list, record: dict, *, show_all: bool = False) -> list:
    """Deduped on id, in canonical order, past the cursor, own echo skipped.
    Advances the cursor over everything SEEN, shown or not - a skipped frame
    must never come back as news."""
    cursor = tuple(record.get("cursor") or (0, "", 0))
    seen, out = set(), []
    top = cursor
    for frame in sorted(frames, key=sort_key):
        kind = str(frame.get("kind") or "")
        if kind in _TRANSPORT_KINDS:
            continue
        key = str(frame.get("id") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        position = sort_key(frame)
        if position > top:
            top = position
        if not show_all:
            if position <= cursor:
                continue
            if str(frame.get("from") or "") == str(record.get("peer") or ""):
                continue
        out.append(frame)
    record["cursor"] = list(top)
    return out


# ── the commands ───────────────────────────────────────────────────────────

def _fail(reason: str, code: int = 1):
    print(f"error: {reason}", file=sys.stderr)
    raise SystemExit(code)


def _print_frame(frame: dict) -> None:
    print(json.dumps(frame, ensure_ascii=False))


def cmd_join(args) -> None:
    target = split_room_url(args.url)
    if args.ca_file:
        ca_pem = Path(args.ca_file).read_text(encoding="ascii")
    else:
        ca_pem = fetch_ca(target["host"], target["port"])
    actual = fingerprint_of(ca_pem)
    if not fingerprints_match(actual, args.ca_fp):
        raise Refused("the certificate at that address does not match the "
                      f"invitation: expected {str(args.ca_fp)[:16]}..., "
                      f"found {actual[:16]}...")
    connection = RoomConnection.connect(args.url, args.ticket, ca_pem)
    try:
        record = {"url": args.url, "room": connection.room, "peer": connection.peer,
                  "role": connection.role, "seat": connection.seat,
                  "ca_pem": ca_pem, "cursor": [0, "", 0]}
        backlog = connection.backlog()
        fold_new(backlog, record, show_all=False)   # start read AFTER history
        saved = save_record(record)
        summary = {"room": connection.room, "peer": connection.peer,
                   "role": connection.role, "seat_saved": str(saved),
                   "history": len(backlog)}
        if connection.seat is None:
            summary["note"] = ("the server minted no seat; reconnecting will "
                               "need a fresh invitation")
        if connection.packet:
            summary["welcome"] = connection.packet
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        connection.close()


def _open(record: dict) -> RoomConnection:
    credential = record.get("seat") or ""
    if not credential:
        raise Refused("this record holds no seat - join again with a fresh invitation")
    return RoomConnection.connect(record["url"], credential, record["ca_pem"])


def cmd_read(args) -> None:
    record = load_record(args.room)
    connection = _open(record)
    try:
        rows = fold_new(connection.backlog(), record, show_all=args.all)
    finally:
        connection.close()
    save_record(record)
    for frame in rows:
        _print_frame(frame)


def cmd_wait(args) -> None:
    record = load_record(args.room)
    connection = _open(record)
    try:
        rows = fold_new(connection.backlog(), record, show_all=False)
        if not rows:
            budget = args.timeout if args.timeout and args.timeout > 0 else None
            deadline = (time.monotonic() + budget) if budget else None
            # The wait holds the line, and a held line keeps its lease alive in
            # slices: wait a slice, renew, wait on. Without it the lease lapsed
            # after 90 quiet seconds while the connection stayed up. A host too
            # old to know the verb refuses it once and is not asked again.
            renew_spoken = True
            while True:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    save_record(record)
                    _fail("nothing was said in time", 1)
                slice_s = 25.0 if remaining is None else min(25.0, remaining)
                try:
                    frame = connection.next_frame(timeout=slice_s)
                except TimeoutError:
                    if renew_spoken:
                        try:
                            ack = connection.renew()
                            if str(ack.get("status") or "") != "renewed":
                                renew_spoken = False
                        except Exception:
                            pass
                    continue
                if frame is None:
                    save_record(record)
                    _fail(CLOSE_REASONS.get(connection.wire.close_code,
                                            "the connection ended"), 1)
                rows = fold_new([frame], record, show_all=False)
                if rows:
                    break
    finally:
        connection.close()
    save_record(record)
    for frame in rows:
        _print_frame(frame)


def _send(args, kind: str) -> None:
    record = load_record(args.room)
    body = {"text": args.text}
    if getattr(args, "status", ""):
        body["status"] = args.status
    payload = {"kind": kind, "body": body}
    if getattr(args, "reply_to", ""):
        payload["reply_to"] = args.reply_to
    if getattr(args, "to", ""):
        payload["to"] = {"peer": args.to}
    connection = _open(record)
    try:
        ack = connection.submit(payload)
    finally:
        connection.close()
    _print_frame(ack)
    if str(ack.get("status") or "") != "committed":
        raise SystemExit(2)


def cmd_leave(args) -> None:
    record = load_record(args.room)
    connection = _open(record)
    try:
        ack = connection.submit({"kind": "leave", "body": {}})
    finally:
        connection.close()
    _print_frame(ack)
    if str(ack.get("status") or "") == "committed":
        record_path(args.room).unlink(missing_ok=True)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Take part in a VAF agent room from a machine without VAF.")
    commands = parser.add_subparsers(dest="command", required=True)

    join = commands.add_parser("join", help="redeem an invitation, keep the seat")
    join.add_argument("--url", required=True, help="wss://<host>:<port>/ws/a2a/<room>")
    join.add_argument("--ticket", required=True, help="the single-use ticket from the invitation")
    join.add_argument("--ca-fp", required=True, help="the CA fingerprint from the invitation")
    join.add_argument("--ca-file", default="", help="the host's ca.pem, if you already have it")
    join.set_defaults(handler=cmd_join)

    read = commands.add_parser("read", help="print new messages, remember the position")
    read.add_argument("room")
    read.add_argument("--all", action="store_true", help="the whole transcript, own echo included")
    read.set_defaults(handler=cmd_read)

    wait = commands.add_parser("wait", help="block until something is said, print it")
    wait.add_argument("room")
    wait.add_argument("--timeout", type=float, default=0,
                      help="give up after this many seconds (default: wait forever)")
    wait.set_defaults(handler=cmd_wait)

    for kind, description in (("say", "tell the room something"),
                              ("answer", "answer a message"),
                              ("report", "report on work you took on")):
        sub = commands.add_parser(kind, help=description)
        sub.add_argument("room")
        sub.add_argument("text")
        sub.add_argument("--reply-to", dest="reply_to", default="",
                         help="the id of the message this responds to")
        sub.add_argument("--to", default="", help="address one member by peer id")
        if kind == "report":
            sub.add_argument("--status", default="working",
                             help="submitted, working, input_required, completed, "
                                  "failed, rejected, canceled")
        sub.set_defaults(handler=_send, kind=kind)

    leave = commands.add_parser("leave", help="leave the room, drop the seat")
    leave.add_argument("room")
    leave.set_defaults(handler=cmd_leave)

    args = parser.parse_args(argv)
    try:
        if args.handler is _send:
            _send(args, args.kind)
        else:
            args.handler(args)
    except Refused as refusal:
        _fail(refusal.reason, 2)
    except OSError as e:
        _fail(str(e), 1)


if __name__ == "__main__":
    main()
