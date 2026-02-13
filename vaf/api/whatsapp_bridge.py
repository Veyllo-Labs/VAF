"""
Long-lived WhatsApp bridge: one Node (Baileys) subprocess per user with linked auth.
Receives messages via Node stdout, enqueues tasks, sends replies via stdin to Node.
User isolation: each user's credentials and session are strictly separate.
"""
import json
import logging
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from vaf.core.config import Config
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


def _wa_bridge_path() -> Path:
    """Path to wa-bridge.js."""
    return Path(__file__).resolve().parents[1] / "whatsapp_node" / "wa-bridge.js"


def _node_path() -> Optional[str]:
    """Resolve Node executable."""
    return shutil.which("node")


def _jid_to_e164(jid: str) -> str:
    """Convert WhatsApp JID to E.164-like string for comparison (e.g. 491234567890@s.whatsapp.net -> 491234567890)."""
    if not jid or "@" not in jid:
        return (jid or "").strip()
    return jid.split("@")[0].strip()


def _normalize_phone(phone: str) -> str:
    """Normalize phone to digits only for comparison."""
    return "".join(c for c in (phone or "") if c.isdigit())


def _allow_from_match(sender_jid: str, allowed_phones: List[str]) -> bool:
    """Check if sender JID matches any allowed phone number."""
    sender_digits = _normalize_phone(_jid_to_e164(sender_jid))
    if not sender_digits:
        return False
    for p in allowed_phones:
        if _normalize_phone(p) == sender_digits:
            return True
    return False


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
        scope = entry.get("user_scope_id") or Config.get("local_admin_scope_id", "00000000-0000-0000-0000-000000000001")
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
    """Read (username, chat_jid, text) from queue, write to that user's Node stdin."""
    global _outgoing_queue, _processes
    while True:
        try:
            item = _outgoing_queue.get(timeout=1.0)
            if item is None:
                break
            username, chat_jid, text = item
            if not username or not chat_jid or not text:
                continue
            with _process_lock:
                proc = _processes.get(username)
            if not proc or proc.poll() is not None:
                logger.warning("WhatsApp process for %s not running, dropped reply", username)
                continue
            chunks = chunk_whatsapp_text(text)
            for chunk in chunks:
                cmd = {"cmd": "send", "to": chat_jid, "text": chunk}
                try:
                    proc.stdin.write(json.dumps(cmd) + "\n")
                    proc.stdin.flush()
                except Exception as e:
                    logger.warning("WhatsApp send failed for %s: %s", username, e)
                    break
            try:
                from vaf.core.log_helper import log_whatsapp_reply
                log_whatsapp_reply(f"SENDER ok username={username} jid={chat_jid}")
            except Exception:
                pass
        except queue.Empty:
            continue
        except Exception as e:
            logger.exception("WhatsApp sender error: %s", e)


def _is_jid_whitelisted(username: str, chat_jid: str) -> bool:
    """Verify chat_jid is in whitelist for this user. Nur Whitelist-Nummern dürfen Antworten erhalten."""
    whatsapp_config = Config.get("whatsapp_config") or {}
    if not isinstance(whatsapp_config, dict):
        return False
    allowed_phones: List[str] = []
    uname = (username or "").strip() or "admin"
    for entry in (whatsapp_config.get("whitelist") or []):
        if isinstance(entry, dict) and (entry.get("vaf_username") or "admin").strip() == uname:
            p = entry.get("phone_number")
            if p:
                allowed_phones.append(str(p))
    return _allow_from_match(chat_jid, allowed_phones)


def _enqueue_reply(username: str, chat_jid: str, text: str) -> None:
    """Callback for headless_runner: enqueue reply for this user's Node process. Only sends to whitelisted contacts."""
    if not _is_jid_whitelisted(username, chat_jid):
        logger.warning("WhatsApp: blocked reply to non-whitelisted JID %s for user %s", chat_jid, username)
        return
    try:
        from vaf.core.log_helper import log_whatsapp_reply
        log_whatsapp_reply(f"BRIDGE enqueue username={username} jid={chat_jid} len={len(text)}")
    except Exception:
        pass
    if _outgoing_queue is not None:
        try:
            _outgoing_queue.put((username, chat_jid, text))
        except Exception:
            pass


def _read_user_process(
    username: str,
    user_scope_id: str,
    auth_dir: Path,
    proc: subprocess.Popen,
) -> None:
    """Read JSON lines from process stdout, handle events."""
    allowed_phones: List[str] = []
    whatsapp_config = Config.get("whatsapp_config") or {}
    if isinstance(whatsapp_config, dict):
        for entry in (whatsapp_config.get("whitelist") or []):
            if isinstance(entry, dict) and entry.get("vaf_username") == username:
                p = entry.get("phone_number")
                if p:
                    allowed_phones.append(str(p))
    if not allowed_phones:
        for entry in (whatsapp_config.get("whitelist") or []):
            if isinstance(entry, dict) and str(entry.get("user_scope_id")) == user_scope_id:
                p = entry.get("phone_number")
                if p:
                    allowed_phones.append(str(p))
    if not allowed_phones:
        logger.warning("WhatsApp: no allowFrom for user %s, rejecting inbound", username)

    for line in proc.stdout:
        if _bridge_stop.is_set():
            break
        line = (line or "").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = obj.get("type")
        if typ == "qr":
            logger.info("WhatsApp QR for %s (scan in Settings -> Connections)", username)
        elif typ == "connected":
            logger.info("WhatsApp connected for user %s", username)
        elif typ == "message":
            from_jid = obj.get("from") or obj.get("senderJid")
            body = (obj.get("body") or "").strip()
            if not body:
                continue
            if not _allow_from_match(from_jid or "", allowed_phones):
                logger.debug("WhatsApp: rejected message from %s (not in allowFrom)", from_jid)
                continue
            save_whatsapp_chat_jid(user_scope_id, username, from_jid)
            session_id = f"whatsapp_{username}_{_jid_to_e164(from_jid)}"
            metadata: Dict[str, Any] = {
                "user_scope_id": user_scope_id,
                "username": username,
                "whatsapp_chat_jid": from_jid,
            }
            tq = TaskQueue()
            tq.add(
                session_id=session_id,
                input_text=body,
                source="whatsapp",
                metadata=metadata,
            )
            logger.info("WhatsApp message enqueued from %s for user %s", from_jid, username)
        elif typ == "error":
            logger.warning("WhatsApp error for %s: %s", username, obj.get("message", ""))


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
            t = threading.Thread(
                target=_read_user_process,
                args=(username, user_scope_id, auth_dir, proc),
                daemon=True,
            )
            t.start()

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


def is_bridge_running() -> bool:
    return _bridge_thread is not None and _bridge_thread.is_alive()
