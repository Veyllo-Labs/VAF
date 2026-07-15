# VAF Memory System

A comprehensive memory graph visualization system with RAG (Retrieval-Augmented Generation) retrieval for the Veyllo Agent Framework.

## Features

- **Encrypted Storage**: AES-256-GCM encryption for all memory content at rest
- **Vector Search**: PostgreSQL with pgvector for fast semantic similarity search
- **RAG Pipeline**: Chunk, embed, retrieve, and query with AI-powered responses
- **Graph Visualization**: ReactFlow-based interactive memory graph
- **Auto-Connections**: Automatically connect semantically related memories

## Quick Start

### 1. Start the Database

```bash
# From the VAF root directory
docker compose -f docker-compose.memory.yml up -d
```

This starts PostgreSQL 16 with the pgvector extension.

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

New dependencies for the memory system:
- `sqlalchemy[asyncio]` - Async ORM
- `asyncpg` - PostgreSQL async driver
- `pgvector` - Vector similarity extension
- `sentence-transformers` - Text embeddings
- `cryptography` - AES encryption

### 3. Initialize the Database

The database schema is automatically created when the memory system is first accessed. You can also initialize manually:

```python
from vaf.memory.database import init_db_sync
init_db_sync()
```

### 4. Access the Memory System

- **Web UI**: Navigate to `http://localhost:3000/memory`
- **API**: Available at `http://localhost:8000/api/memory/`

## Configuration

Settings are available in:
- **Settings Modal**: Memory System tab
- **Config file**: `~/.vaf/config.json`

| Setting | Default | Description |
|---------|---------|-------------|
| `memory_enabled` | `true` | Enable/disable memory system |
| `memory_db_url` | `postgresql://...` | Database connection URL |
| `memory_embedding_model` | `all-MiniLM-L6-v2` | Sentence-transformers model (384-dim) |
| `memory_chunk_size` | `512` | Chunk size in tokens |
| `memory_chunk_overlap` | `50` | Overlap between chunks |
| `memory_auto_connect_threshold` | `0.7` | Similarity threshold for auto-connections |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/memory` | POST | Create a new memory |
| `/api/memory` | GET | List memories |
| `/api/memory/{id}` | GET | Get memory by ID |
| `/api/memory/{id}` | PUT | Update memory |
| `/api/memory/{id}` | DELETE | Delete memory |
| `/api/memory/graph` | GET | Get graph visualization data |
| `/api/memory/rag/query` | POST | RAG query |
| `/api/memory/rag/query/stream` | POST | Streaming RAG query |
| `/api/memory/search` | POST | Semantic search |
| `/api/memory/stats` | GET | System statistics |
| `/api/memory/health` | GET | Health check |

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Frontend                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │MemoryGraph  │  │RagQueryPanel│  │MemoryDetailPanel    │ │
│  │ (ReactFlow) │  │ (Streaming) │  │ (View/Edit)         │ │
│  └─────────────┘  └─────────────┘  └─────────────────────┘ │
│                         │                                   │
│                    Zustand Store                            │
└─────────────────────────│───────────────────────────────────┘
                          │ WebSocket/REST
┌─────────────────────────│───────────────────────────────────┐
│                     Backend                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │ RAG Pipeline│  │ Graph Mgr   │  │ Crypto (AES-256)    │ │
│  └─────────────┘  └─────────────┘  └─────────────────────┘ │
│                         │                                   │
│              ┌──────────┴──────────┐                       │
│              │ Embedding Service   │                       │
│              │ (sentence-trans.)   │                       │
│              └──────────┬──────────┘                       │
└─────────────────────────│───────────────────────────────────┘
                          │
┌─────────────────────────│───────────────────────────────────┐
│                 PostgreSQL + pgvector                       │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │ memories    │  │ chunks      │  │ connections         │ │
│  │ (encrypted) │  │ (enc text + │  │ (graph edges)       │ │
│  │             │  │  vectors)   │  │                     │ │
│  └─────────────┘  └─────────────┘  └─────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

## Usage Examples

### Create a Memory

```python
from vaf.memory.database import get_db
from vaf.memory.rag import RagPipeline

async with get_db() as db:
    pipeline = RagPipeline(db)
    memory = await pipeline.ingest(
        content="Important information to remember...",
        metadata={"title": "My Note", "tags": ["work", "important"]}
    )
```

### RAG Query

```python
async with get_db() as db:
    pipeline = RagPipeline(db)
    result = await pipeline.query("What do I know about work?")
    print(result.answer)
    for source in result.sources:
        print(f"- {source.metadata['title']} ({source.score:.0%})")
```

### Semantic Search

```python
async with get_db() as db:
    pipeline = RagPipeline(db)
    sources = await pipeline.search("project deadline", k=5)
```

## Security

- **Encryption**: Memory TEXT is encrypted with AES-256-GCM at rest - the
  parent content (`memories.encrypted_content`) and the chunk texts
  (`chunks.text`, "enc:gcm:" field format). Decrypted on the fly when read.
- **Key Management**: The key is generated on first run and stored in config.
  A present-but-corrupt key is a hard error, never silently replaced (that
  would orphan all encrypted content). Back the key up separately from data.
- **Unencrypted by necessity**: embedding VECTORS (search needs them), tags,
  dates, and titles (no longer content-derived by default). Be aware that
  modern text embeddings are practically invertible - an attacker with
  database access can approximately reconstruct text from the vectors alone.
  Column encryption therefore protects against file/dump exposure, not
  against a fully compromised database. For complete at-rest protection use
  full-disk/volume encryption in addition.
- **Isolation**: fail-closed forced Row-Level Security on `memories` AND
  `chunks` (each row carries its owner scope; an unscoped session sees
  nothing), enforced for the non-superuser app role.

## Troubleshooting

### Database Connection Failed

1. Ensure Docker is running: `docker ps`
2. Check if postgres container is up: `docker compose -f docker-compose.memory.yml ps`
3. Verify connection URL in settings

### Embedding Model Loading Slow

The embedding model is downloaded on first use. Subsequent loads are cached.

### Memory Not Appearing in Graph

1. Wait for the auto-connect threshold to process
2. Refresh the graph using the refresh button
3. Check if the memory was created successfully in the API

## Development

### Running Tests

```bash
pytest tests/test_memory_store_tool.py tests/test_working_memory.py -v
```

### Extending the System

1. **Custom Embedding Models**: Implement `EmbeddingService` interface
2. **Additional Metadata**: Extend the `Memory` model in `models.py`
3. **Custom Node Types**: Add to `MemoryGraph.tsx` node types

## License

Dual-licensed: GNU AGPL-3.0-or-later or a Commercial License. See [LICENSE](../../LICENSE),
[LICENSING.md](../../LICENSING.md), and [COMMERCIAL.md](../../COMMERCIAL.md) in the project root.
