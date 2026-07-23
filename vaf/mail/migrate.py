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
import logging
import sqlite3
from typing import Any, Dict, Optional

from vaf.mail.store import MailStore, _now

logger = logging.getLogger("vaf.mail.migrate")

_MARKER = "legacy_import_done"


def import_legacy_artifacts(store: MailStore, store_username: str,
                            user_scope_id: Optional[str]) -> Dict[str, Any]:
    """Backfill category/answered_at from the v1 store. Never raises."""
    stats = {"categories": 0, "answered": 0, "seen_legacy_rows": 0, "skipped": False}
    conn = store._conn()
    if conn.execute("SELECT 1 FROM schema_meta WHERE key=?", (_MARKER,)).fetchone():
        stats["skipped"] = True
        return stats
    try:
        from vaf.core.email_sync_store import _db_path, _user_for_query
        legacy_path = _db_path(store_username or None, user_scope_id)
        legacy_user = _user_for_query(store_username or None, user_scope_id)
    except Exception as e:
        logger.info("legacy store helpers unavailable (%s)", e)
        return stats
    if not legacy_path.exists():
        _mark_done(conn)
        return stats
    try:
        lconn = sqlite3.connect(legacy_path, timeout=10)
        lconn.row_factory = sqlite3.Row
        rows = lconn.execute(
            "SELECT message_id, category, answered_at FROM email_messages "
            "WHERE username = ? AND ((category NOT IN ('', 'primary')) "
            "OR (answered_at IS NOT NULL AND answered_at != ''))",
            (legacy_user,)).fetchall()
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
        if category and category != "primary":
            stats["categories"] += conn.execute(
                f"UPDATE messages SET category=? WHERE message_id IN ({q}) "
                f"AND category IN ('', 'primary')", (category, *variants)).rowcount
        if answered:
            stats["answered"] += conn.execute(
                f"UPDATE messages SET answered_at=? WHERE message_id IN ({q}) "
                f"AND answered_at IS NULL", (answered, *variants)).rowcount
    _mark_done(conn)
    return stats


def _mark_done(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO schema_meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_MARKER, _now()))
    conn.commit()
