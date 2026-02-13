"""
Google Drive cloud provider using the REST API v3.

Supports delta sync via the Changes API.
All operations are scoped to a "VAF Sync" folder in the user's Drive root.
"""

import io
import json
import logging
import time
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

import requests

from vaf.cloud.base import (
    AuthMethod,
    CloudFileMetadata,
    CloudProvider,
    DeltaPage,
    SYNC_FOLDER_NAME,
)

logger = logging.getLogger("vaf.cloud.google_drive")

API_BASE = "https://www.googleapis.com/drive/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"
MULTIPART_THRESHOLD = 5 * 1024 * 1024  # 5 MB
RESUMABLE_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB per chunk

# Fields requested in file resource responses
FILE_FIELDS = "id,name,mimeType,size,modifiedTime,md5Checksum,parents,trashed"
FILE_LIST_FIELDS = f"nextPageToken,files({FILE_FIELDS})"

FOLDER_MIME = "application/vnd.google-apps.folder"


class GoogleDriveProvider(CloudProvider):
    """Google Drive provider using REST API v3."""

    provider_name = "google_drive"
    auth_method = AuthMethod.OAUTH2
    supports_delta = True
    max_upload_size = 5 * 1024 * 1024 * 1024  # 5 GB

    def __init__(self, username: str, account_id: str):
        super().__init__(username, account_id)
        self._access_token: Optional[str] = None
        self._sync_folder_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        """Return authorization headers."""
        return {"Authorization": f"Bearer {self._access_token}"}

    def _get(self, url: str, params: Optional[dict] = None, **kwargs) -> requests.Response:
        resp = requests.get(url, headers=self._headers(), params=params, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp

    def _post(self, url: str, **kwargs) -> requests.Response:
        resp = requests.post(url, headers=self._headers(), timeout=30, **kwargs)
        resp.raise_for_status()
        return resp

    def _delete(self, url: str, **kwargs) -> requests.Response:
        resp = requests.delete(url, headers=self._headers(), timeout=30, **kwargs)
        resp.raise_for_status()
        return resp

    def _parse_file(self, item: dict, parent_path: str = "/") -> CloudFileMetadata:
        """Convert a Drive file resource to CloudFileMetadata."""
        mod_time = item.get("modifiedTime", "")
        try:
            from datetime import datetime, timezone
            ts = datetime.fromisoformat(mod_time.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            ts = 0.0

        return CloudFileMetadata(
            file_id=item["id"],
            name=item["name"],
            path=f"{parent_path.rstrip('/')}/{item['name']}",
            size=int(item.get("size", 0)),
            modified_time=ts,
            content_hash=item.get("md5Checksum"),
            etag=None,
            is_folder=item.get("mimeType") == FOLDER_MIME,
            mime_type=item.get("mimeType"),
            extra={"parents": item.get("parents", [])},
        )

    def _resolve_folder_id(self, folder_path: str) -> str:
        """Resolve a path like '/subfolder/deep' to a Drive folder ID."""
        if folder_path in ("", "/"):
            return self._sync_folder_id or "root"

        parts = [p for p in folder_path.strip("/").split("/") if p]
        current_id = self._sync_folder_id or "root"

        for part in parts:
            q = (
                f"'{current_id}' in parents and name = '{part}' "
                f"and mimeType = '{FOLDER_MIME}' and trashed = false"
            )
            resp = self._get(
                f"{API_BASE}/files",
                params={"q": q, "fields": "files(id,name)", "pageSize": 1},
            )
            files = resp.json().get("files", [])
            if not files:
                raise FileNotFoundError(f"Folder not found in Drive: {part}")
            current_id = files[0]["id"]

        return current_id

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    def authenticate(self) -> bool:
        """Obtain and validate an OAuth2 access token for Google Drive."""
        from vaf.cloud.oauth_cloud import get_valid_access_token

        token = get_valid_access_token(self.account_id, self.provider_name, self.username)
        if not token:
            logger.error("Failed to obtain Google Drive access token for %s", self.username)
            return False

        self._access_token = token

        # Quick validation: fetch user info
        try:
            resp = requests.get(
                f"{API_BASE}/about",
                headers=self._headers(),
                params={"fields": "user(emailAddress)"},
                timeout=15,
            )
            resp.raise_for_status()
            email = resp.json().get("user", {}).get("emailAddress", "unknown")
            logger.info("Authenticated to Google Drive as %s", email)
            return True
        except requests.RequestException as exc:
            logger.error("Google Drive auth validation failed: %s", exc)
            self._access_token = None
            err_resp = getattr(exc, "response", None)
            hint = ""
            if err_resp is not None and hasattr(err_resp, "status_code"):
                if err_resp.status_code == 401:
                    hint = " Token invalid/expired. Remove account and reconnect. In Google Cloud Console: enable Drive API, add redirect URIs for both /api/email/oauth/callback and /api/cloud/oauth/callback."
                elif err_resp.status_code == 403:
                    hint = " Drive API not enabled. Go to Google Cloud Console → APIs & Services → Library → enable 'Google Drive API'."
            if hint:
                raise ValueError(f"Google Drive Auth failed:{hint}") from exc
            return False

    def ensure_sync_folder(self) -> str:
        """Find or create the 'VAF Sync' folder in the Drive root."""
        # Search for existing folder
        q = (
            f"name = '{SYNC_FOLDER_NAME}' and 'root' in parents "
            f"and mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        try:
            resp = self._get(
                f"{API_BASE}/files",
                params={"q": q, "fields": "files(id,name)", "pageSize": 1},
            )
            files = resp.json().get("files", [])
            if files:
                self._sync_folder_id = files[0]["id"]
                logger.debug("Found existing sync folder: %s", self._sync_folder_id)
                return self._sync_folder_id
        except requests.RequestException as exc:
            logger.error("Error searching for sync folder: %s", exc)
            raise

        # Create folder
        metadata = {
            "name": SYNC_FOLDER_NAME,
            "mimeType": FOLDER_MIME,
            "parents": ["root"],
        }
        try:
            resp = self._post(
                f"{API_BASE}/files",
                json=metadata,
                params={"fields": "id"},
            )
            self._sync_folder_id = resp.json()["id"]
            logger.info("Created sync folder: %s", self._sync_folder_id)
            return self._sync_folder_id
        except requests.RequestException as exc:
            logger.error("Failed to create sync folder: %s", exc)
            raise

    def list_files(self, folder_path: str = "/") -> List[CloudFileMetadata]:
        """List files inside the sync folder or a subfolder."""
        folder_id = self._resolve_folder_id(folder_path)

        results: List[CloudFileMetadata] = []
        page_token: Optional[str] = None
        q = f"'{folder_id}' in parents and trashed = false"

        while True:
            params = {
                "q": q,
                "fields": FILE_LIST_FIELDS,
                "pageSize": 1000,
            }
            if page_token:
                params["pageToken"] = page_token

            try:
                resp = self._get(f"{API_BASE}/files", params=params)
                data = resp.json()
            except requests.RequestException as exc:
                logger.error("Failed to list files in %s: %s", folder_path, exc)
                raise

            for item in data.get("files", []):
                results.append(self._parse_file(item, folder_path))

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        logger.debug("Listed %d items in %s", len(results), folder_path)
        return results

    def list_folder_by_id(self, folder_id: str, parent_path: str = "/") -> List[CloudFileMetadata]:
        """List contents of any folder by Drive ID. Use for cloud-only browsing (full Drive, no sync)."""
        results: List[CloudFileMetadata] = []
        page_token: Optional[str] = None
        q = f"'{folder_id}' in parents and trashed = false"

        while True:
            params = {
                "q": q,
                "fields": FILE_LIST_FIELDS,
                "pageSize": 500,
            }
            if page_token:
                params["pageToken"] = page_token

            try:
                resp = self._get(f"{API_BASE}/files", params=params)
                data = resp.json()
            except requests.RequestException as exc:
                logger.error("Failed to list folder %s: %s", folder_id, exc)
                raise

            for item in data.get("files", []):
                results.append(self._parse_file(item, parent_path))

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return results

    def upload_file(self, local_path: Path, remote_path: str) -> CloudFileMetadata:
        """Upload a file to the sync folder. Uses multipart for small files, resumable for large."""
        file_size = local_path.stat().st_size
        file_name = local_path.name

        # Determine parent folder ID
        parent_dir = "/".join(remote_path.strip("/").split("/")[:-1])
        parent_id = self._resolve_folder_id(parent_dir) if parent_dir else self._sync_folder_id

        if file_size < MULTIPART_THRESHOLD:
            return self._upload_multipart(local_path, file_name, parent_id, remote_path)
        else:
            return self._upload_resumable(local_path, file_name, file_size, parent_id, remote_path)

    def _upload_multipart(
        self, local_path: Path, file_name: str, parent_id: str, remote_path: str
    ) -> CloudFileMetadata:
        """Upload a small file using multipart upload."""
        import mimetypes

        mime_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
        metadata = {"name": file_name, "parents": [parent_id]}

        # Build multipart body manually for the Drive API
        boundary = "vaf_sync_boundary"
        body = io.BytesIO()

        # Metadata part
        body.write(f"--{boundary}\r\n".encode())
        body.write(b"Content-Type: application/json; charset=UTF-8\r\n\r\n")
        body.write(json.dumps(metadata).encode())
        body.write(b"\r\n")

        # File content part
        body.write(f"--{boundary}\r\n".encode())
        body.write(f"Content-Type: {mime_type}\r\n\r\n".encode())
        body.write(local_path.read_bytes())
        body.write(f"\r\n--{boundary}--\r\n".encode())

        headers = self._headers()
        headers["Content-Type"] = f"multipart/related; boundary={boundary}"

        try:
            resp = requests.post(
                f"{UPLOAD_BASE}/files",
                headers=headers,
                data=body.getvalue(),
                params={"uploadType": "multipart", "fields": FILE_FIELDS},
                timeout=60,
            )
            resp.raise_for_status()
            item = resp.json()
            logger.info("Uploaded %s (%d bytes) via multipart", file_name, local_path.stat().st_size)
            return self._parse_file(item, "/".join(remote_path.strip("/").split("/")[:-1]) or "/")
        except requests.RequestException as exc:
            logger.error("Multipart upload failed for %s: %s", file_name, exc)
            raise

    def _upload_resumable(
        self, local_path: Path, file_name: str, file_size: int,
        parent_id: str, remote_path: str,
    ) -> CloudFileMetadata:
        """Upload a large file using resumable upload."""
        import mimetypes

        mime_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
        metadata = {"name": file_name, "parents": [parent_id]}

        # Initiate resumable session
        headers = self._headers()
        headers["Content-Type"] = "application/json; charset=UTF-8"
        headers["X-Upload-Content-Type"] = mime_type
        headers["X-Upload-Content-Length"] = str(file_size)

        try:
            init_resp = requests.post(
                f"{UPLOAD_BASE}/files",
                headers=headers,
                json=metadata,
                params={"uploadType": "resumable", "fields": FILE_FIELDS},
                timeout=30,
            )
            init_resp.raise_for_status()
            upload_url = init_resp.headers["Location"]
        except requests.RequestException as exc:
            logger.error("Failed to initiate resumable upload for %s: %s", file_name, exc)
            raise

        # Upload chunks
        offset = 0
        with open(local_path, "rb") as fh:
            while offset < file_size:
                chunk = fh.read(RESUMABLE_CHUNK_SIZE)
                chunk_size = len(chunk)
                end = offset + chunk_size - 1

                chunk_headers = {
                    "Content-Length": str(chunk_size),
                    "Content-Range": f"bytes {offset}-{end}/{file_size}",
                }

                try:
                    resp = requests.put(
                        upload_url, headers=chunk_headers, data=chunk, timeout=120,
                    )
                    if resp.status_code in (200, 201):
                        item = resp.json()
                        logger.info(
                            "Resumable upload complete for %s (%d bytes)", file_name, file_size
                        )
                        return self._parse_file(
                            item, "/".join(remote_path.strip("/").split("/")[:-1]) or "/"
                        )
                    elif resp.status_code == 308:
                        # Incomplete — continue
                        offset += chunk_size
                    else:
                        resp.raise_for_status()
                except requests.RequestException as exc:
                    logger.error(
                        "Resumable upload chunk failed at offset %d for %s: %s",
                        offset, file_name, exc,
                    )
                    raise

        raise RuntimeError(f"Resumable upload finished loop without completion for {file_name}")

    def download_file(self, file_id: str, local_path: Path) -> Path:
        """Download a file by its Drive ID. Handles native Google formats via export."""
        meta = self.get_file_metadata(file_id)
        if not meta:
            raise FileNotFoundError(f"File {file_id} not found")

        mime = meta.mime_type or ""
        # Native Google formats must be exported, not downloaded
        GOOGLE_NATIVE = {
            "application/vnd.google-apps.document": "text/plain",
            "application/vnd.google-apps.spreadsheet": "text/csv",
            "application/vnd.google-apps.presentation": "text/plain",
            "application/vnd.google-apps.drawing": "application/pdf",
        }
        if mime in GOOGLE_NATIVE:
            export_mime = GOOGLE_NATIVE[mime]
            try:
                resp = requests.get(
                    f"{API_BASE}/files/{file_id}/export",
                    headers=self._headers(),
                    params={"mimeType": export_mime},
                    stream=True,
                    timeout=120,
                )
                resp.raise_for_status()
                local_path.parent.mkdir(parents=True, exist_ok=True)
                with open(local_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=8192):
                        fh.write(chunk)
                logger.info("Exported %s to %s (mime=%s)", file_id, local_path, export_mime)
                return local_path
            except requests.RequestException as exc:
                logger.error("Export failed for %s: %s", file_id, exc)
                raise

        # Regular file: download
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            resp = requests.get(
                f"{API_BASE}/files/{file_id}",
                headers=self._headers(),
                params={"alt": "media"},
                stream=True,
                timeout=120,
            )
            resp.raise_for_status()

            with open(local_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    fh.write(chunk)

            logger.info("Downloaded file %s to %s", file_id, local_path)
            return local_path
        except requests.RequestException as exc:
            logger.error("Failed to download file %s: %s", file_id, exc)
            raise

    def delete_file(self, file_id: str) -> bool:
        """Move a file to trash (soft delete)."""
        try:
            resp = requests.patch(
                f"{API_BASE}/files/{file_id}",
                headers=self._headers(),
                json={"trashed": True},
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("Trashed file %s", file_id)
            return True
        except requests.RequestException as exc:
            logger.error("Failed to trash file %s: %s", file_id, exc)
            return False

    def get_file_metadata(self, file_id: str) -> Optional[CloudFileMetadata]:
        """Fetch metadata for a single file by ID."""
        try:
            resp = self._get(
                f"{API_BASE}/files/{file_id}",
                params={"fields": FILE_FIELDS},
            )
            return self._parse_file(resp.json())
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.debug("File %s not found", file_id)
                return None
            logger.error("Error fetching metadata for %s: %s", file_id, exc)
            raise

    def get_changes(self, cursor: Optional[str] = None) -> DeltaPage:
        """Retrieve incremental changes using the Drive Changes API."""
        if cursor is None:
            # Get start page token
            try:
                resp = self._get(
                    f"{API_BASE}/changes/startPageToken",
                    params={"fields": "startPageToken"},
                )
                return DeltaPage(cursor=resp.json()["startPageToken"], has_more=False)
            except requests.RequestException as exc:
                logger.error("Failed to get start page token: %s", exc)
                raise

        files: List[CloudFileMetadata] = []
        deleted_ids: List[str] = []

        try:
            resp = self._get(
                f"{API_BASE}/changes",
                params={
                    "pageToken": cursor,
                    "fields": f"nextPageToken,newStartPageToken,changes(removed,fileId,file({FILE_FIELDS}))",
                    "pageSize": 1000,
                    "includeRemoved": True,
                    "spaces": "drive",
                },
            )
            data = resp.json()
        except requests.RequestException as exc:
            logger.error("Failed to fetch changes: %s", exc)
            raise

        for change in data.get("changes", []):
            if change.get("removed"):
                deleted_ids.append(change["fileId"])
            elif "file" in change:
                f = change["file"]
                # Only include files within the sync folder
                if self._sync_folder_id and self._sync_folder_id in f.get("parents", []):
                    if f.get("trashed"):
                        deleted_ids.append(f["id"])
                    else:
                        files.append(self._parse_file(f))

        new_cursor = data.get("newStartPageToken") or data.get("nextPageToken")
        has_more = "nextPageToken" in data and "newStartPageToken" not in data

        return DeltaPage(files=files, deleted_ids=deleted_ids, cursor=new_cursor, has_more=has_more)
