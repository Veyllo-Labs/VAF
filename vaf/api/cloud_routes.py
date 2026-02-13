"""
Cloud storage API: OAuth2 flow, account management, sync triggers, and conflict resolution.

Credentials are stored via credential_cloud (keyring or encrypted file);
config holds only account metadata keyed by username.
"""

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from vaf.core.config import Config
from vaf.core.platform import Platform

from vaf.cloud.oauth_cloud import (
    get_authorization_url,
    exchange_code_for_tokens,
    get_state_provider,
    get_state_redirect_base,
    is_cloud_oauth_configured,
    get_cloud_callback_redirect_uri,
)
from vaf.cloud.credential_cloud import (
    delete_cloud_credentials,
    get_cloud_credentials,
    set_cloud_webdav_credentials,
)
from vaf.cloud.sync_manifest import SyncManifest
from vaf.cloud.sync_engine import SyncEngine
from vaf.cloud.base import SYNC_FOLDER_NAME

logger = logging.getLogger("vaf.api.cloud")

router = APIRouter(prefix="/api/cloud", tags=["cloud"])

# ── Supported providers ───────────────────────────────────────────────────

OAUTH_PROVIDERS = ("google_drive", "onedrive", "dropbox", "icloud")
WEBDAV_PROVIDERS = ("nextcloud",)
ALL_PROVIDERS = OAUTH_PROVIDERS + WEBDAV_PROVIDERS

# Max file size for sync (100 MB)
DEFAULT_MAX_FILE_SIZE = 100 * 1024 * 1024


# ── Auth helper ───────────────────────────────────────────────────────────

def _get_current_username(request: Request) -> str:
    """Current user from auth middleware, or local admin. Used to scope cloud data per user."""
    from vaf.api.config_routes import get_current_username as get_username
    return get_username(request)


# ── Config helpers ────────────────────────────────────────────────────────

def _get_cloud_config(username: Optional[str] = None) -> Dict[str, Any]:
    """Return cloud config for the given user."""
    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
    if not username or username.strip().lower() == local_admin:
        raw = Config.get("cloud_config")
        if isinstance(raw, dict):
            return raw
        return {"accounts": []}
    by_user = Config.get("cloud_config_by_user") or {}
    cc = by_user.get(username.strip(), {}) if isinstance(by_user, dict) else {}
    return cc if isinstance(cc, dict) else {"accounts": []}


def _save_cloud_config(cc: Dict[str, Any], username: Optional[str] = None) -> None:
    """Save cloud config for the given user."""
    config = Config.load()
    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
    if not username or username.strip().lower() == local_admin:
        config["cloud_config"] = cc
    else:
        by_user = config.get("cloud_config_by_user") or {}
        if not isinstance(by_user, dict):
            by_user = {}
        by_user[username.strip()] = cc
        config["cloud_config_by_user"] = by_user
    Config.save(config)


def _find_account(cc: Dict[str, Any], account_id: str) -> Optional[Dict[str, Any]]:
    """Find an account by ID within a cloud config dict."""
    for acc in cc.get("accounts") or []:
        if acc.get("account_id") == account_id:
            return acc
    return None


def _get_cred_username(username: str) -> Optional[str]:
    """Return credential-store username: None for local admin, else the username."""
    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
    if username.strip().lower() == local_admin:
        return None
    return username


def _local_sync_dir(username: str, account_id: str) -> Path:
    """Return the local sync directory for a cloud account."""
    base = Platform.data_dir() / "users" / username / "cloud_sync" / account_id
    base.mkdir(parents=True, exist_ok=True)
    return base


# ── Provider factory ──────────────────────────────────────────────────────

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


# ── Pydantic models ──────────────────────────────────────────────────────

class WebDavConnectRequest(BaseModel):
    url: str
    username: str
    password: str


class ConflictResolveRequest(BaseModel):
    action: str  # keep_local | keep_remote | keep_both


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.get("/providers")
async def list_providers():
    """List available cloud storage providers and whether OAuth is configured for each."""
    providers = []
    for name in ALL_PROVIDERS:
        entry: Dict[str, Any] = {
            "id": name,
            "provider": name,
            "auth_method": "webdav" if name in WEBDAV_PROVIDERS else "oauth2",
        }
        if name in OAUTH_PROVIDERS:
            entry["oauth_configured"] = is_cloud_oauth_configured(name)
        providers.append(entry)
    return {"providers": providers}


@router.get("/accounts")
async def list_accounts(_username: str = Depends(_get_current_username)):
    """Return list of connected cloud accounts for the current user (metadata only)."""
    cc = _get_cloud_config(_username)
    accounts = cc.get("accounts") or []
    return {"accounts": accounts}


@router.get("/oauth/start")
async def oauth_start(
    request: Request,
    provider: str = "google_drive",
    redirect_base: Optional[str] = None,
):
    """Start OAuth2 flow for a cloud storage provider. Returns authorization URL and state.
    redirect_base: frontend origin (e.g. http://localhost:3000) so post-OAuth redirect matches the host the user used."""
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Provider must be one of: {', '.join(OAUTH_PROVIDERS)}")

    if not is_cloud_oauth_configured(provider):
        raise HTTPException(
            status_code=400,
            detail=f"OAuth is not configured for {provider}. An admin must set client ID and secret first.",
        )

    base_url = str(request.base_url).rstrip("/")
    redirect_uri = get_cloud_callback_redirect_uri(base_url)

    try:
        auth_url, state = get_authorization_url(provider, redirect_uri, redirect_base=redirect_base)
        return {"authorization_url": auth_url, "state": state, "redirect_uri": redirect_uri}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
):
    """OAuth callback. Exchanges code for tokens, stores credentials, redirects to frontend."""
    redirect_base = get_state_redirect_base(state) if state else None

    if error:
        return _redirect_error(f"Provider returned error: {error}", redirect_base)
    if not code or not state:
        return _redirect_error("Missing code or state", redirect_base)

    base_url = str(request.base_url).rstrip("/")
    redirect_uri = get_cloud_callback_redirect_uri(base_url)

    try:
        provider = get_state_provider(state)
        if not provider:
            return _redirect_error("Invalid or expired state. Please start the login again.", redirect_base)

        # Get the username from the request (if authenticated) or use admin
        username = _get_current_username(request)
        cred_username = _get_cred_username(username)

        data = exchange_code_for_tokens(provider, code, state, redirect_uri, username=cred_username)
        account_id = data.get("account_id") or str(uuid.uuid4())

        # Add account to config
        cc = _get_cloud_config(username)
        accounts: List[Dict[str, Any]] = list(cc.get("accounts") or [])

        # Update existing or add new
        found = False
        for acc in accounts:
            if acc.get("account_id") == account_id:
                acc["provider"] = provider
                acc["sync_enabled"] = True
                acc["display_name"] = data.get("display_name", provider)
                found = True
                break

        if not found:
            accounts.append({
                "account_id": account_id,
                "provider": provider,
                "display_name": data.get("display_name") or data.get("account_id") or provider,
                "sync_enabled": True,
                "last_synced_at": None,
            })

        cc["accounts"] = accounts
        _save_cloud_config(cc, username)

        return _redirect_success(account_id, provider, redirect_base)
    except ValueError as exc:
        logger.warning("Cloud OAuth callback error: %s", exc)
        return _redirect_error(str(exc), redirect_base)


@router.post("/accounts/webdav")
async def connect_webdav(
    request: Request,
    body: WebDavConnectRequest,
    _username: str = Depends(_get_current_username),
):
    """Connect a Nextcloud/WebDAV account with URL, username, and password."""
    url = (body.url or "").strip().rstrip("/")
    if not url:
        raise HTTPException(status_code=400, detail="WebDAV URL is required")
    webdav_user = (body.username or "").strip()
    if not webdav_user:
        raise HTTPException(status_code=400, detail="Username is required")
    password = (body.password or "").strip()
    if not password:
        raise HTTPException(status_code=400, detail="Password is required")

    account_id = f"nextcloud_{uuid.uuid4().hex[:12]}"
    cred_username = _get_cred_username(_username)

    # Store credentials securely
    set_cloud_webdav_credentials(
        account_id=account_id,
        url=url,
        webdav_username=webdav_user,
        password=password,
        username=cred_username,
    )

    # Add to config
    cc = _get_cloud_config(_username)
    accounts: List[Dict[str, Any]] = list(cc.get("accounts") or [])
    accounts.append({
        "account_id": account_id,
        "provider": "nextcloud",
        "display_name": f"Nextcloud ({webdav_user}@{url.split('//')[1].split('/')[0] if '//' in url else url})",
        "sync_enabled": True,
        "last_synced_at": None,
    })
    cc["accounts"] = accounts
    _save_cloud_config(cc, _username)

    logger.info("Nextcloud account connected: %s for user %s", account_id, _username)
    return {
        "account_id": account_id,
        "provider": "nextcloud",
        "display_name": accounts[-1]["display_name"],
    }


class AccountPatchBody(BaseModel):
    """Fields allowed for PATCH on cloud account."""
    label: Optional[str] = None


@router.patch("/accounts/{account_id}")
async def patch_account(
    request: Request,
    account_id: str,
    body: AccountPatchBody,
    _username: str = Depends(_get_current_username),
):
    """Update account metadata (e.g. label for private/work distinction)."""
    cc = _get_cloud_config(_username)
    acc = _find_account(cc, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    if body.label is not None:
        acc["label"] = body.label.strip() if body.label else None

    _save_cloud_config(cc, _username)
    return {"ok": True, "account_id": account_id, "label": acc.get("label")}


@router.delete("/accounts/{account_id}")
async def disconnect_account(
    request: Request,
    account_id: str,
    _username: str = Depends(_get_current_username),
):
    """Disconnect a cloud account: remove config entry and delete stored credentials."""
    cc = _get_cloud_config(_username)
    accounts = cc.get("accounts") or []
    target = None
    remaining = []
    for acc in accounts:
        if acc.get("account_id") == account_id:
            target = acc
        else:
            remaining.append(acc)

    if not target:
        raise HTTPException(status_code=404, detail="Account not found")

    provider = target.get("provider", "")
    cred_username = _get_cred_username(_username)

    # Delete stored credentials
    try:
        delete_cloud_credentials(account_id, provider, username=cred_username)
    except Exception as exc:
        logger.warning("Failed to delete credentials for %s: %s", account_id, exc)

    cc["accounts"] = remaining
    _save_cloud_config(cc, _username)

    logger.info("Cloud account disconnected: %s (%s) for user %s", account_id, provider, _username)
    return {"ok": True}


@router.post("/accounts/{account_id}/sync")
async def trigger_sync(
    request: Request,
    account_id: str,
    _username: str = Depends(_get_current_username),
):
    """Trigger a manual bi-directional sync for a cloud account."""
    cc = _get_cloud_config(_username)
    acc = _find_account(cc, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    provider_name = acc.get("provider", "")
    cred_username = _get_cred_username(_username)

    try:
        provider = _create_provider(provider_name, _username, account_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Check credentials exist before attempting auth (uses same username as provider)
    creds = get_cloud_credentials(account_id, provider_name, _username)
    if not creds or creds.get("type") != "oauth":
        logger.warning(
            "Cloud sync: no OAuth credentials for account_id=%s provider=%s user=%s",
            account_id, provider_name, _username,
        )
        raise HTTPException(
            status_code=401,
            detail="Credentials not found. Remove the account in Settings → Connections → Cloud, then add Google Drive again.",
        )

    # Authenticate
    try:
        authed = await asyncio.to_thread(provider.authenticate)
        if not authed:
            raise HTTPException(
                status_code=401,
                detail="Token expired or invalid. Remove the account and connect Google Drive again. In Google Cloud Console: enable Drive API, add redirect URIs for /api/email/oauth/callback and /api/cloud/oauth/callback.",
            )
    except HTTPException:
        raise
    except ValueError as exc:
        logger.warning("Cloud auth failed for %s: %s", account_id, exc)
        raise HTTPException(status_code=401, detail=str(exc))
    except Exception as exc:
        logger.error("Auth failed for cloud account %s: %s", account_id, exc)
        raise HTTPException(status_code=401, detail=f"Authentication error: {exc}")

    manifest = SyncManifest(username=_username, provider=provider_name)
    sync_dir = _local_sync_dir(_username, account_id)
    max_size = Config.get("cloud_max_file_size", DEFAULT_MAX_FILE_SIZE)
    conflict_strategy = Config.get("cloud_conflict_strategy", "last_write_wins")

    engine = SyncEngine(
        provider=provider,
        manifest=manifest,
        local_sync_dir=sync_dir,
        max_file_size=max_size,
        conflict_strategy=conflict_strategy,
    )

    try:
        result = await asyncio.to_thread(engine.full_sync)
    except Exception as exc:
        logger.error("Sync failed for cloud account %s: %s", account_id, exc)
        return {
            "ok": False,
            "error": str(exc),
            "uploaded": 0,
            "downloaded": 0,
            "deleted_local": 0,
            "deleted_remote": 0,
            "conflicts": 0,
            "errors": 1,
        }

    # Update last_synced_at
    acc["last_synced_at"] = time.time()
    _save_cloud_config(cc, _username)

    return {
        "ok": result.errors == 0,
        "uploaded": result.uploaded,
        "downloaded": result.downloaded,
        "deleted_local": result.deleted_local,
        "deleted_remote": result.deleted_remote,
        "conflicts": result.conflicts,
        "errors": result.errors,
        "skipped": result.skipped,
    }


@router.get("/accounts/{account_id}/status")
async def sync_status(
    request: Request,
    account_id: str,
    _username: str = Depends(_get_current_username),
):
    """Return sync status for a cloud account."""
    cc = _get_cloud_config(_username)
    acc = _find_account(cc, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    provider_name = acc.get("provider", "")
    manifest = SyncManifest(username=_username, provider=provider_name)

    all_files = manifest.get_all_files()
    conflicts = manifest.get_conflicts()
    pending_uploads = manifest.get_pending_uploads()
    pending_downloads = manifest.get_pending_downloads()

    return {
        "account_id": account_id,
        "provider": provider_name,
        "display_name": acc.get("display_name", ""),
        "sync_enabled": acc.get("sync_enabled", False),
        "last_synced_at": acc.get("last_synced_at"),
        "total_files": len(all_files),
        "conflicts": len(conflicts),
        "pending_uploads": len(pending_uploads),
        "pending_downloads": len(pending_downloads),
    }


@router.get("/accounts/{account_id}/browse")
async def browse_cloud(
    request: Request,
    account_id: str,
    folder_id: str = "root",
    _username: str = Depends(_get_current_username),
):
    """List cloud contents at a folder (cloud-only, no local sync). Use folder_id=root for Drive root."""
    cc = _get_cloud_config(_username)
    acc = _find_account(cc, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    provider_name = acc.get("provider", "")
    cred_username = _get_cred_username(_username)
    creds = get_cloud_credentials(account_id, provider_name, _username)
    if not creds or creds.get("type") != "oauth":
        raise HTTPException(
            status_code=401,
            detail="Credentials not found. Remove and reconnect the account.",
        )

    try:
        provider = _create_provider(provider_name, _username, account_id)
        authed = await asyncio.to_thread(provider.authenticate)
        if not authed:
            raise HTTPException(status_code=401, detail="Authentication failed")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    try:
        items = await asyncio.to_thread(
            provider.list_folder_by_id, folder_id, parent_path="/"
        )
    except NotImplementedError:
        raise HTTPException(
            status_code=400,
            detail=f"{provider_name} does not support cloud browsing yet",
        )
    except Exception as exc:
        logger.warning("Browse failed for %s folder %s: %s", account_id, folder_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "account_id": account_id,
        "folder_id": folder_id,
        "items": [
            {
                "file_id": f.file_id,
                "name": f.name,
                "path": f.path,
                "size": f.size,
                "modified_time": f.modified_time,
                "is_folder": f.is_folder,
                "mime_type": f.mime_type,
            }
            for f in items
        ],
    }


@router.get("/accounts/{account_id}/search")
async def search_cloud(
    request: Request,
    account_id: str,
    q: str = "",
    mime_type: Optional[str] = None,
    _username: str = Depends(_get_current_username),
):
    """Search entire cloud by filename. Use q= for query (e.g. report, Bewilligung, *.pdf)."""
    if not q or not q.strip():
        return {"account_id": account_id, "items": []}

    cc = _get_cloud_config(_username)
    acc = _find_account(cc, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    creds = get_cloud_credentials(account_id, acc.get("provider", ""), _get_cred_username(_username))
    if not creds or creds.get("type") != "oauth":
        raise HTTPException(status_code=401, detail="Credentials not found. Remove and reconnect the account.")

    try:
        provider = _create_provider(acc["provider"], _username, account_id)
        authed = await asyncio.to_thread(provider.authenticate)
        if not authed:
            raise HTTPException(status_code=401, detail="Authentication failed")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    try:
        items = await asyncio.to_thread(provider.search_files, q.strip(), mime_type, 50)
    except NotImplementedError:
        return {"account_id": account_id, "items": []}
    except Exception as exc:
        logger.warning("Search failed for %s q=%s: %s", account_id, q, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "account_id": account_id,
        "query": q,
        "items": [
            {
                "file_id": f.file_id,
                "name": f.name,
                "path": f.path,
                "size": f.size,
                "modified_time": f.modified_time,
                "is_folder": f.is_folder,
                "mime_type": f.mime_type,
            }
            for f in items
        ],
    }


@router.get("/accounts/{account_id}/files")
async def list_synced_files(
    request: Request,
    account_id: str,
    _username: str = Depends(_get_current_username),
):
    """List all synced files from the manifest for a cloud account."""
    cc = _get_cloud_config(_username)
    acc = _find_account(cc, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    provider_name = acc.get("provider", "")
    manifest = SyncManifest(username=_username, provider=provider_name)
    files = manifest.get_all_files()

    return {
        "account_id": account_id,
        "files": [
            {
                "file_id": f["file_id"],
                "remote_path": f["remote_path"],
                "local_path": f["local_path"],
                "size": f.get("size", 0),
                "status": f.get("status", "synced"),
                "last_synced": f.get("last_synced"),
                "remote_mtime": f.get("remote_mtime"),
                "local_mtime": f.get("local_mtime"),
            }
            for f in files
        ],
    }


@router.get("/accounts/{account_id}/conflicts")
async def list_conflicts(
    request: Request,
    account_id: str,
    _username: str = Depends(_get_current_username),
):
    """List files with unresolved conflicts for a cloud account."""
    cc = _get_cloud_config(_username)
    acc = _find_account(cc, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    provider_name = acc.get("provider", "")
    manifest = SyncManifest(username=_username, provider=provider_name)
    conflicts = manifest.get_conflicts()

    return {
        "account_id": account_id,
        "conflicts": [
            {
                "file_id": f["file_id"],
                "remote_path": f["remote_path"],
                "local_path": f["local_path"],
                "size": f.get("size", 0),
                "remote_mtime": f.get("remote_mtime"),
                "local_mtime": f.get("local_mtime"),
                "last_synced": f.get("last_synced"),
            }
            for f in conflicts
        ],
    }


@router.post("/accounts/{account_id}/conflicts/{file_id}/resolve")
async def resolve_conflict(
    request: Request,
    account_id: str,
    file_id: str,
    body: ConflictResolveRequest,
    _username: str = Depends(_get_current_username),
):
    """Resolve a file conflict. Action must be keep_local, keep_remote, or keep_both."""
    action = body.action.strip().lower()
    if action not in ("keep_local", "keep_remote", "keep_both"):
        raise HTTPException(status_code=400, detail="action must be keep_local, keep_remote, or keep_both")

    cc = _get_cloud_config(_username)
    acc = _find_account(cc, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    provider_name = acc.get("provider", "")
    manifest = SyncManifest(username=_username, provider=provider_name)

    entry = manifest.get_file(file_id)
    if not entry:
        raise HTTPException(status_code=404, detail="File not found in manifest")
    if entry.get("status") != "conflict":
        raise HTTPException(status_code=400, detail="File is not in conflict state")

    try:
        provider = _create_provider(provider_name, _username, account_id)
        authed = await asyncio.to_thread(provider.authenticate)
        if not authed:
            raise HTTPException(status_code=401, detail="Authentication failed")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Provider error: {exc}")

    local_path = Path(entry["local_path"])
    remote_path = entry["remote_path"]
    sync_dir = _local_sync_dir(_username, account_id)

    try:
        if action == "keep_local":
            # Upload local version to cloud, overwriting remote
            if local_path.exists():
                remote_meta = await asyncio.to_thread(provider.upload_file, local_path, remote_path)
                local_mtime = local_path.stat().st_mtime
                manifest.upsert_file(
                    file_id=remote_meta.file_id,
                    remote_path=remote_path,
                    local_path=str(local_path),
                    content_hash=remote_meta.content_hash,
                    etag=remote_meta.etag,
                    size=remote_meta.size,
                    remote_mtime=remote_meta.modified_time,
                    local_mtime=local_mtime,
                    status="synced",
                )
            else:
                raise HTTPException(status_code=404, detail="Local file no longer exists")

        elif action == "keep_remote":
            # Download remote version, overwriting local
            local_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(provider.download_file, file_id, local_path)
            remote_meta = await asyncio.to_thread(provider.get_file_metadata, file_id)
            local_mtime = local_path.stat().st_mtime if local_path.exists() else time.time()
            manifest.upsert_file(
                file_id=file_id,
                remote_path=remote_path,
                local_path=str(local_path),
                content_hash=remote_meta.content_hash if remote_meta else None,
                etag=remote_meta.etag if remote_meta else None,
                size=remote_meta.size if remote_meta else 0,
                remote_mtime=remote_meta.modified_time if remote_meta else None,
                local_mtime=local_mtime,
                status="synced",
            )

        elif action == "keep_both":
            # Rename local, download remote to original path
            if local_path.exists():
                stem = local_path.stem
                suffix = local_path.suffix
                conflict_path = local_path.parent / f"{stem} (conflict){suffix}"
                counter = 1
                while conflict_path.exists():
                    counter += 1
                    conflict_path = local_path.parent / f"{stem} (conflict {counter}){suffix}"
                local_path.rename(conflict_path)

            # Download remote
            local_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(provider.download_file, file_id, local_path)
            remote_meta = await asyncio.to_thread(provider.get_file_metadata, file_id)
            local_mtime = local_path.stat().st_mtime if local_path.exists() else time.time()
            manifest.upsert_file(
                file_id=file_id,
                remote_path=remote_path,
                local_path=str(local_path),
                content_hash=remote_meta.content_hash if remote_meta else None,
                etag=remote_meta.etag if remote_meta else None,
                size=remote_meta.size if remote_meta else 0,
                remote_mtime=remote_meta.modified_time if remote_meta else None,
                local_mtime=local_mtime,
                status="synced",
            )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Conflict resolution failed for %s/%s: %s", account_id, file_id, exc)
        raise HTTPException(status_code=500, detail=f"Resolution failed: {exc}")

    return {"ok": True, "action": action, "file_id": file_id}


# ── Redirect helpers ──────────────────────────────────────────────────────

def _redirect_success(account_id: str, provider: str, redirect_base: Optional[str] = None) -> RedirectResponse:
    """Redirect to frontend after successful OAuth. Use redirect_base to match the host the user used (localhost vs 127.0.0.1)."""
    port = os.environ.get("VAF_WEB_UI_PORT", "3000")
    base = (redirect_base or "").rstrip("/")
    if base and base.startswith("http"):
        url = f"{base}/settings?connections=1&cloud_oauth=success&account={account_id}&provider={provider}"
    else:
        url = f"http://localhost:{port}/settings?connections=1&cloud_oauth=success&account={account_id}&provider={provider}"
    return RedirectResponse(url=url, status_code=302)


def _redirect_error(message: str, redirect_base: Optional[str] = None) -> HTMLResponse:
    """Return an error page with a link back to settings."""
    port = os.environ.get("VAF_WEB_UI_PORT", "3000")
    base = (redirect_base or "").rstrip("/")
    if base and base.startswith("http"):
        url = f"{base}/settings?connections=1&cloud_oauth=error"
    else:
        url = f"http://localhost:{port}/settings?connections=1&cloud_oauth=error"
    msg_escaped = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    html_content = f"""
    <!DOCTYPE html>
    <html><head><meta charset="utf-8"><title>Cloud connection failed</title></head>
    <body style="font-family:sans-serif;max-width:480px;margin:2rem auto;padding:1rem;">
    <h2>Cloud connection failed</h2>
    <p>{msg_escaped}</p>
    <p><a href="{url}">Back to Settings</a></p>
    </body></html>
    """
    return HTMLResponse(content=html_content, status_code=200)
