# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Recipient-address parsing for the mail subsystem (mail v2-only port, P2).

Route-independent home for normalize_recipients so the native sender does not have
to import the heavy vaf.core.email_transport module for a pure helper. The historical
name email_transport.normalize_recipients is re-exported from here (a guard test
pins them to one object, Rule 2 single-source)."""
from email.utils import getaddresses
from typing import Any, List


def normalize_recipients(value: Any) -> List[str]:
    """Parse a recipient string ("a@x.com, b@y.com") or list into validated address
    strings. Invalid/empty entries are dropped; order is preserved and duplicates
    removed."""
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    raw = ", ".join(str(x) for x in items if x)
    out: List[str] = []
    for _name, addr in getaddresses([raw]):
        addr = (addr or "").strip()
        if "@" in addr and "." in addr.rsplit("@", 1)[-1] and addr not in out:
            out.append(addr)
    return out
