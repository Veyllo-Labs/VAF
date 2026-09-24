# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Per-user mail store: one SQLite database per user scope (EMAIL_CLIENT.md).

Design invariants:
- FAIL-CLOSED SCOPING: MailStore requires an explicit non-empty user_scope_id;
  there is no default and no admin fallback. Callers resolve the local admin's
  real scope via get_local_admin_scope_id() themselves.
- DB-assigned identity: messages.id is the identity; (folder, UIDVALIDITY, UID)
  are mutable server coordinates (Thunderbird Panorama pattern).
- Derived data (threads, FTS, counters) is rebuildable from message_raw + the
  server; reindex must stay a cheap operation.
- Raw bodies are zstd-compressed and AES-GCM encrypted (crypto.py, decision E4)
  up to RAW_CACHE_MAX_BYTES; larger mail stays header-only (body_state
  'too_large', fetched live on demand).
- Threading is incremental JWZ-lite: join by Gmail thread id, else by any
  References/In-Reply-To overlap with known messages (both directions, via the
  msg_refs table), else new thread; colliding threads are merged. A full
  RFC 5256 rebuild is available via rebuild_threads().
"""
import functools
import json
import re
import sqlite3
import threading as _threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from vaf.mail.addressing import header_addresses
from vaf.mail.parser import ParsedMessage

SCHEMA_VERSION = 2
RAW_CACHE_MAX_BYTES = 256 * 1024
SNIPPET_CHARS = 240

# Schema version 2 (verification and cases, EMAIL_CLIENT.md "Verification and cases"):
# the per-message verdicts (who wrote it, is the From address who it claims to be), the
# cases a conversation belongs to, the ids of every mail VAF sent. Created for a fresh
# store and by the stepwise migration alike, so the two never drift.
_SCHEMA_V2_SQL = """
CREATE TABLE IF NOT EXISTS message_auth (
  message_pk INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
  machine_kind TEXT NOT NULL DEFAULT '',
  machine_reason TEXT NOT NULL DEFAULT '',
  auth_state TEXT NOT NULL DEFAULT 'unknown',
  auth_source TEXT NOT NULL DEFAULT 'none',
  authserv_id TEXT NOT NULL DEFAULT '',
  topmost_authserv_id TEXT NOT NULL DEFAULT '',
  from_domain TEXT NOT NULL DEFAULT '',
  spf TEXT NOT NULL DEFAULT '',
  spf_domain TEXT NOT NULL DEFAULT '',
  dkim TEXT NOT NULL DEFAULT '',
  dkim_domain TEXT NOT NULL DEFAULT '',
  dmarc TEXT NOT NULL DEFAULT '',
  arc TEXT NOT NULL DEFAULT '',
  compauth TEXT NOT NULL DEFAULT '',
  aligned_by TEXT NOT NULL DEFAULT '',
  via_domain TEXT NOT NULL DEFAULT '',
  flags TEXT NOT NULL DEFAULT '[]',
  reasons TEXT NOT NULL DEFAULT '[]',
  headers TEXT NOT NULL DEFAULT '{}',
  policy_key TEXT NOT NULL DEFAULT '',
  computed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  case_id TEXT NOT NULL,
  thread_id INTEGER REFERENCES threads(id) ON DELETE SET NULL,
  correspondent TEXT NOT NULL DEFAULT '',
  contact_id TEXT NOT NULL DEFAULT '',
  extra_participants TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'open',
  trust_max TEXT NOT NULL DEFAULT '',
  opened_by TEXT NOT NULL DEFAULT 'inbound',
  related_case TEXT NOT NULL DEFAULT '',
  related_reason TEXT NOT NULL DEFAULT '',
  outlook_conv_guid TEXT NOT NULL DEFAULT '',
  subject_norm TEXT NOT NULL DEFAULT '',
  opened_at TEXT NOT NULL,
  closed_at TEXT,
  last_inbound_at TEXT,
  last_outbound_at TEXT,
  UNIQUE(account_id, case_id)
);
CREATE INDEX IF NOT EXISTS idx_cases_thread ON cases(thread_id);
CREATE TABLE IF NOT EXISTS case_messages (
  message_pk INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
  case_id TEXT NOT NULL,
  signal TEXT NOT NULL DEFAULT '',
  certainty TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL DEFAULT '',
  decision TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_messages_case ON case_messages(case_id);
CREATE TABLE IF NOT EXISTS sent_ids (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  message_id TEXT NOT NULL,
  case_id TEXT NOT NULL DEFAULT '',
  to_addrs TEXT NOT NULL DEFAULT '',
  sent_by TEXT NOT NULL DEFAULT 'owner',
  in_reply_to TEXT NOT NULL DEFAULT '',
  delivery TEXT NOT NULL DEFAULT 'queued',
  op_id INTEGER,
  enqueued_at TEXT NOT NULL,
  sent_at TEXT,
  UNIQUE(account_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_sent_ids_case ON sent_ids(case_id);
"""

_RE_SUBJECT_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|wg|sv|antw)(\[\d+\])?:\s*)+", re.IGNORECASE)


@functools.lru_cache(maxsize=1)
def _fts_supports_contentless_delete() -> bool:
    """Can this SQLite delete rows from a contentless FTS5 table?

    ``contentless_delete=1`` arrived in SQLite 3.43 (2023). Older builds reject it while
    the table is being CREATEd, which took the whole mail store down with it - and
    ``requires-python`` allows 3.10, whose bundled SQLite on Windows and macOS predates
    3.43 (found by the nightly full matrix: 81 mail tests erroring on exactly this).

    Probed, not version-compared: which FTS5 options a build compiles in is a property of
    the build, and the thing being avoided is a CREATE that fails.
    """
    try:
        probe = sqlite3.connect(":memory:")
    except sqlite3.Error:
        return False
    try:
        probe.execute("CREATE VIRTUAL TABLE t USING fts5(x, content='', contentless_delete=1)")
        return True
    except sqlite3.Error:
        return False
    finally:
        try:
            probe.close()
        except Exception:
            pass


def _fts_create_sql() -> str:
    """DDL for the message search index, in the best form this SQLite supports.

    Preferred: contentless (``content=''``), which keeps only the index and not a second
    copy of every subject and body - it is the reason a mail store can index bodies without
    doubling on disk. Deleting from one needs 3.43+.

    Fallback: an ordinary FTS5 table, which stores its own copy of the indexed columns.
    Same INSERT / DELETE-by-rowid / MATCH / bm25 surface, so no call site changes; it just
    costs disk. Simply dropping the option is NOT an option: a contentless table without it
    refuses DELETE ("cannot DELETE from contentless fts5 table"), and the store deletes
    from this index on every message removal, purge and re-index.
    """
    columns = 'subject, from_addr, to_addrs, body_text'
    tokenizer = 'tokenize="unicode61 remove_diacritics 2"'
    if _fts_supports_contentless_delete():
        return (f"CREATE VIRTUAL TABLE messages_fts USING fts5("
                f"{columns}, content='', contentless_delete=1, {tokenizer})")
    return f"CREATE VIRTUAL TABLE messages_fts USING fts5({columns}, {tokenizer})"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_subject(subject: str) -> str:
    """Base subject per RFC 5256 spirit: strip reply/forward prefixes (incl. the
    German AW:/WG: variants) and collapse whitespace, lowercased."""
    s = _RE_SUBJECT_PREFIX.sub("", subject or "")
    return re.sub(r"\s+", " ", s).strip().lower()


class MailStore:
    """One instance per (user scope). Connections are per-call; SQLite WAL keeps
    concurrent reader/writer behavior sane across the API worker threads."""

    def __init__(self, user_scope_id: str, base_dir: Optional[Path] = None):
        scope = str(user_scope_id or "").strip()
        if not scope:
            raise ValueError("MailStore requires an explicit user_scope_id (fail-closed; "
                             "resolve the local admin scope via get_local_admin_scope_id())")
        self.user_scope_id = scope
        if base_dir is None:
            from vaf.core.platform import Platform
            base_dir = Platform.data_dir()
        self.db_path = Path(base_dir) / "scopes" / scope / "mail.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = _threading.local()
        self.ensure_schema()

    # ── connection / schema ─────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def ensure_schema(self) -> None:
        conn = self._conn()
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'")
        if cur.fetchone() is None:
            self._create_schema(conn)
            return
        row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"mail.db schema {version} is newer than this build ({SCHEMA_VERSION})")
        if version < 2:
            self._migrate_to_2(conn)

    def _migrate_to_2(self, conn: sqlite3.Connection) -> None:
        """Version 1 to 2: the verification and case tables. Additive only (no column of a
        v1 table changes), so a v1 store keeps every row and the derived verdicts are
        filled by the backfill (MailService.backfill_verification) from the cached raw
        bytes and, for header-only rows, at the next sync that touches them."""
        conn.executescript(_SCHEMA_V2_SQL)
        conn.execute("INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))
        conn.execute("INSERT INTO schema_meta(key, value) VALUES('migrated_to_2_at', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_now(),))
        conn.commit()

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(f"""
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE accounts (
          id INTEGER PRIMARY KEY,
          account_id TEXT NOT NULL UNIQUE,
          provider TEXT NOT NULL DEFAULT 'imap',
          email TEXT NOT NULL,
          created_at TEXT NOT NULL,
          last_sync_at TEXT,
          sync_state TEXT NOT NULL DEFAULT '{{}}'
        );
        CREATE TABLE folders (
          id INTEGER PRIMARY KEY,
          account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          special_use TEXT,
          uidvalidity INTEGER,
          uidnext INTEGER,
          highestmodseq INTEGER,
          last_seen_uid INTEGER NOT NULL DEFAULT 0,
          sync_tier TEXT NOT NULL DEFAULT 'lazy',
          updated_at TEXT,
          UNIQUE(account_id, name)
        );
        CREATE TABLE threads (
          id INTEGER PRIMARY KEY,
          account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
          subject_norm TEXT,
          gm_thrid TEXT,
          message_count INTEGER NOT NULL DEFAULT 0,
          last_date_ts INTEGER
        );
        CREATE INDEX idx_threads_gm ON threads(account_id, gm_thrid);
        CREATE TABLE messages (
          id INTEGER PRIMARY KEY,
          account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
          folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
          uid INTEGER,
          message_id TEXT,
          gm_msgid TEXT,
          gm_thrid TEXT,
          thread_id INTEGER REFERENCES threads(id),
          subject TEXT NOT NULL DEFAULT '',
          from_addr TEXT NOT NULL DEFAULT '',
          to_addrs TEXT NOT NULL DEFAULT '',
          cc_addrs TEXT NOT NULL DEFAULT '',
          date_ts INTEGER,
          internaldate_ts INTEGER,
          snippet TEXT NOT NULL DEFAULT '',
          category TEXT NOT NULL DEFAULT '',
          answered_at TEXT,
          flags TEXT NOT NULL DEFAULT '[]',
          server_flags TEXT NOT NULL DEFAULT '[]',
          size_bytes INTEGER,
          has_attachments INTEGER NOT NULL DEFAULT 0,
          body_state TEXT NOT NULL DEFAULT 'none',
          defects TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL,
          UNIQUE(folder_id, uid)
        );
        CREATE INDEX idx_messages_thread ON messages(thread_id);
        CREATE INDEX idx_messages_acct_date ON messages(account_id, date_ts DESC);
        CREATE INDEX idx_messages_msgid ON messages(message_id);
        CREATE INDEX idx_messages_gm_msgid ON messages(account_id, gm_msgid);
        CREATE TABLE msg_refs (
          message_pk INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          ref_id TEXT NOT NULL
        );
        CREATE INDEX idx_msg_refs_ref ON msg_refs(ref_id);
        CREATE INDEX idx_msg_refs_pk ON msg_refs(message_pk);
        CREATE TABLE message_raw (
          message_pk INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
          enc INTEGER NOT NULL DEFAULT 1,
          codec TEXT NOT NULL DEFAULT 'zstd',
          raw BLOB NOT NULL
        );
        CREATE TABLE attachments (
          id INTEGER PRIMARY KEY,
          message_pk INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          part_id TEXT NOT NULL,
          filename TEXT,
          content_type TEXT,
          size_bytes INTEGER,
          content_id TEXT,
          is_inline INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_attachments_msg ON attachments(message_pk);
        CREATE TABLE ops (
          id INTEGER PRIMARY KEY,
          account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
          kind TEXT NOT NULL,
          payload TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT
        );
        CREATE INDEX idx_ops_state ON ops(account_id, state);
        """)
        conn.executescript(_SCHEMA_V2_SQL)
        conn.execute(_fts_create_sql())
        conn.execute("INSERT INTO schema_meta(key, value) VALUES('fts_variant', ?)",
                     ("contentless" if _fts_supports_contentless_delete() else "stored",))
        conn.execute("INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
        conn.execute("INSERT INTO schema_meta(key, value) VALUES('created_at', ?)", (_now(),))
        conn.commit()

    # ── accounts ────────────────────────────────────────────────────────────

    def upsert_account(self, account_id: str, provider: str, email: str) -> int:
        conn = self._conn()
        conn.execute(
            "INSERT INTO accounts(account_id, provider, email, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(account_id) DO UPDATE SET provider=excluded.provider, email=excluded.email",
            (account_id, provider or "imap", email or account_id, _now()))
        conn.commit()
        return self.account_pk(account_id)  # type: ignore[return-value]

    def account_pk(self, account_id: str) -> Optional[int]:
        row = self._conn().execute(
            "SELECT id FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        return int(row["id"]) if row else None

    def list_accounts(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM accounts ORDER BY created_at").fetchall()]

    def delete_account(self, account_id: str) -> bool:
        """Cascade-delete an account and every derived row (FTS cleaned per message)."""
        pk = self.account_pk(account_id)
        if pk is None:
            return False
        conn = self._conn()
        for r in conn.execute("SELECT id FROM messages WHERE account_id=?", (pk,)).fetchall():
            conn.execute("DELETE FROM messages_fts WHERE rowid=?", (int(r["id"]),))
        conn.execute("DELETE FROM accounts WHERE id=?", (pk,))
        conn.commit()
        return True

    def set_account_synced(self, account_pk: int) -> None:
        conn = self._conn()
        conn.execute("UPDATE accounts SET last_sync_at=? WHERE id=?", (_now(), account_pk))
        conn.commit()

    # ── folders ─────────────────────────────────────────────────────────────

    def upsert_folder(self, account_pk: int, name: str, special_use: Optional[str] = None,
                      sync_tier: Optional[str] = None) -> int:
        conn = self._conn()
        conn.execute(
            "INSERT INTO folders(account_id, name, special_use, sync_tier, updated_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(account_id, name) DO UPDATE SET "
            "special_use=COALESCE(excluded.special_use, folders.special_use), "
            "sync_tier=COALESCE(?, folders.sync_tier), updated_at=excluded.updated_at",
            (account_pk, name, special_use, sync_tier or "lazy", _now(), sync_tier))
        conn.commit()
        row = conn.execute("SELECT id FROM folders WHERE account_id=? AND name=?",
                           (account_pk, name)).fetchone()
        return int(row["id"])

    def get_folder(self, account_pk: int, name: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM folders WHERE account_id=? AND name=?", (account_pk, name)).fetchone()
        return dict(row) if row else None

    def list_folders(self, account_pk: int) -> List[Dict[str, Any]]:
        # total + unread per folder for the sidebar (unread = flags without \Seen,
        # same predicate as list_threads). Cheap: one indexed COUNT per folder.
        return [dict(r) for r in self._conn().execute(
            "SELECT f.*, "
            "(SELECT COUNT(*) FROM messages m WHERE m.folder_id=f.id) AS total, "
            "(SELECT COUNT(*) FROM messages m WHERE m.folder_id=f.id "
            " AND m.flags NOT LIKE '%\\\\Seen%') AS unread "
            "FROM folders f WHERE f.account_id=? ORDER BY f.name", (account_pk,)).fetchall()]

    def set_folder_state(self, folder_pk: int, *, uidvalidity: Optional[int] = None,
                         uidnext: Optional[int] = None, highestmodseq: Optional[int] = None,
                         last_seen_uid: Optional[int] = None) -> None:
        sets, args = ["updated_at=?"], [_now()]
        for col, val in (("uidvalidity", uidvalidity), ("uidnext", uidnext),
                         ("highestmodseq", highestmodseq), ("last_seen_uid", last_seen_uid)):
            if val is not None:
                sets.append(f"{col}=?")
                args.append(int(val))
        args.append(folder_pk)
        conn = self._conn()
        conn.execute(f"UPDATE folders SET {', '.join(sets)} WHERE id=?", args)
        conn.commit()

    def reset_folder(self, folder_pk: int, new_uidvalidity: Optional[int]) -> int:
        """UIDVALIDITY changed (RFC 4549 4.1): drop every cached message of the
        folder (FTS cleaned per message) and reset sync bookkeeping."""
        conn = self._conn()
        rows = conn.execute("SELECT id FROM messages WHERE folder_id=?", (folder_pk,)).fetchall()
        for r in rows:
            conn.execute("DELETE FROM messages_fts WHERE rowid=?", (int(r["id"]),))
        conn.execute("DELETE FROM messages WHERE folder_id=?", (folder_pk,))
        conn.execute(
            "UPDATE folders SET uidvalidity=?, uidnext=NULL, highestmodseq=NULL, "
            "last_seen_uid=0, updated_at=? WHERE id=?",
            (new_uidvalidity, _now(), folder_pk))
        # threads spanning the wiped folder keep correct counts; emptied ones go
        conn.execute(
            "UPDATE threads SET message_count="
            "(SELECT COUNT(*) FROM messages WHERE thread_id=threads.id), "
            "last_date_ts=(SELECT MAX(COALESCE(date_ts, internaldate_ts)) "
            "FROM messages WHERE thread_id=threads.id)")
        conn.execute("DELETE FROM threads WHERE message_count <= 0")
        conn.commit()
        return len(rows)

    # ── messages: ingest ────────────────────────────────────────────────────

    def message_uid_map(self, folder_pk: int) -> Dict[int, int]:
        """uid -> message pk for RFC 4549 flag/expunge diffing."""
        return {int(r["uid"]): int(r["id"]) for r in self._conn().execute(
            "SELECT id, uid FROM messages WHERE folder_id=? AND uid IS NOT NULL",
            (folder_pk,)).fetchall()}

    def ingest_message(self, account_pk: int, folder_pk: int, uid: Optional[int],
                       parsed: ParsedMessage, raw: Optional[bytes] = None,
                       server_flags: Optional[Iterable[str]] = None,
                       internaldate_ts: Optional[int] = None,
                       size_bytes: Optional[int] = None,
                       gm_msgid: Optional[str] = None,
                       gm_thrid: Optional[str] = None,
                       category: str = "",
                       auth_policy: Optional[Dict[str, Any]] = None) -> int:
        """Insert or update one message; updates FTS, attachments, raw blob, thread
        linkage and the verification verdict in the same transaction (index desync is
        structurally impossible - the Gloda lesson). `auth_policy` is the account's
        verification policy (vaf/mail/verification.auth_policy_for_account); without it
        the verdict still records the machine kind and the provider's header, with every
        sender unknown rather than verified."""
        conn = self._conn()
        flags_json = json.dumps(sorted(set(server_flags or [])))
        snippet = re.sub(r"\s+", " ", parsed.body_text or "")[:SNIPPET_CHARS]
        existing = None
        adopted_ghost = False
        if uid is not None:
            existing = conn.execute(
                "SELECT id FROM messages WHERE folder_id=? AND uid=?",
                (folder_pk, uid)).fetchone()
            if existing is None and parsed.message_id:
                # A local move re-parented a row into this folder with uid=NULL
                # (move_message_local). When the server copy arrives under its new
                # uid, ADOPT the uid-NULL ghost instead of inserting a second row -
                # otherwise every archive/trash leaves a permanent visible duplicate.
                existing = conn.execute(
                    "SELECT id FROM messages WHERE folder_id=? AND uid IS NULL AND message_id=?",
                    (folder_pk, parsed.message_id)).fetchone()
                adopted_ghost = existing is not None
        try:
            if existing:
                pk = int(existing["id"])
                if adopted_ghost:
                    # Take the server uid, refresh the server shadow, and KEEP the
                    # ghost's local flags (its pending local intent must not be
                    # stomped by the server truth on adoption).
                    conn.execute(
                        "UPDATE messages SET uid=?, server_flags=?, size_bytes=COALESCE(?, size_bytes) "
                        "WHERE id=?",
                        (uid, flags_json, size_bytes, pk))
                else:
                    conn.execute(
                        "UPDATE messages SET server_flags=?, flags=?, size_bytes=COALESCE(?, size_bytes) "
                        "WHERE id=?",
                        (flags_json, flags_json, size_bytes, pk))
            else:
                cur = conn.execute(
                    "INSERT INTO messages(account_id, folder_id, uid, message_id, gm_msgid, gm_thrid, "
                    "subject, from_addr, to_addrs, cc_addrs, date_ts, internaldate_ts, snippet, "
                    "category, flags, server_flags, size_bytes, has_attachments, body_state, defects, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (account_pk, folder_pk, uid, parsed.message_id or None, gm_msgid, gm_thrid,
                     parsed.subject, parsed.from_addr, parsed.to_addrs, parsed.cc_addrs,
                     parsed.date_ts, internaldate_ts, snippet, category,
                     flags_json, flags_json, size_bytes,
                     1 if parsed.has_attachments else 0,
                     "none", json.dumps(parsed.defects[:20]), _now()))
                pk = int(cur.lastrowid)
                for ref in parsed.refs[:64]:
                    conn.execute("INSERT INTO msg_refs(message_pk, ref_id) VALUES(?,?)", (pk, ref))
                if parsed.message_id:
                    conn.execute("INSERT INTO msg_refs(message_pk, ref_id) VALUES(?,?)",
                                 (pk, parsed.message_id))
                for a in parsed.attachments:
                    conn.execute(
                        "INSERT INTO attachments(message_pk, part_id, filename, content_type, "
                        "size_bytes, content_id, is_inline) VALUES(?,?,?,?,?,?,?)",
                        (pk, a.part_id, a.filename, a.content_type, a.size_bytes,
                         a.content_id, 1 if a.is_inline else 0))
                conn.execute(
                    "INSERT INTO messages_fts(rowid, subject, from_addr, to_addrs, body_text) "
                    "VALUES(?,?,?,?,?)",
                    (pk, parsed.subject, parsed.from_addr, parsed.to_addrs,
                     (parsed.body_text or "")[:100_000]))
                self._assign_thread(conn, account_pk, pk, parsed, gm_thrid)
            if not existing or conn.execute(
                    "SELECT 1 FROM message_auth WHERE message_pk=?", (pk,)).fetchone() is None:
                # The verdict is computed from the bytes fetched NOW, before the size
                # decision below: a message too large to cache still gets its row.
                self._write_message_auth_in(conn, pk, self._assess(
                    parsed, account_pk, auth_policy, category))
            if raw is not None and len(raw) <= RAW_CACHE_MAX_BYTES:
                self._store_raw(conn, pk, raw)
                if existing:
                    # body arrived for a header-only row: refresh snippet + FTS
                    # (review finding - the UPDATE branch used to skip both)
                    conn.execute("UPDATE messages SET snippet=? WHERE id=?", (snippet, pk))
                    conn.execute("DELETE FROM messages_fts WHERE rowid=?", (pk,))
                    conn.execute(
                        "INSERT INTO messages_fts(rowid, subject, from_addr, to_addrs, body_text) "
                        "VALUES(?,?,?,?,?)",
                        (pk, parsed.subject, parsed.from_addr, parsed.to_addrs,
                         (parsed.body_text or "")[:100_000]))
                conn.execute("UPDATE messages SET body_state='cached' WHERE id=?", (pk,))
            elif raw is not None:
                conn.execute("UPDATE messages SET body_state='too_large' WHERE id=?", (pk,))
            conn.commit()
            return pk
        except Exception:
            conn.rollback()
            raise

    def _store_raw(self, conn: sqlite3.Connection, pk: int, raw: bytes) -> None:
        import zstandard
        from vaf.mail.crypto import encrypt_blob
        blob = encrypt_blob(zstandard.ZstdCompressor(level=6).compress(raw))
        conn.execute(
            "INSERT INTO message_raw(message_pk, enc, codec, raw) VALUES(?,1,'zstd',?) "
            "ON CONFLICT(message_pk) DO UPDATE SET raw=excluded.raw", (pk, blob))

    def get_raw(self, pk: int) -> Optional[bytes]:
        row = self._conn().execute(
            "SELECT enc, codec, raw FROM message_raw WHERE message_pk=?", (pk,)).fetchone()
        if not row:
            return None
        import zstandard
        data = bytes(row["raw"])
        if int(row["enc"]):
            from vaf.mail.crypto import decrypt_blob
            data = decrypt_blob(data)
        if row["codec"] == "zstd":
            data = zstandard.ZstdDecompressor().decompress(data)
        return data

    # ── threading (incremental JWZ-lite; see module docstring) ─────────────

    def _assign_thread(self, conn: sqlite3.Connection, account_pk: int, pk: int,
                       parsed: ParsedMessage, gm_thrid: Optional[str]) -> None:
        thread_id: Optional[int] = None
        if gm_thrid:
            row = conn.execute(
                "SELECT id FROM threads WHERE account_id=? AND gm_thrid=?",
                (account_pk, gm_thrid)).fetchone()
            if row:
                thread_id = int(row["id"])
        if thread_id is None and (parsed.refs or parsed.message_id):
            ids = list(parsed.refs)
            if parsed.message_id:
                ids.append(parsed.message_id)
            q = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT DISTINCT m.thread_id FROM msg_refs r JOIN messages m ON m.id=r.message_pk "
                f"WHERE r.ref_id IN ({q}) AND m.account_id=? AND m.thread_id IS NOT NULL "
                f"AND m.id != ?",
                (*ids, account_pk, pk)).fetchall()
            found = sorted({int(r["thread_id"]) for r in rows})
            if found:
                thread_id = found[0]
                for other in found[1:]:
                    self._merge_threads(conn, keep=thread_id, drop=other)
        if thread_id is None:
            cur = conn.execute(
                "INSERT INTO threads(account_id, subject_norm, gm_thrid) VALUES(?,?,?)",
                (account_pk, normalize_subject(parsed.subject), gm_thrid))
            thread_id = int(cur.lastrowid)
        elif gm_thrid:
            conn.execute("UPDATE threads SET gm_thrid=COALESCE(gm_thrid, ?) WHERE id=?",
                         (gm_thrid, thread_id))
        conn.execute("UPDATE messages SET thread_id=? WHERE id=?", (thread_id, pk))
        conn.execute(
            "UPDATE threads SET message_count=(SELECT COUNT(*) FROM messages WHERE thread_id=?), "
            "last_date_ts=(SELECT MAX(COALESCE(date_ts, internaldate_ts)) FROM messages WHERE thread_id=?) "
            "WHERE id=?", (thread_id, thread_id, thread_id))

    def _merge_threads(self, conn: sqlite3.Connection, keep: int, drop: int) -> None:
        conn.execute("UPDATE messages SET thread_id=? WHERE thread_id=?", (keep, drop))
        # A case rides on its thread; thread ids are not stable (a Gmail thread id arriving
        # late, a reply joining two threads), so the case follows the merge.
        conn.execute("UPDATE cases SET thread_id=? WHERE thread_id=?", (keep, drop))
        conn.execute("DELETE FROM threads WHERE id=?", (drop,))
        conn.execute(
            "UPDATE threads SET message_count=(SELECT COUNT(*) FROM messages WHERE thread_id=?), "
            "last_date_ts=(SELECT MAX(COALESCE(date_ts, internaldate_ts)) FROM messages WHERE thread_id=?) "
            "WHERE id=?", (keep, keep, keep))

    def rebuild_threads(self) -> int:
        """Full rebuild of thread assignment from msg_refs (cheap reindex command).
        Returns the number of threads after the rebuild."""
        conn = self._conn()
        conn.execute("UPDATE messages SET thread_id=NULL")
        conn.execute("DELETE FROM threads")
        rows = conn.execute(
            "SELECT id, account_id, subject, gm_thrid, message_id FROM messages "
            "ORDER BY COALESCE(date_ts, internaldate_ts, 0), id").fetchall()
        conn.commit()
        for r in rows:
            refs = [x["ref_id"] for x in conn.execute(
                "SELECT ref_id FROM msg_refs WHERE message_pk=?", (int(r["id"]),)).fetchall()]
            parsed = ParsedMessage(message_id=r["message_id"] or "", subject=r["subject"] or "",
                                   refs=[x for x in refs if x != (r["message_id"] or "")])
            self._assign_thread(conn, int(r["account_id"]), int(r["id"]), parsed, r["gm_thrid"])
        conn.commit()
        return int(conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0])

    # ── messages: flags / expunge / queries ────────────────────────────────

    def apply_server_flags(self, folder_pk: int, uid_flags: Dict[int, Iterable[str]]) -> int:
        """RFC 4549 flag resync: the server is authoritative for a message's
        flags UNLESS a local flag change is still queued for it. For a message
        with a pending/sending flags op, only the server shadow (server_flags)
        is updated - the local `flags` (the user's not-yet-pushed intent, e.g. a
        star) is preserved until the op replays. The `AND server_flags != ?`
        guard keeps a no-change resync a no-op (protects the refuted #13 case).
        Returns the number of updated rows."""
        conn = self._conn()
        n = 0
        for uid, flags in uid_flags.items():
            fj = json.dumps(sorted(set(flags)))
            row = conn.execute("SELECT id FROM messages WHERE folder_id=? AND uid=?",
                               (folder_pk, int(uid))).fetchone()
            has_pending = row is not None and conn.execute(
                "SELECT 1 FROM ops WHERE kind='flags' AND state IN ('pending','sending') "
                "AND json_extract(payload, '$.message_pk')=? LIMIT 1",
                (int(row["id"]),)).fetchone() is not None
            if has_pending:
                n += conn.execute(
                    "UPDATE messages SET server_flags=? WHERE folder_id=? AND uid=? "
                    "AND server_flags != ?", (fj, folder_pk, int(uid), fj)).rowcount
            else:
                n += conn.execute(
                    "UPDATE messages SET server_flags=?, flags=? WHERE folder_id=? AND uid=? "
                    "AND server_flags != ?", (fj, fj, folder_pk, int(uid), fj)).rowcount
        conn.commit()
        return n

    def apply_server_flags_delta(self, folder_pk: int, uid: int,
                                 add: Iterable[str] = (), remove: Iterable[str] = ()) -> bool:
        """Update ONLY the server shadow by the delta we actually pushed to the
        server (add/remove), not the full local flag list. The shadow must
        reflect what the server received, so a later resync diffs correctly."""
        conn = self._conn()
        row = conn.execute("SELECT id, server_flags FROM messages WHERE folder_id=? AND uid=?",
                           (folder_pk, int(uid))).fetchone()
        if not row:
            return False
        sf = set(json.loads(row["server_flags"] or "[]"))
        sf |= set(add)
        sf -= set(remove)
        conn.execute("UPDATE messages SET server_flags=? WHERE id=?",
                     (json.dumps(sorted(sf)), int(row["id"])))
        conn.commit()
        return True

    def remove_vanished(self, folder_pk: int, present_uids: Iterable[int],
                        max_uid: Optional[int] = None) -> int:
        """Expunge detection (RFC 4549 4.3.1): cached UIDs missing from the
        server's response are removed locally. max_uid bounds the candidates to
        the range the caller actually resynced - a message ingested moments ago
        with a HIGHER uid is outside the presence window and must never be
        treated as vanished (regression caught by test_incremental_new_mail)."""
        conn = self._conn()
        present = set(int(u) for u in present_uids)
        rows = conn.execute(
            "SELECT id, uid, thread_id FROM messages WHERE folder_id=? AND uid IS NOT NULL",
            (folder_pk,)).fetchall()
        gone = [r for r in rows
                if int(r["uid"]) not in present
                and (max_uid is None or int(r["uid"]) <= int(max_uid))]
        for r in gone:
            conn.execute("DELETE FROM messages_fts WHERE rowid=?", (int(r["id"]),))
            conn.execute("DELETE FROM messages WHERE id=?", (int(r["id"]),))
            tid = r["thread_id"]
            if tid is not None:
                conn.execute(
                    "UPDATE threads SET message_count=(SELECT COUNT(*) FROM messages WHERE thread_id=?) "
                    "WHERE id=?", (tid, tid))
        conn.execute("DELETE FROM threads WHERE message_count <= 0")
        conn.commit()
        return len(gone)

    def get_message(self, pk: int) -> Optional[Dict[str, Any]]:
        row = self._conn().execute("SELECT * FROM messages WHERE id=?", (pk,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["flags"] = json.loads(d.get("flags") or "[]")
        d["server_flags"] = json.loads(d.get("server_flags") or "[]")
        return d

    # ── agent-facing helpers (P3.2): resolve a Message-ID, cache an on-demand body,
    #    write category/answered by pk, locate a message for a live fetch ──

    def pk_by_message_id(self, message_id: str, account_id: Optional[str] = None) -> Optional[int]:
        """Resolve a Message-ID to the best local pk. A self-addressed mail exists
        in several folders (INBOX + Sent/All-Mail); prefer a copy whose body is
        cached so read paths do not land on an empty duplicate. Bracket-tolerant."""
        mid = (message_id or "").strip()
        variants = {mid, mid.strip("<>"), f"<{mid.strip('<>')}>"}
        q = ",".join("?" for _ in variants)
        row = self._conn().execute(
            f"SELECT m.id FROM messages m JOIN accounts a ON a.id=m.account_id "
            f"WHERE m.message_id IN ({q}) AND (?='' OR a.account_id=?) "
            f"ORDER BY (m.body_state='cached') DESC, m.id DESC LIMIT 1",
            (*variants, account_id or "", account_id or "")).fetchone()
        return int(row["id"]) if row else None

    def message_location(self, pk: int) -> tuple:
        """(account_id_str, folder_name, uid) for a pk - drives the on-demand fetch."""
        row = self._conn().execute(
            "SELECT a.account_id AS acct, f.name AS folder, m.uid AS uid "
            "FROM messages m JOIN accounts a ON a.id=m.account_id "
            "JOIN folders f ON f.id=m.folder_id WHERE m.id=?", (pk,)).fetchone()
        return (row["acct"], row["folder"], row["uid"]) if row else (None, None, None)

    def cache_raw(self, pk: int, raw: bytes) -> None:
        """Cache a freshly-fetched raw for an existing message (on-demand body)."""
        conn = self._conn()
        if len(raw) <= RAW_CACHE_MAX_BYTES:
            self._store_raw(conn, pk, raw)
            conn.execute("UPDATE messages SET body_state='cached' WHERE id=?", (pk,))
        else:
            conn.execute("UPDATE messages SET body_state='too_large' WHERE id=?", (pk,))
        conn.commit()

    def set_category(self, pk: int, category: str) -> None:
        conn = self._conn()
        conn.execute("UPDATE messages SET category=? WHERE id=?", (category, pk))
        conn.commit()

    def list_for_relabel(self) -> List[Dict[str, Any]]:
        """Every message as {pk, from_addr, category} for a sender-rule backfill
        (uncapped, unlike list_messages)."""
        rows = self._conn().execute(
            "SELECT id AS pk, from_addr, category FROM messages").fetchall()
        return [dict(r) for r in rows]

    def set_answered(self, pk: int, at: Optional[str] = None) -> None:
        conn = self._conn()
        if at:
            conn.execute("UPDATE messages SET answered_at=? WHERE id=?", (at, pk))
        else:
            conn.execute("UPDATE messages SET answered_at=datetime('now') WHERE id=?", (pk,))
        conn.commit()

    # ── verification (schema v2): one verdict row per message ───────────────

    MESSAGE_AUTH_FIELDS = (
        "machine_kind", "machine_reason", "auth_state", "auth_source", "authserv_id",
        "topmost_authserv_id", "from_domain", "spf", "spf_domain", "dkim", "dkim_domain",
        "dmarc", "arc", "compauth", "aligned_by", "via_domain", "flags", "reasons",
        "headers", "policy_key",
    )
    _MESSAGE_AUTH_JSON = ("flags", "reasons", "headers")

    def _assess(self, parsed: ParsedMessage, account_pk: int,
                auth_policy: Optional[Dict[str, Any]], category: str) -> Dict[str, Any]:
        from vaf.mail.verification import assess
        return assess(parsed, policy=auth_policy,
                      is_own_message_id=lambda mid: self.is_sent_id(account_pk, mid),
                      category=category or "")

    def _write_message_auth_in(self, conn: sqlite3.Connection, pk: int, row: Dict[str, Any]) -> None:
        values: List[Any] = []
        for name in self.MESSAGE_AUTH_FIELDS:
            v = row.get(name)
            if name in self._MESSAGE_AUTH_JSON:
                v = json.dumps(v if v is not None else ([] if name != "headers" else {}))
            values.append("" if v is None else v)
        cols = ", ".join(self.MESSAGE_AUTH_FIELDS)
        marks = ",".join("?" for _ in self.MESSAGE_AUTH_FIELDS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in self.MESSAGE_AUTH_FIELDS)
        conn.execute(
            f"INSERT INTO message_auth(message_pk, {cols}, computed_at) VALUES(?,{marks},?) "
            f"ON CONFLICT(message_pk) DO UPDATE SET {updates}, computed_at=excluded.computed_at",
            (int(pk), *values, _now()))

    def write_message_auth(self, pk: int, row: Dict[str, Any]) -> None:
        """Store (or replace) the verdict of one message. `row` carries the
        MESSAGE_AUTH_FIELDS (missing ones default; flags and reasons are lists, headers a
        dict); computed_at is stamped here. The verdict is computed once at ingest and
        replaced only by an explicit backfill under a new policy: DKIM keys rotate, so a
        later recomputation from the same bytes can differ from the verdict at receipt."""
        conn = self._conn()
        self._write_message_auth_in(conn, pk, row)
        conn.commit()

    def message_auth(self, pks: Iterable[int]) -> Dict[int, Dict[str, Any]]:
        """The stored verdicts of the given messages, keyed by pk (absent when never
        computed, which a caller reads as auth_state unknown)."""
        ids = [int(x) for x in pks]
        out: Dict[int, Dict[str, Any]] = {}
        if not ids:
            return out
        conn = self._conn()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" for _ in chunk)
            for r in conn.execute(f"SELECT * FROM message_auth WHERE message_pk IN ({q})", chunk).fetchall():
                d = dict(r)
                for name in self._MESSAGE_AUTH_JSON:
                    try:
                        d[name] = json.loads(d.get(name) or ("{}" if name == "headers" else "[]"))
                    except Exception:
                        d[name] = {} if name == "headers" else []
                out[int(d["message_pk"])] = d
        return out

    def topmost_authserv_ids(self, account_pk: int, *, limit: int = 200) -> List[str]:
        """The authserv-id of the topmost Authentication-Results header of the account's
        newest inbox messages (the value written by whichever host delivered the mail into
        this mailbox), for learning the provider's id. Empty strings are the Microsoft
        id-less form and stay in the list so the caller can recognise that profile."""
        rows = self._conn().execute(
            "SELECT ma.topmost_authserv_id AS tid, ma.headers AS headers FROM message_auth ma "
            "JOIN messages m ON m.id=ma.message_pk JOIN folders f ON f.id=m.folder_id "
            "WHERE m.account_id=? AND (f.special_use='\\Inbox' OR upper(f.name)='INBOX') "
            "ORDER BY COALESCE(m.date_ts, m.internaldate_ts, 0) DESC, m.id DESC LIMIT ?",
            (int(account_pk), max(1, min(int(limit), 2000)))).fetchall()
        out: List[str] = []
        for r in rows:
            try:
                heads = json.loads(r["headers"] or "{}")
            except Exception:
                heads = {}
            if not (heads.get("auth_results") or []):
                continue  # a message without any Authentication-Results says nothing about the provider
            out.append(str(r["tid"] or ""))
        return out

    def topmost_auth_headers(self, account_pk: int, *, limit: int = 200) -> List[str]:
        """The raw topmost Authentication-Results value per inbox message, newest first
        (the learner's second input: recognising the Microsoft id-less form)."""
        rows = self._conn().execute(
            "SELECT ma.headers AS headers FROM message_auth ma "
            "JOIN messages m ON m.id=ma.message_pk JOIN folders f ON f.id=m.folder_id "
            "WHERE m.account_id=? AND (f.special_use='\\Inbox' OR upper(f.name)='INBOX') "
            "ORDER BY COALESCE(m.date_ts, m.internaldate_ts, 0) DESC, m.id DESC LIMIT ?",
            (int(account_pk), max(1, min(int(limit), 2000)))).fetchall()
        out: List[str] = []
        for r in rows:
            try:
                heads = json.loads(r["headers"] or "{}")
            except Exception:
                continue
            vals = heads.get("auth_results") or []
            if vals:
                out.append(str(vals[0]))
        return out

    # ── sent ids (schema v2): every Message-ID VAF itself sent ───────────────

    def is_sent_id(self, account_pk: int, message_id: str) -> bool:
        """Whether this account sent a mail with that Message-ID (bracketed, as delivered)."""
        mid = str(message_id or "").strip()
        if not mid:
            return False
        if not mid.startswith("<"):
            mid = f"<{mid}>"
        row = self._conn().execute(
            "SELECT 1 FROM sent_ids WHERE account_id=? AND message_id=?", (int(account_pk), mid)).fetchone()
        return row is not None

    def record_sent_id(self, account_pk: int, message_id: str, *, case_id: str = "", to_addrs: str = "",
                       sent_by: str = "owner", in_reply_to: str = "", op_id: Optional[int] = None,
                       delivery: str = "queued") -> int:
        """Remember a Message-ID this account is about to send (written at enqueue, so the
        row exists before the wire and a fast reply is recognised). Idempotent per id."""
        mid = str(message_id or "").strip()
        if not mid:
            raise ValueError("record_sent_id needs a Message-ID")
        if not mid.startswith("<"):
            mid = f"<{mid}>"
        conn = self._conn()
        conn.execute(
            "INSERT INTO sent_ids(account_id, message_id, case_id, to_addrs, sent_by, in_reply_to, delivery, op_id, enqueued_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id, message_id) DO UPDATE SET "
            "case_id=CASE WHEN excluded.case_id != '' THEN excluded.case_id ELSE sent_ids.case_id END, "
            "op_id=COALESCE(excluded.op_id, sent_ids.op_id)",
            (int(account_pk), mid, case_id or "", to_addrs or "", sent_by or "owner", in_reply_to or "",
             delivery or "queued", op_id, _now()))
        conn.commit()
        row = conn.execute("SELECT id FROM sent_ids WHERE account_id=? AND message_id=?",
                           (int(account_pk), mid)).fetchone()
        return int(row["id"])

    def mark_sent_delivery(self, account_pk: int, message_id: str, delivery: str) -> bool:
        """Stamp a sent id's delivery state (sent, bounced, delayed, read, failed); `sent`
        also records sent_at."""
        mid = str(message_id or "").strip()
        if mid and not mid.startswith("<"):
            mid = f"<{mid}>"
        conn = self._conn()
        if delivery == "sent":
            cur = conn.execute("UPDATE sent_ids SET delivery=?, sent_at=COALESCE(sent_at, ?) WHERE account_id=? AND message_id=?",
                               (delivery, _now(), int(account_pk), mid))
        else:
            cur = conn.execute("UPDATE sent_ids SET delivery=? WHERE account_id=? AND message_id=?",
                               (delivery, int(account_pk), mid))
        conn.commit()
        return cur.rowcount > 0

    def sent_id(self, account_pk: int, message_id: str) -> Optional[Dict[str, Any]]:
        mid = str(message_id or "").strip()
        if mid and not mid.startswith("<"):
            mid = f"<{mid}>"
        row = self._conn().execute(
            "SELECT * FROM sent_ids WHERE account_id=? AND message_id=?", (int(account_pk), mid)).fetchone()
        return dict(row) if row else None

    # ── cases (schema v2): the conversations the agent answers in ───────────

    _CASE_STATUSES = ("open", "held", "answered", "closed")

    def open_case(self, account_pk: int, case_id: str, *, thread_id: Optional[int] = None,
                  correspondent: str = "", contact_id: str = "", subject_norm: str = "",
                  opened_by: str = "inbound", trust_max: str = "", related_case: str = "",
                  related_reason: str = "") -> int:
        """A new case for this account (case_id from vaf/mail/case_token.mint_case_id);
        idempotent on (account, case_id)."""
        conn = self._conn()
        conn.execute(
            "INSERT INTO cases(account_id, case_id, thread_id, correspondent, contact_id, subject_norm, "
            "opened_by, trust_max, related_case, related_reason, opened_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id, case_id) DO NOTHING",
            (int(account_pk), case_id, thread_id, (correspondent or "").strip().lower(), contact_id or "",
             subject_norm or "", opened_by or "inbound", trust_max or "", related_case or "",
             related_reason or "", _now()))
        conn.commit()
        row = conn.execute("SELECT id FROM cases WHERE account_id=? AND case_id=?",
                           (int(account_pk), case_id)).fetchone()
        return int(row["id"])

    @staticmethod
    def _case_row(r: Any) -> Dict[str, Any]:
        d = dict(r)
        try:
            d["extra_participants"] = json.loads(d.get("extra_participants") or "[]")
        except Exception:
            d["extra_participants"] = []
        return d

    def case_by_id(self, account_pk: int, case_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute("SELECT * FROM cases WHERE account_id=? AND case_id=?",
                                   (int(account_pk), str(case_id or ""))).fetchone()
        return self._case_row(row) if row else None

    def case_for_thread(self, account_pk: int, thread_id: int) -> Optional[Dict[str, Any]]:
        """The case riding on a thread: an unclosed one first, else the newest."""
        row = self._conn().execute(
            "SELECT * FROM cases WHERE account_id=? AND thread_id=? "
            "ORDER BY (status='closed') ASC, id DESC LIMIT 1", (int(account_pk), int(thread_id))).fetchone()
        return self._case_row(row) if row else None

    def cases_for_address(self, account_pk: int, address: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM cases WHERE account_id=? AND correspondent=? ORDER BY id DESC LIMIT ?",
            (int(account_pk), (address or "").strip().lower(), max(1, int(limit)))).fetchall()
        return [self._case_row(r) for r in rows]

    def list_cases(self, account_pk: Optional[int] = None, *, status: Optional[str] = None,
                   limit: int = 200) -> List[Dict[str, Any]]:
        where, args = ["1=1"], []
        if account_pk is not None:
            where.append("account_id=?")
            args.append(int(account_pk))
        if status:
            where.append("status=?")
            args.append(status)
        args.append(max(1, min(int(limit), 2000)))
        rows = self._conn().execute(
            f"SELECT * FROM cases WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", args).fetchall()
        return [self._case_row(r) for r in rows]

    def set_case_status(self, account_pk: int, case_id: str, status: str) -> bool:
        if status not in self._CASE_STATUSES:
            raise ValueError(f"not a case status: {status!r}")
        conn = self._conn()
        if status == "closed":
            cur = conn.execute("UPDATE cases SET status=?, closed_at=? WHERE account_id=? AND case_id=?",
                               (status, _now(), int(account_pk), case_id))
        else:
            cur = conn.execute("UPDATE cases SET status=?, closed_at=NULL WHERE account_id=? AND case_id=?",
                               (status, int(account_pk), case_id))
        conn.commit()
        return cur.rowcount > 0

    def set_case_contact(self, account_pk: int, case_id: str, contact_id: str) -> None:
        conn = self._conn()
        conn.execute("UPDATE cases SET contact_id=? WHERE account_id=? AND case_id=?",
                     (contact_id or "", int(account_pk), case_id))
        conn.commit()

    def set_case_thread(self, account_pk: int, case_id: str, thread_id: Optional[int]) -> None:
        conn = self._conn()
        conn.execute("UPDATE cases SET thread_id=? WHERE account_id=? AND case_id=?",
                     (thread_id, int(account_pk), case_id))
        conn.commit()

    def add_case_participant(self, account_pk: int, case_id: str, address: str) -> None:
        """An address the owner added to a case by hand (a colleague in Cc who may write)."""
        case = self.case_by_id(account_pk, case_id)
        if not case:
            return
        addr = (address or "").strip().lower()
        if not addr or addr in case["extra_participants"]:
            return
        conn = self._conn()
        conn.execute("UPDATE cases SET extra_participants=? WHERE id=?",
                     (json.dumps(case["extra_participants"] + [addr]), int(case["id"])))
        conn.commit()

    def touch_case(self, account_pk: int, case_id: str, *, inbound: bool = False, outbound: bool = False,
                   trust: str = "") -> None:
        conn = self._conn()
        sets, args = [], []
        if inbound:
            sets.append("last_inbound_at=?")
            args.append(_now())
        if outbound:
            sets.append("last_outbound_at=?")
            args.append(_now())
        if trust:
            # T4 > T3 > ... as text compares, since every rung is one letter and one digit
            sets.append("trust_max=CASE WHEN trust_max < ? THEN ? ELSE trust_max END")
            args.extend([trust, trust])
        if not sets:
            return
        args.extend([int(account_pk), case_id])
        conn.execute(f"UPDATE cases SET {', '.join(sets)} WHERE account_id=? AND case_id=?", args)
        conn.commit()

    def attach_message_to_case(self, pk: int, case_id: str, *, signal: str = "", certainty: str = "",
                               outcome: str = "", decision: str = "", reason: str = "") -> None:
        """How one inbound message was attributed and what was decided; one row per message,
        the newest decision replacing an older one."""
        conn = self._conn()
        conn.execute(
            "INSERT INTO case_messages(message_pk, case_id, signal, certainty, outcome, decision, reason, decided_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(message_pk) DO UPDATE SET case_id=excluded.case_id, "
            "signal=excluded.signal, certainty=excluded.certainty, outcome=excluded.outcome, "
            "decision=excluded.decision, reason=excluded.reason, decided_at=excluded.decided_at",
            (int(pk), case_id or "", signal or "", certainty or "", outcome or "", decision or "", reason or "", _now()))
        conn.commit()

    def case_message(self, pk: int) -> Optional[Dict[str, Any]]:
        row = self._conn().execute("SELECT * FROM case_messages WHERE message_pk=?", (int(pk),)).fetchone()
        return dict(row) if row else None

    def case_messages(self, pks: Iterable[int]) -> Dict[int, Dict[str, Any]]:
        ids = [int(x) for x in pks]
        out: Dict[int, Dict[str, Any]] = {}
        if not ids:
            return out
        conn = self._conn()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" for _ in chunk)
            for r in conn.execute(f"SELECT * FROM case_messages WHERE message_pk IN ({q})", chunk).fetchall():
                out[int(r["message_pk"])] = dict(r)
        return out

    def case_participants(self, account_pk: int, case_id: str) -> set:
        """Every address on the case: the correspondent, the owner's added participants,
        and every From, To and Cc of the messages on the case's thread (lowercased
        mailboxes). The participant check of the attribution reads this."""
        from vaf.mail.addressing import header_addresses
        case = self.case_by_id(account_pk, case_id)
        if not case:
            return set()
        out = set()
        if case.get("correspondent"):
            out.add(case["correspondent"])
        out.update(a for a in case.get("extra_participants") or [] if a)
        tid = case.get("thread_id")
        if tid is not None:
            for r in self._conn().execute(
                    "SELECT from_addr, to_addrs, cc_addrs FROM messages WHERE thread_id=?", (int(tid),)).fetchall():
                for value in (r["from_addr"], r["to_addrs"], r["cc_addrs"]):
                    out.update(header_addresses(value))
        return out

    def sent_ids_for_case(self, account_pk: int, case_id: str) -> List[Dict[str, Any]]:
        """Every mail VAF sent in a case, oldest first (the first row is the case's root
        anchor, appended to References on every later mail of the case)."""
        rows = self._conn().execute(
            "SELECT * FROM sent_ids WHERE account_id=? AND case_id=? ORDER BY id",
            (int(account_pk), str(case_id or ""))).fetchall()
        return [dict(r) for r in rows]

    def front_office_replies_since(self, account_pk: int, address: str, since_iso: str) -> int:
        """How many Front Office answers went to this address since `since_iso` (the
        rate cap's ledger: sent_ids, no second table). The To header is parsed into
        mailboxes, so ann@example.org never counts a mail to joann@example.org."""
        addr = (address or "").strip().lower()
        if not addr:
            return 0
        rows = self._conn().execute(
            "SELECT to_addrs FROM sent_ids WHERE account_id=? AND sent_by='front_office' "
            "AND enqueued_at >= ? AND delivery != 'discarded'",
            (int(account_pk), since_iso)).fetchall()
        return sum(1 for r in rows if addr in header_addresses(r["to_addrs"]))

    def inbound_from_address_since(self, account_pk: int, address: str, since_ts: int) -> int:
        addr = (address or "").strip().lower()
        if not addr:
            return 0
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM messages m JOIN folders f ON f.id=m.folder_id "
            "WHERE m.account_id=? AND lower(m.from_addr) LIKE ? AND COALESCE(m.date_ts, m.internaldate_ts, 0) >= ? "
            "AND (f.special_use='\\Inbox' OR upper(f.name)='INBOX')",
            (int(account_pk), f"%{addr}%", int(since_ts))).fetchone()
        return int(row["n"] or 0)

    def account_state(self, account_pk: int) -> Dict[str, Any]:
        """The account's own JSON state (the answering lane's cursor lives here)."""
        row = self._conn().execute("SELECT sync_state FROM accounts WHERE id=?", (int(account_pk),)).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row["sync_state"] or "{}") or {}
        except Exception:
            return {}

    def set_account_state(self, account_pk: int, **patch: Any) -> Dict[str, Any]:
        state = self.account_state(account_pk)
        state.update(patch)
        conn = self._conn()
        conn.execute("UPDATE accounts SET sync_state=? WHERE id=?", (json.dumps(state), int(account_pk)))
        conn.commit()
        return state

    def new_inbox_messages(self, account_pk: int, *, after_pk: int, min_date_ts: int = 0,
                           limit: int = 200) -> List[Dict[str, Any]]:
        """The account's inbox messages newer than a cursor (by pk, the ingest order), dated
        at or after `min_date_ts`, oldest first: what the answering lane reads after a sync."""
        rows = self._conn().execute(
            "SELECT m.*, f.name AS folder_name, f.special_use AS folder_special_use FROM messages m "
            "JOIN folders f ON f.id=m.folder_id WHERE m.account_id=? AND m.id > ? "
            "AND (f.special_use='\\Inbox' OR upper(f.name)='INBOX') "
            "AND COALESCE(m.date_ts, m.internaldate_ts, 0) >= ? ORDER BY m.id LIMIT ?",
            (int(account_pk), int(after_pk), int(min_date_ts), max(1, int(limit)))).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["flags"] = json.loads(d.get("flags") or "[]")
            out.append(d)
        return out

    def max_message_pk(self, account_pk: int) -> int:
        row = self._conn().execute("SELECT MAX(id) AS m FROM messages WHERE account_id=?", (int(account_pk),)).fetchone()
        return int(row["m"] or 0)

    def messages_for_verification(self, account_pk: int, *, policy_key: str,
                                  limit: int = 5000) -> List[Dict[str, Any]]:
        """Messages whose verdict is missing or was computed under another policy (a newly
        learned authserv-id), oldest first: id, message_id, from_addr, subject, category,
        body_state and the stored header snapshot (empty for a row that was never assessed,
        which the backfill then re-parses from the cached raw bytes when there are any)."""
        rows = self._conn().execute(
            "SELECT m.id, m.message_id, m.from_addr, m.subject, m.category, m.body_state, "
            "ma.headers AS headers, ma.policy_key AS policy_key FROM messages m "
            "LEFT JOIN message_auth ma ON ma.message_pk=m.id "
            "WHERE m.account_id=? AND (ma.message_pk IS NULL OR ma.policy_key != ?) "
            "ORDER BY m.id LIMIT ?", (int(account_pk), policy_key, max(1, int(limit)))).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["headers"] = json.loads(d.get("headers") or "{}")
            except Exception:
                d["headers"] = {}
            out.append(d)
        return out

    def list_attachments(self, pk: int) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM attachments WHERE message_pk=? ORDER BY id", (pk,)).fetchall()]

    def list_messages(self, account_id: Optional[str] = None, folder: Optional[str] = None,
                      category: Optional[str] = None, limit: int = 50, offset: int = 0,
                      unread_only: bool = False) -> List[Dict[str, Any]]:
        where, args = ["1=1"], []
        if account_id:
            where.append("a.account_id=?")
            args.append(account_id)
        if folder:
            where.append("f.name=?")
            args.append(folder)
        if category:
            if category == "primary":
                # non-Gmail ingest stores '' - both mean primary (review finding)
                where.append("(m.category='' OR m.category='primary')")
            else:
                where.append("m.category=?")
                args.append(category)
        if unread_only:
            where.append("m.flags NOT LIKE '%\\\\Seen%'")
        args.extend([max(1, min(int(limit), 200)), max(0, int(offset))])
        rows = self._conn().execute(
            f"SELECT m.*, a.account_id AS acct, f.name AS folder_name FROM messages m "
            f"JOIN accounts a ON a.id=m.account_id JOIN folders f ON f.id=m.folder_id "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY COALESCE(m.date_ts, m.internaldate_ts, 0) DESC, m.id DESC "
            f"LIMIT ? OFFSET ?", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["flags"] = json.loads(d.get("flags") or "[]")
            out.append(d)
        return out

    def list_threads(self, account_id: Optional[str] = None, folder: Optional[str] = None,
                     limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """Conversation list: one row per thread with its newest message's
        envelope, unread/total counts, cross-folder by design."""
        where, args = ["1=1"], []
        if account_id:
            where.append("a.account_id=?")
            args.append(account_id)
        if folder:
            where.append("t.id IN (SELECT DISTINCT m2.thread_id FROM messages m2 "
                         "JOIN folders f2 ON f2.id=m2.folder_id WHERE f2.name=?)")
            args.append(folder)
        args.extend([max(1, min(int(limit), 200)), max(0, int(offset))])
        rows = self._conn().execute(
            f"SELECT t.id AS thread_id, t.message_count, t.last_date_ts, a.account_id AS acct, "
            f"(SELECT COUNT(*) FROM messages mu WHERE mu.thread_id=t.id "
            f" AND mu.flags NOT LIKE '%\\\\Seen%') AS unread_count, "
            f"(SELECT COUNT(*) FROM messages ma WHERE ma.thread_id=t.id "
            f" AND ma.answered_at IS NOT NULL AND ma.answered_at != '') AS answered, "
            f"m.id AS newest_pk, m.subject, m.from_addr, m.snippet, m.has_attachments, m.flags, "
            f"m.category, "
            # The newest message's identity and folder: what the inbox needs to say whether
            # the last word was the correspondent's (not in Sent) and to hand read_mail its ids.
            f"m.message_id AS newest_message_id, fn.name AS newest_folder, "
            f"fn.special_use AS newest_special_use, m.answered_at AS newest_answered_at, "
            f"m.gm_msgid AS newest_gm_msgid, "
            # The newest message's verdict (schema v2): whether a person wrote it and
            # whether its sender authenticated, so a list row can say so without a
            # second query and the inbox can keep a bounce off "waits for you".
            f"COALESCE(ma.machine_kind, '') AS newest_machine_kind, "
            f"COALESCE(ma.auth_state, 'unknown') AS newest_auth_state "
            f"FROM threads t JOIN accounts a ON a.id=t.account_id "
            f"JOIN messages m ON m.id = (SELECT m3.id FROM messages m3 WHERE m3.thread_id=t.id "
            f"  ORDER BY COALESCE(m3.date_ts, m3.internaldate_ts, 0) DESC, m3.id DESC LIMIT 1) "
            f"JOIN folders fn ON fn.id = m.folder_id "
            f"LEFT JOIN message_auth ma ON ma.message_pk = m.id "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY t.last_date_ts DESC LIMIT ? OFFSET ?", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["flags"] = json.loads(d.get("flags") or "[]")
            out.append(d)
        return out

    def thread_messages(self, thread_id: int) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            # folder_special_use travels because it is the only trustworthy way to
            # tell the user's OWN messages from the correspondent's: a From header
            # is not authenticated, but a message sitting in this mailbox's Sent
            # folder really was sent from it.
            "SELECT m.*, f.name AS folder_name, f.special_use AS folder_special_use "
            "FROM messages m "
            "JOIN folders f ON f.id=m.folder_id WHERE m.thread_id=? "
            "ORDER BY COALESCE(m.date_ts, m.internaldate_ts, 0), m.id", (thread_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["flags"] = json.loads(d.get("flags") or "[]")
            out.append(d)
        return out

    def search(self, query: str, account_id: Optional[str] = None,
               limit: int = 50, folder: Optional[str] = None) -> List[Dict[str, Any]]:
        """FTS5 ranked search over subject/from/to/body. Sanitizes the query into
        a prefix-match form so raw user input cannot break FTS syntax."""
        terms = [t for t in re.findall(r"[\w@.\-]+", query or "", flags=re.UNICODE) if t]
        if not terms:
            return []
        fts_query = " ".join(f'"{t}"*' for t in terms[:8])
        where, args = ["messages_fts MATCH ?"], [fts_query]
        if account_id:
            where.append("a.account_id=?")
            args.append(account_id)
        if folder:
            where.append("f.name=?")
            args.append(folder)
        args.append(max(1, min(int(limit), 200)))
        rows = self._conn().execute(
            f"SELECT m.*, a.account_id AS acct, f.name AS folder_name, bm25(messages_fts) AS rank "
            f"FROM messages_fts JOIN messages m ON m.id=messages_fts.rowid "
            f"JOIN accounts a ON a.id=m.account_id JOIN folders f ON f.id=m.folder_id "
            f"WHERE {' AND '.join(where)} ORDER BY rank LIMIT ?", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["flags"] = json.loads(d.get("flags") or "[]")
            out.append(d)
        return out

    @staticmethod
    def exists(user_scope_id: str, base_dir: Optional[Path] = None) -> bool:
        """Whether this scope has a mail store on disk. Constructing a MailStore creates the
        file and its schema; a read that only wants to know "any mail for this person?"
        asks here first so a glance never materialises an empty database."""
        scope = str(user_scope_id or "").strip()
        if not scope:
            return False
        if base_dir is None:
            from vaf.core.platform import Platform
            base_dir = Platform.data_dir()
        return (Path(base_dir) / "scopes" / scope / "mail.db").exists()

    def messages_for_address(self, address: str, *, before_ts: Optional[float] = None,
                             limit: int = 50) -> List[Dict[str, Any]]:
        """Every message exchanged with one address, newest first: the address as a complete
        mailbox in From, To or Cc (parsed from the stored header strings, so ann@example.com
        never matches joann@example.com), with the folder's special_use so the caller can
        tell the mailbox's own sent mail from the correspondent's. Junk, Trash and Drafts
        are left out; rows without any date are left out because a timeline cannot place
        them. Header-only rows (Sent folders sync without bodies) come back with an empty
        snippet. This is the per-person query: search() is ranked full text over the body
        too and cannot say whether a hit was addressed to or merely mentioned the person."""
        from vaf.mail.addressing import header_addresses
        addr = (address or "").strip().lower()
        if not addr:
            return []
        # LIKE narrows the scan to candidates; the parsed-header comparison below decides,
        # and the limit counts decided rows, so a run of near-miss addresses (joann@ when
        # ann@ is wanted) cannot shorten a page.
        pattern = f"%{addr}%"
        where = ["(lower(m.from_addr) LIKE ? OR lower(m.to_addrs) LIKE ? OR lower(m.cc_addrs) LIKE ?)",
                 "COALESCE(m.date_ts, m.internaldate_ts) IS NOT NULL",
                 "COALESCE(f.special_use, '') NOT IN (?, ?, ?)"]
        args: List[Any] = [pattern, pattern, pattern, "\\Junk", "\\Trash", "\\Drafts"]
        if before_ts is not None:
            where.append("COALESCE(m.date_ts, m.internaldate_ts) <= ?")
            args.append(int(before_ts))
        wanted = max(1, min(int(limit), 200))
        cur = self._conn().execute(
            f"SELECT m.id, m.message_id, m.subject, m.from_addr, m.to_addrs, m.cc_addrs, m.snippet, "
            f"COALESCE(m.date_ts, m.internaldate_ts) AS ts, f.name AS folder_name, f.special_use, "
            f"a.account_id AS acct FROM messages m "
            f"JOIN accounts a ON a.id=m.account_id JOIN folders f ON f.id=m.folder_id "
            f"WHERE {' AND '.join(where)} ORDER BY ts DESC, m.id DESC", args)
        out: List[Dict[str, Any]] = []
        for r in cur:
            if addr in header_addresses(r["from_addr"]) or addr in header_addresses(r["to_addrs"]) \
                    or addr in header_addresses(r["cc_addrs"]):
                out.append(dict(r))
                if len(out) >= wanted:
                    break
        return out

    # ── local writes + op queue (phase 2; EMAIL_CLIENT.md K-9 pattern) ─────

    def set_local_flags(self, pk: int, add: Iterable[str] = (),
                        remove: Iterable[str] = ()) -> List[str]:
        """Local-first flag mutation: updates the LOCAL truth immediately and
        returns the new flag list. server_flags stays untouched - the replay
        executor diffs local vs server_flags and pushes only the delta."""
        conn = self._conn()
        row = conn.execute("SELECT flags FROM messages WHERE id=?", (pk,)).fetchone()
        if not row:
            return []
        flags = set(json.loads(row["flags"] or "[]"))
        flags |= set(add)
        flags -= set(remove)
        out = sorted(flags)
        conn.execute("UPDATE messages SET flags=? WHERE id=?", (json.dumps(out), pk))
        conn.commit()
        return out

    def enqueue_op(self, account_pk: int, kind: str, payload: Dict[str, Any],
                   not_before_ts: Optional[int] = None, state: str = "pending") -> int:
        """Durable idempotent operation for server replay. kinds: flags, move,
        append, send. not_before_ts delays execution (undo-send window). `state`
        `held` parks a send for the person's approval: the drain reads pending ops only,
        so a held op never leaves until approve_op turns it pending."""
        conn = self._conn()
        body = dict(payload)
        if not_before_ts is not None:
            body["not_before_ts"] = int(not_before_ts)
        cur = conn.execute(
            "INSERT INTO ops(account_id, kind, payload, state, created_at) VALUES(?,?,?,?,?)",
            (account_pk, kind, json.dumps(body), state if state in ("pending", "held") else "pending", _now()))
        conn.commit()
        return int(cur.lastrowid)

    # ── held sends: a draft the person approves or discards ─────────────────

    def held_ops(self, account_pk: Optional[int] = None, *, thread_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Held send ops (drafts awaiting approval), newest first, payload decoded;
        narrowed to one account and/or one thread (the payload's thread_id)."""
        conn = self._conn()
        where, args = ["kind='send'", "state='held'"], []
        if account_pk is not None:
            where.append("account_id=?")
            args.append(int(account_pk))
        if thread_id is not None:
            where.append("json_extract(payload, '$.thread_id')=?")
            args.append(int(thread_id))
        rows = conn.execute(f"SELECT * FROM ops WHERE {' AND '.join(where)} ORDER BY id DESC", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"] or "{}")
            out.append(d)
        return out

    def approve_op(self, op_id: int, *, not_before_ts: Optional[int] = None) -> bool:
        """A held send becomes pending, runnable now (or after not_before_ts): the person
        approved the draft. Only a held op can be approved. The attempt counter starts over:
        MAX_ATTEMPTS bounds what the SWEEP tries on its own after one approval, and a draft
        that came back to the person after a failed attempt (`release_held_draft`) would
        otherwise run out of tries across their approvals and fail before it was even tried."""
        conn = self._conn()
        now = int(not_before_ts if not_before_ts is not None
                  else datetime.now(timezone.utc).timestamp())
        cur = conn.execute(
            # `last_error` goes with the attempt count: it described the try that failed, and a
            # released op carrying it reports a stale failure on a send that is on its way
            # (`send_outcome` reads the payload).
            "UPDATE ops SET state='pending', attempts=0, updated_at=?, "
            "payload=json_remove(json_set(payload, '$.not_before_ts', ?), '$.last_error') "
            "WHERE id=? AND state='held' AND kind='send'", (_now(), now, int(op_id)))
        conn.commit()
        return cur.rowcount == 1

    def discard_op(self, op_id: int, *, replaced_by: str = "") -> bool:
        """A held send is discarded: the person did not want it. Only a held op.

        `replaced_by` names the newer draft that took its place (`mail:13`), for a draft the
        agent rewrote before anybody decided on it. It is still a discard, so every reader
        of the op state keeps its meaning; the name only says why."""
        conn = self._conn()
        if replaced_by:
            cur = conn.execute(
                "UPDATE ops SET state='discarded', updated_at=?, "
                "payload=json_set(payload, '$.replaced_by', ?) WHERE id=? AND state='held'",
                (_now(), str(replaced_by), int(op_id)))
        else:
            cur = conn.execute(
                "UPDATE ops SET state='discarded', updated_at=? WHERE id=? AND state='held'",
                (_now(), int(op_id)))
        conn.commit()
        return cur.rowcount == 1

    def revise_held_op(self, op_id: int, *, subject: str, body: str, raw_b64: str) -> bool:
        """A held send gets new words before anybody sent it. Only a held op: a released one
        is on its way and its bytes are the ones that leave. The subject and the text travel
        twice, as payload fields (an API sender builds from them) and inside the stored
        RFC822 bytes (the SMTP sender and the Sent copy use those), so both change in one
        UPDATE. `edited` marks the draft for the card and for the agent's next turn."""
        conn = self._conn()
        cur = conn.execute(
            "UPDATE ops SET updated_at=?, payload=json_set(payload, '$.subject', ?, '$.body', ?, "
            "'$.raw_b64', ?, '$.edited', json('true')) WHERE id=? AND state='held' AND kind='send'",
            (_now(), subject, body, raw_b64, int(op_id)))
        conn.commit()
        return cur.rowcount == 1

    def chat_send_ops(self, chat_session_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Every send one chat asked for, in any state, newest first, payload decoded.

        The chat card shows a draft after the decision too (sent, discarded, replaced), as the
        record of what happened to it; `held_ops` lists only what still waits."""
        sid = str(chat_session_id or "").strip()
        if not sid:
            return []
        rows = self._conn().execute(
            "SELECT * FROM ops WHERE kind='send' AND json_extract(payload, '$.chat_session_id')=? "
            "ORDER BY id DESC LIMIT ?", (sid, max(1, int(limit)))).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"] or "{}")
            out.append(d)
        return out

    def pending_ops(self, account_pk: Optional[int] = None,
                    now_ts: Optional[int] = None) -> List[Dict[str, Any]]:
        """Pending ops ready to run (not_before_ts respected), oldest first."""
        conn = self._conn()
        if account_pk is None:
            rows = conn.execute(
                "SELECT * FROM ops WHERE state='pending' ORDER BY id").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ops WHERE state='pending' AND account_id=? ORDER BY id",
                (account_pk,)).fetchall()
        now = int(now_ts if now_ts is not None
                  else datetime.now(timezone.utc).timestamp())
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"] or "{}")
            if int(d["payload"].get("not_before_ts") or 0) <= now:
                out.append(d)
        return out

    def get_op(self, op_id: int) -> Optional[Dict[str, Any]]:
        row = self._conn().execute("SELECT * FROM ops WHERE id=?", (op_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"] or "{}")
        return d

    def claim_op(self, op_id: int) -> bool:
        """Atomically move a pending op to 'sending'. Exactly one racing worker
        wins under SQLite's single-writer lock (the loser gets rowcount 0 and
        must skip), so a non-idempotent side effect (send) runs at most once.
        attempts is incremented HERE (at claim), so even a worker that crashes
        before mark_op still counts toward MAX_ATTEMPTS."""
        conn = self._conn()
        cur = conn.execute(
            "UPDATE ops SET state='sending', attempts=attempts+1, updated_at=? "
            "WHERE id=? AND state='pending'", (_now(), op_id))
        conn.commit()
        return cur.rowcount == 1

    def mark_op(self, op_id: int, state: str, error: Optional[str] = None,
                expect_state: Optional[str] = None) -> bool:
        """Transition an op's state. With expect_state set, the UPDATE only
        applies when the op is still in that state (guards against clobbering a
        row another actor changed meanwhile, e.g. overwriting 'cancelled' with
        'done'). attempts is NOT incremented here - that happens at claim_op."""
        conn = self._conn()
        payload_patch = ""
        args: List[Any] = [state, _now()]
        if error is not None:
            payload_patch = ", payload=json_set(payload, '$.last_error', ?)"
            args.append(error[:500])
        args.append(op_id)
        guard = ""
        if expect_state is not None:
            guard = " AND state=?"
            args.append(expect_state)
        cur = conn.execute(
            f"UPDATE ops SET state=?, updated_at=?{payload_patch} "
            f"WHERE id=?{guard}", args)
        conn.commit()
        return cur.rowcount > 0

    def reclaim_stale_ops(self, account_pk: int, lease_seconds: int = 300) -> int:
        """Re-arm ops stranded in 'sending' by a crashed/interrupted worker
        (updated_at older than the lease). Idempotent kinds (flags/move/append)
        go back to 'pending' for a safe retry; 'send' is PARKED as 'failed'
        (SMTP has no idempotency key - an interrupted send may already have been
        delivered, so it must never be auto-retried). Returns reclaimed count."""
        conn = self._conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=lease_seconds)).isoformat()
        rows = conn.execute(
            "SELECT id, kind FROM ops WHERE account_id=? AND state='sending' "
            "AND updated_at IS NOT NULL AND updated_at < ?",
            (account_pk, cutoff)).fetchall()
        for r in rows:
            if r["kind"] == "send":
                conn.execute(
                    "UPDATE ops SET state='failed', updated_at=?, "
                    "payload=json_set(payload, '$.last_error', ?) WHERE id=?",
                    (_now(), "interrupted mid-send; not auto-retried", int(r["id"])))
            else:
                conn.execute("UPDATE ops SET state='pending', updated_at=? WHERE id=?",
                             (_now(), int(r["id"])))
        conn.commit()
        return len(rows)

    def cancel_op(self, op_id: int, account_pk: Optional[int] = None) -> bool:
        """Cancel a pending op (undo-send). Only pending ops can be cancelled."""
        conn = self._conn()
        args: List[Any] = [_now(), op_id]
        acct_clause = ""
        if account_pk is not None:
            acct_clause = " AND account_id=?"
            args.append(account_pk)
        cur = conn.execute(
            f"UPDATE ops SET state='cancelled', updated_at=? "
            f"WHERE id=? AND state='pending'{acct_clause}", args)
        conn.commit()
        return cur.rowcount > 0

    def move_message_local(self, pk: int, dest_folder_pk: int) -> bool:
        """Local-first move: the row moves to the destination folder now; the
        server uid becomes unknown (NULL) until the replay executor re-syncs.
        Threads are cross-folder by design, so thread linkage is untouched."""
        conn = self._conn()
        cur = conn.execute(
            "UPDATE messages SET folder_id=?, uid=NULL WHERE id=?",
            (dest_folder_pk, pk))
        conn.commit()
        return cur.rowcount > 0

    def find_special_folder(self, account_pk: int, special_use: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM folders WHERE account_id=? AND special_use=?",
            (account_pk, special_use)).fetchone()
        return dict(row) if row else None

    # ── retention (decision E5) ────────────────────────────────────────────

    def evict_old_bodies(self, keep_days: int) -> int:
        """Retention applies to CACHED BODIES only - envelopes/headers stay
        forever (decision E5). Returns evicted blob count."""
        cutoff = int(datetime.now(timezone.utc).timestamp()) - keep_days * 86400
        conn = self._conn()
        # NULL dates stay cached (undated mail must not be treated as epoch-old)
        rows = conn.execute(
            "SELECT message_pk FROM message_raw JOIN messages m ON m.id=message_raw.message_pk "
            "WHERE COALESCE(m.date_ts, m.internaldate_ts) < ?", (cutoff,)).fetchall()
        for r in rows:
            conn.execute("DELETE FROM message_raw WHERE message_pk=?", (int(r["message_pk"]),))
            conn.execute("UPDATE messages SET body_state='none' WHERE id=?", (int(r["message_pk"]),))
        conn.commit()
        return len(rows)

    def maybe_evict_old_bodies(self, keep_days: int) -> int:
        """Run retention at most once per ~20h (marker in schema_meta); called
        from the supervisor sweep so mail_body_retention_days actually acts."""
        conn = self._conn()
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='retention_last_run'").fetchone()
        if row:
            try:
                last = datetime.fromisoformat(str(row["value"]))
                if (datetime.now(timezone.utc) - last).total_seconds() < 20 * 3600:
                    return 0
            except Exception:
                pass
        evicted = self.evict_old_bodies(keep_days)
        conn.execute("INSERT INTO schema_meta(key, value) VALUES('retention_last_run', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_now(),))
        conn.commit()
        return evicted

    # ── counters ────────────────────────────────────────────────────────────

    def counts(self, account_id: Optional[str] = None) -> Dict[str, int]:
        where, args = "", []
        if account_id:
            where = "WHERE a.account_id=?"
            args = [account_id]
        row = self._conn().execute(
            f"SELECT COUNT(*) AS total, "
            f"SUM(CASE WHEN m.flags NOT LIKE '%\\\\Seen%' THEN 1 ELSE 0 END) AS unread "
            f"FROM messages m JOIN accounts a ON a.id=m.account_id {where}", args).fetchone()
        return {"total": int(row["total"] or 0), "unread": int(row["unread"] or 0)}
