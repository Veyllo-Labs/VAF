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
- **Session Compaction**: Background process that every N user turns prompts the LLM to write durable memories (MEMORY:/NO_REPLY) into RAG. The model sees only a user/assistant dialogue excerpt (no system or tool messages). See [Session Compaction (background)](#session-compaction-background).
- **Document learning**: The agent can learn a document (PDF, TXT, MD) via the `learn_document` tool; one tag per document, one contextual LLM summary per section. See [Document memories (learn_document)](#document-memories-learn_document).

### Self-learning behavior

The memory system is **self-learning**: it improves with use. The more you chat (Web UI), the more the framework learns about you and your context.

- **Automatic learning:** Every N user turns (default 15), [session compaction](#session-compaction-background) runs: the LLM is given a recent conversation excerpt and writes durable facts into RAG (preferences, decisions, events, follow-ups). No manual saving is required; normal chat is the main source of long-term memory.
- **Explicit saves:** The agent can call the `memory_save` tool during a conversation to store important information immediately (e.g. after you state a preference or share a detail).
- **Document learning:** The agent can ingest a document via the `learn_document` tool (one tag per document, one contextual LLM summary per section); see [Document memories (learn_document)](#document-memories-learn_document).
- **Attachment retrieval lane:** Web UI attachments are indexed in a separate ephemeral lane (`attachment_ephemeral`) scoped by `session_id + user_scope_id` (TTL-based). This lane is used for active attachment Q&A and is excluded from normal long-term RAG by default.
- **Attachment → long-term transfer:** Use `learn_attached_knowledge` (explicit confirmation required) to persist selected attachment knowledge into long-term memory as `type=knowledge`.
- **Better recall over time:** RAG retrieval runs before each reply. As more memories are stored (from compaction and `memory_save`), semantic search returns more relevant context, so answers become more personalized and consistent across sessions.
- **Scope:** Learning is per user (`user_scope_id`). Only the main user’s Web UI conversations are compacted; contact chats (Telegram, WhatsApp, Discord) are not written to RAG for data protection.

#### User isolation (retrieval is per-user and fails closed)

Memory **retrieval** is per-user, not just writes. Both retrieval lanes — the vector lane and the lexical/hybrid lane — filter on `Memory.user_scope_id == user_scope_id` (the caller's scope). The isolation is **fail-closed**: a missing or empty scope returns **no results**, never a "search all" fallback, and an unparseable scope is treated as a deny rather than a wildcard.

Server-vs-local resolution: `run_memory_search_sync` denies (`RAG_DENY` / `SEARCH_DENIED`) for a missing scope in server mode, and floors to the local-admin scope only in single-user/local mode. See [USER_ISOLATION.md](../security/USER_ISOLATION.md) for the full isolation model.

See [Session Compaction (background)](#session-compaction-background) and the `memory_save` / `memory_search` tools for implementation details. For a high-level overview of self-learning in VAF (including future extensions), see [SELF_LEARNING.md](SELF_LEARNING.md).

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
│  │  │(encrypted)│ │(vectors) │  │   (graph edges)      │   │    │
│  │  └──────────┘  └──────────┘  └──────────────────────┘   │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

### Chat integration (pre-generation injection)

In chat, retrieval runs in the **input phase** before the LLM is called: the user message is used to run a memory search, and the result is injected as the "Memory context (relevant to this query)" block in the first prompt. When `memory_rag_refine_query` is enabled (default), short queries that look like user-profile questions (e.g. "who am I", "what do you remember") are expanded before search so that profile and compaction memories match more often. The `memory_search` tool is for follow-up short queries only; it must not be given model output (e.g. `<think>` content). See [CONTEXT_MANAGEMENT.md](CONTEXT_MANAGEMENT.md#rag-and-memory-context-pre-generation-injection) for details.

**Note:** This Memory System (RAG) is separate from the main agent’s **working memory** (scratchpad: notes, plan, tasks in `.vaf/main/working_memory.json`). Working memory is per-session state with limits and optional timestamps; see [CONTEXT_MANAGEMENT.md](CONTEXT_MANAGEMENT.md) for the persistent layer and working memory behaviour.

**Resume compaction:** Context compression/checkpoints can append a deterministic resume block after the normal compressed summary. This block is rule-based (no LLM call) and exposes operational fields such as current work, pending work, key files, tools used, decisions, and next action. It is controlled by `resume_compaction_enabled` (default `true`) in `config.json`.

## Requirements

- **Docker** (required for PostgreSQL + pgvector + Redis)
- **Python 3.10-3.13**
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

The Memory System is enabled by default. There is no UI toggle — it stays on unless you opt out by setting `memory_enabled: false` in `~/.vaf/config.json`.

### 3. Access the Memory Graph

- **Via Settings**: the **View Graph** button in Settings → Persona (RAG / memory section)
- **Direct URL**: `http://localhost:3000/memory`

## Configuration

Memory settings live in `~/.vaf/config.json` (the Memory System has no UI toggle — it is on by default):

| `config.json` key | Default | Description |
|---------|---------|-------------|
| `memory_enabled` | `true` | Enable/disable the entire memory system |
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
    "memory_compaction_interval": 15,
    "memory_compaction_max_tokens": 4000
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `memory_rag_k` | `5` | Max RAG snippets per query (1–20). Configured in Settings → Persona & Memory. |
| `memory_rag_threshold` | `0.3` | Min relevance score (0.0–1.0). Only snippets with relevance ≥ this value are included (e.g. 0.3 = 30%). Configured as "Min Relevance %" in Settings → Persona & Memory. |
| `memory_rag_refine_query` | `true` | Expand short user-profile-style queries (e.g. "who am I", "preferences") before search to improve recall. Set to `false` to use the raw user message only. |
| `memory_hybrid_enabled` | `true` | Enable long-term RAG hybrid retrieval (vector + lexical) with RRF fusion. |
| `memory_hybrid_rrf_k` | `60` | Reciprocal Rank Fusion denominator constant (higher values smooth rank impact). |
| `memory_hybrid_lexical_k` | `20` | Max lexical candidates retained before fusion. |
| `memory_hybrid_lexical_scan_limit` | `400` | Max lexical chunk rows scanned for hybrid retrieval. |
| `memory_hybrid_lexical_min_score` | `0.05` | Min lexical score required before fusion. Conservative floor to remove zero-overlap lexical noise while preserving recall for RRF fusion. |
| `memory_compaction_interval` | `15` | Run session compaction every N user turns (cumulative per session). |
| `memory_compaction_max_tokens` | `4000` | Max tokens for the compaction LLM reply (API, local LLM, and server mode). Allows more `MEMORY:` lines per run. |
| `resume_compaction_enabled` | `true` | Append a deterministic resume block after context compression and `checkpoint_context` so long sessions resume more predictably. |

### Attachment Lane Retrieval Tuning (config.json)

Use separate thresholds for vector and lexical scoring in the attachment lane:

| Key | Default | Description |
|-----|---------|-------------|
| `attachment_rag_enabled` | `true` | Enable the attachment-specific RAG lane used for session-scoped uploaded documents. |
| `attachment_rag_threshold` | `0.28` | Vector similarity threshold used by attachment vector retrieval (cosine-like relevance scale). |
| `attachment_rag_lexical_min_score` | `0.05` | Lexical score floor used by attachment lexical retrieval (safe mode + hybrid lexical candidate filtering). Keeps lexical scale independent from vector scale. |
| `attachment_rag_safe_mode` | `false` | When true, attachment retrieval uses the bounded lexical safe lane (no embedding/pgvector path). Default is now `false` (vector mode); see the note below. |
| `attachment_rag_hybrid_enabled` | `true` | In vector mode, combine vector + lexical candidates with RRF fusion. |
| `attachment_rag_hybrid_lexical_k` | `16` (dynamic default) | Max lexical candidates retained before fusion in attachment hybrid mode. |
| `attachment_rag_hybrid_lexical_scan_limit` | `96` | Max attachment rows scanned for lexical candidates before filtering/ranking. |
| `attachment_rag_hierarchical_enabled` | `true` | On by default. Two-tier hierarchical indexing for large structured documents (vector mode only; see below). |
| `attachment_rag_hierarchical_min_chars` | `4000` | Minimum document length in characters to activate hierarchical indexing. Shorter docs use flat chunking. |
| `attachment_rag_hierarchical_max_sections` | `15` | Maximum number of sections indexed per document. |
| `attachment_rag_hierarchical_coarse_k` | `3` | Number of sections selected in the Tier 1 coarse search before Tier 2 chunk search. |

Practical guidance:
- Start with `attachment_rag_lexical_min_score=0.05` as a conservative floor.
- Raise only if lexical-only noise starts dominating fused top-k.
- Keep `attachment_rag_threshold` and `attachment_rag_lexical_min_score` independent; they are different score scales.

**Why vector mode is the default.** Earlier the attachment vector path could trigger runaway RSS growth under repeated index/search/clear loops, so the bounded lexical "safe mode" was the conservative default. The root cause turned out to be an infinite loop in the text chunker's tail handling (unbounded chunk creation), **not** the embedding model or pgvector — it is fixed in `TextChunker.chunk()` (`vaf/memory/embeddings.py`, with a `reached_end`/`end >= len(text)` break plus a non-increasing-`start` guard). The vector and hierarchical paths were then verified stable (RSS stays flat over long index/search/clear runs), so vector mode is now the default. Set `attachment_rag_safe_mode=true` to force the lexical fallback. Note that the vector path requires the pgvector database (`vaf-memory-db`) to be running, like the rest of the memory system.

### Hierarchical Attachment Indexing

When `attachment_rag_hierarchical_enabled=true`, large structured documents (patents, contracts, financial reports) are indexed using a **two-tier hierarchy** instead of flat chunking. This prevents hallucinations caused by chunks with no structural context.

**How it works:**

1. **Section detection** — the document is split into sections using markdown headers (`## Title`), page markers, or paragraph breaks. Sections shorter than 500 chars are merged; sections longer than 5 000 chars are split at sentence boundaries. For PDF attachments these Markdown headings are produced by the shared extractor (`vaf/core/pdf_extract.py`), which infers headings from font size and renders tables as Markdown; without it PyPDF2 yields unstructured text and only page-marker sections are found.
2. **Section summaries (Tier 1)** — one LLM call per section generates a 1-2 sentence summary. Each section is stored as a `Memory` row whose embedding encodes the summary. If the LLM call fails, the first 300 chars of the section are used as a fallback.
3. **Chunk index (Tier 2)** — the section's full text is chunked normally (512-token chunks, 50-token overlap) and stored as `Chunk` rows linked to the section Memory.

**Retrieval:**
- Query is embedded → cosine search over **section embeddings** → top `coarse_k` sections selected
- Cosine search over **chunks** scoped to those sections only → top-k chunks returned
- Each result includes `[Section Title]\n<section summary>\n\n<chunk text>` so the LLM gets structural context alongside the raw excerpt

**Fallback chain** — hierarchical mode is silently bypassed and flat chunking is used when:
- `attachment_rag_safe_mode=true` (safe mode always takes precedence)
- Document length < `attachment_rag_hierarchical_min_chars`
- Fewer than 2 sections detected (plain prose, no structure)
- Any error during hierarchical ingest or retrieval

**Requirements:** vector mode (`attachment_rag_safe_mode=false`, the default since the chunker fix). Recommended to raise `attachment_rag_op_timeout_sec` to ≥ 60 for documents with many sections, as section summary LLM calls run during ingest.

### Session Compaction (background)

**Session Compaction** is an automatic background process that periodically asks the LLM to write durable memories from the current session into RAG. It does not append anything to the chat UI.

- **When:** Compaction runs **only for the main user** (Web UI chats). It does **not** run for contact chats (Telegram, WhatsApp, Discord) for data-protection reasons (DSGVO). After every **Web UI** chat task, the headless runner checks whether that session has reached the compaction interval (number of user turns since last compaction). Default: every **15 user turns** (`memory_compaction_interval`). The count is cumulative per session; only messages with role `user` are counted.
- **What:** A single non-streaming LLM call with a prompt like “Store durable memories now. Write any lasting notes to memory/{date}.md. Output MEMORY: \"...\" lines or NO_REPLY.” The prompt includes a **conversation excerpt (user and assistant messages only; no system prompts or tool calls)**. Reply format: `MEMORY: "content" [tag1, tag2]` (tags optional but recommended) or NO_REPLY. Parsed lines are ingested with metadata: `type=conversation`, `source=memory/{date}`, `tags` (orange in memory graph). Then the user-profile summary cache is refreshed. Max reply length: `memory_compaction_max_tokens` (default 4000).
- **Grounding constraint:** the prompt instructs the model to store **only** facts the user stated explicitly or that are directly evidenced in the excerpt. It must not infer or invent habits, routines, schedules, numbers, or preferences that were not stated, and must not convert an exploratory or philosophical remark into a durable preference. This prevents a thin or speculative remark being written back as a hard "fact" that later retrieval treats as ground truth.
- **Quality rules (prompt, added after a live review of a learning run):** each fact must be **self-contained** (retrieval returns facts individually, so subjects are named explicitly - "patent US...", never "the patent"); relative time must be converted to **absolute dates** and drifting snapshot facts (finances, funding status, plans) must carry "as of {date}" in the fact text (the prompt now states today's date); short-lived **conversation state and open todos are not memories** ("has not sent the email yet"); long-established basics (name, company) are not re-stored unless changed.
- **Model-independent gates (`_apply_fact_gates`, between parse and ingest):** length bounds 15-500 chars, junk-marker rejection (NO_REPLY echoes, `<think`, injected-context markers, meta commentary, nested `MEMORY:` lines) and a hard cap of 12 facts per run - a weak model cannot flood the store. Rejections are logged per fact (`COMPACTION_FACT_REJECT` with reason). Additionally, each surviving fact runs a **dedup check** before ingest (chunk-level similarity search, threshold 0.95, same primitive as the auto-capture lane and the same singleton embedding service): near-identical existing facts are skipped and logged as `COMPACTION_DEDUP_SKIP`. Dedup is best-effort and never blocks ingestion on errors.
- **Reasoning-trace stripping:** before the reply is parsed, `_parse_memory_reply` runs `_strip_think_reply` to remove `<think>…</think>` reasoning blocks (and drop an unclosed `<think>` tail). A reasoning model (e.g. `deepseek-v4-pro`) drafts inside `<think>` first, so without this its raw reasoning — and any `MEMORY:` line it merely drafted *inside* the reasoning — would be persisted verbatim. The write path now matches the query and `learn_document` paths, which already strip reasoning.
- **Context:** The conversation excerpt passed to the LLM contains **only user and assistant messages** (no system prompt, no tool calls or tool results). Built from the last N messages in session history, truncated by character limit (~12k chars).
- **Where:** Logic in `vaf/memory/rag.py` (`run_session_compaction_sync`); triggered from `vaf/core/headless_runner.py` (after each chat, or enqueued as a separate task when using a local LLM so only one LLM call runs at a time). State per session: `~/.vaf/compaction_state.json` (last compaction turn per `session_id`).
- **Contact chats (Telegram/WhatsApp/Discord):** Compaction is **disabled** for these sessions. Only the main user’s Web UI session is compacted into long-term memory. Contact conversations are never written to RAG (DSGVO-friendly).
- **Config:** `memory_compaction_enabled` (default `true`), `memory_compaction_interval` (default `15`), `memory_compaction_max_tokens` (default `4000`, used for API, local LLM, and server mode). All require `memory_enabled` to be `true`.
- **Logs:** Compaction events are written to **memory.log** (same directory as other app logs: `VAF_LOG_DIR`, repo `logs/`, or platform data dir). Each line is prefixed with `[COMPACTION]` and includes: `COMPACTION_SKIP` (interval not reached), `COMPACTION_START`, `COMPACTION_NO_REPLY`, `COMPACTION_FACT_REJECT` (gate rejection with reason + preview), `COMPACTION_DEDUP_SKIP` (near-duplicate skipped), `COMPACTION_DONE` (with `memories=N deduped=N rejected=N`), `COMPACTION_LLM_FAIL`, `COMPACTION_INGEST_FAIL`. All lines have an ISO timestamp at the start. The headless runner also writes `QUEUE_DONE session_id=... (compaction)` to `queue.log` when the compaction task finishes.

**Log structure:** One file per domain under the same log directory. **rag.log**: RAG timing, search, embed calls, snippet count, user scope. **memory.log**: compaction, RSS usage, embedding load, profiler, Whisper load. Each line starts with an ISO timestamp. In memory.log, the prefix on each line (`[COMPACTION]`, `[USAGE]`, `[EMBED]`, `[PROFILER]`, `[WHISPER]`) indicates the source.

### Tag Links

**Tag links** create bidirectional associations between tags. When tags A and B are linked:

- All memories with tag A automatically get tag B (and vice versa)
- New memories saved with tag A get tag B added; new memories with tag B get tag A added
- Applies to memory_save, compaction ingest, and manual tag adds

**Creation:**
- In the memory graph: drag from one tag node to another
- Via API: `POST /api/memory/tag-links` with `{"tag_a": "...", "tag_b": "..."}`
- Via UI: "Link Tags" button → enter two tags and click Link

**Storage:** Tag links are stored in `~/.vaf/tag_links.json` (or Docker config dir). No database migration required.

**Sync:** When a link is created, existing memories with either tag are updated to include the other tag.

### Document memories (learn_document)

The agent can **learn a document** into long-term memory via the **`learn_document`** tool. Use it when the user asks to "learn", "remember", or "ingest" a document (e.g. a PDF or text file) so the agent can answer questions about it later.

- **Input:** File path (required) and optional `document_title` (e.g. "Tora"). Supported formats: PDF, TXT, MD.
- **One tag per document:** All memories from that run share a single tag derived from the title (e.g. `doc-tora`). In the memory graph, one tag node is linked to many purple document nodes (one per section).
- **One memory per section (contextual):** The document is extracted to Markdown and split into sections (by headings / page markers / paragraphs). For each section, one **LLM call** produces a contextual summary; that summary becomes the memory **title** — which drives the embedding/retrieval key in `RagPipeline.ingest` — and is prepended to the section text before storage. A single `document_index` root memory holds the document summary, so it is not repeated on every section.
- **Scoping:** Uses the current user’s `user_scope_id` (same as `memory_save`). Paths must be under an allowed root (home, cwd, or VAF data dir).
- **Config (optional in `config.json`):** `learn_document_max_pages` (default 200) caps how much of the document is read; `learn_max_sections` (default 40) caps how many sections are stored.

Implementation: `vaf/tools/learn_document.py`; ingestion uses the same `RagPipeline.ingest()` as other memories with `type=document`, `source=learn_document`, and `tags=[doc-<title>]`. See the memory graph legend: **Document** = purple.

## API Reference

**Authentication and scope.** In server mode the memory endpoints derive `user_scope_id` via `get_current_user_scope`, and an unauthenticated request **fails closed** — it returns no results. A valid access JWT is required to see your own memories; the scope is taken from the token, never from the request body. The plain unauthenticated `curl` examples below only return data in single-user/local mode, where the endpoints fall back to the local-admin scope.

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/memory` | POST | Create a new memory |
| `/api/memory` | GET | List memories (paginated) |
| `/api/memory/{id}` | GET | Get memory by ID (decrypted) |
| `/api/memory/{id}` | PUT | Update memory content/metadata |
| `/api/memory/{id}` | DELETE | Delete memory (soft/hard) |
| `/api/memory/graph` | GET | Get graph data for visualization |
| `/api/memory/rag/query` | POST | Long-term RAG query (returns answer + sources; excludes ephemeral attachment lane by default) |
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
        "type": "note|document|code|conversation|knowledge",
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

Memory TEXT is encrypted at rest using AES-256-GCM - both the parent content
(`memories.encrypted_content` + `nonce` columns) and every chunk text
(`chunks.text`, stored in the `enc:gcm:<nonce>:<ciphertext>` field format and
decrypted on read; a startup migration encrypts legacy plaintext rows). The
user-profile prompt cache on disk is encrypted the same way.

- **Key Generation**: 32-byte random key, auto-generated on first use
- **Storage**: Key stored Base64-encoded in config (consider using system keyring for production)
- **Key safety**: a PRESENT but corrupt/wrong-length key is a hard startup
  error and is never silently replaced - a silent regenerate would
  permanently orphan every encrypted row. Back the key up separately.
- **Nonce**: Unique 12-byte nonce per encryption
- **Unencrypted by necessity**: embedding vectors (similarity search operates
  on them), tags, dates, and titles (no longer content-derived by default;
  content previews are no longer persisted in meta at all)

**Residual risk, stated honestly**: text embeddings are practically
invertible - published attacks reconstruct short factual sentences from the
vectors with high fidelity. Column encryption therefore raises the bar
against file/backup/dump exposure but does NOT protect against an attacker
with full database access; the fail-closed RLS on `memories` and `chunks`
is the control for that layer, and full-disk/volume encryption is the
recommended complement for complete at-rest protection.

The SQL lexical search lane no longer pre-filters on chunk text (it cannot
match ciphertext): candidate rows are fetched under the scan limit and
scored app-side on the decrypted text - same semantics, the SQL `ilike`
optimization is gone. Query tokens are additionally filtered against the
vocabulary-book `stopwords` lists before scoring: without this, a natural
question ("Kannst du dich noch an Kai erinnern?") diluted its one signal
word to 1/7 of the score while a bare "Kai" query scored 1.0. A query
consisting only of function words keeps its tokens (never emptied).

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

## Embedding Engine (ONNX Optimized)

VAF uses a highly optimized embedding pipeline to minimize resource usage while maintaining high retrieval accuracy.

### Architecture
- **Runtime:** **ONNX Runtime (CPU)**
  - Replaces heavy PyTorch/Sentence-Transformers dependencies.
  - **Performance:**
    - **RAM Usage:** ~200 MB (vs. ~1.5 GB with PyTorch).
    - **Startup Time:** < 1 second (vs. 10-200 seconds depending on system load).
    - **Inference:** ~100ms per query on standard CPUs.
- **Model:** `Xenova/all-MiniLM-L6-v2` (Quantized)
  - 384-dimensional dense vectors.
  - Automatically downloaded from HuggingFace Hub on first launch.
- **Tokenizer:** Rust-based `tokenizers` library for sub-millisecond tokenization.

### Optimization Strategy
1.  **Lazy Loading:** The embedding model is loaded only when the first RAG request occurs or background indexing starts.
2.  **Quantization:** Uses int8 quantization to reduce model size without significant accuracy loss.
3.  **Non-Blocking:** Embedding operations are offloaded to avoid blocking the main event loop, ensuring the UI remains responsive even during heavy indexing.

### Supported Models
While `all-MiniLM-L6-v2` is the default, the system is compatible with other ONNX-exported models.
- **Multilingual:** `intfloat/multilingual-e5-small` (requires `memory_embedding_model` config update).

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

### Attachment-RAG Memory Spike (Resolved Root Cause)

If Attachment-RAG vector indexing shows rapid RSS growth under repeated loops (for example index/search/clear stress), check the chunking path first.

Root cause fixed in this codebase:
- A chunk-tail edge case could keep `start` below `len(text)` forever when overlap was applied at end-of-text.
- This created an effectively unbounded chunk loop, which then amplified memory usage when chunks were embedded and written.

What is now implemented:
- End-of-text hard break in `TextChunker.chunk()`.
- Non-progress guard (`next_start <= start`) with warning log, so future overlap regressions fail safe instead of looping forever.

Operational debugging checklist (fast isolation):
1. Run three isolated tests with the same guard window:
   - ONNX-only embedding loop
   - pgvector/DB-only insert loop
   - full `RagPipeline.ingest()` loop
2. Compare peak RSS:
   - if only full ingest explodes, inspect chunking/embedding handoff and per-phase ingest logs.
3. Enable phase logs for ingest:
   - set `memory_ingest_profile_enabled = true` and inspect `INGEST_PROFILE` lines in `rag_YYYY-MM-DD.log`.

This incident showed that component-level stability can still fail in composition. Use isolation early before tuning allocator or thread settings.

## Security Considerations

1. **Change default password** in production (`POSTGRES_PASSWORD` in docker-compose)
2. **Use system keyring** for encryption key storage instead of config file
3. **Network isolation**: The Docker container only exposes port 5432 to localhost
4. **Backup encryption keys** separately from data backups
5. **User isolation — defense in depth, enforced and fail-closed at the database**: `get_db(user_scope_id=...)` sets the per-transaction `app.current_user_scope_id` GUC, and PostgreSQL Row-Level Security on the `memories` table (the `user_isolation_memories` policy) enforces it. The policy is **fail-closed**: a row is visible or writable only when its `user_scope_id` equals the GUC; an unset or empty GUC matches nothing (deny), and a row whose `user_scope_id` is NULL is not blanket-visible. RLS is `ENABLE`d and `FORCE`d on `memories`, and the application data connection uses a non-superuser role (`vaf_app`, `NOBYPASSRLS`) via `memory_db_url`, so RLS is actually enforced for every memory data path. A separate owner connection (`memory_db_owner_url`, superuser role `vaf`) is used only for DDL, migrations, and global maintenance. The application-layer scope filter (see [User isolation](#user-isolation-retrieval-is-per-user-and-fails-closed)) remains the first line of defense; database RLS is an independent, fail-closed second guard. `chunks` additionally carries its **own** `user_scope_id` column and the same forced fail-closed policy (`user_isolation_chunks`): chunk rows hold the searchable text and the embedding vectors (which are practically invertible back to text), so relying on the join to `memories` alone would leave direct chunk access unprotected. The column is stamped at ingest and backfilled by DB migration v2. `connections` has no policy of its own — its queries join `memories`.

## Performance

- **Vector Index**: HNSW (Hierarchical Navigable Small World) for fast approximate nearest neighbor search
- **Batch Embedding**: Multiple texts embedded in parallel
- **Connection Pooling**: SQLAlchemy async with connection reuse
- **Caching**: Embedding results cached in-memory (LRU, 1000 entries)

Typical performance:
- Memory creation: ~500ms (including chunking, embedding, storage)
- RAG query: ~1-2s (embedding + search + LLM)
- Graph load (100 nodes): ~200ms
