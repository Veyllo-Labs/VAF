# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Dropbox cloud provider using the Dropbox API v2.

Named ``dropbox_provider.py`` to avoid collision with the ``dropbox`` pip package.
Supports delta sync via ``list_folder/continue``.
All operations are scoped to a "/VAF Sync" folder in the user's Dropbox.
"""

import json
import logging
import time
from pathlib import Path
from typing import List, Optional

import requests

from vaf.cloud.base import (
    AuthMethod,
    CloudFileMetadata,
    CloudProvider,
    DeltaPage,
    SYNC_FOLDER_NAME,
)

logger = logging.getLogger("vaf.cloud.dropbox_provider")

API_BASE = "https://api.dropboxapi.com/2"
CONTENT_BASE = "https://content.dropboxapi.com/2"
UPLOAD_THRESHOLD = 150 * 1024 * 1024  # 150 MB — single-call upload limit
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB per session chunk

SYNC_ROOT = f"/{SYNC_FOLDER_NAME}"


class DropboxProvider(CloudProvider):
    """Dropbox provider using the HTTP API v2 (no SDK)."""

    provider_name = "dropbox"
    auth_method = AuthMethod.OAUTH2
    supports_delta = True
    max_upload_size = 350 * 1024 * 1024 * 1024  # 350 GB

    def __init__(self, username: str, account_id: str):
        super().__init__(username, account_id)
        self._access_token: Optional[str] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _headers(self, content_type: str = "application/json") -> dict:
        h = {"Authorization": f"Bearer {self._access_token}"}
        if content_type:
            h["Content-Type"] = content_type
        return h

    def _api_post(self, endpoint: str, payload: Optional[dict] = None) -> dict:
        """POST to the Dropbox RPC API and return JSON."""
        url = f"{API_BASE}/{endpoint}"
        resp = requests.post(
            url,
            headers=self._headers(),
            json=payload or {},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _remote_path(self, relative: str) -> str:
        """Build the full Dropbox path from a sync-relative path."""
        clean = relative.strip("/")
        if clean:
            return f"{SYNC_ROOT}/{clean}"
        return SYNC_ROOT

    def _parse_entry(self, entry: dict) -> CloudFileMetadata:
        """Convert a Dropbox metadata entry to CloudFileMetadata."""
        is_folder = entry.get(".tag") == "folder"
        path_display = entry.get("path_display", "")

        # Compute relative path within the sync folder
        idx = path_display.lower().find(SYNC_ROOT.lower())
        if idx >= 0:
            rel_path = path_display[idx + len(SYNC_ROOT):]
        else:
            rel_path = path_display
        if not rel_path:
            rel_path = "/"

        mod_time = 0.0
        if not is_folder:
            server_modified = entry.get("server_modified", "")
            if server_modified:
                try:
                    from datetime import datetime, timezone
                    ts = datetime.fromisoformat(
                        server_modified.replace("Z", "+00:00")
                    ).timestamp()
                    mod_time = ts
                except (ValueError, TypeError):
                    pass

        return CloudFileMetadata(
            file_id=entry.get("id", entry.get("path_lower", "")),
            name=entry.get("name", ""),
            path=rel_path,
            size=int(entry.get("size", 0)),
            modified_time=mod_time,
            content_hash=entry.get("content_hash"),
            etag=entry.get("rev"),
            is_folder=is_folder,
            mime_type=None,  # Dropbox API does not return MIME types
            extra={"rev": entry.get("rev"), "path_lower": entry.get("path_lower")},
        )

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    def authenticate(self) -> bool:
        """Obtain and validate an OAuth2 access token for Dropbox."""
        from vaf.cloud.oauth_cloud import get_valid_access_token

        token = get_valid_access_token(self.account_id, self.provider_name, self.username)
        if not token:
            logger.error("Failed to obtain Dropbox access token for %s", self.username)
            return False

        self._access_token = token

        # Validate token with a lightweight call
        try:
            data = self._api_post("users/get_current_account", payload=None)
            display = data.get("name", {}).get("display_name", "unknown")
            logger.info("Authenticated to Dropbox as %s", display)
            return True
        except requests.RequestException as exc:
            logger.error("Dropbox auth validation failed: %s", exc)
            self._access_token = None
            return False

    def ensure_sync_folder(self) -> str:
        """Create the '/VAF Sync' folder if it does not already exist."""
        try:
            data = self._api_post(
                "files/get_metadata",
                {"path": SYNC_ROOT},
            )
            folder_id = data.get("id", SYNC_ROOT)
            logger.debug("Found existing sync folder: %s", folder_id)
            return folder_id
        except requests.HTTPError as exc:
            if exc.response is None:
                raise
            # 409 conflict with path/not_found means folder doesn't exist
            try:
                err = exc.response.json()
            except ValueError:
                raise exc
            tag = err.get("error", {}).get(".tag", "")
            if tag != "path" or "not_found" not in json.dumps(err):
                raise

        # Create the folder
        try:
            data = self._api_post(
                "files/create_folder_v2",
                {"path": SYNC_ROOT, "autorename": False},
            )
            folder_id = data.get("metadata", {}).get("id", SYNC_ROOT)
            logger.info("Created sync folder: %s", folder_id)
            return folder_id
        except requests.RequestException as exc:
            logger.error("Failed to create sync folder: %s", exc)
            raise

    def list_files(self, folder_path: str = "/") -> List[CloudFileMetadata]:
        """List files in the sync folder or a subfolder."""
        remote = self._remote_path(folder_path)

        results: List[CloudFileMetadata] = []

        try:
            data = self._api_post(
                "files/list_folder",
                {
                    "path": remote,
                    "recursive": False,
                    "include_deleted": False,
                    "limit": 2000,
                },
            )
        except requests.RequestException as exc:
            logger.error("Failed to list files in %s: %s", folder_path, exc)
            raise

        for entry in data.get("entries", []):
            results.append(self._parse_entry(entry))

        # Handle pagination
        while data.get("has_more"):
            cursor = data["cursor"]
            try:
                data = self._api_post(
                    "files/list_folder/continue",
                    {"cursor": cursor},
                )
            except requests.RequestException as exc:
                logger.error("Failed to continue listing in %s: %s", folder_path, exc)
                raise

            for entry in data.get("entries", []):
                results.append(self._parse_entry(entry))

        logger.debug("Listed %d items in %s", len(results), folder_path)
        return results

    def upload_file(self, local_path: Path, remote_path: str) -> CloudFileMetadata:
        """Upload a file to the sync folder."""
        file_size = local_path.stat().st_size
        dest = self._remote_path(remote_path)

        if file_size < UPLOAD_THRESHOLD:
            return self._upload_single(local_path, dest)
        else:
            return self._upload_session(local_path, dest, file_size)

    def _upload_single(self, local_path: Path, dest: str) -> CloudFileMetadata:
        """Upload a file in a single request."""
        api_arg = json.dumps({
            "path": dest,
            "mode": "overwrite",
            "autorename": False,
            "mute": True,
        })

        try:
            with open(local_path, "rb") as fh:
                resp = requests.post(
                    f"{CONTENT_BASE}/files/upload",
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Content-Type": "application/octet-stream",
                        "Dropbox-API-Arg": api_arg,
                    },
                    data=fh,
                    timeout=120,
                )
            resp.raise_for_status()
            entry = resp.json()
            logger.info("Uploaded %s (%d bytes)", local_path.name, local_path.stat().st_size)
            return self._parse_entry(entry)
        except requests.RequestException as exc:
            logger.error("Upload failed for %s: %s", local_path.name, exc)
            raise

    def _upload_session(self, local_path: Path, dest: str, file_size: int) -> CloudFileMetadata:
        """Upload a large file via an upload session."""
        # Start session
        try:
            resp = requests.post(
                f"{CONTENT_BASE}/files/upload_session/start",
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/octet-stream",
                    "Dropbox-API-Arg": json.dumps({"close": False}),
                },
                data=b"",
                timeout=30,
            )
            resp.raise_for_status()
            session_id = resp.json()["session_id"]
        except requests.RequestException as exc:
            logger.error("Failed to start upload session for %s: %s", local_path.name, exc)
            raise

        # Append chunks
        offset = 0
        with open(local_path, "rb") as fh:
            while offset < file_size:
                chunk = fh.read(UPLOAD_CHUNK_SIZE)
                chunk_size = len(chunk)
                remaining = file_size - offset - chunk_size

                if remaining > 0:
                    # Append
                    api_arg = json.dumps({
                        "cursor": {"session_id": session_id, "offset": offset},
                        "close": False,
                    })
                    try:
                        resp = requests.post(
                            f"{CONTENT_BASE}/files/upload_session/append_v2",
                            headers={
                                "Authorization": f"Bearer {self._access_token}",
                                "Content-Type": "application/octet-stream",
                                "Dropbox-API-Arg": api_arg,
                            },
                            data=chunk,
                            timeout=120,
                        )
                        resp.raise_for_status()
                    except requests.RequestException as exc:
                        logger.error(
                            "Upload session append failed at offset %d: %s", offset, exc
                        )
                        raise
                    offset += chunk_size
                else:
                    # Finish
                    api_arg = json.dumps({
                        "cursor": {"session_id": session_id, "offset": offset},
                        "commit": {
                            "path": dest,
                            "mode": "overwrite",
                            "autorename": False,
                            "mute": True,
                        },
                    })
                    try:
                        resp = requests.post(
                            f"{CONTENT_BASE}/files/upload_session/finish",
                            headers={
                                "Authorization": f"Bearer {self._access_token}",
                                "Content-Type": "application/octet-stream",
                                "Dropbox-API-Arg": api_arg,
                            },
                            data=chunk,
                            timeout=120,
                        )
                        resp.raise_for_status()
                        entry = resp.json()
                        logger.info(
                            "Upload session complete for %s (%d bytes)",
                            local_path.name, file_size,
                        )
                        return self._parse_entry(entry)
                    except requests.RequestException as exc:
                        logger.error("Upload session finish failed: %s", exc)
                        raise

        raise RuntimeError(f"Upload session ended without completion for {local_path.name}")

    def download_file(self, file_id: str, local_path: Path) -> Path:
        """Download a file by its Dropbox ID or path."""
        local_path.parent.mkdir(parents=True, exist_ok=True)

        # Dropbox download uses Dropbox-API-Arg header with a path or id
        identifier = {"id": file_id} if file_id.startswith("id:") else {"path": file_id}
        api_arg = json.dumps(identifier)

        try:
            resp = requests.post(
                f"{CONTENT_BASE}/files/download",
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Dropbox-API-Arg": api_arg,
                },
                stream=True,
                timeout=120,
            )
            resp.raise_for_status()

            with open(local_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    fh.write(chunk)

            logger.info("Downloaded %s to %s", file_id, local_path)
            return local_path
        except requests.RequestException as exc:
            logger.error("Failed to download %s: %s", file_id, exc)
            raise

    def delete_file(self, file_id: str) -> bool:
        """Delete a file by its Dropbox ID or path."""
        identifier = {"id": file_id} if file_id.startswith("id:") else {"path": file_id}

        try:
            self._api_post("files/delete_v2", identifier)
            logger.info("Deleted %s", file_id)
            return True
        except requests.RequestException as exc:
            logger.error("Failed to delete %s: %s", file_id, exc)
            return False

    def get_file_metadata(self, file_id: str) -> Optional[CloudFileMetadata]:
        """Fetch metadata for a single file."""
        identifier = {"id": file_id} if file_id.startswith("id:") else {"path": file_id}

        try:
            data = self._api_post("files/get_metadata", identifier)
            return self._parse_entry(data)
        except requests.HTTPError as exc:
            if exc.response is not None:
                try:
                    err = exc.response.json()
                except ValueError:
                    raise exc
                tag = err.get("error", {}).get(".tag", "")
                if tag == "path" and "not_found" in json.dumps(err):
                    logger.debug("File %s not found", file_id)
                    return None
            logger.error("Error fetching metadata for %s: %s", file_id, exc)
            raise

    def get_changes(self, cursor: Optional[str] = None) -> DeltaPage:
        """Retrieve incremental changes using list_folder/continue."""
        files: List[CloudFileMetadata] = []
        deleted_ids: List[str] = []

        if cursor is None:
            # Initial listing — returns a cursor for future delta calls
            try:
                data = self._api_post(
                    "files/list_folder",
                    {
                        "path": SYNC_ROOT,
                        "recursive": True,
                        "include_deleted": True,
                        "limit": 2000,
                    },
                )
            except requests.RequestException as exc:
                logger.error("Failed to get initial delta listing: %s", exc)
                raise
        else:
            try:
                data = self._api_post(
                    "files/list_folder/continue",
                    {"cursor": cursor},
                )
            except requests.RequestException as exc:
                logger.error("Failed to continue delta listing: %s", exc)
                raise

        for entry in data.get("entries", []):
            tag = entry.get(".tag")
            if tag == "deleted":
                deleted_ids.append(entry.get("id", entry.get("path_lower", "")))
            else:
                files.append(self._parse_entry(entry))

        return DeltaPage(
            files=files,
            deleted_ids=deleted_ids,
            cursor=data.get("cursor"),
            has_more=data.get("has_more", False),
        )
