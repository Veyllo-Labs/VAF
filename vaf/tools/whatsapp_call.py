"""
Placeholder for WhatsApp voice/video calls.
Not implemented yet.
"""

from vaf.tools.base import BaseTool


class WhatsAppCallTool(BaseTool):
    """
    Placeholder for WhatsApp voice or video calls. Not implemented yet.
    """
    name = "whatsapp_call"
    permission_level = "write"
    side_effect_class = "irreversible"
    description = (
        "Placeholder: Initiate a WhatsApp voice or video call. Not implemented yet. "
        "Use send_whatsapp to send a text message instead."
    )
    parameters = {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "Chat/contact to call (e.g. +49123456789).",
            },
            "call_type": {
                "type": "string",
                "description": "voice or video. Not implemented.",
            },
        },
        "required": ["chat_id"],
    }

    def run(self, **kwargs) -> str:
        return (
            "WhatsApp calls are not implemented yet. Use send_whatsapp to send a text message, "
            "or send_whatsapp with voice_lang to send a voice message."
        )
