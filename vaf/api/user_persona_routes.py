"""
API Routes for User Persona and Workspace Management.

Endpoints:
- GET  /api/user/persona  - Get identity, soul, and memory
- PUT  /api/user/identity - Update identity.json
- PUT  /api/user/soul     - Update soul.md
- PUT  /api/user/memory   - Update MEMORY.md
- POST /api/user/memory/sync - Trigger RAG re-index of MEMORY.md
"""

import logging
from typing import Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from vaf.auth.user_workspace import get_user_workspace
from vaf.auth.database import get_auth_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/user", tags=["persona"])

class IdentityUpdate(BaseModel):
    name: Optional[str] = None
    emoji: Optional[str] = None
    theme: Optional[str] = None

class ContentUpdate(BaseModel):
    content: str

def get_current_username(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user:
        # Default to admin for localhost bypass (convenience)
        return "admin"
    return user.get("username", "admin")

@router.get("/persona")
async def get_persona(username: str = Depends(get_current_username)):
    try:
        ws = get_user_workspace(username)
        return {
            "identity": ws.get_identity(),
            "soul": ws.get_soul(),
            "memory": ws.get_memory_markdown()
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

@router.put("/memory")
async def update_memory(data: ContentUpdate, username: str = Depends(get_current_username)):
    ws = get_user_workspace(username)
    ws.save_memory_markdown(data.content)
    return {"status": "success"}

@router.post("/memory/sync")
async def sync_memory(request: Request, username: str = Depends(get_current_username)):
    """Trigger a manual sync of MEMORY.md into the RAG index."""
    ws = get_user_workspace(username)
    content = ws.get_memory_markdown()
    
    # Get user scope ID
    user = getattr(request.state, "user", None)
    scope_id = user.get("user_scope_id") if user else None
    
    try:
        from vaf.memory.database import get_db
        from vaf.memory.rag import RagPipeline
        from vaf.memory.models import Memory
        from sqlalchemy import delete
        import uuid
        
        async with get_db() as db:
            pipeline = RagPipeline(db)
            
            # 1. Clear existing RAG entries for this file and user
            # For simplicity, we search for memories with source='MEMORY.md' in metadata
            # This requires metadata to be set during ingest
            
            # 2. Ingest current file content
            await pipeline.ingest(
                content=content,
                metadata={
                    "title": "Long-term Memory",
                    "source": "MEMORY.md",
                    "type": "system_memory"
                },
                user_scope_id=uuid.UUID(scope_id) if scope_id else None
            )
            
        return {"status": "success", "message": "Memory synced to RAG index"}
    except Exception as e:
        logger.error(f"Manual sync failed: {e}")
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")
