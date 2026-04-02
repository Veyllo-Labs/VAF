"""
Label (categorize) an email. Updates the message's category and adds a sender rule so
future mails from that sender get the same label. Same behaviour as changing the label
in the Mail dashboard (Settings → Connections → Email).
Scoped to the current user in network mode.
"""

import re

from vaf.core.email_sync_store import (
    get_message_from_addr,
    init_store,
    list_for_sender_relabel,
    update_message_category as store_update_message_category,
)
from vaf.core.email_transport import apply_sender_rules_to_category, get_account
from vaf.tools.base import BaseTool
from vaf.tools.mail_utils import (
    cred_scope_from_kwargs,
    cred_username_from_kwargs,
    list_accounts_for_user,
    store_candidates_for_mail,
    store_scope_from_kwargs,
    store_username_from_kwargs,
)


def _pattern_from_from_addr(from_addr: str) -> str:
    """Derive a sender rule pattern from From header (e.g. 'Twitch <no-reply@twitch.tv>' -> 'no-reply@twitch.tv')."""
    s = (from_addr or "").strip()
    if not s:
        return s
    m = re.search(r"<([^>]+@[^>]+)>", s)
    if m:
        return m.group(1).strip().lower()
    if "@" in s:
        return s.lower()
    return s


def _add_sender_rule(user_scope_id, pattern: str, category: str) -> None:
    """Add a sender rule to the user's email config (by scope). Same behaviour as Mail dashboard."""
    from vaf.api.email_routes import _get_email_config, _save_email_config

    ec = _get_email_config(None, user_scope_id=user_scope_id)
    rules = list(ec.get("sender_category_rules") or [])
    rules = [r for r in rules if isinstance(r, dict) and (r.get("pattern") or "").strip().lower() != pattern]
    rules.append({"pattern": pattern, "category": category})
    ec["sender_category_rules"] = rules
    _save_email_config(ec, None, user_scope_id=user_scope_id)


class LabelMailTool(BaseTool):
    """
    Set a message's category (label), e.g. promotions, newsletter, social, primary.
    Adds a sender rule so future mails from that sender get the same label.
    Use account_id, message_id, folder from mail_inbox output.
    """
    name = "label_mail"
    permission_level = "write"
    side_effect_class = "reversible"
    description = (
        "Set an email's label/category (e.g. promotions, newsletter, social, primary). "
        "Use after the user asks to label mails (e.g. 'label newsletters as promotions'). "
        "Use account_id, message_id, folder from mail_inbox. A sender rule is added so future mails from that sender get the same label."
    )
    parameters = {
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": "Email address of the connected account (from mail_inbox).",
            },
            "message_id": {
                "type": "string",
                "description": "Message ID from mail_inbox output (IDs by index block).",
            },
            "folder": {
                "type": "string",
                "description": "Folder name (default: INBOX).",
            },
            "category": {
                "type": "string",
                "description": "Label/category to set: primary, social, promotions, or a custom label (e.g. newsletter). Use lowercase; spaces become underscores.",
            },
        },
        "required": ["account_id", "message_id", "category"],
    }

    def run(self, **kwargs) -> str:
        store_username = store_username_from_kwargs(kwargs)
        cred_username = cred_username_from_kwargs(kwargs)
        user_scope_id = store_scope_from_kwargs(kwargs) or cred_scope_from_kwargs(kwargs)
        account_id = (kwargs.get("account_id") or "").strip()
        message_id = (kwargs.get("message_id") or "").strip()
        folder = (kwargs.get("folder") or "INBOX").strip()
        category = (kwargs.get("category") or "primary").strip().lower().replace(" ", "_")[:64] or "primary"

        if not account_id or not message_id:
            return "account_id, message_id, and category are required. Use mail_inbox to get message_id from the 'IDs by index' block."
        if not get_account(account_id, username=cred_username, user_scope_id=user_scope_id):
            return f"Account '{account_id}' not found. Connected: {', '.join(list_accounts_for_user(cred_username, user_scope_id=user_scope_id))}."

        candidates = store_candidates_for_mail(store_username, user_scope_id)
        updated_store_username = None
        updated_scope_id = None
        for try_username, try_scope_id in candidates:
            init_store(try_username, try_scope_id)
            ok = store_update_message_category(
                try_username,
                account_id,
                folder,
                message_id,
                category,
                user_scope_id=try_scope_id,
            )
            if ok:
                updated_store_username = try_username
                updated_scope_id = try_scope_id
                break

        if updated_store_username is None:
            return "Message not found in the synced mailbox. Sync in Settings → Connections → Email and use message_id from mail_inbox."

        from_addr = get_message_from_addr(
            updated_store_username, account_id, folder, message_id, user_scope_id=updated_scope_id
        )
        if from_addr:
            pattern = _pattern_from_from_addr(from_addr)
            if pattern:
                _add_sender_rule(updated_scope_id, pattern, category)
            rows = list_for_sender_relabel(updated_store_username, user_scope_id=updated_scope_id)
            updated = 1
            for row in rows:
                new_cat = apply_sender_rules_to_category(
                    row.get("from_addr") or "",
                    row.get("category") or "primary",
                    updated_store_username if updated_store_username else None,
                    user_scope_id=updated_scope_id,
                )
                new_cat = (new_cat or "primary").strip().lower().replace(" ", "_")[:64] or "primary"
                if new_cat != (row.get("category") or "primary"):
                    if store_update_message_category(
                        updated_store_username,
                        row["account_id"],
                        row["folder"],
                        row["message_id"],
                        new_cat,
                        user_scope_id=updated_scope_id,
                    ):
                        updated += 1
            return f"Label set to '{category}'. A sender rule was added; {updated} message(s) now use this label (including future mails from this sender)."
        return f"Label set to '{category}' for this message."