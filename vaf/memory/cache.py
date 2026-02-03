"""
Redis caching layer for VAF Memory System.

Provides caching for:
- Embeddings (avoid re-computing same text)
- RAG query results (instant response for repeated queries)
- Session state (WebSocket reconnect without data loss)

Usage:
    from vaf.memory.cache import get_cache, CacheKeys
    
    cache = get_cache()
    
    # Cache an embedding
    await cache.set_embedding("some text", [0.1, 0.2, ...])
    embedding = await cache.get_embedding("some text")
    
    # Cache RAG result
    await cache.set_rag_result("query", result_dict, ttl=3600)
    result = await cache.get_rag_result("query")
"""

import json
import hashlib
import logging
from typing import Optional, List, Dict, Any
from vaf.core.config import Config

logger = logging.getLogger(__name__)

# Redis client (lazy loaded)
_redis_client = None
_redis_available = None


class CacheKeys:
    """Cache key prefixes for different data types."""
    EMBEDDING = "vaf:emb:"
    RAG_QUERY = "vaf:rag:"
    RAG_SOURCES = "vaf:rag_src:"
    SESSION = "vaf:session:"
    MEMORY_GRAPH = "vaf:graph:"
    STATS = "vaf:stats:"


class MemoryCache:
    """
    Redis-based caching for the Memory System.
    
    Falls back gracefully when Redis is unavailable.
    """
    
    # Default TTLs (in seconds)
    DEFAULT_EMBEDDING_TTL = 86400 * 7  # 7 days
    DEFAULT_RAG_TTL = 3600  # 1 hour
    DEFAULT_SESSION_TTL = 86400  # 24 hours
    DEFAULT_GRAPH_TTL = 300  # 5 minutes
    DEFAULT_STATS_TTL = 60  # 1 minute
    
    def __init__(self, redis_client=None):
        """Initialize cache with optional Redis client."""
        self._client = redis_client
        self._available = None
    
    @property
    def client(self):
        """Get Redis client, creating if needed."""
        if self._client is None:
            self._client = _get_redis_client()
        return self._client
    
    async def is_available(self) -> bool:
        """Check if Redis is available."""
        if self._available is not None:
            return self._available
        
        try:
            if self.client:
                await self.client.ping()
                self._available = True
                logger.info("Redis cache connected")
            else:
                self._available = False
        except Exception as e:
            logger.debug(f"Redis not available: {e}")
            self._available = False
        
        return self._available
    
    def _hash_key(self, text: str) -> str:
        """Create a hash key from text."""
        return hashlib.sha256(text.encode()).hexdigest()[:32]
    
    # =========================================================================
    # Embedding Cache
    # =========================================================================
    
    async def get_embedding(self, text: str) -> Optional[List[float]]:
        """Get cached embedding for text."""
        if not await self.is_available():
            return None
        
        try:
            key = f"{CacheKeys.EMBEDDING}{self._hash_key(text)}"
            data = await self.client.get(key)
            if data:
                return json.loads(data)
        except Exception as e:
            logger.debug(f"Cache get_embedding error: {e}")
        
        return None
    
    async def set_embedding(
        self, 
        text: str, 
        embedding: List[float],
        ttl: int = None
    ) -> bool:
        """Cache embedding for text."""
        if not await self.is_available():
            return False
        
        try:
            key = f"{CacheKeys.EMBEDDING}{self._hash_key(text)}"
            ttl = ttl or self.DEFAULT_EMBEDDING_TTL
            await self.client.setex(key, ttl, json.dumps(embedding))
            return True
        except Exception as e:
            logger.debug(f"Cache set_embedding error: {e}")
            return False
    
    # =========================================================================
    # RAG Query Cache
    # =========================================================================
    
    async def get_rag_result(
        self,
        query: str,
        k: int = 5,
        user_scope_id: Optional[str] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get cached RAG result for query (key includes scope and filter)."""
        if not await self.is_available():
            return None
        
        try:
            filter_str = json.dumps(metadata_filter or {}, sort_keys=True)
            cache_key = f"{query}:k={k}:scope={user_scope_id}:filter={filter_str}"
            key = f"{CacheKeys.RAG_QUERY}{self._hash_key(cache_key)}"
            data = await self.client.get(key)
            if data:
                logger.debug(f"RAG cache hit for: {query[:50]}...")
                return json.loads(data)
        except Exception as e:
            logger.debug(f"Cache get_rag_result error: {e}")
        
        return None
    
    async def set_rag_result(
        self,
        query: str,
        result: Dict[str, Any],
        k: int = 5,
        user_scope_id: Optional[str] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
        ttl: int = None,
    ) -> bool:
        """Cache RAG result for query (key includes scope and filter)."""
        if not await self.is_available():
            return False
        
        try:
            filter_str = json.dumps(metadata_filter or {}, sort_keys=True)
            cache_key = f"{query}:k={k}:scope={user_scope_id}:filter={filter_str}"
            key = f"{CacheKeys.RAG_QUERY}{self._hash_key(cache_key)}"
            ttl = ttl or self.DEFAULT_RAG_TTL
            await self.client.setex(key, ttl, json.dumps(result))
            return True
        except Exception as e:
            logger.debug(f"Cache set_rag_result error: {e}")
            return False
    
    # =========================================================================
    # Graph Cache
    # =========================================================================
    
    async def get_graph(self, limit: int = 100) -> Optional[Dict[str, Any]]:
        """Get cached graph data."""
        if not await self.is_available():
            return None
        
        try:
            key = f"{CacheKeys.MEMORY_GRAPH}{limit}"
            data = await self.client.get(key)
            if data:
                return json.loads(data)
        except Exception as e:
            logger.debug(f"Cache get_graph error: {e}")
        
        return None
    
    async def set_graph(
        self,
        graph_data: Dict[str, Any],
        limit: int = 100,
        ttl: int = None
    ) -> bool:
        """Cache graph data."""
        if not await self.is_available():
            return False
        
        try:
            key = f"{CacheKeys.MEMORY_GRAPH}{limit}"
            ttl = ttl or self.DEFAULT_GRAPH_TTL
            await self.client.setex(key, ttl, json.dumps(graph_data))
            return True
        except Exception as e:
            logger.debug(f"Cache set_graph error: {e}")
            return False
    
    async def invalidate_graph(self) -> bool:
        """Invalidate all graph caches (call after memory changes)."""
        if not await self.is_available():
            return False
        
        try:
            # Delete all graph keys
            keys = await self.client.keys(f"{CacheKeys.MEMORY_GRAPH}*")
            if keys:
                await self.client.delete(*keys)
            return True
        except Exception as e:
            logger.debug(f"Cache invalidate_graph error: {e}")
            return False
    
    # =========================================================================
    # Stats Cache
    # =========================================================================
    
    async def get_stats(self) -> Optional[Dict[str, Any]]:
        """Get cached stats."""
        if not await self.is_available():
            return None
        
        try:
            key = f"{CacheKeys.STATS}memory"
            data = await self.client.get(key)
            if data:
                return json.loads(data)
        except Exception as e:
            logger.debug(f"Cache get_stats error: {e}")
        
        return None
    
    async def set_stats(self, stats: Dict[str, Any], ttl: int = None) -> bool:
        """Cache stats."""
        if not await self.is_available():
            return False
        
        try:
            key = f"{CacheKeys.STATS}memory"
            ttl = ttl or self.DEFAULT_STATS_TTL
            await self.client.setex(key, ttl, json.dumps(stats))
            return True
        except Exception as e:
            logger.debug(f"Cache set_stats error: {e}")
            return False
    
    # =========================================================================
    # Cache Management
    # =========================================================================
    
    async def clear_all(self) -> bool:
        """Clear all VAF cache entries."""
        if not await self.is_available():
            return False
        
        try:
            keys = await self.client.keys("vaf:*")
            if keys:
                await self.client.delete(*keys)
                logger.info(f"Cleared {len(keys)} cache entries")
            return True
        except Exception as e:
            logger.error(f"Cache clear_all error: {e}")
            return False
    
    async def get_cache_info(self) -> Dict[str, Any]:
        """Get cache statistics."""
        if not await self.is_available():
            return {"available": False}
        
        try:
            info = await self.client.info("memory")
            keys_count = await self.client.dbsize()
            
            return {
                "available": True,
                "used_memory": info.get("used_memory_human", "unknown"),
                "keys_count": keys_count,
                "max_memory": info.get("maxmemory_human", "unlimited"),
            }
        except Exception as e:
            return {"available": False, "error": str(e)}


def _get_redis_client():
    """Get or create Redis client."""
    global _redis_client, _redis_available
    
    if _redis_available is False:
        return None
    
    if _redis_client is not None:
        return _redis_client
    
    try:
        import redis.asyncio as redis
        
        redis_url = Config.get("redis_url", "redis://localhost:6379/0")
        
        _redis_client = redis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        
        logger.info(f"Redis client created: {redis_url}")
        return _redis_client
        
    except ImportError:
        logger.warning("Redis package not installed, caching disabled")
        _redis_available = False
        return None
    except Exception as e:
        logger.warning(f"Failed to create Redis client: {e}")
        _redis_available = False
        return None


# Singleton cache instance
_cache_instance = None


def get_cache() -> MemoryCache:
    """Get the singleton cache instance."""
    global _cache_instance
    
    if _cache_instance is None:
        _cache_instance = MemoryCache()
    
    return _cache_instance


async def close_cache():
    """Close Redis connection."""
    global _redis_client, _cache_instance
    
    if _redis_client:
        await _redis_client.close()
        _redis_client = None
    
    _cache_instance = None
