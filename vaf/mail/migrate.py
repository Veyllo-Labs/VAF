# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Legacy import: user-authored artifacts from the v1 sync store into mail.db.

Reads the v1 email_sync.db DIRECTLY via sqlite (never through the
email_sync_store API - with the v2 flag on that API delegates back to the v2
store, which made the first version of this import circular; review finding).
Only user-authored state is carried over: categories and answered_at markers,
matched bracket-tolerantly by Message-ID. Gated by a schema_meta marker so the
full scan runs once per store (later syncs skip); repeated imports can be
forced by deleting the marker row. The legacy DB is never modified here."""
import json
import logging
import sqlite3
from typing import Any, Dict, Optional

from vaf.mail.store import MailStore, _now

logger = logging.getLogger("vaf.mail.migrate")

_MARKER = "legacy_import_done"
_MAX_ATTEMPTS = 5  # bound retries for legacy rows whose mail never reaches the store


def import_legacy_artifacts(store: MailStore, store_username: str,
                            user_scope_id: Optional[str],
                            account_id: Optional[str] = None) -> Dict[str, Any]:
    """Backfill category/answered_at from the v1 store. Never raises.

    Scoped PER ACCOUNT. The marker used to be store-wide while the caller runs
    once per account, so the first account to sync consumed it and every later
    account silently lost its labels and answered markers - and the parallel
    sweep made that racy inside a single session.

    The pass is also not marked done while legacy rows still have no counterpart
    in mail.db: the first v2 sync is bounded to the newest UIDs, so a label
    belonging to an older mail has nothing to attach to yet and deserves another
    try on a later sync. `_MAX_ATTEMPTS` bounds that for rows that will never
    arrive (mail deleted on the server, outside the sync window)."""
    stats = {"categories": 0, "answered": 0, "seen_legacy_rows": 0,
             "unmatched": 0, "skipped": False}
    conn = store._conn()
    marker = f"{_MARKER}:{account_id}" if account_id else _MARKER
    row = conn.execute("SELECT value FROM schema_meta WHERE key=?", (marker,)).fetchone()
    state = _read_state(row)
    if state.get("done"):
        stats["skipped"] = True
        return stats
    attempts = int(state.get("attempts") or 0) + 1
    try:
        from vaf.core.email_sync_store import _db_path, _user_for_query
        legacy_path = _db_path(store_username or None, user_scope_id)
        legacy_user = _user_for_query(store_username or None, user_scope_id)
    except Exception as e:
        logger.info("legacy store helpers unavailable (%s)", e)
        return stats
    if not legacy_path.exists():
        _mark_done(conn, marker, attempts)
        return stats
    try:
        lconn = sqlite3.connect(legacy_path, timeout=10)
        lconn.row_factory = sqlite3.Row
        sql = ("SELECT message_id, category, answered_at FROM email_messages "
               "WHERE username = ? AND ((category NOT IN ('', 'primary')) "
               "OR (answered_at IS NOT NULL AND answered_at != ''))")
        args: list = [legacy_user]
        if account_id:
            sql += " AND account_id = ?"
            args.append(account_id)
        rows = lconn.execute(sql, args).fetchall()
        lconn.close()
    except Exception as e:
        logger.info("legacy store not readable (%s)", e)
        return stats
    for row in rows:
        stats["seen_legacy_rows"] += 1
        mid = (row["message_id"] or "").strip()
        if not mid or mid.startswith("local-"):
            continue
        variants = {mid, mid.strip("<>"), f"<{mid.strip('<>')}>"}
        q = ",".join("?" for _ in variants)
        category = (row["category"] or "").strip()
        answered = (row["answered_at"] or "").strip()
        if not conn.execute(f"SELECT 1 FROM messages WHERE message_id IN ({q}) LIMIT 1",
                            tuple(variants)).fetchone():
            # the mail is not in the v2 store (yet) - retry on a later sync
            stats["unmatched"] += 1
            continue
        if category and category != "primary":
            stats["categories"] += conn.execute(
                f"UPDATE messages SET category=? WHERE message_id IN ({q}) "
                f"AND category IN ('', 'primary')", (category, *variants)).rowcount
        if answered:
            stats["answered"] += conn.execute(
                f"UPDATE messages SET answered_at=? WHERE message_id IN ({q}) "
                f"AND answered_at IS NULL", (answered, *variants)).rowcount
    done = stats["unmatched"] == 0 or attempts >= _MAX_ATTEMPTS
    _mark_done(conn, marker, attempts, done=done)
    return stats


def _read_state(row) -> Dict[str, Any]:
    """Marker value is JSON since the per-account rework; a pre-existing marker
    from the store-wide era is just a timestamp string and counts as done."""
    if row is None:
        return {}
    raw = (row["value"] if not isinstance(row, str) else row) or ""
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"done": True}
    except Exception:
        return {"done": True}


def _mark_done(conn: sqlite3.Connection, marker: str, attempts: int,
               done: bool = True) -> None:
    value = json.dumps({"done": bool(done), "attempts": int(attempts), "at": _now()})
    conn.execute("INSERT INTO schema_meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (marker, value))
    conn.commit()
