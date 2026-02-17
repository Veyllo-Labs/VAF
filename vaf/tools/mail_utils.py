"""Shared helpers for mail tools (multi-user scoping)."""

from typing import List, Optional, Tuple

from vaf.core.config import Config


def _local_admin() -> str:
    return (Config.get("local_admin_username") or "admin").strip().lower()


def _has_per_user_email_config(username: str) -> bool:
    """Return True if *username* has its own entry in ``email_config_by_user``."""
    by_user = Config.get("email_config_by_user") or {}
    if not isinstance(by_user, dict):
        return False
    ec = by_user.get(username, {})
    return bool(isinstance(ec, dict) and ec.get("accounts"))


def store_username_from_kwargs(kwargs: dict) -> str:
    """Current user for store ('' for local admin). Injected by agent in network mode."""
    u = (kwargs.get("username") or "").strip()
    if not u or u.lower() == _local_admin():
        return ""
    # If this user has no per-user email config, their accounts live in the
    # legacy location → the sync store also lives in the legacy (admin) DB.
    if not _has_per_user_email_config(u):
        return ""
    return u


def cred_username_from_kwargs(kwargs: dict) -> Optional[str]:
    """Current user for credentials/transport (None for local admin)."""
    u = (kwargs.get("username") or "").strip()
    return None if u.lower() == _local_admin() else u if u else None


def list_accounts_for_user(cred_username: Optional[str] = None) -> List[str]:
    """Connected email accounts for this user (multi-user safe)."""
    items = list_accounts_with_labels_for_user(cred_username)
    return [x["email"] for x in items]


def list_accounts_with_labels_for_user(cred_username: Optional[str] = None) -> List[dict]:
    """Connected email accounts with optional labels (multi-user safe). Returns [{"email": str, "label": str}].

    Lookup order:
    - cred_username is None  → legacy ``email_config``
    - cred_username is set   → ``email_config_by_user[cred_username]``,
      with automatic fallback to legacy ``email_config`` when the per-user
      bucket is empty (covers single-user setups where accounts live in
      the legacy location regardless of the active username).
    """
    if cred_username is None:
        ec = Config.get("email_config") or {}
    else:
        by_user = Config.get("email_config_by_user") or {}
        ec = by_user.get(cred_username, {}) if isinstance(by_user, dict) else {}
        # Fallback: per-user bucket empty → check legacy config
        if not (ec.get("accounts") if isinstance(ec, dict) else None):
            ec = Config.get("email_config") or {}
    accounts = ec.get("accounts") or []
    return [
        {
            "email": a.get("email") or a.get("account_id"),
            "label": (a.get("label") or "").strip(),
        }
        for a in accounts
        if a.get("email") or a.get("account_id")
    ]
