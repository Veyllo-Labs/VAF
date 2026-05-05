"""
Persistent per-user notes for the Thinking Mode agent.

The agent can call the `thinking_note_add` tool to store notes like
"User confirmed X is handled — do not ask again" or "User wants to keep Y".
Notes are injected into the system prompt at the start of every thinking run.

Storage: SQLite DB at Platform.data_dir() / "thinking_notes.db"
User isolation: scope_key column (same pattern as thinking_mode.py _key())
Auto-expire: entries older than 30 days are deleted on read
Limit: max 50 notes per user (oldest deleted when exceeded)
"""
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_NOTES_MAX_ENTRIES = 50
_NOTES_MAX_AGE_DAYS = 30
_NOTE_MAX_CHARS = 500


def _db_path() -> Path:
    """Path to the thinking_notes SQLite database."""
    try:
        from vaf.core.platform import Platform
        p = Platform.data_dir() / "thinking_notes.db"
    except Exception:
        p = Path.home() / ".vaf" / "thinking_notes.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    """Open a connection to the notes DB and ensure the schema exists."""
    conn = sqlite3.connect(str(_db_path()), timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    """Create the table if it doesn't exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thinking_notes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_key     TEXT    NOT NULL,
            note          TEXT    NOT NULL,
            created_at    REAL    NOT NULL,
            created_at_iso TEXT   NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_thinking_notes_scope ON thinking_notes(scope_key)"
    )
    conn.commit()


def add_note(scope_key: str, note: str) -> None:
    """
    Persist a note for the given user scope.
    Trims note to _NOTE_MAX_CHARS. After insert, prunes to _NOTES_MAX_ENTRIES.
    No-op on any error.
    """
    try:
        note = (note or "").strip()[:_NOTE_MAX_CHARS]
        if not note:
            return
        now_ts = time.time()
        now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        with _connect() as conn:
            conn.execute(
                "INSERT INTO thinking_notes (scope_key, note, created_at, created_at_iso) VALUES (?, ?, ?, ?)",
                (scope_key, note, now_ts, now_iso),
            )
            # Prune to max entries — keep the newest _NOTES_MAX_ENTRIES rows
            conn.execute(
                """
                DELETE FROM thinking_notes
                WHERE scope_key = ?
                  AND id NOT IN (
                      SELECT id FROM thinking_notes
                      WHERE scope_key = ?
                      ORDER BY created_at DESC
                      LIMIT ?
                  )
                """,
                (scope_key, scope_key, _NOTES_MAX_ENTRIES),
            )
            conn.commit()
    except Exception as exc:
        logger.debug("thinking_notes add_note failed: %s", exc)


def get_notes(scope_key: str) -> List[Dict[str, Any]]:
    """
    Return all non-expired notes for the given user scope, newest first.
    Auto-deletes entries older than _NOTES_MAX_AGE_DAYS. Returns [] on error.
    """
    try:
        cutoff = time.time() - _NOTES_MAX_AGE_DAYS * 86400
        with _connect() as conn:
            # Delete expired entries first
            conn.execute(
                "DELETE FROM thinking_notes WHERE scope_key = ? AND created_at < ?",
                (scope_key, cutoff),
            )
            conn.commit()
            rows = conn.execute(
                "SELECT note, created_at_iso FROM thinking_notes WHERE scope_key = ? ORDER BY created_at DESC",
                (scope_key,),
            ).fetchall()
        return [{"note": r["note"], "created_at_iso": r["created_at_iso"]} for r in rows]
    except Exception as exc:
        logger.debug("thinking_notes get_notes failed: %s", exc)
        return []


def build_notes_prompt(scope_key: str) -> str:
    """
    Build the system-prompt section listing the agent's own notes.
    Returns empty string if there are no notes.
    """
    notes = get_notes(scope_key)
    if not notes:
        return ""
    lines = [
        "**Deine eigenen Notizen aus früheren Thinking-Runs "
        "(beachte diese sorgfältig — sie spiegeln Entscheidungen und Kontext wider):**"
    ]
    for entry in notes:
        ts = entry.get("created_at_iso", "?")
        note = entry.get("note", "").strip()
        if note:
            lines.append(f"- [{ts}] {note}")
    return "\n".join(lines)


def delete_all_notes(scope_key: str) -> int:
    """Delete all notes for a user. Returns number of deleted rows. No-op on error."""
    try:
        with _connect() as conn:
            cur = conn.execute(
                "DELETE FROM thinking_notes WHERE scope_key = ?", (scope_key,)
            )
            conn.commit()
            return cur.rowcount
    except Exception as exc:
        logger.debug("thinking_notes delete_all_notes failed: %s", exc)
        return 0
