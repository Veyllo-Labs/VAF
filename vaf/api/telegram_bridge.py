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
import logging
import os
import queue
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from vaf.core.config import Config
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
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
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
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_file:
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
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as ogg_file:
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
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_file:
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


def _sender_loop(bot_token: str):
    """Run in a thread: read (chat_id, text, voice_lang) from _outgoing_queue and POST to Telegram."""
    global _outgoing_queue
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    while True:
        try:
            item = _outgoing_queue.get(timeout=1.0)
            if item is None:
                break

            # Item can be (chat_id, text) or (chat_id, text, voice_lang)
            if len(item) == 3:
                chat_id, text, voice_lang = item
            else:
                chat_id, text = item
                voice_lang = None

            if not chat_id or not text:
                continue

            # Check if we should reply with voice
            send_voice = False
            if voice_lang:
                send_voice = True
            else:
                # Check pending voice replies
                with _voice_reply_lock:
                    if chat_id in _voice_reply_pending:
                        voice_lang = _voice_reply_pending.pop(chat_id)
                        send_voice = True

            if send_voice and voice_lang:
                # Send voice reply using async in a new event loop
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
                        continue  # Voice sent successfully, skip text
                except Exception as e:
                    logger.warning(f"Voice reply failed, falling back to text: {e}")

            # Send as plain text (no parse_mode) so model output with < & > doesn't break
            payload = {"chat_id": chat_id, "text": text[:4096]}
            resp = requests.post(url, json=payload, timeout=10)
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


def _enqueue_reply(chat_id: str, text: str, voice_lang: Optional[str] = None) -> None:
    """Enqueue a reply. If voice_lang is set, reply will be sent as voice message."""
    try:
        from vaf.core.log_helper import log_telegram_reply
        log_telegram_reply(f"BRIDGE enqueue chat_id={chat_id} len={len(text)} voice={voice_lang} queue={_outgoing_queue is not None}")
    except Exception:
        pass
    if _outgoing_queue is not None:
        try:
            if voice_lang:
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
        }
        if is_relay:
            metadata["relay"] = True
            metadata["relay_to_username"] = vaf_username
        if voice_lang:
            metadata["voice_lang"] = voice_lang  # Pass language to agent context
        tq.add(
            session_id=session_id,
            input_text=text,
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
        entry = _whitelist_lookup(telegram_user_id)
        relay_entry = None
        if not entry:
            relay_entry = _relay_whitelist_lookup(telegram_user_id)
            if not relay_entry:
                await update.message.reply_text(
                    "You are not authorized to use this bot. Please add your Telegram in VAF Settings → Connections."
                )
                return
        if entry:
            user_scope_id = entry.get("user_scope_id")
            vaf_username = entry.get("vaf_username") or "admin"
            is_relay = False
        else:
            user_scope_id = relay_entry.get("user_scope_id")
            vaf_username = relay_entry.get("vaf_username") or "admin"
            is_relay = True
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
        entry = _whitelist_lookup(telegram_user_id)
        relay_entry = None
        if not entry:
            relay_entry = _relay_whitelist_lookup(telegram_user_id)
            if not relay_entry:
                await update.message.reply_text(
                    "You are not authorized to use this bot. Please add your Telegram in VAF Settings → Connections."
                )
                return

        if entry:
            user_scope_id = entry.get("user_scope_id")
            vaf_username = entry.get("vaf_username") or "admin"
            is_relay = False
        else:
            user_scope_id = relay_entry.get("user_scope_id")
            vaf_username = relay_entry.get("vaf_username") or "admin"
            is_relay = True

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

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    if _bridge_thread is not None and _bridge_thread.is_alive():
        return True
    _bridge_stop_requested = False
    _outgoing_queue = queue.Queue()
    bot_token = (telegram_config.get("bot_token") or "").strip()
    _sender_thread = threading.Thread(target=_sender_loop, args=(bot_token,), daemon=True)
    _sender_thread.start()
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
