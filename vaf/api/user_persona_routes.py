# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
API Routes for User Persona and Workspace Management.

Endpoints:
- GET  /api/user/persona        - Get identity and soul
- PUT  /api/user/identity       - Update identity.json
- PUT  /api/user/soul           - Update soul.md
- PUT  /api/user/user-identity  - Update user_identity.json (full or partial)
- DELETE /api/user/user-identity/entry - Delete a specific entry from user_identity
"""

import logging
from datetime import datetime
from typing import Optional, Dict, Any, List, Literal
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from vaf.auth.user_workspace import get_user_workspace
from vaf.auth.database import get_auth_db
from vaf.core.config import Config, get_local_admin_username

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/user", tags=["persona"])

class IdentityUpdate(BaseModel):
    name: Optional[str] = None
    emoji: Optional[str] = None
    theme: Optional[str] = None
    preferred_language: Optional[str] = None
    preferences: Optional[List[str]] = None
    dos: Optional[List[str]] = None
    donts: Optional[List[str]] = None

class ContentUpdate(BaseModel):
    content: str

class UserIdentityUpdate(BaseModel):
    """Update user_identity.json fields."""
    name: Optional[str] = None
    preferred_language: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    preferences: Optional[List[str]] = None
    dos: Optional[List[str]] = None
    donts: Optional[List[str]] = None
    main_messenger: Optional[str] = None  # "telegram" | "discord" | "slack"
    timezone: Optional[str] = None  # IANA e.g. Europe/Berlin
    date_format: Optional[str] = None  # e.g. dd.mm.yyyy
    time_format: Optional[str] = None  # "24h" | "12h"
    last_seen_announcement_version: Optional[str] = None  # major.minor the user last acknowledged (announcement modal)

class UserIdentityEntryUpdate(BaseModel):
    """Update or delete a specific entry in a list field."""
    field: Literal["preferences", "dos", "donts"]
    index: int
    value: Optional[str] = None  # None = delete, str = update

class UserIdentityEntryDelete(BaseModel):
    """Delete a specific entry from a list field."""
    field: Literal["preferences", "dos", "donts"]
    index: int

def get_current_username(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user:
        # Local: same username as WebSocket so user_identity.json and Settings UI match
        return get_local_admin_username()
    return user.get("username", "admin")

@router.get("/persona")
async def get_persona(username: str = Depends(get_current_username)):
    try:
        ws = get_user_workspace(username)
        return {
            "identity": ws.get_identity(),
            "user_identity": ws.get_user_identity(),
            "soul": ws.get_soul(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/identity")
async def update_identity(data: IdentityUpdate, username: str = Depends(get_current_username)):
    ws = get_user_workspace(username)
    current = ws.get_identity()
    updated = {**current, **data.dict(exclude_none=True)}
    ws.save_identity(updated)
    return updated

@router.put("/soul")
async def update_soul(data: ContentUpdate, username: str = Depends(get_current_username)):
    ws = get_user_workspace(username)
    ws.save_soul(data.content)
    return {"status": "success"}

@router.put("/user-identity")
async def update_user_identity(data: UserIdentityUpdate, username: str = Depends(get_current_username)):
    """Update user_identity.json with new values. Adds entry to change_log."""
    ws = get_user_workspace(username)
    current = ws.get_user_identity()

    # Track what changed for the changelog
    changes = []

    # Update fields that are provided
    update_dict = data.dict(exclude_none=True)
    full_dict = data.dict()
    valid_main_messengers = ("telegram", "discord", "slack", "whatsapp")
    if "main_messenger" in full_dict:
        value = full_dict["main_messenger"]
        normalized = (value or "").strip().lower() or None
        normalized = normalized if normalized in valid_main_messengers else None
        if current.get("main_messenger") != normalized:
            changes.append("main_messenger")
            current["main_messenger"] = normalized
    for loc_key in ("city", "country"):
        if loc_key in full_dict:
            val = (full_dict[loc_key] or "").strip() or None
            if current.get(loc_key) != val:
                changes.append(loc_key)
                current[loc_key] = val
    for dt_key in ("timezone", "date_format", "time_format"):
        if dt_key in full_dict:
            val = (full_dict[dt_key] or "").strip() or None
            if dt_key == "time_format" and val and val.lower() not in ("24h", "12h"):
                val = None
            if current.get(dt_key) != val:
                changes.append(dt_key)
                current[dt_key] = val
    # Per-user proactive quiet hours (None = inherit the global thinking config).
    if "quiet_hours_enabled" in full_dict:
        qv = full_dict["quiet_hours_enabled"]
        qv = qv if isinstance(qv, bool) else None
        if current.get("quiet_hours_enabled") != qv:
            changes.append("quiet_hours_enabled")
            current["quiet_hours_enabled"] = qv
    import re as _re_qh
    for qk in ("quiet_hours_start", "quiet_hours_end"):
        if qk in full_dict:
            val = (full_dict[qk] or "").strip() or None
            if val and not _re_qh.match(r"^([01]\d|2[0-3]):[0-5]\d$", val):
                val = None
            if current.get(qk) != val:
                changes.append(qk)
                current[qk] = val
    # System field: the announcement version the user acknowledged. Persist it SILENTLY — it is
    # bookkeeping, not a user-facing profile edit, so it must never land in change_log.
    if "last_seen_announcement_version" in full_dict and full_dict["last_seen_announcement_version"] is not None:
        current["last_seen_announcement_version"] = str(full_dict["last_seen_announcement_version"]).strip() or None
    for key, value in update_dict.items():
        if key in ("main_messenger", "city", "country", "timezone", "date_format", "time_format",
                   "quiet_hours_enabled", "quiet_hours_start", "quiet_hours_end",
                   "last_seen_announcement_version"):
            continue
        if current.get(key) != value:
            changes.append(key)
            current[key] = value

    # Add to change_log if something changed
    if changes:
        if "change_log" not in current or not isinstance(current["change_log"], list):
            current["change_log"] = []
        current["change_log"].append({
            "at": datetime.now().astimezone().isoformat(),
            "action": f"Manual edit: updated {', '.join(changes)}",
            "source": "settings_ui"
        })

    ws.save_user_identity(current)
    return {"status": "success", "user_identity": current}

@router.post("/user-identity/entry")
async def update_user_identity_entry(data: UserIdentityEntryUpdate, username: str = Depends(get_current_username)):
    """Update or delete a specific entry in a list field (preferences, dos, donts)."""
    ws = get_user_workspace(username)
    current = ws.get_user_identity()

    field = data.field
    index = data.index
    value = data.value

    # Ensure the field exists and is a list
    if field not in current or not isinstance(current[field], list):
        current[field] = []

    field_list = current[field]

    # Check index bounds
    if index < 0 or index >= len(field_list):
        raise HTTPException(status_code=400, detail=f"Index {index} out of bounds for {field}")

    # Get old value for changelog
    old_value = field_list[index]

    # Add to change_log
    if "change_log" not in current or not isinstance(current["change_log"], list):
        current["change_log"] = []

    if value is None:
        # Delete
        del field_list[index]
        current["change_log"].append({
            "at": datetime.now().astimezone().isoformat(),
            "action": f"Deleted from {field}: \"{old_value}\"",
            "source": "settings_ui"
        })
    else:
        # Update
        field_list[index] = value
        current["change_log"].append({
            "at": datetime.now().astimezone().isoformat(),
            "action": f"Edited {field}: \"{old_value}\" → \"{value}\"",
            "source": "settings_ui"
        })

    ws.save_user_identity(current)
    return {"status": "success", "user_identity": current}

@router.delete("/user-identity/entry")
async def delete_user_identity_entry(data: UserIdentityEntryDelete, username: str = Depends(get_current_username)):
    """Delete a specific entry from a list field (preferences, dos, donts)."""
    ws = get_user_workspace(username)
    current = ws.get_user_identity()

    field = data.field
    index = data.index

    # Ensure the field exists and is a list
    if field not in current or not isinstance(current[field], list):
        raise HTTPException(status_code=400, detail=f"Field {field} not found or not a list")

    field_list = current[field]

    # Check index bounds
    if index < 0 or index >= len(field_list):
        raise HTTPException(status_code=400, detail=f"Index {index} out of bounds for {field}")

    # Get old value for changelog
    old_value = field_list[index]

    # Delete entry
    del field_list[index]

    # Add to change_log
    if "change_log" not in current or not isinstance(current["change_log"], list):
        current["change_log"] = []
    current["change_log"].append({
        "at": datetime.now().astimezone().isoformat(),
        "action": f"Deleted from {field}: \"{old_value}\"",
        "source": "settings_ui"
    })

    ws.save_user_identity(current)
    return {"status": "success", "user_identity": current}
