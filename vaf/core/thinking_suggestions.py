"""
Thinking mode – per-user storage for suggestions produced during thinking runs.
Stored under thinking_suggestions / user_scope_id so data is isolated per user.
"""
import json
import uuid
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Any

from vaf.core.platform import Platform


def _suggestions_dir(user_scope_id: Optional[str]) -> Path:
    base = Platform.vaf_dir() / "thinking_suggestions"
    if user_scope_id:
        return base / str(user_scope_id).strip()
    return base / "_default"


def _suggestions_path(user_scope_id: Optional[str]) -> Path:
    return _suggestions_dir(user_scope_id) / "suggestions.json"


def _load_suggestions(path: Path) -> List[dict]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_suggestions(path: Path, data: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def add_suggestion(
    user_scope_id: Optional[str],
    category: str,
    suggestion: str,
    priority: int = 0,
    action_taken: bool = False,
    status: str = "pending",
    thinking_run_id: Optional[str] = None,
    action_type: Optional[str] = None,
) -> dict:
    """
    Add a thinking suggestion. Returns the created entry with id and timestamp.
    category: automation | user_knowledge | proactive_help | system_health | todo_note
    status: pending | approved | rejected
    action_type: e.g. create_automation | ask_user
    """
    path = _suggestions_path(user_scope_id)
    items = _load_suggestions(path)
    entry = {
        "id": str(uuid.uuid4())[:8],
        "timestamp": datetime.now().isoformat(),
        "category": (category or "proactive_help").strip().lower(),
        "suggestion": (suggestion or "").strip(),
        "priority": int(priority) if priority is not None else 0,
        "action_taken": bool(action_taken),
        "status": (status or "pending").strip().lower(),
        "thinking_run_id": (thinking_run_id or "").strip() or None,
        "action_type": (action_type or "").strip() or None,
    }
    if entry["status"] not in ("pending", "approved", "rejected"):
        entry["status"] = "pending"
    items.append(entry)
    _save_suggestions(path, items)
    return entry


def list_suggestions(
    user_scope_id: Optional[str] = None,
    status: Optional[str] = None,
) -> List[dict]:
    """Return suggestions for the user, optionally filtered by status."""
    path = _suggestions_path(user_scope_id)
    items = _load_suggestions(path)
    items = [i for i in items if isinstance(i, dict) and i.get("id")]
    if status:
        status_lower = str(status).strip().lower()
        items = [i for i in items if (i.get("status") or "pending") == status_lower]
    return sorted(items, key=lambda x: (x.get("timestamp") or ""), reverse=True)


def mark_action_taken(user_scope_id: Optional[str], suggestion_id: str) -> bool:
    """Set action_taken=True for a suggestion by id. Returns True if found."""
    path = _suggestions_path(user_scope_id)
    items = _load_suggestions(path)
    for i in items:
        if isinstance(i, dict) and i.get("id") == suggestion_id:
            i["action_taken"] = True
            _save_suggestions(path, items)
            return True
    return False


def update_status(user_scope_id: Optional[str], suggestion_id: str, status: str) -> bool:
    """Update status (pending | approved | rejected) for a suggestion by id. Returns True if found."""
    path = _suggestions_path(user_scope_id)
    items = _load_suggestions(path)
    status_lower = (status or "pending").strip().lower()
    if status_lower not in ("pending", "approved", "rejected"):
        return False
    for i in items:
        if isinstance(i, dict) and i.get("id") == suggestion_id:
            i["status"] = status_lower
            _save_suggestions(path, items)
            return True
    return False
