# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Answer on the chat a turn came from, in plain text: one function instead of a chain per
call site.

The runner answered a chat turn by hand in several places (the turn-error reply, the
speaker-confirm ack, the sub-agent drain summary), each an if/elif over the messengers, so
every new chat channel meant another branch at every site. ``reply_to_chat`` is that chain
once: a channel is answered by the address the bridge put into the turn's metadata
(``telegram_chat_id``, ``discord_channel_id``, ``whatsapp_chat_jid``; the Veyllo App by the
account the device row named). The text is sent as given: callers clean it first
(``headless_runner._prepare_channel_outbound``).

Framework, not harness: the per-channel senders are callbacks the bridges register
(``telegram_reply``, ``discord_reply``, ``whatsapp_reply``); without one, nothing is sent.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# The metadata key that addresses each channel's chat, in the order a turn without a known
# source is matched (the order the drain has always tried them in).
CHAT_ADDRESS_KEYS = (("telegram", "telegram_chat_id"), ("discord", "discord_channel_id"),
                     ("whatsapp", "whatsapp_chat_jid"))


def chat_source(source: Optional[str], meta: Optional[Dict[str, Any]], *,
                infer: bool = False) -> Optional[str]:
    """The channel to answer on: ``source`` when its address is in ``meta``. With ``infer``
    (a stored session that may predate its ``source`` field) the first channel whose address
    is present answers otherwise."""
    meta = meta or {}
    keys = dict(CHAT_ADDRESS_KEYS)
    if source in keys and meta.get(keys[source]):
        return source
    if not infer:
        return None
    for name, key in CHAT_ADDRESS_KEYS:
        if meta.get(key):
            return name
    return None


def reply_to_chat(source: Optional[str], meta: Optional[Dict[str, Any]], text: str, *,
                  infer: bool = False) -> bool:
    """Send ``text`` to the chat the turn came from. Whether a sender took it."""
    meta = meta or {}
    text = str(text or "")
    if not text:
        return False
    channel = chat_source(source, meta, infer=infer)
    scope = meta.get("user_scope_id")
    scope = str(scope) if scope else None
    if channel == "telegram":
        from vaf.core.telegram_reply import send_telegram_reply
        return send_telegram_reply(str(meta["telegram_chat_id"]), text) is not False
    if channel == "discord":
        from vaf.core.discord_reply import send_discord_reply
        send_discord_reply(str(meta["discord_channel_id"]), text)
        return True
    if channel == "whatsapp":
        from vaf.core.whatsapp_reply import send_whatsapp_reply
        return bool(send_whatsapp_reply(meta.get("username") or "admin",
                                        str(meta["whatsapp_chat_jid"]), text,
                                        user_scope_id=scope))
    return False
