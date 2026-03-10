"""
Per-user notification store for Web UI: thinking runs, automation results, channel replies.
Append-only list persisted to JSON; optional live push to Web UI via broadcast_to_user.
"""
import json
import logging
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.platform import Platform

logger = logging.getLogger("vaf.core.user_notifications")

MAX_ITEMS = 200 # Increased for better audit trail
MAX_AGE_HOURS = 48


def _notifications_dir() -> Path:
    """Directory for per-user notification JSON files."""
    d = Platform.data_dir() / "notifications"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_scope_key(user_scope_id: Optional[str]) -> str:
    """Safe filename segment from user_scope_id."""
    if not user_scope_id or not str(user_scope_id).strip():
        return "default"
    scope = str(user_scope_id).strip()
    # Keep local admin and "default" in a single shared bucket so notifications
    # created by idle/thinking mode (often using None/default) are visible in WebUI
    # sessions authenticated as local admin scope.
    try:
        from vaf.core.config import get_local_admin_scope_id

        local_admin_scope = str(get_local_admin_scope_id()).strip()
        if scope == "default" or scope == local_admin_scope:
            return "default"
    except Exception:
        if scope == "default":
            return "default"
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in scope)[:64]


def _file_path(user_scope_id: Optional[str]) -> Path:
    return _notifications_dir() / f"{_safe_scope_key(user_scope_id)}.json"


def _trim(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep at most MAX_ITEMS and drop older than MAX_AGE_HOURS."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)).isoformat()
    out = [i for i in items if (i.get("timestamp") or "") >= cutoff]
    if len(out) > MAX_ITEMS:
        out = out[-MAX_ITEMS:]
    return out


def append_notification(
    user_scope_id: Optional[str],
    kind: str,
    title: str,
    status: str = "success",
    summary: Optional[str] = None,
    session_id: Optional[str] = None,
    channel: Optional[str] = None,
    task_name: Optional[str] = None,
    run_id: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """
    Append a notification for the user and optionally broadcast to Web UI.
    kind: "thinking" | "automation" | "channel_reply"
    status: "success" | "skipped" | "error"
    """
    item = {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "title": title,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "sessionId": session_id,
        "channel": channel,
        "task_name": task_name,
        "run_id": run_id,
        **{k: v for k, v in extra.items() if v is not None},
    }
    path = _file_path(user_scope_id)
    items: List[Dict[str, Any]] = []
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                items = json.load(f)
        if not isinstance(items, list):
            items = []
    except Exception as e:
        logger.warning("Could not read notifications file %s: %s", path, e)
        items = []
    items.append(item)
    items = _trim(items)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=0)
    except Exception as e:
        logger.warning("Could not write notifications file %s: %s", path, e)
    # Live push to Web UI
    try:
        from vaf.core.web_interface import get_web_interface
        push_user_scope_id = user_scope_id
        if not push_user_scope_id or not str(push_user_scope_id).strip():
            # Thinking runs often use None/default; active localhost WebUI sockets
            # are subscribed with local admin scope id, so push to that id.
            try:
                from vaf.core.config import get_local_admin_scope_id

                push_user_scope_id = str(get_local_admin_scope_id())
            except Exception:
                push_user_scope_id = None
        if push_user_scope_id:
            get_web_interface().push_update_to_user(
                str(push_user_scope_id).strip(),
                {"type": "notification", "notification": item},
            )
    except Exception as e:
        logger.debug("Could not push notification to Web UI: %s", e)
    return item


def get_notifications(user_scope_id: Optional[str], limit: int = 50) -> List[Dict[str, Any]]:
    """Return the most recent notifications for the user (newest first)."""
    path = _file_path(user_scope_id)
    items: List[Dict[str, Any]] = []
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                items = json.load(f)
        if not isinstance(items, list):
            items = []
    except Exception as e:
        logger.warning("Could not read notifications file %s: %s", path, e)
        return []
    items = _trim(items)
    # Newest first
    items = sorted(items, key=lambda i: i.get("timestamp") or "", reverse=True)
    return items[:limit]
