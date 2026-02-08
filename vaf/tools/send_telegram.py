"""
Send a proactive message to the user via Telegram.
Only available when the user has Telegram connected; use main_messenger or user request to decide when to call this tool.
"""

from vaf.tools.base import BaseTool


class SendTelegramTool(BaseTool):
    """
    Send a message to the user via Telegram.
    Use when the user asked you to send them something (e.g. a summary, result, or notification) and they prefer Telegram or said "via Telegram".
    """
    name = "send_telegram"
    description = (
        "Send a message to the user via Telegram. "
        "Use when the user asked you to send them something (e.g. 'send me the result via Telegram' or when main_messenger is Telegram)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message text to send to the user on Telegram."
            }
        },
        "required": ["message"]
    }

    def run(self, **kwargs) -> str:
        message = (kwargs.get("message") or "").strip()
        if not message:
            return "No message provided. Pass the message text to send."

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
        try:
            text_to_send = message[:4096]
            send_telegram_reply(chat_id, text_to_send)
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

        return "Message sent to the user via Telegram."
