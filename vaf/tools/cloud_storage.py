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
    "Cloud storage for Google Drive, OneDrive. PREFER 'search_all' to search ALL connected clouds at once (like the UI). "
    "search_all: Find files across every cloud in one call. search: Single provider. browse: List folder. read/download: Use file_id (+ provider or account_id from results). "
    "show_in_viewer: Open a cloud file (e.g. PDF) in the Document Viewer (Anhänge). save/list/retrieve: VAF Sync. status: connection. "
    "Accounts may have labels (e.g. 'Privat', 'Arbeit'); search_all returns them."
)

TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["save", "list", "retrieve", "status", "browse", "download", "read", "search", "search_all", "show_in_viewer"],
            "description": "Action. search_all: Search all clouds at once. search: Single provider. show_in_viewer: Open PDF/doc in Document Viewer (Anhänge). read/download: Use file_id.",
        },
        "provider": {
            "type": "string",
            "description": "Cloud provider: google_drive, onedrive, dropbox, nextcloud, icloud. For read/download: use when multiple accounts; omit for first. Ignored for search_all.",
        },
        "account_id": {
            "type": "string",
            "description": "For read/download: use when multiple accounts for same provider (from search_all results).",
        },
        "query": {
            "type": "string",
            "description": "For 'search': filename or pattern (e.g. 'report', 'Bewilligung'). Searches entire cloud at once.",
        },
        "mime_type": {
            "type": "string",
            "description": "For 'search': optional filter, e.g. 'application/pdf' for PDFs only.",
        },
        "file_id": {
            "type": "string",
            "description": "For 'download', 'read', 'show_in_viewer': cloud file ID (from search or search_all).",
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


def _get_cloud_accounts(username: str) -> list:
    """Return list of connected cloud accounts (dict with provider, account_id, label, etc.)."""
    admin_user = Config.get("local_admin_username", "admin")
    if username == admin_user:
        cloud_config = Config.get("cloud_config") or {}
        accounts = cloud_config.get("accounts", [])
    else:
        by_user = Config.get("cloud_config_by_user") or {}
        user_cfg = by_user.get(username, {})
        accounts = user_cfg.get("accounts", [])
    return [a for a in accounts if a.get("sync_enabled", True) and a.get("provider") and a.get("account_id")]


def _get_first_connected_account(username: str) -> Optional[tuple[str, str]]:
    """Return (provider, account_id) for the first connected account, or None."""
    accounts = _get_cloud_accounts(username)
    for acct in accounts:
        return (acct["provider"], acct["account_id"])
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
                      query: Optional[str] = None, mime_type: Optional[str] = None,
                      **kwargs: Any) -> str:
    """Execute the cloud_storage tool action."""
    username = _get_username()
    first = _get_first_connected_account(username)
    if not first and action not in ("search_all", "status"):
        return "No cloud storage connected. Go to Settings → Connections to connect a cloud provider."
    default_provider, default_account_id = first or ("", "")

    account_id_param = kwargs.get("account_id")
    if account_id_param:
        acct = _get_account_by_id(username, account_id_param)
        if not acct:
            return f"Account {account_id_param} not found. Use 'status' or 'search_all' to see accounts."
        account_id = acct["account_id"]
        effective_provider = acct["provider"]
    elif provider:
        acct = _get_account_by_provider(username, provider)
        if not acct:
            return f"No {provider} account connected. Use 'status' to see connected providers."
        account_id = acct["account_id"]
        effective_provider = provider
    else:
        account_id = default_account_id
        effective_provider = default_provider

    file_id = file_id or kwargs.get("file_id")
    if action == "search_all":
        return _action_search_all(username, query or kwargs.get("query"), kwargs.get("mime_type") or mime_type)
    if action == "show_in_viewer":
        return _action_show_in_viewer(username, account_id, effective_provider, file_id)
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
    elif action == "search":
        return _action_search(username, account_id, effective_provider, query or kwargs.get("query"), mime_type or kwargs.get("mime_type"))
    else:
        return f"Unknown action: {action}. Use: search_all, search, browse, download, read, show_in_viewer, save, list, retrieve, status."


def _get_account_by_provider(username: str, provider: str) -> Optional[dict]:
    """Return first account matching provider."""
    for acct in _get_cloud_accounts(username):
        if acct.get("provider") == provider:
            return acct
    return None


def _get_account_by_id(username: str, account_id: str) -> Optional[dict]:
    """Return account by account_id."""
    for acct in _get_cloud_accounts(username):
        if acct.get("account_id") == account_id:
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


def _action_search_all(username: str, query: Optional[str], mime_type: Optional[str] = None) -> str:
    """Search all connected clouds at once (like the UI). Returns combined results with provider/label."""
    if not query or not str(query).strip():
        return "query is required for 'search_all' (e.g. 'report.pdf', 'Bewilligung')."

    accounts = _get_cloud_accounts(username)
    if not accounts:
        return "No cloud storage connected. Go to Settings → Connections to connect a cloud provider."

    def _fmt_size(b: int) -> str:
        if b < 1024:
            return f"{b} B"
        if b < 1024 * 1024:
            return f"{b / 1024:.1f} KB"
        return f"{b / (1024 * 1024):.1f} MB"

    all_items: list = []  # (account_id, provider, label, file_item)
    for acct in accounts:
        prov_name = acct.get("provider", "")
        acc_id = acct.get("account_id", "")
        label = acct.get("label") or None
        try:
            prov = _create_provider(prov_name, username, acc_id)
            if not prov.authenticate():
                continue
            items = prov.search_files(query.strip(), mime_type=mime_type, limit=30)
            for f in items:
                all_items.append((acc_id, prov_name, label, f))
        except (NotImplementedError, Exception) as e:
            logger.debug("search_all skipped %s: %s", prov_name, e)
            continue

    if not all_items:
        return f"No files matching '{query}' found across any cloud."

    lines = []
    for acc_id, prov_name, label, f in all_items[:50]:
        kind = "[F]" if f.is_folder else "[ ]"
        size = _fmt_size(f.size) if not f.is_folder else "Folder"
        label_str = f" label={label}" if label else ""
        lines.append(f"  {kind} {f.name}  id={f.file_id}  provider={prov_name} account_id={acc_id}{label_str}  ({size})")
    if len(all_items) > 50:
        lines.append(f"  ... and {len(all_items) - 50} more. Refine query to narrow results.")
    return (
        f"Found {len(all_items)} item(s) across all clouds matching '{query}':\n"
        + "\n".join(lines)
        + "\n\nUse read(file_id=..., provider=... or account_id=...) or show_in_viewer(file_id=..., provider=... or account_id=...) with id and provider/account_id from above."
    )


def _action_search(username: str, account_id: str, provider: str, query: Optional[str], mime_type: Optional[str] = None) -> str:
    """Search entire cloud by filename. Returns matching files with file_id for read/download."""
    if not query or not str(query).strip():
        return "query is required for 'search' action (e.g. 'report.pdf', 'Bewilligung', '*.pdf')."

    try:
        prov = _create_provider(provider, username, account_id)
        if not prov.authenticate():
            return f"Authentication failed for {provider}. Reconnect the account in Settings."
        items = prov.search_files(query.strip(), mime_type=mime_type, limit=50)
    except NotImplementedError:
        return f"{provider} does not support cloud search. Use browse with folder_id to navigate."
    except Exception as e:
        logger.error("[CloudStorage] Search failed: %s", e)
        return f"Search failed: {e}"

    if not items:
        return f"No files matching '{query}' found in cloud."

    def _fmt_size(b: int) -> str:
        if b < 1024:
            return f"{b} B"
        if b < 1024 * 1024:
            return f"{b / 1024:.1f} KB"
        return f"{b / (1024 * 1024):.1f} MB"

    lines = []
    for f in items[:30]:
        kind = "[F]" if f.is_folder else "[ ]"
        size = _fmt_size(f.size) if not f.is_folder else "Folder"
        lines.append(f"  {kind} {f.name}  id={f.file_id}  ({size})")
    if len(items) > 30:
        lines.append(f"  ... and {len(items) - 30} more. Refine query to narrow results.")
    return f"Found {len(items)} item(s) matching '{query}':\n" + "\n".join(lines) + "\n\nUse read(file_id=...) or download(file_id=...) with the id above."


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


def _action_show_in_viewer(username: str, account_id: str, provider: str, file_id: Optional[str]) -> str:
    """Download a cloud file and open it in the Document Viewer (Anhänge) so the user can see the PDF/doc."""
    if not file_id:
        return "file_id is required for 'show_in_viewer'. Get it from search or search_all."

    import tempfile

    try:
        prov = _create_provider(provider, username, account_id)
        if not prov.authenticate():
            return f"Authentication failed for {provider}. Reconnect the account in Settings."
        meta = prov.get_file_metadata(file_id)
        if not meta:
            return f"File not found: {file_id}"
        if meta.is_folder:
            return "Cannot open a folder in the viewer. Use a file (PDF, DOCX, etc.)."

        suffix = Path(meta.name).suffix or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = Path(tmp.name)
        try:
            prov.download_file(file_id, tmp_path)
            raw = tmp_path.read_bytes()
            content = ""
            try:
                from vaf.tools.librarian import LibrarianTool
                librarian = LibrarianTool()
                content = librarian._read_file(tmp_path, enable_chunking=True) or ""
            except Exception:
                pass
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

        if not content and not raw:
            return f"Could not read file {meta.name}."

        import base64
        import mimetypes
        mime_type = meta.mime_type or mimetypes.guess_type(meta.name)[0] or "application/octet-stream"
        new_doc = {
            "name": meta.name,
            "content": content,
            "data": base64.b64encode(raw).decode("ascii"),
            "mimeType": mime_type,
            "path": f"Cloud ({provider}): {meta.name}",
        }

        try:
            from vaf.core.subagent_ipc import get_current_session_id
            from vaf.core.session import SessionManager
            from vaf.core.web_interface import get_web_interface
        except ImportError as e:
            return f"Error: Could not load dependencies: {e}"

        session_id = get_current_session_id()
        if not session_id:
            return "Error: Document Viewer is only available in the Web UI with an active chat session."

        try:
            session_mgr = SessionManager()
            session = session_mgr.load(session_id)
            if not getattr(session, "runtime_state", None):
                session.runtime_state = {}
            sidebar = session.runtime_state.get("sidebar_documents") or []
            if any(d.get("name") == meta.name for d in sidebar):
                return f'Document "{meta.name}" is already open in the Document Viewer (Anhänge).'
            sidebar.append(new_doc)
            session.runtime_state["sidebar_documents"] = sidebar
            session_mgr.save(session, sync_state=False)
        except FileNotFoundError:
            from vaf.core.session import Session
            new_sess = Session(id=session_id, name=f"Session {session_id}", runtime_state={"sidebar_documents": [new_doc]})
            SessionManager().save(new_sess, sync_state=False)
            sidebar = [new_doc]
        except Exception as e:
            return f"Error: Could not save to session: {e}"

        try:
            get_web_interface()._push_session_update(session_id, {"type": "sidebar_documents_set", "contents": sidebar, "sessionId": session_id})
        except Exception:
            pass

        return f"Document '{meta.name}' has been opened in the Document Viewer (Anhänge). The user can see it in the right panel."
    except NotImplementedError:
        return f"{provider} does not support show_in_viewer."
    except Exception as e:
        logger.error("[CloudStorage] show_in_viewer failed: %s", e)
        return f"Failed to open in viewer: {e}"


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
            disp = acct.get("display_name", acct.get("account_id", "?"))
            label = acct.get("label")
            if label:
                return f"{provider}: {enabled}, last sync: {ago_str}, account: {disp}, label: {label}"
            return f"{provider}: {enabled}, last sync: {ago_str}, account: {disp}"

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
            query=kwargs.get("query"),
            mime_type=kwargs.get("mime_type"),
            **{k: v for k, v in kwargs.items() if k not in ("action", "provider", "folder_id", "file_path", "remote_path", "file_id", "query", "mime_type")},
        )
