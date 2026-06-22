"""
Per-user log of questions/proposals the background thinking run raised with the user, with a status
lifecycle so (a) the next run does not re-ask, and (b) the main agent can pick up a confirmed proposal.

Status lifecycle:
    asked      -> the background run asked the user (waiting for a reply)
    confirmed  -> the user agreed (main agent should carry it out)
    done       -> the proposed action was carried out
    declined   -> the user refused

Stored per user under thinking_requests / <user_scope_id> (mirrors thinking_suggestions.py). Keyed by
the raw scope id: both the thinking run and the main agent now operate under the user's real scope, so
they read/write the same store (see thinking_mode._run_thinking_for_user's scope resolution).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.platform import Platform

STATUSES = ("asked", "confirmed", "done", "declined")
_MAX_ENTRIES = 50  # keep the newest N per user


def _dir(user_scope_id: Optional[str]) -> Path:
    base = Platform.vaf_dir() / "thinking_requests"
    if user_scope_id:
        return base / str(user_scope_id).strip()
    return base / "_default"


def _path(user_scope_id: Optional[str]) -> Path:
    return _dir(user_scope_id) / "requests.json"


def _load(path: Path) -> List[dict]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(path: Path, data: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _now() -> str:
    return datetime.now().isoformat()


def add_request(
    user_scope_id: Optional[str],
    question: str,
    run_seq: int,
    proposed_action: Optional[str] = None,
    thinking_run_id: Optional[str] = None,
    source_note_id: Optional[str] = None,
    source_todo_id: Optional[str] = None,
    details: Optional[str] = None,
) -> dict:
    """Record a new 'asked' request. source_note_id / source_todo_id link the request to the
    automation note/todo it came from, so that note/todo can be marked handled once the user
    confirms (and stops re-surfacing). `details` carries the concrete information behind a teaser
    message (e.g. the actual list of tips the run found) so the main agent can answer a follow-up with
    the REAL facts instead of re-deriving them. Returns the created entry (with id)."""
    path = _path(user_scope_id)
    items = _load(path)
    entry = {
        "id": str(uuid.uuid4())[:8],
        "question": (question or "").strip()[:1000],
        "proposed_action": (proposed_action or "").strip()[:500] or None,
        "details": (details or "").strip()[:4000] or None,
        "status": "asked",
        "run_seq": int(run_seq) if run_seq is not None else 0,
        "thinking_run_id": (thinking_run_id or "").strip() or None,
        "source_note_id": (source_note_id or "").strip() or None,
        "source_todo_id": (source_todo_id or "").strip() or None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    items.append(entry)
    items = items[-_MAX_ENTRIES:]
    _save(path, items)
    return entry


def get_request(user_scope_id: Optional[str], request_id: str) -> Optional[dict]:
    if not request_id:
        return None
    for e in _load(_path(user_scope_id)):
        if isinstance(e, dict) and e.get("id") == request_id:
            return e
    return None


def update_request_status(user_scope_id: Optional[str], request_id: str, status: str) -> Optional[dict]:
    """Move a request to a new status. Returns the updated entry, or None if not found."""
    status = (status or "").strip().lower()
    if status not in STATUSES:
        return None
    path = _path(user_scope_id)
    items = _load(path)
    updated = None
    for e in items:
        if isinstance(e, dict) and e.get("id") == request_id:
            e["status"] = status
            e["updated_at"] = _now()
            updated = e
            break
    if updated is not None:
        _save(path, items)
    return updated


def list_requests(
    user_scope_id: Optional[str],
    status: Optional[str] = None,
    within_runs: Optional[int] = None,
    current_run_seq: Optional[int] = None,
) -> List[dict]:
    """List requests, newest first. Optionally filter by status, and by recency
    (within `within_runs` thinking runs of `current_run_seq`)."""
    items = [e for e in _load(_path(user_scope_id)) if isinstance(e, dict) and e.get("id")]
    if status:
        s = str(status).strip().lower()
        items = [e for e in items if (e.get("status") or "asked") == s]
    if within_runs is not None and current_run_seq is not None:
        items = [
            e for e in items
            if (int(current_run_seq) - int(e.get("run_seq") or 0)) < int(within_runs)
        ]
    return sorted(items, key=lambda e: (e.get("created_at") or ""), reverse=True)


def recent_requests_prompt(
    user_scope_id: Optional[str],
    current_run_seq: int,
    within_runs: int = 6,
) -> str:
    """Prompt block listing the requests raised in the last `within_runs` runs and their status, so the
    agent does NOT re-ask, can follow up on confirmed ones, and treats declined ones like the declined
    list. Returns '' when there is nothing recent."""
    recent = list_requests(user_scope_id, within_runs=within_runs, current_run_seq=current_run_seq)
    if not recent:
        return ""
    lines = [
        "**Requests you already raised with the user recently — do NOT ask these again:**",
    ]
    for e in recent:
        st = e.get("status") or "asked"
        q = (e.get("question") or "").strip().replace("\n", " ")[:160]
        act = e.get("proposed_action")
        suffix = f" (action: {act})" if act else ""
        lines.append(f"- [{st}] \"{q}\"{suffix}")
    lines.append(
        "Rules: 'asked'/'confirmed' are still in flight — do not repeat them. 'done' is finished — do not "
        "mention it again. 'declined' was refused — never re-propose it."
    )
    return "\n".join(lines)
