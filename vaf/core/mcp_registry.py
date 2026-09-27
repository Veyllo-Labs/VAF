# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
MCP server registry — discover the tools of configured MCP servers and expose each one as a native
VAF tool (a dynamically-built BaseTool named ``mcp_<server>_<tool>``).

Servers are declared in a hot-reloadable manifest ``mcp_servers.json`` (manifest style, mirroring
vaf/core/custom_tools_registry.py — not config.py, which is for core/sacred settings):

    {
      "servers": {
        "filesystem": {
          "command": "npx -y @modelcontextprotocol/server-filesystem /some/path",
          "transport": "stdio",          # stdio (default) | http (Streamable HTTP) | sse
          "enabled": true,
          "permission_level": "write",    # default "write" (plan-gated, automation-safe);
                                          # "dangerous" forces a confirmation prompt; "read" = no gate
          "url": "",                      # only for http/sse
          "auth": "",                     # "oauth": every account signs in on its own (http/sse)
          "oauth_client_id": ""           # a client registered at the service by hand (optional)
        }
      }
    }

Secrets are not kept here: a local server's `env` values, a remote server's access token, the
secret of a hand-registered client and every account's sign-in live in the encrypted key ring
(vaf/core/mcp_secrets.py; the sign-in flow is vaf/core/mcp_oauth.py). A value written into this
file by hand is moved there the next time the file is loaded; the file keeps the env NAMES.

Discovery is eager + parallel with a per-batch deadline: a server that is slow / hung / misconfigured
is terminated and skipped — it never blocks VAF startup (same discipline as the bootstrap fix).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Manifest location + I/O (mirrors custom_tools_registry) ──────────────────────────────────────

def get_mcp_manifest_path() -> Path:
    """Path to mcp_servers.json (created lazily next to the custom-tools data)."""
    from vaf.core.platform import Platform
    directory = Platform.data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "mcp_servers.json"


def load_mcp_manifest() -> Dict[str, Any]:
    """Read mcp_servers.json. Returns {} on a missing or malformed file (fail-open, never raises).
    A secret found in the file is moved into the key ring and the file is saved without it."""
    path = get_mcp_manifest_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        logger.error("mcp_registry: failed to read %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    try:
        from vaf.core.mcp_secrets import move_manifest_secrets
        if move_manifest_secrets(data):
            save_mcp_manifest(data)
    except Exception as exc:  # noqa: BLE001 - the servers still load; the move is tried again
        logger.error("mcp_registry: could not move secrets out of %s: %s", path, exc)
    return data


def save_mcp_manifest(data: Dict[str, Any]) -> bool:
    """Atomically write the manifest (temp file + rename), like the custom-tools manifest.
    Never raises; False when the file could not be written (a caller that removes something
    else together with the entry, the server's secrets, must not do so then)."""
    path = get_mcp_manifest_path()
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        tmp.replace(path)
        return True
    except Exception as exc:
        logger.error("mcp_registry: failed to write %s: %s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


REMOTE_TRANSPORTS = ("http", "sse")


def server_headers(server_name: str, server_cfg: Dict[str, Any]) -> Dict[str, str]:
    """What a remote server is sent with every request: the manifest's own (non-secret)
    `headers` and the access token from the key ring as `Authorization: Bearer`. A server that
    signs every account in gets no fixed token: each session carries its account's own."""
    from vaf.core.mcp_oauth import uses_sign_in
    from vaf.core.mcp_remote import bearer_headers
    from vaf.core.mcp_secrets import server_token
    headers = {str(k): str(v) for k, v in (server_cfg.get("headers") or {}).items()
               if str(k).lower() != "authorization"} if isinstance(server_cfg.get("headers"), dict) else {}
    if not uses_sign_in(server_cfg):
        headers.update(bearer_headers(server_token(server_name)))
    return headers


def same_origin(url_a: str, url_b: str) -> bool:
    """Whether two URLs name the same server (scheme, host and port): the audience of a stored
    access token. A path may change; another host is another server."""
    from urllib.parse import urlsplit

    def origin(url: str):
        parts = urlsplit(str(url or "").strip())
        port = parts.port or {"http": 80, "https": 443}.get(parts.scheme.lower())
        return (parts.scheme.lower(), (parts.hostname or "").lower(), port)

    a, b = origin(url_a), origin(url_b)
    return bool(a[1]) and a == b


def _addressable(cfg: Dict[str, Any]) -> bool:
    """A server entry that can be reached: a command for stdio, a URL for a remote one."""
    transport = str(cfg.get("transport", "stdio"))
    return bool(cfg.get("url")) if transport in REMOTE_TRANSPORTS else bool(cfg.get("command"))


def _safe(name: str) -> str:
    """Sanitize a server/tool name into a valid tool-name segment ([A-Za-z0-9_])."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", str(name).strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "x"


# ── Dynamic tool factory ─────────────────────────────────────────────────────────────────────────

def make_mcp_tool(server_name: str, server_cfg: Dict[str, Any], tool_meta: Dict[str, Any]):
    """Build a BaseTool subclass wrapping a single MCP tool. The LLM sees a normal native tool; run()
    delegates to the shared MCP client (warm process cache) with server + tool pre-bound."""
    from vaf.tools.base import BaseTool
    from vaf.tools.mcp_client import get_mcp_client

    real_tool = str(tool_meta.get("name", "")).strip()
    tool_name = f"mcp_{_safe(server_name)}_{_safe(real_tool)}"
    base_desc = str(tool_meta.get("description") or real_tool).strip()
    description = f"{base_desc} (via MCP server '{server_name}')"
    parameters = tool_meta.get("inputSchema")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    # Permission: an optional per-tool override (server_cfg["tool_permissions"][<tool>]) wins,
    # otherwise the per-server level, default "write".
    _tool_perms = server_cfg.get("tool_permissions") if isinstance(server_cfg.get("tool_permissions"), dict) else {}
    permission = str(_tool_perms.get(real_tool) or server_cfg.get("permission_level", "write")).strip().lower()
    if permission not in ("read", "write", "dangerous", "system"):
        permission = "write"
    command = str(server_cfg.get("command", ""))
    transport = str(server_cfg.get("transport", "stdio"))
    server_url = str(server_cfg.get("url", ""))
    from vaf.core.mcp_oauth import uses_sign_in
    per_account = uses_sign_in(server_cfg)

    def _run(self, **kwargs) -> str:
        # Secrets are read at call time, from the ring, never captured in the tool.
        client = get_mcp_client()
        # The dispatcher's identity key is VAF's, never an argument for the server.
        user_scope_id = kwargs.pop("user_scope_id", None) if per_account else None
        try:
            if transport == "stdio":
                from vaf.core.mcp_secrets import effective_env
                return client._call_stdio(command, real_tool, kwargs, effective_env(server_name, server_cfg) or None)
            if transport in REMOTE_TRANSPORTS:
                auth = None
                if per_account:
                    from vaf.core.mcp_oauth import auth_for
                    from vaf.core.mcp_remote import McpSignInRequired
                    try:
                        auth = auth_for(server_name, server_cfg, user_scope_id)
                    except McpSignInRequired as exc:
                        return f"Error: {exc}"
                return client.call_remote(transport, server_url, real_tool, kwargs,
                                          headers=server_headers(server_name, server_cfg), auth=auth)
            return f"Error: Unsupported MCP transport '{transport}'"
        except Exception as exc:
            return f"Error calling MCP tool '{real_tool}': {exc}"

    attrs = {
        "name": tool_name,
        "description": description,
        "parameters": parameters,
        "permission_level": permission,
        "side_effect_class": "irreversible",
        # Every MCP server becomes its own bundle in the tool lists. The
        # category vocabulary is deliberately open, so a server the framework
        # has never heard of still groups correctly without a registry entry.
        "category": f"mcp_{_safe(server_name)}",
        "run": _run,
        "__doc__": description,
    }
    if per_account:
        # Each call runs in the CALLER's session at the server (vaf/core/mcp_oauth.py), so the
        # dispatcher has to say who the caller is.
        attrs["identity_kwargs"] = ("user_scope_id",)
    return type(f"MCPTool_{tool_name}", (BaseTool,), attrs)


# ── Eager + parallel discovery ─────────────────────────────────────────────────────────────────────

def discover_mcp_tools(timeout_seconds: float = 5.0):
    """Connect to every enabled server in the manifest in parallel, list its tools, and return
    ``(tools, status)`` where ``tools`` is {tool_name: BaseTool instance} and ``status`` is
    {server_name: {"connected": bool, "tool_count": int, "error": str|None}} for the UI. A server
    slower than the shared deadline is terminated and skipped — discovery never blocks longer than
    ~timeout_seconds and never raises."""
    manifest = load_mcp_manifest()
    servers = manifest.get("servers", {}) if isinstance(manifest, dict) else {}
    # A remote server has a URL and no command; it used to be skipped here, so no remote
    # server ever registered a tool.
    enabled = [
        (name, cfg) for name, cfg in servers.items()
        if isinstance(cfg, dict) and cfg.get("enabled", True) and _addressable(cfg)
    ]
    if not enabled:
        return {}, {}

    from vaf.tools.mcp_client import get_mcp_client
    client = get_mcp_client()

    discovered: Dict[str, List[Dict[str, Any]]] = {}
    reasons: Dict[str, str] = {}
    sign_in: Dict[str, bool] = {}

    def _discover(name: str, cfg: Dict[str, Any]) -> None:
        try:
            discovered[name] = _list_tools(name, cfg, client)
        except Exception as exc:
            discovered[name] = []
            reasons[name] = str(exc)
            if type(exc).__name__ == "McpSignInRequired":
                sign_in[name] = True

    threads = []
    for name, cfg in enabled:
        # Daemon threads so a hung server can never block process exit.
        th = threading.Thread(target=_discover, args=(name, cfg), daemon=True)
        th.start()
        threads.append((name, cfg, th))

    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    for _name, _cfg, th in threads:
        th.join(max(0.0, deadline - time.monotonic()))

    tools: Dict[str, Any] = {}
    status: Dict[str, Any] = {}
    for name, cfg, th in threads:
        if th.is_alive():
            # Timed out: terminate the server process to unblock the daemon thread, then skip it.
            try:
                proc = client._server_processes.get(str(cfg.get("command", "")))
                if proc is not None and proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
            logger.warning("MCP server '%s' timed out during discovery — skipped", name)
            status[name] = {"connected": False, "tool_count": 0, "error": "timeout"}
            continue
        count = 0
        skipped_task = 0
        for tm in discovered.get(name, []) or []:
            if not isinstance(tm, dict) or not tm.get("name"):
                continue
            # Skip tools that require MCP's optional task layer (tasks/* augmentation): VAF drives
            # tools via a synchronous tools/call, so a "required" tool can never run — don't offer it
            # to the LLM. "forbidden" / "optional" / absent all run fine over tools/call.
            _exec = tm.get("execution")
            if isinstance(_exec, dict) and str(_exec.get("taskSupport", "")).strip().lower() == "required":
                skipped_task += 1
                logger.info("mcp_registry: skipping task-only tool %s/%s (taskSupport=required)", name, tm.get("name"))
                continue
            try:
                inst = make_mcp_tool(name, cfg, tm)()
                tools[inst.name] = inst
                count += 1
            except Exception as exc:
                logger.warning("mcp_registry: failed to build tool %s/%s: %s", name, tm.get("name"), exc)
        if count > 0:
            status[name] = {"connected": True, "tool_count": count, "error": None}
        elif skipped_task > 0:
            # Reachable, but every tool needs the unsupported task layer — report it honestly.
            status[name] = {"connected": True, "tool_count": 0, "error": "all tools require the unsupported task layer"}
        else:
            status[name] = {"connected": False, "tool_count": 0,
                            "error": reasons.get(name) or "no tools (unreachable or empty)"}
            if sign_in.get(name):
                status[name]["sign_in_required"] = True
    if tools:
        logger.info("mcp_registry: registered %d MCP tool(s) from %d server(s)", len(tools), len(enabled))
    return tools, status


def _list_tools(name: str, cfg: Dict[str, Any], client, *, token: Optional[str] = None,
                env: Optional[Dict[str, str]] = None, auth=None, discover: bool = True) -> List[Dict[str, Any]]:
    """tools/list for one server entry, with its secrets from the key ring (or the ones given,
    for a server the editor is testing before it is saved). A server that signs every account
    in is asked as `auth`, or else (`discover`) as one signed-in account
    (`mcp_oauth.discovery_auth`)."""
    from vaf.core.mcp_oauth import discovery_auth, uses_sign_in
    from vaf.core.mcp_secrets import effective_env
    transport = str(cfg.get("transport", "stdio"))
    if transport in REMOTE_TRANSPORTS:
        headers = server_headers(name, cfg)
        if token:
            from vaf.core.mcp_remote import bearer_headers
            headers.update(bearer_headers(token))
        if auth is None and discover and uses_sign_in(cfg) and name:
            auth = discovery_auth(name, cfg)
        return client.list_server_tools("", transport, str(cfg.get("url", "")), headers=headers, auth=auth)
    return client.list_server_tools(str(cfg.get("command", "")), "stdio", "",
                                    (env if env is not None else effective_env(name, cfg)) or None)


def probe_mcp_server(server_cfg: Dict[str, Any], timeout_seconds: float = 5.0, *, name: str = "",
                     token: Optional[str] = None, user_scope_id: Optional[str] = None) -> Dict[str, Any]:
    """Test a single server config (for the UI "test connection" button): list its tools with a
    timeout, terminating a hung server. Returns {connected, tool_count, tools, error}; never raises.
    `name` is the server's name when it is already saved (its stored secrets are used); `token`
    and `server_cfg["env"]` values are the editor's, for a test before saving: an env value left
    empty falls back to the stored one.

    A remote server's stored token is used only when the tested URL names the same server as the
    saved one (`same_origin`): an editor pointed at another address, a typo or a new server under
    an old name, must not send the stored token there. It then needs a token of its own.

    A server that signs every account in (`"auth": "oauth"`) is tested as the tester's own
    account (`user_scope_id`) when it has signed in to the saved server, never as anybody
    else's; otherwise without a sign-in, and a 401 then reads as what it is: the server wants
    each account to sign in (`sign_in_required`), which is the expected answer before the first
    sign-in, not a failure."""
    from vaf.core.mcp_secrets import merge_env, server_env
    from vaf.tools.mcp_client import get_mcp_client
    client = get_mcp_client()
    cmd = str(server_cfg.get("command", ""))
    result: Dict[str, Any] = {}
    env = merge_env(server_env(name) if name else {}, server_cfg.get("env") if isinstance(server_cfg.get("env"), dict) else None)
    stored_name = name
    if name and str(server_cfg.get("transport", "stdio")) in REMOTE_TRANSPORTS:
        saved = ((load_mcp_manifest() or {}).get("servers") or {}).get(name)
        if not (isinstance(saved, dict) and str(saved.get("transport", "stdio")) in REMOTE_TRANSPORTS
                and same_origin(str(saved.get("url", "")), str(server_cfg.get("url", "")))):
            stored_name = ""

    from vaf.core import mcp_oauth
    auth = None
    if stored_name and mcp_oauth.uses_sign_in(server_cfg) and mcp_oauth.signed_in(stored_name, user_scope_id):
        saved_cfg = ((load_mcp_manifest() or {}).get("servers") or {}).get(stored_name) or {}
        if mcp_oauth.uses_sign_in(saved_cfg):
            auth = mcp_oauth.auth_for(stored_name, saved_cfg, user_scope_id)

    def _run() -> None:
        try:
            result["tools"] = _list_tools(stored_name, server_cfg, client, token=token, env=env,
                                          auth=auth, discover=False)
        except Exception as exc:
            result["error"] = str(exc)
            result["unauthorized"] = type(exc).__name__ == "RemoteMcpUnauthorized"

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(max(1.0, float(timeout_seconds)))
    if th.is_alive():
        try:
            proc = client._server_processes.get(cmd)
            if proc is not None and proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
        return {"connected": False, "tool_count": 0, "tools": [], "error": "timeout"}
    tools = result.get("tools") or []
    names = [t.get("name") for t in tools if isinstance(t, dict) and t.get("name")]
    out = {
        "connected": len(names) > 0,
        "tool_count": len(names),
        "tools": names,
        "error": result.get("error") or (None if names else "no tools (unreachable or empty)"),
    }
    if mcp_oauth.uses_sign_in(server_cfg) and auth is None and result.get("unauthorized"):
        out["sign_in_required"] = True
        out["error"] = ("the server is reachable and asks every account to sign in: save it, then "
                        "sign in under Settings > Connections > MCP services")
    return out


# ── Editing the manifest (the Settings form, and anything else that edits a server) ──────────────

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_PERMISSIONS = ("read", "write", "dangerous")


def upsert_server(name: str, *, command: str = "", transport: str = "stdio", url: str = "",
                  enabled: bool = True, permission_level: str = "write",
                  env: Optional[Dict[str, Any]] = None, token: Optional[str] = None,
                  clear_token: bool = False, auth: Optional[str] = None,
                  oauth_client_id: Optional[str] = None, oauth_client_secret: Optional[str] = None,
                  clear_oauth_client_secret: bool = False) -> None:
    """Add or change one server. Raises ValueError with a readable reason for a bad entry.

    Keys the form does not know (`tool_permissions`, `headers`) are kept: the form used to
    rewrite the whole entry and dropped them. Secrets go to the key ring
    (vaf/core/mcp_secrets.py): `env` values are merged (empty keeps the stored value, a name left
    out is removed), a non-empty `token` replaces the stored one, `clear_token` removes it; the
    manifest keeps only the env names.

    `auth="oauth"` makes a remote server sign every account in on its own (vaf/core/mcp_oauth.py)
    instead of sending one token for everybody, so a stored token is dropped. `oauth_client_id`
    and `oauth_client_secret` name a client the admin registered at the service by hand; without
    them each account registers its own client, where the service allows that. `auth`,
    `oauth_client_id` left as None keep what the entry has. The accounts' sign-ins belong to the
    server, the sign-in mode and the client they were made with: a server moved to another host,
    switched away from the sign-in or given another client drops them (and asks the service to
    invalidate them)."""
    from vaf.core import mcp_oauth, mcp_secrets

    name = str(name or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError("Server name must start with a letter and use only letters, digits, _ or -.")
    transport = str(transport or "stdio").strip()
    if transport not in ("stdio", *REMOTE_TRANSPORTS):
        raise ValueError(f"Unknown transport '{transport}'.")
    command = str(command or "").strip()
    url = str(url or "").strip()
    if transport == "stdio" and not command:
        raise ValueError("A command is required for stdio transport.")
    if transport in REMOTE_TRANSPORTS and not url.lower().startswith(("http://", "https://")):
        raise ValueError("A remote server needs an http:// or https:// URL.")
    permission = str(permission_level or "write").strip().lower()
    if permission not in _PERMISSIONS:
        permission = "write"

    manifest = load_mcp_manifest() or {}
    servers = manifest.get("servers") if isinstance(manifest.get("servers"), dict) else {}
    entry = dict(servers.get(name) or {}) if isinstance(servers.get(name), dict) else {}
    before = dict(entry)
    old_url = str(entry.get("url") or "")
    old_remote = str(entry.get("transport", "stdio")) in REMOTE_TRANSPORTS
    old_auth = "oauth" if mcp_oauth.uses_sign_in(entry) else ""
    new_auth = old_auth if auth is None else str(auth or "").strip().lower()
    if new_auth not in ("", "oauth"):
        raise ValueError(f"Unknown sign-in '{auth}'.")
    if new_auth == "oauth" and transport not in REMOTE_TRANSPORTS:
        raise ValueError("A sign-in per account needs a remote server (http or sse).")
    old_client_id = str(entry.get("oauth_client_id") or "").strip() if old_auth else ""
    new_client_id = (old_client_id if oauth_client_id is None else str(oauth_client_id or "").strip()) if new_auth else ""
    new_client_secret = str(oauth_client_secret or "").strip() if new_auth else ""
    if new_client_secret and not new_client_id:
        raise ValueError("A client secret needs the client ID it belongs to.")

    stored_env = mcp_secrets.server_env(name)
    new_env = mcp_secrets.merge_env(stored_env, env) if env is not None else stored_env
    mcp_secrets.set_server_env(name, new_env)
    # A stored token belongs to the server it was stored for: moved to another host without a
    # new token, the server keeps none rather than sending the old one to the new address. A
    # server that signs every account in has no token for everybody at all.
    moved = old_remote and bool(old_url) and not (transport in REMOTE_TRANSPORTS and same_origin(old_url, url))
    if clear_token or new_auth or (moved and not str(token or "").strip()):
        mcp_secrets.clear_server_token(name)
    elif str(token or "").strip():
        mcp_secrets.set_server_token(name, str(token))
    # The same for the accounts' sign-ins and the hand-registered client's secret: they belong
    # to this host, this sign-in mode and this client.
    if old_auth and (moved or not new_auth or new_client_id != old_client_id):
        mcp_oauth.forget_server(name, before)
    if new_client_secret:
        mcp_secrets.set_oauth_client_secret(name, new_client_secret)
    elif (clear_oauth_client_secret or not new_auth or moved or new_client_id != old_client_id) \
            and mcp_secrets.oauth_client_secret(name):
        mcp_secrets.clear_oauth_client_secret(name)
    for key, value in (("auth", new_auth), ("oauth_client_id", new_client_id)):
        if value:
            entry[key] = value
        else:
            entry.pop(key, None)
    entry.pop("oauth_client_secret", None)
    entry.update({
        "command": command,
        "transport": transport,
        "url": url,
        "enabled": bool(enabled),
        "permission_level": permission,
        "env": {key: "" for key in new_env},
    })
    entry.pop("token", None)
    servers[name] = entry
    manifest["servers"] = servers
    save_mcp_manifest(manifest)
    # A changed token or URL opens a fresh session; the old address's session is closed too,
    # or it stays open, token and all, for the rest of the process.
    stale = {u for u in (old_url if old_remote else "", url if transport in REMOTE_TRANSPORTS else "") if u}
    if stale:
        try:
            from vaf.core.mcp_remote import get_remote_pool
            for stale_url in stale:
                get_remote_pool().close(stale_url)
        except Exception:  # noqa: BLE001
            pass


def remove_server(name: str) -> bool:
    """Remove one server and everything the key ring holds for it. True when it existed and
    the manifest was written; a name the manifest does not have leaves the ring alone."""
    from vaf.core import mcp_secrets
    manifest = load_mcp_manifest() or {}
    servers = manifest.get("servers") if isinstance(manifest.get("servers"), dict) else {}
    cfg = servers.pop(str(name), None)
    if cfg is None:
        return False
    manifest["servers"] = servers
    # Secrets go only once the entry is gone: a write that fails leaves the server configured,
    # and it must keep its token and env then.
    if not save_mcp_manifest(manifest):
        return False
    from vaf.core.mcp_oauth import forget_server
    forget_server(str(name), cfg if isinstance(cfg, dict) else {})     # the service is asked to invalidate them
    mcp_secrets.clear_server_secrets(str(name))
    if isinstance(cfg, dict) and cfg.get("url"):
        try:
            from vaf.core.mcp_remote import get_remote_pool
            get_remote_pool().close(str(cfg.get("url")))
        except Exception:  # noqa: BLE001
            pass
    return True


def servers_for_display(status: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """The server list the Settings page may see: every entry with its live status, the env
    names without values, whether a token or a client secret is stored and how many accounts
    signed in, never a secret."""
    from vaf.core.mcp_oauth import uses_sign_in
    from vaf.core.mcp_secrets import oauth_accounts, oauth_client_secret, oauth_record, redacted_env, server_token
    servers = (load_mcp_manifest() or {}).get("servers", {}) or {}
    status = dict(status or {})
    out = []
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        st = status.get(name, {})
        per_account = uses_sign_in(cfg)
        signed_in = sum(1 for account in oauth_accounts(name)
                        if (oauth_record(name, account).get("tokens") or {})) if per_account else 0
        out.append({
            "name": name,
            "command": cfg.get("command", ""),
            "transport": cfg.get("transport", "stdio"),
            "url": cfg.get("url", ""),
            "enabled": bool(cfg.get("enabled", True)),
            "permission_level": cfg.get("permission_level", "write"),
            "env": redacted_env(name, cfg),
            "token_set": bool(server_token(name)) if str(cfg.get("transport", "stdio")) in REMOTE_TRANSPORTS and not per_account else False,
            "auth": "oauth" if per_account else "",
            "oauth_client_id": str(cfg.get("oauth_client_id") or "") if per_account else "",
            "oauth_client_secret_set": bool(oauth_client_secret(name)) if per_account else False,
            "accounts_signed_in": signed_in,
            "sign_in_required": bool(st.get("sign_in_required", False)),
            "connected": bool(st.get("connected", False)),
            "tool_count": int(st.get("tool_count", 0) or 0),
            "error": st.get("error"),
        })
    return out
