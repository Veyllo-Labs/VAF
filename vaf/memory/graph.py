"""
Graph and tree relationship management for VAF Memory System.

Provides:
- Memory graph operations (nodes, edges)
- Tree hierarchy management
- Auto-connection based on similarity
- ReactFlow-compatible data export
"""

from typing import List, Optional, Dict, Any, Tuple
from uuid import UUID
from sqlalchemy import select, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from vaf.memory.models import Memory, Connection, Chunk, EMBEDDING_DIM
from vaf.core.config import Config
import logging

logger = logging.getLogger(__name__)


class GraphManager:
    """
    Manages memory graph relationships and visualization data.
    
    Handles:
    - Creating/removing connections between memories
    - Auto-connecting similar memories
    - Generating ReactFlow-compatible graph data
    - Tree hierarchy traversal
    """
    
    def __init__(self, db: AsyncSession):
        """
        Initialize graph manager with database session.
        
        Args:
            db: Async database session
        """
        self.db = db
        self.auto_connect_threshold = Config.get("memory_auto_connect_threshold", 0.7)
    
    async def get_graph_data(
        self,
        limit: int = 100,
        include_deleted: bool = False,
        highlight_ids: Optional[List[str]] = None,
        relevance_scores: Optional[Dict[str, float]] = None
    ) -> Dict[str, Any]:
        """
        Get memory graph data for ReactFlow visualization.
        
        Args:
            limit: Maximum number of nodes to return
            include_deleted: Include soft-deleted memories
            highlight_ids: Memory IDs to highlight (e.g., RAG sources)
            relevance_scores: Dict mapping memory ID to relevance score
            
        Returns:
            Dict with 'nodes' and 'edges' arrays for ReactFlow
        """
        # Query memories
        query = select(Memory).where(Memory.is_deleted == include_deleted)
        query = query.order_by(Memory.updated_at.desc()).limit(limit)
        
        result = await self.db.execute(query)
        memories = result.scalars().all()
        
        if not memories:
            return {"nodes": [], "edges": []}
        
        memory_ids = [m.id for m in memories]
        
        # Query connections between these memories
        conn_query = select(Connection).where(
            and_(
                Connection.source_id.in_(memory_ids),
                Connection.target_id.in_(memory_ids)
            )
        )
        conn_result = await self.db.execute(conn_query)
        connections = conn_result.scalars().all()
        
        # Build nodes
        highlight_set = set(highlight_ids or [])
        nodes = []
        
        for i, memory in enumerate(memories):
            memory_id_str = str(memory.id)
            
            # Calculate position (simple grid layout, frontend can re-layout)
            x = (i % 5) * 300
            y = (i // 5) * 200
            
            # Get relevance score if provided
            relevance = relevance_scores.get(memory_id_str, 0) if relevance_scores else 0
            
            node = {
                "id": memory_id_str,
                "type": "memoryNode",  # Custom ReactFlow node type
                "position": {"x": x, "y": y},
                "data": {
                    "label": (memory.meta or {}).get("title", "Untitled"),
                    "tags": (memory.meta or {}).get("tags", []),
                    "preview": (memory.meta or {}).get("preview", ""),
                    "type": (memory.meta or {}).get("type", "note"),
                    "createdAt": memory.created_at.isoformat() if memory.created_at else None,
                    "updatedAt": memory.updated_at.isoformat() if memory.updated_at else None,
                    "chunkCount": len(memory.chunks) if memory.chunks else 0,
                    "isHighlighted": memory_id_str in highlight_set,
                    "relevance": relevance,
                    "hasParent": memory.parent_id is not None,
                    "parentId": str(memory.parent_id) if memory.parent_id else None,
                }
            }
            nodes.append(node)
        
        # Build edges
        edges = []
        for conn in connections:
            edge = {
                "id": str(conn.id),
                "source": str(conn.source_id),
                "target": str(conn.target_id),
                "type": "smoothstep",  # ReactFlow edge type
                "animated": conn.connection_type == "semantic",
                "data": {
                    "strength": conn.strength,
                    "connectionType": conn.connection_type,
                    "label": conn.label,
                },
                "style": {
                    "strokeWidth": max(1, int(conn.strength * 3)),
                    "opacity": 0.3 + (conn.strength * 0.7),
                }
            }
            edges.append(edge)
        
        return {"nodes": nodes, "edges": edges}
    
    async def create_connection(
        self,
        source_id: UUID,
        target_id: UUID,
        connection_type: str = "manual",
        strength: float = 1.0,
        label: Optional[str] = None
    ) -> Connection:
        """
        Create a connection between two memories.
        
        Args:
            source_id: Source memory UUID
            target_id: Target memory UUID
            connection_type: Type of connection (semantic, manual, temporal)
            strength: Connection strength (0.0 - 1.0)
            label: Optional label for the connection
            
        Returns:
            Created Connection object
        """
        # Check for existing connection
        existing = await self.db.execute(
            select(Connection).where(
                and_(
                    Connection.source_id == source_id,
                    Connection.target_id == target_id
                )
            )
        )
        if existing.scalar_one_or_none():
            # Update existing
            conn = existing.scalar_one()
            conn.strength = strength
            conn.label = label
            return conn
        
        # Create new connection
        connection = Connection(
            source_id=source_id,
            target_id=target_id,
            connection_type=connection_type,
            strength=strength,
            label=label
        )
        self.db.add(connection)
        await self.db.flush()
        
        return connection
    
    async def remove_connection(self, source_id: UUID, target_id: UUID) -> bool:
        """
        Remove a connection between two memories.
        
        Args:
            source_id: Source memory UUID
            target_id: Target memory UUID
            
        Returns:
            True if connection was removed, False if not found
        """
        result = await self.db.execute(
            select(Connection).where(
                and_(
                    Connection.source_id == source_id,
                    Connection.target_id == target_id
                )
            )
        )
        connection = result.scalar_one_or_none()
        
        if connection:
            await self.db.delete(connection)
            return True
        return False
    
    async def auto_connect_memory(
        self,
        memory: Memory,
        threshold: Optional[float] = None,
        max_connections: int = 5
    ) -> List[Connection]:
        """
        Automatically connect a memory to similar memories.
        
        Uses vector similarity to find related memories and creates
        semantic connections above the threshold.
        
        Args:
            memory: Memory to connect
            threshold: Similarity threshold (default from config)
            max_connections: Maximum number of connections to create
            
        Returns:
            List of created connections
        """
        if memory.embedding is None:
            logger.warning(f"Memory {memory.id} has no embedding, skipping auto-connect")
            return []
        
        threshold = threshold or self.auto_connect_threshold
        
        # Find similar memories using pgvector
        # Cosine distance: 1 - cosine_similarity
        # So we want distance < (1 - threshold)
        max_distance = 1.0 - threshold
        
        query = select(Memory, Memory.embedding.cosine_distance(memory.embedding).label("distance")).where(
            and_(
                Memory.id != memory.id,
                Memory.is_deleted == False,
                Memory.embedding.isnot(None)
            )
        ).order_by("distance").limit(max_connections)
        
        result = await self.db.execute(query)
        similar_memories = result.all()
        
        connections = []
        for similar_memory, distance in similar_memories:
            if distance < max_distance:
                strength = 1.0 - distance  # Convert distance to similarity
                conn = await self.create_connection(
                    source_id=memory.id,
                    target_id=similar_memory.id,
                    connection_type="semantic",
                    strength=strength
                )
                connections.append(conn)
        
        logger.info(f"Auto-connected memory {memory.id} to {len(connections)} similar memories")
        return connections
    
    async def get_memory_connections(
        self,
        memory_id: UUID,
        direction: str = "both"
    ) -> List[Connection]:
        """
        Get all connections for a memory.
        
        Args:
            memory_id: Memory UUID
            direction: "outgoing", "incoming", or "both"
            
        Returns:
            List of Connection objects
        """
        if direction == "outgoing":
            query = select(Connection).where(Connection.source_id == memory_id)
        elif direction == "incoming":
            query = select(Connection).where(Connection.target_id == memory_id)
        else:
            query = select(Connection).where(
                or_(
                    Connection.source_id == memory_id,
                    Connection.target_id == memory_id
                )
            )
        
        result = await self.db.execute(query)
        return result.scalars().all()
    
    async def get_tree_children(
        self,
        parent_id: Optional[UUID] = None
    ) -> List[Memory]:
        """
        Get child memories in tree hierarchy.
        
        Args:
            parent_id: Parent memory UUID (None for root memories)
            
        Returns:
            List of child Memory objects
        """
        query = select(Memory).where(
            and_(
                Memory.parent_id == parent_id,
                Memory.is_deleted == False
            )
        ).order_by(Memory.created_at)
        
        result = await self.db.execute(query)
        return result.scalars().all()
    
    async def get_tree_path(self, memory_id: UUID) -> List[Memory]:
        """
        Get the path from root to a memory in the tree.
        
        Args:
            memory_id: Target memory UUID
            
        Returns:
            List of Memory objects from root to target
        """
        path = []
        current_id = memory_id
        
        while current_id:
            result = await self.db.execute(
                select(Memory).where(Memory.id == current_id)
            )
            memory = result.scalar_one_or_none()
            
            if not memory:
                break
            
            path.insert(0, memory)
            current_id = memory.parent_id
        
        return path
    
    async def move_memory(
        self,
        memory_id: UUID,
        new_parent_id: Optional[UUID]
    ) -> Memory:
        """
        Move a memory to a new parent in the tree.
        
        Args:
            memory_id: Memory to move
            new_parent_id: New parent UUID (None for root)
            
        Returns:
            Updated Memory object
        """
        result = await self.db.execute(
            select(Memory).where(Memory.id == memory_id)
        )
        memory = result.scalar_one_or_none()
        
        if not memory:
            raise ValueError(f"Memory {memory_id} not found")
        
        # Check for circular reference
        if new_parent_id:
            path = await self.get_tree_path(new_parent_id)
            if any(m.id == memory_id for m in path):
                raise ValueError("Cannot create circular reference in tree")
        
        memory.parent_id = new_parent_id
        await self.db.flush()
        
        return memory
    
    async def update_connections(
        self,
        memory_id: UUID,
        related_ids: List[str],
        connection_type: str = "manual"
    ) -> List[Connection]:
        """
        Update manual connections for a memory.
        
        Replaces existing manual connections with new ones.
        
        Args:
            memory_id: Memory UUID
            related_ids: List of related memory ID strings
            connection_type: Type of connections to update
            
        Returns:
            List of new Connection objects
        """
        # Remove existing connections of this type
        existing = await self.db.execute(
            select(Connection).where(
                and_(
                    Connection.source_id == memory_id,
                    Connection.connection_type == connection_type
                )
            )
        )
        for conn in existing.scalars().all():
            await self.db.delete(conn)
        
        # Create new connections
        connections = []
        for related_id in related_ids:
            try:
                target_uuid = UUID(related_id)
                conn = await self.create_connection(
                    source_id=memory_id,
                    target_id=target_uuid,
                    connection_type=connection_type,
                    strength=1.0
                )
                connections.append(conn)
            except ValueError:
                logger.warning(f"Invalid UUID: {related_id}")
        
        return connections
