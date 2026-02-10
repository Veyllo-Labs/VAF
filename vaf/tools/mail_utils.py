"""Shared helpers for mail tools (multi-user scoping)."""

from typing import List, Optional

from vaf.core.config import Config


def _local_admin() -> str:
    return (Config.get("local_admin_username") or "admin").strip().lower()


def store_username_from_kwargs(kwargs: dict) -> str:
    """Current user for store ('' for local admin). Injected by agent in network mode."""
    u = (kwargs.get("username") or "").strip()
    return "" if u.lower() == _local_admin() else u


def cred_username_from_kwargs(kwargs: dict) -> Optional[str]:
    """Current user for credentials/transport (None for local admin)."""
    u = (kwargs.get("username") or "").strip()
    return None if u.lower() == _local_admin() else u if u else None


def list_accounts_for_user(cred_username: Optional[str] = None) -> List[str]:
    """Connected email accounts for this user (multi-user safe)."""
    if cred_username is None:
        ec = Config.get("email_config") or {}
    else:
        by_user = Config.get("email_config_by_user") or {}
        ec = by_user.get(cred_username, {}) if isinstance(by_user, dict) else {}
    accounts = ec.get("accounts") or []
    return [a.get("email") or a.get("account_id") for a in accounts if a.get("email") or a.get("account_id")]
