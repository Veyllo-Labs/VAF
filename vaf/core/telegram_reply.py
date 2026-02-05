"""
Telegram reply hook: headless_runner calls send_telegram_reply(chat_id, text)
when a task has source=telegram. The bridge registers a callback that enqueues
the reply for its sender thread to post to the Telegram API.
"""
from typing import Optional, Callable

_send_callback: Optional[Callable[[str, str], None]] = None


def set_telegram_reply_callback(cb: Optional[Callable[[str, str], None]]) -> None:
    """Register a (chat_id, text) -> None callback. Called by the Telegram bridge."""
    global _send_callback
    _send_callback = cb


def send_telegram_reply(chat_id: str, text: str) -> None:
    """If a Telegram reply callback is registered, invoke it. Used by headless_runner."""
    try:
        from vaf.core.log_helper import log_telegram_reply
        log_telegram_reply(f"REPLY chat_id={chat_id} len={len(text)} callback={_send_callback is not None}")
    except Exception:
        pass
    if _send_callback and chat_id and text:
        try:
            _send_callback(chat_id, text)
        except Exception:
            pass
