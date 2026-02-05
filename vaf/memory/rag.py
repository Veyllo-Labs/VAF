"""
RAG (Retrieval-Augmented Generation) pipeline for VAF Memory System.

Provides:
- Memory ingestion (encrypt, chunk, embed, store)
- Semantic search with pgvector
- Context building for LLM queries
- Streaming responses via VAF providers
"""

import asyncio
import threading
from typing import List, Optional, Dict, Any, AsyncGenerator, Tuple
from uuid import UUID, uuid4
from datetime import datetime
from dataclasses import dataclass
from sqlalchemy import select, and_, func, text, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from vaf.memory.models import Memory, Chunk, Connection, EMBEDDING_DIM
from vaf.memory.crypto import get_crypto, MemoryCrypto
from vaf.memory.embeddings import get_embedding_service, get_chunker, EmbeddingService, TextChunker
from vaf.memory.graph import GraphManager
from vaf.memory.database import get_db
from vaf.core.config import Config
from vaf.core.log_helper import append_domain_log
import logging
import re
from pathlib import Path

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
        # Note: Only E5 models need prefix; MiniLM works without
        model_name = self.embeddings.model_name or ""
        use_prefix = "e5" in model_name.lower()
        summary = f"{metadata.get('title', '')} {' '.join(metadata.get('tags', []))}"
        memory_embedding = await self.embeddings.embed(summary, prefix="passage" if use_prefix else None)
        
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
            chunk_embeddings = await self.embeddings.embed_batch(chunk_texts, prefix="passage" if use_prefix else None)
            
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
        Used to replace memories by source and scope (delete old, then re-ingest from elsewhere if needed).
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
        threshold: float = 0.3,  # Lowered from 0.5 to allow more matches
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
        # Remove <think> blocks from query to avoid recursive RAG loops
        import re
        # 1. Remove complete <think>...</think> blocks
        query = re.sub(r'<think>.*?</think>', '', query, flags=re.DOTALL).strip()
        # 2. Remove unclosed <think>... (streaming/partial thought) - CRITICAL for preventing recursive RAG
        if '<think>' in query:
            query = query.split('<think>')[0].strip()
        
        if not query:
            return []

        # Cap query length to avoid huge encoder allocations (e.g. model passing full thinking)
        from vaf.memory.embeddings import MAX_EMBED_INPUT_CHARS
        if len(query) > MAX_EMBED_INPUT_CHARS:
            query = query[:MAX_EMBED_INPUT_CHARS].rstrip()
        # Embed query - NO prefix for MiniLM (only E5 needs prefix)
        model_name = self.embeddings.model_name or ""
        use_prefix = "e5" in model_name.lower()
        query_embedding = await self.embeddings.embed(query, prefix="query" if use_prefix else None)

        # Debug logging
        logger.info(f"RAG search: query='{query[:50]}...', user_scope_id={user_scope_id}, threshold={threshold}")
        
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

        # Debug: Log to file (consolidated in rag.log)
        q_preview = (query[:100] + "…") if len(query) > 100 else query
        append_domain_log("rag", f"SEARCH query={q_preview!r} user_scope_id={user_scope_id} results={len(sources)} chunks from {len(seen_memories)} memories")
        if sources:
            top = " ".join(f"{s.score:.0%}" for s in sources[:3])
            append_domain_log("rag", f"SEARCH top_scores={top}")

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
            select(Memory)
            .where(Memory.id == memory_id)
            .options(selectinload(Memory.chunks))
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

    async def get_chunk_count(self, memory_id: UUID) -> int:
        """Return chunk count for a memory without touching relationship (async-safe)."""
        result = await self.db.execute(
            select(func.count()).select_from(Chunk).where(Chunk.memory_id == memory_id)
        )
        return result.scalar() or 0

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
        type_filter: Optional[str] = None,
        user_scope_id: Optional[UUID] = None
    ) -> List[Dict[str, Any]]:
        """
        List memories with pagination and filters.

        Args:
            limit: Maximum number of results
            offset: Offset for pagination
            include_deleted: Include soft-deleted memories
            tag_filter: Filter by tags
            type_filter: Filter by type
            user_scope_id: Filter by user scope (only show user's memories)

        Returns:
            List of memory dicts (without content)
        """
        conditions = [Memory.is_deleted == include_deleted]
        if user_scope_id is not None:
            conditions.append(Memory.user_scope_id == user_scope_id)

        query = (
            select(Memory)
            .where(and_(*conditions))
            .options(selectinload(Memory.chunks))
        )
        if type_filter:
            query = query.where(Memory.meta["type"].astext == type_filter)
        # Note: Tag filtering with JSONB requires specific operators
        # For simplicity, we filter in Python after fetching
        query = query.order_by(Memory.updated_at.desc()).offset(offset).limit(limit)
        result = await self.db.execute(query)
        memories = result.unique().scalars().all()

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


def _parse_memory_reply(reply: str) -> List[str]:
    """
    Parse compaction LLM reply for MEMORY: "..." or MEMORY: ... lines.
    Returns list of content strings; NO_REPLY or no MEMORY lines => [].
    """
    if not reply or not reply.strip():
        return []
    reply_upper = reply.strip().upper()
    if "NO_REPLY" in reply_upper and "MEMORY:" not in reply_upper:
        return []
    out = []
    for line in reply.splitlines():
        line = line.strip()
        if not line.upper().startswith("MEMORY:"):
            continue
        rest = line[7:].strip()
        if rest.startswith('"') and rest.endswith('"'):
            rest = rest[1:-1].replace('\\"', '"')
        elif rest.startswith("'") and rest.endswith("'"):
            rest = rest[1:-1].replace("\\'", "'")
        if rest:
            out.append(rest)
    return out


def _load_compaction_state() -> Dict[str, int]:
    """Load last_compaction_at_turn per session_id from compaction_state.json."""
    try:
        from pathlib import Path
        path = Path(Config.APP_DIR) / "compaction_state.json"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                import json
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_compaction_state(state: Dict[str, int]) -> None:
    """Save compaction state to compaction_state.json."""
    try:
        from pathlib import Path
        import json
        path = Path(Config.APP_DIR) / "compaction_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.warning("Failed to save compaction state: %s", e)


def _compaction_log(message: str, session_id: str = "", **kwargs: Any) -> None:
    """Append one line to memory.log with [COMPACTION] prefix."""
    extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
    append_domain_log("memory", f"[COMPACTION] {message} session_id={session_id} {extra}".strip())


def run_session_compaction_sync(
    agent: Any,
    user_scope_id: Optional[UUID],
    session_id: str,
    current_turn_count: int,
) -> None:
    """
    Run session compaction if interval reached: inject prompt, parse MEMORY:/NO_REPLY, ingest to RAG.
    Does not append compaction reply to chat history or UI.
    """
    if not Config.get("memory_enabled", True) or not Config.get("memory_compaction_enabled", True):
        return
    interval = int(Config.get("memory_compaction_interval", 15))
    state = _load_compaction_state()
    last = state.get(session_id, 0)
    if current_turn_count - last < interval:
        _compaction_log("COMPACTION_SKIP", session_id=session_id, turn_count=str(current_turn_count), last=str(last), interval=str(interval), reason="interval_not_reached")
        return
    _compaction_log("COMPACTION_START", session_id=session_id, turn_count=str(current_turn_count), last=str(last), interval=str(interval))
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    prompt = (
        "Session nearing compaction. Store durable memories now. "
        f"Write any lasting notes to memory/{date_str}.md. "
        "Output all MEMORY lines in English. "
        "Reply with NO_REPLY if nothing to store."
    )
    try:
        reply = agent._generate_for_compaction(prompt)
    except Exception as e:
        logger.warning("Compaction LLM call failed: %s", e)
        _compaction_log("COMPACTION_LLM_FAIL", session_id=session_id, error=str(e)[:200])
        return
    memories = _parse_memory_reply(reply)
    if not memories:
        _compaction_log("COMPACTION_NO_REPLY", session_id=session_id)
        state[session_id] = current_turn_count
        _save_compaction_state(state)
        refresh_user_profile_summary(user_scope_id)
        return
    async def _ingest_all() -> None:
        async with get_db() as db:
            pipeline = RagPipeline(db)
            for content in memories:
                if not content or not content.strip():
                    continue
                await pipeline.ingest(
                    content=content.strip(),
                    metadata={
                        "title": f"Compaction {date_str}",
                        "source": f"memory/{date_str}",
                        "type": "memory_flush",
                    },
                    user_scope_id=user_scope_id,
                    auto_connect=False,
                )

    # Run in a daemon thread with timeout - never block the main thread
    def _run_ingest():
        try:
            asyncio.run(_ingest_all())
        except Exception as e:
            logger.warning("Compaction ingest inner error: %s", e)

    try:
        thread = threading.Thread(target=_run_ingest, daemon=True)
        thread.start()
        thread.join(timeout=30)  # Wait max 30s
        if thread.is_alive():
            logger.warning("Compaction ingest timed out (30s) - continuing without waiting")
            _compaction_log("COMPACTION_TIMEOUT", session_id=session_id)
    except Exception as e:
        logger.warning("Compaction ingest failed: %s", e)
        _compaction_log("COMPACTION_INGEST_FAIL", session_id=session_id, error=str(e)[:200])
    _compaction_log("COMPACTION_DONE", session_id=session_id, memories=str(len(memories)), date=date_str)
    state[session_id] = current_turn_count
    _save_compaction_state(state)
    refresh_user_profile_summary(user_scope_id)


def refresh_user_profile_summary(user_scope_id: Optional[UUID]) -> None:
    """
    After compaction: run RAG search for user profile facts and write result to cache.
    Cache is read by build_prompt() for the "User identity (current user)" block.
    """
    if user_scope_id is None:
        return
    if not Config.get("memory_enabled", True):
        return
    try:
        from pathlib import Path
        summary = run_memory_search_sync(
            "user profile facts preferences about this user",
            k=8,
            user_scope_id=user_scope_id,
        )
        cache_dir = Path(Config.APP_DIR) / "user_profile_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{user_scope_id}.txt"
        cache_file.write_text(summary or "", encoding="utf-8")
    except Exception as e:
        logger.warning("User profile summary refresh failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
# AUTO-CAPTURE QUEUE SYSTEM (Memory-Leak-Safe)
#
# PROBLEM: Daemon threads + asyncio.run() + ONNX + asyncpg = 20GB+ memory leaks
# - ONNX Runtime doesn't release memory properly when sessions aren't closed
# - asyncpg connection pools leak in daemon thread event loops
# - asyncio.run() in daemon threads creates orphaned resources
#
# SOLUTION: Queue-based approach that processes in the MAIN event loop
# - No daemon threads for async work
# - Reuses main event loop (no orphaned loops)
# - ONNX model is singleton with proper session management
# - asyncpg uses main thread's connection pool
# ═══════════════════════════════════════════════════════════════════════════════
from queue import Queue, Empty
from typing import NamedTuple


class _AutoCaptureTask(NamedTuple):
    """Queued auto-capture task."""
    user_input: str
    assistant_response: str
    user_scope_id: UUID


# Queue for pending auto-capture tasks (processed in main event loop)
_auto_capture_queue: "Queue[_AutoCaptureTask]" = Queue(maxsize=20)


def run_auto_capture_sync(
    user_input: str,
    assistant_response: str,
    user_scope_id: Optional[UUID],
) -> None:
    """
    Queue auto-capture for later processing in the main event loop.

    This is MEMORY-LEAK SAFE because:
    - No daemon threads (which leak with asyncio.run() + ONNX + asyncpg)
    - Tasks are processed in the main event loop via process_auto_capture_queue()
    - ONNX model and DB connections are reused from main thread

    The queue is processed by the web_server's WebSocket handler after each chat.
    """
    if not Config.get("memory_enabled", True) or not Config.get("memory_auto_capture", True):
        return
    if user_scope_id is None:
        return

    try:
        task = _AutoCaptureTask(
            user_input=user_input,
            assistant_response=assistant_response,
            user_scope_id=user_scope_id,
        )
        # Non-blocking put - drop if queue full (prevents memory buildup)
        _auto_capture_queue.put_nowait(task)
        logger.debug("Auto-capture queued (queue size: %d)", _auto_capture_queue.qsize())
    except Exception as e:
        # Queue full or other error - just skip this capture
        logger.debug("Auto-capture queue full, skipping: %s", e)


async def process_auto_capture_queue(max_tasks: int = 2) -> int:
    """
    Process pending auto-capture tasks from queue.

    MUST be called from the main async context (e.g., web_server WebSocket handler).
    This ensures ONNX and asyncpg run in the main event loop, avoiding memory leaks.

    Args:
        max_tasks: Max tasks to process per call (prevents blocking chat responses)

    Returns:
        Number of tasks processed
    """
    processed = 0

    for _ in range(max_tasks):
        try:
            task = _auto_capture_queue.get_nowait()
        except Empty:
            break

        try:
            await auto_capture_memory(
                task.user_input,
                task.assistant_response,
                task.user_scope_id,
            )
            processed += 1
            logger.debug("Auto-capture processed successfully")
        except Exception as e:
            import traceback
            logger.warning("Auto-capture processing error: %s\n%s", e, traceback.format_exc())
        finally:
            _auto_capture_queue.task_done()

    return processed


def get_auto_capture_queue_size() -> int:
    """Get current auto-capture queue size (for monitoring)."""
    return _auto_capture_queue.qsize()


def refine_rag_request(query: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Refine a RAG query for better retrieval on user-profile / "who am I" style questions.

    Keyword-based (no LLM). When the query suggests user/preferences/remember context,
    appends disambiguating terms so semantic search tends to hit profile/compaction memories.
    Optionally returns a metadata_filter; currently returns None to avoid over-filtering.

    Returns:
        (refined_query, optional_metadata_filter)
    """
    if not query or not query.strip():
        return (query, None)
    q = query.strip().lower()
    # Phrases that suggest "user profile / what do you know about me"
    user_profile_phrases = (
        "who am i", "who am I", "what is this user", "this user", "about me", "about the user",
        "what do you know", "what do you remember", "my preferences", "user preferences",
        "remember me", "merke dir", "was weißt du über mich", "was weisst du",
        "what do you have on me", "my info", "user info", "user facts",
        "preferences", "meine präferenzen", "sag mir was du über mich weißt",
    )
    is_user_query = any(p in q for p in user_profile_phrases) or (
        len(q) <= 80 and any(w in q for w in ("user", "me ", " me", "preferences", "remember"))
    )
    if not is_user_query:
        return (query, None)
    # Short vague query: expand for semantic disambiguation
    max_expand_len = 100
    if len(q) <= max_expand_len:
        refined = (query.strip() + " user profile facts preferences about this user").strip()
        # Cap total length for embedding
        from vaf.memory.embeddings import MAX_EMBED_INPUT_CHARS
        if len(refined) > MAX_EMBED_INPUT_CHARS:
            refined = refined[:MAX_EMBED_INPUT_CHARS].rstrip()
        return (refined, None)
    return (query, None)


def _rag_timing_log(line: str) -> None:
    """Append one timestamped line to rag.log."""
    append_domain_log("rag", line)


def run_memory_search_sync(
    query: str,
    k: int = 5,
    user_scope_id: Optional[UUID] = None,
    caller: Optional[str] = None,
) -> str:
    """
    Run RAG search synchronously for use from sync code (e.g. headless runner).

    Returns a formatted string of top-k snippets for injection into the agent prompt,
    or empty string if memory is disabled, no results, or on error.

    caller: "headless" | "tool" | None – for logging who triggered the RAG call.
    """
    import time as _time
    _t0 = _time.time()

    if not Config.get("memory_enabled", True):
        _rag_timing_log("RAG_SKIP reason=memory_disabled")
        return ""

    # Truncate at entry so we never pass long strings to encoder (avoids 10GB+ RAM spike)
    # Also strip <think> blocks to prevent recursive loops
    from vaf.memory.embeddings import MAX_EMBED_INPUT_CHARS
    import re

    _q = (query or "").strip()
    # 1. Remove complete <think>...</think> blocks
    _q = re.sub(r'<think>.*?</think>', '', _q, flags=re.DOTALL).strip()
    # 2. Remove unclosed <think>... (streaming/partial thought)
    if '<think>' in _q:
        _q = _q.split('<think>')[0].strip()

    if not _q:
        _rag_timing_log(f"RAG_EXIT_EMPTY caller={caller or 'unknown'} duration_sec={_time.time() - _t0:.1f} reason=query_empty_after_strip")
        return ""

    if len(_q) > MAX_EMBED_INPUT_CHARS:
        _q = _q[:MAX_EMBED_INPUT_CHARS].rstrip()
    query = _q

    _rag_timing_log(f"RAG_ENTRY caller={caller or 'unknown'} query_len={len(query)}")

    # Optional: refine query for user-profile style questions (keyword-based)
    metadata_filter: Optional[Dict[str, Any]] = None
    if Config.get("memory_rag_refine_query", True):
        query, metadata_filter = refine_rag_request(query)
        if not query:
            _rag_timing_log(f"RAG_EXIT_EMPTY caller={caller or 'unknown'} duration_sec={_time.time() - _t0:.1f} reason=refine_returned_empty")
            return ""
    _rag_timing_log(f"RAG_AFTER_REFINE query_len={len(query)}")

    # Debug log: who called RAG and with what length (to trace RAM spike)
    qlen = len(query or "")
    will_truncate = qlen >= MAX_EMBED_INPUT_CHARS
    append_domain_log("rag", f"run_memory_search_sync caller={caller or 'unknown'} query_len={qlen} will_truncate={will_truncate}")

    async def _search() -> str:
        async with get_db() as db:
            pipeline = RagPipeline(db)
            sources = await pipeline.search(
                query, k=k, metadata_filter=metadata_filter, user_scope_id=user_scope_id
            )
            
            # PUSH TO WEB UI (for Hover/Info)
            try:
                from vaf.core.web_interface import get_web_interface
                web_sources = []
                for s in sources:
                    web_sources.append({
                        "text": s.text[:200] + "..." if len(s.text) > 200 else s.text,
                        "full_text": s.text,
                        "score": round(s.score, 2),
                        "metadata": s.metadata
                    })
                
                if web_sources:
                    get_web_interface().push_update({
                        "type": "rag_results",
                        "query": query,
                        "sources": web_sources
                    })
            except Exception as e:
                # Don't break RAG if UI push fails
                logger.warning(f"Failed to push RAG results to UI: {e}")

            if not sources:
                return ""
            parts = []
            for i, s in enumerate(sources):
                parts.append(f"[Source {i+1}] (Relevance: {s.score:.0%})\n{s.text}")
            return "\n\n---\n\n".join(parts)

    _RAG_TIMEOUT = 15.0  # seconds; avoid blocking chat if DB is down or slow

    async def _run_with_timeout() -> str:
        return await asyncio.wait_for(_search(), timeout=_RAG_TIMEOUT)

    _rag_timing_log(f"RAG_ASYNC_START timeout_sec={_RAG_TIMEOUT}")

    try:
        _t_async = _time.time()
        # Check if we're already in an event loop (e.g., called from async headless runner)
        # If so, use nest_asyncio or run in a new thread to avoid deadlock
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            # Already in an async context - run in a daemon thread to avoid deadlock
            import threading
            result_container = [None]
            error_container = [None]

            def _run_in_thread():
                try:
                    result_container[0] = asyncio.run(_run_with_timeout())
                except Exception as e:
                    error_container[0] = e

            thread = threading.Thread(target=_run_in_thread, daemon=True)
            thread.start()
            thread.join(timeout=_RAG_TIMEOUT + 5)  # Extra buffer for thread overhead

            if thread.is_alive():
                _dur = _time.time() - _t_async
                _rag_timing_log(f"RAG_TIMEOUT duration_sec={_dur:.1f} timeout_sec={_RAG_TIMEOUT}")
                logger.warning("RAG search timed out after %.0fs (chat continues without memory)", _RAG_TIMEOUT)
                return ""

            if error_container[0] is not None:
                raise error_container[0]

            result = result_container[0] or ""
        else:
            # Not in an async context - safe to use asyncio.run()
            result = asyncio.run(_run_with_timeout())

        _dur = _time.time() - _t_async
        _rag_timing_log(f"RAG_ASYNC_END duration_sec={_dur:.1f} result_len={len(result)}")
        return result
    except asyncio.TimeoutError:
        _dur = _time.time() - _t_async
        _rag_timing_log(f"RAG_TIMEOUT duration_sec={_dur:.1f} timeout_sec={_RAG_TIMEOUT}")
        logger.warning("RAG search timed out after %.0fs (chat continues without memory)", _RAG_TIMEOUT)
        return ""
    except Exception as e:
        _dur = _time.time() - _t_async
        _rag_timing_log(f"RAG_ERROR duration_sec={_dur:.1f} error={repr(e)[:80]}")
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
