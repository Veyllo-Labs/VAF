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


class HandoffAlreadyApproved(HandoffAlreadyDecided):
    """A reject found an approval that was claimed and had already taken effect (its planned
    automation exists), finished that approval instead and did not reject. `action_result`
    is the approval's result: the account is told what was created."""

    def __init__(self, handoff_id: str, action_result: Optional[Dict[str, Any]] = None):
        super().__init__(handoff_id)
        self.action_result = action_result


class HandoffBusy(Exception):
    """Another decision of the same account held the decision lock past the timeout."""


class HandoffNotSaved(Exception):
    """A decision could not be made or written (the lock file could not be opened, a save
    failed, the automation store could not be written). The handoff is still pending; a
    claimed approval is finished by the next approve, or given up by a reject while its
    planned automation does not exist."""


# One decision at a time per account. The holder's work takes milliseconds (8 ms median,
# 326 ms at most, measured with a create action); a caller that waits longer than this
# answers "busy" instead of blocking its connection indefinitely.
_DECISION_LOCK_TIMEOUT_S = 5.0

# Used only when the filelock package cannot be imported: one in-process lock per workspace
# (the machine owner's aliases share one, as they share the workspace). It serializes the
# decisions of this process (the tray's two listeners run in one), not those of a second
# process, and one account's decision never makes another account's wait.
_FALLBACK_DECISION_LOCKS: Dict[str, threading.Lock] = {}
_FALLBACK_DECISION_LOCKS_GUARD = threading.Lock()


def _fallback_decision_lock(user_scope_id: Optional[str]) -> threading.Lock:
    key = _scope_key(user_scope_id)
    with _FALLBACK_DECISION_LOCKS_GUARD:
        return _FALLBACK_DECISION_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _decision_lock(user_scope_id: Optional[str], timeout: Optional[float] = None):
    """Hold the account's decision lock, `<workspace>/handoff.lock`, or raise `HandoffBusy`
    (waited past the timeout) or `HandoffNotSaved` (the lock file could not be opened).

    A kernel lock (`flock`, `msvcrt.locking` through filelock), not a marker file: the
    operating system releases it when its holder dies, so a crashed process never blocks the
    next decision, and two threads of one process exclude each other because every call
    opens its own descriptor. The path comes from the connection's scope alone, and the
    machine owner's aliases share it because they share the workspace.

    `timeout` is how long to wait for the lock (`_DECISION_LOCK_TIMEOUT_S` when None); 0 tries
    once, for a caller that waits elsewhere (the WebSocket waits on its event loop, so no
    worker thread is held while another decision has the lock).

    Named boundary: without filelock only this process is serialized."""
    from vaf.core.secure_store import _get_filelock_cls

    timeout = _DECISION_LOCK_TIMEOUT_S if timeout is None else max(0.0, float(timeout))
    lock_cls = _get_filelock_cls()
    if lock_cls is None:
        try:
            ensure_workspace(user_scope_id)
        except OSError as e:
            append_domain_log("backend", f"[THINKING_WORKSPACE] workspace not set up: {e!r}")
            raise HandoffNotSaved("handoff decision not saved") from None
        fallback = _fallback_decision_lock(user_scope_id)
        if not fallback.acquire(timeout=timeout):
            raise HandoffBusy("handoff is being decided")
        try:
            yield
        finally:
            fallback.release()
        return
    from filelock import Timeout

    try:
        lock = lock_cls(str(ensure_workspace(user_scope_id) / "handoff.lock"), timeout=timeout)
        lock.acquire()
    except Timeout:
        # A `Timeout` is an `OSError`; left as it is, a caller would answer "not found".
        raise HandoffBusy("handoff is being decided") from None
    except OSError as e:
        # The lock file could not be opened (a permission error): the handoff may exist, so
        # this is not "not found". The error names the data path and goes to the log only.
        append_domain_log("backend", f"[THINKING_WORKSPACE] decision lock not taken: {e!r}")
        raise HandoffNotSaved("handoff decision not saved") from None
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


def _ids_are_entries(task_id: str, handoff_id: str) -> bool:
    """Whether both client ids are single entries, decided without touching the filesystem:
    asked before the decision lock, where a refused id must answer "not found" at once and a
    failing workspace setup must not (it answers "not saved" inside the lock)."""
    try:
        _entry(task_id)
        _entry(f"{handoff_id}.json")
        _entry(f"{handoff_id}.md")
    except PathEscape:
        return False
    return True


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


# Fields of an `automation_action` that the action reads as text (`.strip()`) or stores on
# the automation as they are (an update's fields): any other type made the approval raise on
# every attempt, or would put a number where the scheduler reads a time.
_ACTION_TEXT_FIELDS = ("operation", "task_id", "prompt", "name", "description", "frequency",
                       "time", "weekday")


def _action_type_error(action: Dict[str, Any]) -> Optional[str]:
    """The first field of an automation action with a type or value the action cannot use,
    or None. Text fields must be strings, `day` a whole number or a string (it is parsed),
    `enabled` a bool, a number or a word that says on or off (`read_enabled`)."""
    for key in _ACTION_TEXT_FIELDS:
        value = action.get(key)
        if value is not None and not isinstance(value, str):
            return key
    day = action.get("day")
    if day is not None and (isinstance(day, bool) or not isinstance(day, (int, str))):
        return "day"
    enabled = action.get("enabled")
    if enabled is not None:
        from vaf.core.automation import read_enabled

        if read_enabled(enabled) is None:
            # Applied it would be off (it fails closed); refused here so the proposal
            # says what it means.
            return "enabled"
    return None


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
    if isinstance(automation_action, dict) and automation_action:
        bad = _action_type_error(automation_action)
        if bad is not None:
            # Refused where the proposal is made, so its author can correct it.
            raise ValueError(f"invalid automation action: {bad!r} has a type or value "
                             f"that cannot be used")
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

    `planned_id` is the id the approval's claim chose for a create. The caller
    (`_approve_locked`) has read the store strictly and found no task with it.

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
        # day, weekday and enabled are read by AutomationTask itself
        # (coerce_schedule_fields), like every other lane's.
        weekday = action.get("weekday")
        day = action.get("day")
        task = AutomationTask(
            **({"id": planned_id} if planned_id else {}),
            name=name,
            description=description,
            prompt=prompt,
            frequency=frequency,
            time=time_str,
            weekday=weekday if frequency == "weekly" else None,
            day=day if frequency == "monthly" else None,
            enabled=action.get("enabled", True),
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


_INVALID_ACTION = {"ok": False, "error": "invalid automation action"}


def _planned_task_exists(user_scope_id: Optional[str], planned_id: Optional[str]) -> bool:
    """Whether the automation a claimed create planned is in the account's store, read
    strictly (`scoped_task_record`): a task file that exists but cannot be read or parsed is
    not "missing" (the loaders skip it, so `get()` would say so), and a decision built on
    that would reject over, or create again beside, an automation that is on disk. Such a
    file raises `HandoffNotSaved` and the claim stays pending. False for a blank scope:
    there is no account store to look in."""
    if not planned_id or not str(user_scope_id or "").strip():
        return False
    from vaf.core.automation import scoped_task_record

    try:
        return scoped_task_record(str(user_scope_id), planned_id) is not None
    except (OSError, ValueError, RecursionError) as e:
        # RecursionError: json.loads on a deeply nested file; unreadable all the same.
        append_domain_log("backend", f"[THINKING_WORKSPACE] planned automation unreadable: "
                                     f"{e!r}")
        raise HandoffNotSaved("handoff decision not saved") from None


def _created(planned_id: str) -> Dict[str, Any]:
    return {"ok": True, "operation": "create", "task_id": planned_id}


def _claimed_update_may_have_run(handoff: Dict[str, Any]) -> bool:
    """Whether a claimed update may already have changed its automation. Named boundary:
    that cannot be told from the store (the automation exists either way), so a claimed
    update of the right types counts as run; one of the wrong types never ran."""
    action = handoff.get("automation_action") or {}
    if _action_type_error(action) is not None:
        return False
    return str(action.get("operation") or "").strip().lower() == "update"


def _finish_approval(hpath: Path, handoff: Dict[str, Any],
                     result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Store the approval: the result replaces the claim, then the record is saved."""
    if result is not None:
        handoff["automation_action_result"] = result
    handoff.pop("claim", None)
    handoff["status"] = "approved"
    handoff["approved_at"] = datetime.now().isoformat()
    _save_decision(hpath, handoff)
    return result


def _approve_locked(user_scope_id: Optional[str], hpath: Path,
                    handoff: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Approve a pending handoff while the decision lock is held; the action's result.

    With an automation action the approval is CLAIMED on the pending record before the
    action runs, carrying the id a create will use, and the claim is replaced by the
    `approved` status after it. A crash or a failed save anywhere in between leaves the
    claim, and the next approve finishes from it: a planned create whose task is on disk is
    approved with that task and never created again (looked up first, before the type check,
    so a create that ran with a value an older version accepted is not refused afterwards);
    an update is applied again with the same values.

    An action whose fields have the wrong type (a number as the prompt) is never claimed:
    it is approved with the result "invalid automation action", like the action's own
    refusals ("create requires prompt"). Any other error of the action after the claim
    (not the store's `OSError`) ends the decision too: approved with the planned task when
    the create stored it before the error, "invalid automation action" otherwise. No error
    can keep a handoff pending for every later approve and reject."""
    action = handoff.get("automation_action")
    if not isinstance(action, dict):
        return _finish_approval(hpath, handoff, None)
    claim = _valid_claim(handoff)
    planned = claim.get("automation_id") if claim is not None else None
    if _planned_task_exists(user_scope_id, planned):
        return _finish_approval(hpath, handoff, _created(planned))
    if _action_type_error(action) is not None:
        return _finish_approval(hpath, handoff, dict(_INVALID_ACTION))
    if claim is None:
        claim = {"decision": "approve", "claimed_at": datetime.now().isoformat()}
        op = str(action.get("operation") or "").strip().lower()
        if op == "create" and str(user_scope_id or "").strip():
            # Never for a blank scope: that manager would be every account's store, and
            # the action itself is refused for it anyway.
            try:
                claim["automation_id"] = _unused_automation_id(user_scope_id)
            except OSError:
                raise HandoffNotSaved("handoff decision not saved") from None
        handoff["claim"] = claim
        _save_decision(hpath, handoff)
    planned = claim.get("automation_id")
    try:
        result = _apply_automation_action(user_scope_id, action, planned_id=planned)
    except OSError as e:
        # The automation store could not be written. The claim is saved, so this is the
        # state of a failed decision save and is answered the same way; the error text
        # names the server's data path and goes to the server log only.
        append_domain_log("backend", f"[THINKING_WORKSPACE] approve: automation action "
                                     f"failed after the claim: {e!r}")
        raise HandoffNotSaved("handoff decision not saved") from None
    except Exception as e:  # noqa: BLE001 - finished as a refusal; logged
        append_domain_log("backend", f"[THINKING_WORKSPACE] approve: automation action "
                                     f"raised after the claim: {e!r}")
        result = _created(planned) if _planned_task_exists(user_scope_id, planned) \
            else dict(_INVALID_ACTION)
    return _finish_approval(hpath, handoff, result)


def approve_handoff(user_scope_id: Optional[str], task_id: str, handoff_id: str, *,
                    lock_timeout: Optional[float] = None) -> bool:
    """Approve a pending handoff and apply its automation action, once. False for a missing
    handoff (or an id that is not one entry, answered before any wait for the lock);
    `HandoffAlreadyDecided` for one decided before, so a retry never applies the action a
    second time; `HandoffBusy` when another decision of the account holds the lock past the
    timeout; `HandoffNotSaved` when the decision could not be made or saved (the handoff
    stays pending, and a claimed approval is finished by the next approve).

    Decisions of one account run one at a time (`_decision_lock`), so two connections, the
    tray's two listeners or a second process cannot both pass the pending check.
    `lock_timeout` is how long to wait for that lock (five seconds when None, 0 to try once).

    Named boundaries of the retry after a crash or a failed save: an update is applied again
    with the approved values, so a manual edit of that automation in between is overwritten;
    a created task that was deleted in between is created again, because that is what the
    approval asked for; another task created in the same account with the same random 8-hex
    id in between would be taken for the planned one (a 2^-32 chance)."""
    if not _ids_are_entries(task_id, handoff_id):
        return False
    with _decision_lock(user_scope_id, lock_timeout):
        found = _decide(user_scope_id, task_id, handoff_id)
        if found is None:
            return False
        hpath, mdpath, handoff = found
        action_result = _approve_locked(user_scope_id, hpath, handoff)
    _after_decision(user_scope_id, task_id, handoff_id, hpath, mdpath, "approved",
                    "handoff_approved",
                    {"handoff_id": handoff_id, "automation_action_result": action_result})
    return True


def reject_handoff(user_scope_id: Optional[str], task_id: str, handoff_id: str, reason: str = "",
                   *, lock_timeout: Optional[float] = None) -> bool:
    """Reject a pending handoff. False for a missing one; `HandoffAlreadyDecided` for one
    decided before; `HandoffBusy` and `HandoffNotSaved` as for `approve_handoff`.

    A claimed approval that has not taken effect (its planned automation does not exist,
    or its action never ran) is given up: the reject wins and is stored like any other. One
    that may have taken effect is the decision: the reject finishes the approval and raises
    `HandoffAlreadyApproved` with its result, so the account learns what was created, and it
    never stores `rejected` over an automation that exists. A lone surrogate in the reason
    becomes `?`: it cannot be encoded, and the save would fail on it."""
    reason = str(reason or "").encode("utf-8", errors="replace").decode("utf-8")
    if not _ids_are_entries(task_id, handoff_id):
        return False
    finished_approval = False
    with _decision_lock(user_scope_id, lock_timeout):
        found = _decide(user_scope_id, task_id, handoff_id)
        if found is None:
            return False
        hpath, mdpath, handoff = found
        claim = _valid_claim(handoff)
        planned = claim.get("automation_id") if claim is not None else None
        if _planned_task_exists(user_scope_id, planned):
            # The create ran: finish from what was observed. Going through the approval
            # again would read the store a second time, and a delete in between (it does
            # not take this lock) would make it create the task anew.
            action_result = _finish_approval(hpath, handoff, _created(planned))
            finished_approval = True
        elif claim is not None and _claimed_update_may_have_run(handoff):
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
        raise HandoffAlreadyApproved(handoff_id, action_result)
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

