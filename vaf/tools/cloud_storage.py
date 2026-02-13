"""
Cloud Storage agent tool — allows the VAF agent to save/list/retrieve files
from the user's connected cloud storage.
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from vaf.core.config import Config
from vaf.core.platform import Platform
from vaf.tools.base import BaseTool

logger = logging.getLogger("vaf.tools.cloud_storage")

TOOL_NAME = "cloud_storage"
TOOL_DESCRIPTION = (
    "Save, list, retrieve, browse, download, or read files in the user's connected cloud storage "
    "(Google Drive, OneDrive, Dropbox, Nextcloud, iCloud). "
    "Use 'browse' to navigate the full cloud (Drive root and folders). "
    "Use 'download' to download a file by file_id to Downloads. "
    "Use 'read' to read a document's content without keeping it locally (PDF, Word, Google Docs, etc.). "
    "Use 'save' to upload a local file to the cloud sync folder. "
    "Use 'list' for files in the local VAF Sync folder. "
    "Use 'retrieve' for files already synced locally. "
    "Use 'status' to check sync status."
)

TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["save", "list", "retrieve", "status", "browse", "download", "read"],
            "description": "Action to perform",
        },
        "provider": {
            "type": "string",
            "description": "Cloud provider: google_drive, onedrive, dropbox, nextcloud, icloud. Omit to use the first connected provider.",
        },
        "file_id": {
            "type": "string",
            "description": "For 'download' and 'read': the cloud file ID (from browse output).",
        },
        "folder_id": {
            "type": "string",
            "description": "For 'browse': folder ID to list (use 'root' for Drive root). Omit for root.",
        },
        "file_path": {
            "type": "string",
            "description": "For 'save': local file path to upload. For 'retrieve': remote filename in sync folder.",
        },
        "remote_path": {
            "type": "string",
            "description": "Destination path within VAF Sync folder (e.g., 'reports/analysis.pdf'). Defaults to file name.",
        },
    },
    "required": ["action"],
}


def _get_username() -> str:
    """Get current username from environment or config."""
    return os.environ.get("VAF_USERNAME") or Config.get("local_admin_username", "admin")


def _get_sync_dir(username: str, account_id: str) -> Path:
    """Local sync dir must match cloud_routes._local_sync_dir (uses account_id, not provider)."""
    base = Platform.data_dir() / "users" / username / "cloud_sync" / account_id
    base.mkdir(parents=True, exist_ok=True)
    return base


def _get_first_connected_account(username: str) -> Optional[tuple[str, str]]:
    """Return (provider, account_id) for the first connected account, or None."""
    admin_user = Config.get("local_admin_username", "admin")
    if username == admin_user:
        cloud_config = Config.get("cloud_config") or {}
        accounts = cloud_config.get("accounts", [])
    else:
        by_user = Config.get("cloud_config_by_user") or {}
        user_cfg = by_user.get(username, {})
        accounts = user_cfg.get("accounts", [])

    for acct in accounts:
        if acct.get("sync_enabled", True):
            provider = acct.get("provider")
            account_id = acct.get("account_id")
            if provider and account_id:
                return (provider, account_id)
    return None


def _create_provider(provider_name: str, username: str, account_id: str):
    """Instantiate a cloud provider by name."""
    from vaf.cloud.google_drive import GoogleDriveProvider
    from vaf.cloud.onedrive import OneDriveProvider
    from vaf.cloud.dropbox_provider import DropboxProvider
    from vaf.cloud.nextcloud import NextcloudProvider
    from vaf.cloud.icloud import ICloudProvider

    PROVIDERS = {
        "google_drive": GoogleDriveProvider,
        "onedrive": OneDriveProvider,
        "dropbox": DropboxProvider,
        "nextcloud": NextcloudProvider,
        "icloud": ICloudProvider,
    }
    cls = PROVIDERS.get(provider_name)
    if not cls:
        raise ValueError(f"Unknown provider: {provider_name}")
    return cls(username=username, account_id=account_id)


def run_cloud_storage(action: str, provider: Optional[str] = None,
                      folder_id: Optional[str] = None, file_path: Optional[str] = None,
                      remote_path: Optional[str] = None, file_id: Optional[str] = None,
                      **kwargs: Any) -> str:
    """Execute the cloud_storage tool action."""
    username = _get_username()
    first = _get_first_connected_account(username)
    if not first:
        return "No cloud storage connected. Go to Settings → Connections to connect a cloud provider."
    default_provider, default_account_id = first

    account_id = default_account_id
    if provider and provider != default_provider:
        acct = _get_account_by_provider(username, provider)
        if not acct:
            return f"No {provider} account connected. Use 'status' to see connected providers."
        account_id = acct.get("account_id", default_account_id)

    effective_provider = provider or default_provider
    file_id = file_id or kwargs.get("file_id")
    if action == "save":
        return _action_save(username, account_id, effective_provider, file_path, remote_path)
    elif action == "list":
        return _action_list(username, account_id, effective_provider)
    elif action == "retrieve":
        return _action_retrieve(username, account_id, file_path)
    elif action == "status":
        return _action_status(username, effective_provider)
    elif action == "browse":
        return _action_browse(username, account_id, effective_provider, folder_id or "root")
    elif action == "download":
        return _action_download(username, account_id, effective_provider, file_id)
    elif action == "read":
        return _action_read(username, account_id, effective_provider, file_id)
    else:
        return f"Unknown action: {action}. Use: save, list, retrieve, status, browse, download, read."


def _get_account_by_provider(username: str, provider: str) -> Optional[dict]:
    """Return first account matching provider."""
    admin_user = Config.get("local_admin_username", "admin")
    if username == admin_user:
        accounts = (Config.get("cloud_config") or {}).get("accounts", [])
    else:
        user_cfg = (Config.get("cloud_config_by_user") or {}).get(username, {})
        accounts = user_cfg.get("accounts", [])
    for acct in accounts:
        if acct.get("provider") == provider and acct.get("sync_enabled", True):
            return acct
    return None


def _action_save(username: str, account_id: str, provider: str, file_path: Optional[str], remote_path: Optional[str]) -> str:
    """Copy a local file into the sync directory for upload on next sync cycle."""
    if not file_path:
        return "file_path is required for 'save' action."

    source = Path(file_path).expanduser().resolve()
    if not source.exists():
        return f"File not found: {file_path}"
    if not source.is_file():
        return f"Not a file: {file_path}"

    sync_dir = _get_sync_dir(username, account_id)
    dest_name = remote_path or source.name
    dest = sync_dir / dest_name
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        shutil.copy2(str(source), str(dest))
        return f"File saved to cloud sync folder: {dest_name}. It will be uploaded on the next sync cycle."
    except Exception as e:
        logger.error("[CloudStorage] Save failed: %s", e)
        return f"Failed to save file: {e}"


def _action_browse(username: str, account_id: str, provider: str, folder_id: str) -> str:
    """Browse cloud contents at a folder (cloud-only, no local storage)."""
    try:
        prov = _create_provider(provider, username, account_id)
        if not prov.authenticate():
            return f"Authentication failed for {provider}. Reconnect the account in Settings."
        items = prov.list_folder_by_id(folder_id, "/")
    except NotImplementedError:
        return f"{provider} does not support cloud browsing yet. Use 'list' for synced files."
    except ValueError as e:
        return str(e)
    except Exception as e:
        logger.error("[CloudStorage] Browse failed: %s", e)
        return f"Browse failed: {e}"

    folders = [f for f in items if f.is_folder]
    files = [f for f in items if not f.is_folder]
    lines = []

    def _fmt_size(b: int) -> str:
        if b < 1024:
            return f"{b} B"
        if b < 1024 * 1024:
            return f"{b / 1024:.1f} KB"
        return f"{b / (1024 * 1024):.1f} MB"

    if folders:
        lines.append("Folders (use browse with folder_id=<id> to enter):")
        for f in sorted(folders, key=lambda x: x.name.lower()):
            lines.append(f"  [F] {f.name}  (id={f.file_id})")
    if files:
        lines.append("Files:")
        for f in sorted(files, key=lambda x: x.name.lower()):
            lines.append(f"  [ ] {f.name}  ({_fmt_size(f.size)})")

    if not lines:
        return f"Folder is empty."
    return "\n".join(lines)


def _action_list(username: str, account_id: str, provider: str) -> str:
    """List files in the local sync directory."""
    sync_dir = _get_sync_dir(username, account_id)
    files = []
    try:
        for p in sorted(sync_dir.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                rel = p.relative_to(sync_dir)
                size_kb = p.stat().st_size / 1024
                files.append(f"  {rel} ({size_kb:.1f} KB)")
    except Exception as e:
        return f"Error listing files: {e}"

    if not files:
        return f"No files in {provider} sync folder."

    return f"Files in {provider} sync folder ({len(files)}):\n" + "\n".join(files)


def _action_download(username: str, account_id: str, provider: str, file_id: Optional[str]) -> str:
    """Download a file from cloud by file_id to user's Downloads folder."""
    if not file_id:
        return "file_id is required for 'download' action. Get it from browse (e.g. id=xxx)."

    try:
        prov = _create_provider(provider, username, account_id)
        if not prov.authenticate():
            return f"Authentication failed for {provider}. Reconnect the account in Settings."

        meta = prov.get_file_metadata(file_id)
        if not meta:
            return f"File not found: {file_id}"
        if meta.is_folder:
            return "Cannot download a folder. Use browse to list folder contents."

        downloads = Platform.downloads_dir()
        dest = downloads / meta.name
        prov.download_file(file_id, dest)
        return f"Downloaded '{meta.name}' to {dest}"
    except NotImplementedError:
        return f"{provider} does not support download by file_id."
    except Exception as e:
        logger.error("[CloudStorage] Download failed: %s", e)
        return f"Download failed: {e}"


def _action_read(username: str, account_id: str, provider: str, file_id: Optional[str]) -> str:
    """Download to temp, extract text with Librarian, return content, then delete temp (no local copy)."""
    if not file_id:
        return "file_id is required for 'read' action. Get it from browse (e.g. id=xxx)."

    import tempfile

    try:
        prov = _create_provider(provider, username, account_id)
        if not prov.authenticate():
            return f"Authentication failed for {provider}. Reconnect the account in Settings."

        meta = prov.get_file_metadata(file_id)
        if not meta:
            return f"File not found: {file_id}"
        if meta.is_folder:
            return "Cannot read a folder. Use browse to list folder contents."

        suffix = Path(meta.name).suffix or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = Path(tmp.name)
        try:
            prov.download_file(file_id, tmp_path)
            from vaf.tools.librarian import LibrarianTool
            librarian = LibrarianTool()
            content = librarian._read_file(tmp_path, enable_chunking=True)
            if not content or not content.strip():
                return f"Could not extract text from '{meta.name}'. File may be binary or empty."
            return f"### Content of {meta.name}\n\n{content}"
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
    except NotImplementedError:
        return f"{provider} does not support read by file_id."
    except Exception as e:
        logger.error("[CloudStorage] Read failed: %s", e)
        return f"Read failed: {e}"


def _action_retrieve(username: str, account_id: str, file_path: Optional[str]) -> str:
    """Download a file from cloud to the user's Downloads folder."""
    if not file_path:
        return "file_path is required for 'retrieve' action (the remote filename to download)."

    sync_dir = _get_sync_dir(username, account_id)
    source = sync_dir / file_path
    if not source.exists():
        return f"File not found in sync folder: {file_path}. Use 'list' to see available files."

    downloads = Platform.downloads_dir()
    dest = downloads / source.name
    try:
        shutil.copy2(str(source), str(dest))
        return f"File downloaded to: {dest}"
    except Exception as e:
        return f"Failed to retrieve file: {e}"


def _action_status(username: str, provider: str) -> str:
    """Return sync status for the provider."""
    admin_user = Config.get("local_admin_username", "admin")
    if username == admin_user:
        cloud_config = Config.get("cloud_config") or {}
        accounts = cloud_config.get("accounts", [])
    else:
        by_user = Config.get("cloud_config_by_user") or {}
        user_cfg = by_user.get(username, {})
        accounts = user_cfg.get("accounts", [])

    for acct in accounts:
        if acct.get("provider") == provider:
            last = acct.get("last_synced_at")
            if last:
                import time
                ago = int(time.time() - last)
                if ago < 60:
                    ago_str = f"{ago}s ago"
                elif ago < 3600:
                    ago_str = f"{ago // 60}m ago"
                else:
                    ago_str = f"{ago // 3600}h ago"
            else:
                ago_str = "never"
            enabled = "enabled" if acct.get("sync_enabled", True) else "disabled"
            return f"{provider}: {enabled}, last sync: {ago_str}, account: {acct.get('display_name', acct.get('account_id', '?'))}"

    return f"No {provider} account connected."


class CloudStorageTool(BaseTool):
    """Tool for browsing and managing connected cloud storage (Google Drive, OneDrive, etc.)."""

    name = TOOL_NAME
    description = TOOL_DESCRIPTION
    parameters = TOOL_PARAMETERS

    def run(self, **kwargs) -> str:
        return run_cloud_storage(
            action=kwargs.get("action", "browse"),
            provider=kwargs.get("provider"),
            folder_id=kwargs.get("folder_id"),
            file_path=kwargs.get("file_path"),
            remote_path=kwargs.get("remote_path"),
            file_id=kwargs.get("file_id"),
            **{k: v for k, v in kwargs.items() if k not in ("action", "provider", "folder_id", "file_path", "remote_path", "file_id")},
        )
