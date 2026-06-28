# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Back-compat shim. The message store was renamed to ``channel_message_store`` because it is
channel-generic (WhatsApp/Telegram/Discord), not WhatsApp-specific. This module re-exports the new
module for one release. Prefer importing from ``vaf.core.channel_message_store``.
"""
from vaf.core.channel_message_store import (  # noqa: F401
    init_store,
    append_message,
    search_messages,
    list_chats_from_store,
    get_chat_messages,
)
