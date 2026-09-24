# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The outbox routes (/api/outbox/*): what the agent prepared and the person has not sent yet.

A send the agent makes on the person's own web chat turn is parked instead of delivered, and
the turn ends at it (`vaf/core/outbound_hold.py`); these are the verbs the card needs: what
this chat's drafts are, send one, drop one, change its words.

Rules, the same ones the inbox routes follow:
- The caller is `contact_routes.get_current_vaf_user`, and the identity for the store comes
  from that dependency, NEVER from the request body: a draft belongs to whoever parked it.
- Every store call runs under `asyncio.to_thread`; nothing here blocks the event loop.
- The verbs themselves are `outbound_hold.send_draft` / `discard_draft` / `revise_draft`, the
  same functions `vaf outbox` calls, so the two surfaces cannot disagree about what a click
  does. `kind` says which lane an id belongs to, and an id is only ever looked up in the
  caller's own store.
"""
import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from vaf.api.contact_routes import get_current_vaf_user

router = APIRouter(prefix="/api/outbox", tags=["outbox"])

_LIMIT_MAX = 100
_KINDS = ("mail", "call")


def _kind(kind: str) -> str:
    if kind not in _KINDS:
        raise HTTPException(status_code=400, detail="unknown kind")
    return kind


@router.get("")
async def list_outbox(request: Request, limit: int = 50, session_id: Optional[str] = None,
                      settled: bool = False) -> Dict[str, Any]:
    """This person's drafts, newest first, in one row shape.

    Without `settled`: what is still waiting (`pending`), for the whole person or, with
    `session_id`, for one conversation - a message the agent is preparing in one chat must not
    turn up in another. With `settled` and a `session_id`: every draft that chat produced,
    decided ones included, which is what the card in the conversation shows, each under the
    turn that wrote it.
    """
    user = get_current_vaf_user(request)
    from vaf.core import outbound_hold
    n = min(max(int(limit or 1), 1), _LIMIT_MAX)
    if settled and session_id:
        rows = await asyncio.to_thread(outbound_hold.chat_drafts, user["username"],
                                       user["user_scope_id"], session_id, limit=n)
    else:
        rows = await asyncio.to_thread(outbound_hold.pending, user["username"],
                                       user["user_scope_id"], limit=n, session_id=session_id)
    return {"rows": rows, "count": len(rows)}


@router.post("/{kind}/{entry_id}/send")
async def send_entry(kind: str, entry_id: int, request: Request) -> Dict[str, Any]:
    """Send one waiting draft now. A failure leaves it waiting, with the reason. A send that
    left wakes the chat it came from, so the agent carries on where its turn stopped."""
    user = get_current_vaf_user(request)
    from vaf.core import outbound_hold
    # The role comes from the SESSION, not from the auth helper's two-key answer, which
    # carries none: reading `user["role"]` there sent every approval as a plain user, and a
    # tool that installs a file jail from the role would attach a second administrator's
    # file under a jail their own chat turn never had.
    role = str(((getattr(request.state, "user", None) or {}).get("role")) or "user")
    result = await asyncio.to_thread(
        outbound_hold.send_draft, _kind(kind), int(entry_id), username=user["username"],
        user_scope_id=user["user_scope_id"], user_role=role)
    if kind == "mail" and not result.get("ok") and result.get("error") in ("not waiting", "no mail account"):
        raise HTTPException(status_code=404, detail="draft not found")
    return result


@router.delete("/{kind}/{entry_id}")
async def discard_entry(kind: str, entry_id: int, request: Request) -> Dict[str, Any]:
    """Drop one waiting draft. Nothing was on the wire, so nothing is recalled, and the turn
    that stopped at it stays ended."""
    user = get_current_vaf_user(request)
    from vaf.core import outbound_hold
    ok = await asyncio.to_thread(outbound_hold.discard_draft, _kind(kind), int(entry_id),
                                 username=user["username"], user_scope_id=user["user_scope_id"])
    if not ok:
        raise HTTPException(status_code=404, detail="draft not found")
    return {"ok": True}


@router.patch("/{kind}/{entry_id}")
async def revise_entry(kind: str, entry_id: int, request: Request) -> Dict[str, Any]:
    """Change a waiting draft's words: `{"body": ..., "subject": ...}` (subject for a mail
    only). The recipients are not editable here - a different recipient is a different
    message, and the agent writes that one."""
    user = get_current_vaf_user(request)
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    body, subject = payload.get("body"), payload.get("subject")
    if (body is not None and not isinstance(body, str)) or (subject is not None and not isinstance(subject, str)):
        raise HTTPException(status_code=400, detail="body and subject are text")
    if body is None and subject is None:
        raise HTTPException(status_code=400, detail="nothing to change")
    from vaf.core import outbound_hold
    result = await asyncio.to_thread(
        outbound_hold.revise_draft, _kind(kind), int(entry_id), username=user["username"],
        user_scope_id=user["user_scope_id"], body=body, subject=subject)
    if not result.get("ok"):
        if result.get("error") == "empty":
            raise HTTPException(status_code=400, detail="the text is empty")
        raise HTTPException(status_code=404, detail="draft not found")
    return result
