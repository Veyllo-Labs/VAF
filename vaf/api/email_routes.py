"""
Email connection API: OAuth2 PKCE start/callback and account CRUD.

Credentials are stored in credential_store (keyring or encrypted file);
config holds only account metadata.
"""
import asyncio
import logging
import re
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
    update_message_answered as store_update_message_answered,
    update_message_category as store_update_message_category,
    upsert_messages,
)
from vaf.core.email_transport import apply_sender_rules_to_category, fetch_mail, get_message_body_plain
from vaf.core.oauth_pkce import (
    exchange_code_for_tokens,
    get_authorization_url,
    get_state_provider,
    get_valid_access_token,
    is_oauth_provider_configured,
)

logger = logging.getLogger("vaf.api.email")

router = APIRouter(prefix="/api/email", tags=["email"])


def _get_current_username(request: Request) -> str:
    """Current user from auth middleware, or local admin. Used to scope email data per user."""
    from vaf.api.config_routes import get_current_username as get_username
    return get_username(request)

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


def _get_email_config(username: Optional[str] = None) -> Dict[str, Any]:
    """Return email config for the given user. When username is None or local admin, use legacy email_config."""
    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
    if not username or username.strip().lower() == local_admin:
        raw = Config.get("email_config")
        if isinstance(raw, dict):
            return raw
        return {"accounts": []}
    by_user = Config.get("email_config_by_user") or {}
    ec = by_user.get(username.strip(), {}) if isinstance(by_user, dict) else {}
    return ec if isinstance(ec, dict) else {"accounts": []}


def _save_email_config(ec: Dict[str, Any], username: Optional[str] = None) -> None:
    """Save email config for the given user. When username is None or local admin, write to legacy email_config."""
    config = Config.load()
    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
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


def _add_account(account_id: str, provider: str, email: str, enabled: bool = True, username: Optional[str] = None) -> None:
    ec = _get_email_config(username)
    accounts: List[Dict[str, Any]] = list(ec.get("accounts") or [])
    for a in accounts:
        if (a.get("account_id") or a.get("email")) == account_id or a.get("email") == email:
            a["provider"] = provider
            a["enabled"] = enabled
            _save_email_config(ec, username)
            return
    accounts.append({
        "account_id": account_id,
        "provider": provider,
        "email": email or account_id,
        "enabled": enabled,
    })
    ec["accounts"] = accounts
    _save_email_config(ec, username)


def _oauth_callback_base_url() -> str:
    """
    Base URL for OAuth redirect_uri. Must point to this backend so the callback is handled here.
    When the frontend is behind a proxy (e.g. Next.js 3000 -> backend 8001), request.base_url
    can be the frontend origin, which would send the user to the wrong server after sign-in.
    """
    explicit = (Config.get("email_oauth_callback_base_url") or "").strip().rstrip("/")
    if explicit:
        return explicit
    port = Config.get("local_network_port", 8001)
    return f"http://127.0.0.1:{port}"


@router.get("/oauth/start")
async def oauth_start(request: Request, provider: str = "gmail"):
    """
    Start OAuth2 PKCE flow. Returns authorization_url and state.
    Frontend opens authorization_url in browser; callback will run on this server.
    """
    if provider not in ("gmail", "microsoft", "apple"):
        raise HTTPException(status_code=400, detail="provider must be gmail, microsoft, or apple")
    base_url = _oauth_callback_base_url()
    redirect_uri = f"{base_url}/api/email/oauth/callback"
    try:
        auth_url, state = get_authorization_url(provider, redirect_uri)
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
        data = exchange_code_for_tokens(provider, code, state, redirect_uri)
        account_id = data.get("account_id") or "unknown"
        _add_account(account_id, provider, account_id if "@" in account_id else account_id, enabled=True)
        return _redirect_success(account_id, provider)
    except ValueError as e:
        logger.warning("OAuth callback error: %s", e)
        return _redirect_error(str(e))


def _redirect_success(account_id: str, provider: str) -> RedirectResponse:
    # Redirect to frontend; use hash so server doesn't see token
    # Frontend origin from env or default 3000
    import os
    port = os.environ.get("VAF_WEB_UI_PORT", "3000")
    url = f"http://127.0.0.1:{port}/settings?connections=1&email_oauth=success&account={account_id}&provider={provider}"
    return RedirectResponse(url=url, status_code=302)


def _redirect_error(message: str) -> HTMLResponse:
    port = __import__("os").environ.get("VAF_WEB_UI_PORT", "3000")
    url = f"http://127.0.0.1:{port}/settings?connections=1&email_oauth=error"
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
async def list_accounts(_username: str = Depends(_get_current_username)):
    """Return list of configured email accounts for the current user (metadata only, no credentials)."""
    ec = _get_email_config(_username)
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
async def add_account(request: Request, body: AddImapAccountRequest, _username: str = Depends(_get_current_username)):
    """
    Add an IMAP/SMTP account (other provider). Password is stored in keyring/encrypted file only.
    Server host/port can be omitted; defaults are used for known domains (Gmail, Outlook, Yahoo, etc.).
    Scoped to current user in multi-user (network) mode.
    """
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
    cred_username = None if _username.strip().lower() == (Config.get("local_admin_username") or "admin").strip().lower() else _username
    set_email_imap_password(email, password, cred_username)
    ec = _get_email_config(_username)
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
            _save_email_config(ec, _username)
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
    })
    ec["accounts"] = accounts
    _save_email_config(ec, _username)
    ok, _, _ = _test_imap_login(email, password, imap_host, imap_port)
    if ok:
        for a in ec.get("accounts") or []:
            if (a.get("email") or "").lower() == email:
                a["last_verified_at"] = now_iso
                break
        _save_email_config(ec, _username)
    return {"account_id": email, "email": email, "provider": "imap", "last_verified_at": now_iso if ok else None}


def _verify_oauth_gmail(account_id: str, username: Optional[str] = None) -> bool:
    """Verify Gmail OAuth by calling users.getProfile. Returns True if token is valid."""
    token = get_valid_access_token(account_id, "gmail", username)
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


def _verify_oauth_microsoft(account_id: str, username: Optional[str] = None) -> bool:
    """Verify Microsoft OAuth by calling GET /me. Returns True if token is valid."""
    token = get_valid_access_token(account_id, "microsoft", username)
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
async def verify_account(request: Request, account_id: str, _username: str = Depends(_get_current_username)):
    """
    Re-test connection for an existing account.
    IMAP: NOOP login. OAuth (gmail/microsoft): light API call. Updates last_verified_at on success.
    Scoped to current user in multi-user mode.
    """
    cred_username = None if _username.strip().lower() == (Config.get("local_admin_username") or "admin").strip().lower() else _username
    ec = _get_email_config(_username)
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
        creds = get_email_credentials(account_id, "imap", cred_username)
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
            _save_email_config(ec, _username)
        return {"ok": ok, "error": err if not ok else None, "hint": hint if not ok else None}
    if provider == "gmail":
        ok = _verify_oauth_gmail(account_id, cred_username)
        if ok:
            acc["last_verified_at"] = datetime.now(timezone.utc).isoformat()
            _save_email_config(ec, _username)
        return {"ok": ok, "error": None if ok else "Gmail token invalid or expired", "hint": None}
    if provider == "microsoft":
        ok = _verify_oauth_microsoft(account_id, cred_username)
        if ok:
            acc["last_verified_at"] = datetime.now(timezone.utc).isoformat()
            _save_email_config(ec, _username)
        return {"ok": ok, "error": None if ok else "Microsoft token invalid or expired", "hint": None}
    raise HTTPException(status_code=400, detail="Verify not supported for this provider")


@router.post("/accounts/{account_id}/sync")
async def sync_account(request: Request, account_id: str, folder: str = "INBOX", max_messages: int = 100, _username: str = Depends(_get_current_username)):
    """
    Fetch messages from provider and store them in the local sync store.
    Updates last_verified_at on success. Returns { ok, count, error? }. Scoped to current user.
    """
    cred_username = None if _username.strip().lower() == (Config.get("local_admin_username") or "admin").strip().lower() else _username
    ec = _get_email_config(_username)
    accounts = ec.get("accounts") or []
    acc = None
    for a in accounts:
        if (a.get("account_id") or a.get("email")) == account_id:
            acc = a
            break
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    max_messages = min(max(1, max_messages), 200)
    try:
        messages = fetch_mail(account_id, folder=folder, max_messages=max_messages, username=cred_username)
    except Exception as e:
        logger.warning("Sync fetch failed for %s: %s", account_id[:8] + "***", e)
        return {"ok": False, "count": 0, "error": str(e)}
    store_username = "" if cred_username is None else cred_username
    count = upsert_messages(account_id, folder, messages, username=store_username)
    deleted = delete_messages_older_than(username=store_username, days=90)
    acc["last_verified_at"] = datetime.now(timezone.utc).isoformat()
    _save_email_config(ec, _username)
    return {"ok": True, "count": count, "deleted": deleted}


@router.get("/messages/body")
async def get_message_body(
    request: Request,
    account_id: str,
    message_id: str,
    folder: str = "INBOX",
    provider_message_id: Optional[str] = None,
    _username: str = Depends(_get_current_username),
):
    """
    Fetch full message body as plain text only (no HTML). Used when opening a message in the UI.
    provider_message_id is required for Gmail/Microsoft for efficient fetch; optional for IMAP.
    """
    store_username = "" if _username.strip().lower() == (Config.get("local_admin_username") or "admin").strip().lower() else _username
    body = get_message_body_plain(
        account_id=account_id,
        message_id=message_id,
        folder=folder,
        username=store_username,
        provider_message_id=provider_message_id or None,
    )
    if body is None:
        raise HTTPException(status_code=404, detail="Message or body not found")
    return {"body": body}


@router.get("/messages")
async def get_synced_messages(
    request: Request,
    account_id: Optional[str] = None,
    folder: str = "INBOX",
    category: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    _username: str = Depends(_get_current_username),
):
    """
    List synced messages from the local store (paginated). Scoped to current user.
    account_id: optional; if omitted, returns messages from all accounts for that user.
    category: optional primary|social|promotions (Gmail-style). Spam is never stored or returned.
    """
    limit = min(max(1, limit), 100)
    offset = max(0, offset)
    store_username = "" if _username.strip().lower() == (Config.get("local_admin_username") or "admin").strip().lower() else _username
    items = store_list_messages(account_id=account_id, folder=folder, limit=limit, offset=offset, username=store_username, category=category)
    return {"messages": items, "folder": folder, "category": category}


@router.get("/categories")
async def get_categories(_username: str = Depends(_get_current_username)):
    """List distinct categories for the current user (primary, social, promotions + any custom)."""
    store_username = "" if _username.strip().lower() == (Config.get("local_admin_username") or "admin").strip().lower() else _username
    categories = store_list_categories(store_username)
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
    _username: str = Depends(_get_current_username),
):
    """
    Update a message's category (label). Always adds a sender rule for this message's From
    address and applies it to all synced messages from that sender (existing and future).
    """
    store_username = "" if _username.strip().lower() == (Config.get("local_admin_username") or "admin").strip().lower() else _username
    cat = body.category.strip().lower().replace(" ", "_")[:64] or "primary"
    ok = store_update_message_category(
        store_username,
        body.account_id,
        body.folder,
        body.message_id,
        cat,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Message not found")
    if body.answered_at is not None:
        store_update_message_answered(
            store_username, body.account_id, body.folder, body.message_id,
            answered_at=(body.answered_at.strip() or None),
        )
    updated = 1
    from_addr = get_message_from_addr(
        store_username, body.account_id, body.folder, body.message_id
    )
    if from_addr:
        pattern = _pattern_from_from_addr(from_addr)
        if pattern:
            ec = _get_email_config(_username)
            rules = list(ec.get("sender_category_rules") or [])
            rules = [r for r in rules if isinstance(r, dict) and (r.get("pattern") or "").strip().lower() != pattern]
            rules.append({"pattern": pattern, "category": cat})
            ec["sender_category_rules"] = rules
            _save_email_config(ec, _username)
        rows = list_for_sender_relabel(store_username)
        for row in rows:
            new_cat = apply_sender_rules_to_category(
                row.get("from_addr") or "",
                row.get("category") or "primary",
                store_username if store_username else None,
            )
            new_cat = (new_cat or "primary").strip().lower().replace(" ", "_")[:64] or "primary"
            if new_cat != (row.get("category") or "primary"):
                if store_update_message_category(
                    store_username,
                    row["account_id"],
                    row["folder"],
                    row["message_id"],
                    new_cat,
                ):
                    updated += 1
    return {"ok": True, "category": cat, "updated": updated}


@router.post("/messages/apply-sender-rules")
async def apply_sender_rules(
    _username: str = Depends(_get_current_username),
):
    """
    Re-apply sender→category rules to all synced messages for the current user (backfill).
    Use after adding or changing sender_category_rules in config so existing and new mails get the right label.
    Returns { ok, updated } with the number of messages whose category was changed.
    """
    store_username = "" if _username.strip().lower() == (Config.get("local_admin_username") or "admin").strip().lower() else _username
    rows = list_for_sender_relabel(store_username)
    updated = 0
    for row in rows:
        new_cat = apply_sender_rules_to_category(
            row.get("from_addr") or "",
            row.get("category") or "primary",
            store_username if store_username else None,
        )
        new_cat = (new_cat or "primary").strip().lower().replace(" ", "_")[:64] or "primary"
        if new_cat != (row.get("category") or "primary"):
            ok = store_update_message_category(
                store_username,
                row["account_id"],
                row["folder"],
                row["message_id"],
                new_cat,
            )
            if ok:
                updated += 1
    return {"ok": True, "updated": updated}


class PatchAccountBody(BaseModel):
    auto_sync_enabled: Optional[bool] = None


@router.patch("/accounts/{account_id}")
async def patch_account(request: Request, account_id: str, body: PatchAccountBody, _username: str = Depends(_get_current_username)):
    """Update account settings (e.g. auto_sync_enabled). Scoped to current user."""
    ec = _get_email_config(_username)
    accounts = ec.get("accounts") or []
    for a in accounts:
        if (a.get("account_id") or a.get("email")) == account_id:
            if body.auto_sync_enabled is not None:
                a["auto_sync_enabled"] = bool(body.auto_sync_enabled)
            _save_email_config(ec, _username)
            return {"ok": True, "account_id": account_id}
    raise HTTPException(status_code=404, detail="Account not found")


@router.delete("/accounts/{account_id}")
async def remove_account(request: Request, account_id: str, _username: str = Depends(_get_current_username)):
    """Remove account from config and delete credentials from keyring. Scoped to current user."""
    from vaf.core.credential_store import delete_email_credentials
    cred_username = None if _username.strip().lower() == (Config.get("local_admin_username") or "admin").strip().lower() else _username
    ec = _get_email_config(_username)
    accounts = [a for a in (ec.get("accounts") or []) if a.get("account_id") != account_id and a.get("email") != account_id]
    ec["accounts"] = accounts
    _save_email_config(ec, _username)
    delete_email_credentials(account_id, provider=None, username=cred_username)
    return {"ok": True}


# Interval for background auto-sync (must match frontend AUTO_SYNC_INTERVAL_MS / 1000)
EMAIL_AUTO_SYNC_INTERVAL_SEC = 30 * 60  # 30 minutes


def _collect_auto_sync_accounts() -> List[tuple[str, Dict[str, Any], Dict[str, Any]]]:
    """Return list of (config_username, account_dict, full_ec) for every account with auto_sync_enabled."""
    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
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
        cred_username = None if config_username.strip().lower() == (Config.get("local_admin_username") or "admin").strip().lower() else config_username
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
