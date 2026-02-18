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


@router.delete("/{contact_id}")
async def remove_contact(contact_id: str, request: Request) -> Dict[str, str]:
    """Delete a contact."""
    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    user_scope_id = user_info.get("user_scope_id")
    if delete_contact(contact_id, username, user_scope_id=user_scope_id):
        return {"status": "deleted", "message": "Contact deleted."}
    raise HTTPException(status_code=404, detail="Contact not found")
