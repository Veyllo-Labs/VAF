# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Memory System - Graph-based memory with RAG retrieval.

This module provides:
- Encrypted memory storage with pgvector
- RAG pipeline for semantic retrieval
- Graph visualization data for ReactFlow
- AES-256-GCM encryption at rest
- Redis caching for embeddings and queries
"""

from vaf.memory.models import Memory, Chunk, Connection
from vaf.memory.crypto import MemoryCrypto
from vaf.memory.database import get_db, init_db
from vaf.memory.embeddings import EmbeddingService
from vaf.memory.rag import RagPipeline
from vaf.memory.graph import GraphManager
from vaf.memory.cache import get_cache, MemoryCache, close_cache

__all__ = [
    "Memory",
    "Chunk", 
    "Connection",
    "MemoryCrypto",
    "get_db",
    "init_db",
    "EmbeddingService",
    "RagPipeline",
    "GraphManager",
    "get_cache",
    "MemoryCache",
    "close_cache",
]
