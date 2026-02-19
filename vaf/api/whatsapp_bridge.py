"""
Long-lived WhatsApp bridge: one Node (Baileys) subprocess per user with linked auth.
Receives messages via Node stdout, enqueues tasks, sends replies via stdin to Node.
User isolation: each user's credentials and session are strictly separate.
Voice messages from WhatsApp are downloaded by Node, transcribed via Whisper STT, and passed as text.
"""
import json
import logging
import os
import time
import uuid
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from vaf.core.config import Config, get_local_admin_scope_id
from vaf.core.messaging_connections import save_whatsapp_chat_jid
from vaf.core.task_queue import TaskQueue
from vaf.core.whatsapp_auth import get_whatsapp_auth_dir, whatsapp_auth_exists
from vaf.core.whatsapp_reply import set_whatsapp_reply_callback
from vaf.core.whatsapp_send import chunk_whatsapp_text

logger = logging.getLogger("vaf.api.whatsapp_bridge")

_bridge_thread: Optional[threading.Thread] = None
_sender_thread: Optional[threading.Thread] = None
_bridge_stop = threading.Event()
_processes: Dict[str, subprocess.Popen] = {}
_process_lock = threading.Lock()
_outgoing_queue: Optional[queue.Queue] = None
_chat_lists: Dict[str, List[Dict[str, Any]]] = {}
_chat_list_events: Dict[str, threading.Event] = {}
_chat_lists_lock = threading.Lock()

# Connection check: ping/pong from Node to verify socket is connected
_connection_status: Dict[str, bool] = {}
_connection_events: Dict[str, threading.Event] = {}
_connection_lock = threading.Lock()

# Reply with voice when user sent voice (like Telegram)
_voice_reply_pending: Dict[str, str] = {}  # "username|chat_jid" -> voice_lang
_voice_reply_lock = threading.Lock()

# Send confirmation: req_id -> queue that receives (success, error)
_pending_sends: Dict[str, queue.Queue] = {}
_pending_sends_lock = threading.Lock()


def _wa_bridge_path() -> Path:
    """Path to wa-bridge.js."""
    return Path(__file__).resolve().parents[1] / "whatsapp_node" / "wa-bridge.js"


def _node_path() -> Optional[str]:
    """Resolve Node executable."""
    return shutil.which("node")


def _jid_to_e164(jid: str) -> str:
    """Convert WhatsApp JID to E.164-like string (e.g. 491234567890@s.whatsapp.net -> 491234567890).
    Strips :device suffix, skips @lid/@broadcast/@status."""
    if not jid or not isinstance(jid, str):
        return ""
    jid = jid.strip()
    if "@lid" in jid or jid.endswith("@broadcast") or jid.endswith("@status"):
        return ""
    if "@" not in jid:
        return jid
    part = jid.split("@")[0].split(":")[0].strip()
    if not part or not part.isdigit() or len(part) < 7 or len(part) > 15:
        return ""
    return part


def _normalize_phone(phone: str) -> str:
    """Normalize phone to digits only for comparison."""
    return "".join(c for c in (phone or "") if c.isdigit())


def _phone_digits_canonical(phone_or_jid: str) -> str:
    """Digits for matching: E.164-style. Converts 0176... (10 digits) to 49176... so WhatsApp JID 49176... matches contact 0176...."""
    digits = _normalize_phone(phone_or_jid if not (phone_or_jid or "").strip().endswith(".net") else _jid_to_e164(phone_or_jid))
    if not digits:
        return ""
    if len(digits) == 10 and digits.startswith("0"):
        return "49" + digits[1:]
    return digits


def _to_e164_display(phone_or_jid: str) -> str:
    """Return E.164 display form with exactly one leading + (e.g. +491761234567). Avoids double plus."""
    if not phone_or_jid or not isinstance(phone_or_jid, str):
        return ""
    s = (phone_or_jid or "").strip().lstrip("+")
    digits = "".join(c for c in s if c.isdigit())
    if not digits or len(digits) < 7 or len(digits) > 15:
        return ""
    return f"+{digits}"


def _e164_to_jid(phone: str) -> str:
    """Convert E.164 or phone string to WhatsApp JID (e.g. +491761234567 -> 491761234567@s.whatsapp.net)."""
    digits = _normalize_phone(phone or "")
    if not digits or len(digits) < 7 or len(digits) > 15:
        return ""
    return f"{digits}@s.whatsapp.net"


def _append_chat_activity(chat_id: str, user_scope_id: Any, direction: str = "in") -> None:
    """Append one activity entry for the dashboard timeline (keeps last 100)."""
    try:
        config = Config.load()
        wc = config.get("whatsapp_config") or {}
        if not isinstance(wc, dict):
            return
        activity = list(wc.get("chat_activity") or [])
        activity.append({"chat_id": str(chat_id), "user_scope_id": str(user_scope_id) if user_scope_id else None, "ts": time.time(), "direction": direction})
        wc["chat_activity"] = activity[-100:]
        config["whatsapp_config"] = wc
        Config.save(config)
    except Exception:
        pass


def _synthesize_voice_for_reply(text: str, lang: str) -> Optional[str]:
    """Synthesize TTS to temp file. Returns path or None."""
    try:
        import tempfile
        tts_url = (Config.get("speech_tts_docker_url") or "http://localhost:5002").strip().rstrip("/")
        if not tts_url:
            logger.warning("WhatsApp TTS: no speech_tts_docker_url configured")
            return None
        logger.info("WhatsApp TTS: synthesizing lang=%s text_len=%d url=%s", lang, len(text), tts_url)
        resp = requests.post(
            f"{tts_url}/synthesize",
            json={"text": text[:4000], "language": lang[:2].lower(), "format": "ogg"},
            timeout=60,
        )
        if not resp.ok:
            logger.warning("WhatsApp TTS failed: %s - %s", resp.status_code, resp.text[:200])
            return None
        if not resp.content:
            logger.warning("WhatsApp TTS: empty response body")
            return None
        data = resp.content
        if data[:4] not in (b"OggS", b"RIFF"):
            logger.warning("WhatsApp TTS: unknown audio format (magic: %s)", data[:4].hex())
            return None
        suffix = ".ogg" if data[:4] == b"OggS" else ".wav"
        with tempfile.NamedTemporaryFile(prefix="vaf_wa_", suffix=suffix, delete=False) as f:
            f.write(data)
            logger.info("WhatsApp TTS: wrote %d bytes to %s", len(data), f.name)
            return f.name
    except Exception as e:
        logger.warning("WhatsApp TTS synthesis error: %s", e)
        return None


def _transcribe_voice_file(voice_path: str) -> tuple[Optional[str], Optional[str]]:
    """
    Transcribe a voice file via Docker Whisper STT.
    Returns (transcribed_text, detected_language) or (None, None) on error.
    """
    try:
        path_obj = Path(voice_path)
        if not path_obj.is_file():
            logger.warning("WhatsApp STT: voice file not found: %s", voice_path)
            return None, None
        file_size = path_obj.stat().st_size
        stt_url = (Config.get("speech_stt_docker_url") or "http://localhost:5003").strip().rstrip("/")
        asr_endpoint = f"{stt_url}/asr"
        # Use MIME type from extension (Node sends .ogg for PTT, .opus for other audio)
        ext = (path_obj.suffix or "").lower()
        mime = "audio/ogg" if ext == ".ogg" else ("audio/opus" if ext == ".opus" else "audio/ogg")
        filename = f"voice{ext}" if ext else "voice.ogg"
        logger.info("WhatsApp STT: transcribing %s (%d bytes) via %s", voice_path, file_size, asr_endpoint)
        with open(voice_path, "rb") as f:
            stt_resp = requests.post(
                asr_endpoint,
                files={"audio_file": (filename, f, mime)},
                params={"encode": "true", "output": "json"},
                timeout=60,
            )
        if stt_resp.status_code == 404:
            transcribe_endpoint = f"{stt_url}/transcribe"
            with open(voice_path, "rb") as f:
                stt_resp = requests.post(
                    transcribe_endpoint,
                    files={"audio_file": (filename, f, mime)},
                    params={"encode": "true", "output": "json"},
                    timeout=60,
                )
        if not stt_resp.ok:
            logger.warning("WhatsApp STT failed: %s - %s", stt_resp.status_code, (stt_resp.text or "")[:200])
            return None, None
        try:
            data = stt_resp.json()
        except Exception:
            text = (stt_resp.text or "").strip()
            data = {}
        text = (data.get("text") or data.get("transcript") or "").strip()
        if not text and isinstance(data.get("results"), list) and data["results"]:
            text = (data["results"][0].get("transcript") or "").strip()
        language = data.get("language", "en")
        logger.info("WhatsApp voice transcribed: lang=%s, text=%s...", language, (text or "")[:50])
        return text or None, language
    except Exception as e:
        logger.warning("WhatsApp voice transcription error: %s", e)
        return None, None
    finally:
        try:
            os.unlink(voice_path)
        except Exception:
            pass


def _deliver_send_result(req_id: str, success: bool, error: Optional[str] = None) -> None:
    """Deliver send result to waiting caller."""
    with _pending_sends_lock:
        q = _pending_sends.pop(req_id, None)
    if q is not None:
        try:
            q.put((success, error or ""))
        except Exception:
            pass


def _allow_from_match(sender_jid: str, allowed_phones: List[str]) -> bool:
    """Check if sender JID matches any allowed phone number. Normalizes 0176... and +49176... to same form."""
    sender_canonical = _phone_digits_canonical(sender_jid)
    if not sender_canonical:
        return False
    for p in allowed_phones:
        if _phone_digits_canonical(p) == sender_canonical:
            return True
    return False


def _get_allowed_phones_for_user(username: str, user_scope_id: str) -> Tuple[List[str], List[str]]:
    """Return (config_phones, allowed_phones) for inbound checks. config_phones = whitelist only; allowed_phones = whitelist + Front Office contacts. Called per message so new FO contacts work without bridge restart."""
    config_phones: List[str] = []
    allowed_phones: List[str] = []
    whatsapp_config = Config.get("whatsapp_config") or {}
    if not isinstance(whatsapp_config, dict):
        return config_phones, allowed_phones
    for entry in (whatsapp_config.get("whitelist") or []):
        if not isinstance(entry, dict) or not entry.get("phone_number"):
            continue
        p = str(entry.get("phone_number", "")).strip()
        if not p:
            continue
        if entry.get("vaf_username") == username or str(entry.get("user_scope_id")) == user_scope_id:
            if p not in config_phones:
                config_phones.append(p)
            if p not in allowed_phones:
                allowed_phones.append(p)
    try:
        from vaf.core.contacts_store import get_contacts_allowing_assistant, _contact_whatsapp_values
        seen_phones: set = set()
        for scope_arg in (user_scope_id, None, get_local_admin_scope_id()):
            scope_str = str(scope_arg) if scope_arg else ""
            for c in get_contacts_allowing_assistant(username, user_scope_id=scope_arg or None):
                for p in _contact_whatsapp_values(c):
                    if p and str(p).strip():
                        pn = str(p).strip()
                        key = _phone_digits_canonical(pn)
                        if key and key not in seen_phones:
                            seen_phones.add(key)
                            allowed_phones.append(pn)
    except Exception:
        pass
    return config_phones, allowed_phones


def _get_users_to_run() -> List[Tuple[str, str, Path]]:
    """Return list of (user_scope_id, username, auth_dir) for users with linked WhatsApp and whitelist entry."""
    result: List[Tuple[str, str, Path]] = []
    whatsapp_config = Config.get("whatsapp_config") or {}
    if not isinstance(whatsapp_config, dict):
        return result
    whitelist = whatsapp_config.get("whitelist") or []

    for entry in whitelist:
        if not isinstance(entry, dict):
            continue
        if not entry.get("phone_number"):
            continue
        username = (entry.get("vaf_username") or "admin").strip()
        scope = entry.get("user_scope_id") or get_local_admin_scope_id()
        if not whatsapp_auth_exists(username):
            continue
        auth_dir = get_whatsapp_auth_dir(username)
        result.append((str(scope), username, auth_dir))

    return result


def _run_user_process(username: str, auth_dir: Path) -> Optional[subprocess.Popen]:
    """Spawn Node wa-bridge for one user. Returns process or None on failure."""
    node = _node_path()
    if not node:
        logger.error("Node.js not found. Install Node >= 18 for WhatsApp support.")
        return None
    wa_js = _wa_bridge_path()
    if not wa_js.exists():
        logger.error("wa-bridge.js not found at %s", wa_js)
        return None
    auth_str = str(auth_dir.resolve())
    kwargs: dict = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(
            [node, str(wa_js), "--auth-dir", auth_str],
            **kwargs,
        )
        return proc
    except Exception as e:
        logger.exception("Failed to start WhatsApp bridge for %s: %s", username, e)
        return None


def _sender_loop() -> None:
    """Read (username, chat_jid, text, voice_path?, req_id?) from queue, write to that user's Node stdin."""
    global _outgoing_queue, _processes
    while True:
        try:
            item = _outgoing_queue.get(timeout=1.0)
            if item is None:
                break
            req_id = item[4] if len(item) >= 5 else None
            voice_path = item[3] if len(item) >= 4 else None
            document_path = item[5] if len(item) >= 6 else None
            username, chat_jid, text = item[0], item[1], (item[2] or "")
            if not username or not chat_jid:
                continue
            if not voice_path and not text and not document_path:
                continue
            with _process_lock:
                proc = _processes.get(username)
                if (not proc or proc.poll() is not None) and len(_processes) == 1:
                    proc = next(iter(_processes.values()))
            if not proc or proc.poll() is not None:
                logger.warning("WhatsApp process for %s not running, dropped reply", username)
                if req_id:
                    _deliver_send_result(req_id, False, "Process not running (bridge may have restarted)")
                try:
                    from vaf.core.log_helper import log_whatsapp_reply
                    log_whatsapp_reply(f"DROPPED process_not_running username={username} jid={chat_jid}")
                except Exception:
                    pass
                continue
            if voice_path:
                try:
                    from pathlib import Path
                    p = Path(voice_path)
                    if p.is_file():
                        size = p.stat().st_size
                        logger.info("WhatsApp sending voice to %s, path=%s, size=%s", chat_jid, p, size)
                        cmd = {"cmd": "send_voice", "to": chat_jid, "path": str(p.resolve())}
                        if req_id:
                            cmd["req_id"] = req_id
                        proc.stdin.write(json.dumps(cmd) + "\n")
                        proc.stdin.flush()
                    else:
                        err = "Voice file not found"
                        logger.warning("WhatsApp voice file not found: %s", voice_path)
                        if req_id:
                            _deliver_send_result(req_id, False, err)
                except Exception as e:
                    logger.warning("WhatsApp voice send failed for %s: %s", username, e)
                    if req_id:
                        _deliver_send_result(req_id, False, str(e))
            elif document_path:
                try:
                    from pathlib import Path
                    p = Path(document_path)
                    if p.is_file():
                        cmd = {"cmd": "send_document", "to": chat_jid, "path": str(p.resolve()), "caption": (text or "")[:1024]}
                        if req_id:
                            cmd["req_id"] = req_id
                        proc.stdin.write(json.dumps(cmd) + "\n")
                        proc.stdin.flush()
                    else:
                        err = "Document file not found"
                        logger.warning("WhatsApp document not found: %s", document_path)
                        if req_id:
                            _deliver_send_result(req_id, False, err)
                except Exception as e:
                    logger.warning("WhatsApp document send failed for %s: %s", username, e)
                    if req_id:
                        _deliver_send_result(req_id, False, str(e))
            else:
                chunks = chunk_whatsapp_text(text)
                for i, chunk in enumerate(chunks):
                    cmd = {"cmd": "send", "to": chat_jid, "text": chunk}
                    if req_id and i == len(chunks) - 1:
                        cmd["req_id"] = req_id
                    try:
                        proc.stdin.write(json.dumps(cmd) + "\n")
                        proc.stdin.flush()
                    except Exception as e:
                        logger.warning("WhatsApp send failed for %s: %s", username, e)
                        if req_id:
                            _deliver_send_result(req_id, False, str(e))
                        break
            try:
                from vaf.core.log_helper import log_whatsapp_reply
                log_whatsapp_reply(f"SENDER ok username={username} jid={chat_jid}")
            except Exception:
                pass
            try:
                chat_id = f"+{_jid_to_e164(chat_jid)}" if _jid_to_e164(chat_jid) else str(chat_jid or "")
                from vaf.core.whatsapp_message_store import append_message
                if voice_path:
                    body = "[Voice message]"
                    ctype = "voice"
                elif document_path:
                    body = "[Document] " + (text or "") if text else "[Document]"
                    ctype = "document"
                else:
                    body = text
                    ctype = "text"
                append_message(username, chat_id or chat_jid, body, direction="out", content_type=ctype)
                if chat_id:
                    _append_chat_activity(chat_id, None, "out")
            except Exception:
                pass
        except queue.Empty:
            continue
        except Exception as e:
            logger.exception("WhatsApp sender error: %s", e)


def _is_jid_whitelisted(username: str, chat_jid: str, user_scope_id: Optional[str] = None) -> bool:
    """Verify chat_jid is in whitelist for this user (config whitelist or contact with allow_as_assistant_user). @lid = self-chat, always allowed. Pass user_scope_id when replying to a contact so FO contacts in scoped storage are found."""
    if (chat_jid or "").strip().endswith("@lid"):
        return True  # Self-chat: reply to own saved messages
    uname = (username or "").strip() or "admin"
    scope = str(user_scope_id).strip() if user_scope_id else None
    _, allowed_phones = _get_allowed_phones_for_user(uname, scope or get_local_admin_scope_id())
    return _allow_from_match(chat_jid, allowed_phones)


def _enqueue_reply(username: str, chat_jid: str, text: str, voice_path: Optional[str] = None, user_scope_id: Optional[str] = None) -> None:
    """Callback for headless_runner: enqueue reply. If voice_path, send as voice message.
    When user sent a voice message, auto-reply with voice (TTS) when possible. user_scope_id helps resolve FO contacts (scoped storage)."""
    if not _is_jid_whitelisted(username, chat_jid, user_scope_id=user_scope_id):
        logger.warning("WhatsApp: blocked reply to non-whitelisted JID %s for user %s", chat_jid, username)
        return
    # If no voice_path but user sent voice, try to synthesize TTS (like Telegram)
    if not voice_path and text:
        with _voice_reply_lock:
            lang = _voice_reply_pending.pop(f"{username}|{chat_jid}", None)
        if lang:
            voice_path = _synthesize_voice_for_reply(text, lang)
            if voice_path:
                try:
                    from vaf.core.log_helper import log_whatsapp_reply
                    log_whatsapp_reply(f"BRIDGE enqueue voice_reply username={username} lang={lang}")
                except Exception:
                    pass
    try:
        from vaf.core.log_helper import log_whatsapp_reply
        log_whatsapp_reply(f"BRIDGE enqueue username={username} jid={chat_jid} len={len(text)} voice={bool(voice_path)}")
    except Exception:
        pass
    if _outgoing_queue is not None:
        try:
            _outgoing_queue.put((username, chat_jid, text, voice_path))
        except Exception:
            pass


def send_whatsapp_with_confirmation(
    username: str,
    chat_jid: str,
    text: str,
    voice_path: Optional[str] = None,
    document_path: Optional[str] = None,
    timeout: float = 15.0,
    allow_contact_send: bool = False,
) -> str:
    # Voice/document need more time (TTS synthesis, file upload)
    if voice_path or document_path:
        timeout = max(timeout, 45.0)
    """
    Send a WhatsApp message (text, voice, or document) and wait for delivery confirmation from the Node bridge.
    Returns a success message or an error string for the agent to report.
    When allow_contact_send is True, the recipient may be any phone/JID (e.g. a contact); otherwise only whitelisted.
    """
    if not allow_contact_send and not _is_jid_whitelisted(username, chat_jid):
        return (
            "WhatsApp: Cannot send – chat/phone number is not in the whitelist. "
            "Add your number in Settings → Connections → WhatsApp."
        )
    if _outgoing_queue is None:
        return (
            "WhatsApp bridge is not running. Start it in Settings → Connections → WhatsApp (click Start)."
        )
    with _process_lock:
        proc = _processes.get(username)
        if (not proc or proc.poll() is not None) and len(_processes) == 1:
            proc = next(iter(_processes.values()), None)
        if not proc or proc.poll() is not None:
            return (
                "WhatsApp process for this user is not running. "
                "Try: Settings → Connections → WhatsApp → Stop, then Start. "
                "Ensure WhatsApp is linked (QR scanned) and your number is in the whitelist."
            )
    req_id = str(uuid.uuid4())
    result_queue: queue.Queue = queue.Queue()
    with _pending_sends_lock:
        _pending_sends[req_id] = result_queue
    try:
        _outgoing_queue.put((username, chat_jid, text or "", voice_path, req_id, document_path))
    except Exception as e:
        with _pending_sends_lock:
            _pending_sends.pop(req_id, None)
        return f"Failed to enqueue message: {e}"
    try:
        success, error = result_queue.get(timeout=timeout)
        if success:
            if document_path:
                return "Document sent via WhatsApp."
            if voice_path:
                return "Voice message sent via WhatsApp."
            return "Message sent via WhatsApp."
        return f"WhatsApp could not deliver the message: {error}"
    except queue.Empty:
        with _pending_sends_lock:
            _pending_sends.pop(req_id, None)
        return (
            "No delivery confirmation from the WhatsApp bridge within the time limit. "
            "If the message appeared in WhatsApp, it was delivered; otherwise check Settings → Connections → WhatsApp (bridge running, linked)."
        )


def _read_user_process(
    username: str,
    user_scope_id: str,
    auth_dir: Path,
    proc: subprocess.Popen,
) -> None:
    """Read JSON lines from process stdout, handle events. allowed_phones are fetched per message so new Front Office contacts work without bridge restart."""
    try:
        for line in proc.stdout:
            if _bridge_stop.is_set():
                break
            line = (line or "").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                try:
                    from vaf.core.log_helper import log_whatsapp_qr
                    log_whatsapp_qr(f"[Python] JSON decode error: {e}")
                except Exception:
                    pass
                continue
            typ = obj.get("type")
            if typ in ("message", "chats", "connected", "connection_closed"):
                try:
                    from vaf.core.log_helper import log_whatsapp_qr
                    log_whatsapp_qr(f"[Python] got type={typ!r}")
                except Exception:
                    pass
            try:
                _dispatch_bridge_event(username, user_scope_id, typ, obj)
            except Exception as e:
                logger.exception("WhatsApp bridge event handler failed for type=%s: %s", typ, e)
                try:
                    from vaf.core.log_helper import log_whatsapp_qr
                    log_whatsapp_qr(f"[Python] ERROR handling type={typ!r}: {e}")
                except Exception:
                    pass
    except Exception as e:
        logger.exception("WhatsApp bridge stdout read loop failed: %s", e)
        try:
            from vaf.core.log_helper import log_whatsapp_qr
            log_whatsapp_qr(f"[Python] FATAL read loop: {type(e).__name__}: {e}")
        except Exception:
            pass


def _dispatch_bridge_event(username: str, user_scope_id: str, typ: str, obj: Dict[str, Any]) -> None:
    """Handle one JSON event from the bridge (pong, message, chats, etc.)."""
    if typ == "pong":
        connected = bool(obj.get("connected", False))
        with _connection_lock:
            _connection_status[username] = connected
            ev = _connection_events.get(username)
            if ev:
                ev.set()
        if not connected:
            try:
                from vaf.core.log_helper import log_whatsapp_qr
                log_whatsapp_qr(f"[Python] pong connected=false → UI shows orange (bridge running, WhatsApp not connected)")
            except Exception:
                pass
    elif typ == "send_result":
        req_id = obj.get("req_id")
        if req_id:
            _deliver_send_result(req_id, bool(obj.get("success")), obj.get("error", ""))
    elif typ == "qr":
        logger.warning("WhatsApp session expired for %s – bridge needs QR but cannot show it. Stopping bridge and disabling.", username)
        try:
            cfg = Config.load()
            wc = cfg.get("whatsapp_config") or {}
            if isinstance(wc, dict):
                wc = dict(wc)
                wc["enabled"] = False
                cfg["whatsapp_config"] = wc
                Config.save(cfg)
            stop_bridge()
        except Exception as e:
            logger.exception("Failed to disable WhatsApp on session expiry: %s", e)
    elif typ == "connected":
        with _connection_lock:
            _connection_status[username] = True
        try:
            from vaf.core.log_helper import log_whatsapp_qr
            log_whatsapp_qr(f"[Python] connected → status=open for {username}")
        except Exception:
            pass
        logger.info("WhatsApp connected for user %s", username)
    elif typ == "connection_closed":
        with _connection_lock:
            _connection_status[username] = False
        try:
            from vaf.core.log_helper import log_whatsapp_qr
            code = obj.get("statusCode")
            log_whatsapp_qr(f"[Python] connection_closed statusCode={code} → UI will show orange")
        except Exception:
            pass
    elif typ == "owner_sent":
        from_jid = (obj.get("from") or "").strip()
        ts = obj.get("ts")
        if from_jid and isinstance(ts, (int, float)) and ts > 0:
            try:
                cfg = Config.load()
                wc = cfg.get("whatsapp_config") or {}
                if not isinstance(wc, dict):
                    wc = {}
                else:
                    wc = dict(wc)
                owner_control = dict(wc.get("owner_control") or {})
                owner_control[from_jid] = int(ts)
                now = int(time.time())
                max_age = 24 * 3600
                owner_control = {k: v for k, v in owner_control.items() if (now - v) <= max_age}
                if len(owner_control) > 50:
                    by_ts = sorted(owner_control.items(), key=lambda x: -x[1])
                    owner_control = dict(by_ts[:50])
                wc["owner_control"] = owner_control
                cfg["whatsapp_config"] = wc
                Config.save(cfg)
                try:
                    from vaf.core.log_helper import log_whatsapp_inbound, log_whatsapp_qr
                    log_whatsapp_inbound(f"owner_sent chat={from_jid} → owner has control")
                    log_whatsapp_qr(f"[inbound] owner_sent chat={from_jid} → owner has control")
                except Exception:
                    pass
            except Exception as e:
                logger.warning("WhatsApp: failed to save owner_control: %s", e)
    elif typ == "message":
        from_jid = obj.get("from") or obj.get("senderJid")
        try:
            from vaf.core.log_helper import log_whatsapp_qr
            log_whatsapp_qr(f"[inbound] MESSAGE from={from_jid}")
        except Exception:
            pass
        try:
            config_phones, allowed_phones = _get_allowed_phones_for_user(username, user_scope_id)
        except Exception as e:
            logger.warning("WhatsApp: get_allowed_phones_for_user failed: %s", e)
            try:
                from vaf.core.log_helper import log_whatsapp_qr
                log_whatsapp_qr(f"[inbound] ERROR get_allowed_phones: {e}")
            except Exception:
                pass
            return
        if not allowed_phones:
            logger.debug("WhatsApp: no allowFrom for user %s, rejecting inbound from %s", username, obj.get("from"))
        from_e164 = obj.get("fromE164")  # Resolved via Baileys lidMapping when @lid
        body = (obj.get("body") or "").strip()
        voice_path = obj.get("voice_path")
        voice_lang: Optional[str] = None
        was_voice = bool(voice_path)
        if voice_path and body == "<voice>":
            try:
                transcript, voice_lang = _transcribe_voice_file(voice_path)
                body = transcript if transcript else "<media:audio>"
            except Exception as e:
                logger.warning("WhatsApp: transcription failed: %s", e)
                try:
                    from vaf.core.log_helper import log_whatsapp_qr
                    log_whatsapp_qr(f"[inbound] ERROR transcribe: {e}")
                except Exception:
                    pass
                body = "<media:audio>"
        if not body:
            try:
                from vaf.core.log_helper import log_whatsapp_inbound
                log_whatsapp_inbound(f"SKIP no_body from={from_jid}")
            except Exception:
                pass
            return
        # Self-chat: trust Node's selfChat (Node resolves @lid to E.164 and compares to self; do NOT treat all @lid as self – LID is used for other 1:1 chats too, e.g. baba)
        is_self_chat = obj.get("selfChat") is True
        # Allow only when JID or resolved fromE164 matches whitelist/FO. Do NOT accept unresolved @lid (LID numeric part is not a phone; would let strangers through).
        allow_match = bool(allowed_phones) and (
            _allow_from_match(from_jid or "", allowed_phones)
            or (from_e164 and _allow_from_match(from_e164, allowed_phones))
        )
        if (from_jid or "").endswith("@lid") and not from_e164 and not allow_match:
            try:
                from vaf.core.log_helper import log_whatsapp_inbound, log_whatsapp_qr
                log_whatsapp_inbound(f"REJECT unresolved @lid from={from_jid} (not in whitelist/contacts; LID is not a phone number)")
                log_whatsapp_qr(f"[inbound] REJECT unresolved @lid from={from_jid} (not in whitelist/contacts)")
            except Exception:
                pass
        if not is_self_chat and not allow_match:
            from_digits = _phone_digits_canonical(from_jid or "") or (_phone_digits_canonical(from_e164 or "") if from_e164 else "")
            try:
                from vaf.core.log_helper import log_whatsapp_inbound, log_whatsapp_qr
                log_whatsapp_inbound(f"REJECT not_whitelist from={from_jid} allowed_count={len(allowed_phones)}")
                log_whatsapp_qr(f"[inbound] REJECT from={from_jid} from_digits={from_digits or '?'} allowed_count={len(allowed_phones)}")
            except Exception:
                pass
            logger.warning(
                "WhatsApp: rejected message from %s (not in allowFrom). allowed_phones count=%s; check Front Office contact phone format (use +49… or 0…).",
                from_jid,
                len(allowed_phones),
            )
            return
        try:
            from vaf.core.log_helper import log_whatsapp_inbound, log_whatsapp_qr
            log_whatsapp_inbound(f"ACCEPT from={from_jid} self_chat={is_self_chat} body_len={len(body)}")
            log_whatsapp_qr(f"[inbound] ACCEPT from={from_jid} body_len={len(body)}")
            if (from_jid or "").endswith("@lid") and not from_e164:
                log_whatsapp_qr(f"[inbound] ACCEPT unresolved @lid (LID→E.164 not available); reply will go to this chat")
        except Exception:
            pass
        save_whatsapp_chat_jid(user_scope_id, username, from_jid)
        # Use fromE164 when available (resolved @lid from Node); else derive from JID. For unresolved @lid use JID as chat_id and LID part for session.
        raw = from_e164 or _jid_to_e164(from_jid) or ""
        chat_id = _to_e164_display(raw) if raw else str(from_jid or "")
        if not chat_id:
            chat_id = str(from_jid or "")
        if raw:
            resolved_digits = _normalize_phone(raw)
        elif (from_jid or "").endswith("@lid"):
            resolved_digits = (from_jid or "").split("@")[0].strip() or "lid"
        else:
            resolved_digits = ""
        # Persist LID→E.164 so dashboard can show this chat under the contact's phone (Web UI session/history)
        if from_e164 and (from_jid or "").endswith("@lid"):
            try:
                cfg = Config.load()
                wc = cfg.get("whatsapp_config") or {}
                if isinstance(wc, dict):
                    wc = dict(wc)
                    lid_map = dict(wc.get("lid_to_e164") or {})
                    lid_map[str(from_jid)] = _to_e164_display(from_e164)
                    wc["lid_to_e164"] = lid_map
                    cfg["whatsapp_config"] = wc
                    Config.save(cfg)
            except Exception:
                pass
        _append_chat_activity(chat_id, user_scope_id, "in")
        try:
            from vaf.core.whatsapp_message_store import append_message
            append_message(username, chat_id, body, direction="in", sender_jid=from_jid, message_id=obj.get("messageId") or obj.get("message_id"), content_type="voice" if was_voice else "text")
        except Exception:
            pass
        whatsapp_config = Config.get("whatsapp_config") or {}
        inbound_to_agent = whatsapp_config.get("inbound_to_agent", True) if isinstance(whatsapp_config, dict) else True
        # Self-chat (admin number = bridge/linked number): do not reply; store as note/backlog only so the agent doesn't talk to itself
        if is_self_chat:
            try:
                from vaf.core.log_helper import log_whatsapp_inbound, log_whatsapp_qr
                log_whatsapp_inbound(f"SELF_CHAT note from={from_jid} body_len={len(body)} (stored, no reply)")
                log_whatsapp_qr(f"[inbound] SELF_CHAT stored as note from={from_jid} (admin=bridge number, no agent reply)")
            except Exception:
                pass
            logger.info("WhatsApp self-chat from %s stored as note (no agent reply); user %s", from_jid, username)
        elif inbound_to_agent:
            owner_control = (whatsapp_config.get("owner_control") or {}) if isinstance(whatsapp_config, dict) else {}
            last_owner_ts = owner_control.get(from_jid) if from_jid else None
            if last_owner_ts is not None and (time.time() - last_owner_ts) < 600:
                try:
                    from vaf.core.log_helper import log_whatsapp_inbound, log_whatsapp_qr
                    log_whatsapp_inbound(f"owner_control skip from={from_jid} (owner has control, 10 min not elapsed)")
                    log_whatsapp_qr(f"[inbound] owner_control: skip reply from={from_jid} (owner has control, 10 min not elapsed)")
                except Exception:
                    pass
                logger.info("WhatsApp: skip agent reply for %s (owner has control, 10 min not elapsed)", from_jid)
            else:
                session_id = f"whatsapp_{username}_{resolved_digits or 'self'}"
                in_config = _allow_from_match(from_jid or "", config_phones) or (
                    from_e164 and _allow_from_match(from_e164, config_phones)
                )
                from_contact = allow_match and not in_config
                metadata: Dict[str, Any] = {
                    "user_scope_id": user_scope_id,
                    "username": username,
                    "whatsapp_chat_jid": from_jid,
                }
                if from_contact:
                    metadata["from_contact"] = True
                if voice_lang:
                    metadata["voice_lang"] = voice_lang
                    with _voice_reply_lock:
                        _voice_reply_pending[f"{username}|{from_jid}"] = voice_lang
                tq = TaskQueue()
                tq.add(
                    session_id=session_id,
                    input_text=body,
                    source="whatsapp",
                    metadata=metadata,
                )
                try:
                    from vaf.core.log_helper import log_whatsapp_inbound, log_whatsapp_qr
                    log_whatsapp_inbound(f"ENQUEUED session={session_id} user={username}")
                    log_whatsapp_qr(f"[inbound] ENQUEUED session={session_id} from={from_jid}")
                except Exception:
                    pass
                logger.info("WhatsApp message enqueued from %s for user %s", from_jid, username)
        else:
            logger.debug("WhatsApp inbound not forwarded to agent (inbound_to_agent=false); user still reachable for sends.")
    elif typ == "chats":
        chats = obj.get("chats")
        if isinstance(chats, list):
            with _chat_lists_lock:
                _chat_lists[username] = chats
            ev = _chat_list_events.get(username)
            if ev:
                ev.set()
            with _connection_lock:
                _connection_status[username] = True
                conn_ev = _connection_events.get(username)
                if conn_ev:
                    conn_ev.set()
    elif typ == "error":
        err_msg = obj.get("message", "")
        logger.warning("WhatsApp error for %s: %s", username, err_msg)
        try:
            from vaf.core.log_helper import log_whatsapp_reply, log_whatsapp_qr
            log_whatsapp_reply(f"ERROR username={username} msg={err_msg}")
            log_whatsapp_qr(f"[Python] bridge error: {err_msg}")
        except Exception:
            pass
        with _connection_lock:
            _connection_status[username] = False


def _forward_bridge_stderr(username: str, proc: subprocess.Popen) -> None:
    """Read bridge stderr and append to whatsapp_qr.log for debugging connection issues."""
    try:
        from vaf.core.log_helper import log_whatsapp_qr
        for line in (proc.stderr or []):
            if _bridge_stop.is_set():
                break
            s = (line or "").strip()
            if s:
                log_whatsapp_qr(f"[bridge/{username}] {s}")
                logger.debug("[wa-bridge] %s", s)
    except Exception:
        pass


def _run_bridge() -> None:
    """Main bridge loop: start processes for each user, register callback, wait for stop."""
    global _processes
    users = _get_users_to_run()
    if not users:
        logger.info("WhatsApp bridge: no users with linked auth, exiting")
        return

    set_whatsapp_reply_callback(_enqueue_reply)

    for user_scope_id, username, auth_dir in users:
        proc = _run_user_process(username, auth_dir)
        if proc:
            with _process_lock:
                _processes[username] = proc
            threading.Thread(
                target=_read_user_process,
                args=(username, user_scope_id, auth_dir, proc),
                daemon=True,
            ).start()
            threading.Thread(
                target=_forward_bridge_stderr,
                args=(username, proc),
                daemon=True,
            ).start()

    _bridge_stop.wait()
    set_whatsapp_reply_callback(None)
    with _process_lock:
        for username, proc in list(_processes.items()):
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        _processes.clear()


def start_bridge() -> bool:
    """Start the WhatsApp bridge. Returns True if started."""
    global _bridge_thread, _sender_thread, _outgoing_queue, _bridge_stop

    whatsapp_config = Config.get("whatsapp_config") or {}
    if not isinstance(whatsapp_config, dict) or not whatsapp_config.get("enabled"):
        return False
    if not _node_path():
        logger.error("Node.js not found. Install Node >= 18 for WhatsApp.")
        return False
    if not _wa_bridge_path().exists():
        logger.error("wa-bridge.js not found. Run: cd vaf/whatsapp_node && npm install")
        return False

    if _bridge_thread is not None and _bridge_thread.is_alive():
        return True

    _bridge_stop.clear()
    _outgoing_queue = queue.Queue()
    _sender_thread = threading.Thread(target=_sender_loop, daemon=True)
    _sender_thread.start()
    _bridge_thread = threading.Thread(target=_run_bridge, daemon=True)
    _bridge_thread.start()
    logger.info("WhatsApp bridge started")
    return True


def stop_bridge() -> None:
    """Request bridge stop."""
    global _outgoing_queue
    _bridge_stop.set()
    if _outgoing_queue is not None:
        try:
            _outgoing_queue.put(None)
        except Exception:
            pass
    logger.info("WhatsApp bridge stop requested")


def restart_bridge() -> bool:
    """Stop the bridge, wait for full shutdown, then start again. Returns True if restarted."""
    global _bridge_thread
    if _bridge_thread is None or not _bridge_thread.is_alive():
        return start_bridge()
    stop_bridge()
    try:
        _bridge_thread.join(timeout=12)
    except Exception:
        pass
    return start_bridge()


def is_bridge_running() -> bool:
    return _bridge_thread is not None and _bridge_thread.is_alive()


def has_process_for_user(username: str) -> bool:
    """True if we have a running Node process for this user (so sends will not be dropped)."""
    if not is_bridge_running():
        return False
    uname = (username or "").strip() or "admin"
    with _process_lock:
        proc = _processes.get(uname)
        if proc and proc.poll() is None and proc.stdin:
            return True
        if len(_processes) == 1:
            p = next(iter(_processes.values()))
            return p.poll() is None and p.stdin is not None
        return False


def _request_chats(username: str) -> Optional[str]:
    """Send getChats to the Node process. Returns the username whose process was used (for lookup), or None."""
    with _process_lock:
        proc = _processes.get(username)
        target_username = username
        if not proc or proc.poll() is not None or not proc.stdin:
            if len(_processes) == 1:
                target_username = next(iter(_processes.keys()))
                proc = _processes.get(target_username)
            else:
                proc = None
    if proc and proc.poll() is None and proc.stdin:
        try:
            proc.stdin.write(json.dumps({"cmd": "getChats"}) + "\n")
            proc.stdin.flush()
            return target_username
        except Exception as e:
            logger.warning("WhatsApp getChats failed for %s: %s", username, e)
    return None


def get_whatsapp_chats(username: str, force_refresh: bool = False, wait_timeout: float = 3.0) -> List[Dict[str, Any]]:
    """Return the list of all WhatsApp chats. Requests from bridge when running."""
    if not is_bridge_running():
        with _chat_lists_lock:
            return list(_chat_lists.get(username, []))
    used_username = _request_chats(username)
    if not used_username:
        with _chat_lists_lock:
            return list(_chat_lists.get(username, []))
    with _chat_lists_lock:
        if used_username not in _chat_list_events:
            _chat_list_events[used_username] = threading.Event()
        ev = _chat_list_events[used_username]
        ev.clear()
    ev.wait(timeout=wait_timeout)
    with _chat_lists_lock:
        return list(_chat_lists.get(used_username, []))


def get_connection_status(username: str, wait_timeout: float = 5.0) -> bool:
    """Check if the WhatsApp socket is connected (Node has currentSock). Runs ping/pong with the bridge. Timeout 5s so read loop can be busy (e.g. transcribing)."""
    if not is_bridge_running():
        return False
    uname = (username or "").strip() or "admin"
    with _process_lock:
        proc = _processes.get(uname)
        target = uname
        if not proc or proc.poll() is not None or not proc.stdin:
            if len(_processes) == 1:
                target = next(iter(_processes.keys()))
                proc = _processes.get(target)
            else:
                proc = None
    if not proc or proc.poll() is not None or not proc.stdin:
        return False
    with _connection_lock:
        if target not in _connection_events:
            _connection_events[target] = threading.Event()
        ev = _connection_events[target]
        ev.clear()
    try:
        proc.stdin.write(json.dumps({"cmd": "ping"}) + "\n")
        proc.stdin.flush()
    except Exception:
        return False
    ev.wait(timeout=wait_timeout)
    with _connection_lock:
        return _connection_status.get(target, False)
