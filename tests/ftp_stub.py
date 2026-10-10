# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A small FTP/FTPS server for the tests of vaf/core/ftp.py, from the standard library.

Enough of RFC 959/4217 for ftplib's client: AUTH TLS, PBSZ, PROT, USER/PASS, TYPE, PASV,
STOR, RETR, MLSD, LIST, MKD, DELE, PWD, QUIT. Files live in a real folder. One client at a
time, which is all a test needs. `pasv_host` is the address the PASV answer NAMES - a test
sets it to one nobody listens on, to see that the client ignores it.

Certificates come from `cryptography` (already a dependency of VAF): `make_ca()` and
`make_cert()` build a test authority and leaf certificates signed by it or by themselves.
"""
from __future__ import annotations

import datetime
import ipaddress
import os
import posixpath
import socket
import ssl
import threading
from pathlib import Path
from typing import List, Optional, Tuple


def _key():
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.generate_private_key(ec.SECP256R1())


def _pem(obj) -> bytes:
    from cryptography.hazmat.primitives import serialization
    if hasattr(obj, "private_bytes"):
        return obj.private_bytes(serialization.Encoding.PEM,
                                 serialization.PrivateFormat.TraditionalOpenSSL,
                                 serialization.NoEncryption())
    return obj.public_bytes(serialization.Encoding.PEM)


def make_ca(folder: Path) -> Tuple[Path, object, object]:
    """A test certificate authority: (its PEM file, its key, its certificate)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID
    key = _key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "VAF test authority")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    path = folder / "ca.pem"
    path.write_bytes(_pem(cert))
    return path, key, cert


def make_cert(folder: Path, name: str, ca: Optional[Tuple[Path, object, object]] = None) -> Path:
    """A leaf certificate for localhost and 127.0.0.1 in one PEM file with its key; signed by
    `ca`, or by itself without one."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    key = _key()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    issuer_key, issuer_name = key, subject
    if ca is not None:
        issuer_key, issuer_name = ca[1], ca[2].subject
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer_name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            # Python 3.13's default context verifies strictly (VERIFY_X509_STRICT): a leaf
            # needs the identifier of the key that signed it.
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
                           critical=False)
            .sign(issuer_key, hashes.SHA256()))
    path = folder / f"{name}.pem"
    path.write_bytes(_pem(key) + _pem(cert))
    return path


class FtpStub:
    """`with FtpStub(root, cert=...) as server:` listens on 127.0.0.1:server.port."""

    def __init__(self, root: Path, *, cert: Optional[Path] = None, user: str = "alice",
                 password: str = "s3cret-pass", pasv_host: str = "127.0.0.1",
                 mlsd: bool = True):
        self.root = Path(root)
        self.cert = cert
        self.user = user
        self.password = password
        self.pasv_host = pasv_host
        self.mlsd = mlsd
        self.commands: List[str] = []
        self.connections = 0
        self._ctx = None
        if cert is not None:
            self._ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            self._ctx.load_cert_chain(str(cert))
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
        except OSError:
            pass
        self._thread.join(timeout=5)
        self._listener.close()

    # ── the server ────────────────────────────────────────────────────────────
    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            if self._stop.is_set():
                conn.close()
                return
            self.connections += 1
            try:
                self._session(conn)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _path(self, cwd: str, arg: str) -> Path:
        # posixpath, not os.path: an FTP path is POSIX whatever the host. ntpath turned
        # "/docs" into "\\docs", which survived the lstrip and rooted the join at the
        # drive, so every path read as outside the root on Windows.
        rel = posixpath.normpath(posixpath.join(cwd, arg)).lstrip("/")
        path = (self.root / rel).resolve()
        if path != self.root.resolve() and self.root.resolve() not in path.parents:
            raise PermissionError(arg)
        return path

    def _session(self, conn: socket.socket):
        state = {"tls": False, "prot": False, "user": None, "logged": False, "cwd": "/",
                 "pasv": None}
        f = conn.makefile("rb")

        def send(line: str):
            conn.sendall((line + "\r\n").encode())

        send("220 VAF test FTP")
        while True:
            raw = f.readline()
            if not raw:
                return
            line = raw.decode().rstrip("\r\n")
            cmd, _, arg = line.partition(" ")
            cmd = cmd.upper()
            self.commands.append(cmd if cmd != "PASS" else "PASS ***")
            if cmd == "AUTH" and self._ctx is not None and arg.upper() == "TLS":
                send("234 AUTH TLS ok")
                conn = self._ctx.wrap_socket(conn, server_side=True)
                f = conn.makefile("rb")
                state["tls"] = True
            elif cmd == "AUTH":
                send("504 AUTH not supported")
            elif cmd == "PBSZ":
                send("200 PBSZ=0")
            elif cmd == "PROT":
                state["prot"] = arg.upper() == "P"
                send("200 PROT ok")
            elif cmd == "USER":
                state["user"] = arg
                send("331 password please")
            elif cmd == "PASS":
                if state["user"] == self.user and arg == self.password:
                    state["logged"] = True
                    send("230 logged in")
                elif state["user"] == "anonymous":
                    state["logged"] = True
                    send("230 anonymous")
                else:
                    send("530 Login incorrect.")
            elif cmd == "QUIT":
                send("221 bye")
                return
            elif not state["logged"]:
                send("530 Please login with USER and PASS.")
            elif cmd == "TYPE":
                send("200 type ok")
            elif cmd == "PWD":
                send(f'257 "{state["cwd"]}"')
            elif cmd == "PASV":
                data = socket.socket()
                data.bind(("127.0.0.1", 0))
                data.listen(1)
                state["pasv"] = data
                port = data.getsockname()[1]
                h = self.pasv_host.replace(".", ",")
                send(f"227 Entering Passive Mode ({h},{port >> 8},{port & 255})")
            elif cmd == "MKD":
                path = self._path(state["cwd"], arg)
                if path.exists():
                    send("550 exists")
                else:
                    path.mkdir()
                    send(f'257 "{arg}" created')
            elif cmd == "DELE":
                path = self._path(state["cwd"], arg)
                if path.is_file():
                    path.unlink()
                    send("250 deleted")
                else:
                    send("550 no such file")
            elif cmd in ("STOR", "RETR", "MLSD", "LIST"):
                if cmd == "MLSD" and not self.mlsd:
                    send("500 unknown command")
                    continue
                self._transfer(cmd, arg, state, send)
            else:
                send("502 not implemented")

    def _transfer(self, cmd, arg, state, send):
        listener = state.pop("pasv", None)
        if listener is None:
            send("425 use PASV first")
            return
        try:
            path = self._path(state["cwd"], arg or ".")
        except PermissionError:
            send("550 outside")
            listener.close()
            return
        if cmd == "RETR" and not path.is_file():
            send("550 no such file")
            listener.close()
            return
        if cmd == "STOR" and not path.parent.is_dir():
            send("550 no such folder")
            listener.close()
            return
        send("150 opening data connection")
        data, _ = listener.accept()
        listener.close()
        data.settimeout(10)
        if state["prot"] and self._ctx is not None:
            data = self._ctx.wrap_socket(data, server_side=True)
        try:
            if cmd == "STOR":
                with open(path, "wb") as out:
                    while True:
                        chunk = data.recv(65536)
                        if not chunk:
                            break
                        out.write(chunk)
            elif cmd == "RETR":
                data.sendall(path.read_bytes())
            elif cmd == "MLSD":
                lines = []
                for entry in sorted(path.iterdir()):
                    kind = "dir" if entry.is_dir() else "file"
                    size = entry.stat().st_size if entry.is_file() else 0
                    lines.append(f"type={kind};size={size}; {entry.name}\r\n")
                data.sendall("".join(lines).encode())
            else:
                lines = [f"{'d' if e.is_dir() else '-'}rw-r--r-- 1 u g {e.stat().st_size} "
                         f"Jan 1 00:00 {e.name}\r\n" for e in sorted(path.iterdir())]
                data.sendall("".join(lines).encode())
            if isinstance(data, ssl.SSLSocket):
                try:
                    data = data.unwrap()
                except (OSError, ssl.SSLError):
                    pass
        finally:
            try:
                data.close()
            except OSError:
                pass
        send("226 transfer complete")
