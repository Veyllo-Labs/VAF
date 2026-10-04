# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Signing in to a remote MCP server, one account at a time.

A hosted MCP server that holds personal data (a workspace, a mailbox, a tracker) does not take
one fixed token for a whole installation: every person signs in with their own account, through
OAuth as the MCP authorization specification defines it (protected-resource metadata,
authorization-server metadata, dynamic client registration or a client the admin registered by
hand, PKCE, refresh). The official `mcp` SDK implements that flow
(`mcp.client.auth.OAuthClientProvider`); this module is what VAF adds around it:

- where the result lives: each account's tokens, and the client it registered, in the key ring
  (`mcp_secrets.oauth_record`, `mcp_server.<name>.oauth.<account>`), never in a file;
- how a person's browser gets into a flow the SDK runs inline, inside the HTTP request that met
  the 401: `start_sign_in` opens the account's session in the background and hands back the
  authorization address the SDK produced; `finish_sign_in` takes the code the service sent to
  the callback and lets the waiting flow finish (the SDK checks the `state` and exchanges the
  code with its PKCE verifier);
- which account a call signs in as: a server with `"auth": "oauth"` builds tools that declare
  `identity_kwargs = ("user_scope_id",)`, and every call runs in the caller's own session
  (`auth_for`, one pool session per account); an account that never signed in is told so
  before any request is made.

The SDK starts its whole interactive flow on ANY 401, also inside a tool call when a stored
sign-in stopped working (the refresh was refused). There the redirect handler finds no sign-in
the person started and raises `McpSignInRequired`: the call fails with "sign in again" instead
of waiting for a browser nobody opened.

The SDK loads stored tokens without their expiry (`OAuthToken` carries `expires_in`, relative to
when it was issued), so a token that ran out while VAF was not running would be sent as it is,
and the 401 that follows starts the interactive flow instead of the refresh. The storage hands
back the time that is left, and `_Provider._initialize` sets the expiry from it, so the provider
refreshes first.

Named boundaries:
- The tool list is discovered with one signed-in account (the local admin's when it has one):
  the tools are the server's, the same for every account; an account's own data only ever
  travels in its own session.
- Sign-ins of a deleted account stay in the ring until the server is removed: no store in VAF
  has a per-account deletion hook to hang this on.
- Scopes are the ones the server asks for in its metadata (the SDK's selection); an override
  is not offered.
- The raw `mcp_call` tool has no sign-in: it knows a URL, not a configured server.
- The terminal has no sign-in: VAF has no `vaf mcp` command at all. The functions take the
  redirect address from their caller, so a terminal could offer a loopback address.
- A call without a scope is the local admin's, as everywhere (`account_key`). Under the workflow
  rollback modes (`workflow_identity_injection` = legacy/off) a workflow passes none, and its MCP
  calls therefore run as the local admin, like everything else in those modes.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlsplit

from vaf.core import mcp_secrets
from vaf.core.mcp_remote import McpSignInRequired, RemoteAuth, RemoteMcpError, _describe

logger = logging.getLogger(__name__)

SIGN_IN_SECONDS = 600.0          # how long a started sign-in waits for the person's browser
URL_WAIT_SECONDS = 30.0          # metadata discovery and registration, before the address exists
FINISH_WAIT_SECONDS = 30.0       # token exchange and `initialize`, after the callback
REFRESH_MARGIN_SECONDS = 30      # a token this close to its end is refreshed, not sent
REVOKE_TIMEOUT_SECONDS = 10.0
CLIENT_NAME = "VAF"
CALLBACK_PATH = "/api/mcp/oauth/callback"


class McpSignInError(RemoteMcpError):
    """A sign-in could not be started or finished; the message says why."""


def uses_sign_in(cfg: Any) -> bool:
    """Whether a server entry signs every account in on its own (`"auth": "oauth"`)."""
    from vaf.core.mcp_registry import REMOTE_TRANSPORTS
    return (isinstance(cfg, dict) and str(cfg.get("auth") or "").strip().lower() == "oauth"
            and str(cfg.get("transport", "stdio")) in REMOTE_TRANSPORTS)


_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def account_key(user_scope_id: Optional[str]) -> str:
    """The key an account's sign-in is stored under: its scope. No scope is the local admin's,
    the rule of `config.is_local_admin_lane` (the tokenless desktop and the Discord bridge)."""
    from vaf.core.config import get_local_admin_scope_id
    scope = str(user_scope_id or "").strip() or str(get_local_admin_scope_id() or "").strip() or "local"
    return scope if _ACCOUNT_RE.match(scope) else hashlib.sha256(scope.encode("utf-8")).hexdigest()[:32]


def _pool_account(server: str, account: str) -> str:
    return f"oauth:{server}:{account}"


def _sign_in_pool_account(server: str, account: str) -> str:
    """A started sign-in's own session: apart from the account's working session, so a call
    the account makes meanwhile (with its old sign-in) does not wait behind a browser."""
    return f"oauth-sign-in:{server}:{account}"


def _sign_in_hint(server: str) -> str:
    return (f"this account is not signed in to the MCP server '{server}': "
            "sign in under Settings > Connections > MCP services")


def _server_cfg(server: str) -> Optional[Dict[str, Any]]:
    from vaf.core.mcp_registry import load_mcp_manifest
    cfg = ((load_mcp_manifest() or {}).get("servers") or {}).get(str(server))
    return cfg if isinstance(cfg, dict) else None


def _has_tokens(record: Dict[str, Any]) -> bool:
    tokens = record.get("tokens") if isinstance(record, dict) else None
    return isinstance(tokens, dict) and bool(tokens.get("access_token") or tokens.get("refresh_token"))


def signed_in(server: str, user_scope_id: Optional[str]) -> bool:
    return _has_tokens(mcp_secrets.oauth_record(server, account_key(user_scope_id)))


# -- the SDK's storage and provider ----------------------------------------------------------

class _RingStorage:
    """The SDK's `TokenStorage` over the key ring: one account at one server.

    `fresh` is a sign-in the person started: it must not see the stored tokens (with a valid
    one the server would never answer 401, and the flow would never start), and it must not
    touch the stored sign-in until it has one to replace it with. So the client it registers is
    held in memory and written together with the new tokens, in one record that replaces the
    old one; a sign-in that is abandoned leaves the old sign-in exactly as it was."""

    def __init__(self, server: str, account: str, cfg: Dict[str, Any], redirect_uri: str,
                 username: str = "", fresh: bool = False) -> None:
        self.server = server
        self.account = account
        self.cfg = cfg
        self.redirect_uri = redirect_uri
        self.username = username
        self.fresh = fresh
        self._client: Any = None      # a fresh sign-in's registered client, until it has tokens
        self._wrote = False
        self.provider: Any = None     # set once built: a token save records its revocation endpoint

    def _read(self) -> Dict[str, Any]:
        return mcp_secrets.oauth_record(self.server, self.account)

    def _write(self, record: Dict[str, Any]) -> None:
        mcp_secrets.set_oauth_record(self.server, self.account, record)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        if self.fresh and not self._wrote:
            return None
        record = await asyncio.to_thread(self._read)
        if not _has_tokens(record):
            return None
        data = {k: v for k, v in dict(record["tokens"]).items() if k != "expires_in"}
        data.setdefault("access_token", "")
        expires_at = record.get("expires_at")
        if expires_at:
            left = int(float(expires_at) - time.time()) - REFRESH_MARGIN_SECONDS
            data["expires_in"] = left if left > 0 else -1
        try:
            return OAuthToken.model_validate(data)
        except Exception:  # noqa: BLE001 - a record the SDK cannot read is no sign-in
            return None

    async def set_tokens(self, tokens) -> None:
        if self.fresh and not self._wrote:
            # The new sign-in replaces the old record whole: its client and nothing else of it.
            record: Dict[str, Any] = {}
            client = self._client or await self._stored_client()
            if client is not None:
                record["client"] = client.model_dump(mode="json", exclude_none=True)
        else:
            record = await asyncio.to_thread(self._read)
        data = tokens.model_dump(mode="json", exclude_none=True)
        expires_in = data.pop("expires_in", None)
        record["tokens"] = data
        record["expires_at"] = (time.time() + int(expires_in)) if expires_in is not None else None
        record["redirect_uri"] = self.redirect_uri
        metadata = getattr(getattr(self.provider, "context", None), "oauth_metadata", None)
        if metadata is not None and getattr(metadata, "revocation_endpoint", None):
            record["revocation_endpoint"] = str(metadata.revocation_endpoint)
        record.setdefault("signed_in_at", time.time())
        if self.username:
            record["username"] = self.username
        await asyncio.to_thread(self._write, record)
        self._wrote = True

    async def _stored_client(self):
        """The client this account registered earlier. A fresh sign-in takes it only while its
        registered redirect address is the one in use: an address the service does not know for
        that client would be refused, so a client for another address is registered anew."""
        from mcp.shared.auth import OAuthClientInformationFull
        record = await asyncio.to_thread(self._read)
        client = record.get("client")
        if not isinstance(client, dict):
            return None
        if self.fresh and self.redirect_uri not in [str(u) for u in (client.get("redirect_uris") or [])]:
            return None
        try:
            return OAuthClientInformationFull.model_validate(client)
        except Exception:  # noqa: BLE001 - registered again
            return None

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        client_id = str(self.cfg.get("oauth_client_id") or "").strip()
        if client_id:
            secret = await asyncio.to_thread(mcp_secrets.oauth_client_secret, self.server)
            return OAuthClientInformationFull(
                client_id=client_id, client_secret=secret or None, client_name=CLIENT_NAME,
                redirect_uris=[self.redirect_uri],
                token_endpoint_auth_method="client_secret_post" if secret else "none")
        if self._client is not None:
            return self._client
        return await self._stored_client()

    async def set_client_info(self, client_info) -> None:
        if self.fresh and not self._wrote:
            self._client = client_info
            return
        record = await asyncio.to_thread(self._read)
        record["client"] = client_info.model_dump(mode="json", exclude_none=True)
        await asyncio.to_thread(self._write, record)


_provider_cls = None


def _provider_class():
    global _provider_cls
    if _provider_cls is None:
        from mcp.client.auth import OAuthClientProvider

        class _Provider(OAuthClientProvider):
            async def _initialize(self) -> None:
                await super()._initialize()
                # The SDK does not restore the expiry of stored tokens (see the module docstring).
                tokens = self.context.current_tokens
                if tokens is not None and tokens.expires_in is not None:
                    self.context.update_token_expiry(tokens)

        _provider_cls = _Provider
    return _provider_cls


def _build(server: str, cfg: Dict[str, Any], account: str, redirect_uri: str, username: str = "",
           fresh: bool = False):
    """The `httpx.Auth` of one account's session at one server (`fresh`: a sign-in the person
    started, see `_RingStorage`)."""
    from mcp.shared.auth import OAuthClientMetadata

    storage = _RingStorage(server, account, cfg, redirect_uri, username, fresh=fresh)
    metadata = OAuthClientMetadata(redirect_uris=[redirect_uri], client_name=CLIENT_NAME,
                                   grant_types=["authorization_code", "refresh_token"],
                                   response_types=["code"])

    async def redirect_handler(url: str) -> None:
        state = (parse_qs(urlsplit(url).query).get("state") or [""])[0]
        with _lock:
            pending = _pending.get((server, account))
            if pending is not None and not pending.url_future.done():
                pending.state = state
                if state:
                    _by_state[state] = pending
            else:
                pending = None
        if pending is None:
            # Nobody is at a browser: a stored sign-in stopped working inside a call.
            raise McpSignInRequired(_sign_in_hint(server))
        pending.url_future.set_result(url)

    async def callback_handler() -> Tuple[str, Optional[str]]:
        with _lock:
            pending = _pending.get((server, account))
        if pending is None:
            raise McpSignInRequired(_sign_in_hint(server))
        left = max(1.0, SIGN_IN_SECONDS - (time.monotonic() - pending.started))
        try:
            return await asyncio.wait_for(asyncio.wrap_future(pending.code_future), timeout=left)
        except asyncio.TimeoutError:
            raise McpSignInError(f"the sign-in was not finished within {int(SIGN_IN_SECONDS // 60)} minutes")

    provider = _provider_class()(
        server_url=str(cfg.get("url") or ""), client_metadata=metadata, storage=storage,
        redirect_handler=redirect_handler, callback_handler=callback_handler, timeout=SIGN_IN_SECONDS)
    storage.provider = provider
    return provider


# -- which account a call signs in as ---------------------------------------------------------

def _auth(server: str, cfg: Dict[str, Any], account: str) -> RemoteAuth:
    record = mcp_secrets.oauth_record(server, account)
    if not _has_tokens(record):
        raise McpSignInRequired(_sign_in_hint(server))
    redirect_uri = str(record.get("redirect_uri") or "") or f"http://localhost{CALLBACK_PATH}"
    return RemoteAuth(_pool_account(server, account), lambda: _build(server, cfg, account, redirect_uri))


def auth_for(server: str, cfg: Dict[str, Any], user_scope_id: Optional[str]) -> RemoteAuth:
    """The caller's own session at the server. Raises McpSignInRequired when this account
    has not signed in: before any request, so the server never sees an anonymous call."""
    return _auth(server, cfg, account_key(user_scope_id))


def discovery_auth(server: str, cfg: Dict[str, Any]) -> RemoteAuth:
    """A signed-in account to ask the server for its tool list with: the local admin's when
    it has signed in, else the first one that has (see the module docstring)."""
    accounts = [a for a in mcp_secrets.oauth_accounts(server)
                if _has_tokens(mcp_secrets.oauth_record(server, a))]
    if not accounts:
        raise McpSignInRequired(f"no account has signed in to the MCP server '{server}' yet: "
                                "sign in under Settings > Connections > MCP services")
    admin = account_key(None)
    return _auth(server, cfg, admin if admin in accounts else accounts[0])


# -- signing in through a browser -------------------------------------------------------------

@dataclass(eq=False)
class _Pending:
    server: str
    account: str
    scope: str
    username: str
    url_future: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)
    code_future: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)
    connect: Optional[concurrent.futures.Future] = None
    state: str = ""
    started: float = field(default_factory=time.monotonic)


_lock = threading.Lock()
_pending: Dict[Tuple[str, str], _Pending] = {}
_by_state: Dict[str, _Pending] = {}
_last_error: Dict[Tuple[str, str], str] = {}


def _fail(future: concurrent.futures.Future, reason: str) -> None:
    if not future.done():
        try:
            future.set_exception(McpSignInError(reason))
        except concurrent.futures.InvalidStateError:
            pass


def _abort(pending: _Pending, reason: str) -> None:
    """End a started sign-in that will not be finished (a new one, a sign-out, a removal)."""
    with _lock:
        if _pending.get((pending.server, pending.account)) is pending:
            _pending.pop((pending.server, pending.account), None)
        if pending.state:
            _by_state.pop(pending.state, None)
    _fail(pending.url_future, reason)
    _fail(pending.code_future, reason)


def _settle(pending: _Pending, future: concurrent.futures.Future) -> None:
    """The sign-in's session is open or failed. Runs on the pool's loop: no pool call here."""
    key = (pending.server, pending.account)
    if future.cancelled():
        error: Optional[str] = "the sign-in was cancelled"
    elif future.exception() is not None:
        error = _describe(future.exception())
    elif not pending.code_future.done():
        # Open, and nobody was asked to sign in: the server takes calls without a sign-in.
        error = "the server answered without asking for a sign-in: it needs none"
    else:
        error = None
    with _lock:
        current = _pending.get(key) is pending
        if current:
            _pending.pop(key, None)
            if error:
                _last_error[key] = error
            else:
                _last_error.pop(key, None)
        if pending.state:
            _by_state.pop(pending.state, None)
    _fail(pending.url_future, error or "the sign-in ended")


def start_sign_in(server: str, *, user_scope_id: Optional[str], redirect_uri: str,
                  username: str = "") -> Dict[str, Any]:
    """Start signing this account in to the server. Returns `{"authorization_url": ...}`, the
    address the person opens in their browser; the service then sends the browser to
    `redirect_uri` (this installation's `CALLBACK_PATH`), whose handler calls `finish_sign_in`.

    The account's old sign-in stays as it is until the new one has tokens, which then replace it
    whole (`_RingStorage`): a sign-in that is started and abandoned, by the person or by a page
    that sent the request in their name, changes nothing. A client the account registered
    earlier is kept while its registered redirect address still matches. Asked again while a
    sign-in is waiting, it hands back the same address. Raises ValueError for a server without a sign-in per account, and
    McpSignInError when the server did not start one (unreachable, no metadata, the
    registration refused)."""
    from vaf.core.mcp_registry import server_headers
    from vaf.core.mcp_remote import get_remote_pool

    cfg = _server_cfg(server)
    if cfg is None or not uses_sign_in(cfg):
        raise ValueError(f"'{server}' is not an MCP server with a sign-in per account")
    if not cfg.get("enabled", True):
        raise ValueError(f"the MCP server '{server}' is turned off")
    account = account_key(user_scope_id)
    key = (server, account)
    with _lock:
        old = _pending.get(key)
        _last_error.pop(key, None)
    if old is not None:
        if (old.url_future.done() and old.url_future.exception() is None and old.connect is not None
                and not old.connect.done()):
            return {"authorization_url": old.url_future.result()}
        _abort(old, "a new sign-in was started")

    pool = get_remote_pool()
    pool.close(account=_sign_in_pool_account(server, account))
    pending = _Pending(server=server, account=account, scope=str(user_scope_id or ""),
                       username=str(username or ""))
    with _lock:
        _pending[key] = pending
    auth = RemoteAuth(_sign_in_pool_account(server, account),
                      lambda: _build(server, cfg, account, redirect_uri, pending.username, fresh=True))
    from vaf.core.mcp_remote import registered_egress
    pending.connect = pool.open(str(cfg.get("transport")), str(cfg.get("url") or ""), server_headers(server, cfg),
                                auth=auth, connect_timeout=SIGN_IN_SECONDS + FINISH_WAIT_SECONDS,
                                egress=registered_egress(str(cfg.get("url") or "")))
    pending.connect.add_done_callback(lambda fut: _settle(pending, fut))

    concurrent.futures.wait([pending.url_future, pending.connect], timeout=URL_WAIT_SECONDS,
                            return_when=concurrent.futures.FIRST_COMPLETED)
    if pending.url_future.done() and pending.url_future.exception() is None:
        return {"authorization_url": pending.url_future.result()}
    if pending.connect.done():
        exc = pending.connect.exception()
        pool.close(account=_sign_in_pool_account(server, account))
        if exc is None:
            raise McpSignInError("the server answered without asking for a sign-in: it needs none")
        raise McpSignInError(_describe(exc))
    _abort(pending, "the server did not start a sign-in in time")
    raise McpSignInError(f"the server did not start a sign-in within {int(URL_WAIT_SECONDS)} s")


def pending_owner(state: str) -> Optional[Dict[str, str]]:
    """Who started the sign-in this `state` belongs to (the callback checks it is the same
    person before it finishes anything), or None for an unknown or used one."""
    with _lock:
        pending = _by_state.get(str(state or ""))
    if pending is None:
        return None
    return {"server": pending.server, "user_scope_id": pending.scope, "username": pending.username}


_SAFE_ERROR = re.compile(r"[^A-Za-z0-9_ .:-]")


def finish_sign_in(state: str, code: str = "", error: str = "") -> Dict[str, Any]:
    """Hand the callback's answer to the waiting flow and wait for it to finish. Returns
    `{"ok", "server", "error"}`; never raises. A `state` is used once."""
    with _lock:
        pending = _by_state.pop(str(state or ""), None)
    if pending is None:
        return {"ok": False, "server": "", "error": "this sign-in is unknown or has run out: start it again"}
    if error:
        _fail(pending.code_future, f"the service refused the sign-in ({_SAFE_ERROR.sub('', str(error))[:80]})")
    elif not code:
        _fail(pending.code_future, "the service sent no authorization code")
    elif not pending.code_future.done():
        pending.code_future.set_result((str(code), str(state)))
    try:
        if pending.connect is not None:
            pending.connect.result(timeout=FINISH_WAIT_SECONDS)
    except concurrent.futures.TimeoutError:
        return {"ok": False, "server": pending.server,
                "error": f"the service did not finish the sign-in within {int(FINISH_WAIT_SECONDS)} s"}
    except BaseException as exc:  # noqa: BLE001 - the reason goes to the person
        return {"ok": False, "server": pending.server, "error": _describe(exc)}
    finally:
        # The sign-in's session has done its job; a session still holding the OLD tokens in
        # memory goes too, so the next call opens one with the new sign-in.
        from vaf.core.mcp_remote import get_remote_pool
        get_remote_pool().close(account=_sign_in_pool_account(pending.server, pending.account))
    get_remote_pool().close(account=_pool_account(pending.server, pending.account))
    ok = _has_tokens(mcp_secrets.oauth_record(pending.server, pending.account))
    return {"ok": ok, "server": pending.server,
            "error": None if ok else "the sign-in finished without tokens"}


def sign_in_status(user_scope_id: Optional[str]) -> List[Dict[str, Any]]:
    """The servers with a sign-in per account, as this account sees them: signed in or not,
    a sign-in waiting in a browser, the reason the last one failed. Never a token."""
    from vaf.core.mcp_registry import load_mcp_manifest
    account = account_key(user_scope_id)
    out = []
    for name, cfg in ((load_mcp_manifest() or {}).get("servers") or {}).items():
        if not uses_sign_in(cfg):
            continue
        record = mcp_secrets.oauth_record(name, account)
        with _lock:
            waiting = (name, account) in _pending
            error = _last_error.get((name, account))
        out.append({
            "name": name,
            "url": str(cfg.get("url") or ""),
            "enabled": bool(cfg.get("enabled", True)),
            "signed_in": _has_tokens(record),
            "signed_in_at": record.get("signed_in_at") if _has_tokens(record) else None,
            "pending": waiting,
            "error": error,
        })
    return out


# -- signing out ------------------------------------------------------------------------------

def _revoke(server: str, cfg: Dict[str, Any], record: Dict[str, Any],
            preregistered_secret: Optional[str] = None) -> Optional[bool]:
    """Tell the service to invalidate the sign-in (RFC 7009), where it offers that. True when it
    confirmed, False when it did not, None when there is nothing to ask. Best effort: the local
    record is already gone either way. `preregistered_secret` is the admin's client secret read
    before a removal took it out of the ring. The endpoint comes from the service's own
    metadata, so it is reached through the destination guard like any URL VAF did not choose."""
    from vaf.core.mcp_remote import registered_egress
    from vaf.network.egress import egress_session
    endpoint = str(record.get("revocation_endpoint") or "")
    tokens = record.get("tokens") if isinstance(record.get("tokens"), dict) else {}
    token = tokens.get("refresh_token") or tokens.get("access_token")
    if not endpoint or not token:
        return None
    client = record.get("client") if isinstance(record.get("client"), dict) else {}
    pre_registered = str(cfg.get("oauth_client_id") or "").strip()
    client_id = pre_registered or str(client.get("client_id") or "")
    if pre_registered:
        secret = preregistered_secret if preregistered_secret is not None else mcp_secrets.oauth_client_secret(server)
    else:
        secret = str(client.get("client_secret") or "")
    method = str(client.get("token_endpoint_auth_method") or ("client_secret_post" if secret else "none"))
    data = {"token": token, "token_type_hint": "refresh_token" if tokens.get("refresh_token") else "access_token"}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if method == "client_secret_basic" and secret:
        pair = f"{quote(client_id, safe='')}:{quote(secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")
    else:
        data["client_id"] = client_id
        if secret:
            data["client_secret"] = secret
    try:
        with egress_session(registered_egress(str(cfg.get("url") or ""))) as http:
            response = http.post(endpoint, data=data, headers=headers, timeout=REVOKE_TIMEOUT_SECONDS)
        return response.status_code == 200
    except Exception as exc:  # noqa: BLE001
        logger.info("MCP server %s: revoking a sign-in failed: %s", server, exc)
        return False


def sign_out(server: str, *, user_scope_id: Optional[str]) -> Dict[str, Any]:
    """Sign this account out: its session is closed and its record deleted first, then the
    service is asked to invalidate the tokens. Returns `{"revoked": True|False|None}`."""
    from vaf.core.mcp_remote import get_remote_pool
    account = account_key(user_scope_id)
    with _lock:
        pending = _pending.get((server, account))
        _last_error.pop((server, account), None)
    if pending is not None:
        _abort(pending, "signed out")
    record = mcp_secrets.oauth_record(server, account)
    mcp_secrets.clear_oauth_record(server, account)
    get_remote_pool().close(account=_pool_account(server, account))
    get_remote_pool().close(account=_sign_in_pool_account(server, account))
    return {"revoked": _revoke(server, _server_cfg(server) or {}, record) if _has_tokens(record) else None}


def forget_server(server: str, cfg: Optional[Dict[str, Any]] = None) -> int:
    """Drop every account's sign-in at a server (it moved to another host, stopped signing
    accounts in, changed its client, or is being removed). The records go at once; asking the
    service to invalidate them runs in the background. Returns how many there were."""
    from vaf.core.mcp_remote import get_remote_pool
    cfg = dict(cfg if cfg is not None else (_server_cfg(server) or {}))
    with _lock:
        waiting = [p for (name, _account), p in _pending.items() if name == server]
        for (name, account) in [k for k in _last_error if k[0] == server]:
            _last_error.pop((name, account), None)
    for pending in waiting:
        _abort(pending, "the server's sign-in changed")
        get_remote_pool().close(account=_sign_in_pool_account(server, pending.account))
    records = []
    for account in mcp_secrets.oauth_accounts(server):
        record = mcp_secrets.oauth_record(server, account)
        mcp_secrets.clear_oauth_record(server, account)
        get_remote_pool().close(account=_pool_account(server, account))
        if _has_tokens(record):
            records.append(record)
    if records:
        secret = mcp_secrets.oauth_client_secret(server) if str(cfg.get("oauth_client_id") or "").strip() else ""

        def _revoke_all() -> None:
            for record in records:
                _revoke(server, cfg, record, secret)
        threading.Thread(target=_revoke_all, name="vaf-mcp-revoke", daemon=True).start()
    return len(records)
