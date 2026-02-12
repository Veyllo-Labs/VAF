"""
FastAPI routes for VAF Memory System.

Provides REST API endpoints for:
- Memory CRUD operations
- RAG queries with streaming
- Graph visualization data
- Semantic search
"""

from typing import List, Optional, Dict, Any
from uuid import UUID
from fastapi import APIRouter, HTTPException, Query, Depends, BackgroundTasks, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from vaf.memory.database import get_db, init_db, check_db_connection, get_db_stats
from vaf.memory.rag import RagPipeline, RagSource
from vaf.memory.graph import GraphManager
from vaf.memory.cache import get_cache
from vaf.memory.tag_links import get_linked_tags, add_link, remove_link, list_links
from vaf.memory.tag_link_sync import sync_memories_for_tag_link
from vaf.core.config import Config
import json
import logging

logger = logging.getLogger(__name__)

# Create router
memory_router = APIRouter()


# Helper dependency to get user scope
async def get_current_user_scope(request: Request) -> Optional[UUID]:
    """
    Get the user_scope_id from the request.

    - If authenticated (via AuthMiddleware), returns user.user_scope_id.
    - If local (no auth): returns None to search ALL memories (global search).
    """
    user = getattr(request.state, "user", None)
    if user and user.get("user_scope_id"):
        try:
            return UUID(user["user_scope_id"])
        except ValueError:
            pass
    # Local mode: return None to search all memories (both scoped and unscoped)
    return None


# Pydantic models for request/response
class MemoryCreate(BaseModel):
    """Request model for creating a memory."""
    content: str = Field(..., min_length=1, description="Memory content text")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Optional metadata")
    parent_id: Optional[str] = Field(default=None, description="Parent memory ID for hierarchy")
    auto_connect: bool = Field(default=True, description="Auto-connect to similar memories")


class MemoryUpdate(BaseModel):
    """Request model for updating a memory."""
    content: Optional[str] = Field(default=None, description="New content")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Metadata to merge")


class ConnectionUpdate(BaseModel):
    """Request model for updating connections."""
    related_ids: List[str] = Field(..., description="List of related memory IDs")
    connection_type: str = Field(default="manual", description="Connection type")


class AddTagRequest(BaseModel):
    """Request model for adding a tag to a memory."""
    tag: str = Field(..., min_length=1, description="Tag to add")


class TagLinkRequest(BaseModel):
    """Request model for creating a tag link."""
    tag_a: str = Field(..., min_length=1, description="First tag")
    tag_b: str = Field(..., min_length=1, description="Second tag")


class RagQueryRequest(BaseModel):
    """Request model for RAG queries."""
    query: str = Field(..., min_length=1, description="Query text")
    k: int = Field(default=5, ge=1, le=20, description="Number of sources to retrieve")
    metadata_filter: Optional[Dict[str, Any]] = Field(default=None, description="Metadata filter")
    stream: bool = Field(default=False, description="Enable streaming response")


class SemanticSearchRequest(BaseModel):
    """Request model for semantic search."""
    query: str = Field(..., min_length=1, description="Search query")
    k: int = Field(default=10, ge=1, le=50, description="Number of results")
    threshold: float = Field(default=0.5, ge=0.0, le=1.0, description="Similarity threshold")
    metadata_filter: Optional[Dict[str, Any]] = Field(default=None, description="Metadata filter")


class MemoryResponse(BaseModel):
    """Response model for a single memory."""
    id: str
    user_scope_id: Optional[str] = None
    metadata: Dict[str, Any]
    parent_id: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]
    chunk_count: int
    content: Optional[str] = None


class SourceResponse(BaseModel):
    """Response model for a RAG source."""
    memory_id: str
    chunk_id: str
    text: str
    score: float
    metadata: Dict[str, Any]


class RagQueryResponse(BaseModel):
    """Response model for RAG queries."""
    answer: str
    sources: List[SourceResponse]
    context_tokens: int


class GraphResponse(BaseModel):
    """Response model for graph data."""
    nodes: List[Dict[str, Any]]
    edges: List[Dict[str, Any]]


class StatsResponse(BaseModel):
    """Response model for memory stats."""
    memories: int
    chunks: int
    connections: int
    db_connected: bool


# Health check
@memory_router.get("/health")
async def health_check():
    """Check memory system health."""
    try:
        db_ok = await check_db_connection()
        return {
            "status": "healthy" if db_ok else "degraded",
            "db_connected": db_ok,
            "memory_enabled": Config.get("memory_enabled", True)
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "db_connected": False
        }


# Database initialization (admin)
@memory_router.post("/init")
async def initialize_database(drop_existing: bool = False):
    """
    Initialize the memory database.
    
    WARNING: drop_existing=True will delete all data!
    """
    try:
        await init_db(drop_existing=drop_existing)
        return {"status": "success", "message": "Database initialized"}
    except Exception as e:
        logger.error(f"Database init failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Stats
@memory_router.get("/stats", response_model=StatsResponse)
async def get_stats():
    """Get memory system statistics."""
    try:
        cache = get_cache()
        cached = await cache.get_stats()
        if cached is not None:
            return StatsResponse(**cached)

        stats = await get_db_stats()
        db_ok = await check_db_connection()
        response = StatsResponse(
            memories=stats["memories"],
            chunks=stats["chunks"],
            connections=stats["connections"],
            db_connected=db_ok
        )
        await cache.set_stats(response.model_dump())
        return response
    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        return StatsResponse(
            memories=0,
            chunks=0,
            connections=0,
            db_connected=False
        )


# CRUD operations
@memory_router.post("", response_model=MemoryResponse)
async def create_memory(
    request: MemoryCreate,
    user_scope_id: Optional[UUID] = Depends(get_current_user_scope)
):
    """
    Create a new memory.
    
    - Encrypts content at rest
    - Chunks text for RAG retrieval
    - Generates embeddings
    - Auto-connects to similar memories (optional)
    """
    try:
        async with get_db() as db:
            pipeline = RagPipeline(db)
            
            parent_uuid = UUID(request.parent_id) if request.parent_id else None
            
            memory = await pipeline.ingest(
                content=request.content,
                metadata=request.metadata,
                parent_id=parent_uuid,
                auto_connect=request.auto_connect,
                user_scope_id=user_scope_id
            )
            await get_cache().invalidate_graph()
            chunk_count = await pipeline.get_chunk_count(memory.id)
            return MemoryResponse(
                id=str(memory.id),
                user_scope_id=str(memory.user_scope_id) if memory.user_scope_id else None,
                metadata=memory.meta or {},
                parent_id=str(memory.parent_id) if memory.parent_id else None,
                created_at=memory.created_at.isoformat() if memory.created_at else None,
                updated_at=memory.updated_at.isoformat() if memory.updated_at else None,
                chunk_count=chunk_count
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to create memory: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@memory_router.get("", response_model=List[MemoryResponse])
async def list_memories(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    type_filter: Optional[str] = Query(default=None),
    tag: Optional[str] = Query(default=None),
    include_deleted: bool = Query(default=False),
    user_scope_id: Optional[UUID] = Depends(get_current_user_scope)
):
    """List memories with pagination and filters (scoped to current user)."""
    try:
        async with get_db() as db:
            pipeline = RagPipeline(db)

            tag_filter = [tag] if tag else None

            memories = await pipeline.list_memories(
                limit=limit,
                offset=offset,
                include_deleted=include_deleted,
                tag_filter=tag_filter,
                type_filter=type_filter,
                user_scope_id=user_scope_id
            )

            return [MemoryResponse(**m) for m in memories]
    except Exception as e:
        logger.error(f"Failed to list memories: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Tag links (connect tags: A↔B means memories with A get B and vice versa)
@memory_router.get("/tag-links")
async def get_tag_links():
    """List all tag links."""
    links = list_links()
    return {"links": [{"tag_a": a, "tag_b": b} for a, b in links]}


@memory_router.post("/tag-links")
async def create_tag_link(
    request: TagLinkRequest,
    user_scope_id: Optional[UUID] = Depends(get_current_user_scope),
):
    """Create a tag link and sync existing memories."""
    a, b = request.tag_a.strip().lower(), request.tag_b.strip().lower()
    if not a or not b or a == b:
        raise HTTPException(status_code=400, detail="Invalid tags: must be two different non-empty tags")
    if not add_link(a, b):
        raise HTTPException(status_code=409, detail="Tag link already exists")
    updated = await sync_memories_for_tag_link(a, b, user_scope_id=user_scope_id)
    await get_cache().invalidate_graph()
    return {"status": "created", "tag_a": a, "tag_b": b, "memories_synced": updated}


@memory_router.delete("/tag-links")
async def delete_tag_link(tag_a: str = Query(...), tag_b: str = Query(...)):
    """Remove a tag link."""
    if not remove_link(tag_a, tag_b):
        raise HTTPException(status_code=404, detail="Tag link not found")
    await get_cache().invalidate_graph()
    return {"status": "removed"}


# Must be before /{memory_id} or "graph" is matched as memory_id
@memory_router.get("/graph", response_model=GraphResponse)
async def get_graph(
    limit: int = Query(default=100, ge=1, le=500),
    highlight: Optional[str] = Query(default=None, description="Comma-separated memory IDs to highlight"),
    user_scope_id: Optional[UUID] = Depends(get_current_user_scope)
):
    """Get memory graph data for ReactFlow visualization."""
    try:
        use_cache = not highlight and user_scope_id is None
        highlight_ids = highlight.split(",") if highlight else None
        if use_cache:
            cache = get_cache()
            graph_data = await cache.get_graph(limit)
            if graph_data is not None:
                return GraphResponse(**graph_data)

        async with get_db() as db:
            graph_manager = GraphManager(db)
            graph_data = await graph_manager.get_graph_data(
                limit=limit,
                highlight_ids=highlight_ids,
                user_scope_id=user_scope_id
            )
            if use_cache:
                cache = get_cache()
                await cache.set_graph(graph_data, limit)
            return GraphResponse(**graph_data)
    except Exception as e:
        logger.error(f"Failed to get graph: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@memory_router.get("/{memory_id}", response_model=MemoryResponse)
async def get_memory(memory_id: str, include_content: bool = Query(default=True)):
    """Get a memory by ID."""
    try:
        memory_uuid = UUID(memory_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid memory ID")
    
    try:
        async with get_db() as db:
            pipeline = RagPipeline(db)
            memory = await pipeline.get_memory(memory_uuid, decrypt=include_content)
            
            if not memory:
                raise HTTPException(status_code=404, detail="Memory not found")
            
            return MemoryResponse(**memory)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get memory: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@memory_router.put("/{memory_id}", response_model=MemoryResponse)
async def update_memory(memory_id: str, request: MemoryUpdate):
    """Update a memory's content and/or metadata."""
    try:
        memory_uuid = UUID(memory_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid memory ID")
    
    try:
        async with get_db() as db:
            pipeline = RagPipeline(db)
            memory = await pipeline.update_memory(
                memory_uuid,
                content=request.content,
                metadata=request.metadata
            )
            await get_cache().invalidate_graph()
            chunk_count = await pipeline.get_chunk_count(memory.id)
            return MemoryResponse(
                id=str(memory.id),
                user_scope_id=str(memory.user_scope_id) if memory.user_scope_id else None,
                metadata=memory.meta or {},
                parent_id=str(memory.parent_id) if memory.parent_id else None,
                created_at=memory.created_at.isoformat() if memory.created_at else None,
                updated_at=memory.updated_at.isoformat() if memory.updated_at else None,
                chunk_count=chunk_count
            )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to update memory: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@memory_router.delete("/{memory_id}")
async def delete_memory(memory_id: str, hard: bool = Query(default=False)):
    """
    Delete a memory.
    
    - Default: soft delete (set is_deleted flag)
    - hard=True: permanent deletion
    """
    try:
        memory_uuid = UUID(memory_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid memory ID")
    
    try:
        async with get_db() as db:
            pipeline = RagPipeline(db)
            deleted = await pipeline.delete_memory(memory_uuid, soft=not hard)

            if not deleted:
                raise HTTPException(status_code=404, detail="Memory not found")

            await get_cache().invalidate_graph()
            return {"status": "deleted", "id": memory_id, "hard": hard}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete memory: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@memory_router.put("/{memory_id}/connections")
async def update_connections(memory_id: str, request: ConnectionUpdate):
    """Update manual connections for a memory."""
    try:
        memory_uuid = UUID(memory_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid memory ID")

    try:
        async with get_db() as db:
            graph_manager = GraphManager(db)

            connections = await graph_manager.update_connections(
                memory_uuid,
                request.related_ids,
                request.connection_type
            )
            await get_cache().invalidate_graph()
            return {
                "status": "updated",
                "memory_id": memory_id,
                "connection_count": len(connections)
            }
    except Exception as e:
        logger.error(f"Failed to update connections: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@memory_router.post("/{memory_id}/tags")
async def add_tag_to_memory(memory_id: str, request: AddTagRequest):
    """Add a tag to a memory's metadata."""
    try:
        memory_uuid = UUID(memory_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid memory ID")

    try:
        async with get_db() as db:
            pipeline = RagPipeline(db)

            # Get current memory (decrypt=False since we only need metadata)
            memory = await pipeline.get_memory(memory_uuid, decrypt=False)
            if not memory:
                raise HTTPException(status_code=404, detail="Memory not found")

            # Get current tags and add new one (memory is a dict with "metadata" key)
            current_meta = memory.get("metadata") or {}
            current_tags = list(current_meta.get("tags", []))

            # Normalize all existing tags to lowercase (migration for old data)
            current_tags = [t.strip().lower() for t in current_tags if t and t.strip()]

            # Normalize new tag and expand with linked tags (tag links: A↔B means both get both)
            new_tag = request.tag.strip().lower()
            tags_to_add = [new_tag] + list(get_linked_tags(new_tag))
            for t in tags_to_add:
                if t and t not in current_tags:
                    current_tags.append(t)

            # Always update to ensure normalization is saved
            current_meta["tags"] = current_tags

            # Update memory with new metadata
            await pipeline.update_memory(memory_uuid, metadata=current_meta)
            await get_cache().invalidate_graph()

            return {
                "status": "updated",
                "memory_id": memory_id,
                "tags": current_tags
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to add tag: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@memory_router.delete("/{memory_id}/tags/{tag}")
async def remove_tag_from_memory(memory_id: str, tag: str):
    """Remove a tag from a memory's metadata."""
    try:
        memory_uuid = UUID(memory_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid memory ID")

    try:
        async with get_db() as db:
            pipeline = RagPipeline(db)

            # Get current memory (decrypt=False since we only need metadata)
            memory = await pipeline.get_memory(memory_uuid, decrypt=False)
            if not memory:
                raise HTTPException(status_code=404, detail="Memory not found")

            # Get current tags and remove the specified one (memory is a dict with "metadata" key)
            current_meta = memory.get("metadata") or {}
            current_tags = list(current_meta.get("tags", []))

            # Normalize tag
            tag_to_remove = tag.strip().lower()

            if tag_to_remove in current_tags:
                current_tags.remove(tag_to_remove)
                current_meta["tags"] = current_tags

                # Update memory with new metadata
                await pipeline.update_memory(memory_uuid, metadata=current_meta)
                await get_cache().invalidate_graph()

            return {
                "status": "updated",
                "memory_id": memory_id,
                "tags": current_tags
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to remove tag: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# RAG endpoints
@memory_router.post("/rag/query", response_model=RagQueryResponse)
async def rag_query(
    request: RagQueryRequest,
    user_scope_id: Optional[UUID] = Depends(get_current_user_scope)
):
    """
    Perform a RAG query.
    
    1. Embeds the query
    2. Retrieves relevant memory chunks
    3. Builds context from sources
    4. Generates answer using LLM
    
    For streaming responses, use /rag/query/stream endpoint.
    """
    try:
        cache = get_cache()
        scope_str = str(user_scope_id) if user_scope_id else None
        cached = await cache.get_rag_result(
            request.query,
            k=request.k,
            user_scope_id=scope_str,
            metadata_filter=request.metadata_filter,
        )
        if cached is not None:
            return RagQueryResponse(**cached)

        async with get_db() as db:
            pipeline = RagPipeline(db)

            result = await pipeline.query(
                query=request.query,
                k=request.k,
                metadata_filter=request.metadata_filter,
                user_scope_id=user_scope_id
            )

            sources = [
                SourceResponse(
                    memory_id=s.memory_id,
                    chunk_id=s.chunk_id,
                    text=s.text,
                    score=s.score,
                    metadata=s.metadata
                )
                for s in result.sources
            ]

            response = RagQueryResponse(
                answer=result.answer,
                sources=sources,
                context_tokens=result.context_tokens
            )
            cache_dict = response.model_dump()
            await cache.set_rag_result(
                request.query,
                cache_dict,
                k=request.k,
                user_scope_id=scope_str,
                metadata_filter=request.metadata_filter,
            )
            return response
    except Exception as e:
        logger.error(f"RAG query failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@memory_router.post("/rag/query/stream")
async def rag_query_stream(
    request: RagQueryRequest,
    user_scope_id: Optional[UUID] = Depends(get_current_user_scope)
):
    """
    Perform a RAG query with streaming response.
    
    Returns Server-Sent Events (SSE) stream:
    - First event: sources array
    - Subsequent events: answer tokens
    - Final event: done signal
    """
    async def generate():
        try:
            async with get_db() as db:
                pipeline = RagPipeline(db)
                
                first = True
                async for token, sources in pipeline.query_stream(
                    query=request.query,
                    k=request.k,
                    metadata_filter=request.metadata_filter,
                    user_scope_id=user_scope_id
                ):
                    if first and sources is not None:
                        # Send sources first
                        sources_data = [
                            {
                                "memory_id": s.memory_id,
                                "chunk_id": s.chunk_id,
                                "text": s.text,
                                "score": s.score,
                                "metadata": s.metadata
                            }
                            for s in sources
                        ]
                        yield f"data: {json.dumps({'type': 'sources', 'sources': sources_data})}\n\n"
                        first = False
                    
                    # Send token
                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
                
                # Send done signal
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as e:
            logger.error(f"Streaming RAG query failed: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
    
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )


# Semantic search (no LLM)
@memory_router.post("/search", response_model=List[SourceResponse])
async def semantic_search(
    request: SemanticSearchRequest,
    user_scope_id: Optional[UUID] = Depends(get_current_user_scope)
):
    """
    Semantic search without LLM generation.
    
    Returns relevant memory chunks ranked by similarity.
    """
    try:
        async with get_db() as db:
            pipeline = RagPipeline(db)
            
            sources = await pipeline.search(
                query=request.query,
                k=request.k,
                threshold=request.threshold,
                metadata_filter=request.metadata_filter,
                user_scope_id=user_scope_id
            )
            
            return [
                SourceResponse(
                    memory_id=s.memory_id,
                    chunk_id=s.chunk_id,
                    text=s.text,
                    score=s.score,
                    metadata=s.metadata
                )
                for s in sources
            ]
    except Exception as e:
        logger.error(f"Semantic search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
