"""
Persistent store for WhatsApp messages (SQLite).
Stores incoming and outgoing messages so the agent can search and read chat history.
Similar to email_sync_store for mail_inbox/find_mail/read_mail.
"""
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.config import Config
from vaf.core.platform import Platform

logger = logging.getLogger("vaf.core.whatsapp_message_store")

_DB_NAME = "whatsapp_messages.db"
_DEFAULT_RETENTION_DAYS = 90


def _local_admin() -> str:
    return (Config.get("local_admin_username") or "admin").strip().lower()


def _db_path(username: Optional[str] = None) -> Path:
    u = (username or "").strip()
    data_dir = Platform.data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    if not u or u.lower() == _local_admin():
        return data_dir / _DB_NAME
    user_dir = data_dir / "users" / u
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / _DB_NAME


def _get_conn(username: Optional[str] = None):
    import sqlite3
    path = _db_path(username)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_store(username: Optional[str] = None) -> None:
    """Create table if not exists."""
    conn = _get_conn(username)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS whatsapp_messages (
                username TEXT NOT NULL DEFAULT '',
                chat_id TEXT NOT NULL,
                chat_name TEXT,
                sender_jid TEXT,
                body TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL DEFAULT 'in',
                ts REAL NOT NULL,
                message_id TEXT,
                content_type TEXT DEFAULT 'text',
                PRIMARY KEY (username, chat_id, message_id, direction)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_msg_chat ON whatsapp_messages(username, chat_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_msg_ts ON whatsapp_messages(ts)")
        conn.commit()
    finally:
        conn.close()


def append_message(
    username: str,
    chat_id: str,
    body: str,
    direction: str = "in",
    chat_name: Optional[str] = None,
    sender_jid: Optional[str] = None,
    message_id: Optional[str] = None,
    content_type: str = "text",
) -> None:
    """Append one message to the store."""
    import time
    import sqlite3
    init_store(username)
    conn = _get_conn(username)
    try:
        ts = time.time()
        # Use chat_id+ts+direction as fallback unique key when message_id missing
        mid = message_id or f"_{ts}_{direction}"
        conn.execute(
            """
            INSERT OR REPLACE INTO whatsapp_messages
            (username, chat_id, chat_name, sender_jid, body, direction, ts, message_id, content_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (username or "").strip() or "",
                chat_id or "",
                chat_name or "",
                sender_jid or "",
                body or "",
                direction or "in",
                ts,
                mid,
                content_type or "text",
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()


def search_messages(
    username: str,
    query: str,
    chat_id: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Search messages by query (matches body, chat_name, sender)."""
    import sqlite3
    init_store(username)
    conn = _get_conn(username)
    try:
        u = (username or "").strip() or ""
        q = f"%{(query or '').strip()}%"
        if not q or q == "%%":
            return []
        params = [u, q, q, q]
        sql = """
            SELECT chat_id, chat_name, body, direction, ts, content_type
            FROM whatsapp_messages
            WHERE username = ? AND (body LIKE ? OR chat_name LIKE ? OR sender_jid LIKE ?)
        """
        if chat_id:
            sql += " AND chat_id = ?"
            params.append(chat_id)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(min(max(limit, 1), 100))
        cur = conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_chat_messages(
    username: str,
    chat_id: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Get messages for a chat, newest first."""
    import sqlite3
    init_store(username)
    conn = _get_conn(username)
    try:
        cur = conn.execute(
            """
            SELECT chat_id, chat_name, body, direction, ts, content_type
            FROM whatsapp_messages
            WHERE username = ? AND chat_id = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            ((username or "").strip() or "", chat_id or "", min(max(limit, 1), 200)),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()
