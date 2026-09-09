# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The inbox routes (/api/inbox/*): the Posteingang window and the footer badge read the rows
`vaf/core/inbox.py` builds. Design: docs/integrations/INBOX.md.

Rules:
- The caller is `contact_routes.get_current_vaf_user` (request.state.user, or the local admin
  outside network mode): the identity the contacts and the channel dashboards use.
- Every store read runs under asyncio.to_thread; nothing here blocks the event loop.
- A GET never waits on a bridge. The per-channel status is what this process knows about
  itself (the linked WhatsApp account, a running bridge, the mail accounts and their last
  sync), never a round trip to a Node process.
- Sending stays with the channel routes: the inbox opens the channel window (with the draft
  flag) instead of sending.
"""
import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from vaf.api.contact_routes import get_current_vaf_user

router = APIRouter(prefix="/api/inbox", tags=["inbox"])

_LIMIT_MAX = 500


def _channels(channel: Optional[str]) -> Optional[List[str]]:
    """`channel` is one name, a comma list, `all` or empty; an unknown name is a 400."""
    from vaf.core.inbox import CHANNELS
    wanted = [c.strip().lower() for c in (channel or "").split(",") if c.strip()]
    if not wanted or "all" in wanted:
        return None
    unknown = [c for c in wanted if c not in CHANNELS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown channel: {', '.join(unknown)}")
    return wanted


def _status(username: str, user_scope_id: Optional[str]) -> Dict[str, Any]:
    """What the process knows about each channel without asking a bridge."""
    from vaf.core.config import Config
    out: Dict[str, Any] = {}
    try:
        from vaf.api.whatsapp_bridge import is_bridge_running as wa_running
        from vaf.core.whatsapp_auth import whatsapp_auth_exists
        out["whatsapp"] = {"linked": bool(whatsapp_auth_exists(username)), "running": bool(wa_running())}
    except Exception:
        out["whatsapp"] = {"linked": False, "running": False}
    try:
        from vaf.api.telegram_bridge import is_bridge_running as tg_running
        tc = Config.get("telegram_config") or {}
        tc = tc if isinstance(tc, dict) else {}
        out["telegram"] = {"configured": bool(tc.get("bot_token") and tc.get("verified")), "running": bool(tg_running())}
    except Exception:
        out["telegram"] = {"configured": False, "running": False}
    out["discord"] = {"configured": False, "running": False}
    try:
        from vaf.core.contacts_store import is_local_admin_caller
        if is_local_admin_caller(username, user_scope_id):
            from vaf.api.discord_bridge import is_bridge_running as dc_running
            dc = Config.get("discord_config") or {}
            dc = dc if isinstance(dc, dict) else {}
            out["discord"] = {"configured": bool(dc.get("verified") and dc.get("admin_user_id")),
                              "running": bool(dc_running())}
    except Exception:
        pass
    out["mail"] = {"accounts": 0, "last_sync_at": None}
    try:
        from vaf.mail.store import MailStore
        from vaf.tools.mail_utils import mail_v2_active
        if user_scope_id and mail_v2_active(username, user_scope_id) and MailStore.exists(user_scope_id):
            accounts = MailStore(user_scope_id).list_accounts()
            out["mail"] = {"accounts": len(accounts),
                           "last_sync_at": max((a.get("last_sync_at") or "" for a in accounts), default="") or None}
    except Exception:
        pass
    return out


@router.get("")
async def list_inbox(request: Request, channel: Optional[str] = None, view: str = "all",
                     groups: bool = True, done: bool = False, q: str = "", limit: int = 200) -> Dict[str, Any]:
    """The rows of the Posteingang: `channel` (one name, a comma list, or all), `view` (all,
    waits, unread, agent), the `groups` and `done` toggles, `q`, `limit`; and the per-channel
    status the rail shows. Rows, counts and channels are `inbox.list_conversations`' own."""
    user = get_current_vaf_user(request)
    channels = _channels(channel)
    from vaf.core.inbox import list_conversations
    result = await asyncio.to_thread(
        list_conversations, user["username"], user["user_scope_id"], channels=channels, view=view,
        include_groups=groups, include_done=done, query=q, limit=min(max(int(limit or 1), 1), _LIMIT_MAX))
    result["status"] = await asyncio.to_thread(_status, user["username"], user["user_scope_id"])
    return result


@router.get("/summary")
async def inbox_summary(request: Request, groups: bool = True, done: bool = False) -> Dict[str, Any]:
    """The whole inbox's counts, whatever one channel the list is narrowed to: the footer
    badge reads `waits` and `unread`, the inbox window's rail reads every count (per view
    and per channel) under the same group and done toggles as its list."""
    user = get_current_vaf_user(request)
    from vaf.core.inbox import list_conversations
    counts = (await asyncio.to_thread(list_conversations, user["username"], user["user_scope_id"], limit=1,
                                      include_groups=groups, include_done=done))["counts"]
    return dict(counts)


@router.get("/history")
async def inbox_history(request: Request, channel: str, id: str, limit: int = 200) -> Dict[str, Any]:
    """One conversation, oldest first, in the channel windows' pane shape (role, content,
    timestamp, content_type, sender)."""
    user = get_current_vaf_user(request)
    from vaf.core.inbox import conversation_history
    try:
        messages = await asyncio.to_thread(conversation_history, user["username"], user["user_scope_id"],
                                           channel, id, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"messages": messages}


@router.post("/marks")
async def inbox_marks(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """{channel, id, seen?, done?}: the person opened a conversation (`seen`, which takes it
    off "waits for you"); `done` is the primitive without a button. A room's seen is refused:
    opening the room moves its cursor. A Discord mark from anybody but the local admin is
    refused too, as the Discord rows are."""
    user = get_current_vaf_user(request)
    from vaf.core.inbox import mark_conversation
    channel = str(body.get("channel") or "").strip().lower()
    chat_id = str(body.get("id") or "")
    seen = bool(body.get("seen") or False)
    done = body.get("done")
    done = None if done is None else bool(done)
    if not seen and done is None:
        raise HTTPException(status_code=400, detail="nothing to mark")
    try:
        out = await asyncio.to_thread(mark_conversation, user["username"], user["user_scope_id"],
                                      channel, chat_id, seen=seen, done=done)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if seen and channel == "mail":
        # The store's mark writers announce themselves; a mail thread's seen is an IMAP flag.
        from vaf.core.web_interface import notify_inbox_changed
        notify_inbox_changed(user["user_scope_id"])
    return {"ok": True, **out}
