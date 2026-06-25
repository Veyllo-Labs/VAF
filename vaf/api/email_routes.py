# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Email connection API: OAuth2 PKCE start/callback and account CRUD.

Credentials are stored in credential_store (keyring or encrypted file);
config holds only account metadata.
"""
import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from datetime import datetime, timezone

import requests

from vaf.core.config import Config
from vaf.core.credential_store import get_email_credentials, set_email_imap_password
from vaf.core.email_sync_store import (
    delete_messages_older_than,
    get_message_from_addr,
    init_store,
    list_categories as store_list_categories,
    list_for_sender_relabel,
    list_messages as store_list_messages,
    search_messages as store_search_messages,
    update_message_answered as store_update_message_answered,
    update_message_category as store_update_message_category,
    upsert_messages,
)
from vaf.core.email_transport import apply_sender_rules_to_category, fetch_mail, get_message_body_plain
from vaf.tools.mail_utils import annotate_messages_with_agent_visibility, store_candidates_for_mail
from vaf.core.platform import Platform
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

logger = logging.getLogger("vaf.api.email")

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


# Default IMAP/SMTP servers by domain (TLS)
IMAP_SMTP_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "gmail.com": {"imap_host": "imap.gmail.com", "imap_port": 993, "smtp_host": "smtp.gmail.com", "smtp_port": 587},
    "googlemail.com": {"imap_host": "imap.gmail.com", "imap_port": 993, "smtp_host": "smtp.gmail.com", "smtp_port": 587},
    "outlook.com": {"imap_host": "outlook.office365.com", "imap_port": 993, "smtp_host": "smtp.office365.com", "smtp_port": 587},
    "hotmail.com": {"imap_host": "outlook.office365.com", "imap_port": 993, "smtp_host": "smtp.office365.com", "smtp_port": 587},
    "live.com": {"imap_host": "outlook.office365.com", "imap_port": 993, "smtp_host": "smtp.office365.com", "smtp_port": 587},
    "yahoo.com": {"imap_host": "imap.mail.yahoo.com", "imap_port": 993, "smtp_host": "smtp.mail.yahoo.com", "smtp_port": 587},
    "icloud.com": {"imap_host": "imap.mail.me.com", "imap_port": 993, "smtp_host": "smtp.mail.me.com", "smtp_port": 587},
    "me.com": {"imap_host": "imap.mail.me.com", "imap_port": 993, "smtp_host": "smtp.mail.me.com", "smtp_port": 587},
}


class AddImapAccountRequest(BaseModel):
    email: str
    password: str
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None


class TestImapRequest(BaseModel):
    """Body for connection test only; nothing is saved."""
    email: str
    password: str
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None


def _get_email_config(
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return email config for the given user. When username is None or local admin, use legacy email_config.
    If user_scope_id is set, email_config_by_scope is tried first (Phase 2); otherwise username-based lookup."""
    local_admin_scope = get_local_admin_scope_id()
    if user_scope_id:
        by_scope = Config.get("email_config_by_scope") or {}
        if isinstance(by_scope, dict):
            ec = by_scope.get(str(user_scope_id).strip())
            if isinstance(ec, dict) and ec.get("accounts") is not None:
                return ec
        if str(user_scope_id).strip() == str(local_admin_scope).strip():
            raw = Config.get("email_config")
            if isinstance(raw, dict):
                return raw
            return {"accounts": []}
    local_admin = get_local_admin_username().lower()
    if not username or username.strip().lower() == local_admin:
        raw = Config.get("email_config")
        if isinstance(raw, dict):
            return raw
        return {"accounts": []}
    by_user = Config.get("email_config_by_user") or {}
    ec = by_user.get(username.strip(), {}) if isinstance(by_user, dict) else {}
    return ec if isinstance(ec, dict) else {"accounts": []}


def _save_email_config(
    ec: Dict[str, Any],
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> None:
    """Save email config for the given user. When username is None or local admin, write to legacy email_config.
    If user_scope_id is set, write to email_config_by_scope (Phase 2); otherwise username-based."""
    config = Config.load()
    local_admin_scope = get_local_admin_scope_id()
    if user_scope_id and str(user_scope_id).strip() != str(local_admin_scope).strip():
        by_scope = config.get("email_config_by_scope") or {}
        if not isinstance(by_scope, dict):
            by_scope = {}
        by_scope[str(user_scope_id).strip()] = ec
        config["email_config_by_scope"] = by_scope
        Config.save(config)
        return
    local_admin = get_local_admin_username().lower()
    if not username or username.strip().lower() == local_admin:
        config["email_config"] = ec
    else:
        by_user = config.get("email_config_by_user") or {}
        if not isinstance(by_user, dict):
            by_user = {}
        by_user[username.strip()] = ec
        config["email_config_by_user"] = by_user
    Config.save(config)


def _test_imap_login(
    email: str,
    password: str,
    imap_host: Optional[str] = None,
    imap_port: Optional[int] = None,
) -> tuple[bool, str, Optional[str]]:
    """
    Try IMAP login with the given credentials. Does not save anything.
    Returns (success, error_message, hint). hint is for 2FA/App-Password (e.g. Gmail).
    """
    import imaplib
    email = (email or "").strip().lower()
    domain = email.split("@")[-1] if "@" in email else ""
    defaults = IMAP_SMTP_DEFAULTS.get(domain, {})
    host = (imap_host or "").strip() or defaults.get("imap_host", "imap.gmail.com")
    port = imap_port if imap_port is not None else defaults.get("imap_port", 993)
    hint = None
    if domain in ("gmail.com", "googlemail.com"):
        hint = "Gmail with 2FA requires an App Password. Create one at: https://myaccount.google.com/apppasswords"
    elif domain in ("outlook.com", "hotmail.com", "live.com", "live.de", "msn.com", "outlook.de", "office365.com"):
        hint = "Outlook.com no longer supports IMAP with app passwords (Microsoft retired Basic auth in 2024). Use 'Sign in with Microsoft' in the wizard instead—an admin must configure the OAuth client first (expand 'For admins' in the email wizard)."
    try:
        conn = imaplib.IMAP4_SSL(host, port=port)
        conn.login(email, password)
        conn.noop()
        conn.logout()
        return True, "", None
    except imaplib.IMAP4.error as e:
        err = str(e).strip() or "IMAP login failed"
        if "Authentication failed" in err or "LOGIN failed" in err or "invalid credentials" in err.lower():
            return False, err, hint
        return False, err, hint
    except Exception as e:
        err = str(e).strip() or "Connection failed"
        return False, err, hint


def _add_account(
    account_id: str,
    provider: str,
    email: str,
    enabled: bool = True,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> None:
    ec = _get_email_config(username, user_scope_id=user_scope_id)
    accounts: List[Dict[str, Any]] = list(ec.get("accounts") or [])
    for a in accounts:
        if (a.get("account_id") or a.get("email")) == account_id or a.get("email") == email:
            a["provider"] = provider
            a["enabled"] = enabled
            _save_email_config(ec, username, user_scope_id=user_scope_id)
            return
    accounts.append({
        "account_id": account_id,
        "provider": provider,
        "email": email or account_id,
        "enabled": enabled,
        "label": "",
    })
    ec["accounts"] = accounts
    _save_email_config(ec, username, user_scope_id=user_scope_id)


def _oauth_callback_base_url() -> str:
    """
    Base URL for OAuth redirect_uri. Must point to this backend so the callback is handled here.
    When the frontend is behind a proxy (e.g. Next.js 3000 -> backend 8001), request.base_url
    can be the frontend origin, which would send the user to the wrong server after sign-in.
    """
    explicit = (Config.get("email_oauth_callback_base_url") or "").strip().rstrip("/")
    if explicit:
        return explicit
    # Default to the integrated HTTPS proxy when network mode is active.
    # This avoids protocol mismatches (HTTP callback to a TLS-only backend),
    # which surface in browsers as ERR_EMPTY_RESPONSE.
    network_on = bool(Config.get("local_network_enabled", False))
    tls_on = bool(Config.get("local_network_tls_enabled", False))
    if network_on and tls_on:
        https_port = int(Config.get("local_network_https_port", 443) or 443)
        if Platform.is_windows() and https_port == 443:
            https_port = 8443
        suffix = "" if https_port == 443 else f":{https_port}"
        return f"https://localhost{suffix}"
    port = int(Config.get("local_network_port", 8001) or 8001)
    return f"http://localhost:{port}"


def _frontend_base_url() -> str:
    """Return Web UI base URL for post-OAuth redirects."""
    network_on = bool(Config.get("local_network_enabled", False))
    tls_on = bool(Config.get("local_network_tls_enabled", False))
    if network_on and tls_on:
        https_port = int(Config.get("local_network_https_port", 443) or 443)
        if Platform.is_windows() and https_port == 443:
            https_port = 8443
        suffix = "" if https_port == 443 else f":{https_port}"
        return f"https://localhost{suffix}"
    port = __import__("os").environ.get("VAF_WEB_UI_PORT", "3000")
    return f"http://localhost:{port}"


@router.get("/oauth/start")
async def oauth_start(request: Request, provider: str = "gmail", _user: Dict[str, Any] = Depends(_get_current_user)):
    """
    Start OAuth2 PKCE flow. Returns authorization_url and state.
    Frontend opens authorization_url in browser; callback will run on this server.
    """
    if provider not in ("gmail", "microsoft", "apple"):
        raise HTTPException(status_code=400, detail="provider must be gmail, microsoft, or apple")
    require_oauth_actor_in_network_mode(request)
    base_url = _oauth_callback_base_url()
    redirect_uri = f"{base_url}/api/email/oauth/callback"
    _username = _user.get("username")
    _user_scope_id = _user.get("user_scope_id")
    try:
        auth_url, state = get_authorization_url(provider, redirect_uri, username=_username, user_scope_id=_user_scope_id)
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
        data = exchange_code_for_tokens(provider, code, state, redirect_uri)
        account_id = data.get("account_id") or "unknown"
        # Use retrieved scope/user from OAuth state to add the account
        _username = data.get("username")
        _user_scope_id = data.get("user_scope_id")
        _add_account(account_id, provider, account_id if "@" in account_id else account_id, enabled=True, username=_username, user_scope_id=_user_scope_id)
        logger.info("email oauth callback: account added account_id=%s provider=%s scope=%s", account_id, provider, _user_scope_id)
        try:
            from vaf.core.log_helper import append_domain_log
            append_domain_log("backend", f"[EMAIL_OAUTH] account added account_id={account_id} provider={provider}")
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
    url = f"{_frontend_base_url()}/settings?connections=1&email_oauth=error"
    html_content = f"""
    <!DOCTYPE html>
    <html><head><meta charset="utf-8"><title>Email connection failed</title></head>
    <body style="font-family:sans-serif;max-width:480px;margin:2rem auto;padding:1rem;">
    <h2>Email connection failed</h2>
    <p>{message}</p>
    <p><a href="{url}">Back to Settings</a></p>
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
async def test_imap_connection(body: TestImapRequest):
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
    ok, err, hint = _test_imap_login(
        email,
        password,
        body.imap_host,
        body.imap_port,
    )
    if ok:
        return {"ok": True}
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
            ok, _, _ = _test_imap_login(email, password, imap_host, imap_port)
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
    ok, _, _ = _test_imap_login(email, password, imap_host, imap_port)
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
        ok, err, hint = _test_imap_login(
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
        ok = _verify_oauth_gmail(account_id, cred_username, user_scope_id=_user_scope_id)
        if ok:
            acc["last_verified_at"] = datetime.now(timezone.utc).isoformat()
            _save_email_config(ec, _username, user_scope_id=_user_scope_id)
        return {"ok": ok, "error": None if ok else "Gmail token invalid or expired", "hint": None}
    if provider == "microsoft":
        ok = _verify_oauth_microsoft(account_id, cred_username, user_scope_id=_user_scope_id)
        if ok:
            acc["last_verified_at"] = datetime.now(timezone.utc).isoformat()
            _save_email_config(ec, _username, user_scope_id=_user_scope_id)
        return {"ok": ok, "error": None if ok else "Microsoft token invalid or expired", "hint": None}
    raise HTTPException(status_code=400, detail="Verify not supported for this provider")


@router.post("/accounts/{account_id}/sync")
async def sync_account(request: Request, account_id: str, folder: str = "INBOX", max_messages: int = 100, _user: Dict[str, Any] = Depends(_get_current_user)):
    """
    Fetch messages from provider and store them in the local sync store.
    Updates last_verified_at on success. Returns { ok, count, error? }. Scoped to current user.
    """
    _username = _user.get("username", "admin")
    _user_scope_id = _user.get("user_scope_id")
    ec = _get_email_config(_username, user_scope_id=_user_scope_id)
    accounts = ec.get("accounts") or []
    acc = None
    aid_lower = (account_id or "").strip().lower()
    for a in accounts:
        cand = (a.get("account_id") or a.get("email") or "").strip().lower()
        if cand == aid_lower:
            acc = a
            break
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    max_messages = min(max(1, max_messages), 200)
    store_username, cred_username = _store_and_cred_from_user(_user)
    for attempt in range(3):
        try:
            messages = fetch_mail(account_id, folder=folder, max_messages=max_messages, username=cred_username, user_scope_id=_user_scope_id)
            count = upsert_messages(account_id, folder, messages, username=store_username, user_scope_id=_user_scope_id)
            deleted = delete_messages_older_than(username=store_username, user_scope_id=_user_scope_id, days=90)
            acc["last_verified_at"] = datetime.now(timezone.utc).isoformat()
            _save_email_config(ec, _username, user_scope_id=_user_scope_id)
            return {"ok": True, "count": count, "deleted": deleted}
        except Exception as e:
            err_str = str(e)
            is_locked = "locked" in err_str.lower() or "database is locked" in err_str.lower() or "sqlite_busy" in err_str.lower()
            if is_locked and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            logger.warning("Sync failed for %s: %s", account_id[:8] + "***", e)
            return {"ok": False, "count": 0, "error": err_str}


@router.get("/messages/body")
async def get_message_body(
    request: Request,
    account_id: str,
    message_id: str,
    folder: str = "INBOX",
    provider_message_id: Optional[str] = None,
    _user: Dict[str, Any] = Depends(_get_current_user),
):
    """
    Fetch full message body as plain text only (no HTML). Used when opening a message in the UI.
    provider_message_id is required for Gmail/Microsoft for efficient fetch; optional for IMAP.
    """
    try:
        _username = _user.get("username", "admin")
        _user_scope_id = _user.get("user_scope_id")
        store_username, _ = _store_and_cred_from_user(_user)
        body = get_message_body_plain(
            account_id=account_id,
            message_id=message_id,
            folder=folder,
            username=store_username,
            user_scope_id=_user_scope_id,
            provider_message_id=provider_message_id or None,
        )
        if body is None:
            raise HTTPException(status_code=404, detail="Message or body not found")
        return {"body": body}
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("get_message_body failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/messages")
async def get_synced_messages(
    request: Request,
    account_id: Optional[str] = None,
    folder: str = "INBOX",
    category: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    _user: Dict[str, Any] = Depends(_get_current_user),
):
    """
    List synced messages from the local store (paginated). Uses same store fallback as mail_inbox
    (primary → legacy → single-scope) so the dashboard shows mails already in SQLite without waiting for sync.
    account_id: optional; if omitted, returns messages from all accounts for that user.
    category: optional primary|social|promotions (Gmail-style). Spam is never stored or returned.
    """
    _user_scope_id = _user.get("user_scope_id")
    limit = min(max(1, limit), 100)
    offset = max(0, offset)
    store_username, _ = _store_and_cred_from_user(_user)
    items: List[Dict[str, Any]] = []
    for try_username, try_scope_id in store_candidates_for_mail(store_username, _user_scope_id):
        items = store_list_messages(
            account_id=account_id,
            folder=folder,
            limit=limit,
            offset=offset,
            username=try_username,
            user_scope_id=try_scope_id,
            category=category,
        )
        if items:
            break
    items = annotate_messages_with_agent_visibility(items)
    return {"messages": items, "folder": folder, "category": category}


@router.get("/messages/search")
async def search_synced_messages(
    request: Request,
    query: str = "",
    folder: str = "INBOX",
    limit: int = 50,
    _user: Dict[str, Any] = Depends(_get_current_user),
):
    """
    Search synced messages by subject or sender (case-insensitive). Uses same store fallback as find_mail.
    query: search term (e.g. "lieferando", "Postman"); matched against subject and from address.
    """
    query = (query or "").strip()
    if not query:
        return {"messages": [], "query": "", "folder": folder}
    limit = min(max(1, limit), 100)
    store_username, _ = _store_and_cred_from_user(_user)
    _user_scope_id = _user.get("user_scope_id")
    items: List[Dict[str, Any]] = []
    for try_username, try_scope_id in store_candidates_for_mail(store_username, _user_scope_id):
        items = store_search_messages(
            query=query,
            folder=folder,
            limit=limit,
            username=try_username,
            user_scope_id=try_scope_id,
        )
        if items:
            break
    items = annotate_messages_with_agent_visibility(items)
    return {"messages": items, "query": query, "folder": folder}


@router.get("/categories")
async def get_categories(_user: Dict[str, Any] = Depends(_get_current_user)):
    """List distinct categories for the current user (primary, social, promotions + any custom)."""
    _username = _user.get("username", "admin")
    _user_scope_id = _user.get("user_scope_id")
    store_username, _ = _store_and_cred_from_user(_user)
    categories = store_list_categories(store_username, user_scope_id=_user_scope_id)
    return {"categories": categories}


class PatchMessageBody(BaseModel):
    account_id: str
    folder: str = "INBOX"
    message_id: str
    category: str
    answered_at: Optional[str] = None  # ISO timestamp when agent answered; set to mark "Benatwortet am ..."


def _pattern_from_from_addr(from_addr: str) -> str:
    """Derive a sender rule pattern from From header (e.g. 'Twitch <no-reply@twitch.tv>' -> 'no-reply@twitch.tv')."""
    s = (from_addr or "").strip()
    if not s:
        return s
    m = re.search(r"<([^>]+@[^>]+)>", s)
    if m:
        return m.group(1).strip().lower()
    if "@" in s:
        return s.lower()
    return s


@router.patch("/messages")
async def patch_message_category(
    body: PatchMessageBody,
    _user: Dict[str, Any] = Depends(_get_current_user),
):
    """
    Update a message's category (label). Always adds a sender rule for this message's From
    address and applies it to all synced messages from that sender (existing and future).
    """
    _username = _user.get("username", "admin")
    _user_scope_id = _user.get("user_scope_id")
    store_username, _ = _store_and_cred_from_user(_user)
    cat = body.category.strip().lower().replace(" ", "_")[:64] or "primary"
    ok = store_update_message_category(
        store_username,
        body.account_id,
        body.folder,
        body.message_id,
        cat,
        user_scope_id=_user_scope_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Message not found")
    if body.answered_at is not None:
        store_update_message_answered(
            store_username, body.account_id, body.folder, body.message_id,
            answered_at=(body.answered_at.strip() or None),
            user_scope_id=_user_scope_id,
        )
    updated = 1
    from_addr = get_message_from_addr(
        store_username, body.account_id, body.folder, body.message_id, user_scope_id=_user_scope_id
    )
    if from_addr:
        pattern = _pattern_from_from_addr(from_addr)
        if pattern:
            ec = _get_email_config(_username, user_scope_id=_user_scope_id)
            rules = list(ec.get("sender_category_rules") or [])
            rules = [r for r in rules if isinstance(r, dict) and (r.get("pattern") or "").strip().lower() != pattern]
            rules.append({"pattern": pattern, "category": cat})
            ec["sender_category_rules"] = rules
            _save_email_config(ec, _username, user_scope_id=_user_scope_id)
        rows = list_for_sender_relabel(store_username, user_scope_id=_user_scope_id)
        for row in rows:
            new_cat = apply_sender_rules_to_category(
                row.get("from_addr") or "",
                row.get("category") or "primary",
                store_username if store_username else None,
                user_scope_id=_user_scope_id,
            )
            new_cat = (new_cat or "primary").strip().lower().replace(" ", "_")[:64] or "primary"
            if new_cat != (row.get("category") or "primary"):
                if store_update_message_category(
                    store_username,
                    row["account_id"],
                    row["folder"],
                    row["message_id"],
                    new_cat,
                    user_scope_id=_user_scope_id,
                ):
                    updated += 1
    return {"ok": True, "category": cat, "updated": updated}


@router.post("/messages/apply-sender-rules")
async def apply_sender_rules(
    _user: Dict[str, Any] = Depends(_get_current_user),
):
    """
    Re-apply sender→category rules to all synced messages for the current user (backfill).
    Use after adding or changing sender_category_rules in config so existing and new mails get the right label.
    Returns { ok, updated } with the number of messages whose category was changed.
    """
    _username = _user.get("username", "admin")
    _user_scope_id = _user.get("user_scope_id")
    store_username, _ = _store_and_cred_from_user(_user)
    rows = list_for_sender_relabel(store_username, user_scope_id=_user_scope_id)
    updated = 0
    for row in rows:
        new_cat = apply_sender_rules_to_category(
            row.get("from_addr") or "",
            row.get("category") or "primary",
            store_username if store_username else None,
            user_scope_id=_user_scope_id,
        )
        new_cat = (new_cat or "primary").strip().lower().replace(" ", "_")[:64] or "primary"
        if new_cat != (row.get("category") or "primary"):
            ok = store_update_message_category(
                store_username,
                row["account_id"],
                row["folder"],
                row["message_id"],
                new_cat,
                user_scope_id=_user_scope_id,
            )
            if ok:
                updated += 1
    return {"ok": True, "updated": updated}


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
    """Remove account from config and delete credentials from keyring. Scoped to current user."""
    from vaf.core.credential_store import delete_email_credentials
    _username = _user.get("username", "admin")
    _user_scope_id = _user.get("user_scope_id")
    _, cred_username = _store_and_cred_from_user(_user)
    ec = _get_email_config(_username, user_scope_id=_user_scope_id)
    accounts = [a for a in (ec.get("accounts") or []) if a.get("account_id") != account_id and a.get("email") != account_id]
    ec["accounts"] = accounts
    _save_email_config(ec, _username, user_scope_id=_user_scope_id)
    delete_email_credentials(account_id, provider=None, username=cred_username, user_scope_id=_user_scope_id)
    return {"ok": True}


# Interval for background auto-sync (must match frontend AUTO_SYNC_INTERVAL_MS / 1000)
EMAIL_AUTO_SYNC_INTERVAL_SEC = 30 * 60  # 30 minutes


def _collect_auto_sync_accounts() -> List[tuple[str, Dict[str, Any], Dict[str, Any]]]:
    """Return list of (config_username, account_dict, full_ec) for every account with auto_sync_enabled."""
    local_admin = get_local_admin_username().lower()
    result: List[tuple[str, Dict[str, Any], Dict[str, Any]]] = []
    # Legacy / local admin
    ec = _get_email_config(None)
    if isinstance(ec, dict):
        for a in ec.get("accounts") or []:
            if a.get("auto_sync_enabled"):
                result.append((local_admin, a, ec))
    # Per-user config
    by_user = Config.get("email_config_by_user") or {}
    if isinstance(by_user, dict):
        for uname, user_ec in by_user.items():
            if not isinstance(user_ec, dict):
                continue
            for a in user_ec.get("accounts") or []:
                if a.get("auto_sync_enabled"):
                    result.append((uname.strip(), a, user_ec))
    return result


async def run_auto_sync_all_accounts(max_messages: int = 100) -> Dict[str, Any]:
    """
    Sync all email accounts that have auto_sync_enabled=True.
    Intended to be called periodically (e.g. every 30 min) from the web server.
    Runs fetch_mail in a thread so the event loop is not blocked.
    Returns summary: { "synced": N, "failed": N, "errors": [str, ...] }.
    """
    init_store()
    items = _collect_auto_sync_accounts()
    if not items:
        return {"synced": 0, "failed": 0, "errors": []}
    synced = 0
    failed = 0
    errors: List[str] = []
    for config_username, acc, ec in items:
        account_id = acc.get("account_id") or acc.get("email") or ""
        if not account_id:
            continue
        cred_username = None if config_username.strip().lower() == get_local_admin_username().lower() else config_username
        store_username = "" if cred_username is None else cred_username
        limit = min(max(1, max_messages), 200)
        try:
            messages = await asyncio.to_thread(
                fetch_mail,
                account_id,
                folder="INBOX",
                max_messages=limit,
                username=cred_username,
            )
        except Exception as e:
            logger.warning("Auto-sync fetch failed for %s: %s", account_id[:8] + "***", e)
            failed += 1
            errors.append(f"{account_id[:12]}...: {e}")
            continue
        count = upsert_messages(account_id, "INBOX", messages, username=store_username)
        delete_messages_older_than(username=store_username, days=90)
        acc["last_verified_at"] = datetime.now(timezone.utc).isoformat()
        _save_email_config(ec, config_username if store_username else None)
        synced += 1
        if count > 0:
            logger.info("Auto-sync completed for %s: %d messages", account_id[:8] + "***", count)
    return {"synced": synced, "failed": failed, "errors": errors}
