# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Email connection API: OAuth2 PKCE start/callback and account CRUD.

Credentials are stored in credential_store (keyring or encrypted file);
config holds only account metadata.
"""
import asyncio
import html
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from datetime import datetime, timezone

import requests

from vaf.core.config import Config
from vaf.core.email_accounts import (
    IMAP_SMTP_DEFAULTS,
    get_email_config,
    save_email_config,
    test_imap_login,
)
from vaf.core.credential_store import get_email_credentials, set_email_imap_password
from vaf.core.email_transport import (
    _mask_account,
)
from vaf.core.oauth_pkce import (
    exchange_code_for_tokens,
    get_authorization_url,
    get_state_provider,
    get_state_user,
    get_valid_access_token,
    is_oauth_provider_configured,
)
from vaf.api.oauth_session_binding import (
    enforce_callback_actor_binding,
    require_oauth_actor_in_network_mode,
)
from vaf.network.binding import assert_safe_remote_host

logger = logging.getLogger("vaf.api.email")


def _guard_mail_host(host: str) -> None:
    """SSRF guard for a user-supplied IMAP/SMTP host. Refuses non-public addresses unless an
    admin opted in via email_allow_private_hosts (LAN / self-hosted server)."""
    assert_safe_remote_host(host, allow_private=bool(Config.get("email_allow_private_hosts", False)))

router = APIRouter(prefix="/api/email", tags=["email"])


def _get_current_username(request: Request) -> str:
    """Current user from auth middleware, or local admin. Used to scope email data per user."""
    from vaf.api.config_routes import get_current_username as get_username
    return get_username(request)


def _get_current_user(request: Request) -> Dict[str, Any]:
    """Current user with username, role, and user_scope_id (for UUID-based scoping)."""
    from vaf.api.config_routes import get_current_user_or_local_admin
    return get_current_user_or_local_admin(request)


from vaf.core.config import get_local_admin_scope_id, get_local_admin_username


def _store_and_cred_from_user(user: Dict[str, Any]) -> tuple[str, Optional[str]]:
    """Return (store_username, cred_username) for store/credential scope. Uses user_scope_id for local-admin check when available (Phase 6)."""
    username = (user.get("username") or "admin").strip()
    scope = user.get("user_scope_id")
    local_scope = get_local_admin_scope_id()
    if scope and str(scope).strip() == local_scope:
        return "", None
    if not username:
        return "", None
    local_admin = get_local_admin_username().lower()
    if username.lower() == local_admin:
        return "", None
    return username, username


# IMAP/SMTP presets now live in the route-independent SSOT
# vaf/core/email_accounts.py (P4.1); imported above and re-exported here.


class AddImapAccountRequest(BaseModel):
    email: str
    password: str
    imap_host: Optional[str] = None
    imap_port: Optional[int] = Field(default=None, ge=1, le=65535)
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = Field(default=None, ge=1, le=65535)


class TestImapRequest(BaseModel):
    """Body for connection test only; nothing is saved."""
    email: str
    password: str
    imap_host: Optional[str] = None
    imap_port: Optional[int] = Field(default=None, ge=1, le=65535)


# Account-config get/save now live in the route-independent SSOT
# vaf/core/email_accounts.py (Phase 1 of the mail v2-only port: no FastAPI route
# module on the config import path). Re-exported here under the historical private
# names so existing importers keep working; a guard test pins them to one object.
_get_email_config = get_email_config
_save_email_config = save_email_config


# The IMAP connection probe now lives in vaf/core/email_accounts.py (P4.1);
# re-exported here under the historical private name.
_test_imap_login = test_imap_login


def _add_account(
    account_id: str,
    provider: str,
    email: str,
    enabled: bool = True,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
    imap_ready: Optional[bool] = None,
) -> None:
    ec = _get_email_config(username, user_scope_id=user_scope_id)
    accounts: List[Dict[str, Any]] = list(ec.get("accounts") or [])
    for a in accounts:
        if (a.get("account_id") or a.get("email")) == account_id or a.get("email") == email:
            a["provider"] = provider
            a["enabled"] = enabled
            if imap_ready is not None:
                a["imap_ready"] = bool(imap_ready)
            _save_email_config(ec, username, user_scope_id=user_scope_id)
            return
    accounts.append({
        "account_id": account_id,
        "provider": provider,
        "email": email or account_id,
        "enabled": enabled,
        "label": "",
        **({"imap_ready": bool(imap_ready)} if imap_ready is not None else {}),
    })
    ec["accounts"] = accounts
    _save_email_config(ec, username, user_scope_id=user_scope_id)


def _effective_https_suffix() -> str:
    """The ':<port>' suffix for the reachable HTTPS URL (shared helper; see vaf/network/oauth_redirect)."""
    from vaf.network.oauth_redirect import effective_https_suffix
    return effective_https_suffix()


def _oauth_callback_base_url() -> str:
    """
    Base URL for OAuth redirect_uri. Must point to this backend so the callback is handled here.
    Shared with the cloud flow via vaf/network/oauth_redirect so both stay reachable/consistent.
    """
    from vaf.network.oauth_redirect import oauth_callback_base_url
    return oauth_callback_base_url("email_oauth_callback_base_url")


def _frontend_base_url() -> str:
    """Return Web UI base URL for post-OAuth redirects."""
    network_on = bool(Config.get("local_network_enabled", False))
    tls_on = bool(Config.get("local_network_tls_enabled", False))
    if network_on and tls_on:
        return f"https://localhost{_effective_https_suffix()}"
    port = __import__("os").environ.get("VAF_WEB_UI_PORT", "3000")
    return f"http://localhost:{port}"


@router.get("/oauth/start")
async def oauth_start(request: Request, provider: str = "gmail", _user: Dict[str, Any] = Depends(_get_current_user)):
    """
    Start OAuth2 PKCE flow. Returns authorization_url and state.
    Frontend opens authorization_url in browser; callback will run on this server.
    """
    if provider not in ("gmail", "microsoft"):
        # Apple has no OAuth mail API; iCloud Mail connects via the IMAP lane
        # with an app-specific password.
        raise HTTPException(status_code=400, detail="provider must be gmail or microsoft")
    # v2 re-consent lane (EMAIL_CLIENT.md, E1): ?imap=true requests IMAP-capable
    # scopes. Microsoft mail tokens live on a separate resource -> own provider
    # record; Google uses one union token (calendar survives).
    imap_lane = str(request.query_params.get("imap") or "").lower() in ("1", "true", "yes")
    if imap_lane and provider == "microsoft":
        provider = "microsoft_imap"
    require_oauth_actor_in_network_mode(request)
    base_url = _oauth_callback_base_url()
    redirect_uri = f"{base_url}/api/email/oauth/callback"
    _username = _user.get("username")
    _user_scope_id = _user.get("user_scope_id")
    # ?account=<email> preselects the mailbox on the consent screen, so upgrading
    # one of several accounts cannot silently re-consent whichever one the browser
    # happens to be signed in as.
    login_hint = (request.query_params.get("account") or "").strip() or None
    try:
        auth_url, state = get_authorization_url(provider, redirect_uri, username=_username, user_scope_id=_user_scope_id, imap=imap_lane, login_hint=login_hint)
        return {"authorization_url": auth_url, "state": state, "redirect_uri": redirect_uri}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
):
    """
    OAuth callback. Exchanges code for tokens, stores in credential_store,
    adds account to email_config, redirects to success or error page.
    """
    if error:
        return _redirect_error(f"Provider returned error: {error}")
    if not code or not state:
        return _redirect_error("Missing code or state")
    base_url = _oauth_callback_base_url()
    redirect_uri = f"{base_url}/api/email/oauth/callback"
    try:
        provider = get_state_provider(state)
        if not provider:
            return _redirect_error("Invalid or expired state. Please start the login again.")
        state_username, state_scope = get_state_user(state)
        enforce_callback_actor_binding(request, state_username, state_scope)
        # to_thread: token exchange + provider userinfo are blocking requests.post/get.
        data = await asyncio.to_thread(exchange_code_for_tokens, provider, code, state, redirect_uri)
        account_id = data.get("account_id") or "unknown"
        # Use retrieved scope/user from OAuth state to add the account
        _username = data.get("username")
        _user_scope_id = data.get("user_scope_id")
        # v2 re-consent bookkeeping (E1): the config account keeps its display
        # provider; microsoft_imap tokens live under their own credential record.
        # imap_ready marks the account as usable by the v2 IMAP engine.
        _cfg_provider = "microsoft" if provider == "microsoft_imap" else provider
        _got_imap = provider == "microsoft_imap" or (
            provider == "gmail" and "mail.google.com" in (data.get("scope") or ""))
        _add_account(account_id, _cfg_provider, account_id if "@" in account_id else account_id,
                     enabled=True, username=_username, user_scope_id=_user_scope_id,
                     imap_ready=True if _got_imap else None)
        # account_id is the user's email (PII) → mask it in logs.
        masked_account = _mask_account(account_id) if account_id else "unknown"
        logger.info("email oauth callback: account added account_id=%s provider=%s", masked_account, provider)
        try:
            from vaf.core.log_helper import append_domain_log
            append_domain_log("backend", f"[EMAIL_OAUTH] account added account_id={masked_account} provider={provider}")
        except Exception:
            pass
        return _redirect_success(account_id, provider)
    except ValueError as e:
        logger.warning("OAuth callback error: %s", e)
        return _redirect_error(str(e))


def _redirect_success(account_id: str, provider: str) -> RedirectResponse:
    # Redirect to frontend; use hash so server doesn't see token
    frontend = _frontend_base_url()
    url = f"{frontend}/settings?connections=1&email_oauth=success&account={account_id}&provider={provider}"
    return RedirectResponse(url=url, status_code=302)


def _redirect_error(message: str) -> HTMLResponse:
    # message can include provider-controlled text (the OAuth `error` query param) → escape it
    # so a crafted error value cannot inject HTML/script into this page.
    safe_message = html.escape(message or "Email connection failed")
    url = f"{_frontend_base_url()}/settings?connections=1&email_oauth=error"
    html_content = f"""
    <!DOCTYPE html>
    <html><head><meta charset="utf-8"><title>Email connection failed</title></head>
    <body style="font-family:sans-serif;max-width:480px;margin:2rem auto;padding:1rem;">
    <h2>Email connection failed</h2>
    <p>{safe_message}</p>
    <p><a href="{html.escape(url)}">Back to Settings</a></p>
    </body></html>
    """
    return HTMLResponse(content=html_content, status_code=200)


@router.get("/oauth-status")
async def oauth_status():
    """
    Return which OAuth providers are configured (no secrets).
    Used by the email wizard to show Gmail/Microsoft only when an admin has set client ID and secret.
    """
    return {
        "oauth_google_configured": is_oauth_provider_configured("gmail"),
        "oauth_microsoft_configured": is_oauth_provider_configured("microsoft"),
    }


@router.get("/accounts")
async def list_accounts(_user: Dict[str, Any] = Depends(_get_current_user)):
    """Return list of configured email accounts for the current user (metadata only, no credentials)."""
    _username = _user.get("username", "admin")
    _user_scope_id = _user.get("user_scope_id")
    ec = _get_email_config(_username, user_scope_id=_user_scope_id)
    accounts = ec.get("accounts") or []
    return {"accounts": accounts}


@router.post("/accounts/test")
async def test_imap_connection(request: Request, body: TestImapRequest, _user: Dict[str, Any] = Depends(_get_current_user)):
    """
    Test IMAP login with the given credentials. Nothing is saved.
    Use before adding an account to verify email/password (and 2FA app password for Gmail).
    Returns { ok, error?, hint? }. hint suggests App Password for Gmail when login fails.
    """
    email = (body.email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")
    password = (body.password or "").strip()
    if not password:
        raise HTTPException(status_code=400, detail="Password or app password required")
    ok, err, hint = await asyncio.to_thread(_test_imap_login,
        email,
        password,
        body.imap_host,
        body.imap_port,
    )
    if ok:
        return {"ok": True}
    # Feed the shared rate limiter so repeated failed credential tests get blocked per IP
    # (the test route returns 200 even on failure, so the middleware's 401 path won't catch it).
    try:
        from vaf.auth.rate_limit import client_key, record_login_failure
        record_login_failure(client_key(request))
    except Exception:
        pass
    return {"ok": False, "error": err, "hint": hint}


@router.post("/accounts")
async def add_account(request: Request, body: AddImapAccountRequest, _user: Dict[str, Any] = Depends(_get_current_user)):
    """
    Add an IMAP/SMTP account (other provider). Password is stored in keyring/encrypted file only.
    Server host/port can be omitted; defaults are used for known domains (Gmail, Outlook, Yahoo, etc.).
    Scoped to current user in multi-user (network) mode.
    """
    _username = _user.get("username", "admin")
    _user_scope_id = _user.get("user_scope_id")
    email = (body.email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")
    password = (body.password or "").strip()
    if not password:
        raise HTTPException(status_code=400, detail="Password or app password required")
    domain = email.split("@")[-1] if "@" in email else ""
    defaults = IMAP_SMTP_DEFAULTS.get(domain, {})
    imap_host = (body.imap_host or "").strip() or defaults.get("imap_host", "imap.gmail.com")
    imap_port = body.imap_port if body.imap_port is not None else defaults.get("imap_port", 993)
    smtp_host = (body.smtp_host or "").strip() or defaults.get("smtp_host", "smtp.gmail.com")
    smtp_port = body.smtp_port if body.smtp_port is not None else defaults.get("smtp_port", 587)
    _, cred_username = _store_and_cred_from_user(_user)
    set_email_imap_password(email, password, cred_username, user_scope_id=_user_scope_id)
    ec = _get_email_config(_username, user_scope_id=_user_scope_id)
    accounts = list(ec.get("accounts") or [])
    now_iso = datetime.now(timezone.utc).isoformat()
    for a in accounts:
        if (a.get("email") or "").lower() == email:
            a["provider"] = "imap"
            a["enabled"] = True
            a["imap_host"] = imap_host
            a["imap_port"] = imap_port
            a["smtp_host"] = smtp_host
            a["smtp_port"] = smtp_port
            ok, _, _ = await asyncio.to_thread(_test_imap_login, email, password, imap_host, imap_port)
            a["last_verified_at"] = now_iso if ok else None
            _save_email_config(ec, _username, user_scope_id=_user_scope_id)
            return {"account_id": email, "email": email, "provider": "imap", "last_verified_at": a.get("last_verified_at")}
    accounts.append({
        "account_id": email,
        "provider": "imap",
        "email": email,
        "enabled": True,
        "imap_host": imap_host,
        "imap_port": imap_port,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "last_verified_at": None,
        "label": "",
    })
    ec["accounts"] = accounts
    _save_email_config(ec, _username, user_scope_id=_user_scope_id)
    ok, _, _ = await asyncio.to_thread(_test_imap_login, email, password, imap_host, imap_port)
    if ok:
        for a in ec.get("accounts") or []:
            if (a.get("email") or "").lower() == email:
                a["last_verified_at"] = now_iso
                break
        _save_email_config(ec, _username, user_scope_id=_user_scope_id)
    return {"account_id": email, "email": email, "provider": "imap", "last_verified_at": now_iso if ok else None}


def _verify_oauth_gmail(account_id: str, username: Optional[str] = None, user_scope_id: Optional[str] = None) -> bool:
    """Verify Gmail OAuth by calling users.getProfile. Returns True if token is valid."""
    token = get_valid_access_token(account_id, "gmail", username, user_scope_id=user_scope_id)
    if not token:
        return False
    try:
        r = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False


def _verify_oauth_microsoft(account_id: str, username: Optional[str] = None, user_scope_id: Optional[str] = None) -> bool:
    """Verify Microsoft OAuth by calling GET /me. Returns True if token is valid."""
    token = get_valid_access_token(account_id, "microsoft", username, user_scope_id=user_scope_id)
    if not token:
        return False
    try:
        r = requests.get(
            "https://graph.microsoft.com/v1.0/me",
            params={"$select": "id"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False


@router.post("/accounts/{account_id}/verify")
async def verify_account(request: Request, account_id: str, _user: Dict[str, Any] = Depends(_get_current_user)):
    """
    Re-test connection for an existing account.
    IMAP: NOOP login. OAuth (gmail/microsoft): light API call. Updates last_verified_at on success.
    Scoped to current user in multi-user mode.
    """
    _username = _user.get("username", "admin")
    _user_scope_id = _user.get("user_scope_id")
    _, cred_username = _store_and_cred_from_user(_user)
    ec = _get_email_config(_username, user_scope_id=_user_scope_id)
    accounts = ec.get("accounts") or []
    acc = None
    for a in accounts:
        if (a.get("account_id") or a.get("email")) == account_id:
            acc = a
            break
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    provider = (acc.get("provider") or "imap").lower()
    if provider == "imap":
        creds = get_email_credentials(account_id, "imap", cred_username, user_scope_id=_user_scope_id)
        if not creds or "password" not in creds:
            raise HTTPException(status_code=400, detail="No stored password for this account")
        ok, err, hint = await asyncio.to_thread(_test_imap_login,
            acc.get("email") or account_id,
            creds["password"],
            acc.get("imap_host"),
            acc.get("imap_port"),
        )
        if ok:
            acc["last_verified_at"] = datetime.now(timezone.utc).isoformat()
            _save_email_config(ec, _username, user_scope_id=_user_scope_id)
        return {"ok": ok, "error": err if not ok else None, "hint": hint if not ok else None}
    if provider == "gmail":
        # to_thread: the helper does a BLOCKING requests.get; calling it directly from this
        # async handler stalls the whole uvicorn event loop (every request AND /ws).
        ok = await asyncio.to_thread(
            _verify_oauth_gmail, account_id, cred_username, user_scope_id=_user_scope_id)
        if ok:
            acc["last_verified_at"] = datetime.now(timezone.utc).isoformat()
            _save_email_config(ec, _username, user_scope_id=_user_scope_id)
        return {"ok": ok, "error": None if ok else "Gmail token invalid or expired", "hint": None}
    if provider == "microsoft":
        # to_thread: blocking requests.get inside, see the gmail branch above.
        ok = await asyncio.to_thread(
            _verify_oauth_microsoft, account_id, cred_username, user_scope_id=_user_scope_id)
        if ok:
            acc["last_verified_at"] = datetime.now(timezone.utc).isoformat()
            _save_email_config(ec, _username, user_scope_id=_user_scope_id)
        return {"ok": ok, "error": None if ok else "Microsoft token invalid or expired", "hint": None}
    raise HTTPException(status_code=400, detail="Verify not supported for this provider")




class PatchAccountBody(BaseModel):
    auto_sync_enabled: Optional[bool] = None
    label: Optional[str] = None


@router.patch("/accounts/{account_id}")
async def patch_account(request: Request, account_id: str, body: PatchAccountBody, _user: Dict[str, Any] = Depends(_get_current_user)):
    """Update account settings (e.g. auto_sync_enabled, label). Scoped to current user."""
    _username = _user.get("username", "admin")
    _user_scope_id = _user.get("user_scope_id")
    ec = _get_email_config(_username, user_scope_id=_user_scope_id)
    accounts = ec.get("accounts") or []
    aid_lower = (account_id or "").strip().lower()
    for a in accounts:
        cand = (a.get("account_id") or a.get("email") or "").strip().lower()
        if cand == aid_lower:
            if body.auto_sync_enabled is not None:
                a["auto_sync_enabled"] = bool(body.auto_sync_enabled)
            if body.label is not None:
                a["label"] = (body.label or "").strip()[:64]
            _save_email_config(ec, _username, user_scope_id=_user_scope_id)
            return {"ok": True, "account_id": a.get("account_id") or a.get("email")}
    raise HTTPException(status_code=404, detail="Account not found")


@router.delete("/accounts/{account_id}")
async def remove_account(request: Request, account_id: str, _user: Dict[str, Any] = Depends(_get_current_user)):
    """Calendar-safe account delete via the shared email_accounts cascade: drops the
    v2 store rows + the mail-only credential lanes; a gmail/microsoft account keeps
    its shared OAuth token AND config entry (flagged mail_enabled=False) so Calendar
    stays connected, a mail-only account is fully removed. Scoped to current user."""
    from vaf.core.email_accounts import delete_mail_account
    _username = _user.get("username", "admin")
    _user_scope_id = _user.get("user_scope_id")
    _, cred_username = _store_and_cred_from_user(_user)
    res = await asyncio.to_thread(
        lambda: delete_mail_account(account_id, username=_username,
                                    cred_username=cred_username, user_scope_id=_user_scope_id))
    return {"ok": True, **res}
