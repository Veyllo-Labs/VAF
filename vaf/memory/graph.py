"""
Graph and tree relationship management for VAF Memory System.

Provides:
- Memory graph operations (nodes, edges)
- Tree hierarchy management
- Auto-connection based on similarity
- ReactFlow-compatible data export
"""

from typing import List, Optional, Dict, Any, Tuple, Set
from uuid import UUID
from sqlalchemy import select, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
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
        relevance_scores: Optional[Dict[str, float]] = None,
        user_scope_id: Optional[UUID] = None
    ) -> Dict[str, Any]:
        """
        Get memory graph data for ReactFlow visualization.

        Args:
            limit: Maximum number of nodes to return
            include_deleted: Include soft-deleted memories
            highlight_ids: Memory IDs to highlight (e.g., RAG sources)
            relevance_scores: Dict mapping memory ID to relevance score
            user_scope_id: Filter to only show memories for this user scope

        Returns:
            Dict with 'nodes' and 'edges' arrays for ReactFlow
        """
        # Query memories (eager-load chunks so async session doesn't lazy-load)
        # Filter by user_scope_id if provided
        conditions = [Memory.is_deleted == include_deleted]
        if user_scope_id is not None:
            conditions.append(Memory.user_scope_id == user_scope_id)

        query = (
            select(Memory)
            .where(and_(*conditions))
            .options(selectinload(Memory.chunks))
            .order_by(Memory.updated_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(query)
        memories = result.unique().scalars().all()
        logger.debug("get_graph_data memories count: %s", len(memories))

        if not memories:
            return {"nodes": [], "edges": []}

        # Type-to-stroke-color (matches frontend MemoryGraph)
        type_stroke = {
            "note": "#60a5fa",
            "conversation": "#fb923c",
            "memory_flush": "#fb923c",
            "document": "#c084fc",
            "code": "#4ade80",
        }
        default_stroke = "#9ca3af"
        memory_id_to_type = {str(m.id): (m.meta or {}).get("type", "note") for m in memories}
        
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
        
        # Build edges from connections
        edges = []
        for conn in connections:
            mem_type = memory_id_to_type.get(str(conn.source_id), "note")
            stroke = type_stroke.get(mem_type, default_stroke)
            edge = {
                "id": str(conn.id),
                "source": str(conn.source_id),
                "target": str(conn.target_id),
                "type": "smoothstep",
                "animated": conn.connection_type == "semantic",
                "data": {
                    "strength": conn.strength,
                    "connectionType": conn.connection_type,
                    "label": conn.label,
                },
                "style": {
                    "strokeWidth": max(1, int(conn.strength * 3)),
                    "opacity": 0.3 + (conn.strength * 0.7),
                    "stroke": stroke,
                }
            }
            edges.append(edge)

        # Build Tag Master Nodes and edges
        # Collect all unique tags and which memories use them
        # Tags are normalized to lowercase to avoid duplicates like "VAF" and "vaf"
        # Using Set to prevent duplicate memory IDs per tag
        tag_to_memories: Dict[str, Set[str]] = {}
        memory_tag_count: Dict[str, int] = {}  # Track how many tags each memory has

        for memory in memories:
            tags = (memory.meta or {}).get("tags", [])
            memory_id_str = str(memory.id)
            memory_tag_count[memory_id_str] = len(tags)
            for tag in tags:
                # Normalize tag to lowercase for case-insensitive grouping
                normalized_tag = tag.strip().lower()
                if not normalized_tag:
                    continue
                if normalized_tag not in tag_to_memories:
                    tag_to_memories[normalized_tag] = set()
                tag_to_memories[normalized_tag].add(memory_id_str)  # Use add() for Set

        # Find max memory count for scaling tag node sizes
        max_memory_count = max((len(mids) for mids in tag_to_memories.values()), default=1)

        # Create Tag Master Nodes with organic circular positioning
        # Position tags in a circle around the center, memories will be pulled towards their tags
        import math
        tag_count = len(tag_to_memories)
        center_x, center_y = 400, 300  # Center of the graph
        radius = 350  # Radius for tag node circle

        tag_node_counter = 0
        for tag, memory_ids in tag_to_memories.items():
            if len(memory_ids) == 0:
                continue

            tag_node_id = f"tag-{tag}"

            # Position tag nodes in a circle for organic layout
            angle = (2 * math.pi * tag_node_counter) / max(tag_count, 1)
            tag_x = center_x + radius * math.cos(angle)
            tag_y = center_y + radius * math.sin(angle)

            # Calculate size based on memory count (min 1.0, max 2.5 scale)
            size_scale = 1.0 + (len(memory_ids) / max(max_memory_count, 1)) * 1.5

            tag_node = {
                "id": tag_node_id,
                "type": "tagNode",
                "position": {"x": tag_x, "y": tag_y},
                "data": {
                    "label": f"#{tag}",
                    "tag": tag,
                    "memoryCount": len(memory_ids),
                    "isTagNode": True,
                    "sizeScale": size_scale,  # For dynamic sizing in frontend
                }
            }
            nodes.append(tag_node)
            tag_node_counter += 1

            # Create edges from each memory to its tag node
            for memory_id in memory_ids:
                tag_count_for_memory = memory_tag_count.get(memory_id, 1)
                edge_strength = min(1.0, 0.3 + (tag_count_for_memory * 0.2))
                stroke_width = max(1, min(5, tag_count_for_memory + 1))
                mem_type = memory_id_to_type.get(memory_id, "note")
                stroke = type_stroke.get(mem_type, default_stroke)

                edge = {
                    "id": f"tag-edge-{tag}-{memory_id}",
                    "source": memory_id,
                    "target": tag_node_id,
                    "type": "default",
                    "animated": False,
                    "data": {
                        "strength": edge_strength,
                        "connectionType": "tag",
                        "label": None,
                    },
                    "style": {
                        "strokeWidth": stroke_width,
                        "opacity": 0.4 + (edge_strength * 0.4),
                        "stroke": stroke,
                    }
                }
                edges.append(edge)

        # Reposition memory nodes towards their connected tags for organic clustering
        for i, node in enumerate(nodes):
            if node["type"] == "memoryNode":
                memory_id = node["id"]
                connected_tags = [tag for tag, mids in tag_to_memories.items() if memory_id in mids]

                if connected_tags:
                    # Calculate average position of connected tags
                    avg_x, avg_y = 0, 0
                    for tag in connected_tags:
                        tag_node_id = f"tag-{tag}"
                        for n in nodes:
                            if n["id"] == tag_node_id:
                                avg_x += n["position"]["x"]
                                avg_y += n["position"]["y"]
                                break
                    avg_x /= len(connected_tags)
                    avg_y /= len(connected_tags)

                    # Position memory between center and average tag position with some randomness
                    import random
                    random.seed(hash(memory_id))  # Consistent positioning
                    offset_x = random.uniform(-80, 80)
                    offset_y = random.uniform(-80, 80)

                    # Move memory 60% towards tag cluster
                    node["position"]["x"] = center_x + (avg_x - center_x) * 0.5 + offset_x
                    node["position"]["y"] = center_y + (avg_y - center_y) * 0.5 + offset_y

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
