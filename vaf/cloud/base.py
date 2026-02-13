"""
Abstract base class for cloud storage providers and shared data types.

Each provider creates a "VAF Sync" folder in the user's cloud root.
All sync operations are scoped to this folder.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class AuthMethod(Enum):
    OAUTH2 = "oauth2"
    APP_PASSWORD = "app_password"
    LOCAL_FS = "local_fs"


@dataclass
class CloudFileMetadata:
    """Unified metadata for a file in any cloud provider."""

    file_id: str
    name: str
    path: str                        # Relative path within the sync folder
    size: int
    modified_time: float             # Unix timestamp
    content_hash: Optional[str] = None
    etag: Optional[str] = None
    is_folder: bool = False
    mime_type: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DeltaPage:
    """A page of changes from a delta / changes API."""

    files: List[CloudFileMetadata] = field(default_factory=list)
    deleted_ids: List[str] = field(default_factory=list)
    cursor: Optional[str] = None
    has_more: bool = False


@dataclass
class SyncResult:
    """Summary of a single sync cycle."""

    uploaded: int = 0
    downloaded: int = 0
    deleted_local: int = 0
    deleted_remote: int = 0
    conflicts: int = 0
    errors: int = 0
    skipped: int = 0


# Sync-folder name used across all providers
SYNC_FOLDER_NAME = "VAF Sync"

# File extensions / patterns that must never be synced
EXCLUDED_EXTENSIONS = frozenset({
    ".enc", ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite-wal", ".sqlite-shm",
})

EXCLUDED_FILENAMES = frozenset({
    "config.json", "credentials.enc", "cloud_credentials.enc",
    "email_credentials.enc", ".DS_Store", "Thumbs.db",
})


def is_syncable(filepath: Path, max_size_bytes: int) -> bool:
    """Return True if a local file is eligible for sync."""
    name = filepath.name
    if name.startswith("."):
        return False
    if name in EXCLUDED_FILENAMES:
        return False
    if filepath.suffix.lower() in EXCLUDED_EXTENSIONS:
        return False
    try:
        if filepath.stat().st_size > max_size_bytes:
            return False
    except OSError:
        return False
    return True


class CloudProvider(ABC):
    """Abstract base for all cloud storage providers."""

    provider_name: str = ""
    auth_method: AuthMethod = AuthMethod.OAUTH2
    supports_delta: bool = False
    max_upload_size: int = 100 * 1024 * 1024  # 100 MB default

    def __init__(self, username: str, account_id: str):
        self.username = username
        self.account_id = account_id

    @abstractmethod
    def authenticate(self) -> bool:
        """Validate / refresh credentials. Return True if auth is valid."""
        ...

    @abstractmethod
    def list_files(self, folder_path: str = "/") -> List[CloudFileMetadata]:
        """List all files in the VAF Sync folder (or subfolder)."""
        ...

    @abstractmethod
    def upload_file(self, local_path: Path, remote_path: str) -> CloudFileMetadata:
        """Upload a local file to the sync folder. remote_path is relative to sync root."""
        ...

    @abstractmethod
    def download_file(self, file_id: str, local_path: Path) -> Path:
        """Download a file by its provider ID to a local path."""
        ...

    @abstractmethod
    def delete_file(self, file_id: str) -> bool:
        """Delete a file from the sync folder."""
        ...

    @abstractmethod
    def get_file_metadata(self, file_id: str) -> Optional[CloudFileMetadata]:
        """Get metadata for a single file."""
        ...

    def get_changes(self, cursor: Optional[str] = None) -> DeltaPage:
        """Get incremental changes since cursor. Override for delta-capable providers."""
        raise NotImplementedError(f"{self.provider_name} does not support delta sync")

    @abstractmethod
    def ensure_sync_folder(self) -> str:
        """Create the sync folder in cloud root if it doesn't exist. Return its ID/path."""
        ...

    def list_folder_by_id(self, folder_id: str, parent_path: str = "/") -> List[CloudFileMetadata]:
        """List contents of any folder by provider ID. For cloud-only browsing (full drive). Override to support."""
        raise NotImplementedError(f"{self.provider_name} does not support full-drive browsing")

    def search_files(self, query: str, mime_type: Optional[str] = None, limit: int = 100) -> List[CloudFileMetadata]:
        """Search entire cloud by filename. Override to support."""
        raise NotImplementedError(f"{self.provider_name} does not support cloud search")
