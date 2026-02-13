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

logger = logging.getLogger("vaf.tools.cloud_storage")

TOOL_NAME = "cloud_storage"
TOOL_DESCRIPTION = (
    "Save, list, or retrieve files from the user's connected cloud storage "
    "(Google Drive, OneDrive, Dropbox, Nextcloud, iCloud). "
    "Use 'save' to upload a local file to the user's cloud sync folder. "
    "Use 'list' to see files in their cloud sync folder. "
    "Use 'retrieve' to download a file from cloud to local. "
    "Use 'status' to check sync status."
)

TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["save", "list", "retrieve", "status"],
            "description": "Action to perform",
        },
        "provider": {
            "type": "string",
            "description": "Cloud provider: google_drive, onedrive, dropbox, nextcloud, icloud. Omit to use the first connected provider.",
        },
        "file_path": {
            "type": "string",
            "description": "For 'save': local file path to upload. For 'retrieve': remote filename to download.",
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


def _get_sync_dir(username: str, provider: str) -> Path:
    base = Platform.data_dir() / "users" / username / "cloud_sync" / provider
    base.mkdir(parents=True, exist_ok=True)
    return base


def _get_first_connected_provider(username: str) -> Optional[str]:
    """Return the first connected provider for the user, or None."""
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
            return acct.get("provider")
    return None


def run_cloud_storage(action: str, provider: Optional[str] = None,
                      file_path: Optional[str] = None, remote_path: Optional[str] = None,
                      **kwargs: Any) -> str:
    """Execute the cloud_storage tool action."""
    username = _get_username()

    if not provider:
        provider = _get_first_connected_provider(username)
        if not provider:
            return "No cloud storage connected. Go to Settings → Connections to connect a cloud provider."

    if action == "save":
        return _action_save(username, provider, file_path, remote_path)
    elif action == "list":
        return _action_list(username, provider)
    elif action == "retrieve":
        return _action_retrieve(username, provider, file_path)
    elif action == "status":
        return _action_status(username, provider)
    else:
        return f"Unknown action: {action}. Use: save, list, retrieve, status."


def _action_save(username: str, provider: str, file_path: Optional[str], remote_path: Optional[str]) -> str:
    """Copy a local file into the sync directory for upload on next sync cycle."""
    if not file_path:
        return "file_path is required for 'save' action."

    source = Path(file_path).expanduser().resolve()
    if not source.exists():
        return f"File not found: {file_path}"
    if not source.is_file():
        return f"Not a file: {file_path}"

    sync_dir = _get_sync_dir(username, provider)
    dest_name = remote_path or source.name
    dest = sync_dir / dest_name
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        shutil.copy2(str(source), str(dest))
        return f"File saved to cloud sync folder: {dest_name}. It will be uploaded on the next sync cycle."
    except Exception as e:
        logger.error("[CloudStorage] Save failed: %s", e)
        return f"Failed to save file: {e}"


def _action_list(username: str, provider: str) -> str:
    """List files in the local sync directory."""
    sync_dir = _get_sync_dir(username, provider)
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


def _action_retrieve(username: str, provider: str, file_path: Optional[str]) -> str:
    """Download a file from cloud to the user's Downloads folder."""
    if not file_path:
        return "file_path is required for 'retrieve' action (the remote filename to download)."

    sync_dir = _get_sync_dir(username, provider)
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
