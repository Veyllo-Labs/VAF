# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Long-lived Telegram bridge: receives messages from Telegram, enqueues tasks with
user_scope_id/username from whitelist, and sends replies via the telegram_reply hook.
Started/stopped from telegram_routes (POST /api/telegram/start, /stop).
Messages are debounced per chat: wait N seconds for follow-up messages, then send combined text.

Voice messages are transcribed via Docker Whisper STT and the detected language is stored
so TTS responses can be sent back in the same language.
"""
import asyncio
import base64
import html
import logging
import os
import queue
import re
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

# SECURITY: the Telegram Bot API carries the BOT TOKEN in the URL path
# (api.telegram.org/bot<TOKEN>/getUpdates), and httpx logs every request URL
# at INFO level - so the default logging printed the token to the terminal
# and into the log files on every polling tick (live incident: a user
# copy-pasted it from there twice; the token had to be revoked). Silence
# httpx/httpcore request logging BEFORE any client is created.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

from vaf.core.config import Config
from vaf.core.channel_secrets import channel_secret
from vaf.core.channel_ingress_policy import evaluate_ingress, should_log_unauthorized
from vaf.core.task_queue import TaskQueue
from vaf.core.telegram_reply import set_telegram_reply_callback
from vaf.core.tray_context import TrayContext

logger = logging.getLogger("vaf.api.telegram_bridge")

_bridge_thread: Optional[threading.Thread] = None
_sender_thread: Optional[threading.Thread] = None
_bridge_stop = threading.Event()
_bridge_stop_requested = False
_outgoing_queue: Optional[queue.Queue] = None

# Per-chat debounce: wait for follow-up messages, then enqueue combined text
_pending_by_chat: Dict[str, Dict[str, Any]] = {}
_pending_lock = threading.Lock()

# The person's recent text messages per chat session, in memory only: (time, chat id, message
# id, text). When the agent stores a credential it read in one of them (store_credential,
# vaf/core/forget_secrets.py), the bridge tries to delete that message in the Telegram chat as
# well - a burst of messages is one turn, so the one carrying the value has to be found by its
# text. An attempt, logged either way: a bot may delete a person's message in a private chat
# for 48 hours, the Bot API can refuse, and what arrived before this process started is unknown.
_RECENT_INBOUND_MAX = 50
_RECENT_INBOUND_TTL_S = 48 * 3600
_recent_inbound: Dict[str, List[Tuple[float, str, str, str]]] = {}
_recent_lock = threading.Lock()


def _remember_inbound(session_id: str, chat_id: str, message_id: Any, text: str) -> None:
    with _recent_lock:
        entries = _recent_inbound.setdefault(session_id, [])
        entries.append((time.time(), str(chat_id), str(message_id), text))
        del entries[:-_RECENT_INBOUND_MAX]


def _delete_telegram_message(chat_id: str, message_id: str) -> bool:
    bot_token = channel_secret("telegram")
    if not bot_token:
        return False
    try:
        resp = requests.post(f"https://api.telegram.org/bot{bot_token}/deleteMessage",
                             data={"chat_id": chat_id, "message_id": message_id}, timeout=15)
        return bool(resp.ok and (resp.json() or {}).get("ok"))
    except Exception:
        return False


def _forget_listener(env=None, session_id=None, transcript_scrubbed=False, **_):
    """Delete the person's Telegram message that carried a credential the agent just stored."""
    if transcript_scrubbed or not env or not str(session_id or "").startswith("telegram_"):
        return
    now = time.time()
    with _recent_lock:
        entries = list(_recent_inbound.get(str(session_id), ()))
    carrying = [e for e in entries
                if now - e[0] <= _RECENT_INBOUND_TTL_S and any(v in e[3] for v in env.values())]
    from vaf.core.log_helper import log_telegram_reply
    for entry in carrying:
        deleted = _delete_telegram_message(entry[1], entry[2])
        # Into the bridge's own lane log: the module logger's INFO never reached a file.
        log_telegram_reply(f"CREDENTIAL_MESSAGE {'deleted' if deleted else 'not deleted'} "
                           f"chat_id={entry[1]} message_id={entry[2]}")
    if carrying:
        with _recent_lock:
            kept = [e for e in _recent_inbound.get(str(session_id), ()) if e not in carrying]
            _recent_inbound[str(session_id)] = kept


try:
    from vaf.core.forget_secrets import add_listener as _add_forget_listener
    _add_forget_listener(_forget_listener)
except Exception:
    pass

# Accumulated RAG documents per Telegram session (session_id -> [{name, content, ...}]).
# index_session_attachments_sync REPLACES a session's index, so we keep the full set and
# re-index it on each new document. In-memory (reset on restart); the agent always also sees
# the freshly-sent document inline this turn, so a reset only forgets *older* docs from RAG.
_telegram_session_documents: Dict[str, list] = {}


def _store_telegram_message(username, chat_id, body, direction, content_type="text",
                            user_scope_id=None, message_id=None) -> None:
    """Record a Telegram message in the shared channel message store (channel_message_store,
    channel='telegram') so the agent's read_telegram_chat / find_telegram_messages tools can read
    history — the Telegram equivalent of the WhatsApp bridge's append_message calls. Best-effort."""
    try:
        from vaf.core.channel_message_store import append_message
        append_message(
            username=str(username or "admin"), chat_id=str(chat_id or ""), body=str(body or ""),
            direction=direction, content_type=content_type, message_id=message_id,
            channel="telegram", user_scope_id=user_scope_id,
        )
    except Exception:
        pass


def _to_telegram_html(text: str) -> str:
    """
    Convert a small Markdown-like subset to Telegram HTML.
    Supported: **bold**, `inline code`, [label](https://url), and fenced code blocks.
    """
    if not text:
        return ""
    escaped = html.escape(text, quote=False)

    # Fenced code blocks first so inner markers are not transformed.
    escaped = re.sub(
        r"```([\s\S]*?)```",
        lambda m: f"<pre>{m.group(1).strip()}</pre>",
        escaped,
    )
    # Inline links (http/https only).
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        escaped,
    )
    # Inline code.
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
    # Bold with double-asterisk.
    escaped = re.sub(r"\*\*([^\n*][^*]*?)\*\*", r"<b>\1</b>", escaped)
    return escaped


def _telegram_text_payload(chat_id: str, text: str) -> Dict[str, Any]:
    """Build sendMessage payload; prefer formatted HTML when markdown markers are present."""
    raw = str(text or "")[:4096]
    if any(marker in raw for marker in ("**", "`", "[", "```")):
        return {"chat_id": chat_id, "text": _to_telegram_html(raw), "parse_mode": "HTML"}
    return {"chat_id": chat_id, "text": raw}


def _telegram_caption_data(chat_id: str, caption: str) -> Dict[str, Any]:
    """Build sendDocument data with optional HTML parse mode."""
    raw = str(caption or "")[:1024]
    if any(marker in raw for marker in ("**", "`", "[", "```")):
        return {"chat_id": chat_id, "caption": _to_telegram_html(raw), "parse_mode": "HTML"}
    return {"chat_id": chat_id, "caption": raw}


def _drop_unauthorized_telegram(
    telegram_user_id: str,
    chat_id: str,
    message_kind: str = "text",
    reason: str = "not_paired",
    update: Any = None,
) -> None:
    """
    Drop unauthorized inbound Telegram traffic for the AGENT: nothing runs on it, and the
    log is throttled per user to avoid amplification under abuse. The message itself is
    kept in the channel store for the OWNER (the dashboard and the inbox tools read it),
    the way the WhatsApp bridge keeps a rejected sender's message: the ingress policy
    decides who is answered, not what the owner may read on their own bot. `update` is the
    python-telegram-bot Update when the caller has one; its text (or caption, or a
    placeholder naming the kind) and message id are what gets stored.
    """
    uid = str(telegram_user_id or "")
    msg = getattr(update, "effective_message", None) if update is not None else None
    if msg is not None:
        body = (getattr(msg, "text", None) or getattr(msg, "caption", None) or "").strip() or f"<{message_kind}>"
        _store_telegram_message("admin", chat_id, body, "in", content_type=message_kind if message_kind in ("voice", "photo", "document") else "text",
                                message_id=str(getattr(msg, "message_id", "") or "") or None)
    policy = Config.get("channel_ingress_policy")
    # One REJECT line per sender and throttle window in the channel's own inbound log,
    # written with debug logging off too: the sender gets no reply, so this is the only
    # trace of the attempt. Not a security event: a stranger writing to the bot is the
    # channel's everyday traffic (the message is kept for the owner above).
    if should_log_unauthorized("telegram", uid, policy):
        logger.warning(
            "Dropped unauthorized Telegram %s message from user_id=%s chat_id=%s reason=%s",
            message_kind,
            uid,
            str(chat_id or ""),
            reason,
        )
        try:
            from vaf.core.log_helper import log_channel_inbound
            log_channel_inbound("telegram", f"REJECT {reason} user_id={uid} chat_id={str(chat_id or '')} kind={message_kind}",
                                always=True)
        except Exception:
            pass


def _whitelist_lookup(telegram_user_id: str) -> Optional[Dict[str, Any]]:
    """Return admin whitelist entry for this telegram_user_id or None (full agent access)."""
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        return None
    whitelist: List[Dict[str, Any]] = telegram_config.get("whitelist") or []
    for entry in whitelist:
        if str(entry.get("telegram_user_id")) == str(telegram_user_id):
            return entry
    return None


def _relay_whitelist_lookup(telegram_user_id: str) -> Optional[Dict[str, Any]]:
    """Return relay whitelist entry for this telegram_user_id or None (relay-only, no tools)."""
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        return None
    relay: List[Dict[str, Any]] = telegram_config.get("relay_whitelist") or []
    for entry in relay:
        if str(entry.get("telegram_user_id")) == str(telegram_user_id):
            return entry
    return None


def _resolve_telegram_user(telegram_user_id: str, sender: Any = None) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Resolve telegram_user_id to (whitelist/relay/contact entry, is_relay). Returns (None, False) if not allowed.

    A sender is answered only while the Telegram lane of the account they belong to is
    switched on (`messaging_connections.channel_enabled_for_scope`): the account's own
    pairing, its relay contacts and its contacts alike. The switch was shown and stored per
    account while the bot answered every paired account regardless. A sender refused here is
    treated like any other unauthorized one: nothing answers, the message is kept.
    """
    entry, is_relay = _resolve_telegram_sender(telegram_user_id, sender)
    if entry is not None:
        from vaf.core.messaging_connections import channel_enabled_for_scope
        if not channel_enabled_for_scope("telegram", entry.get("user_scope_id")):
            return (None, False)
    return (entry, is_relay)


def _pair_with_code(telegram_user_id: str, telegram_username: Optional[str], code: str) -> Optional[str]:
    """Pair the sender with the account that holds this code; the bot's answer, or None when
    the code is not a live one (the caller then treats the message like any other)."""
    from vaf.core.channel_pairing import pair_telegram_account, redeem_pairing_code
    who = redeem_pairing_code("telegram", code)
    if who is None:
        return None
    scope, username = who
    result = pair_telegram_account(telegram_user_id, telegram_username, user_scope_id=scope,
                                   username=username, may_take_over=False, switch_on=True)
    if result == "taken":
        return ("This Telegram account is already paired with another VAF account. "
                "An admin can move it in Settings, Connections, Telegram.")
    return f"Paired: this Telegram account now belongs to the VAF account {username}. Write here to reach your agent."


def _resolve_telegram_sender(telegram_user_id: str, sender: Any = None) -> Tuple[Optional[Dict[str, Any]], bool]:
    """The entry a sender resolves to, before the owning account's switch is asked.

    `sender` is the Telegram user object when the caller has one: the name an open Front
    Office gives the contact record it creates for a new sender."""
    policy = Config.get("channel_ingress_policy")
    entry = _whitelist_lookup(telegram_user_id)
    if entry:
        allowed, _ = evaluate_ingress("telegram", policy, explicit_match=True)
        if allowed:
            return (entry, False)
        return (None, False)
    entry = _relay_whitelist_lookup(telegram_user_id)
    if entry:
        allowed, _ = evaluate_ingress("telegram", policy, explicit_match=True)
        if allowed:
            return (entry, True)
        return (None, False)
    try:
        from vaf.core.messaging_connections import get_contact_whitelist_telegram_entry
        entry = get_contact_whitelist_telegram_entry(telegram_user_id)
        if entry:
            # The entry exists because a contact carries this Telegram id AND the owner
            # allowed them, so the decision is "allowed"; the policy is still asked, because
            # a later veto on that record has to win here too.
            allowed, _ = evaluate_ingress("telegram", policy, explicit_match=False,
                                          access=_contact_access_for_telegram(telegram_user_id, entry))
            if allowed:
                return (entry, False)
    except Exception:
        pass
    return _open_front_office_entry(telegram_user_id, policy, sender)


def _contact_access_for_telegram(telegram_user_id: str, entry: Dict[str, Any]) -> Optional[str]:
    """What the owner decided about the contact behind this Telegram id, read from the record
    itself rather than from the fact that a lookup returned an entry: the entry is built from
    the allowed set, so it cannot see a denial that arrived afterwards."""
    try:
        from vaf.core.contacts_store import contact_access, find_contact_by_channel
        rec = find_contact_by_channel("telegram", str(telegram_user_id or ""),
                                      (entry or {}).get("vaf_username") or "admin",
                                      (entry or {}).get("user_scope_id"))
        return contact_access(rec)
    except Exception:
        return None


def _open_front_office_entry(telegram_user_id: str, policy: Any, sender: Any) -> Tuple[Optional[Dict[str, Any]], bool]:
    """A sender the open Front Office on Telegram lets in (Settings, Connections): answered
    as a Front Office contact of the ONE owner this bot serves, enrolled in that owner's
    book WITHOUT a decision: the open channel is what answers them, and the record is there
    so the owner can allow or deny them by hand. The single-owner condition is the
    framework's (`messaging_connections.front_office_open` / `single_telegram_owner`), because
    the inbox rows and the channel windows have to give the same answer: a row that says the
    agent answers in a chat this function refuses is a lie the person acts on. The bot is
    shared by every account on the install, a WhatsApp number is not."""
    try:
        from vaf.core.messaging_connections import front_office_open, single_telegram_owner
        if not front_office_open("telegram", policy):
            return (None, False)
        owner = single_telegram_owner()
        if owner is None:
            return (None, False)
        scope, uname = owner
        from vaf.core.contacts_store import admit_front_office_sender
        name = str(getattr(sender, "full_name", None) or getattr(sender, "username", None) or "").strip()
        allowed, _reason, rec = admit_front_office_sender(
            "telegram", telegram_user_id, username=uname, user_scope_id=scope or None,
            raw_policy=policy, display_name=name)
        if not allowed or rec is None:
            return (None, False)
        return ({
            "user_scope_id": scope or None,
            "vaf_username": uname,
            "telegram_user_id": str(telegram_user_id),
            "from_contact": True,
        }, False)
    except Exception:
        return (None, False)


def _append_chat_activity(chat_id: str, user_scope_id: Any, direction: str = "in") -> None:
    """Append one activity entry for the dashboard timeline (keeps last 100)."""
    from vaf.core.messaging_connections import append_channel_activity
    append_channel_activity("telegram", {"chat_id": str(chat_id), "user_scope_id": str(user_scope_id) if user_scope_id else None,
                                    "ts": time.time(), "direction": direction}, keep=100)


async def _transcribe_voice(bot_token: str, file_id: str) -> tuple[Optional[str], Optional[str]]:
    """
    Download voice message from Telegram and transcribe via Docker Whisper STT.
    Returns (transcribed_text, detected_language) or (None, None) on error.
    """
    try:
        # 1. Get file path from Telegram
        # to_thread: requests is blocking, and this coroutine runs on the bridge's own
        # event loop - a direct call would stall every other Telegram message until the
        # transfer finishes (up to the timeouts below).
        file_url = f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}"
        resp = await asyncio.to_thread(requests.get, file_url, timeout=10)
        if not resp.ok:
            logger.warning(f"Failed to get file info: {resp.status_code}")
            return None, None
        file_info = resp.json()
        file_path = file_info.get("result", {}).get("file_path")
        if not file_path:
            logger.warning("No file_path in response")
            return None, None

        # 2. Download the voice file (usually .oga format - Opus in Ogg container)
        download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
        audio_resp = await asyncio.to_thread(requests.get, download_url, timeout=30)
        if not audio_resp.ok:
            logger.warning(f"Failed to download voice file: {audio_resp.status_code}")
            return None, None

        from vaf.core.threat_db import refuse_known_bad
        if refuse_known_bad(audio_resp.content, filename=os.path.basename(file_path),
                            origin="telegram"):
            return None, None      # not written, not transcribed

        # 3. Save to temp file
        suffix = ".ogg" if file_path.endswith(".oga") or file_path.endswith(".ogg") else ".oga"
        with tempfile.NamedTemporaryFile(prefix="vaf_", suffix=suffix, delete=False) as temp_file:
            temp_file.write(audio_resp.content)
            temp_path = temp_file.name

        try:
            # 4. Transcribe via the shared speech client (Docker Whisper STT)
            from vaf.core import speech_client
            text, language = speech_client.transcribe(
                temp_path, mime="audio/ogg", filename="voice.ogg"
            )
            if not text:
                return None, None
            return text, language or "en"

        finally:
            # Cleanup temp file
            try:
                os.unlink(temp_path)
            except Exception:
                pass

    except Exception as e:
        logger.exception(f"Voice transcription error: {e}")
        return None, None


async def _send_voice_reply(bot_token: str, chat_id: str, text: str, language: str) -> bool:
    """
    Synthesize TTS audio and send as voice message to Telegram.
    Returns True on success.
    """
    try:
        # 1. Synthesize via the shared speech client (OGG preferred; the client
        #    already tries container-side and local ffmpeg conversion).
        from vaf.core import speech_client
        audio_data = speech_client.synthesize(text, language, want_format="ogg")

        if not audio_data:
            logger.warning("TTS returned no audio")
            return False

        if audio_data[:4] == b"OggS":
            ogg_data = audio_data
        elif audio_data[:4] == b"RIFF":
            # Client could not convert (no ffmpeg): send the WAV as a document.
            with tempfile.NamedTemporaryFile(prefix="vaf_", suffix=".wav", delete=False) as wav_file:
                wav_file.write(audio_data)
                wav_path = wav_file.name
            try:
                return await _send_audio_as_document(bot_token, chat_id, wav_path)
            finally:
                try:
                    os.unlink(wav_path)
                except Exception:
                    pass
        else:
            logger.warning(f"TTS returned unknown format: {audio_data[:10]}")
            return False

        # 2. Send voice message to Telegram
        send_url = f"https://api.telegram.org/bot{bot_token}/sendVoice"

        # Create temp file for upload
        with tempfile.NamedTemporaryFile(prefix="vaf_", suffix=".ogg", delete=False) as ogg_file:
            ogg_file.write(ogg_data)
            ogg_path = ogg_file.name

        try:
            with open(ogg_path, "rb") as f:
                resp = await asyncio.to_thread(
                    requests.post,
                    send_url,
                    data={"chat_id": chat_id},
                    files={"voice": ("response.ogg", f, "audio/ogg")},
                    timeout=30,
                )

            if not resp.ok:
                logger.warning(f"Failed to send voice: {resp.status_code} - {resp.text[:200]}")
                return False

            logger.info(f"Voice reply sent to chat {chat_id}")
            return True

        finally:
            try:
                os.unlink(ogg_path)
            except Exception:
                pass

    except Exception as e:
        logger.exception(f"Voice reply error: {e}")
        return False


async def _download_telegram_file(bot_token: str, file_id: str, suffix: str = "") -> Optional[str]:
    """
    Download a file from Telegram by file_id. Returns path to temp file or None on error.
    Caller must delete the temp file when done.
    """
    try:
        file_url = f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}"
        resp = await asyncio.to_thread(requests.get, file_url, timeout=10)
        if not resp.ok:
            logger.warning("getFile failed: %s", resp.status_code)
            return None
        file_info = resp.json()
        tg_path = file_info.get("result", {}).get("file_path")
        if not tg_path:
            return None
        download_url = f"https://api.telegram.org/file/bot{bot_token}/{tg_path}"
        dl_resp = await asyncio.to_thread(requests.get, download_url, timeout=60)
        if not dl_resp.ok:
            logger.warning("File download failed: %s", dl_resp.status_code)
            return None
        from vaf.core.threat_db import refuse_known_bad
        if refuse_known_bad(dl_resp.content, filename=os.path.basename(tg_path),
                            origin="telegram"):
            return None            # never written to disk; caller treats it as a failed download
        ext = suffix or os.path.splitext(tg_path)[1] or ".bin"
        if not ext.startswith("."):
            ext = "." + ext
        with tempfile.NamedTemporaryFile(prefix="vaf_", suffix=ext, delete=False) as f:
            f.write(dl_resp.content)
            return f.name
    except Exception as e:
        logger.exception("Download Telegram file failed: %s", e)
        return None


def _extract_document_text(temp_path: str) -> str:
    """Extract text from document (PDF, DOCX, etc.) using Librarian. Returns extracted content or error message."""
    try:
        from pathlib import Path
        from vaf.tools.librarian import LibrarianTool
        librarian = LibrarianTool()
        return librarian._read_file(Path(temp_path), enable_chunking=True)
    except Exception as e:
        logger.warning("Document extraction failed: %s", e)
        return f"[ERROR] Could not extract text from document: {e}"


async def _send_audio_as_document(bot_token: str, chat_id: str, audio_path: str) -> bool:
    """Fallback: send audio as document if voice conversion fails."""
    try:
        send_url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
        with open(audio_path, "rb") as audio_file:
            resp = await asyncio.to_thread(
                requests.post,
                send_url,
                data={"chat_id": chat_id},
                files={"document": ("response.wav", audio_file, "audio/wav")},
                timeout=30,
            )
        return resp.ok
    except Exception:
        return False


# Track which chats expect voice replies (set when user sends voice, cleared after reply)
_voice_reply_pending: Dict[str, str] = {}  # chat_id -> detected_language
_voice_reply_lock = threading.Lock()


def _ensure_sender_thread(bot_token: str) -> bool:
    """
    Ensure Telegram sender thread is alive.
    Returns True if a new sender thread was started, otherwise False.
    """
    global _sender_thread, _outgoing_queue
    token = str(bot_token or "").strip()
    if not token:
        return False
    if _outgoing_queue is None:
        _outgoing_queue = queue.Queue()
    if _sender_thread is not None and _sender_thread.is_alive():
        return False
    _sender_thread = threading.Thread(target=_sender_loop, args=(token,), daemon=True, name="vaf-telegram-sender")
    _sender_thread.start()
    logger.info("Telegram sender thread started")
    try:
        from vaf.core.log_helper import log_telegram_reply
        log_telegram_reply("SENDER thread started")
    except Exception:
        pass
    return True


def send_telegram_message_direct(
    chat_id: str,
    text: str,
    *,
    voice_lang: Optional[str] = None,
    file_path: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Send a Telegram message directly via Bot API without relying on the in-process
    bridge callback. This is used by background/subprocess tools that do not share
    the main process callback registry.
    """
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        return False, "telegram_config missing or invalid"

    bot_token = channel_secret("telegram")
    if not bot_token:
        return False, "telegram bot token missing"

    if not chat_id or not text:
        return False, "chat_id and text are required"

    url_message = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    url_document = f"https://api.telegram.org/bot{bot_token}/sendDocument"

    try:
        if file_path and os.path.isfile(file_path):
            with open(file_path, "rb") as doc_file:
                data = _telegram_caption_data(chat_id, text)
                resp = requests.post(
                    url_document,
                    data=data,
                    files={"document": (os.path.basename(file_path), doc_file)},
                    timeout=30,
                )
            if (not resp.ok) and ("parse entities" in (resp.text or "").lower() or "can't parse" in (resp.text or "").lower()):
                with open(file_path, "rb") as doc_file:
                    resp = requests.post(
                        url_document,
                        data={"chat_id": chat_id, "caption": str(text or "")[:1024]},
                        files={"document": (os.path.basename(file_path), doc_file)},
                        timeout=30,
                    )
            if resp.ok:
                try:
                    from vaf.core.log_helper import log_telegram_reply

                    log_telegram_reply(f"DIRECT document ok chat_id={chat_id} file={os.path.basename(file_path)}")
                except Exception:
                    pass
                return True, ""
            try:
                from vaf.core.log_helper import log_telegram_reply

                log_telegram_reply(f"DIRECT failed chat_id={chat_id} status={resp.status_code} body={resp.text[:200]}")
            except Exception:
                pass
            return False, f"Telegram sendDocument failed: {resp.status_code} {resp.text[:200]}"

        if voice_lang:
            loop = asyncio.new_event_loop()
            try:
                success = loop.run_until_complete(_send_voice_reply(bot_token, chat_id, text, voice_lang))
            finally:
                loop.close()
            if success:
                try:
                    from vaf.core.log_helper import log_telegram_reply

                    log_telegram_reply(f"DIRECT voice ok chat_id={chat_id} lang={voice_lang}")
                except Exception:
                    pass
                return True, ""
            return False, "Telegram sendVoice failed"

        payload = _telegram_text_payload(chat_id, text)
        resp = requests.post(url_message, json=payload, timeout=10)
        if (not resp.ok) and ("parse entities" in (resp.text or "").lower() or "can't parse" in (resp.text or "").lower()):
            payload = {"chat_id": chat_id, "text": str(text or "")[:4096]}
            resp = requests.post(url_message, json=payload, timeout=10)
        if resp.ok:
            try:
                from vaf.core.log_helper import log_telegram_reply

                log_telegram_reply(f"DIRECT ok chat_id={chat_id}")
            except Exception:
                pass
            return True, ""
        try:
            from vaf.core.log_helper import log_telegram_reply

            log_telegram_reply(f"DIRECT failed chat_id={chat_id} status={resp.status_code} body={resp.text[:200]}")
        except Exception:
            pass
        return False, f"Telegram sendMessage failed: {resp.status_code} {resp.text[:200]}"
    except Exception as e:
        try:
            from vaf.core.log_helper import log_telegram_reply

            log_telegram_reply(f"DIRECT error {type(e).__name__}: {e}")
        except Exception:
            pass
        return False, str(e)


def _sender_loop(bot_token: str):
    """Run in a thread: read (chat_id, text, voice_lang?, file_path?) from _outgoing_queue and POST to Telegram."""
    global _outgoing_queue
    url_message = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    url_document = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    while True:
        try:
            item = _outgoing_queue.get(timeout=1.0)
            if item is None:
                break

            # Item: (chat_id, text) | (chat_id, text, voice_lang) | (chat_id, text, voice_lang, file_path)
            if len(item) == 4:
                chat_id, text, voice_lang, file_path = item
            elif len(item) == 3:
                chat_id, text, voice_lang = item
                file_path = None
            else:
                chat_id, text = item
                voice_lang = None
                file_path = None

            if not chat_id or not text:
                continue

            # 1. Document: send file with caption (file_path takes precedence)
            if file_path and os.path.isfile(file_path):
                try:
                    with open(file_path, "rb") as doc_file:
                        doc_data = _telegram_caption_data(chat_id, text)
                        resp = requests.post(
                            url_document,
                            data=doc_data,
                            files={"document": (os.path.basename(file_path), doc_file)},
                            timeout=30,
                        )
                    if (not resp.ok) and ("parse entities" in (resp.text or "").lower() or "can't parse" in (resp.text or "").lower()):
                        # Fallback: retry without parse mode/caption formatting.
                        with open(file_path, "rb") as doc_file:
                            resp = requests.post(
                                url_document,
                                data={"chat_id": chat_id, "caption": str(text or "")[:1024]},
                                files={"document": (os.path.basename(file_path), doc_file)},
                                timeout=30,
                            )
                    if resp.ok:
                        try:
                            from vaf.core.log_helper import log_telegram_reply
                            log_telegram_reply(f"SENDER document ok chat_id={chat_id} file={os.path.basename(file_path)}")
                        except Exception:
                            pass
                        continue
                    logger.warning("Telegram sendDocument failed: %s %s", resp.status_code, resp.text[:200])
                except Exception as e:
                    logger.warning("Telegram document send error: %s, falling back to text", e)

            # 2. Voice: send as voice message (if user sent voice)
            send_voice = bool(voice_lang)
            if not send_voice:
                with _voice_reply_lock:
                    if chat_id in _voice_reply_pending:
                        voice_lang = _voice_reply_pending.pop(chat_id)
                        send_voice = True

            if send_voice and voice_lang:
                try:
                    import asyncio
                    loop = asyncio.new_event_loop()
                    success = loop.run_until_complete(_send_voice_reply(bot_token, chat_id, text, voice_lang))
                    loop.close()
                    if success:
                        try:
                            from vaf.core.log_helper import log_telegram_reply
                            log_telegram_reply(f"SENDER voice ok chat_id={chat_id} lang={voice_lang}")
                        except Exception:
                            pass
                        continue
                except Exception as e:
                    logger.warning("Voice reply failed, falling back to text: %s", e)

            # 3. Text: send as plain message
            payload = _telegram_text_payload(chat_id, text)
            resp = requests.post(url_message, json=payload, timeout=10)
            if (not resp.ok) and ("parse entities" in (resp.text or "").lower() or "can't parse" in (resp.text or "").lower()):
                # Fallback: retry without parse mode/formatting.
                payload = {"chat_id": chat_id, "text": str(text or "")[:4096]}
                resp = requests.post(url_message, json=payload, timeout=10)
            if not resp.ok:
                logger.warning("Telegram sendMessage failed: %s %s", resp.status_code, resp.text)
                try:
                    from vaf.core.log_helper import log_telegram_reply
                    log_telegram_reply(f"SENDER failed chat_id={chat_id} status={resp.status_code} body={resp.text[:200]}")
                except Exception:
                    pass
            else:
                try:
                    from vaf.core.log_helper import log_telegram_reply
                    log_telegram_reply(f"SENDER ok chat_id={chat_id}")
                except Exception:
                    pass
        except queue.Empty:
            continue
        except Exception as e:
            logger.exception("Telegram sender error: %s", e)
            try:
                from vaf.core.log_helper import log_telegram_reply
                log_telegram_reply(f"SENDER error {type(e).__name__}: {e}")
            except Exception:
                pass


def _enqueue_reply(chat_id: str, text: str, voice_lang: Optional[str] = None, *, file_path: Optional[str] = None) -> None:
    """Enqueue a reply. If voice_lang is set, reply as voice. If file_path is set, send as document with caption."""
    # Self-heal: sender thread may die while bot thread remains alive.
    # If that happens, replies get enqueued but never delivered.
    if _sender_thread is None or not _sender_thread.is_alive() or _outgoing_queue is None:
        try:
            _ensure_sender_thread(channel_secret("telegram"))
        except Exception:
            pass

    try:
        from vaf.core.log_helper import log_telegram_reply
        log_telegram_reply(f"BRIDGE enqueue chat_id={chat_id} len={len(text)} voice={voice_lang} file={bool(file_path)} queue={_outgoing_queue is not None}")
    except Exception:
        pass
    if _outgoing_queue is not None:
        try:
            if file_path:
                _outgoing_queue.put((chat_id, text, None, file_path))
            elif voice_lang:
                _outgoing_queue.put((chat_id, text, voice_lang))
            else:
                _outgoing_queue.put((chat_id, text))
        except Exception:
            pass


def _run_bot():
    """Run the Telegram bot in this thread (blocking until stop)."""
    global _bridge_stop, _outgoing_queue
    try:
        from telegram import Update
        from telegram.ext import Application, ContextTypes, MessageHandler, filters
    except ImportError:
        logger.error("python-telegram-bot not installed")
        return

    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        logger.error("telegram_config missing or invalid")
        return
    bot_token = channel_secret("telegram")
    if not bot_token:
        logger.error("telegram bot token missing")
        return

    application = Application.builder().token(bot_token).build()

    async def _on_polling_error(update: object, context) -> None:
        """Keep known polling errors to one actionable line instead of full
        tracebacks ('No error handlers are registered' spam)."""
        err = context.error
        try:
            from telegram.error import Conflict, NetworkError, TimedOut
            if isinstance(err, Conflict):
                logger.warning(
                    "Telegram polling conflict (409): another instance is polling this "
                    "bot token (stale VAF process from a restart, or a second install). "
                    "Ensure only ONE VAF instance runs; rotate the token via @BotFather "
                    "if the conflict persists."
                )
                return
            if isinstance(err, (NetworkError, TimedOut)):
                logger.warning("Telegram polling network hiccup: %s", err)
                return
        except Exception:
            pass
        logger.error("Telegram bridge error: %s", err, exc_info=err)

    application.add_error_handler(_on_polling_error)

    debounce_seconds = max(1, int(Config.get("telegram_debounce_seconds", 5)))

    def _telegram_display_name(user) -> str:
        """The person's name for a chat's memory namespace, as Telegram shows it."""
        return str(getattr(user, "full_name", None) or getattr(user, "username", None)
                   or getattr(user, "id", "") or "").strip()

    async def _delayed_flush(chat_id: str) -> None:
        await asyncio.sleep(debounce_seconds)
        with _pending_lock:
            pending = _pending_by_chat.pop(chat_id, None)
        if not pending:
            return
        text = pending.get("text", "").strip()
        if not text:
            return
        user_scope_id = pending.get("user_scope_id")
        vaf_username = pending.get("vaf_username") or "admin"
        telegram_user_id = pending.get("telegram_user_id", "")
        is_relay = pending.get("relay", False)
        voice_lang = pending.get("voice_lang")  # Language detected from voice message

        # If this was a voice message, register for voice reply
        if voice_lang:
            with _voice_reply_lock:
                _voice_reply_pending[chat_id] = voice_lang
            logger.info(f"Voice message from chat {chat_id}, will reply in {voice_lang}")

        _append_chat_activity(chat_id, user_scope_id, "in")
        try:
            from vaf.core.messaging_connections import save_telegram_chat_id
            save_telegram_chat_id(user_scope_id, vaf_username, str(chat_id))
        except Exception:
            pass
        session_id = f"telegram_{telegram_user_id}"
        tq = TaskQueue()
        metadata = {
            "user_scope_id": user_scope_id,
            "username": vaf_username,
            "telegram_chat_id": str(chat_id),
            "origin_channel": "telegram",
            "task_class": "interactive",
            # Carry the originating message id so the persisted user turn can be matched by a later
            # edit event (a debounced burst carries the last id of the burst).
            "channel_message_id": pending.get("message_id"),
        }
        if is_relay:
            metadata["relay"] = True
            metadata["relay_to_username"] = vaf_username
        if pending.get("from_contact"):
            metadata["from_contact"] = True
            metadata["telegram_user_id"] = str(telegram_user_id)  # So headless can load contact data for prompt
            metadata["chat_label"] = str(pending.get("chat_label") or "")
        if voice_lang:
            metadata["voice_lang"] = voice_lang  # Pass language to agent context
        # So the LLM sees that this user message was a voice message (transcribed)
        user_message = f"[Voice message, transcribed]: {text}" if voice_lang else text
        tq.add(
            session_id=session_id,
            input_text=user_message,
            source="telegram",
            metadata=metadata,
        )
        _store_telegram_message(vaf_username, chat_id, text, "in",
                                "voice" if voice_lang else "text", user_scope_id,
                                message_id=pending.get("message_id"))
        try:
            TrayContext().register_telegram_activity()
        except Exception:
            pass

    async def _enqueue_telegram_image(file_id, display_name, mime, caption,
                                      entry, is_relay, telegram_user_id, chat_id,
                                      message_id=None, chat_label="") -> bool:
        """Download a Telegram image and enqueue it for the VISION pipeline — the same path the
        Web UI uses (metadata['images'] with persisted file entries). Returns False on download
        failure. Shared by handle_photo (compressed photos) and handle_document (image sent as a file)."""
        user_scope_id = entry.get("user_scope_id")
        vaf_username = entry.get("vaf_username") or "admin"
        import mimetypes as _mt
        ext = _mt.guess_extension((mime or "image/jpeg").split(";")[0].strip()) or ".jpg"
        temp_path = await _download_telegram_file(bot_token, file_id, ext)
        if not temp_path:
            return False
        try:
            with open(temp_path, "rb") as _f:
                b64 = base64.b64encode(_f.read()).decode("ascii")
        finally:
            try:
                os.unlink(temp_path)
            except Exception:
                pass

        session_id = f"telegram_{telegram_user_id}"
        attached_images = [{"data": b64, "mime_type": mime or "image/jpeg",
                            "name": display_name or "telegram_image.jpg"}]
        # Reuse the exact Web-UI persistence: writes the image into the user-siloed attachments
        # folder and replaces inline base64 with a {name, mime_type, path} entry (falls back to
        # inline base64 on any failure, so vision never breaks).
        try:
            from vaf.core.web_server import _persist_attached_images_to_files
            loop = asyncio.get_event_loop()
            attached_images = await loop.run_in_executor(
                None, _persist_attached_images_to_files, attached_images, session_id, user_scope_id
            )
        except Exception as e:
            logger.warning("telegram image persist failed, keeping inline: %s", e)

        _append_chat_activity(chat_id, user_scope_id, "in")
        try:
            from vaf.core.messaging_connections import save_telegram_chat_id
            save_telegram_chat_id(user_scope_id, vaf_username, str(chat_id))
        except Exception:
            pass

        metadata = {
            "user_scope_id": user_scope_id,
            "username": vaf_username,
            "telegram_chat_id": str(chat_id),
            "origin_channel": "telegram",
            "task_class": "interactive",
            "images": attached_images,  # -> vision pipeline (headless_runner reads metadata['images'])
        }
        if is_relay:
            metadata["relay"] = True
            metadata["relay_to_username"] = vaf_username
        if entry.get("from_contact"):
            metadata["from_contact"] = True
            metadata["telegram_user_id"] = str(telegram_user_id)
            metadata["chat_label"] = str(chat_label or "")

        user_message = f"[Photo] (User: {caption})" if caption else "[Photo]"
        TaskQueue().add(session_id=session_id, input_text=user_message,
                        source="telegram", metadata=metadata)
        _store_telegram_message(vaf_username, chat_id, user_message, "in", "image", user_scope_id,
                                message_id=message_id)
        try:
            TrayContext().register_telegram_activity()
        except Exception:
            pass
        logger.info("Image from chat %s enqueued for vision", chat_id)
        return True

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.message.text:
            return
        user = update.effective_user
        if not user:
            return
        telegram_user_id = str(user.id)
        chat_id = str(update.effective_chat.id if update.effective_chat else user.id)
        entry, is_relay = _resolve_telegram_user(telegram_user_id, user)
        if not entry:
            _drop_unauthorized_telegram(telegram_user_id, chat_id, "text", update=update)
            return
        user_scope_id = entry.get("user_scope_id")
        vaf_username = entry.get("vaf_username") or "admin"
        msg_text = update.message.text.strip()
        if not msg_text:
            return
        _remember_inbound(f"telegram_{telegram_user_id}", chat_id, update.message.message_id, msg_text)

        with _pending_lock:
            if chat_id not in _pending_by_chat:
                _pending_by_chat[chat_id] = {
                    "text": "",
                    "task": None,
                    "message_id": None,
                    "user_scope_id": user_scope_id,
                    "vaf_username": vaf_username,
                    "telegram_user_id": telegram_user_id,
                    "relay": is_relay,
                    "from_contact": bool(entry.get("from_contact")),
                    "chat_label": _telegram_display_name(user),
                }
            rec = _pending_by_chat[chat_id]
            rec["message_id"] = str(update.message.message_id)  # last message of the burst (edit-stable key)
            rec["text"] = (rec["text"] + " " + msg_text).strip() if rec["text"] else msg_text
            if rec.get("task") and not rec["task"].done():
                rec["task"].cancel()
            rec["task"] = asyncio.create_task(_delayed_flush(chat_id))

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    async def handle_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # The user edited a previously-sent Telegram message. The Bot API delivers the NEW text and
        # the message id but no pre-edit text, so we match the recorded message id against the user
        # turn (tagged at persist time) and patch the authoritative session, which read_telegram_chat
        # and find_telegram_messages both derive from. Deletes are not delivered by the Bot API.
        em = update.edited_message
        if not em or not em.text:
            return
        user = update.effective_user
        if not user:
            return
        telegram_user_id = str(user.id)
        chat_id = str(update.effective_chat.id if update.effective_chat else user.id)
        entry, _is_relay = _resolve_telegram_user(telegram_user_id, user)
        if not entry:
            _drop_unauthorized_telegram(telegram_user_id, chat_id, "edited", update=update)
            return
        new_text = em.text.strip()
        if not new_text:
            return
        try:
            from vaf.core.channel_history import edit_channel_message
            # Session is keyed by telegram_user_id (telegram_<id>), matching handle_message.
            if edit_channel_message("telegram", telegram_user_id, new_text,
                                    message_id=str(em.message_id)):
                logger.info("Telegram edit applied to chat %s (message %s)", chat_id, em.message_id)
        except Exception as e:
            logger.warning("Telegram edited-message handling failed: %s", e)

    application.add_handler(
        MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.TEXT, handle_edited_message)
    )

    async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle voice messages: transcribe via Whisper STT and process as text."""
        if not update.message or not update.message.voice:
            return
        user = update.effective_user
        if not user:
            return
        telegram_user_id = str(user.id)
        chat_id = str(update.effective_chat.id if update.effective_chat else user.id)

        # Check authorization
        entry, is_relay = _resolve_telegram_user(telegram_user_id, user)
        if not entry:
            _drop_unauthorized_telegram(telegram_user_id, chat_id, "voice", update=update)
            return
        user_scope_id = entry.get("user_scope_id")
        vaf_username = entry.get("vaf_username") or "admin"

        # Get voice file ID
        voice = update.message.voice
        file_id = voice.file_id

        # Transcribe voice message (no confirmation message - only errors)
        text, detected_lang = await _transcribe_voice(bot_token, file_id)

        if not text:
            await update.message.reply_text("❌ Konnte Sprachnachricht nicht verstehen. Bitte erneut versuchen.")
            return

        logger.info(f"Voice transcribed: lang={detected_lang}, text={text[:100]}...")

        # Queue the transcribed text (with voice language for TTS reply)
        with _pending_lock:
            if chat_id not in _pending_by_chat:
                _pending_by_chat[chat_id] = {
                    "text": "",
                    "task": None,
                    "message_id": None,
                    "user_scope_id": user_scope_id,
                    "vaf_username": vaf_username,
                    "telegram_user_id": telegram_user_id,
                    "relay": is_relay,
                    # A burst that starts with a voice message is the same person as one that
                    # starts with text: without this a contact ran with the owner's tools.
                    "from_contact": bool(entry.get("from_contact")),
                    "chat_label": _telegram_display_name(user),
                    "voice_lang": detected_lang,  # Store detected language for TTS reply
                }
            rec = _pending_by_chat[chat_id]
            rec["message_id"] = str(update.message.message_id)  # last message of the burst (edit-stable key)
            rec["text"] = (rec["text"] + " " + text).strip() if rec["text"] else text
            rec["voice_lang"] = detected_lang  # Update language
            if rec.get("task") and not rec["task"].done():
                rec["task"].cancel()
            rec["task"] = asyncio.create_task(_delayed_flush(chat_id))

    application.add_handler(
        MessageHandler(filters.VOICE, handle_voice)
    )

    # Supported document extensions for text extraction (PDF, Word, Excel, PowerPoint, text)
    _DOCUMENT_EXTS = (".pdf", ".docx", ".xlsx", ".pptx", ".xls", ".txt", ".md", ".csv", ".json", ".xml")

    async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming documents (PDF, DOCX, etc.): download, extract text, pass to agent as context."""
        if not update.message or not update.message.document:
            return
        doc = update.message.document
        user = update.effective_user
        if not user:
            return
        telegram_user_id = str(user.id)
        chat_id = str(update.effective_chat.id if update.effective_chat else user.id)

        entry, is_relay = _resolve_telegram_user(telegram_user_id, user)
        if not entry:
            _drop_unauthorized_telegram(telegram_user_id, chat_id, "document", update=update)
            return
        user_scope_id = entry.get("user_scope_id")
        vaf_username = entry.get("vaf_username") or "admin"

        file_name = doc.file_name or "document"
        ext = os.path.splitext(file_name.lower())[1]
        doc_mime = getattr(doc, "mime_type", "") or ""

        # Image sent as a file (uncompressed) → route to the vision pipeline, not text/RAG
        # (RAG intentionally skips image/*). Mirrors the Web UI's mime-based routing.
        if doc_mime.startswith("image/") or ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
            caption = (update.message.caption or "").strip()
            ok = await _enqueue_telegram_image(
                doc.file_id, file_name, doc_mime or "image/jpeg", caption,
                entry, is_relay, telegram_user_id, chat_id,
                message_id=str(update.message.message_id),
                chat_label=_telegram_display_name(user),
            )
            if not ok:
                await update.message.reply_text("❌ Bild konnte nicht verarbeitet werden.")
            return

        if ext not in _DOCUMENT_EXTS:
            await update.message.reply_text(
                f"Dokumentformat {ext or 'unbekannt'} wird noch nicht unterstützt. "
                f"Bitte sende PDF, DOCX, XLSX oder Textdateien."
            )
            return

        # Download file from Telegram
        temp_path = await _download_telegram_file(bot_token, doc.file_id, ext)
        if not temp_path:
            await update.message.reply_text("❌ Datei konnte nicht heruntergeladen werden.")
            return

        try:
            # Extract text (sync, run in thread)
            loop = asyncio.get_event_loop()
            extracted = await loop.run_in_executor(None, _extract_document_text, temp_path)
        finally:
            try:
                os.unlink(temp_path)
            except Exception:
                pass

        caption = (update.message.caption or "").strip()
        if caption:
            user_message = f"[Document: {file_name}] (User: {caption})\n\n--- Document content ---\n{extracted}"
        else:
            user_message = f"[Document: {file_name}]\n\n--- Document content ---\n{extracted}"

        _append_chat_activity(chat_id, user_scope_id, "in")
        try:
            from vaf.core.messaging_connections import save_telegram_chat_id
            save_telegram_chat_id(user_scope_id, vaf_username, str(chat_id))
        except Exception:
            pass

        session_id = f"telegram_{telegram_user_id}"
        tq = TaskQueue()
        metadata = {
            "user_scope_id": user_scope_id,
            "username": vaf_username,
            "telegram_chat_id": str(chat_id),
            "origin_channel": "telegram",
            "task_class": "interactive",
        }
        if is_relay:
            metadata["relay"] = True
            metadata["relay_to_username"] = vaf_username
        if entry.get("from_contact"):
            metadata["from_contact"] = True
            metadata["telegram_user_id"] = str(telegram_user_id)
            metadata["chat_label"] = _telegram_display_name(user)

        tq.add(
            session_id=session_id,
            input_text=user_message,
            source="telegram",
            metadata=metadata,
        )
        try:
            TrayContext().register_telegram_activity()
        except Exception:
            pass
        _store_telegram_message(vaf_username, chat_id,
                                f"[Document: {file_name}]" + (f" {caption}" if caption else ""),
                                "in", "document", user_scope_id,
                                message_id=str(update.message.message_id))
        logger.info("Document %s from chat %s enqueued, %d chars extracted", file_name, chat_id, len(extracted))

        # RAG indexing (additive): keep the document retrievable in later turns too. The inline
        # text above already covers THIS turn. index_session_attachments_sync REPLACES the session
        # index, so we re-index the accumulated set (see _telegram_session_documents).
        if bool(Config.get("attachment_rag_enabled", False)) and extracted and not extracted.lstrip().startswith("[ERROR]"):
            try:
                new_doc = {"name": file_name, "content": extracted, "mimeType": doc_mime, "path": ""}
                docs = [d for d in _telegram_session_documents.get(session_id, []) if d.get("name") != file_name]
                docs.append(new_doc)
                _telegram_session_documents[session_id] = docs
                from vaf.memory.attachment_rag import index_session_attachments_sync
                await asyncio.get_event_loop().run_in_executor(
                    None, index_session_attachments_sync, session_id, user_scope_id, list(docs)
                )
                logger.info("Document %s indexed for RAG (%d doc(s) in session)", file_name, len(docs))
            except Exception as e:
                logger.warning("telegram document RAG indexing failed: %s", e)

    application.add_handler(
        MessageHandler(filters.Document.ALL, handle_document)
    )

    async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming photos: download the highest-resolution rendition and route it to the
        vision pipeline (same image path as the Web UI)."""
        if not update.message or not update.message.photo:
            return
        user = update.effective_user
        if not user:
            return
        telegram_user_id = str(user.id)
        chat_id = str(update.effective_chat.id if update.effective_chat else user.id)
        entry, is_relay = _resolve_telegram_user(telegram_user_id, user)
        if not entry:
            _drop_unauthorized_telegram(telegram_user_id, chat_id, "photo", update=update)
            return
        # Telegram sends several JPEG renditions; the last is the highest resolution.
        photo = update.message.photo[-1]
        caption = (update.message.caption or "").strip()
        ok = await _enqueue_telegram_image(
            photo.file_id, "telegram_photo.jpg", "image/jpeg", caption,
            entry, is_relay, telegram_user_id, chat_id,
            message_id=str(update.message.message_id),
            chat_label=_telegram_display_name(user),
        )
        if not ok:
            await update.message.reply_text("❌ Foto konnte nicht verarbeitet werden. Bitte erneut senden.")

    application.add_handler(
        MessageHandler(filters.PHOTO, handle_photo)
    )

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            user = update.effective_user
            if user:
                telegram_user_id = str(user.id)
                chat_id = str(update.effective_chat.id if update.effective_chat else user.id)
                # `/start <code>`: an account pairing its own Telegram (vaf/core/channel_pairing.py).
                # Only a live code gets an answer; anything else goes on as before.
                private = bool(update.effective_chat and getattr(update.effective_chat, "type", "") == "private")
                if private and context.args:
                    answer = await asyncio.to_thread(_pair_with_code, telegram_user_id,
                                                     getattr(user, "username", None), str(context.args[0]))
                    if answer:
                        await update.message.reply_text(answer)
                        return
                entry, _ = _resolve_telegram_user(telegram_user_id, user)
                if not entry:
                    _drop_unauthorized_telegram(telegram_user_id, chat_id, "command_start", update=update)
                    return
        except Exception:
            # Fail-safe: do not leak command responses to unauthorized users.
            return
        await update.message.reply_text(
            "Hi! Send a message and I’ll relay it to your VAF agent. "
            "If you get “not authorized”, add your Telegram in VAF Settings → Connections."
        )

    from telegram.ext import CommandHandler
    application.add_handler(CommandHandler("start", cmd_start))

    # Job to check stop request and exit run_polling (if job queue is available)
    async def check_stop(context):
        if _bridge_stop_requested:
            await application.stop()

    try:
        job_queue = application.job_queue
        if job_queue:
            job_queue.run_repeating(check_stop, interval=3, first=5)
    except Exception:
        pass

    set_telegram_reply_callback(_enqueue_reply)

    try:
        # This runs in a background thread (see start_bridge), so disable run_polling()'s
        # signal handlers — add_signal_handler only works on the main thread (Python 3.13
        # raises RuntimeError otherwise). Stop is handled by the check_stop job above.
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, stop_signals=None)
    except Exception as e:
        logger.exception("Telegram bridge bot error: %s", e)
    finally:
        set_telegram_reply_callback(None)


def start_bridge() -> bool:
    """Start the Telegram bridge (bot + sender thread). Returns True if started."""
    global _bridge_thread, _sender_thread, _outgoing_queue, _bridge_stop_requested
    bot_token = channel_secret("telegram")
    if not bot_token:
        return False
    _bridge_stop_requested = False
    sender_started = _ensure_sender_thread(bot_token)
    if _bridge_thread is not None and _bridge_thread.is_alive():
        if sender_started:
            logger.warning("Telegram bot thread alive but sender was down; sender restarted")
        return True
    _bridge_thread = threading.Thread(target=_run_bot, daemon=True)
    _bridge_thread.start()
    logger.info("Telegram bridge started")
    return True


def stop_bridge() -> None:
    """Request bridge stop (sender thread exits; bot may keep running until next job check)."""
    global _bridge_stop_requested, _outgoing_queue
    _bridge_stop_requested = True
    if _outgoing_queue is not None:
        try:
            _outgoing_queue.put(None)
        except Exception:
            pass
    logger.info("Telegram bridge stop requested")


def is_bridge_running() -> bool:
    return _bridge_thread is not None and _bridge_thread.is_alive()
