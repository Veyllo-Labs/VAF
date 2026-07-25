# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Compatibility shim for the removed legacy mail transport.

This module used to BE the mail transport: three provider dialects (IMAP/SMTP,
Gmail REST, Microsoft Graph) with their own fetch, body and send paths. All of it
was deleted in the mail v2-only port (P7.3) - `vaf/mail/` owns mail end to end now
(`imap_client.py` connects, `sync.py` fetches, `parser.py` parses, `sender.py`
submits over SMTP/XOAUTH2).

What is left has exactly two jobs and no logic of its own:

1. Re-export the account/sender-rule readers under their HISTORICAL names. They
   live in the route-independent SSOT `vaf/core/email_accounts.py` (P3.1); the
   names survive here because `send_mail`, `reply_mail` and `manage_mail` import
   `get_account` from this module, and because `tests/test_email_accounts_ssot.py`
   pins each one to the SSOT object so a future edit cannot fork them again.
   They are shims - do not add behavior here, change the SSOT.
2. `_mask_account`, the single account-id masking rule shared with email_routes.

If a future change leaves this module with no importers, delete it rather than
letting it look like a transport again.
"""
import logging

from vaf.core.email_accounts import (  # noqa: F401 - historical names, pinned by a guard test
    _email_config_candidates,
    apply_sender_rules_to_category,
    get_account,
    get_email_config,
    get_sender_rules,
)
from vaf.mail.addressing import normalize_recipients  # noqa: F401 - historical name

logger = logging.getLogger("vaf.core.email_transport")

# Historical private aliases for the same SSOT objects (guard-pinned).
_get_email_config = get_email_config
_get_sender_rules = get_sender_rules


def _mask_account(account_id: str) -> str:
    """Mask an account id (usually an email address) for logs: first 3 chars + '***'.
    Single masking rule for the mail stack (email_routes.py uses the same one).
    Never log the full account id or full provider response bodies."""
    return (str(account_id) if account_id else "")[:3] + "***"
