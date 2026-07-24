# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Route-independent SSOT for per-user email account CONFIG (metadata only;
credentials live in credential_store).

Why this module exists (mail v2-only port, Phase 1): the account-config reader
used to live in vaf/api/email_routes.py and a drifting near-duplicate in
vaf/core/email_transport.py. Both the v2 mail engine (mail_routes/supervisor),
the calendar client and the label_mail tool imported them, which put a FastAPI
route module on everyone's import path and blocked the eventual legacy teardown.
This module is the single home; the old private names (`_get_email_config` /
`_save_email_config`) are re-exported from their historical locations so existing
importers keep working, and a guard test pins them to the same objects.

Scope isolation (unchanged behavior, carried over verbatim from the email_routes
copy): a user_scope_id resolves email_config_by_scope FIRST and never falls back
across scopes; the local admin uses the legacy email_config blob; a non-admin
username without a scope resolves email_config_by_user.
"""
from typing import Any, Dict, List, Optional

from vaf.core.config import (
    Config,
    get_local_admin_scope_id,
    get_local_admin_username,
)


def get_email_config(
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the email config for the given user. When username is None or the
    local admin, use the legacy email_config. If user_scope_id is set,
    email_config_by_scope is tried first; otherwise a username-based lookup."""
    local_admin_scope = get_local_admin_scope_id()
    if user_scope_id:
        by_scope = Config.get("email_config_by_scope") or {}
        if isinstance(by_scope, dict):
            ec = by_scope.get(str(user_scope_id).strip())
            if isinstance(ec, dict) and ec.get("accounts") is not None:
                return ec
        if str(user_scope_id).strip() == str(local_admin_scope).strip():
            raw = Config.get("email_config")
            if isinstance(raw, dict):
                return raw
            return {"accounts": []}
    local_admin = get_local_admin_username().lower()
    if not username or username.strip().lower() == local_admin:
        raw = Config.get("email_config")
        if isinstance(raw, dict):
            return raw
        return {"accounts": []}
    by_user = Config.get("email_config_by_user") or {}
    ec = by_user.get(username.strip(), {}) if isinstance(by_user, dict) else {}
    return ec if isinstance(ec, dict) else {"accounts": []}


def save_email_config(
    ec: Dict[str, Any],
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> None:
    """Persist the email config for the given user. When username is None or the
    local admin, write the legacy email_config. If user_scope_id is set, write to
    email_config_by_scope; otherwise a username-based write."""
    config = Config.load()
    local_admin_scope = get_local_admin_scope_id()
    if user_scope_id and str(user_scope_id).strip() != str(local_admin_scope).strip():
        by_scope = config.get("email_config_by_scope") or {}
        if not isinstance(by_scope, dict):
            by_scope = {}
        by_scope[str(user_scope_id).strip()] = ec
        config["email_config_by_scope"] = by_scope
        Config.save(config)
        return
    local_admin = get_local_admin_username().lower()
    if not username or username.strip().lower() == local_admin:
        config["email_config"] = ec
    else:
        by_user = config.get("email_config_by_user") or {}
        if not isinstance(by_user, dict):
            by_user = {}
        by_user[username.strip()] = ec
        config["email_config_by_user"] = by_user
    Config.save(config)


# ── account lookup + sender-category rules (relocated from email_transport, P3.1) ──
# These read only the account-config blob this module already owns, so the agent
# tools and the sync path can resolve accounts/rules without importing the heavy
# email_transport module. email_transport re-exports them under their old names.

def _email_config_candidates(
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> List[tuple]:
    """The current user's config candidate only (no cross-scope fallback)."""
    ec = get_email_config(username, user_scope_id=user_scope_id)
    if isinstance(ec, dict) and (ec.get("accounts") or []):
        return [(ec, user_scope_id)]
    return []


def get_account(
    account_id: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return account metadata for account_id (email or account_id). Optional
    username/user_scope_id for multi-user scope."""
    want = (account_id or "").strip().lower()
    for ec, _ in _email_config_candidates(username, user_scope_id):
        for a in ec.get("accounts") or []:
            if (a.get("account_id") or a.get("email") or "").strip().lower() == want:
                return a
    return None


def get_sender_rules(
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Sender->category rules from config. Each: {"pattern": "twitch.tv",
    "category": "social"}. First match wins."""
    ec = get_email_config(username, user_scope_id=user_scope_id)
    rules = ec.get("sender_category_rules")
    if not isinstance(rules, list):
        return []
    out: List[Dict[str, str]] = []
    for r in rules:
        if isinstance(r, dict) and r.get("pattern") and r.get("category"):
            out.append({
                "pattern": str(r["pattern"]).strip(),
                "category": str(r["category"]).strip().lower().replace(" ", "_")[:64] or "primary",
            })
    return out


def apply_sender_rules_to_category(
    from_str: str,
    current_category: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> str:
    """If any rule's pattern is contained in from_str (case-insensitive), return
    that category; else current_category. Used on sync and on backfill."""
    rules = get_sender_rules(username, user_scope_id=user_scope_id)
    from_lower = (from_str or "").lower()
    for r in rules:
        pattern = (r.get("pattern") or "").lower()
        if pattern and pattern in from_lower:
            return r.get("category") or current_category
    return current_category
