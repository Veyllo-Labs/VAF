"""
RAG (Retrieval-Augmented Generation) pipeline for VAF Memory System.

Provides:
- Memory ingestion (encrypt, chunk, embed, store)
- Semantic search with pgvector
- Context building for LLM queries
- Streaming responses via VAF providers
"""

import asyncio
from typing import List, Optional, Dict, Any, AsyncGenerator, Tuple
from uuid import UUID, uuid4
from datetime import datetime
from dataclasses import dataclass
from sqlalchemy import select, and_, func, text, delete
from sqlalchemy.ext.asyncio import AsyncSession
from vaf.memory.models import Memory, Chunk, Connection, EMBEDDING_DIM
from vaf.memory.crypto import get_crypto, MemoryCrypto
from vaf.memory.embeddings import get_embedding_service, get_chunker, EmbeddingService, TextChunker
from vaf.memory.graph import GraphManager
from vaf.memory.database import get_db
from vaf.core.config import Config
import logging
import re

logger = logging.getLogger(__name__)


@dataclass
class RagSource:
    """A source memory/chunk used in RAG retrieval."""
    memory_id: str
    chunk_id: str
    text: str
    score: float  # Similarity score (0-1)
    metadata: Dict[str, Any]


@dataclass
class RagResult:
    """Result of a RAG query."""
    answer: str
    sources: List[RagSource]
    context_tokens: int
    query_embedding: List[float]


class RagPipeline:
    """
    Main RAG pipeline for memory retrieval and generation.
    
    Workflow:
    1. Ingest: encrypt → chunk → embed → store → auto-connect
    2. Query: embed → search → decrypt → context → LLM → stream
    """
    
    def __init__(
        self,
        db: AsyncSession,
        crypto: Optional[MemoryCrypto] = None,
        embedding_service: Optional[EmbeddingService] = None,
        chunker: Optional[TextChunker] = None
    ):
        """
        Initialize RAG pipeline.
        
        Args:
            db: Async database session
            crypto: Encryption handler (default from singleton)
            embedding_service: Embedding service (default from singleton)
            chunker: Text chunker (default from config)
        """
        self.db = db
        self.crypto = crypto or get_crypto()
        self.embeddings = embedding_service or get_embedding_service()
        self.chunker = chunker or get_chunker()
        self.graph = GraphManager(db)
    
    async def ingest(
        self,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        parent_id: Optional[UUID] = None,
        auto_connect: bool = True,
        user_scope_id: Optional[UUID] = None
    ) -> Memory:
        """
        Ingest content into the memory system.
        
        Steps:
        1. Encrypt full content
        2. Chunk text for RAG retrieval
        3. Embed chunks
        4. Store memory and chunks
        5. Auto-connect to similar memories
        
        Args:
            content: Text content to store
            metadata: Optional metadata (title, tags, type, etc.)
            parent_id: Optional parent memory for tree hierarchy
            auto_connect: Whether to auto-connect to similar memories
            user_scope_id: Optional user scope for multi-tenancy
            
        Returns:
            Created Memory object
        """
        if not content or not content.strip():
            raise ValueError("Cannot ingest empty content")
        
        metadata = metadata or {}
        
        # Set default title if not provided
        if "title" not in metadata:
            # Use first 50 chars as title
            metadata["title"] = content[:50].strip().replace('\n', ' ')
            if len(content) > 50:
                metadata["title"] += "..."
        
        # Store a preview in metadata (unencrypted, for display)
        metadata["preview"] = content[:200].strip().replace('\n', ' ')
        if len(content) > 200:
            metadata["preview"] += "..."
        
        metadata["type"] = metadata.get("type", "note")
        metadata["created_at"] = datetime.utcnow().isoformat()
        
        # 1. Encrypt content
        encrypted_content, nonce = self.crypto.encrypt(content)
        
        # 2. Create memory embedding (from title/summary)
        summary = f"{metadata.get('title', '')} {' '.join(metadata.get('tags', []))}"
        memory_embedding = await self.embeddings.embed(summary, prefix="passage")
        
        # 3. Create memory record
        memory = Memory(
            id=uuid4(),
            encrypted_content=encrypted_content,
            nonce=nonce,
            meta=metadata, # Fixed: was metadata=metadata, but model uses 'meta'
            embedding=memory_embedding,
            parent_id=parent_id,
            user_scope_id=user_scope_id
        )
        self.db.add(memory)
        await self.db.flush()
        
        # 4. Chunk and embed
        chunks_data = self.chunker.chunk(content)
        
        if chunks_data:
            chunk_texts = [c["text"] for c in chunks_data]
            chunk_embeddings = await self.embeddings.embed_batch(chunk_texts, prefix="passage")
            
            for chunk_data, embedding in zip(chunks_data, chunk_embeddings):
                chunk = Chunk(
                    id=uuid4(),
                    memory_id=memory.id,
                    text=chunk_data["text"],
                    embedding=embedding,
                    chunk_index=chunk_data["index"],
                    start_char=chunk_data["start_char"],
                    end_char=chunk_data["end_char"]
                )
                self.db.add(chunk)
        
        await self.db.flush()
        
        # 5. Auto-connect to similar memories (scoped!)
        if auto_connect:
            # TODO: Update graph manager to respect scope
            await self.graph.auto_connect_memory(memory)
        
        logger.info(f"Ingested memory {memory.id} with {len(chunks_data)} chunks (Scope: {user_scope_id})")
        
        return memory
    
    async def delete_memories_by_source_scope(
        self,
        source: str,
        user_scope_id: Optional[UUID] = None
    ) -> int:
        """
        Delete memories that match the given source (in meta) and user_scope_id.
        Used by Sync to replace MEMORY.md content for a user (delete old, then ingest).
        """
        filters = [Memory.meta["source"].astext == source]
        if user_scope_id is not None:
            filters.append(Memory.user_scope_id == user_scope_id)
        else:
            filters.append(Memory.user_scope_id.is_(None))
        stmt = delete(Memory).where(and_(*filters))
        result = await self.db.execute(stmt)
        await self.db.flush()
        return result.rowcount if hasattr(result, "rowcount") else 0
    
    async def search(
        self,
        query: str,
        k: int = 5,
        threshold: float = 0.5,
        metadata_filter: Optional[Dict[str, Any]] = None,
        user_scope_id: Optional[UUID] = None
    ) -> List[RagSource]:
        """
        Search for relevant memories using vector similarity.
        
        Args:
            query: Search query
            k: Number of results to return
            threshold: Minimum similarity threshold (0-1)
            metadata_filter: Optional metadata filter
            user_scope_id: Optional user scope filter
            
        Returns:
            List of RagSource objects
        """
        # Embed query (E5: use "query" prefix for best retrieval)
        query_embedding = await self.embeddings.embed(query, prefix="query")
        
        # Convert threshold to distance (cosine distance = 1 - similarity)
        max_distance = 1.0 - threshold
        
        # Build query
        filters = [
            Memory.is_deleted == False,
            Chunk.embedding.cosine_distance(query_embedding) < max_distance
        ]
        
        # Apply scope filter
        if user_scope_id:
            filters.append(Memory.user_scope_id == user_scope_id)
        else:
            # If no scope provided, allow global (null) or enforce?
            # For now, if user_scope_id is None, we search EVERYTHING (Admin mode or Global)
            # OR we search only Null scope.
            # Decision: If user_scope_id is None, search memories where user_scope_id IS NULL.
            filters.append(Memory.user_scope_id.is_(None))

        stmt = select(
            Chunk,
            Chunk.embedding.cosine_distance(query_embedding).label("distance"),
            Memory
        ).join(Memory, Chunk.memory_id == Memory.id).where(
            and_(*filters)
        ).order_by("distance").limit(k)
        
        result = await self.db.execute(stmt)
        rows = result.all()
        
        sources = []
        seen_memories = set()
        
        for chunk, distance, memory in rows:
            # Apply metadata filter
            if metadata_filter:
                skip = False
                for key, value in metadata_filter.items():
                    mem_value = memory.meta.get(key) if memory.meta else None
                    if isinstance(value, list):
                        if not mem_value or not any(v in mem_value for v in value):
                            skip = True
                            break
                    elif mem_value != value:
                        skip = True
                        break
                if skip:
                    continue
            
            score = 1.0 - distance
            
            source = RagSource(
                memory_id=str(memory.id),
                chunk_id=str(chunk.id),
                text=chunk.text,
                score=score,
                metadata=memory.meta or {}
            )
            sources.append(source)
            seen_memories.add(str(memory.id))
        
        logger.info(f"Search found {len(sources)} relevant chunks from {len(seen_memories)} memories")
        
        return sources
    
    async def query(
        self,
        query: str,
        k: int = 5,
        system_prompt: Optional[str] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
        user_scope_id: Optional[UUID] = None
    ) -> RagResult:
        """Perform RAG query with scoping."""
        # Search for relevant chunks
        sources = await self.search(query, k=k, metadata_filter=metadata_filter, user_scope_id=user_scope_id)
        
        if not sources:
            return RagResult(
                answer="I couldn't find any relevant memories to answer your question.",
                sources=[],
                context_tokens=0,
                query_embedding=await self.embeddings.embed(query, prefix="query"),
            )
        
        # Build context
        context_parts = []
        for i, source in enumerate(sources):
            context_parts.append(f"[Source {i+1}] (Relevance: {source.score:.0%})\n{source.text}")
        
        context = "\n\n---\n\n".join(context_parts)
        context_tokens = self.chunker.estimate_tokens(context)
        
        default_system = """You are a helpful assistant with access to a memory database. 
Answer the user's question based on the provided context from relevant memories.
If the context doesn't contain enough information to answer, say so.
Always cite which source(s) you used in your answer."""
        
        full_prompt = f"""{system_prompt or default_system}

## Relevant Context from Memory:

{context}

## User Question:
{query}

## Instructions:
- Answer based on the provided context
- Cite sources using [Source N] notation
- If uncertain, acknowledge it"""
        
        answer = await self._generate_answer(full_prompt)
        
        return RagResult(
            answer=answer,
            sources=sources,
            context_tokens=context_tokens,
            query_embedding=await self.embeddings.embed(query, prefix="query"),
        )
    
    async def query_stream(
        self,
        query: str,
        k: int = 5,
        system_prompt: Optional[str] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
        user_scope_id: Optional[UUID] = None
    ) -> AsyncGenerator[Tuple[str, Optional[List[RagSource]]], None]:
        """Perform RAG query with streaming response and scoping."""
        sources = await self.search(query, k=k, metadata_filter=metadata_filter, user_scope_id=user_scope_id)
        
        if not sources:
            yield ("I couldn't find any relevant memories to answer your question.", sources)
            return
        
        context_parts = []
        for i, source in enumerate(sources):
            context_parts.append(f"[Source {i+1}] (Relevance: {source.score:.0%})\n{source.text}")
        
        context = "\n\n---\n\n".join(context_parts)
        
        default_system = """You are a helpful assistant with access to a memory database. 
Answer the user's question based on the provided context from relevant memories.
Always cite which source(s) you used."""
        
        full_prompt = f"""{system_prompt or default_system}

## Context:
{context}

## Question:
{query}"""
        
        first_chunk = True
        async for token in self._stream_answer(full_prompt):
            if first_chunk:
                yield (token, sources)
                first_chunk = False
            else:
                yield (token, None)
    
    async def _generate_answer(self, prompt: str) -> str:
        """Generate answer using VAF's API backend."""
        try:
            from vaf.core.api_backend import APIBackendManager
            
            backend = APIBackendManager()
            provider = Config.get("provider", "local")
            
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: backend.chat_completion(
                    provider=provider,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=1024
                )
            )
            
            return response.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.error(f"Error generating answer: {e}")
            return f"Error generating answer: {str(e)}"
    
    async def _stream_answer(self, prompt: str) -> AsyncGenerator[str, None]:
        """Stream answer tokens using VAF's API backend."""
        try:
            from vaf.core.api_backend import APIBackendManager
            
            backend = APIBackendManager()
            provider = Config.get("provider", "local")
            
            # Use streaming API
            for chunk in backend.chat_completion_stream(
                provider=provider,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024
            ):
                if chunk:
                    content = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if content:
                        yield content
        except Exception as e:
            logger.error(f"Error streaming answer: {e}")
            yield f"Error: {str(e)}"
    
    async def get_memory(self, memory_id: UUID, decrypt: bool = True) -> Optional[Dict[str, Any]]:
        """
        Get a memory by ID.
        
        Args:
            memory_id: Memory UUID
            decrypt: Whether to decrypt content
            
        Returns:
            Memory dict with optional decrypted content
        """
        result = await self.db.execute(
            select(Memory).where(Memory.id == memory_id)
        )
        memory = result.scalar_one_or_none()
        
        if not memory:
            return None
        
        data = memory.to_dict()
        
        if decrypt:
            try:
                data["content"] = self.crypto.decrypt(memory.encrypted_content, memory.nonce)
            except Exception as e:
                logger.error(f"Failed to decrypt memory {memory_id}: {e}")
                data["content"] = "[Decryption failed]"
        
        return data
    
    async def update_memory(
        self,
        memory_id: UUID,
        content: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Memory:
        """
        Update a memory's content and/or metadata.
        
        Args:
            memory_id: Memory UUID
            content: New content (re-encrypted, re-chunked)
            metadata: New metadata (merged with existing)
            
        Returns:
            Updated Memory object
        """
        result = await self.db.execute(
            select(Memory).where(Memory.id == memory_id)
        )
        memory = result.scalar_one_or_none()
        
        if not memory:
            raise ValueError(f"Memory {memory_id} not found")
        
        if metadata:
            memory.meta = {**(memory.meta or {}), **metadata}
        
        if content:
            # Re-encrypt content
            encrypted_content, nonce = self.crypto.encrypt(content)
            memory.encrypted_content = encrypted_content
            memory.nonce = nonce
            
            # Update preview
            if not memory.meta:
                memory.meta = {}
            memory.meta["preview"] = content[:200].strip().replace('\n', ' ')
            if len(content) > 200:
                memory.meta["preview"] += "..."
            
            # Delete old chunks
            await self.db.execute(
                Chunk.__table__.delete().where(Chunk.memory_id == memory_id)
            )
            
            # Re-chunk and embed
            chunks_data = self.chunker.chunk(content)
            if chunks_data:
                chunk_texts = [c["text"] for c in chunks_data]
                chunk_embeddings = await self.embeddings.embed_batch(chunk_texts, prefix="passage")
                for chunk_data, embedding in zip(chunks_data, chunk_embeddings):
                    chunk = Chunk(
                        id=uuid4(),
                        memory_id=memory.id,
                        text=chunk_data["text"],
                        embedding=embedding,
                        chunk_index=chunk_data["index"],
                        start_char=chunk_data["start_char"],
                        end_char=chunk_data["end_char"]
                    )
                    self.db.add(chunk)
            # Update memory embedding
            meta = memory.meta or {}
            summary = f"{meta.get('title', '')} {' '.join(meta.get('tags', []))}"
            memory.embedding = await self.embeddings.embed(summary, prefix="passage")
        
        memory.updated_at = datetime.utcnow()
        await self.db.flush()
        
        logger.info(f"Updated memory {memory_id}")
        
        return memory
    
    async def delete_memory(self, memory_id: UUID, soft: bool = True) -> bool:
        """
        Delete a memory.
        
        Args:
            memory_id: Memory UUID
            soft: If True, soft delete (set is_deleted flag)
            
        Returns:
            True if deleted, False if not found
        """
        result = await self.db.execute(
            select(Memory).where(Memory.id == memory_id)
        )
        memory = result.scalar_one_or_none()
        
        if not memory:
            return False
        
        if soft:
            memory.is_deleted = True
        else:
            await self.db.delete(memory)
        
        logger.info(f"Deleted memory {memory_id} (soft={soft})")
        
        return True
    
    async def list_memories(
        self,
        limit: int = 50,
        offset: int = 0,
        include_deleted: bool = False,
        tag_filter: Optional[List[str]] = None,
        type_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        List memories with pagination and filters.
        
        Args:
            limit: Maximum number of results
            offset: Offset for pagination
            include_deleted: Include soft-deleted memories
            tag_filter: Filter by tags
            type_filter: Filter by type
            
        Returns:
            List of memory dicts (without content)
        """
        query = select(Memory).where(Memory.is_deleted == include_deleted)
        
        if type_filter:
            query = query.where(Memory.meta["type"].astext == type_filter)
        
        # Note: Tag filtering with JSONB requires specific operators
        # For simplicity, we filter in Python after fetching
        
        query = query.order_by(Memory.updated_at.desc()).offset(offset).limit(limit)
        
        result = await self.db.execute(query)
        memories = result.scalars().all()
        
        # Apply tag filter if specified
        if tag_filter:
            memories = [
                m for m in memories
                if any(tag in (m.meta or {}).get("tags", []) for tag in tag_filter)
            ]
        
        return [m.to_dict() for m in memories]


# Auto-capture: trigger patterns (user or assistant says "remember", etc.)
_AUTO_CAPTURE_TRIGGERS = [
    r"\bremember\b", r"\bspeichern\b", r"\bnotieren\b", r"\bmerken\b",
    r"\bprefer\b", r"\bimportant\b", r"\bwichtig\b", r"\bdecision\b",
    r"\bentscheidung\b", r"\bwill use\b", r"\bbudeme\b", r"\balways\b",
    r"\bnever\b", r"\bimmer\b", r"\bnie\b",
]
_AUTO_CAPTURE_RE = re.compile("|".join(f"({p})" for p in _AUTO_CAPTURE_TRIGGERS), re.I)


async def auto_capture_memory(
    user_input: str,
    assistant_response: str,
    user_scope_id: Optional[UUID],
    max_candidates: int = 1,
    dedupe_threshold: float = 0.95,
) -> int:
    """
    Optionally store high-value snippets from the exchange into the Memory-DB.
    Runs trigger filter; optionally dedupes; ingests at most max_candidates.
    Returns number of memories stored (0 or 1).
    """
    if user_scope_id is None:
        return 0
    combined = f"{user_input or ''}\n{assistant_response or ''}"
    if len(combined.strip()) < 10 or len(combined) > 2000:
        return 0
    if not _AUTO_CAPTURE_RE.search(combined):
        return 0
    # Use assistant response as candidate (or first 400 chars of combined)
    candidate = (assistant_response or "").strip()[:500]
    if not candidate or len(candidate) < 15:
        return 0
    # Skip if looks like injected context
    if "<relevant-memories>" in candidate or "[Source " in candidate:
        return 0

    async with get_db() as db:
        pipeline = RagPipeline(db)
        # Dedupe: if very similar chunk exists, skip
        try:
            existing = await pipeline.search(
                candidate, k=1, threshold=dedupe_threshold, user_scope_id=user_scope_id
            )
            if existing:
                return 0
        except Exception:
            pass
        await pipeline.ingest(
            content=candidate,
            metadata={"title": "Auto-capture", "source": "auto_capture", "type": "note"},
            user_scope_id=user_scope_id,
            auto_connect=False,
        )
    return 1


def run_auto_capture_sync(
    user_input: str,
    assistant_response: str,
    user_scope_id: Optional[UUID],
) -> None:
    """Run auto_capture_memory from sync code (e.g. headless runner). Swallows errors."""
    if not Config.get("memory_enabled", True) or not Config.get("memory_auto_capture", True):
        return
    if user_scope_id is None:
        return
    try:
        asyncio.run(auto_capture_memory(user_input, assistant_response, user_scope_id))
    except Exception as e:
        logger.warning("Auto-capture failed: %s", e)


def run_memory_search_sync(
    query: str,
    k: int = 5,
    user_scope_id: Optional[UUID] = None
) -> str:
    """
    Run RAG search synchronously for use from sync code (e.g. headless runner).

    Returns a formatted string of top-k snippets for injection into the agent prompt,
    or empty string if memory is disabled, no results, or on error.
    """
    if not Config.get("memory_enabled", True):
        return ""

    async def _search() -> str:
        async with get_db() as db:
            pipeline = RagPipeline(db)
            sources = await pipeline.search(
                query, k=k, user_scope_id=user_scope_id
            )
            if not sources:
                return ""
            parts = []
            for i, s in enumerate(sources):
                parts.append(f"[Source {i+1}] (Relevance: {s.score:.0%})\n{s.text}")
            return "\n\n---\n\n".join(parts)

    try:
        return asyncio.run(_search())
    except Exception as e:
        logger.warning("RAG search failed (chat continues without memory): %s", e)
        return ""


# Helper function for creating pipeline instance
async def get_rag_pipeline() -> RagPipeline:
    """
    Get a RAG pipeline with fresh database session.

    Usage:
        async with get_db() as db:
            pipeline = RagPipeline(db)
            result = await pipeline.query("...")
    """
    # This is a convenience function; actual usage should use get_db() context
    raise NotImplementedError("Use get_db() context manager directly")
