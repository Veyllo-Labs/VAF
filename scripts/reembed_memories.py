#!/usr/bin/env python3
"""
Re-embed all memories and chunks with the current embedding model.
Run this after changing the embedding model in config.

Usage:
    python scripts/reembed_memories.py
"""
import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vaf.core.config import Config
from vaf.memory.database import get_db
from vaf.memory.embeddings import get_embedding_service, get_model
from vaf.memory.models import Memory, Chunk
from sqlalchemy import select


async def reembed_all():
    """Re-embed all chunks with current model."""
    print("=" * 60)
    print("Re-embedding all memories with current model")
    print("=" * 60)

    # Get current model name
    model_name = Config.get("memory_embedding_model", "all-MiniLM-L6-v2")
    print(f"Embedding model: {model_name}")

    # Force load the model
    print("Loading embedding model...")
    model = get_model()
    embedding_service = get_embedding_service()
    print(f"Model loaded: {model}")

    async with get_db() as db:
        # Get all chunks
        stmt = select(Chunk).join(Memory, Chunk.memory_id == Memory.id)
        result = await db.execute(stmt)
        chunks = result.scalars().all()

        print(f"Found {len(chunks)} chunks to re-embed")

        if not chunks:
            print("No chunks found!")
            return

        # Re-embed each chunk
        for i, chunk in enumerate(chunks):
            try:
                # Generate new embedding
                new_embedding = await embedding_service.embed(chunk.text, prefix="passage")
                chunk.embedding = new_embedding

                if (i + 1) % 10 == 0:
                    print(f"  Re-embedded {i + 1}/{len(chunks)} chunks...")
                    await db.flush()

            except Exception as e:
                print(f"  Error re-embedding chunk {chunk.id}: {e}")

        # Final flush
        await db.commit()
        print(f"Done! Re-embedded {len(chunks)} chunks.")

        # Also re-embed memory summaries
        stmt = select(Memory)
        result = await db.execute(stmt)
        memories = result.scalars().all()

        print(f"\nRe-embedding {len(memories)} memory summaries...")

        for i, memory in enumerate(memories):
            try:
                # Generate embedding from title/tags
                meta = memory.meta or {}
                summary = f"{meta.get('title', '')} {' '.join(meta.get('tags', []))}"
                if summary.strip():
                    new_embedding = await embedding_service.embed(summary, prefix="passage")
                    memory.embedding = new_embedding

                if (i + 1) % 10 == 0:
                    print(f"  Re-embedded {i + 1}/{len(memories)} memories...")
                    await db.flush()

            except Exception as e:
                print(f"  Error re-embedding memory {memory.id}: {e}")

        await db.commit()
        print(f"Done! Re-embedded {len(memories)} memories.")


def main():
    asyncio.run(reembed_all())
    print("\n✓ Re-embedding complete!")


if __name__ == "__main__":
    main()
