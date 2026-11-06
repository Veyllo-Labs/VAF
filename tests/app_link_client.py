# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A Veyllo App stand-in for the tests: the client half of the inner TLS over memory BIOs,
fed straight into a `LinkSession` (no port, no thread), and a device made the way pairing
makes one."""
import base64
import ssl
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from vaf.core.app_link import protocol as pr


def device_key():
    return ec.generate_private_key(ec.SECP256R1())


def csr_der(key) -> bytes:
    return (x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "phone")]))
            .sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.DER))


def paired_device(scope: str, username: str = "alice", *, model: str = "Pixel 9",
                  relay_member: Optional[str] = None):
    """(row, key, certificate DER) of a device as a confirmed pairing leaves it."""
    from vaf.core.app_link import devices, pki
    key = device_key()
    device_id = str(uuid.uuid4())
    der = pki.issue_device_cert(key.public_key(), device_id)
    serial = str(x509.load_der_x509_certificate(der).serial_number)
    row = devices.add_device(user_scope_id=scope, username=username,
                             spki_sha256=pki.spki_sha256(key.public_key()), cert_serial=serial,
                             model=model, platform="android", app_version="1.4.0",
                             device_id=device_id, relay_member=relay_member)
    return row, key, der


class Pipe:
    """A carrier: what the session sends lands in `to_client`; `feed` hands bytes to it."""

    def __init__(self, transport: str = "direct", peer: str = "10.0.0.5", connection: str = "c1",
                 relay_cid: Optional[str] = None):
        from vaf.core.app_link import bridge
        self.to_client = bytearray()
        self.session = bridge.new_session(transport, self._from_server, connection=connection,
                                          peer=peer, relay_cid=relay_cid)

    def _from_server(self, data: bytes) -> None:
        assert len(data) <= pr.MAX_TLS_BYTES_PER_FRAME
        self.to_client += data

    def feed(self, data: bytes) -> None:
        self.session.feed(data)


class AppClient:
    def __init__(self, ca_der: bytes, *, sni: Optional[str] = pr.SNI_LINK, key=None,
                 cert_der: Optional[bytes] = None, alpn: bool = True,
                 to_server: Optional[Callable[[bytes], None]] = None):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.load_verify_locations(cadata=ca_der)
        context.check_hostname = bool(sni)
        if not sni:
            context.verify_mode = ssl.CERT_REQUIRED
        if alpn:
            context.set_alpn_protocols([pr.ALPN])
        if key is not None and cert_der is not None:
            folder = Path(tempfile.mkdtemp(prefix="applink-client-"))
            (folder / "c.pem").write_bytes(x509.load_der_x509_certificate(cert_der)
                                           .public_bytes(serialization.Encoding.PEM))
            (folder / "k.pem").write_bytes(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))
            context.load_cert_chain(str(folder / "c.pem"), str(folder / "k.pem"))
            for name in ("c.pem", "k.pem"):
                (folder / name).unlink()
            folder.rmdir()
        self._in, self._out = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.tls = context.wrap_bio(self._in, self._out, server_hostname=sni)
        self.reader = pr.FrameReader()
        self.frames: List[Dict[str, Any]] = []
        self.chunks: List[pr.Frame] = []
        self.handshaken = False
        self.error: Optional[Exception] = None
        self.wire_out: List[bytes] = []
        self._to_server = to_server

    def attach(self, pipe: Pipe) -> "AppClient":
        self.pipe = pipe
        if self._to_server is None:
            self._to_server = pipe.feed
        return self

    def pump(self) -> None:
        for _ in range(200):
            moved = False
            out = self._out.read()
            if out:
                self.wire_out.append(out)
                self._to_server(out)
                moved = True
            if self.pipe.to_client:
                self._in.write(bytes(self.pipe.to_client))
                self.pipe.to_client.clear()
                moved = True
            if self.error is None:
                try:
                    if not self.handshaken:
                        self.tls.do_handshake()
                        self.handshaken = True
                        moved = True
                    else:
                        self._read()
                except ssl.SSLWantReadError:
                    pass
                except (ssl.SSLError, OSError) as exc:
                    self.error = exc
            if not moved and not self._out.pending and not self.pipe.to_client:
                break

    def _read(self) -> None:
        data = b""
        while True:
            try:
                piece = self.tls.read(65536)
            except ssl.SSLWantReadError:
                break
            except ssl.SSLZeroReturnError:
                break
            if not piece:
                break
            data += piece
        for frame in self.reader.feed(data):
            if frame.type == pr.FRAME_JSON:
                self.frames.append(frame.obj)
            else:
                self.chunks.append(frame)

    def connect(self) -> "AppClient":
        self.pump()
        return self

    def send(self, obj: Dict[str, Any]) -> None:
        self.send_raw(pr.encode_json(obj))

    def send_raw(self, data: bytes) -> None:
        try:
            self.tls.write(data)
        except (ssl.SSLError, OSError) as exc:
            self.error = self.error or exc
            return
        self.pump()

    def types(self) -> List[str]:
        return [f.get("t") for f in self.frames]

    def of(self, t: str) -> List[Dict[str, Any]]:
        return [f for f in self.frames if f.get("t") == t]


def pair_message(k: str, key, *, device: str = "Pixel 9") -> Dict[str, Any]:
    return {"t": "pair", "v": 1, "k": k, "csr": base64.b64encode(csr_der(key)).decode(),
            "device": device, "platform": "android", "app_version": "1.4.0"}


def ca_der() -> bytes:
    from vaf.network import ssl_utils
    return ssl_utils.app_link_server_files()[2]


def link(scope: str, *, device=None, transport: str = "direct", peer: str = "10.0.0.5",
         connection: str = "c1", hello: bool = True, caps=None):
    """A device connected and greeted: (client, pipe, row)."""
    row, key, der = device if device is not None else paired_device(scope)
    pipe = Pipe(transport, peer=peer, connection=connection,
                relay_cid=("ab" * 16) if transport == "relay" else None)
    client = AppClient(ca_der(), sni=pr.SNI_LINK, key=key, cert_der=der).attach(pipe).connect()
    if hello:
        client.send({"t": "hello", "v": 1, "device_id": row["device_id"], "app_version": "1.4.0",
                     "caps": list(caps or [])})
    return client, pipe, row
