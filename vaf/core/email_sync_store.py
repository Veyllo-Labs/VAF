"""
Persistent store for synced email messages (SQLite in platform data dir).

Used by the Mail Dashboard to show a local copy of fetched emails.
Sync is triggered manually or by auto-sync; listing is paginated.
"""
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.platform import Platform

logger = logging.getLogger("vaf.core.email_sync_store")

_DB_NAME = "email_sync.db"


def _db_path() -> Path:
    """SQLite DB path in platform data dir (OS-independent)."""
    data_dir = Platform.data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / _DB_NAME


def _get_conn() -> sqlite3.Connection:
    path = _db_path()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def init_store() -> None:
    """Create table if not exists. Idempotent. Migrates old table (no username) to new schema."""
    conn = _get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_messages (
                username TEXT NOT NULL DEFAULT '',
                account_id TEXT NOT NULL,
                folder TEXT NOT NULL DEFAULT 'INBOX',
                message_id TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                from_addr TEXT NOT NULL DEFAULT '',
                date_str TEXT NOT NULL DEFAULT '',
                body_snippet TEXT NOT NULL DEFAULT '',
                synced_at TEXT NOT NULL,
                PRIMARY KEY (username, account_id, folder, message_id)
            )
        """)
        cur = conn.execute("PRAGMA table_info(email_messages)")
        cols = [row[1] for row in cur.fetchall()]
        if "username" not in cols:
            conn.execute("ALTER TABLE email_messages RENAME TO email_messages_old")
            conn.execute("""
                CREATE TABLE email_messages (
                    username TEXT NOT NULL DEFAULT '',
                    account_id TEXT NOT NULL,
                    folder TEXT NOT NULL DEFAULT 'INBOX',
                    message_id TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT '',
                    from_addr TEXT NOT NULL DEFAULT '',
                    date_str TEXT NOT NULL DEFAULT '',
                    body_snippet TEXT NOT NULL DEFAULT '',
                    synced_at TEXT NOT NULL,
                    PRIMARY KEY (username, account_id, folder, message_id)
                )
            """)
            conn.execute(
                "INSERT INTO email_messages (username, account_id, folder, message_id, subject, from_addr, date_str, body_snippet, synced_at) "
                "SELECT '', account_id, folder, message_id, subject, from_addr, date_str, body_snippet, synced_at FROM email_messages_old"
            )
            conn.execute("DROP TABLE email_messages_old")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_messages_user_account_folder ON email_messages(username, account_id, folder)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_messages_synced_at ON email_messages(username, account_id, folder, synced_at DESC)"
        )
        conn.commit()
    finally:
        conn.close()


def upsert_messages(
    account_id: str,
    folder: str,
    messages: List[Dict[str, Any]],
    username: Optional[str] = None,
) -> int:
    """
    Insert or replace messages for an account/folder. Returns count written.
    Each message dict should have: subject, from, date, message_id, body_snippet.
    username: when set (multi-user), messages are scoped to that user.
    """
    if not messages:
        return 0
    init_store()
    user = (username or "").strip() or ""
    conn = _get_conn()
    from datetime import datetime, timezone
    synced_at = datetime.now(timezone.utc).isoformat()
    count = 0
    try:
        for m in messages:
            message_id = (m.get("message_id") or "").strip() or f"local-{account_id}-{count}"
            subject = (m.get("subject") or "")[:2048]
            from_addr = (m.get("from") or "")[:1024]
            date_str = (m.get("date") or "")[:256]
            body_snippet = (m.get("body_snippet") or "")[:4096]
            conn.execute(
                """
                INSERT OR REPLACE INTO email_messages
                (username, account_id, folder, message_id, subject, from_addr, date_str, body_snippet, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user, account_id, folder or "INBOX", message_id, subject, from_addr, date_str, body_snippet, synced_at),
            )
            count += 1
        conn.commit()
        return count
    finally:
        conn.close()


def list_messages(
    account_id: Optional[str] = None,
    folder: str = "INBOX",
    limit: int = 50,
    offset: int = 0,
    username: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    List synced messages, newest first. Paginated.
    If account_id is None, returns messages from all accounts (still filtered by folder).
    username: when set (multi-user), only that user's messages are returned.
    """
    init_store()
    user = (username or "").strip() or ""
    conn = _get_conn()
    try:
        if account_id:
            cur = conn.execute(
                """
                SELECT account_id, folder, message_id, subject, from_addr, date_str, body_snippet, synced_at
                FROM email_messages
                WHERE username = ? AND account_id = ? AND folder = ?
                ORDER BY synced_at DESC
                LIMIT ? OFFSET ?
                """,
                (user, account_id, folder or "INBOX", limit, offset),
            )
        else:
            cur = conn.execute(
                """
                SELECT account_id, folder, message_id, subject, from_addr, date_str, body_snippet, synced_at
                FROM email_messages
                WHERE username = ? AND folder = ?
                ORDER BY synced_at DESC
                LIMIT ? OFFSET ?
                """,
                (user, folder or "INBOX", limit, offset),
            )
        rows = cur.fetchall()
        return [
            {
                "account_id": r["account_id"],
                "folder": r["folder"],
                "message_id": r["message_id"],
                "subject": r["subject"],
                "from": r["from_addr"],
                "date": r["date_str"],
                "body_snippet": r["body_snippet"],
                "synced_at": r["synced_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def count_messages(account_id: Optional[str] = None, folder: str = "INBOX", username: Optional[str] = None) -> int:
    """Return total count for account (or all) and folder. username scopes to that user when set."""
    init_store()
    user = (username or "").strip() or ""
    conn = _get_conn()
    try:
        if account_id:
            cur = conn.execute(
                "SELECT COUNT(*) AS n FROM email_messages WHERE username = ? AND account_id = ? AND folder = ?",
                (user, account_id, folder or "INBOX"),
            )
        else:
            cur = conn.execute(
                "SELECT COUNT(*) AS n FROM email_messages WHERE username = ? AND folder = ?",
                (user, folder or "INBOX"),
            )
        row = cur.fetchone()
        return row["n"] if row else 0
    finally:
        conn.close()
