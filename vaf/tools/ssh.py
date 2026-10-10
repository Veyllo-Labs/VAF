# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""ssh: run commands on another machine, copy files to and from it, as this account.

Built on vaf/core/ssh.py (the account's own key and servers, the prompt answer, the command
line); this module is the tool's contract around it:

- Every call is confirmed in the chat (`dangerous`), and a trusted FOLDER does not silence it
  (`trusted_dir_grants = False`): a server is not in a folder. The FIRST connection to a
  server this account has not confirmed is always asked (`ask_reason`), also under "always";
  where nobody is asked (a workflow step) it is refused (`accepts_call_confirmation`), so an
  unattended run only reaches servers a person confirmed.
- Not over messaging channels: dangerous and restricted with "channel", which the policy
  refuses there before any admin lift (vaf/core/tool_contract.py, section 1a).
- A regular account gets it only when its allowlist names it (`account_opt_in`).
- The password is never an argument: `login_credential` NAMES a credential the person stored
  (vaf/core/user_secrets.py), and its value is taken out of everything the tool returns. The
  parameter names avoid "pass" and "secret" on purpose: the confirmation dialog hides any
  argument named like a secret (vaf/core/arg_preview.py), and the person should see WHICH
  stored credential a call uses - it is a name, not a value.
- The remote command is checked with the `remote` profile of vaf/core/command_policy.py.
- `upload` takes a folder as well (a built site): an uncompressed tar stream unpacked into
  remote_path, without links and without .git and .vaf, within the same 500 MB bound.
- The coder uses this tool only in a run started with deploy_to, pinned to that one server
  (vaf/tools/coder.py, _deploy_refusal).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from vaf.tools.base import BaseTool

_OUT_LIMIT = 8000
_ERR_LIMIT = 4000


def transfer_local_path(kwargs: Dict[str, Any], *, download: bool) -> Path:
    """The local side of an upload or download, for ssh and ftp alike: inside this person's
    own files (the write jail is installed around run by `file_access`). Relative means this
    chat's workspace; a download without local_path lands there under the remote name."""
    from vaf.tools.filesystem import is_safe_path
    raw = str(kwargs.get("local_path") or "").strip()
    if not raw and download:
        raw = os.path.basename(str(kwargs.get("remote_path") or "").rstrip("/")) or "download"
    if not raw:
        raise ValueError("local_path is needed")
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        from vaf.core.session import get_session_workspace_dir
        from vaf.core.subagent_ipc import get_current_session_id
        ws = get_session_workspace_dir(get_current_session_id(), create=True)
        if not ws:
            raise ValueError("a relative local_path needs a chat workspace; pass an "
                             "absolute path")
        path = Path(ws) / path
    safe, resolved = is_safe_path(str(path))
    if not safe:
        raise ValueError(str(resolved))
    return Path(resolved)


class SshTool(BaseTool):
    name = "ssh"
    category = "code"
    permission_level = "dangerous"
    side_effect_class = "irreversible"
    channel_restrictions = ("channel",)
    trusted_dir_grants = False
    accepts_call_confirmation = True
    account_opt_in = True
    identity_kwargs = ("user_scope_id", "username", "user_role")
    # The local side of upload/download stays inside this person's own files.
    file_access = "write"
    input_aliases = {"server": ["host", "target", "destination"],
                     "command": ["cmd", "remote_command"],
                     "login_credential": ["password_secret", "password_name", "credential"],
                     "sudo_credential": ["sudo_secret", "sudo_password_name"]}
    description = (
        "Work on ANOTHER machine over SSH, e.g. to update or set up a server. action='run' "
        "runs `command` there; 'upload' copies local_path (a file, or a folder: remote_path is "
        "then the folder it lands in) to remote_path; 'download' copies "
        "remote_path to local_path (default: this chat's workspace); 'install_key' puts this "
        "account's own key on the server once, so later calls need no password. `server` is "
        "user@host or user@host:port. For a password login pass login_credential: the NAME of "
        "a credential the user stored (store the password with store_credential first, never "
        "put it in the command). as_root=true runs the command through sudo (sudo_credential: the "
        "stored sudo password, default login_credential). Commands must not wait for input: "
        "use -y and DEBIAN_FRONTEND=noninteractive. The first connection to a server asks the "
        "user and names its fingerprint. Use this instead of ssh in host_bash. Building "
        "something AND putting it on a server is the coder's job: start coding_agent with "
        "deploy_to=\"user@host:/folder\" (a server the user confirmed) and it uploads its own "
        "result. Use ssh yourself for server work that builds nothing: an update, a restart, a "
        "look at a log."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["run", "upload", "download", "install_key"],
                       "description": "What to do (default run)."},
            "server": {"type": "string", "description": "user@host or user@host:port."},
            "command": {"type": "string", "description": "For run: the command to run there."},
            "login_credential": {"type": "string",
                                "description": "NAME of a stored credential with the login "
                                               "password, e.g. SERVER_PASS. Leave out for a "
                                               "key login."},
            "as_root": {"type": "boolean", "description": "Run the command through sudo.",
                        "default": False},
            "sudo_credential": {"type": "string",
                            "description": "NAME of the stored sudo password (default: "
                                           "login_credential)."},
            "local_path": {"type": "string", "description": "For upload: the file or folder here. For download: the file here."},
            "remote_path": {"type": "string", "description": "For upload/download: the file there (for a folder upload, the folder it lands in)."},
            "timeout": {"type": "integer", "default": 120,
                        "description": "Seconds (max 600). Raise it for an update that runs "
                                       "for minutes."},
        },
        "required": ["server"],
    }

    def budget_seconds(self, args):
        from vaf.core.ssh import MAX_TIMEOUT_SECONDS
        try:
            wanted = int(args.get("timeout") or 120)
        except (TypeError, ValueError):
            wanted = 120
        return min(max(5, wanted), MAX_TIMEOUT_SECONDS) + 30

    # ── the question before the call ──────────────────────────────────────────
    def ask_reason(self, args: Dict[str, Any], *, user_scope_id: Optional[str] = None,
                   username: Optional[str] = None) -> Optional[str]:
        from vaf.core import ssh
        raw = args.get("server") or args.get("host") or args.get("target") \
            or args.get("destination")
        try:
            target = ssh.parse_server(raw)
            if ssh.is_known(target, user_scope_id):
                return None
        except Exception:
            return (f"The server '{raw}' could not be checked, so this call is put to you.")
        return (f"First connection to {target}: the key it shows is remembered for this "
                "account, and a different key later is refused.")

    # ── the call ──────────────────────────────────────────────────────────────
    def run(self, **kwargs) -> str:
        from vaf.core import ssh, user_secrets
        from vaf.core.command_policy import is_command_safe
        from vaf.core.tool_dispatch import clip_middle

        scope = kwargs.get("user_scope_id")
        username = kwargs.get("username")
        action = str(kwargs.get("action") or "run").strip().lower()
        try:
            target = ssh.parse_server(kwargs.get("server"))
            ssh.require_openssh()
        except ssh.SshError as e:
            return f"Error: {e}."
        if not ssh.is_known(target, scope) and not kwargs.get("_call_confirmed"):
            return (f"Error: {target} is a server this account has not connected to yet. The "
                    "first connection must be confirmed by the user in the app; ask them.")

        secrets_env: Dict[str, str] = {}

        def _secret(name_arg: str) -> Optional[str]:
            name = kwargs.get(name_arg)
            if not name:
                return None
            try:
                env_name = user_secrets.env_name(name)
            except ValueError as e:
                raise ssh.SshError(f"{name_arg}: {e}") from None
            found = user_secrets.env_for("$" + env_name, user_scope_id=scope, username=username)
            if env_name not in found:
                raise ssh.SshError(
                    f"no stored credential is named {name!r}. Ask the user for it and store it "
                    "with store_credential, then pass its NAME")
            secrets_env[env_name] = found[env_name]
            return found[env_name]

        try:
            password = _secret("login_credential")
            sudo_password = _secret("sudo_credential") or password
        except ssh.SshError as e:
            return f"Error: {e}."

        stdin: Any = None
        stdout_path: Optional[Path] = None
        local: Optional[Path] = None
        uploaded_files: Optional[int] = None
        if action == "run":
            command = str(kwargs.get("command") or "").strip()
            if not command:
                return "Error: action 'run' needs a command."
            safe, note = is_command_safe(command, profile="remote")
            if not safe:
                return f"[BLOCKED] {note}"
            if kwargs.get("as_root") and target.user != "root":
                command = ssh.as_root(command, with_password=bool(sudo_password))
                if sudo_password:
                    stdin = (sudo_password + "\n").encode("utf-8")
        elif action in ("upload", "download"):
            try:
                local = self._local_path(kwargs, download=(action == "download"))
            except ValueError as e:
                return f"Error: {e}"
            remote = str(kwargs.get("remote_path") or "").strip()
            if not remote:
                return f"Error: action '{action}' needs remote_path."
            import shlex
            if action == "upload" and local.is_dir():
                try:
                    stdin, uploaded_files = self._folder_stream(local, ssh.MAX_TRANSFER_BYTES)
                except (ValueError, OSError) as e:
                    return f"Error: {e}"
                command = f"mkdir -p {shlex.quote(remote)} && tar -xf - -C {shlex.quote(remote)}"
            elif action == "upload":
                if not local.is_file():
                    return f"Error: {local} is not a file or a folder."
                if local.stat().st_size > ssh.MAX_TRANSFER_BYTES:
                    return "Error: the file is larger than 500 MB."
                command = "cat > " + shlex.quote(remote)
                stdin = open(local, "rb")
            else:
                command = "cat " + shlex.quote(remote)
                stdout_path = local.with_name(local.name + ".part")
        elif action == "install_key":
            try:
                ssh.ensure_identity(scope)
            except ssh.SshError as e:
                return f"Error: {e}."
            command = ssh.install_key_command(ssh.public_key(scope) or "")
        else:
            return f"Error: unknown action {action!r} (run, upload, download, install_key)."

        try:
            result = ssh.run(target, command, user_scope_id=scope, password=password,
                             stdin=stdin, timeout=kwargs.get("timeout") or 120,
                             stdout_path=stdout_path)
        except ssh.SshError as e:
            return f"Error: {e}."
        finally:
            if hasattr(stdin, "close"):
                stdin.close()

        return self._report(action, target, result, local, stdout_path, scope,
                            secrets_env, clip_middle, uploaded_files=uploaded_files)

    # ── helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _local_path(kwargs: Dict[str, Any], *, download: bool) -> Path:
        return transfer_local_path(kwargs, download=download)

    # What a folder upload leaves out: a repository's history and the coder's own notes are
    # not part of what gets deployed.
    _FOLDER_SKIP = (".git", ".vaf")

    @classmethod
    def _folder_stream(cls, folder: Path, limit: int):
        """A folder as an uncompressed tar in a temporary file, for `tar -xf -` on the server:
        (open file at its start, number of files). Links are left out - one could point at a
        file outside the folder - and so are .git and .vaf. Refused above `limit` bytes of
        archive, headers included: many small files add a header block each."""
        import tarfile
        import tempfile
        buf = tempfile.TemporaryFile()
        count = total = 0
        too_large = ValueError(f"the folder is larger than {limit // (1024 * 1024)} MB")

        def _keep(info: tarfile.TarInfo):
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            return info

        try:
            with tarfile.open(fileobj=buf, mode="w") as tar:
                for path in sorted(folder.rglob("*")):
                    rel = path.relative_to(folder)
                    if any(part in cls._FOLDER_SKIP for part in rel.parts) or path.is_symlink():
                        continue
                    if path.is_file():
                        total += path.stat().st_size
                        if total > limit:
                            raise too_large         # before reading a file that cannot fit
                        count += 1
                    elif not path.is_dir():
                        continue
                    tar.add(str(path), arcname=rel.as_posix(), recursive=False, filter=_keep)
                    if buf.tell() > limit:
                        raise too_large
        except Exception:
            buf.close()
            raise
        buf.seek(0)
        return buf, count

    @staticmethod
    def _report(action, target, result, local, stdout_path, scope, secrets_env,
                clip_middle, *, uploaded_files: Optional[int] = None) -> str:
        from vaf.core import ssh, user_secrets
        lines = [f"[SSH {target}]"]
        if result.first_contact:
            lines.append(f"First connection: the server's key {result.fingerprint} is now "
                         "remembered for this account.")
        if result.host_key_changed:
            if stdout_path is not None:
                stdout_path.unlink(missing_ok=True)
            return (f"[SSH {target}] REFUSED: the server shows a DIFFERENT key than the one "
                    "remembered. Either it was reinstalled, or someone is in between. Tell the "
                    "user; only if they know why, they remove the server in Settings, "
                    "Connections, SSH (or `vaf ssh forget`), and the next call asks again.")
        if result.auth_failed:
            if stdout_path is not None:
                stdout_path.unlink(missing_ok=True)
            pub = ssh.public_key(scope)
            hint = ("The stored password was not accepted." if secrets_env else
                    "This account's key is not installed on the server.")
            return (f"[SSH {target}] Login refused. {hint} Either install the key once with "
                    "action='install_key' and login_credential, or put this public key into "
                    f"the server's authorized_keys (a hoster's panel has a field for it):\n{pub}")
        if result.timed_out:
            lines.append("Stopped: the time limit was reached.")
        if action == "download" and stdout_path is not None:
            if result.too_large:
                stdout_path.unlink(missing_ok=True)
                return f"[SSH {target}] Download stopped: the file is larger than 500 MB."
            if result.returncode == 0 and not result.timed_out:
                os.replace(stdout_path, local)
                lines.append(f"Downloaded {result.bytes_out} bytes to {local}.")
            else:
                stdout_path.unlink(missing_ok=True)
        elif action == "upload" and result.returncode == 0 and uploaded_files is not None:
            lines.append(f"Uploaded the folder {local} ({uploaded_files} files).")
        elif action == "upload" and result.returncode == 0:
            lines.append(f"Uploaded {local} ({local.stat().st_size} bytes).")
        elif action == "install_key" and result.returncode == 0:
            lines.append("This account's key is installed: later calls need no password.")
        out = user_secrets.scrub(result.stdout.strip(), secrets_env)
        err = user_secrets.scrub(result.stderr.strip(), secrets_env)
        err = "\n".join(ln for ln in err.splitlines()
                        if "Permanently added" not in ln).strip()
        if out:
            lines.append(clip_middle(out, _OUT_LIMIT, marker="... ({left_out} chars left out) ..."))
        if err:
            lines.append("[stderr]\n" + clip_middle(err, _ERR_LIMIT,
                                                    marker="... ({left_out} chars left out) ..."))
        lines.append("OK" if result.returncode == 0 else f"Exit {result.returncode}")
        return "\n".join(lines)
