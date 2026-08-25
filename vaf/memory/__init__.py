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

# Deliberate: these names are re-exported lazily (PEP 562), never at import time.
# Importing them eagerly made the package initialiser pull in rag -> database, while
# database itself is a submodule of this very package. Single-threaded, Python absorbs
# that cycle; with two threads importing concurrently (the tray probing the database
# while the web server mounts the routes) each holds the lock the other waits for, and
# the interpreter aborts the import with a deadlock. The routes then never mount.
_LAZY = {
    "Memory": "vaf.memory.models",
    "Chunk": "vaf.memory.models",
    "Connection": "vaf.memory.models",
    "MemoryCrypto": "vaf.memory.crypto",
    "get_db": "vaf.memory.database",
    "init_db": "vaf.memory.database",
    "EmbeddingService": "vaf.memory.embeddings",
    "RagPipeline": "vaf.memory.rag",
    "GraphManager": "vaf.memory.graph",
    "get_cache": "vaf.memory.cache",
    "MemoryCache": "vaf.memory.cache",
    "close_cache": "vaf.memory.cache",
}

__all__ = list(_LAZY)


def __getattr__(name: str):
    """Resolve a re-exported name on first access, not at package import."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    value = getattr(import_module(module), name)
    globals()[name] = value  # cache, so this runs once per name
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
