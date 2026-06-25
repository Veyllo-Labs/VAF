# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
List connected email accounts for the current user.
Use before send_mail when the user asks to send an email without specifying which account to use.
"""

from vaf.tools.base import BaseTool
from vaf.tools.mail_utils import cred_scope_from_kwargs, cred_username_from_kwargs, list_accounts_with_labels_for_user


class ListEmailAccountsTool(BaseTool):
    """
    List connected email account addresses and their labels (purpose) for the current user.
    Call this when the user asks to send an email but does not specify which account to use.
    Labels (e.g. support, outreach, sending) help choose the right account. Use account_id with send_mail.
    """
    name = "list_email_accounts"
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "List connected email accounts with optional labels (e.g. support, outreach, sending). "
        "Call when the user asks to send an email but does not specify from which account. "
        "Use the label to pick the right account (e.g. 'use support account' → pick account with label 'support'). "
        "Use account_id with send_mail. Do NOT ask the user for the account ID."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def run(self, **kwargs) -> str:
        cred_username = cred_username_from_kwargs(kwargs)
        user_scope_id = cred_scope_from_kwargs(kwargs)
        accounts = list_accounts_with_labels_for_user(cred_username, user_scope_id=user_scope_id)
        if not accounts:
            return (
                "No email accounts connected. The user must add an account in Settings → Connections → Email "
                "(Google, Microsoft, or other IMAP)."
            )
        lines = []
        for a in accounts:
            email = a.get("email") or ""
            label = (a.get("label") or "").strip()
            if label:
                lines.append(f"{email} (label: {label})")
            else:
                lines.append(email)
        if len(lines) == 1:
            return f"One connected account: {lines[0]}. Use this account_id with send_mail."
        return f"Connected accounts: {'; '.join(lines)}. Use the account_id (email) with send_mail. Pick by label when the user specifies (e.g. 'support', 'outreach')."
