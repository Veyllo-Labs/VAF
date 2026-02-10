"""
Mark an email as answered (agent has processed it). Sets answered_at so the UI shows
"Benatwortet am DD.MM.YYYY um HH:MM" and avoids double handling.
Scoped to the current user in network mode.
"""

from vaf.core.email_sync_store import init_store, update_message_answered
from vaf.core.email_transport import get_account
from vaf.tools.base import BaseTool
from vaf.tools.mail_utils import cred_username_from_kwargs, list_accounts_for_user, store_username_from_kwargs


class MarkMailAnsweredTool(BaseTool):
    """
    Mark a message as answered by the agent. Call this after you have processed or
    replied to an email so it shows "Benatwortet am ..." in the Mail UI and is not
    handled again. Use account_id, folder, message_id from mail_inbox.
    """
    name = "mark_mail_answered"
    description = (
        "Mark an email as answered by the agent. Call after you have processed or replied to the message. "
        "Use account_id, folder, message_id from mail_inbox. Avoids double handling and shows 'Benatwortet am ...' in the Mail UI."
    )
    parameters = {
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": "Email address of the connected account (e.g. user@gmail.com).",
            },
            "message_id": {
                "type": "string",
                "description": "Message ID from mail_inbox output.",
            },
            "folder": {
                "type": "string",
                "description": "Folder name (default: INBOX).",
            },
        },
        "required": ["account_id", "message_id"],
    }

    def run(self, **kwargs) -> str:
        store_username = store_username_from_kwargs(kwargs)
        cred_username = cred_username_from_kwargs(kwargs)
        account_id = (kwargs.get("account_id") or "").strip()
        message_id = (kwargs.get("message_id") or "").strip()
        folder = (kwargs.get("folder") or "INBOX").strip()
        if not account_id or not message_id:
            return "account_id and message_id are required. Use mail_inbox to get message_id."
        if not get_account(account_id, username=cred_username):
            return f"Account '{account_id}' not found. Connected accounts: {', '.join(list_accounts_for_user(cred_username))}."
        init_store(store_username)
        ok = update_message_answered(
            username=store_username,
            account_id=account_id,
            folder=folder,
            message_id=message_id,
            answered_at=None,  # use now
        )
        if ok:
            return "Message marked as answered. It will show 'Benatwortet am ...' in the Mail UI."
        return "Message not found in the synced mailbox. Sync the account in Settings → Connections → Email first."
