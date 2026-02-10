"""
Persistent store for synced email messages (SQLite).

DB path: per-user (data_dir/users/{username}/email_sync.db) for network users;
  for local admin / single-user: VAF_EMAIL_SYNC_DB env or data_dir/email_sync.db.
Retention: messages older than 90 days are deleted on sync.
answered_at: set when the agent has processed/answered the mail (avoids double handling).
"""
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.config import Config
from vaf.core.platform import Platform

logger = logging.getLogger("vaf.core.email_sync_store")

_DB_NAME = "email_sync.db"
_DEFAULT_RETENTION_DAYS = 90


def _local_admin() -> str:
    return (Config.get("local_admin_username") or "admin").strip().lower()


def _is_per_user_db(username: Optional[str]) -> bool:
    """True if this user gets their own DB file (network user, not local admin)."""
    u = (username or "").strip()
    return bool(u and u.lower() != _local_admin())


def _db_path(username: Optional[str] = None) -> Path:
    """SQLite path. For local admin / single-user: env or data_dir/email_sync.db. For other users: data_dir/users/{username}/email_sync.db."""
    u = (username or "").strip()
    if not u or u.lower() == _local_admin():
        env_path = os.environ.get("VAF_EMAIL_SYNC_DB", "").strip()
        if env_path:
            p = Path(env_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        data_dir = Platform.data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / _DB_NAME
    data_dir = Platform.data_dir()
    user_dir = data_dir / "users" / u
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / _DB_NAME


def _user_for_query(username: Optional[str]) -> str:
    """Value for WHERE username = ? In per-user DB we store with ''; in shared DB we use the actual username."""
    if _is_per_user_db(username):
        return ""
    return (username or "").strip() or ""


def _get_conn(username: Optional[str] = None) -> sqlite3.Connection:
    path = _db_path(username)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def init_store(username: Optional[str] = None) -> None:
    """Create table if not exists. Idempotent. Uses per-user DB when username is a network user."""
    conn = _get_conn(username)
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
                message_date_iso TEXT,
                answered_at TEXT DEFAULT '',
                PRIMARY KEY (username, account_id, folder, message_id)
            )
        """)
        cur = conn.execute("PRAGMA table_info(email_messages)")
        cols = [row[1] for row in cur.fetchall()]
        if "message_date_iso" not in cols:
            try:
                conn.execute("ALTER TABLE email_messages ADD COLUMN message_date_iso TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass
        if "answered_at" not in cols:
            try:
                conn.execute("ALTER TABLE email_messages ADD COLUMN answered_at TEXT DEFAULT ''")
                conn.commit()
            except sqlite3.OperationalError:
                pass
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


def _parse_message_date_iso(date_str: str) -> Optional[str]:
    """Parse RFC-style date_str to ISO for retention. Returns None if unparseable."""
    if not (date_str or "").strip():
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def upsert_messages(
    account_id: str,
    folder: str,
    messages: List[Dict[str, Any]],
    username: Optional[str] = None,
) -> int:
    """
    Insert or update messages for an account/folder. Preserves answered_at on update.
    Each message dict: subject, from, date, message_id, body_snippet.
    username: when set (multi-user), messages are scoped to that user.
    """
    if not messages:
        return 0
    init_store(username)
    user = _user_for_query(username)
    conn = _get_conn(username)
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
            message_date_iso = _parse_message_date_iso(date_str)
            conn.execute(
                """
                INSERT INTO email_messages
                (username, account_id, folder, message_id, category, provider_message_id, subject, from_addr, date_str, body_snippet, synced_at, message_date_iso, answered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                ON CONFLICT(username, account_id, folder, message_id) DO UPDATE SET
                    category = excluded.category,
                    provider_message_id = excluded.provider_message_id,
                    subject = excluded.subject,
                    from_addr = excluded.from_addr,
                    date_str = excluded.date_str,
                    body_snippet = excluded.body_snippet,
                    synced_at = excluded.synced_at,
                    message_date_iso = excluded.message_date_iso
                """,
                (user, account_id, folder or "INBOX", message_id, category, provider_message_id, subject, from_addr, date_str, body_snippet, synced_at, message_date_iso),
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
    init_store(username)
    user = _user_for_query(username)
    cat = (category or "").strip().lower().replace(" ", "_")[:64] if category else None
    conn = _get_conn(username)
    try:
        sel = "account_id, folder, message_id, category, provider_message_id, subject, from_addr, date_str, body_snippet, synced_at, answered_at"
        if account_id and cat:
            cur = conn.execute(
                f"""
                SELECT {sel}
                FROM email_messages
                WHERE username = ? AND account_id = ? AND folder = ? AND category = ?
                ORDER BY synced_at DESC
                LIMIT ? OFFSET ?
                """,
                (user, account_id, folder or "INBOX", cat, limit, offset),
            )
        elif account_id:
            cur = conn.execute(
                f"""
                SELECT {sel}
                FROM email_messages
                WHERE username = ? AND account_id = ? AND folder = ?
                ORDER BY synced_at DESC
                LIMIT ? OFFSET ?
                """,
                (user, account_id, folder or "INBOX", limit, offset),
            )
        elif cat:
            cur = conn.execute(
                f"""
                SELECT {sel}
                FROM email_messages
                WHERE username = ? AND folder = ? AND category = ?
                ORDER BY synced_at DESC
                LIMIT ? OFFSET ?
                """,
                (user, folder or "INBOX", cat, limit, offset),
            )
        else:
            cur = conn.execute(
                f"""
                SELECT {sel}
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
                "answered_at": (r["answered_at"] or "").strip() if "answered_at" in r.keys() else "",
            }
            for r in rows
        ]
    finally:
        conn.close()


def search_messages(
    query: str,
    folder: str = "INBOX",
    limit: int = 20,
    username: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Search synced messages by subject or sender (from_addr). Case-insensitive LIKE.
    username: when set (multi-user), only that user's messages are searched.
    """
    if not (query or "").strip():
        return []
    init_store(username)
    user = _user_for_query(username)
    pattern = f"%{(query or '').strip()}%"
    conn = _get_conn(username)
    try:
        sel = "account_id, folder, message_id, category, provider_message_id, subject, from_addr, date_str, body_snippet, synced_at, answered_at"
        cur = conn.execute(
            f"""
            SELECT {sel}
            FROM email_messages
            WHERE username = ? AND folder = ?
            AND (subject LIKE ? OR from_addr LIKE ?)
            ORDER BY synced_at DESC
            LIMIT ?
            """,
            (user, folder or "INBOX", pattern, pattern, limit),
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
                "answered_at": (r["answered_at"] or "").strip() if "answered_at" in r.keys() else "",
            }
            for r in rows
        ]
    finally:
        conn.close()


def count_messages(account_id: Optional[str] = None, folder: str = "INBOX", username: Optional[str] = None, category: Optional[str] = None) -> int:
    """Return total count for account (or all) and folder. Optional category filter. username scopes to that user when set."""
    init_store(username)
    user = _user_for_query(username)
    cat = (category or "").strip().lower().replace(" ", "_")[:64] if category else None
    conn = _get_conn(username)
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
    init_store(username)
    user = _user_for_query(username)
    cat = (category or "primary").strip().lower().replace(" ", "_")[:64] or "primary"
    conn = _get_conn(username)
    try:
        cur = conn.execute(
            "UPDATE email_messages SET category = ? WHERE username = ? AND account_id = ? AND folder = ? AND message_id = ?",
            (cat, user, account_id, folder or "INBOX", message_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_message_from_addr(
    username: Optional[str],
    account_id: str,
    folder: str,
    message_id: str,
) -> Optional[str]:
    """Return from_addr for one message, or None if not found. Used to add sender rule from UI."""
    init_store(username)
    user = _user_for_query(username)
    conn = _get_conn(username)
    try:
        cur = conn.execute(
            "SELECT from_addr FROM email_messages WHERE username = ? AND account_id = ? AND folder = ? AND message_id = ?",
            (user, account_id, folder or "INBOX", message_id),
        )
        row = cur.fetchone()
        return (row["from_addr"] or "").strip() or None if row else None
    finally:
        conn.close()


def update_message_answered(
    username: Optional[str],
    account_id: str,
    folder: str,
    message_id: str,
    answered_at: Optional[str] = None,
) -> bool:
    """Set answered_at (ISO timestamp) when the agent has processed/answered this mail. Returns True if updated."""
    init_store(username)
    user = _user_for_query(username)
    ts = (answered_at or "").strip() or datetime.now(timezone.utc).isoformat()
    conn = _get_conn(username)
    try:
        cur = conn.execute(
            "UPDATE email_messages SET answered_at = ? WHERE username = ? AND account_id = ? AND folder = ? AND message_id = ?",
            (ts, user, account_id, folder or "INBOX", message_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_messages_older_than(
    username: Optional[str] = None,
    days: int = _DEFAULT_RETENTION_DAYS,
) -> int:
    """Delete messages older than `days` (by message date or synced_at fallback). Returns count deleted."""
    init_store(username)
    user = _user_for_query(username)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = _get_conn(username)
    try:
        cur = conn.execute(
            """
            DELETE FROM email_messages
            WHERE username = ?
            AND (
                (message_date_iso IS NOT NULL AND message_date_iso < ?)
                OR (message_date_iso IS NULL AND synced_at < ?)
            )
            """,
            (user, cutoff, cutoff),
        )
        conn.commit()
        n = cur.rowcount
        if n:
            logger.info("Email retention: deleted %s message(s) older than %s days", n, days)
        return n
    finally:
        conn.close()


def list_for_sender_relabel(username: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all messages for the user with account_id, folder, message_id, from_addr, category for sender-rule backfill."""
    init_store(username)
    user = _user_for_query(username)
    conn = _get_conn(username)
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
    init_store(username)
    user = _user_for_query(username)
    conn = _get_conn(username)
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
