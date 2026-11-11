# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""ftp: files on another machine over FTPS (or plain FTP when written so), as this account.

Built on vaf/core/ftp.py (the account's own confirmed servers, the certificate check, the
transfers); this module is the tool's contract around it, the same one as the ssh tool's:

- Every call is confirmed in the chat (`dangerous`), and a trusted FOLDER does not silence it
  (`trusted_dir_grants = False`): a server is not in a folder. The FIRST connection to a
  server this account has not confirmed is always asked (`ask_reason`), also under "always";
  where nobody is asked (a workflow step) it is refused (`accepts_call_confirmation`), so an
  unattended run only reaches servers a person confirmed. A plain `ftp://` server says in that
  question that the password and the files cross the network readable.
- Not over messaging channels, and a regular account gets it only when its allowlist names it
  (`account_opt_in`).
- The password is never an argument: `login_credential` NAMES a credential the person stored
  (vaf/core/user_secrets.py), and its value is taken out of everything the tool returns.
- A stored host, login or port is written `$NAME` inside `server`
  (`ftp://$FTP_USER@$FTP_HOST:$FTP_PORT`) and filled in here (`user_secrets.expand`); the values
  are taken out of every answer too. The first-connection question names the resolved host and
  port: the agent can store a value under any name itself (store_credential), so consent given
  on names alone could send the password to a host it planted.
- `read` takes part of a file (the end of a big log) without downloading it, `stat` its size
  and modification time.
- `upload` takes a folder as well (a built site): its files land INSIDE remote_path, without
  links and without .git and .vaf, within 500 MB.
- `host_bash` refuses ftp clients and curl/wget with an ftp:// address, and python_exec and
  `python -c` refuse ftplib and ftp:// addresses, all pointing here
  (vaf/core/command_policy.py, ftp_transfer).
- The coder uses this tool only in a run started with deploy_to=ftps://..., pinned to that
  one server and folder (vaf/tools/coder.py, _deploy_refusal).
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

from vaf.tools.base import BaseTool

_OUT_LIMIT = 8000
MAX_TIMEOUT_SECONDS = 600
ACTIONS = ("list", "upload", "download", "delete", "read", "stat")
READ_DEFAULT_BYTES = 8000


class FtpTool(BaseTool):
    name = "ftp"
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
                     "remote_path": ["path", "remote"],
                     "login_credential": ["password_secret", "password_name", "credential"]}
    description = (
        "Work with files on ANOTHER machine over FTP, e.g. a web space that offers no SSH. "
        "action='list' shows remote_path (default the login folder); 'upload' copies local_path "
        "(a file to remote_path, or a folder INTO remote_path, e.g. a built site) there; "
        "'download' copies remote_path here (default: this chat's workspace); 'delete' removes "
        "one file there; 'read' returns part of a file without downloading it (offset, "
        "max_bytes; a negative offset counts from the end, e.g. -20000 for the end of a big "
        "log); 'stat' gives a file's size and modification time. `server` is "
        "ftps://user@host[:port] (encrypted, the default when the scheme is left out) or "
        "ftp://user@host for a server without encryption; a host, login or port the user "
        "stored as a credential is written $NAME: ftp://$FTP_USER@$FTP_HOST:$FTP_PORT. Pass "
        "login_credential: the NAME of a credential the user stored (store the password with "
        "store_credential first, never put it anywhere else). The first connection to a server "
        "asks the user. Use this instead of curl, lftp or ftp in host_bash. Building something "
        "AND putting it on a web space is the coder's job: start coding_agent with "
        "deploy_to=\"ftps://user@host/folder\" (a server the user confirmed) and it uploads its "
        "own result. Use ftp yourself for file work that builds nothing: a look at what is "
        "there, one file up or down."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS),
                       "description": "What to do (default list). read: part of a file, e.g. "
                                      "the end of a big log, without downloading it; stat: "
                                      "size and modification time."},
            "server": {"type": "string",
                       "description": "ftps://user@host[:port], or ftp://user@host for a server "
                                      "without encryption. A stored host, login or port: "
                                      "ftp://$FTP_USER@$FTP_HOST:$FTP_PORT."},
            "remote_path": {"type": "string",
                            "description": "The folder to list, the file to download, delete, "
                                           "read or stat, the file an upload becomes, or the "
                                           "folder an uploaded folder lands in."},
            "offset": {"type": "integer", "default": 0,
                       "description": "For read: the byte to start at; negative counts from "
                                      "the end (-20000: the last 20 KB)."},
            "max_bytes": {"type": "integer", "default": READ_DEFAULT_BYTES,
                          "description": "For read: how many bytes at most (up to 65536)."},
            "local_path": {"type": "string",
                           "description": "For upload: the file or folder here. For download: "
                                          "the file here."},
            "login_credential": {"type": "string",
                                 "description": "NAME of a stored credential with the FTP "
                                                "password, e.g. FTP_PASS. Leave out for an "
                                                "anonymous login."},
            "timeout": {"type": "integer", "default": 300,
                        "description": "Seconds the whole call may take (max 600)."},
        },
        "required": ["server"],
    }

    def budget_seconds(self, args):
        try:
            wanted = int((args or {}).get("timeout") or 300)
        except (TypeError, ValueError):
            wanted = 300
        return min(max(10, wanted), MAX_TIMEOUT_SECONDS) + 30

    # ── the question before the call ──────────────────────────────────────────
    def ask_reason(self, args: Dict[str, Any], *, user_scope_id: Optional[str] = None,
                   username: Optional[str] = None) -> Optional[str]:
        from vaf.core import ftp
        raw = (args.get("server") or args.get("host") or args.get("target")
               or args.get("destination"))
        try:
            text, used = self._expand(raw, user_scope_id, username)
            target = ftp.parse_server(text)
            if ftp.is_known(target, user_scope_id):
                return None
        except Exception:
            return f"The server '{raw}' could not be checked, so this call is put to you."
        if used:
            # The resolved scheme, host and port, never the login or a password: the person
            # must see where the password goes, not only which names were written.
            names = ", ".join(sorted(n[len("VAF_SECRET_"):] for n in used))
            where = f"{target.name} (written {raw}; {names} filled in from stored credentials)"
        else:
            where = str(target)
        if not target.tls:
            return (f"First connection to {where}, WITHOUT encryption: the password and the "
                    "files cross the network readable. Only if the server offers nothing else.")
        return (f"First connection to {where}: its certificate is checked and remembered for "
                "this account, and a different one later is refused.")

    @staticmethod
    def _expand(raw, user_scope_id, username) -> Tuple[str, Dict[str, str]]:
        """The server text with every `$NAME` filled in, and the env of the stored names it
        used. Raises ValueError for a name this person has not stored."""
        from vaf.core import user_secrets
        text = str(raw or "").strip()
        if "$" not in text:
            return text, {}
        return user_secrets.expand(text, user_scope_id=user_scope_id, username=username)

    # ── the call ──────────────────────────────────────────────────────────────
    def run(self, **kwargs) -> str:
        from vaf.core import user_secrets
        # Every stored value this call used (the server's parts, the password) is taken out of
        # EVERY answer, also an error raised before the transfer or one nobody foresaw.
        secrets_env: Dict[str, str] = {}
        try:
            out = self._run(kwargs, secrets_env)
        except Exception as e:      # noqa: BLE001 - scrubbed here, never raw to the dispatcher
            out = f"Error: {e}"
        return user_secrets.scrub(out, secrets_env)

    def _run(self, kwargs: Dict[str, Any], secrets_env: Dict[str, str]) -> str:
        from vaf.core import ftp, user_secrets
        from vaf.core.bounded_run import cancel_check
        from vaf.tools.ssh import transfer_local_path

        scope = kwargs.get("user_scope_id")
        username = kwargs.get("username")
        action = str(kwargs.get("action") or "list").strip().lower()
        if action not in ACTIONS:
            return f"Error: unknown action {action!r} ({', '.join(ACTIONS)})."
        written = str(kwargs.get("server") or "").strip()
        try:
            expanded, used = self._expand(written, scope, username)
        except ValueError as e:
            return (f"Error: server: {e}. A stored host, login or port is written $NAME, NAME "
                    "being a credential the user stored (store_credential, or Settings, "
                    "Connections, Credentials for commands).")
        # Registered before parsing: a parse error quotes the filled-in text.
        secrets_env.update(used)
        try:
            target = ftp.parse_server(expanded)
        except ftp.FtpError as e:
            return f"Error: {e}."
        # The server as written when it came from stored names: its values stay out of the
        # answer, as the password does.
        shown = written if used else str(target)
        if not ftp.is_known(target, scope) and not kwargs.get("_call_confirmed"):
            return (f"Error: {target} is a server this account has not connected to yet. The "
                    "first connection must be confirmed by the user in the app; ask them.")

        password: Optional[str] = None
        name = kwargs.get("login_credential")
        if name:
            try:
                env_name = user_secrets.env_name(name)
            except ValueError as e:
                return f"Error: login_credential: {e}."
            found = user_secrets.env_for("$" + env_name, user_scope_id=scope, username=username)
            if env_name not in found:
                return (f"Error: no stored credential is named {name!r}. Ask the user for it and "
                        "store it with store_credential, then pass its NAME.")
            secrets_env[env_name] = password = found[env_name]

        remote = str(kwargs.get("remote_path") or "").strip()
        if action in ("upload", "download", "delete", "read", "stat") and not remote:
            return f"Error: action '{action}' needs remote_path."
        try:
            offset = int(kwargs.get("offset") or 0)
            max_bytes = int(kwargs.get("max_bytes") or READ_DEFAULT_BYTES)
        except (TypeError, ValueError):
            return "Error: offset and max_bytes are whole numbers of bytes."
        local = None
        if action in ("upload", "download"):
            try:
                local = transfer_local_path(kwargs, download=(action == "download"))
            except ValueError as e:
                return f"Error: {e}"
            # Before connecting: a missing local file is no reason to reach (and, the first
            # time, remember) a server.
            if action == "upload" and not (local.is_file() or local.is_dir()):
                return f"Error: {local} is not a file or a folder."

        try:
            seconds = min(max(10, int(kwargs.get("timeout") or 300)), MAX_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            seconds = 300
        deadline = time.monotonic() + seconds
        stopped = cancel_check()

        def _stop() -> bool:
            return stopped() or time.monotonic() > deadline

        lines = [f"[FTP {shown}]"]
        try:
            session = ftp.connect(target, user_scope_id=scope, password=password,
                                  confirmed=bool(kwargs.get("_call_confirmed")))
        except ftp.FtpError as e:
            return f"[FTP {shown}] {e}."
        except Exception as e:      # noqa: BLE001 - a fault outside ftplib's own errors
            return f"[FTP {shown}] Error: {e}"
        try:
            if session.first_contact:
                lines.append(self._first_contact_line(session))
            lines.extend(session.notes)
            if action == "list":
                entries = ftp.list_dir(session, remote or ".")
                text = "\n".join(entries) if entries else "(empty)"
                lines.append(text[:_OUT_LIMIT] + ("\n... (more entries left out)"
                                                   if len(text) > _OUT_LIMIT else ""))
            elif action == "upload":
                result = ftp.upload(session, local, remote, check_stop=_stop)
                if local.is_dir():
                    lines.append(f"Uploaded the folder {local} into {remote} "
                                 f"({result['files']} files, {result['bytes']} bytes).")
                else:
                    lines.append(f"Uploaded {local} to {remote} ({result['bytes']} bytes).")
            elif action == "download":
                n = ftp.download(session, remote, local, check_stop=_stop)
                lines.append(f"Downloaded {n} bytes to {local}.")
            elif action == "read":
                lines.append(self._read_text(ftp.read(session, remote, offset=offset,
                                                      max_bytes=max_bytes, check_stop=_stop)))
            elif action == "stat":
                lines.append(self._stat_line(ftp.stat(session, remote)))
            else:
                ftp.delete(session, remote)
                lines.append(f"Deleted {remote}.")
        except ftp.FtpError as e:
            lines.append(f"Error: {e}.")
            return "\n".join(lines)
        except Exception as e:      # the server's own refusal (550 ...), a dropped connection
            lines.append(f"Error: {e}")
            return "\n".join(lines)
        finally:
            ftp.close(session)
        lines.append("OK")
        return "\n".join(lines)

    @staticmethod
    def _read_text(result: Dict[str, Any]) -> str:
        data, start, size = result["data"], result["start"], result["size"]
        total = "unknown" if size is None else str(size)
        if not data:
            return f"No bytes from {start} on (the file has {total} bytes)."
        head = f"Bytes {start}-{start + len(data) - 1} of {total}:"
        if b"\0" in data:
            return f"{head}\nBinary content ({len(data)} bytes); use download."
        return f"{head}\n{data.decode('utf-8', errors='replace')}"

    @staticmethod
    def _stat_line(info: Dict[str, Any]) -> str:
        when = f", modified {info['modified']}" if info.get("modified") else ""
        if info.get("type") == "folder":
            return f"folder{when}"
        size = "size unknown" if info.get("size") is None else f"{info['size']} bytes"
        return f"file, {size}{when}"

    @staticmethod
    def _first_contact_line(session) -> str:
        if session.trust == "authority":
            return ("First connection: a certificate authority vouches for this server; it is "
                    "now remembered for this account.")
        if session.trust == "none":
            return ("First connection, without encryption: the server is now remembered for "
                    "this account.")
        return "First connection: the server is now remembered for this account."
