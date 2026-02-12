"""
Sync existing memories when a tag link is created.

When tags A and B are linked, all memories with A get B and all with B get A.
"""

import logging
from typing import Optional
from uuid import UUID
from vaf.memory.database import get_db
from vaf.memory.rag import RagPipeline
from vaf.core.config import Config

logger = logging.getLogger(__name__)


async def sync_memories_for_tag_link(
    tag_a: str,
    tag_b: str,
    user_scope_id: Optional[UUID] = None,
) -> int:
    """
    Sync existing memories when tag link A↔B is created.

    - All memories with tag_a get tag_b added
    - All memories with tag_b get tag_a added

    Returns number of memories updated.
    """
    a = (tag_a or "").strip().lower()
    b = (tag_b or "").strip().lower()
    if not a or not b or a == b:
        return 0

    updated = 0
    batch_size = 200

    async with get_db() as db:
        pipeline = RagPipeline(db)

        for tag, add_tag in [(a, b), (b, a)]:
            offset = 0
            while True:
                memories = await pipeline.list_memories(
                    limit=batch_size,
                    offset=offset,
                    tag_filter=[tag],
                    user_scope_id=user_scope_id,
                )
                if not memories:
                    break
                for m in memories:
                    meta = m.get("metadata") or {}
                    tags = list(meta.get("tags") or [])
                    if add_tag not in tags:
                        tags.append(add_tag)
                        tags.sort()
                        meta["tags"] = tags
                        try:
                            await pipeline.update_memory(
                                UUID(m["id"]),
                                metadata=meta,
                            )
                            updated += 1
                        except Exception as e:
                            logger.warning("Failed to sync memory %s: %s", m["id"], e)
                offset += batch_size
                if len(memories) < batch_size:
                    break

    if updated:
        logger.info("Tag link sync: %d memories updated for %s <-> %s", updated, a, b)
    return updated
