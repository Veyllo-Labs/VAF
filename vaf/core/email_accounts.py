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


# ── IMAP/SMTP presets + connection probe (relocated from email_routes, P4.1) ──

IMAP_SMTP_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "gmail.com": {"imap_host": "imap.gmail.com", "imap_port": 993, "smtp_host": "smtp.gmail.com", "smtp_port": 587},
    "googlemail.com": {"imap_host": "imap.gmail.com", "imap_port": 993, "smtp_host": "smtp.gmail.com", "smtp_port": 587},
    "outlook.com": {"imap_host": "outlook.office365.com", "imap_port": 993, "smtp_host": "smtp.office365.com", "smtp_port": 587},
    "hotmail.com": {"imap_host": "outlook.office365.com", "imap_port": 993, "smtp_host": "smtp.office365.com", "smtp_port": 587},
    "live.com": {"imap_host": "outlook.office365.com", "imap_port": 993, "smtp_host": "smtp.office365.com", "smtp_port": 587},
    "yahoo.com": {"imap_host": "imap.mail.yahoo.com", "imap_port": 993, "smtp_host": "smtp.mail.yahoo.com", "smtp_port": 587},
    "icloud.com": {"imap_host": "imap.mail.me.com", "imap_port": 993, "smtp_host": "smtp.mail.me.com", "smtp_port": 587},
    "me.com": {"imap_host": "imap.mail.me.com", "imap_port": 993, "smtp_host": "smtp.mail.me.com", "smtp_port": 587},
    "outlook.de": {"imap_host": "outlook.office365.com", "imap_port": 993, "smtp_host": "smtp.office365.com", "smtp_port": 587},
    "gmx.de": {"imap_host": "imap.gmx.net", "imap_port": 993, "smtp_host": "mail.gmx.net", "smtp_port": 587},
    "gmx.net": {"imap_host": "imap.gmx.net", "imap_port": 993, "smtp_host": "mail.gmx.net", "smtp_port": 587},
    "web.de": {"imap_host": "imap.web.de", "imap_port": 993, "smtp_host": "smtp.web.de", "smtp_port": 587},
    "t-online.de": {"imap_host": "secureimap.t-online.de", "imap_port": 993, "smtp_host": "securesmtp.t-online.de", "smtp_port": 587},
}


def test_imap_login(email: str, password: str, imap_host: Optional[str] = None,
                    imap_port: Optional[int] = None) -> tuple:
    """Try an IMAP login with the given credentials; saves nothing. Returns
    (success, error_message, hint). hint is 2FA/app-password guidance."""
    import imaplib
    import ssl as _ssl

    from vaf.network.binding import assert_safe_remote_host
    email = (email or "").strip().lower()
    domain = email.split("@")[-1] if "@" in email else ""
    defaults = IMAP_SMTP_DEFAULTS.get(domain, {})
    host = (imap_host or "").strip() or defaults.get("imap_host", "imap.gmail.com")
    port = imap_port if imap_port is not None else defaults.get("imap_port", 993)
    hint = None
    if domain in ("gmail.com", "googlemail.com"):
        hint = "Gmail with 2FA requires an App Password. Create one at: https://myaccount.google.com/apppasswords"
    elif domain in ("outlook.com", "hotmail.com", "live.com", "live.de", "msn.com", "outlook.de", "office365.com"):
        hint = ("Outlook.com no longer supports IMAP with app passwords (Microsoft retired Basic auth in 2024). "
                "Use 'Sign in with Microsoft' instead - an admin must configure the OAuth client first.")
    try:
        assert_safe_remote_host(host, allow_private=bool(Config.get("email_allow_private_hosts", False)))
    except ValueError as e:
        return False, str(e), hint
    try:
        conn = imaplib.IMAP4_SSL(host, port=port, ssl_context=_ssl.create_default_context(), timeout=30)
        conn.login(email, password)
        conn.noop()
        conn.logout()
        return True, "", None
    except imaplib.IMAP4.error as e:
        return False, (str(e).strip() or "IMAP login failed"), hint
    except Exception as e:
        return False, (str(e).strip() or "Connection failed"), hint


# ── account-config CRUD (P4.1): the /api/mail account endpoints build on these ──

def _acct_key(a: Dict[str, Any]) -> str:
    return (a.get("account_id") or a.get("email") or "").strip().lower()


def add_account(entry: Dict[str, Any], username: Optional[str] = None,
                user_scope_id: Optional[str] = None) -> None:
    """Insert or replace an account entry (matched by account_id/email)."""
    ec = dict(get_email_config(username, user_scope_id=user_scope_id) or {})
    k = (entry.get("account_id") or entry.get("email") or "").strip().lower()
    accounts = [a for a in (ec.get("accounts") or []) if _acct_key(a) != k]
    accounts.append(entry)
    ec["accounts"] = accounts
    save_email_config(ec, username, user_scope_id=user_scope_id)


def patch_account(account_id: str, fields: Dict[str, Any], username: Optional[str] = None,
                  user_scope_id: Optional[str] = None) -> bool:
    ec = dict(get_email_config(username, user_scope_id=user_scope_id) or {})
    accounts = list(ec.get("accounts") or [])
    k = (account_id or "").strip().lower()
    hit = False
    for a in accounts:
        if _acct_key(a) == k:
            a.update(fields)
            hit = True
    if hit:
        ec["accounts"] = accounts
        save_email_config(ec, username, user_scope_id=user_scope_id)
    return hit


def remove_account(account_id: str, username: Optional[str] = None,
                   user_scope_id: Optional[str] = None) -> bool:
    ec = dict(get_email_config(username, user_scope_id=user_scope_id) or {})
    k = (account_id or "").strip().lower()
    accounts = list(ec.get("accounts") or [])
    kept = [a for a in accounts if _acct_key(a) != k]
    if len(kept) == len(accounts):
        return False
    ec["accounts"] = kept
    save_email_config(ec, username, user_scope_id=user_scope_id)
    return True


def set_mail_enabled(account_id: str, enabled: bool, username: Optional[str] = None,
                     user_scope_id: Optional[str] = None) -> bool:
    """Owner decision (2026-07-24): deleting a mail account that ALSO backs Calendar
    keeps its shared OAuth token AND config entry, but flips mail_enabled=False so
    the mail account list hides it while Calendar still resolves it. The marker
    lives in the account dict - no new config key."""
    return patch_account(account_id, {"mail_enabled": bool(enabled)}, username, user_scope_id=user_scope_id)


def list_mail_accounts(username: Optional[str] = None,
                       user_scope_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Accounts to show in the mail UI: everything except mail-disabled entries
    (calendar-only leftovers). A missing mail_enabled means True (existing accounts)."""
    ec = get_email_config(username, user_scope_id=user_scope_id)
    return [a for a in (ec.get("accounts") or []) if a.get("mail_enabled", True) is not False]


def delete_mail_account(account_id: str, username: Optional[str] = None,
                        cred_username: Optional[str] = None,
                        user_scope_id: Optional[str] = None) -> Dict[str, Any]:
    """Calendar-safe account delete (owner decision + EMAIL_CLIENT.md fail-closed
    lifecycle), centralized so the legacy route and the /api/mail endpoint share
    ONE cascade. Drops the v2 store rows/blobs/FTS/ops for the account and its
    MAIL-only IMAP credential lanes. A gmail/microsoft account shares its OAuth
    token with Calendar (Gmail union token; Microsoft's calendar token) - so the
    shared token AND the config entry are KEPT, the entry just flagged
    mail_enabled=False so the mail list hides it while Calendar still resolves it
    (its `enabled` stays true). A mail-only account (imap/icloud) is fully removed."""
    cred_username = cred_username if cred_username is not None else username
    acc = get_account(account_id, username, user_scope_id=user_scope_id) or {}
    provider = (acc.get("provider") or "imap").lower()
    backs_calendar = provider in ("gmail", "microsoft")
    result: Dict[str, Any] = {"ok": True, "backs_calendar": backs_calendar, "kept_for_calendar": False}
    # v2 store cascade (best-effort: the store may not exist on a flag-off instance)
    try:
        if Config.get("mail_engine_v2_enabled", False):
            from vaf.mail.store import MailStore
            scope = (user_scope_id or "").strip() or get_local_admin_scope_id()
            MailStore(scope).delete_account(account_id)
    except Exception as e:  # pragma: no cover - defensive
        result["store_error"] = str(e)
    from vaf.core.credential_store import delete_email_credentials
    if backs_calendar:
        # NEVER revoke the shared OAuth token; drop only the mail-only IMAP lanes.
        for lane in ("imap", "microsoft_imap", "apple", "icloud"):
            delete_email_credentials(account_id, lane, cred_username, user_scope_id=user_scope_id)
        set_mail_enabled(account_id, False, username, user_scope_id=user_scope_id)
        result["kept_for_calendar"] = True
    else:
        delete_email_credentials(account_id, provider=None, username=cred_username, user_scope_id=user_scope_id)
        remove_account(account_id, username, user_scope_id=user_scope_id)
    return result
