# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Bi-directional sync orchestrator for cloud storage providers.

Performs three-way diff (remote vs manifest vs local) to determine
which files need uploading, downloading, or deleting. Supports two
conflict strategies: last_write_wins (newer timestamp wins) and
keep_both (rename local copy, download remote).
"""

import hashlib
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from vaf.cloud.base import (
    CloudFileMetadata,
    CloudProvider,
    SyncResult,
    is_syncable,
)
from vaf.cloud.sync_manifest import SyncManifest

logger = logging.getLogger("vaf.cloud.sync_engine")


def _md5_hash(filepath: Path) -> Optional[str]:
    """Compute MD5 hex digest for a local file. Returns None on error."""
    try:
        h = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as exc:
        logger.warning("Failed to hash %s: %s", filepath, exc)
        return None


def _keep_both_rename(local_path: Path) -> Path:
    """Generate a conflict-renamed path like 'file (conflict).txt'."""
    stem = local_path.stem
    suffix = local_path.suffix
    parent = local_path.parent
    conflict_path = parent / f"{stem} (conflict){suffix}"
    counter = 1
    while conflict_path.exists():
        counter += 1
        conflict_path = parent / f"{stem} (conflict {counter}){suffix}"
    return conflict_path


class SyncEngine:
    """Bi-directional sync orchestrator between a cloud provider and the local filesystem."""

    def __init__(
        self,
        provider: CloudProvider,
        manifest: SyncManifest,
        local_sync_dir: Path,
        max_file_size: int,
        conflict_strategy: str = "last_write_wins",
    ):
        self.provider = provider
        self.manifest = manifest
        self.local_sync_dir = local_sync_dir
        self.max_file_size = max_file_size
        if conflict_strategy not in ("last_write_wins", "keep_both"):
            raise ValueError(f"Unknown conflict strategy: {conflict_strategy}")
        self.conflict_strategy = conflict_strategy

    # ── Public API ────────────────────────────────────────────────────────

    def full_sync(self) -> SyncResult:
        """Complete bi-directional sync cycle.

        1. Ensure sync folder exists in cloud
        2. Get remote files (delta if supported, full list otherwise)
        3. Scan local sync directory
        4. Three-way diff: remote vs manifest vs local
        5. Execute actions (download/upload/delete)
        6. Update manifest
        7. Return SyncResult stats
        """
        result = SyncResult()

        # 1. Ensure sync folder exists in cloud
        try:
            sync_folder_id = self.provider.ensure_sync_folder()
            self.manifest.set_sync_folder_id(sync_folder_id)
        except Exception as exc:
            logger.error("Failed to ensure sync folder: %s", exc)
            result.errors += 1
            return result

        # 2. Get remote files
        remote_files = self._get_remote_files()
        if remote_files is None:
            result.errors += 1
            return result

        # 3. Scan local sync directory
        local_files = self._scan_local_files()

        # 4. Three-way diff
        actions = self._three_way_diff(remote_files, local_files)

        # 5. Execute actions
        for action_type, payload in actions:
            try:
                if action_type == "download":
                    self._execute_download(payload, result)
                elif action_type == "upload":
                    self._execute_upload(payload, result)
                elif action_type == "delete_local":
                    self._execute_delete_local(payload, result)
                elif action_type == "delete_remote":
                    self._execute_delete_remote(payload, result)
                elif action_type == "conflict":
                    self._execute_conflict(payload, result)
            except Exception as exc:
                logger.error("Sync action %s failed for %s: %s", action_type, payload.get("remote_path", "?"), exc)
                result.errors += 1

        logger.info(
            "Sync complete: up=%d down=%d del_local=%d del_remote=%d conflicts=%d errors=%d skipped=%d",
            result.uploaded, result.downloaded, result.deleted_local,
            result.deleted_remote, result.conflicts, result.errors, result.skipped,
        )
        return result

    # ── Remote file retrieval ─────────────────────────────────────────────

    def _get_remote_files(self) -> Optional[Dict[str, CloudFileMetadata]]:
        """Fetch remote file listing. Returns dict keyed by remote_path, or None on error."""
        try:
            if self.provider.supports_delta:
                return self._get_remote_files_delta()
            files = self.provider.list_files("/")
            return {f.path: f for f in files if not f.is_folder}
        except Exception as exc:
            logger.error("Failed to list remote files: %s", exc)
            return None

    def _get_remote_files_delta(self) -> Dict[str, CloudFileMetadata]:
        """Use delta/changes API for incremental sync."""
        cursor = self.manifest.get_cursor()
        all_files: Dict[str, CloudFileMetadata] = {}

        # If no cursor, start from full list
        if not cursor:
            files = self.provider.list_files("/")
            return {f.path: f for f in files if not f.is_folder}

        page = self.provider.get_changes(cursor)
        for f in page.files:
            if not f.is_folder:
                all_files[f.path] = f
        # Handle deleted files by marking them
        for deleted_id in page.deleted_ids:
            manifest_entry = self.manifest.get_file(deleted_id)
            if manifest_entry:
                # Use a sentinel to indicate deletion
                all_files[manifest_entry["remote_path"]] = None  # type: ignore[assignment]

        if page.cursor:
            self.manifest.set_cursor(page.cursor)

        while page.has_more:
            page = self.provider.get_changes(page.cursor)
            for f in page.files:
                if not f.is_folder:
                    all_files[f.path] = f
            for deleted_id in page.deleted_ids:
                manifest_entry = self.manifest.get_file(deleted_id)
                if manifest_entry:
                    all_files[manifest_entry["remote_path"]] = None  # type: ignore[assignment]
            if page.cursor:
                self.manifest.set_cursor(page.cursor)

        return all_files

    # ── Local file scanning ───────────────────────────────────────────────

    def _scan_local_files(self) -> Dict[str, Path]:
        """Scan local sync directory. Returns dict of relative_path -> absolute_path."""
        local_files: Dict[str, Path] = {}
        if not self.local_sync_dir.is_dir():
            self.local_sync_dir.mkdir(parents=True, exist_ok=True)
            return local_files

        for filepath in self.local_sync_dir.rglob("*"):
            if filepath.is_dir():
                continue
            if not is_syncable(filepath, self.max_file_size):
                continue
            rel = filepath.relative_to(self.local_sync_dir).as_posix()
            local_files[rel] = filepath

        return local_files

    # ── Three-way diff ────────────────────────────────────────────────────

    def _three_way_diff(
        self,
        remote_files: Dict[str, CloudFileMetadata],
        local_files: Dict[str, Path],
    ) -> List[Tuple[str, Dict]]:
        """Compare remote, manifest, and local state to produce a list of actions.

        Actions:
        - ("download", {...})     -- remote new or remote changed
        - ("upload", {...})       -- local new or local changed
        - ("delete_local", {...}) -- in manifest, gone from remote
        - ("delete_remote", {...})-- in manifest, gone from local
        - ("conflict", {...})     -- both changed
        """
        actions: List[Tuple[str, Dict]] = []
        manifest_files = {f["remote_path"]: f for f in self.manifest.get_all_files()}

        all_paths: Set[str] = set()
        all_paths.update(remote_files.keys())
        all_paths.update(local_files.keys())
        all_paths.update(manifest_files.keys())

        for rel_path in sorted(all_paths):
            remote = remote_files.get(rel_path)
            local_path = local_files.get(rel_path)
            manifest_entry = manifest_files.get(rel_path)

            in_remote = rel_path in remote_files and remote is not None
            in_local = local_path is not None
            in_manifest = manifest_entry is not None

            # Delta deletions: remote is explicitly None (sentinel)
            remote_deleted = rel_path in remote_files and remote is None

            if remote_deleted and in_manifest:
                # Remote was explicitly deleted (delta API)
                actions.append(("delete_local", {
                    "remote_path": rel_path,
                    "manifest": manifest_entry,
                }))
                continue

            if in_remote and not in_manifest and not in_local:
                # Remote new -- download
                actions.append(("download", {
                    "remote_path": rel_path,
                    "remote_file": remote,
                }))

            elif not in_remote and not in_manifest and in_local:
                # Local new -- upload
                actions.append(("upload", {
                    "remote_path": rel_path,
                    "local_path": local_path,
                }))

            elif in_remote and in_manifest and in_local:
                # File exists everywhere -- check for changes
                remote_changed = self._remote_changed(remote, manifest_entry)
                local_changed = self._local_changed(local_path, manifest_entry)

                if remote_changed and local_changed:
                    # Both changed -- conflict
                    actions.append(("conflict", {
                        "remote_path": rel_path,
                        "remote_file": remote,
                        "local_path": local_path,
                        "manifest": manifest_entry,
                    }))
                elif remote_changed:
                    actions.append(("download", {
                        "remote_path": rel_path,
                        "remote_file": remote,
                        "manifest": manifest_entry,
                    }))
                elif local_changed:
                    actions.append(("upload", {
                        "remote_path": rel_path,
                        "local_path": local_path,
                        "manifest": manifest_entry,
                    }))
                # else: no changes, skip

            elif in_manifest and not in_remote and in_local:
                # In manifest but gone from remote -- delete local
                actions.append(("delete_local", {
                    "remote_path": rel_path,
                    "manifest": manifest_entry,
                }))

            elif in_manifest and in_remote and not in_local:
                # In manifest but gone from local -- delete remote
                actions.append(("delete_remote", {
                    "remote_path": rel_path,
                    "remote_file": remote,
                    "manifest": manifest_entry,
                }))

            elif in_remote and not in_manifest and in_local:
                # Both new (not in manifest) -- treat as conflict
                actions.append(("conflict", {
                    "remote_path": rel_path,
                    "remote_file": remote,
                    "local_path": local_path,
                }))

        return actions

    def _remote_changed(self, remote: CloudFileMetadata, manifest_entry: Dict) -> bool:
        """Check if the remote file differs from what was last synced."""
        # Prefer content hash comparison
        if remote.content_hash and manifest_entry.get("content_hash"):
            return remote.content_hash != manifest_entry["content_hash"]
        # Fall back to etag
        if remote.etag and manifest_entry.get("etag"):
            return remote.etag != manifest_entry["etag"]
        # Fall back to mtime
        remote_mtime = remote.modified_time or 0
        manifest_mtime = manifest_entry.get("remote_mtime") or 0
        return abs(remote_mtime - manifest_mtime) > 1.0

    def _local_changed(self, local_path: Path, manifest_entry: Dict) -> bool:
        """Check if the local file has been modified since last sync."""
        try:
            current_mtime = local_path.stat().st_mtime
        except OSError:
            return False
        manifest_mtime = manifest_entry.get("local_mtime") or 0
        return abs(current_mtime - manifest_mtime) > 1.0

    # ── Action executors ──────────────────────────────────────────────────

    def _execute_download(self, payload: Dict, result: SyncResult) -> None:
        """Download a file from cloud to local."""
        remote_file: CloudFileMetadata = payload["remote_file"]
        rel_path = payload["remote_path"]
        local_path = self.local_sync_dir / rel_path

        local_path.parent.mkdir(parents=True, exist_ok=True)

        logger.debug("Downloading: %s", rel_path)
        self.provider.download_file(remote_file.file_id, local_path)

        local_hash = _md5_hash(local_path)
        try:
            local_mtime = local_path.stat().st_mtime
        except OSError:
            local_mtime = time.time()

        self.manifest.upsert_file(
            file_id=remote_file.file_id,
            remote_path=rel_path,
            local_path=str(local_path),
            content_hash=remote_file.content_hash or local_hash,
            etag=remote_file.etag,
            size=remote_file.size,
            remote_mtime=remote_file.modified_time,
            local_mtime=local_mtime,
            status="synced",
        )
        result.downloaded += 1

    def _execute_upload(self, payload: Dict, result: SyncResult) -> None:
        """Upload a local file to cloud."""
        local_path: Path = payload["local_path"]
        rel_path = payload["remote_path"]

        if not is_syncable(local_path, self.max_file_size):
            logger.debug("Skipping non-syncable file: %s", rel_path)
            result.skipped += 1
            return

        logger.debug("Uploading: %s", rel_path)
        remote_meta = self.provider.upload_file(local_path, rel_path)

        local_hash = _md5_hash(local_path)
        try:
            local_mtime = local_path.stat().st_mtime
        except OSError:
            local_mtime = time.time()

        self.manifest.upsert_file(
            file_id=remote_meta.file_id,
            remote_path=rel_path,
            local_path=str(local_path),
            content_hash=remote_meta.content_hash or local_hash,
            etag=remote_meta.etag,
            size=remote_meta.size,
            remote_mtime=remote_meta.modified_time,
            local_mtime=local_mtime,
            status="synced",
        )
        result.uploaded += 1

    def _execute_delete_local(self, payload: Dict, result: SyncResult) -> None:
        """Delete a local file that was removed from cloud."""
        manifest_entry = payload["manifest"]
        local_path = Path(manifest_entry["local_path"])

        if local_path.exists():
            logger.debug("Deleting local: %s", local_path)
            try:
                local_path.unlink()
            except OSError as exc:
                logger.warning("Failed to delete local file %s: %s", local_path, exc)
                result.errors += 1
                return

        self.manifest.delete_file(manifest_entry["file_id"])
        result.deleted_local += 1

    def _execute_delete_remote(self, payload: Dict, result: SyncResult) -> None:
        """Delete a remote file that was removed locally."""
        manifest_entry = payload["manifest"]
        remote_file = payload.get("remote_file")
        file_id = remote_file.file_id if remote_file else manifest_entry["file_id"]

        logger.debug("Deleting remote: %s (id=%s)", payload["remote_path"], file_id)
        deleted = self.provider.delete_file(file_id)
        if not deleted:
            logger.warning("Remote delete returned False for %s", file_id)

        self.manifest.delete_file(manifest_entry["file_id"])
        result.deleted_remote += 1

    def _execute_conflict(self, payload: Dict, result: SyncResult) -> None:
        """Handle a conflict where both remote and local have changed."""
        remote_file: CloudFileMetadata = payload["remote_file"]
        local_path: Path = payload["local_path"]
        rel_path = payload["remote_path"]
        manifest_entry = payload.get("manifest")

        if self.conflict_strategy == "last_write_wins":
            self._resolve_last_write_wins(remote_file, local_path, rel_path, manifest_entry, result)
        elif self.conflict_strategy == "keep_both":
            self._resolve_keep_both(remote_file, local_path, rel_path, manifest_entry, result)

        result.conflicts += 1

    def _resolve_last_write_wins(
        self,
        remote_file: CloudFileMetadata,
        local_path: Path,
        rel_path: str,
        manifest_entry: Optional[Dict],
        result: SyncResult,
    ) -> None:
        """Newer timestamp wins. Remote wins ties."""
        remote_mtime = remote_file.modified_time or 0
        try:
            local_mtime = local_path.stat().st_mtime
        except OSError:
            local_mtime = 0

        if remote_mtime >= local_mtime:
            # Remote wins -- download
            self._execute_download({
                "remote_file": remote_file,
                "remote_path": rel_path,
                "manifest": manifest_entry,
            }, result)
            # Compensate: download already incremented downloaded, but we also counted as conflict
            result.downloaded -= 1
        else:
            # Local wins -- upload
            self._execute_upload({
                "local_path": local_path,
                "remote_path": rel_path,
                "manifest": manifest_entry,
            }, result)
            result.uploaded -= 1

    def _resolve_keep_both(
        self,
        remote_file: CloudFileMetadata,
        local_path: Path,
        rel_path: str,
        manifest_entry: Optional[Dict],
        result: SyncResult,
    ) -> None:
        """Keep both versions: rename local file, download remote."""
        # Rename local copy
        conflict_path = _keep_both_rename(local_path)
        try:
            local_path.rename(conflict_path)
            logger.info("Conflict: renamed local %s -> %s", local_path.name, conflict_path.name)
        except OSError as exc:
            logger.error("Failed to rename conflict file %s: %s", local_path, exc)
            # Mark as conflict in manifest so user can resolve manually
            if manifest_entry:
                self.manifest.upsert_file(
                    file_id=manifest_entry["file_id"],
                    remote_path=rel_path,
                    local_path=str(local_path),
                    content_hash=manifest_entry.get("content_hash"),
                    etag=manifest_entry.get("etag"),
                    size=manifest_entry.get("size", 0),
                    remote_mtime=manifest_entry.get("remote_mtime"),
                    local_mtime=manifest_entry.get("local_mtime"),
                    status="conflict",
                )
            return

        # Download remote to original path
        self._execute_download({
            "remote_file": remote_file,
            "remote_path": rel_path,
            "manifest": manifest_entry,
        }, result)
        result.downloaded -= 1  # Compensate: counted as conflict, not download
