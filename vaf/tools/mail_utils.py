"""Shared helpers for mail tools (multi-user scoping)."""

from typing import List, Optional, Tuple

from vaf.core.config import Config, get_local_admin_scope_id, get_local_admin_username


def _local_admin() -> str:
    return get_local_admin_username().lower()


def store_scope_from_kwargs(kwargs: dict) -> Optional[str]:
    """Current user_scope_id for store (from agent/route). None if not set."""
    scope = kwargs.get("user_scope_id")
    if scope is None:
        return None
    s = str(scope).strip()
    return s if s else None


def cred_scope_from_kwargs(kwargs: dict) -> Optional[str]:
    """Current user_scope_id for credentials/transport (from agent/route). None if not set."""
    return store_scope_from_kwargs(kwargs)


def store_username_from_kwargs(kwargs: dict) -> str:
    """Current user for store ('' for local admin). Injected by agent in network mode."""
    u = (kwargs.get("username") or "").strip()
    if not u or u.lower() == _local_admin():
        return ""
    return u


def cred_username_from_kwargs(kwargs: dict) -> Optional[str]:
    """Current user for credentials/transport (None for local admin)."""
    u = (kwargs.get("username") or "").strip()
    return None if u.lower() == _local_admin() else u if u else None


def list_accounts_for_user(
    cred_username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> List[str]:
    """Connected email accounts for this user (multi-user safe)."""
    items = list_accounts_with_labels_for_user(cred_username=cred_username, user_scope_id=user_scope_id)
    return [x["email"] for x in items]


def store_candidates_for_mail(
    store_username: str,
    user_scope_id: Optional[str],
) -> List[Tuple[str, Optional[str]]]:
    """Return the single allowed mail-store candidate for this user scope."""
    return [(store_username or "", user_scope_id)]


def list_accounts_with_labels_for_user(
    cred_username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> List[dict]:
    """Connected email accounts with labels. Strict per-user isolation (no cross-user fallback)."""
    local_admin_scope = get_local_admin_scope_id()
    if user_scope_id:
        by_scope = Config.get("email_config_by_scope") or {}
        if isinstance(by_scope, dict):
            ec = by_scope.get(str(user_scope_id).strip(), {})
            if isinstance(ec, dict) and ec.get("accounts") is not None:
                accounts = ec.get("accounts") or []
                return [
                    {"email": a.get("email") or a.get("account_id"), "label": (a.get("label") or "").strip()}
                    for a in accounts
                    if a.get("email") or a.get("account_id")
                ]
        if str(user_scope_id).strip() == str(local_admin_scope).strip():
            ec = Config.get("email_config") or {}
        else:
            ec = {}
    elif cred_username is None:
        ec = Config.get("email_config") or {}
    else:
        by_user = Config.get("email_config_by_user") or {}
        ec = by_user.get(cred_username, {}) if isinstance(by_user, dict) else {}
    accounts = ec.get("accounts") or []
    return [
        {
            "email": a.get("email") or a.get("account_id"),
            "label": (a.get("label") or "").strip(),
        }
        for a in accounts
        if a.get("email") or a.get("account_id")
    ]
