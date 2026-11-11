# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Files on another machine over FTP, encrypted (FTPS) unless written otherwise, as ONE account.

Why not FTP through host_bash (measured before this existed):
- `curl -T` takes one file per call, so a built site with a hundred files was a hundred
  calls or a hand-written loop; `lftp` and `ncftp` are not installed on most machines.
- The password travelled on the command line (`-u user:$VAF_SECRET_...`), visible to every
  process of the machine while the transfer ran.
- Nobody was asked about a new server, and nothing noticed a certificate that changed.
- The coder's deploy pin (vaf/tools/coder.py, deploy_to) was a pin on ssh only: `curl -T`
  to any `ftp://` address went through.

What this does instead, on Python's own ftplib:
- `ftps://` (explicit TLS, `AUTH TLS`, the data channel protected with `PROT P`) is the
  default; `ftp://` without encryption only when it is written that way, and the person is
  told before the first connection that the password then crosses the network readable.
- Every account keeps its OWN list of confirmed servers (`~/.vaf/ftp/<account>/servers.json`,
  owner-only, under the VAF directory where no file tool reaches). A server a public
  certificate authority vouches for (chain and host name) is remembered as such: a renewed
  certificate does not break it. A server whose certificate no authority vouches for - a
  hoster's shared certificate, a self-signed one - is remembered by the certificate's
  SHA-256 fingerprint, and a different certificate later is refused until the person removes
  the server. Whether a server is new is known before the call (`is_known`), which is what
  lets the tool ask the person first.
- The address the server names for a passive data connection is ignored (ftplib's own
  `trust_server_pasv_ipv4_address = False`): the data connection goes to the host of the
  control connection, never to an address a server makes up.
- The data connection resumes the control connection's TLS session. vsftpd and pure-ftpd
  refuse a data connection that does not (`522 SSL connection failed: session reuse
  required`), and ftplib does not do it by itself.
- Cloud metadata addresses (169.254.0.0/16 and the like, vaf/network/binding.py
  `classify_address` == "forbidden") are refused. The LAN is not: a NAS or a server in the
  house is a legitimate target, exactly as for ssh.
- Transfers are bounded (500 MB, the bound ssh and download_file keep), folder uploads leave
  out links (one could point outside the folder) and `.git` and `.vaf`, and Stop ends a
  transfer between blocks (`bounded_run.cancel_check`).
- `read` takes part of a file without writing anything to disk (the end of a big log: SIZE,
  then REST to start there, at most 64 KB), and `stat` gives type, size and modification time
  (MLST, or SIZE and MDTM on a server without it).
- A host or login still carrying `$` is refused with its cause: it is the NAME of a stored
  credential, which only the ftp tool fills in (vaf/core/user_secrets.py, `expand`).

NAMED BOUNDARIES:
- The first connection trusts the server the person confirms; comparing the fingerprint with
  the hoster's panel is a human's job.
- Implicit FTPS (a TLS connection from the first byte, usually port 990) is not offered:
  ftplib has no client for it, and every hoster measured offers explicit TLS on port 21.
- The host name is checked against the forbidden addresses when it is resolved, and the
  connection resolves it again (a name that changes in between is not caught; the first
  connection is confirmed by a person, and a later one goes only to a remembered server).
- No mirroring with deletion: an upload adds and overwrites, `delete` removes one file.
- Engine-internal, not on the facade, like vaf/core/ssh.py: the ftp tool, `vaf ftp` and the
  settings route are its only callers.
- Names travel as UTF-8, ftplib's default (an upload, listing and download of `Ordner ü/Grüße
  ä.txt` round-trip). A server that keeps names in another encoding is reported as such, not
  converted, and no `OPTS UTF8 ON` is sent: no hoster was measured to need either.
- One timeout per socket operation (60 s), and the tool's deadline for the whole call. A
  separate connect timeout was not built: connect, login and a listing measured under 100 ms
  on a live hoster's server.
"""
from __future__ import annotations

import ftplib
import hashlib
import json
import os
import posixpath
import re
import socket
import ssl
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

DEFAULT_PORT = 21
TIMEOUT_SECONDS = 60                     # each socket operation, not the whole transfer
MAX_TRANSFER_BYTES = 500 * 1024 * 1024   # the same bound ssh and download_file keep
SERVERS_FILE = "servers.json"
FOLDER_SKIP = (".git", ".vaf")           # never part of what gets deployed
MAX_READ_BYTES = 64 * 1024               # one `read`: a slice for the model, not a download

_USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@+-]{0,127}$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_IPV6_RE = re.compile(r"^[0-9A-Fa-f:.]{2,45}$")


class FtpError(Exception):
    """Refused or impossible; the message is written for the model and the person."""


_NOT_UTF8 = ("the server sent a file name or answer that is not UTF-8 (it keeps names in an "
             "older encoding); VAF reads FTP names as UTF-8")


@contextmanager
def _names_as_utf8():
    """ftplib reads the control and listing streams as strict UTF-8: a name in another
    encoding raised a bare codec error. Also a decorator (one wrapper for every operation)."""
    try:
        yield
    except UnicodeDecodeError:
        raise FtpError(_NOT_UTF8) from None


def _unreachable(e: BaseException) -> FtpError:
    """The FtpError for a connection that failed before or during the login."""
    if isinstance(e, UnicodeDecodeError):
        return FtpError(_NOT_UTF8)
    return FtpError(f"the server could not be reached: {e}")


@dataclass(frozen=True)
class Target:
    """`ftps://user@host[:port]`, checked. `tls` False only for an explicit `ftp://`."""
    user: str
    host: str
    port: int = DEFAULT_PORT
    tls: bool = True

    @property
    def scheme(self) -> str:
        return "ftps" if self.tls else "ftp"

    @property
    def name(self) -> str:
        """The key in the account's server list: scheme, host and port. The scheme belongs to
        it - a server confirmed encrypted is not thereby confirmed in plain text."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}" + ("" if self.port == DEFAULT_PORT else f":{self.port}")

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return (f"{self.scheme}://{self.user}@{host}"
                + ("" if self.port == DEFAULT_PORT else f":{self.port}"))


def parse_server(text: Any) -> Target:
    """`ftps://user@host[:port]`, `ftp://user@host[:port]`, or `user@host[:port]` (FTPS).
    Without `user@` the login is anonymous. A login name may contain `@` (many hosters use
    `name@domain` for FTP accounts): the host is what follows the LAST `@`."""
    raw = str(text or "").strip()
    if not raw or raw.startswith("-") or any(ch.isspace() or ord(ch) < 32 for ch in raw):
        raise FtpError(f"'{raw}' is not a server: write ftps://user@host or user@host")
    tls = True
    lowered = raw.lower()
    if lowered.startswith("ftps://"):
        raw = raw[len("ftps://"):]
    elif lowered.startswith("ftp://"):
        raw, tls = raw[len("ftp://"):], False
    elif "://" in raw:
        raise FtpError(f"'{text}' is not an FTP server: write ftps://user@host")
    raw = raw.rstrip("/")
    if "/" in raw:
        raise FtpError(f"'{text}' carries a path: name the server alone, the folder separately")
    user, _, rest = raw.rpartition("@")
    user = user or "anonymous"
    # Before the host check: "not a host name" hid the cause when the model wrote a stored
    # credential's name ($FTP_HOST) where the value belongs.
    for part, what in ((user, "login"), (rest, "host name")):
        if "$" in part:
            raise FtpError(f"'{part}' is the name of a stored credential, not a {what}: the ftp "
                           "tool fills in $NAME for a credential the user stored; elsewhere "
                           "(deploy_to) write the server out")
    port_text = ""
    if rest.startswith("["):
        m = re.match(r"^\[([^\]]+)\](?::(\d+))?$", rest)
        if not m or not _IPV6_RE.match(m.group(1)):
            raise FtpError(f"'{rest}' is not a server: write user@[address]:port")
        host, port_text = m.group(1), m.group(2) or ""
    elif rest.count(":") == 1:
        host, port_text = rest.split(":")
    elif rest.count(":") > 1:
        host = rest
        if not _IPV6_RE.match(host):
            raise FtpError(f"'{host}' is not an IPv6 address")
    else:
        host = rest
    if ":" not in host and not _HOST_RE.match(host):
        raise FtpError(f"'{host}' is not a host name or address")
    if not _USER_RE.match(user):
        raise FtpError(f"'{user}' is not a login name")
    port = DEFAULT_PORT
    if port_text:
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            raise FtpError(f"'{port_text}' is not a port")
        port = int(port_text)
    return Target(user=user, host=host, port=port, tls=tls)


# ── the account's confirmed servers ───────────────────────────────────────────

def account_dir(user_scope_id: Optional[str]) -> Path:
    """`~/.vaf/ftp/<account>/`, owner-only, created on first use; the account key is ssh's
    (one derivation for every per-account folder of a remote lane)."""
    from vaf.core.path_jail import PathEscape, safe_entry_name
    from vaf.core.platform import Platform
    from vaf.core.secure_store import harden_dir
    from vaf.core.ssh import account_key
    key = account_key(user_scope_id)
    try:
        name = key if key == "default" else safe_entry_name(key)
    except PathEscape:
        raise FtpError("this account has no usable FTP folder") from None
    root = Path(Platform.vaf_dir()) / "ftp"
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    harden_dir(root)
    harden_dir(folder)
    return folder


def _load(user_scope_id: Optional[str]) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads((account_dir(user_scope_id) / SERVERS_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(user_scope_id: Optional[str], data: Dict[str, Dict[str, Any]]) -> None:
    from vaf.core.secure_store import harden_path
    folder = account_dir(user_scope_id)
    tmp = folder / (SERVERS_FILE + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    harden_path(tmp)
    os.replace(tmp, folder / SERVERS_FILE)


def is_known(target: Target, user_scope_id: Optional[str]) -> bool:
    return target.name in _load(user_scope_id)


def servers(user_scope_id: Optional[str]) -> List[Dict[str, Any]]:
    """The confirmed servers: name, how the certificate is trusted ("authority", "pinned",
    or "none" for plain FTP), the fingerprint when pinned, when confirmed."""
    out = []
    for name, rec in sorted(_load(user_scope_id).items()):
        out.append({"name": name, "trust": rec.get("trust", ""),
                    "fingerprint": rec.get("fingerprint", ""),
                    "confirmed": rec.get("confirmed", "")})
    return out


def forget(name: str, user_scope_id: Optional[str]) -> bool:
    """Remove a confirmed server: the next connection asks the person again."""
    name = str(name or "").strip()
    data = _load(user_scope_id)
    if not name or name not in data:
        return False
    del data[name]
    _save(user_scope_id, data)
    return True


def _remember(target: Target, user_scope_id: Optional[str], trust: str, fingerprint: str) -> None:
    data = _load(user_scope_id)
    data[target.name] = {"trust": trust, "fingerprint": fingerprint,
                         "confirmed": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _save(user_scope_id, data)


# ── the connection ────────────────────────────────────────────────────────────

class _FTPS(ftplib.FTP_TLS):
    """FTP_TLS whose data connection resumes the control connection's TLS session (vsftpd's
    and pure-ftpd's `require_ssl_reuse`), checked against the same host name."""

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host,
                                            session=self.sock.session)
        return conn, size


def _client_context() -> ssl.SSLContext:
    """The verifying context: the system's certificate authorities, the host name checked."""
    return ssl.create_default_context()


def check_address(host: str) -> None:
    """Refuse a host that resolves to a cloud metadata or otherwise forbidden address."""
    from vaf.network.binding import classify_address
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise FtpError(f"the server '{host}' could not be found ({e})") from None
    for info in infos:
        if classify_address(info[4][0]) == "forbidden":
            raise FtpError(f"'{host}' resolves to {info[4][0]}, an address no FTP connection "
                           "may go to")


@dataclass
class Session:
    """An open, logged-in connection and what the first contact learned."""
    ftp: ftplib.FTP
    target: Target
    first_contact: bool = False
    trust: str = ""
    fingerprint: str = ""
    notes: List[str] = field(default_factory=list)


def _fingerprint(ftp: ftplib.FTP_TLS) -> str:
    der = ftp.sock.getpeercert(binary_form=True) or b""
    digest = hashlib.sha256(der).hexdigest().upper()
    return "SHA256:" + ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def connect(target: Target, *, user_scope_id: Optional[str], password: Optional[str],
            confirmed: bool, timeout: float = TIMEOUT_SECONDS) -> Session:
    """Log in to `target` as this account. A server this account has not confirmed is
    refused unless `confirmed` (the person said yes to this call), and is remembered once
    the login succeeded. Raises FtpError with a message for the model and the person."""
    known = _load(user_scope_id).get(target.name)
    if known is None and not confirmed:
        raise FtpError(f"{target.name} is a server this account has not connected to yet; "
                       "the first connection must be confirmed by the user in the app")
    check_address(target.host)
    secret = password if password is not None else "anonymous@"
    first = known is None
    if not target.tls:
        ftp = ftplib.FTP(timeout=timeout)
        try:
            ftp.connect(target.host, target.port)
            ftp.login(target.user, secret)
        except ftplib.error_perm as e:
            ftp.close()
            raise FtpError(f"login refused: {e}") from None
        except (OSError, EOFError, ftplib.Error, UnicodeDecodeError) as e:
            ftp.close()
            raise _unreachable(e) from None
        if first:
            _remember(target, user_scope_id, "none", "")
        return Session(ftp, target, first_contact=first, trust="none")

    # Encrypted. An authority-vouched certificate first; when it is not, the pinned one.
    # A remembered encrypted server is trusted one of two ways; anything else (a pinned entry
    # without its fingerprint, a plain-FTP or unknown trust under an ftps:// name) would reach
    # the unverified handshake below with nothing to compare against.
    if known is not None and not (known.get("trust") == "authority" or (
            known.get("trust") == "pinned" and known.get("fingerprint"))):
        raise FtpError(f"the remembered entry for {target.name} is damaged (no certificate to "
                       "compare with). Tell the user; they remove the server in Settings, "
                       "Connections, FTP (or `vaf ftp forget`), and the next call asks again")
    pinned = (known or {}).get("fingerprint", "")
    attempts = ["authority"] if (known or {}).get("trust") == "authority" else (
        ["pinned"] if pinned else ["authority", "pinned"])
    last_error: Optional[BaseException] = None
    for mode in attempts:
        ctx = _client_context()
        if mode == "pinned":
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        ftp = _FTPS(context=ctx, timeout=timeout)
        try:
            ftp.connect(target.host, target.port)
            ftp.auth()
        except ssl.SSLCertVerificationError as e:
            ftp.close()
            last_error = e
            continue
        except ftplib.error_perm as e:
            ftp.close()
            raise FtpError(f"the server does not offer encryption (AUTH TLS: {e}). If it is "
                           f"plain FTP, write ftp://{target.user}@... and the user is told "
                           "that the password then crosses the network readable") from None
        except (OSError, EOFError, ftplib.Error, UnicodeDecodeError) as e:
            ftp.close()
            raise _unreachable(e) from None
        fp = _fingerprint(ftp)
        if mode == "pinned" and pinned and fp != pinned:
            ftp.close()
            raise FtpError(f"REFUSED: {target.name} shows a DIFFERENT certificate ({fp}) than "
                           f"the one remembered ({pinned}). Either it was renewed, or someone "
                           "is in between. Tell the user; only if they know why, they remove "
                           "the server in Settings, Connections, FTP (or `vaf ftp forget`), "
                           "and the next call asks again")
        try:
            ftp.login(target.user, secret)
            ftp.prot_p()
        except ftplib.error_perm as e:
            ftp.close()
            raise FtpError(f"login refused: {e}") from None
        except (OSError, EOFError, ftplib.Error, UnicodeDecodeError) as e:
            ftp.close()
            raise _unreachable(e) from None
        session = Session(ftp, target, first_contact=first, trust=mode, fingerprint=fp)
        if first:
            _remember(target, user_scope_id, mode, fp if mode == "pinned" else "")
            if mode == "pinned":
                session.notes.append(
                    f"No certificate authority vouches for this server's certificate; its "
                    f"fingerprint {fp} is now remembered, and a different one is refused.")
        return session
    if (known or {}).get("trust") == "authority":
        raise FtpError(f"REFUSED: {target.name} was confirmed with a certificate an authority "
                       f"vouched for, and now shows one none does ({last_error}). Tell the "
                       "user; only if they know why, they remove the server and confirm again")
    raise FtpError(f"the server's certificate could not be checked: {last_error}")


def close(session: Session) -> None:
    try:
        session.ftp.quit()
    except Exception:
        try:
            session.ftp.close()
        except Exception:
            pass


# ── the operations (each on an open session) ──────────────────────────────────

class _Stopped(Exception):
    pass


def _guard(limit: int, check_stop: Optional[Callable[[], bool]]):
    """A per-block callback: counts bytes against `limit` and ends the transfer on Stop."""
    seen = {"bytes": 0}

    def _block(chunk: bytes) -> None:
        seen["bytes"] += len(chunk)
        if seen["bytes"] > limit:
            raise FtpError(f"the transfer is larger than {limit // (1024 * 1024)} MB")
        if check_stop is not None and check_stop():
            raise _Stopped()
    return _block, seen


def _stop_check(check_stop: Optional[Callable[[], bool]]) -> Callable[[], bool]:
    if check_stop is not None:
        return check_stop
    from vaf.core.bounded_run import cancel_check
    return cancel_check()


@_names_as_utf8()
def list_dir(session: Session, path: str = ".") -> List[str]:
    """One line per entry: `d name` for a folder, `- name  <size>` for a file, `? name  <size>`
    when the server does not say which. MLSD where the server has it, the server's own LIST
    lines otherwise."""
    lines: List[str] = []
    try:
        for name, facts in session.ftp.mlsd(path, facts=["type", "size"]):
            if name in (".", ".."):
                continue
            kind = facts.get("type", "").lower()
            if kind in ("cdir", "pdir"):
                continue
            if kind == "dir":
                lines.append(f"d {name}")
            elif kind == "file":
                lines.append(f"- {name}  {facts.get('size', '?')}")
            else:           # RFC 3659 lets a server leave the type out: not called a file
                lines.append(f"? {name}  {facts.get('size', '?')}")
        return lines
    except ftplib.error_perm:
        pass
    raw: List[str] = []
    session.ftp.retrlines(f"LIST {path}" if path not in ("", ".") else "LIST", raw.append)
    return raw


def _ensure_dirs(ftp: ftplib.FTP, folder: str) -> None:
    """MKD every component of `folder` that is not there yet."""
    if folder in ("", ".", "/"):
        return
    parts = [p for p in folder.split("/") if p]
    current = "/" if folder.startswith("/") else ""
    for part in parts:
        current = posixpath.join(current, part) if current else part
        try:
            ftp.mkd(current)
        except ftplib.error_perm as e:
            # 550 (most servers) or 521 is the answer for a folder that is already there;
            # any other refusal shows up at the STOR that needs the folder.
            if str(e)[:3] not in ("550", "521"):
                raise


@_names_as_utf8()
def upload(session: Session, local: Path, remote: str, *,
           check_stop: Optional[Callable[[], bool]] = None,
           limit: int = MAX_TRANSFER_BYTES) -> Dict[str, int]:
    """A file to `remote` (its full path there), or a folder INTO `remote`. Folders: links,
    .git and .vaf left out, the whole size checked against `limit` before the first byte.
    Returns {"files": n, "bytes": n}."""
    stop = _stop_check(check_stop)
    ftp = session.ftp
    if local.is_dir():
        from vaf.core.path_jail import walk_without_links
        files = []
        total = 0
        for rel_text, is_folder in walk_without_links(local, FOLDER_SKIP):
            if is_folder:
                continue
            rel = Path(rel_text)
            total += (local / rel).stat().st_size
            if total > limit:
                raise FtpError(f"the folder is larger than {limit // (1024 * 1024)} MB")
            files.append(rel)
        _ensure_dirs(ftp, remote)
        made = {""}
        sent = 0
        for rel in files:
            parent = rel.parent.as_posix()
            if parent not in made and parent != ".":
                _ensure_dirs(ftp, posixpath.join(remote, parent))
                made.add(parent)
            block, seen = _guard(limit - sent, stop)
            try:
                with open(local / rel, "rb") as fh:
                    ftp.storbinary(f"STOR {posixpath.join(remote, rel.as_posix())}", fh,
                                   callback=block)
            except _Stopped:
                raise FtpError("stopped: the user asked to stop") from None
            sent += seen["bytes"]
        return {"files": len(files), "bytes": sent}
    if not local.is_file():
        raise FtpError(f"{local} is not a file or a folder")
    if local.stat().st_size > limit:
        raise FtpError(f"the file is larger than {limit // (1024 * 1024)} MB")
    _ensure_dirs(ftp, posixpath.dirname(remote))
    block, seen = _guard(limit, stop)
    try:
        with open(local, "rb") as fh:
            ftp.storbinary(f"STOR {remote}", fh, callback=block)
    except _Stopped:
        raise FtpError("stopped: the user asked to stop") from None
    return {"files": 1, "bytes": seen["bytes"]}


@_names_as_utf8()
def download(session: Session, remote: str, local: Path, *,
             check_stop: Optional[Callable[[], bool]] = None,
             limit: int = MAX_TRANSFER_BYTES) -> int:
    """One file to `local`, through a `.part` file that only becomes `local` when complete.
    Returns the number of bytes."""
    stop = _stop_check(check_stop)
    part = local.with_name(local.name + ".part")
    from vaf.core.path_jail import open_for_write
    try:
        # Never through a link at the .part name, never into VAF's own data (WriteRefused
        # is an OSError, so it is reported like any other target that cannot be written).
        sink = open_for_write(part, "wb")
    except OSError as e:
        raise FtpError(f"the download target cannot be written: {e}") from None
    block, seen = _guard(limit, stop)

    def _write(chunk: bytes) -> None:
        block(chunk)
        sink.write(chunk)

    try:
        with sink:
            session.ftp.retrbinary(f"RETR {remote}", _write)
    except _Stopped:
        part.unlink(missing_ok=True)
        raise FtpError("stopped: the user asked to stop") from None
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, local)
    return seen["bytes"]


@_names_as_utf8()
def delete(session: Session, remote: str) -> None:
    """One file. A folder is refused by the server itself (DELE removes files only)."""
    session.ftp.delete(remote)


# ── a look at one file without downloading it ─────────────────────────────────

# The answers of a server that does not have a command: 500 unknown, 501 its arguments not
# understood, 502 not implemented, 504 not for this argument. A missing file is 550.
_NOT_IMPLEMENTED = ("500", "501", "502", "504")


def _code(e: BaseException) -> str:
    return str(e)[:3]


def _utc(stamp: str) -> Optional[str]:
    """`YYYYMMDDHHMMSS[.sss]` (MLST's modify fact, MDTM's answer) as a readable UTC time."""
    digits = stamp.strip().split(".", 1)[0]
    if len(digits) != 14 or not digits.isdigit():
        return None
    return (f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]} "
            f"{digits[8:10]}:{digits[10:12]}:{digits[12:14]} UTC")


def _size(ftp: ftplib.FTP, remote: str) -> Optional[int]:
    """SIZE in binary mode (vsftpd refuses it in ASCII mode), or None when the server does
    not tell."""
    try:
        ftp.voidcmd("TYPE I")
        return ftp.size(remote)
    except (ftplib.error_perm, ftplib.error_reply, ValueError):
        return None


def _mlst_facts(answer: str) -> Optional[Dict[str, str]]:
    """The facts of an MLST answer, or None when it carries no entry line. The entry is the
    line indented by one space, `[facts] SP name` (RFC 3659 7.2): ` type=file;size=12; a.txt`,
    or `  a.txt` when the server sends no facts at all."""
    for line in answer.splitlines():
        if not line.startswith(" "):
            continue
        head, _, _name = line[1:].partition(" ")
        facts: Dict[str, str] = {}
        for fact in head.split(";"):
            key, eq, value = fact.partition("=")
            if eq and key.strip():
                facts[key.strip().lower()] = value.strip()
        return facts
    return None


def _mdtm(ftp: ftplib.FTP, remote: str) -> Optional[str]:
    try:
        return _utc(ftp.sendcmd(f"MDTM {remote}")[4:])
    except (ftplib.error_perm, ftplib.error_reply):
        return None


@_names_as_utf8()
def stat(session: Session, remote: str) -> Dict[str, Any]:
    """{"type": "file"|"folder"|None, "size": int|None,
    "modified": "YYYY-MM-DD HH:MM:SS UTC"|None} of one remote path: MLST where the server has
    it, SIZE and MDTM for what it leaves out or when it has none. RFC 3659 lets an MLST answer
    omit any fact, so a missing one is asked for, never assumed: "file" needs the type fact or
    an answered SIZE (a folder has no size), and with neither the type is None."""
    missing = FtpError(f"{remote} was not found, or it is a folder (list shows a folder)")
    ftp = session.ftp
    try:
        answer = ftp.sendcmd(f"MLST {remote}")
    except ftplib.error_perm as e:
        if _code(e) == "550":
            raise missing from None
        if _code(e) not in _NOT_IMPLEMENTED:
            raise
        answer = ""
    facts = _mlst_facts(answer)
    kind = (facts or {}).get("type", "").lower()
    modified = _utc((facts or {}).get("modify", ""))
    if kind in ("dir", "cdir", "pdir"):
        return {"type": "folder", "size": None, "modified": modified}
    size_fact = (facts or {}).get("size", "")
    size = int(size_fact) if size_fact.isdigit() else _size(ftp, remote)
    if modified is None:
        modified = _mdtm(ftp, remote)
    if kind == "file" or size is not None:
        return {"type": "file", "size": size, "modified": modified}
    if facts is None and modified is None:
        raise missing
    # The path is there (MLST named it, or MDTM answered), but nothing says which kind.
    return {"type": None, "size": None, "modified": modified}


class _Enough(Exception):
    pass


@_names_as_utf8()
def read(session: Session, remote: str, *, offset: int = 0, max_bytes: int = 8000,
         check_stop: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """At most `max_bytes` (clamped to MAX_READ_BYTES) of `remote` from byte `offset`; a
    negative offset counts from the end (-20000: the last 20 KB). Nothing is written to disk.
    Returns {"data": bytes, "start": int, "size": int|None}."""
    stop = _stop_check(check_stop)
    limit = min(max(1, int(max_bytes)), MAX_READ_BYTES)
    ftp = session.ftp
    size = _size(ftp, remote)
    offset = int(offset)
    if offset < 0:
        if size is None:
            raise FtpError("the server does not tell the file size (SIZE), so a read from the "
                           "end is not possible; give a positive offset")
        start = max(0, size + offset)
    else:
        start = offset
    if size is not None and start >= size:
        return {"data": b"", "start": start, "size": size}
    chunks: List[bytes] = []
    seen = {"bytes": 0}

    def _collect(chunk: bytes) -> None:
        take = chunk[:limit - seen["bytes"]]
        chunks.append(take)
        seen["bytes"] += len(take)
        if seen["bytes"] >= limit:
            raise _Enough()
        if stop():
            raise _Stopped()

    try:
        ftp.retrbinary(f"RETR {remote}", _collect, rest=start if start > 0 else None)
    except _Enough:
        # The data connection is closed; the server still answers the RETR (226, or 426 for
        # the cut). Read it so the session stays usable, briefly, and whatever it says.
        try:
            ftp.sock.settimeout(5)
            ftp.voidresp()
        except Exception:
            pass
        finally:
            try:
                ftp.sock.settimeout(ftp.timeout)
            except Exception:
                pass
    except _Stopped:
        raise FtpError("stopped: the user asked to stop") from None
    except ftplib.error_perm as e:
        # REST is sent before RETR, so a "not implemented" answer here is the REST: RETR
        # itself is in every server, and its own refusals are 550 and the like.
        if start > 0 and _code(e) in _NOT_IMPLEMENTED:
            raise FtpError("the server cannot start a transfer in the middle of a file (REST); "
                           "download it instead") from None
        raise
    return {"data": b"".join(chunks), "start": start, "size": size}
