# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The mail_inbox output must lead with the next-step hints (read_mail / find_mail)
so a weak model chains the tools instead of re-listing the inbox. Live-test finding:
a 4B model looped on mail_inbox and never called read_mail/find_mail."""
from vaf.tools.mail_inbox import _format_inbox, _format_inbox_all_accounts

_MSGS = [{"from": "Alice <a@x.com>", "subject": "test - vaf", "message_id": "<1@x>",
          "account_id": "u@x", "provider_message_id": "p1"}]


def test_inbox_output_leads_with_read_and_find_hints():
    for fn in (_format_inbox, _format_inbox_all_accounts):
        out = fn(_MSGS, "INBOX")
        head = out.split("\n\n", 1)[0]
        # both next-step tools named, in the very first block, before the listing
        assert "read_mail" in head, fn.__name__
        assert "find_mail" in head, fn.__name__
        assert "Do NOT call mail_inbox again" in head, fn.__name__
        assert head.index("read_mail") < out.index("Recent emails"), fn.__name__
        # the message_id the model needs for read_mail is still present
        assert "message_id='<1@x>'" in out
