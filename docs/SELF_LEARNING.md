# Self-Learning in VAF

This document describes how VAF learns from usage and improves over time. It is the central place to understand the **current** self-learning mechanisms and to **add new ones** as the framework is extended.

## What “self-learning” means here

**Self-learning** in VAF means: the system gets better with use without manual configuration. User interaction (chat, tools, automations) produces durable state that is reused in later runs, so the agent’s behavior becomes more personalized and consistent.

- **Current scope:** (1) Long-term memory (RAG): facts, preferences, and context from conversation. (2) User profile: name, language, location, do’s/don’ts, and preferences—the model updates this from what the user says so it knows the user better over time. (3) Attachment-scoped retrieval lane (ephemeral, session + user scoped) with optional transfer to long-term memory.
- **Future scope:** Additional subsystems (e.g. workflow patterns, feedback loops) can be documented in this file as they are added.

---

## Current self-learning systems

### 1. Long-term memory (RAG)

The main self-learning component today is the **Memory System** (RAG). It improves with every conversation.

| Mechanism | What it does | Where it’s documented |
|-----------|--------------|------------------------|
| **Session compaction** | Every N user turns (default 15) in the Web UI, the LLM is asked to extract durable memories from the recent dialogue. Those are stored in RAG. No user action required. | [MEMORY_SYSTEM.md – Session Compaction](MEMORY_SYSTEM.md#session-compaction-background) |
| **memory_save tool** | The agent can store a fact or preference immediately when the user shares it (e.g. “I prefer meetings on Tuesday”). | [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md); tool usage in agent/system prompt |
| **Pre-reply retrieval** | Before each reply, the user message (and optionally a refined query) is used to run semantic search over RAG. The top snippets are injected as “Memory context” in the prompt. | [CONTEXT_MANAGEMENT.md – RAG and memory context](CONTEXT_MANAGEMENT.md#rag-and-memory-context-pre-generation-injection) |

**Effect over time:** More chats → more memories in RAG → better retrieval → more relevant context in each reply. Learning is **per user** (`user_scope_id`). Only the main user’s Web UI conversations are compacted; contact chats (Telegram, WhatsApp, Discord) are not written to RAG (data protection).

**Implementation:** `vaf/memory/rag.py` (compaction, ingest, search), `vaf/core/headless_runner.py` (compaction trigger), tools `memory_save` / `memory_search`.

### 2. User profile (identity)

The **user profile** (`user_identity.json`) is a structured description of the current human user: name, preferred language, location (city/country), timezone, date/time format, **preferences**, **do’s**, and **don’ts**. The model learns the user better by updating this profile when the user says things like “call me Mert”, “I prefer German”, “always be concise”, or “don’t use emojis”.

| Mechanism | What it does | Where it’s documented |
|-----------|--------------|------------------------|
| **update_user_identity tool** | The agent updates the user profile when the user states their name, language, location, rules (do’s/don’ts), or preferences. Parameters include `add_preference`, `add_do`, `add_dont`, `name`, `language`, `city`, `country`, `main_messenger`, `timezone`, etc. | [USER_IDENTITY.md – Tool: update_user_identity](USER_IDENTITY.md#tool-update_user_identity) |
| **System prompt injection** | Every turn, the “User identity (current user)” block is built from `user_identity.json` and injected into the system prompt, so the model always sees the latest name, preferences, do’s, and don’ts. | [USER_IDENTITY.md – System prompt injection](USER_IDENTITY.md#system-prompt-injection) |

**Effect over time:** As the user interacts, the agent fills in and refines the profile (name, language, do’s/don’ts, preferences). Later replies automatically respect these rules and preferences because they are in the system prompt every turn. Learning is **per user** (per username / `user_identity.json`).

**Implementation:** `vaf/tools/user_identity.py` (`UpdateUserIdentityTool`), `vaf/auth/user_workspace.py` (read/write `user_identity.json`), `vaf/core/system_prompt.py` (injection of user identity block).

### 3. Document learning (learn_document)

When the user provides a **document** (PDF, TXT, or MD), the agent can learn it into RAG via the **`learn_document`** tool. The document is split by page or section; for each part, a short LLM call extracts key facts; each extraction is stored as one memory with `type=document` and a single document tag (e.g. `doc-tora`). The agent can then answer questions about the document using retrieval.

| Mechanism | What it does | Where it's documented |
|-----------|--------------|------------------------|
| **learn_document tool** | The agent reads the file, splits by page/section, runs an extraction LLM call per part, and ingests each result as a document memory with a shared tag. | [MEMORY_SYSTEM.md – Document memories](MEMORY_SYSTEM.md#document-memories-learn_document) |

**Effect over time:** User-provided documents become queryable knowledge; the memory graph shows one tag per document with many purple document nodes. Learning is per user (`user_scope_id`).

**Implementation:** `vaf/tools/learn_document.py`; `vaf/core/agent.py` (`_generate_for_document_extraction`).

### 4. Attachment-scoped learning lane (ephemeral + transfer)

For active Web UI work with attached documents, VAF uses a **separate ephemeral retrieval lane**. This keeps temporary attachment context separate from long-term memory.

| Mechanism | What it does | Where it's documented |
|-----------|--------------|------------------------|
| **Attachment index (ephemeral)** | On sidebar document updates, extracted content is indexed with `source=attachment_ephemeral`, scoped by `session_id + user_scope_id`, and expires via TTL (default 24h). Retrieval injects top-k snippets into the current turn. | [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md), [WEBUI_WEBSOCKET_FLOW.md](WEBUI_WEBSOCKET_FLOW.md) |
| **learn_attached_knowledge tool** | Transfers selected attachment knowledge into long-term memory only after explicit confirmation (`confirm_learn=true`). Created memories use `type=knowledge` and origin metadata (attachment transfer). | [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md), tool `learn_attached_knowledge` |

**Effect over time:** During active sessions, large attachments are handled via snippet retrieval (not full prepend), reducing token pressure. Important knowledge can be promoted intentionally into durable memory as `knowledge` nodes.

**Implementation:** `vaf/memory/attachment_rag.py`, `vaf/core/web_server.py`, `vaf/core/headless_runner.py`, `vaf/tools/learn_attached_knowledge.py`, `vaf/memory/rag.py` (lane separation), memory graph type mapping in `vaf/memory/graph.py` and `web/components/memory/MemoryGraph.tsx`.

---

## Extending the self-learning system

When you add a **new** self-learning mechanism (e.g. learning from feedback, from automation outcomes, or from tool-usage patterns), add it here so the picture stays complete.

**Template for a new subsection:**

```markdown
### N. [Name of the subsystem]

| Mechanism | What it does | Where it’s documented |
|-----------|--------------|------------------------|
| **…** | … | Link to detailed doc or code |

**Effect over time:** …

**Implementation:** Main modules or entry points.
```

Then:

1. Add a row to the table in **Current self-learning systems** (or a new subsection under it).
2. Optionally add a dedicated doc (e.g. `SOME_FEATURE.md`) and link it from here.
3. If it affects configuration or UX, update [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md), [CONTEXT_MANAGEMENT.md](CONTEXT_MANAGEMENT.md), or the relevant feature doc.

---

## See also

- [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md) — RAG architecture, compaction, API, configuration
- [CONTEXT_MANAGEMENT.md](CONTEXT_MANAGEMENT.md) — How memory context is injected into the prompt
- [USER_IDENTITY.md](USER_IDENTITY.md) — User profile (name, do’s/don’ts, preferences); the agent updates it via `update_user_identity` so the model learns the user better over time
