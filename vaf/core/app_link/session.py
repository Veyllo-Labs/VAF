# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One inner TLS session of the app link, over memory BIOs, and its dispatcher.

Both carriers feed the same class: the relay hands it the TLS bytes behind a cid, the direct
WebSocket the bytes of its binary frames. Nothing is dispatched before the handshake is
complete and the purpose is clear:

- SNI ``pair.vaf.internal``: a pairing (pairing.py). A client certificate is ignored there;
  the one frame accepted is ``pair``.
- SNI ``link.vaf.internal``: a device. A client certificate verified against the device CA
  is required, and its public key must name a device row that stands, with the same serial.
  The account comes ONLY from that row. Then exactly one ``hello`` naming that device, then
  the messages (bridge.py). A certificate of the device CA whose key has no row any more (the
  device was removed) hears ``bye`` with ``removed``, so the app can tell it was unpaired;
  every other refusal (no certificate, another serial, a row that does not stand) closes
  without a word.

Every TLS error (tampered, replayed, reordered or truncated bytes) closes the session; the
frame reader refuses what is malformed. The limits VAF enforces by itself are constants
below, so a hostile relay or client cannot make this process hold more than they allow.
"""
from __future__ import annotations

import hashlib
import logging
import ssl
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

from vaf.core.app_link import protocol as pr

logger = logging.getLogger(__name__)

HANDSHAKE_SECONDS = 15
HANDSHAKE_MAX_BYTES = 64 * 1024
# After the handshake, the first message (`pair` on the pairing name, `hello` on the link
# name) must come within this time; nobody is known yet, so nothing else may hold a slot.
FIRST_MESSAGE_SECONDS = 20
# A pairing waits for the person's confirmation in the web UI (pairing.CONFIRM_SECONDS, 120 s,
# ended by the sweep); whatever happens to the pairing, its session ends after this long.
PAIR_WAIT_SECONDS = 180
# Until a device is known (state "open"), a frame is at most this big: `pair` is about 6 KiB
# (a 4 KiB CSR in base64 and short labels), `hello` far less. 1 MiB frames are for a device.
PRE_OPEN_MAX_PAYLOAD = 16 * 1024
MAX_BUFFERED = 4 * 1024 * 1024
MSG_PER_MINUTE = 30
# Requests of the app's own agent (`agent_ask`) one device may have queued or running at once;
# they count against MSG_PER_MINUTE too.
AGENT_ASK_OPEN_MAX = 3
MAX_TRANSFERS = 4
TRANSFER_SILENCE_SECONDS = 60
SESSIONS_PER_MINUTE_PER_CONNECTION = 30
SESSIONS_PER_MINUTE_PER_PEER = 6
MAX_SESSIONS = 500


class SessionLimiter:
    """New sessions per minute per carrier connection and per peer (a relay cid, a direct
    client address), and the sessions alive at once."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._per_conn: Dict[str, Deque[float]] = {}
        self._per_peer: Dict[str, Deque[float]] = {}
        self.live = 0

    @staticmethod
    def _hit(window: Dict[str, Deque[float]], key: str, now: float, limit: int) -> bool:
        times = window.setdefault(key, deque())
        while times and now - times[0] > 60:
            times.popleft()
        if len(times) >= limit:
            return False
        times.append(now)
        return True

    def admit(self, connection: str, peer: str) -> bool:
        now = self._clock()
        with self._lock:
            if self.live >= MAX_SESSIONS:
                return False
            if not self._hit(self._per_peer, peer, now, SESSIONS_PER_MINUTE_PER_PEER):
                return False
            if not self._hit(self._per_conn, connection, now, SESSIONS_PER_MINUTE_PER_CONNECTION):
                return False
            self.live += 1
            # Windows of peers that went quiet are dropped, so the maps stay bounded.
            for window in (self._per_conn, self._per_peer):
                for key in [k for k, v in window.items() if not v or now - v[-1] > 120]:
                    del window[key]
            return True

    def release(self) -> None:
        with self._lock:
            self.live = max(0, self.live - 1)

    def reset(self) -> None:
        with self._lock:
            self._per_conn.clear()
            self._per_peer.clear()
            self.live = 0


LIMITER = SessionLimiter()


@dataclass
class Transfer:
    xfer: bytes
    name: str
    mime: str
    size: int
    sha256: str
    owner_id: str
    spool: Any = None
    received: int = 0
    next_index: int = 0
    last_at: float = 0.0
    digest: Any = field(default_factory=hashlib.sha256)

    def done(self) -> bool:
        return self.received == self.size


@dataclass
class PendingMessage:
    msg: Dict[str, Any]
    transfers: List[Transfer]


class LinkSession:
    """One TLS session. ``send_bytes`` receives the outgoing TLS bytes, already cut into
    pieces that fit one frame. ``on_close`` runs once."""

    def __init__(self, transport: str, send_bytes: Callable[[bytes], None], peer_label: str, *,
                 clock: Callable[[], float] = time.monotonic,
                 on_close: Optional[Callable[["LinkSession"], None]] = None,
                 relay_cid: Optional[str] = None):
        from vaf.core.app_link import pki
        self.transport = transport
        self.peer_label = peer_label
        self.relay_cid = relay_cid
        self._send_bytes = send_bytes
        self._clock = clock
        self._on_close = on_close
        self._in, self._out = ssl.MemoryBIO(), ssl.MemoryBIO()
        self._tls = pki.server_context().wrap_bio(self._in, self._out, server_side=True)
        self._lock = threading.RLock()
        self._reader = pr.FrameReader(PRE_OPEN_MAX_PAYLOAD)
        self.started_at = clock()
        self.handshake_done_at: Optional[float] = None
        self.pair_wait_since: Optional[float] = None
        self.state = "handshake"
        self.purpose: Optional[str] = None
        self.device: Optional[Dict[str, Any]] = None
        # The app on this connection answered `unknown_type` for a confirmation card: it gets
        # no card any more (vaf/core/app_link/confirmations.py).
        self.confirm_unsupported = False
        self.closed = False
        self.close_reason = ""
        self.dispatched = 0
        self._handshake_bytes = 0
        self._transfers: Dict[bytes, Transfer] = {}
        self._pending: List[PendingMessage] = []

    # ── bytes in, bytes out ─────────────────────────────────────────────────

    def feed(self, data: bytes) -> None:
        with self._lock:
            if self.closed:
                return
            if self.state == "handshake":
                self._handshake_bytes += len(data)
                if self._handshake_bytes > HANDSHAKE_MAX_BYTES:
                    self.close("handshake_too_large", bye=False)
                    return
            if self._in.pending + len(data) + self._reader.buffered > MAX_BUFFERED:
                self.close("buffer_full", bye=False)
                return
            self._in.write(data)
            if self.state == "handshake" and not self._handshake():
                return
            self._read_plaintext()

    def _flush(self) -> None:
        out = self._out.read()
        if not out:
            return
        for piece in pr.split_tls(out):
            try:
                self._send_bytes(piece)
            except Exception:
                logger.debug("app link: sending TLS bytes failed", exc_info=True)

    def _handshake(self) -> bool:
        try:
            self._tls.do_handshake()
        except ssl.SSLWantReadError:
            self._flush()
            return False
        except (ssl.SSLError, OSError, ValueError) as exc:
            self._flush()
            self.close(f"tls:{getattr(exc, 'reason', None) or type(exc).__name__}", bye=False)
            return False
        self._flush()
        self.handshake_done_at = self._clock()
        sni = getattr(self._tls, "vaf_sni", None)
        if self._tls.selected_alpn_protocol() != pr.ALPN or sni not in (pr.SNI_LINK, pr.SNI_PAIR):
            self.close("alpn_or_sni", bye=False)
            return False
        if sni == pr.SNI_PAIR:
            self.purpose, self.state = "pair", "pair"
            return True
        der = self._tls.getpeercert(binary_form=True)
        row = _device_for_certificate(der)
        if row is None:
            if der and _row_for_key(der) is None and _certificate(der) is not None:
                # TLS verified this certificate against the device CA; no row holds its key:
                # the device was removed. Said inside the session, so only that device hears it.
                self.state = "closing"
                self.close("removed", bye=True)
                return False
            self.close("unknown_device", bye=False)
            return False
        self.device = row
        self.purpose, self.state = "link", "await_hello"
        return True

    def _read_plaintext(self) -> None:
        chunks = []
        while True:
            try:
                data = self._tls.read(65536)
            except ssl.SSLWantReadError:
                break
            except ssl.SSLZeroReturnError:
                self.close("peer_closed", bye=False)
                return
            except (ssl.SSLError, OSError, ValueError) as exc:
                self.close(f"tls:{getattr(exc, 'reason', None) or type(exc).__name__}", bye=False)
                return
            if not data:
                break
            chunks.append(data)
        self._flush()
        if not chunks:
            return
        self._reader.push(b"".join(chunks))
        while not self.closed:
            # One frame at a time: a hello raises the frame limit for what follows it.
            try:
                frame = self._reader.next_frame()
            except pr.FrameError:
                self.close("bad_frame", bye=False)
                return
            if frame is None:
                return
            try:
                self._dispatch(frame)
            except Exception:
                logger.warning("app link: a message could not be handled", exc_info=True)
                self.close("internal", bye=True)
                return

    def _write(self, data: bytes) -> bool:
        with self._lock:
            if self.closed or self.state in ("handshake",):
                return False
            try:
                self._tls.write(data)
            except (ssl.SSLError, OSError, ValueError):
                self.close("write_failed", bye=False)
                return False
            self._flush()
            return True

    def send_json(self, obj: Dict[str, Any]) -> bool:
        return self._write(pr.encode_json(obj))

    def send_chunk(self, xfer: bytes, index: int, data: bytes) -> bool:
        return self._write(pr.encode_chunk(xfer, index, data))

    def send_file(self, xfer: bytes, source) -> bool:
        """The chunks of one file: ``source`` is a path, or the file's bytes (a spooled file)."""
        if isinstance(source, (bytes, bytearray)):
            data = bytes(source)
            for index, start in enumerate(range(0, len(data), pr.CHUNK_DATA_MAX)):
                if not self.send_chunk(xfer, index, data[start:start + pr.CHUNK_DATA_MAX]):
                    return False
            return True
        with open(source, "rb") as handle:
            index = 0
            while True:
                data = handle.read(pr.CHUNK_DATA_MAX)
                if not data:
                    return True
                if not self.send_chunk(xfer, index, data):
                    return False
                index += 1

    def close(self, reason: str = "", *, bye: bool = True) -> None:
        with self._lock:
            if self.closed:
                return
            if bye and self.state not in ("handshake",):
                try:
                    self._tls.write(pr.encode_json({"t": "bye", "reason": reason} if reason
                                                   else {"t": "bye"}))
                    self._tls.unwrap()
                except (ssl.SSLError, OSError, ValueError):
                    pass
                self._flush()
            self.closed = True
            self.close_reason = reason
            self.state = "closed"
            for transfer in self._transfers.values():
                _drop_spool(transfer)
            self._transfers.clear()
            self._pending.clear()
        if reason and not reason.startswith(("peer_closed", "replaced", "bye")):
            logger.info("app link: %s session closed (%s, %s)", self.transport, reason,
                        self.peer_label)
        if self._on_close is not None:
            try:
                self._on_close(self)
            except Exception:
                pass

    # ── timeouts, from the sweep ────────────────────────────────────────────

    def check_timeouts(self, now: Optional[float] = None) -> None:
        now = self._clock() if now is None else now
        with self._lock:
            if self.closed:
                return
            if self.state == "handshake" and now - self.started_at > HANDSHAKE_SECONDS:
                self.close("handshake_timeout", bye=False)
                return
            if self.state in ("pair", "await_hello") and self.handshake_done_at is not None \
                    and now - self.handshake_done_at > FIRST_MESSAGE_SECONDS:
                self.close("first_message_timeout", bye=False)
                return
            if self.state == "pair_wait" and self.pair_wait_since is not None \
                    and now - self.pair_wait_since > PAIR_WAIT_SECONDS:
                self.close("pair_wait_timeout", bye=False)
                return
            for transfer in list(self._transfers.values()):
                # A finished file waits for the other files of its message: no more chunks
                # come for it, so its silence means nothing.
                if transfer.done():
                    continue
                if now - transfer.last_at > TRANSFER_SILENCE_SECONDS:
                    self._abort_transfer(transfer.xfer)

    # ── what the app sends ──────────────────────────────────────────────────

    def _dispatch(self, frame: pr.Frame) -> None:
        if frame.type == pr.FRAME_CHUNK:
            self._on_chunk(frame)
            return
        obj = frame.obj or {}
        if self.state == "pair":
            message = self._validate(obj, pr.PAIR_TYPES_FROM_APP)
            if not isinstance(message, dict):
                self.send_json({"t": "error", "code": "unexpected"})
                self.close("unexpected", bye=False)
                return
            self.state = "pair_wait"
            self.pair_wait_since = self._clock()
            self.dispatched += 1
            from vaf.core.app_link import pairing
            pairing.on_pair(self, message)
            return
        if self.state == "pair_wait":
            # Nothing but the confirmation moves a pairing on; the app only waits.
            message = self._validate(obj, ("bye",))
            if isinstance(message, dict):
                self.close("bye", bye=False)
            return
        message = self._validate(obj, pr.MESSAGE_TYPES_FROM_APP)
        if isinstance(message, pr.UnknownType):
            self.send_json({"t": "error", "code": "unknown_type", "ref": message.t})
            return
        if message is None:
            return
        if self.state == "await_hello":
            if message["t"] != "hello":
                self.send_json({"t": "error", "code": "unexpected", "ref": message["t"]})
                self.close("no_hello", bye=False)
                return
            if self.device is None or message["device_id"] != self.device["device_id"]:
                self.send_json({"t": "error", "code": "wrong_device"})
                self.close("wrong_device", bye=False)
                return
            self.state = "open"
            self._reader.max_payload = pr.MAX_FRAME_PAYLOAD
            self.dispatched += 1
            from vaf.core.app_link import bridge
            bridge.on_hello(self, message)
            return
        if self.state != "open":
            return
        if message["t"] == "bye":
            self.close("bye", bye=False)
            return
        if message["t"] == "hello":
            self.send_json({"t": "error", "code": "unexpected", "ref": "hello"})
            return
        if message["t"] in ("msg", "task_result") and message.get("attachments"):
            self._announce(message)
            return
        if message["t"] == "error" and message.get("code") == "xfer_abort":
            self._drop_by_ref(message.get("ref", ""))
        self.dispatched += 1
        from vaf.core.app_link import bridge
        bridge.on_message(self, message, files=[])

    def _validate(self, obj: Dict[str, Any], allowed):
        try:
            return pr.validate_from_app(obj, allowed)
        except pr.ProtocolError as exc:
            self.send_json({"t": "error", "code": exc.code, "ref": exc.ref})
            return None

    # ── inbound files ───────────────────────────────────────────────────────

    def _announce(self, message: Dict[str, Any]) -> None:
        owner = message["id"]
        if len(self._transfers) + len(message["attachments"]) > MAX_TRANSFERS:
            self.send_json({"t": "error", "code": "xfer_abort", "ref": owner})
            return
        transfers = []
        for item in message["attachments"]:
            if item["xfer"] in self._transfers:
                self.send_json({"t": "error", "code": "xfer_abort", "ref": pr.b64url(item["xfer"])})
                return
            transfers.append(Transfer(xfer=item["xfer"], name=item["name"], mime=item["mime"],
                                      size=item["size"], sha256=item["sha256"], owner_id=owner,
                                      spool=tempfile.SpooledTemporaryFile(max_size=1024 * 1024),
                                      last_at=self._clock()))
        for transfer in transfers:
            self._transfers[transfer.xfer] = transfer
        self._pending.append(PendingMessage(msg=message, transfers=transfers))

    def _on_chunk(self, frame: pr.Frame) -> None:
        transfer = self._transfers.get(frame.xfer)
        if transfer is None:
            self.send_json({"t": "error", "code": "xfer_abort", "ref": pr.b64url(frame.xfer)})
            return
        if frame.index != transfer.next_index or transfer.received + len(frame.data) > transfer.size:
            self._abort_transfer(frame.xfer)
            return
        transfer.spool.write(frame.data)
        transfer.digest.update(frame.data)
        transfer.received += len(frame.data)
        transfer.next_index += 1
        transfer.last_at = self._clock()
        if transfer.done():
            if transfer.digest.hexdigest() != transfer.sha256:
                self._abort_transfer(frame.xfer)
                return
            self._complete_pending()

    def _complete_pending(self) -> None:
        ready = [p for p in self._pending if all(t.done() for t in p.transfers)]
        for pending in ready:
            self._pending.remove(pending)
            files = []
            for transfer in pending.transfers:
                self._transfers.pop(transfer.xfer, None)
                transfer.spool.seek(0)
                files.append({"name": transfer.name, "mime": transfer.mime, "size": transfer.size,
                              "data": transfer.spool.read()})
                _drop_spool(transfer)
            self.dispatched += 1
            from vaf.core.app_link import bridge
            bridge.on_message(self, pending.msg, files=files)

    def _abort_transfer(self, xfer: bytes) -> None:
        transfer = self._transfers.pop(xfer, None)
        self.send_json({"t": "error", "code": "xfer_abort", "ref": pr.b64url(xfer)})
        if transfer is None:
            return
        _drop_spool(transfer)
        # A message whose file failed is not handed on half; the app sends it again.
        for pending in [p for p in self._pending if transfer in p.transfers]:
            self._pending.remove(pending)
            for other in pending.transfers:
                if other is not transfer:
                    self._transfers.pop(other.xfer, None)
                    _drop_spool(other)

    def _drop_by_ref(self, ref: str) -> None:
        try:
            xfer = pr.b64url_decode(ref)
        except ValueError:
            return
        if xfer in self._transfers:
            transfer = self._transfers.pop(xfer)
            _drop_spool(transfer)
            for pending in [p for p in self._pending if transfer in p.transfers]:
                self._pending.remove(pending)


def _drop_spool(transfer: Transfer) -> None:
    try:
        if transfer.spool is not None:
            transfer.spool.close()
    except Exception:
        pass


def _certificate(der: Optional[bytes]):
    if not der:
        return None
    from cryptography import x509
    try:
        return x509.load_der_x509_certificate(der)
    except Exception:
        return None


def _row_for_key(der: Optional[bytes]) -> Optional[Dict[str, Any]]:
    """The device row holding a certificate's public key, whatever its state or serial."""
    cert = _certificate(der)
    if cert is None:
        return None
    from vaf.core.app_link import devices, pki
    return devices.by_spki(pki.spki_sha256(cert.public_key()))


def _device_for_certificate(der: Optional[bytes]) -> Optional[Dict[str, Any]]:
    """The device row a verified client certificate names, when it stands and its serial is
    the one issued at pairing; else None."""
    cert = _certificate(der)
    if cert is None:
        return None
    from vaf.core.app_link import devices
    row = _row_for_key(der)
    if row is None or str(row.get("cert_serial")) != str(cert.serial_number):
        return None
    if not devices.row_stands(row):
        return None
    return row
