"""
SQLAlchemy models for the VAF Memory System.

Tables:
- memories: Core memory entries with encrypted content
- chunks: Text chunks for RAG retrieval
- connections: Graph edges between memories
"""

import uuid
from datetime import datetime
from typing import Optional, List, Any
from sqlalchemy import (
    Column, String, DateTime, ForeignKey, Float, Integer, 
    LargeBinary, Text, Index, JSON, Boolean
)
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.dialects.postgresql import UUID, JSONB
from pgvector.sqlalchemy import Vector

Base = declarative_base()

# Vector dimension for all-MiniLM-L6-v2
EMBEDDING_DIM = 384


class Memory(Base):
    """
    Core memory entry with encrypted content.
    
    The content is AES-256-GCM encrypted at rest.
    Metadata remains unencrypted for filtering/searching.
    """
    __tablename__ = "memories"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Encrypted content (AES-256-GCM)
    encrypted_content = Column(LargeBinary, nullable=False)
    
    # Encryption nonce (required for AES-GCM decryption)
    nonce = Column(LargeBinary, nullable=False)
    
    # Unencrypted metadata for filtering/searching
    # Structure: {"title": str, "tags": [], "source": str, "type": str}
    # Note: Named 'meta' to avoid conflict with SQLAlchemy's reserved 'metadata'
    meta = Column(JSONB, nullable=False, default=dict)
    
    # Summary embedding for memory-level similarity search
    # This embeds a summary/title, not the full content
    embedding = Column(Vector(EMBEDDING_DIM), nullable=True)
    
    # Tree hierarchy (optional parent for nested organization)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("memories.id"), nullable=True)
    
    # Multi-tenancy scope (User ID)
    # If null, it's a global/system memory (or legacy)
    user_scope_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    
    # Soft delete flag
    is_deleted = Column(Boolean, default=False, nullable=False)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationships
    chunks = relationship("Chunk", back_populates="memory", cascade="all, delete-orphan")
    children = relationship("Memory", backref="parent", remote_side=[id])
    
    # Connections where this memory is the source
    outgoing_connections = relationship(
        "Connection",
        foreign_keys="Connection.source_id",
        back_populates="source",
        cascade="all, delete-orphan"
    )
    
    # Connections where this memory is the target
    incoming_connections = relationship(
        "Connection",
        foreign_keys="Connection.target_id",
        back_populates="target",
        cascade="all, delete-orphan"
    )
    
    def __repr__(self):
        title = self.meta.get("title", "Untitled") if self.meta else "Untitled"
        return f"<Memory(id={self.id}, title='{title}')>"
    
    def to_dict(self, include_content: bool = False) -> dict:
        """Convert to dictionary for API responses."""
        result = {
            "id": str(self.id),
            "metadata": self.meta,  # Return as 'metadata' in API for consistency
            "parent_id": str(self.parent_id) if self.parent_id else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "chunk_count": len(self.chunks) if self.chunks else 0,
        }
        # Content is added separately after decryption
        return result


class Chunk(Base):
    """
    Text chunk for RAG retrieval.
    
    Each memory is split into overlapping chunks for better retrieval.
    Chunks store embeddings for vector similarity search.
    """
    __tablename__ = "chunks"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Reference to parent memory
    memory_id = Column(UUID(as_uuid=True), ForeignKey("memories.id", ondelete="CASCADE"), nullable=False)
    
    # Chunk text (stored encrypted in the memory, decrypted for embedding)
    # This is stored in plain text for RAG retrieval efficiency
    # The parent memory's full content remains encrypted
    text = Column(Text, nullable=False)
    
    # Chunk embedding for vector search
    embedding = Column(Vector(EMBEDDING_DIM), nullable=False)
    
    # Position in original document (for reconstruction)
    chunk_index = Column(Integer, nullable=False)
    
    # Character offsets in original content
    start_char = Column(Integer, nullable=True)
    end_char = Column(Integer, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    # Relationship
    memory = relationship("Memory", back_populates="chunks")
    
    def __repr__(self):
        return f"<Chunk(id={self.id}, memory_id={self.memory_id}, index={self.chunk_index})>"
    
    def to_dict(self) -> dict:
        """Convert to dictionary for API responses."""
        return {
            "id": str(self.id),
            "memory_id": str(self.memory_id),
            "text": self.text,
            "chunk_index": self.chunk_index,
            "start_char": self.start_char,
            "end_char": self.end_char,
        }


class Connection(Base):
    """
    Graph edge between memories.
    
    Connections can be:
    - semantic: Auto-created based on cosine similarity
    - manual: User-defined relationships
    - temporal: Based on creation time proximity
    """
    __tablename__ = "connections"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Source and target memories
    source_id = Column(UUID(as_uuid=True), ForeignKey("memories.id", ondelete="CASCADE"), nullable=False)
    target_id = Column(UUID(as_uuid=True), ForeignKey("memories.id", ondelete="CASCADE"), nullable=False)
    
    # Connection strength (cosine similarity for semantic, 1.0 for manual)
    strength = Column(Float, nullable=False, default=1.0)
    
    # Connection type
    connection_type = Column(String(50), nullable=False, default="semantic")
    
    # Optional label for manual connections
    label = Column(String(255), nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    # Relationships
    source = relationship("Memory", foreign_keys=[source_id], back_populates="outgoing_connections")
    target = relationship("Memory", foreign_keys=[target_id], back_populates="incoming_connections")
    
    def __repr__(self):
        return f"<Connection(source={self.source_id}, target={self.target_id}, type={self.connection_type})>"
    
    def to_dict(self) -> dict:
        """Convert to dictionary for API responses."""
        return {
            "id": str(self.id),
            "source_id": str(self.source_id),
            "target_id": str(self.target_id),
            "strength": self.strength,
            "connection_type": self.connection_type,
            "label": self.label,
        }


# Indexes for performance
Index("ix_memories_parent", Memory.parent_id)
Index("ix_memories_created", Memory.created_at.desc())
Index("ix_memories_deleted", Memory.is_deleted)
Index("ix_chunks_memory", Chunk.memory_id)
Index("ix_connections_source", Connection.source_id)
Index("ix_connections_target", Connection.target_id)
Index("ix_connections_type", Connection.connection_type)

# Note: Vector indexes (HNSW) are created in database.py during init
