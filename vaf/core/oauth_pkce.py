"""
OAuth2 Authorization Code Flow with PKCE for email providers (Google, Microsoft, Apple).

State and code_verifier are stored temporarily (file under data_dir); tokens
are stored only in credential_store, never in config.
"""

import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import requests

from vaf.core.config import Config
from vaf.core.credential_store import set_email_oauth_tokens
from vaf.core.platform import Platform

logger = logging.getLogger("vaf.core.oauth_pkce")

STATE_TTL_SECONDS = 600  # 10 minutes
_STATE_FILE: Optional[Path] = None


def _state_path() -> Path:
    global _STATE_FILE
    if _STATE_FILE is None:
        _STATE_FILE = Platform.data_dir() / "email_oauth_state.json"
    return _STATE_FILE


def _load_states() -> Dict[str, Dict[str, Any]]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        states = data.get("states") or {}
        now = time.time()
        # Remove expired
        states = {k: v for k, v in states.items() if (v.get("created_at") or 0) + STATE_TTL_SECONDS > now}
        return states
    except Exception:
        return {}


def _save_states(states: Dict[str, Dict[str, Any]]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"states": states}, indent=2), encoding="utf-8")


def _pkce_verifier_and_challenge() -> Tuple[str, str]:
    """Return (code_verifier, code_challenge) for S256."""
    import base64
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


# Provider endpoints and scopes
def _get_account_id_from_tokens(provider: str, access_token: str, token_data: Dict[str, Any]) -> str:
    """Resolve stable account_id (email) from provider userinfo. Falls back to id_token sub or random."""
    if provider == "gmail":
        try:
            r = requests.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if r.status_code == 200:
                info = r.json()
                email = (info.get("email") or "").strip().lower()
                if email:
                    return email
        except Exception as e:
            logger.debug("Google userinfo failed: %s", e)
    if provider == "microsoft":
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
        except Exception as e:
            logger.debug("Microsoft me failed: %s", e)
    return "unknown_" + secrets.token_hex(4)


PROVIDERS: Dict[str, Dict[str, Any]] = {
    "gmail": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": ["https://mail.google.com/"],  # IMAP/SMTP access
        "client_id_key": "email_oauth_google_client_id",
        "client_secret_key": "email_oauth_google_client_secret",
    },
    "microsoft": {
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": [
            "https://outlook.office365.com/IMAP.AccessAsUser.All",
            "https://outlook.office365.com/SMTP.Send",
            "offline_access", "openid",
        ],
        "client_id_key": "email_oauth_microsoft_client_id",
        "client_secret_key": "email_oauth_microsoft_client_secret",
    },
    "apple": {
        "auth_url": "https://appleid.apple.com/auth/authorize",
        "token_url": "https://appleid.apple.com/auth/token",
        "scopes": ["email", "name"],
        "client_id_key": "email_oauth_apple_client_id",
        "client_secret_key": "email_oauth_apple_client_secret",
    },
}


def get_state_provider(state: str) -> Optional[str]:
    """Return provider for a given state, or None if invalid/expired. Does not consume state."""
    states = _load_states()
    entry = states.get(state)
    if not entry:
        return None
    if (entry.get("created_at") or 0) + STATE_TTL_SECONDS < time.time():
        return None
    return entry.get("provider")


def get_authorization_url(provider: str, redirect_uri: str) -> Tuple[str, str]:
    """
    Build OAuth authorization URL with PKCE and store state/code_verifier.
    Returns (authorization_url, state). Raises ValueError if provider or client_id missing.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}")
    conf = PROVIDERS[provider]
    client_id = (Config.get(conf["client_id_key"]) or "").strip()
    if not client_id:
        raise ValueError(f"OAuth client ID not configured for {provider}. Add it in Settings.")
    verifier, challenge = _pkce_verifier_and_challenge()
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(conf["scopes"]),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    if provider == "microsoft":
        params.setdefault("response_mode", "query")
    auth_url = conf["auth_url"] + "?" + urlencode(params)
    states = _load_states()
    states[state] = {
        "provider": provider,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "created_at": time.time(),
    }
    _save_states(states)
    return auth_url, state


def exchange_code_for_tokens(
    provider: str,
    code: str,
    state: str,
    redirect_uri: str,
) -> Dict[str, Any]:
    """
    Exchange authorization code for access/refresh tokens; store in credential_store.
    Returns token response dict (with access_token, refresh_token, expires_in).
    Invalidates state after use. Raises ValueError on invalid state or exchange error.
    """
    states = _load_states()
    entry = states.pop(state, None)
    _save_states(states)
    if not entry or entry.get("provider") != provider:
        raise ValueError("Invalid or expired state. Please start the login again.")
    code_verifier = entry.get("code_verifier")
    if not code_verifier:
        raise ValueError("Missing code_verifier for state.")
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}")
    conf = PROVIDERS[provider]
    client_id = (Config.get(conf["client_id_key"]) or "").strip()
    client_secret = (Config.get(conf["client_secret_key"]) or "").strip()
    token_url = conf["token_url"]
    payload = {
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    resp = requests.post(token_url, data=payload, headers=headers, timeout=30)
    if resp.status_code != 200:
        logger.warning("Token exchange failed: %s %s", resp.status_code, resp.text[:200])
        raise ValueError(f"Token exchange failed: {resp.status_code}")
    data = resp.json()
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    if not access:
        raise ValueError("No access_token in response")
    expires_in = data.get("expires_in")
    expires_at = time.time() + int(expires_in) if expires_in else None
    account_id = _get_account_id_from_tokens(provider, access, data)
    set_email_oauth_tokens(account_id, provider, access, refresh or "", expires_at)
    return {**data, "account_id": account_id}


def get_oauth_callback_redirect_uri(request_base_url: str) -> str:
    """Build redirect_uri for OAuth callback from request base URL."""
    base = request_base_url.rstrip("/")
    return f"{base}/api/email/oauth/callback"
