# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Contacts API: CRUD for central contact list with personal file (language, how to address, birthday, notes, whitelist).

User isolation: Every endpoint uses get_current_vaf_user(request); list/get/create/update/delete
operate only on that user's contacts. User 1 cannot see or modify User 2's contacts.
Auth: request.state.user (set by auth middleware in network mode) or local admin fallback.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
from vaf.core.contacts_store import (
    create_contact,
    delete_contact,
    get_contact_by_id,
    list_contacts,
    update_contact,
)

logger = logging.getLogger("vaf.api.contacts")

router = APIRouter(prefix="/api/contacts", tags=["contacts"])


def get_current_vaf_user(request: Request) -> Dict[str, str]:
    """Return user_scope_id and username for the current request. Used for strict per-user contact isolation."""
    user = getattr(request.state, "user", None)
    if user and user.get("user_scope_id") and user.get("username"):
        return {
            "user_scope_id": str(user["user_scope_id"]),
            "username": user.get("username", "admin"),
        }
    return {
        "user_scope_id": get_local_admin_scope_id(),
        "username": get_local_admin_username(),
    }


class ContactCreate(BaseModel):
    name: str
    channels: Optional[List[Dict[str, str]]] = None  # [{ type, value }, ...]; overrides legacy fields if set
    whatsapp_phone: Optional[str] = None
    telegram_username: Optional[str] = None
    telegram_user_id: Optional[str] = None
    email: Optional[str] = None
    preferred_language: Optional[str] = None
    how_to_address: Optional[str] = None
    birthday: Optional[str] = None
    notes: Optional[str] = None
    allow_as_assistant_user: bool = False


class ContactUpdate(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None
    channels: Optional[List[Dict[str, str]]] = None
    whatsapp_phone: Optional[str] = None
    telegram_username: Optional[str] = None
    telegram_user_id: Optional[str] = None
    email: Optional[str] = None
    preferred_language: Optional[str] = None
    how_to_address: Optional[str] = None
    birthday: Optional[str] = None
    notes: Optional[str] = None
    allow_as_assistant_user: Optional[bool] = None


@router.get("")
async def get_contacts_list(request: Request) -> List[Dict[str, Any]]:
    """List all contacts for the current user."""
    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    user_scope_id = user_info.get("user_scope_id")
    return list_contacts(username, user_scope_id=user_scope_id)


@router.get("/{contact_id}")
async def get_contact(contact_id: str, request: Request) -> Dict[str, Any]:
    """Get one contact by id."""
    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    user_scope_id = user_info.get("user_scope_id")
    contact = get_contact_by_id(contact_id, username, user_scope_id=user_scope_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    return contact


@router.post("")
async def post_contact(request: Request, body: ContactCreate) -> Dict[str, Any]:
    """Create a contact."""
    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    if not (body.name or "").strip():
        raise HTTPException(status_code=400, detail="name is required")
    user_scope_id = user_info.get("user_scope_id")
    contact = create_contact(
        (body.name or "").strip(),
        username,
        user_scope_id=user_scope_id,
        channels=body.channels,
        whatsapp_phone=body.whatsapp_phone,
        telegram_username=body.telegram_username,
        telegram_user_id=body.telegram_user_id,
        email=body.email,
        preferred_language=body.preferred_language,
        how_to_address=body.how_to_address,
        birthday=body.birthday,
        notes=body.notes,
        allow_as_assistant_user=body.allow_as_assistant_user,
    )
    return contact


@router.patch("/{contact_id}")
async def patch_contact(contact_id: str, request: Request, body: ContactUpdate) -> Dict[str, Any]:
    """Update a contact (partial)."""
    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    user_scope_id = user_info.get("user_scope_id")
    if not updates:
        contact = get_contact_by_id(contact_id, username, user_scope_id=user_scope_id)
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")
        return contact
    contact = update_contact(contact_id, username, user_scope_id=user_scope_id, **updates)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    return contact


class NoteCreate(BaseModel):
    text: str


class EventCreate(BaseModel):
    title: str
    when: str          # ISO 8601 or "YYYY-MM-DD HH:MM" in the user's timezone
    note: Optional[str] = None


def _parse_when(when: str, username: str) -> float:
    """User-entered date/time -> unix time, in the user's configured timezone (vaf.core.user_time)."""
    from datetime import datetime
    from vaf.core.user_time import resolve_user_timezone
    raw = (when or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="when is required")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")
        except ValueError:
            raise HTTPException(status_code=400, detail="when must be ISO 8601 or YYYY-MM-DD HH:MM")
    if dt.tzinfo is None:
        tz = resolve_user_timezone(username)
        if tz is not None:
            dt = dt.replace(tzinfo=tz)
    return dt.timestamp()


@router.get("/statuses/values")
async def get_status_values(request: Request) -> Dict[str, Any]:
    """The suggestions for the status field: the defaults plus every status in use."""
    from vaf.core.contacts_store import contact_status_values
    user_info = get_current_vaf_user(request)
    return {"values": contact_status_values(user_info["username"], user_scope_id=user_info.get("user_scope_id"))}


@router.post("/{contact_id}/notes")
async def post_contact_note(contact_id: str, request: Request, body: NoteCreate) -> Dict[str, Any]:
    from vaf.core.contacts_store import add_contact_note
    user_info = get_current_vaf_user(request)
    note = add_contact_note(contact_id, body.text, user_info["username"], user_scope_id=user_info.get("user_scope_id"), source="user")
    if not note:
        raise HTTPException(status_code=404, detail="Contact not found or empty note")
    return note


@router.delete("/{contact_id}/notes/{note_id}")
async def remove_contact_note(contact_id: str, note_id: str, request: Request) -> Dict[str, str]:
    from vaf.core.contacts_store import delete_contact_note
    user_info = get_current_vaf_user(request)
    if delete_contact_note(contact_id, note_id, user_info["username"], user_scope_id=user_info.get("user_scope_id")):
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Note not found")


@router.post("/{contact_id}/events")
async def post_contact_event(contact_id: str, request: Request, body: EventCreate) -> Dict[str, Any]:
    from vaf.core.contacts_store import add_contact_event
    user_info = get_current_vaf_user(request)
    when_ts = _parse_when(body.when, user_info["username"])
    event = add_contact_event(contact_id, body.title, when_ts, user_info["username"], user_scope_id=user_info.get("user_scope_id"),
                              source="user", note=body.note)
    if not event:
        raise HTTPException(status_code=404, detail="Contact not found or empty title")
    return event


@router.delete("/{contact_id}/events/{event_id}")
async def remove_contact_event(contact_id: str, event_id: str, request: Request) -> Dict[str, str]:
    from vaf.core.contacts_store import delete_contact_event
    user_info = get_current_vaf_user(request)
    if delete_contact_event(contact_id, event_id, user_info["username"], user_scope_id=user_info.get("user_scope_id")):
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Event not found")


@router.get("/{contact_id}/overview")
async def get_contact_overview(contact_id: str, request: Request) -> Dict[str, Any]:
    """Status, last contact, upcoming stored events, recent notes, plus the calendar events
    that mention this contact (live, best-effort)."""
    import asyncio
    from vaf.core.contacts_store import contact_calendar_events, contact_summary
    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    user_scope_id = user_info.get("user_scope_id")
    contact = get_contact_by_id(contact_id, username, user_scope_id=user_scope_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    summary = contact_summary(contact)
    try:
        summary["calendar_events"] = await asyncio.wait_for(
            asyncio.to_thread(contact_calendar_events, contact, username, user_scope_id, 30), timeout=6.0)
    except Exception:
        summary["calendar_events"] = []
    return summary


@router.delete("/{contact_id}")
async def remove_contact(contact_id: str, request: Request) -> Dict[str, str]:
    """Delete a contact."""
    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    user_scope_id = user_info.get("user_scope_id")
    if delete_contact(contact_id, username, user_scope_id=user_scope_id):
        return {"status": "deleted", "message": "Contact deleted."}
    raise HTTPException(status_code=404, detail="Contact not found")
