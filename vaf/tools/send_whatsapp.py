"""
Send a proactive message to the user via WhatsApp.
Supports text and voice messages (Sprachnachrichten), like Telegram.
"""
import re
import tempfile
from pathlib import Path

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
                "description": "The message text to send (or to speak, if voice_lang is set).",
            },
            "voice_lang": {
                "type": "string",
                "description": "Optional. Language code (e.g. 'de', 'en') to send as voice message (Sprachnachricht).",
            },
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
            from vaf.api.whatsapp_bridge import is_bridge_running, has_process_for_user
        except ImportError as e:
            return f"WhatsApp send unavailable: {e}"

        if not is_bridge_running():
            return (
                "WhatsApp bridge is not running. Start it in Settings → Connections → WhatsApp "
                "(click Start). The bridge must be connected for sends to work."
            )
        if not has_process_for_user(username):
            return (
                "WhatsApp process for this user is not running (bridge may have just started or auth expired). "
                "Try: Settings → Connections → WhatsApp → Stop, then Start. Ensure WhatsApp is linked (QR scanned) and your number is in the whitelist."
            )

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

        voice_lang = (kwargs.get("voice_lang") or "").strip()
        voice_path = None
        if voice_lang:
            voice_path = self._synthesize_voice(out, voice_lang[:2].lower())

        try:
            send_whatsapp_reply(username, chat_jid, out, voice_path=voice_path)
        except Exception as e:
            return f"Failed to send WhatsApp message: {e}"

        if voice_path:
            try:
                Path(voice_path).unlink(missing_ok=True)
            except Exception:
                pass
            return "Voice message sent to the user via WhatsApp."
        return "Message sent to the user via WhatsApp."

    def _synthesize_voice(self, text: str, lang: str):
        """Synthesize TTS to OGG file, return path. Returns None on failure."""
        try:
            import requests
            from vaf.core.config import Config
            tts_url = (Config.get("speech_tts_docker_url") or Config.get("speech_tts_docker_url_de") or "http://localhost:5002").strip().rstrip("/")
            if not tts_url:
                return None
            resp = requests.post(
                f"{tts_url}/synthesize",
                json={"text": text[:4000], "language": lang, "format": "ogg"},
                timeout=60,
            )
            if not resp.ok or not resp.content:
                return None
            data = resp.content
            if data[:4] != b"OggS" and data[:4] != b"RIFF":
                return None
            suffix = ".ogg" if data[:4] == b"OggS" else ".wav"
            with tempfile.NamedTemporaryFile(prefix="vaf_wa_", suffix=suffix, delete=False) as f:
                f.write(data)
                return f.name
        except Exception:
            return None
