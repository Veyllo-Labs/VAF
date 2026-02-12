"""
Send a proactive message to the user via Telegram.
Only available when the user has Telegram connected; use main_messenger or user request to decide when to call this tool.
Supports optional file attachments (e.g. documents, PDFs) when the user asks for a document.
"""

from pathlib import Path

from vaf.tools.base import BaseTool


def _resolve_path(path_str: str) -> Path | None:
    """Resolve file path (supports file:// URLs and absolute/relative paths). Returns None if invalid."""
    s = (path_str or "").strip()
    if not s:
        return None
    if s.lower().startswith("file://"):
        s = s[7:]
    return Path(s).resolve()


class SendTelegramTool(BaseTool):
    """
    Send a message to the user via Telegram, optionally with a document attachment.
    Use when the user asked you to send them something (e.g. a summary, result, notification, or document)
    and they prefer Telegram or said "via Telegram".
    For documents (invoices, contracts, PDFs): pass file_path after creating/finding the file.
    """
    name = "send_telegram"
    description = (
        "Send a message to the user via Telegram. "
        "Use when the user asked you to send them something (e.g. 'send me the result via Telegram' or when main_messenger is Telegram). "
        "When sending a document (invoice, contract, PDF, etc.), pass file_path with the full path to the file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message text (caption) to send. For documents, use as caption (e.g. 'Here is your invoice').",
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
        file_path: Path | None = _resolve_path(file_path_str) if file_path_str else None

        username = kwargs.get("username") or "admin"
        user_scope_id = kwargs.get("user_scope_id")

        try:
            from vaf.core.messaging_connections import get_telegram_chat_id
            from vaf.core.telegram_reply import send_telegram_reply
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

        try:
            text_to_send = message[:4096]
            send_telegram_reply(chat_id, text_to_send, file_path=str(file_path) if file_path else None)
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

        if file_path:
            return f"Message and document {file_path.name} sent to the user via Telegram."
        return "Message sent to the user via Telegram."
