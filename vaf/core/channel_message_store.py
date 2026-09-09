# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Persistent, channel-generic store for messaging-channel messages (SQLite).

Stores incoming and outgoing messages across channels (WhatsApp/Telegram/Discord) so the agent can
search and read chat history. Each row carries a `channel` column; queries filter by it.
(Formerly `whatsapp_message_store`; renamed because it is channel-generic. A back-compat shim at
`vaf.core.whatsapp_message_store` re-exports this module for one release.)

Isolation: per user and per scope (UUID). When user_scope_id is passed, the DB path is
scopes/<user_scope_id>/channel_messages.db (or data_dir/channel_messages.db for the local-admin scope).
Otherwise per-username: data_dir/users/<username>/channel_messages.db or data_dir for the local admin.

Next to the messages sits `chat_marks`, the person's own state per chat: when they last
opened it (`seen_ts`), when they marked it done (`done_ts`), and when the agent asked them a
question about it (`owner_asked_ts`). It is keyed on (username, channel, chat_id) because the
messages' primary key predates the channel column. `chat_overview` is the one grouped read
every conversation list is built from (the inbox, the channel windows, the agent's tool), and
every writer announces `inbox_changed` so an open window refetches instead of polling.
"""
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
from vaf.core.platform import Platform

logger = logging.getLogger("vaf.core.channel_message_store")

_DB_NAME = "channel_messages.db"
_OLD_DB_NAME = "whatsapp_messages.db"   # legacy file name, migrated on first init
_DEFAULT_RETENTION_DAYS = 90

__all__ = [
    "init_store", "append_message", "search_messages",
    "list_chats_from_store", "chat_overview", "get_chat_messages", "last_message_ts", "oldest_message",
    "delete_message", "mark_deleted", "replace_chat_rows",
    "chat_marks", "mark_seen", "mark_done", "mark_owner_asked",
]

#: Writers announce `inbox_changed` at most this often per scope; a history sync appends
#: hundreds of rows in a burst, and the browser only needs to be told once that the list
#: changed, plus once more at the end of the burst.
_ANNOUNCE_MIN_INTERVAL_S = 2.0
_announce_lock = threading.Lock()
_announce_last: Dict[str, float] = {}
_announce_timers: Dict[str, threading.Timer] = {}


#: The `sender_jid` an outbound row carries when the PERSON sent it from the dashboard
#: (their own words, from their agent's number) rather than the agent. The reply window
#: reads outbound rows to answer "did the agent write to this number", and a row with this
#: label must not open it: nobody asked the agent to talk to that person.
OWNER_SENDER = "owner"


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


def _migrate_legacy_db(new_path: Path) -> None:
    """One-time, non-destructive migration of the legacy whatsapp_messages.db into the new
    channel_messages.db, BEFORE the new file is first opened. WAL-checkpoint the old file, then
    copy it byte-for-byte (rows preserved by construction). The old file is left as a backup."""
    import sqlite3
    import shutil
    old_path = new_path.parent / _OLD_DB_NAME
    if new_path.exists() or not old_path.exists():
        return
    try:
        c = sqlite3.connect(str(old_path), timeout=30.0)
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            c.close()
        shutil.copy2(str(old_path), str(new_path))
        logger.info("Migrated legacy %s -> %s", old_path.name, new_path.name)
    except Exception as e:
        logger.warning("Legacy message store migration failed (%s); starting fresh", e)


def init_store(username: Optional[str] = None, user_scope_id: Optional[str] = None) -> None:
    """Create the channel_messages table if absent; migrate the legacy whatsapp_messages db/table."""
    import sqlite3
    new_path = _db_path(username, user_scope_id)
    _migrate_legacy_db(new_path)
    conn = _get_conn(username, user_scope_id)
    try:
        # Rename the legacy table if the migrated/old db still uses the old name.
        try:
            conn.execute("ALTER TABLE whatsapp_messages RENAME TO channel_messages")
        except sqlite3.OperationalError:
            pass  # already renamed, or fresh install (no legacy table)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_messages (
                username TEXT NOT NULL DEFAULT '',
                chat_id TEXT NOT NULL,
                chat_name TEXT,
                sender_jid TEXT,
                body TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL DEFAULT 'in',
                ts REAL NOT NULL,
                message_id TEXT,
                content_type TEXT DEFAULT 'text',
                channel TEXT NOT NULL DEFAULT 'whatsapp',
                PRIMARY KEY (username, chat_id, message_id, direction)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ch_msg_chat ON channel_messages(username, chat_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ch_msg_ts ON channel_messages(ts)")
        # Older dbs (pre-channel-column) carried no `channel`; add it idempotently.
        try:
            conn.execute("ALTER TABLE channel_messages ADD COLUMN channel TEXT NOT NULL DEFAULT 'whatsapp'")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ch_msg_channel ON channel_messages(username, channel, chat_id)")
        # The person's own state per chat. Created once; on that first creation every chat
        # that already holds messages starts as READ up to its newest row: the day the
        # marker arrives counts as read, or every history row would light up and no click
        # could ever clear it (the rule the Logs seen marker follows).
        had_marks = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_marks'").fetchone() is not None
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_marks (
                username TEXT NOT NULL DEFAULT '',
                channel TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                seen_ts REAL,
                done_ts REAL,
                owner_asked_ts REAL,
                PRIMARY KEY (username, channel, chat_id)
            )
        """)
        if not had_marks:
            conn.execute("""
                INSERT OR IGNORE INTO chat_marks (username, channel, chat_id, seen_ts)
                SELECT username, channel, chat_id, MAX(ts) FROM channel_messages
                GROUP BY username, channel, chat_id
            """)
        conn.commit()
    finally:
        conn.close()


def _reset_announce_state() -> None:
    """Cancel every pending trailing announcement and forget the throttle stamps. A test
    seam: a timer a test left behind would otherwise fire into the next test's patched
    signal sink (the interval is seconds, a test suite runs faster than that)."""
    with _announce_lock:
        timers = list(_announce_timers.values())
        _announce_timers.clear()
        _announce_last.clear()
    for t in timers:
        try:
            t.cancel()
        except Exception:
            pass


def _announce_changed(username: Optional[str], user_scope_id: Optional[str]) -> None:
    """Tell the person's browsers that a conversation list changed (`inbox_changed`).

    The scope is resolved the way `_db_path` resolves the file: an explicit scope, else the
    local admin when the rows live in the admin's file (Discord writes under `admin` with no
    scope), else nobody. Throttled per scope with a trailing edge: the first write of a burst
    announces at once, the rest collapse into one announcement when the interval ends, so the
    browser's refetch after the first frame cannot miss what the burst appended after it.
    Never raises: the store must not fail a write because no browser is listening."""
    try:
        scope = str(user_scope_id).strip() if user_scope_id else ""
        if not scope:
            u = (username or "").strip()
            if not u or u.lower() == _local_admin():
                scope = _local_admin_scope_id() or ""
        if not scope:
            return
        now = time.monotonic()
        with _announce_lock:
            last = _announce_last.get(scope, 0.0)
            if now - last >= _ANNOUNCE_MIN_INTERVAL_S:
                _announce_last[scope] = now
                fire_now = True
            else:
                fire_now = False
                if scope not in _announce_timers:
                    delay = _ANNOUNCE_MIN_INTERVAL_S - (now - last)
                    timer = threading.Timer(max(0.01, delay), _announce_trailing, args=(scope,))
                    timer.daemon = True
                    _announce_timers[scope] = timer
                    timer.start()
        if fire_now:
            _emit_inbox_changed(scope)
    except Exception:
        pass


def _announce_trailing(scope: str) -> None:
    with _announce_lock:
        _announce_timers.pop(scope, None)
        _announce_last[scope] = time.monotonic()
    _emit_inbox_changed(scope)


def _emit_inbox_changed(scope: str) -> None:
    try:
        from vaf.core.web_interface import notify_inbox_changed
        notify_inbox_changed(scope)
    except Exception:
        pass


def _upsert_mark(username: Optional[str], channel: str, chat_id: str, user_scope_id: Optional[str],
                 **columns: Optional[float]) -> None:
    init_store(username, user_scope_id)
    conn = _get_conn(username, user_scope_id)
    try:
        u = (username or "").strip() or ""
        conn.execute(
            "INSERT OR IGNORE INTO chat_marks (username, channel, chat_id) VALUES (?, ?, ?)",
            (u, channel or "whatsapp", chat_id or ""),
        )
        sets = ", ".join(f"{col} = ?" for col in columns)
        conn.execute(
            f"UPDATE chat_marks SET {sets} WHERE username = ? AND channel = ? AND chat_id = ?",
            (*columns.values(), u, channel or "whatsapp", chat_id or ""),
        )
        conn.commit()
    finally:
        conn.close()
    _announce_changed(username, user_scope_id)


def mark_seen(username: str, channel: str, chat_id: str, user_scope_id: Optional[str] = None,
              ts: Optional[float] = None) -> float:
    """The person opened this chat: everything up to `ts` (default now) counts as read.
    Never moves backwards. Returns the marker written."""
    at = float(ts) if ts is not None else time.time()
    init_store(username, user_scope_id)
    conn = _get_conn(username, user_scope_id)
    try:
        row = conn.execute(
            "SELECT seen_ts FROM chat_marks WHERE username = ? AND channel = ? AND chat_id = ?",
            ((username or "").strip() or "", channel or "whatsapp", chat_id or ""),
        ).fetchone()
        if row and row["seen_ts"] is not None and float(row["seen_ts"]) >= at:
            return float(row["seen_ts"])
    finally:
        conn.close()
    _upsert_mark(username, channel, chat_id, user_scope_id, seen_ts=at)
    return at


def mark_done(username: str, channel: str, chat_id: str, user_scope_id: Optional[str] = None,
              done: bool = True, ts: Optional[float] = None) -> None:
    """Mark a chat done (nothing waits until something newer arrives) or take that back."""
    _upsert_mark(username, channel, chat_id, user_scope_id,
                 done_ts=(float(ts) if ts is not None else time.time()) if done else None)


def mark_owner_asked(username: str, channel: str, chat_id: str, user_scope_id: Optional[str] = None,
                     ts: Optional[float] = None) -> None:
    """The agent asked the person a question about this chat (the Front Office back-channel):
    the chat waits for the person until they open it or answer, or the agent writes to the
    contact again."""
    _upsert_mark(username, channel, chat_id, user_scope_id,
                 owner_asked_ts=float(ts) if ts is not None else time.time())


def chat_marks(username: str, user_scope_id: Optional[str] = None,
               channel: Optional[str] = None) -> Dict[Tuple[str, str], Dict[str, Optional[float]]]:
    """Every mark of this identity, keyed by (channel, chat_id). A missing store answers {}."""
    if not store_exists(username, user_scope_id):
        return {}
    init_store(username, user_scope_id)
    conn = _get_conn(username, user_scope_id)
    try:
        clauses, params = ["username = ?"], [(username or "").strip() or ""]
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        cur = conn.execute(
            f"SELECT channel, chat_id, seen_ts, done_ts, owner_asked_ts FROM chat_marks WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        out: Dict[Tuple[str, str], Dict[str, Optional[float]]] = {}
        for row in cur.fetchall():
            d = dict(row)
            out[(d["channel"], d["chat_id"])] = {
                "seen_ts": d.get("seen_ts"), "done_ts": d.get("done_ts"), "owner_asked_ts": d.get("owner_asked_ts"),
            }
        return out
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
    channel: str = "whatsapp",
) -> None:
    """Append one message to the store. ts: optional Unix timestamp (e.g. from history sync); default now.
    channel: messaging channel ('whatsapp' default, 'telegram', 'discord', ...)."""
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
            INSERT OR REPLACE INTO channel_messages
            (username, chat_id, chat_name, sender_jid, body, direction, ts, message_id, content_type, channel)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                channel or "whatsapp",
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()
    _announce_changed(username, user_scope_id)


def delete_message(
    username: str,
    chat_id: str,
    message_id: str,
    direction: str = "in",
    user_scope_id: Optional[str] = None,
    channel: str = "whatsapp",
) -> int:
    """Hard-delete one message by its primary key (username, chat_id, message_id, direction).
    Returns the number of rows removed (0 if no match). message_id must be the real id stored on
    the row. Used by reconcile paths and channels that deliver real delete events (e.g. Discord)."""
    init_store(username, user_scope_id)
    conn = _get_conn(username, user_scope_id)
    try:
        cur = conn.execute(
            """
            DELETE FROM channel_messages
            WHERE username = ? AND chat_id = ? AND message_id = ? AND direction = ? AND channel = ?
            """,
            (
                (username or "").strip() or "",
                chat_id or "",
                str(message_id or ""),
                direction or "in",
                channel or "whatsapp",
            ),
        )
        conn.commit()
        removed = cur.rowcount or 0
    finally:
        conn.close()
    if removed:
        _announce_changed(username, user_scope_id)
    return removed


def mark_deleted(
    username: str,
    chat_id: str,
    message_id: str,
    direction: str = "in",
    user_scope_id: Optional[str] = None,
    channel: str = "whatsapp",
) -> int:
    """Tombstone a message in place (content_type='deleted', body='') without dropping the row.
    Use when a delete event carries no content (e.g. Discord on_message_delete) but the row should
    stay visible as a placeholder in chat history. Returns rows affected (0 if no match)."""
    init_store(username, user_scope_id)
    conn = _get_conn(username, user_scope_id)
    try:
        cur = conn.execute(
            """
            UPDATE channel_messages
            SET content_type = 'deleted', body = ''
            WHERE username = ? AND chat_id = ? AND message_id = ? AND direction = ? AND channel = ?
            """,
            (
                (username or "").strip() or "",
                chat_id or "",
                str(message_id or ""),
                direction or "in",
                channel or "whatsapp",
            ),
        )
        conn.commit()
        changed = cur.rowcount or 0
    finally:
        conn.close()
    if changed:
        _announce_changed(username, user_scope_id)
    return changed


def replace_chat_rows(
    username: str,
    chat_id: str,
    channel: str,
    rows: List[Dict[str, Any]],
    user_scope_id: Optional[str] = None,
) -> int:
    """Atomically make the store mirror `rows` for one (username, chat_id, channel): in a single
    transaction, delete every existing row for that chat+channel, then bulk-insert `rows`. This is
    the derived-index re-sync primitive - the store becomes an exact projection of the authoritative
    session, so stale rows vanish and re-syncs never accumulate duplicates.

    Each row is a dict with keys: body, direction ('in'/'out'), ts (Unix float), message_id,
    content_type, chat_name, sender_jid (missing keys fall back to defaults). Rows without a
    message_id get a stable per-index synthetic key so the bulk insert never collides on the PK.
    Returns the number of rows inserted."""
    import time
    init_store(username, user_scope_id)
    conn = _get_conn(username, user_scope_id)
    try:
        u = (username or "").strip() or ""
        cid = chat_id or ""
        ch = channel or "whatsapp"
        conn.execute(
            "DELETE FROM channel_messages WHERE username = ? AND chat_id = ? AND channel = ?",
            (u, cid, ch),
        )
        inserted = 0
        for idx, r in enumerate(rows or []):
            direction = (r.get("direction") or "in")
            ts = r.get("ts")
            ts = float(ts) if ts is not None else time.time()
            mid = r.get("message_id")
            mid = str(mid) if mid else f"_sync_{idx}_{direction}"
            conn.execute(
                """
                INSERT OR REPLACE INTO channel_messages
                (username, chat_id, chat_name, sender_jid, body, direction, ts, message_id, content_type, channel)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    u,
                    cid,
                    (r.get("chat_name") or ""),
                    (r.get("sender_jid") or ""),
                    (r.get("body") or ""),
                    direction,
                    ts,
                    mid,
                    (r.get("content_type") or "text"),
                    ch,
                ),
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()
    _announce_changed(username, user_scope_id)
    return inserted


def search_messages(
    username: str,
    query: str,
    chat_id: Optional[str] = None,
    limit: int = 20,
    user_scope_id: Optional[str] = None,
    channel: Optional[str] = "whatsapp",
) -> List[Dict[str, Any]]:
    """Search messages by query (matches body, chat_name, sender). channel: filter to one channel
    ('whatsapp' default, 'telegram', ...); pass None/'' to search across all channels."""
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
            SELECT chat_id, chat_name, body, direction, ts, content_type, channel
            FROM channel_messages
            WHERE username = ? AND (body LIKE ? OR chat_name LIKE ? OR sender_jid LIKE ?)
        """
        if channel:
            sql += " AND channel = ?"
            params.append(channel)
        if chat_id:
            sql += " AND chat_id = ?"
            params.append(chat_id)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(min(max(limit, 1), 100))
        cur = conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def chat_overview(
    username: str,
    user_scope_id: Optional[str] = None,
    channel: Optional[str] = None,
    limit: int = 500,
    reply_window_seconds: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """One row per chat, newest first, in ONE statement: everything a conversation list
    needs to say "who, what, when, unread, waiting" without a query per chat.

    Per (channel, chat_id): `last_ts`, `message_count` (tombstones excluded, as `chat_stats`
    counts), `last_in_ts` (newest inbound), `last_agent_ts` (newest outbound the AGENT sent),
    `last_owner_ts` (newest outbound the person sent from the dashboard, `OWNER_SENDER`),
    `unread` (inbound rows after the person's `seen_ts`), the marks (`seen_ts`, `done_ts`,
    `owner_asked_ts`), the newest row's `chat_name` (newest non-empty), `last_body` (160
    chars), `last_direction`, `last_sender`, `last_content_type`, and, when
    `reply_window_seconds` is given, `last_in_within_ts`: the newest inbound that arrived
    inside the window the agent's last message opened (the reply-window rule's second
    input, `whatsapp_bridge.conversation_open_until`). `channel=None` lists every channel.
    Correlated subqueries instead of window functions: the store must run on the SQLite
    every install ships, and `idx_ch_msg_channel` serves each of them."""
    if not store_exists(username, user_scope_id):
        return []
    init_store(username, user_scope_id)
    conn = _get_conn(username, user_scope_id)
    try:
        u = (username or "").strip() or ""
        limit = min(max(int(limit or 1), 1), 500)
        chan_clause = " AND m.channel = ?" if channel else ""
        chan_param: List[Any] = [channel] if channel else []
        same_chat = "n.username = m.username AND n.channel = m.channel AND n.chat_id = m.chat_id"
        live = "COALESCE(n.content_type, 'text') != 'deleted'"
        newest = (f"(SELECT n.{{col}} FROM channel_messages n WHERE {same_chat} AND {live} "
                  f"ORDER BY n.ts DESC LIMIT 1)")
        within = "NULL"
        window_param: List[Any] = []
        if reply_window_seconds is not None and float(reply_window_seconds) > 0:
            within = (f"(SELECT MAX(n.ts) FROM channel_messages n WHERE {same_chat} AND n.direction = 'in' AND {live} "
                      f"AND n.ts <= (SELECT MAX(o.ts) FROM channel_messages o WHERE o.username = m.username "
                      f"AND o.channel = m.channel AND o.chat_id = m.chat_id AND o.direction = 'out' "
                      f"AND COALESCE(o.sender_jid, '') != ? AND COALESCE(o.content_type, 'text') != 'deleted') + ?)")
            window_param = [OWNER_SENDER, float(reply_window_seconds)]
        cur = conn.execute(
            f"""
            SELECT m.channel, m.chat_id,
                   MAX(m.ts) AS last_ts,
                   COUNT(*) AS message_count,
                   MAX(CASE WHEN m.direction = 'in' THEN m.ts END) AS last_in_ts,
                   MAX(CASE WHEN m.direction = 'out' AND COALESCE(m.sender_jid, '') != ? THEN m.ts END) AS last_agent_ts,
                   MAX(CASE WHEN m.direction = 'out' AND COALESCE(m.sender_jid, '') = ? THEN m.ts END) AS last_owner_ts,
                   SUM(CASE WHEN m.direction = 'in' AND m.ts > COALESCE(k.seen_ts, 0) THEN 1 ELSE 0 END) AS unread,
                   k.seen_ts AS seen_ts, k.done_ts AS done_ts, k.owner_asked_ts AS owner_asked_ts,
                   (SELECT n.chat_name FROM channel_messages n WHERE {same_chat} AND n.chat_name IS NOT NULL
                    AND n.chat_name != '' ORDER BY n.ts DESC LIMIT 1) AS chat_name,
                   {newest.format(col='body')} AS last_body,
                   {newest.format(col='direction')} AS last_direction,
                   {newest.format(col='sender_jid')} AS last_sender,
                   {newest.format(col='content_type')} AS last_content_type,
                   {within} AS last_in_within_ts
            FROM channel_messages m
            LEFT JOIN chat_marks k ON k.username = m.username AND k.channel = m.channel AND k.chat_id = m.chat_id
            WHERE m.username = ?{chan_clause} AND COALESCE(m.content_type, 'text') != 'deleted'
            GROUP BY m.channel, m.chat_id
            ORDER BY last_ts DESC
            LIMIT ?
            """,
            (OWNER_SENDER, OWNER_SENDER, *window_param, u, *chan_param, limit),
        )
        rows = []
        for row in cur.fetchall():
            d = dict(row)
            d["chat_name"] = (d.get("chat_name") or "").strip()
            d["last_body"] = (d.get("last_body") or "").strip()[:160]
            d["last_direction"] = d.get("last_direction") or ""
            d["last_sender"] = d.get("last_sender") or ""
            d["last_content_type"] = d.get("last_content_type") or "text"
            d["message_count"] = int(d.get("message_count") or 0)
            d["unread"] = int(d.get("unread") or 0)
            rows.append(d)
        return rows
    finally:
        conn.close()


def list_chats_from_store(
    username: str,
    limit: int = 500,
    user_scope_id: Optional[str] = None,
    channel: Optional[str] = "whatsapp",
) -> List[Dict[str, Any]]:
    """List all chats that have at least one message in the store (for inbox/dashboard merge).
    Returns list of dicts with chat_id, last_ts, message_count, chat_name (newest non-empty),
    last_body and last_direction: a projection of `chat_overview`, which is the one grouped
    read. channel: filter to one channel ('whatsapp' default, 'telegram', ...); None/'' = all."""
    init_store(username, user_scope_id)
    keys = ("chat_id", "last_ts", "message_count", "chat_name", "last_body", "last_direction")
    return [{k: row[k] for k in keys}
            for row in chat_overview(username, user_scope_id=user_scope_id, channel=channel or None, limit=limit)]


def store_exists(username: Optional[str] = None, user_scope_id: Optional[str] = None) -> bool:
    """Whether this identity's message store file exists. Every reader here runs init_store
    and thereby creates the file; a glance that only wants to know "anything stored for this
    person?" asks this first so a read never materialises an empty database."""
    return _db_path(username, user_scope_id).exists()


def get_chat_messages(
    username: str,
    chat_id: str,
    limit: int = 50,
    user_scope_id: Optional[str] = None,
    channel: Optional[str] = "whatsapp",
    before_ts: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Get messages for a chat, newest first. When chat_id is a @lid, also look up lid_to_e164 so messages stored under the resolved E.164 are found.
    Each row carries its message_id (the channel's own id, or the store's `_<ts>_<dir>` fallback key),
    so a reader that pages by time can tell two messages of the same second apart.
    channel: filter to one channel ('whatsapp' default, 'telegram', ...); None/'' = all channels.
    before_ts: only rows at or before this unix time (inclusive), the cursor a paged reader
    passes so page two starts where page one ended instead of at the newest row again."""
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
            chan_clause = " AND channel = ?" if channel else ""
            chan_param = [channel] if channel else []
            ts_clause = " AND ts <= ?" if before_ts is not None else ""
            ts_param = [float(before_ts)] if before_ts is not None else []
            cur = conn.execute(
                f"""
                SELECT chat_id, chat_name, body, direction, ts, content_type, channel, message_id, sender_jid
                FROM channel_messages
                WHERE username = ? AND chat_id = ?{chan_clause}{ts_clause}
                ORDER BY ts DESC
                LIMIT ?
                """,
                ((username or "").strip() or "", cid, *chan_param, *ts_param, min(max(limit, 1), 200)),
            )
            all_rows.extend([dict(row) for row in cur.fetchall()])
        all_rows.sort(key=lambda r: -(r.get("ts") or 0))
        return all_rows[: min(max(limit, 1), 200)]
    finally:
        conn.close()


def oldest_message(
    username: str,
    chat_id: str,
    user_scope_id: Optional[str] = None,
    channel: Optional[str] = "whatsapp",
) -> Optional[Dict[str, Any]]:
    """The oldest stored message of a chat as {message_id, direction, ts}, or None.

    This is the cursor an on-demand history fetch starts from: a channel that can ask
    the network for "messages before X" (WhatsApp's per-chat history sync) needs the
    key of the oldest message it already holds, and the store is where every message
    that passed the bridge is recorded. Rows without a real id (the `_<ts>_<dir>`
    fallback key) are skipped, because the network would not know them."""
    init_store(username, user_scope_id)
    conn = _get_conn(username, user_scope_id)
    try:
        clauses = ["username = ?", "chat_id = ?", "message_id IS NOT NULL", "message_id NOT LIKE '\\_%' ESCAPE '\\'"]
        params: List[Any] = [(username or "").strip() or "", chat_id or ""]
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        cur = conn.execute(
            f"SELECT message_id, direction, ts FROM channel_messages WHERE {' AND '.join(clauses)} ORDER BY ts ASC LIMIT 1",
            tuple(params),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def chat_stats(
    username: str,
    chat_ids: List[str],
    user_scope_id: Optional[str] = None,
    channel: Optional[str] = None,
) -> Dict[str, Any]:
    """Counts and bounds over a set of chats in one query: {"count", "out_count", "first_ts",
    "last_ts"}. out_count is what the agent (or the owner's own number) sent; deleted
    tombstones are not counted; first_ts is the oldest STORED row, which for a channel that
    loads history on demand is not the first contact ever. A missing store or an empty id
    list answers zeros without creating a database."""
    zeros: Dict[str, Any] = {"count": 0, "out_count": 0, "first_ts": None, "last_ts": None}
    ids = [str(c) for c in (chat_ids or []) if c]
    if not ids or not store_exists(username, user_scope_id):
        return zeros
    init_store(username, user_scope_id)
    conn = _get_conn(username, user_scope_id)
    try:
        placeholders = ",".join("?" for _ in ids)
        clauses = ["username = ?", f"chat_id IN ({placeholders})", "COALESCE(content_type, 'text') != 'deleted'"]
        params: List[Any] = [(username or "").strip() or "", *ids]
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        cur = conn.execute(
            f"SELECT COUNT(*) AS count, COALESCE(SUM(direction = 'out'), 0) AS out_count, "
            f"MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM channel_messages WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        row = dict(cur.fetchone() or {})
        return {
            "count": int(row.get("count") or 0),
            "out_count": int(row.get("out_count") or 0),
            "first_ts": float(row["first_ts"]) if row.get("first_ts") is not None else None,
            "last_ts": float(row["last_ts"]) if row.get("last_ts") is not None else None,
        }
    finally:
        conn.close()


def last_message_ts(
    username: str,
    chat_id: str,
    direction: Optional[str] = None,
    user_scope_id: Optional[str] = None,
    channel: Optional[str] = "whatsapp",
    until_ts: Optional[float] = None,
    exclude_sender: Optional[str] = None,
) -> Optional[float]:
    """Unix timestamp of the newest stored message in a chat, or None when the chat has none.

    direction: "out" = newest message the agent SENT, "in" = newest inbound, None =
    either. `until_ts` bounds the answer from above (rows at or before that time), so a
    caller can ask for the newest inbound that fell INSIDE a window. This is the one query
    behind a channel's reply window: "did the agent write to this number within the last N
    hours" is answered by the store that already records every outbound send, so no bridge
    keeps a second ledger of open conversations. An "in" row does NOT mean the sender was
    accepted: the store keeps a rejected sender's message for the owner's inbox too, which
    is why the reply rule (whatsapp_bridge.conversation_open_until) reads inbound rows only
    inside the window an outbound message opened. `exclude_sender` leaves out rows stored
    under that sender label: the reply rule passes OWNER_SENDER, because a message the
    person sent from the dashboard left the number without the agent writing anything."""
    init_store(username, user_scope_id)
    conn = _get_conn(username, user_scope_id)
    try:
        clauses = ["username = ?", "chat_id = ?"]
        params: List[Any] = [(username or "").strip() or "", chat_id or ""]
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        if direction:
            clauses.append("direction = ?")
            params.append(direction)
        if until_ts is not None:
            clauses.append("ts <= ?")
            params.append(float(until_ts))
        if exclude_sender:
            clauses.append("COALESCE(sender_jid, '') != ?")
            params.append(exclude_sender)
        cur = conn.execute(
            f"SELECT MAX(ts) AS ts FROM channel_messages WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        row = cur.fetchone()
        ts = dict(row).get("ts") if row else None
        return float(ts) if ts is not None else None
    finally:
        conn.close()
