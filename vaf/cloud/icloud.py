# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
iCloud Drive cloud provider using the local filesystem.

macOS only. Apple's system daemon (brctl / bird) handles the actual cloud
synchronisation transparently. This provider reads and writes to the local
iCloud Drive directory:

    ~/Library/Mobile Documents/com~apple~CloudDocs/

Does not support delta sync: full directory listings are used.

THE MACHINE OWNER'S ALONE. The folder above is the home of the OS user that runs VAF, and
no credential stands between VAF and it: `authenticate` only checks that it exists. So the
iCloud Drive of this Mac belongs to exactly one VAF account, the local admin, and an
instance is built for nobody else (`ICloudOwnerOnly`, raised in `__init__`, so a direct
construction that skips the factory is refused as well). Two halves, because each lane
supplies a different one: the background sync worker hands over a NAME read from config and
the scope recorded in the account entry; the Discord bridge runs with no scope and the
literal name "admin". The scope half alone would let any name with no scope through, the
name half alone would let a foreign scope through under the owner's name. A second admin is
a different person with a scope of their own and is refused too.

EVERY FILE ID STAYS INSIDE "VAF Sync". An id is an absolute path, and the methods used to
open whatever path they were handed: a file anywhere on this computer, the owner's iCloud
Drive next to "VAF Sync", a `..` id, a link inside the folder that points out. One resolver,
`_in_root`, answers for all of them with `vaf.contained_path`, which decides on RESOLVED
paths. It hands back the in-root spelling, so ids and metadata keep the form a listing
gives and existing sync manifests stay valid; the resolved path is the check only.

Named boundaries:
- A hard link inside "VAF Sync" is a second name of its file and is not seen.
- A link swapped in between the check and the copy is followed for that copy. Only the
  owner can write that folder, and only the owner can use this provider.
- A "VAF Sync" that is itself a link is accepted: the root is resolved before comparing,
  and the owner set that link up.
- An id in a different case ("vaf sync") is refused, never widened. Ids come from this
  provider's own listing in the exact case, and a refusal is the fail-closed answer on a
  case-insensitive volume; a case variant cannot escape either way, because the resolved
  target must still lie under the resolved root.
- The owner's own links that lead out of "VAF Sync" disappear from listings and are
  refused on read.
"""

import hashlib
import logging
import mimetypes
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import List, Optional

from vaf.core.path_jail import PathEscape, contained_path, is_rooted, open_for_write

from vaf.cloud.base import (
    AuthMethod,
    CloudFileMetadata,
    CloudProvider,
    SYNC_FOLDER_NAME,
)

logger = logging.getLogger("vaf.cloud.icloud")

ICLOUD_BASE = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"


class ICloudOwnerOnly(ValueError):
    """An iCloud provider was asked for by an account that is not the machine owner's.
    A ValueError, so every caller of the provider factory reports it like any other
    refusal of that factory."""


_OWNER_ONLY_TEXT = (
    "iCloud Drive on this computer belongs to the computer owner's account, so other "
    "accounts cannot use it. Remove the iCloud entry under Settings, Connections."
)


def _is_machine_owner(username: Optional[str], user_scope_id: Optional[str]) -> bool:
    """The local admin's lane (no scope, or the local-admin scope) AND the local admin's
    name, in any case. Read at call time, so a renamed owner is answered correctly."""
    from vaf.core import config as _config

    if not _config.is_local_admin_lane(user_scope_id):
        return False
    name = str(username or "").strip().casefold()
    return bool(name) and name == _config.get_local_admin_username().casefold()


def _copy_file(source: Path, dest: Path) -> None:
    """A copy that never writes through a link at ``dest`` nor into VAF's own data
    (`open_for_write`); shutil.copy2 followed one."""
    with open(source, "rb") as src, open_for_write(dest, "wb") as out:
        shutil.copyfileobj(src, out)


class ICloudProvider(CloudProvider):
    """iCloud Drive provider backed by the local filesystem on macOS."""

    provider_name = "icloud"
    auth_method = AuthMethod.LOCAL_FS
    supports_delta = False
    max_upload_size = 50 * 1024 * 1024 * 1024  # 50 GB

    def __init__(self, username: str, account_id: str, user_scope_id=None):
        if not _is_machine_owner(username, user_scope_id):
            raise ICloudOwnerOnly(_OWNER_ONLY_TEXT)
        super().__init__(username, account_id, user_scope_id)
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

    def _relative(self, ref: str) -> str:
        """``ref`` as a fragment below the sync root. An absolute ref must lie under the
        root as spelled, or under its resolved form (a home under a linked parent, such as
        /var -> /private/var). Compared as POSIX text, so the comparison is exact in case
        whatever the host's path flavour folds."""
        raw = str(ref or "")
        if not is_rooted(raw):
            return raw
        given = PurePosixPath(Path(raw).as_posix())
        root = self.sync_root
        for base in (root, root.resolve()):
            try:
                return given.relative_to(PurePosixPath(base.as_posix())).as_posix()
            except ValueError:
                continue
        raise PathEscape("iCloud file is outside the VAF Sync folder")

    def _in_root(self, ref: str) -> Path:
        """The in-root spelling of a file id or path, or `PathEscape`. `contained_path`
        refuses `..`, a rooted fragment, a null byte and anything that resolves outside the
        root (a link included); its resolved answer is the check, the returned path keeps
        the spelling a listing gives."""
        root = self.sync_root
        fragment = self._relative(ref)
        contained_path(root, fragment)
        parts = [p for p in fragment.replace("\\", "/").split("/") if p and p != "."]
        return root.joinpath(*parts)

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
        rel = None
        for base in (self.sync_root, self.sync_root.resolve()):
            try:
                rel = path.relative_to(base)
                break
            except ValueError:
                continue
        if rel is None:
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
        """List files in the sync folder or a subfolder.

        ``folder_path`` is a folder path below the sync root, "/sub" or "sub", as
        `CloudFileMetadata.path` spells it. Raises `PathEscape` for one that leaves it."""
        target = self._in_root(str(folder_path or "").lstrip("/"))

        if not target.exists():
            logger.warning("Folder does not exist: %s", target)
            return []

        results: List[CloudFileMetadata] = []
        try:
            for child in target.iterdir():
                # Skip hidden files and macOS metadata
                if child.name.startswith("."):
                    continue
                # A link that leads out of the sync folder is not part of it: listing it
                # would publish the outside target's size and hash.
                try:
                    self._in_root(str(child))
                except PathEscape:
                    logger.debug("Skipping link out of the sync folder: %s", child.name)
                    continue
                results.append(self._to_metadata(child))
        except OSError as exc:
            logger.error("Failed to list files in %s: %s", target, exc)
            raise

        logger.debug("Listed %d items in %s", len(results), folder_path)
        return results

    def upload_file(self, local_path: Path, remote_path: str) -> CloudFileMetadata:
        """Copy a local file into the iCloud Drive sync folder.

        ``remote_path`` is relative to the sync root; one that leaves it, or names the root
        itself, raises `PathEscape`."""
        dest = self._in_root(remote_path)
        if dest == self.sync_root:
            raise PathEscape("remote_path must name a file inside the VAF Sync folder")
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            _copy_file(local_path, dest)
            logger.info("Copied %s to %s (%d bytes)", local_path.name, dest, dest.stat().st_size)
            return self._to_metadata(dest)
        except OSError as exc:
            logger.error("Failed to copy %s to iCloud Drive: %s", local_path.name, exc)
            raise

    def download_file(self, file_id: str, local_path: Path) -> Path:
        """Copy a file from the iCloud Drive to a local destination.

        ``file_id`` is the absolute path within the sync folder; one outside it raises
        `PathEscape`.
        """
        source = self._in_root(file_id)
        if not source.exists():
            raise FileNotFoundError(f"iCloud file not found: {source}")

        local_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            _copy_file(source, local_path)
            logger.info("Copied %s to %s", source.name, local_path)
            return local_path
        except OSError as exc:
            logger.error("Failed to copy %s from iCloud Drive: %s", file_id, exc)
            raise

    def delete_file(self, file_id: str) -> bool:
        """Delete a file from the iCloud Drive sync folder.

        ``file_id`` is the absolute path within the sync folder. One outside it, or the
        folder itself, is refused (False). A link is removed as a link, never its target.
        """
        try:
            target = self._in_root(file_id)
        except PathEscape:
            logger.error("Refusing to delete file outside sync root: %s", file_id)
            return False
        if target == self.sync_root:
            logger.error("Refusing to delete the sync root itself")
            return False

        if not target.exists() and not target.is_symlink():
            logger.warning("File already absent: %s", target)
            return True

        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(str(target))
            else:
                target.unlink()
            logger.info("Deleted %s", target)
            return True
        except OSError as exc:
            logger.error("Failed to delete %s: %s", target, exc)
            return False

    def get_file_metadata(self, file_id: str) -> Optional[CloudFileMetadata]:
        """Get metadata for a single file by its absolute path inside the sync folder.
        None for an id outside it, the same answer as for a missing one."""
        try:
            target = self._in_root(file_id)
        except PathEscape:
            logger.debug("File id outside the sync folder: %s", file_id)
            return None
        if not target.exists():
            logger.debug("File not found: %s", target)
            return None

        try:
            return self._to_metadata(target)
        except OSError as exc:
            logger.error("Error reading metadata for %s: %s", target, exc)
            return None
