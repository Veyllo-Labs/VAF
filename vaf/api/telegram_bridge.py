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

from vaf.core.config import Config
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
) -> None:
    """
    Silently drop unauthorized inbound Telegram traffic.
    Logging is throttled per user to avoid log amplification under abuse.
    """
    uid = str(telegram_user_id or "")
    policy = Config.get("channel_ingress_policy")
    should_log = should_log_unauthorized("telegram", uid, policy)
    if should_log:
        logger.warning(
            "Dropped unauthorized Telegram %s message from user_id=%s chat_id=%s reason=%s",
            message_kind,
            uid,
            str(chat_id or ""),
            reason,
        )


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


def _resolve_telegram_user(telegram_user_id: str) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Resolve telegram_user_id to (whitelist/relay/contact entry, is_relay). Returns (None, False) if not allowed."""
    policy = Config.get("channel_ingress_policy")
    entry = _whitelist_lookup(telegram_user_id)
    if entry:
        allowed, _ = evaluate_ingress("telegram", policy, explicit_match=True, contact_match=False)
        if allowed:
            return (entry, False)
        return (None, False)
    entry = _relay_whitelist_lookup(telegram_user_id)
    if entry:
        allowed, _ = evaluate_ingress("telegram", policy, explicit_match=True, contact_match=False)
        if allowed:
            return (entry, True)
        return (None, False)
    try:
        from vaf.core.messaging_connections import get_contact_whitelist_telegram_entry
        entry = get_contact_whitelist_telegram_entry(telegram_user_id)
        if entry:
            allowed, _ = evaluate_ingress("telegram", policy, explicit_match=False, contact_match=True)
            if allowed:
                return (entry, False)
    except Exception:
        pass
    return (None, False)


def _append_chat_activity(chat_id: str, user_scope_id: Any, direction: str = "in") -> None:
    """Append one activity entry for the dashboard timeline (keeps last 100)."""
    try:
        config = Config.load()
        tc = config.get("telegram_config") or {}
        if not isinstance(tc, dict):
            return
        activity = list(tc.get("chat_activity") or [])
        activity.append({"chat_id": str(chat_id), "user_scope_id": str(user_scope_id) if user_scope_id else None, "ts": time.time(), "direction": direction})
        tc["chat_activity"] = activity[-100:]
        config["telegram_config"] = tc
        Config.save(config)
    except Exception:
        pass


async def _transcribe_voice(bot_token: str, file_id: str) -> tuple[Optional[str], Optional[str]]:
    """
    Download voice message from Telegram and transcribe via Docker Whisper STT.
    Returns (transcribed_text, detected_language) or (None, None) on error.
    """
    try:
        # 1. Get file path from Telegram
        file_url = f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}"
        resp = requests.get(file_url, timeout=10)
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
        audio_resp = requests.get(download_url, timeout=30)
        if not audio_resp.ok:
            logger.warning(f"Failed to download voice file: {audio_resp.status_code}")
            return None, None

        # 3. Save to temp file
        suffix = ".ogg" if file_path.endswith(".oga") or file_path.endswith(".ogg") else ".oga"
        with tempfile.NamedTemporaryFile(prefix="vaf_", suffix=suffix, delete=False) as temp_file:
            temp_file.write(audio_resp.content)
            temp_path = temp_file.name

        try:
            # 4. Send to Whisper STT
            stt_url = (Config.get("speech_stt_docker_url") or "http://localhost:5003").strip().rstrip("/")
            asr_endpoint = f"{stt_url}/asr"

            with open(temp_path, "rb") as f:
                stt_resp = requests.post(
                    asr_endpoint,
                    files={"audio_file": ("voice.ogg", f, "audio/ogg")},
                    params={"encode": "true", "output": "json"},
                    timeout=60,
                )

            if not stt_resp.ok:
                logger.warning(f"STT request failed: {stt_resp.status_code} - {stt_resp.text[:200]}")
                return None, None

            data = stt_resp.json()
            text = (data.get("text") or "").strip()
            language = data.get("language", "en")

            logger.info(f"Voice transcribed: lang={language}, text={text[:50]}...")
            return text, language

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
        # 1. Synthesize audio via Docker TTS - request OGG format directly
        #    The TTS container has ffmpeg installed and can convert internally
        tts_url = (Config.get("speech_tts_docker_url") or "http://localhost:5002").strip().rstrip("/")
        synth_endpoint = f"{tts_url}/synthesize"

        tts_resp = requests.post(
            synth_endpoint,
            json={"text": text, "language": language[:2].lower(), "format": "ogg"},
            timeout=60,  # OGG conversion takes longer
        )

        if not tts_resp.ok:
            logger.warning(f"TTS request failed: {tts_resp.status_code}")
            return False

        audio_data = tts_resp.content
        if not audio_data:
            logger.warning("TTS returned empty audio")
            return False

        # Check if we got OGG (starts with "OggS") or WAV (starts with "RIFF")
        if audio_data[:4] == b"OggS":
            # Got OGG directly - perfect!
            ogg_data = audio_data
        elif audio_data[:4] == b"RIFF":
            # Got WAV - try local ffmpeg conversion
            logger.info("TTS returned WAV, attempting local ffmpeg conversion")
            ogg_data = await _convert_wav_to_ogg_local(audio_data)
            if not ogg_data:
                # Fallback: send as document
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
                resp = requests.post(
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


async def _convert_wav_to_ogg_local(wav_data: bytes) -> Optional[bytes]:
    """Try to convert WAV to OGG using local ffmpeg (if available)."""
    import subprocess

    try:
        with tempfile.NamedTemporaryFile(prefix="vaf_", suffix=".wav", delete=False) as wav_file:
            wav_file.write(wav_data)
            wav_path = wav_file.name

        ogg_path = wav_path.replace(".wav", ".ogg")

        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", wav_path, "-c:a", "libopus", "-b:a", "64k", ogg_path],
                capture_output=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning(f"Local ffmpeg conversion failed: {result.stderr.decode()[:200]}")
                return None

            with open(ogg_path, "rb") as f:
                return f.read()

        finally:
            for path in [wav_path, ogg_path]:
                try:
                    if os.path.exists(path):
                        os.unlink(path)
                except Exception:
                    pass

    except FileNotFoundError:
        logger.warning("ffmpeg not found locally")
        return None
    except Exception as e:
        logger.warning(f"Local WAV to OGG conversion failed: {e}")
        return None


async def _download_telegram_file(bot_token: str, file_id: str, suffix: str = "") -> Optional[str]:
    """
    Download a file from Telegram by file_id. Returns path to temp file or None on error.
    Caller must delete the temp file when done.
    """
    try:
        file_url = f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}"
        resp = requests.get(file_url, timeout=10)
        if not resp.ok:
            logger.warning("getFile failed: %s", resp.status_code)
            return None
        file_info = resp.json()
        tg_path = file_info.get("result", {}).get("file_path")
        if not tg_path:
            return None
        download_url = f"https://api.telegram.org/file/bot{bot_token}/{tg_path}"
        dl_resp = requests.get(download_url, timeout=60)
        if not dl_resp.ok:
            logger.warning("File download failed: %s", dl_resp.status_code)
            return None
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
            resp = requests.post(
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

    bot_token = (telegram_config.get("bot_token") or "").strip()
    if not bot_token:
        return False, "telegram_config.bot_token missing"

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
            cfg = Config.get("telegram_config") or {}
            token = (cfg.get("bot_token") or "").strip() if isinstance(cfg, dict) else ""
            _ensure_sender_thread(token)
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
    bot_token = (telegram_config.get("bot_token") or "").strip()
    if not bot_token:
        logger.error("telegram_config.bot_token missing")
        return

    application = Application.builder().token(bot_token).build()

    debounce_seconds = max(1, int(Config.get("telegram_debounce_seconds", 5)))

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
        }
        if is_relay:
            metadata["relay"] = True
            metadata["relay_to_username"] = vaf_username
        if pending.get("from_contact"):
            metadata["from_contact"] = True
            metadata["telegram_user_id"] = str(telegram_user_id)  # So headless can load contact data for prompt
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
        try:
            TrayContext().register_telegram_activity()
        except Exception:
            pass

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.message.text:
            return
        user = update.effective_user
        if not user:
            return
        telegram_user_id = str(user.id)
        chat_id = str(update.effective_chat.id if update.effective_chat else user.id)
        entry, is_relay = _resolve_telegram_user(telegram_user_id)
        if not entry:
            _drop_unauthorized_telegram(telegram_user_id, chat_id, "text")
            return
        user_scope_id = entry.get("user_scope_id")
        vaf_username = entry.get("vaf_username") or "admin"
        msg_text = update.message.text.strip()
        if not msg_text:
            return

        with _pending_lock:
            if chat_id not in _pending_by_chat:
                _pending_by_chat[chat_id] = {
                    "text": "",
                    "task": None,
                    "user_scope_id": user_scope_id,
                    "vaf_username": vaf_username,
                    "telegram_user_id": telegram_user_id,
                    "relay": is_relay,
                    "from_contact": bool(entry.get("from_contact")),
                }
            rec = _pending_by_chat[chat_id]
            rec["text"] = (rec["text"] + " " + msg_text).strip() if rec["text"] else msg_text
            if rec.get("task") and not rec["task"].done():
                rec["task"].cancel()
            rec["task"] = asyncio.create_task(_delayed_flush(chat_id))

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
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
        entry, is_relay = _resolve_telegram_user(telegram_user_id)
        if not entry:
            _drop_unauthorized_telegram(telegram_user_id, chat_id, "voice")
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
                    "user_scope_id": user_scope_id,
                    "vaf_username": vaf_username,
                    "telegram_user_id": telegram_user_id,
                    "relay": is_relay,
                    "voice_lang": detected_lang,  # Store detected language for TTS reply
                }
            rec = _pending_by_chat[chat_id]
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

        entry, is_relay = _resolve_telegram_user(telegram_user_id)
        if not entry:
            _drop_unauthorized_telegram(telegram_user_id, chat_id, "document")
            return
        user_scope_id = entry.get("user_scope_id")
        vaf_username = entry.get("vaf_username") or "admin"

        file_name = doc.file_name or "document"
        ext = os.path.splitext(file_name.lower())[1]

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
        logger.info("Document %s from chat %s enqueued, %d chars extracted", file_name, chat_id, len(extracted))

    application.add_handler(
        MessageHandler(filters.Document.ALL, handle_document)
    )

    # -------------------------------------------------------------------------
    # PHOTO HANDLER – Placeholder for future implementation
    # -------------------------------------------------------------------------
    # When implementing photo support, consider:
    # 1. Download photo via _download_telegram_file(bot_token, photo.file_id, ".jpg")
    # 2. Option A (OCR): Use pytesseract.image_to_string() for text extraction (receipts, screenshots)
    # 3. Option B (Vision): If LLM supports vision, pass base64 image to multimodal API
    # 4. Build user_message similar to documents: "[Photo] (caption)\n\n--- OCR/Vision output ---"
    # 5. Register: MessageHandler(filters.PHOTO, handle_photo)
    # Dependencies: pytesseract + Tesseract (OCR), or vision-capable model (GPT-4V, Claude, etc.)
    # -------------------------------------------------------------------------

    async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Placeholder: Photo support not yet implemented. Replies with info for future implementation."""
        if not update.message or not update.message.photo:
            return
        user = update.effective_user
        if not user:
            return
        entry, _ = _resolve_telegram_user(str(user.id))
        if not entry:
            _drop_unauthorized_telegram(str(user.id), str(update.effective_chat.id if update.effective_chat else user.id), "photo")
            return  # Silently ignore unauthorized
        await update.message.reply_text(
            "📷 Foto-Unterstützung kommt bald. Aktuell kannst du mir Dokumente (PDF, DOCX) schicken – "
            "diese werden verarbeitet. Für Fotos (z.B. Rechnung abfotografiert) ist OCR/Vision in Planung."
        )
        logger.info("Photo received from user %s – placeholder reply (photo support planned)", user.id)

    application.add_handler(
        MessageHandler(filters.PHOTO, handle_photo)
    )

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            user = update.effective_user
            if user:
                telegram_user_id = str(user.id)
                chat_id = str(update.effective_chat.id if update.effective_chat else user.id)
                entry, _ = _resolve_telegram_user(telegram_user_id)
                if not entry:
                    _drop_unauthorized_telegram(telegram_user_id, chat_id, "command_start")
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
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except Exception as e:
        logger.exception("Telegram bridge bot error: %s", e)
    finally:
        set_telegram_reply_callback(None)


def start_bridge() -> bool:
    """Start the Telegram bridge (bot + sender thread). Returns True if started."""
    global _bridge_thread, _sender_thread, _outgoing_queue, _bridge_stop_requested
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict) or not telegram_config.get("bot_token"):
        return False
    bot_token = (telegram_config.get("bot_token") or "").strip()
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
