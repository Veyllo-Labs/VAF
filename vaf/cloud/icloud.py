"""
iCloud Drive cloud provider using the local filesystem.

macOS only. Apple's system daemon (brctl / bird) handles the actual cloud
synchronisation transparently. This provider reads and writes to the local
iCloud Drive directory:

    ~/Library/Mobile Documents/com~apple~CloudDocs/

Does not support delta sync — full directory listings are used.
"""

import hashlib
import logging
import mimetypes
import shutil
import sys
import time
from pathlib import Path
from typing import List, Optional

from vaf.cloud.base import (
    AuthMethod,
    CloudFileMetadata,
    CloudProvider,
    SYNC_FOLDER_NAME,
)

logger = logging.getLogger("vaf.cloud.icloud")

ICLOUD_BASE = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"


class ICloudProvider(CloudProvider):
    """iCloud Drive provider backed by the local filesystem on macOS."""

    provider_name = "icloud"
    auth_method = AuthMethod.LOCAL_FS
    supports_delta = False
    max_upload_size = 50 * 1024 * 1024 * 1024  # 50 GB

    def __init__(self, username: str, account_id: str):
        super().__init__(username, account_id)
        self._sync_root: Optional[Path] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def sync_root(self) -> Path:
        """Return the resolved sync folder path, raising if not initialised."""
        if self._sync_root is None:
            raise RuntimeError("iCloud provider has not been authenticated")
        return self._sync_root

    @staticmethod
    def _file_hash(path: Path, algorithm: str = "sha256") -> str:
        """Compute a hex-digest hash of a file's contents."""
        h = hashlib.new(algorithm)
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def _to_metadata(self, path: Path) -> CloudFileMetadata:
        """Convert a local Path to CloudFileMetadata."""
        try:
            rel = path.relative_to(self.sync_root)
        except ValueError:
            rel = Path(path.name)

        stat = path.stat()
        is_folder = path.is_dir()

        content_hash = None
        if not is_folder:
            try:
                content_hash = self._file_hash(path)
            except OSError:
                pass

        return CloudFileMetadata(
            file_id=str(path),
            name=path.name,
            path="/" + rel.as_posix(),
            size=stat.st_size if not is_folder else 0,
            modified_time=stat.st_mtime,
            content_hash=content_hash,
            etag=None,
            is_folder=is_folder,
            mime_type=mimetypes.guess_type(str(path))[0] if not is_folder else None,
            extra={"inode": stat.st_ino},
        )

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    def authenticate(self) -> bool:
        """Verify that we are running on macOS and iCloud Drive is available."""
        if sys.platform != "darwin":
            logger.error("iCloud Drive is only available on macOS (current: %s)", sys.platform)
            return False

        if not ICLOUD_BASE.exists():
            logger.error(
                "iCloud Drive directory not found at %s. "
                "Ensure iCloud Drive is enabled in System Settings.",
                ICLOUD_BASE,
            )
            return False

        self._sync_root = ICLOUD_BASE / SYNC_FOLDER_NAME
        logger.info("iCloud Drive available at %s", ICLOUD_BASE)
        return True

    def ensure_sync_folder(self) -> str:
        """Create the 'VAF Sync' folder inside iCloud Drive if missing."""
        self.sync_root.mkdir(parents=True, exist_ok=True)
        logger.debug("Sync folder ensured at %s", self.sync_root)
        return str(self.sync_root)

    def list_files(self, folder_path: str = "/") -> List[CloudFileMetadata]:
        """List files in the sync folder or a subfolder."""
        if folder_path in ("", "/"):
            target = self.sync_root
        else:
            target = self.sync_root / folder_path.strip("/")

        if not target.exists():
            logger.warning("Folder does not exist: %s", target)
            return []

        results: List[CloudFileMetadata] = []
        try:
            for child in target.iterdir():
                # Skip hidden files and macOS metadata
                if child.name.startswith("."):
                    continue
                results.append(self._to_metadata(child))
        except OSError as exc:
            logger.error("Failed to list files in %s: %s", target, exc)
            raise

        logger.debug("Listed %d items in %s", len(results), folder_path)
        return results

    def upload_file(self, local_path: Path, remote_path: str) -> CloudFileMetadata:
        """Copy a local file into the iCloud Drive sync folder."""
        dest = self.sync_root / remote_path.strip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.copy2(str(local_path), str(dest))
            logger.info("Copied %s to %s (%d bytes)", local_path.name, dest, dest.stat().st_size)
            return self._to_metadata(dest)
        except OSError as exc:
            logger.error("Failed to copy %s to iCloud Drive: %s", local_path.name, exc)
            raise

    def download_file(self, file_id: str, local_path: Path) -> Path:
        """Copy a file from the iCloud Drive to a local destination.

        ``file_id`` is the absolute path within iCloud Drive.
        """
        source = Path(file_id)
        if not source.exists():
            raise FileNotFoundError(f"iCloud file not found: {source}")

        local_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.copy2(str(source), str(local_path))
            logger.info("Copied %s to %s", source.name, local_path)
            return local_path
        except OSError as exc:
            logger.error("Failed to copy %s from iCloud Drive: %s", file_id, exc)
            raise

    def delete_file(self, file_id: str) -> bool:
        """Delete a file from the iCloud Drive sync folder.

        ``file_id`` is the absolute path within iCloud Drive.
        """
        target = Path(file_id)
        if not target.exists():
            logger.warning("File already absent: %s", target)
            return True

        # Safety: refuse to delete anything outside the sync folder
        try:
            target.relative_to(self.sync_root)
        except ValueError:
            logger.error("Refusing to delete file outside sync root: %s", target)
            return False

        try:
            if target.is_dir():
                shutil.rmtree(str(target))
            else:
                target.unlink()
            logger.info("Deleted %s", target)
            return True
        except OSError as exc:
            logger.error("Failed to delete %s: %s", target, exc)
            return False

    def get_file_metadata(self, file_id: str) -> Optional[CloudFileMetadata]:
        """Get metadata for a single file by its absolute path."""
        target = Path(file_id)
        if not target.exists():
            logger.debug("File not found: %s", target)
            return None

        try:
            return self._to_metadata(target)
        except OSError as exc:
            logger.error("Error reading metadata for %s: %s", target, exc)
            return None
