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
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
from vaf.core.security_events import log_security_event
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
    # The old bool stays for clients that predate the third state, and it can only mean
    # "allowed": a default False is nobody's decision, and a denial now outranks the channel
    # switch, so inventing one on every create would make the contact unreachable for good.
    allow_as_assistant_user: bool = False
    assistant_access: Optional[Literal["allowed", "denied", "undecided"]] = None
    company: Optional[str] = None
    role: Optional[str] = None
    tags: Optional[List[str]] = None


def _access_from(value: Optional[str]) -> Optional[str]:
    """The API's word for a decision, as the store stores it. "undecided" is a real answer a
    client sends to take a decision back, and it is stored as no decision at all."""
    word = str(value or "").strip().lower()
    return word if word in ("allowed", "denied") else None


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
    # Three answers, so the person can take a decision BACK: None here means "not sent", and
    # a bool has no room left for "undecided".
    allow_as_assistant_user: Optional[bool] = None
    assistant_access: Optional[Literal["allowed", "denied", "undecided"]] = None
    company: Optional[str] = None
    role: Optional[str] = None
    tags: Optional[List[str]] = None


class BulkUpdate(BaseModel):
    ids: List[str]
    status: Optional[str] = None          # left out: unchanged; null or "": cleared
    add_tags: Optional[List[str]] = None
    remove_tags: Optional[List[str]] = None


class BulkIds(BaseModel):
    ids: List[str]


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
        assistant_access=_access_from(body.assistant_access),
        company=body.company,
        role=body.role,
        tags=body.tags,
    )
    # A decision about a person is the one thing on a contact that opens or closes a door.
    # Recorded in the security log like a channel pairing, with the word that was actually
    # taken: a denial reads as a denial, not as "revoked", and no decision writes nothing.
    from vaf.core.contacts_store import contact_access
    _state = contact_access(contact) if contact else None
    if _state:
        log_security_event("contact_access_changed", username=username, path=str(contact.get("id") or ""),
                           detail=f"{'granted' if _state == 'allowed' else 'blocked'}: "
                                  f"{contact.get('name') or contact.get('id')}")
    return contact


@router.post("/bulk")
async def bulk_update_contacts(request: Request, body: BulkUpdate) -> Dict[str, int]:
    """Set a status and/or add and remove tags on several contacts at once. Ids outside the
    caller's book are ignored. Declared as POST so it never collides with PATCH /{contact_id}."""
    from vaf.core.contacts_store import _UNSET, update_contacts_bulk
    user_info = get_current_vaf_user(request)
    status = body.status if "status" in body.model_fields_set else _UNSET
    n = update_contacts_bulk(body.ids, user_info["username"], user_info.get("user_scope_id"),
                             status=status, add_tags=body.add_tags, remove_tags=body.remove_tags)
    return {"updated": n}


@router.post("/bulk/delete")
async def bulk_delete_contacts(request: Request, body: BulkIds) -> Dict[str, int]:
    """Delete several contacts at once; ids outside the caller's book are ignored."""
    from vaf.core.contacts_store import delete_contacts
    user_info = get_current_vaf_user(request)
    return {"deleted": delete_contacts(body.ids, user_info["username"], user_info.get("user_scope_id"))}


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
    # An explicit `allow_as_assistant_user: null` is "not sent", which is what the field's own
    # type says: a bool has no room for "undecided", so None can only mean "no answer here".
    # Left in, it reached the store's legacy-bool branch, where `bool(None)` cleared a decision
    # the client never touched - a blocked contact silently un-blocked by a field that was not
    # even filled in.
    if updates.get("allow_as_assistant_user") is None:
        updates.pop("allow_as_assistant_user", None)
    # The same rule for the three-state field itself: `assistant_access: null` is a field
    # nobody filled in, and "undecided" is the one word that takes a decision back. Passed on,
    # the null reached `_access_from`, came out as "no decision" and cleared a block the
    # client never mentioned.
    if updates.get("assistant_access") is None:
        updates.pop("assistant_access", None)
    _touches_access = "allow_as_assistant_user" in updates or "assistant_access" in updates
    if "assistant_access" in updates:
        updates["assistant_access"] = _access_from(updates.get("assistant_access"))
    before = get_contact_by_id(contact_id, username, user_scope_id=user_scope_id) \
        if _touches_access else None
    contact = update_contact(contact_id, username, user_scope_id=user_scope_id, **updates)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    # The three states are compared as they are, never through bool(): "allowed" and "denied"
    # are both truthy, so a revoke would have compared equal and gone unrecorded - the single
    # most security-relevant change on a contact, invisible in its own log.
    from vaf.core.contacts_store import contact_access
    if before is not None:
        _was, _now = contact_access(before), contact_access(contact)
        if _was != _now:
            _word = {"allowed": "granted", "denied": "blocked"}.get(_now or "", "cleared")
            log_security_event("contact_access_changed", username=username, path=str(contact_id),
                               detail=f"{_word}: {contact.get('name') or contact_id}")
    return contact


class NoteCreate(BaseModel):
    text: str


class EventCreate(BaseModel):
    title: str
    when: str          # ISO 8601 or "YYYY-MM-DD HH:MM" in the user's timezone
    note: Optional[str] = None
    reminder_minutes: Optional[int] = None    # None: the user's calendar default; 0: none
    mirror: bool = True                        # False keeps the event out of the connected calendar


def _parse_when(when: str, username: str) -> float:
    """User-entered date/time -> unix time, in the user's configured timezone (the shared
    grammar in vaf.core.user_time.parse_user_datetime)."""
    from vaf.core.user_time import parse_user_datetime
    if not (when or "").strip():
        raise HTTPException(status_code=400, detail="when is required")
    parsed = parse_user_datetime(when, username)
    if parsed is None:
        raise HTTPException(status_code=400, detail="when must be ISO 8601 or YYYY-MM-DD HH:MM")
    return parsed[0].timestamp()


@router.get("/statuses/values")
async def get_status_values(request: Request) -> Dict[str, Any]:
    """The suggestions for the status field: the defaults plus every status in use."""
    from vaf.core.contacts_store import contact_status_values
    user_info = get_current_vaf_user(request)
    return {"values": contact_status_values(user_info["username"], user_scope_id=user_info.get("user_scope_id"))}


@router.get("/tags/values")
async def get_tag_values(request: Request) -> Dict[str, Any]:
    """Every tag in use, most frequent first (the suggestions behind the tag input)."""
    from vaf.core.contacts_store import contact_tag_values
    user_info = get_current_vaf_user(request)
    return {"values": contact_tag_values(user_info["username"], user_scope_id=user_info.get("user_scope_id"))}


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
                              source="user", note=body.note, reminder_minutes=body.reminder_minutes, mirror=body.mirror)
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
    """Status, last contact, the contact's appointments (the calendar's events linked to it:
    `events` all of them, `upcoming_events` and `next_event` the ones ahead), recent notes,
    plus the calendar events that mention this contact without being linked (best-effort)."""
    import asyncio
    from vaf.core.contacts_store import contact_calendar_events, contact_events, contact_summary
    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    user_scope_id = user_info.get("user_scope_id")
    contact = get_contact_by_id(contact_id, username, user_scope_id=user_scope_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    events = contact_events(contact, username, user_scope_id)
    summary = contact_summary(contact, events=events)
    summary["events"] = events
    try:
        summary["calendar_events"] = await asyncio.wait_for(
            asyncio.to_thread(contact_calendar_events, contact, username, user_scope_id, 30), timeout=6.0)
    except Exception:
        summary["calendar_events"] = []
    # The store keys the window passes to a chat window (no lid jids: the WhatsApp dashboard
    # files a resolved chat under its number), since when the record exists, and the key
    # figures over the stored messages (best-effort like the calendar half).
    from vaf.core.contacts_store import contact_activity_stats, contact_created, contact_endpoints
    summary["endpoints"] = contact_endpoints(contact)
    summary["created"] = contact_created(contact)
    try:
        summary["stats"] = await asyncio.wait_for(
            asyncio.to_thread(contact_activity_stats, contact, username, user_scope_id), timeout=6.0)
    except Exception:
        summary["stats"] = None
    return summary


@router.get("/{contact_id}/timeline")
async def get_contact_timeline(contact_id: str, request: Request, limit: int = 50,
                               cursor: Optional[str] = None, kinds: Optional[str] = None) -> Dict[str, Any]:
    """One newest-first list of everything the user's stores hold about this contact
    (messages, mails, notes, events, the record's creation), paged by an opaque cursor.
    `kinds` narrows the sources: a comma-separated subset of message, mail, note, event,
    created. A lane that takes too long yields an empty page marked timed_out instead of
    blocking the window."""
    import asyncio
    from vaf.core.contacts_store import contact_timeline
    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    user_scope_id = user_info.get("user_scope_id")
    contact = get_contact_by_id(contact_id, username, user_scope_id=user_scope_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    kind_set = {k.strip().lower() for k in (kinds or "").split(",") if k.strip()} or None
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(contact_timeline, contact, username, user_scope_id,
                              limit=min(max(int(limit or 50), 1), 200), cursor=cursor, kinds=kind_set),
            timeout=10.0)
    except asyncio.TimeoutError:
        return {"items": [], "next_cursor": None, "timed_out": True}


@router.delete("/{contact_id}")
async def remove_contact(contact_id: str, request: Request) -> Dict[str, str]:
    """Delete a contact."""
    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    user_scope_id = user_info.get("user_scope_id")
    if delete_contact(contact_id, username, user_scope_id=user_scope_id):
        return {"status": "deleted", "message": "Contact deleted."}
    raise HTTPException(status_code=404, detail="Contact not found")
