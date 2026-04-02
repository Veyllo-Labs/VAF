"""
Send a proactive message to the user via Slack.
Only available when the user has Slack connected. Not yet supported.
"""

from vaf.tools.base import BaseTool


class SendSlackTool(BaseTool):
    """
    Send a message to the user via Slack.
    Use when the user asked you to send them something and they prefer Slack or said "via Slack".
    """
    name = "send_slack"
    permission_level = "write"
    side_effect_class = "irreversible"
    description = (
        "Send a message to the user via Slack. "
        "Use when the user asked you to send them something (e.g. 'send me the result via Slack' or when main_messenger is Slack)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message text to send to the user on Slack."
            }
        },
        "required": ["message"]
    }

    def run(self, **kwargs) -> str:
        return "Slack proactive messaging is not yet supported."
