"""
Show email inbox from the same sync store as the Mail dashboard (Settings → Connections → Email).
Lists messages with the same labels (Primary, Social, Promotions) the user sees. If the store
is empty, fetches from the provider and writes to the store so UI and agent stay in sync.
Use read_mail to get the full body of a specific message.
"""

import logging
from datetime import datetime

from vaf.core.email_sync_store import init_store, list_messages as store_list_messages, upsert_messages
from vaf.core.email_transport import fetch_mail, get_account
from vaf.tools.base import BaseTool
from vaf.tools.mail_utils import (
    cred_scope_from_kwargs,
    cred_username_from_kwargs,
    list_accounts_for_user,
    store_candidates_for_mail,
    store_scope_from_kwargs,
    store_username_from_kwargs,
)

logger = logging.getLogger("vaf.tools.mail_inbox")


# Max lengths for compact list (keeps output within context, avoids truncation)
_FROM_MAX = 38
_SUBJECT_MAX = 50
_DATE_FMT = "%Y-%m-%d %H:%M"  # compact date


def _trunc(s: str, max_len: int) -> str:
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


def _short_date(iso_or_raw: str) -> str:
    if not iso_or_raw:
        return ""
    s = (iso_or_raw or "").strip()
    if len(s) <= 16:
        return s
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime(_DATE_FMT)
    except Exception:
        return s[:16] + "…" if len(s) > 16 else s


def _format_inbox(messages: list, folder: str) -> str:
    if not messages:
        return f"No messages in {folder} (sync first in Settings → Connections → Email if needed)."
    list_lines = []
    id_lines = []
    for i, m in enumerate(messages, 1):
        from_str = _trunc(m.get("from", "") or "", _FROM_MAX)
        date_display = _short_date(m.get("message_date_iso") or m.get("date", ""))
        subj = _trunc(m.get("subject", "") or "", _SUBJECT_MAX)
        list_lines.append(f"{i}. From: {from_str} | Date: {date_display} | Subject: {subj}")
        acc = m.get("account_id") or ""
        mid = m.get("message_id") or ""
        pid = m.get("provider_message_id") or ""
        id_lines.append(f"  {i}: account_id={acc} message_id={mid!r} provider_message_id={pid} folder={folder or 'INBOX'}")
    out = "Recent emails (same as Mail dashboard, newest first):\n" + "\n".join(list_lines)
    out += "\n\nTo read the full body of a message, use read_mail with the IDs below (by index):\n" + "\n".join(id_lines)
    return out


def _format_inbox_all_accounts(messages: list, folder: str) -> str:
    """Compact list + read_mail IDs by index so the model can show N distinct emails and call read_mail by index."""
    if not messages:
        return f"No messages in {folder} across any connected account. Sync in Settings → Connections → Email if needed."
    list_lines = []
    id_lines = []
    for i, m in enumerate(messages, 1):
        from_str = _trunc(m.get("from", "") or "", _FROM_MAX)
        date_display = _short_date(m.get("message_date_iso") or m.get("date", ""))
        subj = _trunc(m.get("subject", "") or "", _SUBJECT_MAX)
        acc = m.get("account_id") or ""
        list_lines.append(f"{i}. From: {from_str} | Date: {date_display} | Subject: {subj} | account: {_trunc(acc, 28)}")
        mid = m.get("message_id") or ""
        pid = m.get("provider_message_id") or ""
        id_lines.append(f"  {i}: account_id={acc} message_id={mid!r} provider_message_id={pid} folder={folder or 'INBOX'}")
    out = "Recent emails (all connected accounts, newest first):\n" + "\n".join(list_lines)
    out += "\n\nTo read a message, use read_mail with the IDs below (by index). Do not invent or repeat entries; use this list only.\n" + "\n".join(id_lines)
    return out


class MailInboxTool(BaseTool):
    """
    Show the inbox (list of recent emails). Uses the same synced mailbox as the Mail dashboard.
    When account_id is omitted, lists messages from ALL connected accounts (user need not specify which account).
    To read a message's content, use read_mail with account_id, message_id, folder, and provider_message_id from the list.
    """
    name = "mail_inbox"
    description = (
        "Show the inbox (list of recent emails). Same mailbox as the Mail dashboard (Settings → Connections → Email). "
        "When the user asks for a specific number (e.g. 'list 15 mails', 'show 20 emails', 'die letzten 50'), you MUST call mail_inbox with max_messages set to that number (e.g. max_messages=15) and then present the tool output to the user as-is. Do NOT reuse an old list or repeat the same entry to reach the count; always call the tool with the requested max_messages. "
        "Omit account_id to list from ALL connected accounts. "
        "Parameters: account_id (optional), folder (default INBOX), max_messages (1–200; default 50). "
        "To read a message's body, use read_mail with account_id, message_id, folder, and provider_message_id from the 'IDs below (by index)' block in the tool output."
    )
    parameters = {
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": "Optional. Email of one connected account. If omitted, messages from ALL connected accounts are listed (prefer when user does not specify an account).",
            },
            "folder": {
                "type": "string",
                "description": "IMAP folder name (default: INBOX).",
            },
            "max_messages": {
                "type": "integer",
                "description": "Number of emails to return. When the user says 'list 15 mails' or 'show 20 emails', pass that exact number (e.g. 15 or 20). Default 50, max 200. Always pass the user-requested count so the tool returns that many distinct entries.",
            },
        },
        "required": [],
    }

    def run(self, **kwargs) -> str:
        store_username = store_username_from_kwargs(kwargs)
        cred_username = cred_username_from_kwargs(kwargs)
        user_scope_id = store_scope_from_kwargs(kwargs) or cred_scope_from_kwargs(kwargs)
        account_id = (kwargs.get("account_id") or "").strip()
        folder = (kwargs.get("folder") or "INBOX").strip()
        max_messages = int(kwargs.get("max_messages") or 50)
        if max_messages < 1 or max_messages > 200:
            max_messages = 50
        accounts = list_accounts_for_user(cred_username, user_scope_id=user_scope_id)
        if not accounts:
            # Debug: log full context so we can diagnose why accounts are missing
            from vaf.core.config import Config
            ec_raw = Config.get("email_config")
            by_user_keys = list((Config.get("email_config_by_user") or {}).keys())
            logger.warning(
                "mail_inbox: No accounts found. cred_username=%r, kwargs_username=%r, "
                "email_config_type=%s, email_config_accounts=%d, email_config_by_user_keys=%r",
                cred_username,
                kwargs.get("username"),
                type(ec_raw).__name__,
                len((ec_raw or {}).get("accounts", [])) if isinstance(ec_raw, dict) else -1,
                by_user_keys,
            )
            return (
                "No email accounts connected. The user must add an account in Settings → Connections → Email "
                "(Google, Microsoft, or other IMAP)."
            )
        if account_id and not get_account(account_id, username=cred_username, user_scope_id=user_scope_id):
            return f"Account '{account_id}' not found. Connected accounts: {', '.join(accounts)}."
        # Try primary store first, then legacy and single-scope stores so we match the Mail Dashboard DB
        store_candidates = store_candidates_for_mail(store_username, user_scope_id)
        messages: list = []
        used_store_username = store_username
        used_scope_id = user_scope_id
        for try_username, try_scope_id in store_candidates:
            init_store(try_username, try_scope_id)
            messages = store_list_messages(
                account_id=account_id or None,
                folder=folder,
                limit=max_messages,
                offset=0,
                username=try_username,
                user_scope_id=try_scope_id,
                category=None,
            )
            if messages:
                used_store_username = try_username
                used_scope_id = try_scope_id
                break
        if messages:
            if account_id:
                return _format_inbox(messages, folder)
            return _format_inbox_all_accounts(messages, folder)
        if not account_id:
            return (
                "No messages in the sync store yet. Ask the user to open Settings → Connections → Email and click Sync on each account, "
                "or call mail_inbox with a specific account_id to fetch that account."
            )
        try:
            messages = fetch_mail(account_id, folder=folder, max_messages=max_messages, username=cred_username, user_scope_id=user_scope_id)
        except Exception as e:
            return f"Failed to fetch mail: {e}"
        if not messages:
            return f"No messages in {folder}. Sync in Settings → Connections → Email to populate the mailbox."
        upsert_messages(account_id, folder, messages, username=store_username, user_scope_id=user_scope_id)
        messages = store_list_messages(
            account_id=account_id, folder=folder, limit=max_messages, offset=0, username=store_username, user_scope_id=user_scope_id, category=None
        )
        return _format_inbox(messages, folder)
