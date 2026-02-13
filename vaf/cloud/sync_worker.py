"""
Background sync worker — periodically syncs all enabled cloud accounts.

Registered as an asyncio task at server startup, runs alongside email auto-sync.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import List, Tuple

from vaf.core.config import Config
from vaf.core.platform import Platform

logger = logging.getLogger("vaf.cloud.sync_worker")


def _get_user_sync_dir(username: str, provider: str) -> Path:
    """Return the local sync directory for a user + provider."""
    base = Platform.data_dir() / "users" / username / "cloud_sync" / provider
    base.mkdir(parents=True, exist_ok=True)
    return base


def _collect_enabled_accounts() -> List[Tuple[str, str, str]]:
    """
    Collect all enabled cloud accounts across all users.
    Returns list of (username, provider_name, account_id).
    """
    results: List[Tuple[str, str, str]] = []

    # Local admin accounts
    cloud_config = Config.get("cloud_config")
    if cloud_config and isinstance(cloud_config, dict):
        for acct in cloud_config.get("accounts", []):
            if acct.get("sync_enabled", True):
                username = Config.get("local_admin_username", "admin")
                results.append((username, acct["provider"], acct["account_id"]))

    # Per-user accounts
    by_user = Config.get("cloud_config_by_user") or {}
    for username, user_cfg in by_user.items():
        if not isinstance(user_cfg, dict):
            continue
        for acct in user_cfg.get("accounts", []):
            if acct.get("sync_enabled", True):
                results.append((username, acct["provider"], acct["account_id"]))

    return results


def _create_provider(provider_name: str, username: str, account_id: str):
    """Create a provider instance by name."""
    from vaf.cloud.google_drive import GoogleDriveProvider
    from vaf.cloud.onedrive import OneDriveProvider
    from vaf.cloud.dropbox_provider import DropboxProvider
    from vaf.cloud.nextcloud import NextcloudProvider
    from vaf.cloud.icloud import ICloudProvider

    providers = {
        "google_drive": GoogleDriveProvider,
        "onedrive": OneDriveProvider,
        "dropbox": DropboxProvider,
        "nextcloud": NextcloudProvider,
        "icloud": ICloudProvider,
    }
    cls = providers.get(provider_name)
    if not cls:
        raise ValueError(f"Unknown cloud provider: {provider_name}")
    return cls(username=username, account_id=account_id)


def _run_sync_for_account(username: str, provider_name: str, account_id: str) -> dict:
    """Run a single sync cycle for one account. Blocking — call via asyncio.to_thread."""
    from vaf.cloud.sync_engine import SyncEngine
    from vaf.cloud.sync_manifest import SyncManifest

    provider = _create_provider(provider_name, username, account_id)
    if not provider.authenticate():
        logger.warning("[CloudSync] Auth failed for %s/%s", username[:4] + "***", provider_name)
        return {"error": "auth_failed"}

    local_dir = _get_user_sync_dir(username, provider_name)
    manifest = SyncManifest(username, provider_name)
    max_size = Config.get("cloud_sync_max_file_size_mb", 100) * 1024 * 1024
    conflict = Config.get("cloud_sync_conflict_resolution", "last_write_wins")

    engine = SyncEngine(
        provider=provider,
        manifest=manifest,
        local_sync_dir=local_dir,
        max_file_size=max_size,
        conflict_strategy=conflict,
    )
    result = engine.full_sync()

    # Update last_synced_at in config
    _update_last_synced(username, account_id)

    return {
        "uploaded": result.uploaded,
        "downloaded": result.downloaded,
        "deleted_local": result.deleted_local,
        "deleted_remote": result.deleted_remote,
        "conflicts": result.conflicts,
        "errors": result.errors,
    }


def _update_last_synced(username: str, account_id: str) -> None:
    """Update last_synced_at timestamp in config for the account."""
    try:
        admin_user = Config.get("local_admin_username", "admin")
        now = time.time()

        if username == admin_user:
            cloud_config = Config.get("cloud_config") or {"accounts": []}
            for acct in cloud_config.get("accounts", []):
                if acct.get("account_id") == account_id:
                    acct["last_synced_at"] = now
            Config.set("cloud_config", cloud_config)
        else:
            by_user = Config.get("cloud_config_by_user") or {}
            user_cfg = by_user.get(username, {"accounts": []})
            for acct in user_cfg.get("accounts", []):
                if acct.get("account_id") == account_id:
                    acct["last_synced_at"] = now
            by_user[username] = user_cfg
            Config.set("cloud_config_by_user", by_user)
    except Exception as e:
        logger.debug("[CloudSync] Failed to update last_synced_at: %s", e)


async def cloud_sync_loop() -> None:
    """Background loop that syncs all enabled cloud accounts periodically."""
    # Wait for server to stabilize
    await asyncio.sleep(120)

    while True:
        if not Config.get("cloud_sync_enabled", False):
            await asyncio.sleep(300)  # Check again in 5 min
            continue

        interval = max(Config.get("cloud_sync_interval_minutes", 15), 1) * 60
        accounts = _collect_enabled_accounts()

        for username, provider_name, account_id in accounts:
            try:
                result = await asyncio.to_thread(
                    _run_sync_for_account, username, provider_name, account_id
                )
                if result.get("error"):
                    logger.warning("[CloudSync] %s/%s: %s", username[:4] + "***", provider_name, result["error"])
                else:
                    total = result.get("uploaded", 0) + result.get("downloaded", 0)
                    if total > 0:
                        logger.info("[CloudSync] %s/%s: up=%d down=%d del=%d err=%d",
                                    username[:4] + "***", provider_name,
                                    result.get("uploaded", 0), result.get("downloaded", 0),
                                    result.get("deleted_local", 0) + result.get("deleted_remote", 0),
                                    result.get("errors", 0))
            except Exception as e:
                logger.warning("[CloudSync] Sync failed %s/%s: %s", username[:4] + "***", provider_name, e)

        await asyncio.sleep(interval)
