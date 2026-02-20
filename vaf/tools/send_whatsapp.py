"""
Send a proactive message to the user via WhatsApp.
Supports text, voice messages (Sprachnachrichten), and documents (PDF, etc.) – WhatsApp as a channel where the bot can send the user content.
"""
import re
import tempfile
from pathlib import Path

from vaf.tools.base import BaseTool
from vaf.tools.filesystem import is_safe_path


def _resolve_path(path_str: str) -> tuple[Path | None, str | None]:
    """Resolve file path (folder aliases like Downloads, absolute paths). Returns (resolved_path, error_message)."""
    s = (path_str or "").strip()
    if not s:
        return None, None
    if s.lower().startswith("file://"):
        s = s[7:]
    safe, result = is_safe_path(s)
    if not safe:
        return None, result
    return Path(result), None


class SendWhatsAppTool(BaseTool):
    """
    Send content via WhatsApp: to the account owner (default) or to a contact (to_phone).
    Use to_phone when the user asks to send a message to someone (e.g. Alice); get the number from get_contact(name='Alice').
    """
    name = "send_whatsapp"
    description = (
        "Send content via WhatsApp: text, voice message (voice_lang), or document (file_path). "
        "Default: sends to the account owner. To send to a contact (e.g. Alice), use to_phone with the contact's WhatsApp number from get_contact(name='...'). "
        "When sending a voice message to a contact, use the contact's preferred_language for voice_lang (get_contact returns 'Preferred language: xx'); e.g. Alice speaks Turkish → voice_lang='tr'. "
        "Example: get_contact(name='Alice') then send_whatsapp(message='...', to_phone='+491761234567', voice_lang='tr')."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message text (or caption for documents; or text to speak if voice_lang is set).",
            },
            "to_phone": {
                "type": "string",
                "description": "Optional. E.164 phone number (e.g. +491761234567) to send to a contact instead of the owner. Use the contact's whatsapp_phone from get_contact when the user asks to send a message to someone (e.g. 'send to Alice').",
            },
            "voice_lang": {
                "type": "string",
                "description": "Optional. Language code for voice message (e.g. 'de', 'en', 'tr'). When sending to a contact, use the contact's preferred_language from get_contact.",
            },
            "file_path": {
                "type": "string",
                "description": "Optional. Full path to a file to send as document (PDF, DOCX, etc.). Use when user asks for a report, notes, or PDF via WhatsApp.",
            },
        },
        "required": ["message"]
    }

    def run(self, **kwargs) -> str:
        message = (kwargs.get("message") or "").strip()
        if not message:
            return "No message provided. Pass the message text (or caption for documents)."

        username = kwargs.get("username") or "admin"
        user_scope_id = kwargs.get("user_scope_id")

        try:
            from vaf.core.messaging_connections import get_whatsapp_chat_jid
            from vaf.api.whatsapp_bridge import (
                is_bridge_running,
                has_process_for_user,
                send_whatsapp_with_confirmation,
                _e164_to_jid,
            )
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

        to_phone = (kwargs.get("to_phone") or kwargs.get("phone_number") or "").strip()
        allow_contact_send = False
        if to_phone:
            chat_jid = _e164_to_jid(to_phone)
            if not chat_jid:
                return (
                    "Invalid phone number for to_phone. Use E.164 format (e.g. +491761234567). "
                    "Get the contact's whatsapp_phone from get_contact(name='...') when the user asks to send to a contact."
                )
            allow_contact_send = True
        else:
            chat_jid = get_whatsapp_chat_jid(user_scope_id, username)
            if not chat_jid:
                return (
                    "No WhatsApp contact found for this user. "
                    "The user must link WhatsApp in Settings → Connections → WhatsApp (scan QR) and add their phone number to the whitelist. "
                    "Once linked, you can send proactive messages."
                )

        # Strip <think>...</think> and internal system phrases for clean delivery
        out = re.sub(r"<think>.*?</think>", "", message, flags=re.DOTALL)
        out = re.sub(r"\n{3,}", "\n\n", out).strip()
        # Safety: block messages that contain internal/system-level content
        try:
            from vaf.core.headless_runner import _sanitize_outgoing_message
            out = _sanitize_outgoing_message(out)
        except Exception:
            pass
        if not out:
            return "Message was blocked (contained internal system content). Send a clean user-facing message without any internal context markers."

        voice_lang = (kwargs.get("voice_lang") or "").strip()
        file_path_str = (kwargs.get("file_path") or "").strip()
        voice_path = None
        document_path = None

        if file_path_str:
            resolved, path_error = _resolve_path(file_path_str)
            if path_error:
                return path_error
            if resolved and resolved.is_file():
                document_path = str(resolved.resolve())
            else:
                return f"File not found or not a file: {file_path_str}"
        elif voice_lang:
            voice_path = self._synthesize_voice(out, voice_lang[:2].lower())
            if not voice_path:
                return (
                    "Voice message could not be generated (TTS failed). "
                    "Check Settings → Speech / TTS: is the TTS service running (speech_tts_docker_url, e.g. http://localhost:5002)? "
                    "You can send the same text as a normal message without voice_lang."
                )
            if Path(voice_path).stat().st_size == 0:
                try:
                    Path(voice_path).unlink(missing_ok=True)
                except Exception:
                    pass
                return "TTS produced an empty file. Cannot send voice message. Send as text instead (omit voice_lang)."

        try:
            # Voice/document use longer timeout in bridge (TTS + upload)
            result = send_whatsapp_with_confirmation(
                username, chat_jid, out,
                voice_path=voice_path,
                document_path=document_path,
                timeout=45.0 if (voice_path or document_path) else 15.0,
                allow_contact_send=allow_contact_send,
            )
        except Exception as e:
            return f"Failed to send WhatsApp message: {e}"
        finally:
            if voice_path:
                try:
                    Path(voice_path).unlink(missing_ok=True)
                except Exception:
                    pass

        return result

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
