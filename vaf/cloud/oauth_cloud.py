"""
OAuth2 Authorization Code Flow with PKCE for cloud storage providers.

Reuses the PKCE helper from vaf/core/oauth_pkce.py but maintains a separate
state file and provider map so email OAuth is not affected.
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
from vaf.core.oauth_pkce import _pkce_verifier_and_challenge
from vaf.core.platform import Platform

from vaf.cloud.credential_cloud import set_cloud_oauth_tokens

logger = logging.getLogger("vaf.cloud.oauth")

STATE_TTL_SECONDS = 600  # 10 minutes
TOKEN_EXPIRY_BUFFER = 60
_STATE_FILE: Optional[Path] = None

# ── Provider definitions ─────────────────────────────────────────────────

CLOUD_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "google_drive": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": [
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/userinfo.email",
        ],
        "client_id_key": "cloud_oauth_google_client_id",
        "client_secret_key": "cloud_oauth_google_client_secret",
    },
    "onedrive": {
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": [
            "https://graph.microsoft.com/Files.ReadWrite",
            "https://graph.microsoft.com/User.Read",
            "offline_access",
        ],
        "client_id_key": "cloud_oauth_microsoft_client_id",
        "client_secret_key": "cloud_oauth_microsoft_client_secret",
    },
    "dropbox": {
        "auth_url": "https://www.dropbox.com/oauth2/authorize",
        "token_url": "https://api.dropboxapi.com/oauth2/token",
        "scopes": [],  # Scopes defined in Dropbox app console
        "client_id_key": "cloud_oauth_dropbox_client_id",
        "client_secret_key": "cloud_oauth_dropbox_client_secret",
    },
}


# ── State persistence ────────────────────────────────────────────────────

def _state_path() -> Path:
    global _STATE_FILE
    if _STATE_FILE is None:
        _STATE_FILE = Platform.data_dir() / "cloud_oauth_state.json"
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


# ── Client credential resolution ────────────────────────────────────────

_ENV_CLOUD_KEYS: Dict[str, Dict[str, str]] = {
    "google_drive": {
        "client_id": "VAF_CLOUD_OAUTH_GOOGLE_CLIENT_ID",
        "client_secret": "VAF_CLOUD_OAUTH_GOOGLE_CLIENT_SECRET",
    },
    "onedrive": {
        "client_id": "VAF_CLOUD_OAUTH_MICROSOFT_CLIENT_ID",
        "client_secret": "VAF_CLOUD_OAUTH_MICROSOFT_CLIENT_SECRET",
    },
    "dropbox": {
        "client_id": "VAF_CLOUD_OAUTH_DROPBOX_CLIENT_ID",
        "client_secret": "VAF_CLOUD_OAUTH_DROPBOX_CLIENT_SECRET",
    },
}


def _get_client_credential(provider: str, key_kind: str) -> str:
    """Return client_id or client_secret. For Google Drive, prefer email OAuth config (shared app for Gmail+Drive)."""
    if provider not in CLOUD_PROVIDERS:
        return ""
    conf = CLOUD_PROVIDERS[provider]
    config_key = conf["client_id_key"] if key_kind == "client_id" else conf["client_secret_key"]
    env_map = _ENV_CLOUD_KEYS.get(provider, {})

    # Google Drive: prefer email OAuth client (same app for Gmail+Drive in UI; Mail works with user's app)
    if provider == "google_drive":
        email_key = "email_oauth_google_client_id" if key_kind == "client_id" else "email_oauth_google_client_secret"
        email_val = (Config.get(email_key) or "").strip()
        if email_val:
            return email_val
        env_email = {"client_id": "VAF_EMAIL_OAUTH_GOOGLE_CLIENT_ID", "client_secret": "VAF_EMAIL_OAUTH_GOOGLE_CLIENT_SECRET"}
        env_val = (os.environ.get(env_email.get(key_kind, "")) or "").strip()
        if env_val:
            return env_val

    # Cloud-specific config
    value = (Config.get(config_key) or "").strip()
    if value:
        return value
    default_value = (Config.DEFAULTS.get(config_key) or "").strip()
    if default_value:
        return default_value
    env_key = env_map.get(key_kind)
    if env_key:
        value = (os.environ.get(env_key) or "").strip()
        if value:
            return value
    return ""


def is_cloud_oauth_configured(provider: str) -> bool:
    """Return True if provider has client_id set."""
    client_id = _get_client_credential(provider, "client_id")
    return bool(client_id)


# ── Account ID resolution ───────────────────────────────────────────────

def _resolve_account_id(provider: str, access_token: str) -> str:
    """Resolve a stable account identifier (email) from the provider's userinfo endpoint."""
    if provider == "google_drive":
        try:
            r = requests.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if r.status_code == 200:
                email = (r.json().get("email") or "").strip().lower()
                if email:
                    return email
        except Exception:
            pass
    elif provider == "onedrive":
        try:
            r = requests.get(
                "https://graph.microsoft.com/v1.0/me",
                params={"$select": "mail,userPrincipalName"},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if r.status_code == 200:
                info = r.json()
                email = (info.get("mail") or info.get("userPrincipalName") or "").strip().lower()
                if email:
                    return email
        except Exception:
            pass
    elif provider == "dropbox":
        try:
            r = requests.post(
                "https://api.dropboxapi.com/2/users/get_current_account",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if r.status_code == 200:
                email = (r.json().get("email") or "").strip().lower()
                if email:
                    return email
        except Exception:
            pass
    return f"cloud_{secrets.token_hex(4)}"


# ── Public API ───────────────────────────────────────────────────────────

def get_state_provider(state: str) -> Optional[str]:
    """Return provider for a state token, or None if invalid/expired."""
    states = _load_states()
    entry = states.get(state)
    if not entry:
        return None
    if (entry.get("created_at") or 0) + STATE_TTL_SECONDS < time.time():
        return None
    return entry.get("provider")


def get_state_redirect_base(state: str) -> Optional[str]:
    """Return redirect_base from state (frontend origin for post-OAuth redirect). Call before exchange_code_for_tokens."""
    states = _load_states()
    entry = states.get(state)
    if not entry:
        return None
    return entry.get("redirect_base")


def get_state_username(state: str) -> Optional[str]:
    """Return initiating username from state, or None."""
    states = _load_states()
    entry = states.get(state)
    if not entry:
        return None
    username = (entry.get("username") or "").strip()
    return username or None


def get_state_user_scope_id(state: str) -> Optional[str]:
    """Return initiating user_scope_id from state, or None."""
    states = _load_states()
    entry = states.get(state)
    if not entry:
        return None
    scope = str(entry.get("user_scope_id") or "").strip()
    return scope or None


def get_authorization_url(
    provider: str,
    redirect_uri: str,
    redirect_base: Optional[str] = None,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Tuple[str, str]:
    """Build OAuth URL with PKCE. Returns (auth_url, state). redirect_base = frontend origin (e.g. http://localhost:3000) for post-OAuth redirect."""
    if provider not in CLOUD_PROVIDERS:
        raise ValueError(f"Unknown cloud provider: {provider}")
    conf = CLOUD_PROVIDERS[provider]
    client_id = _get_client_credential(provider, "client_id")
    if not client_id:
        raise ValueError(f"OAuth client ID not configured for {provider}. Add it in Settings → Cloud Storage.")
    verifier, challenge = _pkce_verifier_and_challenge()
    state = secrets.token_urlsafe(24)
    params: Dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if conf["scopes"]:
        params["scope"] = " ".join(conf["scopes"])
    if provider in ("google_drive",):
        params["access_type"] = "offline"
        # Do NOT use prompt=consent – it forces the consent screen every time, causing a login loop.
        # Google shows consent on first auth automatically; we get refresh_token with access_type=offline.
    if provider == "onedrive":
        params["response_mode"] = "query"
    if provider == "dropbox":
        params["token_access_type"] = "offline"
    auth_url = conf["auth_url"] + "?" + urlencode(params)
    states = _load_states()
    states[state] = {
        "provider": provider,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "redirect_base": (redirect_base or "").strip() or None,
        "username": (username or "").strip() or None,
        "user_scope_id": str(user_scope_id or "").strip() or None,
        "created_at": time.time(),
    }
    _save_states(states)
    return auth_url, state


def exchange_code_for_tokens(
    provider: str,
    code: str,
    state: str,
    redirect_uri: str,
    username: Optional[str] = None,
) -> Dict[str, Any]:
    """Exchange auth code for tokens, store in credential_cloud, return result dict."""
    states = _load_states()
    entry = states.pop(state, None)
    _save_states(states)
    if not entry or entry.get("provider") != provider:
        raise ValueError("Invalid or expired state. Please try again.")
    code_verifier = entry.get("code_verifier")
    if not code_verifier:
        raise ValueError("Missing code_verifier.")
    username_from_state = (entry.get("username") or "").strip() or None
    effective_username = username if username is not None else username_from_state
    exchange_redirect_uri = entry.get("redirect_uri") or redirect_uri
    if provider not in CLOUD_PROVIDERS:
        raise ValueError(f"Unknown cloud provider: {provider}")
    conf = CLOUD_PROVIDERS[provider]
    client_id = _get_client_credential(provider, "client_id")
    client_secret = _get_client_credential(provider, "client_secret")
    payload: Dict[str, str] = {
        "client_id": client_id,
        "code": code,
        "redirect_uri": exchange_redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    elif provider == "google_drive":
        payload["client_secret"] = ""
    resp = requests.post(
        conf["token_url"],
        data=payload,
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
    refresh = data.get("refresh_token", "")
    expires_in = data.get("expires_in")
    expires_at = time.time() + int(expires_in) if expires_in else None
    account_id = _resolve_account_id(provider, access)
    set_cloud_oauth_tokens(account_id, provider, access, refresh, expires_at, effective_username)
    return {**data, "account_id": account_id, "provider": provider, "username": effective_username}


def _cred_username_for_store(username: Optional[str]) -> Optional[str]:
    """Return credential-store username: None for local admin, else the username."""
    if not username or not str(username).strip():
        return None
    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
    if username.strip().lower() == local_admin:
        return None
    return username.strip()


def get_valid_access_token(account_id: str, provider: str, username: Optional[str] = None) -> Optional[str]:
    """Return a valid access token, refreshing if expired. Returns None on failure."""
    from vaf.cloud.credential_cloud import get_cloud_credentials

    cred_user = _cred_username_for_store(username)
    creds = get_cloud_credentials(account_id, provider, username)
    if not creds:
        logger.warning(
            "Cloud OAuth: no credentials for account_id=%s provider=%s username=%s cred_user=%s",
            account_id[:20] + "..." if len(account_id) > 20 else account_id,
            provider,
            username,
            cred_user,
        )
        return None
    if creds.get("type") != "oauth":
        logger.warning("Cloud OAuth: wrong cred type %s for account_id=%s", creds.get("type"), account_id[:20])
        return None
    access = creds.get("access_token")
    refresh = creds.get("refresh_token")
    expires_at = creds.get("expires_at")
    now = time.time()
    if access and (expires_at is None or now + TOKEN_EXPIRY_BUFFER < expires_at):
        return access
    if not refresh or provider not in CLOUD_PROVIDERS:
        return access
    conf = CLOUD_PROVIDERS[provider]
    client_id = _get_client_credential(provider, "client_id")
    client_secret = _get_client_credential(provider, "client_secret")
    payload: Dict[str, str] = {
        "client_id": client_id,
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }
    if client_secret:
        payload["client_secret"] = client_secret
    elif provider == "google_drive":
        payload["client_secret"] = ""
    try:
        resp = requests.post(
            conf["token_url"],
            data=payload,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if resp.status_code != 200:
            logger.warning("Cloud token refresh failed for %s: %s", provider, resp.text[:200])
            return access
        data = resp.json()
        new_access = data.get("access_token")
        if not new_access:
            return access
        expires_in = data.get("expires_in")
        new_expires_at = time.time() + int(expires_in) if expires_in else None
        # Honor refresh-token rotation (Microsoft / Google with rotation): persist the
        # new refresh_token when present, otherwise keep the existing one.
        new_refresh = data.get("refresh_token") or refresh
        set_cloud_oauth_tokens(account_id, provider, new_access, new_refresh, new_expires_at, cred_user)
        return new_access
    except Exception as e:
        logger.warning("Cloud token refresh error for %s: %s", provider, e)
        return access


def get_cloud_callback_redirect_uri(request_base_url: str) -> str:
    """Build redirect_uri for cloud OAuth callback."""
    base = (Config.get("cloud_oauth_callback_base_url") or request_base_url).rstrip("/")
    return f"{base}/api/cloud/oauth/callback"
