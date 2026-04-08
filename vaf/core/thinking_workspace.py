"""
Per-user Thinking Workspace (Virtual Desktop) for Thinking Mode.

Storage root:
    Platform.data_dir() / "workspaces" / <scope_key> /

MVP safety defaults:
- write allowed inside own scope workspace only
- handoff required for externally visible/apply actions
- no destructive external actions in this module
"""
from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.platform import Platform


def _scope_key(user_scope_id: Optional[str]) -> str:
    """Canonical workspace key. Mirrors thinking_mode._key semantics."""
    if user_scope_id is None:
        return "default"
    try:
        from vaf.core.config import get_local_admin_scope_id

        if str(user_scope_id).strip() == str(get_local_admin_scope_id()).strip():
            return "default"
    except Exception:
        pass
    return str(user_scope_id).strip()


def workspace_root(user_scope_id: Optional[str]) -> Path:
    return Platform.data_dir() / "workspaces" / _scope_key(user_scope_id)


def ensure_workspace(user_scope_id: Optional[str]) -> Path:
    root = workspace_root(user_scope_id)
    for rel in ("inbox", "tasks", "archive", "trash"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


def _safe_join(base: Path, relative_path: str) -> Path:
    target = (base / (relative_path or "")).resolve()
    if not str(target).startswith(str(base.resolve())):
        raise ValueError("Path escapes workspace boundary")
    return target


def _task_root(user_scope_id: Optional[str], task_id: str) -> Path:
    return ensure_workspace(user_scope_id) / "tasks" / task_id


def _task_meta_path(user_scope_id: Optional[str], task_id: str) -> Path:
    return _task_root(user_scope_id, task_id) / "meta" / "task.json"


def _task_events_path(user_scope_id: Optional[str], task_id: str) -> Path:
    return _task_root(user_scope_id, task_id) / "events.log"


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def append_event(user_scope_id: Optional[str], task_id: str, event: str, details: Optional[Dict[str, Any]] = None) -> None:
    path = _task_events_path(user_scope_id, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat()
    row = {"ts": ts, "event": event, "details": details or {}}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def create_task(
    user_scope_id: Optional[str],
    title: str,
    source: str,
    description: str = "",
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    task_id = str(uuid.uuid4())[:8]
    task_root = _task_root(user_scope_id, task_id)
    (task_root / "workspace").mkdir(parents=True, exist_ok=True)
    (task_root / "handoff").mkdir(parents=True, exist_ok=True)
    (task_root / "meta").mkdir(parents=True, exist_ok=True)
    meta = {
        "id": task_id,
        "title": (title or "").strip() or f"Task {task_id}",
        "source": (source or "").strip() or "unknown",
        "description": (description or "").strip(),
        "status": "open",  # open | pending_approval | approved | rejected | archived
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "policy": {
            "allow_write": True,
            "requires_approval": True,
            "allow_external_send": False,
            **(policy or {}),
        },
    }
    _save_json(_task_meta_path(user_scope_id, task_id), meta)
    append_event(user_scope_id, task_id, "task_created", {"source": meta["source"]})
    return meta


def get_or_create_open_task_by_source(
    user_scope_id: Optional[str],
    source: str,
    title: str,
    description: str = "",
) -> Dict[str, Any]:
    """Return first open task for source, or create one."""
    for t in list_tasks(user_scope_id, status="open"):
        if str(t.get("source") or "").strip() == (source or "").strip():
            return t
    return create_task(
        user_scope_id=user_scope_id,
        title=title,
        source=source,
        description=description,
    )


def list_tasks(user_scope_id: Optional[str], status: Optional[str] = None) -> List[Dict[str, Any]]:
    tasks_dir = ensure_workspace(user_scope_id) / "tasks"
    out: List[Dict[str, Any]] = []
    for task_dir in tasks_dir.iterdir() if tasks_dir.exists() else []:
        if not task_dir.is_dir():
            continue
        meta = _load_json(task_dir / "meta" / "task.json", {})
        if not isinstance(meta, dict) or not meta.get("id"):
            continue
        if status and meta.get("status") != status:
            continue
        out.append(meta)
    out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return out


def get_task(user_scope_id: Optional[str], task_id: str) -> Optional[Dict[str, Any]]:
    meta = _load_json(_task_meta_path(user_scope_id, task_id), {})
    if not isinstance(meta, dict) or not meta.get("id"):
        return None
    return meta


def update_task_status(user_scope_id: Optional[str], task_id: str, status: str) -> Optional[Dict[str, Any]]:
    meta = get_task(user_scope_id, task_id)
    if not meta:
        return None
    meta["status"] = status
    meta["updated_at"] = datetime.now().isoformat()
    _save_json(_task_meta_path(user_scope_id, task_id), meta)
    append_event(user_scope_id, task_id, "task_status_updated", {"status": status})
    return meta


def write_workspace_file(
    user_scope_id: Optional[str], task_id: str, relative_path: str, content: str, append: bool = False
) -> Path:
    task = get_task(user_scope_id, task_id)
    if not task:
        raise FileNotFoundError("Task not found")
    base = _task_root(user_scope_id, task_id) / "workspace"
    target = _safe_join(base, relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with open(target, mode, encoding="utf-8") as f:
        f.write(content or "")
    append_event(user_scope_id, task_id, "workspace_file_written", {"path": relative_path, "append": append})
    return target


def read_workspace_file(user_scope_id: Optional[str], task_id: str, relative_path: str) -> str:
    base = _task_root(user_scope_id, task_id) / "workspace"
    target = _safe_join(base, relative_path)
    if not target.exists():
        raise FileNotFoundError("File not found")
    return target.read_text(encoding="utf-8")


def create_handoff(
    user_scope_id: Optional[str],
    task_id: str,
    title: str,
    content: str,
    proposed_action: str = "",
    automation_action: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    task = get_task(user_scope_id, task_id)
    if not task:
        raise FileNotFoundError("Task not found")
    handoff_id = str(uuid.uuid4())[:8]
    handoff = {
        "id": handoff_id,
        "task_id": task_id,
        "title": (title or "").strip() or f"Handoff {handoff_id}",
        "proposed_action": (proposed_action or "").strip(),
        "status": "pending",
        "created_at": datetime.now().isoformat(),
    }
    if isinstance(automation_action, dict) and automation_action:
        handoff["automation_action"] = automation_action
    handoff_dir = _task_root(user_scope_id, task_id) / "handoff"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    _save_json(handoff_dir / f"{handoff_id}.json", handoff)
    (handoff_dir / f"{handoff_id}.md").write_text(content or "", encoding="utf-8")
    update_task_status(user_scope_id, task_id, "pending_approval")
    append_event(user_scope_id, task_id, "handoff_created", {"handoff_id": handoff_id})
    return handoff


def list_pending_handoffs(user_scope_id: Optional[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for task in list_tasks(user_scope_id):
        task_id = task.get("id")
        if not task_id:
            continue
        handoff_dir = _task_root(user_scope_id, task_id) / "handoff"
        for p in handoff_dir.glob("*.json"):
            h = _load_json(p, {})
            if isinstance(h, dict) and h.get("status") == "pending":
                out.append(h)
    out.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return out


def get_handoff(user_scope_id: Optional[str], task_id: str, handoff_id: str) -> Optional[Dict[str, Any]]:
    """Get a single handoff JSON by ids."""
    hpath = _task_root(user_scope_id, task_id) / "handoff" / f"{handoff_id}.json"
    handoff = _load_json(hpath, {})
    if not isinstance(handoff, dict) or not handoff.get("id"):
        return None
    return handoff


def _apply_automation_action(user_scope_id: Optional[str], action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply approved automation action from a handoff.
    Supported operations:
    - create: requires prompt; optional name/description/frequency/time/weekday/day/enabled
    - update: requires task_id; supports name/description/prompt/frequency/time/weekday/day/enabled
    """
    from vaf.core.automation import AutomationManager, AutomationTask

    op = str(action.get("operation") or "").strip().lower()
    mgr = AutomationManager(user_scope_id=user_scope_id) if user_scope_id else AutomationManager()
    if op == "create":
        prompt = (action.get("prompt") or "").strip()
        if not prompt:
            return {"ok": False, "error": "create requires prompt"}
        frequency = str(action.get("frequency") or "daily").strip().lower() or "daily"
        time_str = str(action.get("time") or "06:00").strip()
        name = (action.get("name") or "").strip() or (prompt[:50] + ("..." if len(prompt) > 50 else ""))
        description = (action.get("description") or "").strip() or prompt[:200]
        weekday = (action.get("weekday") or "").strip().lower() or None
        day = action.get("day")
        if day is not None:
            try:
                day = max(1, min(31, int(day)))
            except (TypeError, ValueError):
                day = None
        task = AutomationTask(
            name=name,
            description=description,
            prompt=prompt,
            frequency=frequency,
            time=time_str,
            weekday=weekday if frequency == "weekly" else None,
            day=day if frequency == "monthly" else None,
            enabled=bool(action.get("enabled", True)),
            user_scope_id=user_scope_id,
        )
        task = mgr.create(task)
        return {"ok": True, "operation": "create", "task_id": task.id}

    if op == "update":
        target_id = (action.get("task_id") or "").strip()
        if not target_id:
            return {"ok": False, "error": "update requires task_id"}
        update_params = {}
        for key in ("name", "description", "prompt", "frequency", "time", "weekday", "day", "enabled"):
            if key in action and action[key] is not None:
                update_params[key] = action[key]
        if not update_params:
            return {"ok": False, "error": "no fields to update"}
        task = mgr.update(target_id, **update_params)
        if not task:
            return {"ok": False, "error": "automation not found"}
        return {"ok": True, "operation": "update", "task_id": task.id}

    return {"ok": False, "error": "unsupported operation"}


def approve_handoff(user_scope_id: Optional[str], task_id: str, handoff_id: str) -> bool:
    hpath = _task_root(user_scope_id, task_id) / "handoff" / f"{handoff_id}.json"
    mdpath = _task_root(user_scope_id, task_id) / "handoff" / f"{handoff_id}.md"
    handoff = _load_json(hpath, {})
    if not isinstance(handoff, dict) or not handoff.get("id"):
        return False
    handoff["status"] = "approved"
    handoff["approved_at"] = datetime.now().isoformat()
    action_result = None
    if isinstance(handoff.get("automation_action"), dict):
        action_result = _apply_automation_action(user_scope_id, handoff.get("automation_action") or {})
        handoff["automation_action_result"] = action_result
    _save_json(hpath, handoff)

    archive_dir = ensure_workspace(user_scope_id) / "archive" / "approved" / task_id
    archive_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(hpath, archive_dir / hpath.name)
    if mdpath.exists():
        shutil.copy2(mdpath, archive_dir / mdpath.name)
    update_task_status(user_scope_id, task_id, "approved")
    append_event(
        user_scope_id,
        task_id,
        "handoff_approved",
        {"handoff_id": handoff_id, "automation_action_result": action_result},
    )
    return True


def reject_handoff(user_scope_id: Optional[str], task_id: str, handoff_id: str, reason: str = "") -> bool:
    hpath = _task_root(user_scope_id, task_id) / "handoff" / f"{handoff_id}.json"
    mdpath = _task_root(user_scope_id, task_id) / "handoff" / f"{handoff_id}.md"
    handoff = _load_json(hpath, {})
    if not isinstance(handoff, dict) or not handoff.get("id"):
        return False
    handoff["status"] = "rejected"
    handoff["rejected_at"] = datetime.now().isoformat()
    if reason:
        handoff["rejected_reason"] = reason[:500]
    _save_json(hpath, handoff)

    archive_dir = ensure_workspace(user_scope_id) / "archive" / "rejected" / task_id
    archive_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(hpath, archive_dir / hpath.name)
    if mdpath.exists():
        shutil.copy2(mdpath, archive_dir / mdpath.name)
    update_task_status(user_scope_id, task_id, "rejected")
    append_event(user_scope_id, task_id, "handoff_rejected", {"handoff_id": handoff_id, "reason": reason[:200]})
    return True


def collect_existing_task_sources(user_scope_id: Optional[str], limit: int = 5) -> List[Dict[str, str]]:
    """
    Collect lightweight task candidates from existing stores (todos/notes/thinking notes).
    Returns normalized records: {source, title, content}.
    """
    out: List[Dict[str, str]] = []
    try:
        from vaf.core.automation_planner import list_todos, list_notes

        for t in list_todos(user_scope_id):
            if bool(t.get("done")):
                continue
            out.append(
                {
                    "source": "automation_todo",
                    "title": f"Todo {t.get('id', '')}".strip(),
                    "content": (t.get("text") or "").strip(),
                }
            )
        for n in list_notes(user_scope_id):
            out.append(
                {
                    "source": "automation_note",
                    "title": (n.get("title") or f"Note {n.get('id', '')}").strip(),
                    "content": (n.get("content") or "").strip(),
                }
            )
    except Exception:
        pass

    try:
        from vaf.core.thinking_notes import get_notes

        for n in get_notes(_scope_key(user_scope_id))[:limit]:
            out.append(
                {
                    "source": "thinking_note",
                    "title": f"Thinking note {n.get('created_at_iso', '')}".strip(),
                    "content": (n.get("note") or "").strip(),
                }
            )
    except Exception:
        pass

    cleaned = [x for x in out if x.get("content")]
    return cleaned[: max(1, min(limit, 20))]


def mirror_working_memory_snapshot(
    user_scope_id: Optional[str],
    snapshot: Dict[str, Any],
    source: str = "working_memory_sync",
) -> Optional[str]:
    """
    Store the latest working_memory snapshot inside Thinking Workspace.
    Returns task_id on success, None on failure.
    """
    try:
        task = get_or_create_open_task_by_source(
            user_scope_id=user_scope_id,
            source=source,
            title="Working memory sync",
            description="Auto-synced snapshots from update_working_memory.",
        )
        task_id = task.get("id")
        if not task_id:
            return None
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        payload = json.dumps(snapshot or {}, indent=2, ensure_ascii=False)
        write_workspace_file(user_scope_id, task_id, "working_memory/latest.json", payload)
        write_workspace_file(user_scope_id, task_id, f"working_memory/history/{now}.json", payload)
        append_event(user_scope_id, task_id, "working_memory_mirrored", {"snapshot_ts": now})
        return str(task_id)
    except Exception:
        return None


def sync_automation_status_to_workspace(
    user_scope_id: Optional[str],
    automation_data: Dict[str, Any],
    run_status: str = "",
    summary: str = "",
    event: str = "automation_sync",
) -> Optional[str]:
    """
    Mirror automation state into a workspace task so Thinking Mode can inspect
    lifecycle signals (enabled/disabled/last_run/next_run/status).
    """
    try:
        aid = (automation_data.get("id") or "").strip()
        if not aid:
            return None
        source = f"automation:{aid}"
        title = f"Automation {automation_data.get('name') or aid}"
        task = get_or_create_open_task_by_source(
            user_scope_id=user_scope_id,
            source=source,
            title=title,
            description="Mirrored automation lifecycle state.",
        )
        task_id = task.get("id")
        if not task_id:
            return None
        meta = get_task(user_scope_id, task_id) or {}
        if not isinstance(meta, dict):
            meta = {}
        meta["automation"] = {
            "id": automation_data.get("id"),
            "name": automation_data.get("name"),
            "description": automation_data.get("description"),
            "frequency": automation_data.get("frequency"),
            "time": automation_data.get("time"),
            "enabled": automation_data.get("enabled"),
            "last_run": automation_data.get("last_run"),
            "last_completed_local_date": automation_data.get("last_completed_local_date"),
            "next_run": automation_data.get("next_run"),
            "run_status": run_status or None,
            "summary": (summary or "")[:1000] if summary else None,
        }
        meta["updated_at"] = datetime.now().isoformat()
        _save_json(_task_meta_path(user_scope_id, task_id), meta)
        append_event(
            user_scope_id,
            task_id,
            event,
            {
                "automation_id": aid,
                "run_status": run_status,
                "enabled": automation_data.get("enabled"),
            },
        )
        return str(task_id)
    except Exception:
        return None

