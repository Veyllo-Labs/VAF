"""
Send a proactive message to the user via WhatsApp.
Only available when the user has WhatsApp linked; use main_messenger or user request to decide when to call this tool.
"""
import re

from vaf.tools.base import BaseTool


class SendWhatsAppTool(BaseTool):
    """
    Send a message to the user via WhatsApp.
    Use when the user asked you to send them something and they prefer WhatsApp or said "via WhatsApp".
    """
    name = "send_whatsapp"
    description = (
        "Send a message to the user via WhatsApp. "
        "Use when the user asked you to send them something (e.g. 'send me the result via WhatsApp' or when main_messenger is WhatsApp)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message text to send to the user on WhatsApp.",
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
            from vaf.core.messaging_connections import get_whatsapp_chat_jid
            from vaf.core.whatsapp_reply import send_whatsapp_reply
        except ImportError as e:
            return f"WhatsApp send unavailable: {e}"

        chat_jid = get_whatsapp_chat_jid(user_scope_id, username)
        if not chat_jid:
            return (
                "No WhatsApp contact found for this user. "
                "The user must link WhatsApp in Settings → Connections → WhatsApp (scan QR) and add their phone number to the whitelist. "
                "Once linked, you can send proactive messages."
            )

        # Strip <think>...</think> for clean delivery
        out = re.sub(r"<think>.*?</think>", "", message, flags=re.DOTALL)
        out = re.sub(r"\n{3,}", "\n\n", out).strip()
        if not out:
            out = "[No reply text]"

        try:
            send_whatsapp_reply(username, chat_jid, out)
        except Exception as e:
            return f"Failed to send WhatsApp message: {e}"

        return "Message sent to the user via WhatsApp."
