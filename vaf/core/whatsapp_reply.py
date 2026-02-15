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


def send_whatsapp_reply(username: str, chat_jid: str, text: str, voice_path: Optional[str] = None) -> None:
    """If a WhatsApp reply callback is registered, invoke it. Use voice_path for voice messages."""
    try:
        from vaf.core.log_helper import log_whatsapp_reply
        log_whatsapp_reply(
            f"REPLY username={username} jid={chat_jid} len={len(text)} voice={bool(voice_path)} callback={_send_callback is not None}"
        )
    except Exception:
        pass
    if not username or not chat_jid:
        return
    if not _send_callback:
        try:
            from vaf.core.log_helper import log_whatsapp_reply
            log_whatsapp_reply("REPLY DROPPED callback not set (bridge not running?)")
        except Exception:
            pass
        return
    if not text and not voice_path:
        return
    try:
        _send_callback(username, chat_jid, text or "", voice_path)
    except Exception:
        pass
