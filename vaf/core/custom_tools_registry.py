"""
Custom Tools Registry
=====================
Manages user-uploaded Python tools stored outside the built-in vaf/tools/ package.

Storage layout (inside Platform.data_dir()):
    custom_tools/
        my_tool.py          # one file = one tool class (BaseTool subclass)
        another_tool.py
        manifest.json       # access-control manifest (see ManifestEntry below)

Manifest structure:
    {
        "version": 1,
        "tools": {
            "my_tool": {
                "filename":    "my_tool.py",
                "created_by":  "admin",
                "created_at":  "2026-05-20T10:00:00Z",
                "updated_at":  "2026-05-20T10:00:00Z",
                "shared_with": ["*"]   // "*" = all users, or list of user_scope_id strings
            }
        }
    }

Access rules:
    - Only admin users may create / delete / update permissions on custom tools.
    - "shared_with": ["*"]  → visible to every logged-in user.
    - "shared_with": []     → visible only to admin.
    - "shared_with": ["<scope_id>", ...]  → visible to those specific users + admin.

Hot-reload:
    load_custom_tool_class() always does a fresh importlib load — it never caches the
    module object — so calling agent.reload_custom_tools() is sufficient to pick up
    changes without restarting the server.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Protects manifest read/write across threads (the web server is async but
# importlib.util is sync, and multiple WS messages can arrive concurrently).
_manifest_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# Directory helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_custom_tools_dir() -> Path:
    """Return (and create if missing) the custom_tools directory inside data_dir."""
    from vaf.core.platform import Platform
    directory = Platform.data_dir() / "custom_tools"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _manifest_path() -> Path:
    return get_custom_tools_dir() / "manifest.json"


# ─────────────────────────────────────────────────────────────────────────────
# Manifest I/O  (always go through these two functions)
# ─────────────────────────────────────────────────────────────────────────────

def load_manifest() -> Dict[str, Any]:
    """
    Read manifest.json and return its parsed contents.
    Returns a safe empty structure if the file does not exist yet.
    Thread-safe.
    """
    with _manifest_lock:
        path = _manifest_path()
        if not path.exists():
            return {"version": 1, "tools": {}}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            # Ensure required keys exist even on an older manifest format
            data.setdefault("version", 1)
            data.setdefault("tools", {})
            return data
        except Exception as exc:
            logger.error("custom_tools_registry: failed to read manifest.json: %s", exc)
            return {"version": 1, "tools": {}}


def _save_manifest(data: Dict[str, Any]) -> None:
    """
    Atomically write *data* to manifest.json (temp file + rename).
    Must be called while _manifest_lock is held.
    """
    path = _manifest_path()
    # Write to a sibling temp file first, then rename — this prevents a
    # partially-written manifest from being read by another thread.
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)  # atomic on POSIX; best-effort on Windows
    except Exception:
        # Clean up orphaned temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Tool file loading
# ─────────────────────────────────────────────────────────────────────────────

def load_custom_tool_class(tool_name: str):
    """
    Import the .py file for *tool_name* and return the first BaseTool subclass found.

    Uses importlib.util.spec_from_file_location with a private module namespace
    ("_custom_tools.<name>") so custom tools never shadow built-in vaf.tools.* names.

    Returns None (with a logged warning) if the file is missing, cannot be imported,
    or contains no BaseTool subclass.

    This function intentionally does NOT cache the module object so that a fresh call
    always reflects the current file contents (hot-reload support).
    """
    from vaf.tools.base import BaseTool

    manifest = load_manifest()
    entry = manifest.get("tools", {}).get(tool_name)
    if not entry:
        logger.warning("custom_tools_registry: no manifest entry for '%s'", tool_name)
        return None

    file_path = get_custom_tools_dir() / entry["filename"]
    if not file_path.exists():
        logger.warning("custom_tools_registry: file not found for '%s': %s", tool_name, file_path)
        return None

    # Use a private module name to avoid polluting sys.modules with names that
    # could clash with built-in tools (e.g. if someone names their tool "web_search").
    module_name = f"_custom_tools.{tool_name}"

    try:
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            logger.error("custom_tools_registry: cannot create module spec for '%s'", file_path)
            return None

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.error("custom_tools_registry: import error in '%s': %s", file_path, exc)
        return None

    # Find the first concrete BaseTool subclass defined in this module
    # (skip BaseTool itself and any intermediate abstract classes).
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if (
            issubclass(obj, BaseTool)
            and obj is not BaseTool
            and obj.__module__ == module_name
            and not inspect.isabstract(obj)
        ):
            return obj

    logger.warning(
        "custom_tools_registry: no BaseTool subclass found in '%s'", file_path
    )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Access-control helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_visible_tool_names_for_user(user_scope_id: Optional[str]) -> List[str]:
    """
    Return the list of custom tool names visible to the given user.

    - user_scope_id=None is treated as admin → all tools are visible.
    - A tool with shared_with=["*"] is visible to everyone.
    - A tool with shared_with=[] is visible only to admin (None scope).
    - A tool with specific scope IDs is visible to those users + admin.
    """
    manifest = load_manifest()
    visible = []
    for name, entry in manifest.get("tools", {}).items():
        shared_with: List[str] = entry.get("shared_with", ["*"])
        if user_scope_id is None:
            # Admin sees everything
            visible.append(name)
        elif "*" in shared_with:
            visible.append(name)
        elif user_scope_id in shared_with:
            visible.append(name)
    return visible


def is_tool_visible_to_user(tool_name: str, user_scope_id: Optional[str]) -> bool:
    """Convenience single-tool check."""
    return tool_name in get_visible_tool_names_for_user(user_scope_id)


# ─────────────────────────────────────────────────────────────────────────────
# CRUD operations  (all admin-only — callers must enforce this)
# ─────────────────────────────────────────────────────────────────────────────

def register_tool(
    tool_name: str,
    filename: str,
    created_by: str,
    shared_with: Optional[List[str]] = None,
) -> None:
    """
    Add or overwrite a tool entry in the manifest.

    *shared_with* defaults to ["*"] (all users) when not provided.
    Call this after writing the .py file to disk.
    """
    if shared_with is None:
        shared_with = ["*"]

    now = datetime.now(timezone.utc).isoformat()

    with _manifest_lock:
        data = load_manifest()
        existing = data["tools"].get(tool_name, {})
        data["tools"][tool_name] = {
            "filename": filename,
            "created_by": created_by,
            # Preserve original creation timestamp on overwrite
            "created_at": existing.get("created_at", now),
            "updated_at": now,
            "shared_with": shared_with,
        }
        _save_manifest(data)

    logger.info(
        "custom_tools_registry: registered tool '%s' (shared_with=%s)",
        tool_name, shared_with,
    )


def delete_tool(tool_name: str) -> None:
    """
    Remove *tool_name* from the manifest and delete its .py file.
    Raises FileNotFoundError if the tool is not in the manifest.
    """
    with _manifest_lock:
        data = load_manifest()
        entry = data["tools"].pop(tool_name, None)
        if entry is None:
            raise FileNotFoundError(f"Custom tool '{tool_name}' not found in manifest")

        # Delete the source file (best-effort — don't fail if already gone)
        file_path = get_custom_tools_dir() / entry["filename"]
        try:
            file_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning(
                "custom_tools_registry: could not delete file '%s': %s", file_path, exc
            )

        _save_manifest(data)

    logger.info("custom_tools_registry: deleted tool '%s'", tool_name)


def update_tool_permissions(tool_name: str, shared_with: List[str]) -> None:
    """
    Update the *shared_with* list for an existing tool.
    Raises KeyError if the tool is not in the manifest.
    """
    with _manifest_lock:
        data = load_manifest()
        if tool_name not in data["tools"]:
            raise KeyError(f"Custom tool '{tool_name}' not found in manifest")
        data["tools"][tool_name]["shared_with"] = shared_with
        data["tools"][tool_name]["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_manifest(data)

    logger.info(
        "custom_tools_registry: updated permissions for '%s' → shared_with=%s",
        tool_name, shared_with,
    )


def update_tool_source(tool_name: str, new_code: str, updated_by: str) -> None:
    """
    Overwrite the .py file for an existing custom tool.
    Validates that the new code contains a BaseTool subclass before writing
    so a bad edit cannot break the registry.
    Raises KeyError if the tool is not in the manifest.
    Raises ValueError if the new code has no valid BaseTool subclass.
    """
    manifest = load_manifest()
    entry = manifest.get("tools", {}).get(tool_name)
    if entry is None:
        raise KeyError(f"Custom tool '{tool_name}' not found in manifest")

    # Write to a temp file first and try to load it for validation
    file_path = get_custom_tools_dir() / entry["filename"]
    fd, tmp_path = tempfile.mkstemp(dir=get_custom_tools_dir(), suffix=".tmp.py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_code)

        # Temporarily rename to the real filename so the loader finds it
        # (spec_from_file_location only cares about the path, not the name)
        # We validate using a throwaway import of the temp path directly.
        from vaf.tools.base import BaseTool as _BaseTool
        spec = importlib.util.spec_from_file_location(f"_custom_tools_validate.{tool_name}", tmp_path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[attr-defined]
            found = any(
                issubclass(obj, _BaseTool) and obj is not _BaseTool and not inspect.isabstract(obj)
                for _, obj in inspect.getmembers(mod, inspect.isclass)
                if obj.__module__ == mod.__name__
            )
            if not found:
                raise ValueError(
                    "The uploaded code does not contain a valid BaseTool subclass."
                )

        # Validation passed — atomically replace the real file
        os.replace(tmp_path, file_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # Update updated_at in the manifest
    with _manifest_lock:
        data = load_manifest()
        if tool_name in data["tools"]:
            data["tools"][tool_name]["updated_at"] = datetime.now(timezone.utc).isoformat()
            data["tools"][tool_name]["updated_by"] = updated_by
            _save_manifest(data)

    logger.info("custom_tools_registry: updated source for '%s'", tool_name)


# ─────────────────────────────────────────────────────────────────────────────
# Convenience read helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_all_custom_tool_names() -> List[str]:
    """Return all registered custom tool names (admin view — no filtering)."""
    return list(load_manifest().get("tools", {}).keys())


def get_tool_manifest_entry(tool_name: str) -> Optional[Dict[str, Any]]:
    """Return the raw manifest entry for *tool_name*, or None if not found."""
    return load_manifest().get("tools", {}).get(tool_name)


def get_tool_source(tool_name: str) -> Optional[str]:
    """
    Read and return the source code of a custom tool.
    Returns None if the file does not exist.
    """
    entry = get_tool_manifest_entry(tool_name)
    if not entry:
        return None
    file_path = get_custom_tools_dir() / entry["filename"]
    if not file_path.exists():
        return None
    try:
        return file_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.error("custom_tools_registry: cannot read source for '%s': %s", tool_name, exc)
        return None


def save_tool_file(filename: str, code: str) -> Path:
    """
    Write *code* to custom_tools/<filename>.
    Returns the absolute path.  Caller is responsible for calling register_tool()
    afterwards to add the manifest entry.
    """
    file_path = get_custom_tools_dir() / filename
    file_path.write_text(code, encoding="utf-8")
    return file_path
