# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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
import re
import shutil
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from vaf.core.log_helper import append_domain_log
from vaf.core.path_jail import PathEscape, contained_path, safe_entry_name
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


# Task and handoff ids are 8 hex characters. A longer name is refused before any
# filesystem call: one past the file system's name limit (255 BYTES, so a short id of
# multibyte characters can pass a character count) made the OS error, which quotes the
# absolute data path, the answer a client saw. 128 bytes plus ".json" stays far below it.
_MAX_ID_BYTES = 128


def _entry(name: str) -> str:
    """`name` as one entry of at most `_MAX_ID_BYTES` UTF-8 bytes, or `PathEscape`. A name
    that does not encode at all (a lone surrogate) is refused too: the filesystem call
    would fail on it."""
    entry = safe_entry_name(name)
    try:
        size = len(entry.encode("utf-8"))
    except UnicodeEncodeError:
        raise PathEscape("name is not encodable")
    if size > _MAX_ID_BYTES:
        raise PathEscape("name is too long")
    return entry


def describe_os_error(error: OSError) -> str:
    """What a caller may read of an `OSError`: this module's own not-found errors carry no
    file name and pass through as they are; one the operating system raised quotes the
    server's absolute path, so only its reason is given (`File name too long`, `No space
    left on device`)."""
    if error.filename is None and error.filename2 is None:
        return str(error)
    return error.strerror or type(error).__name__


class HandoffAlreadyDecided(Exception):
    """The handoff was approved or rejected before; it is not applied again."""


class HandoffBusy(Exception):
    """Another decision of the same account held the decision lock past the timeout."""


class HandoffNotSaved(Exception):
    """A decision could not be written. The handoff is still pending; when its approval was
    already claimed, the next approve or reject finishes it without applying it twice."""


# One decision at a time per account. The holder's work takes milliseconds (8 ms median,
# 326 ms at most, measured with a create action); a caller that waits longer than this
# answers "busy" instead of blocking its connection indefinitely.
_DECISION_LOCK_TIMEOUT_S = 5.0

# Used only when the filelock package cannot be imported: it serializes the decisions of
# this process (the tray's two listeners run in one), not those of a second process.
_FALLBACK_DECISION_LOCK = threading.Lock()


@contextmanager
def _decision_lock(user_scope_id: Optional[str]):
    """Hold the account's decision lock, `<workspace>/handoff.lock`, or raise `HandoffBusy`.

    A kernel lock (`flock`, `msvcrt.locking` through filelock), not a marker file: the
    operating system releases it when its holder dies, so a crashed process never blocks the
    next decision, and two threads of one process exclude each other because every call
    opens its own descriptor. The path comes from the connection's scope alone, and the
    machine owner's aliases share it because they share the workspace.

    Named boundary: without filelock only this process is serialized."""
    from vaf.core.secure_store import _get_filelock_cls

    timeout = _DECISION_LOCK_TIMEOUT_S
    lock_cls = _get_filelock_cls()
    if lock_cls is None:
        if not _FALLBACK_DECISION_LOCK.acquire(timeout=timeout):
            raise HandoffBusy("handoff is being decided")
        try:
            yield
        finally:
            _FALLBACK_DECISION_LOCK.release()
        return
    from filelock import Timeout

    lock = lock_cls(str(ensure_workspace(user_scope_id) / "handoff.lock"), timeout=timeout)
    try:
        lock.acquire()
    except Timeout:
        # A `Timeout` is an `OSError`; left as it is, a caller would answer "not found".
        raise HandoffBusy("handoff is being decided") from None
    try:
        yield
    finally:
        lock.release()


def _task_root(user_scope_id: Optional[str], task_id: str) -> Path:
    """A task's directory. `task_id` reaches here from the client (the WebSocket handoff
    commands) and from the model (the workspace tools), so it must be ONE entry: a walk
    such as `../../<other scope>/tasks/<id>` would otherwise land in another account's
    workspace. Raises `PathEscape` (a `ValueError`)."""
    return ensure_workspace(user_scope_id) / "tasks" / _entry(task_id)


def _handoff_paths(user_scope_id: Optional[str], task_id: str,
                   handoff_id: str) -> Optional[Tuple[Path, Path]]:
    """(json, md) of one handoff, or None when either id is not a single entry. Callers
    answer None exactly like a missing handoff: a walk in the handoff id reached any
    account's automation file, and a different answer would tell which ids exist."""
    try:
        handoff_dir = _task_root(user_scope_id, task_id) / "handoff"
        return (handoff_dir / _entry(f"{handoff_id}.json"),
                handoff_dir / _entry(f"{handoff_id}.md"))
    except PathEscape:
        return None


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
    """Encode first, then temp file + replace: a value that does not encode (a lone
    surrogate) raises and leaves the old file as it was, where a truncating write emptied
    it. Plain JSON (`encrypt=False`): `_load_json` and the handoff list read it as such."""
    from vaf.core.data_files import write_json_atomic

    write_json_atomic(path, value, encrypt=False, indent=2)


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
    try:
        meta = _load_json(_task_meta_path(user_scope_id, task_id), {})
    except PathEscape:
        return None
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
    target = contained_path(base, relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with open(target, mode, encoding="utf-8") as f:
        f.write(content or "")
    append_event(user_scope_id, task_id, "workspace_file_written", {"path": relative_path, "append": append})
    return target


def read_workspace_file(user_scope_id: Optional[str], task_id: str, relative_path: str) -> str:
    try:
        base = _task_root(user_scope_id, task_id) / "workspace"
    except PathEscape:
        raise FileNotFoundError("File not found")
    target = contained_path(base, relative_path)
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
    paths = _handoff_paths(user_scope_id, task_id, handoff_id)
    if paths is None:
        return None
    handoff = _load_json(paths[0], {})
    if not isinstance(handoff, dict) or not handoff.get("id"):
        return None
    return handoff


def _apply_automation_action(user_scope_id: Optional[str], action: Dict[str, Any], *,
                             planned_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Apply approved automation action from a handoff.
    Supported operations:
    - create: requires prompt; optional name/description/frequency/time/weekday/day/enabled
    - update: requires task_id; supports name/description/prompt/frequency/time/weekday/day/enabled

    `planned_id` is the id the approval's claim chose for a create. When a task with that
    id already exists, an earlier attempt created it and stopped before the decision was
    saved, so it is reported, not created a second time.

    Named boundary: the update asks `AutomationAccess(user_scope_id)` WITHOUT the role, so a
    second admin's approved proposal reaches only its own automations, not the root and
    machine-owner tasks its list shows. Narrower than the WebSocket and the tools, and safe;
    the approval lane carries no role today.
    """
    from vaf.core.automation import AutomationAccess, AutomationManager, AutomationTask

    op = str(action.get("operation") or "").strip().lower()
    if not str(user_scope_id or "").strip():
        # The unscoped manager is every account's store; an action without an account is
        # refused, never applied there.
        return {"ok": False, "error": "no account for this action"}
    if op == "create":
        mgr = AutomationManager(user_scope_id=user_scope_id)
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
        if planned_id and mgr.get(planned_id) is not None:
            return {"ok": True, "operation": "create", "task_id": planned_id}
        task = AutomationTask(
            **({"id": planned_id} if planned_id else {}),
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
        # The same rule as every other automation lane: an id this account does not see
        # is "not found".
        mgr, _seen = AutomationAccess(user_scope_id).get(target_id)
        if mgr is None:
            return {"ok": False, "error": "automation not found"}
        task = mgr.update(target_id, **update_params)
        if not task:
            return {"ok": False, "error": "automation not found"}
        return {"ok": True, "operation": "update", "task_id": task.id}

    return {"ok": False, "error": "unsupported operation"}


def _decide(user_scope_id: Optional[str], task_id: str, handoff_id: str):
    """(hpath, mdpath, handoff) of a handoff that is still `pending`, None for one that
    does not exist (or whose id is refused), `HandoffAlreadyDecided` for one approved or
    rejected before. `create_handoff` writes `pending`; nothing else counts as undecided."""
    paths = _handoff_paths(user_scope_id, task_id, handoff_id)
    if paths is None:
        return None
    hpath, mdpath = paths
    handoff = _load_json(hpath, {})
    if not isinstance(handoff, dict) or not handoff.get("id"):
        return None
    if handoff.get("status") != "pending":
        raise HandoffAlreadyDecided(handoff_id)
    return hpath, mdpath, handoff


def _after_decision(user_scope_id: Optional[str], task_id: str, handoff_id: str, hpath: Path,
                    mdpath: Path, decision: str, event: str, details: Dict[str, Any]) -> None:
    """The bookkeeping after a decision is saved: archive copy, task status, event. Best
    effort: the decision stands once saved, so a failure here is logged and never turns
    the answer into "not found"."""
    try:
        archive_dir = ensure_workspace(user_scope_id) / "archive" / decision / task_id
        archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(hpath, archive_dir / hpath.name)
        if mdpath.exists():
            shutil.copy2(mdpath, archive_dir / mdpath.name)
        update_task_status(user_scope_id, task_id, decision)
        append_event(user_scope_id, task_id, event, details)
    except Exception as e:  # noqa: BLE001 - logged; the saved decision is the answer
        append_domain_log("backend", f"[THINKING_WORKSPACE] {event} {handoff_id}: "
                                     f"bookkeeping failed after the decision: {e!r}")


# An id this module chose for a planned create: 8 lowercase hex characters, the shape of
# `AutomationTask`'s default id. The id becomes a file name in the automation store, so a
# claim carrying anything else is not trusted as one.
_CLAIM_ID = re.compile(r"[0-9a-f]{8}")


def _valid_claim(handoff: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The approval claim saved on a pending handoff, or None when there is none this
    module would have written (no action, another shape, an id that is not 8 hex)."""
    claim = handoff.get("claim")
    if not isinstance(handoff.get("automation_action"), dict) or not isinstance(claim, dict):
        return None
    if claim.get("decision") != "approve":
        return None
    planned = claim.get("automation_id")
    if planned is not None and not (isinstance(planned, str) and _CLAIM_ID.fullmatch(planned)):
        return None
    return claim


def _unused_automation_id(user_scope_id: Optional[str]) -> str:
    """An 8-hex automation id that no task of this account's store carries yet."""
    from vaf.core.automation import AutomationManager

    taken = AutomationManager(user_scope_id=user_scope_id).tasks
    while True:
        candidate = uuid.uuid4().hex[:8]
        if candidate not in taken:
            return candidate


def _save_decision(hpath: Path, handoff: Dict[str, Any]) -> None:
    """`_save_json` for a decision record. A failure becomes `HandoffNotSaved`, with fixed
    text: the `OSError` names the server's data path."""
    try:
        _save_json(hpath, handoff)
    except (OSError, ValueError):
        raise HandoffNotSaved("handoff decision not saved") from None


def _approve_locked(user_scope_id: Optional[str], hpath: Path,
                    handoff: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Approve a pending handoff while the decision lock is held; the action's result.

    With an automation action the approval is CLAIMED on the pending record before the
    action runs, carrying the id a create will use, and the claim is replaced by the
    `approved` status after it. A crash or a failed save anywhere in between leaves the
    claim, and the next call finishes from it: a create whose task already exists is not
    created again, an update is applied again with the same values."""
    action = handoff.get("automation_action")
    if not isinstance(action, dict):
        handoff.pop("claim", None)
        handoff["status"] = "approved"
        handoff["approved_at"] = datetime.now().isoformat()
        _save_decision(hpath, handoff)
        return None
    claim = _valid_claim(handoff)
    if claim is None:
        claim = {"decision": "approve", "claimed_at": datetime.now().isoformat()}
        op = str(action.get("operation") or "").strip().lower()
        if op == "create" and str(user_scope_id or "").strip():
            # Never for a blank scope: that manager would be every account's store, and
            # the action itself is refused for it anyway.
            claim["automation_id"] = _unused_automation_id(user_scope_id)
        handoff["claim"] = claim
        _save_decision(hpath, handoff)
    result = _apply_automation_action(user_scope_id, action,
                                      planned_id=claim.get("automation_id"))
    handoff["automation_action_result"] = result
    handoff.pop("claim", None)
    handoff["status"] = "approved"
    handoff["approved_at"] = datetime.now().isoformat()
    _save_decision(hpath, handoff)
    return result


def approve_handoff(user_scope_id: Optional[str], task_id: str, handoff_id: str) -> bool:
    """Approve a pending handoff and apply its automation action, once. False for a missing
    handoff; `HandoffAlreadyDecided` for one decided before, so a retry never applies the
    action a second time; `HandoffBusy` when another decision of the account holds the lock
    past the timeout; `HandoffNotSaved` when a save failed (the handoff stays pending, and a
    claimed approval is finished by the next approve or reject).

    Decisions of one account run one at a time (`_decision_lock`), so two connections, the
    tray's two listeners or a second process cannot both pass the pending check.

    Named boundaries of the retry after a crash or a failed save: an update is applied again
    with the approved values, so a manual edit of that automation in between is overwritten;
    a created task that was deleted in between is created again, because that is what the
    approval asked for; another task created in the same account with the same random 8-hex
    id in between would be taken for the planned one (a 2^-32 chance)."""
    with _decision_lock(user_scope_id):
        found = _decide(user_scope_id, task_id, handoff_id)
        if found is None:
            return False
        hpath, mdpath, handoff = found
        action_result = _approve_locked(user_scope_id, hpath, handoff)
    _after_decision(user_scope_id, task_id, handoff_id, hpath, mdpath, "approved",
                    "handoff_approved",
                    {"handoff_id": handoff_id, "automation_action_result": action_result})
    return True


def reject_handoff(user_scope_id: Optional[str], task_id: str, handoff_id: str, reason: str = "") -> bool:
    """Reject a pending handoff. False for a missing one; `HandoffAlreadyDecided` for one
    decided before; `HandoffBusy` and `HandoffNotSaved` as for `approve_handoff`.

    An approval that was claimed first is the decision: a reject finishes it (an automation
    may already exist under it) and answers `HandoffAlreadyDecided`, never stores `rejected`.
    A lone surrogate in the reason becomes `?`: it cannot be encoded, and the save would
    fail on it."""
    reason = str(reason or "").encode("utf-8", errors="replace").decode("utf-8")
    finished_approval = False
    with _decision_lock(user_scope_id):
        found = _decide(user_scope_id, task_id, handoff_id)
        if found is None:
            return False
        hpath, mdpath, handoff = found
        if _valid_claim(handoff) is not None:
            action_result = _approve_locked(user_scope_id, hpath, handoff)
            finished_approval = True
        else:
            handoff.pop("claim", None)
            handoff["status"] = "rejected"
            handoff["rejected_at"] = datetime.now().isoformat()
            if reason:
                handoff["rejected_reason"] = reason[:500]
            _save_decision(hpath, handoff)
    if finished_approval:
        _after_decision(user_scope_id, task_id, handoff_id, hpath, mdpath, "approved",
                        "handoff_approved",
                        {"handoff_id": handoff_id, "automation_action_result": action_result})
        raise HandoffAlreadyDecided(handoff_id)
    _after_decision(user_scope_id, task_id, handoff_id, hpath, mdpath, "rejected",
                    "handoff_rejected", {"handoff_id": handoff_id, "reason": reason[:200]})
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

