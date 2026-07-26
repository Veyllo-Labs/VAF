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
import json
import re
import sqlite3
import threading as _threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from vaf.mail.parser import ParsedMessage

SCHEMA_VERSION = 1
RAW_CACHE_MAX_BYTES = 256 * 1024
SNIPPET_CHARS = 240

_RE_SUBJECT_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|wg|sv|antw)(\[\d+\])?:\s*)+", re.IGNORECASE)


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
        # future migrations: if version < SCHEMA_VERSION: migrate stepwise here

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
        CREATE VIRTUAL TABLE messages_fts USING fts5(
          subject, from_addr, to_addrs, body_text,
          content='', contentless_delete=1,
          tokenize="unicode61 remove_diacritics 2"
        );
        """)
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
                       category: str = "") -> int:
        """Insert or update one message; updates FTS, attachments, raw blob and
        thread linkage in the same transaction (index desync is structurally
        impossible - the Gloda lesson)."""
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
            f"m.category "
            f"FROM threads t JOIN accounts a ON a.id=t.account_id "
            f"JOIN messages m ON m.id = (SELECT m3.id FROM messages m3 WHERE m3.thread_id=t.id "
            f"  ORDER BY COALESCE(m3.date_ts, m3.internaldate_ts, 0) DESC, m3.id DESC LIMIT 1) "
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
                   not_before_ts: Optional[int] = None) -> int:
        """Durable idempotent operation for server replay. kinds: flags, move,
        append, send. not_before_ts delays execution (undo-send window)."""
        conn = self._conn()
        body = dict(payload)
        if not_before_ts is not None:
            body["not_before_ts"] = int(not_before_ts)
        cur = conn.execute(
            "INSERT INTO ops(account_id, kind, payload, created_at) VALUES(?,?,?,?)",
            (account_pk, kind, json.dumps(body), _now()))
        conn.commit()
        return int(cur.lastrowid)

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
