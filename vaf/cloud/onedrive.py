# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
OneDrive cloud provider using the Microsoft Graph API.

Supports delta sync via the delta API endpoint.
All operations are scoped to a "VAF Sync" folder in the user's OneDrive root.
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

logger = logging.getLogger("vaf.cloud.onedrive")

GRAPH_BASE = "https://graph.microsoft.com/v1.0/me/drive"
SIMPLE_UPLOAD_THRESHOLD = 4 * 1024 * 1024  # 4 MB
UPLOAD_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB (must be multiple of 320 KiB)


class OneDriveProvider(CloudProvider):
    """OneDrive provider using Microsoft Graph API."""

    provider_name = "onedrive"
    auth_method = AuthMethod.OAUTH2
    supports_delta = True
    max_upload_size = 250 * 1024 * 1024 * 1024  # 250 GB

    def __init__(self, username: str, account_id: str):
        super().__init__(username, account_id)
        self._access_token: Optional[str] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token}"}

    def _get(self, url: str, params: Optional[dict] = None, **kwargs) -> requests.Response:
        resp = requests.get(url, headers=self._headers(), params=params, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp

    def _sync_path(self, relative: str = "") -> str:
        """Build the Graph API path scoped to the sync folder."""
        base = f"{GRAPH_BASE}/root:/{SYNC_FOLDER_NAME}"
        if relative:
            clean = relative.strip("/")
            return f"{base}/{clean}"
        return base

    def _parse_item(self, item: dict) -> CloudFileMetadata:
        """Convert a Graph driveItem to CloudFileMetadata."""
        parent_ref = item.get("parentReference", {})
        parent_path = parent_ref.get("path", "")
        # Strip everything up to and including the sync folder name
        idx = parent_path.find(SYNC_FOLDER_NAME)
        if idx >= 0:
            rel_parent = parent_path[idx + len(SYNC_FOLDER_NAME):]
        else:
            rel_parent = ""

        rel_path = f"{rel_parent.rstrip('/')}/{item['name']}"

        mod_time = item.get("lastModifiedDateTime", "")
        try:
            from datetime import datetime, timezone
            ts = datetime.fromisoformat(mod_time.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            ts = 0.0

        # OneDrive provides quickXorHash or sha1Hash
        hashes = item.get("file", {}).get("hashes", {})
        content_hash = hashes.get("quickXorHash") or hashes.get("sha1Hash")

        return CloudFileMetadata(
            file_id=item["id"],
            name=item["name"],
            path=rel_path,
            size=int(item.get("size", 0)),
            modified_time=ts,
            content_hash=content_hash,
            etag=item.get("eTag"),
            is_folder="folder" in item,
            mime_type=item.get("file", {}).get("mimeType"),
            extra={"ctag": item.get("cTag")},
        )

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    def authenticate(self) -> bool:
        """Obtain and validate an OAuth2 access token for OneDrive."""
        from vaf.cloud.oauth_cloud import get_valid_access_token

        token = get_valid_access_token(self.account_id, self.provider_name, self.username)
        if not token:
            logger.error("Failed to obtain OneDrive access token for %s", self.username)
            return False

        self._access_token = token

        # Validate by fetching drive info
        try:
            resp = requests.get(
                f"{GRAPH_BASE}",
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            owner = resp.json().get("owner", {}).get("user", {}).get("displayName", "unknown")
            logger.info("Authenticated to OneDrive as %s", owner)
            return True
        except requests.RequestException as exc:
            logger.error("OneDrive auth validation failed: %s", exc)
            self._access_token = None
            return False

    def ensure_sync_folder(self) -> str:
        """Find or create the 'VAF Sync' folder in the OneDrive root."""
        # Check if folder exists
        try:
            resp = self._get(f"{self._sync_path()}:")
            folder_id = resp.json()["id"]
            logger.debug("Found existing sync folder: %s", folder_id)
            return folder_id
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                logger.error("Error checking for sync folder: %s", exc)
                raise
            # Folder not found — create it

        # Create the folder
        payload = {
            "name": SYNC_FOLDER_NAME,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        }

        try:
            resp = requests.post(
                f"{GRAPH_BASE}/root/children",
                headers={**self._headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            folder_id = resp.json()["id"]
            logger.info("Created sync folder: %s", folder_id)
            return folder_id
        except requests.RequestException as exc:
            logger.error("Failed to create sync folder: %s", exc)
            raise

    def list_files(self, folder_path: str = "/") -> List[CloudFileMetadata]:
        """List files in the sync folder or a subfolder."""
        if folder_path in ("", "/"):
            url = f"{self._sync_path()}:/children"
        else:
            clean = folder_path.strip("/")
            url = f"{self._sync_path(clean)}:/children"

        results: List[CloudFileMetadata] = []
        next_link: Optional[str] = url

        while next_link:
            try:
                resp = self._get(next_link)
                data = resp.json()
            except requests.RequestException as exc:
                logger.error("Failed to list files in %s: %s", folder_path, exc)
                raise

            for item in data.get("value", []):
                results.append(self._parse_item(item))

            next_link = data.get("@odata.nextLink")

        logger.debug("Listed %d items in %s", len(results), folder_path)
        return results

    def list_folder_by_id(self, folder_id: str, parent_path: str = "/") -> List[CloudFileMetadata]:
        """List contents of any folder by item ID. Use for cloud-only browsing (full OneDrive)."""
        if folder_id == "root":
            url = f"{GRAPH_BASE}/root/children"
        else:
            url = f"{GRAPH_BASE}/items/{folder_id}/children"

        results: List[CloudFileMetadata] = []
        next_link: Optional[str] = url

        while next_link:
            try:
                resp = self._get(next_link)
                data = resp.json()
            except requests.RequestException as exc:
                logger.error("Failed to list folder %s: %s", folder_id, exc)
                raise

            for item in data.get("value", []):
                results.append(self._parse_item(item))

            next_link = data.get("@odata.nextLink")

        return results

    def search_files(self, query: str, mime_type: Optional[str] = None, limit: int = 100) -> List[CloudFileMetadata]:
        """Search entire OneDrive by filename."""
        if not query or not query.strip():
            return []
        from urllib.parse import quote
        q_clean = query.strip().replace("*", "").replace("'", " ").strip() or query
        if not q_clean:
            return []
        url = f"{GRAPH_BASE}/root/search(q='{quote(q_clean, safe='')}')"
        results: List[CloudFileMetadata] = []
        next_link: Optional[str] = url
        while next_link and len(results) < limit:
            try:
                resp = self._get(next_link)
                data = resp.json()
            except requests.RequestException as exc:
                logger.error("OneDrive search failed for %r: %s", query, exc)
                raise
            for item in data.get("value", []):
                if mime_type and item.get("file", {}).get("mimeType") != mime_type:
                    continue
                results.append(self._parse_item(item))
                if len(results) >= limit:
                    break
            next_link = data.get("@odata.nextLink")
        return results

    def upload_file(self, local_path: Path, remote_path: str) -> CloudFileMetadata:
        """Upload a file. Uses simple PUT for <4MB, upload session for larger."""
        file_size = local_path.stat().st_size

        if file_size < SIMPLE_UPLOAD_THRESHOLD:
            return self._upload_simple(local_path, remote_path)
        else:
            return self._upload_session(local_path, remote_path, file_size)

    def _upload_simple(self, local_path: Path, remote_path: str) -> CloudFileMetadata:
        """Upload a small file via PUT."""
        clean = remote_path.strip("/")
        url = f"{self._sync_path(clean)}:/content"

        try:
            with open(local_path, "rb") as fh:
                resp = requests.put(
                    url,
                    headers={**self._headers(), "Content-Type": "application/octet-stream"},
                    data=fh.read(),
                    timeout=60,
                )
            resp.raise_for_status()
            item = resp.json()
            logger.info("Uploaded %s (%d bytes) via simple upload", local_path.name, local_path.stat().st_size)
            return self._parse_item(item)
        except requests.RequestException as exc:
            logger.error("Simple upload failed for %s: %s", local_path.name, exc)
            raise

    def _upload_session(self, local_path: Path, remote_path: str, file_size: int) -> CloudFileMetadata:
        """Upload a large file via an upload session."""
        clean = remote_path.strip("/")
        url = f"{self._sync_path(clean)}:/createUploadSession"

        # Create the upload session
        try:
            resp = requests.post(
                url,
                headers={**self._headers(), "Content-Type": "application/json"},
                json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
                timeout=30,
            )
            resp.raise_for_status()
            upload_url = resp.json()["uploadUrl"]
        except requests.RequestException as exc:
            logger.error("Failed to create upload session for %s: %s", local_path.name, exc)
            raise

        # Upload chunks
        offset = 0
        with open(local_path, "rb") as fh:
            while offset < file_size:
                chunk = fh.read(UPLOAD_CHUNK_SIZE)
                chunk_size = len(chunk)
                end = offset + chunk_size - 1

                headers = {
                    "Content-Length": str(chunk_size),
                    "Content-Range": f"bytes {offset}-{end}/{file_size}",
                }

                try:
                    resp = requests.put(upload_url, headers=headers, data=chunk, timeout=120)

                    if resp.status_code in (200, 201):
                        item = resp.json()
                        logger.info(
                            "Upload session complete for %s (%d bytes)", local_path.name, file_size
                        )
                        return self._parse_item(item)
                    elif resp.status_code == 202:
                        # Accepted — continue
                        offset += chunk_size
                    else:
                        resp.raise_for_status()
                except requests.RequestException as exc:
                    logger.error(
                        "Upload session chunk failed at offset %d for %s: %s",
                        offset, local_path.name, exc,
                    )
                    raise

        raise RuntimeError(f"Upload session ended without completion for {local_path.name}")

    def download_file(self, file_id: str, local_path: Path) -> Path:
        """Download a file by its item ID."""
        local_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            resp = requests.get(
                f"{GRAPH_BASE}/items/{file_id}/content",
                headers=self._headers(),
                stream=True,
                timeout=120,
                allow_redirects=True,
            )
            resp.raise_for_status()

            with open(local_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    fh.write(chunk)

            logger.info("Downloaded item %s to %s", file_id, local_path)
            return local_path
        except requests.RequestException as exc:
            logger.error("Failed to download item %s: %s", file_id, exc)
            raise

    def delete_file(self, file_id: str) -> bool:
        """Delete a file by its item ID."""
        try:
            resp = requests.delete(
                f"{GRAPH_BASE}/items/{file_id}",
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("Deleted item %s", file_id)
            return True
        except requests.RequestException as exc:
            logger.error("Failed to delete item %s: %s", file_id, exc)
            return False

    def get_file_metadata(self, file_id: str) -> Optional[CloudFileMetadata]:
        """Fetch metadata for a single item by ID."""
        try:
            resp = self._get(f"{GRAPH_BASE}/items/{file_id}")
            return self._parse_item(resp.json())
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.debug("Item %s not found", file_id)
                return None
            logger.error("Error fetching metadata for %s: %s", file_id, exc)
            raise

    def get_changes(self, cursor: Optional[str] = None) -> DeltaPage:
        """Retrieve incremental changes using the OneDrive delta API."""
        if cursor:
            url = cursor  # OneDrive delta cursors are full URLs
        else:
            url = f"{self._sync_path()}:/delta"

        files: List[CloudFileMetadata] = []
        deleted_ids: List[str] = []

        try:
            resp = self._get(url)
            data = resp.json()
        except requests.RequestException as exc:
            logger.error("Failed to fetch delta changes: %s", exc)
            raise

        for item in data.get("value", []):
            if item.get("deleted"):
                deleted_ids.append(item["id"])
            else:
                files.append(self._parse_item(item))

        # deltaLink = no more changes, nextLink = more pages
        new_cursor = data.get("@odata.deltaLink") or data.get("@odata.nextLink")
        has_more = "@odata.nextLink" in data

        return DeltaPage(files=files, deleted_ids=deleted_ids, cursor=new_cursor, has_more=has_more)
