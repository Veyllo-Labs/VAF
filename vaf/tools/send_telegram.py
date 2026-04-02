"""
Send a proactive message to the user via Telegram.
Only available when the user has Telegram connected; use main_messenger or user request to decide when to call this tool.
Supports optional file attachments (e.g. documents, PDFs) when the user asks for a document.
"""

import re
from pathlib import Path

from vaf.tools.base import BaseTool
from vaf.tools.filesystem import is_safe_path


def _resolve_path(path_str: str) -> tuple[Path | None, str | None]:
    """Resolve file path (supports file:// URLs, absolute/relative paths, folder aliases like Downloads).
    Returns (resolved_path, error_message). Exactly one is None."""
    s = (path_str or "").strip()
    if not s:
        return None, None
    if s.lower().startswith("file://"):
        s = s[7:]
    safe, result = is_safe_path(s)
    if not safe:
        return None, result  # result = error message
    return Path(result), None


class SendTelegramTool(BaseTool):
    """
    Send a message to the user via Telegram, optionally with a document attachment.
    Use when the user asked you to send them something (e.g. a summary, result, notification, or document)
    and they prefer Telegram or said "via Telegram".
    For documents (invoices, contracts, PDFs): pass file_path after creating/finding the file.
    """
    name = "send_telegram"
    permission_level = "write"
    side_effect_class = "irreversible"
    description = (
        "Send a message to the user via Telegram. "
        "Use when the user asked you to send them something (e.g. 'send me the result via Telegram' or when main_messenger is Telegram). "
        "For voice messages (Sprachnachricht), pass voice_lang (e.g. 'de', 'en'). "
        "For documents (invoice, contract, PDF, etc.), pass file_path with the full path to the file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message text (caption) to send. For documents, use as caption (e.g. 'Here is your invoice'). For voice, this is the text to speak (TTS).",
            },
            "voice_lang": {
                "type": "string",
                "description": "Optional. Language code (e.g. 'de', 'en') to send as voice message (Sprachnachricht). Use when user asks for a voice message via Telegram.",
            },
            "file_path": {
                "type": "string",
                "description": "Optional. Full path to a file to send as document (PDF, DOCX, etc.). Use when user asks for a specific document or when you created one.",
            },
        },
        "required": ["message"],
    }

    def run(self, **kwargs) -> str:
        message = (kwargs.get("message") or "").strip()
        if not message:
            return "No message provided. Pass the message text to send."

        file_path_str = (kwargs.get("file_path") or "").strip()
        file_path: Path | None = None
        if file_path_str:
            resolved, path_error = _resolve_path(file_path_str)
            if path_error:
                return path_error
            file_path = resolved

        username = kwargs.get("username") or "admin"
        user_scope_id = kwargs.get("user_scope_id")

        try:
            from vaf.core.messaging_connections import get_telegram_chat_id
            from vaf.core.telegram_reply import has_telegram_reply_callback, send_telegram_reply
            from vaf.api.telegram_bridge import send_telegram_message_direct
        except ImportError as e:
            return f"Telegram send unavailable: {e}"

        chat_id = get_telegram_chat_id(user_scope_id, username)
        if not chat_id:
            return (
                "No Telegram contact found for this user. "
                "The user must have their Telegram account added in Settings → Connections → Telegram (whitelist). "
                "Once they are in the whitelist, you can send them proactive messages."
            )

        if file_path and not file_path.is_file():
            return f"File not found or not a file: {file_path}"

        # Strip <think>...</think> and internal system phrases for clean delivery
        out = re.sub(r"<think>.*?</think>", "", message, flags=re.DOTALL)
        out = re.sub(r"\n{3,}", "\n\n", out).strip()
        try:
            from vaf.core.headless_runner import _sanitize_outgoing_message
            out = _sanitize_outgoing_message(out)
        except Exception:
            pass
        if not out:
            return "Message was blocked (contained internal system content). Send a clean user-facing message without any internal context markers."

        voice_lang = (kwargs.get("voice_lang") or "").strip()
        if not has_telegram_reply_callback():
            ok, error = send_telegram_message_direct(
                chat_id,
                out[:4096],
                voice_lang=voice_lang[:2].lower() if voice_lang else None,
                file_path=str(file_path) if file_path else None,
            )
            if not ok:
                return f"Failed to send Telegram message: {error}"
            text_to_send = out[:4096]
        else:
            try:
                text_to_send = out[:4096]
                sent = send_telegram_reply(
                    chat_id,
                    text_to_send,
                    voice_lang=voice_lang[:2].lower() if voice_lang else None,
                    file_path=str(file_path) if file_path else None,
                )
                if not sent:
                    return (
                        "Failed to send Telegram message: bridge callback did not accept the message "
                        "(callback missing or enqueue failure)."
                    )
            except Exception as e:
                return f"Failed to send Telegram message: {e}"

        # Append the sent message to the Telegram session so when the user replies (e.g. "Danke!"),
        # the agent has context (the proactive message and links) in that session.
        try:
            from vaf.core.session import SessionManager, Session
            session_id = f"telegram_{chat_id}"
            sm = SessionManager()
            try:
                session = sm.load(session_id, restore_state=False)
            except FileNotFoundError:
                session = Session(
                    id=session_id,
                    name=f"Telegram {chat_id}",
                )
            session.add_message(role="assistant", content=text_to_send)
            sm.save(session, sync_state=False)
        except Exception:
            pass  # Do not fail the tool if session append fails

        try:
            from vaf.core.user_notifications import append_notification
            preview = (text_to_send[:100] + "…") if len(text_to_send) > 100 else text_to_send
            append_notification(
                user_scope_id,
                kind="channel_reply",
                title="Message sent via Telegram",
                status="success",
                summary=preview,
                channel="Telegram",
            )
        except Exception:
            pass

        if voice_lang:
            return "Voice message sent to the user via Telegram."
        if file_path:
            return f"Message and document {file_path.name} sent to the user via Telegram."
        return "Message sent to the user via Telegram."
