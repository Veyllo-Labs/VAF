"""
Show email inbox from the same sync store as the Mail dashboard (Settings → Connections → Email).
Lists messages with the same labels (Primary, Social, Promotions) the user sees. If the store
is empty, fetches from the provider and writes to the store so UI and agent stay in sync.
Use read_mail to get the full body of a specific message.
"""

import logging

from vaf.core.email_sync_store import init_store, list_messages as store_list_messages, upsert_messages
from vaf.core.email_transport import fetch_mail, get_account
from vaf.tools.base import BaseTool
from vaf.tools.mail_utils import cred_scope_from_kwargs, cred_username_from_kwargs, list_accounts_for_user, store_scope_from_kwargs, store_username_from_kwargs

logger = logging.getLogger("vaf.tools.mail_inbox")


def _format_inbox(messages: list, folder: str) -> str:
    if not messages:
        return f"No messages in {folder} (sync first in Settings → Connections → Email if needed)."
    lines = []
    for i, m in enumerate(messages, 1):
        mid = m.get("message_id") or ""
        pid = m.get("provider_message_id") or ""
        cat = m.get("category") or "primary"
        extra = [f"label: {cat}"]
        if mid:
            extra.append(f"message_id: {mid}")
        if pid:
            extra.append(f"provider_message_id: {pid}")
        suffix = " | " + " | ".join(extra)
        # Prefer message_date_iso for unambiguous date; fall back to raw date header
        date_display = m.get("message_date_iso") or m.get("date", "")
        lines.append(
            f"{i}. From: {m.get('from', '')} | Date: {date_display} | Subject: {m.get('subject', '')} | {suffix}"
        )
    out = "Recent emails (same as Mail dashboard, newest first by message date):\n" + "\n".join(lines)
    out += "\n\nTo read the full body of a message, use read_mail with account_id, message_id, folder, and provider_message_id when available."
    return out


def _format_inbox_all_accounts(messages: list, folder: str) -> str:
    """Like _format_inbox but each line includes account_id so read_mail can be used."""
    if not messages:
        return f"No messages in {folder} across any connected account. Sync in Settings → Connections → Email if needed."
    lines = []
    for i, m in enumerate(messages, 1):
        acc = m.get("account_id") or ""
        mid = m.get("message_id") or ""
        pid = m.get("provider_message_id") or ""
        cat = m.get("category") or "primary"
        extra = [f"account_id: {acc}", f"label: {cat}"]
        if mid:
            extra.append(f"message_id: {mid}")
        if pid:
            extra.append(f"provider_message_id: {pid}")
        suffix = " | " + " | ".join(extra)
        date_display = m.get("message_date_iso") or m.get("date", "")
        lines.append(
            f"{i}. From: {m.get('from', '')} | Date: {date_display} | Subject: {m.get('subject', '')} | {suffix}"
        )
    out = "Recent emails (all connected accounts, same as Mail dashboard, newest first by message date):\n" + "\n".join(lines)
    out += "\n\nTo read the full body of a message, use read_mail with account_id, message_id, folder, and provider_message_id from the list."
    return out


class MailInboxTool(BaseTool):
    """
    Show the inbox (list of recent emails). Uses the same synced mailbox as the Mail dashboard.
    When account_id is omitted, lists messages from ALL connected accounts (user need not specify which account).
    To read a message's content, use read_mail with account_id, message_id, folder, and provider_message_id from the list.
    """
    name = "mail_inbox"
    description = (
        "Show the inbox (list of recent emails). Uses the same mailbox as the Mail dashboard (Settings → Connections → Email). "
        "When the user asks for a specific number (e.g. 'list 20 mails', 'die anderen 20', 'show 50 emails'), pass max_messages=20 or 50 so that many are listed – do not show only 3. "
        "When the user asks about mails in general, call without account_id to list from ALL connected accounts. "
        "Optionally pass account_id, folder, and max_messages (1–200; default 50). "
        "To read a message's content, use read_mail with account_id, message_id, folder, and provider_message_id from the list."
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
                "description": "How many mails to list (e.g. 3 for 3, 20 for 20, 50 for 50). When user says 'list 20 mails' or 'die anderen 20', pass 20. Default 50, max 200.",
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
        init_store(store_username, user_scope_id)
        messages = store_list_messages(
            account_id=account_id or None,
            folder=folder,
            limit=max_messages,
            offset=0,
            username=store_username,
            user_scope_id=user_scope_id,
            category=None,
        )
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
