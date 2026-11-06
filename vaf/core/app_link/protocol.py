# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The app link's wire format, version 1: the pairing QR, the frames inside the TLS stream,
the relay envelope around TLS bytes, and the one list of message types the app may send.

Pure and stdlib only, so the vectors (tests/fixtures/app_link/, scripts/app_link_vectors.py)
are produced by exactly the code that runs. Nothing here decides who a peer is: identity
comes from the device certificate in session.py; this module only refuses what is malformed.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import struct
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

PROTOCOL_VERSION = 1
ALPN = "vaf-link/1"
SNI_LINK = "link.vaf.internal"
SNI_PAIR = "pair.vaf.internal"
QR_BASE = "https://veyllo.app/link"
MAX_FRAME_PAYLOAD = 1048576
# 1 MiB minus 32 KiB, so a chunk with its frame head and the TLS overhead fits one relay
# billing unit of 1 MiB wire bytes (protocol section 4).
CHUNK_DATA_MAX = 1015808
RELAY_HEAD = 17
MAX_TLS_BYTES_PER_FRAME = MAX_FRAME_PAYLOAD - RELAY_HEAD
PAIRING_TTL_SECONDS = 600
K_BYTES = 32
P_BYTES = 32
XFER_ID_BYTES = 16
CID_BYTES = 16

FRAME_JSON = 1
FRAME_CHUNK = 2
_HEAD = struct.Struct(">BI")
_CHUNK_HEAD = struct.Struct(">16sI")
MAX_JSON_DEPTH = 4

# The message types the app may send in a link session, and in a pairing session. The ONE
# list: the validator, the dispatcher and the tests read it.
MESSAGE_TYPES_FROM_APP = ("hello", "msg", "ack", "read", "typing", "cancel", "caps",
                          "task_result", "error", "bye", "agent_ask")
PAIR_TYPES_FROM_APP = ("pair",)
TASK_ERRORS = ("declined", "unavailable", "timeout", "failed")
PAIR_ERRORS = ("expired", "used", "denied", "bad_csr", "busy")

MAX_TEXT = 65536
MAX_ATTACHMENTS = 10
MAX_FILE_BYTES = 26214400
MAX_CAPS = 32
MAX_CSR_BYTES = 4096
MAX_LABEL = 64
_CAP = re.compile(r"[a-z_]{1,32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_B64URL = re.compile(r"[A-Za-z0-9_-]*")


class FrameError(ValueError):
    """Bytes that are not a frame of this protocol. The session closes on it."""


class ProtocolError(ValueError):
    """A well-framed message with a missing or wrong field. ``code`` goes back as an error."""

    def __init__(self, code: str, ref: str = ""):
        super().__init__(code)
        self.code = code
        self.ref = ref


@dataclass(frozen=True)
class UnknownType:
    """A message whose ``t`` this side does not take (protocol section 4: ignore, answer
    ``unknown_type``)."""
    t: str


@dataclass(frozen=True)
class Frame:
    type: int
    obj: Optional[Dict[str, Any]] = None
    xfer: bytes = b""
    index: int = 0
    data: bytes = b""


# ── base64url ────────────────────────────────────────────────────────────────

def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    if not isinstance(text, str) or not _B64URL.fullmatch(text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ── the QR ───────────────────────────────────────────────────────────────────

_QR_ORDER = ("v", "m", "p", "api", "k", "ca", "d", "n", "exp")
MAX_NAME_CHARS = 64
MAX_NAME_BYTES = 128


def cut_name(name: str) -> str:
    """The display name cut to 64 characters and 128 bytes of UTF-8, never inside a
    character (protocol section 9)."""
    text = str(name or "")[:MAX_NAME_CHARS]
    while len(text.encode("utf-8")) > MAX_NAME_BYTES:
        text = text[:-1]
    return text


def build_qr(mode: str, *, k: bytes, ca_sha256: bytes, exp: int, p: Optional[bytes] = None,
             api: Optional[str] = None, direct: Sequence[str] = (),
             name: Optional[str] = None) -> str:
    """The pairing URL. Every secret sits in the fragment, which a browser never sends."""
    if mode not in ("r", "l"):
        raise ValueError("mode is r or l")
    if len(k) != K_BYTES or len(ca_sha256) != 32:
        raise ValueError("k and ca are 32 bytes")
    direct = [str(d) for d in direct if str(d)]
    if mode == "r":
        if p is None or api is None:
            raise ValueError("a relay QR carries p and api")
        if len(p) != P_BYTES:
            raise ValueError("p is 32 bytes")
    else:
        if p is not None or api is not None:
            raise ValueError("a local QR carries neither p nor api")
        if not direct:
            raise ValueError("a local QR needs a direct address")
    fields: Dict[str, str] = {"v": str(PROTOCOL_VERSION), "m": mode}
    if mode == "r":
        fields["p"] = b64url(p)  # type: ignore[arg-type]
        fields["api"] = urllib.parse.quote(api, safe="")  # type: ignore[arg-type]
    fields["k"] = b64url(k)
    fields["ca"] = b64url(ca_sha256)
    if direct:
        fields["d"] = ",".join(urllib.parse.quote(d, safe="") for d in direct)
    cut = cut_name(name or "")
    if cut:
        fields["n"] = urllib.parse.quote(cut, safe="")
    fields["exp"] = str(int(exp))
    return QR_BASE + "#" + "&".join(f"{key}={fields[key]}" for key in _QR_ORDER if key in fields)


def parse_qr(url: str) -> Dict[str, Any]:
    """The fields of a pairing URL. Unknown fields are ignored; a missing required field or
    a wrong length raises ``ValueError``."""
    base, _, fragment = str(url).partition("#")
    if base != QR_BASE or not fragment:
        raise ValueError("not a pairing URL")
    raw: Dict[str, str] = {}
    for part in fragment.split("&"):
        key, sep, value = part.partition("=")
        if sep and key not in raw:
            raw[key] = value
    if raw.get("v") != str(PROTOCOL_VERSION) or raw.get("m") not in ("r", "l"):
        raise ValueError("unsupported version or mode")
    out: Dict[str, Any] = {"v": PROTOCOL_VERSION, "m": raw["m"]}
    for key in ("k", "ca", "exp"):
        if key not in raw:
            raise ValueError(f"missing {key}")
    out["k"] = b64url_decode(raw["k"])
    out["ca"] = b64url_decode(raw["ca"])
    if len(out["k"]) != K_BYTES or len(out["ca"]) != 32:
        raise ValueError("wrong length")
    out["exp"] = int(raw["exp"])
    out["d"] = [urllib.parse.unquote(d) for d in raw["d"].split(",")] if raw.get("d") else []
    if "n" in raw:
        out["n"] = urllib.parse.unquote(raw["n"])
    if raw["m"] == "r":
        if "p" not in raw or "api" not in raw:
            raise ValueError("a relay QR carries p and api")
        out["p"] = b64url_decode(raw["p"])
        if len(out["p"]) != P_BYTES:
            raise ValueError("wrong length")
        out["api"] = urllib.parse.unquote(raw["api"])
    elif "p" in raw or "api" in raw or not out["d"]:
        raise ValueError("a local QR carries a direct address and no p or api")
    return out


# ── inner frames ─────────────────────────────────────────────────────────────

def encode_json(obj: Dict[str, Any]) -> bytes:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME_PAYLOAD:
        raise FrameError("payload too large")
    return _HEAD.pack(FRAME_JSON, len(payload)) + payload


def encode_chunk(xfer: bytes, index: int, data: bytes) -> bytes:
    if len(xfer) != XFER_ID_BYTES:
        raise FrameError("xfer is 16 bytes")
    if len(data) > CHUNK_DATA_MAX:
        raise FrameError("chunk too large")
    payload = _CHUNK_HEAD.pack(xfer, index) + data
    return _HEAD.pack(FRAME_CHUNK, len(payload)) + payload


def _no_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise FrameError("duplicate key")
        out[key] = value
    return out


def _depth(value: Any) -> int:
    deepest, stack = 0, [(value, 1)]
    while stack:
        node, level = stack.pop()
        if isinstance(node, dict):
            deepest = max(deepest, level)
            stack.extend((v, level + 1) for v in node.values())
        elif isinstance(node, list):
            deepest = max(deepest, level)
            stack.extend((v, level + 1) for v in node)
    return deepest


def decode_json_payload(payload: bytes) -> Dict[str, Any]:
    try:
        obj = json.loads(payload.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except FrameError:
        raise
    except (ValueError, RecursionError) as exc:
        raise FrameError("not JSON") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("t"), str):
        raise FrameError("not a message object")
    if _depth(obj) > MAX_JSON_DEPTH:
        raise FrameError("nested too deep")
    return obj


class FrameReader:
    """Reassembles frames from TLS plaintext cut anywhere."""

    def __init__(self, max_payload: int = MAX_FRAME_PAYLOAD) -> None:
        self._buf = bytearray()
        # The largest payload accepted right now; a session lowers it until a device is known.
        self.max_payload = max_payload

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def push(self, data: bytes) -> None:
        self._buf += data

    def next_frame(self) -> Optional[Frame]:
        """The next complete frame, judged against the limit that holds NOW, or None. A
        reader that dispatches each frame before asking for the next one lets a message (a
        device's hello) change the limit for the frames after it."""
        if len(self._buf) < _HEAD.size:
            return None
        kind, length = _HEAD.unpack_from(self._buf)
        if kind not in (FRAME_JSON, FRAME_CHUNK):
            raise FrameError("unknown frame type")
        if length > min(self.max_payload, MAX_FRAME_PAYLOAD):
            raise FrameError("payload too large")
        if len(self._buf) < _HEAD.size + length:
            return None
        payload = bytes(self._buf[_HEAD.size:_HEAD.size + length])
        del self._buf[:_HEAD.size + length]
        if kind == FRAME_JSON:
            return Frame(FRAME_JSON, obj=decode_json_payload(payload))
        if length < _CHUNK_HEAD.size or length - _CHUNK_HEAD.size > CHUNK_DATA_MAX:
            raise FrameError("bad chunk")
        xfer, index = _CHUNK_HEAD.unpack_from(payload)
        return Frame(FRAME_CHUNK, xfer=xfer, index=index, data=payload[_CHUNK_HEAD.size:])

    def feed(self, data: bytes) -> List[Frame]:
        self.push(data)
        frames: List[Frame] = []
        while True:
            frame = self.next_frame()
            if frame is None:
                return frames
            frames.append(frame)


# ── the relay envelope ───────────────────────────────────────────────────────

def wrap_relay(cid: bytes, tls_bytes: bytes) -> bytes:
    if len(cid) != CID_BYTES:
        raise FrameError("cid is 16 bytes")
    if len(tls_bytes) > MAX_TLS_BYTES_PER_FRAME:
        raise FrameError("too many TLS bytes for one frame")
    return b"\x01" + cid + tls_bytes


def unwrap_relay(frame: bytes) -> Tuple[bytes, bytes]:
    if len(frame) < RELAY_HEAD or frame[0] != 0x01:
        raise FrameError("not a relay frame")
    return bytes(frame[1:RELAY_HEAD]), bytes(frame[RELAY_HEAD:])


def split_tls(data: bytes) -> List[bytes]:
    """Outgoing TLS bytes in pieces that fit one frame, on BOTH transports, so the inner
    stream is the same whichever way it travels."""
    return [data[i:i + MAX_TLS_BYTES_PER_FRAME]
            for i in range(0, len(data), MAX_TLS_BYTES_PER_FRAME)]


# ── what the app may send ────────────────────────────────────────────────────

def _uuid(value: Any, field: str = "id") -> str:
    try:
        if not isinstance(value, str) or len(value) != 36:
            raise ValueError
        return str(uuid.UUID(value))
    except ValueError:
        raise ProtocolError("bad_field", field) from None


def _label(value: Any, field: str, *, required: bool = True, limit: int = MAX_LABEL) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value or len(value) > limit or not value.isprintable():
        raise ProtocolError("bad_field", field)
    return value


def _text(value: Any, field: str = "text", *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise ProtocolError("bad_field", field)
    return value


def _caps(value: Any) -> List[str]:
    if not isinstance(value, list) or len(value) > MAX_CAPS:
        raise ProtocolError("bad_field", "caps")
    out: List[str] = []
    for cap in value:
        if not isinstance(cap, str) or not _CAP.fullmatch(cap):
            raise ProtocolError("bad_field", "caps")
        if cap not in out:
            out.append(cap)
    return out


def _attachments(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
        raise ProtocolError("bad_field", "attachments")
    out: List[Dict[str, Any]] = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise ProtocolError("bad_field", "attachments")
        try:
            xfer = b64url_decode(item.get("xfer"))
        except (ValueError, binascii.Error):
            raise ProtocolError("bad_field", "xfer") from None
        name = item.get("name")
        mime = item.get("mime")
        size = item.get("size")
        digest = item.get("sha256")
        if len(xfer) != XFER_ID_BYTES or xfer in seen:
            raise ProtocolError("bad_field", "xfer")
        if (not isinstance(name, str) or not 1 <= len(name) <= 255 or "/" in name
                or "\\" in name or name in (".", "..") or not name.isprintable()):
            raise ProtocolError("bad_field", "name")
        if not isinstance(mime, str) or len(mime) > 127 or not mime.isprintable():
            raise ProtocolError("bad_field", "mime")
        if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= MAX_FILE_BYTES:
            raise ProtocolError("bad_field", "size")
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            raise ProtocolError("bad_field", "sha256")
        seen.add(xfer)
        out.append({"xfer": xfer, "name": name, "mime": mime, "size": size, "sha256": digest})
    return out


def validate_from_app(obj: Dict[str, Any], allowed: Iterable[str] = MESSAGE_TYPES_FROM_APP):
    """The checked message, or ``UnknownType`` for a type this side does not take. Raises
    ``ProtocolError`` on a wrong field."""
    t = obj.get("t")
    if t not in tuple(allowed):
        return UnknownType(str(t)[:32])
    if t == "hello":
        if obj.get("v") != PROTOCOL_VERSION:
            raise ProtocolError("unsupported_version")
        return {"t": t, "device_id": _uuid(obj.get("device_id"), "device_id"),
                "app_version": _label(obj.get("app_version"), "app_version", required=False),
                "caps": _caps(obj.get("caps", []))}
    if t == "msg":
        out = {"t": t, "id": _uuid(obj.get("id")), "chat": _label(obj.get("chat"), "chat"),
               "text": _text(obj.get("text", "")),
               "attachments": _attachments(obj.get("attachments"))}
        if obj.get("reply_to") is not None:
            out["reply_to"] = _uuid(obj.get("reply_to"), "reply_to")
        if not out["text"].strip() and not out["attachments"]:
            # Nothing to hand the agent: an empty turn is refused, not queued.
            raise ProtocolError("bad_field", "text")
        return out
    if t == "agent_ask":
        # A task the app's own agent gives VAF (protocol section 10): its own type, so a VAF
        # without it answers `unknown_type` instead of taking it for the person's words. No
        # files in v1: an announced attachment is refused, so no transfer ever starts.
        out = {"t": t, "id": _uuid(obj.get("id")),
               "chat": _label(obj.get("chat") if obj.get("chat") != "" else None, "chat",
                              required=False),
               "text": _text(obj.get("text"))}
        if not out["text"].strip():
            raise ProtocolError("bad_field", "text")
        if obj.get("attachments"):
            raise ProtocolError("bad_field", "attachments")
        return out
    if t in ("ack", "read", "cancel"):
        return {"t": t, "id": _uuid(obj.get("id"))}
    if t == "typing":
        if not isinstance(obj.get("on"), bool):
            raise ProtocolError("bad_field", "on")
        return {"t": t, "chat": _label(obj.get("chat"), "chat"), "on": obj["on"]}
    if t == "caps":
        return {"t": t, "caps": _caps(obj.get("caps"))}
    if t == "task_result":
        ok = obj.get("ok")
        if not isinstance(ok, bool):
            raise ProtocolError("bad_field", "ok")
        error = obj.get("error")
        if error is not None and error not in TASK_ERRORS:
            raise ProtocolError("bad_field", "error")
        if not ok and error is None:
            error = "failed"
        return {"t": t, "id": _uuid(obj.get("id")), "ok": ok,
                "text": _text(obj.get("text"), required=False),
                "attachments": _attachments(obj.get("attachments")), "error": error}
    if t == "error":
        ref = obj.get("ref")
        return {"t": t, "code": _label(obj.get("code"), "code"),
                "ref": ref if isinstance(ref, str) and len(ref) <= 128 else ""}
    if t == "bye":
        reason = obj.get("reason")
        return {"t": t, "reason": reason if isinstance(reason, str) and len(reason) <= 64 else ""}
    if t == "pair":
        if obj.get("v") != PROTOCOL_VERSION:
            raise ProtocolError("unsupported_version")
        k = obj.get("k")
        if not isinstance(k, str) or len(k) != 43 or not _B64URL.fullmatch(k):
            raise ProtocolError("bad_field", "k")
        csr_text = obj.get("csr")
        if not isinstance(csr_text, str) or len(csr_text) > (MAX_CSR_BYTES * 4) // 3 + 4:
            raise ProtocolError("bad_csr")
        try:
            csr = base64.b64decode(csr_text, validate=True)
        except (ValueError, binascii.Error):
            raise ProtocolError("bad_csr") from None
        if not csr or len(csr) > MAX_CSR_BYTES:
            raise ProtocolError("bad_csr")
        return {"t": t, "k": k, "csr": csr, "device": _label(obj.get("device"), "device"),
                "platform": _label(obj.get("platform"), "platform"),
                "app_version": _label(obj.get("app_version"), "app_version")}
    return UnknownType(str(t))  # pragma: no cover - every listed type is handled above
