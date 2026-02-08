"""
Send a proactive message to the user via Discord.
Only available when the user has Discord connected. Implementation planned for Phase 2.
"""

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
                "description": "The message text to send to the user on Discord."
            }
        },
        "required": ["message"]
    }

    def run(self, **kwargs) -> str:
        return "Discord proactive messaging is not yet implemented (Phase 2). Use Telegram for now."
