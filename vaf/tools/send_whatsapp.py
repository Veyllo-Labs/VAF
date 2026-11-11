# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Send a proactive message to the user via WhatsApp.
Supports text, voice messages (Sprachnachrichten), and documents (PDF, etc.) – WhatsApp as a channel where the bot can send the user content.
"""
import re
import tempfile
from pathlib import Path

from vaf.tools.base import BaseTool
from vaf.tools.send_telegram import _resolve_path


class SendWhatsAppTool(BaseTool):
    """
    Send content via WhatsApp from the agent's own linked number: to a contact or any number
    (to_phone), or to the account owner's registered main-user number (default, no to_phone).
    Use to_phone when the user asks to send a message to someone (e.g. Alice); get the number from get_contact(name='Alice').
    """
    name = "send_whatsapp"
    agent_request = "hold"
    category    = "whatsapp"
    identity_kwargs = ("user_role", "user_scope_id", "username")
    # "write", not "read" - the mode names the ROOT SET, not the operation: "read" would
    # make skill files shared by OTHER users attachable to an outgoing message.
    file_access = "write"
    permission_level = "write"
    side_effect_class = "irreversible"
    # What a result says became of the message, for a draft the outward hold sends again
    # (vaf/core/outbound_hold.py): "sent" uses the draft up, "unconfirmed" leaves it
    # ambiguous because it may have left, anything else is a failure and the draft waits.
    # Lower-case prefixes of the bridge's own words (whatsapp_bridge.SENT_TEXTS and
    # SEND_UNCONFIRMED), kept in step by tests/test_outbox_send_outcome.py.
    delivery_markers = {
        "sent": ("message sent via whatsapp", "voice message sent via whatsapp",
                 "document sent via whatsapp"),
        "unconfirmed": ("no delivery confirmation",),
    }
    description = (
        "Send content via WhatsApp from the agent's own linked number: text, voice message (voice_lang), or document (file_path). "
        "Default (no to_phone): sends to the account owner's registered main-user number. To send to a contact (e.g. Alice) or any number, use to_phone with the contact's WhatsApp number from get_contact(name='...'). "
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

        # GUARD: in a contact conversation (Front Office) this tool has one legitimate
        # recipient, the owner's registered number. The reply to the contact is delivered
        # by the headless runner, so a send to the contact's or any other number would
        # duplicate it or turn a contact's turn into a messenger for strangers; and the
        # owner's back-channel (send_whatsapp without to_phone when main_messenger is
        # whatsapp) must go through. The agent exposes _front_office_mode while it handles
        # a contact's message (injected as the _agent kwarg).
        _agent = kwargs.get("_agent")
        front_office = bool(_agent is not None and getattr(_agent, "_front_office_mode", False))

        try:
            from vaf.core.messaging_connections import get_whatsapp_chat_jid
            from vaf.api.whatsapp_bridge import (
                send_whatsapp_with_confirmation,
                _phone_digits_canonical,
            )
            from vaf.core.contacts_store import whatsapp_send_jid
        except ImportError as e:
            return f"WhatsApp send unavailable: {e}"

        from vaf.core.outbound_hold import whatsapp_recipient
        from vaf.core.shown_text import phone_digits
        to_phone = whatsapp_recipient(kwargs)
        allow_contact_send = False
        if to_phone:
            # Plain digits only: the bridge keeps every character whose isdigit() is true (a
            # digit of another script lands in the JID) and drops letters, so "... ext 99"
            # would reach another number than the one written.
            if phone_digits(to_phone) is None:
                return (
                    "Invalid phone number for to_phone: only the digits 0-9, a leading '+' and the "
                    "separators space - . / ( ) are allowed; letters, extensions and digits of other "
                    "scripts are refused. Use E.164 format (e.g. +491761234567). Get the contact's "
                    "whatsapp_phone from get_contact(name='...') when the user asks to send to a contact."
                )
            chat_jid = whatsapp_send_jid(to_phone)
            if not chat_jid:
                return (
                    "Invalid phone number for to_phone. Use E.164 format (e.g. +491761234567). "
                    "Get the contact's whatsapp_phone from get_contact(name='...') when the user asks to send to a contact."
                )
            if front_office:
                owner_jid = get_whatsapp_chat_jid(user_scope_id, username)
                # The same JID, or the same non-empty number. A LID (and a group) canonicalises
                # to "", so comparing digits alone would let every LID-mapped number pass as
                # the owner whenever the owner's own endpoint is a LID.
                owner_digits = _phone_digits_canonical(owner_jid or "")
                same_owner = bool(owner_jid) and (
                    owner_jid == chat_jid
                    or (bool(owner_digits) and owner_digits == _phone_digits_canonical(chat_jid))
                )
                if not same_owner:
                    return (
                        "[TOOL BLOCKED] You are handling a contact's message: send_whatsapp reaches only the "
                        "account owner here. Leave to_phone empty to notify them. Your reply to the contact "
                        "is delivered automatically, and nobody else can be messaged from this conversation."
                    )
            allow_contact_send = True
        else:
            chat_jid = get_whatsapp_chat_jid(user_scope_id, username)
            if not chat_jid:
                return (
                    "No main-user WhatsApp number is registered for this user, so there is no default recipient. "
                    "Pass to_phone (E.164) to message a contact or any number from the agent's own linked number, "
                    "or ask the user to register the number they chat from in Settings → Connections → WhatsApp."
                )

        # Strip <think>...</think> and internal system phrases for clean delivery
        out = re.sub(r"<think>.*?</think>", "", message, flags=re.DOTALL)
        out = re.sub(r"\n{3,}", "\n\n", out).strip()
        # Safety: block messages that contain internal/system-level content
        try:
            from vaf.core.outbound_sanitizer import sanitize_outgoing_message
            out = sanitize_outgoing_message(out)
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
                # Deliberate: the UNRESOLVED path goes to the bridge, like every other
                # sender. Resolving here would hand out the link target after the check
                # ran (the old check-then-resolve ordering bug); is_safe_path already
                # vetted the real target itself.
                document_path = str(resolved)
            else:
                return f"File not found or not a file: {file_path_str}"
        elif voice_lang:
            from vaf.core.fair_queue import QueueBusy
            try:
                voice_path = self._synthesize_voice(out, voice_lang[:2].lower(), user_scope_id)
            except QueueBusy:
                # A load problem, not a setup problem: no settings hint.
                return ("Voice message not sent: the speech service is busy right now. "
                        "Send the same text as a normal message without voice_lang, "
                        "or try the voice message again in a minute.")
            if not voice_path:
                return (
                    "Voice message could not be generated (TTS failed). "
                    "Check Settings → Voice & Speech: is a text-to-speech provider set, or, on an installation "
                    "with local models, is the TTS service running (speech_tts_docker_url, e.g. http://localhost:5002)? "
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

        try:
            from vaf.core.user_notifications import append_notification
            preview = (out[:100] + "…") if len(out) > 100 else out
            append_notification(
                user_scope_id,
                kind="channel_reply",
                title="Message sent via WhatsApp",
                status="success",
                summary=preview,
                channel="WhatsApp",
            )
        except Exception:
            pass

        return result

    def _synthesize_voice(self, text: str, lang: str, user_key=None):
        """Synthesize TTS to OGG file, return path. Returns None on failure; raises
        fair_queue.QueueBusy when the TTS queue gave no slot within WAIT_LIVE_S (a tool
        call has a fixed budget, the agent can fall back to text). `user_key` is the
        caller's scope, the person it waits for."""
        from vaf.core.fair_queue import QueueBusy
        try:
            from vaf.core import speech_client
            from vaf.core.config import Config
            # Unique legacy fallback chain for this tool: default URL -> German
            # per-language URL -> hardcoded default.
            tts_url = (Config.get("speech_tts_docker_url") or Config.get("speech_tts_docker_url_de") or "http://localhost:5002").strip().rstrip("/")
            if not tts_url:
                return None
            data = speech_client.synthesize(text[:4000], lang, want_format="ogg", docker_url=tts_url,
                                            user_key=user_key,
                                            deadline_s=speech_client.WAIT_LIVE_S,
                                            busy_raises=True)
            if not data or data[:4] not in (b"OggS", b"RIFF"):
                return None
            suffix = ".ogg" if data[:4] == b"OggS" else ".wav"
            with tempfile.NamedTemporaryFile(prefix="vaf_wa_", suffix=suffix, delete=False) as f:
                f.write(data)
                return f.name
        except QueueBusy:
            raise
        except Exception:
            return None
