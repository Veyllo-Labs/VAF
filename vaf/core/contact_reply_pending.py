"""
Pending contact replies: store replies that require user approval before sending.
Used when front_office_contact_reply_require_approval is True (from_contact replies).
In-memory only; no persistence. Thread-safe.
"""
import threading
import time
from typing import Any, Dict, Optional

_PENDING: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()
_TTL_SEC = 600  # 10 minutes; entries older than this are treated as expired


def store_pending(
    reply_id: str,
    source: str,
    username: str,
    chat_id_or_jid: str,
    text: str,
    session_id: str,
    contact_name: Optional[str] = None,
) -> None:
    """Store a pending reply. Payload must contain everything needed to send on approve."""
    with _LOCK:
        _PENDING[reply_id] = {
            "reply_id": reply_id,
            "source": source,
            "username": username,
            "chat_id_or_jid": chat_id_or_jid,
            "text": text,
            "session_id": session_id,
            "contact_name": contact_name,
            "ts": time.time(),
        }


def get_and_remove(reply_id: str) -> Optional[Dict[str, Any]]:
    """
    Get pending payload by reply_id and remove it. Returns None if not found or expired.
    Caller uses payload to send via send_telegram_reply or send_whatsapp_reply.
    """
    with _LOCK:
        entry = _PENDING.pop(reply_id, None)
    if not entry:
        return None
    if (time.time() - entry.get("ts", 0)) > _TTL_SEC:
        return None
    # Return only the fields needed to send; drop internal ts
    return {
        "reply_id": entry["reply_id"],
        "source": entry["source"],
        "username": entry["username"],
        "chat_id_or_jid": entry["chat_id_or_jid"],
        "text": entry["text"],
        "session_id": entry["session_id"],
        "contact_name": entry.get("contact_name"),
    }
