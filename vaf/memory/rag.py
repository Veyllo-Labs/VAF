# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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
from sqlalchemy import select, and_, or_, func, text, delete, cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from vaf.memory.models import Memory, Chunk, Connection, EMBEDDING_DIM
from vaf.memory.crypto import get_crypto, MemoryCrypto
from vaf.memory.embeddings import get_embedding_service, get_chunker, EmbeddingService, TextChunker
from vaf.memory.graph import GraphManager
from vaf.memory.database import get_db
from vaf.core.config import Config
from vaf.core.log_helper import append_domain_log
from vaf.memory.tag_links import expand_tags_with_links
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)
ATTACHMENT_EPHEMERAL_SOURCE = "attachment_ephemeral"
_ingest_profile_lock = threading.Lock()
_ingest_profile_seq = 0


def _next_ingest_profile_id() -> int:
    global _ingest_profile_seq
    with _ingest_profile_lock:
        _ingest_profile_seq += 1
        return _ingest_profile_seq


def _ingest_profile_enabled() -> bool:
    return bool(Config.get("memory_ingest_profile_enabled", False))


def _rss_mb() -> float:
    try:
        import os
        import psutil

        return float(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024)
    except Exception:
        return -1.0


def _log_ingest_profile(profile_id: int, stage: str, **fields: Any) -> None:
    if not _ingest_profile_enabled():
        return
    extras = " ".join(f"{k}={v}" for k, v in fields.items())
    append_domain_log("rag", f"INGEST_PROFILE id={profile_id} stage={stage} rss_mb={_rss_mb():.2f} {extras}".strip())


def _tokenize_lexical_query(query: str) -> List[str]:
    return [t for t in re.findall(r"[a-zA-Z0-9_]+", (query or "").lower()) if len(t) >= 2]


def _lexical_score_query_to_text(query_tokens: List[str], text: str) -> float:
    if not text:
        return 0.0
    text_l = text.lower()
    text_tokens = set(_tokenize_lexical_query(text_l))
    q_set = set(query_tokens)
    if not q_set:
        return 0.0
    overlap = len(text_tokens & q_set) / len(q_set)
    phrase = " ".join(query_tokens)
    exact = 1.0 if phrase and phrase in text_l else 0.0
    return min(1.0, (0.75 * overlap) + (0.25 * exact))


def _rrf_merge_sources(
    vector_sources: List["RagSource"],
    lexical_sources: List["RagSource"],
    *,
    top_k: int,
    rrf_k: int = 60,
) -> List["RagSource"]:
    scores: Dict[str, float] = {}
    payload: Dict[str, RagSource] = {}

    for rank, src in enumerate(vector_sources, start=1):
        key = f"{src.memory_id}:{src.chunk_id}"
        scores[key] = scores.get(key, 0.0) + (1.0 / (rrf_k + rank))
        payload.setdefault(key, src)

    for rank, src in enumerate(lexical_sources, start=1):
        key = f"{src.memory_id}:{src.chunk_id}"
        scores[key] = scores.get(key, 0.0) + (1.0 / (rrf_k + rank))
        payload.setdefault(key, src)

    merged = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[: max(1, int(top_k or 5))]
    out: List[RagSource] = []
    for key, _rrf_score in merged:
        src = payload[key]
        out.append(
            RagSource(
                memory_id=src.memory_id,
                chunk_id=src.chunk_id,
                text=src.text,
                score=src.score,  # original cosine similarity (vector) or lexical score, not RRF rank score
                metadata=src.metadata,
            )
        )
    return out


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
        profile_id = _next_ingest_profile_id() if _ingest_profile_enabled() else 0
        _log_ingest_profile(profile_id, "baseline", content_len=len(content or ""))

        if not content or not content.strip():
            raise ValueError("Cannot ingest empty content")
        
        metadata = metadata or {}

        # Set default title if not provided
        if "title" not in metadata:
            # Use first 50 chars as title
            metadata["title"] = content[:50].strip().replace('\n', ' ')
            if len(content) > 50:
                metadata["title"] += "..."

        # Normalize tags and expand with linked tags (tag A↔B: memories get both)
        if "tags" in metadata and isinstance(metadata["tags"], list):
            raw = [t.strip().lower() for t in metadata["tags"] if t and t.strip()]
            metadata["tags"] = expand_tags_with_links(raw)

        # Store a preview in metadata (unencrypted, for display)
        metadata["preview"] = content[:200].strip().replace('\n', ' ')
        if len(content) > 200:
            metadata["preview"] += "..."

        metadata["type"] = metadata.get("type", "note")
        metadata["created_at"] = datetime.utcnow().isoformat()
        
        # 1. Encrypt content
        encrypted_content, nonce = self.crypto.encrypt(content)
        _log_ingest_profile(profile_id, "after_encrypt")
        
        # 2. Create memory embedding (from title/summary)
        # Note: Only E5 models need prefix; MiniLM works without
        model_name = self.embeddings.model_name or ""
        use_prefix = "e5" in model_name.lower()
        summary = f"{metadata.get('title', '')} {' '.join(metadata.get('tags', []))}"
        memory_embedding = await self.embeddings.embed(summary, prefix="passage" if use_prefix else None)
        _log_ingest_profile(profile_id, "after_memory_embedding")
        
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
        _log_ingest_profile(profile_id, "after_memory_flush")
        
        # 4. Chunk and embed
        _log_ingest_profile(profile_id, "before_chunking")
        chunks_data = self.chunker.chunk(content)
        _log_ingest_profile(profile_id, "after_chunking", chunks=len(chunks_data))
        
        if chunks_data:
            chunk_texts = [c["text"] for c in chunks_data]
            _log_ingest_profile(profile_id, "before_chunk_embedding_batch", chunk_texts=len(chunk_texts))
            chunk_embeddings = await self.embeddings.embed_batch(chunk_texts, prefix="passage" if use_prefix else None)
            _log_ingest_profile(profile_id, "after_chunk_embedding_batch", embeddings=len(chunk_embeddings))
            
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
        _log_ingest_profile(profile_id, "after_chunk_flush")
        
        # 5. Auto-connect to similar memories (scoped!)
        if auto_connect:
            # TODO: Update graph manager to respect scope
            await self.graph.auto_connect_memory(memory)
            _log_ingest_profile(profile_id, "after_auto_connect")
        
        logger.info(f"Ingested memory {memory.id} with {len(chunks_data)} chunks (Scope: {user_scope_id})")
        _log_ingest_profile(profile_id, "before_return", memory_id=memory.id, chunks=len(chunks_data))
        
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

        # Keep long-term lane separate from ephemeral attachment lane by default.
        # Explicit attachment lookups can still include source=attachment_ephemeral via metadata_filter.
        wants_attachment_lane = bool(
            metadata_filter and str(metadata_filter.get("source", "")).strip().lower() == ATTACHMENT_EPHEMERAL_SOURCE
        )
        if not wants_attachment_lane:
            filters.append(
                or_(
                    Memory.meta["source"].astext.is_(None),
                    Memory.meta["source"].astext != ATTACHMENT_EPHEMERAL_SOURCE,
                )
            )

        # USER ISOLATION: the scope is mandatory and this fails CLOSED. An empty scope used to mean
        # "search ALL memories (no filter)", which returned one user's chunks/tags to another (the
        # reported cross-user leak). Refuse instead. A deliberate shared/global corpus must be modelled
        # with an explicit sentinel scope + opt-in flag, never an implicit unscoped query.
        if not user_scope_id:
            logger.warning("RagPipeline.search called without user_scope_id - returning no results (fail-closed isolation)")
            return []
        filters.append(Memory.user_scope_id == user_scope_id)

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

        # Optional hybrid retrieval for long-term RAG:
        # combine vector ranking with lexical ranking on the same Chunk store via RRF.
        hybrid_enabled = bool(Config.get("memory_hybrid_enabled"))
        if not hybrid_enabled:
            return sources

        lexical_k = int(Config.get("memory_hybrid_lexical_k", max(k * 4, 20)) or max(k * 4, 20))
        lexical_k = max(k, min(120, lexical_k))
        lexical_scan = int(Config.get("memory_hybrid_lexical_scan_limit", 400) or 400)
        lexical_scan = max(lexical_k, min(2000, lexical_scan))
        rrf_k = int(Config.get("memory_hybrid_rrf_k", 60) or 60)
        rrf_k = max(1, min(500, rrf_k))
        lexical_min_score = float(Config.get("memory_hybrid_lexical_min_score", 0.0) or 0.0)
        lexical_min_score = max(0.0, min(1.0, lexical_min_score))
        query_tokens = _tokenize_lexical_query(query)

        lexical_filters = [Memory.is_deleted == False]
        if not wants_attachment_lane:
            lexical_filters.append(
                or_(
                    Memory.meta["source"].astext.is_(None),
                    Memory.meta["source"].astext != ATTACHMENT_EPHEMERAL_SOURCE,
                )
            )
        # Scope is guaranteed here (the vector lane above already fails closed on an empty scope), but
        # filter unconditionally so the lexical lane can never widen past the caller's scope.
        lexical_filters.append(Memory.user_scope_id == user_scope_id)

        if query_tokens:
            token_ors = [Chunk.text.ilike(f"%{tok}%") for tok in query_tokens[:8]]
            lexical_filters.append(or_(*token_ors))

        lex_stmt = (
            select(Chunk, Memory)
            .join(Memory, Chunk.memory_id == Memory.id)
            .where(and_(*lexical_filters))
            .limit(lexical_scan)
        )
        lex_rows = (await self.db.execute(lex_stmt)).all()
        lexical_debug_enabled = bool(Config.get("debug_logs_enabled", True))
        if lexical_debug_enabled:
            append_domain_log(
                "rag",
                (
                    f"SEARCH_HYBRID_LEXICAL_DEBUG stage=rows query_tokens={len(query_tokens)} "
                    f"user_scope_id={user_scope_id} pre_score_rows={len(lex_rows)} "
                    f"scan_limit={lexical_scan} lexical_min_score={lexical_min_score:.3f}"
                ),
            )
        lexical_sources: List[RagSource] = []
        lexical_scored: List[float] = []
        for chunk, memory in lex_rows:
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
            lscore = _lexical_score_query_to_text(query_tokens, chunk.text or "")
            lexical_scored.append(float(lscore))
            if lscore < lexical_min_score:
                continue
            lexical_sources.append(
                RagSource(
                    memory_id=str(memory.id),
                    chunk_id=str(chunk.id),
                    text=chunk.text,
                    score=float(lscore),
                    metadata=memory.meta or {},
                )
            )
        if lexical_debug_enabled:
            top_scored = sorted(lexical_scored, reverse=True)[:5]
            top_scored_str = ",".join(f"{s:.3f}" for s in top_scored) if top_scored else "none"
            append_domain_log(
                "rag",
                (
                    f"SEARCH_HYBRID_LEXICAL_DEBUG stage=scored scored_rows={len(lexical_scored)} "
                    f"kept_after_min_score={len(lexical_sources)} top_scores={top_scored_str}"
                ),
            )
        lexical_sources.sort(key=lambda s: s.score, reverse=True)
        lexical_sources = lexical_sources[:lexical_k]

        fused = _rrf_merge_sources(sources, lexical_sources, top_k=k, rrf_k=rrf_k)
        if fused:
            vector_keys = {f"{s.memory_id}:{s.chunk_id}" for s in sources}
            lexical_keys = {f"{s.memory_id}:{s.chunk_id}" for s in lexical_sources}
            both = 0
            vector_only = 0
            lexical_only = 0
            for s in fused:
                key = f"{s.memory_id}:{s.chunk_id}"
                if key in vector_keys and key in lexical_keys:
                    both += 1
                elif key in vector_keys:
                    vector_only += 1
                elif key in lexical_keys:
                    lexical_only += 1
            append_domain_log(
                "rag",
                (
                    f"SEARCH_HYBRID_FUSION topk={len(fused)} both={both} vector_only={vector_only} "
                    f"lexical_only={lexical_only} vector_candidates={len(sources)} lexical_candidates={len(lexical_sources)}"
                ),
            )
            return fused

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

            provider = Config.get("provider", "local")
            backend = APIBackendManager(provider)
            
            # chat_completion is a generator, collect all chunks
            def get_response():
                result = ""
                for chunk in backend.chat_completion(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=1024,
                    stream=False
                ):
                    if isinstance(chunk, dict):
                        content = chunk.get("choices", [{}])[0].get("message", {}).get("content", "")
                        if content:
                            result = content
                    elif isinstance(chunk, str):
                        result += chunk
                return result

            response = await asyncio.get_event_loop().run_in_executor(None, get_response)
            return response
        except Exception as e:
            logger.error(f"Error generating answer: {e}")
            return f"Error generating answer: {str(e)}"
    
    async def _stream_answer(self, prompt: str) -> AsyncGenerator[str, None]:
        """Stream answer tokens using VAF's API backend."""
        try:
            from vaf.core.api_backend import APIBackendManager

            provider = Config.get("provider", "local")
            backend = APIBackendManager(provider)
            
            # Use streaming API
            for chunk in backend.chat_completion_stream(
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
    
    async def get_memory(
        self, memory_id: UUID, decrypt: bool = True, user_scope_id: Optional[UUID] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Get a memory by ID.

        Args:
            memory_id: Memory UUID
            decrypt: Whether to decrypt content
            user_scope_id: If set, only return the memory if it belongs to this user

        Returns:
            Memory dict with optional decrypted content
        """
        conditions = [Memory.id == memory_id]
        if user_scope_id is not None:
            conditions.append(Memory.user_scope_id == user_scope_id)

        result = await self.db.execute(
            select(Memory)
            .where(and_(*conditions))
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
        metadata: Optional[Dict[str, Any]] = None,
        user_scope_id: Optional[UUID] = None
    ) -> Memory:
        """
        Update a memory's content and/or metadata.

        Args:
            memory_id: Memory UUID
            content: New content (re-encrypted, re-chunked)
            metadata: New metadata (merged with existing)
            user_scope_id: If set, only update the memory if it belongs to this user

        Returns:
            Updated Memory object
        """
        conditions = [Memory.id == memory_id]
        if user_scope_id is not None:
            conditions.append(Memory.user_scope_id == user_scope_id)

        result = await self.db.execute(
            select(Memory).where(and_(*conditions))
        )
        memory = result.scalar_one_or_none()

        if not memory:
            raise ValueError(f"Memory {memory_id} not found")

        if metadata:
            # Normalize tags and expand with linked tags
            if "tags" in metadata and isinstance(metadata["tags"], list):
                raw = [t.strip().lower() for t in metadata["tags"] if t and t.strip()]
                metadata["tags"] = expand_tags_with_links(raw)
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
    
    async def delete_memory(
        self, memory_id: UUID, soft: bool = True, user_scope_id: Optional[UUID] = None
    ) -> bool:
        """
        Delete a memory.

        Args:
            memory_id: Memory UUID
            soft: If True, soft delete (set is_deleted flag)
            user_scope_id: If set, only delete the memory if it belongs to this user

        Returns:
            True if deleted, False if not found
        """
        conditions = [Memory.id == memory_id]
        if user_scope_id is not None:
            conditions.append(Memory.user_scope_id == user_scope_id)

        result = await self.db.execute(
            select(Memory).where(and_(*conditions))
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

    async def delete_by_tag(
        self,
        tag: str,
        soft: bool = True,
        user_scope_id: Optional[UUID] = None,
    ) -> int:
        """
        Delete all non-deleted memories that carry the given tag.

        Uses PostgreSQL JSONB array containment (@>) to find memories whose
        meta->'tags' array includes the given tag value.

        Returns:
            Count of memories deleted / marked deleted.
        """
        conditions = [
            Memory.is_deleted == False,  # noqa: E712
            Memory.meta.contains({"tags": [tag]}),
        ]
        if user_scope_id is not None:
            conditions.append(Memory.user_scope_id == user_scope_id)

        result = await self.db.execute(select(Memory).where(and_(*conditions)))
        memories = result.scalars().all()

        count = 0
        for memory in memories:
            if soft:
                memory.is_deleted = True
            else:
                await self.db.delete(memory)
            count += 1

        logger.info(f"delete_by_tag tag={tag!r} soft={soft} count={count}")
        return count

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
        conditions.append(
            or_(
                Memory.meta["source"].astext.is_(None),
                Memory.meta["source"].astext != ATTACHMENT_EPHEMERAL_SOURCE,
            )
        )
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

        # Apply tag filter if specified (case-insensitive)
        if tag_filter:
            # Normalize filter tags to lowercase
            normalized_filter = [t.strip().lower() for t in tag_filter if t and t.strip()]
            memories = [
                m for m in memories
                if any(
                    filter_tag in [t.lower() for t in (m.meta or {}).get("tags", [])]
                    for filter_tag in normalized_filter
                )
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

    async with get_db(user_scope_id=user_scope_id) as db:
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


def _strip_think_reply(text: str) -> str:
    """Remove reasoning-model <think>...</think> blocks before parsing MEMORY: lines. Without this, a
    reasoning model (e.g. deepseek-v4-pro) drafts MEMORY: lines INSIDE its <think> trace and the raw
    reasoning itself gets persisted as a memory (observed: 27 '<think>We are asked...' chunks in the DB).
    Mirrors vaf/tools/learn_document._strip_think (the document path already does this); inlined here to
    avoid a tools->memory import cycle. If an unclosed <think> remains (truncated output), drop from it on."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL | re.IGNORECASE)
    if "<think>" in t.lower():
        t = re.split(r"(?i)<think>", t)[0]
    return t.strip()


def _parse_memory_reply(reply: str) -> List[Tuple[str, List[str]]]:
    """
    Parse compaction LLM reply for MEMORY: "..." [tag1, tag2] or MEMORY: "..." lines.
    Returns list of (content, tags) tuples; NO_REPLY or no MEMORY lines => [].
    Tags are optional; format: MEMORY: "content" [tag1, tag2]. If no tags, returns [].
    """
    if not reply or not reply.strip():
        return []
    reply = _strip_think_reply(reply)
    if not reply.strip():
        return []
    reply_upper = reply.strip().upper()
    if "NO_REPLY" in reply_upper and "MEMORY:" not in reply_upper:
        return []
    out: List[Tuple[str, List[str]]] = []
    # Match: MEMORY: "content" [tag1, tag2] or MEMORY: "content" or MEMORY: 'content'
    tag_suffix_re = re.compile(r'\s+\[([^\]]*)\]$')
    for line in reply.splitlines():
        line = line.strip()
        if not line.upper().startswith("MEMORY:"):
            continue
        rest = line[7:].strip()
        # Extract optional tags from suffix [tag1, tag2]
        tags: List[str] = []
        tag_match = tag_suffix_re.search(rest)
        if tag_match:
            tags = [t.strip().lower() for t in tag_match.group(1).split(",") if t.strip()]
            rest = rest[: tag_match.start()].strip()
        # Extract quoted content
        if rest.startswith('"') and rest.endswith('"'):
            content = rest[1:-1].replace('\\"', '"')
        elif rest.startswith("'") and rest.endswith("'"):
            content = rest[1:-1].replace("\\'", "'")
        else:
            content = rest
        if content:
            out.append((content, tags))
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


def _build_compaction_conversation_excerpt(agent: Any, max_chars: int = 12000) -> str:
    """
    Build a readable transcript for compaction from the agent's session history.
    Only user prompts and assistant replies are included; no system messages,
    no tool calls, and no tool results. The model sees only the dialogue.
    """
    history = getattr(agent, "history", None) or []
    if not history:
        return ""
    # Only user and assistant messages; ignore system and tool
    dialogue = [
        (msg.get("role"), (msg.get("content") or "").strip())
        for msg in history
        if (msg.get("role") or "").strip().lower() in ("user", "assistant")
    ]
    lines = []
    total = 0
    for role, content in reversed(dialogue):
        if not content and role != "user":
            continue
        prefix = "User: " if (role or "").strip().lower() == "user" else "Assistant: "
        line = prefix + content.replace("\n", " ").strip()
        if total + len(line) + 2 > max_chars:
            break
        lines.append(line)
        total += len(line) + 2
    lines.reverse()
    return "\n\n".join(lines) if lines else ""


def _is_contact_session(session_id: str) -> bool:
    """
    True if this session is a chat with a contact (Telegram/WhatsApp/Discord), not the main user.
    NOTE: This is a FALLBACK safety check only. The primary DSGVO filter is in headless_runner
    which checks task.metadata["from_contact"]. Main-user Telegram/WhatsApp/Discord sessions
    (where the user themselves chats) are NOT contact sessions and SHOULD be compacted.
    """
    # No longer block by prefix — the headless_runner's from_contact check is the authoritative filter.
    # This function is kept for backward compatibility but always returns False.
    return False


def _trim_telegram_history_after_compaction(session_id: str, current_turn_count: int, keep_user_turns: int) -> None:
    """
    Keep Telegram session history bounded after each Memory Learning run.
    Retains only the newest `keep_user_turns` user turns (+ their following replies/events).
    """
    sid = str(session_id or "")
    if not sid.startswith("telegram_"):
        return
    keep_turns = max(1, int(keep_user_turns or 1))
    try:
        from vaf.core.session import SessionManager
        sm = SessionManager()
        sess = sm.load(sid)
        msgs = list(getattr(sess, "messages", []) or [])
        if not msgs:
            return

        user_seen = 0
        start_idx = 0
        found_cutoff = False
        for idx in range(len(msgs) - 1, -1, -1):
            if getattr(msgs[idx], "role", None) == "user":
                user_seen += 1
                if user_seen >= keep_turns:
                    start_idx = idx
                    found_cutoff = True
                    break
        if not found_cutoff:
            return

        pruned = msgs[start_idx:]
        if len(pruned) >= len(msgs):
            return
        sess.messages = pruned
        if not isinstance(getattr(sess, "runtime_state", None), dict):
            sess.runtime_state = {}
        sess.runtime_state["history_trimmed_at_turn"] = int(current_turn_count)
        sess.runtime_state["history_trimmed_keep_user_turns"] = int(keep_turns)
        sm.save(sess)
        _compaction_log(
            "COMPACTION_HISTORY_TRIM",
            session_id=sid,
            before=str(len(msgs)),
            after=str(len(pruned)),
            keep_user_turns=str(keep_turns),
        )
    except Exception as e:
        _compaction_log("COMPACTION_HISTORY_TRIM_FAIL", session_id=sid, error=str(e)[:200])


def run_session_compaction_sync(
    agent: Any,
    user_scope_id: Optional[UUID],
    session_id: str,
    current_turn_count: int,
) -> None:
    """
    Run session compaction if interval reached: inject prompt, parse MEMORY:/NO_REPLY, ingest to RAG.
    Does not append compaction reply to chat history or UI.
    Runs for main user sessions (Web, Telegram, WhatsApp, Discord).
    DSGVO: Contact chats are filtered upstream in headless_runner (from_contact metadata).
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

    # Track whether we sent a terminal UI update (completed/error). Finally-block will send one if we didn't, so the UI never stays stuck.
    ui_terminated = [False]  # list to allow assignment in nested function

    def _notify_ui(status: str, message: str, memories_saved: int = 0) -> None:
        try:
            from vaf.core.web_interface import get_web_interface
            get_web_interface().push_update({
                "type": "memory_learning",
                "status": status,
                "session_id": session_id,
                "memories_saved": memories_saved,
                "message": message,
            })
            ui_terminated[0] = True
        except Exception:
            pass

    try:
        # Broadcast to WebUI: Memory Learning started
        try:
            from vaf.core.web_interface import get_web_interface
            get_web_interface().push_update({
                "type": "memory_learning",
                "status": "started",
                "session_id": session_id,
                "message": "Memory Learning in progress... Analyzing conversation for important facts."
            })
        except Exception:
            pass

        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        conversation = _build_compaction_conversation_excerpt(agent)
        if conversation:
            prompt = (
                "You are storing durable memories from this chat. Read the conversation below and output concrete facts worth remembering: "
                "user preferences, name, decisions, events, technical choices, or anything the user would want recalled later. "
                'Output each fact as: MEMORY: "fact in English" [tag1, tag2]. '
                "Use 1-3 relevant tags per memory (e.g. preferences, work, personal, project-x, decisions). Tags help filter in the memory graph. "
                "GROUNDING (critical): store ONLY facts the user STATED explicitly or that are directly evidenced in the conversation. "
                "Do NOT infer or invent habits, routines, schedules, preferences, or numbers that were not stated. "
                "Do NOT turn an exploratory, hypothetical, or philosophical remark into a durable preference or routine. "
                "Preserve the user's exact wording for named concepts; do not paraphrase or guess spellings. "
                "If you are not sure a fact was actually stated, leave it out. "
                "Do not output meta-commentary (e.g. no \"final check\", \"compliance\", or \"retention policy\"). "
                f"Reply with exactly NO_REPLY if there is nothing concrete to store.\n\n"
                "--- Conversation ---\n"
                f"{conversation}\n"
                "---\n\n"
                "Output MEMORY: \"...\" [tags] lines or NO_REPLY."
            )
        else:
            prompt = (
                "Session nearing compaction. Store durable memories now. "
                f"Write any lasting notes to memory/{date_str}.md. "
                'Output each fact as MEMORY: "fact in English" [tag1, tag2]. Use 1-3 tags per memory. Reply with NO_REPLY if nothing to store.'
            )
        try:
            reply = agent._generate_for_compaction(prompt)
        except Exception as e:
            logger.warning("Compaction LLM call failed: %s", e)
            _compaction_log("COMPACTION_LLM_FAIL", session_id=session_id, error=str(e)[:200])
            _notify_ui("error", "Memory Learning failed. Will retry later.")
            return
        memory_tuples = _parse_memory_reply(reply)
        if not memory_tuples:
            _compaction_log("COMPACTION_NO_REPLY", session_id=session_id)
            state[session_id] = current_turn_count
            _save_compaction_state(state)
            _trim_telegram_history_after_compaction(
                session_id=session_id,
                current_turn_count=current_turn_count,
                keep_user_turns=interval,
            )
            _notify_ui("completed", "Memory Learning complete! No new facts to remember.", 0)
            # Run refresh in background so we never block the queue worker or risk affecting the web server
            def _refresh_bg():
                try:
                    refresh_user_profile_summary(user_scope_id)
                except Exception as ex:
                    logger.debug("User profile summary refresh failed: %s", ex)
            threading.Thread(target=_refresh_bg, daemon=True).start()
            return
        async def _ingest_all() -> None:
            async with get_db(user_scope_id=user_scope_id) as db:
                pipeline = RagPipeline(db)
                for content, tags in memory_tuples:
                    if not content or not content.strip():
                        continue
                    meta: Dict[str, Any] = {
                        "source": f"memory/{date_str}",
                        "type": "conversation",  # From 15-message compaction; orange in graph
                    }
                    if tags:
                        meta["tags"] = tags
                    else:
                        meta["tags"] = ["compaction"]  # Fallback so memories are filterable
                    await pipeline.ingest(
                        content=content.strip(),
                        metadata=meta,
                        user_scope_id=user_scope_id,
                        auto_connect=False,
                    )

        # Run in a daemon thread with timeout - never block the queue worker
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
        _compaction_log("COMPACTION_DONE", session_id=session_id, memories=str(len(memory_tuples)), date=date_str)
        state[session_id] = current_turn_count
        _save_compaction_state(state)

        # Save last_compaction_at_turn in session.runtime_state for UI display
        try:
            from vaf.core.session import SessionManager
            _sm = SessionManager()
            _session = _sm.load(session_id)
            if not hasattr(_session, 'runtime_state') or _session.runtime_state is None:
                _session.runtime_state = {}
            _session.runtime_state["last_compaction_at_turn"] = current_turn_count
            _sm.save(_session)
        except Exception as e:
            logger.debug("Failed to save compaction turn to session: %s", e)

        _trim_telegram_history_after_compaction(
            session_id=session_id,
            current_turn_count=current_turn_count,
            keep_user_turns=interval,
        )

        _notify_ui("completed", f"Memory Learning complete! Saved {len(memory_tuples)} memories.", len(memory_tuples))
        # Run refresh in background so we never block the queue worker
        def _refresh_bg():
            try:
                refresh_user_profile_summary(user_scope_id)
            except Exception as ex:
                logger.debug("User profile summary refresh failed: %s", ex)
        threading.Thread(target=_refresh_bg, daemon=True).start()

    except Exception as e:
        logger.exception("Session compaction failed: %s", e)
        _compaction_log("COMPACTION_FAIL", session_id=session_id, error=str(e)[:200])
        _notify_ui("error", "Memory Learning failed. Will retry later.")
    finally:
        # Ensure UI never stays on "in progress" if we didn't send completed/error (e.g. crash or push_update dropped)
        if not ui_terminated[0]:
            _notify_ui("completed", "Memory Learning finished.")


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
# RAG MEMORY-SAFETY CHARTER  —  read this before adding ANY new embedding/ingest lane
#
# (Why isn't the fancy semantic search over channel history here yet? Current official
#  reason: skill issue. Real translation: get the leak story right first. — PS, Mert Elsner)
#
# WHY THIS EXISTS: this subsystem has a real memory-leak history. Rapid, repeated
# embedding/ingest churn caused multi-GB RSS runaways. The defenses you see across
# the memory stack are scar tissue from exactly that, NOT premature optimization:
#   - the process-global singleton embedding model (never reloaded per item),
#   - the attachment lane's `attachment_rag_safe_mode` lexical fallback (bypasses
#     the vector/embedding lane entirely),
#   - the RSS "killer", per-window index rate-limiting and burst coalescing,
#   - the main-loop AUTO-CAPTURE QUEUE below.
# These leaks are a DIFFERENT subsystem from the frontend QtWebEngine/GPU renderer
# leak, and they came first. The frontend leak_diag / renderer auto-recovery cannot
# see a backend embedding leak — there is currently no automatic backend RSS watchdog.
#
# RULE for any NEW RAG/embedding lane — e.g. a "semantic search over channel history"
# indexer over Telegram/Discord/WhatsApp messages (intentionally deferred for now):
#   1. Reuse the singleton model via get_embedding_service(); never construct an
#      embedding model per item.
#   2. Run ingest ONLY on the main event loop (enqueue here / drain in the main loop).
#      NEVER daemon-thread + asyncio.run() — that combo + ONNX + asyncpg is the 20GB
#      leak documented just below.
#   3. Index incrementally: embed only NEW or content-changed items, keyed on a STABLE
#      content hash (not a positional message id). Never embed inside a full
#      delete + re-insert chat rewrite (it would re-embed the entire history).
#   4. Gate behind a config flag, default OFF. Fail CLOSED — keep the lexical/SQLite
#      path working — when the pgvector container (vaf-memory-db) is down.
#   5. Pass auto_connect=False on ingest, and watch backend RSS during the first bulk
#      backfill.
# ═══════════════════════════════════════════════════════════════════════════════


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

    # USER ISOLATION: resolve a concrete scope before any retrieval; never search unscoped.
    # A missing scope previously fell through to a fail-open "search ALL memories" query that leaked
    # other users' snippets/tags. Policy:
    #   - server/multi-user mode + no scope -> DENY (we cannot assume the caller is the admin);
    #   - genuine single-user/local mode + no scope -> fall back to the local-admin scope (the bootstrap
    #     sets this to the admin's real account UUID, so the desktop keeps seeing its own memories).
    # search() additionally fails closed on an empty scope as a backstop.
    if not user_scope_id:
        try:
            _server_mode = bool(Config.get("local_network_enabled", False))
        except Exception:
            _server_mode = False
        if _server_mode:
            _rag_timing_log(f"RAG_DENY caller={caller or 'unknown'} reason=no_user_scope_in_server_mode")
            append_domain_log("rag", f"SEARCH_DENIED caller={caller or 'unknown'} reason=no_user_scope (server mode; refusing to search unscoped)")
            return ""
        from vaf.core.config import get_local_admin_scope_id
        user_scope_id = get_local_admin_scope_id()
    # Normalize to UUID for the DB comparison and the RLS GUC; an unparseable scope is a deny.
    try:
        from uuid import UUID as _UUID
        user_scope_id = user_scope_id if isinstance(user_scope_id, _UUID) else _UUID(str(user_scope_id))
    except (ValueError, TypeError):
        _rag_timing_log(f"RAG_DENY caller={caller or 'unknown'} reason=invalid_user_scope")
        append_domain_log("rag", f"SEARCH_DENIED caller={caller or 'unknown'} reason=invalid_user_scope")
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

    # Min relevance score (0.0-1.0): only snippets >= this threshold are in RAG results
    threshold = float(Config.get("memory_rag_threshold", 0.3))
    threshold = max(0.0, min(1.0, threshold))

    async def _search() -> str:
        # Pass the resolved scope into get_db so RLS (app.current_user_scope_id) is actually engaged
        # for this transaction as defense-in-depth, not just the SQLAlchemy filter.
        async with get_db(user_scope_id=user_scope_id) as db:
            pipeline = RagPipeline(db)
            sources = await pipeline.search(
                query, k=k, threshold=threshold, metadata_filter=metadata_filter, user_scope_id=user_scope_id
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
