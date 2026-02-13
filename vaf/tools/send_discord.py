"""
Send a proactive message to the user via Discord.
Only available when the user has Discord connected; use main_messenger or user request to decide when to call this tool.
"""
import re

from vaf.tools.base import BaseTool


class SendDiscordTool(BaseTool):
    """
    Send a message to the user via Discord.
    Use when the user asked you to send them something and they prefer Discord or said "via Discord".
    """
    name = "send_discord"
    description = (
        "Send a message to the user via Discord. "
        "Use when the user asked you to send them something (e.g. 'send me the result via Discord' or when main_messenger is Discord)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message text to send to the user on Discord.",
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
            from vaf.core.messaging_connections import get_discord_user_id
            from vaf.core.config import Config
            from vaf.core.discord_send import send_discord_dm
        except ImportError as e:
            return f"Discord send unavailable: {e}"

        user_id = get_discord_user_id(user_scope_id, username)
        if not user_id:
            return (
                "No Discord contact found. "
                "The user must complete Discord setup in Settings → Connections → Discord. "
                "Once verified, you can send proactive messages via Discord."
            )

        discord_config = Config.get("discord_config") or {}
        bot_token = (discord_config.get("bot_token") or "").strip()
        if not bot_token:
            return "Discord bot token missing. Please complete Discord setup in Settings → Connections."

        # Strip <think>...</think> for clean delivery
        out = re.sub(r"<think>.*?</think>", "", message, flags=re.DOTALL)
        out = re.sub(r"\n{3,}", "\n\n", out).strip()
        if not out:
            out = "[No reply text]"

        try:
            ok = send_discord_dm(bot_token, user_id, out, chunk=True)
            if not ok:
                return "Failed to send Discord message. Check bot token and user permissions."
        except Exception as e:
            return f"Failed to send Discord message: {e}"

        return "Message sent to the user via Discord."
