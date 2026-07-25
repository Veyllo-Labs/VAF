# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Shared helpers for mail tools (multi-user scoping + safety filters)."""

from email.utils import parseaddr
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


def mail_v2_active(store_username: str, user_scope_id: Optional[str]) -> bool:
    """Whether the mail engine may serve THIS caller. A USER-ISOLATION rule.

    It survived the engine flag on purpose and has nothing to do with a rollout:
    the store is scope-keyed with no username dimension, so a legacy per-username
    caller (username set, NO user_scope_id) has no store of its own. Serving it
    would resolve through the local admin scope and hand that caller the ADMIN's
    mailbox - reads AND writes. Two other layers already refuse exactly this
    (email_sync_store's `_legacy_user` branch and
    MailSyncSupervisor._collect_accounts, which never syncs email_config_by_user
    accounts); this is the ONE place the tools ask, so the rule cannot drift
    across the five call sites (Rule 2). Such an install heals itself: the user
    reconnects the account once and lands on a scope-keyed config.
    """
    legacy_user = bool((store_username or "").strip()) and not (user_scope_id or "").strip()
    return not legacy_user


def list_accounts_with_labels_for_user(
    cred_username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> List[dict]:
    """Connected email accounts with labels, resolved through the single
    account-config SSOT (email_accounts.get_email_config) so this matches every
    other reader instead of duplicating the scope-resolution. Strict per-user
    isolation, no cross-user fallback (P3.5)."""
    from vaf.core.email_accounts import get_email_config
    ec = get_email_config(cred_username, user_scope_id=user_scope_id)
    accounts = ec.get("accounts") or []
    return [
        {
            "email": a.get("email") or a.get("account_id"),
            "label": (a.get("label") or "").strip(),
        }
        for a in accounts
        if a.get("email") or a.get("account_id")
    ]


_FREE_MAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "yahoo.com",
    "icloud.com",
    "gmx.de",
    "gmx.net",
    "mail.com",
    "proton.me",
    "protonmail.com",
}

# Shared with send_mail's high-risk outbound gate - single source, do not copy
# (the previous duplicate in send_mail.py had already drifted).
_EXEC_IMPERSONATION_WORDS = (
    "ceo",
    "cfo",
    "finance",
    "accounts payable",
    "buchhaltung",
    "geschaeftsfuehrung",
    "geschäftsführung",
    "director",
    "vorstand",
)

_SOCIAL_ENGINEERING_WORDS = (
    "urgent",
    "dringend",
    "immediately",
    "sofort",
    "wire transfer",
    "bank transfer",
    "überweisung",
    "gift card",
    "amazon card",
    "credentials",
    "passwort",
    "password",
    "verify account",
    "konto bestätigen",
    "invoice overdue",
    "rechnung überfällig",
)


def _email_domain_from_from_header(from_value: str) -> str:
    _, addr = parseaddr(from_value or "")
    if "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[-1].strip().lower()


def _phishing_score(message: dict) -> tuple[int, List[str]]:
    category = str(message.get("category") or "").strip().lower()
    sender = str(message.get("from") or "")
    subject = str(message.get("subject") or "")
    snippet = str(message.get("body_snippet") or "")
    domain = _email_domain_from_from_header(sender)
    sender_lower = sender.lower()
    text = f"{subject}\n{snippet}\n{sender}".lower()

    score = 0
    reasons: List[str] = []

    if category in {"spam", "junk", "junkemail"}:
        score += 10
        reasons.append("provider_spam_category")
    if domain.startswith("xn--"):
        score += 3
        reasons.append("punycode_domain")
    if any(word in text for word in _SOCIAL_ENGINEERING_WORDS):
        score += 2
        reasons.append("social_engineering_language")
    if any(word in sender_lower for word in _EXEC_IMPERSONATION_WORDS) and domain in _FREE_MAIL_DOMAINS:
        score += 3
        reasons.append("exec_impersonation_free_mail")
    if ("reply-to" in text and "different" in text) or ("unusual activity" in text and "click" in text):
        score += 1
        reasons.append("phishing_pattern")
    return score, reasons


def _phishing_filter_policy() -> tuple[bool, int, set[str]]:
    cfg = Config.load() or {}
    enabled = bool(cfg.get("email_agent_phishing_filter_enabled", True))
    threshold_raw = cfg.get("email_agent_phishing_score_threshold", 3)
    try:
        threshold = int(threshold_raw)
    except Exception:
        threshold = 3
    threshold = max(1, min(threshold, 10))
    trusted = cfg.get("email_agent_trusted_sender_domains") or []
    trusted_domains = {str(x).strip().lower() for x in trusted if str(x).strip()}
    return enabled, threshold, trusted_domains


def annotate_messages_with_agent_visibility(messages: List[dict]) -> List[dict]:
    """
    Annotate each message with agent-visibility metadata:
    - suspicious_for_agent: bool
    - suspicious_reasons: List[str]
    - suspicious_score: int
    This is used by Web UI to show warnings while the agent-side tools still hide those mails.
    """
    enabled, threshold, trusted_domains = _phishing_filter_policy()
    out: List[dict] = []
    for m in messages or []:
        row = dict(m or {})
        if not enabled:
            row["suspicious_for_agent"] = False
            row["suspicious_reasons"] = []
            row["suspicious_score"] = 0
            out.append(row)
            continue
        domain = _email_domain_from_from_header(str(row.get("from") or ""))
        score, reasons = _phishing_score(row)
        suspicious = bool(score >= threshold and not (domain and domain in trusted_domains))
        row["suspicious_for_agent"] = suspicious
        row["suspicious_reasons"] = reasons if suspicious else []
        row["suspicious_score"] = score if suspicious else 0
        out.append(row)
    return out


def filter_phishing_messages_for_agent(messages: List[dict]) -> tuple[List[dict], int]:
    """
    Filter high-risk phishing-like messages from agent-facing mail tools.
    This does not delete anything from the mailbox; it only hides suspicious entries
    from tool outputs to reduce prompt-injection/social-engineering risk.
    """
    enabled, threshold, trusted_domains = _phishing_filter_policy()
    if not enabled:
        return messages, 0

    safe: List[dict] = []
    blocked = 0
    for m in messages or []:
        domain = _email_domain_from_from_header(str(m.get("from") or ""))
        if domain and domain in trusted_domains:
            safe.append(m)
            continue
        score, _ = _phishing_score(m)
        if score >= threshold:
            blocked += 1
            continue
        safe.append(m)
    return safe, blocked
