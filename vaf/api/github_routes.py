"""
GitHub connection API: OAuth2 flow and account management.

Credentials stored via vaf.github.credential_github; config holds only
account metadata (github_config for local admin, github_config_by_user for others).
"""

import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from vaf.core.config import Config
from vaf.api.config_routes import get_current_user_or_local_admin
from vaf.github.oauth import (
    get_authorization_url,
    get_github_callback_redirect_uri,
    get_state_redirect_base,
    get_state_user,
    exchange_code_for_tokens,
    is_github_oauth_configured,
    validate_pat_and_get_login,
    start_github_device_flow,
    poll_github_device_token,
    SCOPE_FULL,
    SCOPE_READ_ONLY,
)
from vaf.github.credential_github import (
    delete_github_credentials,
    get_github_oauth_token,
    set_github_oauth_tokens,
)
from vaf.github.activity import get_github_activity

logger = logging.getLogger("vaf.api.github")

router = APIRouter(prefix="/api/github", tags=["github"])


class ConnectTokenBody(BaseModel):
    token: str


class DeviceFlowPollBody(BaseModel):
    device_code: str


class PermissionUpdateBody(BaseModel):
    allow_write: bool


def _get_current_username(request: Request) -> str:
    from vaf.api.config_routes import get_current_username as get_username
    return get_username(request)


def _get_github_config(username: Optional[str] = None) -> Dict[str, Any]:
    """Return GitHub config for the given user."""
    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
    if not username or username.strip().lower() == local_admin:
        raw = Config.get("github_config")
        if isinstance(raw, dict):
            return raw
        return {"accounts": []}
    by_user = Config.get("github_config_by_user") or {}
    cc = by_user.get(username.strip(), {}) if isinstance(by_user, dict) else {}
    return cc if isinstance(cc, dict) else {"accounts": []}


def _save_github_config(gc: Dict[str, Any], username: Optional[str] = None) -> None:
    """Save GitHub config for the given user."""
    config = Config.load()
    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
    if not username or username.strip().lower() == local_admin:
        config["github_config"] = gc
    else:
        by_user = config.get("github_config_by_user") or {}
        if not isinstance(by_user, dict):
            by_user = {}
        by_user[username.strip()] = gc
        config["github_config_by_user"] = by_user
    Config.save(config)


def _redirect_success(account_id: str, redirect_base: Optional[str] = None) -> RedirectResponse:
    """Redirect to frontend after successful GitHub OAuth."""
    port = os.environ.get("VAF_WEB_UI_PORT", "3000")
    base = (redirect_base or "").rstrip("/")
    if base and base.startswith("http"):
        url = f"{base}/settings?connections=1&github_oauth=success&account={account_id}"
    else:
        url = f"http://localhost:{port}/settings?connections=1&github_oauth=success&account={account_id}"
    return RedirectResponse(url=url, status_code=302)


def _redirect_error(message: str, redirect_base: Optional[str] = None) -> HTMLResponse:
    """Return an error page with a link back to settings."""
    port = os.environ.get("VAF_WEB_UI_PORT", "3000")
    base = (redirect_base or "").rstrip("/")
    if base and base.startswith("http"):
        url = f"{base}/settings?connections=1&github_oauth=error"
    else:
        url = f"http://localhost:{port}/settings?connections=1&github_oauth=error"
    msg_escaped = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    html_content = f"""
    <!DOCTYPE html>
    <html><head><meta charset="utf-8"><title>GitHub connection failed</title></head>
    <body style="font-family:sans-serif;max-width:480px;margin:2rem auto;padding:1rem;">
    <h2>GitHub connection failed</h2>
    <p>{msg_escaped}</p>
    <p><a href="{url}">Back to Settings</a></p>
    </body></html>
    """
    return HTMLResponse(content=html_content, status_code=200)


@router.get("/oauth/start")
async def oauth_start(
    request: Request,
    redirect_base: Optional[str] = None,
    scope: Optional[str] = None,
    _user: Dict[str, Any] = Depends(get_current_user_or_local_admin),
):
    """
    Start GitHub OAuth flow. Returns authorization URL and state.
    scope: 'read_only' for public_repo only; otherwise full repo access.
    """
    if not is_github_oauth_configured():
        raise HTTPException(
            status_code=400,
            detail="GitHub OAuth is not configured. An admin must set Client ID in Settings → Connections.",
        )
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = get_github_callback_redirect_uri(base_url)
    scope_param = SCOPE_READ_ONLY if (scope or "").strip().lower() == "read_only" else SCOPE_FULL
    try:
        auth_url, state = get_authorization_url(
            redirect_uri,
            redirect_base=redirect_base,
            scope=scope_param,
            username=_user.get("username"),
            user_scope_id=_user.get("user_scope_id"),
        )
        return {"authorization_url": auth_url, "state": state, "redirect_uri": redirect_uri}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/device/start")
async def start_device(
    scope: Optional[str] = None,
    _user: Dict[str, Any] = Depends(get_current_user_or_local_admin),
):
    """Start GitHub Device Flow. Returns device_code, user_code, verification_uri."""
    if not is_github_oauth_configured():
        raise HTTPException(
            status_code=400,
            detail="GitHub OAuth is not configured. An admin must set Client ID in Settings → Connections.",
        )
    scope_param = SCOPE_READ_ONLY if (scope or "").strip().lower() == "read_only" else SCOPE_FULL
    try:
        return start_github_device_flow(scope_param)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/device/poll")
async def poll_device(
    body: DeviceFlowPollBody,
    _user: Dict[str, Any] = Depends(get_current_user_or_local_admin),
):
    """Poll GitHub for access token using device_code."""
    username = _user.get("username", "admin")
    user_scope_id = _user.get("user_scope_id")
    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
    cred_username = username if username.strip().lower() != local_admin else None
    cred_scope = user_scope_id if user_scope_id and str(user_scope_id).strip() != str(Config.get("local_admin_scope_id", "")).strip() else None

    try:
        res = poll_github_device_token(body.device_code, username=cred_username, user_scope_id=cred_scope)
        if res.get("status") == "success":
            account_id = res.get("account_id")
            gc = _get_github_config(username)
            accounts = list(gc.get("accounts") or [])
            found = False
            for acc in accounts:
                if acc.get("account_id") == account_id:
                    acc["login"] = account_id
                    acc["scopes"] = res.get("scope", "")
                    acc["allow_write"] = res.get("allow_write", False)
                    acc["enabled"] = True
                    found = True
                    break
            if not found:
                accounts.append({
                    "account_id": account_id,
                    "login": account_id,
                    "scopes": res.get("scope", ""),
                    "allow_write": res.get("allow_write", False),
                    "enabled": True,
                })
            gc["accounts"] = accounts
            _save_github_config(gc, username)
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
):
    """OAuth callback. Exchanges code for tokens, stores credentials, redirects to frontend."""
    redirect_base = get_state_redirect_base(state) if state else None
    if error:
        return _redirect_error(f"GitHub returned error: {error}", redirect_base)
    if not code or not state:
        return _redirect_error("Missing code or state", redirect_base)
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = get_github_callback_redirect_uri(base_url)
    try:
        # Prefer user from state (set at start) so we attribute to the right user after redirect
        state_username, state_scope = get_state_user(state)
        user = get_current_user_or_local_admin(request)
        username = state_username or user.get("username", "admin")
        user_scope_id = state_scope or user.get("user_scope_id")
        local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
        cred_username = username if username and username.strip().lower() != local_admin else None
        cred_scope = user_scope_id if user_scope_id and str(user_scope_id).strip() != str(Config.get("local_admin_scope_id", "")).strip() else None

        data = exchange_code_for_tokens(
            code, state, redirect_uri,
            username=cred_username,
            user_scope_id=cred_scope,
        )
        account_id = data.get("account_id", "")
        allow_write = data.get("allow_write", False)
        scope_str = data.get("scope", "")

        gc = _get_github_config(username)
        accounts = list(gc.get("accounts") or [])
        found = False
        for acc in accounts:
            if acc.get("account_id") == account_id:
                acc["login"] = account_id
                acc["scopes"] = scope_str
                acc["allow_write"] = allow_write
                acc["enabled"] = True
                found = True
                break
        if not found:
            accounts.append({
                "account_id": account_id,
                "login": account_id,
                "scopes": scope_str,
                "allow_write": allow_write,
                "enabled": True,
            })
        gc["accounts"] = accounts
        _save_github_config(gc, username)

        return _redirect_success(account_id, redirect_base)
    except ValueError as exc:
        logger.warning("GitHub OAuth callback error: %s", exc)
        return _redirect_error(str(exc), redirect_base)


@router.get("/accounts")
async def list_accounts(_user: Dict[str, Any] = Depends(get_current_user_or_local_admin)):
    """Return list of connected GitHub accounts for the current user (metadata only, no tokens)."""
    username = _user.get("username", "admin")
    gc = _get_github_config(username)
    accounts = gc.get("accounts") or []
    # Return safe fields only
    out = []
    for acc in accounts:
        out.append({
            "account_id": acc.get("account_id"),
            "login": acc.get("login"),
            "scopes": acc.get("scopes"),
            "allow_write": acc.get("allow_write", False),
            "enabled": acc.get("enabled", True),
        })
    return {"accounts": out}


@router.get("/status")
async def github_status(_user: Dict[str, Any] = Depends(get_current_user_or_local_admin)):
    """Return whether GitHub is connected and OAuth is configured (for UI)."""
    username = _user.get("username", "admin")
    gc = _get_github_config(username)
    accounts = gc.get("accounts") or []
    connected = len(accounts) > 0 and any(acc.get("enabled", True) for acc in accounts)
    return {
        "oauth_configured": is_github_oauth_configured(),
        "connected": connected,
        "accounts": [
            {"account_id": a.get("account_id"), "login": a.get("login"), "allow_write": a.get("allow_write", False)}
            for a in accounts
        ],
    }


@router.post("/connect-token")
async def connect_with_token(
    body: ConnectTokenBody,
    _user: Dict[str, Any] = Depends(get_current_user_or_local_admin),
):
    """
    Connect a GitHub account using a Personal Access Token (PAT).
    Validates the token with GitHub API, stores it, and adds the account to config.
    """
    token = (body.token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token is required.")
    try:
        account_id = validate_pat_and_get_login(token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    username = _user.get("username", "admin")
    user_scope_id = _user.get("user_scope_id")
    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
    cred_username = username if username.strip().lower() != local_admin else None
    cred_scope = user_scope_id if user_scope_id and str(user_scope_id).strip() != str(Config.get("local_admin_scope_id", "")).strip() else None

    set_github_oauth_tokens(
        account_id,
        token,
        refresh_token=None,
        username=cred_username,
        user_scope_id=cred_scope,
    )

    gc = _get_github_config(username)
    accounts = list(gc.get("accounts") or [])
    found = False
    for acc in accounts:
        if acc.get("account_id") == account_id:
            acc["login"] = account_id
            acc["scopes"] = ""  # PAT scopes not returned by /user
            acc["allow_write"] = True  # PAT typically has repo access
            acc["enabled"] = True
            found = True
            break
    if not found:
        accounts.append({
            "account_id": account_id,
            "login": account_id,
            "scopes": "",
            "allow_write": True,
            "enabled": True,
        })
    gc["accounts"] = accounts
    _save_github_config(gc, username)
    logger.info("GitHub account connected via PAT: %s for user %s", account_id, username)
    return {"account_id": account_id, "login": account_id}


@router.delete("/accounts/{account_id}")
async def disconnect_account(
    request: Request,
    account_id: str,
    _user: Dict[str, Any] = Depends(get_current_user_or_local_admin),
):
    """Disconnect a GitHub account: remove from config and delete stored credentials."""
    username = _user.get("username", "admin")
    user_scope_id = _user.get("user_scope_id")
    gc = _get_github_config(username)
    accounts = gc.get("accounts") or []
    remaining = [a for a in accounts if a.get("account_id") != account_id]
    if len(remaining) == len(accounts):
        raise HTTPException(status_code=404, detail="Account not found")

    cred_username = username if username.strip().lower() != (Config.get("local_admin_username") or "admin").strip().lower() else None
    cred_scope = user_scope_id if user_scope_id and str(user_scope_id).strip() != str(Config.get("local_admin_scope_id", "")).strip() else None
    try:
        delete_github_credentials(account_id, username=cred_username, user_scope_id=cred_scope)
    except Exception as exc:
        logger.warning("Failed to delete GitHub credentials for %s: %s", account_id, exc)

    gc["accounts"] = remaining
    _save_github_config(gc, username)
    logger.info("GitHub account disconnected: %s for user %s", account_id, username)
    return {"ok": True}


@router.patch("/accounts/{account_id}/permissions")
async def update_permissions(
    account_id: str,
    body: PermissionUpdateBody,
    _user: Dict[str, Any] = Depends(get_current_user_or_local_admin),
):
    """Toggle allow_write for a GitHub account."""
    username = _user.get("username", "admin")
    gc = _get_github_config(username)
    accounts = list(gc.get("accounts") or [])
    
    found = False
    for acc in accounts:
        if acc.get("account_id") == account_id:
            acc["allow_write"] = body.allow_write
            found = True
            break
            
    if not found:
        raise HTTPException(status_code=404, detail="Account not found")
        
    gc["accounts"] = accounts
    _save_github_config(gc, username)
    return {"ok": True, "allow_write": body.allow_write}


@router.get("/activity")
async def get_activity(
    limit: int = 50,
    _user: Dict[str, Any] = Depends(get_current_user_or_local_admin),
):
    """Return recent GitHub activity for the current user."""
    username = _user.get("username", "admin")
    activity = get_github_activity(username, limit=limit)
    return {"activity": activity}


@router.get("/repos")
async def get_repos(
    account_id: str,
    per_page: int = 30,
    _user: Dict[str, Any] = Depends(get_current_user_or_local_admin),
):
    """Return repositories for a connected GitHub account. account_id must belong to the current user."""
    username = _user.get("username", "admin")
    user_scope_id = _user.get("user_scope_id")
    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
    cred_username = username if (username or "").strip().lower() != local_admin else None
    cred_scope = user_scope_id if user_scope_id and str(user_scope_id).strip() != str(Config.get("local_admin_scope_id", "")).strip() else None

    gc = _get_github_config(username)
    accounts = list(gc.get("accounts") or [])
    if not any(acc.get("account_id") == account_id for acc in accounts):
        raise HTTPException(status_code=404, detail="Account not found")
    token = get_github_oauth_token(account_id, username=cred_username, user_scope_id=cred_scope)
    if not token:
        raise HTTPException(status_code=403, detail="No token for this account")
    try:
        from github import Auth, Github
        g = Github(auth=Auth.Token(token))
        user = g.get_user()
        repos = user.get_repos(sort="updated", type="all")
        per_page = min(100, max(1, per_page))
        out = []
        for i, repo in enumerate(repos):
            if i >= per_page:
                break
            out.append({
                "name": repo.name,
                "full_name": repo.full_name,
                "description": repo.description or "",
                "private": repo.private,
                "stargazers_count": repo.stargazers_count,
                "html_url": repo.html_url,
                "updated_at": repo.updated_at.isoformat() if repo.updated_at else None,
            })
        return {"repos": out, "account_id": account_id}
    except Exception as e:
        logger.warning("GitHub repos fetch failed for %s: %s", account_id, e)
        raise HTTPException(status_code=502, detail="Failed to fetch repos from GitHub")
