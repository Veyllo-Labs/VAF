# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
WhatsApp reply hook: headless_runner calls send_whatsapp_reply(username, chat_jid, text)
when a task has source=whatsapp. The bridge registers a callback that enqueues
the reply for the Node subprocess to send via Baileys.
Supports optional voice_path for voice messages (Sprachnachrichten).
"""
from typing import Callable, Optional

_send_callback: Optional[Callable[..., None]] = None


def set_whatsapp_reply_callback(cb: Optional[Callable[..., None]]) -> None:
    """Register a callback (username, chat_jid, text, voice_path?). Called by the WhatsApp bridge."""
    global _send_callback
    _send_callback = cb


def send_whatsapp_reply(
    username: str,
    chat_jid: str,
    text: str,
    voice_path: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> bool:
    """Hand a WhatsApp reply to the bridge callback. Use voice_path for voice messages. user_scope_id helps
    resolve Front Office contacts (scoped storage). Returns True only if the message was actually enqueued for
    the bridge; False when it was dropped (no callback registered / empty recipient / empty body / the bridge
    refused or errored) so a caller can fall back to another channel instead of assuming success."""
    try:
        from vaf.core.log_helper import log_whatsapp_reply
        log_whatsapp_reply(
            f"REPLY username={username} jid={chat_jid} len={len(text)} voice={bool(voice_path)} callback={_send_callback is not None}"
        )
    except Exception:
        pass
    if not username or not chat_jid:
        return False
    if not _send_callback:
        try:
            from vaf.core.log_helper import log_whatsapp_reply
            log_whatsapp_reply("REPLY DROPPED callback not set (bridge not running?)")
        except Exception:
            pass
        return False
    if not text and not voice_path:
        return False
    # The nested try keeps the bool contract intact even if the retry itself raises: any failure ->
    # False, never a propagated exception.
    try:
        try:
            result = _send_callback(username, chat_jid, text or "", voice_path, user_scope_id)
        except TypeError:
            # Older callback signature without user_scope_id.
            result = _send_callback(username, chat_jid, text or "", voice_path)
        # A callback that returns a bool reports real enqueue success (e.g. the bridge dropped a
        # non-whitelisted recipient); a legacy callback returning None is assumed to have accepted it.
        return result if isinstance(result, bool) else True
    except Exception:
        return False
