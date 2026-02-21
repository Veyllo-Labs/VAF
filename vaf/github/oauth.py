"""
GitHub OAuth2 authorization code flow.

State is stored in a file under Platform.data_dir(). Tokens are stored via
credential_github (keyring or encrypted file). Redirect URI is localhost.
"""

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import requests

from vaf.core.config import Config
from vaf.core.platform import Platform

from vaf.github.credential_github import set_github_oauth_tokens

logger = logging.getLogger("vaf.github.oauth")

STATE_TTL_SECONDS = 600  # 10 minutes
_GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
_GITHUB_USER_URL = "https://api.github.com/user"
_STATE_FILE: Optional[Path] = None

# Scopes: repo = full repo access (read+write+private), public_repo = public only
# read:user, user:email for login identity
# Note: GitHub has no "read-only private repos" scope. We use "repo" for both
# modes so private repos are accessible; write-protection is enforced server-side
# via allow_write=False in the account config.
SCOPE_FULL = "read:user user:email repo"
SCOPE_READ_ONLY = "read:user user:email repo"


def _state_path() -> Path:
    global _STATE_FILE
    if _STATE_FILE is None:
        _STATE_FILE = Platform.data_dir() / "github_oauth_state.json"
    return _STATE_FILE


def _load_states() -> Dict[str, Dict[str, Any]]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        states = data.get("states") or {}
        now = time.time()
        return {k: v for k, v in states.items() if (v.get("created_at") or 0) + STATE_TTL_SECONDS > now}
    except Exception:
        return {}


def _save_states(states: Dict[str, Dict[str, Any]]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"states": states}, indent=2), encoding="utf-8")


def _get_client_id() -> str:
    """Return GitHub OAuth client ID from config or env."""
    value = (Config.get("github_oauth_client_id") or "").strip()
    if value:
        return value
    return (os.environ.get("VAF_GITHUB_OAUTH_CLIENT_ID") or "").strip()


def _get_client_secret() -> str:
    """Return GitHub OAuth client secret from config or env."""
    value = (Config.get("github_oauth_client_secret") or "").strip()
    if value:
        return value
    return (os.environ.get("VAF_GITHUB_OAUTH_CLIENT_SECRET") or "").strip()


def is_github_oauth_configured() -> bool:
    """Return True if GitHub OAuth client_id is set."""
    return bool(_get_client_id())


def get_github_callback_redirect_uri(request_base_url: str) -> str:
    """Build redirect_uri for GitHub OAuth callback."""
    base = (Config.get("email_oauth_callback_base_url") or request_base_url).rstrip("/")
    return f"{base}/api/github/oauth/callback"


def get_authorization_url(
    redirect_uri: str,
    redirect_base: Optional[str] = None,
    scope: str = SCOPE_FULL,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Build GitHub OAuth authorize URL. Returns (auth_url, state).
    redirect_base: frontend origin for post-OAuth redirect (e.g. http://localhost:3000).
    scope: SCOPE_FULL (repo) or SCOPE_READ_ONLY (public_repo).
    username/user_scope_id: stored in state so callback can attribute account to the right user.
    """
    client_id = _get_client_id()
    if not client_id:
        raise ValueError("GitHub OAuth client ID not configured. Add it in Settings → Connections → GitHub.")
    state = secrets.token_urlsafe(24)
    params: Dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
    }
    auth_url = _GITHUB_AUTH_URL + "?" + urlencode(params)
    states = _load_states()
    states[state] = {
        "redirect_uri": redirect_uri,
        "redirect_base": (redirect_base or "").strip() or None,
        "scope": scope,
        "username": (username or "").strip() or None,
        "user_scope_id": str(user_scope_id).strip() if user_scope_id else None,
        "created_at": time.time(),
    }
    _save_states(states)
    return auth_url, state


def get_state_user(state: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (username, user_scope_id) from state for callback. (None, None) if missing/invalid."""
    states = _load_states()
    entry = states.get(state)
    if not entry:
        return None, None
    return entry.get("username"), entry.get("user_scope_id")


def get_state_redirect_base(state: str) -> Optional[str]:
    """Return redirect_base from state for post-OAuth redirect. Call before exchange_code_for_tokens."""
    states = _load_states()
    entry = states.get(state)
    if not entry:
        return None
    return entry.get("redirect_base")


def _resolve_login(access_token: str) -> str:
    """Get GitHub login (username) from API for use as account_id."""
    try:
        r = requests.get(
            _GITHUB_USER_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10,
        )
        if r.status_code == 200:
            login = (r.json().get("login") or "").strip().lower()
            if login:
                return login
    except Exception as e:
        logger.debug("GitHub user fetch failed: %s", e)
    return "github_" + secrets.token_hex(4)


def validate_pat_and_get_login(token: str) -> str:
    """
    Validate a Personal Access Token with GitHub API and return the account login.
    Raises ValueError if the token is invalid or the user cannot be resolved.
    """
    t = (token or "").strip()
    if not t:
        raise ValueError("Token is required.")
    try:
        r = requests.get(
            _GITHUB_USER_URL,
            headers={
                "Authorization": f"Bearer {t}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10,
        )
        if r.status_code == 401:
            raise ValueError("Invalid token or token expired.")
        if r.status_code != 200:
            raise ValueError(f"GitHub API error: {r.status_code}")
        login = (r.json().get("login") or "").strip().lower()
        if not login:
            raise ValueError("Could not read GitHub username from token.")
        return login
    except ValueError:
        raise
    except Exception as e:
        logger.debug("GitHub PAT validation failed: %s", e)
        raise ValueError("Could not verify token with GitHub.") from e


def exchange_code_for_tokens(
    code: str,
    state: str,
    redirect_uri: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Exchange authorization code for access token, store in credential_github, return result dict.
    Returns dict with account_id (GitHub login), access_token, scope, allow_write (derived from scope).
    """
    states = _load_states()
    entry = states.pop(state, None)
    _save_states(states)
    if not entry:
        raise ValueError("Invalid or expired state. Please try again.")
    exchange_redirect_uri = entry.get("redirect_uri") or redirect_uri
    client_id = _get_client_id()
    client_secret = _get_client_secret()
    if not client_id:
        raise ValueError("GitHub OAuth client ID not configured.")
    resp = requests.post(
        _GITHUB_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": exchange_redirect_uri,
        },
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        try:
            err = resp.json()
            msg = err.get("error_description") or err.get("error") or resp.text[:200]
        except Exception:
            msg = resp.text[:200]
        raise ValueError(f"Token exchange failed: {msg}")
    data = resp.json()
    access = data.get("access_token")
    if not access:
        raise ValueError("No access_token in response")
    scope_str = data.get("scope") or entry.get("scope") or SCOPE_FULL
    account_id = _resolve_login(access)
    allow_write = "repo" in (scope_str or "").split()
    set_github_oauth_tokens(
        account_id,
        access,
        refresh_token=None,
        username=username,
        user_scope_id=user_scope_id,
    )
    return {
        "account_id": account_id,
        "access_token": access,
        "scope": scope_str,
        "allow_write": allow_write,
    }


def start_github_device_flow(scope: str = SCOPE_FULL) -> Dict[str, Any]:
    """
    Start GitHub Device Flow (OAuth 2.0 Device Authorization Grant).
    Returns dict with device_code, user_code, verification_uri, expires_in, interval.
    """
    client_id = _get_client_id()
    if not client_id:
        raise ValueError("GitHub OAuth client ID not configured. Device Flow requires a Client ID.")

    resp = requests.post(
        _GITHUB_DEVICE_CODE_URL,
        data={"client_id": client_id, "scope": scope},
        headers={"Accept": "application/json"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise ValueError(f"GitHub Device Flow failed: {resp.text}")
    return resp.json()


def poll_github_device_token(
    device_code: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Poll GitHub for access token using device_code.
    Returns dict with access_token or raises error if still pending, expired, or denied.
    Common errors: 'authorization_pending', 'slow_down', 'expired_token', 'access_denied'.
    """
    client_id = _get_client_id()
    resp = requests.post(
        _GITHUB_TOKEN_URL,
        data={
            "client_id": client_id,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
        headers={"Accept": "application/json"},
        timeout=15,
    )
    data = resp.json()
    error = data.get("error")
    if error:
        return {"status": "error", "error": error, "error_description": data.get("error_description")}

    access = data.get("access_token")
    if not access:
        raise ValueError("No access_token in response")

    scope_str = data.get("scope") or ""
    account_id = _resolve_login(access)
    allow_write = "repo" in (scope_str or "").split()

    set_github_oauth_tokens(
        account_id,
        access,
        refresh_token=None,
        username=username,
        user_scope_id=user_scope_id,
    )

    return {
        "status": "success",
        "account_id": account_id,
        "access_token": access,
        "scope": scope_str,
        "allow_write": allow_write,
    }
