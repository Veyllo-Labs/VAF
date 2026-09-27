# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An MCP server's secrets, kept in the encrypted key ring.

Two kinds, one per transport:
- a local (stdio) server's environment: `env` in `mcp_servers.json`, typically an API key
  (`GITHUB_TOKEN`) the server process reads;
- a remote server's access token, sent as `Authorization: Bearer <token>`.

A remote server that signs every account in on its own (`"auth": "oauth"`,
vaf/core/mcp_oauth.py) adds two more: each account's sign-in (`mcp_server.<name>.oauth.<account>`,
a JSON record with the tokens and the client the account registered) and the secret of a client
the admin registered at the service by hand (`mcp_server.<name>.oauth_client_secret`).

Both used to sit in plaintext in `mcp_servers.json`, and the Settings list sent the manifest's
`env` to the admin's browser as it was - the same exposure the messenger bot tokens had
(vaf/core/channel_secrets.py). Now they live in the ring (`vaf.core.data_keyring`) under
`mcp_server.<name>.env` (a JSON object) and `mcp_server.<name>.token`, and this module is the
only place they are read or written:

- The manifest may still carry them, because editing `mcp_servers.json` by hand is a
  documented way to add a server and it is how an older release left it: `env` values, a
  `token`, `headers.Authorization: Bearer ...` (the shape other MCP clients write), or an
  `oauth_client_secret`.
  `move_manifest_secrets` moves them into the ring and takes them out of the file, after the
  ring read them back; the manifest keeps the NAMES of the env variables, never a value.
- The write side keeps what the browser does not re-send: an env variable sent with an empty
  value keeps its stored value, one left out is removed; an empty token keeps the stored
  token, and only an explicit `clear_token` removes it.
- `redacted_env` is what a browser may see: the names, every value empty.

Deliberate: EVERY env value is treated as a secret, not only the ones whose names look like
one. A path costs nothing in the ring, and a guess by name is how a secret stays in the file.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_lock = threading.RLock()


def _ring_name(server: str, kind: str) -> str:
    return f"mcp_server.{server}.{kind}"


def server_token(server: str) -> str:
    """The remote server's access token, or "" (none, or the ring cannot be read)."""
    from vaf.core import data_keyring
    try:
        return data_keyring.peek_data_secret(_ring_name(server, "token"))
    except Exception as exc:  # noqa: BLE001 - an unreadable ring reads as "no token"
        logger.error("The key ring cannot be read for MCP server %s: %s", server, exc)
        return ""


def server_env(server: str) -> Dict[str, str]:
    """The local server's environment from the ring ({} when none)."""
    from vaf.core import data_keyring
    try:
        raw = data_keyring.peek_data_secret(_ring_name(server, "env"))
    except Exception as exc:  # noqa: BLE001
        logger.error("The key ring cannot be read for MCP server %s: %s", server, exc)
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _put(name: str, value: str) -> None:
    """Store and read back; raise when the write did not stick (never lose the only copy)."""
    from vaf.core import data_keyring
    data_keyring.set_data_secret(name, value)
    if data_keyring.peek_data_secret(name) != value:
        raise RuntimeError(f"{name} did not read back from the key ring")


def _drop(name: str) -> None:
    from vaf.core import data_keyring
    try:
        data_keyring.delete_data_secret(name)
    except Exception as exc:  # noqa: BLE001 - a missing entry is the goal either way
        logger.warning("Could not remove %s from the key ring: %s", name, exc)


def set_server_env(server: str, env: Dict[str, str]) -> None:
    """Replace the server's stored environment ({} removes it)."""
    with _lock:
        clean = {str(k): str(v) for k, v in (env or {}).items() if str(k).strip()}
        if clean:
            _put(_ring_name(server, "env"), json.dumps(clean, sort_keys=True))
        else:
            _drop(_ring_name(server, "env"))


def set_server_token(server: str, token: str) -> None:
    value = str(token or "").strip()
    if not value:
        raise ValueError("an empty token is not a token; use clear_server_secrets or clear_token")
    with _lock:
        _put(_ring_name(server, "token"), value)


def clear_server_token(server: str) -> None:
    with _lock:
        _drop(_ring_name(server, "token"))


def clear_server_secrets(server: str) -> None:
    """Everything the ring holds for a server: a removed server leaves nothing behind."""
    with _lock:
        _drop(_ring_name(server, "token"))
        _drop(_ring_name(server, "env"))
        _drop(_ring_name(server, "oauth_client_secret"))
        for account in oauth_accounts(server):
            _drop(_oauth_name(server, account))


# -- sign-in per account (vaf/core/mcp_oauth.py runs the flow; the records live here) ----------

def _oauth_name(server: str, account: str) -> str:
    return _ring_name(server, f"oauth.{account}")


def oauth_record(server: str, account: str) -> Dict[str, Any]:
    """One account's sign-in at a server ({} when it has none, or the ring cannot be read)."""
    from vaf.core import data_keyring
    try:
        raw = data_keyring.peek_data_secret(_oauth_name(server, account))
    except Exception as exc:  # noqa: BLE001 - an unreadable ring reads as "not signed in"
        logger.error("The key ring cannot be read for MCP server %s: %s", server, exc)
        return {}
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def set_oauth_record(server: str, account: str, record: Dict[str, Any]) -> None:
    with _lock:
        _put(_oauth_name(server, account), json.dumps(record, sort_keys=True))


def clear_oauth_record(server: str, account: str) -> None:
    with _lock:
        _drop(_oauth_name(server, account))


def oauth_accounts(server: str) -> list:
    """The accounts with a sign-in record at a server (their keys, never a token)."""
    from vaf.core import data_keyring
    prefix = _oauth_name(server, "")
    try:
        return [name[len(prefix):] for name in data_keyring.data_secret_names(prefix)]
    except Exception as exc:  # noqa: BLE001
        logger.error("The key ring cannot be read for MCP server %s: %s", server, exc)
        return []


def oauth_client_secret(server: str) -> str:
    """The secret of the client the admin registered at the service by hand, or ""."""
    from vaf.core import data_keyring
    try:
        return data_keyring.peek_data_secret(_ring_name(server, "oauth_client_secret"))
    except Exception as exc:  # noqa: BLE001
        logger.error("The key ring cannot be read for MCP server %s: %s", server, exc)
        return ""


def set_oauth_client_secret(server: str, secret: str) -> None:
    value = str(secret or "").strip()
    if not value:
        raise ValueError("an empty client secret is not a secret; use clear_oauth_client_secret")
    with _lock:
        _put(_ring_name(server, "oauth_client_secret"), value)


def clear_oauth_client_secret(server: str) -> None:
    with _lock:
        _drop(_ring_name(server, "oauth_client_secret"))


def merge_env(stored: Dict[str, str], incoming: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """What a save leaves stored: a name sent with a value takes it, a name sent empty keeps
    the stored value (the browser never sees it, so it cannot re-send it), a name left out is
    removed. `incoming` None means "the env was not part of this save": unchanged."""
    if incoming is None:
        return dict(stored)
    out: Dict[str, str] = {}
    for key, value in incoming.items():
        name = str(key).strip()
        if not name:
            continue
        text = "" if value is None else str(value)
        if text:
            out[name] = text
        elif name in stored:
            out[name] = stored[name]
    return out


def _bearer(value: Any) -> str:
    text = str(value or "").strip()
    return text[7:].strip() if text.lower().startswith("bearer ") else ""


def move_manifest_secrets(manifest: Dict[str, Any]) -> bool:
    """Move secrets found in a manifest into the ring and take them out of it, in place.
    Returns True when the manifest changed (the caller saves it). A value in the file wins
    over the ring: it is the newer, hand-written one. A move that does not read back keeps
    the value in the file and is tried again on the next load."""
    servers = manifest.get("servers") if isinstance(manifest, dict) else None
    if not isinstance(servers, dict):
        return False
    changed = False
    with _lock:
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                continue
            try:
                env = cfg.get("env")
                if isinstance(env, dict) and any(str(v) for v in env.values()):
                    stored = server_env(name)
                    stored.update({str(k): str(v) for k, v in env.items() if str(v)})
                    set_server_env(name, stored)
                    cfg["env"] = {str(k): "" for k in env}
                    changed = True
                token = str(cfg.get("token") or "").strip()
                headers = cfg.get("headers") if isinstance(cfg.get("headers"), dict) else {}
                header_token = next((_bearer(v) for k, v in headers.items()
                                     if str(k).lower() == "authorization" and _bearer(v)), "")
                if token or header_token:
                    set_server_token(name, token or header_token)
                    cfg.pop("token", None)
                    if header_token:
                        cfg["headers"] = {k: v for k, v in headers.items() if str(k).lower() != "authorization"}
                        if not cfg["headers"]:
                            cfg.pop("headers")
                    changed = True
                client_secret = str(cfg.get("oauth_client_secret") or "").strip()
                if client_secret:
                    set_oauth_client_secret(name, client_secret)
                    cfg.pop("oauth_client_secret", None)
                    changed = True
            except Exception as exc:  # noqa: BLE001 - the file keeps the only copy until it moves
                logger.error("Could not move the secrets of MCP server %s into the key ring: %s", name, exc)
    return changed


def effective_env(server: str, cfg: Dict[str, Any]) -> Dict[str, str]:
    """The environment a local server is started with. The manifest's `env` names which
    variables there are (a value still written there wins: it is the newer, hand-written one,
    and moves on the next load); the ring holds the values. No `env` in the manifest: the
    ring's variables as they are."""
    stored = server_env(server)
    names = cfg.get("env") if isinstance(cfg, dict) else None
    if not isinstance(names, dict):
        return stored
    out: Dict[str, str] = {}
    for key, value in names.items():
        text = "" if value is None else str(value)
        if text:
            out[str(key)] = text
        elif str(key) in stored:
            out[str(key)] = stored[str(key)]
    return out


def redacted_env(server: str, cfg: Dict[str, Any]) -> Dict[str, str]:
    """The environment a browser may see: every name, no value."""
    names = cfg.get("env") if isinstance(cfg, dict) else None
    keys = [str(k) for k in names] if isinstance(names, dict) else list(server_env(server).keys())
    return {name: "" for name in keys}


def secrets_in_manifest(manifest: Dict[str, Any]) -> list:
    """Server names whose manifest entry still carries a secret value (for `vaf secure status`)."""
    servers = manifest.get("servers") if isinstance(manifest, dict) else None
    out = []
    for name, cfg in (servers or {}).items() if isinstance(servers, dict) else []:
        if not isinstance(cfg, dict):
            continue
        env = cfg.get("env")
        headers = cfg.get("headers") if isinstance(cfg.get("headers"), dict) else {}
        if (isinstance(env, dict) and any(str(v) for v in env.values())) or str(cfg.get("token") or "").strip() \
                or str(cfg.get("oauth_client_secret") or "").strip() \
                or any(str(k).lower() == "authorization" and _bearer(v) for k, v in headers.items()):
            out.append(str(name))
    return out
