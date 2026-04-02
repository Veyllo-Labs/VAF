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
from vaf.core.channel_ingress_policy import evaluate_ingress, should_log_unauthorized
from vaf.core.messaging_connections import save_whatsapp_chat_jid
from vaf.core.platform import Platform
from vaf.core.task_queue import TaskQueue
from vaf.core.whatsapp_auth import get_whatsapp_auth_dir, whatsapp_auth_exists
from vaf.core.whatsapp_reply import set_whatsapp_reply_callback
from vaf.core.whatsapp_send import chunk_whatsapp_text

logger = logging.getLogger("vaf.api.whatsapp_bridge")

_bridge_thread: Optional[threading.Thread] = None
_sender_thread: Optional[threading.Thread] = None
_chat_sync_thread: Optional[threading.Thread] = None
_bridge_stop = threading.Event()
_processes: Dict[str, subprocess.Popen] = {}
_process_lock = threading.Lock()
_outgoing_queue: Optional[queue.Queue] = None
_chat_lists: Dict[str, List[Dict[str, Any]]] = {}
_chat_list_events: Dict[str, threading.Event] = {}
_chat_lists_lock = threading.Lock()

# LID→E.164 from Node (Baileys lidMapping), so UI can show "this LID = this number" when WhatsApp resolved it
_lid_mappings: Dict[str, List[Dict[str, str]]] = {}
_lid_mappings_events: Dict[str, threading.Event] = {}
_lid_mappings_lock = threading.Lock()

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
_external_pending_results: Dict[str, Path] = {}
_external_pending_results_lock = threading.Lock()

# Per-chat inbound debounce (like Telegram 5s rule but 7s for WhatsApp).
# Key: "username|from_jid" → accumulated state dict.
# When a new message arrives we reset the timer; when it fires we flush to TaskQueue.
_wa_pending: Dict[str, Dict[str, Any]] = {}
_wa_pending_lock = threading.Lock()
WA_DEBOUNCE_SECONDS = 7


def _ipc_base_dir() -> Path:
    return Platform.data_dir() / "whatsapp_bridge_ipc"


def _ipc_requests_dir() -> Path:
    return _ipc_base_dir() / "requests"


def _ipc_results_dir() -> Path:
    return _ipc_base_dir() / "results"


def _ipc_state_path() -> Path:
    return _ipc_base_dir() / "bridge_state.json"


def _ensure_ipc_dirs() -> None:
    _ipc_requests_dir().mkdir(parents=True, exist_ok=True)
    _ipc_results_dir().mkdir(parents=True, exist_ok=True)


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _write_bridge_state(running: bool) -> None:
    try:
        _ensure_ipc_dirs()
        with _process_lock:
            usernames = sorted(_processes.keys())
        _write_json_atomic(
            _ipc_state_path(),
            {
                "running": bool(running),
                "usernames": usernames,
                "updated_at": time.time(),
            },
        )
    except Exception:
        pass


def _read_bridge_state() -> Dict[str, Any]:
    try:
        path = _ipc_state_path()
        if not path.exists():
            return {"running": False, "usernames": []}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"running": False, "usernames": []}
        usernames = data.get("usernames") or []
        if not isinstance(usernames, list):
            usernames = []
        return {
            "running": bool(data.get("running")),
            "usernames": [str(u).strip() for u in usernames if str(u).strip()],
            "updated_at": float(data.get("updated_at") or 0.0),
        }
    except Exception:
        return {"running": False, "usernames": []}


def _write_external_send_result(response_path: Optional[Path], success: bool, error: Optional[str] = None) -> None:
    if response_path is None:
        return
    try:
        _write_json_atomic(
            response_path,
            {
                "success": bool(success),
                "error": str(error or ""),
                "updated_at": time.time(),
            },
        )
    except Exception:
        pass


def _register_external_response_path(req_id: Optional[str], response_path: Optional[Path]) -> None:
    if not req_id or response_path is None:
        return
    with _external_pending_results_lock:
        _external_pending_results[req_id] = response_path


def _pop_external_response_path(req_id: Optional[str]) -> Optional[Path]:
    if not req_id:
        return None
    with _external_pending_results_lock:
        return _external_pending_results.pop(req_id, None)


def _complete_send_request(
    req_id: Optional[str],
    success: bool,
    error: Optional[str] = None,
    *,
    response_path: Optional[Path] = None,
) -> None:
    external_response_path = response_path or _pop_external_response_path(req_id)
    if req_id:
        _deliver_send_result(req_id, success, error)
    _write_external_send_result(external_response_path, success, error)


def _dequeue_external_send_request() -> Optional[Tuple[Any, ...]]:
    try:
        _ensure_ipc_dirs()
        for path in sorted(_ipc_requests_dir().glob("*.json")):
            response_path: Optional[Path] = None
            req_id: Optional[str] = None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                path.unlink(missing_ok=True)
                if not isinstance(data, dict):
                    continue
                req_id = str(data.get("req_id") or "").strip() or None
                response_path_str = str(data.get("response_path") or "").strip()
                response_path = Path(response_path_str) if response_path_str else None
                return (
                    str(data.get("username") or "").strip(),
                    str(data.get("chat_jid") or "").strip(),
                    str(data.get("text") or ""),
                    str(data.get("voice_path") or "").strip() or None,
                    req_id,
                    str(data.get("document_path") or "").strip() or None,
                    response_path,
                )
            except Exception as e:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
                _write_external_send_result(response_path, False, f"Invalid IPC request: {e}")
    except Exception:
        return None
    return None


def _wait_for_external_send_result(
    response_path: Path,
    *,
    timeout: float,
    voice_path: Optional[str] = None,
    document_path: Optional[str] = None,
) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if response_path.exists():
            try:
                data = json.loads(response_path.read_text(encoding="utf-8"))
            except Exception as e:
                try:
                    response_path.unlink(missing_ok=True)
                except Exception:
                    pass
                return f"WhatsApp could not deliver the message: Invalid IPC result: {e}"
            try:
                response_path.unlink(missing_ok=True)
            except Exception:
                pass
            if bool(data.get("success")):
                if document_path:
                    return "Document sent via WhatsApp."
                if voice_path:
                    return "Voice message sent via WhatsApp."
                return "Message sent via WhatsApp."
            return f"WhatsApp could not deliver the message: {data.get('error', '')}"
        time.sleep(0.1)
    return (
        "No delivery confirmation from the WhatsApp bridge within the time limit. "
        "If the message appeared in WhatsApp, it was delivered; otherwise check Settings → Connections → WhatsApp (bridge running, linked)."
    )


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
    """Convert E.164 or phone string to WhatsApp JID.
    Prefers @lid JID when the number is found in lid_to_e164 config map (LID-migrated accounts).
    Falls back to <digits>@s.whatsapp.net."""
    digits = _normalize_phone(phone or "")
    if not digits or len(digits) < 7 or len(digits) > 15:
        return ""
    # Check lid_to_e164 map: some accounts (privacy-migrated) must be addressed by @lid JID
    try:
        _wc = Config.get("whatsapp_config") or {}
        _lid_map = (_wc.get("lid_to_e164") or {}) if isinstance(_wc, dict) else {}
        for _lid, _e164 in _lid_map.items():
            _e164_digits = "".join(c for c in str(_e164) if c.isdigit())
            if _e164_digits and (_e164_digits.endswith(digits) or digits.endswith(_e164_digits)):
                return _lid  # Use @lid JID for this contact
    except Exception:
        pass
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

    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
    admin_auth_exists = whatsapp_auth_exists(local_admin)

    processed_usernames = set()

    for entry in whitelist:
        if not isinstance(entry, dict):
            continue
        if not entry.get("phone_number"):
            continue
        username = (entry.get("vaf_username") or "admin").strip()
        if username in processed_usernames:
            continue
            
        scope = entry.get("user_scope_id") or get_local_admin_scope_id()
        auth_dir = get_whatsapp_auth_dir(username)
        
        # Check if auth exists for this user OR we can fallback to admin
        if (auth_dir / "creds.json").exists():
            result.append((str(scope), username, auth_dir))
            processed_usernames.add(username)
        elif admin_auth_exists and username.lower() != local_admin:
            # Fallback to local admin auth for this user
            admin_auth_dir = Config.APP_DIR / "users" / local_admin / "whatsapp"
            result.append((str(scope), username, admin_auth_dir))
            processed_usernames.add(username)

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
            try:
                item = _outgoing_queue.get(timeout=1.0)
            except queue.Empty:
                item = _dequeue_external_send_request()
                if item is None:
                    continue
            if item is None:
                break
            req_id = item[4] if len(item) >= 5 else None
            voice_path = item[3] if len(item) >= 4 else None
            document_path = item[5] if len(item) >= 6 else None
            response_path = item[6] if len(item) >= 7 else None
            username, chat_jid, text = item[0], item[1], (item[2] or "")
            _register_external_response_path(req_id, response_path)
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
                    _complete_send_request(
                        req_id,
                        False,
                        "Process not running (bridge may have restarted)",
                        response_path=response_path,
                    )
                try:
                    from vaf.core.log_helper import log_whatsapp_reply
                    log_whatsapp_reply(f"DROPPED process_not_running username={username} jid={chat_jid}")
                except Exception:
                    pass
                continue
            if voice_path:
                try:
                    from pathlib import Path
                    # For voice messages, prefer @lid JID over @s.whatsapp.net when known.
                    # Some WhatsApp accounts (privacy-migrated) only receive voice via @lid;
                    # text works with @s.whatsapp.net but voice does not.
                    voice_jid = chat_jid
                    if not (chat_jid or "").endswith("@lid"):
                        try:
                            from vaf.core.config import Config as _Cfg
                            _wc = _Cfg.get("whatsapp_config") or {}
                            _lid_map = (_wc.get("lid_to_e164") or {}) if isinstance(_wc, dict) else {}
                            # Build reverse map: E.164 digits → @lid JID
                            _jid_digits = "".join(c for c in (chat_jid or "").split("@")[0] if c.isdigit())
                            for _lid, _e164 in _lid_map.items():
                                _e164_digits = "".join(c for c in str(_e164) if c.isdigit())
                                if not (_e164_digits and _jid_digits):
                                    continue
                                if not (_e164_digits.endswith(_jid_digits) or _jid_digits.endswith(_e164_digits)):
                                    continue
                                # Only use genuine LIDs: a real LID has different digits than the phone number.
                                # If lid_digits == phone_digits it was incorrectly stored (phone@lid, not a real LID).
                                _lid_digits = "".join(c for c in (_lid or "").split("@")[0] if c.isdigit())
                                if _lid_digits == _jid_digits:
                                    logger.info("WhatsApp voice: skipping fake LID %s (lid digits == phone digits)", _lid)
                                    continue
                                voice_jid = _lid
                                logger.info("WhatsApp voice: resolved %s → %s via lid_to_e164", chat_jid, voice_jid)
                                try:
                                    from vaf.core.log_helper import log_whatsapp_reply
                                    log_whatsapp_reply(f"VOICE_LID_RESOLVE {chat_jid} → {voice_jid}")
                                except Exception:
                                    pass
                                break
                        except Exception:
                            pass
                    p = Path(voice_path)
                    if p.is_file():
                        size = p.stat().st_size
                        logger.info("WhatsApp sending voice to %s, path=%s, size=%s", voice_jid, p, size)
                        cmd = {"cmd": "send_voice", "to": voice_jid, "path": str(p.resolve())}
                        if req_id:
                            cmd["req_id"] = req_id
                        proc.stdin.write(json.dumps(cmd) + "\n")
                        proc.stdin.flush()
                    else:
                        err = "Voice file not found"
                        logger.warning("WhatsApp voice file not found: %s", voice_path)
                        if req_id:
                            _complete_send_request(req_id, False, err, response_path=response_path)
                except Exception as e:
                    logger.warning("WhatsApp voice send failed for %s: %s", username, e)
                    if req_id:
                        _complete_send_request(req_id, False, str(e), response_path=response_path)
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
                            _complete_send_request(req_id, False, err, response_path=response_path)
                except Exception as e:
                    logger.warning("WhatsApp document send failed for %s: %s", username, e)
                    if req_id:
                        _complete_send_request(req_id, False, str(e), response_path=response_path)
            else:
                # For text messages, also prefer @lid JID when known (same reason as voice).
                # Some LID-migrated WhatsApp accounts only reliably receive messages via @lid JID.
                text_jid = chat_jid
                if not (chat_jid or "").endswith("@lid"):
                    try:
                        from vaf.core.config import Config as _Cfg
                        _wc = _Cfg.get("whatsapp_config") or {}
                        _lid_map = (_wc.get("lid_to_e164") or {}) if isinstance(_wc, dict) else {}
                        _jid_digits = "".join(c for c in (chat_jid or "").split("@")[0] if c.isdigit())
                        for _lid, _e164 in _lid_map.items():
                            _e164_digits = "".join(c for c in str(_e164) if c.isdigit())
                            if not (_e164_digits and _jid_digits):
                                continue
                            if not (_e164_digits.endswith(_jid_digits) or _jid_digits.endswith(_e164_digits)):
                                continue
                            # Only use genuine LIDs (real LID digits ≠ phone digits).
                            _lid_digits = "".join(c for c in (_lid or "").split("@")[0] if c.isdigit())
                            if _lid_digits == _jid_digits:
                                logger.info("WhatsApp text: skipping fake LID %s (lid digits == phone digits)", _lid)
                                continue
                            text_jid = _lid
                            logger.info("WhatsApp text: resolved %s → %s via lid_to_e164", chat_jid, text_jid)
                            try:
                                from vaf.core.log_helper import log_whatsapp_reply
                                log_whatsapp_reply(f"TEXT_LID_RESOLVE {chat_jid} → {text_jid}")
                            except Exception:
                                pass
                            break
                    except Exception:
                        pass
                chunks = chunk_whatsapp_text(text)
                for i, chunk in enumerate(chunks):
                    cmd = {"cmd": "send", "to": text_jid, "text": chunk}
                    if req_id and i == len(chunks) - 1:
                        cmd["req_id"] = req_id
                    try:
                        proc.stdin.write(json.dumps(cmd) + "\n")
                        proc.stdin.flush()
                    except Exception as e:
                        logger.warning("WhatsApp send failed for %s: %s", username, e)
                        if req_id:
                            _complete_send_request(req_id, False, str(e), response_path=response_path)
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
            # Fallback: if TTS returns empty (e.g. unsupported lang like tr), try English so we still send voice
            if not voice_path and (lang or "")[:2].lower() != "en":
                logger.info("WhatsApp TTS: fallback to en after empty/fail for lang=%s", lang)
                voice_path = _synthesize_voice_for_reply(text, "en")
                if voice_path:
                    lang = "en"
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
    use_external_ipc = _outgoing_queue is None
    with _process_lock:
        proc = _processes.get(username)
        if (not proc or proc.poll() is not None) and len(_processes) == 1:
            proc = next(iter(_processes.values()), None)
        if not proc or proc.poll() is not None:
            use_external_ipc = True
        else:
            use_external_ipc = False
    if use_external_ipc:
        state = _read_bridge_state()
        if not state.get("running"):
            return (
                "WhatsApp bridge is not running. Start it in Settings → Connections → WhatsApp (click Start)."
            )
        usernames = [str(u).strip() for u in (state.get("usernames") or []) if str(u).strip()]
        uname = (username or "").strip() or "admin"
        if usernames and uname not in usernames and len(usernames) != 1:
            return (
                "WhatsApp process for this user is not running. "
                "Try: Settings → Connections → WhatsApp → Stop, then Start. "
                "Ensure WhatsApp is linked (QR scanned) and your number is in the whitelist."
            )
        req_id = str(uuid.uuid4())
        response_path = _ipc_results_dir() / f"{req_id}.json"
        request_path = _ipc_requests_dir() / f"{time.time_ns()}_{req_id}.json"
        try:
            _write_json_atomic(
                request_path,
                {
                    "req_id": req_id,
                    "username": uname,
                    "chat_jid": chat_jid,
                    "text": text or "",
                    "voice_path": str(voice_path or ""),
                    "document_path": str(document_path or ""),
                    "response_path": str(response_path),
                },
            )
        except Exception as e:
            return f"Failed to enqueue WhatsApp message for bridge delivery: {e}"
        return _wait_for_external_send_result(
            response_path,
            timeout=timeout,
            voice_path=voice_path,
            document_path=document_path,
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
            if typ in ("message", "chats", "connected", "connection_closed", "lid_mappings", "history_messages"):
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


def _wa_flush(pending_key: str) -> None:
    """Debounce timer fired: flush accumulated messages for this chat to TaskQueue."""
    with _wa_pending_lock:
        rec = _wa_pending.pop(pending_key, None)
    if not rec:
        return
    body = rec.get("body", "").strip()
    if not body:
        return
    session_id = rec["session_id"]
    metadata = rec["metadata"]
    if isinstance(metadata, dict):
        metadata.setdefault("origin_channel", "whatsapp")
        metadata.setdefault("task_class", "interactive")
    username = rec["username"]
    from_jid = rec["from_jid"]
    voice_lang = rec.get("voice_lang")

    # Set voice_reply_pending for the *final* combined message (last voice_lang wins)
    if voice_lang:
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
        parts = rec.get("parts", 1)
        log_whatsapp_inbound(f"DEBOUNCE_FLUSH session={session_id} user={username} parts={parts} body_len={len(body)}")
        log_whatsapp_qr(f"[inbound] DEBOUNCE_FLUSH session={session_id} parts={parts}")
    except Exception:
        pass
    logger.info("WhatsApp debounce flush: %s parts → session %s user %s", rec.get("parts", 1), session_id, username)


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
            _complete_send_request(str(req_id), bool(obj.get("success")), obj.get("error", ""))
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
    elif typ == "history_messages":
        # Chat history from WhatsApp (messaging-history.set); store so inbox has full history
        try:
            from vaf.core.whatsapp_message_store import append_message
            stored = 0
            for m in obj.get("messages") or []:
                chat_id = (m.get("chat_id") or "").strip()
                if not chat_id:
                    continue
                body = (m.get("body") or "").strip() or "<message>"
                direction = "out" if m.get("direction") == "out" else "in"
                ts_val = m.get("ts")
                ts_float = float(ts_val) if ts_val is not None else None
                append_message(
                    username,
                    chat_id,
                    body,
                    direction=direction,
                    message_id=m.get("message_id"),
                    content_type=m.get("content_type") or "text",
                    user_scope_id=user_scope_id,
                    ts=ts_float,
                )
                stored += 1
            if stored:
                try:
                    from vaf.core.log_helper import log_whatsapp_qr
                    log_whatsapp_qr(f"[Python] history_messages: stored {stored} to message store")
                except Exception:
                    pass
        except Exception as e:
            logger.warning("WhatsApp: store history_messages failed: %s", e)
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
        # Resolve unresolved @lid via manual config (lid_to_e164) so known contacts like Baba can still be accepted when Node doesn't send fromE164
        resolved_e164_from_config: Optional[str] = None
        if (from_jid or "").endswith("@lid") and not from_e164:
            try:
                wc = Config.get("whatsapp_config") or {}
                if isinstance(wc, dict):
                    lid_map = wc.get("lid_to_e164") or {}
                    if isinstance(lid_map, dict):
                        resolved_e164_from_config = (lid_map.get(str(from_jid)) or "").strip()
                        if resolved_e164_from_config and not resolved_e164_from_config.startswith("+"):
                            resolved_e164_from_config = "+" + resolved_e164_from_config
            except Exception:
                pass
        # Allow when: JID or fromE164 matches whitelist/FO, or unresolved @lid is manually mapped (lid_to_e164) to an allowed number
        allow_match = bool(allowed_phones) and (
            _allow_from_match(from_jid or "", allowed_phones)
            or (from_e164 and _allow_from_match(from_e164, allowed_phones))
            or (resolved_e164_from_config and _allow_from_match(resolved_e164_from_config, allowed_phones))
        )
        explicit_allow = bool(config_phones) and (
            _allow_from_match(from_jid or "", config_phones)
            or (from_e164 and _allow_from_match(from_e164, config_phones))
            or (resolved_e164_from_config and _allow_from_match(resolved_e164_from_config, config_phones))
        )
        contact_allow = bool(allow_match and not explicit_allow)
        ingress_policy = Config.get("channel_ingress_policy")
        policy_allowed, policy_reason = evaluate_ingress(
            "whatsapp",
            ingress_policy,
            explicit_match=explicit_allow,
            contact_match=contact_allow,
        )
        if (from_jid or "").endswith("@lid") and not from_e164 and not resolved_e164_from_config and not policy_allowed:
            try:
                from vaf.core.log_helper import log_whatsapp_inbound, log_whatsapp_qr
                log_whatsapp_inbound(f"REJECT unresolved @lid from={from_jid} (not in whitelist/contacts; LID is not a phone number)")
                log_whatsapp_qr(f"[inbound] REJECT unresolved @lid from={from_jid} (not in whitelist/contacts)")
            except Exception:
                pass
        if not policy_allowed:
            from_digits = _phone_digits_canonical(from_jid or "") or (_phone_digits_canonical(from_e164 or "") if from_e164 else "")
            try:
                from vaf.core.log_helper import log_whatsapp_inbound, log_whatsapp_qr
                log_whatsapp_inbound(
                    f"REJECT not_paired from={from_jid} allowed_count={len(allowed_phones)} reason={policy_reason}"
                )
                log_whatsapp_qr(
                    f"[inbound] REJECT from={from_jid} from_digits={from_digits or '?'} allowed_count={len(allowed_phones)} reason={policy_reason}"
                )
            except Exception:
                pass
            sender_for_throttle = str(from_jid or from_e164 or "")
            if should_log_unauthorized("whatsapp", sender_for_throttle, ingress_policy):
                logger.warning(
                    "WhatsApp: dropped unauthorized inbound from=%s reason=%s explicit=%s contact=%s",
                    from_jid,
                    policy_reason,
                    explicit_allow,
                    contact_allow,
                )
            # Record activity so dashboard still shows this chat (as Read-only) even when Node chat list omits it after reconnect
            _reject_chat_id = _to_e164_display(_jid_to_e164(from_jid or "")) if _jid_to_e164(from_jid or "") else str(from_jid or "")
            if _reject_chat_id:
                _append_chat_activity(_reject_chat_id, None, "in")
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
        # Use fromE164 when available (resolved @lid from Node); else manual lid_to_e164 mapping; else derive from JID. For unresolved @lid without mapping use JID as chat_id and LID part for session.
        raw = from_e164 or resolved_e164_from_config or _jid_to_e164(from_jid) or ""
        chat_id = _to_e164_display(raw) if raw else str(from_jid or "")
        if not chat_id:
            chat_id = str(from_jid or "")
        if raw:
            resolved_digits = _normalize_phone(raw)
        elif (from_jid or "").endswith("@lid"):
            resolved_digits = (from_jid or "").split("@")[0].strip() or "lid"
        else:
            resolved_digits = ""
        # Persist LID→E.164 so dashboard can show this chat under the contact's phone (Web UI session/history).
        # GUARD: only store genuine LIDs — a real LID has different digits than the phone number.
        # If lid_digits == e164_digits it means Node sent phone@lid (not a real privacy LID) → skip.
        if from_e164 and (from_jid or "").endswith("@lid"):
            try:
                _lid_digits = "".join(c for c in (from_jid or "").split("@")[0] if c.isdigit())
                _e164_digits = "".join(c for c in (from_e164 or "") if c.isdigit())
                _is_genuine_lid = bool(_lid_digits and _e164_digits and _lid_digits != _e164_digits)
                if _is_genuine_lid:
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
            append_message(
                username, chat_id, body, direction="in", sender_jid=from_jid,
                message_id=obj.get("messageId") or obj.get("message_id"),
                content_type="voice" if was_voice else "text",
                user_scope_id=user_scope_id,
            )
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
                in_config = explicit_allow
                from_contact = contact_allow and not in_config
                metadata: Dict[str, Any] = {
                    "user_scope_id": user_scope_id,
                    "username": username,
                    "whatsapp_chat_jid": from_jid,
                }
                if from_contact:
                    metadata["from_contact"] = True
                if voice_lang:
                    metadata["voice_lang"] = voice_lang

                # --- 7-second debounce (like Telegram 5s rule) ---
                # Accumulate follow-up messages for WA_DEBOUNCE_SECONDS, then flush all at once.
                pending_key = f"{username}|{from_jid}"
                with _wa_pending_lock:
                    existing = _wa_pending.get(pending_key)
                    if existing:
                        # Reset timer: cancel old, append text
                        old_timer: Optional[threading.Timer] = existing.get("timer")
                        if old_timer is not None:
                            old_timer.cancel()
                        sep = "\n" if existing["body"] else ""
                        existing["body"] = existing["body"] + sep + body
                        existing["parts"] = existing.get("parts", 1) + 1
                        # Last voice_lang wins (so reply is in the language of the most recent voice msg)
                        if voice_lang:
                            existing["voice_lang"] = voice_lang
                            existing["metadata"]["voice_lang"] = voice_lang
                        action = "DEBOUNCE_RESET"
                        parts = existing["parts"]
                    else:
                        # First message for this chat: create new record
                        _wa_pending[pending_key] = {
                            "session_id": session_id,
                            "username": username,
                            "from_jid": from_jid,
                            "body": body,
                            "parts": 1,
                            "voice_lang": voice_lang,
                            "metadata": metadata,
                            "timer": None,  # set below
                        }
                        existing = _wa_pending[pending_key]
                        action = "DEBOUNCE_START"
                        parts = 1

                    # Start/restart the flush timer (outside lock scope would be cleaner but
                    # we assign back to existing dict which is already in the map, so fine here)
                    new_timer = threading.Timer(WA_DEBOUNCE_SECONDS, _wa_flush, args=[pending_key])
                    new_timer.daemon = True
                    new_timer.start()
                    existing["timer"] = new_timer

                try:
                    from vaf.core.log_helper import log_whatsapp_inbound, log_whatsapp_qr
                    log_whatsapp_inbound(f"{action} session={session_id} user={username} parts={parts} body_len={len(body)}")
                    log_whatsapp_qr(f"[inbound] {action} session={session_id} parts={parts} from={from_jid}")
                except Exception:
                    pass
                logger.info("WhatsApp debounce %s: from=%s parts=%s user=%s (flush in %ss)", action, from_jid, parts, username, WA_DEBOUNCE_SECONDS)
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
    elif typ == "lid_mappings":
        mappings = obj.get("mappings")
        if isinstance(mappings, list):
            with _lid_mappings_lock:
                _lid_mappings[username] = [{"lid": str(m.get("lid", "")), "e164": str(m.get("e164", "")).strip() or ""} for m in mappings if m.get("lid")]
            ev = _lid_mappings_events.get(username)
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


def _chat_sync_loop() -> None:
    """Periodically request full chat list from Node (getChats) so _chat_lists stays in sync. Runs until _bridge_stop."""
    interval = 600  # default 10 min
    try:
        wc = Config.get("whatsapp_config") or {}
        if isinstance(wc, dict) and "chat_sync_interval_sec" in wc:
            interval = int(wc.get("chat_sync_interval_sec") or 0)
        if interval <= 0:
            return
    except Exception:
        pass
    # Give bridge time to start and populate _processes
    for _ in range(6):
        if _bridge_stop.is_set():
            return
        time.sleep(10)
    while not _bridge_stop.is_set():
        with _process_lock:
            usernames = list(_processes.keys())
        for username in usernames:
            if _bridge_stop.is_set():
                return
            _request_chats(username)
        # Sleep in small steps so we can exit quickly when stop is set
        for _ in range(interval):
            if _bridge_stop.is_set():
                return
            time.sleep(1)
    logger.debug("WhatsApp chat sync loop exited")


def _run_bridge() -> None:
    """Main bridge loop: start processes for each user, register callback, wait for stop."""
    global _processes
    users = _get_users_to_run()
    if not users:
        logger.info("WhatsApp bridge: no users with linked auth, exiting")
        _write_bridge_state(False)
        return

    set_whatsapp_reply_callback(_enqueue_reply)
    _write_bridge_state(True)

    for user_scope_id, username, auth_dir in users:
        proc = _run_user_process(username, auth_dir)
        if proc:
            with _process_lock:
                _processes[username] = proc
            _write_bridge_state(True)
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
    _write_bridge_state(False)


def start_bridge() -> bool:
    """Start the WhatsApp bridge. Returns True if started."""
    global _bridge_thread, _sender_thread, _chat_sync_thread, _outgoing_queue, _bridge_stop

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
    _ensure_ipc_dirs()
    _outgoing_queue = queue.Queue()
    _sender_thread = threading.Thread(target=_sender_loop, daemon=True)
    _sender_thread.start()
    _bridge_thread = threading.Thread(target=_run_bridge, daemon=True)
    _bridge_thread.start()
    _write_bridge_state(True)
    # Periodic chat list sync (default every 10 min) so bot has latest chats
    try:
        sync_interval = int((whatsapp_config.get("chat_sync_interval_sec") or 600))
        if sync_interval > 0:
            _chat_sync_thread = threading.Thread(target=_chat_sync_loop, daemon=True)
            _chat_sync_thread.start()
            logger.info("WhatsApp chat sync started (interval=%ds)", sync_interval)
    except Exception:
        pass
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
    _write_bridge_state(False)
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


def _request_sync_chats(username: str) -> Optional[str]:
    """Send syncChats to the Node (triggers Baileys fetchMessageHistory so full chat list is synced). Returns username used, or None."""
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
            proc.stdin.write(json.dumps({"cmd": "syncChats"}) + "\n")
            proc.stdin.flush()
            return target_username
        except Exception as e:
            logger.warning("WhatsApp syncChats failed for %s: %s", username, e)
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


def sync_whatsapp_chats(username: str, wait_timeout: float = 25.0) -> List[Dict[str, Any]]:
    """Request full chat list sync from WhatsApp (Node calls Baileys fetchMessageHistory), then return the updated list. Use when the left-side list is incomplete."""
    if not is_bridge_running():
        with _chat_lists_lock:
            return list(_chat_lists.get(username, []))
    used_username = _request_sync_chats(username)
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


def _request_lid_mappings(username: str) -> Optional[str]:
    """Ask Node for LID→E.164 from Baileys lidMapping. Returns username used for lookup."""
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
            proc.stdin.write(json.dumps({"cmd": "getLidMappings"}) + "\n")
            proc.stdin.flush()
            return target_username
        except Exception as e:
            logger.warning("WhatsApp getLidMappings failed for %s: %s", username, e)
    return None


def get_lid_mappings(username: str, wait_timeout: float = 2.5) -> List[Dict[str, str]]:
    """Return LID→E.164 known by Node (Baileys). Requests from bridge when running. Empty if not available."""
    if not is_bridge_running():
        with _lid_mappings_lock:
            return list(_lid_mappings.get(username, []))
    used_username = _request_lid_mappings(username)
    if not used_username:
        with _lid_mappings_lock:
            return list(_lid_mappings.get(username, []))
    with _lid_mappings_lock:
        if used_username not in _lid_mappings_events:
            _lid_mappings_events[used_username] = threading.Event()
        ev = _lid_mappings_events[used_username]
        ev.clear()
    ev.wait(timeout=wait_timeout)
    with _lid_mappings_lock:
        return list(_lid_mappings.get(used_username, []))


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
