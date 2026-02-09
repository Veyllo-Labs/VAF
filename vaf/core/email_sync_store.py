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
                category TEXT NOT NULL DEFAULT 'primary',
                provider_message_id TEXT DEFAULT '',
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
        if "category" not in cols:
            try:
                conn.execute("ALTER TABLE email_messages ADD COLUMN category TEXT NOT NULL DEFAULT 'primary'")
                conn.commit()
            except sqlite3.OperationalError:
                pass
        if "provider_message_id" not in cols:
            try:
                conn.execute("ALTER TABLE email_messages ADD COLUMN provider_message_id TEXT DEFAULT ''")
                conn.commit()
            except sqlite3.OperationalError:
                pass
        if "username" not in cols:
            conn.execute("ALTER TABLE email_messages RENAME TO email_messages_old")
            conn.execute("""
                CREATE TABLE email_messages (
                    username TEXT NOT NULL DEFAULT '',
                    account_id TEXT NOT NULL,
                    folder TEXT NOT NULL DEFAULT 'INBOX',
                    message_id TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'primary',
                    subject TEXT NOT NULL DEFAULT '',
                    from_addr TEXT NOT NULL DEFAULT '',
                    date_str TEXT NOT NULL DEFAULT '',
                    body_snippet TEXT NOT NULL DEFAULT '',
                    synced_at TEXT NOT NULL,
                    PRIMARY KEY (username, account_id, folder, message_id)
                )
            """)
            conn.execute(
                "INSERT INTO email_messages (username, account_id, folder, message_id, category, subject, from_addr, date_str, body_snippet, synced_at) "
                "SELECT '', account_id, folder, message_id, 'primary', subject, from_addr, date_str, body_snippet, synced_at FROM email_messages_old"
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
            category = (m.get("category") or "primary").strip().lower() or "primary"
            if category not in ("primary", "social", "promotions"):
                category = "primary"
            provider_message_id = (m.get("provider_message_id") or "")[:256]
            subject = (m.get("subject") or "")[:2048]
            from_addr = (m.get("from") or "")[:1024]
            date_str = (m.get("date") or "")[:256]
            body_snippet = (m.get("body_snippet") or "")[:4096]
            conn.execute(
                """
                INSERT OR REPLACE INTO email_messages
                (username, account_id, folder, message_id, category, provider_message_id, subject, from_addr, date_str, body_snippet, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user, account_id, folder or "INBOX", message_id, category, provider_message_id, subject, from_addr, date_str, body_snippet, synced_at),
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
    category: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    List synced messages, newest first. Paginated.
    If account_id is None, returns messages from all accounts (still filtered by folder).
    username: when set (multi-user), only that user's messages are returned.
    category: when set, only that category (primary|social|promotions or custom). Spam is never stored.
    """
    init_store()
    user = (username or "").strip() or ""
    cat = (category or "").strip().lower().replace(" ", "_")[:64] if category else None
    conn = _get_conn()
    try:
        if account_id and cat:
            cur = conn.execute(
                """
                SELECT account_id, folder, message_id, category, provider_message_id, subject, from_addr, date_str, body_snippet, synced_at
                FROM email_messages
                WHERE username = ? AND account_id = ? AND folder = ? AND category = ?
                ORDER BY synced_at DESC
                LIMIT ? OFFSET ?
                """,
                (user, account_id, folder or "INBOX", cat, limit, offset),
            )
        elif account_id:
            cur = conn.execute(
                """
                SELECT account_id, folder, message_id, category, provider_message_id, subject, from_addr, date_str, body_snippet, synced_at
                FROM email_messages
                WHERE username = ? AND account_id = ? AND folder = ?
                ORDER BY synced_at DESC
                LIMIT ? OFFSET ?
                """,
                (user, account_id, folder or "INBOX", limit, offset),
            )
        elif cat:
            cur = conn.execute(
                """
                SELECT account_id, folder, message_id, category, provider_message_id, subject, from_addr, date_str, body_snippet, synced_at
                FROM email_messages
                WHERE username = ? AND folder = ? AND category = ?
                ORDER BY synced_at DESC
                LIMIT ? OFFSET ?
                """,
                (user, folder or "INBOX", cat, limit, offset),
            )
        else:
            cur = conn.execute(
                """
                SELECT account_id, folder, message_id, category, provider_message_id, subject, from_addr, date_str, body_snippet, synced_at
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
                "category": r["category"] if "category" in r.keys() else "primary",
                "provider_message_id": r["provider_message_id"] if "provider_message_id" in r.keys() else "",
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


def count_messages(account_id: Optional[str] = None, folder: str = "INBOX", username: Optional[str] = None, category: Optional[str] = None) -> int:
    """Return total count for account (or all) and folder. Optional category filter. username scopes to that user when set."""
    init_store()
    user = (username or "").strip() or ""
    cat = (category or "").strip().lower().replace(" ", "_")[:64] if category else None
    conn = _get_conn()
    try:
        if account_id and cat:
            cur = conn.execute(
                "SELECT COUNT(*) AS n FROM email_messages WHERE username = ? AND account_id = ? AND folder = ? AND category = ?",
                (user, account_id, folder or "INBOX", cat),
            )
        elif account_id:
            cur = conn.execute(
                "SELECT COUNT(*) AS n FROM email_messages WHERE username = ? AND account_id = ? AND folder = ?",
                (user, account_id, folder or "INBOX"),
            )
        elif cat:
            cur = conn.execute(
                "SELECT COUNT(*) AS n FROM email_messages WHERE username = ? AND folder = ? AND category = ?",
                (user, folder or "INBOX", cat),
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


def update_message_category(
    username: Optional[str],
    account_id: str,
    folder: str,
    message_id: str,
    category: str,
) -> bool:
    """Update one message's category. Returns True if a row was updated. Category is normalized (lowercase, spaces to underscore, max 64 chars)."""
    init_store()
    user = (username or "").strip() or ""
    cat = (category or "primary").strip().lower().replace(" ", "_")[:64] or "primary"
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE email_messages SET category = ? WHERE username = ? AND account_id = ? AND folder = ? AND message_id = ?",
            (cat, user, account_id, folder or "INBOX", message_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_for_sender_relabel(username: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all messages for the user with account_id, folder, message_id, from_addr, category for sender-rule backfill."""
    init_store()
    user = (username or "").strip() or ""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "SELECT account_id, folder, message_id, from_addr, category FROM email_messages WHERE username = ?",
            (user,),
        )
        rows = cur.fetchall()
        return [
            {
                "account_id": r["account_id"],
                "folder": r["folder"],
                "message_id": r["message_id"],
                "from_addr": r["from_addr"],
                "category": r["category"] if r["category"] else "primary",
            }
            for r in rows
        ]
    finally:
        conn.close()


def list_categories(username: Optional[str] = None) -> List[str]:
    """Return distinct category values for this user. Standard (primary, social, promotions) first, then rest alphabetically."""
    init_store()
    user = (username or "").strip() or ""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "SELECT DISTINCT category FROM email_messages WHERE username = ? ORDER BY category",
            (user,),
        )
        rows = cur.fetchall()
        seen = {r["category"] for r in rows if r["category"]}
        standard = ["primary", "social", "promotions"]
        result = [c for c in standard if c in seen]
        for c in sorted(seen):
            if c not in standard:
                result.append(c)
        return result
    finally:
        conn.close()
