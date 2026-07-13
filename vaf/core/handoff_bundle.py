# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Per-user store of "handoff bundles": the full working context a background automation built before it
hit a genuine blocker / important clarification it could not resolve on its own.

A scheduled automation runs a real agent in the background (silently, no live Web UI emits). When that
agent must ask the user something it truly cannot decide, it raises ONE clean question via `ask_user`
and stores its entire working history here as a bundle. The question is recorded as a normal tracked
request (thinking_requests) carrying this bundle's id. When the user replies, the MAIN agent loads the
bundle and continues the work with full context — deliberately integrated (a concise note + a bounded,
compacted slice of the history), never raw-dumped over the user's chat.

Storage mirrors thinking_requests.py: one directory per user, keyed by the RAW scope id, so a bundle is
only ever read under the same scope it was written for. The writer resolves the scope exactly as the
request store does (`user_scope_id or get_local_admin_scope_id()`) and passes that SAME scope to both the
request and the bundle, so the main agent — which finds the request under its own scope — finds the
matching bundle under that scope too. A bundle written for user A is unreadable for user B.

Bundle status lifecycle:
    open      -> the automation raised a handoff and is waiting for the user
    resolved  -> the main agent picked up the reply and continued the work
    expired   -> never answered within the retention window; dropped by lazy cleanup
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.platform import Platform

STATUSES = ("open", "resolved", "expired")
_MAX_ENTRIES = 20            # keep the newest N bundles per user
_RETENTION_DAYS = 7         # an unanswered handoff is dropped after this many days
# Known message keys we persist (OpenAI-style); anything else is dropped so a stray non-serializable
# value on a history message can never break the atomic write.
_MSG_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name", "kind")


def _dir(user_scope_id: Optional[str]) -> Path:
    # Raw-scope keying, identical to thinking_requests._dir, so a bundle stays aligned with the request
    # that links to it (same scope -> same directory -> reader finds both or neither).
    base = Platform.vaf_dir() / "handoff_bundles"
    if user_scope_id:
        return base / str(user_scope_id).strip()
    return base / "_default"


def _path(user_scope_id: Optional[str], bundle_id: str) -> Path:
    return _dir(user_scope_id) / f"{bundle_id}.json"


def _now() -> datetime:
    return datetime.now()


# Per-message content cap for stored bundles. The only reader
# (_render_handoff_bundle) digests the last 8 messages at ~300 chars each, so
# storing unbounded content (or image payloads) is pure privacy residue on disk.
_MAX_MSG_CONTENT = 4000


def _sanitize_history(history: Optional[List[dict]]) -> List[dict]:
    """Reduce each message to JSON-safe known keys, coercing content to str. A defensive copy so the
    stored bundle can never be corrupted by a transient/non-serializable object on the live history.
    Data minimization: multimodal content keeps only its text parts (never base64 image payloads),
    and content is capped at _MAX_MSG_CONTENT chars."""
    out: List[dict] = []
    for msg in (history or []):
        if not isinstance(msg, dict):
            continue
        clean: Dict[str, Any] = {}
        for k in _MSG_KEYS:
            if k not in msg or msg[k] is None:
                continue
            v = msg[k]
            if k == "content" and isinstance(v, list):
                # Multimodal parts: keep text, drop image/audio payloads.
                texts = []
                for part in v:
                    if isinstance(part, dict) and part.get("type") == "text":
                        texts.append(str(part.get("text") or ""))
                v = "\n".join(t for t in texts if t)
            if k == "content" and not isinstance(v, str):
                v = str(v)
            if k == "content" and len(v) > _MAX_MSG_CONTENT:
                v = v[:_MAX_MSG_CONTENT] + " ...[truncated]"
            clean[k] = v
        if clean:
            out.append(clean)
    return out


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def _read(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def cleanup(user_scope_id: Optional[str]) -> int:
    """Drop expired bundles (past expires_at, or status 'expired'/'resolved' older than retention) and cap
    to the newest _MAX_ENTRIES. Runs lazily on create/load so the store cannot grow unbounded without a
    scheduler. Returns the number of files removed."""
    d = _dir(user_scope_id)
    if not d.exists():
        return 0
    now_iso = _now().isoformat()
    entries: List[tuple] = []  # (created_at, path, data)
    removed = 0
    for p in d.glob("*.json"):
        data = _read(p)
        if not isinstance(data, dict):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
            continue
        expires = str(data.get("expires_at") or "")
        if expires and expires < now_iso:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
            continue
        entries.append((str(data.get("created_at") or ""), p, data))
    # Cap to newest N (by created_at), drop the oldest overflow.
    if len(entries) > _MAX_ENTRIES:
        entries.sort(key=lambda e: e[0])  # oldest first
        for _created, p, _data in entries[: len(entries) - _MAX_ENTRIES]:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def create(
    user_scope_id: Optional[str],
    *,
    history: Optional[List[dict]],
    summary: str = "",
    question: str = "",
    proposed_action: Optional[str] = None,
    session_id: Optional[str] = None,
    source: str = "automation",
) -> dict:
    """Store a new bundle (status 'open') and return it (with id). `history` is a snapshot of the
    automation agent's full working context; it is sanitized + deep-copied on write."""
    now = _now()
    bundle = {
        "id": str(uuid.uuid4())[:8],
        "source": (source or "automation").strip() or "automation",
        "status": "open",
        "summary": (summary or "").strip()[:4000] or None,
        "question": (question or "").strip()[:1000],
        "proposed_action": (proposed_action or "").strip()[:500] or None,
        "session_id": (session_id or "").strip() or None,
        "history": _sanitize_history(history),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=_RETENTION_DAYS)).isoformat(),
        "updated_at": now.isoformat(),
    }
    _write_atomic(_path(user_scope_id, bundle["id"]), bundle)
    # Trim AFTER the write so the store is strictly bounded (drops expired + caps to the newest N).
    cleanup(user_scope_id)
    return bundle


def load(user_scope_id: Optional[str], bundle_id: str) -> Optional[dict]:
    """Load a bundle for THIS scope, or None (not found / wrong scope / expired). The scope is the
    isolation boundary: a bundle written for another user lives in another directory and is unreadable."""
    if not bundle_id:
        return None
    cleanup(user_scope_id)
    data = _read(_path(user_scope_id, str(bundle_id).strip()))
    if not isinstance(data, dict):
        return None
    if str(data.get("expires_at") or "") and str(data.get("expires_at")) < _now().isoformat():
        return None
    return data


def update_status(user_scope_id: Optional[str], bundle_id: str, status: str) -> Optional[dict]:
    """Move a bundle to a new status (open -> resolved at pickup, or -> expired). Returns the updated
    bundle, or None if not found / invalid status."""
    status = (status or "").strip().lower()
    if status not in STATUSES or not bundle_id:
        return None
    path = _path(user_scope_id, str(bundle_id).strip())
    data = _read(path)
    if not isinstance(data, dict):
        return None
    data["status"] = status
    data["updated_at"] = _now().isoformat()
    if status == "resolved":
        # A resolved bundle's history has no remaining reader (the pickup digest
        # was already rendered) - keep metadata + summary for audit, drop the
        # snapshot. Removes exactly the data that is pure privacy residue.
        data.pop("history", None)
    _write_atomic(path, data)
    return data


def list_bundles(user_scope_id: Optional[str], status: Optional[str] = None) -> List[dict]:
    """List bundles for a user, newest first, optionally filtered by status."""
    d = _dir(user_scope_id)
    if not d.exists():
        return []
    items: List[dict] = []
    for p in d.glob("*.json"):
        data = _read(p)
        if not isinstance(data, dict) or not data.get("id"):
            continue
        if status and (data.get("status") or "open") != str(status).strip().lower():
            continue
        items.append(data)
    return sorted(items, key=lambda e: (e.get("created_at") or ""), reverse=True)


def deliver_handoff(
    user_scope_id: Optional[str],
    *,
    message: str,
    proposed_action: Optional[str] = None,
    details: Optional[str] = None,
    history: Optional[List[dict]] = None,
    session_id: Optional[str] = None,
    username: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """The automation analogue of thinking_mode.deliver_tracked_message — minus the proactive-mode/evidence
    gate (which would silently drop a message outside a thinking run), plus a handoff bundle.

    It (1) stores the agent's full working context as a bundle, (2) records a tracked request linked to
    that bundle (so the main agent's reply pickup finds it), (3) sets waiting_for_reply, and (4) delivers
    the clean question to the user's Web UI session with the SAME defer-not-drop behavior as the reference
    (if the main agent is mid-turn, the request is recorded and surfaces on the user's next visit).

    Returns the request dict with an extra `delivered` flag + `bundle_id`, or None if `message` was empty.
    """
    message = (message or "").strip()
    if not message:
        return None

    # Resolve the scope EXACTLY like deliver_tracked_message, and pass the SAME value to both the bundle
    # and the request, so the main agent (which finds the request under its own scope) finds the bundle too.
    from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
    from vaf.core import thinking_requests as treq
    from vaf.core import thinking_mode as tm

    scope = user_scope_id or get_local_admin_scope_id()
    uname = (username or "").strip() or get_local_admin_username()

    bundle = create(
        scope,
        history=history,
        summary=details or "",
        question=message,
        proposed_action=proposed_action,
        session_id=session_id,
        source="automation",
    )

    req = treq.add_request(
        scope,
        question=message,
        run_seq=tm.current_run_seq(scope),
        proposed_action=(proposed_action or "").strip() or None,
        thinking_run_id=None,
        details=(details or "").strip() or None,
        session_id=session_id,
        bundle_id=bundle["id"],
    )

    # Anchor the question to one web session (the delivery target passed in, else the latest web session),
    # set the waiting state so the main agent picks up the reply, then deliver — deferring (not dropping)
    # the live emit if the user's main agent is mid-turn.
    _anchor_sid = (session_id or "").strip() or tm._latest_web_session_id(scope)
    tm.set_waiting_for_reply(
        scope, username=uname, display_name=uname,
        question_text=message, request_id=req["id"], session_id=_anchor_sid,
    )
    if tm._main_agent_busy(scope):
        sid = None
    else:
        sid = tm.emit_message_to_web_ui(scope, message, session_id=_anchor_sid)

    _effective_sid = sid or _anchor_sid
    if _effective_sid and req.get("session_id") != _effective_sid:
        treq.set_request_session(scope, req["id"], _effective_sid)
        req = treq.get_request(scope, req["id"]) or req
    if sid and sid != _anchor_sid:
        tm.set_waiting_for_reply(
            scope, username=uname, display_name=uname,
            question_text=message, request_id=req["id"], session_id=sid,
        )

    req = dict(req)
    req["delivered"] = bool(sid)
    req["bundle_id"] = bundle["id"]
    return req
