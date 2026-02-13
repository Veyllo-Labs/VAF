"""
Nextcloud cloud provider using WebDAV (PROPFIND, PUT, GET, DELETE, MKCOL).

Does not support delta sync — full listing is used for each sync cycle.
Connects via app-password credentials (no OAuth).
"""

import hashlib
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

import requests

from vaf.cloud.base import (
    AuthMethod,
    CloudFileMetadata,
    CloudProvider,
    SYNC_FOLDER_NAME,
)

logger = logging.getLogger("vaf.cloud.nextcloud")

DAV_NS = "DAV:"
OC_NS = "http://owncloud.org/ns"
NC_NS = "http://nextcloud.org/ns"

# PROPFIND body requesting the fields we care about
PROPFIND_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:prop>
    <d:getlastmodified/>
    <d:getcontentlength/>
    <d:getcontenttype/>
    <d:getetag/>
    <d:resourcetype/>
    <oc:fileid/>
    <oc:checksums/>
    <oc:size/>
  </d:prop>
</d:propfind>"""


class NextcloudProvider(CloudProvider):
    """Nextcloud provider using pure WebDAV HTTP requests."""

    provider_name = "nextcloud"
    auth_method = AuthMethod.APP_PASSWORD
    supports_delta = False
    max_upload_size = 16 * 1024 * 1024 * 1024  # 16 GB (Nextcloud chunked)

    def __init__(self, username: str, account_id: str):
        super().__init__(username, account_id)
        self._server_url: Optional[str] = None
        self._webdav_username: Optional[str] = None
        self._password: Optional[str] = None
        self._dav_base: Optional[str] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _auth(self) -> requests.auth.HTTPBasicAuth:
        return requests.auth.HTTPBasicAuth(self._webdav_username, self._password)

    def _dav_url(self, relative: str = "") -> str:
        """Build the full WebDAV URL scoped to the sync folder."""
        clean = relative.strip("/")
        base = f"{self._dav_base}/{SYNC_FOLDER_NAME}"
        if clean:
            return f"{base}/{clean}"
        return base

    def _propfind(self, url: str, depth: str = "1") -> ET.Element:
        """Execute a PROPFIND request and return the parsed XML root."""
        resp = requests.request(
            "PROPFIND",
            url,
            auth=self._auth(),
            headers={
                "Content-Type": "application/xml; charset=UTF-8",
                "Depth": depth,
            },
            data=PROPFIND_BODY,
            timeout=30,
        )
        resp.raise_for_status()
        return ET.fromstring(resp.content)

    def _parse_multistatus(self, root: ET.Element, base_url: str) -> List[CloudFileMetadata]:
        """Parse a WebDAV multistatus XML response into metadata objects."""
        results: List[CloudFileMetadata] = []

        for response in root.findall(f"{{{DAV_NS}}}response"):
            href_el = response.find(f"{{{DAV_NS}}}href")
            if href_el is None or href_el.text is None:
                continue

            href = href_el.text.rstrip("/")
            propstat = response.find(f"{{{DAV_NS}}}propstat")
            if propstat is None:
                continue

            prop = propstat.find(f"{{{DAV_NS}}}prop")
            if prop is None:
                continue

            # Check if this is a collection (folder)
            resourcetype = prop.find(f"{{{DAV_NS}}}resourcetype")
            is_folder = (
                resourcetype is not None
                and resourcetype.find(f"{{{DAV_NS}}}collection") is not None
            )

            # Skip the sync folder root itself
            if href.rstrip("/").endswith(SYNC_FOLDER_NAME):
                continue

            # Extract name from href
            name = href.split("/")[-1]
            if not name:
                continue

            # Compute relative path within the sync folder
            sync_marker = f"/{SYNC_FOLDER_NAME}/"
            idx = href.find(sync_marker)
            if idx >= 0:
                rel_path = "/" + href[idx + len(sync_marker):]
            else:
                rel_path = "/" + name

            # Parse fields
            file_id_el = prop.find(f"{{{OC_NS}}}fileid")
            file_id = file_id_el.text if file_id_el is not None and file_id_el.text else href

            etag_el = prop.find(f"{{{DAV_NS}}}getetag")
            etag = etag_el.text.strip('"') if etag_el is not None and etag_el.text else None

            size_el = prop.find(f"{{{DAV_NS}}}getcontentlength")
            if size_el is None or not size_el.text:
                size_el = prop.find(f"{{{OC_NS}}}size")
            size = int(size_el.text) if size_el is not None and size_el.text else 0

            modified_el = prop.find(f"{{{DAV_NS}}}getlastmodified")
            mod_time = 0.0
            if modified_el is not None and modified_el.text:
                try:
                    from email.utils import parsedate_to_datetime
                    mod_time = parsedate_to_datetime(modified_el.text).timestamp()
                except (ValueError, TypeError):
                    pass

            content_type_el = prop.find(f"{{{DAV_NS}}}getcontenttype")
            mime_type = (
                content_type_el.text
                if content_type_el is not None and content_type_el.text
                else None
            )

            checksums_el = prop.find(f"{{{OC_NS}}}checksums")
            content_hash = None
            if checksums_el is not None:
                checksum_el = checksums_el.find(f"{{{OC_NS}}}checksum")
                if checksum_el is not None and checksum_el.text:
                    content_hash = checksum_el.text

            from urllib.parse import unquote
            results.append(CloudFileMetadata(
                file_id=str(file_id),
                name=unquote(name),
                path=unquote(rel_path),
                size=size,
                modified_time=mod_time,
                content_hash=content_hash,
                etag=etag,
                is_folder=is_folder,
                mime_type=mime_type,
                extra={"href": href},
            ))

        return results

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    def authenticate(self) -> bool:
        """Load WebDAV credentials and validate connectivity."""
        from vaf.cloud.credential_cloud import get_cloud_credentials

        creds = get_cloud_credentials(self.account_id, "nextcloud", self.username)
        if not creds:
            logger.error("No Nextcloud credentials found for %s", self.username)
            return False

        self._server_url = creds["url"].rstrip("/")
        self._webdav_username = creds["webdav_username"]
        self._password = creds["password"]
        self._dav_base = f"{self._server_url}/remote.php/dav/files/{self._webdav_username}"

        # Validate by issuing a PROPFIND on the root
        try:
            root = self._propfind(self._dav_base, depth="0")
            logger.info(
                "Authenticated to Nextcloud at %s as %s",
                self._server_url, self._webdav_username,
            )
            return True
        except requests.RequestException as exc:
            logger.error("Nextcloud auth validation failed: %s", exc)
            self._server_url = None
            return False

    def ensure_sync_folder(self) -> str:
        """Create the 'VAF Sync' folder via MKCOL if it does not exist."""
        url = self._dav_url()

        # Check if already exists
        try:
            self._propfind(url, depth="0")
            logger.debug("Sync folder already exists")
            return url
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                logger.error("Error checking for sync folder: %s", exc)
                raise

        # Create via MKCOL
        try:
            resp = requests.request(
                "MKCOL",
                url,
                auth=self._auth(),
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("Created sync folder at %s", url)
            return url
        except requests.RequestException as exc:
            logger.error("Failed to create sync folder: %s", exc)
            raise

    def list_files(self, folder_path: str = "/") -> List[CloudFileMetadata]:
        """List files in the sync folder or a subfolder via PROPFIND."""
        url = self._dav_url(folder_path)

        try:
            root = self._propfind(url, depth="1")
            results = self._parse_multistatus(root, url)
            logger.debug("Listed %d items in %s", len(results), folder_path)
            return results
        except requests.RequestException as exc:
            logger.error("Failed to list files in %s: %s", folder_path, exc)
            raise

    def upload_file(self, local_path: Path, remote_path: str) -> CloudFileMetadata:
        """Upload a file via PUT."""
        url = self._dav_url(remote_path)

        # Ensure parent directories exist
        parts = remote_path.strip("/").split("/")
        if len(parts) > 1:
            self._ensure_parents(parts[:-1])

        try:
            with open(local_path, "rb") as fh:
                resp = requests.put(
                    url,
                    auth=self._auth(),
                    data=fh,
                    timeout=120,
                )
            resp.raise_for_status()
            logger.info("Uploaded %s (%d bytes)", local_path.name, local_path.stat().st_size)
        except requests.RequestException as exc:
            logger.error("Upload failed for %s: %s", local_path.name, exc)
            raise

        # Fetch metadata of the uploaded file
        metadata = self.get_file_metadata(url)
        if metadata is None:
            # Construct minimal metadata as fallback
            import mimetypes
            return CloudFileMetadata(
                file_id=url,
                name=local_path.name,
                path=f"/{remote_path.strip('/')}",
                size=local_path.stat().st_size,
                modified_time=time.time(),
                mime_type=mimetypes.guess_type(str(local_path))[0],
            )
        return metadata

    def _ensure_parents(self, path_parts: List[str]) -> None:
        """Create intermediate folders if they do not exist."""
        current = ""
        for part in path_parts:
            current = f"{current}/{part}" if current else part
            url = self._dav_url(current)
            try:
                self._propfind(url, depth="0")
            except requests.HTTPError:
                try:
                    requests.request("MKCOL", url, auth=self._auth(), timeout=15).raise_for_status()
                    logger.debug("Created intermediate folder %s", current)
                except requests.RequestException:
                    pass  # May already exist due to race condition

    def download_file(self, file_id: str, local_path: Path) -> Path:
        """Download a file by its WebDAV URL (used as file_id) or relative path."""
        local_path.parent.mkdir(parents=True, exist_ok=True)

        url = file_id if file_id.startswith("http") else self._dav_url(file_id)

        try:
            resp = requests.get(url, auth=self._auth(), stream=True, timeout=120)
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
        """Delete a file via the WebDAV DELETE method."""
        url = file_id if file_id.startswith("http") else self._dav_url(file_id)

        try:
            resp = requests.delete(url, auth=self._auth(), timeout=15)
            resp.raise_for_status()
            logger.info("Deleted %s", file_id)
            return True
        except requests.RequestException as exc:
            logger.error("Failed to delete %s: %s", file_id, exc)
            return False

    def get_file_metadata(self, file_id: str) -> Optional[CloudFileMetadata]:
        """Get metadata for a single file via PROPFIND with Depth: 0."""
        url = file_id if file_id.startswith("http") else self._dav_url(file_id)

        try:
            root = self._propfind(url, depth="0")
            items = self._parse_multistatus(root, url)
            if items:
                return items[0]
            return None
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.debug("File %s not found", file_id)
                return None
            logger.error("Error fetching metadata for %s: %s", file_id, exc)
            raise
