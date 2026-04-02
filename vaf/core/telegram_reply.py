"""
Telegram reply hook: headless_runner calls send_telegram_reply(chat_id, text)
when a task has source=telegram. The bridge registers a callback that enqueues
the reply for its sender thread to post to the Telegram API.
Supports optional file_path for document delivery (send_document).
"""
from typing import Callable, Optional

_send_callback: Optional[Callable[..., None]] = None


def set_telegram_reply_callback(cb: Optional[Callable[..., None]]) -> None:
    """Register a callback (chat_id, text, voice_lang=None, file_path=None). Called by the Telegram bridge."""
    global _send_callback
    _send_callback = cb


def has_telegram_reply_callback() -> bool:
    """Return True if Telegram reply callback is currently registered in this process."""
    return _send_callback is not None


def send_telegram_reply(
    chat_id: str,
    text: str,
    *,
    voice_lang: Optional[str] = None,
    file_path: Optional[str] = None,
) -> bool:
    """If a Telegram reply callback is registered, invoke it. Used by headless_runner and send_telegram tool.
    Pass voice_lang (e.g. 'de', 'en') to send as Sprachnachricht."""
    try:
        from vaf.core.log_helper import log_telegram_reply
        log_telegram_reply(f"REPLY chat_id={chat_id} len={len(text)} voice={bool(voice_lang)} file={bool(file_path)} callback={_send_callback is not None}")
    except Exception:
        pass
    if _send_callback and chat_id and text:
        try:
            if file_path is not None:
                _send_callback(chat_id, text, voice_lang=voice_lang, file_path=file_path)
            elif voice_lang:
                _send_callback(chat_id, text, voice_lang=voice_lang)
            else:
                _send_callback(chat_id, text)
            return True
        except Exception:
            return False
    return False
