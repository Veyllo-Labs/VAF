"""
Email connection API: OAuth2 PKCE start/callback and account CRUD.

Credentials are stored in credential_store (keyring or encrypted file);
config holds only account metadata.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from datetime import datetime, timezone

from vaf.core.config import Config
from vaf.core.credential_store import get_email_credentials, set_email_imap_password
from vaf.core.oauth_pkce import (
    exchange_code_for_tokens,
    get_authorization_url,
    get_state_provider,
)

logger = logging.getLogger("vaf.api.email")

router = APIRouter(prefix="/api/email", tags=["email"])

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


def _get_email_config() -> Dict[str, Any]:
    raw = Config.get("email_config")
    if isinstance(raw, dict):
        return raw
    return {"accounts": []}


def _save_email_config(ec: Dict[str, Any]) -> None:
    config = Config.load()
    config["email_config"] = ec
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
        hint = "Gmail with 2FA requires an App Password: Google Account → Security → 2-Step Verification → App passwords."
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


def _add_account(account_id: str, provider: str, email: str, enabled: bool = True) -> None:
    ec = _get_email_config()
    accounts: List[Dict[str, Any]] = list(ec.get("accounts") or [])
    for a in accounts:
        if (a.get("account_id") or a.get("email")) == account_id or a.get("email") == email:
            a["provider"] = provider
            a["enabled"] = enabled
            _save_email_config(ec)
            return
    accounts.append({
        "account_id": account_id,
        "provider": provider,
        "email": email or account_id,
        "enabled": enabled,
    })
    ec["accounts"] = accounts
    _save_email_config(ec)


@router.get("/oauth/start")
async def oauth_start(request: Request, provider: str = "gmail"):
    """
    Start OAuth2 PKCE flow. Returns authorization_url and state.
    Frontend opens authorization_url in browser; callback will run on this server.
    """
    if provider not in ("gmail", "microsoft", "apple"):
        raise HTTPException(status_code=400, detail="provider must be gmail, microsoft, or apple")
    base_url = str(request.base_url).rstrip("/")
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
    base_url = str(request.base_url).rstrip("/")
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


@router.get("/accounts")
async def list_accounts():
    """Return list of configured email accounts (metadata only, no credentials)."""
    ec = _get_email_config()
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
async def add_account(body: AddImapAccountRequest):
    """
    Add an IMAP/SMTP account (other provider). Password is stored in keyring/encrypted file only.
    Server host/port can be omitted; defaults are used for known domains (Gmail, Outlook, Yahoo, etc.).
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
    set_email_imap_password(email, password)
    ec = _get_email_config()
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
            _save_email_config(ec)
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
    _save_email_config(ec)
    ok, _, _ = _test_imap_login(email, password, imap_host, imap_port)
    if ok:
        for a in ec.get("accounts") or []:
            if (a.get("email") or "").lower() == email:
                a["last_verified_at"] = now_iso
                break
        _save_email_config(ec)
    return {"account_id": email, "email": email, "provider": "imap", "last_verified_at": now_iso if ok else None}


@router.post("/accounts/{account_id}/verify")
async def verify_account(account_id: str):
    """
    Re-test IMAP login for an existing account (credentials from keyring).
    Updates last_verified_at on success. Only for provider=imap.
    """
    ec = _get_email_config()
    accounts = ec.get("accounts") or []
    acc = None
    for a in accounts:
        if (a.get("account_id") or a.get("email")) == account_id:
            acc = a
            break
    if not acc or acc.get("provider") != "imap":
        raise HTTPException(status_code=404, detail="IMAP account not found")
    creds = get_email_credentials(account_id, "imap")
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
        _save_email_config(ec)
    return {"ok": ok, "error": err if not ok else None, "hint": hint if not ok else None}


@router.delete("/accounts/{account_id}")
async def remove_account(account_id: str):
    """Remove account from config and delete credentials from keyring."""
    from vaf.core.credential_store import delete_email_credentials
    ec = _get_email_config()
    accounts = [a for a in (ec.get("accounts") or []) if a.get("account_id") != account_id and a.get("email") != account_id]
    ec["accounts"] = accounts
    _save_email_config(ec)
    delete_email_credentials(account_id, provider=None)
    return {"ok": True}
