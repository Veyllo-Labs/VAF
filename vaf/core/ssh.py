# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A shell on another machine, over the system's own OpenSSH, as ONE account.

Why not `ssh` through host_bash (measured before this existed):
- ssh reads a password from the terminal, never from a variable. host_bash inherits VAF's
  terminal, so the prompt went to a terminal nobody sees and the call hung until its timeout;
  a detached command opened the desktop's password window instead.
- The first connection asks "continue connecting?" the same way.
- `ssh` in a free command line uses the machine owner's `~/.ssh`, its config and its agent,
  for every account alike.

What this does instead:
- Every account has its OWN identity: `~/.vaf/ssh/<account>/` (0700) with an ed25519 key
  that OpenSSH itself encrypts with a passphrase, and its own `known_hosts`. The passphrase
  lives in the account's encrypted credential store (vaf/core/user_secrets.py, namespace
  `ssh`), never in a file and never in a command line. `<account>` is trust's scope key, the
  one derivation used for the folder AND for the passphrase's address.
- A prompt is answered by a small askpass helper (`SSH_ASKPASS` with
  `SSH_ASKPASS_REQUIRE=force`), which reads the answer from its environment. The two secrets
  never share a run: a key login offers only the key (the password and keyboard-interactive
  methods are off, so a server cannot ask for anything), a password login turns the key off.
  A server's own prompt therefore can never be answered with the key's passphrase.
- `-F none` reads no `~/.ssh/config`, `IdentityAgent=none` offers no agent key,
  `IdentitiesOnly` offers no other key, and `StrictHostKeyChecking=accept-new` takes a new
  server's key into THIS account's list and refuses a changed one. Whether a server is new is
  known before the call (`is_known`), which is what lets the tool ask the person first.
- The command is argv, never a shell line on this side, and the remote command follows `--`:
  measured, `ssh -G -F none localhost -oProxyCommand=echo` still takes an option placed after
  the host.

NAMED BOUNDARIES:
- The first connection trusts the key the server shows (the person sees server and
  fingerprint; comparing it with the hoster's panel is a human's job).
- Windows is refused: the helper is a POSIX script, the OpenSSH that ships with many Windows
  installs (8.1) predates `SSH_ASKPASS_REQUIRE`, and the folder rights do not apply there.
- No background runs yet: a detached command keeps stdin open for host_process, so a wrong
  sudo password would wait for more lines.
- An account that also has host_bash can read files on this machine, the key folder included
  (vaf/tools/host_bash.py: an account permission outside the file jail).
- Engine-internal, not on the facade: the ssh tool, `vaf ssh` and the settings route are its
  only callers.
"""
from __future__ import annotations

import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

NAMESPACE = "ssh"                 # the passphrase's namespace in the credential store
_PASSPHRASE = "KEY_PASSPHRASE"
KEY_FILE = "id_ed25519"
KNOWN_HOSTS_FILE = "known_hosts"
DEFAULT_PORT = 22
MAX_TIMEOUT_SECONDS = 600
MIN_TIMEOUT_SECONDS = 5
MAX_TRANSFER_BYTES = 500 * 1024 * 1024      # the same bound download_file keeps
KEY_COMMENT = "vaf-agent"                    # lands in the server's authorized_keys

_USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_IPV6_RE = re.compile(r"^[0-9A-Fa-f:.]{2,45}$")
_FINGERPRINT_RE = re.compile(r"SHA256:[A-Za-z0-9+/=]+")

# The answer to an OpenSSH prompt comes from the environment of the one call that needs it;
# nothing secret is written here. Passphrase first: a key prompt never says "password".
_ASKPASS_SCRIPT = """#!/bin/sh
# Written by VAF (vaf/core/ssh.py): answers an OpenSSH prompt for one call.
case "$1" in
  *[Pp]assphrase*) [ -n "$VAF_ASKPASS_PASSPHRASE" ] || exit 1
                   printf '%s\\n' "$VAF_ASKPASS_PASSPHRASE" ;;
  *[Pp]assword*)   [ -n "$VAF_ASKPASS_PASSWORD" ] || exit 1
                   printf '%s\\n' "$VAF_ASKPASS_PASSWORD" ;;
  *) exit 1 ;;
esac
"""
_askpass_lock = threading.Lock()


class SshError(Exception):
    """Refused or impossible; the message is written for the model and the person."""


@dataclass(frozen=True)
class Target:
    """`user@host[:port]`, checked. Never an option: a leading `-` is refused."""
    user: str
    host: str
    port: int = DEFAULT_PORT

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}"

    @property
    def known_hosts_name(self) -> str:
        """How OpenSSH names this server in known_hosts."""
        if self.port == DEFAULT_PORT:
            return self.host
        return f"[{self.host}]:{self.port}"

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.user}@{host}" + ("" if self.port == DEFAULT_PORT else f":{self.port}")


def parse_server(text: Any) -> Target:
    """`user@host`, `user@host:2222`, `user@[::1]:2222`. Raises SshError for anything else."""
    raw = str(text or "").strip()
    if not raw or raw.startswith("-") or any(ch.isspace() or ord(ch) < 32 for ch in raw):
        raise SshError(f"'{raw}' is not a server: write user@host or user@host:port")
    if "@" not in raw:
        raise SshError(f"'{raw}' has no login name: write user@host or user@host:port")
    user, _, rest = raw.rpartition("@")
    port_text = ""
    if rest.startswith("["):
        m = re.match(r"^\[([^\]]+)\](?::(\d+))?$", rest)
        if not m:
            raise SshError(f"'{raw}' is not a server: write user@[address]:port")
        host, port_text = m.group(1), m.group(2) or ""
        if not _IPV6_RE.match(host):
            raise SshError(f"'{host}' is not an IPv6 address")
    elif rest.count(":") == 1:
        host, port_text = rest.split(":")
    elif rest.count(":") > 1:
        host = rest
        if not _IPV6_RE.match(host):
            raise SshError(f"'{host}' is not an IPv6 address")
    else:
        host = rest
    if ":" not in host and not _HOST_RE.match(host):
        raise SshError(f"'{host}' is not a host name or address")
    if not _USER_RE.match(user):
        raise SshError(f"'{user}' is not a login name")
    port = DEFAULT_PORT
    if port_text:
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            raise SshError(f"'{port_text}' is not a port")
        port = int(port_text)
    return Target(user=user, host=host, port=port)


# ── the account's own identity ──────────────────────────────────────────────

def account_key(user_scope_id: Optional[str]) -> str:
    """The one derivation for the folder AND the passphrase: no scope and the machine owner's
    scope are "default" (the owner), any other scope is its own."""
    from vaf.core.trust import _scope_key
    return _scope_key(user_scope_id)


def _store_scope(user_scope_id: Optional[str]) -> Optional[str]:
    key = account_key(user_scope_id)
    return None if key == "default" else key


def account_dir(user_scope_id: Optional[str]) -> Path:
    """`~/.vaf/ssh/<account>/`, owner-only, created on first use. Under the VAF directory on
    purpose: the file tools refuse all of it for everyone (vaf/tools/filesystem.py), so no
    read_file reaches the key."""
    from vaf.core.path_jail import PathEscape, safe_entry_name
    from vaf.core.platform import Platform
    from vaf.core.secure_store import harden_dir
    key = account_key(user_scope_id)
    try:
        name = key if key == "default" else safe_entry_name(key)
    except PathEscape:
        raise SshError("this account has no usable SSH folder") from None
    root = Path(Platform.vaf_dir()) / "ssh"
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    harden_dir(root)
    harden_dir(folder)
    return folder


def require_openssh() -> None:
    from vaf.core.platform import Platform
    if Platform.is_windows():
        raise SshError("SSH to other machines is not available on Windows yet: the password "
                       "prompt answer needs a POSIX helper and OpenSSH 8.4 or newer")
    for binary in ("ssh", "ssh-keygen"):
        if shutil.which(binary) is None:
            raise SshError(f"the OpenSSH client ('{binary}') is not installed on this computer")


def askpass_helper() -> Path:
    """The prompt answerer, written at run time with 0700 (a packaged script can lose its
    executable bit on install)."""
    from vaf.core.platform import Platform
    from vaf.core.secure_store import harden_dir
    root = Path(Platform.vaf_dir()) / "ssh"
    root.mkdir(parents=True, exist_ok=True)
    harden_dir(root)
    path = root / "askpass.sh"
    with _askpass_lock:
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current != _ASKPASS_SCRIPT:
            tmp = root / f".askpass.{os.getpid()}.tmp"
            tmp.write_text(_ASKPASS_SCRIPT, encoding="utf-8")
            os.chmod(tmp, 0o700)
            os.replace(tmp, path)
        os.chmod(path, 0o700)
    return path


def _env(*, password: Optional[str] = None, passphrase: Optional[str] = None) -> Dict[str, str]:
    """This process's environment for one OpenSSH call: the helper, and AT MOST one secret."""
    if password and passphrase:
        raise SshError("a call carries the password or the key's passphrase, never both")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("VAF_ASKPASS_", "VAF_SECRET_")) and k != "SSH_AUTH_SOCK"}
    env["SSH_ASKPASS"] = str(askpass_helper())
    env["SSH_ASKPASS_REQUIRE"] = "force"
    if password:
        env["VAF_ASKPASS_PASSWORD"] = password
    if passphrase:
        env["VAF_ASKPASS_PASSPHRASE"] = passphrase
    return env


def _passphrase(user_scope_id: Optional[str], *, create: bool = False) -> Optional[str]:
    from vaf.core import user_secrets
    scope = _store_scope(user_scope_id)
    try:
        value = user_secrets.account_value(NAMESPACE, _PASSPHRASE, user_scope_id=scope)
    except Exception as e:
        # Measured: with the store unreadable the key login failed as "key not installed",
        # which sends the person to the server for a problem on this computer.
        raise SshError("this account's SSH key cannot be unlocked: the credential store is not "
                       f"readable on this computer ({type(e).__name__}: {e})") from None
    if value or not create:
        return value
    value = secrets.token_urlsafe(32)
    user_secrets.set_account_value(NAMESPACE, _PASSPHRASE, value, user_scope_id=scope)
    return value


def _tool(argv: List[str], *, env: Optional[Dict[str, str]] = None,
          timeout: float = 30) -> subprocess.CompletedProcess:
    """One short ssh-keygen call: no terminal, no stdin."""
    kwargs: Dict[str, Any] = {"capture_output": True, "text": True, "timeout": timeout,
                              "stdin": subprocess.DEVNULL, "env": env}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    return subprocess.run(argv, **kwargs)


def public_key(user_scope_id: Optional[str]) -> Optional[str]:
    """This account's public key line, or None while it has none."""
    pub = account_dir(user_scope_id) / f"{KEY_FILE}.pub"
    try:
        text = pub.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def ensure_identity(user_scope_id: Optional[str]) -> Path:
    """The account's private key, created on first use. Never replaces an existing one:
    a new key would lock the account out of every server the old one was installed on."""
    require_openssh()
    folder = account_dir(user_scope_id)
    key = folder / KEY_FILE
    pub = folder / f"{KEY_FILE}.pub"
    if key.exists():
        if not pub.exists():
            passphrase = _passphrase(user_scope_id)
            if not passphrase:
                raise SshError("this account's SSH key has lost its passphrase; remove the key "
                               "in Settings, Connections, SSH to create a new one")
            r = _tool(["ssh-keygen", "-y", "-f", str(key)], env=_env(passphrase=passphrase))
            if r.returncode != 0 or not r.stdout.strip():
                raise SshError("this account's SSH key could not be read: "
                               + (r.stderr or "").strip()[:200])
            pub.write_text(r.stdout.strip() + f" {KEY_COMMENT}\n", encoding="utf-8")
        return key
    passphrase = _passphrase(user_scope_id, create=True)
    r = _tool(["ssh-keygen", "-q", "-t", "ed25519", "-a", "64", "-C", KEY_COMMENT,
               "-f", str(key)], env=_env(passphrase=passphrase), timeout=60)
    if r.returncode != 0 or not key.exists() or not pub.exists():
        raise SshError("the SSH key could not be created: " + (r.stderr or "").strip()[:200])
    os.chmod(key, 0o600)
    return key


# ── the servers this account knows ──────────────────────────────────────────

def known_hosts_path(user_scope_id: Optional[str]) -> Path:
    return account_dir(user_scope_id) / KNOWN_HOSTS_FILE


def is_known(target: Target, user_scope_id: Optional[str]) -> bool:
    path = known_hosts_path(user_scope_id)
    if not path.exists():
        return False
    r = _tool(["ssh-keygen", "-F", target.known_hosts_name, "-f", str(path)], timeout=15)
    return r.returncode == 0 and bool(r.stdout.strip())


def fingerprint(target: Target, user_scope_id: Optional[str]) -> str:
    path = known_hosts_path(user_scope_id)
    if not path.exists():
        return ""
    r = _tool(["ssh-keygen", "-l", "-F", target.known_hosts_name, "-f", str(path)], timeout=15)
    m = _FINGERPRINT_RE.search(r.stdout or "")
    return m.group(0) if m else ""


def known_hosts(user_scope_id: Optional[str]) -> List[Dict[str, str]]:
    """[{"host", "fingerprint", "type"}] for this account's servers."""
    path = known_hosts_path(user_scope_id)
    if not path.exists() or not path.read_bytes().strip():
        return []
    r = _tool(["ssh-keygen", "-l", "-f", str(path)], timeout=15)
    out = []
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[1].startswith("SHA256:"):
            out.append({"host": parts[2], "fingerprint": parts[1],
                        "type": parts[-1].strip("()")})
    return out


def forget_host(name: str, user_scope_id: Optional[str]) -> bool:
    """Remove one server (its known_hosts name, e.g. `[host]:2222`). True when it was there."""
    path = known_hosts_path(user_scope_id)
    name = str(name or "").strip()
    if not name or name.startswith("-") or not path.exists():
        return False
    r = _tool(["ssh-keygen", "-R", name, "-f", str(path)], timeout=15)
    try:
        Path(str(path) + ".old").unlink()          # ssh-keygen's backup copy
    except OSError:
        pass
    return r.returncode == 0 and "found" in (r.stdout or "") and "not found" not in (r.stdout or "")


# ── one call ─────────────────────────────────────────────────────────────────

def _option_path(path: Path) -> str:
    """A path as OpenSSH reads it inside an option: quoted (UserKnownHostsFile takes a LIST,
    so an unquoted space makes two files) and with `%` doubled (it expands tokens there).
    A `${` would expand an environment variable, so such a path is refused."""
    text = str(path)
    if '"' in text or "${" in text:
        raise SshError(f"the SSH folder's path cannot be handed to OpenSSH safely: {text}")
    return '"' + text.replace("%", "%%") + '"'


def build_argv(target: Target, remote_command: str, *, user_scope_id: Optional[str],
               password_login: bool) -> List[str]:
    """The whole OpenSSH command line for one call. The remote command follows `--`."""
    folder = account_dir(user_scope_id)
    argv = ["ssh", "-F", "none", "-T",
            "-o", "BatchMode=no",
            "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none",
            "-o", f"UserKnownHostsFile={_option_path(folder / KNOWN_HOSTS_FILE)}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=4",
            "-o", "NumberOfPasswordPrompts=1"]
    if password_login:
        # The key is not offered at all, so its passphrase is never in this call.
        argv += ["-o", "PubkeyAuthentication=no",
                 "-o", "PreferredAuthentications=keyboard-interactive,password"]
    else:
        # Only the key: with password and keyboard-interactive off the server cannot ask
        # for anything, so no server prompt can be answered with the passphrase.
        key = str(folder / KEY_FILE).replace("%", "%%")
        if "${" in key:
            raise SshError(f"the SSH folder's path cannot be handed to OpenSSH safely: {key}")
        argv += ["-o", "PreferredAuthentications=publickey",
                 "-o", "PasswordAuthentication=no",
                 "-o", "KbdInteractiveAuthentication=no",
                 "-i", key]
    argv += ["-p", str(target.port), target.destination, "--", remote_command]
    return argv


def as_root(command: str, *, with_password: bool) -> str:
    """The remote command run through sudo.

    With a password it arrives as the first line of stdin: `sudo -S` reads it, and the
    command itself starts with its stdin on /dev/null, so it can never read that line -
    also when sudo did not ask (NOPASSWD, a cached ticket). A wrong password makes sudo read
    on and hit the end of the input, so it fails instead of waiting. Without one, `sudo -n`
    fails at once where a password would be needed."""
    if with_password:
        return "sudo -S -p '' -- sh -c " + shlex.quote("exec 0</dev/null\n" + command)
    return "sudo -n -- sh -c " + shlex.quote(command)


def install_key_command(pub: str) -> str:
    """Append the public key to the server's authorized_keys once (no ssh-copy-id needed)."""
    q = shlex.quote(pub.strip())
    return ("umask 077 && mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
            f"(grep -qxF {q} ~/.ssh/authorized_keys || printf '%s\\n' {q} >> ~/.ssh/authorized_keys)")


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    too_large: bool = False
    first_contact: bool = False
    fingerprint: str = ""
    host_key_changed: bool = False
    auth_failed: bool = False
    bytes_out: int = 0


def run(target: Target, remote_command: str, *, user_scope_id: Optional[str],
        password: Optional[str] = None, stdin: Any = None, timeout: float = 120,
        stdout_path: Optional[Path] = None, max_bytes: int = MAX_TRANSFER_BYTES) -> Result:
    """Run `remote_command` on `target` as this account. Never raises for what happens on the
    other machine; SshError only for what could not even be started.

    `password` makes it a password login (the key stays out); without it the account's key is
    used. `stdin` is bytes (a sudo password line) or a binary file (an upload), streamed.
    With `stdout_path` the output is written there (a download) and bounded by `max_bytes`."""
    require_openssh()
    if not password:
        ensure_identity(user_scope_id)
        passphrase = _passphrase(user_scope_id)
        if not passphrase:
            raise SshError("this account's SSH key has lost its passphrase; remove the key in "
                           "Settings, Connections, SSH to create a new one, and install it again")
        env = _env(passphrase=passphrase)
    else:
        env = _env(password=password)
    argv = build_argv(target, remote_command, user_scope_id=user_scope_id,
                      password_login=bool(password))
    was_known = is_known(target, user_scope_id)
    timeout = max(float(MIN_TIMEOUT_SECONDS), min(float(timeout or 120), float(MAX_TIMEOUT_SECONDS)))

    kwargs: Dict[str, Any] = {"stdin": subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                              "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "env": env}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    # The download target is opened before ssh starts: one that cannot be written is an error
    # while nothing runs yet, never an exception with a live child, its timer and its readers.
    try:
        sink = open(stdout_path, "wb") if stdout_path is not None else None
    except OSError as e:
        raise SshError(f"the download target cannot be written: {e}") from None
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except OSError as e:
        if sink is not None:
            sink.close()
        raise SshError(f"ssh could not be started: {e}") from None

    state = {"timed_out": False, "too_large": False}

    def _stop() -> None:
        from vaf.core.platform import Platform
        Platform.terminate_process_tree(proc.pid, grace=2.0,
                                        pgid=None if os.name == "nt" else proc.pid)

    def _on_timeout() -> None:
        state["timed_out"] = True
        _stop()

    timer = threading.Timer(timeout, _on_timeout)
    timer.daemon = True
    err_chunks: List[bytes] = []

    def _read_err() -> None:
        for chunk in iter(lambda: proc.stderr.read(65536), b""):
            if sum(len(c) for c in err_chunks) < 1_000_000:
                err_chunks.append(chunk)

    def _feed() -> None:
        try:
            if isinstance(stdin, (bytes, bytearray)):
                proc.stdin.write(bytes(stdin))
            else:
                for chunk in iter(lambda: stdin.read(65536), b""):
                    proc.stdin.write(chunk)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    threads = [threading.Thread(target=_read_err, daemon=True)]
    if stdin is not None:
        threads.append(threading.Thread(target=_feed, daemon=True))
    timer.start()
    for t in threads:
        t.start()
    out_chunks: List[bytes] = []
    written = 0
    try:
        for chunk in iter(lambda: proc.stdout.read(65536), b""):
            written += len(chunk)
            if sink is not None:
                if written > max_bytes:
                    state["too_large"] = True
                    _stop()
                    break
                sink.write(chunk)
            elif written <= 2_000_000:
                out_chunks.append(chunk)
        proc.wait()
    finally:
        timer.cancel()
        if sink is not None:
            sink.close()
        for t in threads:
            t.join(timeout=5)

    stderr = b"".join(err_chunks).decode("utf-8", errors="replace")
    result = Result(returncode=proc.returncode if proc.returncode is not None else -1,
                    stdout=b"".join(out_chunks).decode("utf-8", errors="replace"),
                    stderr=stderr, timed_out=state["timed_out"],
                    too_large=state["too_large"], bytes_out=written)
    result.host_key_changed = ("REMOTE HOST IDENTIFICATION HAS CHANGED" in stderr
                               or (was_known and "Host key verification failed" in stderr))
    result.auth_failed = result.returncode == 255 and "Permission denied" in stderr
    if not was_known and is_known(target, user_scope_id):
        result.first_contact = True
        result.fingerprint = fingerprint(target, user_scope_id)
    return result
