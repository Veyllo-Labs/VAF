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
    """Return (store_username, user_scope_id) candidates for the email sync store, in try order.

    Used so the tool can read from the same DB as the Mail Dashboard when the primary
    (from WebSocket/task metadata) is empty but accounts/messages live in legacy or a single scope.
    """
    from vaf.core.config import get_local_admin_scope_id

    local_admin_scope = get_local_admin_scope_id()
    candidates: List[Tuple[str, Optional[str]]] = []
    seen: set = set()

    def add(su: str, sid: Optional[str]) -> None:
        key = (su or "", (sid or "") or "")
        if key not in seen:
            seen.add(key)
            candidates.append((su, sid))

    add(store_username or "", user_scope_id)
    add("", local_admin_scope)
    by_scope = Config.get("email_config_by_scope") or {}
    if isinstance(by_scope, dict):
        scopes_with_accounts = [
            sid for sid, ec in by_scope.items()
            if isinstance(ec, dict) and (ec.get("accounts") or [])
        ]
        if len(scopes_with_accounts) == 1:
            add("", scopes_with_accounts[0])
    return candidates


def list_accounts_with_labels_for_user(
    cred_username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> List[dict]:
    """Connected email accounts with optional labels (multi-user safe). Returns [{"email": str, "label": str}].

    Lookup order when user_scope_id is set:
    - email_config_by_scope[user_scope_id], then legacy email_config for local admin scope.
    Otherwise:
    - cred_username is None  → legacy ``email_config``
    - cred_username is set   → ``email_config_by_user[cred_username]``,
      with automatic fallback to legacy ``email_config`` when the per-user
      bucket is empty (covers single-user setups where accounts live in
      the legacy location regardless of the active username).
    """
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
        # Fallback: per-user bucket empty → check legacy config
        if not (ec.get("accounts") if isinstance(ec, dict) else None):
            ec = Config.get("email_config") or {}
    accounts = ec.get("accounts") or []
    result = [
        {
            "email": a.get("email") or a.get("account_id"),
            "label": (a.get("label") or "").strip(),
        }
        for a in accounts
        if a.get("email") or a.get("account_id")
    ]

    # Fallback so the tool sees the same accounts as the Mail Dashboard (Settings → Connections → Email).
    # If the primary lookup (by scope or username) found nothing, try legacy email_config and, in
    # single-scope setups, the only scope in email_config_by_scope that has accounts (e.g. when
    # the Dashboard saved under JWT scope but the chat WebSocket was connected as local admin).
    if not result:
        legacy = Config.get("email_config") or {}
        legacy_accounts = (legacy.get("accounts") or []) if isinstance(legacy, dict) else []
        if legacy_accounts:
            result = [
                {"email": a.get("email") or a.get("account_id"), "label": (a.get("label") or "").strip()}
                for a in legacy_accounts
                if a.get("email") or a.get("account_id")
            ]
    if not result:
        by_scope = Config.get("email_config_by_scope") or {}
        if isinstance(by_scope, dict):
            scopes_with_accounts = [
                (sid, (ec or {}).get("accounts") or [])
                for sid, ec in by_scope.items()
                if isinstance(ec, dict) and (ec.get("accounts") or [])
            ]
            # Single scope with accounts → use it so Dashboard and tool stay in sync
            if len(scopes_with_accounts) == 1:
                _, scope_accounts = scopes_with_accounts[0]
                result = [
                    {"email": a.get("email") or a.get("account_id"), "label": (a.get("label") or "").strip()}
                    for a in scope_accounts
                    if a.get("email") or a.get("account_id")
                ]
    return result
