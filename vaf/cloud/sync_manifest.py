"""
SQLite-backed sync manifest — tracks file state per user per provider.

One database file per (username, provider) pair, stored at:
    ~/.vaf/users/{username}/cloud_sync_{provider}.db
"""

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from vaf.core.platform import Platform

logger = logging.getLogger("vaf.cloud.manifest")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS sync_files (
    file_id       TEXT    NOT NULL PRIMARY KEY,
    remote_path   TEXT    NOT NULL,
    local_path    TEXT    NOT NULL,
    content_hash  TEXT,
    etag          TEXT,
    size          INTEGER DEFAULT 0,
    remote_mtime  REAL,
    local_mtime   REAL,
    last_synced   REAL,
    status        TEXT    DEFAULT 'synced'
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class SyncManifest:
    """Thread-safe SQLite manifest for tracking synced file state."""

    def __init__(self, username: str, provider: str):
        self.username = username
        self.provider = provider
        self._lock = threading.Lock()
        self._db_path = self._resolve_path()
        self._ensure_db()

    def _resolve_path(self) -> Path:
        base = Platform.data_dir() / "users" / self.username
        base.mkdir(parents=True, exist_ok=True)
        return base / f"cloud_sync_{self.provider}.db"

    def _ensure_db(self) -> None:
        with self._lock:
            con = sqlite3.connect(str(self._db_path))
            try:
                con.executescript(_CREATE_SQL)
                con.commit()
            finally:
                con.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self._db_path))
        con.row_factory = sqlite3.Row
        return con

    # ── File tracking ────────────────────────────────────────────────────

    def get_file(self, file_id: str) -> Optional[Dict]:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute("SELECT * FROM sync_files WHERE file_id = ?", (file_id,)).fetchone()
                return dict(row) if row else None
            finally:
                con.close()

    def get_file_by_remote_path(self, remote_path: str) -> Optional[Dict]:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute("SELECT * FROM sync_files WHERE remote_path = ?", (remote_path,)).fetchone()
                return dict(row) if row else None
            finally:
                con.close()

    def get_all_files(self) -> List[Dict]:
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute("SELECT * FROM sync_files").fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()

    def upsert_file(
        self,
        file_id: str,
        remote_path: str,
        local_path: str,
        content_hash: Optional[str] = None,
        etag: Optional[str] = None,
        size: int = 0,
        remote_mtime: Optional[float] = None,
        local_mtime: Optional[float] = None,
        status: str = "synced",
    ) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    """INSERT INTO sync_files
                       (file_id, remote_path, local_path, content_hash, etag, size,
                        remote_mtime, local_mtime, last_synced, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(file_id) DO UPDATE SET
                           remote_path = excluded.remote_path,
                           local_path  = excluded.local_path,
                           content_hash = excluded.content_hash,
                           etag        = excluded.etag,
                           size        = excluded.size,
                           remote_mtime = excluded.remote_mtime,
                           local_mtime = excluded.local_mtime,
                           last_synced = excluded.last_synced,
                           status      = excluded.status
                    """,
                    (file_id, remote_path, local_path, content_hash, etag, size,
                     remote_mtime, local_mtime, time.time(), status),
                )
                con.commit()
            finally:
                con.close()

    def delete_file(self, file_id: str) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute("DELETE FROM sync_files WHERE file_id = ?", (file_id,))
                con.commit()
            finally:
                con.close()

    def get_pending_uploads(self) -> List[Dict]:
        return self._get_by_status("pending_upload")

    def get_pending_downloads(self) -> List[Dict]:
        return self._get_by_status("pending_download")

    def get_conflicts(self) -> List[Dict]:
        return self._get_by_status("conflict")

    def _get_by_status(self, status: str) -> List[Dict]:
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute("SELECT * FROM sync_files WHERE status = ?", (status,)).fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()

    # ── Key-value state ──────────────────────────────────────────────────

    def get_state(self, key: str) -> Optional[str]:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
                return row["value"] if row else None
            finally:
                con.close()

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    "INSERT INTO sync_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
                con.commit()
            finally:
                con.close()

    def get_cursor(self) -> Optional[str]:
        return self.get_state("delta_cursor")

    def set_cursor(self, cursor: str) -> None:
        self.set_state("delta_cursor", cursor)

    def get_sync_folder_id(self) -> Optional[str]:
        return self.get_state("sync_folder_id")

    def set_sync_folder_id(self, folder_id: str) -> None:
        self.set_state("sync_folder_id", folder_id)
