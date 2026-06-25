# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Persistent store for WhatsApp messages (SQLite).
Stores incoming and outgoing messages so the agent can search and read chat history.
Similar to email_sync_store for mail_inbox/find_mail/read_mail.

Isolation: Per user and per scope (UUID). When user_scope_id is passed, the DB path is
scopes/<user_scope_id>/whatsapp_messages.db (or data_dir/whatsapp_messages.db for local admin scope).
Otherwise per-username: data_dir/users/<username>/whatsapp_messages.db or data_dir for local admin.
Dashboard and tools pass user_scope_id when available so scope users get their own DB.
"""
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
from vaf.core.platform import Platform

logger = logging.getLogger("vaf.core.whatsapp_message_store")

_DB_NAME = "whatsapp_messages.db"
_DEFAULT_RETENTION_DAYS = 90


def _local_admin() -> str:
    return get_local_admin_username().lower()


def _local_admin_scope_id() -> str:
    return get_local_admin_scope_id()


def _db_path(username: Optional[str] = None, user_scope_id: Optional[str] = None) -> Path:
    data_dir = Platform.data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    if user_scope_id:
        scope_str = str(user_scope_id).strip()
        if scope_str == _local_admin_scope_id():
            return data_dir / _DB_NAME
        scope_dir = data_dir / "scopes" / scope_str
        scope_dir.mkdir(parents=True, exist_ok=True)
        return scope_dir / _DB_NAME
    u = (username or "").strip()
    if not u or u.lower() == _local_admin():
        return data_dir / _DB_NAME
    user_dir = data_dir / "users" / u
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / _DB_NAME


def _get_conn(username: Optional[str] = None, user_scope_id: Optional[str] = None):
    import sqlite3
    path = _db_path(username, user_scope_id)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_store(username: Optional[str] = None, user_scope_id: Optional[str] = None) -> None:
    """Create table if not exists."""
    conn = _get_conn(username, user_scope_id)
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
    user_scope_id: Optional[str] = None,
    ts: Optional[float] = None,
) -> None:
    """Append one message to the store. ts: optional Unix timestamp (e.g. from history sync); default now."""
    import time
    import sqlite3
    init_store(username, user_scope_id)
    conn = _get_conn(username, user_scope_id)
    try:
        ts = float(ts) if ts is not None else time.time()
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
    user_scope_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Search messages by query (matches body, chat_name, sender)."""
    import sqlite3
    init_store(username, user_scope_id)
    conn = _get_conn(username, user_scope_id)
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


def list_chats_from_store(
    username: str,
    limit: int = 500,
    user_scope_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List all chats that have at least one message in the store (for inbox/dashboard merge).
    Returns list of dicts with chat_id, last_ts, message_count; chat_name from latest message if present."""
    import sqlite3
    init_store(username, user_scope_id)
    conn = _get_conn(username, user_scope_id)
    try:
        u = (username or "").strip() or ""
        limit = min(max(limit, 1), 500)
        cur = conn.execute(
            """
            SELECT chat_id, MAX(ts) AS last_ts, COUNT(*) AS message_count
            FROM whatsapp_messages
            WHERE username = ?
            GROUP BY chat_id
            ORDER BY last_ts DESC
            LIMIT ?
            """,
            (u, limit),
        )
        rows = [dict(row) for row in cur.fetchall()]
        # Optionally attach latest chat_name per chat
        for r in rows:
            cid = r.get("chat_id") or ""
            cur2 = conn.execute(
                """
                SELECT chat_name FROM whatsapp_messages
                WHERE username = ? AND chat_id = ? AND chat_name IS NOT NULL AND chat_name != ''
                ORDER BY ts DESC LIMIT 1
                """,
                (u, cid),
            )
            row2 = cur2.fetchone()
            r["chat_name"] = (dict(row2).get("chat_name") or "").strip() if row2 else ""
        return rows
    finally:
        conn.close()


def get_chat_messages(
    username: str,
    chat_id: str,
    limit: int = 50,
    user_scope_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Get messages for a chat, newest first. When chat_id is a @lid, also look up lid_to_e164 so messages stored under the resolved E.164 are found."""
    import sqlite3
    from vaf.core.config import Config
    init_store(username, user_scope_id)
    chat_ids_to_try = [chat_id or ""]
    if (chat_id or "").strip().endswith("@lid"):
        try:
            wc = Config.get("whatsapp_config") or {}
            if isinstance(wc, dict):
                lid_map = wc.get("lid_to_e164") or {}
                if isinstance(lid_map, dict):
                    resolved = (lid_map.get(chat_id.strip()) or "").strip()
                    if resolved and not resolved.startswith("+"):
                        resolved = "+" + resolved
                    if resolved:
                        chat_ids_to_try.append(resolved)
        except Exception:
            pass
    conn = _get_conn(username, user_scope_id)
    try:
        all_rows = []
        seen = set()
        for cid in chat_ids_to_try:
            if not cid or cid in seen:
                continue
            seen.add(cid)
            cur = conn.execute(
                """
                SELECT chat_id, chat_name, body, direction, ts, content_type
                FROM whatsapp_messages
                WHERE username = ? AND chat_id = ?
                ORDER BY ts DESC
                LIMIT ?
                """,
                ((username or "").strip() or "", cid, min(max(limit, 1), 200)),
            )
            all_rows.extend([dict(row) for row in cur.fetchall()])
        all_rows.sort(key=lambda r: -(r.get("ts") or 0))
        return all_rows[: min(max(limit, 1), 200)]
    finally:
        conn.close()
