# VAF Memory System

The Memory System provides persistent, encrypted memory storage with RAG (Retrieval-Augmented Generation) capabilities for VAF. It enables the agent to remember and retrieve information across sessions using semantic search.

## Features

- **Encrypted Storage**: AES-256-GCM encryption for all memory content at rest
- **Vector Search**: PostgreSQL with pgvector extension for semantic similarity search
- **Redis Caching**: Fast caching for embeddings, RAG queries, and graph data
- **RAG pipeline**: Retrieval and answer generation with source citations
- **Graph Visualization**: Interactive ReactFlow-based memory graph
- **Auto-Connections**: Automatically links semantically related memories
- **Streaming**: Token streaming for RAG query responses
- **Session Compaction**: Background process that every N user turns prompts the LLM to write durable memories (MEMORY:/NO_REPLY) into RAG; see [Session Compaction (background)](#session-compaction-background).

## Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    MEMORY SYSTEM ARCHITECTURE                   │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                    WEB UI (/memory)                     │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌───────────────┐    │    │
│  │  │ Memory Graph│  │ RAG Query   │  │ Memory Detail │    │    │
│  │  │ (ReactFlow) │  │   Panel     │  │    Panel      │    │    │
│  │  └─────────────┘  └─────────────┘  └───────────────┘    │    │
│  └─────────────────────────────────────────────────────────┘    │
│                              │                                  │
│                         WebSocket/REST                          │
│                              │                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                    BACKEND (FastAPI)                    │    │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────┐   │    │
│  │  │   RAG    │  │  Graph   │  │ Embedding│  │ Crypto │   │    │
│  │  │ Pipeline │  │ Manager  │  │ Service  │  │AES-256 │   │    │
│  │  └──────────┘  └──────────┘  └──────────┘  └────────┘   │    │
│  └─────────────────────────────────────────────────────────┘    │
│                              │                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              POSTGRESQL + PGVECTOR (Docker)             │    │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐   │    │
│  │  │ memories │  │  chunks  │  │     connections      │   │    │
│  │  │(encrypted)│  │(vectors) │  │   (graph edges)     │   │    │
│  │  └──────────┘  └──────────┘  └──────────────────────┘   │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

### Chat integration (pre-generation injection)

In chat, retrieval runs in the **input phase** before the LLM is called: the user message is used to run a memory search, and the result is injected as the "Memory context (relevant to this query)" block in the first prompt. When `memory_rag_refine_query` is enabled (default), short queries that look like user-profile questions (e.g. "who am I", "what do you remember") are expanded before search so that profile and compaction memories match more often. The `memory_search` tool is for follow-up short queries only; it must not be given model output (e.g. `<think>` content). See [CONTEXT_MANAGEMENT.md](CONTEXT_MANAGEMENT.md#rag-and-memory-context-pre-generation-injection) for details.

## Requirements

- **Docker** (required for PostgreSQL + pgvector + Redis)
- **Python 3.10+**
- **sentence-transformers** (auto-installed)
- **redis** (auto-installed)

## Quick Start

### 1. Start the Services

```bash
# From the VAF root directory
docker compose -f docker-compose.memory.yml up -d
```

This starts:
- **PostgreSQL 16** with pgvector extension (port 5432)
- **Redis 7** for caching (port 6379)

### 2. Enable Memory System

The Memory System is enabled by default. You can toggle it in Settings → Advanced → System.

### 3. Access the Memory Graph

- **Via Settings**: Settings → Advanced → System → Memory System button
- **Direct URL**: `http://localhost:3000/memory`

## Configuration

Settings are available in the VAF Settings Modal under Advanced → System:

| Setting | Default | Description |
|---------|---------|-------------|
| Memory System | Enabled | Enable/disable the entire memory system |
| Chunk Size | 512 | Size of text chunks in tokens for RAG retrieval |
| Auto-Connect Threshold | 0.7 | Cosine similarity threshold (0-1) for auto-connecting memories |

### Advanced Configuration (config.json)

Additional settings in `~/.vaf/config.json`:

```json
{
    "memory_enabled": true,
    "memory_rag_refine_query": true,
    "memory_db_url": "postgresql://vaf:vaf_dev_secret@localhost:5432/vaf_memory",
    "memory_encryption_key": "",
    "memory_embedding_model": "all-MiniLM-L6-v2",
    "memory_auto_connect_threshold": 0.7,
    "memory_chunk_size": 512,
    "memory_chunk_overlap": 50,
    "memory_db_echo": false,
    "memory_compaction_enabled": true,
    "memory_compaction_interval": 15
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `memory_rag_refine_query` | `true` | Expand short user-profile-style queries (e.g. "who am I", "preferences") before search to improve recall. Set to `false` to use the raw user message only. |

### Session Compaction (background)

**Session Compaction** is an automatic background process that periodically asks the LLM to write durable memories from the current session into RAG. It does not append anything to the chat UI.

- **When:** After every chat task, the headless runner checks whether the session has reached the compaction interval (number of user turns since last compaction). Default: every **15 user turns** (`memory_compaction_interval`).
- **What:** A single non-streaming LLM call with a prompt like “Store durable memories now. Write any lasting notes to memory/{date}.md. Output MEMORY: \"...\" lines or NO_REPLY.” The reply is parsed for `MEMORY:` lines; those are ingested into RAG (metadata: `type=memory_flush`, `source=memory/{date}`). Then the user-profile summary cache is refreshed.
- **Where:** Logic in `vaf/memory/rag.py` (`run_session_compaction_sync`); triggered from `vaf/core/headless_runner.py` (after each chat, or enqueued as a separate task when using a local LLM so only one LLM call runs at a time). State per session: `~/.vaf/compaction_state.json` (last compaction turn per `session_id`).
- **Config:** `memory_compaction_enabled` (default `true`), `memory_compaction_interval` (default `15`). Both require `memory_enabled` to be `true`.
- **Logs:** `~/.vaf/logs/compaction.log` (or `%USERPROFILE%\.vaf\logs\compaction.log` on Windows). Lines: `COMPACTION_SKIP` (interval not reached), `COMPACTION_START`, `COMPACTION_NO_REPLY`, `COMPACTION_DONE` (with `memories=N`), `COMPACTION_LLM_FAIL`, `COMPACTION_INGEST_FAIL`. The headless runner also writes `QUEUE_DONE session_id=... (compaction)` to `queue.log` when the compaction task finishes.

## API Reference

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/memory` | POST | Create a new memory |
| `/api/memory` | GET | List memories (paginated) |
| `/api/memory/{id}` | GET | Get memory by ID (decrypted) |
| `/api/memory/{id}` | PUT | Update memory content/metadata |
| `/api/memory/{id}` | DELETE | Delete memory (soft/hard) |
| `/api/memory/graph` | GET | Get graph data for visualization |
| `/api/memory/rag/query` | POST | RAG query (returns answer + sources) |
| `/api/memory/rag/query/stream` | POST | Streaming RAG query (SSE) |
| `/api/memory/search` | POST | Semantic search (no LLM) |
| `/api/memory/stats` | GET | System statistics |
| `/api/memory/health` | GET | Health check |
| `/api/memory/init` | POST | Initialize database schema |

### Create Memory

```bash
curl -X POST http://localhost:8000/api/memory \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Important meeting notes about project timeline...",
    "metadata": {
      "title": "Project Meeting Notes",
      "tags": ["work", "meetings"],
      "type": "note"
    },
    "auto_connect": true
  }'
```

### RAG Query

```bash
curl -X POST http://localhost:8000/api/memory/rag/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What was discussed in the project meeting?",
    "k": 5
  }'
```

### Semantic Search

```bash
curl -X POST http://localhost:8000/api/memory/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "project timeline",
    "k": 10,
    "threshold": 0.5
  }'
```

## Data Model

### Memory

```python
{
    "id": "uuid",
    "metadata": {
        "title": "string",
        "tags": ["string"],
        "type": "note|document|code|conversation",
        "preview": "string (first 200 chars)",
        "created_at": "ISO datetime"
    },
    "parent_id": "uuid | null",  # For tree hierarchy
    "chunk_count": "integer",
    "created_at": "ISO datetime",
    "updated_at": "ISO datetime"
}
```

### Connection

```python
{
    "id": "uuid",
    "source_id": "uuid",
    "target_id": "uuid",
    "strength": "float (0-1)",  # Cosine similarity
    "connection_type": "semantic|manual|temporal",
    "label": "string | null"
}
```

## Encryption

All memory content is encrypted using AES-256-GCM:

- **Key Generation**: 32-byte random key, auto-generated on first use
- **Storage**: Key stored Base64-encoded in config (consider using system keyring for production)
- **Nonce**: Unique 12-byte nonce per encryption
- **Metadata**: Stays unencrypted for filtering/searching

### Key Rotation

To rotate the encryption key:

1. Export all memories (decrypted)
2. Generate new key: `python -c "from vaf.memory.crypto import MemoryCrypto; print(MemoryCrypto.generate_key())"`
3. Update `memory_encryption_key` in config
4. Re-import memories

## Redis Caching

Redis provides a high-performance caching layer for the Memory System:

### What Gets Cached

| Cache Type | TTL | Purpose |
|------------|-----|---------|
| Embeddings | 7 days | Avoid re-computing same text |
| RAG Queries | 1 hour | Instant response for repeated questions |
| Graph Data | 5 min | Fast graph visualization loading |
| Stats | 1 min | Dashboard statistics |

### Cache Benefits

- **Embedding Cache**: ~100x faster for repeated text (skip model inference)
- **RAG Query Cache**: Instant response for repeated questions
- **Reduced API Costs**: Fewer calls to embedding models
- **Better UX**: Graph loads instantly after first fetch

### Configuration

```json
{
    "redis_url": "redis://localhost:6379/0",
    "redis_enabled": true
}
```

### Fallback Behavior

If Redis is unavailable, the system falls back gracefully:
- In-memory caching still works (per-session)
- All features remain functional, just slower for repeated queries

## Embedding Models

The default model is `all-MiniLM-L6-v2` (384 dimensions), which offers a good balance of speed and quality.

Available models:

| Model | Dimensions | Notes |
|-------|------------|-------|
| all-MiniLM-L6-v2 | 384 | Default, fast, good quality |
| all-mpnet-base-v2 | 768 | More accurate, slower |
| paraphrase-MiniLM-L6-v2 | 384 | Good for paraphrasing |
| multi-qa-MiniLM-L6-cos-v1 | 384 | Optimized for Q&A |

Change the model in Settings → Advanced → Memory System.

## Docker Management

### Start Services

```bash
docker compose -f docker-compose.memory.yml up -d
```

This starts both PostgreSQL and Redis.

### Stop Services

```bash
docker compose -f docker-compose.memory.yml down
```

### View Logs

```bash
docker compose -f docker-compose.memory.yml logs -f
```

### Reset Database

```bash
# Stop and remove volume
docker compose -f docker-compose.memory.yml down -v

# Restart
docker compose -f docker-compose.memory.yml up -d
```

### Connection Details

**PostgreSQL:**
- **Host**: localhost
- **Port**: 5432
- **Database**: vaf_memory
- **User**: vaf
- **Password**: vaf_dev_secret (change in production!)

**Redis:**
- **Host**: localhost
- **Port**: 6379
- **URL**: redis://localhost:6379/0

## Troubleshooting

### Database Connection Failed

1. Check Docker is running: `docker ps`
2. Check container status: `docker compose -f docker-compose.memory.yml ps`
3. Check logs: `docker compose -f docker-compose.memory.yml logs`
4. Verify port 5432 is not in use: `netstat -an | grep 5432`

### Embedding Model Slow to Load

The embedding model is downloaded on first use (~90MB). Subsequent loads use the cached model.

### Memory Not Appearing in Graph

1. Ensure the memory was created successfully (check API response)
2. Wait for auto-connect threshold processing
3. Refresh the graph using the refresh button
4. Check if Memory System is enabled in settings

### Decryption Failed

If you see "[Decryption failed]" for memory content:
- The encryption key may have changed
- Memory data may be corrupted
- Check `memory_encryption_key` in config

## Security Considerations

1. **Change default password** in production (`POSTGRES_PASSWORD` in docker-compose)
2. **Use system keyring** for encryption key storage instead of config file
3. **Network isolation**: The Docker container only exposes port 5432 to localhost
4. **Backup encryption keys** separately from data backups

## Performance

- **Vector Index**: HNSW (Hierarchical Navigable Small World) for fast approximate nearest neighbor search
- **Batch Embedding**: Multiple texts embedded in parallel
- **Connection Pooling**: SQLAlchemy async with connection reuse
- **Caching**: Embedding results cached in-memory (LRU, 1000 entries)

Typical performance:
- Memory creation: ~500ms (including chunking, embedding, storage)
- RAG query: ~1-2s (embedding + search + LLM)
- Graph load (100 nodes): ~200ms
