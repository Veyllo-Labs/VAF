"""
WhatsApp reply hook: headless_runner calls send_whatsapp_reply(username, chat_jid, text)
when a task has source=whatsapp. The bridge registers a callback that enqueues
the reply for the Node subprocess to send via Baileys.
"""
from typing import Callable, Optional

_send_callback: Optional[Callable[..., None]] = None


def set_whatsapp_reply_callback(cb: Optional[Callable[..., None]]) -> None:
    """Register a callback (username, chat_jid, text). Called by the WhatsApp bridge."""
    global _send_callback
    _send_callback = cb


def send_whatsapp_reply(username: str, chat_jid: str, text: str) -> None:
    """If a WhatsApp reply callback is registered, invoke it. Used by headless_runner and send_whatsapp tool."""
    try:
        from vaf.core.log_helper import log_whatsapp_reply
        log_whatsapp_reply(
            f"REPLY username={username} jid={chat_jid} len={len(text)} callback={_send_callback is not None}"
        )
    except Exception:
        pass
    if _send_callback and username and chat_jid and text:
        try:
            _send_callback(username, chat_jid, text)
        except Exception:
            pass
