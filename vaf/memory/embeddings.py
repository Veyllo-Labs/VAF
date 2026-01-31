"""
Embedding service for VAF Memory System.

Uses sentence-transformers for text embeddings:
- all-MiniLM-L6-v2: Fast, 384-dim, good quality
- Async batch processing for performance
- Caching layer for repeated queries
"""

import asyncio
from typing import List, Optional, Dict, Any
from functools import lru_cache
import hashlib
import logging
from vaf.core.config import Config

logger = logging.getLogger(__name__)

# Global model instance (lazy loaded)
_model = None
_model_name = None


def get_model():
    """
    Get or load the sentence-transformers model.
    
    Uses lazy loading to avoid importing heavy dependencies at startup.
    """
    global _model, _model_name
    
    model_name = Config.get("memory_embedding_model", "all-MiniLM-L6-v2")
    
    if _model is None or _model_name != model_name:
        try:
            from sentence_transformers import SentenceTransformer
            logger.info(f"Loading embedding model: {model_name}")
            _model = SentenceTransformer(model_name)
            _model_name = model_name
            logger.info(f"Embedding model loaded: {model_name}")
        except ImportError:
            raise ImportError(
                "sentence-transformers is required for embeddings. "
                "Install with: pip install sentence-transformers"
            )
    
    return _model


class EmbeddingService:
    """
    Service for generating text embeddings.
    
    Features:
    - Lazy model loading
    - Batch processing
    - Two-tier caching: In-memory (fast) + Redis (persistent)
    - Async wrapper for sync operations
    """
    
    # Cache size for embeddings (based on text hash)
    CACHE_SIZE = 1000
    
    def __init__(self, model_name: Optional[str] = None):
        """
        Initialize embedding service.
        
        Args:
            model_name: Override model name from config
        """
        self.model_name = model_name or Config.get("memory_embedding_model", "all-MiniLM-L6-v2")
        self._cache: Dict[str, List[float]] = {}
        self._cache_keys: List[str] = []
        self._redis_cache = None
    
    def _get_redis_cache(self):
        """Get Redis cache (lazy load)."""
        if self._redis_cache is None:
            try:
                from vaf.memory.cache import get_cache
                self._redis_cache = get_cache()
            except Exception:
                pass
        return self._redis_cache
    
    def _get_cache_key(self, text: str) -> str:
        """Generate cache key from text hash."""
        return hashlib.sha256(text.encode()).hexdigest()[:16]
    
    def _add_to_cache(self, text: str, embedding: List[float]):
        """Add embedding to cache with LRU eviction."""
        key = self._get_cache_key(text)
        
        if key in self._cache:
            return
        
        # Evict oldest if cache is full
        if len(self._cache_keys) >= self.CACHE_SIZE:
            old_key = self._cache_keys.pop(0)
            del self._cache[old_key]
        
        self._cache[key] = embedding
        self._cache_keys.append(key)
    
    def _get_from_cache(self, text: str) -> Optional[List[float]]:
        """Get embedding from cache if available."""
        key = self._get_cache_key(text)
        return self._cache.get(key)
    
    def embed_sync(self, text: str) -> List[float]:
        """
        Generate embedding for a single text synchronously.
        
        Args:
            text: Text to embed
            
        Returns:
            List of floats (embedding vector)
        """
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text")
        
        # Check cache
        cached = self._get_from_cache(text)
        if cached:
            return cached
        
        # Generate embedding
        model = get_model()
        embedding = model.encode(text, convert_to_numpy=True).tolist()
        
        # Cache result
        self._add_to_cache(text, embedding)
        
        return embedding
    
    def embed_batch_sync(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for multiple texts synchronously.
        
        More efficient than calling embed_sync multiple times.
        
        Args:
            texts: List of texts to embed
            
        Returns:
            List of embedding vectors
        """
        if not texts:
            return []
        
        # Separate cached and uncached texts
        results = [None] * len(texts)
        uncached_indices = []
        uncached_texts = []
        
        for i, text in enumerate(texts):
            if not text or not text.strip():
                results[i] = [0.0] * 384  # Zero vector for empty text
                continue
            
            cached = self._get_from_cache(text)
            if cached:
                results[i] = cached
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)
        
        # Batch encode uncached texts
        if uncached_texts:
            model = get_model()
            embeddings = model.encode(uncached_texts, convert_to_numpy=True, show_progress_bar=False)
            
            for idx, embedding, text in zip(uncached_indices, embeddings, uncached_texts):
                embedding_list = embedding.tolist()
                results[idx] = embedding_list
                self._add_to_cache(text, embedding_list)
        
        return results
    
    async def embed(self, text: str) -> List[float]:
        """
        Generate embedding for a single text asynchronously.
        
        Checks caches (memory, then Redis) before computing.
        Runs the synchronous operation in a thread pool.
        
        Args:
            text: Text to embed
            
        Returns:
            List of floats (embedding vector)
        """
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text")
        
        # Check in-memory cache first (fastest)
        cached = self._get_from_cache(text)
        if cached:
            return cached
        
        # Check Redis cache (persistent)
        redis_cache = self._get_redis_cache()
        if redis_cache:
            try:
                cached = await redis_cache.get_embedding(text)
                if cached:
                    # Also add to memory cache for faster next access
                    self._add_to_cache(text, cached)
                    return cached
            except Exception:
                pass
        
        # Generate embedding
        loop = asyncio.get_event_loop()
        embedding = await loop.run_in_executor(None, self.embed_sync, text)
        
        # Store in Redis cache (async, don't wait)
        if redis_cache:
            try:
                asyncio.create_task(redis_cache.set_embedding(text, embedding))
            except Exception:
                pass
        
        return embedding
    
    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for multiple texts asynchronously.
        
        Args:
            texts: List of texts to embed
            
        Returns:
            List of embedding vectors
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.embed_batch_sync, texts)
    
    def get_dimension(self) -> int:
        """Get embedding dimension for current model."""
        # Standard dimensions for common models
        dimensions = {
            "all-MiniLM-L6-v2": 384,
            "all-mpnet-base-v2": 768,
            "paraphrase-MiniLM-L6-v2": 384,
            "multi-qa-MiniLM-L6-cos-v1": 384,
        }
        return dimensions.get(self.model_name, 384)
    
    def clear_cache(self):
        """Clear the embedding cache."""
        self._cache.clear()
        self._cache_keys.clear()
    
    @staticmethod
    def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
        """
        Calculate cosine similarity between two vectors.
        
        Args:
            vec1: First embedding vector
            vec2: Second embedding vector
            
        Returns:
            Similarity score (0.0 - 1.0)
        """
        import math
        
        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = math.sqrt(sum(a * a for a in vec1))
        norm2 = math.sqrt(sum(b * b for b in vec2))
        
        if norm1 == 0 or norm2 == 0:
            return 0.0
        
        return dot_product / (norm1 * norm2)


# Singleton instance
_embedding_service: Optional[EmbeddingService] = None


def get_embedding_service() -> EmbeddingService:
    """
    Get the singleton EmbeddingService instance.
    
    Returns:
        Configured EmbeddingService instance
    """
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service


def reset_embedding_service():
    """Reset the embedding service (useful for testing or model changes)."""
    global _embedding_service, _model, _model_name
    _embedding_service = None
    _model = None
    _model_name = None


# Text chunking utilities for RAG
class TextChunker:
    """
    Utility class for chunking text for RAG retrieval.
    
    Uses character-based chunking with token estimation.
    """
    
    # Approximate characters per token (conservative estimate)
    CHARS_PER_TOKEN = 4
    
    def __init__(
        self,
        chunk_size: int = 512,
        chunk_overlap: int = 50,
        min_chunk_size: int = 100
    ):
        """
        Initialize chunker.
        
        Args:
            chunk_size: Target chunk size in tokens
            chunk_overlap: Overlap between chunks in tokens
            min_chunk_size: Minimum chunk size in tokens
        """
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.min_chunk_size = min_chunk_size
        
        # Convert to characters
        self.char_chunk_size = chunk_size * self.CHARS_PER_TOKEN
        self.char_overlap = chunk_overlap * self.CHARS_PER_TOKEN
        self.char_min_size = min_chunk_size * self.CHARS_PER_TOKEN
    
    def chunk(self, text: str) -> List[Dict[str, Any]]:
        """
        Split text into overlapping chunks.
        
        Tries to split on sentence boundaries when possible.
        
        Args:
            text: Text to chunk
            
        Returns:
            List of dicts with 'text', 'start_char', 'end_char', 'index'
        """
        if not text or len(text) < self.char_min_size:
            return [{
                "text": text,
                "start_char": 0,
                "end_char": len(text),
                "index": 0
            }] if text else []
        
        chunks = []
        start = 0
        index = 0
        
        while start < len(text):
            end = start + self.char_chunk_size
            
            # If we're not at the end, try to find a good break point
            if end < len(text):
                # Look for sentence boundary
                search_start = max(start + self.char_min_size, end - 200)
                search_text = text[search_start:end + 100]
                
                # Try to break on sentence-ending punctuation
                best_break = -1
                for punct in ['. ', '.\n', '! ', '!\n', '? ', '?\n']:
                    idx = search_text.rfind(punct)
                    if idx != -1:
                        potential_break = search_start + idx + len(punct)
                        if potential_break > best_break:
                            best_break = potential_break
                
                if best_break > start + self.char_min_size:
                    end = best_break
                else:
                    # Fall back to word boundary
                    space_idx = text[start:end].rfind(' ')
                    if space_idx > self.char_min_size:
                        end = start + space_idx + 1
            else:
                end = len(text)
            
            chunk_text = text[start:end].strip()
            
            if chunk_text:
                chunks.append({
                    "text": chunk_text,
                    "start_char": start,
                    "end_char": end,
                    "index": index
                })
                index += 1
            
            # Move start with overlap
            start = end - self.char_overlap
            if start >= len(text):
                break
        
        return chunks
    
    def estimate_tokens(self, text: str) -> int:
        """Estimate token count for text."""
        return len(text) // self.CHARS_PER_TOKEN


def get_chunker(
    chunk_size: Optional[int] = None,
    chunk_overlap: Optional[int] = None
) -> TextChunker:
    """
    Get a TextChunker with config-based defaults.
    
    Args:
        chunk_size: Override chunk size
        chunk_overlap: Override overlap
        
    Returns:
        Configured TextChunker instance
    """
    return TextChunker(
        chunk_size=chunk_size or Config.get("memory_chunk_size", 512),
        chunk_overlap=chunk_overlap or Config.get("memory_chunk_overlap", 50)
    )
