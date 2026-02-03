"""
API Routes for User Persona and Workspace Management.

Endpoints:
- GET  /api/user/persona  - Get identity and soul
- PUT  /api/user/identity - Update identity.json
- PUT  /api/user/soul     - Update soul.md
"""

import logging
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from vaf.auth.user_workspace import get_user_workspace
from vaf.auth.database import get_auth_db
from vaf.core.config import Config

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

def get_current_username(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user:
        # Local: same username as WebSocket so user_identity.json and Settings UI match
        return Config.get("local_admin_username", "admin")
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
