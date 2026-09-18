# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The outbox routes (/api/outbox/*): what the agent prepared and the person has not sent yet.

A send the agent makes on the person's own web chat turn is parked instead of delivered
(`vaf/core/outbound_hold.py`); these are the three verbs the card needs: what is waiting, send
it, drop it.

Rules, the same ones the inbox routes follow:
- The caller is `contact_routes.get_current_vaf_user`, and the identity for the store comes
  from that dependency, NEVER from the request body: a draft belongs to whoever parked it.
- Every store call runs under `asyncio.to_thread`; nothing here blocks the event loop.
- Two lanes, two verbs behind one surface. A mail draft is an artifact that already exists in
  the mail outbox, so sending it means releasing and draining it; a parked messenger call is
  re-dispatched through its own tool. `kind` says which lane an id belongs to, and an id is
  only ever looked up in the caller's own store.
"""
import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from vaf.api.contact_routes import get_current_vaf_user

router = APIRouter(prefix="/api/outbox", tags=["outbox"])

_LIMIT_MAX = 100


def _mail_service(user: Dict[str, Any]):
    """The caller's own mail service, or None when this identity has no mail lane at all.

    None rather than an exception for the two cases that MEAN "no mail here": a scope the
    fail-closed constructor refuses, and an install whose mail module is not importable. Not
    for anything else. `MailStore` creates its file on construction, so a missing store is not
    an error at all - it answers with an empty outbox - and a broad `except` could only ever
    turn a real failure (a permission error, a corrupt database) into "no mail account", which
    is the one answer that sends the person looking in the wrong place. Those propagate and
    the route reports an operational error. The scope comes from the auth dependency, so a
    mail id can only ever be looked up in the caller's own outbox.
    """
    scope = (user.get("user_scope_id") or "").strip()
    if not scope:
        return None
    try:
        from vaf.mail.service import MailService
    except ImportError:
        return None
    try:
        return MailService(scope)
    except ValueError:
        return None


def _send_mail_draft(user: Dict[str, Any], op_id: int) -> Dict[str, Any]:
    """Release a held mail draft and drain it. The act itself lives in the mail layer
    (`vaf.mail.service.release_held_draft`), so this route and `vaf outbox send` cannot
    disagree about what Send does."""
    svc = _mail_service(user)
    if svc is None:
        return {"ok": False, "error": "no mail account"}
    from vaf.mail.service import release_held_draft
    return release_held_draft(str(user.get("user_scope_id") or ""), str(user.get("username") or ""),
                              int(op_id), service=svc)


@router.get("")
async def list_outbox(request: Request, limit: int = 50,
                      session_id: Optional[str] = None) -> Dict[str, Any]:
    """What is waiting for this person's word, newest first, in one row shape.

    `session_id` narrows it to one conversation, which is what the card in a chat asks for: a
    message the agent is preparing in one chat must not turn up in another. Without it the
    whole person's list comes back, which is what the CLI and any overview want.
    """
    user = get_current_vaf_user(request)
    from vaf.core.outbound_hold import pending
    rows = await asyncio.to_thread(pending, user["username"], user["user_scope_id"],
                                   limit=min(max(int(limit or 1), 1), _LIMIT_MAX),
                                   session_id=session_id)
    return {"rows": rows, "count": len(rows)}


@router.post("/{kind}/{entry_id}/send")
async def send_entry(kind: str, entry_id: int, request: Request) -> Dict[str, Any]:
    """Send one waiting draft now. A failure leaves it waiting, with the reason."""
    user = get_current_vaf_user(request)
    if kind == "mail":
        result = await asyncio.to_thread(_send_mail_draft, user, int(entry_id))
        if not result.get("ok") and result.get("error") == "not waiting":
            raise HTTPException(status_code=404, detail="draft not found")
        return result
    if kind == "call":
        from vaf.core.outbound_hold import approve_call
        # The role comes from the SESSION, not from the auth helper's two-key answer, which
        # carries none: reading `user["role"]` there sent every approval as a plain user, and a
        # tool that installs a file jail from the role would attach a second administrator's
        # file under a jail their own chat turn never had.
        role = str(((getattr(request.state, "user", None) or {}).get("role")) or "user")
        result = await asyncio.to_thread(
            approve_call, int(entry_id), username=user["username"],
            user_scope_id=user["user_scope_id"], user_role=role)
        return {"ok": bool(result.get("ok")),
                "error": "" if result.get("ok") else result.get("result", "")}
    raise HTTPException(status_code=400, detail="unknown kind")


@router.delete("/{kind}/{entry_id}")
async def discard_entry(kind: str, entry_id: int, request: Request) -> Dict[str, Any]:
    """Drop one waiting draft. Nothing was on the wire, so nothing is recalled."""
    user = get_current_vaf_user(request)
    if kind == "mail":
        svc = _mail_service(user)
        if svc is None:
            raise HTTPException(status_code=404, detail="draft not found")
        ok = await asyncio.to_thread(svc.discard_draft, int(entry_id))
        if not ok:
            raise HTTPException(status_code=404, detail="draft not found")
        return {"ok": True}
    if kind == "call":
        from vaf.core.outbound_hold import discard_call
        ok = await asyncio.to_thread(discard_call, int(entry_id), username=user["username"],
                                     user_scope_id=user["user_scope_id"])
        if not ok:
            raise HTTPException(status_code=404, detail="draft not found")
        return {"ok": True}
    raise HTTPException(status_code=400, detail="unknown kind")
